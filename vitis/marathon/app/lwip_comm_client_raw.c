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

uint32_t packets_rx = 0;
uint32_t packets_tx = 0;
uint32_t samples_rx = 0;
uint32_t samples_tx = 0;
uint32_t bytes_rx   = 0;
uint32_t bytes_tx   = 0;

#define MAX_PAYLOAD_SAMPLES 2000

/* 1 = buffer-at-a-time DMA through the TDM filter (dma_stream.c); 0 = legacy
   per-sample AXI-Lite chains (axi_processing.c). Both peripherals sit in the
   bitstream so they can be A/B'd on identical data; cleared automatically
   if dma_stream_init() fails, so a DMA problem degrades rather than kills
   the system. */
int comm_use_dma = 1;

/* Cumulative, never reset -- PC differences it for rate, or reads it as a
   lifetime count. A resync means framing was lost badly enough to need
   dropping the connection; watch this as the overload signal. */
uint32_t comm_resyncs = 0;

/* Per-packet service latency: complete-in-ring to echo-handed-to-lwIP.
   Deliberately NOT end-to-end -- that's dominated by the PC scheduler/relay/
   TCP, noise this architecture doesn't control. Drained once/s by
   comm_latency_take(); sum is 64-bit since a second at 1400 pkt/s would
   otherwise wrap a uint32. */
static uint32_t lat_min_us = 0xFFFFFFFFu;
static uint32_t lat_max_us = 0;
static uint64_t lat_sum_us = 0;
static uint32_t lat_count  = 0;

/* Start time of the packet in flight. One slot suffices: AXI-Lite finishes
   in a single call, and DMA is gated on dma_stream_busy() so only one
   transfer is ever outstanding. */
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

    /* Latency-sensitive echo, not bulk transfer: each packet ends in a
       partial segment Nagle would hold back until the previous data is
       ACKed, serializing the stream into one packet per round trip. */
    tcp_nagle_disable(client_pcb);

    connected = 0;

    comm_log("[N] connecting\r\n");

    err_t err = tcp_connect(client_pcb, &server_ip, 5001, tcp_client_connected);
    if (err != ERR_OK) {
        comm_log("[E] connect failed %d\r\n", err);
    }
}

