# web-quality-kit

Shared quality checks for web projects (static sites, React/Vite apps, Cloudflare Workers), kept in one place so every repository gets the same tools, the same health panel and the same "do not get worse" rule.

## What runs

`actions/web-ci` runs inside the repository's own CI job. Every check is optional: leave its input empty and it is off.

| Check | Tool | Input |
|---|---|---|
| Lint, size and complexity | ESLint (`tools/eslint.config.mjs`: recommended + typescript-eslint + React hooks; complexity 15, function 120 lines, depth 4, 5 params, file 800 lines) | `lint-paths` |
| Types | `tsc --noEmit` with the project's own tsconfig (strict) | `tsc-projects` |
| Valid HTML | html-validate (`tools/htmlvalidate.json`) | `html-globs` |
| Broken links | linkinator on the built site (only the site's own links) | `site-dir` |
| Lighthouse: performance, accessibility, best practices, SEO | Lighthouse CI, median of 3 runs per page | `lighthouse-paths` |
| Accessibility (axe) | the axe-core rules inside Lighthouse's accessibility audit | `lighthouse-paths` |
| Bundle size | sum of `.js` and `.css`, gzip, with a limit | `bundle-dir`, `bundle-max-kb` |
| Vulnerable dependencies | `npm audit` on each lockfile | `audit-dirs` |
| Tests | the repository's own command; lines starting with `ok` / `FALHOU` are counted | `test-command` |
| Personal data and secrets | `scripts/data_guard.py`: CPF/CNPJ with valid check digits, mobile numbers, e-mails, data files, tokens. Values are never printed, only masked and hashed | `data-guard: "true"` (public repositories) |
| Modern web (warning only) | Baseline CSS (`@eslint/css` `use-baseline`) and Modern Web Guidance `legacy-scan` (old code with a native browser replacement; telemetry off) | `modern` |

Tools are pinned in `tools/package-lock.json` and installed with `npm ci`; a new tool version is a pull request here.

## Health against main ("do not get worse")

The numbers of the last green run on the default branch are the reference (artifact `web-health`). A pull request fails when:

- a lint, type, HTML or axe case is **new** (a case is file + rule + the text of its line, so cases that already existed only warn, even when their line number moves);
- a broken link, a personal-data finding or a high/critical vulnerability is new;
- the bundle grew more than `bundle-growth-pct` (default 10%);
- a test failed, or fewer tests pass.

Lighthouse scores move a few points between runs of the same code, so they only warn (🟡 below 90 or more than 5 points below main). The modern-web check only warns. The first run, and any check main did not have yet, is a baseline: it shows what is bad and never fails. `fail-on-worse: "false"` turns the whole step into a report.

The panel goes to the run summary: 🟢🟡🔴, bars, arrows against main (↑ better, ↓ worse), and the cases in collapsed sections.

## Use

```yaml
jobs:
  web-health:
    # Private repositories: CI_RUNNER picks the machine; public: GitHub's.
    runs-on: ${{ fromJSON(vars.CI_RUNNER || '"ubuntu-latest"') }}
    timeout-minutes: 30
    permissions:
      contents: read
      actions: read   # main's health numbers
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with:
          node-version: 22
      - run: npm ci && npm run build
        working-directory: app
      - uses: luanharaujo/web-quality-kit/actions/web-ci@v1
        with:
          lint-paths: app/src api/src
          tsc-projects: |
            app
            api
          html-globs: app/index.html
          site-dir: app/dist
          site-mount: /treinos
          lighthouse-paths: /treinos/
          lighthouse-categories: performance accessibility best-practices
          bundle-dir: app/dist
          bundle-max-kb: "300"
          audit-dirs: app api
```

The workflow must also run on `push` to the default branch, so main's numbers are saved. Dependabot: copy `templates/dependabot.yml`.

A public value the personal-data check should accept (the business phone on the contact page) goes into `.data-guard-allow` at the repository root: one hash per line (the hash is in the panel), with a comment saying what it is.

## Run it on a laptop

```sh
(cd tools && npm ci)
cd ../my-site
LINT_PATHS="src" SITE_DIR=dist LH_PATHS="/" LH_RUNS=1 OUT=/tmp/health bash ../web-quality-kit/scripts/web_ci.sh
python3 ../web-quality-kit/scripts/web_health.py compare - /tmp/health/health.json
```

## Kit's own tests

`tests/test_web_health.py` (each gate fails when worse, old cases only warn, the baseline never fails), `tests/test_data_guard.py` (bad case caught, good case passes, no value in the output) and `tests/test_collect.sh` (every tool finds the problem planted in `tests/casos/site`). They run in `.github/workflows/check.yml`.

The test cases in `tests/casos/` contain made-up personal data on purpose (the bad cases the guard must catch).
