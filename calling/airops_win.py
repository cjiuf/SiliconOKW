"""Windows (PE) 1.19.5.0 air-wing read/write layer.

Why this module exists
----------------------
`airops.py` is entirely Linux 1.19.4.1: its WING_VT 0x440E200, the attach ctor
0x241F840, the mission ctors and the libstdc++ error sinks are ELF addresses.
On hoi4.exe they match nothing, which is why `hoi4_air_wings` reported 0 wings
and no air order ever worked.

Measured on the live PE build 2026-11-16 (research/windows/pedis.py, all RVAs):

  CAirWing RTTI vtables        0x29F7748 (primary), 0x29F7780 (secondary)
      Instances() returns 560 objects because each wing carries BOTH vptrs
      (primary at +0x00, secondary at +0x10). Keep only the primary ones
      -> 280 real wings.
      +0x18  packed {type,id} handle (type 0x45 = 69)
      +0x38  CAirWingPool*
  CAirWingPool RTTI            0x29F77D8
      +0x20  equipment/type definition object; its +0x18 is an MSVC
             std::string holding the type key ("fighter", "cas", ...)
      +0x40  CStrategicAir*  -- groups wings per country (44 groups here)
  CPdxArray<T>                 0x18 bytes: data +0x00, cap +0x08, size +0x0C

Commands (all built with cmdwin.build_by_vt, NO ctor call, NO foreign code):

  CAttachAirWingToArmyCommand   vt 0x2A97728  size 0x48  ctor 0x19CB480
      +0x28 CPdxArray<packed handle> of the wings to attach (data/cap/size
            at +0x28/+0x30/+0x34), +0x40 packed COrdersGroup handle.
      CanExecute 0x19D3920 / Execute 0x19D00E0 both walk that array and
      resolve each handle; Execute calls 0xC40C30(army, wing) on each.
  CStratAirSetMissionCommand    vt 0x2A96BE8  size 0x48  ctor 0x19CCA80
      +0x28 CPdxArray<packed handle> (the wings), +0x40 i32 mission mask,
      +0x44 byte flag1, +0x45 byte flag2, +0x46 byte 0.
  CStratAirEnableMissionCommand vt 0x2A96B28  size 0x60  ctor 0x19CC8E0
      +0x28 CPdxArray<handle>, +0x40 second CPdxArray (target CStrategicAir),
      +0x58 i32 value (mission mask), +0x5c byte 0.

All three use the same "array of packed handles at +0x28" shape, so one helper
builds them all. A paused game never drains the queue, so every post goes
through cmdwin.post_drained().
"""
from __future__ import annotations

import struct
import offsets_win as _OW

VT_AIRWING_MAIN = _OW.AIR_VT_WING_MAIN
VT_AIRWING_SECONDARY = _OW.AIR_VT_WING_SECONDARY
VT_AIRWINGPOOL = _OW.AIR_VT_WINGPOOL
#: CStrategicAir + 0x90 = u32 CCountryTag id of the air force's owner
#: (measured 2026-11-19: 44 groups, 44 distinct tag ids, ENG 36 wings / GER 15)
SA_OWNER = _OW.AIR_SA_OWNER

# --- CAirWing live state (measured 2026-11-16 on the 15 German wings) ------
# +0x7C  i32  planes currently in the wing      (100 fighter / 76..80 bomber)
# +0xA0  i32  mission mask  (1 air superiority, 2 cas, 4 interception, ...)
# +0xA4  i32  1 while that mission is ENABLED
# +0x80  i32  derived total (planes x equipment cost) -- not used in output
# CStratAirSetMissionCommand with flag1=flag2=True writes BOTH +0xA0 and
# +0xA4, i.e. the engine's own set-mission command already enables the
# mission.  CStratAirEnableMissionCommand is therefore NOT needed for a
# normal player order (verified: wing 2 +0xA0 0->1 and +0xA4 0->1).
WING_ALLOWED_MISSIONS = _OW.AIR_WING_ALLOWED_MISSIONS
WING_PLANES = _OW.AIR_WING_PLANES
WING_MISSION = _OW.AIR_WING_MISSION
WING_MISSION_ON = _OW.AIR_WING_MISSION_ON
WING_MIN_0x60 = _OW.AIR_WING_MIN_0x60
WING_CUR_0x64 = _OW.AIR_WING_CUR_0x64
WING_BUSY_0x79 = _OW.AIR_WING_BUSY_0x79
WING_RANGE = _OW.AIR_WING_RANGE
WING_TRANSFER_TARGET = _OW.AIR_WING_TRANSFER_TARGET
WING_TRANSFER_REGION = _OW.AIR_WING_TRANSFER_REGION
WING_TRANSFER_MISSION = _OW.AIR_WING_TRANSFER_MISSION

GS_STRAT_AIR_MGR = _OW.AIR_GS_MGR
SAM_STRAT_AIRS = _OW.AIR_SAM_STRAT_AIRS
SAM_LAND_AIRBASE_PROVS = _OW.AIR_SAM_LAND_AIRBASE_PROVS
SA_AIRBASE_MGR = _OW.AIR_SA_AIRBASE_MGR
ABM_AIRBASES = _OW.AIR_ABM_AIRBASES
AB_COUNTRIES = _OW.AIR_AB_COUNTRIES
ABC_POOLS = _OW.AIR_ABC_POOLS
AWP_DEF = _OW.AIR_AWP_DEF
AWP_WINGS = _OW.AIR_AWP_WINGS
AWDEF_MISSION_FLAGS = _OW.AIR_AWDEF_MISSION_FLAGS
AWDEF_PARADROP_BIT = _OW.AIR_AWDEF_PARADROP_BIT

MISSION_NAMES = {1: "air_superiority", 2: "cas", 4: "interception",
                 8: "strategic_bombing", 16: "naval_strike",
                 32: "naval_patrol", 64: "port_strike", 128: "logistics_strike"}


def _heap_ptr(addr):
    return 0x10000 < int(addr or 0) < 0x7F0000000000


def _read_wing_range_km(p, w):
    try:
        raw = int(p.i64(w + WING_RANGE))
        if 0 < raw < 10_000_000_000:
            return round(raw / 100000.0, 1)
    except Exception:
        pass
    return None


def mission_name(mask):
    """Human name for a mission mask (bit 0 wins; 0 means none)."""
    if not mask:
        return None
    return MISSION_NAMES.get(mask & -mask, "mask_%d" % mask)


def decode_mission_mask(mask):
    """将空军任务位掩码解析为已知任务名列表（未知高位保留 0x... 标注）。"""
    m = int(mask or 0)
    if m <= 0:
        return []
    out = []
    known_bits = 0
    for bit in sorted(MISSION_NAMES):
        if m & bit:
            out.append(MISSION_NAMES[bit])
            known_bits |= bit
    extra = m & ~known_bits
    if extra:
        # 0x800 经反汇编 0x19D2372 / 0x10098C5 实证为空军训练 (training)
        if extra & 0x800:
            out.append("training")
            extra &= ~0x800
        if extra:
            out.append("extra_mask_%s" % hex(extra))
    return out


