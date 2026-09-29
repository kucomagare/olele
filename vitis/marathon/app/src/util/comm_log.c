#include <string.h>
#include <stdio.h>
#include <stdarg.h>
#include "xil_printf.h"
#include "comm_log.h"
#include "mono_clock.h"

#define LOG_BUF_SIZE 2048

/* DO NOT remove this budget. xil_printf() blocks until every byte clears
 * the UART (~87 us/byte @ 115200), and uncapped logging was a measured
 * livelock (2026-08-17): overflow -> resync log -> ~5.9 ms block -> more
 * overflow, loop fell from ~737k/s to 162/s, throughput to 0 pkt/s, no
 * recovery until the sender backed off. LOG_BUDGET_BYTES/LOG_WINDOW_MS
 * caps UART time to ~4.4% of wall clock regardless of how bad things get;
 * excess messages are dropped and counted, with one summary line/window. */
#define LOG_BUDGET_BYTES 512U
#define LOG_WINDOW_MS    1000U

static char log_buffer[LOG_BUF_SIZE];
static int  log_len = 0;

static uint32_t log_mask = COMM_LOG_ALL;

/* Category of a message, taken from the "[X]" prefix its format string already
   carries. Reading the tag rather than adding a parameter keeps every existing
   call site unchanged and makes it impossible for a message's category to
   disagree with the tag it prints. */
static uint32_t log_category(const char *fmt)
{
    if (fmt[0] != '[')
        return COMM_LOG_OTHER;
    switch (fmt[1]) {
    case 'S': return COMM_LOG_STATS;
    case 'E': return COMM_LOG_ERROR;
    case 'N': return COMM_LOG_NOTICE;
    /* '[CLK]' also starts with C -- check for the closing bracket too. */
    case 'C': return (fmt[2] == ']') ? COMM_LOG_CONFIG : COMM_LOG_OTHER;
    default:  return COMM_LOG_OTHER;
    }
}

void comm_log_set_mask(uint32_t mask)
{
    log_mask = mask & COMM_LOG_ALL;
}

uint32_t comm_log_get_mask(void)
{
    return log_mask;
}

static uint32_t budget_spent = 0;
static uint32_t suppressed   = 0;
static uint64_t window_start = 0;

/* Bypasses the budget -- only for the one-per-window suppression summary,
   which must never itself be dropped. */
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

    /* Checked before formatting -- a muted category costs a mask test, not a vsnprintf. */
    if (!(log_mask & log_category(fmt)))
        return;

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

        if (suppressed > 0 && (log_mask & COMM_LOG_ERROR)) {
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
