#!/usr/bin/env python3
"""Direct, in-process console commands for HOI4 1.19.4.1 (Linux) / 1.19.5.0 (Windows).

The keyboard path (`xconsole.py`) works, but it needs a live window manager, a
live console command table inside the running game and a working X server. This
module removes all three: it finds the engine's own command table in memory and
calls each command's handler directly.

Both builds share the entry layout (stride 0x1C8); only *where* the table lives
and *how* the argument array is built differ:

    Linux   table found by scanning writable memory for `manpower`
            args built with operator new, libstdc++ basic_string layout
    Windows table read from G_CONSOLE_MANAGER (an inline CPdxArray, 370/514)
            args built inside the injector's own scratch page, MSVC std::string

Layout of one table entry (stride 0x1C8, measured on 1.19.4.1 and 1.19.5.0):

    +0x08  std::string  command name (SSO for short names, hence the bytes live
                        at +0x18 and the object is at +0x08)
    +0x28  i32          alias count
    +0x30  char*[]      aliases
    +0x68  void*        handler  -- offset from the ELF base
    +0x78  void*        context  (0 for the commands checked so far)

Handler signature, recovered from `manpower` (0x1C4AC20):

    void handler(SResult* out, CPdxArray<std::string>* args)

    out+0x00  u8         status, 1 = success
    out+0x08  std::string  the message the console would print
    args      the arguments WITHOUT the command name
              (size == 1 is what `manpower 100000` produces)

The table is a CPdxArray of 370 entries in this build and contains `pp`,
`manpower`, `annex`, `spawn`, `tag`, `observe`, `no_wargoal`, ...

The table address is per-process, so it is cached by pid in `console_table.json`
next to this file. The first lookup in a new process scans writable memory for
the literal "manpower" entry; that costs roughly half a minute.
"""
from __future__ import annotations

import json
import os
import struct
import sys

import offsets_win as _OW
import session_state as _SS

STRIDE = _OW.CMD_STRIDE
CACHE = _SS.state_path(_SS.CONSOLE_TABLE_NAME)
ALLOC_OFF = _OW.G_DEFAULT_ALLOC

_WIN = sys.platform == "win32"
WIN_TEXT_LO, WIN_TEXT_SPAN = 0x1000, 0x3900000   # whole .text of hoi4.exe 1.19.5.0

_cache = {"pid": None, "base": 0, "count": 0}


def _clear_console_cache():
    _cache.update(pid=None, base=0, count=0)
    if "_index" in globals():
        _index.cache = None


_SS.register_cache("consolecmd._cache", _clear_console_cache)


def _text_range(g):
    if _WIN:
        return g.base + WIN_TEXT_LO, g.base + WIN_TEXT_SPAN
    return g.base + 0x5111C0, g.base + 0x367DAC0


def _looks_like_entry(g, addr):
    p = g.p
    lo, hi = _text_range(g)
    try:
        name = p.stdstring(addr + 0x08)
        fn = p.u64(addr + 0x68)
    except Exception:
        return None
    if not name or len(name) > 64 or any(ord(c) < 32 for c in name):
        return None
    if not (lo <= fn < hi):
        return None
    return name


def _scan_for_entry(g, needle="manpower"):
    """Find one table entry by its command name (SSO: name bytes sit at entry+0x18)."""
    p = g.p
    want = needle.encode()
    lo, hi = _text_range(g)
    hits = []
    for m in p.regions(writable=True):
        a, b = m["start"], m["end"]
        pos, ov = a, b""
        while pos < b:
            n = min(4 << 20, b - pos)
            try:
                data = ov + p.read(pos, n)
            except Exception:
                pos += n
                ov = b""
                continue
            st = 0
            while True:
                i = data.find(want, st)
                if i < 0:
                    break
                addr = pos - len(ov) + i
                for delta in (0x18, 0x08):
                    entry = addr - delta
                    nm = _looks_like_entry(g, entry)
                    if nm == needle:
                        hits.append(entry)
                        break
                st = i + 1
            ov = data[-32:]
            pos += n
        if hits:
            break
    return hits[0] if hits else 0


