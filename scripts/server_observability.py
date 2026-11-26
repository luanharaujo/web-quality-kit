#!/usr/bin/env python3
"""Structured logs and request ids of a server (Cloudflare Worker or any HTTP
server that uses the kit's templates/server/observe.ts).

  server_observability.py check [--config observability.json] [--root .] > observability.json

1. Helper: the app's observe.ts (and d1_queries.ts next to it) must be the
   kit's own copies.
2. Loose logs: console.* calls in the server code outside the helper
   (tools/console_outside_log.mjs). Mode "absolute" (new repository): any is
   red. Mode "baseline" (old repository migrating): up to baseline.console
   are tolerated, and the number may not grow against main.
3. Request id, on the running server, for every configured route and for an
   unknown path (proof that the wrapper sits at the entry):
   - without x-request-id the response brings a valid new one;
   - a valid x-request-id from the caller comes back unchanged;
   - an invalid one (spaces, too short, quotes) is replaced;
   - the server's log has a JSON "request" line with that id and the fixed
     fields, and every JSON log line has a request_id.
4. Personal data: no line of the server's output (JSON or plain text) may
   carry an e-mail, a CPF or a phone number (the routes send some on purpose when configured to).

The JSON written here is read by web_health.py (--observability).

Config (observability.json, paths relative to the repository root):
  mode            "absolute" | "baseline"
  helper          the app's copy of observe.ts (d1_queries.ts in the same folder)
  source          folders with the server code (lint)
  server.start    command that starts the server ({port}, {tmp} replaced)
  server.url      base URL ({port} replaced; a free port is picked)
  server.ready    path polled until it answers (default /)
  seed            optional command run once before the server starts ({tmp}, n=3)
  headers_command optional, prints a JSON object of request headers (login)
  routes          [{"route": "GET /x", "path": "/x", "body": {...}, "status": 404}] requests
                  to make; a 4xx or 5xx fails unless it is the route's "status"
  baseline        {"console": 3} loose console calls tolerated (mode baseline)
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

import server_run
from server_run import OPENER, Server, fill, run

HERE = os.path.dirname(os.path.abspath(__file__))
KIT = os.path.dirname(HERE)
TEMPLATES = os.path.join(KIT, "templates", "server")
HEADER = "x-request-id"
ID_OK = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
FIXED = ["time", "level", "event", "request_id", "route", "method", "status", "ms", "queries"]
INVALID_IDS = ["has spaces in it", "short", 'quote"12345']
UNKNOWN = "/__kit_unknown_path__"
PII = [("e-mail", re.compile(r"[\w.+-]{1,64}@[\w-]{1,63}(?:\.[\w-]{1,63}){0,7}\.[A-Za-z]{2,24}\b")),
       ("CPF", re.compile(r"\b\d{3}\.\d{3}\.\d{3}-\d{2}\b|\b(?<!\d)\d{11}(?!\d)\b")),
       ("phone", re.compile(r"(?<!\d)(?:\+?\d{2,3}[\s.-]?)?\(?\d{2}\)?[\s.-]?9\d{4}[\s.-]?\d{4}(?!\d)"))]


def same_file(a, b):
    with open(a, "rb") as fa, open(b, "rb") as fb:
        return fa.read().replace(b"\r\n", b"\n") == fb.read().replace(b"\r\n", b"\n")


def call(base, route, headers):
    """(status, response headers) of one request; 4xx/5xx are answers too."""
    method, _, path = route["route"].partition(" ")
    path = route.get("path") or path
    data = None
    h = dict(headers)
    if "body" in route:
        data = json.dumps(route["body"]).encode()
        h.setdefault("content-type", "application/json")
    req = urllib.request.Request(base + path, data=data, method=method.upper(), headers=h)
    try:
        with OPENER.open(req, timeout=30) as r:  # nosec B310: local server, http only; redirects not followed
            r.read()
            return r.status, r.headers
    except urllib.error.HTTPError as e:
        e.read()
        return e.code, e.headers


def log_lines(path):
    """JSON objects found in the server's output (other lines are the runtime's own)."""
    out = []
    with open(path, errors="replace") as f:
        for line in f:
            start = line.find("{")
            if start < 0:
                continue
            try:
                obj = json.loads(line[start:].strip())
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                out.append((line.rstrip("\n"), obj))
    return out


UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
ID_FIELD = re.compile(r'"request_id"\s*:\s*"[^"]{0,80}"')
KIT_ID = re.compile(r"\bkit-[0-9a-z]{12}\b")
EPOCH = re.compile(r"(?<!\d)1\d{9}(?:\d{3})?(?!\d)")  # Unix time in s or ms (10 or 13 digits from 1)


def without_ids(text):
    """The line without request ids (UUIDs, the check's own kit-<hex> ids, any
    request_id value) and Unix times: their digits can look like a CPF or a phone."""
    return EPOCH.sub("", KIT_ID.sub("", UUID.sub("", ID_FIELD.sub("", text))))


def redact(text):
    """Error text shown in the panel (already cleaned and cut by server_run):
    one more pass with this check's own personal data patterns."""
    text = server_run.redact(text, keep=len(text))
    for _, rx in PII:
        text = rx.sub("***", text)
    return text


