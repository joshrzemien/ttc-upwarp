#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
command -v docker >/dev/null || { echo 'Docker is required for the isolated Wine/MinGW toolchain.' >&2; exit 1; }
docker build --tag "${TTC_WINDOWS_IMAGE:-ttc-upwarp-windows:bookworm}" \
  --file "$ROOT/scripts/windows-toolchain.Dockerfile" "$ROOT/scripts"
mkdir -p "$ROOT/.tools/bin"
for tool in wine x86_64-w64-mingw32-gcc; do
  target="$ROOT/.tools/bin/$tool"
  if [[ -e "$target" || -L "$target" ]]; then
    [[ "$(readlink -f -- "$target")" == "$ROOT/scripts/windows_tool.sh" ]] || {
      echo "Refusing to replace existing tool: $target" >&2; exit 1;
    }
  else
    ln -s ../../scripts/windows_tool.sh "$target"
  fi
done
printf 'Wine and MinGW wrappers installed in %s/.tools/bin\n' "$ROOT"
