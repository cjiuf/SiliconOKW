"""Drive the peace conference -- lifecycle commands *and* the demands themselves.

Everything here was read out of hoi4.exe 1.19.5.0; addresses are PE RVAs.

Lifecycle commands (all one int at +0x28 = the country index the UI would send)

    CDonePeaceConferenceCommand      vtable 0x2AFC800  ctor 0x1ED1D70  size 0x30
    CEndTurnPeaceConferenceCommand   vtable 0x2AFC8C0                  size 0x38
    CPassPeaceConferenceCommand      vtable 0x2AFC740  ctor 0x1ED1E50  size 0x30

Demands are data, not commands.  The chain is

    CPeaceConference +0x1E8 : std::map<countryIndex, CPeaceBiddingTurn*>
    CPeaceBiddingTurn       : 0x40 bytes
        +0x00 vtable 0x29EAEC0
        +0x08 int
        +0x10 CPdxArray<CPeaceAction*>   {data,cap,size,alloc}  (0x18)
        +0x28 CPdxArray<...>                                     (0x18)

`CPeaceConference::EndTurn(int* countryIndex, CPeaceBiddingTurn** turn)`
(0xEB5390) finds-or-inserts the map slot and takes ownership of the turn, so it
is the correct way to install one.

The action objects are built by five per-type builders that all append to
`arg+0x38` (a CPdxArray), keyed only by `arg+0x10` = the *losing* country index:

    0x14F26C0   change government   0x30D2
    0x14F2870   liberate            0x30D0
    0x14F3070   puppet              0x30D1
    0x14F3560   take navy           0x30EE
    0x14F3950   take states         0x30CF
    0x14F2680   all five in one call

Type ids were confirmed by calling the engine's own name lookup (0xEA9F00):
0x30CF -> 获得地区, 0x30D0 -> 解放, 0x30D1 -> 傀儡, 0x30D2 -> 改变政府,
0x30EE -> 夺取海军.

Object layouts (from the ctors):

    CTakeStatePeaceAction  0x50   0x15B46D0(this, int* a, int* b, CState*)
        +0x08 0x30CF   +0x30 *a   +0x34 *b   +0x38 *a   +0x48 state
    CPuppetPeaceAction     0x60   0x15B4530(this, int* a, int* b, int* c, int state)
        +0x08 0x30D1   +0x30 *a   +0x34 *b   +0x38 *a   +0x48 state  +0x50 *c
    CTakeNavyPeaceAction          0x15B4600(this, int* a, int* b, void* r9)
        +0x08 0x30EE   +0x48 r9   +0x50 5th arg   (name is built in the ctor)

`a` is the int returned by 0xC07100(&local, 0) -- measured as 0 -- and `b` is
the country index of the loser whose assets are being taken.
"""
import struct

import cmdwin

DONE_VT, DONE_CTOR, DONE_SIZE = 0x2AFC800, 0x1ED1D70, 0x30
ENDTURN_VT, ENDTURN_SIZE = 0x2AFC8C0, 0x38
PASS_VT, PASS_CTOR, PASS_SIZE = 0x2AFC740, 0x1ED1E50, 0x30
CANEXEC_DONE = 0x1ED23A0
CANEXEC_PASS = 0x1ED2A00

CONF_TURN_MAP = 0x1E8
CONF_BID_MAP = 0x1C8
VT_TURN = 0x29EAEC0
TURN_SIZE = 0x40
FN_INSTALL_TURN = 0xEB5390
FN_PUSH_BACK = 0x1403371D0

BUILD_CHANGE_GOV = 0x14F26C0
BUILD_LIBERATE = 0x14F2870
BUILD_PUPPET = 0x14F3070
BUILD_NAVY = 0x14F3560
BUILD_STATES = 0x14F3950
BUILD_ALL = 0x14F2680

BUILDERS = {
    'state': BUILD_STATES,
    'puppet': BUILD_PUPPET,
    'navy': BUILD_NAVY,
    'liberate': BUILD_LIBERATE,
    'gov': BUILD_CHANGE_GOV,
}

ACTION_SIZE = {'state': 0x50, 'puppet': 0x60}


VT_PEACE_CONFERENCE = 0x29CDA28


def conference(g):
    """Every live CPeaceConference (normally zero or one)."""
    from objindex import _scan_regions_for_vptrs
    p = g.p
    tgt = p.base + VT_PEACE_CONFERENCE
    return _scan_regions_for_vptrs(
        p, p.regions(writable=True, min_size=4096), [tgt], max_hits=8, chunk_size=8 << 20
    )


