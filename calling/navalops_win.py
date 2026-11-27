"""Naval task-force orders on HOI4 1.19.5.0 (PE / Windows).

The legacy Linux offsets (ELF ctor addresses 0x2253A90 etc., fleet array at
+0xB0 and TF handle at +0x18 as a *pointer*) do not match hoi4.exe. Everything
below was recovered with research/windows/pedis.py and verified live on
2026-11-16 (pid 10696).

Object layout (all RVAs are vtable / function RVAs in hoi4.exe):

  CFleet                       vptr 0x29DF3D8
      +0x08  packed handle {type=0x3D(61), id}
      +0x48  CPdxArray<CStrategicNavalRegion*> assigned regions (data/cap/size)
      +0x54  i32 region count (same as the array's size, read by CanExecute)
      +0xB8  CPdxArray<CTaskForce*>  (Linux used +0xB0)
      +0xE0  MSVC std::string fleet name
  CTaskForce                   vptr 0x29DF900 (secondary 0x29DFAE0 / ...)
      +0x10  secondary interface pointer; the handle resolves to tf+0x10
      +0x18  packed handle {type=0x33(51), id}  <- what the commands want
      +0x60  CPdxArray ship/composition data
      +0xA0  i32 ship count
      +0x360 embedded CNavalMission (vptr 0x29FB658 after a mission is set)
      +0x374 i32 current mission type  (0=None, 1=Patrol, 2=Strike Force,
             3=Convoy Raiding, 4=Convoy Escort, 9=Naval Invasion Support)

Command layouts (built with cmdwin.build_by_vt, NO constructor call -- the
pecmd ctor/size for these classes is wrong):

  CNavalMissionSetTypeCommand   vt 0x2A2A808, size 0x50
      +0x28  CPdxArray<packed CTaskForce handle>
             {data +0x28, cap +0x30, size +0x34, IAllocator* +0x38}
      +0x40  i32 mission type (CanExecute requires 0..9)
      +0x44  byte flag 0, +0x45 byte flag 1
      CanExecute 0x13EE8C0 (vt+0x48), Execute 0x13E6C40 (vt+0x50)
      CanExecute resolves EACH handle with 0x2284870; a handle of the wrong
      type is dereferenced -> crash risk. We only ever pass tf+0x18.
  CNavalMissionAddRegionCommand vt 0x2A2A988, size 0x38
      +0x28  packed CFleet handle (fleet+0x08)
      +0x30  i32 strategic naval region id
      CanExecute 0x13EDF70 (vt+0x48), Execute 0x13E5FE0 (vt+0x50)
  CNavalMissionRemoveRegionCommand vt 0x2A2AA48 (layout not exercised here)

Region table (global pointer at image+0x33DD6B0):
      [gp+0x2E0] array of CStrategicNavalRegion*, [gp+0x2EC] count (305).
      A region is usable only when [region+0xA0] > 0 -- CanExecute (tail
      0x140DBA393) dereferences that pointer WITHOUT a null check. Feeding it
      an out-of-range id killed hoi4.exe on 2026-11-16 (crash #5). Exactly 98
      of the 304 regions are valid; we validate before every call.

VERIFIED LIVE 2026-11-16:
  SetType Patrol on fleet 0 / TF 0 -> tf+0x374 0->1, CNavalMission vptr set.
  AddRegion 9 on fleet 0         -> fleet+0x54 0->1, +0x48 array holds the
                                     region-9 object pointer.
Both used cmdwin.post_drained while the game was paused; date advanced and
pause was restored.
"""
from __future__ import annotations
import struct
import sys

import cmdwin
import offsets_win as _OW
from commands import BASE_INIT

VT_SET_TYPE = _OW.NAVAL_VT_SET_TYPE
SIZE_SET_TYPE = _OW.NAVAL_SIZE_SET_TYPE
CANEXEC_SET_TYPE = _OW.NAVAL_CANEXEC_SET_TYPE
EXEC_SET_TYPE = _OW.NAVAL_EXEC_SET_TYPE

