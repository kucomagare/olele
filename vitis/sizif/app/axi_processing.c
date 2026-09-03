#include "xil_io.h"
#include "axi_processing.h"

/* ch1/ch2 are separate VHDL entities on purpose (not one module reused)
   so their architectures can diverge -- vivado/sizif/hdl/axi_processing_ch{1,2}.vhd.
   reg0 (write) = input sample, reg3 (read) = result via my_axi's fir_result port. */
#define AXI_CH1_BASE     0x40001000u
#define AXI_CH2_BASE     0x40002000u
#define AXI_PROC_REG_IN  0x0u
#define AXI_PROC_REG_OUT 0xCu

void axi_process_sample(packet_data_t *entry)
{
    /* No polling: filter latency is negligible next to one AXI4-Lite round trip. */
    Xil_Out32(AXI_CH1_BASE + AXI_PROC_REG_IN, (u32)entry->ch1);
    Xil_Out32(AXI_CH2_BASE + AXI_PROC_REG_IN, (u32)entry->ch2);
    entry->ch1 = (uint16_t)(Xil_In32(AXI_CH1_BASE + AXI_PROC_REG_OUT) & 0xFFFFu);
    entry->ch2 = (uint16_t)(Xil_In32(AXI_CH2_BASE + AXI_PROC_REG_OUT) & 0xFFFFu);
}
