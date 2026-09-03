#include "xil_io.h"
#include "axi_processing.h"

/* Separate ch1/ch2 VHDL entities on purpose so their architectures can
   diverge (vivado/marathon/hdl/axi_processing_ch1/2.vhd). Addresses match
   assign_bd_address in bd_CoraZ7_Eth.tcl. reg0 (write) = input sample,
   reg3 (read) = processed result. */
#define AXI_CH1_BASE     0x40001000u
#define AXI_CH2_BASE     0x40002000u
#define AXI_PROC_REG_IN  0x0u
#define AXI_PROC_REG_OUT 0xCu

void axi_process_sample(packet_data_t *entry)
{
    /* No polling: filter latency (~2 AXI clocks) is negligible next to
       one AXI4-Lite round trip, so the result is already settled below. */
    Xil_Out32(AXI_CH1_BASE + AXI_PROC_REG_IN, (u32)entry->ch1);
    Xil_Out32(AXI_CH2_BASE + AXI_PROC_REG_IN, (u32)entry->ch2);
    entry->ch1 = (uint16_t)(Xil_In32(AXI_CH1_BASE + AXI_PROC_REG_OUT) & 0xFFFFu);
    entry->ch2 = (uint16_t)(Xil_In32(AXI_CH2_BASE + AXI_PROC_REG_OUT) & 0xFFFFu);
}
