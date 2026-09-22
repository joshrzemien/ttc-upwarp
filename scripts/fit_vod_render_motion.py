#!/usr/bin/env python3
"""Auditable Mario screen-space tracking and temporal fit for the VOD candidates.

The script intentionally compares only localized Mario image coordinates.  It does
not use full-frame similarity, does not treat VOD frames as N64 VIs, and never
fills a failed localization with an interpolated coordinate. Images are read from
local manifest paths or explicitly mapped historical paths, then hash-verified.
SSH fetching requires an explicit --remote-host; manifest host labels are inert.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
import os
import sys
from pathlib import Path
from typing import Any, Iterable

import cv2
import numpy as np
import PIL
from PIL import Image

from extract_vod_fit_targets import parse_path_map, read_asset_bytes

ROOT = Path(__file__).resolve().parents[1]
TARGET_MANIFEST_DEFAULT = ROOT / "results/vod_fit_targets.json"
RENDER_MANIFEST_DEFAULT = ROOT / "results/vod_fit_render_manifest.json"
RESULT_DEFAULT = ROOT / "media/vod_render_motion_fit.json"
CACHE_DEFAULT = Path(os.environ.get("VOD_MOTION_CACHE", "/tmp/vod_motion_cache"))
SCENARIOS = ("reachable_no_mutation", "synthetic_no_flip", "synthetic_bit_clear")
EVENT_ENCODED = (431, 432)
LANDING_VISIBLE_ENCODED = 500
ENCODED_START = 340
ENCODED_END = 530
EMU_START = 1
EMU_END = 280
MAPPING_STEPS = (1, 2)
VOD_NOMINAL_PTS_STEP = 1001
PAIR_COVERAGE_GATE = {
    "pre_event": 30,
    "event": 1,
    "transition": 5,
    "landing": 10,
}

# These are fixed before scenario ranking.  The red/blue thresholds were chosen
# from emulator sprite pixels, then held fixed for the lossy VOD images.
TRACK_CONFIG: dict[str, Any] = {
    "hsv": {
        "red_hue_low": 14,
        "red_hue_high": 170,
        "red_saturation_min": 70,
        "red_value_min": 45,
        "blue_hue_low": 90,
        "blue_hue_high": 140,
        "blue_saturation_min": 50,
        "blue_value_min": 25,
    },
    "vod": {
        "roi": [0, 34, 340, 190],
        "min_red_area": 80,
        "min_local_blue_pixels_radius2": 8,
        "max_component_width": 70,
        "max_component_height": 60,
        "continuity_gate_px": 55.0,
        "forward_backward_tolerance_px": 7.0,
        # The clock/power-meter region is only rejected when it lacks the
        # minimum local Mario-like red/blue support.  This avoids masking the
        # real cap during the event while rejecting red overlay blobs.
        "overlay_region": [110, 30, 215, 100],
        "allow_overlay_candidate_area": 120,
        "allow_overlay_candidate_blue_radius2": 8,
    },
    "emulator": {
        "roi": [0, 60, 640, 460],
        "min_red_area": 40,
        "min_local_blue_pixels_radius2": 10,
        "max_component_width": 70,
        "max_component_height": 60,
        "continuity_gate_px": 90.0,
        "forward_backward_tolerance_px": 8.0,
        # Fixed power-meter/clock suppression.  Mario candidates in this
        # rectangle are discarded rather than guessed around the overlay.
        "overlay_region": [285, 70, 355, 155],
    },
    "viterbi": {
        "null_emission": 0.65,
        "candidate_emission_base": 0.55,
        "candidate_score_scale": 500.0,
        "max_score": 1600.0,
        "continuity_weight": 0.9,
        "large_jump_cost": 9.0,
        "null_to_candidate_cost": 0.15,
        "candidate_to_null_cost": 0.05,
    },
}


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def finite(value: Any) -> bool:
    return isinstance(value, (int, float, np.integer, np.floating)) and math.isfinite(float(value))


def clean_float(value: Any, digits: int = 6) -> float | None:
    if value is None:
        return None
    value = float(value)
    return round(value, digits) if math.isfinite(value) else None


def json_clean(value: Any) -> Any:
    """Convert numpy/scalar values and non-finite numbers to stable JSON."""
    if isinstance(value, dict):
        return {str(key): json_clean(val) for key, val in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_clean(item) for item in value]
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return clean_float(value)
    if isinstance(value, float):
        return clean_float(value)
    return value


def quantile(values: Iterable[float], q: float) -> float:
    vals = np.asarray([float(x) for x in values if finite(x)], dtype=float)
    if vals.size == 0:
        return float("inf")
    return float(np.quantile(vals, q, method="linear"))


def interval_count(values: list[dict[str, Any]], lo: int, hi: int) -> int:
    return sum(1 for value in values if lo <= int(value["index"]) <= hi and value.get("point") is not None)


def region_intersects(bbox: tuple[int, int, int, int], region: list[int]) -> bool:
    x, y, w, h = bbox
    rx, ry, rw, rh = region
    return not (x + w < rx or x > rw or y + h < ry or y > rh)


def load_rgb(path: Path) -> np.ndarray:
    with Image.open(path) as image:
        image.load()
        if image.mode not in ("RGB", "RGBA"):
            image = image.convert("RGB")
        return np.asarray(image.convert("RGB"), dtype=np.uint8)


def fetch_and_verify(
    source_path: str,
    expected_sha256: str,
    expected_size: int,
    expected_dimensions: tuple[int, int],
    destination: Path,
    remote_host: str | None,
    path_maps: Iterable[tuple[Path, Path]] = (),
) -> dict[str, Any]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    cached = destination.is_file()
    if cached:
        raw = destination.read_bytes()
        source_record: dict[str, Any] = {"transport": "cache", "path": str(destination)}
    else:
        raw, source_record = read_asset_bytes(source_path, remote_host, path_maps)
    digest = sha256_bytes(raw)
    if digest != expected_sha256:
        raise RuntimeError(f"image hash mismatch for {source_path}: {digest} != {expected_sha256}")
    if len(raw) != int(expected_size):
        raise RuntimeError(f"image size mismatch for {source_path}: {len(raw)} != {expected_size}")
    with Image.open(io.BytesIO(raw)) as image:
        image.load()
        dimensions = tuple(int(x) for x in image.size)
        mode = image.mode
    if dimensions != expected_dimensions:
        raise RuntimeError(f"image dimensions mismatch for {source_path}: {dimensions} != {expected_dimensions}")
    if not cached:
        destination.write_bytes(raw)
    return {
        "remote_path": source_path,
        "cache_path": str(destination),
        "source": source_record,
        "sha256": digest,
        "size_bytes": len(raw),
        "width": dimensions[0],
        "height": dimensions[1],
        "mode": mode,
        "hash_verified": True,
    }


def validate_target_manifest(target: dict[str, Any], manifest_path: Path | None = None) -> dict[str, Any]:
    if target.get("manifest_kind") != "vod_fit_targets":
        raise RuntimeError("unexpected VOD target manifest kind")
    selection = target.get("selection", {})
    if selection.get("encoded_index_range") != [ENCODED_START, ENCODED_END]:
        raise RuntimeError("unexpected VOD target index range")
    targets = target.get("targets")
    if not isinstance(targets, list) or len(targets) != ENCODED_END - ENCODED_START + 1:
        raise RuntimeError("VOD target count is not 191")
    indices = [int(item["encoded_index"]) for item in targets]
    if indices != list(range(ENCODED_START, ENCODED_END + 1)):
        raise RuntimeError("VOD target indices are not contiguous")
    event_pts = int(next(item["source_pts"] for item in targets if int(item["encoded_index"]) == EVENT_ENCODED[0]))
    offsets: list[float] = []
    for item in targets:
        if int(item["width"]) != 340 or int(item["height"]) != 215:
            raise RuntimeError("unexpected VOD crop dimensions")
        if item.get("png_sha256") != target["remote_pngs"][item["png_filename"]]["sha256"]:
            raise RuntimeError(f"VOD PNG hash disagreement: {item['png_filename']}")
        offset = (int(item["source_pts"]) - event_pts) / VOD_NOMINAL_PTS_STEP
        if abs(offset - round(offset)) > 1.0e-6:
            raise RuntimeError(f"non-integral VOD source frame offset at encoded index {item['encoded_index']}")
        offsets.append(offset)
    gap_pairs = [
        {
            "from_encoded_index": indices[index],
            "to_encoded_index": indices[index + 1],
            "source_pts_delta": int(targets[index + 1]["source_pts"]) - int(targets[index]["source_pts"]),
        }
        for index in range(len(targets) - 1)
        if int(targets[index + 1]["source_pts"]) - int(targets[index]["source_pts"]) != VOD_NOMINAL_PTS_STEP
    ]
    if list(target.get("event", {}).get("mapped_encoded_indices", [])) != [431, 432]:
        raise RuntimeError("unexpected VOD event interval")
    landing_item = next(item for item in targets if int(item["encoded_index"]) == LANDING_VISIBLE_ENCODED)
    return {
        "target_count": len(targets),
        "encoded_index_range": [indices[0], indices[-1]],
        "pre_event_count": int(selection.get("pre_event_count", 0)),
        "post_event_count": int(selection.get("post_event_count", 0)),
        "landing_visible_encoded_index": int(target["config"]["landing_visible_index"]),
        "event_source_pts": event_pts,
        "landing_source_frame_offset": clean_float(
            (int(landing_item["source_pts"]) - event_pts) / VOD_NOMINAL_PTS_STEP
        ),
        "source_frame_offset_range": [clean_float(min(offsets)), clean_float(max(offsets))],
        "nonuniform_source_pts_gaps": gap_pairs,
        "manifest_sha256": sha256_file(manifest_path or TARGET_MANIFEST_DEFAULT),
    }


def validate_render_manifest(render: dict[str, Any], manifest_path: Path | None = None) -> dict[str, Any]:
    if render.get("schema_version") != 1:
        raise RuntimeError("unexpected render manifest schema")
    if tuple(render.get("scenarios", {}).keys()) != ("reachable_no_mutation", "synthetic_bit_clear", "synthetic_no_flip"):
        # JSON ordering is not semantically important, but all exact names must
        # be present.  The stable output order is SCENARIOS below.
        if set(render.get("scenarios", {})) != set(SCENARIOS):
            raise RuntimeError("render scenario names do not match contract")
    for scenario in SCENARIOS:
        rows = render["scenarios"][scenario].get("render_rows")
        if not isinstance(rows, list) or len(rows) != EMU_END:
            raise RuntimeError(f"{scenario}: expected 280 render rows")
        vis = [int(row["vi"]) for row in rows]
        if vis != list(range(EMU_START, EMU_END + 1)):
            raise RuntimeError(f"{scenario}: VIs are not contiguous")
        for row in rows:
            image = row.get("image")
            if not image or tuple((int(image["width"]), int(image["height"]))) != (640, 480):
                raise RuntimeError(f"{scenario}: malformed image metadata")
            for key in ("gfx_x", "gfx_y", "gfx_z", "camera_pos_x", "camera_pos_y", "camera_pos_z"):
                if not finite(row["state"].get(key)):
                    raise RuntimeError(f"{scenario}: non-finite state field {key}")
    return {
        "scenario_names": list(SCENARIOS),
        "rows_per_scenario": EMU_END,
        "manifest_sha256": sha256_file(manifest_path or RENDER_MANIFEST_DEFAULT),
    }


def stage_images(
    target: dict[str, Any],
    render: dict[str, Any],
    cache_root: Path,
    remote_host: str | None,
    path_maps: Iterable[tuple[Path, Path]] = (),
) -> tuple[dict[int, Path], dict[str, dict[int, Path]], dict[str, Any]]:
    target_paths: dict[int, Path] = {}
    target_consumed: list[dict[str, Any]] = []
    for item in target["targets"]:
        encoded = int(item["encoded_index"])
        destination = cache_root / "vod" / item["png_filename"]
        record = fetch_and_verify(
            item["remote_png_path"],
            item["png_sha256"],
            int(item["png_size_bytes"]),
            (340, 215),
            destination,
            remote_host,
            path_maps,
        )
        record.update({"encoded_index": encoded, "relation": item["relation"]})
        target_paths[encoded] = destination
        target_consumed.append(record)

    scenario_paths: dict[str, dict[int, Path]] = {}
    scenario_consumed: dict[str, list[dict[str, Any]]] = {}
    for scenario in SCENARIOS:
        rows = render["scenarios"][scenario]["render_rows"]
        by_path: dict[str, Path] = {}
        records: list[dict[str, Any]] = []
        for row in rows:
            image = row["image"]
            remote_path = str(image["path"])
            if remote_path not in by_path:
                basename = Path(remote_path).name
                destination = cache_root / scenario / basename
                record = fetch_and_verify(
                    remote_path,
                    image["sha256"],
                    int(image["size_bytes"]),
                    (640, 480),
                    destination,
                    remote_host,
                    path_maps,
                )
                record.update({"image_ordinal": int(image["image_ordinal"])})
                by_path[remote_path] = destination
                records.append(record)
        scenario_paths[scenario] = {
            int(row["vi"]): by_path[str(row["image"]["path"])] for row in rows
        }
        scenario_consumed[scenario] = sorted(records, key=lambda item: int(item["image_ordinal"]))
    return target_paths, scenario_paths, {
        "vod": {
            "listed_remote_images": len(target_consumed),
            "hash_verified_images": len(target_consumed),
            "all_hashes_verified": len(target_consumed) == len(target["targets"]),
            "images": target_consumed,
        },
        "scenarios": {
            scenario: {
                "listed_unique_remote_images": len(scenario_consumed[scenario]),
                "hash_verified_images": len(scenario_consumed[scenario]),
                "all_hashes_verified": len(scenario_consumed[scenario]) == len(
                    {str(row["image"]["path"]) for row in render["scenarios"][scenario]["render_rows"]}
                ),
                "images": scenario_consumed[scenario],
            }
            for scenario in SCENARIOS
        },
    }


def make_candidates(rgb: np.ndarray, domain: str) -> list[dict[str, Any]]:
    config = TRACK_CONFIG["vod" if domain == "vod" else "emulator"]
    hsv_config = TRACK_CONFIG["hsv"]
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    hue, saturation, value = (hsv[:, :, index] for index in range(3))
    red = (
        ((hue <= hsv_config["red_hue_low"]) | (hue >= hsv_config["red_hue_high"]))
        & (saturation >= hsv_config["red_saturation_min"])
        & (value >= hsv_config["red_value_min"])
    ).astype(np.uint8)
    blue = (
        (hue >= hsv_config["blue_hue_low"])
        & (hue <= hsv_config["blue_hue_high"])
        & (saturation >= hsv_config["blue_saturation_min"])
        & (value >= hsv_config["blue_value_min"])
    ).astype(np.uint8)
    _, y0, _, y1 = config["roi"]
    red[:y0] = 0
    red[y1:] = 0
    blue[:y0] = 0
    blue[y1:] = 0
    count, labels, stats, centroids = cv2.connectedComponentsWithStats(red, 8)
    candidates: list[dict[str, Any]] = []
    overlay_region = config["overlay_region"]
    for label in range(1, count):
        x, y, width, height, area = (int(item) for item in stats[label])
        if area < 8 or area > 3000 or width < 3 or height < 2:
            continue
        if width > int(config["max_component_width"]) or height > int(config["max_component_height"]):
            continue
        local_blue: list[int] = []
        for radius in (2, 4, 6, 10):
            x0 = max(0, x - radius)
            y0b = max(0, y - radius)
            x1 = min(rgb.shape[1], x + width + radius)
            y1b = min(rgb.shape[0], y + height + radius)
            local_blue.append(int(blue[y0b:y1b, x0:x1].sum()))
        bbox = (x, y, width, height)
        in_overlay_region = region_intersects(bbox, overlay_region)
        if domain == "emu":
            masked_overlay = in_overlay_region
        else:
            masked_overlay = in_overlay_region and not (
                area >= int(config["allow_overlay_candidate_area"])
                and local_blue[0] >= int(config["allow_overlay_candidate_blue_radius2"])
            )
        score = (
            local_blue[0] * 8.0
            + local_blue[1] * 2.0
            + local_blue[2]
            + min(area, 400) * 0.1
        )
        strict = (
            area >= int(config["min_red_area"])
            and local_blue[0] >= int(config["min_local_blue_pixels_radius2"])
            and not masked_overlay
        )
        candidates.append(
            {
                "x": float(centroids[label][0]),
                "y": float(centroids[label][1]),
                "area": area,
                "bbox": [x, y, width, height],
                "blue_radius2": local_blue[0],
                "blue_radius4": local_blue[1],
                "blue_radius6": local_blue[2],
                "blue_radius10": local_blue[3],
                "score": float(score),
                "strict": bool(strict),
                "masked_overlay": bool(masked_overlay),
                "in_overlay_region": bool(in_overlay_region),
            }
        )
    return sorted(candidates, key=lambda item: (item["score"], item["blue_radius2"], item["area"]), reverse=True)


def viterbi_track(candidate_lists: list[list[dict[str, Any]]], domain: str) -> list[dict[str, Any] | None]:
    config = TRACK_CONFIG["viterbi"]
    domain_config = TRACK_CONFIG["vod" if domain == "vod" else "emulator"]
    strict_lists = [[item for item in candidates if item["strict"]] for candidates in candidate_lists]
    gate = float(domain_config["continuity_gate_px"])
    previous_states: list[int | None] = [None]
    previous_costs = [0.0]
    states: list[list[int | None]] = []
    backpointers: list[list[int]] = []
    for time_index, candidates in enumerate(strict_lists):
        current_states: list[int | None] = [None] + list(range(len(candidates)))
        current_costs: list[float] = []
        current_back: list[int] = []
        for state in current_states:
            if state is None:
                emission = float(config["null_emission"])
            else:
                emission = float(config["candidate_emission_base"]) - min(
                    float(candidates[state]["score"]), float(config["max_score"])
                ) / float(config["candidate_score_scale"])
            best_value = float("inf")
            best_previous = 0
            for previous_index, previous_state in enumerate(previous_states):
                transition = 0.0
                if state is not None and previous_state is not None and time_index > 0:
                    current = candidates[state]
                    previous = strict_lists[time_index - 1][previous_state]
                    distance = math.hypot(
                        float(current["x"]) - float(previous["x"]),
                        float(current["y"]) - float(previous["y"]),
                    )
                    if distance <= gate:
                        transition = float(config["continuity_weight"]) * (distance / gate) ** 2
                    else:
                        transition = float(config["large_jump_cost"]) + (distance - gate) / gate * 4.0
                elif state is not None and previous_state is None:
                    transition = float(config["null_to_candidate_cost"])
                elif state is None and previous_state is not None:
                    transition = float(config["candidate_to_null_cost"])
                value = previous_costs[previous_index] + emission + transition
                if value < best_value:
                    best_value = value
                    best_previous = previous_index
            current_costs.append(best_value)
            current_back.append(best_previous)
        states.append(current_states)
        backpointers.append(current_back)
        previous_states, previous_costs = current_states, current_costs
    if not states:
        return []
    index = min(range(len(previous_costs)), key=lambda item: previous_costs[item])
    output: list[dict[str, Any] | None] = []
    for time_index in range(len(states) - 1, -1, -1):
        state = states[time_index][index]
        output.append(None if state is None else strict_lists[time_index][state])
        index = backpointers[time_index][index]
    return output[::-1]


def image_sequence_track(paths_by_index: dict[int, Path], domain: str) -> tuple[dict[int, dict[str, Any] | None], dict[str, Any]]:
    # A reused emulator screenshot is one image observation, not a fabricated
    # new image.  Track unique paths and project the selected point back to rows.
    unique_paths: list[Path] = []
    path_to_ordinal: dict[str, int] = {}
    index_to_ordinal: dict[int, int] = {}
    for index in sorted(paths_by_index):
        key = str(paths_by_index[index])
        if key not in path_to_ordinal:
            path_to_ordinal[key] = len(unique_paths)
            unique_paths.append(paths_by_index[index])
        index_to_ordinal[index] = path_to_ordinal[key]
    candidate_lists = [make_candidates(load_rgb(path), domain) for path in unique_paths]
    forward = viterbi_track(candidate_lists, domain)
    backward_reversed = viterbi_track(candidate_lists[::-1], domain)
    backward = backward_reversed[::-1]
    tolerance = float(TRACK_CONFIG["vod" if domain == "vod" else "emulator"]["forward_backward_tolerance_px"])
    selected_by_ordinal: dict[int, dict[str, Any] | None] = {}
    agreement_by_ordinal: dict[int, float | None] = {}
    failure_by_ordinal: dict[int, list[str]] = {}
    for ordinal, (left, right) in enumerate(zip(forward, backward)):
        if left is None or right is None:
            selected_by_ordinal[ordinal] = None
            agreement_by_ordinal[ordinal] = None
            if left is None and right is None:
                candidates = candidate_lists[ordinal]
                if candidates and all(bool(item.get("masked_overlay")) for item in candidates):
                    failure_by_ordinal[ordinal] = ["fixed_overlay_or_HUD_mask"]
                elif not any(bool(item.get("strict")) for item in candidates):
                    failure_by_ordinal[ordinal] = ["no_candidate_meeting_fixed_red_blue_support"]
                else:
                    failure_by_ordinal[ordinal] = ["one_direction_track_failure"]
            else:
                failure_by_ordinal[ordinal] = ["one_direction_track_failure"]
            continue
        distance = math.hypot(float(left["x"]) - float(right["x"]), float(left["y"]) - float(right["y"]))
        agreement_by_ordinal[ordinal] = distance
        if distance <= tolerance:
            selected_by_ordinal[ordinal] = left
            failure_by_ordinal[ordinal] = []
        else:
            selected_by_ordinal[ordinal] = None
            failure_by_ordinal[ordinal] = ["forward_backward_disagreement"]
    rows: dict[int, dict[str, Any] | None] = {}
    failure_flags_by_index: dict[int, list[str]] = {}
    for index, ordinal in index_to_ordinal.items():
        selected = selected_by_ordinal[ordinal]
        failure_flags_by_index[index] = list(failure_by_ordinal.get(ordinal, []))
        if selected is None:
            rows[index] = None
        else:
            point = dict(selected)
            point["forward_backward_distance_px"] = agreement_by_ordinal[ordinal]
            point["image_ordinal"] = ordinal
            point["reused_image"] = sum(1 for value in index_to_ordinal.values() if value == ordinal) > 1
            rows[index] = point
    selected_count = sum(value is not None for value in rows.values())
    return rows, {
        "unique_image_count": len(unique_paths),
        "row_count": len(rows),
        "selected_row_count": selected_count,
        "failed_row_count": len(rows) - selected_count,
        "forward_backward_agreement_count": sum(value is not None for value in selected_by_ordinal.values()),
        "forward_backward_disagreement_count": sum(
            "forward_backward_disagreement" in failure_by_ordinal.get(index, [])
            for index in range(len(unique_paths))
        ),
        "reused_row_count": sum(
            max(0, sum(1 for value in index_to_ordinal.values() if value == ordinal) - 1)
            for ordinal in range(len(unique_paths))
        ),
        "reused_image_count": sum(1 for ordinal in range(len(unique_paths)) if sum(1 for value in index_to_ordinal.values() if value == ordinal) > 1),
        "failure_flags_by_index": {str(index): flags for index, flags in sorted(failure_flags_by_index.items())},
        "domain": domain,
        "fixed_config": TRACK_CONFIG["vod" if domain == "vod" else "emulator"],
    }

def make_vod_track(
    target: dict[str, Any],
    paths: dict[int, Path],
    points: dict[int, dict[str, Any] | None],
    failure_flags_by_index: dict[str, list[str]] | None = None,
) -> list[dict[str, Any]]:
    by_index = {int(item["encoded_index"]): item for item in target["targets"]}
    event_source_pts = int(by_index[EVENT_ENCODED[0]]["source_pts"])
    output: list[dict[str, Any]] = []
    for encoded in range(ENCODED_START, ENCODED_END + 1):
        item = by_index[encoded]
        point = points.get(encoded)
        flags = [] if point is not None else list((failure_flags_by_index or {}).get(str(encoded), []))
        if point is None and not flags:
            flags = ["no_reliable_forward_backward_agreement_or_color_candidate"]
        output.append(
            {
                "index": encoded,
                "encoded_index": encoded,
                "relation": item["relation"],
                "source_pts": int(item["source_pts"]),
                "source_frame_offset_from_event": (int(item["source_pts"]) - event_source_pts) / VOD_NOMINAL_PTS_STEP,
                "source_time_rational": item["source_time_rational"],
                "point": None
                if point is None
                else {
                    "x": clean_float(point["x"]),
                    "y": clean_float(point["y"]),
                    "confidence": clean_float(min(1.0, float(point["blue_radius2"]) / 50.0)),
                    "blue_radius2": int(point["blue_radius2"]),
                    "red_area": int(point["area"]),
                    "forward_backward_distance_px": clean_float(point["forward_backward_distance_px"]),
                    "image_ordinal": int(point["image_ordinal"]),
                    "reused_image": bool(point["reused_image"]),
                },
                "failure_flags": flags,
            }
        )
    return output


def make_emulator_track(
    render: dict[str, Any],
    scenario: str,
    paths: dict[int, Path],
    points: dict[int, dict[str, Any] | None],
    failure_flags_by_index: dict[str, list[str]] | None = None,
) -> list[dict[str, Any]]:
    rows = render["scenarios"][scenario]["render_rows"]
    output: list[dict[str, Any]] = []
    for row in rows:
        vi = int(row["vi"])
        point = points.get(vi)
        flags = [] if point is not None else list((failure_flags_by_index or {}).get(str(vi), []))
        if point is None and not flags:
            flags = ["no_reliable_forward_backward_agreement_or_color_candidate"]
        output.append(
            {
                "index": vi,
                "vi": vi,
                "image_ordinal": int(row["image"]["image_ordinal"]),
                "image_reused": bool(row["image_reused"]),
                "state": row["state"],
                "point": None
                if point is None
                else {
                    "x": clean_float(point["x"]),
                    "y": clean_float(point["y"]),
                    "confidence": clean_float(min(1.0, float(point["blue_radius2"]) / 60.0)),
                    "blue_radius2": int(point["blue_radius2"]),
                    "red_area": int(point["area"]),
                    "forward_backward_distance_px": clean_float(point["forward_backward_distance_px"]),
                    "image_ordinal": int(point["image_ordinal"]),
                    "reused_image": bool(point["reused_image"]),
                },
                "failure_flags": flags,
            }
        )
    return output




def points_array(track: list[dict[str, Any]], lo: int | None = None, hi: int | None = None) -> tuple[np.ndarray, np.ndarray]:
    selected: list[dict[str, Any]] = []
    for row in track:
        index = int(row["index"])
        if lo is not None and index < lo:
            continue
        if hi is not None and index > hi:
            continue
        if row.get("point") is not None:
            selected.append(row)
    if not selected:
        return np.empty((0,), dtype=int), np.empty((0, 2), dtype=float)
    return (
        np.asarray([int(row["index"]) for row in selected], dtype=int),
        np.asarray([[float(row["point"]["x"]), float(row["point"]["y"])] for row in selected], dtype=float),
    )


def fit_similarity(source: np.ndarray, target: np.ndarray) -> dict[str, Any] | None:
    if source.shape != target.shape or source.shape[0] < 3:
        return None
    source_mean = source.mean(axis=0)
    target_mean = target.mean(axis=0)
    source_centered = source - source_mean
    target_centered = target - target_mean
    covariance = source_centered.T @ target_centered
    left, singular_values, right_transpose = np.linalg.svd(covariance)
    # A similarity fit from a stationary/collinear fragment can drive the
    # residual arbitrarily low while carrying no 2-D trajectory information.
    # Reject it before mapping ranking rather than calling that a fit.
    if (
        len(singular_values) < 2
        or not finite(singular_values[0])
        or not finite(singular_values[1])
        or singular_values[0] <= 1.0e-9
        or singular_values[1] / singular_values[0] < 1.0e-3
    ):
        return None
    rotation = left @ right_transpose
    if np.linalg.det(rotation) < 0:
        left[:, -1] *= -1.0
        rotation = left @ right_transpose
    denominator = float(np.sum(source_centered * source_centered))
    if denominator <= 1.0e-9 or not finite(denominator):
        return None
    scale = float(np.sum(singular_values) / denominator)
    translation = target_mean - scale * (source_mean @ rotation)
    predicted = scale * (source @ rotation) + translation
    residuals = np.linalg.norm(predicted - target, axis=1)
    return {
        "scale": clean_float(scale),
        "rotation": [[clean_float(rotation[0, 0]), clean_float(rotation[0, 1])], [clean_float(rotation[1, 0]), clean_float(rotation[1, 1])]],
        "translation": [clean_float(translation[0]), clean_float(translation[1])],
        "residuals": residuals,
        "predicted": predicted,
        "rank_singular_values": [clean_float(value) for value in singular_values],
    }


def scenario_landing_vi(render: dict[str, Any], scenario: str) -> int | None:
    rows = render["scenarios"][scenario]["render_rows"]
    landing_actions = {0x04000471, 0x04000472}
    for row in rows:
        if int(row["state"].get("action", -1)) in landing_actions:
            return int(row["vi"])
    return None


def scenario_event_vi(render: dict[str, Any], scenario: str) -> int | None:
    rows = render["scenarios"][scenario]["render_rows"]
    for row in rows:
        if row["state"].get("event") != "tick":
            return int(row["vi"])
    return None


def mapped_encoded_for_source_offset(vod_track: list[dict[str, Any]], source_offset: float | None) -> int | None:
    if source_offset is None or not finite(source_offset):
        return None
    for row in vod_track:
        row_offset = float(row["source_frame_offset_from_event"])
        if abs(row_offset - float(source_offset)) <= 1.0e-9:
            return int(row["index"])
    return None
def source_frame_offset_at_encoded(track: list[dict[str, Any]], encoded: int) -> float | None:
    for row in track:
        if int(row.get("index", row.get("encoded_index", -1))) == int(encoded):
            value = row.get("source_frame_offset_from_event")
            return None if value is None else float(value)
    return None


def track_event_vi(track: list[dict[str, Any]]) -> int | None:
    for row in track:
        if row.get("state", {}).get("event") != "tick":
            return int(row["index"])
    return None


def track_landing_vi(track: list[dict[str, Any]]) -> int | None:
    landing_actions = {0x04000471, 0x04000472}
    for row in track:
        if int(row.get("state", {}).get("action", -1)) in landing_actions:
            return int(row["index"])
    return None


def transition_timing_metrics(
    vod_track: list[dict[str, Any]],
    event_vi: int | None,
    landing_vi: int | None,
    start_vi: int,
    step: int,
) -> dict[str, Any]:
    expected_event = source_frame_offset_at_encoded(vod_track, EVENT_ENCODED[0])
    expected_landing = source_frame_offset_at_encoded(vod_track, LANDING_VISIBLE_ENCODED)
    event_offset = (event_vi - start_vi) / step if event_vi is not None else None
    landing_offset = (landing_vi - start_vi) / step if landing_vi is not None else None
    event_error = (
        abs(event_offset - expected_event)
        if event_offset is not None and expected_event is not None
        else None
    )
    landing_error = (
        abs(landing_offset - expected_landing)
        if landing_offset is not None and expected_landing is not None
        else None
    )
    errors = [value for value in (event_error, landing_error) if value is not None]
    return {
        "expected_event_source_frame_offset": clean_float(expected_event),
        "expected_landing_source_frame_offset": clean_float(expected_landing),
        "event_source_frame_offset": clean_float(event_offset),
        "landing_source_frame_offset": clean_float(landing_offset),
        "event_mapped_encoded_frame": mapped_encoded_for_source_offset(vod_track, event_offset),
        "landing_mapped_encoded_frame": mapped_encoded_for_source_offset(vod_track, landing_offset),
        "event_transition_error_source_frame_offsets": clean_float(event_error),
        "landing_transition_error_source_frame_offsets": clean_float(landing_error),
        "transition_penalty": clean_float(0.1 * sum(float(value) for value in errors)),
    }



def velocity_metrics(indices: np.ndarray, source: np.ndarray, target: np.ndarray) -> dict[str, Any]:
    stationary_epsilon = 1.0e-6
    log_epsilon = 1.0e-3
    if len(indices) < 2:
        return {
            "velocity_pair_count": 0,
            "velocity_transition_count": 0,
            "both_moving_count": 0,
            "both_stationary_count": 0,
            "source_stationary_target_moving_count": 0,
            "source_moving_target_stationary_count": 0,
            "one_sided_stationary_count": 0,
            "direction_error": None,
            "magnitude_log_error": None,
            "stationary_epsilon": stationary_epsilon,
            "log_epsilon": log_epsilon,
            "one_sided_direction_penalty": 2.0,
        }
    source_velocity = np.diff(source, axis=0)
    target_velocity = np.diff(target, axis=0)
    directions: list[float] = []
    magnitudes: list[float] = []
    both_moving_count = 0
    both_stationary_count = 0
    source_stationary_target_moving_count = 0
    source_moving_target_stationary_count = 0
    for source_step, target_step in zip(source_velocity, target_velocity):
        source_norm = float(np.linalg.norm(source_step))
        target_norm = float(np.linalg.norm(target_step))
        source_stationary = source_norm < stationary_epsilon
        target_stationary = target_norm < stationary_epsilon
        if source_stationary and target_stationary:
            both_stationary_count += 1
            continue
        if not source_stationary and not target_stationary:
            both_moving_count += 1
            cosine = float(np.dot(source_step, target_step) / (source_norm * target_norm))
            cosine = max(-1.0, min(1.0, cosine))
            directions.append(1.0 - cosine)
            magnitudes.append(abs(math.log((source_norm + log_epsilon) / (target_norm + log_epsilon))))
            continue
        if source_stationary:
            source_stationary_target_moving_count += 1
        else:
            source_moving_target_stationary_count += 1
        # No direction exists for a stationary vector: count the transition as
        # maximally direction-discordant, and use the same finite log epsilon
        # as moving-vs-moving magnitude comparisons.
        directions.append(2.0)
        magnitudes.append(abs(math.log((source_norm + log_epsilon) / (target_norm + log_epsilon))))
    one_sided_count = source_stationary_target_moving_count + source_moving_target_stationary_count
    return {
        "velocity_pair_count": int(len(directions)),
        "velocity_transition_count": int(len(source_velocity)),
        "both_moving_count": int(both_moving_count),
        "both_stationary_count": int(both_stationary_count),
        "source_stationary_target_moving_count": int(source_stationary_target_moving_count),
        "source_moving_target_stationary_count": int(source_moving_target_stationary_count),
        "one_sided_stationary_count": int(one_sided_count),
        "direction_error": clean_float(float(np.median(directions)) if directions else None),
        "magnitude_log_error": clean_float(float(np.median(magnitudes)) if magnitudes else None),
        "stationary_epsilon": stationary_epsilon,
        "log_epsilon": log_epsilon,
        "one_sided_direction_penalty": 2.0,
    }


def classify_encoded_index(encoded: int) -> str:
    if encoded < EVENT_ENCODED[0]:
        return "pre_event"
    if encoded in EVENT_ENCODED:
        return "event"
    if encoded <= 499:
        return "transition"
    if encoded <= 530:
        return "landing"
    return "post_event"


def empty_category_counts() -> dict[str, int]:
    return {"pre_event": 0, "event": 0, "transition": 0, "landing": 0, "post_event": 0}


def evaluate_mapping(
    vod_track: list[dict[str, Any]],
    emulator_track: list[dict[str, Any]],
    scenario: str,
    render: dict[str, Any],
    start_vi: int,
    step: int,
) -> dict[str, Any]:
    emu_by_vi = {int(row["index"]): row for row in emulator_track}
    raw_pairs: list[tuple[int, dict[str, Any], dict[str, Any]]] = []
    raw_category_counts = empty_category_counts()
    for vod_row in vod_track:
        encoded = int(vod_row["index"])
        source_offset = float(vod_row["source_frame_offset_from_event"])
        if not finite(source_offset) or abs(source_offset - round(source_offset)) > 1.0e-6:
            raise RuntimeError(f"non-integral source frame offset at encoded index {encoded}")
        vi = int(start_vi + step * int(round(source_offset)))
        if vi < EMU_START or vi > EMU_END:
            continue
        emulator_row = emu_by_vi.get(vi)
        if vod_row.get("point") is None or emulator_row is None or emulator_row.get("point") is None:
            continue
        raw_pairs.append((encoded, vod_row, emulator_row))
        raw_category_counts[classify_encoded_index(encoded)] += 1

    # A reused emulator screenshot is one emitted image observation, not a new
    # rendered observation.  Keep the first encoded row for each identity in
    # deterministic encoded-index order; report raw row counts separately.
    unique_by_image: dict[int, tuple[int, dict[str, Any], dict[str, Any]]] = {}
    for pair in raw_pairs:
        identity = int(pair[2].get("image_ordinal", pair[2]["index"]))
        unique_by_image.setdefault(identity, pair)
    pairs = list(unique_by_image.values())
    category_counts = empty_category_counts()
    for encoded, _, _ in pairs:
        category_counts[classify_encoded_index(encoded)] += 1

    base_result: dict[str, Any] = {
        "start_vi": int(start_vi),
        "step_emulator_vis_per_nominal_source_frame": int(step),
        "pair_count": len(pairs),
        "unique_emulator_image_count": len(unique_by_image),
        "pair_row_count": len(raw_pairs),
        "category_pair_counts": category_counts,
        "category_pair_counts_raw_rows": raw_category_counts,
        "pair_observation_policy": "one representative encoded row per unique emulator emitted screenshot identity; raw rows reported separately",
    }
    if len(pairs) < 3:
        return {
            **base_result,
            "finite": False,
            "raw_score": float("inf"),
            "reason": "fewer_than_three_unique_rendered_image_pairs",
        }
    source = np.asarray([[float(pair[2]["point"]["x"]), float(pair[2]["point"]["y"])] for pair in pairs], dtype=float)
    target = np.asarray([[float(pair[1]["point"]["x"]), float(pair[1]["point"]["y"])] for pair in pairs], dtype=float)
    transform = fit_similarity(source, target)
    if transform is None:
        return {
            **base_result,
            "finite": False,
            "raw_score": float("inf"),
            "reason": "degenerate_source_geometry",
        }
    residuals = np.asarray(transform["residuals"], dtype=float)
    # Direction and magnitude are compared in the VOD target space after the
    # fitted scale+rotation, matching the held-out control protocol.
    transformed_source = np.asarray(transform["predicted"], dtype=float)
    velocity = velocity_metrics(np.asarray([pair[0] for pair in pairs], dtype=int), transformed_source, target)
    median_residual = float(np.median(residuals))
    p90_residual = float(np.quantile(residuals, 0.90))
    direction_error = float(velocity["direction_error"]) if finite(velocity["direction_error"]) else 1.0
    magnitude_error = float(velocity["magnitude_log_error"]) if finite(velocity["magnitude_log_error"]) else 1.0
    event_vi = scenario_event_vi(render, scenario)
    landing_vi = scenario_landing_vi(render, scenario)
    timing = transition_timing_metrics(vod_track, event_vi, landing_vi, start_vi, step)
    transition_penalty = float(timing["transition_penalty"] or 0.0)
    raw_score = median_residual + 0.5 * p90_residual + 20.0 * direction_error + 3.0 * magnitude_error + transition_penalty
    finite_result = bool(np.isfinite(residuals).all() and finite(raw_score))
    return {
        **base_result,
        "finite": finite_result,
        "raw_score": clean_float(raw_score),
        "median_position_residual_px": clean_float(median_residual),
        "p90_position_residual_px": clean_float(p90_residual),
        "rmse_position_residual_px": clean_float(float(np.sqrt(np.mean(residuals**2)))),
        "velocity_pair_count": int(velocity["velocity_pair_count"]),
        "velocity_transition_count": int(velocity["velocity_transition_count"]),
        "both_moving_count": int(velocity["both_moving_count"]),
        "both_stationary_count": int(velocity["both_stationary_count"]),
        "source_stationary_target_moving_count": int(velocity["source_stationary_target_moving_count"]),
        "source_moving_target_stationary_count": int(velocity["source_moving_target_stationary_count"]),
        "one_sided_stationary_count": int(velocity["one_sided_stationary_count"]),
        "velocity_stationary_epsilon": velocity["stationary_epsilon"],
        "velocity_log_epsilon": velocity["log_epsilon"],
        "velocity_one_sided_direction_penalty": velocity["one_sided_direction_penalty"],
        "velocity_direction_error_1_minus_cosine": velocity["direction_error"],
        "velocity_magnitude_log_error": velocity["magnitude_log_error"],
        "velocity_comparison_space": "VOD target space after fitted similarity scale and rotation",
        "event_vi": event_vi,
        "landing_vi": landing_vi,
        **timing,
        "transform": {
            "type": "2d_similarity_source_emulator_to_target_vod",
            "scale": transform["scale"],
            "rotation": transform["rotation"],
            "translation": transform["translation"],
            "singular_values": transform["rank_singular_values"],
        },
    }


def mapping_search(
    vod_track: list[dict[str, Any]],
    emulator_track: list[dict[str, Any]],
    scenario: str,
    render: dict[str, Any],
) -> dict[str, Any]:
    mappings: list[dict[str, Any]] = []
    for step in MAPPING_STEPS:
        for start_vi in range(EMU_START, EMU_END + 1):
            mappings.append(evaluate_mapping(vod_track, emulator_track, scenario, render, start_vi, step))
    finite_mappings = [item for item in mappings if item.get("finite") and finite(item.get("raw_score"))]
    finite_mappings.sort(
        key=lambda item: (
            float(item["raw_score"]),
            -int(item["pair_count"]),
            int(item["step_emulator_vis_per_nominal_source_frame"]),
            int(item["start_vi"]),
        )
    )
    eligible_mappings = [
        item
        for item in finite_mappings
        if int(item.get("category_pair_counts", {}).get("pre_event", 0)) >= PAIR_COVERAGE_GATE["pre_event"]
        and int(item.get("category_pair_counts", {}).get("event", 0)) >= PAIR_COVERAGE_GATE["event"]
        and int(item.get("category_pair_counts", {}).get("transition", 0)) >= PAIR_COVERAGE_GATE["transition"]
        and int(item.get("category_pair_counts", {}).get("landing", 0)) >= PAIR_COVERAGE_GATE["landing"]
    ]
    best = finite_mappings[0] if finite_mappings else None
    runner_up = finite_mappings[1] if len(finite_mappings) > 1 else None
    eligible_best = eligible_mappings[0] if eligible_mappings else None
    eligible_runner_up = eligible_mappings[1] if len(eligible_mappings) > 1 else None
    return {
        "mapping_count": len(mappings),
        "searched_start_vi_range": [EMU_START, EMU_END],
        "searched_steps_emulator_vis_per_nominal_source_frame": list(MAPPING_STEPS),
        "best": best,
        "runner_up": runner_up,
        "finite_mapping_count": len(finite_mappings),
        "eligible_mapping_count": len(eligible_mappings),
        "eligible_best": eligible_best,
        "eligible_runner_up": eligible_runner_up,
        "pair_coverage_gate": dict(PAIR_COVERAGE_GATE),
    }


def projection_control(render: dict[str, Any], scenario: str, track: list[dict[str, Any]]) -> dict[str, Any]:
    rows = render["scenarios"][scenario]["render_rows"]
    samples_by_identity: dict[int, tuple[int, np.ndarray, np.ndarray]] = {}
    raw_row_count = 0
    for row in rows:
        point_row = track[int(row["vi"]) - 1]
        if point_row.get("point") is None:
            continue
        raw_row_count += 1
        identity = int(row["image"]["image_ordinal"])
        if identity in samples_by_identity:
            continue
        features = np.asarray(
            [
                float(row["state"]["gfx_x"]) - float(row["state"]["camera_pos_x"]),
                float(row["state"]["gfx_y"]) - float(row["state"]["camera_pos_y"]),
                float(row["state"]["gfx_z"]) - float(row["state"]["camera_pos_z"]),
                1.0,
            ],
            dtype=float,
        )
        target = np.asarray([float(point_row["point"]["x"]), float(point_row["point"]["y"])], dtype=float)
        samples_by_identity[identity] = (int(row["vi"]), features, target)
    samples = [samples_by_identity[identity] for identity in sorted(samples_by_identity)]
    if len(samples) < 8:
        return {
            "sample_count": len(samples),
            "raw_row_count": raw_row_count,
            "finite": False,
            "reason": "too_few_reliable_state_projection_samples",
        }
    matrix = np.stack([sample[1] for sample in samples])
    targets = np.stack([sample[2] for sample in samples])
    identities = [identity for identity in sorted(samples_by_identity)]
    heldout = np.asarray([(identity % 2) == 1 for identity in identities], dtype=bool)
    train = ~heldout
    train_identities = {identity for identity, is_heldout in zip(identities, heldout) if not is_heldout}
    heldout_identities = {identity for identity, is_heldout in zip(identities, heldout) if is_heldout}
    overlap_count = len(train_identities & heldout_identities)
    if overlap_count:
        raise RuntimeError(f"{scenario}: projection control image identity split overlaps")
    coefficients = np.linalg.lstsq(matrix[train], targets[train], rcond=None)[0]
    residuals = np.linalg.norm(matrix[heldout] @ coefficients - targets[heldout], axis=1)
    all_residuals = np.linalg.norm(matrix @ coefficients - targets, axis=1)
    return {
        "sample_count": len(samples),
        "raw_row_count": raw_row_count,
        "train_count": int(train.sum()),
        "heldout_count": int(heldout.sum()),
        "train_image_identity_count": len(train_identities),
        "heldout_image_identity_count": len(heldout_identities),
        "image_identity_overlap_count": overlap_count,
        "heldout_median_residual_px": clean_float(float(np.median(residuals))),
        "heldout_p90_residual_px": clean_float(float(np.quantile(residuals, 0.90))),
        "all_median_residual_px": clean_float(float(np.median(all_residuals))),
        "all_p90_residual_px": clean_float(float(np.quantile(all_residuals, 0.90))),
        "finite": bool(np.isfinite(residuals).all()),
        "features": "[gfx_x-camera_pos_x,gfx_y-camera_pos_y,gfx_z-camera_pos_z,1], image-identity-even fit/image-identity-odd held-out",
    }


def paired_synthetic_control(render: dict[str, Any], tracks: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    left = tracks["synthetic_no_flip"]
    right = tracks["synthetic_bit_clear"]
    pairs = []
    for vi in range(1, 100):
        a = left[vi - 1].get("point")
        b = right[vi - 1].get("point")
        if a is not None and b is not None:
            pairs.append(math.hypot(float(a["x"]) - float(b["x"]), float(a["y"]) - float(b["y"])))
    return {
        "interval": [1, 99],
        "paired_reliable_frame_count": len(pairs),
        "max_pixel_difference": clean_float(max(pairs) if pairs else None),
        "median_pixel_difference": clean_float(float(np.median(pairs)) if pairs else None),
        "exact_image_pair_hash_control": True,
        "pass": bool(pairs and max(pairs) <= 1.0e-6),
    }


def make_control_target(source_track: list[dict[str, Any]], start_vi: int = 100, step: int = 1, seed: int = 0) -> list[dict[str, Any]]:
    # Create an encoded-index view of the emulator track without touching any
    # VOD pixels.  Perturbations are deterministic and are only used to set a
    # selection-aware positive-control score threshold.
    output: list[dict[str, Any]] = []
    by_vi = {int(row["index"]): row for row in source_track}
    for encoded in range(ENCODED_START, ENCODED_END + 1):
        vi = start_vi + step * (encoded - EVENT_ENCODED[0])
        source = by_vi.get(vi)
        if source is None or source.get("point") is None:
            point = None
        else:
            phase = float(seed * 0.731 + encoded * 0.173)
            amplitude = 1.25 if seed else 0.0
            point = {
                "x": float(source["point"]["x"]) + amplitude * math.sin(phase),
                "y": float(source["point"]["y"]) + amplitude * math.cos(phase * 1.17),
            }
        output.append(
            {
                "index": encoded,
                "encoded_index": encoded,
                "source_frame_offset_from_event": encoded - EVENT_ENCODED[0],
                "image_ordinal": None if source is None else int(source.get("image_ordinal", vi)),
                "point": point,
            }
        )
    return output


def evaluate_heldout_control(
    control_target: list[dict[str, Any]],
    source_track: list[dict[str, Any]],
) -> dict[str, Any]:
    """Fit a known positive mapping on disjoint emitted-image identities."""
    source_by_vi = {int(row["index"]): row for row in source_track}
    samples_by_identity: dict[int, tuple[int, list[float], list[float]]] = {}
    raw_row_count = 0
    for row in control_target:
        encoded = int(row["index"])
        vi = 100 + (encoded - EVENT_ENCODED[0])
        source_row = source_by_vi.get(vi)
        if source_row is None or source_row.get("point") is None or row.get("point") is None:
            continue
        raw_row_count += 1
        identity = int(source_row.get("image_ordinal", source_row["index"]))
        if identity in samples_by_identity:
            continue
        source_point = [float(source_row["point"]["x"]), float(source_row["point"]["y"])]
        target_point = [float(row["point"]["x"]), float(row["point"]["y"])]
        samples_by_identity[identity] = (encoded, source_point, target_point)
    samples = list(samples_by_identity.values())
    identities = list(samples_by_identity)
    train_identities = {identity for identity in identities if identity % 2 == 0}
    heldout_identities = {identity for identity in identities if identity % 2 == 1}
    overlap_count = len(train_identities & heldout_identities)
    if overlap_count:
        raise RuntimeError("heldout control image identity split overlaps")
    train_samples = [sample for identity, sample in samples_by_identity.items() if identity in train_identities]
    heldout_samples = [sample for identity, sample in samples_by_identity.items() if identity in heldout_identities]
    train_source = [sample[1] for sample in train_samples]
    train_target = [sample[2] for sample in train_samples]
    test_source = [sample[1] for sample in heldout_samples]
    test_target = [sample[2] for sample in heldout_samples]
    if len(train_source) < 3 or len(test_source) < 3:
        return {
            "finite": False,
            "raw_row_count": raw_row_count,
            "sample_count": len(samples),
            "train_count": len(train_source),
            "heldout_count": len(test_source),
            "train_image_identity_count": len(train_identities),
            "heldout_image_identity_count": len(heldout_identities),
            "image_identity_overlap_count": overlap_count,
            "raw_score": float("inf"),
            "reason": "too_few_train_or_heldout_pairs",
        }
    transform = fit_similarity(np.asarray(train_source, dtype=float), np.asarray(train_target, dtype=float))
    if transform is None:
        return {
            "finite": False,
            "raw_row_count": raw_row_count,
            "sample_count": len(samples),
            "train_count": len(train_source),
            "heldout_count": len(test_source),
            "train_image_identity_count": len(train_identities),
            "heldout_image_identity_count": len(heldout_identities),
            "image_identity_overlap_count": overlap_count,
            "raw_score": float("inf"),
            "reason": "degenerate_train_geometry",
        }
    heldout_source = np.asarray(test_source, dtype=float)
    heldout_target = np.asarray(test_target, dtype=float)
    predicted_heldout = (
        transform["scale"] * (heldout_source @ np.asarray(transform["rotation"], dtype=float))
        + np.asarray(transform["translation"], dtype=float)
    )
    residuals = np.linalg.norm(predicted_heldout - heldout_target, axis=1)
    velocity = velocity_metrics(
        np.arange(len(heldout_source), dtype=int),
        predicted_heldout,
        heldout_target,
    )
    direction = float(velocity["direction_error"]) if finite(velocity["direction_error"]) else 1.0
    magnitude = float(velocity["magnitude_log_error"]) if finite(velocity["magnitude_log_error"]) else 1.0
    timing = transition_timing_metrics(
        control_target,
        track_event_vi(source_track),
        track_landing_vi(source_track),
        100,
        1,
    )
    transition_penalty = float(timing["transition_penalty"] or 0.0)
    raw_score = float(np.median(residuals)) + 0.5 * float(np.quantile(residuals, 0.90)) + 20.0 * direction + 3.0 * magnitude + transition_penalty
    return {
        "finite": bool(np.isfinite(residuals).all() and finite(raw_score)),
        "raw_row_count": raw_row_count,
        "sample_count": len(samples),
        "train_count": len(train_source),
        "heldout_count": len(test_source),
        "train_image_identity_count": len(train_identities),
        "heldout_image_identity_count": len(heldout_identities),
        "image_identity_overlap_count": overlap_count,
        "median_position_residual_px": clean_float(float(np.median(residuals))),
        "p90_position_residual_px": clean_float(float(np.quantile(residuals, 0.90))),
        "velocity_pair_count": int(velocity["velocity_pair_count"]),
        "velocity_transition_count": int(velocity["velocity_transition_count"]),
        "both_moving_count": int(velocity["both_moving_count"]),
        "both_stationary_count": int(velocity["both_stationary_count"]),
        "source_stationary_target_moving_count": int(velocity["source_stationary_target_moving_count"]),
        "source_moving_target_stationary_count": int(velocity["source_moving_target_stationary_count"]),
        "one_sided_stationary_count": int(velocity["one_sided_stationary_count"]),
        "velocity_direction_error_1_minus_cosine": velocity["direction_error"],
        "velocity_magnitude_log_error": velocity["magnitude_log_error"],
        "raw_score": clean_float(raw_score),
        **timing,
        "fit_protocol": "even emitted-image identities fit transform; odd emitted-image identities held out",
    }


def calibrate_controls(render: dict[str, Any], tracks: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    positive_search_scores: list[float] = []
    positive_fixed: list[dict[str, Any]] = []
    positive_heldout: list[dict[str, Any]] = []
    for seed in range(16):
        control_target = make_control_target(tracks["synthetic_no_flip"], seed=seed)
        # The synthetic control does not have VOD metadata, but the mapping
        # evaluator only consumes index/point for this calibration call.
        search = mapping_search(control_target, tracks["synthetic_no_flip"], "synthetic_no_flip", render)
        if search["best"] and finite(search["best"].get("raw_score")):
            positive_search_scores.append(float(search["best"]["raw_score"]))
        fixed = evaluate_mapping(control_target, tracks["synthetic_no_flip"], "synthetic_no_flip", render, 100, 1)
        if fixed.get("finite"):
            positive_fixed.append(fixed)
        heldout = evaluate_heldout_control(control_target, tracks["synthetic_no_flip"])
        if heldout.get("finite"):
            positive_heldout.append(heldout)
    shifted_target = make_control_target(tracks["synthetic_no_flip"], seed=0)
    time_shifted = evaluate_mapping(shifted_target, tracks["synthetic_no_flip"], "synthetic_no_flip", render, 110, 1)
    post_separation = evaluate_mapping(
        make_control_target(tracks["synthetic_bit_clear"], seed=0),
        tracks["synthetic_no_flip"],
        "synthetic_no_flip",
        render,
        100,
        1,
    )
    positive_values = (
        positive_search_scores
        + [float(item["raw_score"]) for item in positive_fixed]
        + [float(item["raw_score"]) for item in positive_heldout]
    )
    positive_max = max(positive_values or [0.0])
    negative_values = [float(item["raw_score"]) for item in (time_shifted, post_separation) if finite(item.get("raw_score"))]
    negative_min = min(negative_values) if negative_values else float("inf")
    if finite(negative_min) and negative_min > positive_max:
        raw_threshold = 0.5 * (positive_max + negative_min)
    else:
        raw_threshold = max(5.0, positive_max * 1.5 + 5.0)
    positive_median_threshold = max(
        4.0,
        0.5
        * (
            max(
                [
                    float(item["median_position_residual_px"])
                    for item in positive_fixed + positive_heldout
                    if finite(item.get("median_position_residual_px"))
                ]
                or [0.0]
            )
            + min(
                (
                    float(item["median_position_residual_px"])
                    for item in (time_shifted, post_separation)
                    if finite(item.get("median_position_residual_px"))
                ),
                default=raw_threshold,
            )
        ),
    )
    control = {
        "positive_control": {
            "kind": "synthetic_no_flip_self_alignment_with_deterministic_localized_coordinate_jitters",
            "seed_count": 16,
            "all_mapping_count_per_seed": len(MAPPING_STEPS) * (EMU_END - EMU_START + 1),
            "best_raw_scores": [clean_float(value) for value in positive_search_scores],
            "selection_aware_q99_raw_score": clean_float(quantile(positive_search_scores, 0.99)),
            "maximum_positive_raw_score": clean_float(positive_max),
        },
        "same_scenario_heldout": {
            "kind": "synthetic_no_flip_even_emitted_image_identities_fit_odd_emitted_image_identities_held_out",
            "known_mapping": {"start_vi": 100, "step_emulator_vis_per_nominal_source_frame": 1},
            "scores": positive_heldout,
            "pass": bool(positive_heldout and all(float(item["raw_score"]) <= raw_threshold for item in positive_heldout)),
        },
        "same_scenario_fixed_alignment": {
            "kind": "known_mapping_all_reliable_pairs_with_deterministic_localized_coordinate_jitters",
            "scores": [
                {
                    "raw_score": item.get("raw_score"),
                    "median_position_residual_px": item.get("median_position_residual_px"),
                    "p90_position_residual_px": item.get("p90_position_residual_px"),
                }
                for item in positive_fixed
            ],
        },
        "deliberately_time_shifted_alignment": {
            "wrong_mapping": {"start_vi": 110, "step_emulator_vis_per_nominal_source_frame": 1},
            "score": time_shifted,
            "must_reject": True,
        },
        "synthetic_no_flip_vs_bit_clear_after_vi101": {
            "mapping": {"start_vi": 100, "step_emulator_vis_per_nominal_source_frame": 1},
            "score": post_separation,
            "must_separate": True,
        },
        "calibrated_thresholds": {
            "raw_score_acceptance_threshold": clean_float(raw_threshold),
            "position_median_acceptance_threshold_px": clean_float(positive_median_threshold),
            "velocity_direction_acceptance_threshold_1_minus_cosine": 0.75,
            "velocity_magnitude_log_acceptance_threshold": 1.5,
            "transition_error_acceptance_threshold_source_frame_offsets": 25.0,
            "interpretation": "diagnostic only for cross-domain VOD-vs-emulator classification; no image-level VOD localizer positive control",
            "formula": "midpoint(max selection-aware positive raw score, min deliberate-negative raw score) when separable; otherwise max(5,1.5*positive_max+5)",
            "multiple_comparison_count": len(MAPPING_STEPS) * (EMU_END - EMU_START + 1),
        },
        "separable": bool(finite(negative_min) and negative_min > positive_max),
        "negative_min_raw_score": clean_float(negative_min),
        "cross_domain_positive_control_available": False,
        "cross_domain_positive_control_limitation": "Positive controls perturb already-localized emulator coordinates; no image-level VOD localizer control was available, so threshold failures are diagnostic only.",
    }
    return json_clean(control)


def classify_scenario(
    scenario: str,
    search: dict[str, Any],
    vod_track: list[dict[str, Any]],
    calibration: dict[str, Any],
) -> dict[str, Any]:
    unrestricted_best = search.get("best")
    unrestricted_runner_up = search.get("runner_up")
    best = search.get("eligible_best")
    runner_up = search.get("eligible_runner_up")
    eligible_mapping_count = int(search.get("eligible_mapping_count", 0))
    threshold = calibration["calibrated_thresholds"]
    cross_domain_thresholds_available = bool(calibration.get("cross_domain_positive_control_available", False))
    vod_pre = interval_count(vod_track, ENCODED_START, EVENT_ENCODED[0] - 1)
    vod_event = interval_count(vod_track, EVENT_ENCODED[0], EVENT_ENCODED[-1])
    vod_transition = interval_count(vod_track, EVENT_ENCODED[-1] + 1, LANDING_VISIBLE_ENCODED - 1)
    vod_landing = interval_count(vod_track, LANDING_VISIBLE_ENCODED, ENCODED_END)
    coverage = {
        "vod_reliable_total": sum(row.get("point") is not None for row in vod_track),
        "vod_reliable_pre_event": vod_pre,
        "vod_reliable_event": vod_event,
        "vod_reliable_transition_433_499": vod_transition,
        "vod_reliable_landing_500_530": vod_landing,
        "required_pre_event_minimum": 30,
        "required_transition_minimum": 5,
        "required_landing_minimum": 10,
    }
    insufficient = vod_pre < 30 or vod_event < 1 or vod_transition < 5 or vod_landing < 10
    reason: list[str] = []
    if vod_pre < 30:
        reason.append("fewer_than_30_reliable_pre_event_VOD_frames")
    if vod_event < 1:
        reason.append("event_interval_not_localized")
    if vod_transition < 5:
        reason.append("transition_interval_433_499_has_insufficient_reliable_VOD_samples")
    if vod_landing < 10:
        reason.append("landing_window_has_insufficient_reliable_VOD_samples")
    status = "indeterminate" if insufficient else "rejected"
    acceptance_checks: dict[str, Any] = {
        "coverage_sufficient": not insufficient,
        "eligible_mapping_count": eligible_mapping_count,
        "required_mapping_pair_coverage": dict(PAIR_COVERAGE_GATE),
        "unrestricted_best_mapping": unrestricted_best,
        "unrestricted_runner_up_mapping": unrestricted_runner_up,
    }
    if best is None:
        status = "indeterminate"
        reason.append("no_eligible_mapping_meets_pair_coverage_gate")
    else:
        category_counts = best.get("category_pair_counts", {})
        mapping_pre = int(category_counts.get("pre_event", 0))
        mapping_event = int(category_counts.get("event", 0))
        mapping_transition = int(category_counts.get("transition", 0))
        mapping_landing = int(category_counts.get("landing", 0))
        acceptance_checks.update(
            {
                "best_mapping_pair_coverage": {
                    "pre_event": mapping_pre,
                    "event": mapping_event,
                    "transition_433_499": mapping_transition,
                    "landing_500_530": mapping_landing,
                },
                "best_mapping_pair_coverage_sufficient": True,
            }
        )
        raw = float(best["raw_score"])
        med_value = best.get("median_position_residual_px")
        direction_value = best.get("velocity_direction_error_1_minus_cosine")
        magnitude_value = best.get("velocity_magnitude_log_error")
        med = float(med_value) if finite(med_value) else float("inf")
        direction = float(direction_value) if finite(direction_value) else float("inf")
        magnitude = float(magnitude_value) if finite(magnitude_value) else float("inf")
        transition_values = [
            float(value)
            for value in (
                best.get("event_transition_error_source_frame_offsets"),
                best.get("landing_transition_error_source_frame_offsets"),
            )
            if finite(value)
        ]
        missing_transition = len(transition_values) < 2
        max_transition = max(transition_values) if transition_values else None
        if missing_transition:
            reason.append("event_or_landing_transition_not_observed_in_emulator_state")
        acceptance_checks.update(
            {
                "raw_score": clean_float(raw),
                "raw_score_threshold": threshold["raw_score_acceptance_threshold"],
                "median_position_residual_px": clean_float(med),
                "median_position_residual_threshold_px": threshold["position_median_acceptance_threshold_px"],
                "max_transition_error_source_frame_offsets": clean_float(max_transition),
                "transition_error_source_frame_offset_threshold": threshold["transition_error_acceptance_threshold_source_frame_offsets"],
                "velocity_direction_error": clean_float(direction),
                "velocity_direction_threshold": threshold["velocity_direction_acceptance_threshold_1_minus_cosine"],
                "velocity_magnitude_error": clean_float(magnitude),
                "velocity_magnitude_threshold": threshold["velocity_magnitude_log_acceptance_threshold"],
            }
        )
        passed = (
            cross_domain_thresholds_available
            and bool(calibration.get("separable"))
            and raw <= float(threshold["raw_score_acceptance_threshold"])
            and med <= float(threshold["position_median_acceptance_threshold_px"])
            and direction <= float(threshold["velocity_direction_acceptance_threshold_1_minus_cosine"])
            and magnitude <= float(threshold["velocity_magnitude_log_acceptance_threshold"])
            and max_transition is not None
            and max_transition <= float(threshold["transition_error_acceptance_threshold_source_frame_offsets"])
            and not insufficient
        )
        if passed:
            status = "fit"
        else:
            status = (
                "indeterminate"
                if insufficient
                or not cross_domain_thresholds_available
                or not bool(calibration.get("separable"))
                or missing_transition
                else "rejected"
            )
            if not cross_domain_thresholds_available:
                reason.append("no_image_level_cross_domain_positive_control_for_VOD_localizer_thresholds")
            if not calibration.get("separable"):
                reason.append("positive_and_negative_controls_not_separable")
            if raw > float(threshold["raw_score_acceptance_threshold"]):
                reason.append("raw_score_exceeds_diagnostic_threshold")
            if med > float(threshold["position_median_acceptance_threshold_px"]):
                reason.append("position_residual_exceeds_diagnostic_threshold")
            if direction > float(threshold["velocity_direction_acceptance_threshold_1_minus_cosine"]):
                reason.append("velocity_direction_disagreement")
            if magnitude > float(threshold["velocity_magnitude_log_acceptance_threshold"]):
                reason.append("velocity_magnitude_disagreement")
            if max_transition is not None and max_transition > float(threshold["transition_error_acceptance_threshold_source_frame_offsets"]):
                reason.append("event_or_landing_transition_timing_disagreement")
    return {
        "status": status,
        "scenario": scenario,
        "eligible_mapping_count": eligible_mapping_count,
        "observed_score": None if best is None else best.get("raw_score"),
        "calibrated_threshold": threshold["raw_score_acceptance_threshold"],
        "margin_threshold_minus_score": None if best is None else clean_float(float(threshold["raw_score_acceptance_threshold"]) - float(best["raw_score"])),
        "best_mapping": best,
        "runner_up_mapping": runner_up,
        "coverage": coverage,
        "acceptance_checks": acceptance_checks,
        "rejection_or_indeterminate_reasons": sorted(set(reason)),
        "limitations": [
            "Coordinates are localized screen-space cap points only; failed frames remain null.",
            "No full-scene or camera-image similarity was used as a proxy for motion.",
            "Encoded VOD frames are not equated with N64 VIs; the reported mapping is only a searched alignment.",
            "Emulator screenshot reuse and VOD H.264/composite scaling are retained in the evidence and not interpreted as game-update cadence; pair gates/ranking use one representative row per unique emitted emulator screenshot identity and report raw rows separately.",
            "Positive controls perturb already-localized emulator coordinates rather than image pixels; no image-level cross-domain VOD localizer positive control was available, so score/component threshold failures are diagnostic and cannot establish a cross-domain fit.",
        ],
    }

def validate_controls_and_result(result: dict[str, Any]) -> None:
    if set(result["scenarios"]) != set(SCENARIOS):
        raise RuntimeError("result scenarios do not match required names")
    if result["search"]["mapping_count_per_scenario"] != 560:
        raise RuntimeError("mapping search is incomplete")
    analyzer = result.get("analyzer", {})
    analyzer_path = ROOT / str(analyzer.get("path", ""))
    if analyzer.get("deterministic") is not True:
        raise RuntimeError("analyzer is not marked deterministic")
    if analyzer.get("path") != str(Path(__file__).resolve().relative_to(ROOT)):
        raise RuntimeError("analyzer path is not repo-relative script path")
    if analyzer.get("sha256") != sha256_file(analyzer_path):
        raise RuntimeError("embedded analyzer SHA-256 does not match current script")
    if not analyzer.get("python_version") or not analyzer.get("numpy_version") or not analyzer.get("pillow_version"):
        raise RuntimeError("missing analyzer dependency versions")
    if not result["image_consumption"]["vod"]["all_hashes_verified"]:
        raise RuntimeError("not all VOD image hashes were verified")
    if any(not item["all_hashes_verified"] for item in result["image_consumption"]["scenarios"].values()):
        raise RuntimeError("not all emulator image hashes were verified")
    if not result["controls"]["paired_synthetic_pre_event"]["pass"]:
        raise RuntimeError("paired synthetic pre-event tracker control failed")
    if not result["controls"]["finite_residuals"]:
        raise RuntimeError("non-finite residual in result")
    if any(result["scenarios"][name]["status"] not in {"fit", "rejected", "indeterminate"} for name in SCENARIOS):
        raise RuntimeError("invalid scenario status")
    calibration = result["controls"]["calibration"]
    if not calibration.get("cross_domain_positive_control_available", False):
        if any(result["scenarios"][name]["status"] != "indeterminate" for name in SCENARIOS):
            raise RuntimeError("cross-domain status must remain indeterminate without image-level positive control")
    for name in SCENARIOS:
        if int(result["scenarios"][name].get("eligible_mapping_count", 0)) == 0 and result["scenarios"][name]["status"] != "indeterminate":
            raise RuntimeError("scenario with no eligible mapping must remain indeterminate")
    for name, projection in result["controls"]["projection_continuity_against_gfx_and_camera_state"].items():
        if int(projection.get("image_identity_overlap_count", 0)) != 0:
            raise RuntimeError(f"{name}: projection control image identity overlap")
def run(args: argparse.Namespace) -> dict[str, Any]:
    analyzer_path = Path(__file__).resolve()
    analyzer_metadata = {
        "path": str(analyzer_path.relative_to(ROOT)),
        "sha256": sha256_file(analyzer_path),
        "python_version": sys.version.split()[0],
        "numpy_version": np.__version__,
        "pillow_version": PIL.__version__,
        "opencv_version": cv2.__version__,
        "deterministic": True,
    }
    target_path = Path(args.target_manifest).resolve()
    render_path = Path(args.render_manifest).resolve()
    target = json.loads(target_path.read_text())
    render = json.loads(render_path.read_text())
    target_validation = validate_target_manifest(target, target_path)
    render_validation = validate_render_manifest(render, render_path)
    remote_host = args.remote_host
    target_paths, scenario_paths, image_consumption = stage_images(
        target,
        render,
        Path(args.cache_dir).resolve(),
        remote_host,
        args.path_map,
    )
    vod_points, vod_tracker_summary = image_sequence_track(target_paths, "vod")
    vod_track = make_vod_track(target, target_paths, vod_points, vod_tracker_summary.get("failure_flags_by_index"))
    emulator_tracks: dict[str, list[dict[str, Any]]] = {}
    emulator_tracker_summaries: dict[str, dict[str, Any]] = {}
    projection_controls: dict[str, dict[str, Any]] = {}
    for scenario in SCENARIOS:
        points, summary = image_sequence_track(scenario_paths[scenario], "emu")
        emulator_tracks[scenario] = make_emulator_track(
            render,
            scenario,
            scenario_paths[scenario],
            points,
            summary.get("failure_flags_by_index"),
        )
        emulator_tracker_summaries[scenario] = summary
        projection_controls[scenario] = projection_control(render, scenario, emulator_tracks[scenario])
    paired = paired_synthetic_control(render, emulator_tracks)
    calibration = calibrate_controls(render, emulator_tracks)
    searches: dict[str, dict[str, Any]] = {}
    scenario_results: dict[str, dict[str, Any]] = {}
    for scenario in SCENARIOS:
        searches[scenario] = mapping_search(vod_track, emulator_tracks[scenario], scenario, render)
        scenario_results[scenario] = classify_scenario(scenario, searches[scenario], vod_track, calibration)
    finite_residuals = True
    for scenario in SCENARIOS:
        for mapping_name in ("best", "runner_up", "eligible_best", "eligible_runner_up"):
            mapping = searches[scenario].get(mapping_name)
            if mapping is not None:
                finite_residuals = finite_residuals and bool(mapping.get("finite"))
                for key in ("raw_score", "median_position_residual_px", "p90_position_residual_px", "rmse_position_residual_px"):
                    if mapping.get(key) is not None:
                        finite_residuals = finite_residuals and finite(mapping[key])
    result: dict[str, Any] = {
        "manifest_kind": "vod_render_motion_fit",
        "schema_version": 1,
        "status": "complete",
        "analyzer": analyzer_metadata,
        "inputs": {
            "target_manifest": str(target_path),
            "target_manifest_sha256": sha256_file(target_path),
            "render_manifest": str(render_path),
            "render_manifest_sha256": sha256_file(render_path),
            "remote_host": remote_host,
            "target_validation": target_validation,
            "render_validation": render_validation,
        },
        "search": {
            "mapping_count_per_scenario": len(MAPPING_STEPS) * (EMU_END - EMU_START + 1),
            "mapping_count_all_scenarios": len(SCENARIOS) * len(MAPPING_STEPS) * (EMU_END - EMU_START + 1),
            "searched_start_vi_range": [EMU_START, EMU_END],
            "searched_steps_emulator_vis_per_nominal_source_frame": list(MAPPING_STEPS),
            "pair_coverage_gate": dict(PAIR_COVERAGE_GATE),
            "temporal_mapping": {
                "formula": "source_frame_offset=(source_pts-event431_source_pts)/1001; VI=start_vi+step*round(source_frame_offset)",
                "event431_source_pts": target_validation["event_source_pts"],
                "nominal_pts_step": VOD_NOMINAL_PTS_STEP,
                "landing_source_frame_offset": target_validation["landing_source_frame_offset"],
                "observed_gaps": target_validation["nonuniform_source_pts_gaps"],
                "observed_gap_semantics": "source-frame offsets follow source PTS; encoded indices are labels and do not imply one-source-frame ordinal increments",
            },
            "transform_search": "2D similarity (scale, rotation, translation), no full-frame/scene score",
            "multiple_comparison_count": len(SCENARIOS) * len(MAPPING_STEPS) * (EMU_END - EMU_START + 1),
        },
        "tracker": {
            "method": "fixed HSV red-cap connected components paired with local blue-pixel support, fixed HUD/clock/bottom masks, Viterbi continuity, forward/backward agreement",
            "fixed_configuration": TRACK_CONFIG,
            "vod": {
                "summary": vod_tracker_summary,
                "reliable_frame_count": sum(row.get("point") is not None for row in vod_track),
                "localization_failure_rate": clean_float(sum(row.get("point") is None for row in vod_track) / len(vod_track)),
                "frames": vod_track,
            },
            "emulator": {
                scenario: {
                    "summary": emulator_tracker_summaries[scenario],
                    "reliable_frame_count": sum(row.get("point") is not None for row in emulator_tracks[scenario]),
                    "localization_failure_rate": clean_float(
                        sum(row.get("point") is None for row in emulator_tracks[scenario]) / len(emulator_tracks[scenario])
                    ),
                    "frames": emulator_tracks[scenario],
                }
                for scenario in SCENARIOS
            },
        },
        "image_consumption": image_consumption,
        "controls": {
            "paired_synthetic_pre_event": paired,
            "projection_continuity_against_gfx_and_camera_state": projection_controls,
            "calibration": calibration,
            "finite_residuals": finite_residuals,
            "deterministic_seed_protocol": "seeds 0..15; perturbation phase seed*0.731 + encoded_index*0.173; no random entropy",
        },
        "scenarios": scenario_results,
        "limitations": [
            "This result is a Mario motion/screen-space analysis, not a scene or camera agreement result.",
            "VOD crops are 340x215 decoded H.264/composite pixels; emulator images are 640x480 screenshots. Similarity normalization handles scale/aspect differences but does not recover original pixels.",
            "Emulator screenshot reuse is reported per scenario; repeated images are not treated as new rendered observations.",
            "The mapping permits one or two emulator VIs per nominal 1001-PTS source-frame interval solely as a search hypothesis; encoded indices are retained only as labels and no claim is made about the original game-update cadence.",
            "Red HUD arrows, top HUD, power-meter/clock overlay, and bottom overlays are explicitly masked or rejected by fixed rules. Failed/ambiguous frames have null coordinates.",
            "No manual spot labels, cherry-picked frames, or committed copyrighted images are used.",
        ],
        "command": " ".join([sys.executable, *sys.argv]),
    }
    result = json_clean(result)
    validate_controls_and_result(result)
    output_path = Path(args.output).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-manifest", default=str(TARGET_MANIFEST_DEFAULT))
    parser.add_argument("--render-manifest", default=str(RENDER_MANIFEST_DEFAULT))
    parser.add_argument("--output", default=str(RESULT_DEFAULT))
    parser.add_argument("--cache-dir", default=str(CACHE_DEFAULT))
    parser.add_argument("--remote-host", help="explicit SSH fallback for assets missing locally")
    parser.add_argument("--path-map", action="append", type=parse_path_map, default=[], metavar="OLD=LOCAL", help="relocate manifest path prefixes without altering provenance; repeatable")
    args = parser.parse_args()
    try:
        result = run(args)
    except Exception as error:  # pragma: no cover - CLI diagnostic path
        print(f"fit_vod_render_motion.py: {error}", file=sys.stderr)
        return 1
    print(json.dumps({
        "output": str(Path(args.output).resolve()),
        "scenario_status": {name: result["scenarios"][name]["status"] for name in SCENARIOS},
        "vod_reliable_frame_count": result["tracker"]["vod"]["reliable_frame_count"],
        "mapping_count_per_scenario": result["search"]["mapping_count_per_scenario"],
        "result_sha256": sha256_file(Path(args.output).resolve()),
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
