#!/usr/bin/env python3
"""Proof of the PR description checks: the lint copy equals the central one, the sticky
comment shows the real result of each check, every metric of the health panel has an
explanation (pt and en), and posting creates, updates or skips as it should.
Run: python3 tests/test_pr_description.py"""
import hashlib
import http.server
import json
import os
import re
import subprocess
import sys
import tempfile
import threading

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
S = os.path.join(ROOT, "scripts")
sys.path.insert(0, S)
import pr_description as pd  # noqa: E402

LINT_SHA256 = "809103ec6e5f08c4f504a1e0eb1100c3c9f79035f966d2b4fcf3f5d54d5f0d8f"  # pr_lint.py is copied from the central source: a new copy updates this
CENTRAL = os.environ.get("PR_LINT_CENTRAL", "")  # path of the central pr_lint.py (set by the owner locally)
WEB = os.path.exists(os.path.join(S, "web_health.py"))
fails = []


def mkfile(suffix=""):
    fd, path = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    return path


def check(name, ok, extra=""):
    print(("ok   " if ok else "FAIL ") + name + (" " + str(extra) if not ok and extra else ""))
    if not ok:
        fails.append(name)


# --- the lint copy -----------------------------------------------------------
mine = open(os.path.join(S, "pr_lint.py"), "rb").read()
check("lint copy matches the pinned hash", hashlib.sha256(mine).hexdigest() == LINT_SHA256)
if CENTRAL:
    check("lint copy equals the central source", mine == open(CENTRAL, "rb").read())

PT_BODY = open(os.path.join(ROOT, "tests", "cases", "pr_body_pt.md")).read()
EN_BODY = "## What this PR does\nChanges the sign-in button from blue to green on the login screen.\n\n## Why\nThe blue one was easy to mistake for the link next to it, so this is clearer.\n\n## Risk and how to undo\nLow risk, color only. To undo, revert the PR.\n\n<details>\n<summary>Technical details</summary>\nlogin.css\n</details>\n"


def lint(body, private, user="someone"):
    ev = {"pull_request": {"body": body, "user": {"login": user}}, "repository": {"private": private}}
    p = mkfile(suffix=".json")
    json.dump(ev, open(p, "w"))
    return subprocess.run([sys.executable, os.path.join(S, "pr_lint.py"), "--event", p], capture_output=True, text=True).returncode


check("lint: good pt body in a private repo passes", lint(PT_BODY, True) == 0)
check("lint: good en body in a public repo passes", lint(EN_BODY, False) == 0)
check("lint: pt body in a public repo fails", lint(PT_BODY, False) == 1)
check("lint: empty body fails", lint("", True) == 1)
check("lint: Dependabot body is exempt", lint("", True, "dependabot[bot]") == 0)
check("lint: canary App body is exempt", lint("", True, "luanharaujo-canary[bot]") == 0)

# --- the texts: every metric of the real panel has an explanation -------------
texts = json.load(open(os.path.join(S, "pr_texts.json")))
for lang in ("pt", "en"):
    t = texts[lang]
    check(f"{lang}: every check has name, meaning, rigor", all(c["name"] and c["means"] and c["rigor"] in ("blocks", "warns") for c in t["checks"]))
    check(f"{lang}: glossary is not empty", len(t["glossary"]) >= 6)

if WEB:
    import web_health as wh
    labels = [l for _, l in wh.CASE_METRICS] + ["Tests", "Bundle size (JS+CSS, gzip)",
              "Vulnerable dependencies (npm audit)", "Lighthouse Performance", "Lighthouse Accessibility",
              "Lighthouse Best practices", "Lighthouse SEO", *wh.BUDGET_LABELS.values(),
              "Performance budget: configuration", "Site rules: configuration", *wh.SERVER_LABELS.values()]
    pt = json.load(open(os.path.join(S, "panel_pt.json")))
    for lang in ("en", "pt"):
        for label in labels:
            shown = label if lang == "en" else pt.get(label, label)
            if lang == "pt" and label.startswith("Lighthouse"):
                shown = "Lighthouse " + pt.get(label.split(" ", 1)[1], label.split(" ", 1)[1])
            hit = any(n.lower() in shown.lower() for c in texts[lang]["checks"] for n in c["match"])
            check(f"{lang}: panel item '{shown}' is explained", hit)
    METRICS = {"kit": 1, "metrics": {"eslint": [], "tests": {"passed": 5, "failed": 1, "exit": 1},
               "bundle": {"kb": 50, "gzip_kb": 10.0, "largest": [], "max_kb": 0}}}
    BASE = {"kit": 1, "metrics": {"eslint": [], "tests": {"passed": 6, "failed": 0, "exit": 0},
            "bundle": {"kb": 50, "gzip_kb": 10.0, "largest": [], "max_kb": 0}}}
    p, pb = mkfile(suffix=".json"), mkfile(suffix=".json")
    json.dump(METRICS, open(p, "w")); json.dump(BASE, open(pb, "w"))
    panel_en = subprocess.run([sys.executable, os.path.join(S, "web_health.py"), "compare", pb, p], capture_output=True, text=True).stdout
    panel_pt = subprocess.run([sys.executable, os.path.join(S, "web_health.py"), "compare", pb, p, "--lang", "pt"], capture_output=True, text=True).stdout
    RED = (texts["en"]["checks"][0]["name"], texts["pt"]["checks"][0]["name"])
