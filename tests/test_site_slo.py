#!/usr/bin/env python3
"""Proof of the live-site goals: site_slo.py judges LCP p75 over a window of
weekly Lighthouse runs and availability over a prober's log, and only says
"at risk" or "breached" when the goal is in danger; budget.py accepts only
stricter goals."""
import datetime
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SLO = os.path.join(HERE, "..", "scripts", "site_slo.py")
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))
import budget  # noqa: E402

MOBILE = {"formFactor": "mobile", "screenEmulation": {"mobile": True}, "throttlingMethod": "simulate",
          "throttling": {"rttMs": 150, "throughputKbps": 1638.4, "cpuSlowdownMultiplier": 4}}
FAILS = []
LAST = {}


def expect(name, ok, detail=""):
    print(("ok   " if ok else "FAIL ") + name)
    if not ok:
        print("     " + str(detail)[:1500])
        FAILS.append(name)


def config(slo=None):
    path = os.path.join(tempfile.mkdtemp(), "web-budget.json")
    json.dump({"slo": slo} if slo is not None else {}, open(path, "w"))
    return path


def lab(lcps, history=(), page="/", settings=MOBILE, today="2026-11-26", slo=None, pages="", site="https://example.org",
        final=None):
    d = tempfile.mkdtemp()
    for i, lcp in enumerate(lcps):
        json.dump({"requestedUrl": f"{site}{page}", "finalUrl": f"{site}{final or page}",
                   "configSettings": settings,
                   "audits": {"largest-contentful-paint": {"numericValue": lcp}}},
                  open(os.path.join(d, f"lhr-{i}.json"), "w"))
    hist = os.path.join(d, "history.jsonl")
    with open(hist, "w") as f:
        f.writelines(json.dumps(h) + "\n" for h in history)
    r = subprocess.run([sys.executable, SLO, "lab", d, "--history", hist, "--config", config(slo), "--today", today, "--pages", pages,
                        "--base-url", site],
                       capture_output=True, text=True)
    LAST["err"] = r.stderr
    return r.returncode, r.stdout, [json.loads(ln) for ln in open(hist) if ln.strip()]


def weeks(values, page="/", today="2026-11-26", skip=0):
    day = datetime.date.fromisoformat(today)
    return [{"date": (day - datetime.timedelta(days=7 * (i + 1 + skip))).isoformat(), "page": page, "lcp_p75_ms": v}
            for i, v in enumerate(values)]


def probe(downs, total=8100, now="2026-11-26T12:00:00+00:00", extra=""):
    end = datetime.datetime.fromisoformat(now)
    path = os.path.join(tempfile.mkdtemp(), "probes.csv")
    with open(path, "w") as f:
        f.write(extra)
        for i in range(total):
            t = end - datetime.timedelta(minutes=5 * i)
            f.write(f"{t.isoformat()},{0 if i < downs else 1}\n")
    r = subprocess.run([sys.executable, SLO, "probe", path, "--config", config(), "--now", now],
                       capture_output=True, text=True)
    return r.returncode, r.stdout, r.stderr


# --- LCP p75 over the window ----------------------------------------------------
rc, out, hist = lab([1800, 1900, 2000])
expect("lab: fast site is ok", rc == 0 and "🟢 ok" in out, out)
expect("lab: this week goes into the history", [h["page"] for h in hist] == ["/"] and hist[0]["lcp_p75_ms"] == 1950, hist)
expect("lab: query strings do not split a page", out.count("| `/") == 1, out)
rc, out, hist = lab([2100], [{"date": "2026-11-26", "page": "/", "lcp_p75_ms": 1500}])
expect("lab: a rerun on the same day replaces that day", len(hist) == 1 and hist[0]["lcp_p75_ms"] == 2100
       and "(1 week)" in out, (hist, out))
