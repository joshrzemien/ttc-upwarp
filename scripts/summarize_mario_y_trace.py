#!/usr/bin/env python3
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
import struct
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, TextIO


OPCODE_NAMES = {
    40: "SB",
    41: "SH",
    42: "SWL",
    43: "SW",
    44: "SDL",
    45: "SDR",
    46: "SWR",
    56: "SC",
    57: "SWC1",
    58: "SWC2",
    60: "SCD",
    61: "SDC1",
    62: "SDC2",
    63: "SD",
}
HEX_FIELDS = {
    "pc",
    "opcode",
    "address",
    "physical",
    "effective",
    "old",
    "new",
    "xor",
    "mask",
    "value",
    "base_gpr",
    "source_gpr",
    "source_fpr",
    "source_fpr_single",
    "destination",
    "source",
    "gpr_rs",
    "gpr_rt",
    "fpr_fs",
    "fpr_ft",
    "fpr_fd",
    "fpr_ft_single",
}
DECIMAL_FIELDS = {
    "event",
    "age",
    "sequence",
    "rs",
    "rt",
    "fs",
    "ft",
    "fd",
    "length",
    "count",
    "skip",
}


def open_text(path: Path) -> TextIO:
    if path.suffix == ".gz":
        return gzip.open(path, "rt", errors="replace")
    return path.open(errors="replace")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        while chunk := file.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def parse_fields(line: str) -> tuple[str, dict[str, Any]]:
    tokens = line.rstrip("\n").split(",")
    record: dict[str, Any] = {}
    for token in tokens[1:]:
        if "=" not in token:
            continue
        key, value = token.split("=", 1)
        if key in HEX_FIELDS:
            try:
                record[key] = int(value, 16)
            except ValueError:
                record[key] = value
        elif key in DECIMAL_FIELDS:
            try:
                record[key] = int(value)
            except ValueError:
                record[key] = value
        else:
            record[key] = value
    return tokens[0], record


def hex_word(value: int) -> str:
    return f"0x{value:08X}"


def word_float(value: int) -> float:
    return struct.unpack(">f", struct.pack(">I", value))[0]


