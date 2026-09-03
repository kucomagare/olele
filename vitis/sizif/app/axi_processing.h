#ifndef AXI_PROCESSING_H
#define AXI_PROCESSING_H

#include "packet_format.h"

/* Runs one "data" record's ch1/ch2 through the AXI-Lite processing
   peripherals in place (ts untouched). Sync write-reg0/read-reg3, no polling. */
void axi_process_sample(packet_data_t *entry);

#endif /* AXI_PROCESSING_H */
