#ifndef DMA_STREAM_H
#define DMA_STREAM_H

#include <stdint.h>

/* Buffer-at-a-time DMA path through the PL, replacing the per-sample
 * AXI-Lite round trips in axi_processing.c. Point is determinism, not
 * throughput -- DMA runs in fabric at a fixed rate, CPU touches it once
 * per buffer instead of inheriting scheduler jitter every sample.
 *
 * Main-loop usage (never blocks): poll() a finished buffer and release()
 * it; if not busy(), fill tx_buf() and start() the next transfer.
 */

/* Must be a multiple of 8: a frame is 4*(N+1) bytes at 32-bit slots, so 8
   frames = 32*(N+1) bytes, satisfying both the 32-byte cache-line rule and
   "whole number of frames" for any N. Get it wrong and channel assignment
   silently rotates on the next buffer. */
#define DMA_FRAMES_PER_BUF 2000

/* Locate/init the AXI DMA and program the TDM filter's control registers.
   0 on success; caller falls back to the AXI-Lite path on failure. */
int dma_stream_init(void);

/* Non-zero if a transfer is in flight or a result awaits collection --
   dma_stream_start() would be refused. */
int dma_stream_busy(void);

/* Where to place payload bytes before dma_stream_start(). 32-byte aligned,
   valid for up to DMA_FRAMES_PER_BUF frames. */
uint8_t *dma_stream_tx_buf(void);

/* Flush TX out of cache, arm S2MM, start MM2S. type/length pass through
   untouched for the completion side to rebuild the wire header. */
int dma_stream_start(uint32_t nbytes, uint16_t type, uint16_t length);

/* Returns 1 when done. *out is a contiguous block starting with the 4-byte
   wire header -- straight to tcp_write(), no repacking. */
int dma_stream_poll(uint8_t **out, uint32_t *out_bytes,
                    uint16_t *type, uint16_t *length);

/* Caller is done with the buffer dma_stream_poll() handed out. */
void dma_stream_release(void);

/* Runtime filter control (vivado/marathon/hdl/axi_tdm_filter.vhd). shift 0
   == bypass. */
void dma_stream_set_filter(uint32_t n_channels, uint32_t shift, uint32_t ctrl);

/* Real read-back, not a copy of the last write -- a value the hardware
   clamped/ignored/never received shows up as a mismatch instead of being
   silently reported as applied. status = STATUS word. NULL ptrs OK. */
void dma_stream_get_filter(uint32_t *n_channels, uint32_t *shift,
                           uint32_t *ctrl, uint32_t *status);

#endif /* DMA_STREAM_H */
