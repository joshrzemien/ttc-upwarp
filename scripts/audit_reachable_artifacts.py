#!/usr/bin/env python3
"""Audit the retained reachable-route campaign artifacts.

The audit is intentionally evidence-preserving: a missing or invalid retained
artifact is reported in the JSON result, rather than treated as proof that the
historical campaign did not happen. All paths in the result are relative to
``--root`` so that a rerun does not depend on the checkout's absolute path.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import platform
import re
import struct
import sys
import zlib
from pathlib import Path
from types import ModuleType
from typing import Any

SCHEMA_VERSION = 1
SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
MD5_RE = re.compile(r"^[0-9a-fA-F]{32}$")
M64_MAGIC = b"M64\x1a"
M64_SAVE_MAGIC = b"M64+SAVE"
EXPECTED_ROM_MD5_FALLBACK = "85d61f5525af708c9f1e84dce6dc10e9"
RAW_CONTAINER_OFFSET = 444
MARIO_STATE_PHYS = 0x00339E00
RDRAM_VIRTUAL_BASE = 0x80000000
RDRAM_VIRTUAL_LIMIT = 0x80800000


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        while chunk := file.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def relative_path(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root).as_posix()
    except ValueError:
        return path.resolve().as_posix()


def require_mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value


def require_string(mapping: dict[str, Any], key: str, label: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label}.{key} must be a non-empty string")
    return value


def require_sha256(value: Any, label: str) -> str:
    if not isinstance(value, str) or SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"{label} must be a 64-digit SHA-256 string")
    return value.lower()


def require_md5(value: Any, label: str) -> str:
    if not isinstance(value, str) or MD5_RE.fullmatch(value) is None:
        raise ValueError(f"{label} must be a 32-digit MD5 string")
    return value.lower()


def load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ValueError(f"missing required {label}: {path}") from error
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"could not parse {label} {path}: {error}") from error
    return require_mapping(value, label)


def comparison_status(expected: str | None, observed: str | None) -> str:
    if observed is None:
        return "missing"
    if expected is None:
        return "not-recorded"
    return "hash-matched" if observed == expected else "hash-mismatch"


def file_record(path: Path, root: Path, expected_sha256: str | None = None) -> dict[str, Any]:
    record: dict[str, Any] = {
        "path": relative_path(path, root),
        "present": path.is_file(),
        "expected_sha256": expected_sha256,
        "observed_sha256": None,
        "size_bytes": None,
        "hash_status": "missing",
    }
    if not path.is_file():
        return record
    try:
        record["size_bytes"] = path.stat().st_size
        record["observed_sha256"] = sha256_file(path)
    except OSError as error:
        raise RuntimeError(f"could not read {path}: {error}") from error
    record["hash_status"] = comparison_status(
        expected_sha256, record["observed_sha256"]
    )
    return record


def parse_route_movie(data: bytes) -> dict[str, Any]:
    result: dict[str, Any] = {
        "format": "M64",
        "signature": data[:4].hex(),
        "format_status": "unparseable",
        "version": None,
        "declared_input_samples": None,
        "stored_input_samples": None,
        "header_bytes": None,
        "trailing_input_bytes": None,
        "rom_name": None,
        "rom_crc32": None,
        "rom_country": None,
    }
    if len(data) < 4 or data[:4] != M64_MAGIC:
        result["format_status"] = "invalid-signature"
        return result
    if len(data) < 8:
        result["format_status"] = "truncated-header"
        return result
    version = struct.unpack_from("<I", data, 4)[0]
    header_bytes = 0x400 if version >= 3 else 0x200
    result["version"] = version
    result["header_bytes"] = header_bytes
    if len(data) < 0x1C:
        result["format_status"] = "truncated-header"
        return result
    result["declared_input_samples"] = struct.unpack_from("<I", data, 0x18)[0]
    if len(data) >= 0xE8:
        result["rom_name"] = data[0xC4:0xE4].split(b"\0", 1)[0].decode(
            "utf-8", "replace"
        )
        result["rom_crc32"] = f"0x{struct.unpack_from('<I', data, 0xE4)[0]:08x}"
        result["rom_country"] = f"0x{struct.unpack_from('<H', data, 0xE8)[0]:04x}"
    if len(data) < header_bytes:
        result["format_status"] = "truncated-header"
        return result
    payload_bytes = len(data) - header_bytes
    result["stored_input_samples"] = payload_bytes // 4
    result["trailing_input_bytes"] = payload_bytes % 4
    declared_samples = int(result["declared_input_samples"])
    stored_samples = int(result["stored_input_samples"])
    if result["trailing_input_bytes"] != 0:
        result["format_status"] = "invalid-input-payload"
    elif stored_samples != declared_samples:
        result["format_status"] = "input-count-mismatch"
    else:
        result["format_status"] = "valid"
    return result


def _gzip_member(data: bytes) -> tuple[bytes, dict[str, Any], bytes]:
    """Decode one gzip member and return output, member facts, and remainder."""

    decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)
    output = bytearray()
    error: str | None = None
    try:
        output.extend(decompressor.decompress(data))
        output.extend(decompressor.flush())
    except zlib.error as exc:
        error = str(exc)
    consumed = len(data) - len(decompressor.unused_data) - len(decompressor.unconsumed_tail)
    member = {
        "compressed_bytes_available": len(data),
        "compressed_bytes_consumed": consumed,
        "decompressed_size_bytes": len(output),
        "eof": bool(decompressor.eof),
        "error": error,
    }
    return bytes(output), member, decompressor.unused_data


def decode_gzip_stream(data: bytes) -> tuple[bytes, dict[str, Any]]:
    """Decode all gzip members, checking that every member reaches its footer."""

    result: dict[str, Any] = {
        "format": "gzip",
        "format_status": "not-gzip",
        "stream_status": "missing",
        "status": "missing",
        "compressed_size_bytes": len(data),
        "decompressed_size_bytes": 0,
        "member_count": 0,
        "members": [],
        "trailing_size_bytes": 0,
        "error": None,
        "header": None,
    }
    if len(data) < 2 or data[:2] != b"\x1f\x8b":
        result["format_status"] = "invalid"
        result["stream_status"] = "invalid"
        result["status"] = "invalid"
        result["error"] = "gzip magic not present"
        return b"", result
    if len(data) >= 10:
        result["header"] = {
            "magic": data[:2].hex(),
            "compression_method": data[2],
            "flags": data[3],
            "mtime": struct.unpack_from("<I", data, 4)[0],
            "extra_flags": data[8],
            "operating_system": data[9],
        }
    else:
        result["header"] = {"magic": data[:2].hex(), "header_bytes_available": len(data)}
    result["format_status"] = "gzip"

    remaining = data
    output = bytearray()
    while remaining:
        if len(remaining) < 2 or remaining[:2] != b"\x1f\x8b":
            result["trailing_size_bytes"] = len(remaining)
            result["stream_status"] = "valid-with-trailing-data"
            result["status"] = "valid-with-trailing-data"
            break
        member_output, member, remainder = _gzip_member(remaining)
        output.extend(member_output)
        result["members"].append(member)
        result["member_count"] += 1
        if member["error"] is not None:
            result["error"] = member["error"]
            result["stream_status"] = "truncated" if not member["eof"] else "invalid"
            result["status"] = result["stream_status"]
            remaining = b""
            break
        if not member["eof"]:
            result["stream_status"] = "truncated"
            result["status"] = "truncated"
            remaining = b""
            break
        remaining = remainder
    else:
        result["stream_status"] = "valid"
        result["status"] = "valid"

    result["decompressed_size_bytes"] = len(output)
    return bytes(output), result


def parse_m64_save_header(data: bytes, expected_rom_md5: str) -> dict[str, Any]:
    result: dict[str, Any] = {
        "signature": data[:8].decode("ascii", "replace"),
        "signature_hex": data[:8].hex(),
        "header_status": "unparseable",
        "format_version_bytes_hex": data[8:12].hex(),
        "rom_md5_offset": 12,
        "rom_md5_ascii": None,
        "rom_md5": None,
        "rom_identity_status": "unavailable",
        "rom_identity_matches_expected": None,
        "prefix_bytes_hex": data[:12].hex(),
    }
    if len(data) < len(M64_SAVE_MAGIC) or data[:8] != M64_SAVE_MAGIC:
        result["header_status"] = "invalid-signature"
        return result
    if len(data) < 44:
        result["header_status"] = "truncated-header"
        raw = data[12:]
    else:
        result["header_status"] = "parseable"
        raw = data[12:44]
    try:
        rom_ascii = raw.decode("ascii")
    except UnicodeDecodeError:
        rom_ascii = raw.decode("ascii", "replace")
    result["rom_md5_ascii"] = rom_ascii
    if len(raw) == 32 and MD5_RE.fullmatch(rom_ascii):
        rom_md5 = rom_ascii.lower()
        result["rom_md5"] = rom_md5
        result["rom_identity_status"] = "parseable"
        result["rom_identity_matches_expected"] = rom_md5 == expected_rom_md5
    elif len(raw) < 32 and MD5_RE.fullmatch(rom_ascii):
        result["rom_identity_status"] = "truncated"
    else:
        result["rom_identity_status"] = "unparseable"
    return result


def parse_mario_state(
    data: bytes, stream_status: str, save_header_status: str
) -> dict[str, Any]:
    """Parse bounded RDRAM fields only from a complete M64+SAVE stream."""

    result: dict[str, Any] = {
        "status": "not-attempted",
        "raw_container_offset": RAW_CONTAINER_OFFSET,
        "mario_state_physical_address": f"0x{MARIO_STATE_PHYS:08X}",
        "mario_state_virtual_base": f"0x{RDRAM_VIRTUAL_BASE:08X}",
        "action": None,
        "mario_obj_pointer": None,
        "state_position": None,
        "gfx_position": None,
        "positions_equal": None,
        "error": None,
    }
    if stream_status != "valid":
        result["error"] = "complete gzip EOF required"
        return result
    if save_header_status != "parseable":
        result["error"] = "parseable M64+SAVE header required"
        return result
    state_base = RAW_CONTAINER_OFFSET + MARIO_STATE_PHYS
    state_required_end = state_base + 0x8C
    if len(data) < state_required_end:
        result["status"] = "bounds-unavailable"
        result["error"] = "MarioState fields exceed decompressed payload"
        return result
    action = struct.unpack_from("<I", data, state_base + 0x0C)[0]
    mario_obj = struct.unpack_from("<I", data, state_base + 0x88)[0]
    result["action"] = f"0x{action:08X}"
    result["mario_obj_pointer"] = f"0x{mario_obj:08X}"
    state_position = struct.unpack_from("<3f", data, state_base + 0x3C)
    result["state_position"] = list(state_position)
    if not (RDRAM_VIRTUAL_BASE <= mario_obj < RDRAM_VIRTUAL_LIMIT):
        result["status"] = "invalid-pointer"
        result["error"] = "marioObj pointer is outside the supported RDRAM virtual range"
        return result
    object_offset = RAW_CONTAINER_OFFSET + (mario_obj - RDRAM_VIRTUAL_BASE)
    gfx_required_end = object_offset + 0x2C
    if len(data) < gfx_required_end:
        result["status"] = "bounds-unavailable"
        result["error"] = "Mario object gfx position exceeds decompressed payload"
        return result
    gfx_position = struct.unpack_from("<3f", data, object_offset + 0x20)
    result["gfx_position"] = list(gfx_position)
    result["positions_equal"] = state_position == gfx_position
    result["status"] = "parsed"
    return result


def import_builder(path: Path) -> ModuleType:
    module_name = "_reachable_artifact_integrity_builder"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load builder module {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def reconstruct_sequence_hash(builder_path: Path) -> dict[str, Any]:
    """Recreate the builder's default sequence bytes without retaining a file."""

    result: dict[str, Any] = {
        "retained": False,
        "status": "unavailable",
        "raw_domain": {"x": [-61, 61], "y": [-63, 63]},
        "button_class_count": 16,
        "analog_equivalence_class_count": None,
        "sequence_count": None,
        "reconstructed_sha256": None,
        "error": None,
    }
    try:
        module = import_builder(builder_path)
        adjusted_stick = getattr(module, "adjusted_stick")
        classes: dict[tuple[int, int, int], list[tuple[int, int]]] = {}
        for raw_x in range(-61, 62):
            for raw_y in range(-63, 64):
                classes.setdefault(adjusted_stick(raw_x, raw_y), []).append((raw_x, raw_y))
        rows: list[str] = []
        for index, (key, members) in enumerate(sorted(classes.items())):
            del key
            raw_x, raw_y = members[0]
            for buttons in range(16):
                rows.append(f"c{index:05}_b{buttons:02}|1:{buttons}:{raw_x}:{raw_y}")
        encoded = ("\n".join(rows) + "\n").encode("utf-8")
        result.update(
            {
                "status": "reconstructed",
                "analog_equivalence_class_count": len(classes),
                "sequence_count": len(rows),
                "reconstructed_sha256": sha256_bytes(encoded),
            }
        )
    except (AttributeError, ImportError, OSError, RuntimeError, TypeError, ValueError) as error:
        result["error"] = str(error)
    return result


