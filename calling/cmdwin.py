"""The player command channel on the Windows build (1.19.5.0).

Linux posts commands through `CCommandQueue::Post`, found by pattern-scanning
`CCommand::Run`. On the PE build the same pieces exist but at different places,
and they were recovered from the *call sites of the command constructors*
instead -- every command creation site does

    new(size) -> ctor(cmd, &tag, args...) -> Post(session, cmd, 0)

so the call that follows the constructor is `Post`, and the object it is called
on comes from the idler's vtable slot +0x88 (a `CSession*`, not the command
queue the Linux build returns).

    FN_POST         0x22C4250   Post(session, cmd, flag)  -> 1 on success
    IDLER_VT_SESSION 0x88       CSession* (GetPlayerTag is +0xA0)
    ALLOC           0x30FD000   CPdxNewDeleteAllocator object; Allocate at
                                vtable+0x08, Deallocate at vtable+0x10
    OP_NEW          0x25A01DC   CRT operator new(size_t)
    OP_DELETE       0x25CEC00   CRT operator delete(void*)

`Post` owns the command on every path: the success path hands it to
`[session+0x40]->vt[0x28](cmd)`, and each of the four drop paths ends with
`call [cmd->vt[0]](cmd, 1)` -- the deleting destructor, which releases through
CRT `operator delete`. Commands therefore have to come from `operator new`,
not from the game's `IAllocator`: mixing the two aborts the process whenever a
command is refused (the 7/7 SetResearch crashes).

The drop reasons, read from the string constants `Post` builds:

    flag != 0                        "Dropped command: "
    [session+0x725] != 0             "Dropped command: "
    cmd->vt[0x80] / vt[0x60] != 0    "Dropped command: "   (both are 0x11D530,
                                                           a `xor al,al; ret`,
                                                           so they never fire)
    ... later re-checks              "Dropped (posted during command
                                      execution): "
    [session+0x3c] != 0
    [session+0x6cc] == 0             "Dropped (sender not authorized): "
    [session+0x726] != 0
    cmd->vt[0x98] failed             "Dropped (sender not authorized): "

Command constructors are the authoritative description of a command's layout,
so we call them instead of hand-filling a struct:

    CSetNationalFocusCommand  0x11D0DC0   (cmd, CCountryTag*, CNationalFocus*)
        +0x28 int32 tag, +0x30 CNationalFocus*, min size 0x38
    CSetResearchCommand       0x11D1980   (layout not verified yet)
    CAddConstructionCommand   0x11CC8C0   (layout not verified yet)

Verified live 2026-11-14: building + posting a SetNationalFocus command changed
`current_focus()` from None to the requested focus and it survived day ticks.
"""
from __future__ import annotations
import struct
import sys

import offsets as O
import offsets_win as _OW
from commands import BASE_INIT

FN_POST = _OW.CMD_FN_POST
IDLER_VT_SESSION = _OW.CMD_IDLER_VT_SESSION
IDLER_VT_TAG = _OW.CMD_IDLER_VT_TAG
CTOR_SET_NATIONAL_FOCUS = _OW.CMD_CTOR_SET_NATIONAL_FOCUS
CTOR_SET_RESEARCH = _OW.CMD_CTOR_SET_RESEARCH
CTOR_ADD_CONSTRUCTION = _OW.CMD_CTOR_ADD_CONSTRUCTION
VT_ADD_CONSTRUCTION = _OW.CMD_VT_ADD_CONSTRUCTION
VT_BUILDING_REF = _OW.CMD_VT_BUILDING_REF
OP_NEW = _OW.CMD_OP_NEW
OP_DELETE = _OW.CMD_OP_DELETE
# `xor al, al; ret` -- the shared do-nothing stub every command vtable already
# uses for its unimplemented slots.
FN_NOOP_DTOR = _OW.CMD_FN_NOOP_DTOR


def _alloc_obj(p):
    return p.base + O.ALLOC_OFF


def alloc(game, size):
    """Allocate from the game's own heap (so the game can free it later)."""
    p = game.p
    obj = _alloc_obj(p)
    allocate = p.u64(p.u64(obj) + 0x08)
    return game.call(allocate, (obj, size), absolute=True, force=True)


def free(game, ptr):
    p = game.p
    obj = _alloc_obj(p)
    deallocate = p.u64(p.u64(obj) + 0x10)
    return game.call(deallocate, (obj, ptr), absolute=True, force=True)


def _i32buf(game, value):
    """A scratch int32 the game can read an id out of (ctors take int*)."""
    a = alloc(game, 8)
    game.p.write(a, struct.pack("<i", int(value)))
    return a


def new_cmd(game, size):
    """Allocate a command object the way the game allocates its own.

    Only used for probing: `Post` releases commands through the CRT, which is
    why objects it takes have to come from the CRT heap too.  Calling the CRT
    allocator from a hijacked thread was measured to take the process down, so
    real commands go through `alloc` + `attach_fake_vtable` instead.
    """
    return game.call(game.p.base + OP_NEW, (size,), absolute=True, force=True)


def delete_cmd(game, ptr):
    """Release a command we allocated but never posted."""
    return game.call(game.p.base + OP_DELETE, (ptr,), absolute=True, force=True)


def fake_vtable(game, vt_rva, size=0xB0):
    """A copy of a command vtable whose slot 0 (the deleting destructor) is a no-op.

    `Post` reaches `call [cmd->vt[0]](cmd, 1)` on the immediate path and the
    command queue reaches the same slot when it later discards the object.  Both
    would call the real deleting destructor, which frees through the CRT -- fatal
    for an object that came from the game's own allocator.  Giving the object a
    private vtable whose slot 0 returns immediately makes every release path a
    leak instead of a crash, and leaves every other virtual method real.
    """
    cache = getattr(game, "_fake_vts", None)
    if cache is None:
        cache = {}
        game._fake_vts = cache
    key = int(vt_rva)
    entry = cache.get(key)
    if entry is not None:
        cached_ptr, cached_size = entry if isinstance(entry, tuple) else (entry, 0xB0)
        if cached_size >= size:
            return cached_ptr
    p = game.p
    blob = bytearray(p.read(p.base + vt_rva, size))
    struct.pack_into("<Q", blob, 0, p.base + FN_NOOP_DTOR)
    fake = alloc(game, size)
    p.write(fake, bytes(blob))
    cache[key] = (fake, size)
    return fake


def attach_fake_vtable(game, cmd, vt_rva):
    """Point a command object at its no-free vtable.  Call after the ctor ran."""
    fake = fake_vtable(game, vt_rva)
    game.p.write(cmd, struct.pack("<Q", fake))
    return fake


def session_flags(game):
    """The CSession bytes `Post` gates on, before anything is posted."""
    p = game.p
    s = session(game)
    return {
        "session": s,
        "busy_0x725": p.u8(s + 0x725),
        "b_0x3c": p.u8(s + 0x3C),
        "b_0x6cc": p.u8(s + 0x6CC),
        "b_0x726": p.u8(s + 0x726),
        "tick_0x84": p.i32(s + 0x84),
        "seq_0x60": p.i32(s + 0x60),
    }


def idler(game):
    p = game.p
    v = p.u64(p.base + O.G_IDLER)
    if not v:
        raise RuntimeError("no CInGameIdler (not inside a game)")
    return v


def session(game):
    """The CSession the command queue belongs to."""
    p = game.p
    i = idler(game)
    return game.call(p.u64(p.u64(i) + IDLER_VT_SESSION), (i,),
                     absolute=True, force=True)


def player_tag_ptr(game):
    p = game.p
    i = idler(game)
    return game.call(p.u64(p.u64(i) + IDLER_VT_TAG), (i,),
                     absolute=True, force=True)


def player_tag_id(game):
    return game.p.i32(player_tag_ptr(game))


def post(game, cmd, force=0):
    """Queue a command object. force=1 skips the CanExecute check."""
    return game.call(game.p.base + FN_POST, (session(game), cmd, force),
                     absolute=True, force=True)


def command_queue(game):
    """The CCommandQueue `Post` hands accepted commands to (`session+0x40`)."""
    p = game.p
    q = p.u64(session(game) + 0x40)
    if not q:
        raise RuntimeError("no command queue at session+0x40")
    return q


def enqueue(game, cmd, tick=None, seq=None):
    """Push a command straight into the queue, skipping `Post`'s gauntlet.

    `Post`'s accepted path ends in `[session+0x40]->vt[0x28](cmd)` after stamping
    the object with the session tick (`cmd+0xC`) and a sequence number
    (`cmd+0x18`, clamped to 0x7fff).  Every other path builds a "Dropped ..."
    message and then calls the command's deleting destructor, which is where the
    earlier experiments died.  Stamp and enqueue directly instead.
    """
    import win_pipeline as _wp
    _wp.ensure_live_version(game, only_if_live=True)
    p = game.p
    s = session(game)
    if tick is None:
        tick = p.i32(s + 0x84)
    if seq is None:
        seq = p.i32(s + 0x60)
    p.write(cmd + 0xC, struct.pack("<i", int(tick) & 0xFFFFFFFF))
    p.write(cmd + 0x18, struct.pack("<H", min(int(seq) & 0xFFFF, 0x7FFF)))
    q = command_queue(game)
    fn = p.u64(p.u64(q) + 0x28)
    return game.call(fn, (q, cmd), absolute=True, force=True)


def _tag_block(game, tag_id):
    """A 16-byte block holding an int32 CCountryTag the ctor can read."""
    b = alloc(game, 0x10)
    game.p.write(b, struct.pack("<i", tag_id))
    return b


def make_national_focus(game, focus_ptr, tag_id):
    p = game.p
    cmd = alloc(game, 0x40)
    tagp = _tag_block(game, tag_id)
    game.call(p.base + CTOR_SET_NATIONAL_FOCUS, (cmd, tagp, focus_ptr),
              absolute=True, force=True)
    if p.u64(cmd) != p.base + VT_SET_NATIONAL_FOCUS or p.i32(cmd + 0x28) != tag_id:
        raise RuntimeError("CSetNationalFocusCommand ctor did not produce the "
                           "expected object (vptr=%#x tag=%d)"
                           % (p.u64(cmd), p.i32(cmd + 0x28)))
    attach_fake_vtable(game, cmd, VT_SET_NATIONAL_FOCUS)
    return cmd


def set_national_focus(game, focus_ptr, tag_id=None):
    try:
        cur = game.current_focus()
    except Exception:
        cur = None
    if isinstance(cur, dict) and cur.get("ptr") == focus_ptr:
        return {"ok": False, "posted": 0, "reason": "focus already selected"}
    if tag_id is None:
        tag_id = int(game.player_tag_id())
    cmd = make_national_focus(game, focus_ptr, tag_id)
    ok = enqueue(game, cmd)
    return {"ok": bool(ok), "cmd": cmd, "posted": ok}

