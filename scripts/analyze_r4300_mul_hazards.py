#!/usr/bin/env python3
"""Audit R4300 multiply-hazard candidates in retained Mario-Y context rings.

The trace contains 64-instruction rings rather than a complete instruction
stream. Dynamic sequence numbers are therefore the only adjacency key used by
this audit; PC arithmetic is intentionally not used.
"""
from __future__ import annotations

import argparse
import collections
import gzip
import hashlib
import json
import platform
import sys
from pathlib import Path
from typing import Any, TextIO


SCHEMA_VERSION = 1
ANALYZER_VERSION = "1"
ERRATUM_URL = "http://ultra64.ca/files/documentation/online-manuals/man/developerNews/news-02.html"
ERRATUM_TITLE = "Nintendo 64 Developers News 1.2"
ERRATUM_REVISION = "1.2"

CONTEXT_FIELDS = (
    "event",
    "age",
    "sequence",
    "pc",
    "opcode",
    "rs",
    "rt",
    "fs",
    "ft",
    "fd",
    "gpr_rs",
    "gpr_rt",
    "fpr_fs",
    "fpr_ft",
    "fpr_fd",
    "fpr_ft_single",
)
DECIMAL_FIELDS = {"event", "age", "sequence", "rs", "rt", "fs", "ft", "fd"}
HEX_FIELDS = {
    "pc",
    "opcode",
    "gpr_rs",
    "gpr_rt",
    "fpr_fs",
    "fpr_ft",
    "fpr_fd",
    "fpr_ft_single",
}
PAYLOAD_FIELDS = (
    "sequence",
    "pc",
    "opcode",
    "rs",
    "rt",
    "fs",
    "ft",
    "fd",
    "gpr_rs",
    "gpr_rt",
    "fpr_fs",
    "fpr_ft",
    "fpr_fd",
    "fpr_ft_single",
)
MULTIPLY_NAMES = ("MUL.S", "MUL.D", "MULT", "MULTU", "DMULT", "DMULTU")
FP_MULTIPLY_NAMES = frozenset(("MUL.S", "MUL.D"))
ERRATUM_SOURCE_CLASSES = frozenset(("finite_zero", "infinity", "signaling_nan"))


class TraceInputError(ValueError):
    """Raised when a retained trace is malformed for this audit."""


def open_text(path: Path) -> TextIO:
    if path.suffix == ".gz":
        return gzip.open(path, "rt", encoding="utf-8", errors="strict")
    return path.open("rt", encoding="utf-8", errors="strict")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        while chunk := file.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def parse_number(key: str, value: str, line_number: int) -> int:
    try:
        number = int(value, 10 if key in DECIMAL_FIELDS else 16)
    except ValueError as error:
        raise TraceInputError(
            f"line {line_number}: invalid {key} value {value!r}"
        ) from error
    if number < 0:
        raise TraceInputError(f"line {line_number}: negative {key} value")
    if key in {"pc", "opcode", "fpr_ft_single"} and number > 0xFFFFFFFF:
        raise TraceInputError(f"line {line_number}: {key} exceeds 32 bits")
    if key in {"gpr_rs", "gpr_rt", "fpr_fs", "fpr_ft", "fpr_fd"} and number > 0xFFFFFFFFFFFFFFFF:
        raise TraceInputError(f"line {line_number}: {key} exceeds 64 bits")
    if key in {"rs", "rt", "fs", "ft", "fd"} and number > 31:
        raise TraceInputError(f"line {line_number}: {key} exceeds register range")
    if key == "age" and number > 0xFFFF:
        raise TraceInputError(f"line {line_number}: age is implausibly large")
    return number


def parse_context_line(line: str, line_number: int) -> dict[str, Any]:
    tokens = line.rstrip("\n").split(",")
    if not tokens or tokens[0] != "MARIO_Y_CONTEXT":
        raise TraceInputError(f"line {line_number}: invalid context prefix")
    fields: dict[str, Any] = {}
    for token in tokens[1:]:
        if "=" not in token:
            raise TraceInputError(f"line {line_number}: context token lacks '='")
        key, value = token.split("=", 1)
        if key not in CONTEXT_FIELDS:
            raise TraceInputError(f"line {line_number}: unexpected context field {key!r}")
        if key in fields:
            raise TraceInputError(f"line {line_number}: duplicate context field {key!r}")
        if value == "":
            raise TraceInputError(f"line {line_number}: empty {key} value")
        fields[key] = parse_number(key, value, line_number)
    missing = sorted(set(CONTEXT_FIELDS) - set(fields))
    if missing:
        raise TraceInputError(
            f"line {line_number}: missing context fields {','.join(missing)}"
        )

    opcode = fields["opcode"]
    derived = {
        "rs": (opcode >> 21) & 31,
        "rt": (opcode >> 16) & 31,
        "fs": (opcode >> 11) & 31,
        "ft": (opcode >> 16) & 31,
        "fd": (opcode >> 6) & 31,
    }
    for key, expected in derived.items():
        if fields[key] != expected:
            raise TraceInputError(
                f"line {line_number}: {key}={fields[key]} disagrees with opcode"
            )
    if fields["event"] <= 0:
        raise TraceInputError(f"line {line_number}: event must be positive")
    return fields


