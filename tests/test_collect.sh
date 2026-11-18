#!/usr/bin/env bash
# Proof that every tool runs and is read: a small bad site must show up in
# each metric. Needs Node and Chrome (GitHub's ubuntu image has both).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
OUT="$(mktemp -d)"
cd "$HERE/casos/site"
OUT="$OUT" LINT_PATHS="." HTML_GLOBS="*.html" SITE_DIR=. LH_PATHS="/" LH_RUNS=1 BUNDLE_DIR=. \
  MODERN=true TEST_COMMAND="echo '  ok   um'; echo '  FALHOU dois'" \
  bash "$HERE/../scripts/web_ci.sh" > "$OUT/log.txt" 2>&1 || { cat "$OUT/log.txt"; exit 1; }
python3 - "$OUT/health.json" <<'PY'
import json, sys
m = json.load(open(sys.argv[1]))["metrics"]
rules = lambda k: {c["rule"] for c in m[k]}
assert "@typescript-eslint/no-unused-vars" in rules("eslint"), m["eslint"]
assert "no-dup-id" in rules("html"), m["html"]
assert any(c["text"].endswith("sumiu.html") for c in m["links"]), m["links"]
assert "image-alt" in rules("axe"), m["axe"]
assert "/" in m["lighthouse"] and "performance" in m["lighthouse"]["/"], m["lighthouse"]
assert "css/use-baseline" in rules("modern"), m["modern"]
assert "js/replace-children-innerhtml" in rules("modern"), m["modern"]
assert m["bundle"]["gzip_kb"] > 0, m["bundle"]
assert m["tests"] == {"passed": 1, "failed": 1, "exit": 0}, m["tests"]
print("collect: ok")
PY
