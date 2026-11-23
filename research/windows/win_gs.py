# -*- coding: utf-8 -*-
"""Definitive Windows 1.19.5.0 gamestate chain (no hardcoded heap addresses)."""
import json
import pickle
import struct
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "calling"))
import mem  # noqa: E402

G_GAMESTATE = 0x33DD6B0          # image base + this -> CCurrentGameState*
GS_COUNTRIES = 0x310             # CPdxArray<CCountry*> {data,cap,size,alloc}
GS_TAG2INDEX = 0x340
GS_PLAYER_TAG = 0x518

p = mem.Proc()
gs = p.u64(p.base + G_GAMESTATE)
if not (0x10000 < gs < 0x7FFFFFFFFFFF):
    raise SystemExit("bad gamestate pointer %#x" % gs)
vptr = p.u64(gs)
print("base         : %#x" % p.base)
print("g_gameState  : %#x  (vptr %#x = base+%#x)" % (gs, vptr, vptr - p.base))
print("+0x08 vptr2  : %#x = base+%#x" % (p.u64(gs + 8), p.u64(gs + 8) - p.base))

data, cap, size, alloc = struct.unpack("<QIIQ", p.try_read(gs + GS_COUNTRIES, 0x18))
print("countries    : data=%#x cap=%d size=%d" % (data, cap, size))
td, tcap, tsize, _ = struct.unpack("<QIIQ", p.try_read(gs + GS_TAG2INDEX, 0x18))
print("tag2index    : data=%#x cap=%d size=%d" % (td, tcap, tsize))

tag = struct.unpack("<i", p.try_read(gs + GS_PLAYER_TAG, 4))[0]
print("player tag id: %d" % tag)

R = pickle.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "rtti_win.pkl"), "rb"))
CO_VT = p.base + R["classes"][".?AVCCountry@@"][0]


def msstr(addr):
    d = p.try_read(addr, 0x20)
    if not d or len(d) < 0x20:
        return None
    n, c = struct.unpack_from("<QQ", d, 0x10)
    if n > 0x4000 or c > 0x4000:
        return None
    if c <= 15:
        return d[:n].decode("utf-8", "replace")
    return (p.try_read(struct.unpack_from("<Q", d, 0)[0], n) or b"").decode("utf-8", "replace")


rows = []
for i in range(size):
    cp = p.u64(data + i * 8)
    ok = p.u64(cp) == CO_VT if cp else False
    rows.append({"i": i, "ptr": cp, "ok": ok, "tag": p.u32(cp + 8) if ok else None,
                 "name": msstr(cp + 0x10) if ok else None,
                 "adj": msstr(cp + 0x30) if ok else None})
bad = [r for r in rows if not r["ok"]]
print("entries      : %d  invalid=%d" % (len(rows), len(bad)))

print("\nindex  tag  name")
for r in rows[:24]:
    print("%5d  %3s  %s" % (r["i"], r["tag"], r["name"]))
me = rows[tag]
print("\nPLAYER -> [%d] tag=%d  %s / %s" % (me["i"], me["tag"], me["name"], me["adj"]))
json.dump(rows, open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "wgs_countries.json"), "w", encoding="utf-8"),
          ensure_ascii=False, indent=1)
print("wrote wgs_countries.json")
