#!/usr/bin/env python3
"""Database checks of a server (Cloudflare Worker + D1 or any HTTP server that
uses the kit's query counter, templates/server/d1_queries.ts).

  server_queries.py check [--config server-queries.json] [--root .] > server_queries.json

1. N+1: every list route is called with 3 rows and with 30 rows in the
   database; the number of queries (header x-db-queries) must be the same.
   Growing with the rows = red.
2. Coverage: every GET route the scanner finds must have that test, or be
   exempt with a reason. Repository in mode "absolute" (new): a route without
   the test is red. Mode "baseline" (old repository migrating): routes listed
   in baseline.routes are tolerated; any other is red, and the list may not
   grow against main (web_health.py compare).
3. Indexes: the SQL of every tested request (header x-db-sql) goes through
   EXPLAIN QUERY PLAN on an in-memory SQLite with the repository's
   migrations. A full table scan where the query filters or joins, or an
   automatic index, means a missing index = red, unless the table is in
   exempt_tables (with a reason) or the query is in baseline.scans.
4. Safety: the counter must be the kit's own copy, and DB_QUERY_HEADERS may
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
  routes_command  prints "METHOD /path" lines; required until the kit ships the
                  interaction check's scanner (scripts/interaction_gate.py)
  migrations      folder of .sql migrations (index check; empty = off)
  exempt_tables   {"plans": "reason"} small-by-nature tables
  pagination      {"param": "limit", "size": 10, "max": 50, "next_fields": [...],
                  "items_field": "items", "exempt": {"GET /totals": "reason"},
                  "cache_buster": "_rows"} (cache_buster: extra query parameter
                  on the cap calls so a response cache cannot answer with old
                  rows; "" turns it off for an API that refuses unknown parameters)
                  (defaults shown; items_field default = items, data or results,
                  else the only list in the response; exempt = tested lists that do
                  not page by nature, such as a grouped summary, each with a reason)
  fetch_timeout   {"max_ms": 10000, "internal_hosts": ["api.example.com"]}
  baseline        {"routes": [...], "scans": [{"table": t, "sql": s}],
                   "unpaged": ["GET /x"], "fetch": [{"file": f, "text": t}]}

5. Pagination: each tested list route, with more rows than a page, called
   with ?limit=size must return exactly size items and a next cursor/page;
   with no limit, and with a limit far above the cap, it must stay at or under
   the cap (refusing that limit with 400 or 422 also passes). A route that fails is red
   (baseline mode: tolerated if in baseline.unpaged, which may not grow).
6. Time limit: every fetch to an external host needs a signal
   (AbortSignal.timeout(ms) up to max_ms, or an AbortController); a binding
   (env.X.fetch) and a relative URL are internal. Red, with baseline.fetch.
"""
import argparse
import http.client
import base64
import collections
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
LEAK_VAR = "DB_QUERY_HEADERS"


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


def get(url, headers, timeout=30, body=False):
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:  # nosec B310: local server, http(s) only
        data = r.read()
        return (r.status, r.headers, data) if body else (r.status, r.headers)


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
                self.fail(f"the server stopped (exit {self.proc.returncode})")
            try:
                get(ready, {}, timeout=5)
                return
            except (urllib.error.URLError, OSError):
                time.sleep(0.5)
        self.fail(f"the server did not answer {ready}")

    def fail(self, why):
        # Never leave the server running when it did not get ready.
        text = self.tail()
        self.stop()
        raise RuntimeError(f"{why}: {text}")

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


def headers_for(cfg, root, tmp, server):
    if not cfg.get("headers_command"):
        return {}
    return json.loads(run(fill(cfg["headers_command"], tmp=tmp, port=server.port), root, "headers_command"))


