#!/usr/bin/env python3
"""After a deploy: is the site that is live the one this run published?

  live_check.py --base-url https://example.com --site-dir _site \
                [--path / --path /adm/versao.json ...] [--tries 10] [--wait 30]

For each path: the live page must answer 200 and, when --site-dir is given,
its bytes must be the same as the published file (path/ = path/index.html).
A CDN keeps the old page for a few minutes, so each round asks again with a
query string that skips the cache, up to --tries rounds --wait seconds apart.
Exit 1 when some path still differs after the last round. Prints a Markdown
table for the run summary.
"""
import argparse
import hashlib
import os
import sys
import time
import urllib.error
import urllib.request


def local_file(site_dir, path):
    rel = path.lstrip("/")
    if rel == "" or rel.endswith("/"):
        rel += "index.html"
    return os.path.join(site_dir, rel)


def fetch(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": "web-quality-kit-live-check",
                                               "Cache-Control": "no-cache"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, b""
    except (urllib.error.URLError, OSError) as e:
        return 0, str(e).encode()


def check(base, path, site_dir, salt):
    sep = "&" if "?" in path else "?"
    status, body = fetch(f"{base.rstrip('/')}{path}{sep}live-check={salt}")
    if status != 200:
        return False, f"HTTP {status}"
    if not site_dir:
        return True, "200"
    try:
        want = hashlib.sha256(open(local_file(site_dir, path), "rb").read()).hexdigest()
    except OSError:
        return False, "not in the published files"
    got = hashlib.sha256(body).hexdigest()
    return (True, "200, same as published") if got == want else (False, "200, but an older or different version")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--base-url", required=True)
    p.add_argument("--site-dir", default="")
    p.add_argument("--path", action="append", default=[])
    p.add_argument("--tries", type=int, default=10)
    p.add_argument("--wait", type=float, default=30)
    p.add_argument("--salt", default=str(int(time.time())))
    a = p.parse_args()
    paths = a.path or ["/"]
    pending = {path: "" for path in paths}
    results = {}
    for n in range(1, a.tries + 1):
        for path in list(pending):
            ok, why = check(a.base_url, path, a.site_dir, f"{a.salt}-{n}")
            results[path] = (ok, why, n)
            if ok:
                del pending[path]
        if not pending:
            break
        if n < a.tries:
            print(f"round {n}: {len(pending)} not live yet, waiting {a.wait:g} s", file=sys.stderr)
            time.sleep(a.wait)
    print("## No ar (live check)\n\n| | Page | Result | Round |\n|---|---|---|---|")
    for path in paths:
        ok, why, n = results[path]
        print(f"| {'🟢' if ok else '🔴'} | `{path}` | {why} | {n} |")
    if pending:
        for path in pending:
            print(f"::error::{path} is not the published version: {results[path][1]}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
