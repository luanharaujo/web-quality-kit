#!/usr/bin/env python3
"""Proof of the logs and request id checks: the kit's helper passes; loose
console logs, a server without the wrapper, a homemade wrapper that ignores
the caller's id, personal data in a log line and a changed helper are caught;
the console baseline only shrinks."""
import copy
import json
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
S = os.path.join(ROOT, "scripts")
CASES = os.path.join(ROOT, "tests", "cases", "observability")
sys.path.insert(0, S)
import server_observability as so  # noqa: E402

PT = json.load(open(os.path.join(S, "panel_pt.json")))

fails = 0


def check(name, cond, detail=""):
    global fails
    print(("ok   " if cond else "FAIL ") + name + ("" if cond else f"  {detail}"))
    fails += 0 if cond else 1


def cfg(variant, **over):
    c = json.load(open(os.path.join(CASES, f"observability.{variant}.json")))
    c.update(over)
    return c


def start(variant):
    return dict(cfg("good")["server"], start=f"node tests/cases/observability/server.mjs {variant} {{port}}")


def panel(result, base=None, *flags):
    d = tempfile.mkdtemp()
    head, b = os.path.join(d, "h.json"), os.path.join(d, "b.json")
    json.dump({"kit": 1, "metrics": {"observability": result}}, open(head, "w"))
    if base is not None:
        json.dump({"kit": 1, "metrics": {"observability": base}}, open(b, "w"))
    p = subprocess.run([sys.executable, os.path.join(S, "web_health.py"), "compare", b if base is not None else "-",
                        head, *flags], capture_output=True, text=True)
    return p.returncode, p.stdout


def problems(r):
    return " | ".join(p for x in r["routes"] for p in x["problems"])


# --- the template itself (Node) ---------------------------------------------------
node = subprocess.run(["node", os.path.join(CASES, "observe_check.mjs")], capture_output=True, text=True)
for line in node.stdout.splitlines():
    check("helper: " + line.split(" ", 1)[1], line.startswith("ok "), line)
check("helper: node checks ran", node.returncode == 0 and node.stdout.count("ok ") >= 25, node.stdout + node.stderr)

# --- the good server passes ---------------------------------------------------------
good = so.check(cfg("good"), ROOT)
check("good: no errors", good["errors"] == [], good["errors"])
check("good: every route has the id, echoes and replaces", all(r["ok"] for r in good["routes"]), good["routes"])
check("good: the unknown path is probed too", any(so.UNKNOWN in r["route"] for r in good["routes"]))
check("good: log lines all JSON with the id, no personal data", good["log_problems"] == [], good["log_problems"])
check("good: no loose console", good["console"] == [], good["console"])
rc, out = panel(good)
check("good: panel green, exit 0", rc == 0 and "🔴" not in out, out)

redir = so.check(cfg("good", routes=cfg("good")["routes"] + [{"route": "GET /login"}]), ROOT)
check("a 302 to another host is the answer: not followed, still has the id",
      next(r for r in redir["routes"] if r["route"] == "GET /login")["ok"], redir["routes"])
check("package@version in the output is not personal data", good["log_problems"] == [], good["log_problems"])
ready_redirect = so.check(cfg("good", server=dict(start("good"), ready="/login")), ROOT)
check("a ready path that redirects counts as an answer", ready_redirect["errors"] == [], ready_redirect["errors"])
check("the report shows field names, never the line", all("ana" not in p["example"] for p in so.check(cfg("bad"), ROOT)["log_problems"]))
locked = so.check(cfg("good", routes=[{"route": "GET /private"}]), ROOT)
check("a route that answers 401 did not exercise its work: caught",
      any("answered 401" in p for p in locked["routes"][0]["problems"]), locked["routes"])
expected = so.check(cfg("good", routes=[{"route": "GET /private", "status": 401}]), ROOT)
check("a route meant to answer 401 says so and passes", expected["routes"][0]["ok"], expected["routes"])
twins = so.check(cfg("good", routes=[{"route": "GET /orders/:id", "path": "/orders/1"},
                                    {"route": "GET /orders/:id", "path": "/orders/999", "status": 404}]), ROOT)
check("two entries with the same route name are both checked", len(twins["routes"]) == 3 and all(r["ok"] for r in twins["routes"]),
      twins["routes"])
remote = so.check(cfg("good", server=dict(start("good"), url="http://127.0.0.1.example.com:{port}")), ROOT)
check("a server url that only starts like 127.0.0.1 is refused", any("local address" in e for e in remote["errors"]), remote["errors"])
typo = so.check(cfg("good", routes=[{"route": "GET /ordrs"}]), ROOT)
check("a route that answers 404 (typo) is caught", any("answered 404" in p for p in typo["routes"][0]["problems"]), typo["routes"])
nosrc = so.check(cfg("good", source=["tests/cases/observability/no_such_folder"], routes=[]), ROOT)
check("a source folder that does not exist is an error", any("not found" in e for e in nosrc["errors"]), nosrc["errors"])

