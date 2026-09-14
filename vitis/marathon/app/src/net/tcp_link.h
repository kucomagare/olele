#ifndef TCP_LINK_H
#define TCP_LINK_H

#include <stdint.h>
#include "lwip/tcp.h"

/* The TCP connection to the PC relay (address in board_config.h): connect,
   reconnect with backoff, push received bytes into rx_ring, send packets.
   Knows nothing about what the packets mean -- that's comm_process.c. */

extern struct tcp_pcb *client_pcb;

/* Opens the first connection. Called once from main(). */
void lwip_comm_client_thread(void *arg);

/* Once per main-loop pass: retries a reconnect deferred by the backoff. */
void tcp_link_service(void);

/* Non-zero when connected and client_pcb is usable for sending. */
int tcp_link_up(void);

/* Framing is lost: drop the connection, discard the ring, reconnect. */
void tcp_client_resync(const char *why);

/* Build the 4-byte header, swap host-order records to wire order, send. */
void tcp_client_send(struct tcp_pcb *tpcb, uint16_t type, uint16_t length, uint8_t *payload_bytes);

/* Send a block that is already complete and in wire order (DMA path). */
void tcp_client_send_raw(struct tcp_pcb *tpcb, uint8_t *wire, uint32_t nbytes,
                         uint16_t length);

#endif /* TCP_LINK_H */
