#include "dma_stream.h"
#include "packet_format.h"
#include "comm_log.h"

#include "xparameters.h"
#include "xaxidma.h"
#include "xil_cache.h"
#include "xil_io.h"

/* ============================================================
   HARDWARE ADDRESSES
   ============================================================
   Must match the assign_bd_address calls in
   vivado/marathon/bd_CoraZ7_Eth.tcl. The real-time group is kept
   contiguous in 0x40000000-0x4001FFFF on purpose -- see
   research_info/DMA_talk_260825.txt section 9. */
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

/* ============================================================
   BUFFERS
   ============================================================ */

#define DMA_BUF_BYTES  (DMA_FRAMES_PER_BUF * (uint32_t)sizeof(packet_data_t))

/* Header padding. The obvious trick -- DMA into rx_buf+4 so the 4-byte wire
   header sits in front of the payload and tcp_write() is a single call on a
   single contiguous buffer -- breaks cache alignment, because the DMA
   destination would no longer be 32-byte aligned and Xil_DCacheInvalidateRange
   would spill onto the neighbouring bytes. Reserving 32 bytes instead keeps
   the DMA target aligned AND leaves room to write the header immediately
   before the payload, at offset DMA_HDR_PAD-4. */
#define DMA_HDR_PAD    32u

/* Two buffer pairs is true ping-pong: while the DMA chews on one, the CPU
   fills the other. In simple mode the DMA allows only one outstanding
   transfer per channel, so this overlaps CPU work with DMA work rather than
   queueing transfers -- there is a small gap between them, invisible at
   these rates.
 *
 * NOTE: two is only safe because tcp_write() is called with
 * TCP_WRITE_FLAG_COPY. Without that flag lwIP keeps a REFERENCE to the
 * buffer until the PC ACKs, and a retransmit or window stall would let the
 * DMA overwrite data still being sent -- corrupt samples on the wire with
 * nothing in the logs. Going zero-copy means raising this to 4. */
#define DMA_NBUF 2

static uint8_t tx_buf[DMA_NBUF][DMA_BUF_BYTES] __attribute__((aligned(32)));
static uint8_t rx_buf[DMA_NBUF][DMA_HDR_PAD + DMA_BUF_BYTES] __attribute__((aligned(32)));

/* ============================================================
   STATE
   ============================================================ */

typedef enum { DMA_ST_IDLE = 0, DMA_ST_RUNNING, DMA_ST_DONE } dma_state_t;

static XAxiDma    axi_dma;
static int        dma_ready = 0;
static dma_state_t state    = DMA_ST_IDLE;
static int        cur       = 0;
static uint32_t   cur_bytes = 0;
static uint16_t   cur_type  = 0;
static uint16_t   cur_len   = 0;

/* Round a length up to a whole number of 32-byte cache lines. Safe to
   over-flush/over-invalidate here because the extra bytes are always inside
   our own buffer -- the buffers are 32-byte aligned and sized in whole
   frames, which for 32-bit slots is already a multiple of 32. */
static inline uint32_t cache_len(uint32_t n)
{
    return (n + 31u) & ~31u;
}

/* ============================================================
   FILTER CONTROL
   ============================================================ */

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

/* ============================================================
   INIT
   ============================================================ */

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

    /* Scatter-gather is disabled in the block design (a build-time
       parameter, not a runtime mode). If this ever trips, the bitstream and
       this firmware have drifted apart. */
    if (XAxiDma_HasSg(&axi_dma)) {
        comm_log("[E] dma has sg\r\n");
        return -1;
    }

    /* Polled operation: completion is checked once per main-loop pass.
       Interrupts are wired to their own IRQ_F2P bits in the block design
       and can be turned on later without a hardware change. */
    XAxiDma_IntrDisable(&axi_dma, XAXIDMA_IRQ_ALL_MASK, XAXIDMA_DEVICE_TO_DMA);
    XAxiDma_IntrDisable(&axi_dma, XAXIDMA_IRQ_ALL_MASK, XAXIDMA_DMA_TO_DEVICE);

    /* Byte-swap ON: samples arrive from the PC big-endian, and doing the
       swap in fabric is pure rewiring, so the CPU never touches a sample.
       This is what makes the buffer that lands in DDR identical to what
       goes back out on the wire -- no repacking loop at all. */
    dma_stream_set_filter(TDM_DEFAULT_NCHAN, TDM_DEFAULT_SHIFT, TDM_CTRL_SWAP);

    dma_ready = 1;
    state     = DMA_ST_IDLE;
    cur       = 0;

    comm_log("[N] dma ok n=%u sh=%u\r\n",
             (unsigned)TDM_DEFAULT_NCHAN, (unsigned)TDM_DEFAULT_SHIFT);
    return 0;
}

/* ============================================================
   SUBMIT / COMPLETE
   ============================================================ */

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

    /* The A9's caches are NOT coherent with the HP ports: without this the
       payload may still be sitting in L1/L2 where the DMA cannot see it. */
    Xil_DCacheFlushRange((UINTPTR)tx_buf[cur], cache_len(nbytes));

    /* Invalidate the destination BEFORE the transfer as well as after. A
       speculative fill between arming and completion would otherwise leave a
       stale line that the post-transfer invalidate cannot distinguish from
       fresh data. */
    Xil_DCacheInvalidateRange((UINTPTR)(rx_buf[cur] + DMA_HDR_PAD), cache_len(nbytes));

    /* Arm the SINK before opening the tap. Starting MM2S against an unarmed
       S2MM backpressures the stream -- tready stays low and data piles up in
       the filter pipeline. It usually recovers, but it is an unpredictable
       stall inside the datapath for no reason. */
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

    /* Both directions must finish. S2MM is the one that matters -- if tlast
       never arrives it simply never completes, with no error and no timeout,
       which is why the filter carries tlast through explicitly. */
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