def make_research(game, tech_ptr, slot, tag_id, flags=(0, 0, 0)):
    """CSetResearchCommand, built by hand from its ctor's own stores.

    ctor 0x11D1980 = (cmd, CCountryTag*, CTechnology*, int slot,
                      bool, bool, bool) and writes
        +0x08 byte 0   +0x0C -1   +0x10 0   +0x18 0xFFFF   +0x1C 0
        +0x28 int32 tag   +0x30 CTechnology*   +0x38 int32 slot
        +0x3C/+0x3D/+0x3E bools
    The three bools live on the stack, which the injector cannot pass, so the
    object is filled directly instead of calling the ctor.
    """
    p = game.p
    cmd = alloc(game, 0x40)
    blob = bytearray(0x40)
    struct.pack_into("<Q", blob, 0x00, p.base + VT_SET_RESEARCH)
    blob[0x08] = 0
    struct.pack_into("<i", blob, 0x0C, -1)
    struct.pack_into("<Q", blob, 0x10, 0)
    struct.pack_into("<I", blob, 0x18, 0xFFFF)
    struct.pack_into("<Q", blob, 0x1C, 0)
    struct.pack_into("<i", blob, 0x28, int(tag_id))
    struct.pack_into("<Q", blob, 0x30, int(tech_ptr))
    struct.pack_into("<i", blob, 0x38, int(slot))
    blob[0x3C] = flags[0] & 0xFF
    blob[0x3D] = flags[1] & 0xFF
    blob[0x3E] = flags[2] & 0xFF
    p.write(cmd, bytes(blob))
    attach_fake_vtable(game, cmd, VT_SET_RESEARCH)
    return cmd


def set_research(game, tech_ptr, slot, tag_id=None, check=True):
    """Start researching a technology in the given slot.

    `tech_ptr` must be a **CTechnologyTemplate***, not a live CTechnology: the
    engine's own CanExecute resolves the technology as
    `ts->data[tech->+0x38 - 1]`, i.e. +0x38 is a 1-based index that only the
    template carries (live objects have +0x38 == 0).  2026-11-16: every command
    built from a live object was refused in C++ and silently dropped, which is
    why all four slots stayed empty for the whole campaign.
    """
    if tag_id is None:
        tag_id = int(game.player_tag_id())
    cmd = make_research(game, tech_ptr, slot, tag_id)
    can = None
    if check:
        can = can_exec(game, cmd, CANEXEC_SET_RESEARCH)
        if not can:
            return {"ok": False, "cmd": cmd, "posted": 0, "can_execute": False,
                    "error": "CSetResearchCommand::CanExecute refused"}
    r = post_drained(game, cmd)
    return {"ok": bool(r.get("posted")), "cmd": cmd, "posted": r.get("posted"),
            "drained": r.get("drained"), "can_execute": can}


# CAddConstructionCommand is 0x50 bytes.  Layout came from a live capture of the
# real constructor (Frida hook on 0x11CC8C0 while clicking the build UI):
#
#   {"typeId": 1, "level": 1, "dl": 2,
#    "ref": {"f08": 59, "f0C": 20013, "f10": 0}}
#
# so the two fields that matter are NOT what the ctor's parameter order suggests:
#   ref+0x08 = state id,      ref+0x0C = BUILDING TYPE id,  ref+0x10 = 0
#   cmd+0x28 = 1 (not the type id), cmd+0x4C = 2 (the bool/priority)
# Getting either wrong made the queue accept the command and silently drop it,
# which is why three earlier layouts produced no building.
def make_add_construction(game, state_id, building_type_id, level=1,
                          priority=2, type_field=1):
    p = game.p
    cmd = alloc(game, 0x50)
    c = bytearray(0x50)
    struct.pack_into("<Q", c, 0x00, p.base + VT_ADD_CONSTRUCTION)
    struct.pack_into("<i", c, 0x0C, -1)
    struct.pack_into("<I", c, 0x18, 0xFFFF)
    struct.pack_into("<i", c, 0x28, int(type_field))
    struct.pack_into("<Q", c, 0x30, p.base + VT_BUILDING_REF)
    struct.pack_into("<i", c, 0x38, int(state_id))
    struct.pack_into("<i", c, 0x3C, int(building_type_id))
    struct.pack_into("<i", c, 0x40, 0)
    struct.pack_into("<i", c, 0x48, int(level))
    c[0x4C] = int(priority) & 0xFF
    p.write(cmd, bytes(c))
    attach_fake_vtable(game, cmd, VT_ADD_CONSTRUCTION)
    return cmd


def add_construction(game, state_id, building_type_id, level=1, priority=2):
    """Queue a building.  Verified live: state 5 gained a (20013, level 0) entry."""
    cmd = make_add_construction(game, state_id, building_type_id, level, priority)
    ok = enqueue(game, cmd)
    return {"ok": bool(ok), "cmd": cmd, "posted": ok}


# --- advisors --------------------------------------------------------------
#
# CAddAdvisorCommand -- fully disassembled off hoi4.exe 1.19.5.0 on 2026-11-15
# (research/windows/pedis.py; all addresses are RVAs):
#
#   RTTI  .?AVCAddAdvisorCommand@@            -> vtable 0x2A11A88
#   real param ctor  0x11CC6D0  (this, handle_block*, std::string*)
#       +0x28 <- [arg1+8]           the packed {type,id} CCharacter handle
#       +0x30 <- std::string(arg2)  slot name, MSVC copy ctor 0x11F770
#   clone ctor       0x11D4460  `mov ecx,0x50; call operator new` -> SIZE = 0x50
#       copies +0x28 (handle) and +0x30 (string) verbatim
#   CanExecute       0x11ECC20 (vtable +0x48) reads +0x28/+0x2C (handle halves),
#       resolves it, then passes lea [this+0x30] as the slot string.
#   serialize        0x11EB730 (vtable +0x98) likewise reads +0x28 and +0x30.
#
# pecmd reported `args: 0` and its "ctor" 0x11D2F00 is actually vtable SLOT 0
# -- the scalar deleting destructor (frees the std::string then operator delete).
# Calling it as a ctor is what killed hoi4.exe on 2026-11-15 (crash #1).
#
# The old builder wrote the handle at +0x24 (the *Linux* offset) and used size
# 0x70, so the command posted but CanExecute saw a zero handle and dropped it:
# "posts but never applies".  PE uses +0x28 for the handle and 0x50 bytes.
#
# We build by field layout (build_by_vt), NOT by calling the ctor: nothing
# foreign is executed, and every offset above is confirmed by disassembly.
VT_ADD_ADVISOR = _OW.CMD_VT_ADD_ADVISOR
ADD_ADVISOR_SIZE = _OW.CMD_ADD_ADVISOR_SIZE
CTOR_ADD_ADVISOR = _OW.CMD_CTOR_ADD_ADVISOR     # real param ctor (documented; not called)


def make_add_advisor(game, packed_handle, slot):
    """CAddAdvisorCommand. slot is a short name ('political_advisor', 'theorist'...)."""
    name = slot.encode("utf-8") if isinstance(slot, str) else bytes(slot)
    if not name:
        raise ValueError("advisor slot name is empty")
    # MSVC std::string: 16-byte SSO buffer at +0x30, size at +0x40,
    # capacity at +0x48. Long names (>15) need a heap buffer -- build one so
    # 'political_advisor' (17 chars) and 'high_command' work too.
    p = game.p
    if len(name) <= 15:
        str_fields = [(0x30, "16s", name.ljust(16, b"\0")),
                      (0x40, "<Q", len(name)), (0x48, "<Q", 15)]
    else:
        data = alloc(game, len(name) + 1)
        p.write(data, name + b"\0")
        str_fields = [(0x30, "<Q", data), (0x40, "<Q", len(name)),
                      (0x48, "<Q", len(name))]
    return build_by_vt(game, VT_ADD_ADVISOR, ADD_ADVISOR_SIZE, [
        (0x28, "<Q", int(packed_handle) & 0xFFFFFFFFFFFFFFFF),
    ] + str_fields)


# --- grand doctrine --------------------------------------------------------
#
# CUnlockGrandDoctrineCommand -- disassembled off 1.19.5.0 on 2026-11-15:
#   RTTI vtable 0x2AAA8A0 (also what pecmd found)
#   real ctor 0x1B0FD20 (this, int* tag, CGrandDoctrineTemplate* tpl)
#       +0x28 <- tpl (r8)     +0x30 <- *tag (rdx)
#   factory 0x1B0FEE0 `mov ecx,0x38; call operator new` -> SIZE = 0x38
#   clone 0x1B0FDE0 copies +0x28 and +0x30
#   CanExecute 0x1B10310 (vtable +0x48):
#       +0x28 == 0 -> "#~ invalid grand doctrine"
#       +0x30 <= 0 -> "#~ invalid country tag"
#   Execute 0x1B0FF80 (vtable +0x50) dereferences [tpl+0x2f8] (the doctrine
#   definition) and [tpl+0x24] (its id).
#
# Crash #3 (2026-11-15) sent only {"0x28": tpl} through send_command, so
# +0x30 (the country tag) stayed 0; that is an invalid tag and the command was
# built from a pecmd size/vtable that had never been checked. We now fill BOTH
# fields with a proven vtable+size and no ctor call.
VT_UNLOCK_DOCTRINE = _OW.CMD_VT_UNLOCK_DOCTRINE
UNLOCK_DOCTRINE_SIZE = _OW.CMD_UNLOCK_DOCTRINE_SIZE
CTOR_UNLOCK_DOCTRINE = _OW.CMD_CTOR_UNLOCK_DOCTRINE    # real ctor (documented; not called)


def make_unlock_doctrine(game, doctrine_ptr, tag_id=None):
    if tag_id is None:
        tag_id = player_tag_id(game)
    return build_by_vt(game, VT_UNLOCK_DOCTRINE, UNLOCK_DOCTRINE_SIZE, [
        (0x28, "<Q", int(doctrine_ptr) & 0xFFFFFFFFFFFFFFFF),
        (0x30, "<i", int(tag_id)),
    ])


# --- shared helpers --------------------------------------------------------
#
# A paused game does NOT drain the command queue (§29.8): enqueue() returns 1
# but the command just sits there. Every high-level write tool therefore has to
# briefly run the clock or its success read-back observes a frozen world.
CANEXEC_ADD_ADVISOR = _OW.CMD_CANEXEC_ADD_ADVISOR           # CAddAdvisorCommand::CanExecute (vtable +0x48)
CANEXEC_UNLOCK_DOCTRINE = _OW.CMD_CANEXEC_UNLOCK_DOCTRINE   # CUnlockGrandDoctrineCommand::CanExecute (+0x48)
VT_REMOVE_CONVEYOR_LINE = _OW.CMD_VT_REMOVE_CONVEYOR_LINE
CTOR_REMOVE_CONVEYOR_LINE = _OW.CMD_CTOR_REMOVE_CONVEYOR_LINE
CANEXEC_REMOVE_CONVEYOR_LINE = _OW.CMD_CANEXEC_REMOVE_CONVEYOR_LINE
SIZE_REMOVE_CONVEYOR_LINE = _OW.CMD_SIZE_REMOVE_CONVEYOR_LINE
VT_REMOVE_CONVEYOR = _OW.CMD_VT_REMOVE_CONVEYOR
CTOR_REMOVE_CONVEYOR = _OW.CMD_CTOR_REMOVE_CONVEYOR
CANEXEC_REMOVE_CONVEYOR = _OW.CMD_CANEXEC_REMOVE_CONVEYOR
SIZE_REMOVE_CONVEYOR = _OW.CMD_SIZE_REMOVE_CONVEYOR


