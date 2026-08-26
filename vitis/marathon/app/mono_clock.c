#include "mono_clock.h"
#include "xparameters.h"
#include "xil_io.h"
#include "sleep.h"

/* Cortex-A9 SCU global timer: PERIPHBASE 0xF8F00000 + 0x200. Fixed by the
   MPCore architecture, so no XPAR_* lookup is needed (and this SDT BSP
   does not define XPAR_GLOBAL_TMR_BASEADDR anyway). Clocked at CPU_3x2x,
   i.e. core clock / 2 = 325 MHz on this part. */
#define GTIMER_BASE       0xF8F00200U
#define GTIMER_LOWER      0x00U
#define GTIMER_UPPER      0x04U
#define GTIMER_CONTROL    0x08U
#define GTIMER_HZ         (XPAR_CPU_CORE_CLOCK_FREQ_HZ / 2U)

void mono_clock_init(void)
{
    /* ps7_init normally enables this; don't depend on it. */
    if ((Xil_In32(GTIMER_BASE + GTIMER_CONTROL) & 1U) == 0U)
        Xil_Out32(GTIMER_BASE + GTIMER_CONTROL, 1U);
}

static uint64_t gtimer_ticks(void)
{
    uint32_t hi, lo;

    /* Re-read the high word to catch a low-word rollover between reads. */
    do {
        hi = Xil_In32(GTIMER_BASE + GTIMER_UPPER);
        lo = Xil_In32(GTIMER_BASE + GTIMER_LOWER);
    } while (Xil_In32(GTIMER_BASE + GTIMER_UPPER) != hi);

    return ((uint64_t)hi << 32) | (uint64_t)lo;
}

uint64_t mono_now_ms(void)
{
    return gtimer_ticks() / (GTIMER_HZ / 1000U);
}

uint64_t mono_now_us(void)
{
    return gtimer_ticks() / (GTIMER_HZ / 1000000U);
}

uint32_t mono_clock_selftest_ms(uint32_t sleep_ms)
{
    uint64_t t0 = mono_now_ms();
    usleep(sleep_ms * 1000U);
    return (uint32_t)(mono_now_ms() - t0);
}
