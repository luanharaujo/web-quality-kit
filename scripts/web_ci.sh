#!/usr/bin/env bash
# Runs every check the caller turned on and writes their outputs to $OUT,
# then health.json. Same script in CI (actions/web-ci) and on a laptop.
# A check that finds problems does not stop the others: the verdict is
# web_health.py compare's job.
#
# Settings (environment, all optional; empty = check off):
#   LINT_PATHS      paths for ESLint, space separated
#   TSC_PROJECTS    one tsconfig directory per line (tsc --noEmit -p <dir>)
#   HTML_GLOBS      HTML files for html-validate, space separated globs
#   SITE_DIR        static site root for broken links and Lighthouse
#   LH_PATHS        pages for Lighthouse, space separated (e.g. "/ /treinos/")
#   SITE_MOUNT      serve SITE_DIR under this path (an app built with base /treinos/)
#   LINKS_SKIP_PATHS  site paths the broken-links check ignores (space separated, e.g.
#                   "/manifest.webmanifest"): files that live outside SITE_DIR on the
#                   real site, so this server cannot serve them. Exact path, any ?query
#   LH_CATEGORIES   Lighthouse categories shown, default all four (a noindex page: drop seo)
#   LH_RUNS         Lighthouse runs per page (median), default 3
#   BUDGET_MODE     "absolute" or "baseline" = performance budget on the Lighthouse
#                   pages (scripts/budget.py); needs LH_PATHS. Empty = off
#   BUDGET_FILE     the repository's web-budget.json (stricter limits, exceptions)
#   SITE_RULES      "absolute" or "baseline" = static rules on SITE_DIR (scripts/site_rules.py):
#                   cache headers, fonts, images. Empty = off
#   HEADERS_FILE    the published _headers file, default SITE_DIR/_headers
#   BUNDLE_DIR      folder whose .js/.css are summed; BUNDLE_MAX_KB its limit (gzip)
#   AUDIT_DIRS      folders with a package-lock.json for npm audit, space separated
#   TEST_COMMAND    shell command that runs the tests (output parsed for ok/FALHOU)
#   DATA_GUARD      "true" = look for personal data and secrets (public repositories)
#   SERVER_QUERIES  the repository's server-queries.json = database checks of the server
#                   (scripts/server_queries.py: N+1 with 3 vs 30 rows, list routes
#                   without that test, missing indexes) and the query-in-a-loop
#                   lint (warning only) on its "source" folders. Empty = off
#   MODERN          "true" = Baseline CSS check (@eslint/css use-baseline) and Modern Web
#                   Guidance legacy-scan (old code with a native replacement), warning only
set -uo pipefail
KIT="${KIT:-$(cd "$(dirname "$0")/.." && pwd)}"
OUT="${OUT:-$PWD/.web-health}"
BIN="$KIT/tools/node_modules/.bin"
mkdir -p "$OUT"
args=(collect --root "$PWD")
log() { echo "::group::$1"; }
end() { echo "::endgroup::"; }

if [[ ! -x "$BIN/eslint" ]]; then
  (cd "$KIT/tools" && npm ci --ignore-scripts --no-fund --no-audit --loglevel=error) || { echo "::error::Could not install the kit's tools"; exit 1; }
fi

if [[ -n "${LINT_PATHS:-}" ]]; then
  log "ESLint"
  cfg="$KIT/tools/eslint.config.mjs"
  [[ -n "${ESLINT_CONFIG:-}" ]] && cfg="$ESLINT_CONFIG"
  # shellcheck disable=SC2086
  KIT_LINT_ROOT="$PWD" "$BIN/eslint" --config "$cfg" --no-warn-ignored --no-error-on-unmatched-pattern --format json $LINT_PATHS > "$OUT/eslint.json"
  rc=$?
  if [[ $rc -gt 1 ]]; then echo "::error::ESLint did not run (exit $rc)"; exit 1; fi
  args+=(--eslint "$OUT/eslint.json")
  end
fi

if [[ -n "${TSC_PROJECTS:-}" ]]; then
  log "tsc"
  i=0
  while read -r dir; do
    [[ -z "$dir" ]] && continue
    i=$((i + 1))
    tsc="$dir/node_modules/.bin/tsc"
    [[ -x "$tsc" ]] || tsc="$BIN/tsc"
    (cd "$dir" && "$tsc" --noEmit -p . --pretty false) > "$OUT/tsc_$i.txt" 2>&1
    grep -c "error TS" "$OUT/tsc_$i.txt" || true
    args+=(--tsc "$dir:$OUT/tsc_$i.txt")
  done <<< "$TSC_PROJECTS"
  end
fi

