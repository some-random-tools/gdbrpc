"""JSON-RPC method implementations.

All handlers run on GDB's main thread (invoked from Dispatcher).
Static parameter specs are additionally used by the connection reader
threads for early rejection (methods.check_request — pure Python only).
"""

import traceback

from . import __version__
from .errors import GdbrpcError
from .plugin import Option, validate_options

# Static parameter specs per method (reader-side validation).
# View-specific params are validated on the main thread in views.register.
_NAME = [Option("name", type="str", required=True)]
_PARAM_SPECS = {
    "plugins.load": _NAME,
    "plugins.unload": _NAME,
    "commands.help": _NAME,
    "views.register": [
        Option("name", type="str", required=True),
        Option("params", type="any", required=False, default={}),
    ],
    "views.unregister": _NAME,
}


def check_request(method, params):
    """Reader-thread static validation. Raises GdbrpcError, never touches gdb."""
    if method not in METHODS:
        raise GdbrpcError(-32601, f"method not found: {method}")
    spec = _PARAM_SPECS.get(method)
    if spec:
        validate_options(spec, params)


def _get_entry(app, name):
    entry = app.manager.commands.get(name)
    if entry is None:
        raise GdbrpcError(-32601, f"unknown command/view: '{name}'")
    return entry


# -- plugins -------------------------------------------------------------

def _plugins_list(app, conn, params):
    return [rec.to_dict() for rec in app.manager.plugins.all_records()]


def _plugins_load(app, conn, params):
    return app.manager.load(params["name"])


def _plugins_load_all(app, conn, params):
    return app.manager.load_all()


def _plugins_unload(app, conn, params):
    return app.manager.unload(params["name"])


def _plugins_unload_all(app, conn, params):
    return app.manager.unload_all()


# -- commands ------------------------------------------------------------

def _commands_list(app, conn, params):
    return [e.to_dict() for e in app.manager.commands.all_entries()]


def _commands_help(app, conn, params):
    entry = _get_entry(app, params["name"])
    d = entry.to_dict()
    d["help"] = _format_help(entry)
    return d


def _format_help(entry):
    lines = [
        f"{entry.name} — {entry.description}",
        f"kind: {entry.kind}, plugin: {entry.plugin_name}",
    ]
    if entry.kind == "view":
        lines.append(f"triggers: {', '.join(entry.triggers)}")
    if entry.options:
        lines.append("options:")
        for o in entry.options:
            req = "required" if o.required else "optional"
            bits = [f"  {o.name} ({o.type}, {req})"]
            if o.default is not None:
                bits.append(f"default: {o.default!r}")
            if o.enum_values:
                bits.append(f"one of: {', '.join(map(str, o.enum_values))}")
            lines.append(" ".join(bits))
            if o.help:
                lines.append(f"    {o.help}")
    else:
        lines.append("options: none")
    return "\n".join(lines)


# -- views -----------------------------------------------------------------

def _views_list(app, conn, params):
    return [
        e.to_dict()
        for e in app.manager.commands.all_entries()
        if e.kind == "view"
    ]


def _views_register(app, conn, params):
    entry = _get_entry(app, params["name"])
    if entry.kind != "view":
        raise GdbrpcError(
            -32000, f"'{entry.name}' is a {entry.kind}, not a view"
        )
    view_params = params.get("params") or {}
    if not isinstance(view_params, dict):
        raise GdbrpcError(-32602, "view params must be an object")
    validated = validate_options(entry.options, view_params)
    app.viewhub.register(conn, entry, validated)

    # immediate snapshot (decision #17) so a new client is not blind
    try:
        data = entry.instance.provide(dict(validated))
        conn.notify(
            "view.update",
            {"view": entry.name, "trigger": "register", "data": data},
        )
    except Exception as e:
        conn.notify(
            "view.error",
            {
                "view": entry.name,
                "trigger": "register",
                "error": str(e),
                "traceback": traceback.format_exc(),
            },
        )
    return {"registered": entry.name}


def _views_unregister(app, conn, params):
    app.viewhub.unregister(conn, params["name"])
    return {"unregistered": params["name"]}


# -- command execution ----------------------------------------------------

def _commands_execute(app, conn, params):
    """Run a plugin command over RPC (main thread, like every handler;
    the result must be JSON-serializable)."""
    entry = _get_entry(app, params["name"])
    if entry.kind != "command":
        raise GdbrpcError(
            -32000, f"'{entry.name}' is a {entry.kind}, not a command"
        )
    cmd_params = params.get("params") or {}
    if not isinstance(cmd_params, dict):
        raise GdbrpcError(-32602, "command params must be an object")
    validated = validate_options(entry.options, cmd_params)
    return entry.instance.run(validated)


# -- server ----------------------------------------------------------------

def _server_info(app, conn, params):
    return {
        "version": __version__,
        "protocol": "jsonrpc-2.0 (newline-delimited TCP)",
        "methods": sorted(METHODS),
        "triggers": ["stop"],
        "config": {
            "port": app.config.port,
            "plugin_dir": app.config.plugin_dir,
        },
    }


METHODS = {
    "plugins.list": _plugins_list,
    "plugins.load": _plugins_load,
    "plugins.load_all": _plugins_load_all,
    "plugins.unload": _plugins_unload,
    "plugins.unload_all": _plugins_unload_all,
    "commands.list": _commands_list,
    "commands.help": _commands_help,
    "views.list": _views_list,
    "views.register": _views_register,
    "views.unregister": _views_unregister,
    "commands.execute": _commands_execute,
    "server.info": _server_info,
}


def handle(app, item):
    """Main-thread dispatch for one request item."""
    try:
        fn = METHODS.get(item.method)
        if fn is None:
            raise GdbrpcError(-32601, f"method not found: {item.method}")
        spec = _PARAM_SPECS.get(item.method)
        params = (
            validate_options(spec, item.params) if spec else dict(item.params)
        )
        item.result = fn(app, item.conn, params)
    except GdbrpcError as e:
        item.error = e.to_dict()
    except Exception:
        item.error = {
            "code": -32000,
            "message": "internal error",
            "data": traceback.format_exc(),
        }