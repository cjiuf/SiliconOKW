"""Diplomacy: justifying war goals, declaring war, and related actions.

1.19.4.1 layout, re-derived 2026-11-04 from the binary itself
(rtti.pkl + .eh_frame_hdr function starts + disassembly):

  CDeclareWarAction          vtable 0x4359288  size 0x78  default ctor 0xA77450
  CGenerateWarGoalAction     vtable 0x4359718  size 0x90  default ctor 0xA7DB30
  CDiplomaticActionCommand   vtable 0x4358EB0  size 0x30  +0x24 tag, +0x28 action*
  CDiplomaticAction (base)   vtable 0x4359040
      +0x08 action type      +0x10/+0x14 sender tag {id,0}
      +0x18/+0x1c target tag +0x20 scope (embedded)  +0x6C CWarGoal handle
      +0x88 CWarGoalType* (GenerateWarGoal only)

The old constants (VT_DECLAREWAR 0x41AED30, FN_DECLAREWAR_CTOR 0xA4FB80) were
1.19.2 values: 0x41AED30 does not exist in this image at all and 0xA4FB80 lands in
the middle of an unrelated function, which is why every declare_war call died with
sig=11. Both constructors here take ONLY `this`; the sides and the war goal are
plain fields, so they are written directly (no guessed varargs).
"""
import struct
import sys

import offsets as O
import session_state as _SS

# 进程级已知 CTimedWargoalActivity* 指针追踪集（用于 1.5s 轮询零深扫感知正当化结束）
# key = int(pid) -> set[int]
_TRACKED_ACTIVITIES = {}
_SS.register_cache("diplomacy._TRACKED_ACTIVITIES", container=_TRACKED_ACTIVITIES)


def track_activity_ptr(g, ptr):
    """登记已知的 CTimedWargoalActivity* 指针，供 justifying_fast O(1) 跟踪其完成/消失。"""
    try:
        pid = int(getattr(getattr(g, "p", None), "pid", 0) or 0)
        addr = int(ptr, 16) if isinstance(ptr, str) else int(ptr)
    except Exception:
        return
    if not (0x10000 < addr < 0x7F0000000000):
        return
    bucket = _TRACKED_ACTIVITIES.setdefault(pid, set())
    if len(bucket) >= 32:
        bucket.clear()
    bucket.add(addr)

VT_DIPCMD           = 0x4358EB0   # CDiplomaticActionCommand
VT_DECLAREWAR       = 0x4359288   # CDeclareWarAction
VT_GENERATE_WARGOAL = 0x4359718   # CGenerateWarGoalAction
CTOR_DECLAREWAR     = 0xA77450    # void CDeclareWarAction(this) — DESERIALISATION ctor
CTOR_WARGOAL        = 0xA7DB30    # void CGenerateWarGoalAction(this) — ditto
# The real (parameterised) constructors. The zero-arg ones above only call the
# DEFAULT base ctor 0x3602650, which leaves from/to and the whole Scope block
# (+0x20..+0x48) empty; CanExecute then always returns false. These call the full
# base ctor 0xA6C2F0(this, from, to, scope, flag, -1) instead.
CTOR_DECLAREWAR_PARAM = 0xA77580  # (this, from*, to*, CWarGoal*, u8 flag, scope*)
CTOR_WARGOAL_PARAM    = 0xA7DD10  # (this, from*, to*, CWarGoalType*, scope*)
SCOPE_MAGIC = 0x29C77F8           # the 4-byte value every Scope block is built with
SZ_DECLAREWAR       = 0x78
SZ_WARGOAL          = 0x90
SZ_DIPCMD           = 0x30
G_GAMESTATE         = 0x459E9F0   # 1.19.4.1
RUN_CMD             = 0x20AE0A0   # bool CCommand::Run(cmd, std::string* err)

ACT_TYPE   = 0x08
ACT_FROM   = 0x10
ACT_TO     = 0x18
ACT_WARGOAL = 0x6C
WG_STATES  = 0x70
WG_TYPE    = 0x88

CO_TAG_INDEX = 0x08     # the CCountryTag inside CCountry (GER=1, POL=10 ...)


def wargoal_types(g):
    """War goal types in the game: name -> CWarGoalType*"""
    p = g.p
    out = {}
    hits = list(g.oi.instances("CWarGoalType"))
    if not hits:
        # A freshly re-attached process has no instances indexed yet, and the
        # empty list made every justify call report "unknown wargoal (options: )".
        try:
            hits = list(g.oi.deep_instances("CWarGoalType"))
        except Exception:
            hits = []
    for w in hits:
        try:
            import sys as _sys
            if _sys.platform == "win32":
                # 1.19.5: the name is a MSVC std::string at +0x18 (buffer),
                # size at +0x28, capacity at +0x30 -- not a char* at +0x10.
                # Reading +0x10 as a pointer produced garbage for every entry,
                # so the map came back empty and justify_wargoal rejected every
                # name ("unknown wargoal (options: )").
                sz = p.i64(w + 0x28)
                cap = p.i64(w + 0x30)
                if cap >= 16:
                    n = (p.read(p.u64(w + 0x18), sz).decode("utf-8", "replace")
                         if 0 < sz < 200 else "")
                else:
                    n = p.read(w + 0x18, max(0, min(sz, 15))).decode("utf-8", "replace")
            else:
                n = p.cstr(p.u64(w + 0x10))
        except Exception:
            continue
        if n and n.isprintable():
            out[n] = w
    return out


def tag_index(g, tag):
    return g.p.i32(g.country(tag).ptr + CO_TAG_INDEX)


def _post_action(g, act_ptr, tag_id):
    """Wrap a CDiplomaticAction* in a CDiplomaticActionCommand and Run it.

    The command object is a flat 0x30 bytes; CCommand::Run performs CanExecute and
    hands it to the queue — exactly what the diplomacy dialog does on "Send".
    No 1.19.2 constructor is called (FN_DIPCMD_CTOR/FN_DIPCMD_CANEXE are gone).
    """
    from commands import BASE_INIT
    p = g.p
    cmd = g.malloc(SZ_DIPCMD)
    blob = bytearray(SZ_DIPCMD)
    struct.pack_into("<Q", blob, 0x00, p.base + VT_DIPCMD)
    for off, fmt, val in BASE_INIT:
        struct.pack_into(fmt, blob, off, val)
    struct.pack_into("<i", blob, 0x24, int(tag_id))
    struct.pack_into("<Q", blob, 0x28, int(act_ptr))
    p.write(cmd, bytes(blob))
    rv = g.cmd.post(cmd)
    return bool(rv & 0xFF), ("" if (rv & 0xFF) else
                             (getattr(g.cmd, "last_error", "") or "CanExecute refused the action"))


def _dipcmd(g, act_ptr, tag_id):
    """A hand-built CDiplomaticActionCommand wrapping `act_ptr` (0x30 bytes, flat)."""
    from commands import BASE_INIT
    p = g.p
    cmd = g.malloc(SZ_DIPCMD)
    blob = bytearray(SZ_DIPCMD)
    struct.pack_into("<Q", blob, 0x00, p.base + VT_DIPCMD)
    for off, fmt, val in BASE_INIT:
        struct.pack_into(fmt, blob, off, val)
    struct.pack_into("<i", blob, 0x24, int(tag_id))
    struct.pack_into("<Q", blob, 0x28, int(act_ptr))
    p.write(cmd, bytes(blob))
    return cmd


def _can_execute_cmd(g, cmd):
    """Read-only CanExecute probe (vtable+0x50) on an already built command."""
    p = g.p
    fn = p.u64(p.base + VT_DIPCMD + 0x50)
    # CDiplomaticActionCommand::CanExecute (0xA68750, disassembled 2026-11-04:
    # reads [cmd+0x28]=action, [action+0x10]=sender id, returns a bool; no writes).
    import api
    api.allow_call(0xA68750)   # must be the IMAGE OFFSET, not the runtime address
    return bool(g.call(fn, (cmd, 0), absolute=True) & 0xFF)


def _can_execute(g, act_ptr, tag_id):
    """Read-only CanExecute probe (vtable+0x50) on a hand-built command."""
    cmd = _dipcmd(g, act_ptr, tag_id)
    try:
        return _can_execute_cmd(g, cmd)
    finally:
        try:
            g.free(cmd)
        except Exception:
            pass


def _i32buf(g, v):
    a = g.malloc(4)
    g.p.write(a, struct.pack("<i", int(v)))
    return a


def _scope_obj(g):
    """The 0x18-byte Scope block the parameterised constructors expect.

    Both of them read `[scope+0x08]` and copy it into the local scope struct
    (`{vtable 0x445CD48+0x10, <that value>, vtable 0x445CD48+0x60}`). The default
    constructors hard-code the same value (0x29C77F8) with a 32-bit immediate, so a
    fresh 0x18 buffer carrying it is enough to let the real ctor build the block.
    """
    s = g.malloc(0x18)
    g.p.write(s, struct.pack("<QQQ", 0, SCOPE_MAGIC, 0))
    return s


def _new_action_param(g, size, ctor, vtable, args):
    """Build an action with its PARAMETERISED constructor (the one the UI uses)."""
    p = g.p
    act = g.malloc(size)
    p.write(act, b"\0" * size)
    import api
    api.allow_call(ctor)
    g.call(ctor, [act] + list(args))
    got = p.u64(act)
    if got != p.base + vtable:
        raise RuntimeError("constructor %#x did not build the expected action "
                           "(vptr=%#x, want %#x) — refusing to post"
                           % (ctor, got, p.base + vtable))
    return act


def _new_action(g, size, ctor, vtable):
    p = g.p
    act = g.malloc(size)
    p.write(act, b"\0" * size)
    import api
    api.allow_call(ctor)
    g.call(ctor, [act])
    got = p.u64(act)
    if got != p.base + vtable:
        raise RuntimeError("constructor %#x did not build the expected action "
                           "(vptr=%#x, want %#x) — refusing to post"
                           % (ctor, got, p.base + vtable))
    return act


def _set_sides(g, act, src_tag, dst_tag):
    p = g.p
    i_from, i_to = tag_index(g, src_tag), tag_index(g, dst_tag)
    p.write(act + ACT_FROM, struct.pack("<ii", i_from, 0))
    p.write(act + ACT_TO, struct.pack("<ii", i_to, 0))
    return i_from, i_to


# ------------------------------------------------------------------ war goal
WARGOAL_NEEDS_STATES = ("take_state", "take_core_state", "take_claimed_state",
                        "take_state_focus")


def _state_array(g, act, state_ptrs):
    """Write CPdxArray<int32> at +0x70: data, cap, size, allocator.

    The elements are STATE IDS (4 bytes each), not CState* and not SHandles.
    Proof: the serializer for field 0x2E3B (0x84CA40) walks the array with
    `mov esi, DWORD PTR [r14 + rbx*4]` — a 4-byte stride. Writing 8-byte pointers
    or handles made the game crash (minidump) on the first real justification.
    """
    p = g.p
    n = len(state_ptrs)
    buf = g.malloc(max(1, n) * 4)
    if n:
        p.write(buf, b"".join(struct.pack("<i", p.i32(int(x) + 0x48)) for x in state_ptrs))
    p.write(act + WG_STATES, struct.pack("<QiiQ", buf, n, n, p.base + 0x4505128))


