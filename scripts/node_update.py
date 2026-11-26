#!/usr/bin/env python3
"""The newest Node.js patch or minor of the LTS line a repository pins, from
nodejs.org's own release list (no Renovate, no extra service).

  node_update.py plan <repo dir> [--index index.json|URL] > plan.json
  node_update.py apply <repo dir> plan.json
  node_update.py body plan.json [--lang pt|en]   (title on the first line)

Only exact pins change, and only inside the same major, and only when that
major is an LTS line: .node-version, .nvmrc and package.json "engines.node"
(exact, ^ or ~). A major alone ("22"), an alias ("lts/*") or a range
(">=20") already floats and is left alone. A newer LTS major is reported,
never applied: moving to it is a decision, not an update.

plan.json: {"current", "target", "security", "newer_lts", "files": [{"path",
"old", "new"}]}; "files" is empty when no pin needs a change.
"""
import argparse
import datetime
import json
import os
import re
import sys
import urllib.request

INDEX_URL = "https://nodejs.org/dist/index.json"
EXACT = re.compile(r"^(v?)(\d+)\.(\d+)\.(\d+)$")
ENGINE = re.compile(r"^([\^~]?)(v?)(\d+)\.(\d+)\.(\d+)$")


def ver(text):
    m = re.match(r"^v?(\d+)\.(\d+)\.(\d+)$", text.strip())
    return tuple(int(x) for x in m.groups()) if m else None


def load_index(src):
    if re.match(r"^https://", src):
        req = urllib.request.Request(src, headers={"User-Agent": "web-quality-kit-node-update"})
        with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310 (https only)
            return json.load(r)
    return json.load(open(src))


def pins(repo):
    """[(path, old text, prefix, (major, minor, patch), kind)] of exact pins."""
    out = []
    for name in (".node-version", ".nvmrc"):
        path = os.path.join(repo, name)
        if os.path.isfile(path):
            text = open(path).read().strip()
            m = EXACT.match(text)
            if m:
                out.append((name, text, m.group(1), tuple(int(x) for x in m.groups()[1:]), "file"))
    pkg = os.path.join(repo, "package.json")
    if os.path.isfile(pkg):
        try:
            data = json.load(open(pkg))
            engines = data.get("engines") if isinstance(data, dict) else None
            # Old-style engines (a list) or a package.json that is not an object: no pin there.
            node = engines.get("node", "") if isinstance(engines, dict) else ""
        except ValueError:
            node = ""
        m = ENGINE.match(str(node).strip())
        if m:
            out.append(("package.json", node.strip(), m.group(1) + m.group(2),
                        tuple(int(x) for x in m.groups()[2:]), "engines"))
    return out


ALIAS = "alias"
COOLDOWN_DAYS = 7   # like the kit's Dependabot: a release waits a week, unless it is a security patch


def runtime_major(repo):
    """The major written in .node-version or .nvmrc as an exact version;
    ALIAS when the file floats (an alias like lts/*, or a major alone like
    "22"): then no engines pin is tied to it and the floor is left alone;
    None when there is no version file."""
    seen = False
    for name in (".node-version", ".nvmrc"):
        path = os.path.join(repo, name)
        if os.path.isfile(path):
            seen = True
            m = EXACT.match(open(path).read().strip())
            if m:
                return int(m.group(2))
    return ALIAS if seen else None


