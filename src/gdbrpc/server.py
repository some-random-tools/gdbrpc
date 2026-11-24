"""TCP server: JSON-RPC 2.0, newline-delimited, localhost only (decision #5/#6).

Threading model (see DESIGN.md section 4):
- one reader thread per connection: parse, static-validate, submit, wait
- one writer thread per connection: drain the outbound queue (responses
  + notifications) and write to the socket — so the main thread never
  touches a socket and a dead/slow client can never block GDB.
"""

import json
import queue
import socket
import threading

from .errors import GdbrpcError
from .queue import RequestItem

# JSON-RPC 2.0 error codes
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32000


class Connection:
    def __init__(self, app, sock, addr):
        self.app = app
        self.sock = sock
        self.addr = addr
        self.outbound = queue.Queue()
        self.alive = True
        self._close_lock = threading.Lock()
        threading.Thread(
            target=self._write_loop, daemon=True,
            name=f"gdbrpc-writer-{addr}",
        ).start()
        threading.Thread(
            target=self._read_loop, daemon=True,
            name=f"gdbrpc-reader-{addr}",
        ).start()

    # -- outbound --------------------------------------------------------

    def notify(self, method, params):
        """Queue a notification (called from the main thread — non-blocking)."""
        self._put({"jsonrpc": "2.0", "method": method, "params": params})

    def send_result(self, rid, result):
        self._put({"jsonrpc": "2.0", "id": rid, "result": result})

    def send_error(self, rid, code, message, data=None):
        err = {"code": code, "message": message}
        if data is not None:
            err["data"] = data
        self._put({"jsonrpc": "2.0", "id": rid, "error": err})

    def _put(self, obj):
        if self.alive:
            self.outbound.put(obj)

    def _write_loop(self):
        while True:
            item = self.outbound.get()
            if item is None:
                # Input closed (client EOF): everything queued *before*
                # this sentinel was already flushed by the loop above —
                # only now hard-close. (Fixes the lost-response race
                # with half-closing one-shot clients like `nc`.)
                self.close()
                return
            try:
                self.sock.sendall(
                    (json.dumps(item) + "\n").encode("utf-8")
                )
            except OSError:
                self.close()
                return

    # -- inbound -----------------------------------------------------------

    def _read_loop(self):
        buf = b""
        while self.alive:
            try:
                chunk = self.sock.recv(65536)
            except OSError:
                # socket error: same treatment as EOF — let the writer
                # flush, then close.
                self.outbound.put(None)
                return
            if not chunk:
                # EOF: client half-closed (one-shot `nc` style). Do NOT
                # close the socket here — the response may not be sent yet.
                # The sentinel lands *behind* any pending response in the
                # outbound queue, so the writer flushes first, then closes.
                self.outbound.put(None)
                return
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                if line.strip():
                    try:
                        self._handle_line(line)
                    except Exception:
                        # never let a broken request kill the reader
                        import traceback
                        self.send_error(
                            None, INTERNAL_ERROR, "reader error",
                            traceback.format_exc(),
                        )

    def _handle_line(self, line):
        try:
            msg = json.loads(line)
        except Exception:
            self.send_error(None, PARSE_ERROR, "parse error")
            return
        if not isinstance(msg, dict):
            self.send_error(None, INVALID_REQUEST, "invalid request")
            return
        method = msg.get("method")
        if not isinstance(method, str):
            self.send_error(
                msg.get("id"), INVALID_REQUEST, "invalid request: missing method"
            )
            return
        rid = msg.get("id")
        if rid is None and "id" not in msg:
            # client -> server notifications are not supported in v0
            return
        params = msg.get("params")
        if params is None:
            params = {}
        if not isinstance(params, dict):
            self.send_error(rid, INVALID_PARAMS, "params must be an object")
            return

        # static validation in this thread (pure Python, no gdb involved)
        try:
            self.app.methods.check_request(method, params)
        except GdbrpcError as e:
            self.send_error(rid, e.code, e.message, e.data)
            return

        item = RequestItem(self, rid, method, params)
        self.app.dispatcher.submit(item)
        # Queue-only model (decision #2): the client waits until GDB
        # processes the request in its event loop.
        item.done.wait()
        if item.error is not None:
            self.send_error(
                rid,
                item.error.get("code", INTERNAL_ERROR),
                item.error.get("message", "internal error"),
                item.error.get("data"),
            )
        else:
            self.send_result(rid, item.result)

    # -- teardown ----------------------------------------------------------

    def close(self):
        """Hard close (server stop, writer error, EOF flush). Idempotent."""
        with self._close_lock:
            if not self.alive:
                return
            self.alive = False
            try:
                self.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                self.sock.close()
            except OSError:
                pass
        self.outbound.put(None)
        self.app.on_connection_closed(self)


class RpcServer:
    def __init__(self, app, host="127.0.0.1"):
        self.app = app
        self.host = host
        self.port = None
        self._listener = None
        self._conns = []
        self._lock = threading.Lock()

    def start(self, port):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((self.host, port))
        except OSError as e:
            raise GdbrpcError(
                -32000, f"cannot bind {self.host}:{port}: {e}"
            )
        s.listen(16)
        self._listener = s
        self.port = port
        threading.Thread(
            target=self._accept_loop, daemon=True, name="gdbrpc-accept"
        ).start()

    def stop(self):
        listener, self._listener = self._listener, None
        self.port = None
        if listener is not None:
            try:
                listener.close()
            except OSError:
                pass
        with self._lock:
            conns = list(self._conns)
        for conn in conns:
            conn.close()

    def is_running(self):
        return self._listener is not None

    def forget(self, conn):
        with self._lock:
            try:
                self._conns.remove(conn)
            except ValueError:
                pass

    def _accept_loop(self):
        while True:
            listener = self._listener
            if listener is None:
                return
            try:
                sock, addr = listener.accept()
            except OSError:
                return
            conn = Connection(self.app, sock, addr)
            with self._lock:
                self._conns.append(conn)