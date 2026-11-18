#!/usr/bin/env bash
# Installs the pinned security tools into $1 (default: $RUNNER_TEMP/security-tools):
# gitleaks (release tarball, SHA-256 checked) and Semgrep CE with all its
# dependencies (tools/semgrep-requirements.txt: exact versions and SHA-256, in
# its own virtualenv). Prints the folder to put on PATH.
# A new version is a pull request here: version + the published SHA-256.
set -euo pipefail
DEST="${1:-${RUNNER_TEMP:-/tmp}/security-tools}"
GITLEAKS_VERSION=8.30.1
mkdir -p "$DEST/bin"
os="$(uname -s)"; arch="$(uname -m)"
case "$os/$arch" in
  Linux/x86_64)  gl=linux_x64;    gl_sha=551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb ;;
  Linux/aarch64) gl=linux_arm64;  gl_sha=e4a487ee7ccd7d3a7f7ec08657610aa3606637dab924210b3aee62570fb4b080 ;;
  Darwin/arm64)  gl=darwin_arm64; gl_sha=b40ab0ae55c505963e365f271a8d3846efbc170aa17f2607f13df610a9aeb6a5 ;;
  *) echo "::error::No pinned security tools for $os/$arch" >&2; exit 1 ;;
esac
check() { # file, expected sha256
  local got; got="$(shasum -a 256 "$1" | cut -d' ' -f1)"
  [[ "$got" == "$2" ]] || { echo "::error::SHA-256 mismatch for $(basename "$1"): $got" >&2; exit 1; }
}
tmp="$(mktemp -d)"
if [[ ! -x "$DEST/bin/gitleaks" ]]; then
  f="gitleaks_${GITLEAKS_VERSION}_${gl}.tar.gz"
  curl -fsSL -o "$tmp/$f" "https://github.com/gitleaks/gitleaks/releases/download/v${GITLEAKS_VERSION}/$f"
  check "$tmp/$f" "$gl_sha"
  tar -xzf "$tmp/$f" -C "$tmp" gitleaks && mv "$tmp/gitleaks" "$DEST/bin/gitleaks"
fi
if [[ ! -x "$DEST/bin/semgrep" ]]; then
  # Semgrep and every dependency at an exact version, each file checked against
  # its SHA-256 (pip --require-hashes), wheels only: no install script runs.
  # Semgrep's wheels cover Python 3.10 to 3.14.
  py=""
  for c in python3.12 python3.13 python3.14 python3.11 python3.10 python3; do
    if command -v "$c" >/dev/null && "$c" -c 'import sys; sys.exit(not (3, 10) <= sys.version_info[:2] <= (3, 14))'; then
      py="$c"; break
    fi
  done
  [[ -n "$py" ]] || { echo "::error::Semgrep needs Python 3.10 to 3.14" >&2; exit 1; }
  "$py" -m venv "$DEST/semgrep-venv"
  "$DEST/semgrep-venv/bin/pip" install --quiet --disable-pip-version-check --require-hashes \
    --only-binary :all: -r "$(dirname "$0")/../tools/semgrep-requirements.txt" >&2
  ln -sf "$DEST/semgrep-venv/bin/semgrep" "$DEST/bin/semgrep"
fi
rm -rf "$tmp"
echo "$DEST/bin"
