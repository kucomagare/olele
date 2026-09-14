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
   not from an lwIP callback. */
void comm_process(void);

/* Which processing path is live: 1 = DMA through the TDM stream filter,
   0 = the legacy per-sample AXI-Lite chains. Both are in the bitstream so
   they can be A/B'd on identical data. Cleared automatically if DMA
   initialisation fails. */
#include "packet_format.h"

extern int comm_use_dma;
extern uint32_t comm_resyncs;

/* Push one metrics packet to the PC. Called once per second from main.c's
   stats block; no-op while disconnected. */
void comm_send_metrics(const packet_metrics_t *m);

/* Drain the per-packet service-latency accumulator: min/mean/max in
   microseconds since the last call, then reset. Called once per second from
   main.c's stats block. All zero means no packets were serviced in the
   window. */
void comm_latency_take(uint32_t *min_us, uint32_t *mean_us, uint32_t *max_us);

/* Throughput statistics (used by main.c) */
extern uint32_t packets_rx;
extern uint32_t packets_tx;
extern uint32_t samples_rx;
extern uint32_t samples_tx;
extern uint32_t bytes_rx;
extern uint32_t bytes_tx;

#endif
