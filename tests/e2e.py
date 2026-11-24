#!/usr/bin/env python3
"""End-to-end test: real gdb + real inferior + a real RPC client.

Starts gdb (which boots the gdbrpc server via the shim), connects over
TCP, exercises the full v0 method surface and the view push model.
"""

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = Path("~/.config/gdbrpc").expanduser()
ENV_FILE = CONFIG_DIR / ".env"
SHIM = CONFIG_DIR / "bootstrap.py"


def _port():
    if os.environ.get("GDBRPC_PORT"):
        return int(os.environ["GDBRPC_PORT"])
    for line in ENV_FILE.read_text().splitlines():
        if line.startswith("GDBRPC_PORT="):
            return int(line.partition("=")[2])
    return 8944


class Client:
    def __init__(self, port):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=20)
        self.sock.settimeout(20)
        self.buf = b""
        self.notes = []  # notifications received so far
        self._id = 0

    def _readline(self):
        while b"\n" not in self.buf:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise EOFError("server closed connection")
            self.buf += chunk
        line, self.buf = self.buf.split(b"\n", 1)
        return line

    def request(self, method, params=None):
        self._id += 1
        rid = self._id
        payload = {
            "jsonrpc": "2.0", "id": rid, "method": method,
            "params": params or {},
        }
        self.sock.sendall((json.dumps(payload) + "\n").encode())
        while True:
            msg = json.loads(self._readline())
            if msg.get("id") == rid:
                return msg
            if "method" in msg:
                self.notes.append(msg)
                continue
            raise AssertionError(f"unexpected message: {msg}")

    def wait_note(self, name, timeout=15):
        """Wait for a notification with the given method name."""
        deadline = time.time() + timeout
        while True:
            for i, n in enumerate(self.notes):
                if n.get("method") == name:
                    return self.notes.pop(i)
            remaining = deadline - time.time()
            if remaining <= 0:
                raise TimeoutError(f"no '{name}' notification within {timeout}s")
            self.sock.settimeout(remaining)
            try:
                chunk = self.sock.recv(65536)
            except socket.timeout:
                continue
            if not chunk:
                raise EOFError("server closed connection")
            self.buf += chunk
            while b"\n" in self.buf:
                line, self.buf = self.buf.split(b"\n", 1)
                self.notes.append(json.loads(line))


failures = []


def check(cond, label):
    print(("PASS  " if cond else "FAIL  ") + label)
    if not cond:
        failures.append(label)


