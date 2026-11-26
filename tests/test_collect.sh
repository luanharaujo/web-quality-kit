#!/usr/bin/env bash
# Proof that every tool runs and is read: a small bad site must show up in
# each metric. Needs Node and Chrome (GitHub's ubuntu image has both).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
OUT="$(mktemp -d)"
cd "$HERE/cases/site"
OUT="$OUT" LINT_PATHS="." HTML_GLOBS="*.html" SITE_DIR=. LH_PATHS="/" LH_RUNS=1 BUNDLE_DIR=. \
  LINKS_SKIP_PATHS="/outside.webmanifest" MODERN=true MWG_INCLUDE_TESTS=1 TEST_COMMAND="echo '  ok   um'; echo '  FALHOU dois'" \
  bash "$HERE/../scripts/web_ci.sh" > "$OUT/log.txt" 2>&1 || { cat "$OUT/log.txt"; exit 1; }
python3 - "$OUT/health.json" <<'PY'
import json, sys
m = json.load(open(sys.argv[1]))["metrics"]
rules = lambda k: {c["rule"] for c in m[k]}
assert "@typescript-eslint/no-unused-vars" in rules("eslint"), m["eslint"]
assert "no-dup-id" in rules("html"), m["html"]
assert any(c["text"].endswith("gone.html") for c in m["links"]), m["links"]
assert not any("outside.webmanifest" in c["text"] for c in m["links"]), m["links"]
assert "image-alt" in rules("axe"), m["axe"]
assert "/" in m["lighthouse"] and "performance" in m["lighthouse"]["/"], m["lighthouse"]
assert "css/use-baseline" in rules("modern"), m["modern"]
assert "dynamic-sibling-animations" in rules("modern"), m["modern"]
assert m["bundle"]["gzip_kb"] > 0, m["bundle"]
assert m["tests"] == {"passed": 1, "failed": 1, "exit": 0}, m["tests"]
print("collect: ok")
PY

# Without links-skip-paths the same link is reported (the skip is what hides it).
OUT2="$(mktemp -d)"
OUT="$OUT2" SITE_DIR=. bash "$HERE/../scripts/web_ci.sh" > "$OUT2/log.txt" 2>&1 || { cat "$OUT2/log.txt"; exit 1; }
python3 - "$OUT2/health.json" <<'PY'
import json, sys
m = json.load(open(sys.argv[1]))["metrics"]
assert any("outside.webmanifest" in c["text"] for c in m["links"]), m["links"]
print("links-skip-paths: ok")
PY

# Performance budget with a real Lighthouse run (phone, slow network): a heavy
# page (big incompressible script in <head>, long task at load) must blow the
# JS weight and LCP limits, and the absolute budget must fail the job.
HEAVY="$(mktemp -d)"
python3 - "$HEAVY" <<'PY'
import base64, os, sys
d = sys.argv[1]
blob = base64.b64encode(os.urandom(300 * 1024)).decode()
open(os.path.join(d, "big.js"), "w").write(
    f'window.BLOB = "{blob}";\nconst t = Date.now(); while (Date.now() - t < 600) {{}}\n')
open(os.path.join(d, "index.html"), "w").write(
    '<!doctype html><html lang="en"><head><meta charset="utf-8">'
    '<meta name="viewport" content="width=device-width, initial-scale=1"><title>Heavy</title>'
    '<script src="/big.js"></script></head><body><main><h1>Heavy page</h1>'
    '<p>Text that becomes the largest paint once the blocking script is done.</p></main></body></html>\n')
PY
OUT3="$(mktemp -d)"
(cd "$HEAVY" && OUT="$OUT3" SITE_DIR=. LH_PATHS="/" LH_RUNS=1 BUDGET_MODE=absolute \
  bash "$HERE/../scripts/web_ci.sh" > "$OUT3/log.txt" 2>&1) || { cat "$OUT3/log.txt"; exit 1; }
python3 - "$OUT3/health.json" <<'PY'
import json, sys
b = json.load(open(sys.argv[1]))["metrics"]["budget"]
assert not b["errors"], b["errors"]
page = b["pages"]["/"]
assert page["js_kb"]["value"] > page["js_kb"]["limit"], page
assert page["lcp_ms"]["value"] > page["lcp_ms"]["limit"], page
print("budget (real Lighthouse):", {k: v["value"] for k, v in page.items()})
PY
if python3 "$HERE/../scripts/web_health.py" compare - "$OUT3/health.json" --no-fail > "$OUT3/panel.md"; then
  cat "$OUT3/panel.md"; echo "absolute budget did not fail the heavy page"; exit 1
fi
echo "budget fails the heavy page: ok"

# The light test site meets the default budget.
OUT4="$(mktemp -d)"
(cd "$HERE/cases/site" && OUT="$OUT4" SITE_DIR=. LH_PATHS="/" LH_RUNS=1 BUDGET_MODE=absolute \
  BUDGET_FILE=/nonexistent bash "$HERE/../scripts/web_ci.sh" > "$OUT4/log.txt" 2>&1) || { cat "$OUT4/log.txt"; exit 1; }
python3 "$HERE/../scripts/web_health.py" compare - "$OUT4/health.json" > "$OUT4/panel.md" \
  || { cat "$OUT4/panel.md"; echo "the light site should meet the budget"; exit 1; }
echo "budget passes the light page: ok"

# budget-mode without Lighthouse pages is refused.
if OUT="$(mktemp -d)" SITE_DIR=. BUDGET_MODE=absolute bash "$HERE/../scripts/web_ci.sh" > /dev/null 2>&1; then
  echo "budget-mode without lighthouse-paths should fail"; exit 1
fi
echo "budget needs lighthouse-paths: ok"

# Site rules through web_ci.sh: the bad test site has a PNG and no _headers.
OUT5="$(mktemp -d)"
(cd "$HERE/cases/site" && OUT="$OUT5" SITE_DIR=. SITE_RULES=absolute BUDGET_FILE=/nonexistent \
  bash "$HERE/../scripts/web_ci.sh" > "$OUT5/log.txt" 2>&1) || { cat "$OUT5/log.txt"; exit 1; }
python3 - "$OUT5/health.json" <<'PY'
import json, sys
m = json.load(open(sys.argv[1]))["metrics"]
assert m["site_rules"]["mode"] == "absolute", m.get("site_rules")
assert any(c["rule"] == "cache-headers-file" for c in m["cache"]), m["cache"]
assert any(c["rule"] == "img-format" and c["text"].endswith("foto.png") for c in m["images"]), m["images"]
print("site rules through web_ci.sh: ok")
PY
if python3 "$HERE/../scripts/web_health.py" compare - "$OUT5/health.json" --no-fail > /dev/null; then
  echo "absolute site rules did not fail the bad site"; exit 1
fi
echo "site rules fail the bad site: ok"

# The login check runs inside web_ci.sh: the bad example is caught, the site without a login is not.
OUTA="$(mktemp -d)"
(cd "$HERE/cases/auth/bad" && OUT="$OUTA" AUTH_DYNAMIC=false bash "$HERE/../scripts/web_ci.sh" > "$OUTA/log.txt" 2>&1) || { cat "$OUTA/log.txt"; exit 1; }
python3 - "$OUTA/health.json" "$OUT/health.json" <<'PY'
import json, sys
bad = json.load(open(sys.argv[1]))["metrics"]["auth"]
assert bad["login"] and any(f["rule"] == "A1" for f in bad["findings"]), bad
site = json.load(open(sys.argv[2]))["metrics"]["auth"]
assert not site["login"] and site["findings"] == [], site
print("auth: ok")
PY
