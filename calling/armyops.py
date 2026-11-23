"""Armies, army groups and commanders, through the player command channel.

What this corresponds to in the UI:
  * Select divisions -> the "+" button     => COrderGroupCommand  (new ARMY)
  * Select armies    -> the "+" button     => CArmyGroupCommand  (new ARMY GROUP)
  * Drag a general onto the portrait       => CSetArmyLeaderCommand

Important: a command field always takes THAT OBJECT'S OWN handle.
  CArmy         +0x18  -> {51,  id}
  COrdersGroup  +0x08  -> {53,  id}
  CTheatre      +0x08  -> {67,  id}
  CCharacter    +0x08  -> {73,  id}
  CArmyLeader   +0x08  -> {4713,id}     <-- assigning a general wants THIS, not CCharacter
"""
import struct

# CPdxArray element allocator. 0x43591B8 (the old value) is a VTABLE, not an
# allocator instance: its first qword is 0xa773a0, so ~CPdxArray called
# [0xa773a0+0x18] and the game aborted with "free(): invalid size" x3 followed by
# a stack-smashing SIGSEGV. 0x4505128 is the static CPdxNewDeleteAllocator object
# (its first qword is 0x4342550, 22CPdxNewDeleteAllocator) and is exactly what
# COrderGroupCommand's own constructor writes into cmd+0x38.
#
# Work order 1107 proposed 0x43449B0 / 0x4358CD0 / 0x43445B8 for CArmy*/handle/int.
# All three are VTABLES in rtti.pkl (CPdxSmallSingleBufferAllocator<CArmy*,int>
# has 0x43449B0 as its vtable address), so writing them into cmd+0x10 is the exact
# `free(): invalid pointer` bug above. One allocator for all three arrays is safe:
# CPdxNewDeleteAllocator is type-erased (allocate(size, align)), which is why the
# same object already serves COrdersGroup, CArmy* and int arrays in every run.
ALLOC = 0x4505128

# --- COrderGroupCommand (new army) ----------------------------------------
OG_CTOR   = 0x23151C0   # 1.19.4.1 (this, CPdxArray<CArmy*>*, CTheatre*, CArmyGroup*|0)
OG_CANEXE = None        # 1.19.4.1: kullanilmiyor, RUN_CMD icinde yapiliyor
OG_SIZE   = 0x58
# --- CArmyGroupCommand (new army group) -----------------------------------
AG_CTOR   = 0x23167D0   # 1.19.4.1
AG_CANEXE = None
AG_SIZE   = 0x80
# --- CSetArmyLeaderCommand ------------------------------------------------
SL_CTOR   = 0x2332940   # 1.19.4.1
SL_CANEXE = None
RUN_CMD   = 0x20AE0A0   # bool Run(cmd, std::string* err) -> CanExecute + Post
SL_SIZE   = 0x38

FN_RESOLVE = 0x2d82f10  # handle* -> object*

# COrdersGroup fields
OG_HANDLE      = 0x08
OG_THEATRE     = 0x40
OG_DIVISIONS   = 0x50   # CPdxArray<CArmy*>
OG_LEADER      = 0x88   # CArmyLeader*
OG_ORDERS      = 0x98   # CPdxArray<COrderInstance*>
OG_NAME        = 0x160  # std::string
OG_IS_GROUP    = 0x39   # 0 = army, non-zero = army group
OG_PARENT      = 0x1b8  # parent army group (COrdersGroup*, 0 if unattached)
OG_MEMBERS     = 0x228  # armies inside the group: CPdxArray<COrdersGroup*>
OG_NOGROUP_CHK = 0x1b0  # the UI's check for whether a group can be created
CARMY_OG       = 0xC0   # CArmy -> COrdersGroup*


# ------------------------------------------------------------------ helpers
def _mkarray(g, ptrs):
    p = g.p
    data = g.malloc(max(1, len(ptrs)) * 8)
    p.write(data, b"".join(struct.pack("<Q", x) for x in ptrs))
    hdr = g.malloc(0x18)
    p.write(hdr, struct.pack("<QiiQ", data, len(ptrs), len(ptrs), p.base + ALLOC))
    return hdr


def _mkstring(g):
    s = g.malloc(0x20)
    g.p.write(s, struct.pack("<QQ", s + 0x10, 0) + b"\0" * 16)
    return s


def _readstring(g, s):
    ln = g.p.u64(s + 8)
    return g.p.read(g.p.u64(s), ln).decode("utf8", "replace") if ln else ""


def resolve_handle(g, type_id, obj_id):
    """Handle -> object. 1.19.2'nin resolver adresi (0x2d82f10) bu build'de gecersiz:
    cagirmak oyunu SIGSEGV ile dusuruyor, bu yuzden devre disi."""
    raise RuntimeError("handle resolver 1.19.4.1 icin henuz bulunamadi")


def check_field_handle(g, cmd, off, expect_ptr, what):
    """Does the handle in the command resolve to the expected object?

    Sadece bir teshis/kontrol: resolver yoksa sessizce gecilir, komut yine gonderilir.
    """
    t, i = struct.unpack("<ii", g.p.read(cmd + off, 8))
    try:
        got = resolve_handle(g, t, i)
    except Exception:
        return t, i
    if got != expect_ptr:
        raise RuntimeError(
            "%s: +0x%x handle {%d,%d} -> 0x%x, beklenen 0x%x" % (what, off, t, i, got, expect_ptr))
    return t, i


# ------------------------------------------------------------------ reads
def orders_groups(g, country=None):
    """The country's armies (COrdersGroup), collected through its divisions."""
    from game import _arr, item_name
    p = g.p
    co = country.ptr if country is not None else g.player().ptr
    seen, out = set(), []
    for d in _arr(p, co, 0x290):
        og = p.u64(d + CARMY_OG)
        if og and og not in seen:
            seen.add(og)
            out.append(og)
    return out


def og_info(g, og):
    p = g.p
    ldr = p.u64(og + OG_LEADER)
    name = ""
    try:
        name = p.stdstring(og + OG_NAME)
    except Exception:
        pass
    return {
        "ptr": hex(og),
        "handle": list(struct.unpack("<ii", p.read(og + OG_HANDLE, 8))),
        "name": name,
        "is_army_group": bool(p.u8(og + OG_IS_GROUP)),
        "divisions": p.i32(og + OG_DIVISIONS + 0xC),
        "leader_ptr": hex(ldr) if ldr else None,
        "leader": (p.cstr(p.u64(ldr + 0x40)) if ldr else None),
        "parent_group": hex(p.u64(og + OG_PARENT)) if p.u64(og + OG_PARENT) else None,
        "member_armies": (p.i32(og + OG_MEMBERS + 0xC) if p.u8(og + OG_IS_GROUP) else None),
        "orders": p.i32(og + OG_ORDERS + 0xC),
    }


