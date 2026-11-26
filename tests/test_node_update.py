#!/usr/bin/env python3
"""Proof of the Node.js update: node_update.py proposes the newest patch or
minor of the pinned LTS line, only for exact pins, never a new major, and
apply changes only those values."""
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "..", "scripts", "node_update.py")
FAILS = []

# The shape of https://nodejs.org/dist/index.json (newest first), trimmed.
INDEX = [
    {"version": "v25.2.0", "lts": False, "security": False},
    {"version": "v24.11.1", "lts": "Krypton", "security": False},
    {"version": "v24.11.0", "lts": "Krypton", "security": False},
    {"version": "v24.10.0", "lts": False, "security": False},
    {"version": "v22.21.1", "lts": "Jod", "security": False},
    {"version": "v22.21.0", "lts": "Jod", "security": True},
    {"version": "v22.20.0", "lts": "Jod", "security": False},
    {"version": "v22.0.0", "lts": False, "security": False},
    {"version": "v23.11.0", "lts": False, "security": False},
    {"version": "v20.19.5", "lts": "Iron", "security": False},
]


def expect(name, ok, detail=""):
    print(("ok   " if ok else "FAIL ") + name)
    if not ok:
        print("     " + str(detail)[:1500])
        FAILS.append(name)


def repo(files):
    d = tempfile.mkdtemp()
    for name, text in files.items():
        open(os.path.join(d, name), "w").write(text)
    return d


def plan(d):
    idx = os.path.join(tempfile.mkdtemp(), "index.json")
    json.dump(INDEX, open(idx, "w"))
    r = subprocess.run([sys.executable, SCRIPT, "plan", d, "--index", idx], capture_output=True, text=True)
    if r.returncode:
        print(r.stderr)
    return json.loads(r.stdout)


PKG = '{\n  "name": "site",\n  "engines": {\n    "node": "22.20.0"\n  },\n  "dependencies": {"node": "1.0.0"}\n}\n'
d = repo({".node-version": "22.20.0\n", ".nvmrc": "v22.20.0\n", "package.json": PKG})
p = plan(d)
expect("patch/minor of the same LTS line", p["target"] == "22.21.1" and p["current"] == "22.20.0", p)
expect("all three exact pins change", sorted(f["path"] for f in p["files"]) == [".node-version", ".nvmrc", "package.json"], p)
expect("a security release on the way is flagged", p["security"] is True, p)
expect("a newer LTS major is reported, not applied", p["newer_lts"] == "24.11.1" and "24." not in json.dumps(p["files"]), p)
pf = os.path.join(tempfile.mkdtemp(), "plan.json")
json.dump(p, open(pf, "w"))
r = subprocess.run([sys.executable, SCRIPT, "apply", d, pf], capture_output=True, text=True)
expect("apply runs", r.returncode == 0, r.stderr)
expect(".node-version keeps its format", open(os.path.join(d, ".node-version")).read() == "22.21.1\n", "")
expect(".nvmrc keeps its v", open(os.path.join(d, ".nvmrc")).read() == "v22.21.1\n", "")
expect("package.json: only engines.node changes, bytes otherwise the same",
       open(os.path.join(d, "package.json")).read() == PKG.replace('"node": "22.20.0"', '"node": "22.21.1"'),
       open(os.path.join(d, "package.json")).read())
expect("after apply, no pin needs a change", plan(d)["files"] == [], plan(d))

p = plan(repo({".nvmrc": "22.20.0\n", "package.json": '{"engines": {"node": "^18.17.0"}}'}))
expect("a pin of another major (engines ^18) is left alone", [f["path"] for f in p["files"]] == [".nvmrc"], p)
p = plan(repo({".nvmrc": "22\n", "package.json": '{"engines": {"node": "^18.17.0"}}'}))
expect("a floating .nvmrc 22 still names the line: engines ^18 is left alone", p["files"] == [], p)
p = plan(repo({".nvmrc": "22.21.1\n", ".node-version": "22.20.0\n"}))
expect("the most behind pin of the line decides", [f["path"] for f in p["files"]] == [".node-version"]
       and p["current"] == "22.20.0", p)
