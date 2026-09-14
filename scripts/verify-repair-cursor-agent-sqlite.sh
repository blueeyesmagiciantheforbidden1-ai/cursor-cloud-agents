#!/usr/bin/env bash
# Verify + repair Cursor Agent Windows better-sqlite3 ABI mismatch for BOTH
# win32-x64 and win32-arm64 official bundles, without touching sign-in state.
#
# Usage:
#   ./scripts/verify-repair-cursor-agent-sqlite.sh
#   AGENT_VERSION=2026.09.10-fd3934a ./scripts/verify-repair-cursor-agent-sqlite.sh
#
# This downloads clean official Windows packages, confirms they ship ABI 127
# better-sqlite3 against Node 24 (ABI 137), then applies the same replacement
# the PowerShell repair script performs and checks the resulting hashes.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
AGENT_VERSION="${AGENT_VERSION:-2026.09.10-fd3934a}"
BSQL_VERSION="${BSQL_VERSION:-12.11.1}"
TARGET_ABI="${TARGET_ABI:-137}"
WRONG_ABI="${WRONG_ABI:-127}"
WORK="${TMPDIR:-/tmp}/cursor-agent-sqlite-verify-$$"
ARTIFACT_DIR="${ARTIFACT_DIR:-/opt/cursor/artifacts}"

cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT

mkdir -p "$WORK" "$ARTIFACT_DIR"
cd "$WORK"

log() { printf '%s\n' "$*" >&2; }
fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }

sha() { sha256sum "$1" | awk '{print $1}'; }

download() {
  local url="$1" out="$2"
  log "GET $url"
  curl -fsSL --retry 3 --retry-delay 2 -o "$out" "$url"
}

extract_prebuild() {
  local ver="$1" abi="$2" plat="$3" dest="$4"
  local name="better-sqlite3-v${ver}-node-v${abi}-${plat}.tar.gz"
  local url="https://github.com/WiseLibs/better-sqlite3/releases/download/v${ver}/${name}"
  download "$url" "$name"
  mkdir -p "$dest"
  tar -xzf "$name" -C "$dest"
  local node
  node="$(find "$dest" -type f -path '*/build/Release/better_sqlite3.node' | head -n1)"
  [[ -n "$node" ]] || fail "missing better_sqlite3.node in $name"
  printf '%s' "$node"
}

repair_tree() {
  local tree="$1" plat="$2" replacement="$3"
  local target
  target="$(find "$tree" -type f -path '*/node_modules/better-sqlite3/build/Release/better_sqlite3.node' | head -n1)"
  [[ -n "$target" ]] || fail "no better_sqlite3.node under $tree"
  local before after
  before="$(sha "$target")"
  cp -f "$target" "${target}.abi${WRONG_ABI}.bak"
  cp -f "$replacement" "$target"
  after="$(sha "$target")"
  [[ "$before" != "$after" ]] || fail "$plat: repair did not change binary"
  [[ "$after" == "$(sha "$replacement")" ]] || fail "$plat: repaired hash mismatch"
  # Sign-in preservation invariant for the real Windows script: never delete agent root.
  # Here we only assert we left a backup beside the module.
  [[ -f "${target}.abi${WRONG_ABI}.bak" ]] || fail "$plat: missing backup"
  log "OK $plat repaired"
  log "  before=$before"
  log "  after =$after"
  printf '%s\n' "$before" >"$ARTIFACT_DIR/better-sqlite3-${plat}-before.sha256"
  printf '%s\n' "$after" >"$ARTIFACT_DIR/better-sqlite3-${plat}-after.sha256"
}

log "=== Cursor Agent Windows better-sqlite3 ABI verify/repair ==="
log "agent version : $AGENT_VERSION"
log "better-sqlite3: $BSQL_VERSION"
log "wrong ABI     : $WRONG_ABI (shipped)"
log "target ABI    : $TARGET_ABI (Node 24)"
log ""

declare -A EXPECT_WRONG_SHA=()
declare -A REPLACEMENT=()

