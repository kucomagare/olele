#include "lwip/tcp.h"
#include "lwip/ip_addr.h"
#include <string.h>
#include <stdint.h>
#include "sleep.h"
#include "packet_format.h"
#include "comm_log.h"
#include "rx_ring.h"
#include "axi_processing.h"
#include "mono_clock.h"

/* -- globals -- */

struct tcp_pcb *client_pcb = NULL;
static int connected = 0;

/* Forward declarations */
static void tcp_client_start(void);
static err_t tcp_client_connected(void *arg, struct tcp_pcb *tpcb, err_t err);
static err_t tcp_client_recv(void *arg, struct tcp_pcb *tpcb, struct pbuf *p, err_t err);
static void tcp_client_error(void *arg, err_t err);
static void tcp_client_resync(const char *why);
static err_t tcp_client_sent(void *arg, struct tcp_pcb *tpcb, u16_t len);
static void tcp_client_send(struct tcp_pcb *tpcb, uint16_t type, uint16_t length, uint8_t *payload_bytes);

/* -- throughput statistics -- */

uint32_t packets_rx = 0;
uint32_t packets_tx = 0;
uint32_t samples_rx = 0;
uint32_t samples_tx = 0;
uint32_t bytes_rx   = 0;
uint32_t bytes_tx   = 0;

#define MAX_PAYLOAD_SAMPLES 2000

/* -- start / restart TCP client -- */

/* Minimum spacing between connect attempts. Without it, tcp_client_error()
 * re-enters tcp_client_start() as fast as the loop spins -- measured
 * ~45,000 attempts/s, exhausting lwIP's PCB pool (MEMP_NUM_TCP_PCB=32) and
 * leaving the link down permanently. Was masked until comm_log_flush()'s
 * blocking UART writes stopped incidentally throttling reconnects. */
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
            /* Too soon; comm_process() retries once backoff expires. */
            connect_pending = 1;
            return;
        }
        next_connect_ms = now + RECONNECT_BACKOFF_MS;
        connect_pending = 0;
    }

    IP4_ADDR(&server_ip, 192,168,1,100);   // PC IP

    client_pcb = tcp_new();
    if (!client_pcb) {
        comm_log("[E] pcb alloc failed\r\n");
        return;
    }

    tcp_arg(client_pcb, NULL);
    tcp_recv(client_pcb, tcp_client_recv);
    tcp_err(client_pcb, tcp_client_error);
    tcp_sent(client_pcb, tcp_client_sent);

    /* Latency-sensitive echo: Nagle would hold each partial-segment packet
       until ACKed, serializing the stream into one packet per round trip. */
    tcp_nagle_disable(client_pcb);

    connected = 0;

    comm_log("[N] connecting\r\n");

    err_t err = tcp_connect(client_pcb, &server_ip, 5001, tcp_client_connected);
    if (err != ERR_OK) {
        comm_log("[E] connect failed %d\r\n", err);
    }
}

/* -- thread entry (called once from main) -- */

void lwip_comm_client_thread(void *arg)
{
    comm_log("[N] client start\r\n");
    tcp_client_start();
}

/* -- connected callback -- */

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

/* -- error callback (auto-reconnect) -- */

static void tcp_client_error(void *arg, err_t err)
{
    comm_log("[E] tcp %d, reconn\r\n", err);
    connected = 0;
    client_pcb = NULL;
    tcp_client_start();
}

/* -- sent callback (optional) -- */

static err_t tcp_client_sent(void *arg, struct tcp_pcb *tpcb, u16_t len)
{
    return ERR_OK;
}

/* -- resync (framing lost / unrecoverable backlog) -- */

/* Tear down and reconnect, discarding the ring -- the only correct response
   to lost framing. No sync marker in the stream: once read position is off
   by one byte, every header is garbage (limping on reliably livelocked
   under load). Reconnect resyncs because the PC relay always starts a new
   connection on a packet boundary. */
static void tcp_client_resync(const char *why)
{
    comm_log("[E] %s, resync\r\n", why);
    connected = 0;
    tcp_client_start();
}

/* -- receive callback (fast: only push into ring) -- */

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

    /* Check up front -- pushing part then bailing would itself fragment
       the ring. A full ring means the sender durably outruns us, so
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

/* Reverse byte order of every field per the width table in packet_format.h.
   Self-inverse: same call converts wire<->host either direction. */