if [[ -n "${HTML_GLOBS:-}" ]]; then
  log "html-validate"
  # The globs go to html-validate as written (set -f), so "site/**/*.html" also
  # matches site/index.html, which bash without globstar would miss.
  set -f
  # shellcheck disable=SC2086
  "$BIN/html-validate" --config "$KIT/tools/htmlvalidate.json" --formatter json $HTML_GLOBS > "$OUT/html.json"
  set +f
  [[ -s "$OUT/html.json" ]] || echo "[]" > "$OUT/html.json"
  args+=(--html "$OUT/html.json")
  end
fi

if [[ -n "${SITE_RULES:-}" ]]; then
  [[ "$SITE_RULES" == "absolute" || "$SITE_RULES" == "baseline" ]] \
    || { echo "::error::site-rules must be absolute or baseline, not '$SITE_RULES'"; exit 1; }
  [[ -n "${SITE_DIR:-}" ]] || { echo "::error::site-rules needs site-dir"; exit 1; }
  log "Site rules (cache, fonts, images)"
  python3 "$KIT/scripts/site_rules.py" check "$SITE_DIR" --mode "$SITE_RULES" \
    --config "${BUDGET_FILE:-web-budget.json}" ${HEADERS_FILE:+--headers "$HEADERS_FILE"} --mount "${SITE_MOUNT:-}" > "$OUT/site_rules.json" \
    || { echo "::error::site_rules.py did not run"; exit 1; }
  args+=(--site-rules "$OUT/site_rules.json")
  end
fi

if [[ -n "${SITE_DIR:-}" && -n "${SITE_MOUNT:-}" ]]; then
  mounted="$OUT/site"
  rm -rf "$mounted" && mkdir -p "$mounted$SITE_MOUNT" && cp -R "$SITE_DIR/." "$mounted$SITE_MOUNT/"
  SITE_DIR="$mounted"
fi

