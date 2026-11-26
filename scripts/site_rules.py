#!/usr/bin/env python3
"""Static rules on a built site (no browser): cache headers, fonts, images.

  site_rules.py check <site dir> --mode absolute|baseline
                [--config web-budget.json] [--headers <site>/_headers] > rules.json

Cache (Cloudflare `_headers` file, the one the site publishes):
  cache-headers-file  the file is missing
  cache-hashed        a file with a content hash in its name (js, css, fonts,
                      images) is not served with max-age >= 1 year + immutable
  cache-html          an HTML page is cached long (max-age > 600 s, or immutable)
Fonts:
  font-cdn            a font or font stylesheet from another host (Google
                      Fonts, Typekit...): fonts are served by the site itself
  font-format         a font that is not WOFF2
  font-weights        more weights of one family than the limit (default 2)
  font-subset         a WOFF2 maps characters outside the Latin script
                      (Greek, Cyrillic, icons...): ship a Latin subset
Images:
  img-alt             <img> without alt (alt="" is fine for decoration)
  img-size            <img> without width and height (or an inline
                      aspect-ratio): the page jumps while it loads
  img-format          a raster image in a page or stylesheet that is not WebP
                      or AVIF (SVG is fine)
  img-lazy            an <img> after the first ones (limit img_eager, default
                      2) without loading="lazy"
  img-px, img-kb      an image file over 1600 px on its longer side or 300 KB

Limits and exceptions come from web-budget.json (budget.py: only stricter
limits; an exception names the rule or limit, the files, a reason, who
approved it and when). A case covered by an exception is listed apart and
does not count. The verdict is web_health.py compare's: absolute = any case
fails; baseline = a new case fails.
"""
import argparse
import datetime
import fnmatch
import json
import os
import re
import struct
import subprocess
import sys
from html.parser import HTMLParser

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import budget  # noqa: E402

