#include "dma_stream.h"
#include "packet_format.h"
#include "comm_log.h"

#include "xparameters.h"
#include "xaxidma.h"
#include "xil_cache.h"
#include "xil_io.h"

/* Hardware addresses -- must match assign_bd_address in
   vivado/marathon/bd_CoraZ7_Eth.tcl. Real-time group kept contiguous in
   0x40000000-0x4001FFFF on purpose, see research_info/dma-architecture.md
   "AMP-ready block design". */
#define TDM_FILTER_BASE   0x40000000u
#define TDM_REG_NCHAN     0x0u   /* channels per frame (frame = 1 + N slots) */
#define TDM_REG_SHIFT     0x4u   /* alpha = 1/2**SHIFT; 0 == bypass          */
#define TDM_REG_CTRL      0x8u   /* bit0 byte-swap in fabric, bit1 clear     */
#define TDM_REG_STATUS    0xCu   /* read-only status word                    */

#define TDM_CTRL_SWAP     0x1u
#define TDM_CTRL_CLEAR    0x2u

/* Defaults programmed at init. SHIFT matches the SHIFT=4 generic the
   AXI-Lite chains were synthesised with, so the DMA path and the legacy
   path produce comparable output and can be A/B'd against each other. */
#define TDM_DEFAULT_NCHAN 2u
#define TDM_DEFAULT_SHIFT 4u

#define DMA_BUF_BYTES  (DMA_FRAMES_PER_BUF * (uint32_t)sizeof(packet_data_t))

/* DMA'ing straight into rx_buf+4 (header then payload, one contiguous
   tcp_write) breaks 32-byte cache alignment. Reserving 32 bytes keeps the
   DMA target aligned and still leaves room to write the header at
   DMA_HDR_PAD-4, right before the payload. */
#define DMA_HDR_PAD    32u

/* True ping-pong: DMA chews one buffer while the CPU fills the other (simple
   mode allows only one outstanding transfer per channel, so this overlaps
   CPU work with DMA work, not DMA with DMA -- small gap between transfers,
   invisible at these rates).
 *
 * 2 is only safe because tcp_write() uses TCP_WRITE_FLAG_COPY. Without it
 * lwIP keeps a REFERENCE until the PC ACKs, and a retransmit/window stall
 * would let the DMA overwrite data still being sent -- corrupt samples,
 * nothing in the logs. Going zero-copy means raising this to 4. */
#define DMA_NBUF 2

static uint8_t tx_buf[DMA_NBUF][DMA_BUF_BYTES] __attribute__((aligned(32)));
static uint8_t rx_buf[DMA_NBUF][DMA_HDR_PAD + DMA_BUF_BYTES] __attribute__((aligned(32)));

typedef enum { DMA_ST_IDLE = 0, DMA_ST_RUNNING, DMA_ST_DONE } dma_state_t;

static XAxiDma    axi_dma;
static int        dma_ready = 0;
static dma_state_t state    = DMA_ST_IDLE;
static int        cur       = 0;
static uint32_t   cur_bytes = 0;
static uint16_t   cur_type  = 0;
static uint16_t   cur_len   = 0;

/* Round up to a whole number of 32-byte cache lines. Safe to over-flush/
   over-invalidate: buffers are 32-byte aligned and sized in whole frames,
   already a multiple of 32 for 32-bit slots. */
static inline uint32_t cache_len(uint32_t n)
{
    return (n + 31u) & ~31u;
}

void dma_stream_set_filter(uint32_t n_channels, uint32_t shift, uint32_t ctrl)
{
    Xil_Out32(TDM_FILTER_BASE + TDM_REG_NCHAN, n_channels);
    Xil_Out32(TDM_FILTER_BASE + TDM_REG_SHIFT, shift);
    Xil_Out32(TDM_FILTER_BASE + TDM_REG_CTRL,  ctrl);
}

void dma_stream_get_filter(uint32_t *n_channels, uint32_t *shift,
                           uint32_t *ctrl, uint32_t *status)
{
    if (n_channels) *n_channels = Xil_In32(TDM_FILTER_BASE + TDM_REG_NCHAN);
    if (shift)      *shift      = Xil_In32(TDM_FILTER_BASE + TDM_REG_SHIFT);
    if (ctrl)       *ctrl       = Xil_In32(TDM_FILTER_BASE + TDM_REG_CTRL);
    if (status)     *status     = Xil_In32(TDM_FILTER_BASE + TDM_REG_STATUS);
}

