#!/usr/bin/env python3
"""Health numbers of a web project: collect them, compare with main.

  web_health.py collect [--eslint eslint.json] [--tsc tsc.txt ...] [--html html.json]
                        [--links links.json] [--bundle DIR --bundle-max-kb N]
                        [--lhci .lighthouseci] [--audit audit.json ...]
                        [--tests tests.txt --tests-exit N] [--data data.json]
                        [--modern modern.json] [--root .] > health.json
  web_health.py compare <base.json|-> <head.json> [--no-fail] > panel.md

Every metric is optional: the panel shows the ones that were collected.
`compare` with base "-" (no numbers from main yet) is the baseline: it shows
what is bad today and never fails.

Against main, the job fails ("never worse") when:
  - a lint, type, HTML or accessibility case is NEW (a case = file + rule +
    the text of its line; cases that already existed only warn);
  - a broken link, a personal-data finding or a high/critical vulnerability
    is new;
  - the bundle grew more than --bundle-growth-pct (default 10%);
  - a test failed or the number of passing tests went down.
Lighthouse scores move a few points between runs on the same code, so they
only warn: 🟡 below 90, or more than 5 points below main.

Performance budget (--budget, from budget.py: lab LCP, CLS, TBT, page and
JS weight per page, phone on a slow network):
  - an invalid web-budget.json (a looser limit with no exception, an expired
    exception) or a page Lighthouse could not measure always fails;
  - mode "absolute": any page over a limit fails, even on the first run and
    with --no-fail (the budget is the rule, not a comparison);
  - mode "baseline" (a site migrating to the budget): a page over a limit
    fails only when it is new or worse than on main beyond the noise margin
    (BUDGET_NOISE); the panel shows how far each page still is from the goal.

Database of a server (--server-queries, from server_queries.py; always a rule,
fails even on the first run and with --no-fail):
  - a list route whose number of queries grows from 3 to 30 rows (N+1);
  - a GET route with no 3-vs-30 test and no exemption (mode "baseline":
    tolerated if listed in the repository's baseline, which may not grow);
  - a full table scan or automatic index in EXPLAIN QUERY PLAN (missing
    index), outside the exempt tables and the baseline (may not grow);
  - a configuration error (counter differs from the kit's, the query header
    switched on in a wrangler config, the server did not answer).
A database query inside a loop (--query-loop) only warns.
"""
import argparse
import glob
import gzip
import json
import os
import re
import statistics
import sys
from collections import Counter

SEVERITIES = ["critical", "high", "moderate", "low"]
LH_CATEGORIES = [("performance", "Performance"), ("accessibility", "Accessibility"),
                 ("best-practices", "Best practices"), ("seo", "SEO")]


def rel(path, root):
    path = os.path.abspath(path) if os.path.isabs(path) else path
    return os.path.relpath(path, root) if os.path.isabs(path) else path


def line_text(path, line):
    try:
        return open(path, errors="replace").read().splitlines()[line - 1].strip()[:200]
    except (OSError, IndexError):
        return ""


# --- collect -----------------------------------------------------------------

def eslint_cases(path, root):
    raw = open(path).read().strip()
    cases = []
    for f in json.loads(raw) if raw else []:
        for m in f["messages"]:
            line = m.get("line") or 0
            rule = m.get("ruleId") or "parse-error"
            text = "" if rule == "max-lines" else line_text(f["filePath"], line)
            cases.append({"file": rel(f["filePath"], root), "rule": rule, "line": line,
                          "text": text, "level": "error" if m["severity"] == 2 else "warning"})
    return cases


TSC_LINE = re.compile(r"^(.+?)\((\d+),\d+\): error (TS\d+): (.*)$")


def tsc_cases(paths, root):
    cases = []
    for path in paths:
        prefix = ""
        if ":" in path and not os.path.exists(path):
            prefix, path = path.split(":", 1)
        for ln in open(path, errors="replace"):
            m = TSC_LINE.match(ln.strip())
            if m:
                file = os.path.join(prefix, m.group(1)) if prefix else m.group(1)
                cases.append({"file": os.path.normpath(file), "rule": m.group(3),
                              "line": int(m.group(2)), "text": m.group(4)[:200]})
            elif re.search(r"error TS\d+", ln):
                cases.append({"file": "", "rule": "TS", "line": 0, "text": ln.strip()[:200]})
    return cases


