#!/usr/bin/env python3
"""Proof of the static site rules: a good site has no case, and each bad
thing planted in a bad site (cache headers, fonts, images) is caught by its
own rule. Exceptions hide only the files they name. The verdict: absolute
fails on any case, baseline only on new ones. Needs Node (WOFF2 reader)."""
import json
import os
import struct
import subprocess
import sys
import tempfile
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
RULES = os.path.join(HERE, "..", "scripts", "site_rules.py")
HEALTH = os.path.join(HERE, "..", "scripts", "web_health.py")

failures = []


def expect(name, cond, detail=""):
    print(("ok   " if cond else "FAIL ") + name)
    if not cond:
        failures.append(f"{name}: {detail}")


def write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb" if isinstance(data, bytes) else "w") as f:
        f.write(data)


def png(w, h, pad_kb=0):
    def chunk(kind, body):
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))
    raw = os.urandom(pad_kb * 1024)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"tEXt", b"pad\x00" + raw) + chunk(b"IEND", b""))


def webp(w, h):
    body = b"VP8X" + struct.pack("<I", 10) + b"\x00\x00\x00\x00" + (w - 1).to_bytes(3, "little") + (h - 1).to_bytes(3, "little")
    return b"RIFF" + struct.pack("<I", 4 + len(body)) + b"WEBP" + body


def woff2(ranges):
    """A WOFF2 with only a cmap (format 12) table: enough for the reader."""
    groups = b"".join(struct.pack(">III", a, b, 1) for a, b in ranges)
    sub = struct.pack(">HHIII", 12, 0, 16 + len(groups), 0, len(ranges)) + groups
    cmap = struct.pack(">HH", 0, 1) + struct.pack(">HHI", 3, 10, 12) + sub
    js = ("const z=require('zlib');process.stdout.write(z.brotliCompressSync(Buffer.from(process.argv[1],'hex'))"
          ".toString('hex'))")
    comp = bytes.fromhex(subprocess.run(["node", "-e", js, cmap.hex()], capture_output=True, text=True, check=True).stdout)
    n = len(cmap)
    b128 = bytes([0x80 | (n >> 7), n & 0x7f]) if n >= 128 else bytes([n])
    directory = bytes([0]) + b128
    header = b"wOF2" + b"\x00\x01\x00\x00" + struct.pack(">IHH", 0, 1, 0) + struct.pack(">II", 0, len(comp))
    header += b"\x00" * (48 - len(header))
    return header + directory + comp