def wing_allowed_missions(p, wing_ptr):
    """纯只读获取单支 CAirWing 允许执行的任务位掩码（`CAirWing + 0x30`，0x19CD6BB 实证）。"""
    try:
        raw = int(p.u32(int(wing_ptr) + WING_ALLOWED_MISSIONS))
        return raw, decode_mission_mask(raw)
    except Exception:
        return None, []


def wing_state(g, w):
    """Live planes / mission / enabled / range_km / transfer state for one CAirWing pointer."""
    p = g.p
    try:
        m = p.u32(w + WING_MISSION)
        out = {"planes": p.u32(w + WING_PLANES),
               "mission": m,
               "mission_name": mission_name(m),
               "mission_on": p.u32(w + WING_MISSION_ON) != 0}
        rng = _read_wing_range_km(p, w)
        if rng is not None:
            out["range_km"] = rng
        try:
            tr_h = int(p.u64(w + WING_TRANSFER_TARGET))
            if tr_h:
                out["transferring"] = True
                out["transfer_target_handle"] = hex(tr_h)
        except Exception:
            pass
        return out
    except Exception:
        return {"planes": None, "mission": None, "mission_name": None,
                "mission_on": None}

VT_ATTACH = _OW.AIR_VT_ATTACH
SIZE_ATTACH = _OW.AIR_SIZE_ATTACH
CANEXEC_ATTACH = _OW.AIR_CANEXEC_ATTACH
VT_SET_MISSION = _OW.AIR_VT_SET_MISSION
SIZE_SET_MISSION = _OW.AIR_SIZE_SET_MISSION
CANEXEC_SET_MISSION = _OW.AIR_CANEXEC_SET_MISSION
VT_ENABLE_MISSION = _OW.AIR_VT_ENABLE_MISSION
SIZE_ENABLE_MISSION = _OW.AIR_SIZE_ENABLE_MISSION
CANEXEC_ENABLE_MISSION = _OW.AIR_CANEXEC_ENABLE_MISSION
VT_TRANSFER = _OW.AIR_VT_TRANSFER
SIZE_TRANSFER = _OW.AIR_SIZE_TRANSFER
CANEXEC_TRANSFER = _OW.AIR_CANEXEC_TRANSFER
EXEC_TRANSFER = _OW.AIR_EXEC_TRANSFER

# 第二期数据驱动覆盖：优先从 calling/profile.py 读取空军虚表与命令地址
try:
    import profile as _prof
    if _prof.load():
        _aw_vts = _prof.anchor_list("VT_AIRWING")
        if _aw_vts:
            VT_AIRWING_MAIN = _aw_vts[0]
            if len(_aw_vts) > 1:
                VT_AIRWING_SECONDARY = _aw_vts[1]
        VT_AIRWINGPOOL = _prof.anchor("VT_AIRWINGPOOL", VT_AIRWINGPOOL)
        VT_ATTACH = _prof.command_field("CAttachAirWingToArmyCommand", "vt", VT_ATTACH)
        SIZE_ATTACH = _prof.command_field("CAttachAirWingToArmyCommand", "size", SIZE_ATTACH)
        CANEXEC_ATTACH = _prof.command_field("CAttachAirWingToArmyCommand", "can_execute", CANEXEC_ATTACH)
        VT_SET_MISSION = _prof.command_field("CStratAirSetMissionCommand", "vt", VT_SET_MISSION)
        SIZE_SET_MISSION = _prof.command_field("CStratAirSetMissionCommand", "size", SIZE_SET_MISSION)
        CANEXEC_SET_MISSION = _prof.command_field("CStratAirSetMissionCommand", "can_execute", CANEXEC_SET_MISSION)
        VT_ENABLE_MISSION = _prof.command_field("CStratAirEnableMissionCommand", "vt", VT_ENABLE_MISSION)
        SIZE_ENABLE_MISSION = _prof.command_field("CStratAirEnableMissionCommand", "size", SIZE_ENABLE_MISSION)
        CANEXEC_ENABLE_MISSION = _prof.command_field("CStratAirEnableMissionCommand", "can_execute", CANEXEC_ENABLE_MISSION)
        VT_TRANSFER = _prof.command_field("CStratAirTransferCommand", "vt", VT_TRANSFER)
        SIZE_TRANSFER = _prof.command_field("CStratAirTransferCommand", "size", SIZE_TRANSFER)
        CANEXEC_TRANSFER = _prof.command_field("CStratAirTransferCommand", "can_execute", CANEXEC_TRANSFER)
        EXEC_TRANSFER = _prof.command_field("CStratAirTransferCommand", "execute", EXEC_TRANSFER)
except Exception:
    pass


def _msvc_str(p, addr):
    """MSVC std::string at `addr`: 16-byte SSO, size +0x10, cap +0x18."""
    try:
        n = p.u64(addr + 0x10)
        if not (0 < n < 64):
            return ""
        b = p.read(addr, n) if n <= 15 else p.read(p.u64(addr), n)
        return b.decode("utf-8", "replace")
    except Exception:
        return ""


def wing_type(p, pool):
    """The plane type key from a CAirWingPool (or '')."""
    d = p.u64(pool + 0x20)
    if not d:
        return ""
    # measured: the definition's key is an MSVC std::string at +0x18
    return _msvc_str(p, d + 0x18)


import session_state as _SS

_AUTO_REFRESH_DONE = {}
_AUTO_REFRESH_EMPTY_AT = {}
_SS.register_cache("airops_win._AUTO_REFRESH_DONE", container=_AUTO_REFRESH_DONE)
_SS.register_cache("airops_win._AUTO_REFRESH_EMPTY_AT", container=_AUTO_REFRESH_EMPTY_AT)


