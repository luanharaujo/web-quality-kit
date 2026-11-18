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

Against main, the job fails ("do not get worse") when:
  - a lint, type, HTML or accessibility case is NEW (a case = file + rule +
    the text of its line; cases that already existed only warn);
  - a broken link, a personal-data finding or a high/critical vulnerability
    is new;
  - the bundle grew more than --bundle-growth-pct (default 10%);
  - a test failed or the number of passing tests went down.
Lighthouse scores move a few points between runs on the same code, so they
only warn: 🟡 below 90, or more than 5 points below main.
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
LH_CATEGORIES = [("performance", "Desempenho"), ("accessibility", "Acessibilidade"),
                 ("best-practices", "Boas práticas"), ("seo", "SEO")]


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
            out.append({"file": link.get("parent", ""), "rule": str(link.get("status", "")),
                        "line": 0, "text": link.get("url", "")})
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


def num(x, decimals=0):
    return f"{x:.{decimals}f}".replace(".", ",")


def arrow(diff, decimals=0, higher_is_better=True):
    if round(diff, decimals) == 0:
        return "="
    better = (diff > 0) == higher_is_better
    return f"{'↑' if better else '↓'} {'+' if diff > 0 else '−'}{num(abs(diff), decimals)}"


CASE_METRICS = [("eslint", "Lint (ESLint)"), ("tsc", "Tipos (tsc estrito)"),
                ("html", "HTML válido"), ("links", "Links quebrados"), ("axe", "Acessibilidade (axe)"),
                ("data", "Dado pessoal/segredo no repositório"),
                ("modern", "Web moderna: legado com recurso nativo (só aviso)")]
WARN_ONLY = {"modern"}


def compare(a):
    head = json.load(open(a.head))["metrics"]
    base = None if a.base == "-" else json.load(open(a.base))["metrics"]
    first = base is None
    base = base or {}
    rows, details, failed = [], [], []

    def row(ok, warn, item, now, limit, vs):
        rows.append(f"| {dot(ok, warn)} | {item} | {now} | {limit} | {vs} |")

    if "tests" in head:
        t, bt = head["tests"], base.get("tests")
        good = t["failed"] == 0 and t["exit"] == 0
        ok = first or (good and (not bt or t["passed"] >= bt["passed"]))
        vs = "—" if not bt else arrow(t["passed"] - bt["passed"])
        row(ok, not good, "Testes", f"{t['passed']} passam, {t['failed']} falham", "nenhum falha; não diminuir", vs)
        if not ok:
            failed.append("testes")

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
        extra = f" ({len(new)} novos)" if new else ""
        row(ok, len(cases) > 0, label, f"{len(cases)}{extra}", "nenhum novo" if gate else "só aviso", vs)
        if not ok:
            failed.append(label)
        show = new if new else cases
        if show:
            title = f"{label}: {'novos' if new else 'existentes'} ({len(show)})"
            details += [f"<details><summary>{title}</summary>", "", "| Onde | Regra | Detalhe |", "|---|---|---|"]
            for c in show[:60]:
                where = f"{c['file']}:{c['line']}" if c.get("line") else c["file"]
                text = c.get("masked") if name == "data" else c["text"]
                text = (text or "").replace("|", "\\|")[:90]
                details.append(f"| `{where}` | {c['rule']} | {text} |")
            if len(show) > 60:
                details.append(f"| … | | mais {len(show) - 60} |")
            details += ["", "</details>", ""]

    if "bundle" in head:
        b, bb = head["bundle"], base.get("bundle")
        growth = (b["gzip_kb"] - bb["gzip_kb"]) / bb["gzip_kb"] * 100 if bb and bb["gzip_kb"] else 0
        ok = first or growth <= a.bundle_growth_pct
        over = bool(b.get("max_kb")) and b["gzip_kb"] > b["max_kb"]
        frac = b["gzip_kb"] / b["max_kb"] if b.get("max_kb") else 0
        lim = f"máx {num(b['max_kb'])} kB; crescer até {num(a.bundle_growth_pct)}%" if b.get("max_kb") \
            else f"crescer até {num(a.bundle_growth_pct)}%"
        vs = "—" if not bb else arrow(b["gzip_kb"] - bb["gzip_kb"], 1, higher_is_better=False)
        row(ok, over, "Tamanho do pacote (JS+CSS, gzip)",
            f"{bar(frac) + ' ' if frac else ''}{num(b['gzip_kb'], 1)} kB", lim, vs)
        if not ok:
            failed.append("tamanho do pacote")

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
            row(True, worst < 90 or drop, f"Lighthouse {label} (pior página)",
                f"{bar(worst / 100)} {worst} `{worst_page}`", "mín 90 (só aviso)", vs)

    if "audit" in head:
        v, bv = head["audit"], base.get("audit")
        serious = v["critical"] + v["high"]
        ok = first or not bv or serious <= bv["critical"] + bv["high"]
        vs = "—" if not bv else arrow(serious - bv["critical"] - bv["high"], higher_is_better=False)
        row(ok, sum(v.values()) > 0, "Dependências vulneráveis (npm audit)",
            f"{v['critical']} crít, {v['high']} altas, {v['moderate']} médias, {v['low']} baixas",
            "altas+críticas não subir", vs)
        if not ok:
            failed.append("dependências vulneráveis")
        pk = head.get("audit_packages") or {}
        if pk:
            details += ["<details><summary>Pacotes vulneráveis</summary>", "", "| Pacote | Gravidade |", "|---|---|"]
            details += [f"| `{n}` | {s} |" for n, s in sorted(pk.items(), key=lambda x: SEVERITIES.index(x[1]) if x[1] in SEVERITIES else 9)]
            details += ["", "</details>", ""]

    title = "## Saúde web (linha de base)" if first else "## Saúde web (contra a main)"
    out = [title, "", "| | Item | Agora | Limite | vs main |", "|---|---|---|---|---|", *rows, ""]
    if first:
        out += ["Primeira medida: nada falha; daqui em diante vale \"não piorar\".", ""]
    out += details
    print("\n".join(out))
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
    c.add_argument("--legacy", action="append", help="'dir:file' from modern-web-guidance legacy-scan --json")
    k = sub.add_parser("compare")
    k.add_argument("base")
    k.add_argument("head")
    k.add_argument("--bundle-growth-pct", type=float, default=10.0)
    k.add_argument("--no-fail", action="store_true")
    a = p.parse_args()
    return collect(a) if a.cmd == "collect" else compare(a)


if __name__ == "__main__":
    sys.exit(main())
