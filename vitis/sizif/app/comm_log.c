#include <string.h>
#include <stdio.h>
#include <stdarg.h>
#include "xil_printf.h"
#include "comm_log.h"
#include "mono_clock.h"

#define LOG_BUF_SIZE 2048

/* xil_printf() BLOCKS until every byte clears the UART (~87us/byte @115200).
 * Uncapped, an overflow-logging burst livelocked hw (2026-08-17): logging
 * stalled the loop -> ring grew -> more overflow logged -> loop fell
 * 737k/s -> 162/s, no recovery without sender backoff. Fix: cap UART output
 * to LOG_BUDGET_BYTES/LOG_WINDOW_MS, drop+count the rest, one "+N
 * suppressed" summary per window so a hidden burst stays visible.
 */
#define LOG_BUDGET_BYTES 512U
#define LOG_WINDOW_MS    1000U

static char log_buffer[LOG_BUF_SIZE];
static int  log_len = 0;

static uint32_t budget_spent = 0;
static uint32_t suppressed   = 0;
static uint64_t window_start = 0;

/* Bypasses the budget -- only for the suppression summary itself, which
   must never be the thing that gets dropped. */
static void log_append_raw(const char *s, int n)
{
    if (n > 0 && log_len + n < LOG_BUF_SIZE) {
        memcpy(log_buffer + log_len, s, n);
        log_len += n;
    }
}

void comm_log(const char *fmt, ...)
{
    va_list args;
    char tmp[256];
    int n;

    va_start(args, fmt);
    n = vsnprintf(tmp, sizeof(tmp), fmt, args);
    va_end(args);

    if (n <= 0)
        return;

    if (budget_spent + (uint32_t)n > LOG_BUDGET_BYTES) {
        suppressed++;
        return;
    }
    budget_spent += (uint32_t)n;

    log_append_raw(tmp, n);
}

void comm_log_flush(void)
{
    uint64_t now = mono_now_ms();

    if (window_start == 0)
        window_start = now;

    if (now - window_start >= LOG_WINDOW_MS) {
        window_start = now;
        budget_spent = 0;

        if (suppressed > 0) {
            char tmp[64];
            int n = snprintf(tmp, sizeof(tmp),
                             "[E] +%lu suppressed\r\n",
                             (unsigned long)suppressed);
            log_append_raw(tmp, n);
            suppressed = 0;
        }
    }

    if (log_len > 0) {
        log_buffer[log_len] = '\0';
        xil_printf("%s", log_buffer);
        log_len = 0;
    }
}
