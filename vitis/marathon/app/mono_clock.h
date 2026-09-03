#ifndef MONO_CLOCK_H
#define MONO_CLOCK_H

#include <stdint.h>

/* Monotonic wall clock from the Cortex-A9 SCU global timer (64-bit,
   free-running, core-clock/2). Shared by main.c's [STATS] window and
   comm_log.c's output budget.

   DO NOT switch to the BSP's own time functions -- both looked right and
   cost real debugging time: XTime_GetTime() resolves to a *16-bit* TTC
   counter wrapping every ~0.6 ms; get_time_ms() (platform.c) has a ~340 us
   tick interval, not 1 ms. Verify with mono_clock_selftest_ms() at boot
   rather than trusting either by name. */

void     mono_clock_init(void);
uint64_t mono_now_ms(void);

/* Same counter, microsecond resolution -- for per-packet service-latency
   measurement, where milliseconds are too coarse. */
uint64_t mono_now_us(void);

/* Times a known-length usleep with mono_now_ms(). Returns the measured
   duration in ms; a healthy clock returns ~the value passed in. */
uint32_t mono_clock_selftest_ms(uint32_t sleep_ms);

#endif /* MONO_CLOCK_H */
