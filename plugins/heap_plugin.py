"""Heap plugin: glibc heap views (tcache, fastbins, bins, main_arena).

Reuses the parser from the heap-plugin project
(~/scratch/heap-plugin/src/server/heap-deep.py) — imported lazily on the
first snapshot, because importing it registers its own gdb commands and
prints banners (and needs libc symbols only present once the inferior
runs).
"""

import importlib.util
import os
import sys

import gdb

from gdbrpc.plugin import Plugin, View

HEAP_DEEP_PATH_DEFAULT = os.path.expanduser(
    "~/scratch/heap-plugin/src/server/heap-deep.py"
)
_VENDORED_HEAP_DEEP = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "_heap_deep.py"
)
_HEAP_MODULE = "gdbrpc_heap_deep"


def _heap_deep_path():
    """Resolve heap-deep.py: GDBRPC_HEAP_DEEP_PATH env var, then the
    .env file, then the default location. (The heap-plugin repo is NOT
    part of the gdbrpc repo — containers/other machines can point at a
    checkout; the vendored _heap_deep.py next to this plugin usually
    makes that unnecessary.)"""
    for source in (
        os.environ.get("GDBRPC_HEAP_DEEP_PATH"),
        _env_value("GDBRPC_HEAP_DEEP_PATH"),
    ):
        if source:
            return os.path.expanduser(source.strip())
    return HEAP_DEEP_PATH_DEFAULT


def _env_value(key):
    """Read one key from ~/.config/gdbrpc/.env (if present)."""
    path = os.path.expanduser("~/.config/gdbrpc/.env")
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith(key + "="):
                    return line.partition("=")[2]
    except OSError:
        pass
    return None


