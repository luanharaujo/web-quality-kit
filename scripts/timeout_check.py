#!/usr/bin/env python3
"""Warn about test time limits that are short and fixed in the code.

In CI (CI=true) a test may run on a slow or busy machine: every test time
limit has a floor of 30 s there, so a red test means a real error. This check
lists the places that keep a shorter limit: a literal under 30 s in a test or
test config, and a vitest or Playwright config whose own defaults (5 s) were
never raised for CI. It only warns (exit 0).

A limit written with the kit's helpers (ciTimeout, ci_timeout, ci_limit,
templates/) or on a line that reads the CI variable is fine. A limit that must stay short (a
test of the time limit itself) says why in a comment on the same line or the
line above: `kit:short-timeout <reason>`.

Usage: timeout_check.py [root] [--format json|md]"""
import argparse
import json
import os
import re
import subprocess
import sys

FLOOR_S = 30
SKIP_DIRS = {"node_modules", ".git", "dist", "build", ".build", "vendor", "coverage", ".wrangler",
             ".venv", "venv", "__pycache__", "DerivedData", "Pods", "playwright-report", "test-results"}
MAX_BYTES = 800_000
MAX_LINE = 2_000  # a longer line is generated code: skipped, so the patterns stay fast

JS = (".js", ".mjs", ".cjs", ".ts", ".mts", ".cts", ".jsx", ".tsx")
SH = (".sh", ".bash", ".zsh")
TEST_DIR = re.compile(r"(^|/)(tests?|__tests__|e2e|spec|\w*Tests)/")
TEST_NAME = re.compile(
    r"(\.(test|spec|e2e)\.[cm]?[jt]sx?$)|(^test[a-z]{0,2}([_-][^/]*)?\.(py|sh|bash|zsh|[cm]?[jt]s)$)"
    r"|(^ci_test[^/]*$)|(_test\.py$)|(^conftest\.py$)|(\w*Tests?\.swift$)")
JS_CONFIG = re.compile(r"^(vitest|vite|jest|playwright)([.-]\w+)*\.config\.[cm]?[jt]s$|^vitest\.workspace\.[cm]?[jt]s$")
PY_CONFIG = {"pytest.ini", "pyproject.toml", "setup.cfg", "tox.ini"}

# A kit helper, or the CI variable really read: the word "CI" in a comment or
# in a text does not hide a short limit.
CI_AWARE = re.compile(r"\b(ciTimeout|vitestTimeouts|playwrightTimeouts|ci_timeout|ci_limit|inCI|isCI|in_ci)\b|"
                      r"\benv\.CI\b|\$\{?CI\b|(\benviron(ment)?(\.get\(|\[)|\bgetenv\(|\benv\[)\s*['\"]CI['\"]")
ALLOW = re.compile(r"kit:short-timeout\s+\S")
COMMENT = {"js": ("//", "*", "/*"), "swift": ("//", "*", "/*"), "py": ("#",), "sh": ("#",), "ini": ("#", ";")}
NUM = r"([0-9][0-9_]*(?:\.[0-9]+)?(?:e[0-9]+)?)"
END = r"(?![\w.])(?!\s*[*+/-])"  # a lone literal, not the start of an expression such as 60 * 1000
ARG = r"(?![\w.])"  # a command line value: what follows may be the next option (-s) or a URL

