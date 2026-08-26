#include "lwip/tcp.h"
#include "lwip/ip_addr.h"
#include <string.h>
#include <stdint.h>
#include "sleep.h"
#include "packet_format.h"
#include "comm_log.h"
#include "rx_ring.h"
#include "axi_processing.h"
#include "dma_stream.h"

/* Config packet ops -- see the "config" description in
   shared/marathon/packet_format.json, which is the source of truth. */
#define CONFIG_OP_READ  0u
#define CONFIG_OP_WRITE 1u
#include "mono_clock.h"

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
static void tcp_client_send_raw(struct tcp_pcb *tpcb, uint8_t *wire, uint32_t nbytes,
                                uint16_t length);

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

/* Which processing path is live.
 *
 * 1 = buffer-at-a-time DMA through the TDM stream filter (dma_stream.c).
 * 0 = the legacy per-sample AXI-Lite chains (axi_processing.c).
 *
 * Both peripherals are present in the bitstream deliberately, so the two
 * can be A/B'd against identical data without a rebuild. Cleared
 * automatically if dma_stream_init() fails, so a DMA problem degrades to a
 * working system rather than a dead one. */
int comm_use_dma = 1;

/* Cumulative, never reset: the metrics packet reports it as a running total
   so the PC can see both the rate (by differencing) and the lifetime count.
   A resync is the overload signal worth watching -- it means framing was
   lost badly enough to need dropping the connection. */
uint32_t comm_resyncs = 0;

/* Per-packet service latency: from a complete packet being available in the
   ring to its echo being handed to lwIP. Deliberately NOT end-to-end round
   trip -- that would be dominated by the PC's scheduler, the relay and TCP,
   which is precisely the noise this architecture does not control. This
   isolates the board, which is what the DMA conversion was actually meant to
   make deterministic.
 *
 * Accumulated here, drained once per second by comm_latency_take(). uint32 us
 * is good for ~71 minutes per sample, so overflow is not a concern; the sum is
 * 64-bit because a second's worth at 1400 pkt/s would otherwise wrap. */
static uint32_t lat_min_us = 0xFFFFFFFFu;
static uint32_t lat_max_us = 0;
static uint64_t lat_sum_us = 0;
static uint32_t lat_count  = 0;

/* Start time of the packet currently being serviced. One slot is enough: the
   AXI-Lite path finishes within a single call, and the DMA path is gated on
   dma_stream_busy() so only one transfer is ever in flight. */
static uint64_t lat_t0 = 0;

static void latency_record(uint64_t t0)
{
    if (t0 == 0)
        return;
    uint32_t dt = (uint32_t)(mono_now_us() - t0);
    if (dt < lat_min_us) lat_min_us = dt;
    if (dt > lat_max_us) lat_max_us = dt;
    lat_sum_us += dt;
    lat_count++;
}

void comm_latency_take(uint32_t *min_us, uint32_t *mean_us, uint32_t *max_us)
{
    *min_us  = (lat_count ? lat_min_us : 0);
    *max_us  = lat_max_us;
    *mean_us = (uint32_t)(lat_count ? (lat_sum_us / lat_count) : 0);
    lat_min_us = 0xFFFFFFFFu;
    lat_max_us = 0;
    lat_sum_us = 0;
    lat_count  = 0;
}
static int dma_init_done = 0;

/* ============================================================
   START / RESTART TCP CLIENT
   ============================================================ */

/* Minimum spacing between connection attempts.
 *
 * Without this, tcp_client_error() re-enters tcp_client_start()
 * immediately, so a failing link retries as fast as the main loop spins.
 * Measured 2026-08-17 at ~45,000 connect attempts per second, which
 * exhausts lwIP's PCB pool (MEMP_NUM_TCP_PCB = 32) and leaves the link
 * permanently down at 0 pkt/s.
 *
 * This was hidden until today: the blocking UART writes in comm_log_flush()
 * used to throttle reconnects to ~160/s purely as a side effect of being
 * slow. Bounding the log output (correctly) removed that accidental brake
 * and exposed the missing deliberate one. */
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
            /* Too soon -- comm_process() will retry once the backoff
               expires. Leaving client_pcb NULL keeps the send path idle
               in the meantime. */
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

    /* This is a latency-sensitive echo, not a bulk transfer: each packet
       is 4 + 6*N bytes, so it ends in a partial segment that Nagle would
       otherwise hold back until the previous data is ACKed -- serializing
       the stream into one packet per round trip. */
    tcp_nagle_disable(client_pcb);

    connected = 0;

    comm_log("[N] connecting\r\n");

    err_t err = tcp_connect(client_pcb, &server_ip, 5001, tcp_client_connected);
    if (err != ERR_OK) {
        comm_log("[E] connect failed %d\r\n", err);
    }
}

