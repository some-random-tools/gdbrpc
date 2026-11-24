"""
heap-deep.py - Deep heap inspection for GDB with TUI window support.

Displays: tcache, fastbins, smallbins, largebins, unsorted bin, arenas.
Usage:
  (gdb) source heap-deep.py
  (gdb) heap-deep          # print to console
  (gdb) heap-deep-tui      # activate TUI window (GDB >= 10.1)
  (gdb) heap-deep-auto-dump # auto-write to file on every stop

If glibc debug symbols are not loaded, some structures may fail to parse.
Install them with your distro's libc debug package (e.g. libc6-dbg).
"""

import gdb
import re

# ---------------------------------------------------------------------------
# Colors
# ---------------------------------------------------------------------------
class Colorizer:
    _codes = {
        'red': '\033[31m', 'green': '\033[32m', 'yellow': '\033[33m',
        'blue': '\033[34m', 'magenta': '\033[35m', 'cyan': '\033[36m',
        'white': '\033[37m', 'bold': '\033[1m', 'dim': '\033[2m',
        'reset': '\033[0m'
    }

    def __init__(self, enabled=True):
        self.enabled = enabled

    def __call__(self, text, *colors):
        if not self.enabled:
            return str(text)
        prefix = ''.join(self._codes.get(c, '') for c in colors)
        return prefix + str(text) + self._codes['reset']


