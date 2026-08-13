# vivado/sizif

Hand-written Vivado sources for the "sizif" hardware version (Cora Z7-10,
PS7 + AXI interconnect + `axi_gpio` x N wired to an ADC + custom `axi_fir`
IP + `system_ila`). This is the only thing tracked in git for this
version — `build/` is regenerated locally, never committed.

```
cora_z7.tcl   regenerates the full Vivado project into ./build/
hdl/          fpga_top.v, my_axi.v, axi_fir.v
xdc/          Cora-Z7-10-Master.xdc (pin/clock constraints)
dcp/          axi_fir.dcp — pre-synthesized checkpoint for the custom axi_fir IP,
              required by cora_z7.tcl, not regenerable from the .v alone
build/        empty in git; the actual Vivado project lands here
build.sh      wrapper that runs cora_z7.tcl with the right paths
```

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