/* ============================================================
   THREAD ENTRY (called once from main)
   ============================================================ */

void lwip_comm_client_thread(void *arg)
{
    comm_log("[N] client start\r\n");
    tcp_client_start();
}

/* ============================================================
   CONNECTED CALLBACK
   ============================================================ */

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

/* ============================================================
   ERROR CALLBACK (AUTO‑RECONNECT)
   ============================================================ */

static void tcp_client_error(void *arg, err_t err)
{
    comm_log("[E] tcp %d, reconn\r\n", err);
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
    comm_resyncs++;
    comm_log("[E] %s, resync\r\n", why);
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
        comm_log("[N] closed, reconn\r\n");
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
    /* This is the module's once-per-loop entry point, so the deferred
       reconnect retry lives here: tcp_client_start() returns without
       connecting if it was called inside the backoff window, and this is
       what picks it up again afterwards. */
    if (connect_pending && mono_now_ms() >= next_connect_ms)
        tcp_client_start();

    /* One-time DMA bring-up, done here rather than in main() so the path
       stays self-contained: nothing outside this module needs to know which
       processing path is in use. */
    if (comm_use_dma && !dma_init_done) {
        dma_init_done = 1;
        if (dma_stream_init() != 0) {
            comm_log("[E] dma init, using axi-lite\r\n");
            comm_use_dma = 0;
        }
    }

    /* Collect a finished transfer BEFORE trying to start another -- the
       buffer has to be released before it can be reused. The DMA wrote the
       payload in wire order (the filter byte-swaps in fabric) directly
       behind a gap reserved for the header, so this goes out as one
       contiguous write with no repacking. */
    if (comm_use_dma) {
        uint8_t *out;
        uint32_t out_bytes;
        uint16_t out_type, out_len;
        if (dma_stream_poll(&out, &out_bytes, &out_type, &out_len)) {
            if (client_pcb && connected)
                tcp_client_send_raw(client_pcb, out, out_bytes, out_len);
            /* Measured to here, not to poll() returning: the echo leaving is
               what the latency is of. */
            latency_record(lat_t0);
            lat_t0 = 0;
            dma_stream_release();
        }
    }

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
            comm_log("[E] bad type %u\r\n", type);
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

        /* Same argument for the DMA: if a transfer is still in flight there
           is nowhere to put this packet, so leave it in the ring untouched
           rather than consuming it and having to drop it. Must be checked
           BEFORE the rx_ring_advance() below. */
        if (comm_use_dma && type == PACKET_TYPE_DATA && dma_stream_busy())
            return;

        /* Now we can safely consume header + body */
        rx_ring_advance(4);

        static uint8_t payload_buf[MAX_PAYLOAD_BYTES];

        if (length > MAX_PAYLOAD_SAMPLES) {
            /* Also a framing-loss symptom rather than a real oversized
               packet: the sender is capped well below this by
               TCP_SND_BUF (see the backpressure check above), so a length
               this large means we parsed a garbage header. */
            comm_log("[E] len %u too big\r\n", length);
            tcp_client_resync("framing lost");
            return;
        }

        /* DMA path: copy the body straight out of the ring into the DMA
           buffer and hand it to the fabric. Deliberately NO byte swap here
           -- the TDM filter does it in hardware, which is what takes the CPU
           out of the per-sample path entirely. Without that, killing the
           AXI-Lite round trips would still leave a per-sample swap loop
           behind and cap the win at ~3.8x. */
        if (comm_use_dma && type == PACKET_TYPE_DATA) {
            if (length > DMA_FRAMES_PER_BUF) {
                comm_log("[E] len %u > dma buf\r\n", length);
                tcp_client_resync("dma buf too small");
                return;
            }

            /* Clock starts here: the packet is complete and about to be
               serviced. Anything before this is waiting for the wire, which is
               not the board's latency. */
            lat_t0 = mono_now_us();

            rx_ring_peek(0, dma_stream_tx_buf(), body_bytes);
            rx_ring_advance(body_bytes);

            packets_rx++;
            samples_rx += length;
            bytes_rx   += total_needed;

            if (dma_stream_start(body_bytes, type, length) != 0) {
                comm_log("[E] dma start\r\n");
                tcp_client_resync("dma start failed");
                return;
            }
            /* The result is collected on a later pass of the main loop --
               nothing blocks here. */
            continue;
        }

        /* Bulk-copy the body out of the ring (at most one wrap), then
           byte-swap each record in place (network byte order: MSB first,
           matching tcp_client_send) instead of popping byte-by-byte. */
        uint64_t t0 = mono_now_us();

        rx_ring_peek(0, payload_buf, body_bytes);
        rx_ring_advance(body_bytes);
        for (uint32_t i = 0; i < length; i++)
            swap_be_fields(payload_buf + i * record_size, type);

        /* Stats */
        packets_rx++;
        samples_rx += length;
        bytes_rx   += total_needed;

        /* Config packets: apply (op=WRITE) or not (op=READ), then always
           reply with a read-back of the actual fabric registers. Replying
           with the read-back rather than an echo is the point -- a value the
           hardware clamped or never received shows up on the PC as a
           mismatch instead of being confirmed as if it had taken effect.

           Handled here on the AXI-Lite path, not in the DMA branch above:
           these are register pokes, not stream data, and they must not be
           gated on dma_stream_busy() -- config has to stay reachable while
           the stream is saturated, which is exactly when you want to change
           it. */
        if (type == PACKET_TYPE_CONFIG) {
            if (length >= 1) {
                packet_config_t *req = (packet_config_t *)payload_buf;
                if (req->op == CONFIG_OP_WRITE) {
                    dma_stream_set_filter(req->n_channels, req->shift, req->ctrl);
                    comm_log_set_mask(req->log_mask);
                }

                packet_config_t rsp;
                rsp.op = req->op;
                dma_stream_get_filter(&rsp.n_channels, &rsp.shift,
                                      &rsp.ctrl, &rsp.status);
                rsp.log_mask = comm_log_get_mask();

                /* Tagged [C], so muting the config category mutes this too --
                   which is correct: if you asked for silence, the packet that
                   asked for it should not answer back on the UART. The reply
                   still goes to the PC either way. */
                comm_log("[C] op=%lu n=%lu sh=%lu ctrl=%lu log=%02lx st=%08lx\r\n",
                         (unsigned long)rsp.op, (unsigned long)rsp.n_channels,
                         (unsigned long)rsp.shift, (unsigned long)rsp.ctrl,
                         (unsigned long)rsp.log_mask, (unsigned long)rsp.status);

                if (client_pcb && connected)
                    tcp_client_send(client_pcb, PACKET_TYPE_CONFIG, 1,
                                    (uint8_t *)&rsp);
            }
            continue;
        }

        /* "data" packets get each channel run through its own AXI-Lite
           processing chain in the PL (see axi_processing.c). */
        if (type == PACKET_TYPE_DATA) {
            packet_data_t *entries = (packet_data_t *)payload_buf;
            for (uint32_t i = 0; i < length; i++)
                axi_process_sample(&entries[i]);

            if (client_pcb && connected)
                tcp_client_send(client_pcb, type, length, payload_buf);

            /* AXI-Lite path completes inside this one call, so the whole
               service time is measured here -- directly comparable with the
               DMA path's figure above, which spans main-loop passes. */
            latency_record(t0);
        }
    }
}


