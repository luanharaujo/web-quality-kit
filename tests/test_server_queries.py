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
check("bad: panel in Portuguese", "Consultas ao banco por lista" in out and "3 itens" in out, out)

# --- baseline mode: tolerated today, never grows -------------------------------
base_cfg = cfg("bad", mode="baseline", baseline={"routes": ["GET /products"],
               "scans": [{"table": "orders", "sql": "SELECT total FROM orders WHERE customer_id = ?"}]},
               exempt={"GET /health": "no list"})
old = sq.check(base_cfg, ROOT)
check("baseline: listed route tolerated", old["uncovered"] == [] and len(old["baseline_routes"]) == 1, old)
check("baseline: listed scan tolerated", old["scans"] == [] and len(old["baseline_scans"]) == 1, old)
fixed = copy.deepcopy(old)
fixed["routes"] = [dict(r, ok=True, large=r["small"]) for r in fixed["routes"]]
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
open(os.path.join(d, "wrangler.toml"), "w").write('[vars]\nQLD_QUERY_HEADER = "1"\n')
r = sq.check(cfg("good"), d)
check("a changed counter copy is an error", any("differs" in e for e in r["errors"]), r["errors"])
check("QLD_QUERY_HEADER in wrangler.toml is an error", any("wrangler.toml sets QLD_QUERY_HEADER" in e for e in r["errors"]), r["errors"])
nohdr = sq.check(cfg("good", server=dict(cfg("good")["server"], start=cfg("good")["server"]["start"].replace("server.mjs", "server.mjs --no-header"))), ROOT)
check("a server not wrapped by the counter is an error", any("no x-qld-queries" in e for e in nohdr["errors"]), nohdr["errors"])

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
