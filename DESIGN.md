# gdbrpc — Design Document

A plugin framework and RPC server running inside GDB.

Plugins are lazily loaded from a plugin directory into a registry, exposed over
a JSON-RPC server on localhost. External clients list/load/unload plugins, list
commands and their options, trigger plugin functionality, and register **views**
that are pushed to them on GDB stop events — no polling.

- **Status:** design / pre-implementation
- **Target environment:** GDB 16.3 (Debian), embedded Python 3.13.5
- **Maintainer:** single-user personal project, kept simple on purpose

---

## 1. Overview

```
                     ┌───────────────────────────────────────────┐
                     │  GDB process (embedded Python 3.13)         │
                     │                                            │
 client ──requests──►│  server thread: accept, parse, validate    │
 client ◄─notifs──── │            │ request queue                 │
                     │            ▼                               │
                     │  main thread (GDB event loop, post_event):│
                     │    drain queue → execute handlers          │
                     │    on stop: view.provide(opts) → outbound  │
                     │            │                                │
                     │  per-connection writer threads:           │
                     │    responses + view notifications          │
                     └───────────────────────────────────────────┘
```

Two core invariants of the whole architecture:

1. **Nothing on the main thread ever touches a socket.** GDB's Python API is
   only safe from GDB's main thread. All GDB work happens in the main-thread
   drainer; results and view notifications are handed to per-connection
   outbound queues written by dedicated writer threads.
2. **Nothing off the main thread ever touches GDB.** The server thread only
   accepts connections, parses JSON, validates against schemas, and enqueues.

Clients can never interrupt GDB. Requests wait in a queue and are processed
in the GDB event loop.

---

## 2. Decisions (locked)

| # | Question | Decision |
|---|----------|----------|
| 1 | GDB API safety | All handlers execute on the main thread via `gdb.post_event`; queue drained by the GDB event loop |
| 2 | Concurrency | Queue-only. No futures, no preemption. Client waits until GDB processes the request. One rule for *all* methods — even pure-metadata methods go through the queue |
| 3 | Plugin layout | One Python file per plugin, metadata via class attributes, side effects only in `setup()` |
| 4 | Option schemas | Hand-rolled. No argparse. Machine-readable listing is a first-class feature |
| 5 | RPC protocol | JSON-RPC 2.0, newline-delimited, TCP |
| 6 | Binding / auth | `127.0.0.1` only, **no authentication** (v0) |
| 7 | Load on loaded plugin | **Reload** (teardown → fresh import → setup) |
| 8 | Unload on not-loaded plugin | **Error** (not a no-op) |
| 9 | `unload_all` | Best-effort, reverse load order, per-plugin results, keep going on failures |
| 10 | Name clashes | Enforced **at discovery time** — a plugin claiming an existing name (or a command/view name clash) is marked `failed` immediately, before load |
| 11 | Discovery | Import the module (no `setup()` call), read class-attribute metadata, keep state `discovered` until explicitly loaded |
| 12 | Config folder | `~/.config/gdbrpc/` — path is a constant in the code |
| 13 | Venv path | **Option B**: `GDBRPC_SITE_PACKAGES` lives in `.env`, the generated shim stays generic |
| 14 | PATH access | Standard pip console scripts in the venv's `bin/` (documented; no custom symlinking) |
| 15 | Protocol versioning | `server.info` returns a version; no per-response version field; no legacy clients |
| 16 | View triggers | Declarable list per view (`triggers = ["stop"]`); only `"stop"` implemented for now, plumb for more later |
| 17 | Snapshot on register | Yes — first notification pushed immediately on `views.register` |
| 18 | View parameters | Views may declare `Option` schemas; validated at registration, passed to `provide(opts)` |
| 19 | Multiple clients per view | Allowed. Registrations are per-connection with their own params; auto-unregistered on disconnect |
| 20 | `provide()` failure | Best-effort: error notification with traceback to that view's clients; other views unaffected; stop handler never poisoned |
| 21 | GDB-side visibility | Framework registers convenience commands (`gdbrpc status`, `gdbrpc restart`); installed via `source` line in `~/.gdbinit` |
| 22 | Plugin failure reports | Load failures → plugin state `failed` + traceback returned over RPC; command failures → JSON-RPC error with traceback in `data` |
| 23 | Plugin autoload | **All discovered plugins load immediately at gdb startup** (`GDBRPC_AUTOLOAD=1`, default on); `GDBRPC_AUTOLOAD=0` restores lazy loading |

---

## 3. Package & installation

### 3.1 Repository layout

