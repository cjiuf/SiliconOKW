"""Offline regression tests for the 2026-11-19 code review & optimization pass.

Verifies all P0/P1 bug fixes without requiring a live hoi4.exe process,
and confirms that intentional design downgrades / hard constraints remain intact.
"""
import inspect
import os
import struct
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "calling"))
sys.path.insert(0, ROOT)

passed = 0
failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        print(f"  PASS  {name}")
        passed += 1
    else:
        print(f"  FAIL  {name}  {detail}")
        failed += 1


def test_msvc_stdstring_32byte_layout():
    import cmdwin
    import game as gm
    src_cw = inspect.getsource(cmdwin._game_string)
    src_gm = inspect.getsource(gm.MacroMixin._pe_duplicate_template)
    check("cmdwin._game_string writes 32-byte MSVC heap layout (<QQQQ)",
          'struct.pack("<QQQQ", data, 0, n, n)' in src_cw)
    check("game._pe_duplicate_template writes 32-byte MSVC heap layout (<QQQQ)",
          'struct.pack("<QQQQ", sb, 0, len(nb), len(nb))' in src_gm)


def test_mem_stdstring_and_scan_overlap():
    import mem

    class FakeProc(mem.Proc):
        def __init__(self, blob):
            self._blob = blob
            self._MSVC_STRING = True
            self.maps = [{"start": 0x10000, "end": 0x10000 + len(blob), "perms": "rw-p", "path": ""}]

        def try_read(self, addr, size):
            off = addr - 0x10000
            if off < 0 or off + size > len(self._blob):
                return None
            return self._blob[off:off + size]

    # Non-SSO empty string (n=0, cap=31) should return "", not None
    raw_empty = struct.pack("<QQQQ", 0x20000, 0, 0, 31)
    fp = FakeProc(raw_empty)
    check("mem.Proc.stdstring non-SSO empty string returns ''", fp.stdstring(0x10000) == "")

    # Corrupt n > cap should return None
    raw_bad = struct.pack("<QQQQ", 0x20000, 0, 40, 31)
    fp2 = FakeProc(raw_bad)
    check("mem.Proc.stdstring rejects n > cap", fp2.stdstring(0x10000) is None)

    # scan_value chunk-boundary overlap
    # Put needle across 8 MiB boundary (0x800000 - 4 .. 0x800000 + 4)
    buf = bytearray(0x800020)
    needle = b"\xDE\xAD\xBE\xEF\xCA\xFE\xBA\xBE"
    buf[0x7FFFFC:0x800004] = needle
    fp3 = FakeProc(bytes(buf))
    hits = fp3.scan_value(needle, align=4)
    check("mem.Proc.scan_value finds needle crossing 8 MiB chunk boundary",
          hits == [0x10000 + 0x7FFFFC], f"got={hits}")


def test_cmdwin_fake_vtable_size_cache():
    import cmdwin

    allocs = []

    class FakeP:
        base = 0x100000

        def read(self, addr, size):
            return b"\x11" * size

        def write(self, addr, data):
            pass

    class FakeG:
        p = FakeP()

    orig_alloc = cmdwin.alloc
    try:
        def fake_alloc(g, sz):
            ptr = 0x500000 + len(allocs) * 0x1000
            allocs.append((ptr, sz))
            return ptr

        cmdwin.alloc = fake_alloc
        g = FakeG()
        vt1 = cmdwin.fake_vtable(g, 0x2000, size=0xB0)
        vt2 = cmdwin.fake_vtable(g, 0x2000, size=0xB0)
        vt3 = cmdwin.fake_vtable(g, 0x2000, size=0x1D8)
        check("cmdwin.fake_vtable reuses cache for same size", vt1 == vt2)
        check("cmdwin.fake_vtable reallocates when larger size requested",
              vt3 != vt1 and len(allocs) == 2 and allocs[1][1] == 0x1D8)
    finally:
        cmdwin.alloc = orig_alloc


def test_events_win_can_execute_mask():
    import events_win

    class FakeP:
        def u64(self, addr):
            return 0x200000

    class FakeG:
        p = FakeP()

        def __init__(self, ret):
            self._ret = ret

        def call(self, slot, args, **kw):
            return self._ret

    check("events_win.can_execute masks high RAX bits (0x100 -> False)",
          events_win.can_execute(FakeG(0x100), 0x1000) is False)
    check("events_win.can_execute masks high RAX bits (0x101 -> True)",
          events_win.can_execute(FakeG(0x101), 0x1000) is True)


