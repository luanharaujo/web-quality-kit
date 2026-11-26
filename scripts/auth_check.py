#!/usr/bin/env python3
"""Login check ("auth"): every system with a login follows the same policy.

  auth_check.py check [--config .quality/auth.json] [--root .] [--no-dynamic] > auth.json

The repository declares its login in `.quality/auth.json` (template:
templates/auth.json). Without the file, the check still looks for a login in
the source (navigator.credentials, a login-code table, /login routes, a
password input): a login found and not declared is a failure.

Static rules (read the source, no server):
  A1  password is forbidden unless the repository registers an exception; with
      one, the parameters must reach the minimums (Argon2id m>=19 MiB t>=2 p=1,
      scrypt N>=2^17 r=8 p=1, bcrypt cost>=12 legacy, PBKDF2-SHA256>=600k) and
      MD5/SHA-1/SHA-256 on a password, or a password column that is not a
      hash, fail
  A2  the Worker's `ratelimits` binding is called in every public login or
      sign-up route before any D1 use, and the binding is declared in wrangler
  A3  Turnstile is verified on the server (success, hostname, action) before
      the route does anything else
  A5  no account lock and no global ceiling (COUNT over a whole table)
  A6  the route logs a `login_attempt` event without phone, e-mail, IP,
      user agent, code or hash, and has no console.* with such data
Dynamic rules (run the server declared in "server" against a local siteverify
that answers like Cloudflare's public test keys; every route with "body"). The
app reads TURNSTILE_SECRET and TURNSTILE_VERIFY_URL (default Cloudflare's):
  A3  no token, a refused token, a spent or repeated token, a token for another
      hostname and one for another action are all refused
  A4  known and unknown identifiers get the same status, JSON keys, size band
      and a median time within 50 ms (30 of each)
  A5  after a burst of failed attempts, someone who solves the challenge is
      still let in from another IP (never 423 or 429); the bursting IP itself
      may get a 429 with Retry-After of at most 120 s; after 40 valid-token
      requests to one target from 40 IPs, the owner is not refused
A0  every switch that weakens the check ("login": false, "public": false,
    "anonymous_writes": false, "log_in_helper", ignore_paths, mode "baseline",
    password_exception, no server or body, auth-dynamic "false") needs an entry
    in "exceptions" (hatch, reason, approved_by, date), and web_health.py fails
    a route, source folder or helper that main had and this change removes, and
    a switch from "absolute" to "baseline". auth-config "off" only in the kit.
A7  mode "absolute" (new repository): everything fails. Mode "baseline" (old
repository): the findings listed in "baseline" are tolerated and the list may
only shrink (web_health.py compares it with main). It is meant for repositories
that existed before the policy; which ones is decided outside this kit.

The route handler is found by the path literal in the source (to the next
route, 80 lines at most) or by "handler": "file:function". D1 use through a
helper is invisible to it: list the helpers in "d1_calls".
"""
import argparse
import json
import os
import re
import socket
import statistics
import subprocess
import sys
import tempfile
import time
import http.client
import urllib.error
import urllib.request
from email.parser import BytesParser
from email.policy import HTTP
from urllib.parse import parse_qs

