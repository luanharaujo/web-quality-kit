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
    def log_message(self, *args):
        pass


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
        args += ["--path", p]
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
    finally:
        server.shutdown()
        shutil.rmtree(tmp)
    print("live_check: ok")


if __name__ == "__main__":
    main()
