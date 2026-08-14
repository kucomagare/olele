#include <string.h>
#include <stdio.h>
#include <stdarg.h>
#include "xil_printf.h"
#include "comm_log.h"

#define LOG_BUF_SIZE 2048

static char log_buffer[LOG_BUF_SIZE];
static int  log_len = 0;

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

    if (log_len + n < LOG_BUF_SIZE) {
        memcpy(log_buffer + log_len, tmp, n);
        log_len += n;
    }
}

void comm_log_flush(void)
{
    if (log_len > 0) {
        log_buffer[log_len] = '\0';
        xil_printf("%s", log_buffer);
        log_len = 0;
    }
}
