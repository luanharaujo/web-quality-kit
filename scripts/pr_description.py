#!/usr/bin/env python3
"""The sticky pull-request comment <!-- PR-VERIFICACOES -->: what each check means
and how strict it is (section 4), the health panel (5) and what each metric means (6).
Plain text and emoji only, so it reads on a phone. Texts, including the interface words, live in pr_texts.json (pt, en).

  pr_description.py build --lang pt|en [--panel FILE] [--needs FILE] [--texts FILE]  > comment.md
  pr_description.py post  --event $GITHUB_EVENT_PATH --body-file comment.md

`post` creates the comment, or edits the existing one (found by the marker). It reads
only the event and the files given; it never runs anything from the pull request.
It skips quietly (exit 0, a warning) when the token cannot write: a pull request from a
fork, or Dependabot (its token is read-only unless the repository allows otherwise).
"""
import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request

MARKER = "<!-- PR-VERIFICACOES -->"
HERE = os.path.dirname(os.path.abspath(__file__))
MAX_PANEL = 40000  # a comment holds 65536 characters
DOT = {"success": "🟢", "failure": "🔴", "cancelled": "🔴", "skipped": "⚪"}


def panel_rows(panel):
    """[(dot, item)] from the table rows of the health panel."""
    rows = []
    for ln in panel.splitlines():
        cells = [c.strip() for c in ln.strip().strip("|").split("|")]
        if ln.startswith("|") and len(cells) >= 2 and cells[0] in ("🟢", "🟡", "🔴"):
            rows.append((cells[0], cells[1]))
    return rows


def build(lang, panel, needs, texts):
    kit = texts[lang]
    ui = kit["ui"]
    rows = panel_rows(panel)
    out = [MARKER, "", f"## {ui['checks']}", "", ui["cols"], "|---|---|---|---|---|"]
    off = []
    for chk in kit["checks"]:
        hit = [d for d, item in rows if any(n.lower() in item.lower() for n in chk["match"])]
        if not hit:
            off.append(chk["name"])
            continue
        res = {"🟢": ui["ok"], "🟡": ui["warn"], "🔴": ui["bad"]}[hit[0]]
        out.append(f"| {hit[0]} | {chk['name']} | {chk['means']} | {ui[chk['rigor']]} | {res} |")
    if off:
        out += ["", f"⚪ {ui['off']}: " + ", ".join(off) + "."]
    if needs:
        out += ["", f"{ui['jobs']}: " + ", ".join(
            f"{DOT.get(v.get('result'), '⚪')} `{k}` ({ui['ok'] if v.get('result') == 'success' else ui['skip'] if v.get('result') == 'skipped' else ui['bad']})"
            for k, v in sorted(needs.items()))]
    out += ["", f"## {ui['health']}", ""]
    if panel.strip():
        out.append(re.sub(r"^## ", "### ", panel.strip()[:MAX_PANEL], flags=re.M))
    else:
        out.append(ui["nopanel"])
    out += ["", f"## {ui['glossary']}", "", "<details>", f"<summary>{ui['glossary']}</summary>", ""]
    out += [f"- **{g['term']}**: {g['text']}" for g in kit["glossary"]]
    out += ["", "</details>", "", f"<sub>{ui['foot']}</sub>", ""]
    return "\n".join(out)


def api(method, url, token, data=None):
    req = urllib.request.Request(url, method=method, headers={
        "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "quality-kit-pr-description"},
        data=json.dumps(data).encode() if data is not None else None)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def post(event_path, body, token, apiurl="https://api.github.com"):
    ev = json.load(open(event_path))
    pr, repo = ev.get("pull_request") or {}, (ev.get("repository") or {}).get("full_name", "")
    if not pr or not repo:
        print("::warning::Not a pull request event: nothing to comment."); return 0
    head_repo = ((pr.get("head") or {}).get("repo") or {}).get("full_name", "")
    if head_repo != repo:
        print("::notice::Pull request from a fork: the token cannot write, comment skipped."); return 0
    base = f"{apiurl}/repos/{repo}/issues/{pr['number']}/comments"
    try:
        found, page = None, 1
        while not found:
            batch = api("GET", f"{base}?per_page=100&page={page}", token)
            found = next((c for c in batch if MARKER in (c.get("body") or "")
                          and (c.get("user") or {}).get("type") == "Bot"), None)
            if len(batch) < 100:
                break
            page += 1
        if found:
            api("PATCH", f"{apiurl}/repos/{repo}/issues/comments/{found['id']}", token, {"body": body})
        else:
            api("POST", base, token, {"body": body})
    except urllib.error.HTTPError as e:
        if e.code in (401, 403, 404):
            print(f"::warning::Comment skipped: the token cannot write here (HTTP {e.code}). "
                  "Dependabot and fork pull requests get a read-only token.")
            return 0
        raise
    print("comment updated" if found else "comment created")
    return 0


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--lang", choices=["pt", "en"], required=True)
    b.add_argument("--panel"); b.add_argument("--needs")
    b.add_argument("--texts", default=os.path.join(HERE, "pr_texts.json"))
    p = sub.add_parser("post")
    p.add_argument("--event", required=True); p.add_argument("--body-file", required=True)
    a = ap.parse_args()
    if a.cmd == "build":
        panel = open(a.panel).read() if a.panel and os.path.exists(a.panel) else ""
        needs = json.load(open(a.needs)) if a.needs and os.path.exists(a.needs) else {}
        print(build(a.lang, panel, needs, json.load(open(a.texts))))
        return 0
    return post(a.event, open(a.body_file).read(), os.environ.get("GITHUB_TOKEN", ""))


if __name__ == "__main__":
    sys.exit(main())
