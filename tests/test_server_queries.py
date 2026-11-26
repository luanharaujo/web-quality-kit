#!/usr/bin/env python3
"""Proof of the database checks: an example server with an N+1, a list route
without the test and a missing index is caught; the good one passes; the
query headers never leave localhost; the baseline only shrinks."""
import copy
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
S = os.path.join(ROOT, "scripts")
CASES = os.path.join(ROOT, "tests", "cases", "server")
sys.path.insert(0, S)
import server_queries as sq  # noqa: E402

fails = 0


def check(name, cond, detail=""):
    global fails
    print(("ok   " if cond else "FAIL ") + name + ("" if cond else f"  {detail}"))
    fails += 0 if cond else 1


def cfg(variant, **over):
    c = json.load(open(os.path.join(CASES, f"server-queries.{variant}.json")))
    c.update(over)
    return c


def panel(result, base=None, *flags):
    d = tempfile.mkdtemp()
    head, b = os.path.join(d, "h.json"), os.path.join(d, "b.json")
    json.dump({"kit": 1, "metrics": {"server_queries": result}}, open(head, "w"))
    if base is not None:
        json.dump({"kit": 1, "metrics": {"server_queries": base}}, open(b, "w"))
    p = subprocess.run([sys.executable, os.path.join(S, "web_health.py"), "compare", b if base is not None else "-",
                        head, *flags], capture_output=True, text=True)
    return p.returncode, p.stdout


# --- the good server passes, the bad one is caught -----------------------------
good = sq.check(cfg("good"), ROOT)
check("good: no errors", good["errors"] == [], good["errors"])
check("good: both lists constant", [r["ok"] for r in good["routes"]] == [True, True], good["routes"])
check("good: JOIN list is 1 query, IN list is 2", [r["small"] for r in good["routes"]] == [1, 2], good["routes"])
check("good: every GET route tested or exempt", good["uncovered"] == [], good["uncovered"])
check("good: no missing index", good["scans"] == [], good["scans"])
rc, out = panel(good)
check("good: panel green, exit 0", rc == 0 and "🔴" not in out, out)

bad = sq.check(cfg("bad"), ROOT)
orders = next(r for r in bad["routes"] if r["route"] == "GET /orders")
check("bad: N+1 in a for loop caught (3 → 30 rows)", not orders["ok"] and orders["large"] - orders["small"] == 27, orders)
cust = next(r for r in bad["routes"] if r["route"] == "GET /customers")
check("bad: a batch built per row is still N+1", not cust["ok"], cust)
check("bad: list route without the test flagged", [u["route"] for u in bad["uncovered"]] == ["GET /products"], bad["uncovered"])
check("bad: missing index on orders.customer_id", [(s["table"], s["plan"]) for s in bad["scans"]] == [("orders", "SCAN orders")], bad["scans"])
rc, out = panel(bad)
check("bad: red on the first run (a rule, not a comparison)", rc == 1 and out.count("🔴") >= 3, out)
rc, _ = panel(bad, None, "--no-fail")
check("bad: red even with --no-fail", rc == 1)
rc, out = panel(bad, None, "--lang", "pt")
PT = json.load(open(os.path.join(S, "panel_pt.json")))
check("bad: panel in Portuguese", PT["Database queries per list (3 vs 30 rows)"] in out, out)

# --- baseline mode: tolerated today, never grows -------------------------------
base_cfg = cfg("bad", mode="baseline", baseline={"routes": ["GET /products"],
               "scans": [{"table": "orders", "sql": "SELECT total FROM orders WHERE customer_id = ?"}]},
               exempt={"GET /health": "no list"})
old = sq.check(base_cfg, ROOT)
check("baseline: listed route tolerated", old["uncovered"] == [] and len(old["baseline_routes"]) == 1, old)
check("baseline: listed scan tolerated", old["scans"] == [] and len(old["baseline_scans"]) == 1, old)
fixed = copy.deepcopy(old)
fixed["routes"] = [dict(r, ok=True, large=r["small"]) for r in fixed["routes"]]
fixed.update(unpaged=[], fetch=[])  # this part only looks at the routes and scans baselines
rc, out = panel(fixed, fixed)
check("baseline: same baseline as main passes (🟡)", rc == 0 and "🟡" in out, out)
smaller = dict(fixed, baseline_routes=[])
rc, _ = panel(fixed, smaller)
check("baseline: a route added to the baseline against main is red", rc == 1)
rc, _ = panel(smaller, fixed)
check("baseline: shrinking the baseline passes", rc == 0)
swapped = dict(fixed, baseline_routes=[{"route": "GET /other", "where": ""}])
rc, _ = panel(swapped, fixed)
check("baseline: swapping a baseline route for another (same size) is red", rc == 1)
check("baseline: a tested route that grows is red anyway", panel(old, old)[0] == 1)

