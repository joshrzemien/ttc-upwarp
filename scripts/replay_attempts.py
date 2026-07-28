#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path

ROOT = Path("/home/zman/projects/labs/ttc_upwarp")
EMULATOR = ROOT / "Fuzzy64/mupen64plus-ui-console/projects/unix/mupen64plus"
CORE = ROOT / "Fuzzy64/mupen64plus-core/projects/unix/libmupen64plus.so.2.0.0"
RSP = ROOT / "Fuzzy64/mupen64plus-rsp-hle/projects/unix/mupen64plus-rsp-hle.so"
STATE = ROOT / "emulator/tas26.jp.m64p.st"
SCRIPT = ROOT / "scripts/replay_trace.lua"
ROM = ROOT / "emulator/sm64.jp.z64"


def replay(movie: Path) -> dict[str, object]:
    env = os.environ.copy()
    env.update({
        "M64_PATH": str(movie),
        "MAX_VIS": "400",
        "TRACE_EVERY": "1",
        "SDL_AUDIODRIVER": "dummy",
    })
    command = [
        str(EMULATOR), "--emumode", "0", "--corelib", str(CORE), "--rsp", str(RSP),
        "--savestate", str(STATE), "--fuzzer-lua", str(SCRIPT), str(ROM),
    ]
    run = subprocess.run(command, env=env, capture_output=True, text=True, timeout=30)
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
    args = parser.parse_args()
    movies = sorted(args.directory.glob("*.m64"))
    results = [replay(movie) for movie in movies]
    print(json.dumps({"attempts": results}, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
