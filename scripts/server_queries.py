#!/usr/bin/env python3
"""Database checks of a server (Cloudflare Worker + D1 or any HTTP server that
uses the kit's query counter, templates/server/d1_queries.ts).

  server_queries.py check [--config server-queries.json] [--root .] > server_queries.json

1. N+1: every list route is called with 3 rows and with 30 rows in the
   database; the number of queries (header x-qld-queries) must be the same.
   Growing with the rows = red.
2. Coverage: every GET route the scanner finds must have that test, or be
   exempt with a reason. Repository in mode "absolute" (new): a route without
   the test is red. Mode "baseline" (old repository migrating): routes listed
   in baseline.routes are tolerated; any other is red, and the list may not
   grow against main (web_health.py compare).
3. Indexes: the SQL of every tested request (header x-qld-sql) goes through
   EXPLAIN QUERY PLAN on an in-memory SQLite with the repository's
   migrations. A full table scan where the query filters or joins, or an
   automatic index, means a missing index = red, unless the table is in
   exempt_tables (with a reason) or the query is in baseline.scans.
4. Safety: the counter must be the kit's own copy, and QLD_QUERY_HEADER may
   never be set in a wrangler config (that would be production).

The JSON written here is read by web_health.py (--server-queries).

Config (server-queries.json, paths relative to the repository root):
  mode            "absolute" | "baseline"
  counter         the app's copy of d1_queries.ts
  server.start    command that starts the server ({port}, {tmp} replaced)
  server.url      base URL ({port} replaced; a free port is picked)
  server.ready    path polled until it answers (default /)
  seed            command that leaves exactly {n} rows in the lists ({tmp})
  headers_command optional, prints a JSON object of request headers (login);
                  run after every seed
  routes          [{"route": "GET /x", "path": "/x?y=1"}] the tested lists
  exempt          {"GET /health": "reason"} GET routes that list nothing
  source          folders with the server code (route scanner and loop lint)
  routes_command  optional, prints "METHOD /path" lines instead of the scanner
  migrations      folder of .sql migrations (index check; empty = off)
  exempt_tables   {"plans": "reason"} small-by-nature tables
  baseline        {"routes": [...], "scans": [{"table": t, "sql": s}]}
"""
import argparse
import base64
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
KIT = os.path.dirname(HERE)
TEMPLATE = os.path.join(KIT, "templates", "server", "d1_queries.ts")
SIZES = (3, 30)
LEAK_VAR = "QLD_QUERY_HEADER"


# --- routes ------------------------------------------------------------------

def norm_path(path):
    """/x/123/, /x/{id}, /x/:slug → /x/:id (same key as the interaction scanner)."""
    parts = []
    for p in path.split("?")[0].strip("/").split("/"):
        if not p:
            continue
        if p.startswith(":") or (p.startswith("{") and p.endswith("}")) or p.startswith("$") or re.fullmatch(r"\d+", p):
            p = ":id"
        parts.append(p)
    return "/" + "/".join(parts)


def route_key(text):
    method, _, path = text.strip().partition(" ")
    return f"{method.upper()} {norm_path(path.strip())}"


def scan_routes(cfg, root):
    """GET routes of the server, as {key: where}."""
    if cfg.get("routes_command"):
        out = subprocess.run(cfg["routes_command"], shell=True, cwd=root, capture_output=True, text=True)
        if out.returncode:
            raise RuntimeError(f"routes_command failed: {out.stderr.strip()[:300]}")
        found = {route_key(l): "" for l in out.stdout.splitlines() if l.strip()}
    else:
        sys.path.insert(0, HERE)
        try:
            import interaction_gate  # the route scanner of the interaction check
        except ImportError:
            raise RuntimeError("no route scanner: set routes_command in server-queries.json")
        dirs = [os.path.join(root, d) for d in cfg.get("source", [])]
        res = interaction_gate.scan(dirs, methods=("GET",))
        where = res.get("where", {})
        found = {route_key(r): where.get(r, "") for r in res.get("routes", [])}
    return {k: v for k, v in found.items() if k.startswith("GET ")}


# --- server ------------------------------------------------------------------

def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def fill(text, **kw):
    for k, v in kw.items():
        text = text.replace("{" + k + "}", str(v))
    return text


def run(cmd, root, what):
    out = subprocess.run(cmd, shell=True, cwd=root, capture_output=True, text=True)
    if out.returncode:
        raise RuntimeError(f"{what} failed (exit {out.returncode}): {(out.stderr or out.stdout).strip()[-400:]}")
    return out.stdout


def get(url, headers, timeout=30):
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:  # nosec B310: local server, http(s) only
        r.read()
        return r.status, r.headers