# ------------------------------------------------------------------ actions
def create_army(g, divisions, theatre=None):
    """Collect the given divisions (CArmy*) into a new army.

    Windows takes a different route: the PE build's COrderGroupCommand ctor wants
    a parent COrdersGroup, not a CTheatre, and the command's own Execute body is
    0x00C417A0 (theatre + handle array -> COrdersGroup*). Call that directly --
    see armyops_win for the disassembly it came from.
    """
    import sys
    if sys.platform == "win32":
        import armyops_win
        return armyops_win.create_army(g, divisions)
    from game import _arr
    if theatre is None:
        theatre = _arr(g.p, g.player().ptr, 0x168)[0]
    own = g._inj is None
    if own: g.attach()
    try:
        arr = _mkarray(g, list(divisions))
        cmd = g.malloc(OG_SIZE); g.p.write(cmd, b"\0" * OG_SIZE)
        g.call(OG_CTOR, [cmd, arr, theatre, 0])
        check_field_handle(g, cmd, 0x40, theatre, "create_army/theatre")
        s = _mkstring(g)
        ok = g.call(RUN_CMD, [cmd, s]) & 0xFF
        if not ok:
            return False, _readstring(g, s) or "command rejected"
        return True, ""
    finally:
        if own: g.detach()


def create_army_group(g, armies, theatre=None):
    """Collect the given armies (COrdersGroup*) into a new army group."""
    import sys as _sys
    if _sys.platform == "win32":
        # 2026-11-16 crash post-mortem: AG_CTOR (0x23167D0) is a LINUX offset.  On the
        # PE build g.call() still adds the image base, so this "attempt" executed
        # whatever code lives at base+0x23167D0 with a fabricated command object.
        # It returned cleanly ("array was not filled (0/5)") and the heap blew up
        # ~40 minutes later (EXCEPTION_ACCESS_VIOLATION in RtlAllocateHeap).
        # There is no verified Windows army-group ctor yet; refuse instead of guessing.
        return False, ("create_army_group is not implemented for the Windows build "
                       "(no verified COrderGroupCommand ctor; the Linux offset corrupts "
                       "the heap). Assign commanders to the individual armies instead.")
    from game import _arr
    p = g.p
    if theatre is None:
        theatre = _arr(p, g.player().ptr, 0x168)[0]
    for og in armies:
        if p.u8(og + OG_IS_GROUP):
            return False, "0x%x is already an army group" % og
        if p.u64(og + OG_PARENT):
            return False, "0x%x is already inside an army group" % og
        if p.u64(og + OG_NOGROUP_CHK):
            return False, "0x%x gruplanamaz" % og
    own = g._inj is None
    if own: g.attach()
    try:
        arr = _mkarray(g, list(armies))
        cmd = g.malloc(AG_SIZE); g.p.write(cmd, b"\0" * AG_SIZE)
        g.call(AG_CTOR, [cmd, arr, theatre])
        check_field_handle(g, cmd, 0x24, theatre, "create_army_group/theatre")
        n = p.i32(cmd + 0x3C)
        if n != len(armies):
            return False, "array was not filled (%d/%d)" % (n, len(armies))
        s = _mkstring(g)
        ok = g.call(RUN_CMD, [cmd, s]) & 0xFF
        if not ok:
            return False, _readstring(g, s) or "command rejected"
        return True, ""
    finally:
        if own: g.detach()


def set_army_leader(g, orders_group, leader, flag=0):
    """Assign a commander to an army or army group.

    leader: CArmyLeader* (CCharacter+0xC0) or CCharacter*, converted automatically.
            Passing 0 removes the commander.
    """
    p = g.p
    own = g._inj is None
    if own: g.attach()
    try:
        lp = leader
        if lp:
            t = p.i32(lp + 8)
            if t == 73:                 # given a CCharacter, move to its CArmyLeader
                lp = p.u64(lp + 0xA8)   # 1.19.5; was 0xC0 in 1.19.4.1
                if not lp:
                    return False, "this character has no CArmyLeader"
            if p.i32(lp + 8) != 4713:
                return False, "0x%x is not a CArmyLeader (handle type %d)" % (lp, p.i32(lp + 8))
        cmd = g.malloc(SL_SIZE); p.write(cmd, b"\0" * SL_SIZE)
        g.call(SL_CTOR, [cmd, lp, orders_group, flag])
        if lp:
            check_field_handle(g, cmd, 0x24, lp, "set_army_leader/leader")
        check_field_handle(g, cmd, 0x2C, orders_group, "set_army_leader/army")
        s = _mkstring(g)
        ok = g.call(RUN_CMD, [cmd, s]) & 0xFF
        if not ok:
            return False, _readstring(g, s) or "command rejected"
        return True, ""
    finally:
        if own: g.detach()


# ------------------------------------------------------------------ general
AL_ATTACK    = 0xF58
AL_DEFENSE   = 0xF68
AL_PLANNING  = 0xF78
AL_LOGISTICS = 0xF88
AL_NAME      = 0x40
AL_TAG       = 0x120


def leader_skills(g, army_leader):
    """A CArmyLeader's effective skills, trait bonuses included."""
    p = g.p
    return {
        "attack":    p.i32(army_leader + AL_ATTACK),
        "defense":   p.i32(army_leader + AL_DEFENSE),
        "planning":  p.i32(army_leader + AL_PLANNING),
        "logistics": p.i32(army_leader + AL_LOGISTICS),
    }


# ================================================================== BATTLE PLANS
# COrderNewRootCommand  = give a front (CFront) a "line" order.
#   ctor(this, COrdersGroup* og, void* front_handle_ptr, i32 section,
#        i64 from, i64 to, CPdxArray<CArmy*>* divisions|0, u8 flag)
#   fields:  +0x24 og handle | +0x30 array | +0x48 root_front handle
#             +0x50 root_section | +0x58 from | +0x60 to | +0x6c flag
#   from/to  : 0..100000 fixed point (which slice of the front)
NR_CTOR   = 0x21f3a10
# 2026-11-08: on 1.19.4.1 `COrderNewRootCommand` has two ctors, both writing
# vtable 0x446a588: 0x2325E10 (7 args, takes an internal front object) and
# 0x2326180 (8 args, takes a pointer to an {i32 type, i32 id} handle + section).
# cmdctors.pkl only knows the 4-arg-looking wrapper, so NR_CTOR above is dead on
# this build. The 8-arg one is what the UI uses once a front handle exists.
NR_CTOR8  = 0x2326180
NR_CANEXE = 0x21f4280
NR_SIZE   = 0x70
# COrderExecuteCommand
OE_CTOR   = 0x21fee60
OE_CANEXE = 0x21ff200
OE_SIZE   = 0x38