GET_CONF = 0x1ED1EE0
# 2026-11-23 (复测 7 B-9): 反汇编 GET_CONF 0x1ED1EE0 -> 0x1E05F0 / 0xC128A0 / 0x1ED1FF9 实证：
# CCurrentGameState+0x4E0 为和平会议容器，+0x14 为活跃和会计数 (i32)，+0x08 为指针数组 data*，
# data[0] 即当前活跃 CPeaceConference*。零注入纯只读 O(1)，可安全放入 turnloop 轮询。
GS_PEACE_CONF = 0x4E0
_LAST_CONFERENCE = {}


def live_conference_readonly(g):
    """0 注入纯只读 O(1) 读取当前进行中的 CPeaceConference*，无和会返回 None。"""
    p = getattr(g, "p", None)
    if p is None:
        return None
    try:
        import offsets_win as _ow
        gs_rva = getattr(_ow, "G_GAMESTATE", 0x33DD6B0)
    except Exception:
        gs_rva = 0x33DD6B0
    try:
        gs = p.u64(p.base + gs_rva)
        if not (0x10000 < gs < 0x7F0000000000):
            return None
        mgr = gs + GS_PEACE_CONF
        cnt = p.i32(mgr + 0x14)
        if cnt <= 0 or cnt > 16:
            return None
        arr = p.u64(mgr + 0x08)
        if not (0x10000 < arr < 0x7F0000000000):
            return None
        conf = p.u64(arr)
        if 0x10000 < conf < 0x7F0000000000:
            return conf
    except Exception:
        return None
    return None


def live_conference(g):
    """The open conference in O(1). 优先走 0 注入纯只读直读，失败时回退引擎 getter。"""
    v = live_conference_readonly(g)
    if v:
        return v
    # 若纯只读确认 G_GAMESTATE 有效且计数为 0，直接返回 None，绝不无谓注入暂停游戏线程
    p = getattr(g, "p", None)
    if p is not None:
        try:
            import offsets_win as _ow
            gs = p.u64(p.base + getattr(_ow, "G_GAMESTATE", 0x33DD6B0))
            if 0x10000 < gs < 0x7F0000000000:
                cnt = p.i32(gs + GS_PEACE_CONF + 0x14)
                if cnt == 0:
                    return None
        except Exception:
            pass
    try:
        v = g.call(g.p.base + GET_CONF, (), absolute=True, force=True)
    except Exception:
        v = 0
    if v and 0x10000 < v < 0x7F0000000000:
        return v
    return None


def summary(g):
    """Small dict describing the live conference (for MCP tools / logging)."""
    p = g.p
    pid = getattr(p, "pid", None)
    conf = live_conference(g)
    if not conf:
        out = {"open": False}
        last = _LAST_CONFERENCE.get(pid)
        if last:
            out["last_conference"] = last
        out["note"] = (
            "no active peace conference; note that a capitulated/losing player does not "
            "receive a bidding turn (only victors submit peace demands)"
        )
        return out
    maps = {}
    for off in (0x1B8, 0x1C8, 0x1D8, 0x1E8, 0x1F8):
        maps["%#x" % off] = int(p.u64(conf + off + 8))
    parts = []
    data = p.u64(conf + 0xF8)
    n = p.i32(conf + 0x104)
    if 0x10000 < data < 0x7F0000000000:
        for i in range(max(0, min(n, 8))):
            o = p.u64(data + 8 * i)
            if not (0x10000 < o < 0x7F0000000000):
                continue
            parts.append({"country": p.i32(o + 0x08),
                          "score": p.i32(o + 0x4C),
                          "score_now": p.i32(o + 0x48)})
    cur = {"open": True, "conf": conf, "maps": maps,
           "participants": parts,
           "started": bool(p.read(conf + 0xDD, 1)[0]),
           "no_bid": bool(p.read(conf + 0xDC, 1)[0])}
    try:
        cur_date = g.date().get("text")
    except Exception:
        cur_date = None
    _LAST_CONFERENCE[pid] = {
        "conf": hex(conf),
        "seen_date": cur_date,
        "participants": parts,
        "started": cur["started"],
    }
    return cur


EXEC = {"done": 0x1ED22E0, "endturn": 0x1ED2300, "pass": 0x1ED2350}