def measure(cfg, root, tmp, server, routes):
    """{route: {3: count, 30: count}} and the SQL seen, or errors."""
    counts = {r["route"]: {} for r in routes}
    errors, sql = [], set()
    for n in SIZES:
        run(fill(cfg["seed"], n=n, tmp=tmp), root, f"seed {n}")
        headers = headers_for(cfg, root, tmp, server)
        for r in routes:
            key = route_key(r["route"])
            path = r.get("path") or key.split(" ", 1)[1]
            url = server.base + path
            try:
                # Twice: the larger count wins, so a cache that answers the
                # second call cannot hide an N+1 (0 and 0 would pass).
                calls = [get(url, {**headers, **r.get("headers", {})})[1] for _ in range(2)]
            except urllib.error.HTTPError as e:
                errors.append(f"{r['route']} with {n} rows: HTTP {e.code}")
                continue
            except (urllib.error.URLError, OSError, http.client.HTTPException) as e:
                errors.append(f"{r['route']} with {n} rows: {e}")
                continue
            values = [h.get("x-db-queries") for h in calls]
            if None in values:
                errors.append(f"{r['route']}: no x-db-queries header (is the handler wrapped by withQueryCount, "
                              f"with DB_QUERY_HEADERS=1 in .dev.vars?)")
                continue
            for extra in calls:
                if extra.get("x-db-sql"):
                    sql.update(json.loads(base64.b64decode(extra["x-db-sql"]).decode()))
            counts[r["route"]][n] = max(int(v) for v in values)
    return counts, sorted(sql), errors


# --- pagination --------------------------------------------------------------

NEXT_FIELDS = ("next", "cursor", "next_cursor", "nextCursor", "next_page", "nextPage")


def page_of(data, items_field=None, next_fields=NEXT_FIELDS):
    """(items, next) of a list response: a bare JSON list has no next. Items
    come from items_field, else items/data/results, else the only list in the
    object; anything else (no list, two unnamed lists, a number) is None."""
    body = json.loads(data)
    if isinstance(body, list):
        return body, None
    if not isinstance(body, dict):
        return None, None
    if items_field:
        items = body.get(items_field)
    else:
        named = [body[k] for k in ("items", "data", "results") if isinstance(body.get(k), list)]
        lists = [v for v in body.values() if isinstance(v, list)]
        items = named[0] if named else (lists[0] if len(lists) == 1 else None)
    if not isinstance(items, list):
        items = None
    nxt = next((body[f] for f in next_fields if body.get(f) not in (None, "", False)), None)
    return items, nxt


def with_param(path, name, value):
    return f"{path}{'&' if '?' in path else '?'}{name}={value}"


def paging(cfg, root, tmp, server, routes, headers_for):
    """Each list route, with more rows than one page: limit=size must return
    exactly size items and a next cursor/page; no limit, and a limit far above
    the cap, must stay at or under the cap. Returns {route: problem or ""}."""
    p = cfg.get("pagination", {})
    size, cap = int(p.get("size", 10)), int(p.get("max", 50))
    param = p.get("param", "limit")
    buster = p.get("cache_buster", "_rows")
    fields = tuple(p.get("next_fields", NEXT_FIELDS))
    result = {}
    over = cap * 20  # a client asking for far more than the cap must still get at most the cap
    for n, step in ((max(30, size * 2), "page"), (cap + 10, "cap"), (cap + 10, "over")):
        if step != "over":  # same rows as the cap step
            run(fill(cfg["seed"], n=n, tmp=tmp), root, f"seed {n}")
        headers = headers_for()
        for r in routes:
            key = route_key(r["route"])
            if result.get(key):
                continue
            path = r.get("path") or key.split(" ", 1)[1]
            url = server.base + (with_param(path, param, size) if step == "page"
                                 else with_param(path, param, over) if step == "over" else path)
            if step != "page" and buster:
                # A new URL after the reseed, so a response cache cannot answer with the old rows.
                url = with_param(url, buster, n)
            try:
                _, _, data = get(url, {**headers, **r.get("headers", {})}, body=True)
                items, nxt = page_of(data, r.get("items_field") or p.get("items_field"), fields)
            except urllib.error.HTTPError as e:
                # Refusing a limit above the cap (400 or 422) keeps the cap too.
                if step == "over" and e.code in (400, 422):
                    result.setdefault(key, "")
                else:
                    result[key] = f"{step}: {e}"
                continue
            except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException) as e:
                result[key] = f"{step}: {e}"
                continue
            if items is None:
                result[key] = "the response has no list"
            elif step == "page" and (len(items) != size or nxt is None):
                result[key] = (f"{param}={size} with {n} rows returned {len(items)} items"
                               + ("" if nxt is not None else " and no next cursor"))
            elif step == "cap" and len(items) > cap:
                result[key] = f"no {param} with {n} rows returned {len(items)} items (cap {cap})"
            elif step == "over" and len(items) > cap:
                result[key] = f"{param}={over} with {n} rows returned {len(items)} items (cap {cap})"
            else:
                result.setdefault(key, "")
    return result


