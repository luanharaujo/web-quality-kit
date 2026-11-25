#!/usr/bin/env python3
"""Performance budget: lab Core Web Vitals and page weight, per page.

  budget.py check <lhci dir> --mode absolute|baseline [--config web-budget.json]
                  [--pages "/ /about/"] > budget.json

Reads the Lighthouse reports (lhr-*.json, median of the runs per page) and
the repository's web-budget.json (optional). The kit's defaults are the
budget; a repository may only make a limit STRICTER. A looser limit needs an
entry in "exceptions" (reason, who approved, date, optional expiry); without
it, or with an expired one, the config is an error and the job fails in any
mode.

Lighthouse must have run as a phone on a slow network (formFactor mobile,
simulated throttling at least as slow as Lighthouse's "Slow 4G"): numbers
taken on a fast desktop say nothing about real visitors, so other settings
are an error too.

The verdict (absolute: any page over a limit fails; baseline: only a page
that got worse than main fails) is web_health.py compare's job; this script
only measures and validates.

web-budget.json:
  {
    "limits": {"lcp_ms": 2000, "js_kb": 50},
    "exceptions": [
      {"limit": "page_kb", "value": 1500, "pages": ["/gallery/"],
       "reason": "photo gallery", "approved_by": "owner", "date": "2026-11-25",
       "expires": "2027-05-25"},
      {"rule": "img-format", "files": ["og/*.png"], "reason": "social previews need PNG",
       "approved_by": "owner", "date": "2026-11-25"}
    ],
    "migration": {"reason": "old site, images being converted", "until": "2027-02-01"},
    "cache": {"hashed_extra": ["^static/v\\d+/"]}
  }

"limits" also takes the static rules' limits (site_rules.py): img_px, img_kb,
font_weights, img_eager. "migration" is required in baseline mode.
"""
import argparse
import datetime
import fnmatch
import glob
import json
import os
import re
import statistics
import sys

# Kit defaults (the strictest a repository gets without asking): Google's
# "good" thresholds for LCP and CLS; TBT stands in for INP in the lab
# (Lighthouse's "good" TBT on mobile is 200 ms); weights are transfer sizes.
LH_DEFAULTS = {
    "lcp_ms": 2500,
    "cls": 0.1,
    "tbt_ms": 200,
    "page_kb": 1024,
    "js_kb": 150,
}
# Static rules on the built site (site_rules.py): image file size, fonts,
# images allowed above the fold without loading="lazy".
STATIC_DEFAULTS = {
    "img_px": 1600,
    "img_kb": 300,
    "font_weights": 2,
    "img_eager": 2,
}
DEFAULTS = {**LH_DEFAULTS, **STATIC_DEFAULTS}
RULES = ("cache-headers-file", "cache-hashed", "cache-html", "font-cdn", "font-format", "font-weights",
         "font-subset", "img-alt", "img-size", "img-format", "img-lazy", "img-px", "img-kb")
# Slowest acceptable lab profile: Lighthouse's default mobile "Slow 4G".
MIN_RTT_MS = 150
MAX_THROUGHPUT_KBPS = 1638.4
MIN_CPU_SLOWDOWN = 4
EXCEPTION_FIELDS = ("reason", "approved_by", "date")


def load(path, today, mode="absolute"):
    """The repository's config, validated: {"limits", "exceptions", "errors",
    "hashed"}. Baseline mode needs a "migration" entry (reason, until): a site
    stays in baseline only with a written plan and a date."""
    limits, exceptions, errors = load_config(path, today)
    cfg = {}
    if path and os.path.exists(path):
        try:
            cfg = json.load(open(path))
        except (OSError, ValueError):
            cfg = {}
    cfg = cfg if isinstance(cfg, dict) else {}
    if mode == "baseline":
        mig = cfg.get("migration")
        if not isinstance(mig, dict) or len(str(mig.get("reason", "")).strip()) < 10 or not mig.get("until"):
            errors.append("baseline mode needs \"migration\": {\"reason\": ..., \"until\": \"YYYY-MM-DD\"} "
                          "in the budget file (when the site will meet the budget)")
        else:
            try:
                until = datetime.date.fromisoformat(str(mig["until"]))
                if until < today:
                    errors.append(f"migration.until {until} has passed: meet the budget and switch to absolute, "
                                  "or set a new date")
            except ValueError:
                errors.append("migration.until must be YYYY-MM-DD")
    hashed = (cfg.get("cache") or {}).get("hashed_extra") or [] if isinstance(cfg.get("cache"), dict) else []
    good = []
    for pat in hashed if isinstance(hashed, list) else [None]:
        try:
            re.compile(pat)
            good.append(pat)
        except (re.error, TypeError):
            errors.append(f"cache.hashed_extra: {pat!r} is not a regular expression")
    return {"limits": limits, "exceptions": exceptions, "errors": errors, "hashed": good}


