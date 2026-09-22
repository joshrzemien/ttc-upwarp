#!/usr/bin/env python3
"""Fit decoded VOD crops against emulator-rendered scene images.

This is a scene/camera comparison only: it does not infer Mario RAM or a
mechanism. Every PNG named by the input manifests is fetched to a temporary
cache and verified before use. A finite geometric and temporal grid is scored
with normalized-luma and gradient features.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
import sys
from pathlib import Path
from typing import Any, Iterable

import numpy as np
from PIL import Image

from extract_vod_fit_targets import parse_path_map, read_asset_bytes

ROOT = Path(__file__).resolve().parents[1]
TARGET_MANIFEST = ROOT / "results" / "vod_fit_targets.json"
RENDER_MANIFEST = ROOT / "results" / "vod_fit_render_manifest.json"
DEFAULT_OUTPUT = ROOT / "media" / "vod_render_scene_fit.json"
DEFAULT_CACHE = Path("/tmp") / "ttc-vod-scene-png-cache"
SCENARIOS = ("reachable_no_mutation", "synthetic_no_flip", "synthetic_bit_clear")
TARGET_START = 340
TARGET_END = 530
TARGET_COUNT = TARGET_END - TARGET_START + 1
EMULATOR_FIRST_VI = 1
EMULATOR_LAST_VI = 280
EVENT_INDICES = (431, 432)
EVENT_REFERENCE_INDEX = 431
PLACEMENT_MARKER_VI = 100
MUTATION_MARKER_VI = 101
MIN_PRE_EVENT_FRAMES = 30
MIN_POST_EVENT_FRAMES = 30
CONTROL_WINDOW_LENGTH = 30
CONTROL_ADJACENT_SHIFT = 1
CONTROL_WRONG_SHIFT = 180
CONTROL_POSITIVE_START_VI = 140
CONTROL_WRONG_START_VI = 1
FEATURE_WIDTH = 48
CROSS_DOMAIN_POSITIVE_CONTROL_AVAILABLE = False
FEATURE_HEIGHT = 30
FEATURE_SIZE = (FEATURE_WIDTH, FEATURE_HEIGHT)
BLACK_ROW_PREVALENCE = 0.25
# These are emulator VI / VOD encoded-frame slopes.  2 is the nominal
# 60-Hz-emulator-VI to 30-fps-VOD relation.  Nearby exact rationals test clock
# drift without pretending that frame numbers are VIs.
TEMPORAL_SLOPES = (
    (1, 1), (6, 5), (5, 4), (4, 3), (3, 2),
    (5, 3), (7, 4), (9, 5), (2, 1),
)


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def finite(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def json_float(value: float) -> float:
    value = float(value)
    if not math.isfinite(value):
        raise ValueError(f"non-finite score {value!r}")
    return round(value, 8)


def percentile(values: Iterable[float], q: float) -> float:
    array = np.asarray(list(values), dtype=np.float64)
    if array.size == 0 or not np.isfinite(array).all():
        raise AssertionError("empty or non-finite control scores")
    return float(np.percentile(array, q, method="linear"))


def similarity01(dot: float) -> float:
    """Map a cosine similarity from [-1, 1] to the candidate [0, 1] scale."""
    value = (float(dot) + 1.0) / 2.0
    if not math.isfinite(value):
        raise AssertionError(f"non-finite similarity {dot!r}")
    return value


def ranges(indices: Iterable[int]) -> list[list[int]]:
    ordered = sorted(set(int(value) for value in indices))
    result: list[list[int]] = []
    for value in ordered:
        if not result or value != result[-1][1] + 1:
            result.append([value, value])
        else:
            result[-1][1] = value
    return result


def decode_png(raw: bytes, expected_width: int, expected_height: int, label: str) -> np.ndarray:
    with Image.open(io.BytesIO(raw)) as image:
        image.load()
        if image.size != (expected_width, expected_height):
            raise AssertionError(f"{label}: dimensions {image.size} != {(expected_width, expected_height)}")
        if image.mode not in {"RGB", "RGBA", "P", "L"}:
            raise AssertionError(f"{label}: unsupported PNG mode {image.mode}")
        return np.asarray(image.convert("RGB"), dtype=np.uint8).copy()


def cache_path(cache_dir: Path, namespace: str, remote_path: str) -> Path:
    token = hashlib.sha256(remote_path.encode("utf-8")).hexdigest()
    return cache_dir / namespace / f"{token}.png"


def fetch_verified_png(
    *, host: str | None, remote_path: str, expected_sha256: str, expected_size: int,
    expected_width: int, expected_height: int, cache_file: Path,
    path_maps: Iterable[tuple[Path, Path]] = (),
) -> tuple[np.ndarray, dict[str, Any]]:
    """Load one manifest-listed PNG locally, from verified cache, or explicit SSH."""
    raw: bytes | None = None
    source_record: dict[str, Any] = {"transport": "cache", "path": str(cache_file)}
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    if cache_file.exists():
        candidate = cache_file.read_bytes()
        if len(candidate) == expected_size and sha256_bytes(candidate) == expected_sha256:
            raw = candidate
        else:
            cache_file.unlink()
    if raw is None:
        raw, source_record = read_asset_bytes(remote_path, host, path_maps)
        if len(raw) != expected_size or sha256_bytes(raw) != expected_sha256:
            raise AssertionError(
                f"{remote_path}: fetched bytes fail manifest hash/size "
                f"{sha256_bytes(raw)}/{len(raw)} != {expected_sha256}/{expected_size}"
            )
        cache_file.write_bytes(raw)
    actual_sha256 = sha256_bytes(raw)
    actual_size = len(raw)
    if actual_sha256 != expected_sha256 or actual_size != expected_size:
        raise AssertionError(
            f"{remote_path}: cache bytes fail manifest hash/size "
            f"{actual_sha256}/{actual_size} != {expected_sha256}/{expected_size}"
        )
    pixels = decode_png(raw, expected_width, expected_height, remote_path)
    return pixels, {
        "remote_path": remote_path, "cache_path": str(cache_file),
        "source": source_record,
        "sha256": actual_sha256, "size_bytes": actual_size,
        "width": expected_width, "height": expected_height,
        "hash_verified": True, "dimensions_verified": True,
    }


def load_inputs(
    cache_dir: Path,
    target_path: Path = TARGET_MANIFEST,
    render_path: Path = RENDER_MANIFEST,
    remote_host: str | None = None,
    path_maps: Iterable[tuple[Path, Path]] = (),
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Load and hash-check every target and every unique rendered-row PNG."""
    target_raw = target_path.read_bytes()
    render_raw = render_path.read_bytes()
    target_manifest = json.loads(target_raw)
    render_manifest = json.loads(render_raw)
    if target_manifest.get("schema_version") != 1 or render_manifest.get("schema_version") != 1:
        raise AssertionError("unsupported input manifest schema")
    if target_manifest.get("manifest_kind") != "vod_fit_targets":
        raise AssertionError("unexpected target manifest kind")
    config = target_manifest["config"]
    if config["start_index"] != TARGET_START or config["post_end_index"] != TARGET_END:
        raise AssertionError("target manifest index range changed")
    target_rows = target_manifest["targets"]
    if len(target_rows) != TARGET_COUNT:
        raise AssertionError(f"expected {TARGET_COUNT} VOD rows, got {len(target_rows)}")
    if [int(row["encoded_index"]) for row in target_rows] != list(range(TARGET_START, TARGET_END + 1)):
        raise AssertionError("VOD encoded indices are not exactly contiguous 340..530")
    if target_manifest["crop"]["width"] != 340 or target_manifest["crop"]["height"] != 215:
        raise AssertionError("unexpected target crop dimensions")

    target_pixels: dict[int, np.ndarray] = {}
    target_records: dict[str, dict[str, Any]] = {}
    for row in target_rows:
        encoded = int(row["encoded_index"])
        filename = str(row["png_filename"])
        expected = target_manifest["remote_pngs"].get(filename)
        if not isinstance(expected, dict):
            raise AssertionError(f"missing remote PNG metadata for {filename}")
        remote_path = str(row["remote_png_path"])
        if int(row["width"]) != 340 or int(row["height"]) != 215:
            raise AssertionError(f"{filename}: row dimensions changed")
        pixels, record = fetch_verified_png(
            host=remote_host, remote_path=remote_path, path_maps=path_maps,
            expected_sha256=str(expected["sha256"]), expected_size=int(expected["size_bytes"]),
            expected_width=340, expected_height=215, cache_file=cache_path(cache_dir, "vod", remote_path),
        )
        if record["sha256"] != row["png_sha256"] or record["size_bytes"] != int(row["png_size_bytes"]):
            raise AssertionError(f"{filename}: row and remote_pngs metadata disagree")
        target_pixels[encoded] = pixels
        target_records[filename] = record

    scenario_pixels: dict[str, dict[str, np.ndarray]] = {}
    scenario_records: dict[str, dict[str, dict[str, Any]]] = {}
    scenario_rows: dict[str, list[dict[str, Any]]] = {}
    for scenario in SCENARIOS:
        info = render_manifest["scenarios"].get(scenario)
        if not isinstance(info, dict):
            raise AssertionError(f"missing scenario {scenario}")
        rows = info.get("render_rows")
        if not isinstance(rows, list) or len(rows) != EMULATOR_LAST_VI:
            raise AssertionError(f"{scenario}: expected 280 render rows")
        if [int(row["vi"]) for row in rows] != list(range(1, EMULATOR_LAST_VI + 1)):
            raise AssertionError(f"{scenario}: VIs are not contiguous 1..280")
        scenario_rows[scenario] = rows
        scenario_pixels[scenario] = {}
        scenario_records[scenario] = {}
        for row in rows:
            image = row["image"]
            remote_path = str(image["path"])
            if int(image["width"]) != 640 or int(image["height"]) != 480 or image.get("mode") != "RGB":
                raise AssertionError(f"{scenario} VI{row['vi']}: unexpected PNG metadata")
            if remote_path not in scenario_pixels[scenario]:
                pixels, record = fetch_verified_png(
                    host=remote_host, remote_path=remote_path, path_maps=path_maps,
                    expected_sha256=str(image["sha256"]), expected_size=int(image["size_bytes"]),
                    expected_width=640, expected_height=480, cache_file=cache_path(cache_dir, scenario, remote_path),
                )
                scenario_pixels[scenario][remote_path] = pixels
                scenario_records[scenario][remote_path] = record
            else:
                record = scenario_records[scenario][remote_path]
                if record["sha256"] != image["sha256"] or record["size_bytes"] != int(image["size_bytes"]):
                    raise AssertionError(f"{scenario} VI{row['vi']}: reused image metadata mismatch")

    input_hashes = {
        "target_manifest_sha256": sha256_bytes(target_raw), "render_manifest_sha256": sha256_bytes(render_raw),
        "target_config_sha256": str(target_manifest["config_sha256"]), "target_png_count": len(target_records),
        "scenario_unique_png_counts": {scenario: len(scenario_records[scenario]) for scenario in SCENARIOS},
        "all_consumed_pngs_hash_verified": True, "all_consumed_pngs_dimensions_verified": True,
    }
    data = {
        "target_manifest": target_manifest, "render_manifest": render_manifest,
        "target_manifest_path": str(target_path.resolve()), "render_manifest_path": str(render_path.resolve()),
        "remote_host": remote_host,
        "target_rows": target_rows, "target_pixels": target_pixels, "target_records": target_records,
        "scenario_rows": scenario_rows, "scenario_pixels": scenario_pixels,
        "scenario_records": scenario_records, "input_hashes": input_hashes,
    }
    return target_manifest, render_manifest, data