def justify_wargoal(g, target_tag, wargoal="take_state", states=(), source_tag=None,
                    dry_run=False):
    """Start justifying a war goal (the same command as the diplomacy dialog).

    `states` may be CState pointers (Game.justify_wargoal resolves ids) or ids.
    With no states and a state-based wargoal, every state of the target is tried
    in turn and the first one CanExecute accepts is used — that is exactly what a
    player does by clicking through the list.
    """
    if sys.platform == "win32":
        return False, "diplomacy.justify_wargoal uses Linux ELF offsets; use cmdwin.justify_wargoal on Windows PE"
    p = g.p
    wgs = wargoal_types(g)
    if wargoal not in wgs:
        return False, "unknown wargoal: %s (options: %s)" % (
            wargoal, ", ".join(sorted(wgs)))
    own = g._inj is None
    if own:
        g.attach()
    try:
        src = source_tag or g.player().tag
        act = _new_action_param(g, SZ_WARGOAL, CTOR_WARGOAL_PARAM, VT_GENERATE_WARGOAL,
                                [_i32buf(g, tag_index(g, src)),
                                 _i32buf(g, tag_index(g, target_tag)),
                                 wgs[wargoal], _scope_obj(g)])
        i_from = tag_index(g, src)

        if states:
            cands = [list(states)]
        elif wargoal in WARGOAL_NEEDS_STATES:
            try:
                sts = [s["ptr"] for s in g.states(target_tag)]
            except Exception:
                sts = []
            # Danzig first (85) — the historical German claim — then the rest.
            sts.sort(key=lambda x: 0 if p.i32(x + 0x48) == 85 else 1)
            cands = [[x] for x in sts]
        else:
            cands = [[]]
        if not cands:
            return False, "the target owns no state to take"

        tried = []
        for st in cands:
            _state_array(g, act, st)
            # ALWAYS pre-check CanExecute. Posting an action whose fields the game
            # does not fully understand faults the process (observed: sig=11 rip=0x21
            # when the state array held pointers). A refused pre-check costs nothing.
            ok = _can_execute(g, act, i_from)
            err = "" if ok else "CanExecute false"
            if ok and not dry_run:
                ok, err = _post_action(g, act, i_from)
            ids = [p.i32(x + 0x48) for x in st]
            tried.append({"states": ids, "ok": ok})
            if ok:
                return True, ""
            if len(tried) >= 12:
                break
        return False, "CanExecute refused every attempt: %s" % tried
    finally:
        if own:
            g.detach()


# ------------------------------------------------------------------ declare war
# ---------------------------------------------------------------- CWarGoal
# Offsets inside CWarGoal, re-derived 2026-11-13 from the ctor at 0x258C880.
#
# sizeof(CWarGoal) = 0x90 (the only `new` feeding the ctor allocates 0x90). The
# ctor itself writes only +0x08 (an id copied from a global), +0x10 (a flag
# byte), the embedded Scope at +0x18..+0x2F (vtables 0x445CD48+0x10 / +0x60,
# magic 0x29C77F8), +0x30, and three CPdxArrays at +0x48/+0x60/+0x78.
# owner/target/type are filled in afterwards by CAddWarGoalAction / the
# deserialiser:
#
#     +0x38  i32  owner  country index   (GER = 1)
#     +0x3C  i32  target country index   (DEN = 25)
#     +0x40  u64  CWarGoalType*          (0x560A45390660 == take_state)
#
# Verified live: the CWarGoal produced by a GER->DEN take_state justification
# read exactly (owner=1, target=25, type=take_state). The +0x10/+0x14 pair the
# old code used read (1, 1) on that same object, i.e. it was never owner/target
# and every "owner_tag" this tool printed before 2026-11-13 was noise.
WG_OWNER = 0x38
WG_TARGET = 0x3C
WG_TYPE = 0x40
WG_ID = 0x08
# 2026-11-23 (复测 7 B-1): 实机纯只读验证 7/7 命中 —— CDiplomacyStatus+0x80 为该国当前所有活跃
# CWarGoal* 的 CPdxArray（CCountry+0x530 为被宣称列表）。0.2ms O(1) 直读，彻底告别
# live_instances 漏新 arena 与 deep_instances 多 GB 超时扫描。
DP_WARGOALS = 0x80


def _ensure_tags_map(g):
    """确保国家 tag_id -> TAG 映射就绪（兼容新实例尚未 _load_tags 的情况）。"""
    try:
        if hasattr(g, "_load_tags"):
            g._load_tags()
    except Exception:
        pass
    tags = getattr(g, "_tags", None) or []
    id2tag = {}
    try:
        import combat
        if getattr(g, "p", None) is not None:
            id2tag = combat._country_id_map(g) or {}
    except Exception:
        id2tag = {}

    def _resolve(i):
        if i in id2tag:
            return id2tag[i]
        if 0 <= i < len(tags):
            return tags[i]
        return None

    return _resolve


def _target_country_exists(g, target_tag, owner_tag=None):
    """O(1) 纯只读判定目标国家是否仍然存在（拥有 >= 1 州）；离线未填 CO_STATES 的 mock 保守返回 True。

    2026-11-26 (长跑 §6 根治): 和会完全吞并某国（如 HOL）后，CDiplomacyStatus+0x80
    可能残留指向已灭亡国家（0 州）的旧 CWarGoal*；不剔除会导致 CB_AVAILABLE 红点长期霸屏。
    物理不变量防误杀：先核验发起国自身 CO_STATES 计数 > 0，仅在 CO_STATES 布局可读且有效时
    才判定目标国 0 州为已灭亡（离线轻量 mock 内存未填 CO_STATES 时 owner 计数也为 0，直接保守放行）。
    """
    if not target_tag:
        return True
    p = getattr(g, "p", None)
    if p is None or not hasattr(g, "country"):
        return True
    try:
        from game import _count, CO_STATES
        # 先用发起国（或玩家国）验证 CO_STATES 在当前进程/桩中是否真实可读且非空
        ref_tag = owner_tag
        if not ref_tag and hasattr(g, "player_tag"):
            try:
                ref_tag = g.player_tag()
            except Exception:
                ref_tag = None
        if ref_tag:
            rc = g.country(ref_tag)
            rptr = getattr(rc, "ptr", None)
            if not rptr or not (0x10000 < int(rptr) < 0x7F0000000000):
                return True
            if int(_count(p, int(rptr), CO_STATES)) <= 0:
                return True
        c = g.country(target_tag)
        cptr = getattr(c, "ptr", None)
        if not cptr or not (0x10000 < int(cptr) < 0x7F0000000000):
            return True
        cnt = int(_count(p, int(cptr), CO_STATES))
        return cnt > 0
    except Exception:
        return True


def _decode_wargoal_obj(p, w, inv_types, resolve_tag, exists_fn=None):
    """把单个 CWarGoal* 指针解码为标准化 dict。"""
    wid = p.u64(w + WG_ID)
    owner = p.i32(w + WG_OWNER)
    target = p.i32(w + WG_TARGET)
    tptr = p.u64(w + WG_TYPE)
    tname = inv_types.get(tptr)
    ow_tag = resolve_tag(owner)
    tg_tag = resolve_tag(target)
    tgt_exists = True if (exists_fn is None or not tg_tag) else bool(exists_fn(tg_tag))
    rec = {
        "ptr": hex(w),
        "id": wid,
        "owner_index": owner,
        "target_index": target,
        "owner_tag": ow_tag,
        "target_tag": tg_tag,
        "wargoal": tname,
        "target_exists": tgt_exists,
    }
    rec["valid"] = (
        bool(wid) and tname is not None
        and 0 <= owner < 4096 and 0 <= target < 4096
        and tgt_exists
    )
    if not tgt_exists:
        rec["invalid_reason"] = "target_annexed"
    rec["tags_resolved"] = bool(ow_tag and tg_tag)
    return rec


def country_wargoals_direct(g, tag=None, valid_only=True):
    """Windows 1.19.5 O(1) 权威纯只读直读：CCountry+0xF88 (CDiplomacyStatus) -> +0x80 CPdxArray<CWarGoal*>。

    返回 (records, ok)。ok=True 表示引擎结构直读成功（即使列表为空也证明确实无 CB）；
    ok=False 表示非 Windows 或底层指针不可读，应由调用方决定是否回退旧路径。
    """
    import sys as _sys
    if _sys.platform != "win32":
        return [], False
    p = getattr(g, "p", None)
    if p is None:
        return [], False

    co_dip_off = O.CO_DIPLOMACY_STATUS
    resolve_tag = _ensure_tags_map(g)
    try:
        types = wargoal_types(g)
    except Exception:
        types = {}
    inv = {v: k for k, v in (types or {}).items()}
    exists_cache = {}

    def _exists(t):
        if t not in exists_cache:
            exists_cache[t] = _target_country_exists(g, t, owner_tag=tag)
        return exists_cache[t]

    if tag:
        try:
            c_ptrs = [g.country(tag).ptr]
        except Exception:
            return [], False
    else:
        try:
            c_ptrs = list(g.country_ptrs() or [])
        except Exception:
            return [], False
        if not c_ptrs:
            return [], False

    out = []
    seen = set()
    read_any_dip = False
    for cptr in c_ptrs:
        try:
            dip_s = p.u64(cptr + co_dip_off)
            if not (0x10000 < dip_s < 0x7F0000000000):
                continue
            read_any_dip = True
            data, cap, sz = struct.unpack("<Qii", p.read(dip_s + DP_WARGOALS, 16))
            if not (0x10000 < data < 0x7F0000000000) or sz <= 0 or sz > 256:
                continue
            ptrs = struct.unpack("<%dQ" % sz, p.read(data, sz * 8))
        except Exception:
            continue
        for w in ptrs:
            if not (0x10000 < w < 0x7F0000000000) or w in seen:
                continue
            seen.add(w)
            try:
                rec = _decode_wargoal_obj(p, w, inv, resolve_tag, exists_fn=_exists)
                rec["source"] = "CDiplomacyStatus+0x80"
                if valid_only and not rec["valid"]:
                    continue
                out.append(rec)
            except Exception:
                continue
    return out, read_any_dip


def wargoals(g, tag=None, deep=None, valid_only=False):
    """Live CWarGoal objects, decoded.

    2026-11-23 (复测 7 B-1): Windows 1.19.5 优先走 O(1) 实时直读
    `CCountry+0xF88 -> CDiplomacyStatus+0x80`（0.2ms，100% 包含新造 CB 与国策 CB，
    不再依赖易漏新 arena 的 `oi.live_instances` 或动辄 5 秒的 multi-GB 深扫）。
    仅在非 Windows、直读失败或显式 `deep=True` 时合并 RTTI 扫描。
    """
    p = g.p
    resolve_tag = _ensure_tags_map(g)
    direct_rows, direct_ok = country_wargoals_direct(g, tag=tag, valid_only=valid_only)
    if direct_ok and deep is not True:
        return direct_rows

    types = wargoal_types(g)
    inv = {v: k for k, v in types.items()}
    out = list(direct_rows)
    seen = {int(r["ptr"], 16) for r in out if isinstance(r.get("ptr"), str)}

    want_owner = tag_index(g, tag) if tag else None
    auto = deep is None
    hits = []
    try:
        hits = list(g.oi.live_instances("CWarGoal"))
    except Exception:
        hits = []
    if deep:
        try:
            for a in g.oi.deep_instances("CWarGoal"):
                if a not in hits:
                    hits.append(a)
        except Exception:
            pass
    exists_cache = {}
    for w in hits:
        if w in seen:
            continue
        seen.add(w)
        try:
            rec = _decode_wargoal_obj(
                p, w, inv, resolve_tag,
                exists_fn=lambda t: exists_cache.setdefault(t, _target_country_exists(g, t)),
            )
            if want_owner is not None and rec["owner_index"] != want_owner:
                continue
            if valid_only and not rec["valid"]:
                continue
            out.append(rec)
        except Exception:
            pass
    if auto and not direct_ok and not any(x["valid"] for x in out):
        # The fast arena scan found nothing usable; a CB created in a fresh arena
        # is invisible to it, so pay for the full writable-memory walk once.
        return wargoals(g, tag=tag, deep=True, valid_only=valid_only)
    return out


