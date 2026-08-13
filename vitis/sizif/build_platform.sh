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

platform.build()

platform_xpfm = client.get_platform("sizif_platform")
app = client.create_app_component(
    name="tmp_app",
    platform=platform_xpfm,
    domain="standalone_ps7_cortexa9_0",
    template="hello_world",
)
status = app.build(target="hw")
print("tmp_app build status:", status)

client.close()
EOF

(cd "$BUILD_DIR" && vitis -s "$PY_SCRIPT")

echo
echo "If the above finished without error, this should now exist:"
echo "  $BUILD_DIR/sizif_platform/export/sizif_platform/sw/standalone_ps7_cortexa9_0"
echo "with Xilinx.spec / cortexa9_toolchain.cmake / include/ / lib/ populated."
echo "Next: ./build_app.sh"