class Server:
    def __init__(self, cfg, root, tmp):
        srv = cfg["server"]
        self.port = free_port()
        self.base = fill(srv["url"], port=self.port).rstrip("/")
        if not self.base.startswith(("http://127.0.0.1", "http://localhost")):
            raise RuntimeError("server.url must be a local address (127.0.0.1 or localhost)")
        self.log = open(os.path.join(tmp, "server.log"), "w")
        self.proc = subprocess.Popen(fill(srv["start"], port=self.port, tmp=tmp), shell=True, cwd=root,
                                     stdout=self.log, stderr=subprocess.STDOUT, start_new_session=True)
        ready = self.base + srv.get("ready", "/")
        deadline = time.time() + float(srv.get("timeout", 90))
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"the server stopped (exit {self.proc.returncode}): {self.tail()}")
            try:
                get(ready, {}, timeout=5)
                return
            except (urllib.error.URLError, OSError):
                time.sleep(0.5)
        raise RuntimeError(f"the server did not answer {ready}: {self.tail()}")

    def tail(self):
        self.log.flush()
        with open(self.log.name) as f:
            return f.read()[-400:]

    def stop(self):
        try:
            os.killpg(self.proc.pid, 15)
            self.proc.wait(10)
        except (ProcessLookupError, subprocess.TimeoutExpired, PermissionError):
            try:
                os.killpg(self.proc.pid, 9)
            except (ProcessLookupError, PermissionError):
                pass
        self.log.close()


def measure(cfg, root, tmp, server, routes):
    """{route: {3: count, 30: count}} and the SQL seen, or errors."""
    counts = {r["route"]: {} for r in routes}
    errors, sql = [], set()
    for n in SIZES:
        run(fill(cfg["seed"], n=n, tmp=tmp), root, f"seed {n}")
        headers = {}
        if cfg.get("headers_command"):
            headers = json.loads(run(fill(cfg["headers_command"], tmp=tmp, port=server.port), root, "headers_command"))
        for r in routes:
            key = route_key(r["route"])
            path = r.get("path") or key.split(" ", 1)[1]
            url = server.base + path
            try:
                # Twice: the first request after a seed may warm a cache.
                for _ in range(2):
                    status, h = get(url, {**headers, **r.get("headers", {})})
            except urllib.error.HTTPError as e:
                errors.append(f"{r['route']} with {n} rows: HTTP {e.code}")
                continue
            except (urllib.error.URLError, OSError) as e:
                errors.append(f"{r['route']} with {n} rows: {e}")
                continue
            value = h.get("x-qld-queries")
            if value is None:
                errors.append(f"{r['route']}: no x-qld-queries header (is the handler wrapped by withQueryCount, "
                              f"with QLD_QUERY_HEADER=1 in .dev.vars?)")
                continue
            counts[r["route"]][n] = int(value)
            if h.get("x-qld-sql"):
                sql.update(json.loads(base64.b64decode(h["x-qld-sql"]).decode()))
    return counts, sorted(sql), errors


# --- indexes -----------------------------------------------------------------

def schema_db(root, folder):
    db = sqlite3.connect(":memory:")
    path = os.path.join(root, folder)
    for name in sorted(os.listdir(path)):
        if name.endswith(".sql"):
            with open(os.path.join(path, name)) as f:
                db.executescript(f.read())
    return db


def plan(db, sql):
    """EXPLAIN QUERY PLAN rows (detail text), binding NULL to each parameter."""
    for n in range(0, 200):
        try:
            return [row[3] for row in db.execute("EXPLAIN QUERY PLAN " + sql, [None] * n)]
        except sqlite3.ProgrammingError as e:
            m = re.search(r"uses (\d+)", str(e))
            if m and int(m.group(1)) != n:
                return [row[3] for row in db.execute("EXPLAIN QUERY PLAN " + sql, [None] * int(m.group(1)))]
            if "binding" not in str(e).lower():
                raise
    raise sqlite3.ProgrammingError("too many parameters")


def missing_indexes(db, sql):
    """Tables read in full where an index would do: a bare SCAN when the query
    filters (WHERE) or when it is not the first table read (a join), and any
    automatic index SQLite builds on the fly."""
    tables = {r[0].lower() for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    # The plan names a table by its alias when it has one (FROM orders o).
    alias = {t: t for t in tables}
    for t, a in re.findall(r"\b(?:from|join)\s+([\w\"`\[\]]+)(?:\s+(?:as\s+)?(?!on\b|where\b|join\b|left\b|inner\b|cross\b|order\b|group\b|limit\b|using\b)(\w+))?", sql, re.I):
        t = t.strip('"`[]').lower()
        if t in tables and a:
            alias[a.lower()] = t
    filters = re.search(r"\bwhere\b", sql, re.I) is not None
    found, reads = [], 0
    for detail in plan(db, sql):
        m = re.match(r"(SCAN|SEARCH) (\S+)(?: AS \S+)?(.*)", detail)
        if not m:
            continue
        verb, table, rest = m.group(1), alias.get(m.group(2).lower()), m.group(3)
        if table is None:  # subquery, CTE, constant row
            continue
        reads += 1
        if "AUTOMATIC" in rest:
            found.append((table, detail))
        elif verb == "SCAN" and "USING" not in rest and (filters or reads > 1):
            found.append((table, detail))
    return found


def norm_sql(sql):
    return re.sub(r"\s+", " ", sql).strip()


# --- check -------------------------------------------------------------------

def same_file(a, b):
    with open(a, "rb") as fa, open(b, "rb") as fb:
        return fa.read().replace(b"\r\n", b"\n") == fb.read().replace(b"\r\n", b"\n")


def leaks(root):
    """wrangler configs that set QLD_QUERY_HEADER (would reach production)."""
    found = []
    for base, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in ("node_modules", ".git", ".wrangler")]
        for f in files:
            if re.fullmatch(r"wrangler(\.[\w-]+)*\.(toml|json|jsonc)", f):
                path = os.path.join(base, f)
                with open(path, errors="replace") as fh:
                    if LEAK_VAR in fh.read():
                        found.append(os.path.relpath(path, root))
    return sorted(found)