def _apply_profile():
    """第二期数据驱动覆盖：从 calling/profile.py 加载和会相关虚表、命令与辅助函数 RVA。"""
    global DONE_VT, DONE_CTOR, DONE_SIZE
    global ENDTURN_VT, ENDTURN_SIZE
    global PASS_VT, PASS_CTOR, PASS_SIZE
    global CANEXEC_DONE, CANEXEC_PASS, VT_TURN, VT_PEACE_CONFERENCE
    global FN_INSTALL_TURN, GET_CONF
    try:
        import profile as _prof
        if not _prof.load():
            return
        DONE_VT = _prof.anchor("PEACE_DONE_VT", DONE_VT)
        DONE_CTOR = _prof.command_field("CDonePeaceConferenceCommand", "ctor", DONE_CTOR)
        DONE_SIZE = _prof.command_field("CDonePeaceConferenceCommand", "size", DONE_SIZE)
        ENDTURN_VT = _prof.anchor("PEACE_ENDTURN_VT", ENDTURN_VT)
        ENDTURN_SIZE = _prof.command_field("CEndTurnPeaceConferenceCommand", "size", ENDTURN_SIZE)
        PASS_VT = _prof.anchor("PEACE_PASS_VT", PASS_VT)
        PASS_CTOR = _prof.command_field("CPassPeaceConferenceCommand", "ctor", PASS_CTOR)
        PASS_SIZE = _prof.command_field("CPassPeaceConferenceCommand", "size", PASS_SIZE)
        CANEXEC_DONE = _prof.anchor("PEACE_CANEXEC_DONE", CANEXEC_DONE)
        CANEXEC_PASS = _prof.anchor("PEACE_CANEXEC_PASS", CANEXEC_PASS)
        VT_TURN = _prof.anchor("VT_PEACE_TURN", VT_TURN)
        VT_PEACE_CONFERENCE = _prof.anchor("VT_PEACE_CONFERENCE", VT_PEACE_CONFERENCE)
        FN_INSTALL_TURN = _prof.anchor("PEACE_FN_INSTALL_TURN", FN_INSTALL_TURN)
        GET_CONF = _prof.anchor("PEACE_GET_CONF", GET_CONF)
        EXEC["done"] = _prof.anchor("PEACE_EXEC_DONE", EXEC["done"])
        EXEC["endturn"] = _prof.anchor("PEACE_EXEC_ENDTURN", EXEC["endturn"])
        EXEC["pass"] = _prof.anchor("PEACE_EXEC_PASS", EXEC["pass"])
    except Exception:
        pass


_apply_profile()


def maps(g, conf=None):
    """The five inline turn maps of the conference, as sizes."""
    p = g.p
    conf = conf or live_conference(g)
    if not conf:
        return {}
    return {hex(off): map_size(g, conf, off) for off in (0x1B8, 0x1C8, 0x1D8, 0x1E8, 0x1F8)}


def finish(g, winner_idx, do_pass=False, wait=20.0):
    """Close the conference without the pass -> done race.

    `pass` APPENDS a second turn to +0x1F8 (measured: 1 -> 2).  `done` then merges
    both into +0x1D8/+0x1C8 and the engine logs

        History for <TAG> is invalid. Number of turn is 2 when it should be 1.
        Will resize.

    -- and sometimes hangs while applying the treaty (2026-11-15: 2 of 5 runs).
    The submitted turn is already sitting in +0x1F8, so Done on its own is enough
    and keeps the count at 1.
    """
    import time
    p = g.p
    conf = live_conference(g)
    if not conf:
        raise RuntimeError("no peace conference is open")
    out = {"before": maps(g, conf)}
    n = out["before"].get("0x1f8")
    out["submitted_turns"] = n
    if n and int(n) > 1:
        out["warning"] = ("+0x1F8 already holds %s turns; install_turns should have "
                          "left exactly one" % n)
    if do_pass:
        out["pass"] = execute(g, "pass", winner_idx)
        out["after_pass"] = maps(g)
        t0 = time.time()
        while time.time() - t0 < wait:
            if map_size(g, conf, 0x1F8) <= 1:
                break
            time.sleep(0.25)
        out["waited_after_pass"] = round(time.time() - t0, 2)
    out["done"] = execute(g, "done", winner_idx)
    out["after_done"] = maps(g)
    out["started"] = bool(p.read(conf + 0xDD, 1)[0])
    return out