VT_ADD_REGION = _OW.NAVAL_VT_ADD_REGION
SIZE_ADD_REGION = _OW.NAVAL_SIZE_ADD_REGION
CANEXEC_ADD_REGION = _OW.NAVAL_CANEXEC_ADD_REGION
EXEC_ADD_REGION = _OW.NAVAL_EXEC_ADD_REGION

VT_REMOVE_REGION = _OW.NAVAL_VT_REMOVE_REGION
SIZE_REMOVE_REGION = _OW.NAVAL_SIZE_REMOVE_REGION
CANEXEC_REMOVE_REGION = _OW.NAVAL_CANEXEC_REMOVE_REGION
EXEC_REMOVE_REGION = _OW.NAVAL_EXEC_REMOVE_REGION

FLEET_VT = _OW.NAVAL_FLEET_VT
TASKFORCE_VT = _OW.NAVAL_TASKFORCE_VT
FLEET_HANDLE_OFF = _OW.NAVAL_FLEET_HANDLE_OFF
FLEET_NAME_OFF = _OW.NAVAL_FLEET_NAME_OFF             # MSVC std::string (name may be heap-allocated)
FLEET_REGION_COUNT_OFF = _OW.NAVAL_FLEET_REGION_COUNT_OFF
FLEET_REGION_DATA_OFF = _OW.NAVAL_FLEET_REGION_DATA_OFF
FLEET_TF_ARRAY_OFF = _OW.NAVAL_FLEET_TF_ARRAY_OFF
TF_HANDLE_OFF = _OW.NAVAL_TF_HANDLE_OFF
TF_SHIP_COUNT_OFF = _OW.NAVAL_TF_SHIP_COUNT_OFF
TF_MISSION_TYPE_OFF = _OW.NAVAL_TF_MISSION_TYPE_OFF
TF_MISSION_OBJ_OFF = _OW.NAVAL_TF_MISSION_OBJ_OFF
# 2026-11-22 (复测 5 §9.3 残留 A): 反汇编 CNavalMissionSetTypeCommand::Execute
# (0x13E6E09..0x13E6E89 -> 0x0DDE3C0) 证实：当 tf+0x4B8 (EActivity) 非 0 且非 4
# （例如舰队正在返港/修理/集结）时，引擎不会立刻覆盖 +0x374，而是把新任务写入
# tf+0x4F0 (queued_mission)，等当前活动结束后自动转入执行。
TF_ACTIVITY_OFF = _OW.NAVAL_TF_ACTIVITY_OFF
TF_QUEUED_MISSION_OFF = _OW.NAVAL_TF_QUEUED_MISSION_OFF

REGION_TABLE_SLOT_RVA = _OW.G_GAMESTATE
REGION_ARRAY_OFF = _OW.NAVAL_REGION_ARRAY_OFF
REGION_COUNT_OFF = _OW.NAVAL_REGION_COUNT_OFF
REGION_VALID_OFF = _OW.NAVAL_REGION_VALID_OFF          # > 0 means this is a usable naval region
FN_RESOLVE_HANDLE = _OW.CMD_FN_RESOLVE_HANDLE

