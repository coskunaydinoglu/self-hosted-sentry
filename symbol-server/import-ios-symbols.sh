#!/usr/bin/env bash
# Sort iOS system symbols from Xcode's DeviceSupport folders into the layout the
# internal symbol server expects, then (optionally) sync them to the server.
#
# Run on a Mac that has connected the devices/OS versions you ship to. Every
# iOS build you want symbolicated (UIKit, libsystem, libdispatch, ...) must
# have been pulled from a real device once by Xcode.
#
#   ./import-ios-symbols.sh [OUTPUT_DIR] [RSYNC_TARGET]
#   ./import-ios-symbols.sh ./sorted user@sentry-host:/opt/sentry/symbol-server/data/
#
# Requires symsorter: https://github.com/getsentry/symbolicator/releases
# (download symsorter-Darwin-universal) or `cargo install --git https://github.com/getsentry/symbolicator symsorter`.
set -euo pipefail

OUT="${1:-./sorted-symbols}"
TARGET="${2:-}"
SYMSORTER="${SYMSORTER:-symsorter}"
DEVICE_SUPPORT="${DEVICE_SUPPORT:-$HOME/Library/Developer/Xcode/iOS DeviceSupport}"

if ! command -v "$SYMSORTER" >/dev/null 2>&1; then
  echo "symsorter not found; set SYMSORTER=/path/to/symsorter" >&2
  exit 1
fi
if [[ ! -d "$DEVICE_SUPPORT" ]]; then
  echo "no DeviceSupport folder at $DEVICE_SUPPORT (connect a device to Xcode first)" >&2
  exit 1
fi

mkdir -p "$OUT"
count=0
for symbols in "$DEVICE_SUPPORT"/*/Symbols; do
  [[ -d "$symbols" ]] || continue
  version_dir="$(basename "$(dirname "$symbols")")" # e.g. "17.5.1 (21F90)" or "iPhone15,2 17.5.1 (21F90)"
  bundle="$(echo "$version_dir" | tr -c 'A-Za-z0-9._-' '_' | sed 's/_*$//')"
  echo "==> sorting $version_dir (bundle $bundle)"
  # -zz: zstd compression, keeps the volume small; Symbolicator decompresses on the fly.
  "$SYMSORTER" -zz --prefix "" --bundle-id "ios_$bundle" -o "$OUT" "$symbols"
  count=$((count + 1))
done

if [[ "$count" -eq 0 ]]; then
  echo "nothing sorted: no */Symbols folders under $DEVICE_SUPPORT" >&2
  exit 1
fi
echo "sorted $count OS version(s) into $OUT"

if [[ -n "$TARGET" ]]; then
  echo "==> syncing to $TARGET"
  rsync -a --info=progress2 "$OUT"/ "$TARGET"
fi
