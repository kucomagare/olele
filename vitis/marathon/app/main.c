/*
 * Copyright (C) 2018 - 2022 Xilinx, Inc.
 * Copyright (C) 2022 - 2023 Advanced Micro Devices, Inc.
 * All rights reserved.
 *
 * Redistribution and use in source and binary forms, with or without modification,
 * are permitted provided that the following conditions are met:
 *
 * 1. Redistributions of source code must retain the above copyright notice,
 *    this list of conditions and the following disclaimer.
 * 2. Redistributions in binary form must reproduce the above copyright notice,
 *    this list of conditions and the following disclaimer in the documentation
 *    and/or other materials provided with the distribution.
 * 3. The name of the author may not be used to endorse or promote products
 *    derived from this software without specific prior written permission.
 *
 * THIS SOFTWARE IS PROVIDED BY THE AUTHOR ``AS IS'' AND ANY EXPRESS OR IMPLIED
 * WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED WARRANTIES OF
 * MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT
 * SHALL THE AUTHOR BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL,
 * EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT
 * OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
 * INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
 * CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING
 * IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY
 * OF SUCH DAMAGE.
 *
 */

#include <stdio.h>
#include "xparameters.h"
#include "netif/xadapter.h"
#include "platform.h"
#include "platform_config.h"
#include "lwipopts.h"
#include "xil_printf.h"
#include "sleep.h"
#include "lwip/priv/tcp_priv.h"
#include "lwip/init.h"
#include "lwip/inet.h"
#include "lwip/sys.h"
#include "lwip_comm_client_raw.h"
#include "rx_ring.h"   /* rx_ring_used() -- ring occupancy for the metrics packet */
#include "mono_clock.h"

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

/* This build is IPv4 + static IP only: the BSP sets LWIP_IPV6 0 and
   LWIP_DHCP 0 (see lwipopts.h), and the board's address is fixed at
   192.168.1.10 by convention -- the PC-side relay identifies the board by
   source IP, so it can't float. The stock template's IPv6/DHCP branches
   were dropped rather than kept as dead #if blocks. */
#define DEFAULT_IP_ADDRESS	"192.168.1.10"
#define DEFAULT_IP_MASK	  	"255.255.255.0"
#define DEFAULT_GW_ADDRESS	"192.168.1.1"

extern volatile int TcpFastTmrFlag;
extern volatile int TcpSlowTmrFlag;

struct netif server_netif;

static void print_ip(char *msg, ip_addr_t *ip)
{
	print(msg);
	xil_printf("\r%d.%d.%d.%d\r\n", ip4_addr1(ip), ip4_addr2(ip),
			ip4_addr3(ip), ip4_addr4(ip));
}

static void print_ip_settings(ip_addr_t *ip, ip_addr_t *mask, ip_addr_t *gw)
{
	print_ip("Board IP:       ", ip);
	print_ip("Netmask :       ", mask);
	print_ip("Gateway :       ", gw);
}

static void assign_default_ip(ip_addr_t *ip, ip_addr_t *mask, ip_addr_t *gw)
{
	int err;

	xil_printf("\rConfiguring default IP %s \r\n", DEFAULT_IP_ADDRESS);

	err = inet_aton(DEFAULT_IP_ADDRESS, ip);
	if (!err)
		xil_printf("\rInvalid default IP address: %d\r\n", err);

	err = inet_aton(DEFAULT_IP_MASK, mask);
	if (!err)
		xil_printf("\rInvalid default IP MASK: %d\r\n", err);

	err = inet_aton(DEFAULT_GW_ADDRESS, gw);
	if (!err)
		xil_printf("\rInvalid default gateway address: %d\r\n", err);
}