# 第二期数据驱动覆盖：优先从 calling/profile.py 读取海军虚表、命令与句柄解析函数
try:
    import profile as _prof
    if _prof.load():
        VT_SET_TYPE = _prof.command_field("CNavalMissionSetTypeCommand", "vt", VT_SET_TYPE)
        SIZE_SET_TYPE = max(SIZE_SET_TYPE, int(_prof.command_field("CNavalMissionSetTypeCommand", "size", SIZE_SET_TYPE)))
        CANEXEC_SET_TYPE = _prof.command_field("CNavalMissionSetTypeCommand", "can_execute", CANEXEC_SET_TYPE)
        EXEC_SET_TYPE = _prof.command_field("CNavalMissionSetTypeCommand", "execute", EXEC_SET_TYPE)
        VT_ADD_REGION = _prof.command_field("CNavalMissionAddRegionCommand", "vt", VT_ADD_REGION)
        SIZE_ADD_REGION = _prof.command_field("CNavalMissionAddRegionCommand", "size", SIZE_ADD_REGION)
        CANEXEC_ADD_REGION = _prof.command_field("CNavalMissionAddRegionCommand", "can_execute", CANEXEC_ADD_REGION)
        EXEC_ADD_REGION = _prof.command_field("CNavalMissionAddRegionCommand", "execute", EXEC_ADD_REGION)
        VT_REMOVE_REGION = _prof.command_field("CNavalMissionRemoveRegionCommand", "vt", VT_REMOVE_REGION)
        SIZE_REMOVE_REGION = _prof.command_field("CNavalMissionRemoveRegionCommand", "size", SIZE_REMOVE_REGION)
        CANEXEC_REMOVE_REGION = _prof.command_field("CNavalMissionRemoveRegionCommand", "can_execute", CANEXEC_REMOVE_REGION)
        EXEC_REMOVE_REGION = _prof.command_field("CNavalMissionRemoveRegionCommand", "execute", EXEC_REMOVE_REGION)
        FLEET_VT = _prof.anchor("VT_FLEET", FLEET_VT)
        TASKFORCE_VT = _prof.anchor("VT_TASKFORCE", TASKFORCE_VT)
        REGION_TABLE_SLOT_RVA = _prof.anchor("G_GAMESTATE", REGION_TABLE_SLOT_RVA)
        FN_RESOLVE_HANDLE = _prof.anchor("FN_RESOLVE_HANDLE", FN_RESOLVE_HANDLE)
except Exception:
    pass

# 2026-11-22 (复测 5 P1 #3.1): 反汇编 CNavalMissionAddRegionCommand::Execute
# -> 0x0DBE060 证实：tf+0x374 == 8 是 HOI4 的“预备舰队 (Reserve Fleet)”哨兵值，
# 并非扫雷。对 mission==8 的预备舰队调用 AddRegion/SetType 会触发 0x0DBA810
# 把预备舰队转正为新舰队并把 mission 重置为 0，导致每次 housekeep 都凭空多出新舰队。
MISSION_RESERVE = 8

MISSION_NAMES = {
    0: "none", 1: "patrol", 2: "strike_force", 3: "convoy_raiding",
    4: "convoy_escort", 5: "naval_invasion_support_1",
    6: "naval_invasion_support_2", 7: "minelaying", 8: "reserve",
    9: "naval_invasion_support",
}


def _u64(p, a):
    return struct.unpack("<Q", p.read(a, 8))[0]


def _vptr_rva(p, obj):
    try:
        v = _u64(p, obj)
        return (v - p.base) if v > p.base else None
    except Exception:
        return None


def _array(p, obj, off):
    """CPdxArray at obj+off -> (data, cap, size, items)."""
    data, cap, size = struct.unpack("<Qii", p.read(obj + off, 16))
    if not (data > 0x10000 and 0 <= size <= 4096 and cap >= size):
        return 0, 0, 0, []
    items = [_u64(p, data + 8*k) for k in range(size)]
    return data, cap, size, items


def fleet_task_forces(g, fleet_ptr):
    """CTaskForce rows for one CFleet (PE offsets)."""
    p = g.p
    fp = int(fleet_ptr)
    if _vptr_rva(p, fp) != FLEET_VT:
        raise ValueError("%#x is not a CFleet (vptr %s)" % (fp, _vptr_rva(p, fp)))
    _d, _c, _s, items = _array(p, fp, FLEET_TF_ARRAY_OFF)
    out = []
    for i, tf in enumerate(items):
        if not tf or _vptr_rva(p, tf) != TASKFORCE_VT:
            continue
        h = _u64(p, tf + TF_HANDLE_OFF)
        t, ident = struct.unpack("<ii", p.read(tf + TF_HANDLE_OFF, 8))
        mission = p.i32(tf + TF_MISSION_TYPE_OFF)
        try:
            activity = p.i32(tf + TF_ACTIVITY_OFF)
            queued_mission = p.i32(tf + TF_QUEUED_MISSION_OFF)
        except Exception:
            activity, queued_mission = 0, 0
        eff_mission = mission if mission else (queued_mission if activity not in (0, 4) else 0)
        out.append({
            "index": i,
            "ptr": hex(tf),
            "handle": [t, ident],
            "packed_handle": hex(h),
            "ships": p.i32(tf + TF_SHIP_COUNT_OFF),
            "mission": mission,
            "mission_name": MISSION_NAMES.get(mission),
            "activity": activity,
            "queued_mission": queued_mission,
            "queued_mission_name": MISSION_NAMES.get(queued_mission) if queued_mission else None,
            "effective_mission": eff_mission,
            "is_reserve": (mission == MISSION_RESERVE),
            "regions": p.i32(fp + FLEET_REGION_COUNT_OFF),
        })
    return out