# --- the bad ones are caught ----------------------------------------------------------
bad = so.check(cfg("bad"), ROOT)
check("bad: three loose console calls (log, log, error)", [c["line"] for c in bad["console"]] == [5, 10, 18], bad["console"])
kinds = " ".join(p["problem"] for p in bad["log_problems"])
check("bad: JSON line without request_id caught", "without request_id" in kinds, bad["log_problems"])
check("bad: e-mail in a log line caught", "personal data" in kinds and "e-mail" in kinds, bad["log_problems"])
check("bad: the e-mail is masked in the report", "ana@" not in json.dumps(bad), bad["log_problems"])
rc, out = panel(bad)
check("bad: red on the first run (a rule)", rc == 1 and out.count("🔴") >= 2, out)
check("bad: red even with --no-fail", panel(bad, None, "--no-fail")[0] == 1)
nf = subprocess.run([sys.executable, os.path.join(S, "web_health.py"), "compare", "-", (lambda d: (json.dump(
    {"kit": 1, "metrics": {"observability": bad}}, open(os.path.join(d, "h.json"), "w")), os.path.join(d, "h.json"))[1])(
    tempfile.mkdtemp()), "--no-fail"], capture_output=True, text=True)
check("bad: with --no-fail the error names the rule, not the performance budget",
      "Rule broken" in nf.stderr and "Performance budget" not in nf.stderr, nf.stderr)
rc, out = panel(bad, None, "--lang", "pt")
check("bad: panel in Portuguese", PT["console.* outside the log helper"] in out and PT["Request id and JSON log per request"] in out, out)

nowrap = so.check(cfg("good", server=start("nowrap")), ROOT)
check("no wrapper: every route lacks x-request-id",
      all(any("no x-request-id" in p for p in r["problems"]) for r in nowrap["routes"]), problems(nowrap))
check("no wrapper: no request log line", all(any("no JSON \"request\" log line" in p for p in r["problems"]) for r in nowrap["routes"]),
      problems(nowrap))

fresh = so.check(cfg("good", server=start("fresh-id")), ROOT)
check("homemade wrapper: the caller's id not kept is caught",
      all(any("did not come back" in p for p in r["problems"]) for r in fresh["routes"]), problems(fresh))
check("homemade wrapper: text log instead of JSON is caught",
      all(any("no JSON \"request\" log line" in p for p in r["problems"]) for r in fresh["routes"]), problems(fresh))
check("homemade wrapper: e-mail in a plain-text line is caught too",
      any("personal data" in p["problem"] for p in fresh["log_problems"]), fresh["log_problems"])
check("homemade wrapper: red", panel(fresh)[0] == 1)

# --- baseline mode: tolerated today, never grows ---------------------------------------
old = so.check(cfg("bad", mode="baseline", baseline={"console": 3}), ROOT)
check("baseline: 3 loose calls tolerated", old["baseline_console"] == 3 and old["console_allowed"] == 3, old)
only_console = dict(old, log_problems=[], routes=[dict(r, ok=True, problems=[]) for r in old["routes"]])
rc, out = panel(only_console, only_console)
check("baseline: same as main passes (🟡)", rc == 0 and "🟡" in out, out)
fewer = dict(only_console, console=only_console["console"][:2], baseline_console=2)
check("baseline: one more than main is red", panel(only_console, fewer)[0] == 1)
check("baseline: shrinking passes", panel(fewer, only_console)[0] == 0)
over = dict(only_console, console_allowed=2, baseline_console=2)
check("baseline: more calls than the baseline allows is red", panel(over)[0] == 1)
broken_main = dict(only_console, errors=["source: not found: src"], baseline_console=0, console=[])
broken_main.pop("console_ran", None)
check("baseline: a main where the lint could not run is not compared", panel(only_console, broken_main)[0] == 0)
bad_config_main = dict(fewer, errors=["observability.json: bad JSON"], console=[], baseline_console=0)
bad_config_main.pop("console_ran", None)
check("baseline: a main with a broken config (lint never ran) is not compared", panel(only_console, bad_config_main)[0] == 0)
server_down_main = dict(fewer, errors=["server: did not start"])
check("baseline: a main whose server failed still counts its console calls", panel(only_console, server_down_main)[0] == 1)
abs_bad = so.check(cfg("bad"), ROOT)
check("absolute: any loose call is red even without other problems",
      panel(dict(abs_bad, log_problems=[], routes=[dict(r, ok=True, problems=[]) for r in abs_bad["routes"]]))[0] == 1)

