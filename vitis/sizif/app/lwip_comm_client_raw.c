#include "lwip/tcp.h"
#include "lwip/ip_addr.h"
#include <string.h>
#include <stdint.h>
#include "sleep.h"
#include "packet_format.h"
#include "comm_log.h"
#include "rx_ring.h"
#include "axi_processing.h"

/* ============================================================
   GLOBALS
   ============================================================ */

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

/* ============================================================
   THROUGHPUT STATISTICS
   ============================================================ */

uint32_t packets_rx = 0;
uint32_t packets_tx = 0;
uint32_t samples_rx = 0;
uint32_t samples_tx = 0;
uint32_t bytes_rx   = 0;
uint32_t bytes_tx   = 0;

#define MAX_PAYLOAD_SAMPLES 2000

/* ============================================================
   START / RESTART TCP CLIENT
   ============================================================ */

static void tcp_client_start(void)
{
    ip_addr_t server_ip;

    if (client_pcb != NULL) {
        tcp_close(client_pcb);
        client_pcb = NULL;
    }

    rx_ring_reset();

    IP4_ADDR(&server_ip, 192,168,1,100);   // PC IP

    client_pcb = tcp_new();
    if (!client_pcb) {
        comm_log("\r[PCB] Failed to create PCB\r\n");
        return;
    }

    tcp_arg(client_pcb, NULL);
    tcp_recv(client_pcb, tcp_client_recv);
    tcp_err(client_pcb, tcp_client_error);
    tcp_sent(client_pcb, tcp_client_sent);

    /* This is a latency-sensitive echo, not a bulk transfer: each packet
       is 4 + 6*N bytes, so it ends in a partial segment that Nagle would
       otherwise hold back until the previous data is ACKed -- serializing
       the stream into one packet per round trip. */
    tcp_nagle_disable(client_pcb);

    connected = 0;

    comm_log("\r[PCB] Connecting RAW TCP...\r\n");

    err_t err = tcp_connect(client_pcb, &server_ip, 5001, tcp_client_connected);
    if (err != ERR_OK) {
        comm_log("\r[PCB] tcp_connect failed: %d\r\n", err);
    }
}

/* ============================================================
   THREAD ENTRY (called once from main)
   ============================================================ */

void lwip_comm_client_thread(void *arg)
{
    comm_log("\r[PCB] lwip_comm_client_thread STARTED\r\n");
    tcp_client_start();
}

/* ============================================================
   CONNECTED CALLBACK
   ============================================================ */

static err_t tcp_client_connected(void *arg, struct tcp_pcb *tpcb, err_t err)
{
    if (err == ERR_OK) {
        connected = 1;
        comm_log("\r[PCB] RAW TCP connected!\r\n");
        return ERR_OK;
    }

    comm_log("\r[PCB] Connect error: %d\r\n", err);
    return ERR_ABRT;
}

/* ============================================================
   ERROR CALLBACK (AUTO‑RECONNECT)
   ============================================================ */

static void tcp_client_error(void *arg, err_t err)
{
    comm_log("\r[PCB] TCP error: %d, reconnecting...\r\n", err);
    connected = 0;
    client_pcb = NULL;
    tcp_client_start();
}

/* ============================================================
   SENT CALLBACK (optional)
   ============================================================ */

static err_t tcp_client_sent(void *arg, struct tcp_pcb *tpcb, u16_t len)
{
    return ERR_OK;
}

/* ============================================================
   RESYNC (framing lost / unrecoverable backlog)
   ============================================================ */

/* Tear down the connection and start a fresh one, discarding whatever is
   in the ring.

   This is the only correct response to a lost or hopeless framing state.
   The stream is length-prefixed with no sync marker, so once the read
   position is off by even one byte there is no way to recover in-band --
   every subsequent 4-byte header read is garbage, and a plausible-looking
   type/length pair can swallow kilobytes of valid data before failing
   again. The previous code tried to limp on (drop the oldest bytes on
   overflow, skip 4 bytes on an unknown type) and reliably ended up in an
   unrecoverable loop under load.

   A reconnect is cheap and *guaranteed* to resync, because the PC-side
   relay is packet-aware: it reassembles each packet (header + full body)
   before forwarding, so a new connection always begins on a packet
   boundary. tcp_client_start() closes the old pcb and calls
   rx_ring_reset() for us. */
static void tcp_client_resync(const char *why)
{
    comm_log("\r[PCB] %s -- reconnecting to resync\r\n", why);
    connected = 0;
    tcp_client_start();
}

/* ============================================================
   RECEIVE CALLBACK (FAST: only push into ring)
   ============================================================ */

static err_t tcp_client_recv(void *arg, struct tcp_pcb *tpcb,
                             struct pbuf *p, err_t err)
{
    if (!p || err != ERR_OK) {
        comm_log("\r[PCB] Connection closed, reconnecting...\r\n");
        connected = 0;
        tcp_close(tpcb);
        client_pcb = NULL;
        tcp_client_start();
        return ERR_OK;
    }