else:
    sample = {"target": "Core", "coverage": 85.0, "tests": 8, "lint": [], "rules": []}
    base = dict(sample, coverage=90.0, tests=10)
    d = tempfile.mkdtemp()
    pb, ph = os.path.join(d, "b.json"), os.path.join(d, "h.json")
    json.dump(base, open(pb, "w")); json.dump(sample, open(ph, "w"))
    panel_pt = panel_en = subprocess.run([sys.executable, os.path.join(S, "health.py"), "compare", pb, ph], capture_output=True, text=True).stdout
    RED = (texts["en"]["checks"][0]["name"], texts["pt"]["checks"][0]["name"])
    import health
    for ln in panel_pt.splitlines():
        cells = [c.strip() for c in ln.strip().strip("|").split("|")]
        if ln.startswith("|") and cells[0] in ("🟢", "🟡", "🔴"):
            check(f"panel item '{cells[1]}' is explained", any(n.lower() in cells[1].lower() for c in texts["pt"]["checks"] for n in c["match"]))

# --- the comment ---------------------------------------------------------------
for lang, panel, red in (("pt", panel_pt, RED[1]), ("en", panel_en, RED[0])):
    body = pd.build(lang, panel, {"web-health": {"result": "failure"}, "other": {"result": "success"}}, texts)
    check(f"{lang}: comment starts with the marker", body.startswith(pd.MARKER))
    check(f"{lang}: has the three sections", all(("## " + texts[lang]["ui"][k]) in body for k in ("checks", "health", "glossary")))
    row = next(l for l in body.splitlines() if l.startswith("| ") and f"| {red} |" in l)
    check(f"{lang}: a failing metric shows 🔴 and blocks", row.startswith("| 🔴") and texts[lang]["ui"]["blocks"] in row, row)
    check(f"{lang}: job results listed", "🔴 `web-health`" in body and "🟢 `other`" in body)
    check(f"{lang}: panel title demoted, no second H2 from the panel", "### " in body and body.splitlines().count("## " + texts[lang]["ui"]["health"]) == 1 and sum(l.startswith("## ") for l in body.splitlines()) == 3)
    check(f"{lang}: no HTML beyond details/summary/sub", "<script" not in body and "<img" not in body)
empty = pd.build("pt", "", {}, texts)
check("no panel: says so and still names every check as not run", texts["pt"]["ui"]["nopanel"] in empty and all(c["name"] in empty for c in texts["pt"]["checks"]))
huge = pd.build("en", "| 🟢 | Tests | x | y | z |\n" + "x" * 90000, {}, texts)
check("a huge panel stays under the comment limit", len(huge) < 65000)

# --- posting (a fake GitHub API) -------------------------------------------------
LOG = []
STATE = {"comments": [], "status": 200}


class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def reply(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code); self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)

    def body(self):
        return json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")

    def do_GET(self):
        self.reply(200, STATE["comments"])

    def do_POST(self):
        LOG.append(("POST", self.path)); b = self.body()
        if STATE["status"] != 200:
            return self.reply(STATE["status"], {"message": "no"})
        STATE["comments"].append({"id": 7, "body": b["body"], "user": {"type": "Bot"}}); self.reply(201, {})

    def do_PATCH(self):
        LOG.append(("PATCH", self.path)); b = self.body()
        STATE["comments"][0]["body"] = b["body"]; self.reply(200, {})