def can_unlock_doctrine(game, doctrine_ptr, tag_id=None):
    """Ask the game's own CanExecute (0x1B10310) whether a grand doctrine can be
    assigned right now. Verified 2026-11-15: returns 1 for the 3 land doctrines
    whose prerequisites are met, and a game-state message for the other 9.
    After a successful unlock it flips to 0 (mutual exclusivity / already
    assigned), which is the reliable read-back -- CCountry+0x15E0 is a stale
    Linux offset and always reads empty on the PE build.
    """
    if tag_id is None:
        tag_id = player_tag_id(game)
    cmd = make_unlock_doctrine(game, doctrine_ptr, tag_id)
    try:
        return bool(game.call(game.p.base + CANEXEC_UNLOCK_DOCTRINE, (cmd, 0),
                              absolute=True, force=True) & 0xFF)
    except Exception:
        return None


def can_add_advisor(game, packed_handle, slot):
    """Ask the game's own CanExecute whether this advisor can be hired right now.

    Pure validation: builds an inert command and calls 0x11ECC20 with a NULL
    error sink. Verified 2026-11-15 across 12 advisors -- returns 1 for 11
    hireable ones and 0 for an occupied slot / already-hired / unmet trigger.
    After a successful hire it flips to 0 (the 'already hired' gate), which is
    the reliable read-back signal: advisors do NOT appear in active_ideas.
    """
    cmd = make_add_advisor(game, packed_handle, slot)
    try:
        return bool(game.call(game.p.base + CANEXEC_ADD_ADVISOR, (cmd, 0),
                              absolute=True, force=True) & 0xFF)
    except Exception:
        return None


def post_drained(game, cmd, seconds=2.0, done_fn=None):
    """enqueue + briefly run the clock, restoring the previous pause state.

    Returns {"posted", "was_paused", "drained", "date_before", "date_after"}.
    The pause byte is idler+0x6C1 (session.G_IDLER / IDLER_PAUSED); writing it
    is the PE-native resume path (session.pause does the same). A World-News
    popup can still hold the clock -- date_after == date_before reports that.

    2026-11-26 (P0-1 / P2-3 根治):
      1. 若游戏原本处于暂停态，**先在暂停态下完成 enqueue(game, cmd)** 再短暂解暂停！
         旧实现先解暂停并死睡 0.15s，让主线程及 16 个 TBB 线程并发执行
         `hourlyUpdateUnits (0x1EF640 -> 0x20D060 -> 0xDD7BC0 -> 0xC07CE7)` 时
         再用远线程向队列塞命令，造成竞态崩溃；先入队后解暂停彻底消除入队竞态。
      2. 轮询间隔压缩至 0.03s，并支持可选 `done_fn`（命令回读已生效即立刻恢复暂停），
         不再无条件死睡 0.15s + 0.15s + 0.10s，显著减少 4 速下的多余时钟推进。
    """
    import time as _t
    import session as S
    import win_pipeline as _wp
    _wp.ensure_live_version(game, only_if_live=True)
    p = game.p
    idler = p.u64(p.base + S.G_IDLER)
    if not idler:
        raise RuntimeError("G_IDLER is null; game session not active")
    was = bool(p.u8(idler + S.IDLER_PAUSED))
    d0 = None
    try:
        d0 = game.date_raw()
    except Exception as _de:
        import diag as _diag
        _diag.warn("cmdwin.post_drained.date_before", _de)

    # 关键修复：若游戏当前暂停，先在暂停态下安全入队，避免主循环/TBB 线程并发跑 tick 时竞态
    posted = enqueue(game, cmd)
    if not posted:
        return {"posted": posted, "was_paused": was, "drained": False,
                "date_before": d0, "date_after": d0}

    unpaused = False
    if was:
        p.write(idler + S.IDLER_PAUSED, b"\x00")
        unpaused = True
    d1 = None
    done_hit = False
    try:
        wait_max = max(0.15, float(seconds or 2.0))
        deadline = _t.monotonic() + wait_max
        while _t.monotonic() < deadline:
            _t.sleep(0.03)
            if done_fn is not None:
                try:
                    if done_fn():
                        done_hit = True
                        break
                except Exception:
                    pass
            if d0 is not None:
                try:
                    cur_d = game.date_raw()
                except Exception:
                    cur_d = None
                if cur_d is not None and cur_d != d0:
                    d1 = cur_d
                    if done_fn is None:
                        _t.sleep(0.04)      # 极短收尾确保同 tick 命令体落地
                        break
                    # 若调用方给了 done_fn，时钟已跳但状态尚未就绪时再最多多等一小段
                    try:
                        if done_fn():
                            done_hit = True
                            break
                    except Exception:
                        pass
    finally:
        if was and unpaused:
            try:
                p.write(idler + S.IDLER_PAUSED, b"\x01")
                _t.sleep(0.03)
            except Exception as _pe:
                import diag as _diag
                _diag.warn("cmdwin.post_drained.restore_pause", _pe)
    try:
        d1 = game.date_raw()
    except Exception as _de:
        import diag as _diag
        _diag.warn("cmdwin.post_drained.date_after", _de)
    drained = True if done_hit else ((d1 != d0) if (d0 is not None and d1 is not None) else None)
    return {"posted": posted, "was_paused": was,
            "drained": drained,
            "date_before": d0, "date_after": d1}


# CSetResearchCommand::CanExecute -- vtable 0x2A11548 slot +0x48.
CANEXEC_SET_RESEARCH = _OW.CMD_CANEXEC_SET_RESEARCH


def can_exec(game, cmd, fn_rva):
    """Ask the game's own CanExecute on an inert command object.

    Pure validation: it reads the command and the game state and returns a
    bool.  Every PE command path should call this before post_drained, because
    a refused command is silently dropped and looks exactly like success.
    """
    return bool(game.call(game.p.base + fn_rva, (cmd, 0),
                          absolute=True, force=True) & 0xFF)


# --- diplomacy -------------------------------------------------------------
#
# Declaring war is an action object wrapped in a command, not a plain command:
#
#   CDeclareWarAction        0x1180B70(this, int* from, int* to, CWarGoal*, bool, void* scopeOut)
#   CDiplomaticActionCommand 0x2A073C8, action pointer at +0x28, sender at +0x24
#   validator               0x11C2420(cmd, std::string* msgOut) -> bool
#
# Both objects come from the game allocator but the command's execute ends with
# `action->vt[0](action, 1)` -- the deleting destructor, which frees through the
# CRT.  That is the same failure that killed seven SetResearch attempts (§25), so
# both objects get a private vtable whose slot 0 does nothing.

CTOR_DECLARE_WAR = _OW.CMD_CTOR_DECLARE_WAR
VT_DECLARE_WAR = _OW.CMD_VT_DECLARE_WAR
VT_DIPLO_CMD = _OW.CMD_VT_DIPLO_CMD
FN_DIPLO_VALIDATE = _OW.CMD_FN_DIPLO_VALIDATE
# Filled into the scope struct the ctor forwards to CDiplomaticAction; the
# default ctor 0x1180CD0 uses exactly this triple.
SCOPE_MAGIC = _OW.CMD_SCOPE_MAGIC
# `CDeclareWarAction::IsAvailable` (0x11B9C40) can only return true through the
# early-out in 0x11B8460, which tests this byte.  Its real meaning is not
# established; it is exposed as an explicit knob and defaults to on because the
# validator rejects every action without it.
DECLARE_WAR_FLAG_OFF = _OW.CMD_DECLARE_WAR_FLAG_OFF
# CSetGameSpeedCommand -- 经 1.19.5.0 PE 反汇编彻底确认（2026-11-22）：
#   vtable    0x29F3A80    ctor  0xF84510(this, int speed)
#   size      0x30         (factory 0xF848F0 / clone 0xF84770: mov ecx, 0x30)
#   speed     +0x28 int32  (合法值 0..4；CanExecute 0xF85FB0: cmp [rcx+0x28], 4; setbe al)
#   Execute   0xF85BC0 -> 尾调用 CCurrentGameState::SetSpeed (0x203F10)，写入 [gs+0x4BC]
# 旧代码写成 size=0x28 且偏移 +0x24（Linux 布局），导致命令永远是空操作。
VT_SET_GAME_SPEED = _OW.CMD_VT_SET_GAME_SPEED
CTOR_SET_GAME_SPEED = _OW.CMD_CTOR_SET_GAME_SPEED
SIZE_SET_GAME_SPEED = _OW.CMD_SIZE_SET_GAME_SPEED
CANEXEC_SET_GAME_SPEED = _OW.CMD_CANEXEC_SET_GAME_SPEED
VT_INCREASE_GAME_SPEED = _OW.CMD_VT_INCREASE_GAME_SPEED


def make_declare_war(game, to_id, from_id=None, wargoal=0, force=True):
    p = game.p
    if from_id is None:
        from_id = player_tag_id(game)
    if not from_id:
        raise RuntimeError("not in a game yet (player tag is 0)")
    scope = alloc(game, 0x18)
    p.write(scope, struct.pack("<QQQ", 0, SCOPE_MAGIC, 0))
    act = alloc(game, 0x88)
    p.write(act, b"\0" * 0x88)
    game.call(p.base + CTOR_DECLARE_WAR,
              (act, _i32buf(game, from_id), _i32buf(game, to_id), wargoal, 1, scope),
              absolute=True, force=True)
    if force:
        p.write(act + DECLARE_WAR_FLAG_OFF, b"\x01")
    fake_vtable(game, VT_DECLARE_WAR, size=0x1D8)
    attach_fake_vtable(game, act, VT_DECLARE_WAR)

    cmd = alloc(game, 0x30)
    blob = bytearray(0x30)
    struct.pack_into("<Q", blob, 0x00, p.base + VT_DIPLO_CMD)
    for off, fmt, val in BASE_INIT:
        struct.pack_into(fmt, blob, off, val)
    struct.pack_into("<i", blob, 0x24, int(from_id))
    struct.pack_into("<Q", blob, 0x28, int(act))
    p.write(cmd, bytes(blob))
    attach_fake_vtable(game, cmd, VT_DIPLO_CMD)
    return {"act": act, "cmd": cmd, "from": from_id, "to": to_id}


