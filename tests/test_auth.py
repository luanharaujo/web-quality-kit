#!/usr/bin/env python3
"""Proof of the login check: the good example server passes every item; the bad
one is caught on each, with a readable reason; a login without a declaration
fails; the baseline only shrinks."""
import copy
import json
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
S = os.path.join(ROOT, "scripts")
CASES = os.path.join(ROOT, "tests", "cases", "auth")
sys.path.insert(0, S)
import auth_check as ac  # noqa: E402

fails = 0


LEGACY_HASH = ('import { createHash } from "node:crypto";\n'
               'export const legacy = (password) => createHash("sha256").update(password).digest("hex");\n')


def check(name, cond, detail=""):
    global fails
    print(("ok   " if cond else "FAIL ") + name + ("" if cond else f"  {detail}"))
    fails += 0 if cond else 1


def load(variant):
    return json.load(open(os.path.join(CASES, variant, ".quality", "auth.json")))


def copy_case(variant):
    d = tempfile.mkdtemp()
    dst = os.path.join(d, "app")
    shutil.copytree(os.path.join(CASES, variant), dst)
    # the harness lives one level up from the example
    shutil.copy(os.path.join(CASES, "harness.mjs"), os.path.join(d, "harness.mjs"))
    if variant == "bad":  # written here: a weak hash is not committed as source
        open(os.path.join(dst, "src", "password.js"), "a").write(LEGACY_HASH)
    return dst


def put(root, name, text):
    path = os.path.join(root, name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, "w").write(text)


def edit(root, name, old, new):
    text = open(os.path.join(root, name)).read()
    assert old in text, old
    put(root, name, text.replace(old, new))


def run(root, cfg=None, dynamic=True, declared=True):
    return ac.check(cfg if cfg is not None else load("good"), root, declared, dynamic)


def ids(res):
    return {f["id"] for f in res["findings"]}


def panel(result, base=None):
    d = tempfile.mkdtemp()
    head, b = os.path.join(d, "h.json"), os.path.join(d, "b.json")
    json.dump({"kit": 1, "metrics": {"auth": result}}, open(head, "w"))
    if base is not None:
        json.dump({"kit": 1, "metrics": {"auth": base}}, open(b, "w"))
    p = subprocess.run([sys.executable, os.path.join(S, "web_health.py"), "compare", b if base is not None else "-", head],
                       capture_output=True, text=True)
    return p.returncode, p.stdout


# --- good: passes everything ------------------------------------------------------
root = copy_case("good")
good = run(root)
check("good: no configuration errors", good["errors"] == [], good["errors"])
check("good: no findings (static and dynamic)", good["findings"] == [], good["findings"])
check("good: dynamic checks ran (A3, A4, A5)", {c["rule"] for c in good["checks"]} == {"A3", "A4", "A5"}, good["checks"])
rc, out = panel(good)
check("good: panel green, exit 0", rc == 0 and "🔴" not in out and "Login" in out, out)

# --- bad: every item is caught ----------------------------------------------------
bad = run(copy_case("bad"), load("bad"))
got = ids(bad)
for want, why in [
        ("A0:route:/signin", "route not declared"),
        ("A1:plain:src/schema.sql", "password column that is not a hash"),
        ("A1:weakhash:src/password.js", "SHA-256 on a password"),
        ("A1:bcrypt:src/password.js", "bcrypt cost 8"),
        ("A1:params:bcrypt", "bcrypt exception below the minimum"),
        ("A2:POST /login/start:order", "D1 before the rate limit"),
        ("A3:POST /login/start:missing", "no Turnstile"),
        ("A3:POST /signup:missing", "no siteverify"),
        ("A4:POST /login/start:status", "enumeration"),
        ("A5:global:src/worker.js:login_codes", "global COUNT ceiling"),
        ("A5:global:src/worker.js:challenges", "COUNT with a WHERE that does not filter by the client (a ceiling per kind of challenge)"),
        ("A3:verify-url-wrangler:wrangler.toml", "TURNSTILE_VERIFY_URL in wrangler"),
        ("A3:verify-url-guard:src/verify.js", "override accepted for any address"),
        ("A5:POST /signup:target", "owner locked out by 40 requests from 40 IPs to the same target"),
        ("A5:lockout:src/worker.js", "account lock"),
        ("A5:POST /signup:ip", "owner locked out after a burst"),
        ("A6:console:POST /signup", "console.log of the phone"),
        ("A6:event:POST /signup:phone", "event with the phone"),
        ("A6:POST /login/start", "no login_attempt event")]:
    check(f"bad: caught: {why} ({want})", want in got, sorted(got))