def find_wargoal(g, target_tag, source_tag=None, deep=None):
    """The player's live casus belli against `target_tag`, or None.

    Windows 上优先 O(1) 直读 `CDiplomacyStatus+0x80`（权威实时，永不漏新造 CB）；
    仅在直读不可用（如 Linux 或离线 mock）时回退至 `wargoals()`。
    """
    src = str(source_tag or g.player().tag).upper()
    tgt = str(target_tag).upper()
    i_src, i_tgt = tag_index(g, src), tag_index(g, tgt)

    # 1. Windows O(1) 实时权威路径（0.2ms，新造 CB / 国策 CB 全可见）
    direct_rows, direct_ok = country_wargoals_direct(g, tag=src, valid_only=True)
    if direct_ok:
        for w in direct_rows:
            if w["owner_index"] == i_src and w["target_index"] == i_tgt:
                return w
        # 仅在显式要求 deep=True 时再补做一次全堆核验；常规权威直读未命中即确定无 CB
        if deep is not True:
            return None

    for w in wargoals(g, tag=src, deep=deep, valid_only=True):
        if w["owner_index"] == i_src and w["target_index"] == i_tgt:
            return w
    if deep is False:
        return None
    if deep is not True:
        import time as _t
        _now = _t.time()
        if _now - getattr(g, "_deep_wargoal_at", 0.0) <= 120.0:
            return None
        g._deep_wargoal_at = _now
        for w in wargoals(g, tag=src, deep=True, valid_only=True):
            if w["owner_index"] == i_src and w["target_index"] == i_tgt:
                return w
    return None


def declare_war(g, target_tag, wargoal_ptr=None, flag=0, dry_run=False):
    """Declare war. Requires a valid war goal (justify one first)."""
    if sys.platform == "win32":
        return False, "diplomacy.declare_war uses Linux ELF offsets; use cmdwin.declare_war on Windows PE"
    p = g.p
    own = g._inj is None
    if own:
        g.attach()
    try:
        src = g.player().tag
        i_from = tag_index(g, src)
        # 2026-11-13: CWarGoal DOES exist (vtable 0x4477D88, sizeof 0x90) — the old
        # "no RTTI class" note was wrong, it just has no `C*Action` name so the
        # diploscan cache never listed it. `find_wargoal` returns the one CB whose
        # (+0x38 owner, +0x3C target) matches, which is exactly the pointer the
        # diplomacy dialog hands to CDeclareWarAction. Passing the OTHER vtable hits
        # (freed blocks) is what produced the sig=11 crashes.
        cb = find_wargoal(g, target_tag, src) if not wargoal_ptr else None
        cands = ([int(wargoal_ptr)] if wargoal_ptr
                 else ([int(cb["ptr"], 16)] if cb else []))
        # A NULL war goal is only meaningful while the `no_wargoal` cheat is on, and
        # only POSTING it is safe: CDeclareWarAction::CanExecute dereferences the
        # pointer (sig=11 rip=+0x... with rax=0), so a dry run must never probe 0.
        if not dry_run and 0 not in cands:
            cands.append(0)
        if dry_run and not cands:
            live = [w for w in wargoals(g, deep=False) if w["valid"]]
            return False, ("no live casus belli against %s (the game refuses a war "
                           "without a wargoal). Valid war goals on the board: %s"
                           % (target_tag, [(w["owner_tag"], w["target_tag"], w["wargoal"])
                                           for w in live] or "none"))
        tried = []
        for wg in cands:
            try:
                act = _new_action_param(
                    g, SZ_DECLAREWAR, CTOR_DECLAREWAR_PARAM, VT_DECLAREWAR,
                    [_i32buf(g, i_from), _i32buf(g, tag_index(g, target_tag)),
                     wg, int(flag), _scope_obj(g)])
            except Exception as e:
                tried.append({"wargoal": hex(wg), "error": str(e)})
                continue
            if dry_run:
                ok = _can_execute(g, act, i_from)
                tried.append({"wargoal": hex(wg), "applicable": ok})
                if ok:
                    return True, "applicable (dry_run: nothing was sent)"
                continue
            ok, err = _post_action(g, act, i_from)
            tried.append({"wargoal": hex(wg), "ok": ok, "error": err})
            if ok:
                return True, ""
        if dry_run:
            return False, "no war goal was applicable: %s" % tried
        if not cb and not wargoal_ptr:
            live = [w for w in wargoals(g, deep=False) if w["valid"]]
            return False, ("no live casus belli against %s (the game refuses a war "
                           "without a wargoal). Valid war goals on the board: %s"
                           % (target_tag, [(w["owner_tag"], w["target_tag"], w["wargoal"])
                                           for w in live] or "none"))
        return False, ("the game refused every war goal (%d tried). Last: %s"
                       % (len(tried), tried[-1] if tried else ""))
    finally:
        if own:
            g.detach()


def _load_action_cache():
    """读取预生成的 diploactions 缓存（Linux ELF diploscan 已迁出 calling/）。"""
    import os
    import safe_cache
    cache = os.path.join(os.path.dirname(os.path.abspath(__file__)), "diploactions.pkl")
    if not safe_cache.exists(cache):
        return {}
    return safe_cache.load(cache)


def action_catalog():
    """All diplomatic action classes: {name: {vtable, ctor, size}}"""
    raw = _load_action_cache()
    out = {}
    allmap = raw.get("_all") if isinstance(raw, dict) else None
    if isinstance(allmap, dict):
        for name, info in allmap.items():
            ctors = info.get("ctors") or []
            pick = None
            for c in ctors:
                sizes = c.get("sizes") or []
                if sizes:
                    pick = (c.get("addr"), sizes[0])
                    break
            if not pick and ctors:
                pick = (ctors[0].get("addr"), 112)
            if pick and pick[0]:
                out[name] = {"vtable": info.get("vtable"), "ctor": pick[0], "size": pick[1]}
    for name, info in (raw or {}).items():
        if name == "_all" or not isinstance(info, dict):
            continue
        if "ctor" in info and "size" in info:
            out[name] = {"vtable": info.get("vtable"), "ctor": info["ctor"], "size": info["size"]}
    return out


def build_action(g, action_class, target_tag, source_tag=None):
    """Build an action object with its default ctor, then write the two sides."""
    cat = action_catalog()
    if action_class not in cat:
        return None, "unknown action: %s" % action_class
    info = cat[action_class]
    if action_class == "CDeclareWarAction":
        info = {"vtable": VT_DECLAREWAR, "ctor": CTOR_DECLAREWAR, "size": SZ_DECLAREWAR}
    elif action_class == "CGenerateWarGoalAction":
        info = {"vtable": VT_GENERATE_WARGOAL, "ctor": CTOR_WARGOAL, "size": SZ_WARGOAL}
    p = g.p
    try:
        act = _new_action(g, info["size"], info["ctor"], info["vtable"])
    except Exception as e:
        return None, str(e)
    i_from, i_to = _set_sides(g, act, source_tag or g.player().tag, target_tag)
    if p.i32(act + ACT_FROM) != i_from or p.i32(act + ACT_TO) != i_to:
        return None, "%s: sides were not written" % action_class
    return act, None


def send_action(g, action_class, target_tag, source_tag=None, dry_run=False):
    own = g._inj is None
    if own:
        g.attach()
    try:
        act, err = build_action(g, action_class, target_tag, source_tag)
        if err:
            return False, err
        tag_id = tag_index(g, g.player().tag)
        if dry_run:
            ok = _can_execute(g, act, tag_id)
            return ok, ("applicable (dry_run: nothing was sent)" if ok
                        else "not applicable (conditions are not met)")
        return _post_action(g, act, tag_id)
    finally:
        if own:
            g.detach()


def _add_game_days(g, days):
    """当前游戏日期 + days 天 -> 'YYYY.MM.DD'。"""
    try:
        from api import date_to_parts
        y, m, d, _h = date_to_parts(int(g.date_raw() + float(days) * 24))
        return "%04d.%02d.%02d" % (y, m, d)
    except Exception:
        return None


# ---------------------------------------------------------------- 工期模型
# 1.19.5.0 游戏原生数据：common/wargoals/00_invasion.txt
#   take_state = { generate_base_cost = 200  generate_per_state_cost = 50 }
# NDiplomacy 常数（common/defines/00_defines.lua）：
#   WARGOAL_PER_JUSTIFY_AND_WAR_COST_FACTOR = 1.5    （每多一个同存正当化/交战对象 x1.5）
#   BASE_GENERATE_WARGOAL_DAILY_PP          = 0.2    （每天烧 0.2 PP）
#   POLITICAL_POWER_UPPER_CAP               = 2000.0
# 注意（2026-11-26 A7 核证）：
#   defines 中的 WARGOAL_JUSTIFY_TENSION_FROM_PRODUCTION = 30.0 是世界紧张度增长系数，
#   不是正当化天数！旧代码误将其当作 +30 天加进基础工期，导致单州无并发高估 30 天 / 6 PP
#   （实机 1936 GER->HOL take_state 真实为 200 天 / 40 PP，非 230 天 / 46 PP）。
WARGOAL_BASE_COST = 200.0
WARGOAL_PER_STATE_COST = 50.0
WARGOAL_PRODUCTION_TERM = 0.0   # 保留符号兼容外部引用；非天数项，已归零
WARGOAL_PER_COUNTRY = 1.5
WARGOAL_DAILY_PP = 0.2
POLITICAL_POWER_CAP = 2000.0

# 2026-11-26 (A1 PE 1.19.5.0 反汇编实锤 O(1) 根治):
# 反汇编 CIsJustifyingWargoalAgainst (0x3f9490) -> CDiplomacyStatus::IsJustifyingWargoalAgainst (0xda4a10)
# 证实引擎在每个国家直接维护两处 O(1) 数组，无需扫描 3.8 GB 堆即可获知正当化活动：
#   1) CCountry + 0xF88 (CDiplomacyStatus*) + 0xE0 -> CPdxArray<CCountryTag> (data +0xE0, cap +0xE8, size +0xEC)
#   2) CCountry + 0xF90 (CPoliticalStatus*) + 0x08 -> CPdxArray<CTimedActivity*> (data +0x08, cap +0x10, size +0x14)
WIN_CO_POL_STATUS       = 0xF90
WIN_DS_JUSTIFYING_TAGS  = 0xE0
WIN_PS_TIMED_ACTIVITIES = 0x08


def _estimate_days(n_states, n_concurrent):
    base = WARGOAL_BASE_COST + WARGOAL_PER_STATE_COST * max(0, int(n_states) - 1)
    return round(base * (WARGOAL_PER_COUNTRY ** max(0, int(n_concurrent))), 1)