FRONT_HANDLE   = 0x08
FRONT_PROVS    = 0x20     # CPdxArray<CProvince*>
FRONT_SECTIONS = 0x58     # CPdxArray<CFrontSection*>
SEC_PROVS      = 0x30
SEC_ID         = 0x08     # NOTE: the section ID, not its index in the array
PROV_ID        = 0xA4
PROV_STATE     = 0xE0
FULL = 100000

_ORDER_CTORS_CACHE = None


def _order_ctor(name, fallback=None):
    """The 1.19.4.1 constructor VA of a COrder*Command, from cmdctors.pkl.

    The constants below (NR_CTOR/NF_CTOR/OE_CTOR/OD_CTOR/OA_CTOR) are 1.19.2
    leftovers: calling them on this build jumps into an unrelated function. The
    pickle is produced by `python3 calling/cmdctor.py` from the ctor of every
    vtable that carries a `*Command` RTTI name, so it follows the binary.
    """
    global _ORDER_CTORS_CACHE
    if _ORDER_CTORS_CACHE is None:
        try:
            import cmdctor
            _ORDER_CTORS_CACHE = cmdctor.build()
        except Exception:
            _ORDER_CTORS_CACHE = {}
    entry = _ORDER_CTORS_CACHE.get(name) or {}
    va = entry.get("ctor") or fallback
    if isinstance(va, str):
        va = int(va, 16)
    if va:
        import api
        api.allow_call(int(va))
    return va


def fronts_towards(g, enemy_tag, own_tag=None, refresh_if_empty=False):
    """Front sections FACING enemy_tag.

    A front section can touch more than one neighbour (a Silesian section may have
    province bordering Czechoslovakia too), so a section is assigned to whichever
    country the majority of them face; otherwise a line drawn against Poland would
    also appear on the Czechoslovak border.

    `refresh_if_empty`: the Polish border CFront is created after the annexations,
    so a cache from 1936 can miss it. Rebuild the ObjectIndex ONCE (3.8 GB scan)
    and retry — never do this inside a tick, it takes minutes.
    """
    out = _fronts_towards(g, enemy_tag, own_tag)
    if not out and refresh_if_empty:
        g.oi.build(force=True)
        out = _fronts_towards(g, enemy_tag, own_tag)
    return out


def _fronts_towards(g, enemy_tag, own_tag=None):
    from game import _arr
    from collections import Counter
    import mapdata
    p = g.p
    if own_tag is None:
        own_tag = g.player().tag
    owners, _ = mapdata.province_owner_map(g)
    adj = mapdata.build_adjacency()
    _, ptype = mapdata.load_definitions()

    def faces(pid):
        """The FOREIGN countries neighbouring this province."""
        out = Counter()
        for n in adj.get(pid, ()):
            t = owners.get(n)
            if t and t != own_tag and ptype.get(n) == "land":
                out[t] += 1
        return out

    out = []
    for f in g.oi.instances("CFront"):
        try:
            secs = _arr(p, f, FRONT_SECTIONS)
        except Exception:
            continue
        for s in secs:
            try:
                ids = [p.i32(x + PROV_ID) for x in _arr(p, s, SEC_PROVS)
                       if g.oi.class_of(x) == "CProvince"]
            except Exception:
                continue
            ids = [pid for pid in ids if owners.get(pid) == own_tag]   # our provinces only
            if not ids:
                continue
            tally = Counter()
            mine = []
            for pid in ids:
                fc = faces(pid)
                for t in fc:
                    tally[t] += 1
                if enemy_tag in fc:
                    mine.append(pid)
            if not mine:
                continue
            top = tally.most_common(1)[0]
            if top[0] != enemy_tag:      # this section faces a different country
                continue
            out.append({"front": f,
                        "front_handle": [p.i32(f + 8), p.i32(f + 0xC)],
                        "section": p.i32(s + SEC_ID),
                        "section_index": secs.index(s),
                        "border_provinces": sorted(mine),
                        "faces": dict(tally)})
    return out


def draw_front_line(g, orders_group, front, section=None, frm=0, to=FULL,
                    divisions=None, flag=0):
    """Give an army or army group a line order along a whole CFront.

    2026-11-08 (work order 1108 §3), corrected by disassembling the binary:
    `COrderNewRootCommand` has TWO constructors that both write vtable 0x446a588.

      0x2325E10  7 args  (this, COrdersGroup* og, CFront* front, i64 from,
                          i64 to, CPdxArray<CArmy*>*, u8 flag)
                          +0x48 = [[front+0x10]+0x8] | +0x50 = *(i32*)(front+0x8)
                          +0x58 = from | +0x60 = to | +0x6c = flag
      0x2326180  8 args  (this, og, i32* handle_buf, i32 section, i64 from,
                          i64 to, CPdxArray<CArmy*>*, u8 flag)
                          +0x48 = *handle_buf | +0x50 = section

    `cmdctors.pkl` only records 0x2325E10 (the 7-arg one), and the old code
    called it with the 8-arg convention.  That shifted every argument by one:
    `from` became `to`, `to` (100000) became the division array pointer and the
    ctor dereferenced address 100000 -> SIGSEGV at rip=0x580bc5176128, i.e. the
    "COrderNewRootCommand does nothing and then crashes" tombstone from run 13.

    The game's own call site of the 7-arg ctor (0x1a70fcb) passes an object that
    is NOT a plain `CFront*` (it carries a 0x48-stride array at +0x380), and a
    live call with `fronts_towards`'s CFront* SIGSEGV'd inside `Run`. The 8-arg
    ctor is the one the UI uses when the handle already exists: its own caller
    (0x1a777ba) passes `&order->front_handle` — a pointer to an 8-byte
    `{i32 type, i32 id}` — plus the section id. We build exactly that buffer.
    """
    p = g.p
    ctor = NR_CTOR8
    okg, msgg = _ctor_guard(g, ctor, "COrderNewRootCommand")
    if not okg:
        return False, msgg
    try:
        import api as _api
        _api.allow_call(int(ctor))
    except Exception:
        pass
    if not front:
        return False, "no CFront"
    own = g._inj is None
    if own: g.attach()
    try:
        hp = g.malloc(8)
        p.write(hp, p.read(front + FRONT_HANDLE, 8))
        sec = int(section or 0)
        arr = _mkarray(g, list(divisions)) if divisions else 0
        cmd = g.malloc(NR_SIZE); p.write(cmd, b"\0" * NR_SIZE)
        g.call(ctor, [cmd, orders_group, hp, sec, int(frm), int(to), arr, int(flag)])
        check_field_handle(g, cmd, 0x24, orders_group, "draw_front_line/og")
        warn = ""
        try:
            if p.u64(cmd + 0x48) != p.u64(hp):
                warn = "handle field +0x48 mismatch"
            if p.i32(cmd + 0x50) != sec:
                warn = warn or "section field +0x50 mismatch (%d != %d)" % (
                    p.i32(cmd + 0x50), sec)
            if p.i64(cmd + 0x58) != int(frm) or p.i64(cmd + 0x60) != int(to):
                warn = warn or "from/to mismatch"
        except Exception as e:
            warn = "post-check skipped: %s" % e
        s = _mkstring(g)
        ok = g.call(RUN_CMD, [cmd, s]) & 0xFF
        if not ok:
            return False, _readstring(g, s) or ("CanExecute false%s"
                                                % (" [%s]" % warn if warn else ""))
        return True, warn
    finally:
        if own: g.detach()


