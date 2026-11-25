#!/usr/bin/env python3
"""Proof of the performance budget: budget.py measures and validates the
config (a looser limit without a registered exception, an expired exception
or a desktop/fast-network Lighthouse run are errors), and web_health.py
compare fails a page over its limit in absolute mode (always) and in
baseline mode only when the page is new or got worse than main."""
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
BUDGET = os.path.join(HERE, "..", "scripts", "budget.py")
HEALTH = os.path.join(HERE, "..", "scripts", "web_health.py")
MOBILE = {"formFactor": "mobile", "screenEmulation": {"mobile": True}, "throttlingMethod": "simulate",
          "throttling": {"rttMs": 150, "throughputKbps": 1638.4, "cpuSlowdownMultiplier": 4}}


def lhr(page, lcp=1200, cls=0.0, tbt=50, total_kb=300, js_kb=40, settings=MOBILE):
    return {"requestedUrl": f"http://localhost:1234{page}", "configSettings": settings, "audits": {
        "largest-contentful-paint": {"numericValue": lcp},
        "cumulative-layout-shift": {"numericValue": cls},
        "total-blocking-time": {"numericValue": tbt},
        "total-byte-weight": {"numericValue": total_kb * 1024},
        "network-requests": {"details": {"items": [
            {"resourceType": "Document", "transferSize": 10 * 1024},
            {"resourceType": "Script", "transferSize": js_kb * 1024}]}}}}


MIGRATION = {"migration": {"reason": "old site being brought to the budget", "until": "2027-06-01"}}