def pii_in(text):
    return [name for name, rx in PII if rx.search(text)]


def lint(cfg, root, helper):
    skip = [helper, os.path.join(os.path.dirname(helper), "d1_queries.ts")]
    p = subprocess.run(["node", os.path.join(KIT, "tools", "console_outside_log.mjs"), root, *skip, "--",
                        *cfg.get("source", [])], capture_output=True, text=True)
    if p.returncode:
        raise RuntimeError(f"console lint failed: {p.stderr.strip()[-300:]}")
    return json.loads(p.stdout)


def probe(server, routes, headers, tmp):
    """Results per route and the run's log problems."""
    results = []
    sent = []  # the id sent to each entry, by position (two entries may share a route name)
    for r in routes:
        problems = []
        try:
            st, h = call(server.base, r, headers)
            if not r.get("unknown") and st >= 400 and st != r.get("status"):
                problems.append(f"the route answered {st}, so its work was not exercised (login in headers_command? "
                                f"body or path wrong? server error?); a route meant to answer {st} says \"status\": {st}")
            got = h.get(HEADER)
            if not got:
                problems.append(f"no {HEADER} in the response (status {st})")
            elif not ID_OK.match(got):
                problems.append(f"{HEADER} {got!r} is not a valid id")
            mine = "kit-" + "".join("abcdefghjkmnpqrstuvwxyz"[b % 23] for b in os.urandom(12))  # no digit runs
            st, h = call(server.base, r, {**headers, HEADER: mine})
            if h.get(HEADER) != mine:
                problems.append(f"the caller's {HEADER} did not come back (sent {mine}, got {h.get(HEADER)!r})")
            sent.append(mine)
            for bad in INVALID_IDS:
                st, h = call(server.base, r, {**headers, HEADER: bad})
                if h.get(HEADER) == bad or not ID_OK.match(h.get(HEADER) or ""):
                    problems.append(f"an invalid {HEADER} ({bad!r}) was not replaced by a valid one")
                    break
        except (urllib.error.URLError, OSError) as e:
            problems.append(f"request failed: {e}")
        if len(sent) < len(results) + 1:
            sent.append(None)
        results.append({"route": r["route"], "problems": problems})
    # Wait for the runtime to flush: until every id sent shows up, at most 5 s.
    deadline = time.time() + 5
    while True:
        server.log.flush()
        lines = log_lines(server.log.name)
        found = {o.get("request_id") for _, o in lines}
        if all(m in found for m in sent if m) or time.time() > deadline:
            break
        time.sleep(0.2)
    log_problems = []
    for res, mine in zip(results, sent):
        if not mine:
            continue
        own = [o for _, o in lines if o.get("request_id") == mine]
        req = [o for o in own if o.get("event") == "request"]
        if not req:
            res["problems"].append(f"no JSON \"request\" log line with request_id {mine}")
        elif [k for k in FIXED if k not in req[0]]:
            res["problems"].append("request log line without " + ", ".join(k for k in FIXED if k not in req[0]))
    # The report shows field names only: a loose line may carry a token or a name.
    loose = [o for _, o in lines if "request_id" not in o]
    if loose:
        log_problems.append({"problem": "JSON log line without request_id", "count": len(loose),
                             "example": "fields: " + ", ".join(sorted(str(k)[:30] for k in loose[0])[:12])})
    # every output line, JSON or not: a plain-text line from a dependency leaks too
    with open(server.log.name, errors="replace") as f:
        every = [line.rstrip("\n")[:4000] for line in f]  # bounded work per line
    hits = [(i, pii_in(without_ids(text))) for i, text in enumerate(every)]
    hits = [(i, found) for i, found in hits if found]
    if hits:
        kinds = sorted({k for _, found in hits for k in found})
        log_problems.append({"problem": "personal data in a log line (" + ", ".join(kinds) + ")", "count": len(hits),
                             "example": f"line {hits[0][0] + 1} of the server output"})
    for r in results:
        r["ok"] = not r["problems"]
    return results, log_problems