def orders_of(g, orders_group):
    """The army's current orders (COrderInstance)."""
    from game import _arr
    p = g.p
    out = []
    for o in _arr(p, orders_group, OG_ORDERS):
        out.append({
            "ptr": hex(o),
            "id": p.i32(o + 0x244),
            "type": p.i32(o + 0x30),          # 1=offensive 2=front line 3=invasion 4=paradrop
            "assigned_divisions": p.i32(o + 0x21C),
            "root_front": [p.i32(o + 0x258), p.i32(o + 0x25C)],
            "root_section": p.i32(o + 0x260),
            "from": p.i64(o + 0x268),
            "to": p.i64(o + 0x270),
            "fallback": bool(p.u8(o + 0x298)),
        })
    return out


# COrderDeleteCommand(this, COrdersGroup*, i32 order_id) deletes an order
# 2026-11-17 RE-DERIVED for the 2026-11-13 hoi4.exe with
# research/windows/vtscan.py COrderDeleteCommand:
#   the only `.text` site that stores vtable 0x2a84c20 into [rcx] is the function
#   at 0x18C89D0.  It initialises +0x08 (byte 0), +0x0c (int -1), +0x10 (qword 0),
#   +0x18 (int 0xFF), +0x1c (qword 0), writes the vtable at +0x00, copies the
#   COrdersGroup handle ({u64} at og+8) into +0x28, and does
#   `mov [rbx+0x30], edi` for the order id  (arg3 -> +0x30, NOT +0x2C:
#   the old +0x24/+0x2C table was the 1.19.4.1 layout).
OD_CTOR   = 0x18C89D0
OD_CANEXE = None               # not called; _run_cmd does CanExecute -> Post
OD_SIZE   = 0x38


def delete_order(g, orders_group, order_id):
    p = g.p
    own = g._inj is None
    if own: g.attach()
    try:
        import api as _api
        ok, msg = _ctor_guard(g, OD_CTOR, "COrderDeleteCommand")
        if not ok:
            return False, msg
        _api.allow_call(OD_CTOR)
        cmd = g.malloc(OD_SIZE); p.write(cmd, b"\0" * OD_SIZE)
        g.call(OD_CTOR, [cmd, orders_group, int(order_id)])
        if not p.u64(cmd):
            return False, "COrderDeleteCommand ctor wrote no vtable"
        if p.i32(cmd + 0x30) != int(order_id):
            return False, ("order id not written (+0x30 = %d, wanted %d)"
                           % (p.i32(cmd + 0x30), int(order_id)))
        rv = g._run_cmd(cmd)
        if not (rv & 0xFF):
            return False, getattr(g, "_last_cmd_error", "") or "CanExecute false (no such order)"
        return True, ""
    finally:
        if own: g.detach()


# ---------------------------------------------------------------- ctor guard
# 2026-11-17: hoi4.exe was replaced on 2026-11-13 (LastWriteTime 14:04) and every
# COrder*Command ctor constant in this file is a **1.19.4.1** offset.  Verified
# with research/windows/vtscan.py, which re-derives the ctor from the RTTI vtable
# that research/windows/rtti_win.pkl already holds (only ONE function in the whole
# .text stores a given command vtable into [rcx]):
#
#   OD_CTOR 0x231C510 is TWO BYTES INTO `mov rdx,[rbx+0xd0]`  -- 0x231C510
#   disassembles from `00 00 48 85 d2 ...`, i.e. the tail of an instruction.
#   Calling it is exactly the c0000005 at hoi4.exe+0x231C510 in the Windows event
#   log that killed the game at 2026-11-17 19:43:59 on hoi4_delete_order.
#
# Same story for COrderAssignCommand: OA_CTOR 0x231D060 is a real function but it
# stores vtable 0x2BBA00A, not COrderAssignCommand's 0x2A84CE0.
#
# So: never call a ctor blind.  Every hand-rolled ctor goes through _ctor_guard(),
# which reads the live function prologue and REFUSES unless it really stores the
# class vtable -- a wrong constant now becomes a clear error instead of a dead game.
_CMD_VT = {
    "COrderDeleteCommand":   0x2A84C20,
    "COrderAssignCommand":   0x2A84CE0,
    "COrderNewFrontCommand": 0x2A853A0,
    "COrderNewRootCommand":  0x2A856A0,
    "COrderExecuteCommand":  0x2A85D60,
    "COrderUnassignCommand": 0x2A84DA0,
    "COrderDeleteAllCommand": 0x2A84AA0,
}


_PROLOGUES = (
    b"\x40\x53", b"\x40\x55", b"\x40\x56", b"\x40\x57",
    b"\x48\x89\x5c\x24", b"\x48\x89\x4c\x24", b"\x48\x89\x54\x24",
    b"\x48\x83\xec", b"\x48\x81\xec", b"\x4c\x8b\xdc", b"\x48\x8b\xc4",
    b"\x41\x54", b"\x41\x55", b"\x41\x56", b"\x41\x57",
    b"\x55", b"\x53", b"\x56", b"\x57",
)


def _ctor_guard(g, rva, cls):
    """(ok, msg).  True only when `rva` really constructs `cls` on this build.

    Two independent checks, because a stale constant fails in two different ways:
      * it lands in the MIDDLE of an unrelated instruction (0x231C510 did) ->
        the prologue test catches it;
      * it lands on a real function that just belongs to another class
        (0x231D060 did) -> the vtable-store test catches it.
    """
    vt = _CMD_VT.get(cls)
    if not vt:
        return True, "unchecked"
    p = g.p
    try:
        # rva is an IMAGE OFFSET: p.read wants an absolute address.
        blob = p.read(p.base + int(rva), 0x180)
    except Exception as e:
        return False, "cannot read ctor 0x%x: %r" % (int(rva), e)
    if not any(blob.startswith(pr) for pr in _PROLOGUES):
        return False, ("ctor 0x%x does not start on a function prologue "
                       "(%s) -- stale offset, refusing"
                       % (int(rva), blob[:6].hex()))
    for i in range(len(blob) - 7):
        # lea rax,[rip+disp32]  -> the vtable
        if blob[i:i + 3] == b"\x48\x8d\x05":
            disp = struct.unpack_from("<i", blob, i + 3)[0]
            if int(rva) + i + 7 + disp == vt:
                return True, ""
        # mov qword ptr [rcx], imm32
        if blob[i:i + 3] == b"\x48\xc7\x01":
            if struct.unpack_from("<I", blob, i + 3)[0] == vt:
                return True, ""
        # mov rax, imm64
        if blob[i:i + 2] == b"\x48\xb8":
            if struct.unpack_from("<Q", blob, i + 2)[0] == vt:
                return True, ""
    return False, ("ctor 0x%x does NOT store the %s vtable (0x%x) -- this is a "
                   "stale 1.19.4.1 offset for the current hoi4.exe; refusing "
                   "instead of killing the process"
                   % (int(rva), cls, vt))