LATIN = [(0x20, 0x7E), (0xA0, 0xFF), (0x2013, 0x2014)]
GOOD_HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Good</title>
<link rel="stylesheet" href="/_astro/index.Ab12Cd34.css"></head><body>
<img src="/_astro/logo.Xy98Zw76.svg" alt="Logo" width="120" height="40">
<img src="/_astro/hero.Qw12Er34.webp" alt="" width="800" height="600" fetchpriority="high">
<img src="/_astro/photo.Ty56Ui78.webp" alt="A photo" style="aspect-ratio: 4 / 3" loading="lazy">
<picture><source srcset="/_astro/b.Op90As12.avif" type="image/avif">
<img src="/_astro/b.Df34Gh56.webp" alt="B" width="10" height="10" loading="lazy"></picture>
</body></html>
"""
GOOD_CSS = """@font-face { font-family: "Body"; font-weight: 400; src: url(/_astro/body.Kl12Mn34.woff2) format("woff2"); }
@font-face { font-family: "Body"; font-weight: 700; src: url(/_astro/body-bold.Pq56Rs78.woff2) format("woff2"); }
@font-face { font-family: "Title"; font-weight: 100 900; src: url(/_astro/title.Tu90Vw12.woff2) format("woff2"); }
.hero { background: url(/_astro/hero.Qw12Er34.webp); }
"""
GOOD_HEADERS = """/_astro/*
  Cache-Control: public, max-age=31536000, immutable
/*
  X-Content-Type-Options: nosniff
"""


def good_site():
    d = tempfile.mkdtemp()
    write(f"{d}/index.html", GOOD_HTML)
    write(f"{d}/about/index.html", GOOD_HTML)
    write(f"{d}/_astro/index.Ab12Cd34.css", GOOD_CSS)
    write(f"{d}/_headers", GOOD_HEADERS)
    write(f"{d}/_astro/logo.Xy98Zw76.svg", "<svg xmlns='http://www.w3.org/2000/svg'/>")
    for name in ("hero.Qw12Er34", "photo.Ty56Ui78", "b.Df34Gh56"):
        write(f"{d}/_astro/{name}.webp", webp(1200, 900))
    write(f"{d}/_astro/b.Op90As12.avif", b"\x00\x00\x00\x1cftypavif" + b"\x00" * 20)
    for name in ("body.Kl12Mn34", "body-bold.Pq56Rs78", "title.Tu90Vw12"):
        write(f"{d}/_astro/{name}.woff2", woff2(LATIN))
    write(f"{d}/favicon.svg", "<svg xmlns='http://www.w3.org/2000/svg'/>")
    return d


def run(site, mode="absolute", config=None):
    cfg = os.path.join(tempfile.mkdtemp(), "web-budget.json")
    if config is not None:
        json.dump(config, open(cfg, "w"))
    r = subprocess.run([sys.executable, RULES, "check", site, "--mode", mode, "--config", cfg, "--today", "2026-11-25"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stderr)
    return json.loads(r.stdout)


def rules_of(out):
    return {(c["rule"], c["file"]) for k in ("cache", "fonts", "images") for c in out[k]}


good = run(good_site())
expect("good site: no case", not rules_of(good) and not good["errors"], rules_of(good) or good["errors"])

# --- bad site: one problem per rule --------------------------------------------
bad = good_site()
write(f"{bad}/_headers", GOOD_HEADERS.replace("max-age=31536000, immutable", "max-age=3600")
      + "/about/\n  Cache-Control: public, max-age=86400\n")
write(f"{bad}/cdn.html", '<!doctype html><html lang="en"><head><title>x</title>'
      '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter"></head><body>'
      '<img src="/a.png" width="1" height="1" alt="a">'
      '<img src="/_astro/hero.Qw12Er34.webp" width="1" height="1">'
      '<img src="/_astro/hero.Qw12Er34.webp" alt="no size">'
      '<img src="/_astro/hero.Qw12Er34.webp" alt="third, not lazy" width="1" height="1">'
      '</body></html>')
write(f"{bad}/_astro/extra.Zx12Cv34.css",
      '@import url("https://fonts.googleapis.com/css2?family=Roboto");\n'
      '@font-face { font-family: Many; font-weight: 300; src: url(/f/many-300.woff2); }\n'
      '@font-face { font-family: Many; font-weight: 400; src: url(/f/many-400.woff) format("woff"); }\n'
      '@font-face { font-family: Many; font-weight: 700; src: url(https://cdn.example.com/many-700.woff2); }\n'
      '.x { background-image: url("/bg.jpg"); }\n')
write(f"{bad}/f/many-300.woff2", woff2(LATIN + [(0x0400, 0x04FF)]))
write(f"{bad}/f/many-400.woff", b"wOFF")
write(f"{bad}/a.png", png(2000, 100))
write(f"{bad}/bg.jpg", b"\xff\xd8\xff\xe0" + b"\x00" * 10)
write(f"{bad}/big.webp", webp(800, 600) + os.urandom(320 * 1024))
os.remove(f"{bad}/_astro/b.Df34Gh56.webp")
write(f"{bad}/_astro/b.Df34Gh56.webp", webp(800, 600))
out = run(bad)
found = rules_of(out)
want = {
    ("cache-hashed", "_astro/index.Ab12Cd34.css"),
    ("cache-html", "about/index.html"),
    ("font-cdn", "cdn.html"),
    ("font-cdn", "_astro/extra.Zx12Cv34.css"),
    ("font-format", "_astro/extra.Zx12Cv34.css"),
    ("font-format", "f/many-400.woff"),
    ("font-weights", "_astro/extra.Zx12Cv34.css"),
    ("font-subset", "f/many-300.woff2"),
    ("img-format", "cdn.html"),
    ("img-format", "_astro/extra.Zx12Cv34.css"),
    ("img-alt", "cdn.html"),
    ("img-size", "cdn.html"),
    ("img-lazy", "cdn.html"),
    ("img-px", "a.png"),
    ("img-kb", "big.webp"),
}
for w in sorted(want):
    expect(f"bad site: {w[0]} caught in {w[1]}", w in found, sorted(found))
# The short cache in _headers covers every hashed file under _astro/.
extra = {f for f in found - want if not (f[0] == "cache-hashed" and f[1].startswith("_astro/"))}
expect("bad site: nothing outside the planted problems", not extra, sorted(extra))
subset = [c for c in out["fonts"] if c["rule"] == "font-subset"]
expect("font-subset names the script", subset and "Cyrillic" in subset[0]["text"], subset)

# _headers missing
nohead = good_site()
os.remove(f"{nohead}/_headers")
expect("no _headers file is a case", ("cache-headers-file", "_headers") in rules_of(run(nohead)), rules_of(run(nohead)))

# Cloudflare detach ("! Cache-Control") drops a long cache set by an earlier rule.
det = good_site()
write(f"{det}/_headers", GOOD_HEADERS + "/_astro/index.Ab12Cd34.css\n  ! Cache-Control\n")
expect("detached Cache-Control is caught", ("cache-hashed", "_astro/index.Ab12Cd34.css") in rules_of(run(det)), "")

# A site served under a path: _headers patterns carry the mount.
mnt = good_site()
write(f"{mnt}/_headers", GOOD_HEADERS.replace("/_astro/*", "/app/_astro/*"))
r = subprocess.run([sys.executable, RULES, "check", mnt, "--mode", "absolute", "--config", "/nonexistent",
                    "--mount", "/app/"], capture_output=True, text=True)
expect("mounted site: patterns with the mount match", not json.loads(r.stdout)["cache"], r.stdout[:300])

# --- limits and exceptions ------------------------------------------------------
strict = run(good_site(), config={"limits": {"img_px": 1000, "font_weights": 1}})
expect("stricter img_px catches a 1200 px image", ("img-px", "_astro/hero.Qw12Er34.webp") in rules_of(strict), rules_of(strict))
expect("stricter font_weights catches two weights", any(c["rule"] == "font-weights" for c in strict["fonts"]), strict["fonts"])
exc = {"rule": "img-px", "files": ["a.png"], "reason": "a print poster kept at full size",
       "approved_by": "owner", "date": "2026-11-20"}
r = run(bad, config={"exceptions": [exc]})
expect("rule exception hides its file", ("img-px", "a.png") not in rules_of(r), "")
expect("rule exception is listed apart", any(c["file"] == "a.png" for c in r["excepted"]), r["excepted"])
expect("rule exception hides nothing else", ("img-kb", "big.webp") in rules_of(r), "")
r = run(bad, config={"exceptions": [{"limit": "img_kb", "value": 400, "files": ["big.webp"],
                                     "reason": "hero photo, measured on a phone", "approved_by": "owner",
                                     "date": "2026-11-20"}]})
expect("limit exception on files raises the limit there", ("img-kb", "big.webp") not in rules_of(r), "")
r = run(good_site(), config={"limits": {"img_kb": 900}})
expect("looser limit is a configuration error", any("looser" in e for e in r["errors"]), r["errors"])
r = run(good_site(), mode="baseline", config={})
expect("baseline without migration is a configuration error", any("migration" in e for e in r["errors"]), r["errors"])


# --- verdict --------------------------------------------------------------------
def compare(base, head, *flags):
    d = tempfile.mkdtemp()
    hp = os.path.join(d, "h.json")
    json.dump({"kit": 1, "metrics": head}, open(hp, "w"))
    bp = "-"
    if base is not None:
        bp = os.path.join(d, "b.json")
        json.dump({"kit": 1, "metrics": base}, open(bp, "w"))
    r = subprocess.run([sys.executable, HEALTH, "compare", bp, hp, *flags], capture_output=True, text=True)
    return r.returncode, r.stdout


def metrics(rules_out):
    return {"site_rules": {"mode": rules_out["mode"], "errors": rules_out["errors"],
                           "excepted": len(rules_out["excepted"])},
            **{k: rules_out[k] for k in ("cache", "fonts", "images")}}


rc, panel = compare(None, metrics(out), "--no-fail")
expect("absolute: any case fails, even first run with --no-fail", rc == 1, panel)
rc, panel = compare(None, metrics(good))
expect("absolute: good site passes", rc == 0, panel)
mig = {"migration": {"reason": "old site, images being converted", "until": "2027-03-01"}}
old = run(bad, mode="baseline", config=mig)
rc, panel = compare(None, metrics(old))
expect("baseline: first run does not fail", rc == 0, panel)
rc, panel = compare(metrics(old), metrics(old))
expect("baseline: same cases as main pass", rc == 0, panel)
write(f"{bad}/new.png", png(10, 10))
write(f"{bad}/index.html", GOOD_HTML.replace("photo.Ty56Ui78.webp", "photo.png"))
rc, panel = compare(metrics(old), metrics(run(bad, mode="baseline", config=mig)))
expect("baseline: a new case fails", rc == 1, panel)
cfg_bad = run(good_site(), config={"limits": {"img_kb": 900}})
rc, panel = compare(None, metrics(cfg_bad), "--no-fail")
expect("configuration error fails in any mode", rc == 1, panel)

if failures:
    print("\n".join(failures), file=sys.stderr)
    sys.exit(1)
print("site rules: ok")
