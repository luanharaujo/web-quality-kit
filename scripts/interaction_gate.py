#!/usr/bin/env python3
"""Interaction gate: every route that writes data must be under the checks.

  routes <dir>...                  list the server's write routes (JSON)
  collect --routes F --report D... build the numbers (JSON) from the routes
                                   and the fixture's reports
  compare BASE CURRENT             panel (Markdown) and exit code

The write routes come from the server code (scan below) or from the
repository's own command (action input `routes-command`, one "METHOD /path"
per line). A route is covered when the end-to-end tests went through it
under the checks (A: busy state, double tap, no early success, error and
retry, uploads) and when an idempotency contract test passed for it (B).
Routes with no screen (webhooks) go in interaction.json "no_ui" with
a reason: they still need the contract. "exclude" (with a reason) takes a
route out of both.

Mode "new": anything uncovered or failing is red. Mode "baseline": what
main already has only warns; something new uncovered or failing is red.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

WRITE = ("POST", "PUT", "PATCH", "DELETE")
ID_SEGMENT = re.compile(r"^(\d+|[0-9a-f]{8}-[0-9a-f-]{27,}|[0-9a-f]{16,}|[A-Za-z0-9_-]{24,})$", re.I)
PARAM_SEGMENT = re.compile(r"^(:.*|\{.*\}|\[.*\]|#.*|\*.*|<.*>)$")
CODE_EXT = {".js", ".mjs", ".cjs", ".ts", ".mts", ".cts", ".jsx", ".tsx"}
SKIP_DIRS = {"node_modules", "dist", "build", ".git", ".wrangler", "coverage", "test", "tests", "__tests__", "e2e"}
M = "POST|PUT|PATCH|DELETE"
Q = "['\"`]"

# Shapes the scan knows (see scan()): app.post('/x'), router.delete("/x/:id"),
# route('POST', '/x'), register('PUT', 'videos/#id'), and the two below.
# method === 'POST' && path === '/x'   (and the other order)
RE_IF = re.compile(rf"\bmethod\s*===?\s*(['\"])({M})\1\s*&&\s*[\w.]*(?:path|pathname|url|route)\w*\s*===?\s*({Q})([^'\"`]+)\3", re.I)
RE_IF_REV = re.compile(rf"[\w.]*(?:path|pathname|url|route)\w*\s*===?\s*({Q})([^'\"`]+)\1\s*&&\s*[\w.]*method\s*===?\s*(['\"])({M})\3", re.I)
# a write method compared somewhere: a route the scan could not name
RE_COMPARE = re.compile(rf"===?\s*(['\"])({M})\1|(['\"])({M})\3\s*===?")


def normalize_path(path: str) -> str:
    clean = str(path).split("?", 1)[0]  # "#id" is a route param here, not a fragment
    parts = []
    for seg in clean.split("/"):
        if not seg:
            continue
        parts.append(":id" if ID_SEGMENT.match(seg) or PARAM_SEGMENT.match(seg) else seg)
    return "/" + "/".join(parts)


def route_key(method: str, path: str) -> str:
    return f"{method.upper()} {normalize_path(path)}"


def key_matches(observed: str, declared: str) -> bool:
    om, _, op = observed.partition(" ")
    dm, _, dp = declared.partition(" ")
    if om != dm:
        return False
    o = [s for s in op.split("/") if s]
    d = [s for s in dp.split("/") if s]
    if len(d) > len(o):
        return False
    tail = o[len(o) - len(d):]
    return all(a == ":id" or b == ":id" or a == b for a, b in zip(d, tail))


def code_files(root: Path):
    if root.is_file():
        yield root
        return
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for name in filenames:
            p = Path(dirpath, name)
            if p.suffix in CODE_EXT and not re.search(r"\.(test|spec)\.", name) and not name.endswith(".d.ts"):
                yield p


def pages_route(path: Path) -> str | None:
    parts = path.with_suffix("").parts
    if "functions" not in parts:
        return None
    rel = list(parts[parts.index("functions") + 1:])
    if rel and rel[-1] in ("index", "_middleware"):
        if rel[-1] == "_middleware":
            return None
        rel = rel[:-1]
    return "/" + "/".join(rel)


def scan(dirs: list[str], methods: tuple[str, ...] = WRITE) -> dict:
    """Routes the server code declares. `methods` widens the scan (e.g. GET
    for the query checks); "where" says file:line of each route's first sight."""
    routes: set[str] = set()
    where: dict[str, str] = {}
    unmapped: list[str] = []
    alt = "|".join(methods)
    re_call = re.compile(rf"\.({alt.lower()})\s*\(\s*({Q})(/[^'\"`]*)\2", re.I)
    re_pair = re.compile(rf"(['\"])({alt})\1\s*,\s*({Q})([^'\"`\s]*)\3")
    re_if = re.compile(RE_IF.pattern.replace(M, alt), re.I)
    re_if_rev = re.compile(RE_IF_REV.pattern.replace(M, alt), re.I)
    re_cmp = re.compile(RE_COMPARE.pattern.replace(M, alt))
    re_pages = re.compile(r"export\s+(?:async\s+)?(?:function|const|let)\s+onRequest(" + "|".join(m.capitalize() for m in methods) + r")\b")

    def add(method: str, path: str, f: Path, i: int) -> None:
        key = route_key(method, path)
        routes.add(key)
        where.setdefault(key, f"{f.as_posix()}:{i}")

    for d in dirs:
        root = Path(d)
        if not root.exists():
            continue
        for f in code_files(root):
            try:
                text = f.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            page = pages_route(f)
            for i, line in enumerate(text.splitlines(), 1):
                named = False
                for m in re_call.finditer(line):
                    add(m.group(1), m.group(3), f, i)
                    named = True
                for m in re_pair.finditer(line):
                    add(m.group(2), m.group(4), f, i)
                    named = True
                for m in re_if.finditer(line):
                    add(m.group(2), m.group(4), f, i)
                    named = True
                for m in re_if_rev.finditer(line):
                    add(m.group(4), m.group(2), f, i)
                    named = True
                pm = re_pages.search(line)
                if pm and page is not None:
                    add(pm.group(1), page, f, i)
                    named = True
                if not named and re_cmp.search(line):
                    unmapped.append(f"{f.as_posix()}:{i}")
    return {"routes": sorted(routes), "where": dict(sorted(where.items())), "unmapped": unmapped}


