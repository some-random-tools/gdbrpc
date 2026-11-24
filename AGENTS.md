# gdbrpc — Agent Reference

Plugin framework + JSON-RPC server running **inside GDB**, with console
clients and a GUI dashboard in mps-tray. Personal single-user project
(user: jb @ testvm, Debian, GDB 16.3, embedded Python 3.13.5).

Everything below reflects the **actually implemented & tested state**
(see DESIGN.md for the full decision log, README.md for user docs).

## Quick Orientation

| Path | What |
|---|---|
| `~/scratch/gdbrpc/` | this repo (server framework, CLI, tests, tools) |
| `~/scratch/mps-ui/user_plugins/gdbrpc_dashboard.py` | the GUI dashboard plugin (mps-tray) |
| `~/scratch/heap-plugin/src/server/heap-deep.py` | external heap parser (imported by our heap plugin) |
| `~/.config/gdbrpc/` | runtime config: `.env`, `bootstrap.py` shim, `plugins/` |
| `~/.gdbinit` (last line) | `source /home/jb/.config/gdbrpc/bootstrap.py` |

Install: `./install.sh` (or `make install`), targets: `install update status
test lint clean`. All idempotent.

## Architecture (locked decisions, verified)

Two core invariants:

1. **Nothing on the main thread touches a socket.** GDB's Python API is only
   safe from GDB's main thread. Server/reader/writer threads never call
   `gdb.*`.
2. **Nothing off the main thread touches gdb.** All handlers run on the
   main thread via `gdb.post_event`; every RPC request crosses the queue
   exactly once. Clients can never interrupt gdb — they wait.

```
client ──requests──► reader thread ──queue──► main thread (post_event)
client ◄──notifs──── writer thread ◄─outbound─ main thread (stop events)
```

- JSON-RPC 2.0, newline-delimited TCP, binds **127.0.0.1 only**, no auth (v0)
- Plugins: one `.py` file, one `Plugin` subclass, metadata via class attrs,
  side effects only in `setup()`/`teardown()`. Discovery imports each file
  (no `setup()` call); **name clashes rejected at discovery**.
- Autoload: **all discovered plugins load at gdb startup**
  (`GDBRPC_AUTOLOAD=1`, default). `plugins.load` on a loaded plugin =
  **reload**; `unload` on not-loaded = error; `unload_all` best-effort
  reverse order.
- **Resuming commands (continue/step/…) are non-blocking**: gdb cannot block
  inside post_event handlers. They return `{"resumed": ...}` and the stop
  arrives via view pushes. Runtime restriction, documented in DESIGN.md §6.
- Views: `triggers = ["stop"]` (declarable list, only stop implemented),
  registered **per connection** with individual params, immediate snapshot
  on `views.register`, auto-unregister on disconnect, per-view failure
  isolation (error notification, never poisons the stop handler).

## Methods (v0)

`plugins.list/load/load_all/unload/unload_all`, `commands.list/help`,
`commands.execute`, `views.list/register/unregister`, `server.info`.
Params validated centrally against hand-rolled `Option` schemas
(`plugin.Option`: name/type/required/default/help/enum_values;
types: str int float bool enum any).

## Config (`~/.config/gdbrpc/.env`)

```ini
GDBRPC_PORT=8944
GDBRPC_PLUGIN_DIR=~/.config/gdbrpc/plugins
GDBRPC_SITE_PACKAGES=<venv site-packages>   # read by GDB (shim uses site.addsitedir!)
GDBRPC_AUTOLOAD=1
GDBRPC_LINE_MARKER_COLOR=#e8c262             # code-view highlight bar
GDBRPC_DIFF_COLOR=#e67e22                    # bold diff color (4+vmmap views)
```

Env vars `GDBRPC_PORT` / `GDBRPC_PLUGIN_DIR` / `GDBRPC_AUTOLOAD` override
`.env` (needed by the e2e test, which uses ports 9xxx).

