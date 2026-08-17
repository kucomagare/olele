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

    /* start our custom TCP client thread */
    lwip_comm_client_thread(NULL);

    uint32_t stats_ms = 0;   // Real time

    while (1) {

      usleep(1000);   // 1 ms real time

      stats_ms += 1;  // Stats expect real milliseconds

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

      static uint32_t last_stats_time = 0;

      if (stats_ms - last_stats_time >= 1000) {
        /* Integer fixed-point MB/s (avoids depending on float printf
           support in this toolchain's libc build). Counters are already
           accumulated over a ~1s window, so bytes == bytes/sec here. */
        uint32_t rx_mb_int  = bytes_rx / 1000000UL;
        uint32_t rx_mb_frac = (bytes_rx % 1000000UL) / 10000UL;
        uint32_t tx_mb_int  = bytes_tx / 1000000UL;
        uint32_t tx_mb_frac = (bytes_tx % 1000000UL) / 10000UL;

        comm_log("\r[STATS] RX: %lu pkts, %lu samples, %lu.%02lu MB/s | "
                 "TX: %lu pkts, %lu samples, %lu.%02lu MB/s\r\n",
                 packets_rx, samples_rx, rx_mb_int, rx_mb_frac,
                 packets_tx, samples_tx, tx_mb_int, tx_mb_frac);

        packets_rx = packets_tx = 0;
        samples_rx = samples_tx = 0;
        bytes_rx   = bytes_tx   = 0;

        last_stats_time = stats_ms;
      }
    }


	/* never reached */
	cleanup_platform();

	return 0;
}