# --- the interaction scanner (scripts/interaction_gate.py, once it is in the kit) ---
if os.path.exists(os.path.join(S, "interaction_gate.py")):
    c = cfg("bad")
    del c["routes_command"]
    sc = sq.check(c, ROOT)
    check("scanner: untested GET route found with its file:line",
          [u["route"] for u in sc["uncovered"]] == ["GET /products"] and "handlers_bad.mjs" in sc["uncovered"][0]["where"], sc["uncovered"])

# --- pagination and external calls (1.11.0) -------------------------------------
check("good: lists paginate (limit, next cursor, cap)", good["unpaged"] == [], good["unpaged"])
check("good: external calls all have a time limit or stay internal", good["fetch"] == [], good["fetch"])
check("bad: lists without pagination caught", sorted(u["route"] for u in bad["unpaged"]) == ["GET /customers", "GET /orders"], bad["unpaged"])
rules = [(c["line"], c["rule"]) for c in bad["fetch"]]
check("bad: fetch without signal, URL in a variable, globalThis.fetch and a too-long timeout caught",
      rules[:5] == [(3, "fetch-no-timeout"), (4, "fetch-no-timeout"), (5, "fetch-no-timeout"), (6, "fetch-timeout-too-long"), (7, "fetch-no-timeout")], rules)
check("bad: a URL built from a variable (`${API}/x`) and new URL('/x', external base) are external",
      (8, "fetch-no-timeout") in rules and (9, "fetch-no-timeout") in rules, rules)
loose = sq.check(cfg("bad", fetch_timeout={"max_ms": 60000}), ROOT)
check("fetch: max_ms is configurable", [c["line"] for c in loose["fetch"] if c["rule"] == "fetch-timeout-too-long"] == [], loose["fetch"])
rc, out = panel(bad, None, "--lang", "pt")
PT = json.load(open(os.path.join(S, "panel_pt.json")))
check("bad: panel shows pagination and time limit rows",
      PT["List routes without pagination"] in out and PT["External call without a time limit"] in out, out)
fb = cfg("bad", mode="baseline", baseline={"unpaged": ["GET /orders", "GET /customers"],
         "fetch": [{"file": c["file"], "text": c["text"]} for c in bad["fetch"]]})
fbr = sq.check(fb, ROOT)
check("baseline: unpaged routes and old fetch calls tolerated", fbr["unpaged"] == [] and fbr["fetch"] == []
      and len(fbr["baseline_unpaged"]) == 2 and len(fbr["baseline_fetch"]) == 8, fbr)
one = [{"file": c["file"], "text": c["text"]} for c in bad["fetch"] if c["line"] != 10]
dup = sq.check(dict(fb, baseline=dict(fb["baseline"], fetch=one)), ROOT)
check("baseline: a second identical fetch in the same file is a new case", [c["line"] for c in dup["fetch"]] == [10], dup["fetch"])
twice = dict(fbr, baseline_fetch=fbr["baseline_fetch"] + [fbr["baseline_fetch"][0]])
check("baseline: one more identical entry than main is red", panel(twice, fbr)[0] == 1)
grown = dict(fbr, baseline_fetch=fbr["baseline_fetch"] + [{"file": "x.ts", "line": 1, "rule": "fetch-no-timeout", "text": "fetch(u)"}])
check("baseline: a fetch added to the baseline against main is red", panel(grown, fbr)[0] == 1)

