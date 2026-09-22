#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import shutil
import socket
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "scripts/floor_null_probe.c"
HEADER = ROOT / "scripts/wafel_jp_dll.h"
EXPECTED_DLL_SHA256 = "a3dc4984628bfc2bcdc92eb2c3af47beae1472fd54c07a24c286046d7962f67b"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def resolve_tool(configured: str | None, name: str) -> str:
    candidates = [configured] if configured else [name, str(ROOT / ".tools/bin" / name)]
    if not configured and name == "wine":
        candidates.insert(1, "wine64")
    for candidate in candidates:
        resolved = shutil.which(candidate)
        if resolved:
            return os.path.abspath(resolved)
    raise RuntimeError(
        f"missing executable {configured or name}; set its command-line option/environment variable "
        "or run scripts/setup_windows_toolchain.sh"
    )


def run_logged(
    argv: list[str], log_path: Path, *, env: dict[str, str] | None = None
) -> tuple[str, str, int, str]:
    command = shlex.join(argv)
    with log_path.open("x", encoding="utf-8") as output:
        result = subprocess.run(argv, cwd=ROOT, env=env, stdout=output, stderr=subprocess.STDOUT)
    text = log_path.read_text(encoding="utf-8", errors="replace")
    if result.returncode != 0:
        raise RuntimeError(f"command failed ({result.returncode}): {command}\nlog: {log_path}\n{text}")
    return text, str(log_path), result.returncode, command


def parse_kv_line(line: str) -> tuple[str, dict[str, str]]:
    parts = line.strip().split()
    event = parts[0]
    fields: dict[str, str] = {}
    for token in parts[1:]:
        if "=" in token:
            key, value = token.split("=", 1)
            fields[key] = value
    return event, fields


def as_float(fields: dict[str, str], key: str) -> float:
    return float(fields[key])


def as_int(fields: dict[str, str], key: str) -> int:
    return int(fields[key], 0)


def as_decimal_int(fields: dict[str, str], key: str) -> int:
    return int(fields[key])


def parse_log(text: str) -> dict[str, Any]:
    events: dict[str, list[dict[str, str]]] = {}
    for line in text.splitlines():
        if not line or line.startswith("wine:") or line.startswith("01"):
            continue
        if "=" not in line:
            continue
        event, fields = parse_kv_line(line)
        events.setdefault(event, []).append(fields)
    return {"events": events, "text": text}


def run_case(
    wine: str,
    exe: Path,
    dll: Path,
    wineprefix: Path,
    log_dir: Path,
    mode: str,
    frames: int,
    speed: int,
    seed: int,
    log_name: str,
) -> tuple[str, str, int, str]:
    env = dict(os.environ, WINEDEBUG=os.environ.get("WINEDEBUG", "-all"),
               WINEPREFIX=str(wineprefix), WINEARCH="win64")
    argv = [wine, str(exe), f"Z:{dll.as_posix()}", mode, str(frames), str(speed), str(seed)]
    text, path, returncode, command = run_logged(argv, log_dir / log_name, env=env)
    environment = shlex.join(["env", f"WINEDEBUG={env['WINEDEBUG']}",
                             f"WINEPREFIX={wineprefix}", "WINEARCH=win64"])
    return text, path, returncode, f"{environment} {command}"


def parse_positive(text: str) -> dict[str, Any]:
    rows: dict[str, dict[str, str]] = {}
    for line in text.splitlines():
        if line.startswith("POSITIVE_"):
            event, fields = parse_kv_line(line)
            rows[event] = fields
    if set(rows) != {"POSITIVE_GEOMETRY", "POSITIVE_UPDATE"}:
        raise RuntimeError(f"positive output missing rows: {sorted(rows)}")
    geometry = rows["POSITIVE_GEOMETRY"]
    update = rows["POSITIVE_UPDATE"]
    return {
        "geometry": {
            "first_floor_null": geometry["first_floor_null"] == "1",
            "first_floor_height": as_float(geometry, "first_height"),
            "before_physical_xyz": [as_float(geometry, f"before_physical_{axis}") for axis in "xyz"],
            "before_gfx_xyz": [as_float(geometry, f"before_gfx_{axis}") for axis in "xyz"],
            "copied_physical_xyz": [as_float(geometry, f"copied_physical_{axis}") for axis in "xyz"],
            "after_gfx_xyz": [as_float(geometry, f"after_gfx_{axis}") for axis in "xyz"],
            "second_floor_null": geometry["second_floor_null"] == "1",
            "second_floor_height": as_float(geometry, "second_floor_height"),
            "action": geometry["action"],
            "platform_null": geometry["platform_null"] == "1",
            "branch_hits": as_decimal_int(geometry, "branch_hits"),
        },
        "update": {
            "first_floor_null": update["first_floor_null"] == "1",
            "first_floor_height": as_float(update, "first_height"),
            "before_physical_xyz": [as_float(update, f"before_physical_{axis}") for axis in "xyz"],
            "before_gfx_xyz": [as_float(update, f"before_gfx_{axis}") for axis in "xyz"],
            "after_physical_xyz": [as_float(update, f"after_physical_{axis}") for axis in "xyz"],
            "after_gfx_xyz": [as_float(update, f"after_gfx_{axis}") for axis in "xyz"],
            "one_update_y_delta": as_float(update, "one_update_y_delta"),
            "second_floor_null": update["second_floor_null"] == "1",
            "second_floor_height": as_float(update, "second_floor_height"),
            "action": update["action"],
            "platform_null": update["platform_null"] == "1",
            "branch_hits": as_decimal_int(update, "branch_hits"),
        },
    }


