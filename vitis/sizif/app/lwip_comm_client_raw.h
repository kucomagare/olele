#ifndef LWIP_COMM_CLIENT_RAW_H
#define LWIP_COMM_CLIENT_RAW_H

#include "lwip/tcp.h"
#include "comm_log.h"

/* PCB pointer */
extern struct tcp_pcb *client_pcb;

/* Thread entry */
void lwip_comm_client_thread(void *arg);

/* Throughput statistics (used by main.c) */
extern uint32_t packets_rx;
extern uint32_t packets_tx;
extern uint32_t samples_rx;
extern uint32_t samples_tx;
extern uint32_t bytes_rx;
extern uint32_t bytes_tx;

#endif