def fleet_name(p, fp):
    """CFleet's MSVC std::string at +0xE0 (16-byte SSO / size +0x10 / cap +0x18)."""
    try:
        n = p.u64(fp + FLEET_NAME_OFF + 0x10)
        if not (0 < n < 128):
            return None
        q = p.u64(fp + FLEET_NAME_OFF)
        src = (fp + FLEET_NAME_OFF) if n <= 15 else q
        b = p.read(src, n)
        s = b.decode("utf-8", "replace")
        # CFleet+0xE0 is only a name on SOME fleets; on others it is arbitrary
        # binary data. Reject control chars, replacement chars and unassigned
        # code points, but keep CJK / accented letters (fleet names are localised).
        if any(c in "\ufffd" or (not c.isprintable() and not c.isspace()) for c in s):
            return None
        return s
    except Exception:
        return None


def naval_fleets(g, tag=None):
    """All player fleets with their task forces, handles and mission state."""
    p = g.p
    out = []
    for f in g.fleets(tag):
        fp = int(f["ptr"], 16)
        t, ident = struct.unpack("<ii", p.read(fp + FLEET_HANDLE_OFF, 8))
        tfs = fleet_task_forces(g, fp)
        # 与引擎 0x0DBE060 逐指令对齐：tf_count > 0 && tf[0]->mission == 8 即为预备舰队
        is_reserve = bool(tfs and int(tfs[0].get("mission") or 0) == MISSION_RESERVE)
        out.append({
            "fleet_index": f["index"],
            "fleet_name": f["name"] or fleet_name(p, fp),
            "fleet_ptr": f["ptr"],
            "handle": [t, ident],
            "packed_handle": hex(_u64(p, fp + FLEET_HANDLE_OFF)),
            "regions": p.i32(fp + FLEET_REGION_COUNT_OFF),
            "is_reserve": is_reserve,
            "task_forces": tfs,
        })
    return out


def region_table(g):
    """(count, array_ptr, global_ptr) of the strategic naval region table."""
    p = g.p
    gp = _u64(p, p.base + REGION_TABLE_SLOT_RVA)
    if not gp or gp < 0x10000:
        raise RuntimeError("strategic naval region table is not initialised")
    return p.i32(gp + REGION_COUNT_OFF), _u64(p, gp + REGION_ARRAY_OFF), gp


def valid_regions(g):
    """Region ids with [region+0xA0] > 0 (the CanExecute gate)."""
    p = g.p
    cnt, arr, _gp = region_table(g)
    out = []
    for j in range(1, cnt):
        r = _u64(p, arr + 8*j)
        if not r or r < 0x10000:
            continue
        try:
            if p.i64(r + REGION_VALID_OFF) > 0:
                out.append(j)
        except Exception:
            continue
    return out


def region_is_valid(g, region_id):
    """Crash-#5 guard: prove the id is in range AND its object is usable."""
    p = g.p
    cnt, arr, _gp = region_table(g)
    rid = int(region_id)
    if rid < 1 or rid >= cnt:
        return False, "region id %d out of range 1..%d" % (rid, cnt - 1)
    robj = _u64(p, arr + 8*rid)
    if not robj or robj < 0x10000:
        return False, "region id %d has no object" % rid
    v = p.i64(robj + REGION_VALID_OFF)
    if v <= 0:
        return False, "region id %d is not a naval region ([+0xA0]=%d)" % (rid, v)
    return True, robj


