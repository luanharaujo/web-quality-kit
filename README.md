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
| Site rules: cache, fonts, images | `scripts/site_rules.py` on the built site (no browser): Cloudflare `_headers` (hashed files `max-age=31536000, immutable`, HTML short, `s-maxage` included); fonts self-hosted, WOFF2, Latin subset (read from the font's cmap), at most 2 weights per family, no font CDN; every `<img>` with `alt` and `width`/`height` (or an inline `aspect-ratio`), WebP/AVIF (SVG fine), `loading="lazy"` after the first 2, files up to 1600 px and 300 KB | `site-rules`, `headers-file` |
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

Same two modes: `absolute` fails on any case; `baseline` fails on a new case (one main did not have). The limits `img_px` (1600), `img_kb` (300), `font_weights` (2) and `img_eager` (2 images at the top of a page may load eagerly) live in `web-budget.json` `limits`, stricter only. A case that must stay is an exception with `rule` (e.g. `img-format`) and the `files` it covers (globs), plus `reason`, `approved_by`, `date` and ideally `expires`; covered cases are counted apart in the panel. Hashed files are the ones in a folder only a build tool writes (`_astro/`, `_next/static/`, `_app/immutable/`). `assets/` is never guessed, because hand-written sites use it too and a year of immutable cache on a file that keeps its name cannot be undone in a browser: declare `"cache": {"hashed_dirs": ["assets/"]}` when it is a Vite output folder, and the whole folder is checked. An undeclared `assets/` (subfolders included, 2 files or more) where most names look hashed gets a yellow note in the panel: its cache is not checked until it is declared, but its other cases (fonts, images) still count. Single paths go in `"hashed_extra": ["regex"]`; both can only add files to the check. In the kit's build folders the hash in a name is replaced by `[hash]` so a new build does not turn old cases into new ones (a whole 8-letter name made of lower-case words, like `main-app.js`, stays a name). In a declared folder, and in any `assets/` folder, only names that look like a hash are replaced (this is for identity only, it never asks for a cache header), so a name with an unusual hash (e.g. all lowercase, not hex) keeps it and can show up again as a new case after a rebuild.

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
5. **Pagination:** with more rows than a page, each list route called with `?limit=10` must return exactly 10 items and a next cursor or page (`next`, `cursor`, `next_cursor`...), and with no limit, or with a limit far above the cap, it must stop at the cap (default 50; refusing that limit with 400 or 422 also passes). The cap calls add `?_rows=<n>` so a response cache cannot answer with the old rows; an API that refuses unknown parameters sets `pagination.cache_buster` to `""`. A tested list that does not page by nature (a grouped summary) goes in `pagination.exempt` with a reason, also in absolute mode. Settings in `pagination`. Baseline: `baseline.unpaged`.
6. **Time limit on external calls:** every `fetch` to another host needs `signal: AbortSignal.timeout(ms)` up to `fetch_timeout.max_ms` (default 10 s) or an AbortController's signal. A binding (`env.API.fetch`), a relative URL and a host in `fetch_timeout.internal_hosts` are internal. `fetch(new Request(url, init))` reads like `fetch(url, init)`, and a Worker forwarding its own incoming request (`return fetch(request)` in its `fetch` handler) is not flagged. A protocol-relative URL (`//host/x`) is external, and the timeout may be a literal, a simple expression (`60 * 1000`) or a `const` in the same file; any other expression is not checked against `max_ms`. A PR that stops running a part main measured (for example by removing `source`) is red. Baseline: `baseline.fetch` (file and line text).

These are rules, not comparisons: they fail on the first run and with `fail-on-worse: "false"`.

**Upgrading the kit in a repository that already runs `server-queries`:** the check requires the app's `d1_queries.ts` to be identical to the kit's, so a new kit version that changes it (1.11.0 does: the route template in the log) is red until the file is copied again. A pull request that only bumps the kit version is not enough: it must also copy `templates/server/d1_queries.ts` and, in baseline mode, list today's cases of any new rule (`baseline.unpaged`, `baseline.fetch` in 1.11.0). Read the cases from the first red run.

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
          cache-check: "true"      # also the real Cache-Control the host answers
```

`cache-check: "true"` asks the live site for the same cache rules as `site-rules` (a year + `immutable` on hashed files, at most 600 s on pages): a host that ignores `_headers` fails here even when the file was right. Hashed files are the kit's build folders plus `cache.hashed_dirs` / `hashed_extra` from `budget-file` (check out the repo in that job), at most 30 of them per run.

## Every week: are the goals safe? (`actions/web-monitor`)

Copy `templates/web-monitor.yml`. Every Monday it runs Lighthouse on the live site (phone, slow network, 3 runs per page) and keeps each page's LCP p75 in a history artifact. Goals come from `web-budget.json`, stricter only:

```json
"slo": {"availability_pct": 99.9, "lcp_p75_ms": 2500, "window_days": 28}
```

- 🟢 the window meets the goal and so does this week: the run is green, nobody is told.
- 🟡 at risk: this week is over the goal, or half of the availability error budget is spent. The run fails, so GitHub emails the owner.
- 🔴 breached: the window's p75 is over the goal, or the whole error budget is spent.

Availability needs a prober that writes `ISO time,1|0` lines (`probes-file`); an outage itself is the prober's alarm, this job only answers "will we miss the goal this month?". Lab numbers are not real users' numbers: they are the same phone every week, which is what makes a trend.

## Node.js updates (`actions/node-update`)

Copy `templates/node-update.yml`. Once a week it reads nodejs.org's own release list and, when the LTS line the repo pins has a newer patch or minor, opens a pull request that changes only that number in `.node-version`, `.nvmrc` and `package.json` `engines.node` (exact, `^` or `~`). No Renovate, no other service.

- Never a new major: a newer LTS is only reported in the PR and in the run. Moving to it is a decision.
- A release waits 7 days, like the kit's Dependabot; a security release skips the wait only when it is a patch of the pinned minor.
- A major alone (`22`), an alias (`lts/*`) or a range (`>=20`) already floats: nothing changes.
- The PR is opened by a GitHub App made only for this: contents and pull-requests write, no ruleset bypass. An App that can bypass the main rules must never have its key in a repo secret. Its token is the `token` input and its slug the `app-slug` input; the run stops before any call when that slug is in `refused-apps` (the Apps with bypass, from a variable). Because an App opens it, the repo's CI runs on the PR, and its commits are made through the API, signed by GitHub. A PR opened with the default `GITHUB_TOKEN` would not run CI.
- One PR per version (`kit/node-X.Y.Z`), branched from the commit it read, so it never undoes newer work on main. A newer version closes the open PR of an older one. Closing a PR and keeping its branch means "no" for that version; a failed run deletes the branch it made, and a branch left without any PR (a cancelled run) is made again.
- Only pins of the line the repo runs on change (the major of an exact version in `.node-version` or `.nvmrc`; when that file floats, as `lts/*` or `22`, the `engines` floor is left alone; next to an exact version file, an `engines` `^`/`~` is a support floor and is left alone too): an `engines` pin of another major (`^18.17.0` next to `.nvmrc` 22) is left alone.
- The repo's CI tests the new version only if it reads the version file (`actions/setup-node` with `node-version-file: .nvmrc`), not a fixed `node-version`.
- Merging is the owner's call, like any PR.

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

## Interaction checks (`actions/interaction`)

Every action that saves data (a click or a submit that sends POST, PUT,
PATCH or DELETE) is checked in the repository's own Playwright tests, with no
test written for it. The tests import from the kit instead of Playwright:

```js
import { test, expect } from 'web-quality-kit-interaction'; // was '@playwright/test'
```

The checks tap twice and cut the network on purpose: run them only against a
test environment (local server and database), never production.

The action links that package, runs the suite twice and checks each write
route once per run:

| Pass | Rule | Why |
|---|---|---|
| slow server (2 s) | within 400 ms of the tap the control is `disabled`/`aria-disabled` with `aria-busy`, or a `role=status`/`aria-live` region says what is happening | past 400 ms a still screen looks frozen and people tap again (Doherty threshold) |
| slow server | a second tap right away reaches the server once, or both requests carry the same `Idempotency-Key` | a double tap must not book twice |
| slow server | no success words ("booked", "paid", "saved"...) before the server answers, unless the route is listed in `optimistic` | someone else may have taken the slot |
| failing network | an error in `role=alert` with a "try again" button; what the person typed or chose is still there; "try again" sends again | never make them start over |
| uploads | a progress bar with a value (`role=progressbar` + `aria-valuenow`, or `<progress value>`), one per file; the drop zone reacts to `dragenter`; after the answer, a thumbnail or the file type and size; one failed file does not stop the others and keeps its own retry without choosing the file again | a spinner hides the truth; a file name is not proof |

Server side, a contract test per write route:

```js
import { test, expectIdempotent } from 'web-quality-kit-interaction';
test('booking is idempotent', async ({ request }) => {
  await expectIdempotent(request, { url: '/api/bookings', data: { slot: 42 },
    count: async () => (await (await request.get('/api/test/count?table=bookings')).json()).n });
});
```

Nothing depends on remembering: the action lists the server's write routes
(`server-dirs` scan: `app.post('/x')`, `route('POST', '/x')`,
`method === 'POST' && path === '/x'`, Pages Functions `onRequestPost`; or the
repository's `routes-command`) and every route needs an end-to-end pass
through the checks and a passing contract. Write handlers the scan cannot
name are counted too, so they cannot hide. `interaction.json` (template in
`templates/`) holds the exceptions, each with a reason: `optimistic`,
`no_ui` (webhooks: contract only) and `exclude`. The success and retry
words are English by default; an app in another language sets `success` and
`retry` (regular expressions) in `interaction.json`, listing its own words and
the English ones, since each key replaces the whole default pattern.

`mode: new` (new repositories) makes any gap red. `mode: baseline`
(existing ones) only fails when something gets
worse than main. Resumable uploads (tus or multipart) are recommended for
large files such as videos, not checked.

Locally: `KIT_INTERACTION_ENFORCE=throw` (default) fails the test with the
reason; `KIT_INTERACTION_MODE=fail` runs the failing-network pass.

## Run it on a laptop

```sh
(cd tools && npm ci --ignore-scripts)
cd ../my-site
LINT_PATHS="src" SITE_DIR=dist LH_PATHS="/" LH_RUNS=1 OUT=/tmp/health bash ../web-quality-kit/scripts/web_ci.sh
python3 ../web-quality-kit/scripts/web_health.py compare - /tmp/health/health.json
```

## Kit's own tests

`tests/test_site_rules.py` (a good built site has no case; a bad one has each cache, font and image problem caught by its own rule; exceptions hide only their files; absolute fails on any case, baseline on new ones), `tests/test_budget.py` (stricter limits apply, a looser one without a registered exception and an expired exception are errors, a desktop or fast-network report is an error; absolute fails over the limit, baseline only when worse or new), `tests/test_web_health.py` (each gate fails when worse, old cases only warn, the baseline never fails), `tests/test_data_guard.py` (bad case caught, good case passes, no value in the output) and `tests/test_server_queries.py` (an example server with an N+1 in a loop, a batch built per row, a list route without the test and a missing index is caught; the good one with JOIN/IN and an index passes; the query headers never leave localhost and carry no SQL in production logs; the baseline only shrinks; lists without pagination and fetch calls without a time limit are caught) and `tests/test_pr_description.py` (lint copy equals the central one, the comment shows each check's real result, every panel metric is explained, create/update/skip on a fake API) and `tests/test_collect.sh` (every tool finds the problem planted in `tests/cases/site`; a heavy page fails the budget with a real Lighthouse run and the light one passes). They run in `.github/workflows/check.yml`.

Interaction: `tests/test_interaction.sh` runs a demo app (`tests/interaction/demo`) with one good page and one bad page per rule, in both passes, and checks that each bad page is caught by its own rule and the good pages pass; `tests/test_interaction_gate.py` proves the route scan, the coverage and new vs baseline (job `interaction`).

The test cases in `tests/cases/` contain made-up personal data on purpose (the bad cases the guard must catch).
