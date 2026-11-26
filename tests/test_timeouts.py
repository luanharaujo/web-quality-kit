#!/usr/bin/env python3
"""Proof of the CI time limits: every short fixed test limit is caught by the
check, limits written with the kit's helpers pass, each helper keeps 30 s or
more in CI (and the local value on a laptop), and the panel only warns."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import types

HERE = os.path.dirname(os.path.abspath(__file__))
KIT = os.path.join(HERE, "..")
sys.path[:0] = [os.path.join(KIT, "scripts"), os.path.join(KIT, "templates")]
import ci_timeouts  # noqa: E402
import server_run  # noqa: E402
import timeout_check  # noqa: E402

FAILED = []


def check(name, ok, detail=""):
    print(("  ok     " if ok else "  FAILED ") + name + ("" if ok else f": {detail}"))
    if not ok:
        FAILED.append(name)


def repo(files):
    d = tempfile.mkdtemp()
    for path, text in files.items():
        full = os.path.join(d, path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w") as f:
            f.write(text)
    return d


def lines(found, path):
    return sorted(c["line"] for c in found if c["file"] == path)


# --- the check: bad cases ----------------------------------------------------
BAD = {
    # kit:short-timeout a bad case for the check to find
    "package.json": '{"devDependencies": {"vitest": "3.0.0"}, "scripts": {"test": "vitest run", "t": "node --test --test-timeout=5000"}}',
    "vitest.config.ts": "export default { test: { testTimeout: 5_000 } }\n",
    "test/a.test.ts": ("it('x', async () => {\n"
                       "  await vi.waitFor(check, { timeout: 2000 })\n"
                       "}, 5000)\n"
                       "const s = AbortSignal.timeout(5_000)\n"
                       "test.setTimeout(10000)\n"
                       "// timeout: 1\n"),
    "web/playwright.config.mjs": "export default defineConfig({ testDir: '.' })\n",
    # kit:short-timeout a bad case for the check to find
    "tests/test_api.py": ("@pytest.mark.timeout(5)\n"
                          "def test_x():\n"
                          # kit:short-timeout a bad case for the check to find
                          "    subprocess.run(['x'], timeout=10)\n"
                          # kit:short-timeout a bad case for the check to find
                          "    urlopen(u, timeout=2.5)\n"),
    "pytest.ini": "[pytest]\ntimeout = 5\n",
    "scripts/test_local.sh": ("curl -s --max-time 5 $API/health\n"
                              "timeout 10 ./run.sh\n"
                              "for _ in $(seq 1 20); do curl -s $API && break; sleep 0.5; done\n"
                              "curl -m 3 $API\n"),
    "App/Tests/LoginTests.swift": ("XCTAssertTrue(button.waitForExistence(timeout: 5))\n"
                                   "wait(for: [done], timeout: 2.0)\n"
                                   "await fulfillment(of: [done], timeout: 1)\n"
                                   "executionTimeAllowance = 10\n"),
    # kit:short-timeout a bad case for the check to find
    ".github/workflows/ci.yml": "      - run: npx playwright test --timeout=5000\n      - run: pytest -q --timeout 10\n",
}
bad = timeout_check.check(repo(BAD))
check("vitest/Jest: short literals, waitFor, per-test limit, AbortSignal, setTimeout; comments ignored",
      lines(bad, "test/a.test.ts") == [2, 3, 4, 5], lines(bad, "test/a.test.ts"))
check("a vitest config with a short limit is caught, and so is the 5 s default",
      lines(bad, "vitest.config.ts") == [0, 1], lines(bad, "vitest.config.ts"))
check("a Playwright config left with the 5 s expect default is caught",
      [c["rule"] for c in bad if c["file"] == "web/playwright.config.mjs"] == ["playwright-default-timeout"], bad)
check("--test-timeout in package.json is caught", lines(bad, "package.json") == [1], lines(bad, "package.json"))
check("pytest: mark, subprocess and urlopen limits", lines(bad, "tests/test_api.py") == [1, 3, 4], lines(bad, "tests/test_api.py"))
check("pytest.ini global timeout", lines(bad, "pytest.ini") == [2], lines(bad, "pytest.ini"))
check("shell API tests: curl, timeout and a short wait loop",
      lines(bad, "scripts/test_local.sh") == [1, 2, 3, 4], lines(bad, "scripts/test_local.sh"))
check("XCTest: waitForExistence, wait, fulfillment, executionTimeAllowance",
      lines(bad, "App/Tests/LoginTests.swift") == [1, 2, 3, 4], lines(bad, "App/Tests/LoginTests.swift"))
check("runner flags in a workflow", lines(bad, ".github/workflows/ci.yml") == [1, 2], lines(bad, ".github/workflows/ci.yml"))

quiet = timeout_check.check(repo({
    "App/Tests/A.swift": ('wait(for: [x], timeout: 5) // CI\n'
                          'XCTAssertTrue(b.waitForExistence(timeout: 5), "button on CI screen")\n'),
    "test/a.test.js": "const s = { timeout: 5000 } // slow in CI\n"}))
check("the word CI in a comment or a text does not hide a short limit", len(quiet) == 3, quiet)

# --- the check: good cases ---------------------------------------------------
GOOD = {
    "package.json": '{"devDependencies": {"vitest": "3.0.0"}}',
    "vitest.config.ts": ("import { vitestTimeouts } from './ci-timeouts.mjs'\n"
                         "export default { test: { ...vitestTimeouts() } }\n"),
    "api/package.json": '{"devDependencies": {"vitest": "3.0.0"}}',
    "api/vitest.config.ts": "export default { test: { testTimeout: process.env.CI ? 30000 : 5000 } }\n",
    "web/playwright.config.mjs": "import { defineConfig } from 'web-quality-kit-interaction'\n",
    "e2e/playwright.config.ts": "export default { expect: { timeout: 30_000 } }\n",
    "ci-timeouts.mjs": "export const vitestTimeouts = () => ({ testTimeout: ciTimeout(5_000) })\n",
    "test/a.test.ts": ("await vi.waitFor(check, { timeout: ciTimeout(2000) })\n"
                       "const slow = { timeout: 60_000 }\n"
                       "const unlimited = { timeout: 0 }\n"
                       "const expr = { timeout: 60 * 1000 }\n"
                       "// kit:short-timeout proves the time limit itself\n"
                       "const s = AbortSignal.timeout(10)\n"
                       "setTimeout(() => {\n"
                       "}, 100)\n"),
    # kit:short-timeout a bad case for the check to find
    "tests/test_api.py": ("subprocess.run(c, timeout=ci_timeout(5))\n"
                          "subprocess.run(c, timeout=120)\n"
                          "hang(timeout=1)  # kit:short-timeout the limit under test\n"),
    "scripts/test_local.sh": ('curl --max-time "$(ci_limit 5)" $API\n'
                              "curl -m 60 $API\n"
                              "for _ in $(seq 1 120); do curl -s $API && break; sleep 0.5; done\n"
                              "for _ in $(seq 1 20); do kill -0 $P || break; sleep 0.5; done\n"
                              "sleep 2\n"),
    "App/Tests/LoginTests.swift": "XCTAssertTrue(button.waitForExistence(timeout: ciTimeout(5)))\nwait(for: [d], timeout: 60)\n",
    "src/client.ts": "fetch(u, { timeout: 5000 })\n",
    "node_modules/x/test/a.test.js": "const s = { timeout: 1 }\n",
}
good = timeout_check.check(repo(GOOD))
check("limits written with the helpers, long, unlimited, justified or outside tests pass", good == [], good)
md = timeout_check.markdown(bad)
check("the markdown report lists the cases", "warning only" in md and "test/a.test.ts:2" in md, md[:200])

for config, ok in (("testTimeout: 60 * 1000", True), ("expect: { timeout: 60 * 1000 }", True),
                   ("testTimeout: SLOW_MS", True), ("testTimeout: 5 * 1000", False),
                   ("/* testTimeout: 60000 */", False), ("retries: 1 // testTimeout: 60000", False),
                   ("include: ['**/*.ts'], testTimeout: 60000, exclude: ['x/*/y']", True)):
    got = timeout_check.raised("export default { test: { " + config + " } }\n")
    check(f"config limit read as {'safe' if ok else 'short'}: {config}", got is ok, got)

# --- file names are matched in linear time ------------------------------------
start = time.time()
long_names = repo({"vite-" + "--" * 60 + "x": "", "playwright-" + "--" * 60 + "x": "",
                   "a/package.json": '{"devDependencies": {"vitest": "3.0.0"}}', "a/vite-" + "--" * 60 + "x": "",
                   "b/vitest.e2e-slow.config.ts": "export default { test: { testTimeout: 5000 } }\n"})
seen = timeout_check.check(long_names)
check("a long crafted file name is matched at once, and hyphenated config names still count",
      time.time() - start < 20 and "b/vitest.e2e-slow.config.ts" in [c["file"] for c in seen], seen)

# --- a giant line (generated code) never stalls the check ----------------------
start = time.time()
huge = repo({"scripts/test_big.sh": "curl " * 40_000 + "\n" + "playwright test " * 16_000 + "\ncurl -m 3 x\n",
             "test/big.test.js": " /*" * 100_000 + "\n",
             "App/Tests/BigTests.swift": "wait(" * 80_000 + "\n",
             "package.json": '{"devDependencies": {"vitest": "3.0.0"}}', "vitest.config.ts": " /* x\n" * 60_000})
seen = timeout_check.check(huge)
check("giant lines are skipped at once, the rest is still read",
      time.time() - start < 30 and ("scripts/test_big.sh", 3) in [(c["file"], c["line"]) for c in seen],
      [time.time() - start, seen[:3]])
check("only the kit's defineConfig counts as raised",
      timeout_check.raised("import { defineConfig, devices } from 'web-quality-kit-interaction'\n")
      and not timeout_check.raised("import { devices } from 'web-quality-kit-interaction'\n"))

# --- a tree that carries a bare repository never runs its command --------------
marker = os.path.join(tempfile.mkdtemp(), "ran")
trap = repo({"HEAD": "ref: refs/heads/main\n",
             "config": ('[core]\n\trepositoryformatversion = 0\n\tbare = false\n\tworktree = .\n'
                        f'\tfsmonitor = "touch {marker}; false"\n'),
             "objects/keep": "", "refs/keep": "",
             # kit:short-timeout a bad case for the check to find
             "test/a.test.js": "const s = { timeout: 5000 }\n"})
seen = timeout_check.check(trap)
check("a bare repository inside the tree runs nothing, and its tests are still read",
      not os.path.exists(marker) and [c["file"] for c in seen] == ["test/a.test.js"], [os.path.exists(marker), seen])

# --- the helpers keep the floor in CI only -----------------------------------
os.environ["CI"] = "true"
check("python: floor in CI, long values and no limit untouched",
      [ci_timeouts.ci_timeout(v) for v in (5, 90, 0, None)] == [30, 90, 0, None])
# kit:short-timeout a bad case for the check to find
cfg = types.SimpleNamespace(option=types.SimpleNamespace(timeout=5), getini=lambda n: "")
ci_timeouts.ci_timeouts_hook(cfg)
check("pytest hook raises the global limit", cfg.option.timeout == 30, cfg.option.timeout)
cfg = types.SimpleNamespace(option=types.SimpleNamespace(timeout=None), getini=lambda n: "7")
os.environ.pop("PYTEST_TIMEOUT", None)
ci_timeouts.ci_timeouts_hook(cfg)
check("pytest hook raises the ini limit", cfg.option.timeout == 30, cfg.option.timeout)
check("the kit's server start and request limits have a floor in CI", server_run.ci_floor(2, 120) == 120)

SH = os.path.join(KIT, "templates", "ci_timeouts.sh")


def sh(script, ci):
    env = dict(os.environ, CI=ci)
    return subprocess.run(["bash", "-euc", f'. "{SH}"; {script}'], env=env, capture_output=True, text=True, timeout=60)


check("shell: ci_limit has the floor in CI", sh("ci_limit 5; ci_limit 90; ci_limit 0", "true").stdout.split() == ["30", "90", "0"])
check("shell: ci_limit keeps the local value on a laptop", sh("ci_limit 5", "").stdout.strip() == "5")
check("shell: wait_until returns as soon as the command succeeds", sh("wait_until 5 true", "true").returncode == 0)
t = time.time()
r = sh("wait_until 1 false", "")
check("shell: wait_until fails after the local limit on a laptop", r.returncode == 1 and time.time() - t < 10, r.returncode)
r = sh(f'( sleep 2; touch "{os.path.join(tempfile.gettempdir(), "kit-zero-limit")}.$$" ) & '
       f'wait_until 0 test -f "{os.path.join(tempfile.gettempdir(), "kit-zero-limit")}.$$"', "true")
check("shell: wait_until 0 waits without limit", r.returncode == 0, r.stderr)
marker = os.path.join(tempfile.mkdtemp(), "ready")
t = time.time()
r = sh(f'( sleep 3; touch "{marker}" ) & wait_until 1 test -f "{marker}"', "true")
check("shell: in CI a 1 s wait for a 3 s start is green", r.returncode == 0, r.stderr)

node = shutil.which("node")
if node:
    js = ("const t = await import(process.argv[1]); const c = await import(process.argv[2]);"
          "console.log(JSON.stringify([t.vitestTimeouts(), t.playwrightTimeouts(), t.ciTimeout(0) + (c.ciExtra() === (t.inCI() ? 20000 : 0) ? 0 : 1),"
          "c.ciLimits({ use: { actionTimeout: 1000 }, projects: [{ expect: { timeout: 1000 } }], webServer: { command: 'x' } })]))")

    def run_js(ci, mode=""):
        env = dict(os.environ, CI=ci, KIT_INTERACTION_MODE=mode)
        r = subprocess.run([node, "--input-type=module", "-e", js,
                            "file://" + os.path.abspath(os.path.join(KIT, "templates", "ci-timeouts.mjs")),
                            "file://" + os.path.abspath(os.path.join(KIT, "interaction", "ci.mjs"))],
                           env=env, capture_output=True, text=True, check=True)
        return json.loads(r.stdout)

    vt, pw, zero, kit = run_js("true")
    check("vitest: 30 s for tests and hooks in CI", set(vt.values()) == {30000} and zero == 0, vt)
    check("Playwright: 30 s expect and 90 s test in CI", pw == {"timeout": 90000, "expect": {"timeout": 30000}}, pw)
    check("interaction config: floors on the config, projects, actions and web server",
          kit["timeout"] == 90000 and kit["expect"]["timeout"] == 30000 and kit["use"]["actionTimeout"] == 30000
          and kit["projects"][0]["expect"]["timeout"] == 30000 and kit["webServer"]["timeout"] == 120000, kit)
    _, pw, _, kit = run_js("true", "fail")
    check("failing-network pass: longer test limit, expect and actions untouched",
          kit["timeout"] == 60000 and "expect" not in kit and kit["use"]["actionTimeout"] == 1000
          and pw == {"timeout": 60000, "expect": {"timeout": 5000}}, [kit, pw])
    vt, pw, _, kit = run_js("")
    check("on a laptop nothing changes", vt["testTimeout"] == 5000 and pw["expect"]["timeout"] == 5000
          and "timeout" not in kit, [vt, pw, kit])
else:
    check("node is installed", False)
os.environ.pop("CI")
check("python: the local value on a laptop", ci_timeouts.ci_timeout(5) == 5 and server_run.ci_floor(2, 120) == 2)

# --- the panel only warns ----------------------------------------------------
d = tempfile.mkdtemp()
health = os.path.join(KIT, "scripts", "web_health.py")
base = {"kit": 1, "metrics": {"timeouts": []}}
head = {"kit": 1, "metrics": {"timeouts": bad}}
for name, data in (("b.json", base), ("h.json", head)):
    with open(os.path.join(d, name), "w") as f:
        json.dump(data, f)
with open(os.path.join(KIT, "scripts", "panel_pt.json")) as f:
    translated = json.load(f)["Short test time limit (warning only)"]
for lang, label in (("en", "Short test time limit"), ("pt", translated)):
    r = subprocess.run([sys.executable, health, "compare", os.path.join(d, "b.json"), os.path.join(d, "h.json"),
                        "--lang", lang], capture_output=True, text=True)
    row = next((x for x in r.stdout.split("\n") if x.startswith("|") and label in x), "")
    check(f"{lang}: new cases show 🟡 and never fail", r.returncode == 0 and row.startswith("| 🟡"), r.stdout[:400] + r.stderr)

print("timeouts: ok" if not FAILED else f"timeouts: {len(FAILED)} failed")
sys.exit(1 if FAILED else 0)
