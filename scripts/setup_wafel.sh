#!/usr/bin/env bash
set -euo pipefail

root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
cc=${CC:-}
wine=${WINE:-}
python=${PYTHON:-python3}
rom=${SM64_JP_ROM:-}
dll=${DLL:-$root/wafel/libsm64/sm64_jp.dll}
mode=all

usage() {
    cat <<'EOF'
Usage: scripts/setup_wafel.sh [--rom PATH] [--dll PATH] [--build-only | --unlock-only]
                              [--cc PATH] [--wine PATH] [--python PATH]

Compile the three Win64 C harnesses and unlock the pinned Wafel JP DLL using
an original Japanese SM64 ROM supplied by you. No ROM is downloaded. A newly
compiled or differently versioned libsm64 is NOT equivalent: the harnesses use
private RVAs from the exact historical DLL and reject any other SHA256.

Requirements: Python 3, libsodium (only for unlock), Win64 MinGW GCC, Wine64.
If native tools are unavailable, first run scripts/setup_windows_toolchain.sh;
its project-local .tools/bin wrappers are detected automatically. CC and WINE
must each name one executable (not a shell command with flags).
Docker tools can access only project-local DLLs, executables, and Wine prefixes;
copy external artifacts under the project or explicitly select native tools.

--build-only   Compile without requiring a ROM/DLL; useful before supplying one.
--unlock-only  Unlock/verify without requiring a compiler or Wine.
Environment: CC, WINE, PYTHON, SM64_JP_ROM, DLL, WINEPREFIX.
EOF
}

while (($#)); do
    case "$1" in
        --rom|--dll|--cc|--wine|--python)
            if (($# < 2)); then printf 'Missing value for %s\n' "$1" >&2; exit 2; fi
            case "$1" in
                --rom) rom=$2 ;;
                --dll) dll=$2 ;;
                --cc) cc=$2 ;;
                --wine) wine=$2 ;;
                --python) python=$2 ;;
            esac
            shift 2 ;;
        --build-only|--unlock-only)
            if [[ $mode != all ]]; then printf 'Choose only one mode.\n' >&2; exit 2; fi
            mode=${1#--}; shift ;;
        --help|-h) usage; exit 0 ;;
        *) printf 'Unknown argument: %s\n' "$1" >&2; usage >&2; exit 2 ;;
    esac
done

if [[ -z $cc ]]; then
    if command -v x86_64-w64-mingw32-gcc >/dev/null 2>&1; then
        cc=$(command -v x86_64-w64-mingw32-gcc)
    elif [[ -x $root/.tools/bin/x86_64-w64-mingw32-gcc ]]; then
        cc=$root/.tools/bin/x86_64-w64-mingw32-gcc
    fi
fi
if [[ -z $wine ]]; then
    if command -v wine >/dev/null 2>&1; then
        wine=$(command -v wine)
    elif command -v wine64 >/dev/null 2>&1; then
        wine=$(command -v wine64)
    elif [[ -x $root/.tools/bin/wine ]]; then
        wine=$root/.tools/bin/wine
    fi
fi

# Check the mount boundary before compiling or creating an unlocked artifact.
# Resolving symlinks also catches an existing output that points outside ROOT.
dll=$("$python" - "$root" "$dll" "$cc" "$wine" "$mode" <<'PY'
import os
import shutil
import sys
from pathlib import Path

root = Path(sys.argv[1])
dll = Path(sys.argv[2]).expanduser().resolve()
cc, wine, mode = sys.argv[3:]
wrapper = root / "scripts/windows_tool.sh"

def container_tool(command):
    executable = shutil.which(command) if command else None
    return executable is not None and Path(executable).resolve() == wrapper

if container_tool(cc) or container_tool(wine):
    artifacts = {"DLL": dll}
    if mode != "unlock-only":
        for name in ("wafel_incident_fuzz", "wafel_ttc_probe", "floor_null_probe"):
            artifacts[f"{name} executable"] = (root / f"scripts/{name}.exe").resolve()
    if mode == "all" and container_tool(wine):
        artifacts["WINEPREFIX"] = Path(os.environ.get("WINEPREFIX", root / ".wine-wafel")).expanduser().resolve()
    for label, path in artifacts.items():
        if not path.is_relative_to(root):
            print(f"Container {label} must resolve inside {root}: {path}. "
                  "Copy artifacts under the project or select native CC/WINE tools.", file=sys.stderr)
            raise SystemExit(2)
print(dll)
PY
)