YEAR = 31536000
HTML_MAX_AGE = 600
RASTER = (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tif", ".tiff")
MODERN_IMG = (".webp", ".avif", ".svg")
IMAGE_FILES = RASTER + (".webp", ".avif", ".svg")
FONT_FILES = (".woff2", ".woff", ".ttf", ".otf", ".eot")
HASHABLE = (".js", ".mjs", ".css") + FONT_FILES + IMAGE_FILES + (".ico",)
FONT_HOSTS = ("fonts.googleapis.com", "fonts.gstatic.com", "use.typekit.net", "p.typekit.net",
              "fonts.bunny.net", "fast.fonts.net", "cloud.typography.com", "use.fontawesome.com",
              "kit.fontawesome.com", "fonts.cdnfonts.com", "cdn.jsdelivr.net/npm/@fontsource")


# Folders that only a build tool writes (Astro, Next.js, SvelteKit): every
# file there is content-hashed, so the folder decides. Others are added with
# "cache": {"hashed_dirs": [...]} or hashed_extra.
HASHED_DIRS = ("_astro/", "_next/static/", "_app/immutable/")
# assets/ is Vite's output, but also a plain folder in hand-written sites, and
# a year of immutable cache on a file that keeps its name cannot be undone in
# a browser. So the kit never guesses: assets/ (or any other folder) is checked
# only when the repository declares it in "cache": {"hashed_dirs": [...]}. A
# folder that looks like build output but is not declared gets a note in the
# panel (it does not fail).
LOOKS_BUILD_SHARE = 0.8
LOOKS_BUILD_MIN = 3

# Next.js: 16 or more hex digits (a plain number is not one) and the build id folder.
HEX_HASH = re.compile(r"(^|[-.])(?=\d*[a-f])[0-9a-f]{16,}(?=[-.])")
BUILD_ID = re.compile(r"(?<=_next/static/)(?!chunks/|css/|media/)[A-Za-z0-9_-]{16,}(?=/)")
# Where a short hash sits (8 URL-safe characters): Astro and SvelteKit
# name.HASH.ext (Astro images add _suffix), Vite name-HASH.ext, SvelteKit
# chunks HASH.ext.
SHORT_HASH = (re.compile(r"(\.)([A-Za-z0-9_-]{8})(?=(?:_[A-Za-z0-9_-]+|\.chunk)?\.[A-Za-z0-9]+$)"),
              re.compile(r"(-)([A-Za-z0-9_-]{8})(?=\.[A-Za-z0-9]+$)"),
              re.compile(r"(^)([A-Za-z0-9_-]{8})(?=\.[A-Za-z0-9]+$)"))
# A piece that reads as a word, a number or a camera name, not as a hash.
WORD = re.compile(r"[A-Z]?[a-z]*|[A-Z]+|\d+|[A-Z]?[a-z]+\d+|(?:[A-Z]?[a-z]{2,})(?:[A-Z][a-z]{2,})*\d*")
HEX8 = re.compile(r"(?=.*\d)(?=.*[a-f])[0-9a-f]{8}")
# A whole 8-character name made of lower-case words (main-app, polyfill) is a
# name even in a build folder; a random hash looks like this about 1 in 1000.
WORDY = re.compile(r"[a-z]+(?:[-_][a-z]+)*")


def looks_hashed(token):
    """For declared folders: hex, or upper case mixed with lower case or digits
    (sizes like 1280x720 or 800w are neither), and not made of words."""
    if HEX8.fullmatch(token):
        return True
    if not (re.search(r"[A-Z]", token) and re.search(r"[a-z0-9]", token)):
        return False
    return not all(WORD.fullmatch(piece) for piece in re.split(r"[-_]", token))


def unhash_name(name, guess=False):
    """The file name with its build hash replaced by [hash]; the rest stays.
    In a kit build folder the position says it is a hash; guess=True (a folder
    declared in hashed_dirs) also asks the characters to look like one."""
    new = HEX_HASH.sub(lambda m: (".[hash]" if m.group(1) else "[hash]"), name)
    if new != name:
        return new
    for i, pattern in enumerate(SHORT_HASH):
        m = pattern.search(name)
        if not m:
            continue
        token = m.group(2)
        # In a kit build folder the position is enough, except a whole name of
        # 8 characters (SvelteKit chunks) made of words: main-app.js is a name.
        if looks_hashed(token) if guess else not (i == 2 and WORDY.fullmatch(token)):
            mark = ".[hash]" if m.group(1) else "[hash]"
            return name[:m.start()] + mark + name[m.end():]
    return name


def in_dirs(rel, dirs):
    return any(("/" + d) in ("/" + rel) for d in dirs)


def looks_like_build(files, dirs=HASHED_DIRS):
    """Folders named assets/ (any depth) that are not declared but where most
    names carry a hash: [(folder, hashed, total)], one entry per folder."""
    groups = {}
    for f in files:
        f = f.replace(os.sep, "/")
        if not f.lower().endswith(HASHABLE) or in_dirs(f, dirs):
            continue
        k = f.rfind("assets/")
        if k < 0 or (k > 0 and f[k - 1] != "/"):
            continue
        folder, name = f[:k + len("assets/")], f[k + len("assets/"):]
        if "/" in name:
            continue
        groups.setdefault(folder, []).append(name)
    out = []
    for folder, names in sorted(groups.items()):
        hits = sum(unhash_name(n, guess=True) != n for n in names)
        if len(names) >= LOOKS_BUILD_MIN and hits >= LOOKS_BUILD_SHARE * len(names):
            out.append((folder, hits, len(names)))
    return out


def is_hashed(rel, extra, dirs=HASHED_DIRS):
    """A file in a build folder (the kit's or a declared one) or matched by
    hashed_extra: it must be cached for a year, immutable."""
    rel = rel.replace(os.sep, "/").lstrip("/")
    if not rel.lower().endswith(HASHABLE):
        return False
    return any(re.search(p, rel) for p in extra) or in_dirs(rel, dirs)


def unhash(text, fixed=HASHED_DIRS, declared=()):
    """The same file across builds: the build hash in a name becomes [hash],
    so "never worse" compares cases, not hashes. In the kit's build folders the
    position says it is a hash; in a declared folder (or one that looks like
    build output) the characters must also look like one (photo-mountain.png
    keeps its name). Elsewhere names stay."""
    def one(m):
        url = m.group(0)
        sure = in_dirs(url, fixed)
        if not sure and not in_dirs(url, declared):
            return url
        path, mark, query = url.partition("?")
        folder, slash, name = BUILD_ID.sub("[hash]", path).rpartition("/")
        return folder + slash + unhash_name(name, guess=not sure) + mark + query
    return re.sub(r"[^\s'\"()|,]+", one, text)


# --- cache ---------------------------------------------------------------------

def parse_headers(path):
    """Cloudflare _headers: a URL pattern line, then indented 'Name: value'
    lines; '! Name' detaches a header set by an earlier rule."""
    rules = []
    for n, ln in enumerate(open(path, errors="replace"), 1):
        if not ln.strip() or ln.lstrip().startswith("#"):
            continue
        if not ln[0].isspace():
            pat = re.sub(r"^https?://[^/]+", "", ln.strip())
            rules.append({"pattern": pat, "line": n, "set": [], "detach": []})
        elif rules:
            body = ln.strip()
            if body.startswith("!"):
                rules[-1]["detach"].append(body[1:].strip().lower())
            elif ":" in body:
                name, value = body.split(":", 1)
                rules[-1]["set"].append((name.strip().lower(), value.strip()))
    return rules


def pattern_re(pat):
    out = ""
    for part in re.split(r"(\*|:[A-Za-z]\w*)", pat):
        if part == "*":
            out += ".*"
        elif part.startswith(":") and len(part) > 1:
            out += "[^/]+"
        else:
            out += re.escape(part)
    return re.compile("^" + out + "$")


def cache_control(url, rules):
    values = []
    for r in rules:
        if not pattern_re(r["pattern"]).match(url):
            continue
        if "cache-control" in r["detach"]:
            values = []
        values += [v for k, v in r["set"] if k == "cache-control"]
    return ", ".join(values)


def max_age(cc, shared=False):
    """max-age (the browser); shared=True: the longest of max-age and
    s-maxage (a CDN keeps the page that long)."""
    names = r"(?:max-age|s-maxage)" if shared else r"max-age"
    m = re.findall(r"(?:^|[,\s])" + names + r"\s*=\s*(\d+)", cc, re.I)
    return max(int(x) for x in m) if m else None


def page_url(rel, mount=""):
    url = mount + "/" + rel.replace(os.sep, "/")
    if url.endswith("/index.html"):
        return url[: -len("index.html")]
    return url[: -len(".html")] if url.endswith(".html") else url


def cache_cases(site, files, headers, extra_hashed, mount="", dirs=HASHED_DIRS):
    if not os.path.exists(headers):
        return [case(os.path.relpath(headers, site) if headers.startswith(site) else headers,
                     "cache-headers-file", 0, "no _headers file: hashed files cannot be cached for a year")]
    rules = parse_headers(headers)
    out = []
    for rel in files:
        if rel.endswith(".html"):
            url = page_url(rel, mount)
            cc = cache_control(url, rules)
            age = max_age(cc, shared=True)
            if "immutable" in cc.lower() or (age is not None and age > HTML_MAX_AGE):
                out.append(case(rel, "cache-html", 0, "", f"{url}: Cache-Control '{cc}' (HTML: max-age <= {HTML_MAX_AGE})"))
        elif is_hashed(rel, extra_hashed, dirs):
            url = mount + "/" + rel.replace(os.sep, "/")
            cc = cache_control(url, rules)
            age = max_age(cc) or 0
            if age < YEAR or "immutable" not in cc.lower():
                out.append(case(rel, "cache-hashed", 0, "",
                                f"Cache-Control '{cc or 'none'}' (needs max-age={YEAR}, immutable)"))
    return out


# --- HTML and CSS ----------------------------------------------------------------

class Page(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tags = []
        self.styles = []
        self._style = None

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v if v is not None else "") for k, v in attrs}
        line = self.getpos()[0]
        self.tags.append((tag, a, line))
        if tag == "style":
            self._style = (line, [])

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_data(self, data):
        if self._style:
            self._style[1].append(data)

    def handle_endtag(self, tag):
        if tag == "style" and self._style:
            self.styles.append((self._style[0], "".join(self._style[1])))
            self._style = None


