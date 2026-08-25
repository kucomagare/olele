#ifndef DMA_STREAM_H
#define DMA_STREAM_H

#include <stdint.h>

/* Buffer-at-a-time DMA path through the PL, replacing the per-sample
   AXI-Lite round trips in axi_processing.c.
 *
 * The point is NOT throughput -- at EEG rates the AXI-Lite path is already
 * ~300x oversized. It is DETERMINISM: a per-sample CPU loop is at the mercy
 * of the scheduler, so its cadence becomes a function of whatever else the
 * system is doing. With DMA the data path runs in fabric at a fixed rate and
 * the CPU only appears once per buffer, late-but-correct.
 *
 * Usage from the main loop (never blocks):
 *
 *     if (dma_stream_poll(&out, &n, &type, &len)) {
 *         ... send out[0..n) ...
 *         dma_stream_release();
 *     }
 *     if (!dma_stream_busy()) {
 *         memcpy(dma_stream_tx_buf(), payload, nbytes);
 *         dma_stream_start(nbytes, type, len);
 *     }
 */

/* Frames per DMA buffer. Must be a multiple of 8: a frame is 4*(N+1) bytes
   with 32-bit TDM slots, so 8 frames is always 32*(N+1) bytes -- satisfying
   BOTH the 32-byte cache-line requirement and the "whole number of frames"
   requirement, for any channel count. Get this wrong and channel assignment
   silently rotates on the next buffer. */
#define DMA_FRAMES_PER_BUF 2000

/* One-time setup: locate and initialise the AXI DMA, then program the TDM
   filter's control registers. Returns 0 on success, non-zero on failure
   (in which case the caller should fall back to the AXI-Lite path). */
int dma_stream_init(void);

/* Non-zero if a transfer is in flight or a result is waiting to be
   collected -- i.e. dma_stream_start() would be refused. */
int dma_stream_busy(void);

/* Where to place payload bytes before calling dma_stream_start(). Always
   32-byte aligned. Valid for up to DMA_FRAMES_PER_BUF frames. */
uint8_t *dma_stream_tx_buf(void);

/* Flush the TX buffer out of cache, arm S2MM, then start MM2S. type/length
   are carried through untouched so the completion side can rebuild the wire
   header. Returns 0 on success. */
int dma_stream_start(uint32_t nbytes, uint16_t type, uint16_t length);

/* Returns 1 when a transfer has completed. *out points at a contiguous
   block starting with the 4-byte wire header, so it can go straight to
   tcp_write() with no repacking; *out_bytes covers header + payload. */
int dma_stream_poll(uint8_t **out, uint32_t *out_bytes,
                    uint16_t *type, uint16_t *length);

/* Caller is done with the buffer dma_stream_poll() handed out. */
void dma_stream_release(void);

/* Runtime filter control (see vivado/marathon/hdl/axi_tdm_filter.vhd).
   shift 0 == bypass. */
void dma_stream_set_filter(uint32_t n_channels, uint32_t shift, uint32_t ctrl);

#endif /* DMA_STREAM_H */
