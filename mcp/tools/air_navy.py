"""海军舰队/特遣队/海区任务、空军联队/任务与集团军空军附着工具。"""
from __future__ import annotations

import json
import os
import sys

from .common import TAG, _BOOL, _INT, _STR, game, tool
import session_state as _SS
from .common import _ptr

_AIR_DONE_PATH = _SS.state_path(_SS.AIR_ATTACH_NAME)

def _air_session_fp(g):
    """获取极轻量对局指纹 (pid, gs_ptr)，防止同 PID 读档/重开新局沿用旧 air_attach.json。"""
    pid = getattr(getattr(g, "p", None), "pid", None)
    gs_ptr = None
    if sys.platform == "win32":
        try:
            import offsets_win as _ow
            gs_rva = getattr(_ow, "G_GAMESTATE", None)
            if gs_rva:
                gs_ptr = int(g.p.u64(int(g.p.base) + int(gs_rva)))
        except Exception:
            gs_ptr = None
    return pid, gs_ptr


def _air_done_load(pid=None, gs_ptr=None):
    """(wing, army) handle pairs this tool already attached (scoped to pid + game session)."""
    try:
        with open(_AIR_DONE_PATH, encoding="utf-8") as f:
            d = json.load(f)
        if pid is not None and d.get("pid") is not None and int(d["pid"]) != int(pid):
            return set()
        if gs_ptr is not None and d.get("gs_ptr") is not None and int(d["gs_ptr"]) != int(gs_ptr):
            return set()
        return set(d.get("pairs") or [])
    except Exception:
        return set()


def _air_done_save(pairs, pid=None, gs_ptr=None):
    _SS.ensure_parent(_AIR_DONE_PATH)
    tmp = "%s.%d.tmp" % (_AIR_DONE_PATH, os.getpid())
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"pid": pid, "gs_ptr": gs_ptr, "pairs": sorted(pairs)[-500:]}, f)
        os.replace(tmp, _AIR_DONE_PATH)
    except Exception:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass


def _live_army_wing_map(g, armies):
    """2026-11-22 (P3-4): 纯只读遍历每个陆军 COrdersGroup+0x70 (CPdxArray<CAirWing*>)，
    直接从游戏内存建立 {wing_ptr -> army_info} 权威映射，不再依赖易过期的本地缓存。
    """
    from game import _arr
    out = {}
    for og in armies:
        try:
            optr = int(og["ptr"], 16) if isinstance(og.get("ptr"), str) else int(og["ptr"])
            for wp in _arr(g.p, optr, 0x70):
                if wp:
                    out[int(wp)] = {"ptr": optr, "name": og.get("name"), "handle": og.get("handle")}
        except Exception:
            continue
    return out


@tool("hoi4_fleets", "Naval fleets.", TAG)
def _fleets(a):
    return game().fleets(a.get("tag"))


@tool("hoi4_air_wings",
      "The player's air wings with live state: planes in the wing, mission mask "
      "and whether that mission is enabled. Ownership is exact on the PE build: "
      "CAirWing+0x38 -> CAirWingPool+0x40 -> CStrategicAir+0x90 = CCountryTag id, "
      "so this returns only the player's own wings (GER 15 / ENG 36 in 1936) and "
      "includes wings created after game start. Pass all=true for a capped world "
      "census.",
      dict(TAG, all={"type": "boolean"}, limit={"type": "integer"}))
def _wings(a):
    return game().air_wings(a.get("tag"), show_all=bool(a.get("all")),
                            limit=int(a.get("limit") or 0))


@tool("hoi4_attach_air_wings",
      "Attach the player's air wings to the player's armies "
      "(CAttachAirWingToArmyCommand).  Ownership is now exact: "
      "CAirWing+0x38 -> CAirWingPool+0x40 -> CStrategicAir+0x90 = CCountryTag "
      "id, so only wings the player really owns are touched (measured 44 groups "
      "/ 44 distinct tags).  `allow_unverified` is accepted for compatibility "
      "and no longer changes anything.",
      dict(TAG, refresh={"type": "boolean"}, allow_unverified={"type": "boolean"}))