def parse_sweep(text: str) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    done: dict[str, str] | None = None
    for line in text.splitlines():
        if line.startswith("SWEEP_ROW"):
            _, fields = parse_kv_line(line)
            rows.append(
                {
                    "physical_y": as_float(fields, "physical_y"),
                    "gfx_y": as_float(fields, "gfx_y"),
                    "first_floor_null": fields["first_floor_null"] == "1",
                    "first_floor_height": as_float(fields, "first_height"),
                    "copied_xyz": [
                        as_float(fields, "copied_x"),
                        as_float(fields, "copied_y"),
                        as_float(fields, "copied_z"),
                    ],
                    "second_floor_null": fields["second_floor_null"] == "1",
                    "second_floor_height": as_float(fields, "second_floor_height"),
                    "propagation_delta_y": as_float(fields, "propagation_delta"),
                    "branch_hits": as_decimal_int(fields, "branch_hits"),
                }
            )
        elif line.startswith("SWEEP_DONE"):
            _, done = parse_kv_line(line)
    if done is None or len(rows) != as_decimal_int(done, "rows"):
        raise RuntimeError("synthetic sweep output is incomplete")
    return {
        "rows": rows,
        "row_count": len(rows),
        "first_null_count": as_decimal_int(done, "first_null"),
        "second_null_count": as_decimal_int(done, "second_null"),
        "min_propagation_delta_y": as_float(done, "min_delta"),
        "max_propagation_delta_y": as_float(done, "max_delta"),
        "incident_scale_delta_y": as_float(done, "incident_delta"),
        "branch_hits": as_decimal_int(done, "branch_hits"),
    }


def parse_natural(text: str) -> dict[str, Any]:
    start: dict[str, str] | None = None
    done: dict[str, str] | None = None
    frames: list[dict[str, Any]] = []
    for line in text.splitlines():
        if line.startswith("NATURAL_START"):
            _, start = parse_kv_line(line)
        elif line.startswith("NATURAL_FRAME"):
            _, fields = parse_kv_line(line)
            frames.append(
                {
                    "frame": as_decimal_int(fields, "frame"),
                    "timer": as_decimal_int(fields, "timer"),
                    "action": fields["action"],
                    "xyz": [as_float(fields, axis) for axis in "xyz"],
                    "floor_height": as_float(fields, "floor_height"),
                    "floor_null": fields["floor_null"] == "1",
                }
            )
        elif line.startswith("NATURAL_DONE"):
            _, done = parse_kv_line(line)
    if start is None or done is None:
        raise RuntimeError("natural output is incomplete")
    requested = as_decimal_int(done, "requested_frames")
    completed = as_decimal_int(done, "completed_updates")
    return {
        "requested_frames": requested,
        "completed_updates": completed,
        "branch_hits": as_decimal_int(done, "branch_hits"),
        "speed": as_int(start, "speed"),
        "seed": as_decimal_int(start, "seed"),
        "controller_only": start["controller_only"] == "1",
        "representative_frames": frames,
    }