def read_reports(dirs: list[str]) -> list[dict]:
    out = []
    for d in dirs:
        p = Path(d)
        if not p.is_dir():
            continue
        for f in sorted(p.glob("*.jsonl")):
            for line in f.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    out.append(json.loads(line))
    return out


def listed(items: list, key: str) -> bool:
    return any(key_matches(key, it if isinstance(it, str) else it["route"]) for it in items)


def load_config(path: str | None) -> dict:
    cfg = {"no_ui": [], "exclude": []}
    if path and Path(path).is_file():
        cfg.update(json.loads(Path(path).read_text(encoding="utf-8")))
    for field in ("no_ui", "exclude", "optimistic", "duplicate_reads_ok", "no_loading_state"):
        cfg.setdefault(field, [])
        for item in cfg[field]:
            if not isinstance(item, dict) or not item.get("route") or not str(item.get("reason", "")).strip():
                raise SystemExit(f'interaction.json "{field}": each entry needs "route" and a "reason" ({item!r})')
    return cfg


def collect(routes: dict, entries: list[dict], cfg: dict) -> dict:
    seen = {e["route"] for e in entries if e.get("type") == "seen"}
    undeclared = sorted(s for s in seen if not any(key_matches(s, r) for r in routes["routes"]))
    # A write the tests made that the route list lacks is a route too: an
    # incomplete scan must not make the panel look covered.
    declared = [r for r in sorted(set(routes["routes"]) | set(undeclared)) if not listed(cfg["exclude"], r)]
    contracts_ok = {e["route"] for e in entries if e.get("type") == "contract" and e.get("ok")}
    failing = sorted({f'{e["route"]} {e.get("check") or "contract"}' for e in entries
                      if e.get("type") in ("check", "contract") and e.get("ok") is False})
    without_e2e = [r for r in declared if not listed(cfg["no_ui"], r) and not any(key_matches(s, r) for s in seen)]
    without_contract = [r for r in declared if not any(key_matches(c, r) or key_matches(r, c) for c in contracts_ok)]
    covered = [r for r in declared if r not in without_e2e and r not in without_contract]
    return {
        "routes": declared,
        "covered": covered,
        "excluded": [r for r in sorted(set(routes["routes"]) | set(undeclared)) if r not in declared],
        "without_e2e": without_e2e,
        "without_contract": without_contract,
        "failing": failing,
        "unmapped": routes.get("unmapped", []),
        "seen_undeclared": undeclared,
        "where": {r: routes.get("where", {}).get(r, "") for r in routes["routes"]},
    }


