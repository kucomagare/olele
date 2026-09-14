#ifndef PACKET_CODEC_H
#define PACKET_CODEC_H

#include <stdint.h>
#include "packet_format.h"

/* Wire-format helpers shared by the receive (comm_process.c) and send
   (tcp_link.c) sides. Struct layouts and field widths come from the
   generated packet_format.h -- edit shared/marathon/packet_format.json. */

#define MAX_PAYLOAD_SAMPLES 2000

/* Bounds the static payload buffers in comm_process.c and tcp_link.c
   regardless of which type arrives -- sized off the biggest packet type in
   packet_format.h ("data"). */
#define MAX_PAYLOAD_BYTES (MAX_PAYLOAD_SAMPLES * sizeof(packet_data_t))

/* Big-endian <-> host swap of one record, in place. Self-inverse. */
void swap_be_fields(uint8_t *record, uint16_t type);

#endif /* PACKET_CODEC_H */
