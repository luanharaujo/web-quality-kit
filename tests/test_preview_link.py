#!/usr/bin/env python3
"""Proof of the preview link: it is the first line of the description, rewritten on each
push, never duplicated, the rest of the body untouched, the description lint still
passes, and the script writes, skips or fails as it should against a fake GitHub API.
Run: python3 tests/test_preview_link.py"""
import http.server
import json
import os
import subprocess
import sys
import tempfile
import threading

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
S = os.path.join(ROOT, "scripts")
sys.path.insert(0, S)
import pr_lint  # noqa: E402
import preview_link as pl  # noqa: E402

fails = []


def check(name, ok, extra=""):
    print(("ok   " if ok else "FAIL ") + name + (" " + str(extra) if not ok and extra else ""))
    if not ok:
        fails.append(name)


texts = json.load(open(os.path.join(S, "pr_texts.json"), encoding="utf-8"))
EN = texts["en"]["preview"]
URL = "https://pr-7-site-preview.account.workers.dev"
SHA1, SHA2 = "a" * 40, "b" * 40
B1, B2 = pl.build_block(URL, SHA1, "", EN), pl.build_block(URL, SHA2, "", EN)
BODY = ("## What this PR does\nChanges the sign-in button from blue to green on the login screen.\n\n"
        "## Why\nThe blue one was easy to mistake for the link next to it.\n\n"
        "## Risk and how to undo\nLow risk, color only. To undo, revert the PR.\n\n"
        "<details>\n<summary>Technical details</summary>\nlogin.css\n</details>\n")

once = pl.set_link(BODY, B1)
check("link added on top, body kept as it was", once == "\n".join(B1) + "\n\n" + BODY, once)
check("the first visible line is the link", once.splitlines()[1].startswith(f"**[{EN['open']}]({URL})**"))
check("the line names the commit", "`aaaaaaa`" in once.splitlines()[1])
twice = pl.set_link(once, B2)
check("next push rewrites it, never duplicates", twice.count(pl.START) == 1 and "`bbbbbbb`" in twice and "`aaaaaaa`" not in twice and twice.endswith(BODY), twice)
check("same commit again: nothing changes", pl.set_link(once, B1) is once)
moved = f"Note above.\n\n{pl.START}\nold\n{pl.END}\n\n{BODY}"
out = pl.set_link(moved, B1)
check("a block that was moved goes back to the top", out == "\n".join(B1) + "\n\nNote above.\n\n" + BODY, out)
out = pl.set_link(pl.set_link(BODY, B1) + f"\n{pl.START}\nold\n{pl.END}\n", B2)
check("a second block is removed", out.count(pl.START) == 1 and "old" not in out, out)
crlf = BODY.replace("\n", "\r\n")
out = pl.set_link(crlf, B1)
check("CRLF stays CRLF", "\n" not in out.replace("\r\n", "") and pl.set_link(out, B1) is out and out.endswith(crlf))
for fence in ("```", "~~~~"):
    quoted = f"See:\n\n{fence}\n{pl.START}\nx\n{pl.END}\n{fence}\n"
    out = pl.set_link(quoted, B1)
    check(f"markers inside a {fence} code block are text", out.endswith(quoted) and pl.set_link(out, B1) is out, out)
lone = f"{pl.START}\nimportant note\n"
out = pl.set_link(pl.set_link(lone, B1), B2)
check("a start marker with no end never swallows text", "important note" in out and "`bbbbbbb`" in out and "`aaaaaaa`" not in out, out)
for pad in ("    ", "      "):
    quoted = f"- item\n\n{pad}{pl.START}\n{pad}x\n{pad}{pl.END}\n"
    out = pl.set_link(quoted, B1)
    check(f"markers indented by {len(pad)} spaces are text", out.endswith(quoted) and pl.set_link(out, B1) is out, out)
for lost, name in ((pl.END, "end"), (pl.START, "start")):
    broken = once.replace(lost + "\n", "")
    out = pl.set_link(broken, B2)
    check(f"a deleted {name} marker leaves no stale link", out == pl.set_link(BODY, B2), out)
check("empty and missing body", pl.set_link("", B1) == pl.set_link(None, B1) == "\n".join(B1) + "\n")
check("an empty body does not grow", pl.set_link(pl.set_link("", B1), B1) == "\n".join(B1) + "\n")

check("lint passes with the block on top", pr_lint.lint(once, "public") == [], pr_lint.lint(once, "public"))
pt_body = open(os.path.join(ROOT, "tests", "cases", "pr_body_pt.md"), encoding="utf-8").read()
pt_block = pl.build_block(URL, SHA1, "", texts["pt"]["preview"])
check("lint passes with the block on top (pt)", pr_lint.lint(pl.set_link(pt_body, pt_block), "private") == [])
check("the block does not hide a bad description", pr_lint.lint(pl.set_link("", B1), "public") != [])


def refused(url, note=""):
    try:
        pl.build_block(url, SHA1, note, EN)
    except ValueError:
        return True
    return False


