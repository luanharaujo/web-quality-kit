#!/usr/bin/env bash
# Secrets (gitleaks) and code patterns (Semgrep CE) for one repository, "never
# worse": in a pull request only the new commits / new findings count; on a
# push only the pushed commits; MODE=full scans everything and only reports.
#
# Environment:
#   MODE             pr | push | full
#   BASE             base branch (pr), e.g. main
#   RANGE            commit range (push), e.g. <before>..<after>
#   GITLEAKS_CONFIG  config file; empty = .gitleaks.toml in the repo if present, else gitleaks' defaults
#   SEMGREP_CONFIG   rules, default p/ci (Semgrep registry; no token, no code sent, metrics off)
#   SEMGREP_PATHS    folders to scan, default .
#   OUT              output folder (default: a new temporary one); the panel goes to $OUT/security.md
# Exit 1 = a new secret or a new Semgrep finding (not in full mode).
set -uo pipefail
MODE="${MODE:-full}"
OUT="${OUT:-$(mktemp -d)}"
mkdir -p "$OUT"
export SEMGREP_ENABLE_VERSION_CHECK=0 SEMGREP_SEND_METRICS=off
fail=0
md="$OUT/security.md"
echo "## Security" > "$md"
echo >> "$md"
echo "| | Check | Result |" >> "$md"
echo "|---|---|---|" >> "$md"

# --- secrets -------------------------------------------------------------
# In a pull request the config comes from the base branch, never from the
# pull request itself (an allowlist added there could hide what it adds).
cfg=()
if [[ -n "${GITLEAKS_CONFIG:-}" ]]; then cfg=(--config "$GITLEAKS_CONFIG")
elif [[ "$MODE" == pr ]]; then
  if git show "origin/${BASE:?}:.gitleaks.toml" > "$OUT/base.gitleaks.toml" 2>/dev/null; then
    cfg=(--config "$OUT/base.gitleaks.toml")
  else
    printf '[extend]\nuseDefault = true\n' > "$OUT/base.gitleaks.toml"; cfg=(--config "$OUT/base.gitleaks.toml")
  fi
elif [[ -f .gitleaks.toml ]]; then cfg=(--config .gitleaks.toml); fi
# Pull request: ignore lists also come from the base branch, and inline
# "gitleaks:allow" comments do not count.
extra=()
if [[ "$MODE" == pr ]]; then
  mkdir -p "$OUT/base-ignore"
  git show "origin/${BASE}:.gitleaksignore" > "$OUT/base-ignore/.gitleaksignore" 2>/dev/null || : > "$OUT/base-ignore/.gitleaksignore"
  extra=(--gitleaks-ignore-path "$OUT/base-ignore" --ignore-gitleaks-allow)
fi
case "$MODE" in
  pr)   opts="origin/${BASE:?}..HEAD" ;;
  push) opts="${RANGE:-HEAD~1..HEAD}" ;;
  *)    opts="--all" ;;
esac
gitleaks git ${cfg[@]+"${cfg[@]}"} ${extra[@]+"${extra[@]}"} --no-banner --redact --exit-code 0 --log-opts="$opts" \
  --report-format json --report-path "$OUT/gitleaks.json" . > "$OUT/gitleaks.log" 2>&1
grc=$?
if [[ $grc -ne 0 || ! -s "$OUT/gitleaks.json" ]]; then
  echo "::error::gitleaks did not run (exit $grc)"; cat "$OUT/gitleaks.log"; exit 1
fi
n=$(python3 -c 'import json,sys; print(len(json.load(open(sys.argv[1]))))' "$OUT/gitleaks.json")
scope=$([[ $MODE == full ]] && echo "whole history" || echo "new commits")
if [[ $n -gt 0 ]]; then
  [[ $MODE != full ]] && fail=1
  echo "| $([[ $MODE == full ]] && echo 🟡 || echo 🔴) | Secrets (gitleaks, $scope) | $n found |" >> "$md"
else
  echo "| 🟢 | Secrets (gitleaks, $scope) | none |" >> "$md"
fi

# --- code patterns -------------------------------------------------------
# Semgrep only reads the .semgrepignore of the checked-out code. A pull request
# that changes it is not scanned on trust: it fails, and the owner reviews the
# change (then it can go in on its own pull request, approved by a person).
if [[ "$MODE" == pr ]] && ! git diff --quiet "origin/${BASE}...HEAD" -- .semgrepignore; then
  echo "| 🔴 | Code patterns (Semgrep CE) | this pull request changes .semgrepignore: review it by hand |" >> "$md"
  echo "::error::.semgrepignore changed in this pull request: Semgrep's exclusions need a person's review"
  fail=1
fi
sargs=(scan --config "${SEMGREP_CONFIG:-p/ci}" --metrics=off --json --output "$OUT/semgrep.json" --quiet)
# In a pull request "# nosemgrep" comments do not count: one added there could
# hide its own finding.
[[ $MODE == pr ]] && sargs+=(--baseline-commit "$(git merge-base "origin/${BASE}" HEAD)" --disable-nosem)
# shellcheck disable=SC2086
semgrep "${sargs[@]}" ${SEMGREP_PATHS:-.} > "$OUT/semgrep.log" 2>&1
src=$?
if [[ ! -s "$OUT/semgrep.json" ]]; then
  echo "::error::Semgrep did not run (exit $src)"; tail -20 "$OUT/semgrep.log"; exit 1
fi
m=$(python3 -c 'import json,sys; print(len(json.load(open(sys.argv[1]))["results"]))' "$OUT/semgrep.json")
sscope=$([[ $MODE == pr ]] && echo "new in this pull request" || echo "whole code")
if [[ $m -gt 0 ]]; then
  [[ $MODE == pr ]] && fail=1
  echo "| $([[ $MODE == pr ]] && echo 🔴 || echo 🟡) | Code patterns (Semgrep CE, $sscope) | $m found |" >> "$md"
else
  echo "| 🟢 | Code patterns (Semgrep CE, $sscope) | none |" >> "$md"
fi

# --- details (secrets are redacted by gitleaks) ---------------------------
python3 - "$OUT/gitleaks.json" "$OUT/semgrep.json" >> "$md" <<'PY'
import json, sys
leaks = json.load(open(sys.argv[1]))
res = json.load(open(sys.argv[2]))["results"]
if leaks:
    print("\n<details><summary>Secrets</summary>\n\n| Where | Rule | Commit |\n|---|---|---|")
    for l in leaks[:50]:
        print(f"| `{l['File']}:{l['StartLine']}` | {l['RuleID']} | {l['Commit'][:7]} |")
    print("\n</details>")
if res:
    print("\n<details><summary>Code patterns</summary>\n\n| Where | Rule | Severity |\n|---|---|---|")
    for r in res[:50]:
        print(f"| `{r['path']}:{r['start']['line']}` | {r['check_id'].split('.')[-1]} | {r['extra'].get('severity','')} |")
    print("\n</details>")
PY
[[ $fail -eq 1 ]] && echo "::error::New secret or code finding (see the Security panel)"
exit $fail