# --- cache, missing source, server that never gets ready, check new on main ---
cached = cfg("bad", routes=[{"route": "GET /orders"}])
cached["server"] = dict(cached["server"], start=cached["server"]["start"].replace(" bad ", " cached "))
cr = sq.check(cached, ROOT)
check("a response cache cannot hide an N+1 (the larger of the two calls counts)", [r["ok"] for r in cr["routes"]] == [False], cr["routes"])
miss = sq.check(cfg("good", source=["tests/cases/server/handlers_good.mjs", "tests/cases/server/nope"]), ROOT)
check("a source folder that does not exist is a configuration error", any("nope does not exist" in e for e in miss["errors"]), miss["errors"])
hang = cfg("good", server={"start": "sleep 31.4", "url": "http://127.0.0.1:{port}", "ready": "/health", "timeout": 2})
hr = sq.check(hang, ROOT)
alive = subprocess.run(["pgrep", "-f", "sleep 31.4"], capture_output=True).returncode == 0
check("a server that never gets ready is an error and is not left running", any("did not answer" in e for e in hr["errors"]) and not alive, hr["errors"])
check("a server that failed leaves pagination and indexes unmeasured", hr["measured"]["unpaged"] is False and hr["measured"]["scans"] is False, hr["measured"])
new_scan = dict(fixed, measured={"routes": True, "scans": True, "unpaged": False, "fetch": False})
main_without = dict(new_scan, baseline_scans=[], measured={"routes": True, "scans": False, "unpaged": False, "fetch": False})
check("a check main never ran is a first measure, not a growing baseline", panel(new_scan, main_without)[0] == 0)
main_with = dict(main_without, measured=dict(main_without["measured"], scans=True))
check("the same baseline grown on a main that ran the check is red", panel(new_scan, main_with)[0] == 1)
old_main = {k: v for k, v in fbr.items() if k not in ("measured", "unpaged", "baseline_unpaged", "fetch", "baseline_fetch")}
off = dict(fbr, routes=[], uncovered=[], scans=[], measured=dict(fbr["measured"], fetch=False), fetch=[], baseline_fetch=[])
rc_off, out_off = panel(off, dict(fbr, routes=[], uncovered=[], scans=[]))
check("a PR that stops a part main measured (source removed) is red", rc_off == 1 and "not measured in this PR" in out_off, out_off)
newly = dict(fbr, routes=[], uncovered=[], scans=[], baseline_routes=[])
main_untested = dict(fbr, routes=[], uncovered=[], scans=[], baseline_unpaged=[r for r in fbr["baseline_unpaged"] if r["route"] != "GET /orders"],
                     baseline_routes=[{"route": "GET /orders", "where": ""}])
check("an old route that gets its first test may enter baseline.unpaged", panel(newly, main_untested)[0] == 0, panel(newly, main_untested)[1])
old_out = panel(fbr, old_main)[1]
new_rows = [l for l in old_out.splitlines() if l.startswith("| ") and ("without pagination" in l or "without a time limit" in l)]
check("a main from kit 1.9 (no pagination or fetch numbers) does not block the update in baseline mode",
      len(new_rows) == 2 and all(l.startswith("| 🟡") for l in new_rows), old_out)
same_old = dict(old_main, measured=None)
same_old.pop("measured")
check("same result against an old main with the same red items fails only for them, not for the new baselines",
      panel(dict(fbr, routes=[], uncovered=[], scans=[]), dict(same_old, routes=[], uncovered=[], scans=[]))[0] == 0,
      panel(dict(fbr, routes=[], uncovered=[], scans=[]), dict(same_old, routes=[], uncovered=[], scans=[]))[1])

# --- pagination: a limit far above the cap, lists that do not page by nature -----
def uncapped(**over):
    c = cfg("good", **{"routes": [{"route": "GET /orders"}, {"route": "GET /totals"}, {"route": "GET /strict"}], **over})
    c["server"] = dict(c["server"], start=c["server"]["start"].replace(" good ", " uncapped "))
    return c


ur = sq.check(uncapped(), ROOT)
probs = {u["route"]: u["problem"] for u in ur["unpaged"]}
check("pagination: a limit far above the cap that returns every row is caught",
      "limit=1000" in probs.get("GET /orders", ""), ur["unpaged"])
check("pagination: a grouped summary without an exemption is caught", "GET /totals" in probs, ur["unpaged"])
check("pagination: refusing a limit above the cap with 400 passes", "GET /strict" not in probs, ur["unpaged"])
exact = lambda **p: sq.check(cfg("good", routes=[{"route": "GET /exact"}], pagination=p,
                                 server=dict(cfg("good")["server"], start=cfg("good")["server"]["start"].replace(" good ", " uncapped "))), ROOT)
ex1 = exact()
check("pagination: the cache-buster parameter is visible to an API that refuses unknown parameters",
      [u["route"] for u in ex1["unpaged"]] == ["GET /exact"], ex1["unpaged"])
ex2 = exact(cache_buster="")
check("pagination: cache_buster \"\" turns it off for such an API", ex2["unpaged"] == [], ex2["unpaged"])
ue = sq.check(uncapped(pagination={"exempt": {"GET /totals": "one row per status, a handful at most"}}), ROOT)
check("pagination: an exempt route with a reason passes in absolute mode",
      [u["route"] for u in ue["unpaged"]] == ["GET /orders"] and ue["mode"] == "absolute", ue["unpaged"])