def hex_word(value: int, width: int = 8) -> str:
    return f"0x{value:0{width}X}"


def classify_float32(bits: int) -> str:
    exponent = (bits >> 23) & 0xFF
    fraction = bits & 0x7FFFFF
    if exponent == 0 and fraction == 0:
        return "finite_zero"
    if exponent == 0xFF and fraction == 0:
        return "infinity"
    if exponent == 0xFF and fraction != 0:
        return "quiet_nan" if fraction & 0x400000 else "signaling_nan"
    return "other"


def classify_float64(bits: int) -> str:
    exponent = (bits >> 52) & 0x7FF
    fraction = bits & ((1 << 52) - 1)
    if exponent == 0 and fraction == 0:
        return "finite_zero"
    if exponent == 0x7FF and fraction == 0:
        return "infinity"
    if exponent == 0x7FF and fraction != 0:
        return "quiet_nan" if fraction & (1 << 51) else "signaling_nan"
    return "other"


def decode_multiply(row: dict[str, Any]) -> str | None:
    opcode = row["opcode"]
    major = opcode >> 26
    funct = opcode & 0x3F
    if major == 0x11 and funct == 0x02:
        fmt = (opcode >> 21) & 0x1F
        if fmt == 16:
            return "MUL.S"
        if fmt == 17:
            return "MUL.D"
    if major == 0:
        return {
            24: "MULT",
            25: "MULTU",
            28: "DMULT",
            29: "DMULTU",
        }.get(funct)
    return None


def canonical_payload(row: dict[str, Any]) -> tuple[int, ...]:
    return tuple(int(row[key]) for key in PAYLOAD_FIELDS)


def count_classes(values: list[str]) -> dict[str, int]:
    counts = collections.Counter(values)
    return {
        name: counts.get(name, 0)
        for name in (
            "finite_zero",
            "infinity",
            "signaling_nan",
            "quiet_nan",
            "other",
        )
    }


def source_class_views(row: dict[str, Any], name: str) -> dict[str, Any]:
    if name == "MUL.S":
        fs = int(row["fpr_fs"])
        ft = int(row["fpr_ft"])
        low = {
            "fs": classify_float32(fs & 0xFFFFFFFF),
            "ft": classify_float32(ft & 0xFFFFFFFF),
        }
        high = {
            "fs": classify_float32((fs >> 32) & 0xFFFFFFFF),
            "ft": classify_float32((ft >> 32) & 0xFFFFFFFF),
        }
        any_word = {
            source: sorted({low[source], high[source]})
            for source in ("fs", "ft")
        }
        return {
            "low32": low,
            "high32": high,
            "any_recorded_word": any_word,
        }
    if name == "MUL.D":
        return {
            "double64": {
                "fs": classify_float64(int(row["fpr_fs"])),
                "ft": classify_float64(int(row["fpr_ft"])),
            }
        }
    return {}


def source_has_erratum_class_primary(views: dict[str, Any], name: str) -> bool:
    """Eligibility using the primary interpretation for each instruction."""
    if name == "MUL.S":
        return any(
            views["low32"][source] in ERRATUM_SOURCE_CLASSES
            for source in ("fs", "ft")
        )
    if name == "MUL.D":
        return any(
            views["double64"][source] in ERRATUM_SOURCE_CLASSES
            for source in ("fs", "ft")
        )
    return False


def source_has_erratum_class_any_recorded_word(
    views: dict[str, Any], name: str
) -> bool:
    """Conservative MUL.S eligibility while the active 32-bit lane is unknown."""
    if name == "MUL.S":
        return any(
            views["any_recorded_word"][source]
            and any(
                value in ERRATUM_SOURCE_CLASSES
                for value in views["any_recorded_word"][source]
            )
            for source in ("fs", "ft")
        )
    if name == "MUL.D":
        return any(
            views["double64"][source] in ERRATUM_SOURCE_CLASSES
            for source in ("fs", "ft")
        )
    return False


def source_has_zero_any_recorded_word(views: dict[str, Any]) -> bool:
    return any(
        "finite_zero" in views["any_recorded_word"][source]
        for source in ("fs", "ft")
    )


def source_has_zero_primary_word(views: dict[str, Any]) -> bool:
    return any(
        views["low32"][source] == "finite_zero"
        for source in ("fs", "ft")
    )


def merge_intervals(sequences: list[int]) -> list[dict[str, int]]:
    if not sequences:
        return []
    intervals: list[tuple[int, int]] = []
    start = previous = sequences[0]
    for sequence in sequences[1:]:
        if sequence == previous + 1:
            previous = sequence
            continue
        intervals.append((start, previous))
        start = previous = sequence
    intervals.append((start, previous))
    return [
        {
            "first_sequence": first,
            "last_sequence": last,
            "inclusive_length": last - first + 1,
        }
        for first, last in intervals
    ]