**Crucial**: the shim and the dashboard both use `site.addsitedir()` —
NOT plain `sys.path.insert()` — because the editable install relies on a
`.pth` file that only `addsitedir` processes. Cost us debugging twice.

## Repo layout

```
src/gdbrpc/
  config.py     .env parsing, Config dataclass, env-var overrides
  plugin.py     Plugin/Command/View/Option base classes + validate_options
  registry.py   plugin records (discovered/loaded/failed) + command registry
  manager.py    PluginManager (scan/load/unload/reload) + ViewHub
  queue.py      Dispatcher (post_event bridge), RequestItem
  server.py     RpcServer + Connection (reader/writer threads per conn)
  methods.py    all RPC methods (reader-side static validation + handlers)
  session.py    venv-side headless client (Session, render_text) — no gdb!
  bootstrap.py  gdb-side App: startup, stop hook, `gdbrpc status/restart`
  cli.py        console script: install/status/new-plugin/attach/exec
plugins/         example_plugin.py (16 views), debugger_plugin.py (dbg cmds),
                 heap_plugin.py (imports ../heap-plugin parser)
tests/           e2e.py (run with GDBRPC_PORT=xxxx GDBRPC_AUTOLOAD=0)
                 + phase 2 checks autoload default
tools/rpc        raw JSON-RPC shell helper (params via heredoc!)
install.sh, Makefile
```

## Plugins in `~/.config/gdbrpc/plugins/` (copies of plugins/)

**example** (16 views): registers(+filter), backtrace, breakpoints, frame,
locals, threads, disasm(+count), stack(+count, **x/Nxg = 64-bit**), code
(**whole source file** + `{"file","text","line"}` payload), vmmap,
auxv, env(+filter), sections(+filter), files, floats, allregisters(+filter).
Command: echo (schema demo). `_clean()` strips ANSI (gdb 16 highlights!).

**dbg** (remote control): continue/step/next/stepi/finish (all
ResumeCommand → non-blocking `{"resumed": ...}`), eval(expression),
memory(address,count,fmt), break(location), delete-breakpoint(number).

**heap** — `heap-tcache/heap-fastbins/heap-bins/heap-arena` views, rendered
as plain text from the **HeapParser imported from
`~/scratch/heap-plugin/src/server/heap-deep.py`** (lazy import on first
snapshot — it registers its own gdb commands and needs libc symbols).
Safe-linking deobfuscation comes from that parser.
NOTE: `HEAP_DEEP_PATH` constant in heap_plugin.py → update if repo moves.
If no heap yet: views degrade to "(tcache unavailable: …)" text.

## Console client (`venv` side, stdlib only)

- `Session` (session.py): connect(), register(view,params) — registrations
  re-sent on every (re)connect — blocking request(), auto-reconnect loop
  (`run()`/`stop()`). **Socket must be blocking after dial**
  (`settimeout(None)`): a recv timeout used to be mistaken for EOF and
  caused a reconnect loop every ~5s. Fixed & covered.
- `gdbrpc attach <view> [--params JSON]`: one view per console (tmux is
  the layout engine); clears the screen before each push (`\033[2J\033[H`),
  status to the **bottom line** via save/jump/clear/restore escapes.
  Console render of code view: `>` marker on current line.
- `gdbrpc exec <command> [--params JSON]`: one-shot commands.execute.
- `tools/rpc <method> [params-json]`, params via heredoc (paste-safe!).

## GUI dashboard (mps-tray plugin)

File: `~/scratch/mps-ui/user_plugins/gdbrpc_dashboard.py`, **copied to**
`~/.config/mps-tray/plugins/` after every change (restart mps-tray to pick
up). PyQt6. Signal bridge `BRIDGE` (update/error/status) — session reader
thread only *emits*, GUI slots do widgets. **Connect signals BEFORE
starting the session thread** or the initial "connected" status (which
triggers the first view refresh) is lost — cost us a real bug hunt.

