#!/usr/bin/env python3
"""Reconstruct the local research toolchain; never fetch a game ROM."""
from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCES = {
    "wafel": ("branpk/wafel", "5b808b60af15d316a5e2b0f87db34421d6225b57"),
    "TTC-Upwarp-Overlay": ("danebou/TTC-Upwarp-Overlay", "8cc951f7221c3a3078a9924b723581a5995cf49e"),
}
ROM_MD5 = "85d61f5525af708c9f1e84dce6dc10e9"


def run(*command: str) -> None:
    subprocess.run(command, cwd=ROOT, check=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rom", type=Path, help="your JP ROM; z64/v64/n64 byte order accepted")
    parser.add_argument("--skip-media", action="store_true", help="do not retrieve/regenerate the public video dataset")
    args = parser.parse_args()
    uv = shutil.which("uv")
    if uv is None:
        raise SystemExit("Install uv and put it on PATH before running setup.")
    for name, (repo, revision) in SOURCES.items():
        path = ROOT / name
        if not path.exists():
            run("git", "clone", "--filter=blob:none", "--no-checkout", f"https://github.com/{repo}.git", str(path))
            run("git", "-C", str(path), "checkout", "--detach", revision)
        actual = subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
        if actual != revision:
            raise SystemExit(f"{name} must be at {revision}; existing checkout was not reset.")
    python = ROOT / ".venv/bin/python"
    if not python.exists():
        run(uv, "venv", "--python", "3.12", str(ROOT / ".venv"))
    run(uv, "pip", "sync", "--python", str(python), str(ROOT / "requirements.txt"))
    (ROOT / "emulator").mkdir(exist_ok=True)
    (ROOT / ".tools").mkdir(exist_ok=True)
    rom = ROOT / "emulator/sm64.jp.z64"
    if args.rom is not None:
        descriptor, temporary = tempfile.mkstemp(prefix="rom-import-", suffix=".z64", dir=ROOT / ".tools")
        os.close(descriptor)
        temporary = Path(temporary)
        try:
            run(str(python), str(ROOT / "scripts/n64_to_z64.py"), str(args.rom.expanduser().resolve()), str(temporary))
            if hashlib.md5(temporary.read_bytes()).hexdigest() != ROM_MD5:
                raise SystemExit("Supplied ROM is not the original Japanese SM64 release; no ROM installed.")
            if rom.exists():
                if rom.read_bytes() != temporary.read_bytes():
                    raise SystemExit(f"Refusing to replace existing ROM: {rom}")
            else:
                temporary.rename(rom)
        finally:
            temporary.unlink(missing_ok=True)
    if rom.exists() and hashlib.md5(rom.read_bytes()).hexdigest() != ROM_MD5:
        raise SystemExit(f"Incorrect ROM at {rom}; expected MD5 {ROM_MD5}")
    if not (shutil.which("wine") or shutil.which("wine64")) or not shutil.which("x86_64-w64-mingw32-gcc"):
        run("bash", str(ROOT / "scripts/setup_windows_toolchain.sh"))
    run("bash", str(ROOT / "scripts/setup_mips_toolchain.sh"))
    run("bash", str(ROOT / "scripts/setup_sm64.sh"))
    run("bash", str(ROOT / "scripts/setup_fuzzy64.sh"))
    run("bash", str(ROOT / "scripts/setup_wafel.sh"), "--build-only")
    run(str(python), str(ROOT / "scripts/restore_research_inputs.py"))
    if not args.skip_media:
        run(str(python), str(ROOT / "scripts/restore_media.py"))
    if not rom.exists():
        print(f"\nTools and recoverable inputs are ready. Game execution remains blocked on your JP ROM.\n"
              f"Run: {sys.executable} scripts/setup_environment.py --rom /path/to/your/rom\n"
              f"Expected normalized MD5: {ROM_MD5}")
        return
    run("bash", str(ROOT / "scripts/setup_wafel.sh"), "--unlock-only", "--rom", str(rom))
    if not (ROOT / "emulator/route_vi475.m64p").exists():
        run(str(python), str(ROOT / "scripts/restore_research_inputs.py"), "--capture-route")
    (ROOT / "media").mkdir(exist_ok=True)
    run(str(python), str(ROOT / "scripts/run_reproduction.py"), "--output", str(ROOT / "media/reproduction.json"))
    print("\nLocal toolchain, inputs, exact JP DLL and ROM-backed emulator reproduction are ready.")


if __name__ == "__main__":
    main()
