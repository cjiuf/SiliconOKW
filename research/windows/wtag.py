# -*- coding: utf-8 -*-
"""Step 3e: country-tag std::string table + its global."""
import pickle
import struct
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "calling"))
import mem  # noqa: E402

p = mem.Proc()
BASE = 0x19E05827C80
print("assumed table base %#x (GER at +0x20, ENG +0x40, SOV +0x60)" % BASE)

print("\n-- 0x60 bytes before --")
d = p.try_read(BASE - 0x60, 0x60)
for i in range(0, len(d), 16):
    print("  %#012x  %-47s |%s|" % (BASE - 0x60 + i, d[i:i + 16].hex(" "),
          "".join(chr(c) if 32 <= c < 127 else "." for c in d[i:i + 16])))

for off in range(-0x40, 0, 8):
    q, cp, sz, al = struct.unpack("<QIIQ", p.try_read(BASE + off, 0x18))
    if 0 < sz <= 0x2000 and sz <= cp and (q == BASE or abs(q - BASE) < 0x100):
        print("  header at %+#x: data=%#x cap=%d size=%d alloc=%#x" % (off, q, cp, sz, al))


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


print("\n-- tag table dump --")
for i in list(range(0, 26)) + [440, 441, 442, 443]:
    print("  [%3d] %r" % (i, msstr(BASE + i * 0x20)))

lo, hi = p.base, p.base + 0x38A3000
regs = [m for m in p.regions(writable=True) if m["start"] >= lo and m["end"] <= hi + 0x1000]
print("\n-- image references to the table --")
for m in regs:
    blob = p.try_read(m["start"], m["end"] - m["start"])
    if not blob:
        continue
    for val, label in ((BASE, "data"), (BASE - 0x18, "header-0x18")):
        needle = struct.pack("<Q", val)
        i = 0
        while True:
            i = blob.find(needle, i)
            if i < 0:
                break
            if i % 8 == 0:
                print("   %s -> %#x  (rva %#x)" % (label, m["start"] + i, m["start"] + i - p.base))
            i += 1
