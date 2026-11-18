#!/usr/bin/env python3
"""Proof of "do not get worse": each gated metric fails when it gets worse
than main, cases that already existed only warn, the first run (no numbers
from main) never fails, and Lighthouse only warns."""
import copy
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
HEALTH = os.path.join(HERE, "..", "scripts", "web_health.py")

BASE = {"kit": 1, "metrics": {
    "eslint": [{"file": "a.js", "rule": "complexity", "line": 3, "text": "function f() {", "level": "warning"}],
    "tsc": [],
    "html": [],
    "links": [],
    "axe": [{"file": "/", "rule": "color-contrast", "line": 0, "text": "", "value": 2}],
    "data": [],
    "modern": [],
    "bundle": {"kb": 500, "gzip_kb": 100.0, "largest": [], "max_kb": 300},
    "lighthouse": {"/": {"performance": 95, "accessibility": 98, "best-practices": 96, "seo": 100}},
    "audit": {"critical": 0, "high": 1, "moderate": 2, "low": 0},
    "tests": {"passed": 64, "failed": 0, "exit": 0},
}}


def compare(base, head, *flags):
    d = tempfile.mkdtemp()
    bp, hp = os.path.join(d, "b.json"), os.path.join(d, "h.json")
    json.dump(head, open(hp, "w"))
    if base is None:
        bp = "-"
    else:
        json.dump(base, open(bp, "w"))
    r = subprocess.run([sys.executable, HEALTH, "compare", bp, hp, *flags], capture_output=True, text=True)
    return r.returncode, r.stdout


def worse(change):
    head = copy.deepcopy(BASE)
    change(head["metrics"])
    return head


def main():
    rc, out = compare(BASE, BASE)
    assert rc == 0, out
    assert "🔴" not in out and "against main" in out, out

    # Each of these must fail.
    bad = {
        "new lint case": lambda m: m["eslint"].append(
            {"file": "b.js", "rule": "no-unused-vars", "line": 1, "text": "const x = 1", "level": "error"}),
        "new type error": lambda m: m["tsc"].append({"file": "src/a.ts", "rule": "TS2322", "line": 4, "text": "x"}),
        "new HTML error": lambda m: m["html"].append({"file": "index.html", "rule": "no-dup-id", "line": 9, "text": "x"}),
        "new broken link": lambda m: m["links"].append({"file": "/", "rule": "404", "line": 0, "text": "/gone"}),
        "new axe rule": lambda m: m["axe"].append({"file": "/", "rule": "image-alt", "line": 0, "text": "", "value": 1}),
        "personal data": lambda m: m["data"].append({"file": "a.html", "rule": "cpf", "line": 2, "text": "h", "masked": "5**5"}),
        "bundle +20%": lambda m: m["bundle"].update(gzip_kb=120.0),
        "high vulnerability": lambda m: m["audit"].update(high=2),
        "failing test": lambda m: m["tests"].update(failed=1, exit=1),
        "fewer tests": lambda m: m["tests"].update(passed=60),
    }
    for name, change in bad.items():
        rc, out = compare(BASE, worse(change))
        assert rc == 1, f"{name}: should fail\n{out}"
        assert "🔴" in out, f"{name}: no red dot\n{out}"
        rc, _ = compare(BASE, worse(change), "--no-fail")
        assert rc == 0, f"{name}: --no-fail must not fail"

    # These only warn.
    ok = {
        "same lint case on another line": lambda m: m["eslint"][0].update(line=40),
        "Lighthouse drop": lambda m: m["lighthouse"]["/"].update(performance=60),
        "modern CSS case": lambda m: m["modern"].append({"file": "a.css", "rule": "css/use-baseline", "line": 1, "text": "x"}),
        "bundle +5%": lambda m: m["bundle"].update(gzip_kb=105.0),
        "moderate vulnerability": lambda m: m["audit"].update(moderate=5),
        "fixed lint case": lambda m: m["eslint"].clear(),
    }
    for name, change in ok.items():
        rc, out = compare(BASE, worse(change))
        assert rc == 0, f"{name}: should not fail\n{out}"
    rc, out = compare(BASE, worse(ok["Lighthouse drop"]))
    assert "🟡 | Lighthouse Performance" in out, out
    rc, out = compare(BASE, worse(ok["Lighthouse drop"]), "--lang", "pt")
    assert "🟡 | Lighthouse Desempenho" in out and "contra a main" in out, out

    # First run: everything bad is shown, nothing fails.
    rc, out = compare(None, worse(lambda m: [f(m) for f in bad.values()]))
    assert rc == 0 and "baseline" in out, out

    # A metric main did not have yet (new in the kit) is a baseline too.
    base = worse(lambda m: m.pop("html"))
    rc, out = compare(base, worse(bad["new HTML error"]))
    assert rc == 0, out
    print("web_health: ok")


if __name__ == "__main__":
    main()