check("bad: every message is readable", all(len(f["msg"]) > 20 for f in bad["findings"]))
rc, out = panel(bad)
check("bad: panel red, exit 1", rc == 1 and "🔴" in out and "owner" in out, out[:400])

# --- Turnstile details (dynamic) --------------------------------------------------
msgs = [f["msg"] for f in bad["findings"] if f["rule"] == "A3"]
for frag in ("without a token", "another hostname", "another action", "twice", "siteverify refuses", "spent"):
    check(f"bad: dynamic A3 '{frag}'", any(frag in m for m in msgs), msgs)

# --- A5: a short 429 on the bursting IP is allowed, nothing longer or silent --------
os.environ["EXAMPLE_LIMIT"] = "40"
for name, old, new, passes in [
        ("Retry-After 60 on the same IP passes", None, None, True),
        ("429 without Retry-After fails", ', { "retry-after": "60" }', "", False),
        ("Retry-After 600 fails", '"retry-after": "60"', '"retry-after": "600"', False)]:
    root = copy_case("good")
    if old:
        w = os.path.join(root, "src", "worker.js")
        text = open(w).read()
        assert old in text
        open(w, "w").write(text.replace(old, new))
    r = run(root)
    found = {i for i in ids(r) if i.startswith("A5")}
    check(f"A5: {name}", (found == set()) == passes and (passes or all(i.endswith(":ip") for i in found)), r["findings"])
del os.environ["EXAMPLE_LIMIT"]

# --- declaration ------------------------------------------------------------------
root = copy_case("good")
shutil.rmtree(os.path.join(root, ".quality"))
und = run(root, {}, dynamic=False, declared=False)
check("login without declaration fails", ids(und) == {"A0:undeclared"}, und["findings"])
empty = tempfile.mkdtemp()
open(os.path.join(empty, "index.html"), "w").write("<h1>site</h1>")
none = run(empty, {}, declared=False)
check("no login and no declaration: nothing to check", none["findings"] == [] and not none["login"], none)
rc, out = panel(none)
check("no login: no row in the panel", rc == 0 and "Login" not in out, out)

root = copy_case("good")
r = run(root, {"login": False}, dynamic=False)
check("login: false while a login exists fails", "A0:undeclared" in ids(r), r["findings"])
r = run(empty, {"login": False}, dynamic=False)
check("switch: login: false without exception fails", ids(r) == {"A0:escape:login:false"}, r)
r = run(empty, {"login": False, "exceptions": [{"hatch": "login:false", "reason": "static site", "approved_by": "security",
                                                "date": "2026-11-25"}]}, dynamic=False)
check("login: false with exception in a site without login passes", r["findings"] == [] and not r["login"], r)
rc, out = panel(r)
check("switch: the login:false exception is listed", rc == 0 and "login:false" in out, out)

cfg = load("good")
cfg["routes"][0].pop("kind")
check("config: kind is required", any("kind" in e for e in run(root, cfg, dynamic=False)["errors"]))
cfg = load("good")
cfg["methods"] = ["sms"]
check("config: unknown method", bool(run(root, cfg, dynamic=False)["errors"]))

# --- global ceiling: a WHERE that filters by the client passes ----------------------
root = copy_case("good")
open(os.path.join(root, "src", "limite.js"), "w").write(
    'export const login = (db, ip) => db.prepare("SELECT COUNT(*) AS n FROM challenges WHERE ip = ?").bind(ip);\n')
check("global ceiling: COUNT filtered by ip passes", not any(i.startswith("A5:global") for i in ids(run(root, dynamic=False))))

# --- switches that weaken the check -------------------------------------------------
root = copy_case("good")


def hatch_cfg(change):
    c = load("good")
    change(c)
    return c


def exception(h):
    return {"hatch": h, "reason": "r", "approved_by": "security", "date": "2026-11-25"}