# (kind, regex, unit in seconds): the first group is the value.
RULES = {
    "js": [
        (r"\b(?:testTimeout|hookTimeout|teardownTimeout|actionTimeout|navigationTimeout|timeout)\s*:\s*" + NUM + END, 0.001),
        (r"(?:\btest\.setTimeout|\.setDefaultTimeout|\.setDefaultNavigationTimeout)\(\s*" + NUM + END, 0.001),
        (r"\bAbortSignal\.timeout\(\s*" + NUM + END, 0.001),
    ],
    "py": [
        (r"\bmark\.timeout\(\s*(?:timeout\s*=\s*)?" + NUM + END, 1),
        (r"\b(?:timeout|timeout_s|timeout_seconds)\s*=\s*" + NUM + END, 1),
        (r"\.settimeout\(\s*" + NUM + END, 1),
    ],
    "sh": [
        # -m 5, -m5 and grouped options such as -fsSm5
        (r"\bcurl\b[^|;&]*?\s(?:--max-time[ =]|--connect-timeout[ =]|-[A-Za-z]{0,8}m)\s*" + NUM + ARG, 1),
        (r"(?:^|[;&|(]\s*)g?timeout\s+(?:-[ks]\s*\S+\s+)*" + NUM + r"s?(?=\s)", 1),
    ],
    "swift": [
        (r"\b(?:waitForExistence|waitForNonExistence|waitForExpectations|fulfillment|wait)\s*\("
         r"[^\n]*?\btimeout\s*:\s*" + NUM + END, 1),
        (r"\bexecutionTimeAllowance\s*=\s*" + NUM + END, 1),
    ],
    "ini": [(r"^\s*timeout\s*=\s*\"?" + NUM, 1)],
}
# Command line flags of the test runners, in any file read.
FLAGS = [(r"--(?:testTimeout|hookTimeout|teardownTimeout|test-timeout)[= ]\s*" + NUM + ARG, 0.001)]
PW_FLAG = (r"\bplaywright\s+test\b[^\n]*?--timeout[= ]\s*" + NUM + ARG, 0.001)
PYTEST_FLAG = (r"\bpytest\b[^\n]*?--timeout[= ]\s*" + NUM + ARG, 1)
# `for _ in $(seq 1 20); do curl ... && break; sleep 0.5; done`: a wait of 10 s.
LOOP = re.compile(r"\bfor\s+\w+\s+in\s+\$\(\s*seq\s+(?:1\s+)?([0-9]+)\s*\)\s*;?\s*do\b(.*?&&\s*break\b.*?)"
                  r"\bsleep\s+([0-9.]+)\s*;?\s*done")
# vitest and Jest: it('name', fn, 5000)
TRAILING = re.compile(r"^(\s*)\}\s*,\s*" + NUM + r"\s*\)\s*;?\s*$")
CASE_OPEN = re.compile(r"^\s*(it|test|beforeAll|beforeEach|afterAll|afterEach)(\.\w+(\([^)]*\))?)*\(")

WEB = "web-quality-kit templates/"
FIX = {"js": f"ciTimeout(ms) from {WEB}ci-timeouts.mjs",
       "py": f"ci_timeout(seconds) from {WEB}ci_timeouts.py",
       "sh": f"ci_limit / wait_until from {WEB}ci_timeouts.sh",
       "swift": "a ciTimeout(seconds) helper that keeps 30 s or more in CI",
       "ini": f"ci_timeouts_hook from {WEB}ci_timeouts.py"}


def files(root):
    # A tree may carry a bare repository (HEAD, config, objects/, refs/) whose
    # config names a command for git to run: never accept one found like that.
    env = dict(os.environ, GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="safe.bareRepository",
               GIT_CONFIG_VALUE_0="explicit")
    try:
        out = subprocess.run(["git", "-c", "core.fsmonitor=false", "-C", root, "ls-files", "-z", "--cached",
                              "--others", "--exclude-standard"],
                             capture_output=True, check=True, env=env).stdout.decode(errors="replace")
        found = [f for f in out.split("\0") if f]
    except (OSError, subprocess.CalledProcessError):
        found = [os.path.relpath(os.path.join(d, n), root)
                 for d, dirs, names in os.walk(root)
                 if not dirs.__setitem__(slice(None), [x for x in dirs if x not in SKIP_DIRS])
                 for n in names]
    return sorted(f for f in found if not SKIP_DIRS & set(f.split("/")[:-1]))


def kind_of(path):
    """The rule set of a file, or None when it is not a test or test config."""
    name = os.path.basename(path)
    is_test = bool(TEST_DIR.search(path) or TEST_NAME.search(name))
    if name in PY_CONFIG:
        return "ini"
    if name.endswith(JS) and (is_test or JS_CONFIG.match(name)):
        return "js"
    if name.endswith(".py") and is_test:
        return "py"
    if name.endswith(SH) and is_test:
        return "sh"
    if name.endswith(".swift") and is_test:
        return "swift"
    if name == "package.json" or re.search(r"(^|/)\.github/workflows/[^/]+\.ya?ml$", path):
        return "flags"
    return None


