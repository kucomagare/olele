# olele

Cora Z7 FPGA + firmware + PC-app stack for streaming signal loopback over
Ethernet. Tracked source only — every `<tool>/<name>/build/` directory is
regenerated locally and gitignored.

```
bootenv.sh      source (not execute) once per shell: sets $REPO_PATH,
                $VARIANT, $SYSTEM_CMAKE, sources Vivado+Vitis settings64.sh,
                adds the ARM toolchain to PATH
build_all.sh    builds vivado -> vitis -> pc_app for one variant,
                start to finish, no GUI steps
compile_all.sh  fast-path recompile: firmware C code + C++ relay only,
                skips the slow Vivado/platform steps -- use after editing
                source, once build_all.sh has run at least once
run_all.sh      starts the PC app and flashes/runs the firmware on the
                board (opens a PuTTY serial console too)
stop_all.sh     stops the PC app (server + client)
clean_all.sh    wipes build/ in every <tool>/$VARIANT dir
pc_app.sh       passthrough to pc_app/$VARIANT/system.sh -- PC side only, no
                board involvement: ./pc_app.sh restart-client after editing
                config.py, restart-server after tcp_server_app.cpp, plus
                start|stop|restart|status|logs
vivado/<v>/     block design + custom RTL, batch-built bitstream/.xsa
vitis/<v>/      bare-metal lwIP TCP client firmware
pc_app/<v>/     Python client + C++ relay server
shared/         gen_packet_header.py (variant-independent tool) plus
                <v>/packet_format.json: the single source of truth for that
                variant's wire packet/sample structure, used by both its
                firmware and its PC app
```

## Variants

`<v>` above is a **project variant** — a complete, independent
hardware+firmware+PC-app stack living under `<tool>/<variant>/`. Two exist:

| Variant | Architecture | Status |
|---|---|---|
| `sizif` | AXI-Lite, CPU in the per-sample path | **frozen reference** — keep working, A/B against it |
| `marathon` | AXI DMA + AXI-Stream, CPU per buffer | active development (bare-metal, no PetaLinux yet) |

`marathon` began as a copy of `sizif` and diverges from there. Design notes
for it live outside this repo, in `research_info/DMA_talk_260825.txt`.

Every root script acts on **one** variant, chosen by the `VARIANT`
environment variable. Its default is set in exactly one place,
`bootenv.sh` -- no other script or doc hardcodes a variant name as "the
default", so changing it is a one-line edit there:

```bash
./build_all.sh                    # whatever bootenv.sh defaults VARIANT to
VARIANT=<project> ./build_all.sh  # a specific variant, one-shot

export VARIANT=<project>          # ...or switch for the whole shell
./build_all.sh                    # now builds that variant
```

`cdvivado` / `cdvitis` / `cdpcapp` follow `$VARIANT` too, and re-point
immediately when you change it — no need to re-source `bootenv.sh`.

The per-variant scripts derive their own name from their directory
(`VARIANT="$(basename "$SCRIPT_DIR")"`), so running
`vitis/marathon/build_app.sh` directly does the right thing without
`VARIANT` being set at all. **Adding a third variant is a directory copy**
plus a `shared/<name>/packet_format.json`; no script needs editing.

Both variants keep their own `build/` tree, Vitis platform
(`<variant>_platform`), Python venv and PID files, so their builds never
collide. Two things are genuinely shared and cannot run at once: **the board**
(one bitstream at a time) and **TCP port 5001** (only one relay server may
bind it).

## Build everything

```bash
source bootenv.sh
./build_all.sh
```

Runs, in order: Vivado project creation + batch synthesis/implementation/
bitstream/`.xsa` export, Vitis platform+app creation (via the Vitis Python
automation API, no GUI) + firmware build, then the PC app's relay server
binary + venv. See each `<tool>/<variant>/README.md` for what each step does
and how to run its pieces individually.

Changed only the firmware C code (`vitis/<variant>/app/*.c`) or the C++
relay server (`pc_app/<variant>/tcp_server_app.cpp`)? Skip the slow Vivado
synth/impl and Vitis platform-creation steps and just recompile:

```bash
./compile_all.sh
```

Rebuilds the firmware ELF against the already-built Vitis platform export,
then rebuilds `tcp_server_app` (skips the Python venv step if it already
exists). Requires `build_all.sh` to have succeeded at least once already.
Editing any Python module needs neither script — Python isn't compiled,
just restart the client:

```bash
./pc_app.sh restart-client
```

This is the fast loop for tuning `SEND_RATE`/`CHUNK_SIZE` in `config.py`:
both are PC-side only (the firmware reads `length` off the wire), so the
board keeps running untouched and no reflash is needed. `./pc_app.sh` is a
passthrough to `pc_app/$VARIANT/system.sh`, callable from anywhere in the
repo; use `restart-server` instead after editing `tcp_server_app.cpp`.

Not run by `build_all.sh` or `compile_all.sh` — once builds succeed:

```bash
./run_all.sh
```

Starts the PC app's relay server + Python client (`pc_app/<variant>/system.sh
start`), then flashes and runs the firmware on the board over JTAG
(`vitis/<variant>/run.sh`), which itself opens a PuTTY serial console for
`[STATS]` output. Requires `pc_app/<variant>/config.py`'s
`BOARD_CONNECTED` to be `True` (edit it yourself — `run_all.sh` warns but
won't flip it for you) and the board connected/powered.

```bash
./stop_all.sh
```

Stops the PC app (server + client) and the PuTTY serial console
`run.sh` launched. The board's firmware itself has no "stop" — it keeps
running until reset/reflashed (`./run_all.sh` again) or power-cycled.
Each piece can still be run/stopped individually: `vitis/<variant>/run.sh` for
the board, `./pc_app.sh {start|stop|restart|restart-client|restart-server|status|logs}`
for the PC side.

## Wire packet format

`shared/<variant>/packet_format.json` is the single source of truth for the TCP
packet/sample structure shared by the board, the C++ relay, and the
Python client -- field names, bit widths, and signed/unsigned are defined
there once. `python_client.py` reads it directly at runtime;
`vitis/<variant>/build_app.sh` and `pc_app/<variant>/build.sh` generate a matching
C header via `shared/gen_packet_header.py` before compiling, since neither
the firmware nor the relay can read JSON at runtime. See
`pc_app/<variant>/README.md`'s "Wire protocol reminder" for the current field
layout.

## Clean

```bash
./clean_all.sh
```

Wipes `build/` in every `<tool>/$VARIANT` dir (calls each one's own
`clean.sh`), for testing that everything really does regenerate from
tracked source alone. Run individual `<tool>/<variant>/clean.sh` scripts to
clean just one.
