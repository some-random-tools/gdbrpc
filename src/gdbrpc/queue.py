"""Request queue: the bridge between the server threads and GDB's main thread.

Every RPC request crosses this boundary exactly once (see DESIGN.md
section 9): the reader thread fills a RequestItem and submits it; the
dispatcher hands it to GDB's event loop via gdb.post_event, where the
handler runs on the main thread; the reader then waits on item.done.
"""

import queue
import threading
import traceback


class RequestItem:
    __slots__ = ("conn", "id", "method", "params", "result", "error", "done")

    def __init__(self, conn, rid, method, params):
        self.conn = conn
        self.id = rid
        self.method = method
        self.params = params
        self.result = None
        self.error = None          # dict: {"code", "message", "data"?}
        self.done = threading.Event()


class Dispatcher:
    """Routes request items onto GDB's main thread.

    submit() is called from connection reader threads; _drain() runs on
    GDB's main thread (gdb.post_event is documented as thread-safe).
    """

    def __init__(self, handler):
        self._handler = handler    # fn(item), main thread
        self._queue = queue.Queue()

    def submit(self, item):
        self._queue.put(item)
        import gdb  # deferred: only importable inside GDB
        gdb.post_event(self._drain)

    def _drain(self):
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                return
            try:
                self._handler(item)
            except Exception:
                item.error = {
                    "code": -32000,
                    "message": "internal error",
                    "data": traceback.format_exc(),
                }
            item.done.set()