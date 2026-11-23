# -*- coding: utf-8 -*-
"""Step 3f: CState owner field + percentage fields in CCountry."""
import pickle
import struct
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "calling"))
import mem  # noqa: E402

p = mem.Proc()
gs = p.u64(p.base + 0x33DD6B0)
data, cap, size, _ = struct.unpack("<QIIQ", p.try_read(gs + 0x310, 0x18))
countries = [p.u64(data + i * 8) for i in range(size)]
CO = 0x460


def states_of(i):
    c = countries[i]
    d, cp, sz, al = struct.unpack("<QIIQ", p.try_read(c + CO, 0x18))
    return [p.u64(d + k * 8) for k in range(sz)]


G, E, S = states_of(1), states_of(2), states_of(3)
print("states owned: GER %d  ENG %d  SOV %d" % (len(G), len(E), len(S)))

print("\n-- u32/u16/u8 field == country index for all three countries --")
for width, fmt in ((4, "<I"), (2, "<H"), (1, "<B")):
    for o in range(0, 0x600 - width):
        try:
            if all(struct.unpack_from(fmt, p.try_read(s + o, width))[0] == 1 for s in G) and \
               all(struct.unpack_from(fmt, p.try_read(s + o, width))[0] == 2 for s in E) and \
               all(struct.unpack_from(fmt, p.try_read(s + o, width))[0] == 3 for s in S):
                print("   width=%d offset +%#05x" % (width, o))
        except Exception:
            pass

print("\n-- CState[GER[0]] head --")
st = G[0]
d = p.try_read(st, 0x140)
for i in range(0, 0x140, 16):
    row = d[i:i + 16]
    print("  +%04x  %-47s |%s|" % (i, row.hex(" "),
          "".join(chr(c) if 32 <= c < 127 else "." for c in row)))

print("\n-- CCountry int64 fields in [0,100000] that vary --")
blobs = [p.try_read(countries[i], 0x1710) for i in range(26)]
for o in range(0, 0x1700, 8):
    vals = [struct.unpack_from("<q", b, o)[0] for b in blobs]
    if not all(0 <= v <= 100000 for v in vals):
        continue
    if len(set(vals)) < 6:
        continue
    print("  +%#05x  %s" % (o, "  ".join("%6.3f" % (v / 1000.0) for v in vals[:8])))
