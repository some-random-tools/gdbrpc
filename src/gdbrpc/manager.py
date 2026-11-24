"""Plugin discovery, load/unload lifecycle, and the view registration hub."""

import importlib.util
import os
import sys
import threading
import traceback
from pathlib import Path

from .errors import GdbrpcError
from .plugin import Plugin
from .registry import (
    DISCOVERED,
    FAILED,
    LOADED,
    CommandEntry,
    CommandRegistry,
    PluginRecord,
    PluginRegistry,
)


def _find_plugin_classes(module):
    """Plugin subclasses *defined in* the given module (not imported)."""
    return [
        obj
        for obj in vars(module).values()
        if isinstance(obj, type)
        and issubclass(obj, Plugin)
        and obj is not Plugin
        and obj.__module__ == module.__name__
    ]


class PluginManager:
    """Discovers plugins in a directory and manages their lifecycle.

    All public methods are invoked from RPC handlers, i.e. on GDB's main
    thread. The registries carry their own locks because connection reader
    threads read them (static validation / view lookups).
    """

    def __init__(self):
        self.plugins = PluginRegistry()
        self.commands = CommandRegistry()
        self._import_counter = 0
        self._load_seq = 0

    # -- discovery -----------------------------------------------------

    def scan(self, plugin_dir):
        pdir = Path(os.path.expanduser(str(plugin_dir)))
        if not pdir.is_dir():
            return
        for path in sorted(pdir.glob("*.py")):
            if path.name.startswith("_"):
                continue
            self._discover(path)

    def _next_module_name(self, stem):
        self._import_counter += 1
        return f"gdbrpc_plugin_{stem}_{self._import_counter}"

    @staticmethod
    def _import_module(path, modname):
        spec = importlib.util.spec_from_file_location(modname, str(path))
        if spec is None or spec.loader is None:
            raise GdbrpcError(-32000, f"cannot create import spec for {path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[modname] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            sys.modules.pop(modname, None)
            raise
        return module

    @staticmethod
    def _declared_names(cls):
        """(kind, name, class) triples declared by a Plugin subclass."""
        out = []
        for kind, attr in (("command", "commands"), ("view", "views")):
            for ccls in getattr(cls, attr, None) or []:
                name = getattr(ccls, "name", "")
                if not name:
                    raise GdbrpcError(
                        -32000, f"{ccls.__name__} has no name attribute"
                    )
                out.append((kind, name, ccls))
        names = [n for _, n, _ in out]
        if len(set(names)) != len(names):
            raise GdbrpcError(
                -32000, f"plugin '{cls.name}': duplicate command/view names"
            )
        return out

    def _discover(self, path):
        stem = path.stem
        modname = self._next_module_name(stem)
        try:
            module = self._import_module(path, modname)
            classes = _find_plugin_classes(module)
            if not classes:
                raise GdbrpcError(
                    -32000, "no gdbrpc.plugin.Plugin subclass defined"
                )
            if len(classes) > 1:
                names = ", ".join(c.__name__ for c in classes)
                raise GdbrpcError(
                    -32000, f"multiple Plugin subclasses defined: {names}"
                )
            cls = classes[0]
            name = cls.name
            if not name:
                raise GdbrpcError(-32000, f"{cls.__name__} has no name")
            declared = self._declared_names(cls)

            # Uniqueness checks — as early as possible (decision #10).
            existing = self.plugins.get(name)
            if existing is not None and existing.path != str(path):
                raise GdbrpcError(
                    -32000,
                    f"plugin name '{name}' already registered by {existing.path}",
                )
            taken = {}
            for other in self.plugins.all_records():
                if other.name == name:
                    continue
                for n in other.command_names + other.view_names:
                    taken.setdefault(n, f"plugin '{other.name}'")
            for entry in self.commands.all_entries():
                taken.setdefault(
                    entry.name,
                    f"loaded {entry.kind} of plugin '{entry.plugin_name}'",
                )
            for kind, cname, _ in declared:
                if cname in taken:
                    raise GdbrpcError(
                        -32000,
                        f"{kind} name '{cname}' already in use "
                        f"by {taken[cname]}",
                    )

            record = PluginRecord(
                name=name,
                path=str(path),
                state=DISCOVERED,
                description=cls.description,
                version=cls.version,
            )
            record.command_names = [n for k, n, _ in declared if k == "command"]
            record.view_names = [n for k, n, _ in declared if k == "view"]
            self.plugins.add(record)
        except Exception as e:
            record = PluginRecord(name=stem, path=str(path), state=FAILED)
            record.error = f"{e}\n{traceback.format_exc()}"
            self.plugins.add(record)

    # -- load / unload ---------------------------------------------------

    def load(self, name):
        record = self.plugins.get(name)
        if record is None:
            raise GdbrpcError(-32000, f"unknown plugin '{name}'")
        if record.state == LOADED:
            # reload semantics (decision #7): teardown, fresh import, setup
            self._teardown(record)
            record.state = DISCOVERED

        modname = self._next_module_name(name)
        instances = []
        try:
            module = self._import_module(Path(record.path), modname)
            classes = _find_plugin_classes(module)
            if len(classes) != 1:
                raise GdbrpcError(
                    -32000, "expected exactly one Plugin subclass"
                )
            cls = classes[0]
            if cls.name != record.name:
                raise GdbrpcError(
                    -32000,
                    f"plugin name changed after discovery: '{cls.name}'",
                )
            declared = self._declared_names(cls)
            plugin = cls()
            for kind, cname, ccls in declared:
                entry = self.commands.get(cname)
                if entry is not None:
                    raise GdbrpcError(
                        -32000,
                        f"{kind} name '{cname}' already in use "
                        f"(loaded by plugin '{entry.plugin_name}')",
                    )
                instances.append((kind, ccls(plugin)))
            plugin.setup()
        except Exception as e:
            record.state = FAILED
            record.instance = None
            record.error = f"{e}\n{traceback.format_exc()}"
            raise GdbrpcError(
                -32000, f"failed to load plugin '{name}': {e}",
                data=record.error,
            )

        for kind, inst in instances:
            self.commands.add(
                CommandEntry(inst.name, kind, name, inst)
            )
        record.state = LOADED
        record.instance = plugin
        record.module_name = modname
        record.error = None
        record.command_names = [i.name for k, i in instances if k == "command"]
        record.view_names = [i.name for k, i in instances if k == "view"]
        self._load_seq += 1
        record.load_seq = self._load_seq
        return {
            "loaded": name,
            "commands": list(record.command_names),
            "views": list(record.view_names),
        }

    def unload(self, name):
        record = self.plugins.get(name)
        if record is None:
            raise GdbrpcError(-32000, f"unknown plugin '{name}'")
        if record.state != LOADED:
            # decision #8: error, not a no-op
            raise GdbrpcError(
                -32000,
                f"plugin '{name}' is not loaded (state: {record.state})",
            )
        warnings = self._teardown(record)
        record.state = DISCOVERED
        record.load_seq = None
        result = {"unloaded": name}
        if warnings:
            result["warnings"] = warnings
        return result

    def load_all(self):
        results = {}
        for record in sorted(self.plugins.all_records(), key=lambda r: r.name):
            if record.state == LOADED:
                results[record.name] = {"ok": True, "skipped": "already loaded"}
                continue
            try:
                self.load(record.name)
                results[record.name] = {"ok": True}
            except GdbrpcError as e:
                results[record.name] = {
                    "ok": False,
                    "error": e.message,
                    "traceback": e.data,
                }
        return {"results": results}

    def unload_all(self):
        # best effort, reverse load order (decision #9)
        loaded = [r for r in self.plugins.all_records() if r.state == LOADED]
        loaded.sort(key=lambda r: r.load_seq or 0, reverse=True)
        results = {}
        for record in loaded:
            try:
                res = self.unload(record.name)
                results[record.name] = (
                    {"ok": True, "warnings": res["warnings"]}
                    if "warnings" in res
                    else {"ok": True}
                )
            except GdbrpcError as e:
                results[record.name] = {"ok": False, "error": e.message}
        return {"results": results}

    def _teardown(self, record):
        """Unregister a loaded plugin; collect teardown errors (best effort)."""
        errors = []
        if record.instance is not None:
            try:
                record.instance.teardown()
            except Exception:
                errors.append(
                    "teardown failed:\n" + traceback.format_exc()
                )
        for name in list(record.command_names) + list(record.view_names):
            self.commands.remove(name)
        record.instance = None
        return errors


class ViewHub:
    """Per-connection view registrations and trigger refreshes.

    register/unregister run on the main thread (RPC handlers);
    remove_connection runs on writer threads when a socket dies;
    refresh runs on the main thread inside the GDB stop-event hook.
    """

    def __init__(self, command_registry):
        self._commands = command_registry
        self._lock = threading.Lock()
        self._registrations = {}   # view name -> [(conn, params)]
        self._entries = {}         # view name -> CommandEntry

    def register(self, conn, entry, params):
        with self._lock:
            self._entries[entry.name] = entry
            regs = self._registrations.setdefault(entry.name, [])
            regs[:] = [(c, p) for c, p in regs if c is not conn]
            regs.append((conn, params))

    def unregister(self, conn, view_name):
        with self._lock:
            regs = self._registrations.get(view_name, [])
            kept = [(c, p) for c, p in regs if c is not conn]
            if len(kept) == len(regs):
                raise GdbrpcError(
                    -32000,
                    f"view '{view_name}' is not registered by this connection",
                )
            if kept:
                self._registrations[view_name] = kept
            else:
                self._registrations.pop(view_name, None)

    def remove_connection(self, conn):
        with self._lock:
            for name in list(self._registrations):
                regs = self._registrations[name]
                regs[:] = [(c, p) for c, p in regs if c is not conn]
                if not regs:
                    self._registrations.pop(name, None)

    def snapshot(self, trigger):
        with self._lock:
            out = []
            for name, regs in self._registrations.items():
                entry = self._entries[name]
                if trigger not in entry.triggers:
                    continue
                out.extend((conn, entry, params) for conn, params in regs)
            return out

    def refresh(self, trigger):
        """Collect view data and push notifications. Main thread only.

        Never raises: per-view failures become view.error notifications
        (decision #20). Views whose plugin has been unloaded are skipped.
        """
        for conn, entry, params in self.snapshot(trigger):
            if self._commands.get(entry.name) is None:
                continue  # plugin was unloaded after registration
            try:
                data = entry.instance.provide(dict(params))
                conn.notify(
                    "view.update",
                    {"view": entry.name, "trigger": trigger, "data": data},
                )
            except Exception as e:
                conn.notify(
                    "view.error",
                    {
                        "view": entry.name,
                        "trigger": trigger,
                        "error": str(e),
                        "traceback": traceback.format_exc(),
                    },
                )