def execute(g, what, country_idx):
    """Run one of the three conference commands directly (the queue does not drain)."""
    p = g.p
    conf = live_conference(g)
    if not conf:
        raise RuntimeError("no peace conference is open")
    size = 0x38 if what == "endturn" else 0x30
    cmd = cmdwin.alloc(g, size)
    p.write(cmd, b"\0" * size)
    p.write(cmd + 0x28, struct.pack("<i", int(country_idx)))
    if size >= 0x38:
        p.write(cmd + 0x30, struct.pack("<Q", 0))
    r = g.call(p.base + EXEC[what], (cmd,), absolute=True, force=True)
    return {"ran": what, "result": r, "summary": summary(g)}


def _post(g, vt, ctor, size, idx):
    p = g.p
    cmd = cmdwin.alloc(g, size)
    p.write(cmd, b"\0" * size)
    buf = cmdwin.alloc(g, 4)
    p.write(buf, struct.pack("<i", int(idx)))
    try:
        if ctor:
            g.call(p.base + ctor, (cmd, buf), absolute=True, force=True)
            if p.u64(cmd) != p.base + vt:
                return False, "ctor did not install the vtable", cmd
        else:
            p.write(cmd, struct.pack("<Q", p.base + vt))
            p.write(cmd + 0x28, struct.pack("<i", int(idx)))
    finally:
        try:
            cmdwin.free(g, buf)
        except Exception:
            pass
    cmdwin.attach_fake_vtable(g, cmd, vt)
    if not cmdwin.post(g, cmd):
        return False, "Post refused", cmd
    return True, "", cmd


def done(g, idx=1):
    """Submit my demands and close my side of the conference."""
    return _post(g, DONE_VT, DONE_CTOR, DONE_SIZE, idx)


def passed(g, idx=1):
    """Pass (give up my remaining bids)."""
    return _post(g, PASS_VT, PASS_CTOR, PASS_SIZE, idx)


def end_turn(g, idx=1, turn=0):
    """Submit this country's demands and close the round.

    CEndTurnPeaceConferenceCommand::Execute (0x1ED2300) is

        conf = 0x1ED1EE0()
        0x1A86C60(*(cmd+0x30), conf)        # CPeaceBiddingTurn::Attach(conf)
        0xEB5390(conf, &cmd+0x28, &cmd+0x30)  # steal the turn into the map

    so +0x28 is the country index and +0x30 the CPeaceBiddingTurn* -- not zero.
    Passing 0 (the old behaviour) hands the engine a null turn.
    """
    p = g.p
    cmd = cmdwin.alloc(g, ENDTURN_SIZE)
    p.write(cmd, b"\0" * ENDTURN_SIZE)
    p.write(cmd, struct.pack("<Q", p.base + ENDTURN_VT))
    p.write(cmd + 0x28, struct.pack("<i", int(idx)))
    p.write(cmd + 0x30, struct.pack("<Q", int(turn)))
    cmdwin.attach_fake_vtable(g, cmd, ENDTURN_VT)
    if not cmdwin.post(g, cmd):
        return False, "Post refused", cmd
    return True, "", cmd


def _allocator(g):
    import offsets_win as W
    return g.p.base + W.G_DEFAULT_ALLOC


def new_turn(g):
    """A zeroed CPeaceBiddingTurn with two empty arrays and a valid allocator."""
    p = g.p
    turn = cmdwin.alloc(g, TURN_SIZE)
    p.write(turn, b"\0" * TURN_SIZE)
    p.write(turn, struct.pack("<Q", p.base + VT_TURN))
    empty = struct.pack("<QiiQ", 0, 0, 0, _allocator(g))
    p.write(turn + 0x10, empty)
    p.write(turn + 0x28, empty)
    return turn


def install_turn(g, conf, country_idx, turn, map_off=CONF_BID_MAP):
    """Hand the turn to the conference.  Returns the turn.

    +0x1C8 is the map `done` consults: EB50E0 looks the player's index up there
    and sets conf+0xDC when the entry is missing.  +0x1E8 is what EndTurn fills.

    Calling 0xEB5390 by hand does not work: the function reads its `find` result
    out of a stack slot its own caller is expected to have set up, so an
    injected call silently writes the value into the tree's head sentinel and
    leaves the size at 0.  The map node is therefore linked by hand -- with a
    single element that is just the standard red-black shape.
    """
    p = g.p
    head = p.u64(conf + map_off)
    if not (0x10000 < head < 0x7F0000000000):
        raise RuntimeError("conference %#x has no tree head at %#x" % (conf, map_off))
    cur_sz = p.u64(conf + map_off + 8)
    if cur_sz > 0:
        raise RuntimeError("install_turn: map at %#x already has %d entries; refusing to overwrite multi-winner RB-tree" % (map_off, cur_sz))
    node = cmdwin.alloc(g, 0x30)
    p.write(node, b"\0" * 0x30)
    # _Left, _Parent, _Right -> head ; _Color = black ; _Isnil = 0
    p.write(node, struct.pack("<QQQ", head, head, head))
    p.write(node + 0x18, struct.pack("<BB", 0, 0))
    p.write(node + 0x20, struct.pack("<i", int(country_idx)))
    p.write(node + 0x28, struct.pack("<Q", turn))
    # the head sentinel is a nil node; point it at the only real node
    p.write(head, struct.pack("<QQQ", node, node, node))
    p.write(conf + map_off + 8, struct.pack("<Q", 1))
    return turn