def _win_direct_timed_activities(g, want=None):
    """Windows PE 纯只读 O(1) 读 CPoliticalStatus+0x08 与 CDiplomacyStatus+0xE0。

    返回 (ptrs, has_justifying_flag, direct_ok)：
      - ptrs: [(owner_tag, activity_ptr), ...]
      - has_justifying_flag: 任意被检国家 CDiplomacyStatus+0xE0 的 size > 0
      - direct_ok: 是否成功通过引擎结构体完成校验（离线 mock 或无效内存时返回 False 以走兜底）
    """
    p = getattr(g, "p", None)
    if p is None or not hasattr(g, "country"):
        return [], False, False
    if want:
        tags = [str(want).upper()]
    else:
        try:
            tags = list(g.tags() or ())
        except Exception:
            return [], False, False
    if not tags:
        return [], False, False

    ptrs = []
    seen = set()
    has_flag = False
    checked = 0

    # 批量扫描多国时优先使用 fastmem.read_batch（两趟批读覆盖 439 国，<1ms）
    if not want and len(tags) > 8 and hasattr(g, "_load_tags") and hasattr(g, "country_ptrs"):
        try:
            import fastmem as _fm
            g._load_tags()
            cptrs = g.country_ptrs() or []
            gtags = getattr(g, "_tags", None) or []
            t2i = getattr(g, "_tag2idx", None) or []
            pairs = []
            for tid, t in enumerate(gtags):
                if not t or t == "---" or tid >= len(t2i):
                    continue
                ci = t2i[tid]
                if 0 <= ci < len(cptrs):
                    cp = int(cptrs[ci])
                    if 0x10000 < cp < 0x7F0000000000:
                        pairs.append((t, cp))
            if pairs:
                # WIN_CO_DIPLO_STATUS (0xF88) 与 WIN_CO_POL_STATUS (0xF90) 紧邻，一次读 16B
                hdrs = _fm.read_batch(p, [(cp + WIN_CO_DIPLO_STATUS, 16) for _, cp in pairs])
                if hdrs is not None and len(hdrs) == len(pairs):
                    valid_sub = []
                    sub_reqs = []
                    for (t, _cp), raw in zip(pairs, hdrs):
                        if not raw or len(raw) < 16:
                            continue
                        dip, pol = struct.unpack("<QQ", raw)
                        if 0x10000 < dip < 0x7F0000000000 and 0x10000 < pol < 0x7F0000000000:
                            valid_sub.append(t)
                            # dip+0xE8: (j_cap, j_sz) 8B; pol+0x08: (a_data, a_cap, a_sz) 16B
                            sub_reqs.append((dip + WIN_DS_JUSTIFYING_TAGS + 0x08, 8))
                            sub_reqs.append((pol + WIN_PS_TIMED_ACTIVITIES, 16))
                    sub_raw = _fm.read_batch(p, sub_reqs) if sub_reqs else []
                    if sub_raw is not None and len(sub_raw) == len(sub_reqs):
                        for idx, t in enumerate(valid_sub):
                            j_raw = sub_raw[idx * 2]
                            a_raw = sub_raw[idx * 2 + 1]
                            if not j_raw or len(j_raw) < 8 or not a_raw or len(a_raw) < 16:
                                continue
                            j_cap, j_sz = struct.unpack("<ii", j_raw)
                            a_data, a_cap, a_sz = struct.unpack("<Qii", a_raw)
                            if not (0 <= j_sz <= 64 and 0 <= j_cap <= 4096 and j_sz <= max(j_cap, 0)):
                                continue
                            if not (0 <= a_sz <= 64 and 0 <= a_cap <= 4096 and a_sz <= max(a_cap, 0)):
                                continue
                            checked += 1
                            if j_sz > 0:
                                has_flag = True
                            if a_sz > 0 and 0x10000 < a_data < 0x7F0000000000:
                                try:
                                    ap_raw = p.read(a_data, a_sz * 8)
                                    for ap in struct.unpack("<%dQ" % a_sz, ap_raw):
                                        if (0x10000 < ap < 0x7F0000000000) and ap not in seen:
                                            seen.add(ap)
                                            ptrs.append((t, ap))
                                except Exception:
                                    continue
                        if checked > 0:
                            return ptrs, has_flag, True
        except Exception:
            ptrs, seen, has_flag, checked = [], set(), False, 0

    for t in tags:
        try:
            co = g.country(t)
            cptr = getattr(co, "ptr", 0)
            if not (0x10000 < int(cptr) < 0x7F0000000000):
                continue
            dip = p.u64(cptr + WIN_CO_DIPLO_STATUS)
            pol = p.u64(cptr + WIN_CO_POL_STATUS)
            if not (0x10000 < dip < 0x7F0000000000 and 0x10000 < pol < 0x7F0000000000):
                continue
            j_sz = p.i32(dip + WIN_DS_JUSTIFYING_TAGS + 0x0C)
            j_cap = p.i32(dip + WIN_DS_JUSTIFYING_TAGS + 0x08)
            a_sz = p.i32(pol + WIN_PS_TIMED_ACTIVITIES + 0x0C)
            a_cap = p.i32(pol + WIN_PS_TIMED_ACTIVITIES + 0x08)
            if not (0 <= j_sz <= 64 and 0 <= j_cap <= 4096 and j_sz <= max(j_cap, 0)):
                continue
            if not (0 <= a_sz <= 64 and 0 <= a_cap <= 4096 and a_sz <= max(a_cap, 0)):
                continue
            checked += 1
            if j_sz > 0:
                has_flag = True
            if a_sz > 0:
                a_data = p.u64(pol + WIN_PS_TIMED_ACTIVITIES)
                if 0x10000 < a_data < 0x7F0000000000:
                    for i in range(a_sz):
                        ap = p.u64(a_data + i * 8)
                        if (0x10000 < ap < 0x7F0000000000) and ap not in seen:
                            seen.add(ap)
                            ptrs.append((t, ap))
        except Exception:
            continue
    return ptrs, has_flag, (checked > 0)


def _country_timed_activities(g):
    """CTimedWargoalActivity* read straight from CPoliticalStatus+0x08.

    fieldmaps.pkl: CPoliticalStatus {8: ('timed_activities', 'object')} and the
    array uses the same CPdxArray layout as PL_PARTIES (+0x20): data +0, cap +8,
    size +0xC. vtable 0x4398978 is 21CTimedWargoalActivity in rtti.pkl.

    2026-11-26 (A1): Windows PE 同样直接走 CPoliticalStatus+0x08 / CDiplomacyStatus+0xE0
    的 O(1) 读取，避免每次无正当化时穿透到 3.8 GB 全堆扫描；仅当 direct 不可用或
    dip 标记存在正当化但 pol 列表为空时才回退到 RTTI 索引。
    """
    import sys
    if sys.platform == "win32":
        direct, has_flag, direct_ok = _win_direct_timed_activities(g, want=None)
        if direct:
            return direct
        if direct_ok and not has_flag:
            return []
        try:
            return [(None, a) for a in g.oi.instances("CTimedWargoalActivity")]
        except Exception:
            return []
    from game import _arr, CO_POLITICS
    p = g.p
    try:
        vt = p.va(0x4398978)
    except Exception:
        try:
            vt = g.base + 0x4398978
        except Exception:
            return []
    out, seen = [], set()
    try:
        tags = list(g.tags())
    except Exception:
        return out
    for t in tags:
        try:
            pol = p.u64(g.country(t).ptr + CO_POLITICS)
            if not pol:
                continue
            for a in _arr(p, pol, 0x08, limit=64):
                if not a or a in seen or a < 0x10000:
                    continue
                try:
                    if p.u64(a) == vt:
                        seen.add(a)
                        out.append((t, a))     # (owner tag, activity ptr)
                except Exception:
                    continue
        except Exception:
            continue
    return out


