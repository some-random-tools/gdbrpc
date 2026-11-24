"""Plugin-facing API: Option schema, Command, View and Plugin base classes.

Everything here must be importable *inside GDB* (plugins do
``from gdbrpc.plugin import Plugin, Command, View, Option``), but this module
itself never touches gdb.
"""

from dataclasses import dataclass, field
from typing import Any

from .errors import GdbrpcError

VALID_TYPES = ("str", "int", "float", "bool", "enum", "any")


@dataclass
class Option:
    """A single command/view option with a hand-rolled schema (decision #4)."""

    name: str
    type: str = "str"               # one of VALID_TYPES
    required: bool = False
    default: Any = None
    help: str = ""
    enum_values: list = field(default_factory=lambda: [])  # only for type="enum"

    def __post_init__(self):
        if self.type not in VALID_TYPES:
            raise ValueError(
                f"option '{self.name}': invalid type '{self.type}' "
                f"(must be one of {VALID_TYPES})"
            )

    def to_dict(self):
        d = {
            "name": self.name,
            "type": self.type,
            "required": self.required,
            "default": self.default,
            "help": self.help,
        }
        if self.type == "enum":
            d["enum_values"] = list(self.enum_values)
        return d


def _invalid(option, raw):
    return GdbrpcError(
        -32602, f"invalid value for option '{option.name}': {raw!r}"
    )


def _coerce(option, raw):
    t = option.type
    if t == "any":
        return raw
    if t == "str":
        return raw if isinstance(raw, str) else str(raw)
    if t == "int":
        if isinstance(raw, bool):
            raise _invalid(option, raw)
        try:
            return int(raw)
        except (TypeError, ValueError):
            raise _invalid(option, raw)
    if t == "float":
        if isinstance(raw, bool):
            raise _invalid(option, raw)
        try:
            return float(raw)
        except (TypeError, ValueError):
            raise _invalid(option, raw)
    if t == "bool":
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, str) and raw.lower() in (
            "true", "false", "1", "0", "yes", "no",
        ):
            return raw.lower() in ("true", "1", "yes")
        raise _invalid(option, raw)
    if t == "enum":
        if raw in option.enum_values:
            return raw
        raise GdbrpcError(
            -32602,
            f"invalid value for option '{option.name}': {raw!r} "
            f"(must be one of {option.enum_values})",
        )
    raise _invalid(option, raw)


def validate_options(options, params):
    """Validate a params dict against a list of Options.

    Returns a new dict with defaults filled in. Raises GdbrpcError(-32602)
    on unknown keys, missing required options or bad values.
    """
    params = dict(params or {})
    known = {o.name for o in options}
    unknown = set(params) - known
    if unknown:
        raise GdbrpcError(-32602, f"unknown option(s): {sorted(unknown)}")
    out = {}
    for o in options:
        if o.name in params:
            out[o.name] = _coerce(o, params[o.name])
        elif o.required:
            raise GdbrpcError(-32602, f"missing required option '{o.name}'")
        else:
            out[o.name] = o.default
    return out


class Command:
    """A plugin command. All metadata lives in class attributes.

    ``run(opts)`` is always executed on GDB's main thread.
    Set ``gdb_command = True`` to additionally register an interactive
    GDB command (v0: RPC-only by default).
    """

    name = ""
    description = ""
    options = []
    gdb_command = False

    def __init__(self, plugin=None):
        self.plugin = plugin

    def run(self, opts):
        raise NotImplementedError


class View:
    """A plugin view: a push-based data source.

    ``triggers`` is a declarable list (decision #16); only "stop" is
    implemented for now. ``provide(opts)`` runs on GDB's main thread
    inside the stop-event handler and must return JSON-serializable data.
    """

    name = ""
    description = ""
    triggers = ["stop"]
    options = []

    def __init__(self, plugin=None):
        self.plugin = plugin

    def provide(self, opts):
        raise NotImplementedError


class Plugin:
    """Base class for plugins (one file = exactly one Plugin subclass).

    Class attributes are metadata only — readable at discovery time
    without calling setup(). All side effects belong in setup()/teardown().
    """

    name = ""
    description = ""
    version = ""
    commands = []   # list of Command subclasses
    views = []      # list of View subclasses

    def setup(self):
        """Called on load. May touch gdb.* (runs on the main thread)."""

    def teardown(self):
        """Called on unload/reload. May touch gdb.* (main thread)."""