static void swap_be_fields(uint8_t *record, uint16_t type)
{
    uint32_t n_fields;
    const uint8_t *field_bytes = packet_field_bytes(type, &n_fields);
    uint32_t off = 0;

    for (uint32_t i = 0; i < n_fields; i++) {
        uint8_t w = field_bytes[i];
        for (uint8_t a = 0, b = (uint8_t)(w - 1); a < b; a++, b--) {
            uint8_t tmp = record[off + a];
            record[off + a] = record[off + b];
            record[off + b] = tmp;
        }
        off += w;
    }
}

/* Bounds the static buffers below, sized off the biggest packet type ("data"). */
#define MAX_PAYLOAD_BYTES (MAX_PAYLOAD_SAMPLES * sizeof(packet_data_t))

/* -- processing function (called from main loop) -- */
void comm_process(void)
{
    /* Once-per-loop entry point: picks up a deferred reconnect retry that
       tcp_client_start() left pending inside the backoff window. */
    if (connect_pending && mono_now_ms() >= next_connect_ms)
        tcp_client_start();

    while (1)
    {
        /* Need at least header */
        if (rx_ring_used() < 4)
            return;

        /* Peek header without consuming it */
        uint8_t hdr[4];
        rx_ring_peek(0, hdr, 4);

        uint16_t type   = ((uint16_t)hdr[0] << 8) | hdr[1];
        uint16_t length = ((uint16_t)hdr[2] << 8) | hdr[3];

        uint32_t record_size = packet_record_size(type);
        if (record_size == 0) {
            /* Unknown type = framing already lost; can't skip an
               unknown-size body, so reconnect instead of limping on. */
            comm_log("[E] bad type %u\r\n", type);
            tcp_client_resync("framing lost");
            return;
        }

        uint32_t body_bytes = length * record_size;
        uint32_t total_needed = 4 + body_bytes;

        /* Not enough data yet */
        if (rx_ring_used() < total_needed)
            return;

        /* Backpressure: don't consume until we can forward it -- popping
           then dropping on a failed tcp_write() would lose data. */
        if (client_pcb && connected) {
            uint16_t need = 4 + (uint16_t)body_bytes;
            if (tcp_sndbuf(client_pcb) < need)
                return;
        }

        /* Now we can safely consume header + body */
        rx_ring_advance(4);

        static uint8_t payload_buf[MAX_PAYLOAD_BYTES];

        if (length > MAX_PAYLOAD_SAMPLES) {
            /* Framing-loss symptom, not a real oversized packet -- sender
               is capped well below this by TCP_SND_BUF. */
            comm_log("[E] len %u too big\r\n", length);
            tcp_client_resync("framing lost");
            return;
        }

        /* Bulk-copy the body out (at most one wrap), then byte-swap each
           record in place, instead of popping byte-by-byte. */
        rx_ring_peek(0, payload_buf, body_bytes);
        rx_ring_advance(body_bytes);
        for (uint32_t i = 0; i < length; i++)
            swap_be_fields(payload_buf + i * record_size, type);

        packets_rx++;
        samples_rx += length;
        bytes_rx   += total_needed;

        /* "config" is consumed but ignored (placeholder). "data" gets
           each channel run through its AXI-Lite chain (axi_processing.c). */
        if (type == PACKET_TYPE_DATA) {
            packet_data_t *entries = (packet_data_t *)payload_buf;
            for (uint32_t i = 0; i < length; i++)
                axi_process_sample(&entries[i]);

            if (client_pcb && connected)
                tcp_client_send(client_pcb, type, length, payload_buf);
        }
    }
}


/* -- send response -- */

static void tcp_client_send(struct tcp_pcb *tpcb, uint16_t type, uint16_t length, uint8_t *payload_bytes)
{
    static uint8_t buf[4 + MAX_PAYLOAD_BYTES];
    uint32_t record_size = packet_record_size(type);
    uint32_t body_bytes = length * record_size;
    uint16_t total_bytes = (uint16_t)(4 + body_bytes);

    buf[0] = (type >> 8) & 0xFF;
    buf[1] = (type     ) & 0xFF;
    buf[2] = (length >> 8) & 0xFF;
    buf[3] = (length     ) & 0xFF;

    /* payload_bytes is host-order; swap back to wire order here
       (swap_be_fields is its own inverse). */
    memcpy(buf + 4, payload_bytes, body_bytes);
    for (uint32_t i = 0; i < length; i++)
        swap_be_fields(buf + 4 + i * record_size, type);

    err_t err = tcp_write(tpcb, buf, total_bytes, TCP_WRITE_FLAG_COPY);
    if (err == ERR_OK) {
        /* tcp_write() only queues -- without this the segment waits for
           lwIP's timer/ACK, pinning throughput at ~33 pkt/s. */
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