def html_cases(path, root):
    raw = open(path).read().strip()
    cases = []
    for f in json.loads(raw) if raw else []:
        for m in f["messages"]:
            cases.append({"file": rel(f["filePath"], root), "rule": m.get("ruleId", ""),
                          "line": m.get("line", 0), "text": m.get("message", "")[:200]})
    return cases


def link_cases(path):
    raw = open(path).read().strip()
    data = json.loads(raw) if raw else {}
    out = []
    for link in data.get("links", []):
        if link.get("state") == "BROKEN":
            strip = lambda u: re.sub(r"^https?://(localhost|127\.0\.0\.1)(:\d+)?", "", u or "")
            out.append({"file": strip(link.get("parent", "")), "rule": str(link.get("status", "")),
                        "line": 0, "text": strip(link.get("url", ""))})
    return out


def bundle_size(directory):
    total = gz = 0
    files = []
    for path in glob.glob(os.path.join(directory, "**", "*"), recursive=True):
        if os.path.isfile(path) and path.endswith((".js", ".mjs", ".css")):
            data = open(path, "rb").read()
            total += len(data)
            g = len(gzip.compress(data, 9))
            gz += g
            files.append((g, os.path.relpath(path, directory)))
    files.sort(reverse=True)
    return {"kb": round(total / 1024, 1), "gzip_kb": round(gz / 1024, 1),
            "largest": [{"file": f, "gzip_kb": round(g / 1024, 1)} for g, f in files[:5]]}


def lighthouse(directory, wanted):
    """Median score per category over the runs, per URL, and axe results
    (Lighthouse's accessibility audits are axe-core rules)."""
    runs = {}
    axe = {}
    for path in sorted(glob.glob(os.path.join(directory, "lhr-*.json"))):
        lhr = json.load(open(path))
        url = lhr.get("requestedUrl") or lhr.get("finalUrl", "")
        page = re.sub(r"^https?://[^/]+", "", url) or "/"
        cats = {k: round((v.get("score") or 0) * 100) for k, v in lhr.get("categories", {}).items()}
        runs.setdefault(page, []).append(cats)
        refs = lhr.get("categories", {}).get("accessibility", {}).get("auditRefs", [])
        for ref in refs:
            audit = lhr["audits"].get(ref["id"], {})
            if audit.get("score") == 0 and audit.get("scoreDisplayMode") == "binary":
                items = (audit.get("details") or {}).get("items") or []
                key = (page, ref["id"])
                axe[key] = max(axe.get(key, 0), len(items) or 1)
    pages = {}
    for page, cats in runs.items():
        pages[page] = {k: int(statistics.median(c.get(k, 0) for c in cats)) for k, _ in LH_CATEGORIES
                       if k in wanted}
    cases = [{"file": p, "rule": r, "line": 0, "text": "", "value": n} for (p, r), n in sorted(axe.items())]
    return pages, cases


def legacy_cases(path, prefix):
    """modern-web-guidance legacy-scan --json: places where a native browser
    feature replaces old code (e.g. el.replaceChildren() for innerHTML = "")."""
    raw = open(path).read().strip()
    data = json.loads(raw) if raw else {}
    cases = []
    for guide in data.get("guides", []):
        for rule in guide.get("rules", []):
            for f in rule.get("findings", []):
                cases.append({"file": os.path.normpath(os.path.join(prefix, f.get("file", ""))),
                              "rule": rule.get("ruleId", ""), "line": f.get("line", 0),
                              "text": (f.get("lineContent") or "").strip()[:200]})
    return cases


