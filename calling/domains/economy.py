"""装备类型、军工/船坞产线、工厂分配、装备库存与战略资源领域 Mixin。"""
from __future__ import annotations

import offsets as O
import os
import struct
import sys

from ._shared import (
    CO_PRODUCTION,
    CO_RESOURCES,
    ET_ID,
    ET_NAME,
    EV_TYPE_ID,
    ML_EFF_COST,
    ML_FACTORIES,
    ML_MAX_EFFICIENCY,
    ML_OUTPUT,
    ML_PRIORITY,
    ML_UNIT_COST,
    ML_VARIANT,
    PS_LINES,
    PS_MIL_LINES,
    PS_VARIANTS_ACTIVE,
    REPO_ROOT,
    RESOURCE_NAMES,
    RES_ARRAY,
    RES_STRIDE,
    _arr,
    item_name,
    read_scoped,
)

class EconomyMixin:
    """装备类型、军工/船坞产线、工厂分配、装备库存与战略资源领域 Mixin。"""

    PE_ADD_LINE_VT = 0x2A0FBC8
    PE_ADD_LINE_CANEXE = 0x11EDB10
    PE_ADD_LINE_VARIANT_OK = 0x140C2CAD0   # (CEquipmentVariant*, int *tag) -> bool

    PS_STOCKPILE = 0x220   # CPdxArray<{CEquipmentVariant*; int64 count x1e5}>

    def _win_equipment_types(self):
        """{equipment archetype id: script key} on the PE build.

        CEquipmentType carries the id at +0x08 and the key as an MSVC std::string
        at +0x18 (verified live: 456 objects, 15400 infantry_equipment,
        15401 artillery_equipment, 20000 anti_air_equipment).  CEquipmentVariant
        +0x18 holds exactly that id, so this one map is enough to name any
        production line.  Cached in research/windows/eqtypes_win.pkl.
        """
        import safe_cache
        root = REPO_ROOT
        path = os.path.join(root, "research", "windows", "eqtypes_win.pkl")
        try:
            if safe_cache.exists(path):
                d = safe_cache.load(path)
                if len(d) > 100:
                    return d
        except (safe_cache.UnsafePickleError, safe_cache.CacheIntegrityError):
            raise
        except Exception:
            pass
        d = {}
        for t in self.win_scan_class("CEquipmentType"):
            nm = item_name(self.p, t + 0x18)
            if nm:
                d[self.p.i32(t + 0x08)] = nm
        if len(d) > 100:
            try:
                safe_cache.save(path, d, fmt="json")
            except Exception:
                pass
        return d

    def _load_equipment(self):
        if getattr(self, "_eq_by_id", None) is not None:
            return
        p = self.p
        # 2026-11-21 (P1-3): on the PE build the id->key map comes from a small
        # pickle.  Asking `oi.singleton("CEquipmentDatabase")` FIRST made the
        # ObjectIndex walk the whole address space (10-15 s) just to fail and
        # fall back to this map -- and every cold `hoi4_overview` paid it.
        if sys.platform == "win32":
            byid = self._win_equipment_types()
            if byid:
                self._eq_by_id = byid
                self._eq_by_name = {v: k for k, v in byid.items()}
                return
        db = self.oi.singleton("CEquipmentDatabase")
        if not db and sys.platform == "win32":
            byid = self._win_equipment_types()
            self._eq_by_id = byid
            self._eq_by_name = {v: k for k, v in byid.items()}
            return
        if not db:
            self._eq_by_id, self._eq_by_name = {}, {}
            return
        byid, byname = {}, {}
        # CEquipmentDatabase+0x68 is CPdxArray<CEquipmentArchetype*>: 457 entries in
        # 1.19.5, id at +0x08, name at +0x18. The old list (0x60/0x78/0xF0/0x120) was
        # the 1.19.2 layout and every entry landed 8 bytes off the real CPdxArray
        # header, so this map came out EMPTY and every production line reported
        # equipment: null while the variant pointer itself was correct all along.
        for off in (0x68,):
            for t in _arr(p, db, off):
                if not t:
                    continue
                nm = item_name(p, t + ET_NAME)
                if nm:
                    byid[p.i32(t + ET_ID)] = nm
                    byname[nm] = t
        self._eq_by_id, self._eq_by_name = byid, byname

    def equipment_types(self, filter_text=""):
        self._load_equipment()
        f = filter_text.lower()
        return sorted(n for n in self._eq_by_name if not f or f in n.lower())

    @read_scoped("_active_variant_names", key_fn=lambda self, tag=None: (self._rs_tag_key(tag),))
    def _active_variant_names(self, tag=None):
        """纯内存读取本国已解锁可生产的装备型号名列表（< 1ms）。"""
        p = self.p
        self._load_equipment()
        ps = p.u64(self._c(tag).ptr + CO_PRODUCTION)
        out = []
        seen = set()
        for v in _arr(p, ps, PS_VARIANTS_ACTIVE):
            nm = self._eq_by_id.get(p.i32(v + EV_TYPE_ID))
            if nm and nm not in seen:
                seen.add(nm)
                out.append(nm)
        return out

    @read_scoped(
        "production_lines",
        key_fn=lambda self, tag=None, include_obsolete=True: (self._rs_tag_key(tag), bool(include_obsolete)),
    )
    def production_lines(self, tag=None, include_obsolete=True):
        p = self.p
        self._load_equipment()
        ps = p.u64(self._c(tag).ptr + CO_PRODUCTION)
        out = []
        # 2026-11-21 (P1-3): name the line from its own vptr.  `self.oi.class_of`
        # forces the ObjectIndex to build (10-15 s of heap scanning) on its first
        # call -- that is what made a cold hoi4_overview blow the 50 s budget.
        # The two vtables cover every line; anything else still goes through RTTI.
        _vt_names = {}
        for _nm, _attr in (("CMilitaryProductionLine", "VT_MIL_LINE"),
                           ("CNavalProductionLine", "VT_NAV_LINE")):
            _rva = getattr(O, _attr, None)
            if _rva:
                _vt_names[p.base + _rva] = _nm
        for i, l in enumerate(_arr(p, ps, PS_LINES)):
            v = p.u64(l + ML_VARIANT)
            eid = p.i32(v + EV_TYPE_ID) if v else None
            cost = p.i64(l + ML_UNIT_COST) / O.FIXED
            icpd = p.i64(l + ML_OUTPUT) / O.FIXED
            _cls = _vt_names.get(p.u64(l))
            if _cls is None:
                _cls = self.oi.class_of(l)
            _max_eff = p.i32(l + ML_MAX_EFFICIENCY)
            _row = {
                "index": i,
                "equipment": self._eq_by_id.get(eid),
                "factories": p.i32(l + ML_FACTORIES),
                # 2026-11-21 (续测 P2-8): three lines sit permanently at 0 factories
                # (18/19/20 in a 1936 GER save) and therefore read
                # output_per_day 0.0.  They are real, empty line objects the player
                # left behind -- not a reader bug -- so flag them instead of letting
                # a caller mistake them for dead/stuck production.
                "idle": p.i32(l + ML_FACTORIES) == 0,
                "unit_cost": round(cost, 3),
                # +0x38 is NOT a cost.  It is the IC already invested in the unit
                # currently on the line: every observed value is < unit_cost and
                # the four submarine lines (all unit_cost 440) read 42.31 /
                # 438.31 / 394.31 / 350.31, i.e. hulls at different completion
                # stages.  The old name `effective_cost` implied a modified price
                # and was simply wrong.
                "unit_progress_ic": round(p.i64(l + ML_EFF_COST) / O.FIXED, 3),
                # +0x30 is IC per day for the whole line, not units per day:
                # one dockyard line reads 2.935 = 2.5 base x 1.174, and
                # industrial_capacity_dockyard_factor is exactly +0.174.
                "output_per_day": round(icpd, 3),
                "output_units_per_day": (round(icpd / cost, 4) if cost > 0 else None),
                "max_efficiency": _max_eff,
                "priority": p.i32(l + ML_PRIORITY),
                "class": _cls,
            }
            # 2026-11-26 (P2-9): CNavalProductionLine+0xC8 实测为引擎单舰船坞硬上限
            # （主力舰/重巡=5，轻巡/驱逐/潜艇=10，运输船=15），直接透出 max_dockyards 消除盲撞。
            if _cls == "CNavalProductionLine" and 1 <= _max_eff <= 25:
                _row["max_dockyards"] = _max_eff
            out.append(_row)
        # 2026-11-22 (复测 5 §8.4 时刻 ②): 每条军工产线透出 variant_year / obsolete /
        # upgrade_to，过时产线附带 upgrade_gain / retool_cost / payback_days / suggested_action。
        if include_obsolete and out:
            try:
                import equipmeta as _em
                avail = []
                seen_v = set()
                for v in _arr(p, ps, PS_VARIANTS_ACTIVE):
                    nm = self._eq_by_id.get(p.i32(v + EV_TYPE_ID))
                    if nm and nm not in seen_v:
                        seen_v.add(nm)
                        avail.append(nm)
                analysis = _em.analyze_production_lines(out, avail)
                for row in out:
                    a = analysis.get(row["index"])
                    if not a:
                        continue
                    row["archetype"] = a.get("archetype")
                    row["variant_year"] = a.get("variant_year")
                    row["obsolete"] = bool(a.get("obsolete"))
                    row["upgrade_to"] = a.get("upgrade_to")
                    if a.get("obsolete"):
                        row["upgrade_to_year"] = a.get("upgrade_to_year")
                        row["upgrade_gain"] = a.get("upgrade_gain")
                        row["retool_cost"] = a.get("retool_cost")
                        row["payback_days"] = a.get("payback_days")
                        row["suggested_action"] = a.get("suggested_action")
            except Exception:
                pass
        return out

    @read_scoped("factory_allocation", key_fn=lambda self, tag=None: (self._rs_tag_key(tag),))
    def factory_allocation(self, tag=None):
        """引擎精确口径的军工 / 船坞分配账本（纯只读）。

        2026-11-23 (复测 6 P1-1): 依据 CProductionStatus::UpdateFactories (0xede8c0)
        与 GetFreeNavalDockyards (0xeda420) 反汇编结果，直接读取引擎实时维护的
        真实可用数（已计入核心+占领合规+傀儡上供+离岸工厂+受损预留+舰船改装线
        CShipRefitProductionLine [type 71]+修理船坞 CStrategicNavy+0x1A4），
        彻底消除过去用原始建筑总和减 type-57 产线造成的假空闲船坞与清不掉的 IDLE_DOCK 红点。
        """
        p = self.p
        c = self._c(tag)
        prod = 0
        lines = []
        try:
            prod = p.u64(c.ptr + CO_PRODUCTION)
            lines = _arr(p, prod, PS_MIL_LINES) if prod else []
        except Exception:
            prod, lines = 0, []

        mil_lines_used = 0
        nav_lines_used = 0
        refit_lines_used = 0
        mil_lines_req = 0
        nav_lines_req = 0
        refit_lines_req = 0
        for ln in lines:
            try:
                ht = self.handle_of(ln)["type"]
                used = max(0, int(p.i32(ln + ML_FACTORIES))) + max(0, int(p.i32(ln + 0x1C)))
                req = max(0, int(p.i32(ln + 0xE8))) if sys.platform == "win32" else used
            except Exception:
                continue
            if ht == 56:
                mil_lines_used += used
                mil_lines_req += req
            elif ht == 57:
                nav_lines_used += used
                nav_lines_req += req
            elif ht == 71:
                refit_lines_used += used
                refit_lines_req += req

        raw_mil = int(getattr(c, "military_factories", 0) or 0)
        raw_dock = int(getattr(c, "naval_dockyards", 0) or 0)
        raw_civ = int(getattr(c, "civilian_factories", 0) or 0)

        # Windows 1.19.5.0 权威引擎口径（0xede8c0 / 0xeda420）
        if sys.platform == "win32" and prod:
            try:
                mil_tot = int(p.i64(prod + 0x2D0) // 100000)
                mil_res = max(0, int(p.i32(prod + 0x308)))
                mil_used_eng = max(0, int(p.i32(prod + 0x2F0)))
                dock_tot = int(p.i64(prod + 0x330) // 100000)
                dock_res = max(0, int(p.i32(prod + 0x368)))
                dock_used_eng = max(0, int(p.i32(prod + 0x350)))

                # CGameState + 0x698 (CStrategicNavyManager) -> +0x08[tag_idx] -> +0x1A4 修理占用船坞
                # 2026-11-23 (复测 8 B-12 根因修复): self._tag2idx 是 list 而非 dict！
                # 旧代码写 self._tag2idx.get(...) 每次抛 AttributeError 被吞成 0，导致把 1936 德国
                # 3 座修理船坞误报为 3 座空闲船坞，触发不可解 IDLE_DOCK 假死锁。
                repair_dockyards = 0
                try:
                    gs = self.gamestate
                    snm = p.u64(gs + 0x698) if gs else 0
                    if snm:
                        arr_d = p.u64(snm + 0x08)
                        arr_n = int(p.i32(snm + 0x14))
                        self._load_tags()
                        t2i = getattr(self, "_tag2idx", None)
                        if isinstance(t2i, dict):
                            tidx = int(t2i.get(c.tag_id, c.tag_id))
                        elif isinstance(t2i, (list, tuple)) and 0 <= c.tag_id < len(t2i):
                            tidx = int(t2i[c.tag_id])
                        else:
                            tidx = int(c.tag_id)
                        if arr_d and 0 <= tidx < arr_n:
                            sn = p.u64(arr_d + tidx * 8)
                            if sn and 0x10000 < sn < 0x7FFFFFFFFFFF:
                                rd = int(p.i32(sn + 0x1A4))
                                if 0 <= rd <= 500:
                                    repair_dockyards = rd
                except Exception:
                    repair_dockyards = 0

                if (0 <= mil_tot <= 5000 and 0 <= dock_tot <= 5000
                        and mil_used_eng <= mil_tot + 64 and dock_used_eng <= dock_tot + 64):
                    mil_avail = max(0, mil_tot - mil_res)
                    mil_idle = max(0, mil_avail - mil_used_eng)
                    dock_avail = max(0, dock_tot - dock_res - repair_dockyards)
                    dock_idle = max(0, dock_avail - dock_used_eng)
                    return {
                        "source": "engine_production_status",
                        "civilian_factories": raw_civ,
                        "military_raw_buildings": raw_mil,
                        "military_available": mil_avail,
                        "military_used": mil_used_eng,
                        "military_requested": mil_lines_req,
                        "military_reserved": mil_res,
                        "military_idle": mil_idle,
                        "naval_raw_buildings": raw_dock,
                        "naval_total_effective": dock_tot,
                        "naval_available": dock_avail,
                        "naval_used": dock_used_eng,
                        "naval_build_used": nav_lines_used,
                        "naval_refit_used": refit_lines_used,
                        "naval_repair_dockyards": repair_dockyards,
                        "naval_reserved": dock_res,
                        "naval_requested": nav_lines_req + refit_lines_req,
                        "naval_idle": dock_idle,
                    }
            except Exception:
                pass

        # 非 Windows 或读取异常时优雅降级（同时计入 type 57 造船线 + type 71 改装线）
        nav_used_sum = nav_lines_used + refit_lines_used
        return {
            "source": "lines_sum_fallback",
            "civilian_factories": raw_civ,
            "military_raw_buildings": raw_mil,
            "military_available": raw_mil,
            "military_used": mil_lines_used,
            "military_requested": mil_lines_req,
            "military_reserved": 0,
            "military_idle": max(0, raw_mil - mil_lines_used),
            "naval_raw_buildings": raw_dock,
            "naval_total_effective": raw_dock,
            "naval_available": raw_dock,
            "naval_used": nav_used_sum,
            "naval_build_used": nav_lines_used,
            "naval_refit_used": refit_lines_used,
            "naval_repair_dockyards": 0,
            "naval_reserved": 0,
            "naval_requested": nav_lines_req + refit_lines_req,
            "naval_idle": max(0, raw_dock - nav_used_sum),
        }

    def set_production_amount(self, line_index=None, amount: int = 0, tag=None,
                              auto_reclaim: bool = True, equipment=None):
        """Set how many FACTORIES a production line gets (the +/- buttons in the UI).

        NOTE. This used to silently do nothing: there is no command called
        "SetProductionLineAmount", and the result was never checked before returning
        {"ok": True}. The right command is `CAddProductionLineFactoriesCommand`:
        arg0 is the line's HANDLE (type 56) and arg1 is a DELTA, not an absolute
        count. The delta is computed here, and afterwards the line's factory field
        (CMilitaryProductionLine+0x18) is read back to report whether it really
        changed.

        2026-11-23 (复测 7 B-10): 当陆/空军工产线（type 56）需要加厂但空闲军工不足，且
        `auto_reclaim=True`（默认）时，自动从库存严重超储（`equipmeta.compute_surplus_lines`）
        的产线安全抽调缺口厂数（每条源线至少保留 1 厂），再补给目标线，返回 `reclaimed_from`。

        2026-11-27 (D-01 根治):
          * 支持直接传装备名定位（`equipment="transport_plane_equipment_1"` 或
            `line_index="transport_plane_equipment_1"`），彻底免疫产线增删导致的行号偏移；
          * 同时传整数 `line_index` 与 `equipment` 时执行防错行校验：若该行实际装备不符，
            直接拒绝并返回 `blocked_by="production_line_mismatch"` 及当前真实行号表，杜绝误砍步枪线。
        """
        p = self.p
        c = self._c(tag)
        prod = p.u64(c.ptr + CO_PRODUCTION)
        lines = _arr(p, prod, PS_MIL_LINES)

        def _line_eq_name(ln_ptr):
            try:
                self._load_equipment()
                return self._eq_by_id.get(p.i32(p.u64(ln_ptr + ML_VARIANT) + EV_TYPE_ID))
            except Exception:
                return None

        def _lines_table():
            tbl = []
            for idx_i, ln_i in enumerate(lines):
                try:
                    eq_i = _line_eq_name(ln_i)
                    fac_i = int(p.i32(ln_i + ML_FACTORIES))
                except Exception:
                    eq_i, fac_i = None, None
                tbl.append({"line": idx_i, "equipment": eq_i, "factories": fac_i})
            return tbl

        want_eq = str(equipment).strip() if equipment is not None and str(equipment).strip() else None
        resolved_idx = None
        if isinstance(line_index, str):
            s_li = line_index.strip()
            if s_li.isdigit():
                resolved_idx = int(s_li)
            elif s_li:
                want_eq = want_eq or s_li
        elif line_index is not None:
            resolved_idx = int(line_index)

        if resolved_idx is None:
            if not want_eq:
                return {"ok": False, "blocked_by": "missing_production_line",
                        "error": "set_production_amount requires line_index or equipment"}
            tbl = _lines_table()
            wl = want_eq.lower()
            exact = [r for r in tbl if r["equipment"] and r["equipment"].lower() == wl]
            partial = exact or [r for r in tbl if r["equipment"] and wl in r["equipment"].lower()]
            if not partial:
                return {
                    "ok": False,
                    "blocked_by": "no_matching_production_line",
                    "requested_equipment": want_eq,
                    "error": "no production line matches equipment %r" % want_eq,
                    "lines": tbl,
                }
            # 优先匹配当前已有工厂的线，其次取第一条匹配线
            partial.sort(key=lambda r: (-(int(r["factories"] or 0) > 0), r["line"]))
            resolved_idx = int(partial[0]["line"])
        else:
            if not (0 <= resolved_idx < len(lines)):
                return {
                    "ok": False,
                    "blocked_by": "invalid_line_index",
                    "error": "no such line: %d (of %d)" % (resolved_idx, len(lines)),
                    "lines": _lines_table(),
                }
            if want_eq:
                actual_eq = _line_eq_name(lines[resolved_idx])
                if actual_eq and want_eq.lower() not in actual_eq.lower() and actual_eq.lower() != want_eq.lower():
                    tbl = _lines_table()
                    wl = want_eq.lower()
                    matches = [r for r in tbl if r["equipment"] and (r["equipment"].lower() == wl or wl in r["equipment"].lower())]
                    return {
                        "ok": False,
                        "blocked_by": "production_line_mismatch",
                        "line": resolved_idx,
                        "actual_equipment": actual_eq,
                        "expected_equipment": want_eq,
                        "matching_lines": matches,
                        "lines": tbl,
                        "error": (
                            "line %d currently produces %r, not %r (index likely shifted after create/remove line). "
                            "Use equipment=%r or one of matching_lines=%s."
                            % (resolved_idx, actual_eq, want_eq, want_eq, matches)
                        ),
                    }

        line_index = resolved_idx
        line = lines[line_index]
        before = p.i32(line + ML_FACTORIES)
        # 2026-11-23 (复测 8 B-11/B-12): 反汇编证实 CAddProductionLineFactoriesCommand 修改的是
        # line+0xE8 (requested_factories)，再由 UpdateFactories 把有效空闲工厂分配到 line+0x18
        # (assigned_factories)。同步读取 requested_before 以便区分“命令未被消费（暂停）”与
        # “命令已消费但引擎无空闲工厂（如船坞全被修理占满）”，并在缩减时一并清掉灰格请求。
        req_before = before
        has_req_field = False
        if sys.platform == "win32":
            try:
                _rb = int(p.i32(line + 0xE8))
                if 0 <= _rb <= 200 and (_rb >= before or before == 0):
                    req_before = _rb
                    has_req_field = True
            except Exception:
                pass
        delta = int(amount) - before
        req_delta = int(amount) - req_before
        if delta == 0 and req_delta == 0:
            return {"ok": True, "line": line_index, "factories": before,
                    "requested_factories": req_before, "note": "already set"}
        h = self.handle_of(line)
        # 56 = 陆/空军工产线，57 = 海军造船线，71 = 舰船改装线（CShipRefitProductionLine）
        if h["type"] not in (56, 57, 71):
            return {"ok": False, "error": "expected a production line handle (56/57/71), got %d" % h["type"]}

        is_naval = h["type"] in (57, 71)
        fac_kind = "dockyards" if is_naval else "military factories"

        reclaimed_from = []
        if auto_reclaim and delta > 0 and h["type"] == 56:
            try:
                alloc0 = self.factory_allocation(c.tag) if hasattr(self, "factory_allocation") else {}
                idle0 = int(alloc0.get("military_idle")) if isinstance(alloc0, dict) and alloc0.get("military_idle") is not None else None
            except Exception:
                idle0 = None
            if idle0 is not None and idle0 < delta:
                need_reclaim = delta - max(0, idle0)
                try:
                    import equipmeta as _em_rc
                    pls_now = self.production_lines(c.tag, include_obsolete=False) or []
                    st_tot = (self.equipment_stockpile(c.tag) or {}).get("totals_by_equipment") or {}
                    divs_n = int((self.divisions(c.tag, limit=1) or {}).get("count") or 0)
                    tot_mil = int(alloc0.get("military_available") or sum(
                        int(x.get("factories") or 0) for x in pls_now
                        if x.get("class") == "CMilitaryProductionLine"
                    ))
                    surplus = _em_rc.compute_surplus_lines(pls_now, st_tot, tot_mil, divs_n) or []
                    for sr in surplus:
                        if need_reclaim <= 0:
                            break
                        s_idx = int(sr.get("line", -1))
                        if s_idx == line_index or s_idx < 0:
                            continue
                        cur_f = int(sr.get("factories") or 0)
                        sugg_f = max(1, int(sr.get("suggested_factories") or 1))
                        avail_take = max(0, cur_f - sugg_f)
                        if avail_take <= 0:
                            continue
                        take = min(need_reclaim, avail_take)
                        rr = self.set_production_amount(
                            s_idx, cur_f - take, tag=tag, auto_reclaim=False
                        )
                        actual_taken = max(0, cur_f - int(rr.get("after", cur_f)))
                        if actual_taken > 0:
                            reclaimed_from.append({
                                "line": s_idx,
                                "equipment": sr.get("equipment"),
                                "before": cur_f,
                                "after": int(rr.get("after", cur_f - actual_taken)),
                                "reclaimed": actual_taken,
                                "surplus_ratio": (
                                    sr.get("ratio") if sr.get("ratio") is not None else sr.get("surplus_ratio")
                                ),
                            })
                            need_reclaim -= actual_taken
                except Exception:
                    pass

        # 发令前先检查真实空闲工厂：若需要加厂但空闲明确为 0，绝不盲目往 +0xE8 叠灰格，
        # 也不误导成“游戏暂停”。
        pre_alloc = {}
        pre_idle = None
        try:
            pre_alloc = self.factory_allocation(c.tag)
            if isinstance(pre_alloc, dict) and pre_alloc.get("source") == "engine_production_status":
                pre_idle = int(pre_alloc["naval_idle"] if is_naval else pre_alloc["military_idle"])
        except Exception:
            pre_alloc, pre_idle = {}, None

        steps = []
        tray = []
        hit_line_cap = False
        if not (delta > 0 and pre_idle == 0):
            cur_assigned = before
            cur_req = req_before
            max_step = 5
            for _ in range(32):
                # 缩减或清理灰格时以 requested 为准；增加时以 assigned 为准（受 requested 同步推进）
                ref = cur_req if (has_req_field and int(amount) <= cur_req) else cur_assigned
                if cur_assigned == int(amount) and (not has_req_field or cur_req == int(amount)):
                    break
                step = int(amount) - ref
                if step == 0:
                    break
                step = max(-max_step, min(max_step, step))
                self.send_command("AddProductionLineFactories",
                                  {"line_id": (h["id"] << 32) | (h["type"] & 0xFFFFFFFF),
                                   "amount": step if step >= 0 else (step & 0xFFFFFFFF)},
                                  tag)
                self._settle(0.18)
                nxt_assigned = p.i32(line + ML_FACTORIES)
                nxt_req = p.i32(line + 0xE8) if has_req_field else nxt_assigned
                steps.append(step)
                tray.append(nxt_assigned)
                if nxt_assigned == cur_assigned and (not has_req_field or nxt_req == cur_req):
                    # 2026-11-23 (复测 10 B-2 PE 反汇编实锤):
                    # CAddProductionLineFactoriesCommand::CanExecute (0x11EDCC0) 严格校验
                    # `requested + step <= line->GetMaxFactories()`（驱逐舰/潜艇 10、主力舰 5、
                    # 运输船 15、陆军线 150）。若 cur=9 试图 step=+3 -> 12 > 10，CanExecute 直接
                    # 拒绝整条命令！此时自动将步长缩为 1 再试，既能精准打满单线上限（9 -> 10），
                    # 又能准确区分“撞单舰船坞上限”与“游戏暂停命令队列未消费”。
                    if abs(step) > 1:
                        max_step = 1
                        continue
                    if step > 0 and is_naval:
                        hit_line_cap = True
                    break
                if step > 0 and nxt_assigned == cur_assigned:
                    # 2026-11-27 (D-28 根治): 当 requested (+0xE8) 已增加但 assigned (+0x18) 尚未
                    # 刷新时，短轮询最多 3 次等待 UpdateFactories 把刚腾出的空闲工厂结算到位，
                    # 避免同计划刚缩减步枪线后紧接加厂被提前误判为缺工厂。
                    if has_req_field and nxt_req > nxt_assigned:
                        for _wait_i in range(3):
                            self._settle(0.20)
                            _re_a = p.i32(line + ML_FACTORIES)
                            if _re_a > nxt_assigned:
                                nxt_assigned = _re_a
                                tray[-1] = nxt_assigned
                                if nxt_assigned >= int(amount) or nxt_assigned >= nxt_req:
                                    break
                    if nxt_assigned == cur_assigned:
                        break          # requested 已涨但实际分配仍未涨 -> 已无可用空闲工厂，立即停手防叠灰格
                cur_assigned = nxt_assigned
                cur_req = nxt_req

        after = p.i32(line + ML_FACTORIES)
        req_after = p.i32(line + 0xE8) if has_req_field else after
        paused = None
        try:
            import session as _s
            paused = bool(_s.is_paused(self))
        except Exception:
            pass
        alloc = {}
        try:
            alloc = self.factory_allocation(c.tag)
            idle_fac = int(alloc["naval_idle"] if is_naval else alloc["military_idle"])
        except Exception:
            alloc = pre_alloc
            idle_fac = pre_idle
        # 2026-11-26 (P2-9): 直接从 CNavalProductionLine+ML_MAX_EFFICIENCY 读取本船坞线的
        # 真实单舰船坞硬上限（重巡/战列/航母=5，轻巡/驱逐/潜艇=10，运输船=15）。
        line_cap = None
        if is_naval:
            try:
                _lc = int(p.i32(line + ML_MAX_EFFICIENCY))
                if 1 <= _lc <= 25:
                    line_cap = _lc
            except Exception:
                line_cap = None
        note = ""
        if after != int(amount):
            extra_dock = ""
            if is_naval and alloc:
                extra_dock = (
                    " (effective_dockyards=%s, repair=%s, available_for_build=%s, "
                    "build_used=%s, refit_used=%s, idle=%s, raw_buildings=%s)"
                    % (alloc.get("naval_total_effective"), alloc.get("naval_repair_dockyards"),
                       alloc.get("naval_available"), alloc.get("naval_build_used"),
                       alloc.get("naval_refit_used"), alloc.get("naval_idle"),
                       alloc.get("naval_raw_buildings"))
                )
            cmd_consumed = (after != before) or (has_req_field and req_after != req_before)
            if (hit_line_cap or (line_cap is not None and after >= line_cap and int(amount) > line_cap)) and delta > 0 and is_naval:
                hit_line_cap = True
                note = (
                    "line %d hit its per-ship dockyard cap at %d (requested %d, line_cap=%s; "
                    "CAddProductionLineFactoriesCommand::CanExecute refuses new_req > GetMaxFactories() "
                    "— capital ships including heavy cruisers cap at 5, light cruisers/screens/subs at 10, convoys at 15). "
                    "Put the remaining idle dockyards (%s) on another naval line or open a new naval line%s"
                    % (line_index, after, int(amount), line_cap if line_cap is not None else after, idle_fac, extra_dock)
                )
            elif delta > 0 and (idle_fac == 0 or (after > before)):
                note = ("only reached %d of %d — not enough spare %s "
                        "(idle=%s%s; the engine only hands out effective free factories)"
                        % (after, int(amount), fac_kind, idle_fac, extra_dock))
            elif paused and (idle_fac is None or idle_fac > 0) and not cmd_consumed:
                note = ("the game is PAUSED and the command queue was not consumed "
                        "(idle=%s%s); unpause briefly or retry" % (idle_fac, extra_dock))
            else:
                note = ("only reached %d of %d — not enough spare %s%s "
                        "(the engine only hands out effective free factories, or line hit per-ship dockyard cap)"
                        % (after, int(amount), fac_kind, extra_dock))
        out = {"ok": after == int(amount), "line": line_index,
               "before": before, "after": after, "requested": int(amount),
               "idle_factories": idle_fac,
               "hit_line_cap": bool(hit_line_cap),
               "steps": steps, "readback": tray, "paused": paused, "note": note}
        if line_cap is not None:
            out["line_cap"] = line_cap
            out["reached_cap"] = bool(after >= line_cap)
        if has_req_field:
            out["requested_before"] = req_before
            out["requested_after"] = req_after
        if reclaimed_from:
            out["reclaimed_from"] = reclaimed_from
            out["reclaimed_total"] = sum(int(x["reclaimed"]) for x in reclaimed_from)
        if is_naval and alloc:
            out["dockyard_breakdown"] = {
                "effective": alloc.get("naval_total_effective"),
                "used_total": alloc.get("naval_used"),
                "build_used": alloc.get("naval_build_used"),
                "refit_used": alloc.get("naval_refit_used"),
                "repair_dockyards": alloc.get("naval_repair_dockyards"),
                "idle": alloc.get("naval_idle"),
            }
        # 2026-11-22 (复测 5 §8.4 时刻 ②): 模型在调整工厂数那一刻，若该线正在生产
        # 过时型号，直接在返回体中附带 obsolete / upgrade_to / upgrade_gain / suggested_action。
        try:
            _pls = self.production_lines(c.tag, include_obsolete=True)
            if 0 <= line_index < len(_pls):
                _row = _pls[line_index]
                out["equipment"] = _row.get("equipment")
                if _row.get("obsolete"):
                    out["obsolete"] = True
                    out["upgrade_to"] = _row.get("upgrade_to")
                    out["upgrade_gain"] = _row.get("upgrade_gain")
                    out["retool_cost"] = _row.get("retool_cost")
                    out["payback_days"] = _row.get("payback_days")
                    out["suggested_action"] = _row.get("suggested_action")
        except Exception:
            pass
        return out

    def _variant_for(self, equipment, tag=None):
        """(variant_ptr, handle) of an ACTIVE equipment variant we can produce."""
        p = self.p
        self._load_equipment()
        prod = p.u64(self._c(tag).ptr + CO_PRODUCTION)
        for v in _arr(p, prod, PS_VARIANTS_ACTIVE):
            if self._eq_by_id.get(p.i32(v + EV_TYPE_ID)) == equipment:
                return v, self.handle_of(v)
        return None, None

    def set_production_line_equipment(self, line_index: int, equipment: str, tag=None,
                                      dry_run=False):
        """SWAP an existing production line to another equipment variant (CA换装).

        2026-11-22 (复测 5 P2 §8.2): 修复两大缺陷：
          1) 旧实现在 Windows 上仍调用 1.19.2 Linux 的 `send_command("SetProductionLine",
             {"0x24": ..., "0x2C": ...})`，被 WIN_SAFE 静默拒绝导致永远换装失败；
             现通过 `CSetProductionLineCommand` 真实 PE 构造器（vt 0x2A0FF88,
             ctor 0x11D1550, size 0x38, +0x28 line handle, +0x30 variant handle）构建。
          2) `dry_run=True` 以前不调用 `CanExecute`，会发假绿灯；现在无论 dry_run
             真假都调用引擎自身的 `CSetProductionLineCommand::CanExecute (0x11F2C30)`，
             并把 `can_execute` 如实透出。
        """
        p = self.p
        c = self._c(tag)
        self._load_equipment()
        prod = p.u64(c.ptr + CO_PRODUCTION)
        lines = _arr(p, prod, PS_MIL_LINES)
        if not (0 <= line_index < len(lines)):
            return {"ok": False, "error": "no such line: %d (of %d)" % (line_index, len(lines))}
        line = lines[line_index]
        before_name = self._eq_by_id.get(p.i32(p.u64(line + ML_VARIANT) + EV_TYPE_ID))
        var, vh = self._variant_for(equipment, tag)
        if var is None:
            avail = sorted({self._eq_by_id.get(p.i32(v + EV_TYPE_ID))
                            for v in _arr(p, prod, PS_VARIANTS_ACTIVE)} - {None})
            return {"ok": False, "error": "no active variant for %s" % equipment,
                    "available": avail[:40]}
        lh = self.handle_of(line)
        if lh["type"] != 56:
            mil_lines = []
            for idx_i, ln_i in enumerate(lines):
                try:
                    lhi = self.handle_of(ln_i)
                    if lhi.get("type") == 56:
                        eq_i = self._eq_by_id.get(p.i32(p.u64(ln_i + ML_VARIANT) + EV_TYPE_ID))
                        mil_lines.append({"line": idx_i, "equipment": eq_i})
                except Exception:
                    continue
            is_naval = lh["type"] in (57, 71)
            kind_desc = "naval dockyard line (CNavalProductionLine)" if is_naval else "non-military production line"
            return {
                "ok": False,
                "blocked_by": "not_military_production_line",
                "line": line_index,
                "before": before_name,
                "requested": equipment,
                "line_handle": [lh["type"], lh["id"]],
                "is_naval_line": is_naval,
                "military_lines": mil_lines,
                "error": (
                    "line %d (%s) is a %s (handle type %d, expected military line handle 56); "
                    "cannot swap it to military/air equipment %s. To open a new line for %s, "
                    "call create_production_line(equipment=%r) instead (or pick a military line index from military_lines)."
                    % (line_index, before_name, kind_desc, lh["type"], equipment, equipment, equipment)
                ),
            }
        if vh["type"] != 70:
            return {"ok": False, "error": "expected equipment variant handle 70, got %d" % vh["type"]}
        packed_line = (lh["id"] << 32) | (lh["type"] & 0xFFFFFFFF)
        packed_var = (vh["id"] << 32) | (vh["type"] & 0xFFFFFFFF)
        out = {"line": line_index, "before": before_name, "requested": equipment,
               "line_handle": [lh["type"], lh["id"]],
               "variant_handle": [vh["type"], vh["id"]],
               "dry_run": bool(dry_run)}
        if before_name == equipment:
            out.update(ok=True, can_execute=True, after=before_name, note="already producing %s" % equipment)
            return out

        if sys.platform == "win32":
            import cmdwin
            cmd = None
            try:
                cmd = cmdwin.build_command(
                    self, "CSetProductionLineCommand",
                    {"line": packed_line, "variant": packed_var},
                )
                out["cmd"] = hex(cmd)
                can = bool(self.call(
                    p.base + cmdwin.CANEXEC_SET_PROD_LINE, (cmd, 0),
                    absolute=True, force=True,
                ) & 0xFF)
                out["can_execute"] = can
                if not can:
                    try:
                        allowed = bool(self.call(
                            p.base + self.PE_ADD_LINE_VARIANT_OK,
                            (var, cmd + 0x28), absolute=True, force=True,
                        ) & 0xFF)
                        out["variant_allowed"] = allowed
                    except Exception:
                        pass
                    try:
                        cmdwin.free(self, cmd)
                    except Exception:
                        pass
                    out.update(
                        ok=False,
                        after=before_name,
                        blocked_by="can_execute_refused",
                        error="CSetProductionLineCommand::CanExecute refused switching line %d (%s -> %s)"
                              % (line_index, before_name, equipment),
                    )
                    return out
                if dry_run:
                    try:
                        cmdwin.free(self, cmd)
                    except Exception:
                        pass
                    out.update(ok=True, after=before_name)
                    return out
                posted = cmdwin.post_drained(self, cmd, seconds=2.0)
                out["posted"] = posted
            except Exception as exc:
                if cmd:
                    try:
                        cmdwin.free(self, cmd)
                    except Exception:
                        pass
                out.update(ok=False, after=before_name, error="%s: %s" % (type(exc).__name__, exc))
                return out

            self._settle(0.25)
            after_name = self._eq_by_id.get(p.i32(p.u64(line + ML_VARIANT) + EV_TYPE_ID))
            out["after"] = after_name
            out["ok"] = (after_name == equipment)
            if out["ok"]:
                try:
                    import equipmeta as _em
                    _em.clear_obsolete_seen(pid=getattr(p, "pid", None), tag=c.tag, line_index=line_index)
                except Exception:
                    pass
            else:
                out["error"] = "CSetProductionLineCommand posted but line variant remained %s" % after_name
            return out
        raise NotImplementedError("set_production_line_equipment requires Windows PE 1.19.5.0")

    def create_production_line(self, equipment: str, tag=None, dry_run=False,
                               move_to_front: bool = False):
        """Create a brand-new production line for an equipment variant.

        This used to be a coin flip: the command's two handle slots were never
        identified, so the old code posted `(variant, 0)` and `(0, variant)` and
        hoped.  Neither is what the engine wants.  The real ctor is

            0x11CD270(this, int *countryTag, {u64} *variant, {u64} *mio, bool front)

        with the country tag at +0x28, the equipment variant handle at +0x2C and
        an optional military-industrial-organisation handle at +0x34.  Verified
        by disassembling CanExecute (which prints "Invalid Target" when +0x28 is
        <= 0, then resolves +0x2C and asks the variant whether the country may
        build it) and Execute (which reads +0x28, takes [CCountry+0xF68] -- the
        very CProductionStatus this module already reads -- and calls
        0x140ED0FF0(prod, variant) to find-or-create the line, then
        0x140ED6100(prod, line, 0) to move it to the top when +0x3C is set).

        2026-11-27 (D-01 根治): 默认 `move_to_front=False`，不再强制把新线插到 index 0
        导致既有产线行号整体 +1 漂移；同时在返回体回显最新行号映射并提示按 `equipment` 调厂。
        """
        import cmdwin
        p = self.p
        c = self._c(tag)
        self._load_equipment()
        prod = p.u64(c.ptr + CO_PRODUCTION)
        before_ptrs = _arr(p, prod, PS_MIL_LINES)
        before = len(before_ptrs)
        var, vh = self._variant_for(equipment, tag)
        if var is None:
            avail = sorted({self._eq_by_id.get(p.i32(v + EV_TYPE_ID))
                            for v in _arr(p, prod, PS_VARIANTS_ACTIVE)} - {None})
            return {"ok": False, "error": "no active variant for %s" % equipment,
                    "available": avail[:40]}
        if vh["type"] != 70:
            return {"ok": False,
                    "error": "expected equipment variant handle 70, got %d" % vh["type"]}
        # Is a line for this equipment already there?  Execute's find-or-create
        # would just hand back that same line, which reads as "nothing happened".
        for i, ln in enumerate(before_ptrs):
            if p.u64(ln + ML_VARIANT) == var:
                return {"ok": False, "already_exists": True, "line_index": i,
                        "before": before, "equipment": equipment,
                        "error": "a production line for %s already exists (line %d); "
                                 "change its factory count instead"
                                 % (equipment, i)}
        packed = (vh["id"] << 32) | (vh["type"] & 0xFFFFFFFF)
        want_front = 1 if move_to_front else 0
        out = {"before": before, "requested": equipment, "dry_run": bool(dry_run),
               "move_to_front": bool(move_to_front),
               "variant_handle": [vh["type"], vh["id"]], "tag_id": int(c.tag_id)}
        if dry_run:
            out.update(ok=True)
            return out
        cmd = cmdwin.build_command(self, "CAddProductionLineCommand",
                                   {"tag": int(c.tag_id),
                                    "variant_handle": packed,
                                    "mio_handle": 0,
                                    "move_to_front": want_front})
        out["cmd"] = hex(cmd)
        out["flag_0x3c"] = p.u8(cmd + 0x3C)
        if out["flag_0x3c"] != want_front:
            p.write(cmd + 0x3C, bytes([want_front]))
            out["flag_forced"] = True
            out["flag_0x3c"] = p.u8(cmd + 0x3C)
        can = self.call(p.base + self.PE_ADD_LINE_CANEXE, (cmd,),
                        absolute=True, force=True) & 0xFF
        out["can_execute"] = bool(can)
        if not can:
            # Narrow it down without guessing: the same predicate CanExecute uses.
            allowed = self.call(p.base + self.PE_ADD_LINE_VARIANT_OK,
                                (var, cmd + 0x28), absolute=True, force=True) & 0xFF
            out["variant_allowed_for_country"] = bool(allowed)
            out["error"] = ("CAddProductionLineCommand::CanExecute refused"
                            + ("" if allowed else
                               " -- %s is not producible by this country "
                               "(not researched / embargoed)" % equipment))
            return out
        def _new_line_appeared():
            try:
                cur_lines = _arr(p, p.u64(self._c(tag).ptr + CO_PRODUCTION), PS_MIL_LINES)
                return any(x not in before_ptrs for x in cur_lines)
            except Exception:
                return False

        # 2026-11-26 (P2-4 顺带根治): 暂停态下 cmdwin.post 不会消费命令队列，导致永远
        # 报 "no new line appeared"；统一改走 post_drained（暂停态先安全入队再短暂消费）。
        if hasattr(cmdwin, "post_drained"):
            try:
                posted = cmdwin.post_drained(self, cmd, seconds=2.0, done_fn=_new_line_appeared)
            except TypeError:
                posted = cmdwin.post_drained(self, cmd, seconds=2.0)
        else:
            posted = cmdwin.post(self, cmd)
        self._settle(0.15)
        after_lines = _arr(p, p.u64(self._c(tag).ptr + CO_PRODUCTION), PS_MIL_LINES)
        new = [x for x in after_lines if x not in before_ptrs]
        out["posted"] = posted
        out["after"] = len(after_lines)
        if not new:
            out["ok"] = False
            out["error"] = ("the command was accepted but no new line appeared -- "
                            "execute did not create one")
            return out
        line = new[0]
        name = self._eq_by_id.get(p.i32(p.u64(line + ML_VARIANT) + EV_TYPE_ID))
        fac_now = p.i32(line + ML_FACTORIES)
        req_now = fac_now
        if sys.platform == "win32":
            try:
                _rn = int(p.i32(line + 0xE8))
                if 0 <= _rn <= 200:
                    req_now = _rn
            except Exception:
                req_now = fac_now
            # D-28: 新建产线默认 requested_factories(+0xE8)=1，若有空闲工厂但 UpdateFactories
            # 尚未完成结算，短暂等待最多 2 次以便回执中的 factories 与引擎状态一致。
            if req_now > fac_now:
                for _w_new in range(2):
                    self._settle(0.18)
                    _fn2 = p.i32(line + ML_FACTORIES)
                    if _fn2 != fac_now:
                        fac_now = _fn2
                        break
        out.update(ok=name == equipment, equipment=name,
                   line_index=after_lines.index(line),
                   factories=fac_now,
                   requested_factories=req_now,
                   units=fac_now)
        if req_now > fac_now:
            out["pending_factories"] = req_now - fac_now
            out["pending_factories_note"] = (
                "new line defaults to requested_factories=%d (currently assigned %d); "
                "it will automatically claim %d factory as soon as military factories are freed"
                % (req_now, fac_now, req_now - fac_now)
            )
        if not out["ok"]:
            out["error"] = ("a line appeared but it is for %s, not %s"
                            % (name, equipment))
        else:
            out["index_shifted"] = bool(out["line_index"] == 0 and before > 0)
            out["hint"] = (
                "set_production supports equipment=%r (or line=%d + equipment=%r guard) "
                "to stay immune to line-index shifts" % (name, out["line_index"], name)
            )
            # 2026-11-22 (复测 5 §8.4 时刻 ②): 若新开的装备本身已有更新的已解锁型号，立刻提醒
            try:
                _pls = self.production_lines(c.tag, include_obsolete=True)
                out["lines_snapshot"] = [
                    {"line": r.get("index", idx_r), "equipment": r.get("equipment"), "factories": r.get("factories")}
                    for idx_r, r in enumerate(_pls or ())
                ]
                _li = out.get("line_index")
                if isinstance(_li, int) and 0 <= _li < len(_pls) and _pls[_li].get("obsolete"):
                    _row = _pls[_li]
                    out["obsolete"] = True
                    out["upgrade_to"] = _row.get("upgrade_to")
                    out["upgrade_gain"] = _row.get("upgrade_gain")
                    out["retool_cost"] = _row.get("retool_cost")
                    out["payback_days"] = _row.get("payback_days")
                    out["suggested_action"] = _row.get("suggested_action")
            except Exception:
                pass
        return out

    def remove_production_line(self, line_index: int, tag=None):
        """Remove a production line entirely.

        CRemoveProductionLineCommand (PE 1.19.5.0): vt 0x2A10048, size 0x30,
        ctor 0x11D0210 takes ONE argument -- an object carrying the line handle
        at +8 -- and stores it at +0x28.  The old call passed a country tag at
        +0x24 and an index at +0x2C (a stale 1.19.2 shape), and the command was
        never on the windows whitelist, so it was refused before it was even
        built and this function reported a bare `ok: false`.
        """
        p = self.p
        c = self._c(tag)
        self._load_equipment()
        prod = p.u64(c.ptr + CO_PRODUCTION)
        lines = _arr(p, prod, PS_MIL_LINES)
        if not (0 <= line_index < len(lines)):
            return {"ok": False, "error": "no such line: %d (of %d)"
                    % (line_index, len(lines))}
        line = lines[line_index]
        h = self.handle_of(line)
        # 56 = land/air production line, 57 = NAVAL production line. Both are valid.
        if h["type"] not in (56, 57):
            return {"ok": False, "error": "expected a production line handle (56/57), got %d"
                    % h["type"]}
        name = self._eq_by_id.get(p.i32(p.u64(line + ML_VARIANT) + EV_TYPE_ID))
        before = len(lines)
        packed = (h["id"] << 32) | (h["type"] & 0xFFFFFFFF)
        res = self.send_command("CRemoveProductionLineCommand",
                                {"line_handle": packed}, tag)
        self._settle(0.4)
        after = len(_arr(p, p.u64(self._c(tag).ptr + CO_PRODUCTION), PS_MIL_LINES))
        out = {"ok": after < before, "before": before, "after": after,
               "equipment": name, "line_handle": [h["type"], h["id"]],
               "run_result": (res or {}).get("run_result")}
        if not out["ok"]:
            out["error"] = (res or {}).get("error") or \
                "the game accepted the command but the line is still there"
        return out

    @read_scoped(
        "equipment_stockpile",
        key_fn=lambda self, tag=None, limit=120: (self._rs_tag_key(tag), int(limit)),
    )
    def equipment_stockpile(self, tag=None, limit=120):
        """Equipment counts in the stockpile, per VARIANT (not per equipment name).

        2026-11-21 (P2-11): the 2026-11-20 playtest read
            infantry_equipment_0: 975
            infantry_equipment_0: 188
            infantry_equipment_1: 24505 / 4435 / 100
        and could not tell whether those were separate variants or double
        counting -- `equipment` is the ARCHETYPE name, and one archetype owns one
        row per variant (captured / lend-leased / obsolete marks).  Keep one row
        per variant, name the variant pointer, and sum per archetype so "how many
        rifles do I have" has an answer.
        """
        p = self.p
        self._load_equipment()
        ps = p.u64(self._c(tag).ptr + CO_PRODUCTION)
        n = p.i32(ps + self.PS_STOCKPILE + O.PDXARRAY_SIZE)
        base = p.u64(ps + self.PS_STOCKPILE + O.PDXARRAY_DATA)
        out = []
        totals = {}
        if base and n:
            raw = p.try_read(base, 16 * n) or b""
            for i in range(min(n, len(raw) // 16, limit)):
                vp, cnt = struct.unpack_from("<qq", raw, i * 16)
                nm = self._eq_by_id.get(p.i32(vp + EV_TYPE_ID)) if vp else None
                val = round(cnt / O.FIXED, 1)
                out.append({"equipment": nm, "variant_ptr": hex(vp) if vp else None,
                            "count": val})
                if nm:
                    totals[nm] = round(totals.get(nm, 0.0) + val, 1)
        out.sort(key=lambda r: (str(r.get("equipment")), -float(r.get("count") or 0)))
        # variants that can be produced
        variants = []
        for v in _arr(p, ps, PS_VARIANTS_ACTIVE)[:limit]:
            nm = self._eq_by_id.get(p.i32(v + EV_TYPE_ID))
            if nm:
                variants.append({"equipment": nm, "variant_ptr": hex(v)})
        return {"stockpile": out, "totals_by_equipment": totals,
                "available_variants": variants,
                "note": ("one row per VARIANT; several rows can share an "
                         "`equipment` name (captured / lend-lease / obsolete). "
                         "Use totals_by_equipment for the real count.")}

    def resources(self, tag=None):
        """Strategic resources: production and available amount."""
        p = self.p
        r = p.u64(self._c(tag).ptr + CO_RESOURCES)
        if not r:
            return {}
        out = {}
        for i, nm in enumerate(RESOURCE_NAMES):
            a = r + RES_ARRAY + i * RES_STRIDE
            out[nm] = round(p.i64(a) / O.FIXED, 2)
        return out