rc, out, _ = lab([3000, 3100, 3200], weeks([1800, 1900, 2000]))
expect("lab: one slow week is at risk (the window still meets the goal)", rc == 1 and "🟡 at risk" in out, out)
rc, out, _ = lab([3000, 3100, 3200], weeks([2900, 3000, 3100]))
expect("lab: a slow window is breached", rc == 1 and "🔴 breached" in out, out)
rc, out, hist = lab([1800], weeks([3000] * 20, skip=4))
expect("lab: weeks before the window do not count", rc == 0 and "🟢 ok" in out, out)
expect("lab: history older than window + 90 days is dropped", len(hist) < 21, len(hist))
rc, out, _ = lab([2200], slo={"lcp_p75_ms": 2000})
expect("lab: a stricter goal from web-budget.json is used", rc == 1 and "≤ 2.00 s" in out, out)
rc, out, hist = lab([1800], slo={"window_days": 3})
expect("lab: a config error fails after the week is kept in the history",
       rc == 1 and "configuration" in out and len(hist) == 1, (out, hist))
rc, out, _ = lab([1800], settings=dict(MOBILE, formFactor="desktop"))
expect("lab: a desktop run is refused", rc == 1 and "phone" in out, out)

rc, out, hist = lab([1800], pages="/ /contact/")
expect("lab: a page Lighthouse did not measure fails (no green without a number)",
       rc == 1 and "🔴 not measured | `/contact/`" in out, out)
expect("lab: the pages measured that week still go into the history", [h["page"] for h in hist] == ["/"], hist)
expect("lab: a missing page is not reported as LCP over the goal",
       "not measured" in LAST["err"] and "over the goal" not in LAST["err"], LAST["err"])
rc, out, _ = lab([1800], pages="/")
expect("lab: every expected page measured is ok", rc == 0, out)
rc, out, hist = lab([1800], site="https://x.github.io/repo", pages="/")
expect("lab: a site in a subfolder keeps the page name", rc == 0 and hist[0]["page"] == "/", (out, hist))
rc, out, hist = lab([1800], page="/r%C3%A9sum%C3%A9/", pages="/r\u00e9sum\u00e9/")
expect("lab: an accented path names the same page as its escaped form", rc == 0 and hist[0]["page"] == "/r\u00e9sum\u00e9/",
       (out, hist))
rc, out, hist = lab([1800], page="/", pages="/ /?lang=en")
expect("lab: a query is another page (/?lang=en not measured is red, not mixed with /)",
       rc == 1 and "not measured | `/?lang=en`" in out, out)
rc, out, hist = lab([1800], page="/?lang=en", pages="/?lang=en", site="https://x.github.io/repo")
expect("lab: a query on a subfolder site keeps its name", rc == 0 and hist[0]["page"] == "/?lang=en", (out, hist))
rc, out, hist = lab([1800], page="/about", final="/about/", pages="/about")
expect("lab: a redirect keeps the asked page name", rc == 0 and hist[0]["page"] == "/about", (out, hist))

# --- availability over the prober's log -------------------------------------------
rc, out, _ = probe(0)
expect("probe: always up is ok", rc == 0 and "🟢 ok" in out and "100.000%" in out, out)
# Goal 99.9% = 0.1% error budget. A probe every 5 minutes fills the 28 days
# with 8064 probes: 2 down = 25% spent, 5 down = 62% (at risk), 9 down = 112%.
rc, out, _ = probe(2)
expect("probe: a quarter of the budget spent is still ok", rc == 0 and "🟢 ok" in out and "25%" in out, out)
rc, out, _ = probe(5)
expect("probe: more than half of the budget spent is at risk", rc == 1 and "🟡 at risk" in out and "62%" in out, out)
rc, out, _ = probe(9)
expect("probe: the whole budget spent is breached", rc == 1 and "🔴 breached" in out, out)
rc, out, _ = probe(0, total=0)
expect("probe: no probe in the window is an error (is the prober running?)", rc == 1 and "prober" in out, out)
path = os.path.join(tempfile.mkdtemp(), "probes.csv")
open(path, "w").write("2026-11-01T12:00:00+00:00,1\n")
r = subprocess.run([sys.executable, SLO, "probe", path, "--config", config(), "--now", "2026-11-26T12:00:00+00:00"],
                   capture_output=True, text=True)