def justifying(g, include_queued=False):
    """所有正在进行的正当化（读引擎真实对象，不是猜的）。

    1.19.4.1 `CTimedWargoalActivity` 布局（2026-11-12 用活进程重新互证）：
      +0x08 int32 活动 id
      +0x0C int32 发起国 tag id         （实测 GER=1）
      +0x10 int32 目标国 tag id         （实测 HOL=8 / BEL=7）
      +0x14 int32 未知/变体字段（HOL take_state=0，BEL take_state=1，不是类型也不是发起国）
      +0x18 int64 总时长，定点 x1e5，单位=天
      +0x20 int64 每日进度，定点 x1e5，单位=天/天
      +0x28 int64 已过天数，定点 x1e5
    旧注释把 +0x14 当成“发起国”，于是 take_state 的活动读出 from=0，被
    `src not in idx2tag` 直接丢掉——正当化明明在跑却报 not_started。
    旧实现逐 8 字节猜 (t1,t2) 并把 +0x10 当发起国，所以把 GER->HOL 印成了
    HOL->GER（方向反了），也没有 days_left / eta_date。

    2026-11-26: Windows PE 优先走 `_win_direct_timed_activities()` O(1) 直读；
    当所有国家 `CDiplomacyStatus+0xE0` 与 `CPoliticalStatus+0x08` 均权威确认为空时，
    直接短路返回 `[]`，不再穿透到 3.8 GB 全堆扫描（`CTimedWargoalActivity` /
    `CGenerateWarGoalAction`）。仅在 `include_queued=True` 或直读失败时才回退堆扫。
    """
    custom = getattr(g, "_mock_justifying", None)
    if custom is not None:
        raw = custom(None) if callable(custom) else list(custom)
        return [dict(r) for r in (raw or []) if isinstance(r, dict)]
    p = g.p
    out = []

    import sys as _sys
    _pe = _sys.platform == "win32"
    _win_authoritative_empty = False

    def _bust_negative(cache_name):
        """Drop an EMPTY cache entry only: live=2s, deep=5s TTL. The old code
        cached [] before the engine had built the activity and every later poll
        in the same 3s window read that []; a stale negative outlived the event."""
        c = getattr(g.oi, cache_name, None)
        if c and "CTimedWargoalActivity" in c and not c["CTimedWargoalActivity"][1]:
            c.pop("CTimedWargoalActivity", None)

    def _instances():
        """live_instances misses objects created after the index was built."""
        nonlocal _win_authoritative_empty
        if _pe:
            direct, has_flag, direct_ok = _win_direct_timed_activities(g, want=None)
            if direct:
                return direct
            if direct_ok and not has_flag:
                _win_authoritative_empty = True
                return []
            # 直读不可用（如离线测试桩）或 flag 指示存在正当化但 pol 未读出时，先查已索引对象
            inst_fn = getattr(getattr(g, "oi", None), "instances", None)
            if callable(inst_fn):
                try:
                    indexed = [(None, a) for a in (inst_fn("CTimedWargoalActivity") or ())]
                    if indexed:
                        return indexed
                except Exception:
                    pass
        else:
            direct = _country_timed_activities(g)
            if direct:
                return direct
        _bust_negative("_live_cache")
        live_fn = getattr(getattr(g, "oi", None), "live_instances", None)
        seen = list(live_fn("CTimedWargoalActivity")) if callable(live_fn) else []
        if seen:
            return seen
        _bust_negative("_deep_cache")
        try:
            # build(force=True) here re-scanned ~3.8 GB on every miss; the direct
            # read above already covers objects the index has never seen.
            deep_fn = getattr(getattr(g, "oi", None), "deep_instances", None)
            return list(deep_fn("CTimedWargoalActivity")) if callable(deep_fn) else []
        except Exception:
            return []

    insts = _instances()
    if not insts and _win_authoritative_empty and not include_queued:
        return []

    idx2tag = {}
    try:
        import combat as _cb_j
        idx2tag = _cb_j._country_id_map(g) or {}
    except Exception:
        idx2tag = {}
    if not idx2tag:
        for t in g.tags():
            try:
                idx2tag[tag_index(g, t)] = t
            except Exception:
                pass

    def _raw_date_to_str(raw_val):
        """将 CBaseTimedActivity 内部的 CGameDate (+0x38/+0x50) 转成 'YYYY.MM.DD'；无效哨兵返回 None。"""
        try:
            rv = int(raw_val)
            # 1930..2020 合理游戏小时范围（约 16,900,000 .. 17,700,000；无效哨兵为 0 或极大负数）
            if not (16_000_000 <= rv <= 18_500_000):
                return None
            from api import date_to_parts
            y, m, d, _h = date_to_parts(rv)
            if 1930 <= y <= 2020:
                return "%04d.%02d.%02d" % (y, m, d)
        except Exception:
            pass
        return None

    cur_date_raw = None
    if insts:
        try:
            cur_date_raw = int(g.date_raw())
        except Exception:
            cur_date_raw = None

    for item in insts:
        # _country_timed_activities() yields (owner_tag, ptr): the owner of the
        # CPoliticalStatus list IS the country that started the justification.
        owner, a = item if isinstance(item, tuple) else (None, item)
        try:
            if _pe:
                # 2026-11-15 live PE capture (GER->SIA justification created by
                # CGenerateWarGoalAction): +0x0C = activity id (65735),
                # +0x10 = SOURCE tag id (1 = GER), +0x14 = TARGET tag id
                # (62 = SIA). The Linux layout is the opposite (src at +0x0C,
                # tgt at +0x10); using it here printed HOL->GER backwards.
                src_tag = idx2tag.get(p.i32(a + 0x10))
                tgt = p.i32(a + 0x14)
            else:
                tgt = p.i32(a + 0x10)
                src_tag = owner if owner else idx2tag.get(p.i32(a + 0x0C))
            if tgt not in idx2tag or not src_tag or src_tag == idx2tag.get(tgt):
                continue
            total = p.i64(a + 0x18) / O.FIXED
            per = p.i64(a + 0x20) / O.FIXED
            # 2026-11-23 (复测 9 E-6 实锤根治):
            # 反汇编 PE 1.19.5.0 官方 UI 渲染函数 (0xb2c09d -> POLITICS_TIMED_ACTIVITY_DAY_PROGRESS)
            # 与官方剩余天数函数 CBaseTimedActivity::GetDaysLeft (0xb2adb0) 证实：
            #   * +0x18 = REQUIRED（总需求天数，定点 x1e5）
            #   * +0x28 = CURRENT（已完成天数，定点 x1e5）—— 单位就是天，绝不能除以 3！
            #     （旧代码拍脑袋加的 `done = done / 3.0` 把已走 210 天的进度硬算成 70 天、
            #       把只剩 5 天的借口谎报成 145 天！）
            #   * +0x20 = 每日进度速率/每日 PP 门槛（定点 x1e5，0xb2adb0 用 (REQUIRED-CURRENT)*1e5/per，
            #     0xb2b982 比较 CCountryPolitics+0xE0 < [a+0x20] 判定 POLITICS_PAUSED_NO_POLITICAL_POWER）
            #   * +0x38 / +0x50 = 起始 / 截止 CGameDate（若 +0x50 有效，0xb2adb0 取两者的较小值）
            done = p.i64(a + 0x28) / O.FIXED
            rem_raw = max(0.0, total - done)
            days_by_progress = (rem_raw / per) if per > 1e-6 else rem_raw

            start_date_raw = None
            end_date_raw = None
            try:
                start_date_raw = int(p.i32(a + 0x38))
                end_date_raw = int(p.i32(a + 0x50))
            except Exception:
                pass
            start_date_str = _raw_date_to_str(start_date_raw) if start_date_raw is not None else None
            end_date_str = _raw_date_to_str(end_date_raw) if end_date_raw is not None else None

            days = days_by_progress
            if end_date_str is not None and cur_date_raw is not None and end_date_raw is not None:
                days_by_end = max(0.0, (end_date_raw - cur_date_raw) / 24.0)
                if days_by_end < days:
                    days = days_by_end
            days = max(0.0, round(days, 2))

            # 对齐 0xb2b982: 发起国当前 PP < +0x20 门槛时正当化会暂停
            paused_by_pp = False
            try:
                if per > 0 and hasattr(g, "country"):
                    src_pp = getattr(g.country(src_tag), "political_power", None)
                    if src_pp is not None and float(src_pp) < float(per):
                        paused_by_pp = True
            except Exception:
                paused_by_pp = False

            eta = end_date_str if (end_date_str and cur_date_raw is not None and (end_date_raw - cur_date_raw) / 24.0 <= days_by_progress + 0.5) else (_add_game_days(g, days) if days is not None else None)
            track_activity_ptr(g, a)
            out.append({
                "ptr": hex(a),
                "from": src_tag,
                "target": idx2tag[tgt],
                "field_0x14": p.i32(a + 0x14),
                "source_class": "CTimedWargoalActivity",
                "days_left": days,
                "eta_date": eta,
                "start_date": start_date_str,
                "end_date": end_date_str,
                "progress_days": round(done, 2),
                "total_days": round(total, 2),
                "per_day": round(per, 3),
                "paused_by_pp": paused_by_pp,
                "progress": round(min(1.0, done / total), 3) if total > 0 else None,
                "remaining_pp_drain": round(days * WARGOAL_DAILY_PP, 1) if days is not None else None,
                # 2026-11-19: the state list the engine reads when this activity
                # completes (`movsxd rdx,[rax+rcx*4-4]` over +0x68/+0x74, used to
                # build DIPLOMACY_MESSAGE_TIMED_WARGOAL_FINISH_*).  0 here means
                # the game will AV at hoi4.exe+0xB2E62E the moment the counter
                # hits zero -- that is exactly what killed the 1936 campaign
                # twice (00:32 and 00:52, identical stacks).
                "states_in_activity": p.i32(a + 0x74),
            })
        except Exception:
            pass
    # 多条正当化活动存在时，按剩余天数升序排列，确保优先展示最快到期的借口
    out.sort(key=lambda r: (float("inf") if r.get("days_left") is None else float(r["days_left"]), str(r.get("target") or "")))
    if not out and (include_queued or not _win_authoritative_empty):
        # 命令已入队、引擎还没建出 activity 的那几帧（Windows 权威空默认不再触发 3.8 GB 深扫）
        try:
            _left = g.oi.deep_instances("CGenerateWarGoalAction")
        except Exception:
            _left = g.oi.live_instances("CGenerateWarGoalAction")
        for act in _left:
            try:
                src, tgt = p.i32(act + 0x10), p.i32(act + 0x18)
                if src in idx2tag and tgt in idx2tag and src != tgt:
                    out.append({"ptr": hex(act), "from": idx2tag[src],
                                "target": idx2tag[tgt],
                                "source_class": "CGenerateWarGoalAction",
                                "days_left": None, "eta_date": None,
                                "note": "queued, engine has not created the timed activity yet"})
            except Exception:
                pass
    return out


def country_wargoals_fast(g, tag=None, valid_only=True):
    """供 1.5s 轮询与红点 fast 巡检使用的 O(1) 纯只读 CB 查询（绝不触发全堆 deep_instances 慢扫）。

    返回 (rows, ok)。支持离线单测通过 `g._mock_wargoals` 注入。
    """
    custom = getattr(g, "_mock_wargoals", None)
    if custom is not None:
        raw = custom(tag) if callable(custom) else list(custom)
        want = str(tag).upper() if tag else None
        out = []
        for r in (raw or []):
            if not isinstance(r, dict):
                continue
            if want and str(r.get("owner_tag") or "").upper() != want:
                continue
            if valid_only and r.get("valid") is False:
                continue
            out.append(dict(r))
        return out, True

    rows, ok = country_wargoals_direct(g, tag=tag, valid_only=valid_only)
    if ok:
        return rows, True
    return [], False


def justifying_fast(g, tag=None):
    """供 1.5s 轮询与红点 fast 巡检使用的 O(1)/毫秒级正当化查询（绝不触发 deep_instances 全堆慢扫）。

    Windows 上合并已知追踪指针 `_TRACKED_ACTIVITIES` 与已就绪 `g._oi` 的轻量 arena 查询，
    并在解码时校验虚表类名与进度合法性，自动清理已释放或已完成的旧指针。
    支持离线单测通过 `g._mock_justifying` 注入。
    """
    want = str(tag).upper() if tag else None
    custom = getattr(g, "_mock_justifying", None)
    if custom is not None:
        raw = custom(tag) if callable(custom) else list(custom)
        out = []
        for r in (raw or []):
            if not isinstance(r, dict):
                continue
            sc = r.get("source_class")
            if sc and sc != "CTimedWargoalActivity":
                continue
            if want and str(r.get("from") or "").upper() != want:
                continue
            out.append(dict(r))
        return out

    p = getattr(g, "p", None)
    if p is None:
        return []

    pid = int(getattr(p, "pid", 0) or 0)
    cand_ptrs = []
    seen = set()

    def _add(addr):
        try:
            a = int(addr)
        except Exception:
            return
        if (0x10000 < a < 0x7F0000000000) and a not in seen:
            seen.add(a)
            cand_ptrs.append(a)

    if sys.platform == "win32":
        for a in list(_TRACKED_ACTIVITIES.get(pid) or ()):
            _add(a)
        # 2026-11-26 (A1 根治): 优先 O(1) 读取 CCountry+0xF90 (CPoliticalStatus+0x08) 与
        # CCountry+0xF88 (CDiplomacyStatus+0xE0)。当引擎原生数组明确为 0 正当化时，
        # 绝不再穿透 oi.live_instances("CTimedWargoalActivity") 触发 3.8 GB 全堆扫描！
        direct_ptrs, has_just_flag, direct_ok = _win_direct_timed_activities(g, want=want)
        for _owner, a in direct_ptrs:
            _add(a)
        need_rtti_fallback = (not direct_ok) or (has_just_flag and not direct_ptrs)
        if need_rtti_fallback:
            oi = getattr(g, "_oi", None)
            if oi is not None:
                for fn_name in ("instances", "live_instances"):
                    fn = getattr(oi, fn_name, None)
                    if not callable(fn):
                        continue
                    try:
                        for a in (fn("CTimedWargoalActivity") or ()):
                            _add(a)
                    except Exception:
                        continue
    else:
        for _owner, a in _country_timed_activities(g):
            _add(a)

    if not cand_ptrs:
        return []

    idx2tag = {}
    try:
        import combat as _cb_jf
        idx2tag = _cb_jf._country_id_map(g) or {}
    except Exception:
        idx2tag = {}
    if not idx2tag:
        try:
            for t in (g.tags() or ()):
                try:
                    idx2tag[tag_index(g, t)] = t
                except Exception:
                    pass
        except Exception:
            return []
    if not idx2tag:
        return []

    cur_date_raw = None
    try:
        cur_date_raw = int(g.date_raw())
    except Exception:
        cur_date_raw = None

    oi = getattr(g, "_oi", None)
    _pe = sys.platform == "win32"
    out = []
    stale = set()

    def _raw_date_to_str(raw_val):
        try:
            rv = int(raw_val)
            if not (16_000_000 <= rv <= 18_500_000):
                return None
            from api import date_to_parts
            y, m, d, _h = date_to_parts(rv)
            if 1930 <= y <= 2020:
                return "%04d.%02d.%02d" % (y, m, d)
        except Exception:
            pass
        return None

    for a in cand_ptrs:
        try:
            if oi is not None:
                cls_nm = oi.class_of(a)
                if cls_nm is not None and cls_nm != "CTimedWargoalActivity":
                    stale.add(a)
                    continue
            if _pe:
                src_tag = idx2tag.get(p.i32(a + 0x10))
                tgt_id = p.i32(a + 0x14)
            else:
                src_tag = idx2tag.get(p.i32(a + 0x0C))
                tgt_id = p.i32(a + 0x10)
            tgt_tag = idx2tag.get(tgt_id)
            if not src_tag or not tgt_tag or src_tag == tgt_tag:
                stale.add(a)
                continue
            total = p.i64(a + 0x18) / O.FIXED
            per = p.i64(a + 0x20) / O.FIXED
            done = p.i64(a + 0x28) / O.FIXED
            if not (1.0 <= total <= 5000.0) or done < -1.0 or done > total + 10.0:
                stale.add(a)
                continue
            rem_raw = max(0.0, total - done)
            days_by_progress = (rem_raw / per) if per > 1e-6 else rem_raw
            end_date_raw = None
            try:
                end_date_raw = int(p.i32(a + 0x50))
            except Exception:
                end_date_raw = None
            end_date_str = _raw_date_to_str(end_date_raw) if end_date_raw is not None else None
            days = days_by_progress
            if end_date_str is not None and cur_date_raw is not None and end_date_raw is not None:
                days_by_end = max(0.0, (end_date_raw - cur_date_raw) / 24.0)
                if days_by_end < days:
                    days = days_by_end
            days = max(0.0, round(days, 2))
            if days <= 0.0 and done >= total - 0.01:
                stale.add(a)
                continue
            track_activity_ptr(g, a)
            if want and str(src_tag).upper() != want:
                continue
            eta = end_date_str or _add_game_days(g, days)
            out.append({
                "ptr": hex(a),
                "from": src_tag,
                "target": tgt_tag,
                "source_class": "CTimedWargoalActivity",
                "days_left": days,
                "eta_date": eta,
                "progress_days": round(done, 2),
                "total_days": round(total, 2),
                "progress": round(min(1.0, done / total), 3) if total > 0 else None,
            })
        except Exception:
            stale.add(a)
            continue

    if stale and pid in _TRACKED_ACTIVITIES:
        _TRACKED_ACTIVITIES[pid].difference_update(stale)

    out.sort(key=lambda r: (float("inf") if r.get("days_left") is None else float(r["days_left"]), str(r.get("target") or "")))
    return out