srv = http.server.HTTPServer(("127.0.0.1", 0), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
url = f"http://127.0.0.1:{srv.server_port}"


def event(fork=False):
    p = mkfile(suffix=".json")
    json.dump({"pull_request": {"number": 3, "head": {"repo": {"full_name": "other/x" if fork else "o/x"}}},
               "repository": {"full_name": "o/x"}}, open(p, "w"))
    return p


pd.post(event(), pd.MARKER + " one", "t", url)
check("post creates the comment when there is none", LOG == [("POST", "/repos/o/x/issues/3/comments")], LOG)
LOG.clear(); pd.post(event(), pd.MARKER + " two", "t", url)
check("post edits the same comment on the next push", LOG == [("PATCH", "/repos/o/x/issues/comments/7")] and STATE["comments"][0]["body"].endswith("two"), LOG)
STATE["comments"][0]["user"]["type"] = "User"; LOG.clear(); pd.post(event(), pd.MARKER + " three", "t", url)
check("a person's comment with the marker is not taken over", LOG == [("POST", "/repos/o/x/issues/3/comments")], LOG)
LOG.clear(); check("a fork pull request is skipped without failing", pd.post(event(True), "x", "t", url) == 0 and not LOG)
STATE["comments"].clear(); STATE["status"] = 403
check("a read-only token (Dependabot) skips without failing", pd.post(event(), "x", "t", url) == 0)
srv.shutdown()

# --- the workflow template -----------------------------------------------------
tpl = open(os.path.join(ROOT, "templates", "pr-description.yml")).read()
check("template: job is named pr-descricao", "\n  pr-descricao:" in tpl)
check("template: never pull_request_target", "pull_request_target:" not in tpl and "pull_request:" in tpl)
check("template: events", "[opened, edited, synchronize, reopened]" in tpl)
check("template: ready_for_review does not rerun the checks", not re.search(r"^[^#\n]*ready_for_review", tpl, re.M))
check("template: read only", "contents: read" in tpl and not re.search(r"^[^#\n]*write", tpl, re.M))
check("template: runner from CI_RUNNER, Dependabot on ubuntu-latest", "fromJSON(vars.CI_RUNNER || '\"ubuntu-latest\"')" in tpl and "dependabot[bot]" in tpl)
check("template: lint only", "mode: lint" in tpl)
check("template: reports on every pull request", not re.search(r"^\s*(needs|if|paths|paths-ignore|branches):", tpl, re.M))


def cancels_on_edit(text):
    """The defect: a workflow that listens to `edited` and has a concurrency group (a newer
    run cancels the older one, or the pending one, on the same commit)."""
    code = "\n".join(ln.split("#")[0] for ln in text.splitlines())
    edited = re.search(r"types:\s*\[[^\]]*\bedited\b|^\s*-\s*edited\s*$", code, re.M)
    return bool(edited and re.search(r"^\s*concurrency:", code, re.M))


BAD = "on:\n  pull_request:\n    types: [opened, edited, synchronize]\nconcurrency:\n  group: pr\n  cancel-in-progress: true\n"
BAD_LIST = "on:\n  pull_request:\n    types:\n      - edited\njobs:\n  a:\n    concurrency:\n      group: pr\n"
check("rule: edited with a concurrency group is caught", cancels_on_edit(BAD) and cancels_on_edit(BAD_LIST))
check("rule: edited alone and concurrency alone pass", not cancels_on_edit(BAD.split("concurrency:")[0]) and not cancels_on_edit(BAD.replace("edited, ", "")))
tdir = os.path.join(ROOT, "templates")
hit = [f for f in sorted(os.listdir(tdir)) if f.endswith(".yml") and cancels_on_edit(open(os.path.join(tdir, f)).read())]
check("templates: none can cancel its own run on an edited description", not hit, hit)
job = open(os.path.join(tdir, "pr-comment-job.yml")).read()
check("comment job: separate name, so only one check is called pr-descricao", "\n  pr-verificacoes:" in job and "\n  pr-descricao:" not in job)
check("comment job: comment only, with needs", "mode: comment" in job and "toJSON(needs)" in job)
check("comment job: only the comment can write", job.count(": write") == 1 and "pull-requests: write" in job)
act = open(os.path.join(ROOT, "actions", "pr-description", "action.yml")).read()
steps = {b.split("\n")[0]: b for b in act.split("\n    - name: ")[1:]}
check("action: lint mode never writes the comment", all("if: inputs.mode != 'lint'" in steps[n] for n in ("Health panel of this run", "Checks and health comment")))
check("action: comment mode never runs the lint", "if: inputs.mode != 'comment'" in steps["Description standard (lint)"])
guard = steps["Mode"].split("run: |\n")[1].split("\n\n")[0]
run_mode = lambda m: subprocess.run(["bash", "-c", guard], env=dict(os.environ, MODE=m), capture_output=True).returncode
check("action: an unknown mode fails", [run_mode(m) for m in ("all", "lint", "comment", "lnt", "")] == [0, 0, 0, 1, 1])
check("action: every external action is pinned by SHA", all(len(l.split("@")[1].split()[0]) == 40 for l in act.splitlines() if "uses: actions/" in l))

print("%d failed" % len(fails) if fails else "all ok")
sys.exit(1 if fails else 0)
