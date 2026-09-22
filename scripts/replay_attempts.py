#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def replay(movie: Path, args: argparse.Namespace) -> dict[str, object]:
    env = os.environ.copy()
    env.update({
        "M64_PATH": str(movie.resolve()),
        "MAX_VIS": "400",
        "TRACE_EVERY": "1",
        "SDL_AUDIODRIVER": "dummy",
    })
    command = [
        str(args.emulator), "--emumode", "0", "--corelib", str(args.core), "--rsp", str(args.rsp),
        "--gfx", "dummy", "--audio", "dummy", "--input", "dummy", "--nosaveoptions",
        "--datadir", str(args.data_dir), "--configdir", str(args.config_dir),
        "--savestate", str(args.savestate), "--fuzzer-lua", str(args.lua), str(args.rom),
    ]
    run = subprocess.run(command, cwd=args.root, env=env, capture_output=True, text=True, timeout=args.timeout)
    text = run.stdout + run.stderr
    rows: list[tuple[int, float]] = []
    for line in text.splitlines():
        fields = line.split(",")
        if len(fields) >= 7 and fields[0] == "TRACE":
            rows.append((int(fields[1]), float(fields[6])))

    steps = [(rows[index][1] - rows[index - 1][1], rows[index][0]) for index in range(1, len(rows))]
    max_step, max_step_vi = max(steps, default=(0.0, 0))
    return {
        "movie": movie.name,
        "samples": len(rows),
        "finished": "finished=true" in text,
        "exit_code": run.returncode,
        "max_upward_step": max_step,
        "max_upward_step_vi": max_step_vi,
        "min_y": min((y for _, y in rows), default=None),
        "max_y": max((y for _, y in rows), default=None),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--emulator", type=Path)
    parser.add_argument("--core", type=Path)
    parser.add_argument("--rsp", type=Path)
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
        "savestate": "emulator/tas26.jp.m64p.st",
        "lua": "scripts/replay_trace.lua",
        "rom": "emulator/sm64.jp.z64",
    }
    for name, relative_path in defaults.items():
        setattr(args, name, (getattr(args, name) or args.root / relative_path).expanduser().resolve())
    missing = [str(getattr(args, name)) for name in defaults if not getattr(args, name).is_file()]
    if missing:
        raise FileNotFoundError("missing replay dependency: " + ", ".join(missing))
    args.data_dir = (args.data_dir or args.root / "emulator/share/mupen64plus").expanduser().resolve()
    args.config_dir = (args.config_dir or args.root / "emulator/config").expanduser().resolve()
    movies = sorted(args.directory.glob("*.m64"))
    if not movies:
        raise FileNotFoundError(f"no .m64 files in {args.directory}")
    results = [replay(movie, args) for movie in movies]
    print(json.dumps({"attempts": results}, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
