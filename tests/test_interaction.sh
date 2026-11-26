#!/usr/bin/env bash
# Proof of the interaction checks: the demo app's bad pages are caught, each
# by its rule, and the good pages pass. Runs both passes the action runs.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
kit="$here/.."
demo="$here/interaction"
out="$(mktemp -d)"
trap 'rm -rf "$out" "$demo/node_modules"' EXIT

(cd "$kit/interaction" && npm ci --ignore-scripts --no-fund --no-audit --loglevel=error)
mkdir -p "$demo/node_modules"
ln -sfn "$kit/interaction" "$demo/node_modules/web-quality-kit-interaction"
pw="$kit/interaction/node_modules/.bin/playwright"

cd "$demo"
# The read checks only warn by default; here they must count, to prove them.
export KIT_INTERACTION_READS=enforce
for mode in slow fail; do
  KIT_INTERACTION_MODE=$mode KIT_INTERACTION_ENFORCE=record KIT_INTERACTION_REPORT_DIR="$out/$mode" \
    "$pw" test -c playwright.config.mjs --output "$out/results-$mode" >"$out/$mode.log" 2>&1 \
    || { echo "playwright ($mode) failed:"; cat "$out/$mode.log"; exit 1; }
done
node "$demo/check_report.mjs" "$out/slow" "$out/fail"
python3 "$here/test_interaction_gate.py"
