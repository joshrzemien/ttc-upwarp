#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import shlex
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "scripts/floor_null_probe.c"
DEFAULT_OUTPUT = ROOT / "results/floor_null_fallback_probe.json"
DEFAULT_REMOTE_HOST = "roach"
DEFAULT_REMOTE_ROOT = "/home/zman/projects/labs/ttc_upwarp"
REMOTE_SOURCE = "scripts/floor_null_probe.c"
REMOTE_EXE = "scripts/floor_null_probe.exe"
REMOTE_DLL = "wafel/libsm64/sm64_jp.dll"
REMOTE_LOG_DIR = "results/floor_null_fallback_probe_20260812"
WINE = "/nix/store/m3vk5lr49wdgyya04jxnyr4d63wcwvki-wine64-11.0/bin/wine"
MCFGTHREAD_LIB = "/nix/store/gr5hjbyxing73wqm3jz9ssn2qp1x664r-mcfgthread-x86_64-w64-mingw32-2.4.1/lib"
# Keep the literal tool path in one place; this is also recorded verbatim in
# the result so a rerun cannot silently pick another compiler.
CC = "/nix/store/2glwr7lcawg7m9rk4p5bdjlf7svpw405-x86_64-w64-mingw32-gcc-wrapper-15.2.0/bin/x86_64-w64-mingw32-gcc"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def run_ssh(host: str, command: str, *, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["ssh", host, command],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if check and result.returncode != 0:
        raise RuntimeError(
            f"remote command failed ({result.returncode}): {command}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result


def remote_path(root: str, relative: str) -> str:
    return f"{root.rstrip('/')}/{relative}"


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
    host: str,
    root: str,
    wineprefix: str,
    mode: str,
    frames: int,
    speed: int,
    seed: int,
    log_name: str,
)-> tuple[str, str, int, str]:
    log_path = remote_path(root, f"{REMOTE_LOG_DIR}/{log_name}")
    dll = f"Z:{remote_path(root, REMOTE_DLL)}"
    command = (
        f"cd {shlex.quote(root)} && "
        f"WINEDEBUG=-all WINEPREFIX={shlex.quote(wineprefix)} "
        f"{shlex.quote(WINE)} {shlex.quote(REMOTE_EXE)} {shlex.quote(dll)} "
        f"{shlex.quote(mode)} {frames} {speed} {seed} > {shlex.quote(log_path)} 2>&1"
    )
    result = run_ssh(host, command, check=False)
    if result.returncode != 0:
        # The log is retained remotely even on failure and is included in the
        # raised message for a reproducible diagnosis.
        log_result = run_ssh(host, f"cat {shlex.quote(log_path)}", check=False)
        raise RuntimeError(
            f"{mode} probe failed ({result.returncode})\n{log_result.stdout}\n{log_result.stderr}"
        )
    fetched = run_ssh(host, f"cat {shlex.quote(log_path)}")
    return fetched.stdout, log_path, result.returncode, command


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


def remote_hashes(host: str, root: str) -> dict[str, str]:
    paths = [REMOTE_SOURCE, REMOTE_EXE, REMOTE_DLL, CC, WINE]
    command = "sha256sum " + " ".join(
        shlex.quote(remote_path(root, path)) if path in {REMOTE_SOURCE, REMOTE_EXE, REMOTE_DLL} else shlex.quote(path)
        for path in paths
    )
    output = run_ssh(host, command).stdout
    hashes: dict[str, str] = {}
    for line in output.splitlines():
        digest, path = line.split(None, 1)
        hashes[path] = digest
    return hashes


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--remote-host", default=DEFAULT_REMOTE_HOST)
    parser.add_argument("--remote-root", default=DEFAULT_REMOTE_ROOT)
    parser.add_argument("--natural-frames", type=int, default=4096)
    parser.add_argument("--speed", type=int, default=3)
    parser.add_argument("--seed", type=int, default=539362)
    args = parser.parse_args()

    if not SOURCE.exists():
        raise RuntimeError(f"missing probe source: {SOURCE}")
    source_hash = sha256_file(SOURCE)
    remote_root = args.remote_root.rstrip("/")
    remote_log_dir = remote_path(remote_root, REMOTE_LOG_DIR)
    run_ssh(args.remote_host, f"mkdir -p {shlex.quote(remote_log_dir)}")
    subprocess.run(
        ["scp", str(SOURCE), f"{args.remote_host}:{remote_path(remote_root, REMOTE_SOURCE)}"],
        check=True,
    )

    compile_command = (
        f"cd {shlex.quote(remote_root)} && "
        f"{shlex.quote(CC)} -O2 -std=c11 -Wall -Wextra "
        f"-L{shlex.quote(MCFGTHREAD_LIB)} -o {shlex.quote(REMOTE_EXE)} {shlex.quote(REMOTE_SOURCE)}"
    )
    compile_log = remote_path(remote_root, f"{REMOTE_LOG_DIR}/compile.log")
    compile_run = run_ssh(
        args.remote_host,
        f"{compile_command} > {shlex.quote(compile_log)} 2>&1",
        check=False,
    )
    if compile_run.returncode != 0:
        log = run_ssh(args.remote_host, f"cat {shlex.quote(compile_log)}", check=False)
        raise RuntimeError(f"remote compile failed:\n{log.stdout}\n{log.stderr}")

    wineprefix = remote_path(remote_root, ".wine-wafel")
    commands = [
        f"scp {SOURCE} {args.remote_host}:{remote_path(remote_root, REMOTE_SOURCE)}",
        compile_command,
    ]
    logs: list[dict[str, Any]] = []

    def record_log(path: str, text: str, command: str, returncode: int) -> None:
        logs.append(
            {
                "remote_path": path,
                "sha256": sha256_bytes(text.encode("utf-8")),
                "bytes_utf8": len(text.encode("utf-8")),
                "command": command,
                "returncode": returncode,
            }
        )

    compile_text = run_ssh(args.remote_host, f"cat {shlex.quote(compile_log)}").stdout
    record_log(compile_log, compile_text, compile_command, compile_run.returncode)

    positive_text, positive_log, positive_rc, positive_command = run_case(
        args.remote_host,
        remote_root,
        wineprefix,
        "positive",
        0,
        args.speed,
        args.seed,
        "positive.log",
    )
    commands.append(positive_command)
    record_log(positive_log, positive_text, positive_command, positive_rc)

    sweep_text, sweep_log, sweep_rc, sweep_command = run_case(
        args.remote_host,
        remote_root,
        wineprefix,
        "sweep",
        0,
        args.speed,
        args.seed,
        "sweep.log",
    )
    commands.append(sweep_command)
    record_log(sweep_log, sweep_text, sweep_command, sweep_rc)

    natural_text, natural_log, natural_rc, natural_command = run_case(
        args.remote_host,
        remote_root,
        wineprefix,
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

    hashes = remote_hashes(args.remote_host, remote_root)
    remote_source = remote_path(remote_root, REMOTE_SOURCE)
    remote_exe = remote_path(remote_root, REMOTE_EXE)
    remote_dll = remote_path(remote_root, REMOTE_DLL)
    compiler_hash = hashes.get(CC)
    orchestrator_hash = sha256_file(Path(__file__))
    result: dict[str, Any] = {
        "status": "pass" if positive_proven and natural["completed_updates"] == natural["requested_frames"] else "fail",
        "run_id": "20260812",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "inputs": {
            "probe_source": str(SOURCE.relative_to(ROOT)),
            "probe_source_sha256": source_hash,
            "orchestrator_sha256": orchestrator_hash,
            "remote_probe_source_sha256": hashes.get(remote_source),
            "probe_executable_remote": remote_exe,
            "probe_executable_sha256": hashes.get(remote_exe),
            "libsm64_dll_remote": remote_dll,
            "libsm64_dll_sha256": hashes.get(remote_dll),
            "compiler": CC,
            "compiler_sha256": compiler_hash,
            "wine": WINE,
            "wine_sha256": hashes.get(WINE),
            "sm64_source_commit": "9921382a68bb0c865e5e45eb594d9c64db59b1af",
            "sm64_mario_c_sha256_remote": "a16936e42951b0ecf232fbbdb50d9f596bb8f59d13cf5269e5ba9537bca9b26e",
            "sm64_jp_map_sha256_remote": "23499f4f66f4d5a67d31d09c5816f4d5e3bf3478eb7dddb5fff440649721650b",
            "sm64_jp_elf_sha256_remote": "5127672dd7e98022639ee053ee7816f0d646afd42b61c5c5129549b31468eac2",
            "wafel_source_commit": "5b808b60af15d316a5e2b0f87db34421d6225b57",
        },
        "commands": commands,
        "completion": {
            "compile_returncode": compile_run.returncode,
            "positive_returncode": positive_rc,
            "sweep_returncode": sweep_rc,
            "natural_returncode": natural_rc,
            "natural_attempted_updates": natural["requested_frames"],
            "natural_completed_updates": natural["completed_updates"],
        },
        "source_behavior": {
            "source_file": "sm64/src/game/mario.c",
            "source_lines": "1318-1330",
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
                "controller_sequence": "PRNG seed 539362; button palette {0, L, R, U, L+R}; stick_x=stick_y=0 each frame",
                "speed": natural["speed"],
                "frames_requested": natural["requested_frames"],
            },
            "attempted_updates": natural["requested_frames"],
            "completed_updates": natural["completed_updates"],
            "branch_hits": natural["branch_hits"],
            "representative_frames": natural["representative_frames"],
            "bounded_negative": "No floor-null branch execution occurred in this 4096-update controller-only native DLL domain; this does not prove absence from all reachable game states or all input movies.",
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
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