def main():
    port = _port()
    log = open(ROOT / "tests" / "e2e.log", "w+b")
    # GDBRPC_AUTOLOAD=0 keeps the lazy-loading behavior under test;
    # autoload itself is verified in a second phase below.
    env = dict(os.environ)
    env["GDBRPC_PORT"] = str(port)
    env["GDBRPC_AUTOLOAD"] = "0"
    gdb = subprocess.Popen(
        [
            "gdb", "-q", "-nx", str(ROOT / "tests" / "target"),
            "-ex", "set pagination off",
            "-ex", f"source {SHIM}",
            "-ex", "break main",
            "-ex", "break work",
            "-ex", "run",
        ],
        stdin=subprocess.PIPE, stdout=log, stderr=subprocess.STDOUT,
        env=env,
    )
    try:
        time.sleep(3)  # let gdb boot: shim, plugin scan, server
        c = Client(port)

        # -- server.info ------------------------------------------------
        r = c.request("server.info")
        check("result" in r, "server.info responds")
        info = r.get("result", {})
        check(info.get("version") == "0.1.0", f"version: {info.get('version')}")
        check("views.register" in info.get("methods", []),
              "server.info lists methods")

        # -- plugins.list (discovery, not loaded) -------------------------
        r = c.request("plugins.list")
        recs = {p["name"]: p for p in r["result"]}
        check("example" in recs, "example plugin discovered")
        check(recs["example"]["state"] == "discovered",
              "example is 'discovered' before load")
        check(recs["example"]["commands"] == ["echo"],
              "declared commands visible pre-load")
        check(set(recs["example"]["views"]) == {
            "registers", "backtrace", "breakpoints", "frame", "locals",
            "threads", "disasm", "stack", "code", "vmmap", "auxv",
            "env", "sections", "files", "floats", "allregisters",
        }, "declared views visible pre-load")
        check("dbg" in recs, "dbg plugin discovered")
        check(recs["dbg"]["state"] == "discovered",
              "dbg is 'discovered' before load")

        # -- commands.list / views.list before load ------------------------
        r = c.request("commands.list")
        check(r["result"] == [], "commands.list empty before load")
        r = c.request("views.list")
        check(r["result"] == [], "views.list empty before load")

        # -- load ----------------------------------------------------------
        r = c.request("plugins.load", {"name": "example"})
        check("loaded" in r.get("result", {}) or r.get("error") is None,
              f"plugins.load: {r}")

        # -- commands.list / help / views.list after load -------------------
        r = c.request("commands.list")
        names = {e["name"]: e for e in r["result"]}
        check("echo" in names, "echo command registered")
        check(names["echo"]["plugin"] == "example", "echo has plugin attr")
        check("registers" in names, "registers view in commands.list")
        r = c.request("commands.help", {"name": "echo"})
        helpd = r.get("result", {})
        check("count" in json.dumps(helpd.get("options", [])),
              "commands.help shows options")
        check("message" in helpd.get("help", ""), "commands.help formatted text")
        r = c.request("views.list")
        views = {v["name"]: v for v in r["result"]}
        check(views["registers"]["triggers"] == ["stop"],
              "views.list shows triggers")

        # -- errors ---------------------------------------------------------
        r = c.request("commands.help", {"name": "nope"})
        check(r.get("error", {}).get("code") == -32601,
              "unknown command -> -32601")
        r = c.request("views.unregister", {"name": "registers"})
        check(r.get("error", {}).get("code") == -32000,
              "unregister of unregistered view -> error")
        r = c.request("bogus.method")
        check(r.get("error", {}).get("code") == -32601,
              "unknown method -> -32601")

        # -- views.register + immediate snapshot ----------------------------
        c.notes.clear()
        r = c.request("views.register",
                       {"name": "registers", "params": {"filter": "rip,rsp"}})
        check(r.get("result", {}).get("registered") == "registers",
              "views.register responds")
        snap = c.wait_note("view.update")
        data = snap["params"]
        check(data["view"] == "registers", "snapshot view name")
        check("rsp" in data["data"]["registers"],
              f"snapshot registers filtered: {data['data']['registers'][:60]!r}")

        r = c.request("views.register", {"name": "backtrace"})
        check(r.get("result", {}).get("registered") == "backtrace",
              "backtrace registered")
        snap = c.wait_note("view.update")
        check("main" in snap["params"]["data"]["backtrace"],
              "backtrace snapshot contains main")

        r = c.request("views.register", {"name": "breakpoints"})
        check(r.get("result", {}).get("registered") == "breakpoints",
              "breakpoints view registered")
        snap = c.wait_note("view.update")
        check("breakpoint" in snap["params"]["data"]["breakpoints"].lower(),
              "breakpoints view snapshot")

        # code view: WHOLE source file + current line (no ANSI)
        r = c.request("views.register", {"name": "code"})
        snap = c.wait_note("view.update")
        data = snap["params"]["data"]
        check("text" in data and "line" in data, "code view payload has text+line")
        code_text = data["text"]
        check(isinstance(data["line"], int) and data["line"] >= 1,
              f"code view current line: {data.get('line')}")
        check("\x1b" not in code_text and "[31m" not in code_text,
              "code view has no ANSI escapes")
        check("main" in code_text, "code view shows source")
        check("return total" in code_text,
              "code view shows the WHOLE file")
        r = c.request("views.unregister", {"name": "code"})
        check(r.get("result", {}).get("unregistered") == "code",
              "unregister code view")
        r = c.request("views.unregister", {"name": "breakpoints"})
        check(r.get("result", {}).get("unregistered") == "breakpoints",
              "unregister breakpoints view")

        # -- stop-triggered push ---------------------------------------------
        # gdb is stopped at main; continue -> stops in work() -> stop event
        # -> both views refresh and push.
        c.notes.clear()
        gdb.stdin.write(b"continue\n")
        gdb.stdin.flush()
        got_regs = got_bt = False
        deadline = time.time() + 15
        while time.time() < deadline and not (got_regs and got_bt):
            n = c.wait_note("view.update", timeout=max(0.5, deadline - time.time()))
            if n["params"]["view"] == "registers":
                got_regs = "rsp" in n["params"]["data"]["registers"]
            if n["params"]["view"] == "backtrace":
                got_bt = "work" in n["params"]["data"]["backtrace"]
        check(got_regs, "stop push: registers view")
        check(got_bt, "stop push: backtrace view (in work)")

        # -- unregister then stop again: only remaining views push -----------
        r = c.request("views.unregister", {"name": "backtrace"})
        check(r.get("result", {}).get("unregistered") == "backtrace",
              "views.unregister")
        # breakpoints/code were already unregistered above
        c.notes.clear()
        gdb.stdin.write(b"continue\n")
        gdb.stdin.flush()
        n = c.wait_note("view.update")
        check(n["params"]["view"] == "registers", "only registered views push")
        time.sleep(0.5)
        check(all(x["params"]["view"] == "registers" for x in c.notes),
              "no backtrace notification after unregister")

        # -- unload / error paths ---------------------------------------------
        r = c.request("plugins.unload", {"name": "example"})
        check("unloaded" in r.get("result", {}), "plugins.unload")
        r = c.request("plugins.unload", {"name": "example"})
        check(r.get("error", {}).get("code") == -32000,
              "double unload -> error (decision #8)")
        r = c.request("commands.list")
        check(r["result"] == [], "commands gone after unload")

        # -- load_all / unload_all ----------------------------------------------
        r = c.request("plugins.load_all")
        check(r["result"]["results"]["example"]["ok"] is True, "load_all")
        check(r["result"]["results"]["dbg"]["ok"] is True, "load_all loads dbg")
        r = c.request("plugins.unload_all")
        check(r["result"]["results"]["example"]["ok"] is True, "unload_all")
        r = c.request("plugins.load", {"name": "example"})
        check("loaded" in r.get("result", {}), "reload after unload_all")

        # -- reload semantics on loaded plugin -----------------------------------
        r = c.request("plugins.load", {"name": "example"})
        check(r.get("result", {}).get("loaded") == "example",
              "load on loaded -> reload (decision #7)")

        # -- commands.execute (client decision #4) ------------------------------
        r = c.request("commands.execute",
                      {"name": "echo",
                       "params": {"message": "hi", "count": 2, "upper": True}})
        check(r.get("result", {}).get("messages") == ["HI", "HI"],
              "commands.execute returns command result")
        r = c.request("commands.execute", {"name": "echo", "params": {}})
        check(r.get("error", {}).get("code") == -32602,
              "commands.execute missing required -> -32602")
        r = c.request("commands.execute", {"name": "backtrace", "params": {}})
        check(r.get("error", {}).get("code") == -32000,
              "commands.execute on a view -> error")

        # -- dbg plugin: remote-control commands ------------------------------
        r = c.request("plugins.load", {"name": "dbg"})
        check(r.get("result", {}).get("loaded") == "dbg", "dbg plugin loaded")
        r = c.request("commands.execute",
                      {"name": "eval", "params": {"expression": "1 + 2"}})
        check(r.get("result", {}).get("value") == "3", "dbg.eval expression")
        r = c.request("commands.execute",
                      {"name": "eval", "params": {"expression": "&i"}})
        addr = r.get("result", {}).get("value", "").split()[0]
        check(addr.startswith("0x"), f"dbg.eval &i -> {addr}")
        r = c.request("commands.execute",
                      {"name": "memory",
                       "params": {"address": addr, "count": 4, "fmt": "x"}})
        check("0x" in r.get("result", {}).get("memory", ""),
              "dbg.memory dump")
        r = c.request("commands.execute",
                      {"name": "break", "params": {"location": "work"}})
        out = r.get("result", {}).get("output", "")
        check("Breakpoint" in out, f"dbg.break via RPC: {out.strip()}")
        bpnum = int(out.split()[1])
        r = c.request("commands.execute",
                      {"name": "delete-breakpoint", "params": {"number": bpnum}})
        check(r.get("error") is None, "dbg.delete-breakpoint")
        r = c.request("commands.execute", {"name": "step", "params": {}})
        check(r.get("result", {}).get("resumed") == "step",
              "dbg.step resumes (non-blocking)")
        n = c.wait_note("view.update")
        check(n["params"]["view"] == "registers",
              "stop push arrives after remote step")
        r = c.request("commands.execute", {"name": "continue", "params": {}})
        check(r.get("result", {}).get("resumed") == "continue",
              "dbg.continue resumes (non-blocking)")
        n = c.wait_note("view.update")
        check(n["params"]["view"] == "registers",
              "stop push arrives after remote continue")

        # -- heap plugin (parser from ../heap-plugin) -------------------------
        r = c.request("plugins.load", {"name": "heap"})
        check(r.get("result", {}).get("loaded") == "heap", "heap plugin loaded")

        def snapshot_of(view, timeout=15):
            r = c.request("views.register", {"name": view})
            check(r.get("result", {}).get("registered") == view,
                  f"{view} registered")
            deadline = time.time() + timeout
            while time.time() < deadline:
                try:
                    n = c.wait_note("view.update", timeout=3)
                except TimeoutError:
                    break
                if n["params"]["view"] == view:
                    return n["params"].get("data")
            check(False, f"{view}: snapshot received")
            return {}

        # stop after the frees: heap populated (tcache bins filled)
        r = c.request("commands.execute",
                      {"name": "break", "params": {"location": "heap_pause"}})
        check("Breakpoint" in r.get("result", {}).get("output", ""),
              "break heap_pause via dbg")
        r = c.request("commands.execute", {"name": "continue", "params": {}})
        check(r.get("result", {}).get("resumed") == "continue",
              "continue to heap_pause")
        time.sleep(1.5)  # land at heap_pause (stop + pushes)

        data = snapshot_of("heap-tcache")
        text = data.get("text", "")
        check("bin" in text and "count 1" in text,
              f"heap-tcache shows freed bins: {text.splitlines()[0][:60]!r}")

        data = snapshot_of("heap-arena")
        text = data.get("text", "")
        check("main_arena" in text and "system_mem" in text,
              "heap-arena shows main_arena metadata")

        data = snapshot_of("heap-fastbins")
        check("arena" in data.get("text", ""), "heap-fastbins renders")

        data = snapshot_of("heap-bins")
        check("unsorted" in data.get("text", ""), "heap-bins renders")

        for view in ("heap-tcache", "heap-arena", "heap-fastbins",
                     "heap-bins"):
            c.request("views.unregister", {"name": view})

        # -- gdbrpc status via gdb prompt ------------------------------------------
        gdb.stdin.write(b"gdbrpc status\n")
        gdb.stdin.flush()
        time.sleep(1)

        print("\n-- client checks done, shutting down gdb --")
    finally:
        try:
            gdb.stdin.write(b"kill\nquit\n")
            gdb.stdin.flush()
        except (BrokenPipeError, OSError):
            pass
        try:
            gdb.wait(timeout=10)
        except subprocess.TimeoutExpired:
            gdb.kill()
        log.close()
        logdata = open(ROOT / "tests" / "e2e.log", "rb").read().decode(
            "utf-8", "replace"
        )
        check("[gdbrpc] v0.1.0 listening" in logdata,
              "gdb log shows server startup banner")
        check("gdbrpc 0.1.0" in logdata and "server: listening" in logdata,
              "gdbrpc status output present in log")

    # -- phase 2: default .env behavior = autoload plugins at startup -----
    print("\n-- phase 2: autoload (default) --")
    env2 = dict(os.environ)
    env2["GDBRPC_PORT"] = str(port + 1)
    env2["GDBRPC_AUTOLOAD"] = "1"   # phase 1 runs lazy; force autoload
    gdb2 = subprocess.Popen(
        ["gdb", "-q", "-nx", str(ROOT / "tests" / "target"),
         "-ex", "set pagination off",
         "-ex", f"source {SHIM}"],
        stdin=subprocess.PIPE,
        stdout=open(ROOT / "tests" / "e2e2.log", "w+b"),
        stderr=subprocess.STDOUT, env=env2,
    )
    try:
        time.sleep(4)
        c2 = Client(port + 1)
        r = c2.request("plugins.list")
        states = {p["name"]: p["state"] for p in r["result"]}
        check(states.get("example") == "loaded" and
              states.get("dbg") == "loaded",
              f"autoload: plugins loaded at startup: {states}")
        r = c2.request("views.list")
        check(len(r["result"]) >= 9, "autoload: views available immediately")
        r = c2.request("commands.execute",
                      {"name": "eval", "params": {"expression": "1"}})
        check(r.get("error") is None, "autoload: command executable at startup")
        c2.sock.close()
    finally:
        try:
            gdb2.stdin.write(b"quit\n")
            gdb2.stdin.flush()
        except (BrokenPipeError, OSError):
            pass
        try:
            gdb2.wait(timeout=10)
        except subprocess.TimeoutExpired:
            gdb2.kill()

    print()
    if failures:
        print(f"FAILED ({len(failures)}):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())