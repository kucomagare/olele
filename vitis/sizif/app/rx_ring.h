#ifndef RX_RING_H
#define RX_RING_H

#include <stdint.h>

/* Circular byte buffer between the fast lwIP recv callback (pushes) and
   comm_process() (peeks/parses full packets at its own pace). No
   protocol/AXI knowledge here on purpose -- generic, independently testable. */

uint32_t rx_ring_used(void);
uint32_t rx_ring_free(void);

/* Bulk copy into the ring, wrapping at most once. `len` must not exceed
   the ring's total capacity. */
void rx_ring_push(const uint8_t *data, uint32_t len);

/* Copy `len` bytes starting `offset` bytes ahead of the read position
   into dst, without consuming them. */
void rx_ring_peek(uint32_t offset, uint8_t *dst, uint32_t len);

/* Consume `len` bytes from the read position (must have been peeked
   already, or be known-valid raw bytes to skip). */
void rx_ring_advance(uint32_t len);

/* Discard everything -- call at the start of a fresh TCP connection so
   leftover bytes from a connection cut mid-packet don't get prepended to
   the next connection's stream and desync framing permanently. */
void rx_ring_reset(void);

/* NOTE: deliberately no "drop oldest N bytes to make room" call. One
   (rx_ring_make_room) used to exist and caused an unrecoverable failure
   under overload -- dropping bytes from a length-prefixed stream destroys
   header alignment permanently. Only safe response to a full ring is
   discard-all + resync (tcp_client_resync()). DO NOT reintroduce. */

#endif /* RX_RING_H */