def audit_counts(paths):
    counts = Counter()
    packages = {}
    for path in paths:
        prefix = ""
        if ":" in path and not os.path.exists(path):
            prefix, path = path.split(":", 1)
        raw = open(path).read().strip()
        data = json.loads(raw) if raw else {}
        vulns = data.get("metadata", {}).get("vulnerabilities", {})
        for sev in SEVERITIES:
            counts[sev] += int(vulns.get(sev, 0))
        for name, v in (data.get("vulnerabilities") or {}).items():
            packages[f"{prefix}:{name}" if prefix else name] = v.get("severity", "")
    return {s: counts[s] for s in SEVERITIES}, packages


def test_counts(path, exit_code, pass_re, fail_re):
    text = open(path, errors="replace").read()
    passed = len(re.findall(pass_re, text, re.M))
    failed = len(re.findall(fail_re, text, re.M))
    return {"passed": passed, "failed": failed, "exit": exit_code}


def collect(a):
    """Every metric the caller collected, in one JSON."""
    root = os.path.abspath(a.root)
    out = {"kit": 1, "metrics": {}}
    m = out["metrics"]
    if a.eslint:
        m["eslint"] = eslint_cases(a.eslint, root)
    if a.tsc:
        m["tsc"] = tsc_cases(a.tsc, root)
    if a.html:
        m["html"] = html_cases(a.html, root)
    if a.links:
        m["links"] = link_cases(a.links)
    if a.bundle:
        m["bundle"] = bundle_size(a.bundle)
        m["bundle"]["max_kb"] = a.bundle_max_kb
    if a.lhci and glob.glob(os.path.join(a.lhci, "lhr-*.json")):
        m["lighthouse"], m["axe"] = lighthouse(a.lhci, a.lh_categories.split())
    if a.audit:
        m["audit"], m["audit_packages"] = audit_counts(a.audit)
    if a.tests:
        m["tests"] = test_counts(a.tests, a.tests_exit, a.pass_regex, a.fail_regex)
    if a.data:
        m["data"] = [{"file": f["file"], "rule": f["kind"], "line": f["line"], "text": f["hash"],
                      "masked": f.get("masked", "")} for f in json.load(open(a.data))]
    if a.modern:
        m["modern"] = eslint_cases(a.modern, root)
    if a.budget:
        m["budget"] = json.load(open(a.budget))
    if a.server_queries:
        m["server_queries"] = json.load(open(a.server_queries))
    if a.query_loop:
        m["query_loop"] = json.load(open(a.query_loop))
    if a.site_rules:
        r = json.load(open(a.site_rules))
        m["site_rules"] = {"mode": r["mode"], "errors": r["errors"], "excepted": len(r.get("excepted", []))}
        for k in SITE_RULES:
            m[k] = r[k]
    for item in a.legacy or []:
        prefix, path = item.split(":", 1) if ":" in item and not os.path.exists(item) else (".", item)
        m.setdefault("modern", []).extend(legacy_cases(path, prefix))
    print(json.dumps(out, ensure_ascii=False))
    return 0


# --- compare -----------------------------------------------------------------

def key(c):
    return (c["file"], c["rule"], c["text"])


def new_cases(base, head):
    """Head cases beyond what main had (same key counted as a multiset)."""
    left = Counter(key(c) for c in base)
    new = []
    for c in head:
        k = key(c)
        if left[k] > 0:
            left[k] -= 1
        else:
            new.append(c)
    return new


def dot(ok, warn=False):
    return "🔴" if not ok else ("🟡" if warn else "🟢")


def bar(fraction, width=10):
    full = max(0, min(width, round(fraction * width)))
    return "`" + "▓" * full + "░" * (width - full) + "`"


# Panel language: English by default; --lang pt reads the Portuguese texts
# from panel_pt.json (same keys as the English texts in this file).
PT = {}
LANG = "en"


def T(text):
    return PT.get(text, text) if LANG == "pt" else text


def num(x, decimals=0):
    text = f"{x:.{decimals}f}"
    return text.replace(".", ",") if LANG == "pt" else text


def arrow(diff, decimals=0, higher_is_better=True):
    if round(diff, decimals) == 0:
        return "="
    better = (diff > 0) == higher_is_better
    return f"{'↑' if better else '↓'} {'+' if diff > 0 else '−'}{num(abs(diff), decimals)}"


