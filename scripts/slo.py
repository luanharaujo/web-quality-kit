#!/usr/bin/env python3
"""Service level objectives of a server: the written target and the alert on
the error budget.

  slo.py validate [--file slo.json] [--root .] > slo_check.json
  slo.py alert --slo slo.json --data numbers.json [--state state.json] > decision.json

slo.json (one per server, templates/slo.json):
  service           name of the service (the Worker's name)
  owner             who gets the warning: a short handle of the service's
                    owner (letters, digits, dot, dash, underscore, e.g. "api-team")
  availability_pct  share of requests without a server error (5xx) over the
                    window, e.g. 99.5; between 90 and 99.99
  latency_p95_ms    95% of the requests answer within this, e.g. 500
  window_days       the window of the target (default 30)
  alert             when to warn (defaults below):
    error_pct         errors above this share ...           (5)
    minutes           ... of the requests of the last N minutes (5);
                      p95 above the target must hold in every one of them
    min_requests      ... with at least this many requests in those minutes (20)
    min_errors        ... and at least this many errors in them (5): one error
                      on a quiet night is never an incident, whatever the target
    severe_error_pct  errors above this share = severe, escalated beyond the owner (20)
    severe_burn       or the budget burning this many times faster than the
                      target allows over the last hour and over the last
                      minutes (14.4: 2% of a 30-day budget in one hour); this
                      opens a severe incident even below error_pct

alert is pure (no network, no token): who runs it fetches the numbers and
passes them in, and keeps the state file between runs.

numbers.json:
  now       ISO time of the evaluation
  minutes   [{"t": ISO start of the minute, "requests": n, "errors": n5xx, "p95_ms": x}]
            the last 60 minutes or more, one item per complete minute
            (missing before complete_until = no traffic)
  complete_until  optional ISO time up to which the numbers are known
            (default: end of the latest minute); the windows end there.
            Older than alert.max_delay_min (15) before now, in the future, or
            no minutes at all = level "no_data": nothing decided, nobody told
            (notify "none"), an open incident stays open. Watching that the
            numbers keep coming is the caller's job.
  window    {"requests": n, "errors": n} over window_days (budget used so far)

Decision (stdout): {"level": "ok"|"owner"|"severe"|"no_data", "notify": "none"|"owner"|"owner+escalate",
"reasons": [...], "numbers": {...}, "state": {...}}. One warning per incident:
while an incident is open nothing is repeated, except the first time it turns
severe; it closes when the last 30 minutes of numbers look fine (see
looks_fine), measured on the numbers, not on the clock between runs.
Exit 2: slo.json invalid; exit 3: numbers file unreadable or malformed
({"error": "invalid numbers", "problems": [...]}).
"""
import argparse
import datetime as dt
import json
import os
import re
import sys

DEFAULT_ALERT = {"error_pct": 5.0, "minutes": 5, "min_requests": 20, "min_errors": 5, "severe_error_pct": 20.0,
                 "severe_burn": 14.4, "max_delay_min": 15}
CLOSE_AFTER_MIN = 30
OWNER = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{1,39}$")
FIELDS = {"service", "owner", "availability_pct", "latency_p95_ms", "window_days", "alert"}


def number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def validate(slo):
    """List of problems (empty = valid)."""
    err = []
    if not isinstance(slo, dict):
        return ["slo.json must be a JSON object"]
    for k in sorted(set(slo) - FIELDS):
        err.append(f"unknown field {k!r}")
    if not isinstance(slo.get("service"), str) or not slo["service"].strip():
        err.append("service: the name of the service is required")
    if not isinstance(slo.get("owner"), str) or not OWNER.match(slo["owner"]):
        err.append("owner: a short handle of the owner is required (e.g. \"api-team\")")
    a = slo.get("availability_pct")
    if not number(a) or not 90 <= a <= 99.99:
        err.append("availability_pct: a number between 90 and 99.99 is required (e.g. 99.5)")
    p = slo.get("latency_p95_ms")
    if not number(p) or not 10 <= p <= 30000:
        err.append("latency_p95_ms: a number of milliseconds between 10 and 30000 is required (e.g. 500)")
    w = slo.get("window_days", 30)
    if not isinstance(w, int) or isinstance(w, bool) or not 1 <= w <= 90:
        err.append("window_days: whole days between 1 and 90")
    al = slo.get("alert", {})
    if not isinstance(al, dict):
        err.append("alert: an object is required")
        al = {}
    for k in sorted(set(al) - set(DEFAULT_ALERT)):
        err.append(f"alert: unknown field {k!r}")
    cfg = {**DEFAULT_ALERT, **al}
    for k, v in cfg.items():
        if not number(v) or v <= 0:
            err.append(f"alert.{k}: a positive number is required")
    if not err:
        if not 1 <= cfg["minutes"] <= CLOSE_AFTER_MIN:
            err.append(f"alert.minutes: between 1 and {CLOSE_AFTER_MIN} (the window that closes an incident)")
        if cfg["error_pct"] >= cfg["severe_error_pct"]:
            err.append("alert.severe_error_pct must be above alert.error_pct")
        if number(a) and cfg["error_pct"] < 100 - a:
            err.append("alert.error_pct below the error the target already allows: it would warn all the time")
    return err


