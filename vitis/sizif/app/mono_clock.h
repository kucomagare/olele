#ifndef MONO_CLOCK_H
#define MONO_CLOCK_H

#include <stdint.h>

/* Monotonic wall clock: SCU global timer (64-bit, core-clock/2). NOT the
   BSP's XTime_GetTime() (16-bit TTC, wraps ~0.6ms) or get_time_ms()
   (platform.c, ~340us ticks, not 1ms) -- both measured wrong, cost real
   debugging time. Verify with mono_clock_selftest_ms() at boot. */

void     mono_clock_init(void);
uint64_t mono_now_ms(void);

/* Times a usleep(sleep_ms) with mono_now_ms(); healthy clock returns ~sleep_ms. */
uint32_t mono_clock_selftest_ms(uint32_t sleep_ms);

#endif /* MONO_CLOCK_H */