def build_actions(g, loser_idx, kind='state', winner_idx=None):
    """Run one of the engine's own per-type builders.

    Returns a list of CPeaceAction* that the engine just allocated for the given
    losing country -- exactly what the conference window would list.

    The builder fills `+0x30`/`+0x38` from a stack local produced by
    ``0xC07100(&local, 0)`` (0x14F3A2F..0x14F3A48).  Called from the window that
    local holds the player's country index, but an injected call gets 0 -- and a
    demand whose winner is 0 is silently worth nothing (round 2 came back with
    cost 0).  Pass `winner_idx` to write the real one.
    """
    p = g.p
    arg = cmdwin.alloc(g, 0x100)
    p.write(arg, b"\0" * 0x100)
    p.write(arg + 0x10, struct.pack("<i", int(loser_idx)))
    # Every builder appends to arg+0x38; the puppet one also *clears* arg+0x68
    # first (0x14F30A5 calls 0x1AE000 on it), so that array needs a real
    # allocator too -- a zeroed one took the game down.
    empty = struct.pack("<QiiQ", 0, 0, 0, _allocator(g))
    p.write(arg + 0x38, empty)
    p.write(arg + 0x68, empty)
    g.call(p.base + BUILDERS[kind], (arg,), absolute=True, force=True)
    data = p.u64(arg + 0x38)
    cap = p.i32(arg + 0x40)
    size = p.i32(arg + 0x44)
    if not (0x10000 < data < 0x7F0000000000) or not (0 <= size <= 4096):
        return []
    acts = [p.u64(data + 8 * i) for i in range(size)]
    if winner_idx:
        for a in acts:
            if 0x10000 < a < 0x7F0000000000:
                p.write(a + 0x30, struct.pack("<i", int(winner_idx)))
                p.write(a + 0x38, struct.pack("<i", int(winner_idx)))
    return acts


# --------------------------------------------------------------------------
# Puppet / take-navy: the per-type builders 0x14F3070 / 0x14F3560 crash the
# process when called from an injected thread (0xEB67D0 inside them wants the
# real window context; two attempts on 2026-11-15 took hoi4.exe down).  The
# constructors are plain field writers, so build the action objects directly.
#
#   base 0x15B43F0(this, type, int* a, int* b, int* c)
#       +0x08 type, +0x10 std::string name (from 0x253ECD0(type)), +0x30 = *a,
#       +0x34 = *b, +0x38 = *a, +0x3c/+0x40 = 0
#   puppet 0x15B4530(this, a*, b*, c*, int state)   size 0x60
#       +0x48 state, +0x50 = *c ; builder passes b == c == &loser (0x14F30FE)
#   navy   0x15B4600(this, a*, b*, void* r9, void* arg5)  size 0x58
#       +0x48 = r9, +0x50 = arg5 ; the "whole fleet" call is r9 = 0,
#       arg5 = 0x186A0 (100000 = 100%, see 0x14F3849..0x14F385E)
# --------------------------------------------------------------------------
PUPPET_CTOR = 0x15B4530
NAVY_CTOR = 0x15B4600
ACTION_SIZE_CTOR = {'puppet': 0x60, 'navy': 0x58}


def make_action(g, kind, winner_idx, loser_idx, state=0, share=100000, fleet=0):
    """Allocate one puppet / take-navy action through its constructor.

    For `navy`, `fleet` is the value the builder puts in r9 (see 0x14F3849:
    "whole fleet" is 0 and `share` is 100000 = 100%).  Live result 2026-11-15:
    with fleet=0 against Denmark's two fleets the treaty moved 6 of 7 ships --
    one fleet's worth -- so a "take the whole navy" demand is one action per
    fleet, not one action in total.
    """
    p = g.p
    size = ACTION_SIZE_CTOR[kind]
    obj = cmdwin.alloc(g, size)
    p.write(obj, b"\0" * size)
    a = cmdwin.alloc(g, 4)
    b = cmdwin.alloc(g, 4)
    p.write(a, struct.pack("<i", int(winner_idx)))
    p.write(b, struct.pack("<i", int(loser_idx)))
    if kind == 'puppet':
        g.call(p.base + PUPPET_CTOR, (obj, a, b, b, int(state)),
               absolute=True, force=True)
    elif kind == 'navy':
        g.call(p.base + NAVY_CTOR, (obj, a, b, int(fleet), int(share)),
               absolute=True, force=True)
    else:
        raise ValueError(kind)
    return obj


