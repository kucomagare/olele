# olele

Cora Z7 FPGA + firmware + PC-app stack for streaming signal loopback over
Ethernet. Tracked source only — every `<tool>/<name>/build/` directory is
regenerated locally and gitignored.

```
bootenv.sh      source (not execute) once per shell: sets $REPO_PATH,
                $SYSTEM_CMAKE, sources Vivado+Vitis settings64.sh, adds
                the ARM toolchain to PATH
build_all.sh    builds vivado -> vitis -> pc_app for the "sizif" version,
                start to finish, no GUI steps
compile_all.sh  fast-path recompile: firmware C code + C++ relay only,
                skips the slow Vivado/platform steps -- use after editing
                source, once build_all.sh has run at least once
run_all.sh      starts the PC app and flashes/runs the firmware on the
                board (opens a PuTTY serial console too)
stop_all.sh     stops the PC app (server + client)
clean_all.sh    wipes build/ in every <tool>/sizif dir
vivado/sizif/   block design + custom RTL, batch-built bitstream/.xsa
vitis/sizif/    bare-metal lwIP TCP client firmware
pc_app/sizif/   Python client + C++ relay server
```

`sizif` is the current hardware/firmware version name — future variants
will live as siblings, e.g. `vivado/<other-name>/`.

## Build everything

```bash
source bootenv.sh
./build_all.sh
```

Runs, in order: Vivado project creation + batch synthesis/implementation/
bitstream/`.xsa` export, Vitis platform+app creation (via the Vitis Python
automation API, no GUI) + firmware build, then the PC app's relay server
binary + venv. See each `<tool>/sizif/README.md` for what each step does
and how to run its pieces individually.

Changed only the firmware C code (`vitis/sizif/app/src/*.c`) or the C++
relay server (`pc_app/sizif/tcp_server_app.cpp`)? Skip the slow Vivado
synth/impl and Vitis platform-creation steps and just recompile:

```bash
./compile_all.sh
```

Rebuilds the firmware ELF against the already-built Vitis platform export,
then rebuilds `tcp_server_app` (skips the Python venv step if it already
exists). Requires `build_all.sh` to have succeeded at least once already.
Editing `pc_app/sizif/python_client.py` needs neither script — Python
isn't compiled, just rerun it.

Not run by `build_all.sh` or `compile_all.sh` — once builds succeed:

```bash
./run_all.sh
```

Starts the PC app's relay server + Python client (`pc_app/sizif/system.sh
start`), then flashes and runs the firmware on the board over JTAG
(`vitis/sizif/run.sh`), which itself opens a PuTTY serial console for
`[STATS]` output. Requires `pc_app/sizif/python_client.py`'s
`BOARD_CONNECTED` to be `True` (edit it yourself — `run_all.sh` warns but
won't flip it for you) and the board connected/powered.

```bash
./stop_all.sh
```

Stops the PC app (server + client) and the PuTTY serial console
`run.sh` launched. The board's firmware itself has no "stop" — it keeps
running until reset/reflashed (`./run_all.sh` again) or power-cycled.
Each piece can still be run/stopped individually: `vitis/sizif/run.sh`,
`pc_app/sizif/system.sh {start|stop|status|logs}`.

## Clean

```bash
./clean_all.sh
```

Wipes `build/` in every `<tool>/sizif` dir (calls each one's own
`clean.sh`), for testing that everything really does regenerate from
tracked source alone. Run individual `<tool>/sizif/clean.sh` scripts to
clean just one.