# ---------------------------------------------------------------------------
# Low-level heap parser (works when glibc debug symbols are present)
# ---------------------------------------------------------------------------
class HeapParser:
    def __init__(self):
        self.ptr_size = gdb.lookup_type('void').pointer().sizeof
        try:
            self.word_type = gdb.lookup_type('size_t')
        except Exception:
            self.word_type = gdb.lookup_type('unsigned long')

        self.nfastbins = self._const('NFASTBINS', 10)
        self.nbins = self._const('NBINS', 128)
        self.nsmallbins = self._const('NSMALLBINS', 64)
        self.tcache_max_bins = self._const('TCACHE_MAX_BINS', 64)

        self._chunk_type = None
        for name in ('struct malloc_chunk', 'malloc_chunk'):
            try:
                self._chunk_type = gdb.lookup_type(name)
                break
            except Exception:
                pass

    def _const(self, name, default):
        try:
            return int(gdb.parse_and_eval(name))
        except Exception:
            return default

    def _read_ptr(self, addr):
        """Read a pointer-sized value at addr and return as void*."""
        return (gdb.Value(int(addr))
                .cast(self.word_type.pointer())
                .dereference()
                .cast(gdb.lookup_type('void').pointer()))

    def _read_word(self, addr):
        return int(gdb.Value(int(addr))
                   .cast(self.word_type.pointer())
                   .dereference())

    def _reveal_ptr(self, pos, ptr):
        """Deobfuscate a safe-linking pointer (glibc 2.32+).
        pos = address where the pointer is stored.
        ptr = raw value read from that address."""
        if self.ptr_size == 8:
            mask = 0xFFFFFFFFFFFFFFFF
        else:
            mask = 0xFFFFFFFF
        return ((pos >> 12) ^ ptr) & mask

    def _chunk_fd(self, chunk_ptr):
        if self._chunk_type:
            c = chunk_ptr.cast(self._chunk_type.pointer()).dereference()
            return c['fd']
        return self._read_ptr(int(chunk_ptr) + 2 * self.ptr_size)

    def _chunk_bk(self, chunk_ptr):
        if self._chunk_type:
            c = chunk_ptr.cast(self._chunk_type.pointer()).dereference()
            return c['bk']
        return self._read_ptr(int(chunk_ptr) + 3 * self.ptr_size)

    def _chunk_size(self, chunk_ptr):
        if self._chunk_type:
            c = chunk_ptr.cast(self._chunk_type.pointer()).dereference()
            return int(c['mchunk_size']) & ~0x7
        return int(self._read_ptr(int(chunk_ptr) + self.ptr_size)) & ~0x7

    def _walk_fwd(self, start, limit=20):
        """Walk a forward-linked list (used for doubly-linked bins)."""
        chain = []
        seen = set()
        p = start
        while int(p) != 0:
            addr = int(p)
            if addr in seen:
                chain.append((p, 'loop'))
                break
            seen.add(addr)
            chain.append(p)
            if len(chain) >= limit:
                chain.append('...')
                break
            try:
                p = self._chunk_fd(p)
            except Exception:
                break
        return chain

    def _walk_fastbin(self, head, head_addr, limit=20):
        """Walk a fastbin chain with safe-linking deobfuscation."""
        chain = []
        seen = set()
        p = self._reveal_ptr(head_addr, int(head))
        while int(p) != 0:
            addr = int(p)
            if addr in seen:
                chain.append((p, 'loop'))
                break
            seen.add(addr)
            chain.append(p)
            if len(chain) >= limit:
                chain.append('...')
                break
            fd_addr = addr + 2 * self.ptr_size
            try:
                fd_obf = self._read_word(fd_addr)
                p = self._reveal_ptr(fd_addr, fd_obf)
            except Exception:
                break
        return chain

    def _arena_type(self):
        for name in ('struct malloc_state', 'malloc_state', 'mstate'):
            try:
                return gdb.lookup_type(name)
            except Exception:
                pass
        return None

    def get_arenas(self):
        arena_type = self._arena_type()
        if not arena_type:
            return []
        try:
            main_addr = int(gdb.parse_and_eval('&main_arena'))
        except Exception:
            return []

        arenas = []
        seen = set()
        addr = main_addr
        while addr not in seen:
            seen.add(addr)
            try:
                arena = gdb.Value(addr).cast(arena_type.pointer()).dereference()
                arenas.append((addr, arena))
            except Exception:
                break
            try:
                nxt = int(arena['next'])
            except Exception:
                break
            if nxt == main_addr or nxt == 0:
                break
            addr = nxt
        return arenas

    def get_arena_meta(self, arena_addr, arena):
        meta = {}
        for key in ('top', 'last_remainder', 'system_mem', 'max_system_mem'):
            try:
                meta[key] = arena[key]
            except Exception:
                meta[key] = 0
        return meta

    def get_fastbins(self, arena_addr, arena):
        out = []
        try:
            fb = arena['fastbinsY']
            fb_field = arena.type['fastbinsY']
            fb_base = arena_addr + fb_field.bitpos // 8
        except Exception:
            return out
        for i in range(self.nfastbins):
            head = fb[i]
            if int(head) == 0:
                continue
            head_addr = fb_base + i * self.ptr_size
            size = (i + 2) * (16 if self.ptr_size == 8 else 8)
            out.append({'idx': i, 'size': size,
                        'chain': self._walk_fastbin(head, head_addr)})
        return out

    def _bin_head_addr(self, arena_addr, arena, bin_idx):
        # bin_at(m, i) = &bins[(i-1)*2] - offsetof(fd)
        arena_type = arena.type
        bins_field = arena_type['bins']
        bins_base = arena_addr + bins_field.bitpos // 8
        fd_field_addr = bins_base + (bin_idx - 1) * 2 * self.ptr_size
        return fd_field_addr - 2 * self.ptr_size

    def _walk_bin(self, arena_addr, arena, bin_idx):
        bin_head = self._bin_head_addr(arena_addr, arena, bin_idx)
        arena_type = arena.type
        bins_field = arena_type['bins']
        bins_base = arena_addr + bins_field.bitpos // 8
        fd_ptr_addr = bins_base + (bin_idx - 1) * 2 * self.ptr_size
        start = self._read_ptr(fd_ptr_addr)

        chain = []
        seen = set()
        p = start
        while int(p) != 0 and int(p) != bin_head:
            addr = int(p)
            if addr in seen:
                chain.append((p, 'loop'))
                break
            seen.add(addr)
            chain.append(p)
            if len(chain) >= 10:
                chain.append('...')
                break
            try:
                p = self._chunk_fd(p)
            except Exception:
                break
        return chain

    def get_smallbins(self, arena_addr, arena):
        out = []
        for i in range(2, self.nsmallbins):
            chain = self._walk_bin(arena_addr, arena, i)
            if not chain:
                continue
            size = i * (16 if self.ptr_size == 8 else 8)
            out.append({'idx': i, 'size': size, 'chain': chain})
        return out

    def get_largebins(self, arena_addr, arena):
        out = []
        for i in range(self.nsmallbins, self.nbins):
            chain = self._walk_bin(arena_addr, arena, i)
            if not chain:
                continue
            out.append({'idx': i, 'chain': chain})
        return out

    def get_unsorted(self, arena_addr, arena):
        return self._walk_bin(arena_addr, arena, 1)

    def get_tcache(self):
        try:
            tcache_ptr = gdb.parse_and_eval('tcache')
        except Exception:
            return []
        if int(tcache_ptr) == 0:
            return []

        tcache_type = None
        for name in ('struct tcache_perthread_struct', 'tcache_perthread_struct'):
            try:
                tcache_type = gdb.lookup_type(name)
                break
            except Exception:
                pass
        if not tcache_type:
            return []

        tcache = tcache_ptr.cast(tcache_type.pointer()).dereference()
        try:
            counts = tcache['counts']
            entries = tcache['entries']
        except Exception:
            return []

        out = []
        for i in range(self.tcache_max_bins):
            count = int(counts[i])
            if count == 0:
                continue
            entry = entries[i]
            size = (i + 2) * (16 if self.ptr_size == 8 else 8)
            chain = []
            seen = set()
            p = entry
            while int(p) != 0:
                addr = int(p)
                if addr in seen:
                    chain.append((p, 'loop'))
                    break
                seen.add(addr)
                chain.append(p)
                if len(chain) > count + 2:
                    chain.append('...')
                    break
                try:
                    next_obf = self._read_word(addr)
                    p = self._reveal_ptr(addr, next_obf)
                except Exception:
                    break
            out.append({'idx': i, 'size': size, 'count': count, 'chain': chain})
        return out

# ---------------------------------------------------------------------------
# VENDORED from the heap-plugin repo (some-random-tools/heap-plugin,
# src/server/heap-deep.py) — parser part ONLY (Colorizer + HeapParser).
# The rest of that file (TUI windows, gdb command registration, banners)
# is NOT vendored: importing it from gdbrpc must not register commands.
# HeapParser walks glibc heap structures (tcache incl. safe-linking,
# fastbins, small/large/unsorted bins, arenas) on gdb's main thread.
# Requires glibc debug symbols (e.g. libc6-dbg).
# ---------------------------------------------------------------------------
