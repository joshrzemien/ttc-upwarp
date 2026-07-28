#!/usr/bin/env python3
from pathlib import Path
import sys

source = Path(sys.argv[1])
destination = Path(sys.argv[2])
data = bytearray(source.read_bytes())
if len(data) % 4:
    raise SystemExit(f"ROM size {len(data)} is not word-aligned")

magic = bytes(data[:4])
if magic == bytes.fromhex("37804012"):  # byte-swapped (.v64)
    for offset in range(0, len(data), 2):
        data[offset : offset + 2] = data[offset : offset + 2][::-1]
elif magic == bytes.fromhex("40123780"):  # little-endian (.n64)
    for offset in range(0, len(data), 4):
        data[offset : offset + 4] = data[offset : offset + 4][::-1]
elif magic != bytes.fromhex("80371240"):
    raise SystemExit(f"unrecognized ROM magic: {magic.hex()}")

if data[:4] != bytes.fromhex("80371240"):
    raise SystemExit(f"unexpected converted magic: {data[:4].hex()}")
destination.write_bytes(data)
