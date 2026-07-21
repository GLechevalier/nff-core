#!/bin/sh
# nff installer — macOS / Linux
#
#   curl -fsSL https://nanoforgeflow.com/install.sh | sh
#   curl -fsSL https://raw.githubusercontent.com/GLechevalier/nff/main/scripts/install.sh | sh   # mirror
#
# Downloads the prebuilt standalone binary from the latest GitHub Release
# (or a pinned version) and installs it to ~/.local/bin.
#
#   NFF_VERSION=0.2.40 curl -fsSL .../install.sh | sh    # pin a version
#   ./install.sh 0.2.40                                  # same, as an argument
#
# A copy of this file is served from the nanoforgeflow-landing repo's `public/`
# directory — keep the two in sync (canonical source: nff/scripts/install.sh).

set -eu

REPO="GLechevalier/nff"
VERSION="${1:-${NFF_VERSION:-latest}}"
INSTALL_DIR="${NFF_INSTALL_DIR:-$HOME/.local/bin}"

say()  { printf '%s\n' "$*"; }
fail() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

# ── Detect platform → release asset name ────────────────────────────────────
OS="$(uname -s)"
ARCH="$(uname -m)"
case "$OS" in
  Linux)
    case "$ARCH" in
      x86_64|amd64)   ASSET="nff-linux-x64" ;;
      aarch64|arm64)  ASSET="nff-linux-arm64" ;;
      *) fail "unsupported Linux architecture: $ARCH (prebuilt binaries: x64, arm64). Try: pip install nff" ;;
    esac ;;
  Darwin)
    case "$ARCH" in
      arm64)          ASSET="nff-macos-arm64" ;;
      x86_64)         ASSET="nff-macos-x64" ;;
      *) fail "unsupported macOS architecture: $ARCH. Try: pip install nff" ;;
    esac ;;
  *) fail "unsupported OS: $OS (this script supports Linux and macOS; on Windows use install.ps1)" ;;
esac

if [ "$VERSION" = "latest" ]; then
  BASE_URL="https://github.com/$REPO/releases/latest/download"
else
  VERSION="${VERSION#v}"
  BASE_URL="https://github.com/$REPO/releases/download/v$VERSION"
fi

# ── Download ────────────────────────────────────────────────────────────────
if command -v curl >/dev/null 2>&1; then
  fetch() { curl -fsSL -o "$2" "$1"; }
elif command -v wget >/dev/null 2>&1; then
  fetch() { wget -qO "$2" "$1"; }
else
  fail "neither curl nor wget found"
fi

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

say "Downloading $ASSET ($VERSION) ..."
fetch "$BASE_URL/$ASSET" "$TMP_DIR/nff" || fail "download failed: $BASE_URL/$ASSET"

# ── Verify checksum (best-effort: older releases have no SHA256SUMS) ────────
if fetch "$BASE_URL/SHA256SUMS" "$TMP_DIR/SHA256SUMS" 2>/dev/null; then
  EXPECTED="$(awk -v a="$ASSET" '$2 == a || $2 == "*"a {print $1}' "$TMP_DIR/SHA256SUMS")"
  if [ -n "$EXPECTED" ]; then
    if command -v sha256sum >/dev/null 2>&1; then
      ACTUAL="$(sha256sum "$TMP_DIR/nff" | awk '{print $1}')"
    else
      ACTUAL="$(shasum -a 256 "$TMP_DIR/nff" | awk '{print $1}')"
    fi
    [ "$ACTUAL" = "$EXPECTED" ] || fail "checksum mismatch for $ASSET (expected $EXPECTED, got $ACTUAL)"
    say "Checksum OK."
  else
    say "WARNING: $ASSET not listed in SHA256SUMS — skipping verification."
  fi
else
  say "WARNING: no SHA256SUMS published for this release — skipping verification."
fi

# ── Install ─────────────────────────────────────────────────────────────────
mkdir -p "$INSTALL_DIR"
mv "$TMP_DIR/nff" "$INSTALL_DIR/nff"
chmod +x "$INSTALL_DIR/nff"
say "Installed to $INSTALL_DIR/nff"

# ── Ensure INSTALL_DIR is on PATH (mirrors nff-rs installer.rs) ─────────────
case ":$PATH:" in
  *":$INSTALL_DIR:"*) ;;
  *)
    EXPORT_LINE="export PATH=\"\$PATH:$INSTALL_DIR\""
    ADDED=""
    for RC in "$HOME/.bashrc" "$HOME/.zshrc" "$HOME/.profile"; do
      if [ -f "$RC" ]; then
        if ! grep -q "$INSTALL_DIR" "$RC" 2>/dev/null; then
          printf '\n%s\n' "$EXPORT_LINE" >> "$RC"
          say "Added $INSTALL_DIR to PATH in $RC"
        fi
        ADDED=1
        break
      fi
    done
    if [ -z "$ADDED" ]; then
      printf '%s\n' "$EXPORT_LINE" >> "$HOME/.profile"
      say "Added $INSTALL_DIR to PATH in ~/.profile"
    fi
    say "Restart your terminal (or 'source' your shell rc) to pick up the new PATH."
    ;;
esac

# ── Verify ──────────────────────────────────────────────────────────────────
say ""
say "nff installed successfully:"
"$INSTALL_DIR/nff" --version
say ""
say "Next: run 'nff init' to detect your board and register the MCP server."
