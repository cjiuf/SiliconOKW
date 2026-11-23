# -*- coding: utf-8 -*-
"""Rebuild CPdxHandle -> object resolution in Python (pure reads).

`CSelectEventOptionCommand::CanExecute/Execute` do:

    lea rcx, [cmd+0x2c]          ; 8-byte handle {u32 type, u32 id}
    call 0x2284870               ; -> object*
        eax = [rcx]              ; type
        if (type < 0x64)  m = g_table[type]        ; 0x34FFC00
        else              m = g_other
        return *(u64*)lookup(m, handle)

lookup() (0x2282880) is an open-addressing hash map:
    key   = (type << 32) | id
    h     = key * 0x49631a45
    h     = (h >> 32) ^ (int32)h
    b     = h & [m+0x14]
    entry = [m+8] + b*0x18   ; entries are 0x18 bytes: {value, type, id, ...}
    probe up to entry[+4] (a byte) entries forward
"""
import struct

TABLE = 0x34FFC00
N_TYPES = 0x64


def resolve(p, handle_addr):
    base = p.base
    typ = p.u32(handle_addr)
    ident = p.u32(handle_addr + 4)
    if typ >= N_TYPES:
        return {"ok": False, "why": "type out of range", "type": typ}
    m = p.u64(base + TABLE + typ * 8)
    if not m:
        return {"ok": False, "why": "no map for type", "type": typ}
    buckets = p.u64(m + 8)
    mask = p.i32(m + 0x14) if hasattr(p, "i32") else struct.unpack("<i", p.read(m + 0x14, 4))[0]
    key = ((typ << 32) | ident) & 0xFFFFFFFFFFFFFFFF
    h = (key * 0x49631A45) & 0xFFFFFFFFFFFFFFFF
    h = ((h >> 32) ^ (h & 0xFFFFFFFF)) & 0xFFFFFFFFFFFFFFFF
    b = h & (mask & 0xFFFFFFFF)
    ent = buckets + b * 0x18
    try:
        n = p.read(ent + 4, 1)[0]
    except Exception:
        return {"ok": False, "why": "bucket unreadable"}
    for k in range(1, n + 1):
        e = ent + (k - 1) * 0x18
        t = p.u32(e + 8)
        i2 = p.u32(e + 0xC)
        if t == typ and i2 == ident:
            return {"ok": True, "type": typ, "id": ident, "obj": p.u64(e), "entry": e}
        if k > 64:
            break
    return {"ok": False, "why": "not found", "type": typ, "id": ident, "bucket": b, "n": n}