def _attach_air(a):
    # 2026-11-19: the 2026-11-16 "refuses by default" gate is gone. It was a
    # workaround for not knowing the wing -> country link; CStrategicAir+0x90
    # provides it, so both paths now agree and the verified PE path always runs.
    return _air_ger_attach(a)


@tool("hoi4_naval_task_forces",
      "List all naval fleets and their task forces (CTaskForce ptr, handle, ship count).",
      TAG)
def _naval_tfs(a):
    import sys
    g = game()
    if sys.platform == "win32":
        # 2026-11-16: legacy Linux offsets (fleet +0xB0, TF handle at +0x18 as
        # pointer) do not apply here. On the PE build the fleet task-force array
        # is at +0xB8 and the TF handle at +0x18 resolves through 0x2284870.
        import navalops_win as NW
        try:
            fleets = NW.naval_fleets(g, a.get("tag"))
        except Exception as e:
            return {"ok": False, "error": repr(e),
                    "note": "PE naval scan failed"}
        tfs = [tf for f in fleets for tf in f["task_forces"]]
        return {
            "ok": True, "count": len(fleets), "fleets": fleets,
            "task_force_count": len(tfs),
            "valid_regions_count": len(NW.valid_regions(g)),
            "note": "PE 1.19.5.0: handle=packed {type,id}, mission=tf+0x374, "
                    "regions=fleet+0x54",
        }
    return {"ok": False, "error": "Linux navalops path removed; Windows PE only"}


@tool("hoi4_naval_regions",
      "List the game's strategic naval regions (id, display name, naval terrain, province "
      "count) and whether each one is USABLE right now. hoi4_naval_mission needs numeric "
      "region ids and an out-of-range id is a null dereference inside the engine, so this "
      "is the safe way to pick one. Read-only: the ids come from map/strategicregions/*.txt "
      "and the names from the game's own localisation; 'usable_now' comes from the live "
      "CStrategicNavalRegion table. Filter with name= (substring, case-insensitive) and/or "
      "usable_only=true.",
      {"name": _STR, "usable_only": _BOOL, "limit": _INT})
def _naval_regions(a):
    import navalregions as NR
    g = game()
    valid = None
    live_error = None
    try:
        import navalops_win as NW
        valid = NW.valid_regions(g)
    except Exception as e:
        live_error = "%s: %s" % (type(e).__name__, e)
    rows = NR.catalogue(valid)
    needle = (a.get("name") or "").strip().lower()
    if needle:
        rows = [r for r in rows
                if needle in (r.get("name") or "").lower()
                or needle in (r.get("name_key") or "").lower()]
    if a.get("usable_only"):
        rows = [r for r in rows if r.get("usable_now")]
    lim = int(a.get("limit") or 300)
    out = {"ok": True, "count": len(rows), "regions": rows[:lim],
           "usable_count": (None if valid is None else len(valid)),
           "note": "pass the id from here to hoi4_naval_mission(regions=[...]) "
                   "or hoi4_naval_mission(region_names=['North Sea'])"}
    if live_error:
        out["live_table_error"] = live_error
        out["note"] = ("live region table unreadable (%s) -- ids are still correct, "
                       "but 'usable_now' is null and hoi4_naval_mission will refuse "
                       "until the game is reachable" % live_error)
    return out


@tool("hoi4_naval_mission",
      "Assign a naval mission to a task force and add strategic naval region(s) to its fleet. "
      "mission: 0=None (clear/stop mission), 1=Patrol, 2=Strike Force, 3=Convoy Raiding, "
      "4=Convoy Escort, 9=Naval Invasion Support. On PE, tf_ptr may be a raw CTaskForce pointer, "
      "a packed handle, or the numeric handle id; fleet_ptr likewise. Region ids "
      "are validated against the game's own naval-region table before CanExecute "
      "(an out-of-range id crashes the engine). Call hoi4_naval_regions first to see "
      "the ids and names, or pass region_names=['North Sea'] and let it resolve them. "
      "Use hoi4_naval_remove_regions to unassign regions. dry_run=true builds and "
      "validates but does not post.",
      {"fleet_ptr": _STR, "tf_ptr": _STR, "mission": _INT,
       "regions": {"type": "array", "items": _INT},
       "region_names": {"type": "array", "items": _STR,
                        "description": "Region display names or name keys, resolved "
                                       "through the game's own files (e.g. 'North Sea')"},
       "dry_run": _BOOL},
      ["fleet_ptr", "tf_ptr", "mission"])