def wargoal_quote(g, target_tag, wargoal="take_state", state_ptrs=(), state_ids=(),
                  source_tag=None):
    """询价：正当化 target_tag 的指定州，现在够不够 PP、差多少、大概多少天。

    返回的 `required_pp` 是引擎自己算出来的整数门槛，不是猜的：进入 `with g:`
    后全部游戏线程被 ptrace 冻结，把 CCountryPolitics+0xE0 的 PP 临时写成探测值，
    直接调 CDiplomaticActionCommand::CanExecute（0xA68750），在 0..2000 之间二分，
    求出 CanExecute 第一次放行的整数 PP；`finally` 里把 8 字节原始值写回，
    游戏线程全程冻结，看不到任何中间值，也不发任何命令。
    """
    p = g.p
    wgs = wargoal_types(g)
    if wargoal not in wgs:
        return {"ok": False, "error": "unknown wargoal: %s (options: %s)"
                % (wargoal, ", ".join(sorted(wgs)))}
    src = source_tag or g.player().tag
    src_c = g.country(src)
    tgt_c = g.country(target_tag)
    pol = src_c.politics + O.PO_POLITICAL_POWER
    cur_pp = src_c.political_power
    own = g._inj is None
    if own:
        g.attach()
    try:
        act = _new_action_param(g, SZ_WARGOAL, CTOR_WARGOAL_PARAM, VT_GENERATE_WARGOAL,
                                [_i32buf(g, tag_index(g, src)),
                                 _i32buf(g, tag_index(g, target_tag)),
                                 wgs[wargoal], _scope_obj(g)])
        _state_array(g, act, list(state_ptrs))
        cmd = _dipcmd(g, act, tag_index(g, src))
        saved = p.i64(pol)
        if not state_ptrs and wargoal in WARGOAL_NEEDS_STATES:
            # A take_state quote over an EMPTY state array can never be allowed.
            # Pick the first state that passes at max PP (Danzig first), exactly the
            # order justify_wargoal uses, so the quote describes the same attempt.
            try:
                _cands = [s["ptr"] for s in g.states(target_tag)]
            except Exception:
                _cands = []
            _cands.sort(key=lambda x: 0 if p.i32(x + O.ST_ID) == 85 else 1)
            _picked = None
            _cap = int(POLITICAL_POWER_CAP * O.FIXED)
            try:
                for _st in _cands[:12]:
                    _state_array(g, act, [_st])
                    p.write(pol, struct.pack("<q", _cap))
                    if _can_execute_cmd(g, cmd):
                        _picked = _st
                        break
            finally:
                p.write(pol, struct.pack("<q", saved))
            if _picked is not None:
                _state_array(g, act, [_picked])
                if not state_ids:
                    state_ids = [p.i32(_picked + O.ST_ID)]
        try:
            can_now = _can_execute_cmd(g, cmd)
            p.write(pol, struct.pack("<q", int(POLITICAL_POWER_CAP * O.FIXED)))
            allowed = _can_execute_cmd(g, cmd)
            required = None
            if allowed:
                lo, hi = 0, int(POLITICAL_POWER_CAP)
                while lo < hi:
                    mid = (lo + hi) // 2
                    p.write(pol, struct.pack("<q", mid * O.FIXED))
                    if _can_execute_cmd(g, cmd):
                        hi = mid
                    else:
                        lo = mid + 1
                required = lo
        finally:
            p.write(pol, struct.pack("<q", saved))
            try:
                g.free(cmd)
            except Exception:
                pass
    finally:
        if own:
            g.detach()

    n_states = max(1, len(state_ptrs))
    # 2026-11-11: queued CGenerateWarGoalAction leftovers are not concurrent
    # justifications; counting them turned a 230-day quote into 13,263 days.
    concurrent = len([x for x in justifying(g)
                      if x.get("from") == src
                      and x.get("source_class") == "CTimedWargoalActivity"])
    est = _estimate_days(n_states, concurrent)
    shortfall = None if required is None else max(0, required - cur_pp)
    if required is None:
        note = "blocked by diplomatic rules (already justifying/war with this country, truce, NAP...)"
    elif shortfall:
        note = "need %d more PP (have %s, engine wants %d)" % (shortfall, cur_pp, required)
    else:
        note = "ready to justify now"
    return {
        "ok": allowed,
        "source": src,
        "target": target_tag,
        "wargoal": wargoal,
        "states": list(state_ids),
        "can_justify_now": can_now,
        "allowed_by_rules": allowed,
        "current_pp": cur_pp,
        "required_pp": required,
        "pp_shortfall": shortfall,
        "daily_pp_drain": WARGOAL_DAILY_PP,
        "concurrent_justifications": concurrent,
        "estimated_days": est,
        "estimated_completion_date": _add_game_days(g, est),
        "estimated_total_pp": round(est * WARGOAL_DAILY_PP, 1),
        "estimate_source": ("00_invasion.txt: 200 + 50/额外州，x1.5/同存正当化（无生产天数项）；"
                            "不含国策/紧张度/意识形态修正，精确值在开跑后用 "
                            "hoi4_justifying 读 CTimedWargoalActivity"),
        "note": note,
    }


# ======================================================================
# 2026-11-23 (复测 6 P0-1): O(1) 实时阵营 / 外交关系读取与宣战连锁推演
#
# 实机 1.19.5.0 PE 只读验证布局：
#   1) 阵营实时表（不再做 12 秒全内存扫，也不受旧 pid 永久缓存过时影响）：
#        CGameState + 0x3F8 -> CFactionSystem*
#        CFactionSystem + 0x20 -> CPdxArray<CFaction*>
#        CFaction + 0x18 -> std::string 名称；+0x58 -> CPdxArray<CCountry*> 成员（首元素为阵营首领）
#   2) 国家外交关系实时表（与 combat._war_relations_win 共用同一数组）：
#        CCountry + 0xF88 -> CDiplomacyStatus*
#        CDiplomacyStatus + 0x20 -> CPdxArray<CRelation*>
#        每条 CRelation:
#          +0x08 i32  关系类型 id:
#              14346 = CWarRelation               (交战)
#              10458 = CGuaranteeRelation         (+0x10 保独者 -> +0x14 被保独者)
#              12497 = CSubjectRelation           (+0x10 宗主国 -> +0x14 附庸国)
#              12220 = CNonAggressionPactRelation (互不侵犯条约)
#              10548 = CMilitaryAccessRelation    (+0x10 通行方 -> +0x14 授权方)
#          +0x10 i32  from CCountryTag id
#          +0x14 i32  to   CCountryTag id
# ======================================================================

WIN_GS_FACTION_SYSTEM = 0x3F8
WIN_FS_FACTIONS       = 0x20
WIN_FACTION_NAME      = 0x18
WIN_FACTION_MEMBERS   = 0x58

WIN_CO_DIPLO_STATUS   = 0xF88
WIN_DS_RELATIONS      = 0x20
REL_TYPE_ID_OFF       = 0x08
REL_FROM_OFF          = 0x10
REL_TO_OFF            = 0x14

REL_ID_WAR            = 14346
REL_ID_GUARANTEE      = 10458
REL_ID_SUBJECT        = 12497
REL_ID_NAP            = 12220
REL_ID_MIL_ACCESS     = 10548


def _fast_faction_ptrs(g):
    """O(1) 读取所有存活 CFaction* 指针；仅在无法读取 CFactionSystem 时退回旧路径。"""
    import sys as _sys
    if _sys.platform == "win32":
        try:
            from game import _arr
            p = g.p
            gs = g.gamestate
            if gs:
                fs = p.u64(gs + WIN_GS_FACTION_SYSTEM)
                if fs and 0x10000 < fs < 0x7FFFFFFFFFFF:
                    arr = _arr(p, fs, WIN_FS_FACTIONS)
                    # 校验元素为合法堆指针，防止版本迁移后误读垃圾值
                    valid = [x for x in arr if 0x10000 < x < 0x7FFFFFFFFFFF]
                    if len(valid) == len(arr) and len(arr) <= 128:
                        return valid
        except Exception:
            pass
    try:
        return list(g._faction_objects() or [])
    except Exception:
        return []