CASE_METRICS = [("eslint", "Lint (ESLint)"), ("tsc", "Types (strict tsc)"),
                ("html", "Valid HTML"), ("links", "Broken links"), ("axe", "Accessibility (axe)"),
                ("data", "Personal data or secret in the repository"),
                ("modern", "Modern web: old code with a native replacement (warning only)"),
                ("query_loop", "Database query inside a loop (warning only)"),
                ("cache", "Cache headers"), ("fonts", "Fonts"), ("images", "Images")]
SITE_RULES = ("cache", "fonts", "images")
WARN_ONLY = {"modern", "query_loop"}
SERVER_LABELS = {"config": "Database check: configuration",
                 "n1": "Database queries per list (3 vs 30 rows)",
                 "uncovered": "List routes without the 3-vs-30 test",
                 "scans": "Missing index (full table scan)",
                 "unpaged": "List routes without pagination",
                 "fetch": "External call without a time limit"}


# Lab numbers move a little between runs of the same code; in baseline mode a
# page over its limit fails only when it got worse than main beyond this.
BUDGET_NOISE = {"lcp_ms": (0.10, 100), "tbt_ms": (0.10, 50), "cls": (0, 0.01),
                "page_kb": (0.02, 2), "js_kb": (0.02, 1)}
BUDGET_LABELS = {"lcp_ms": "LCP (lab)", "cls": "CLS (lab)", "tbt_ms": "TBT (lab, stand-in for INP)",
                 "page_kb": "Page weight (transferred)", "js_kb": "JavaScript (transferred)"}


def fmt_budget(name, v):
    if name == "cls":
        return num(v, 3)
    if name.endswith("_ms"):
        return f"{num(v / 1000, 2)} s"
    return f"{num(v, 0)} kB"


def worse(name, now, before):
    rel_tol, abs_tol = BUDGET_NOISE[name]
    return now > before + max(before * rel_tol, abs_tol)


def budget_rows(b, bb, first, row, details):
    """Panel rows of the performance budget. Returns (hard, soft): hard fails
    even with --no-fail (config errors; in absolute mode, any page over a
    limit); soft is baseline mode's "worse than main", which --no-fail turns
    off like the other comparisons."""
    hard, soft = [], []
    absolute = b.get("mode") == "absolute"
    # Main without budget numbers (the PR that turns the budget on) is a
    # baseline for it, like any check main did not have yet.
    first = first or bb is None
    base_pages = (bb or {}).get("pages") or {}
    if b.get("errors"):
        row(False, True, T("Performance budget: configuration"), str(len(b["errors"])), T("none"), "—")
        hard.append("budget configuration")
        details += [f"<details open><summary>{T('Performance budget: configuration')}</summary>", ""]
        details += [f"- {e}" for e in b["errors"]] + ["", "</details>", ""]
    pages = b.get("pages") or {}
    over_any = False
    table = []
    for name, label in BUDGET_LABELS.items():
        vals = {p: r[name] for p, r in pages.items() if name in r}
        if not vals:
            continue
        frac = {p: v["value"] / v["limit"] if v["limit"] else (0 if v["value"] == 0 else 9) for p, v in vals.items()}
        worst_page = max(frac, key=frac.get)
        w = vals[worst_page]
        bad, regressed = [], []
        for p, v in vals.items():
            if v["value"] <= v["limit"]:
                continue
            bad.append(p)
            prev = (base_pages.get(p) or {}).get(name)
            if absolute or (prev is None and not first) or (prev is not None and worse(name, v["value"], prev["value"])):
                regressed.append(p)
        over_any |= bool(bad)
        ok = not regressed
        prevs = [base_pages[p][name]["value"] for p in vals if name in (base_pages.get(p) or {})]
        if not prevs:
            vs = "—"
        elif name == "cls":
            vs = arrow(w["value"] - max(prevs), 3, higher_is_better=False)
        elif name.endswith("_ms"):
            vs = arrow((w["value"] - max(prevs)) / 1000, 2, higher_is_better=False)
        else:
            vs = arrow(w["value"] - max(prevs), 0, higher_is_better=False)
        lim = "≤ " + fmt_budget(name, w["limit"]) + (" " + T("(exception)") if w.get("exception") else "")
        row(ok, bool(bad), T(label) + " " + T("(worst page)"),
            f"{bar(min(frac[worst_page], 1))} {fmt_budget(name, w['value'])} `{worst_page}`", lim, vs)
        if not ok:
            (hard if absolute else soft).append(f"{T(label)}: {', '.join(regressed)}")
    for p, r in pages.items():
        cells = []
        for name in BUDGET_LABELS:
            if name in r:
                v = r[name]
                mark = "🔴" if v["value"] > v["limit"] else "🟢"
                cells.append(f"{mark} {fmt_budget(name, v['value'])} / {fmt_budget(name, v['limit'])}")
            else:
                cells.append("—")
        table.append(f"| `{p}` | " + " | ".join(cells) + " |")
    if table:
        mode = T("absolute") if absolute else T("baseline (migrating)")
        details += [f"<details><summary>{T('Performance budget per page')}: {mode}</summary>", "",
                    "| " + T("Page") + " | " + " | ".join(T(x) for x in BUDGET_LABELS.values()) + " |",
                    "|---" * (len(BUDGET_LABELS) + 1) + "|", *table, ""]
        if not absolute and not over_any and pages:
            details.append(T("Every page meets the budget: switch budget-mode to absolute."))
            details.append("")
        details += [T("Lab numbers: phone on a slow network (Lighthouse mobile, Slow 4G, 4x CPU), median of the runs."),
                    "", "</details>", ""]
    return hard, soft


