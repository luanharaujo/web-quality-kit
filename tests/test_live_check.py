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

    def log_message(self, *args):
        pass

    def end_headers(self):
        path = self.path.split("?")[0]
        for prefix, value in self.rules.items():
            if path.startswith(prefix):
                self.send_header("Cache-Control", value)
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
        # --cache without the published files is refused.
        r = subprocess.run([sys.executable, CHECK, "--base-url", base, "--cache"], capture_output=True, text=True)
        assert r.returncode == 2, r.stderr
    finally:
        server.shutdown()
        shutil.rmtree(tmp)
    print("live_check: ok")


if __name__ == "__main__":
    main()