un = sq.check(uncapped(pagination={"exempt": {"GET /totals": ""}}), ROOT)
check("pagination: an exemption without a reason is an error", any("without a reason: GET /totals" in e for e in un["errors"]), un["errors"])
check("page_of: named list wins over an earlier one", sq.page_of('{"errors": [], "items": [1, 2]}') == ([1, 2], None))
check("page_of: two unnamed lists are ambiguous", sq.page_of('{"a": [1], "b": [2]}')[0] is None)
check("page_of: items_field pointing at a number is no list", sq.page_of('{"items": 3}', "items")[0] is None)

# --- fetch(new Request(...)) and a forwarded incoming request -------------------
rq, _ = sq.fetch_cases(cfg("good", source=["tests/cases/server/external_request.mjs"]), ROOT)
check("fetch: new Request with a relative URL or a signal passes; without a signal it is caught",
      [(c["line"], c["rule"]) for c in rq][:2] == [(5, "fetch-no-timeout"), (9, "fetch-no-timeout")], rq)
check("fetch: a one-parameter fetch(url) wrapper is not a Worker handler", (10, "fetch-no-timeout") in [(c["line"], c["rule"]) for c in rq], rq)
check("fetch: an explicit signal after a spread is the one checked", (11, "fetch-timeout-too-long") in [(c["line"], c["rule"]) for c in rq]
      and 12 not in [c["line"] for c in rq], rq)
of, _ = sq.fetch_cases(cfg("good", source=["tests/cases/server/obs_fetch.ts"]), ROOT)
check("fetch: obs.fetch (the request-id helper) is a fetch, by name or as withObservability's parameter",
      [(c["line"], c["rule"]) for c in of] == [(5, "fetch-no-timeout"), (12, "fetch-no-timeout")], of)
hd = tempfile.mkdtemp(dir=ROOT)
for n in ("observe.ts", "d1_queries.ts"):
    shutil.copy(os.path.join(ROOT, "templates", "server", n), hd)
hc, _ = sq.fetch_cases(cfg("good", source=[os.path.relpath(hd, ROOT)]), ROOT)
with open(os.path.join(hd, "observe.ts"), "a") as f:
    f.write("\nexport const ping = () => fetch('https://x.example/')\n")
hx, _ = sq.fetch_cases(cfg("good", source=[os.path.relpath(hd, ROOT)]), ROOT)
shutil.rmtree(hd)
check("fetch: an unchanged copy of the kit's helper is skipped; an edited one is not", hc == [] and any("x.example" in c["text"] for c in hx), (hc, hx))
wk, _ = sq.fetch_cases(cfg("good", source=["tests/cases/server/worker_satisfies.ts", "tests/cases/server/worker_exported_var.ts"]), ROOT)
check("fetch: a Worker exported with `satisfies` or through a variable may forward its request",
      [(c["file"].rsplit("/", 1)[-1], c["line"]) for c in wk] == [("worker_exported_var.ts", 5)], wk)
check("fetch: signal: undefined sets no time limit", any(c["line"] == 5 and c["rule"] == "fetch-no-timeout" for c in wk), wk)
rl = [(c["line"], c["rule"]) for c in rq]
check("fetch: a protocol-relative URL (//host) is external", (13, "fetch-no-timeout") in rl, rq)
check("fetch: a timeout from a const or a simple expression is checked against the max",
      (15, "fetch-timeout-too-long") in rl and (16, "fetch-timeout-too-long") in rl and 17 not in [c["line"] for c in rq], rq)
check("fetch: a const resolves by scope; a parameter with the same name is not the const",
      (18, "fetch-timeout-too-long") in rl and not {19, 21} & {c["line"] for c in rq}, rq)
check("fetch: a const Request with a relative URL and a signal passes", 22 not in [c["line"] for c in rq], rq)
check("fetch: a Workers runtime class (WorkerEntrypoint, DurableObject) may forward its request", 23 not in [c["line"] for c in rq], rq)
wc, _ = sq.fetch_cases(cfg("good", source=["tests/cases/server/worker_class.ts"]), ROOT)
check("fetch: an export default class may forward its request", wc == [], wc)
br = sq.check(uncapped(mode="baseline", routes=[{"route": "GET /exact"}], baseline={"unpaged": ["GET /exact"]}), ROOT)
check("baseline.unpaged tolerates a list that does not page, not a route that fails",
      [u["route"] for u in br["unpaged"]] == ["GET /exact"] and "HTTP Error 400" in br["unpaged"][0]["problem"], br)