def _parser():
    """Lazily load a HeapParser: the vendored parser (plugins/_heap_deep.py,
    shipped with the repo — works everywhere) or, when overridden via
    GDBRPC_HEAP_DEEP_PATH / .env, the external heap-plugin checkout."""
    path = _heap_deep_path() if not os.path.isfile(_VENDORED_HEAP_DEEP) \
        else _VENDORED_HEAP_DEEP
    if path != _VENDORED_HEAP_DEEP and not os.path.isfile(path):
        raise FileNotFoundError(
            f"heap-deep.py not found at {path} — set GDBRPC_HEAP_DEEP_PATH "
            "in the environment or ~/.config/gdbrpc/.env"
        )
    mod = sys.modules.get(_HEAP_MODULE)
    if mod is None or not hasattr(mod, "HeapParser"):
        # (a cached module WITHOUT HeapParser is a poisoned leftover
        # from a failed exec — re-import instead of trusting it)
        spec = importlib.util.spec_from_file_location(_HEAP_MODULE, path)
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot create import spec for {path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[_HEAP_MODULE] = module
        try:
            spec.loader.exec_module(module)
        except Exception:
            # never leave a half-executed module in sys.modules —
            # it would poison every later call with AttributeError
            sys.modules.pop(_HEAP_MODULE, None)
            raise
        mod = module
    return mod.HeapParser()


def _hex(v):
    try:
        return f"0x{int(v):x}"
    except Exception:
        return str(v)


def _chain(ch):
    """Render a chain: ints, (ptr, reason) tuples and '...' markers."""
    out = []
    for c in ch:
        if c == "...":
            out.append("...")
        elif isinstance(c, tuple):
            out.append(f"{_hex(c[0])} ({c[1]})")
        else:
            out.append(_hex(c))
    return " -> ".join(out) if out else "(empty)"


def _chunksz(p, parser):
    try:
        return parser._chunk_size(p)
    except Exception:
        return None


def _try(fn, label):
    """Run fn; on gdb errors return (label unavailable) text instead."""
    try:
        return fn()
    except Exception as e:
        return {"text": f"({label} unavailable: {e})"}


class HeapTcacheView(View):
    name = "heap-tcache"
    description = "glibc tcache bins (size, count, chains, safe-linking)"
    triggers = ["stop"]
    options = []

    def provide(self, opts):
        return _try(self._provide, "tcache")

    def _provide(self):
        parser = _parser()
        lines = []
        for b in parser.get_tcache():
            lines.append(
                f"bin {b['idx']:2d} size 0x{b['size']:03x}"
                f" count {b['count']}: {_chain(b['chain'])}"
            )
        return {"text": "\n".join(lines) if lines else "(tcache empty)"}


class HeapFastbinsView(View):
    name = "heap-fastbins"
    description = "glibc fastbins per arena (safe-linking deobfuscated)"
    triggers = ["stop"]
    options = []

    def provide(self, opts):
        return _try(self._provide, "fastbins")

    def _provide(self):
        parser = _parser()
        lines = []
        for arena_addr, arena in parser.get_arenas():
            lines.append(f"arena {_hex(arena_addr)}:")
            fbs = parser.get_fastbins(arena_addr, arena)
            if not fbs:
                lines.append("  (fastbins empty)")
            for fb in fbs:
                lines.append(
                    f"  fastbin {fb['idx']} size 0x{fb['size']:x}: "
                    f"{_chain(fb['chain'])}"
                )
        return {"text": "\n".join(lines) if lines else "(no arenas)"}


class HeapBinsView(View):
    name = "heap-bins"
    description = "unsorted/small/large bins per arena (regular + large)"
    triggers = ["stop"]
    options = []

    def provide(self, opts):
        return _try(self._provide, "bins")

    def _provide(self):
        parser = _parser()
        lines = []
        for arena_addr, arena in parser.get_arenas():
            lines.append(f"arena {_hex(arena_addr)}:")
            unsorted = parser.get_unsorted(arena_addr, arena)
            lines.append(f"  unsorted: {_chain(unsorted)}")
            small = parser.get_smallbins(arena_addr, arena)
            if small:
                lines.append("  smallbins:")
                for sb in small:
                    lines.append(
                        f"    bin {sb['idx']} size 0x{sb['size']:x}: "
                        f"{_chain(sb['chain'])}"
                    )
            large = parser.get_largebins(arena_addr, arena)
            if large:
                lines.append("  largebins:")
                for lb in large:
                    lines.append(
                        f"    bin {lb['idx']}: {_chain(lb['chain'])}"
                    )
        return {"text": "\n".join(lines) if lines else "(no arenas)"}


class HeapArenaView(View):
    name = "heap-arena"
    description = "main_arena / arenas metadata (top, system_mem, ...)"
    triggers = ["stop"]
    options = []

    def provide(self, opts):
        return _try(self._provide, "arena")

    def _provide(self):
        parser = _parser()
        lines = []
        for arena_addr, arena in parser.get_arenas():
            meta = parser.get_arena_meta(arena_addr, arena)
            name = "main_arena" if int(arena_addr) == int(
                gdb.parse_and_eval("&main_arena")
            ) else f"arena@{_hex(arena_addr)}"
            lines.append(f"{name} @ {_hex(arena_addr)}")
            lines.append(f"  top            = {_hex(meta['top'])}")
            lines.append(
                f"  last_remainder = {_hex(meta['last_remainder'])}"
            )
            lines.append(
                f"  system_mem     = {int(meta['system_mem'])}"
                f" ({int(meta['system_mem']) // 1024} KiB)"
            )
            lines.append(
                f"  max_system_mem = {int(meta['max_system_mem'])}"
                f" ({int(meta['max_system_mem']) // 1024} KiB)"
            )
            top = meta["top"]
            size = _chunksz(top, parser)
            if size is not None:
                lines.append(f"  top chunk size = 0x{size:x}")
        return {"text": "\n".join(lines) if lines else "(no arenas)"}


class HeapPlugin(Plugin):
    name = "heap"
    description = "glibc heap views via the heap-plugin parser: " \
                  "tcache, fastbins, bins, arenas"
    version = "0.1.0"
    commands = []
    views = [
        HeapTcacheView,
        HeapFastbinsView,
        HeapBinsView,
        HeapArenaView,
    ]

    def setup(self):
        pass

    def teardown(self):
        pass