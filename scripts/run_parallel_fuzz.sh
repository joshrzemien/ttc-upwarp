#!/usr/bin/env bash
set -uo pipefail

root=/home/zman/projects/labs/ttc_upwarp
out="$root/results/${OUT_NAME:-software_fuzz}"
script="$root/scripts/${FUZZ_SCRIPT:-software_path_fuzz.lua}"
workers="${JOBS:-28}"
first_seed="${FIRST_SEED:-1}"
last_seed="${LAST_SEED:-$workers}"
mkdir -p "$out"

run_seed() {
    local seed="$1"
    env \
        MAX_VI="${MAX_VI:-15000}" \
        FUZZ_SEED="$seed" \
        M64_PATH="$root/TTC-Upwarp-Overlay/Tas Attempts/tas26.m64" \
        SDL_AUDIODRIVER=dummy \
        "$root/Fuzzy64/mupen64plus-ui-console/projects/unix/mupen64plus" \
        --emumode 0 \
        --corelib "$root/Fuzzy64/mupen64plus-core/projects/unix/libmupen64plus.so.2.0.0" \
        --rsp "$root/Fuzzy64/mupen64plus-rsp-hle/projects/unix/mupen64plus-rsp-hle.so" \
        --savestate "$root/emulator/tas26.jp.m64p.st" \
        --fuzzer-lua "$script" \
        "$root/emulator/sm64.jp.z64" \
        >"$out/seed_${seed}.log" 2>&1
}

export -f run_seed
export root out script MAX_VI
seq "$first_seed" "$last_seed" | xargs -P "$workers" -n 1 bash -c 'run_seed "$1"' _