for hatch, change in [
        ("public:false:POST /signup", lambda c: c["routes"][1].update(public=False)),
        ("anonymous_writes:false:POST /signup", lambda c: c["routes"][1].update(anonymous_writes=False)),
        ("log_in_helper:POST /signup", lambda c: c["routes"][1].update(log_in_helper=True)),
        ("ignore_paths:/signin", lambda c: c.update(ignore_paths=["/signin"])),
        ("password_exception", lambda c: c.update(password_exception=dict(
            exception("x"), algorithm="argon2id", params={"memory_kib": 19456, "iterations": 2, "parallelism": 1}))),
        ("no_dynamic:POST /signup", lambda c: c["routes"][1].pop("body"))]:
    c = hatch_cfg(change)
    r = run(root, c, dynamic=False)
    check(f"switch: {hatch} without exception fails", f"A0:escape:{hatch}" in ids(r), ids(r))
    c["exceptions"] = [exception(hatch)]
    r = run(root, c, dynamic=False)
    check(f"switch: {hatch} with exception passes", f"A0:escape:{hatch}" not in ids(r), ids(r))
    rc, out = panel(r)
    check(f"switch: {hatch} exception listed in the panel", hatch in out, out)
r = run(root, hatch_cfg(lambda c: c.update(d1_calls=["saveCode"])), dynamic=False)
check("switch: adding a D1 helper makes the check stronger, no exception needed", not any("d1_calls" in i for i in ids(r)), ids(r))
c = hatch_cfg(lambda c: c.update(ignore_paths=["/x"], exceptions=[{"hatch": "ignore_paths:/x"}]))
check("switch: exception without reason, approver and date is an error", bool(run(root, c, dynamic=False)["errors"]))
r = ac.check(load("good"), root, True, False, "someone/old-app")
check("switch: auth-dynamic false in a repository needs an exception", "A0:escape:auth-dynamic:false" in ids(r), ids(r))
r = ac.check(load("good"), root, True, False, ac.KIT_REPO)
check("switch: the kit itself may skip the server checks", not any("auth-dynamic" in i for i in ids(r)))
r = ac.check({}, root, False, True, "someone/old-app", True)
check("switch: auth-config off in a repository fails", ids(r) == {"A0:auth-config-off"}, ids(r))
rc, out = panel(r)
check("switch: auth-config off turns the panel red", rc == 1 and "🔴" in out, out)
check("switch: auth-config off in the kit passes", ac.check({}, root, False, True, ac.KIT_REPO, True)["findings"] == [])
c = hatch_cfg(lambda c: c["routes"][0].update(unknown=c["routes"][0]["known"]))
check("switch: same known and unknown identifier is an error", bool(run(root, c, dynamic=False)["errors"]))

main_res = run(root, load("good"), dynamic=False)
for name, change, hatch in [
        ("route removed", lambda c: c["routes"].pop(1), "route_removed:POST /signup"),
        ("source narrowed", lambda c: c.update(source=["src/x"]), "source_narrowed")]:
    put(root, "src/x/empty.js", "")
    c = hatch_cfg(change)
    r = run(root, c, dynamic=False)
    rc, out = panel(dict(r, findings=[]), main_res)
    check(f"switch: {name} against main fails", rc == 1 and hatch in out, out)
    c["exceptions"] = [exception(hatch)]
    r = run(root, c, dynamic=False)
    rc, out = panel(dict(r, findings=[]), main_res)
    check(f"switch: {name} with exception passes", rc == 0, out)
rc, out = panel(dict(main_res, d1_calls=[]), dict(main_res, d1_calls=["saveCode"]))
check("switch: a D1 helper removed against main fails", rc == 1 and "d1_calls_removed:saveCode" in out, out)
rc, out = panel(dict(main_res, mode="baseline"), main_res)
check("switch: absolute -> baseline against main fails", rc == 1 and "mode:baseline" in out, out)
gone = run(tempfile.mkdtemp(), {}, declared=False)
rc, out = panel(gone, main_res)
check("switch: the declaration deleted with its routes fails", rc == 1 and "route_removed" in out, out)

