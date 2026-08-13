# vitis/sizif

Bare-metal firmware for the "sizif" hardware version: an lwIP raw-mode TCP
client running on the Zynq PS (Cortex-A9), based on the Xilinx
`lwip_tcp_perf_client` template but heavily customized (`lwip_comm_client_raw.c`
implements the actual comms/ring-buffer/protocol logic; see `app/main.c`
for the top-level loop and stats reporting).

```
app/                  hand-written sources (excludes generated BSP)
  main.c, lwip_comm_client_raw.{c,h}, platform*.c/h, i2c_access.c, sfp.c,
  si5324.c, iic_phyreset.c, lscript.ld, CMakeLists.txt, UserConfig.cmake,
  lwip_tcp_perf_client.cmake, app.yaml, README.txt
build/                 empty in git; platform + app build output lands here
build_platform.sh      creates the Vitis platform component from a .xsa
build_app.sh           builds app/ against that platform via CMake
run.sh                 downloads + runs the ELF on the board via xsdb (JTAG)
```

Phase 2 (app) below is scripted and confirmed working. Phase 1 (platform)
is scripted via the Vitis Python automation API (`vitis -s`) instead of
xsct/Tcl -- see the comment header in `build_platform.sh` for why (xsct's
`setws` hangs on this install's "embedded" Vitis, and the GUI can't import
an xsct-created platform either). The Python-API version has not yet been
run end-to-end on this machine; if it fails, the fallback is still a fully
GUI-native platform+app creation (delete `build/sizif_platform` first,
then `vitis` with workspace = `build/`, New Component -> Platform from the
.xsa, New Component -> Application with the Hello World template). Phase 3
(run on hardware) is best-effort, not yet confirmed end-to-end.

## 1. Get the .xsa

Build `vivado/sizif` first (see its README) through "Export Hardware".
You'll end up with something like
`vivado/sizif/build/tcp_client/CoraZ7_Eth_wrapper.xsa` (or wherever you
pointed `write_hw_platform`).

## 2. Create the platform

```bash
source /tools/Xilinx/Vitis/2023.2/settings64.sh
./build_platform.sh /path/to/CoraZ7_Eth_wrapper.xsa
```

This runs `vitis -s <script>` (Vitis's Python automation API, headless —
spawns `vitis-server` directly, no GUI) to create a platform named
`sizif_platform` in `build/`, with a `standalone_ps7_cortexa9_0` domain
(matching `app/app.yaml`'s `domain_path`), `lwip213` explicitly enabled as
a BSP library (not on by default), builds the platform, then creates and
builds a throwaway `tmp_app` (Hello World template) against it — that
throwaway build is what forces Vitis to populate the full CMake export
tree (`Xilinx.spec`, `cortexa9_toolchain.cmake`, `Findcommon.cmake`,
`include/`, `lib/`) that `build_app.sh` needs; `platform build()` alone
does not produce them. `tmp_app` itself is never used for anything else.

Not yet confirmed by an actual run on this machine — if it errors out,
see "Fallback: fully GUI-native" below.

### Fallback: fully GUI-native (if the script above fails)

This was the originally-proven-working path before the Python-API script
existed. `setws`/`app create` via `xsct` (Tcl) hangs on this machine's
Vitis 2023.2 "embedded installer" (`Error: --classic option is not
supported as classic Vitis IDE is not included in the Vitis embedded
installer`), and the GUI can't import a platform created outside it
either — so if the Python-API route also fails, fall back to doing it
entirely by hand:

1. `rm -rf build/sizif_platform` (delete any partial platform first).
2. `vitis`, with the workspace set to `build/`.
3. **File → New Component → Platform** — from the `.xsa`, name
   `sizif_platform`, `ps7_cortexa9_0`/standalone, keep boot artifacts on.
   Build it, then enable `lwip213` via BSP Settings and rebuild.
4. **File → New Component → Application** — platform `sizif_platform`,
   domain `standalone_ps7_cortexa9_0`, template **Hello World**, name
   e.g. `tmp_app`. Build it (right-click → **Build**).

After either path, `build/sizif_platform/export/sizif_platform/sw/standalone_ps7_cortexa9_0`
has everything `build_app.sh` needs.

## 3. Build the application

```bash
./build_app.sh
```

Builds `app/` via CMake against
`build/sizif_platform/export/sizif_platform/sw/standalone_ps7_cortexa9_0`,
using the system `cmake` (not the Vitis-bundled one) and `-DNON_YOCTO=ON`
(required — without it the lwIP include path isn't added and the build
fails on missing `lwip/tcp.h`). Output: `build/app/lwip_tcp_perf_client.elf`.

## 4. Run it on the board

No Vivado needed — with the board connected/powered:

```bash
./run.sh
```

This programs the bitstream (defaults to
`vivado/sizif/build/tcp_client/tcp_client.runs/impl_1/CoraZ7_Eth_wrapper.bit`,
override by passing a different `.bit` path as `$1`), initializes the PS
via `ps7_init.tcl` (normally the FSBL's job — needed here since we're
going straight over JTAG, not booting through the FSBL), then downloads
and runs the ELF — the same sequence the Vitis IDE's "Run" button does
under the hood. **If this errors:** fall back to the Vitis IDE — right-click
the `lwip_tcp_perf_client` component → **Run As → Launch on Hardware
(Single Application Debug)**.

`run.sh` auto-launches a PuTTY serial console (115200 baud) on the board's
UART before flashing, so firmware output is visible immediately — no
manual step needed. `[STATS]` lines report RX/TX packet/sample/MB-per-second
counters once the PC app is streaming to it. Each run also logs the full
session to `build/putty_logs/session_<timestamp>.log` (via PuTTY's `-log`
flag, one file per run so nothing gets overwritten) — useful for comparing
a good boot against a bad one after the fact. If PuTTY or the UART device
isn't found, `run.sh` just warns and continues (JTAG flashing still
works); in that case open one yourself, e.g.
`putty -serial /dev/serial/by-id/usb-Digilent_..._if01-port0 -sercfg 115200,8,n,1,N`
(or `minicom`).