# ------------------------------------------------------------------ assigning divisions
# COrderAssignCommand(this, CPdxArray<handle>* divisions, COrdersGroup* og, i32 order_id)
OA_CTOR   = 0x18CFD10   # 2026-11-17 re-derived with research/windows/vtscan.py.
                        # COrderAssignCommand has NO plain constructor in this
                        # build -- the ctor is INLINED into a factory:
                        #   40 53 / 48 83 ec 20 / mov ecx,0x50 / call operator new
                        #   mov rbx,rax / test / mov byte [rax+8],0
                        #   lea rcx,[rbx+0x28] / mov dword [rax+0xc],-1
                        #   ... lea rax,[0x2a84ce0] / mov [rbx],rax
                        #   [rbx+0x40] = global null handle / [rbx+0x48]=0 / [rbx+0x4c]=0
                        # It allocates its own 0x50-byte object and RETURNS it in rax,
                        # so the caller must use the returned pointer, not a
                        # caller-allocated buffer.  Field layout is unchanged from
                        # the 1.19.4.1 one (+0x40 og handle / +0x48 order id /
                        # +0x4c flag), which is why only the call site changed.
OA_CANEXE = 0x21ED460
OA_SIZE   = 0x50
CARMY_HANDLE = 0x18


def _mkhandlearray(g, divisions):
    """Build a CPdxArray<handle> from a list of CArmy*."""
    p = g.p
    n = len(divisions)
    data = g.malloc(max(1, n) * 8)
    p.write(data, b"".join(p.read(d + CARMY_HANDLE, 8) for d in divisions))
    hdr = g.malloc(0x18)
    p.write(hdr, struct.pack("<QiiQ", data, n, n, p.base + ALLOC))
    return hdr


def assign_to_order(g, orders_group, order_id, divisions):
    """Assign divisions to a battle plan order (dragging them onto it in the UI)."""
    p = g.p
    if not divisions:
        return False, "no divisions to assign"
    own = g._inj is None
    if own: g.attach()
    try:
        arr = _mkhandlearray(g, list(divisions))
        okg, msgg = _ctor_guard(g, OA_CTOR, "COrderAssignCommand")
        if not okg:
            return False, msgg
        try:
            import api as _api
            _api.allow_call(int(OA_CTOR))
        except Exception:
            pass
        cmd = g.call(OA_CTOR, [0])                  # factory: allocates + returns
        if not cmd:
            return False, "COrderAssignCommand factory returned null"
        if not p.u64(cmd):
            return False, "COrderAssignCommand factory wrote no vtable"
        # 2026-11-08 (work order 1108(2) §1.1): the COrdersGroup handle goes at
        # +0x40, NOT +0x24. The ctor itself writes `mov [rdi+0x40], rax` from the
        # global null-handle at 0x231d0b1, and cmdctors.pkl records
        # {'offset': 0x40, 'arg': 2, 'kind': 'handle'} for this class. Writing it
        # at +0x24 left +0x40 at its default, RUN_CMD silently refused, and every
        # newly trained division stayed in the pool (the 5/4/6/7/2-division
        # fragments seen in the 1939 run).
        p.write(cmd + 0x40, p.read(orders_group + OG_HANDLE, 8))
        p.write(cmd + 0x28, p.read(arr, 0x18))      # whole CPdxArray header
        p.write(cmd + 0x48, struct.pack("<i", int(order_id)))
        check_field_handle(g, cmd, 0x40, orders_group, "assign_to_order/og")
        if p.i32(cmd + 0x34) != len(divisions):
            return False, "division array was not filled (%d/%d)" % (p.i32(cmd + 0x34), len(divisions))
        ok = g.call(RUN_CMD, [cmd, _mkstring(g)]) & 0xFF
        if not ok:
            return False, "CanExecute false"
        return True, ""
    finally:
        if own: g.detach()


def group_divisions(g, orders_group, tag=None):
    """Divisions of an army, or of every army in an army group. CArmy bases.

    The array at COrdersGroup+0x50 holds the sub-object at CArmy+0xB8, which is why
    divisions are gathered from the country's own list via the `CArmy+0xC0` link.
    """
    from game import _arr
    p = g.p
    members = {orders_group}
    if p.u8(orders_group + OG_IS_GROUP):
        members |= set(_arr(p, orders_group, OG_MEMBERS))
    co = (g.country(tag) if tag else g.player()).ptr
    out = []
    for d in _arr(p, co, 0x290):
        og = p.u64(d + CARMY_OG)
        if og in members or (og and p.u64(og + OG_PARENT) in members):
            out.append(d)
    return out


# ------------------------------------------------------------------ execute plan
# COrderExecuteCommand(this, COrdersGroup* og, i32 order_id, u8 flag)
#   order_id = 0  -> the group's ENTIRE plan
OE2_CTOR   = 0x2332310   # VERIFIED 2026-11-08: writes vtable 0x446b078 at 0x2332359.
                         #   (this, COrdersGroup* og, i32 order_id, u8 flag, u8 flag2)
                         #   +0x24 = og handle | +0x2c = order_id | +0x30 = flag | +0x31 = flag2
                         # The old 0x21FEE60 was a 1.19.2 leftover: it returns without
                         # writing the vtable, so RUN_CMD saw [cmd]==0 and SIGSEGV'd at
                         # RUN_CMD+0xDE (0x20AE17E) with rax=0.
OE2_CANEXE = 0x21FF200
OE2_SIZE   = 0x38


def execute_plan(g, orders_group, order_id=0, flag=0):
    """Execute the battle plan, the green button in the UI.

    Divisions start marching to the line. order_id=0 means the army's whole plan.
    """
    p = g.p
    ctor = OE2_CTOR   # verified; `_order_ctor` falls back to the stale 1.19.2 one
    okg, msgg = _ctor_guard(g, ctor, "COrderExecuteCommand")
    if not okg:
        return False, msgg
    try:
        import api as _api
        _api.allow_call(int(ctor))
    except Exception:
        pass
    if not ctor:
        return False, "COrderExecuteCommand ctor not found"
    own = g._inj is None
    if own: g.attach()
    try:
        cmd = g.malloc(OE2_SIZE); p.write(cmd, b"\0" * OE2_SIZE)
        g.call(ctor, [cmd, orders_group, order_id, flag, 0])
        # 2026-11-08: RUN_CMD does `mov rax,[cmd]; call [rax+0x50]` at 0x20AE17E.
        # When the constructor never wrote the vtable, [cmd] is 0 and that is the
        # SIGSEGV at RIP=RUN_CMD+0xDE with rax=0 that killed two runs. Refuse.
        if not p.u64(cmd):
            return False, ("COrderExecuteCommand ctor %#x wrote no vtable "
                           "(wrong ctor address for this build)" % ctor)
        check_field_handle(g, cmd, 0x24, orders_group, "execute_plan/og")
        if p.i32(cmd + 0x2C) != order_id:
            return False, "order id was not written"
        s = _mkstring(g)
        ok = g.call(RUN_CMD, [cmd, s]) & 0xFF
        if not ok:
            return False, _readstring(g, s) or "CanExecute false (no plan, or it cannot be executed)"
        return True, ""
    finally:
        if own: g.detach()


