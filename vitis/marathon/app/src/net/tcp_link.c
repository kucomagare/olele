#include "lwip/tcp.h"
#include "lwip/ip_addr.h"
#include <string.h>
#include <stdint.h>
#include "tcp_link.h"
#include "packet_codec.h"
#include "comm_stats.h"
#include "comm_log.h"
#include "rx_ring.h"
#include "mono_clock.h"
#include "board_config.h"

struct tcp_pcb *client_pcb = NULL;
static int connected = 0;

static void tcp_client_start(void);
static err_t tcp_client_connected(void *arg, struct tcp_pcb *tpcb, err_t err);
static err_t tcp_client_recv(void *arg, struct tcp_pcb *tpcb, struct pbuf *p, err_t err);
static void tcp_client_error(void *arg, err_t err);
static err_t tcp_client_sent(void *arg, struct tcp_pcb *tpcb, u16_t len);

/* DO NOT remove this backoff. Without it tcp_client_error() re-enters
   tcp_client_start() as fast as the main loop spins -- measured 2026-08-17
   at ~45,000 connects/s, which exhausts lwIP's PCB pool (MEMP_NUM_TCP_PCB =
   32) and leaves the link permanently down. (Blocking UART writes used to
   accidentally throttle this to ~160/s; bounding comm_log_flush() removed
   that brake and exposed the need for a deliberate one.) */
#define RECONNECT_BACKOFF_MS 250

static uint64_t next_connect_ms = 0;
static int      connect_pending = 0;

static void tcp_client_start(void)
{
    ip_addr_t server_ip;

    if (client_pcb != NULL) {
        tcp_close(client_pcb);
        client_pcb = NULL;
    }

    rx_ring_reset();
    connected = 0;

    {
        uint64_t now = mono_now_ms();
        if (now < next_connect_ms) {
            /* Too soon -- comm_process() retries once backoff expires;
               client_pcb stays NULL so the send path idles meanwhile. */
            connect_pending = 1;
            return;
        }
        next_connect_ms = now + RECONNECT_BACKOFF_MS;
        connect_pending = 0;
    }

    PC_IP_ADDR(&server_ip);

    client_pcb = tcp_new();
    if (!client_pcb) {
        comm_log("[E] pcb alloc failed\r\n");
        return;
    }

    tcp_arg(client_pcb, NULL);
    tcp_recv(client_pcb, tcp_client_recv);
    tcp_err(client_pcb, tcp_client_error);
    tcp_sent(client_pcb, tcp_client_sent);

    /* Latency-sensitive echo, not bulk transfer: each packet ends in a
       partial segment Nagle would hold back until the previous data is
       ACKed, serializing the stream into one packet per round trip. */
    tcp_nagle_disable(client_pcb);

    connected = 0;

    comm_log("[N] connecting\r\n");

    err_t err = tcp_connect(client_pcb, &server_ip, PC_TCP_PORT, tcp_client_connected);
    if (err != ERR_OK) {
        comm_log("[E] connect failed %d\r\n", err);
    }
}

void lwip_comm_client_thread(void *arg)
{
    comm_log("[N] client start\r\n");
    tcp_client_start();
}

void tcp_link_service(void)
{
    if (connect_pending && mono_now_ms() >= next_connect_ms)
        tcp_client_start();
}

int tcp_link_up(void)
{
    return client_pcb && connected;
}

static err_t tcp_client_connected(void *arg, struct tcp_pcb *tpcb, err_t err)
{
    if (err == ERR_OK) {
        connected = 1;
        comm_log("[N] connected\r\n");
        return ERR_OK;
    }

    comm_log("[E] connect err %d\r\n", err);
    return ERR_ABRT;
}

/* Auto-reconnect on any TCP error. */
static void tcp_client_error(void *arg, err_t err)
{
    comm_log("[E] tcp %d, reconn\r\n", err);
    connected = 0;
    client_pcb = NULL;
    tcp_client_start();
}

static err_t tcp_client_sent(void *arg, struct tcp_pcb *tpcb, u16_t len)
{
    return ERR_OK;
}

