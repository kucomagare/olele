#include <string.h>
#include <stdio.h>
#include <stdarg.h>
#include "xil_printf.h"
#include "comm_log.h"
#include "mono_clock.h"

#define LOG_BUF_SIZE 2048

/* Output budget -- the important part of this file.
 *
 * comm_log_flush() calls xil_printf(), which BLOCKS until every byte has
 * been shifted out the UART. At 115200 baud that is ~87 us per byte, so a
 * 68-byte reconnect message costs ~5.9 ms of dead CPU.
 *
 * Without a cap that is a livelock, observed on hardware 2026-08-17:
 * offering more than the board could take filled the RX ring, each
 * overflow logged a resync, each log blocked the main loop for ~5.9 ms,
 * and the stall caused more overflow. The loop fell from ~737,000
 * passes/s to 162/s and throughput to 0 pkt/s, and it could not recover
 * until the sender backed off -- the diagnostics had displaced all the
 * work they were describing.
 *
 * So: at most LOG_BUDGET_BYTES per LOG_WINDOW_MS actually reach the UART.
 * At 512 B/s that bounds logging at ~44 ms/s, i.e. ~4.4% of wall time, no
 * matter how badly things are going. Excess messages are dropped and
 * counted, and one short summary line per window reports the count -- the
 * first message of a burst (the informative one) always gets through, and
 * you can still tell that something was hidden.
 */
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
    /* '[C]' is config; '[CLK]' also starts with C, hence the closing-bracket
       check rather than matching on the letter alone. */
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

/* Append without consulting the budget. Only for the suppression summary,
   which is short, bounded to one per window, and is precisely the message
   you must not drop. */
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

    /* Checked before formatting: a muted category should cost a mask test,
       not a vsnprintf. */
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
