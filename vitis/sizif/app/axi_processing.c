#include "xil_io.h"
#include "axi_processing.h"

/* axi_processing_ch1_0/axi_processing_ch2_0 -- each channel's own
   processing chain, deliberately separate VHDL entities (not one module
   instantiated twice) so their architectures can diverge and be compared
   independently -- see vivado/sizif/hdl/axi_processing_ch1.vhd and
   axi_processing_ch2.vhd. Addresses match the assign_bd_address calls in
   vivado/sizif/bd_CoraZ7_Eth.tcl. Register layout mirrors axi_fir_0's:
   reg0 (offset 0x0, write) takes the new input sample, reg3 (offset
   0xC, read) returns the processed result (routed through my_axi's
   "fir_result" read-back port). */
#define AXI_CH1_BASE     0x40001000u
#define AXI_CH2_BASE     0x40002000u
#define AXI_PROC_REG_IN  0x0u
#define AXI_PROC_REG_OUT 0xCu

void axi_process_sample(packet_data_t *entry)
{
    /* Synchronous: no polling needed. The filter's internal latency (a
       couple of AXI clocks) is negligible next to one AXI4-Lite round
       trip, so by the time the read below is issued the result is
       already settled. */
    Xil_Out32(AXI_CH1_BASE + AXI_PROC_REG_IN, (u32)entry->ch1);
    Xil_Out32(AXI_CH2_BASE + AXI_PROC_REG_IN, (u32)entry->ch2);
    entry->ch1 = (uint16_t)(Xil_In32(AXI_CH1_BASE + AXI_PROC_REG_OUT) & 0xFFFFu);
    entry->ch2 = (uint16_t)(Xil_In32(AXI_CH2_BASE + AXI_PROC_REG_OUT) & 0xFFFFu);
}
