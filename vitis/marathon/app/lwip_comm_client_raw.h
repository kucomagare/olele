#ifndef LWIP_COMM_CLIENT_RAW_H
#define LWIP_COMM_CLIENT_RAW_H

#include <stdint.h>   /* uint32_t below -- don't rely on lwip/tcp.h for it */
#include "lwip/tcp.h"
#include "comm_log.h"

/* PCB pointer */
extern struct tcp_pcb *client_pcb;

/* Thread entry */
void lwip_comm_client_thread(void *arg);

/* Packet reassembly + processing; called once per pass of main()'s loop,
   not from an lwIP callback. Was previously missing from this header, so
   main.c called it via an implicit declaration. */
void comm_process(void);

/* Which processing path is live: 1 = DMA through the TDM stream filter,
   0 = the legacy per-sample AXI-Lite chains. Both are in the bitstream so
   they can be A/B'd on identical data. Cleared automatically if DMA
   initialisation fails. */
extern int comm_use_dma;

/* Throughput statistics (used by main.c) */
extern uint32_t packets_rx;
extern uint32_t packets_tx;
extern uint32_t samples_rx;
extern uint32_t samples_tx;
extern uint32_t bytes_rx;
extern uint32_t bytes_tx;

#endif
