#ifndef COMM_LOG_H
#define COMM_LOG_H

/* Small buffered logger: comm_log() appends formatted text to an
   in-memory buffer (cheap, safe to call from the fast TCP-recv path),
   comm_log_flush() writes it all out via xil_printf() once per main-loop
   iteration -- keeps UART output off the hot path. */

/* Category bits. comm_log() picks the category from the "[X]" tag the format
   string already starts with, so no call site has to change and no message can
   drift out of sync with its own tag. Anything untagged, or tagged with
   something not listed here, counts as OTHER -- including the boot banner and
   [CLK], which is why OTHER is on by default. */
#define COMM_LOG_STATS   0x01u   /* [S] per-second throughput line          */
#define COMM_LOG_ERROR   0x02u   /* [E] errors, resyncs, suppression counts */
#define COMM_LOG_NOTICE  0x04u   /* [N] connect/reconnect/lifecycle         */
#define COMM_LOG_CONFIG  0x08u   /* [C] config packet read-backs            */
#define COMM_LOG_OTHER   0x10u   /* everything else, e.g. [CLK] and banners */
#define COMM_LOG_ALL     0x1Fu
#define COMM_LOG_NONE    0x00u

void comm_log(const char *fmt, ...);
void comm_log_flush(void);

/* Runtime UART verbosity. Set to COMM_LOG_NONE for silence.

   Worth knowing what this does and does not buy: UART output is already
   bounded to LOG_BUDGET_BYTES per second (see comm_log.c) precisely so it can
   never run away, so muting is not needed for safety. It is for keeping the
   console readable -- and for the last few percent of loop time when
   measuring, since a flush blocks the main loop until the bytes are shifted
   out. */
void comm_log_set_mask(uint32_t mask);
uint32_t comm_log_get_mask(void);

#endif /* COMM_LOG_H */