# --- justify war goal (CGenerateWarGoalAction) -----------------------------
#
# Same two-object shell as CDeclareWarAction (§34, proven to declare real wars),
# with the action swapped for CGenerateWarGoalAction:
#
#   RTTI vtables: CGenerateWarGoalAction 0x2A079B8, CDiplomaticActionCommand 0x2A073C8
#   param ctor   0x1181120 (this, from*, to*, CWarGoalType* r9, ?, scopeOut*)
#   default ctor 0x1181220 (this)  -- builds every derived field internally:
#       +0x08 = 0x341B (action id)  +0x12 = 1
#       +0x58/+0x60 = action-definition lookups keyed by 0x341B
#       +0x90 = default CWarGoalType* (0xA880D0())
#       +0x14/+0x18 = from, +0x1C/+0x20 = to  (both 0 -- empty tags)
#       scope triple {0x27963D0, 0x29C77F8, 0x2796400}
#
# Calling the DEFAULT ctor is deliberately chosen over the param ctor: it takes
# only `this` (no stack args to get wrong) and fills the two registry lookups
# that a hand-built struct cannot fabricate. We then patch from/to, override the
# CWarGoalType* when the caller wants a specific goal, and set +0x70 (the same
# availability gate declare_war uses -- 0x11B8460 returns true as soon as it is
# nonzero, which is what lets the wrapped command's validator pass).
#
# Verified 2026-11-15: CGenerateWarGoalAction::IsAvailable (0x11BAD40) begins
# with the identical `call 0x11B8460; test al,al` early-out as CDeclareWarAction
# (0x11B9C40), so the +0x70 recipe transfers unchanged.
VT_GENERATE_WARGOAL = _OW.CMD_VT_GENERATE_WARGOAL
CTOR_GENERATE_WARGOAL_DEFAULT = _OW.CMD_CTOR_GENERATE_WARGOAL_DEFAULT
GENERATE_WARGOAL_SIZE = _OW.CMD_GENERATE_WARGOAL_SIZE     # ctor writes through +0x90


def make_justify_wargoal(game, to_id, wargoal_type_ptr=0, from_id=None, force=True):
    """Build CGenerateWarGoalAction + CDiplomaticActionCommand (not posted)."""
    p = game.p
    if from_id is None:
        from_id = player_tag_id(game)
    if not from_id:
        raise RuntimeError("not in a game yet (player tag is 0)")
    act = alloc(game, GENERATE_WARGOAL_SIZE)
    p.write(act, b"\0" * GENERATE_WARGOAL_SIZE)
    # default ctor takes only `this` -- unlike the param ctor it has NO stack
    # args to get wrong, and it builds the scope triple, both registry lookups
    # and the CWarGoal container itself. Ordinary game code, no injection risk.
    game.call(p.base + CTOR_GENERATE_WARGOAL_DEFAULT, (act,),
              absolute=True, force=True)
    # base ctor left from/to as empty tags (0) -> patch all four copies
    p.write(act + 0x14, struct.pack("<iiii", int(from_id), int(from_id),
                                     int(to_id), int(to_id)))
    if wargoal_type_ptr:
        p.write(act + 0x90, struct.pack("<Q", int(wargoal_type_ptr)))
    if force:
        p.write(act + DECLARE_WAR_FLAG_OFF, b"\x01")
    fake_vtable(game, VT_GENERATE_WARGOAL, size=0x1D8)
    attach_fake_vtable(game, act, VT_GENERATE_WARGOAL)

    # 0x30 is the size the PROVEN make_declare_war uses for this same wrapper.
    # pecmd guessed 0x88; a wrong size is exactly the class of value that has
    # killed the game three times, so mirror the working recipe instead.
    cmd = alloc(game, 0x30)
    blob = bytearray(0x30)
    struct.pack_into("<Q", blob, 0x00, p.base + VT_DIPLO_CMD)
    for off, fmt, val in BASE_INIT:
        struct.pack_into(fmt, blob, off, val)
    struct.pack_into("<i", blob, 0x24, int(from_id))
    struct.pack_into("<Q", blob, 0x28, int(act))
    p.write(cmd, bytes(blob))
    attach_fake_vtable(game, cmd, VT_DIPLO_CMD)
    return {"act": act, "cmd": cmd, "from": from_id, "to": to_id}


def justify_wargoal(game, to_id, wargoal_type_ptr=0, from_id=None, force=True,
                    seconds=2.0):
    """Post a war-goal justification. Read success back via hoi4_justifying.

    `seconds` is how long the clock runs after the enqueue.  The default 2.0 is
    what every early caller got; callers that have to touch the new
    `CTimedWargoalActivity` before its timer can fire pass something tiny
    (see game.justify_wargoal, crash #7 on 2026-11-19).
    """
    objs = make_justify_wargoal(game, to_id, wargoal_type_ptr, from_id, force)
    # A paused game never drains the queue, and the timed activity is created by
    # Execute on a later tick (§29.8) -- run the clock briefly, restore pause.
    drained = post_drained(game, objs["cmd"], seconds=seconds)
    return {"ok": bool(drained["posted"]), "posted": drained["posted"],
            "drained": drained.get("drained"), **objs}


def build_by_vt(game, vt_rva, size, fields=()):
    """Build a CCommand that has no ctor we trust: vtable + BASE_INIT + fields.

    `fields` is a sequence of (offset, struct-format, value).  The object is
    pointed at the no-free vtable copy so any drop path leaks instead of
    aborting; see `fake_vtable`.
    """
    p = game.p
    size = int(size)
    blob = bytearray(size)
    struct.pack_into("<Q", blob, 0x00, p.base + int(vt_rva))
    for off, fmt, val in BASE_INIT:
        if off + struct.calcsize(fmt) <= size:
            struct.pack_into(fmt, blob, off, val)
    for off, fmt, val in fields:
        struct.pack_into(fmt, blob, int(off), val)
    cmd = alloc(game, size)
    p.write(cmd, bytes(blob))
    attach_fake_vtable(game, cmd, int(vt_rva))
    return cmd


# --- generic command construction from the PE catalogue -------------------
#
# `pecmd` recovers {vtable, ctor, size, fields} for every C*Command class in
# hoi4.exe out of MSVC RTTI + the ctor itself.  That is what lets a command be
# built by name instead of by hand: the ctor is the authoritative description of
# the layout, and calling it also runs whatever base-class setup / embedded
# sub-object construction the class does (which a hand-filled struct misses --
# the conveyor and production-line commands are all of that shape).

ARG_TAGPTR = "tagptr"      # pointer to an int32 CCountryTag
ARG_PTR = "ptr"            # a plain pointer
ARG_INT = "int"            # an int32 value
ARG_HANDLEPTR = "handleptr"  # pointer to {0, u64 handle} -- these ctors read [rdx+8]
ARG_STRING = "string"      # pointer to a 32-byte MSVC std::string living in game memory

# How each command's ctor args map onto the names the tool layer uses.
# Only the classes the tools actually call need an entry; anything else falls
# back to filling the recovered field offsets directly.
ARGSPEC = {
    "CSetNationalFocusCommand": {"tag": 1, "focus_ptr": 2},
    "CDropCurrentNationalFocusCommand": {"tag": 1},
    "CSetContinuousFocusCommand": {"tag": 1, "focus_ptr": 2},
    "CDropContinuousFocusCommand": {"tag": 1},
    "CSetResearchCommand": {"tag": 1, "tech_ptr": 2, "slot": 3, "flag": 4},
    "CSelectDecisionCommand": {"tag": 1, "decision_ptr": 2},
    "CAddIdeaCommand": {"tag": 1, "idea_ptr": 2},
    "CRemoveIdeaCommand": {"tag": 1, "idea_ptr": 2},
    "CReplaceIdeaCommand": {"tag": 1, "old_ptr": 2, "new_ptr": 3},
    "CRemoveConstructionCommand": {"tag": 1, "index": 2},
    "CCreateConveyorCommand": {"template_handle": 1, "priority": 2},
    "CCreateConveyorExtendedCommand": {"template_handle": 1, "priority": 2,
                                       "lines": 3, "state_id": 4},
    "CSetConveyorLocationCommand": {"conveyor_handle": 1, "province_id": 2},
    "CSetConveyorPriorityCommand": {"conveyor_handle": 1, "priority": 2},
    "CSetConveyorSeriesCommand": {"conveyor_handle": 1, "series": 2},
    "CAddConveyorLineCommand": {"conveyor_handle": 1, "count": 2},
    "CRemoveConveyorLineCommand": {"line_handle": 1},
    "CDeployConveyorCommand": {"conveyor_handle": 1},
    "CRemoveConveyorCommand": {"conveyor_handle": 1},
    "CSetProductionLineAmountToProduceCommand": {"line": 1, "amount": 2},
    "CSetProductionLineCommand": {"line": 1, "variant": 2},
    "CAddProductionLineFactoriesCommand": {"line_id": 1, "amount": 2},
    # ctor 0x11CD270 -> +0x28 tag i32, +0x2C variant handle, +0x34 MIO handle,
    # +0x3C bool.  The bool is ctor ARGUMENT 4 (it travels on the stack), so the
    # indices here must stay contiguous 1..4: build_command walks `range(1, n+1)`
    # and appends whatever it finds, so a hole at 4 would pad a 0 into the bool
    # and push the real flag into a 6th slot the ctor never reads.  That is
    # exactly what happened first time round (flag_0x3c_before == 0).
    "CAddProductionLineCommand": {"tag": 1, "variant_handle": 2,
                                  "mio_handle": 3, "move_to_front": 4},
    "CRemoveProductionLineCommand": {"line_handle": 1},
    "CSetArmyLeaderCommand": {"arg0": 1, "arg1": 2, "arg2": 3},
    # 2026-11-23 (复测 6 P1-2): 经 PE 反汇编确认 CCreateUnitLeaderCommand:
    #   vt 0x2A12748, size 0x30, ctor 0x11CDF70(this, CCountryTag* tag, int engine_type)
    #   +0x28 i32 tag_id (>0), +0x2C i32 engine_type (0=field_marshal, 1=corps_commander, 2=admiral)
    #   CanExecute 0x11EE9D0 校验 engine_type in {0,1,2}、tag>0 及指挥点数 (CCountry+0x1F0)
    "CCreateUnitLeaderCommand": {"tag": 1, "leader_type": 2},
    "CUpdateDivisionTemplateCommand": {"arg0": 1, "arg1": 2},
    "CUnlockGrandDoctrineCommand": {"tag": 1, "doctrine_ptr": 2},
    "CSetGameSpeedCommand": {"speed": 1},
}
# per-argument kind, when it is not simply "matches the recovered field kind"
ARG_KIND = {
    "CSetNationalFocusCommand": {1: ARG_TAGPTR, 2: ARG_PTR},
    "CDropCurrentNationalFocusCommand": {1: ARG_TAGPTR},
    "CSetContinuousFocusCommand": {1: ARG_TAGPTR, 2: ARG_PTR},
    "CDropContinuousFocusCommand": {1: ARG_TAGPTR},
    "CSetResearchCommand": {1: ARG_TAGPTR, 2: ARG_PTR, 3: ARG_INT, 4: ARG_INT},
    "CSelectDecisionCommand": {1: ARG_TAGPTR, 2: ARG_PTR},
    "CAddIdeaCommand": {1: ARG_TAGPTR, 2: ARG_PTR},
    "CRemoveIdeaCommand": {1: ARG_TAGPTR, 2: ARG_PTR},
    "CReplaceIdeaCommand": {1: ARG_TAGPTR, 2: ARG_PTR, 3: ARG_PTR},
    "CRemoveConstructionCommand": {1: ARG_TAGPTR, 2: ARG_INT},
    "CCreateConveyorCommand": {1: ARG_HANDLEPTR, 2: ARG_INT},
    "CCreateConveyorExtendedCommand": {1: ARG_HANDLEPTR, 2: ARG_INT,
                                       3: ARG_INT, 4: ARG_INT},
    "CSetConveyorLocationCommand": {1: ARG_HANDLEPTR, 2: ARG_INT},
    "CSetConveyorPriorityCommand": {1: ARG_HANDLEPTR, 2: ARG_INT},
    "CSetConveyorSeriesCommand": {1: ARG_HANDLEPTR, 2: ARG_INT},
    "CAddConveyorLineCommand": {1: ARG_HANDLEPTR, 2: ARG_INT},
    "CRemoveConveyorLineCommand": {1: ARG_HANDLEPTR},
    "CDeployConveyorCommand": {1: ARG_HANDLEPTR},
    "CRemoveConveyorCommand": {1: ARG_HANDLEPTR},
    "CSetProductionLineAmountToProduceCommand": {1: ARG_HANDLEPTR, 2: ARG_INT},
    "CSetProductionLineCommand": {1: ARG_HANDLEPTR, 2: ARG_HANDLEPTR},
    "CAddProductionLineFactoriesCommand": {1: ARG_HANDLEPTR, 2: ARG_INT},
    "CAddProductionLineCommand": {1: ARG_TAGPTR, 2: ARG_HANDLEPTR,
                                  3: ARG_HANDLEPTR, 4: ARG_INT},
    "CRemoveProductionLineCommand": {1: ARG_HANDLEPTR},
    "CCreateUnitLeaderCommand": {1: ARG_TAGPTR, 2: ARG_INT},
    "CUnlockGrandDoctrineCommand": {1: ARG_TAGPTR, 2: ARG_PTR},
    "CSetGameSpeedCommand": {1: ARG_INT},
}


