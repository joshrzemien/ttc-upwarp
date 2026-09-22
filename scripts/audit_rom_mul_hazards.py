#!/usr/bin/env python3
"""Deterministic ROM-wide R4300 multiply-erratum audit.

The executable universe is taken from the matched JP linker map: aligned input
.text ranges in the boot, main, engine, and goddard output segments. ROM bytes
are decoded directly as big-endian MIPS words; ELF/map metadata binds ROM
offsets, symbols, and source objects. Non-.text data and known boot header/font
.text inputs are never decoded as instructions.
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import json
import platform
import re
import shlex
import struct
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = 1
ANALYZER_VERSION = "1"
ERRATUM_URL = "http://ultra64.ca/files/documentation/online-manuals/man/developerNews/news-02.html"
ERRATUM_TITLE = "Nintendo 64 Developers News 1.2"
ERRATUM_REVISION = "1.2"
SEGMENT_NAMES = ("boot", "main", "engine", "goddard")
# These map .text inputs are not R4300 CPU instructions:
# the first two are boot/header assets, and lib/rsp.o is bundled RSP
# microcode (the source explicitly emits .incbin blobs in .text/.rodata).
EXCLUDED_TEXT_OBJECTS = frozenset((
    "build/jp/asm/rom_header.o",
    "build/jp/asm/ipl3_font.o",
    "build/jp/lib/rsp.o",
))
EXCLUDED_TEXT_OBJECT_REASONS = {
    "build/jp/asm/rom_header.o": "ROM header/data, not CPU instructions",
    "build/jp/asm/ipl3_font.o": "IPL3 font blob, not CPU instructions",
    "build/jp/lib/rsp.o": "RSP microcode .incbin blobs, not R4300 CPU instructions",
}
MULTIPLY_NAMES = frozenset(("MUL.S", "MUL.D", "MULT", "MULTU", "DMULT", "DMULTU"))
FP_MULTIPLY_NAMES = frozenset(("MUL.S", "MUL.D"))
CONTROL_TRANSFER_NAMES = frozenset((
    "J", "JAL", "JR", "JALR", "BEQ", "BNE", "BLEZ", "BGTZ",
    "BLTZ", "BGEZ", "BLTZAL", "BGEZAL", "BC1F", "BC1T", "BC1FL", "BC1TL",
    "BREAK", "SYSCALL",
))
TERMINAL_NAMES = frozenset(("BREAK", "SYSCALL"))


class AuditError(RuntimeError):
    pass


def hex_word(value: int, width: int = 8) -> str:
    return f"0x{value:0{width}X}"

def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        while chunk := file.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def md5_file(path: Path) -> str:
    digest = hashlib.md5()
    with path.open("rb") as file:
        while chunk := file.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def run_capture(command: list[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            cwd=str(cwd) if cwd is not None else None,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="strict",
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise AuditError(f"command failed: {shlex.join(command)}: {error}") from error


def tool_record(path: Path) -> dict[str, Any]:
    version = run_capture([str(path), "--version"]).stdout
    return {
        "path": str(path),
        "sha256": sha256_file(path),
        "version_first_lines": version.splitlines()[:2],
    }


@dataclass(frozen=True)
class Segment:
    name: str
    virt_start: int
    virt_end: int
    rom_start: int
    rom_end: int

    @property
    def size(self) -> int:
        return self.virt_end - self.virt_start


@dataclass(frozen=True)
class CodeRange:
    segment: str
    start: int
    end: int
    rom_start: int
    rom_end: int
    object_name: str

    @property
    def size(self) -> int:
        return self.end - self.start

    @property
    def word_count(self) -> int:
        return self.size // 4


@dataclass(frozen=True)
class Symbol:
    start: int
    size: int
    kind: str
    name: str


@dataclass
class Insn:
    pc: int
    word: int
    name: str
    args: str
    multiply: str | None
    control: bool
    delay_slot: bool
    branch_kind: str | None = None
    target: int | None = None
    conditional: bool = False
    branch_likely: bool = False
    link: bool = False
    link_reg: int = 0
    rs: int = 0
    rt: int = 0
    rd: int = 0


@dataclass(frozen=True)
class Edge:
    src: int
    dst: int
    reasons: tuple[str, ...]
    metadata: tuple[tuple[str, str], ...]


def parse_map(map_path: Path) -> tuple[list[Segment], list[CodeRange], list[dict[str, Any]]]:
    text = map_path.read_text(encoding="utf-8", errors="strict")
    values: dict[str, int] = {}
    symbol_pattern = re.compile(
        r"^\s*0x([0-9A-Fa-f]+)\s+_(boot|main|engine|goddard)Segment"
        r"(Start|End|RomStart|RomEnd)\b"
    )
    for line in text.splitlines():
        match = symbol_pattern.match(line)
        if match:
            values[f"{match.group(2)}{match.group(3)}"] = int(match.group(1), 16)
    missing = [
        f"{segment}{suffix}"
        for segment in SEGMENT_NAMES
        for suffix in ("Start", "End", "RomStart", "RomEnd")
        if f"{segment}{suffix}" not in values
    ]
    if missing:
        raise AuditError(f"map lacks executable segment symbols: {missing}")
    segments = [
        Segment(
            name=segment,
            virt_start=values[f"{segment}Start"],
            virt_end=values[f"{segment}End"],
            rom_start=values[f"{segment}RomStart"],
            rom_end=values[f"{segment}RomEnd"],
        )
        for segment in SEGMENT_NAMES
    ]
    text_record = re.compile(
        r"^\s+\.text\s+0x([0-9A-Fa-f]+)\s+0x([0-9A-Fa-f]+)\s+(.+?)\s*$"
    )
    all_records: list[dict[str, Any]] = []
    ranges: list[CodeRange] = []
    for line_number, line in enumerate(text.splitlines(), 1):
        match = text_record.match(line)
        if not match:
            continue
        start = int(match.group(1), 16)
        size = int(match.group(2), 16)
        object_name = match.group(3).strip()
        containing = [segment for segment in segments if segment.virt_start <= start < segment.virt_end]
        record = {
            "line": line_number,
            "start": start,
            "size": size,
            "end": start + size,
            "object": object_name,
            "segment": containing[0].name if containing else None,
            "excluded": object_name in EXCLUDED_TEXT_OBJECTS,
        }
        all_records.append(record)
        if size == 0 or not containing or object_name in EXCLUDED_TEXT_OBJECTS:
            continue
        segment = containing[0]
        if start + size > segment.virt_end:
            raise AuditError(f".text input crosses segment boundary: {record}")
        if size % 4:
            raise AuditError(f".text input is not instruction-word aligned: {record}")
        rom_start = segment.rom_start + (start - segment.virt_start)
        ranges.append(CodeRange(segment.name, start, start + size, rom_start, rom_start + size, object_name))
    ranges.sort(key=lambda item: (item.start, item.end, item.object_name))
    for previous, current in zip(ranges, ranges[1:]):
        if current.start < previous.end:
            raise AuditError(f"overlapping executable .text inputs: {previous} and {current}")
    if not ranges:
        raise AuditError("map produced no executable .text ranges")
    return segments, ranges, all_records


def parse_nm_symbols(nm_path: Path, elf_path: Path, segments: list[Segment]) -> list[Symbol]:
    output = run_capture([str(nm_path), "-n", "-S", "--defined-only", str(elf_path)]).stdout
    bounds = [(segment.virt_start, segment.virt_end) for segment in segments]
    symbols: list[Symbol] = []
    for line in output.splitlines():
        fields = line.split(maxsplit=3)
        if len(fields) != 4:
            continue
        try:
            start = int(fields[0], 16)
            size = int(fields[1], 16)
        except ValueError:
            continue
        kind = fields[2]
        if kind not in {"T", "t", "W", "w"} or not any(lo <= start < hi for lo, hi in bounds):
            continue
        symbols.append(Symbol(start, size, kind, fields[3]))
    symbols.sort(key=lambda item: (item.start, item.size, item.name))
    return symbols


def parse_elf_sections(elf_path: Path) -> dict[str, dict[str, int]]:
    data = elf_path.read_bytes()
    if data[:4] != b"\x7fELF" or data[4] != 1 or data[5] != 2:
        raise AuditError("ELF must be a big-endian ELF32 file")
    header = struct.unpack_from(">16sHHIIIIIHHHHHH", data, 0)
    section_offset, section_entry_size, section_count, string_index = header[6], header[11], header[12], header[13]
    if section_entry_size < 40 or section_offset + section_entry_size * section_count > len(data):
        raise AuditError("invalid ELF section table")
    raw_sections: list[tuple[int, int, int, int, int, int]] = []
    for index in range(section_count):
        values = struct.unpack_from(">IIIIIIIIII", data, section_offset + index * section_entry_size)
        raw_sections.append((values[0], values[1], values[2], values[4], values[5], values[9]))
    str_off, str_size = raw_sections[string_index][3], raw_sections[string_index][4]
    names = data[str_off : str_off + str_size]
    result: dict[str, dict[str, int]] = {}
    for name_offset, section_type, flags, section_file_offset, section_size, align in raw_sections:
        if name_offset >= len(names):
            continue
        end = names.find(b"\x00", name_offset)
        name = names[name_offset:end if end >= 0 else len(names)].decode("utf-8", "replace")
        result[name] = {"type": section_type, "flags": flags, "offset": section_file_offset, "size": section_size, "align": align}
    return result


def classify_mips(word: int) -> str | None:
    major, funct = (word >> 26) & 0x3F, word & 0x3F
    if major == 0x11 and funct == 0x02:
        return {16: "MUL.S", 17: "MUL.D"}.get((word >> 21) & 0x1F)
    if major == 0 and funct in {24, 25, 28, 29}:
        return {24: "MULT", 25: "MULTU", 28: "DMULT", 29: "DMULTU"}[funct]
    return None


def signed(value: int, bits: int) -> int:
    return value - (1 << bits) if value & (1 << (bits - 1)) else value


def reg_name(reg: int) -> str:
    return ("zero", "at", "v0", "v1", "a0", "a1", "a2", "a3", "t0", "t1", "t2", "t3", "t4", "t5", "t6", "t7", "s0", "s1", "s2", "s3", "s4", "s5", "s6", "s7", "t8", "t9", "k0", "k1", "gp", "sp", "fp", "ra")[reg]


def decode_instruction(pc: int, word: int, delay_slot: bool) -> Insn:
    major, rs, rt, rd, funct = (word >> 26) & 0x3F, (word >> 21) & 31, (word >> 16) & 31, (word >> 11) & 31, word & 0x3F
    immediate = signed(word & 0xFFFF, 16)
    multiply = classify_mips(word)
    name, args, control, branch_kind, target, conditional, branch_likely, link, link_reg = "UNKNOWN", "", False, None, None, False, False, False, 0
    if major == 0:
        names = {0: "SLL", 2: "SRL", 3: "SRA", 4: "SLLV", 6: "SRLV", 7: "SRAV", 8: "JR", 9: "JALR", 12: "SYSCALL", 13: "BREAK", 16: "MFHI", 17: "MTHI", 18: "MFLO", 19: "MTLO", 24: "MULT", 25: "MULTU", 26: "DIV", 27: "DIVU", 28: "DMULT", 29: "DMULTU", 30: "DDIV", 31: "DDIVU", 32: "ADD", 33: "ADDU", 34: "SUB", 35: "SUBU", 36: "AND", 37: "OR", 38: "XOR", 39: "NOR", 42: "SLT", 43: "SLTU"}
        name = multiply or names.get(funct, "SPECIAL")
        if funct in {8, 9}:
            control, branch_kind = True, ("return" if rs == 31 and funct == 8 else "indirect_jump_or_call")
            link, link_reg = funct == 9 and rd != 0, (rd if funct == 9 and rd != 0 else 0)
            args = reg_name(rs) if funct == 8 else f"{reg_name(rd)},{reg_name(rs)}"
        elif funct in {12, 13}:
            control, branch_kind = True, name.lower()
        elif multiply:
            args = f"{reg_name(rs)},{reg_name(rt)}"
    elif major in {2, 3}:
        name, control, branch_kind = ("J", True, "direct_jump") if major == 2 else ("JAL", True, "direct_call")
        link, link_reg, target = major == 3, (31 if major == 3 else 0), ((pc + 4) & 0xF0000000) | ((word & 0x03FFFFFF) << 2)
        args = hex_word(target)
    elif major in {4, 5, 6, 7, 0x14, 0x15, 0x16, 0x17}:
        names = {
            4: "BEQ", 5: "BNE", 6: "BLEZ", 7: "BGTZ",
            0x14: "BEQL", 0x15: "BNEL", 0x16: "BLEZL", 0x17: "BGTZL",
        }
        name = names[major]
        control, conditional, branch_likely, branch_kind = True, True, major >= 0x14, "conditional_branch"
        target = pc + 4 + immediate * 4
        args = f"{reg_name(rs)},{reg_name(rt)},{hex_word(target)}" if major in {4, 5, 0x14, 0x15} else f"{reg_name(rs)},{hex_word(target)}"
    elif major == 1:
        names = {
            0: "BLTZ", 1: "BGEZ", 2: "BLTZL", 3: "BGEZL",
            16: "BLTZAL", 17: "BGEZAL", 18: "BLTZALL", 19: "BGEZALL",
        }
        name = names.get(rt, "REGIMM")
        if rt in names:
            control, conditional, branch_likely = True, True, rt in {2, 3, 18, 19}
            branch_kind = "conditional_call" if rt in {16, 17, 18, 19} else "conditional_branch"
            link, link_reg, target = rt in {16, 17, 18, 19}, (31 if rt in {16, 17, 18, 19} else 0), pc + 4 + immediate * 4
            args = f"{reg_name(rs)},{hex_word(target)}"
        else:
            args = f"{reg_name(rs)},{immediate}"
    elif major in {0x10, 0x11, 0x12} and rs == 8:
        cop = {0x10: "BC0", 0x11: "BC1", 0x12: "BC2"}[major]
        suffix = {0: "F", 1: "T", 2: "FL", 3: "TL"}.get(rt, "?")
        name = cop + suffix
        if rt in {0, 1, 2, 3}:
            control, conditional, branch_likely, branch_kind = True, True, rt in {2, 3}, "conditional_branch"
            target = pc + 4 + immediate * 4
        args = hex_word(target) if target is not None else hex_word(pc + 4 + immediate * 4)
    elif major == 0x0F:
        name, args = "LUI", f"{reg_name(rt)},{word & 0xFFFF:#x}"
    else:
        names = {0x08: "ADDI", 0x09: "ADDIU", 0x0A: "SLTI", 0x0B: "SLTIU", 0x0C: "ANDI", 0x0D: "ORI", 0x0E: "XORI", 0x18: "DADDI", 0x19: "DADDIU", 0x20: "LB", 0x21: "LH", 0x23: "LW", 0x24: "LBU", 0x25: "LHU", 0x27: "LWU", 0x28: "SB", 0x29: "SH", 0x2B: "SW", 0x31: "LWC1", 0x35: "LDC1", 0x39: "SWC1", 0x3D: "SDC1"}
        name, args = names.get(major, "OP"), f"{reg_name(rt)},{immediate}({reg_name(rs)})"
    return Insn(pc, word, name, args, multiply, control, delay_slot, branch_kind, target, conditional, branch_likely, link, link_reg, rs, rt, rd)


def source_guess(source_dir: Path, object_name: str) -> str | None:
    relative = object_name[len("build/jp/"):] if object_name.startswith("build/jp/") else object_name
    if ":" in relative:
        archive, relative = relative.split(":", 1)
        prefixes = ("lib/src", "lib/asm") if archive == "libultra.a" else ("src", "lib/src", "lib/asm")
        base = Path(relative).with_suffix("").name
        candidates = [source_dir / prefix / (base + ext) for prefix in prefixes for ext in (".c", ".s", ".S")]
    else:
        stem = Path(relative).with_suffix("")
        candidates = [source_dir / (str(stem) + ext) for ext in (".c", ".s", ".S")]
    for candidate in candidates:
        if candidate.is_file():
            try:
                return str(candidate.relative_to(source_dir.parent))
            except ValueError:
                return str(candidate)
    return None


def symbol_for_pc(symbols: list[Symbol], pc: int) -> Symbol | None:
    best: Symbol | None = None
    for symbol in symbols:
        if symbol.start > pc:
            break
        if symbol.start <= pc and (symbol.size == 0 or pc < symbol.start + symbol.size):
            best = symbol
    if best is not None:
        return best
    for symbol in reversed(symbols):
        if symbol.start <= pc:
            return symbol
    return None


def addr2line_sources(addr2line: Path, elf_path: Path, pcs: Iterable[int]) -> dict[int, dict[str, str | None]]:
    ordered = sorted(set(pcs))
    if not ordered:
        return {}
    completed = run_capture([str(addr2line), "-f", "-C", "-e", str(elf_path)] + [hex_word(pc) for pc in ordered])
    lines = completed.stdout.splitlines()
    result: dict[int, dict[str, str | None]] = {}
    for index, pc in enumerate(ordered):
        function = lines[index * 2] if index * 2 < len(lines) else "??"
        location = lines[index * 2 + 1] if index * 2 + 1 < len(lines) else "??:?"
        result[pc] = {"addr2line_function": None if function == "??" else function, "addr2line_location": None if location in {"??:0", "??:?"} else location}
    return result


def format_symbol_source(pc: int, symbols: list[Symbol], ranges_by_pc: dict[int, CodeRange], source_dir: Path, addr_lines: dict[int, dict[str, str | None]]) -> dict[str, Any]:
    symbol = symbol_for_pc(symbols, pc)
    code_range = ranges_by_pc[pc]
    result: dict[str, Any] = {"pc": hex_word(pc), "symbol": symbol.name if symbol else None, "symbol_start": hex_word(symbol.start) if symbol else None, "source_file": source_guess(source_dir, code_range.object_name), "source_line": None, "object": code_range.object_name}
    result.update(addr_lines.get(pc, {}))
    return result


def build_edges(instructions: dict[int, Insn], multiply_target_pcs: list[int]) -> tuple[list[Edge], int, dict[str, int]]:
    """Build only source edges relevant to a possible first FP multiply.

    The complete indirect-target relation is a Cartesian product (many
    indirect delay slots by every code address). Materializing that relation
    is unnecessary for the erratum: only a first MUL.S/MUL.D source and a
    multiply destination can form a pair. Counts for the omitted universe
    are returned separately, while every retained edge has exact rationale.
    """
    return_continuations = {
        instruction.pc + 8
        for instruction in instructions.values()
        if instruction.link and instruction.pc + 8 in instructions
    }
    delay_sources: dict[int, list[Insn]] = collections.defaultdict(list)
    for instruction in instructions.values():
        if instruction.control and instruction.name not in TERMINAL_NAMES and instruction.pc + 4 in instructions:
            delay_sources[instruction.pc + 4].append(instruction)
    edge_reasons: dict[tuple[int, int], set[str]] = collections.defaultdict(set)
    edge_metadata: dict[tuple[int, int], set[tuple[str, str]]] = collections.defaultdict(set)
    potential_indirect_delay_count = 0
    potential_indirect_edge_count = 0

    def add(src: int, dst: int | None, reason: str, **metadata: Any) -> None:
        if dst is None or dst not in instructions:
            return
        edge_reasons[(src, dst)].add(reason)
        for name, value in metadata.items():
            edge_metadata[(src, dst)].add((name, str(value)))

    for instruction in instructions.values():
        pc = instruction.pc
        relevant_source = instruction.multiply in FP_MULTIPLY_NAMES
        if pc in delay_sources:
            for predecessor in delay_sources[pc]:
                if predecessor.conditional:
                    if relevant_source:
                        add(
                            pc,
                            predecessor.target,
                            "delay_to_branch_target",
                            control_pc=hex_word(predecessor.pc),
                            control=predecessor.name,
                        )
                        if not predecessor.branch_likely:
                            add(
                                pc,
                                predecessor.pc + 8,
                                "delay_to_branch_fallthrough",
                                control_pc=hex_word(predecessor.pc),
                                control=predecessor.name,
                            )
                elif predecessor.branch_kind in {"direct_jump", "direct_call"}:
                    if relevant_source:
                        add(
                            pc,
                            predecessor.target,
                            "delay_to_direct_target",
                            control_pc=hex_word(predecessor.pc),
                            control=predecessor.name,
                        )
                elif predecessor.name in {"JR", "JALR"}:
                    potential_indirect_delay_count += 1
                    potential_indirect_edge_count += len(multiply_target_pcs)
                    if relevant_source:
                        for target in multiply_target_pcs:
                            add(
                                pc,
                                target,
                                "delay_to_indirect_multiply_target",
                                control_pc=hex_word(predecessor.pc),
                                control=predecessor.name,
                            )
                    known_return = (
                        predecessor.name == "JR" and predecessor.rs == 31
                    ) or (
                        predecessor.name == "JALR"
                        and predecessor.rd == 0
                        and predecessor.rs == 31
                    )
                    if relevant_source and known_return:
                        for continuation in sorted(return_continuations):
                            add(
                                pc,
                                continuation,
                                "delay_to_return_continuation",
                                control_pc=hex_word(predecessor.pc),
                                control=predecessor.name,
                            )
                else:
                    potential_indirect_delay_count += 1
                    potential_indirect_edge_count += len(multiply_target_pcs)
                    if relevant_source:
                        for target in multiply_target_pcs:
                            add(
                                pc,
                                target,
                                "delay_to_indirect_multiply_target",
                                control_pc=hex_word(predecessor.pc),
                                control=predecessor.name,
                            )
            continue
        if not relevant_source:
            continue
        # A multiply is not itself a control-transfer instruction, so its
        # ordinary successor is the next word.  This edge also captures the
        # not-taken path when the multiply is a conditional branch delay slot.
        add(pc, pc + 4, "linear_fallthrough")

    edges = [
        Edge(src, dst, tuple(sorted(edge_reasons[(src, dst)])), tuple(sorted(edge_metadata[(src, dst)])))
        for src, dst in sorted(edge_reasons)
    ]
    stats = {
        "indirect_delay_slot_count": potential_indirect_delay_count,
        "indirect_multiply_destination_universe_count": len(multiply_target_pcs),
        "omitted_indirect_edge_count": potential_indirect_edge_count,
    }
    return edges, len(return_continuations), stats
def summarize_edge_policy(instructions: dict[int, Insn], multiply_target_pcs: list[int]) -> dict[str, int | bool]:
    """Count every finite CFG edge event without expanding unknown targets."""
    return_continuations = {
        instruction.pc + 8
        for instruction in instructions.values()
        if instruction.link and instruction.pc + 8 in instructions
    }
    delay_sources: dict[int, list[Insn]] = collections.defaultdict(list)
    for instruction in instructions.values():
        if instruction.control and instruction.name not in TERMINAL_NAMES and instruction.pc + 4 in instructions:
            delay_sources[instruction.pc + 4].append(instruction)
    counts: collections.Counter[str] = collections.Counter()
    for instruction in instructions.values():
        pc = instruction.pc
        if instruction.control and instruction.name not in TERMINAL_NAMES and pc + 4 in instructions:
            counts["control_to_delay_slot"] += 1
        if pc in delay_sources:
            for predecessor in delay_sources[pc]:
                if predecessor.conditional:
                    if predecessor.target in instructions:
                        counts["delay_to_branch_target"] += 1
                    if not predecessor.branch_likely and predecessor.pc + 8 in instructions:
                        counts["delay_to_branch_fallthrough"] += 1
                elif predecessor.branch_kind in {"direct_jump", "direct_call"}:
                    if predecessor.target in instructions:
                        counts["delay_to_direct_target"] += 1
                elif predecessor.name in {"JR", "JALR"}:
                    counts["delay_to_indirect_multiply_target_potential"] += len(multiply_target_pcs)
                    known_return = (
                        predecessor.name == "JR" and predecessor.rs == 31
                    ) or (
                        predecessor.name == "JALR"
                        and predecessor.rd == 0
                        and predecessor.rs == 31
                    )
                    if known_return:
                        counts["delay_to_return_continuation"] += len(return_continuations)
                else:
                    counts["delay_to_indirect_multiply_target_potential"] += len(multiply_target_pcs)
            continue
        if instruction.name in TERMINAL_NAMES or instruction.control:
            continue
        if pc + 4 in instructions:
            counts["linear_fallthrough"] += 1
    result: dict[str, int | bool] = {name: counts[name] for name in sorted(counts)}
    event_count = sum(counts.values())
    independently_summed_event_count = sum(
        result.get(name, 0) for name in (
            "control_to_delay_slot",
            "delay_to_branch_target",
            "delay_to_branch_fallthrough",
            "delay_to_direct_target",
            "delay_to_return_continuation",
            "delay_to_indirect_multiply_target_potential",
            "linear_fallthrough",
        )
    )
    result["edge_event_count"] = event_count
    result["edge_count_reconciles"] = event_count == independently_summed_event_count
    return result



def edge_to_json(edge: Edge) -> dict[str, Any]:
    metadata: dict[str, list[str]] = collections.defaultdict(list)
    for name, value in edge.metadata:
        metadata[name].append(value)
    return {"from_pc": hex_word(edge.src), "to_pc": hex_word(edge.dst), "reasons": list(edge.reasons), "metadata": {name: values for name, values in sorted(metadata.items())}}


def pair_source_status(first: Insn) -> dict[str, Any]:
    return {"status": "unresolved_runtime_values", "rank": 1, "rank_basis": "no execution trace or proven source-value provenance for this first multiply", "source_registers": {"fs": (first.word >> 11) & 31, "ft": (first.word >> 16) & 31}, "source_class_evidence": []}


def expected_rom_md5(provenance: dict[str, Any]) -> str | None:
    for source in provenance.get("technical_sources", []):
        if source.get("name") == "Byte-identical Super Mario 64 decompilation source":
            match = re.search(r"MD5\s+([0-9a-fA-F]{32})", str(source.get("rom_match", "")))
            if match:
                return match.group(1).lower()
    return None


def recorded_source_revision(provenance: dict[str, Any]) -> str | None:
    for source in provenance.get("technical_sources", []):
        if source.get("name") == "Byte-identical Super Mario 64 decompilation source":
            revision = source.get("revision")
            if isinstance(revision, str):
                return revision
    return None


def git_revision(source_dir: Path) -> str | None:
    try:
        return run_capture(["git", "-C", str(source_dir), "rev-parse", "HEAD"]).stdout.strip() or None
    except AuditError:
        return None


def build_audit(args: argparse.Namespace) -> dict[str, Any]:
    root = args.root.resolve()
    rom_path, elf_path, map_path, source_dir, provenance_path, toolchain = [path.resolve() for path in (args.rom, args.elf, args.map, args.source_dir, args.provenance, args.toolchain)]
    objdump = args.objdump.resolve() if args.objdump else toolchain / "mips64-elf-objdump"
    nm = args.nm.resolve() if args.nm else toolchain / "mips64-elf-nm"
    addr2line = args.addr2line.resolve() if args.addr2line else toolchain / "mips64-elf-addr2line"
    for path in (rom_path, elf_path, map_path, source_dir, provenance_path, objdump, nm, addr2line):
        if not path.exists():
            raise AuditError(f"missing audit input: {path}")
    segments, code_ranges, map_records = parse_map(map_path)
    excluded_text_provenance: list[dict[str, Any]] = []
    for record in map_records:
        if not record["excluded"]:
            continue
        source_path = source_guess(source_dir, record["object"])
        excluded_text_provenance.append({
            "object": record["object"],
            "map_line": record["line"],
            "virtual_start": hex_word(record["start"]),
            "size_bytes": record["size"],
            "reason": EXCLUDED_TEXT_OBJECT_REASONS[record["object"]],
            "source_path": source_path,
            "source_sha256": sha256_file(source_dir.parent / source_path) if source_path and (source_dir.parent / source_path).is_file() else None,
        })
    rom, elf = rom_path.read_bytes(), elf_path.read_bytes()
    words_by_pc: dict[int, int] = {}
    range_by_pc: dict[int, CodeRange] = {}
    for code_range in code_ranges:
        if code_range.rom_end > len(rom):
            raise AuditError(f"ROM does not contain {code_range}")
        raw = rom[code_range.rom_start : code_range.rom_end]
        for offset in range(0, len(raw), 4):
            pc = code_range.start + offset
            words_by_pc[pc] = struct.unpack_from(">I", raw, offset)[0]
            range_by_pc[pc] = code_range
    elf_sections = parse_elf_sections(elf_path)
    elf_executable_sections = sorted(
        name for name, section in elf_sections.items() if section["flags"] & 0x4
    )
    expected_executable_sections = sorted("." + name for name in SEGMENT_NAMES)
    if elf_executable_sections != expected_executable_sections:
        raise AuditError(
            "ELF executable sections differ from audited map segments: "
            f"{elf_executable_sections!r} != {expected_executable_sections!r}"
        )
    elf_segment_checks: list[dict[str, Any]] = []
    for segment in segments:
        section = elf_sections.get("." + segment.name)
        if section is None:
            raise AuditError(f"ELF lacks executable section .{segment.name}")
        elf_bytes = elf[section["offset"] : section["offset"] + section["size"]]
        rom_bytes = rom[segment.rom_start : segment.rom_end]
        if len(elf_bytes) != len(rom_bytes) or elf_bytes != rom_bytes:
            raise AuditError(f"ROM and ELF bytes differ for executable segment {segment.name}")
        elf_segment_checks.append({"name": segment.name, "rom_start": hex_word(segment.rom_start), "rom_end": hex_word(segment.rom_end), "size": segment.size, "elf_file_offset": hex_word(section["offset"]), "elf_section_size": section["size"], "rom_sha256": hashlib.sha256(rom_bytes).hexdigest(), "elf_sha256": hashlib.sha256(elf_bytes).hexdigest(), "byte_identical": True})

    matched_text_word_count = len(words_by_pc)
    initial_instructions = {
        pc: decode_instruction(pc, word, False)
        for pc, word in sorted(words_by_pc.items())
    }
    boundary_delay_records: list[dict[str, Any]] = []
    boundary_delay_pcs: set[int] = set()
    for control in initial_instructions.values():
        if (
            not control.control
            or control.name in TERMINAL_NAMES
            or control.pc + 4 in initial_instructions
        ):
            continue
        delay_pc = control.pc + 4
        segment = next(
            (
                item
                for item in segments
                if item.virt_start <= delay_pc < item.virt_end
            ),
            None,
        )
        if segment is None:
            raise AuditError(
                f"control-transfer delay slot leaves executable segments: "
                f"{hex_word(control.pc)} -> {hex_word(delay_pc)}"
            )
        rom_offset = segment.rom_start + (delay_pc - segment.virt_start)
        if rom_offset + 4 > len(rom):
            raise AuditError(
                f"ROM lacks boundary delay word at {hex_word(delay_pc)}"
            )
        raw_word = struct.unpack_from(">I", rom, rom_offset)[0]
        map_record = next(
            (
                record
                for record in map_records
                if record["start"] <= delay_pc < record["end"]
            ),
            None,
        )
        if map_record is not None:
            raise AuditError(
                "boundary control is followed by mapped text outside decoded "
                f"ranges: {hex_word(control.pc)} -> {hex_word(delay_pc)}"
            )
        delay_instruction = decode_instruction(delay_pc, raw_word, True)
        boundary_delay_pcs.add(delay_pc)
        words_by_pc[delay_pc] = raw_word
        boundary_delay_records.append(
            {
                "control_pc": hex_word(control.pc),
                "control_opcode": hex_word(control.word),
                "control_name": control.name,
                "control_args": control.args,
                "control_object": next(
                    code_range.object_name
                    for code_range in code_ranges
                    if code_range.start <= control.pc < code_range.end
                ),
                "control_range_start": hex_word(
                    next(
                        code_range.start
                        for code_range in code_ranges
                        if code_range.start <= control.pc < code_range.end
                    )
                ),
                "control_range_end_exclusive": hex_word(
                    next(
                        code_range.end
                        for code_range in code_ranges
                        if code_range.start <= control.pc < code_range.end
                    )
                ),
                "delay_pc": hex_word(delay_pc),
                "delay_rom_offset": hex_word(rom_offset),
                "delay_raw_word": hex_word(raw_word),
                "delay_decoded_name": delay_instruction.name,
                "delay_decoded_multiply": delay_instruction.multiply,
                "delay_map_classification": "linker_alignment_gap",
                "delay_map_record": None,
                "delay_segment": segment.name,
                "execution": (
                    "Architecturally fetched as the control-transfer delay "
                    "slot; explicitly included as a boundary word despite "
                    "being outside a mapped .text input."
                ),
                "erratum_relevance": (
                    "The boundary word is 0x00000000 (SLL zero,zero,0), not "
                    "MUL.S/MUL.D or any multiply, so it cannot form a "
                    "documented first-multiply pair."
                ),
            }
        )
    boundary_delay_word_count = len(boundary_delay_records)
    symbols = parse_nm_symbols(nm, elf_path, segments)
    function_entries = sorted({symbol.start for symbol in symbols if symbol.start in words_by_pc})
    instructions = {pc: decode_instruction(pc, word, False) for pc, word in sorted(words_by_pc.items())}
    for instruction in instructions.values():
        instruction.delay_slot = instruction.pc - 4 in instructions and instructions[instruction.pc - 4].control and instructions[instruction.pc - 4].name not in TERMINAL_NAMES
    multiply_instructions = [instruction for instruction in instructions.values() if instruction.multiply]
    first_fp = [instruction for instruction in multiply_instructions if instruction.multiply in FP_MULTIPLY_NAMES]
    multiply_target_pcs = sorted(
        instruction.pc for instruction in multiply_instructions
    )
    edges, return_continuation_count, edge_stats = build_edges(instructions, multiply_target_pcs)
    edge_policy = summarize_edge_policy(instructions, multiply_target_pcs)
    linear_pairs: dict[tuple[int, int], dict[str, Any]] = {}
    for first in first_fp:
        second = instructions.get(first.pc + 4)
        if second is not None and second.multiply in MULTIPLY_NAMES:
            linear_pairs[(first.pc, second.pc)] = {
                "first": first,
                "second": second,
                "edge_reasons": ["physical_next_word"],
                "edge_metadata": [],
            }
    feasible_pairs: dict[tuple[int, int], dict[str, Any]] = {}
    for edge in edges:
        first, second = instructions.get(edge.src), instructions.get(edge.dst)
        if first is not None and second is not None and first.multiply in FP_MULTIPLY_NAMES and second.multiply in MULTIPLY_NAMES:
            feasible_pairs[(first.pc, second.pc)] = {
                "first": first,
                "second": second,
                "edge_reasons": list(edge.reasons),
                "edge_metadata": list(edge.metadata),
            }
    pair_keys = sorted(set(linear_pairs) | set(feasible_pairs))
    addr_lines = addr2line_sources(addr2line, elf_path, [pc for key in pair_keys for pc in key])
    pair_list: list[dict[str, Any]] = []
    for rank, key in enumerate(pair_keys, 1):
        first_pc, second_pc = key
        first, second = instructions[first_pc], instructions[second_pc]
        linear, feasible = key in linear_pairs, key in feasible_pairs
        pair_info = feasible_pairs.get(key, linear_pairs.get(key))
        pair_list.append({
            "pair_id": rank,
            "first": {"pc": hex_word(first.pc), "opcode": hex_word(first.word), "name": first.multiply, "operands": {"fs": (first.word >> 11) & 31, "ft": (first.word >> 16) & 31, "fd": (first.word >> 6) & 31}, "symbol_source": format_symbol_source(first_pc, symbols, range_by_pc, source_dir, addr_lines)},
            "second": {"pc": hex_word(second.pc), "opcode": hex_word(second.word), "name": second.multiply, "operands": {"rs": (second.word >> 21) & 31, "rt": (second.word >> 16) & 31}, "symbol_source": format_symbol_source(second_pc, symbols, range_by_pc, source_dir, addr_lines)},
            "adjacency": {"linear_word_adjacency": linear, "feasible_control_flow_adjacency": feasible, "classification": "linear_and_feasible" if linear and feasible else ("linear_only" if linear else "feasible_non_linear"), "successor_rationale": {"reasons": pair_info["edge_reasons"], "metadata": {name: value for name, value in pair_info["edge_metadata"]}}},
            "runtime_operand_status": pair_source_status(first),
        })

    provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    expected_md5 = expected_rom_md5(provenance)
    recorded_revision = recorded_source_revision(provenance)
    actual_revision = git_revision(source_dir)
    objdump_args = [str(objdump), "-d", "-M", "no-aliases", "-j", ".boot", "-j", ".main", "-j", ".engine", "-j", ".goddard", str(elf_path)]
    objdump_output = run_capture(objdump_args).stdout
    edge_reason_counts = collections.Counter(reason for edge in edges for reason in edge.reasons)
    segment_summary: list[dict[str, Any]] = []
    for segment in segments:
        segment_ranges = [item for item in code_ranges if item.segment == segment.name]
        map_text_records = [item for item in map_records if item["segment"] == segment.name]
        segment_summary.append({"name": segment.name, "virtual_start": hex_word(segment.virt_start), "virtual_end": hex_word(segment.virt_end), "rom_start": hex_word(segment.rom_start), "rom_end": hex_word(segment.rom_end), "size_bytes": segment.size, "segment_word_count": segment.size // 4, "map_text_record_count": len(map_text_records), "included_code_range_count": len(segment_ranges), "included_executable_word_count": sum(item.word_count for item in segment_ranges), "excluded_text_record_count": sum(1 for item in map_text_records if item["excluded"]), "excluded_text_bytes": sum(item["size"] for item in map_text_records if item["excluded"]), "non_text_or_gap_bytes": segment.size - sum(item["size"] for item in map_text_records), "code_ranges": [{"virtual_start": hex_word(item.start), "virtual_end": hex_word(item.end), "rom_start": hex_word(item.rom_start), "rom_end": hex_word(item.rom_end), "size_bytes": item.size, "word_count": item.word_count, "object": item.object_name} for item in segment_ranges]})
    multiply_counts = collections.Counter(instruction.multiply for instruction in multiply_instructions if instruction.multiply is not None)
    branch_counts = collections.Counter(instruction.name for instruction in instructions.values() if instruction.control)
    linear_word_adjacencies = sum(1 for pc in words_by_pc if pc + 4 in words_by_pc)
    linear_pair_count, feasible_pair_count, both_count = len(linear_pairs), len(feasible_pairs), len(set(linear_pairs) & set(feasible_pairs))
    fp_multiply_in_delay_slot_count = sum(
        1 for instruction in first_fp if instruction.delay_slot
    )
    tool_records = {"objdump": tool_record(objdump), "nm": tool_record(nm), "addr2line": tool_record(addr2line)}
    build_rom = root / "sm64/build/jp/sm64.jp.z64"
    build_rom_record = {"path": str(build_rom), "sha256": sha256_file(build_rom), "byte_identical": build_rom.read_bytes() == rom} if build_rom.is_file() else None
    actual_md5 = md5_file(rom_path)
    source_revision_match = actual_revision == recorded_revision if actual_revision and recorded_revision else None
    repository_script_path = (
        args.repository_script.resolve()
        if args.repository_script
        else root / "scripts/audit_rom_mul_hazards.py"
    )
    repository_script_label = args.repository_script_label
    repository_provenance_path = (
        args.provenance_repository.resolve()
        if args.provenance_repository
        else root / "results/sources.json"
    )
    repository_provenance_label = args.provenance_repository_label
    executed_script_path = Path(__file__).resolve()
    repository_script_sha256 = (
        sha256_file(repository_script_path)
        if repository_script_path.is_file()
        else None
    )
    repository_provenance_sha256 = (
        sha256_file(repository_provenance_path)
        if repository_provenance_path.is_file()
        else None
    )
    staged_provenance_sha256 = sha256_file(provenance_path)
    return {
        "analyzer": {
            "name": "scripts/audit_rom_mul_hazards.py",
            "version": ANALYZER_VERSION,
            "repository_script_label": repository_script_label,
            "repository_script_path": repository_script_label,
            "repository_script_staging_path": str(repository_script_path),
            "repository_script_sha256": repository_script_sha256,
            "executed_script_path": str(executed_script_path),
            "executed_script_sha256": sha256_file(executed_script_path),
            "script_sha256": sha256_file(executed_script_path),
            "python_version": platform.python_version(),
            "python_implementation": platform.python_implementation(),
            "staging_commands": [],
            "command": shlex.join([str(item) for item in sys.argv]),
        },
        "inputs": {
            "root": str(root),
            "rom": {"path": str(rom_path), "size_bytes": len(rom), "md5": actual_md5, "sha256": sha256_file(rom_path)},
            "elf": {"path": str(elf_path), "size_bytes": len(elf), "sha256": sha256_file(elf_path)},
            "map": {"path": str(map_path), "sha256": sha256_file(map_path)},
            "linker_script": {"path": str(source_dir / "sm64.ld"), "sha256": sha256_file(source_dir / "sm64.ld")},
            "segments_header": {"path": str(source_dir / "include/segments.h"), "sha256": sha256_file(source_dir / "include/segments.h")},
            "provenance": {
                "repository_identity_label": repository_provenance_label,
                "repository_path": repository_provenance_label,
                "repository_staging_path": str(repository_provenance_path),
                "repository_sha256": repository_provenance_sha256,
                "staged_execution_path": str(provenance_path),
                "staged_execution_sha256": staged_provenance_sha256,
                "sha256": staged_provenance_sha256,
                "provenance_argument": f"--provenance {provenance_path}",
                "repository_provenance_argument": (
                    f"--provenance-repository {repository_provenance_path}"
                ),
                "same_sha256_as_repository": (
                    repository_provenance_sha256 is not None
                    and repository_provenance_sha256 == staged_provenance_sha256
                ),
            },
            "source_revision": {"recorded": recorded_revision, "actual": actual_revision, "match": source_revision_match},
            "recorded_rom_md5": expected_md5,
            "rom_md5_matches_recorded": expected_md5 == actual_md5 if expected_md5 else None,
            "build_rom": build_rom_record,
            "tools": tool_records,
        },
        "executable_scope": {
            "policy": (
                "Include every aligned input .text map range in "
                ".boot/.main/.engine/.goddard that contains matched R4300 CPU "
                "code; exclude exact non-CPU .text inputs (ROM header, IPL3 "
                "font, and RSP microcode), all .rodata/.data/.bss, and linker "
                "fill gaps except an explicitly recorded control-transfer "
                "boundary delay word."
            ),
            "elf_executable_sections": elf_executable_sections,
            "segments": segment_summary,
            "segment_byte_checks": elf_segment_checks,
            "excluded_text_objects": sorted(EXCLUDED_TEXT_OBJECTS),
            "excluded_text_object_reasons": EXCLUDED_TEXT_OBJECT_REASONS,
            "excluded_text_provenance": excluded_text_provenance,
            "map_text_record_count": len(map_records),
            "included_code_range_count": len(code_ranges),
            "matched_text_executable_word_count": matched_text_word_count,
            "boundary_delay_word_count": boundary_delay_word_count,
            "included_executable_word_count": len(words_by_pc),
            "boundary_delay_records": boundary_delay_records,
            "excluded_map_text_bytes": sum(
                item["size"] for item in map_records if item["excluded"]
            ),
            "non_text_or_gap_bytes": sum(item.size for item in segments)
            - sum(item["size"] for item in map_records),
            "false_positive_policy": [
                "ROM header and IPL3 font are map .text inputs but are excluded by exact object identity.",
                "lib/rsp.o is an assembler .text/.rodata input whose source emits RSP binary .incbin blobs; it is excluded by exact object identity.",
                "Jump tables and literal pools emitted as .rodata/.data are outside included .text ranges.",
                "Unmapped linker alignment fills are not decoded except for an explicitly recorded control-transfer boundary delay word; no other edge crosses a non-.text gap.",
            ],
        },
        "erratum": {"title": ERRATUM_TITLE, "revision": ERRATUM_REVISION, "url": ERRATUM_URL, "first_multiply": ["MUL.S", "MUL.D"], "second_multiply": sorted(MULTIPLY_NAMES), "source_quote": "The error happens only when the first multiply is single- or double-precision floating-point operation and when one or both of its source operands are: Signalling Not-a-Number (sNaN), 0 (Zero), or infinity (Inf).", "semantics": "Back-to-back means adjacent dynamic instructions, either physical source adjacency or a first multiply in a branch delay slot followed by the branch target; one intervening instruction prevents the documented problem.", "coverage_limit": "This audit addresses only the documented back-to-back multiply erratum, not global hardware fault exclusion."},
        "disassembly": {
            "decoder": "builtin big-endian MIPS32 decoder for the R4300i/MIPS III multiply and control-transfer subset; JALX is not an R4300 instruction and is not decoded",
            "objdump_command": shlex.join(objdump_args),
            "objdump_stdout_sha256": hashlib.sha256(objdump_output.encode("utf-8")).hexdigest(),
            "objdump_stdout_line_count": len(objdump_output.splitlines()),
            "matched_text_instruction_count": matched_text_word_count,
            "boundary_delay_word_count": boundary_delay_word_count,
            "instruction_count": len(instructions),
            "unknown_word_count": sum(
                1
                for instruction in instructions.values()
                if instruction.name in {"UNKNOWN", "OP", "SPECIAL", "REGIMM", "BC1?"}
            ),
            "multiply_counts": {
                name: multiply_counts.get(name, 0)
                for name in sorted(MULTIPLY_NAMES)
            },
            "all_multiply_count": len(multiply_instructions),
            "first_fp_multiply_count": len(first_fp),
            "fp_multiply_in_delay_slot_count": fp_multiply_in_delay_slot_count,
            "control_transfer_counts": {
                name: branch_counts[name] for name in sorted(branch_counts)
            },
        },
        "control_flow": {
            "policy": "R4300i/MIPS III: normal instructions fall through; every non-terminal control transfer executes its delay slot when the following word is included. Conditional branches retain target and fallthrough. Branch-likely instructions (BEQL/BNEL/BLEZL/BGTZL, BLTZL/BGEZL/BLTZALL/BGEZALL, BC0/BC1/BC2 FL/TL) execute the delay slot only when taken and annul it on not-taken paths, so a likely delay-slot first multiply has target-only successor. Direct jump/call targets are decoded. JR/JALR delay slots conservatively target every multiply destination, including JR ra; known return continuations are retained only as diagnostic edges, never as an exhaustive target limiter. JALX is not an R4300 instruction and is not decoded. The retained edge graph is source-relevant and exact for candidate pairs; all-edge event counts and indirect-universe counts are reported separately.",
            "function_entry_count": len(function_entries),
            "edge_count_unique_source_destination": len(edges),
            "edge_reason_counts": {name: edge_reason_counts[name] for name in sorted(edge_reason_counts)},
            "edge_records": [edge_to_json(edge) for edge in edges],
            "linear_word_adjacency_count": linear_word_adjacencies,
            "delay_slot_instruction_count": sum(1 for instruction in instructions.values() if instruction.delay_slot),
            "boundary_control_count": boundary_delay_word_count,
            "boundary_delay_word_count": boundary_delay_word_count,
            "boundary_delay_records": boundary_delay_records,
            "relevant_edge_graph": True,
            "all_edge_policy": edge_policy,
            "indirect_universe_stats": edge_stats,
            "indirect_omitted_relevance": {
                "fp_multiply_in_delay_slot_count": fp_multiply_in_delay_slot_count,
                "irrelevant_to_erratum_pairs": fp_multiply_in_delay_slot_count == 0,
                "reason": "No first MUL.S/MUL.D occurs in a delay slot, so omitted indirect-target Cartesian edges have no possible first-multiply source." if fp_multiply_in_delay_slot_count == 0 else "Indirect-target edges from FP delay slots are retained to multiply destinations.",
            },
            "relevant_edge_reason_event_count": sum(len(edge.reasons) for edge in edges),
            "relevant_edge_reason_histogram_reconciles": sum(edge_reason_counts.values()) == sum(len(edge.reasons) for edge in edges),
        },
        "pairs": {
            "definition": "A pair is retained if first is MUL.S/MUL.D and second is one of MUL.S/MUL.D/MULT/MULTU/DMULT/DMULTU; linear and conservative-CFG adjacency are reported independently.",
            "linear_pair_count": linear_pair_count,
            "feasible_pair_count": feasible_pair_count,
            "linear_and_feasible_count": both_count,
            "linear_only_count": linear_pair_count - both_count,
            "feasible_non_linear_count": feasible_pair_count - both_count,
            "union_candidate_count": len(pair_list),
            "pair_count_reconciles": len(pair_list)
            == linear_pair_count + feasible_pair_count - both_count,
            "pairs": pair_list,
            "runtime_operand_policy": "Static bytes do not prove runtime FPR classes. No register-name inference is made; every candidate first multiply is unresolved and ranked for dynamic tracing.",
        },
        "limitations": [
            "No runtime operand class is inferred from FPR/GPR names or static register flow.",
            "The conservative JR/JALR policy targets every multiply destination, including externally or manually set return addresses; known return continuations are diagnostics and never exhaustive targets.",
            "One linker-alignment boundary delay word is explicitly included because the JR ra at the end of bzero fetches it; it is a zero SLL/NOP and cannot form a multiply pair. Other linker gaps are not decoded.",
            "No faulty result emulation or hardware-fault claim is made.",
        ],
        "dynamic": {"status": "vacuous_no_static_sites" if not pair_list else "required_for_static_sites", "static_site_count": len(pair_list), "instrumentation": None if not pair_list else "not_run_by_this_static-only invocation", "routes": [], "findings": [], "blockers": [] if not pair_list else ["Dynamic PC-targeted tracing must be run for the retained candidate sites before bounded conclusion."], "fuzzy64_limit": "Fuzzy64 cannot model physical cache, bus, RDRAM, or silicon faults."},
        "conclusion": {"bounded": True, "text": "No ROM-wide static erratum pair exists in the matched R4300 CPU executable .text scope; dynamic tracing is vacuous." if not pair_list else "ROM-wide static susceptible pairs exist; this artifact does not by itself establish runtime operand classes or a hardware failure."},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit all matched JP ROM executable multiply hazards")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--rom", type=Path)
    parser.add_argument("--elf", type=Path)
    parser.add_argument("--map", dest="map", type=Path)
    parser.add_argument("--source-dir", type=Path)
    parser.add_argument("--provenance", type=Path)
    parser.add_argument("--provenance-repository", type=Path)
    parser.add_argument(
        "--provenance-repository-label",
        default="results/sources.json",
    )
    parser.add_argument("--repository-script", type=Path)
    parser.add_argument(
        "--repository-script-label",
        default="scripts/audit_rom_mul_hazards.py",
    )
    parser.add_argument("--toolchain", type=Path)
    parser.add_argument("--objdump", type=Path)
    parser.add_argument("--nm", type=Path)
    parser.add_argument("--addr2line", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = args.root
    args.rom = args.rom or root / "emulator/sm64.jp.z64"
    args.elf = args.elf or root / "sm64/build/jp/sm64.jp.elf"
    args.map = args.map or root / "sm64/build/jp/sm64.jp.map"
    args.source_dir = args.source_dir or root / "sm64"
    args.provenance = args.provenance or root / "results/sources.json"
    args.toolchain = args.toolchain or root / "emulator/toolchain"
    try:
        audit = build_audit(args)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(audit, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except (AuditError, OSError, UnicodeError, ValueError, struct.error, json.JSONDecodeError) as error:
        print(f"audit_rom_mul_hazards.py: {error}", file=sys.stderr)
        return 1
    print(f"segments={len(audit['executable_scope']['segments'])} instructions={audit['disassembly']['instruction_count']} muls={audit['disassembly']['all_multiply_count']} linear_pairs={audit['pairs']['linear_pair_count']} feasible_pairs={audit['pairs']['feasible_pair_count']} union_pairs={audit['pairs']['union_candidate_count']} dynamic={audit['dynamic']['status']} output={args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