for plat in win32-x64 win32-arm64; do
  wrong="$(extract_prebuild "$BSQL_VERSION" "$WRONG_ABI" "$plat" "prebuild-${plat}-${WRONG_ABI}")"
  right="$(extract_prebuild "$BSQL_VERSION" "$TARGET_ABI" "$plat" "prebuild-${plat}-${TARGET_ABI}")"
  EXPECT_WRONG_SHA["$plat"]="$(sha "$wrong")"
  REPLACEMENT["$plat"]="$right"
  log "prebuild $plat ABI $WRONG_ABI sha=$(sha "$wrong")"
  log "prebuild $plat ABI $TARGET_ABI sha=$(sha "$right")"
done

for arch in x64 arm64; do
  plat="win32-${arch}"
  zip="windows-${arch}.zip"
  url="https://downloads.cursor.com/lab/${AGENT_VERSION}/windows/${arch}/agent-cli-package.zip"
  download "$url" "$zip"
  mkdir -p "bundle-${arch}"
  unzip -q -o "$zip" -d "bundle-${arch}"

  # Confirm bundled Node is 24.x (PE resources often store the version as UTF-16LE).
  node_exe="bundle-${arch}/dist-package/node.exe"
  [[ -f "$node_exe" ]] || fail "missing node.exe in windows/${arch} bundle"
  if ! python3 - "$node_exe" <<'PY'
import sys
from pathlib import Path
data = Path(sys.argv[1]).read_bytes()
ok = b"v24." in data or "24.5".encode("utf-16le") in data or "24.".encode("utf-16le") in data
raise SystemExit(0 if ok else 1)
PY
  then
    fail "windows/${arch} node.exe does not look like Node 24"
  fi
  log "windows/${arch} node.exe looks like Node 24"
  target="$(find "bundle-${arch}" -type f -path '*/node_modules/better-sqlite3/build/Release/better_sqlite3.node' | head -n1)"
  [[ -n "$target" ]] || fail "missing better_sqlite3.node in windows/${arch}"
  bundled_sha="$(sha "$target")"
  expected_wrong="${EXPECT_WRONG_SHA[$plat]}"
  [[ "$bundled_sha" == "$expected_wrong" ]] || fail "windows/${arch} better_sqlite3 is not ABI ${WRONG_ABI} prebuild (got $bundled_sha, expected $expected_wrong)"

  pkg_ver="$(python3 -c "import json;print(json.load(open('bundle-${arch}/dist-package/node_modules/better-sqlite3/package.json'))['version'])")"
  [[ "$pkg_ver" == "$BSQL_VERSION" ]] || log "WARN: package.json version is $pkg_ver (script default pin is $BSQL_VERSION)"

  # node_sqlite3 is N-API (separate from this ABI bug); just record presence for both arches.
  [[ -f "bundle-${arch}/dist-package/node_sqlite3.node" ]] || fail "missing node_sqlite3.node in windows/${arch}"

  repair_tree "bundle-${arch}" "$plat" "${REPLACEMENT[$plat]}"
done

{
  echo "Cursor Agent Windows better-sqlite3 ABI repair verification"
  echo "agent_version=$AGENT_VERSION"
  echo "better_sqlite3=$BSQL_VERSION"
  echo "shipped_abi=$WRONG_ABI"
  echo "repaired_abi=$TARGET_ABI"
  echo "arches=win32-x64,win32-arm64"
  echo "sign_in_preserved=yes (only better_sqlite3.node replaced; agent root not wiped)"
  echo "powershell_repair=scripts/repair-cursor-agent-sqlite.ps1"
} | tee "$ARTIFACT_DIR/cursor-agent-sqlite-repair-summary.txt"

log ""
log "Both Windows architectures verified and repaired successfully."
log "On each Windows machine (keeping existing sign-in), run:"
log "  powershell -ExecutionPolicy Bypass -File scripts/repair-cursor-agent-sqlite.ps1"
log "Then restart: agent worker start"
