#include "lwip/tcp.h"
#include "lwip/ip_addr.h"
#include <string.h>
#include <stdint.h>
#include <stdio.h>
#include <stdarg.h>
#include "xil_printf.h"
#include "sleep.h"

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
static void tcp_client_send(struct tcp_pcb *tpcb, uint16_t type, uint16_t length, uint16_t *payload);

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
   OPTIONAL PERIODIC SENDER
   ============================================================ */

void send_periodic(void *arg)
{
    struct tcp_pcb *tpcb = (struct tcp_pcb *)arg;

    static uint8_t toggle = 0;

    uint16_t length = 100;
    uint16_t payload[100];

    uint16_t a = 1000;
    uint16_t b = 3000;

    toggle = !toggle;
    uint16_t v = toggle ? a : b;

    for (int i = 0; i < length; i++)
        payload[i] = v;

    tcp_client_send(tpcb, 2, length, payload);
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

        /* =====================================================
           DEBUG: PRINT HEADER
           ===================================================== */
        //comm_log("\r[DBG] RAW HEADER: %02X %02X %02X %02X\r\n",
        //         h0, h1, h2, h3);
        //comm_log("[DBG] type=%u length=%u\r\n", type, length);

        uint32_t body_bytes = length * 2;
        uint32_t total_needed = 4 + body_bytes;

        /* Not enough data yet */
        if (rx_ring_used() < total_needed)
            return;

        /* Backpressure: don't consume this packet until we can actually
           forward it. Popping it and dropping it on a failed tcp_write()
           would silently lose data instead of just waiting for the peer's
           window to free up. */
        if (client_pcb && connected) {
            uint16_t need = 4 + (uint16_t)(length * sizeof(uint16_t));
            if (tcp_sndbuf(client_pcb) < need)
                return;
        }

        /* Now we can safely consume header + body */
        rx_ring_advance(4);

        static uint16_t payload_buf[MAX_PAYLOAD_SAMPLES];
        uint16_t *payload = payload_buf;

        if (length > MAX_PAYLOAD_SAMPLES) {
            comm_log("\r[PCB] payload too large (%u samples), dropping\r\n", length);
            rx_ring_advance(body_bytes);
            continue;
        }

        /* Bulk-copy the body out of the ring (at most one wrap), then
           byte-swap in place (network byte order: MSB first, matching
           tcp_client_send) instead of popping byte-by-byte. */
        rx_ring_peek(0, (uint8_t *)payload, body_bytes);
        rx_ring_advance(body_bytes);
        for (uint32_t i = 0; i < length; i++) {
          uint8_t msb = ((uint8_t *)payload)[i * 2];
          uint8_t lsb = ((uint8_t *)payload)[i * 2 + 1];
          payload[i] = ((uint16_t)msb << 8) | lsb;
        }


        /* =====================================================
           DEBUG: PRINT PAYLOAD
           ===================================================== */
        //comm_log("[DBG] first: ");
        //for (uint32_t i = 0; i < (length < 10 ? length : 10); i++)
        //    comm_log("%u ", payload[i]);
        //comm_log("\r\n");
        
        //comm_log("[DBG] last: ");
        //for (uint32_t i = (length > 10 ? length - 10 : 0); i < length; i++)
        //    comm_log("%u ", payload[i]);
        //comm_log("\r\n");

        /* Stats */
        packets_rx++;
        samples_rx += length;
        bytes_rx   += total_needed;

        /* Process */
        for (uint32_t i = 0; i < length; i++)
            payload[i] *= 2;

        /* Send */
        if (client_pcb && connected)
            tcp_client_send(client_pcb, type, length, payload);
    }
}


/* ============================================================
   SEND RESPONSE
   ============================================================ */

static void tcp_client_send(struct tcp_pcb *tpcb, uint16_t type, uint16_t length, uint16_t *payload)
{
    static uint8_t buf[4 + MAX_PAYLOAD_SAMPLES * sizeof(uint16_t)];
    uint16_t total_bytes = 4 + length * sizeof(uint16_t);

    buf[0] = (type >> 8) & 0xFF;
    buf[1] = (type     ) & 0xFF;
    buf[2] = (length >> 8) & 0xFF;
    buf[3] = (length     ) & 0xFF;

    for (int i = 0; i < length; i++) {
        uint16_t v = payload[i];
        buf[4 + i*2] = (v >> 8) & 0xFF;
        buf[5 + i*2] = (v     ) & 0xFF;
    }

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