/* ============================================================
   SEND RESPONSE
   ============================================================ */

/* Exported so main.c can push metrics from the same place it computes the
   [S] console line -- one computation, two outputs, so the serial console and
   the GUI can never disagree about what the board is doing. Silently does
   nothing while disconnected; metrics are a status feed, not something worth
   queueing or retrying. */
void comm_send_metrics(const packet_metrics_t *m)
{
    if (client_pcb && connected)
        tcp_client_send(client_pcb, PACKET_TYPE_METRICS, 1, (uint8_t *)m);
}

/* Send an already-complete wire-order block (4-byte header followed by the
   payload) exactly as-is. This is the DMA path's counterpart to
   tcp_client_send(): there is nothing to build and nothing to swap, because
   the buffer the DMA produced IS the wire format.
 *
 * TCP_WRITE_FLAG_COPY is still used. Dropping it would be genuinely
 * zero-copy, but lwIP then holds a reference to the buffer until the PC
 * ACKs, and with only two DMA buffers a retransmit or window stall would let
 * the next transfer overwrite data still in flight. Going zero-copy means
 * raising DMA_NBUF to 4 first. */
static void tcp_client_send_raw(struct tcp_pcb *tpcb, uint8_t *wire, uint32_t nbytes,
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
            comm_log("[E] output %d\r\n", err);

        packets_tx++;
        samples_tx += length;
        bytes_tx   += total_bytes;

    } else {
        comm_log("[E] write %d\r\n", err);
    }
}