def load_config(path, today):
    """Returns (limits, valid exceptions, errors)."""
    errors = []
    if not path or not os.path.exists(path):
        return dict(DEFAULTS), [], errors
    try:
        cfg = json.load(open(path))
    except (OSError, ValueError) as e:
        return dict(DEFAULTS), [], [f"{path}: not valid JSON ({e})"]
    if not isinstance(cfg, dict):
        return dict(DEFAULTS), [], [f"{path}: must be a JSON object"]
    unknown = set(cfg) - {"limits", "exceptions", "$schema", "slo", "migration", "cache"}
    if unknown:
        errors.append(f"{path}: unknown keys {sorted(unknown)}")
    limits = dict(DEFAULTS)
    for k, v in (cfg.get("limits") or {}).items():
        if k not in DEFAULTS:
            errors.append(f"limits.{k}: unknown limit (known: {', '.join(DEFAULTS)})")
        elif not isinstance(v, (int, float)) or isinstance(v, bool) or v < 0:
            errors.append(f"limits.{k}: must be a number >= 0")
        elif v > DEFAULTS[k]:
            errors.append(f"limits.{k} = {v} is looser than the kit's {DEFAULTS[k]}: "
                          "only stricter limits go in \"limits\"; a looser one needs an entry in \"exceptions\"")
        else:
            limits[k] = v
    exceptions = []
    for i, e in enumerate(cfg.get("exceptions") or []):
        where = f"exceptions[{i}]"
        if not isinstance(e, dict):
            errors.append(f"{where}: must be an object")
            continue
        missing = [f for f in EXCEPTION_FIELDS if not e.get(f)]
        if ("limit" in e) == ("rule" in e):
            missing.insert(0, "limit or rule (one of them)")
        elif "limit" in e and "value" not in e:
            missing.append("value")
        elif "rule" in e and not e.get("files"):
            missing.append("files (a rule exception names the files it covers)")
        if missing:
            errors.append(f"{where}: missing {', '.join(missing)}")
            continue
        if "rule" in e and e["rule"] not in RULES:
            errors.append(f"{where}: unknown rule {e['rule']} (known: {', '.join(RULES)})")
            continue
        if "limit" in e and e["limit"] not in DEFAULTS:
            errors.append(f"{where}: unknown limit {e['limit']}")
            continue
        if "limit" in e and (not isinstance(e["value"], (int, float)) or isinstance(e["value"], bool) or e["value"] < 0):
            errors.append(f"{where}: value must be a number >= 0")
            continue
        try:
            datetime.date.fromisoformat(str(e["date"]))
            expires = datetime.date.fromisoformat(str(e["expires"])) if e.get("expires") else None
        except ValueError:
            errors.append(f"{where}: date/expires must be YYYY-MM-DD")
            continue
        if expires and expires < today:
            errors.append(f"{where}: expired on {expires} ({e['limit']}); renew it or meet the limit")
            continue
        if len(str(e["reason"]).strip()) < 10:
            errors.append(f"{where}: reason too short to explain the exception")
            continue
        bad_list = False
        for key in ("pages", "files"):
            v = e.get(key)
            if v is not None and (not isinstance(v, list) or not v or not all(isinstance(p, str) for p in v)):
                errors.append(f"{where}: {key} must be a non-empty list of paths")
                bad_list = True
        if bad_list:
            continue
        exceptions.append(e)
    return limits, exceptions, errors


def covers(e, where):
    """Does the exception reach this page (exact path) or file (glob)?"""
    if e.get("pages") is None and e.get("files") is None:
        return True
    return where in (e.get("pages") or []) or any(fnmatch.fnmatch(where, g) for g in e.get("files") or [])


def limit_for(name, where, limits, exceptions):
    """The limit of a metric on a page or file, and the exception that set it (if any)."""
    for e in exceptions:
        if e.get("limit") == name and covers(e, where):
            return max(limits[name], e["value"]), e
    return limits[name], None