# ================================================================== OFFENSIVE LINE
# COrderNewFrontCommand = a CHILD FRONT (kind 1) under a root front: an offensive line.
#   ctor(this, COrdersGroup* og, CPdxArray<i32>* province_yolu, COrderInstance* ust_emir,
#        CPdxArray<CArmy*>* divisions|0, u8, u8, u8, void*|0)
#   +0x24 og | +0x38 division handle array | +0x50 province id array
#   +0x68 parent order's instance_id | +0x6C/+0x6D/+0x6E flags
NF_CTOR   = 0x232CC20   # VERIFIED 2026-11-08 (vtable 0x446ac90, 9 UI call sites).
                        # The old 0x21F9BF0 was a 1.19.2 leftover.
NF_CANEXE = 0x232E3C0   # 2026-11-08: real vtable+0x50 entry (was 0x21FB390 = 1.19.2, calling it SIGSEGVs).
NF_SIZE   = 0x70


def _mkintarray(g, ids):
    p = g.p
    n = len(ids)
    data = g.malloc(max(1, n) * 4)
    p.write(data, b"".join(struct.pack("<i", int(x)) for x in ids))
    hdr = g.malloc(0x18)
    p.write(hdr, struct.pack("<QiiQ", data, n, n, p.base + ALLOC))
    return hdr


def target_line(g, enemy_tag, depth="full", own_tag=None):
    """Province list for an offensive line inside enemy territory.

    depth="full" -> the enemy provinces FURTHEST from us, i.e. all the way through.
    depth=<n> -> that many provinces in from the border.
    """
    import mapdata
    from collections import deque
    if own_tag is None:
        own_tag = g.player().tag
    owners, _ = mapdata.province_owner_map(g)
    adj = mapdata.build_adjacency()
    _, ptype = mapdata.load_definitions()
    enemy = {pid for pid, t in owners.items() if t == enemy_tag and ptype.get(pid) == "land"}
    if not enemy:
        return []
    start = [pid for pid in enemy
             if any(owners.get(n) == own_tag and ptype.get(n) == "land" for n in adj.get(pid, ()))]
    if not start:
        start = sorted(enemy)[:1]
    dist = {p0: 0 for p0 in start}
    q = deque(start)
    while q:
        cur = q.popleft()
        for n in adj.get(cur, ()):
            if n in enemy and n not in dist:
                dist[n] = dist[cur] + 1
                q.append(n)
    if depth == "full":
        top = max(dist.values())
        return sorted(p0 for p0, d in dist.items() if d == top)
    depth = int(depth)
    return sorted(p0 for p0, d in dist.items() if d == depth) or sorted(dist, key=dist.get)[-1:]


def offensive_line(g, orders_group, parent_order_id, provinces, divisions=None):
    """Add an OFFENSIVE LINE (a child front) to a root front order."""
    p = g.p
    from game import _arr
    if not provinces:
        return False, "target province list is empty"
    ctor = NF_CTOR
    okg, msgg = _ctor_guard(g, ctor, "COrderNewFrontCommand")
    if not okg:
        return False, msgg
    try:
        import api as _api
        _api.allow_call(int(ctor))
    except Exception:
        pass
    if not ctor:
        return False, "COrderNewFrontCommand ctor not found"
    own = g._inj is None
    if own: g.attach()
    try:
        parent = None
        for o in _arr(p, orders_group, OG_ORDERS):
            if p.i32(o + 0x244) == parent_order_id:
                parent = o
                break
        if parent is None:
            return False, "parent order not found: %d" % parent_order_id
        path = _mkintarray(g, provinces)
        # 2026-11-08 (work order vtable_1108 §1.2): the ctor's 5th parameter is a
        # C++ `const CPdxArray<CArmy*>&`, i.e. a POINTER to a valid array header.
        # The UI call site 0x7b2e86 passes `lea [rbp-0x80]`, a size-0 array — never
        # NULL. Passing 0 made the ctor dereference a low address and SIGSEGV.
        darr = _mkarray(g, list(divisions) if divisions else [])
        cmd = g.malloc(NF_SIZE); p.write(cmd, b"\0" * NF_SIZE)
        g.call(ctor, [cmd, orders_group, path, parent, darr, 0, 0, 0, 0])
        check_field_handle(g, cmd, 0x24, orders_group, "offensive_line/og")
        if p.i32(cmd + 0x68) != parent_order_id:
            return False, "parent order id was not written (%d)" % p.i32(cmd + 0x68)
        if p.i32(cmd + 0x5C) != len(provinces):
            return False, "province path was not filled (%d/%d)" % (p.i32(cmd + 0x5C), len(provinces))
        s = _mkstring(g)
        ok = g.call(RUN_CMD, [cmd, s]) & 0xFF
        if not ok:
            return False, _readstring(g, s) or "CanExecute false (not at war, or the line is not valid)"
        return True, ""
    finally:
        if own: g.detach()


# ------------------------------------------------------------------ fallback line
# COrderNewFallbackCommand(this, og, CPdxArray<i32>* province_path, CPdxArray<CArmy*>* divisions|0)
# V2-05: 1.19.4.1 COrderNewFallbackCommand ctor 0x23247A0, vt 0x446A330.
FB_CTOR   = 0x23247A0
FB_CANEXE = 0x2324EC0          # reference only
FB_SIZE   = 0x1D8   # C3: 472 bytes in 1.19.4.1 (cmdfields.pkl). 0x70 overflowed the heap block by 360 bytes.


def fallback_line(g, orders_group, provinces, divisions=None):
    """Issue a fallback line order."""
    p = g.p
    if not provinces:
        return False, "province list is empty"
    own = g._inj is None
    if own: g.attach()
    try:
        path = _mkintarray(g, provinces)
        darr = _mkarray(g, list(divisions)) if divisions else 0
        import api as _api
        _api.allow_call(FB_CTOR)
        cmd = g.malloc(FB_SIZE); p.write(cmd, b"\0" * FB_SIZE)
        g.call(FB_CTOR, [cmd, orders_group, path, darr])
        if not p.u64(cmd):
            return False, "COrderNewFallbackCommand ctor wrote no vtable"
        check_field_handle(g, cmd, 0x24, orders_group, "fallback_line/og")
        rv = g._run_cmd(cmd)
        if not (rv & 0xFF):
            return False, getattr(g, "_last_cmd_error", "") or "CanExecute false"
        return True, ""
    finally:
        if own: g.detach()