def source_commit(directory: Path) -> str | None:
    result = subprocess.run(
        ["git", "-C", str(directory), "rev-parse", "HEAD"],
        capture_output=True, text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the exact JP Wafel floor-null probe locally.")
    parser.add_argument("--output", type=Path, help="new result JSON (default: timestamped results/local/)")
    parser.add_argument("--log-dir", type=Path, help="new directory for raw logs")
    parser.add_argument("--cc", default=os.environ.get("CC"), help="Win64 MinGW executable")
    parser.add_argument("--wine", default=os.environ.get("WINE"), help="Wine64 executable")
    parser.add_argument("--dll", type=Path, default=Path(os.environ.get("DLL", ROOT / "wafel/libsm64/sm64_jp.dll")))
    parser.add_argument("--exe", type=Path, default=Path(os.environ.get("EXE", ROOT / "scripts/floor_null_probe.exe")))
    parser.add_argument("--wineprefix", type=Path, default=Path(os.environ.get("WINEPREFIX", ROOT / ".wine-wafel")))
    parser.add_argument("--natural-frames", type=int, default=4096)
    parser.add_argument("--speed", type=int, default=3)
    parser.add_argument("--seed", type=int, default=539362)
    args = parser.parse_args()

    if args.natural_frames < 1 or args.speed not in range(4) or not 0 <= args.seed < 2**64:
        parser.error("--natural-frames must be positive, --speed 0..3, and --seed an unsigned 64-bit integer")
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    args.output = (args.output or ROOT / f"results/local/floor_null_fallback_probe_{run_id}.json").resolve()
    log_dir = (args.log_dir or args.output.with_suffix(".logs")).resolve()
    if args.output.exists() or log_dir.exists():
        parser.error("output and log directory must be new; historical results are never overwritten")
    exe = args.exe.expanduser().resolve()
    dll = args.dll.expanduser().resolve()
    wineprefix = args.wineprefix.expanduser().resolve()
    cc = resolve_tool(args.cc, "x86_64-w64-mingw32-gcc")
    wine = resolve_tool(args.wine, "wine")
    wrapper = ROOT / "scripts/windows_tool.sh"
    container_cc = Path(cc).resolve() == wrapper
    container_wine = Path(wine).resolve() == wrapper
    if container_cc or container_wine:
        artifacts = {"DLL": dll, "EXE": exe}
        if container_wine:
            artifacts["WINEPREFIX"] = wineprefix
        for label, path in artifacts.items():
            if not path.is_relative_to(ROOT):
                parser.error(
                    f"container {label} must resolve inside {ROOT}: {path}; "
                    "copy artifacts under the project or select native --cc/--wine tools"
                )
    if not dll.is_file():
        parser.error(f"missing {dll}; run scripts/setup_wafel.sh --rom /path/to/your/JP-ROM")
    dll_hash = sha256_file(dll)
    if dll_hash != EXPECTED_DLL_SHA256:
        parser.error(f"incompatible DLL SHA256 {dll_hash}; required exact historical image {EXPECTED_DLL_SHA256}")
    source_hash = sha256_file(SOURCE)
    header_hash = sha256_file(HEADER)
    log_dir.mkdir(parents=True)
    exe.parent.mkdir(parents=True, exist_ok=True)
    compile_args = [cc, "-O2", "-std=c11", "-Wall", "-Wextra", "-static-libgcc",
                    "-o", str(exe), str(SOURCE), "-lbcrypt", "-lm"]
    compile_text, compile_log, compile_rc, compile_command = run_logged(compile_args, log_dir / "compile.log")
    commands = [compile_command]
    logs: list[dict[str, Any]] = []

    def record_log(path: str, text: str, command: str, returncode: int) -> None:
        logs.append(
            {
                "path": path,
                "sha256": sha256_bytes(text.encode("utf-8")),
                "bytes_utf8": len(text.encode("utf-8")),
                "command": command,
                "returncode": returncode,
            }
        )

    record_log(compile_log, compile_text, compile_command, compile_rc)

    positive_text, positive_log, positive_rc, positive_command = run_case(
        wine,
        exe,
        dll,
        wineprefix,
        log_dir,
        "positive",
        0,
        args.speed,
        args.seed,
        "positive.log",
    )
    commands.append(positive_command)
    record_log(positive_log, positive_text, positive_command, positive_rc)

    sweep_text, sweep_log, sweep_rc, sweep_command = run_case(
        wine,
        exe,
        dll,
        wineprefix,
        log_dir,
        "sweep",
        0,
        args.speed,
        args.seed,
        "sweep.log",
    )
    commands.append(sweep_command)
    record_log(sweep_log, sweep_text, sweep_command, sweep_rc)

    natural_text, natural_log, natural_rc, natural_command = run_case(
        wine,
        exe,
        dll,
        wineprefix,
        log_dir,
        "natural",
        args.natural_frames,
        args.speed,
        args.seed,
        "natural.log",
    )
    commands.append(natural_command)
    record_log(natural_log, natural_text, natural_command, natural_rc)

    positive = parse_positive(positive_text)
    sweep = parse_sweep(sweep_text)
    natural = parse_natural(natural_text)
    geometry = positive["geometry"]
    update = positive["update"]

    def equal_xyz(left: list[float], right: list[float]) -> bool:
        return all(abs(a - b) <= 1.0e-5 for a, b in zip(left, right))

    relation_rows = [
        row
        for row in sweep["rows"]
        if row["first_floor_null"]
        and not row["second_floor_null"]
        and abs(row["copied_xyz"][0] - 800.0) <= 1.0e-5
        and abs(row["copied_xyz"][2] - 1900.0) <= 1.0e-5
        and abs(row["copied_xyz"][1] - row["gfx_y"]) <= 1.0e-5
        and abs(row["propagation_delta_y"] - (row["gfx_y"] - row["physical_y"])) <= 1.0e-5
    ]
    positive_proven = (
        geometry["first_floor_null"]
        and not geometry["second_floor_null"]
        and geometry["branch_hits"] == 1
        and equal_xyz(geometry["copied_physical_xyz"], geometry["before_gfx_xyz"])
        and update["first_floor_null"]
        and not update["second_floor_null"]
        and update["branch_hits"] == 1
        and update["one_update_y_delta"] >= 500.0
    )

    source_artifacts = {
        str(path.relative_to(ROOT)): sha256_file(path) if path.is_file() else None
        for path in [ROOT / "sm64/src/game/mario.c", ROOT / "sm64/build/jp/sm64.jp.map",
                     ROOT / "sm64/build/jp/sm64.jp.elf"]
    }
    orchestrator_hash = sha256_file(Path(__file__))
    result: dict[str, Any] = {
        "status": "pass" if positive_proven and natural["completed_updates"] == natural["requested_frames"] else "fail",
        "run_id": run_id,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "execution": {"mode": "local", "host": socket.gethostname(), "root": str(ROOT),
                      "wineprefix": str(wineprefix)},
        "inputs": {
            "probe_source": str(SOURCE.relative_to(ROOT)),
            "probe_source_sha256": source_hash,
            "dll_guard_header": str(HEADER.relative_to(ROOT)),
            "dll_guard_header_sha256": header_hash,
            "orchestrator_sha256": orchestrator_hash,
            "probe_executable": str(exe),
            "probe_executable_sha256": sha256_file(exe),
            "libsm64_dll": str(dll),
            "libsm64_dll_sha256": dll_hash,
            "compiler": cc,
            "compiler_sha256": sha256_file(Path(cc)),
            "compiler_version": subprocess.check_output([cc, "--version"], cwd=ROOT, text=True).strip(),
            "wine": wine,
            "wine_sha256": sha256_file(Path(wine)),
            "wine_version": subprocess.check_output([wine, "--version"], cwd=ROOT, text=True).strip(),
            "sm64_source_commit": source_commit(ROOT / "sm64"),
            "source_artifacts_sha256": source_artifacts,
            "wafel_source_commit": source_commit(ROOT / "wafel"),
        },
        "commands": commands,
        "completion": {
            "compile_returncode": compile_rc,
            "positive_returncode": positive_rc,
            "sweep_returncode": sweep_rc,
            "natural_returncode": natural_rc,
            "natural_attempted_updates": natural["requested_frames"],
            "natural_completed_updates": natural["completed_updates"],
        },
        "source_behavior": {
            "source_file": "sm64/src/game/mario.c",
            "source_lines": "1318-1330",
            "reference_commit": "9921382a68bb0c865e5e45eb594d9c64db59b1af",
            "preconditions": [
                "f32_find_wall_collision has run on MarioState.pos",
                "first find_floor(pos.x,pos.y,pos.z,&m->floor) returns a NULL surface",
                "m->marioObj and m->marioObj->header.gfx.pos are valid",
            ],
            "fallback": "vec3f_copy(m->pos, m->marioObj->header.gfx.pos)",
            "second_query": "find_floor(m->pos.x,m->pos.y,m->pos.z,&m->floor)",
            "postcondition": "with a non-NULL second surface, geometry inputs continue; with NULL, level_trigger_warp(WARP_OP_DEATH) runs",
            "jp_map_addresses": {
                "update_mario_geometry_inputs": "0x80253834",
                "find_floor": "0x80381900",
            },
            "jp_assembly_branch": {
                "first_find_floor_result_test": "0x80253834 function: store result, branch on m->floor == NULL",
                "fallback_copy": "0x80253834 function: vec3f_copy(m->pos, m->marioObj->header.gfx.pos)",
                "second_find_floor": "immediately after copy, then jump back to ceiling/gas/water handling",
            },
            "possible_gfx_physical_divergence_fields": [
                "marioObj->header.gfx.pos (renderer position) can lag or differ from MarioState.pos",
                "marioObj object oPosX/oPosY/oPosZ are separate object fields and are not the fallback source",
                "gfx animation translation, quicksand sink, throwMatrix attachment, teleports/warps, and renderer/object updates can write or retain graphical position",
            ],
        },
        "instrumentation": {
            "method": "runtime x64 trampoline patch in the loaded DLL; the floor-null target's original 14 bytes are copied, a counter increment is executed, then those bytes resume at target+14",
            "target_discovery": "scan exported update_mario_geometry_inputs for cmp qword [m+0x70],0 followed by je and verify the 14-byte target prologue",
            "target_relative_to_geometry": "0x200",
            "verified_original_target_prologue": "488B83A80000004889F1488D5038",
            "branch_counter": "incremented only on the NULL-path target, not on direct find_floor calls",
            "traces": "positive and sweep rows capture state around direct geometry calls; native run counter is actual sm64_update execution",
        },
        "positive_control": {
            "status": "pass" if positive_proven else "fail",
            "branch_execution_proven": positive_proven,
            "geometry_call": geometry,
            "one_update": update,
            "interpretation": "The direct geometry call uses injected OOB physical XYZ and a valid graphical XYZ; the runtime counter hit once and the copied physical XYZ exactly matched gfx XYZ. The separate update case propagated the injected Y divergence through one full update.",
        },
        "natural_bounded_run": {
            "status": "complete" if natural["completed_updates"] == natural["requested_frames"] else "incomplete",
            "domain": {
                "initialization": "bootstrap_ttc: sm64_init, init_mario_from_save_file, TTC level/area/act selection, one game update",
                "post_bootstrap_state_writes": "only gControllerPads button/stick bytes; no MarioState, Object, floor, platform, RAM, or field injection",
                "controller_sequence": f"PRNG seed {natural['seed']}; button palette {{0, L, R, U, L+R}}; stick_x=stick_y=0 each frame",
                "speed": natural["speed"],
                "frames_requested": natural["requested_frames"],
            },
            "attempted_updates": natural["requested_frames"],
            "completed_updates": natural["completed_updates"],
            "branch_hits": natural["branch_hits"],
            "representative_frames": natural["representative_frames"],
            "bounded_negative": (
                f"No floor-null branch execution occurred in this {natural['completed_updates']}-update "
                "controller-only native DLL domain; this does not prove absence from all reachable game states or all input movies."
                if natural["branch_hits"] == 0 else None
            ),
        },
        "synthetic_sweep": {
            "domain": "5 physical Y values x 6 graphical Y values; physical x/z=(10000,10000) outside loaded TTC collision, graphical x/z=(800,1900), direct update_mario_geometry_inputs call",
            "rows": sweep["row_count"],
            "first_query_null_count": sweep["first_null_count"],
            "second_query_null_count": sweep["second_null_count"],
            "branch_hits": sweep["branch_hits"],
            "min_propagation_delta_y": sweep["min_propagation_delta_y"],
            "max_propagation_delta_y": sweep["max_propagation_delta_y"],
            "incident_scale_propagation_delta_y": sweep["incident_scale_delta_y"],
            "propagation_relation_exact": len(relation_rows) == sweep["row_count"],
            "relation": "copied_physical_xyz = graphical_xyz; copied_y - physical_y = graphical_y - physical_y",
            "incident_scale_movement_possible_under_injected_divergence": sweep["incident_scale_delta_y"] >= 500.0,
            "representative_traces": [
                row
                for row in sweep["rows"]
                if (row["physical_y"], row["gfx_y"]) in {
                    (-4207.0, -4207.0),
                    (-4207.0, -1051.75),
                    (1000.0, 500.0),
                }
            ],
        },
        "logs": logs,
        "limitations": [
            "The positive control is synthetic state divergence and is not evidence of a natural initiator or reachable precursor.",
            "The natural run starts from Wafel's bootstrap_ttc initialization, not the unavailable incident savestate; it is a bounded controller-only native domain.",
            "The runtime counter instruments the loaded JP libsm64 binary's compiled NULL-path target; it does not alter the fallback semantics, but it is not a rebuild of the DLL from modified mario.c source.",
            "No ROM MD5 is applicable because this run exercises the prebuilt unlocked DLL directly and does not load a ROM.",
        ],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as output:
        output.write(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps(result, indent=2, sort_keys=True))
    if result["status"] != "pass":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