def test_peacewin_overflow_and_rbtree_guard():
    import peacewin
    src_exec = inspect.getsource(peacewin.execute)
    check("peacewin.execute guards cmd+0x30 write behind size >= 0x38",
          "if size >= 0x38:" in src_exec)
    src_it = inspect.getsource(peacewin.install_turn)
    check("peacewin.install_turn refuses overwriting non-empty RB-tree",
          "cur_sz > 0" in src_it)
    src_desc = inspect.getsource(peacewin.describe)
    check("peacewin.describe reads MSVC std::string size/cap at +0x20",
          "act + 0x20, 16" in src_desc)


def test_legality_not_unknown_government():
    import legality
    st = {"tag": "GER", "government": None, "focuses": set()}
    unk = []
    # NOT { has_government = communism } when government is unknown must NOT reject
    res = legality._eval("NOT = { has_government = communism }", st, unk)
    check("legality._eval NOT(unknown government) does not falsely reject",
          res is True and len(unk) == 1, f"res={res} unk={unk}")
    # Known matching government inside NOT must still reject
    st2 = {"tag": "SOV", "government": "communism", "focuses": set()}
    unk2 = []
    res2 = legality._eval("NOT = { has_government = communism }", st2, unk2)
    check("legality._eval NOT(known matching government) rejects",
          res2 is False and len(unk2) == 0, f"res={res2}")


def test_ragemicro_halt_and_leaving_safe():
    import ragemicro
    src_tick = inspect.getsource(ragemicro.RageMicro.tick)
    check("ragemicro._tick routes kind=='halt' to unitorders.halt",
          'if a["kind"] == "halt":' in src_tick and "unitorders.halt(self.g, d)" in src_tick)
    src_safe = inspect.getsource(ragemicro.RageMicro._leaving_is_safe)
    check("ragemicro._leaving_is_safe does not double-subtract committed_from",
          "len(staying) - leaving" not in src_safe and "if len(staying) > 0:" in src_safe)


def test_movemicro_halt_tuple_unpack():
    import movemicro
    src = inspect.getsource(movemicro._halt_dying_attacks)
    check("movemicro._halt_dying_attacks unpacks (ok, err) from unitorders.halt",
          "ok, err = unitorders.halt(" in src)


def test_turnloop_pp_none_and_until_date():
    import turnloop
    prev = {"political_power": None}
    cur = {"political_power": 150}
    res = turnloop.wake_reasons(prev, cur, pp_threshold=100)
    check("turnloop.diff handles prev['political_power'] is None",
          any("political power reached 150" in x for x in res), f"res={res}")
    src_w = inspect.getsource(turnloop.TurnLoop.wait)
    check("turnloop.wait preserves target_hours when start_hours is None",
          "target_hours if deadline_hours is None" in src_w)


def test_offsets_win_no_linux_bleed():
    import offsets_win as W
    check("offsets_win.FP_COMPLETED is explicitly None", W.FP_COMPLETED is None)
    check("offsets_win.GS_PLAYER_TAG_ALT is explicitly None", W.GS_PLAYER_TAG_ALT is None)
    check("offsets_win.CO_CAPITAL_STATE is explicitly None", W.CO_CAPITAL_STATE is None)


def test_hard_constraints_intact():
    import armyops_win
    check("Hard constraint: armyops_win.RAW_OFFENSIVE_CTOR_ALLOWED remains False",
          armyops_win.RAW_OFFENSIVE_CTOR_ALLOWED is False)
    with open(os.path.join(ROOT, "mcp", "server.py"), encoding="utf-8") as f:
        srv = f.read()
    check("Hard constraint: RageMicro uses independent Game() in mcp/server.py",
          "ragemicro.RageMicro(Game(), **kw)" in srv)
    check("Hard constraint: MAX_PAUSE_HOURS = 300 intact in mcp/server.py",
          "MAX_PAUSE_HOURS = 300" in srv)


def main():
    test_msvc_stdstring_32byte_layout()
    test_mem_stdstring_and_scan_overlap()
    test_cmdwin_fake_vtable_size_cache()
    test_events_win_can_execute_mask()
    test_peacewin_overflow_and_rbtree_guard()
    test_legality_not_unknown_government()
    test_ragemicro_halt_and_leaving_safe()
    test_movemicro_halt_tuple_unpack()
    test_turnloop_pp_none_and_until_date()
    test_offsets_win_no_linux_bleed()
    test_hard_constraints_intact()
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
