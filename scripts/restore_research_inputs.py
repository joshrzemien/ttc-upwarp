#!/usr/bin/env python3
"""Restore the published base state and controller payload, then recapture VI-475 if a ROM is supplied."""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from prepare_emulator import convert_state

ROOT = Path(__file__).resolve().parents[1]
BASE_SHA256 = "2732c65808c54b74a85a97a18c50fa4c7fbb1afd85d60efcbd6d81ee796323a7"
SOURCE_SHA256 = "3d46c1b472becbf86f194d6ac9eeed9fa1b6089c6c08fd4419ddf9c12dc7325b"
SEQUENCES_SHA256 = "763ce21139ef066b8560f1584c64b4d06d09958614f9a2ee9ee4e6907c538da4"
ROM_MD5 = "85d61f5525af708c9f1e84dce6dc10e9"
ROUTE_SHA256 = "aa2233675b1636cca50e3a95558dba488ad827964ecfb35403a4ce7f04920bd7"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def require_hash(path: Path, expected: str) -> None:
    actual = sha256(path)
    if actual != expected:
        raise ValueError(f"Unexpected SHA256 for {path}: {actual}; expected {expected}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rom", type=Path, default=ROOT / "emulator/sm64.jp.z64")
    parser.add_argument("--capture-route", action="store_true", help="requires the supplied JP ROM and built emulator")
    args = parser.parse_args()
    destination = ROOT / "emulator"
    destination.mkdir(exist_ok=True)
    source = ROOT / "TTC-Upwarp-Overlay/Tas Attempts/tas26.st"
    base = destination / "tas26.jp.m64p.st"
    require_hash(source, SOURCE_SHA256)
    if not base.exists():
        convert_state(source, base)
    require_hash(base, BASE_SHA256)
    sequences = destination / "controller_equivalence_sequences.txt"
    if not sequences.exists():
        subprocess.run([sys.executable, str(ROOT / "scripts/build_controller_equivalence_sequences.py"),
                        str(sequences), "--manifest", str(destination / "controller_equivalence_sequences.json")], check=True)
    require_hash(sequences, SEQUENCES_SHA256)
    route = ROOT / "results/reachable_route_p078_r078_a+000.m64"
    require_hash(route, ROUTE_SHA256)
    print("Restored base state and 196,304 controller sequences match historical SHA256 values.")
    if not args.capture_route:
        print("VI-475 capture not requested; rerun with --capture-route after supplying the JP ROM.")
        return
    rom = args.rom.expanduser().resolve()
    if hashlib.md5(rom.read_bytes()).hexdigest() != ROM_MD5:
        raise ValueError("Route capture requires the canonical big-endian JP ROM")
    snapshot = destination / "route_vi475.m64p"
    if snapshot.exists():
        raise FileExistsError(f"Refusing to overwrite snapshot: {snapshot}")
    with tempfile.TemporaryDirectory(prefix=".route-capture-", dir=destination) as staging:
        pending = Path(staging) / snapshot.name
        env = os.environ | {"M64_PATH": str(route), "SAVE_PATH": str(pending), "SAVE_VI": "475",
                           "POST_SAVE_VIS": "300", "SDL_AUDIODRIVER": "dummy"}
        command = [str(destination / "bin/mupen64plus"), "--emumode", "0", "--nospeedlimit", "--nosaveoptions",
                   "--corelib", str(destination / "lib/libmupen64plus.so.2.0.0"),
                   "--configdir", str(destination / "config"), "--datadir", str(destination / "share/mupen64plus"),
                   "--rsp", str(destination / "lib/mupen64plus/mupen64plus-rsp-hle.so"),
                   "--gfx", "dummy", "--audio", "dummy", "--input", "dummy",
                   "--savestate", str(base), "--fuzzer-lua", str(ROOT / "scripts/save_replay_snapshot.lua"), str(rom)]
        run = subprocess.run(command, cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
        log = run.stdout + run.stderr
        (destination / "route_vi475.log").write_text(log)
        if run.returncode or "SNAPSHOT_DONE,vi=775" not in log:
            raise RuntimeError(f"Route capture failed; inspect {destination / 'route_vi475.log'}")
        raw = gzip.decompress(pending.read_bytes())
        if not raw.startswith(b"M64+SAVE\0") or raw[12:44].decode("ascii").lower() != ROM_MD5:
            raise ValueError("Captured state has an invalid M64+SAVE header or ROM binding")
        pending.rename(snapshot)
    manifest = {"kind": "local_route_recapture", "historical_snapshot_identity_claimed": False,
                "snapshot": {"path": str(snapshot), "sha256": sha256(snapshot),
                             "decompressed_sha256": hashlib.sha256(raw).hexdigest()},
                "base_sha256": BASE_SHA256, "route_sha256": ROUTE_SHA256,
                "rom_md5": ROM_MD5, "save_vi": 475, "post_save_vis": 300,
                "command": command, "environment": {key: env[key] for key in ("M64_PATH", "SAVE_PATH", "SAVE_VI", "POST_SAVE_VIS")}}
    (destination / "route_vi475.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"Recaptured {snapshot}; new provenance in {destination / 'route_vi475.json'}")


if __name__ == "__main__":
    main()
