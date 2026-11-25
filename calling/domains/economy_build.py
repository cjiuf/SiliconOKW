"""建筑类型、建造队列与州建筑/占领详情领域 Mixin。"""
from __future__ import annotations

import offsets as O
import os
import struct
import sys

from ._shared import (
    BG_LEVEL,
    BG_TYPE_ID,
    BL_BUILDING,
    BL_COST,
    BL_PROGRESS,
    BL_RATE,
    BT_ID,
    BT_KEY,
    BT_NAME,
    BUILDING_REF_VTABLE,
    CO_PRODUCTION,
    CO_STATES,
    PS_CONSTRUCTION,
    REPO_ROOT,
    ST_BUILDINGS,
    ST_COMPLIANCE,
    ST_ID,
    ST_PROVINCES,
    ST_RESISTANCE,
    _arr,
    _count,
    item_name,
)

class EconomyBuildMixin:
    """建筑类型、建造队列与州建筑/占领详情领域 Mixin。"""

    def _bld_cache_path(self):
        root = REPO_ROOT
        return os.path.join(root, "research", "windows", "blddb_win.pkl")

    def _bld_cache_load(self):
        """{id: (key, province_level)} from disk, or None.

        2026-11-21 (P1-3): the building table is 55 immutable templates, but the
        only way to read it live is `win_scan_class("CBuildingTemplate")`, an
        8.5 GB byte scan -- and `_load_buildings` used to reach it through
        `oi.singleton("CBuildingDatabase")`, which builds the whole ObjectIndex
        (50 s measured) before falling back.  Every cold hoi4_overview paid it
        for a table that never changes.  Commands only need `id`, so the id/key
        half is cached on disk the same way `eqtypes_win.pkl` already is.
        """
        import safe_cache
        try:
            path = self._bld_cache_path()
            if safe_cache.exists(path):
                d = safe_cache.load(path)
                if isinstance(d, dict) and len(d) > 10:
                    return d
        except (safe_cache.UnsafePickleError, safe_cache.CacheIntegrityError):
            raise
        except Exception:
            pass
        return None

    def _bld_cache_save(self, byid, bykey):
        import safe_cache
        try:
            d = {int(bid): (key, bool(bykey.get(key, {}).get("province_level")))
                 for bid, key in byid.items()}
            safe_cache.save(self._bld_cache_path(), d, fmt="json")
        except Exception:
            pass

    def _load_buildings(self):
        if getattr(self, "_bld_by_key", None) is not None:
            return
        p = self.p
        if sys.platform == "win32":
            cached = self._bld_cache_load()
            if cached:
                bykey, byid = {}, {}
                for bid, val in cached.items():
                    key, plev = val if isinstance(val, tuple) else (val, False)
                    bykey[key] = {"id": int(bid), "name": key, "ptr": 0,
                                  "province_level": bool(plev)}
                    byid[int(bid)] = key
                for bid, key in getattr(O, "BT_EXTRA", {}).items():
                    bykey.setdefault(key, {"id": bid, "name": key, "ptr": 0,
                                           "province_level": True})
                    byid[bid] = key
                self._bld_by_key, self._bld_by_id = bykey, byid
                return
        db = self.oi.singleton("CBuildingDatabase")
        if not db:
            self._oi = None
            db = self.oi.singleton("CBuildingDatabase")
        if not db and sys.platform == "win32":
            # PE 1.19.5.0: CBuildingDatabase has no RTTI-visible singleton. The 55
            # CBuildingTemplate objects are found by vtable instead; id at +0x08,
            # script key (MSVC std::string) at +0x270. Verified live: 20002
            # arms_factory, 20013 industrial_complex, 22395 coastal_bunker.
            bykey, byid = {}, {}
            for t in self.win_scan_class("CBuildingTemplate"):
                key = item_name(p, t + BT_KEY)
                if key:
                    bid = p.i32(t + BT_ID)
                    bykey[key] = {"id": bid, "name": key, "ptr": t,
                                  "province_level": p.i32(t + 0x294) > 0}
                    byid[bid] = key
            for bid, key in getattr(O, "BT_EXTRA", {}).items():
                bykey.setdefault(key, {"id": bid, "name": key, "ptr": 0,
                                       "province_level": True})
                byid[bid] = key
            if len(byid) > 10:
                self._bld_cache_save(byid, bykey)
            self._bld_by_key, self._bld_by_id = bykey, byid
            return
        if not db:
            self._bld_by_key = None
            self._bld_by_id = {}
            self._oi = None
            return
        bykey, byid = {}, {}
        for t in _arr(p, db, O.BLD_DB_ITEMS):
            key = item_name(p, t + BT_KEY)
            if key:
                bid = p.i32(t + BT_ID)
                name = item_name(p, t + BT_NAME) if BT_NAME else None
                bykey[key] = {"id": bid, "name": name or key, "ptr": t,
                              "province_level": p.i32(t + 0x294) > 0}
                byid[bid] = key
        # Type ids the database does not describe (Windows: `infrastructure`).
        for bid, key in getattr(O, "BT_EXTRA", {}).items():
            bykey.setdefault(key, {"id": bid, "name": key, "ptr": 0,
                                   "province_level": True})
            byid[bid] = key
        self._bld_by_key, self._bld_by_id = bykey, byid

    def building_types(self):
        self._load_buildings()
        return {k: {"id": v["id"], "name": v["name"], "province_level": v["province_level"]}
                for k, v in sorted(self._bld_by_key.items())}

    def construction_queue(self, tag=None):
        p = self.p
        self._load_buildings()
        c_obj = self._c(tag)
        ps = p.u64(c_obj.ptr + CO_PRODUCTION)
        # 2026-11-22 (P3-4): 预建本国 CBuilding* -> (state_id, state_buildings_map) 反查表，
        # 仅遍历本国自有州（~24 州 / ~72 指针，< 1ms），精确补齐每条建造线的 state / state_name / slots_free。
        bld_to_state = {}
        state_blds = {}
        try:
            for s in _arr(p, c_obj.ptr, CO_STATES):
                sid = p.i32(s + ST_ID)
                bmap = {}
                for bp in _arr(p, s, ST_BUILDINGS):
                    bld_to_state[bp] = sid
                    nm = self._bld_by_id.get(p.i32(bp + BG_TYPE_ID))
                    if nm:
                        bmap[nm] = struct.unpack("<h", p.read(bp + BG_LEVEL, 2))[0]
                state_blds[sid] = bmap
        except Exception:
            pass
        _slot_buildings = set()
        _slot_bonus = None
        _meta_cache = {}
        try:
            import statemeta as _sm
            _slot_buildings = _sm._load_slot_buildings()
            try:
                _slot_bonus = (c_obj.modifiers() or {}).get("global_building_slots_factor")
            except Exception:
                _slot_bonus = None
        except Exception:
            _sm = None

        out = []
        prev_funded_total = 0
        # NOTE: this array is MIXED, alongside CBuildingProductionLine it also holds
        # CRailwayProductionLine, where +0x70 is NOT a building pointer (reading it
        # produced garbage and an EFAULT). Separate each element by its class.
        for i, l in enumerate(_arr(p, ps, PS_CONSTRUCTION)):
            # 2026-11-22: classify by vptr.  `oi.class_of()` forced the ObjectIndex
            # build (50 s) on the first construction read, which is inside the
            # first snapshot of every cold hoi4_wait_for.
            _vtb = getattr(O, "VT_BUILDING_LINE", None)
            cls = "CBuildingProductionLine" if (_vtb and p.u64(l) == p.base + _vtb) else None
            if cls is None:
                cls = self.oi.class_of(l)
            fac = p.i32(l + 0x18)
            row = {"index": i, "class": cls, "factories": fac}
            if cls == "CBuildingProductionLine":
                prog = p.i64(l + BL_PROGRESS) / O.FIXED
                cost = p.i64(l + BL_COST) / O.FIXED
                rate = p.i64(l + BL_RATE) / O.FIXED
                b = p.u64(l + BL_BUILDING)
                # 2026-11-22: was `self.oi.class_of(b) == "CBuilding"`, which is
                # another ObjectIndex build; the type id lookup already proves the
                # pointer is a building (a bad pointer raises and is dropped).
                bld_name = None
                if b:
                    try:
                        bld_name = self._bld_by_id.get(p.i32(b + BG_TYPE_ID))
                    except Exception:
                        pass
                sid = bld_to_state.get(b) if b else None
                sname = None
                sfree = None
                if sid is not None and _sm is not None:
                    if sid not in _meta_cache:
                        try:
                            _meta_cache[sid] = _sm.state_meta(sid, state_blds.get(sid), _slot_bonus)
                        except Exception:
                            _meta_cache[sid] = {}
                    _m = _meta_cache[sid]
                    sname = _m.get("name")
                    sfree = _m.get("slots_free")

                # 区分“正在建造 / 排队等民工 / 满槽或无民工真停滞”：
                # 当国家可用民工已被前面建造线吃满（如 15+6=21）而本州仍有空槽时，
                # 0 厂属于正常队列等待（status="queued", stalled=False），不应误判为卡死。
                no_slot = (
                    bld_name in _slot_buildings
                    and sfree is not None
                    and int(sfree) <= 0
                )
                if fac > 0:
                    status = "building"
                    stalled = False
                    stall_reason = None
                elif no_slot:
                    status = "stalled_no_slot"
                    stalled = True
                    stall_reason = "state %s has no free building slots" % sid
                elif prev_funded_total > 0:
                    status = "queued"
                    stalled = False
                    stall_reason = None
                else:
                    status = "stalled_no_cic"
                    stalled = True
                    stall_reason = "no civilian factories allocated to queue head"

                row.update({
                    "state": sid,
                    "state_name": sname,
                    "state_slots_free": sfree,
                    "building": bld_name,
                    "progress": round(prog / cost, 4) if cost > 0 else 0.0,
                    "progress_ic": round(prog, 2),
                    "cost_ic": round(cost, 2),
                    "ic_per_day": round(rate, 2),
                    "days_left": (round(max(0.0, (cost - prog) / rate), 1)
                                  if rate > 0 else None),
                    "status": status,
                    "stalled": stalled,
                })
                if status == "queued":
                    row["waiting_for_cic"] = True
                if stall_reason:
                    row["stall_reason"] = stall_reason
                prev_funded_total += max(0, fac)
            else:
                # CRailwayProductionLine -- different class, offsets not verified.
                row.update({"state": None, "state_name": None,
                            "progress": None, "progress_ic": None, "cost_ic": None,
                            "ic_per_day": None, "days_left": None,
                            "building": ("railway" if cls == "CRailwayProductionLine"
                                         else None),
                            "status": "unknown",
                            "layout_verified": False})
                prev_funded_total += max(0, fac)
            out.append(row)
        return out

    def states(self, tag=None):
        p = self.p
        return [{"id": p.i32(s + ST_ID), "ptr": s} for s in _arr(p, self._c(tag).ptr, CO_STATES)]

    def add_construction(self, state_id: int, building: str, level: int = 1,
                         province: int = 0, tag=None):
        """Queue a building: the same as using the construction tab."""
        self._load_buildings()
        c = self._c(tag)
        b = self._bld_by_key.get(building)
        if not b:
            raise KeyError("no such building type: %s (see building_types)" % building)
        if sys.platform == "win32":
            # Real command channel on the PE build.  The queue IS readable now
            # (PS_CONSTRUCTION + BL_PROGRESS were fixed on 2026-11-19), so report
            # the line the engine really created instead of trusting the post.
            #
            # `level` goes straight into CAddConstructionCommand+0x48.  Disassembly
            # of Execute (0x11DAEF0) shows `cmp [rdi+0x48], r15d / jle ...`, i.e.
            # it is the TARGET LEVEL of that building slot, not "how many levels to
            # queue": the engine builds one 7200-IC step at a time until the slot
            # reaches it.  A player clicking the build button sends 1, which is also
            # this default.  Measured: level=2 on a fresh slot still produced a
            # single queue line with cost_ic 7200.
            import cmdwin
            before = self.construction_queue(c.tag)
            r = cmdwin.add_construction(self, int(state_id), b["id"], level)
            self._settle()
            after = self.construction_queue(c.tag)
            new = [q for q in after if q.get("building") == building
                   and q["index"] >= len(before)]
            out = {"ok": bool(r.get("ok")) and len(after) > len(before),
                   "state": int(state_id), "building": building,
                   "type_id": b["id"], "level_sent": level,
                   "queue_before": len(before), "queue_after": len(after),
                   "levels_queued": max(0, len(after) - len(before)),
                   "cmd": r.get("cmd")}
            if new:
                out["line"] = new[0]
                # 2026-11-21 (P1-5): `ok` only ever meant "the queue grew".  When
                # every civilian factory is already busy the engine still creates
                # the line -- with 0 factories and stalled=true -- and the
                # 2026-11-20 playtest read that as success.  Say which one it is
                # instead of leaving ok:true and stalled:true side by side.
                _ln = new[0]
                try:
                    _fac = int(_ln.get("factories") or 0)
                except Exception:
                    _fac = 0
                if _fac <= 0 or _ln.get("stalled"):
                    out["queued"] = True
                    out["progressing"] = False
                    out["stalled"] = bool(_ln.get("stalled"))
                    out["warning"] = (
                        "queued, but this line holds %d civilian factor%s -- it "
                        "will not progress until CIC frees up. ok=true means the "
                        "queue grew, not that it is building."
                        % (_fac, "y" if _fac == 1 else "ies"))
                else:
                    out["progressing"] = True
            if level != 1:
                out["note"] = ("level is the slot's target level, not a repeat "
                               "count; one queue line was created (cost_ic is a "
                               "single level).")
            if not out["ok"]:
                out["error"] = ("the game accepted the pointer but the queue did "
                                "not grow -- usually the state has no free "
                                "building slot for this type")
            return out
        before = len(self.construction_queue(c.tag))
        # CBuildingReference (1.19.4.1, verified by disassembly and live test):
        #   +0x08 = location id   (state id for a state-level building, province id
        #                          for a province-level one)
        #   +0x0C = building type id
        #   +0x10 = 0
        # The AddConstruction ctor copies ref+8..ref+0x10 into cmd+0x30..+0x3C.
        loc = int(province or 0)
        if loc <= 0:
            loc = int(state_id)
        with self:
            cmd = self.cmd.make("AddConstruction", tag=c.tag_id,
                                ref_vptr=self.va(BUILDING_REF_VTABLE),
                                province=loc, building_type=b["id"], state=0,
                                level=level, flag=1)
            # CCommand::Run: CanExecute -> Post(queue, cmd, 1), the click path.
            rv = self._run_cmd(cmd)
        if not (rv & 0xFF):
            return {"ok": False, "before": before, "after": before,
                    "error": "the game refused the construction order (CanExecute false)"}
        # The command is asynchronous: the construction line appears on the next tick.
        q = self.construction_queue(c.tag)
        for _ in range(12):
            if len(q) > before:
                break
            self._settle(0.5)
            q = self.construction_queue(c.tag)
        return {"ok": len(q) > before, "before": before, "after": len(q), "queue": q}

    def remove_construction(self, index: int, tag=None):
        """Remove one queued construction line. Returns ok only if the queue shrank.

        2026-11-20: `CRemoveConstructionCommand` (vtable 0x2A10C48, CanExecute
        0x11F17C0, Execute 0x11E2F30) is REFUSED by its own CanExecute on this
        build for every field combination that could be constructed from the
        published layout -- it wants an 8-byte value at +0x28 that neither the
        country tag (1) nor the line handle ({59,130}) satisfies (the only value
        that passed was {59,1}, and that Execute then ignored).  See
        KNOWN_ISSUES P1 #15.

        So the queue is edited directly instead: CProductionStatus+PS_CONSTRUCTION
        is a CPdxArray<CProductionLine*>; shift the pointers after `index` left by
        one and decrement the count.  Verified live 2026-03-05: the line vanishes,
        its 15 civilian factories go back to idle, and the engine does NOT
        re-create it on later ticks (checked after 4 in-game days).
        """
        p = self.p
        c = self._c(tag)
        ps = p.u64(c.ptr + CO_PRODUCTION)
        off = PS_CONSTRUCTION
        n = p.i32(ps + off + O.PDXARRAY_SIZE)
        data = p.u64(ps + off + O.PDXARRAY_DATA)
        before = self.construction_queue(c.tag)
        if not (0 <= index < n) or not data:
            return {"ok": False, "before": len(before),
                    "error": "no such queue line: %d (of %d)" % (index, n)}
        row = before[index] if index < len(before) else {}
        if n > 1:
            raw = p.read(data, 8 * n)
            # 2026-11-22 (复测 5 P0 §2.1): 正确 splice 是保留前 index 项、把 [index+1:] 左移 1 格、
            # 尾部仅清零 1 个指针槽 (8 字节)。旧写法 `raw[8*(index+1):] + b"\0"*(8*(index+1))`
            # 在 index>0 时会直接抹掉前 index 条产线并在尾部留空指针，导致引擎遍历 AV 崩档。
            new_buf = raw[:8 * index] + raw[8 * (index + 1):] + b"\0" * 8
            p.write(data, new_buf)
        else:
            p.write(data, b"\0" * 8)
        p.write(ps + off + O.PDXARRAY_SIZE, struct.pack("<i", n - 1))
        self._settle(0.2)
        after = self.construction_queue(c.tag)
        return {"ok": len(after) < len(before),
                "before": len(before), "after": len(after),
                "removed": row.get("building"), "removed_state": row.get("state"),
                "state_line": index,
                "method": "queue-array",
                "note": "edited the queue array directly: the engine's "
                        "CRemoveConstructionCommand rejects this build "
                        "(KNOWN_ISSUES P1 #15)"}

    def state_detail(self, state_id: int, tag=None):
        p = self.p
        self._load_buildings()
        # 2026-11-23 (复测 6 P2-1): 传了 tag 时先查该国 CO_STATES；找不到时自动回退全局
        # 遍历所有国家，避免查傀儡/占领/他国州（如 state 9 归属 RKH）直接抛裸 KeyError。
        # 全局仍找不到时返回结构化错误 JSON，不再向 MCP 客户端吐 traceback。
        _found = None
        _owner_tag = None
        req_tag = str(tag).upper() if tag else None
        if req_tag:
            try:
                _co = self._c(req_tag)
                for s in _arr(p, _co.ptr, CO_STATES):
                    if p.i32(s + ST_ID) == state_id:
                        _found = s
                        _owner_tag = _co.tag
                        break
            except Exception:
                _found = None
        if _found is None:
            try:
                for c in self.countries():
                    for s in _arr(p, c.ptr, CO_STATES):
                        if p.i32(s + ST_ID) == state_id:
                            _found = s
                            _owner_tag = c.tag
                            break
                    if _found:
                        break
            except Exception:
                _found = None
        if _found is None:
            return {
                "ok": False,
                "error": "state not found: %s" % state_id,
                "state": int(state_id),
                "requested_tag": req_tag,
            }
        s = _found
        blds = {}
        for b in _arr(p, s, ST_BUILDINGS):
            nm = self._bld_by_id.get(p.i32(b + BG_TYPE_ID))
            if nm:
                blds[nm] = struct.unpack("<h", p.read(b + BG_LEVEL, 2))[0]
        # 读出真实核心国家列表（Windows: CState + 0xB0 CPdxArray<int32>）
        cores = None
        if sys.platform == "win32":
            try:
                cd = p.u64(s + 0xB0)
                cn = p.i32(s + 0xBC)
                if cd and 0 < cn <= 128:
                    import combat as _cb_sd
                    id2tag = _cb_sd._country_id_map(self)
                    vals = struct.unpack("<%di" % cn, p.read(cd, 4 * cn))
                    cores = [id2tag.get(v, str(v)) for v in vals]
                elif cn == 0:
                    cores = []
            except Exception:
                cores = None
        res = {
            "ok": True,
            "id": state_id,
            "owner": _owner_tag,
            "ptr": hex(s),
            "buildings": blds,
            "provinces": _count(p, s, ST_PROVINCES),
            "cores": cores,
            "is_core_of_owner": (bool(_owner_tag in cores) if (cores is not None and _owner_tag) else None),
            "resistance": (round(p.i64(s + ST_RESISTANCE) / O.FIXED, 3)
                           if ST_RESISTANCE else None),
            "compliance": (round(p.i64(s + ST_COMPLIANCE) / O.FIXED, 3)
                           if ST_COMPLIANCE else None),
            "building_status_ptr": hex(s + 0x120),
        }
        if req_tag and _owner_tag and req_tag != str(_owner_tag).upper():
            res["requested_tag"] = req_tag
            res["owner_note"] = (
                "state %s is owned/controlled by %s, not %s (resolved via global fallback)"
                % (state_id, _owner_tag, req_tag)
            )
        # 2026-11-21 (P3-6): null is not "the reader failed" -- a CORE state has
        # no resistance/compliance at all.  Say so instead of leaving two nulls.
        if res.get("resistance") is None and res.get("compliance") is None:
            res["occupation_note"] = (
                "null = no occupation data: this is a CORE state of the country "
                "that owns it. resistance/compliance only exist for non-core "
                "occupied territory.")
        # 2026-11-21 (P1-6): id + building map alone cannot answer "which
        # state is 42" or "can I still build here".  Both come from the
        # game's own data files (history/states, common/state_category,
        # common/buildings) -- see calling/statemeta.py.
        try:
            import statemeta
            _bonus = None
            try:
                _bonus = (self._c(_owner_tag or tag).modifiers() or {}).get(
                    "global_building_slots_factor")
            except Exception:
                _bonus = None
            res.update(statemeta.state_meta(state_id, blds, _bonus))
        except Exception as _e:
            res["state_meta_error"] = repr(_e)
        return res

    def states_detail(self, tag=None, limit=40):
        out = []
        for s in self.states(tag)[:limit]:
            try:
                out.append(self.state_detail(s["id"], tag))
            except Exception:
                pass
        return out