SRC_EXT = {".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".html", ".htm", ".sql", ".py", ".swift"}
SKIP_DIRS = {"node_modules", ".git", "dist", "build", ".deploy", ".wrangler", "coverage", ".quality", "tests", "test",
             "__tests__", "docs"}
# Tests and maintenance scripts are not the app that serves the login.
TEST_FILE = re.compile(r"(\.test\.|\.spec\.|_test\.|/tests?/|/fixtures?/|/(test|teste)_[^/]*$)")
MAINTENANCE = re.compile(r"^/(?:[^/]+/)?(?:scripts|ops)/")
# Code that answers requests: it is part of the app wherever it lives.
SERVES = re.compile(r"""export\s+default\s*\{|\bcreateServer\(|\b(?:Deno|Bun)\.serve\(|"""
                    r"""\baddEventListener\(\s*['"]fetch|\b(?:app|router)\.(?:get|post|put|delete|all)\(\s*['"`]/""")
MAX_BYTES = 1024 * 1024
KINDS = {"login", "signup", "verify", "pair"}
METHODS = {"passkey", "whatsapp_code", "email_code", "password"}

TURNSTILE_TEST = {"pass": "1x0000000000000000000000000000000AA",
                  "fail": "2x0000000000000000000000000000000AA",
                  "spent": "3x0000000000000000000000000000000AA"}

# Login found in the source without a declaration.
DETECT = [
    ("passkey", re.compile(r"navigator\.credentials\.(get|create)|@simplewebauthn|generateAuthenticationOptions")),
    ("login code", re.compile(r"\b(codigos_login|login_codes)\b")),
    ("login route", re.compile(r"""['"`]/(?:api/)?(?:login|entrar|signin|cadastro|signup|register)(?:/[\w/:-]*)?['"`]""")),
    ("password field", re.compile(r"""<input[^>]+type\s*=\s*['"]?password|\bname\s*=\s*['"](?:password|senha)['"]|\bSecureField\b""", re.I)),
]
# Paths that look like login routes in the source; each must be declared.
ROUTE_LIT = re.compile(r"""['"`](/(?:api/)?(?:login|entrar|signin|cadastro|signup|register|pair)(?:/[\w/:-]*)?)['"`]""")

PASSWORD_USE = re.compile(r"\b(bcrypt|argon2|scrypt|pbkdf2|hashPassword|verifyPassword|checkPassword)\w*|"
                          r"""<input[^>]+type\s*=\s*['"]?password|\bSecureField\b|\b(?:password|senha)_?hash\b""", re.I)
WEAK_HASH = re.compile(r"(?:\b(?:md5|sha-?1|sha-?256)\b|createHash\(\s*['\"]\s*(?:md5|sha1|sha256)|digest\(\s*['\"]SHA-(?:1|256)['\"])"
                       r".{0,100}\b(?:password|senha|passwd)\b|"
                       r"\b(?:password|senha|passwd)\b.{0,100}(?:\b(?:md5|sha-?1|sha-?256)\b|createHash\(\s*['\"]\s*(?:md5|sha1|sha256)|digest\(\s*['\"]SHA-(?:1|256)['\"])", re.I)
PASSWORD_COLUMN = re.compile(r"\b(password|senha|passwd)\b\s+(text|varchar|blob|char)", re.I)
D1_USE = re.compile(r"\.prepare\(|\.batch\(|\benv\.DB\b|\bdb\.(?:prepare|exec|batch)\(|\bgetSession\(|\bDurableObject|\.idFromName\(")
RL_CALL = re.compile(r"\b(\w+)\.limit\(\s*\{")
TURNSTILE_CALL = re.compile(r"siteverify|verifyTurnstile|verificarTurnstile|validateTurnstile", re.I)
# COUNT over a table; fine only when its WHERE filters by a column of the client.
COUNT_FROM = re.compile(r"COUNT\(\s*(?:\*|1|\w+)\s*\)(?:\s+AS\s+\w+)?\s+FROM\s+(\w+)", re.I)
CLIENT_COL = re.compile(r"\b(ip|ip_hash|bucket|chave|key|pessoa|pessoa_id|person|person_id|telefone|phone|"
                        r"credencial|credencial_id|credential|credential_id|alvo|target)\b", re.I)
LOCKOUT = re.compile(r"\bSET\b[^;]{0,120}\b(locked|bloqueado|blocked|trancad[oa]|lockout\w*|locked_until)\b", re.I)
PRIVATE_NAME = re.compile(r"\b(telefone|phone|celular|whatsapp|wa_id|email|e_mail|ip|ip_address|ua|user_?agent|"
                          r"codigo|code|hash|\w*_hash|\w*hash|cf-connecting-ip|x-forwarded-for)\b", re.I)
CONSOLE = re.compile(r"console\.(log|info|warn|error|debug)\(")


def files(root, only=None):
    out = []
    base = os.path.join(root, only) if only else root
    if os.path.isfile(base):
        return [base]
    for dp, dns, fns in os.walk(base):
        dns[:] = [d for d in dns if d not in SKIP_DIRS]
        for fn in fns:
            p = os.path.join(dp, fn)
            rel = os.path.relpath(p, root)
            if os.path.splitext(fn)[1] in SRC_EXT and not TEST_FILE.search("/" + rel) \
                    and os.path.isfile(p) and os.path.getsize(p) <= MAX_BYTES \
                    and (not MAINTENANCE.search("/" + rel) or SERVES.search(read(p))):
                out.append(p)
    return sorted(out)


def read(p):
    try:
        return open(p, encoding="utf-8", errors="replace").read()
    except OSError:
        return ""


def strip_comments(text, ext):
    """The text with comments blanked (same length and lines): commented-out
    code never satisfies a rule. When in doubt the text is kept: a block
    comment counts only if it closes and does not start glued to a word, a
    "//" glued to ":" is part of a URL, and in HTML only <!-- --> is removed."""
    out = list(text)

    def blank(a, b):
        for k in range(a, b):
            if out[k] != "\n":
                out[k] = " "

    html = ext in (".html", ".htm")
    hashed = ext in (".py", ".toml")
    sql = ext == ".sql"
    slashes = not (html or hashed or sql)
    i, n = 0, len(text)

    def string_end(j, depth=0):
        """Where the string that opens at j ends, with the ${} of a template."""
        q = text[j]
        j += 1
        while j < n and text[j] != q and (q == "`" or text[j] != "\n"):
            if q == "`" and depth < 20 and text.startswith("${", j):
                j, level = j + 2, 1
                while j < n and level:
                    if text[j] in "\"'`":
                        j = string_end(j, depth + 1)
                        continue
                    level += (text[j] == "{") - (text[j] == "}")
                    j += 1
                continue
            j += 2 if text[j] == "\\" else 1
        return j + 1

    while i < n:
        c = text[i]
        if html:
            if text.startswith("<!--", i):
                end = text.find("-->", i + 4)
                end = n if end < 0 else end + 3
                blank(i, end)
                i = end
            else:
                i += 1
        elif c in "\"'" or (c == "`" and slashes):
            i = string_end(i)
        elif (hashed and c == "#") or (sql and text.startswith("--", i)) or (
                slashes and text.startswith("//", i) and text[i - 1:i] != ":"):
            end = text.find("\n", i)
            end = n if end < 0 else end
            blank(i, end)
            i = end
        elif (slashes or sql) and text.startswith("/*", i) and (
                i == 0 or text[i - 1].isspace() or text[i - 1] in "({,;=})") and text.find("*/", i + 2) >= 0:
            end = text.find("*/", i + 2) + 2
            blank(i, end)
            i = end
        elif slashes and c == "\\":
            i += 2
        elif slashes and c == "/" and re.search(r"(?:^|[(,=:\[!&|?{};+*%~^>-]|\breturn)\s*$", text[max(0, i - 40):i]):
            # A regular expression literal: quotes and slashes in it are its own.
            j, inside = i + 1, False
            while j < n and text[j] != "\n" and (inside or text[j] != "/"):
                if text[j] == "\\":
                    j += 1
                elif text[j] in "[]":
                    inside = text[j] == "["
                j += 1
            i = j + 1
        else:
            i += 1
    return "".join(out)


def code(p):
    """A source or wrangler file as the static rules read it."""
    ext = os.path.splitext(p)[1]
    if ext in (".json", ".jsonc"):
        ext = ".js"
    return strip_comments(read(p), ext)


def source_files(cfg, root):
    folders = cfg.get("source") or ["."]
    seen, out = set(), []
    for f in folders:
        for p in files(root, f if f != "." else None):
            if p not in seen:
                seen.add(p)
                out.append(p)
    return out


def num(m, default=None):
    return int(m.group(1).replace("_", "")) if m else default


# --- region of a route handler ---------------------------------------------------

ROUTE_START = re.compile(r"""(?:pathname|path|url\.pathname)\s*===?\s*['"`]/|\bcase\s+['"`]/|"""
                         r"""\.(?:get|post|put|delete|patch|all|on)\(\s*['"`]/|\broute\(\s*['"`]/""")


def closing(text, i):
    """Index of the bracket that closes the "(" or "{" at i."""
    opening = text[i]
    close = ")" if opening == "(" else "}"
    depth = 0
    for j in range(i, len(text)):
        depth += (text[j] == opening) - (text[j] == close)
        if depth == 0:
            return j
    return len(text) - 1


# What may sit between a brace of a return type and the body: `Promise<{..}> {`.
NEXT_BRACE = re.compile(r"\s*(?:[>\]|&\[]\s*)*(?:=>\s*)?\{")


def brace_body(text, start):
    i = text.find("{", start)
    p = text.find("(", start)
    if 0 <= p < i:
        end = closing(text, p)
        # Braces in a parameter list are not the body. A "(" that holds a
        # function is a wrapper around the handler, not its parameters.
        if not re.search(r"=>|\bfunction\b", text[p:end]):
            i = text.find("{", end)
    if i < 0:
        return None
    end = closing(text, i)
    more = NEXT_BRACE.match(text, end + 1)
    while more:
        end = closing(text, more.end() - 1)
        more = NEXT_BRACE.match(text, end + 1)
    return text[start:end + 1]


def regions(route, root, srcs):
    """[(file, first_line, text)] of the handler of `route` ({"route": "POST /x", "handler"?})."""
    h = route.get("handler")
    if h:
        fn, _, name = h.partition(":")
        p = os.path.join(root, fn)
        text = code(p)
        m = re.search(r"(?:function\s+%s\b|(?:const|let|var)\s+%s\s*=|\b%s\s*\([^)]*\)\s*(?::[^{;=]*)?\{)" % ((re.escape(name),) * 3), text)
        if not m:
            return []
        body = brace_body(text, m.start())
        return [(fn, text.count("\n", 0, m.start()) + 1, body or "")] if body else []
    path = route["route"].split(" ", 1)[-1]
    lit = re.compile(r"""['"`]%s['"`]""" % re.escape(path))
    out = []
    for p in srcs:
        lines = code(p).splitlines()
        for i, line in enumerate(lines):
            if lit.search(line) and (ROUTE_START.search(line) or re.search(r"\b(?:app|router|r)\.\w+\(", line)):
                end = len(lines)
                for j in range(i + 1, min(len(lines), i + 80)):
                    if ROUTE_START.search(lines[j]) and not lit.search(lines[j]):
                        end = j
                        break
                    end = min(len(lines), i + 80)
                out.append((os.path.relpath(p, root), i + 1, "\n".join(lines[i:end])))
    return out


# --- static rules ---------------------------------------------------------------

class Findings:
    def __init__(self):
        self.items = []

    def add(self, rule, subject, where, msg):
        """The id of a second occurrence ends in "#2", and so on: a baseline
        that tolerates one never tolerates a new one."""
        used = {f["id"] for f in self.items}
        fid, n = f"{rule}:{subject}", 1
        while fid in used:
            n += 1
            fid = f"{rule}:{subject}#{n}"
        self.items.append({"rule": rule, "id": fid, "where": where, "msg": msg})


def wrangler_ratelimits(root):
    names = set()
    for fn in ("wrangler.toml", "wrangler.json", "wrangler.jsonc"):
        for dp, dns, fns in os.walk(root):
            dns[:] = [d for d in dns if d not in SKIP_DIRS]
            if fn in fns:
                text = code(os.path.join(dp, fn))
                if fn.endswith(".toml"):
                    for blk in re.split(r"(?m)^\[\[", text):
                        if re.match(r"(?:env\.[\w-]+\.)?(?:ratelimits|unsafe\.bindings)\]\]", blk):
                            m = re.search(r'name\s*=\s*"(\w+)"', blk)
                            if m:
                                names.add(m.group(1))
                else:
                    for m in re.finditer(r'"ratelimits"\s*:\s*\[(.*?)\]', text, re.S):
                        names.update(re.findall(r'"name"\s*:\s*"(\w+)"', m.group(1)))
    return names


ALGORITHMS = ("argon2id", "scrypt", "bcrypt", "pbkdf2-sha256")


def check_password(cfg, root, srcs, fnd):
    exc = cfg.get("password_exception")
    uses = []
    for p in srcs:
        rel = os.path.relpath(p, root)
        text = code(p)
        for i, line in enumerate(text.splitlines(), 1):
            if WEAK_HASH.search(line):
                fnd.add("A1", f"weakhash:{rel}", f"{rel}:{i}", "MD5, SHA-1 or SHA-256 on its own applied to a password (use Argon2id or scrypt)")
            if PASSWORD_USE.search(line):
                uses.append((rel, i))
        if p.endswith(".sql"):
            for i, line in enumerate(text.splitlines(), 1):
                if PASSWORD_COLUMN.search(line) and not re.search(r"hash", line, re.I):
                    fnd.add("A1", f"plain:{rel}", f"{rel}:{i}", "a password column that is not a hash (store only the hash)")
    method_password = "password" in (cfg.get("methods") or [])
    if (uses or method_password) and not exc:
        first = {}
        for rel, i in uses:
            first.setdefault(rel, f"{rel}:{i}")
        # One id per file: a baseline that tolerates one never covers another.
        found = [(f"password:{rel}", where) for rel, where in first.items()]
        for subject, where in found or [("password", ".quality/auth.json")]:
            fnd.add("A1", subject, where,
                    "password login is forbidden by default (passkey or code); a registered exception needs reason, approved_by, date and the algorithm")
        return
    if not exc:
        return
    miss = [k for k in ("reason", "approved_by", "date", "algorithm") if not exc.get(k)]
    if miss:
        fnd.add("A1", "exception", ".quality/auth.json", "password_exception without " + ", ".join(miss))
        return
    wanted = str(exc["algorithm"]).lower()
    # One of the fixed names, never the text of the file: it goes to the output.
    algo = next((a for a in ALGORITHMS if a == wanted), "unknown")
    p = exc.get("params") or {}
    bad = []
    if algo == "argon2id":
        if p.get("variant", "argon2id") != "argon2id":
            bad.append("only Argon2id")
        if p.get("memory_kib", 0) < 19 * 1024:
            bad.append("memory_kib >= 19456")
        if p.get("iterations", 0) < 2:
            bad.append("iterations >= 2")
        if p.get("parallelism") != 1:
            bad.append("parallelism = 1")
    elif algo == "scrypt":
        if p.get("N", 0) < 2 ** 17:
            bad.append("N >= 131072")
        if p.get("r") != 8:
            bad.append("r = 8")
        if p.get("p") != 1:
            bad.append("p = 1")
    elif algo == "bcrypt":
        if p.get("cost", 0) < 12:
            bad.append("cost >= 12")
        if not p.get("legacy"):
            bad.append("bcrypt only as legacy (legacy: true)")
        if not p.get("rejects_over_72_bytes"):
            bad.append("rejects passwords over 72 bytes")
    elif algo == "pbkdf2-sha256":
        if p.get("iterations", 0) < 600000:
            bad.append("iterations >= 600000")
        if not p.get("fips"):
            bad.append("only when FIPS requires it (fips: true)")
    else:
        bad.append("algorithm must be argon2id, scrypt, bcrypt (legacy) or pbkdf2-sha256 (FIPS)")
    for b in bad:
        fnd.add("A1", f"params:{algo}", ".quality/auth.json", f"password_exception.{algo}: needs {b}")
    # The declared numbers must be the ones in the code.
    for p_ in srcs:
        rel = os.path.relpath(p_, root)
        text = code(p_)
        for m in re.finditer(r"(?:bcrypt\.(?:hash|hashSync)\([^)]*?,\s*(\d+)\s*\)|genSalt(?:Sync)?\(\s*(\d+)|\b(?:salt_?rounds|rounds|cost)\s*[:=]\s*(\d+))", text, re.I):
            val = int(next(g for g in m.groups() if g))
            if val < 12 and re.search(r"bcrypt", text, re.I):
                fnd.add("A1", f"bcrypt:{rel}", f"{rel}:{text.count(chr(10), 0, m.start()) + 1}", f"bcrypt cost {val} is below the minimum of 12")
        for m in re.finditer(r"\biterations\s*[:=]\s*([\d_]+)", text):
            val = int(m.group(1).replace("_", ""))
            if re.search(r"pbkdf2", text, re.I) and val < 600000:
                fnd.add("A1", f"pbkdf2:{rel}", f"{rel}:{text.count(chr(10), 0, m.start()) + 1}", f"PBKDF2 iterations {val} is below the minimum of 600000")
        for m in re.finditer(r"\bmemoryCost\s*[:=]\s*([\d_]+)", text):
            if int(m.group(1).replace("_", "")) < 19 * 1024:
                fnd.add("A1", f"argon2:{rel}", f"{rel}:{text.count(chr(10), 0, m.start()) + 1}", "Argon2id memoryCost is below the minimum of 19456 KiB")
        for m in re.finditer(r"\btimeCost\s*[:=]\s*(\d+)", text):
            if int(m.group(1)) < 2:
                fnd.add("A1", f"argon2t:{rel}", f"{rel}:{text.count(chr(10), 0, m.start()) + 1}", "Argon2id timeCost is below the minimum of 2")


STRING = re.compile(r"""`(?:\\.|[^`\\])*`|"(?:\\.|[^"\\\n])*"|'(?:\\.|[^'\\\n])*'""")


def call_args(text, start):
    """The arguments of a call whose "(" ends at `start` (400 characters at most)."""
    depth, quote, j = 1, None, start
    while j < min(len(text), start + 400):
        c = text[j]
        if quote:
            if c == "\\":
                j += 1
            elif c == quote:
                quote = None
        elif c in "\"'`":
            quote = c
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if not depth:
                break
        j += 1
    return text[start:j]


def code_only(args):
    """Without the words of fixed text, what is left names the data logged.
    A string that is a key (`get("..")`, `[".."]`, `{"..": x}`) and `${..}` in a
    template stay."""
    def keep(m):
        s = m.group(0)
        if s[0] == "`":
            return " ".join(re.findall(r"\$\{([^}]*)\}", s))
        before, after = m.string[:m.start()], m.string[m.end():]
        key = re.search(r"(?:\bget\(|\[)\s*$", before) \
            or (re.search(r"[{,]\s*$", before) and re.match(r"\s*:", after))
        return s if key else '""'
    return STRING.sub(keep, args)


def check_route(r, cfg, root, srcs, bindings, fnd):
    name = r["route"]
    where_all = regions(r, root, srcs)
    if not where_all:
        fnd.add("A0", f"handler:{name}", ".quality/auth.json",
                f"handler of {name} not found in the source (add \"handler\": \"file:function\")")
        return
    d1 = re.compile(D1_USE.pattern + ("|" + "|".join(re.escape(x) + r"\(" for x in cfg["d1_calls"]) if cfg.get("d1_calls") else ""))
    for file, line, text in where_all:
        loc = f"{file}:{line}"
        lim = None
        for m in RL_CALL.finditer(text):
            if not bindings or m.group(1).split(".")[-1] in bindings or "env" in text[max(0, m.start() - 6):m.start()]:
                lim = m
                break
        d1m = d1.search(text)
        if r.get("public", True):
            if not lim:
                fnd.add("A2", name, loc, "no `ratelimits` binding call (RATELIMIT.limit({key})) in the route: the cheap filter comes before the costly step")
            elif d1m and d1m.start() < lim.start():
                fnd.add("A2", f"{name}:order", loc, "D1 is used before the `ratelimits` binding in the route")
        if r.get("anonymous_writes", r.get("kind") in ("login", "signup", "verify", "pair")):
            t = TURNSTILE_CALL.search(text)
            if not t:
                fnd.add("A3", f"{name}:missing", loc, "no Turnstile verification on the server in a public route that writes or sends")
            elif d1m and d1m.start() < t.start():
                fnd.add("A3", f"{name}:order", loc, "D1 is used before the Turnstile verification")
        if not re.search(r"login_attempt", text) and not r.get("log_in_helper"):
            fnd.add("A6", name, loc, "the route does not log a `login_attempt` event")
        for m in CONSOLE.finditer(text):
            if PRIVATE_NAME.search(code_only(call_args(text, m.end()))):
                fnd.add("A6", f"console:{name}", f"{file}:{line + text.count(chr(10), 0, m.start())}",
                        "console.* with phone, e-mail, IP, user agent, code or hash in a login route")
        for m in re.finditer(r"login_attempt", text):
            brace = text.rfind("{", max(0, m.start() - 200), m.start())
            start = brace if brace >= 0 and "}" not in text[brace:m.start()] else m.start()
            near = text[start:m.start() + 400].split("\n\n")[0]
            near = near.split("})")[0]
            keys = re.findall(r"""["']?([A-Za-z_-]+)["']?\s*:""", near)
            keys += re.findall(r"[{,]\s*([A-Za-z_]\w*)\s*(?=[,}]|$)", near)  # `{ phone, ip }`
            bad = sorted({k.lower() for k in keys if PRIVATE_NAME.fullmatch(k)})
            if bad:
                fnd.add("A6", f"event:{name}:{','.join(bad)}", f"{file}:{line + text.count(chr(10), 0, m.start())}",
                        "login_attempt event with " + ", ".join(bad) + " (the event carries no personal data)")


# The value being read (not its name in a type declaration).
VERIFY_READ = re.compile(r"""(?:\.|\[\s*['"])TURNSTILE_VERIFY_URL\b|\{[^{}\n]*\bTURNSTILE_VERIFY_URL\b[^{}\n]*\}\s*=""")
# A local host inside a comparison (a default such as 'http://localhost:8932'
# is not a check) ...
_LOCAL = r"""['"`](?:http://)?(?:127\.0\.0\.1|localhost)(?::\d*)?/?['"`]"""
LOCAL_COMPARE = re.compile("|".join((
    r"[=!]==?\s*" + _LOCAL, _LOCAL + r"\s*[=!]==?", r"\bcase\s+" + _LOCAL,
    r"\.(?:startsWith|includes)\(\s*" + _LOCAL,
    _LOCAL + r"\s*,?\s*\]\s*\)?\s*\.(?:includes|has)\(",
    r"""\\/\\/\(?(?:\?:)?(?:127|localhost)[^\n]{0,80}/[a-z]*\s*\.test\(""",
    r"""\.match\(\s*/[^\n]{0,40}\\/\\/\(?(?:\?:)?(?:127|localhost)""")))
# ... of a host: one of these words within 120 characters.
HOST_WORD = re.compile(r"host|origin", re.I)


def local_guard(text):
    return any(HOST_WORD.search(text[max(0, m.start() - 120):m.end() + 120]) for m in LOCAL_COMPARE.finditer(text))


def check_verify_url(root, srcs, fnd):
    """TURNSTILE_VERIFY_URL is a test override: never a wrangler var, and the
    code that reads it accepts only a local http address."""
    for dp, dns, fns in os.walk(root):
        dns[:] = [d for d in dns if d not in SKIP_DIRS]
        for fn in fns:
            if fn in ("wrangler.toml", "wrangler.json", "wrangler.jsonc"):
                path = os.path.join(dp, fn)
                text = code(path)
                if "TURNSTILE_VERIFY_URL" in text:
                    rel = os.path.relpath(path, root)
                    fnd.add("A3", f"verify-url-wrangler:{rel}", rel,
                            "TURNSTILE_VERIFY_URL is set in the wrangler config: it would point production's Turnstile check elsewhere (it is for tests only)")
    texts = [(p, code(p)) for p in srcs if not p.endswith(".sql")]
    # The check may live next to the siteverify call, in another file.
    guarded = any(local_guard(t) for _, t in texts if "siteverify" in t or "TURNSTILE_VERIFY_URL" in t)
    for p, text in texts:
        m = VERIFY_READ.search(text)
        if m and not guarded:
            rel = os.path.relpath(p, root)
            fnd.add("A3", f"verify-url-guard:{rel}", f"{rel}:{text.count(chr(10), 0, m.start()) + 1}",
                    "TURNSTILE_VERIFY_URL is accepted without a check that it is http://127.0.0.1 or http://localhost (the override must never work in production)")


def check_turnstile_impl(cfg, root, srcs, fnd):
    """The file that calls siteverify must read success, hostname and action."""
    for p in srcs:
        text = code(p)
        if "turnstile/v0/siteverify" not in text:  # the file that holds Cloudflare's address
            continue
        rel = os.path.relpath(p, root)
        for word in ("success", "hostname", "action"):
            if not re.search(r"\b%s\b" % word, text):
                fnd.add("A3", f"verify:{word}", rel, f"the Turnstile verification never checks `{word}`")


def sql_tail(text, m, is_sql):
    """The rest of the SQL statement after a match: up to the quote that opened
    its string (";" in a .sql file), 600 characters at most."""
    if is_sql:
        quote = ";"
    else:
        found = max(((text.rfind(q, 0, m.start()), q) for q in "\"'`"), key=lambda x: x[0])
        quote = found[1] if found[0] >= 0 else "\n"
    end = text.find(quote, m.end())
    return text[m.end():end if 0 <= end <= m.end() + 600 else m.end() + 600]


LOGIN_FILE = re.compile(r"(login|auth|passkey|webauthn|pair|cadastro|signup|signin)[^/]*$", re.I)
LOGIN_TABLE = re.compile(r"login|codigo|code|desafio|challenge|pair|cadastro|signup|tentativa|attempt|passkey|"
                         r"sess|convite|invite|link_request|otp", re.I)


def check_global(cfg, root, srcs, fnd):
    for p in srcs:
        rel = os.path.relpath(p, root)
        text = code(p)
        if not re.search(r"login|codigo|challenge|desafio|pair|cadastro", text, re.I):
            continue
        for m in COUNT_FROM.finditer(text):
            if not (LOGIN_FILE.search(rel) or LOGIN_TABLE.search(m.group(1))):
                continue
            tail = sql_tail(text, m, p.endswith(".sql"))
            w = re.search(r"\bWHERE\b(.*)", tail, re.I | re.S)
            if not w or not CLIENT_COL.search(w.group(1)):
                i = text.count("\n", 0, m.start()) + 1
                fnd.add("A5", f"global:{rel}:{m.group(1)}", f"{rel}:{i}",
                        f"a ceiling by COUNT over `{m.group(1)}` that does not filter by the client (ip, key, person, phone, credential): one bot locks everyone out (limit per IP in the binding)")
        for i, line in enumerate(text.splitlines(), 1):
            if LOCKOUT.search(line):
                fnd.add("A5", f"lockout:{rel}", f"{rel}:{i}", "the account is locked by a counter: delay or challenge instead, never lock the owner out")


def detect_login(root, srcs, ignore=()):
    hits = []
    for p in srcs:
        text = code(p)
        rel = os.path.relpath(p, root)
        for what, rx in DETECT:
            for m in rx.finditer(text):
                if what == "login route" and m.group(0)[1:-1] in ignore:
                    continue
                hits.append((what, f"{rel}:{text.count(chr(10), 0, m.start()) + 1}"))
                break
    return hits


def declared_paths(cfg):
    return {r["route"].split(" ", 1)[-1] for r in cfg.get("routes", [])}


def shape_errors(cfg):
    """A value of the wrong type is an error in the panel, never a crash."""
    if not isinstance(cfg, dict):
        return ["the file must hold a JSON object"]
    errs = []

    def need(ok, name, kind):
        if not ok:
            errs.append(f"{name} must be {kind}")

    def texts(v):
        return isinstance(v, list) and all(isinstance(x, str) for x in v)

    for key in ("methods", "source", "ignore_paths", "d1_calls", "baseline"):
        need(key not in cfg or texts(cfg[key]), key, "a list of strings")
    need(isinstance(cfg.get("exceptions", []), list), "exceptions", "a list")
    for key in ("server", "turnstile", "password_exception"):
        need(key not in cfg or isinstance(cfg[key], dict), key, "an object")
    routes = cfg.get("routes", [])
    need(isinstance(routes, list) and all(isinstance(r, dict) for r in routes), "routes", "a list of objects")
    if not errs:
        for r in routes:
            need(isinstance(r.get("route"), str), "routes[].route", "a string such as \"POST /login\"")
            for key in ("handler", "kind", "known", "unknown", "turnstile_action"):
                need(key not in r or isinstance(r[key], str), f"routes[].{key}", "a string")
            need("headers" not in r or isinstance(r["headers"], dict), "routes[].headers", "an object")
        server = cfg.get("server")
        if server is not None:
            for key in ("start", "url"):
                need(isinstance(server.get(key), str), f"server.{key}", "a string")
            need("ready" not in server or isinstance(server["ready"], str), "server.ready", "a string")
            need("timeout" not in server or isinstance(server["timeout"], (int, float)), "server.timeout", "a number")
        need("seed" not in cfg or isinstance(cfg["seed"], str), "seed", "a string")
        params = cfg.get("password_exception", {}).get("params", {})
        need(isinstance(params, dict), "password_exception.params", "an object")
        if isinstance(params, dict):
            for key in ("memory_kib", "iterations", "parallelism", "N", "r", "p", "cost"):
                need(key not in params or isinstance(params[key], int), f"password_exception.params.{key}", "a number")
    return errs


def check_config(cfg):
    errs = []
    if cfg.get("login") is False:
        return errs
    if cfg.get("mode") not in ("absolute", "baseline"):
        errs.append("mode must be \"absolute\" or \"baseline\"")
    ms = cfg.get("methods")
    if not ms or not set(ms) <= METHODS:
        errs.append("methods must list passkey, whatsapp_code, email_code and/or password")
    if not cfg.get("identifier"):
        errs.append("identifier is missing (telefone, email, credential, ...)")
    for r in cfg.get("routes") or []:
        if not re.match(r"^(GET|POST|PUT|DELETE|PATCH) /", r.get("route", "")):
            errs.append(f"route '{r.get('route')}' must look like \"POST /login/iniciar\"")
        if r.get("body") and r.get("known", "known") == r.get("unknown", "unknown"):
            errs.append(f"route {r.get('route')}: known and unknown must be different identifiers")
        if r.get("kind") not in KINDS:
            errs.append(f"route {r.get('route')}: kind must be one of {sorted(KINDS)}")
    if not cfg.get("routes"):
        errs.append("routes is empty: declare every login or sign-up route (or {\"login\": false})")
    return errs


def static(cfg, root, declared):
    fnd = Findings()
    errors = []
    srcs = source_files(cfg or {}, root)
    if not declared:
        hits = detect_login(root, srcs)
        if hits:
            what, where = hits[0]
            fnd.add("A0", "undeclared", where, f"a login was found ({what}) but the repository has no .quality/auth.json")
        return fnd.items, errors, srcs
    if cfg.get("login") is False:
        hits = detect_login(root, srcs, set(cfg.get("ignore_paths") or []))
        if hits:
            fnd.add("A0", "undeclared", hits[0][1], f"\"login\": false but a login was found ({hits[0][0]}); a page or a call "
                    "to another service goes in ignore_paths, with its exception")
        return fnd.items, errors, srcs
    errors = check_config(cfg)
    errors += [f"source folder '{f}' does not exist" for f in cfg.get("source") or []
               if not os.path.exists(os.path.join(root, f))]
    if errors:
        return fnd.items, errors, srcs
    # A route literal that looks like a login route and is not declared.
    declared_p = declared_paths(cfg) | set(cfg.get("ignore_paths") or [])
    seen = set()
    for p in srcs:
        if p.endswith(".sql"):
            continue
        rel = os.path.relpath(p, root)
        text = code(p)
        for m in ROUTE_LIT.finditer(text):
            path = m.group(1)
            if path not in declared_p and path not in seen and not any(path.startswith(d.rstrip("/") + "/") and d != "/" for d in declared_p if d.endswith("/")):
                seen.add(path)
                fnd.add("A0", f"route:{path}", f"{rel}:{text.count(chr(10), 0, m.start()) + 1}",
                        f"route {path} looks like a login or sign-up route and is not declared (routes, or ignore_paths for a page)")
    bindings = wrangler_ratelimits(root)
    check_password(cfg, root, srcs, fnd)
    for r in cfg["routes"]:
        check_route(r, cfg, root, srcs, bindings, fnd)
    if any(r.get("public", True) for r in cfg["routes"]) and not bindings:
        fnd.add("A2", "binding", "wrangler", "no `ratelimits` binding declared in wrangler (toml/json/jsonc)")
    check_turnstile_impl(cfg, root, srcs, fnd)
    check_verify_url(root, srcs, fnd)
    check_global(cfg, root, srcs, fnd)
    return fnd.items, errors, srcs


# --- dynamic rules --------------------------------------------------------------

def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def siteverify_fields(raw, content_type):
    """The fields of a siteverify request: form, multipart or JSON, as Cloudflare accepts."""
    kind = content_type.split(";")[0].strip().lower()
    try:
        if kind == "application/json":
            j = json.loads(raw)
            return {k: str(v) for k, v in j.items()} if isinstance(j, dict) else {}
        if kind == "multipart/form-data":
            msg = BytesParser(policy=HTTP).parsebytes(b"content-type: " + content_type.encode() + b"\r\n\r\n" + raw)
            return {part.get_param("name", header="content-disposition"):
                    part.get_payload(decode=True).decode(errors="replace") for part in msg.iter_parts()}
        return {k: v[0] for k, v in parse_qs(raw.decode(errors="replace")).items()}
    except ValueError:
        return {}


class FakeSiteverify:
    """Cloudflare's siteverify as a local server, with the public test secrets:
    pass accepts a token once (a second use = timeout-or-duplicate), fail
    refuses, spent says "already used". The token carries its hostname and
    action ("kit.<hostname>.<action>.<n>") because Cloudflare's own test keys
    always answer hostname example.com and no action, which would make the
    server's hostname and action checks impossible to test."""

    def __init__(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        import threading
        seen = self.seen = set()

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                form = siteverify_fields(self.rfile.read(int(self.headers.get("content-length") or 0)),
                                         self.headers.get("content-type") or "")
                secret = form.get("secret", "")
                token = form.get("response", "")
                parts = token.split(".")
                if secret == TURNSTILE_TEST["fail"]:
                    out = {"success": False, "error-codes": ["invalid-input-response"]}
                elif secret == TURNSTILE_TEST["spent"] or token in seen:
                    out = {"success": False, "error-codes": ["timeout-or-duplicate"]}
                elif secret != TURNSTILE_TEST["pass"] or len(parts) < 4 or parts[0] != "kit":
                    out = {"success": False, "error-codes": ["invalid-input-response"]}
                else:
                    seen.add(token)
                    out = {"success": True, "error-codes": [], "hostname": parts[1], "action": parts[2]}
                raw = json.dumps(out).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *a):
                pass

        self.http = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.http.daemon_threads = True
        self.http.block_on_close = False
        self.url = f"http://127.0.0.1:{self.http.server_address[1]}/siteverify"
        threading.Thread(target=self.http.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    def stop(self):
        self.http.shutdown()
        self.http.server_close()


class Server:
    def __init__(self, cfg, root, secret, verify_url):
        srv = cfg["server"]
        self.port = free_port()
        self.url = srv["url"].replace("{port}", str(self.port))
        self.log = tempfile.TemporaryFile()
        # Only these placeholders: other braces belong to the shell.
        cmd = srv["start"].replace("{port}", str(self.port)).replace("{turnstile_secret}", secret) \
            .replace("{verify_url}", verify_url)
        env = dict(os.environ, TURNSTILE_SECRET=secret, TURNSTILE_VERIFY_URL=verify_url)
        self.proc = subprocess.Popen(cmd, shell=True, cwd=root, stdout=self.log, stderr=subprocess.STDOUT,
                                     start_new_session=True, env=env)
        deadline = time.time() + srv.get("timeout", 120)
        ready = self.url + srv.get("ready", "/")
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError("the server stopped: " + self.tail())
            try:
                urllib.request.urlopen(ready, timeout=2)
                return
            except urllib.error.HTTPError:
                return
            except OSError:
                time.sleep(0.3)
        self.stop()
        raise RuntimeError("the server did not answer: " + self.tail())

    def tail(self):
        self.log.seek(0)
        return self.log.read().decode(errors="replace")[-400:]

    def stop(self):
        try:
            os.killpg(self.proc.pid, 15)
            self.proc.wait(timeout=10)
        except (ProcessLookupError, subprocess.TimeoutExpired, PermissionError):
            try:
                os.killpg(self.proc.pid, 9)
            except (ProcessLookupError, PermissionError):
                pass


def fill_body(tpl, ident, token):
    return json.loads(json.dumps(tpl).replace("{id}", ident).replace("{token}", token))


def send(url, route, ident, token, ip):
    method, path = route["route"].split(" ", 1)
    body = fill_body(route["body"], ident, token)
    headers = {"content-type": "application/json", "cf-connecting-ip": ip, "x-forwarded-for": ip,
               "user-agent": "kit-auth-check"}
    headers.update(route.get("headers") or {})
    req = urllib.request.Request(url + path, data=json.dumps(body).encode(), headers=headers, method=method)
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            status, raw, wait = r.status, r.read(), r.headers.get("retry-after")
    except urllib.error.HTTPError as e:
        status, raw, wait = e.code, e.read(), e.headers.get("retry-after")
    ms = (time.perf_counter() - t0) * 1000
    try:
        j = json.loads(raw)
        keys = sorted(j) if isinstance(j, dict) else type(j).__name__
    except ValueError:
        keys = "(not json)"
    return status, keys, len(raw), ms, raw, wait


def short_wait(retry_after):
    """Retry-After in seconds, at most RETRY_MAX."""
    return bool(retry_after) and retry_after.strip().isdigit() and int(retry_after) <= RETRY_MAX


def ip_of(n):
    return f"198.51.{100 + (n // 250) % 100}.{n % 250 + 1}"


SEED_TIMEOUT = 300


def dynamic(cfg, root):
    res, errors = [], []
    routes = [r for r in cfg["routes"] if r.get("body")]
    if not cfg.get("server") or not routes:
        return res, errors
    if cfg.get("seed"):
        try:
            out = subprocess.run(cfg["seed"], shell=True, cwd=root, capture_output=True, text=True, timeout=SEED_TIMEOUT)
        except subprocess.TimeoutExpired:
            return res, [f"seed command did not finish in {SEED_TIMEOUT} s"]
        if out.returncode != 0:
            return res, [f"seed command failed: {out.stderr[-200:] or out.stdout[-200:]}"]
    host = (cfg.get("turnstile") or {}).get("hostname", "localhost")
    n = [0]

    def nxt():
        n[0] += 1
        return n[0]

    def token(r, hostname=None, action=None):
        act = action or r.get("turnstile_action") or r["route"].split(" ", 1)[-1]
        return f"kit.{hostname or host}.{act}.{nxt()}"

    goods = {}

    def refusal(name, sub_id, why, s):
        """A bad token must get a 4xx that differs from the answer to a valid
        one: the same answer proves nothing about the check."""
        same = (s[0], s[4]) == (goods[name][0], goods[name][4]) if name in goods else False
        res.append({"rule": "A3", "route": name, "sub": sub_id, "ok": 400 <= s[0] < 500 and not same,
                    "detail": f"{why}: status {s[0]}, the same answer as with a valid token (nothing shows it was checked)" if same
                    else f"{why}: status {s[0]} (must be refused, 4xx)"})

    fake = FakeSiteverify()
    try:
        try:
            srv = Server(cfg, root, TURNSTILE_TEST["pass"], fake.url)
        except RuntimeError as e:
            return res, [str(e)]
        try:
            for r in routes:
                name = r["route"]
                known, unknown = r.get("known", "known"), r.get("unknown", "unknown")
                none = send(srv.url, r, known, "", ip_of(nxt()))
                good = send(srv.url, r, known, token(r), ip_of(nxt()))
                res.append({"rule": "A3", "route": name, "sub": "no-token", "ok": 400 <= none[0] < 500,
                            "detail": f"without a token: status {none[0]} (must be refused, 4xx)"})
                ignored = (good[0], good[4]) == (none[0], none[4])
                res.append({"rule": "A3", "route": name, "sub": "ignored", "ok": not ignored,
                            "detail": "a valid token changes nothing: the server ignores the token" if ignored
                            else "a valid token is read"})
                goods[name] = good
                for sub_id, why, tok in (("hostname", "a token for another hostname", token(r, hostname="evil.example")),
                                         ("action", "a token for another action", token(r, action="another-route"))):
                    refusal(name, sub_id, why, send(srv.url, r, known, tok, ip_of(nxt())))
                t = token(r)
                send(srv.url, r, known, t, ip_of(nxt()))
                refusal(name, "replay", "the same token used twice", send(srv.url, r, known, t, ip_of(nxt())))
                a, b = [], []
                for _ in range(30):
                    a.append(send(srv.url, r, known, token(r), ip_of(nxt())))
                    b.append(send(srv.url, r, unknown, token(r), ip_of(nxt())))
                st_a, st_b = {x[0] for x in a}, {x[0] for x in b}
                ka, kb = {json.dumps(x[1]) for x in a}, {json.dumps(x[1]) for x in b}
                la, lb = statistics.median(x[2] for x in a), statistics.median(x[2] for x in b)
                ta, tb = statistics.median(x[3] for x in a), statistics.median(x[3] for x in b)
                problems = {}
                if st_a != st_b or len(st_a) > 1:
                    problems["status"] = f"status {sorted(st_a)} vs {sorted(st_b)}"
                if ka != kb or len(ka) > 1:
                    problems["keys"] = "JSON keys differ"
                if abs(la - lb) > max(16, 0.2 * max(la, lb)):
                    problems["size"] = f"size {la:.0f} vs {lb:.0f} bytes"
                if abs(ta - tb) >= 50:
                    problems["time"] = f"median time {ta:.0f} vs {tb:.0f} ms"
                for sub_id, what in problems.items():
                    res.append({"rule": "A4", "route": name, "sub": sub_id, "ok": False,
                                "detail": "known and unknown identifiers differ: " + what})
                if not problems:
                    res.append({"rule": "A4", "route": name, "ok": True,
                                "detail": "known and unknown identifiers look the same"})
                for _ in range(40):
                    send(srv.url, r, known, "", "203.0.113.9")
                same_ip = send(srv.url, r, known, token(r), "203.0.113.9")
                other_ip = send(srv.url, r, known, token(r), ip_of(nxt()))
                # The same IP may be refused for a short while (high risk:
                # that locks the IP, not the account); another IP never is.
                locked = [other_ip[0]] if other_ip[0] in (423, 429) else []
                if same_ip[0] == 423 or (same_ip[0] == 429 and not short_wait(same_ip[5])):
                    locked.append(same_ip[0])
                res.append({"rule": "A5", "route": name, "sub": "ip", "ok": not locked,
                            "detail": "the owner still gets in after a burst of failures (from the same IP, at most a short 429)" if not locked
                            else f"after 40 failed attempts from one IP, a valid token got status {locked[0]}: the owner is locked out"})
                # A per-target limit: many clients (a botnet), each with a valid
                # token, hit the same target; the owner must not be locked out.
                for _ in range(40):
                    send(srv.url, r, known, token(r), ip_of(nxt()))
                owner = send(srv.url, r, known, token(r), ip_of(nxt()))
                res.append({"rule": "A5", "route": name, "sub": "target", "ok": owner[0] not in (423, 429),
                            "detail": "the owner still gets in after 40 requests to the same target from 40 IPs" if owner[0] not in (423, 429)
                            else f"after 40 requests to the same target from 40 IPs with valid tokens, the owner got status {owner[0]}: the target is locked (use a challenge or a delay, never a refusal)"})
        finally:
            srv.stop()
        for key, why in (("fail", "a token the siteverify refuses"), ("spent", "an already spent token")):
            try:
                srv = Server(cfg, root, TURNSTILE_TEST[key], fake.url)
            except RuntimeError as e:
                errors.append(str(e))
                continue
            try:
                for r in routes:
                    refusal(r["route"], {"fail": "refused", "spent": "spent"}[key], why,
                            send(srv.url, r, r.get("known", "known"), token(r), ip_of(nxt())))
            finally:
                srv.stop()
    except (OSError, http.client.HTTPException) as e:
        errors.append(f"the server stopped answering during the checks ({type(e).__name__}: {e})")
    finally:
        fake.stop()
    return res, errors


# --- main -----------------------------------------------------------------------

RETRY_MAX = 120
KIT_REPO = "luanharaujo/web-quality-kit"


def escapes(cfg):
    """Every switch that makes the check weaker. Each is an id; one that is new
    against main fails unless the `exceptions` block registers it."""
    out = []
    if cfg.get("login") is False:
        out.append("login:false")
    for r in cfg.get("routes") or []:
        if r.get("public") is False:
            out.append(f"public:false:{r.get('route')}")
        if r.get("anonymous_writes") is False:
            out.append(f"anonymous_writes:false:{r.get('route')}")
        if r.get("log_in_helper"):
            out.append(f"log_in_helper:{r.get('route')}")
    out += [f"ignore_paths:{x}" for x in cfg.get("ignore_paths") or []]
    if cfg.get("password_exception"):
        out.append("password_exception")
    if cfg.get("mode") == "baseline":
        out.append("mode:baseline")
    return out


def exception_entries(cfg, errors):
    ok = []
    for e in cfg.get("exceptions") or []:
        miss = [k for k in ("hatch", "reason", "approved_by", "date") if not (isinstance(e, dict) and e.get(k))]
        if miss:
            errors.append(f"exceptions: an entry is missing {', '.join(miss)}")
        else:
            ok.append({k: e[k] for k in ("hatch", "reason", "approved_by", "date")})
    return ok


def check(cfg, root, declared, run_dynamic=True, repo="", workflow_off=False):
    cfg = {} if cfg is None else cfg
    notes = []
    malformed = declared and not workflow_off and shape_errors(cfg)
    findings, errors, _ = static(cfg, root, declared) if not (workflow_off or malformed) else ([], [], [])
    if workflow_off:
        if repo and repo != KIT_REPO:
            findings.append({"rule": "A0", "id": "A0:auth-config-off", "where": "workflow",
                             "msg": "auth-config: \"off\" in a repository's workflow: only the kit's own check.yml may switch the login check off"})
        return {"declared": declared, "mode": "absolute", "errors": errors, "findings": findings, "tolerated": [],
                "baseline": [], "checks": [], "login": bool(findings), "escapes": [], "exceptions": [],
                "exception_ids": [], "routes": [], "notes": notes}
    if malformed:
        return {"declared": True, "mode": "absolute", "errors": malformed, "findings": [], "tolerated": [],
                "baseline": [], "checks": [], "login": True, "escapes": [], "exceptions": [],
                "exception_ids": [], "routes": [], "notes": notes}
    exc = exception_entries(cfg, errors) if declared else []
    exc_ids = [e["hatch"] for e in exc]
    esc = escapes(cfg) if declared else []
    if declared and cfg.get("login") is not False and not errors:
        if run_dynamic is False and repo and repo != KIT_REPO:
            esc.append("auth-dynamic:false")
        esc += [f"no_dynamic:{r['route']}" for r in cfg["routes"] if not (cfg.get("server") and r.get("body"))]
    # Every switch needs a registered exception; web_health.py also fails a
    # switch from "absolute" to "baseline" against main.
    for e in esc:
        if e not in exc_ids:
            findings.append({"rule": "A0", "id": f"A0:escape:{e}", "where": "workflow" if e.startswith("auth-") else ".quality/auth.json",
                             "msg": f"`{e}` weakens the login check: register it in `exceptions` (hatch, reason, approved_by, date) or remove it"})
    dyn = []
    if declared and not errors and cfg.get("login") is not False and run_dynamic:
        dyn, derr = dynamic(cfg, root)
        errors += derr
    for d in dyn:
        if not d["ok"]:
            findings.append({"rule": d["rule"], "id": f"{d['rule']}:{d['route']}" + (f":{d['sub']}" if d.get("sub") else ""),
                             "where": d["route"], "msg": d["detail"]})
    baseline = list(cfg.get("baseline") or [])
    mode = cfg.get("mode", "absolute") if declared else "absolute"
    tolerated = [f for f in findings if mode == "baseline" and f["id"] in baseline
                 and not f["id"].startswith("A0:escape:")]
    failing = [f for f in findings if f not in tolerated]
    return {"declared": declared, "mode": mode, "errors": errors, "findings": failing,
            "tolerated": tolerated, "baseline": baseline, "checks": dyn,
            "login": bool(declared and cfg.get("login") is not False) or bool(findings),
            "escapes": esc, "exceptions": exc, "exception_ids": exc_ids,
            "routes": [r.get("route") for r in cfg.get("routes") or []], "source": cfg.get("source") or ["."],
            "d1_calls": cfg.get("d1_calls") or [], "notes": notes}


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check")
    c.add_argument("--config", default=".quality/auth.json")
    c.add_argument("--root", default=".")
    c.add_argument("--no-dynamic", action="store_true")
    c.add_argument("--repo", default="", help="owner/name of the repository being checked (GITHUB_REPOSITORY)")
    c.add_argument("--off", action="store_true", help="the workflow switched the check off (allowed only in the kit's own repository)")
    a = ap.parse_args()
    root = os.path.abspath(a.root)
    cfg_path = a.config if os.path.isabs(a.config) else os.path.join(root, a.config)
    declared = os.path.exists(cfg_path)
    cfg = {}
    if declared:
        try:
            cfg = json.load(open(cfg_path))
        except ValueError as e:
            print(json.dumps({"declared": True, "mode": "absolute", "errors": [f"{a.config} is not valid JSON: {e}"],
                              "findings": [], "tolerated": [], "baseline": [], "checks": [], "login": True,
                              "escapes": [], "exceptions": [], "exception_ids": [], "routes": [], "notes": []}))
            return 0
    print(json.dumps(check(cfg, root, declared, not a.no_dynamic, a.repo, a.off), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
