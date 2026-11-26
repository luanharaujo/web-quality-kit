#!/usr/bin/env python3
"""Service level objectives of a live site, and when they are at risk.

  site_slo.py lab <lhci dir> --history history.jsonl [--config web-budget.json]
             [--today YYYY-MM-DD] > report.md
      Weekly Lighthouse on the live site (phone, slow network). Appends this
      week's LCP p75 per page (over the runs) to the history file and judges
      the window (default 28 days): breached = the window's p75 is over the
      goal; at risk = this week is over the goal. Exit 1 in both cases.

  site_slo.py probe <probes.csv> [--config web-budget.json] [--now ISO] > report.md
      Availability from a prober's log ("ISO time,1|0" per line, 1 = up).
      Error budget = (100 - goal)% of the window. At risk = half of the budget
      already spent; breached = all of it. Exit 1 in both cases. The prober
      itself wakes the owner when the site is down (an outage is an event);
      this is the slower question "will we miss the month's goal?".

Goals in web-budget.json, stricter only (budget.py validates):
  "slo": {"availability_pct": 99.9, "lcp_p75_ms": 2500, "window_days": 28}
"""
import argparse
import datetime
import glob
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import budget  # noqa: E402

RISK_SHARE = 0.5


def p75(values):
    v = sorted(values)
    if not v:
        return None
    k = (len(v) - 1) * 0.75
    lo, hi = int(k), min(int(k) + 1, len(v) - 1)
    return v[lo] + (v[hi] - v[lo]) * (k - lo)


def lab(a):
    today = datetime.date.fromisoformat(a.today) if a.today else datetime.date.today()
    cfg = budget.load(a.config, today)
    if cfg["errors"]:
        print("## SLO\n\n🔴 configuration:\n" + "\n".join(f"- {e}" for e in cfg["errors"]))
        return 1
    slo = cfg["slo"]
    week = {}
    for path in sorted(glob.glob(os.path.join(a.lhci, "lhr-*.json"))):
        lhr = json.load(open(path))
        errs = budget.profile_errors(lhr)
        if errs:
            print("## SLO\n\n🔴 " + "; ".join(errs))
            return 1
        v = (lhr.get("audits", {}).get("largest-contentful-paint") or {}).get("numericValue")
        page = re.sub(r"^https?://[^/]+", "", lhr.get("finalUrl") or lhr.get("requestedUrl", "")) or "/"
        page = re.sub(r"[?#].*$", "", page) or "/"
        if v is not None:
            week.setdefault(page, []).append(float(v))
    if not week:
        print("## SLO\n\n🔴 no Lighthouse result from the live site")
        return 1
    history = []
    if a.history and os.path.exists(a.history):
        history = [json.loads(ln) for ln in open(a.history) if ln.strip()]
    # A rerun on the same day replaces that day's number instead of adding a week.
    history = [h for h in history if not (h["date"] == today.isoformat() and h["page"] in week)]
    for page, vals in week.items():
        history.append({"date": today.isoformat(), "page": page, "lcp_p75_ms": round(p75(vals), 1)})
    start = today - datetime.timedelta(days=slo["window_days"])
    history = [h for h in history if datetime.date.fromisoformat(h["date"]) > start - datetime.timedelta(days=90)]
    if a.history:
        with open(a.history, "w") as f:
            f.writelines(json.dumps(h) + "\n" for h in history)
    goal = slo["lcp_p75_ms"]
    rows, bad = [], False
    for page in sorted(week):
        window = [h["lcp_p75_ms"] for h in history if h["page"] == page and datetime.date.fromisoformat(h["date"]) > start]
        now = p75(week[page])
        win = p75(window)
        if win > goal:
            state, bad = "🔴 breached", True
        elif now > goal:
            state, bad = "🟡 at risk", True
        else:
            state = "🟢 ok"
        rows.append(f"| {state} | `{page}` | {now / 1000:.2f} s | {win / 1000:.2f} s ({len(window)} week{'s' if len(window) != 1 else ''}) | ≤ {goal / 1000:.2f} s |")
    print(f"## SLO: LCP p75 on the live site ({slo['window_days']} days)\n")
    print("| | Page | This week | Window | Goal |\n|---|---|---|---|---|")
    print("\n".join(rows))
    print("\nLab numbers: phone on a slow network (Lighthouse mobile, Slow 4G, 4x CPU), p75 of the runs.")
    if bad:
        print("::error::SLO at risk or missed: LCP p75 over the goal on the live site", file=sys.stderr)
    return 1 if bad else 0


def probe(a):
    now = datetime.datetime.fromisoformat(a.now) if a.now else datetime.datetime.now(datetime.timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=datetime.timezone.utc)
    cfg = budget.load(a.config, now.date())
    if cfg["errors"]:
        print("## SLO\n\n🔴 configuration:\n" + "\n".join(f"- {e}" for e in cfg["errors"]))
        return 1
    slo = cfg["slo"]
    start = now - datetime.timedelta(days=slo["window_days"])
    samples = []
    for n, ln in enumerate(open(a.csv), 1):
        ln = ln.strip()
        if not ln or ln.startswith("#"):
            continue
        try:
            t, ok = ln.split(",")[:2]
            when = datetime.datetime.fromisoformat(t.strip())
            if when.tzinfo is None:
                when = when.replace(tzinfo=datetime.timezone.utc)
            up = ok.strip() == "1"
        except ValueError:
            print(f"::warning::{a.csv}:{n}: not 'ISO time,1|0'", file=sys.stderr)
            continue
        if start < when <= now:
            samples.append(up)
    if not samples:
        print("## SLO\n\n🔴 no probe in the window: is the prober running?")
        return 1
    avail = 100 * sum(samples) / len(samples)
    budget_share = (100 - avail) / (100 - slo["availability_pct"]) if slo["availability_pct"] < 100 else float(avail < 100)
    if budget_share >= 1:
        state = "🔴 breached"
    elif budget_share >= RISK_SHARE:
        state = "🟡 at risk"
    else:
        state = "🟢 ok"
    print(f"## SLO: availability ({slo['window_days']} days)\n")
    print("| | Up | Goal | Error budget spent | Probes |\n|---|---|---|---|---|")
    print(f"| {state} | {avail:.3f}% | ≥ {slo['availability_pct']}% | {budget_share * 100:.0f}% | {len(samples)} |")
    if budget_share >= RISK_SHARE:
        print(f"::error::SLO at risk: availability {avail:.3f}%, {budget_share * 100:.0f}% of the error budget spent",
              file=sys.stderr)
        return 1
    return 0


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    lp = sub.add_parser("lab")
    lp.add_argument("lhci")
    lp.add_argument("--history", default="")
    lp.add_argument("--config", default="web-budget.json")
    lp.add_argument("--today")
    pp = sub.add_parser("probe")
    pp.add_argument("csv")
    pp.add_argument("--config", default="web-budget.json")
    pp.add_argument("--now")
    a = p.parse_args()
    return lab(a) if a.cmd == "lab" else probe(a)


if __name__ == "__main__":
    sys.exit(main())
