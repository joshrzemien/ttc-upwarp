#!/usr/bin/env python3
"""Run and validate the hash-bound emulator-rendered candidate captures.

This script runs locally by default. It stages isolated Mupen64Plus
configurations, runs three separate scenarios, validates retained PNGs, and
writes a manifest without embedding image bytes. Remote execution is an
explicit --remote-host/--remote-root opt-in.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
ORCHESTRATOR = Path(__file__).resolve()
CAPTURE_SCRIPT = ROOT / "scripts/capture_vod_fit.lua"
SCENARIOS = ("reachable_no_mutation", "synthetic_no_flip", "synthetic_bit_clear")
HEX_FIELDS = {"timer", "action", "y_word", "floor_ptr", "camera_ptr", "timer_repeat", "mario_object"}
INT_FIELDS = {"vi", "frame_id", "input_position", "camera_mode", "camera_yaw", "placed", "mutated"}
FLOAT_FIELDS = {
    "x", "y", "z", "vx", "vy", "vz", "forward_vel", "floor_height", "ceil_height",
    "gfx_x", "gfx_y", "gfx_z", "lakitu_focus_x", "lakitu_focus_y", "lakitu_focus_z",
    "lakitu_pos_x", "lakitu_pos_y", "lakitu_pos_z", "camera_focus_x", "camera_focus_y",
    "camera_focus_z", "camera_pos_x", "camera_pos_y", "camera_pos_z", "camera_unused",
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run_command(host: str | None, command: str, *, timeout: float = 120.0) -> str:
    invocation = ["ssh", host, command] if host else ["sh", "-c", command]
    result = subprocess.run(invocation, check=False, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f"command failed ({result.returncode}): {command}\n{result.stdout}\n{result.stderr}")
    return result.stdout


def file_record(host: str | None, path: str) -> dict[str, Any]:
    if host is None:
        local = Path(path)
        return {"path": str(local), "sha256": sha256_file(local), "size_bytes": local.stat().st_size}
    q = shlex.quote(path)
    output = run_command(host, f"set -eu; test -f {q}; sha256sum {q}; wc -c < {q}")
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    digest = lines[0].split()[0]
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise RuntimeError(f"invalid SHA-256 for {path}: {digest!r}")
    return {"path": path, "sha256": digest, "size_bytes": int(lines[1])}


def upload_script(host: str, remote_path: str) -> None:
    result = subprocess.run(["scp", str(CAPTURE_SCRIPT), f"{host}:{remote_path}"], check=False, capture_output=True, text=True, timeout=120.0)
    if result.returncode:
        raise RuntimeError(f"scp failed: {result.stdout}\n{result.stderr}")


def stage_configs(host: str | None, source: str, result_root: str, python: str) -> dict[str, Any]:
    config_root = f"{result_root}/config"
    rewrite = (
        "import pathlib,sys; p=pathlib.Path(sys.argv[1]); png=sys.argv[2]; s=p.read_text(); "
        "assert s.count('ScreenshotPath = \\\"/tmp\\\"') == 1; assert s.count('Fullscreen = False') == 1; "
        "s=s.replace('ScreenshotPath = \\\"/tmp\\\"', 'ScreenshotPath = \\\"'+png+'\\\"').replace('Fullscreen = False', 'Fullscreen = True'); p.write_text(s)"
    )
    run_command(host, f"set -eu; test ! -e {shlex.quote(result_root)}; mkdir -p {shlex.quote(config_root)}")
    source_copy = f"{config_root}/source.mupen64plus.cfg"
    run_command(host, f"set -eu; cp {shlex.quote(source)} {shlex.quote(source_copy)}")
    source_record = file_record(host, source)
    source_copy_record = file_record(host, source_copy)
    if source_record["sha256"] != source_copy_record["sha256"]:
        raise RuntimeError("staged source config hash mismatch")
    scenarios: dict[str, Any] = {}
    for scenario in SCENARIOS:
        scenario_root = f"{result_root}/{scenario}"
        config_dir = f"{config_root}/{scenario}"
        png_dir = f"{scenario_root}/png"
        log_path = f"{scenario_root}/emulator.log"
        run_command(host, "set -eu; "
            f"test ! -e {shlex.quote(scenario_root)}; mkdir -p {shlex.quote(config_dir)} {shlex.quote(png_dir)}; "
            f"cp {shlex.quote(source)} {shlex.quote(config_dir + '/mupen64plus.cfg')}; "
            f"{shlex.quote(python)} -c {shlex.quote(rewrite)} {shlex.quote(config_dir + '/mupen64plus.cfg')} {shlex.quote(png_dir)}")
        staged = f"{config_dir}/mupen64plus.cfg"
        scenarios[scenario] = {"config_dir": config_dir, "config": file_record(host, staged), "png_dir": png_dir, "log_path": log_path}
    return {"source": source_record, "staged_source_copy": source_copy_record, "scenarios": scenarios}


def run_scenario(
    args: argparse.Namespace, root: str, paths: dict[str, str], scenario: str,
    info: dict[str, Any], runtime_environment: dict[str, str],
) -> dict[str, Any]:
    host = args.remote_host
    binding = "reachable" if scenario == "reachable_no_mutation" else "synthetic"
    snapshot = paths[f"{binding}_snapshot"]
    movie = paths[f"{binding}_movie"]
    env = {
        **runtime_environment,
        "SDL_AUDIODRIVER": "dummy", "SDL_VIDEODRIVER": "x11",
        "SCENARIO": scenario, "MAX_VI": str(args.max_vi), "CAPTURE_START_VI": "1",
        "CAPTURE_END_VI": str(args.max_vi), "PLACE_VI": str(args.place_vi), "MUTATE_VI": str(args.mutate_vi),
        "EVENT_X": "800", "EVENT_Y": "-4207", "EVENT_Z": "1900", "M64_PATH": movie,
    }
    env_text = " ".join(f"{key}={shlex.quote(value)}" for key, value in sorted(env.items()))
    command_args = [
        paths["emulator"], "--nospeedlimit", "--emumode", "0", "--nosaveoptions",
        "--configdir", info["config_dir"], "--datadir", paths["data_dir"],
        "--corelib", paths["core"], "--rsp", paths["rsp"], "--gfx", paths["video"],
        "--audio", "dummy", "--input", "dummy", "--savestate", snapshot,
        "--fuzzer-lua", paths["capture_script"], paths["rom"],
    ]
    command = (
        f"set -eu; cd {shlex.quote(root)}; {env_text} {shlex.join(command_args)} "
        f"> {shlex.quote(info['log_path'])} 2>&1"
    )
    run_command(host, command, timeout=args.timeout)
    return {
        "command": shlex.join(["ssh", host, command]) if host else shlex.join(["sh", "-c", command]),
        "log": file_record(host, info["log_path"]),
        "log_text": run_command(host, f"set -eu; cat {shlex.quote(info['log_path'])}", timeout=120.0),
        "snapshot": file_record(host, snapshot), "movie": file_record(host, movie),
    }


def parse_kv(line: str, prefix: str) -> dict[str, str]:
    return {part.split("=", 1)[0]: part.split("=", 1)[1] for part in line[len(prefix):].split(",") if "=" in part}


def parse_scalar(name: str, value: str) -> Any:
    if name in HEX_FIELDS:
        return int(value, 16)
    if name in INT_FIELDS:
        return int(value)
    if name in FLOAT_FIELDS:
        return float(value)
    return value


def parse_log(text: str, scenario: str) -> dict[str, Any]:
    columns: list[str] | None = None
    header: dict[str, str] | None = None
    done: dict[str, str] | None = None
    rows: list[dict[str, Any]] = []
    events: list[dict[str, int]] = []
    current_vi: int | None = None
    for line in text.splitlines():
        if line.startswith("RENDER_HEADER,"):
            header = parse_kv(line, "RENDER_HEADER,")
        elif line.startswith("RENDER_COLUMNS,"):
            columns = line[len("RENDER_COLUMNS,"):].split(",")
        elif line.startswith("RENDER_ROW,"):
            if columns is None:
                raise RuntimeError(f"{scenario}: row before columns")
            fields = line.split(",")
            if len(fields) != len(columns) + 1:
                raise RuntimeError(f"{scenario}: row field count {len(fields)-1}, expected {len(columns)}")
            row = {name: parse_scalar(name, value) for name, value in zip(columns, fields[1:])}
            rows.append(row)
            current_vi = int(row["vi"])
        elif line.startswith("Core: Captured screenshot for frame "):
            match = re.search(r"frame (\d+)\.", line)
            if match is None or current_vi is None:
                raise RuntimeError(f"{scenario}: malformed screenshot marker")
            events.append({"core_frame": int(match.group(1)), "vi": current_vi})
        elif line.startswith("RENDER_DONE,"):
            done = parse_kv(line, "RENDER_DONE,")
    if header is None or columns is None or done is None or not rows or header.get("scenario") != scenario or done.get("scenario") != scenario:
        raise RuntimeError(f"{scenario}: incomplete or mismatched log")
    return {"header": header, "columns": columns, "rows": rows, "events": events, "done": done}


PNG_VALIDATOR = r'''import hashlib,json,sys
from pathlib import Path
from PIL import Image,ImageChops,ImageStat
root=Path(sys.argv[1]); out=[]; previous=None
for path in sorted(root.glob("*.png")):
 raw=path.read_bytes(); digest=hashlib.sha256(raw).hexdigest()
 with Image.open(path) as image:
  image.load(); width,height=image.size; rgb=image.convert("RGB"); colors=rgb.getcolors(maxcolors=width*height) or []; mean=ImageStat.Stat(rgb).mean; nonblank=ImageChops.difference(rgb,Image.new("RGB",rgb.size,(0,0,0))).getbbox() is not None
 out.append({"path":str(path),"sha256":digest,"size_bytes":len(raw),"width":width,"height":height,"mode":"RGB","unique_colors":len(colors),"mean_rgb":[round(x,6) for x in mean],"nonblank":nonblank,"changed_from_previous":previous is not None and digest != previous}); previous=digest
print(json.dumps(out,separators=(",",":")))'''


def validate_pngs(host: str | None, png_dir: str, python: str) -> list[dict[str, Any]]:
    output = run_command(host, f"set -eu; {shlex.quote(python)} -c {shlex.quote(PNG_VALIDATOR)} {shlex.quote(png_dir)}", timeout=300.0)
    result = json.loads(output)
    if not isinstance(result, list) or not result:
        raise RuntimeError(f"no PNGs validated in {png_dir}")
    return result


def finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and math.isfinite(float(value))


def near(value: Any, expected: float, tolerance: float = 1.0e-3) -> bool:
    return finite(value) and abs(float(value) - expected) <= tolerance


def is_landing(action: Any) -> bool:
    return int(action) in (0x04000471, 0x04000472)


def validate_scenario(scenario: str, parsed: dict[str, Any], pngs: list[dict[str, Any]], *, max_vi: int, place_vi: int, mutate_vi: int) -> dict[str, Any]:
    rows = parsed["rows"]
    if [int(row["vi"]) for row in rows] != list(range(1, max_vi + 1)):
        raise RuntimeError(f"{scenario}: VIs are not contiguous 1..max_vi")
    done = parsed["done"]
    if int(done["captured_vis"]) != max_vi or int(done["screenshot_requests"]) != max_vi:
        raise RuntimeError(f"{scenario}: completion counts mismatch")
    if not all(item["width"] == 640 and item["height"] == 480 and item["nonblank"] for item in pngs):
        raise RuntimeError(f"{scenario}: invalid/blank PNG")
    image_hashes = len({item["sha256"] for item in pngs})
    if image_hashes < 8:
        raise RuntimeError(f"{scenario}: changing game pixels not demonstrated")
    timing: dict[str, Any] = {"placement_vi": place_vi, "mutation_vi": None, "first_physics_vi": place_vi + 2}
    if scenario == "reachable_no_mutation":
        if int(done["field_write_events"]) != 0 or any(row["event"] != "tick" or row["placed"] or row["mutated"] for row in rows):
            raise RuntimeError("reachable_no_mutation contains synthetic writes")
        write_class = "controller_input_only"
        lower: list[dict[str, Any]] = []
        upper: list[dict[str, Any]] = []
    else:
        place = rows[place_vi - 1]
        if place["event"] != "place_ACT_FREEFALL_C5837800" or int(place["action"]) != 0x0100088C or int(place["y_word"]) != 0xC5837800:
            raise RuntimeError(f"{scenario}: placement marker/state failed")
        if any(not near(place[key], val) for key, val in (("x", 800), ("y", -4207), ("z", 1900), ("gfx_x", 800), ("gfx_y", -4207), ("gfx_z", 1900))):
            raise RuntimeError(f"{scenario}: injected XYZ failed")
        first_physics = rows[place_vi + 1]
        timing["first_physics"] = {"vi": int(first_physics["vi"]), "timer": int(first_physics["timer"]), "action": int(first_physics["action"]), "y_word": int(first_physics["y_word"]), "x": first_physics["x"], "y": first_physics["y"], "z": first_physics["z"], "floor_height": first_physics["floor_height"]}
        if scenario == "synthetic_no_flip":
            if int(done["field_write_events"]) != 1 or any(row["mutated"] for row in rows):
                raise RuntimeError("synthetic_no_flip write invariant failed")
            write_class = "placement_only"
            timing["mutation_vi"] = None
        else:
            mutation = rows[mutate_vi - 1]
            if mutation["event"] != "clear_bit_24_C5837800_to_C4837800" or int(mutation["y_word"]) != 0xC4837800 or not near(mutation["y"], -1051.75) or not near(mutation["gfx_y"], -4207):
                raise RuntimeError("synthetic_bit_clear mutation invariant failed")
            if int(done["field_write_events"]) != 2:
                raise RuntimeError("synthetic_bit_clear write count mismatch")
            write_class = "placement_and_bit_clear"
            timing["mutation_vi"] = mutate_vi
            timing["mutation"] = {"vi": mutate_vi, "timer": int(mutation["timer"]), "source_y_word": 0xC5837800, "mutated_y_word": int(mutation["y_word"]), "physical_y": mutation["y"], "gfx_y": mutation["gfx_y"]}
        lower = [row for row in rows if int(row["vi"]) > place_vi and is_landing(row["action"]) and near(row["floor_height"], -5211, 0.5)]
        upper = [row for row in rows if int(row["vi"]) > place_vi and is_landing(row["action"]) and near(row["floor_height"], -2487, 0.5)]
        timing["first_lower_landing_vi"] = int(lower[0]["vi"]) if lower else None
        timing["first_upper_landing_vi"] = int(upper[0]["vi"]) if upper else None
        if scenario == "synthetic_no_flip" and (not lower or upper or timing["first_lower_landing_vi"] != 138):
            raise RuntimeError("synthetic_no_flip expected VI138 lower landing failed")
        if scenario == "synthetic_bit_clear" and (not upper or timing["first_upper_landing_vi"] != 150):
            raise RuntimeError("synthetic_bit_clear expected VI150 upper landing failed")
    return {
        "status": "pass", "state_row_count": len(rows), "first_vi": 1, "last_vi": max_vi, "contiguous": True,
        "screenshot_request_count": int(done["screenshot_requests"]), "unique_rendered_png_count": len(pngs), "unique_rendered_png_hash_count": image_hashes,
        "nonblank_png_count": sum(1 for item in pngs if item["nonblank"]), "changed_png_count": sum(1 for item in pngs if item["changed_from_previous"]),
        "field_write_events": int(done["field_write_events"]), "declared_write_class": write_class, "timing": timing,
        "lower_landing": {"count": len(lower), "first_vi": int(lower[0]["vi"]) if lower else None, "floor_height": -5211.0},
        "upper_landing": {"count": len(upper), "first_vi": int(upper[0]["vi"]) if upper else None, "floor_height": -2487.0},
    }


def attach_images(scenario: str, parsed: dict[str, Any], pngs: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    events = parsed["events"]
    rows = parsed["rows"]
    if len(events) != len(pngs):
        raise RuntimeError(f"{scenario}: screenshot markers {len(events)} != PNGs {len(pngs)}")
    row_by_vi = {int(row["vi"]): row for row in rows}
    by_vi: dict[int, dict[str, Any]] = {}
    rendered: list[dict[str, Any]] = []
    for ordinal, (event, image) in enumerate(zip(events, pngs)):
        vi = int(event["vi"])
        record = {key: image[key] for key in ("path", "sha256", "size_bytes", "width", "height", "mode", "unique_colors", "mean_rgb", "nonblank", "changed_from_previous")}
        record.update({"capture_vi": vi, "core_screenshot_frame": int(event["core_frame"]), "image_ordinal": ordinal})
        by_vi[vi] = record
        row = row_by_vi[vi]
        rendered.append({"id": f"{scenario}_vi{vi:03d}", "vi": vi, "frame_id": int(row["frame_id"]), "core_screenshot_frame": int(event["core_frame"]), "state_row": row, "image": record})
    rows_out: list[dict[str, Any]] = []
    latest: dict[str, Any] | None = None
    first_image = next(iter(by_vi.values()), None)
    for row in rows:
        vi = int(row["vi"])
        if vi in by_vi:
            latest = by_vi[vi]
        if latest is None:
            # The first plugin screenshot can be reported one VI after its
            # request. Keep the row and make that asynchronous association
            # explicit rather than dropping the contiguous state row.
            latest = first_image
        if latest is None:
            raise RuntimeError(f"{scenario}: no decoded PNG")
        rows_out.append({"id": f"{scenario}_vi{vi:03d}", "vi": vi, "frame_id": int(row["frame_id"]), "state": row, "image": latest, "image_reused": int(latest["capture_vi"]) != vi})
    return rows_out, rendered


def baseline_projection(rows: list[dict[str, Any]], end_vi: int) -> list[tuple[Any, ...]]:
    keys = ("vi", "input_position", "timer", "action", "y_word", "x", "y", "z", "vx", "vy", "vz", "forward_vel", "floor_height", "ceil_height", "floor_ptr", "gfx_x", "gfx_y", "gfx_z", "lakitu_focus_x", "lakitu_focus_y", "lakitu_focus_z", "lakitu_pos_x", "lakitu_pos_y", "lakitu_pos_z", "camera_ptr", "camera_mode", "camera_yaw", "camera_focus_x", "camera_focus_y", "camera_focus_z", "camera_pos_x", "camera_pos_y", "camera_pos_z", "timer_repeat", "mario_object")
    return [tuple(row[key] for key in keys) for row in rows if int(row["vi"]) < end_vi]


def build_manifest(args: argparse.Namespace) -> dict[str, Any]:
    host = args.remote_host
    root = args.remote_root.rstrip("/") if host else str(args.root.expanduser().resolve())
    python = args.remote_python if host else sys.executable
    defaults = {
        "emulator": "emulator/bin/mupen64plus",
        "core": "emulator/lib/libmupen64plus.so.2.0.0",
        "rsp": "emulator/lib/mupen64plus/mupen64plus-rsp-hle.so",
        "video": "emulator/lib/mupen64plus/mupen64plus-video-glide64mk2.so",
        "data_dir": "emulator/share/mupen64plus",
        "config": "emulator/config/mupen64plus.cfg",
        "rom": "emulator/sm64.jp.z64",
        "reachable_snapshot": "emulator/route_vi475.m64p",
        "reachable_movie": "results/reachable_route_p078_r078_a+000.m64",
        "synthetic_snapshot": "emulator/tas26.jp.m64p.st",
        "synthetic_movie": "TTC-Upwarp-Overlay/Tas Attempts/tas26.m64",
    }

    def execution_path(value: str) -> str:
        path = Path(value)
        if not host:
            path = path.expanduser()
        if not path.is_absolute():
            path = Path(root) / path
        return str(path if host else path.resolve())

    paths = {name: execution_path(getattr(args, name) or value) for name, value in defaults.items()}
    paths["capture_script"] = f"{root}/scripts/capture_vod_fit.lua"
    result_root = execution_path(args.result_dir)
    if host:
        upload_script(host, paths["capture_script"])
    files = {name: file_record(host, path) for name, path in paths.items() if name != "data_dir"}
    files["video_plugin"] = files.pop("video")
    files["video_data_ini"] = file_record(host, f"{paths['data_dir']}/Glide64mk2.ini")
    files["reproduce_bitflip_pattern"] = file_record(host, f"{root}/scripts/reproduce_bitflip.lua")
    xwayland = run_command(host, "command -v Xwayland || true").strip()
    if xwayland:
        files["xwayland"] = file_record(host, xwayland)
    environment_query = "import json,os; print(json.dumps({k:os.environ[k] for k in ('DISPLAY','WAYLAND_DISPLAY','XDG_RUNTIME_DIR') if k in os.environ}))"
    runtime_environment = json.loads(run_command(host, f"{shlex.quote(python)} -c {shlex.quote(environment_query)}"))
    for key, value in (("DISPLAY", args.display), ("WAYLAND_DISPLAY", args.wayland_display), ("XDG_RUNTIME_DIR", args.runtime_dir)):
        if value is not None:
            runtime_environment[key] = value
    configs = stage_configs(host, paths["config"], result_root, python)
    parsed: dict[str, dict[str, Any]] = {}
    output: dict[str, Any] = {}
    run_records: dict[str, dict[str, Any]] = {}
    for scenario in SCENARIOS:
        run = run_scenario(args, root, paths, scenario, configs["scenarios"][scenario], runtime_environment)
        log = parse_log(run["log_text"], scenario)
        pngs = validate_pngs(host, configs["scenarios"][scenario]["png_dir"], python)
        validation = validate_scenario(scenario, log, pngs, max_vi=args.max_vi, place_vi=args.place_vi, mutate_vi=args.mutate_vi)
        rows, rendered = attach_images(scenario, log, pngs)
        parsed[scenario] = log
        run_records[scenario] = run
        output[scenario] = {"id_prefix": f"{scenario}_vi", "classification": scenario, "render_rows": rows, "rendered_frames": rendered, "completion": log["done"], "validation": validation, "remote_log": run["log"], "command": run["command"], "config": configs["scenarios"][scenario], "snapshot": run["snapshot"], "movie": run["movie"]}
    baseline = baseline_projection(parsed["synthetic_no_flip"]["rows"], args.place_vi)
    baseline_checks = {name: baseline_projection(parsed[name]["rows"], args.place_vi) == baseline for name in ("synthetic_bit_clear",)}
    if not all(baseline_checks.values()):
        raise RuntimeError("synthetic controls do not share the same pre-event baseline")
    files.update({
        "source_config": configs["source"], "staged_source_config": configs["staged_source_copy"],
        "fresh_vi475_snapshot": run_records["reachable_no_mutation"]["snapshot"],
        "reachable_route_movie": run_records["reachable_no_mutation"]["movie"],
        "synthetic_base_snapshot": run_records["synthetic_no_flip"]["snapshot"],
        "synthetic_base_movie": run_records["synthetic_no_flip"]["movie"],
    })
    files["rom"]["md5"] = run_command(host, f"set -eu; md5sum {shlex.quote(files['rom']['path'])} | cut -d' ' -f1").strip()
    local_capture_script = CAPTURE_SCRIPT if host else Path(paths["capture_script"])
    files["capture_script"].update({"local_path": str(local_capture_script), "local_sha256": sha256_file(local_capture_script)})
    files["orchestrator"] = {"path": str(ORCHESTRATOR), "sha256": sha256_file(ORCHESTRATOR), "size_bytes": ORCHESTRATOR.stat().st_size}
    return {
        "schema_version": 1, "name": "Fuzzy64 emulator-rendered VOD fit candidates", "status": "complete",
        "execution": {"transport": "ssh" if host else "local", "root": root, "environment": runtime_environment},
        "remote_host": host, "remote_root": root, "remote_result_dir": result_root,
        "capture": {"max_vi": args.max_vi, "capture_start_vi": 1, "capture_end_vi": args.max_vi, "place_vi": args.place_vi, "mutate_vi": args.mutate_vi, "event_xyz": [800.0, -4207.0, 1900.0], "screenshot_dimensions": [640, 480], "display": runtime_environment.get("DISPLAY"), "wayland_display": runtime_environment.get("WAYLAND_DISPLAY"), "emulator_mode": "Fuzzy64 pure interpreter (R4300Emulator=0)", "screenshot_method": "Fuzzer:takeScreenshot() once per captured VI; Glide64mk2 fullscreen X11 output", "image_row_policy": "render_rows contain every VI; image is the nearest decoded screenshot associated by the ordered Core screenshot marker (the first marker may follow VI1); image_reused records rows without a new PNG; rendered_frames retains only newly emitted PNGs"},
        "toolchain": files,
        "configuration": {
            "source": configs["source"], "staged_source_copy": configs["staged_source_copy"],
            "scenario_configs": {name: info["config"] for name, info in configs["scenarios"].items()},
            "overrides_only_in_staged_copies": {
                "Core[ScreenshotPath]": "scenario-specific PNG directory", "Video-General[Fullscreen]": True,
            },
            "command_line_overrides": {
                "emumode": 0, "gfx": paths["video"], "rsp": paths["rsp"], "audio": "dummy", "input": "dummy",
            },
        },
        "route_binding": {"reachable_no_mutation": {"classification": "reachable_no_mutation", "snapshot": files["fresh_vi475_snapshot"], "movie": files["reachable_route_movie"], "field_writes": "none beyond controller input"}, "synthetic_controls": {"classification": "synthetic_control", "shared_snapshot": files["synthetic_base_snapshot"], "shared_movie": files["synthetic_base_movie"], "paired_controls_share_declared_baseline": True, "pre_event_vi_count": args.place_vi - 1, "pre_event_projection_equal": baseline_checks}},
        "injection_contract": {"reachable_no_mutation": {"classification": "reachable_no_mutation", "writes": [], "allowed_input_write": "controller input only via inputs:setRaw(movie:getNextInput())"}, "synthetic_no_flip": {"classification": "synthetic_control", "writes": ["MarioState.action=0x0100088C (ACT_FREEFALL)", "MarioState.vel=(0,-20,0), forward_vel=0", "MarioState.pos=(800,-4207,1900), IEEE-754 Y word=0xC5837800", "MarioObject.header.gfx.pos=(800,-4207,1900)"], "mutation": "none"}, "synthetic_bit_clear": {"classification": "synthetic_control", "writes": ["same placement fields as synthetic_no_flip", "at mutate_vi=101: MarioState.pos[1] word 0xC5837800 -> 0xC4837800"], "mutation": "conditional bit 24 clear at established event update"}},
        "scenarios": output,
        "validation": {"status": "pass", "scenario_count": 3, "scenario_classifications": {name: output[name]["classification"] for name in SCENARIOS}, "synthetic_baseline": {"status": "pass", "pre_event_vi_count": args.place_vi - 1, "no_flip_vs_bit_clear_equal": True}, "limitations": ["Emulator-rendered images are synthetic candidates, not original-N64 timing or incident-VOD evidence.", "The fresh VI-475 reachable snapshot/route and established synthetic reproduction snapshot/movie are intentionally separate bindings; no cross-binding equivalence is claimed.", "Rows at emulator VIs without a new plugin screenshot reuse the latest retained PNG and mark image_reused=true; rendered_frames identifies the actual decoded PNG cadence."]},
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "media/vod_fit_render_manifest.json")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--result-dir", default="media/vod_fit_renders", help="new capture directory; relative to execution root")
    parser.add_argument("--remote-host", help="explicit SSH execution opt-in")
    parser.add_argument("--remote-root", help="absolute project root on --remote-host")
    parser.add_argument("--remote-python", default="python3", help="remote interpreter with Pillow installed")
    for name in ("emulator", "core", "rsp", "video", "data-dir", "config", "rom", "reachable-snapshot", "reachable-movie", "synthetic-snapshot", "synthetic-movie"):
        parser.add_argument(f"--{name}", help="path on execution host; relative paths resolve against its root")
    parser.add_argument("--max-vi", type=int, default=280)
    parser.add_argument("--place-vi", type=int, default=100)
    parser.add_argument("--mutate-vi", type=int, default=101)
    parser.add_argument("--display", help="override DISPLAY; otherwise inherit execution host environment")
    parser.add_argument("--wayland-display", help="override WAYLAND_DISPLAY")
    parser.add_argument("--runtime-dir", help="override XDG_RUNTIME_DIR")
    parser.add_argument("--timeout", type=float, default=600.0)
    args = parser.parse_args()
    if args.remote_host and (not args.remote_root or not Path(args.remote_root).is_absolute()):
        parser.error("--remote-host requires an absolute --remote-root")
    if args.remote_root and not args.remote_host:
        parser.error("--remote-root requires --remote-host")
    if args.max_vi < 120 or args.place_vi <= 30 or args.mutate_vi <= args.place_vi:
        raise SystemExit("require max_vi>=120, place_vi>30, and mutate_vi>place_vi")
    if args.output.exists():
        raise SystemExit(f"refusing to overwrite existing manifest: {args.output}")
    manifest = build_manifest(args)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    args.output.write_text(encoded)
    print(json.dumps({"output": str(args.output), "sha256": sha256_file(args.output), "bytes": args.output.stat().st_size, "scenario_counts": {name: manifest["scenarios"][name]["validation"] for name in SCENARIOS}}, indent=2, sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, subprocess.SubprocessError, ValueError, json.JSONDecodeError) as exc:
        print(f"render capture failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