for bad in ("", "http://a.example.com/", "https://a.example.com/) [x](https://evil.example", "https://a.example.com/ x",
            "https://user@evil.example.com/", "https://localhost/", "javascript:alert(1)", URL + "\n## x"):
    check(f"URL refused: {bad!r}", refused(bad))
check("URL with path and query accepted", not refused(URL + "/pt/?draft=1#top"))
for bad in ("a\nb", pl.END, "<b>x</b>", "x" * 201):
    check(f"note refused: {bad[:20]!r}", refused(URL, bad))
check("note shown after the link", pl.build_block(URL, SHA1, "sign in by e-mail", EN)[1].endswith("(sign in by e-mail)"))

# --- against a fake GitHub API ----------------------------------------------------
LOG = []
STATE = {"body": BODY, "sha": SHA1, "get": 200, "patch": 200, "headers": {}}


class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def reply(self, code, obj):
        data = STATE.get("raw") or json.dumps(obj).encode()
        self.send_response(code); self.send_header("Content-Length", str(len(data)))
        for k, v in STATE["headers"].items():
            self.send_header(k, v)
        self.end_headers(); self.wfile.write(data)

    def do_GET(self):
        self.reply(STATE["get"], {"body": STATE["body"], "head": {"sha": STATE["sha"]}})

    def do_PATCH(self):
        b = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        LOG.append((self.path, sorted(b)))
        if STATE["patch"] == 200:
            STATE["body"] = b["body"]
        self.reply(STATE["patch"], {})


srv = http.server.HTTPServer(("127.0.0.1", 0), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
API = f"http://127.0.0.1:{srv.server_port}"


def run(sha=SHA1, fork=False, user="someone", url=URL, private=False, lang="auto"):
    fd, path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w") as f:
        json.dump({"pull_request": {"number": 3, "user": {"login": user},
                                    "head": {"sha": sha, "repo": {"full_name": "other/x" if fork else "o/x"}}},
                   "repository": {"full_name": "o/x", "private": private}}, f)
    env = dict(os.environ, GITHUB_EVENT_PATH=path, GITHUB_API_URL=API, GITHUB_TOKEN="t",
               PREVIEW_URL=url, PREVIEW_NOTE="", LANG_IN=lang)
    return subprocess.run([sys.executable, "-E", os.path.join(S, "preview_link.py")], env=env, capture_output=True, text=True)


r = run()
check("writes the link, only the body is sent", r.returncode == 0 and LOG == [("/repos/o/x/pulls/3", ["body"])] and STATE["body"].startswith(pl.START), r.stdout + r.stderr)
LOG.clear(); r = run()
check("same push again: no write", r.returncode == 0 and not LOG, r.stdout)
STATE["sha"] = SHA2; r = run(sha=SHA1)
check("an old run does not overwrite a newer push", r.returncode == 0 and not LOG, r.stdout)
r = run(sha=SHA2, private=True)
check("new push updates it, in Portuguese on a private repository", r.returncode == 0 and len(LOG) == 1 and texts["pt"]["preview"]["open"] in STATE["body"] and "`bbbbbbb`" in STATE["body"], r.stdout)
LOG.clear(); STATE["body"] = BODY
check("fork: skipped", run(sha=SHA2, fork=True).returncode == 0 and not LOG)
check("Dependabot: skipped", run(sha=SHA2, user="dependabot[bot]").returncode == 0 and not LOG)
check("bad URL fails and writes nothing", run(sha=SHA2, url="http://x.example.com").returncode == 1 and not LOG)
r = run(sha=SHA2, lang="pt-BR")
check("unknown language fails with a message", r.returncode == 1 and "::error::" in r.stdout and not r.stderr, r.stderr)
STATE["patch"] = 403
check("missing permission fails the step", run(sha=SHA2).returncode == 1)
STATE["headers"] = {"X-RateLimit-Remaining": "0"}
check("rate limit (403) only warns", run(sha=SHA2).returncode == 0)
STATE["headers"], STATE["patch"] = {}, 429
check("rate limit (429) only warns", run(sha=SHA2).returncode == 0)
STATE["patch"] = 422
r = run(sha=SHA2)
check("a refused description fails without blaming the permission", r.returncode == 1 and "pull-requests" not in r.stdout, r.stdout)
STATE["patch"] = 502
check("GitHub down only warns", run(sha=SHA2).returncode == 0)
STATE["raw"] = b"<html>oops"
r = run(sha=SHA2)
check("an answer that is not JSON only warns", r.returncode == 0 and "::warning::" in r.stdout and not r.stderr, r.stderr)
srv.shutdown()

# --- the action and the template ----------------------------------------------------
act = open(os.path.join(ROOT, "actions", "preview-link", "action.yml")).read()
check("action: inputs go through the environment, never into the shell line", 'run: python3 -E "$SCRIPT"' in act and act.count("run:") == 1)
tpl = open(os.path.join(ROOT, "templates", "preview-link.yml")).read()
check("template: least permission and the URL from the preview step", "pull-requests: write" in tpl and "pull_request_target" not in tpl and "url: ${{ steps." in tpl)

print("%d failed" % len(fails) if fails else "all ok")
sys.exit(1 if fails else 0)
