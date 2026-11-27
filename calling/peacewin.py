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
import offsets_win as _OW

DONE_VT, DONE_CTOR, DONE_SIZE = _OW.PEACE_DONE_VT, _OW.PEACE_DONE_CTOR, _OW.PEACE_DONE_SIZE
ENDTURN_VT, ENDTURN_SIZE = _OW.PEACE_ENDTURN_VT, _OW.PEACE_ENDTURN_SIZE
PASS_VT, PASS_CTOR, PASS_SIZE = _OW.PEACE_PASS_VT, _OW.PEACE_PASS_CTOR, _OW.PEACE_PASS_SIZE
CANEXEC_DONE = _OW.PEACE_CANEXEC_DONE
CANEXEC_PASS = _OW.PEACE_CANEXEC_PASS

CONF_TURN_MAP = _OW.PEACE_CONF_TURN_MAP
CONF_BID_MAP = _OW.PEACE_CONF_BID_MAP
CONF_SUBMIT_MAP = _OW.PEACE_CONF_SUBMIT_MAP
VT_TURN = _OW.PEACE_VT_TURN
TURN_SIZE = _OW.PEACE_TURN_SIZE
FN_INSTALL_TURN = _OW.PEACE_FN_INSTALL_TURN
FN_PUSH_BACK = _OW.PEACE_FN_PUSH_BACK

BUILD_CHANGE_GOV = _OW.PEACE_BUILD_CHANGE_GOV
BUILD_LIBERATE = _OW.PEACE_BUILD_LIBERATE
BUILD_PUPPET = _OW.PEACE_BUILD_PUPPET
BUILD_NAVY = _OW.PEACE_BUILD_NAVY
BUILD_STATES = _OW.PEACE_BUILD_STATES
BUILD_ALL = _OW.PEACE_BUILD_ALL

BUILDERS = {
    'state': BUILD_STATES,
    'puppet': BUILD_PUPPET,
    'navy': BUILD_NAVY,
    'liberate': BUILD_LIBERATE,
    'gov': BUILD_CHANGE_GOV,
}
VALID_DEMAND_KINDS = tuple(BUILDERS.keys())

_KIND_SUGGESTIONS = {
    "annex": "state",
    "annex_all": "state",
    "take_state": "state",
    "take_states": "state",
    "states": "state",
    "change_gov": "gov",
    "change_government": "gov",
    "topple_government": "gov",
    "take_navy": "navy",
    "fleet": "navy",
}


def validate_kinds(kinds):
    """校验并规范化 peace_demands 的 kinds 参数（D-17 根治，杜绝裸 KeyError）。"""
    if kinds is None:
        return ["state"]
    if isinstance(kinds, str):
        raw = [kinds]
    elif isinstance(kinds, (list, tuple)):
        raw = list(kinds)
    else:
        raise ValueError(
            "kinds must be a list of strings (valid: %s; default: ['state'])"
            % ", ".join(VALID_DEMAND_KINDS)
        )
    if not raw:
        raise ValueError(
            "kinds cannot be empty (valid: %s; omit kinds to use default ['state'])"
            % ", ".join(VALID_DEMAND_KINDS)
        )
    norm = []
    invalid = []
    hints = []
    for k in raw:
        ks = str(k or "").strip().lower()
        if ks in BUILDERS:
            if ks not in norm:
                norm.append(ks)
        else:
            invalid.append(k)
            sugg = _KIND_SUGGESTIONS.get(ks)
            if sugg:
                hints.append("%r -> use %r" % (k, sugg))
    if invalid:
        hint_s = ("; hint: " + ", ".join(hints)) if hints else ""
        raise ValueError(
            "invalid peace demand kind(s): %s. Valid kinds: %s (safe default: ['state']; "
            "omit `kinds` to take states)%s"
            % (invalid, ", ".join(VALID_DEMAND_KINDS), hint_s)
        )
    return norm

ACTION_SIZE = {'state': 0x50, 'puppet': 0x60}


VT_PEACE_CONFERENCE = _OW.PEACE_VT_CONFERENCE


def conference(g):
    """Every live CPeaceConference (normally zero or one)."""
    from objindex import _scan_regions_for_vptrs
    p = g.p
    tgt = p.base + VT_PEACE_CONFERENCE
    return _scan_regions_for_vptrs(
        p, p.regions(writable=True, min_size=4096), [tgt], max_hits=8, chunk_size=8 << 20
    )