def all_factions(g):
    """实时返回全图所有阵营：[{name, leader, members, ptr}, ...]。纯只读。"""
    # 允许单测或上层自定义直供接口
    custom = getattr(g, "_mock_factions", None)
    if callable(custom):
        return list(custom() or [])
    if isinstance(custom, list):
        return list(custom)

    from game import _arr, item_name
    p = g.p
    out = []
    for f in _fast_faction_ptrs(g):
        try:
            nm = item_name(p, f + WIN_FACTION_NAME) or ""
            if not nm or not nm.isprintable():
                for off in range(0x08, 0x120, 8):
                    s = item_name(p, f + off)
                    if s and s.isprintable() and 2 < len(s) < 64 and not s.startswith(" "):
                        nm = s
                        break
            mems = []
            for mp in _arr(p, f, WIN_FACTION_MEMBERS):
                cc = g.country_by_ptr(mp)
                if cc and getattr(cc, "tag", None):
                    mems.append(str(cc.tag).upper())
            if not mems:
                continue
            leader = None
            try:
                lp = p.u64(f + WIN_FACTION_MEMBERS - 8)
                lc = g.country_by_ptr(lp) if (lp and 0x10000 < lp < 0x7FFFFFFFFFFF) else None
                if lc and getattr(lc, "tag", None):
                    leader = str(lc.tag).upper()
            except Exception:
                leader = None
            if not leader:
                leader = mems[0]
            out.append({
                "name": nm or leader,
                "leader": leader,
                "members": mems,
                "ptr": hex(f),
            })
        except Exception:
            continue
    return out


def _classify_relation(g, addr):
    """同时结合 RTTI 类名与关系 type_id 分类，避免冷缓存下 oi 漏判。"""
    cls = ""
    try:
        cls = g.oi.class_of(addr) or ""
    except Exception:
        cls = ""
    if "Guarantee" in cls:
        return "guarantee"
    if "Subject" in cls:
        return "subject"
    if "NonAggression" in cls:
        return "nap"
    if "MilitaryAccess" in cls:
        return "mil_access"
    if "WarRelation" in cls:
        return "war"
    try:
        tid = int(g.p.i32(addr + REL_TYPE_ID_OFF))
    except Exception:
        tid = 0
    if tid == REL_ID_GUARANTEE:
        return "guarantee"
    if tid == REL_ID_SUBJECT:
        return "subject"
    if tid == REL_ID_NAP:
        return "nap"
    if tid == REL_ID_MIL_ACCESS:
        return "mil_access"
    if tid == REL_ID_WAR:
        return "war"
    return ""


def country_relations(g, tag, _id2tag=None, _factions_by_tag=None):
    """O(1) 纯只读获取某国的外交关系快照：阵营、保独、宗主/附庸、互不侵犯、军事通行、交战。"""
    tag = str(tag).upper()
    custom = getattr(g, "_mock_relations", None)
    if callable(custom):
        res = custom(tag)
        if res is not None:
            return res
    elif isinstance(custom, dict) and tag in custom:
        return dict(custom[tag])

    out = {
        "tag": tag,
        "faction": None,
        "guaranteed_by": [],
        "guaranteeing": [],
        "overlord": None,
        "subjects": [],
        "non_aggression_pacts": [],
        "military_access_from": [],
        "military_access_to": [],
        "at_war_with": [],
    }
    if _factions_by_tag is None:
        _factions_by_tag = {}
        for fac in all_factions(g):
            for m in fac.get("members") or []:
                _factions_by_tag[m] = fac
    fac = _factions_by_tag.get(tag)
    if fac:
        out["faction"] = {
            "name": fac.get("name"),
            "leader": fac.get("leader"),
            "members": list(fac.get("members") or []),
        }

    import sys as _sys
    if _sys.platform != "win32":
        try:
            import combat
            out["at_war_with"] = sorted(combat.enemy_tags(g, tag))
        except Exception:
            pass
        return out

    from game import _arr
    import combat
    p = g.p
    try:
        c = g.country(tag)
        my_id = int(p.i32(c.ptr + 8))
    except Exception:
        return out
    if _id2tag is None:
        _id2tag = combat._country_id_map(g)

    try:
        dip = p.u64(c.ptr + WIN_CO_DIPLO_STATUS)
        arr = _arr(p, dip, WIN_DS_RELATIONS) if dip else []
    except Exception:
        arr = []

    g_by, g_to, subs, naps, ma_from, ma_to, wars = (
        set(), set(), set(), set(), set(), set(), set()
    )
    overlord = None
    for a in arr:
        if not a:
            continue
        kind = _classify_relation(g, a)
        if not kind:
            continue
        try:
            from_id = int(p.i32(a + REL_FROM_OFF))
            to_id = int(p.i32(a + REL_TO_OFF))
        except Exception:
            continue
        if not from_id or not to_id or from_id == to_id:
            continue
        from_tag = _id2tag.get(from_id)
        to_tag = _id2tag.get(to_id)
        if not from_tag or not to_tag:
            continue
        if kind == "guarantee":
            if to_id == my_id and from_tag != tag:
                g_by.add(from_tag)
            elif from_id == my_id and to_tag != tag:
                g_to.add(to_tag)
        elif kind == "subject":
            if to_id == my_id and from_tag != tag:
                overlord = from_tag
            elif from_id == my_id and to_tag != tag:
                subs.add(to_tag)
        elif kind == "nap":
            other = to_tag if from_id == my_id else (from_tag if to_id == my_id else None)
            if other and other != tag:
                naps.add(other)
        elif kind == "mil_access":
            if to_id == my_id and from_tag != tag:
                ma_from.add(from_tag)
            elif from_id == my_id and to_tag != tag:
                ma_to.add(to_tag)
        elif kind == "war":
            other = to_tag if from_id == my_id else (from_tag if to_id == my_id else None)
            if other and other != tag:
                wars.add(other)

    out["guaranteed_by"] = sorted(g_by)
    out["guaranteeing"] = sorted(g_to)
    out["overlord"] = overlord
    out["subjects"] = sorted(subs)
    out["non_aggression_pacts"] = sorted(naps)
    out["military_access_from"] = sorted(ma_from)
    out["military_access_to"] = sorted(ma_to)
    out["at_war_with"] = sorted(wars)
    return out


def _quick_division_count(g, tag):
    """O(1) 读取某国当前陆军师数量（绝不走深扫，避免拖慢宣战预检）。"""
    tag = str(tag).upper()
    custom = getattr(g, "_mock_div_counts", None)
    if isinstance(custom, dict) and tag in custom:
        return int(custom[tag])
    try:
        from game import _count, CO_ARMIES
        c = g.country(tag)
        return max(0, int(_count(g.p, c.ptr, CO_ARMIES)))
    except Exception:
        try:
            return int((g.divisions(tag, limit=1, deep=False) or {}).get("count") or 0)
        except Exception:
            return 0


def _quick_majors(g):
    """O(1) 读取主要国家 tag 集合。"""
    custom = getattr(g, "_mock_majors", None)
    if custom is not None:
        return {str(x).upper() for x in custom}
    out = set()
    try:
        from game import _arr
        for ptr in _arr(g.p, g.gamestate, O.GS_MAJORS):
            cc = g.country_by_ptr(ptr)
            if cc and getattr(cc, "tag", None):
                out.add(str(cc.tag).upper())
    except Exception:
        pass
    return out


def _border_exposure_for_tags(g, me_tag, tags):
    """一次性计算我方对多个国家的接壤省数、当前边境驻军数与是否存在原生前线。"""
    me_tag = str(me_tag).upper()
    tags = [str(t).upper() for t in tags if t]
    if not tags:
        return {}
    custom = getattr(g, "_mock_border_exposure", None)
    if callable(custom):
        return custom(me_tag, tags)

    result = {}
    # 1. 一次性读全省控制者表 + 陆地邻接表（2026-11-23 复测 7 B-5：与 game._border_divisions
    #    及 movemicro.deploy_to_border 统一使用 province_controller_map，战时亦精确一致）
    try:
        import mapdata
        owners = mapdata.province_controller_map(g) or {}
        if not owners:
            owners, _ = mapdata.province_owner_map(g)
        adj = mapdata.build_adjacency()
        _, ptype = mapdata.load_definitions()
    except Exception:
        owners, adj, ptype = {}, {}, {}

    if not owners or not adj:
        return result

    tset = set(tags)
    border_by_tag = {t: set() for t in tags}
    for prov, own in owners.items():
        if own != me_tag:
            continue
        if ptype and ptype.get(prov) != "land":
            continue
        for nb in adj.get(prov) or ():
            if ptype and ptype.get(nb) != "land":
                continue
            nb_own = owners.get(nb)
            if nb_own in tset:
                border_by_tag[nb_own].add(prov)

    touching = [t for t in tags if border_by_tag[t]]
    if not touching:
        return result

    # 2. 一次性读我方全部师所在省份
    # 2026-11-23 (复测 7 B-5 根因修复): divenum 的导出函数名为 armies(g, tag)，
    # 旧代码误写为 enumerate_divisions 触发 AttributeError 被 except 静默吞掉，导致驻军恒报 0。
    my_provs = []
    try:
        import divenum, unitorders
        rows = divenum.armies(g, me_tag)
        for a in rows.get("armies") or []:
            try:
                pv = unitorders.province_of(g, a)
                if pv is not None:
                    my_provs.append(pv)
            except Exception:
                continue
    except Exception:
        my_provs = []

    # 3. 一次性检查我方是否已对各接壤国画出原生前线（type-2 且 assigned > 0）
    front_ok_tags = set()
    import sys as _sys
    if _sys.platform == "win32":
        try:
            import armyops_win as _aw
            import armyops as _ao
            co = g.country(me_tag)
            ogs = [og for og in _ao.orders_groups(g, co)
                   if not g.p.u8(og + _aw.OG_IS_GROUP)]
            # 收集每个已有兵力的前线所指向的 front_handle
            armed_handles = set()
            for og in ogs:
                try:
                    for o in _aw.orders(g, og):
                        if o.get("type") == 2 and int(o.get("assigned_divisions") or 0) > 0:
                            rf = tuple(int(x) for x in (o.get("root_front") or ()))
                            if rf:
                                armed_handles.add(rf)
                except Exception:
                    continue
            if armed_handles:
                for t in touching:
                    try:
                        segs = _aw.fronts_towards(g, t, me_tag)
                        facing = {tuple(int(x) for x in s["front_handle"]) for s in segs}
                        if facing & armed_handles:
                            front_ok_tags.add(t)
                    except Exception:
                        continue
        except Exception:
            pass

    for t in touching:
        bset = border_by_tag[t]
        on_border = sum(1 for pv in my_provs if pv in bset)
        result[t] = {
            "tag": t,
            "border_provinces": len(bset),
            "border_province_ids": sorted(bset),
            "divisions_on_border": on_border,
            "has_front_line": t in front_ok_tags,
        }
    return result


def divisions_on_border(g, target_tag, me_tag=None):
    """统一权威边境驻军查询（与 war_readiness / co_belligerent_borders / deploy_to_border 同口径）。

    定义：当前物理驻扎在「由 me_tag 控制且陆路接壤 target_tag 控制省」上的我方陆军师数量
    （使用 province_controller_map + divenum.armies + unitorders.province_of）。
    """
    me = str(me_tag or g.player_tag()).upper()
    tgt = str(target_tag).upper()
    bmap = _border_exposure_for_tags(g, me, [tgt])
    row = bmap.get(tgt)
    if not row:
        return {
            "own_tag": me,
            "enemy_tag": tgt,
            "border_provinces": 0,
            "border_sample": [],
            "divisions_on_border": 0,
            "has_front_line": False,
        }
    b_ids = row.get("border_province_ids") or []
    return {
        "own_tag": me,
        "enemy_tag": tgt,
        "border_provinces": int(row.get("border_provinces") or len(b_ids)),
        "border_sample": b_ids[:16],
        "divisions_on_border": int(row.get("divisions_on_border") or 0),
        "has_front_line": bool(row.get("has_front_line")),
    }


