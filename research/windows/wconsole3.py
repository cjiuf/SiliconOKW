# -*- coding: utf-8 -*-
"""Step 3i: verify CConsoleCmd fields (name / aliases / func) on 1.19.5.0."""
import re
import struct
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "calling"))
import mem  # noqa: E402

p = mem.Proc()
MGR = p.base + 0x33DD968
data, cap, size, alloc = struct.unpack("<QIIQ", p.try_read(MGR, 0x18))
TEXT_LO, TEXT_HI = p.base + 0x1000, p.base + 0x2784D48
NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,40}$")


def msstr(addr):
    b = p.try_read(addr, 0x20)
    if not b or len(b) < 0x20:
        return None
    n, c = struct.unpack_from("<QQ", b, 0x10)
    if n == 0 or n > 64 or c > 0x100 or c < n:
        return None
    raw = b[:n] if c <= 15 else (p.try_read(struct.unpack_from("<Q", b, 0)[0], n) or b"")
    if len(raw) != n:
        return None
    try:
        s = raw.decode("ascii")
    except UnicodeDecodeError:
        return None
    return s if NAME_RE.match(s) else None


rows = []
for i in range(size):
    e = data + i * 0x1C8
    b = p.try_read(e, 0x1C8)
    nm = msstr(e + 0x08)
    fn = struct.unpack_from("<Q", b, 0x68)[0]
    ac = struct.unpack_from("<I", b, 0x28)[0]
    al = []
    for k in range(min(ac, 3)):
        q = struct.unpack_from("<Q", b, 0x30 + k * 8)[0]
        if q:
            s = p.cstr(q, 32)
            if s:
                al.append(s)
    rows.append((i, nm, fn, al, TEXT_LO <= fn < TEXT_HI))

ok = sum(1 for r in rows if r[4])
print("commands: %d   func in .text: %d" % (len(rows), ok))
print("\n idx  func_rva    aliases        name")
for i, nm, fn, al, good in rows[:25]:
    print("%4d  %#010x  %-14s %s%s" % (i, fn - p.base, ",".join(al)[:14], nm, "" if good else "   <-- not .text"))

print("\n-- well-known commands --")
for want in ("tag", "annex", "pp", "manpower", "whitepeace", "instant_wargoal",
             "add_equipment_to_stockpile", "observe", "nuke", "research"):
    for i, nm, fn, al, good in rows:
        if nm == want:
            print("  %-26s [%3d] func rva %#010x aliases=%s" % (nm, i, fn - p.base, al))
            break
