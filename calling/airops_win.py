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

VT_AIRWING_MAIN = 0x29F7748
VT_AIRWING_SECONDARY = 0x29F7780
VT_AIRWINGPOOL = 0x29F77D8
#: CStrategicAir + 0x90 = u32 CCountryTag id of the air force's owner
#: (measured 2026-11-19: 44 groups, 44 distinct tag ids, ENG 36 wings / GER 15)
SA_OWNER = 0x90

# --- CAirWing live state (measured 2026-11-16 on the 15 German wings) ------
# +0x7C  i32  planes currently in the wing      (100 fighter / 76..80 bomber)
# +0xA0  i32  mission mask  (1 air superiority, 2 cas, 4 interception, ...)
# +0xA4  i32  1 while that mission is ENABLED
# +0x80  i32  derived total (planes x equipment cost) -- not used in output
# CStratAirSetMissionCommand with flag1=flag2=True writes BOTH +0xA0 and
# +0xA4, i.e. the engine's own set-mission command already enables the
# mission.  CStratAirEnableMissionCommand is therefore NOT needed for a
# normal player order (verified: wing 2 +0xA0 0->1 and +0xA4 0->1).
WING_PLANES = 0x7C
WING_MISSION = 0xA0
WING_MISSION_ON = 0xA4

MISSION_NAMES = {1: "air_superiority", 2: "cas", 4: "interception",
                 8: "strategic_bombing", 16: "naval_strike",
                 32: "naval_patrol", 64: "port_strike", 128: "logistics_strike"}


def mission_name(mask):
    """Human name for a mission mask (bit 0 wins; 0 means none)."""
    if not mask:
        return None
    return MISSION_NAMES.get(mask & -mask, "mask_%d" % mask)


def wing_state(g, w):
    """Live planes / mission / enabled for one CAirWing pointer."""
    p = g.p
    try:
        return {"planes": p.u32(w + WING_PLANES),
                "mission": p.u32(w + WING_MISSION),
                "mission_name": mission_name(p.u32(w + WING_MISSION)),
                "mission_on": p.u32(w + WING_MISSION_ON) != 0}
    except Exception:
        return {"planes": None, "mission": None, "mission_name": None,
                "mission_on": None}

VT_ATTACH = 0x2A97728
SIZE_ATTACH = 0x48
CANEXEC_ATTACH = 0x19D3920
VT_SET_MISSION = 0x2A96BE8
SIZE_SET_MISSION = 0x48
CANEXEC_SET_MISSION = 0x19D4B30
VT_ENABLE_MISSION = 0x2A96B28
SIZE_ENABLE_MISSION = 0x60
CANEXEC_ENABLE_MISSION = 0x19D4740

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


_AUTO_REFRESH_DONE = {}
_AUTO_REFRESH_EMPTY_AT = {}


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


def country_wings(g, tag, refresh=False, wings=None):
    """Every CAirWing owned by `tag`, via CStrategicAir+0x90 (see german_wings)."""
    p = g.p
    c = g._c(tag)
    tid = c.tag_id
    allw = all_wings(g, refresh=refresh) if wings is None else wings
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
        rows.append(row)
    rows.sort(key=lambda r: r["id"])
    return {"ok": bool(rows), "count": len(rows), "tag": c.tag,
            "owner_tag_id": tid, "sa": hex(next(iter(seen_sa))) if seen_sa else None,
            "ownership": "CStrategicAir+0x90 (exact, not a heuristic)",
            "initial_run": len(rows), "wings": rows}


def select_wings(g, spec):
    """Resolve a wing selector to [(id, packed handle)] for the player's wings.

    spec (case-insensitive):
        "3" / "0x..."   one wing id (or packed handle / raw pointer)
        "1,2,3"         several wing ids
        "fighter" / "fighters"  -> every player wing of that plane type
        "all"           -> every player wing
    Only the deterministic id-1..k starting block is ever returned, so a typo
    can never touch a foreign air force.
    """
    ger = german_wings(g)
    if not ger.get("ok"):
        raise ValueError(ger.get("error") or "no player air-wing block")
    rows = ger["wings"]

    def _hv(r):
        return int(r["handle"], 16)

    s = str(spec).strip().lower()
    if s in ("all", "*", ""):
        return [(r["id"], _hv(r)) for r in rows]
    alias = {"fighters": "fighter", "bombers": "tac_bomber",
             "tactical": "tac_bomber", "naval_bomber": "nav_bomber",
             "transports": "transport_plane", "close_air_support": "cas"}
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