def describe(g, act):
    """Fields of a CPeaceAction, including the name string it built."""
    p = g.p
    # The name is a std::string at +0x10, but its size/capacity words differ
    # per action class (CPuppetPeaceAction is 0x60 while the state action is
    # 0x50), so a bad read must never take the caller down.
    name = ""
    try:
        ln, cap = struct.unpack("<QQ", p.read(act + 0x20, 16))
        if 0 < ln <= 0x40 and ln <= cap <= 0x10000:
            if cap >= 16:
                name = p.read(p.u64(act + 0x10), min(ln, 64)).decode('utf-8', 'replace')
            else:
                name = p.read(act + 0x10, min(ln, 15)).decode('utf-8', 'replace')
    except Exception:
        name = "<unreadable>" 
    return {"act": act, "type": p.i32(act + 8), "name": name,
            "a": p.i32(act + 0x30), "b": p.i32(act + 0x34), "a2": p.i32(act + 0x38),
            "state": p.i32(act + 0x48), "extra": p.i32(act + 0x50)}


def make_puppet_demands(g, winner_idx, loser_idx, state_ids):
    return [make_action(g, 'puppet', winner_idx, loser_idx, st) for st in state_ids]


def make_navy_demand(g, winner_idx, loser_idx):
    return make_action(g, 'navy', winner_idx, loser_idx)


def map_size(g, conf, off):
    return g.p.u64(conf + off + 8)


# --------------------------------------------------------------------------
# conf+0x1F8 is where the *window's* 提交要求 button puts a turn.
#
# Verified live 2026-11-15 (Germany, HOL beaten, one state selected by hand):
# pressing the button left +0x1B8/+0x1C8/+0x1D8/+0x1E8 at size 0 and made
# +0x1F8 size 1.  The node is 0x40 bytes -- key at +0x20 and an *inline*
# CPdxArray<CPeaceBiddingTurn*> at +0x28 (data +0x28, cap +0x30, size +0x34,
# alloc +0x38) -- not the 0x30-byte pointer node that 0xEB5390 builds in
# +0x1E8.  The turn it held was byte-identical to what new_turn()+builders
# produce (vtable 0x29EAEC0, one 0x30CF action for state 35).
#
# `done` (CDonePeaceConferenceCommand::Execute 0x1ED22E0 -> 0xEB9AE0) only
# looks at +0x1D8 and +0x1C8, so the merge +0x1F8 -> +0x1D8 -> +0x1C8 has to
# happen in the engine's own end-of-round/apply path; installing the turn is
# therefore the whole job on our side.
# --------------------------------------------------------------------------
CONF_SUBMIT_MAP = 0x1F8


def _map_find(p, conf, off, key):
    head = p.u64(conf + off)
    if not head:
        return None
    cur = p.u64(head + 8)
    while cur and cur != head:
        k = p.i32(cur + 0x20)
        if k == key:
            return cur
        cur = p.u64(cur) if key < k else p.u64(cur + 0x10)
    return None


def install_turns(g, conf, country_idx, turns, map_off=CONF_SUBMIT_MAP):
    """Put `turns` (a list of CPeaceBiddingTurn*) in conf+map_off for a country.

    Creates the 0x40-byte inline-array node when the key is absent and rewrites
    the array header when it is already there, so calling it twice is safe.
    """
    p = g.p
    head = p.u64(conf + map_off)
    if not (0x10000 < head < 0x7F0000000000):
        raise RuntimeError("conference %#x has no tree head at %#x" % (conf, map_off))
    data = cmdwin.alloc(g, 8 * max(1, len(turns)))
    p.write(data, b"".join(struct.pack("<Q", t) for t in turns))
    arr = struct.pack("<QiiQ", data, len(turns), len(turns), _allocator(g))

    node = _map_find(p, conf, map_off, country_idx)
    if node:
        p.write(node + 0x28, arr)
        return node
    cur_sz = p.u64(conf + map_off + 8)
    if cur_sz > 0:
        raise RuntimeError("install_turns: map at %#x already has %d entries; refusing to overwrite multi-winner RB-tree" % (map_off, cur_sz))
    node = cmdwin.alloc(g, 0x40)
    p.write(node, b"\0" * 0x40)
    p.write(node, struct.pack("<QQQ", head, head, head))
    p.write(node + 0x18, struct.pack("<BB", 0, 0))
    p.write(node + 0x20, struct.pack("<i", int(country_idx)))
    p.write(node + 0x28, arr)
    p.write(head, struct.pack("<QQQ", node, node, node))
    p.write(conf + map_off + 8, struct.pack("<Q", 1))
    return node


