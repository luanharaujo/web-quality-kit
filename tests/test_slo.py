#!/usr/bin/env python3
"""Proof of slo.py: a good slo.json passes and the usual mistakes are caught;
the alert warns the owner on errors above 5% over the last 5 minutes, never on a lone
error or a quiet night, escalates only when severe, and warns once per
incident."""
import datetime as dt
import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
S = os.path.join(ROOT, "scripts")
sys.path.insert(0, S)
import slo  # noqa: E402

fails = 0


def check(name, cond, detail=""):
    global fails
    print(("ok   " if cond else "FAIL ") + name + ("" if cond else f"  {detail}"))
    fails += 0 if cond else 1


GOOD = json.load(open(os.path.join(ROOT, "templates", "slo.json")))
NOW = dt.datetime(2026, 11, 25, 21, 0, tzinfo=dt.timezone.utc)


def iso(t):
    return t.isoformat().replace("+00:00", "Z")


def data(per_minute, minutes=60, at=NOW, window=(100000, 100)):
    """per_minute(i) → (requests, errors, p95) for minute i before now (0 = latest)."""
    items = []
    for i in range(minutes):
        r, e, p = per_minute(i)
        items.append({"t": iso(at - dt.timedelta(minutes=i)), "requests": r, "errors": e, "p95_ms": p})
    return {"now": iso(at), "minutes": items, "window": {"requests": window[0], "errors": window[1]}}


def ev(d, state=None, s=GOOD):
    return slo.evaluate(s, d, state)


# --- validate ---------------------------------------------------------------------
check("template slo.json is valid", slo.validate(GOOD) == [], slo.validate(GOOD))
check("minimal slo.json (defaults) is valid",
      slo.validate({"service": "x", "owner": "api-team", "availability_pct": 99.5, "latency_p95_ms": 500}) == [])
cases = {
    "no owner": ({**GOOD, "owner": ""}, "owner"),
    "owner not a handle (an e-mail)": ({**GOOD, "owner": "someone@example.com"}, "owner"),
    "availability 100% (no budget)": ({**GOOD, "availability_pct": 100}, "availability_pct"),
    "availability as text": ({**GOOD, "availability_pct": "99.5"}, "availability_pct"),
    "latency missing": ({k: v for k, v in GOOD.items() if k != "latency_p95_ms"}, "latency_p95_ms"),
    "unknown field (typo)": ({**GOOD, "latency_p99": 900}, "unknown field"),
    "alert window of 0 minutes": ({**GOOD, "alert": {"minutes": 0}}, "alert.minutes"),
    "alert window longer than the closing window": ({**GOOD, "alert": {"minutes": 45}}, "alert.minutes"),
    "severe not above the normal limit": ({**GOOD, "alert": {"error_pct": 20, "severe_error_pct": 10}}, "severe_error_pct"),
    "alert below what the target allows": ({**GOOD, "availability_pct": 95, "alert": {"error_pct": 1}}, "warn all the time"),
}
for name, (bad, word) in cases.items():
    errs = slo.validate(bad)
    check(f"invalid: {name}", any(word in e for e in errs), errs)

d = tempfile.mkdtemp()
open(os.path.join(d, "slo.json"), "w").write("{not json")
out = json.loads(subprocess.run([sys.executable, os.path.join(S, "slo.py"), "validate", "--root", d],
                                capture_output=True, text=True).stdout)
check("validate CLI: broken JSON is an error", out["errors"] != [], out)
out = json.loads(subprocess.run([sys.executable, os.path.join(S, "slo.py"), "validate", "--root", d, "--file", "none.json"],
                                capture_output=True, text=True).stdout)
check("validate CLI: missing file is an error", out["errors"] != [], out)

# --- alert --------------------------------------------------------------------------
calm = data(lambda i: (100, 0, 120))
r = ev(calm)
check("calm traffic: ok, nobody told", r["level"] == "ok" and r["notify"] == "none", r)

lone = data(lambda i: (100, 1 if i == 0 else 0, 120))
check("a lone error: nobody told", ev(lone)["notify"] == "none", ev(lone))

quiet = data(lambda i: (2, 1 if i < 5 else 0, 120))
check("quiet night (10 requests, 5 errors): nobody told", ev(quiet)["notify"] == "none", ev(quiet))

