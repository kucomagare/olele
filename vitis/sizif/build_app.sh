#!/usr/bin/env bash
# Builds the lwip_tcp_perf_client application against the platform export
# produced by build_platform.sh. Output lands in ./build/app/ (gitignored).
#
# Uses the system cmake (not the Vitis-bundled one — on this dev machine
# the bundled cmake-3.24.2 needs libssl.so.10, which isn't installed) and
# -DNON_YOCTO=ON, which is required or lwip_tcp_perf_client.cmake won't add
# the include path where the lwIP headers actually live.
#
# If you `source $REPO_PATH/bootenv.sh` first, SYSTEM_CMAKE/ARM_GNU_TOOLCHAIN_BIN
# are picked up from there; otherwise this falls back to sensible defaults.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$SCRIPT_DIR/app"
BUILD_DIR="$SCRIPT_DIR/build/app"
EXPORT_DIR="$SCRIPT_DIR/build/sizif_platform/export/sizif_platform/sw/standalone_ps7_cortexa9_0"
SHARED_DIR="$SCRIPT_DIR/../../shared"

exec > >(tee "$SCRIPT_DIR/build_app.log") 2>&1

if [[ ! -d "$EXPORT_DIR" ]]; then
    echo "Platform export dir not found at:"
    echo "  $EXPORT_DIR"
    echo "Run ./build_platform.sh <xsa> first."
    exit 1
fi

# Packet format header (generated from shared/packet_format.json), picked
# up via the plain #include "packet_format.h" in lwip_comm_client_raw.c
# (quoted includes search the including file's own directory first).
python3 "$SHARED_DIR/gen_packet_header.py" "$SHARED_DIR/packet_format.json" "$APP_DIR/packet_format.h"

export PATH="${ARM_GNU_TOOLCHAIN_BIN:-/tools/Xilinx/Vitis/2023.2/gnu/aarch32/lin/gcc-arm-none-eabi/bin}:$PATH"
CMAKE_BIN="${SYSTEM_CMAKE:-cmake}"

rm -rf "$BUILD_DIR"
"$CMAKE_BIN" -S "$APP_DIR" -B "$BUILD_DIR" -G "Unix Makefiles" \
  -DCMAKE_TOOLCHAIN_FILE="$EXPORT_DIR/cortexa9_toolchain.cmake" \
  -DCMAKE_SPECS_FILE="$EXPORT_DIR/Xilinx.spec" \
  -DCMAKE_INCLUDE_PATH="$EXPORT_DIR/include" \
  -DCMAKE_LIBRARY_PATH="$EXPORT_DIR/lib" \
  -DCMAKE_MODULE_PATH="$EXPORT_DIR" \
  -DNON_YOCTO=ON \
  -DCMAKE_MAKE_PROGRAM=make

"$CMAKE_BIN" --build "$BUILD_DIR" -j"$(nproc)"

echo
echo "Build complete: $BUILD_DIR/lwip_tcp_perf_client.elf"
