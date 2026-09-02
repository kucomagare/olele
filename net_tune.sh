#!/usr/bin/env bash
# Host-side network tuning for streaming to the Cora Z7 over Ethernet.
#
#   ./proj net apply       apply the tuning (default)
#   ./proj net show        print the current route flags and exit
#   ./proj net revert      remove the flags this script adds
#   ./proj net loss        measure the current packet loss to the board
#
# WHY THIS EXISTS
# ---------------
# Streaming marathon at 1400 pkt/s x 960 samples (16.1 MB/s each way) was
# wildly unstable without this: the board saw anywhere from 196 to 1997 pkt/s
# and the Python client logged continuous "dropped late" and "send-stalls".
# With it, both ends lock to 1399-1401 pkt/s and 16.13 MB/s with zero drops
# and zero stalls. Measured 2026-08-26.
#
# The settings are kernel state, not files, so they are LOST ON REBOOT. Re-run
# this after every boot before doing any throughput work -- otherwise the
# fluctuation comes back and looks like a board or firmware problem, which is
# exactly the wrong place to go looking.
#
# WHAT IT ACTUALLY DOES, AND WHAT IT DELIBERATELY DOES NOT
# --------------------------------------------------------
# Only one change, because only one change was measured to help: three flags
# on the route to the board.
#
#   quickack 1   Almost certainly the active ingredient. Delayed ACKs (ato:40)
#                interacting with loss recovery are what turned the link's
#                standing packet loss into multi-hundred-millisecond stalls
#                that reached the application. Disabling the delay lets TCP
#                absorb the same loss invisibly.
#   initcwnd 50  Start sending 50 segments rather than 10, so a fresh
#   initrwnd 50  connection does not spend its first seconds climbing out of
#                slow-start against lwIP's 65535-byte window ceiling.
#
# Note what is NOT here. cwnd sits at 6 both before and after, and at the
# ~0.37 ms LAN RTT that already supports ~23 MB/s -- the congestion window was
# never the constraint, so nothing here tries to raise it.
#
# There is a similar script at tcp_client_lwip/cpp_app/tcp_config.sh. Do not
# use it for olele: it belongs to a different project, was tuned for a
# different workload (one-directional iperf-style perf client, ~300 Mbit), and
# its own route line omits the metric, so on this machine it fails silently
# with "No such process" and applies nothing. Its other settings (rx-usecs,
# txqueuelen, rmem/wmem sysctls, offload flags) are untested against olele's
# bidirectional 16 MB/s echo and are deliberately not copied here. Add them
# only with a measurement showing they help.
#
# KNOWN REMAINING LIMIT
# ---------------------
# The link to the board carries a standing ~0.8% packet loss (1.31 MB
# retransmitted out of 162.8 MB over 10 s). This script does not change that;
# it stops it from reaching the application. That loss is the ceiling on
# pushing the rate further, and it points at the board's lwIP receive path
# (EMAC rx descriptor count, pbuf pool sizing in the BSP), not at the PC.
# Use `./proj net loss` to check it.
set -euo pipefail

BOARD_IP="${BOARD_IP:-192.168.1.10}"
FLAGS=(initcwnd 50 initrwnd 50 quickack 1)
LOSS_SECONDS="${LOSS_SECONDS:-10}"

# Resolve the interface from the board address rather than hardcoding it, so
# this keeps working if the NIC is renamed or the board moves to another port.
resolve_route() {
    DEV="$(ip -o route get "$BOARD_IP" 2>/dev/null | grep -o 'dev [^ ]*' | awk '{print $2}')"
    if [[ -z "${DEV:-}" ]]; then
        echo "No route to $BOARD_IP -- is the board plugged in and the link up?" >&2
        exit 1
    fi
    # The specific subnet route, never the default: these flags should apply to
    # the board only, not to every host this machine talks to.
    # NOT 'ip route show ... dev $DEV': filtering by device makes iproute2 omit
    # the 'dev' token from its own output, and feeding that back to
    # 'ip route change' yields a scope-link route with no device, which fails.
    ROUTE_LINE="$(ip route show to match "$BOARD_IP" 2>/dev/null \
                  | grep -v '^default' | grep " dev $DEV" | head -1)"
    if [[ -z "$ROUTE_LINE" ]]; then
        echo "$BOARD_IP is only reachable via the default route on $DEV." >&2
        echo "Refusing to tune the default route -- that would affect every host." >&2
        exit 1
    fi
}