def all_wings(g, refresh=False):
    """Every real CAirWing on the PE build, with pool/type/strategic-air group.

    Pure read (RTTI object index). De-duplicates the secondary vptr twin.
    2026-11-23 (复测 8 B-15 & C-6): 刚冷启动或读新档时 ObjectIndex 可能只覆盖旧 heap arena
    （只扫出 0 或 16 翼而非全图 ~200+ 翼导致玩家空军报 0 翼）。
    当未显式刷新但总翼数 < 30 时：
      1. 优先用单类虚表直扫 `g.win_scan_class("CAirWing", use_cache=False)`（快于重建全量 OI）；
      2. 若仍不足 30 翼再兜底 `g.oi.build(force=True)`；
      3. 仅当确实拿到 >= 30 翼才标记本周完成；若仍在加载中扫出不足 30 翼仅做 5s 短防抖，绝不锁死整周！
    """
    import time as _time
    import cmdwin  # noqa: F401 (kept for symmetry with other _win modules)
    p = g.p
    if refresh:
        g.oi.build(force=True)

    main = p.base + VT_AIRWING_MAIN

    def _pack_raw(raw_ptrs):
        res = []
        for w in sorted(set(raw_ptrs)):
            try:
                if p.u64(w) != main:
                    continue
                handle = p.u64(w + 0x18)
                pool = p.u64(w + 0x38)
                if not handle or not pool:
                    continue
                sa = p.u64(pool + 0x40)
                res.append({"ptr": w, "handle": handle, "pool": pool,
                            "sa": sa, "type": wing_type(p, pool)})
            except Exception:
                continue
        return res

    def _collect():
        try:
            raw = g.oi.instances("CAirWing")
        except Exception:
            raw = []
        return _pack_raw(raw)

    out = _collect()
    pid = getattr(p, "pid", None)
    try:
        date_raw = g.date_raw()
    except Exception:
        date_raw = None
    auto_key = (pid, int(date_raw) // (24 * 7) if date_raw is not None else 0)
    now_m = _time.monotonic()
    last_empty = _AUTO_REFRESH_EMPTY_AT.get(pid, 0.0)
    if (not refresh) and len(out) < 30 and not _AUTO_REFRESH_DONE.get(auto_key) and (now_m - last_empty) >= 5.0:
        # 优先单类虚表直扫，避免冷启动重建全量 ObjectIndex 耗时过长
        if hasattr(g, "win_scan_class"):
            try:
                scanned = g.win_scan_class("CAirWing", use_cache=False) or []
                out_vt = _pack_raw(scanned)
                if len(out_vt) > len(out):
                    out = out_vt
            except Exception:
                pass
        if len(out) < 30:
            try:
                g.oi.build(force=True)
                out2 = _collect()
                if len(out2) > len(out):
                    out = out2
            except Exception:
                pass
        if len(out) >= 30:
            _AUTO_REFRESH_DONE[auto_key] = True
            _AUTO_REFRESH_EMPTY_AT.pop(pid, None)
        else:
            _AUTO_REFRESH_EMPTY_AT[pid] = _time.monotonic()
    return out


def wing_groups(wings):
    """{CStrategicAir*: [wing, ...]} -- one group per country's air force."""
    gs = {}
    for w in wings:
        gs.setdefault(w["sa"], []).append(w)
    return gs


def air_force_report(g, refresh=False):
    """Read-only full picture, grouped by strategic-air owner."""
    wings = all_wings(g, refresh=refresh)
    gs = wing_groups(wings)
    import collections
    groups = []
    for sa, lst in sorted(gs.items(), key=lambda kv: -len(kv[1])):
        hist = collections.Counter(w["type"] for w in lst)
        ids = [w["handle"] >> 32 for w in lst]
        groups.append({"sa": hex(sa), "n": len(lst), "types": dict(hist),
                       "id_min": min(ids), "id_max": max(ids),
                       "first_wing": hex(lst[0]["ptr"]),
                       "first_handle": hex(lst[0]["handle"])})
    return {"count": len(wings), "groups": len(gs), "air_forces": groups}


def german_wings(g, refresh=False, tag=None):
    """The player's air wings, proven by CStrategicAir ownership.

    2026-11-07 -> 2026-11-18 this function could only guess: "CAirWing carries no
    country pointer, so take the deterministic id-1..k starting block".  That
    was true for CAirWing / CAirWingPool / CStrategicAir *directly*, but the
    owner is one hop further out:

        CAirWing + 0x38  -> CAirWingPool
        CAirWingPool+0x40 -> CStrategicAir      (one object per country's air force)
        CStrategicAir+0x90 -> u32 CCountryTag id   <-- THE OWNER

    Measured 2026-11-19 on pid 61964: all 44 CStrategicAir objects carry a tag
    id at +0x90 and they are pairwise distinct, 440 countries, e.g.
    ENG(tag 2) 36 wings / JAP(48) 22 / USA(68) 20 / GER(1) 15 / SOV(3) 15 /
    FRA(5) 14 / ITA(16) 9 / CZE(9) 6 / POL(10) 4 -- exactly the 1936 order of
    battle.  Wings created later by the player inherit the same CStrategicAir
    and are therefore included, which the old id-1 block heuristic missed.

    Falls back to `initial_run`-style behaviour only if no tag id can be read.
    """
    if tag is None:
        try:
            tag = g._c(None).tag
        except Exception:
            tag = "GER"
    return country_wings(g, tag, refresh=refresh)


def airbase_wings(g, tag=None):
    """纯只读 0 注入：沿引擎 `CStrategicAirManager` 链映射本国所有机场停驻联队与精确省 ID。

    2026-11-26 (P1-1 PE 1.19.5.0 静态反汇编 + 活进程 15/15 实证):
      - `gamestate + 0x690` -> `CStrategicAirManager* (sam)`
      - `sam + 0x48` -> `CPdxArray<CPdxArray<int32>>[state_id]`（陆地空军基地省表，来自 0xCB0DA2）
      - `sam + 0x30` -> `[tag_idx] -> CStrategicAir* (sa)` -> `sa + 0x88 -> CAirBaseManager*`
        -> `+0x90[state_id] -> CAirBase* (ab)` -> `ab + 0x30[tag_idx] -> CAirBaseCountry* (abc)`
        -> `abc + 0xB8 -> CPdxArray<CAirWingPool*>` -> `pool + 0x28 -> CPdxArray<CAirWing*>`
      - 伞降就绪判据（精确复现 0xCA9F40 / 0xFE2B90）：
        `def+0x5A8 & 0x100000` (支持伞降) && `wing+0xA0 == 0` (空闲未挂任务)
        && `wing+0x79 == 0` && `[wing+0x64] >= [wing+0x60]` && `planes > 0`。
    """
    p = getattr(g, "p", None)
    if p is None:
        return {"ok": False, "airbases": [], "by_wing_ptr": {}, "state_provinces": {}}
    c = g._c(tag)
    tid = int(c.tag_id)
    try:
        gs = p.u64(p.base + _OW.G_GAMESTATE)
        if not _heap_ptr(gs):
            return {"ok": False, "tag": c.tag, "tag_id": tid,
                    "airbases": [], "by_wing_ptr": {}, "state_provinces": {}}
        sam = p.u64(gs + GS_STRAT_AIR_MGR)
        if not _heap_ptr(sam):
            return {"ok": False, "tag": c.tag, "tag_id": tid,
                    "airbases": [], "by_wing_ptr": {}, "state_provinces": {}}
    except Exception:
        return {"ok": False, "tag": c.tag, "tag_id": tid,
                "airbases": [], "by_wing_ptr": {}, "state_provinces": {}}

    # 1. 读取引擎陆地机场省表 sam + 0x48 (stride 0x18 per state_id)
    state_provinces = {}
    try:
        lp_data = p.u64(sam + SAM_LAND_AIRBASE_PROVS)
        lp_sz = p.i32(sam + SAM_LAND_AIRBASE_PROVS + 0x0C)
        if _heap_ptr(lp_data) and 0 < lp_sz <= 4096:
            blk = p.read(lp_data, lp_sz * 0x18)
            for sid in range(lp_sz):
                base_off = sid * 0x18
                pdata = struct.unpack_from("<Q", blk, base_off)[0]
                psz = struct.unpack_from("<i", blk, base_off + 0x0C)[0]
                if _heap_ptr(pdata) and 0 < psz <= 64:
                    try:
                        pblk = p.read(pdata, psz * 4)
                        provs = [struct.unpack_from("<i", pblk, k * 4)[0] for k in range(psz)]
                        provs = [pv for pv in provs if pv > 0]
                        if provs:
                            state_provinces[sid] = provs
                    except Exception:
                        continue
    except Exception:
        state_provinces = {}

    # 2. 定位本国 CStrategicAir*
    sa = 0
    try:
        sa_data = p.u64(sam + SAM_STRAT_AIRS)
        sa_sz = p.i32(sam + SAM_STRAT_AIRS + 0x0C)
        if _heap_ptr(sa_data) and 0 < sa_sz <= 1024:
            if 0 <= tid < sa_sz:
                cand = p.u64(sa_data + 8 * tid)
                if _heap_ptr(cand) and p.u32(cand + SA_OWNER) == tid:
                    sa = cand
            if not sa:
                for i in range(sa_sz):
                    cand = p.u64(sa_data + 8 * i)
                    if _heap_ptr(cand):
                        try:
                            if p.u32(cand + SA_OWNER) == tid:
                                sa = cand
                                break
                        except Exception:
                            continue
    except Exception:
        sa = 0

    if not _heap_ptr(sa):
        return {"ok": bool(state_provinces), "tag": c.tag, "tag_id": tid,
                "airbases": [], "by_wing_ptr": {}, "state_provinces": state_provinces}

    # 3. 遍历 CAirBaseManager + 0x90 中的各州 CAirBase*
    airbases = []
    by_wing_ptr = {}
    try:
        abm = p.u64(sa + SA_AIRBASE_MGR)
        if not _heap_ptr(abm):
            return {"ok": True, "tag": c.tag, "tag_id": tid,
                    "airbases": [], "by_wing_ptr": {}, "state_provinces": state_provinces}
        ab_data = p.u64(abm + ABM_AIRBASES)
        ab_sz = p.i32(abm + ABM_AIRBASES + 0x0C)
        if not _heap_ptr(ab_data) or not (0 < ab_sz <= 4096):
            return {"ok": True, "tag": c.tag, "tag_id": tid,
                    "airbases": [], "by_wing_ptr": {}, "state_provinces": state_provinces}
        ab_ptrs = struct.unpack("<%dQ" % ab_sz, p.read(ab_data, ab_sz * 8))
    except Exception:
        return {"ok": bool(state_provinces), "tag": c.tag, "tag_id": tid,
                "airbases": [], "by_wing_ptr": {}, "state_provinces": state_provinces}

    for sid, ab in enumerate(ab_ptrs):
        if not _heap_ptr(ab):
            continue
        try:
            c_data = p.u64(ab + AB_COUNTRIES)
            c_sz = p.i32(ab + AB_COUNTRIES + 0x0C)
            if not _heap_ptr(c_data) or not (0 <= tid < c_sz <= 1024):
                continue
            abc = p.u64(c_data + 8 * tid)
            if not _heap_ptr(abc):
                continue
            p_data = p.u64(abc + ABC_POOLS)
            p_sz = p.i32(abc + ABC_POOLS + 0x0C)
            if not _heap_ptr(p_data) or not (0 < p_sz <= 128):
                continue
            pools = struct.unpack("<%dQ" % p_sz, p.read(p_data, p_sz * 8))
        except Exception:
            continue

        provs = list(state_provinces.get(sid) or ())
        main_prov = provs[0] if provs else None
        ab_wings = []
        transport_planes = 0
        avail_paradrop_planes = 0
        max_transport_range = None

        for pool in pools:
            if not _heap_ptr(pool):
                continue
            try:
                wtype = wing_type(p, pool)
                dptr = p.u64(pool + AWP_DEF)
                mflags = p.u64(dptr + AWDEF_MISSION_FLAGS) if _heap_ptr(dptr) else 0
                can_para = bool(mflags & AWDEF_PARADROP_BIT)
                w_data = p.u64(pool + AWP_WINGS)
                w_sz = p.i32(pool + AWP_WINGS + 0x0C)
                if not _heap_ptr(w_data) or not (0 < w_sz <= 256):
                    continue
                w_ptrs = struct.unpack("<%dQ" % w_sz, p.read(w_data, w_sz * 8))
            except Exception:
                continue

            for w in w_ptrs:
                if not _heap_ptr(w):
                    continue
                try:
                    handle = p.u64(w + 0x18)
                    planes = int(p.u32(w + WING_PLANES))
                    mission = int(p.u32(w + WING_MISSION))
                    mission_on = bool(p.u32(w + WING_MISSION_ON))
                    busy_flag = int(p.u8(w + WING_BUSY_0x79))
                    min_c = int(p.i32(w + WING_MIN_0x60))
                    cur_c = int(p.i32(w + WING_CUR_0x64))
                    rng = _read_wing_range_km(p, w)
                except Exception:
                    continue
                para_ready = bool(
                    can_para and planes > 0 and mission == 0
                    and busy_flag == 0 and cur_c >= min_c
                )
                is_transport = bool(can_para or str(wtype).startswith("transport"))
                if is_transport:
                    transport_planes += planes
                    if para_ready:
                        avail_paradrop_planes += planes
                        if rng is not None:
                            max_transport_range = max(max_transport_range or 0.0, rng)
                    elif rng is not None and max_transport_range is None:
                        max_transport_range = rng
                wmeta = {
                    "ptr": hex(w),
                    "handle": hex(handle),
                    "id": handle >> 32,
                    "type": wtype,
                    "planes": planes,
                    "mission": mission,
                    "mission_name": mission_name(mission),
                    "mission_on": mission_on,
                    "range_km": rng,
                    "air_base_state": sid,
                    "air_base_province": main_prov,
                    "air_base_provinces": provs,
                    "can_paradrop": can_para,
                    "paradrop_ready": para_ready,
                }
                by_wing_ptr[int(w)] = wmeta
                ab_wings.append(wmeta)

        if ab_wings:
            airbases.append({
                "state_id": sid,
                "province": main_prov,
                "provinces": provs,
                "wing_count": len(ab_wings),
                "total_planes": sum(x["planes"] for x in ab_wings),
                "transport_planes": transport_planes,
                "available_paradrop_planes": avail_paradrop_planes,
                "max_transport_range_km": max_transport_range,
                "wings": ab_wings,
            })

    airbases.sort(key=lambda r: (-r["available_paradrop_planes"], -r["transport_planes"], r["state_id"]))
    return {
        "ok": True,
        "tag": c.tag,
        "tag_id": tid,
        "airbases": airbases,
        "by_wing_ptr": by_wing_ptr,
        "state_provinces": state_provinces,
    }


def country_wings(g, tag, refresh=False, wings=None):
    """Every CAirWing owned by `tag`, via CStrategicAir+0x90 (see german_wings)."""
    p = g.p
    c = g._c(tag)
    tid = c.tag_id
    allw = all_wings(g, refresh=refresh) if wings is None else wings
    ab_info = {}
    try:
        ab_info = airbase_wings(g, c.tag) or {}
    except Exception:
        ab_info = {}
    by_ptr = ab_info.get("by_wing_ptr") or {}
    rows = []
    seen_sa = set()
    for w in allw:
        sa = w.get("sa")
        if not sa:
            continue
        try:
            owner = p.u32(sa + SA_OWNER)
        except Exception:
            continue
        if owner != tid:
            continue
        seen_sa.add(sa)
        row = {"ptr": hex(w["ptr"]), "handle": hex(w["handle"]),
               "id": w["handle"] >> 32, "type": w["type"]}
        row.update(wing_state(g, w["ptr"]))
        meta = by_ptr.get(int(w["ptr"]))
        if meta:
            row["air_base_state"] = meta.get("air_base_state")
            row["air_base_province"] = meta.get("air_base_province")
            if meta.get("air_base_provinces"):
                row["air_base_provinces"] = meta["air_base_provinces"]
            if meta.get("range_km") is not None:
                row["range_km"] = meta["range_km"]
            row["can_paradrop"] = bool(meta.get("can_paradrop"))
            row["paradrop_ready"] = bool(meta.get("paradrop_ready"))
        rows.append(row)
    rows.sort(key=lambda r: r["id"])
    # 补充正在转场联队的目标州/省反查映射（0 注入纯只读）
    ab_by_handle = {}
    if any(r.get("transfer_target_handle") for r in rows):
        try:
            for ab in (all_airbases(g, c.tag, only_controlled=False) or ()):
                if ab.get("handle_hex"):
                    ab_by_handle[ab["handle_hex"].lower()] = ab
        except Exception:
            ab_by_handle = {}
    for row in rows:
        th = row.get("transfer_target_handle")
        if th and th.lower() in ab_by_handle:
            tab = ab_by_handle[th.lower()]
            row["transferring_to_state"] = tab.get("state_id")
            if tab.get("province") is not None:
                row["transferring_to_province"] = tab.get("province")

    return {"ok": bool(rows), "count": len(rows), "tag": c.tag,
            "owner_tag_id": tid, "sa": hex(next(iter(seen_sa))) if seen_sa else None,
            "ownership": "CStrategicAir+0x90 (exact, not a heuristic)",
            "initial_run": len(rows), "wings": rows}


def all_airbases(g, tag=None, only_controlled=True):
    """纯只读枚举空军基地（CAirBaseManager+0x90[state_id] -> CAirBase*），含句柄、省 ID、等级容量与驻机数。

    2026-11-26: 支撑 `air_transfer` 精准按 state_id / province_id 定位目标 `CAirBase` 句柄（type 65）。
    """
    p = getattr(g, "p", None)
    if p is None or not hasattr(p, "u64"):
        return []
    try:
        c = g._c(tag)
        gs = g.gamestate
        if not _heap_ptr(gs):
            return []
        sam = p.u64(gs + GS_STRAT_AIR_MGR)
        if not _heap_ptr(sam):
            return []
        # 解析 tag_idx
        tidx = int(c.tag_id)
        try:
            g._load_tags()
            t2i = getattr(g, "_tag2idx", None)
            if isinstance(t2i, dict):
                tidx = int(t2i.get(c.tag_id, c.tag_id))
            elif isinstance(t2i, (list, tuple)) and 0 <= c.tag_id < len(t2i):
                tidx = int(t2i[c.tag_id])
        except Exception:
            pass
        sa_data = p.u64(sam + SAM_STRAT_AIRS)
        sa_sz = p.i32(sam + SAM_STRAT_AIRS + 0x0C)
        if not _heap_ptr(sa_data) or not (0 <= tidx < sa_sz):
            return []
        sa = p.u64(sa_data + 8 * tidx)
        if not _heap_ptr(sa):
            return []
        abm = p.u64(sa + SA_AIRBASE_MGR)
        if not _heap_ptr(abm):
            return []
        ab_data = p.u64(abm + ABM_AIRBASES)
        ab_sz = p.i32(abm + ABM_AIRBASES + 0x0C)
        if not _heap_ptr(ab_data) or not (0 < ab_sz <= 4096):
            return []
    except Exception:
        return []

    ab_wings = airbase_wings(g, c.tag) or {}
    state_provs = ab_wings.get("state_provinces") or {}
    planes_by_state = {
        int(x["state_id"]): int(x.get("total_planes") or 0)
        for x in (ab_wings.get("airbases") or ())
        if x.get("state_id") is not None
    }
    wings_cnt_by_state = {
        int(x["state_id"]): int(x.get("wing_count") or 0)
        for x in (ab_wings.get("airbases") or ())
        if x.get("state_id") is not None
    }

    allowed_states = None
    if only_controlled:
        try:
            allowed_states = {int(s["id"]) for s in (g.states(c.tag) or []) if s.get("id")}
            allowed_states.update(planes_by_state.keys())
        except Exception:
            allowed_states = None

    raw_ptrs = p.read(ab_data, 8 * ab_sz)
    out = []
    for sid in range(ab_sz):
        if allowed_states is not None and sid not in allowed_states:
            continue
        abp = struct.unpack_from("<Q", raw_ptrs, 8 * sid)[0]
        if not _heap_ptr(abp):
            continue
        try:
            ht, hi = struct.unpack("<ii", p.read(abp + 0x08, 8))
        except Exception:
            continue
        if ht <= 0 or hi < 0:
            continue
        packed = (hi << 32) | (ht & 0xFFFFFFFF)
        provs = list(state_provs.get(sid) or ())
        main_prov = provs[0] if provs else None
        rec = {
            "state_id": sid,
            "province": main_prov,
            "provinces": provs,
            "airbase_ptr": hex(abp),
            "handle": [ht, hi],
            "packed_handle": packed,
            "handle_hex": hex(packed),
            "wing_count": wings_cnt_by_state.get(sid, 0),
            "current_planes": planes_by_state.get(sid, 0),
        }
        if only_controlled:
            try:
                det = g.state_detail(sid, c.tag) or {}
                if det.get("name"):
                    rec["state_name"] = det["name"]
                lv = int((det.get("buildings") or {}).get("air_base") or 0)
                if lv > 0:
                    rec["level"] = lv
                    rec["capacity"] = lv * 200
                    rec["free_capacity"] = max(0, lv * 200 - rec["current_planes"])
            except Exception:
                pass
        out.append(rec)
    out.sort(key=lambda r: (-int(r.get("level") or 0), -int(r.get("current_planes") or 0), r["state_id"]))
    return out


def find_airbase(g, state_id=None, province_id=None, tag=None):
    """按 state_id 或 province_id 精确查找目标 `CAirBase` 记录（含 type-65 句柄与容量）。"""
    want_state = int(state_id) if state_id is not None else None
    want_prov = int(province_id) if province_id is not None else None
    if want_state is None and want_prov is None:
        return None
    # 先查本国控制机场（附带 level/capacity/name），若未命中再查全球机场（支持盟友机场）
    for only_ctrl in (True, False):
        for ab in all_airbases(g, tag=tag, only_controlled=only_ctrl):
            if want_state is not None and ab.get("state_id") == want_state:
                return ab
            if want_prov is not None:
                if ab.get("province") == want_prov or want_prov in (ab.get("provinces") or ()):
                    return ab
    # 若传了 province_id 但该省不是机场主省，按 mapdata 省->州映射反查所在州的机场
    if want_prov is not None and want_state is None:
        try:
            import mapdata as _md_ab
            _, p2s = _md_ab._load_state_provinces_raw()
            sid = (p2s or {}).get(want_prov)
            if sid:
                return find_airbase(g, state_id=int(sid), province_id=None, tag=tag)
        except Exception:
            pass
    return None


def select_wings(g, spec, tag=None):
    """Resolve a wing selector to [(id, packed handle)] for the player's wings.

    spec (case-insensitive):
        "3" / 3 / "0x..."  one wing id (or packed handle / raw pointer)
        "1,2,3" / [1,2,3]  several wing ids
        "fighter" / "fighters" / "transport" / "transport_plane" -> every player wing of that plane type
        "all"              -> every player wing
    """
    ger = german_wings(g, tag=tag)
    if not ger.get("ok"):
        raise ValueError(ger.get("error") or "no player air-wing block")
    rows = ger["wings"]

    def _hv(r):
        return int(r["handle"], 16)

    if isinstance(spec, (list, tuple)):
        spec = ",".join(str(x) for x in spec)
    s = str(spec).strip().lower()
    if s in ("all", "*", ""):
        return [(r["id"], _hv(r)) for r in rows]
    alias = {"fighters": "fighter", "bombers": "tac_bomber",
             "tactical": "tac_bomber", "naval_bomber": "nav_bomber",
             "transport": "transport_plane", "transports": "transport_plane",
             "close_air_support": "cas"}
    s = alias.get(s, s)
    if s in {r["type"] for r in rows}:
        return [(r["id"], _hv(r)) for r in rows if r["type"] == s]
    out = []
    by_ptr = {r["ptr"].lower(): r for r in rows}
    by_handle = {r["handle"].lower(): r for r in rows}
    for tok in s.replace(";", ",").split(","):
        tok = tok.strip()
        if not tok:
            continue
        if tok in by_ptr:
            out.append((by_ptr[tok]["id"], _hv(by_ptr[tok])))
            continue
        if tok in by_handle:
            out.append((by_handle[tok]["id"], _hv(by_handle[tok])))
            continue
        v = int(tok, 0) if tok.startswith("0x") else int(tok)
        if v > 0xFFFFFFFF:                      # packed handle
            hk = hex(v)
            if hk not in by_handle:
                raise ValueError("wing handle %s is not one of the player's wings" % hk)
            out.append((by_handle[hk]["id"], v))
            continue
        hit = [r for r in rows if r["id"] == v]
        if not hit:
            raise ValueError("wing id %d is not one of the player's wings "
                             "(ids %s)" % (v, [r["id"] for r in rows]))
        out.append((v, _hv(hit[0])))
    return out


# ---------------------------------------------------------------- builders
def _handle_array_block(game, handles, cap_pad=0):
    """A heap block holding an array of packed 8-byte {type,id} handles.

    Returns (data_ptr, count). The block lives in the game allocator, so the
    command can own it; it is intentionally leaked (a few bytes per order).
    """
    import cmdwin
    p = game.p
    handles = [int(h) & 0xFFFFFFFFFFFFFFFF for h in handles]
    n = len(handles)
    if not n:
        return 0, 0
    data = cmdwin.alloc(game, 8 * (n + cap_pad))
    p.write(data, b"".join(struct.pack("<Q", h) for h in handles))
    return data, n


def _array_fields(game, off, handles):
    """(offset, fmt, value) triples describing a CPdxArray at `off`."""
    import offsets_win as W
    data, n = _handle_array_block(game, handles)
    return [(off, "<Q", data), (off + 8, "<i", n), (off + 0xC, "<i", n),
            (off + 0x10, "<Q", game.p.base + W.G_DEFAULT_ALLOC)]


def make_attach(game, wing_handles, army_handle):
    """CAttachAirWingToArmyCommand (no ctor call)."""
    import cmdwin
    fields = _array_fields(game, 0x28, wing_handles)
    fields.append((0x40, "<Q", int(army_handle) & 0xFFFFFFFFFFFFFFFF))
    return cmdwin.build_by_vt(game, VT_ATTACH, SIZE_ATTACH, fields)


def make_set_mission(game, wing_handles, mission_mask, flag1=1, flag2=1):
    """CStratAirSetMissionCommand (no ctor call)."""
    import cmdwin
    fields = _array_fields(game, 0x28, wing_handles)
    fields += [(0x40, "<i", int(mission_mask)),
               (0x44, "<B", int(bool(flag1)) & 0xFF),
               (0x45, "<B", int(bool(flag2)) & 0xFF),
               (0x46, "<B", 0)]
    return cmdwin.build_by_vt(game, VT_SET_MISSION, SIZE_SET_MISSION, fields)


def make_transfer(game, wing_handle, airbase_handle=0, to_reserve=False,
                  keep_army=False, region_id=0, mission_mask=0):
    """构建 CStratAirTransferCommand（72 字节纯标量平铺结构体，经 PE 1.19.5.0 完整反汇编实证）。

    字段映射（ctor 0x19CCCF0 / clone 0x19CF3F0 / CanExecute 0x19D4CC0 / Execute 0x19D2090）：
      +0x28 u64  wing_handle (type 69)
      +0x30 u64  target_airbase_handle (type 65; to_reserve=True 时可为 0)
      +0x38 u8   to_reserve (0=转场到目标机场, 1=解散回后备机库)
      +0x39 u8   keep_army_attachment (0=自动脱离集团军挂靠, 与游戏 ctor 行为一致)
      +0x3C i32  target_strategic_region_id (0=不改空域)
      +0x40 i32  target_mission_mask (0=不改任务)
    """
    import cmdwin
    if not to_reserve and not int(airbase_handle or 0):
        raise ValueError("CStratAirTransferCommand requires non-zero airbase_handle when to_reserve=False")
    fields = [
        (0x28, "<Q", int(wing_handle) & 0xFFFFFFFFFFFFFFFF),
        (0x30, "<Q", int(airbase_handle or 0) & 0xFFFFFFFFFFFFFFFF),
        (0x38, "<B", 1 if to_reserve else 0),
        (0x39, "<B", 1 if keep_army else 0),
        (0x3C, "<i", int(region_id or 0)),
        (0x40, "<i", int(mission_mask or 0)),
    ]
    return cmdwin.build_by_vt(game, VT_TRANSFER, SIZE_TRANSFER, fields)


_TRANSFER_MISSION_ALIASES = {
    "none": 0, "off": 0, "keep": 0,
    "air_superiority": 1, "superiority": 1,
    "cas": 2, "close_air_support": 2,
    "interception": 4, "intercept": 4,
    "strategic_bombing": 8, "strat_bombing": 8,
    "naval_strike": 16,
    "naval_patrol": 32,
    "port_strike": 64,
    "logistics_strike": 128,
}


def transfer_wings(g, wings, target_state=None, target_province=None,
                   to_reserve=False, keep_army=False, region_id=0, mission=0,
                   tag=None, dry_run=False):
    """将一支或多支空军联队转场至指定州/省空军基地（或解散回后备机库）。

    特性：
      1. 0 注入前置校验：支持按州 ID (`target_state`) 或机场省 ID (`target_province`) 定位
         目标 `CAirBase` 句柄（type 65），无机场时明确拒拦并列出可用机场；
      2. 幂等跳过已在目标机场或已在向同一目标转场的联队，避免重复发令被 CanExecute 拒；
      3. 估算目标机场转场后驻机数与容量（`level * 200`），超容自动附 `capacity_warning`；
      4. `dry_run=True` 完全 0 注入返回完整预览；实写前逐联队跑 `CanExecute (0x19D4CC0)`，
         经 `post_drained(done_fn=...)` 暂停态安全消费，并立即回读 `wing + 0x48` 确认转场生效。
    """
    import cmdwin
    c = g._c(tag)

    # 1. 解析可选 mission 参数
    if isinstance(mission, str):
        ml = mission.strip().lower()
        if ml in _TRANSFER_MISSION_ALIASES:
            m_mask = _TRANSFER_MISSION_ALIASES[ml]
        else:
            try:
                m_mask = int(ml, 0)
            except ValueError:
                return {
                    "ok": False,
                    "blocked_by": "invalid_mission",
                    "error": "unknown mission %r (expected 0/none or %s)"
                             % (mission, ", ".join(sorted(MISSION_NAMES.values()))),
                }
    else:
        m_mask = int(mission or 0)

    # 2. 解析目标机场
    target_ab = None
    if not to_reserve:
        if target_state is None and target_province is None:
            avail = all_airbases(g, c.tag, only_controlled=True)
            return {
                "ok": False,
                "blocked_by": "missing_target_airbase",
                "error": "air_transfer requires target_state or target_province (unless to_reserve=true)",
                "available_airbases": [
                    {k: x.get(k) for k in ("state_id", "state_name", "province", "level", "capacity", "current_planes", "free_capacity") if x.get(k) is not None}
                    for x in avail[:20]
                ],
            }
        target_ab = find_airbase(g, state_id=target_state, province_id=target_province, tag=c.tag)
        if target_ab is None:
            avail = all_airbases(g, c.tag, only_controlled=True)
            return {
                "ok": False,
                "blocked_by": "no_airbase_at_target",
                "target_state": target_state,
                "target_province": target_province,
                "error": (
                    "no CAirBase found at target (state=%r, province=%r): build an air_base there first "
                    "or pick one of available_airbases." % (target_state, target_province)
                ),
                "available_airbases": [
                    {k: x.get(k) for k in ("state_id", "state_name", "province", "level", "capacity", "current_planes", "free_capacity") if x.get(k) is not None}
                    for x in avail[:20]
                ],
            }

    # 3. 解析目标联队集合
    try:
        picked = select_wings(g, wings, tag=c.tag)
    except Exception as e:
        return {"ok": False, "blocked_by": "invalid_wings", "error": str(e)}
    if not picked:
        return {"ok": False, "blocked_by": "no_matching_wings",
                "error": "selector %r matched no player air wings" % (wings,)}

    cw = country_wings(g, c.tag) or {}
    by_id = {int(w["id"]): w for w in (cw.get("wings") or []) if w.get("id") is not None}
    target_h = int(target_ab["packed_handle"]) if target_ab else 0
    target_sid = target_ab.get("state_id") if target_ab else None
    target_prov = target_ab.get("province") if target_ab else None

    to_send = []
    skipped = []
    moving_planes = 0
    for wid, wh in picked:
        wrow = by_id.get(int(wid)) or {}
        cur_state = wrow.get("air_base_state")
        cur_tr = str(wrow.get("transfer_target_handle") or "").lower()
        planes_n = int(wrow.get("planes") or 0)
        if not to_reserve:
            if cur_state == target_sid and not cur_tr:
                skipped.append({
                    "id": wid, "type": wrow.get("type"), "planes": planes_n,
                    "status": "already_at_target", "air_base_state": cur_state,
                })
                continue
            if cur_tr and cur_tr == hex(target_h).lower():
                skipped.append({
                    "id": wid, "type": wrow.get("type"), "planes": planes_n,
                    "status": "already_transferring", "transfer_target_handle": cur_tr,
                })
                continue
        moving_planes += planes_n
        to_send.append({
            "id": wid,
            "handle": wh,
            "ptr": int(wrow["ptr"], 16) if wrow.get("ptr") else 0,
            "type": wrow.get("type"),
            "planes": planes_n,
            "from_state": cur_state,
            "from_province": wrow.get("air_base_province"),
        })

    cap_warn = None
    cap_after = None
    if target_ab and target_ab.get("capacity"):
        cap_total = int(target_ab["capacity"])
        cur_p = int(target_ab.get("current_planes") or 0)
        est_p = cur_p + moving_planes
        cap_after = {"current_planes": cur_p, "incoming_planes": moving_planes,
                     "estimated_total_planes": est_p, "capacity": cap_total,
                     "overcrowded": est_p > cap_total}
        if est_p > cap_total:
            cap_warn = (
                "target airbase state %s (%s) capacity is %d planes (level %s), "
                "but transfer will bring estimated total to %d (+%d overcrowded; mission efficiency penalty applies)"
                % (target_sid, target_ab.get("state_name") or "?", cap_total,
                   target_ab.get("level"), est_p, est_p - cap_total)
            )

    out_base = {
        "dry_run": bool(dry_run),
        "to_reserve": bool(to_reserve),
        "target_state": target_sid,
        "target_state_name": target_ab.get("state_name") if target_ab else None,
        "target_province": target_prov,
        "target_airbase_handle": target_ab.get("handle") if target_ab else None,
        "target_airbase_handle_hex": hex(target_h) if target_h else None,
        "wings_selected": len(picked),
        "to_transfer_count": len(to_send),
        "already_at_target_count": len(skipped),
        "capacity_estimate": cap_after,
        "capacity_warning": cap_warn,
    }

    if not to_send:
        return {
            **out_base,
            "ok": True,
            "transferred_count": 0,
            "failed_count": 0,
            "skipped": skipped,
            "results": [],
            "note": "all selected wing(s) are already at or transferring to the target airbase",
        }

    if dry_run:
        return {
            **out_base,
            "ok": True,
            "planned_transfers": [
                {
                    "id": x["id"], "type": x["type"], "planes": x["planes"],
                    "from_state": x["from_state"], "from_province": x["from_province"],
                    "to_state": target_sid, "to_province": target_prov,
                    "handle": hex(x["handle"]),
                }
                for x in to_send
            ],
            "skipped": skipped,
        }

    # 4. 逐联队 CanExecute 预检 + 暂停态安全排空 + 立即回读 wing+0x48
    p = g.p
    results = []
    ok_cnt = 0
    fail_cnt = 0
    for item in to_send:
        wid = item["id"]
        wh = item["handle"]
        wptr = item["ptr"]
        try:
            cmd = make_transfer(
                g, wh, target_h,
                to_reserve=bool(to_reserve),
                keep_army=bool(keep_army),
                region_id=int(region_id or 0),
                mission_mask=m_mask,
            )
            can = can_exec(g, cmd, CANEXEC_TRANSFER)
            if not can:
                fail_cnt += 1
                results.append({
                    "id": wid, "type": item["type"], "ok": False,
                    "can_execute": False, "posted": 0,
                    "from_state": item["from_state"],
                    "error": "CStratAirTransferCommand::CanExecute refused (check carrier/land mismatch, wing readiness +0x64/+0x60, or airbase ownership)",
                })
                continue

            def _transfer_done(_wp=wptr, _th=target_h, _res=bool(to_reserve)):
                if not _heap_ptr(_wp):
                    return False
                try:
                    if _res:
                        return bool(p.u8(_wp + WING_BUSY_0x79))
                    return int(p.u64(_wp + WING_TRANSFER_TARGET)) == _th
                except Exception:
                    return False

            try:
                pr = cmdwin.post_drained(g, cmd, seconds=2.0, done_fn=_transfer_done)
            except TypeError:
                pr = cmdwin.post_drained(g, cmd, seconds=2.0)

            read_tr = None
            verified = False
            arrived = False
            if _heap_ptr(wptr):
                try:
                    if to_reserve:
                        verified = bool(p.u8(wptr + WING_BUSY_0x79))
                        arrived = verified
                    else:
                        read_tr = int(p.u64(wptr + WING_TRANSFER_TARGET))
                        verified = (read_tr == target_h)
                        if not verified:
                            # 极快完成或瞬移场景：复查是否已直接落入目标机场
                            ab_after = airbase_wings(g, c.tag) or {}
                            meta = (ab_after.get("by_wing_ptr") or {}).get(wptr) or {}
                            if meta.get("air_base_state") == target_sid:
                                verified = True
                                arrived = True
                except Exception:
                    pass
            row_ok = bool(pr.get("posted")) and (verified or not _heap_ptr(wptr))
            if row_ok:
                ok_cnt += 1
            else:
                fail_cnt += 1
            in_transit = bool(row_ok and (not to_reserve) and (not arrived))
            results.append({
                "id": wid,
                "type": item["type"],
                "planes": item["planes"],
                "from_state": item["from_state"],
                "to_state": target_sid,
                "ok": row_ok,
                "can_execute": True,
                "verified": verified,
                "order_verified": verified,
                "arrived": arrived,
                "in_transit": in_transit,
                "transfer_target_readback": hex(read_tr) if read_tr else None,
                "posted": pr.get("posted"),
                "drained": pr.get("drained"),
            })
        except Exception as exc:
            fail_cnt += 1
            results.append({
                "id": wid, "type": item["type"], "ok": False,
                "error": "%s: %s" % (type(exc).__name__, exc),
            })

    in_transit_n = sum(1 for r in results if r.get("in_transit"))
    res_out = {
        **out_base,
        "ok": ok_cnt > 0 and fail_cnt == 0,
        "transferred_count": ok_cnt,
        "failed_count": fail_cnt,
        "in_transit_count": in_transit_n,
        "arrived": bool(ok_cnt > 0 and in_transit_n == 0),
        "skipped": skipped,
        "results": results,
    }
    if in_transit_n > 0:
        res_out["arrival_note"] = (
            "%d wing(s) accepted transfer order and are IN TRANSIT to state %s (province %s); "
            "air_base_state will stay at the origin airport until game time advances and the wings land. "
            "Check inspect(topic='air') until transferring is cleared before launching paradrops from the destination."
            % (in_transit_n, target_sid, target_prov)
        )
    return res_out


def make_enable_mission(game, wing_handles, mission_mask, target_handles=()):
    """CStratAirEnableMissionCommand -- BLOCKED, see below.

    2026-11-16 crash #4: a CAirWing handle was put into the SECOND handle
    array at +0x40; CanExecute 0x19D4740 resolves every entry and dereferences
    the result, killing hoi4.exe.

    Follow-up disassembly (§77.3) established that +0x40 is actually
    OPTIONAL: ctor 0x19CC900 only fills it when rsi != 0, CanExecute skips
    the whole second-array loop when +0x4c == 0, and Execute /
    0x19CD020 both handle target=NULL. Repeated probes with an EMPTY target
    array returned CanExecute=0 with the game alive. The remaining rejection
    is a business gate in 0x19CD370 -> 0x19CD546: it reads
    [cmd->vtable + 0x58] as a strategic-region id, calls
    0x140FFBCD0(regionObj, wingObj) and requires 2 plus a region-id match.
    That needs a region-specialised vtable/context we refuse to fabricate.

    No safe command can be built here today, so this stays disabled and the
    class stays out of WIN_SAFE. hoi4_air_mission uses SetMission only.
    """
    raise RuntimeError(
        "CStratAirEnableMissionCommand is BLOCKED: the +0x40 target array is "
        "optional (leaving it empty is safe), but CanExecute still requires a "
        "region-specialised vtable/context via [cmd->vtable+0x58] (0x19CD546). "
        "A wrong handle crashed hoi4.exe on 2026-11-16 (crash #4); we do not "
        "guess that context. Use set_mission only.")


def can_exec(game, cmd, fn_rva):
    """Ask the game's own CanExecute on an inert command (pure validation)."""
    return bool(game.call(game.p.base + fn_rva, (cmd, 0),
                          absolute=True, force=True) & 0xFF)


def post(game, cmd):
    """Enqueue with a brief clock run; a paused game never drains otherwise."""
    import cmdwin
    return cmdwin.post_drained(game, cmd)