### Top row (3 framed panels side by side, ONE common height)
- **status panel**: StatusView (dot: green/yellow/red + grey=disconnected)
  + **host/port fields + Connect/Disconnect buttons**. Connect = stop
  session, fresh Session(host,port), re-register all panes. Disconnect =
  stop without retry (grey). Initial auto-connect on startup.
- **layout panel** (rows stacked vertically): Mode radios | N + row:/col: |
  View+params+Add+Refresh+Remove-all | Layout+Save+Load+Tile+Snap
- **debug panel**: Debug: continue/step/next stacked vertically

### Modes (radio; modes live in MODES tuple + _relayout branch)
- **columns** (default): N columns, fill across (row-major). Outer H splitter.
- **rows**: N rows, fill DOWN first (column-major). Outer V splitter.
- **grid**: N×N row-major chunks. Outer V splitter.
- **free**: QMdiArea, **frameless subwindows** (`FramelessWindowHint` —
  only our own header row, no Qt title bar; drag by title moves the
  subwindow). CornerGrip→**EdgeGrip ×8** (all edges+corners, cursors,
  120×80 minimum). N = Tile column count here.
- **slots**: N×N Slot drop-targets (dashed) + shelf for unplaced panes.
  Drag pane **by its title** (DragLabel) onto a slot: free→move,
  occupied→**swap**, shelf→occupied→overwrite (occupant removed!). Empty
  grid builds with zero panes. Slot clicks + pane body clicks set
  row:/col: target fields (via event filters — clicks land on child
  widgets and never reach the pane itself otherwise!).

### Target highlighting & persistence
- `row:/col:` fields = Add-pane target in every grid-ish mode (disabled in
  free). Green highlight: Slot (SLOT_TARGET_STYLE) or pane
  (PANE_TARGET_STYLE) at index (r*n+c row-major, c*n+r in rows mode).
  Radios + fields drive it; auto-advances to next free cell after Add.
- Layout JSONs (`~/.config/gdbrpc/layouts/<name>.json`): mode, cols, snap,
  panes[{view,params}], + mode-specific sizes: `outer_sizes/inner_sizes`
  (splitters), `free_geometry` [x,y,w,h] per pane, `slots`+`shelf`+
  `slots_row_sizes` (slots). Old key names still readable. Load applies
  sizes AFTER panes added — plus a **deferred re-apply pass** (QTimer
  singleShot) because the deleteLater teardown of the previous pane area
  undoes the first application (real bug, fixed).

### Per-update rendering
- **Diff views** (`DIFF_VIEWS = registers, stack, breakpoints, threads,
  vmmap`): difflib.SequenceMatcher **per line, character-level** vs the
  previous update; changed fragments → **bold + DIFF_COLOR**. First
  update plain. Unchanged lines plain. Consecutive stops of an unchanged
  process legitimately produce no bold (verified).
- **code** view: whole file + `line` → full-width highlight bar
  (LINE_MARKER_COLOR) via ExtraSelection + cursor move.
- All pane content passes through `render_text` on the session side.

### Snap (free mode)
Toggle in layout panel (default on, `snap_enabled`, persisted). Move snaps
(±12px) to MDI borders + neighbour pane edges; **resize grips snap too**
(same candidates). Threshold constant in DragLabel._snap_target /
EdgeGrip._candidates.

### Tile button
- free: **our own tiling** — row-major into N columns, deterministic
  (Qt's tileSubWindows() ignores N and produces arbitrary layouts — real
  bug, fixed).
- slots: equal rows+columns; splitter modes: equal shares.

## Testing (how to run & what exists)

```sh
cd ~/scratch/gdbrpc
GDBRPC_PORT=9099 GDBRPC_AUTOLOAD=0 .venv/bin/python tests/e2e.py  # lazy-mode suite + autoload phase
make test          # same, plain
```

- e2e spawns a real gdb on `tests/target` (compiled from tests/target.c —
  heap allocations + `heap_pause()` markers + work()), a fifo-driven
  inferior, and one persistent client connection.
