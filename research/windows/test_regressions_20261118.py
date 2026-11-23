"""Offline regression tests for the 2026-11-18 tool fixes.

Run:  python research/windows/test_regressions_20261118.py

No game process is required: everything here reads the game's own files or
exercises pure-Python logic. The live behaviour of each fix was verified against
hoi4.exe (pid 50312, 1938.03.01) and is recorded in
`hoi4-1132德国1936新局_灭荷起手_崩溃与工具bug-20261117.md` §27.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CALLING = os.path.abspath(os.path.join(HERE, "..", "..", "calling"))
sys.path.insert(0, CALLING)

FAILS = []


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + ("   " + detail if detail else ""))
    if not cond:
        FAILS.append(name)


# ---------------------------------------------------------------- naval regions
def test_naval_regions():
    print("\n[naval regions]  hoi4_naval_regions / hoi4_naval_mission(region_names=)")
    import navalregions as NR
    data = NR.load()
    regions = data["regions"]
    check("catalogue parses every strategic region", len(regions) == 304,
          "got %d" % len(regions))
    check("North Sea is id 16", NR.lookup("North Sea") == 16,
          "got %r" % (NR.lookup("North Sea"),))
    check("lookup is case-insensitive", NR.lookup("north sea") == 16)
    check("lookup accepts a raw id", NR.lookup("16") == 16 and NR.lookup(16) == 16)
    check("lookup rejects garbage", NR.lookup("no such sea") is None)
    rec = regions[16]
    check("region carries a display name", rec["name"] == "North Sea", rec["name"])
    check("region carries naval terrain", rec["naval_terrain"] == "water_shallow_sea")
    check("region carries a province count", rec["provinces"] > 0)
    # catalogue() must never claim usability when the live table is not readable
    rows = NR.catalogue(None)
    check("usable_now is null without the live table",
          all(r["usable_now"] is None for r in rows))
    rows = NR.catalogue([16, 42])
    by = {r["id"]: r for r in rows}
    check("usable_now reflects the live id set",
          by[16]["usable_now"] is True and by[9]["usable_now"] is False)


# --------------------------------------------------------------- focus details
def test_focus_detail():
    print("\n[focus detail]  prerequisites parsed from script, not from memory")
    import focusplan
    f = focusplan.load()["focuses"]
    check("focus cache rebuilt with the new schema", focusplan.SCHEMA == 2)
    ans = f.get("GER_anschluss")
    check("GER_anschluss exists", bool(ans))
    check("anschluss prerequisite is an OR of the two opening focuses",
          ans["prereq_groups"] == [["GER_reorganize_the_wehrmacht",
                                    "GER_heed_von_neuraths_concerns"]],
          repr(ans["prereq_groups"]))
    reo = f.get("GER_reorganize_the_wehrmacht")
    check("reorganize/neurath are mutually exclusive",
          set(reo["mutex"]) == {"GER_heed_von_neuraths_concerns"}, repr(reo["mutex"]))
    wf = f.get("GER_war_with_france")
    check("war_with_france accepts around_maginot OR operation_weserubung",
          wf["prereq_groups"] == [["GER_around_maginot", "GER_operation_weserubung"]],
          repr(wf["prereq_groups"]))
    check("every parsed key is a non-empty string (no binary garbage)",
          all(isinstance(k, str) and k and k.isascii()
              for rec in f.values() for g in rec.get("prereq_groups") or [] for k in g))


# --------------------------------------------------------- research occupancy
def test_research_occupancy():
    print("\n[research]  a busy slot is never reported as empty")
    import game
    src = open(os.path.join(CALLING, "game.py"), encoding="utf-8").read()
    check("fill_all_research_slots uses slot_busy",
          'empty = [s["slot"] for s in slots if not s.get("slot_busy")]' in src)
    # 2026-11-19: the expression was re-wrapped over two lines and now also
    # treats slot_done as free; assert the invariant, not the exact spelling.
    check("start_research picks a free slot by slot_busy",
          'if not s.get("slot_busy")' in src)
    check("start_research accepts a cold name table as success",
          'got.get("researching") in (None, tech_name)' in src)
    check("_load_tech_db can block until the scan lands",
          "def _load_tech_db(self, wait_s=0.0)" in src)
    import turnloop
    tl = open(os.path.join(CALLING, "turnloop.py"), encoding="utf-8").read()
    check("snapshot counts research slots by slot_busy",
          'if not sl.get("slot_busy")' in tl)
    hk = open(os.path.join(CALLING, "housekeep.py"), encoding="utf-8").read()
    check("housekeep counts research slots by slot_busy",
          'if not s.get("slot_busy")' in hk)


# ------------------------------------------------------------ army groups (PE)
def test_army_group_constants():
    print("\n[army group]  the PE constructor is pinned to the decoded address")
    import armyops_win as AW
    check("ctor RVA", AW.FN_ARMY_GROUP_CTOR == 0x18C6840, hex(AW.FN_ARMY_GROUP_CTOR))
    check("vtable RVA", AW.ARMY_GROUP_VT == 0x2A846E0, hex(AW.ARMY_GROUP_VT))
    check("command size covers the ctor's last store (this+0x78)",
          AW.ARMY_GROUP_SIZE >= 0x80)
    # The ctor must really write that vtable: look for the RIP-relative LEA whose
    # target is ARMY_GROUP_VT inside the first 0x40 bytes of the function.
    exe = r"D:\SteamLibrary\steamapps\common\Hearts of Iron IV\hoi4.exe"
    ok, why = False, "exe not found"
    if os.path.exists(exe):
        import struct
        data = open(exe, "rb").read()
        pe = struct.unpack_from("<I", data, 0x3C)[0]
        nsec = struct.unpack_from("<H", data, pe + 6)[0]
        opt_size = struct.unpack_from("<H", data, pe + 20)[0]
        opt = pe + 24
        sec = opt + opt_size
        for i in range(nsec):
            off = sec + i * 40
            vsize, vaddr, rsize, raddr = struct.unpack_from("<IIII", data, off + 8)
            if vaddr <= AW.FN_ARMY_GROUP_CTOR < vaddr + max(vsize, rsize):
                foff = raddr + (AW.FN_ARMY_GROUP_CTOR - vaddr)
                body = data[foff:foff + 0x40]
                ok, why = False, "no LEA to the vtable in the prologue"
                for j in range(len(body) - 7):
                    if body[j] == 0x48 and body[j + 1] == 0x8D and body[j + 2] == 0x05:
                        disp = struct.unpack_from("<i", body, j + 3)[0]
                        target = AW.FN_ARMY_GROUP_CTOR + j + 7 + disp
                        if target == AW.ARMY_GROUP_VT:
                            ok, why = True, "LEA at +0x%x" % j
                            break
                break
    check("ctor prologue really loads the army-group vtable", ok, why)
    check("create_army_group refuses an empty list without touching the game",
          AW.create_army_group(None, [])[:2] == (False, "no armies to group"))


def test_create_template_cost():
    """CCreateDivisionTemplateCommand: the gate is the XP price, not a wall."""
    print("\n[create template]  CanExecute = 0x1C2DBF0, price helper = 0x1C2BD70")
    import config
    import struct

    data = open(config.binary_path(required=True), "rb").read()
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    nsec = struct.unpack_from("<H", data, pe + 6)[0]
    opt_size = struct.unpack_from("<H", data, pe + 20)[0]
    sec = pe + 24 + opt_size
    body = None
    for i in range(nsec):
        off = sec + i * 40
        vsize, vaddr, rsize, raddr = struct.unpack_from("<IIII", data, off + 8)
        if vaddr <= 0x1C2DBF0 < vaddr + max(vsize, rsize):
            foff = raddr + (0x1C2DBF0 - vaddr)
            body = data[foff:foff + 0xC0]
            break
    check("CanExecute body is mapped in .text", body is not None)

    found_call = False
    if body:
        for j in range(len(body) - 5):
            if body[j] == 0xE8:
                rel = struct.unpack_from("<i", body, j + 1)[0]
                if 0x1C2DBF0 + j + 5 + rel == 0x1C2BD70:
                    found_call = True
                    break
    check("CanExecute calls the price helper 0x1C2BD70", found_call)

    # the comparison itself: cmp qword [rsp+0x40], rax ; jl -> return false
    gate = b"\x48\x39\x44\x24\x40"
    check("CanExecute compares army XP against the price",
          body is not None and gate in body)

    import game as G
    check("Game exposes _pe_create_template_cost", hasattr(G.Game, "_pe_create_template_cost"))
    check("Game exposes _pe_army_xp", hasattr(G.Game, "_pe_army_xp"))
    check("PE_CREATE_CANEXE still points at the verified slot",
          G.Game.PE_CREATE_CANEXE == 0x1C2DBF0)

    src = open(os.path.join(CALLING, "game.py"), "r", encoding="utf-8").read()
    check("the cost gate is reported as blocked_by=army_xp", 'blocked_by"] = "army_xp"' in src)

    tsrc = open(os.path.join(CALLING, "templedit.py"), "r", encoding="utf-8").read()
    check("template edit compares compacted slot lists",
          "_compact(" in tsrc and "ok:false on a perfectly good edit" in tsrc)

    ssrc = open(os.path.join(os.path.dirname(HERE), "..", "mcp", "server.py"),
                "r", encoding="utf-8").read()
    check("duplicate_division_template exposes new_name",
          '"new_name"' in ssrc and 'a.get("new_name")' in ssrc)


def test_event_option_scope():
    """+0x38 of CSelectEventOptionCommand is a CEventScope, not a string."""
    print("\n[event option]  the command must be built through its factory")
    import events_win as EW
    check("factory pinned at 0xA590D0", EW.SEL_CTOR == 0xA590D0)
    check("CanExecute pinned at 0x15D66C0", EW.SEL_CANEXEC == 0x15D66C0)
    check("Execute pinned at 0x15D53E0", EW.SEL_EXECUTE == 0x15D53E0)

    src = open(os.path.join(CALLING, "events_win.py"), "r", encoding="utf-8").read()
    body = src.split("def make_select_option", 1)[1].split("\ndef ", 1)[0]
    check("builds through the factory", "p.base + SEL_CTOR" in body)
    check("verifies the returned vtable", "p.base + SEL_VT" in body)
    check("no longer writes a string over +0x38", "blob[0x38" not in body)
    for field in ("+ 0x28", "+ 0x2C", "+ 0x34", "+ 0xE8"):
        check("sets the scalar field %s" % field, field in body)

    check("select_option takes write=", "write=False" in src)
    check("write path verifies the pending event went away",
          "pending_after" in src and "did not go away" in src)

    # the CEventScope vtable the factory installs at +0x38
    import config
    import struct
    data = open(config.binary_path(required=True), "rb").read()
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    nsec = struct.unpack_from("<H", data, pe + 6)[0]
    opt_size = struct.unpack_from("<H", data, pe + 20)[0]
    sec = pe + 24 + opt_size
    body2 = None
    for i in range(nsec):
        off = sec + i * 40
        vsize, vaddr, rsize, raddr = struct.unpack_from("<IIII", data, off + 8)
        if vaddr <= 0x57BEE0 < vaddr + max(vsize, rsize):
            foff = raddr + (0x57BEE0 - vaddr)
            body2 = data[foff:foff + 0x20]
            break
    ok = False
    if body2:
        for j in range(len(body2) - 7):
            if body2[j] == 0x48 and body2[j + 1] == 0x8D and body2[j + 2] == 0x05:
                disp = struct.unpack_from("<i", body2, j + 3)[0]
                if 0x57BEE0 + j + 7 + disp == 0x2852168:
                    ok = True
                    break
    check("0x57BEE0 really loads the CEventScope vtable 0x2852168", ok)


def test_production_line_specs():
    print("\n[production lines]  hoi4_add_production_line / hoi4_remove_production_line")
    import struct
    import cmdwin
    import pedis

    add = cmdwin.spec_of("CAddProductionLineCommand")
    check("CAddProductionLineCommand vtable = 0x2A0FBC8 (was a stale 0x4413A48)",
          add["vt"] == 0x2A0FBC8, hex(add["vt"]))
    check("... ctor = 0x11CD270, the only `new(0x40)` + vtable store site",
          add["ctor"] == 0x11CD270, hex(add["ctor"]))
    check("... size = 0x40", add["size"] == 0x40, hex(add["size"]))
    check("... FOUR arguments (tag, variant, mio, stack bool)", add["args"] == 4,
          str(add["args"]))
    got = {f["offset"]: (f["size"], f["arg"]) for f in add["fields"]}
    check("... +0x28 is the i32 country tag from arg1", got.get(0x28) == (4, 1),
          repr(got.get(0x28)))
    check("... +0x2C is the variant handle from arg2", got.get(0x2C) == (8, 2),
          repr(got.get(0x2C)))
    check("... +0x34 is the MIO handle from arg3", got.get(0x34) == (8, 3),
          repr(got.get(0x34)))
    check("... +0x3C is the move-to-top bool from arg4 (contiguous index!)",
          got.get(0x3C) == (1, 4), repr(got.get(0x3C)))
    check("CAddProductionLineCommand is on the windows whitelist",
          cmdwin.win_safe("CAddProductionLineCommand"))
    # build_command walks range(1, n+1) and appends whatever each index holds, so
    # a hole in the mapping shifts every later argument by one and pads a 0 into
    # the stack bool.  That is how the first version put the flag in slot 6.
    idxs = sorted(cmdwin.ARGSPEC["CAddProductionLineCommand"].values())
    check("... ARGSPEC indices are contiguous 1..4 (no hole before the stack arg)",
          idxs == [1, 2, 3, 4], repr(idxs))

    rm = cmdwin.spec_of("CRemoveProductionLineCommand")
    check("CRemoveProductionLineCommand vtable = 0x2A10048", rm["vt"] == 0x2A10048,
          hex(rm["vt"]))
    check("... ctor = 0x11D0210", rm["ctor"] == 0x11D0210, hex(rm["ctor"]))
    check("... takes exactly ONE argument", rm["args"] == 1, str(rm["args"]))
    check("... and stores it at +0x28 (no country tag, no index)",
          [(f["offset"], f["size"]) for f in rm["fields"]] == [(0x28, 8)],
          repr([(hex(f["offset"]), f["size"]) for f in rm["fields"]]))
    check("CRemoveProductionLineCommand is on the windows whitelist",
          cmdwin.win_safe("CRemoveProductionLineCommand"))

    # the ctors really are the functions that stamp those vtables
    for ctor, vt, label in ((add["ctor"], add["vt"], "0x11CD270"),
                            (rm["ctor"], rm["vt"], "0x11D0210")):
        body = pedis.read(ctor, 0x80)
        ok = False
        for j in range(len(body) - 7):
            if body[j] == 0x48 and body[j + 1] == 0x8D \
                    and body[j + 2] in (0x05, 0x0D, 0x15, 0x1D):
                disp = struct.unpack_from("<i", body, j + 3)[0]
                if ctor + j + 7 + disp == vt:
                    ok = True
                    break
        check("%s really loads vtable %#x" % (label, vt), ok)

    # vtable slot +0x48 is CanExecute -- same slot as CSelectEventOptionCommand
    for vt, fn, label in ((add["vt"], 0x11EDB10, "CAddProductionLineCommand"),
                          (rm["vt"], 0x11F18B0, "CRemoveProductionLineCommand")):
        slot = pedis.u64(vt + 0x48) - pedis.IMAGE_BASE
        check("%s vtable+0x48 = CanExecute %#x" % (label, fn), slot == fn, hex(slot))

    # and CanExecute's very first test is `cmp dword [rcx+0x28], edi ; jg`
    body = pedis.read(0x11EDB10, 0x30)
    check("add CanExecute reads +0x28 as an i32 before anything else",
          b"\x39\x79\x28" in body, body[:12].hex())
    check('... and its failure string is "Invalid Target"',
          pedis.cstr(0x2A13FB8) == "Invalid Target", repr(pedis.cstr(0x2A13FB8)))


def main():
    test_naval_regions()
    test_focus_detail()
    test_research_occupancy()
    test_army_group_constants()
    test_create_template_cost()
    test_event_option_scope()
    test_production_line_specs()
    print("\n%s  (%d failure%s)" % ("ALL PASS" if not FAILS else "FAILURES: " + ", ".join(FAILS),
                                    len(FAILS), "" if len(FAILS) == 1 else "s"))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
