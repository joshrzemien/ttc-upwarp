#!/usr/bin/env bash
set -euo pipefail

root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
source_dir="$root/Fuzzy64"
prefix="$root/emulator"
revision=2757d269fcffc6225ec51e72ae953f6b30be9cc4
prepare_only=0

usage() {
    cat <<'USAGE'
Usage: bash scripts/setup_fuzzy64.sh [--prepare-only]

Clone missing pinned Fuzzy64 sources, apply the Linux/Lua and Mario-Y trace
patches, build core/RSP/Glide64/console, and stage a project-local emulator.
Existing sources are never reset or cleaned; incompatible patches fail rather
than overwrite local edits. Existing runtime configuration and data are kept.
No ROMs, game data, or system packages are downloaded or installed.

--prepare-only  Prepare sources without compiling or staging the emulator.

Environment:
  JOBS       Parallel compiler jobs (default: available CPU count).
  HIRES      0 by default; 1 enables optional GlideHQ texture packs/filters and
             additionally requires Boost headers and Boost.Filesystem.
  LUA_PKG    Lua 5.1 pkg-config module (default: lua51).

Arch dependencies: base-devel git nasm pkgconf sdl2-compat zlib libpng lua51
                   freetype2 libglvnd glu
Optional HIRES=1: boost boost-libs
USAGE
}

for arg in "$@"; do
    case "$arg" in
        --prepare-only) prepare_only=1 ;;
        -h|--help) usage; exit 0 ;;
        *) printf 'Unknown argument: %s\n' "$arg" >&2; usage >&2; exit 2 ;;
    esac
done

fail() {
    printf 'Fuzzy64 setup: %s\n' "$*" >&2
    exit 1
}

command -v git >/dev/null || fail 'git is required.'
if [[ ! -e "$source_dir" ]]; then
    git clone --filter=blob:none --no-checkout https://github.com/danebou/Fuzzy64.git "$source_dir"
    git -C "$source_dir" checkout --detach "$revision"
elif [[ ! -d "$source_dir" ]]; then
    fail "$source_dir exists but is not a source directory."
fi

components=(mupen64plus-core mupen64plus-rsp-hle mupen64plus-video-glide64mk2 mupen64plus-ui-console)
for component in "${components[@]}"; do
    if [[ ! -f "$source_dir/$component/projects/unix/Makefile" ]]; then
        [[ -e "$source_dir/.git" ]] || fail "Missing $component sources in restored tree without Git metadata."
        # Only initialize missing submodules: updating populated ones can move
        # restored checkouts away from the user's local changes.
        git -C "$source_dir" submodule update --init -- "$component"
    fi
    [[ -f "$source_dir/$component/projects/unix/Makefile" ]] || fail "Missing $component/projects/unix/Makefile."
done

apply_once() {
    local tree=$1 patch=$2
    [[ -f "$patch" ]] || fail "Missing patch: $patch"
    # The discovery ceiling also allows source-only restored trees below the
    # parent project repository: paths must be relative to this source root.
    if GIT_CEILING_DIRECTORIES="$tree" git -C "$tree" apply --check "$patch" 2>/dev/null; then
        GIT_CEILING_DIRECTORIES="$tree" git -C "$tree" apply "$patch"
        printf 'Applied %s\n' "${patch##*/}"
    elif GIT_CEILING_DIRECTORIES="$tree" git -C "$tree" apply --reverse --check "$patch" 2>/dev/null; then
        printf 'Already applied: %s\n' "${patch##*/}"
    else
        fail "${patch##*/} conflicts with $tree. Sources were not reset; reconcile local changes before rerunning."
    fi
}

apply_once "$source_dir/mupen64plus-core" "$root/fuzzy64-mario-y-trace.patch"
apply_once "$source_dir" "$root/scripts/fuzzy64-linux-compat.patch"

if (( prepare_only )); then
    printf 'Fuzzy64 sources prepared; no build performed.\n'
    exit 0
fi

