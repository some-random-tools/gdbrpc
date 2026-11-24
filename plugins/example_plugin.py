"""Example plugin: view showcase.

All views render as console-like text (client renders them into the whole
screen). Options are validated centrally before provide() runs.
"""

import re

import gdb

from gdbrpc.plugin import Command, Option, Plugin, View

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _clean(text):
    """Strip ANSI escape sequences (gdb 16 syntax-highlights e.g.
    `list` output; panes want plain text)."""
    return _ANSI.sub("", text)


def _exec(cmd):
    out = gdb.execute(cmd, to_string=True)
    return _clean(out) if out.strip() else "(no output)"


class EchoCommand(Command):
    name = "echo"
    description = "Echo a message (option schema demo)"
    options = [
        Option("message", type="str", required=True,
               help="Message to echo"),
        Option("count", type="int", required=False, default=1,
               help="Repeat count"),
        Option("upper", type="bool", required=False, default=False,
               help="Uppercase the message"),
    ]

    def run(self, opts):
        message = opts["message"]
        if opts["upper"]:
            message = message.upper()
        return {"messages": [message] * opts["count"]}


class RegistersView(View):
    name = "registers"
    description = "Register values (info registers)"
    triggers = ["stop"]
    options = [
        Option("filter", type="str", required=False, default=None,
               help="Comma-separated register names to keep (optional)"),
    ]

    def provide(self, opts):
        out = _clean(gdb.execute("info registers", to_string=True))
        if opts["filter"]:
            wanted = {r.strip() for r in opts["filter"].split(",")}
            out = "\n".join(
                line
                for line in out.splitlines()
                if line.split() and line.split()[0] in wanted
            )
        return {"registers": out}


class BacktraceView(View):
    name = "backtrace"
    description = "Call stack (bt)"
    triggers = ["stop"]
    options = []

    def provide(self, opts):
        return {"backtrace": _clean(gdb.execute("bt", to_string=True))}


class BreakpointsView(View):
    name = "breakpoints"
    description = "Breakpoint list (info breakpoints)"
    triggers = ["stop"]
    options = []

    def provide(self, opts):
        return {"breakpoints": _exec("info breakpoints")}


class FrameView(View):
    name = "frame"
    description = "Current frame details (info frame)"
    triggers = ["stop"]
    options = []

    def provide(self, opts):
        return {"frame": _exec("info frame")}


class LocalsView(View):
    name = "locals"
    description = "Local variables of the current frame (info locals)"
    triggers = ["stop"]
    options = []

    def provide(self, opts):
        return {"locals": _exec("info locals")}


class ThreadsView(View):
    name = "threads"
    description = "Thread list (info threads)"
    triggers = ["stop"]
    options = []

    def provide(self, opts):
        return {"threads": _exec("info threads")}


class DisasmView(View):
    name = "disasm"
    description = "Disassembly around the program counter (x/Ni $pc)"
    triggers = ["stop"]
    options = [
        Option("count", type="int", required=False, default=10,
               help="Number of instructions"),
    ]

    def provide(self, opts):
        return {"disassembly": _exec(f"x/{opts['count']}i $pc")}


class StackView(View):
    name = "stack"
    description = "Raw stack entries from $sp (x/Nxg $sp — 64-bit)"
    triggers = ["stop"]
    options = [
        Option("count", type="int", required=False, default=32,
               help="Number of 64-bit entries"),
    ]

    def provide(self, opts):
        return {"stack": _exec(f"x/{opts['count']}xg $sp")}


class CodeView(View):
    name = "code"
    description = "Whole source file with the current line highlighted"
    triggers = ["stop"]
    options = []

    def provide(self, opts):
        # current source location straight from gdb's symbol table
        try:
            sal = gdb.selected_frame().find_sal()
        except gdb.error as e:
            return {"file": None, "text": f"(no frame: {e})", "line": None}
        symtab, line = sal.symtab, sal.line
        if symtab is None or not line:
            return {"file": None, "text": "(no source location)", "line": None}
        path = symtab.fullname()
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError as e:
            return {"file": path, "text": f"(cannot read {path}: {e})",
                    "line": None}
        return {"file": path, "text": _clean(text), "line": line}


class AllRegistersView(View):
    name = "allregisters"
    description = "All registers incl. eflags/segments (info all-registers)"
    triggers = ["stop"]
    options = [
        Option("filter", type="str", required=False, default=None,
               help="Comma-separated register names to keep (optional)"),
    ]

    def provide(self, opts):
        out = _exec("info all-registers")
        if opts["filter"]:
            wanted = {r.strip() for r in opts["filter"].split(",")}
            out = "\n".join(
                line
                for line in out.splitlines()
                if line.split() and line.split()[0] in wanted
            )
        return {"allregisters": out}


class VmmapView(View):
    name = "vmmap"
    description = "Process memory map with permissions (info proc mappings)"
    triggers = ["stop"]
    options = []

    def provide(self, opts):
        return {"vmmap": _exec("info proc mappings")}


class AuxvView(View):
    name = "auxv"
    description = "Auxiliary vector (canary AT_RANDOM, vDSO, stack top)"
    triggers = ["stop"]
    options = []

    def provide(self, opts):
        return {"auxv": _exec("info auxv")}


class EnvView(View):
    description = "Environment variables (leak targets / ROP helper)"
    name = "env"
    triggers = ["stop"]
    options = [
        Option("filter", type="str", required=False, default=None,
               help="Only show variables containing this substring"),
    ]

    def provide(self, opts):
        out = _exec("show environment")
        if opts["filter"]:
            needle = opts["filter"].lower()
            out = "\n".join(
                line for line in out.splitlines()
                if needle in line.lower()
            )
        return {"env": out}


class SectionsView(View):
    description = "ELF sections with addresses and permissions (RE)"
    name = "sections"
    triggers = ["stop"]
    options = [
        Option("filter", type="str", required=False, default=None,
               help="Only show sections containing this substring"),
    ]

    def provide(self, opts):
        out = _exec("maintenance info sections")
        if opts["filter"]:
            needle = opts["filter"].lower()
            out = "\n".join(
                line for line in out.splitlines()
                if needle in line.lower()
            )
        return {"sections": out}


class FilesView(View):
    description = "Loaded object files, symbols and entry point (info files)"
    name = "files"
    triggers = ["stop"]
    options = []

    def provide(self, opts):
        return {"files": _exec("info files")}


class FloatView(View):
    description = "Floating point / SIMD registers xmm..ymm (info float)"
    name = "floats"
    triggers = ["stop"]
    options = []
    def provide(self, opts):
        return {"floats": _exec("info float")}


class ExamplePlugin(Plugin):
    name = "example"
    description = "View showcase: registers, backtrace, breakpoints, frame, " \
                  "locals, threads, disasm, stack, code + the echo command"
    version = "0.2.0"
    commands = [EchoCommand]
    views = [
        RegistersView,
        BacktraceView,
        BreakpointsView,
        FrameView,
        LocalsView,
        ThreadsView,
        DisasmView,
        StackView,
        CodeView,
        VmmapView,
        AuxvView,
        EnvView,
        SectionsView,
        FilesView,
        FloatView,
        AllRegistersView,
    ]

    def setup(self):
        pass

    def teardown(self):
        pass