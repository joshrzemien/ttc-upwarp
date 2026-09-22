#!/usr/bin/env python3
from __future__ import annotations

import argparse
import gzip
import hashlib
import struct
from pathlib import Path

OLD_MD5_END = 0x20
OLD_LUT_R = 0x802208
OLD_LUT_W = 0x902208
OLD_AFTER_LUT = 0xA02208
OLD_CP0 = 0xA0230C
OLD_AFTER_CP0 = 0xA0240C
OLD_QUEUE = 0xA02BB4
NEW_STATE_DATA_SIZE = 16_788_244


def queue_end(raw: bytes) -> int:
    offset = OLD_QUEUE
    while offset + 4 <= len(raw):
        value = struct.unpack_from("<I", raw, offset)[0]
        offset += 4
        if value == 0xFFFFFFFF:
            return offset
    raise ValueError("savestate event queue has no 0xffffffff terminator")


def convert_state(source: Path, destination: Path, rom_md5: str | None = None) -> None:
    raw = gzip.decompress(source.read_bytes())
    source_md5 = raw[:32].decode("ascii").lower()
    if len(source_md5) != 32 or any(c not in "0123456789abcdef" for c in source_md5):
        raise ValueError("savestate has no valid embedded ROM MD5")
    if rom_md5 is not None and rom_md5.lower() != source_md5:
        raise ValueError(f"ROM MD5 {rom_md5} does not match savestate ROM {source_md5}")
    rom_md5 = source_md5
    end = queue_end(raw)

    cp0_32 = bytearray()
    for offset in range(OLD_CP0, OLD_AFTER_CP0, 8):
        cp0_32.extend(raw[offset : offset + 4])

    state_data = b"".join(
        (
            raw[OLD_MD5_END:OLD_LUT_R],
            bytes(2 * 0x100000 * 4),
            raw[OLD_AFTER_LUT:OLD_CP0],
            cp0_32,
            raw[OLD_AFTER_CP0:OLD_QUEUE],
        )
    )
    if len(state_data) != NEW_STATE_DATA_SIZE:
        raise ValueError(f"converted state body is {len(state_data)}, expected {NEW_STATE_DATA_SIZE}")

    header = b"M64+SAVE" + struct.pack(">I", 0x00010000) + rom_md5.upper().encode("ascii")
    converted = header + state_data + raw[OLD_QUEUE:end]
    destination.parent.mkdir(parents=True, exist_ok=True)
    # GzipFile emits the portable OS byte (255), unlike gzip.compress on
    # Python 3.11/3.12. Keep the historical bytes stable across Python versions.
    with destination.open("wb") as output:
        with gzip.GzipFile(filename="", mode="wb", fileobj=output, compresslevel=6, mtime=0) as stream:
            stream.write(converted)

    queue = [struct.unpack_from("<I", raw, offset)[0] for offset in range(OLD_QUEUE, end, 4)]
    print(f"source={source}")
    print(f"source_md5={raw[:32].decode('ascii', errors='replace')}")
    print(f"source_raw_size={len(raw)} queue_words={[hex(value) for value in queue]}")
    print(f"converted={destination} raw_size={len(converted)} gzip_size={destination.stat().st_size}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Convert a Mupen64 0.5 state for Fuzzy64")
    parser.add_argument("state", type=Path)
    parser.add_argument("--rom", type=Path, help="verify ROM identity; otherwise preserve the source state's embedded MD5")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    rom_md5 = None
    if args.rom is not None:
        rom = args.rom.read_bytes()
        rom_md5 = hashlib.md5(rom).hexdigest()
        print(f"rom={args.rom} size={len(rom)} md5={rom_md5}")
    convert_state(args.state, args.output, rom_md5)


if __name__ == "__main__":
    main()