def read(root, path):
    full = os.path.join(root, path)
    try:
        if os.path.islink(full) or os.path.getsize(full) > MAX_BYTES:
            return None
        with open(full, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return None


def value(text):
    return float(text.replace("_", ""))


def case(path, number, line, rule, detail):
    return {"file": path, "line": number, "rule": rule, "text": line.strip()[:160], "detail": detail}


def pytest_section(lines):
    """Line numbers that belong to a pytest section of an ini or toml file."""
    inside, out = False, set()
    for i, line in enumerate(lines):
        head = line.strip()
        if head.startswith("["):
            inside = head.strip("[]") in ("pytest", "tool:pytest", "tool.pytest.ini_options")
        elif inside:
            out.add(i)
    return out


def trailing_timeout(lines, i):
    m = TRAILING.match(lines[i])
    if not m:
        return None
    indent = m.group(1)
    for j in range(i - 1, -1, -1):
        line = lines[j]
        if line.strip() and len(line) - len(line.lstrip()) <= len(indent):
            return m.group(2) if CASE_OPEN.match(line) else None
    return None


def scan(path, kind, text):
    lines = text.split("\n")
    section = pytest_section(lines) if kind == "ini" else None
    found = []
    for i, line in enumerate(lines):
        if len(line) > MAX_LINE:
            continue
        stripped = line.lstrip()
        if stripped.startswith(COMMENT.get(kind, ("#",))):
            continue
        if CI_AWARE.search(line) or ALLOW.search(line) or (i and ALLOW.search(lines[i - 1])):
            continue
        rules = [] if section is not None and i not in section else list(RULES.get(kind, []))
        rules += FLAGS + [PW_FLAG, PYTEST_FLAG]
        values = [value(m.group(1)) * unit for rx, unit in rules for m in re.finditer(rx, line)]
        if kind == "sh":
            values += [int(m.group(1)) * float(m.group(3)) for m in LOOP.finditer(line)]
        if kind == "js":
            last = trailing_timeout(lines, i)
            if last:
                values.append(value(last) * 0.001)
        short = [v for v in values if 0 < v < FLOOR_S]
        if short:
            fix = FIX.get(kind, "a limit of 30 s or more")
            found.append(case(path, i + 1, line, "short-test-timeout",
                              f"{min(short):g} s in CI; floor {FLOOR_S} s: use {fix}"))
    return found


def safe(expression):
    """A limit of 30 s or more, none (0), or one this check cannot compute (a
    name or a call); a literal such as 5_000 or 5 * 1000 is computed."""
    parts = [p.strip() for p in expression.split("*")]
    if not all(re.fullmatch(NUM, p) for p in parts):
        return True
    total = 1.0
    for p in parts:
        total *= value(p)
    return total == 0 or total >= FLOOR_S * 1000


def without_blocks(text):
    """The text without its /* */ comments, in one pass."""
    kept, at = [], 0
    for m in re.finditer(r"(?<!\S)/\*", text):
        if m.start() < at:
            continue
        end = text.find("*/", m.end())
        if end < 0:
            break
        kept.append(text[at:m.start()] + " ")
        at = end + 2
    return "".join(kept) + text[at:]


def raised(text):
    """True when a vitest or Playwright config already sets a CI-safe limit."""
    # Comments do not count. A "/*" inside a glob such as '**/*.ts' follows a
    # quote or a star, never a space, so it is left alone.
    text = "\n".join(x for x in text[:100_000].split("\n") if len(x) <= MAX_LINE)
    code = without_blocks(text)
    code = re.sub(r"(?<!\S)//[^\n]*", " ", code)
    helper = re.search(r"\b(vitestTimeouts|playwrightTimeouts|ciTimeout|ciTimeouts)\b|"
                       r"\bdefineConfig\b[^}\n]{0,200}\}\s{0,20}from\s{0,20}['\"]web-quality-kit-interaction", code)
    conditional = re.search(r"(testTimeout|timeout)\s*:[^,\n}]*\b(CI|isCI|inCI)\b", code)
    limits = [m.group(2).strip() for m in
              re.finditer(r"\b(testTimeout|expect\s*:\s*\{\s*timeout)\s*:\s*([^,}\n]+)", code)]
    return bool(helper or conditional or any(safe(v) for v in limits))


def defaults(root, listed):
    """vitest and Playwright projects that still run with the 5 s defaults."""
    found = []
    dirs = {}
    for path in listed:
        dirs.setdefault(os.path.dirname(path), []).append(os.path.basename(path))
    for folder, names in sorted(dirs.items()):
        join = (lambda n, f=folder: f"{f}/{n}" if f else n)
        for name in names:
            if re.match(r"^playwright([.-]\w+)*\.config\.[cm]?[jt]s$", name):
                text = read(root, join(name))
                if text is not None and not raised(text) and not ALLOW.search(text):
                    found.append(case(join(name), 0, "", "playwright-default-timeout",
                                      "expect waits only 5 s in CI: add ...playwrightTimeouts() (web-quality-kit templates/ci-timeouts.mjs)"))
        if "package.json" not in names:
            continue
        try:
            pkg = json.loads(read(root, join("package.json")) or "{}")
            deps = {**(pkg.get("dependencies") or {}), **(pkg.get("devDependencies") or {})}
            scripts = " ".join(str(v) for v in (pkg.get("scripts") or {}).values())
        except (ValueError, AttributeError, TypeError):
            continue
        if "vitest" not in deps or re.search(r"--testTimeout[= ]\s*([3-9]\d{4}|\d{6,})\b", scripts):
            continue
        configs = [n for n in names if re.match(r"^(vitest|vite)([.-]\w+)*\.config\.[cm]?[jt]s$", n)]
        if any(n.startswith("vitest") for n in configs):
            configs = [n for n in configs if n.startswith("vitest")]
        texts = [(n, read(root, join(n)) or "") for n in configs]
        if not texts:
            found.append(case(join("package.json"), 0, "", "vitest-default-timeout",
                              "vitest without a config runs with 5 s in CI: add test: { ...vitestTimeouts() }"))
        for name, text in texts:
            if not raised(text) and not ALLOW.search(text):
                found.append(case(join(name), 0, "", "vitest-default-timeout",
                                  "tests run with 5 s in CI: add test: { ...vitestTimeouts() } (web-quality-kit templates/ci-timeouts.mjs)"))
    return found


def check(root):
    listed = files(root)
    found = []
    for path in listed:
        kind = kind_of(path)
        # The kit's own helpers hold the short local values on purpose.
        if kind is None or re.search(r"(^|/)(ci-timeouts\.(mjs|d\.mts)|ci_timeouts\.(py|sh)|CITimeouts\.swift)$", path):
            continue
        text = read(root, path)
        if text is not None:
            found += scan(path, kind, text)
    return found + defaults(root, listed)


def markdown(found):
    if not found:
        return "## Test time limits\n\n🟢 No short fixed test time limit (CI floor 30 s).\n"
    out = ["## Test time limits (warning only)", "",
           f"🟡 {len(found)} short limit(s): in CI a slow machine could turn these tests red (floor {FLOOR_S} s).", "",
           f"<details><summary>Cases ({len(found)})</summary>", "", "| Where | Rule | Detail |", "|---|---|---|"]
    for c in found[:60]:
        where = f"{c['file']}:{c['line']}" if c["line"] else c["file"]
        out.append(f"| `{where.replace('|', '')}` | {c['rule']} | {c['detail'].replace('|', '/')} |")
    if len(found) > 60:
        out.append(f"| … | | {len(found) - 60} more |")
    return "\n".join(out + ["", "</details>", ""])


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("root", nargs="?", default=".")
    p.add_argument("--format", choices=["json", "md"], default="json")
    a = p.parse_args(argv)
    if not os.path.isdir(a.root):
        print(f"::error::not a folder: {a.root}", file=sys.stderr)
        return 2
    found = check(a.root)
    print(json.dumps(found, ensure_ascii=False) if a.format == "json" else markdown(found))
    return 0


if __name__ == "__main__":
    sys.exit(main())