def make_set_type(g, tf_handles, mission):
    """Build CNavalMissionSetTypeCommand (no ctor call).

    tf_handles must be PACKED CTaskForce handles read from tf+0x18. Passing any
    other handle type lets CanExecute dereference a wrong object (crash #4/#5).
    """
    p = g.p
    mission = int(mission)
    if not 0 <= mission <= 9:
        raise ValueError("mission must be 0..9")
    handles = [int(h) & 0xFFFFFFFFFFFFFFFF for h in tf_handles]
    if not handles:
        raise ValueError("need at least one task-force handle")
    buf = cmdwin.alloc(g, 8*len(handles) + 16)
    p.write(buf, b"".join(struct.pack("<Q", h) for h in handles))
    fields = [(0x28, "<Q", buf), (0x30, "<i", len(handles)),
              (0x34, "<i", len(handles)),
              (0x38, "<Q", p.base + 0x30FD000),   # CPdxNewDeleteAllocator
              (0x40, "<i", mission), (0x44, "<B", 0), (0x45, "<B", 1)]
    return cmdwin.build_by_vt(g, VT_SET_TYPE, SIZE_SET_TYPE, fields)


def make_add_region(g, fleet_handle, region_id):
    """Build CNavalMissionAddRegionCommand (no ctor call)."""
    return cmdwin.build_by_vt(g, VT_ADD_REGION, SIZE_ADD_REGION, [
        (0x28, "<Q", int(fleet_handle) & 0xFFFFFFFFFFFFFFFF),
        (0x30, "<i", int(region_id)),
    ])


def make_remove_regions(g, fleet_handle, region_ids):
    """Build CNavalMissionRemoveRegionCommand (no ctor call).

    Unlike AddRegion this command takes an ARRAY of i32 region ids in an
    embedded CPdxArray at +0x30 (data +0x30, cap +0x38, size +0x3c,
    IAllocator* +0x40). CanExecute 0x13EE560 steps that array 4 bytes at a
    time, resolves each id through the region table and requires the region
    to already be assigned to the fleet. Verified live 2026-11-16: removing
    region 9 from fleet 0 took fleet+0x54 from 2 to 1 and dropped the
    region-9 object from the +0x48 array.
    """
    p = g.p
    ids = [int(r) for r in region_ids]
    if not ids:
        raise ValueError("need at least one region id")
    buf = cmdwin.alloc(g, 4*len(ids) + 16)
    p.write(buf, b"".join(struct.pack("<i", r) for r in ids))
    return cmdwin.build_by_vt(g, VT_REMOVE_REGION, SIZE_REMOVE_REGION, [
        (0x28, "<Q", int(fleet_handle) & 0xFFFFFFFFFFFFFFFF),
        (0x30, "<Q", buf),
        (0x38, "<i", len(ids)),
        (0x3c, "<i", len(ids)),
        (0x40, "<Q", p.base + 0x30FD000),
    ])


def can_exec(g, cmd, rva):
    """Ask the game's own CanExecute; NULL error sink, exactly as Post does."""
    try:
        return bool(g.call(g.p.base + rva, (cmd, 0),
                           absolute=True, force=True) & 0xFF)
    except Exception:
        return None


def post(g, cmd, seconds=2.0):
    """enqueue + briefly run the clock, restoring the previous pause state."""
    return cmdwin.post_drained(g, cmd, seconds=seconds)


def resolve_handle(g, packed):
    """0x2284870(handle*) -> object* (the same resolver CanExecute uses)."""
    import cmdwin as CW
    p = g.p
    b = CW.alloc(g, 0x10)
    try:
        p.write(b, struct.pack("<Q", int(packed) & 0xFFFFFFFFFFFFFFFF))
        return g.call(p.base + FN_RESOLVE_HANDLE, (b,), absolute=True, force=True)
    finally:
        try:
            CW.free(g, b)
        except Exception:
            pass


