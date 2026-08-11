#!/usr/bin/env bash
# Download the PocketBase binary for this machine into ./pocketbase/
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
VERSION="${POCKETBASE_VERSION:-0.39.10}"
OS="$(uname -s | tr '[:upper:]' '[:lower:]')"
ARCH="$(uname -m)"

case "$ARCH" in
  x86_64|amd64) ARCH="amd64" ;;
  arm64|aarch64) ARCH="arm64" ;;
  *) echo "Unsupported architecture: $ARCH" >&2; exit 1 ;;
esac

case "$OS" in
  darwin|linux) ;;
  *) echo "Unsupported OS: $OS" >&2; exit 1 ;;
esac

ZIP="pocketbase_${VERSION}_${OS}_${ARCH}.zip"
URL="https://github.com/pocketbase/pocketbase/releases/download/v${VERSION}/${ZIP}"
TMP="$(mktemp -d)"

echo "Downloading PocketBase v${VERSION} (${OS}/${ARCH})..."
curl -fsSL "$URL" -o "${TMP}/${ZIP}"
unzip -qo "${TMP}/${ZIP}" -d "$TMP"
mv "${TMP}/pocketbase" "${ROOT}/pocketbase"
chmod +x "${ROOT}/pocketbase"
rm -rf "$TMP"
echo "Installed: ${ROOT}/pocketbase"