def _win_table(g):
    """(base, count) from G_CONSOLE_MANAGER, or (0, 0) if that global is not one.

    1.19.5.0 keeps the array **inline** in .data (not a pointer to a manager
    object), so this is an O(1) lookup:  {void* data; i32 cap; i32 size; alloc*}
    """
    p = g.p
    try:
        from offsets import G_CONSOLE_MANAGER, PDXARRAY_DATA, PDXARRAY_SIZE
        a = g.va(G_CONSOLE_MANAGER)
        raw = p.read(a, 0x18)
        base = struct.unpack_from("<Q", raw, PDXARRAY_DATA)[0]
        cap = struct.unpack_from("<i", raw, 0x08)[0]
        size = struct.unpack_from("<i", raw, PDXARRAY_SIZE)[0]
    except Exception:
        return 0, 0
    if not base or not (0 < size <= 4096) or cap < size:
        return 0, 0
    if not _looks_like_entry(g, base):
        return 0, 0
    # walk the array rather than trusting `size`: a table rebuilt mid-load can
    # announce 370 while only N entries are populated.
    count, cur = 0, base
    while count < size and _looks_like_entry(g, cur):
        count += 1
        cur += STRIDE
    return base, count


def table(g):
    """(base, count) of the console command table for this process."""
    global _cache
    p = g.p
    if _cache["pid"] == p.pid and _cache["base"]:
        return _cache["base"], _cache["count"]
    if _WIN:
        base, count = _win_table(g)
        if base:
            _cache = {"pid": p.pid, "base": base, "count": count}
            return base, count
    try:
        with open(CACHE) as _fh:
            d = json.load(_fh)
        if d.get("pid") == p.pid and d.get("base"):
            if _looks_like_entry(g, d["base"]):
                _cache = {"pid": p.pid, "base": int(d["base"]), "count": int(d["count"])}
                return _cache["base"], _cache["count"]
    except Exception:
        pass
    entry = _scan_for_entry(g)
    if not entry:
        raise RuntimeError("console command table not found in memory")
    start = entry
    while True:
        prev = start - STRIDE
        if not _looks_like_entry(g, prev):
            break
        start = prev
    count, cur = 0, start
    while _looks_like_entry(g, cur):
        count += 1
        cur += STRIDE
    _cache.update(pid=p.pid, base=start, count=count)
    try:
        _SS.ensure_parent(CACHE)
        with open(CACHE, "w") as _fh:
            json.dump(_cache, _fh)
    except Exception:
        pass
    return start, count


def _index(g):
    """{name_or_alias: entry_addr} for this process (cached by pid+table)."""
    base, count = table(g)
    key = (g.p.pid, base, count)
    if _index.cache and _index.cache[0] == key:
        return _index.cache[1]
    p = g.p
    idx = {}
    for i in range(count):
        e = base + i * STRIDE
        nm = _looks_like_entry(g, e)
        if nm:
            idx.setdefault(nm, e)
    for i in range(count):
        e = base + i * STRIDE
        try:
            na = p.i32(e + 0x28)
            if not (0 < na <= 8):
                continue
            for k in range(na):
                ap = p.u64(e + 0x30 + k * 8)
                if ap:
                    s = p.cstr(ap, 48)
                    if s:
                        idx.setdefault(s, e)
        except Exception:
            continue
    _index.cache = (key, idx)
    return idx


_index.cache = None


def find(g, name):
    return _index(g).get(name, 0)


def commands(g):
    """{name: {func, entry, aliases}} from the engine's own table."""
    base, count = table(g)
    out = {}
    p = g.p
    for i in range(count):
        e = base + i * STRIDE
        nm = _looks_like_entry(g, e)
        if not nm:
            continue
        aliases = []
        try:
            na = p.i32(e + 0x28)
            if 0 < na <= 8:
                for k in range(na):
                    ap = p.u64(e + 0x30 + k * 8)
                    if ap:
                        s = p.cstr(ap, 48)
                        if s:
                            aliases.append(s)
        except Exception:
            pass
        out[nm] = {"func": "img+%#x" % (p.u64(e + 0x68) - g.base),
                   "entry": hex(e), "aliases": aliases}
    return out


