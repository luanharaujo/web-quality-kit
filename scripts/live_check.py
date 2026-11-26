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
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import budget  # noqa: E402
import site_rules  # noqa: E402


def local_file(site_dir, path):
    """The published file behind a page address, clean URLs included
    (/about serves about.html or about/index.html, as static hosts do)."""
    rel = path.split("?")[0].lstrip("/")
    if rel == "" or rel.endswith("/"):
        return os.path.join(site_dir, rel + "index.html")
    full = os.path.join(site_dir, rel)
    if not os.path.splitext(rel)[1] and not os.path.isfile(full):
        for alt in (rel + ".html", rel + "/index.html"):
            if os.path.isfile(os.path.join(site_dir, alt)):
                return os.path.join(site_dir, alt)
    return full


def headers_of(msg):
    """Lower-case names; a header sent twice (a CDN and the origin both set
    Cache-Control) keeps both values, joined, so neither is lost."""
    out = {}
    for k, v in (msg.items() if msg else []):
        k = k.lower()
        out[k] = f"{out[k]}, {v}" if k in out else v
    return out


def fetch(url, timeout=20, with_headers=False):
    if not url.lower().startswith(("http://", "https://")):
        return (0, b"not an http(s) URL", {}) if with_headers else (0, b"not an http(s) URL")
    req = urllib.request.Request(url, headers={"User-Agent": "web-quality-kit-live-check",
                                               "Cache-Control": "no-cache"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 (scheme checked above)
            out = (r.status, r.read(), headers_of(r.headers))
    except urllib.error.HTTPError as e:
        out = (e.code, b"", headers_of(e.headers))
    except (urllib.error.URLError, OSError) as e:
        out = (0, str(e).encode(), {})
    return out if with_headers else out[:2]


def fetch_steady(url, tries=3, wait=2.0):
    """One answer worth judging: a network error or a 5xx is asked again
    (a deploy should not fail on a single blip)."""
    for n in range(tries):
        status, body, h = fetch(url, with_headers=True)
        if status and status < 500:
            break
        if n < tries - 1:
            time.sleep(wait)
    return status, body, h


def spread(files, sample, seed=""):
    """At most `sample` files, taken in turn from each folder, so one big
    build folder does not hide another (a declared assets/ next to _astro/).
    The seed (the run id) moves the start in each folder, so runs check
    different files over time instead of the same first ones."""
    folders = {}
    for f in files:
        folders.setdefault(f.rsplit("/", 1)[0], []).append(f)
    shift = int(hashlib.sha256(seed.encode()).hexdigest(), 16) if seed else 0
    out, queues = [], []
    for _, v in sorted(folders.items()):
        k = shift % len(v)
        queues.append(v[k:] + v[:k])
    while len(out) < sample and any(queues):
        for q in queues:
            if q and len(out) < sample:
                out.append(q.pop(0))
    return out


def cache_problems(base, site_dir, paths, sample, extra=(), hashed_dirs=site_rules.HASHED_DIRS, exceptions=(),
                   tries=3, wait=2.0, seed=""):
    """(url, problem) for each page and hashed file whose live Cache-Control
    breaks the rules of site_rules.py (same exceptions: cache-html,
    cache-hashed). No cache-busting query here: the CDN's own answer is
    what visitors get."""
    hashed = []
    for root, dirs, names in os.walk(site_dir):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        for n in sorted(names):
            rel = os.path.relpath(os.path.join(root, n), site_dir).replace(os.sep, "/")
            if site_rules.is_hashed(rel, extra, hashed_dirs) and not budget.rule_exception("cache-hashed", rel, exceptions):
                hashed.append("/" + rel)
    out, pages = [], []

    def url(path):
        return base.rstrip("/") + urllib.parse.quote(path, safe="/?=&%")

    for path in paths:
        local = local_file(site_dir, path)
        rel = os.path.relpath(local, site_dir).replace(os.sep, "/")
        if not local.endswith(".html") or budget.rule_exception("cache-html", rel, exceptions):
            continue
        pages.append(path)
        status, _, h = fetch_steady(url(path), tries, wait)
        cc = h.get("cache-control", "")
        age = site_rules.max_age(cc, shared=True)
        if status != 200:
            out.append((path, f"HTTP {status}"))
        elif "immutable" in cc.lower() or (age is not None and age > site_rules.HTML_MAX_AGE):
            out.append((path, f"page cached too long: '{cc}'"))
    chosen = spread(hashed, sample, seed)
    for path in chosen:
        status, _, h = fetch_steady(url(path), tries, wait)
        cc = h.get("cache-control", "")
        if status != 200:
            out.append((path, f"HTTP {status}"))
        elif (site_rules.max_age(cc, shortest=True) or 0) < site_rules.YEAR or "immutable" not in cc.lower():
            out.append((path, f"Cache-Control '{cc or 'none'}' (needs max-age={site_rules.YEAR}, immutable)"))
    return out, len(chosen), pages, len(hashed)


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
    # The config is read now but judged after the live check: a budget problem
    # must not hide whether the deploy is live.
    cfg = budget.load(a.config, datetime.date.today()) if a.cache else {"errors": [], "hashed": [], "hashed_dirs": [], "exceptions": []}
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
    if a.cache and cfg["errors"]:
        for e in cfg["errors"]:
            print(f"::error::{a.config}: {e}", file=sys.stderr)
        return 2
    if a.cache:
        problems, n, checked, total = cache_problems(a.base_url, a.site_dir, paths, a.cache_sample, cfg["hashed"],
                                     site_rules.HASHED_DIRS + tuple(cfg["hashed_dirs"]), cfg["exceptions"],
                                     wait=min(a.wait, 5), seed=a.salt)
        print(f"\n## Live cache (Cache-Control)\n\n{len(checked)} pages, {n} hashed files checked.\n")
        skipped = [p for p in paths if p not in checked]
        if skipped:
            print(f"🟡 not HTML pages (or with a cache-html exception), cache not checked: "
                  f"{', '.join(f'`{p}`' for p in skipped)}\n")
        if not os.path.exists(a.config):
            print(f"🟡 {a.config} not found (no checkout in this job?): only the kit's build folders count as hashed.\n")
        files = [os.path.relpath(os.path.join(r, f), a.site_dir) for r, _, fs in os.walk(a.site_dir) for f in fs]
        for folder, hits, total in site_rules.looks_like_build(files, site_rules.HASHED_DIRS + tuple(cfg["hashed_dirs"])):
            print(f"🟡 {folder} looks like build output ({hits} of {total} names carry a hash) but is not declared "
                  "in cache.hashed_dirs: its cache was not checked.\n")
            print(f"::warning::live cache: {folder} looks like build output but is not declared", file=sys.stderr)
        if problems:
            print("| | Address | Problem |\n|---|---|---|")
            for path, why in problems:
                print(f"| 🔴 | `{path}` | {why} |")
                print(f"::error::live cache: {path}: {why}", file=sys.stderr)
            return 1
        if n == 0:
            print(("🟡 pages are right, but" if checked else "🟡") +
                  " no hashed file was checked (none found in the published files).")
            print("::warning::live cache: no hashed file was checked", file=sys.stderr)
        else:
            if n < total:
                print(f"🟡 right Cache-Control on the {len(checked)} pages and on a sample of {n} of {total} hashed "
                      "files (the sample changes every run).")
            else:
                print("🟢 every page and hashed file has the right Cache-Control.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
