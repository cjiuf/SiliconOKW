# -*- coding: utf-8 -*-
"""Find direct callers (E8 rel32) of one or more RVAs in hoi4.exe .text."""
import re
import struct
import sys

from wdump import D, SECS, dump

nm, tva, tvs, trp, trs = next(s for s in SECS if s[0] == ".text")
BLOB = D[trp:trp + trs]


def callers(target):
    out = []
    for m in re.finditer(rb"\xe8", BLOB):
        o = m.start()
        if o + 5 > len(BLOB):
            continue
        rel = struct.unpack_from("<i", BLOB, o + 1)[0]
        if tva + o + 5 + rel == target:
            out.append(tva + o)
    return out


def enclosing_start(site, back=0x800):
    """Very rough: find nearest preceding 'cc cc' padding or prologue before site."""
    o = tva
    idx = site - tva
    j = idx
    # walk back to a run of int3 (alignment padding) then the next byte is function start
    while j > max(0, idx - back):
        if BLOB[j] == 0xCC and BLOB[j + 1] == 0xCC:
            return tva + j + 1
        j -= 1
    return None


if __name__ == "__main__":
    for a in sys.argv[1:]:
        t = int(a, 0)
        cs = callers(t)
        print("=== callers of %#x : %d" % (t, len(cs)))
        for c in cs:
            print("   site %#x   fn~%#x" % (c, enclosing_start(c) or 0))
