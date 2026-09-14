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

/* IPv4 + static IP only (LWIP_IPV6/LWIP_DHCP off, lwipopts.h). Fixed at
   192.168.1.10 -- the PC relay identifies the board by source IP, so it
   can't float. Stock template's IPv6/DHCP branches were dropped, not kept
   as dead #if blocks. */
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

    /* Clock self-check: two BSP time sources here turned out wrong (see
       mono_clock.h), each producing a plausible-looking but silently
       rescaled [STATS] line. Time a known 100 ms sleep at boot -- if this
       doesn't print ~100, every rate below is suspect. */
    mono_clock_init();
    xil_printf("[CLK] 100ms = %lu ms\r\n", mono_clock_selftest_ms(100));

    /* start our custom TCP client thread */
    lwip_comm_client_thread(NULL);

    uint64_t last_stats_ms = mono_now_ms();

    /* Loop-pass instrumentation distinguishes "the loop is slow" from "the
       work in it is slow". Reference at CHUNK_SIZE=500: idle 875/s @1142us,
       saturated 662/s @1510us -- the 368us delta over ~0.42 pkt/pass is
       ~437 ns/AXI-Lite transaction, the real per-sample cost and ceiling. */
    uint32_t loop_passes = 0;
    /* Peak ring occupancy over the window -- rx_ring_used() sampled only at
       stats time would read near zero; the burst in between is the signal. */
    uint32_t ring_peak = 0;

    while (1) {

      /* No sleep here on purpose (removed a usleep(1000) that only existed
         to fake-increment two now-dead millisecond counters, wasting ~1000
         us of every 1510 us pass). Busy-polling is correct for this app --
         bare metal, single purpose, lwIP timers run off timer_callback()'s
         ISR in platform.c, unaffected by this loop's pace. */
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