# --- A1: password policy ------------------------------------------------------------
root = copy_case("good")
cfg = load("good")
cfg["methods"] = ["whatsapp_code", "password"]
check("A1: password without exception fails", "A1:password" in ids(run(root, cfg, dynamic=False)))
exc = {"reason": "legacy", "approved_by": "security", "date": "2026-11-25"}
for algo, params, expect in [
        ("argon2id", {"memory_kib": 19456, "iterations": 2, "parallelism": 1}, False),
        ("argon2id", {"memory_kib": 4096, "iterations": 2, "parallelism": 1}, True),
        ("argon2id", {"memory_kib": 19456, "iterations": 1, "parallelism": 1}, True),
        ("scrypt", {"N": 2 ** 17, "r": 8, "p": 1}, False),
        ("scrypt", {"N": 2 ** 14, "r": 8, "p": 1}, True),
        ("bcrypt", {"cost": 12, "legacy": True, "rejects_over_72_bytes": True}, False),
        ("bcrypt", {"cost": 10, "legacy": True, "rejects_over_72_bytes": True}, True),
        ("bcrypt", {"cost": 12, "rejects_over_72_bytes": True}, True),
        ("pbkdf2-sha256", {"iterations": 600000, "fips": True}, False),
        ("pbkdf2-sha256", {"iterations": 100000, "fips": True}, True),
        ("md5", {}, True)]:
    c = copy.deepcopy(cfg)
    c["password_exception"] = dict(exc, algorithm=algo, params=params)
    got = [i for i in ids(run(root, c, dynamic=False)) if i.startswith("A1:params") or i == "A1:exception"]
    check(f"A1: {algo} {params} -> {'rejected' if expect else 'accepted'}", bool(got) == expect, got)
c = copy.deepcopy(cfg)
c["password_exception"] = {"algorithm": "argon2id"}
check("A1: exception without reason, approver and date", "A1:exception" in ids(run(root, c, dynamic=False)))

# --- A2: the binding must exist in wrangler ----------------------------------------
root = copy_case("good")
os.remove(os.path.join(root, "wrangler.toml"))
check("A2: no ratelimits binding in wrangler", "A2:binding" in ids(run(root, dynamic=False)))

# --- A7: baseline ------------------------------------------------------------------
root = copy_case("bad")
cfg = load("bad")
cfg["exceptions"] = [{"hatch": h, "reason": "r", "approved_by": "security", "date": "2026-11-25"}
                     for h in ("password_exception", "mode:baseline")]
static_ids = sorted(ids(run(root, cfg, dynamic=False)))
assert static_ids, "bad example must have static findings"
cfg["mode"] = "baseline"
cfg["baseline"] = static_ids
r = run(root, cfg, dynamic=False)
check("baseline: listed findings are tolerated", r["findings"] == [] and {f["id"] for f in r["tolerated"]} == set(static_ids), r["findings"])
cfg["baseline"] = static_ids[1:]
r = run(root, cfg, dynamic=False)
check("baseline: a finding outside the list fails", len(r["findings"]) == 1, r["findings"])
cfg["baseline"] = static_ids
r = run(root, cfg, dynamic=False)
rc, out = panel(r, dict(r, tolerated=r["tolerated"][1:], baseline=r["baseline"][1:]))
check("baseline: it grew against main -> fails", rc == 1 and "new in the baseline" in out, out)
rc, out = panel(r, r)
check("baseline: same as main -> 🟡, exit 0", rc == 0 and "🟡" in out and "🔴" not in out, out)
rc, out = panel(dict(r, tolerated=r["tolerated"][1:], baseline=r["baseline"][1:]), r)
check("baseline: shrank against main -> exit 0", rc == 0, out)

# --- baseline needs its exception ----------------------------------------------------
without = dict(cfg, exceptions=[e for e in cfg["exceptions"] if e["hatch"] != "mode:baseline"])
r = run(root, without, dynamic=False)
check("baseline: without the mode:baseline exception it fails", "A0:escape:mode:baseline" in ids(r), ids(r))
cfg["baseline"] = static_ids + ["A0:escape:auth-dynamic:false"]
r = ac.check(cfg, root, True, False, "someone/old-app")
check("switch: the baseline never tolerates a switch", "A0:escape:auth-dynamic:false" in ids(r), ids(r))
r = run(root, cfg, dynamic=False)
rc, out = panel(dict(r, findings=[]), dict(r, mode="absolute"))
check("switch: absolute -> baseline fails even with the exception", rc == 1 and "mode:baseline" in out, out)
check("the kit keeps no list of repositories", not hasattr(ac, "BASELINE_REPOS"))
check("the template starts absolute",
      json.load(open(os.path.join(ROOT, "templates", "auth.json")))["mode"] == "absolute")

def exception_of(h):
    return {"hatch": h, "reason": "r", "approved_by": "security", "date": "2026-11-25"}


