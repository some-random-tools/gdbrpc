"""Debugg plugin: remote-control commands.

Everything you would type at the (gdb) prompt — driven over RPC instead.
Runs on GDB's main thread like every handler; blocking commands (continue,
step, ...) hold their response until the inferior stops again.
"""

import re

import gdb

from gdbrpc.plugin import Command, Option, Plugin

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _exec(cmd):
    out = gdb.execute(cmd, to_string=True)
    out = _ANSI.sub("", out)
    return out if out.strip() else "(no output)"


class ResumeCommand(Command):
    """Base for resuming commands.

    GDB cannot block inside post_event handlers, so these commands resume
    the inferior and return immediately — the resulting stop is reported
    via the stop-triggered view pushes, not via this response.
    """

    options = []

    def run(self, opts):
        gdb.execute(self.name, to_string=True)
        return {"resumed": self.name}


class ContinueCommand(ResumeCommand):
    name = "continue"
    description = "Resume the inferior (non-blocking; stop via view pushes)"


class StepCommand(ResumeCommand):
    name = "step"
    description = "Step one line, into calls (non-blocking; stop via views)"


class NextCommand(ResumeCommand):
    name = "next"
    description = "Step one line, over calls (non-blocking; stop via views)"


class StepInstructionCommand(ResumeCommand):
    name = "stepi"
    description = "Step one instruction (non-blocking; stop via views)"


class FinishCommand(ResumeCommand):
    name = "finish"
    description = "Run until frame returns (non-blocking; stop via views)"


class EvalCommand(Command):
    name = "eval"
    description = "Evaluate an expression in the current frame"
    options = [
        Option("expression", type="str", required=True,
               help="Any GDB expression, e.g. 'total', '&main', 'x/4xw buf'"),
    ]

    def run(self, opts):
        value = gdb.parse_and_eval(opts["expression"])
        return {"expression": opts["expression"], "value": str(value)}


class MemoryCommand(Command):
    name = "memory"
    description = "Dump memory (x/NF ADDR)"
    options = [
        Option("address", type="str", required=True,
               help="Address or expression evaluating to an address"),
        Option("count", type="int", required=False, default=16,
               help="Number of units"),
        Option("fmt", type="enum", required=False, default="x",
               enum_values=["x", "d", "u", "t", "c"],
               help="Unit format: x hex, d decimal, u unsigned, "
                    "t binary, c char"),
    ]

    def run(self, opts):
        text = _exec(
            f"x/{opts['count']}{opts['fmt']} {opts['address']}"
        )
        return {"memory": text}


class BreakCommand(Command):
    name = "break"
    description = "Set a breakpoint at a location"
    options = [
        Option("location", type="str", required=True,
               help="Function, file:line or address"),
    ]

    def run(self, opts):
        return {"output": _exec(f"break {opts['location']}")}


class DeleteBreakpointCommand(Command):
    name = "delete-breakpoint"
    description = "Delete a breakpoint by number"
    options = [
        Option("number", type="int", required=True,
               help="Breakpoint number (see breakpoints view)"),
    ]

    def run(self, opts):
        return {"output": _exec(f"delete {opts['number']}")}


class DbgPlugin(Plugin):
    name = "dbg"
    description = "Remote-control debugger commands: continue, step, next, " \
                  "stepi, finish, eval, memory, break, delete-breakpoint"
    version = "0.1.0"
    commands = [
        ContinueCommand,
        StepCommand,
        NextCommand,
        StepInstructionCommand,
        FinishCommand,
        EvalCommand,
        MemoryCommand,
        BreakCommand,
        DeleteBreakpointCommand,
    ]
    views = []

    def setup(self):
        pass

    def teardown(self):
        pass