def format_multiply(row: dict[str, Any], name: str, event_ids: list[int]) -> dict[str, Any]:
    return {
        "sequence": int(row["sequence"]),
        "pc": hex_word(int(row["pc"])),
        "opcode": hex_word(int(row["opcode"])),
        "name": name,
        "event_ids": event_ids,
        "source_class_views": source_class_views(row, name),
    }


def parse_trace(path: Path) -> tuple[
    list[dict[str, Any]],
    dict[int, list[dict[str, Any]]],
    dict[int, list[dict[str, Any]]],
    dict[str, str],
]:
    rows: list[dict[str, Any]] = []
    by_sequence: dict[int, list[dict[str, Any]]] = collections.defaultdict(list)
    by_event: dict[int, list[dict[str, Any]]] = collections.defaultdict(list)
    runtime: dict[str, str] = {}
    with open_text(path) as file:
        for line_number, line in enumerate(file, 1):
            if line.startswith("Mupen64Plus Console User-Interface"):
                runtime["emulator_banner"] = line.rstrip("\n")
            elif line.startswith("Core: Starting R4300 emulator:"):
                runtime["cpu_mode"] = line.rstrip("\n").split(":", 2)[-1].strip()
            if line.startswith("MARIO_Y_CONTEXT,"):
                row = parse_context_line(line, line_number)
                row["_line"] = line_number
                rows.append(row)
                by_sequence[int(row["sequence"])].append(row)
                by_event[int(row["event"])].append(row)
    if not rows:
        raise TraceInputError("trace contains no MARIO_Y_CONTEXT rows")
    return rows, by_sequence, by_event, runtime


def validate_contexts(
    rows: list[dict[str, Any]],
    by_sequence: dict[int, list[dict[str, Any]]],
    by_event: dict[int, list[dict[str, Any]]],
) -> tuple[dict[int, dict[str, Any]], list[dict[str, Any]]]:
    conflicts: list[dict[str, Any]] = []
    for sequence, duplicates in sorted(by_sequence.items()):
        first_payload = canonical_payload(duplicates[0])
        for duplicate in duplicates[1:]:
            if canonical_payload(duplicate) != first_payload:
                conflicts.append(
                    {
                        "sequence": sequence,
                        "lines": [int(item["_line"]) for item in duplicates],
                        "payloads": [list(canonical_payload(item)) for item in duplicates],
                    }
                )
        ages_by_event: dict[int, set[int]] = collections.defaultdict(set)
        for duplicate in duplicates:
            ages_by_event[int(duplicate["event"])].add(int(duplicate["age"]))
        for event, ages in sorted(ages_by_event.items()):
            if len(ages) > 1:
                conflicts.append(
                    {
                        "sequence": sequence,
                        "event": event,
                        "lines": [
                            int(item["_line"])
                            for item in duplicates
                            if int(item["event"]) == event
                        ],
                        "reason": "same sequence has multiple ages in one event",
                    }
                )
    if conflicts:
        raise TraceInputError(
            "inconsistent duplicate sequence rows: "
            + json.dumps(conflicts[:3], sort_keys=True)
        )

    for event, event_rows in sorted(by_event.items()):
        ages = sorted({int(row["age"]) for row in event_rows})
        row_count_by_age = collections.Counter(
            int(row["age"]) for row in event_rows
        )
        sequences_by_age: dict[int, int] = {}
        for row in event_rows:
            sequences_by_age.setdefault(int(row["age"]), int(row["sequence"]))
        full_ring = ages == list(range(64))
        one_row_per_age = full_ring and all(
            row_count_by_age[age] == 1 for age in range(64)
        )
        contiguous_sequence = one_row_per_age and all(
            sequences_by_age[age] == sequences_by_age[0] - age
            for age in range(64)
        )
        if not full_ring or not one_row_per_age or not contiguous_sequence:
            raise TraceInputError(
                f"event {event} does not contain a complete contiguous 64-instruction ring"
            )

    unique_rows = {
        sequence: duplicates[0]
        for sequence, duplicates in sorted(by_sequence.items())
    }
    return unique_rows, conflicts


