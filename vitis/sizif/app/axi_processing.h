#ifndef AXI_PROCESSING_H
#define AXI_PROCESSING_H

#include "packet_format.h"

/* Runs one "data" record's ch1/ch2 through their respective AXI-Lite
   processing peripherals in the PL (axi_processing_ch1_0/ch2_0 -- see
   vivado/sizif/hdl/axi_processing_ch1.vhd and ch2.vhd), in place. ts is
   left untouched. Synchronous: write reg0, read reg3 back -- no polling
   needed, see the .c file for why. */
void axi_process_sample(packet_data_t *entry);

#endif /* AXI_PROCESSING_H */
