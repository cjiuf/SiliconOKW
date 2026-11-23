# -*- coding: utf-8 -*-
"""Step 3g: locate the console command table (CPdxArray<CConsoleCmd> singleton)."""
import pickle
import struct
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "calling"))
import mem  # noqa: E402

p = mem.Proc()
ALLOC = p.base + 0x30FD000
DLO = p.base + 0x30FD000
DHI = p.base + 0x30FD000 + 0x598754
print("scanning .data %#x..%#x" % (DLO, DHI))
blob = p.try_read(DLO, DHI - DLO)
print("read %d bytes" % len(blob))


def msstr(addr):
    b = p.try_read(addr, 0x20)
    if not b or len(b) < 0x20:
        return None
    n, c = struct.unpack_from("<QQ", b, 0x10)
    if n > 0x100 or c > 0x100:
        return None
    if c <= 15:
        return b[:n].decode("latin-1")
    return (p.try_read(struct.unpack_from("<Q", b, 0)[0], n) or b"").decode("latin-1")


cands = []
for o in range(0, len(blob) - 0x18, 8):
    q, cp, sz, al = struct.unpack_from("<QIIQ", blob, o)
    if al != ALLOC:
        continue
    if not (0x10000 < q < 0x7FFFFFFFFFFF):
        continue
    if not (50 <= sz <= 5000 and sz <= cp <= 20000):
        continue
    cands.append((o, q, cp, sz))
print("candidate arrays: %d" % len(cands))
for o, q, cp, sz in cands:
    first = msstr(q + 0x08) or msstr(q)
    if first is not None:
        first = first.encode("ascii", "backslashreplace").decode("ascii")
    print("  .data+%#08x -> data=%#x cap=%-5d size=%-5d first_str=%r" % (o, q, cp, sz, first))
