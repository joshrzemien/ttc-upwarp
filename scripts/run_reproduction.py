#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
from pathlib import Path

ROOT = Path("/home/zman/projects/labs/ttc_upwarp")
EMULATOR = ROOT / "Fuzzy64/mupen64plus-ui-console/projects/unix/mupen64plus"
CORE = ROOT / "Fuzzy64/mupen64plus-core/projects/unix/libmupen64plus.so.2.0.0"
RSP = ROOT / "Fuzzy64/mupen64plus-rsp-hle/projects/unix/mupen64plus-rsp-hle.so"
MOVIE = ROOT / "TTC-Upwarp-Overlay/Tas Attempts/tas26.m64"
STATE = ROOT / "emulator/tas26.jp.m64p.st"
SCRIPT = ROOT / "scripts/reproduce_bitflip.lua"
ROM = ROOT / "emulator/sm64.jp.z64"
EXPECTED_ROM_MD5 = "85d61f5525af708c9f1e84dce6dc10e9"


def run_case(inject: bool) -> list[dict[str, int | float | str]]:
    env = os.environ.copy()
    env.update({
        "EVENT_X": "800",
        "EVENT_Z": "1900",
        "INJECT_BIT_FLIP": "1" if inject else "0",
        "M64_PATH": str(MOVIE),
        "STOP_VI": "160",
        "SDL_AUDIODRIVER": "dummy",
    })
    command = [
        str(EMULATOR), "--emumode", "0", "--corelib", str(CORE), "--rsp", str(RSP),
        "--savestate", str(STATE), "--fuzzer-lua", str(SCRIPT), str(ROM),
    ]
    run = subprocess.run(command, env=env, capture_output=True, text=True, timeout=30)
    if run.returncode != 0:
        raise RuntimeError(f"emulator failed ({run.returncode}):\n{run.stdout}\n{run.stderr}")

    rows: list[dict[str, int | float | str]] = []
    for line in (run.stdout + run.stderr).splitlines():
        fields = line.split(",")
        if len(fields) != 11 or fields[0] != "BITFLIP":
            continue
        rows.append({
            "vi": int(fields[1]),
            "event": fields[2],
            "action": fields[3],
            "y_word": fields[4],
            "x": float(fields[5]),
            "y": float(fields[6]),
            "z": float(fields[7]),
            "vy": float(fields[8]),
            "floor": float(fields[9]),
            "ceil": float(fields[10]),
        })
    if not rows:
        raise RuntimeError("emulator produced no BITFLIP trace rows")
    return rows


def at_vi(rows: list[dict[str, int | float | str]], vi: int) -> dict[str, int | float | str]:
    return next(row for row in rows if row["vi"] == vi)


def first_landing(rows: list[dict[str, int | float | str]]) -> dict[str, int | float | str]:
    return next(row for row in rows if row["action"] == "04000471")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    rom_md5 = hashlib.md5(ROM.read_bytes()).hexdigest()
    if rom_md5 != EXPECTED_ROM_MD5:
        raise RuntimeError(f"unexpected ROM MD5: {rom_md5}")

    injected = run_case(True)
    control = run_case(False)
    source = at_vi(injected, 100)
    flipped = at_vi(injected, 101)
    injected_after_step = at_vi(injected, 102)
    control_after_step = at_vi(control, 102)
    injected_landing = first_landing(injected)
    control_landing = first_landing(control)

    assert source["event"] == "place_C5837800"
    assert source["y_word"] == "C5837800" and source["y"] == -4207.0
    assert flipped["event"] == "clear_bit_24"
    assert flipped["y_word"] == "C4837800" and flipped["y"] == -1051.75
    assert injected_after_step["floor"] == -2487.0
    assert control_after_step["floor"] == -5211.0
    assert injected_landing["vi"] == 150 and injected_landing["y"] == -2487.0
    assert control_landing["vi"] == 138 and control_landing["y"] == -5211.0

    result = {
        "status": "pass",
        "rom_md5": rom_md5,
        "source": source,
        "flipped": flipped,
        "injected_first_physics_step": injected_after_step,
        "injected_landing": injected_landing,
        "control_first_physics_step": control_after_step,
        "control_landing": control_landing,
        "instantaneous_y_delta": float(flipped["y"]) - float(source["y"]),
        "vis_from_flip_to_upper_landing": int(injected_landing["vi"]) - int(flipped["vi"]),
    }
    encoded = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded)
    print(encoded, end="")


if __name__ == "__main__":
    main()
