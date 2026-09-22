#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

FIELD_RE = re.compile(r"([^=,]+)=([^,]+)")
BOOLEAN_FIELDS = {"saw_nan"}
HEX_FIELDS = {
    "source_word",
    "mutated_word",
    "first_action",
    "first_y_word",
    "landing_action",
    "landing_y_word",
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        while chunk := file.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def parse_value(name: str, value: str) -> Any:
    if name in BOOLEAN_FIELDS:
        return value == "true"
    if name in HEX_FIELDS:
        return value
    if any(marker in value.lower() for marker in (".", "e", "nan", "inf")):
        return float(value)
    return int(value)


def parse_scan(output: str) -> dict[str, Any]:
    lines = [line for line in output.splitlines() if line.startswith("BIT_SCAN,")]
    if len(lines) != 1:
        raise ValueError(f"expected one BIT_SCAN line, found {len(lines)}")
    return {name: parse_value(name, value) for name, value in FIELD_RE.findall(lines[0])}


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare all 32 single-bit mutations of Mario's Y word")
    parser.add_argument("output", type=Path)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--workers", type=int, default=min(16, os.cpu_count() or 1))
    parser.add_argument("--emulator", type=Path)
    parser.add_argument("--core", type=Path)
    parser.add_argument("--rsp", type=Path)
    parser.add_argument("--savestate", type=Path)
    parser.add_argument("--rom", type=Path)
    parser.add_argument("--movie", type=Path)
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
    movie = (args.movie or root / "TTC-Upwarp-Overlay/Tas Attempts/tas26.m64").expanduser().resolve()
    lua = (args.lua or root / "scripts/single_bit_y_scan.lua").expanduser().resolve()
    data_dir = (args.data_dir or root / "emulator/share/mupen64plus").expanduser().resolve()
    config_dir = (args.config_dir or root / "emulator/config").expanduser().resolve()

    dependencies = [emulator, core, rsp, savestate, rom, movie, lua]
    missing = [str(path) for path in dependencies if not path.is_file()]
    if missing:
        raise FileNotFoundError("missing scan dependency: " + ", ".join(missing))

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

    def run_bit(bit: int) -> dict[str, Any]:
        env = os.environ.copy()
        env.update(
            {
                "BIT_INDEX": str(bit),
                "M64_PATH": str(movie),
                "SDL_AUDIODRIVER": "dummy",
            }
        )
        run = subprocess.run(
            command,
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        combined = run.stdout + "\n" + run.stderr
        if run.returncode != 0:
            return {
                "bit": bit,
                "status": "error",
                "returncode": run.returncode,
                "output_tail": combined[-4000:],
            }
        try:
            result = parse_scan(combined)
        except ValueError as error:
            return {"bit": bit, "status": "error", "error": str(error), "output_tail": combined[-4000:]}
        landing_penalty = 1_000_000.0 if result["landing_updates"] < 0 else 0.0
        result["target_error"] = (
            abs(result["mutated_y"] - (-1051.75))
            + abs(result["landing_y"] - (-2487.0))
            + 50.0 * abs(result["landing_updates"] - 25)
            + landing_penalty
        )
        return {"status": "ok", **result}

    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = [executor.submit(run_bit, bit) for bit in range(32)]
        for future in as_completed(futures):
            results.append(future.result())
    results.sort(key=lambda result: result["bit"])
    successful = [result for result in results if result["status"] == "ok"]
    ranked = sorted(successful, key=lambda result: result["target_error"])

    payload = {
        "schema_version": 1,
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "source_y": -4207.0,
        "source_y_word": "C5837800",
        "comparison_target": {
            "post_y": -1051.75,
            "instantaneous_delta": 3155.25,
            "landing_y": -2487.0,
            "landing_updates_from_mutation": 25,
            "landing_updates_from_first_physics": 24,
            "target_error": "abs(post_y-target)+abs(landing_y-target)+50*abs(landing_updates-target), with 1e6 penalty for no landing",
        },
        "dependencies": {
            path.name: {"path": str(path), "sha256": sha256(path)} for path in dependencies
        },
        "successful_runs": len(successful),
        "failed_runs": 32 - len(successful),
        "ranked_bits": [result["bit"] for result in ranked],
        "results": results,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    print(f"wrote {args.output}: ok={len(successful)} failed={32 - len(successful)} best={payload['ranked_bits'][:5]}")


if __name__ == "__main__":
    main()
