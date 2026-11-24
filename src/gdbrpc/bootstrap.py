"""GDB-side entry point.

Called by the generated shim (~/.config/gdbrpc/bootstrap.py) as
``gdbrpc.bootstrap.start()``. Only importable inside GDB.
"""

import gdb

from . import __version__, methods
from .config import load_config
from .errors import GdbrpcError
from .manager import PluginManager, ViewHub
from .queue import Dispatcher
from .server import RpcServer


class App:
    def __init__(self, config):
        self.config = config
        self.version = __version__
        self.manager = PluginManager()
        self.viewhub = ViewHub(self.manager.commands)
        self.dispatcher = Dispatcher(lambda item: methods.handle(self, item))
        self.server = RpcServer(self, host=getattr(config, "bind", "127.0.0.1"))
        self.methods = methods
        self.start_error = None

    def start(self):
        # discovery runs on the main thread (we are called from .gdbinit)
        self.manager.scan(self.config.plugin_dir)
        if getattr(self.config, "autoload", True):
            # load all discovered plugins immediately (GDBRPC_AUTOLOAD=0
            # in .env disables this)
            result = self.manager.load_all()
            ok = sum(
                1 for r in result["results"].values() if r.get("ok")
            )
            failed = [n for n, r in result["results"].items()
                      if not r.get("ok")]
            total = len(result["results"])
            if failed:
                gdb.write(
                    f"[gdbrpc] autoload: {ok}/{total} loaded, "
                    f"FAILED: {', '.join(failed)}\n"
                )
            else:
                gdb.write(f"[gdbrpc] autoload: {ok}/{total} plugin(s) loaded\n")
        try:
            self.server.start(self.config.port)
        except GdbrpcError as e:
            self.start_error = e.message
            gdb.write(f"[gdbrpc] {e.message}\n")
        # view refreshes on stop events (main thread)
        gdb.events.stop.connect(self._on_stop)
        self._register_gdb_command()
        if self.start_error is None:
            gdb.write(
                f"[gdbrpc] v{self.version} listening on "
                f"{self.server.host}:{self.server.port}, "
                f"{len(self.manager.plugins.all_records())} plugin(s) "
                "discovered\n"
            )

    def _on_stop(self, event):
        # best-effort; refresh() isolates per-view failures (decision #20)
        self.viewhub.refresh("stop")

    def on_connection_closed(self, conn):
        # called from reader/writer threads on socket death
        self.viewhub.remove_connection(conn)
        self.server.forget(conn)

    def restart(self):
        # re-read .env (decision: restart = config re-read + new server)
        self.server.stop()
        self.config = load_config()
        self.start_error = None
        try:
            self.server.start(self.config.port)
        except GdbrpcError as e:
            self.start_error = e.message
        if self.start_error:
            gdb.write(f"[gdbrpc] restart failed: {self.start_error}\n")
        else:
            gdb.write(
                f"[gdbrpc] restarted, listening on "
                f"{self.server.host}:{self.server.port}\n"
            )

    def status_text(self):
        records = sorted(self.manager.plugins.all_records(), key=lambda r: r.name)
        lines = [f"gdbrpc {self.version}"]
        if self.server.is_running():
            lines.append(
                f"server: listening on {self.server.host}:{self.server.port}"
            )
        else:
            extra = f" ({self.start_error})" if self.start_error else ""
            lines.append(f"server: not running{extra}")
        lines.append(f"plugin dir: {self.config.plugin_dir}")
        lines.append(f"plugins: {len(records)}")
        for rec in records:
            lines.append(f"  [{rec.state:>9}] {rec.name}  ({rec.path})")
            if rec.error:
                lines.append("    " + rec.error.strip().replace("\n", "\n    "))
        return "\n".join(lines) + "\n"

    def _register_gdb_command(self):
        app = self

        class GdbrpcCommand(gdb.Command):
            """gdbrpc status | gdbrpc restart"""

            def __init__(self):
                super().__init__("gdbrpc", gdb.COMMAND_SUPPORT)

            def invoke(self, arg, from_tty):
                arg = (arg or "").strip()
                if arg == "status":
                    gdb.write(app.status_text())
                elif arg == "restart":
                    app.restart()
                    gdb.write(app.status_text())
                else:
                    gdb.write("usage: gdbrpc status | gdbrpc restart\n")

        try:
            GdbrpcCommand()
        except gdb.error:
            pass  # already registered (e.g. bootstrap sourced twice)


_app = None


def start(config=None):
    """Entry point called from the shim. Returns the App singleton."""
    global _app
    config = config or load_config()
    _app = App(config)
    _app.start()
    return _app