GET_CONF = _OW.PEACE_GET_CONF
# 2026-11-23 (复测 7 B-9): 反汇编 GET_CONF 0x1ED1EE0 -> 0x1E05F0 / 0xC128A0 / 0x1ED1FF9 实证：
# CCurrentGameState+0x4E0 为和平会议容器，+0x14 为活跃和会计数 (i32)，+0x08 为指针数组 data*，
# data[0] 即当前活跃 CPeaceConference*。零注入纯只读 O(1)，可安全放入 turnloop 轮询。
GS_PEACE_CONF = _OW.PEACE_GS_CONF
import session_state as _SS

_LAST_CONFERENCE = {}
_SS.register_cache("peacewin._LAST_CONFERENCE", container=_LAST_CONFERENCE)
_SUBMITTED_DEMANDS = {}
_SS.register_cache("peacewin._SUBMITTED_DEMANDS", container=_SUBMITTED_DEMANDS)

ACT_COST = _OW.PEACE_ACT_COST
ACT_TYPE_TAKE_STATE = _OW.PEACE_ACT_TYPE_TAKE_STATE


def _tag_by_idx(g):
    """纯只读构建 {country_idx: TAG} 映射（兼容离线 mock）。"""
    out = {}
    try:
        for t in (g.tags() or ()):
            try:
                out[int(g._c(t).tag_id)] = str(t).upper()
            except Exception:
                continue
    except Exception:
        pass
    return out


def _action_key(p, act):
    """每条 CPeaceAction 的去重键 `(type, loser_idx, state_id)`。"""
    try:
        atype = int(p.i32(act + 8))
    except Exception:
        atype = ACT_TYPE_TAKE_STATE
    try:
        lidx = int(p.i32(act + 0x34))
    except Exception:
        lidx = 0
    try:
        sid = int(p.i32(act + 0x48))
    except Exception:
        sid = 0
    return (atype, lidx, sid)


def _action_cost(p, act):
    """读取 CPeaceAction + 0x40 (i32) 结算分值（0x1A86D10 TurnCost 实证）。"""
    try:
        return int(p.i32(act + ACT_COST))
    except Exception:
        return 0


def _read_existing_turn_actions(p, conf, winner_idx, map_off=CONF_SUBMIT_MAP):
    """纯只读收集 conf+map_off 中该战胜国当前已排队的全部 CPeaceAction*。"""
    try:
        node = _map_find(p, conf, map_off, int(winner_idx))
    except Exception:
        return []
    if not node:
        return []
    try:
        tdata = p.u64(node + 0x28)
        tsize = p.i32(node + 0x34)
    except Exception:
        return []
    if not (0x10000 < tdata < 0x7F0000000000) or not (0 < tsize <= 16):
        return []
    acts = []
    for i in range(tsize):
        try:
            t = p.u64(tdata + 8 * i)
            if not (0x10000 < t < 0x7F0000000000):
                continue
            adata = p.u64(t + 0x10)
            asize = p.i32(t + 0x1C)
            if not (0x10000 < adata < 0x7F0000000000) or not (0 < asize <= 4096):
                continue
            for j in range(asize):
                a = p.u64(adata + 8 * j)
                if 0x10000 < a < 0x7F0000000000 and (a & 7) == 0:
                    acts.append(a)
        except Exception:
            continue
    return acts


def _winner_score_info(g, winner_idx):
    """从当前和会参与者表中读取 winner_idx 的 `(score_now, score_total)`。"""
    try:
        s = summary(g)
        for pt in (s.get("participants") or ()):
            if int(pt.get("country") or -1) == int(winner_idx):
                return pt.get("score_now"), pt.get("score")
    except Exception:
        pass
    return None, None