spike = data(lambda i: (100, 30 if i == 0 else 0, 120))
check("one bad minute inside 5 (6%) still counts as the 5-minute share", ev(spike)["notify"] == "owner", ev(spike))

incident = data(lambda i: (100, 8 if i < 5 else 0, 120))
r = ev(incident)
check("errors 8% for 5 min: owner told, no escalation", r["level"] == "owner" and r["notify"] == "owner", r)
check("reason says the share and the window", "8.0%" in r["reasons"][0] and "5 min" in r["reasons"][0], r["reasons"])

state = r["state"]
r2 = ev(data(lambda i: (100, 8 if i < 5 else 0, 120), at=NOW + dt.timedelta(minutes=5)), state)
check("same incident 5 min later: not repeated", r2["notify"] == "none" and r2["state"]["open"], r2)

severe = ev(data(lambda i: (100, 30 if i < 5 else 0, 120), at=NOW + dt.timedelta(minutes=10)), r2["state"])
check("incident turns severe (30%): escalated once", severe["level"] == "severe" and severe["notify"] == "owner+escalate", severe)
again = ev(data(lambda i: (100, 30 if i < 5 else 0, 120), at=NOW + dt.timedelta(minutes=15)), severe["state"])
check("still severe: not repeated", again["notify"] == "none", again)

st = again["state"]
for k in range(1, 8):
    st = ev(data(lambda i: (100, 0, 120), at=NOW + dt.timedelta(minutes=15 + 5 * k)), st)["state"]
check("30 minutes of fine numbers close the incident", st == {}, st)
recent_bad = data(lambda i: (100, 30 if 10 <= i < 15 else 0, 120))
r = ev(recent_bad, {"open": True, "level": "owner", "since": iso(NOW)})
check("errors 10 minutes ago: calm now, but the incident stays open", r["level"] == "ok" and r["state"].get("open"), r)
r = ev(incident, r["state"])
check("relapse while open: same incident, not repeated", r["notify"] == "none", r)
r = ev(incident, st)
check("after closing, a new incident warns again", r["notify"] == "owner", r)

direct = ev(data(lambda i: (100, 50 if i < 5 else 0, 120)))
check("first look already severe: owner and escalation", direct["notify"] == "owner+escalate", direct)

burn = ev(data(lambda i: (100, 6 if i < 60 else 0, 120)))
check("6% for a whole hour (burn 12x, under 14.4): owner only", burn["level"] == "owner", burn)
burn2 = ev(data(lambda i: (100, 8 if i < 60 else 0, 120)))
check("8% for a whole hour: budget burning 16x = severe", burn2["level"] == "severe" and any("burning" in x for x in burn2["reasons"]), burn2)

strict = {**GOOD, "availability_pct": 99.9}
fast = ev(data(lambda i: (100, 4.9, 120)), s=strict)
check("99.9% target, 4.9% errors for an hour (burn 49x, under 5%): severe on its own",
      fast["level"] == "severe" and fast["notify"] == "owner+escalate", fast)
stb, sent_notes = {}, []
for k in range(0, 24):
    rk = ev(data(lambda i: (50, 0 if (i + 5 * k) % 7 == 0 else 1, 120), at=NOW + dt.timedelta(minutes=5 * k)), stb, s=strict)
    stb = rk["state"]
    if rk["notify"] != "none":
        sent_notes.append(rk["notify"])
check("99.9% target, about 1.8% errors for 2 hours (5-minute windows of 4 or 5 errors): one warning, no close-and-reopen loop", sent_notes == ["owner+escalate"],
      sent_notes)
lone_strict = ev(data(lambda i: (4, 1 if i == 0 else 0, 120)), s=strict)
check("99.9% target, 20 requests and 1 error: a lone error never pages", lone_strict["notify"] == "none", lone_strict)
healed = ev(data(lambda i: (100, 8 if i >= 10 else 0, 120)), s=strict)
check("a burning hour that already recovered does not page", healed["notify"] == "none", healed)

spent = ev(incident | {"window": {"requests": 1000, "errors": 6}})
check("budget of the window spent + incident = severe", spent["level"] == "severe", spent)
spent_calm = ev(calm | {"window": {"requests": 1000, "errors": 6}})
check("budget spent but calm now: no page", spent_calm["notify"] == "none", spent_calm)