# --- what must not fail, and what must not slip through -----------------------------
for name, body in [("FormData", "body: form({ secret: env.TURNSTILE_SECRET, response: token }) });"),
                   ("JSON", 'headers: { "content-type": "application/json" }, body: JSON.stringify({ secret: env.TURNSTILE_SECRET, response: token }) });')]:
    app = copy_case("good")
    edit(app, "src/worker.js", "body: new URLSearchParams({ secret: env.TURNSTILE_SECRET, response: token }) });", body)
    if name == "FormData":
        edit(app, "src/worker.js", "async function verifyTurnstile",
             "const form = (o) => { const f = new FormData(); for (const [k, v] of Object.entries(o)) f.append(k, v); return f; };\n\nasync function verifyTurnstile")
    r = run(app)
    check(f"siteverify called with {name} passes", r["findings"] == [] and r["errors"] == [], r["findings"] + r["errors"])

# A server that refuses with the same answer whatever the token says proves nothing.
app = copy_case("good")
edit(app, "src/worker.js", "j.success === true && j.hostname === hostname && j.action === action", "j.success === true")
edit(app, "src/worker.js", 'return json({ ok: true, reason_text: "challenge" });\n}\n\nasync function signup',
     'return json({ ok: false, reason_text: "wrong code" }, 401);\n}\n\nasync function signup')
found = ids(run(app))
check("A3: the same 401 for a valid token and a wrong hostname fails",
      {"A3:POST /login/start:hostname", "A3:POST /login/start:action"} <= found, found)

app = copy_case("good")
edit(app, "src/worker.js", "async function start(request, env) {", "async function start(request: Request, { ...env }: { DB: any }): Promise<{ ok: boolean }> {")
r = run(app, dynamic=False)
check("handler with a destructured parameter and an object return type is read whole", r["findings"] == [], r["findings"])

app = copy_case("good")
w = open(os.path.join(app, "wrangler.toml")).read()
put(app, "wrangler.toml", w.replace("[[ratelimits]]", "[[env.production.ratelimits]]"))
check("A2: a binding declared under an environment counts",
      "[[env.production.ratelimits]]" in open(os.path.join(app, "wrangler.toml")).read()
      and "A2:binding" not in ids(run(app, dynamic=False)))

app = copy_case("good")
put(app, "src/env.ts", "export interface Env { TURNSTILE_VERIFY_URL?: string }\n")
r = run(app, dynamic=False)
check("TURNSTILE_VERIFY_URL in a type declaration passes", r["findings"] == [], r["findings"])
app = copy_case("good")
put(app, "src/entry.js", "export const url = (env) => siteverifyUrl(env.TURNSTILE_VERIFY_URL);\n")
r = run(app, dynamic=False)
check("TURNSTILE_VERIFY_URL checked in the file that calls siteverify passes", r["findings"] == [], r["findings"])

app = copy_case("good")
edit(app, "src/worker.js", "async function start(request, env) {",
     'async function start(request, env) {\n  console.log("invalid code (retry)", 1);')
r = run(app, dynamic=False)
check("A6: fixed words in console pass", r["findings"] == [], r["findings"])
app = copy_case("good")
edit(app, "src/worker.js", "async function start(request, env) {",
     'async function start(request, env) {\n  console.log(ok ? "phone invalid" : "code sent", "phone invalid");')
r = run(app, dynamic=False)
check("A6: loose text that names phone passes", r["findings"] == [], r["findings"])
for what, line in [("a header", 'console.log("from", request.headers.get("cf-connecting-ip"));'),
                   ("a template", "console.log(`for ${phone}`);"),
                   ("a quoted key", 'console.log(JSON.stringify({"phone": x}));'),
                   ("a quoted key after another", "console.log({ n: 1, 'telefone': t });")]:
    app = copy_case("good")
    edit(app, "src/worker.js", "async function start(request, env) {", "async function start(request, env) {\n  " + line)
    check(f"A6: console with {what} fails", "A6:console:POST /login/start" in ids(run(app, dynamic=False)))

app = copy_case("good")
put(app, "src/check.js", 'export const check = async () => (await (await fetch("https://challenges.cloudflare.com/turnstile/v0/siteverify")).json()).ok;\n')
check("a siteverify call that never reads success fails",
      "A3:verify:success" in ids(run(app, dynamic=False)))

GUARD = 'u.protocol === "http:" && (u.hostname === "127.0.0.1" || u.hostname === "localhost")'
for what, guard, passes in [
        ("a default address on localhost is not a check", "true", False),
        ("a list of local hosts is a check", '["127.0.0.1", "localhost"].includes(u.hostname)', True),
        ("a pattern tested on the origin is a check", r"/^http:\/\/(127\.0\.0\.1|localhost)(:\d+)?$/.test(u.origin)", True)]:
    app = copy_case("good")
    edit(app, "src/worker.js", GUARD, guard)
    edit(app, "src/worker.js", "function siteverifyUrl", "const DEV = 'http://localhost:8932';\n\nfunction siteverifyUrl")
    found = ids(run(app, dynamic=False))
    check(f"verify URL: {what}", ("A3:verify-url-guard:src/worker.js" not in found) == passes, found)