# 仅允许确认可安全在注入线程调用的 builder；puppet/navy 的引擎 builder 0x14F3070/0x14F3560
# 依赖真实 UI 窗口上下文，注入调用会直接崩进程（见上方注释）。
SAFE_OPTION_KINDS = ("state",)


def _clear_options_cache(g):
    if hasattr(g, "_peace_opts_cache"):
        g._peace_opts_cache.clear()
    g._peace_opts_conf = None


def options(g, loser_idx, winner_idx=None, kind="state", refresh=False):
    """列出当前和平会谈中可向某战败国索取的地区选项（只读缓存，避免 build_actions 堆泄漏）。

    2026-11-22 (P2 需求):
      1. `build_actions` 每次调用都会在引擎堆里 new 一批 CPeaceAction* 且永不释放，
         因此按 `(pid, conf_ptr)` 隔离缓存，同一场会议内对同一 `(winner, loser, kind)`
         只调一次 `build_actions`；会议关闭或换新会议时自动清空旧缓存。
      2. `winner_idx` 必须为真实国家索引（> 0），绝不传 0，否则引擎给该要求计价为 0。
    """
    kind = str(kind or "state")
    if kind not in SAFE_OPTION_KINDS:
        return {
            "ok": False,
            "error": ("kind=%r is not safe for peace_options builder "
                      "(allowed: %s; puppet/navy builders crash outside window context)"
                      % (kind, ", ".join(SAFE_OPTION_KINDS))),
        }
    conf = live_conference(g)
    if not conf:
        _clear_options_cache(g)
        return {"ok": False, "error": "no peace conference is open"}

    if winner_idx is None:
        winner_idx = int(g.player_tag_id())
    else:
        winner_idx = int(winner_idx)
    if winner_idx <= 0:
        return {"ok": False, "error": "invalid winner country index (%d); must be > 0" % winner_idx}
    loser_idx = int(loser_idx)

    pid = getattr(getattr(g, "p", None), "pid", None)
    conf_sig = (pid, int(conf))
    if getattr(g, "_peace_opts_conf", None) != conf_sig:
        g._peace_opts_conf = conf_sig
        g._peace_opts_cache = {}
    cache = getattr(g, "_peace_opts_cache", None)
    if cache is None:
        cache = {}
        g._peace_opts_cache = cache

    key = (winner_idx, loser_idx, kind)
    if (not refresh) and key in cache:
        rows = cache[key]
        return {
            "ok": True,
            "conf": hex(int(conf)),
            "winner_idx": winner_idx,
            "loser_idx": loser_idx,
            "kind": kind,
            "cached": True,
            "count": len(rows),
            "options": [dict(r) for r in rows],
        }

    acts = build_actions(g, loser_idx, kind, winner_idx=winner_idx)
    rows = []
    seen_states = set()
    for act in acts:
        if not (0x10000 < act < 0x7F0000000000) or (act & 7):
            continue
        d = describe(g, act)
        sid = d.get("state")
        if kind == "state" and sid in seen_states:
            continue
        if kind == "state" and sid is not None:
            seen_states.add(sid)
        sname = None
        if sid is not None and sid > 0:
            try:
                det = g.state_detail(int(sid)) or {}
                sname = det.get("name")
            except Exception:
                sname = None
        rows.append({
            "state": sid,
            "name": sname,
            "type": d.get("type"),
            "act": hex(int(d["act"])),
            "extra": d.get("extra"),
        })
    cache[key] = rows
    return {
        "ok": True,
        "conf": hex(int(conf)),
        "winner_idx": winner_idx,
        "loser_idx": loser_idx,
        "kind": kind,
        "cached": False,
        "count": len(rows),
        "options": [dict(r) for r in rows],
    }


