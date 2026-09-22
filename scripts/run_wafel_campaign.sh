#!/usr/bin/env bash
set -euo pipefail

root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
workers=${WORKERS:-16}
trials=${TRIALS:-25000000}
first_seed=${FIRST_SEED:-1000}
horizon=${HORIZON:-1}
reset_mode=${RESET_MODE:-0}
profile=${PROFILE:-0}
speed_override=${SPEED_MODE:--1}
run_name=${RUN_NAME:-local_$(date -u +%Y%m%dT%H%M%S%NZ)}
out=${OUT_DIR:-$root/results/wafel_fuzz/$run_name}
wine=${WINE:-}
exe=${EXE:-$root/scripts/wafel_incident_fuzz.exe}
dll=${DLL:-$root/wafel/libsm64/sm64_jp.dll}
prefix=${WINEPREFIX:-$root/.wine-wafel}

if [[ -z $wine ]]; then
    if command -v wine >/dev/null 2>&1; then
        wine=$(command -v wine)
    elif command -v wine64 >/dev/null 2>&1; then
        wine=$(command -v wine64)
    elif [[ -x $root/.tools/bin/wine ]]; then
        wine=$root/.tools/bin/wine
    else
        printf 'Missing Wine64; run scripts/setup_windows_toolchain.sh or set WINE.\n' >&2
        exit 2
    fi
fi
exe=$(realpath -m -- "$exe")
dll=$(realpath -m -- "$dll")
prefix=$(realpath -m -- "$prefix")
wine_path=$(command -v "$wine") || {
    printf 'Wine64 executable not found: %s\n' "$wine" >&2
    exit 2
}
if [[ $(realpath -- "$wine_path") == "$root/scripts/windows_tool.sh" ]]; then
    for input in "$exe" "$dll" "$prefix"; do
        case "$input" in
            "$root"/*) ;;
            *)
                printf 'Container artifact/prefix must resolve inside %s: %s. Copy artifacts under the project or select native WINE.\n' "$root" "$input" >&2
                exit 2 ;;
        esac
    done
fi
for input in "$exe" "$dll"; do
    if [[ ! -f $input ]]; then
        printf 'Missing %s; run scripts/setup_wafel.sh --rom /path/to/your/JP-ROM.\n' "$input" >&2
        exit 2
    fi
done
dll="Z:$dll"
if [[ ! $workers =~ ^[1-9][0-9]*$ ]]; then
    printf 'WORKERS must be a positive integer.\n' >&2
    exit 2
fi

# Never overwrite a previous campaign, including checked-in historical logs.
mkdir -p -- "$(dirname -- "$out")"
mkdir -- "$out"
# Docker Wine invocations have separate server/PID namespaces. Reuse one prefix
# per worker rather than concurrently locking a shared prefix across containers.
mkdir -p -- "$prefix/workers"
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
    worker_prefix="$prefix/workers/worker_$worker"
    env WINEDEBUG="${WINEDEBUG:--all}" WINEPREFIX="$worker_prefix" WINEARCH=win64 \
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