for what, line in [("shorthand properties", 'env.obs.log.info("login_attempt", { result: "ok", phone, ip });'),
                   ("one object", 'log({event: "login_attempt", phone, ip});'),
                   ("keys before the name", 'log({ phone, event: "login_attempt" });')]:
    app = copy_case("good")
    edit(app, "src/worker.js", "async function start(request, env) {", "async function start(request, env) {\n  " + line)
    found = ids(run(app, dynamic=False))
    check(f"A6: event with {what} fails", any(i.startswith("A6:event:POST /login/start") for i in found), found)

app = copy_case("good")
os.symlink(os.path.join(app, "gone.js"), os.path.join(app, "src", "broken.js"))
check("a broken link in the source does not stop the check", run(app, dynamic=False)["findings"] == [])

for what, change in [("a route that is not an object", {"routes": ["POST /x"]}),
                     ("a number written as text", {"password_exception": {"algorithm": "scrypt", "params": {"N": "131072"}}}),
                     ("a list written as text", {"source": "src"}),
                     ("a server without start", {"server": {"url": "x"}})]:
    r = run(app, dict(load("good"), **change), dynamic=False)
    rc, out = panel(r)
    check(f"config: {what} is an error in the panel", r["errors"] != [] and rc == 1 and "🔴" in out, r["errors"])
r = run(app, ["not an object"], dynamic=False)
check("config: a file that is not an object is an error", r["errors"] != [], r)

# A baseline never covers a new occurrence.
app = copy_case("bad")
once = ids(run(app, load("bad"), dynamic=False))
open(os.path.join(app, "src", "password.js"), "a").write(
    'export const again = (password) => createHash("md5").update(password).digest("hex");\n')
added = ids(run(app, load("bad"), dynamic=False)) - once
check("a second weak hash in the same file has its own id", added == {"A1:weakhash:src/password.js#2"}, added)

site = tempfile.mkdtemp()
put(site, "a.js", 'fetch("/register", { method: "POST" });\n')
cfg = {"login": False, "ignore_paths": ["/register"],
       "exceptions": [exception_of(h) for h in ("login:false", "ignore_paths:/register")]}
r = run(site, cfg, dynamic=False)
check("login: false with the path in ignore_paths passes", r["findings"] == [], r["findings"])

app = copy_case("good")
put(app, "src/scripts/server.js", 'export default { fetch: (r) => new URL(r.url).pathname === "/signin" };\n')
check("a server under scripts/ is read", "A0:route:/signin" in ids(run(app, dynamic=False)))
app = copy_case("good")
put(app, "src/scripts/seed.js", 'await fetch(base + "/signin");\n')
check("a maintenance script is not the app", run(app, dynamic=False)["findings"] == [])
c = load("good")
c["source"] = ["srcc"]
check("config: a source folder that does not exist is an error",
      any("srcc" in e for e in run(app, c, dynamic=False)["errors"]))

app = copy_case("good")
edit(app, "src/worker.js", "async function start(request, env) {",
     'async function start(request, env) {\n  if (request.headers.get("cf-connecting-ip")) process.exit(1);')
r = run(app)
check("a server that dies during the checks is an error in the panel, not a crash",
      any("stopped answering" in e or "stopped" in e for e in r["errors"]), r["errors"])
rc, out = panel(r)
check("server died: panel red, exit 1", rc == 1 and "🔴" in out, out)

# --- code written in Portuguese is detected too ---------------------------------------
pt = tempfile.mkdtemp()
open(os.path.join(pt, "a.js"), "w").write('db.prepare("SELECT 1 FROM codigos_login");\n')
check("a Portuguese login-code table is a login", ids(run(pt, {}, declared=False)) == {"A0:undeclared"})

# --- the policy is the policy: Cloudflare's public test secrets ----------------------
check("test secrets are Cloudflare's public ones",
      ac.TURNSTILE_TEST == {"pass": "1x0000000000000000000000000000000AA", "fail": "2x0000000000000000000000000000000AA",
                            "spent": "3x0000000000000000000000000000000AA"})

sys.exit(1 if fails else 0)
