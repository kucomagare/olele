#ifndef COMM_LOG_H
#define COMM_LOG_H

/* Buffered logger: comm_log() appends to an in-memory buffer (cheap, safe
   from the fast TCP-recv path); comm_log_flush() writes it via xil_printf()
   once per main-loop pass, keeping UART off the hot path. */

/* Category is read from the format string's own "[X]" tag, so no call site
   changes and a message can't drift from its tag. Untagged (boot banner,
   [CLK]) counts as OTHER, on by default. */
#define COMM_LOG_STATS   0x01u   /* [S] per-second throughput line          */
#define COMM_LOG_ERROR   0x02u   /* [E] errors, resyncs, suppression counts */
#define COMM_LOG_NOTICE  0x04u   /* [N] connect/reconnect/lifecycle         */
#define COMM_LOG_CONFIG  0x08u   /* [C] config packet read-backs            */
#define COMM_LOG_OTHER   0x10u   /* everything else, e.g. [CLK] and banners */
#define COMM_LOG_ALL     0x1Fu
#define COMM_LOG_NONE    0x00u

void comm_log(const char *fmt, ...);
void comm_log_flush(void);

/* Runtime UART verbosity, COMM_LOG_NONE for silence. UART output is already
   budget-capped (comm_log.c) so muting isn't needed for safety -- it's for
   console readability and shaving the last bit of loop time when a flush's
   block would otherwise skew a measurement. */
void comm_log_set_mask(uint32_t mask);
uint32_t comm_log_get_mask(void);

#endif /* COMM_LOG_H */
