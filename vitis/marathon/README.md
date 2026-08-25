# vitis/marathon

Bare-metal firmware for the "marathon" hardware version: an lwIP raw-mode TCP
client running on the Zynq PS (Cortex-A9), based on the Xilinx
`lwip_tcp_perf_client` template but heavily customized. `lwip_comm_client_raw.c`
is the TCP protocol state machine (lifecycle callbacks + packet framing);
it delegates to three small, independently-focused modules rather than
doing everything itself:
- `comm_log.{c,h}` — buffered logging (append cheaply, flush once/loop)
- `rx_ring.{c,h}` — generic circular byte buffer, no protocol knowledge
- `axi_processing.{c,h}` — pokes/reads the ch1/ch2 AXI-Lite peripherals

## Reading the UART output

Messages are kept short on purpose: `xil_printf()` blocks until every byte
has shifted out, so at 115200 baud each byte costs ~87 us of main-loop
time, and output is capped by a 512 B/s budget in `comm_log.c`.

```
[CLK] 100ms = 100 ms                                    clock self-check, at boot
[S] 700p/s 350k smp/s 2.09MB/s loop 130k/s              once per second
[N] connecting / connected / closed, reconn             normal lifecycle
[E] ring full, resync / tcp -14, reconn / bad type N    problems
[E] +N suppressed                                       N messages dropped by the budget
```

`[S]` fields are packet rate, sample rate, throughput, and main-loop rate.
Two fields appear **only when abnormal**, so their presence is itself the
signal:

- `tx=N` — TX diverged from RX. On an echo path they should be equal;
  a difference means packets were dropped or the link reset mid-stream.
- `w=N` — the stats window was not ~1000 ms, i.e. something stalled the
  loop long enough to skew the measurement.

If `[CLK]` does not read ~100, treat every rate below it as wrong — see
`mono_clock.h` for the two BSP time sources that produce exactly that
failure.

**Framing invariant:** the wire format is length-prefixed with no sync
marker, so the read position is either exactly right or worthless — there
is no in-band recovery. Every "we are lost" path (RX ring full, unknown
packet type, implausible length) therefore funnels into
`tcp_client_resync()`, which drops the TCP connection and reconnects; the
PC-side relay reassembles whole packets before forwarding, so a fresh
connection is guaranteed to start on a packet boundary. Never "recover" by
discarding a partial run of bytes and continuing — that is precisely what
turned a transient overload into permanent corruption once already.

See `app/main.c` for the top-level loop and stats reporting.

## This is an SDT-flow build (matters more than it looks)

The platform is created through the Vitis Python API against a System
Device Tree, so the generated `Xilinx.spec` compiles everything with
`-DSDT`. Xilinx's template ships two mutually-exclusive copies of the
platform-init code and picks one on that flag:

- `platform.c` is `#if defined(SDT) || __MICROBLAZE__` — **this is the
  live one**, despite the generic name.
- `platform_zynq.c` was `#ifndef SDT` — dead here, so it has been deleted
  along with the SFP/I2C/PHY-reset sources (`sfp.c`, `si5324.c`,
  `i2c_access.c`, `iic_phyreset.c`), which were guarded on
  `XPAR_GIGE_PCS_PMA_*` / `XPS_BOARD_ZCU102` and can never apply to a Cora
  Z7. None of them contributed a single symbol to the linked ELF.

Consequence worth remembering: under SDT there is no separate
`platform_enable_interrupts()` call — interrupt setup happens inside
`init_platform()` via `xinterrupt_wrap`. `platform.h` still declares that
function and `platform.c` still has a dead `#ifndef SDT` block calling it;
both are left as-is to stay close to the vendor template.

Getting back to the legacy (non-SDT) flow would mean reinstalling Vitis
with the full installer, not flipping a flag — this install is the
"embedded installer", which omits the classic IDE entirely.

```
app/                  hand-written sources (excludes generated BSP)
  main.c, lwip_comm_client_raw.{c,h}, comm_log.{c,h}, rx_ring.{c,h},
  axi_processing.{c,h}, platform.{c,h}, platform_config.h.in,
  lscript.ld, CMakeLists.txt, UserConfig.cmake,
  lwip_tcp_perf_client.cmake, app.yaml
build/                 empty in git; platform + app build output lands here
build_platform.sh      creates the Vitis platform component from a .xsa
build_app.sh           builds app/ against that platform via CMake
run.sh                 downloads + runs the ELF on the board via xsdb (JTAG)
```

Phase 2 (app) below is scripted and confirmed working. Phase 1 (platform)
is scripted via the Vitis Python automation API (`vitis -s`) instead of
xsct/Tcl -- see the comment header in `build_platform.sh` for why (xsct's
`setws` hangs on this install's "embedded" Vitis, and the GUI can't import
an xsct-created platform either). If it ever fails, the fallback is still a
fully GUI-native platform+app creation (delete `build/marathon_platform`
first, then `vitis` with workspace = `build/`, New Component -> Platform
from the .xsa, New Component -> Application with the Hello World
template). Phase 3 (run on hardware) is confirmed working end to end: the
board connects and streams (`[PCB] RAW TCP connected!` followed by matched
RX/TX `[STATS]`).

## 1. Get the .xsa

Build `vivado/marathon` first (see its README) through "Export Hardware".
You'll end up with something like
`vivado/marathon/build/tcp_client/CoraZ7_Eth_wrapper.xsa` (or wherever you
pointed `write_hw_platform`).

