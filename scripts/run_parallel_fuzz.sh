#!/usr/bin/env bash
set -euo pipefail

root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
out="${OUT_DIR:-$root/results/local/${OUT_NAME:-software_fuzz_$(date -u +%Y%m%dT%H%M%S%NZ)}}"
script="$root/scripts/${FUZZ_SCRIPT:-software_path_fuzz.lua}"
workers="${JOBS:-$(nproc)}"
first_seed="${FIRST_SEED:-1}"
last_seed="${LAST_SEED:-$workers}"
emulator="${EMULATOR:-$root/emulator/bin/mupen64plus}"
rom="${ROM:-$root/emulator/sm64.jp.z64}"
state="${SAVESTATE:-$root/emulator/tas26.jp.m64p.st}"
movie="${M64_PATH:-$root/TTC-Upwarp-Overlay/Tas Attempts/tas26.m64}"
for input in "$emulator" "$rom" "$state" "$movie" "$script"; do
    [[ -f "$input" ]] || { printf 'Missing experiment input: %s\n' "$input" >&2; exit 2; }
done
mkdir -p -- "$(dirname -- "$out")"
mkdir -- "$out"

run_seed() {
    local seed="$1"
    env \
        MAX_VI="${MAX_VI:-15000}" \
        FUZZ_SEED="$seed" \
        M64_PATH="$movie" \
        SDL_AUDIODRIVER=dummy \
        "$emulator" \
        --emumode 0 --nospeedlimit --gfx dummy --audio dummy --input dummy \
        --corelib "$root/emulator/lib/libmupen64plus.so.2.0.0" \
        --rsp "$root/emulator/lib/mupen64plus/mupen64plus-rsp-hle.so" \
        --configdir "$root/emulator/config" --datadir "$root/emulator/share/mupen64plus" \
        --savestate "$state" \
        --fuzzer-lua "$script" \
        "$rom" \
        >"$out/seed_${seed}.log" 2>&1
}

export -f run_seed
export root out script emulator rom state movie MAX_VI
seq "$first_seed" "$last_seed" | xargs -P "$workers" -n 1 bash -c 'run_seed "$1"' _
