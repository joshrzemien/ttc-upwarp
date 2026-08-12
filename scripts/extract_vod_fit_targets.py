#!/usr/bin/env python3
"""Extract hash-bound VOD gameplay crops for the next TTC fit experiment.

This extractor deliberately keeps the audited bounded format-134 stream and its
same-source extension as separate inputs.  The extension is admitted only after
its decoded RGB frames have a unique contiguous hash alignment to the audited
prefix; the overlapping extension frame is then de-duplicated.  All rows remain
encoded-video observations.  They are not N64 VI or game-update evidence.

The default extraction writes PNGs to a temporary local staging directory,
retains the two bounded inputs and PNGs on the requested remote host, and emits
a self-contained JSON manifest containing local/remote hashes and exact PTS.
Use ``--validate-only`` to re-check a manifest, including remote PNG hashes and
PNG decoding/dimensions.
"""
from __future__ import annotations

import argparse
import binascii
import hashlib
import json
import math
import os
import platform
import shutil
import struct
import subprocess
import sys
import tempfile
import zlib
from fractions import Fraction
from pathlib import Path
from typing import Any, Iterable


EXPECTED_AUDIT_SHA256 = "92f7b14066cae3d6001a3ba8a1a6e6025627b17d4cc624954dea8cb664edb763"
EXPECTED_AUDIT_SIZE = 1017347
DEFAULT_CROP = (0, 145, 340, 215)
DEFAULT_EVENT = (Fraction("3481.4"), Fraction("3481.47"))
DEFAULT_SOURCE_OFFSET = Fraction(104010887, 30000)
DEFAULT_START_INDEX = 340
DEFAULT_POST_END_INDEX = 530
LOW_RESOLUTION = (32, 18)
NEAR_REPEAT_LUMA_MAE_MAX = 2
NEAR_REPEAT_DHASH_HAMMING_MAX = 24
SCENE_CHANGE_LUMA_MAE_MIN = 20
SCENE_CHANGE_DHASH_HAMMING_MIN = 192
REMOTE_PROJECT = "/home/zman/projects/labs/ttc_upwarp"
REMOTE_RESULT_DIR = "results/vod_fit_targets_20260812"
REMOTE_AUDITED_NAME = "audited_vod_segment_format134.mp4"
REMOTE_EXTENSION_NAME = "same_source_vod_extension_format134.mp4"


class ExtractionError(RuntimeError):
    """An input, decode, alignment, or manifest invariant failed."""


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
    except OSError as exc:
        raise ExtractionError(f"cannot hash {path}: {exc}") from exc
    return digest.hexdigest()


def decimal_text(value: Fraction, places: int = 12) -> str:
    """Deterministic rounded decimal text without float conversion."""
    sign = "-" if value < 0 else ""
    value = abs(value)
    whole, remainder = divmod(value.numerator, value.denominator)
    if remainder == 0:
        return f"{sign}{whole}"
    digits: list[str] = []
    for _ in range(places):
        remainder *= 10
        digit, remainder = divmod(remainder, value.denominator)
        digits.append(str(digit))
    if remainder * 2 >= value.denominator:
        carry = 1
        for index in range(len(digits) - 1, -1, -1):
            digit = int(digits[index]) + carry
            if digit == 10:
                digits[index] = "0"
            else:
                digits[index] = str(digit)
                carry = 0
                break
        if carry:
            whole += 1
    return f"{sign}{whole}.{''.join(digits).rstrip('0') or '0'}"


def fraction_text(value: Fraction) -> str:
    return str(value.numerator) if value.denominator == 1 else f"{value.numerator}/{value.denominator}"


def parse_fraction(value: str) -> Fraction:
    try:
        result = Fraction(value)
    except (TypeError, ValueError, ZeroDivisionError) as exc:
        raise argparse.ArgumentTypeError(f"invalid rational {value!r}") from exc
    if result.denominator <= 0:
        raise argparse.ArgumentTypeError(f"invalid rational {value!r}")
    return result


def run(command: list[str], *, text: bool = True) -> subprocess.CompletedProcess[Any]:
    try:
        return subprocess.run(
            command,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=text,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        stderr = getattr(exc, "stderr", b"") or b""
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", errors="replace")
        raise ExtractionError(f"command failed: {' '.join(command)}\n{str(stderr).strip()}") from exc


def tool_version(executable: str) -> str:
    completed = run([executable, "-version"])
    line = completed.stdout.splitlines()[0] if completed.stdout.splitlines() else ""
    if not line:
        raise ExtractionError(f"{executable} emitted no version")
    return line


def executable_record(executable: str) -> dict[str, Any]:
    path = shutil.which(executable)
    if path is None:
        raise ExtractionError(f"required executable not found: {executable}")
    resolved = Path(path).resolve()
    return {
        "name": executable,
        "path": str(resolved),
        "sha256": sha256_file(resolved),
        "version": tool_version(executable),
    }


def ffprobe_stream_and_frames(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    stream_json = json.loads(
        run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_streams",
                "-of",
                "json",
                str(path),
            ]
        ).stdout
    )
    streams = stream_json.get("streams")
    if not isinstance(streams, list) or not streams or not isinstance(streams[0], dict):
        raise ExtractionError(f"ffprobe found no video stream in {path}")
    frame_json = json.loads(
        run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_frames",
                "-show_entries",
                "frame=best_effort_timestamp,pkt_duration,key_frame,pict_type,coded_picture_number,display_picture_number",
                "-of",
                "json",
                str(path),
            ]
        ).stdout
    )
    frames = frame_json.get("frames")
    if not isinstance(frames, list) or not frames:
        raise ExtractionError(f"ffprobe returned no decoded frames in {path}")
    metadata: list[dict[str, Any]] = []
    for index, row in enumerate(frames):
        if not isinstance(row, dict) or row.get("best_effort_timestamp") is None:
            raise ExtractionError(f"frame {index} in {path} has no best-effort PTS")
        try:
            pts = int(row["best_effort_timestamp"])
        except (TypeError, ValueError) as exc:
            raise ExtractionError(f"frame {index} in {path} has invalid PTS") from exc
        metadata.append(
            {
                "source_decoded_index": index,
                "clip_pts": pts,
                "duration": int(row["pkt_duration"]) if row.get("pkt_duration") is not None else None,
                "key_frame": bool(int(row.get("key_frame", 0) or 0)),
                "picture_type": str(row.get("pict_type", "UNKNOWN")),
                "coded_picture_number": (
                    int(row["coded_picture_number"])
                    if row.get("coded_picture_number") is not None
                    else None
                ),
                "display_picture_number": (
                    int(row["display_picture_number"])
                    if row.get("display_picture_number") is not None
                    else None
                ),
            }
        )
    return streams[0], metadata


