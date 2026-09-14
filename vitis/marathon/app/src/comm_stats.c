#include <stdio.h>
#include "comm_stats.h"
#include "tcp_link.h"
#include "comm_log.h"
#include "rx_ring.h"
#include "mono_clock.h"

uint32_t packets_rx = 0;
uint32_t packets_tx = 0;
uint32_t samples_rx = 0;
uint32_t samples_tx = 0;
uint32_t bytes_rx   = 0;
uint32_t bytes_tx   = 0;

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

void latency_record(uint64_t t0)
{
    if (t0 == 0)
        return;
    uint32_t dt = (uint32_t)(mono_now_us() - t0);
    if (dt < lat_min_us) lat_min_us = dt;
    if (dt > lat_max_us) lat_max_us = dt;
    lat_sum_us += dt;
    lat_count++;
}

static void comm_latency_take(uint32_t *min_us, uint32_t *mean_us, uint32_t *max_us)
{
    *min_us  = (lat_count ? lat_min_us : 0);
    *max_us  = lat_max_us;
    *mean_us = (uint32_t)(lat_count ? (lat_sum_us / lat_count) : 0);
    lat_min_us = 0xFFFFFFFFu;
    lat_max_us = 0;
    lat_sum_us = 0;
    lat_count  = 0;
}

/* Called from comm_stats_report(), right where the [S] line is computed --
   one computation, two outputs, console and GUI can't disagree. No-op
   while disconnected; metrics are a status feed, not worth retrying. */
static void comm_send_metrics(const packet_metrics_t *m)
{
    if (tcp_link_up())
        tcp_client_send(client_pcb, PACKET_TYPE_METRICS, 1, (uint8_t *)m);
}

/* Compact count for the [STATS] line: 873, 2.4k, 130k, 1.2M. Integer only
   -- this toolchain's libc build has no float printf. */
static void fmt_si(char *out, size_t n, uint32_t v)
{
    if (v < 1000U)
        snprintf(out, n, "%lu", (unsigned long)v);
    else if (v < 10000U)
        snprintf(out, n, "%lu.%luk", (unsigned long)(v / 1000U),
                                     (unsigned long)((v % 1000U) / 100U));
    else if (v < 1000000U)
        snprintf(out, n, "%luk", (unsigned long)(v / 1000U));
    else
        snprintf(out, n, "%lu.%luM", (unsigned long)(v / 1000000U),
                                     (unsigned long)((v % 1000000U) / 100000U));
}

void comm_stats_report(uint64_t stats_now, uint32_t elapsed,
                       uint32_t loop_passes, uint32_t ring_peak)
{
    /* Normalize by the window that actually elapsed (it overshoots
       1000ms by a varying amount) rather than assuming exactly 1000ms,
       so rates stay accurate regardless of jitter. Integer fixed-point
       throughout (no float printf here); *1000 done in 64 bits since
       bytes-per-window*1000 overflows 32 bits above ~4 MB/s. */
    uint32_t rx_pps = (uint32_t)(((uint64_t)packets_rx * 1000ULL) / elapsed);
    uint32_t tx_pps = (uint32_t)(((uint64_t)packets_tx * 1000ULL) / elapsed);
    uint32_t rx_sps = (uint32_t)(((uint64_t)samples_rx * 1000ULL) / elapsed);

    uint64_t rx_bps = ((uint64_t)bytes_rx * 1000ULL) / elapsed;
    uint32_t rx_mb_int  = (uint32_t)(rx_bps / 1000000ULL);
    uint32_t rx_mb_frac = (uint32_t)((rx_bps % 1000000ULL) / 10000ULL);

    uint32_t loops_ps = (uint32_t)(((uint64_t)loop_passes * 1000ULL) / elapsed);

    /* Only anomalies get printed -- TX==RX and window==1000ms are the
       normal case, so their absence told you nothing; presence is now
       the signal. Every byte here is charged against comm_log's 512 B/s
       budget (comm_log.c), ~1 us/byte at 115200 baud. */
    char extra[40];
    int  epos = 0;
    extra[0] = '\0';
    if (tx_pps != rx_pps)
        epos += snprintf(extra + epos, sizeof(extra) - epos,
                         " tx=%lu", (unsigned long)tx_pps);
    if (elapsed < 990U || elapsed > 1010U)
        snprintf(extra + epos, sizeof(extra) - epos,
                 " w=%lu", (unsigned long)elapsed);

    char smp[12], lps[12];
    fmt_si(smp, sizeof(smp), rx_sps);
    fmt_si(lps, sizeof(lps), loops_ps);

    comm_log("[S] %lup/s %s smp/s %lu.%02luMB/s loop %s/s%s\r\n",
             (unsigned long)rx_pps, smp,
             (unsigned long)rx_mb_int, (unsigned long)rx_mb_frac,
             lps, extra);

    /* Same numbers as [S] above, pushed to the GUI -- built once here so
       console and PC can't disagree. Sent before counters clear. */
    packet_metrics_t m;
    m.uptime_s  = (uint32_t)(stats_now / 1000ULL);
    m.window_ms = elapsed;
    m.rx_pps    = rx_pps;
    m.tx_pps    = tx_pps;
    m.rx_sps    = rx_sps;
    m.rx_bps    = (uint32_t)rx_bps;
    m.loop_ps   = loops_ps;
    m.ring_used = rx_ring_used();
    m.ring_peak = ring_peak;
    m.resyncs   = comm_resyncs;
    comm_latency_take(&m.lat_min_us, &m.lat_mean_us, &m.lat_max_us);
    comm_send_metrics(&m);

    packets_rx = packets_tx = 0;
    samples_rx = samples_tx = 0;
    bytes_rx   = bytes_tx   = 0;
}
