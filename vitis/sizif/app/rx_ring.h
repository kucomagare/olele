#ifndef RX_RING_H
#define RX_RING_H

#include <stdint.h>

/* Generic circular byte buffer for staging inbound TCP bytes between the
   fast lwIP recv callback (just pushes) and comm_process() (peeks/parses
   full packets out at its own pace, since a TCP recv callback can hand
   over a partial packet). No protocol/AXI knowledge here on purpose --
   keep it that way, it's the one piece of this that's fully generic and
   independently testable. */

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

/* Drop the oldest bytes to guarantee at least `needed` bytes are free
   (drops less if the ring doesn't have that many bytes used). Returns
   the number of bytes actually dropped, 0 if nothing needed dropping. */
uint32_t rx_ring_make_room(uint32_t needed);

#endif /* RX_RING_H */