def decode_rgb(path: Path, width: int, height: int, expected_frames: int) -> list[bytes]:
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(path),
        "-map",
        "0:v:0",
        "-fps_mode",
        "passthrough",
        "-pix_fmt",
        "rgb24",
        "-f",
        "rawvideo",
        "pipe:1",
    ]
    completed = run(command, text=False)
    frame_size = width * height * 3
    if len(completed.stdout) % frame_size:
        raise ExtractionError(f"ffmpeg emitted a partial RGB frame for {path}")
    frames = [
        completed.stdout[index : index + frame_size]
        for index in range(0, len(completed.stdout), frame_size)
    ]
    if len(frames) != expected_frames:
        raise ExtractionError(
            f"decoded frame count mismatch for {path}: ffprobe={expected_frames}, ffmpeg={len(frames)}"
        )
    return frames


def crop_rgb(frame: bytes, width: int, crop: tuple[int, int, int, int]) -> bytes:
    x, y, crop_width, crop_height = crop
    row_stride = width * 3
    crop_stride = crop_width * 3
    result = bytearray(crop_stride * crop_height)
    out = 0
    for row in range(y, y + crop_height):
        start = row * row_stride + x * 3
        result[out : out + crop_stride] = frame[start : start + crop_stride]
        out += crop_stride
    return bytes(result)


def sampled_luma(frame: bytes, width: int, height: int, crop: tuple[int, int, int, int]) -> bytes:
    crop_x, crop_y, crop_width, crop_height = crop
    low_width, low_height = LOW_RESOLUTION
    values = bytearray(low_width * low_height)
    out = 0
    row_stride = width * 3
    for output_y in range(low_height):
        source_y = crop_y + ((2 * output_y + 1) * crop_height) // (2 * low_height)
        source_y = min(height - 1, source_y)
        for output_x in range(low_width):
            source_x = crop_x + ((2 * output_x + 1) * crop_width) // (2 * low_width)
            source_x = min(width - 1, source_x)
            source = source_y * row_stride + source_x * 3
            red, green, blue = frame[source : source + 3]
            values[out] = (54 * red + 183 * green + 19 * blue + 128) // 256
            out += 1
    return bytes(values)