def _naval_mission(a):
    import sys
    g = game()

    # Resolve any names to ids up front: a wrong id is a NULL DEREF in the
    # engine's CanExecute (crash #5 on 2026-11-16), so this is the only place a
    # name is allowed to become a number. Shared by the Windows and Linux paths.
    import navalregions as NR
    req_regions = []
    for r in (a.get("regions") or []):
        try:
            req_regions.append(int(r))
        except Exception:
            return {"ok": False, "error": "bad region id: %r" % (r,)}
    for nm in (a.get("region_names") or []):
        rid = NR.lookup(nm)
        if rid is None:
            return {"ok": False,
                    "error": "unknown naval region %r -- call hoi4_naval_regions "
                             "to list the valid names" % (nm,)}
        if rid not in req_regions:
            req_regions.append(rid)

    if sys.platform == "win32":
        import navalops_win as NW
        p = g.p
        dry = bool(a.get("dry_run", False))

        def as_handle(s, kind):
            """raw pointer / packed handle / numeric id -> packed handle."""
            s = str(s)
            if s.startswith("0x") and len(s) > 10:
                ptr = _ptr(s)
                off = NW.FLEET_HANDLE_OFF if kind == "fleet" else NW.TF_HANDLE_OFF
                return p.u64(ptr + off), "raw_ptr"
            v = int(s, 0) if s.startswith("0x") else int(s)
            if v > 0xFFFFFFFF:
                return v, "packed"
            typ = 0x3D if kind == "fleet" else 0x33
            return (v << 32) | typ, "id"

        try:
            tfh, tf_kind = as_handle(a["tf_ptr"], "tf")
            fh, f_kind = as_handle(a["fleet_ptr"], "fleet")
        except Exception as e:
            return {"ok": False, "error": "bad fleet_ptr/tf_ptr: %s" % e}

        # resolve BOTH handles before any CanExecute (crash #4/#5 rule)
        tf_obj = NW.resolve_handle(g, tfh)
        f_obj = NW.resolve_handle(g, fh)
        if not tf_obj or tf_obj < 0x10000 or not f_obj or f_obj < 0x10000:
            return {"ok": False, "error": "fleet/tf handle does not resolve",
                    "tf_handle": hex(tfh), "fleet_handle": hex(fh),
                    "tf_obj": hex(tf_obj or 0), "fleet_obj": hex(f_obj or 0)}

        mission = int(a["mission"])
        cmd = NW.make_set_type(g, [tfh], mission)
        can = NW.can_exec(g, cmd, NW.CANEXEC_SET_TYPE)
        res = {
            "ok": False, "mode": "pe-player-command",
            "tf_handle": hex(tfh), "fleet_handle": hex(fh),
            "tf_input": tf_kind, "fleet_input": f_kind,
            "mission": mission, "mission_name": NW.MISSION_NAMES.get(mission),
            "can_execute": can, "dry_run": dry,
            "set_cmd": hex(cmd),
        }
        # Report region validity even in dry-run (crash #5 guard is the whole
        # point of pre-validating them).
        reg_preview = []
        for reg in req_regions:
            good, why = NW.region_is_valid(g, reg)
            reg_preview.append({"region": reg, "valid": good,
                                "detail": hex(why) if good else why})
        res["region_preview"] = reg_preview
        if not can:
            res["error"] = "CNavalMissionSetTypeCommand::CanExecute refused"
            return res
        if dry:
            res["ok"] = True
            res["note"] = "validated only; no command posted"
            return res

        # post THIS command (not a freshly built one) so nothing is duplicated
        r_post = NW.post(g, cmd, seconds=2.5)
        tf_ptr = tf_obj - 0x10          # resolver returns tf+0x10 on PE
        try:
            mission_after = p.i32(tf_ptr + NW.TF_MISSION_TYPE_OFF)
        except Exception:
            mission_after = None
        r_m = {"ok": bool(r_post.get("posted")) and mission_after == mission,
               "posted": r_post, "mission_after": mission_after,
               "mission": mission, "cmd": hex(cmd)}
        res["set_mission"] = r_m
        r_regs = []
        for reg in req_regions:
            ok, why = NW.region_is_valid(g, reg)
            if not ok:
                r_regs.append({"ok": False, "region": reg, "error": why,
                               "note": "refused before CanExecute (crash #5 guard)"})
                continue
            c = NW.make_add_region(g, fh, reg)
            cr = NW.can_exec(g, c, NW.CANEXEC_ADD_REGION)
            if dry or not cr:
                r_regs.append({"ok": bool(cr) and dry, "region": reg,
                               "can_execute": cr, "cmd": hex(c),
                               "note": "not posted" if dry else "CanExecute refused"})
                continue
            _posted = NW.post(g, c, seconds=2.5)
            r_regs.append({"ok": bool(_posted.get("posted")), "region": reg,
                           "can_execute": cr, "cmd": hex(c), "posted": _posted})
        res["regions"] = r_regs
        # 2026-11-19 (measured): a strategic naval REGION belongs to the FLEET
        # (`fleet+0x54`), not to the task force.  Asking for the same region on
        # a second TF of the same fleet is refused by the engine — that is
        # idempotency, not a failure of the call.  The old code folded it into
        # `ok:false` while ALSO leaving `refused_regions` empty, so the two
        # fields contradicted each other and the caller retried a no-op.
        guarded = [x for x in r_regs if "guard" in (x.get("note") or "")]
        refused = [x["region"] for x in r_regs
                   if not x.get("ok") and x not in guarded]
        res["ok"] = bool(r_m.get("ok"))
        res["refused_regions"] = [x["region"] for x in guarded] + refused
        if refused:
            res["note"] = ("region(s) %s refused — most likely already assigned "
                           "to this fleet (regions are per FLEET, not per task "
                           "force); the mission itself is set" % refused)
        return res
    return {"ok": False, "error": "Linux navalops path removed; Windows PE only"}


