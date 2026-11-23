# -*- coding: utf-8 -*-
"""Find every RIP-relative reference to a given RVA in hoi4.exe .text."""
import re
import struct
import os
import sys

import config
EXE = config.binary_path()
D = open(EXE, "rb").read()
e = struct.unpack_from("<I", D, 0x3C)[0]
coff = e + 4
nsec = struct.unpack_from("<H", D, coff + 2)[0]
opt = coff + 20
opt_size = struct.unpack_from("<H", D, coff + 16)[0]
secs = []
for i in range(nsec):
    o = opt + opt_size + i * 40
    nm = D[o:o + 8].rstrip(b"\0").decode()
    vs, va, rs, rp = struct.unpack_from("<IIII", D, o + 8)
    secs.append((nm, va, vs, rp, rs))

text = next(s for s in secs if s[0] == ".text")
nm, tva, tvs, trp, trs = text
blob = D[trp:trp + trs]
print(".text rva %#x size %#x" % (tva, trs))

TARGETS = [int(x, 0) for x in sys.argv[1:]] or [0x33DD968]


def refs(target):
    """lea/mov r64,[rip+disp32] and mov [rip+disp32],r64 that land on target"""
    pat = re.compile(rb"[\x48-\x4f][\x8d\x8b\x89\x8b\x3b\x39][\x05\x0d\x15\x1d\x25\x2d\x35\x3d]")
    out = []
    for m in pat.finditer(blob):
        o = m.start()
        if o + 7 > len(blob):
            continue
        disp = struct.unpack_from("<i", blob, o + 3)[0]
        tgt = tva + o + 7 + disp
        if tgt in TARGETS:
            out.append((tva + o, tgt, blob[o:o + 7]))
    # 4C 8D / 4C 8B etc already covered by [\x48-\x4f]
    return out


for t in TARGETS:
    hits = refs(t)
    print("\ntarget %#x : %d refs" % (t, len(hits)))
    for rva, tgt, raw in hits[:40]:
        print("   %#010x  %s" % (rva, raw.hex(" ")))
