# vivado/sizif

Hand-written Vivado sources for the "sizif" hardware version (Cora Z7-10,
PS7 + AXI interconnect + `axi_gpio` x N wired to an ADC + custom `axi_fir`
IP + `system_ila`). This is the only thing tracked in git for this
version — `build/` is regenerated locally, never committed.

```
cora_z7.tcl        regenerates the full Vivado project into ./build/ --
                    project creation, source imports, wrapper generation;
                    sources bd_CoraZ7_Eth.tcl for the block design itself
bd_CoraZ7_Eth.tcl   the block design (proc cr_bd_CoraZ7_Eth) -- split into
                    its own file so re-exporting from the GUI after a
                    change is a straight overwrite, no manual merge
hdl/                fpga_top.v, my_axi.v, axi_fir.v
xdc/                Cora-Z7-10-Master.xdc (pin/clock constraints)
dcp/                axi_fir.dcp — pre-synthesized checkpoint for the custom axi_fir IP,
                    required by cora_z7.tcl, not regenerable from the .v alone
build/              empty in git; the actual Vivado project lands here
build.sh            wrapper that runs cora_z7.tcl with the right paths
```

## Changing the block design

Edit it in the Vivado GUI (`vivado build/tcp_client/tcp_client.xpr`, open
the block design), then sync the change back to tracked source:

1. Validate Design, then save the block design (Ctrl+S).
2. **File → Export → Export Block Design...**, pointed at this exact
   path: `vivado/sizif/bd_CoraZ7_Eth.tcl` (overwrite it).
3. `./clean.sh && ./build.sh` to confirm the updated file reproduces the
   design from tracked source alone.

`cora_z7.tcl` itself doesn't need touching — it just `source`s this file
and calls `cr_bd_CoraZ7_Eth`; wrapper regeneration happens automatically
right after. The one thing this doesn't cover: changes to the actual RTL
*inside* `fpga_top.v`/`my_axi.v`/`axi_fir.v` (not just how those modules
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
`build/tcp_client/CoraZ7_Eth_wrapper.xsa`, which is what `vitis/sizif/`
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