def run_budget(reports, config=None, mode="absolute", pages=None):
    d = tempfile.mkdtemp()
    if mode == "baseline" and config is None:
        config = MIGRATION
    for i, r in enumerate(reports):
        json.dump(r, open(os.path.join(d, f"lhr-{i}.json"), "w"))
    cfg = os.path.join(d, "web-budget.json")
    if config is not None:
        json.dump(config, open(cfg, "w"))
    pages = pages if pages is not None else " ".join(sorted({r["requestedUrl"][21:] for r in reports}))
    out = subprocess.run([sys.executable, BUDGET, "check", d, "--mode", mode, "--config", cfg,
                          "--pages", pages, "--today", "2026-11-25"],
                         capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def compare(base_budget, head_budget, *flags):
    d = tempfile.mkdtemp()
    hp = os.path.join(d, "h.json")
    json.dump({"kit": 1, "metrics": {"budget": head_budget}}, open(hp, "w"))
    bp = "-"
    if base_budget is not None:
        bp = os.path.join(d, "b.json")
        json.dump({"kit": 1, "metrics": {"budget": base_budget}}, open(bp, "w"))
    r = subprocess.run([sys.executable, HEALTH, "compare", bp, hp, *flags], capture_output=True, text=True)
    return r.returncode, r.stdout


failures = []


def expect(name, cond, detail=""):
    print(("ok   " if cond else "FAIL ") + name)
    if not cond:
        failures.append(f"{name}: {detail}")


# --- budget.py ---------------------------------------------------------------
good = run_budget([lhr("/"), lhr("/"), lhr("/", lcp=3000)])
expect("median of the runs", good["pages"]["/"]["lcp_ms"]["value"] == 1200, good)
expect("defaults without a config file", good["pages"]["/"]["lcp_ms"]["limit"] == 2500 and not good["errors"], good)
expect("JS weight is the sum of Script requests", good["pages"]["/"]["js_kb"]["value"] == 40, good)

r = run_budget([lhr("/")], {"limits": {"lcp_ms": 2000, "js_kb": 30}})
expect("stricter limit applies", r["pages"]["/"]["lcp_ms"]["limit"] == 2000 and not r["errors"], r)

r = run_budget([lhr("/")], {"limits": {"page_kb": 2048}})
expect("looser limit without exception is an error", any("looser" in e for e in r["errors"]), r)
expect("looser limit is not applied", r["pages"]["/"]["page_kb"]["limit"] == 1024, r)

r = run_budget([lhr("/")], {"limits": {"speed_index": 1}})
expect("unknown limit is an error", any("unknown limit" in e for e in r["errors"]), r)

exc = {"limit": "page_kb", "value": 1500, "pages": ["/gallery/"], "reason": "photo gallery with large originals",
       "approved_by": "owner", "date": "2026-11-20", "expires": "2027-05-20"}
r = run_budget([lhr("/", total_kb=1200), lhr("/gallery/", total_kb=1200)], {"exceptions": [exc]})
expect("exception raises the limit on its page", r["pages"]["/gallery/"]["page_kb"]["limit"] == 1500
       and r["pages"]["/gallery/"]["page_kb"]["exception"], r)
expect("exception does not reach other pages", r["pages"]["/"]["page_kb"]["limit"] == 1024, r)

r = run_budget([lhr("/")], {"exceptions": [dict(exc, expires="2026-11-01")]})
expect("expired exception is an error", any("expired" in e for e in r["errors"]), r)
r = run_budget([lhr("/")], {"exceptions": [{k: v for k, v in exc.items() if k != "approved_by"}]})
expect("exception without approver is an error", any("missing approved_by" in e for e in r["errors"]), r)

r = run_budget([lhr("/")], {"exceptions": [dict(exc, value="1500")]})
expect("exception with a non-numeric value is an error", any("value must be a number" in e for e in r["errors"]), r)

desktop = dict(MOBILE, formFactor="desktop", screenEmulation={"mobile": False})
r = run_budget([lhr("/", settings=desktop)])
expect("desktop run is an error", any("phone" in e for e in r["errors"]), r)
fast = dict(MOBILE, throttling={"rttMs": 40, "throughputKbps": 10240, "cpuSlowdownMultiplier": 1})
r = run_budget([lhr("/", settings=fast)])
expect("fast network run is an error", any("Slow 4G" in e for e in r["errors"]), r)
r = run_budget([lhr("/", settings=dict(MOBILE, throttlingMethod="provided"))])
expect("unthrottled run is an error", any("simulate" in e for e in r["errors"]), r)

r = run_budget([lhr("/")], pages="/ /about/")
expect("page without a Lighthouse result is an error", any("/about/" in e for e in r["errors"]), r)
r = run_budget([], pages="")
expect("no Lighthouse at all is an error", r["errors"], r)

r = run_budget([lhr("/")], {}, mode="baseline")
expect("baseline without a migration plan is an error", any("migration" in e for e in r["errors"]), r)
r = run_budget([lhr("/")], {"migration": {"reason": "old site being brought over", "until": "2026-11-01"}}, mode="baseline")
expect("baseline with a passed migration date is an error", any("has passed" in e for e in r["errors"]), r)
r = run_budget([lhr("/")], {"exceptions": [{"rule": "img-format", "reason": "social preview images",
                                            "approved_by": "owner", "date": "2026-11-20"}]})
expect("rule exception without files is an error", any("files" in e for e in r["errors"]), r)
r = run_budget([lhr("/")], {"exceptions": [{"rule": "no-such-rule", "files": ["a"], "reason": "some reason here",
                                            "approved_by": "owner", "date": "2026-11-20"}]})
expect("unknown rule is an error", any("unknown rule" in e for e in r["errors"]), r)
r = run_budget([lhr("/")], {"limits": {"img_kb": 500}})
expect("looser static limit is an error too", any("looser" in e for e in r["errors"]), r)

# --- compare: absolute -------------------------------------------------------
slow = run_budget([lhr("/", lcp=4000)])
rc, panel = compare(None, slow)
expect("absolute: over the limit fails even on the first run", rc == 1, panel)
expect("absolute: panel shows the page", "`/`" in panel and "🔴" in panel, panel)
rc, panel = compare(None, slow, "--no-fail")
expect("absolute: --no-fail does not switch the budget off", rc == 1, panel)
rc, panel = compare(None, good)
expect("absolute: within budget passes", rc == 0, panel)
cfg_err = run_budget([lhr("/")], {"limits": {"cls": 0.5}})
rc, panel = compare(None, cfg_err, "--no-fail")
expect("bad config fails in any mode", rc == 1 and "configuration" in panel, panel)

# --- compare: baseline -------------------------------------------------------
old = run_budget([lhr("/", lcp=4000, js_kb=300), lhr("/b/")], mode="baseline")
rc, panel = compare(None, old)
expect("baseline: first run never fails", rc == 0, panel)
same = run_budget([lhr("/", lcp=4200, js_kb=300), lhr("/b/")], mode="baseline")
rc, panel = compare(old, same)
expect("baseline: over the limit but within noise of main passes", rc == 0, panel)
expect("baseline: still shown as not met", "🟡" in panel, panel)
worse = run_budget([lhr("/", lcp=5000, js_kb=300), lhr("/b/")], mode="baseline")
rc, panel = compare(old, worse)
expect("baseline: worse than main fails", rc == 1, panel)
heavier = run_budget([lhr("/", lcp=4000, js_kb=320), lhr("/b/")], mode="baseline")
rc, panel = compare(old, heavier)
expect("baseline: more JS than main fails", rc == 1, panel)
newpage = run_budget([lhr("/", lcp=4000, js_kb=300), lhr("/b/"), lhr("/new/", cls=0.3)], mode="baseline")
rc, panel = compare(old, newpage)
expect("baseline: a new page must meet the budget", rc == 1, panel)
broke = run_budget([lhr("/", lcp=4000, js_kb=300), lhr("/b/", cls=0.2)], mode="baseline")
rc, panel = compare(old, broke)
expect("baseline: a page that was in budget and left it fails", rc == 1, panel)
d = tempfile.mkdtemp()
bp, hp = os.path.join(d, "b.json"), os.path.join(d, "h.json")
json.dump({"kit": 1, "metrics": {"tests": {"passed": 1, "failed": 0, "exit": 0}}}, open(bp, "w"))
json.dump({"kit": 1, "metrics": {"tests": {"passed": 1, "failed": 0, "exit": 0}, "budget": old}}, open(hp, "w"))
r = subprocess.run([sys.executable, HEALTH, "compare", bp, hp], capture_output=True, text=True)
expect("baseline: the PR that turns the budget on (main has no budget numbers) does not fail", r.returncode == 0, r.stdout)
met = run_budget([lhr("/"), lhr("/b/")], mode="baseline")
rc, panel = compare(old, met)
expect("baseline: met everywhere suggests absolute", rc == 0 and "switch budget-mode to absolute" in panel, panel)
rc, panel = compare(old, worse, "--lang", "pt")
PT = json.load(open(os.path.join(HERE, "..", "scripts", "panel_pt.json")))
expect("panel in Portuguese", PT["Performance budget per page"] in panel and PT["(worst page)"] in panel, panel)

if failures:
    print("\n".join(failures), file=sys.stderr)
    sys.exit(1)
print("budget: ok")
