#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

FIELD_RE = re.compile(r"([^=,]+)=([^,]+)")
BOOLEAN_FIELDS = {"finished", "entered_ttc", "exited_ttc"}
HEX_FIELDS = {"nearest_action", "nearest_floor", "nearest_platform", "final_action"}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        while chunk := file.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def parse_value(name: str, value: str) -> Any:
    if name in BOOLEAN_FIELDS:
        if value not in ("true", "false"):
            raise ValueError(f"invalid boolean {name}={value}")
        return value == "true"
    if name in HEX_FIELDS:
        return value
    if any(marker in value.lower() for marker in (".", "e", "nan", "inf")):
        return float(value)
    return int(value)


def parse_outcome(output: str) -> dict[str, Any]:
    lines = [line for line in output.splitlines() if line.startswith("OUTCOME,")]
    if len(lines) != 1:
        raise ValueError(f"expected one OUTCOME line, found {len(lines)}")
    return {name: parse_value(name, value) for name, value in FIELD_RE.findall(lines[0])}


def main() -> None:
    parser = argparse.ArgumentParser(description="Run replay_outcome.lua over an M64 directory in parallel")
    parser.add_argument("movie_dir", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) // 2))
    parser.add_argument("--max-vis", type=int, default=1200)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--emulator", type=Path)
    parser.add_argument("--core", type=Path)
    parser.add_argument("--rsp", type=Path)
    parser.add_argument("--savestate", type=Path)
    parser.add_argument("--rom", type=Path)
    parser.add_argument("--lua", type=Path)
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--config-dir", type=Path)
    args = parser.parse_args()

    root = args.root.resolve()
    emulator = (args.emulator or root / "emulator/bin/mupen64plus").expanduser().resolve()
    core = (args.core or root / "emulator/lib/libmupen64plus.so.2.0.0").expanduser().resolve()
    rsp = (args.rsp or root / "emulator/lib/mupen64plus/mupen64plus-rsp-hle.so").expanduser().resolve()
    savestate = (args.savestate or root / "emulator/tas26.jp.m64p.st").expanduser().resolve()
    rom = (args.rom or root / "emulator/sm64.jp.z64").expanduser().resolve()
    lua = (args.lua or root / "scripts/replay_outcome.lua").expanduser().resolve()
    data_dir = (args.data_dir or root / "emulator/share/mupen64plus").expanduser().resolve()
    config_dir = (args.config_dir or root / "emulator/config").expanduser().resolve()

    required = [emulator, core, rsp, savestate, rom, lua]
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise FileNotFoundError("missing campaign dependency: " + ", ".join(missing))

    manifest_path = args.movie_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.is_file() else None
    metadata_by_movie = {
        entry["movie"]: entry for entry in manifest.get("entries", [])
    } if manifest else {}
    movies = sorted(args.movie_dir.glob("*.m64"))
    if not movies:
        raise FileNotFoundError(f"no .m64 files in {args.movie_dir}")

    command = [
        str(emulator),
        "--gfx", "dummy", "--audio", "dummy", "--input", "dummy", "--nosaveoptions",
        "--datadir", str(data_dir), "--configdir", str(config_dir),
        "--nospeedlimit",
        "--emumode",
        "0",
        "--corelib",
        str(core),
        "--rsp",
        str(rsp),
        "--savestate",
        str(savestate),
        "--fuzzer-lua",
        str(lua),
        str(rom),
    ]

    def run_movie(movie: Path) -> dict[str, Any]:
        env = os.environ.copy()
        env.update(
            {
                "M64_PATH": str(movie.resolve()),
                "MAX_VIS": str(args.max_vis),
                "SDL_AUDIODRIVER": "dummy",
            }
        )
        started = time.monotonic()
        try:
            run = subprocess.run(
                command,
                cwd=root,
                env=env,
                capture_output=True,
                text=True,
                timeout=args.timeout,
            )
            elapsed = time.monotonic() - started
            combined = run.stdout + "\n" + run.stderr
            if run.returncode != 0:
                return {
                    "movie": movie.name,
                    "status": "error",
                    "returncode": run.returncode,
                    "seconds": elapsed,
                    "output_tail": combined[-4000:],
                }
            outcome = parse_outcome(combined)
            return {
                "movie": movie.name,
                "status": "ok",
                "seconds": elapsed,
                **metadata_by_movie.get(movie.name, {}),
                **outcome,
            }
        except (subprocess.TimeoutExpired, ValueError) as error:
            return {
                "movie": movie.name,
                "status": "error",
                "seconds": time.monotonic() - started,
                "error": str(error),
            }

    started = time.monotonic()
    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {executor.submit(run_movie, movie): movie for movie in movies}
        for completed, future in enumerate(as_completed(futures), start=1):
            results.append(future.result())
            if completed % 100 == 0 or completed == len(futures):
                print(f"completed {completed}/{len(futures)}", flush=True)
    elapsed = time.monotonic() - started
    results.sort(key=lambda result: result["movie"])

    successful = [result for result in results if result["status"] == "ok"]
    best = sorted(successful, key=lambda result: result["nearest_distance"])[:20]
    survivors = [
        result
        for result in successful
        if result["entered_ttc"] and not result["exited_ttc"]
    ]
    summary = {
        "schema_version": 1,
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "elapsed_seconds": elapsed,
        "workers": args.workers,
        "max_vis": args.max_vis,
        "movies": len(movies),
        "successful_runs": len(successful),
        "failed_runs": len(results) - len(successful),
        "surviving_runs": len(survivors),
        "dependencies": {
            "emulator": {"path": str(emulator), "sha256": sha256(emulator)},
            "core": {"path": str(core), "sha256": sha256(core)},
            "rsp": {"path": str(rsp), "sha256": sha256(rsp)},
            "savestate": {"path": str(savestate), "sha256": sha256(savestate)},
            "rom": {"path": str(rom), "sha256": sha256(rom)},
            "lua": {"path": str(lua), "sha256": sha256(lua)},
            "manifest": {"path": str(manifest_path), "sha256": sha256(manifest_path)} if manifest else None,
        },
        "best_nearest_spinner": best,
        "results": results,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    print(
        f"wrote {args.output}: ok={len(successful)} failed={len(results) - len(successful)} "
        f"survivors={len(survivors)} elapsed={elapsed:.3f}s"
    )


if __name__ == "__main__":
    main()
