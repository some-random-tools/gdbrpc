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

HEAP_DEEP_PATH = os.path.expanduser(
    "~/scratch/heap-plugin/src/server/heap-deep.py"
)
_HEAP_MODULE = "gdbrpc_heap_deep"


def _parser():
    """Lazily import heap-deep.py and return a fresh HeapParser."""
    if _HEAP_MODULE not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            _HEAP_MODULE, HEAP_DEEP_PATH
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[_HEAP_MODULE] = module
        spec.loader.exec_module(module)
    parser = sys.modules[_HEAP_MODULE].HeapParser()
    return parser


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