def is_external(url):
    return bool(re.match(r"^(https?:)?//", url.strip(), re.I))


def path_of(url):
    return re.split(r"[?#]", url.strip(), maxsplit=1)[0]


def srcset_urls(value):
    return [p.strip().split()[0] for p in value.split(",") if p.strip()]


def strip_comments(css):
    """CSS without /* comments */, same length (offsets and lines stay right)."""
    return re.sub(r"/\*.*?\*/", lambda m: re.sub(r"[^\n]", " ", m.group(0)), css, flags=re.S)


def declarations(body, prop):
    """Every value of `prop` in a declaration block; a ";" inside url(...) or
    quotes (data: URIs) does not end the value."""
    out = []
    for m in re.finditer(r"(?:^|[;{\s])" + prop + r"\s*:", body, re.I):
        i, depth, quote = m.end(), 0, ""
        while i < len(body):
            ch = body[i]
            if quote:
                quote = "" if ch == quote else quote
            elif ch in "'\"":
                quote = ch
            elif ch == "(":
                depth += 1
            elif ch == ")":
                depth = max(0, depth - 1)
            elif ch == ";" and depth == 0:
                break
            i += 1
        out.append(body[m.end():i])
    return out


def font_faces(css):
    """(family, weight, [src values], start, end) per @font-face block."""
    out = []
    for m in re.finditer(r"@font-face\s*{([^}]*)}", css, re.I):
        body = m.group(1)
        fam = declarations(body, "font-family")
        wt = declarations(body, "font-weight")
        weight = (wt[-1].strip().lower() if wt else "400")
        weight = {"normal": "400", "bold": "700"}.get(weight, weight)
        out.append((fam[-1].strip().strip("'\"").lower() if fam else "?", weight,
                    declarations(body, "src"), m.start(), m.end()))
    return out