def server_rows(s, bs, row, details):
    """Panel rows of the database checks. Returns the failures (all hard)."""
    hard = []
    bs = bs or {}
    if s["errors"]:
        row(False, True, T(SERVER_LABELS["config"]), str(len(s["errors"])), T("none"), "—")
        hard.append("database check configuration")
        details += [f"<details open><summary>{T(SERVER_LABELS['config'])}</summary>", ""]
        details += [f"- {e}" for e in s["errors"]] + ["", "</details>", ""]
    if s["routes"]:
        bad = [r for r in s["routes"] if not r["ok"]]
        worst = max(s["routes"], key=lambda r: r["large"] - r["small"])
        now = f"{len(s['routes']) - len(bad)}/{len(s['routes'])} " + T("constant")
        if bad:
            now += f"; `{worst['route']}` {worst['small']}→{worst['large']}"
        row(not bad, False, T(SERVER_LABELS["n1"]), now, T("same number of queries"), "—")
        if bad:
            hard.append("N+1 queries")
        details += [f"<details{' open' if bad else ''}><summary>{T(SERVER_LABELS['n1'])}</summary>", "",
                    T("| Route | 3 rows | 30 rows |"), "|---|---|---|"]
        details += [f"| {'🔴' if not r['ok'] else '🟢'} `{r['route']}` | {r['small']} | {r['large']} |" for r in s["routes"]]
        details += ["", "</details>", ""]

    def ident(item):
        if "file" in item:
            return (item["file"], item["text"])
        return item.get("route") or (item.get("table"), item.get("sql"))

    def measured_on_main(name):
        # A check main never ran (no migrations or source then) has no numbers
        # to compare: its first run is a first measure, not a growing baseline.
        kind = name.replace("baseline_", "")
        m = bs.get("measured")
        if m is not None:
            return bool(m.get(kind))
        # Main from before "measured" (kit 1.9/1.10): it ran the route counts,
        # and the index check when it had migrations; pagination and fetch
        # did not exist yet, so their first run is a first measure.
        if kind == "scans":
            return bool(bs.get("indexes"))
        return kind == "routes"

    def tolerated(name, label, items, base_items, fmt):
        # A baseline item main did not have = the baseline grew. Counted per
        # item, so swapping one item for another, or a second identical one,
        # counts too, not only the size.
        compare = bool(bs) and measured_on_main(name)
        before = Counter(ident(i) for i in bs.get(name, []))
        if name == "baseline_unpaged":
            # A route main already had in its baseline without the 3-vs-30 test
            # that now gets the test is an old route, not a new unpaged one.
            before.update(ident(i) for i in bs.get("baseline_routes", []))
        grew = False
        for i in base_items:
            if before[ident(i)] > 0:
                before[ident(i)] -= 1
            else:
                grew = compare
        vs = "—" if not compare else arrow(len(base_items) - len(bs.get(name, [])), higher_is_better=False)
        ok = not items and not grew
        now = str(len(items))
        if base_items:
            now += " (" + T("{n} in the baseline").format(n=len(base_items)) + ")"
        row(ok, bool(base_items), T(label), now, T("none; baseline never grows"), vs)
        if not ok:
            hard.append(label)
        if items or base_items:
            details.extend([f"<details{' open' if items else ''}><summary>{T(label)}</summary>", ""])
            details.extend(f"- 🔴 {fmt(i)}" for i in items)
            details.extend(f"- 🟡 {fmt(i)} ({T('baseline')})" for i in base_items[:40])
            details.extend(["", "</details>", ""])

    tolerated("baseline_routes", SERVER_LABELS["uncovered"], s["uncovered"], s["baseline_routes"],
              lambda r: f"`{r['route']}`" + (f" {r['where']}" if r.get("where") else ""))
    if s.get("indexes"):
        tolerated("baseline_scans", SERVER_LABELS["scans"], s["scans"], s["baseline_scans"],
                  lambda x: f"`{x['table']}`: {x['plan']} · `{x['sql'][:120]}`")
    if "unpaged" in s and s["routes"]:
        tolerated("baseline_unpaged", SERVER_LABELS["unpaged"], s["unpaged"], s["baseline_unpaged"],
                  lambda r: f"`{r['route']}`: {r.get('problem', '')}")
    if s.get("fetch") or s.get("baseline_fetch"):
        tolerated("baseline_fetch", SERVER_LABELS["fetch"], s["fetch"], s["baseline_fetch"],
                  lambda c: f"`{c['file']}:{c['line']}` {c['rule']} · `{c['text'][:100]}`")
    # A part main measured that this PR no longer runs (source or migrations
    # removed from the config) would just drop out of the panel: red instead.
    if bs and not s.get("errors"):
        mine = s.get("measured") or {}
        for kind in ("scans", "unpaged", "fetch"):
            if measured_on_main("baseline_" + kind) and not mine.get(kind, True):
                row(False, False, T(SERVER_LABELS[kind]), T("not measured in this PR"), T("main measured it"), "—")
                hard.append(SERVER_LABELS[kind])
    return hard