def check(cfg, root):
    out = {"mode": cfg.get("mode", "absolute"), "routes": [], "uncovered": [], "baseline_routes": [],
           "scans": [], "baseline_scans": [], "errors": [], "indexes": bool(cfg.get("migrations"))}
    err = out["errors"]
    if out["mode"] not in ("absolute", "baseline"):
        err.append(f"mode must be absolute or baseline, not {out['mode']!r}")
        return out
    for name, why in {**cfg.get("exempt", {}), **cfg.get("exempt_tables", {})}.items():
        if not str(why).strip():
            err.append(f"exemption without a reason: {name}")
    counter = os.path.join(root, cfg.get("counter", ""))
    if not cfg.get("counter") or not os.path.isfile(counter):
        err.append("counter: the app's copy of templates/server/d1_queries.ts was not found")
    elif not same_file(counter, TEMPLATE):
        err.append(f"counter: {cfg['counter']} differs from the kit's d1_queries.ts (copy it again, unchanged)")
    for f in leaks(root):
        err.append(f"{f} sets {LEAK_VAR}: the query headers must never reach production (use .dev.vars)")

    tested = {route_key(r["route"]) for r in cfg.get("routes", [])}
    exempt = {route_key(k) for k in cfg.get("exempt", {})}
    base = cfg.get("baseline", {})
    tolerated = {route_key(k) for k in base.get("routes", [])} if out["mode"] == "baseline" else set()
    try:
        found = scan_routes(cfg, root)
    except RuntimeError as e:
        err.append(str(e))
        found = {}
    for key in sorted(found):
        if key in tested or key in exempt:
            continue
        (out["baseline_routes"] if key in tolerated else out["uncovered"]).append({"route": key, "where": found[key]})

    if not cfg.get("routes"):
        return out
    tmp = tempfile.mkdtemp(prefix="qld-queries-")
    server = None
    try:
        run(fill(cfg["seed"], n=SIZES[0], tmp=tmp), root, "seed")
        server = Server(cfg, root, tmp)
        counts, sql, errors = measure(cfg, root, tmp, server, cfg["routes"])
        err.extend(errors)
    except (RuntimeError, KeyError, json.JSONDecodeError) as e:
        err.append(f"server: {e}")
        return out
    finally:
        if server:
            server.stop()
        shutil.rmtree(tmp, ignore_errors=True)
    for r in cfg["routes"]:
        c = counts.get(r["route"], {})
        if len(c) == len(SIZES):
            out["routes"].append({"route": route_key(r["route"]), "small": c[SIZES[0]], "large": c[SIZES[1]],
                                  "ok": c[SIZES[0]] == c[SIZES[1]]})

    if cfg.get("migrations"):
        try:
            db = schema_db(root, cfg["migrations"])
        except (OSError, sqlite3.Error) as e:
            err.append(f"migrations: {e}")
            return out
        small = {t.lower() for t in cfg.get("exempt_tables", {})}
        known = {(s["table"].lower(), norm_sql(s["sql"])) for s in base.get("scans", [])} if out["mode"] == "baseline" else set()
        for q in sql:
            try:
                hits = missing_indexes(db, q)
            except sqlite3.Error as e:
                err.append(f"EXPLAIN failed for {norm_sql(q)[:120]}: {e}")
                continue
            for table, detail in hits:
                if table in small:
                    continue
                item = {"table": table, "sql": norm_sql(q), "plan": detail}
                (out["baseline_scans"] if (table, norm_sql(q)) in known else out["scans"]).append(item)
    return out


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check")
    c.add_argument("--config", default="server-queries.json")
    c.add_argument("--root", default=".")
    a = p.parse_args()
    root = os.path.abspath(a.root)
    try:
        with open(os.path.join(root, a.config)) as f:
            cfg = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(json.dumps({"mode": "absolute", "routes": [], "uncovered": [], "baseline_routes": [], "scans": [],
                          "baseline_scans": [], "indexes": False, "errors": [f"{a.config}: {e}"]}))
        return 0
    print(json.dumps(check(cfg, root), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