if [[ $mode != unlock-only ]]; then
    if [[ -z $cc ]]; then
        printf 'Missing Win64 MinGW GCC; run scripts/setup_windows_toolchain.sh or set CC.\n' >&2
        exit 2
    fi
    for harness in wafel_incident_fuzz wafel_ttc_probe floor_null_probe; do
        "$cc" -O2 -std=c11 -Wall -Wextra -static-libgcc \
            -o "$root/scripts/$harness.exe" "$root/scripts/$harness.c" -lbcrypt -lm
        printf 'Built %s\n' "$root/scripts/$harness.exe"
    done
fi

if [[ $mode == build-only ]]; then exit 0; fi

# Reproduce wafel_api/src/lock.rs at Wafel commit
# 5b808b60af15d316a5e2b0f87db34421d6225b57 using the same libsodium APIs as
# pwbox 0.5.0 (scrypt-nacl + XSalsa20-Poly1305 detached). This avoids building
# the Windows GUI/Rust workspace just to unlock its already-pinned DLL.
"$python" - "$root" "$rom" "$dll" <<'PY'
import ctypes
import ctypes.util
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

root = Path(sys.argv[1])
rom_arg = sys.argv[2]
dll = Path(sys.argv[3]).expanduser().resolve()
expected_dll = "a3dc4984628bfc2bcdc92eb2c3af47beae1472fd54c07a24c286046d7962f67b"
expected_locked = "fd45424e5a96256aa1b99df781892c4f72f442e34aa4f55e5ce13371c4a726c3"
expected_rom = "8a20a5c83d6ceb0f0506cfc9fa20d8f438cafe51"

def fail(message):
    raise SystemExit(message)

if dll.exists():
    actual = hashlib.sha256(dll.read_bytes()).hexdigest()
    if actual != expected_dll:
        fail(f"Refusing incompatible DLL {dll}\nExpected SHA256 {expected_dll}\nActual   SHA256 {actual}\nUse --dll with a different output path to unlock the pinned artifact.")
    print(f"Verified exact JP DLL: {dll}\nSHA256 {actual}")
    raise SystemExit(0)

if not rom_arg:
    candidates = [root / "emulator/sm64.jp.z64", root / "sm64/baserom.jp.z64",
                  root / "roms/sm64_jp.z64", root / "wafel/roms/sm64_jp.z64"]
    rom_arg = next((str(path) for path in candidates if path.is_file()), "")
if not rom_arg:
    fail("Missing legally supplied original JP SM64 ROM. Pass --rom /path/to/your/rom (z64/v64/n64), or SM64_JP_ROM.\nAlternatively supply the exact historical DLL via --dll PATH. No ROM has been downloaded.")
rom_path = Path(rom_arg).expanduser()
if not rom_path.is_file():
    fail(f"ROM does not exist: {rom_path}")
rom = bytearray(rom_path.read_bytes())
if len(rom) < 4 or len(rom) % 4:
    fail("Invalid ROM length (must be a whole number of 32-bit words).")
if rom[:4] == b"\x37\x80\x40\x12":
    rom[0::2], rom[1::2] = rom[1::2], rom[0::2]
elif rom[:4] == b"\x40\x12\x37\x80":
    rom[0::4], rom[1::4], rom[2::4], rom[3::4] = rom[3::4], rom[2::4], rom[1::4], rom[0::4]
elif rom[:4] != b"\x80\x37\x12\x40":
    fail("Unknown N64 ROM byte order.")
actual_rom = hashlib.sha1(rom).hexdigest()
if actual_rom != expected_rom:
    fail(f"Original Japanese SM64 ROM required (not US, Shindou, or a ROM hack).\nExpected normalized SHA1 {expected_rom}\nActual   normalized SHA1 {actual_rom}")

