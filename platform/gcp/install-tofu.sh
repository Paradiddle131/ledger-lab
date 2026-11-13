#!/usr/bin/env bash
# Install the pinned OpenTofu into ~/.local/bin, refusing a download whose checksum differs from
# the one recorded here. Linux x86-64 only; elsewhere, install this version by hand.
set -euo pipefail
VERSION=1.11.5
SHA256=901121681e751574d739de5208cad059eddf9bd739b575745cf9e3c961b28a13
BIN="$HOME/.local/bin"

if "$BIN/tofu" version 2>/dev/null | head -1 | grep -qx "OpenTofu v$VERSION"; then
  echo "tofu: $VERSION already installed"; exit 0
fi
work="$(mktemp -d)"
trap 'rm -rf -- "$work"' EXIT
zip="$work/tofu.zip"
curl -fsSL -o "$zip" "https://github.com/opentofu/opentofu/releases/download/v$VERSION/tofu_${VERSION}_linux_amd64.zip"
echo "$SHA256  $zip" | sha256sum -c --quiet
mkdir -p "$BIN"
python3 -c 'import sys, zipfile; zipfile.ZipFile(sys.argv[1]).extract("tofu", sys.argv[2])' "$zip" "$BIN"
chmod +x "$BIN/tofu"
"$BIN/tofu" version | head -1
