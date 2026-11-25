# web-quality-kit

Shared quality checks for web projects (static sites, React/Vite apps, Cloudflare Workers), kept in one place so every repository gets the same tools, the same health panel and the same "do not get worse" rule.

## What runs

`actions/web-ci` runs inside the repository's own CI job. Every check is optional: leave its input empty and it is off.

| Check | Tool | Input |
|---|---|---|
| Lint, size and complexity | ESLint (`tools/eslint.config.mjs`: recommended + typescript-eslint + React hooks; complexity 15, function 120 lines, depth 4, 5 params, file 800 lines) | `lint-paths` |
| Types | `tsc --noEmit` with the project's own tsconfig (strict) | `tsc-projects` |
| Valid HTML | html-validate (`tools/htmlvalidate.json`) | `html-globs` |
| Broken links | linkinator on the built site (only the site's own links) | `site-dir` (`links-skip-paths` for files that live outside it on the real site) |
| Lighthouse: performance, accessibility, best practices, SEO | Lighthouse CI, median of 3 runs per page | `lighthouse-paths` |
| Accessibility (axe) | the axe-core rules inside Lighthouse's accessibility audit | `lighthouse-paths` |
| Performance budget (phone, slow network) | `scripts/budget.py` on the Lighthouse reports: lab LCP, CLS, TBT (stands in for INP), page weight and JS weight per page; limits in `web-budget.json` | `budget-mode`, `budget-file` |
| Site rules: cache, fonts, images | `scripts/site_rules.py` on the built site (no browser): Cloudflare `_headers` (hashed files `max-age=31536000, immutable`, HTML short); fonts self-hosted, WOFF2, Latin subset (read from the font's cmap), at most 2 weights per family, no font CDN; every `<img>` with `alt` and `width`/`height` (or an inline `aspect-ratio`), WebP/AVIF (SVG fine), `loading="lazy"` after the first 2, files up to 1600 px and 300 KB | `site-rules`, `headers-file` |
| Bundle size | sum of `.js` and `.css`, gzip, with a limit | `bundle-dir`, `bundle-max-kb` |
| Vulnerable dependencies | `npm audit` on each lockfile | `audit-dirs` |
| Tests | the repository's own command; lines starting with `ok` / `✓` / `PASS` count as passed and `FALHOU` / `not ok` / `✗` / `FAIL` as failed | `test-command` |
| Personal data and secrets | `scripts/data_guard.py`: CPF/CNPJ with valid check digits, mobile numbers, e-mails, data files, tokens. Values are never printed, only masked and hashed | `data-guard: "true"` (public repositories) |
| Database of a server (N+1, routes, indexes) | `scripts/server_queries.py` against the local server, with the kit's query counter in the app; query-in-a-loop lint (`tools/query_in_loop.mjs`, warning only) | `server-queries` |
| Modern web (warning only) | Baseline CSS (`@eslint/css` `use-baseline`) and Modern Web Guidance `legacy-scan` (old code with a native browser replacement; telemetry off) | `modern` |

Tools are pinned in `tools/package-lock.json` and installed with `npm ci --ignore-scripts` (no install script of any dependency runs); a new tool version is a pull request here.

## Health against main ("do not get worse")

The numbers of the last green run on the default branch are the reference (artifact `web-health`). A pull request fails when:

- a lint, type, HTML or axe case is **new** (a case is file + rule + the text of its line, so cases that already existed only warn, even when their line number moves);
- a broken link, a personal-data finding or a high/critical vulnerability is new;
- the bundle grew more than `bundle-growth-pct` (default 10%);
- a test failed, or fewer tests pass.

Lighthouse scores move a few points between runs of the same code, so they only warn (🟡 below 90 or more than 5 points below main). The modern-web check only warns. The first run, and any check main did not have yet, is a baseline: it shows what is bad and never fails. `fail-on-worse: "false"` turns the whole step into a report.

## Performance budget (`budget-mode`)

Lighthouse runs as a mid phone on a slow network (`formFactor: mobile`, simulated "Slow 4G", 4x CPU); a report taken any other way is an error, since desktop numbers say nothing about real visitors. These settings are passed on every Lighthouse run; they were already Lighthouse's defaults, so existing scores stay the same. Per page, median of the runs:

| Limit | Kit default | Key |
|---|---|---|
| Largest Contentful Paint | 2.5 s | `lcp_ms` |
| Cumulative Layout Shift | 0.1 | `cls` |
| Total Blocking Time (lab stand-in for INP) | 200 ms | `tbt_ms` |
| Page weight, transferred | 1024 kB | `page_kb` |
| JavaScript, transferred | 150 kB | `js_kb` |

- `budget-mode: absolute` (new sites): any page over a limit fails, on every run, even the first and even with `fail-on-worse: "false"`.
- `budget-mode: baseline` (a site migrating to the budget): a page over a limit fails only when it is new or worse than on main beyond the lab noise; the panel shows how far each page is from the goal and says when to switch to `absolute`.

`web-budget.json` at the repository root (`templates/web-budget.json`) may only make limits **stricter**. A looser limit is an `exceptions` entry with `limit`, `value`, `reason`, `approved_by`, `date` and, ideally, `expires` and `pages`; without one, or once it expires, the configuration error fails the job in any mode.

`baseline` also needs a written plan in the same file, `"migration": {"reason": "...", "until": "YYYY-MM-DD"}`; without it, or once the date has passed, it is a configuration error.

## Site rules (`site-rules`)

Same two modes: `absolute` fails on any case; `baseline` fails on a new case (one main did not have). The limits `img_px` (1600), `img_kb` (300), `font_weights` (2) and `img_eager` (2 images at the top of a page may load eagerly) live in `web-budget.json` `limits`, stricter only. A case that must stay is an exception with `rule` (e.g. `img-format`) and the `files` it covers (globs), plus `reason`, `approved_by`, `date` and ideally `expires`; covered cases are counted apart in the panel. A build tool's hashed file is recognised by its name (`name.Ab12Cd34.js`, `name-Ab12Cd34.css`); other long-lived paths go in `"cache": {"hashed_extra": ["regex"]}`, which can only add files to the check.

## Database of a server (`server-queries`)

For a Cloudflare Worker with D1 (or any server that can wrap its database the same way). The app copies `templates/server/d1_queries.ts` unchanged and wraps its fetch handler:

```ts
import { withQueryCount } from './d1_queries'
export default { fetch: (req, env, ctx) => withQueryCount(req, env, (env) => handle(req, env, ctx)) }
```

- **Production:** one JSON log line per request that touched the database, `{"event":"db_queries","method","route","status","queries","ms"}` (ids in the path become `:id`; no SQL).
- **Local tests:** with `DB_QUERY_HEADERS=1` in `.dev.vars` and a request to localhost, the response carries `x-db-queries` (count) and `x-db-sql` (the distinct SQL, base64 JSON). Any other host never gets them. The variable in a `wrangler.*` config is an error.

`server-queries.json` at the repository root (`templates/server-queries.json`) says how to start the local server, how to seed exactly `{n}` rows, and which routes are lists. The check:

1. **N+1:** each list route is called with 3 and with 30 rows; the number of queries must be the same. Growing with the rows fails.
2. **Coverage:** every GET route has that test. The routes come from `routes_command` (prints `METHOD /path` lines) or, when the kit has it, from the interaction check's route scanner (`scripts/interaction_gate.py`); with neither, the check fails with "no route scanner". A GET route that lists nothing goes in `exempt` with a reason. `mode: absolute` (new repository): a route without the test fails. `mode: baseline` (old repository): the routes in `baseline.routes` are tolerated, and that list may not grow against main.
3. **Indexes:** the SQL of the tested requests goes through `EXPLAIN QUERY PLAN` on an in-memory SQLite with the repository's `migrations`. A full table scan where the query filters (`WHERE`) or joins, or an automatic index, fails, except for tables in `exempt_tables` (small by nature, with a reason) and, in baseline mode, the queries in `baseline.scans`.
4. A database call inside `for`/`map`/`forEach` only warns (the 3-vs-30 test decides).

These are rules, not comparisons: they fail on the first run and with `fail-on-worse: "false"`.

The panel goes to the run summary: 🟢🟡🔴, bars, arrows against main (↑ better, ↓ worse), and the cases in collapsed sections. It is plain text and emoji so it reads well on a phone. `lang: pt` writes it in Portuguese.

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
      - uses: actions/checkout@v7
      - uses: actions/setup-node@v7
        with:
          node-version: 22
      - run: npm ci --ignore-scripts && npm run build
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
          budget-mode: absolute   # new site; "baseline" while migrating an old one
          site-rules: absolute
          server-queries: server-queries.json   # a server with a database
```

The workflow must also run on `push` to the default branch, so main's numbers are saved. Dependabot: copy `templates/dependabot.yml`.

**A required check never has a paths filter in its trigger.** If the ruleset requires a check and its workflow has `paths:` under `pull_request`, a pull request that touches none of those paths never gets the check and stays blocked forever. Run the job on every pull request and skip the work inside it (compare with the base branch, e.g. `git diff --name-only origin/$BASE...HEAD`).

A public value the personal-data check should accept (the business phone on the contact page) goes into `.data-guard-allow` at the repository root: one hash per line (the hash is in the panel), with a comment saying what it is.

## After the deploy: is it live? (`actions/web-live`)

A job after the deploy checks that each page answers 200 and is byte for byte the file this run published. That catches a deploy that did not happen, or a part still at its old version. A CDN can keep the old page for a few minutes, so it asks again with a cache-skipping query string (10 rounds, 30 s apart by default) and fails the job if a page still differs.

```yaml
  live:
    needs: deploy
    runs-on: ubuntu-latest
    timeout-minutes: 10
    steps:
      - uses: luanharaujo/web-quality-kit/actions/web-live@v1.1.0
        with:
          base-url: https://example.com
          pages-artifact: "true"   # the files actions/deploy-pages published in this run
          paths: / /adm/versao.json /treinos/
```

## Security (`actions/security`)

Secrets with [gitleaks](https://github.com/gitleaks/gitleaks) and code patterns with [Semgrep CE](https://semgrep.dev), for any repository (web, Swift, anything in git), "never worse":

- **Pull request:** gitleaks reads only the new commits; Semgrep reports only what main does not have (`--baseline-commit`). Anything found fails the job.
- **Push:** gitleaks reads the pushed commits (fails on a find); Semgrep reports.
- **Schedule / by hand:** both scan everything and only report.

Both tools are pinned and SHA-256 checked: the gitleaks release, and Semgrep with every dependency (`tools/semgrep-requirements.txt`, `pip --require-hashes`, wheels only). In a pull request, the gitleaks config and `.gitleaksignore` come from the base branch, `gitleaks:allow` and `# nosemgrep` comments are ignored, and a change to `.semgrepignore` fails the job for a person to review, so a pull request cannot allowlist its own finding. Semgrep runs without a token: rules come from its public registry (`p/ci` by default), no code is sent and metrics are off. A `.gitleaks.toml` at the repository root extends gitleaks' rules (or pass `gitleaks-config`). gitleaks redacts what it finds, so the run log never shows a secret. A public repository on a self-hosted runner is refused. Semgrep needs Python 3.10 to 3.14: the action looks on PATH, then in `/opt/homebrew/bin` and `/usr/local/bin` (a self-hosted runner started by launchd often has only `/usr/bin` on PATH, where macOS's Python is 3.9).

```yaml
  security:   # gitleaks + semgrep
    runs-on: ${{ fromJSON(vars.CI_RUNNER || '"ubuntu-latest"') }}
    timeout-minutes: 15
    steps:
      - uses: actions/checkout@v7
        with:
          fetch-depth: 0
      - uses: luanharaujo/web-quality-kit/actions/security@v1.2.1
```

Proof: `tests/test_security.sh` builds a bait repository and checks that a planted token and an `eval()` fail a pull request, that the same findings already on main do not block a clean one, and that the token never appears in the output.

## Pull request description (`actions/pr-description`)

Keeps the description of every pull request in the standard, without depending on anyone's memory:

- **Body lint** (`scripts/pr_lint.py`, a copy of the central source, pinned by hash in the test): the three sections, a `<details>` technical block, and the language of the repository's visibility (Portuguese for a private repository, English for a public one). It reads only the event; nothing from the body is executed. Dependabot and the canary App are exempt.
- **Sticky comment** `<!-- PR-VERIFICACOES -->`, one per pull request, created or edited on every push: *Checks* (what each check means, how strict it is, the real result), *Health* (the panel, with dots, bars and arrows against main) and *What each metric means* (a glossary, so a reader who did not design the system can trust the numbers). Texts are in `scripts/pr_texts.json` (pt and en); only text and emoji, for GitHub Mobile.

Copy `templates/pr-description.yml`: event `pull_request` (never `pull_request_target`), `contents: read` + `pull-requests: write`, job `pr-descricao`. A fork pull request, or Dependabot (read-only token), skips the comment with a warning instead of failing. For the panel, make the job `needs:` the CI job: `web-ci` saves it as the `pr-panel` artifact (`panel-artifact` input; empty = off).

## Run it on a laptop

```sh
(cd tools && npm ci --ignore-scripts)
cd ../my-site
LINT_PATHS="src" SITE_DIR=dist LH_PATHS="/" LH_RUNS=1 OUT=/tmp/health bash ../web-quality-kit/scripts/web_ci.sh
python3 ../web-quality-kit/scripts/web_health.py compare - /tmp/health/health.json
```

## Kit's own tests

`tests/test_site_rules.py` (a good built site has no case; a bad one has each cache, font and image problem caught by its own rule; exceptions hide only their files; absolute fails on any case, baseline on new ones), `tests/test_budget.py` (stricter limits apply, a looser one without a registered exception and an expired exception are errors, a desktop or fast-network report is an error; absolute fails over the limit, baseline only when worse or new), `tests/test_web_health.py` (each gate fails when worse, old cases only warn, the baseline never fails), `tests/test_data_guard.py` (bad case caught, good case passes, no value in the output) and `tests/test_server_queries.py` (an example server with an N+1 in a loop, a batch built per row, a list route without the test and a missing index is caught; the good one with JOIN/IN and an index passes; the query headers never leave localhost and carry no SQL in production logs; the baseline only shrinks) and `tests/test_pr_description.py` (lint copy equals the central one, the comment shows each check's real result, every panel metric is explained, create/update/skip on a fake API) and `tests/test_collect.sh` (every tool finds the problem planted in `tests/cases/site`; a heavy page fails the budget with a real Lighthouse run and the light one passes). They run in `.github/workflows/check.yml`.

The test cases in `tests/cases/` contain made-up personal data on purpose (the bad cases the guard must catch).