expect("probe: a prober that stopped days ago is not green", r.returncode == 1 and "stopped" in r.stdout, r.stdout)
open(path, "w").writelines([f"2026-11-{d:02d}T12:00:00+00:00,1\n" for d in (1, 2)] +
                           [f"2026-11-26T{h:02d}:00:00+00:00,1\n" for h in range(12)])
r = subprocess.run([sys.executable, SLO, "probe", path, "--config", config(), "--now", "2026-11-26T12:00:00+00:00"],
                   capture_output=True, text=True)
expect("probe: a long silence inside the window is unknown, not green",
       r.returncode == 1 and "silent for" in r.stdout and "🟢" not in r.stdout, r.stdout)
open(path, "w").write("2026-10-01T12:00:00+00:00,1\n2026-11-26T11:00:00+00:00,1\n")
r = subprocess.run([sys.executable, SLO, "probe", path, "--config", config(), "--now", "2026-11-26T12:00:00+00:00"],
                   capture_output=True, text=True)
expect("probe: a known prober silent from the window start until today is unknown",
       r.returncode == 1 and "🔴 unknown" in r.stdout, r.stdout)
open(path, "w").writelines(f"2026-11-26T{h:02d}:00:00+00:00,1\n" for h in range(12))
r = subprocess.run([sys.executable, SLO, "probe", path, "--config", config(), "--now", "2026-11-26T12:00:00+00:00"],
                   capture_output=True, text=True)
expect("probe: a new prober is a partial window (yellow, does not fail)",
       r.returncode == 0 and "🟡 partial window" in r.stdout and "new prober" in r.stdout, r.stdout)
r = subprocess.run([sys.executable, SLO, "probe", "/nonexistent/probes.csv", "--config", config(),
                    "--now", "2026-11-26T12:00:00+00:00"], capture_output=True, text=True)
expect("probe: a missing log is a clear red line, not a traceback",
       r.returncode == 1 and "not found" in r.stdout and "Traceback" not in r.stderr, r.stdout + r.stderr)
open(path, "w").writelines(f"2026-11-26T{h:02d}:00:00Z,1\n" for h in range(12))
r = subprocess.run([sys.executable, SLO, "probe", path, "--config", config(), "--now", "2026-11-26T12:00:00+00:00"],
                   capture_output=True, text=True)
expect("probe: times ending in Z are read", "| 12 |" in r.stdout and "warning" not in r.stderr, r.stdout + r.stderr)
rc, out, err = probe(0, extra="not a line\n")
expect("probe: a bad line is a warning, not a crash", rc == 0 and "not 'ISO time,1|0'" in err, err)
rc, out, _ = probe(0, extra="2026-10-01T00:00:00+00:00,0\n" * 50)
expect("probe: probes before the window do not count", rc == 0 and "🟢 ok" in out, out)

# --- goals in web-budget.json: stricter only --------------------------------------
today = datetime.date(2026, 11, 26)
for slo, bad in (({"availability_pct": 99.0}, "availability_pct"), ({"availability_pct": 101}, "availability_pct"),
                 ({"lcp_p75_ms": 4000}, "lcp_p75_ms"), ({"lcp_p75_ms": 0}, "lcp_p75_ms"),
                 ({"window_days": 3}, "window_days"), ({"window_days": 7.5}, "window_days"),
                 ({"uptime": 99.99}, "uptime"), ({"lcp_p75_ms": True}, "lcp_p75_ms"), ([], "slo")):
    errs = budget.load(config(slo), today)["errors"]
    expect(f"slo config refused: {json.dumps(slo)}", any(bad in e for e in errs), errs)
cfg = budget.load(config({"availability_pct": 99.95, "lcp_p75_ms": 2000, "window_days": 14}), today)
expect("slo config: stricter goals accepted", not cfg["errors"] and cfg["slo"] ==
       {"availability_pct": 99.95, "lcp_p75_ms": 2000, "window_days": 14}, cfg)
expect("slo config: defaults without an entry", budget.load(config(), today)["slo"] == budget.SLO_DEFAULTS, "")

if FAILS:
    print(f"\n{len(FAILS)} failed")
    sys.exit(1)
print("site slo: ok")
