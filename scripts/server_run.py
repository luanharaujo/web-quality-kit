#!/usr/bin/env python3
"""Start a local server for the kit's server checks (server_queries.py,
server_observability.py): a free port, the start command with {port} and {tmp}
filled in, a wait until it answers, and a clean stop of the whole process group.
The server's output goes to <tmp>/server.log (the observability check reads its
log lines there)."""
import os
import re
import socket
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request


def ci_floor(seconds, floor):
    """In CI (CI=true) a slow or busy machine must not turn a check red: no
    wait is under `floor` seconds there."""
    if os.environ.get("CI", "").lower() in ("true", "1"):
        return max(seconds, floor)
    return seconds


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def fill(text, **kw):
    for k, v in kw.items():
        text = text.replace("{" + k + "}", str(v))
    return text


# What may show in the panel from the server's or a command's output. Only
# whole lines are kept: the end of the output is taken at a line boundary (a
# cut inside a line could drop the word "Bearer" and keep the token), a line
# too long to check is dropped, the lines are cleaned, and only then is the
# result cut to `keep` characters.
CREDENTIAL = re.compile(r"(?i)(authorization|cookie|token|password|passwd|secret|bearer|api[_-]?key|session[_-]?id)[^\n]*")
# Linear patterns: long output never makes these slow.
EMAIL = re.compile(r"[\w.+-]{1,64}@[\w-]{1,63}(?:\.[\w-]{1,63}){0,7}\.[A-Za-z]{2,24}\b")
DIGIT_RUN = re.compile(r"(?<!\d)\d{8,}(?!\d)")  # any length
DIGITS_SPLIT = re.compile(r"(?<!\d)\d[\d.\s()-]{6,40}\d(?!\d)")  # phones, CPF with separators
SCAN = 8000  # how much of the end of the output is looked at
LONG_LINE = 2000  # a longer line is dropped, not shown


def redact(text, keep=400):
    if len(text) > SCAN:
        text = text[-SCAN:]
        text = text[text.find("\n") + 1:] if "\n" in text else ""  # drop the partial first line
    lines = ["[a long line removed]" if len(line) > LONG_LINE else line for line in text.split("\n")]
    text = CREDENTIAL.sub(r"\1 ***", "\n".join(lines))
    text = DIGITS_SPLIT.sub("***", DIGIT_RUN.sub("***", EMAIL.sub("***", text)))
    return text[-keep:]


def run(cmd, root, what):
    out = subprocess.run(cmd, shell=True, cwd=root, capture_output=True, text=True)
    if out.returncode:
        raise RuntimeError(f"{what} failed (exit {out.returncode}): {redact((out.stderr or out.stdout).strip())}")
    return out.stdout


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A 3xx is the server's answer: never follow it to another host (the
    login headers of the checks would go along)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


OPENER = urllib.request.build_opener(_NoRedirect)


def get(url, headers, timeout=30, body=False):
    """(status, headers), or (status, headers, body) with body=True, of one
    GET. A 3xx is returned as the answer (its headers are the route's own),
    never followed; 4xx and 5xx raise HTTPError."""
    req = urllib.request.Request(url, headers=headers)
    try:
        with OPENER.open(req, timeout=ci_floor(timeout, 60)) as r:  # nosec B310: local server, http(s) only
            data = r.read()
            return (r.status, r.headers, data) if body else (r.status, r.headers)
    except urllib.error.HTTPError as e:
        if 300 <= e.code < 400:
            data = e.read()
            return (e.code, e.headers, data) if body else (e.code, e.headers)
        raise


class Server:
    def __init__(self, cfg, root, tmp):
        srv = cfg["server"]
        self.port = free_port()
        self.base = fill(srv["url"], port=self.port).rstrip("/")
        parts = urllib.parse.urlsplit(self.base)
        if parts.scheme != "http" or parts.hostname not in ("127.0.0.1", "localhost"):
            raise RuntimeError("server.url must be a local address (127.0.0.1 or localhost)")
        self.log = open(os.path.join(tmp, "server.log"), "w")
        self.proc = subprocess.Popen(fill(srv["start"], port=self.port, tmp=tmp), shell=True, cwd=root,
                                     stdout=self.log, stderr=subprocess.STDOUT, start_new_session=True)
        ready = self.base + srv.get("ready", "/")
        deadline = time.time() + ci_floor(float(srv.get("timeout", 90)), 120)
        while time.time() < deadline:
            if self.proc.poll() is not None:
                self.fail(f"the server stopped (exit {self.proc.returncode})")
            try:
                get(ready, {}, timeout=5)  # in CI: 60 s (get)
                return
            except (urllib.error.URLError, OSError):
                time.sleep(0.5)
        self.fail(f"the server did not answer {ready}")

    def fail(self, why):
        # Never leave the server running when it did not get ready.
        text = self.tail()
        self.stop()
        raise RuntimeError(f"{why}: {text}")

    def tail(self):
        self.log.flush()
        with open(self.log.name) as f:
            return redact(f.read())

    def stop(self):
        try:
            os.killpg(self.proc.pid, 15)
            self.proc.wait(10)
        except (ProcessLookupError, subprocess.TimeoutExpired, PermissionError):
            try:
                os.killpg(self.proc.pid, 9)
            except (ProcessLookupError, PermissionError):
                pass
        self.log.close()
