#!/usr/bin/env bash
# Creates the Vitis platform component ("sizif_platform") from a hardware
# .xsa (produced by vivado/sizif's build.sh + Vivado's Export Hardware step)
# AND the throwaway app needed to populate the CMake export tree, via the
# Vitis 2023.2 Python automation API (`vitis -s <script>`). Output lands in
# ./build/ (gitignored).
#
# This replaces an earlier xsct/Tcl version of this script. That version
# could only get as far as `platform generate` -- the remaining step
# (build a throwaway app against the domain, needed to force Vitis to
# populate Xilinx.spec / cortexa9_toolchain.cmake / Findcommon.cmake /
# include/ / lib/) requires `setws`, which hangs on this machine's Vitis
# "embedded installer" ("classic Vitis IDE is not included"). The GUI
# couldn't pick up an xsct-created platform either (tried both
# File->Open Workspace and `vitis -w <dir>` -- neither showed the platform).
#
# The Python API (`import vitis; vitis.create_client(...)`) is a different
# code path: it spawns a standalone `vitis-server` process directly
# (see /tools/Xilinx/Vitis/2023.2/cli/vitis/_server.py), not the
# Electron/classic-IDE machinery that `setws` depends on. The module
# explicitly detects VITIS_EMBEDDED_INSTALL and supports "embedded
#
# Domain-add and app-create calls below follow the official reference
# example for exactly this scenario:
# /tools/Xilinx/Vitis/2023.2/cli/examples/platform_uc4_zynq.py
# ("Platform flow use case 4: Creation of zynq platform, baremetal domain
# creation, generation and app component creation"). Two API gotchas that
# example clarified vs. a first-principles reading of __init__.py's
# docstrings: (1) the domain must be added via `platform.add_domain(...)`
# after `create_platform_component`, not via that call's own `domain_name=`
# param -- the latter doesn't register the domain in a form
# `create_app_component` can later find by name. (2) `create_app_component`'s
# `platform=` argument must be the platform's `.xpfm` path string from
# `client.get_platform(name)`, not the `Platform` object and not a bare
# name string -- passing the object raises a TypeError from the underlying
# gRPC call, and passing a bare name string produces a mystifying
# "Invalid Template ... for Domain ''" error instead.
# component related functions" -- exactly platform/app creation for a
# baremetal ps7_cortexa9_0 domain, which is all we need. Confirmed by
# reading the actual installed module (not guessed); not yet confirmed
# by an actual run on this machine.
set -euo pipefail

