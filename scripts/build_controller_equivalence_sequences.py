#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import math
import struct
from collections import Counter
from pathlib import Path


def f32(value: float) -> float:
    return struct.unpack(">f", struct.pack(">f", value))[0]


def f32_word(value: float) -> int:
    return struct.unpack(">I", struct.pack(">f", value))[0]


def adjusted_stick(raw_x: int, raw_y: int) -> tuple[int, int, int]:
    stick_x = f32(raw_x + 6 if raw_x <= -8 else raw_x - 6 if raw_x >= 8 else 0)
    stick_y = f32(raw_y + 6 if raw_y <= -8 else raw_y - 6 if raw_y >= 8 else 0)
    squares = f32(f32(stick_x * stick_x) + f32(stick_y * stick_y))
    magnitude = f32(math.sqrt(squares))
    if magnitude > 64.0:
        scale = f32(f32(64.0) / magnitude)
        stick_x = f32(stick_x * scale)
        stick_y = f32(stick_y * scale)
        magnitude = f32(64.0)
    return f32_word(stick_x), f32_word(stick_y), f32_word(magnitude)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build one-update sequences for normalized N64 stick classes"
    )
    parser.add_argument("output", type=Path)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--max-x", type=int, default=61)
    parser.add_argument("--max-y", type=int, default=63)
    parser.add_argument(
        "--button-classes",
        type=int,
        default=16,
        help="A/B/Z/R bit classes; must be between 1 and 16",
    )
    args = parser.parse_args()
    if not 1 <= args.button_classes <= 16:
        raise SystemExit("--button-classes must be between 1 and 16")

    classes: dict[tuple[int, int, int], list[tuple[int, int]]] = {}
    for raw_x in range(-args.max_x, args.max_x + 1):
        for raw_y in range(-args.max_y, args.max_y + 1):
            classes.setdefault(adjusted_stick(raw_x, raw_y), []).append((raw_x, raw_y))

    rows: list[str] = []
    representatives: list[dict[str, object]] = []
    for index, (key, members) in enumerate(sorted(classes.items())):
        raw_x, raw_y = members[0]
        representatives.append(
            {
                "class": index,
                "stick_x_word": f"0x{key[0]:08X}",
                "stick_y_word": f"0x{key[1]:08X}",
                "stick_magnitude_word": f"0x{key[2]:08X}",
                "representative": [raw_x, raw_y],
                "raw_member_count": len(members),
            }
        )
        for buttons in range(args.button_classes):
            rows.append(f"c{index:05}_b{buttons:02}|1:{buttons}:{raw_x}:{raw_y}")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text("\n".join(rows) + "\n")
    member_histogram = Counter(len(members) for members in classes.values())
    manifest = {
        "schema_version": 1,
        "raw_domain": {
            "x": [-args.max_x, args.max_x],
            "y": [-args.max_y, args.max_y],
            "pair_count": (2 * args.max_x + 1) * (2 * args.max_y + 1),
            "description": "Conservative rectangular superset of documented N64 controller values",
        },
        "normalization": {
            "deadzone": "raw values -7 through +7 map to zero per axis",
            "offset": "subtract 6 from values >=8; add 6 to values <=-8",
            "magnitude_cap": 64.0,
            "arithmetic": "IEEE-754 binary32 after each SM64 operation",
        },
        "button_bits": {"0": "A", "1": "B", "2": "Z", "3": "R"},
        "button_class_count": args.button_classes,
        "analog_equivalence_class_count": len(classes),
        "sequence_count": len(rows),
        "raw_members_per_class_histogram": {
            str(size): count for size, count in sorted(member_histogram.items())
        },
        "sequence_file": str(args.output),
        "sequence_sha256": sha256(args.output),
        "representatives": representatives,
    }
    manifest_path = args.manifest or args.output.with_suffix(".json")
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    print(
        f"raw_pairs={manifest['raw_domain']['pair_count']} "
        f"analog_classes={len(classes)} sequences={len(rows)} "
        f"sha256={manifest['sequence_sha256']} output={args.output} "
        f"manifest={manifest_path}"
    )


if __name__ == "__main__":
    main()