def detect_fixed_mask(scenario_pixels: dict[str, dict[str, np.ndarray]]) -> dict[str, Any]:
    """Infer common black border/overlay rows and columns from decoded pixels only."""
    arrays = [pixels for records in scenario_pixels.values() for pixels in records.values()]
    if not arrays:
        raise AssertionError("no emulator PNGs for fixed mask detection")
    height, width = arrays[0].shape[:2]
    row_all_black = np.zeros((len(arrays), height), dtype=np.bool_)
    col_all_black = np.zeros((len(arrays), width), dtype=np.bool_)
    for index, array in enumerate(arrays):
        if array.shape != (height, width, 3):
            raise AssertionError("emulator image dimensions differ during mask detection")
        black = np.all(array == 0, axis=2)
        row_all_black[index] = black.all(axis=1); col_all_black[index] = black.all(axis=0)
    row_prevalence = row_all_black.mean(axis=0); col_prevalence = col_all_black.mean(axis=0)
    masked_rows = np.flatnonzero(row_prevalence >= BLACK_ROW_PREVALENCE).tolist(); masked_cols = np.flatnonzero(col_prevalence >= BLACK_ROW_PREVALENCE).tolist()
    top = 0
    while top in masked_rows: top += 1
    bottom = height
    while bottom - 1 in masked_rows: bottom -= 1
    left = 0
    while left in masked_cols: left += 1
    right = width
    while right - 1 in masked_cols: right -= 1
    if not (0 <= left < right <= width and 0 <= top < bottom <= height):
        raise AssertionError("fixed mask removed the whole emulator image")
    return {
        "method": "decoded RGB rows/columns all-black in >=25% of all unique scenario PNGs",
        "sample_image_count": len(arrays), "prevalence_threshold": BLACK_ROW_PREVALENCE,
        "masked_row_ranges": ranges(masked_rows), "masked_column_ranges": ranges(masked_cols),
        "active_viewport": {"x": left, "y": top, "width": right - left, "height": bottom - top},
        "row_black_prevalence": [round(float(value), 6) for value in row_prevalence],
        "column_black_prevalence": [round(float(value), 6) for value in col_prevalence],
    }


def normalize_vector(values: np.ndarray) -> np.ndarray:
    values = np.asarray(values, dtype=np.float32); values = values - np.float32(values.mean()); norm = float(np.linalg.norm(values))
    if norm <= 1.0e-7: return np.zeros(values.shape, dtype=np.float32)
    return (values / np.float32(norm)).astype(np.float32)


def feature_for_crop(crop: np.ndarray) -> dict[str, np.ndarray]:
    image = Image.fromarray(np.asarray(crop, dtype=np.uint8), mode="RGB").resize(FEATURE_SIZE, Image.Resampling.BICUBIC)
    raster = np.asarray(image, dtype=np.float32) / np.float32(255.0)
    luma = 0.2126 * raster[:, :, 0] + 0.7152 * raster[:, :, 1] + 0.0722 * raster[:, :, 2]
    luma_feature = normalize_vector(luma.reshape(-1)); gx = np.gradient(luma, axis=1); gy = np.gradient(luma, axis=0)
    gradient_feature = normalize_vector(np.concatenate((gx.reshape(-1), gy.reshape(-1))))
    combined = np.concatenate((np.sqrt(np.float32(0.58)) * luma_feature, np.sqrt(np.float32(0.42)) * gradient_feature)).astype(np.float32)
    norm = float(np.linalg.norm(combined))
    if norm > 1.0e-7: combined /= np.float32(norm)
    return {"combined": combined, "luma": luma_feature, "gradient": gradient_feature}


