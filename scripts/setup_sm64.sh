#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
REVISION=9921382a68bb0c865e5e45eb594d9c64db59b1af
ROM=${SM64_JP_ROM:-$ROOT/emulator/sm64.jp.z64}
case "${1:-}" in
  --rom) [[ $# == 2 ]] || { echo 'Usage: setup_sm64.sh [--rom FILE]' >&2; exit 2; }; ROM=$2 ;;
  --help|-h) echo 'Usage: setup_sm64.sh [--rom FILE]; without a ROM builds host tools only.'; exit 0 ;;
  '') ;;
  *) echo 'Usage: setup_sm64.sh [--rom FILE]' >&2; exit 2 ;;
esac
if [[ ! -d "$ROOT/sm64" ]]; then
  git clone --no-checkout https://github.com/n64decomp/sm64.git "$ROOT/sm64"
  git -C "$ROOT/sm64" checkout --detach "$REVISION"
fi
[[ "$(git -C "$ROOT/sm64" rev-parse HEAD)" == "$REVISION" ]] || {
  echo "sm64 must be at $REVISION; refusing to alter an existing checkout." >&2; exit 1;
}
PATCH="$ROOT/scripts/sm64-host-tools.patch"
# Apply each file independently so an existing host-tools-only patch can be upgraded.
for FILE in Makefile tools/armips.cpp; do
  if git -C "$ROOT/sm64" apply --include="$FILE" --reverse --check "$PATCH" 2>/dev/null; then
    continue
  fi
  git -C "$ROOT/sm64" apply --include="$FILE" --check "$PATCH"
  git -C "$ROOT/sm64" apply --include="$FILE" "$PATCH"
done
make -C "$ROOT/sm64/tools" -j"${JOBS:-$(nproc)}"
if [[ ! -f "$ROM" ]]; then
  echo "Host tools ready. JP ROM required for the matching game build: $ROM" >&2
  exit 0
fi
ROM=$(realpath -- "$ROM")
printf '85d61f5525af708c9f1e84dce6dc10e9  %s\n' "$ROM" | md5sum --check
DEST="$ROOT/sm64/baserom.jp.z64"
if [[ -e "$DEST" || -L "$DEST" ]]; then
  cmp -- "$ROM" "$DEST"
else
  ln -s "$ROM" "$DEST"
fi
[[ -x "$ROOT/emulator/toolchain/mips64-elf-as" ]] || bash "$ROOT/scripts/setup_mips_toolchain.sh"
PATH="$ROOT/emulator/toolchain:$PATH" make -C "$ROOT/sm64" VERSION=jp CROSS="$ROOT/emulator/toolchain/mips64-elf-" -j"${JOBS:-$(nproc)}"
cmp -- "$ROM" "$ROOT/sm64/build/jp/sm64.jp.z64"
echo 'JP decompilation build is byte-identical to the supplied ROM.'
