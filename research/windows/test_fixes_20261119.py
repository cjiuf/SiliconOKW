"""Offline regressions for the 2026-11-19 bug-fix round.

Runs with no game attached.  Each check is a plain assertion so it can be wired
into the usual pytest-less runner:

    python research/windows/test_fixes_20261119.py

Covers:
  * techdata parses the script `XOR = { ... }` block (the reason
    fill_research could start concentrated_industry AND dispersed_industry);
  * events_win.scope_copy_in rebases every pointer that aliases the source
    CEventScope onto the destination, and scope_copy_out restores the bytes;
  * the event success criterion is per-event-id, not "pending count dropped".
"""
import os
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "calling"))

FAILED = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (("  " + detail) if detail else ""))
    if not cond:
        FAILED.append(name)


# --------------------------------------------------------------- techdata XOR
def test_tech_xor():
    import techdata
    t = techdata.load(force=True)
    check("techdata: 552+ technologies parsed", len(t) > 500, "n=%d" % len(t))
    ci = (t.get("concentrated_industry") or {}).get("xor") or []
    di = (t.get("dispersed_industry") or {}).get("xor") or []
    check("concentrated_industry XOR dispersed_industry",
          "dispersed_industry" in ci, str(ci))
    check("dispersed_industry XOR concentrated_industry",
          "concentrated_industry" in di, str(di))
    mt = (t.get("improved_machine_tools") or {}).get("xor") or []
    check("improved_machine_tools has no XOR", mt == [], str(mt))


# ------------------------------------------------------------- scope copy in
class FakeP(object):
    def __init__(self):
        self.mem = {}
        self.base = 0x140000000
        self.u64v = None

    def _read(self, addr, n):
        return bytes(self.mem.get(addr + i, 0) for i in range(n))

    def read(self, addr, n):
        return self._read(addr, n)

    def write(self, addr, data):
        for i, b in enumerate(data):
            self.mem[addr + i] = b

    def u64(self, addr):
        return struct.unpack("<Q", self._read(addr, 8))[0]


class FakeG(object):
    def __init__(self, p):
        self.p = p


def test_scope_copy():
    import events_win as ew
    p = FakeP()
    src = 0x200000
    dst = 0x300000                      # command base
    sc = dst + ew.SEL_SCOPE_OFF         # the inline scope lives at cmd+0x38

    # default-ctor shape: vt, 0, 1, seed, three self pointers, then payload
    p.write(src, struct.pack("<Q", p.base + ew.SCOPE_VT))
    p.write(src + 8, struct.pack("<i", 0))
    p.write(src + 0xC, struct.pack("<i", 1))
    p.write(src + 0x10, struct.pack("<i", 0x5EA6BA9E))
    for off in (0x18, 0x20, 0x28):
        p.write(src + off, struct.pack("<Q", src))        # self pointers
    p.write(src + 0x30, struct.pack("<Q", src + 0x40))    # aliases own buffer
    p.write(src + 0x40, struct.pack("<Q", 0xDEADBEEF00))  # external heap ptr

    # destination starts as the factory's empty scope
    p.write(sc, struct.pack("<Q", p.base + ew.SCOPE_VT))
    p.write(sc + 0x18, struct.pack("<Q", sc))
    orig = p.read(sc, ew.SEL_SCOPE_SIZE)

    saved = ew.scope_copy_in(FakeG(p), dst, src)
    check("scope_copy_in returns the original bytes",
          isinstance(saved, (bytes, bytearray)) and len(saved) == ew.SEL_SCOPE_SIZE)

    got = [struct.unpack("<Q", p.read(sc + o, 8))[0] for o in (0x18, 0x20, 0x28)]
    check("scope copy: self pointers rebased onto dst",
          got == [sc, sc, sc], str([hex(x) for x in got]))
    alias = struct.unpack("<Q", p.read(sc + 0x30, 8))[0]
    check("scope copy: pointer into source body rebased",
          alias == sc + 0x40, hex(alias))
    heap = struct.unpack("<Q", p.read(sc + 0x40, 8))[0]
    check("scope copy: external heap pointer copied verbatim",
          heap == 0xDEADBEEF00, hex(heap))
    vt = struct.unpack("<Q", p.read(sc, 8))[0]
    check("scope copy: vtable preserved", vt == p.base + ew.SCOPE_VT, hex(vt))

    ew.scope_copy_out(FakeG(p), dst, saved)
    check("scope_copy_out restores the factory bytes",
          p.read(sc, ew.SEL_SCOPE_SIZE) == orig)


# ---------------------------------------------------------- event ok criterion
def test_event_ok_semantics():
    import events_win as ew
    src = open(ew.__file__, encoding="utf-8").read()
    check("event success judged by event id, not pending count",
          "event_still_pending" in src and "out[\"ok\"] = not still" in src)
    check("event command copies the record scope before post",
          "scope_copy_in(g, cmd, src)" in src)


def main():
    test_tech_xor()
    test_scope_copy()
    test_event_ok_semantics()
    print()
    if FAILED:
        print("FAILED: %d -> %s" % (len(FAILED), FAILED))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