# Reuse the existing route line verbatim, minus any flags we are about to set.
# Rebuilding it by hand is how the other script lost the 'metric 100' and
# silently no-opped; this way every attribute the route already has (metric,
# proto, scope, src, ...) is preserved whatever they happen to be.
strip_flags() {
    sed -E 's/ (initcwnd|initrwnd|quickack) [0-9]+//g' <<<"$1"
}

show() {
    resolve_route
    echo "interface : $DEV"
    echo "route     : $ROUTE_LINE"
    if grep -q 'quickack' <<<"$ROUTE_LINE"; then
        echo "status    : TUNED"
    else
        echo "status    : NOT TUNED  (run './proj net apply' to apply)"
    fi
}

apply() {
    resolve_route
    echo "before: $ROUTE_LINE"
    # shellcheck disable=SC2086  # the route line must word-split into arguments
    sudo ip route change $(strip_flags "$ROUTE_LINE") "${FLAGS[@]}"
    resolve_route
    echo "after : $ROUTE_LINE"
    if grep -q 'quickack' <<<"$ROUTE_LINE"; then
        echo
        echo "Applied. Existing TCP connections do NOT pick this up -- restart the"
        echo "PC side so new ones are established:  ./proj pc restart"
    else
        echo "FAILED: the flags are not on the route." >&2
        exit 1
    fi
}

revert() {
    resolve_route
    # shellcheck disable=SC2086
    sudo ip route change $(strip_flags "$ROUTE_LINE")
    resolve_route
    echo "reverted: $ROUTE_LINE"
}

# Retransmission ratio on the live connection to the board, sampled over a
# window rather than read cumulatively -- the cumulative counter is dominated
# by whatever the link did minutes ago and hides what it is doing now.
loss() {
    snap() { ss -tni state established 2>/dev/null | grep -A1 "$BOARD_IP:" | tr '\n' ' '; }
    local a b
    a="$(snap)"
    if [[ -z "$a" ]]; then
        echo "No established connection to $BOARD_IP -- is the system running?" >&2
        exit 1
    fi
    echo "sampling ${LOSS_SECONDS}s..."
    sleep "$LOSS_SECONDS"
    b="$(snap)"
    field() { grep -o "$2:[0-9]*" <<<"$1" | head -1 | cut -d: -f2; }
    local s0 s1 r0 r1
    s0="$(field "$a" bytes_sent)";    s1="$(field "$b" bytes_sent)"
    r0="$(field "$a" bytes_retrans)"; r1="$(field "$b" bytes_retrans)"
    if [[ -z "${s1:-}" || -z "${r1:-}" ]]; then
        echo "Connection went away mid-sample." >&2
        exit 1
    fi
    awk -v ds=$((s1-s0)) -v dr=$((r1-r0)) -v t="$LOSS_SECONDS" 'BEGIN {
        if (ds <= 0) { print "no data sent during the window"; exit }
        printf "sent      %8.2f MB  (%.2f MB/s)\n", ds/1e6, ds/t/1e6
        printf "retrans   %8.3f MB\n", dr/1e6
        printf "loss      %8.3f %%   (was 0.807%% on 2026-08-26)\n", dr/ds*100
    }'
}

case "${1:-apply}" in
    apply|"") apply ;;
    show)     show ;;
    revert)   revert ;;
    loss)     loss ;;
    *) echo "Usage: $0 {apply|show|revert|loss}" >&2; exit 1 ;;
esac
