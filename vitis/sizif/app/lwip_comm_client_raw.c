#include "lwip/tcp.h"
#include "lwip/ip_addr.h"
#include <string.h>
#include <stdint.h>
#include <stdio.h>
#include <stdarg.h>
#include "xil_printf.h"
#include "xil_io.h"
#include "sleep.h"
#include "packet_format.h"

/* axi_processing_ch1_0/axi_processing_ch2_0 -- each channel's own
   processing chain, deliberately separate VHDL entities (not one module
   instantiated twice) so their architectures can diverge and be compared
   independently -- see vivado/sizif/hdl/axi_processing_ch1.vhd and
   axi_processing_ch2.vhd. Addresses match the assign_bd_address calls in
   vivado/sizif/bd_CoraZ7_Eth.tcl. Register layout mirrors axi_fir_0's:
   reg0 (offset 0x0, write) takes the new input sample, reg3 (offset
   0xC, read) returns the processed result (routed through my_axi's
   "fir_result" read-back port). */
#define AXI_CH1_BASE   0x40001000u
#define AXI_CH2_BASE   0x40002000u
#define AXI_PROC_REG_IN  0x0u
#define AXI_PROC_REG_OUT 0xCu

/* ============================================================
   LOGGING SYSTEM
   ============================================================ */

#define LOG_BUF_SIZE 2048

static char log_buffer[LOG_BUF_SIZE];
static int  log_len = 0;

void comm_log(const char *fmt, ...)
{
    va_list args;
    char tmp[256];
    int n;

    va_start(args, fmt);
    n = vsnprintf(tmp, sizeof(tmp), fmt, args);
    va_end(args);

    if (n <= 0)
        return;

    if (log_len + n < LOG_BUF_SIZE) {
        memcpy(log_buffer + log_len, tmp, n);
        log_len += n;
    }
}

void comm_log_flush(void)
{
    if (log_len > 0) {
        log_buffer[log_len] = '\0';
        xil_printf("%s", log_buffer);
        log_len = 0;
    }
}

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

/* ============================================================
   RX RING BUFFER (STREAM)
   ============================================================ */

#define RX_RING_SIZE 131072   /* 128 KB */
#define MAX_PAYLOAD_SAMPLES 2000

static uint8_t  rx_ring[RX_RING_SIZE];
static uint32_t rx_head = 0;  /* write index */
static uint32_t rx_tail = 0;  /* read index */

static inline uint32_t rx_ring_used(void)
{
    if (rx_head >= rx_tail)
        return rx_head - rx_tail;
    else
        return RX_RING_SIZE - (rx_tail - rx_head);
}

static inline uint32_t rx_ring_free(void)
{
    return RX_RING_SIZE - rx_ring_used() - 1;
}

/* Bulk copy into the ring, wrapping at most once, instead of a per-byte
   modulo. `len` must not exceed RX_RING_SIZE. */
static inline void rx_ring_push(const uint8_t *data, uint32_t len)
{
    uint32_t first_chunk = RX_RING_SIZE - rx_head;
    if (first_chunk >= len) {
        memcpy(&rx_ring[rx_head], data, len);
        rx_head = (rx_head + len) % RX_RING_SIZE;
    } else {
        memcpy(&rx_ring[rx_head], data, first_chunk);
        memcpy(&rx_ring[0], data + first_chunk, len - first_chunk);
        rx_head = len - first_chunk;
    }
}

/* Copy `len` bytes starting `offset` bytes ahead of rx_tail into dst,
   without consuming them (peek). */
static inline void rx_ring_peek(uint32_t offset, uint8_t *dst, uint32_t len)
{
    uint32_t start = (rx_tail + offset) % RX_RING_SIZE;
    uint32_t first_chunk = RX_RING_SIZE - start;
    if (first_chunk >= len) {
        memcpy(dst, &rx_ring[start], len);
    } else {
        memcpy(dst, &rx_ring[start], first_chunk);
        memcpy(dst + first_chunk, &rx_ring[0], len - first_chunk);
    }
}

/* Consume `len` bytes from the tail (must have been peeked already). */
static inline void rx_ring_advance(uint32_t len)
{
    rx_tail = (rx_tail + len) % RX_RING_SIZE;
}

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

    /* Discard any bytes left over from a connection that was cut mid-packet,
       otherwise they get prepended to the next connection's stream and
       desync framing permanently. */
    rx_head = 0;
    rx_tail = 0;

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

    struct pbuf *q = p;
    while (q != NULL) {
        uint32_t free = rx_ring_free();
        if (q->len > free) {
            /* Drop oldest data to make room */
            uint32_t drop = q->len - free;
            if (drop > rx_ring_used())
                drop = rx_ring_used();
            rx_tail = (rx_tail + drop) % RX_RING_SIZE;
            comm_log("\r[PCB] RX ring overflow, dropped %lu bytes\r\n",
                     (unsigned long)drop);
        }

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
            /* Unknown type -- we have no way to know how many body bytes
               belong to it. Drop just the header and hope the stream
               resyncs on its own (mirrors the "payload too large" recovery
               below); a real TCP error will fully reset the ring anyway. */
            comm_log("\r[PCB] unknown packet type %u, dropping header\r\n", type);
            rx_ring_advance(4);
            continue;
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
            comm_log("\r[PCB] payload too large (%u records), dropping\r\n", length);
            rx_ring_advance(body_bytes);
            continue;
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
           processing chain in the PL (axi_processing_ch1_0 for ch1,
           axi_processing_ch2_0 for ch2): write the sample to reg0, read
           the processed result back from reg3 -- synchronous, no polling
           needed, the filter's internal latency (a couple of AXI clocks)
           is negligible next to one AXI4-Lite
           round trip. ts is left untouched so the PC can match RX to TX. */
        if (type == PACKET_TYPE_DATA) {
            packet_data_t *entries = (packet_data_t *)payload_buf;
            for (uint32_t i = 0; i < length; i++) {
                Xil_Out32(AXI_CH1_BASE + AXI_PROC_REG_IN, (u32)entries[i].ch1);
                Xil_Out32(AXI_CH2_BASE + AXI_PROC_REG_IN, (u32)entries[i].ch2);
                entries[i].ch1 = (uint16_t)(Xil_In32(AXI_CH1_BASE + AXI_PROC_REG_OUT) & 0xFFFFu);
                entries[i].ch2 = (uint16_t)(Xil_In32(AXI_CH2_BASE + AXI_PROC_REG_OUT) & 0xFFFFu);
            }

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
        /* Let LWIP batch tcp_output() via timers */

        packets_tx++;
        samples_tx += length;
        bytes_tx   += total_bytes;

    } else {
        comm_log("\r[PCB] tcp_write failed: %d\r\n", err);
    }
}