## 2. Create the platform

```bash
source /tools/Xilinx/Vitis/2023.2/settings64.sh
./build_platform.sh /path/to/CoraZ7_Eth_wrapper.xsa
```

This runs `vitis -s <script>` (Vitis's Python automation API, headless —
spawns `vitis-server` directly, no GUI) to create a platform named
`marathon_platform` in `build/`, with a `standalone_ps7_cortexa9_0` domain
(matching `app/app.yaml`'s `domain_path`), `lwip213` explicitly enabled as
a BSP library (not on by default), applies the lwIP tuning (see below) and
builds the platform. That produces the full CMake export tree
(`Xilinx.spec`, `cortexa9_toolchain.cmake`, `Findcommon.cmake`,
`include/`, `lib/`) that `build_app.sh` needs — the BSP libraries are
compiled as part of the platform build under the SDT flow.

(Earlier versions of this script also created and built a throwaway
`tmp_app` here, believing `platform.build()` alone didn't populate that
tree. It does — verified on a from-scratch rebuild 2026-08-17 where the
`tmp_app` step failed outright and the export tree was still complete and
usable. The step was removed; it only guaranteed an `ALREADY_EXISTS`
error on every re-run.)

Confirmed working on this machine. If it ever errors out, see "Fallback:
fully GUI-native" below.

### Tuning the BSP (lwIP buffers etc.)

`lwipopts.h` is **generated** — don't edit it in the build tree, the next
platform build overwrites it. Its values come from `lwip213_*` CMake cache
variables (`libsrc/lwip213/src/lwip213.cmake`), set via
`domain.set_config(option="lib", ..., lib_name="lwip213")` in
`build_platform.sh`'s Python block. `bsp.yaml` in the built BSP lists every
settable parameter with its default and description.

These are applied **at domain-creation time only**, so changing one means a
full platform rebuild — the script refuses to run against an existing
`build/marathon_platform` and tells you to delete it first:

```bash
rm -rf build/marathon_platform
./build_platform.sh /path/to/CoraZ7_Eth_wrapper.xsa
./build_app.sh
```

No Vivado step needed — the `.xsa` is unchanged.

Two traps worth knowing:

- **65535 is a hard ceiling** for `tcp_wnd` and `tcp_snd_buf`.
  `LWIP_WND_SCALE` is off, so `tcpwnd_size_t` is `u16_t`; 65536 wraps to 0.
- **Don't raise `n_rx_descriptors` alone.** The Xilinx EMAC port pins one
  `PBUF_POOL` buffer per RX descriptor for the ring's lifetime
  (`xemacpsif_dma.c`), so with `pbuf_pool_size=256`, going to 256
  descriptors consumes the whole pool at init and starves everything else.
  Raise `lwip213_pbuf_pool_size` in the same step or leave it at 64.

Bad combinations mostly fail at compile time rather than on hardware —
`lwip-2.1.3/src/core/init.c` has `#error` sanity checks for window sizes,
`TCP_SND_BUF >= 2*TCP_MSS`, `TCP_SND_QUEUELEN`, and `TCP_WND` against the
pbuf pool capacity.

### Fallback: fully GUI-native (if the script above fails)

This was the originally-proven-working path before the Python-API script
existed. `setws`/`app create` via `xsct` (Tcl) hangs on this machine's
Vitis 2023.2 "embedded installer" (`Error: --classic option is not
supported as classic Vitis IDE is not included in the Vitis embedded
installer`), and the GUI can't import a platform created outside it
either — so if the Python-API route also fails, fall back to doing it
entirely by hand:

1. `rm -rf build/marathon_platform` (delete any partial platform first).
2. `vitis`, with the workspace set to `build/`.
3. **File → New Component → Platform** — from the `.xsa`, name
   `marathon_platform`, `ps7_cortexa9_0`/standalone, keep boot artifacts on.
   Build it, then enable `lwip213` via BSP Settings and rebuild.
4. **File → New Component → Application** — platform `marathon_platform`,
   domain `standalone_ps7_cortexa9_0`, template **Hello World**, name
   e.g. `tmp_app`. Build it (right-click → **Build**).

After either path, `build/marathon_platform/export/marathon_platform/sw/standalone_ps7_cortexa9_0`
has everything `build_app.sh` needs.

## 3. Build the application

```bash
./build_app.sh
```

Builds `app/` via CMake against
`build/marathon_platform/export/marathon_platform/sw/standalone_ps7_cortexa9_0`,
using the system `cmake` (not the Vitis-bundled one) and `-DNON_YOCTO=ON`
(required — without it the lwIP include path isn't added and the build
fails on missing `lwip/tcp.h`). Output: `build/app/lwip_tcp_perf_client.elf`.

Before compiling, this also regenerates `app/packet_format.h` from
`../../shared/<variant>/packet_format.json` (see the root README's "Wire packet
format" section) — `lwip_comm_client_raw.c` picks it up via a plain
`#include "packet_format.h"`. The generated header is gitignored; edit
`packet_format.json`, not `app/packet_format.h` directly.

## 4. Run it on the board

No Vivado needed — with the board connected/powered:

```bash
./run.sh
```

This programs the bitstream (defaults to
`vivado/marathon/build/tcp_client/tcp_client.runs/impl_1/CoraZ7_Eth_wrapper.bit`,
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