@tool("hoi4_naval_remove_regions",
      "Unassign strategic naval region(s) from a fleet through "
      "CNavalMissionRemoveRegionCommand. fleet_ptr may be a raw CFleet pointer, "
      "a packed handle or the numeric handle id. Region ids are validated "
      "before CanExecute; a region that is not currently assigned is refused "
      "by the engine. When regions_after reaches 0 and clear_mission_when_empty=true "
      "(default), all non-reserve task forces in that fleet also have their mission "
      "reset to 0 (none) so FUEL_ZERO / idle checks stay consistent. dry_run=true only validates.",
      {"fleet_ptr": _STR, "regions": {"type": "array", "items": _INT},
       "clear_mission_when_empty": _BOOL, "dry_run": _BOOL},
      ["fleet_ptr", "regions"])
def _naval_remove_regions(a):
    import sys
    if sys.platform != "win32":
        return {"ok": False, "error": "only implemented on the PE 1.19.5.0 backend"}
    import navalops_win as NW
    g = game()
    p = g.p
    if "fleet_ptr" not in a or a["fleet_ptr"] is None:
        return {"ok": False, "error": "missing required parameter: fleet_ptr"}
    try:
        s = str(a["fleet_ptr"])
        if s.startswith("0x") and len(s) > 10:
            fh = p.u64(_ptr(s) + NW.FLEET_HANDLE_OFF)
            kind = "raw_ptr"
        else:
            v = int(s, 0) if s.startswith("0x") else int(s)
            fh = v if v > 0xFFFFFFFF else ((v << 32) | 0x3D)
            kind = "packed" if v > 0xFFFFFFFF else "id"
    except Exception as e:
        return {"ok": False, "error": "invalid fleet_ptr %r: %s" % (a.get("fleet_ptr"), e)}
    obj = NW.resolve_handle(g, fh)
    if not obj or obj < 0x10000:
        return {"ok": False, "error": "fleet handle does not resolve",
                "handle": hex(fh)}
    ids = [int(x) for x in (a.get("regions") or [])]
    checked, refused = [], []
    for rid in ids:
        ok, why = NW.region_is_valid(g, rid)
        (checked if ok else refused).append(
            rid if ok else {"region": rid, "error": why})
    # CanExecute requires the region to be PRESENT in fleet+0x48. Report that
    # explicitly so a 'refused' result is diagnosable without a crash.
    assigned = []
    n = p.i32(obj + NW.FLEET_REGION_COUNT_OFF)
    d = p.u64(obj + NW.FLEET_REGION_DATA_OFF)
    if d > 0x10000 and n > 0:
        assigned = [p.u64(d + 8*k) for k in range(n)]
    cnt, arr, _ = NW.region_table(g)
    missing = [r for r in checked if p.u64(arr + 8*r) not in assigned]
    cmd = NW.make_remove_regions(g, fh, checked) if checked else 0
    can = NW.can_exec(g, cmd, NW.CANEXEC_REMOVE_REGION) if cmd else None
    res = {
        "ok": False, "mode": "pe-player-command",
        "fleet_handle": hex(fh), "fleet_input": kind,
        "fleet_ptr": hex(obj), "regions_requested": ids,
        "regions_valid": checked, "refused": refused,
        "assigned_region_objects": [hex(x) for x in assigned],
        "not_assigned": missing, "can_execute": can,
        "cmd": hex(cmd) if cmd else None,
        "dry_run": bool(a.get("dry_run", False)),
    }
    if not can:
        res["error"] = ("CNavalMissionRemoveRegionCommand::CanExecute refused "
                        "(every id must already be assigned to this fleet)")
        return res
    if a.get("dry_run", False):
        res["ok"] = True
        res["note"] = "validated only; no command posted"
        return res
    posted = NW.post(g, cmd, seconds=2.5)
    res["posted"] = posted
    try:
        res["regions_after"] = p.i32(obj + NW.FLEET_REGION_COUNT_OFF)
    except Exception:
        pass
    res["ok"] = bool(posted.get("posted"))
    clear_m = bool(a.get("clear_mission_when_empty", True))
    if res["ok"] and clear_m and res.get("regions_after") == 0:
        mc = NW.clear_fleet_missions(g, obj)
        res["mission_cleared"] = bool(mc.get("ok") and mc.get("cleared", 0) > 0)
        res["mission_clear_detail"] = mc
    return res