if [[ -n "${SITE_DIR:-}" ]]; then
  log "Broken links (only this site's own links)"
  # Our own static server, so root-relative links (/treinos/assets/...) resolve
  # the way they do on the real site; the check starts at each Lighthouse page.
  port=$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])')
  python3 -m http.server "$port" --bind 127.0.0.1 --directory "$SITE_DIR" > "$OUT/http.log" 2>&1 & SERVER=$!
  for _ in $(seq 1 50); do curl -s -o /dev/null "http://127.0.0.1:$port/" && break; sleep 0.1; done
  starts=()
  for p in ${LH_PATHS:-${SITE_MOUNT:-}/}; do starts+=("http://127.0.0.1:$port$p"); done
  skip='^https?://(?!(localhost|127\.0\.0\.1)[:/])'
  if [[ -n "${LINKS_SKIP_PATHS:-}" ]]; then
    alt=""
    for p in $LINKS_SKIP_PATHS; do
      [[ "$p" == /* ]] || { echo "::error::links-skip-paths: '$p' must start with /"; kill "$SERVER"; exit 1; }
      alt+="${alt:+|}$(printf '%s' "$p" | sed 's/[][\.^$*+?(){}|/]/\\&/g')"
    done
    skip+="|^http://127\\.0\\.0\\.1:$port($alt)([?#].*)?\$"
  fi
  "$BIN/linkinator" "${starts[@]}" --recurse --format json --skip "$skip" \
    > "$OUT/links.json" 2> "$OUT/links.err"
  kill "$SERVER" 2>/dev/null
  python3 -c 'import json,sys; json.load(open(sys.argv[1]))' "$OUT/links.json" 2>/dev/null \
    || { echo "::warning::linkinator gave no report"; cat "$OUT/links.err"; echo '{}' > "$OUT/links.json"; }
  args+=(--links "$OUT/links.json")
  end
fi

if [[ -n "${BUDGET_MODE:-}" ]]; then
  [[ "$BUDGET_MODE" == "absolute" || "$BUDGET_MODE" == "baseline" ]] \
    || { echo "::error::budget-mode must be absolute or baseline, not '$BUDGET_MODE'"; exit 1; }
  [[ -n "${SITE_DIR:-}" && -n "${LH_PATHS:-}" ]] \
    || { echo "::error::budget-mode needs site-dir and lighthouse-paths"; exit 1; }
fi

if [[ -n "${SITE_DIR:-}" && -n "${LH_PATHS:-}" ]]; then
  log "Lighthouse"
  rm -rf "$OUT/lhci" .lighthouseci
  urls=()
  for p in $LH_PATHS; do urls+=(--url="http://localhost$p"); done
  "$BIN/lhci" collect --staticDistDir="$SITE_DIR" "${urls[@]}" -n "${LH_RUNS:-3}" \
    --settings.chromeFlags="--headless=new --no-sandbox --disable-dev-shm-usage" \
    --settings.formFactor=mobile --settings.throttlingMethod=simulate > "$OUT/lhci.log" 2>&1 \
    || { echo "::warning::Lighthouse did not finish"; tail -20 "$OUT/lhci.log"; }
  [[ -d .lighthouseci ]] && mv .lighthouseci "$OUT/lhci"
  args+=(--lhci "$OUT/lhci" --lh-categories "${LH_CATEGORIES:-performance accessibility best-practices seo}")
  if [[ -n "${BUDGET_MODE:-}" ]]; then
    # Pages as Lighthouse saw them (under SITE_MOUNT when the site is mounted).
    python3 "$KIT/scripts/budget.py" check "$OUT/lhci" --mode "$BUDGET_MODE" \
      --config "${BUDGET_FILE:-web-budget.json}" --pages "$LH_PATHS" > "$OUT/budget.json" \
      || { echo "::error::budget.py did not run"; exit 1; }
    args+=(--budget "$OUT/budget.json")
  fi
  end
fi

if [[ -n "${BUNDLE_DIR:-}" ]]; then
  args+=(--bundle "$BUNDLE_DIR" --bundle-max-kb "${BUNDLE_MAX_KB:-0}")
fi

if [[ -n "${AUDIT_DIRS:-}" ]]; then
  log "npm audit"
  i=0
  for dir in $AUDIT_DIRS; do
    i=$((i + 1))
    (cd "$dir" && npm audit --json --package-lock-only) > "$OUT/audit_$i.json" 2> "$OUT/audit_$i.err"
    python3 -c 'import json,sys; json.load(open(sys.argv[1]))["metadata"]' "$OUT/audit_$i.json" 2>/dev/null \
      || { echo "::warning::npm audit failed in $dir"; cat "$OUT/audit_$i.err"; echo '{}' > "$OUT/audit_$i.json"; }
    args+=(--audit "$dir:$OUT/audit_$i.json")
  done
  end
fi

if [[ -n "${TEST_COMMAND:-}" ]]; then
  log "Tests"
  bash -c "$TEST_COMMAND" 2>&1 | tee "$OUT/tests.txt"
  args+=(--tests "$OUT/tests.txt" --tests-exit "${PIPESTATUS[0]}")
  end
fi

if [[ -n "${SERVER_QUERIES:-}" ]]; then
  log "Database (N+1, routes, indexes)"
  python3 "$KIT/scripts/server_queries.py" check --config "$SERVER_QUERIES" --root "$PWD" > "$OUT/server_queries.json" \
    || { echo "::error::server_queries.py did not run"; exit 1; }
  args+=(--server-queries "$OUT/server_queries.json")
  src=()
  while IFS= read -r d; do [[ -n "$d" ]] && src+=("$d"); done \
    < <(python3 -c 'import json,sys; print("\n".join(json.load(open(sys.argv[1])).get("source", [])))' "$SERVER_QUERIES" 2>/dev/null)
  if [[ ${#src[@]} -gt 0 ]]; then
    node "$KIT/tools/query_in_loop.mjs" "$PWD" "${src[@]}" > "$OUT/query_loop.json" \
      || { echo "::warning::query-in-loop lint failed"; echo '[]' > "$OUT/query_loop.json"; }
    args+=(--query-loop "$OUT/query_loop.json")
  fi
  end
fi

if [[ "${DATA_GUARD:-}" == "true" ]]; then
  python3 "$KIT/scripts/data_guard.py" --root "$PWD" > "$OUT/data.json"
  args+=(--data "$OUT/data.json")
fi

if [[ "${MODERN:-}" == "true" ]]; then
  log "Modern web (Baseline CSS, legacy-scan)"
  KIT_LINT_ROOT="$PWD" "$BIN/eslint" --config "$KIT/tools/eslint.css.config.mjs" --no-warn-ignored \
    --format json . > "$OUT/modern.json"
  rc=$?
  if [[ $rc -gt 1 ]]; then echo "::error::ESLint (Baseline CSS) did not run (exit $rc)"; exit 1; fi
  args+=(--modern "$OUT/modern.json")
  # Old code with a native replacement (Modern Web Guidance by the Chrome,
  # Edge, Firefox and MDN teams), on the same paths as the lint.
  i=0
  for dir in ${LINT_PATHS:-.}; do
    [[ -d "$dir" ]] || continue
    i=$((i + 1))
    DISABLE_TELEMETRY=1 "$BIN/modern-web-guidance" legacy-scan "$dir" --json ${MWG_INCLUDE_TESTS:+--include-docs-and-tests} > "$OUT/legacy_$i.json" 2> "$OUT/legacy_$i.err" \
      || { echo "::warning::legacy-scan failed in $dir"; cat "$OUT/legacy_$i.err"; echo '{}' > "$OUT/legacy_$i.json"; }
    args+=(--legacy "$dir:$OUT/legacy_$i.json")
  done
  end
fi

python3 "$KIT/scripts/web_health.py" "${args[@]}" > "$OUT/health.json"
