#include "xil_io.h"
#include "axi_processing.h"
#include "board_config.h"

/* Separate ch1/ch2 VHDL entities on purpose so their architectures can
   diverge (vivado/marathon/hdl/axi_processing_ch1/2.vhd). Base addresses in
   board_config.h. reg0 (write) = input sample,
   reg3 (read) = processed result. */
#define AXI_PROC_REG_IN  0x0u
#define AXI_PROC_REG_OUT 0xCu

void axi_process_sample(packet_data_t *entry)
{
    /* No polling: filter latency (~2 AXI clocks) is negligible next to
       one AXI4-Lite round trip, so the result is already settled below. */
    Xil_Out32(AXI_CH1_BASE + AXI_PROC_REG_IN, (u32)entry->ch1);
    Xil_Out32(AXI_CH2_BASE + AXI_PROC_REG_IN, (u32)entry->ch2);
    entry->ch1 = Xil_In32(AXI_CH1_BASE + AXI_PROC_REG_OUT);
    entry->ch2 = Xil_In32(AXI_CH2_BASE + AXI_PROC_REG_OUT);
}
