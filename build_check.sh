#!/usr/bin/env bash
# build_check.sh -- read a finished Vivado build and say whether it is actually OK.
#
#   ./build_check.sh              check the current build
#   ./build_check.sh --save       check, then record these numbers as the baseline
#   ./build_check.sh --baseline   print the stored baseline and exit
#   ./build_check.sh -v <name>    check a variant other than $VARIANT
#
# WHY THIS EXISTS
# ---------------
# "The build finished" and "the build is OK" are different claims, and the
# obvious check for the second one is wrong in two specific ways this script
# exists to get right:
#
#   1. `grep -c ERROR build.log` is USELESS here. Vivado echoes the sourced Tcl
#      back into the log, and cora_z7.tcl/bd_CoraZ7_Eth.tcl contain commented
#      -severity "ERROR" template lines. A perfectly clean build reports 16
#      hits. Real Vivado errors start at column 0, hence "^ERROR" everywhere
#      below -- never drop the anchor.
#
#   2. The PS7 DDR criticals (PSU-1/PSU-2, negative DQS skew) fire on every
#      single build of this design and always have. Leaving them in the output
#      trains you to ignore critical warnings, which is exactly backwards. They
#      are filtered here so that anything this script does print is new.
#
# Timing is compared against a saved baseline rather than judged in isolation,
# because the number that matters is the DELTA. WNS moving 0.8ns on a 20ns
# period is meaningless on its own but can mean a lot if you just rewrote
# something. The endpoint COUNT is the real structural check: if it changed and
# you did not intend to add or remove registers, look at that before anything
# else -- it catches a whole class of translation and refactoring mistakes that
# still meet timing and still program the board.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_PATH="$SCRIPT_DIR"

MODE="check"
VARIANT_ARG=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --save)          MODE="save"; shift ;;
        --baseline)      MODE="show-baseline"; shift ;;
        -v|--variant)    VARIANT_ARG="${2:-}"; shift 2 ;;
        -h|--help)       sed -n '2,35p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "build_check: unknown option '$1'" >&2; exit 2 ;;
    esac
done

[[ -n "$VARIANT_ARG" ]] && export VARIANT="$VARIANT_ARG"
# shellcheck source=bootenv.sh
source "$REPO_PATH/bootenv.sh" >/dev/null 2>&1 || true
VARIANT="${VARIANT:-marathon}"

VIV="$REPO_PATH/vivado/$VARIANT"
BUILD="$VIV/build/tcp_client"
RUNS="$BUILD/tcp_client.runs"
BASELINE="$VIV/.build_check_baseline"

if [[ ! -d "$VIV" ]]; then
    echo "No such variant: '$VARIANT' (there is no vivado/$VARIANT)" >&2
    exit 2
fi

if [[ "$MODE" == "show-baseline" ]]; then
    if [[ -f "$BASELINE" ]]; then cat "$BASELINE"; else echo "no baseline saved for $VARIANT"; fi
    exit 0
fi

RED=$'\033[31m'; YEL=$'\033[33m'; GRN=$'\033[32m'; DIM=$'\033[2m'; OFF=$'\033[0m'
[[ -t 1 ]] || { RED=""; YEL=""; GRN=""; DIM=""; OFF=""; }

FAIL=0
WARN=0
ok()   { printf '  %sok%s    %s\n'   "$GRN" "$OFF" "$1"; }
warn() { printf '  %swarn%s  %s\n'   "$YEL" "$OFF" "$1"; WARN=$((WARN+1)); }
bad()  { printf '  %sFAIL%s  %s\n'   "$RED" "$OFF" "$1"; FAIL=$((FAIL+1)); }
note() { printf '        %s%s%s\n'   "$DIM" "$1" "$OFF"; }

echo "=== build_check: $VARIANT ==="

# ---------------------------------------------------------------- logs -----
echo
echo "logs"
found_log=0
for L in "$REPO_PATH/build_all.log" "$VIV/build.log" "$VIV/build_bitstream.log"; do
    [[ -f "$L" ]] || continue
    found_log=1
    rel="${L#"$REPO_PATH"/}"
    age=$(( ( $(date +%s) - $(stat -c %Y "$L") ) / 60 ))
    n=$(grep -c "^ERROR" "$L")
    if (( n )); then
        bad "$rel: $n error(s)   ${age}m old"
        grep -n "^ERROR" "$L" | head -5 | sed 's/^/        /'
    else
        ok "$rel: no errors   ${age}m old"
    fi
    # PSU-1/PSU-2 are the permanent PS7 DDR skew criticals; anything else is news.
    while IFS= read -r line; do
        warn "$rel: $line"
    done < <(grep "^CRITICAL WARNING" "$L" | grep -v "PSU-[12]" | sort -u | head -10)
done
(( found_log )) || warn "no build logs found -- has this variant been built?"

