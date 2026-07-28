#!/usr/bin/env python3
from __future__ import annotations

import argparse
import gzip
import math
import struct
from pathlib import Path

RDRAM_OFFSET = 0x1B0
RDRAM_SIZE = 8 * 1024 * 1024


def u32(memory: bytes, offset: int) -> int:
    return struct.unpack_from("<I", memory, offset)[0]


def f32(memory: bytes, offset: int) -> float:
    return struct.unpack_from("<f", memory, offset)[0]


def pointer(value: int) -> bool:
    return 0x80000000 <= value < 0x80800000


def main() -> None:
    parser = argparse.ArgumentParser(description="Locate MarioState candidates in a Mupen64 0.5 savestate")
    parser.add_argument("state", type=Path)
    args = parser.parse_args()

    raw = gzip.decompress(args.state.read_bytes())
    memory = raw[RDRAM_OFFSET : RDRAM_OFFSET + RDRAM_SIZE]
    candidates: list[tuple[int, int]] = []
    for base in range(0, len(memory) - 0xC8, 4):
        pos = [f32(memory, base + offset) for offset in (0x3C, 0x40, 0x44)]
        ptrs = [u32(memory, base + offset) for offset in (0x64, 0x68, 0x88, 0x90, 0x94, 0x98, 0x9C, 0xA0)]
        health = u32(memory, base + 0xAC) & 0xFFFF
        score = sum(pointer(value) for value in ptrs)
        score += 2 if pointer(ptrs[2]) else 0
        score += 2 if all(math.isfinite(value) and abs(value) < 100000 for value in pos) else 0
        score += 20 if health == 0x880 else (3 if health in range(0x100, 0x881, 0x100) else 0)
        if score >= 10:
            candidates.append((score, base))

    for score, base in sorted(candidates, reverse=True)[:50]:
        pos = [f32(memory, base + offset) for offset in (0x3C, 0x40, 0x44)]
        vel = [f32(memory, base + offset) for offset in (0x48, 0x4C, 0x50)]
        ptrs = [u32(memory, base + offset) for offset in (0x60, 0x64, 0x68, 0x78, 0x7C, 0x80, 0x84, 0x88, 0x8C, 0x90, 0x94, 0x98, 0x9C, 0xA0)]
        print(f"score={score} base=0x80{base:06x} action=0x{u32(memory, base + 0x0C):08x} "
              f"pos=({pos[0]:.3f}, {pos[1]:.3f}, {pos[2]:.3f}) "
              f"vel=({vel[0]:.3f}, {vel[1]:.3f}, {vel[2]:.3f}) "
              f"fvel={f32(memory, base + 0x54):.3f} health=0x{u32(memory, base + 0xAC) & 0xFFFF:04x}\n"
              f"  ptrs={' '.join(f'{value:08x}' for value in ptrs)}")


if __name__ == "__main__":
    main()
