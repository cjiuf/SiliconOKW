# -*- coding: utf-8 -*-
"""MSVC RTTI scanner for hoi4.exe (1.19.5.0).

Recovers, straight from the PE image:
  * every RTTITypeDescriptor  (".?AV<Class>@@")
  * every RTTICompleteObjectLocator (signature==1, pSelf==own RVA)
  * every vtable that points at such a COL (vtable[-1] == image_base + COL RVA)

Output: stage/rtti_win.pkl  {"classes": {name: [vtable_rva,...]},
                             "vt": {vtable_rva: name}, "pe": {...}}
This is the Windows counterpart of calling/rtti.pkl on Linux.
"""
import array
import pickle
import struct
import os
import sys
import time

import config
EXE = config.binary_path()
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rtti_win.pkl")

t0 = time.time()
d = open(EXE, "rb").read()
print("file          : %s  %.1f MB" % (EXE, len(d) / 1e6))

# ------------------------------------------------------------------ PE
e_lfanew = struct.unpack_from("<I", d, 0x3C)[0]
assert d[e_lfanew:e_lfanew + 4] == b"PE\0\0"
coff = e_lfanew + 4
nsec = struct.unpack_from("<H", d, coff + 2)[0]
opt_size = struct.unpack_from("<H", d, coff + 16)[0]
opt = coff + 20
magic = struct.unpack_from("<H", d, opt)[0]
assert magic == 0x20B, "not PE32+"
image_base = struct.unpack_from("<Q", d, opt + 0x18)[0]
size_of_image = struct.unpack_from("<I", d, opt + 0x38)[0]
sec_tab = opt + opt_size
sections = []
for i in range(nsec):
    o = sec_tab + i * 40
    nm = d[o:o + 8].rstrip(b"\0").decode("latin1")
    vsize, va, rawsize, rawptr = struct.unpack_from("<IIII", d, o + 8)
    sections.append((nm, va, vsize, rawptr, rawsize))
print("image_base    : %#x   SizeOfImage %#x" % (image_base, size_of_image))
for s in sections:
    print("  %-9s va %#010x vsize %#09x raw %#010x+%#x" % s)


def off2rva(off):
    for nm, va, vs, rp, rs in sections:
        if rp <= off < rp + rs:
            return va + (off - rp)
    return None


def rva2off(rva):
    for nm, va, vs, rp, rs in sections:
        if va <= rva < va + max(vs, rs):
            return rp + (rva - va)
    return None


def u32(rva):
    o = rva2off(rva)
    return struct.unpack_from("<I", d, o)[0] if o is not None else None


def u64(rva):
    o = rva2off(rva)
    return struct.unpack_from("<Q", d, o)[0] if o is not None else None


# ------------------------------------------------- type descriptors
tds = {}          # td_rva -> name
bad = 0
i = 0
while True:
    i = d.find(b".?AV", i)
    if i < 0:
        break
    start = i
    j = d.find(b"\0", i)
    name = d[start:j]
    i = start + 4
    if len(name) < 6 or len(name) > 300 or not name.endswith(b"@@"):
        continue
    r = off2rva(start)
    if r is None or r < 0x10:
        continue
    td = r - 0x10
    # TypeDescriptor { void* pVFTable; void* spare; char name[]; }
    pvf = u64(td)
    if pvf is None or not (image_base <= pvf < image_base + size_of_image):
        bad += 1
        continue
    tds[td] = name.decode("latin1")
print("\ntype descriptors: %d (rejected %d)  %.1fs" % (len(tds), bad, time.time() - t0))

# --------------------------------------- complete object locators
cols = {}         # col_rva -> td_rva
n = len(d)
pos = 0
SIG = b"\x01\x00\x00\x00"
while True:
    pos = d.find(SIG, pos)
    if pos < 0:
        break
    p = pos
    pos = p + 1
    if p % 4:
        continue
    r = off2rva(p)
    if r is None:
        continue
    td = u32(r + 0x0C)
    if td is None or td not in tds:
        continue
    self_rva = u32(r + 0x14)
    if self_rva != r:
        continue
    cols[r] = td
print("object locators : %d   %.1fs" % (len(cols), time.time() - t0))

# ------------------------------------------------------------ vtables
target = {image_base + c: c for c in cols}
hits = {}
for nm, va, vs, rp, rs in sections:
    if nm not in (".rdata", ".data"):
        continue
    blob = d[rp:rp + rs]
    blob += b"\0" * (-len(blob) % 8)
    a = array.array("Q")
    a.frombytes(blob)
    for k, v in enumerate(a):
        c = target.get(v)
        if c is not None:
            hits[va + k * 8 + 8] = c
print("vtables         : %d   %.1fs" % (len(hits), time.time() - t0))

classes = {}
for vt, c in hits.items():
    classes.setdefault(tds[cols[c]], []).append(vt)
print("classes w/ vtable: %d" % len(classes))

INTEREST = ["CCountry", "CGameState", "CState", "CConsoleCmdManager", "CConsoleCmd",
            "CCountryTag", "CDate", "CWarGoal", "CProvince", "CPop", "CDivision",
            "CArmy", "CFaction", "CDiplomacy", "CCountryPolitics", "CCountryEconomy"]
print("\n-- interesting classes --")
for k in INTEREST:
    for nm, vts in sorted(classes.items()):
        base = nm[4:].rstrip("@")
        if base == k:
            print("  %-22s %s" % (base, " ".join("%#x" % v for v in sorted(vts))))

pickle.dump({"classes": classes, "vt": {v: tds[cols[c]] for v, c in hits.items()},
             "pe": {"image_base": image_base, "size_of_image": size_of_image,
                    "sections": sections}},
            open(OUT, "wb"), protocol=4)
print("\nwrote", OUT, "in %.1fs" % (time.time() - t0))
