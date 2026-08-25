#ifndef MONO_CLOCK_H
#define MONO_CLOCK_H

#include <stdint.h>

/* Monotonic wall clock, read straight from the Cortex-A9 SCU global timer
   (64-bit, free-running, core-clock/2). Shared by main.c's [STATS] window
   and comm_log.c's output budget.

   Deliberately not the BSP's own time functions -- both of the obvious
   candidates are wrong here, and each cost real debugging time:

     XTime_GetTime()  resolves to the xiltimer TTC implementation
                      (XSLEEPTIMER_IS_TTCPS), which returns a *16-bit*
                      counter that wraps every ~0.6 ms.
     get_time_ms()    (platform.c) returns an ISR tick counter whose
                      interval is ~340 us, not 1 ms.

   Verify with mono_clock_selftest_ms() at boot rather than trusting any of
   this by name. */

void     mono_clock_init(void);
uint64_t mono_now_ms(void);

/* Times a known-length usleep with mono_now_ms(). Returns the measured
   duration in ms; a healthy clock returns ~the value passed in. */
uint32_t mono_clock_selftest_ms(uint32_t sleep_ms);

#endif /* MONO_CLOCK_H */
