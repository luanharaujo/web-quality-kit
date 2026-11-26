#!/usr/bin/env python3
"""Proof of the live check: a site that matches passes; a stale page (the
CDN still has the old one) or a missing page fails after the last round."""
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
CHECK = os.path.join(HERE, "..", "scripts", "live_check.py")


class Quiet(SimpleHTTPRequestHandler):
    # Cache-Control per path prefix, like a CDN applying a _headers file.
    rules = {}
    flaky = set()   # paths that answer 503 once, like a CDN blip

    def log_message(self, *args):
        pass

    def translate_path(self, path):
        # Clean URLs, like static hosts: /about serves about.html.
        full = super().translate_path(path)
        if not os.path.splitext(full)[1] and not os.path.exists(full) and os.path.isfile(full + ".html"):
            return full + ".html"
        return full

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in self.flaky:
            self.flaky.discard(path)
            self.send_error(503)
            return
        super().do_GET()

    def end_headers(self):
        path = self.path.split("?")[0]
        for prefix, value in self.rules.items():
            if path.startswith(prefix):
                for v in value if isinstance(value, list) else [value]:
                    self.send_header("Cache-Control", v)
                break
        super().end_headers()


def serve(folder):
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(Quiet, directory=folder))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def write(folder, files):
    for name, text in files.items():
        path = os.path.join(folder, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        open(path, "w").write(text)


def run(base, site, *paths):
    args = [sys.executable, CHECK, "--base-url", base, "--site-dir", site, "--tries", "2", "--wait", "0.1"]
    for p in paths:
        args += [p] if p.startswith("--") else ["--path", p]
    return subprocess.run(args, capture_output=True, text=True)


def main():
    sys.path.insert(0, os.path.join(HERE, "..", "scripts"))
    import live_check
    got = live_check.spread(["/_astro/a.js", "/_astro/b.js", "/_astro/c.js", "/assets/x.js"], 2)
    assert got == ["/_astro/a.js", "/assets/x.js"], got  # a declared folder is not hidden by a big one
    many = [f"/_astro/{i:03d}.js" for i in range(100)]
    picks = {tuple(live_check.spread(many, 30, f"run-{k}")) for k in range(5)}
    assert len(picks) > 1, picks  # the sample moves from run to run
    tmp = tempfile.mkdtemp()
    live, published = os.path.join(tmp, "live"), os.path.join(tmp, "published")
    files = {"index.html": "<h1>cover</h1>", "adm/versao.json": '{"versao": "new"}'}
    write(live, files)
    write(published, files)
    server, base = serve(live)
    try:
        r = run(base, published, "/", "/adm/versao.json")
        assert r.returncode == 0, r.stdout + r.stderr
        assert "🔴" not in r.stdout, r.stdout

        write(live, {"adm/versao.json": '{"versao": "old"}'})  # the CDN still has the old one
        r = run(base, published, "/", "/adm/versao.json")
        assert r.returncode == 1 and "older or different" in r.stdout, r.stdout + r.stderr

        r = run(base, published, "/gone/")
        assert r.returncode == 1 and "HTTP 404" in r.stdout, r.stdout

        # Live cache: the host applies the right headers -> passes.
        write(live, {"adm/versao.json": '{"versao": "new"}'})
        asset = {"_astro/app.Ab12Cd34.js": "console.log(1)"}
        write(live, asset)
        write(published, asset)
        Quiet.rules = {"/_astro/": "public, max-age=31536000, immutable", "/": "public, max-age=0, must-revalidate"}
        r = run(base, published, "/", "--cache")
        assert r.returncode == 0 and "1 hashed files" in r.stdout, r.stdout + r.stderr
        # The host ignored _headers: the hashed file gets a short cache -> fails.
        Quiet.rules = {"/": "public, max-age=3600"}
        r = run(base, published, "/", "--cache")
        assert r.returncode == 1 and "_astro/app.Ab12Cd34.js" in r.stdout and "page cached too long" in r.stdout, r.stdout
        # A declared Vite folder (web-budget.json cache.hashed_dirs) is checked
        # live too; undeclared, it is not asked for a year of cache.
        vite = {"assets/index-Bh-3x9aZ.js": "console.log(2)"}
        write(live, vite)
        write(published, vite)
        Quiet.rules = {"/_astro/": "public, max-age=31536000, immutable", "/": "public, max-age=0, must-revalidate"}
        r = run(base, published, "/", "--cache")
        assert r.returncode == 0 and "1 hashed files" in r.stdout, r.stdout + r.stderr
        cfg = os.path.join(tmp, "web-budget.json")
        with open(cfg, "w") as f:
            f.write('{"cache": {"hashed_dirs": ["assets/"]}}')
        r = run(base, published, "/", "--cache", f"--config={cfg}")
        assert r.returncode == 1 and "assets/index-Bh-3x9aZ.js" in r.stdout, r.stdout + r.stderr
        with open(cfg, "w") as f:
            f.write('{"cache": {"hashed_dirs": ["assets"]}}')
        r = run(base, published, "/", "--cache", f"--config={cfg}")
        assert r.returncode == 2 and "hashed_dirs" in r.stderr, r.stderr
        # A name with a space and an accent is asked URL-encoded, not a crash.
        os.remove(os.path.join(live, "assets", "index-Bh-3x9aZ.js"))
        os.remove(os.path.join(published, "assets", "index-Bh-3x9aZ.js"))
        odd = {"_astro/foto ç.Ab12Cd34.png": "png"}
        write(live, odd)
        write(published, odd)
        Quiet.rules = {"/_astro/": "public, max-age=31536000, immutable", "/": "public, max-age=0, must-revalidate"}
        r = run(base, published, "/", "--cache")
        assert r.returncode == 0 and "2 hashed files" in r.stdout, r.stdout + r.stderr
        # One 503 on a hashed file is asked again, not a failed deploy.
        Quiet.flaky = {"/_astro/app.Ab12Cd34.js"}
        r = run(base, published, "/", "--cache")
        assert r.returncode == 0 and not Quiet.flaky, r.stdout + r.stderr
        # s-maxage counts for pages, as in site-rules: a CDN keeps the page a day.
        Quiet.rules = {"/_astro/": "public, max-age=31536000, immutable", "/": "public, max-age=0, s-maxage=86400"}
        r = run(base, published, "/", "--cache")
        assert r.returncode == 1 and "page cached too long" in r.stdout, r.stdout
        # Exceptions registered for site-rules hold live too (cache-html, cache-hashed).
        Quiet.rules = {"/": "public, max-age=3600"}
        with open(cfg, "w") as f:
            f.write('{"exceptions": ['
                    '{"rule": "cache-html", "files": ["index.html"], "reason": "landing page, CDN purge on deploy",'
                    ' "approved_by": "owner", "date": "2026-11-20"},'
                    '{"rule": "cache-hashed", "files": ["_astro/*"], "reason": "host cannot set headers yet",'
                    ' "approved_by": "owner", "date": "2026-11-20"}]}')
        r = run(base, published, "/", "--cache", f"--config={cfg}")
        assert r.returncode == 0 and "0 hashed files" in r.stdout, r.stdout + r.stderr
        # An expired rule exception is a config error (not a crash), judged after
        # the live check: the page table still says the deploy is live.
        with open(cfg, "w") as f:
            f.write('{"exceptions": [{"rule": "cache-html", "files": ["index.html"], "reason": "landing page, CDN purge",'
                    ' "approved_by": "owner", "date": "2026-01-01", "expires": "2026-02-01"}]}')
        r = run(base, published, "/", "--cache", f"--config={cfg}")
        assert r.returncode == 2 and "200, same as published" in r.stdout and "expired" in r.stderr \
            and "Traceback" not in r.stderr, r.stdout + r.stderr
        # Nothing hashed to check, or no config file: yellow, never a silent green.
        bare_live, bare_pub = os.path.join(tmp, "bare-live"), os.path.join(tmp, "bare-pub")
        write(bare_live, {"index.html": "<h1>plain</h1>"})
        write(bare_pub, {"index.html": "<h1>plain</h1>"})
        server2, base2 = serve(bare_live)
        try:
            Quiet.rules = {"/": "public, max-age=0, must-revalidate"}
            r = run(base2, bare_pub, "/", "--cache", "--config=/nonexistent/web-budget.json")
            assert r.returncode == 0 and "no hashed file was checked" in r.stdout and "not found" in r.stdout \
                and "🟢 every" not in r.stdout, r.stdout
            # An undeclared Vite assets/ is named, since its cache is not checked.
            vite2 = {"assets/a-Bh-3x9aZ.js": "1", "assets/b-C38fXH4l.css": "2"}
            write(bare_live, vite2)
            write(bare_pub, vite2)
            r = run(base2, bare_pub, "/", "--cache")
            assert "assets/ looks like build output" in r.stdout and "not declared" in r.stderr, r.stdout + r.stderr
        finally:
            server2.shutdown()
        # A clean URL (/about served by about.html) is compared and its cache asked.
        clean = {"about.html": "<h1>about</h1>"}
        write(live, clean)
        write(published, clean)
        Quiet.rules = {"/_astro/": "public, max-age=31536000, immutable", "/about": "public, max-age=31536000",
                       "/": "public, max-age=0, must-revalidate"}
        r = run(base, published, "/about", "/", "--cache")
        assert r.returncode == 1 and "| `/about` | 200, same as published" in r.stdout \
            and "2 pages" in r.stdout and "page cached too long" in r.stdout, r.stdout + r.stderr
        # A path that is not a page is named as not checked, never counted.
        r = run(base, published, "/", "/adm/versao.json", "--cache")
        assert "1 pages" in r.stdout and "not HTML pages" in r.stdout and "`/adm/versao.json`" in r.stdout, r.stdout
        # Two Cache-Control lines (CDN + origin): both count, the long one fails.
        two = os.path.join(tmp, "two")
        write(two, {"index.html": "<h1>two</h1>"})
        Quiet.rules = {"/": ["public, max-age=86400", "no-cache"]}
        server3, base3 = serve(two)
        try:
            r = run(base3, two, "/", "--cache")
            assert r.returncode == 1 and "page cached too long" in r.stdout, r.stdout + r.stderr
        finally:
            server3.shutdown()
        # The same for a hashed file: a short max-age next to a long one fails.
        Quiet.rules = {"/_astro/": ["public, max-age=60", "public, max-age=31536000, immutable"],
                       "/": "public, max-age=0, must-revalidate"}
        r = run(base, published, "/", "--cache")
        assert r.returncode == 1 and "_astro/app.Ab12Cd34.js" in r.stdout, r.stdout + r.stderr
        # More hashed files than the sample: yellow, with "n of total".
        lots = {f"_astro/f{i:02d}.Ab12Cd34.js": str(i) for i in range(5)}
        write(live, lots)
        write(published, lots)
        Quiet.rules = {"/_astro/": "public, max-age=31536000, immutable", "/": "public, max-age=0, must-revalidate"}
        r = run(base, published, "/", "--cache", "--cache-sample=2")
        assert r.returncode == 0 and "sample of 2 of" in r.stdout and "🟢 every" not in r.stdout, r.stdout
        # The same with an undeclared build folder next to it (its note must not
        # change the count of hashed files).
        side = {"assets/a-Bh-3x9aZ.js": "1", "assets/b-C38fXH4l.css": "2"}
        write(live, side)
        write(published, side)
        r = run(base, published, "/", "--cache", "--cache-sample=2")
        assert "sample of 2 of 7" in r.stdout and "🟢 every" not in r.stdout and "not declared" in r.stdout, r.stdout
        for name in side:
            os.remove(os.path.join(live, name))
            os.remove(os.path.join(published, name))
        # --cache without the published files is refused.
        r = subprocess.run([sys.executable, CHECK, "--base-url", base, "--cache"], capture_output=True, text=True)
        assert r.returncode == 2, r.stderr
    finally:
        server.shutdown()
        shutil.rmtree(tmp)
    print("live_check: ok")


if __name__ == "__main__":
    main()
