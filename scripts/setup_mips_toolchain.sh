#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
VERSION=2.46.0
SHA256=d75a94f4d73e7a4086f7513e67e439e8fcdcbb726ffe63f4661744e6256b2cf2
ARCHIVE="$ROOT/emulator/downloads/binutils-$VERSION.tar.xz"
BUILD="$ROOT/emulator/build/binutils-mips"
PREFIX="$ROOT/emulator/toolchain"
mkdir -p "$(dirname -- "$ARCHIVE")" "$BUILD" "$PREFIX"
if [[ ! -f "$ARCHIVE" ]]; then
  curl --fail --location --output "$ARCHIVE.part" "https://ftp.gnu.org/gnu/binutils/binutils-$VERSION.tar.xz"
  printf '%s  %s\n' "$SHA256" "$ARCHIVE.part" | sha256sum --check
  mv "$ARCHIVE.part" "$ARCHIVE"
fi
printf '%s  %s\n' "$SHA256" "$ARCHIVE" | sha256sum --check
if [[ ! -d "$ROOT/emulator/build/binutils-$VERSION" ]]; then
  tar -xf "$ARCHIVE" -C "$ROOT/emulator/build"
fi
cd "$BUILD"
"../binutils-$VERSION/configure" --target=mips64-elf --prefix="$PREFIX" --bindir="$PREFIX" \
  --disable-nls --disable-werror --disable-gdb --disable-gprofng --disable-sim --disable-gold --without-debuginfod
make -j"${JOBS:-$(nproc)}"
make install
"$PREFIX/mips64-elf-objdump" --version