jobs=${JOBS:-$(nproc)}
hires=${HIRES:-0}
lua_pkg=${LUA_PKG:-lua51}
[[ "$jobs" =~ ^[1-9][0-9]*$ ]] || fail 'JOBS must be a positive integer.'
[[ "$hires" == 0 || "$hires" == 1 ]] || fail 'HIRES must be 0 or 1.'
for tool in make gcc g++ nasm pkg-config sdl2-config install; do
    command -v "$tool" >/dev/null || fail "Missing $tool; see --help for the Arch package list."
done
for package in zlib libpng sdl2 freetype2 gl glu "$lua_pkg"; do
    pkg-config --exists "$package" || fail "Missing pkg-config dependency: $package (see --help)."
done
lua_version=$(pkg-config --modversion "$lua_pkg")
[[ "$lua_version" == 5.1 || "$lua_version" == 5.1.* ]] || fail "LUA_PKG must select Lua 5.1, not $lua_version."

core="$source_dir/mupen64plus-core"
rsp="$source_dir/mupen64plus-rsp-hle"
video="$source_dir/mupen64plus-video-glide64mk2"
console="$source_dir/mupen64plus-ui-console"
api="$core/src/api"
shared="$prefix/share/mupen64plus"
plugins="$prefix/lib/mupen64plus"

# Force recompilation so restored objects cannot retain another machine's flags
# or compiler ABI. Do not clean or remove any restored source files.
make -B -C "$core/projects/unix" -j "$jobs" all "LUA_PKG=$lua_pkg" "SHAREDIR=$shared"
make -B -C "$rsp/projects/unix" -j "$jobs" all "APIDIR=$api"
make -B -C "$video/projects/unix" -j "$jobs" all "APIDIR=$api" "HIRES=$hires"
make -B -C "$console/projects/unix" -j "$jobs" all PIE=1 "APIDIR=$api" \
    "COREDIR=$prefix/lib/" "PLUGINDIR=$plugins" "SHAREDIR=$shared"

# Stage only after all four builds succeed. No system-wide make install/sudo.
install -d "$prefix/bin" "$prefix/lib" "$plugins" "$shared" "$prefix/config"
install -m 0755 "$console/projects/unix/mupen64plus" "$prefix/bin/mupen64plus"
install -m 0644 "$core/projects/unix/libmupen64plus.so.2.0.0" "$prefix/lib/libmupen64plus.so.2.0.0"
ln -sfn libmupen64plus.so.2.0.0 "$prefix/lib/libmupen64plus.so.2"
install -m 0644 "$rsp/projects/unix/mupen64plus-rsp-hle.so" "$plugins/mupen64plus-rsp-hle.so"
install -m 0644 "$video/projects/unix/mupen64plus-video-glide64mk2.so" "$plugins/mupen64plus-video-glide64mk2.so"
for data in "$core"/data/* "$video/data/Glide64mk2.ini"; do
    if [[ -f "$data" && ! -e "$shared/${data##*/}" ]]; then
        install -m 0644 "$data" "$shared/${data##*/}"
    fi
done

config="$prefix/config/mupen64plus.cfg"
if [[ ! -e "$config" ]]; then
    cat >"$config" <<'CONFIG'
[Core]
Version = 1.01
OnScreenDisplay = False
R4300Emulator = 0
ScreenshotPath = "/tmp"

[Video-General]
Fullscreen = False
ScreenWidth = 640
ScreenHeight = 480
VerticalSync = False

[UI-Console]
Version = 1.00
VideoPlugin = "dummy"
AudioPlugin = "dummy"
InputPlugin = "dummy"
RspPlugin = "mupen64plus-rsp-hle.so"
CONFIG
fi

printf '\nBuilt and staged Fuzzy64 in %s\n' "$prefix"
printf 'Console: %s/bin/mupen64plus\nConfig: %s\n' "$prefix" "$config"
printf 'Use --emumode 0 for full Mario-Y instruction tracing; TRACE_MARIO_Y=1 enables trace output.\n'
printf 'Headless: --gfx dummy --audio dummy --input dummy --nospeedlimit\n'
printf 'Rendering: --gfx %s/mupen64plus-video-glide64mk2.so\n' "$plugins"