int dma_stream_init(void)
{
    XAxiDma_Config *cfg;

    cfg = XAxiDma_LookupConfig(XPAR_AXI_DMA_0_BASEADDR);
    if (cfg == NULL) {
        comm_log("[E] dma cfg\r\n");
        return -1;
    }

    if (XAxiDma_CfgInitialize(&axi_dma, cfg) != XST_SUCCESS) {
        comm_log("[E] dma init\r\n");
        return -1;
    }

    /* SG is disabled in the block design (build-time param) -- tripping
       here means the bitstream and this firmware have drifted apart. */
    if (XAxiDma_HasSg(&axi_dma)) {
        comm_log("[E] dma has sg\r\n");
        return -1;
    }

    /* Polled: completion checked once per main-loop pass. IRQ_F2P bits are
       wired regardless, so interrupts can be turned on later in software. */
    XAxiDma_IntrDisable(&axi_dma, XAXIDMA_IRQ_ALL_MASK, XAXIDMA_DEVICE_TO_DMA);
    XAxiDma_IntrDisable(&axi_dma, XAXIDMA_IRQ_ALL_MASK, XAXIDMA_DMA_TO_DEVICE);

    /* Byte-swap ON: PC sends big-endian, swap in fabric is free rewiring,
       so the DDR buffer ends up byte-identical to the outgoing wire frame
       -- no CPU repacking loop. */
    dma_stream_set_filter(TDM_DEFAULT_NCHAN, TDM_DEFAULT_SHIFT, TDM_CTRL_SWAP);

    dma_ready = 1;
    state     = DMA_ST_IDLE;
    cur       = 0;

    comm_log("[N] dma ok n=%u sh=%u\r\n",
             (unsigned)TDM_DEFAULT_NCHAN, (unsigned)TDM_DEFAULT_SHIFT);
    return 0;
}

int dma_stream_busy(void)
{
    return (state != DMA_ST_IDLE);
}

uint8_t *dma_stream_tx_buf(void)
{
    return tx_buf[cur];
}

int dma_stream_start(uint32_t nbytes, uint16_t type, uint16_t length)
{
    if (!dma_ready || state != DMA_ST_IDLE)
        return -1;
    if (nbytes == 0 || nbytes > DMA_BUF_BYTES)
        return -1;

    /* A9 caches aren't coherent with the HP ports -- without this the
       payload may still be sitting in L1/L2, invisible to the DMA. */
    Xil_DCacheFlushRange((UINTPTR)tx_buf[cur], cache_len(nbytes));

    /* Invalidate before AND after the transfer: a speculative fill between
       arming and completion would leave a stale line the post-transfer
       invalidate can't tell apart from fresh data. */
    Xil_DCacheInvalidateRange((UINTPTR)(rx_buf[cur] + DMA_HDR_PAD), cache_len(nbytes));

    /* Arm the sink before opening the tap -- MM2S against an unarmed S2MM
       backpressures the stream (tready low, pipeline fills) and stalls
       unpredictably even though it usually recovers. */
    if (XAxiDma_SimpleTransfer(&axi_dma,
                               (UINTPTR)(rx_buf[cur] + DMA_HDR_PAD), nbytes,
                               XAXIDMA_DEVICE_TO_DMA) != XST_SUCCESS) {
        comm_log("[E] s2mm start\r\n");
        return -1;
    }

    if (XAxiDma_SimpleTransfer(&axi_dma,
                               (UINTPTR)tx_buf[cur], nbytes,
                               XAXIDMA_DMA_TO_DEVICE) != XST_SUCCESS) {
        comm_log("[E] mm2s start\r\n");
        return -1;
    }

    cur_bytes = nbytes;
    cur_type  = type;
    cur_len   = length;
    state     = DMA_ST_RUNNING;
    return 0;
}

int dma_stream_poll(uint8_t **out, uint32_t *out_bytes,
                    uint16_t *type, uint16_t *length)
{
    if (state != DMA_ST_RUNNING)
        return 0;

    /* S2MM is the one that matters -- if tlast never arrives it never
       completes, no error, no timeout, hence the filter pipes tlast
       through explicitly rather than wiring it straight across. */
    if (XAxiDma_Busy(&axi_dma, XAXIDMA_DEVICE_TO_DMA) ||
        XAxiDma_Busy(&axi_dma, XAXIDMA_DMA_TO_DEVICE))
        return 0;

    Xil_DCacheInvalidateRange((UINTPTR)(rx_buf[cur] + DMA_HDR_PAD), cache_len(cur_bytes));

    /* Write the wire header immediately in front of the payload the DMA just
       produced, so the whole thing is one contiguous tcp_write(). */
    uint8_t *hdr = rx_buf[cur] + DMA_HDR_PAD - 4u;
    hdr[0] = (uint8_t)((cur_type   >> 8) & 0xFF);
    hdr[1] = (uint8_t)( cur_type         & 0xFF);
    hdr[2] = (uint8_t)((cur_len    >> 8) & 0xFF);
    hdr[3] = (uint8_t)( cur_len          & 0xFF);

    *out       = hdr;
    *out_bytes = cur_bytes + 4u;
    *type      = cur_type;
    *length    = cur_len;

    state = DMA_ST_DONE;
    return 1;
}

void dma_stream_release(void)
{
    if (state != DMA_ST_DONE)
        return;
    cur   = (cur + 1) % DMA_NBUF;
    state = DMA_ST_IDLE;
}
