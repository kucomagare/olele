#ifndef COMM_STATS_H
#define COMM_STATS_H

#include <stdint.h>
#include "packet_format.h"

/* Everything measured about the stream: throughput counters, resync count,
   per-packet service latency, and the once-per-second report that turns
   them into the [S] UART line and the metrics packet for the PC. */

/* Throughput counters for the current 1 s window, cleared by the report. */
extern uint32_t packets_rx;
extern uint32_t packets_tx;
extern uint32_t samples_rx;
extern uint32_t samples_tx;
extern uint32_t bytes_rx;
extern uint32_t bytes_tx;

extern uint32_t comm_resyncs;

/* Adds one service-time sample measured from t0 (mono_now_us()) to now.
   t0 == 0 means "no packet in flight" and is ignored. */
void latency_record(uint64_t t0);

/* Print [S], send the metrics packet, clear the window counters.
   Called from main() once at least 1000 ms have elapsed. */
void comm_stats_report(uint64_t stats_now, uint32_t elapsed,
                       uint32_t loop_passes, uint32_t ring_peak);

#endif /* COMM_STATS_H */