def plan(repo, index, today=None):
    found = pins(repo)
    result = {"current": None, "target": None, "security": False, "newer_lts": None, "files": []}
    if not found:
        return result
    # The line the repo runs on: the major of the exact version in its version
    # file; when that file floats (lts/*, "22") nothing changes. Pins of another
    # major (engines "^18.17.0" next to .nvmrc 22) say what else is supported;
    # changing them is a decision, so they are left alone. With a version file,
    # an engines "^"/"~" is a support floor, not the runtime: it stays too.
    major = runtime_major(repo)
    if major == ALIAS:
        return result
    if major is None:
        line = [p for p in found if p[3][0] == max(q[3] for q in found)[0]]
    else:
        line = [p for p in found if p[3][0] == major and (p[4] == "file" or not p[2].lstrip("v"))]
    if not line:
        return result
    # The most behind pin decides: .nvmrc 22.21.1 next to .node-version
    # 22.20.0 still gets the update.
    current = min(p[3] for p in line)
    result["current"] = ".".join(map(str, current))
    releases = [(ver(r["version"]), r) for r in index if ver(r.get("version", ""))]
    same = [(v, r) for v, r in releases if v[0] == current[0]]
    if not same or not any(r.get("lts") for _, r in same):
        return result   # not an LTS line: no automatic update
    today = today or datetime.date.today()
    ripe = today - datetime.timedelta(days=COOLDOWN_DAYS)

    def ready(v, r):
        # A week old, or a security release that is only a patch of the pinned minor.
        try:
            old_enough = datetime.date.fromisoformat(str(r.get("date", ""))[:10]) <= ripe
        except ValueError:
            old_enough = False
        return old_enough or (r.get("security") and v[:2] == current[:2])

    lts_same = [(v, r) for v, r in same if r.get("lts") and ready(v, r)]
    if not lts_same:
        return result
    target = max(v for v, _ in lts_same)
    lts_majors = sorted({v[0] for v, r in releases if r.get("lts")})
    if lts_majors and lts_majors[-1] > current[0]:
        newest = max(v for v, r in releases if r.get("lts") and v[0] == lts_majors[-1])
        result["newer_lts"] = ".".join(map(str, newest))
    if target <= current:
        return result
    result["target"] = ".".join(map(str, target))
    result["security"] = any(r.get("security") for v, r in same if current < v <= target)
    for path, old, prefix, v, kind in line:
        if v < target:
            result["files"].append({"path": path, "old": old, "new": prefix + result["target"], "kind": kind})
    return result


def apply(repo, p):
    for f in p["files"]:
        path = os.path.join(repo, f["path"])
        text = open(path, newline="").read()   # keep CRLF as it is
        if f["kind"] == "engines":
            # Only the value of "node" inside "engines" changes; the rest of
            # package.json keeps its bytes (formatting, order, other fields).
            new, n = re.subn(r'("engines"\s*:\s*\{[^{}]*?"node"\s*:\s*")' + re.escape(f["old"]) + '"',
                             lambda m: m.group(1) + f["new"] + '"', text, count=1)
        else:
            new, n = text.replace(f["old"], f["new"], 1), text.count(f["old"])
        if not n:
            print(f"::error::{f['path']}: {f['old']} not found", file=sys.stderr)
            return 1
        open(path, "w", newline="").write(new)
    return 0


def body(p, lang):
    t = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "pr_texts.json")))[lang]["node_update"]
    v = {"current": p["current"], "target": p["target"], "major": p["target"].split(".")[0],
         "files": ", ".join(f"`{f['path']}`" for f in p["files"]), "newer": p["newer_lts"]}
    why = t["why"] + (" " + t["why_security"].format(**v) if p["security"] else "")
    heads = t["heads"]
    risk = t["risk"] + ("\n\n" + t["newer"].format(**v) if p["newer_lts"] else "")
    return (t["title"].format(**v) + "\n" + f"## {heads[0]}\n{t['what'].format(**v)}\n\n## {heads[1]}\n{why}\n\n"
            f"## {heads[2]}\n{risk}\n\n<details>\n<summary>{heads[3]}</summary>\n\n{t['details']}\n\n</details>\n")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    pp = sub.add_parser("plan")
    pp.add_argument("repo")
    pp.add_argument("--index", default=INDEX_URL)
    pp.add_argument("--today", help="YYYY-MM-DD (tests)")
    pa = sub.add_parser("apply")
    pa.add_argument("repo")
    pa.add_argument("plan")
    pb = sub.add_parser("body")
    pb.add_argument("plan")
    pb.add_argument("--lang", choices=["pt", "en"], default="pt")
    a = ap.parse_args()
    if a.cmd == "body":
        print(body(json.load(open(a.plan)), a.lang), end="")
        return 0
    if a.cmd == "plan":
        today = datetime.date.fromisoformat(a.today) if a.today else None
        print(json.dumps(plan(a.repo, load_index(a.index), today), indent=1))
        return 0
    return apply(a.repo, json.load(open(a.plan)))


if __name__ == "__main__":
    sys.exit(main())