# --------------------------------------------------- naval invasion and paradrop
# NOTE: the field meanings of these two commands could not be tested in a war (the
# game was at peace). For safety they are only sent if the game's own CanExecute agrees
# and `confirm=True` is required.
# V2-05 (verified): 1.19.4.1 addresses straight out of cmdctors.pkl.
# NOTE the "corrections" in the V2 work order (0x2333940 / 0x23360C0 / 0x2336C80)
# are all exactly +0x40 off and land in the MIDDLE of the function — objdump shows
# 0x2333900/0x2336080/0x2336C40 are the real `push rbp; mov rbp,rsp` prologues.
INV_CTOR = 0x2333900; INV_CANEXE = 0x2333C40; INV_SIZE  = 0x1D8   # COrderSetInvasionSourceCommand
PD_CTOR  = 0x2336080; PD_CANEXE  = 0x23363C0; PD_SIZE   = 0x1D8   # COrderSetParadropSourceCommand
PDT_CTOR = 0x2336C40; PDT_CANEXE = 0x2336F80; PDT_SIZE = 0x38     # COrderSetParadropTargetCommand
GET_ORDER   = 0x2346C40   # COrdersGroup::GetOrder(i32 id) -> COrderInstance*
PRED_TARGET = 0x22EEFE0   # COrderInstance::CanSetProvinceTarget(order, CProvince*)


def naval_invasion(g, orders_group, order_id, source_province, target_province,
                   flag=0, divisions=None, confirm=False):
    """Naval invasion order (kind 3).

    Constructor signature: (this, og, i32, u8, CPdxArray<CArmy*>*, i32, i32)
    Which integer is source and which is target could not be tested in a war, so the
    command is only sent when CanExecute agrees and confirm=True is passed.
    """
    p = g.p
    own = g._inj is None
    if own: g.attach()
    try:
        darr = _mkarray(g, list(divisions) if divisions else [])
        cmd = g.malloc(INV_SIZE); p.write(cmd, b"\0" * INV_SIZE)
        g.call(INV_CTOR, [cmd, orders_group, int(order_id), int(flag), darr,
                          int(source_province), int(target_province)])
        check_field_handle(g, cmd, 0x24, orders_group, "naval_invasion/og")
        ok = g.call(INV_CANEXE, [cmd, 0]) & 0xFF
        if not ok:
            return False, ("the game refused (you must be at war, and have a source port and target "
                           "and a suitable coastline)")
        if not confirm:
            return False, "looks applicable, but this command has never been tested; pass confirm=true"
        g.cmd.post(cmd)
        return True, ""
    finally:
        if own: g.detach()


