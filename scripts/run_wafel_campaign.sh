#!/usr/bin/env bash
set -euo pipefail

root=${ROOT:-/home/zman/projects/labs/ttc_upwarp}
workers=${WORKERS:-16}
trials=${TRIALS:-25000000}
first_seed=${FIRST_SEED:-1000}
horizon=${HORIZON:-1}
reset_mode=${RESET_MODE:-0}
profile=${PROFILE:-0}
speed_override=${SPEED_MODE:--1}
run_name=${RUN_NAME:-fixed_speed_400m}
out="$root/results/wafel_fuzz/$run_name"
wine=${WINE:-/nix/store/m3vk5lr49wdgyya04jxnyr4d63wcwvki-wine64-11.0/bin/wine}
exe=${EXE:-$root/scripts/wafel_incident_fuzz.exe}
dll="Z:/home/zman/projects/labs/ttc_upwarp/wafel/libsm64/sm64_jp.dll"
prefix=${WINEPREFIX:-$root/.wine-wafel}

mkdir -p "$out"
printf 'CAMPAIGN_START run=%s workers=%d trials_per_worker=%d first_seed=%d horizon=%d reset_mode=%d profile=%d speed_override=%d\n' \
    "$run_name" "$workers" "$trials" "$first_seed" "$horizon" "$reset_mode" "$profile" "$speed_override"

pids=()
for ((worker = 0; worker < workers; worker++)); do
    seed=$((first_seed + worker))
    if ((speed_override >= 0)); then
        speed=$speed_override
    else
        speed=$((worker % 4))
    fi
    warmup=$((worker * 997))
    log="$out/seed_${seed}_speed_${speed}.log"
    env WINEDEBUG=-all WINEPREFIX="$prefix" \
        "$wine" "$exe" "$dll" "$seed" "$trials" "$warmup" "$speed" "$horizon" "$reset_mode" "$profile" \
        >"$log" 2>&1 &
    pids+=("$!")
done

failed=0
for pid in "${pids[@]}"; do
    if ! wait "$pid"; then
        failed=$((failed + 1))
    fi
done

printf 'CAMPAIGN_DONE run=%s failed=%d logs=%s\n' "$run_name" "$failed" "$out"
if ((failed != 0)); then
    exit 1
fi
