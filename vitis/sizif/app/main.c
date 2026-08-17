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
#include "xil_io.h"

/* ---- Wall clock -------------------------------------------------------
   Read the Cortex-A9 SCU *global* timer directly: 64-bit, free-running,
   monotonic, clocked at CPU_3x2x (= core clock / 2). Fixed address on
   Zynq-7000: PERIPHBASE 0xF8F00000 + 0x200.

   Not XTime_GetTime(): in this BSP that resolves to the xiltimer TTC
   implementation (XSLEEPTIMER_IS_TTCPS), which returns
   XTtcPs_GetCounterValue() -- a *16-bit* counter that wraps every ~0.6 ms.
   Using it made every window read as 0 ms, so [STATS] stopped printing
   entirely. The cortexa9 standalone xtime_l.c has a correct global-timer
   version of the same function, but the xiltimer one wins at link time.

   Not platform.c's get_time_ms() either: that returns an ISR tick counter
   whose interval is ~340 us, not 1 ms. */
#define GTIMER_BASE       0xF8F00200U
#define GTIMER_LOWER      0x00U
#define GTIMER_UPPER      0x04U
#define GTIMER_CONTROL    0x08U
#define GTIMER_HZ         (XPAR_CPU_CORE_CLOCK_FREQ_HZ / 2U)   /* 325 MHz */

static void gtimer_start(void)
{
    /* ps7_init normally enables this, but don't depend on it. */
    if ((Xil_In32(GTIMER_BASE + GTIMER_CONTROL) & 1U) == 0U)
        Xil_Out32(GTIMER_BASE + GTIMER_CONTROL, 1U);
}

static uint64_t now_ms(void)
{
    uint32_t hi, lo;

    /* Re-read the high word to catch a low-word rollover between reads. */
    do {
        hi = Xil_In32(GTIMER_BASE + GTIMER_UPPER);
        lo = Xil_In32(GTIMER_BASE + GTIMER_LOWER);
    } while (Xil_In32(GTIMER_BASE + GTIMER_UPPER) != hi);

    return (((uint64_t)hi << 32) | (uint64_t)lo) / (GTIMER_HZ / 1000U);
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
    gtimer_start();
    {
        uint64_t t0 = now_ms();
        usleep(100000);
        uint32_t measured = (uint32_t)(now_ms() - t0);
        xil_printf("\r[CLK] usleep(100ms) measured as %lu ms (expect ~100)\r\n",
                   measured);
    }

    /* start our custom TCP client thread */
    lwip_comm_client_thread(NULL);

    /* Stats window is timed off the TTC hardware counter via
       XTime_GetTime() (COUNTS_PER_SECOND = XSLEEPTIMER_FREQ, ~108.2 MHz on
       this part). Two earlier attempts at this were both wrong, so the
       reasoning is worth keeping:

       1. Counting main-loop passes and calling each one a millisecond.
          A pass is usleep(1000) *plus* the work in it, so the window ran
          long -- about 14% at 100 pkt/s, and worse under load.

       2. get_time_ms() from platform.c, which looks like the obvious
          answer and is not. It returns `tickcntr`, an ISR counter whose
          interval is NOT 1 ms: measured here it advances ~2.94 times per
          millisecond, so a window labelled "1001 ms" was really ~340 ms
          and every rate came out ~3x too low. The BSP is internally
          inconsistent about this -- platform.c's own tcp_fasttmr logic
          (`tickcntr % 25`) only makes sense if a tick were 10 ms, and the
          MicroBlaze variant of get_time_ms() returns `tickcntr * 10`.
          Don't trust tickcntr for wall-clock; use the hardware counter.

       Ground truth for both corrections was the PC-side relay's own packet
       counters, which are independent of anything running on the board. */
    uint64_t last_stats_ms = now_ms();

    /* Instrumentation: how many times per second does this loop actually
       run, and how long is a pass? Distinguishes "the loop is slow" from
       "the work in it is slow".

       Reference points measured at CHUNK_SIZE=500, with the sleep still in
       (see below): idle 875/s at 1142 us/pass, saturated 662/s at 1510
       us/pass. The 368 us difference over ~0.42 packets/pass works out to
       ~437 ns per AXI-Lite transaction (~22 cycles @ 50 MHz), which is the
       real per-sample cost and the eventual hard ceiling. */
    uint32_t loop_passes = 0;

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

      uint64_t stats_now = now_ms();
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
        uint32_t tx_sps = (uint32_t)(((uint64_t)samples_tx * 1000ULL) / elapsed);

        uint64_t rx_bps = ((uint64_t)bytes_rx * 1000ULL) / elapsed;
        uint64_t tx_bps = ((uint64_t)bytes_tx * 1000ULL) / elapsed;

        uint32_t rx_mb_int  = (uint32_t)(rx_bps / 1000000ULL);
        uint32_t rx_mb_frac = (uint32_t)((rx_bps % 1000000ULL) / 10000ULL);
        uint32_t tx_mb_int  = (uint32_t)(tx_bps / 1000000ULL);
        uint32_t tx_mb_frac = (uint32_t)((tx_bps % 1000000ULL) / 10000ULL);

        /* Loop rate + average pass time in microseconds. With the sleep
           gone, pass_us is pure work: it should be small when idle and
           rise with load as comm_process() does more AXI transactions per
           pass. If it climbs while pkt/s plateaus, the per-sample AXI cost
           is the wall (see the note above loop_passes). */
        uint32_t loops_ps = (uint32_t)(((uint64_t)loop_passes * 1000ULL) / elapsed);
        uint32_t pass_us  = loop_passes
                          ? (uint32_t)(((uint64_t)elapsed * 1000ULL) / loop_passes)
                          : 0;

        comm_log("\r[STATS] (%lu ms) RX: %lu pkt/s, %lu samp/s, %lu.%02lu MB/s | "
                 "TX: %lu pkt/s, %lu samp/s, %lu.%02lu MB/s | "
                 "loop: %lu/s (%lu us/pass)\r\n",
                 elapsed,
                 rx_pps, rx_sps, rx_mb_int, rx_mb_frac,
                 tx_pps, tx_sps, tx_mb_int, tx_mb_frac,
                 loops_ps, pass_us);

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
