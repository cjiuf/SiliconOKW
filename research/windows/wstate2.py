# -*- coding: utf-8 -*-
"""Step 3b: proper CPdxArray enumeration (alloc-tagged) + CState back-links."""
import pickle
import struct
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "calling"))
import mem  # noqa: E402

R = pickle.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "rtti_win.pkl"), "rb"))
C, VT = R["classes"], R["vt"]
p = mem.Proc()
ALLOC = p.base + 0x30FD000
IMG_LO, IMG_HI = p.base, p.base + 0x38A3000
gs = p.u64(p.base + 0x33DD6B0)
data, cap, size, _ = struct.unpack("<QIIQ", p.try_read(gs + 0x310, 0x18))
countries = [p.u64(data + i * 8) for i in range(size)]
ST_VT = p.base + C[".?AVCState@@"][0]
CO_VT = p.base + C[".?AVCCountry@@"][0]


def cls(v):
    return VT.get(v - IMG_LO) if IMG_LO <= v < IMG_HI else None


def safe_u64(a):
    b = p.try_read(a, 8)
    return struct.unpack("<Q", b)[0] if b and len(b) == 8 else 0


def arrays_in(blob, label):
    print("\n-- %s: CPdxArray (alloc-tagged) --" % label)
    for o in range(0, len(blob) - 0x18, 8):
        q, cp, sz, al = struct.unpack_from("<QIIQ", blob, o)
        if al != ALLOC or not (0x10000 < q < 0x7FFFFFFFFFFF):
            continue
        if not (0 < sz <= cp <= 0x100000):
            continue
        e0 = safe_u64(q)
        nm = cls(safe_u64(e0)) if 0x10000 < e0 < 0x7FFFFFFFFFFF else None
        print("  +%#06x data=%#x cap=%-6d size=%-6d -> %s" % (o, q, cp, sz, nm))


arrays_in(p.try_read(gs, 0x20000), "CGameState")
ger = countries[1]
arrays_in(p.try_read(ger, 0x1710), "CCountry[1] GER")

# a CState: who owns it?
st = 0x19EAADE6C80
print("\n-- CState %#x head --" % st)
d = p.try_read(st, 0x100)
for i in range(0, 0x80, 16):
    row = d[i:i + 16]
    print("  +%04x  %-47s |%s|" % (i, row.hex(" "),
          "".join(chr(c) if 32 <= c < 127 else "." for c in row)))
owner = p.u64(st + 0x18)
print("+0x18 = %#x  is a CCountry? %s" % (owner, owner in countries))
for o in range(0, 0x100, 8):
    q = p.u64(st + o)
    if q in countries:
        print("   +%#04x -> CCountry[%d]" % (o, countries.index(q)))
