#!/usr/bin/env python3
"""Keep the preview link as the first line of the pull-request description.

The link lives in a block between two HTML comment markers. On every push the block is
rewritten (new commit) and moved back to the top; the rest of the body is kept as it
was. The body is untrusted text: only what is between the markers is replaced, and
nothing from it is executed or reused. The description lint (pr_lint.py) ignores
whatever comes before the first section, so the block does not affect it.

Env: GITHUB_EVENT_PATH, GITHUB_TOKEN, PREVIEW_URL, PREVIEW_NOTE (optional),
     LANG_IN (pt, en or auto), GITHUB_API_URL (set by Actions).
Skips with a notice on a fork or Dependabot pull request (read-only token) and when a
newer commit was pushed (its own run writes the link). A missing permission fails the
step; GitHub being down only warns.
"""
import json
import os
import re
import sys
import urllib.error

from pr_description import api

START = "<!-- PREVIEW-LINK -->"
END = "<!-- /PREVIEW-LINK -->"
URL_OK = re.compile(r"https://[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+(:[0-9]{1,5})?(/[A-Za-z0-9._~/%?&=#+,:;@!-]*)?")
# The line build_block writes: what is left of our block if one marker is deleted.
LINK_LINE = re.compile(r"\*\*\[[^]]*\]\(https://\S+\)\*\* · \S+ `[0-9a-f]{7}`.*")
FENCE = re.compile(r" {0,3}(`{3,}|~{3,})(.*)")
HERE = os.path.dirname(os.path.abspath(__file__))
MAX_NOTE = 200


def build_block(url, sha, note, texts):
    if not URL_OK.fullmatch(url or ""):
        raise ValueError("the preview URL must be a plain https URL")
    note = (note or "").strip()
    if len(note) > MAX_NOTE or any(c in note for c in "<>\r\n"):
        raise ValueError(f"the note must be one line, up to {MAX_NOTE} characters, with no HTML")
    line = f"**[{texts['open']}]({url})** · {texts['version']} `{sha[:7]}`"
    if note:
        line += f" ({note})"
    return [START, line, END]


def own_blocks(lines):
    """[(first, last)] of our marker blocks. A marker is a whole line with no indentation;
    one quoted in a code block is text. A marker that lost its pair is removed only
    together with our own link line next to it."""
    fence, start, found = None, None, []

    def is_link(i):
        return 0 <= i < len(lines) and LINK_LINE.fullmatch(lines[i]) is not None

    for i, line in enumerate(lines):
        f = FENCE.fullmatch(line)
        if fence:
            if f and f[1].startswith(fence) and not f[2].strip():
                fence = None
        elif f and not (f[1][0] == "`" and "`" in f[2]):
            if start is not None and is_link(start + 1):
                found.append((start, start + 1))
            fence, start = f[1], None
        elif line.rstrip() == START:
            if start is not None and is_link(start + 1):
                found.append((start, start + 1))
            start = i
        elif line.rstrip() == END:
            if start is not None:
                found.append((start, i))
                start = None
            elif is_link(i - 1):
                found.append((i - 1, i))
    if start is not None and is_link(start + 1):
        found.append((start, start + 1))
    return found


def set_link(body, block):
    """The new body: the block first, then the rest. The same string if nothing changes."""
    text = body or ""
    eol = "\r\n" if "\r\n" in text else "\n"
    lines = re.split(r"\r?\n", text)
    old = list(lines)
    for first, last in reversed(own_blocks(lines)):
        after = last + 1
        if after < len(lines) and not lines[after].strip():
            after += 1
        del lines[first:after]
    while lines and not lines[0].strip():
        del lines[0]
    new = block + [""] + lines
    return text if new == old else eol.join(new)


def run(env, event, apiurl):
    pr, repo = event.get("pull_request") or {}, (event.get("repository") or {}).get("full_name", "")
    if not pr or not repo:
        print("::error::preview-link needs a pull_request event."); return 1
    head = pr.get("head") or {}
    if (head.get("repo") or {}).get("full_name", "") != repo:
        print("::notice::Pull request from a fork: the token cannot write, preview link skipped."); return 0
    if (pr.get("user") or {}).get("login") == "dependabot[bot]":
        print("::notice::Dependabot pull request: the token cannot write, preview link skipped."); return 0
    lang = env.get("LANG_IN") or "auto"
    if lang == "auto":
        lang = "pt" if (event.get("repository") or {}).get("private") else "en"
    with open(os.path.join(HERE, "pr_texts.json"), encoding="utf-8") as f:
        all_texts = json.load(f)
    if lang not in all_texts:
        print("::error::`lang` must be pt, en or auto."); return 1
    texts = all_texts[lang]["preview"]
    try:
        block = build_block(env.get("PREVIEW_URL", ""), head.get("sha", ""), env.get("PREVIEW_NOTE", ""), texts)
    except ValueError as e:
        print(f"::error::{e}."); return 1
    url = f"{apiurl}/repos/{repo}/pulls/{int(pr['number'])}"
    token = env.get("GITHUB_TOKEN", "")
    try:
        # The body as it is now, not the one in the event: someone may have edited it.
        now = api("GET", url, token)
        if now["head"]["sha"] != head.get("sha"):
            print("::notice::A newer commit was pushed: its run writes the preview link."); return 0
        body = now.get("body") or ""
        new = set_link(body, block)
        if new == body:
            print("the description already starts with this preview link"); return 0
        api("PATCH", url, token, {"body": new})
    except urllib.error.HTTPError as e:
        limited = e.code == 429 or (e.code == 403 and (
            e.headers.get("Retry-After") or e.headers.get("X-RateLimit-Remaining") == "0"))
        if e.code >= 500 or limited:
            print(f"::warning::Preview link not written: GitHub answered HTTP {e.code}."); return 0
        why = {422: "GitHub refused the new description (too long?)"}.get(
            e.code, "the job needs `pull-requests: write`")
        print(f"::error::Preview link not written (HTTP {e.code}): {why}.")
        return 1
    except OSError as e:
        print(f"::warning::Preview link not written: could not reach the GitHub API ({e})."); return 0
    print("preview link written at the top of the description")
    return 0


def main():
    with open(os.environ["GITHUB_EVENT_PATH"], encoding="utf-8") as f:
        event = json.load(f)
    return run(os.environ, event, os.environ.get("GITHUB_API_URL", "https://api.github.com"))


if __name__ == "__main__":
    sys.exit(main())
