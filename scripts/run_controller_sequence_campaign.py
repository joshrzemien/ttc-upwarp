#!/usr/bin/env python3
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

DEFAULT_ROOT = Path("/home/zman/projects/labs/ttc_upwarp")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        while chunk := file.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def parse_value(key: str, value: str) -> Any:
    if key == "id" or key.endswith(("action", "floor", "platform")):
        return value
    try:
        return int(value)
    except ValueError:
        try:
            return float(value)
        except ValueError:
            return value


def parse_fields(line: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for token in line.split(",")[1:]:
        if "=" in token:
            key, value = token.split("=", 1)
            result[key] = parse_value(key, value)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run restored-state controller sequence candidates in parallel"
    )
    parser.add_argument("sequences", type=Path)
    parser.add_argument("snapshot", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--workers", type=int, default=min(32, os.cpu_count() or 1))
    parser.add_argument("--timeout", type=float, default=600.0)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--movie", type=Path, default=Path("/tmp/tas26_jp.m64"))
    parser.add_argument("--rom", type=Path)
    parser.add_argument("--emulator", type=Path)
    parser.add_argument("--core", type=Path)
    parser.add_argument("--rsp", type=Path)
    parser.add_argument("--lua", type=Path)
    args = parser.parse_args()

    root = args.root.resolve()
    rom = args.rom or root / "emulator/sm64.jp.z64"
    emulator = args.emulator or root / "Fuzzy64/mupen64plus-ui-console/projects/unix/mupen64plus"
    core = args.core or root / "Fuzzy64/mupen64plus-core/projects/unix/libmupen64plus.so.2.0.0"
    rsp = args.rsp or root / "Fuzzy64/mupen64plus-rsp-hle/projects/unix/mupen64plus-rsp-hle.so"
    lua = args.lua or root / "scripts/controller_sequence_scan.lua"

    required = [args.sequences, args.snapshot, args.movie, rom, emulator, core, rsp, lua]
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        raise SystemExit("missing required paths: " + ", ".join(missing))

    rows = [
        line
        for line in args.sequences.read_text().splitlines()
        if line and not line.startswith("#")
    ]
    if not rows:
        raise SystemExit("sequence file is empty")
    ids = [line.split("|", 1)[0] for line in rows]
    if len(set(ids)) != len(ids):
        raise SystemExit("sequence IDs must be unique")

    worker_count = min(max(1, args.workers), len(rows))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    log_dir = args.output.with_suffix("")
    if log_dir.exists():
        shutil.rmtree(log_dir)
    log_dir.mkdir()

    with tempfile.TemporaryDirectory(prefix="controller-sequences-") as temp_name:
        temp_dir = Path(temp_name)
        shards: list[Path] = []
        for worker in range(worker_count):
            shard = temp_dir / f"shard-{worker:03}.txt"
            shard.write_text("\n".join(rows[worker::worker_count]) + "\n")
            shards.append(shard)

        def run_worker(worker: int) -> dict[str, Any]:
            shard = shards[worker]
            env = os.environ.copy()
            env.update(
                {
                    "M64_PATH": str(args.movie.resolve()),
                    "SEQUENCES_FILE": str(shard),
                    "SNAPSHOT_PATH": str(args.snapshot.resolve()),
                    "SDL_AUDIODRIVER": "dummy",
                }
            )
            command = [
                str(emulator),
                "--nospeedlimit",
                "--emumode",
                "0",
                "--corelib",
                str(core),
                "--rsp",
                str(rsp),
                "--savestate",
                str(args.snapshot.resolve()),
                "--fuzzer-lua",
                str(lua),
                str(rom),
            ]
            started = time.monotonic()
            try:
                run = subprocess.run(
                    command,
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=args.timeout,
                    check=False,
                )
                timed_out = False
                stdout = run.stdout
                stderr = run.stderr
                returncode = run.returncode
            except subprocess.TimeoutExpired as error:
                timed_out = True
                stdout = error.stdout or ""
                stderr = error.stderr or ""
                if isinstance(stdout, bytes):
                    stdout = stdout.decode(errors="replace")
                if isinstance(stderr, bytes):
                    stderr = stderr.decode(errors="replace")
                returncode = None
            elapsed = time.monotonic() - started
            log = log_dir / f"worker-{worker:03}.log"
            log.write_text(stdout + ("\nSTDERR:\n" + stderr if stderr else ""))
            parsed = [
                parse_fields(line)
                for line in stdout.splitlines()
                if line.startswith("SEQUENCE,")
            ]
            done = next(
                (
                    parse_fields(line)
                    for line in stdout.splitlines()
                    if line.startswith("SEQUENCE_DONE,")
                ),
                None,
            )
            return {
                "worker": worker,
                "returncode": returncode,
                "timed_out": timed_out,
                "elapsed_seconds": elapsed,
                "expected": len(rows[worker::worker_count]),
                "parsed": len(parsed),
                "done": done,
                "results": parsed,
                "log": log.name,
            }

        started = time.monotonic()
        with concurrent.futures.ThreadPoolExecutor(max_workers=worker_count) as executor:
            workers = list(executor.map(run_worker, range(worker_count)))
        elapsed = time.monotonic() - started

    results = [result for worker in workers for result in worker.pop("results")]
    result_ids = {str(result.get("id")) for result in results}
    missing_ids = sorted(set(ids) - result_ids)
    duplicate_results = len(results) - len(result_ids)
    complete = (
        not missing_ids
        and duplicate_results == 0
        and all(worker["returncode"] == 0 for worker in workers)
        and all(not worker["timed_out"] for worker in workers)
        and all(worker["done"] is not None for worker in workers)
    )
    ranked_nearest = sorted(results, key=lambda result: float(result["nearest_distance"]))
    ranked_height = sorted(results, key=lambda result: float(result["max_y"]), reverse=True)
    ranked_upward_step = sorted(
        results, key=lambda result: float(result["max_upward_step"]), reverse=True
    )
    ranked_contact = sorted(
        results,
        key=lambda result: (
            int(result["top_contact_updates"]),
            -float(result["nearest_distance"]),
        ),
        reverse=True,
    )
    payload = {
        "schema_version": 1,
        "complete": complete,
        "candidate_count": len(rows),
        "result_count": len(results),
        "missing_ids": missing_ids,
        "duplicate_results": duplicate_results,
        "worker_count": worker_count,
        "elapsed_seconds": elapsed,
        "throughput_candidates_per_second": len(results) / elapsed,
        "inputs": {
            "sequence_file": str(args.sequences.resolve()),
            "sequence_sha256": sha256(args.sequences),
            "snapshot": str(args.snapshot.resolve()),
            "snapshot_sha256": sha256(args.snapshot),
            "movie": str(args.movie.resolve()),
            "movie_sha256": sha256(args.movie),
            "rom": str(rom.resolve()),
            "rom_md5": hashlib.md5(rom.read_bytes()).hexdigest(),
            "scanner": str(lua.resolve()),
            "scanner_sha256": sha256(lua),
        },
        "workers": workers,
        "ranked_nearest": ranked_nearest[:100],
        "ranked_height": ranked_height[:100],
        "ranked_upward_step": ranked_upward_step[:100],
        "ranked_contact": ranked_contact[:100],
        "all_results": sorted(results, key=lambda result: str(result["id"])),
    }
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    print(
        f"complete={complete} candidates={len(rows)} results={len(results)} "
        f"workers={worker_count} elapsed={elapsed:.3f}s "
        f"rate={len(results) / elapsed:.3f}/s output={args.output}"
    )
    if not complete:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