def war_escalation_forecast(g, target_tag, me_tag=None):
    """宣战前外交连锁推演：阵营、保独、宗主/附庸、互不侵犯与接壤强敌空防体检。

    纯只读，O(1) 直读 CFactionSystem + CDiplomacyStatus，绝不触发全内存慢扫。
    """
    if not me_tag:
        me_tag = g.player_tag()
    me = str(me_tag).upper()
    tgt = str(target_tag).upper()

    # 构建一次性上下文缓存，避免重复读全图
    factions_list = all_factions(g)
    factions_by_tag = {}
    for fac in factions_list:
        for m in fac.get("members") or []:
            factions_by_tag[str(m).upper()] = fac

    try:
        import combat
        id2tag = combat._country_id_map(g) if getattr(g, "p", None) is not None else {}
    except Exception:
        id2tag = {}

    rel_cache = {}

    def _rel(t):
        tu = str(t).upper()
        if tu not in rel_cache:
            rel_cache[tu] = country_relations(g, tu, _id2tag=id2tag, _factions_by_tag=factions_by_tag)
        return rel_cache[tu]

    me_rel = _rel(me)
    tgt_rel = _rel(tgt)
    majors = _quick_majors(g)

    me_fac = me_rel.get("faction")
    me_fac_members = set(me_fac.get("members") or []) if me_fac else set()
    me_subjects = set(me_rel.get("subjects") or [])
    me_overlord = me_rel.get("overlord")
    me_enemies = set(me_rel.get("at_war_with") or [])
    me_naps = set(me_rel.get("non_aggression_pacts") or [])

    # 1. 外交合法性硬拦校验
    diplomatic_blockers = []
    diplomatic_warnings = []
    if tgt == me:
        diplomatic_blockers.append("cannot declare war on yourself (%s)" % me)
    else:
        if tgt in me_enemies:
            diplomatic_blockers.append("already at war with %s" % tgt)
        if me_fac and tgt in me_fac_members:
            diplomatic_blockers.append(
                "%s is in your own faction (%s) -- cannot declare war on a faction ally"
                % (tgt, me_fac.get("name") or me_fac.get("leader"))
            )
        if tgt in me_subjects:
            diplomatic_blockers.append("%s is your subject -- cannot declare war on your own puppet" % tgt)
        if me_overlord and tgt == me_overlord:
            diplomatic_blockers.append("%s is your overlord -- cannot declare war directly" % tgt)
        if tgt in me_naps:
            diplomatic_warnings.append(
                "active non-aggression pact with %s -- engine usually refuses declaration until cancelled" % tgt
            )

    # 2. 多级连锁参战推演
    # 我方自己/同阵营/己方附庸不会站在目标国一边参战
    friendly_side = {me, tgt} | me_fac_members | me_subjects
    if me_overlord:
        friendly_side.add(me_overlord)

    cb_map = {}  # tag -> {"tag", "reasons": [], "certainty": "certain"|"likely"}
    conflicting_guarantors = []

    def _add_cb(ctag, reason, certainty="likely"):
        cu = str(ctag).upper()
        if not cu or cu in friendly_side:
            return
        row = cb_map.get(cu)
        if row is None:
            cb_map[cu] = {"tag": cu, "reasons": [reason], "certainty": certainty}
        else:
            if reason not in row["reasons"]:
                row["reasons"].append(reason)
            if certainty == "certain":
                row["certainty"] = "certain"

    # (A) 目标国自身阵营成员
    tgt_fac = tgt_rel.get("faction")
    if tgt_fac:
        fname = tgt_fac.get("name") or tgt_fac.get("leader") or "faction"
        for m in tgt_fac.get("members") or []:
            if m != tgt:
                _add_cb(m, "same_faction(%s)" % fname, certainty="certain")

    # (B) 目标国宗主与附庸
    tgt_overlord = tgt_rel.get("overlord")
    if tgt_overlord:
        _add_cb(tgt_overlord, "overlord_of(%s)" % tgt, certainty="certain")
    for sub in tgt_rel.get("subjects") or []:
        _add_cb(sub, "subject_of(%s)" % tgt, certainty="certain")

    # (C) 目标国保独国（Guarantors）
    raw_guarantors = list(tgt_rel.get("guaranteed_by") or [])
    active_guarantors = []
    for gr in raw_guarantors:
        if gr == me:
            diplomatic_warnings.append("you are currently guaranteeing %s yourself" % tgt)
            continue
        if gr in me_fac_members or gr in me_subjects or gr == me_overlord:
            conflicting_guarantors.append(gr)
            continue
        active_guarantors.append(gr)
        _add_cb(gr, "guarantees(%s)" % tgt, certainty="likely")

    # (D) 二级连锁展开：保独国 / 宗主国 / 同阵营成员背后的阵营与附庸
    # （经典案例：ENG/FRA 保独 BEL -> 宣 BEL 触发 ENG/FRA 参战 -> 同盟国全员与英联邦附庸全部参战！）
    seed_expand = []
    if tgt_overlord and tgt_overlord not in friendly_side:
        seed_expand.append((tgt_overlord, "overlord"))
    for gr in active_guarantors:
        seed_expand.append((gr, "guarantor"))

    for seed, role in seed_expand:
        s_rel = _rel(seed)
        s_fac = s_rel.get("faction")
        if s_fac:
            sf_name = s_fac.get("name") or s_fac.get("leader") or "faction"
            for m in s_fac.get("members") or []:
                if m == seed or m == tgt:
                    continue
                _add_cb(m, "faction_ally_of_%s(%s:%s)" % (role, seed, sf_name), certainty="likely")
                # 阵营盟友的附庸（例如 ENG 盟友 FRA 的 SYR/LEB 或 HOL 的 INS）
                for msub in _rel(m).get("subjects") or []:
                    _add_cb(msub, "subject_of_ally(%s)" % m, certainty="likely")
        for ssub in s_rel.get("subjects") or []:
            _add_cb(ssub, "subject_of_%s(%s)" % (role, seed), certainty="likely")

    # 若目标国本身已在阵营中，其盟友的附庸也可能参战
    if tgt_fac:
        for m in tgt_fac.get("members") or []:
            if m == tgt or m in friendly_side:
                continue
            for msub in _rel(m).get("subjects") or []:
                _add_cb(msub, "subject_of_faction_ally(%s)" % m, certainty="likely")

    # 3. 统计兵力与已交战状态
    tgt_divs = _quick_division_count(g, tgt)
    co_details = []
    cb_total_divs = 0
    new_cb_tags = []
    for cu, info in sorted(cb_map.items()):
        divs = _quick_division_count(g, cu)
        already = cu in me_enemies
        info["divisions"] = divs
        info["is_major"] = cu in majors
        info["already_at_war"] = already
        cb_total_divs += divs
        if not already:
            new_cb_tags.append(cu)
        co_details.append(info)

    # 排序：未交战主要国家/高师数在前，便于模型一眼抓重点
    co_details.sort(key=lambda r: (bool(r["already_at_war"]), not r["is_major"], -r["divisions"], r["tag"]))

    # 4. 一次性检测所有接壤潜在参战国的边境布防
    exposure = _border_exposure_for_tags(g, me, [r["tag"] for r in co_details])
    border_rows = []
    undefended_new_major = []
    thin_new_borders = []
    for row in co_details:
        t = row["tag"]
        ex = exposure.get(t)
        if not ex:
            continue
        bprovs = int(ex.get("border_provinces") or 0)
        on_b = int(ex.get("divisions_on_border") or 0)
        has_fl = bool(ex.get("has_front_line"))
        edivs = int(row.get("divisions") or 0)
        if on_b == 0:
            status = "undefended"
        elif (not has_fl and edivs >= 10) or on_b < max(1, bprovs // 2):
            status = "thin"
        else:
            status = "defended"
        entry = {
            "tag": t,
            "border_provinces": bprovs,
            "divisions_on_border": on_b,
            "enemy_divisions": edivs,
            "is_major": bool(row.get("is_major")),
            "has_front_line": has_fl,
            "already_at_war": bool(row.get("already_at_war")),
            "reasons": list(row.get("reasons") or []),
            "status": status,
        }
        border_rows.append(entry)
        if not entry["already_at_war"]:
            # 新拉入且有实战威胁（主要国或 >=5 师）且接壤 >=3 省、我方完全 0 驻军 -> 高危空防
            if on_b == 0 and bprovs >= 3 and (edivs >= 5 or entry["is_major"]):
                undefended_new_major.append(entry)
            elif status in ("undefended", "thin") and (edivs >= 5 or entry["is_major"] or bprovs >= 3):
                thin_new_borders.append(entry)

    # 生成可读预警列表
    if conflicting_guarantors:
        diplomatic_warnings.append(
            "your ally/subject %s also guarantees %s" % (", ".join(sorted(conflicting_guarantors)), tgt)
        )
    if new_cb_tags:
        top_names = [
            "%s(%d divs)" % (r["tag"], r["divisions"]) if r["divisions"] else r["tag"]
            for r in co_details if not r["already_at_war"]
        ][:6]
        more = " (+%d more)" % (len(new_cb_tags) - len(top_names)) if len(new_cb_tags) > len(top_names) else ""
        diplomatic_warnings.append(
            "diplomatic escalation: declaring on %s (%d divs) may pull in %d co-belligerents: %s%s "
            "(est. %d total enemy divisions)"
            % (tgt, tgt_divs, len(new_cb_tags), ", ".join(top_names), more, tgt_divs + cb_total_divs)
        )
    elif co_details:
        top_names = [r["tag"] for r in co_details[:6]]
        more = " (+%d more)" % (len(co_details) - len(top_names)) if len(co_details) > len(top_names) else ""
        diplomatic_warnings.append(
            "diplomatic links: %s is backed by %d country/countries (%s%s), all already at war with %s"
            % (tgt, len(co_details), ", ".join(top_names), more, me)
        )
    for b in thin_new_borders:
        fl_txt = "has front line" if b["has_front_line"] else "NO front line"
        diplomatic_warnings.append(
            "co-belligerent border risk: %s (%s, %d enemy divs) shares %d border provinces with %s, "
            "only %d divisions on border (%s)"
            % (b["tag"], ", ".join(b["reasons"][:2]), b["enemy_divisions"],
               b["border_provinces"], me, b["divisions_on_border"], fl_txt)
        )

    return {
        "me": me,
        "target": tgt,
        "target_faction": tgt_fac,
        "guaranteed_by": active_guarantors,
        "all_guarantors": raw_guarantors,
        "overlord": tgt_overlord,
        "subjects": list(tgt_rel.get("subjects") or []),
        "non_aggression_pact": tgt in me_naps,
        "co_belligerents": [r["tag"] for r in co_details],
        "new_co_belligerents": [r["tag"] for r in co_details if not r["already_at_war"]],
        "co_belligerent_details": co_details,
        "co_belligerent_borders": border_rows,
        "undefended_new_co_belligerents": undefended_new_major,
        "target_divisions": tgt_divs,
        "co_belligerent_divisions": cb_total_divs,
        "total_enemy_divisions_est": tgt_divs + cb_total_divs,
        "diplomatic_blockers": diplomatic_blockers,
        "diplomatic_warnings": diplomatic_warnings,
    }