def line_at(text, offset, base=1):
    return base + text.count("\n", 0, offset)


CSS_URL = re.compile(r"url\(\s*(?:'([^']*)'|\"([^\"]*)\"|([^)\s]*))\s*\)")


def css_urls(text):
    for m in CSS_URL.finditer(text):
        yield m.start(), (m.group(1) or m.group(2) or m.group(3) or "").strip()


def css_cases(rel, css, base_line, faces):
    css = strip_comments(css)
    out = []
    for m in re.finditer(r"@import\s+(?:url\()?\s*['\"]?([^'\")\s;]+)", css, re.I):
        if any(h in m.group(1) for h in FONT_HOSTS):
            out.append(case(rel, "font-cdn", line_at(css, m.start(), base_line), m.group(1)))
    spans = []
    for fam, weight, srcs, start, end in font_faces(css):
        spans.append((start, end))
        ln = line_at(css, start, base_line)
        faces.append((fam, weight, rel, ln))
        for src in srcs:
            for _, url in css_urls(src):
                if is_external(url):
                    out.append(case(rel, "font-cdn", ln, url))
                elif url.startswith("data:"):
                    if not url.lower().startswith(("data:font/woff2", "data:application/font-woff2")):
                        out.append(case(rel, "font-format", ln, url.split(",")[0][:40]))
                elif not path_of(url).lower().endswith(".woff2"):
                    out.append(case(rel, "font-format", ln, url))
    for off, url in css_urls(css):
        if any(a <= off < b for a, b in spans):
            continue
        u = path_of(url)
        if not u.startswith("data:") and u.lower().endswith(RASTER):
            out.append(case(rel, "img-format", line_at(css, off, base_line), u))
    return out


def html_cases(rel, text, eager, faces):
    p = Page()
    p.feed(text)
    out = []
    n_img = 0
    for tag, a, line in p.tags:
        if tag == "link":
            href = a.get("href", "")
            if any(h in href for h in FONT_HOSTS):
                out.append(case(rel, "font-cdn", line, href))
        elif tag == "img":
            n_img += 1
            src = a.get("src", "")
            if "alt" not in a:
                out.append(case(rel, "img-alt", line, src))
            has_ratio = "aspect-ratio" in a.get("style", "").lower()
            if not (a.get("width") and a.get("height")) and not has_ratio:
                out.append(case(rel, "img-size", line, src))
            for u in [src] + srcset_urls(a.get("srcset", "")):
                if u and not u.startswith("data:") and path_of(u).lower().endswith(RASTER):
                    out.append(case(rel, "img-format", line, u))
            if n_img > eager and a.get("loading", "").lower() != "lazy":
                out.append(case(rel, "img-lazy", line, src, f"image {n_img} on the page"))
        elif tag == "source":
            for u in srcset_urls(a.get("srcset", "")):
                if not u.startswith("data:") and path_of(u).lower().endswith(RASTER):
                    out.append(case(rel, "img-format", line, u))
    for base_line, css in p.styles:
        out += css_cases(rel, css, base_line, faces)
    for tag, a, line in p.tags:
        if "style" in a and "url(" in a["style"]:
            for _, url in css_urls(a["style"]):
                u = path_of(url)
                if not u.startswith("data:") and u.lower().endswith(RASTER):
                    out.append(case(rel, "img-format", line, u))
    return out


# --- files ---------------------------------------------------------------------