@tool("hoi4_air_ger_attach",
      "Scan the player's own air wings (ownership via CStrategicAir+0x90) and attach "
      "them round-robin to land armies. Kept as an alias of hoi4_attach_air_wings.",
      TAG)
def _air_ger_attach(a):
    import sys
    g = game()
    if sys.platform == "win32":
        # 2026-11-16: PE path uses the MSVC RTTI index + pure-data
        # CAttachAirWingToArmyCommand (verified live: 13/15 starting wings
        # attached, CanExecute flips 1->0 on re-attach).
        # 2026-11-19: ownership is exact now (CStrategicAir+0x90), so newly
        # created wings are attached too instead of only the id-1 block.
        # 2026-11-22 (P3-4): 优先用 COrdersGroup+0x70 实机内存判已挂载状态，
        # 彻底消除读档/重启后首调报假失败；若首趟全部 CanExecute 拒绝则清缓存+refresh 重试一次。
        import airops_win as AW
        _tag = g._c(a.get("tag")).tag
        _cur_pid, _cur_gs = _air_session_fp(g)
        explicit_refresh = a.get("refresh")

        def _attempt(do_refresh, clear_cache=False):
            info = AW.country_wings(g, _tag, refresh=bool(do_refresh))
            if not info.get("ok") or not info.get("wings"):
                return info, 0
            groups = g.order_groups().get("groups") or []
            armies = [og for og in groups if not og.get("is_army_group")]
            if not armies:
                return {"ok": False, "error": "no land armies to attach air wings to"}, 0
            live_map = _live_army_wing_map(g, armies)
            done = set() if clear_cache else _air_done_load(pid=_cur_pid, gs_ptr=_cur_gs)
            _dirty_done = bool(clear_cache)
            out, skipped = [], []
            can_exec_refusals = 0
            # 不能挂载到陆军集团军的空军机种（如海轰/战轰/海上巡逻机/运输机未手动挂载时）
            _UNATTACHABLE = ("nav_bomber", "naval_bomber", "strat_bomber",
                             "strategic_bomber", "maritime_patrol_plane", "transport_plane")
            unattached_idx = 0
            for w in info["wings"]:
                wptr = int(w["ptr"], 16)
                wh = int(w["handle"], 16)
                live_owner = live_map.get(wptr)
                if live_owner is not None:
                    # 内存实证该联队已挂载在我方陆军集团军（COrdersGroup+0x70），
                    # 幂等视为已完成，不再无谓拆挂或触发注入调用。
                    oh = live_owner.get("handle")
                    if wh and oh:
                        done.add("%d:%d" % (wh, (int(oh[1]) << 32) | (int(oh[0]) & 0xFFFFFFFF)))
                        _dirty_done = True
                    out.append({"id": w["id"], "type": w["type"],
                                "army": live_owner.get("name"),
                                "already_attached": True, "verified_from_memory": True})
                    continue
                if w["type"] in _UNATTACHABLE:
                    skipped.append({"id": w["id"], "type": w["type"],
                                    "reason": "wing_type_unsupported"})
                    continue
                og = armies[unattached_idx % len(armies)]
                unattached_idx += 1
                h = og["handle"]
                army_handle = (int(h[1]) << 32) | (int(h[0]) & 0xFFFFFFFF)
                if not wh or not army_handle:
                    skipped.append({"id": w["id"], "type": w["type"],
                                    "army": og["name"], "reason": "stale_handle"})
                    continue
                try:
                    cmd = AW.make_attach(g, [wh], army_handle)
                    if not AW.can_exec(g, cmd, AW.CANEXEC_ATTACH):
                        can_exec_refusals += 1
                        skipped.append({"id": w["id"], "type": w["type"],
                                        "army": og["name"],
                                        "reason": "can_execute_refused"})
                        continue
                    r = AW.post(g, cmd)
                except Exception as _ae:
                    skipped.append({"id": w["id"], "type": w["type"],
                                    "army": og["name"], "reason": "call_failed",
                                    "error": str(_ae)})
                    continue
                done.add("%d:%d" % (wh, army_handle))
                _dirty_done = True
                out.append({"id": w["id"], "type": w["type"], "army": og["name"],
                            "posted": r.get("posted"), "drained": r.get("drained")})
            if _dirty_done:
                _air_done_save(done, pid=_cur_pid, gs_ptr=_cur_gs)
            res = {"ok": len(out) > 0, "attached_count": len(out),
                   "attached": out, "skipped": skipped,
                   "german_group": info["sa"]}
            if not out:
                res["hint"] = ("No wing could be attached (all non-transport wings refused CanExecute "
                               "and were not found in any COrdersGroup+0x70 array).")
            return res, can_exec_refusals

        res, refusals = _attempt(bool(explicit_refresh), clear_cache=False)
        if not res.get("ok") and refusals > 0 and explicit_refresh is None:
            res_retry, _ = _attempt(True, clear_cache=True)
            res_retry["retried_after_refresh"] = True
            return res_retry
        return res
    return {"ok": False, "error": "Linux airops path removed; Windows PE only"}