def batch_features(features: list[dict[str, np.ndarray]]) -> dict[str, np.ndarray]:
    return {key: np.stack([feature[key] for feature in features]).astype(np.float32) for key in ("combined", "luma", "gradient")}


def crop_active(array: np.ndarray, viewport: dict[str, int]) -> np.ndarray:
    x, y = int(viewport["x"]), int(viewport["y"]); width, height = int(viewport["width"]), int(viewport["height"])
    return array[y:y + height, x:x + width]


def transform_family(viewport: dict[str, int], target_width: int, target_height: int) -> list[dict[str, Any]]:
    width, height = int(viewport["width"]), int(viewport["height"]); target_ratio = target_width / target_height; transforms: list[dict[str, Any]] = []
    for crop_width in (480, 560, 640):
        crop_height = int(round(crop_width / target_ratio))
        if crop_width > width or crop_height > height: continue
        for x_name, x_anchor in (("left", 0.0), ("center", 0.5), ("right", 1.0)):
            crop_x = int(round((width - crop_width) * x_anchor))
            for y_name, y_anchor in (("top", 0.0), ("center", 0.5), ("bottom", 1.0)):
                crop_y = int(round((height - crop_height) * y_anchor))
                transforms.append({"id": f"w{crop_width}_h{crop_height}_x{x_name}_y{y_name}", "crop_width": crop_width, "crop_height": crop_height, "crop_x": crop_x, "crop_y": crop_y, "x_anchor": x_name, "y_anchor": y_name, "aspect_ratio": f"{crop_width}:{crop_height}", "resize": {"width": target_width, "height": target_height, "resampling": "bicubic"}})
    if len(transforms) != 27: raise AssertionError(f"unexpected transform family size {len(transforms)}")
    return transforms


def transformed_feature(array: np.ndarray, viewport: dict[str, int], transform: dict[str, Any]) -> dict[str, np.ndarray]:
    active = crop_active(array, viewport); x, y = int(transform["crop_x"]), int(transform["crop_y"]); width, height = int(transform["crop_width"]), int(transform["crop_height"]); crop = active[y:y + height, x:x + width]
    if crop.shape[:2] != (height, width): raise AssertionError(f"transform crop out of bounds: {transform}")
    return feature_for_crop(crop)


def canonical_source_feature(array: np.ndarray, viewport: dict[str, int]) -> dict[str, np.ndarray]:
    return feature_for_crop(crop_active(array, viewport))