def set_mission(g, tf_handle, mission, tag=None):
    """Set one task force's mission. Returns a full diagnostic dict."""
    p = g.p
    obj = resolve_handle(g, tf_handle)
    if not obj or obj < 0x10000:
        return {"ok": False, "error": "task-force handle does not resolve",
                "handle": hex(int(tf_handle))}
    cmd = make_set_type(g, [tf_handle], mission)
    can = can_exec(g, cmd, CANEXEC_SET_TYPE)
    res = {
        "ok": False, "mode": "pe-player-command", "cmd": hex(cmd),
        "handle": hex(int(tf_handle)), "mission": int(mission),
        "mission_name": MISSION_NAMES.get(int(mission)),
        "can_execute": can, "resolved": hex(obj),
    }
    if not can:
        res["error"] = "CNavalMissionSetTypeCommand::CanExecute refused"
        return res
    posted = post(g, cmd, seconds=2.5)
    res["posted"] = posted
    # read back: the TF itself is obj-0x10 (see the handle resolver)
    tf = obj - 0x10
    try:
        res["mission_after"] = p.i32(tf + TF_MISSION_TYPE_OFF)
        res["activity_after"] = p.i32(tf + TF_ACTIVITY_OFF)
        res["queued_mission_after"] = p.i32(tf + TF_QUEUED_MISSION_OFF)
        res["mission_vptr_rva"] = _vptr_rva(p, tf + TF_MISSION_OBJ_OFF) and \
            hex(_vptr_rva(p, tf + TF_MISSION_OBJ_OFF))
    except Exception:
        pass
    want_m = int(mission)
    direct_ok = (res.get("mission_after") == want_m)
    queued_ok = (
        want_m != 0
        and res.get("queued_mission_after") == want_m
        and res.get("activity_after") not in (None, 0, 4)
    )
    res["queued"] = bool(queued_ok and not direct_ok)
    res["effective_mission_after"] = (
        res.get("mission_after") if res.get("mission_after")
        else (res.get("queued_mission_after") if queued_ok else 0)
    )
    res["ok"] = bool(posted.get("posted")) and (direct_ok or queued_ok)
    if res["queued"]:
        res["note"] = (
            "task force is mid-activity (activity=%d); engine queued mission=%d (%s) at "
            "tf+0x4F0 and will engage automatically when the current activity completes"
            % (res.get("activity_after"), want_m, MISSION_NAMES.get(want_m))
        )
    elif not res["ok"]:
        if not posted.get("posted"):
            res["error"] = "CNavalMissionSetTypeCommand failed to post to session queue"
        else:
            res["error"] = (
                "command posted but neither tf+0x374 (mission=%s) nor tf+0x4F0 "
                "(queued_mission=%s, activity=%s) matched requested mission=%d"
                % (res.get("mission_after"), res.get("queued_mission_after"),
                   res.get("activity_after"), want_m)
            )
    return res


def add_region(g, fleet_handle, region_id, tag=None):
    """Add one strategic naval region to a fleet, with the crash-#5 guard."""
    p = g.p
    ok, why = region_is_valid(g, region_id)
    if not ok:
        return {"ok": False, "error": why, "region": int(region_id),
                "note": "refused before CanExecute (null deref guard)"}
    cmd = make_add_region(g, fleet_handle, region_id)
    can = can_exec(g, cmd, CANEXEC_ADD_REGION)
    res = {
        "ok": False, "mode": "pe-player-command", "cmd": hex(cmd),
        "fleet_handle": hex(int(fleet_handle)), "region": int(region_id),
        "can_execute": can, "region_obj": hex(why),
    }
    if not can:
        res["error"] = "CNavalMissionAddRegionCommand::CanExecute refused"
        return res
    posted = post(g, cmd, seconds=2.5)
    res["posted"] = posted
    try:
        obj = resolve_handle(g, fleet_handle)
        if obj and obj >= 0x10000:
            res["regions_after"] = p.i32(obj + FLEET_REGION_COUNT_OFF)
    except Exception:
        pass
    res["ok"] = bool(posted.get("posted"))
    return res