def paradrop(g, orders_group, source_province, target_province,
             divisions=None, order_id=0, confirm=False, wait=8.0):
    """Build a PARADROP order (COrderInstance kind 4) with the game's own commands.

    Read out of 1.19.4.1 and then measured live (2026-11-10):

      COrderSetParadropSourceCommand   vtable 0x446B780  ctor 0x2336080
        CanExecute 0x23365E0   Execute 0x2336390
        (this, COrdersGroup*, i32 province_id, i32 order_id, CPdxArray* divisions)
          +0x24 og handle | +0x2c order_id | +0x30 province_id | +0x38 division array
        CanExecute: GetProvince(+0x30) != NULL && 0x22EED60(og, province)
        Execute:    og = ResolveHandle(+0x24)
                    order = CreateOrder(og, 4) when +0x2c == 0, else FindOrderById(og, +0x2c)
                    order->+0xB8 = +0x30                    # source province
                    if order->+0x7C == 0: return            # no target yet -> stops here
                    when +0x44 > 0: posts COrderAssignCommand with the array

      COrderSetParadropTargetCommand   vtable 0x446B848  ctor 0x2336C40
        CanExecute 0x2336E50   Execute 0x2336D20
        (this, COrdersGroup*, i32 order_id, i32 province_id)
          +0x2c order_id | +0x30 province_id
        Execute: order = FindOrderById(og, +0x2c); targets[0] = +0x30; count = 1
        CanExecute ends in 0x22EEFE0(order, province): land province, not the source,
        and inside paradrop range of a transport wing.

    Two things that cost a whole evening and are therefore written down:

      1. `CCommand::Run` (0x20AE0A0) only *queues* the command. The order shows up on
         the game's next tick, and the injector freezes every game thread while it is
         attached, so the wait loop must run with the process detached.
      2. order_id != 0 is refused by CanExecute; the game's own UI call site passes 0
         (`xor ecx,ecx` at 0x80a269). Every call therefore creates a new type-4 order
         and the engine never merges them.

      3. 2026-11-10, after nine rounds of "0 hits": every target province was
         refused because the ORDER'S SOURCE PROVINCE must be a province whose air
         base actually hosts the transport wing.  CanExecute ends in
             0x23DF580(CAirBase(GetProvince(order->0xB8)), 0x100000,
                       &ordersGroup->0x3C, targetProvince)
         and 0x23DF580 walks CAirBase+0x30[countryIndex] (a CCountryAirContainer,
         wings at +0xB8/+0xC4) keeping the biggest range among the wings whose
         definition carries the 0x100000 capability bit (transport planes).  No
         transport wing in that base -> range 0 -> the target is refused no matter
         which province it is.  The old sweeps set the source to 11500/6521/64/908,
         none of which is the transport base (519 here), so 0 of 1291 land provinces
         passed.  With the source at 519, 109 provinces pass immediately.
         airops.paradrop_source_provinces() / source_has_transport() now resolve and
         verify that, and paradrop() refuses a source without transport planes.
    """
    import time
    p = g.p
    own = g._inj is None
    from game import _arr as _garr

    def ids():
        if own:
            g.attach()
        try:
            return {p.i32(o + 0x244) for o in _garr(p, orders_group, OG_ORDERS)}
        finally:
            if own:
                g.detach()

    import api as _api
    import airops as _airops
    _api.allow_call(PD_CTOR)
    _api.allow_call(PDT_CTOR)

    # A paradrop source is only legal when that province's air base hosts a
    # transport-capable wing of this country: COrderInstance::CanSetProvinceTarget
    # (0x22EEFE0) asks 0x23DF580(CAirBase, 0x100000, &ordersGroup->0x3C, target)
    # and rejects the target when that returns 0.  Resolve or verify it here so
    # the caller does not have to know which air base the transports sit in.
    src_note = ""
    if own:
        g.attach()
    try:
        if source_province is None:
            cands = _airops.paradrop_source_provinces(g, orders_group)
            if not cands:
                return False, ("no transport-plane wing of this country is stationed in any "
                               "air base; a paradrop needs one")
            source_province = cands[0]["province"]
            src_note = " (auto: transport wing based in this province)"
        else:
            wings = _airops.source_has_transport(g, orders_group, int(source_province))
            if not wings:
                cands = _airops.paradrop_source_provinces(g, orders_group)
                return False, ("province %d cannot start a paradrop: its air base holds no "
                               "transport wing of this country. Use one of %s"
                               % (int(source_province),
                                  [c["province"] for c in cands] or "none"))
    finally:
        if own:
            g.detach()

    oid = int(order_id or 0)
    if oid:
        # Reuse an existing type-4 order.  COrderSetParadropSourceCommand::CanExecute
        # refuses order_id != 0, so write the source province into the order exactly
        # like that command's Execute does (order->+0xB8) and go straight to the target.
        if own:
            g.attach()
        try:
            _api.allow_call(GET_ORDER)
            op = g.call(GET_ORDER, [orders_group, oid]) & 0xFFFFFFFFFFFFFFFF
            if not op:
                return False, "order %d does not exist in this army" % oid
            p.write(op + 0xB8, struct.pack("<i", int(source_province)))
        finally:
            if own:
                g.detach()
    else:
        before = ids()
        if own:
            g.attach()
        try:
            cmd = g.malloc(PD_SIZE)
            p.write(cmd, b"\0" * PD_SIZE)
            g.call(PD_CTOR, [cmd, orders_group, int(source_province), 0, 0])
            if not p.u64(cmd):
                return False, "COrderSetParadropSourceCommand ctor wrote no vtable"
            # Divisions travel inside the source command (+0x38 CPdxArray<handle>), like
            # the UI's selection does; Execute then posts COrderAssignCommand for the
            # freshly created order.
            if divisions:
                data = g.malloc(max(1, len(divisions)) * 8)
                p.write(data, b"".join(p.read(int(d) + CARMY_HANDLE, 8) for d in divisions))
                p.write(cmd + 0x38, struct.pack("<Qii", data, len(divisions), len(divisions)))
            check_field_handle(g, cmd, 0x24, orders_group, "paradrop/og")
            rv = g._run_cmd(cmd)
        finally:
            if own:
                g.detach()
        if not (rv & 0xFF):
            return False, (getattr(g, "_last_cmd_error", "")
                           or "the game refused the paradrop source")
        deadline = time.time() + wait
        while not oid:
            new = ids() - before
            if new:
                oid = max(new)
                break
            if time.time() > deadline:
                return False, ("the source command was queued but no order appeared within "
                               "%.1fs (is the game paused?)" % wait)
            time.sleep(0.25)

    # Ask the engine's own predicate before posting the target command: it is the
    # same 0x22EEFE0 the command's CanExecute uses, so a failure here is exact.
    if own:
        g.attach()
    try:
        _api.allow_call(GET_ORDER)
        order_ptr = g.call(GET_ORDER, [orders_group, int(oid)]) & 0xFFFFFFFFFFFFFFFF
    finally:
        if own:
            g.detach()
    if not order_ptr:
        return False, "order %d disappeared from the army before the target was set" % oid

    if own:
        g.attach()
    try:
        _api.allow_call(PRED_TARGET)
        tptr = g.province_ptr(int(target_province))
        ok_pred = bool(tptr) and bool(g.call(PRED_TARGET, [order_ptr, tptr]) & 0xFF)
    finally:
        if own:
            g.detach()
    if not ok_pred:
        return False, ("province %d is refused as a paradrop target from province %d: it must "
                       "be a land province other than the source and inside the transport "
                       "wing's range (use hoi4_paradrop_targets for the legal list)"
                       % (int(target_province), int(source_province)))

    if not confirm:
        return False, ("source %d accepted%s on order %d; pass confirm=true to set the "
                       "target" % (int(source_province), src_note, oid))

    if own:
        g.attach()
    try:
        cmd2 = g.malloc(PDT_SIZE)
        p.write(cmd2, b"\0" * PDT_SIZE)
        g.call(PDT_CTOR, [cmd2, orders_group, int(oid), int(target_province)])
        if not p.u64(cmd2):
            return False, "COrderSetParadropTargetCommand ctor wrote no vtable"
        rv2 = g._run_cmd(cmd2)
    finally:
        if own:
            g.detach()
    if not (rv2 & 0xFF):
        return False, (getattr(g, "_last_cmd_error", "")
                       or ("the game refused province %d as a paradrop target for order "
                           "%d (0x22EEFE0: land, not the source, and inside paradrop "
                           "range of a transport wing)"
                           % (int(target_province), oid)))

    if divisions:
        if own:
            g.attach()
        try:
            _api.allow_call(GET_ORDER)
            op = g.call(GET_ORDER, [orders_group, int(oid)]) & 0xFFFFFFFFFFFFFFFF
            have = p.i32(op + 0x21C) if op else 0
        finally:
            if own:
                g.detach()
        if not have:
            ok, err = assign_to_order(g, orders_group, oid, list(divisions))
            if not ok:
                return False, "target accepted but the divisions were refused: %s" % err
    return True, ""



def paradrop_targets(g, orders_group, source_province=None, limit=2000):
    """Every province id the engine accepts as a paradrop target from this army.

    Uses COrderInstance::CanSetProvinceTarget (0x22EEFE0) directly, so the answer
    is exactly what COrderSetParadropTargetCommand::CanExecute would decide.  The
    source province is written into an existing type-4 order for the duration of
    the sweep and restored afterwards (the predicate reads it from the order).
    """
    import api as _api
    import airops as _airops
    from game import _arr as _garr
    p = g.p
    own = g._inj is None
    if own:
        g.attach()
    try:
        _api.allow_call(PRED_TARGET)
        if source_province is None:
            cands = _airops.paradrop_source_provinces(g, orders_group)
            if not cands:
                return []
            source_province = cands[0]["province"]
        orders = [o for o in _garr(p, orders_group, OG_ORDERS) if p.i32(o + 0x30) == 4]
        if not orders:
            return []
        order = orders[0]
        old = p.i32(order + 0xB8)
        p.write(order + 0xB8, struct.pack("<i", int(source_province)))
        out = []
        try:
            for pid in range(1, limit):
                try:
                    pr = g.province_ptr(pid)
                except Exception:
                    continue
                if not pr:
                    continue
                d8 = p.u64(pr + 0xD8)
                if not d8 or not (p.u8(d8 + 0xD2) & 1):
                    continue
                try:
                    if g.call(PRED_TARGET, [order, pr]) & 0xFF:
                        out.append(pid)
                except Exception:
                    pass
        finally:
            p.write(order + 0xB8, struct.pack("<i", old))
        return out
    finally:
        if own:
            g.detach()
