# -*- coding: utf-8 -*-
"""Step 3a: structural map of CCountry on 1.19.5.0 - every pointer and CPdxArray."""
import pickle
import struct
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "calling"))
import mem  # noqa: E402

R = pickle.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "rtti_win.pkl"), "rb"))
C = R["classes"]
VT = R["vt"]
p = mem.Proc()
CO_VT = p.base + C[".?AVCCountry@@"][0]
IMG_LO, IMG_HI = p.base, p.base + 0x38A3000

gs = p.u64(p.base + 0x33DD6B0)
data, cap, size, _ = struct.unpack("<QIIQ", p.try_read(gs + 0x310, 0x18))
ptrs = [p.u64(data + i * 8) for i in range(size)]

# player flag check across all countries
flags = [p.u32(q + 0x120) for q in ptrs]
print("CCountry+0x120 across 440 countries: %s" %
      {v: flags.count(v) for v in sorted(set(flags))})
print("countries with +0x120==1: %s" % [i for i, v in enumerate(flags) if v == 1])

ger = ptrs[1]
blob = p.try_read(ger, 0x1710)


def cls_of(v):
    if IMG_LO <= v < IMG_HI:
        return VT.get(v - IMG_LO)
    return None


print("\n--- pointer fields (qword -> object with known vtable) ---")
for o in range(0, len(blob) - 8, 8):
    q = struct.unpack_from("<Q", blob, o)[0]
    if not (0x10000 < q < 0x7FFFFFFFFFFF):
        continue
    v = p.try_read(q, 8)
    if not v or len(v) < 8:
        continue
    nm = cls_of(struct.unpack("<Q", v)[0])
    if nm:
        print("  +%#05x -> %#x  %s" % (o, q, nm))

print("\n--- CPdxArray fields (data/cap/size/alloc) ---")
for o in range(0, len(blob) - 0x18, 8):
    q, cp, sz, al = struct.unpack_from("<QIIQ", blob, o)
    if not (0x10000 < q < 0x7FFFFFFFFFFF) or not (0 <= sz <= cp <= 0x100000) or sz == 0 or cp == 0:
        continue
    v = p.try_read(q, 8)
    if not v or len(v) < 8:
        continue
    nm = cls_of(struct.unpack("<Q", v)[0])
    if nm and al in (p.base + 0x30FD000, 0):
        print("  +%#05x data=%#x cap=%d size=%d  element=%s" % (o, q, cp, sz, nm))