```
gdbrpc/                          # repo root
  DESIGN.md
  pyproject.toml                 # pip-installable, console scripts, metadata
  src/gdbrpc/
    __init__.py                  # __version__, config-folder constant
    config.py                    # .env parsing (stdlib-only)
    plugin.py                    # Plugin / Command / View / Option base classes
    registry.py                  # plugin + command/view registries
    manager.py                   # discovery, load/unload/reload, teardown
    queue.py                     # request queue + outbound queues + drainer wiring
    server.py                    # TCP server, per-connection reader, framing
    methods.py                   # JSON-RPC method implementations
    bootstrap.py                 # gdb-side entry point called by the shim
    cli.py                       # console scripts: install, status, ...
  plugins/
    example_plugin.py            # example plugin: one command + one view
```

Everything is **pure Python, stdlib only** — deliberately, so that GDB's
embedded interpreter can import the package directly from a venv's
site-packages without any ABI concerns.

### 3.2 Install flow

`pip install -e .` (or later a wheel / PyPI) puts the package into a custom
venv. Then:

```
$ gdbrpc install          # run once from the venv
```

performs, idempotently:

1. Create `~/.config/gdbrpc/` and `~/.config/gdbrpc/plugins/` if missing.
2. Write `~/.config/gdbrpc/.env` if missing — template with `GDBRPC_PORT`,
   `GDBRPC_PLUGIN_DIR`, `GDBRPC_SITE_PACKAGES`. If a `.env` exists, only
   insert/update `GDBRPC_SITE_PACKAGES`; never clobber user settings.