def build_audit(trace: Path) -> dict[str, Any]:
    rows, by_sequence, by_event, runtime = parse_trace(trace)
    unique_rows, duplicate_conflicts = validate_contexts(rows, by_sequence, by_event)
    sequences = sorted(unique_rows)
    intervals = merge_intervals(sequences)
    duplicate_sequence_counts = {
        str(sequence): len(duplicates)
        for sequence, duplicates in sorted(by_sequence.items())
        if len(duplicates) > 1
    }

    event_ids_for_sequence = {
        sequence: sorted({int(row["event"]) for row in duplicates})
        for sequence, duplicates in by_sequence.items()
    }
    multiply_rows: list[tuple[int, str, dict[str, Any]]] = []
    multiply_pc_counts: collections.Counter[str] = collections.Counter()
    multiply_name_counts: collections.Counter[str] = collections.Counter()
    for sequence, row in sorted(unique_rows.items()):
        name = decode_multiply(row)
        if name is None:
            continue
        multiply_rows.append((sequence, name, row))
        multiply_name_counts[name] += 1
        multiply_pc_counts[hex_word(int(row["pc"]))] += 1

    all_multiply_by_sequence = {
        sequence: (name, row) for sequence, name, row in multiply_rows
    }
    adjacent_multiply_pairs: list[dict[str, Any]] = []
    adjacent_fp_pairs: list[dict[str, Any]] = []
    candidate_erratum_pairs: list[dict[str, Any]] = []
    for sequence, (first_name, first_row) in sorted(all_multiply_by_sequence.items()):
        second = all_multiply_by_sequence.get(sequence + 1)
        if second is None:
            continue
        second_name, second_row = second
        pair = {
            "first": format_multiply(first_row, first_name, event_ids_for_sequence[sequence]),
            "second": format_multiply(
                second_row, second_name, event_ids_for_sequence[sequence + 1]
            ),
            "dynamic_sequence_gap": 1,
            "intervening_instruction_count": 0,
        }
        adjacent_multiply_pairs.append(pair)
        if first_name in FP_MULTIPLY_NAMES:
            adjacent_fp_pairs.append(pair)
            if source_has_erratum_class_any_recorded_word(
                source_class_views(first_row, first_name), first_name
            ):
                candidate_erratum_pairs.append(pair)

    fp_rows = [
        (sequence, name, row)
        for sequence, name, row in multiply_rows
        if name in FP_MULTIPLY_NAMES
    ]
    mul_s_rows = [(sequence, row) for sequence, name, row in fp_rows if name == "MUL.S"]
    low_zero_mul_s = [
        (sequence, row)
        for sequence, row in mul_s_rows
        if any(
            classify_float32(int(row[key]) & 0xFFFFFFFF) == "finite_zero"
            for key in ("fpr_fs", "fpr_ft")
        )
    ]
    high_zero_mul_s = [
        (sequence, row)
        for sequence, row in mul_s_rows
        if any(
            classify_float32((int(row[key]) >> 32) & 0xFFFFFFFF) == "finite_zero"
            for key in ("fpr_fs", "fpr_ft")
        )
    ]
    primary_word_zero_mul_s = [
        (sequence, row)
        for sequence, row in mul_s_rows
        if source_has_zero_primary_word(source_class_views(row, "MUL.S"))
    ]
    any_recorded_word_zero_mul_s = [
        (sequence, row)
        for sequence, row in mul_s_rows
        if source_has_zero_any_recorded_word(source_class_views(row, "MUL.S"))
    ]
    any_recorded_word_erratum_mul_s = [
        (sequence, row)
        for sequence, row in mul_s_rows
        if source_has_erratum_class_any_recorded_word(
            source_class_views(row, "MUL.S"), "MUL.S"
        )
    ]
    low_erratum_mul_s = [
        (sequence, row)
        for sequence, row in mul_s_rows
        if any(
            classify_float32(int(row[key]) & 0xFFFFFFFF)
            in ERRATUM_SOURCE_CLASSES
            for key in ("fpr_fs", "fpr_ft")
        )
    ]
    high_erratum_mul_s = [
        (sequence, row)
        for sequence, row in mul_s_rows
        if any(
            classify_float32((int(row[key]) >> 32) & 0xFFFFFFFF)
            in ERRATUM_SOURCE_CLASSES
            for key in ("fpr_fs", "fpr_ft")
        )
    ]

    def class_counts(
        rows_for_count: list[tuple[int, dict[str, Any]]], word: str
    ) -> dict[str, dict[str, int]]:
        values: dict[str, list[str]] = {"fs": [], "ft": []}
        for _, row in rows_for_count:
            for source in ("fs", "ft"):
                bits = int(row["fpr_" + source])
                if word == "low32":
                    bits &= 0xFFFFFFFF
                else:
                    bits = (bits >> 32) & 0xFFFFFFFF
                values[source].append(classify_float32(bits))
        return {source: count_classes(classes) for source, classes in values.items()}
    def any_recorded_word_class_counts(
        rows_for_count: list[tuple[int, dict[str, Any]]]
    ) -> dict[str, dict[str, int]]:
        values: dict[str, list[str]] = {"fs": [], "ft": []}
        for _, row in rows_for_count:
            views = source_class_views(row, "MUL.S")
            for source in ("fs", "ft"):
                values[source].extend(
                    [
                        views["low32"][source],
                        views["high32"][source],
                    ]
                )
        return {
            source: count_classes(classes) for source, classes in values.items()
        }

    def mul_s_lane_status(
        rows_for_status: list[tuple[int, dict[str, Any]]]
    ) -> dict[str, Any]:
        active_classes: list[str] = []
        low_classes: list[str] = []
        high_classes: list[str] = []
        differs_low = 0
        differs_high = 0
        differs_either = 0
        matches_any = 0
        for _, row in rows_for_status:
            views = source_class_views(row, "MUL.S")
            active = classify_float32(int(row["fpr_ft_single"]))
            low = views["low32"]["ft"]
            high = views["high32"]["ft"]
            active_classes.append(active)
            low_classes.append(low)
            high_classes.append(high)
            differs_low += active != low
            differs_high += active != high
            differs_either += active != low or active != high
            matches_any += active in views["any_recorded_word"]["ft"]
        row_count = len(rows_for_status)
        return {
            "status": "unresolved_active_lane",
            "row_count": row_count,
            "fpr_ft_single_is_separate": True,
            "active_ft_single_class_counts": count_classes(active_classes),
            "recorded_ft_low32_class_counts": count_classes(low_classes),
            "recorded_ft_high32_class_counts": count_classes(high_classes),
            "rows_active_ft_differs_from_low32": differs_low,
            "rows_active_ft_differs_from_high32": differs_high,
            "rows_active_ft_differs_from_either_recorded_word": differs_either,
            "rows_active_ft_matches_any_recorded_word": matches_any,
            "disagreement_observed": differs_either > 0,
        }

    def gap_summary(rows_for_gap: list[tuple[int, dict[str, Any]]]) -> dict[str, Any]:
        ordered = sorted(rows_for_gap)
        gaps = [second[0] - first[0] for first, second in zip(ordered, ordered[1:])]
        if not gaps:
            return {
                "instruction_count": len(ordered),
                "minimum_dynamic_sequence_gap": None,
                "minimum_intervening_instruction_count": None,
                "gap_histogram": {},
                "minimum_gap_pairs": [],
            }
        minimum = min(gaps)
        min_pairs = []
        for (first_sequence, first_row), (second_sequence, second_row) in zip(
            ordered, ordered[1:]
        ):
            if second_sequence - first_sequence != minimum:
                continue
            min_pairs.append(
                {
                    "first_sequence": first_sequence,
                    "second_sequence": second_sequence,
                    "dynamic_sequence_gap": minimum,
                    "intervening_instruction_count": minimum - 1,
                    "first_pc": hex_word(int(first_row["pc"])),
                    "second_pc": hex_word(int(second_row["pc"])),
                    "event_ids": sorted(
                        set(event_ids_for_sequence[first_sequence])
                        | set(event_ids_for_sequence[second_sequence])
                    ),
                }
            )
        histogram = collections.Counter(gaps)
        return {
            "instruction_count": len(ordered),
            "minimum_dynamic_sequence_gap": minimum,
            "minimum_intervening_instruction_count": minimum - 1,
            "gap_histogram": {str(gap): histogram[gap] for gap in sorted(histogram)},
            "minimum_gap_pairs": min_pairs,
        }

    def following_multiply_gap_summary(
        first_rows: list[tuple[int, dict[str, Any]]],
    ) -> dict[str, Any]:
        ordered_multiply_rows = sorted(
            (sequence, row)
            for sequence, _, row in multiply_rows
        )
        pairs: list[tuple[int, dict[str, Any], int, dict[str, Any]]] = []
        for first_sequence, first_row in sorted(first_rows):
            following = next(
                (
                    (second_sequence, second_row)
                    for second_sequence, second_row in ordered_multiply_rows
                    if second_sequence > first_sequence
                ),
                None,
            )
            if following is None:
                continue
            pairs.append(
                (first_sequence, first_row, following[0], following[1])
            )
        gaps = [
            second_sequence - first_sequence
            for first_sequence, _, second_sequence, _ in pairs
        ]
        if not gaps:
            return {
                "source_instruction_count": len(first_rows),
                "following_multiply_count": 0,
                "minimum_dynamic_sequence_gap": None,
                "minimum_intervening_instruction_count": None,
                "gap_histogram": {},
                "minimum_gap_pairs": [],
            }
        minimum = min(gaps)
        minimum_pairs = [
            {
                "first_sequence": first_sequence,
                "second_sequence": second_sequence,
                "dynamic_sequence_gap": minimum,
                "intervening_instruction_count": minimum - 1,
                "first_pc": hex_word(int(first_row["pc"])),
                "second_pc": hex_word(int(second_row["pc"])),
                "event_ids": sorted(
                    set(event_ids_for_sequence[first_sequence])
                    | set(event_ids_for_sequence[second_sequence])
                ),
            }
            for first_sequence, first_row, second_sequence, second_row in pairs
            if second_sequence - first_sequence == minimum
        ]
        histogram = collections.Counter(gaps)
        return {
            "source_instruction_count": len(first_rows),
            "following_multiply_count": len(pairs),
            "minimum_dynamic_sequence_gap": minimum,
            "minimum_intervening_instruction_count": minimum - 1,
            "gap_histogram": {
                str(gap): histogram[gap] for gap in sorted(histogram)
            },
            "minimum_gap_pairs": minimum_pairs,
        }
    primary_zero_same_class_spacing = gap_summary(primary_word_zero_mul_s)
    primary_zero_following_multiply_spacing = following_multiply_gap_summary(
        primary_word_zero_mul_s
    )
    any_zero_same_class_spacing = gap_summary(any_recorded_word_zero_mul_s)
    any_zero_following_multiply_spacing = following_multiply_gap_summary(
        any_recorded_word_zero_mul_s
    )
    any_erratum_same_class_spacing = gap_summary(
        any_recorded_word_erratum_mul_s
    )
    any_erratum_following_multiply_spacing = following_multiply_gap_summary(
        any_recorded_word_erratum_mul_s
    )

    def format_mul_s_observation(
        sequence: int, row: dict[str, Any]
    ) -> dict[str, Any]:
        views = source_class_views(row, "MUL.S")
        active_class = classify_float32(int(row["fpr_ft_single"]))
        return {
            "sequence": sequence,
            "pc": hex_word(int(row["pc"])),
            "opcode": hex_word(int(row["opcode"])),
            "event_ids": event_ids_for_sequence[sequence],
            "source_classes_low32": views["low32"],
            "source_classes_high32": views["high32"],
            "source_classes_any_recorded_word": views["any_recorded_word"],
            "ft_active_single_bits": hex_word(int(row["fpr_ft_single"])),
            "ft_active_single_class": active_class,
            "ft_active_single_matches_low32_ft": (
                active_class == views["low32"]["ft"]
            ),
            "ft_active_single_matches_high32_ft": (
                active_class == views["high32"]["ft"]
            ),
        }


    observed_zero_rows = [
        format_mul_s_observation(sequence, row)
        for sequence, row in primary_word_zero_mul_s
    ]
    observed_any_word_erratum_rows = [
        format_mul_s_observation(sequence, row)
        for sequence, row in any_recorded_word_erratum_mul_s
    ]
    observed_any_word_zero_rows = [
        format_mul_s_observation(sequence, row)
        for sequence, row in any_recorded_word_zero_mul_s
    ]

    multiply_classes: dict[str, Any] = {}
    for name in ("MUL.S", "MUL.D"):
        name_rows = [
            (sequence, row)
            for sequence, candidate, row in fp_rows
            if candidate == name
        ]
        views = [source_class_views(row, name) for _, row in name_rows]
        if name == "MUL.S":
            multiply_classes[name] = {
                "count": len(name_rows),
                "low32_source_class_counts": class_counts(name_rows, "low32"),
                "high32_source_class_counts": class_counts(name_rows, "high32"),
                "any_recorded_word_source_class_counts": (
                    any_recorded_word_class_counts(name_rows)
                ),
            }
        else:
            multiply_classes[name] = {
                "count": len(name_rows),
                "double64_source_class_counts": {
                    source: count_classes(
                        [view["double64"][source] for view in views]
                    )
                    for source in ("fs", "ft")
                },
            }

    duplicate_rows = len(rows) - len(unique_rows)
    sequence_first = sequences[0]
    sequence_last = sequences[-1]
    sequence_span_length = sequence_last - sequence_first + 1
    events = sorted(by_event)
    complete_ring_count = sum(
        1 for event_rows in by_event.values()
        if len({int(row["age"]) for row in event_rows}) == 64
    )

    audit: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "input": {
            "trace_path": str(trace),
            "trace_sha256": sha256(trace),
        },
        "tool": {
            "name": "scripts/analyze_r4300_mul_hazards.py",
            "version": ANALYZER_VERSION,
            "script_sha256": sha256(Path(__file__).resolve()),
            "python_version": platform.python_version(),
            "python_implementation": platform.python_implementation(),
        },
        "sources": [
            {
                "name": ERRATUM_TITLE,
                "revision": ERRATUM_REVISION,
                "url": ERRATUM_URL,
            }
        ],
        "trace_runtime": runtime,
        "erratum": {
            "condition": (
                "Nintendo Developer News 1.2 says the first multiply must be "
                "MUL.S or MUL.D with one or both source operands sNaN, zero, "
                "or infinity; the second multiply may be any floating-point or "
                "integer multiply. The sequence is back-to-back either in source "
                "code or from a branch delay slot to the branch target. One "
                "intervening instruction prevents the problem."
            ),
            "adjacency_key": "dynamic sequence difference exactly 1; this includes branch-delay-to-target adjacency",
            "source_quote": (
                "The error happens only when the first multiply is single- or "
                "double-precision floating-point operation and when one or both "
                "of its source operands are: Signalling Not-a-Number (sNaN), 0 "
                "(Zero), or infinity (Inf)."
            ),
        },
        "observations": {
            "context_rows": len(rows),
            "unique_dynamic_sequences": len(unique_rows),
            "duplicate_rows": duplicate_rows,
            "duplicate_sequence_count": len(duplicate_sequence_counts),
            "duplicate_sequence_multiplicities": duplicate_sequence_counts,
            "context_event_count": len(events),
            "context_event_first": events[0],
            "context_event_last": events[-1],
            "context_rings": {
                "ring_size": 64,
                "complete_ring_count": complete_ring_count,
                "rows_per_event_histogram": {
                    str(count): number
                    for count, number in sorted(
                        collections.Counter(
                            len(rows_for_event) for rows_for_event in by_event.values()
                        ).items()
                    )
                },
            },
            "context_sequence_span": {
                "first_sequence": sequence_first,
                "last_sequence": sequence_last,
                "inclusive_length": sequence_span_length,
                "observed_unique_sequences": len(unique_rows),
                "unobserved_sequences_within_span": sequence_span_length - len(unique_rows),
                "observed_fraction_numerator": len(unique_rows),
                "observed_fraction_denominator": sequence_span_length,
                "interval_count": len(intervals),
                "intervals": intervals,
            },
            "multiply_opcode_counts": {
                name: multiply_name_counts.get(name, 0) for name in MULTIPLY_NAMES
            },
            "multiply_pc_counts": {
                pc: multiply_pc_counts[pc] for pc in sorted(multiply_pc_counts)
            },
            "multiply_source_class_counts": multiply_classes,
            "primary_word_zero_mul_s": {
                "definition": (
                    "Historical low32-primary MUL.S view: at least one source "
                    "low32 recorded FPR word classifies as finite zero. This "
                    "does not resolve the active MUL.S lane; fpr_ft_single is "
                    "reported separately."
                ),
                "instruction_count": len(primary_word_zero_mul_s),
                "low32_instruction_count": len(low_zero_mul_s),
                "high32_instruction_count": len(high_zero_mul_s),
                "rows": observed_zero_rows,
                "same_class_spacing": primary_zero_same_class_spacing,
                "following_multiply_spacing": (
                    primary_zero_following_multiply_spacing
                ),
                "low32_same_class_spacing": gap_summary(low_zero_mul_s),
                "high32_same_class_spacing": gap_summary(high_zero_mul_s),
                "event_ids": sorted(
                    {
                        event
                        for sequence, _ in primary_word_zero_mul_s
                        for event in event_ids_for_sequence[sequence]
                    }
                ),
                "pc_counts": {
                    pc: count
                    for pc, count in sorted(
                        collections.Counter(
                            row["pc"] for row in observed_zero_rows
                        ).items()
                    )
                },
            },
            "any_recorded_word_zero_mul_s": {
                "definition": (
                    "Conservative MUL.S zero view: at least one source has "
                    "finite_zero in either recorded low32 or high32 FPR word."
                ),
                "instruction_count": len(any_recorded_word_zero_mul_s),
                "low32_instruction_count": len(low_zero_mul_s),
                "high32_instruction_count": len(high_zero_mul_s),
                "rows": observed_any_word_zero_rows,
                "same_class_spacing": any_zero_same_class_spacing,
                "following_multiply_spacing": (
                    any_zero_following_multiply_spacing
                ),
                "low32_same_class_spacing": gap_summary(low_zero_mul_s),
                "high32_same_class_spacing": gap_summary(high_zero_mul_s),
                "event_ids": sorted(
                    {
                        event
                        for sequence, _ in any_recorded_word_zero_mul_s
                        for event in event_ids_for_sequence[sequence]
                    }
                ),
                "pc_counts": {
                    pc: count
                    for pc, count in sorted(
                        collections.Counter(
                            row["pc"] for row in observed_any_word_zero_rows
                        ).items()
                    )
                },
            },
            "any_recorded_word_erratum_mul_s": {
                "definition": (
                    "Conservative MUL.S erratum-source view: at least one "
                    "source has sNaN, finite_zero, or infinity in either "
                    "recorded low32 or high32 FPR word. Candidate eligibility "
                    "uses this unresolved-lane view."
                ),
                "instruction_count": len(any_recorded_word_erratum_mul_s),
                "zero_instruction_count": len(any_recorded_word_zero_mul_s),
                "low32_instruction_count": len(low_erratum_mul_s),
                "high32_instruction_count": len(high_erratum_mul_s),
                "rows": observed_any_word_erratum_rows,
                "same_class_spacing": any_erratum_same_class_spacing,
                "following_multiply_spacing": (
                    any_erratum_following_multiply_spacing
                ),
                "source_class_counts": any_recorded_word_class_counts(mul_s_rows),
                "active_lane_status": mul_s_lane_status(mul_s_rows),
                "event_ids": sorted(
                    {
                        event
                        for sequence, _ in any_recorded_word_erratum_mul_s
                        for event in event_ids_for_sequence[sequence]
                    }
                ),
                "pc_counts": {
                    pc: count
                    for pc, count in sorted(
                        collections.Counter(
                            row["pc"] for row in observed_any_word_erratum_rows
                        ).items()
                    )
                },
            },
            "adjacent_multiply_pairs": {
                "observed_dynamic_transition_count": sum(
                    1 for sequence in unique_rows if sequence + 1 in unique_rows
                ),
                "all_multiply_pair_count": len(adjacent_multiply_pairs),
                "first_fp_to_second_multiply_pair_count": len(adjacent_fp_pairs),
                "candidate_erratum_pair_count": len(candidate_erratum_pairs),
                "candidate_eligibility": (
                    "MUL.S uses any recorded low32/high32 source class while "
                    "the active lane is unresolved; MUL.D uses its recorded "
                    "double64 source classes."
                ),
                "all_multiply_pairs": adjacent_multiply_pairs,
                "candidate_erratum_pairs": candidate_erratum_pairs,
            },
            "relevant_mario_y_event_ids": {
                "events_with_any_multiply": sorted(
                    {
                        event
                        for _, _, row in multiply_rows
                        for event in event_ids_for_sequence[int(row["sequence"])]
                    }
                ),
                "events_with_primary_word_zero_mul_s": sorted(
                    {
                        event
                        for sequence, _ in primary_word_zero_mul_s
                        for event in event_ids_for_sequence[sequence]
                    }
                ),
                "events_with_any_recorded_word_zero_mul_s": sorted(
                    {
                        event
                        for sequence, _ in any_recorded_word_zero_mul_s
                        for event in event_ids_for_sequence[sequence]
                    }
                ),
                "events_with_any_recorded_word_erratum_mul_s": sorted(
                    {
                        event
                        for sequence, _ in any_recorded_word_erratum_mul_s
                        for event in event_ids_for_sequence[sequence]
                    }
                ),
                "events_with_candidate_erratum_pairs": sorted(
                    {
                        event
                        for pair in candidate_erratum_pairs
                        for side in ("first", "second")
                        for event in pair[side]["event_ids"]
                    }
                ),
            },
            "validation": {
                "malformed_context_rows": 0,
                "duplicate_sequence_conflicts": len(duplicate_conflicts),
                "all_context_register_fields_match_opcode": True,
                "all_context_events_have_contiguous_64_instruction_rings": True,
            },
        },
        "inference": {
            "candidate_erratum_pairs_zero_in_observed_route_context_any_recorded_word": (
                len(candidate_erratum_pairs) == 0
            ),
            "candidate_eligibility": (
                "MUL.S first-source eligibility includes an erratum class in "
                "either recorded low32 or high32 word because the active lane "
                "is unresolved; fpr_ft_single remains separate evidence."
            ),
            "any_recorded_word_zero_mul_s_instruction_count": len(
                any_recorded_word_zero_mul_s
            ),
            "any_recorded_word_erratum_mul_s_instruction_count": len(
                any_recorded_word_erratum_mul_s
            ),
            "minimum_any_recorded_word_zero_to_following_multiply_dynamic_sequence_gap": (
                any_zero_following_multiply_spacing[
                    "minimum_dynamic_sequence_gap"
                ]
            ),
            "minimum_any_recorded_word_zero_to_following_multiply_intervening_instruction_count": (
                any_zero_following_multiply_spacing[
                    "minimum_intervening_instruction_count"
                ]
            ),
            "minimum_any_recorded_word_erratum_to_following_multiply_dynamic_sequence_gap": (
                any_erratum_following_multiply_spacing[
                    "minimum_dynamic_sequence_gap"
                ]
            ),
            "minimum_any_recorded_word_erratum_to_following_multiply_intervening_instruction_count": (
                any_erratum_following_multiply_spacing[
                    "minimum_intervening_instruction_count"
                ]
            ),
            "no_faulty_result_emulation_performed": True,
            "physical_initiator_or_normal_gameplay_trigger_solved": False,
        },
        "scope_limits": [
            "The 64-instruction rings around Mario-Y writes are route-only observations, not ROM-wide disassembly.",
            "Only multiply pairs whose two dynamic sequence rows are both retained are tested; gaps between rings remain unobserved.",
            "The audit does not emulate the R4300 faulty result or establish that a candidate would corrupt a result.",
            "MARIO_Y_CONTEXT stores raw 64-bit FPR snapshots and separately attempts one ft single snapshot. The low32 view remains explicit historical primary evidence, but active MUL.S lane identity is unresolved; conservative erratum eligibility therefore treats either recorded low32 or high32 source word as possible, while fpr_ft_single is retained separately and disagreement is reported.",
            "The physical initiator and any normal-gameplay trigger are not solved by this route trace.",
        ],
    }
    return audit


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Audit R4300 multiply hazards in MARIO_Y_CONTEXT route rings"
    )
    parser.add_argument("trace", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    try:
        audit = build_audit(args.trace)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(audit, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except (OSError, EOFError, UnicodeError, TraceInputError, gzip.BadGzipFile) as error:
        print(f"analyze_r4300_mul_hazards.py: {error}", file=sys.stderr)
        return 1
    pairs = audit["observations"]["adjacent_multiply_pairs"]
    primary_spacing = audit["observations"]["primary_word_zero_mul_s"][
        "following_multiply_spacing"
    ]
    any_zero_spacing = audit["observations"]["any_recorded_word_zero_mul_s"][
        "following_multiply_spacing"
    ]
    any_erratum_spacing = audit["observations"][
        "any_recorded_word_erratum_mul_s"
    ]["following_multiply_spacing"]
    print(
        f"contexts={audit['observations']['context_rows']} "
        f"unique={audit['observations']['unique_dynamic_sequences']} "
        f"muls={sum(audit['observations']['multiply_opcode_counts'].values())} "
        f"candidate_pairs={pairs['candidate_erratum_pair_count']} "
        f"primary_zero_mul_s_gap={primary_spacing['minimum_dynamic_sequence_gap']} "
        f"any_zero_mul_s_gap={any_zero_spacing['minimum_dynamic_sequence_gap']} "
        f"any_erratum_mul_s_gap={any_erratum_spacing['minimum_dynamic_sequence_gap']} "
        f"output={args.output}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
