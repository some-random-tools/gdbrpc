"""Headless RPC client session: connect, register views, auto-reconnect.

Runs in the *venv* Python (never inside GDB) — pure stdlib, so it adds
no constraints on the runtime package. Used by the CLI (attach/exec)
and later by the GUI client.
"""

import json
import socket
import threading
import time


class RpcClientError(Exception):
    """A JSON-RPC error response or connection failure."""

    def __init__(self, code, message, data=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


def render_text(data):
    """Views deliver text-oriented payloads (decision: render like console
    output). Render rules:

    - str: as-is
    - dict with a single string value: that value
    - dict with "text" (+ optional "line"): the whole text, with the
      current line marked "=> " (GUI clients highlight it instead)
    - anything else: pretty-printed JSON fallback
    """
    if isinstance(data, str):
        return data
    if isinstance(data, dict):
        if isinstance(data.get("text"), str):
            text = data["text"]
            line = data.get("line")
            if line and isinstance(line, int) and 1 <= line <= text.count("\n") + 1:
                lines = text.splitlines()
                lines[line - 1] = "> " + lines[line - 1]
                return "\n".join(lines)
            return text
        values = list(data.values())
        if len(values) == 1 and isinstance(values[0], str):
            return values[0]
    return json.dumps(data, indent=2, default=str)


class Session:
    """Auto-reconnecting client session.

    - ``register(view, params)`` records a view registration; it is
      (re-)sent on every (re)connect, so pushed updates survive gdb
      restarts (decision #5 of the client discussion).
    - ``request(method, params)`` issues a blocking JSON-RPC request.
    - ``run()`` blocks: connect -> re-register -> dispatch notifications
      until ``stop()``; on disconnect it retries after ``retry_delay``.
    """

    def __init__(self, port, host="127.0.0.1",
                 on_update=None, on_error=None, on_status=None):
        self.host = host
        self.port = port
        self.on_update = on_update    # fn(view, trigger, data)
        self.on_error = on_error      # fn(view, trigger, error, traceback)
        self.on_status = on_status    # fn(text)
        self._registrations = []      # [(name, params)]
        self._sock = None
        self._buf = b""
        self._id = 0
        self._pending = {}             # id -> {"ev", "resp"}
        self._idlock = threading.Lock()
        self._stop = threading.Event()

    # -- public -------------------------------------------------------

    def register(self, name, params=None):
        with self._idlock:
            self._registrations = [
                (n, p) for n, p in self._registrations if n != name
            ]
            self._registrations.append((name, params or {}))
        if self._sock is not None:
            try:
                self.request("views.register", {"name": name, "params": params or {}})
            except RpcClientError as e:
                if self.on_error:
                    self.on_error(name, "register", f"{e.code}: {e.message}", e.data)

    def connect(self):
        """Connect once; re-send all registrations. True on success."""
        try:
            self._sock = socket.create_connection(
                (self.host, self.port), timeout=5
            )
            # timeout is only for dialing; attached sockets block forever —
            # a socket.timeout on recv would otherwise be mistaken for
            # EOF and trigger a pointless reconnect loop
            self._sock.settimeout(None)
        except OSError:
            self._sock = None
            return False
        self._buf = b""
        self._pending = {}
        threading.Thread(
            target=self._read_loop, daemon=True, name="gdbrpc-client-reader"
        ).start()
        for name, params in list(self._registrations):
            try:
                self.request("views.register", {"name": name, "params": params})
            except RpcClientError as e:
                if self.on_error:
                    self.on_error(name, "register", f"{e.code}: {e.message}", e.data)
        return True

    def request(self, method, params=None, timeout=30.0):
        sock = self._sock
        if sock is None:
            raise RpcClientError(-32000, "not connected")
        with self._idlock:
            self._id += 1
            rid = self._id
        slot = {"ev": threading.Event(), "resp": None}
        self._pending[rid] = slot
        payload = {
            "jsonrpc": "2.0", "id": rid, "method": method,
            "params": params or {},
        }
        try:
            sock.sendall((json.dumps(payload) + "\n").encode("utf-8"))
        except OSError as e:
            self._pending.pop(rid, None)
            raise RpcClientError(-32000, f"send failed: {e}")
        if not slot["ev"].wait(timeout):
            self._pending.pop(rid, None)
            raise RpcClientError(-32000, f"timeout waiting for '{method}'")
        self._pending.pop(rid, None)
        resp = slot["resp"]
        if resp is None:
            raise RpcClientError(-32000, "connection lost")
        if "error" in resp:
            err = resp["error"]
            raise RpcClientError(
                err.get("code", -32000), err.get("message", "error"),
                err.get("data"),
            )
        return resp.get("result")

    def run(self, retry_delay=2.0):
        """Blocking attach loop with auto-reconnect."""
        while not self._stop.is_set():
            if self.connect():
                if self.on_status:
                    self.on_status(f"connected to {self.host}:{self.port}")
                while self._sock is not None and not self._stop.is_set():
                    time.sleep(0.2)
            if self._stop.is_set():
                break
            if self.on_status:
                self.on_status(f"connection lost; retrying in {retry_delay}s")
            self._stop.wait(retry_delay)

    def stop(self):
        self._stop.set()
        self.close()

    def close(self):
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    # -- internals ------------------------------------------------------

    def _read_loop(self):
        sock = self._sock
        while True:
            try:
                chunk = sock.recv(65536)
            except OSError:
                chunk = b""
            if not chunk:
                break
            self._buf += chunk
            while b"\n" in self._buf:
                line, self._buf = self._buf.split(b"\n", 1)
                if not line.strip():
                    continue
                try:
                    msg = json.loads(line)
                except Exception:
                    continue
                self._dispatch(msg)
        if self._sock is sock:
            self._sock = None
            for slot in self._pending.values():
                slot["resp"] = None
                slot["ev"].set()

    def _dispatch(self, msg):
        rid = msg.get("id")
        if rid is not None and rid in self._pending:
            self._pending[rid]["resp"] = msg
            self._pending[rid]["ev"].set()
            return
        method = msg.get("method", "")
        params = msg.get("params") or {}
        if method == "view.update" and self.on_update:
            self.on_update(
                params.get("view"), params.get("trigger"), params.get("data")
            )
        elif method == "view.error" and self.on_error:
            self.on_error(
                params.get("view"), params.get("trigger"),
                params.get("error"), params.get("traceback"),
            )