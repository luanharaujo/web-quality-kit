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
import urllib.parse

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


def norm(path):
    """One spelling per page: no query or fragment, percent-escapes decoded
    (/música/ and /m%C3%BAsica/ are the same page)."""
    return urllib.parse.unquote(re.sub(r"[?#].*$", "", path)) or "/"


def page_of(url, base=""):
    """The page as asked (paths input), not where it ended up: a redirect
    (/about to /about/) or a site in a subfolder (https://x.github.io/repo)
    must keep the name the history and --pages use."""
    path = norm(re.sub(r"^https?://[^/]+", "", url))
    prefix = norm(re.sub(r"^https?://[^/]+", "", base or "")).rstrip("/")
    if prefix and (path == prefix or path.startswith(prefix + "/")):
        path = path[len(prefix):] or "/"
    return path


def lab(a):
    today = datetime.date.fromisoformat(a.today) if a.today else datetime.date.today()
    # A config error (even one unrelated to the SLO) fails the run, but only
    # after this week's numbers are in the history: a broken file must not
    # empty the window week after week.
    cfg = budget.load(a.config, today)
    slo = cfg["slo"]
    week = {}
    for path in sorted(glob.glob(os.path.join(a.lhci, "lhr-*.json"))):
        lhr = json.load(open(path))
        errs = budget.profile_errors(lhr)
        if errs:
            print("## SLO\n\n🔴 " + "; ".join(errs))
            return 1
        v = (lhr.get("audits", {}).get("largest-contentful-paint") or {}).get("numericValue")
        page = page_of(lhr.get("requestedUrl") or lhr.get("finalUrl", ""), a.base_url)
        if v is not None:
            week.setdefault(page, []).append(float(v))
    missing = [p for p in a.pages.split() if norm(p) not in week]
    if not week:
        print("## SLO\n\n🔴 no Lighthouse result from the live site for: " + (", ".join(missing) or "any page"))
        print("::error::SLO unknown: no page was measured (Lighthouse failed)", file=sys.stderr)
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
    if cfg["errors"]:
        rows.append("")
        rows.append("🔴 configuration (" + a.config + "):\n" + "\n".join(f"- {e}" for e in cfg["errors"]))
        print(f"::error::{a.config}: configuration errors", file=sys.stderr)
    if not os.path.exists(a.config):
        rows.append(f"\n🟡 {a.config} not found: the kit's goals were used.")
    # Pages measured this week stay in the history; a page without a result
    # is red: no green without a number.
    for page in missing:
        rows.append(f"| 🔴 not measured | `{page}` | no result | | |")
        print(f"::error::SLO unknown: {page} was not measured (Lighthouse failed on it)", file=sys.stderr)
    print("\n".join(rows))
    print("\nLab numbers: phone on a slow network (Lighthouse mobile, Slow 4G, 4x CPU), p75 of the runs.")
    if bad:
        print("::error::SLO at risk or missed: LCP p75 over the goal on the live site", file=sys.stderr)
    return 1 if bad or missing or cfg["errors"] else 0


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
    samples, times = [], []
    last = start
    older = False   # lines before the window: the prober is not new
    if not os.path.isfile(a.csv):
        print(f"## SLO\n\n🔴 prober log {a.csv} not found: fetch it into the job before this step")
        print(f"::error::SLO unknown: prober log {a.csv} not found", file=sys.stderr)
        return 1
    for n, ln in enumerate(open(a.csv), 1):
        ln = ln.strip()
        if not ln or ln.startswith("#"):
            continue
        try:
            t, ok = ln.split(",")[:2]
            when = datetime.datetime.fromisoformat(re.sub(r"Z$", "+00:00", t.strip()))
            if when.tzinfo is None:
                when = when.replace(tzinfo=datetime.timezone.utc)
            up = ok.strip() == "1"
        except ValueError:
            print(f"::warning::{a.csv}:{n}: not 'ISO time,1|0'", file=sys.stderr)
            continue
        if when <= start:
            older = True
        elif when <= now:
            samples.append(up)
            times.append(when)
            last = max(last, when)
    if not samples:
        print("## SLO\n\n🔴 no probe in the window: is the prober running?")
        return 1
    if now - last > datetime.timedelta(hours=a.max_gap_hours):
        print(f"## SLO\n\n🔴 the last probe is from {last.isoformat()}: the prober stopped, so availability is unknown")
        print("::error::SLO unknown: the prober stopped", file=sys.stderr)
        return 1
    avail = 100 * sum(samples) / len(samples)
    budget_share = (100 - avail) / (100 - slo["availability_pct"]) if slo["availability_pct"] < 100 else float(avail < 100)
    if budget_share >= 1:
        state = "🔴 breached"
    elif budget_share >= RISK_SHARE:
        state = "🟡 at risk"
    else:
        state = "🟢 ok"
    times.sort()
    # A known prober (it has lines before the window) is also judged on the
    # silence from the window start to its first probe; a new one is not.
    edges = ([start] if older else []) + times
    gap = max((b - a_ for a_, b in zip(edges, edges[1:])), default=datetime.timedelta(0))
    max_gap = datetime.timedelta(hours=a.max_gap_hours)
    unknown = gap > max_gap
    partial = not older and times[0] - start > max_gap
    if unknown:
        state = "🔴 unknown"
    elif partial and state == "🟢 ok":
        state = "🟡 partial window"
    print(f"## SLO: availability ({slo['window_days']} days)\n")
    print("| | Up | Goal | Error budget spent | Probes | Longest silence |\n|---|---|---|---|---|---|")
    print(f"| {state} | {avail:.3f}% | ≥ {slo['availability_pct']}% | {budget_share * 100:.0f}% | {len(samples)} "
          f"| {gap.total_seconds() / 3600:.1f} h |")
    if unknown:
        print(f"\n🔴 the prober was silent for {gap.total_seconds() / 3600:.0f} h inside the window, so "
              "availability is unknown for that time.")
        print("::error::SLO unknown: the prober was silent inside the window", file=sys.stderr)
        return 1
    if partial:
        days = (now - times[0]).total_seconds() / 86400
        print(f"\n🟡 new prober: {days:.1f} of {slo['window_days']} days watched so far.")
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
    lp.add_argument("--pages", default="", help="pages that must have a result, space separated")
    lp.add_argument("--base-url", default="", help="the site's address; its path (a subfolder) is not part of a page")
    pp = sub.add_parser("probe")
    pp.add_argument("csv")
    pp.add_argument("--config", default="web-budget.json")
    pp.add_argument("--now")
    pp.add_argument("--max-gap-hours", type=float, default=24, help="older last probe = the prober stopped")
    a = p.parse_args()
    return lab(a) if a.cmd == "lab" else probe(a)


if __name__ == "__main__":
    sys.exit(main())