slow = ev(data(lambda i: (100, 0, 900 if i < 5 else 100)))
check("p95 900 ms for 5 min (target 500): owner told", slow["notify"] == "owner" and "p95" in slow["reasons"][0], slow)
blip = ev(data(lambda i: (100, 0, 900 if i == 0 else 100)))
check("one slow minute: nobody told", blip["notify"] == "none", blip)

# minutes labelled by their start, evaluated 30 s after the last complete one
anchored = data(lambda i: (100, 0, 900 if i < 5 else 100), at=NOW - dt.timedelta(minutes=1))
anchored["now"] = iso(NOW + dt.timedelta(seconds=30))
r = ev(anchored)
check("window ends at the last complete minute: slow p95 still warns at hh:mm:30", r["notify"] == "owner", r)
anchored_err = data(lambda i: (100, 8 if i < 5 else 0, 120), at=NOW - dt.timedelta(minutes=1))
anchored_err["now"] = iso(NOW)
check("window ends at the last complete minute: 5 minutes of errors at hh:mm:00", ev(anchored_err)["numbers"]["requests"] == 500,
      ev(anchored_err)["numbers"])

old = data(lambda i: (100, 30 if i < 5 else 0, 120), minutes=5)
old["now"] = iso(NOW + dt.timedelta(hours=24))
open_state = {"open": True, "level": "severe", "since": iso(NOW)}
r_old = ev(old, open_state)
check("numbers from a day ago: no_data, nothing decided, the incident stays open",
      r_old["level"] == "no_data" and r_old["notify"] == "none" and r_old["state"] == open_state, r_old)
empty = ev({"now": iso(NOW), "minutes": [], "window": {"requests": 0, "errors": 0}}, open_state)
check("no minutes at all (collection failed): no_data, the incident does not close", empty["level"] == "no_data"
      and empty["state"] == open_state, empty)
st = {"open": True, "level": "owner", "since": iso(NOW)}
gap = ev({"now": iso(NOW + dt.timedelta(minutes=5)), "minutes": []}, st)
check("no_data never closes an incident", gap["state"] == st, gap)
slow_quiet = ev(data(lambda i: (3, 0, 5000)), {"open": True, "level": "owner", "since": iso(NOW)})
check("still slow on low traffic (p95 5000 ms, no errors): the incident stays open", slow_quiet["level"] == "ok"
      and slow_quiet["state"].get("open"), slow_quiet)
still_slow = ev(data(lambda i: (100, 0, 900 if 10 <= i < 15 else 100)), {"open": True, "level": "owner", "since": iso(NOW)})
check("5 slow minutes in a row 10 minutes ago: the incident stays open", still_slow["state"].get("open"), still_slow)
spikes = ev(data(lambda i: (100, 0, 900 if i % 20 == 7 else 100)), {"open": True, "level": "severe", "since": iso(NOW)})
check("a lone slow minute every 20 (cold start) does not keep it open", spikes["state"] == {}, spikes)
silent = ev(data(lambda i: (0, 0, None)), {"open": True, "level": "owner", "since": iso(NOW)})
check("30 minutes without any traffic do not close it either", silent["state"].get("open"), silent)
future = data(lambda i: (100, 100 if i < 2 else 0, 120))
future["complete_until"] = iso(NOW + dt.timedelta(hours=2))
check("complete_until in the future: no_data, never ok", ev(future)["level"] == "no_data", ev(future))
open_owner = {"open": True, "level": "owner", "since": iso(NOW)}
st2, levels = dict(open_owner), set()
for k in range(0, 8):
    r2 = ev(data(lambda i: (3, 3 if i < 5 else 0, 120), at=NOW + dt.timedelta(minutes=5 * k)), st2)
    st2 = r2["state"]
    levels.add(r2["level"])
check("still failing on low traffic (15 of 15 errors, under min_requests): level ok, the incident stays open",
      levels == {"ok"} and st2.get("open") is True, (levels, st2))
naive = data(lambda i: (100, 0, 120))
for m in naive["minutes"]:
    m["t"] = m["t"].replace("Z", "")
