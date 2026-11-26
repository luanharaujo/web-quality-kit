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

--cache (needs --site-dir): once the pages are live, the real Cache-Control
answered by the site is checked with the same rules as site_rules.py: every
hashed file (up to --cache-sample of them, plus each page) must say
max-age=31536000 and immutable; an HTML page must not be cached longer than
600 s. This catches a _headers file the host did not apply.
"""
import argparse
import datetime
import hashlib
import os
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import budget  # noqa: E402
import site_rules  # noqa: E402


def local_file(site_dir, path):
    rel = path.lstrip("/")
    if rel == "" or rel.endswith("/"):
        rel += "index.html"
    return os.path.join(site_dir, rel)


def fetch(url, timeout=20, with_headers=False):
    if not url.lower().startswith(("http://", "https://")):
        return (0, b"not an http(s) URL", {}) if with_headers else (0, b"not an http(s) URL")
    req = urllib.request.Request(url, headers={"User-Agent": "web-quality-kit-live-check",
                                               "Cache-Control": "no-cache"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 (scheme checked above)
            out = (r.status, r.read(), {k.lower(): v for k, v in r.headers.items()})
    except urllib.error.HTTPError as e:
        out = (e.code, b"", {k.lower(): v for k, v in (e.headers or {}).items()})
    except (urllib.error.URLError, OSError) as e:
        out = (0, str(e).encode(), {})
    return out if with_headers else out[:2]


def cache_problems(base, site_dir, paths, sample, extra=(), hashed_dirs=site_rules.HASHED_DIRS):
    """(url, problem) for each page and hashed file whose live Cache-Control
    breaks the rules. No cache-busting query here: the CDN's own answer is
    what visitors get."""
    hashed = []
    for root, dirs, names in os.walk(site_dir):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        for n in sorted(names):
            rel = os.path.relpath(os.path.join(root, n), site_dir).replace(os.sep, "/")
            if site_rules.is_hashed(rel, extra, hashed_dirs):
                hashed.append("/" + rel)
    out = []
    for path in paths:
        local = local_file(site_dir, path)
        if not local.endswith(".html"):
            continue
        status, _, h = fetch(base.rstrip("/") + path, with_headers=True)
        cc = h.get("cache-control", "")
        age = site_rules.max_age(cc)
        if status == 200 and ("immutable" in cc.lower() or (age is not None and age > site_rules.HTML_MAX_AGE)):
            out.append((path, f"page cached too long: '{cc}'"))
    for path in hashed[:sample]:
        status, _, h = fetch(base.rstrip("/") + path, with_headers=True)
        cc = h.get("cache-control", "")
        if status != 200:
            out.append((path, f"HTTP {status}"))
        elif (site_rules.max_age(cc) or 0) < site_rules.YEAR or "immutable" not in cc.lower():
            out.append((path, f"Cache-Control '{cc or 'none'}' (needs max-age={site_rules.YEAR}, immutable)"))
    return out, min(len(hashed), sample)


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
    p.add_argument("--cache", action="store_true", help="check the live Cache-Control too")
    p.add_argument("--cache-sample", type=int, default=30, help="hashed files checked at most")
    p.add_argument("--config", default="web-budget.json",
                   help="the repo's budget file: cache.hashed_dirs and cache.hashed_extra add hashed files")
    a = p.parse_args()
    if a.cache and not a.site_dir:
        print("::error::--cache needs --site-dir (the published files say which are hashed)", file=sys.stderr)
        return 2
    cfg = budget.load(a.config, datetime.date.today()) if a.cache else {"errors": [], "hashed": [], "hashed_dirs": []}
    if cfg["errors"]:
        for e in cfg["errors"]:
            print(f"::error::{a.config}: {e}", file=sys.stderr)
        return 2
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
    if a.cache:
        problems, n = cache_problems(a.base_url, a.site_dir, paths, a.cache_sample, cfg["hashed"],
                                     site_rules.HASHED_DIRS + tuple(cfg["hashed_dirs"]))
        print(f"\n## Live cache (Cache-Control)\n\n{len(paths)} pages, {n} hashed files checked.\n")
        if problems:
            print("| | Address | Problem |\n|---|---|---|")
            for path, why in problems:
                print(f"| 🔴 | `{path}` | {why} |")
                print(f"::error::live cache: {path}: {why}", file=sys.stderr)
            return 1
        print("🟢 every page and hashed file has the right Cache-Control.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