@tool("hoi4_air_mission",
      "Give the player's air wings a strategic mission. mission_mask: 1=Air "
      "Superiority, 2=CAS, 4=Interception, 8=Strategic Bombing, 16=Naval "
      "Strike, 64=Port Strike, 128=Logistics Strike. wing_ptr accepts one wing "
      "id, a comma list, or a whole type: 'fighters', 'cas', 'tac_bomber', "
      "'nav_bomber', 'all'. On the PE build CStratAirSetMissionCommand writes "
      "both the mission mask and the enabled flag, so no second command is "
      "needed; every wing is read back after the post.",
      {"wing_ptr": _STR, "mission_mask": _INT, "target_strat_air_ptr": _STR,
       "flag1": _BOOL, "flag2": _BOOL},
      ["wing_ptr", "mission_mask"])
def _air_mission(a):
    import sys
    g = game()
    mask = int(a["mission_mask"])
    if sys.platform == "win32":
        # 2026-11-16: PE path. CStratAirSetMissionCommand is verified working
        # (CanExecute=1, wing +0xA0 mission mask AND +0xA4 enabled flag
        # written -> CStratAirEnableMissionCommand is not needed).
        import airops_win as AW
        try:
            sel = AW.select_wings(g, a["wing_ptr"])
        except Exception as e:
            return {"ok": False, "error": str(e)}
        if not sel:
            return {"ok": False, "error": "selector matched no player wing",
                    "selector": a["wing_ptr"]}
        handles = [h for _i, h in sel]
        cmd = AW.make_set_mission(g, handles, mask,
                                  a.get("flag1", True), a.get("flag2", True))
        can = AW.can_exec(g, cmd, AW.CANEXEC_SET_MISSION)
        if not can:
            _names = {1: "air_superiority", 2: "cas", 4: "interception",
                      8: "strategic_bombing", 16: "naval_strike",
                      64: "port_strike", 128: "logistics_strike"}
            return {"ok": False,
                    "error": "CStratAirSetMissionCommand::CanExecute refused",
                    "mission_mask": mask,
                    "mission_name": _names.get(mask, str(mask)),
                    "wings": len(handles),
                    "hint": ("the engine refuses a mission the wing type cannot fly. "
                             "tac_bomber accepts strategic bombing (8) / naval strike "
                             "(16) but NOT air superiority (1); fighter wings accept "
                             "1 / 4; CAS wings accept 2 / 16."),
                    "wing_ids": [i for i, _h in sel], "mission_mask": mask}
        r = AW.post(g, cmd)
        # 2026-11-21 (续测 P1-8): the mask alone never makes a wing fly.  +0xA4 is
        # a PERSISTENT enable flag -- measured: write 1, still 1 after 1.8 s --
        # and nothing in this tool ever set it, so five fighter wings sat at
        # mission=1 / mission_on=false through peace AND through a declared war.
        # airops.enable_mission() cannot be used here (ENABLE_MISSION_CTOR is a
        # Linux ELF offset), so set the flag directly; the engine keeps it.
        try:
            import struct as _st8
            for _w in (AW.german_wings(g).get("wings") or []):
                if (_w.get("mission") or 0) == mask and _w.get("ptr"):
                    g.p.write(int(_w["ptr"], 16) + 0xA4, _st8.pack("<i", 1))
        except Exception:
            pass
        # 2026-11-17: success is judged on the MASK (+0xA0), which is what the
        # player order actually sets. `mission_on` (+0xA4) is the engine's
        # runtime flag: world census at 1936.02 showed 82 wings with a mission
        # and only 2 with +0xA4 == 1 (the two at war), so +0xA4 clearly means
        # "currently generating sorties", not "the player enabled this".
        # Reporting ok:false because 15 peacetime wings are not sortieing was
        # a false negative; it is reported separately now.
        import time as _t
        byid = {}
        for _ in range(6):
            byid = {w["id"]: w for w in AW.german_wings(g)["wings"]}
            if all((byid.get(wid) or {}).get("mission") == mask
                   for wid, _h in sel):
                break
            _t.sleep(0.15)
        rows = []
        for wid, _h in sel:
            w = byid.get(wid) or {}
            st = ({"planes": w.get("planes"), "mission": w.get("mission"),
                   "mission_name": w.get("mission_name"),
                   "mission_on": w.get("mission_on")} if w else {})
            rows.append({"id": wid, "type": w.get("type"), "handle": hex(_h),
                         "readback": st})
        applied = [x for x in rows if (x["readback"] or {}).get("mission") == mask]
        sortieing = [x for x in rows if (x["readback"] or {}).get("mission_on")]
        return {"ok": bool(r.get("posted")) and len(applied) == len(rows),
                "mission_mask": mask, "wings_selected": len(rows),
                "wings_mission_set": len(applied),
                "wings_sortieing_now": len(sortieing),
                "can_execute": can,
                "posted": r.get("posted"), "drained": r.get("drained"),
                "note": ("mission_on (+0xA4) IS the player enable, not a sortie "
                         "counter: writing 1 persisted for 1.8 s, and after this "
                         "tool started setting it, 5/5 fighter wings went from "
                         "mission_on=false (through peace AND war) to true."),
                "wings": rows}
    return {"ok": False, "error": "Linux airops path removed; Windows PE only"}