def image_size(path):
    """(width, height) of PNG, JPEG, GIF, WebP and AVIF; None if unknown."""
    with open(path, "rb") as f:
        head = f.read(64)
        if head[:8] == b"\x89PNG\r\n\x1a\n":
            return struct.unpack(">II", head[16:24])
        if head[:6] in (b"GIF87a", b"GIF89a"):
            return struct.unpack("<HH", head[6:10])
        if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
            kind = head[12:16]
            if kind == b"VP8 ":
                w, h = struct.unpack("<HH", head[26:30])
                return w & 0x3FFF, h & 0x3FFF
            if kind == b"VP8L":
                b = head[21:25]
                bits = int.from_bytes(b, "little")
                return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
            if kind == b"VP8X":
                return int.from_bytes(head[24:27], "little") + 1, int.from_bytes(head[27:30], "little") + 1
        f.seek(0)
        data = f.read()
    if data[:2] == b"\xff\xd8":
        i = 2
        while i + 9 < len(data):
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                h, w = struct.unpack(">HH", data[i + 5:i + 9])
                return w, h
            if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                i += 2
                continue
            i += 2 + struct.unpack(">H", data[i + 2:i + 4])[0]
        return None
    if data[4:8] == b"ftyp" and b"avi" in data[8:32]:
        k = data.find(b"ispe")
        if k > 0:
            return struct.unpack(">II", data[k + 8:k + 16])
    return None


# Unicode blocks of a Latin text font (Google Fonts' latin + latin-ext): Basic
# Latin to Latin Extended-B, IPA and spacing modifiers, combining marks,
# phonetic extensions, Latin Extended Additional/C/D, general
# punctuation to miscellaneous symbols and arrows, Latin ligatures, BOM.
LATIN = [(0x0000, 0x036F), (0x1D00, 0x1DBF), (0x1E00, 0x1EFF), (0x2000, 0x2BFF), (0x2C60, 0x2C7F), (0xA720, 0xA7FF),
         (0xFB00, 0xFB06), (0xFEFF, 0xFEFF), (0xFFFC, 0xFFFD)]
BLOCKS = [(0x0370, 0x03FF, "Greek"), (0x1F00, 0x1FFF, "Greek"), (0x0400, 0x052F, "Cyrillic"),
          (0x1C80, 0x1C8F, "Cyrillic"), (0x2DE0, 0x2DFF, "Cyrillic"), (0xA640, 0xA69F, "Cyrillic"),
          (0x0590, 0x05FF, "Hebrew"), (0x0600, 0x06FF, "Arabic"), (0x0900, 0x0DFF, "Indic"),
          (0x3000, 0x9FFF, "CJK"), (0xAC00, 0xD7AF, "Hangul"), (0xE000, 0xF8FF, "private use (icons)")]


def outside_latin(ranges):
    names = set()
    count = 0
    for start, end in ranges:
        cp = start
        while cp <= end:
            inside = next((hi for lo, hi in LATIN if lo <= cp <= hi), None)
            if inside is not None:
                cp = inside + 1
                continue
            nxt = min([lo for lo, _ in LATIN if lo > cp] + [end + 1])
            stop = min(end, nxt - 1)
            count += stop - cp + 1
            for lo, hi, name in BLOCKS:
                if lo <= stop and cp <= hi:
                    names.add(name)
            if not any(lo <= stop and cp <= hi for lo, hi, _ in BLOCKS):
                names.add("other")
            cp = stop + 1
    return count, sorted(names)


def font_subset_cases(site, fonts):
    if not fonts:
        return []
    here = os.path.dirname(os.path.abspath(__file__))
    try:
        r = subprocess.run(["node", os.path.join(here, "font_cmap.mjs"), *[os.path.join(site, f) for f in fonts]],
                           capture_output=True, text=True)
    except FileNotFoundError:
        return [case(f, "font-subset", 0, "could not read the font", "node not found: the WOFF2 reader needs Node")
                for f in fonts]
    if r.returncode != 0:
        return [case(f, "font-subset", 0, "could not read the font", r.stderr.strip()[:120]) for f in fonts]
    try:
        data = json.loads(r.stdout)
    except ValueError:
        data = {}
    if not data:
        return [case(f, "font-subset", 0, "could not read the font", "the WOFF2 reader gave no output") for f in fonts]
    out = []
    for f in fonts:
        info = data.get(os.path.join(site, f), {})
        if "error" in info:
            out.append(case(f, "font-subset", 0, "could not read the font", info["error"]))
            continue
        count, names = outside_latin(info.get("ranges", []))
        if count:
            out.append(case(f, "font-subset", 0, "outside Latin: " + ", ".join(names), f"{count} characters"))
    return out


def case(file, rule, line, text, detail=""):
    """A case; file + rule + text is its identity against main, so the text
    holds no number that moves on its own (sizes, counts, positions go in
    detail)."""
    return {"file": file, "rule": rule, "line": line, "text": str(text)[:200], "detail": str(detail)[:200]}


