#!/usr/bin/env python3
from __future__ import annotations

import argparse
import struct
from pathlib import Path

BUTTONS = {
    0x0001: "C-R",
    0x0002: "C-L",
    0x0004: "C-D",
    0x0008: "C-U",
    0x0010: "R",
    0x0020: "L",
    0x0100: "D-R",
    0x0200: "D-L",
    0x0400: "D-D",
    0x0800: "D-U",
    0x1000: "START",
    0x2000: "Z",
    0x4000: "B",
    0x8000: "A",
}


def cstr(data: bytes) -> str:
    return data.split(b"\0", 1)[0].decode("utf-8", "replace")


def inspect(path: Path, show_inputs: bool) -> None:
    data = path.read_bytes()
    if data[:4] != b"M64\x1a":
        raise ValueError(f"{path}: invalid signature {data[:4]!r}")
    version, uid, vis, rerecords = struct.unpack_from("<4I", data, 4)
    fps, controllers, ext_version, ext_flags = struct.unpack_from("4B", data, 0x14)
    samples = struct.unpack_from("<I", data, 0x18)[0]
    start_type, controller_flags = struct.unpack_from("<H2xI", data, 0x1C)
    offset = 0x400 if version >= 3 else 0x200
    inputs = []
    for i in range((len(data) - offset) // 4):
        buttons, x, y = struct.unpack_from(">Hbb", data, offset + 4 * i)
        inputs.append((buttons, x, y))
    print(
        f"{path.name}: version={version} uid={uid} vis={vis} samples={samples} "
        f"stored={len(inputs)} rerecords={rerecords} fps={fps} controllers={controllers} "
        f"start_type={start_type} controller_flags=0x{controller_flags:08x} "
        f"ext={ext_version}:{ext_flags}"
    )
    print(
        f"  rom={cstr(data[0xC4:0xE4])!r} crc32=0x{struct.unpack_from('<I', data, 0xE4)[0]:08x} "
        f"country=0x{struct.unpack_from('<H', data, 0xE8)[0]:04x}"
    )
    print(
        f"  plugins: video={cstr(data[0x122:0x162])!r}; audio={cstr(data[0x162:0x1A2])!r}; "
        f"input={cstr(data[0x1A2:0x1E2])!r}; rsp={cstr(data[0x1E2:0x222])!r}"
    )
    print(f"  author={cstr(data[0x222:0x300])!r} description={cstr(data[0x300:0x400])!r}")
    if show_inputs:
        for i, (buttons, x, y) in enumerate(inputs):
            names = "+".join(name for mask, name in BUTTONS.items() if buttons & mask) or "-"
            print(f"  {i:03d}: {names:15s} x={x:4d} y={y:4d} raw=0x{buttons:04x}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("paths", nargs="+", type=Path)
    parser.add_argument("--inputs", action="store_true")
    args = parser.parse_args()
    for path in args.paths:
        inspect(path, args.inputs)


if __name__ == "__main__":
    main()