3. Write `~/.config/gdbrpc/bootstrap.py` (the shim, see 3.3) if missing or
   if its content differs from what this version would generate (overwrite —
   it's ours).
4. Append `source ~/.config/gdbrpc/bootstrap.py` to `~/.gdbinit` **only if**
   not already present.

### 3.3 The shim (`bootstrap.py`)

A small, **generic, machine-generated** file. It contains no environment
specific constants — everything comes from `.env`:

1. Parse `~/.config/gdbrpc/.env`.
2. Append `GDBRPC_SITE_PACKAGES` to `sys.path`.
3. Check the embedded interpreter version ≥ the package minimum
   (fail loudly with a clear message, not an obscure traceback).
4. `import gdbrpc` → `gdbrpc.bootstrap.start(config)`.

The `.gdbinit` line is stable forever; switching venvs (dev/test) is a
one-line `.env` edit.

### 3.4 `.env`

```ini
GDBRPC_PORT=8944
GDBRPC_PLUGIN_DIR=~/.config/gdbrpc/plugins
GDBRPC_SITE_PACKAGES=/home/jb/venvs/gdb/lib/python3.13/site-packages
```

- `GDBRPC_PORT` — TCP port, default `8944`
- `GDBRPC_PLUGIN_DIR` — plugin directory (also resolved by `gdbrpc install`
  which writes a default pointing at `~/.config/gdbrpc/plugins`)
- `GDBRPC_AUTOLOAD` — `1` (**default**): all discovered plugins load
  immediately at gdb startup; `0`: lazy loading (explicit `plugins.load`)

Parsed with a tiny stdlib-only parser (no `python-dotenv` dependency) —
supports `KEY=VALUE`, `#` comments, `~` expansion. Environment variables
(`GDBRPC_PORT`, `GDBRPC_PLUGIN_DIR`, `GDBRPC_AUTOLOAD`) override `.env`.

### 3.5 CLI

Console scripts (in venv `bin/`, hence on `PATH`):

- `gdbrpc install` — see 3.2
- `gdbrpc status` — health check: venv, `.env`, `GDBRPC_SITE_PACKAGES`
  exists & contains the package, shim present, `.gdbinit` line present,
  port not already in use. Clear messages, tells you exactly which file/line
  to fix.
- `gdbrpc new-plugin <name>` — scaffold a plugin file into the plugin dir

---

## 4. Runtime architecture

### 4.1 Startup sequence

```
.gdbinit → source bootstrap.py
  → parse .env
  → sys.path ← site-packages
  → version check
  → import gdbrpc → gdbrpc.bootstrap.start(config)
       → start TCP server thread (127.0.0.1:port)
       → scan plugin dir → import each plugin module (no setup())
         → read class-attribute metadata → registry: discovered / failed
         → name-clash checks (decision #10)
       → install drainer into GDB event loop (post_event, self-reposting)
       → register gdb stop-event hook → triggers view refresh
       → register GDB convenience commands
```

### 4.2 Threads

| Thread | Role |
|---|---|
| main (GDB) | Drains the request queue; executes every handler; runs `view.provide()` on stop events; never touches sockets |
| server (accept) | `accept()` loop, spawns per-connection reader threads |
| connection reader (per conn) | Read lines, parse JSON-RPC, validate params against schema, enqueue request, wait for result event |
| connection writer (per conn) | Drains the connection's outbound queue (responses + notifications), writes to socket; on write error marks connection dead and unregisters its views |

### 4.3 Request flow

```
reader thread:  line → json → method lookup → schema validation
                  → RequestItem(conn, id, method, params) → request queue
                  → wait on item.done (threading.Event)

main thread:    drainer picks RequestItem
                  → dispatch to method implementation
                  → result / exception → item.result
                  → set item.done
                  → put response on conn.outbound

writer thread:  outbound → serialize → socket
```

Validation failures and unknown methods are answered directly by the reader
thread (no GDB involvement, no queue) — cheap and correct.

---

## 5. Plugin model

### 5.1 Plugin file

One Python file per plugin. A plugin subclasses `gdbrpc.plugin.Plugin`; the
plugin's commands and views are declared as inner subclasses with all metadata
in **class attributes** (strings, lists — safe at class-definition time):

```python
from gdbrpc.plugin import Plugin, Command, View, Option


class RegistersView(View):
    name = "registers"
    description = "Snapshot of current registers"
    triggers = ["stop"]
    options = [Option("regs", type="str", required=False, default=None,
                      help="comma-separated filter of register names")]

    def provide(self, opts): ...          # runs on main thread, returns JSON-able


class ExamplePlugin(Plugin):
    name = "example"                      # unique key in the registry
    description = "Example plugin"
    version = "1.0"
    commands = [RegistersView]            # commands and views, side by side
    views = [RegistersView]

    def setup(self): ...                   # may touch gdb.*; runs on load
    def teardown(self): ...                # may touch gdb.*; runs on unload
```

*(API detail to be finalized in implementation: whether commands/views are one
decorated list, inner classes discovered by convention, or two attributes as
sketched. The invariant that matters: metadata readable without executing
`setup()`, and without any `gdb.*` calls.)*

### 5.2 Plugin states

```
discovered ──load──► loaded ──unload──► discovered
     │                    │
     └── discovery error ─┴────► failed (with traceback)
```

- **discovered** — module imported, metadata read, clash-checked. No
  side effects yet.
- **loaded** — `setup()` ran successfully, commands registered.
- **failed** — import/clash/setup error; carries a traceback string.

`load` on a `loaded` plugin = **reload**: `teardown()` → fresh import →
`setup()`.

### 5.3 Registry & name uniqueness

- Plugin names are unique registry keys.
- Command and view names are unique **globally across all loaded plugins**
  (a name clash marks the newly discovered plugin `failed` at discovery time,
  decision #10).
- Clashes are detected during the discovery scan, before any `setup()` runs.

### 5.4 Unload semantics

- Unload removes the plugin's commands/views from the registries and calls
  `teardown()`.
- The Python module stays in `sys.modules` — best effort only; plugins are
  documented to keep module-level side effects out.
- `unload` on a `discovered`/`failed` plugin → JSON-RPC error.
- `unload_all` → best effort in reverse load order; response contains
  per-plugin results.

---

## 6. Commands & options

- Commands are registry entries like views; whether a command additionally
  registers an **interactive GDB command** (a `gdb.Command` subclass) is just
  a flag on the declaration. The registry itself is uniform.
- Hand-rolled option schema, centrally validated before any handler runs:

```python
Option(
    name="count",
    type="int",                # str | int | float | bool | enum
    required=False,
    default=1,
    help="how many",
    enum_values=[],            # only meaningful when type == "enum"
)
```

- RPC-triggered command execution runs the handler on the main thread,
  same queue path as everything else.
- **Resuming commands (continue/step/next/stepi/finish) are non-blocking:**
  GDB cannot block inside post_event handlers, so they resume the inferior
  and answer `{"resumed": "continue"}` immediately. The resulting stop is
  reported through the stop-triggered view pushes (which is the dashboard
  model anyway). This is a runtime restriction, not a protocol choice.

---

## 7. Views

Push-based data sources. A client **registers** a view once and receives
notifications whenever the view's triggers fire — no polling.

### 7.1 Model

- `triggers = ["stop"]` — declarable list; only `"stop"` is implemented now
  (decision #16). The trigger plumbing is generic: adding e.g. `"new_objfile"`
  later is a framework-side change, no plugin redesign.

### 7.2 Registration

```
views.register(name, params?)
  → validate params against the view's Option schema
  → registration is per-connection (own params)
  → immediate snapshot: provide(params) runs once, result pushed as
    a notification right away (decision #17)
```

On disconnect of the connection, all of its registrations are dropped
(decision #19) — the writer thread signals the death; view registrations are
pruned.

### 7.3 Refresh on stop

```
gdb stop event (main thread)
  → drainer iterates all registrations for views with trigger "stop"
  → view.provide(params) — main thread, GDB-safe
  → success: notification {view: name, data: ...} → conn.outbound (non-blocking)
  → failure: error notification with traceback → conn.outbound
  → one broken view never affects other views (decision #20)
```

### 7.4 Notification envelope

JSON-RPC 2.0 notifications:

```json
{"jsonrpc": "2.0", "method": "view.update", "params": {"view": "registers", "data": {...}}}
```

Errors as `{"method": "view.error", "params": {"view": ..., "error": ..., "traceback": ...}}`.

---

## 8. RPC protocol

- JSON-RPC 2.0 over TCP, **one JSON document per line** (newline-delimited).
- Requests carry `id`; responses echo it. Notifications have no `id`.
- Server binds `127.0.0.1` only; no authentication (decision #6).
- Errors: JSON-RPC error objects; `data` carries traceback strings for
  plugin/command/view failures (decisions #20, #22).
- No per-response version field (decision #15) — `server.info` is the
  single source of versioning truth.

### 8.1 Methods (v0)

| Method | Semantics |
|---|---|
| `plugins.list` | All plugins, all states, metadata + provided commands/views |
| `plugins.load` | `params: {name}`; reload semantics if already loaded |
| `plugins.load_all` | Load every discovered plugin, per-plugin results |
| `plugins.unload` | `params: {name}`; error if not loaded |
| `plugins.unload_all` | Best effort, reverse load order, per-plugin results |
| `commands.list` | All registered commands/views incl. option schemas |
| `commands.help` | `params: {name}`; one command, full formatted help |
| `commands.execute` | `params: {name, params?}`; runs a command on the main thread, returns its JSON result |
| `views.list` | Available views, their triggers and schemas |
| `views.register` | `params: {name, params?}`; snapshot pushed immediately |
| `views.unregister` | `params: {name}`; error if not registered by this connection |
| `server.info` | Version, protocol, method list, supported triggers |

### 8.2 GDB-side convenience commands (v0)

- `gdbrpc status` — print server state, port, plugin count/states
- `gdbrpc restart` — restart the RPC server thread (config re-read)

### 8.3 Explicit non-goals (v0)

- Auth / TLS / non-localhost binding
- Interrupting GDB from clients (cancellation, timeouts)
- Trigger types beyond `"stop"`
- True module unloading (`del sys.modules` + fresh import is enough — reload
  semantics already cover the dev workflow)

---

## 9. Concurrency & safety model (summary)

| Rule | Rationale |
|---|---|
| Every RPC method handler runs on the main thread | GDB API safety; one uniform rule, even for pure-metadata methods |
| Server/reader threads never call `gdb.*` | They only parse/validate/enqueue |
| Main thread never blocks on sockets | Responses & notifications go through outbound queues with writer threads |
| Writer threads never call `gdb.*` | They only serialize & write |
| View refresh runs inside the stop-event handler on the main thread | That is the moment all inferior state changed and is safe to read |

Failure isolation is best-effort *everywhere*: `unload_all`, view refresh,
load failures — one failing element produces an error report and the rest
continues.

---

## 10. Build order

1. `src/gdbrpc/plugin.py` — `Option`, `Command`, `View`, `Plugin` base classes
2. `registry.py` — plugin + command/view registries
3. `config.py` — `.env` parsing, constants (config folder)
4. `queue.py` — request queue + outbound queues + drainer wiring
5. `manager.py` — discovery, load/unload/reload
6. `server.py` + `methods.py` — JSON-RPC plumbing + method implementations
7. View dispatch on stop events
8. `bootstrap.py` — gdb-side entry point
9. `cli.py` + `pyproject.toml` — install/status/new-plugin
10. `plugins/example_plugin.py` — one command + one view (registers snapshot)
11. End-to-end test: real `gdb`, sample inferior program, `nc`/client script

---

## 11. Client

A venv-side client (never runs inside GDB; free to use any deps, currently
stdlib-only):

- `gdbrpc.session.Session` — headless: connect, `register(view, params)`
  (re-sent on every reconnect), blocking `request()`, notification callbacks,
  auto-reconnect loop (`run()`/`stop()`)
- `gdbrpc attach <view> [--params JSON]` — **TUI = one view per console**;
  panes are arranged by tmux, not the app. Prints each pushed update as
  plain text (views deliver console-like text), status to stderr, until
  Ctrl-C. Auto-reconnects across gdb restarts.
- `gdbrpc exec <command> [--params JSON]` — one-shot `commands.execute`
- `tools/rpc` — raw one-shot JSON-RPC shell helper

### GUI (future milestone)

Native window app with **layout files** (`~/.config/gdbrpc/layouts/*.json`):
panes with view + params + placement (orientation/size), loadable into
windows. Reuses `Session`. Layouts are a GUI concept — the TUI deliberately
has none (tmux is the layout engine there).

## 12. Open items (fine to defer)

- Additional view triggers (`"new_objfile"`, `"breakpoint"`, ...)
- GUI client + layout files (see section 11)
- Richer view data rendering (structured data, per-view renderers)