# Fields `pecmd.ctor_layout` cannot see.
#
# The recoverer tracks `this` by register, and `rcx` is in that set from the
# entry point onwards.  A ctor that stores through a *temporary* pointer
#     lea rcx, [rbx + 0x2c]        ; fieldptr[rcx] = 0x2c
#     mov qword ptr [rcx], rax     ; seen as `[this+0]`, not `[this+0x2c]`
# therefore loses the field: the parser takes the `br in this` branch and uses
# m.disp == 0, which it then swallows as another vtable store.  Whole classes of
# command write their handle arguments that way, so the recovered entry is
# incomplete rather than wrong -- vtable, ctor and size are all correct.
#
# Patch only what has been read out of the ctor by hand, and cite the ctor.
SPEC_PATCH = {
    # 0x11CD270 -> CAddProductionLineCommand, verified 2026-11-18:
    #   (this, int *countryTag /*rdx*/, CHandleOwner *a /*r8*/,
    #          CHandleOwner *b /*r9*/, bool moveToFront /*stack*/)
    #   +0x28 = *rdx            i32  country tag         (CanExecute: "Invalid Target" if <= 0)
    #   +0x2C = *(u64*)(r8+8)   handle  equipment variant (type 70)
    #   +0x34 = *(u64*)(r9+8)   handle  military-industrial organisation, {0,0} = none
    #   +0x3C = the stack bool  -> Execute::0x140ED6100(prod, line, 0) = move to top
    "CAddProductionLineCommand": dict(
        args=4,
        fields=[
            {"offset": 0x28, "size": 4, "arg": 1, "kind": "deref"},
            {"offset": 0x2C, "size": 8, "arg": 2, "kind": "handle"},
            {"offset": 0x34, "size": 8, "arg": 3, "kind": "handle"},
            {"offset": 0x3C, "size": 1, "arg": 4, "kind": "bool"},
        ]),
    # 0x11D0210 -> CRemoveProductionLineCommand (this, {u64 handle} *line)
    #   +0x28 = *(u64*)(rdx+8)  the production line handle (type 56/57)
    #   CanExecute 0x11F18B0 = 0x1411EC7A0(&+0x28) != 0, i.e. "handle is live".
    #   Execute 0x11E3260: resolve +0x28 -> line, [line+0x20] = owner country,
    #   [country+0xF68] = CProductionStatus, 0x140EE0170(prod, line, 0) = remove.
    #   Nothing else is touched, so a bad handle is refused, not dereferenced.
    "CRemoveProductionLineCommand": dict(
        args=1,
        fields=[{"offset": 0x28, "size": 8, "arg": 1, "kind": "handle"}]),
    # 0x11D1550 -> CSetProductionLineCommand (this, {u64 handle} *line, {u64 handle} *variant)
    #   vt 0x2A0FF88, size 0x38, CanExecute 0x11F2C30, Execute 0x11E6BE0
    #   +0x28 = *(u64*)(rdx+8)  production line handle (type 56)
    #   +0x30 = *(u64*)(r8+8)   equipment variant handle (type 70)
    #   CanExecute 解析双句柄并校验 variant->CanBeProducedBy(owner_tag) (0xC2CAD0)。
    "CSetProductionLineCommand": dict(
        vt=0x2A0FF88,
        ctor=0x11D1550,
        size=0x38,
        args=2,
        fields=[
            {"offset": 0x28, "size": 8, "arg": 1, "kind": "handle"},
            {"offset": 0x30, "size": 8, "arg": 2, "kind": "handle"},
        ]),
    # 0xF84510 -> CSetGameSpeedCommand (this, int speed), size 0x30 (factory 0xF848F0)
    #   +0x28 = edx (int32 speed, 0..4; CanExecute 0xF85FB0: cmp [rcx+0x28], 4; setbe al)
    #   Execute 0xF85BC0 -> CCurrentGameState::SetSpeed (0x203F10) 写入 [gs+0x4BC]。
    "CSetGameSpeedCommand": dict(
        vt=VT_SET_GAME_SPEED,
        ctor=CTOR_SET_GAME_SPEED,
        size=SIZE_SET_GAME_SPEED,
        args=1,
        fields=[{"offset": 0x28, "size": 4, "arg": 1, "kind": "int"}]),
    # 2026-11-27 (D-07 PE 1.19.5.0 ctor + RTTI 实证修正):
    #   CRemoveConveyorLineCommand: vt 0x2AC0850, ctor 0x1C2F020, size 0x30,
    #     CanExecute 0x1C31940, Execute 0x1C30D30; +0x28 接收单条 line (type 63) handle
    "CRemoveConveyorLineCommand": dict(
        vt=VT_REMOVE_CONVEYOR_LINE,
        ctor=CTOR_REMOVE_CONVEYOR_LINE,
        size=SIZE_REMOVE_CONVEYOR_LINE,
        args=1,
        fields=[{"offset": 0x28, "size": 8, "arg": 1, "kind": "handle"}]),
    #   CRemoveConveyorCommand: vt 0x2AC0610, ctor 0x1C2EF50, size 0x30,
    #     CanExecute 0x1C316E0, Execute 0x1C30CB0; +0x28 接收整条 conveyor (type 64) handle
    "CRemoveConveyorCommand": dict(
        vt=VT_REMOVE_CONVEYOR,
        ctor=CTOR_REMOVE_CONVEYOR,
        size=SIZE_REMOVE_CONVEYOR,
        args=1,
        fields=[{"offset": 0x28, "size": 8, "arg": 1, "kind": "handle"}]),
}


def spec_of(cls):
    """The PE catalogue entry for a command class (or short name)."""
    import pecmd
    s = dict(pecmd.get(cls))
    patch = SPEC_PATCH.get(s.get("class") or "")
    if patch:
        for k, v in patch.items():
            if k != "fields":
                s[k] = v
        s["fields"] = [dict(f) for f in patch["fields"]]
    return s