GROUP = {"cache": "cache", "font": "fonts", "img": "images"}


def check(a):
    today = datetime.date.fromisoformat(a.today) if a.today else datetime.date.today()
    cfg = budget.load(a.config, today, a.mode)
    limits, exceptions, errors = cfg["limits"], cfg["exceptions"], cfg["errors"]
    site = os.path.abspath(a.site)
    files = []
    for root, dirs, names in os.walk(site):
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d != "node_modules")
        for n in sorted(names):
            files.append(os.path.relpath(os.path.join(root, n), site))
    declared = tuple(cfg["hashed_dirs"])
    dirs = HASHED_DIRS + declared
    cases = cache_cases(site, files, a.headers or os.path.join(site, "_headers"), cfg["hashed"],
                        a.mount.rstrip("/"), dirs)
    built = looks_like_build(files, dirs)
    notes = [f"{folder} looks like build output ({hits} of {total} names carry a hash) but is not declared: "
             f"add \"{folder}\" to \"cache\": {{\"hashed_dirs\": [...]}} so its cache is checked"
             for folder, hits, total in built]
    # Identity (not cache): names in such a folder lose their hash too, so a
    # rebuild of an undeclared Vite site does not show old cases as new ones.
    same = declared + tuple(folder for folder, _, _ in built)
    faces = []
    for rel in files:
        low = rel.lower()
        full = os.path.join(site, rel)
        if low.endswith(".html"):
            cases += html_cases(rel, open(full, errors="replace").read(), limits["img_eager"], faces)
        elif low.endswith(".css"):
            cases += css_cases(rel, open(full, errors="replace").read(), 1, faces)
        if low.endswith(FONT_FILES) and not low.endswith(".woff2"):
            cases.append(case(rel, "font-format", 0, "font file that is not WOFF2"))
        if low.endswith(IMAGE_FILES):
            kb = os.path.getsize(full) / 1024
            lim_kb, _ = budget.limit_for("img_kb", rel, limits, exceptions)
            if kb > lim_kb:
                cases.append(case(rel, "img-kb", 0, "", f"{kb:.0f} KB (limit {lim_kb:g} KB)"))
            try:
                size = None if low.endswith(".svg") else image_size(full)
            except (struct.error, OSError, IndexError) as e:
                cases.append(case(rel, "img-px", 0, "unreadable image", str(e)[:80]))
                size = None
            lim_px, _ = budget.limit_for("img_px", rel, limits, exceptions)
            if size and max(size) > lim_px:
                cases.append(case(rel, "img-px", 0, "", f"{size[0]}x{size[1]} px (limit {lim_px:g} px)"))
    weights = {}
    for fam, weight, rel, line in faces:
        weights.setdefault(fam, {}).setdefault(weight, (rel, line))
    for fam, ws in sorted(weights.items()):
        lim, _ = budget.limit_for("font_weights", fam, limits, exceptions)
        if len(ws) > lim:
            # One case per weight over the limit: one more weight = one more case.
            for w in sorted(ws)[int(lim):]:
                rel, line = ws[w]
                cases.append(case(rel, "font-weights", line, f"{fam} weight {w}",
                                  f"{len(ws)} weights ({', '.join(sorted(ws))}); limit {lim:g}"))
    cases += font_subset_cases(site, [f for f in files if f.lower().endswith(".woff2")])
    out = {"mode": a.mode, "errors": errors, "notes": notes, "cache": [], "fonts": [], "images": [], "excepted": []}
    for c in cases:
        exc = budget.rule_exception(c["rule"], c["file"], exceptions)
        c = dict(c, file=unhash(c["file"], HASHED_DIRS, same), text=unhash(c["text"], HASHED_DIRS, same))
        if exc:
            out["excepted"].append(dict(c, reason=exc["reason"]))
        else:
            out[GROUP[c["rule"].split("-")[0]]].append(c)
    print(json.dumps(out, ensure_ascii=False))
    return 0


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check")
    c.add_argument("site")
    c.add_argument("--mode", choices=["absolute", "baseline"], required=True)
    c.add_argument("--config", default="web-budget.json")
    c.add_argument("--headers", help="the published _headers file (default: <site>/_headers)")
    c.add_argument("--mount", default="", help="the site is served under this path (e.g. /treinos)")
    c.add_argument("--today", help="YYYY-MM-DD (tests)")
    return check(p.parse_args())


if __name__ == "__main__":
    sys.exit(main())