# ------------------------------------------------------------ artifacts ----
echo
echo "artifacts"
for A in "$BUILD/CoraZ7_Eth_wrapper.xsa" \
         "$RUNS/impl_1/CoraZ7_Eth_wrapper.bit"; do
    if [[ -f "$A" ]]; then
        ok "$(basename "$A")  $(stat -c %y "$A" | cut -d. -f1)"
    else
        bad "missing: ${A#"$BUILD"/}"
    fi
done

# --------------------------------------------------------------- runs ------
echo
echo "runs"
for R in synth_1 impl_1; do
    if [[ -f "$RUNS/$R/runme.log" ]]; then
        if grep -q "^ERROR" "$RUNS/$R/runme.log"; then
            bad "$R: errors in runme.log"
        else
            ok "$R: clean"
        fi
    else
        warn "$R: no runme.log (run not launched?)"
    fi
done

# ------------------------------------------------------------- timing ------
echo
echo "timing"
RPT=$(ls -t "$RUNS"/impl_1/*timing_summary_routed.rpt 2>/dev/null | head -1)
WNS=""; WHS=""; TEP=""
if [[ -z "$RPT" ]]; then
    bad "no routed timing summary -- implementation did not complete"
else
    read -r WNS TNS TNSF TEP WHS THS THSF _ < <(
        awk '/Design Timing Summary/{f=1} f&&/^ *-?[0-9]+\.[0-9]+ /{print; exit}' "$RPT")
    fmt="WNS %s ns   WHS %s ns   endpoints %s"
    # shellcheck disable=SC2059
    line=$(printf "$fmt" "$WNS" "$WHS" "$TEP")
    if (( TNSF > 0 || THSF > 0 )); then
        bad "$line   -- $TNSF setup / $THSF hold FAILING"
    else
        ok "$line"
    fi
    if grep -q "All user specified timing constraints are met" "$RPT"; then
        note "all user specified timing constraints are met"
    else
        bad "report does not say constraints are met"
    fi
    # Worst path: where it is matters more than what it is.
    awk '/Max Delay Paths/{f=1}
         f&&/^ *Source:/{src=$2}
         f&&/^ *Destination:/{dst=$2}
         f&&/^ *Logic Levels:/{ll=$3; printf "        worst path, %s logic levels\n          from %s\n          to   %s\n", ll, src, dst; exit}' "$RPT" \
      | sed "s/^/$DIM/;s/$/$OFF/"
fi

# ----------------------------------------------------------- baseline ------
echo
echo "baseline"
if [[ -f "$BASELINE" ]]; then
    # shellcheck disable=SC1090
    B_WNS=$(grep -oP '^WNS=\K.*' "$BASELINE"); B_WHS=$(grep -oP '^WHS=\K.*' "$BASELINE")
    B_TEP=$(grep -oP '^ENDPOINTS=\K.*' "$BASELINE"); B_WHEN=$(grep -oP '^WHEN=\K.*' "$BASELINE")
    note "saved $B_WHEN"
    if [[ -n "$TEP" && "$TEP" != "$B_TEP" ]]; then
        warn "endpoint count changed: $B_TEP -> $TEP"
        note "same logic should give the same count -- if you did not mean to add or"
        note "remove registers, investigate this before trusting the timing numbers"
    elif [[ -n "$TEP" ]]; then
        ok "endpoint count unchanged ($TEP)"
    fi
    for pair in "WNS $B_WNS $WNS" "WHS $B_WHS $WHS"; do
        set -- $pair
        [[ -z "${3:-}" || -z "${2:-}" ]] && continue
        d=$(awk -v a="$2" -v b="$3" 'BEGIN{printf "%+.3f", b-a}')
        if awk -v b="$3" 'BEGIN{exit !(b<0)}'; then
            bad "$1 $2 -> $3 ($d ns) NEGATIVE"
        else
            note "$1 $2 -> $3 ($d ns)"
        fi
    done
else
    note "no baseline yet -- run with --save once you have a build you trust"
fi

if [[ "$MODE" == "save" ]]; then
    if (( FAIL )); then
        echo
        echo "refusing to save a baseline from a build with failures" >&2
        exit 1
    fi
    { echo "WHEN=$(date '+%Y-%m-%d %H:%M')"
      echo "WNS=$WNS"; echo "WHS=$WHS"; echo "ENDPOINTS=$TEP"
    } > "$BASELINE"
    echo
    echo "baseline saved: $BASELINE"
fi

echo
if (( FAIL )); then
    echo "${RED}FAILED${OFF}: $FAIL problem(s), $WARN warning(s)"
    exit 1
elif (( WARN )); then
    echo "${YEL}OK with $WARN warning(s)${OFF}"
    exit 0
else
    echo "${GRN}OK${OFF}"
    exit 0
fi