# Commands whose pecmd layout, vtable and construction path have actually been
# exercised on the 1.19.5.0 PE build without taking the process down.  Everything
# else is refused by CommandLayer.send_raw: a wrong vtable/size turns the command
# object into something the engine executes as code, and that is unrecoverable.
# 2026-11-15: CAddAdvisorCommand (guessed ctor 0x11D2B40), CGenerateWarGoalAction
# and CUnlockGrandDoctrineCommand each killed hoi4.exe this way.
WIN_SAFE = {
    "CSetNationalFocusCommand",
    "CDropCurrentNationalFocusCommand",
    "CSetContinuousFocusCommand",
    "CDropContinuousFocusCommand",
    "CSetResearchCommand",
    "CSelectDecisionCommand",
    "CAddIdeaCommand",
    "CRemoveIdeaCommand",
    "CReplaceIdeaCommand",
    "CRemoveConstructionCommand",
    "CCreateConveyorCommand",
    "CCreateConveyorExtendedCommand",
    "CSetConveyorLocationCommand",
    "CSetConveyorPriorityCommand",
    "CSetConveyorSeriesCommand",
    "CAddConveyorLineCommand",
    "CRemoveConveyorLineCommand",
    "CDeployConveyorCommand",
    "CRemoveConveyorCommand",
    "CSetProductionLineAmountToProduceCommand",
    "CSetProductionLineCommand",
    "CAddProductionLineFactoriesCommand",
    # --- verified 2026-11-18 by full PE disassembly + live read-back --------
    # CAddProductionLineCommand  vt 0x2A0FBC8 size 0x40 ctor 0x11CD270
    #   (this, int *tag, {u64 handle} *variant, {u64 handle} *mio, bool front)
    #   +0x28 i32 country tag, +0x2C variant handle, +0x34 MIO handle,
    #   +0x3C u8 "move the new line to the top".
    #   CanExecute 0x11EDB10: +0x28 > 0 ("Invalid Target"), handle 0x2C must be
    #     non-zero and resolve, variant->CanBeProducedBy(tag) (0x140C2CAD0),
    #     then the MIO cross-check only when 0x34 != {0,0}.
    #   Execute 0x11DC130: [ *(CCountry[+0x28]) + 0xF68 ] = CProductionStatus,
    #     then 0x140ED0FF0(prod, variant) finds-or-creates the line and
    #     0x140ED6100(prod, line, 0) reorders it to index 0 when +0x3C is set.
    #     (+0xF68 is the same object game.CO_PRODUCTION reads; +0x58/+0x64 are
    #     the military line array data/count, matching _arr(prod, PS_MIL_LINES).)
    #   The auto catalog reported args=1 and only +0x28 -- see SPEC_PATCH.
    "CAddProductionLineCommand",
    # CRemoveProductionLineCommand  vt 0x2A10048 size 0x30 ctor 0x11D0210
    #   (this, {u64 handle} *line) -> +0x28 line handle only; no country tag.
    #   Was in ARGSPEC but never in this list, so hoi4_remove_production_line
    #   silently did nothing for weeks (send_raw refused, the caller only saw
    #   before == after and reported ok:false with no reason).
    "CRemoveProductionLineCommand",
    "CSetArmyLeaderCommand",
    "CCreateUnitLeaderCommand",
    # --- verified 2026-11-15 by full PE disassembly + live read-back --------
    # CAddAdvisorCommand:        vt 0x2A11A88 size 0x50, real ctor 0x11CC6D0
    #   (pecmd's 0x11D2F00 was the DELETING DESTRUCTOR -> crash #1). Field
    #   +0x28 = packed CCharacter handle (Linux used +0x24), +0x30 = slot string.
    #   Object is byte-identical to the real ctor's output; CanExecute flip
    #   1->0 proved a real hire (Manstein theorist, Kesselring air_chief).
    "CAddAdvisorCommand",
    # CUnlockGrandDoctrineCommand: vt 0x2AAA8A0 size 0x38, ctor 0x1B0FD20.
    #   +0x28 = CGrandDoctrineTemplate*, +0x30 = country tag id (old path left
    #   it 0 -> CanExecute "invalid country tag" / Execute null deref = crash #3).
    #   superior_firepower assigned: its CanExecute flipped 1->0 and the other
    #   land doctrines became mutually-exclusive.
    "CUnlockGrandDoctrineCommand",
    # CGenerateWarGoalAction + CDiplomaticActionCommand: same proven two-object
    #   shell as declare_war (§34). Default ctor 0x1181220, vtable 0x2A079B8,
    #   size 0xA0. Live result: a CTimedWargoalActivity with from=GER(1),
    #   target=SIA(62), 160-day timer -- verified by hoi4_justifying.
    "CGenerateWarGoalAction",
    # --- verified 2026-11-16 by PE disassembly + live read-back ------------
    # Air layer (calling/airops_win.py). Both are built with build_by_vt (no
    # ctor call) and go through their own CanExecute before post_drained:
    #   CAttachAirWingToArmyCommand  vt 0x2A97728 size 0x48,
    #     CanExecute 0x19D3920, +0x28 array of wing handles, +0x40 army handle.
    #     12 German wings attached round-robin to 6 armies; id 1 refused
    #     (already attached) and id 15 (nav_bomber) correctly rejected.
    "CAttachAirWingToArmyCommand",
    #   CStratAirSetMissionCommand   vt 0x2A96BE8 size 0x48,
    #     CanExecute 0x19D4B30, +0x28 array, +0x40 mission mask i32,
    #     +0x44/45/46 flags. Live: wing +0xA0 mission mask 0 -> 1.
    #   CStratAirEnableMissionCommand is DELIBERATELY NOT whitelisted: its
    #     CanExecute (0x19D4740) resolves a SECOND handle array at +0x40 as
    #     CStrategicAir objects; feeding it a CAirWing handle killed the game
    #     (crash #4). airops_win.make_enable_mission raises instead.
    "CStratAirSetMissionCommand",
    # --- verified 2026-11-26 by full PE disassembly --------------------------
    # CStratAirTransferCommand: vt 0x2A96CA8, ctor 0x19CCCF0, clone 0x19CF3F0,
    #   size 0x48 (flat scalars, no heap arrays), CanExecute 0x19D4CC0, Execute 0x19D2090.
    #   +0x28 u64 wing_handle (type 69), +0x30 u64 target_airbase_handle (type 65),
    #   +0x38 u8 to_reserve (0=rebase, 1=reserve), +0x39 u8 keep_army (0=detach),
    #   +0x3C i32 region_id (0=unchanged), +0x40 i32 mission_mask (0=unchanged).
    "CStratAirTransferCommand",
    #   CUpdateDivisionTemplateCommand  vt 0x2A26E38 size 0x2B0,
    #     CanExecute 0x1C2DD90, Execute 0x1C2D040. +0x28 = shallow copy of the
    #     live CDivisionTemplateData, +0x270 template handle, +0x278 tag,
    #     +0x280 XP cost. Verified: Infanterie-Division 9 -> 10 infantry via
    #     hoi4_template_edit, hoi4_template_composition read back {infantry:10}
    #     and the engine deducted its own army XP (749994 -> 429987).
    #     CanExecute refuses edits the country cannot afford (e.g. +1
    #     artillery_brigade costs 2.5 XP) instead of crashing.
    "CUpdateDivisionTemplateCommand",
    # --- verified 2026-11-16 by PE disassembly + live read-back ------------
    # Naval layer (calling/navalops_win.py). Both built with build_by_vt.
    #   CNavalMissionSetTypeCommand:   vt 0x2A2A808 size 0x50,
    #     CanExecute 0x13EE8C0 / Execute 0x13E6C40. +0x28 CPdxArray of packed
    #     CTaskForce handles (tf+0x18, type 0x33), +0x40 mission 0..9,
    #     +0x44/45 flags. Live: tf+0x374 0->1 (Patrol), then 1->3
    #     (Convoy Raiding), CNavalMission vptr switched to 0x29FB658.
    "CNavalMissionSetTypeCommand",
    #   CNavalMissionAddRegionCommand: vt 0x2A2A988 size 0x38,
    #     CanExecute 0x13EDF70 / Execute 0x13E5FE0. +0x28 packed CFleet handle
    #     (fleet+0x08, type 0x3D), +0x30 region id. Live: fleet+0x54 0->1->2
    #     and +0x48 array gained the region-9 / region-18 objects.
    #   CRASH #5: CanExecute's tail 0x140DBA393 dereferences [region+0xA0]
    #     with NO null check if the region id is out of range / non-naval.
    #     navalops_win.region_is_valid() gates EVERY id before CanExecute.
    "CNavalMissionAddRegionCommand",
    #   CNavalMissionRemoveRegionCommand: vt 0x2A2AA48 size 0x48,
    #     CanExecute 0x13EE560 / Execute 0x13E6570. +0x28 packed CFleet handle,
    #     +0x30 CPdxArray<i32> of region ids (cap +0x38 / size +0x3c / alloc
    #     +0x40). Live 2026-11-16: removing region 9 took fleet+0x54 2->1 and
    #     dropped the region-9 object from the +0x48 array. Ids are gated by the
    #     same region_is_valid() guard; CanExecute also requires the region to
    #     already be assigned (it only compares pointers, no [r8+0xa0] deref).
    "CNavalMissionRemoveRegionCommand",
    # CNavalMissionRemoveRegionCommand used to be excluded; now proven.
    # --- verified 2026-11-22 by full PE disassembly + live read-back --------
    # CSetGameSpeedCommand: vt 0x29F3A80, ctor 0xF84510, size 0x30,
    #   +0x28 int32 speed (0..4; CanExecute 0xF85FB0 checks unsigned <= 4;
    #   Execute 0xF85BC0 -> CCurrentGameState::SetSpeed 0x203F10 -> [gs+0x4BC]).
    "CSetGameSpeedCommand",
}


def win_safe(cls):
    """True when this command may be built and posted on the PE backend."""
    try:
        name = spec_of(cls)["class"]
    except Exception:
        name = str(cls)
    return name in WIN_SAFE


def _handle_block(game, packed):
    """A 16-byte block whose +0x08 is the game's 8-byte {type,id} handle."""
    b = alloc(game, 0x10)
    game.p.write(b, struct.pack("<QQ", 0, int(packed) & 0xFFFFFFFFFFFFFFFF))
    return b


def _game_string(game, text):
    """A 32-byte MSVC std::string in game memory.

    Layout: [+0x00] 16-byte SSO buffer *or* heap pointer, [+0x10] size,
    [+0x18] capacity.  Short strings live inside the object itself, which is
    what the game's own default ctor produces (pecmd sees capacity 0xf at +0x18).
    """
    b = text.encode("utf-8") if isinstance(text, str) else bytes(text)
    n = len(b)
    sp = alloc(game, 0x20)
    if n <= 15:
        game.p.write(sp, b.ljust(16, b"\0") + struct.pack("<QQ", n, 15))
    else:
        data = alloc(game, n + 1)
        game.p.write(data, b + b"\0")
        # MSVC std::string is 32 bytes: +0x00 ptr, +0x08 union pad, +0x10 size, +0x18 cap
        game.p.write(sp, struct.pack("<QQQQ", data, 0, n, n))
    return sp


def build_command(game, cls, fields):
    """Allocate + construct + vtable-patch one command, by class name.

    Uses the class's real ctor whenever the argument list is known
    (`ARGSPEC`), and otherwise writes the fields `pecmd` recovered.
    """
    p = game.p
    spec = spec_of(cls)
    name = spec["class"]
    amap = ARGSPEC.get(name)
    kinds = ARG_KIND.get(name, {})
    if amap:
        n = max(list(amap.values()) + [spec["args"]])
        vals = [None] * (n + 1)
        for kw, idx in amap.items():
            if kw in fields:
                vals[idx] = fields[kw]
        args = []
        for i in range(1, n + 1):
            v = vals[i]
            kind = kinds.get(i, ARG_INT)
            if kind == ARG_TAGPTR:
                if v is None:
                    v = player_tag_id(game)
                args.append(_i32buf(game, int(v)))
            elif kind == ARG_HANDLEPTR:
                if v is None:
                    raise ValueError("%s needs %s" % (name, amap and
                                                      [k for k, j in amap.items() if j == i]))
                args.append(_handle_block(game, v))
            elif kind == ARG_STRING:
                if v is None:
                    raise ValueError("%s needs %s" % (name, amap and
                                                      [k for k, j in amap.items() if j == i]))
                args.append(_game_string(game, v))
            elif kind == ARG_PTR:
                args.append(int(v or 0))
            else:
                args.append(int(v or 0))
        size = spec["size"]
        cmd = alloc(game, size)
        game.call(p.base + spec["ctor"], tuple([cmd] + args),
                  absolute=True, force=True)
        got = p.u64(cmd)
        if got != p.base + spec["vt"]:
            raise RuntimeError("%s ctor produced vptr %#x, expected %#x"
                               % (name, got, p.base + spec["vt"]))
        attach_fake_vtable(game, cmd, spec["vt"])
        return cmd
    # no argument map: write the recovered offsets
    out = []
    for f in spec["fields"]:
        if f["arg"] is None:
            continue
        out.append((f["offset"], {8: "<Q", 4: "<i", 2: "<h", 1: "<B"}[f["size"]],
                    int(fields.get("arg%d" % f["arg"], 0))))
    return build_by_vt(game, spec["vt"], spec["size"], out)


def dispatch(game, cls, fields):
    """Build a command by class name and put it on the game's queue."""
    cmd = build_command(game, cls, fields)
    return {"ok": bool(enqueue(game, cmd)), "cmd": cmd}