def format_event(kind: str, raw: dict[str, Any]) -> dict[str, Any]:
    old_word = int(raw["old"])
    new_word = int(raw["new"])
    event: dict[str, Any] = {
        "event": int(raw["event"]),
        "kind": kind,
        "origin": str(raw.get("origin", "UNKNOWN")),
        "old_word": hex_word(old_word),
        "new_word": hex_word(new_word),
        "xor": hex_word(int(raw["xor"])),
        "old_y": word_float(old_word),
        "new_y": word_float(new_word),
        "y_delta": word_float(new_word) - word_float(old_word),
        "changed": old_word != new_word,
        "exact_incident_transition": old_word == 0xC5837800
        and new_word == 0xC4837800,
    }
    if kind == "memory_write":
        opcode = int(raw["opcode"])
        major = opcode >> 26
        mask = int(raw["mask"])
        source_word = (
            int(raw["source_fpr_single"])
            if major == 57
            else int(raw["source_gpr"]) & 0xFFFFFFFF
        )
        event.update(
            {
                "width": raw["width"],
                "pc": hex_word(int(raw["pc"])),
                "opcode": hex_word(opcode),
                "opcode_major": major,
                "opcode_name": OPCODE_NAMES.get(major, "UNKNOWN"),
                "address": hex_word(int(raw["address"])),
                "physical": hex_word(int(raw["physical"])),
                "effective_address": hex_word(int(raw["effective"])),
                "effective_address_matches": (
                    int(raw["effective"]) & 0x1FFFFFFF
                )
                == int(raw["physical"]),
                "mask": hex_word(mask),
                "source_register": int(raw["rt"]),
                "source_word": hex_word(source_word),
                "source_matches_full_write": mask == 0xFFFFFFFF
                and source_word == new_word,
            }
        )
    else:
        event.update(
            {
                "engine": raw["engine"],
                "pc_at_initiation": hex_word(int(raw["pc"])),
                "opcode_at_initiation": hex_word(int(raw["opcode"])),
                "destination": hex_word(int(raw["destination"])),
                "length": int(raw["length"]),
                "count": int(raw["count"]),
                "skip": int(raw["skip"]),
                "source": hex_word(int(raw["source"])),
            }
        )
    return event


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Summarize comprehensive Fuzzy64 Mario-Y provenance output"
    )
    parser.add_argument("trace", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--include-events", type=int, default=100)
    args = parser.parse_args()

    events: list[dict[str, Any]] = []
    contexts: dict[int, list[dict[str, Any]]] = defaultdict(list)
    malformed_trace_lines: list[str] = []
    with open_text(args.trace) as file:
        for line in file:
            if line.startswith("MARIO_Y_WRITE,"):
                try:
                    _, fields = parse_fields(line)
                    events.append(format_event("memory_write", fields))
                except (KeyError, TypeError, ValueError, struct.error):
                    malformed_trace_lines.append(line.rstrip())
            elif line.startswith("MARIO_Y_DMA,"):
                try:
                    _, fields = parse_fields(line)
                    events.append(format_event("dma", fields))
                except (KeyError, TypeError, ValueError, struct.error):
                    malformed_trace_lines.append(line.rstrip())
            elif line.startswith("MARIO_Y_CONTEXT,"):
                try:
                    _, fields = parse_fields(line)
                    contexts[int(fields["event"])].append(fields)
                except (KeyError, TypeError, ValueError):
                    malformed_trace_lines.append(line.rstrip())

    if not events:
        raise SystemExit("no Mario-Y events found")
    events.sort(key=lambda event: event["event"])
    event_ids = [event["event"] for event in events]
    duplicate_event_ids = sorted(
        event for event, count in Counter(event_ids).items() if count > 1
    )
    missing_event_ids = sorted(set(range(event_ids[0], event_ids[-1] + 1)) - set(event_ids))

    context_integrity: list[dict[str, Any]] = []
    for event in events:
        rows = contexts[event["event"]]
        ages = sorted(int(row["age"]) for row in rows)
        context_integrity.append(
            {
                "event": event["event"],
                "rows": len(rows),
                "ages_complete": ages == list(range(len(ages))),
                "age_zero_matches_event_pc": bool(rows)
                and next(
                    int(row["pc"]) for row in rows if int(row["age"]) == 0
                )
                == int(event.get("pc", event.get("pc_at_initiation")), 16),
            }
        )
    context_failures = [
        row
        for row in context_integrity
        if not row["ages_complete"] or not row["age_zero_matches_event_pc"]
    ]


    changed = [event for event in events if event["changed"]]
    write_events = [event for event in events if event["kind"] == "memory_write"]
    cpu_events = [event for event in write_events if event["origin"] == "CPU"]
    host_events = [event for event in write_events if event["origin"] != "CPU"]
    dma_events = [event for event in events if event["kind"] == "dma"]
    source_mismatches = [
        event
        for event in cpu_events
        if event["mask"] == "0xFFFFFFFF"
        and not event["source_matches_full_write"]
    ]
    pc_histogram = Counter(event["pc"] for event in cpu_events)
    opcode_histogram = Counter(event["opcode_name"] for event in cpu_events)
    origin_histogram = Counter(event["origin"] for event in events)
    engine_histogram = Counter(event["engine"] for event in dma_events)
    finite_delta_events = [
        event for event in events if math.isfinite(float(event["y_delta"]))
    ]
    largest_upward_write = max(
        finite_delta_events, key=lambda event: float(event["y_delta"])
    )
    largest_downward_write = min(
        finite_delta_events, key=lambda event: float(event["y_delta"])
    )


    summary = {
        "schema_version": 1,
        "trace": str(args.trace),
        "trace_sha256": sha256(args.trace),
        "coverage": {
            "emulator_mode": "Fuzzy64 pure interpreter",
            "target": "MarioState.pos[1] at virtual 0x80339E40 / physical 0x00339E40",
            "cpu": "All RDRAM and framebuffer write handlers, with PC, opcode, effective address, GPR/FPR sources, and a 64-instruction context ring",
            "dma": ["PI cartridge/save to RDRAM", "SI PIF to RDRAM", "SP memory to RDRAM"],
            "origin_tagging": "CPU execution and host Fuzzer writes are distinguished; DMA records identify their initiator origin",
            "limits": [
                "Pure-interpreter coverage does not validate N64 cache-coherency behavior.",
                "The trace observes emulated write paths, not physical DRAM or bus faults outside emulator semantics.",
                "RDP plugin-side framebuffer writes that bypass core memory handlers are outside this patch.",
            ],
        },
        "event_count": len(events),
        "memory_write_count": len(write_events),
        "cpu_write_count": len(cpu_events),
        "host_write_count": len(host_events),
        "changed_event_count": len(changed),
        "exact_incident_transition_count": sum(
            event["exact_incident_transition"] for event in events
        ),
        "single_bit_24_clear_count": sum(
            event["xor"] == "0x01000000"
            and int(event["old_word"], 16) & 0x01000000 != 0
            and int(event["new_word"], 16) & 0x01000000 == 0
            for event in events
        ),
        "largest_upward_write": largest_upward_write,
        "largest_downward_write": largest_downward_write,
        "origin_histogram": dict(sorted(origin_histogram.items())),
        "opcode_histogram": dict(sorted(opcode_histogram.items())),
        "pc_histogram": dict(sorted(pc_histogram.items())),
        "dma_engine_histogram": dict(sorted(engine_histogram.items())),
        "integrity": {
            "event_ids_contiguous": not missing_event_ids,
            "missing_event_ids": missing_event_ids,
            "duplicate_event_ids": duplicate_event_ids,
            "malformed_trace_line_count": len(malformed_trace_lines),
            "malformed_trace_lines": malformed_trace_lines[:20],
            "context_event_count": len(context_integrity),
            "context_failure_count": len(context_failures),
            "context_failures": context_failures[:20],
            "full_cpu_source_mismatch_count": len(source_mismatches),
            "full_cpu_source_mismatches": source_mismatches[:20],
        },
        "changed_events": changed[: args.include_events],
        "events": events[: args.include_events],
        "event_output_truncated": len(events) > args.include_events,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    print(
        f"events={len(events)} cpu={len(cpu_events)} dma={len(dma_events)} "
        f"changed={len(changed)} exact={summary['exact_incident_transition_count']} "
        f"malformed={len(malformed_trace_lines)} output={args.output}"
    )
    if malformed_trace_lines or missing_event_ids or duplicate_event_ids:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