def clear_fleet_missions(g, fleet_ptr):
    """将指定舰队下所有非预备且挂着任务的特遣舰队批量清为 mission=0 (none)。

    2026-11-23 (复测 9 D-2): 反汇编证实 CNavalMissionSetTypeCommand::CanExecute (0x13EE8C0)
    与 Execute (0x13E6C40) 原生按数组循环处理 +0x28 中的全部 TF 句柄，并支持 mission=0 清任务，
    因此仅需 1 次命令即可清完整个舰队的全部非预备特遣舰队。
    """
    p = g.p
    fp = int(fleet_ptr)
    try:
        tfs = fleet_task_forces(g, fp)
    except Exception as e:
        return {"ok": False, "attempted": 0, "cleared": 0, "error": str(e)}
    targets = [
        tf for tf in tfs
        if not tf.get("is_reserve")
        and int(tf.get("mission") or 0) != MISSION_RESERVE
        and int(tf.get("effective_mission") or tf.get("mission") or 0) != 0
    ]
    if not targets:
        return {"ok": True, "attempted": 0, "cleared": 0, "task_forces": []}
    handles = [int(tf["packed_handle"], 16) for tf in targets]
    cmd = make_set_type(g, handles, 0)
    can = can_exec(g, cmd, CANEXEC_SET_TYPE)
    if not can:
        return {
            "ok": False,
            "attempted": len(handles),
            "cleared": 0,
            "can_execute": can,
            "error": "CNavalMissionSetTypeCommand(mission=0)::CanExecute refused",
        }
    posted = post(g, cmd, seconds=2.5)
    after_rows = []
    cleared = 0
    for tf in targets:
        tf_ptr = int(tf["ptr"], 16)
        try:
            m_after = int(p.i32(tf_ptr + TF_MISSION_TYPE_OFF))
        except Exception:
            m_after = None
        if m_after == 0:
            cleared += 1
        after_rows.append({
            "ptr": tf["ptr"],
            "handle": tf["handle"],
            "mission_before": tf.get("mission"),
            "mission_after": m_after,
        })
    return {
        "ok": bool(posted.get("posted")) and cleared == len(targets),
        "attempted": len(targets),
        "cleared": cleared,
        "cmd": hex(cmd),
        "posted": posted,
        "task_forces": after_rows,
    }


def remove_regions(g, fleet_handle, region_ids, tag=None, clear_mission_when_empty=True):
    """Remove strategic naval regions from a fleet, with the crash-#5 guard."""
    p = g.p
    checked, refused = [], []
    for rid in region_ids:
        ok, why = region_is_valid(g, rid)
        if not ok:
            refused.append({"region": int(rid), "error": why})
        else:
            checked.append(int(rid))
    if not checked:
        return {"ok": False, "error": "no valid region ids",
                "refused": refused}
    cmd = make_remove_regions(g, fleet_handle, checked)
    can = can_exec(g, cmd, CANEXEC_REMOVE_REGION)
    res = {
        "ok": False, "mode": "pe-player-command", "cmd": hex(cmd),
        "fleet_handle": hex(int(fleet_handle)), "regions": checked,
        "refused": refused, "can_execute": can,
    }
    if not can:
        res["error"] = ("CNavalMissionRemoveRegionCommand::CanExecute refused "
                        "(a region must already be assigned to the fleet)")
        return res
    posted = post(g, cmd, seconds=2.5)
    res["posted"] = posted
    obj = 0
    try:
        obj = resolve_handle(g, fleet_handle)
        if obj and obj >= 0x10000:
            res["regions_after"] = p.i32(obj + FLEET_REGION_COUNT_OFF)
    except Exception:
        pass
    res["ok"] = bool(posted.get("posted"))
    if res["ok"] and clear_mission_when_empty and obj and obj >= 0x10000 and res.get("regions_after") == 0:
        mc = clear_fleet_missions(g, obj)
        res["mission_cleared"] = bool(mc.get("ok") and mc.get("cleared", 0) > 0)
        res["mission_clear_detail"] = mc
    return res


def mission_state(g, tag=None):
    """Read-only snapshot: every fleet, its TF missions and assigned regions."""
    rows = naval_fleets(g, tag)
    return {"ok": True, "fleets": rows,
            "valid_regions_count": len(valid_regions(g))}