# CDropCurrentNationalFocusCommand -- recovered 2026-11-15 from the vtable, not
# from a table lookup: RTTI says .?AVCDropCurrentNationalFocusCommand@@ ->
# 0x2A12988, and scanning .text for `lea reg,[rip+vtable]` gives exactly three
# hits: the ctor 0x11CE6F0, the clone 0x11D56A0 and a size-0x30 factory 0x11D8D20.
# The old 29376048 (0x1C03F30) was a stale Linux-derived number; calling it with
# (cmd, tagp) is what took the process down.
#
#     0x11CE6F0  (rcx = cmd, rdx = CCountryTag*)        size 0x30
#       +0x08 byte 0   +0x0C -1   +0x10 0   +0x18 0xFFFF   +0x1C 0
#       +0x28 int32 = *tag
#       [rcx] = 0x2A12988
#
# Same 12-byte shape as CSetNationalFocusCommand (0x11D0DC0 -> 0x2A128C8) minus
# the CNationalFocus* argument: the command carries only "who", so dropping is
# "clear whatever that country has selected".
CTOR_DROP_NATIONAL_FOCUS = _OW.CMD_CTOR_DROP_NATIONAL_FOCUS   # (cmd, CCountryTag*) -> CDropCurrentNationalFocusCommand
VT_DROP_NATIONAL_FOCUS = _OW.CMD_VT_DROP_NATIONAL_FOCUS       # .?AVCDropCurrentNationalFocusCommand@@
SIZE_DROP_NATIONAL_FOCUS = _OW.CMD_SIZE_DROP_NATIONAL_FOCUS


def make_drop_national_focus(game, tag_id):
    p = game.p
    cmd = alloc(game, SIZE_DROP_NATIONAL_FOCUS)
    tagp = _tag_block(game, tag_id)
    game.call(p.base + CTOR_DROP_NATIONAL_FOCUS, (cmd, tagp),
              absolute=True, force=True)
    got_vt, got_tag = p.u64(cmd), p.i32(cmd + 0x28)
    if got_vt != p.base + VT_DROP_NATIONAL_FOCUS or got_tag != tag_id:
        raise RuntimeError("CDropCurrentNationalFocusCommand ctor did not "
                           "produce the expected object (vptr=%#x tag=%d)"
                           % (got_vt, got_tag))
    attach_fake_vtable(game, cmd, VT_DROP_NATIONAL_FOCUS)
    return cmd


def drop_national_focus(game, tag_id=None):
    """Cancel the focus in progress (`CDropCurrentNationalFocusCommand`)."""
    if tag_id is None:
        tag_id = int(game.player_tag_id())
    if not tag_id:
        raise RuntimeError("not in a game yet (player tag is 0)")
    cur = game.current_focus()
    if not cur:
        return {"ok": False, "posted": 0, "reason": "no focus in progress"}
    cmd = make_drop_national_focus(game, tag_id)
    ok = enqueue(game, cmd)
    return {"ok": bool(ok), "cmd": cmd, "posted": ok, "was": cur.get("key")}


_DEFAULT_SPEED_SECONDS = (2.0, 0.5, 0.2, 0.1, 0.0)


def game_speed_seconds(game):
    """读取引擎 NGame::GAME_SPEED_SECONDS（每小时对应真实秒数，5 档 float）。"""
    p = game.p
    rva = getattr(O, "G_GAME_SPEED_SECONDS", None)
    if rva:
        try:
            arr_ptr = p.u64(p.base + rva)
            arr_sz = p.i32(p.base + rva + 0x0C)
            if 0x10000 < arr_ptr < 0x7F0000000000 and 1 <= arr_sz <= 16:
                raw = p.read(arr_ptr, 4 * arr_sz)
                vals = struct.unpack("<%df" % arr_sz, raw)
                if all(0.0 <= v <= 60.0 for v in vals):
                    return tuple(round(float(v), 4) for v in vals)
        except Exception:
            pass
    return _DEFAULT_SPEED_SECONDS


def get_game_speed(game):
    """纯只读获取当前引擎速度档位（CCurrentGameState+0x4BC，范围 0..4）。"""
    p = game.p
    gs = p.u64(p.base + O.G_GAMESTATE)
    if not gs:
        return None
    off = getattr(O, "GS_GAME_SPEED", 0x4BC)
    return int(p.i32(gs + off))


# 工具层允许设置的最高引擎速度档位：3 = UI 4 速（GAME_SPEED_SECONDS[3]=0.1s/h，约 0.42 天/秒）。
# 引擎 4 档（UI 5 速，0.0s/h）无节流上限、吃满单核 CPU，会冲穿红点急刹守护与事件窗口，故工具层硬禁。
MAX_TOOL_ENGINE_SPEED = 3


def make_set_game_speed(game, speed):
    """构造真实的 CSetGameSpeedCommand（size=0x30, +0x28=speed 0..3；超 3 强制钳位到 UI 4 速）。"""
    speed = int(speed)
    if not 0 <= speed <= 5:
        raise ValueError("speed must be between 0 and 5")
    engine_speed = min(speed, MAX_TOOL_ENGINE_SPEED)
    return build_by_vt(game, VT_SET_GAME_SPEED, SIZE_SET_GAME_SPEED,
                       [(0x28, "<i", engine_speed)])


def set_game_speed(game, speed, measure_seconds=None, use_command=False):
    """设置游戏速度（工具层最高允许 engine 3 = UI 4 速；传入 4/5 自动钳位至 3），并强制回读验证。

    2026-11-22 根因修复：
      1. 引擎真实速度位于 CCurrentGameState+0x4BC（int32，0..4，对应 UI 1~5 格），
         主循环 Tick 间隔计算 (0x206755) 与 HUD 速度条 (0x192FC41) 均直读该字段。
      2. 旧版 CSetGameSpeedCommand 错写成 size=0x28、偏移 +0x24（实为 size=0x30、
         偏移 +0x28，且 CanExecute 0xF85FB0 校验 unsigned <= 4），导致命令为空操作。
      3. 默认直接写 CCurrentGameState+0x4BC（0 注入、无需前台、暂停态即刻生效，
         与 session.pause 写 idler+0x6C1 一致），写后 80ms 回读确认不回弹。
      4. 诚实回执：当游戏正在运行（未暂停）且未显式禁用测速（measure_seconds=0）时，
         默认采样 2.2s 返回实测速率 measured_rate（天/秒）；暂停态默认不偷跑时钟，
         显式传入 measure_seconds > 0 时可短跑采样并严格恢复暂停态。
    2026-11-23 硬上限锁四速：
      5. UI 5 速（engine_speed=4）对应 GAME_SPEED_SECONDS[4]=0.0（无节流上限、单核跑满），
         会导致红点急刹/wait_for 拉停前已白跑数天。工具层统一钳位上限为 MAX_TOOL_ENGINE_SPEED=3
         （UI 4 速，0.1s/h 固定节流），绝不允许工具开启 5 速。
    """
    import time as _t
    import session as _sess

    requested = int(speed)
    if not 0 <= requested <= 5:
        raise ValueError("speed must be between 0 and 5")
    engine_speed = min(requested, MAX_TOOL_ENGINE_SPEED)
    clamped = (engine_speed != requested)

    p = game.p
    import win_pipeline as _wp
    _wp.ensure_live_version(game, only_if_live=True)
    gs = p.u64(p.base + O.G_GAMESTATE)
    if not gs:
        return {
            "ok": False,
            "verified": False,
            "requested": requested,
            "speed": engine_speed,
            "ui_speed": engine_speed + 1,
            "max_engine_speed": MAX_TOOL_ENGINE_SPEED,
            "max_ui_speed": MAX_TOOL_ENGINE_SPEED + 1,
            "error": "no CCurrentGameState (not in a game yet)",
        }

    off = getattr(O, "GS_GAME_SPEED", 0x4BC)
    speed_before = int(p.i32(gs + off))
    posted = 0
    method = "gamestate_direct"
    cmd_error = None

    if use_command:
        method = "command"
        cmd = make_set_game_speed(game, engine_speed)
        can = can_exec(game, cmd, CANEXEC_SET_GAME_SPEED)
        if not can:
            return {
                "ok": False,
                "verified": False,
                "method": method,
                "requested": requested,
                "speed": engine_speed,
                "ui_speed": engine_speed + 1,
                "speed_before": speed_before,
                "speed_after": speed_before,
                "can_execute": False,
                "posted": 0,
                "error": "CSetGameSpeedCommand::CanExecute refused",
            }
        dr = post_drained(game, cmd)
        posted = int(bool(dr.get("posted")))
        if not posted:
            cmd_error = "CSetGameSpeedCommand failed to post"
    else:
        p.write(gs + off, struct.pack("<i", engine_speed))

    _t.sleep(0.08)
    speed_after = int(p.i32(gs + off))
    mem_ok = (speed_after == engine_speed) and (cmd_error is None)

    tbl = game_speed_seconds(game)
    sec_per_hour = tbl[engine_speed] if 0 <= engine_speed < len(tbl) else None
    theo_dps = (round(1.0 / (sec_per_hour * 24.0), 4)
                if (sec_per_hour is not None and sec_per_hour > 0) else None)

    try:
        was_paused = bool(_sess.is_paused(game))
    except Exception:
        was_paused = None

    # 决定是否执行时钟实测：
    # - 显式传 measure_seconds=0 -> 不测速；
    # - 显式传 measure_seconds>0 -> 按指定秒数测速（上限 20s，暂停态则临时解暂停并在 finally 恢复）；
    # - 未传（None）-> 仅在当前已经处于运行态（未暂停）时自动采 2.2s，绝不擅自偷跑暂停存档。
    if measure_seconds is None:
        m_sec = 2.2 if (mem_ok and was_paused is False) else 0.0
    else:
        m_sec = max(0.0, min(float(measure_seconds), 20.0))

    measured_rate = None
    measured_hours = None
    measured_actual_s = None
    measure_note = None
    verify_mode = "memory_readback"

    if mem_ok and m_sec > 0.0:
        idler = 0
        temp_unpaused = False
        try:
            if was_paused:
                idler = p.u64(p.base + _sess.G_IDLER)
                if idler:
                    p.write(idler + _sess.IDLER_PAUSED, b"\x00")
                    temp_unpaused = True
                    _t.sleep(0.05)
            h0 = int(game.date_raw())
            t0 = _t.monotonic()
            _t.sleep(m_sec)
            h1 = int(game.date_raw())
            dt = _t.monotonic() - t0
            measured_hours = int(h1 - h0)
            measured_actual_s = round(dt, 3)
            measured_rate = round((measured_hours / 24.0) / max(dt, 1e-6), 4)
            verify_mode = "memory_and_clock"
        except Exception as e:
            measure_note = "clock measurement error: %s" % e
        finally:
            if temp_unpaused and idler:
                try:
                    p.write(idler + _sess.IDLER_PAUSED, b"\x01")
                except Exception as _pe:
                    import diag as _diag
                    _diag.warn("cmdwin.set_game_speed.restore_pause", _pe)
    elif mem_ok and was_paused:
        measure_note = ("game is paused; speed verified via CCurrentGameState+0x4BC "
                        "readback without advancing the clock (pass measure_seconds>0 "
                        "or unpause to sample live days/sec)")

    out = {
        "ok": bool(mem_ok),
        "verified": bool(mem_ok),
        "verify_mode": verify_mode,
        "method": method,
        "requested": requested,
        "speed": speed_after,
        "engine_speed": engine_speed,
        "ui_speed": speed_after + 1 if 0 <= speed_after <= 4 else None,
        "max_engine_speed": MAX_TOOL_ENGINE_SPEED,
        "max_ui_speed": MAX_TOOL_ENGINE_SPEED + 1,
        "clamped": clamped,
        "speed_before": speed_before,
        "speed_after": speed_after,
        "changed": speed_after != speed_before,
        "seconds_per_hour": sec_per_hour,
        "speed_cap_mode": ("unlimited" if sec_per_hour == 0.0 else "fixed_interval")
                          if sec_per_hour is not None else None,
        "theoretical_days_per_second": theo_dps,
        "paused": was_paused,
        "measured_rate": measured_rate,
        "measured_hours": measured_hours,
        "measured_seconds": measured_actual_s,
        "posted": posted,
    }
    if sec_per_hour == 0.0:
        out["speed_note"] = (
            "UI speed 5 (engine_speed=4) runs uncapped (seconds_per_hour=0.0 in "
            "NGame::GAME_SPEED_SECONDS), so theoretical_days_per_second is null and "
            "measured_rate reflects actual CPU speed."
        )
    elif clamped:
        out["speed_note"] = (
            "Tool speed cap enforced: max allowed is UI speed 4 (engine_speed=3, "
            "fixed_interval 0.1s/hour). UI speed 5 (engine_speed=4) is disabled "
            "because it runs uncapped on CPU and overshoots red-dot brakes."
        )
    if clamped:
        out["note"] = ("tool max game speed is capped at engine speed %d (UI speed %d); "
                       "requested %d clamped to %d (UI speed 5 is disabled)"
                       % (MAX_TOOL_ENGINE_SPEED, MAX_TOOL_ENGINE_SPEED + 1,
                          requested, engine_speed))
    if measure_note:
        out["measure_note"] = measure_note
    if not mem_ok:
        out["error"] = cmd_error or (
            "wrote CCurrentGameState+0x4BC=%d but read back %d"
            % (engine_speed, speed_after)
        )
    return out