# --- configuration -------------------------------------------------------------------
d = tempfile.mkdtemp()
shutil.copytree(os.path.join(ROOT, "tests"), os.path.join(d, "tests"))
helper = os.path.join(d, "tests", "cases", "observability", "app", "observe.ts")
open(helper, "a").write("// changed\n")
os.remove(os.path.join(d, "tests", "cases", "observability", "app", "d1_queries.ts"))
r = so.check(cfg("good", routes=[]), d)
check("a changed helper copy is an error", any("differs from the kit's observe.ts" in e for e in r["errors"]), r["errors"])
check("d1_queries.ts missing next to the helper is an error", any("d1_queries.ts is missing" in e for e in r["errors"]), r["errors"])
check("no routes is an error", any(e.startswith("routes:") for e in r["errors"]), r["errors"])
check("bad mode is an error", so.check(cfg("good", mode="loose"), ROOT)["errors"] != [])
dead = so.check(cfg("good", server=dict(start("good"), start="exit 3")), ROOT)
check("a server that does not start is an error", any(e.startswith("server:") for e in dead["errors"]), dead["errors"])
check("configuration error is red", panel(dead)[0] == 1)

# --- the personal data patterns of the output scan -------------------------------------
PHONE_INTL = "+55" + "61" + "99999" + "1234"
check("scan: phone with +55 is personal data", so.pii_in("sent to " + PHONE_INTL) == ["phone"], so.pii_in(PHONE_INTL))
check("scan: package@version is not", so.pii_in("hono@4.6.1 wrangler@3.90.0") == [])
ID_LIKE = "kit-c0" + "5977301468"
UUID_LIKE = "0b1e2c3d-4" + "567-4abc-9def-" + "61999991234a"
check("scan: a Unix time in ms is not a phone", so.pii_in(so.without_ids("at " + "1795912" + "345678")) == [])
PHONE_SP = "119" + "9999" + "1234"
check("scan: an 11-digit phone that starts with 1 is still a phone", "phone" in so.pii_in(so.without_ids("to " + PHONE_SP)),
      so.pii_in(so.without_ids(PHONE_SP)))
check("scan: request ids are not personal data (kit-<hex>, UUID, request_id field)",
      so.pii_in(so.without_ids(f'{{"request_id":"{ID_LIKE}"}} {UUID_LIKE} {ID_LIKE}')) == [],
      so.pii_in(so.without_ids(f'{ID_LIKE} {UUID_LIKE}')))

import server_run  # noqa: E402
long_out = "login failed: Authorization: Bearer " + "x" * 500 + "\nx-api-key: k" + "y" * 20 + "\nsession_id=abc123def\n"
red = server_run.redact(long_out)
check("server output is cleaned before it is cut (a long token never leaks)",
      "xxxxx" not in red and "yyyy" not in red and "abc123def" not in red and len(red) <= 400, red)

huge = server_run.redact("login with Authorization: Bearer " + "t" * 9000)
check("a 9000-character token line never leaks (dropped, not cut)", "tttt" not in huge, huge[:80])
cut = server_run.redact("x" * 7000 + "\nAuthorization: Bearer " + "s" * 1500 + "\nok\n" + "y" * 100)
check("a long output cut at a line boundary still hides the token", "ssss" not in cut, cut[:80])
check("a run of 50 digits is hidden", "1111" not in server_run.redact("id " + "1" * 50))
import time  # noqa: E402
t0 = time.time()
server_run.redact("a" * 40000 + " " + "b." * 20000)
so.pii_in("a" * 40000)
check("long output is cleaned fast (< 1 s for 40 thousand characters)", time.time() - t0 < 1, time.time() - t0)

# --- the lint on its own -------------------------------------------------------------
def lint(*files, skip=()):
    p = subprocess.run(["node", os.path.join(ROOT, "tools", "console_outside_log.mjs"), ROOT, *skip, "--", *files],
                       capture_output=True, text=True)
    return json.loads(p.stdout)


t = tempfile.mkdtemp(dir=CASES)
try:
    open(os.path.join(t, "a.ts"), "w").write(
        "const x: number = 1\n// console.log('comment')\nconst s = 'console.log(1)'\n"
        "globalThis.console.warn('w')\nconsole['error']('e')\nlogger.log('fine')\n")
    open(os.path.join(t, "a.test.ts"), "w").write("console.log('tests may print')\n")
    open(os.path.join(t, "b.ts"), "w").write("p.catch(console.error)\nconst { log } = console\nconst x = 1\n"
                                             "const o = { logger: console }\nnew Logger(console)\nif (typeof console !== 'undefined') {}\n")
    hits = lint(os.path.relpath(t, ROOT))
    check("lint: comments and strings are not calls; globalThis and console[...] are",
          [(h["file"][-4:], h["line"]) for h in hits if h["file"].endswith("a.ts")] == [("a.ts", 4), ("a.ts", 5)], hits)
    check("lint: console passed as a value and console destructured are caught",
          [h["line"] for h in hits if h["file"].endswith("b.ts")] == [1, 2, 4, 5], hits)
    check("lint: test files are skipped", all(not h["file"].endswith(".test.ts") for h in hits), hits)
    check("lint: the helper files are skipped",
          lint("templates/server", skip=["templates/server/observe.ts", "templates/server/d1_queries.ts"]) == [])
finally:
    shutil.rmtree(t)

print("server_observability: " + ("ok" if not fails else f"{fails} failed"))
sys.exit(1 if fails else 0)
