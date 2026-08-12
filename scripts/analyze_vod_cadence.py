#!/usr/bin/env python3
"""Analyze encoded-frame cadence in a bounded VOD clip.

The analyzer deliberately treats the input as an encoded video observation.  It
never maps visual changes to N64 game updates.  ffprobe supplies exact decoded
frame PTS values and ffmpeg supplies one RGB24 buffer per decoded frame; all
hashes and fingerprints are derived from those buffers without OpenCV.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import platform
import subprocess
import sys
from fractions import Fraction
from pathlib import Path
from typing import Any, BinaryIO


ANALYZER_SCHEMA_VERSION = 1
ANALYZER_VERSION = "1"
LOW_RESOLUTION = (32, 18)
NEAR_REPEAT_LUMA_MAE_MAX = 2
NEAR_REPEAT_DHASH_HAMMING_MAX = 24
SCENE_CHANGE_LUMA_MAE_MIN = 20
SCENE_CHANGE_DHASH_HAMMING_MIN = 192
DEFAULT_SOURCE_ID = "TTh3LY-5KKg"
DEFAULT_SOURCE_URL = "https://www.youtube.com/watch?v=TTh3LY-5KKg"
DEFAULT_RETRIEVAL_FORMAT = "134"


class ToolFailure(RuntimeError):
    """A required media tool failed or emitted unusable output."""


def parse_fraction(text: str) -> Fraction:
    try:
        value = Fraction(text)
    except (TypeError, ValueError, ZeroDivisionError) as exc:
        raise argparse.ArgumentTypeError(f"not a finite rational value: {text!r}") from exc
    if value.denominator <= 0:
        raise argparse.ArgumentTypeError(f"invalid rational value: {text!r}")
    return value


def fraction_text(value: Fraction) -> str:
    if value.denominator == 1:
        return str(value.numerator)
    return f"{value.numerator}/{value.denominator}"


def decimal_text(value: Fraction, places: int = 12) -> str:
    """Return deterministic decimal text, rounded half-up, without float use."""
    sign = "-" if value < 0 else ""
    value = abs(value)
    whole, remainder = divmod(value.numerator, value.denominator)
    if remainder == 0:
        return f"{sign}{whole}"
    digits: list[str] = []
    denominator = value.denominator
    for _ in range(places):
        remainder *= 10
        digit, remainder = divmod(remainder, denominator)
        digits.append(str(digit))
    if remainder * 2 >= denominator:
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
    decimal = "".join(digits).rstrip("0")
    return f"{sign}{whole}.{decimal or '0'}"


def parse_timebase(text: str) -> tuple[int, int]:
    try:
        numerator_text, denominator_text = text.split("/", 1)
        numerator = int(numerator_text)
        denominator = int(denominator_text)
    except (AttributeError, ValueError) as exc:
        raise ToolFailure(f"invalid ffprobe time base: {text!r}") from exc
    if numerator <= 0 or denominator <= 0:
        raise ToolFailure(f"invalid ffprobe time base: {text!r}")
    return numerator, denominator


def run_json(command: list[str]) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            command,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        stderr = getattr(exc, "stderr", "") or ""
        raise ToolFailure(
            f"command failed: {' '.join(command)}\n{stderr.strip()}"
        ) from exc
    try:
        parsed = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ToolFailure(
            f"command did not emit JSON: {' '.join(command)}"
        ) from exc
    if not isinstance(parsed, dict):
        raise ToolFailure(f"command emitted non-object JSON: {' '.join(command)}")
    return parsed


def tool_version(executable: str) -> str:
    try:
        completed = subprocess.run(
            [executable, "-version"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ToolFailure(f"cannot obtain {executable} version") from exc
    first_line = completed.stdout.splitlines()[0] if completed.stdout.splitlines() else ""
    if not first_line:
        raise ToolFailure(f"{executable} emitted no version")
    return first_line


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
    except OSError as exc:
        raise ToolFailure(f"cannot hash input {path}") from exc
    return digest.hexdigest()


def read_exact(stream: BinaryIO, size: int) -> bytes:
    data = bytearray()
    while len(data) < size:
        chunk = stream.read(size - len(data))
        if not chunk:
            break
        data.extend(chunk)
    return bytes(data)


def crop_rgb(frame: bytes, width: int, crop: tuple[int, int, int, int]) -> bytes:
    x, y, crop_width, crop_height = crop
    row_stride = width * 3
    crop_stride = crop_width * 3
    cropped = bytearray(crop_stride * crop_height)
    destination = 0
    for row in range(y, y + crop_height):
        start = row * row_stride + x * 3
        cropped[destination : destination + crop_stride] = frame[start : start + crop_stride]
        destination += crop_stride
    return bytes(cropped)


def sampled_luma(
    frame: bytes,
    frame_width: int,
    frame_height: int,
    crop: tuple[int, int, int, int],
) -> bytes:
    """Sample a fixed 32x18 crop-centered luma grid.

    The sample point is the center of each output cell.  BT.709 integer
    coefficients are rounded to nearest: (54R + 183G + 19B + 128) // 256.
    """
    crop_x, crop_y, crop_width, crop_height = crop
    low_width, low_height = LOW_RESOLUTION
    row_stride = frame_width * 3
    values = bytearray(low_width * low_height)
    destination = 0
    for output_y in range(low_height):
        source_y = crop_y + ((2 * output_y + 1) * crop_height) // (2 * low_height)
        source_y = min(frame_height - 1, source_y)
        for output_x in range(low_width):
            source_x = crop_x + ((2 * output_x + 1) * crop_width) // (2 * low_width)
            source_x = min(frame_width - 1, source_x)
            source = source_y * row_stride + source_x * 3
            red, green, blue = frame[source : source + 3]
            values[destination] = (54 * red + 183 * green + 19 * blue + 128) // 256
            destination += 1
    return bytes(values)


def dhash(luma: bytes) -> str:
    width, height = LOW_RESOLUTION
    bit_count = height * (width - 1)
    packed = bytearray((bit_count + 7) // 8)
    bit_index = 0
    for row in range(height):
        row_start = row * width
        for column in range(width - 1):
            if luma[row_start + column] > luma[row_start + column + 1]:
                packed[bit_index // 8] |= 1 << (7 - (bit_index % 8))
            bit_index += 1
    return packed.hex()


def hamming_hex(left: str, right: str) -> int:
    left_bytes = bytes.fromhex(left)
    right_bytes = bytes.fromhex(right)
    return sum((a ^ b).bit_count() for a, b in zip(left_bytes, right_bytes))


def stream_probe(input_path: Path) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    stream_json = run_json(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_streams",
            "-show_format",
            "-of",
            "json",
            str(input_path),
        ]
    )
    streams = stream_json.get("streams")
    if not isinstance(streams, list) or not streams:
        raise ToolFailure("ffprobe found no video stream")
    stream = streams[0]
    if not isinstance(stream, dict):
        raise ToolFailure("ffprobe returned malformed video stream")
    frame_json = run_json(
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
            str(input_path),
        ]
    )
    frames = frame_json.get("frames")
    if not isinstance(frames, list):
        raise ToolFailure("ffprobe returned no decoded frame list")
    packet_json = run_json(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_packets",
            "-show_entries",
            "packet=pts,dts,duration,flags,size",
            "-of",
            "json",
            str(input_path),
        ]
    )
    packets = packet_json.get("packets")
    if not isinstance(packets, list):
        raise ToolFailure("ffprobe returned no packet list")
    return stream, frames, packets


def int_field(row: dict[str, Any], name: str, default: int | None = None) -> int | None:
    value = row.get(name, default)
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ToolFailure(f"ffprobe frame field {name!r} is not an integer") from exc


def frame_metadata(raw_frames: list[dict[str, Any]]) -> list[dict[str, Any]]:
    metadata: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_frames):
        if not isinstance(raw, dict):
            raise ToolFailure(f"ffprobe frame {index} is malformed")
        pts = int_field(raw, "best_effort_timestamp")
        if pts is None:
            raise ToolFailure(f"ffprobe frame {index} has no best-effort PTS")
        metadata.append(
            {
                "index": index,
                "pts": pts,
                "duration": int_field(raw, "pkt_duration"),
                "key_frame": bool(int_field(raw, "key_frame", 0)),
                "pict_type": str(raw.get("pict_type", "UNKNOWN")),
                "coded_picture_number": int_field(raw, "coded_picture_number"),
                "display_picture_number": int_field(raw, "display_picture_number"),
            }
        )
    return metadata


def packet_summary(packets: list[dict[str, Any]]) -> dict[str, Any]:
    parsed: list[dict[str, int | str]] = []
    for index, packet in enumerate(packets):
        if not isinstance(packet, dict) or packet.get("pts") is None:
            continue
        try:
            pts = int(packet["pts"])
            dts = int(packet["dts"]) if packet.get("dts") is not None else None
            duration = int(packet["duration"]) if packet.get("duration") is not None else None
        except (TypeError, ValueError) as exc:
            raise ToolFailure(f"ffprobe packet {index} has malformed timestamp") from exc
        parsed.append({"pts": pts, "dts": dts, "duration": duration, "flags": str(packet.get("flags", ""))})
    if not parsed:
        raise ToolFailure("ffprobe returned no timestamped packets")
    nonnegative = [row for row in parsed if int(row["pts"]) >= 0]
    return {
        "packet_count": len(parsed),
        "negative_pts_packet_count": sum(int(row["pts"]) < 0 for row in parsed),
        "first_packet_pts": int(parsed[0]["pts"]),
        "first_nonnegative_packet_pts": int(nonnegative[0]["pts"]) if nonnegative else None,
        "last_packet_pts": int(parsed[-1]["pts"]),
        "first_packet_flags": str(parsed[0]["flags"]),
    }


def analyze_frames(
    input_path: Path,
    stream: dict[str, Any],
    metadata: list[dict[str, Any]],
    crop: tuple[int, int, int, int],
    timebase: tuple[int, int],
    source_offset: Fraction,
    requested_source_offset: Fraction,
) -> list[dict[str, Any]]:
    width = int(stream.get("width", 0))
    height = int(stream.get("height", 0))
    if width <= 0 or height <= 0:
        raise ToolFailure("ffprobe returned invalid video dimensions")
    frame_size = width * height * 3
    decoder_command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(input_path),
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
    try:
        process = subprocess.Popen(
            decoder_command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError as exc:
        raise ToolFailure("cannot start ffmpeg decoder") from exc
    if process.stdout is None or process.stderr is None:
        raise ToolFailure("ffmpeg decoder pipes unavailable")

    rows: list[dict[str, Any]] = []
    previous_full_hash: str | None = None
    previous_crop_hash: str | None = None
    previous_luma: bytes | None = None
    previous_dhash: str | None = None
    try:
        for index, frame in enumerate(metadata):
            rgb = read_exact(process.stdout, frame_size)
            if len(rgb) != frame_size:
                raise ToolFailure(
                    f"ffmpeg decoded {len(rgb)} bytes for frame {index}, expected {frame_size}"
                )
            full_hash = hashlib.sha256(rgb).hexdigest()
            crop_rgb_bytes = crop_rgb(rgb, width, crop)
            crop_hash = hashlib.sha256(crop_rgb_bytes).hexdigest()
            luma = sampled_luma(rgb, width, height, crop)
            luma_hash = hashlib.sha256(luma).hexdigest()
            current_dhash = dhash(luma)
            clip_pts = int(frame["pts"])
            clip_seconds = Fraction(clip_pts * timebase[0], timebase[1])
            source_seconds = source_offset + clip_seconds
            source_ticks = source_seconds / Fraction(timebase[0], timebase[1])
            if source_ticks.denominator != 1:
                source_pts: int | None = None
            else:
                source_pts = source_ticks.numerator
            luma_diff_sum: int | None = None
            luma_diff_mean: str | None = None
            dhash_distance: int | None = None
            full_exact = previous_full_hash is not None and full_hash == previous_full_hash
            crop_exact = previous_crop_hash is not None and crop_hash == previous_crop_hash
            if previous_luma is not None:
                luma_diff_sum = sum(abs(a - b) for a, b in zip(luma, previous_luma))
                luma_diff_mean = decimal_text(
                    Fraction(luma_diff_sum, len(luma)), places=6
                )
            if previous_dhash is not None:
                dhash_distance = hamming_hex(current_dhash, previous_dhash)
            near_repeat = (
                not full_exact
                and not crop_exact
                and luma_diff_sum is not None
                and luma_diff_sum <= NEAR_REPEAT_LUMA_MAE_MAX * len(luma)
                and dhash_distance is not None
                and dhash_distance <= NEAR_REPEAT_DHASH_HAMMING_MAX
            )
            scene_change = (
                luma_diff_sum is not None
                and (
                    luma_diff_sum >= SCENE_CHANGE_LUMA_MAE_MIN * len(luma)
                    or (dhash_distance is not None and dhash_distance >= SCENE_CHANGE_DHASH_HAMMING_MIN)
                )
            )
            if previous_full_hash is None:
                repeat_class = "first_frame"
            elif full_exact:
                repeat_class = "exact_full_frame"
            elif crop_exact:
                repeat_class = "exact_gameplay_crop"
            elif near_repeat:
                repeat_class = "near_gameplay_crop"
            else:
                repeat_class = "not_repeat"
            rows.append(
                {
                    "index": index,
                    "clip_pts": clip_pts,
                    "clip_timebase": f"{timebase[0]}/{timebase[1]}",
                    "clip_time_rational": fraction_text(clip_seconds),
                    "clip_time_seconds": decimal_text(clip_seconds),
                    "source_pts": source_pts,
                    "source_time_rational": fraction_text(source_seconds),
                    "source_time_seconds": decimal_text(source_seconds),
                    "key_frame": bool(frame["key_frame"]),
                    "picture_type": frame["pict_type"],
                    "coded_picture_number": frame["coded_picture_number"],
                    "display_picture_number": frame["display_picture_number"],
                    "full_frame_sha256": full_hash,
                    "gameplay_crop_sha256": crop_hash,
                    "luma_grid_sha256": luma_hash,
                    "luma_dhash_hex": current_dhash,
                    "luma_difference_sum": luma_diff_sum,
                    "luma_difference_mean": luma_diff_mean,
                    "dhash_hamming_distance": dhash_distance,
                    "repeat_class": repeat_class,
                    "full_frame_exact_repeat": full_exact,
                    "gameplay_crop_exact_repeat": crop_exact,
                    "near_repeat": near_repeat,
                    "scene_change": scene_change,
                }
            )
            previous_full_hash = full_hash
            previous_crop_hash = crop_hash
            previous_luma = luma
            previous_dhash = current_dhash
        trailing = process.stdout.read(1)
        if trailing:
            raise ToolFailure("ffmpeg emitted more RGB data than ffprobe frame count")
    finally:
        process.stdout.close()
    stderr = process.stderr.read().decode("utf-8", errors="replace")
    return_code = process.wait()
    if return_code != 0:
        raise ToolFailure(f"ffmpeg decoder exited {return_code}: {stderr.strip()}")
    if len(rows) != len(metadata):
        raise ToolFailure("decoded frame count does not match ffprobe frame count")
    return rows


def cadence_summary(rows: list[dict[str, Any]], timebase: tuple[int, int]) -> dict[str, Any]:
    deltas = [rows[index]["clip_pts"] - rows[index - 1]["clip_pts"] for index in range(1, len(rows))]
    histogram: dict[str, int] = {}
    for delta in deltas:
        key = str(delta)
        histogram[key] = histogram.get(key, 0) + 1
    nominal_pts = 1001 if timebase == (1, 30000) else None
    nominal_seconds = "1001/30000" if timebase == (1, 30000) else None
    return {
        "decoded_frame_count": len(rows),
        "first_clip_pts": rows[0]["clip_pts"] if rows else None,
        "last_clip_pts": rows[-1]["clip_pts"] if rows else None,
        "delta_pts_histogram": dict(sorted(histogram.items(), key=lambda item: int(item[0]))),
        "nonuniform_delta_count": sum(1 for delta in deltas if delta != deltas[0]) if deltas else 0,
        "all_decoded_pts_strictly_increasing": all(delta > 0 for delta in deltas),
        "nominal_frame_interval_pts": nominal_pts,
        "nominal_frame_interval_seconds_rational": nominal_seconds,
        "nominal_capture_rate": "30000/1001" if timebase == (1, 30000) else None,
    }


def event_selection(
    rows: list[dict[str, Any]],
    event_start: Fraction,
    event_end: Fraction,
    requested_offset: Fraction,
    source_offset: Fraction,
    timebase: tuple[int, int],
    padding_frames: int,
) -> dict[str, Any]:
    clip_timebase = Fraction(timebase[0], timebase[1])
    requested_indices = [
        row["index"]
        for row in rows
        if requested_offset + row["clip_pts"] * clip_timebase >= event_start
        and requested_offset + row["clip_pts"] * clip_timebase < event_end
    ]
    mapped_indices = [
        row["index"]
        for row in rows
        if Fraction(row["source_time_rational"]) >= event_start
        and Fraction(row["source_time_rational"]) < event_end
    ]
    selected = mapped_indices or requested_indices
    if selected:
        first = max(0, min(selected) - padding_frames)
        last = min(len(rows) - 1, max(selected) + padding_frames)
        window_indices = list(range(first, last + 1))
    else:
        target = event_start - source_offset
        nearest = min(
            range(len(rows)),
            key=lambda index: abs(rows[index]["clip_pts"] * clip_timebase - target),
        )
        first = max(0, nearest - padding_frames)
        last = min(len(rows) - 1, nearest + padding_frames)
        window_indices = list(range(first, last + 1))
    return {
        "source_interval_seconds": [decimal_text(event_start), decimal_text(event_end)],
        "semantics": "half_open_start_inclusive_end_exclusive",
        "requested_offset_clip_interval_seconds": [
            decimal_text(event_start - requested_offset),
            decimal_text(event_end - requested_offset),
        ],
        "mapped_source_offset_clip_interval_seconds": [
            decimal_text(event_start - source_offset),
            decimal_text(event_end - source_offset),
        ],
        "requested_offset_frame_indices": requested_indices,
        "mapped_source_frame_indices": mapped_indices,
        "selected_frame_indices_for_window": selected,
        "adjacent_padding_encoded_frames": padding_frames,
        "window_first_frame_index": first,
        "window_last_frame_index": last,
        "window_frame_indices": window_indices,
    }


def apply_source_offset(
    rows: list[dict[str, Any]],
    timebase: tuple[int, int],
    source_offset: Fraction,
) -> None:
    """Map decoded clip PTS values onto exact source-timeline times."""
    clip_timebase = Fraction(timebase[0], timebase[1])
    for row in rows:
        source_seconds = source_offset + int(row["clip_pts"]) * clip_timebase
        source_ticks = source_seconds / clip_timebase
        row["source_pts"] = (
            source_ticks.numerator if source_ticks.denominator == 1 else None
        )
        row["source_time_rational"] = fraction_text(source_seconds)
        row["source_time_seconds"] = decimal_text(source_seconds)


def verify_source_probe_alignment(
    probe_path: Path,
    clip_rows: list[dict[str, Any]],
    clip_timebase: tuple[int, int],
    expected_dimensions: tuple[int, int],
    crop: tuple[int, int, int, int],
) -> tuple[Fraction, dict[str, Any]]:
    """Locate clip frame zero in a source-PTS-preserving probe by RGB hashes."""
    probe_stream, raw_probe_frames, _ = stream_probe(probe_path)
    try:
        probe_timebase = parse_timebase(str(probe_stream["time_base"]))
        probe_dimensions = (
            int(probe_stream["width"]),
            int(probe_stream["height"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ToolFailure("source-start probe lacks valid dimensions/timebase") from exc
    if probe_dimensions != expected_dimensions:
        raise ToolFailure(
            "source-start probe dimensions differ from bounded clip: "
            f"{probe_dimensions} != {expected_dimensions}"
        )
    probe_metadata = frame_metadata(raw_probe_frames)
    if not probe_metadata:
        raise ToolFailure("source-start probe has no decoded frames")
    probe_rows = analyze_frames(
        probe_path,
        probe_stream,
        probe_metadata,
        crop,
        probe_timebase,
        Fraction(0),
        Fraction(0),
    )
    match_span = min(8, len(clip_rows))
    if match_span == 0:
        raise ToolFailure("bounded clip has no decoded frames to align")
    clip_hashes = [
        str(row["full_frame_sha256"]) for row in clip_rows[:match_span]
    ]
    probe_hashes = [str(row["full_frame_sha256"]) for row in probe_rows]
    matching_indices = [
        index
        for index in range(len(probe_rows) - match_span + 1)
        if probe_hashes[index : index + match_span] == clip_hashes
    ]
    if len(matching_indices) != 1:
        raise ToolFailure(
            "source-start probe RGB alignment is not unique: "
            f"{len(matching_indices)} matches for {match_span} frames"
        )
    match_index = matching_indices[0]
    matched = probe_rows[match_index]
    matched_pts = int(matched["clip_pts"])
    matched_source_time = Fraction(
        matched_pts * probe_timebase[0],
        probe_timebase[1],
    )
    clip_frame_zero_pts = int(clip_rows[0]["clip_pts"])
    clip_frame_zero_time = (
        clip_frame_zero_pts * Fraction(clip_timebase[0], clip_timebase[1])
    )
    source_offset = matched_source_time - clip_frame_zero_time
    return source_offset, {
        "method": "unique_contiguous_decoded_rgb_sha256_match",
        "matched_probe_frame_index": match_index,
        "matched_probe_pts": matched_pts,
        "matched_probe_timebase": (
            f"{probe_timebase[0]}/{probe_timebase[1]}"
        ),
        "matched_probe_time_rational_seconds": fraction_text(matched_source_time),
        "matched_probe_time_decimal_seconds": decimal_text(matched_source_time),
        "clip_frame_zero_pts": clip_frame_zero_pts,
        "clip_frame_zero_timebase": f"{clip_timebase[0]}/{clip_timebase[1]}",
        "clip_frame_zero_time_rational_seconds": fraction_text(
            clip_frame_zero_time
        ),
        "source_timeline_offset_rational_seconds": fraction_text(source_offset),
        "source_timeline_offset_decimal_seconds": decimal_text(source_offset),
        "matched_frame_span": match_span,
        "matching_probe_frame_indices": matching_indices,
        "clip_frame_zero_full_frame_sha256": clip_hashes[0],
        "matched_probe_frame_full_frame_sha256": str(
            matched["full_frame_sha256"]
        ),
        "probe_decoded_frame_count": len(probe_rows),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Hash and classify every decoded encoded frame in a bounded VOD clip"
    )
    parser.add_argument("--input-video", required=True, type=Path)
    parser.add_argument(
        "--source-offset",
        required=True,
        type=parse_fraction,
        help="requested source start in seconds; fallback origin when no PTS-preserving probe is supplied",
    )
    parser.add_argument(
        "--event-interval",
        required=True,
        nargs=2,
        type=parse_fraction,
        metavar=("START", "END"),
        help="source seconds, interpreted as [START, END)",
    )
    parser.add_argument(
        "--crop",
        required=True,
        nargs=4,
        type=int,
        metavar=("X", "Y", "WIDTH", "HEIGHT"),
        help="DOTA gameplay crop rectangle in decoded pixels",
    )
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--source-id", default=DEFAULT_SOURCE_ID)
    parser.add_argument("--source-url", default=DEFAULT_SOURCE_URL)
    parser.add_argument("--format-id", default=DEFAULT_RETRIEVAL_FORMAT)
    parser.add_argument("--yt-dlp-version", default=None)
    parser.add_argument("--retrieval-command", default=None)
    parser.add_argument(
        "--source-start-probe-path",
        type=Path,
        default=None,
        help="PTS-preserving copy of the bounded source; decoded RGB frames are aligned automatically",
    )
    parser.add_argument("--source-start-probe-command", default=None)
    parser.add_argument("--event-padding-frames", type=int, default=5)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    input_path = args.input_video
    if not input_path.is_file():
        raise ToolFailure(f"input video does not exist: {input_path}")
    if args.event_padding_frames < 0:
        raise ToolFailure("event padding must be nonnegative")
    event_start, event_end = args.event_interval
    if event_start >= event_end:
        raise ToolFailure("event interval must have START < END")
    requested_offset: Fraction = args.source_offset
    crop = tuple(args.crop)
    if len(crop) != 4 or crop[2] <= 0 or crop[3] <= 0 or crop[0] < 0 or crop[1] < 0:
        raise ToolFailure("crop must be nonnegative X/Y and positive WIDTH/HEIGHT")
    stream, raw_frames, packets = stream_probe(input_path)
    try:
        timebase = parse_timebase(str(stream["time_base"]))
        width = int(stream["width"])
        height = int(stream["height"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ToolFailure("ffprobe stream lacks valid dimensions/timebase") from exc
    if crop[0] + crop[2] > width or crop[1] + crop[3] > height:
        raise ToolFailure(f"crop {crop} exceeds decoded frame {width}x{height}")
    source_offset = requested_offset
    source_mapping_method = "requested_offset_plus_clip_best_effort_pts_inferred"
    source_mapping_exact = False
    metadata = frame_metadata(raw_frames)
    if not metadata:
        raise ToolFailure("no decoded frames")
    rows = analyze_frames(
        input_path,
        stream,
        metadata,
        crop,
        timebase,
        source_offset,
        requested_offset,
    )
    input_hash = sha256_file(input_path)
    script_path = Path(__file__).resolve()
    source_probe_hash = None
    source_probe_input: dict[str, Any] | None = None
    source_probe_alignment: dict[str, Any] | None = None
    if args.source_start_probe_path is not None:
        if not args.source_start_probe_path.is_file():
            raise ToolFailure(
                f"source-start probe does not exist: {args.source_start_probe_path}"
            )
        source_probe_hash = sha256_file(args.source_start_probe_path)
        source_probe_input = {
            "path": str(args.source_start_probe_path),
            "resolved_path": str(args.source_start_probe_path.resolve()),
            "sha256": source_probe_hash,
            "size_bytes": args.source_start_probe_path.stat().st_size,
        }
        source_offset, source_probe_alignment = (
            verify_source_probe_alignment(
                args.source_start_probe_path,
                rows,
                timebase,
                (width, height),
                crop,
            )
        )
        apply_source_offset(rows, timebase, source_offset)
        source_mapping_method = "verified_source_probe_rgb_alignment"
        source_mapping_exact = True
    cadence = cadence_summary(rows, timebase)
    packet_info = packet_summary(packets)
    event_info = event_selection(
        rows,
        event_start,
        event_end,
        requested_offset,
        source_offset,
        timebase,
        args.event_padding_frames,
    )
    event_rows = [rows[index] for index in event_info["window_frame_indices"]]
    full_exact_count = sum(1 for row in rows if row["full_frame_exact_repeat"])
    crop_exact_count = sum(1 for row in rows if row["gameplay_crop_exact_repeat"])
    near_count = sum(1 for row in rows if row["near_repeat"])
    scene_count = sum(1 for row in rows if row["scene_change"])
    output: dict[str, Any] = {
        "schema_version": ANALYZER_SCHEMA_VERSION,
        "analyzer": {
            "version": ANALYZER_VERSION,
            "path": str(script_path),
            "sha256": sha256_file(script_path),
            "python_version": platform.python_version(),
            "platform": platform.platform(),
        },
        "input": {
            "path": str(input_path),
            "resolved_path": str(input_path.resolve()),
            "sha256": input_hash,
            "size_bytes": input_path.stat().st_size,
        },
        "retrieval": {
            "source_id": args.source_id,
            "source_url": args.source_url,
            "format_id": args.format_id,
            "yt_dlp_version": args.yt_dlp_version,
            "command": args.retrieval_command,
            "requested_source_interval_seconds": [
                decimal_text(requested_offset),
                decimal_text(requested_offset + Fraction(16, 1)),
            ],
            "bounded_segment_only": True,
        },
        "tools": {
            "ffmpeg_version": tool_version("ffmpeg"),
            "ffprobe_version": tool_version("ffprobe"),
            "ffprobe_stream_command": [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_streams",
                "-show_format",
                "-of",
                "json",
                str(input_path),
            ],
            "ffprobe_frame_command": [
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
                str(input_path),
            ],
            "decoder_command": [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(input_path),
                "-map",
                "0:v:0",
                "-fps_mode",
                "passthrough",
                "-pix_fmt",
                "rgb24",
                "-f",
                "rawvideo",
                "pipe:1",
            ],
        },
        "observations": {
            "media_stream": {
                "codec_name": stream.get("codec_name"),
                "codec_tag_string": stream.get("codec_tag_string"),
                "width": width,
                "height": height,
                "pix_fmt": stream.get("pix_fmt"),
                "color_range": stream.get("color_range"),
                "color_space": stream.get("color_space"),
                "r_frame_rate": stream.get("r_frame_rate"),
                "avg_frame_rate": stream.get("avg_frame_rate"),
                "time_base": f"{timebase[0]}/{timebase[1]}",
                "start_pts": stream.get("start_pts"),
                "duration_ts": stream.get("duration_ts"),
                "duration": stream.get("duration"),
                "nb_frames_metadata": stream.get("nb_frames"),
            },
            "clip_hash": input_hash,
            "clip_sha256": input_hash,
            "packet_probe": packet_info,
            "decoded_cadence": cadence,
            "crop": {
                "x": crop[0],
                "y": crop[1],
                "width": crop[2],
                "height": crop[3],
                "coordinate_space": "decoded RGB frame pixels, origin top-left",
                "selection_basis": "left-bottom DOTA_Teabag gameplay pane in the 640x360 composite",
            },
            "fingerprint": {
                "grid_width": LOW_RESOLUTION[0],
                "grid_height": LOW_RESOLUTION[1],
                "sampling": "center sample of each crop cell",
                "luma_formula": "(54R + 183G + 19B + 128) // 256 (BT.709, 8-bit RGB)",
                "dhash": "row-major horizontal adjacent comparison, big-endian bit packing",
                "near_repeat_thresholds": {
                    "luma_difference_mean_max": NEAR_REPEAT_LUMA_MAE_MAX,
                    "dhash_hamming_distance_max": NEAR_REPEAT_DHASH_HAMMING_MAX,
                },
                "scene_change_thresholds": {
                    "luma_difference_mean_min": SCENE_CHANGE_LUMA_MAE_MIN,
                    "dhash_hamming_distance_min": SCENE_CHANGE_DHASH_HAMMING_MIN,
                },
                "counts": {
                    "full_frame_exact_repeats": full_exact_count,
                    "gameplay_crop_exact_repeats": crop_exact_count,
                    "near_gameplay_crop_repeats": near_count,
                    "scene_changes": scene_count,
                },
            },
            "source_time_mapping": {
                "requested_source_interval_offset_seconds": decimal_text(requested_offset),
                "decoded_clip_frame_zero_pts": rows[0]["clip_pts"],
                "decoded_clip_frame_zero_timebase": f"{timebase[0]}/{timebase[1]}",
                "source_timeline_offset_rational_seconds": fraction_text(source_offset),
                "source_timeline_offset_decimal_seconds": decimal_text(source_offset),
                "decoded_clip_frame_zero_mapped_source_rational_seconds": rows[0][
                    "source_time_rational"
                ],
                "decoded_clip_frame_zero_mapped_source_decimal_seconds": rows[0][
                    "source_time_seconds"
                ],
                "method": source_mapping_method,
                "exact_for_encoded_source_timeline": source_mapping_exact,
                "probe_input": source_probe_input,
                "probe_path": str(args.source_start_probe_path) if args.source_start_probe_path else None,
                "probe_hash": source_probe_hash,
                "probe_command": args.source_start_probe_command,
                "probe_alignment": source_probe_alignment,
                "observation": "The section stream-copy includes negative-PTS pre-roll packets. When a source-preserving probe is supplied, clip frame zero is mapped to source PTS only after a unique contiguous exact-RGB hash match between decoded clip and probe frames.",
            },
            "event_interval": event_info,
            "event_adjacent_frames": event_rows,
            "all_decoded_frames": rows,
        },
        "inference": {
            "encoded_cadence": "The retained decoded H.264 frames are the encoded-frame observation; this is not an N64 VI or game-update cadence.",
            "event_frame_selection": "Event membership uses exact rational source-time comparisons and half-open [start,end) semantics; mapped source-origin PTS take precedence over the requested offset when verified.",
            "visual_repeat_and_scene_labels": "Repeat and scene labels are deterministic pixel/fingerprint classifications only and do not identify an N64 state update, software writer, or physical initiator.",
        },
        "scope_limits": [
            "This audit covers only the bounded video-only format-134 segment requested from the public YouTube VOD; it does not download or retain the full VOD.",
            "YouTube H.264 encoding is lossy and composited at approximately 29.97 fps; duplicated, dropped, reordered, or otherwise absent source frames cannot be recovered from the clip.",
            "The section stream-copy seek includes negative-PTS pre-roll packets; decoded frame rows are paired to ffprobe best-effort PTS after the container's visible edit boundary.",
            "A source-preserving copyts probe can establish encoded-stream PTS alignment for this bounded retrieval, but no original N64 VI/game-update timestamp survives in the VOD.",
            "Full-frame and gameplay-crop hashes, low-resolution luma differences, and perceptual fingerprints describe encoded pixels only; they must not be used alone to infer N64 game updates.",
            "No claim is made here about the physical initiator or a normal-gameplay trigger for the upwarp.",
        ],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    try:
        args.output.write_text(
            json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        raise ToolFailure(f"cannot write output {args.output}") from exc
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ToolFailure as exc:
        print(f"analyze_vod_cadence.py: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