def validate_declare_war(game, cmd):
    """Ask the game's own validator.  msgOut must be NULL, exactly as Execute calls it."""
    return game.call(game.p.base + FN_DIPLO_VALIDATE, (cmd, 0),
                     absolute=True, force=True) & 0xFF


def declare_war(game, to_id, from_id=None, wargoal=0, force=True, validate=False):
    """Declare war.  Verified live: the game raised its own war notification.

    `validate=True` additionally calls the game's checker first.  It is off by
    default because that call has been observed to take the process down when the
    action carries the patched vtable; the command's execute runs the same check
    internally anyway, so skipping it changes nothing about the outcome.
    """
    objs = make_declare_war(game, to_id, from_id=from_id, wargoal=wargoal, force=force)
    valid = validate_declare_war(game, objs["cmd"]) if validate else None
    if validate and not valid:
        return {"ok": False, "valid": False, "posted": 0, **objs}
    posted = enqueue(game, objs["cmd"])
    return {"ok": bool(posted), "valid": valid, "posted": posted, **objs}


VT_SET_NATIONAL_FOCUS = _OW.CMD_VT_SET_NATIONAL_FOCUS
VT_SET_RESEARCH = _OW.CMD_VT_SET_RESEARCH
VT_SET_PROD_LINE = _OW.CMD_VT_SET_PROD_LINE
CTOR_SET_PROD_LINE = _OW.CMD_CTOR_SET_PROD_LINE
CANEXEC_SET_PROD_LINE = _OW.CMD_CANEXEC_SET_PROD_LINE
SIZE_SET_PROD_LINE = _OW.CMD_SIZE_SET_PROD_LINE


def _apply_profile():
    """第二期数据驱动覆盖：从 calling/profile.py 加载当前 PE 版本的命令地址与布局。"""
    global FN_POST, OP_NEW, FN_NOOP_DTOR
    global CTOR_SET_NATIONAL_FOCUS, VT_SET_NATIONAL_FOCUS
    global CTOR_SET_RESEARCH, VT_SET_RESEARCH, CANEXEC_SET_RESEARCH
    global CTOR_ADD_CONSTRUCTION, VT_ADD_CONSTRUCTION, VT_BUILDING_REF
    global VT_ADD_ADVISOR, ADD_ADVISOR_SIZE, CTOR_ADD_ADVISOR, CANEXEC_ADD_ADVISOR
    global VT_UNLOCK_DOCTRINE, UNLOCK_DOCTRINE_SIZE, CTOR_UNLOCK_DOCTRINE, CANEXEC_UNLOCK_DOCTRINE
    global VT_DECLARE_WAR, VT_DIPLO_CMD, FN_DIPLO_VALIDATE, VT_GENERATE_WARGOAL
    global VT_SET_GAME_SPEED, CTOR_SET_GAME_SPEED, SIZE_SET_GAME_SPEED, CANEXEC_SET_GAME_SPEED
    global VT_INCREASE_GAME_SPEED
    global CTOR_DROP_NATIONAL_FOCUS, VT_DROP_NATIONAL_FOCUS, SIZE_DROP_NATIONAL_FOCUS
    global VT_SET_PROD_LINE, CTOR_SET_PROD_LINE, CANEXEC_SET_PROD_LINE, SIZE_SET_PROD_LINE
    try:
        import profile as _prof
        if not _prof.load():
            return
        FN_POST = _prof.anchor("FN_POST", FN_POST)
        OP_NEW = _prof.anchor("OP_NEW", OP_NEW)
        FN_NOOP_DTOR = _prof.anchor("FN_NOOP_DTOR", FN_NOOP_DTOR)
        VT_BUILDING_REF = _prof.anchor("VT_BUILDING_REF", VT_BUILDING_REF)
        VT_DECLARE_WAR = _prof.anchor("VT_DECLARE_WAR", VT_DECLARE_WAR)
        VT_DIPLO_CMD = _prof.anchor("VT_DIPLO_CMD", VT_DIPLO_CMD)
        FN_DIPLO_VALIDATE = _prof.anchor("FN_DIPLO_VALIDATE", FN_DIPLO_VALIDATE)
        VT_GENERATE_WARGOAL = _prof.anchor("VT_GENERATE_WARGOAL", VT_GENERATE_WARGOAL)

        VT_SET_NATIONAL_FOCUS = _prof.command_field("CSetNationalFocusCommand", "vt", VT_SET_NATIONAL_FOCUS)
        CTOR_SET_NATIONAL_FOCUS = _prof.command_field("CSetNationalFocusCommand", "ctor", CTOR_SET_NATIONAL_FOCUS)

        VT_DROP_NATIONAL_FOCUS = _prof.command_field("CDropCurrentNationalFocusCommand", "vt", VT_DROP_NATIONAL_FOCUS)
        CTOR_DROP_NATIONAL_FOCUS = _prof.command_field("CDropCurrentNationalFocusCommand", "ctor", CTOR_DROP_NATIONAL_FOCUS)
        SIZE_DROP_NATIONAL_FOCUS = _prof.command_field("CDropCurrentNationalFocusCommand", "size", SIZE_DROP_NATIONAL_FOCUS)

        VT_SET_RESEARCH = _prof.command_field("CSetResearchCommand", "vt", VT_SET_RESEARCH)
        CTOR_SET_RESEARCH = _prof.command_field("CSetResearchCommand", "ctor", CTOR_SET_RESEARCH)
        CANEXEC_SET_RESEARCH = _prof.command_field("CSetResearchCommand", "can_execute", CANEXEC_SET_RESEARCH)

        VT_ADD_CONSTRUCTION = _prof.command_field("CAddConstructionCommand", "vt", VT_ADD_CONSTRUCTION)
        CTOR_ADD_CONSTRUCTION = _prof.command_field("CAddConstructionCommand", "ctor", CTOR_ADD_CONSTRUCTION)

        VT_ADD_ADVISOR = _prof.command_field("CAddAdvisorCommand", "vt", VT_ADD_ADVISOR)
        ADD_ADVISOR_SIZE = _prof.command_field("CAddAdvisorCommand", "size", ADD_ADVISOR_SIZE)
        CTOR_ADD_ADVISOR = _prof.command_field("CAddAdvisorCommand", "ctor", CTOR_ADD_ADVISOR)
        CANEXEC_ADD_ADVISOR = _prof.command_field("CAddAdvisorCommand", "can_execute", CANEXEC_ADD_ADVISOR)

        VT_UNLOCK_DOCTRINE = _prof.command_field("CUnlockGrandDoctrineCommand", "vt", VT_UNLOCK_DOCTRINE)
        UNLOCK_DOCTRINE_SIZE = _prof.command_field("CUnlockGrandDoctrineCommand", "size", UNLOCK_DOCTRINE_SIZE)
        CTOR_UNLOCK_DOCTRINE = _prof.command_field("CUnlockGrandDoctrineCommand", "ctor", CTOR_UNLOCK_DOCTRINE)
        CANEXEC_UNLOCK_DOCTRINE = _prof.command_field("CUnlockGrandDoctrineCommand", "can_execute", CANEXEC_UNLOCK_DOCTRINE)

        VT_SET_GAME_SPEED = _prof.command_field("CSetGameSpeedCommand", "vt", VT_SET_GAME_SPEED)
        CTOR_SET_GAME_SPEED = _prof.command_field("CSetGameSpeedCommand", "ctor", CTOR_SET_GAME_SPEED)
        SIZE_SET_GAME_SPEED = _prof.command_field("CSetGameSpeedCommand", "size", SIZE_SET_GAME_SPEED)
        CANEXEC_SET_GAME_SPEED = _prof.command_field("CSetGameSpeedCommand", "can_execute", CANEXEC_SET_GAME_SPEED)
        VT_INCREASE_GAME_SPEED = _prof.command_field("CIncreaseGameSpeedCommand", "vt", VT_INCREASE_GAME_SPEED)

        VT_SET_PROD_LINE = _prof.command_field("CSetProductionLineCommand", "vt", VT_SET_PROD_LINE)
        CTOR_SET_PROD_LINE = _prof.command_field("CSetProductionLineCommand", "ctor", CTOR_SET_PROD_LINE)
        CANEXEC_SET_PROD_LINE = _prof.command_field("CSetProductionLineCommand", "can_execute", CANEXEC_SET_PROD_LINE)
        SIZE_SET_PROD_LINE = _prof.command_field("CSetProductionLineCommand", "size", SIZE_SET_PROD_LINE)

        SPEC_PATCH["CSetProductionLineCommand"].update(
            vt=VT_SET_PROD_LINE, ctor=CTOR_SET_PROD_LINE, size=SIZE_SET_PROD_LINE
        )
        SPEC_PATCH["CSetGameSpeedCommand"].update(
            vt=VT_SET_GAME_SPEED, ctor=CTOR_SET_GAME_SPEED, size=SIZE_SET_GAME_SPEED
        )
    except Exception as _e:
        import diag as _diag
        _diag.warn("cmdwin.apply_profile", _e)


_apply_profile()