def compare(a):
    global LANG, PT
    LANG = a.lang
    if LANG == "pt":
        PT = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "panel_pt.json")))
    head = json.load(open(a.head))["metrics"]
    base = None if a.base == "-" else json.load(open(a.base))["metrics"]
    first = base is None
    base = base or {}
    rows, details, failed, hard_failed = [], [], [], []

    def row(ok, warn, item, now, limit, vs):
        rows.append(f"| {dot(ok, warn)} | {item} | {now} | {limit} | {vs} |")

    if "tests" in head:
        t, bt = head["tests"], base.get("tests")
        good = t["failed"] == 0 and t["exit"] == 0
        ok = first or (good and (not bt or t["passed"] >= bt["passed"]))
        vs = "—" if not bt else arrow(t["passed"] - bt["passed"])
        row(ok, not good, T("Tests"), T("{p} pass, {f} fail").format(p=t["passed"], f=t["failed"]),
            T("none fails; never fewer"), vs)
        if not ok:
            failed.append("tests")

    for name, label in CASE_METRICS:
        if name not in head:
            continue
        cases = head[name]
        known = name in base
        new = [] if first or not known else new_cases(base[name], cases)
        old = len(cases) - len(new)
        gate = name not in WARN_ONLY
        ok = not (new and gate)
        vs = "—" if first or not known else arrow(len(cases) - len(base[name]), higher_is_better=False)
        extra = " (" + T("{n} new").format(n=len(new)) + ")" if new else ""
        row(ok, len(cases) > 0, T(label), f"{len(cases)}{extra}", T("none new") if gate else T("warning only"), vs)
        if not ok:
            failed.append(label)
        show = new if new else cases
        if show:
            title = f"{T(label)}: {T('new') if new else T('existing')} ({len(show)})"
            details += [f"<details><summary>{title}</summary>", "", T("| Where | Rule | Detail |"), "|---|---|---|"]
            for c in show[:60]:
                where = f"{c['file']}:{c['line']}" if c.get("line") else c["file"]
                text = c.get("masked") if name == "data" else c["text"]
                if c.get("detail"):
                    text = f"{text} ({c['detail']})" if text else c["detail"]
                text = (text or "").replace("|", "\\|")[:90]
                details.append(f"| `{where}` | {c['rule']} | {text} |")
            if len(show) > 60:
                details.append("| … | | " + T("{n} more").format(n=len(show) - 60) + " |")
            details += ["", "</details>", ""]

    if "bundle" in head:
        b, bb = head["bundle"], base.get("bundle")
        growth = (b["gzip_kb"] - bb["gzip_kb"]) / bb["gzip_kb"] * 100 if bb and bb["gzip_kb"] else 0
        ok = first or growth <= a.bundle_growth_pct
        over = bool(b.get("max_kb")) and b["gzip_kb"] > b["max_kb"]
        frac = b["gzip_kb"] / b["max_kb"] if b.get("max_kb") else 0
        grow = T("grow up to {g}%").format(g=num(a.bundle_growth_pct))
        lim = T("max {m} kB").format(m=num(b["max_kb"])) + "; " + grow if b.get("max_kb") else grow
        vs = "—" if not bb else arrow(b["gzip_kb"] - bb["gzip_kb"], 1, higher_is_better=False)
        row(ok, over, T("Bundle size (JS+CSS, gzip)"),
            f"{bar(frac) + ' ' if frac else ''}{num(b['gzip_kb'], 1)} kB", lim, vs)
        if not ok:
            failed.append("bundle size")

    if "lighthouse" in head:
        bl = base.get("lighthouse", {})
        for cat, label in LH_CATEGORIES:
            scores = {p: s[cat] for p, s in head["lighthouse"].items() if cat in s}
            if not scores:
                continue
            worst_page = min(scores, key=scores.get)
            worst = scores[worst_page]
            prev = [bl[p][cat] for p in scores if cat in bl.get(p, {})]
            drop = any(bl[p][cat] - scores[p] > 5 for p in scores if cat in bl.get(p, {}))
            vs = "—" if not prev else arrow(worst - min(prev))
            row(True, worst < 90 or drop, "Lighthouse " + T(label) + " " + T("(worst page)"),
                f"{bar(worst / 100)} {worst} `{worst_page}`", T("min 90 (warning only)"), vs)

    if "site_rules" in head:
        sr = head["site_rules"]
        if sr.get("errors"):
            row(False, True, T("Site rules: configuration"), str(len(sr["errors"])), T("none"), "—")
            hard_failed.append("site rules configuration")
            failed.append("site rules configuration")
            details += [f"<details open><summary>{T('Site rules: configuration')}</summary>", ""]
            details += [f"- {e}" for e in sr["errors"]] + ["", "</details>", ""]
        if sr.get("mode") == "absolute":
            for k in SITE_RULES:
                if head.get(k):
                    label = dict(CASE_METRICS)[k]
                    hard_failed.append(label)
                    if label not in failed:
                        failed.append(label)
                    # Absolute: any case fails, so the row is red and the limit is "none".
                    rows[:] = [r.replace("| 🟡 |", "| 🔴 |", 1).replace(f"| {T('none new')} |", f"| {T('none')} |")
                               if f"| {T(label)} |" in r else r for r in rows]
        if sr.get("excepted"):
            details += [T("Site rules: {n} cases covered by registered exceptions (web-budget.json).").format(
                n=sr["excepted"]), ""]

    if "budget" in head:
        hard, soft = budget_rows(head["budget"], base.get("budget"), first, row, details)
        failed += hard + soft
        hard_failed += hard

    db_hard = []
    if "server_queries" in head:
        hard = db_hard = server_rows(head["server_queries"], base.get("server_queries"), row, details)
        failed += hard
        hard_failed += hard

    if "audit" in head:
        v, bv = head["audit"], base.get("audit")
        serious = v["critical"] + v["high"]
        ok = first or not bv or serious <= bv["critical"] + bv["high"]
        vs = "—" if not bv else arrow(serious - bv["critical"] - bv["high"], higher_is_better=False)
        row(ok, sum(v.values()) > 0, T("Vulnerable dependencies (npm audit)"),
            T("{c} critical, {h} high, {m} moderate, {l} low").format(
                c=v["critical"], h=v["high"], m=v["moderate"], l=v["low"]),
            T("high+critical never more"), vs)
        if not ok:
            failed.append("vulnerable dependencies")
        pk = head.get("audit_packages") or {}
        if pk:
            details += [f"<details><summary>{T('Vulnerable packages')}</summary>", "", T("| Package | Severity |"), "|---|---|"]
            details += [f"| `{n}` | {s} |" for n, s in sorted(pk.items(), key=lambda x: SEVERITIES.index(x[1]) if x[1] in SEVERITIES else 9)]
            details += ["", "</details>", ""]

    title = T("## Web health (baseline)") if first else T("## Web health (against main)")
    out = [title, "", T("| | Item | Now | Limit | vs main |"), "|---|---|---|---|---|", *rows, ""]
    if first and hard_failed:
        out += [T("First measure: only the rules in 🔴 fail; the rest is \"never worse\" from now on."), ""]
    elif first:
        out += [T("First measure: nothing fails; from now on, \"never worse\"."), ""]
    out += details
    print("\n".join(out))
    if hard_failed and a.no_fail:
        site = {dict(CASE_METRICS)[k] for k in SITE_RULES} | {"site rules configuration"}
        groups = [("Performance budget", [x for x in hard_failed if x not in site and x not in db_hard]),
                  ("Rule broken", [x for x in hard_failed if x in db_hard]),
                  ("Site rules", [x for x in hard_failed if x in site])]
        for name, broken in groups:
            if broken:
                print(f"::error::{name}: {', '.join(broken)}", file=sys.stderr)
        return 1
    if failed and not a.no_fail:
        print(f"::error::Health got worse than main: {', '.join(failed)}", file=sys.stderr)
        return 1
    return 0


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("collect")
    c.add_argument("--root", default=".")
    c.add_argument("--eslint")
    c.add_argument("--tsc", action="append", help="tsc output; 'dir:file' prefixes paths with dir")
    c.add_argument("--html")
    c.add_argument("--links")
    c.add_argument("--bundle")
    c.add_argument("--bundle-max-kb", type=float, default=0)
    c.add_argument("--lhci")
    c.add_argument("--lh-categories", default="performance accessibility best-practices seo")
    c.add_argument("--audit", action="append", help="npm audit --json; 'dir:file' labels packages")
    c.add_argument("--tests")
    c.add_argument("--tests-exit", type=int, default=0)
    c.add_argument("--pass-regex", default=r"^\s*(ok|✓|✔|PASS)\b")
    c.add_argument("--fail-regex", default=r"^\s*(FALHOU|not ok|✗|✘|FAIL)\b")
    c.add_argument("--data")
    c.add_argument("--modern", help="ESLint JSON of the Baseline CSS check")
    c.add_argument("--budget", help="budget.py check output")
    c.add_argument("--server-queries", help="server_queries.py check output")
    c.add_argument("--query-loop", help="tools/query_in_loop.mjs output (cases)")
    c.add_argument("--site-rules", help="site_rules.py check output")
    c.add_argument("--legacy", action="append", help="'dir:file' from modern-web-guidance legacy-scan --json")
    k = sub.add_parser("compare")
    k.add_argument("base")
    k.add_argument("head")
    k.add_argument("--bundle-growth-pct", type=float, default=10.0)
    k.add_argument("--no-fail", action="store_true")
    k.add_argument("--lang", choices=["en", "pt"], default="en", help="panel language")
    a = p.parse_args()
    return collect(a) if a.cmd == "collect" else compare(a)


if __name__ == "__main__":
    sys.exit(main())