def parse_time(s):
    t = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)  # no zone given = UTC


def burning(window, slo, cfg):
    """The budget still burning at severe_burn or more over the window (with
    min_errors): an incident opened by a fast burn stays open while it lasts."""
    _, req, errs = window
    allowed = 1 - slo["availability_pct"] / 100
    return bool(req) and errs >= cfg["min_errors"] and (errs / req) / allowed >= cfg["severe_burn"]


def looks_fine(window, slo, cfg):
    """The last 30 minutes of numbers close an incident only if they look
    fine: some traffic, errors within error_pct in total, and no stretch of
    alert.minutes that would have warned (above error_pct with at least
    min_errors, or every minute with traffic above the p95 target, as when
    opening). Minutes without traffic count neither way, so a quiet night
    keeps the incident open until real requests prove it over."""
    items, req, errs = window
    if not req or 100 * errs / req > cfg["error_pct"]:
        return False
    # nor any stretch of alert.minutes inside them that would have warned:
    # errors (as when opening) or every minute with traffic slow (a lone slow
    # minute, like a cold start, does not keep an incident open)
    span = dt.timedelta(minutes=cfg["minutes"])
    times = sorted((parse_time(m["t"]), m) for m in items)
    for start, _ in times:
        part = [m for t, m in times if start <= t < start + span]
        r, e = sum(m["requests"] for m in part), sum(m["errors"] for m in part)
        if r and e >= cfg["min_errors"] and 100 * e / r > cfg["error_pct"]:
            return False
        busy = [m for m in part if m["requests"] > 0 and m.get("p95_ms") is not None]
        if len(busy) >= cfg["minutes"] and all(m["p95_ms"] > slo["latency_p95_ms"] for m in busy):
            return False
    return True


def data_problems(data):
    """What is wrong with the numbers file; empty when evaluate() can read it."""
    if not isinstance(data, dict):
        return ["the numbers must be a JSON object"]
    out = []

    def when(v, name):
        try:
            parse_time(v)
        except (TypeError, ValueError, AttributeError):
            out.append(f"{name}: not an ISO time")

    def count(v, name):
        if not number(v) or v < 0:
            out.append(f"{name}: not a count")

    when(data.get("now"), "now")
    if data.get("complete_until") is not None:
        when(data["complete_until"], "complete_until")
    minutes = data.get("minutes", [])
    if not isinstance(minutes, list):
        out.append("minutes: not a list")
        minutes = []
    for i, m in enumerate(minutes[:2000]):
        if not isinstance(m, dict):
            out.append(f"minutes[{i}]: not an object")
            continue
        when(m.get("t"), f"minutes[{i}].t")
        count(m.get("requests"), f"minutes[{i}].requests")
        count(m.get("errors"), f"minutes[{i}].errors")
        if m.get("p95_ms") is not None and not number(m["p95_ms"]):
            out.append(f"minutes[{i}].p95_ms: not a number")
    win = data.get("window")
    if win is not None:
        if not isinstance(win, dict):
            out.append("window: not an object")
        else:
            for k in ("requests", "errors"):
                if k in win:
                    count(win[k], f"window.{k}")
    return out[:20]