def script_record(path: Path, root: Path) -> dict[str, Any]:
    record = file_record(path, root)
    record.update(
        {
            "artifact_type": "script",
            "hash_status": "not-recorded" if record["present"] else "missing",
            "campaign_binding": {"status": "not-recorded"},
        }
    )
    return record


def snapshot_record(
    path: Path,
    root: Path,
    expected_checkpoint_sha256: str | None,
    campaign_sha256: str | None,
    expected_rom_md5: str,
) -> dict[str, Any]:
    base = file_record(path, root, expected_checkpoint_sha256)
    result: dict[str, Any] = {
        "name": path.name,
        "path": base["path"],
        "artifact_type": "mupen64plus_m64p_savestate",
        "present": base["present"],
        "compressed_size_bytes": base["size_bytes"],
        "sha256": base["observed_sha256"],
        "checkpoint_recorded_sha256": expected_checkpoint_sha256,
        "hash_status": base["hash_status"],
        "status_labels": [],
        "gzip": None,
        "m64_save_header": None,
        "mario_state": None,
        "campaign_binding": {
            "status": "not-recorded" if campaign_sha256 is None else "not-campaign-bound",
            "recorded_campaign_sha256": campaign_sha256,
            "campaign_time_bytes_present": None if campaign_sha256 is None else False,
            "matching_retained_path": None,
        },
    }
    if not base["present"]:
        result["status_labels"] = ["missing"]
        if campaign_sha256 is not None:
            result["campaign_binding"]["reason"] = "campaign-named snapshot path is missing"
        return result

    try:
        compressed = path.read_bytes()
    except OSError as error:
        raise RuntimeError(f"could not read snapshot {path}: {error}") from error
    decompressed, gzip_result = decode_gzip_stream(compressed)
    result["gzip"] = gzip_result
    result["decompressed_size_bytes"] = gzip_result["decompressed_size_bytes"]
    result["m64_save_header"] = parse_m64_save_header(decompressed, expected_rom_md5)
    result["mario_state"] = parse_mario_state(
        decompressed,
        gzip_result["stream_status"],
        result["m64_save_header"]["header_status"],
    )
    labels = ["present", gzip_result["stream_status"]]
    if base["hash_status"] == "hash-matched":
        labels.append("hash-matched")
    elif base["hash_status"] == "hash-mismatch":
        labels.append("hash-mismatch")
    if campaign_sha256 is not None:
        if base["observed_sha256"] == campaign_sha256:
            result["campaign_binding"].update(
                {
                    "status": "campaign-bound",
                    "campaign_time_bytes_present": True,
                    "matching_retained_path": base["path"],
                }
            )
            labels.append("campaign-bound")
        else:
            result["campaign_binding"]["reason"] = (
                "retained bytes do not match the campaign-time SHA-256"
            )
            labels.append("not-campaign-bound")
    result["status_labels"] = labels
    return result


