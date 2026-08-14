#ifndef COMM_LOG_H
#define COMM_LOG_H

/* Small buffered logger: comm_log() appends formatted text to an
   in-memory buffer (cheap, safe to call from the fast TCP-recv path),
   comm_log_flush() writes it all out via xil_printf() once per main-loop
   iteration -- keeps UART output off the hot path. */

void comm_log(const char *fmt, ...);
void comm_log_flush(void);

#endif /* COMM_LOG_H */