    /* Test the whole chain up front: pushing part of it and then bailing
       would itself leave a fragment in the ring. A full ring means
       comm_process() has been unable to drain for a long time (its
       backpressure check is holding because our own TX is backed up),
       i.e. the sender is durably outrunning us -- dropping the link is
       both the honest signal and the only non-corrupting way out. */
    if (rx_ring_free() < p->tot_len) {
        pbuf_free(p);
        tcp_client_resync("RX ring full, sender outrunning us");
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

/* Reverse the byte order of every field in one record, in place, per the
   field-width table in packet_format.h -- generalizes the old fixed
   2-byte-per-sample swap so widths can change (16/32 bit) without this
   code changing, only packet_format.json + a rebuild. Self-inverse, so
   the same call converts wire (big-endian) -> host and host -> wire. */
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

/* Largest record size we accept, sized off the biggest packet type known
   to packet_format.h -- currently "data" (ts+ch1+ch2). Bounds the static
   buffers below regardless of which type actually arrives. */
#define MAX_PAYLOAD_BYTES (MAX_PAYLOAD_SAMPLES * sizeof(packet_data_t))

/* ============================================================
   PROCESSING FUNCTION (called from main loop)
   ============================================================ */
void comm_process(void)
{
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
            /* An unknown type means we are not actually looking at a
               header -- framing is already lost. We can't skip the body
               (its size is unknown), and sliding forward a byte or four at
               a time through a multi-KB packet stream does not resync in
               practice. Reconnect instead. */
            comm_log("\r[PCB] unknown packet type %u\r\n", type);
            tcp_client_resync("framing lost");
            return;
        }

        uint32_t body_bytes = length * record_size;
        uint32_t total_needed = 4 + body_bytes;

        /* Not enough data yet */
        if (rx_ring_used() < total_needed)
            return;

        /* Backpressure: don't consume this packet until we can actually
           forward it. Popping it and dropping it on a failed tcp_write()
           would silently lose data instead of just waiting for the peer's
           window to free up. */
        if (client_pcb && connected) {
            uint16_t need = 4 + (uint16_t)body_bytes;
            if (tcp_sndbuf(client_pcb) < need)
                return;
        }

        /* Now we can safely consume header + body */
        rx_ring_advance(4);

        static uint8_t payload_buf[MAX_PAYLOAD_BYTES];

        if (length > MAX_PAYLOAD_SAMPLES) {
            /* Also a framing-loss symptom rather than a real oversized
               packet: the sender is capped well below this by
               TCP_SND_BUF (see the backpressure check above), so a length
               this large means we parsed a garbage header. */
            comm_log("\r[PCB] payload too large (%u records)\r\n", length);
            tcp_client_resync("framing lost");
            return;
        }

        /* Bulk-copy the body out of the ring (at most one wrap), then
           byte-swap each record in place (network byte order: MSB first,
           matching tcp_client_send) instead of popping byte-by-byte. */
        rx_ring_peek(0, payload_buf, body_bytes);
        rx_ring_advance(body_bytes);
        for (uint32_t i = 0; i < length; i++)
            swap_be_fields(payload_buf + i * record_size, type);

        /* Stats */
        packets_rx++;
        samples_rx += length;
        bytes_rx   += total_needed;

        /* Process. "config" packets are a placeholder for now -- consumed
           above but otherwise ignored (no processing, no echo). "data"
           packets get each channel run through its own AXI-Lite
           processing chain in the PL (see axi_processing.c). */
        if (type == PACKET_TYPE_DATA) {
            packet_data_t *entries = (packet_data_t *)payload_buf;
            for (uint32_t i = 0; i < length; i++)
                axi_process_sample(&entries[i]);

            if (client_pcb && connected)
                tcp_client_send(client_pcb, type, length, payload_buf);
        }
    }
}


/* ============================================================
   SEND RESPONSE
   ============================================================ */

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

    /* payload_bytes is host-order (already byte-swapped on the way in by
       comm_process); swap each record back to wire order (big-endian) on
       the way out -- swap_be_fields is its own inverse. */
    memcpy(buf + 4, payload_bytes, body_bytes);
    for (uint32_t i = 0; i < length; i++)
        swap_be_fields(buf + 4 + i * record_size, type);

    err_t err = tcp_write(tpcb, buf, total_bytes, TCP_WRITE_FLAG_COPY);
    if (err == ERR_OK) {
        /* Push it out now. tcp_write() only *queues* -- without this the
           segment leaves when lwIP next feels like it (fast timer, or an
           incoming ACK happening to trigger a flush), which turned the
           whole pipeline into one packet per round trip and pinned
           throughput at ~33 pkt/s no matter what else was tuned. The
           previous comment here ("let LWIP batch tcp_output() via timers")
           described that as intentional; it was the bug. */
        err = tcp_output(tpcb);
        if (err != ERR_OK)
            comm_log("\r[PCB] tcp_output failed: %d\r\n", err);

        packets_tx++;
        samples_tx += length;
        bytes_tx   += total_bytes;

    } else {
        comm_log("\r[PCB] tcp_write failed: %d\r\n", err);
    }
}