def _owned_state_ids(g, tag=None):
    """纯只读收集指定国家当前法理拥有（owned）的州 ID 集合；不可读时返回 None。

    2026-11-26 (P3-10 根治):
      实证 SPR/D03 战时差异证明 `CCountry+0x460 (CO_STATES)` 是战时**控制州**（controlled），
      `CCountry+0x478 (CO_STATES_B)` 才是**法理拥有州**（owned）！
      旧实现调用 `g.states(tag)`（读 +0x460），导致和会前已战时占领的敌国本土州（如 7/35/36）
      被提前算进 `states_before`，造成 `net_states_gained` 缺少已占领诉求州、与 `acquired_states` 不一致。
      现优先在 Windows 上直读 `CO_STATES_B`（法理拥有州），离线 mock 再回退 `g.states(tag)`。
    """
    p = getattr(g, "p", None)
    if p is not None and hasattr(p, "u64") and hasattr(p, "i32") and hasattr(g, "_c"):
        try:
            co = g._c(tag)
            cptr = getattr(co, "ptr", 0)
            b_off = getattr(_OW, "CO_STATES_B", None)
            st_id_off = getattr(_OW, "ST_ID", None)
            if cptr and cptr > 0x10000 and b_off is not None and st_id_off is not None:
                data = p.u64(cptr + b_off)
                size = p.i32(cptr + b_off + 0x0C)
                if 0x10000 < data < 0x7F0000000000 and 0 <= size <= 2048:
                    out = set()
                    for i in range(size):
                        sp = p.u64(data + 8 * i)
                        if 0x10000 < sp < 0x7F0000000000:
                            sid = int(p.i32(sp + st_id_off))
                            if sid > 0:
                                out.add(sid)
                    return out
                if size == 0:
                    return set()
        except Exception:
            pass
    try:
        rows = g.states(tag)
    except Exception:
        return None
    if rows is None:
        return None
    out = set()
    for st in rows:
        try:
            sid = int(st.get("id") if isinstance(st, dict) else st)
            if sid > 0:
                out.add(sid)
        except Exception:
            continue
    return out


def _is_finalized_conference(p, conf):
    """纯只读判定 CPeaceConference 是否已执行完毕收尾（条约已应用、仅剩界面/对象待释放）。

    实证（WINDOWS_1.19.5.md §62.3 & 1936->1940 长跑 §5）：
      - 会议打开未提交时：+0xDD == 0, +0x1C8 == 0, +0x1F8 == 0
      - 提交要求后：+0xDD == 0, +0x1F8 >= 1
      - 执行 Done 且条约已应用、州易主后：+0xDD == 1, +0x1C8 >= 1, +0x1F8 == 0
    """
    try:
        done_flag = bool(p.read(conf + 0xDD, 1)[0])
        if not done_flag:
            return False
        c8 = int(p.u64(conf + 0x1C8 + 8))
        f8 = int(p.u64(conf + 0x1F8 + 8))
        return c8 > 0 and f8 == 0
    except Exception:
        return False


def live_conference_readonly(g, include_finalized=False):
    """0 注入纯只读 O(1) 读取当前进行中的 CPeaceConference*，无和会（或条约已结算收尾）返回 None。"""
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
            if not include_finalized and _is_finalized_conference(p, conf):
                return None
            return conf
    except Exception:
        return None
    return None


def live_conference(g, include_finalized=False):
    """The open conference in O(1). 优先走 0 注入纯只读直读，失败时回退引擎 getter。"""
    v = live_conference_readonly(g, include_finalized=include_finalized)
    if v:
        return v
    # 若纯只读确认 G_GAMESTATE 有效且无未结算和会（计数为 0 或仅剩已结算残留对象），直接返回 None
    p = getattr(g, "p", None)
    if p is not None:
        try:
            import offsets_win as _ow
            gs = p.u64(p.base + getattr(_ow, "G_GAMESTATE", 0x33DD6B0))
            if 0x10000 < gs < 0x7F0000000000:
                cnt = p.i32(gs + GS_PEACE_CONF + 0x14)
                if cnt == 0:
                    return None
                if not include_finalized and live_conference_readonly(g, include_finalized=True):
                    return None
        except Exception:
            pass
    try:
        v = g.call(g.p.base + GET_CONF, (), absolute=True, force=True)
    except Exception:
        v = 0
    if v and 0x10000 < v < 0x7F0000000000:
        if not include_finalized and p is not None and _is_finalized_conference(p, v):
            return None
        return v
    return None


