#!/usr/bin/env python3
"""Look for personal data and secrets in the files git tracks.

  data_guard.py [--root .] [--allow .data-guard-allow] [paths...] > data.json

Prints a JSON list of findings: {file, line, kind, masked, hash}. The value
itself is never printed (logs of a public repository are public too): only a
masked form and a SHA-256 of it. A value that is fine to publish (the
business phone on the contact page, a public e-mail) goes into the allow file,
one hash per line, with a comment saying what it is.

What counts:
  data-file   a file with the shape of data (.csv, .xlsx, .sql, .db, .ofx,
              .pdf, .json other than config files)
  cpf, cnpj   Brazilian ids, formatted, with valid check digits
  phone       Brazilian mobile number (55 + area code + 9 + 8 digits, or
              "(61) 9xxxx-xxxx")
  email       an e-mail address outside example/noreply domains
  secret      private key header or a token with a known prefix
"""
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys

DATA_EXT = {".csv", ".tsv", ".xlsx", ".xls", ".ods", ".sql", ".db", ".sqlite", ".ofx", ".pdf",
            ".json", ".jsonl", ".vcf", ".bak"}
CONFIG_JSON = re.compile(r"(^|/)(package(-lock)?|tsconfig[\w.-]*|jsconfig|manifest|lighthouserc|"
                         r"renovate|\.eslintrc|composer|deno|wrangler|vercel|firebase|"
                         r"\.?prettierrc|turbo|nx|app|bower)\.json$")
SKIP_DIRS = ("node_modules/", ".git/")
MAX_BYTES = 5 * 1024 * 1024  # bigger text files are generated, not typed by someone
THIRD_PARTY = re.compile(r"(^|/)vendor/|\.min\.(js|css)$")
# Test fixtures use made-up mobile numbers 9 0000-xxxx; nobody has one.
FAKE_PHONE = re.compile(r"^9000")
EMAIL_NOT = re.compile(r"^(s\.whatsapp\.net|lid|g\.us|c\.us|broadcast)$", re.I)
TEXT_EXT = {".html", ".htm", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".css", ".md", ".txt",
            ".json", ".yml", ".yaml", ".toml", ".xml", ".svg", ".webmanifest", ".csv", ".tsv",
            ".sh", ".py", ".env", ".ini", ".cfg", ".vcf", ".sql"}

CPF = re.compile(r"(?<![\d.])(\d{3})\.(\d{3})\.(\d{3})-(\d{2})(?![\d-])")
CNPJ = re.compile(r"(?<![\d.])(\d{2})\.(\d{3})\.(\d{3})/(\d{4})-(\d{2})(?![\d-])")
PHONE = re.compile(r"(?<!\d)(?:\+?55[\s-]?)?\(?([1-9]\d)\)?[\s-]?(9\d{4})[\s-]?(\d{4})(?!\d)")
PHONE_RAW = re.compile(r"(?<![\w])55([1-9]\d)(9\d{8})(?!\d)")
EMAIL = re.compile(r"(?<![\w.+-])([\w.+-]{1,64})@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,})")
EMAIL_OK = re.compile(r"(^|\.)(example\.(com|org|net)|exemplo\.com(\.br)?|users\.noreply\.github\.com|"
                      r"noreply\.[\w.]+|localhost|test|invalid)$", re.I)
SECRET = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----|\b(ghp|gho|ghs|github_pat)_[A-Za-z0-9_]{20,}|"
                    r"\bsk-[A-Za-z0-9_-]{20,}|\bAKIA[0-9A-Z]{16}\b|\bxox[bpas]-[A-Za-z0-9-]{10,}|"
                    r"\bAIza[0-9A-Za-z_-]{35}\b")


def cpf_ok(d):
    if len(set(d)) == 1:
        return False
    for n in (9, 10):
        s = sum(int(d[i]) * (n + 1 - i) for i in range(n))
        if (s * 10 % 11) % 10 != int(d[n]):
            return False
    return True


def cnpj_ok(d):
    if len(set(d)) == 1:
        return False
    for n in (12, 13):
        w = [6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2][13 - n:]
        s = sum(int(d[i]) * w[i] for i in range(n))
        r = s % 11
        if (0 if r < 2 else 11 - r) != int(d[n]):
            return False
    return True


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()[:16]


def mask(value):
    keep = 3 if len(value) > 8 else 1
    return value[:keep] + "*" * max(1, len(value) - 2 * keep) + value[-keep:]


def tracked(root, paths):
    try:
        out = subprocess.run(["git", "-C", root, "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", *paths],
                             capture_output=True, check=True).stdout.decode()
        return [f for f in out.split("\0") if f]
    except (subprocess.CalledProcessError, FileNotFoundError):
        files = []
        for p in paths or ["."]:
            for d, _, names in os.walk(os.path.join(root, p)):
                files += [os.path.relpath(os.path.join(d, n), root) for n in names]
        return files


def scan_text(rel, text):
    found = []
    for no, line in enumerate(text.splitlines(), 1):
        if len(line) > 5000:  # minified bundle: scan in pieces, line number stays
            pieces = [line[i:i + 5000] for i in range(0, len(line), 4900)]
        else:
            pieces = [line]
        seen = set()
        for piece in pieces:
            hits = []
            for m in CPF.finditer(piece):
                if cpf_ok("".join(m.groups())):
                    hits.append(("cpf", m.group(0)))
            for m in CNPJ.finditer(piece):
                if cnpj_ok("".join(m.groups())):
                    hits.append(("cnpj", m.group(0)))
            for m in list(PHONE.finditer(piece)) + list(PHONE_RAW.finditer(piece)):
                number = "".join(m.groups())
                if not FAKE_PHONE.match(number[2:]):
                    hits.append(("phone", "55" + number))
            for m in EMAIL.finditer(piece):
                if not (EMAIL_OK.search(m.group(2)) or EMAIL_NOT.match(m.group(2))):
                    hits.append(("email", m.group(0).lower()))
            for m in SECRET.finditer(piece):
                hits.append(("secret", m.group(0)))
            for kind, value in hits:
                if (kind, value) in seen:
                    continue
                seen.add((kind, value))
                found.append({"file": rel, "line": no, "kind": kind,
                              "masked": mask(value), "hash": digest(value)})
    return found


def scan(root, paths, allow):
    findings = []
    for rel in tracked(root, paths):
        if rel.startswith(SKIP_DIRS) or "/node_modules/" in rel or THIRD_PARTY.search(rel):
            continue
        ext = os.path.splitext(rel)[1].lower()
        if ext in DATA_EXT and not (ext == ".json" and CONFIG_JSON.search(rel)):
            findings.append({"file": rel, "line": 0, "kind": "data-file", "masked": "",
                             "hash": digest(rel)})
        if ext not in TEXT_EXT and os.path.basename(rel) not in (".env", ".dev.vars"):
            continue
        try:
            if os.path.getsize(os.path.join(root, rel)) > MAX_BYTES:
                continue
            text = open(os.path.join(root, rel), encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        findings += scan_text(rel, text)
    return [f for f in findings if f["hash"] not in allow]


def read_allow(path):
    if not path or not os.path.exists(path):
        return set()
    return {ln.split("#")[0].strip() for ln in open(path) if ln.split("#")[0].strip()}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("paths", nargs="*")
    p.add_argument("--root", default=".")
    p.add_argument("--allow", default=".data-guard-allow")
    a = p.parse_args()
    allow = read_allow(os.path.join(a.root, a.allow) if not os.path.isabs(a.allow) else a.allow)
    print(json.dumps(scan(a.root, a.paths, allow), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
