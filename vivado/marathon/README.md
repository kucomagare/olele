# vivado/marathon

Hand-written Vivado sources for the "marathon" hardware version (Cora Z7-10,
PS7 + AXI interconnect + `axi_gpio` x N wired to an ADC + AXI DMA
IP + two independent per-channel AXI processing chains (`axi_processing_ch1`,
`axi_processing_ch2`) + a TDM streaming filter). This is the only thing tracked in
git for this version — `build/` is regenerated locally, never committed.

```
cora_z7.tcl        regenerates the full Vivado project into ./build/ --
                    project creation, source imports, wrapper generation;
                    sources bd_CoraZ7_Eth.tcl for the block design itself
bd_CoraZ7_Eth.tcl   the block design (proc cr_bd_CoraZ7_Eth) -- split into
                    its own file so re-exporting from the GUI after a
                    change is a straight overwrite, no manual merge
hdl/                fpga_top.v, my_axi.v, axi_tdm_filter.vhd,
                    axi_processing_ch1.vhd, axi_processing_ch2.vhd
xdc/                Cora-Z7-10-Master.xdc (pin/clock constraints)
                    required by cora_z7.tcl, not regenerable from the .v alone
build/              empty in git; the actual Vivado project lands here
build.sh            wrapper that runs cora_z7.tcl with the right paths
```

## Changing the block design

Edit it in the Vivado GUI (`vivado build/tcp_client/tcp_client.xpr`, open
the block design), then sync the change back to tracked source:

1. Validate Design, then save the block design (Ctrl+S).
2. **File → Export → Export Block Design...**, pointed at this exact
   path: `vivado/marathon/bd_CoraZ7_Eth.tcl` (overwrite it).
3. `./clean.sh && ./build.sh` to confirm the updated file reproduces the
   design from tracked source alone.

`cora_z7.tcl` itself doesn't need touching — it just `source`s this file
and calls `cr_bd_CoraZ7_Eth`; wrapper regeneration happens automatically
right after. The one thing this doesn't cover: changes to the actual RTL
*inside* `fpga_top.v`/`my_axi.v`/`axi_tdm_filter.vhd` (not just how those modules
are wired into the BD) still need direct edits under `hdl/`.

## 1. Recreate the project

```bash
source /tools/Xilinx/Vivado/2023.2/settings64.sh   # adjust if installed elsewhere
./build.sh
```

This creates `build/tcp_client/tcp_client.xpr` (project name is `tcp_client`
internally — inherited from the original export, harmless). Nothing is
synthesized yet at this point.

## 2. Build + export hardware (batch, no GUI)

```bash
./build_bitstream.sh
```

Runs synthesis, implementation, bitstream generation, and
`write_hw_platform` (with bitstream included) all in Vivado batch mode —
not yet run end to end on this machine, so treat it as best-effort until
confirmed. Output: the existing project's runs get built, plus
`build/tcp_client/CoraZ7_Eth_wrapper.xsa`, which is what `vitis/marathon/`
needs next.

### Or, via the GUI

```bash
vivado build/tcp_client/tcp_client.xpr
```

Flow Navigator: **Run Synthesis** → **Run Implementation** →
**Generate Bitstream**, then **File → Export → Export Hardware…** (check
"Include bitstream"), export as `CoraZ7_Eth_wrapper.xsa`.

## 3. Flash the board (prove the bitstream works)

1. Connect the Cora Z7 to the PC over USB-JTAG and power it on.
2. **Flow Navigator → Open Hardware Manager → Open Target → Auto Connect.**
3. Right-click the `xc7z010` device → **Program Device**, pick the
   `.bit` produced in step 2 (`build/tcp_client/tcp_client.runs/impl_1/CoraZ7_Eth_wrapper.bit`).
4. Once programming reports success, the board is running this hardware
   design — that's the proof point for this step. Firmware (Vitis) is a
   separate follow-up.

## Custom AXI peripherals

Three custom AXI4-Lite slaves, all wrapping the shared `my_axi.v` bus
interface (which reserves reg3/offset `0xC` on read to return an external
"result" input instead of its own stored value — the hook each of these
rides its computed output back to the CPU on):

| Instance                | Base addr    | Logic                                | reg0 (write) | reg3 (read)      |
|--------------------------|--------------|----------------------------------------|--------------|-------------------|
| `axi_tdm_filter_0`       | `0x40000000` | TDM stream filter (`hdl/axi_tdm_filter.vhd`) | control regs | status (reg3)     |
| `axi_dma_0`              | `0x40010000` | AXI DMA, simple mode (no SG)           | descriptors  | see Xilinx docs   |
| `axi_processing_ch1_0`   | `0x40001000` | ch1's chain (`hdl/axi_processing_ch1.vhd`) | input sample | processed output |
| `axi_processing_ch2_0`   | `0x40002000` | ch2's chain (`hdl/axi_processing_ch2.vhd`) | input sample | processed output |

`axi_processing_ch1.vhd` and `axi_processing_ch2.vhd` are deliberately
separate files/entities (not one module instantiated twice), so each
channel's architecture can diverge and be compared independently rather
than always running identical processing. Both currently implement the
same single-pole IIR low-pass filter: `y[n] = y[n-1] + (x[n] - y[n-1]) >>
SHIFT` (`SHIFT` generic, default 4 -- alpha = 1/16, no multiplier needed
since it's a power-of-two shift) -- edit one file alone to give that
channel a different architecture. The firmware
(`vitis/marathon/app/lwip_comm_client_raw.c`) writes each channel's raw
sample to its chain's reg0 and reads the processed result back from reg3,
synchronously, per sample -- see the `AXI_CH1_BASE`/`AXI_CH2_BASE`
`#define`s there, which must stay in sync with the `assign_bd_address`
calls in `bd_CoraZ7_Eth.tcl` if either ever changes.
