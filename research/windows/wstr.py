# -*- coding: utf-8 -*-
"""Find ASCII/UTF-16 strings in hoi4.exe data and their code xrefs."""
import re
import struct
import sys

from wdump import D, SECS, rva2off

# data sections only
DATA = [(nm, va, vs, rp, rs) for nm, va, vs, rp, rs in SECS if nm != ".text"]


def find_strings(pat, wide=False):
    """Return list of (rva, text)."""
    res = []
    if wide:
        raw = pat.encode("utf-16-le")
    else:
        raw = pat.encode("ascii")
    for nm, va, vs, rp, rs in DATA:
        blob = D[rp:rp + rs]
        start = 0
        while True:
            i = blob.find(raw, start)
            if i < 0:
                break
            res.append((va + i, "%s @%#x" % (nm, va + i)))
            start = i + 1
    return res


def xrefs_to(targets):
    """Scan .text for rip-relative refs landing in targets set."""
    text = next(s for s in SECS if s[0] == ".text")
    nm, tva, tvs, trp, trs = text
    blob = D[trp:trp + trs]
    out = []
    # any instruction with modrm rip-relative: pattern-based, cover common opcodes
    for m in re.finditer(rb"[\x48\x4c][\x8d\x8b\x89\x03\x3b\x39\x2b\x85\x8b]", blob):
        o = m.start()
        if o + 7 > len(blob):
            continue
        modrm = blob[o + 2]
        if (modrm >> 6) != 0 or (modrm & 7) != 5:
            continue
        disp = struct.unpack_from("<i", blob, o + 3)[0]
        tgt = tva + o + 7 + disp
        for t in targets:
            if t[0] <= tgt < t[0] + t[1]:
                out.append((tva + o, tgt))
    return out


if __name__ == "__main__":
    for pat in sys.argv[1:]:
        wide = pat.startswith("w:")
        p = pat[2:] if wide else pat
        hits = find_strings(p, wide)
        print("%s -> %d hits" % (pat, len(hits)))
        for rva, note in hits[:20]:
            print("   %#x %s" % (rva, note))
