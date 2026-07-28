#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import math
import struct
from datetime import datetime, timezone
from pathlib import Path


def parse_range(value: str) -> list[int]:
    parts = [int(part) for part in value.split(":")]
    if len(parts) == 1:
        return parts
    if len(parts) not in (2, 3):
        raise argparse.ArgumentTypeError("range must be VALUE, START:STOP, or START:STOP:STEP")
    values = list(range(*parts))
    if not values:
        raise argparse.ArgumentTypeError(f"empty range: {value}")
    return values


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def input_offset(data: bytes) -> int:
    if data[:4] != b"M64\x1a":
        raise ValueError("invalid M64 signature")
    version = struct.unpack_from("<I", data, 4)[0]
    return 0x400 if version >= 3 else 0x200


def input_chunks(data: bytes) -> list[bytes]:
    offset = input_offset(data)
    trailing = len(data) - offset
    if trailing % 4 != 0:
        raise ValueError(f"M64 input payload has {trailing % 4} trailing bytes")
    return [data[index : index + 4] for index in range(offset, len(data), 4)]


def rotate_input(chunk: bytes, angle_degrees: int) -> bytes:
    if angle_degrees % 360 == 0:
        return chunk
    buttons, x, y = struct.unpack(">Hbb", chunk)
    angle = math.radians(angle_degrees)
    rotated_x = round(x * math.cos(angle) - y * math.sin(angle))
    rotated_y = round(x * math.sin(angle) + y * math.cos(angle))
    rotated_x = min(127, max(-128, rotated_x))
    rotated_y = min(127, max(-128, rotated_y))
    return struct.pack(">Hbb", buttons, rotated_x, rotated_y)


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a grid of M64 movies by splicing two input streams")
    parser.add_argument("prefix_m64", type=Path)
    parser.add_argument("route_m64", type=Path)
    parser.add_argument("output_dir", type=Path)
    parser.add_argument("--prefix-counts", type=parse_range, default=parse_range("63"))
    parser.add_argument("--route-starts", type=parse_range, default=parse_range("54"))
    parser.add_argument("--angles", type=parse_range, default=parse_range("0"))
    parser.add_argument("--route-end", type=int)
    args = parser.parse_args()

    prefix_data = args.prefix_m64.read_bytes()
    route_data = args.route_m64.read_bytes()
    prefix_offset = input_offset(prefix_data)
    prefix_inputs = input_chunks(prefix_data)
    route_inputs = input_chunks(route_data)
    route_declared_samples = struct.unpack_from("<I", route_data, 0x18)[0]
    route_end = args.route_end if args.route_end is not None else route_declared_samples

    if route_end > len(route_inputs):
        raise ValueError(f"route end {route_end} exceeds {len(route_inputs)} stored inputs")
    if max(args.prefix_counts) > len(prefix_inputs):
        raise ValueError(f"prefix count exceeds {len(prefix_inputs)} stored inputs")
    if max(args.route_starts) >= route_end:
        raise ValueError(f"route start must be below route end {route_end}")

    args.output_dir.mkdir(parents=True, exist_ok=True)
    entries: list[dict[str, object]] = []
    for prefix_count in args.prefix_counts:
        for route_start in args.route_starts:
            for angle in args.angles:
                chunks = prefix_inputs[:prefix_count]
                chunks += [rotate_input(chunk, angle) for chunk in route_inputs[route_start:route_end]]
                header = bytearray(prefix_data[:prefix_offset])
                struct.pack_into("<I", header, 0x0C, 0xFFFFFFFF)
                struct.pack_into("<I", header, 0x18, len(chunks))
                movie = bytes(header) + b"".join(chunks)
                name = f"p{prefix_count:03d}_r{route_start:03d}_a{angle:+04d}.m64"
                (args.output_dir / name).write_bytes(movie)
                entries.append(
                    {
                        "movie": name,
                        "prefix_count": prefix_count,
                        "route_start": route_start,
                        "angle_degrees": angle,
                        "input_samples": len(chunks),
                        "sha256": sha256(movie),
                    }
                )

    manifest = {
        "schema_version": 1,
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "prefix_m64": {
            "path": str(args.prefix_m64),
            "sha256": sha256(prefix_data),
            "stored_inputs": len(prefix_inputs),
        },
        "route_m64": {
            "path": str(args.route_m64),
            "sha256": sha256(route_data),
            "stored_inputs": len(route_inputs),
            "declared_inputs": route_declared_samples,
        },
        "route_end": route_end,
        "entries": entries,
    }
    encoded = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    (args.output_dir / "manifest.json").write_text(encoded)
    print(f"wrote {len(entries)} movies to {args.output_dir}")


if __name__ == "__main__":
    main()