d = repo({".nvmrc": "22.20.0\r\n"})
open(os.path.join(d, "package.json"), "w", newline="").write('{\r\n  "engines": {"node": "22.20.0"}\r\n}\r\n')
pf = os.path.join(tempfile.mkdtemp(), "plan.json")
json.dump(plan(d), open(pf, "w"))
subprocess.run([sys.executable, SCRIPT, "apply", d, pf], check=True)
expect("CRLF files keep CRLF", open(os.path.join(d, "package.json"), newline="").read()
       == '{\r\n  "engines": {"node": "22.21.1"}\r\n}\r\n' and open(os.path.join(d, ".nvmrc"), newline="").read() == "22.21.1\r\n", "")
p = plan(repo({".nvmrc": "22\n"}))
expect("a major alone already floats: nothing changes", p["files"] == [] and p["current"] is None, p)
p = plan(repo({".nvmrc": "lts/*\n", "package.json": '{"engines": {"node": "^18.17.0"}}'}))
expect("an alias in .nvmrc: the engines floor is left alone", p["files"] == [], p)
p = plan(repo({".nvmrc": "lts/*\n"}))
expect("an alias already floats: nothing changes", p["files"] == [], p)
p = plan(repo({"package.json": '{"engines": {"node": ">=20"}}'}))
expect("a range is left alone", p["files"] == [], p)
p = plan(repo({"package.json": '{"engines": {"node": "^22.20.0"}}'}))
expect("^ and ~ keep their operator", p["files"] and p["files"][0]["new"] == "^22.21.1", p)
p = plan(repo({".node-version": "23.5.0"}))
expect("an odd (non-LTS) line is not updated", p["files"] == [] and p["target"] is None, p)
p = plan(repo({".node-version": "24.11.1"}))
expect("already on the newest: nothing changes", p["files"] == [] and p["newer_lts"] is None, p)
p = plan(repo({".node-version": "22.0.0"}))
expect("from before the line became LTS, to its newest LTS", p["target"] == "22.21.1", p)
p = plan(repo({}))
expect("no pin: nothing changes", p == {"current": None, "target": None, "security": False, "newer_lts": None, "files": []}, p)

# --- the PR text passes the kit's own description check, in both languages
LINT = os.path.join(HERE, "..", "scripts", "pr_lint.py")
pf = os.path.join(tempfile.mkdtemp(), "plan.json")
json.dump({"current": "22.20.0", "target": "22.21.1", "security": True, "newer_lts": "24.11.1",
           "files": [{"path": ".nvmrc", "old": "22.20.0", "new": "22.21.1", "kind": "file"}]}, open(pf, "w"))
for lang, vis in (("pt", "private"), ("en", "public")):
    text = subprocess.run([sys.executable, SCRIPT, "body", pf, "--lang", lang], capture_output=True, text=True).stdout
    bf = os.path.join(tempfile.mkdtemp(), "body.md")
    open(bf, "w").write(text.split("\n", 1)[1])
    r = subprocess.run([sys.executable, LINT, "--body-file", bf, "--visibility", vis], capture_output=True, text=True)
    expect(f"PR text ({lang}) passes the description check", r.returncode == 0 and text.startswith("Node 22.20.0"),
           r.stdout + r.stderr)

# --- the action's shell, with a fake gh that logs every call ---------------------
ACTION = os.path.join(HERE, "..", "actions", "node-update", "action.yml")
lines = open(ACTION).read().split("\n")
start = next(i for i, ln in enumerate(lines) if ln.strip() == "run: |") + 1
RUN = "\n".join(ln[8:] for ln in lines[start:] if ln.startswith("        ") or not ln.strip())
FAKE_GH = """#!/bin/bash
echo "$*" >> "$GH_LOG"
case "$*" in
  "api repos/o/r/git/ref/heads/kit/node-"*) [[ -n "$GH_BRANCH_EXISTS" ]] && { echo '{}'; exit 0; }; exit 1;;
  "api repos/o/r -q .default_branch") echo main;;
  "api repos/o/r/git/ref/heads/main -q .object.sha") echo abc123;;
  "api repos/o/r/git/refs -f"*) echo '{}';;
  "pr list -R o/r --head"*) [[ -n "$GH_BRANCH_ORPHAN" ]] && echo 0 || echo 1;;
  "pr list"*) echo '[{"number": 7, "headRefName": "kit/node-22.20.5", "isCrossRepository": false},
    {"number": 8, "headRefName": "kit/node-update-fix", "isCrossRepository": false},
    {"number": 9, "headRefName": "kit/node-22.19.0", "isCrossRepository": true}]';;
  "pr close"*) echo closed;;
  "api repos/o/r/contents/"*"?ref="*) echo blobsha;;
  "api -X PUT"*) [[ -n "$GH_PUT_FAILS" ]] && exit 1; echo '{}';;
  "api -X DELETE"*) echo deleted;;
  "pr create"*) echo https://github.com/o/r/pull/9;;
esac
"""