# --- external calls ----------------------------------------------------------

def fetch_cases(cfg, root):
    """fetch to an external host without a time limit (tools/fetch_timeout.mjs)."""
    src = cfg.get("source", [])
    if not src:
        return [], []
    t = cfg.get("fetch_timeout", {})
    cmd = ["node", os.path.join(KIT, "tools", "fetch_timeout.mjs"), root, "--max-ms", str(int(t.get("max_ms", 10000)))]
    for h in t.get("internal_hosts", []):
        cmd += ["--internal", h]
    out = subprocess.run(cmd + ["--"] + src, capture_output=True, text=True)
    if out.returncode:
        raise RuntimeError(f"fetch_timeout.mjs failed: {out.stderr.strip()[-300:]}")
    return json.loads(out.stdout), t


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
    """wrangler configs that set DB_QUERY_HEADERS (would reach production)."""
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
           "scans": [], "baseline_scans": [], "errors": [], "indexes": bool(cfg.get("migrations")),
           # Set only once each part has really run: a main whose server or
           # lint failed has no numbers for it, so the PR's run is a first measure.
           "measured": {"routes": True, "scans": False, "unpaged": False, "fetch": False},
           "unpaged": [], "baseline_unpaged": [], "fetch": [], "baseline_fetch": []}
    err = out["errors"]
    if out["mode"] not in ("absolute", "baseline"):
        err.append(f"mode must be absolute or baseline, not {out['mode']!r}")
        return out
    for name, why in {**cfg.get("exempt", {}), **cfg.get("exempt_tables", {}),
                      **cfg.get("pagination", {}).get("exempt", {})}.items():
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

    baseline_mode = out["mode"] == "baseline"
    for d in cfg.get("source", []):
        if not os.path.exists(os.path.join(root, d)):
            err.append(f"source: {d} does not exist (a typo would switch the lint rules off)")
    fetch_base = base.get("fetch", []) if baseline_mode else []
    if not isinstance(fetch_base, list) or not all(isinstance(c, dict) and {"file", "text"} <= c.keys() for c in fetch_base):
        err.append('baseline.fetch: each entry must be {"file": ..., "text": ...}')
        fetch_base = []
    try:
        cases, _ = fetch_cases(cfg, root)
        # Counted per (file, line text): a second identical call in the same
        # file is a new case, not covered by the first one's baseline entry.
        known = collections.Counter((c["file"], c["text"]) for c in fetch_base)
        for c in cases:
            k = (c["file"], c["text"])
            if known[k] > 0:
                known[k] -= 1
                out["baseline_fetch"].append(c)
            else:
                out["fetch"].append(c)
        out["measured"]["fetch"] = bool(cfg.get("source"))
    except (RuntimeError, ValueError) as e:
        err.append(str(e))

    if not cfg.get("routes"):
        return out
    tmp = tempfile.mkdtemp(prefix="db-queries-")
    server = None
    try:
        run(fill(cfg["seed"], n=SIZES[0], tmp=tmp), root, "seed")
        server = Server(cfg, root, tmp)
        counts, sql, errors = measure(cfg, root, tmp, server, cfg["routes"])
        err.extend(errors)
        no_paging = {route_key(k) for k in cfg.get("pagination", {}).get("exempt", {})}
        listed = [r for r in cfg["routes"] if route_key(r["route"]) not in no_paging]
        pages = paging(cfg, root, tmp, server, listed, lambda: headers_for(cfg, root, tmp, server))
        paged_ok = {route_key(k) for k in base.get("unpaged", [])} if baseline_mode else set()
        for key, problem in sorted(pages.items()):
            if problem:
                # The baseline tolerates a list that does not page yet, never a
                # route that fails (HTTP error, no list in the response).
                broken = problem.startswith(("page:", "cap:", "over:")) or problem == "the response has no list"
                (out["baseline_unpaged"] if key in paged_ok and not broken else out["unpaged"]).append({"route": key, "problem": problem})
        out["measured"]["unpaged"] = True
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
        out["measured"]["scans"] = True
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