def rule_exception(rule, file, exceptions):
    for e in exceptions:
        if e.get("rule") == rule and covers(e, file):
            return e
    return None


def metrics_of(lhr):
    audits = lhr.get("audits", {})

    def value(audit_id):
        v = (audits.get(audit_id) or {}).get("numericValue")
        return None if v is None else float(v)

    items = ((audits.get("network-requests") or {}).get("details") or {}).get("items") or []
    js = sum(i.get("transferSize") or 0 for i in items if i.get("resourceType") == "Script")
    total = value("total-byte-weight")
    if total is None:
        total = sum(i.get("transferSize") or 0 for i in items)
    return {
        "lcp_ms": value("largest-contentful-paint"),
        "cls": value("cumulative-layout-shift"),
        "tbt_ms": value("total-blocking-time"),
        "page_kb": total / 1024,
        "js_kb": js / 1024,
    }


def profile_errors(lhr):
    c = lhr.get("configSettings") or {}
    t = c.get("throttling") or {}
    errs = []
    if c.get("formFactor") != "mobile" or not (c.get("screenEmulation") or {}).get("mobile"):
        errs.append("Lighthouse did not emulate a phone (formFactor mobile)")
    if c.get("throttlingMethod") != "simulate":
        errs.append(f"Lighthouse throttling is {c.get('throttlingMethod')!r}, not 'simulate'")
    if (t.get("rttMs") or 0) < MIN_RTT_MS or (t.get("throughputKbps") or 1e9) > MAX_THROUGHPUT_KBPS \
            or (t.get("cpuSlowdownMultiplier") or 0) < MIN_CPU_SLOWDOWN:
        errs.append("Lighthouse network/CPU profile is faster than Slow 4G on a mid phone")
    return errs


def page_of(lhr):
    url = lhr.get("requestedUrl") or lhr.get("finalUrl", "")
    return re.sub(r"^https?://[^/]+", "", url) or "/"


def check(a):
    today = datetime.date.fromisoformat(a.today) if a.today else datetime.date.today()
    cfg = load(a.config, today, a.mode)
    limits, exceptions, errors = cfg["limits"], cfg["exceptions"], cfg["errors"]
    runs = {}
    for path in sorted(glob.glob(os.path.join(a.lhci or "", "lhr-*.json"))):
        lhr = json.load(open(path))
        for e in profile_errors(lhr):
            if e not in errors:
                errors.append(e)
        if lhr.get("runtimeError"):
            errors.append(f"Lighthouse failed on {page_of(lhr)}: {lhr['runtimeError'].get('code')}")
            continue
        runs.setdefault(page_of(lhr), []).append(metrics_of(lhr))
    wanted = (a.pages or "").split()
    for p in wanted:
        if p not in runs:
            errors.append(f"no Lighthouse result for {p}: the budget cannot be checked")
    if not wanted and not runs:  # nothing measured
        errors.append("no Lighthouse results: the budget needs lighthouse-paths")
    pages = {}
    for page, rs in sorted(runs.items()):
        row = {}
        for name in LH_DEFAULTS:
            vals = [r[name] for r in rs if r[name] is not None]
            if not vals:
                errors.append(f"{page}: Lighthouse gave no {name}")
                continue
            lim, exc = limit_for(name, page, limits, exceptions)
            v = statistics.median(vals)
            row[name] = {"value": round(v, 3 if name == "cls" else 1), "limit": lim,
                         "exception": bool(exc)}
        pages[page] = row
    out = {"mode": a.mode, "pages": pages, "errors": errors,
           "defaults": LH_DEFAULTS, "exceptions": [
               {k: e.get(k) for k in ("limit", "value", "pages", "reason", "approved_by", "date", "expires")}
               for e in exceptions if "limit" in e]}
    print(json.dumps(out, ensure_ascii=False))
    return 0


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check")
    c.add_argument("lhci")
    c.add_argument("--mode", choices=["absolute", "baseline"], required=True)
    c.add_argument("--config", default="web-budget.json")
    c.add_argument("--pages", default="")
    c.add_argument("--today", help="YYYY-MM-DD (tests)")
    a = p.parse_args()
    return check(a)


if __name__ == "__main__":
    sys.exit(main())
