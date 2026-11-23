# -*- coding: utf-8 -*-
"""Step 3c: differential scan - which fields tick while the game runs?"""
import pickle
import struct
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "calling"))
import mem  # noqa: E402

p = mem.Proc()
gs = p.u64(p.base + 0x33DD6B0)
data, cap, size, _ = struct.unpack("<QIIQ", p.try_read(gs + 0x310, 0x18))
ger = p.u64(data + 1 * 8)
pol = p.u64(ger + 0xF90)
N_CO, N_POL = 0x1710, 0x800


def snap():
    return (p.try_read(ger, N_CO), p.try_read(pol, N_POL))


a_co, a_pol = snap()
print("sampled, waiting 45s ...")
time.sleep(45)
b_co, b_pol = snap()

for label, a, b in (("CCountry", a_co, b_co), ("CPoliticalStatus", a_pol, b_pol)):
    print("\n-- %s: increasing int64 fields --" % label)
    rows = []
    for o in range(0, len(a) - 8, 4):
        x = struct.unpack_from("<q", a, o)[0]
        y = struct.unpack_from("<q", b, o)[0]
        if 0 < y - x < 50_000_000:
            rows.append((o, x, y, y - x))
    rows.sort(key=lambda r: -r[3])
    for o, x, y, d in rows[:20]:
        print("   +%#06x  %d -> %d  (+%d)" % (o, x, y, d))
    print("   total %d" % len(rows))
    print("-- %s: increasing int32 fields --" % label)
    rows = []
    for o in range(0, len(a) - 4, 4):
        x = struct.unpack_from("<i", a, o)[0]
        y = struct.unpack_from("<i", b, o)[0]
        if 0 < y - x < 100_000:
            rows.append((o, x, y, y - x))
    rows.sort(key=lambda r: -r[3])
    for o, x, y, d in rows[:20]:
        print("   +%#06x  %d -> %d  (+%d)" % (o, x, y, d))
    print("   total %d" % len(rows))