def check(cfg, root):
    out = {"mode": cfg.get("mode", "absolute"), "errors": [], "console": [], "baseline_console": 0,
           "console_allowed": 0, "routes": [], "log_problems": []}
    err = out["errors"]
    if out["mode"] not in ("absolute", "baseline"):
        err.append(f"mode must be absolute or baseline, not {out['mode']!r}")
        return out
    helper = os.path.join(root, cfg.get("helper", ""))
    if not cfg.get("helper") or not os.path.isfile(helper):
        err.append("helper: the app's copy of templates/server/observe.ts was not found")
    else:
        for name in ("observe.ts", "d1_queries.ts"):
            copy = os.path.join(os.path.dirname(helper), name)
            if not os.path.isfile(copy):
                err.append(f"helper: {name} is missing next to {cfg['helper']} (copy it from the kit)")
            elif not same_file(copy, os.path.join(TEMPLATES, name)):
                err.append(f"helper: {os.path.relpath(copy, root)} differs from the kit's {name} (copy it again, unchanged)")
    missing = [s for s in cfg.get("source", []) if not os.path.exists(os.path.join(root, s))]
    if not cfg.get("source"):
        err.append("source: list the folders with the server code")
    elif missing:
        err.append("source: not found: " + ", ".join(missing))
    else:
        try:
            cases = lint(cfg, root, helper)
            out["console_ran"] = True  # a count main can be compared with
        except (RuntimeError, json.JSONDecodeError) as e:
            err.append(str(e))
            cases = []
        allowed = int(cfg.get("baseline", {}).get("console", 0)) if out["mode"] == "baseline" else 0
        out["console_allowed"] = allowed
        out["baseline_console"] = min(len(cases), allowed)
        out["console"] = cases

    routes = list(cfg.get("routes", []))
    if not routes:
        err.append("routes: list at least one request to make (one that writes, if the server has any)")
        return out
    routes.append({"route": "GET " + UNKNOWN, "unknown": True})
    tmp = tempfile.mkdtemp(prefix="kit-observe-")
    server = None
    try:
        if cfg.get("seed"):
            run(fill(cfg["seed"], n=3, tmp=tmp), root, "seed")
        server = Server(cfg, root, tmp)
        headers = {}
        if cfg.get("headers_command"):
            headers = json.loads(run(fill(cfg["headers_command"], tmp=tmp, port=server.port), root, "headers_command"))
        out["routes"], out["log_problems"] = probe(server, routes, headers, tmp)
    except (RuntimeError, KeyError, json.JSONDecodeError) as e:
        err.append(f"server: {redact(str(e))}")
    finally:
        if server:
            server.stop()
        shutil.rmtree(tmp, ignore_errors=True)
    return out


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check")
    c.add_argument("--config", default="observability.json")
    c.add_argument("--root", default=".")
    a = p.parse_args()
    root = os.path.abspath(a.root)
    try:
        with open(os.path.join(root, a.config)) as f:
            cfg = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(json.dumps({"mode": "absolute", "errors": [f"{a.config}: {e}"], "console": [], "baseline_console": 0,
                          "console_allowed": 0, "routes": [], "log_problems": []}))
        return 0
    print(json.dumps(check(cfg, root), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