def _mkargs(g, args):
    """CPdxArray<std::string> with a valid libstdc++ basic_string layout.

    2026-11-12 (V3-01): the old code malloc'd a 0x20 scratch block per argument and
    wrote `16 + len(b)` bytes into it.  For any argument longer than 16 bytes the
    `b"\0" * (0x20 - 0x10 - len(b))` tail became empty and the write ran past the
    block -- `add_equipment 600 transport_plane_equipment_1` (27 chars) smashed the
    next malloc chunk header and the game died with SIGABRT.  Build each slot in
    place: SSO points at its OWN inline buffer, longer strings get a real buffer.
    """
    p = g.p
    n = len(args)
    data = g.malloc(max(1, n) * 0x20)
    for i, s in enumerate(args):
        b = s.encode("utf-8")
        slot = data + i * 0x20
        if len(b) <= 15:
            # SSO: _M_p -> slot+0x10 (inline buffer), length, 16 bytes incl. NUL
            p.write(slot, struct.pack("<QQ", slot + 0x10, len(b)) + b.ljust(16, b"\0"))
        else:
            buf = g.malloc(len(b) + 1)
            p.write(buf, b + b"\0")
            # _M_p, length, capacity, then 8 bytes of the union
            p.write(slot, struct.pack("<QQQ", buf, len(b), len(b)) + b"\0" * 8)
    hdr = g.malloc(0x18)
    p.write(hdr, struct.pack("<QiiQ", data, n, n, g.base + ALLOC_OFF))
    return hdr


def _run_win(g, entry, args):
    """Call one handler on Windows through the thread-hijack injector.

    The argument array is built inside the injector's own scratch page, so no
    operator new and no per-argument heap blocks: nothing to leak, nothing to
    smash.  `CString` here is MSVC std::string, not libstdc++.
    """
    p = g.p
    if getattr(g, "read_only", False):
        raise RuntimeError(
            "READ-ONLY mode is on: refusing to call a console handler.")
    from offsets import G_DEFAULT_ALLOC
    fn = p.u64(entry + 0x68)
    lo, hi = _text_range(g)
    if not (lo <= fn < hi):
        raise RuntimeError("handler %#x of %r is outside the image"
                           % (fn, p.stdstring(entry + 0x08)))
    own = g._inj is None
    if own:
        g.attach()
    try:
        return g._inj.run_console_cmd(fn, args, g.base + G_DEFAULT_ALLOC)
    finally:
        if own:
            g.detach()


def run(g, line):
    """Execute one console command line. Returns (ok, message)."""
    import api
    parts = line.split()
    if not parts:
        raise ValueError("empty command")
    name, args = parts[0], parts[1:]
    e = find(g, name)
    if not e:
        # A long-running process can rebuild its command table (the 3h41m instance
        # answered "Unknown command" to everything). Drop the cache and look again.
        global _cache
        _cache = {"pid": None, "base": 0, "count": 0}
        _index.cache = None
        try:
            os.remove(CACHE)
        except Exception:
            pass
        e = find(g, name)
        if not e:
            return False, "unknown command: %s" % name
    if _WIN:
        return _run_win(g, e, args)
    p = g.p
    fn = p.u64(e + 0x68) - g.base
    api.allow_call(fn)
    own = g._inj is None
    if own:
        g.attach()
    try:
        res = g.malloc(0x40)
        p.write(res, b"\0" * 0x40)
        arr = _mkargs(g, args)
        g.call(fn, [res, arr])
        return bool(p.u8(res)), (p.stdstring(res + 0x08) or "")
    finally:
        if own:
            g.detach()
