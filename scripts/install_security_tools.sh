#!/usr/bin/env bash
# Installs the pinned security tools into $1 (default: $RUNNER_TEMP/security-tools):
# gitleaks (release tarball, SHA-256 checked) and Semgrep CE (wheel, SHA-256
# checked, in its own virtualenv). Prints the folder to put on PATH.
# A new version is a pull request here: version + the published SHA-256.
set -euo pipefail
DEST="${1:-${RUNNER_TEMP:-/tmp}/security-tools}"
GITLEAKS_VERSION=8.30.1
SEMGREP_VERSION=1.183.0
mkdir -p "$DEST/bin"
os="$(uname -s)"; arch="$(uname -m)"
case "$os/$arch" in
  Linux/x86_64)  gl=linux_x64;    gl_sha=551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb
                 sg=manylinux_2_34_x86_64; sg_sha=f36115c4d0cb43056ceb308273421a2f6a35e055e7e029601e130850f836646e ;;
  Linux/aarch64) gl=linux_arm64;  gl_sha=e4a487ee7ccd7d3a7f7ec08657610aa3606637dab924210b3aee62570fb4b080
                 sg=manylinux_2_34_aarch64; sg_sha=b058cc62ca32af79d2eaf44c3ff0931bfecc74857107eaf6b1b0ebb6676604ec ;;
  Darwin/arm64)  gl=darwin_arm64; gl_sha=b40ab0ae55c505963e365f271a8d3846efbc170aa17f2607f13df610a9aeb6a5
                 sg=macosx_11_0_arm64; sg_sha=fa13eb666ea84cb9d11cea244cb106fefde57ffe51e251a0f91c7de1c4da0944 ;;
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
  w="semgrep-${SEMGREP_VERSION}-cp310.cp311.cp312.cp313.cp314.py310.py311.py312.py313.py314-none-${sg}.whl"
  curl -fsSL -o "$tmp/$w" "https://files.pythonhosted.org/packages/py3/s/semgrep/$w" 2>/dev/null \
    || python3 -m pip download --quiet --no-deps --only-binary :all: --dest "$tmp" "semgrep==${SEMGREP_VERSION}"
  check "$tmp/$w" "$sg_sha"
  python3 -m venv "$DEST/semgrep-venv"
  "$DEST/semgrep-venv/bin/pip" install --quiet --disable-pip-version-check "$tmp/$w"
  ln -sf "$DEST/semgrep-venv/bin/semgrep" "$DEST/bin/semgrep"
fi
rm -rf "$tmp"
echo "$DEST/bin"