def dhash(luma: bytes) -> str:
    width, height = LOW_RESOLUTION
    packed = bytearray((height * (width - 1) + 7) // 8)
    bit = 0
    for row in range(height):
        base = row * width
        for column in range(width - 1):
            if luma[base + column] > luma[base + column + 1]:
                packed[bit // 8] |= 1 << (7 - (bit % 8))
            bit += 1
    return packed.hex()


def hamming_hex(left: str, right: str) -> int:
    return sum((a ^ b).bit_count() for a, b in zip(bytes.fromhex(left), bytes.fromhex(right)))


def make_png_rgb(width: int, height: int, rgb: bytes) -> bytes:
    expected = width * height * 3
    if len(rgb) != expected:
        raise ExtractionError(f"PNG input has {len(rgb)} bytes, expected {expected}")

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", binascii.crc32(kind + payload) & 0xFFFFFFFF)

    scanlines = b"".join(b"\x00" + rgb[row * width * 3 : (row + 1) * width * 3] for row in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(scanlines, 9))
        + chunk(b"IEND", b"")
    )


def paeth(a: int, b: int, c: int) -> int:
    estimate = a + b - c
    pa = abs(estimate - a)
    pb = abs(estimate - b)
    pc = abs(estimate - c)
    if pa <= pb and pa <= pc:
        return a
    if pb <= pc:
        return b
    return c


def decode_png_rgb(data: bytes) -> tuple[int, int, bytes]:
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ExtractionError("PNG signature is invalid")
    position = 8
    width = height = None
    idat = bytearray()
    while position < len(data):
        if position + 12 > len(data):
            raise ExtractionError("PNG chunk is truncated")
        length = struct.unpack(">I", data[position : position + 4])[0]
        kind = data[position + 4 : position + 8]
        end = position + 12 + length
        if end > len(data):
            raise ExtractionError("PNG chunk payload is truncated")
        payload = data[position + 8 : position + 8 + length]
        expected_crc = struct.unpack(">I", data[position + 8 + length : end])[0]
        if binascii.crc32(kind + payload) & 0xFFFFFFFF != expected_crc:
            raise ExtractionError("PNG chunk CRC mismatch")
        if kind == b"IHDR":
            if length != 13:
                raise ExtractionError("PNG IHDR length is invalid")
            width, height, bit_depth, color_type, compression, filtering, interlace = struct.unpack(
                ">IIBBBBB", payload
            )
            if (bit_depth, color_type, compression, filtering, interlace) != (8, 2, 0, 0, 0):
                raise ExtractionError("PNG is not non-interlaced 8-bit RGB")
        elif kind == b"IDAT":
            idat.extend(payload)
        elif kind == b"IEND":
            break
        position = end
    if width is None or height is None or not idat:
        raise ExtractionError("PNG lacks IHDR or IDAT")
    try:
        filtered = zlib.decompress(bytes(idat))
    except zlib.error as exc:
        raise ExtractionError("PNG IDAT does not decode") from exc
    stride = width * 3
    expected = height * (stride + 1)
    if len(filtered) != expected:
        raise ExtractionError(f"PNG scanline length {len(filtered)} != {expected}")
    output = bytearray(height * stride)
    previous = bytearray(stride)
    offset = 0
    for row in range(height):
        filter_type = filtered[offset]
        encoded = filtered[offset + 1 : offset + 1 + stride]
        offset += stride + 1
        decoded = bytearray(stride)
        for index, value in enumerate(encoded):
            left = decoded[index - 3] if index >= 3 else 0
            up = previous[index]
            up_left = previous[index - 3] if index >= 3 else 0
            if filter_type == 0:
                predictor = 0
            elif filter_type == 1:
                predictor = left
            elif filter_type == 2:
                predictor = up
            elif filter_type == 3:
                predictor = (left + up) // 2
            elif filter_type == 4:
                predictor = paeth(left, up, up_left)
            else:
                raise ExtractionError(f"PNG uses unsupported filter {filter_type}")
            decoded[index] = (value + predictor) & 0xFF
        output[row * stride : (row + 1) * stride] = decoded
        previous = decoded
    return int(width), int(height), bytes(output)


def frame_features(
    frame: bytes,
    width: int,
    height: int,
    crop: tuple[int, int, int, int],
    previous: dict[str, Any] | None,
) -> dict[str, Any]:
    crop_bytes = crop_rgb(frame, width, crop)
    luma = sampled_luma(frame, width, height, crop)
    current_dhash = dhash(luma)
    luma_difference_sum = None
    luma_difference_mean = None
    dhash_distance = None
    if previous is not None:
        luma_difference_sum = sum(abs(a - b) for a, b in zip(luma, previous["luma"]))
        luma_difference_mean = decimal_text(Fraction(luma_difference_sum, len(luma)), 6)
        dhash_distance = hamming_hex(current_dhash, previous["luma_dhash_hex"])
    full_hash = sha256_bytes(frame)
    crop_hash = sha256_bytes(crop_bytes)
    full_exact = previous is not None and full_hash == previous["full_frame_sha256"]
    crop_exact = previous is not None and crop_hash == previous["gameplay_crop_sha256"]
    near_repeat = (
        previous is not None
        and not full_exact
        and not crop_exact
        and luma_difference_sum is not None
        and luma_difference_sum <= NEAR_REPEAT_LUMA_MAE_MAX * len(luma)
        and dhash_distance is not None
        and dhash_distance <= NEAR_REPEAT_DHASH_HAMMING_MAX
    )
    scene_change = (
        luma_difference_sum is not None
        and (
            luma_difference_sum >= SCENE_CHANGE_LUMA_MAE_MIN * len(luma)
            or (dhash_distance is not None and dhash_distance >= SCENE_CHANGE_DHASH_HAMMING_MIN)
        )
    )
    if previous is None:
        repeat_class = "first_frame"
    elif full_exact:
        repeat_class = "exact_full_frame"
    elif crop_exact:
        repeat_class = "exact_gameplay_crop"
    elif near_repeat:
        repeat_class = "near_gameplay_crop"
    else:
        repeat_class = "not_repeat"
    return {
        "full_frame_sha256": full_hash,
        "gameplay_crop_sha256": crop_hash,
        "luma_grid_sha256": sha256_bytes(luma),
        "luma_dhash_hex": current_dhash,
        "luma_difference_sum": luma_difference_sum,
        "luma_difference_mean": luma_difference_mean,
        "dhash_hamming_distance": dhash_distance,
        "repeat_class": repeat_class,
        "full_frame_exact_repeat": full_exact,
        "gameplay_crop_exact_repeat": crop_exact,
        "near_repeat": near_repeat,
        "scene_change": scene_change,
        "luma": luma,
        "crop_bytes": crop_bytes,
    }


def load_audit(audit_path: Path) -> dict[str, Any]:
    try:
        audit = json.loads(audit_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ExtractionError(f"cannot read audit {audit_path}: {exc}") from exc
    rows = audit.get("observations", {}).get("all_decoded_frames")
    if not isinstance(rows, list) or len(rows) < 480:
        raise ExtractionError("audit lacks the expected 480 decoded rows")
    if audit.get("input", {}).get("sha256") != EXPECTED_AUDIT_SHA256:
        raise ExtractionError("audit input SHA-256 does not match the audited format-134 hash")
    return audit


def stream_dimensions(stream: dict[str, Any], path: Path) -> tuple[int, int, tuple[int, int]]:
    try:
        width = int(stream["width"])
        height = int(stream["height"])
        numerator, denominator = (int(value) for value in str(stream["time_base"]).split("/", 1))
    except (KeyError, TypeError, ValueError) as exc:
        raise ExtractionError(f"invalid ffprobe stream metadata for {path}") from exc
    if (width, height) != (640, 360) or (numerator, denominator) != (1, 30000):
        raise ExtractionError(
            f"{path} must be the audited 640x360 1/30000 stream, got {width}x{height} {numerator}/{denominator}"
        )
    return width, height, (numerator, denominator)


def build_input_rows(
    path: Path,
    *,
    source_offset: Fraction,
    crop: tuple[int, int, int, int],
    audit_rows: list[dict[str, Any]] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[bytes]]:
    stream, metadata = ffprobe_stream_and_frames(path)
    width, height, timebase = stream_dimensions(stream, path)
    if crop[0] + crop[2] > width or crop[1] + crop[3] > height:
        raise ExtractionError(f"crop {crop} exceeds {path} dimensions")
    frames = decode_rgb(path, width, height, len(metadata))
    rows: list[dict[str, Any]] = []
    previous: dict[str, Any] | None = None
    for metadata_row, frame in zip(metadata, frames):
        features = frame_features(frame, width, height, crop, previous)
        clip_pts = int(metadata_row["clip_pts"])
        source_seconds = source_offset + Fraction(clip_pts * timebase[0], timebase[1])
        source_ticks = source_seconds / Fraction(timebase[0], timebase[1])
        if source_ticks.denominator != 1:
            raise ExtractionError(f"source PTS is not integral for {path} frame {metadata_row['source_decoded_index']}")
        row = {
            **{key: value for key, value in metadata_row.items() if key != "source_decoded_index"},
            "source_decoded_index": int(metadata_row["source_decoded_index"]),
            "source_pts": int(source_ticks),
            "source_time_rational": fraction_text(source_seconds),
            "source_time_seconds": decimal_text(source_seconds),
            **{key: value for key, value in features.items() if key not in {"luma", "crop_bytes"}},
            "_luma": features["luma"],
            "_crop_bytes": features["crop_bytes"],
        }
        rows.append(row)
        previous = features
    if audit_rows is not None:
        if len(rows) != len(audit_rows):
            raise ExtractionError(f"audited input decoded row count changed: {len(rows)} != {len(audit_rows)}")
        for index, (row, audit_row) in enumerate(zip(rows, audit_rows)):
            if row["full_frame_sha256"] != audit_row.get("full_frame_sha256"):
                raise ExtractionError(f"audited RGB hash mismatch at frame {index}")
            if row["gameplay_crop_sha256"] != audit_row.get("gameplay_crop_sha256"):
                raise ExtractionError(f"audited crop hash mismatch at frame {index}")
            if row["clip_pts"] != int(audit_row.get("clip_pts")):
                raise ExtractionError(f"audited PTS mismatch at frame {index}")
    return stream, rows, frames


def align_extension(audited_rows: list[dict[str, Any]], extension_rows: list[dict[str, Any]]) -> dict[str, int]:
    """Align the extension by a unique RGB span and terminal PTS overlap.

    The bounded section's final edit boundary omits one source PTS (479479),
    so its terminal row (PTS 480480) is ext row 480, not a terminal
    eight-frame contiguous span.  The unique eight-frame prefix span proves
    the same decoded stream; the terminal RGB hash plus exact source PTS then
    identifies the de-duplication boundary.
    """
    span = min(8, len(audited_rows))
    expected_prefix = [row["full_frame_sha256"] for row in audited_rows[:span]]
    prefix_matches = [
        index
        for index in range(len(extension_rows) - span + 1)
        if [row["full_frame_sha256"] for row in extension_rows[index : index + span]] == expected_prefix
    ]
    if len(prefix_matches) != 1:
        raise ExtractionError(f"extension/audit prefix RGB alignment is not unique: {prefix_matches}")
    prefix_start = prefix_matches[0]
    for offset in range(span):
        if extension_rows[prefix_start + offset]["source_pts"] != audited_rows[offset]["source_pts"]:
            raise ExtractionError("extension/audit prefix hash alignment has mismatched source PTS")
    target = audited_rows[-1]["full_frame_sha256"]
    terminal_matches = [index for index, row in enumerate(extension_rows) if row["full_frame_sha256"] == target]
    if len(terminal_matches) != 1:
        raise ExtractionError(f"extension/audit terminal RGB hash alignment is not unique: {terminal_matches}")
    terminal = terminal_matches[0]
    if terminal <= prefix_start + span:
        raise ExtractionError("extension alignment has no post-overlap frames")
    if extension_rows[terminal]["source_pts"] != audited_rows[-1]["source_pts"]:
        raise ExtractionError(
            f"extension/audit aligned hash has different source PTS: {extension_rows[terminal]['source_pts']} != {audited_rows[-1]['source_pts']}"
        )
    return {
        "prefix_decoded_index": prefix_start,
        "terminal_decoded_index": terminal,
        "span": span,
    }


def remote_run(host: str, command: list[str], *, text: bool = True) -> subprocess.CompletedProcess[Any]:
    return run(["ssh", host, *command], text=text)


def remote_sha_sizes(host: str, remote_dir: str, names: Iterable[str]) -> dict[str, dict[str, Any]]:
    names = list(names)
    if not names:
        return {}
    shell_names = " ".join(shlex_quote(name) for name in names)
    command = [
        f"cd {shlex_quote(remote_dir)} && sha256sum -- {shell_names} && stat -c '%n %s' -- {shell_names}",
    ]
    completed = remote_run(host, command)
    hashes: dict[str, dict[str, Any]] = {}
    for line in completed.stdout.splitlines():
        pieces = line.split()
        if len(pieces) == 2 and len(pieces[0]) == 64 and all(char in "0123456789abcdef" for char in pieces[0]):
            hashes.setdefault(Path(pieces[1]).name, {})["sha256"] = pieces[0]
        elif len(pieces) == 2 and pieces[1].isdigit():
            hashes.setdefault(Path(pieces[0]).name, {})["size_bytes"] = int(pieces[1])
    missing = [name for name in names if name not in hashes or set(hashes[name]) != {"sha256", "size_bytes"}]
    if missing:
        raise ExtractionError(f"remote hash/size query omitted {missing}")
    return hashes


def shlex_quote(value: str) -> str:
    # Keep the script dependency-free while quoting the only remote path input.
    return "'" + value.replace("'", "'\\''") + "'"


def retain_remote(
    host: str,
    remote_dir: str,
    audited_path: Path,
    extension_path: Path,
    staging: Path,
    png_names: list[str],
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    remote_run(host, ["mkdir", "-p", remote_dir])
    source_remote: dict[str, Any] = {}
    for local, name in ((audited_path, REMOTE_AUDITED_NAME), (extension_path, REMOTE_EXTENSION_NAME)):
        destination = f"{host}:{remote_dir}/{name}"
        run(["scp", "-q", str(local), destination])
        remote_hash = remote_sha_sizes(host, remote_dir, [name])[name]
        source_remote[name] = {
            "path": f"{remote_dir}/{name}",
            "sha256": remote_hash["sha256"],
            "size_bytes": remote_hash["size_bytes"],
        }
    png_paths = [str(staging / name) for name in png_names]
    if png_paths:
        run(["scp", "-q", *png_paths, f"{host}:{remote_dir}/"])
    png_remote = remote_sha_sizes(host, remote_dir, png_names)
    return source_remote, png_remote


def validate_png_file(path: Path, expected_width: int, expected_height: int, expected_rgb_hash: str) -> dict[str, Any]:
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise ExtractionError(f"cannot read PNG {path}: {exc}") from exc
    width, height, rgb = decode_png_rgb(data)
    if (width, height) != (expected_width, expected_height):
        raise ExtractionError(f"PNG {path} dimensions {(width, height)} != {(expected_width, expected_height)}")
    rgb_hash = sha256_bytes(rgb)
    if rgb_hash != expected_rgb_hash:
        raise ExtractionError(f"PNG {path} RGB hash {rgb_hash} != {expected_rgb_hash}")
    return {"sha256": sha256_bytes(data), "size_bytes": len(data), "width": width, "height": height}


def row_without_private(row: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in row.items() if not key.startswith("_")}


def validate_manifest_data(
    manifest: dict[str, Any],
    *,
    staging_dir: Path | None = None,
    remote_host: str | None = None,
) -> dict[str, Any]:
    rows = manifest.get("targets")
    if not isinstance(rows, list):
        raise ExtractionError("manifest targets must be a list")
    crop = manifest.get("crop")
    if not isinstance(crop, dict) or (crop.get("width"), crop.get("height")) != (340, 215):
        raise ExtractionError("manifest crop must be 340x215")
    ids = [str(row.get("id")) for row in rows]
    indices = [int(row.get("encoded_index")) for row in rows]
    if len(ids) != len(set(ids)) or len(indices) != len(set(indices)):
        raise ExtractionError("manifest target IDs/encoded indices are not unique")
    if indices != list(range(indices[0], indices[-1] + 1)):
        raise ExtractionError("manifest encoded indices are not contiguous")
    if len(rows) < 90:
        raise ExtractionError("manifest has fewer than 90 target rows")
    if sum(row.get("relation") == "pre_event" for row in rows) < 30:
        raise ExtractionError("manifest has fewer than 30 pre-event rows")
    event_rows = [row for row in rows if row.get("relation") == "event"]
    if [row.get("encoded_index") for row in event_rows] != [431, 432]:
        raise ExtractionError("manifest event rows are not exactly encoded indices 431/432")
    event_config = manifest.get("event", {})
    try:
        event_start = Fraction(event_config["source_interval_rational_seconds"][0])
        event_end = Fraction(event_config["source_interval_rational_seconds"][1])
    except (KeyError, IndexError, TypeError, ValueError, ZeroDivisionError) as exc:
        raise ExtractionError("manifest event interval is not exact rational metadata") from exc
    exact_event_indices = [
        int(row["encoded_index"])
        for row in rows
        if event_start <= Fraction(str(row["source_time_rational"])) < event_end
    ]
    if exact_event_indices != [431, 432]:
        raise ExtractionError(f"manifest exact-rational event membership is {exact_event_indices}, expected [431, 432]")
    if any(row.get("width") != 340 or row.get("height") != 215 for row in rows):
        raise ExtractionError("manifest target dimensions are inconsistent")
    if any(row.get("source_time_rational") is None or row.get("source_pts") is None for row in rows):
        raise ExtractionError("manifest target source times are incomplete")
    if any(Fraction(rows[index]["source_time_rational"]) >= Fraction(rows[index + 1]["source_time_rational"]) for index in range(len(rows) - 1)):
        raise ExtractionError("manifest source times are not strictly increasing")
    remote_pngs = manifest.get("remote_pngs")
    if not isinstance(remote_pngs, dict) or len(remote_pngs) != len(rows):
        raise ExtractionError("manifest remote_pngs does not cover every target")
    local_checks = 0
    for row in rows:
        name = Path(str(row["remote_png_path"])).name
        if name not in remote_pngs:
            raise ExtractionError(f"remote PNG metadata missing {name}")
        if staging_dir is not None:
            local = staging_dir / name
            check = validate_png_file(local, 340, 215, str(row["gameplay_crop_sha256"]))
            if check["sha256"] != remote_pngs[name]["sha256"] or check["size_bytes"] != remote_pngs[name]["size_bytes"]:
                raise ExtractionError(f"local PNG metadata mismatch for {name}")
            local_checks += 1
    remote_checks = 0
    if remote_host is not None:
        remote_dir = str(manifest["remote_result_dir"])
        observed = remote_sha_sizes(remote_host, remote_dir, list(remote_pngs))
        for name, expected in remote_pngs.items():
            if observed[name] != {"sha256": expected["sha256"], "size_bytes": expected["size_bytes"]}:
                raise ExtractionError(f"remote PNG metadata mismatch for {name}")
        remote_checks = len(observed)
    return {
        "valid": True,
        "target_count": len(rows),
        "encoded_index_range": [indices[0], indices[-1]],
        "pre_event_count": sum(row.get("relation") == "pre_event" for row in rows),
        "event_count": len(event_rows),
        "post_event_count": sum(row.get("relation") == "post_event" for row in rows),
        "local_png_checks": local_checks,
        "remote_png_checks": remote_checks,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Extract and validate hash-bound VOD fit target PNGs")
    parser.add_argument("--input-video", type=Path, default=Path("/tmp/ttc-vod-cadence-media/vod_segment.mp4"))
    parser.add_argument("--extension-video", type=Path, default=Path("/tmp/vod_extended.mp4"))
    parser.add_argument("--audit", type=Path, default=Path("results/vod_cadence_audit.json"))
    parser.add_argument("--output", type=Path, default=Path("results/vod_fit_targets.json"))
    parser.add_argument("--remote-host", default="roach")
    parser.add_argument("--remote-result-dir", default=f"{REMOTE_PROJECT}/{REMOTE_RESULT_DIR}")
    parser.add_argument("--start-index", type=int, default=DEFAULT_START_INDEX)
    parser.add_argument("--post-end-index", type=int, default=DEFAULT_POST_END_INDEX)
    parser.add_argument("--event-start", type=parse_fraction, default=DEFAULT_EVENT[0])
    parser.add_argument("--event-end", type=parse_fraction, default=DEFAULT_EVENT[1])
    parser.add_argument("--source-offset", type=parse_fraction, default=DEFAULT_SOURCE_OFFSET)
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--manifest", type=Path, default=None)
    parser.add_argument("--staging-dir", type=Path, default=None)
    parser.add_argument("--keep-staging", action="store_true")
    return parser


def extract(args: argparse.Namespace) -> dict[str, Any]:
    if args.start_index < 0 or args.post_end_index < args.start_index:
        raise ExtractionError("invalid target index range")
    if args.start_index > 431 or args.post_end_index < 432:
        raise ExtractionError("target range must include audited event frames 431/432")
    if args.event_start >= args.event_end:
        raise ExtractionError("event interval must be increasing")
    audit = load_audit(args.audit)
    if not args.input_video.is_file() or not args.extension_video.is_file():
        raise ExtractionError("both exact audited input and same-source extension are required")
    audited_hash = sha256_file(args.input_video)
    if audited_hash != EXPECTED_AUDIT_SHA256 or args.input_video.stat().st_size != EXPECTED_AUDIT_SIZE:
        raise ExtractionError(
            f"exact audited input mismatch: {audited_hash}/{args.input_video.stat().st_size} != {EXPECTED_AUDIT_SHA256}/{EXPECTED_AUDIT_SIZE}"
        )
    crop = DEFAULT_CROP
    _, audited_rows, _ = build_input_rows(
        args.input_video,
        source_offset=args.source_offset,
        crop=crop,
        audit_rows=audit["observations"]["all_decoded_frames"][:480],
    )
    _, extension_rows, extension_frames = build_input_rows(
        args.extension_video,
        source_offset=args.source_offset,
        crop=crop,
    )
    extension_alignment = align_extension(audited_rows, extension_rows)
    extension_terminal = extension_alignment["terminal_decoded_index"]
    extension_append = extension_terminal + 1
    extension_output_rows: list[dict[str, Any]] = []
    for ext_index in range(extension_append, len(extension_rows)):
        output_index = len(audited_rows) + (ext_index - extension_append)
        row = dict(extension_rows[ext_index])
        row["encoded_index"] = output_index
        row["originating_input"] = "same_source_extension"
        row["originating_input_sha256"] = sha256_file(args.extension_video)
        row["originating_decoded_index"] = ext_index
        row["_frame"] = extension_frames[ext_index]
        extension_output_rows.append(row)
    output_rows: list[dict[str, Any]] = []
    for index, row in enumerate(audited_rows):
        row = dict(row)
        row["encoded_index"] = index
        row["originating_input"] = "audited_format134_bounded_stream"
        row["originating_input_sha256"] = audited_hash
        row["originating_decoded_index"] = index
        row["_frame"] = None
        output_rows.append(row)
    all_source_rows = output_rows + extension_output_rows
    endpoint_candidates = [row for row in all_source_rows if row["encoded_index"] == args.post_end_index]
    if len(endpoint_candidates) != 1:
        raise ExtractionError(f"post endpoint {args.post_end_index} is unavailable in source inputs")
    selected = [row for row in all_source_rows if args.start_index <= row["encoded_index"] <= args.post_end_index]
    if len(selected) != args.post_end_index - args.start_index + 1:
        raise ExtractionError("selected target rows are not contiguous")
    # Exact audited RGB frames are retained separately so event rows cannot be
    # accidentally sourced from the extension overlap.
    audited_frames = decode_rgb(args.input_video, 640, 360, len(audited_rows))
    for row in output_rows:
        row["_frame"] = audited_frames[row["originating_decoded_index"]]
    for row in extension_output_rows:
        row["_frame"] = extension_frames[row["originating_decoded_index"]]

    audit_hash = sha256_file(args.audit)
    extension_hash = sha256_file(args.extension_video)
    config = {
        "source_offset": fraction_text(args.source_offset),
        "event_interval": [fraction_text(args.event_start), fraction_text(args.event_end)],
        "crop": {"x": crop[0], "y": crop[1], "width": crop[2], "height": crop[3]},
        "start_index": args.start_index,
        "post_end_index": args.post_end_index,
        "extension_alignment_span": extension_alignment["span"],
        "extension_alignment_prefix_decoded_index": extension_alignment["prefix_decoded_index"],
        "extension_alignment_terminal_decoded_index": extension_terminal,
        "extension_overlap_deduplicated": True,
        "landing_visible_index": 500,
        "landing_stability_hold_through_index": args.post_end_index,
    }
    staging = args.staging_dir or Path(tempfile.mkdtemp(prefix="vod-fit-targets-"))
    staging.mkdir(parents=True, exist_ok=True)
    local_png: dict[str, dict[str, Any]] = {}
    manifest_rows: list[dict[str, Any]] = []
    previous_selected: dict[str, Any] | None = None
    for row in selected:
        encoded_index = int(row["encoded_index"])
        source_time = Fraction(str(row["source_time_rational"]))
        in_event = args.event_start <= source_time < args.event_end
        if in_event:
            if encoded_index not in (431, 432):
                raise ExtractionError(f"unexpected exact-rational event member encoded index {encoded_index}")
            relation = "event"
        elif encoded_index < 431:
            relation = "pre_event"
        else:
            relation = "post_event"
        if row["originating_input"] == "audited_format134_bounded_stream" and encoded_index not in range(480):
            raise ExtractionError("audited-origin row index is outside the exact bounded stream")
        target_id = f"vod_{encoded_index:03d}"
        png_name = f"{target_id}.png"
        png_bytes = make_png_rgb(crop[2], crop[3], row["_crop_bytes"])
        png_path = staging / png_name
        png_path.write_bytes(png_bytes)
        decoded_width, decoded_height, decoded_rgb = decode_png_rgb(png_bytes)
        if (decoded_width, decoded_height) != (crop[2], crop[3]) or sha256_bytes(decoded_rgb) != row["gameplay_crop_sha256"]:
            raise ExtractionError(f"locally emitted PNG failed RGB verification: {png_name}")
        local_info = {"sha256": sha256_bytes(png_bytes), "size_bytes": len(png_bytes), "width": decoded_width, "height": decoded_height}
        local_png[png_name] = local_info
        row_public = row_without_private(row)
        row_public.update(
            {
                "id": target_id,
                "encoded_index": encoded_index,
                "relation": relation,
                "width": crop[2],
                "height": crop[3],
                "crop_x": crop[0],
                "crop_y": crop[1],
                "png_filename": png_name,
                "remote_png_path": f"{args.remote_result_dir}/{png_name}",
                "png_sha256": local_info["sha256"],
                "png_size_bytes": local_info["size_bytes"],
                "originating_input": row["originating_input"],
                "originating_input_sha256": row["originating_input_sha256"],
                "originating_decoded_index": row["originating_decoded_index"],
            }
        )
        manifest_rows.append(row_public)
    source_remote, remote_png = retain_remote(
        args.remote_host,
        args.remote_result_dir,
        args.input_video,
        args.extension_video,
        staging,
        list(local_png),
    )
    for row in manifest_rows:
        remote_info = remote_png[row["png_filename"]]
        if remote_info != {"sha256": row["png_sha256"], "size_bytes": row["png_size_bytes"]}:
            raise ExtractionError(f"remote PNG differs from local PNG: {row['png_filename']}")
    endpoint = next(row for row in manifest_rows if row["encoded_index"] == args.post_end_index)
    stability_rows = [row for row in manifest_rows if 500 <= row["encoded_index"] <= args.post_end_index]
    stability_means = [float(row["luma_difference_mean"]) for row in stability_rows if row["luma_difference_mean"] is not None]
    stability_dhash = [int(row["dhash_hamming_distance"]) for row in stability_rows if row["dhash_hamming_distance"] is not None]
    selected_source_deltas = [
        int(manifest_rows[index]["source_pts"]) - int(manifest_rows[index - 1]["source_pts"])
        for index in range(1, len(manifest_rows))
    ]
    delta_histogram: dict[str, int] = {}
    for delta in selected_source_deltas:
        delta_histogram[str(delta)] = delta_histogram.get(str(delta), 0) + 1
    nonuniform_delta_indices = [
        manifest_rows[index]["encoded_index"]
        for index in range(1, len(manifest_rows))
        if selected_source_deltas[index - 1] != 1001
    ]
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "manifest_kind": "vod_fit_targets",
        "scope": "encoded VOD pixels only; no N64 VI/game-update or mechanism inference",
        "generator": {
            "path": str(Path(__file__).resolve()),
            "sha256": sha256_file(Path(__file__).resolve()),
            "python_version": platform.python_version(),
            "platform": platform.platform(),
        },
        "inputs": {
            "audited_format134_bounded_stream": {
                "local_path": str(args.input_video),
                "sha256": audited_hash,
                "size_bytes": args.input_video.stat().st_size,
                "remote_path": source_remote[REMOTE_AUDITED_NAME]["path"],
                "remote_sha256": source_remote[REMOTE_AUDITED_NAME]["sha256"],
                "remote_size_bytes": source_remote[REMOTE_AUDITED_NAME]["size_bytes"],
                "format_id": "134",
                "source_id": "TTh3LY-5KKg",
                "source_url": "https://www.youtube.com/watch?v=TTh3LY-5KKg",
                "retrieval_command": "yt-dlp --ignore-config --no-warnings --no-playlist --format 134 --download-sections '*3467.0-3483.0' --output '/tmp/ttc-vod-cadence-media/vod_segment.%(ext)s' --no-part 'https://www.youtube.com/watch?v=TTh3LY-5KKg'",
                "audit_sha256": audit.get("input", {}).get("sha256"),
                "audit_size_bytes": audit.get("input", {}).get("size_bytes"),
            },
            "same_source_format134_extension": {
                "local_path": str(args.extension_video),
                "sha256": extension_hash,
                "size_bytes": args.extension_video.stat().st_size,
                "remote_path": source_remote[REMOTE_EXTENSION_NAME]["path"],
                "remote_sha256": source_remote[REMOTE_EXTENSION_NAME]["sha256"],
                "remote_size_bytes": source_remote[REMOTE_EXTENSION_NAME]["size_bytes"],
                "format_id": "134",
                "source_id": "TTh3LY-5KKg",
                "source_url": "https://www.youtube.com/watch?v=TTh3LY-5KKg",
                "retrieval_command": "yt-dlp --ignore-config --no-warnings --no-playlist --format 134 --download-sections '*3467-3488' --output '/tmp/vod_extended.%(ext)s' --no-part 'https://www.youtube.com/watch?v=TTh3LY-5KKg'",
                "purpose": "same-source post-event continuation; not mixed with another VOD",
            },
            "cadence_audit": {
                "path": str(args.audit),
                "sha256": audit_hash,
                "input_sha256": audit.get("input", {}).get("sha256"),
                "source_mapping": audit.get("observations", {}).get("source_time_mapping"),
                "event_interval": audit.get("observations", {}).get("event_interval"),
            },
        },
        "tools": {
            "ffmpeg": executable_record("ffmpeg"),
            "ffprobe": executable_record("ffprobe"),
            "decoder_command": ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", "<input>", "-map", "0:v:0", "-fps_mode", "passthrough", "-pix_fmt", "rgb24", "-f", "rawvideo", ""],
            "png_encoder": "deterministic Python zlib PNG: filter=0, RGB8, compression_level=9",
        },
        "config": config,
        "config_sha256": sha256_bytes(canonical_json(config)),
        "crop": {
            "x": crop[0],
            "y": crop[1],
            "width": crop[2],
            "height": crop[3],
            "coordinate_space": "decoded 640x360 RGB frame pixels, origin top-left",
            "selection_basis": "audited left-bottom DOTA gameplay pane",
        },
        "event": {
            "source_interval_rational_seconds": [fraction_text(args.event_start), fraction_text(args.event_end)],
            "source_interval_decimal_seconds": [decimal_text(args.event_start), decimal_text(args.event_end)],
            "semantics": "half-open start-inclusive/end-exclusive",
            "mapped_encoded_indices": [431, 432],
            "mapped_source_pts": [audited_rows[431]["source_pts"], audited_rows[432]["source_pts"]],
            "mapping_method": "audited verified source-PTS alignment; exact rational arithmetic",
        },
        "cadence": {
            "timebase": "1/30000",
            "nominal_delta_pts": 1001,
            "selected_source_pts_delta_histogram": dict(sorted(delta_histogram.items(), key=lambda item: int(item[0]))),
            "nonuniform_delta_after_encoded_indices": nonuniform_delta_indices,
            "nonuniform_delta_justification": "The audited bounded edit omits source PTS 479479; audited row 479 is PTS 480480. The same-source extension supplies later PTS-contiguous rows after the ext480/audit479 overlap without inventing the omitted frame.",
            "encoded_indices_are_contiguous": [row["encoded_index"] for row in manifest_rows] == list(range(args.start_index, args.post_end_index + 1)),
        },
        "selection": {
            "encoded_index_range": [args.start_index, args.post_end_index],
            "target_count": len(manifest_rows),
            "pre_event_count": sum(row["relation"] == "pre_event" for row in manifest_rows),
            "event_count": sum(row["relation"] == "event" for row in manifest_rows),
            "post_event_count": sum(row["relation"] == "post_event" for row in manifest_rows),
            "ids_contiguous": [row["encoded_index"] for row in manifest_rows] == list(range(args.start_index, args.post_end_index + 1)),
            "landing_visible_encoded_index": 500,
            "endpoint_encoded_index": args.post_end_index,
            "endpoint_source_time_rational": endpoint["source_time_rational"],
            "endpoint_source_time_seconds": endpoint["source_time_seconds"],
            "endpoint_basis": "pixel-only endpoint review: Mario is visibly on the lower cage floor by encoded frame 500; retain every subsequent encoded crop through frame 530 (31-frame hold) to cover completed landing stabilization, without a Mario localizer or N64 timing claim",
            "endpoint_pixel_stability_window": {
                "start_encoded_index": 500,
                "end_encoded_index": args.post_end_index,
                "frame_count": len(stability_rows),
                "luma_difference_mean_average": decimal_text(Fraction(str(sum(stability_means) / len(stability_means))).limit_denominator(1000000), 6) if stability_means else None,
                "luma_difference_mean_max": decimal_text(Fraction(str(max(stability_means))).limit_denominator(1000000), 6) if stability_means else None,
                "dhash_distance_max": max(stability_dhash) if stability_dhash else None,
                "scene_change_count": sum(bool(row["scene_change"]) for row in stability_rows),
            },
        },
        "alignment": {
            "method": "unique_contiguous_8_frame_decoded_RGB_SHA256_prefix_match_plus_terminal_RGB_SHA256_and_source_PTS",
            "alignment_span": extension_alignment["span"],
            "extension_prefix_decoded_index": extension_alignment["prefix_decoded_index"],
            "extension_terminal_decoded_index": extension_terminal,
            "extension_terminal_source_pts": extension_rows[extension_terminal]["source_pts"],
            "audited_terminal_source_pts": audited_rows[-1]["source_pts"],
            "extension_omitted_decoded_index": extension_terminal - 1,
            "extension_omitted_source_pts": extension_rows[extension_terminal - 1]["source_pts"],
            "extension_omitted_full_frame_sha256": extension_rows[extension_terminal - 1]["full_frame_sha256"],
            "overlap_deduplicated": True,
            "extension_appended_decoded_index_start": extension_append,
            "extension_appended_output_encoded_index_start": len(audited_rows),
            "note": "The extension contains an extra PTS-479 frame omitted by the audited section edit; ext frame 480 is the unique RGB-identical overlap with audited row 479 and is not emitted twice.",
        },
        "remote_result_dir": args.remote_result_dir,
        "remote_pngs": remote_png,
        "targets": manifest_rows,
        "limitations": [
            "Rows are decoded H.264/composite VOD pixels, not original N64 VI or game-update observations.",
            "No Mario bounding boxes or localization confidence are recorded; no deterministic auditable localizer was used.",
            "The post-event endpoint is a pixel-reviewed landing/stabilization endpoint, not a claim about game state or mechanism.",
            "The two source files are both format-134 retrievals from TTh3LY-5KKg; the extension is explicitly hash-bound to the audited prefix and the overlap is de-duplicated.",
        ],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    validation = validate_manifest_data(manifest, staging_dir=staging, remote_host=args.remote_host)
    manifest["validation"] = validation
    args.output.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if not args.keep_staging and args.staging_dir is None:
        shutil.rmtree(staging, ignore_errors=True)
    return validation


def main() -> int:
    args = build_parser().parse_args()
    if args.validate_only:
        manifest_path = args.manifest or args.output
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        staging = args.staging_dir
        if staging is None and args.remote_host is not None:
            staging = Path(tempfile.mkdtemp(prefix="vod-fit-validate-"))
            names = [Path(str(row["remote_png_path"])).name for row in manifest["targets"]]
            remote_dir = str(manifest["remote_result_dir"])
            run(["scp", "-q", *[f"{args.remote_host}:{remote_dir}/{name}" for name in names], str(staging)])
        result = validate_manifest_data(manifest, staging_dir=staging, remote_host=args.remote_host)
        if staging is not None and args.staging_dir is None:
            shutil.rmtree(staging, ignore_errors=True)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    result = extract(args)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ExtractionError, OSError, json.JSONDecodeError) as exc:
        print(f"extract_vod_fit_targets.py: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