def summary(g):
    """Small dict describing the live conference (for MCP tools / logging)."""
    p = g.p
    pid = getattr(p, "pid", None)
    conf = live_conference(g, include_finalized=True)
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
    started_flag = bool(p.read(conf + 0xDD, 1)[0])
    no_bid_flag = bool(p.read(conf + 0xDC, 1)[0])
    finalized = bool(started_flag and maps.get("0x1c8", 0) > 0 and maps.get("0x1f8", 0) == 0)
    cur = {
        "open": not finalized,
        "conf": conf,
        "maps": maps,
        "participants": parts,
        "started": started_flag,
        "no_bid": no_bid_flag,
        "finalized": finalized,
        "treaty_applied": finalized,
    }
    if finalized:
        cur["conference_closing"] = True
        cur["note"] = (
            "peace treaty has already been applied (+0xDD=1, +0x1C8>0, +0x1F8=0); "
            "the residual conference object clears on the next unpaused game tick "
            "(if the clock stays paused on the treaty summary popup, call resume / "
            "dismiss_popups_and_unpause or wait_and_brief(fast_forward=true))."
        )
    try:
        cur_date = g.date().get("text")
    except Exception:
        cur_date = None
    _LAST_CONFERENCE[pid] = {
        "conf": hex(conf),
        "seen_date": cur_date,
        "participants": parts,
        "started": cur["started"],
        "finalized": finalized,
    }
    return cur


EXEC = {"done": _OW.PEACE_EXEC_DONE, "endturn": _OW.PEACE_EXEC_ENDTURN, "pass": _OW.PEACE_EXEC_PASS}


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
    except Exception as _e:
        import diag as _diag
        _diag.warn("peacewin.apply_profile", _e)


_apply_profile()


def maps(g, conf=None):
    """The five inline turn maps of the conference, as sizes."""
    p = g.p
    conf = conf or live_conference(g)
    if not conf:
        return {}
    return {hex(off): map_size(g, conf, off) for off in (0x1B8, 0x1C8, 0x1D8, 0x1E8, 0x1F8)}