check("minute times without a zone are taken as UTC", ev(naive)["level"] == "ok", ev(naive))
late = data(lambda i: (100, 0, 9000), minutes=60)
late["now"] = iso(NOW + dt.timedelta(minutes=3))
check("numbers 3 minutes late: the slow p95 still warns", ev(late)["notify"] == "owner", ev(late))
quiet_known = {"now": iso(NOW), "complete_until": iso(NOW), "minutes": [], "window": {"requests": 0, "errors": 0}}
check("complete_until with no minutes = no traffic: ok", ev(quiet_known)["level"] == "ok", ev(quiet_known))

# --- alert CLI ------------------------------------------------------------------------
t = tempfile.mkdtemp()
for name, obj in {"slo.json": GOOD, "d.json": incident}.items():
    json.dump(obj, open(os.path.join(t, name), "w"))
p = subprocess.run([sys.executable, os.path.join(S, "slo.py"), "alert", "--slo", os.path.join(t, "slo.json"),
                    "--data", os.path.join(t, "d.json"), "--state", os.path.join(t, "none.json")], capture_output=True, text=True)
check("alert CLI: decision on stdout", p.returncode == 0 and json.loads(p.stdout)["notify"] == "owner", p.stdout + p.stderr)
json.dump({**GOOD, "owner": ""}, open(os.path.join(t, "slo.json"), "w"))
p = subprocess.run([sys.executable, os.path.join(S, "slo.py"), "alert", "--slo", os.path.join(t, "slo.json"),
                    "--data", os.path.join(t, "d.json")], capture_output=True, text=True)
check("alert CLI: invalid slo.json exits 2", p.returncode == 2, p.stdout)

json.dump(GOOD, open(os.path.join(t, "slo.json"), "w"))
for label, bad in (("no now", {"minutes": []}), ("minutes is null", {"now": iso(NOW), "minutes": None}),
                   ("a minute without requests", {"now": iso(NOW), "minutes": [{"t": iso(NOW), "errors": 0}]}),
                   ("a list instead of an object", [])):
    json.dump(bad, open(os.path.join(t, "d.json"), "w"))
    p = subprocess.run([sys.executable, os.path.join(S, "slo.py"), "alert", "--slo", os.path.join(t, "slo.json"),
                        "--data", os.path.join(t, "d.json")], capture_output=True, text=True)
    check(f"alert CLI: numbers with {label} exit 3 with a JSON error, no traceback",
          p.returncode == 3 and json.loads(p.stdout)["error"] == "invalid numbers" and "Traceback" not in p.stderr,
          p.stdout + p.stderr)
big = {"now": iso(NOW), "minutes": [{"t": iso(NOW), "requests": 1, "errors": 0}] * 10000 + [{}]}
json.dump(big, open(os.path.join(t, "d.json"), "w"))
p = subprocess.run([sys.executable, os.path.join(S, "slo.py"), "alert", "--slo", os.path.join(t, "slo.json"),
                    "--data", os.path.join(t, "d.json")], capture_output=True, text=True)
check("alert CLI: too many minutes exit 3, no traceback", p.returncode == 3 and "Traceback" not in p.stderr, p.stdout[:300] + p.stderr[-300:])
json.dump(incident, open(os.path.join(t, "d.json"), "w"))
for label, text in (("empty", ""), ("cut", '{"open": tr'), ("a list", "[]")):
    open(os.path.join(t, "st.json"), "w").write(text)
    p = subprocess.run([sys.executable, os.path.join(S, "slo.py"), "alert", "--slo", os.path.join(t, "slo.json"),
                        "--data", os.path.join(t, "d.json"), "--state", os.path.join(t, "st.json")], capture_output=True, text=True)
    ok = p.returncode == 0 and "Traceback" not in p.stderr
    out = json.loads(p.stdout) if ok else {}
    check(f"alert CLI: a state file that is {label} starts over and warns (the alert never goes mute)",
          ok and out.get("notify") == "owner" and out.get("warnings"), p.stdout + p.stderr)
open(os.path.join(t, "d.json"), "w").write("{not json")
p = subprocess.run([sys.executable, os.path.join(S, "slo.py"), "alert", "--slo", os.path.join(t, "slo.json"),
                    "--data", os.path.join(t, "d.json")], capture_output=True, text=True)
check("alert CLI: a broken numbers file exits 3", p.returncode == 3 and "Traceback" not in p.stderr, p.stdout + p.stderr)

print("slo: " + ("ok" if not fails else f"{fails} failed"))
sys.exit(1 if fails else 0)