if [[ $# -ne 1 ]]; then
    echo "Usage: $0 <path-to-CoraZ7_Eth_wrapper.xsa>"
    exit 1
fi

XSA_PATH="$(realpath "$1")"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD_DIR="$SCRIPT_DIR/build"

exec > >(tee "$SCRIPT_DIR/build_platform.log") 2>&1

mkdir -p "$BUILD_DIR"

# create_platform_component fails with an unhelpful gRPC error if the
# component already exists, and the BSP tuning below is only applied while
# the domain is being created -- so re-running against an existing platform
# would silently keep the old lwIP settings even if it did succeed. Refuse
# up front with an actionable message instead.
if [[ -d "$BUILD_DIR/sizif_platform" ]]; then
    echo "A platform already exists at:"
    echo "  $BUILD_DIR/sizif_platform"
    echo
    echo "Delete it first if you want to rebuild (required after changing"
    echo "any lwip213_* setting in this script -- they are applied at"
    echo "domain-creation time and are not picked up by a re-run):"
    echo
    echo "  rm -rf $BUILD_DIR/sizif_platform"
    echo
    echo "Then re-run this script, followed by ./build_app.sh."
    exit 1
fi

PY_SCRIPT="$(mktemp --suffix=.py)"
trap 'rm -f "$PY_SCRIPT"' EXIT

cat > "$PY_SCRIPT" <<EOF
import vitis

client = vitis.create_client(workspace="${BUILD_DIR}")

platform = client.create_platform_component(
    name="sizif_platform",
    hw="${XSA_PATH}",
)

domain = platform.add_domain(
    name="standalone_ps7_cortexa9_0",
    cpu="ps7_cortexa9_0",
    os="standalone",
)
domain.set_lib(lib_name="lwip213")

# lwIP tuning. These map 1:1 onto the lwip213_* CMake cache variables in
# the generated BSP (libsrc/lwip213/src/lwip213.cmake), which in turn
# become the #defines in lwipopts.h -- that header is generated, so it
# must be changed here rather than edited in the build tree.
#
# Why: with the stock values the board's echo throughput was pinned at
# ~0.4 MB/s, limited by bytes-in-flight rather than by CPU, link speed or
# the AXI peripherals. Measured on hardware 2026-08-17: 146 pkt/s at
# 3004 B/packet (0.44 MB/s) vs 57 pkt/s at 6004 B/packet (0.34 MB/s) --
# both land near ~6 KB in flight, which is what TCP_SND_BUF=8192 allows.
#
# 65535 is the hard ceiling for both window values, not a round number:
# LWIP_WND_SCALE is off in this BSP, so tcpwnd_size_t is u16_t and
# tcp_sndbuf() returns u16_t. 65536 would wrap to 0.
for _param, _value in [
    # Inbound receive window: was 2048, the tightest limit of the two.
    ("lwip213_tcp_wnd",        65535),
    # Outbound unacked allowance, gates the echo path back to the PC.
    ("lwip213_tcp_snd_buf",    65535),
    # Heap backing copied send data (we use TCP_WRITE_FLAG_COPY), sized to
    # comfortably hold a full 64 KB send buffer. ~+400 KB of .bss.
    ("lwip213_mem_size",       524288),
    # TCP_SND_QUEUELEN is derived as 16*TCP_SND_BUF/TCP_MSS = 718 at the
    # values above, so the segment pool has to clear that.
    ("lwip213_memp_n_tcp_seg", 1024),
]:
    domain.set_config(option="lib", param=_param, value=_value,
                      lib_name="lwip213")

# NOTE: deliberately NOT raising lwip213_n_rx_descriptors. The Xilinx EMAC
# port (contrib/ports/xilinx/netif/xemacpsif_dma.c) allocates one pbuf from
# PBUF_POOL per RX descriptor at init and pins it for the lifetime of the
# ring. With PBUF_POOL_SIZE=256, going to 256 descriptors would consume the
# entire pool at startup and starve TCP reassembly/transmit. If you ever do
# want deeper rings, raise lwip213_pbuf_pool_size in the same step.

platform.build()

# NOTE: this used to be followed by creating and building a throwaway
# "tmp_app" (hello_world template), on the belief that platform.build()
# alone did not populate the CMake export tree (Xilinx.spec,
# cortexa9_toolchain.cmake, include/, lib/) that build_app.sh needs.
#
# That is not true, at least on Vitis 2023.2 here. Proven on 2026-08-17:
# build/sizif_platform was deleted, this script ran, the tmp_app step
# errored out before creating anything (stale workspace registration), and
# the export tree still came out complete -- build_app.sh then configured
# and linked against it with no trouble. It makes sense in hindsight: the
# BSP libraries (liblwip213.a and friends) are compiled as part of the
# platform build itself under the SDT flow.
#
# So the step is gone: it added a guaranteed failure on every re-run
# (create_app_component raises ALREADY_EXISTS) for no benefit. If a future
# platform build ever does leave the export tree incomplete, recreate a
# throwaway app by hand -- the GUI fallback in README.md covers it.

client.close()
EOF

(cd "$BUILD_DIR" && vitis -s "$PY_SCRIPT")

echo
echo "If the above finished without error, this should now exist:"
echo "  $BUILD_DIR/sizif_platform/export/sizif_platform/sw/standalone_ps7_cortexa9_0"
echo "with Xilinx.spec / cortexa9_toolchain.cmake / include/ / lib/ populated."
echo "Next: ./build_app.sh"