def submit_demands(g, conf, winner_idx, loser_idx, kinds=("state",),
                   map_off=CONF_SUBMIT_MAP, states=None, filter_info=None):
    """Build one turn from the engine's builders and submit it like the button.

    `states` (optional iterable of int state ids): when given, only `state`
    demands whose state id is in `states` are included in the submitted turn.
    """
    p = g.p
    winner_idx = int(winner_idx)
    if winner_idx <= 0:
        raise ValueError("winner_idx must be > 0 (got %d)" % winner_idx)
    wanted_states = None
    if states is not None:
        wanted_states = [int(s) for s in states]
        if not wanted_states:
            raise ValueError("states subset cannot be an empty list; omit `states` to demand all")
    wanted_set = set(wanted_states) if wanted_states is not None else None

    acts = []
    available_states = []
    matched_states = []
    for kind in kinds:
        for act in build_actions(g, loser_idx, kind, winner_idx=winner_idx):
            if not (0x10000 < act < 0x7F0000000000) or (act & 7):
                continue
            if kind == "state":
                sid = int(p.i32(act + 0x48))
                available_states.append(sid)
                if wanted_set is not None and sid not in wanted_set:
                    continue
                matched_states.append(sid)
            acts.append(act)

    if filter_info is not None:
        filter_info["available_states"] = available_states
        if wanted_states is not None:
            filter_info["requested_states"] = wanted_states
            filter_info["matched_states"] = matched_states
            filter_info["ignored_states"] = [s for s in wanted_states if s not in set(matched_states)]

    if wanted_set is not None and not acts:
        raise ValueError(
            "none of the requested states %s are available for loser %d (available: %s)"
            % (wanted_states, int(loser_idx), available_states)
        )

    turn = new_turn(g)
    if acts:
        data = cmdwin.alloc(g, 8 * len(acts))
        p.write(data, b"".join(struct.pack("<Q", a) for a in acts))
        p.write(turn + 0x10, struct.pack("<QiiQ", data, len(acts), len(acts), _allocator(g)))
    install_turns(g, conf, winner_idx, [turn], map_off)
    return turn, acts


def show_submitted(g, conf, country_idx, map_off=CONF_SUBMIT_MAP):
    p = g.p
    node = _map_find(p, conf, map_off, country_idx)
    if not node:
        return None
    data = p.u64(node + 0x28)
    size = p.i32(node + 0x34)
    out = []
    for i in range(min(size, 16)):
        t = p.u64(data + 8 * i)
        adata = p.u64(t + 0x10)
        asize = p.i32(t + 0x1C)
        acts = []
        for j in range(min(asize, 32)):
            a = p.u64(adata + 8 * j)
            acts.append((p.i32(a + 8), p.i32(a + 0x34), p.i32(a + 0x48)))
        out.append((t, acts))
    return out


def add_demands(g, conf, winner_idx, loser_idx, kinds=('state',), map_off=None):
    """Give the winner's bid turn the engine's own demands for `loser_idx`.

    The builders already return a finished CPdxArray at arg+0x38, so the array
    header is written into the turn instead of growing it with push_back: that
    keeps the engine's own allocator and avoids calling 0x1403371D0 by hand
    (doing so crashed the game on 2026-11-15).

    `map_off` may be one offset or a list; a separate turn (and a separate set
    of action objects) is built for each, so nothing is ever owned twice.
    """
    p = g.p
    alloc = _allocator(g)
    offs = map_off or [CONF_BID_MAP]
    if isinstance(offs, int):
        offs = [offs]
    turns, acts = [], []
    for off in offs:
        got = []
        for kind in kinds:
            for act in build_actions(g, loser_idx, kind, winner_idx=winner_idx):
                if not (0x10000 < act < 0x7F0000000000) or act & 7:
                    continue
                got.append(act)
        turn = new_turn(g)
        if got:
            data = cmdwin.alloc(g, 8 * len(got))
            p.write(data, b"".join(struct.pack("<Q", a) for a in got))
            p.write(turn + 0x10, struct.pack("<QiiQ", data, len(got), len(got), alloc))
        install_turn(g, conf, winner_idx, turn, off)
        turns.append(turn)
        acts.extend(got)
    return turns, acts


if __name__ == "__main__":
    import os
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from game import Game
    g = Game()
    cs = conference(g)
    print("conferences", [hex(c) for c in cs])
    for c in cs:
        p = g.p
        print("  turn map head=%#x size=%d" % (p.u64(c + CONF_TURN_MAP), p.u64(c + CONF_TURN_MAP + 8)))
        print("  ints", [p.i32(c + o) for o in range(0, 0x40, 4)])