- Covers: discovery/load/reload/unload semantics, all methods, error codes
  (-32601/-32602/-32000), view snapshot-on-register, stop pushes,
  unregister isolation, autoload phase, commands.execute, dbg remote
  control (incl. non-blocking resume + push after), **heap plugin**
  (tcache counts/main_arena/bins after `break heap_pause`),
  code view whole-file+line payload.
- GUI tests (not committed, run ad hoc): offscreen Qt
  (`QT_QPA_PLATFORM=offscreen PYTHONPATH=~/scratch/mps-ui`) + a fifo-driven
  gdb on port 9xxx; drive dashboard via `dash._add_pane()` /
  `mode_buttons[...].setChecked()` and **real QMouseEvent sendEvent** for
  clicks/drags (direct handler calls miss event-filter bugs — learned the
  hard way).
- `gdbrpc status` probes the port: busy+answers-jsonrpc → info line
  "live server (all good)", busy+other → warning, free → ok.

## Gotchas learned the hard way (all fixed, do not regress)

1. **`site.addsitedir()` not `sys.path.insert`** — editable install `.pth`
   (bit twice: GDB shim + dashboard import).
2. **GDB can't block in post_event** — resume commands return immediately;
   stop info arrives via view pushes. Test for `{"resumed": ...}`, not frames.
3. **Reader threads must not close on half-close (nc EOF)** — writer flushes
   pending responses via a queued sentinel first; also `nc -q1` one-shots
   used to lose responses (writer raced close).
4. **`socket.create_connection(timeout=...)` timeout persists** — set
   `settimeout(None)` after dial, or recv timeouts masquerade as EOF →
   reconnect loop every 5s.
5. **argparse subparser dest vs positional name collision** — `command`
   positional silently overwrote the subparser dest → exec no-op'd.
6. **Connect bridge signals BEFORE starting the session thread** — initial
   "connected" status otherwise lost → empty view combo.
7. **QLabel/QFrame default size policies expand** — set Fixed vertical
  policies on labels and give the pane area stretch=1, or the status label
   eats the window.
8. **Deferred deleteLater teardown re-arranges QMdiArea** — geometry must
   be re-applied via QTimer.singleShot after load.
9. **gdb.execute("list"/"info proc …", to_string=True) is ANSI-colored in
   gdb 16** — strip `\x1b[…m` (regex in _clean()).
10. **Drag needs event filters on pane children** (text area/title consume
    clicks); title-release must be swallowed (no pick-after-drop).
11. **`x/Nxw` splits 64-bit addresses** — use `g` (giant) for the stack view.
12. **heap-deep.py imports print banners + register commands** — lazy
    import at first snapshot, only when inferior has libc symbols.
13. **Qt tileSubWindows ignores your N** and respects current sizes —
    use the custom deterministic tiler.
14. Layout save/load applies sizes **after** panes exist; slots mode
    saves/restores `slots_row_sizes`/`slots_area_size`.
15. Terminal paste wraps long lines — keep commands short (tools/rpc
    heredoc pattern) — the user's terminal mangles wrapped pastes.

## Runtime restart protocol (after changes)

- **Server-side change** (anything in `src/gdbrpc/` or `plugins/`, copied to
  `~/.config/gdbrpc/plugins/`): restart **gdb** (autoload at startup picks
  plugins up; a running session keeps old code).
- **Dashboard change**: copy file to `~/.config/mps-tray/plugins/` +
  restart **mps-tray** (gdb untouched).
- Both: copy + both restarts.
- Verify server: `tools/rpc server.info`; health: `make status`.

## Open / deferred

- `GDBRPC_BIND` (non-localhost bind) not configurable — server hardcodes
  127.0.0.1; host/port on the **client** side are (Connect fields).
- More view triggers (new_objfile, breakpoint events) — plumbing exists
  (`triggers` list per view), only "stop" implemented.
- More heap views could reuse additional parser pieces; gadgets search /
  watch view are ideas in README.
- The `.env` diff/line colors are read at plugin **import** time (restart
  mps-tray after editing).