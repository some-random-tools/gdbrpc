"""Registries: plugins (with lifecycle states) and commands/views."""

import threading

# Plugin states (see DESIGN.md section 5.2)
DISCOVERED = "discovered"
LOADED = "loaded"
FAILED = "failed"


class PluginRecord:
    """Registry record for one plugin file."""

    def __init__(self, name, path, state=DISCOVERED,
                 description="", version="", error=None):
        self.name = name
        self.path = path
        self.state = state
        self.description = description
        self.version = version
        self.error = error          # traceback string when FAILED
        self.instance = None        # Plugin instance when LOADED
        self.module_name = None
        self.command_names = []     # declared command names (metadata)
        self.view_names = []        # declared view names (metadata)
        self.load_seq = None        # ordering for unload_all

    def to_dict(self):
        d = {
            "name": self.name,
            "state": self.state,
            "path": self.path,
            "description": self.description,
            "version": self.version,
            "commands": list(self.command_names),
            "views": list(self.view_names),
        }
        if self.error:
            d["error"] = self.error
        return d


class CommandEntry:
    """A registered command or view instance of a loaded plugin."""

    def __init__(self, name, kind, plugin_name, instance):
        self.name = name
        self.kind = kind                # "command" | "view"
        self.plugin_name = plugin_name
        self.instance = instance
        self.description = instance.description
        self.options = list(instance.options)
        self.triggers = list(getattr(instance, "triggers", []))

    def to_dict(self):
        d = {
            "name": self.name,
            "kind": self.kind,
            "plugin": self.plugin_name,
            "description": self.description,
            "options": [o.to_dict() for o in self.options],
        }
        if self.kind == "view":
            d["triggers"] = self.triggers
        return d


class PluginRegistry:
    def __init__(self):
        self._lock = threading.Lock()
        self._records = {}

    def exists(self, name):
        with self._lock:
            return name in self._records

    def get(self, name):
        with self._lock:
            return self._records.get(name)

    def add(self, record):
        with self._lock:
            self._records[record.name] = record

    def remove(self, name):
        with self._lock:
            self._records.pop(name, None)

    def all_records(self):
        with self._lock:
            return list(self._records.values())


class CommandRegistry:
    def __init__(self):
        self._lock = threading.Lock()
        self._entries = {}

    def get(self, name):
        with self._lock:
            return self._entries.get(name)

    def add(self, entry):
        with self._lock:
            self._entries[entry.name] = entry

    def remove(self, name):
        with self._lock:
            self._entries.pop(name, None)

    def all_entries(self):
        with self._lock:
            return list(self._entries.values())