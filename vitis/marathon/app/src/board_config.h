#ifndef BOARD_CONFIG_H
#define BOARD_CONFIG_H

/* Every constant this firmware shares with something outside the ELF.
   Nothing checks these against their partners at build time -- change one
   here, change the partner named next to it in the same commit. */

/* ---- Network: partners in pc_app/marathon/ -------------------------------- */

/* tcp_server_app.cpp PCB_IP -- the relay tells board from PC by this address,
   so it must be static. */
#define BOARD_IP_ADDRESS    "192.168.1.10"
#define BOARD_IP_MASK       "255.255.255.0"
#define BOARD_GW_ADDRESS    "192.168.1.1"
#define BOARD_MAC_ADDRESS   { 0x00, 0x0a, 0x35, 0x00, 0x01, 0x02 }

/* The PC running tcp_server_app: config.py HOST, tcp_server_app.cpp SERVER_PORT. */
#define PC_IP_ADDR(ip)      IP4_ADDR((ip), 192, 168, 1, 100)
#define PC_TCP_PORT         5001

/* Largest packet, in records: tcp_server_app.cpp MAX_SAMPLES. */
#define MAX_PAYLOAD_SAMPLES 2000

/* ---- PL peripherals: partner is assign_bd_address in
        vivado/marathon/bd_CoraZ7_Eth.tcl ------------------------------------ */

/* The real-time group is kept contiguous in 0x40000000-0x4001FFFF on purpose,
   see research_info/dma-architecture.md "AMP-ready block design". */
#define TDM_FILTER_BASE     0x40000000u   /* axi_tdm_filter,     user_top s00_axi */
#define AXI_CH1_BASE        0x40001000u   /* axi_processing_ch1, user_top s01_axi */
#define AXI_CH2_BASE        0x40002000u   /* axi_processing_ch2, user_top s02_axi */
/* axi_dma_0 (0x40010000) is not here: it's an IP core with a driver, so it
   comes from the generated XPAR_AXI_DMA_0_BASEADDR instead. */

#endif /* BOARD_CONFIG_H */