def evaluate(slo, data, state=None):
    state = dict(state or {})
    cfg = {**DEFAULT_ALERT, **slo.get("alert", {})}
    now = parse_time(data["now"])
    allowed = 1 - slo["availability_pct"] / 100  # error share the target allows

    # Each item is the minute that starts at t. The numbers are known up to
    # complete_until (default: the end of the latest minute present); the
    # windows end there, so a source that lags a few minutes still gives
    # full windows, and a minute missing before it means no traffic.
    seen = [parse_time(m["t"]) for m in data.get("minutes", []) if parse_time(m["t"]) <= now]
    if data.get("complete_until"):
        end = parse_time(data["complete_until"])
    elif seen:
        end = max(seen) + dt.timedelta(minutes=1)
    else:
        end = None
    if end is not None and end > now + dt.timedelta(minutes=1):
        end = None  # numbers "known" into the future: a clock is wrong, trust nothing
    if end is None or now - end > dt.timedelta(minutes=cfg["max_delay_min"]):
        # No numbers (collection failed or lags too much): nothing is decided
        # and an open incident stays open; no data is not "normal".
        since = "never" if end is None else end.isoformat()
        return {"service": slo.get("service"), "owner": slo.get("owner"), "level": "no_data", "notify": "none",
                "reasons": [f"no numbers since {since}"], "numbers": {}, "state": state}

    def last(minutes):
        start = end - dt.timedelta(minutes=minutes)
        items = [m for m in data.get("minutes", []) if start <= parse_time(m["t"]) < end]
        return items, sum(m["requests"] for m in items), sum(m["errors"] for m in items)

    recent, req, errs = last(cfg["minutes"])
    hour, hreq, herrs = last(60)
    err_pct = 100 * errs / req if req else 0.0
    burn = (herrs / hreq) / allowed if hreq else 0.0
    slow = [m for m in recent if m.get("p95_ms") is not None and m["requests"] > 0]
    win = data.get("window") or {}
    budget_used = (win.get("errors", 0) / win["requests"]) / allowed if win.get("requests") else 0.0
    numbers = {"minutes": cfg["minutes"], "requests": req, "errors": errs, "error_pct": round(err_pct, 2),
               "burn_1h": round(burn, 2), "budget_used_pct": round(100 * budget_used, 1)}

    reasons, level = [], "ok"
    busy = req >= cfg["min_requests"] and errs >= cfg["min_errors"]
    traffic = req >= cfg["min_requests"]
    if busy and err_pct > cfg["error_pct"]:
        level = "owner"
        reasons.append(f"errors {err_pct:.1f}% over the last {cfg['minutes']} min (limit {cfg['error_pct']}%)")
    if traffic and len(slow) >= cfg["minutes"] and all(m["p95_ms"] > slo["latency_p95_ms"] for m in slow):
        level = "owner"
        worst = max(m["p95_ms"] for m in slow)
        reasons.append(f"p95 above {slo['latency_p95_ms']} ms for {cfg['minutes']} min in a row (worst {worst:.0f} ms)")
    if busy and err_pct >= cfg["severe_error_pct"]:
        level = "severe"
        reasons.append(f"errors {err_pct:.1f}% (severe from {cfg['severe_error_pct']}%)")
    # Fast burn: over the last hour AND still over the last minutes (so an
    # hour that already recovered does not page). Opens an incident on its own.
    recent_burn = (errs / req) / allowed if req else 0.0
    if hreq >= cfg["min_requests"] and busy and burn >= cfg["severe_burn"] and recent_burn >= cfg["severe_burn"]:
        level = "severe"
        reasons.append(f"budget burning {burn:.1f}x faster than the target allows over the last hour")
    if budget_used >= 1 and level != "ok":
        level = "severe"
        reasons.append(f"the error budget of {slo.get('window_days', 30)} days is spent ({100 * budget_used:.0f}%)")

    notify = "none"
    rank = {"ok": 0, "owner": 1, "severe": 2}
    if level != "ok":
        if not state.get("open"):
            state.update(open=True, since=data["now"], level=level)
            notify = "owner+escalate" if level == "severe" else "owner"
        elif rank[level] > rank[state.get("level", "owner")]:
            state["level"] = level
            notify = "owner+escalate"
    elif state.get("open") and looks_fine(last(CLOSE_AFTER_MIN), slo, cfg) and not burning(last(60), slo, cfg):
        state = {}
    return {"service": slo.get("service"), "owner": slo.get("owner"), "level": level, "notify": notify,
            "reasons": reasons, "numbers": numbers, "state": state}


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser("validate")
    v.add_argument("--file", default="slo.json")
    v.add_argument("--root", default=".")
    al = sub.add_parser("alert")
    al.add_argument("--slo", required=True)
    al.add_argument("--data", required=True)
    al.add_argument("--state")
    a = p.parse_args()
    if a.cmd == "validate":
        path = os.path.join(os.path.abspath(a.root), a.file)
        try:
            with open(path) as f:
                slo = json.load(f)
            errors = validate(slo)
        except (OSError, json.JSONDecodeError) as e:
            slo, errors = None, [f"{a.file}: {e}"]
        out = {"file": a.file, "errors": errors}
        if not errors:
            out.update(availability_pct=slo["availability_pct"], latency_p95_ms=slo["latency_p95_ms"], owner=slo["owner"])
        print(json.dumps(out, ensure_ascii=False))
        return 0
    with open(a.slo) as f:
        slo = json.load(f)
    problems = validate(slo)
    if problems:
        print(json.dumps({"error": "invalid slo.json", "problems": problems}))
        return 2
    try:
        with open(a.data) as f:
            data = json.load(f)
        bad = data_problems(data)
    except (OSError, json.JSONDecodeError) as e:
        bad = [f"numbers file: {type(e).__name__}"]
    if bad:
        # The caller fetched these over the network: a clear error, never a traceback.
        print(json.dumps({"error": "invalid numbers", "problems": bad}))
        return 3
    state = None
    if a.state and os.path.exists(a.state):
        with open(a.state) as f:
            state = json.load(f)
    print(json.dumps(evaluate(slo, data, state), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
