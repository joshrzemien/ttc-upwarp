#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXPECTED_ROM_MD5 = "85d61f5525af708c9f1e84dce6dc10e9"


def run_case(inject: bool, args: argparse.Namespace) -> list[dict[str, int | float | str]]:
    env = os.environ.copy()
    env.update({
        "EVENT_X": "800",
        "EVENT_Z": "1900",
        "INJECT_BIT_FLIP": "1" if inject else "0",
        "M64_PATH": str(args.movie),
        "STOP_VI": "160",
        "SDL_AUDIODRIVER": "dummy",
    })
    command = [
        str(args.emulator), "--emumode", "0", "--corelib", str(args.core), "--rsp", str(args.rsp),
        "--gfx", "dummy", "--audio", "dummy", "--input", "dummy", "--nosaveoptions",
        "--datadir", str(args.data_dir), "--configdir", str(args.config_dir),
        "--savestate", str(args.savestate), "--fuzzer-lua", str(args.lua), str(args.rom),
    ]
    run = subprocess.run(command, cwd=args.root, env=env, capture_output=True, text=True, timeout=args.timeout)
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
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--emulator", type=Path)
    parser.add_argument("--core", type=Path)
    parser.add_argument("--rsp", type=Path)
    parser.add_argument("--movie", type=Path)
    parser.add_argument("--savestate", type=Path)
    parser.add_argument("--lua", type=Path)
    parser.add_argument("--rom", type=Path)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--config-dir", type=Path)
    args = parser.parse_args()
    args.root = args.root.resolve()
    defaults = {
        "emulator": "emulator/bin/mupen64plus",
        "core": "emulator/lib/libmupen64plus.so.2.0.0",
        "rsp": "emulator/lib/mupen64plus/mupen64plus-rsp-hle.so",
        "movie": "TTC-Upwarp-Overlay/Tas Attempts/tas26.m64",
        "savestate": "emulator/tas26.jp.m64p.st",
        "lua": "scripts/reproduce_bitflip.lua",
        "rom": "emulator/sm64.jp.z64",
    }
    for name, relative_path in defaults.items():
        setattr(args, name, (getattr(args, name) or args.root / relative_path).expanduser().resolve())
    missing = [str(getattr(args, name)) for name in defaults if not getattr(args, name).is_file()]
    if missing:
        raise FileNotFoundError("missing reproduction dependency: " + ", ".join(missing))
    args.data_dir = (args.data_dir or args.root / "emulator/share/mupen64plus").expanduser().resolve()
    args.config_dir = (args.config_dir or args.root / "emulator/config").expanduser().resolve()

    rom_md5 = hashlib.md5(args.rom.read_bytes()).hexdigest()
    if rom_md5 != EXPECTED_ROM_MD5:
        raise RuntimeError(f"unexpected ROM MD5: {rom_md5}")

    injected = run_case(True, args)
    control = run_case(False, args)
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
