#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd -- "$(dirname -- "$(readlink -f -- "${BASH_SOURCE[0]}")")/.." && pwd)
case "$(basename -- "$0")" in
  wine) TOOL=/usr/lib/wine/wine64 ;;
  x86_64-w64-mingw32-gcc) TOOL=/usr/bin/x86_64-w64-mingw32-gcc ;;
  *) echo 'Invoke via .tools/bin/wine or .tools/bin/x86_64-w64-mingw32-gcc' >&2; exit 2 ;;
esac
options=(--rm --network none --user "$(id -u):$(id -g)"
  --mount "type=bind,source=$ROOT,target=$ROOT" --workdir "$ROOT")
if [[ "$TOOL" == /usr/lib/wine/wine64 ]]; then
  PREFIX=$(realpath -m -- "${WINEPREFIX:-$ROOT/.wine-wafel}")
  case "$PREFIX" in "$ROOT"/*) ;; *) echo 'Container Wine prefix must be inside the project; use native Wine for external prefixes.' >&2; exit 2 ;; esac
  mkdir -p "$ROOT/.tools/home" "$PREFIX"
  options+=(--env "HOME=$ROOT/.tools/home" --env "WINEPREFIX=$PREFIX"
    --env "WINEDEBUG=${WINEDEBUG:--all}" --env "WINEARCH=win64")
fi
# Only the checkout is mounted. Fail early rather than silently losing an
# external compiler output or asking Wine to open a host-only artifact.
for argument in "$@"; do
  case "$argument" in
    /*) path=$argument ;;
    [zZ]:*) path=${argument:2}; path=${path//\\//} ;;
    *) continue ;;
  esac
  path=$(realpath -m -- "$path")
  case "$path" in
    "$ROOT"|"$ROOT"/*) ;;
    *) printf 'Container tool cannot access %s; copy the artifact inside %s or use native tools.\n' "$argument" "$ROOT" >&2; exit 2 ;;
  esac
done
exec docker run "${options[@]}" \
  "${TTC_WINDOWS_IMAGE:-ttc-upwarp-windows:bookworm}" "$TOOL" "$@"