/* Tear down and reconnect, discarding the ring -- the only correct response
   to lost framing. The stream is length-prefixed with no sync marker, so
   once the read position is off by a byte there's no in-band recovery
   (every header read is garbage, and a plausible type/length can swallow
   kilobytes before failing again -- a prior "limp on" approach reliably
   deadlocked under load). A reconnect is guaranteed to resync because the
   PC relay is packet-aware and always forwards on a packet boundary.
   tcp_client_start() closes the old pcb and resets the ring. */
void tcp_client_resync(const char *why)
{
    comm_resyncs++;
    comm_log("[E] %s, resync\r\n", why);
    connected = 0;
    tcp_client_start();
}

/* Fast path: only pushes into the ring, no parsing. */
static err_t tcp_client_recv(void *arg, struct tcp_pcb *tpcb,
                             struct pbuf *p, err_t err)
{
    if (!p || err != ERR_OK) {
        comm_log("[N] closed, reconn\r\n");
        connected = 0;
        tcp_close(tpcb);
        client_pcb = NULL;
        tcp_client_start();
        return ERR_OK;
    }

    /* Check capacity up front -- pushing part of the chain then bailing
       would itself leave a fragment. A full ring means comm_process()
       couldn't drain for a long time (sender durably outrunning us), so
       dropping the link is the only non-corrupting way out. */
    if (rx_ring_free() < p->tot_len) {
        pbuf_free(p);
        tcp_client_resync("ring full");
        return ERR_OK;
    }

    struct pbuf *q = p;
    while (q != NULL) {
        rx_ring_push((const uint8_t *)q->payload, q->len);
        q = q->next;
    }

    u16_t tot_len = p->tot_len;
    pbuf_free(p);
    tcp_recved(tpcb, tot_len);

    return ERR_OK;
}

/* DMA path's counterpart to tcp_client_send() -- sends an already-complete
   wire-order block as-is, nothing to build or swap.
 *
 * TCP_WRITE_FLAG_COPY stays on: dropping it would be genuinely zero-copy,
 * but lwIP then holds a reference until the PC ACKs, and with only 2 DMA
 * buffers a retransmit/stall would let the next transfer overwrite data
 * still in flight. Zero-copy needs DMA_NBUF raised to 4 first. */
void tcp_client_send_raw(struct tcp_pcb *tpcb, uint8_t *wire, uint32_t nbytes,
                                uint16_t length)
{
    err_t err = tcp_write(tpcb, wire, (u16_t)nbytes, TCP_WRITE_FLAG_COPY);
    if (err == ERR_OK) {
        err = tcp_output(tpcb);
        if (err != ERR_OK)
            comm_log("[E] output %d\r\n", err);

        packets_tx++;
        samples_tx += length;
        bytes_tx   += nbytes;
    } else {
        comm_log("[E] write %d\r\n", err);
    }
}

void tcp_client_send(struct tcp_pcb *tpcb, uint16_t type, uint16_t length, uint8_t *payload_bytes)
{
    static uint8_t buf[4 + MAX_PAYLOAD_BYTES];
    uint32_t record_size = packet_record_size(type);
    uint32_t body_bytes = length * record_size;
    uint16_t total_bytes = (uint16_t)(4 + body_bytes);

    buf[0] = (type >> 8) & 0xFF;
    buf[1] = (type     ) & 0xFF;
    buf[2] = (length >> 8) & 0xFF;
    buf[3] = (length     ) & 0xFF;

    /* payload_bytes is host-order; swap back to wire order on the way out
       (swap_be_fields is its own inverse). */
    memcpy(buf + 4, payload_bytes, body_bytes);
    for (uint32_t i = 0; i < length; i++)
        swap_be_fields(buf + 4 + i * record_size, type);

    err_t err = tcp_write(tpcb, buf, total_bytes, TCP_WRITE_FLAG_COPY);
    if (err == ERR_OK) {
        /* DO NOT remove: tcp_write() only queues. Without an explicit
           tcp_output() here the segment waits for lwIP's own timer/ACK
           trigger, which measured out at ~33 pkt/s -- one packet per round
           trip. (A prior comment called that batching intentional; it was
           the bug.) */
        err = tcp_output(tpcb);
        if (err != ERR_OK)
            comm_log("[E] output %d\r\n", err);

        packets_tx++;
        samples_tx += length;
        bytes_tx   += total_bytes;

    } else {
        comm_log("[E] write %d\r\n", err);
    }
}