def hash_comparison(
    source_artifact: str,
    field: str,
    expected_sha256: str,
    observed_sha256: str | None,
    path: str | None,
    evidence_binding: str,
) -> dict[str, Any]:
    status = comparison_status(expected_sha256, observed_sha256)
    return {
        "source_artifact": source_artifact,
        "field": field,
        "path": path,
        "expected_sha256": expected_sha256,
        "observed_sha256": observed_sha256,
        "status": status,
        "evidence_binding": evidence_binding,
    }


def source_references(source_manifest: dict[str, Any]) -> list[dict[str, Any]]:
    references: list[dict[str, Any]] = []
    for section in ("technical_sources", "incident_media", "contemporaneous_analysis", "later_context"):
        entries = source_manifest.get(section, [])
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            references.append(
                {
                    "section": section,
                    "name": entry.get("name"),
                    "url": entry.get("url"),
                    "revision": entry.get("revision"),
                }
            )
    return references


def build_audit(root: Path) -> dict[str, Any]:
    checkpoint_path = root / "results/reachable_state_checkpoint.json"
    summary_path = root / "results/controller_equivalence_summary.json"
    sources_path = root / "results/sources.json"
    reassessment_path = root / "results/reachable_campaign_reassessment.json"
    checkpoint = load_json(checkpoint_path, "reachable state checkpoint")
    summary = load_json(summary_path, "controller-equivalence summary")
    sources = load_json(sources_path, "source manifest")

    checkpoint_route = require_mapping(checkpoint.get("route"), "checkpoint.route")
    checkpoint_construction = require_mapping(
        checkpoint_route.get("construction"), "checkpoint.route.construction"
    )
    checkpoint_local = require_mapping(checkpoint.get("local_snapshots"), "checkpoint.local_snapshots")
    checkpoint_files = checkpoint_local.get("files")
    if not isinstance(checkpoint_files, list):
        raise ValueError("checkpoint.local_snapshots.files must be an array")

    summary_inputs = require_mapping(summary.get("inputs"), "summary.inputs")
    summary_integrity = require_mapping(summary.get("integrity"), "summary.integrity")
    route_path = root / require_string(checkpoint_route, "path", "checkpoint.route")
    expected_route_hash = require_sha256(checkpoint_route.get("sha256"), "checkpoint.route.sha256")
    summary_route_hash = require_sha256(
        summary_inputs.get("route_movie_sha256"), "summary.inputs.route_movie_sha256"
    )
    campaign_snapshot_name = require_string(
        summary_inputs, "snapshot_name", "summary.inputs"
    )
    campaign_snapshot_hash = require_sha256(
        summary_inputs.get("snapshot_sha256_at_campaign_time"),
        "summary.inputs.snapshot_sha256_at_campaign_time",
    )
    scanner_expected_hash = require_sha256(
        summary_inputs.get("scanner_sha256"), "summary.inputs.scanner_sha256"
    )
    sequence_expected_hash = require_sha256(
        summary_inputs.get("sequence_sha256"), "summary.inputs.sequence_sha256"
    )
    full_campaign_expected_hash = require_sha256(
        summary_integrity.get("full_campaign_sha256"),
        "summary.integrity.full_campaign_sha256",
    )
    expected_rom_md5 = require_md5(
        summary_inputs.get("rom_md5", EXPECTED_ROM_MD5_FALLBACK),
        "summary.inputs.rom_md5",
    )

    snapshot_expected: dict[str, str] = {}
    for index, entry_value in enumerate(checkpoint_files):
        entry = require_mapping(entry_value, f"checkpoint.local_snapshots.files[{index}]")
        name = require_string(entry, "name", f"checkpoint.local_snapshots.files[{index}]")
        snapshot_expected[name] = require_sha256(
            entry.get("sha256"), f"checkpoint.local_snapshots.files[{index}].sha256"
        )

    route_base = file_record(route_path, root, expected_route_hash)
    route_data = route_path.read_bytes() if route_base["present"] else b""
    route_record = {
        **route_base,
        "artifact_type": "m64_route_movie",
        "format": parse_route_movie(route_data) if route_data else None,
        "campaign_binding": {
            "status": (
                "campaign-bound"
                if route_base["observed_sha256"] == summary_route_hash
                else "not-campaign-bound"
            ),
            "recorded_campaign_sha256": summary_route_hash,
        },
    }
    route_labels = ["present"] if route_base["present"] else ["missing"]
    if route_base["hash_status"] == "hash-matched":
        route_labels.append("hash-matched")
    elif route_base["hash_status"] == "hash-mismatch":
        route_labels.append("hash-mismatch")
    if route_record["campaign_binding"]["status"] == "campaign-bound":
        route_labels.append("campaign-bound")
    route_record["status_labels"] = route_labels

    script_paths = {
        "audit": root / "scripts/audit_reachable_artifacts.py",
        "scanner": root / "scripts/controller_sequence_scan.lua",
        "builder": root / "scripts/build_controller_equivalence_sequences.py",
        "runner": root / "scripts/run_controller_sequence_campaign.py",
        "route_builder": root / "scripts/build_m64_splice_grid.py",
        "summarizer": root / "scripts/summarize_controller_sequence_campaign.py",
        "validator": root / "scripts/validate_controller_sequence_campaign.py",
    }
    script_records = {
        role: script_record(path, root) for role, path in script_paths.items()
    }

    snapshot_dir = root / "results/reachable_snapshots"
    sequence_candidates = [
        root / "results/controller_equivalence_sequences.txt",
        root / "results/controller_equivalence_sequences.log",
    ]
    sequence_path = next(
        (path for path in sequence_candidates if path.is_file()),
        sequence_candidates[0],
    )
    aggregate_candidates = [
        root / "results/controller_equivalence_campaign.json",
        root / "results/controller_equivalence_campaign.jsonl",
    ]
    aggregate_path = next(
        (path for path in aggregate_candidates if path.is_file()),
        aggregate_candidates[0],
    )
    current_names = (
        {path.name for path in snapshot_dir.glob("*.m64p") if path.is_file()}
        if snapshot_dir.is_dir()
        else set()
    )
    snapshot_names = sorted(set(snapshot_expected) | current_names)
    snapshots: list[dict[str, Any]] = []
    for name in snapshot_names:
        path = snapshot_dir / name
        snapshots.append(
            snapshot_record(
                path,
                root,
                snapshot_expected.get(name),
                campaign_snapshot_hash if name == campaign_snapshot_name else None,
                expected_rom_md5,
            )
        )

    scanner_record = script_records["scanner"]
    sequence_reconstruction = reconstruct_sequence_hash(script_paths["builder"])
    sequence_reconstruction["recorded_sha256"] = sequence_expected_hash
    if sequence_reconstruction["reconstructed_sha256"] is not None:
        sequence_reconstruction["reconstruction_hash_status"] = comparison_status(
            sequence_expected_hash, sequence_reconstruction["reconstructed_sha256"]
        )
    else:
        sequence_reconstruction["reconstruction_hash_status"] = "missing"
    sequence_payload = file_record(
        sequence_path,
        root,
        sequence_expected_hash,
    )
    full_campaign_aggregate = file_record(
        aggregate_path,
        root,
        full_campaign_expected_hash,
    )

    comparisons: list[dict[str, Any]] = []
    comparisons.append(
        hash_comparison(
            "results/reachable_state_checkpoint.json",
            "route.sha256",
            expected_route_hash,
            route_base["observed_sha256"],
            route_base["path"],
            "checkpoint-route",
        )
    )
    comparisons.append(
        hash_comparison(
            "results/controller_equivalence_summary.json",
            "inputs.route_movie_sha256",
            summary_route_hash,
            route_base["observed_sha256"],
            route_base["path"],
            "campaign-route",
        )
    )
    comparisons.append(
        hash_comparison(
            "results/controller_equivalence_summary.json",
            "inputs.scanner_sha256",
            scanner_expected_hash,
            scanner_record["observed_sha256"],
            scanner_record["path"],
            "campaign-scanner",
        )
    )
    for name in sorted(snapshot_expected):
        record = next(snapshot for snapshot in snapshots if snapshot["name"] == name)
        comparisons.append(
            hash_comparison(
                "results/reachable_state_checkpoint.json",
                f"local_snapshots.files[{name}].sha256",
                snapshot_expected[name],
                record["sha256"],
                record["path"],
                "checkpoint-retained-snapshot",
            )
        )
    campaign_snapshot_record = next(
        (snapshot for snapshot in snapshots if snapshot["name"] == campaign_snapshot_name),
        None,
    )
    comparisons.append(
        hash_comparison(
            "results/controller_equivalence_summary.json",
            "inputs.snapshot_sha256_at_campaign_time",
            campaign_snapshot_hash,
            campaign_snapshot_record["sha256"] if campaign_snapshot_record else None,
            campaign_snapshot_record["path"] if campaign_snapshot_record else None,
            "campaign-time-snapshot",
        )
    )
    comparisons.extend(
        [
            hash_comparison(
                "results/reachable_state_checkpoint.json",
                "route.construction.prefix_source_sha256",
                require_sha256(
                    checkpoint_construction.get("prefix_source_sha256"),
                    "checkpoint.route.construction.prefix_source_sha256",
                ),
                None,
                None,
                "checkpoint-unresolved-source",
            ),
            hash_comparison(
                "results/reachable_state_checkpoint.json",
                "route.construction.route_source_sha256",
                require_sha256(
                    checkpoint_construction.get("route_source_sha256"),
                    "checkpoint.route.construction.route_source_sha256",
                ),
                None,
                None,
                "checkpoint-unresolved-source",
            ),
            hash_comparison(
                "results/reachable_state_checkpoint.json",
                "route.construction.base_savestate_sha256",
                require_sha256(
                    checkpoint_construction.get("base_savestate_sha256"),
                    "checkpoint.route.construction.base_savestate_sha256",
                ),
                None,
                None,
                "checkpoint-unresolved-base-state",
            ),
            hash_comparison(
                "results/controller_equivalence_summary.json",
                "integrity.full_campaign_sha256",
                full_campaign_expected_hash,
                full_campaign_aggregate["observed_sha256"],
                full_campaign_aggregate["path"],
                "raw-campaign-aggregate",
            ),
        ]
    )
    comparisons.sort(key=lambda item: (item["source_artifact"], item["field"]))

    evidence_files = []
    for path in (checkpoint_path, summary_path, reassessment_path, sources_path):
        evidence_files.append(file_record(path, root))

    campaign_snapshot_bound = bool(
        campaign_snapshot_record is not None
        and campaign_snapshot_record.get("sha256") == campaign_snapshot_hash
        and (campaign_snapshot_record.get("gzip") or {}).get("stream_status") == "valid"
    )
    campaign_snapshot_absent = not campaign_snapshot_bound
    truncated_snapshot_names = sorted(
        snapshot["name"]
        for snapshot in snapshots
        if (snapshot.get("gzip") or {}).get("stream_status") == "truncated"
    )
    valid_snapshot_names = sorted(
        snapshot["name"]
        for snapshot in snapshots
        if (snapshot.get("gzip") or {}).get("stream_status") == "valid"
    )
    sequence_reconstruction_matches = (
        sequence_reconstruction["reconstruction_hash_status"] == "hash-matched"
    )
    scanner_matches = (
        scanner_record["observed_sha256"] == scanner_expected_hash
    )
    sequence_payload_matches = (
        sequence_payload["observed_sha256"] == sequence_expected_hash
    )
    aggregate_matches = (
        full_campaign_aggregate["observed_sha256"]
        == full_campaign_expected_hash
    )
    reproducibility_blockers: list[str] = []
    if not campaign_snapshot_bound:
        reproducibility_blockers.append(
            f"campaign-time {campaign_snapshot_name} bytes are not retained as a complete gzip under the recorded SHA-256"
        )
    if not scanner_matches:
        reproducibility_blockers.append(
            "the current controller scanner SHA-256 differs from the campaign-recorded scanner hash"
        )
    if not sequence_payload_matches:
        reproducibility_blockers.append(
            "the sequence payload and manifest are not retained under the recorded SHA-256"
        )
    if not aggregate_matches:
        reproducibility_blockers.append(
            "the full per-candidate campaign aggregate is absent or does not match its recorded SHA-256"
        )
    reproducible_from_retained_inputs = not reproducibility_blockers

    observations = [
        {
            "id": "route-hash",
            "finding": "The retained reachable route SHA-256 matches both checkpoint and campaign route hashes.",
            "status": route_base["hash_status"],
            "sha256": route_base["observed_sha256"],
        },
        {
            "id": "snapshot-gzip-streams",
            "finding": "Retained Mupen savestate gzip members were read through their stream ends.",
            "truncated": truncated_snapshot_names,
            "valid": valid_snapshot_names,
        },
        {
            "id": "snapshot-checkpoint-hashes",
            "finding": "Current snapshot bytes are compared with checkpoint-recorded hashes.",
            "hash_matched": sorted(
                snapshot["name"] for snapshot in snapshots if snapshot["hash_status"] == "hash-matched"
            ),
            "hash_mismatched": sorted(
                snapshot["name"] for snapshot in snapshots if snapshot["hash_status"] == "hash-mismatch"
            ),
            "missing": sorted(
                snapshot["name"] for snapshot in snapshots if not snapshot["present"]
            ),
        },
        {
            "id": "campaign-time-snapshot",
            "finding": (
                "The retained campaign snapshot path is checked for a complete "
                "gzip stream and the campaign-time SHA-256."
            ),
            "campaign_snapshot_name": campaign_snapshot_name,
            "campaign_recorded_sha256": campaign_snapshot_hash,
            "retained_sha256": (
                campaign_snapshot_record["sha256"]
                if campaign_snapshot_record
                else None
            ),
            "campaign_time_bytes_present": not campaign_snapshot_absent,
        },
        {
            "id": "scanner-hash",
            "finding": "The current controller scanner is compared with the campaign-recorded hash.",
            "recorded_sha256": scanner_expected_hash,
            "current_sha256": scanner_record["observed_sha256"],
            "status": comparison_status(scanner_expected_hash, scanner_record["observed_sha256"]),
        },
        {
            "id": "sequence-payload",
            "finding": "The campaign sequence payload is checked at the conventional retained path; the current builder also re-derives the recorded hash in memory.",
            "recorded_sha256": sequence_expected_hash,
            "retained": bool(sequence_payload["present"]),
            "retained_path": sequence_payload["path"],
            "retained_sha256": sequence_payload["observed_sha256"],
            "retained_hash_status": sequence_payload["hash_status"],
            "reconstruction_hash_status": sequence_reconstruction["reconstruction_hash_status"],
            "reconstructed_sha256": sequence_reconstruction["reconstructed_sha256"],
        },
        {
            "id": "raw-campaign-aggregate",
            "finding": "The full per-candidate aggregate is checked at the conventional retained path.",
            "recorded_sha256": full_campaign_expected_hash,
            "retained": bool(full_campaign_aggregate["present"]),
            "retained_path": full_campaign_aggregate["path"],
            "retained_sha256": full_campaign_aggregate["observed_sha256"],
            "retained_hash_status": full_campaign_aggregate["hash_status"],
            "summary_note": summary_integrity.get("full_campaign_note"),
        },
    ]

    inferences = [
        {
            "id": "historical-one-update-reproducibility",
            "conclusion": (
                "The historical recorded one-update result "
                + (
                    "is independently reproducible from retained inputs."
                    if reproducible_from_retained_inputs
                    else "is not independently reproducible from retained inputs."
                )
            ),
            "reproducible_from_retained_inputs": reproducible_from_retained_inputs,
            "blocking_observations": [item for item in reproducibility_blockers],
            "sequence_hash_rederivation_matches": sequence_reconstruction_matches,
        },
        {
            "id": "historical-run-neutrality",
            "conclusion": "These integrity findings do not establish that the historical campaign never happened.",
            "historical_run_status": "not-determined",
        },
    ]

    recapture_gate_blocked = bool(truncated_snapshot_names)
    campaign_snapshot_gate_blocked = not campaign_snapshot_bound
    scanner_gate_blocked = not scanner_matches
    payload_gate_blocked = not (
        sequence_payload_matches and aggregate_matches
    )
    gates = [
        {
            "id": "recapture-valid-savestates",
            "status": "blocked" if recapture_gate_blocked else "passed",
            "condition": (
                f"gzip EOF validation fails for {', '.join(truncated_snapshot_names)}."
                if recapture_gate_blocked
                else "Every named retained Mupen savestate reaches gzip EOF."
            ),
            "action": "Recapture or resave each named Mupen savestate and retain the complete gzip bytes; rerun this audit before using it.",
        },
        {
            "id": "recover-campaign-snapshot",
            "status": "blocked" if campaign_snapshot_gate_blocked else "passed",
            "condition": (
                f"The campaign-time {campaign_snapshot_name} SHA-256 has no matching complete retained gzip."
                if campaign_snapshot_gate_blocked
                else (
                    f"The retained {campaign_snapshot_name} gzip is complete "
                    "and matches the campaign-time SHA-256."
                )
            ),
            "action": (
                f"Recover or recapture the exact campaign-time {campaign_snapshot_name} "
                "savestate, verify its SHA-256 and gzip EOF, then rerun the campaign."
            ),
        },
        {
            "id": "pin-scanner-revision",
            "status": "blocked" if scanner_gate_blocked else "passed",
            "condition": (
                "Current controller_sequence_scan.lua does not match the recorded campaign hash."
                if scanner_gate_blocked
                else "Current controller_sequence_scan.lua matches the recorded campaign hash."
            ),
            "action": "Recover the scanner revision matching the recorded hash or explicitly rerun with the current scanner and record its hash.",
        },
        {
            "id": "retain-sequence-and-aggregate",
            "status": "blocked" if payload_gate_blocked else "passed",
            "condition": (
                "The sequence payload/manifest or full per-candidate aggregate is absent or hash-mismatched."
                if payload_gate_blocked
                else "The retained sequence payload and full aggregate match their recorded hashes."
            ),
            "action": "Retain the exact generated sequence plus manifest and the deterministic all-results aggregate with SHA-256.",
        },
        {
            "id": "rerun-one-update-campaign",
            "status": (
                "passed"
                if reproducible_from_retained_inputs
                else "blocked"
            ),
            "condition": (
                "Every campaign input and retained output is independently bound."
                if reproducible_from_retained_inputs
                else "At least one campaign input or retained output is not independently bound."
            ),
            "action": "After the recapture and hash gates pass, rerun the one-update campaign with complete restores and preserve logs, sequence, snapshot, scanner, and aggregate hashes together.",
        },
    ]

    return {
        "schema_version": SCHEMA_VERSION,
        "audit": {
            "name": "historical reachable artifact integrity",
            "path_basis": "all paths are relative to --root",
            "filesystem_timestamps_excluded": True,
            "scope": "The retained bytes and hashes from the compact historical campaign record; the fresh reassessment is cataloged separately.",
        },
        "tool_versions": {
            "python_implementation": platform.python_implementation(),
            "python_version": platform.python_version(),
            "zlib_version": zlib.ZLIB_VERSION,
            "sha256": "hashlib.sha256",
            "gzip_decoder": "zlib.decompressobj(16 + zlib.MAX_WBITS), read until eof",
        },
        "inputs": {
            "evidence_files": evidence_files,
            "expected_rom_md5": expected_rom_md5,
        },
        "artifacts": {
            "route": route_record,
            "scripts": script_records,
            "snapshots": snapshots,
            "sequence_reconstruction": sequence_reconstruction,
            "sequence_payload": sequence_payload,
            "full_campaign_aggregate": full_campaign_aggregate,
        },
        "recorded_hash_comparisons": comparisons,
        "observations": observations,
        "inferences": inferences,
        "reproducibility": {
            "historical_recorded_one_update_result_independently_reproducible_from_retained_inputs": reproducible_from_retained_inputs,
            "blockers": reproducibility_blockers,
            "historical_run_status": "not-determined",
        },
        "integrity_gates": gates,
        "source_references": source_references(sources),
        "scope_limits": [
            "This audit checks the compact historical campaign's retained bytes, hashes, gzip framing, and parseable M64+SAVE identity; it does not run an emulator or ROM.",
            "A valid retained savestate does not prove that it was the savestate used by the historical campaign unless its campaign-time hash matches.",
            "Missing or mismatched artifacts cannot distinguish an absent historical run from lost or changed evidence.",
            "This audit makes no claim that the physical initiator or a normal-gameplay trigger has been solved.",
        ],
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path("."),
        help="repository root (default: current directory)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="JSON output path (default: ROOT/results/reachable_artifact_integrity.json)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    root = args.root.expanduser().resolve()
    if not root.is_dir():
        print(f"error: root is not a directory: {root}", file=sys.stderr)
        return 2
    output = args.output.expanduser() if args.output is not None else Path(
        "results/reachable_artifact_integrity.json"
    )
    if not output.is_absolute():
        output = root / output
    try:
        audit = build_audit(root)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(audit, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except (OSError, RuntimeError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    print(f"wrote {relative_path(output, root)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
