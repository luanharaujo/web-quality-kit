#!/usr/bin/env bash
# Proof of the security job on a bait repository built here: a fake token and
# an eval() planted in a pull request must fail it; the same things already on
# main must not (only new ones count); full mode only reports. The fake token
# is assembled at run time, so this file holds no secret.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
SCAN="$HERE/../scripts/security_scan.sh"
RULES="$HERE/cases/semgrep/rules.yml"
T="$(mktemp -d)"
trap 'rm -rf "$T"' EXIT
token="ghp_$(printf 'a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8')"
mkdir "$T/repo" && cd "$T/repo" && git init -q -b main && git config user.email t@example.com && git config user.name t
echo 'export const ok = 1' > app.js && git add . && git commit -qm base
git clone -q --bare "$T/repo" "$T/origin.git" && git remote add origin "$T/origin.git" && git fetch -q origin

run() { rm -rf "$T/out"; MODE=$1 BASE=main SEMGREP_CONFIG="$RULES" OUT="$T/out" bash "$SCAN" > "$T/run.log" 2>&1; }

# 1. a clean pull request passes
git switch -qc clean && echo 'export const two = 2' >> app.js && git commit -qam clean
run pr || { cat "$T/run.log" "$T/out/security.md"; echo "clean PR failed"; exit 1; }

# 2. a pull request with a token fails, and the token is not in the output
git switch -q main && git switch -qc leak && echo "const t = \"$token\"" > cfg.js && git add . && git commit -qm leak
if run pr; then echo "token not caught"; exit 1; fi
grep -q "github-pat" "$T/out/security.md" || { cat "$T/out/security.md"; echo "no rule in panel"; exit 1; }
! grep -rq "$token" "$T/out" "$T/run.log" || { echo "token leaked in the output"; exit 1; }

# 3. a pull request adding eval() fails
git switch -q main && git switch -qc evil && echo 'eval(location.hash)' >> app.js && git commit -qam evil
if run pr; then echo "eval not caught"; exit 1; fi
grep -q "no-eval" "$T/out/security.md" || { cat "$T/out/security.md"; exit 1; }

# 4. eval() already on main: a new, clean pull request still passes (only new findings count)
git switch -q main && git merge -q evil && git push -q origin main && git fetch -q origin
git switch -qc later && echo 'export const three = 3' >> app.js && git commit -qam later
run pr || { cat "$T/run.log" "$T/out/security.md"; echo "old finding blocked a clean PR"; exit 1; }

# 5. a pull request cannot allowlist its own finding (base config, # nosemgrep ignored)
git switch -q main && git switch -qc sneaky
printf '[extend]\nuseDefault = true\n[allowlist]\npaths = ["cfg2.js"]\n' > .gitleaks.toml
echo "const t = \"$token\"" > cfg2.js
echo 'eval(location.search) // nosemgrep' >> app.js
git add . && git commit -qm sneaky
if run pr; then cat "$T/out/security.md"; echo "allowlist in the pull request hid its own finding"; exit 1; fi
grep -q "github-pat" "$T/out/security.md" && grep -q "no-eval" "$T/out/security.md" || { cat "$T/out/security.md"; exit 1; }

# 6. nor with .gitleaksignore, gitleaks:allow or .semgrepignore
git switch -q main && git switch -qc sneaky2
echo "const t = \"$token\" // gitleaks:allow" > cfg3.js
git add . && git commit -qm sneaky2
if run pr; then echo "gitleaks:allow hid a token"; exit 1; fi
git switch -q main && git switch -qc sneaky3
echo 'eval(window.name)' > hidden.js && echo 'hidden.js' > .semgrepignore
git add . && git commit -qm sneaky3
if run pr; then echo ".semgrepignore hid a finding"; exit 1; fi
grep -q "semgrepignore" "$T/out/security.md" || { cat "$T/out/security.md"; exit 1; }

# 7. fail closed: a scanner that cannot run fails the job
git switch -q later
if GITLEAKS_CONFIG=/nonexistent/config.toml run pr; then echo "broken gitleaks passed"; exit 1; fi
grep -q "gitleaks did not run" "$T/run.log" || { cat "$T/run.log"; exit 1; }

# 8. full mode reports and never fails
run full || { cat "$T/run.log"; echo "full mode failed"; exit 1; }
grep -q "no-eval" "$T/out/security.md"
echo "security: ok"
