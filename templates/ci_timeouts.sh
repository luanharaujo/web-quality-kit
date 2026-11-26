# Test time limits that are generous in CI, for shell test scripts (copy of
# web-quality-kit templates/ci_timeouts.sh; load it with `. ci_timeouts.sh`).
# A CI job may run on a slow or busy machine: with CI=true no wait is under
# 30 s, so a red test means a real error. Whole seconds only.
#
#   curl --max-time "$(ci_limit 10)" "$API/health"
#   wait_until 10 curl -sf -o /dev/null "$API/health" || exit 1

# ci_limit <seconds>: the limit to use (0 stays "no limit").
ci_limit() {
  case "${CI:-}" in
    true | TRUE | True | 1) if [ "$1" -gt 0 ] && [ "$1" -lt 30 ]; then echo 30; return; fi ;;
  esac
  echo "$1"
}

# wait_until <seconds> <command...>: run the command twice a second until it
# succeeds (0) or the limit passes (1). It returns as soon as it succeeds.
wait_until() {
  local limit end
  limit="$(ci_limit "$1")"
  shift
  end=$((SECONDS + limit))
  until "$@" >/dev/null 2>&1; do
    if [ "$SECONDS" -ge "$end" ]; then return 1; fi
    sleep 0.5
  done
}