def finish(g, winner_idx, do_pass=False, wait=20.0, allow_empty=False):
    """Close the conference without the pass -> done race.

    `pass` APPENDS a second turn to +0x1F8 (measured: 1 -> 2).  `done` then merges
    both into +0x1D8/+0x1C8 and the engine logs

        History for <TAG> is invalid. Number of turn is 2 when it should be 1.
        Will resize.

    -- and sometimes hangs while applying the treaty (2026-11-15: 2 of 5 runs).
    The submitted turn is already sitting in +0x1F8, so Done on its own is enough
    and keeps the count at 1.

    2026-11-26 (P0-3 增强):
      在执行 `done` 前后纯只读回读玩家实有州集合与每一块已提交诉求州的最终归属，
      透明汇报 `states_before` / `states_after` / `net_states_gained` / `acquired_states` /
      `not_acquired_states`，彻底杜绝静默丢州却毫无感知。
    2026-11-27 (D-18 根治):
      1. 默认拦截「零索赔」关闭会议（`queued_actions == 0` 且 `submitted_turns == 0` 且未传
         `allow_empty=True`），返回 `blocked_by="empty_peace_demands"`，防止前一步
         `peace_demands` 失败后误把和会以白和平关闭；确需白和平可显式传 `allow_empty=True`。
      2. 结算完成后 `at_war_after` 区分「刚通过本场和会结清、待下 1 游戏小时清理的残余敌国」
         与「仍在进行的场外其他战争」，消除同帧 `at_war_after=true` 与下一帧 `战争=否` 矛盾。
    """
    import time
    p = g.p
    pid = getattr(p, "pid", None)
    conf = live_conference(g)
    if not conf:
        raise RuntimeError("no peace conference is open")

    idx_map = _tag_by_idx(g)
    winner_tag = idx_map.get(int(winner_idx))
    if not winner_tag:
        try:
            winner_tag = str(g.player_tag() or "").upper() or None
        except Exception:
            winner_tag = None

    states_before = _owned_state_ids(g, winner_tag)

    # 汇总提交到 +0x1F8（及本会话缓存）的全部索赔动作与州诉求，用于空单拦截与结算后核对
    queued_acts_now = _read_existing_turn_actions(p, conf, int(winner_idx), CONF_SUBMIT_MAP)
    demanded_states = []
    demanded_losers = set()
    seen_dem = set()
    for a in queued_acts_now:
        try:
            atype, lidx, sid = _action_key(p, a)
            if lidx > 0 and lidx in idx_map:
                demanded_losers.add(idx_map[lidx])
            if atype == ACT_TYPE_TAKE_STATE and sid > 0 and sid not in seen_dem:
                seen_dem.add(sid)
                demanded_states.append(sid)
        except Exception:
            continue
    cached_rec = _SUBMITTED_DEMANDS.get((pid, int(conf))) or {}
    for ltag in (cached_rec.get("queued_states_by_loser") or {}).keys():
        if ltag and not str(ltag).startswith("#"):
            demanded_losers.add(str(ltag).upper())
    for sid in (cached_rec.get("states") or ()):
        if sid > 0 and sid not in seen_dem:
            seen_dem.add(sid)
            demanded_states.append(int(sid))

    # 读取结算前的和会参与者集合（用于结算后区分本场和会残余关系与场外活跃敌国）
    conf_participants_tags = set()
    try:
        _sum0 = summary(g) or {}
        for _pt in (_sum0.get("participants") or ()):
            _cid = int(_pt.get("country") or -1)
            if _cid in idx_map:
                conf_participants_tags.add(idx_map[_cid])
    except Exception:
        pass

    out = {"before": maps(g, conf)}
    n = out["before"].get("0x1f8")
    out["submitted_turns"] = n
    out["queued_actions"] = len(queued_acts_now)

    # D-18 零索赔白和平护栏：+0x1F8 无任何排队动作且本会话也无已提交要求时拒绝直接关闭和会
    if (not allow_empty) and len(queued_acts_now) == 0 and (not n or int(n) == 0) and not demanded_states:
        return {
            "ok": False,
            "blocked_by": "empty_peace_demands",
            "submitted_turns": int(n or 0),
            "queued_actions": 0,
            "conference_still_open": True,
            "before": out["before"],
            "error": (
                "refusing to finalize peace conference with 0 queued demands (submitted_turns=0; "
                "closing now would produce a WHITE PEACE with 0 states gained). "
                "Queue demands via peace_demands(loser=...) first, or pass allow_empty=true "
                "if you genuinely intend a white peace."
            ),
        }

    if allow_empty and len(queued_acts_now) == 0 and (not n or int(n) == 0) and not demanded_states:
        out["white_peace_confirmed"] = True
        out["warning"] = "finalizing peace conference with 0 queued demands (white peace confirmed via allow_empty=True)"

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
    out["after_done"] = maps(g, conf)
    out["started"] = bool(p.read(conf + 0xDD, 1)[0])
    out["finalized"] = _is_finalized_conference(p, conf)
    out["treaty_applied"] = out["finalized"]

    # 结算后纯只读回读法理拥有州（owned）归属变化（P0-3 / P3-10）
    states_after = _owned_state_ids(g, winner_tag)
    gained = []
    if states_before is not None:
        out["states_before_count"] = len(states_before)
    if states_after is not None:
        out["states_after_count"] = len(states_after)
    if states_before is not None and states_after is not None:
        gained = sorted(states_after - states_before)
        out["net_states_gained_count"] = len(gained)
        out["net_states_gained"] = gained

    if demanded_states:
        acquired = []
        not_acquired = []
        for sid in demanded_states:
            cur_owner = None
            sname = None
            if states_after is not None and sid in states_after:
                acquired.append(sid)
                continue
            try:
                det = g.state_detail(int(sid)) or {}
                cur_owner = det.get("owner")
                sname = det.get("name")
            except Exception:
                cur_owner = None
            if winner_tag and cur_owner and str(cur_owner).upper() == str(winner_tag).upper():
                acquired.append(sid)
            else:
                row = {"state": sid, "owner": cur_owner}
                if sname:
                    row["name"] = sname
                not_acquired.append(row)
        out["demanded_states"] = demanded_states
        out["demanded_count"] = len(demanded_states)
        out["acquired_states"] = acquired
        out["acquired_count"] = len(acquired)
        out["not_acquired_states"] = not_acquired
        out["not_acquired_count"] = len(not_acquired)
        out["partial_acquisition"] = bool(not_acquired)
        if gained:
            unreq = [s for s in gained if s not in set(demanded_states)]
            if unreq:
                out["unrequested_states_gained"] = unreq
        if not_acquired:
            out["acquisition_warning"] = (
                "requested %d state(s) but only acquired %d (%d not acquired: %s); "
                "remaining demands may have exceeded warscore budget or been contested by other victors"
                % (
                    len(demanded_states),
                    len(acquired),
                    len(not_acquired),
                    ", ".join("%d(owner=%s)" % (x["state"], x.get("owner")) for x in not_acquired[:8]),
                )
            )

    if out["finalized"]:
        _SUBMITTED_DEMANDS.pop((pid, int(conf)), None)
        try:
            import combat as _cb_pw
            raw_enemies = list(_cb_pw.enemy_tags(g, winner_tag) or [])
            raw_at_war = bool(raw_enemies) if raw_enemies else bool(_cb_pw.at_war(g, winner_tag))
            out["at_war_after_raw"] = raw_at_war
            settled_tags = set(demanded_losers) | (conf_participants_tags - ({str(winner_tag).upper()} if winner_tag else set()))
            # 若存在未参与本场和会的敌国，则真实仍处于其他战争；若所有残留敌国均属本场已结清和会对象
            # （或刚落实条约的暂停帧无活跃战斗），则标记 at_war_after=False 消除同帧假战争矛盾。
            remaining_outside = [e for e in raw_enemies if str(e).upper() not in settled_tags] if settled_tags else []
            has_live_combats = False
            try:
                has_live_combats = bool(_cb_pw.combats(g, only_mine=True, me=winner_tag))
            except Exception:
                has_live_combats = False
            if raw_at_war and not remaining_outside and not has_live_combats:
                out["at_war_after"] = False
                if raw_enemies:
                    out["at_war_pending_cleanup_enemies"] = raw_enemies
                out["at_war_note"] = (
                    "treaty finalized on paused frame; residual CWarRelation entries (%s) "
                    "for this settled conference clear on the next game hour"
                    % (", ".join(raw_enemies) if raw_enemies else "conference_closing")
                )
            else:
                out["at_war_after"] = raw_at_war
                if remaining_outside:
                    out["remaining_enemies_outside_conference"] = remaining_outside
        except Exception:
            pass
        # 2026-11-26 (P3-11 / 2026-11-27 D-06): 暂停帧下和会条约虽已立刻改变州 owner，
        # 但 CCountryResources、工厂缓存及残留和会/战争关系对象需推进 >= 1 游戏小时后完全清理。
        out["resources_refresh_note"] = (
            "peace treaty applied on the current frame; CCountryResources (e.g. rubber/oil), "
            "factory totals, and residual conference/war-relation objects finish clearing after >= 1 game hour."
        )
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
        except Exception as _fe:
            import diag as _diag
            _diag.warn("peacewin.post.free_buf", _fe)
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
    kind = validate_kinds([kind])[0]
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
PUPPET_CTOR = _OW.PEACE_PUPPET_CTOR
NAVY_CTOR = _OW.PEACE_NAVY_CTOR
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
            "cost": _action_cost(p, act),
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
CONF_SUBMIT_MAP = _OW.PEACE_CONF_SUBMIT_MAP


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
            "cost": d.get("cost"),
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
                   map_off=CONF_SUBMIT_MAP, states=None, filter_info=None,
                   append=True, replace=False, replace_all=False):
    """Build demands from the engine's builders and install them in conf+map_off.

    2026-11-26 (P0-3 根治):
      1. **保序**：传入 `states=[...]` 时严格按调用方给定优先级顺序构造动作列表（去重保序），
         不再按 `build_actions` 的州 ID 升序重排，避免高价值资源州排在后面被分数截断；
      2. **默认追加合并（`append=True, replace=False`）**：同一场和会多次调用 `peace_demands`
         （例如先后向 HOL 与 INS 提诉求）时，先读取 `conf+map_off` 里该战胜国已排队的动作，
         与本次新动作按 `(type, loser_idx, state_id)` 保序合并去重后写入**单个**
         `CPeaceBiddingTurn`（保持 +0x1F8 为 1 turn 规避 2-turn 卡死，同时彻底消除后次覆写前次）；
      3. **按 loser 替换（2026-11-27 D-19 根治）**：`replace=True` 默认仅替换当前 `loser_idx`
         已排队的索赔，**完整保留其他战败国的已排队动作**，避免对第二个 loser 传 `replace=True`
         静默清空第一个 loser；确需清空整场会议全部战败国队列时显式传 `replace_all=True`。
      4. **kinds 严格校验（2026-11-27 D-17 根治）**：非法 kind（如 `annex`）在构造前即返回清晰
         错误及合法列表，绝不抛裸 `KeyError`。
      5. **预算透出**：汇总每条动作的 `act+0x40` 分值与 `score_now`，在 `filter_info` 返回
         `mode`/`added_actions`/`total_queued_actions`/`queued_states_by_loser`/`estimated_total_cost`/`score_now`/`over_budget`。
    """
    p = g.p
    pid = getattr(p, "pid", None)
    winner_idx = int(winner_idx)
    loser_idx = int(loser_idx)
    if winner_idx <= 0:
        raise ValueError("winner_idx must be > 0 (got %d)" % winner_idx)

    # D-17: 前置校验 kinds，防止裸 KeyError
    try:
        kinds = validate_kinds(kinds)
    except ValueError as _ke:
        if filter_info is not None:
            filter_info["blocked_by"] = "invalid_peace_kind"
            filter_info["valid_kinds"] = list(VALID_DEMAND_KINDS)
            filter_info["safe_kinds"] = list(SAFE_OPTION_KINDS)
            filter_info["default_kinds"] = ["state"]
        raise

    want_replace_all = bool(replace_all)
    want_replace_loser = (not want_replace_all) and (bool(replace) or (not bool(append)))
    effective_append = (not want_replace_all) and (not want_replace_loser)

    wanted_states = None
    if states is not None:
        # 去重并严格保留调用方输入顺序
        wanted_states = []
        seen_w = set()
        for s in states:
            si = int(s)
            if si not in seen_w:
                seen_w.add(si)
                wanted_states.append(si)
        if not wanted_states:
            raise ValueError("states subset cannot be an empty list; omit `states` to demand all")

    new_acts = []
    available_states = []
    matched_states = []
    for kind in kinds:
        built = [
            act for act in build_actions(g, loser_idx, kind, winner_idx=winner_idx)
            if (0x10000 < act < 0x7F0000000000) and (act & 7) == 0
        ]
        if kind == "state":
            state_to_act = {}
            for act in built:
                sid = int(p.i32(act + 0x48))
                if sid not in state_to_act:
                    state_to_act[sid] = act
                    available_states.append(sid)
            if wanted_states is not None:
                # 严格按调用方传入 states 的顺序装配，绝不升序重排
                for sid in wanted_states:
                    act = state_to_act.get(sid)
                    if act is not None:
                        matched_states.append(sid)
                        new_acts.append(act)
            else:
                for sid in available_states:
                    matched_states.append(sid)
                    new_acts.append(state_to_act[sid])
        else:
            new_acts.extend(built)

    if wanted_states is not None and not new_acts:
        if filter_info is not None:
            filter_info["available_states"] = available_states
            filter_info["requested_states"] = wanted_states
            filter_info["matched_states"] = []
            filter_info["ignored_states"] = list(wanted_states)
        raise ValueError(
            "none of the requested states %s are available for loser %d (available: %s)"
            % (wanted_states, loser_idx, available_states)
        )

    idx_map = _tag_by_idx(g)
    existing_all = _read_existing_turn_actions(p, conf, winner_idx, map_off)
    dropped_acts = []
    preserved_other_by_loser = {}
    if effective_append:
        prev_acts = list(existing_all)
    elif want_replace_loser:
        # D-19: replace=True 仅替换当前 loser_idx，完整保留其它战败国的已排队动作
        prev_acts = []
        for a in existing_all:
            try:
                atype, lidx, sid = _action_key(p, a)
            except Exception:
                continue
            if lidx == loser_idx:
                dropped_acts.append((atype, lidx, sid))
            else:
                prev_acts.append(a)
                if atype == ACT_TYPE_TAKE_STATE and sid > 0:
                    lkey = idx_map.get(lidx, "#%d" % lidx)
                    preserved_other_by_loser.setdefault(lkey, []).append(sid)
    else:
        # replace_all=True: 显式清空整场会议全部旧动作，并透明回报所有被清除的战败国/州
        prev_acts = []
        for a in existing_all:
            try:
                dropped_acts.append(_action_key(p, a))
            except Exception:
                continue

    dropped_by_loser = {}
    for atype, lidx, sid in dropped_acts:
        if atype == ACT_TYPE_TAKE_STATE and sid > 0:
            lkey = idx_map.get(lidx, "#%d" % lidx)
            dropped_by_loser.setdefault(lkey, []).append(sid)

    merged_acts = []
    added_acts = []
    seen_keys = set()
    skipped_dup = 0
    for a in prev_acts:
        try:
            k = _action_key(p, a)
        except Exception:
            continue
        if k not in seen_keys:
            seen_keys.add(k)
            merged_acts.append(a)
    for a in new_acts:
        try:
            k = _action_key(p, a)
        except Exception:
            continue
        if k in seen_keys:
            skipped_dup += 1
            continue
        seen_keys.add(k)
        merged_acts.append(a)
        added_acts.append(a)

    turn = new_turn(g)
    if merged_acts:
        data = cmdwin.alloc(g, 8 * len(merged_acts))
        p.write(data, b"".join(struct.pack("<Q", a) for a in merged_acts))
        p.write(turn + 0x10, struct.pack("<QiiQ", data, len(merged_acts), len(merged_acts), _allocator(g)))
    install_turns(g, conf, winner_idx, [turn], map_off)

    # 汇总队列全貌与预算估算，并缓存给 peace_finish 做结算回读
    queued_by_loser = {}
    queued_states = []
    est_total_cost = 0
    for a in merged_acts:
        atype, lidx, sid = _action_key(p, a)
        est_total_cost += _action_cost(p, a)
        if atype == ACT_TYPE_TAKE_STATE and sid > 0:
            lkey = idx_map.get(lidx, "#%d" % lidx)
            queued_by_loser.setdefault(lkey, []).append(sid)
            queued_states.append(sid)
    est_added_cost = sum(_action_cost(p, a) for a in added_acts)
    score_now, score_total = _winner_score_info(g, winner_idx)
    over_budget = (
        bool(est_total_cost > int(score_now))
        if (score_now is not None and est_total_cost > 0)
        else False
    )

    _SUBMITTED_DEMANDS[(pid, int(conf))] = {
        "winner_idx": winner_idx,
        "states": list(queued_states),
        "queued_states_by_loser": {k: list(v) for k, v in queued_by_loser.items()},
        "estimated_total_cost": est_total_cost,
    }

    if filter_info is not None:
        if effective_append:
            filter_info["mode"] = "append"
        elif want_replace_all:
            filter_info["mode"] = "replace_all"
        else:
            filter_info["mode"] = "replace_loser"
        filter_info["replace_scope"] = (
            "all" if want_replace_all else ("loser" if want_replace_loser else "none")
        )
        filter_info["previous_actions"] = len(existing_all)
        filter_info["preserved_actions"] = len(prev_acts)
        filter_info["replaced_actions_count"] = len(dropped_acts)
        filter_info["added_actions"] = len(added_acts)
        filter_info["skipped_duplicate_actions"] = skipped_dup
        filter_info["total_queued_actions"] = len(merged_acts)
        filter_info["queued_states_by_loser"] = queued_by_loser
        if preserved_other_by_loser:
            filter_info["preserved_other_losers"] = {
                k: len(v) for k, v in preserved_other_by_loser.items()
            }
        if dropped_by_loser:
            filter_info["dropped_states_by_loser"] = dropped_by_loser
        if want_replace_all and dropped_by_loser:
            cur_ltag = idx_map.get(loser_idx, "#%d" % loser_idx)
            other_dropped = {k: v for k, v in dropped_by_loser.items() if k != cur_ltag}
            if other_dropped:
                filter_info["warning"] = (
                    "replace_all=True cleared previous demands for other loser(s): %s"
                    % ", ".join("%s(%d states)" % (k, len(v)) for k, v in sorted(other_dropped.items()))
                )
        filter_info["estimated_added_cost"] = est_added_cost
        filter_info["estimated_total_cost"] = est_total_cost
        filter_info["score_now"] = score_now
        filter_info["score_total"] = score_total
        filter_info["over_budget"] = over_budget
        if over_budget:
            filter_info["budget_warning"] = (
                "queued demands estimated cost (%d) exceeds current warscore (%s); "
                "later states in the queue may be truncated at peace_finish"
                % (est_total_cost, score_now)
            )
        filter_info["available_states"] = available_states
        if wanted_states is not None:
            filter_info["requested_states"] = wanted_states
            filter_info["matched_states"] = matched_states
            filter_info["ignored_states"] = [s for s in wanted_states if s not in set(matched_states)]

    return turn, merged_acts


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