badbase = sq.check(cfg("bad", mode="baseline", routes=[], baseline={"fetch": ["fetch(u)"]}), ROOT)
check("a malformed baseline.fetch is a configuration error, not a crash", any("baseline.fetch" in e for e in badbase["errors"]), badbase["errors"])
check("fetch: a Worker forwarding its incoming request is not flagged", all("return fetch(request)" not in c["text"] for c in rq), rq)
moved = sq.check(cfg("good", routes=[{"route": "GET /old-orders"}]), ROOT)
check("a list route that redirects is an error, not 0 queries", any("answered 301" in e for e in moved["errors"]), moved["errors"])

# --- exemptions ------------------------------------------------------------------
ex = sq.check(cfg("bad", exempt_tables={"orders": "fixed list of 10 rows"}), ROOT)
check("exempt table: no scan reported", ex["scans"] == [], ex["scans"])
noreason = sq.check(cfg("good", exempt={"GET /health": " "}), ROOT)
check("exemption without a reason is an error", any("without a reason" in e for e in noreason["errors"]), noreason["errors"])

# --- missing_indexes on its own --------------------------------------------------
db = sqlite3.connect(":memory:")
db.executescript(open(os.path.join(CASES, "migrations_bad", "0001_init.sql")).read())
check("plan: full list without WHERE is fine", sq.missing_indexes(db, "SELECT id FROM orders ORDER BY id") == [])
check("plan: primary key lookup is fine", sq.missing_indexes(db, "SELECT name FROM customers WHERE id = ?") == [])
check("plan: WHERE on a column without index is caught", sq.missing_indexes(db, "SELECT id FROM orders WHERE total > ?") != [])
j = sq.missing_indexes(db, "SELECT o.id FROM orders o JOIN plans p ON p.name = o.customer_id")
check("plan: join on columns without index is caught (automatic index)", j != [] and "AUTOMATIC" in j[0][1], j)
check("plan: join through a primary key is fine",
      sq.missing_indexes(db, "SELECT c.name FROM orders o JOIN customers c ON c.id = o.customer_id") == [])
check("plan: named parameters work", sq.missing_indexes(db, "SELECT id FROM orders WHERE total = ?1") != [])

# --- safety: counter copy, production leak, no header ----------------------------
d = tempfile.mkdtemp()
shutil.copytree(os.path.join(ROOT, "tests"), os.path.join(d, "tests"))
os.makedirs(os.path.join(d, "templates", "server"))
with open(os.path.join(ROOT, "templates", "server", "d1_queries.ts")) as f:
    open(os.path.join(d, "templates", "server", "d1_queries.ts"), "w").write(f.read() + "// changed\n")
open(os.path.join(d, "wrangler.toml"), "w").write('[vars]\nDB_QUERY_HEADERS = "1"\n')
r = sq.check(cfg("good"), d)
check("a changed counter copy is an error", any("differs" in e for e in r["errors"]), r["errors"])
check("DB_QUERY_HEADERS in wrangler.toml is an error", any("wrangler.toml sets DB_QUERY_HEADERS" in e for e in r["errors"]), r["errors"])
nohdr = sq.check(cfg("good", server=dict(cfg("good")["server"], start=cfg("good")["server"]["start"].replace("server.mjs", "server.mjs --no-header"))), ROOT)
check("a server not wrapped by the counter is an error", any("no x-db-queries" in e for e in nohdr["errors"]), nohdr["errors"])

node = subprocess.run(["node", os.path.join(CASES, "counter_check.mjs")], capture_output=True, text=True)
for line in node.stdout.splitlines():
    check("counter: " + line.split(" ", 1)[1], line.startswith("ok "), line)
check("counter: node checks ran", node.returncode == 0 and node.stdout.count("ok ") >= 5, node.stdout + node.stderr)

# --- lint: query inside a loop ---------------------------------------------------
def lint(f):
    p = subprocess.run(["node", os.path.join(ROOT, "tools", "query_in_loop.mjs"), ROOT, f], capture_output=True, text=True)
    return json.loads(p.stdout)


lb = lint(os.path.join(CASES, "handlers_bad.mjs"))
check("lint: for loop and per-row batch flagged", [c["line"] for c in lb] == [9, 16], lb)
check("lint: good server has nothing", lint(os.path.join(CASES, "handlers_good.mjs")) == [])
check("lint: Promise.all and regex exec are not queries", lint(os.path.join(CASES, "loop_false_positives.mjs")) == [])

print("server_queries: " + ("ok" if not fails else f"{fails} failed"))
sys.exit(1 if fails else 0)