void lwip_comm_client_thread(void *arg)
{
    comm_log("[N] client start\r\n");
    tcp_client_start();
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
static void tcp_client_resync(const char *why)
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

/* Byte-swaps every field of one record per packet_format.h's width table,
   so field widths can change via packet_format.json + rebuild with no code
   change here. Self-inverse: same call does wire->host and host->wire. */
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

/* Bounds the static buffers below regardless of which type arrives --
   sized off the biggest packet type in packet_format.h ("data"). */
#define MAX_PAYLOAD_BYTES (MAX_PAYLOAD_SAMPLES * sizeof(packet_data_t))

void comm_process(void)
{
    /* Module's once-per-loop entry point -- picks up the deferred reconnect
       retry once backoff expires. */
    if (connect_pending && mono_now_ms() >= next_connect_ms)
        tcp_client_start();

    /* One-time DMA bring-up, done here so the path stays self-contained. */
    if (comm_use_dma && !dma_init_done) {
        dma_init_done = 1;
        if (dma_stream_init() != 0) {
            comm_log("[E] dma init, using axi-lite\r\n");
            comm_use_dma = 0;
        }
    }

    /* Collect a finished transfer before starting another -- the buffer must
       be released before reuse. DMA already wrote the payload in wire order
       right behind the header gap, so this is one contiguous write, no
       repacking. */
    if (comm_use_dma) {
        uint8_t *out;
        uint32_t out_bytes;
        uint16_t out_type, out_len;
        if (dma_stream_poll(&out, &out_bytes, &out_type, &out_len)) {
            if (client_pcb && connected)
                tcp_client_send_raw(client_pcb, out, out_bytes, out_len);
            /* Measured to the echo leaving, not to poll() returning. */
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
            /* Unknown type = not actually a header, framing already lost.
               Body size is unknown so we can't skip it, and sliding forward
               byte-by-byte doesn't resync in practice -- reconnect. */
            comm_log("[E] bad type %u\r\n", type);
            tcp_client_resync("framing lost");
            return;
        }

        uint32_t body_bytes = length * record_size;
        uint32_t total_needed = 4 + body_bytes;

        /* Not enough data yet */
        if (rx_ring_used() < total_needed)
            return;

        /* Backpressure: don't consume until we can actually forward it --
           popping and dropping on a failed tcp_write() would silently lose
           data instead of waiting for the peer's window. */
        if (client_pcb && connected) {
            uint16_t need = 4 + (uint16_t)body_bytes;
            if (tcp_sndbuf(client_pcb) < need)
                return;
        }

        /* Same for DMA: a transfer in flight means nowhere to put this
           packet, so leave it in the ring. Must run BEFORE rx_ring_advance()
           below. */
        if (comm_use_dma && type == PACKET_TYPE_DATA && dma_stream_busy())
            return;

        /* Now we can safely consume header + body */
        rx_ring_advance(4);

        static uint8_t payload_buf[MAX_PAYLOAD_BYTES];

        if (length > MAX_PAYLOAD_SAMPLES) {
            /* A framing-loss symptom, not a real oversized packet -- the
               sender is capped well below this by TCP_SND_BUF, so this
               means we parsed a garbage header. */
            comm_log("[E] len %u too big\r\n", length);
            tcp_client_resync("framing lost");
            return;
        }

        /* DMA path: copy straight into the DMA buffer, NO byte swap -- the
           TDM filter does it in hardware, which is what takes the CPU fully
           out of the per-sample path (otherwise a swap loop would cap the
           win at ~3.8x). */
        if (comm_use_dma && type == PACKET_TYPE_DATA) {
            if (length > DMA_FRAMES_PER_BUF) {
                comm_log("[E] len %u > dma buf\r\n", length);
                tcp_client_resync("dma buf too small");
                return;
            }

            /* Clock starts here -- packet complete, about to be serviced.
               Anything earlier is wire wait, not board latency. */
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
            /* Result collected on a later main-loop pass -- nothing blocks. */
            continue;
        }

        /* Bulk-copy out of the ring (at most one wrap), byte-swap each
           record in place to network order, instead of popping byte-by-byte. */
        uint64_t t0 = mono_now_us();

        rx_ring_peek(0, payload_buf, body_bytes);
        rx_ring_advance(body_bytes);
        for (uint32_t i = 0; i < length; i++)
            swap_be_fields(payload_buf + i * record_size, type);

        /* Stats */
        packets_rx++;
        samples_rx += length;
        bytes_rx   += total_needed;

        /* Config: apply (WRITE) or not (READ), always reply with a
           read-back of the actual registers -- not an echo, so a clamped or
           dropped value shows up as a mismatch instead of a false confirm.
           Handled on the AXI-Lite path, not gated on dma_stream_busy(): config
           must stay reachable exactly when the stream is saturated. */
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

                /* Tagged [C] -- muting config mutes this too, correctly; the
                   PC reply still goes out either way. */
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

        /* "data" packets: each channel through its own AXI-Lite chain. */
        if (type == PACKET_TYPE_DATA) {
            packet_data_t *entries = (packet_data_t *)payload_buf;
            for (uint32_t i = 0; i < length; i++)
                axi_process_sample(&entries[i]);

            if (client_pcb && connected)
                tcp_client_send(client_pcb, type, length, payload_buf);

            /* Completes inside this one call, so the whole service time is
               measured here -- comparable with the DMA path's figure above. */
            latency_record(t0);
        }
    }
}

/* Exported so main.c pushes metrics from where it computes the [S] line --
   one computation, two outputs, console and GUI can't disagree. No-op
   while disconnected; metrics are a status feed, not worth retrying. */
void comm_send_metrics(const packet_metrics_t *m)
{
    if (client_pcb && connected)
        tcp_client_send(client_pcb, PACKET_TYPE_METRICS, 1, (uint8_t *)m);
}

/* DMA path's counterpart to tcp_client_send() -- sends an already-complete
   wire-order block as-is, nothing to build or swap.
 *
 * TCP_WRITE_FLAG_COPY stays on: dropping it would be genuinely zero-copy,
 * but lwIP then holds a reference until the PC ACKs, and with only 2 DMA
 * buffers a retransmit/stall would let the next transfer overwrite data
 * still in flight. Zero-copy needs DMA_NBUF raised to 4 first. */
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
