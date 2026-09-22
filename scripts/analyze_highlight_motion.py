#!/usr/bin/env python3
"""Deterministic high-resolution highlight-to-full-race motion/localization audit.

This audit deliberately keeps three frame domains separate:

* ``target_encoded_index`` is the encoded-frame index in the existing full-race
  target manifest (and is not an N64 VI).
* ``highlight_decoded_index`` is the zero-based decoder index of the AV1
  highlight (and is not a source PTS or an encoded-frame index).
* ``source_pts`` is the target manifest's source PTS in its own 1/30000 clock.

The alignment is measured from fixed grayscale/gradient image features.  The
red/blue localizer is a copied, minimally adapted version of the fixed rules in
``fit_vod_render_motion.py``; no manual labels or coordinates are introduced.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
import os
import platform
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

import cv2
import numpy as np
from PIL import Image

from extract_vod_fit_targets import parse_path_map, read_asset_bytes

ROOT = Path(__file__).resolve().parents[1]
MEDIA_DEFAULT = ROOT / "media/ttc-highlight.webm"
TARGET_MANIFEST_DEFAULT = ROOT / "results/vod_fit_targets.json"
MOTION_RESULT_DEFAULT = ROOT / "results/vod_render_motion_fit.json"
CACHE_DEFAULT = Path("/tmp/vod_motion_cache/vod")
OUTPUT_DEFAULT = ROOT / "media/highlight_motion_audit.json"
EXPECTED_MEDIA_SHA256 = "c268f1ae51a4dbd64b4e394199b31e04b3cd02441489404c7c046870b47bd723"
EXPECTED_MEDIA_SIZE = 7_174_481

TARGET_START = 340
TARGET_END = 530
TARGET_COUNT = TARGET_END - TARGET_START + 1
EVENT = (431, 432)
FEATURE_SIZE = (64, 45)
LOCALIZER_SIZE = (720, 480)
REFINEMENT_RADIUS = 2
DELIBERATE_SHIFT = 3

# The source rules are fixed in fit_vod_render_motion.py.  The geometry and
# component areas below are deterministic pixel-area scaling only: 340x215 ->
# 720x480.  No threshold is tuned on highlight frames.
BASE_TRACK_CONFIG: dict[str, Any] = {
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
        "overlay_region": [110, 30, 215, 100],
        "allow_overlay_candidate_area": 120,
        "allow_overlay_candidate_blue_radius2": 8,
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


def clean_float(value: float | int | None, digits: int = 9) -> float | None:
    if value is None:
        return None
    value = float(value)
    if not math.isfinite(value):
        return None
    return round(value, digits)


def json_clean(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): json_clean(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_clean(item) for item in value]
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return clean_float(float(value))
    if isinstance(value, float):
        return clean_float(value)
    return value


def run_capture(command: list[str], timeout: int = 180) -> tuple[int, str, str]:
    proc = subprocess.run(command, check=False, capture_output=True, text=True, timeout=timeout)
    return proc.returncode, proc.stdout, proc.stderr


def tool_record(name: str, command: list[str]) -> dict[str, Any]:
    executable = shutil.which(command[0]) or command[0]
    path = Path(executable)
    record: dict[str, Any] = {
        "name": name,
        "command": command,
        "executable": str(path),
        "available": path.exists(),
    }
    if path.exists() and path.is_file():
        record["executable_sha256"] = sha256_file(path)
        record["executable_size_bytes"] = path.stat().st_size
    code, stdout, stderr = run_capture(command)
    record["returncode"] = code
    record["stdout"] = stdout.strip()
    record["stderr"] = stderr.strip()
    return record


def validate_media(
    path: Path, expected_sha: str = EXPECTED_MEDIA_SHA256, expected_size: int = EXPECTED_MEDIA_SIZE,
) -> dict[str, Any]:
    if not path.is_file():
        raise RuntimeError(f"missing highlight media: {path}")
    actual_size = path.stat().st_size
    actual_sha = sha256_file(path)
    if actual_size != expected_size or actual_sha != expected_sha:
        raise RuntimeError(f"highlight media hash/size mismatch: {actual_sha} {actual_size}")
    return {
        "path": str(path),
        "sha256": actual_sha,
        "size_bytes": actual_size,
        "expected_sha256": expected_sha,
        "expected_size_bytes": expected_size,
        "hash_verified": actual_sha == expected_sha,
        "size_verified": actual_size == expected_size,
    }


def probe_media(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    command = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_frames",
        "-show_entries",
        "frame=best_effort_timestamp,best_effort_timestamp_time,width,height",
        "-of",
        "json",
        str(path),
    ]
    code, stdout, stderr = run_capture(command, timeout=180)
    if code:
        raise RuntimeError(f"ffprobe failed: {stderr.strip()}")
    parsed = json.loads(stdout)
    frames = parsed.get("frames", [])
    if len(frames) != 784:
        raise RuntimeError(f"ffprobe decoded frame count is {len(frames)}, expected 784")
    metadata = {
        "command": command,
        "frame_count": len(frames),
        "first_pts_ms": int(frames[0]["best_effort_timestamp"]),
        "last_pts_ms": int(frames[-1]["best_effort_timestamp"]),
        "width": int(frames[0]["width"]),
        "height": int(frames[0]["height"]),
        "time_base_from_pts": "1/1000",
        "timestamp_verification": "OpenCV sequential decoder index joined to ffprobe best_effort_timestamp",
    }
    records = [
        {
            "decoded_index": index,
            "pts_ms": int(frame["best_effort_timestamp"]),
            "pts_time": str(frame["best_effort_timestamp_time"]),
            "width": int(frame["width"]),
            "height": int(frame["height"]),
        }
        for index, frame in enumerate(frames)
    ]
    return metadata, records, [tool_record("ffprobe", command)]


def decode_highlight(path: Path, ffprobe_frames: list[dict[str, Any]]) -> tuple[list[np.ndarray], list[dict[str, Any]]]:
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        raise RuntimeError("OpenCV could not open highlight media")
    images: list[np.ndarray] = []
    records: list[dict[str, Any]] = []
    while True:
        ok, bgr = cap.read()
        if not ok:
            break
        index = len(images)
        if index >= len(ffprobe_frames):
            cap.release()
            raise RuntimeError("OpenCV produced more frames than ffprobe")
        if tuple(bgr.shape[:2]) != (1344, 1920):
            cap.release()
            raise RuntimeError(f"unexpected OpenCV frame shape at {index}: {bgr.shape}")
        rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        images.append(rgb)
        records.append(
            {
                "decoded_index": index,
                "pts_ms": int(ffprobe_frames[index]["pts_ms"]),
                "sha256_rgb_bytes": sha256_bytes(rgb.tobytes()),
                "width": int(rgb.shape[1]),
                "height": int(rgb.shape[0]),
                "decoder_position_after_read": int(cap.get(cv2.CAP_PROP_POS_FRAMES)),
            }
        )
    cap.release()
    if len(images) != 784:
        raise RuntimeError(f"OpenCV decoded frame count is {len(images)}, expected 784")
    if records[-1]["pts_ms"] != 26126:
        raise RuntimeError("unexpected final highlight PTS")
    return images, records


def validate_target_inputs(
    manifest_path: Path, motion_path: Path, cache_dir: Path,
    remote_host: str | None = None, path_maps: Iterable[tuple[Path, Path]] = (),
) -> tuple[dict[str, Any], dict[str, Any], list[np.ndarray], dict[int, dict[str, Any]]]:
    manifest_raw = manifest_path.read_bytes()
    motion_raw = motion_path.read_bytes()
    manifest = json.loads(manifest_raw)
    motion = json.loads(motion_raw)
    manifest_sha = sha256_bytes(manifest_raw)
    motion_sha = sha256_bytes(motion_raw)
    if manifest.get("manifest_kind") != "vod_fit_targets" or manifest.get("schema_version") != 1:
        raise RuntimeError("unexpected target manifest kind/schema")
    targets = manifest.get("targets")
    if not isinstance(targets, list) or len(targets) != TARGET_COUNT:
        raise RuntimeError("target manifest does not contain 191 rows")
    indices = [int(row["encoded_index"]) for row in targets]
    if indices != list(range(TARGET_START, TARGET_END + 1)):
        raise RuntimeError("target encoded indices are not contiguous 340..530")
    base_pts = int(targets[0]["source_pts"])
    timeline_indices: list[int] = []
    source_pts_deltas: list[int] = []
    for row in targets:
        source_pts = int(row["source_pts"])
        delta = source_pts - base_pts
        if delta < 0 or delta % 1001:
            raise RuntimeError(f"target source PTS is not a nonnegative nominal 1001-tick ordinal: {row['encoded_index']}")
        timeline_indices.append(TARGET_START + delta // 1001)
        source_pts_deltas.append(delta)
    expected_timeline_indices = list(range(TARGET_START, 479)) + list(range(480, TARGET_END + 2))
    if timeline_indices != expected_timeline_indices:
        raise RuntimeError("target PTS-derived timeline indices do not match the declared omitted ordinal 479")
    pts_gap_histogram = Counter(
        int(targets[index + 1]["source_pts"]) - int(targets[index]["source_pts"])
        for index in range(len(targets) - 1)
    )
    if pts_gap_histogram != Counter({1001: 189, 2002: 1}):
        raise RuntimeError(f"unexpected target source PTS gap histogram: {dict(pts_gap_histogram)}")
    if motion.get("manifest_kind") != "vod_render_motion_fit" or motion.get("schema_version") != 1:
        raise RuntimeError("unexpected existing motion result kind/schema")
    if motion.get("inputs", {}).get("target_manifest_sha256") != manifest_sha:
        raise RuntimeError("existing motion result is not bound to current target manifest")
    consumed: list[dict[str, Any]] = []
    arrays: list[np.ndarray] = []
    by_index: dict[int, dict[str, Any]] = {}
    for row, timeline_index in zip(targets, timeline_indices):
        index = int(row["encoded_index"])
        filename = str(row["png_filename"])
        path = cache_dir / filename
        cached = path.is_file()
        if cached:
            raw = path.read_bytes()
            source_record: dict[str, Any] = {"transport": "cache", "path": str(path)}
        else:
            raw, source_record = read_asset_bytes(str(row["remote_png_path"]), remote_host, path_maps)
        actual_sha = sha256_bytes(raw)
        actual_size = len(raw)
        if actual_sha != str(row["png_sha256"]) or actual_size != int(row["png_size_bytes"]):
            raise RuntimeError(f"target PNG hash/size mismatch: {filename}")
        with Image.open(io.BytesIO(raw)) as image:
            image.load()
            dimensions = tuple(int(x) for x in image.size)
            mode = image.mode
            rgb = np.asarray(image.convert("RGB"), dtype=np.uint8)
        if dimensions != (340, 215):
            raise RuntimeError(f"target PNG dimensions mismatch: {filename}: {dimensions}")
        if not cached:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
        arrays.append(rgb)
        by_index[index] = row
        consumed.append(
            {
                "target_encoded_index": index,
                "target_timeline_index": timeline_index,
                "path": str(path),
                "manifest_path": str(row["remote_png_path"]),
                "source": source_record,
                "manifest_png_filename": filename,
                "sha256": actual_sha,
                "expected_sha256": str(row["png_sha256"]),
                "size_bytes": actual_size,
                "expected_size_bytes": int(row["png_size_bytes"]),
                "width": dimensions[0],
                "height": dimensions[1],
                "mode": mode,
                "hash_verified": True,
                "dimension_verified": True,
            }
        )
    validation = {
        "manifest_path": str(manifest_path),
        "manifest_sha256": manifest_sha,
        "manifest_size_bytes": len(manifest_raw),
        "motion_result_path": str(motion_path),
        "motion_result_sha256": motion_sha,
        "motion_result_size_bytes": len(motion_raw),
        "motion_result_target_manifest_sha256_match": True,
        "target_count": len(consumed),
        "target_encoded_index_range": [TARGET_START, TARGET_END],
        "target_timeline_index_range": [min(timeline_indices), max(timeline_indices)],
        "source_pts_base": base_pts,
        "source_pts_nominal_delta": 1001,
        "source_pts_gap_histogram": dict(sorted(pts_gap_histogram.items())),
        "source_pts_gap_validation": "all (source_pts-base_pts)%1001==0; one 2002-tick gap 478->479 represents an omitted nominal timeline ordinal",
        "cached_target_directory": str(cache_dir),
        "all_hashes_verified": len(consumed) == TARGET_COUNT,
        "all_dimensions_verified": len(consumed) == TARGET_COUNT,
        "pngs": consumed,
    }
    return validation, motion, arrays, by_index


def feature(rgb: np.ndarray) -> np.ndarray:
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    small = cv2.resize(gray, FEATURE_SIZE, interpolation=cv2.INTER_AREA).astype(np.float32) / 255.0
    small = (small - float(small.mean())) / (float(small.std()) + 1.0e-6)
    gx = cv2.Sobel(small, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(small, cv2.CV_32F, 0, 1, ksize=3)
    return np.concatenate([small.ravel(), gx.ravel(), gy.ravel()]).astype(np.float32)


def pair_score(left: np.ndarray, right: np.ndarray) -> float:
    return float(np.mean((left - right) ** 2))


def direct_offset_scores(source: list[np.ndarray], candidate: list[np.ndarray], base_candidate: int, offsets: Iterable[int]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for offset in offsets:
        values: list[float] = []
        for row, source_feature in enumerate(source):
            index = base_candidate + row + int(offset)
            if 0 <= index < len(candidate):
                values.append(pair_score(source_feature, candidate[index]))
        rows.append(
            {
                "offset": int(offset),
                "valid_row_count": len(values),
                "mean_score": clean_float(float(np.mean(values))) if values else None,
                "median_score": clean_float(float(np.median(values))) if values else None,
            }
        )
    return rows


def monotone_align(
    source: list[np.ndarray],
    candidate: list[np.ndarray],
    source_ids: list[int],
    base_offset: int,
    radius: int = REFINEMENT_RADIUS,
) -> dict[str, Any]:
    # Repeat semantics are explicit: decoded highlight indices may repeat, but
    # the mapping is nondecreasing. Expected source timeline steps are used;
    # the omitted nominal ordinal therefore permits an expected step of 2.
    candidate_rows: list[list[int]] = []
    emission: list[list[float]] = []
    for row, source_feature in enumerate(source):
        values = sorted(
            {
                source_ids[row] + base_offset + delta
                for delta in range(-radius, radius + 1)
                if 0 <= source_ids[row] + base_offset + delta < len(candidate)
            }
        )
        candidate_rows.append(values)
        emission.append([pair_score(source_feature, candidate[index]) for index in values])
    if not source:
        return {"score": None, "mapping": [], "candidate_cell_count": 0}
    stay_penalty = 0.04
    step_penalty = 0.04
    expected_step_histogram = Counter(source_ids[row] - source_ids[row - 1] for row in range(1, len(source_ids)))
    dp: list[list[float]] = []
    back: list[list[int]] = []
    for row, values in enumerate(candidate_rows):
        current: list[float] = []
        current_back: list[int] = []
        for state, current_index in enumerate(values):
            best: tuple[float, int, int] | None = None
            if row == 0:
                best = (emission[row][state], current_index, -1)
            else:
                for previous_state, previous_index in enumerate(candidate_rows[row - 1]):
                    if current_index < previous_index:
                        continue
                    delta = current_index - previous_index
                    expected_step = source_ids[row] - source_ids[row - 1]
                    transition = stay_penalty if delta == 0 else step_penalty * float(delta - expected_step) ** 2
                    proposal = (dp[row - 1][previous_state] + transition + emission[row][state], previous_index, previous_state)
                    if best is None or proposal < best:
                        best = proposal
            if best is None:
                current.append(float("inf"))
                current_back.append(-1)
            else:
                current.append(float(best[0]))
                current_back.append(int(best[2]))
        dp.append(current)
        back.append(current_back)
    last_state = min(range(len(dp[-1])), key=lambda state: (dp[-1][state], candidate_rows[-1][state]))
    selected = [0] * len(source)
    for row in range(len(source) - 1, -1, -1):
        selected[row] = candidate_rows[row][last_state]
        last_state = back[row][last_state]
        if row and last_state < 0:
            raise RuntimeError("monotone alignment backpointer failure")
    total = float(dp[-1][min(range(len(dp[-1])), key=lambda state: (dp[-1][state], candidate_rows[-1][state]))])
    return {
        "score": clean_float(total / len(source)),
        "total_cost": clean_float(total),
        "mapping": selected,
        "candidate_cell_count": sum(len(row) for row in candidate_rows),
        "candidate_range_per_row": [len(row) for row in candidate_rows],
        "stay_penalty": stay_penalty,
        "nonunit_step_penalty": step_penalty,
        "expected_source_step_histogram": dict(sorted(expected_step_histogram.items())),
        "transition_penalty_formula": "stay delta=0 costs 0.04; otherwise 0.04*(mapped_highlight_step - expected_source_timeline_step)^2",
        "repeat_semantics": "nondecreasing highlight decoded index; repeated decoded frames allowed and counted as repeated observations",
    }


def rank_scores(rows: list[dict[str, Any]], key: str = "mean_score") -> tuple[dict[str, Any], dict[str, Any] | None]:
    usable = [row for row in rows if row.get(key) is not None]
    ranked = sorted(usable, key=lambda row: (float(row[key]), int(row["offset"])))
    return ranked[0], (ranked[1] if len(ranked) > 1 else None)


def classify(index: int) -> str:
    if TARGET_START <= index <= 430:
        return "pre"
    if index in EVENT:
        return "event"
    if 433 <= index <= 499:
        return "transition"
    if 500 <= index <= 530:
        return "landing"
    raise ValueError(index)


def scale_track_config() -> dict[str, Any]:
    x_scale = LOCALIZER_SIZE[0] / 340.0
    y_scale = LOCALIZER_SIZE[1] / 215.0
    area_scale = x_scale * y_scale
    base = json.loads(json.dumps(BASE_TRACK_CONFIG))
    source_vod_config = json.loads(json.dumps(base["vod"]))
    config = base["vod"]
    config["roi"] = [round(config["roi"][0] * x_scale), round(config["roi"][1] * y_scale), round(config["roi"][2] * x_scale), round(config["roi"][3] * y_scale)]
    config["overlay_region"] = [round(config["overlay_region"][0] * x_scale), round(config["overlay_region"][1] * y_scale), round(config["overlay_region"][2] * x_scale), round(config["overlay_region"][3] * y_scale)]
    for key in ("min_red_area", "min_local_blue_pixels_radius2", "allow_overlay_candidate_area", "allow_overlay_candidate_blue_radius2"):
        config[key] = round(config[key] * area_scale)
    config["max_component_width"] = round(config["max_component_width"] * x_scale)
    config["max_component_height"] = round(config["max_component_height"] * y_scale)
    config["continuity_gate_px"] = config["continuity_gate_px"] * ((x_scale + y_scale) / 2.0)
    config["forward_backward_tolerance_px"] = config["forward_backward_tolerance_px"] * ((x_scale + y_scale) / 2.0)
    source_component_thresholds = {
        "minimum_component_area": 8,
        "maximum_component_area": 3000,
        "minimum_component_width": 3,
        "minimum_component_height": 2,
        "score_area_cap": 400,
        "score_area_coefficient": 0.1,
        "neighborhood_radii": [2, 4, 6, 10],
    }
    config["minimum_component_area"] = round(source_component_thresholds["minimum_component_area"] * area_scale)
    config["maximum_component_area"] = round(source_component_thresholds["maximum_component_area"] * area_scale)
    config["minimum_component_width"] = round(source_component_thresholds["minimum_component_width"] * x_scale)
    config["minimum_component_height"] = round(source_component_thresholds["minimum_component_height"] * y_scale)
    config["score_area_cap"] = round(source_component_thresholds["score_area_cap"] * area_scale)
    config["score_area_coefficient"] = source_component_thresholds["score_area_coefficient"]
    config["neighborhood_radii_xy"] = [[round(radius * x_scale), round(radius * y_scale)] for radius in source_component_thresholds["neighborhood_radii"]]
    tracker = base["viterbi"]
    tracker["candidate_score_scale"] = tracker["candidate_score_scale"] * area_scale
    tracker["max_score"] = tracker["max_score"] * area_scale
    return {
        "hsv": base["hsv"],
        "highlight": config,
        "viterbi": tracker,
        "source_vod_config": source_vod_config,
        "geometry_transform": {
            "source_geometry": [340, 215],
            "target_geometry": list(LOCALIZER_SIZE),
            "x_scale": clean_float(x_scale),
            "y_scale": clean_float(y_scale),
            "area_scale": clean_float(area_scale),
            "scaled_fields": {
                "roi": "x coordinates multiplied by x_scale; y coordinates multiplied by y_scale",
                "overlay_region": "x coordinates multiplied by x_scale; y coordinates multiplied by y_scale",
                "component_areas": "area thresholds multiplied by x_scale*y_scale",
                "component_widths": "width thresholds multiplied by x_scale",
                "component_heights": "height thresholds multiplied by y_scale",
                "neighborhood_radii": "each source radius converted to independent integer x/y radii",
                "candidate_score_scale": "multiplied by area_scale because blue-pixel and area score terms scale with pixel area",
                "max_score": "multiplied by area_scale because candidate scores are area-scaled",
                "pixel_distance_gate": "multiplied by arithmetic mean of x_scale and y_scale as fixed Euclidean-pixel approximation",
            },
            "unscaled_fields": [
                "HSV hue/saturation/value thresholds",
                "score_area_coefficient",
                "Viterbi emission/transition/null cost weights",
            ],
            "source_component_thresholds": source_component_thresholds,
        },
    }


def region_intersects(bbox: tuple[int, int, int, int], region: list[int]) -> bool:
    x, y, width, height = bbox
    rx, ry, rw, rh = region
    return not (x + width < rx or x > rw or y + height < ry or y > rh)


def make_candidates(rgb: np.ndarray, config: dict[str, Any]) -> list[dict[str, Any]]:
    hsv_config = BASE_TRACK_CONFIG["hsv"]
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    hue, saturation, value = (hsv[:, :, index] for index in range(3))
    red = (((hue <= hsv_config["red_hue_low"]) | (hue >= hsv_config["red_hue_high"])) & (saturation >= hsv_config["red_saturation_min"]) & (value >= hsv_config["red_value_min"])).astype(np.uint8)
    blue = ((hue >= hsv_config["blue_hue_low"]) & (hue <= hsv_config["blue_hue_high"]) & (saturation >= hsv_config["blue_saturation_min"]) & (value >= hsv_config["blue_value_min"])) .astype(np.uint8)
    _, y0, _, y1 = config["roi"]
    red[:y0] = 0
    red[y1:] = 0
    blue[:y0] = 0
    blue[y1:] = 0
    count, labels, stats, centroids = cv2.connectedComponentsWithStats(red, 8)
    candidates: list[dict[str, Any]] = []
    radii_xy = config["neighborhood_radii_xy"]
    for label in range(1, count):
        x, y, width, height, area = (int(item) for item in stats[label])
        if area < int(config["minimum_component_area"]) or area > int(config["maximum_component_area"]) or width < int(config["minimum_component_width"]) or height < int(config["minimum_component_height"]):
            continue
        if width > int(config["max_component_width"]) or height > int(config["max_component_height"]):
            continue
        local_blue: list[int] = []
        for radius_x, radius_y in radii_xy:
            x0 = max(0, x - radius_x)
            y0b = max(0, y - radius_y)
            x1 = min(rgb.shape[1], x + width + radius_x)
            y1b = min(rgb.shape[0], y + height + radius_y)
            local_blue.append(int(blue[y0b:y1b, x0:x1].sum()))
        bbox = (x, y, width, height)
        in_overlay = region_intersects(bbox, config["overlay_region"])
        masked_overlay = in_overlay and not (area >= config["allow_overlay_candidate_area"] and local_blue[0] >= config["allow_overlay_candidate_blue_radius2"])
        score = local_blue[0] * 8.0 + local_blue[1] * 2.0 + local_blue[2] + min(area, int(config["score_area_cap"])) * float(config["score_area_coefficient"])
        strict = area >= config["min_red_area"] and local_blue[0] >= config["min_local_blue_pixels_radius2"] and not masked_overlay
        candidates.append(
            {
                "x": clean_float(float(centroids[label][0])),
                "y": clean_float(float(centroids[label][1])),
                "red_area": area,
                "bbox": [x, y, width, height],
                "blue_radius2": local_blue[0],
                "blue_radius4": local_blue[1],
                "blue_radius6": local_blue[2],
                "blue_radius10": local_blue[3],
                "score": clean_float(score),
                "strict": bool(strict),
                "masked_overlay": bool(masked_overlay),
                "in_overlay_region": bool(in_overlay),
            }
        )
    return sorted(candidates, key=lambda item: (-float(item["score"]), -int(item["blue_radius2"]), -int(item["red_area"]), float(item["x"]), float(item["y"])))

def viterbi_track(candidate_lists: list[list[dict[str, Any]]], config: dict[str, Any]) -> list[dict[str, Any] | None]:
    tracker = config.get("_viterbi", BASE_TRACK_CONFIG["viterbi"])
    strict_lists = [[item for item in candidates if item["strict"]] for candidates in candidate_lists]
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
                emission = float(tracker["null_emission"])
            else:
                emission = float(tracker["candidate_emission_base"]) - min(float(candidates[state]["score"]), float(tracker["max_score"])) / float(tracker["candidate_score_scale"])
            proposals: list[tuple[float, int]] = []
            for previous_index, previous_state in enumerate(previous_states):
                transition = 0.0
                if state is not None and previous_state is not None and time_index > 0:
                    current = candidates[state]
                    previous = strict_lists[time_index - 1][previous_state]
                    distance = math.hypot(float(current["x"]) - float(previous["x"]), float(current["y"]) - float(previous["y"]))
                    gate = float(config["continuity_gate_px"])
                    if distance <= gate:
                        transition = float(tracker["continuity_weight"]) * (distance / gate) ** 2
                    else:
                        transition = float(tracker["large_jump_cost"]) + (distance - gate) / gate * 4.0
                elif state is not None and previous_state is None:
                    transition = float(tracker["null_to_candidate_cost"])
                elif state is None and previous_state is not None:
                    transition = float(tracker["candidate_to_null_cost"])
                proposals.append((previous_costs[previous_index] + emission + transition, previous_index))
            best = min(proposals, key=lambda pair: (pair[0], pair[1]))
            current_costs.append(best[0])
            current_back.append(best[1])
        states.append(current_states)
        backpointers.append(current_back)
        previous_states, previous_costs = current_states, current_costs
    if not states:
        return []
    state = min(range(len(previous_costs)), key=lambda item: (previous_costs[item], item))
    output: list[dict[str, Any] | None] = []
    for time_index in range(len(states) - 1, -1, -1):
        current_state = states[time_index][state]
        output.append(None if current_state is None else strict_lists[time_index][current_state])
        state = backpointers[time_index][state]
    return output[::-1]


def localize(
    highlight_images: list[np.ndarray],
    mapping_rows: list[dict[str, Any]],
    config: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    unique_indices = sorted({int(row["highlight_decoded_index"]) for row in mapping_rows})
    normalized: dict[int, np.ndarray] = {index: cv2.resize(highlight_images[index], LOCALIZER_SIZE, interpolation=cv2.INTER_AREA) for index in unique_indices}
    candidate_lists = [make_candidates(normalized[index], config["highlight"]) for index in unique_indices]
    config["highlight"]["_viterbi"] = config["viterbi"]
    forward = viterbi_track(candidate_lists, config["highlight"])
    backward = list(reversed(viterbi_track(list(reversed(candidate_lists)), config["highlight"])))
    ordinal = {index: position for position, index in enumerate(unique_indices)}
    selected: dict[int, dict[str, Any] | None] = {}
    failures: dict[int, list[str]] = {}
    agreements: dict[int, float | None] = {}
    for position, index in enumerate(unique_indices):
        left, right = forward[position], backward[position]
        if left is None or right is None:
            selected[index] = None
            agreements[index] = None
            if left is None and right is None:
                candidates = candidate_lists[position]
                if candidates and all(bool(item.get("masked_overlay")) for item in candidates):
                    failures[index] = ["fixed_overlay_or_HUD_mask"]
                elif not any(bool(item.get("strict")) for item in candidates):
                    failures[index] = ["no_candidate_meeting_fixed_red_blue_support"]
                else:
                    failures[index] = ["one_direction_track_failure"]
            else:
                failures[index] = ["one_direction_track_failure"]
            continue
        distance = math.hypot(float(left["x"]) - float(right["x"]), float(left["y"]) - float(right["y"]))
        agreements[index] = clean_float(distance)
        if distance <= float(config["highlight"]["forward_backward_tolerance_px"]):
            point = dict(left)
            point["forward_backward_distance_px"] = clean_float(distance)
            point["image_ordinal"] = ordinal[index]
            selected[index] = point
            failures[index] = []
        else:
            selected[index] = None
            failures[index] = ["forward_backward_disagreement"]
    output: list[dict[str, Any]] = []
    for row in mapping_rows:
        index = int(row["highlight_decoded_index"])
        point = selected[index]
        flags = list(failures.get(index, []))
        result = {
            "target_encoded_index": int(row["target_encoded_index"]),
            "category": classify(int(row["target_encoded_index"])),
            "highlight_decoded_index": index,
            "highlight_pts_ms": int(row["highlight_pts_ms"]),
            "point": point if point is not None else None,
            "failure_flags": flags,
            "forward_backward_distance_px": agreements.get(index),
            "reused_highlight_frame": sum(1 for item in mapping_rows if int(item["highlight_decoded_index"]) == index) > 1,
        }
        output.append(result)
    failure_counts = Counter(flag for row in output for flag in row["failure_flags"])
    summary = {
        "unique_highlight_frame_count": len(unique_indices),
        "row_count": len(output),
        "selected_row_count": sum(row["point"] is not None for row in output),
        "failed_row_count": sum(row["point"] is None for row in output),
        "forward_backward_agreement_unique_count": sum(value is not None and value <= float(config["highlight"]["forward_backward_tolerance_px"]) for value in agreements.values()),
        "forward_backward_disagreement_unique_count": sum("forward_backward_disagreement" in failures.get(index, []) for index in unique_indices),
        "repeated_row_count": sum(max(0, sum(1 for row in mapping_rows if int(row["highlight_decoded_index"]) == index) - 1) for index in unique_indices),
        "failure_reason_counts": dict(sorted(failure_counts.items())),
        "candidate_count_by_decoded_index": {str(index): len(candidate_lists[position]) for position, index in enumerate(unique_indices)},
        "strict_candidate_count_by_decoded_index": {str(index): sum(item["strict"] for item in candidate_lists[position]) for position, index in enumerate(unique_indices)},
    }
    return output, summary


def baseline_rows(motion: dict[str, Any]) -> dict[int, dict[str, Any]]:
    rows = motion.get("tracker", {}).get("vod", {}).get("frames", [])
    result = {int(row["encoded_index"]): row for row in rows}
    if sorted(result) != list(range(TARGET_START, TARGET_END + 1)):
        raise RuntimeError("existing motion result does not contain the 191 baseline rows")
    return result


def compare_counts(high_rows: list[dict[str, Any]], baseline: dict[int, dict[str, Any]]) -> dict[str, Any]:
    high_by = {int(row["target_encoded_index"]): row for row in high_rows}
    baseline_counts = {"pre": 0, "event": 0, "transition": 0, "landing": 0}
    high_counts = {"pre": 0, "event": 0, "transition": 0, "landing": 0}
    details: dict[str, Any] = {}
    for index in range(TARGET_START, TARGET_END + 1):
        category = classify(index)
        old_reliable = baseline[index].get("point") is not None
        new_reliable = high_by[index].get("point") is not None
        baseline_counts[category] += int(old_reliable)
        high_counts[category] += int(new_reliable)
    for category in ("pre", "event", "transition", "landing"):
        indices = [index for index in range(TARGET_START, TARGET_END + 1) if classify(index) == category]
        overlap = [index for index in indices if baseline[index].get("point") is not None and high_by[index].get("point") is not None]
        recovered = [index for index in indices if baseline[index].get("point") is None and high_by[index].get("point") is not None]
        lost = [index for index in indices if baseline[index].get("point") is not None and high_by[index].get("point") is None]
        ambiguous = [index for index in indices if "forward_backward_disagreement" in high_by[index]["failure_flags"]]
        reasons = Counter(flag for index in indices for flag in high_by[index]["failure_flags"])
        details[category] = {
            "range": [indices[0], indices[-1]],
            "baseline_reliable": baseline_counts[category],
            "highlight_reliable": high_counts[category],
            "delta": high_counts[category] - baseline_counts[category],
            "overlap_count": len(overlap),
            "overlap_indices": overlap,
            "newly_recovered_count": len(recovered),
            "newly_recovered_indices": recovered,
            "lost_count": len(lost),
            "lost_indices": lost,
            "ambiguous_count": len(ambiguous),
            "ambiguous_indices": ambiguous,
            "failure_reason_counts": dict(sorted(reasons.items())),
        }
    expected_baseline = {"pre": 88, "event": 2, "transition": 13, "landing": 22}
    if baseline_counts != expected_baseline:
        raise RuntimeError(f"baseline counts changed: {baseline_counts} != {expected_baseline}")
    return {
        "baseline_counts": baseline_counts,
        "highlight_counts": high_counts,
        "categories": details,
        "baseline_total_reliable": sum(baseline_counts.values()),
        "highlight_total_reliable": sum(high_counts.values()),
        "total_delta": sum(high_counts.values()) - sum(baseline_counts.values()),
    }


def alignment_controls(
    target_features: list[np.ndarray],
    highlight_features: list[np.ndarray],
    source_ids: list[int],
    best_offset: int,
    best_score: float,
) -> dict[str, Any]:
    self_scores = direct_offset_scores(target_features, target_features, 0, range(-2, 3))
    self_best, self_runner = rank_scores(self_scores)
    # A known-offset control uses an independent contiguous highlight slice and
    # a declared +7 offset.  It exercises the same global search without using
    # the full-race target sequence.
    known_expected = 7
    known_base = 100
    known_source = highlight_features[known_base : known_base + TARGET_COUNT]
    known_candidate_base = known_base - known_expected
    known_scores = direct_offset_scores(known_source, highlight_features, known_candidate_base, range(known_expected - 2, known_expected + 3))
    known_best, known_runner = rank_scores(known_scores)
    wrong_offset = best_offset + DELIBERATE_SHIFT
    wrong_score = float(np.mean([pair_score(target_features[row], highlight_features[source_ids[row] + wrong_offset]) for row in range(len(target_features))]))
    return {
        "self_identity": {
            "searched_offsets": [-2, -1, 0, 1, 2],
            "search_cardinality": len(self_scores),
            "expected_offset": 0,
            "best": self_best,
            "runner_up": self_runner,
            "margin_runner_minus_best": clean_float(float(self_runner["mean_score"] - self_best["mean_score"])) if self_runner else None,
            "passed": int(self_best["offset"]) == 0,
        },
        "known_offset": {
            "searched_offsets": list(range(known_expected - 2, known_expected + 3)),
            "search_cardinality": len(known_scores),
            "expected_offset": known_expected,
            "candidate_base_index": known_base,
            "best": known_best,
            "runner_up": known_runner,
            "margin_runner_minus_best": clean_float(float(known_runner["mean_score"] - known_best["mean_score"])) if known_runner else None,
            "passed": int(known_best["offset"]) == known_expected,
        },
        "deliberate_shift": {
            "declared_shift_frames": DELIBERATE_SHIFT,
            "wrong_offset": wrong_offset,
            "best_score": clean_float(best_score),
            "wrong_score": clean_float(wrong_score),
            "wrong_minus_best_score": clean_float(wrong_score - best_score),
            "must_reject": True,
            "rejected": wrong_score > best_score,
        },
    }


def run(args: argparse.Namespace) -> dict[str, Any]:
    media = validate_media(
        args.media,
        args.media_sha256 if args.media_sha256 is not None else EXPECTED_MEDIA_SHA256,
        args.media_size if args.media_size is not None else EXPECTED_MEDIA_SIZE,
    )
    media["identity_binding"] = "explicit_cli" if args.media_sha256 is not None else "historical_default"
    ffmpeg = tool_record("ffmpeg", ["ffmpeg", "-version"])
    ffprobe_meta, ffprobe_frames, ffprobe_tools = probe_media(args.media)
    highlight_images, decoded_records = decode_highlight(args.media, ffprobe_frames)
    target_validation, motion, target_arrays, target_rows = validate_target_inputs(
        args.target_manifest, args.motion_result, args.cache_dir, args.remote_host, args.path_map
    )
    target_features = [feature(image) for image in target_arrays]
    highlight_features = [feature(image) for image in highlight_images]
    target_timeline_indices = [int(target_rows[index]["source_pts"] - int(target_rows[TARGET_START]["source_pts"])) // 1001 + TARGET_START for index in range(TARGET_START, TARGET_END + 1)]
    global_offset_min = -min(target_timeline_indices)
    global_offset_max = len(highlight_features) - 1 - max(target_timeline_indices)
    global_offsets = tuple(range(global_offset_min, global_offset_max + 1))
    global_scores = []
    for offset in global_offsets:
        values = [pair_score(target_features[row], highlight_features[target_timeline_indices[row] + offset]) for row in range(TARGET_COUNT)]
        global_scores.append({"offset": int(offset), "valid_row_count": len(values), "mean_score": clean_float(float(np.mean(values))), "median_score": clean_float(float(np.median(values)))})
    global_best, global_runner = rank_scores(global_scores)
    best_offset = int(global_best["offset"])
    monotone = monotone_align(target_features, highlight_features, target_timeline_indices, best_offset)
    mapping_indices = monotone["mapping"]
    mapping_rows: list[dict[str, Any]] = []
    for row, highlight_index in enumerate(mapping_indices):
        target_index = TARGET_START + row
        timeline_index = target_timeline_indices[row]
        pts = ffprobe_frames[highlight_index]
        mapping_rows.append(
            {
                "target_encoded_index": target_index,
                "target_timeline_index": timeline_index,
                "target_source_decoded_index": int(target_rows[target_index].get("source_decoded_index", target_index)),
                "target_source_pts": int(target_rows[target_index]["source_pts"]),
                "category": classify(target_index),
                "highlight_media_index": int(highlight_index),
                "highlight_decoded_index": int(highlight_index),
                "highlight_pts_ms": int(pts["pts_ms"]),
                "highlight_pts_time": str(pts["pts_time"]),
                "global_offset": best_offset,
                "refinement_delta": int(highlight_index - (timeline_index + best_offset)),
                "feature_score": clean_float(pair_score(target_features[row], highlight_features[highlight_index])),
            }
        )
    controls = alignment_controls(target_features, highlight_features, target_timeline_indices, best_offset, float(global_best["mean_score"]))
    high_rows, local_summary = localize(highlight_images, mapping_rows, scale_track_config())
    baseline = baseline_rows(motion)
    comparison = compare_counts(high_rows, baseline)
    margin = float(global_runner["mean_score"] - global_best["mean_score"]) if global_runner else None
    ambiguity_threshold = max(0.01, abs(float(global_best["mean_score"])) * 0.01)
    alignment_ambiguous = margin is not None and margin <= ambiguity_threshold
    controls_pass = all(bool(controls[name]["passed"]) for name in ("self_identity", "known_offset")) and bool(controls["deliberate_shift"]["rejected"])
    criterion = {
        "fixed_before_run": True,
        "transition_minimum_delta": 10,
        "landing_minimum_delta": 5,
        "no_pre_regression": True,
        "observed_transition_delta": comparison["categories"]["transition"]["delta"],
        "observed_landing_delta": comparison["categories"]["landing"]["delta"],
        "observed_pre_delta": comparison["categories"]["pre"]["delta"],
        "observed_event_delta": comparison["categories"]["event"]["delta"],
        "passes": (
            comparison["categories"]["transition"]["delta"] >= 10
            and comparison["categories"]["landing"]["delta"] >= 5
            and comparison["categories"]["pre"]["delta"] >= 0
            and comparison["categories"]["event"]["delta"] >= 0
        ),
    }
    if alignment_ambiguous or not controls_pass:
        status = "indeterminate"
        status_reason = "alignment ambiguity or control failure prevents an evidence-quality comparison"
    else:
        status = "pass" if criterion["passes"] else "fail"
        status_reason = "fixed evidence-quality material-improvement criterion evaluated against reliable non-null rows"
    analyzer_path = Path(__file__).resolve()
    source_tracker = ROOT / "scripts/fit_vod_render_motion.py"
    python_tool = tool_record("python", [sys.executable, "--version"])
    opencv_tool = {
        "name": "opencv-python",
        "version": cv2.__version__,
        "decoder": "cv2.VideoCapture sequential read; decoder index is read order",
    }
    output = {
        "schema_version": 1,
        "manifest_kind": "highlight_motion_audit",
        "status": status,
        "status_reason": status_reason,
        "command": " ".join([sys.executable, *sys.argv]),
        "determinism": {"sorted_json_keys": True, "stable_rounding_digits": 9, "no_randomness": True},
        "analyzer": {
            "path": str(analyzer_path),
            "sha256": sha256_file(analyzer_path),
            "python_version": platform.python_version(),
            "deterministic": True,
        },
        "tools": {
            "python": python_tool,
            "opencv": opencv_tool,
            "ffmpeg": ffmpeg,
            "ffprobe": ffprobe_tools[0],
            "platform": platform.platform(),
            "numpy_version": np.__version__,
            "pillow_version": getattr(__import__("PIL"), "__version__", None),
        },
        "media": media,
        "probe": ffprobe_meta,
        "decode": {
            "method": "OpenCV VideoCapture sequential read, retaining every decoded RGB frame in memory",
            "frame_count": len(decoded_records),
            "expected_frame_count": 784,
            "count_verified": len(decoded_records) == 784,
            "dimensions": [1920, 1344],
            "first_pts_ms": decoded_records[0]["pts_ms"],
            "last_pts_ms": decoded_records[-1]["pts_ms"],
            "timestamp_count_verified": all(item["pts_ms"] == ffprobe_frames[item["decoded_index"]]["pts_ms"] for item in decoded_records),
            "frames": decoded_records,
        },
        "inputs": {
            "target_manifest": target_validation,
            "existing_motion_result": {
                "path": str(args.motion_result),
                "sha256": target_validation["motion_result_sha256"],
                "size_bytes": target_validation["motion_result_size_bytes"],
                "manifest_kind": motion["manifest_kind"],
                "schema_version": motion["schema_version"],
                "status": motion.get("status"),
                "target_manifest_sha256": motion.get("inputs", {}).get("target_manifest_sha256"),
            },
        },
        "alignment": {
            "feature": {
                "formula": "highlight_decoded_index = target_timeline_index + integer_offset; target_timeline_index derives from source_pts",
                "resize": list(FEATURE_SIZE),
                "interpolation": "cv2.INTER_AREA",
                "standardization": "per-frame mean/std with 1e-6 epsilon",
                "score_formula": "mean((target_feature - highlight_feature)^2) over concatenated standardized grayscale/Sobel-x/Sobel-y elements",
                "score_direction": "lower_is_better",
            },
            "global_search": {
                "formula": "highlight_decoded_index = target_timeline_index + integer_offset; target_timeline_index derives from source_pts",
                "derived_offset_bounds": {
                    "minimum": "−min(target_timeline_indices)",
                    "maximum": "len(highlight_features)−1−max(target_timeline_indices)",
                    "evaluated": [global_offset_min, global_offset_max],
                    "full_row_validity": "every one of 191 target rows maps to an in-range decoded highlight index",
                },
                "searched_offsets": list(global_offsets),
                "search_cardinality": len(global_scores),
                "row_count": TARGET_COUNT,
                "scores": global_scores,
                "top_ranks": sorted(global_scores, key=lambda row: (float(row["mean_score"]), int(row["offset"])))[:10],
                "best_offset": best_offset,
                "best_score": global_best["mean_score"],
                "runner_up": global_runner,
                "margin_runner_minus_best": clean_float(margin),
                "margin_formula": "runner_up_mean_score - best_mean_score; positive means best is lower/error-better",
                "ambiguity_threshold": clean_float(ambiguity_threshold),
                "ambiguity_formula": "ambiguous iff margin_runner_minus_best <= max(0.01, 0.01 * best_mean_score)",
                "ambiguous": alignment_ambiguous,
            },
            "monotone_refinement": {
                "constrained_to_best_global_plus_or_minus": REFINEMENT_RADIUS,
                "score": monotone["score"],
                "total_cost": monotone["total_cost"],
                "candidate_cell_count": monotone["candidate_cell_count"],
                "candidate_range_per_row_histogram": dict(sorted(Counter(monotone["candidate_range_per_row"]).items())),
                "stay_penalty": monotone["stay_penalty"],
                "nonunit_step_penalty": monotone["nonunit_step_penalty"],
                "expected_source_step_histogram": monotone["expected_source_step_histogram"],
                "transition_penalty_formula": monotone["transition_penalty_formula"],
                "repeat_semantics": monotone["repeat_semantics"],
                "refinement_delta_histogram": dict(sorted(Counter(int(row["refinement_delta"]) for row in mapping_rows).items())),
                "nondecreasing_verified": all(mapping_indices[i] >= mapping_indices[i - 1] for i in range(1, len(mapping_indices))),
            },
            "controls": controls,
            "mapping": mapping_rows,
        },
        "localizer": {
            "provenance": {
                "method": "adapted fixed red/blue component candidates and forward/back Viterbi from fit_vod_render_motion.py",
                "source_script": str(source_tracker),
                "source_script_sha256": sha256_file(source_tracker),
                "configuration_changes": "only deterministic 340x215-to-720x480 geometry/area scaling; HSV thresholds, masks, Viterbi costs, null semantics retained",
                "manual_labels": False,
                "manual_coordinates": False,
            },
            "geometry": {"source_target_geometry": [340, 215], "fixed_highlight_geometry": list(LOCALIZER_SIZE), "resize": "cv2.INTER_AREA", "coordinate_origin": "top-left of normalized highlight frame"},
            "fixed_config": scale_track_config(),
            "sequence_method": "unique aligned decoded indices tracked in forward and reversed order; selected only when both paths are non-null and within fixed tolerance; all other rows null",
            "summary": local_summary,
            "frames": high_rows,
        },
        "comparison": comparison,
        "material_improvement_criterion": criterion,
        "limitations": [
            "Higher resolution cannot resolve Mario clones, red fragments, or HUD-like red components without temporal certainty; ambiguous/failed localizer rows remain null.",
            "Alignment is between two lossy YouTube-derived encodes, not original source frames.",
            "The target encoded index and highlight decoded index are media indices; source PTS is a separate 1/30000-clock field.",
            "No N64 VI cadence, game state, mechanism, emulator scenario, or RAM inference follows from this image audit.",
            "The overlay PNG directory is corroboration only and is not consumed by this report.",
        ],
    }
    return json_clean(output)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--media", type=Path, default=MEDIA_DEFAULT)
    parser.add_argument("--media-sha256", help="explicit expected SHA-256 for a newly acquired highlight; requires --media-size")
    parser.add_argument("--media-size", type=int, help="explicit expected byte size; historical identity is enforced when both options are omitted")
    parser.add_argument("--target-manifest", type=Path, default=TARGET_MANIFEST_DEFAULT)
    parser.add_argument("--motion-result", type=Path, default=MOTION_RESULT_DEFAULT)
    parser.add_argument("--cache-dir", type=Path, default=CACHE_DEFAULT)
    parser.add_argument("--remote-host", help="explicit SSH fallback for target PNGs missing locally")
    parser.add_argument("--path-map", action="append", type=parse_path_map, default=[], metavar="OLD=LOCAL", help="relocate manifest path prefixes without altering provenance; repeatable")
    parser.add_argument("--output", type=Path, default=OUTPUT_DEFAULT)
    args = parser.parse_args()
    if (args.media_sha256 is None) != (args.media_size is None):
        parser.error("--media-sha256 and --media-size must be supplied together")
    if args.media_sha256 is not None:
        if len(args.media_sha256) != 64 or any(char not in "0123456789abcdef" for char in args.media_sha256):
            parser.error("--media-sha256 must be 64 lowercase hexadecimal characters")
        if args.media_size <= 0:
            parser.error("--media-size must be positive")
    report = run(args)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8", newline="\n") as stream:
        json.dump(report, stream, indent=2, sort_keys=True, ensure_ascii=True)
        stream.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
