#!/usr/bin/env python3
"""Proof of the unused-code check: the kit's pinned knip finds an unused file,
export and dependency in a small project, web_ci.sh turns them into cases,
and web_health.py compare fails any case in absolute mode and only a new
one in baseline mode (a moved line is the same case)."""
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
KIT = os.path.abspath(os.path.join(HERE, ".."))
HEALTH = os.path.join(KIT, "scripts", "web_health.py")
KNIP = os.path.join(KIT, "tools", "node_modules", ".bin", "knip")
FAILS = []


def expect(name, ok, detail=""):
    print(("ok   " if ok else "FAIL ") + name)
    if not ok:
        print("     " + str(detail)[:2000])
        FAILS.append(name)


def project(extra=None):
    d = tempfile.mkdtemp()
    files = {
        "package.json": json.dumps({"name": "demo", "private": True, "type": "module", "main": "src/index.js",
                                    "dependencies": {"left-pad": "1.3.0"}}),
        "src/index.js": 'import { used } from "./a.js";\nconsole.log(used);\n',
        "src/a.js": "export const used = 1;\nexport const unusedExport = 2;\n",
        "src/b.js": "export const orphan = 3;\n",
    }
    files.update(extra or {})
    for name, text in files.items():
        os.makedirs(os.path.dirname(os.path.join(d, name)), exist_ok=True)
        open(os.path.join(d, name), "w").write(text)
    return d


def collect(d, mode):
    """The kit's own path: web_ci.sh runs knip, then web_health collect."""
    out = os.path.join(tempfile.mkdtemp(), "out")
    os.makedirs(out)
    ci = open(os.path.join(KIT, "scripts", "web_ci.sh")).read()
    start = ci.index('if [[ -n "${KNIP:-}" ]]; then')
    block = ci[start:ci.index("\nfi\n", start) + 4]
    r = subprocess.run(["bash", "-c", 'log() { :; }; end() { :; }; args=();\n' + block + '\nprintf "%s\\n" "${args[@]}"'],
                       cwd=d, capture_output=True, text=True,
                       env=dict(os.environ, KIT=KIT, BIN=os.path.dirname(KNIP), OUT=out, KNIP=mode))
    if r.returncode or not os.path.exists(os.path.join(out, "knip.json")):
        print("web_ci.sh knip step:", r.returncode, r.stdout, r.stderr)
    c = subprocess.run([sys.executable, HEALTH, "collect", "--root", d, "--knip", os.path.join(out, "knip.json"),
                        "--knip-mode", mode], capture_output=True, text=True)
    return json.loads(c.stdout)


def compare(base, head, *flags):
    d = tempfile.mkdtemp()
    hp, bp = os.path.join(d, "h.json"), "-"
    json.dump(head, open(hp, "w"))
    if base is not None:
        bp = os.path.join(d, "b.json")
        json.dump(base, open(bp, "w"))
    r = subprocess.run([sys.executable, HEALTH, "compare", bp, hp, *flags], capture_output=True, text=True)
    return r.returncode, r.stdout


if not os.path.exists(KNIP):
    print("FAIL knip is not installed in tools/ (npm ci there first)")
    sys.exit(1)

base = collect(project(), "baseline")
cases = {(c["file"], c["rule"], c["text"]) for c in base["metrics"]["knip"]}
expect("knip finds the unused file", ("src/b.js", "unused-file", "src/b.js") in cases, cases)
expect("knip finds the unused export", ("src/a.js", "unused-export", "unusedExport") in cases, cases)
expect("knip finds the unused dependency", ("package.json", "unused-dependency", "left-pad") in cases, cases)
expect("only those three", len(cases) == 3, cases)

rc, panel = compare(None, base)
expect("baseline, first run: does not fail", rc == 0, panel)
rc, panel = compare(base, base)
expect("baseline, same findings as main: passes", rc == 0, panel)
moved = collect(project({"src/a.js": "// a comment above\n\nexport const used = 1;\nexport const unusedExport = 2;\n"}),
                "baseline")
rc, panel = compare(base, moved)
expect("baseline, the same finding on another line is not new", rc == 0, panel)
worse = collect(project({"src/a.js": "export const used = 1;\nexport const unusedExport = 2;\nexport const another = 4;\n"}),
                "baseline")
rc, panel = compare(base, worse)
expect("baseline, a new unused export fails", rc == 1 and "Unused code and dependencies (knip)" in panel, panel)

strict = collect(project(), "absolute")
rc, panel = compare(None, strict, "--no-fail")
expect("absolute: any finding fails, even on the first run with --no-fail", rc == 1, panel)
clean = collect(project({"package.json": json.dumps({"name": "demo", "private": True, "type": "module",
                                                    "main": "src/index.js"}),
                         "src/a.js": "export const used = 1;\n",
                         "knip.json": json.dumps({"ignore": ["src/b.js"]})}), "absolute")
rc, panel = compare(None, clean)
expect("absolute: a clean project (the repo's knip.json is honoured) passes", rc == 0 and not clean["metrics"]["knip"],
       (panel, clean["metrics"].get("knip")))

bad = os.path.join(tempfile.mkdtemp(), "knip.json")
open(bad, "w").write("not json")
c = subprocess.run([sys.executable, HEALTH, "collect", "--knip", bad, "--knip-mode", "baseline"], capture_output=True, text=True)
rc, panel = compare(None, json.loads(c.stdout))
expect("an unreadable report is a configuration error, not a silent pass", rc == 1 and "configuration" in panel, panel)

if FAILS:
    print(f"\n{len(FAILS)} failed")
    sys.exit(1)
print("knip: ok")
