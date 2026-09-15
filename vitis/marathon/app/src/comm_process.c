#include <stdint.h>
#include "comm_process.h"
#include "packet_codec.h"
#include "tcp_link.h"
#include "comm_stats.h"
#include "comm_log.h"
#include "rx_ring.h"
#include "mono_clock.h"
#include "axi_processing.h"
#include "dma_stream.h"

/* Config packet ops -- see the "config" description in
   shared/marathon/packet_format.json, which is the source of truth. */
#define CONFIG_OP_READ  0u
#define CONFIG_OP_WRITE 1u

/* 1 = buffer-at-a-time DMA through the TDM filter (dma_stream.c); 0 = legacy
   per-sample AXI-Lite chains (axi_processing.c). Both peripherals sit in the
   bitstream so they can be A/B'd on identical data; cleared automatically
   if dma_stream_init() fails, so a DMA problem degrades rather than kills
   the system. */
int comm_use_dma = 1;

/* Start time of the packet in flight. One slot suffices: AXI-Lite finishes
   in a single call, and DMA is gated on dma_stream_busy() so only one
   transfer is ever outstanding. */
static uint64_t lat_t0 = 0;

static int dma_init_done = 0;

void comm_process(void)
{
    /* Module's once-per-loop entry point -- picks up the deferred reconnect
       retry once backoff expires. */
    tcp_link_service();

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
            if (tcp_link_up())
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
        if (tcp_link_up()) {
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
        tcp_link_consumed(4);

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
            tcp_link_consumed(body_bytes);

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
        tcp_link_consumed(body_bytes);
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

                if (tcp_link_up())
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

            if (tcp_link_up())
                tcp_client_send(client_pcb, type, length, payload_buf);

            /* Completes inside this one call, so the whole service time is
               measured here -- comparable with the DMA path's figure above. */
            latency_record(t0);
        }
    }
}