TEXT = {
    "title": "Actions that save data",
    "covered": "write routes under the checks (e2e + contract)",
    "without_e2e": "write routes no end-to-end test went through",
    "without_contract": "write routes without an idempotency contract test",
    "failing": "checks failing (busy state, double tap, early success, error and retry, upload, contract)",
    "unmapped": "write handlers the scan could not name (give them a route table or use routes-command)",
    "undeclared": "writes seen in the tests that the route scan missed (counted as routes above)",
    "new": "new",
    "baseline": "No numbers from main: everything is the baseline this time (only warns).",
}


def texts(lang: str) -> dict:
    """English texts; Portuguese from panel_pt.json (same keys as the English)."""
    if lang != "pt":
        return TEXT
    pt = json.loads((Path(__file__).resolve().parent / "panel_pt.json").read_text(encoding="utf-8"))
    return {k: pt.get(v, v) for k, v in TEXT.items()}


def bar(pct: float) -> str:
    full = round(pct / 10)
    return "▓" * full + "░" * (10 - full)


def compare(base: dict | None, cur: dict, mode: str, lang: str) -> tuple[str, bool]:
    t = texts(lang)
    lines = [f"### {t['title']}", ""]
    worse = False
    total = len(cur["routes"])
    pct = 100.0 * len(cur["covered"]) / total if total else 100.0
    pct_txt = f"{pct:.1f}".replace(".", ",") if lang == "pt" else f"{pct:.1f}"
    arrow = "="
    if base is not None:
        btotal = len(base["routes"])
        bpct = 100.0 * len(base["covered"]) / btotal if btotal else 100.0
        arrow = "↑" if pct > bpct + 0.05 else "↓" if pct < bpct - 0.05 else "="
    dot = "🟢" if pct == 100 else "🟡"
    lines.append(f"{dot} {bar(pct)} {pct_txt}% {arrow} · {len(cur['covered'])}/{total} {t['covered']}")
    for field in ("failing", "without_e2e", "without_contract", "unmapped"):
        items = cur[field]
        old = set(base[field]) if base is not None else None
        if mode == "new":
            new_items = items
        elif old is None:
            new_items = []
        else:
            new_items = [i for i in items if i not in old]
        red = bool(new_items)
        worse |= red
        dot = "🔴" if red else ("🟡" if items else "🟢")
        a = "="
        if old is not None:
            a = "↑" if len(items) > len(old) else "↓" if len(items) < len(old) else "="
        lines.append(f"{dot} {len(items)} {t[field]} {a}")
        for i in (new_items if red else items)[:15]:
            at = cur.get("where", {}).get(i, "")
            lines.append(f"  - {'**' + t['new'] + '** ' if red and mode != 'new' else ''}`{i}`" + (f" ({at})" if at else ""))
    if cur.get("seen_undeclared"):
        lines.append(f"ℹ️ {len(cur['seen_undeclared'])} {t['undeclared']}: " + ", ".join(f"`{s}`" for s in cur["seen_undeclared"][:10]))
    if mode != "new" and base is None:
        lines += ["", t["baseline"]]
    return "\n".join(lines) + "\n", worse


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("routes")
    r.add_argument("dirs", nargs="+")
    c = sub.add_parser("collect")
    c.add_argument("--routes", required=True)
    c.add_argument("--report", nargs="+", required=True)
    c.add_argument("--config")
    p = sub.add_parser("compare")
    p.add_argument("base")
    p.add_argument("current")
    p.add_argument("--mode", choices=["new", "baseline"], default="baseline")
    p.add_argument("--lang", default="en")
    p.add_argument("--no-fail", action="store_true")
    a = ap.parse_args(argv)

    if a.cmd == "routes":
        json.dump(scan(a.dirs), sys.stdout, indent=1)
        print()
        return 0
    if a.cmd == "collect":
        text = Path(a.routes).read_text(encoding="utf-8")
        try:
            routes = json.loads(text)
        except json.JSONDecodeError:  # routes-command output: "METHOD /path" lines
            routes = {"routes": [], "unmapped": []}
            for line in text.splitlines():
                parts = line.split()
                if len(parts) >= 2 and parts[0].upper() in WRITE:
                    routes["routes"].append(route_key(parts[0], parts[1]))
            routes["routes"] = sorted(set(routes["routes"]))
        json.dump(collect(routes, read_reports(a.report), load_config(a.config)), sys.stdout, indent=1)
        print()
        return 0
    base = None if a.base == "-" or not Path(a.base).is_file() else json.loads(Path(a.base).read_text(encoding="utf-8"))
    cur = json.loads(Path(a.current).read_text(encoding="utf-8"))
    panel, worse = compare(base, cur, a.mode, a.lang)
    sys.stdout.write(panel)
    return 1 if worse and not a.no_fail else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
