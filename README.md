# gdbrpc

Plugin framework and JSON-RPC server running **inside GDB**.

- Plugins (one Python file each) are lazily discovered and can be
  loaded/unloaded/reloaded over RPC.
- Plugins define **commands** (with hand-rolled option schemas) and
  **views** — push-based data sources that are delivered to registered
  clients on GDB stop events, without polling.
- Protocol: JSON-RPC 2.0, newline-delimited, bound to `127.0.0.1` only.

See `DESIGN.md` for the full architecture and the decision log.

## Install

```sh
./install.sh            # or: make install
make status            # sanity check
```

Then start `gdb` — the server starts automatically and listens on
`127.0.0.1:8944` (configurable in `~/.config/gdbrpc/.env`).

## Quick test

```sh
cp plugins/example_plugin.py ~/.config/gdbrpc/plugins/
.venv/bin/python tests/e2e.py
```

## RPC methods (v0)

`plugins.list`, `plugins.load`, `plugins.load_all`, `plugins.unload`,
`plugins.unload_all`, `commands.list`, `commands.help`, `commands.execute`,
`views.list`, `views.register`, `views.unregister`, `server.info`

## Example plugins

**`example`** (view showcase — one pane each, arranged via tmux):

| view | content | options |
|---|---|---|
| `registers` | info registers | `filter` (comma-separated names) |
| `backtrace` | bt | — |
| `breakpoints` | info breakpoints | — |
| `frame` | info frame | — |
| `locals` | info locals | — |
| `threads` | info threads | — |
| `disasm` | x/Ni $pc | `count` |
| `stack` | x/Nxg $sp — **64-bit** entries | `count` |
| `code` | **whole source file** + current line highlighted | — |
| `vmmap` | memory map with permissions (info proc mappings) | — |
| `auxv` | auxiliary vector (canary AT_RANDOM, vDSO) | — |
| `env` | environment variables | `filter` (substring) |
| `sections` | ELF sections with addresses/perms | `filter` (substring) |
| `files` | loaded objects + entry point (info files) | — |
| `floats` | FP/SIMD registers (info float) | — |
| `allregisters` | info all-registers (incl. eflags/segments) | `filter` |

plus the `echo` command (option schema demo).

**`heap`** (parser reused from `~/scratch/heap-plugin`): `heap-tcache`
(size/count/chains, safe-linking deobfuscated), `heap-fastbins`,
`heap-bins` (unsorted/small/large), `heap-arena` (main_arena metadata
+ top chunk size). All render `(x unavailable: ...)` text when the
inferior hasn't set up a heap yet.

**`dbg`** (remote-control commands): `continue`, `step`, `next`, `stepi`,
`finish` (all **non-blocking** — the stop arrives via view pushes), `eval`
(expression), `memory` (x/NF ADDR), `break`, `delete-breakpoint`.

## Configuration

`~/.config/gdbrpc/.env`:

```ini
GDBRPC_PORT=8944
GDBRPC_PLUGIN_DIR=~/.config/gdbrpc/plugins
GDBRPC_SITE_PACKAGES=<venv site-packages, written by gdbrpc install>
GDBRPC_AUTOLOAD=1
```

`GDBRPC_AUTOLOAD=1` (**default**): all discovered plugins load immediately at
gdb startup — no manual `plugins.load` needed. `0` restores lazy loading.

## Clients

One view per console — arrange panes with tmux:

```sh
.venv/bin/gdbrpc attach registers
.venv/bin/gdbrpc attach registers --params '{"filter":"rip,rsp"}'
.venv/bin/gdbrpc exec echo --params '{"message":"hi","count":3}'
```

`attach` prints the snapshot immediately, then one block per gdb stop,
and auto-reconnects when gdb restarts. Raw JSON-RPC: `tools/rpc`.

## GDB-side commands

- `gdbrpc status` — server/plugin overview
- `gdbrpc restart` — restart the RPC server (re-reads `.env`)