#ifndef COMM_PROCESS_H
#define COMM_PROCESS_H

/* What happens to a received packet: reassemble it from rx_ring, then
   DATA -> DMA path (dma_stream.c) or legacy AXI-Lite path (axi_processing.c),
   CONFIG -> filter registers + read-back reply. */

/* Packet reassembly + processing; called once per pass of main()'s loop,
   not from an lwIP callback. */
void comm_process(void);

/* Which processing path is live: 1 = DMA through the TDM stream filter,
   0 = the legacy per-sample AXI-Lite chains. Both are in the bitstream so
   they can be A/B'd on identical data. Cleared automatically if DMA
   initialisation fails. */
extern int comm_use_dma;

#endif /* COMM_PROCESS_H */