def action(files, **env):
    d = repo(files)
    subprocess.run(["git", "init", "-q", d], check=True)
    subprocess.run(["git", "-C", d, "-c", "user.name=t", "-c", "user.email=t@example.org", "commit", "-q",
                    "--allow-empty", "-m", "base"], check=True)
    global HEADSHA
    HEADSHA = subprocess.run(["git", "-C", d, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    tmp = tempfile.mkdtemp()
    os.makedirs(os.path.join(tmp, "bin"))
    open(os.path.join(tmp, "bin", "gh"), "w").write(FAKE_GH)
    os.chmod(os.path.join(tmp, "bin", "gh"), 0o755)
    idx = os.path.join(tmp, "index.json")
    json.dump(INDEX, open(idx, "w"))
    log = os.path.join(tmp, "gh.log")
    e = dict(os.environ, PATH=os.path.join(tmp, "bin") + os.pathsep + os.environ["PATH"], GH_LOG=log,
             KIT=os.path.join(HERE, ".."), GITHUB_WORKSPACE=d, RUNNER_TEMP=tmp, GITHUB_REPOSITORY="o/r",
             GITHUB_STEP_SUMMARY=os.path.join(tmp, "summary"), LANG_PR="pt", INDEX=idx,
             **{"GITHUB_REF_NAME": "main", **env})
    r = subprocess.run(["bash", "-eo", "pipefail", "-c", RUN], capture_output=True, text=True, env=e)
    calls = open(log).read().splitlines() if os.path.exists(log) else []
    return r, calls


r, calls = action({".nvmrc": "22.21.1\n"})
expect("action: nothing to update, no call to GitHub", r.returncode == 0 and not calls and "nothing" in r.stdout, (r.stdout, calls))
r, calls = action({".nvmrc": "22.20.0\n"}, GH_BRANCH_EXISTS="1")
expect("action: a branch already there means already proposed", r.returncode == 0 and "already proposed" in r.stdout
       and not any("pr create" in c for c in calls), (r.stdout, calls))
r, calls = action({".nvmrc": "22.20.0\n", "package.json": PKG})
expect("action: branch, one API commit per file, then the PR", r.returncode == 0
       and any("refs/heads/kit/node-22.21.1" in c for c in calls)
       and sum(c.startswith("api -X PUT") for c in calls) == 2
       and any(c.startswith("pr create") and "Node 22.20.0 → 22.21.1" in c for c in calls), (r.stdout + r.stderr, calls))
expect("action: the branch starts at the checked-out commit, not the base tip",
       any("git/refs -f ref=refs/heads/kit/node-22.21.1 -f sha=" + HEADSHA in c for c in calls), calls)
expect("action: an older open proposal is closed", any(c.startswith("pr close 7") for c in calls), calls)
expect("action: a hand-made kit/node-* branch and a fork's PR are left alone",
       not any(c.startswith(("pr close 8", "pr close 9")) for c in calls), calls)
r, calls = action({".nvmrc": "22.20.0\n"}, GH_BRANCH_EXISTS="1", GH_BRANCH_ORPHAN="1")
expect("action: a branch without any PR (interrupted run) is made again",
       r.returncode == 0 and any(c.startswith("api -X DELETE") for c in calls)
       and any(c.startswith("pr create") for c in calls), (r.stdout + r.stderr, calls))
r, calls = action({".nvmrc": "22.20.0\n"}, GH_PUT_FAILS="1")
expect("action: a failed commit deletes the branch (no orphan 'already proposed')",
       r.returncode != 0 and any(c.startswith("api -X DELETE") and "kit/node-22.21.1" in c for c in calls), (r.stderr, calls))
r, calls = action({".nvmrc": "22.20.0\n"}, GITHUB_REF_NAME="feature")
expect("action: refuses to run off the default branch", r.returncode != 0 and not any("refs -f" in c for c in calls),
       (r.stdout, calls))

if FAILS:
    print(f"\n{len(FAILS)} failed")
    sys.exit(1)
print("node update: ok")