def score_matrices(target_batch: dict[str, np.ndarray], source_batch: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
    matrices = {}
    for key in ("combined", "luma", "gradient"):
        matrix = target_batch[key] @ source_batch[key].T
        if not np.isfinite(matrix).all(): raise AssertionError(f"non-finite {key} score matrix")
        matrices[key] = ((matrix + np.float32(1.0)) / np.float32(2.0)).astype(np.float32)
    return matrices


def target_controls(target_pixels: dict[int, np.ndarray], target_batch: dict[str, np.ndarray]) -> dict[str, Any]:
    positive: list[float] = []
    negative: list[float] = []
    perturb_features: list[dict[str, np.ndarray]] = []
    for index in range(TARGET_START, TARGET_END + 1):
        source = target_pixels[index]
        cropped = source[2:-2, 3:-3]
        small_size = (
            max(8, int(round(cropped.shape[1] * 0.87))),
            max(8, int(round(cropped.shape[0] * 0.87))),
        )
        small = Image.fromarray(cropped, mode="RGB").resize(small_size, Image.Resampling.BILINEAR)
        restored = small.resize((source.shape[1], source.shape[0]), Image.Resampling.BICUBIC)
        jpeg_buffer = io.BytesIO()
        restored.save(jpeg_buffer, format="JPEG", quality=68, optimize=False, progressive=False, subsampling=2)
        with Image.open(io.BytesIO(jpeg_buffer.getvalue())) as decoded:
            perturb_features.append(feature_for_crop(np.asarray(decoded.convert("RGB"), dtype=np.uint8).copy()))
    perturb_batch = batch_features(perturb_features)
    for row_index in range(TARGET_COUNT):
        positive.append(similarity01(float(target_batch["combined"][row_index] @ perturb_batch["combined"][row_index])))
    # Do not wrap the sequence: every negative pair is exactly +53 encoded
    # frames later, with no reverse-end artifact.
    for row_index in range(TARGET_COUNT - 53):
        negative.append(similarity01(float(target_batch["combined"][row_index] @ target_batch["combined"][row_index + 53])))
    def stats(values: list[float]) -> dict[str, Any]:
        return {
            "min": json_float(min(values)), "p05": json_float(percentile(values, 5)),
            "median": json_float(percentile(values, 50)), "p95": json_float(percentile(values, 95)),
            "max": json_float(max(values)),
        }
    return {
        "positive_control": {
            "name": "VOD self-alignment with fixed crop/scale and JPEG-quality-68 perturbation",
            "count": len(positive), "scores": stats(positive),
        },
        "negative_control": {
            "name": "VOD deliberately wrong temporal pair exactly +53 encoded frames (non-wrapping)",
            "count": len(negative), "scores": stats(negative),
        },
    }


def emulator_controls(scenario_rows: dict[str, list[dict[str, Any]]], scenario_pixels: dict[str, dict[str, np.ndarray]], viewport: dict[str, int]) -> tuple[dict[str, Any], dict[str, dict[str, np.ndarray]]]:
    """Calibrate adjacent/correct and deliberately wrong *windows*.

    Individual reused rows can be identical far apart in a capture. A fixed
    30-frame window and a fixed +180-VI displacement make the negative
    control a deliberate wrong-time window rather than a nearest repeated PNG.
    All controls use the same cosine-to-[0,1] scale as candidate scores.
    """
    positive: list[float] = []
    negative: list[float] = []
    canonical: dict[str, dict[str, np.ndarray]] = {}
    for scenario in SCENARIOS:
        rows = scenario_rows[scenario]
        by_path = scenario_pixels[scenario]
        unique_features = {path: canonical_source_feature(array, viewport) for path, array in by_path.items()}
        canonical[scenario] = batch_features([unique_features[str(row["image"]["path"])] for row in rows])
        features = canonical[scenario]["combined"]
        for start in range(CONTROL_POSITIVE_START_VI, EMULATOR_LAST_VI - CONTROL_WINDOW_LENGTH - CONTROL_ADJACENT_SHIFT + 2):
            left = features[start - 1:start - 1 + CONTROL_WINDOW_LENGTH]
            right = features[start - 1 + CONTROL_ADJACENT_SHIFT:start - 1 + CONTROL_ADJACENT_SHIFT + CONTROL_WINDOW_LENGTH]
            positive.append(float(np.mean((np.sum(left * right, axis=1) + 1.0) / 2.0)))
        for start in range(CONTROL_WRONG_START_VI, EMULATOR_LAST_VI - CONTROL_WRONG_SHIFT - CONTROL_WINDOW_LENGTH + 2):
            left = features[start - 1:start - 1 + CONTROL_WINDOW_LENGTH]
            right = features[start - 1 + CONTROL_WRONG_SHIFT:start - 1 + CONTROL_WRONG_SHIFT + CONTROL_WINDOW_LENGTH]
            negative.append(float(np.mean((np.sum(left * right, axis=1) + 1.0) / 2.0)))
    def stats(values: list[float]) -> dict[str, Any]:
        return {"min": json_float(min(values)), "p05": json_float(percentile(values, 5)), "median": json_float(percentile(values, 50)), "p95": json_float(percentile(values, 95)), "max": json_float(max(values))}
    return {
        "positive_control": {
            "name": "same-scenario adjacent/correct 30-frame windows after fixed VI140 warm-up (start to start+1 VI; reused images included)",
            "count": len(positive), "window_length": CONTROL_WINDOW_LENGTH, "shift_vi": CONTROL_ADJACENT_SHIFT,
            "start_vi_range": [CONTROL_POSITIVE_START_VI, EMULATOR_LAST_VI - CONTROL_WINDOW_LENGTH - CONTROL_ADJACENT_SHIFT + 1],
            "start_range_reason": "fixed common VI140 warm-up boundary chosen before scenario ranking to exclude startup/overlay rows; identical for all scenarios",
            "score_scale": "cosine mapped from [-1,1] to [0,1], same as candidate scores",
            "scores": stats(positive),
        },
        "negative_control": {
            "name": "same-scenario deliberately wrong 30-frame windows (start to start+180 VI)",
            "count": len(negative), "window_length": CONTROL_WINDOW_LENGTH, "shift_vi": CONTROL_WRONG_SHIFT,
            "start_vi_range": [CONTROL_WRONG_START_VI, EMULATOR_LAST_VI - CONTROL_WRONG_SHIFT - CONTROL_WINDOW_LENGTH + 1],
            "score_scale": "cosine mapped from [-1,1] to [0,1], same as candidate scores",
            "scores": stats(negative),
        },
    }, canonical


def paired_synthetic_controls(scenario_rows: dict[str, list[dict[str, Any]]], canonical: dict[str, dict[str, np.ndarray]]) -> dict[str, Any]:
    no_flip_rows = scenario_rows["synthetic_no_flip"]
    bit_clear_rows = scenario_rows["synthetic_bit_clear"]
    exact_pre: list[int] = []
    exact_post: list[int] = []
    post_scores: list[float] = []
    for index, (no_flip, bit_clear) in enumerate(zip(no_flip_rows, bit_clear_rows)):
        vi = int(no_flip["vi"])
        same_hash = no_flip["image"]["sha256"] == bit_clear["image"]["sha256"]
        if vi <= 99 and same_hash:
            exact_pre.append(vi)
        if vi >= 100 and same_hash:
            exact_post.append(vi)
        post_scores.append(similarity01(float(canonical["synthetic_no_flip"]["combined"][index] @ canonical["synthetic_bit_clear"]["combined"][index])))
    first_visual_divergence = next((vi for vi in range(100, 281) if vi not in exact_post), None)
    if exact_pre != list(range(1, 100)):
        raise AssertionError("paired controls do not have exactly 99 pre-event equal rows")
    if first_visual_divergence is None:
        raise AssertionError("paired synthetic controls never visually diverge")
    post_after = [post_scores[vi - 1] for vi in range(first_visual_divergence, 281)]
    if not post_after or not all(finite(score) for score in post_after):
        raise AssertionError("paired post-mutation scores are not finite")
    return {
        "pre_event_definition": "VI <= 99, before declared placement/mutation marker VI100/VI101",
        "pre_event_exact_equal_count": len(exact_pre),
        "pre_event_exact_equal_vi_range": [1, 99],
        "post_marker_exact_equal_count": len(exact_post),
        "post_marker_exact_equal_vi_range": ranges(exact_post),
        "first_visual_divergence_vi": first_visual_divergence,
        "declared_mutation_vi": 101,
        "post_divergence_feature_score": {
            "count": len(post_after), "min": json_float(min(post_after)),
            "median": json_float(percentile(post_after, 50)),
            "p95": json_float(percentile(post_after, 95)), "max": json_float(max(post_after)),
        },
        "score_scale": "cosine mapped from [-1,1] to [0,1], same as candidate scores",
        "hash_equality_checked_for_all_rows": True,
    }
def target_source_frame_offsets(target_rows: list[dict[str, Any]]) -> tuple[list[int], dict[str, Any]]:
    """Convert target source PTS to exact integer source-frame offsets.

    The target cadence has one 2002-PTS interval at encoded 478->479. This
    function therefore never substitutes encoded-index subtraction.
    """
    rows_by_index = {int(row["encoded_index"]): row for row in target_rows}
    if set(rows_by_index) != set(range(TARGET_START, TARGET_END + 1)):
        raise AssertionError("target rows do not cover the declared encoded range")
    event_pts = int(rows_by_index[EVENT_REFERENCE_INDEX]["source_pts"])
    offsets: list[int] = []
    deltas: list[int] = []
    for encoded in range(TARGET_START, TARGET_END + 1):
        diff = int(rows_by_index[encoded]["source_pts"]) - event_pts
        quotient, remainder = divmod(diff, 1001)
        if remainder != 0:
            raise AssertionError(f"encoded {encoded}: source PTS diff {diff} is not integral 1001-frame offset")
        offsets.append(quotient)
        if encoded > TARGET_START:
            deltas.append(int(rows_by_index[encoded]["source_pts"]) - int(rows_by_index[encoded - 1]["source_pts"]))
    if offsets[EVENT_REFERENCE_INDEX - TARGET_START] != 0:
        raise AssertionError("event source-frame offset is not zero")
    breaks = []
    for encoded in range(TARGET_START + 1, TARGET_END + 1):
        delta = int(rows_by_index[encoded]["source_pts"]) - int(rows_by_index[encoded - 1]["source_pts"])
        if delta != 1001:
            breaks.append({
                "from_encoded_index": encoded - 1,
                "to_encoded_index": encoded,
                "pts_delta": delta,
                "source_frame_delta": delta // 1001 if delta % 1001 == 0 else None,
            })
    return offsets, {
        "basis": "exact target source_pts relative to encoded event index 431",
        "event_encoded_index": EVENT_REFERENCE_INDEX,
        "event_source_pts": event_pts,
        "pts_timebase": "1/30000",
        "nominal_pts_per_source_frame": 1001,
        "offset_range": [min(offsets), max(offsets)],
        "offset_at_event_indices": {str(index): offsets[index - TARGET_START] for index in EVENT_INDICES},
        "source_pts_delta_histogram": {str(delta): deltas.count(delta) for delta in sorted(set(deltas))},
        "non_nominal_delta_breaks": breaks,
        "encoded_index_gap_not_treated_as_source_frame": True,
    }

def calibrate(target_control: dict[str, Any], emulator_control: dict[str, Any]) -> dict[str, Any]:
    positive_envelope = min(
        target_control["positive_control"]["scores"]["p05"],
        emulator_control["positive_control"]["scores"]["p05"],
    )
    negative_envelope = max(
        target_control["negative_control"]["scores"]["p95"],
        emulator_control["negative_control"]["scores"]["p95"],
    )
    margin = positive_envelope - negative_envelope
    separated = bool(margin > 0.0)
    return {
        "score_scale": "all candidate and control scores use cosine mapped from [-1,1] to [0,1]",
        "positive_control_p05_envelope": json_float(positive_envelope),
        "negative_control_p95_envelope": json_float(negative_envelope),
        "control_margin": json_float(margin),
        "separated": separated,
        "cross_domain_positive_control_available": CROSS_DOMAIN_POSITIVE_CONTROL_AVAILABLE,
        "cross_domain_positive_control_absent": not CROSS_DOMAIN_POSITIVE_CONTROL_AVAILABLE,
        "known_positive_vod_to_glide64mk2_pair": None,
        "interpretation": (
            "within-domain self/wrong-time controls separate, but no known-positive VOD-to-Glide64mk2 pair calibrates renderer/lighting/texture domain transfer; low cross-domain scores cannot distinguish domain shift from scene/camera mismatch"
        ),
        "criterion": {
            "strict_within_domain_pass_requires": [
                "at least 30 mapped pre-event and 30 mapped post-event VOD frames",
                "observed weighted score >= positive-control p05 envelope",
                "observed weighted score > wrong-time negative-control p95 envelope",
                "finite components and deterministic ranking",
            ],
            "fit_classification_requires": "a known-positive cross-domain calibration pair in addition to strict within-domain criteria",
            "rejected_when": "not used without cross-domain positive calibration",
            "indeterminate_when": "cross-domain positive calibration is absent or positive/negative control envelopes overlap",
        },
        "calibration_scope": "VOD self perturbations plus fixed-window same-scenario adjacent/wrong-time controls; synthetic mutation is reported separately, not pooled as wrong-time control",
    }


def round_half_up_fraction(numerator: int, denominator: int) -> int:
    if numerator >= 0: return (numerator + denominator // 2) // denominator
    return -((-numerator + denominator // 2) // denominator)


def mapping_series(num: int, den: int, event_vi: int, source_offsets: list[int]) -> list[int]:
    return [
        event_vi + round_half_up_fraction(num * source_offset, den)
        for source_offset in source_offsets
    ]
def unique_mapping_positions(vis: list[int], valid: list[bool], source_identities: list[str]) -> list[int]:
    """Return first target positions for each unique emitted screenshot identity."""
    seen: set[str] = set()
    positions: list[int] = []
    for position, (vi, is_valid) in enumerate(zip(vis, valid)):
        if not is_valid:
            continue
        identity = source_identities[vi - 1]
        if identity in seen:
            continue
        seen.add(identity)
        positions.append(position)
    return positions


def mapping_grid(source_offsets: list[int], source_identities: list[str]) -> list[dict[str, Any]]:
    if len(source_offsets) != TARGET_COUNT or source_offsets[EVENT_REFERENCE_INDEX - TARGET_START] != 0:
        raise AssertionError("source-offset vector does not match target event")
    if len(source_identities) != EMULATOR_LAST_VI:
        raise AssertionError("source identity vector does not cover all emulator VIs")
    encoded_indices = list(range(TARGET_START, TARGET_END + 1))
    mappings: list[dict[str, Any]] = []
    for num, den in TEMPORAL_SLOPES:
        for event_vi in range(EMULATOR_FIRST_VI, EMULATOR_LAST_VI + 1):
            vis = mapping_series(num, den, event_vi, source_offsets)
            valid = [EMULATOR_FIRST_VI <= vi <= EMULATOR_LAST_VI for vi in vis]
            unique_positions = unique_mapping_positions(vis, valid, source_identities)
            unique_encoded = [encoded_indices[position] for position in unique_positions]
            pre = sum(encoded < EVENT_REFERENCE_INDEX for encoded in unique_encoded)
            event = sum(encoded in EVENT_INDICES for encoded in unique_encoded)
            post = sum(encoded > max(EVENT_INDICES) for encoded in unique_encoded)
            if pre < MIN_PRE_EVENT_FRAMES or post < MIN_POST_EVENT_FRAMES:
                continue
            mappings.append({
                "num": num, "den": den, "event_vi": event_vi,
                "vis": vis, "valid": valid, "unique_positions": unique_positions,
                "source_offsets": source_offsets,
                "raw_pre_event_count": sum(valid[index] and encoded < EVENT_REFERENCE_INDEX for index, encoded in enumerate(encoded_indices)),
                "raw_event_frame_count": sum(valid[index] and encoded in EVENT_INDICES for index, encoded in enumerate(encoded_indices)),
                "raw_post_event_count": sum(valid[index] and encoded > max(EVENT_INDICES) for index, encoded in enumerate(encoded_indices)),
                "pre_event_count": pre, "event_frame_count": event, "post_event_count": post,
            })
    if not mappings:
        raise AssertionError("temporal grid has no mappings meeting unique-emitted-observation coverage")
    return mappings


def mapping_record(mapping: dict[str, Any], transform: dict[str, Any], pair_scores: dict[str, np.ndarray], target_indices_all: np.ndarray, source_pixel_hashes: list[str]) -> dict[str, Any]:
    valid = np.asarray(mapping["valid"], dtype=np.bool_)
    unique_positions = np.asarray(mapping["unique_positions"], dtype=np.int64)
    target_indices = target_indices_all[unique_positions]
    source_indices_all = np.asarray(mapping["vis"], dtype=np.int64) - 1
    source_indices = source_indices_all[unique_positions]
    combined = pair_scores["combined"][target_indices, source_indices]
    luma = pair_scores["luma"][target_indices, source_indices]
    gradient = pair_scores["gradient"][target_indices, source_indices]
    if not (np.isfinite(combined).all() and np.isfinite(luma).all() and np.isfinite(gradient).all()):
        raise AssertionError("non-finite mapped score")
    encoded_all = np.asarray(range(TARGET_START, TARGET_END + 1), dtype=np.int64)
    valid_encoded = encoded_all[unique_positions]
    valid_source_offsets = np.asarray(mapping["source_offsets"], dtype=np.int64)[unique_positions]
    selected_pixel_hashes = [source_pixel_hashes[index] for index in source_indices.tolist()]
    raw_count = int(valid.sum())
    unique_count = int(len(unique_positions))
    distinct_pixel_hash_count = int(len(set(selected_pixel_hashes)))
    return {
        "slope_numerator": int(mapping["num"]),
        "slope_denominator": int(mapping["den"]),
        "slope_decimal": json_float(mapping["num"] / mapping["den"]),
        "intercept_event_vi": int(mapping["event_vi"]),
        "equation": "emulator_vi = intercept_event_vi + round_half_up((slope_num/slope_den)*(source_pts-event_source_pts)/1001)",
        "mapping_basis": "exact target source_pts offsets; encoded index labels retained for reporting",
        "image_identity_basis": "emitted PNG path plus manifest image_ordinal; first target row per emitted identity is deterministic representative, group weight=1",
        "rounding": "nearest VI, half-up exact integer arithmetic",
        "transform": transform,
        "observed_score": json_float(float(combined.mean())),
        "score_components": {
            "weighted": json_float(float(combined.mean())),
            "normalized_luma": json_float(float(luma.mean())),
            "gradient_structure": json_float(float(gradient.mean())),
            "weighted_formula": "0.58*normalized-luma correlation + 0.42*gradient correlation, each cosine mapped to [0,1]",
        },
        "score_distribution": {
            "min": json_float(float(combined.min())),
            "p05": json_float(float(np.percentile(combined, 5, method="linear"))),
            "median": json_float(float(np.percentile(combined, 50, method="linear"))),
            "p95": json_float(float(np.percentile(combined, 95, method="linear"))),
            "max": json_float(float(combined.max())),
        },
        "mapped_vi_at_event": [int(mapping["vis"][event - TARGET_START]) for event in EVENT_INDICES],
        "mapped_vod_index_min": int(valid_encoded.min()),
        "mapped_vod_index_max": int(valid_encoded.max()),
        "mapped_source_frame_offset_min": int(valid_source_offsets.min()),
        "mapped_source_frame_offset_max": int(valid_source_offsets.max()),
        "mapped_vi_min": int(source_indices.min() + 1),
        "mapped_vi_max": int(source_indices.max() + 1),
        "mapped_unique_vi_count": int(len(set(source_indices.tolist()))),
        "mapped_frame_count": raw_count,
        "mapped_raw_row_count": raw_count,
        "mapped_unique_image_count": unique_count,
        "mapped_unique_emitted_observation_count": unique_count,
        "mapped_distinct_pixel_hash_count": distinct_pixel_hash_count,
        "pre_event_raw_row_count": int(mapping["raw_pre_event_count"]),
        "event_raw_frame_count": int(mapping["raw_event_frame_count"]),
        "post_event_raw_row_count": int(mapping["raw_post_event_count"]),
        "pre_event_frame_count": int(mapping["pre_event_count"]),
        "event_frame_count": int(mapping["event_frame_count"]),
        "post_event_frame_count": int(mapping["post_event_count"]),
        "coverage_requirement_met": True,
    }

def rank_search(target_batch: dict[str, np.ndarray], transform_batches: list[dict[str, np.ndarray]], transforms: list[dict[str, Any]], source_offsets: list[int], source_identities: list[str], source_pixel_hashes: list[str]) -> dict[str, Any]:
    mappings = mapping_grid(source_offsets, source_identities)
    target_indices = np.arange(TARGET_COUNT, dtype=np.int64)
    pair_matrices = [score_matrices(target_batch, source_batch) for source_batch in transform_batches]
    records: list[dict[str, Any]] = []
    for mapping in mappings:
        for transform, matrices in zip(transforms, pair_matrices):
            records.append(mapping_record(mapping, transform, matrices, target_indices, source_pixel_hashes))
    sort_key = lambda record: (
        -float(record["observed_score"]), int(record["slope_numerator"]),
        int(record["slope_denominator"]), int(record["intercept_event_vi"]),
        str(record["transform"]["id"]),
    )
    records.sort(key=sort_key)
    if records != sorted(records, key=sort_key):
        raise AssertionError("scene ranking is not stable under its declared sort key")
    best = records[0]
    best_affine = (best["slope_numerator"], best["slope_denominator"], best["intercept_event_vi"])
    runner_up = next(record for record in records if (record["slope_numerator"], record["slope_denominator"], record["intercept_event_vi"]) != best_affine)
    anchored = [record for record in records if record["intercept_event_vi"] in (PLACEMENT_MARKER_VI, MUTATION_MARKER_VI)]
    accepted_intercepts: dict[str, list[int]] = {}
    mapping_counts: dict[str, int] = {}
    for num, den in TEMPORAL_SLOPES:
        values = sorted({int(mapping["event_vi"]) for mapping in mappings if mapping["num"] == num and mapping["den"] == den})
        key = f"{num}/{den}"
        accepted_intercepts[key] = [values[0], values[-1]] if values else []
        mapping_counts[key] = len(values)
    by_mapping = len(mappings)
    ranking_digest = sha256_bytes(canonical_json([
        (record["observed_score"], record["slope_numerator"], record["slope_denominator"], record["intercept_event_vi"], record["transform"]["id"])
        for record in records[:20]
    ]).encode())
    return {
        "mapping_grid": {
            "slope_values": [{"numerator": num, "denominator": den, "decimal": json_float(num / den)} for num, den in TEMPORAL_SLOPES],
            "equation": "emulator_vi = intercept_event_vi + round_half_up((slope_num/slope_den)*source_frame_offset), source_frame_offset=(source_pts-event_source_pts)/1001",
            "image_identity_basis": "source row emitted PNG path plus manifest image_ordinal; first target representative per emitted identity, group weight=1; source pixel SHA-256 is reported separately",
            "intercept_definition": "emulator VI at VOD encoded event index 431",
            "event_intercept_range": [1, EMULATOR_LAST_VI],
            "accepted_event_intercept_range_per_slope": accepted_intercepts,
            "mapping_count_per_slope": mapping_counts,
            "coverage_requirement": {
                "minimum_pre_event_frames": MIN_PRE_EVENT_FRAMES,
                "minimum_post_event_frames": MIN_POST_EVENT_FRAMES,
                "pre_event_definition": "encoded_index < 431",
                "event_definition": "encoded_index in [431,432], reported separately",
                "post_event_definition": "encoded_index > 432",
                "coverage_unit": "unique emitted PNG path+manifest image_ordinal observations (first target representative, group weight=1); distinct pixel hashes are diagnostic only",
                "raw_rows_reported_separately": True,
                "out_of_range_frames_excluded_not_padded": True,
            },
            "vod_encoded_index_range": [TARGET_START, TARGET_END],
            "emulator_vi_range": [EMULATOR_FIRST_VI, EMULATOR_LAST_VI],
            "source_frame_offset_range": [min(source_offsets), max(source_offsets)],
            "affine_mapping_count": by_mapping,
            "transform_count": len(transforms),
            "joint_candidate_count": by_mapping * len(transforms),
            "complete": True,
        },
        "best": best,
        "runner_up_distinct_affine": runner_up,
        "event_anchored_best": anchored[0] if anchored else None,
        "top_ranked": records[:5],
        "ranking_digest": ranking_digest,
        "stable_sort_key": "(-observed_score, slope_numerator, slope_denominator, intercept_event_vi, transform_id)",
    }


def state_summary(rows: list[dict[str, Any]], mapping: dict[str, Any]) -> dict[str, Any]:
    selected = [rows[vi - 1] for vi in mapping["vis"] if EMULATOR_FIRST_VI <= vi <= EMULATOR_LAST_VI]
    def values(field: str) -> list[float]: return [float(row["state"][field]) for row in selected if finite(row["state"].get(field))]
    def summarize(field: str) -> dict[str, Any]:
        vals = values(field); return {"count": len(vals), "min": json_float(min(vals)) if vals else None, "max": json_float(max(vals)) if vals else None, "mean": json_float(float(np.mean(vals))) if vals else None}
    modes: dict[str, int] = {}; actions: dict[str, int] = {}
    for row in selected:
        mode = str(row["state"].get("camera_mode")); action = str(row["state"].get("action")); modes[mode] = modes.get(mode, 0) + 1; actions[action] = actions.get(action, 0) + 1
    fields = ["camera_pos_x", "camera_pos_y", "camera_pos_z", "camera_focus_x", "camera_focus_y", "camera_focus_z", "camera_yaw", "floor_height", "gfx_x", "gfx_y", "gfx_z"]
    return {"mapped_state_row_count": len(selected), "mapped_unique_state_vi_count": len(set(row["state"]["vi"] for row in selected)), "mapped_image_reused_row_count": sum(bool(row.get("image_reused")) for row in selected), "camera_mode_histogram": dict(sorted(modes.items())), "action_histogram": dict(sorted(actions.items())), "camera_and_scene_ranges": {field: summarize(field) for field in fields}, "state_events_seen": {"placed_rows": sum(int(row["state"].get("placed", 0)) for row in selected), "mutated_rows": sum(int(row["state"].get("mutated", 0)) for row in selected), "event_labels": sorted({str(row["state"].get("event")) for row in selected})}, "note": "state rows are reported metadata only; no Mario RAM/mechanism inference is performed"}


def scenario_result(scenario: str, search: dict[str, Any], calibration: dict[str, Any], rows: list[dict[str, Any]], source_offsets: list[int]) -> dict[str, Any]:
    best = search["best"]
    observed = float(best["observed_score"])
    threshold = float(calibration["positive_control_p05_envelope"])
    wrong_upper = float(calibration["negative_control_p95_envelope"])
    strict_within_domain_pass = observed >= threshold and observed > wrong_upper
    status = "fit" if calibration["cross_domain_positive_control_available"] and calibration["separated"] and strict_within_domain_pass else "indeterminate"
    anchored = search["event_anchored_best"]
    failure_assessment = {
        "calibration": (
            "within-domain controls separate; no known-positive VOD-to-Glide64mk2 pair"
            if calibration["cross_domain_positive_control_absent"] else
            "cross-domain positive calibration available and controls separated"
        ),
        "scene_geometry_camera": "not identifiable from this cross-domain score without a renderer-transfer positive control",
        "temporal_behavior": "exhaustive VI-per-VOD affine grid scored using exact source-PTS offsets; strict within-domain threshold remains the reported diagnostic",
        "event_anchor_check": {
            "available": anchored is not None,
            "anchored_score": anchored["observed_score"] if anchored else None,
            "best_minus_anchored": json_float(observed - float(anchored["observed_score"])) if anchored else None,
        },
        "interpretation": (
            "indeterminate: low cross-domain score cannot distinguish renderer/lighting/texture domain shift from scene/camera/state mismatch"
            if status == "indeterminate" else
            "fit under strict within-domain and cross-domain calibrated criteria"
        ),
    }
    return {
        "scenario": scenario,
        "status": status,
        "observed_score": best["observed_score"],
        "calibrated_threshold": calibration["positive_control_p05_envelope"],
        "strict_within_domain_threshold_pass": strict_within_domain_pass,
        "cross_domain_positive_control_absent": calibration["cross_domain_positive_control_absent"],
        "margin": json_float(observed - threshold),
        "margin_vs_negative_control": json_float(observed - wrong_upper),
        "acceptance": {
            "score_at_or_above_positive_envelope": observed >= threshold,
            "score_above_wrong_time_envelope": observed > wrong_upper,
            "coverage_requirement_met": bool(best["coverage_requirement_met"]),
            "scene_camera_agreement_meets_calibrated_criterion": status == "fit",
        },
        "best_mapping": best,
        "runner_up_mapping": search["runner_up_distinct_affine"],
        "event_anchored_best": anchored,
        "failure_assessment": failure_assessment,
        "window_coverage": {
            "vod_encoded_indices_available": [TARGET_START, TARGET_END],
            "mapped_vod_index_range": [best["mapped_vod_index_min"], best["mapped_vod_index_max"]],
            "mapped_source_frame_offset_range": [best["mapped_source_frame_offset_min"], best["mapped_source_frame_offset_max"]],
            "mapped_frame_count": best["mapped_frame_count"],
            "mapped_raw_row_count": best["mapped_raw_row_count"],
            "mapped_unique_emitted_observation_count": best["mapped_unique_image_count"],
            "mapped_distinct_pixel_hash_count": best["mapped_distinct_pixel_hash_count"],
            "pre_event_raw_row_count": best["pre_event_raw_row_count"],
            "event_raw_frame_count": best["event_raw_frame_count"],
            "post_event_raw_row_count": best["post_event_raw_row_count"],
            "pre_event_frame_count": best["pre_event_frame_count"],
            "event_frame_count": best["event_frame_count"],
            "post_event_frame_count": best["post_event_frame_count"],
            "coverage_unit": "unique emitted PNG path+manifest image_ordinal observations; first target representative per emitted identity, group weight=1",
            "emulator_vi_min": best["mapped_vi_min"],
            "emulator_vi_max": best["mapped_vi_max"],
            "unique_emulator_vis": best["mapped_unique_vi_count"],
            "reused_images_accounted_for": True,
        },
        "state_summary_for_best_mapping": state_summary(rows, {"vis": mapping_series(int(best["slope_numerator"]), int(best["slope_denominator"]), int(best["intercept_event_vi"]), source_offsets)}),
        "failure_mode": "insufficient cross-domain calibration (strict within-domain threshold failure is diagnostic only)",
        "limitations": [
            "VOD encoded frames are not equated with emulator VIs; all declared VI-per-source-frame affine mappings were scored.",
            "VOD crops are decoded H.264-derived RGB and emulator screenshots are separately decoded PNGs; normalized luma/gradient features reduce but do not erase codec/resolution differences.",
            "No known-positive VOD-to-Glide64mk2 pair is available, so scene/camera incompatibility cannot be separated from renderer/lighting/texture domain shift.",
            "A scene score cannot infer Mario RAM, mutation mechanism, or original-N64 timing.",
        ],
    }
def build_report(target_manifest: dict[str, Any], render_manifest: dict[str, Any], data: dict[str, Any], cache_dir: Path) -> dict[str, Any]:
    target_pixels: dict[int, np.ndarray] = data["target_pixels"]
    scenario_pixels: dict[str, dict[str, np.ndarray]] = data["scenario_pixels"]
    scenario_rows: dict[str, list[dict[str, Any]]] = data["scenario_rows"]
    source_offsets, source_timing = target_source_frame_offsets(target_manifest["targets"])
    mask = detect_fixed_mask(scenario_pixels)
    viewport = mask["active_viewport"]
    transforms = transform_family(viewport, int(target_manifest["crop"]["width"]), int(target_manifest["crop"]["height"]))
    target_batch = batch_features([feature_for_crop(target_pixels[index]) for index in range(TARGET_START, TARGET_END + 1)])
    target_control = target_controls(target_pixels, target_batch)
    emulator_control, canonical = emulator_controls(scenario_rows, scenario_pixels, viewport)
    paired_control = paired_synthetic_controls(scenario_rows, canonical)
    calibration = calibrate(target_control, emulator_control)
    scenario_results: dict[str, Any] = {}
    search_specs: dict[str, Any] = {}
    ranking_digests: dict[str, str] = {}
    for scenario in SCENARIOS:
        by_path = scenario_pixels[scenario]
        rows = scenario_rows[scenario]
        source_identities = [
            f"{row['image']['path']}|image_ordinal={row['image']['image_ordinal']}"
            for row in rows
        ]
        source_pixel_hashes = [str(row["image"]["sha256"]) for row in rows]
        if len(source_identities) != EMULATOR_LAST_VI or len(source_pixel_hashes) != EMULATOR_LAST_VI:
            raise AssertionError(f"{scenario}: source identity/hash rows are incomplete")
        transform_batches: list[dict[str, np.ndarray]] = []
        for transform in transforms:
            feature_by_path = {path: transformed_feature(array, viewport, transform) for path, array in by_path.items()}
            transform_batches.append(batch_features([feature_by_path[str(row["image"]["path"])] for row in rows]))
        search = rank_search(target_batch, transform_batches, transforms, source_offsets, source_identities, source_pixel_hashes)
        if not finite(search["best"]["observed_score"]) or not finite(search["runner_up_distinct_affine"]["observed_score"]):
            raise AssertionError(f"{scenario}: non-finite best/runner-up score")
        scenario_results[scenario] = scenario_result(scenario, search, calibration, rows, source_offsets)
        search_specs[scenario] = search["mapping_grid"]
        ranking_digests[scenario] = search["ranking_digest"]
    assert len(transforms) == 27
    assert data["input_hashes"]["all_consumed_pngs_hash_verified"]
    assert data["input_hashes"]["all_consumed_pngs_dimensions_verified"]
    assert paired_control["pre_event_exact_equal_count"] == 99
    assert paired_control["first_visual_divergence_vi"] is not None
    for result in scenario_results.values():
        assert result["window_coverage"]["pre_event_frame_count"] >= MIN_PRE_EVENT_FRAMES
        assert result["window_coverage"]["post_event_frame_count"] >= MIN_POST_EVENT_FRAMES
    mapping_spec = search_specs[SCENARIOS[0]]
    verified_records = {
        "target": data["target_records"],
        **{scenario: data["scenario_records"][scenario] for scenario in SCENARIOS},
    }
    verified_records_digest = sha256_bytes(canonical_json(verified_records).encode())
    script_hash = sha256_file(Path(__file__))
    search_spec_hash = sha256_bytes(canonical_json({"transforms": transforms, "mapping": search_specs}).encode())
    return {
        "schema_version": 1,
        "status": "pass" if calibration["separated"] and calibration["cross_domain_positive_control_available"] else "indeterminate",
        "cross_domain_positive_control_absent": calibration["cross_domain_positive_control_absent"],
        "classification_note": "scenario statuses are indeterminate because no known-positive VOD-to-Glide64mk2 pair calibrates renderer/lighting/texture transfer; strict within-domain threshold results remain diagnostic",
        "analyzer": {
            "path": str(Path(__file__).resolve().relative_to(ROOT)), "sha256": script_hash,
            "python": sys.version.split()[0], "numpy": np.__version__, "pillow": Image.__version__,
            "deterministic": True,
            "feature_grid": {"width": FEATURE_WIDTH, "height": FEATURE_HEIGHT, "resampling": "bicubic"},
            "score_formula": "0.58 normalized-luma correlation + 0.42 gradient-structure correlation; each cosine mapped to [0,1]",
        },
        "inputs": {
            "target_manifest": data["target_manifest_path"],
            "render_manifest": data["render_manifest_path"],
            "remote_host": data["remote_host"],
            "hashes": {**data["input_hashes"], "verified_png_records_sha256": verified_records_digest},
            "remote_png_consumption": {
                "target_unique_png_count": len(data["target_records"]),
                "scenario_emitted_observation_counts_by_path": {scenario: len(data["scenario_records"][scenario]) for scenario in SCENARIOS},
                "scenario_distinct_pixel_hash_counts": {
                    scenario: len({record["sha256"] for record in data["scenario_records"][scenario].values()})
                    for scenario in SCENARIOS
                },
                "all_listed_render_row_images_consumed_or_reused": True,
                "cache_directory": str(cache_dir), "cache_is_temporary": True,
            },
        },
        "fixed_masks": mask,
        "transform_search": {
            "target_crop_dimensions": [int(target_manifest["crop"]["width"]), int(target_manifest["crop"]["height"])],
            "family_definition": "source active viewport crop widths 480/560/640; target-aspect crop heights; left/center/right and top/center/bottom translations; bicubic resize",
            "transform_count": len(transforms), "transforms": transforms, "search_spec_sha256": search_spec_hash,
        },
        "temporal_search": {
            "event_reference_index": EVENT_REFERENCE_INDEX,
            "placement_marker_vi": PLACEMENT_MARKER_VI,
            "mutation_marker_vi": MUTATION_MARKER_VI,
            "mapping_note": "slopes are emulator VIs per exact source-frame offset derived from target source_pts; encoded indices are labels only; event 431/432 are reported separately from true post 433..530",
            "source_timing": source_timing,
            "declared_affine_mapping_domain_count": len(TEMPORAL_SLOPES) * (EMULATOR_LAST_VI - EMULATOR_FIRST_VI + 1),
            "affine_mapping_count_per_scenario": {scenario: search_specs[scenario]["affine_mapping_count"] for scenario in SCENARIOS},
            "joint_candidate_count_per_scenario": {scenario: search_specs[scenario]["joint_candidate_count"] for scenario in SCENARIOS},
            "complete_grid": all(spec["complete"] for spec in search_specs.values()),
            "slope_values": mapping_spec["slope_values"],
            "accepted_event_intercept_range_per_slope": {scenario: search_specs[scenario]["accepted_event_intercept_range_per_slope"] for scenario in SCENARIOS},
            "mapping_count_per_slope": {scenario: search_specs[scenario]["mapping_count_per_slope"] for scenario in SCENARIOS},
            "source_frame_offset_range": mapping_spec["source_frame_offset_range"],
            "coverage_requirement": mapping_spec["coverage_requirement"],
        },
        "controls": {"vod": target_control, "emulator": emulator_control, "paired_synthetic": paired_control, "calibration": calibration},
        "scenarios": scenario_results,
        "validation": {
            "status": "pass",
            "input_manifest_schema_checked": True,
            "all_consumed_png_hashes_checked": True,
            "all_consumed_png_dimensions_checked": True,
            "finite_score_checked": True,
            "transform_cardinality_checked": len(transforms) == 27,
            "temporal_grid_cardinality_checked": all(spec["complete"] for spec in search_specs.values()),
            "source_pts_offsets_integral_checked": True,
            "source_pts_gap_not_collapsed_checked": bool(source_timing["non_nominal_delta_breaks"]),
            "temporal_coverage_checked": all(result["window_coverage"]["pre_event_frame_count"] >= MIN_PRE_EVENT_FRAMES and result["window_coverage"]["post_event_frame_count"] >= MIN_POST_EVENT_FRAMES for result in scenario_results.values()),
            "paired_controls_checked": True,
            "stable_ranking": bool(all(ranking_digests.values())),
            "ranking_digests": ranking_digests,
            "stable_ranking_definition": "deterministic sort key with explicit numeric and transform-id tie breaks; sorted-list invariant asserted",
            "search_spec_sha256": search_spec_hash,
        },
        "limitations": [
            "This report tests full-frame scene/camera agreement only; it does not infer Mario RAM or a causal mechanism from VOD pixels.",
            "No manual ROI or scenario-dependent mask was used. The only emulator mask is the jointly decoded common black border mask above.",
            "Exact source_pts offsets, including the single 2002-PTS gap at encoded 478->479, are used; encoded index is retained only as a label.",
            "A fit would still be candidate scene agreement, not proof of original-N64 identity or incident causality.",
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE, help="temporary cache for hash-verified PNG bytes")
    parser.add_argument("--target-manifest", type=Path, default=TARGET_MANIFEST)
    parser.add_argument("--render-manifest", type=Path, default=RENDER_MANIFEST)
    parser.add_argument("--remote-host", help="explicit SSH fallback for assets missing locally")
    parser.add_argument("--path-map", action="append", type=parse_path_map, default=[], metavar="OLD=LOCAL", help="relocate manifest path prefixes without altering provenance; repeatable")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    target_manifest, render_manifest, data = load_inputs(
        args.cache_dir, args.target_manifest, args.render_manifest, args.remote_host, args.path_map
    )
    report = build_report(target_manifest, render_manifest, data, args.cache_dir)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    statuses = ", ".join(f"{scenario}={report['scenarios'][scenario]['status']}" for scenario in SCENARIOS)
    print(f"wrote {args.output}: {statuses}; joint candidates/scenario={report['temporal_search']['joint_candidate_count_per_scenario']}")
    return 0


if __name__ == "__main__": raise SystemExit(main())