int main(void)
{
	struct netif *netif;
    
	/* the mac address of the board. this should be unique per board */
	unsigned char mac_ethernet_address[] = {
		0x00, 0x0a, 0x35, 0x00, 0x01, 0x02 };

	netif = &server_netif;

	init_platform();

	xil_printf("\r\r\r\n\n");
	xil_printf("\r-----lwIP RAW Mode TCP Client Application-----\r\n");

	/* initialize lwIP */
	lwip_init();

	/* Add network interface to the netif_list, and set it as default */
	if (!xemac_add(netif, NULL, NULL, NULL, mac_ethernet_address,
				PLATFORM_EMAC_BASEADDR)) {
		xil_printf("\rError adding N/W interface\r\n");
		return -1;
	}

	netif_set_default(netif);

	/* Under the SDT flow (this build passes -DSDT, see the platform's
	   generated Xilinx.spec) interrupt setup happens inside
	   init_platform() via xinterrupt_wrap -- there is no separate
	   platform_enable_interrupts() call to make here. */

	/* specify that the network if is up */
	netif_set_up(netif);

	assign_default_ip(&(netif->ip_addr), &(netif->netmask), &(netif->gw));
	print_ip_settings(&(netif->ip_addr), &(netif->netmask), &(netif->gw));
	xil_printf("\r\n");

    /* Clock self-check. Two different "obvious" time sources in this BSP
       turned out to be wrong (one ~3x fast, one a 16-bit counter that made
       every window read as 0 ms), and each time the symptom was a plausible
       looking but silently rescaled [STATS] line. So: time a known 100 ms
       sleep and print the result at boot. If this doesn't say ~100, every
       rate below it is suspect and you know immediately. */
    mono_clock_init();
    xil_printf("[CLK] 100ms = %lu ms\r\n", mono_clock_selftest_ms(100));

    /* start our custom TCP client thread */
    lwip_comm_client_thread(NULL);

    /* Stats window is timed with mono_now_ms() (SCU global timer). Two
       earlier attempts used BSP-provided time functions and both were
       wrong -- one ~3x fast, one a 16-bit counter -- see mono_clock.h for
       which and why. Ground truth for both corrections was the PC-side
       relay's packet counters, which are independent of anything running
       on the board; the [CLK] self-check above now catches it directly. */
    uint64_t last_stats_ms = mono_now_ms();

    /* Instrumentation: how many times per second does this loop actually
       run, and how long is a pass? Distinguishes "the loop is slow" from
       "the work in it is slow".

       Reference points measured at CHUNK_SIZE=500, with the sleep still in
       (see below): idle 875/s at 1142 us/pass, saturated 662/s at 1510
       us/pass. The 368 us difference over ~0.42 packets/pass works out to
       ~437 ns per AXI-Lite transaction (~22 cycles @ 50 MHz), which is the
       real per-sample cost and the eventual hard ceiling. */
    uint32_t loop_passes = 0;
    /* Peak ring occupancy over the window, sampled once per main-loop pass.
       rx_ring_used() at the instant the stats fire would almost always read
       near zero -- the interesting moment is the burst in between. */
    uint32_t ring_peak = 0;

    while (1) {

      /* No sleep here on purpose. There used to be a usleep(1000), but it
         was never a functional requirement -- it existed so that two
         software counters (tick_ms, stats_ms) could each be incremented
         once per pass and pretend to be milliseconds. Both are gone:
         tick_ms was dead (incremented, never read) and stats_ms has been
         replaced by the hardware clock above, so the sleep was spending
         ~1000 us of every 1510 us pass to maintain nothing.

         Busy-polling is correct for this app: bare metal, single purpose,
         nothing to yield to, and the lwIP timers are driven by
         timer_callback() in platform.c (an ISR) rather than by this loop,
         so their timing is unaffected. This is also how Xilinx's own
         raw-mode lwIP examples are written. */
      loop_passes++;
      {
        uint32_t used = rx_ring_used();
        if (used > ring_peak) ring_peak = used;
      }

      if (TcpFastTmrFlag) {
        tcp_fasttmr();
        TcpFastTmrFlag = 0;
      }
      if (TcpSlowTmrFlag) {
        tcp_slowtmr();
        TcpSlowTmrFlag = 0;
      }

      xemacif_input(netif);
      comm_process();

      comm_log_flush();

      uint64_t stats_now = mono_now_ms();
      uint32_t elapsed = (uint32_t)(stats_now - last_stats_ms);

      if (elapsed >= 1000) {
        /* Normalize every counter by the window that actually elapsed
           rather than assuming it was exactly 1000 ms -- the loop only
           checks once per pass, so the window overshoots slightly and by a
           varying amount. All rates below are therefore true per-second
           figures regardless of window jitter.

           Integer fixed-point throughout (no float printf in this
           toolchain's libc build); the intermediate *1000 is done in 64
           bits because bytes-per-window * 1000 overflows 32 bits above
           ~4 MB/s, which is now reachable after the TCP_SND_BUF bump. */
        uint32_t rx_pps = (uint32_t)(((uint64_t)packets_rx * 1000ULL) / elapsed);
        uint32_t tx_pps = (uint32_t)(((uint64_t)packets_tx * 1000ULL) / elapsed);
        uint32_t rx_sps = (uint32_t)(((uint64_t)samples_rx * 1000ULL) / elapsed);

        uint64_t rx_bps = ((uint64_t)bytes_rx * 1000ULL) / elapsed;
        uint32_t rx_mb_int  = (uint32_t)(rx_bps / 1000000ULL);
        uint32_t rx_mb_frac = (uint32_t)((rx_bps % 1000000ULL) / 10000ULL);

        uint32_t loops_ps = (uint32_t)(((uint64_t)loop_passes * 1000ULL) / elapsed);

        /* Only the anomalies get printed. TX is identical to RX on an echo
           path, and the window is 1000 ms unless something stalled the
           loop -- printing them every second was ~half the line and told
           you nothing. Now their *presence* is the signal.

           Every byte here costs: at 115200 baud this line blocks the loop
           for ~1 us/byte, and it is charged against comm_log's 512 B/s
           budget (see comm_log.c). The old 130-byte version spent a
           quarter of that budget once a second. */
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

        /* Same numbers as the [S] line above, pushed to the GUI. Built here
           rather than recomputed anywhere else so the serial console and the
           PC can never disagree about what the board is doing. Sent before
           the counters are cleared, obviously. */
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

        ring_peak = 0;

        packets_rx = packets_tx = 0;
        samples_rx = samples_tx = 0;
        bytes_rx   = bytes_tx   = 0;

        loop_passes = 0;
        last_stats_ms = stats_now;
      }
    }


	/* never reached */
	cleanup_platform();

	return 0;
}
