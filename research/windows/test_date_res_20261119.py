"""Offline regression tests for the 2026-11-19 date + resource fixes.

Run:  python research/windows/test_date_res_20261119.py

  * hoi4_date reported 1936.02.17.17 while the HUD said 1936.07.16 -- the
    Windows path never called the exact CDate scan, and the fallback sorted by
    distance to a five-month-stale game.log stamp.
  * hoi4_resources returned 0.0 for all seven resources -- CO_RESOURCES still
    pointed at the Linux offset 0x11C0 instead of 0x11F8.

No game process is required. Live verification is recorded in
research/WINDOWS_1.19.5.md §103/§104.
"""
import inspect
import os
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CALLING = os.path.abspath(os.path.join(HERE, "..", "..", "calling"))
sys.path.insert(0, CALLING)
sys.path.insert(0, HERE)

FAILS = []


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + ("   " + detail if detail else ""))
    if not cond:
        FAILS.append(name)


# ------------------------------------------------------------------ helpers
def api_class():
    import api
    for _n, obj in inspect.getmembers(api, inspect.isclass):
        if hasattr(obj, "_date_sig_ok") and hasattr(obj, "_find_date_exact"):
            return api, obj
    raise RuntimeError("no class in api.py owns _date_sig_ok")


class _Stub:
    """Minimal stand-in for the reader used by _date_sig_ok."""

    def __init__(self, blob):
        self.blob = blob
        outer = self

        class _P:
            def read(self, addr, n):
                return outer.blob[:n]

        self.p = _P()


def triple(raw, hour, frac=None):
    """The 16 bytes _date_sig_ok reads, starting 8 bytes before the counter."""
    if frac is None:
        frac = hour / 24.0
    return struct.pack("<fiii", frac, hour, raw, 0)


# -------------------------------------------------------------------- tests
def test_date_signature():
    print("\n[date]  CDate signature test")
    api, cls = api_class()

    raw = api.parts_to_date(1936, 7, 16, 9)
    check("parts round-trip", api.date_to_parts(raw) == (1936, 7, 16, 9),
          str(api.date_to_parts(raw)))
    check("counter is (year*365+doy)*24+hour",
          raw == (1936 * 365 + 196) * 24 + 9, str(raw))

    ok = cls._date_sig_ok(_Stub(triple(raw, 9)), 0x1000)
    check("accepts a real CDate triple (float hour/24, hour, counter, 0)", ok)

    bad = cls._date_sig_ok(_Stub(struct.pack("<fiii", 0.0, 535, raw, 0)), 0x1000)
    check("rejects the {value, 535} ladder entry", not bad)

    bad2 = cls._date_sig_ok(_Stub(triple(raw + 1, 9)), 0x1000)
    check("rejects counter/hour mismatch", not bad2)

    bad3 = cls._date_sig_ok(_Stub(struct.pack("<fiii", 0.375, 9, raw, 7)), 0x1000)
    check("rejects a non-zero +4 dword", not bad3)

    bad4 = cls._date_sig_ok(_Stub(triple(api.parts_to_date(1901, 1, 1, 0), 0)), 0x1000)
    check("rejects a date outside 1930..1948", not bad4)


def test_date_selection_order():
    print("\n[date]  Windows path uses the exact scan, midday filter first")
    api, cls = api_class()
    src_w = inspect.getsource(cls._win_date_addr)
    # 2026-11-19: _find_date_exact gained a `force` parameter and the midday
    # filter moved into _win_date_addr, so this test now checks the invariant
    # where it actually lives instead of pinning an exact call spelling.
    check("_win_date_addr calls _find_date_exact",
          "_find_date_exact(force=force)" in src_w or "_find_date_exact()" in src_w)
    check("_win_date_addr marks the pick as verified", "_date_verified = True" in src_w)

    i_mid = src_w.index("midday = [a for a in")
    i_sort = src_w.find("hits.sort(key=lambda a: -self.p.i32(a))", i_mid)
    check("the non-midnight filter runs BEFORE the descending sort", i_sort > i_mid,
          "midday@%d sort@%d" % (i_mid, i_sort))

    # The floor rule: the log can lag, never lead -> smallest candidate at/after it.
    src_f = inspect.getsource(cls._win_date_addr)
    check("fallback keeps copies >= log - 1 day", "lrv - 24" in src_f)
    check("fallback sorts ascending (smallest above the floor)", "hits.sort(key=lambda a: self.p.i32(a))" in src_f)


def test_resources_offsets():
    print("\n[resources]  CCountry+0x11F8 and the 7x16-byte amount array")
    import game as G
    check("CO_RESOURCES is 0x11F8", G.CO_RESOURCES == 0x11F8, hex(G.CO_RESOURCES))
    check("RES_ARRAY is 0x60", G.RES_ARRAY == 0x60, hex(G.RES_ARRAY))
    check("RES_STRIDE is 0x10", G.RES_STRIDE == 0x10, hex(G.RES_STRIDE))
    check("seven resources", len(G.RESOURCE_NAMES) == 7)
    check("order is oil..coal", G.RESOURCE_NAMES ==
          ["oil", "aluminium", "rubber", "tungsten", "steel", "chromium", "coal"])
    # Both consumers of the pointer must go through the constant.
    check("resources() reads through CO_RESOURCES",
          "CO_RESOURCES" in inspect.getsource(G.Game.resources))
    src_sub = inspect.getsource(G.Game.country_subsystems)
    check("country_subsystems does not hardcode 0x11C0",
          "0x11C0" not in src_sub and "0x11c0" not in src_sub)

    # 7 records fit between +0x60 and +0x60+7*0x10 = +0xD0, which is where the
    # array's allocator word sits in the live object -- i.e. the stride is right.
    check("7 records end exactly at 0xD0",
          G.RES_ARRAY + 7 * G.RES_STRIDE == 0xD0, hex(G.RES_ARRAY + 7 * G.RES_STRIDE))


if __name__ == "__main__":
    test_date_signature()
    test_date_selection_order()
    test_resources_offsets()
    print("\n%s" % ("ALL PASS" if not FAILS else "FAILURES: %s" % FAILS))
    sys.exit(1 if FAILS else 0)