locked = root / "wafel/libsm64/sm64_jp.dll.locked"
if not locked.is_file():
    fail(f"Missing pinned encrypted artifact: {locked}\nRestore branpk/wafel commit 5b808b60af15d316a5e2b0f87db34421d6225b57.")
locked_bytes = locked.read_bytes()
if hashlib.sha256(locked_bytes).hexdigest() != expected_locked:
    fail("Encrypted DLL does not match the pinned Wafel artifact; refusing to unlock.")
box = json.loads(locked_bytes)
del locked_bytes
if box["kdf"] != "scrypt-nacl" or box["cipher"] != "xsalsa20-poly1305":
    fail("Unsupported encrypted DLL format.")
salt = bytes.fromhex(box["kdfparams"]["salt"])
nonce = bytes.fromhex(box["cipherparams"]["iv"])
mac = bytes.fromhex(box["mac"])
ciphertext = bytes.fromhex(box["ciphertext"])
if (len(salt), len(nonce), len(mac)) != (32, 24, 16):
    fail("Invalid encrypted DLL salt/nonce/MAC length.")
library = ctypes.util.find_library("sodium")
if not library:
    fail("Missing libsodium shared library (Arch: libsodium; Debian: libsodium23). No Python packages or Rust build are required.")
sodium = ctypes.CDLL(library)
sodium.sodium_init.restype = ctypes.c_int
if sodium.sodium_init() < 0:
    fail("libsodium initialization failed.")
derive = sodium.crypto_pwhash_scryptsalsa208sha256
derive.argtypes = [ctypes.c_void_p, ctypes.c_ulonglong, ctypes.c_void_p, ctypes.c_ulonglong, ctypes.c_void_p, ctypes.c_ulonglong, ctypes.c_size_t]
derive.restype = ctypes.c_int
open_box = sodium.crypto_secretbox_open_detached
open_box.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulonglong, ctypes.c_void_p, ctypes.c_void_p]
open_box.restype = ctypes.c_int
key = ctypes.create_string_buffer(32)
rom_buffer = (ctypes.c_ubyte * len(rom)).from_buffer(rom)
if derive(key, 32, rom_buffer, len(rom), salt, box["kdfparams"]["opslimit"], box["kdfparams"]["memlimit"]) != 0:
    fail("libsodium could not derive the unlock key.")
plaintext = ctypes.create_string_buffer(len(ciphertext))
if open_box(plaintext, ciphertext, mac, len(ciphertext), nonce, key) != 0:
    fail("DLL authentication failed; no output written.")
actual = hashlib.sha256(plaintext).hexdigest()
if actual != expected_dll:
    fail(f"Unlocked DLL is not the historical RVA-compatible image: {actual}. No output written.")
dll.parent.mkdir(parents=True, exist_ok=True)
with tempfile.NamedTemporaryFile(dir=dll.parent, prefix=dll.name + ".", delete=False) as output:
    temporary = Path(output.name)
    try:
        output.write(plaintext)
        output.flush()
        os.fsync(output.fileno())
        os.link(temporary, dll)  # Never overwrite another artifact.
    finally:
        temporary.unlink()
print(f"Unlocked exact JP DLL: {dll}\nSHA256 {actual}")
PY

if [[ $mode == unlock-only ]]; then exit 0; fi
if [[ -z $wine ]]; then
    printf 'DLL and harnesses ready; Wine64 missing. Run scripts/setup_windows_toolchain.sh or set WINE.\n' >&2
    exit 2
fi
if ! command -v "$wine" >/dev/null 2>&1; then
    printf 'Wine64 executable not found: %s\n' "$wine" >&2
    exit 2
fi
printf 'Ready. Local smoke commands (no historical result files overwritten):\n'
printf 'WINE=%q DLL=%q WORKERS=1 TRIALS=100 bash %q\n' "$wine" "$dll" "$root/scripts/run_wafel_campaign.sh"
printf '%q %q --wine %q --cc %q --dll %q --natural-frames 32\n' "$python" "$root/scripts/probe_floor_null_fallback.py" "$wine" "$cc" "$dll"
