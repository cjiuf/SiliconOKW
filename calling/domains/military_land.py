"""陆军编制、新兵训练队列、师模板复制、集团军编组与陆战微操委托领域 Mixin。"""
from __future__ import annotations

import offsets as O
import legality
import struct
import sys

from ._shared import (
    CO_ARMIES,
    CO_CAPITAL_STATE,
    CO_DIV_TEMPLATES,
    CO_SUBUNITS,
    CO_THEATRES,
    DT_NAME,
    SU_CATEGORY,
    SU_KEY,
    _CONV_PROGRESS,
    _arr,
    item_name,
    read_scoped,
)

class MilitaryLandMixin:
    """陆军编制、新兵训练队列、师模板复制、集团军编组与陆战微操委托领域 Mixin。"""

    _VP_CACHE = {}

    PE_CREATE_CTOR   = 0x1C2B7D0
    PE_CREATE_VT     = 0x2A26EF8
    PE_CREATE_CANEXE = 0x1C2DBF0
    PE_CREATE_SIZE   = 0x290

    def armies(self, tag=None):
        p = self.p
        c = self._c(tag)
        out = []
        for i, a in enumerate(_arr(p, c.ptr, CO_ARMIES)):
            out.append({"index": i, "ptr": hex(a), "class": self.oi.class_of(a)})
        return {"count": len(out), "armies": out}

    def division_templates(self, tag=None):
        p = self.p
        c = self._c(tag)
        out = []
        for i, t in enumerate(_arr(p, c.ptr, CO_DIV_TEMPLATES)):
            out.append({"index": i, "name": item_name(p, t + DT_NAME), "ptr": hex(t)})
        return out

    def _template_handle(self, t_ptr):
        """Packed {type,id} handle of a CDivisionTemplate (it lives at +0x08)."""
        raw = self.p.read(t_ptr + 0x08, 8)
        t, i = struct.unpack("<ii", raw)
        return raw, t, i, (i << 32) | (t & 0xFFFFFFFF)

    _SPECIAL_FORCES_SUBUNITS = frozenset({
        "paratrooper", "mountaineers", "marine", "marine_commando",
        "penal_battalion", "rangers", "camelry",
    })
    _SPECIAL_FORCES_NAME_HINTS = ("paratroop", "fallschirm", "gebirg", "mountain", "marine")

    def _estimate_special_forces_cap(self, tag=None):
        """纯只读估算本国特种部队营数占用与上限（0 注入，用于诊断 0.0 卡死训练带，D-23）。"""
        try:
            import divreserve as _dr_sf
            co = self._c(tag)
            p = self.p
            tmpls = self.template_composition(co.tag) or []
            by_ptr = {}
            by_name = {}
            for t in tmpls:
                if not isinstance(t, dict):
                    continue
                line_map = t.get("line") or {}
                tot_bat = sum(int(v or 0) for v in line_map.values())
                sf_bat = sum(
                    int(v or 0) for k, v in line_map.items()
                    if str(k).lower() in self._SPECIAL_FORCES_SUBUNITS
                )
                rec = {
                    "name": t.get("name"),
                    "total_battalions": tot_bat,
                    "sf_battalions": sf_bat,
                    "is_special_forces": sf_bat > 0,
                }
                if t.get("ptr"):
                    by_ptr[str(t["ptr"]).lower()] = rec
                if t.get("name"):
                    by_name[str(t["name"]).lower()] = rec
            total_field_bat = 0
            sf_field_bat = 0
            for d_ptr in (_arr(p, co.ptr, CO_ARMIES) or ()):
                try:
                    ref_ptr = int(p.u64(int(d_ptr) + _dr_sf.CDIV_TEMPLATE) or 0)
                except Exception:
                    ref_ptr = 0
                rec = by_ptr.get(hex(ref_ptr).lower()) if ref_ptr else None
                if rec:
                    total_field_bat += rec["total_battalions"]
                    sf_field_bat += rec["sf_battalions"]
            # HOI4 基础规则：特种部队营上限 = max(24, floor(总营数 * 5%))
            est_cap = max(24, int(total_field_bat * 0.05))
            return {
                "total_field_battalions": total_field_bat,
                "special_forces_battalions": sf_field_bat,
                "estimated_cap": est_cap,
                "at_or_over_cap": sf_field_bat >= est_cap,
                "by_name": by_name,
            }
        except Exception:
            return None

    @read_scoped("conveyors", key_fn=lambda self, tag=None: (self._rs_tag_key(tag),))
    def conveyors(self, tag=None):
        """Live division-training conveyors.

        CCountry+0xF48 -> CDeploymentStatus, its CPdxArray<CMilitaryDeploymentConveyor*>
        at +0x68. Verified live 2026-11-04: after CreateConveyor the array went from
        0 to 1 and the element's class resolves to CMilitaryDeploymentConveyor.
        (An `oi.instances()` scan cannot be used here — the index is a snapshot taken
        before the conveyor existed.)

        2026-11-27 (D-22 / D-23 根治):
          * 实证 PE 1.19.5.0 中 `conv+0x48` 为 `series`（0=无限循环，>0=剩余批次），
            `CMilitaryDeployment+0x68` 为 `equipment_ratio`（fixed 1e5，如 83401 = 83.4%），
            `+0xB0` 为已配人力；
          * 补齐 `series`/`min_progress`/`equipment_ratio`/`stall_reason`，当特种部队训练带
            （如 Paratroopers）装备已齐但卡在 `progress=0.0` 时自动识别特种兵上限锁死。
        """
        p = self.p
        c = self._c(tag)
        out = []
        # Windows puts CDeploymentStatus at +0xF70 (offsets_win); the Linux table has
        # no name for it and has always used +0xF48. Reading the Linux offset on the PE
        # build returned a null pointer, so conveyors() was empty for ever and
        # train_divisions reported "no conveyor appeared" even when one had been made.
        dep_off = O.CO_DEPLOYMENT_STATUS
        conv_off = O.CO_DEPLOYMENT_CONVEYORS
        dep = p.u64(c.ptr + dep_off)
        if not dep:
            return out
        sf_info_cache = [False, None]  # [loaded, dict|None]

        def _get_sf_info():
            if not sf_info_cache[0]:
                sf_info_cache[0] = True
                sf_info_cache[1] = self._estimate_special_forces_cap(c.tag)
            return sf_info_cache[1]

        for conv in _arr(p, dep, conv_off):
            try:
                h_type, h_id = struct.unpack("<ii", p.read(conv + 0x08, 8))
            except Exception:
                continue
            row = {"ptr": hex(conv), "handle": [h_type, h_id],
                   "packed_handle": (h_id << 32) | (h_type & 0xFFFFFFFF)}
            # CMilitaryDeploymentConveyor.  The field map is build-specific:
            #   Linux 1.19.4.1: +0x18 template  +0x48 lines  +0x70 line array  +0x88 priority
            #   PE    1.19.5.0: +0x20 template  +0x48 series  +0x4C deploy province (int32)
            #                   +0x50 priority  +0x78 line array
            try:
                if sys.platform == "win32":
                    t_off, prio_off, arr_off = 0x20, 0x50, 0x78
                    row["deploy_province"] = p.i32(conv + 0x4C)
                    try:
                        _ser = int(p.i32(conv + 0x48))
                        if 0 <= _ser <= 1000:
                            row["series"] = _ser
                            row["infinite"] = (_ser == 0)
                    except Exception:
                        pass
                else:
                    t_off, prio_off, arr_off = 0x18, 0x88, 0x70
                row["priority"] = p.i32(conv + prio_off)
                row["template"] = item_name(p, p.u64(conv + t_off) + DT_NAME)
                # 2026-11-22 (复测 5 §3.10): CPdxArray 布局为 {data +0, cap +8, size +0x0C}；
                # 旧代码读 `arr_off + 8` 读成了容量 (cap=6) 而非实际长度 (size=5)，导致
                # `lines: 6` 与 `lines_detail` 5 条长期不一致。现改为读 PDXARRAY_SIZE (+0x0C)。
                _arr_cap = p.i32(conv + arr_off + 8)
                _arr_size = p.i32(conv + arr_off + O.PDXARRAY_SIZE)
                row["lines"] = _arr_size
                row["lines_capacity"] = _arr_cap
                _rows, _done, _best = [], 0, 0.0
                _eq_vals = []
                for _ln in _arr(p, conv, arr_off):
                    _md = p.u64(_ln + 0x30 if sys.platform == "win32" else _ln + 0x28)
                    if not _md or _md < 0x10000000000:
                        continue
                    _pr = p.u32(_md + 0x60) / 1e5
                    _lrow = {"line_ptr": hex(_ln), "progress": round(_pr, 3),
                             "done": _pr >= 0.999}
                    if sys.platform == "win32":
                        try:
                            _eq_raw = int(p.i64(_md + 0x68))
                            if 0 <= _eq_raw <= 105000:
                                _eq_r = round(_eq_raw / 1e5, 3)
                                _lrow["equipment_ratio"] = _eq_r
                                _eq_vals.append(_eq_r)
                        except Exception:
                            pass
                        try:
                            _mp_v = int(p.i32(_md + 0xB0))
                            if 0 <= _mp_v <= 100000:
                                _lrow["manpower"] = _mp_v
                        except Exception:
                            pass
                    try:
                        _lt, _li = struct.unpack("<ii", p.read(_ln + 0x08, 8))
                        if _lt > 0 and _li >= 0:
                            _lrow["handle"] = [_lt, _li]
                            _lrow["packed_handle"] = (_li << 32) | (_lt & 0xFFFFFFFF)
                    except Exception:
                        pass
                    _rows.append(_lrow)
                    _best = max(_best, _pr)
                    _done += 1 if _pr >= 0.999 else 0
                row["lines_detail"] = _rows
                if len(_rows) != _arr_size:
                    row["lines_mismatch"] = {"array_size": _arr_size, "detail_count": len(_rows)}
                row["progress"] = round(_best, 3)
                row["min_progress"] = round(min(r["progress"] for r in _rows), 3) if _rows else 0.0
                if _eq_vals:
                    row["equipment_ratio"] = round(sum(_eq_vals) / len(_eq_vals), 3)
                    row["min_equipment_ratio"] = round(min(_eq_vals), 3)
                row["lines_done"] = _done
                row["deployment_state"] = (
                    "no_location" if not row.get("deploy_province")
                    else "ready" if _rows and _done >= len(_rows)
                    else "training")
                # D-23: 对 progress==0.0 的训练带做零注入根因诊断（无部署地 / 特种兵上限 / 缺装备）
                if _rows and _best <= 0.001:
                    if not row.get("deploy_province"):
                        row["stall_reason"] = "no_deploy_location"
                        row["stall_detail"] = "conveyor has no deployment province set"
                    else:
                        _tpl_l = str(row.get("template") or "").lower()
                        _eq_avg = row.get("equipment_ratio")
                        _sf_i = _get_sf_info()
                        _tpl_sf = False
                        if _sf_i and _tpl_l in (_sf_i.get("by_name") or {}):
                            _tpl_sf = bool(_sf_i["by_name"][_tpl_l].get("is_special_forces"))
                        elif any(h in _tpl_l for h in self._SPECIAL_FORCES_NAME_HINTS):
                            _tpl_sf = True
                        if _eq_avg is not None and _eq_avg < 0.90:
                            row["stall_reason"] = "waiting_for_equipment"
                            row["stall_detail"] = (
                                "equipment fulfillment is %.1f%% (< 90%%); training cannot progress without equipment"
                                % (_eq_avg * 100.0)
                            )
                        elif _tpl_sf and (_sf_i is None or _sf_i.get("at_or_over_cap")):
                            row["stall_reason"] = "special_forces_cap_reached"
                            if _sf_i:
                                row["stall_detail"] = (
                                    "template %r is special forces and field special-forces battalions (%d) "
                                    "have reached/exceeded the estimated cap (%d of %d total field battalions); "
                                    "cancel these lines with cancel_training(conveyor=%r, cancel_all=true) or grow regular army first"
                                    % (
                                        row.get("template"),
                                        _sf_i["special_forces_battalions"],
                                        _sf_i["estimated_cap"],
                                        _sf_i["total_field_battalions"],
                                        row["ptr"],
                                    )
                                )
                            else:
                                row["stall_detail"] = (
                                    "template %r is special forces with 0.0 training progress; "
                                    "likely blocked by special-forces battalion cap" % row.get("template")
                                )
                        else:
                            row["stall_reason"] = "stalled_zero_progress"
                            row["stall_detail"] = (
                                "progress is 0.0 (equipment_ratio=%s, deploy_province=%s); "
                                "check equipment stockpile, manpower, or special-forces cap"
                                % (_eq_avg, row.get("deploy_province"))
                            )
                _prev = _CONV_PROGRESS.get((p.pid, conv))
                if _prev is not None and _best + 0.01 < _prev and _best < 0.25:
                    row["new_batch"] = True
                    row["previous_progress"] = _prev
                _CONV_PROGRESS[(p.pid, conv)] = _best
            except Exception:
                pass
            out.append(row)
        return out

    def deployment_province(self, state_id=None, tag=None) -> int:
        """A province inside `state_id` that can host a deployment (VP province first).

        CSetConveyorLocationCommand does NOT take a state id: its Execute bounds-checks
        the value against the game's PROVINCE count (13414), and a state id such as 64
        is refused while Berlin's province 6521 is accepted. That single wrong unit is
        why every conveyor stayed at "No location set" and 100 %-trained divisions
        never appeared on the map.
        """
        import mapdata
        st = int(state_id or self._homeland_state_id(tag))
        key = (mapdata.GAME, st)
        if key in self._VP_CACHE:
            return self._VP_CACHE[key]
        import glob, os as _os, re
        provs = sorted((mapdata._load_state_provinces_raw()[0] or {}).get(st) or [])
        best = None
        for fn in glob.glob(_os.path.join(mapdata.GAME, "history", "states", "%d-*.txt" % st)):
            try:
                txt = open(fn, encoding="utf-8", errors="replace").read()
            except OSError:
                continue
            m = re.search(r"victory_points\s*=\s*\{([^}]*)\}", txt)
            if not m:
                continue
            pairs = []
            for line in m.group(1).splitlines():
                line = line.split("#")[0].strip()
                parts = line.split()
                if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
                    pairs.append((int(parts[1]), int(parts[0])))
            if pairs:
                best = max(pairs)[1]
        if best is None:
            best = provs[0] if provs else 6521
        self._VP_CACHE[key] = best
        return best

    def _homeland_state_id(self, tag=None) -> int:
        """A deployment state that is connected to the capital by land.

        HOI4 refuses to deploy a finished division into a state that has no land
        connection to the capital: East Prussia (state 5 / 763, behind the Polish
        corridor in 1936) is the classic trap — the training conveyor fills to 100 %
        and nothing ever appears on the map. `CCountry+0xFF0` is the capital STATE
        id, so the capital's own state is always the safe answer. The hard-coded
        German list is only a fallback for the case where the capital is occupied.
        """
        p = self.p
        c = self._c(tag)
        try:
            own = {int(s["id"]) for s in self.states(c.tag)}
        except Exception:
            own = set()
        cap = 0
        if CO_CAPITAL_STATE is not None:
            try:
                cap = p.i32(c.ptr + CO_CAPITAL_STATE)
            except Exception:
                cap = 0
        if cap and (not own or cap in own):
            return cap
        if c.tag == "GER":
            # Brandenburg 64 = Berlin; then the other core states west of the corridor.
            for pref in (64, 59, 60, 52, 51, 55, 42, 57, 65):
                if pref in own:
                    return pref
        # last resort: an owned state that is not one of the two East Prussian ids
        rest = sorted(x for x in own if x not in (5, 763))
        if rest:
            return rest[0]
        return 64

    def train_divisions(self, template_name=None, lines=2, state_id=None, tag=None,
                        conveyor=None, series=0):
        """Open a division-training conveyor and push its first line onto the map.

        If `conveyor` (hex pointer or packed handle of an existing conveyor from
        `hoi4_conveyors`) is given, adds `lines` parallel training lines to THAT
        existing conveyor instead of creating a brand-new conveyor (复测 5 §3.11).

        `series`: number of sequential batches to train per line (0 = infinite loop,
        1 = train a single batch then automatically close the conveyor; D-12).

        Three things killed the 1936 run here (2 conveyors x 5 lines, 17 months,
        0 divisions):
          * the deployment state was `states("GER")[0]` = East Prussia, an exclave
            behind the Polish corridor, so a finished division could never deploy;
          * 10 parallel lines competed for ~26 rifles/day, so no single division ever
            reached 100 % equipment;
          * the conveyor ran at normal equipment priority.
        The defaults are therefore: capital state, 2 lines, priority 2 (highest),
        unlimited series. Raise `lines` only when the rifle stockpile is large.
        """
        p = self.p
        c = self._c(tag)
        from commands import COMMANDS as _CMDS
        # 2026-11-22 (复测 5 §3.11): 支持指定已有传送带指针直接加线，不再被迫新建传送带
        if conveyor is not None and str(conveyor).strip():
            want = str(conveyor).strip().lower()
            existing = self.conveyors(c.tag)
            hit = None
            for cv in existing:
                if (str(cv.get("ptr") or "").lower() == want
                        or str(cv.get("packed_handle") or "") == want
                        or hex(int(cv.get("packed_handle") or 0)).lower() == want):
                    hit = cv
                    break
            if hit is None:
                return {"ok": False, "error": "no existing conveyor matches %r" % conveyor,
                        "conveyors": [x.get("ptr") for x in existing]}
            add_n = max(1, int(lines or 1))
            lines_before = int(hit.get("lines") or 0)
            with self:
                rv = self._run_cmd(self.cmd.make("AddConveyorLine",
                                                 conveyor_handle=hit["packed_handle"],
                                                 count=add_n))
            self._settle(0.4)
            after_rows = [x for x in self.conveyors(c.tag) if x.get("ptr") == hit.get("ptr")]
            lines_after = int(after_rows[0].get("lines") or lines_before) if after_rows else lines_before
            return {
                "ok": bool(lines_after > lines_before or (rv & 0xFF)),
                "mode": "add_lines_to_existing",
                "conveyor": hit.get("ptr"),
                "conveyor_handle": hit.get("handle"),
                "template": hit.get("template"),
                "added_requested": add_n,
                "lines_before": lines_before,
                "lines_after": lines_after,
                "error": getattr(self, "_last_cmd_error", "") if lines_after <= lines_before else "",
            }
        tmpls = self.division_templates(c.tag)
        if not tmpls:
            return {"ok": False, "error": "no division templates found"}
        pick = None
        for t in tmpls:
            if template_name and (t.get("name") == template_name
                                  or str(t.get("index")) == str(template_name)):
                pick = t
                break
        if not pick:
            pick = next((t for t in tmpls if "Infanterie" in (t.get("name") or "")
                         or "Infantry" in (t.get("name") or "")), tmpls[0])
        t_ptr = int(pick["ptr"], 16)
        _, h_type, h_id, packed = self._template_handle(t_ptr)
        if h_type <= 0:
            return {"ok": False, "error": "template handle looks invalid: %d" % h_type}
        before = {x["ptr"] for x in self.conveyors(c.tag)}
        with self:
            cmd = self.cmd.make("CreateConveyor", template_handle=packed, priority=2)
            rv = self._run_cmd(cmd)
        self._settle(0.6)
        new = [x for x in self.conveyors(c.tag) if x["ptr"] not in before]
        if not new:
            return {"ok": False, "run": bool(rv & 0xFF), "template": pick.get("name"),
                    "template_handle": [h_type, h_id],
                    "error": getattr(self, "_last_cmd_error", "") or "no conveyor appeared"}
        conv = new[0]
        ch = conv["packed_handle"]
        if state_id is None or int(state_id) in (5, 763):
            state_id = self._homeland_state_id(c.tag)
        prov = self.deployment_province(state_id, tag=c.tag)
        errors = []
        with self:
            rv_loc = self._run_cmd(self.cmd.make("SetConveyorLocation",
                                                 conveyor_handle=ch, province_id=int(prov)))
            if not (rv_loc & 0xFF):
                errors.append("location: %s" % (getattr(self, "_last_cmd_error", "") or "refused"))
            if "SetConveyorPriority" in _CMDS:
                rv_pri = self._run_cmd(self.cmd.make("SetConveyorPriority",
                                                     conveyor_handle=ch, priority=2))
                if not (rv_pri & 0xFF):
                    errors.append("priority: %s" % (getattr(self, "_last_cmd_error", "") or "refused"))
            if "SetConveyorSeries" in _CMDS:
                self._run_cmd(self.cmd.make("SetConveyorSeries",
                                            conveyor_handle=ch, series=max(0, int(series or 0))))
            if int(lines) > 1:
                self._run_cmd(self.cmd.make("AddConveyorLine",
                                            conveyor_handle=ch, count=int(lines) - 1))
        self._settle(0.4)
        ser_val = max(0, int(series or 0))
        return {"ok": True, "template": pick.get("name"), "template_handle": [h_type, h_id],
                "conveyor": conv["ptr"], "conveyor_handle": conv["handle"],
                "lines": int(lines), "series": ser_val, "infinite": ser_val == 0,
                "series_note": (
                    "series=0 trains indefinitely until cancelled; pass series=1 to stop automatically after 1 batch"
                    if ser_val == 0 else "conveyor will train %d batch(es) then stop" % ser_val
                ),
                "state_id": int(state_id),
                "deploy_province": int(prov),
                "errors": errors,
                "deploy_error": getattr(self, "_last_cmd_error", "")}

    def cancel_training(self, lines=None, conveyor=None, template=None,
                        cancel_all=False, tag=None, dry_run=False):
        """缩减或取消训练传送带排线（P1-3 / D-07 根治，解除 DEFICIT_OVER_TRAINING 死锁）。

        2026-11-27 经 1.19.5.0 PE ctor 反汇编 + MSVC RTTI 实证：
          * `CRemoveConveyorLineCommand` (vt 0x2AC0850, ctor 0x1C2F020, size 0x30,
            CanExecute 0x1C31940, Execute 0x1C30D30) 接收单条训练线（type 63）的 handle；
          * `CRemoveConveyorCommand` (vt 0x2AC0610, ctor 0x1C2EF50, size 0x30,
            CanExecute 0x1C316E0, Execute 0x1C30CB0) 接收整条传送带（type 64）的 handle。
        优先从训练进度最低的排线开始削减，最大程度保留即将训完的部队；若某传送带全部
        排线都要取消（或 `cancel_all=True`），直接整条删除传送带。
        """
        c = self._c(tag)
        convs = self.conveyors(c.tag) or []
        if not convs:
            return {
                "ok": False,
                "blocked_by": "no_training_conveyors",
                "error": "no active division-training conveyors to cancel",
                "lines_before": 0,
                "lines_after": 0,
            }

        # 过滤目标传送带
        matched = list(convs)
        if conveyor is not None and str(conveyor).strip():
            want = str(conveyor).strip().lower()
            matched = [
                cv for cv in matched
                if (str(cv.get("ptr") or "").lower() == want
                    or str(cv.get("packed_handle") or "") == want
                    or hex(int(cv.get("packed_handle") or 0)).lower() == want)
            ]
        if template is not None and str(template).strip():
            tw = str(template).strip().lower()
            matched = [
                cv for cv in matched
                if tw in str(cv.get("template") or "").lower()
            ]
        if not matched:
            return {
                "ok": False,
                "blocked_by": "no_matching_conveyor",
                "error": "no training conveyor matched filter (conveyor=%r, template=%r)" % (conveyor, template),
                "conveyors": [
                    {"ptr": cv.get("ptr"), "template": cv.get("template"), "lines": cv.get("lines")}
                    for cv in convs
                ],
            }

        total_before = sum(max(0, int(cv.get("lines") or 0)) for cv in convs)
        matched_lines = sum(max(0, int(cv.get("lines") or 0)) for cv in matched)
        if cancel_all:
            want_remove = matched_lines
        elif lines is None:
            # 默认至少减 1 条（若只有 1 条则删整条传送带）
            want_remove = 1
        else:
            want_remove = max(1, int(lines))
        want_remove = min(want_remove, matched_lines)

        # 生成削减计划：先按单条线进度升序选最不值钱的线；若某传送带所有线都被选中，合并为整带删除
        flat_lines = []
        for cv in matched:
            details = list(cv.get("lines_detail") or ())
            for idx_l, ld in enumerate(details):
                flat_lines.append({
                    "conveyor": cv.get("ptr"),
                    "conveyor_packed_handle": cv.get("packed_handle"),
                    "conveyor_lines": int(cv.get("lines") or len(details)),
                    "template": cv.get("template"),
                    "line_ptr": ld.get("line_ptr"),
                    "line_handle": ld.get("handle"),
                    "line_packed_handle": ld.get("packed_handle"),
                    "progress": float(ld.get("progress") or 0.0),
                    "idx_in_conveyor": idx_l,
                })
        # 优先删进度最低的线（相同进度按倒序从尾部删，最稳妥）
        flat_lines.sort(key=lambda x: (x["progress"], -x["idx_in_conveyor"]))
        chosen = flat_lines[:want_remove]

        by_conv_chosen = {}
        for item in chosen:
            by_conv_chosen.setdefault(item["conveyor"], []).append(item)

        plan_ops = []
        for cv in matched:
            cptr = cv.get("ptr")
            picks = by_conv_chosen.get(cptr) or []
            if not picks and not cancel_all:
                continue
            cv_total = int(cv.get("lines") or len(cv.get("lines_detail") or ()))
            if cancel_all or len(picks) >= cv_total:
                plan_ops.append({
                    "kind": "remove_conveyor",
                    "conveyor": cptr,
                    "template": cv.get("template"),
                    "packed_handle": cv.get("packed_handle"),
                    "lines_removed": cv_total,
                })
            else:
                for it in picks:
                    plan_ops.append({
                        "kind": "remove_line",
                        "conveyor": cptr,
                        "template": cv.get("template"),
                        "line_ptr": it.get("line_ptr"),
                        "packed_handle": it.get("line_packed_handle"),
                        "progress": it.get("progress"),
                        "lines_removed": 1,
                    })

        if dry_run:
            return {
                "ok": True,
                "dry_run": True,
                "lines_before": total_before,
                "planned_remove": sum(x["lines_removed"] for x in plan_ops),
                "projected_lines_after": max(0, total_before - sum(x["lines_removed"] for x in plan_ops)),
                "operations": plan_ops,
            }

        op_results = []
        for op_rec in plan_ops:
            ph = op_rec.get("packed_handle")
            if not ph:
                op_results.append({**op_rec, "ok": False, "error": "missing handle for conveyor/line"})
                continue
            cmd_name = (
                "CRemoveConveyorCommand" if op_rec["kind"] == "remove_conveyor"
                else "CRemoveConveyorLineCommand"
            )
            field_key = "conveyor_handle" if op_rec["kind"] == "remove_conveyor" else "line_handle"
            step_before = sum(max(0, int(cv.get("lines") or 0)) for cv in (self.conveyors(c.tag) or []))
            if sys.platform == "win32":
                import cmdwin
                try:
                    cmd_ptr = cmdwin.build_command(self, cmd_name, {field_key: int(ph)})
                    can_rva = (
                        cmdwin.CANEXEC_REMOVE_CONVEYOR if op_rec["kind"] == "remove_conveyor"
                        else cmdwin.CANEXEC_REMOVE_CONVEYOR_LINE
                    )
                    can_ok = cmdwin.can_exec(self, cmd_ptr, can_rva)
                    if not can_ok:
                        op_results.append({
                            **op_rec, "ok": False, "can_execute": False,
                            "error": "%s::CanExecute refused" % cmd_name,
                        })
                        continue
                    def _lines_dropped(_b=step_before):
                        try:
                            cur_n = sum(max(0, int(x.get("lines") or 0)) for x in (self.conveyors(c.tag) or []))
                            return cur_n < _b
                        except Exception:
                            return False
                    try:
                        pr = cmdwin.post_drained(self, cmd_ptr, seconds=2.0, done_fn=_lines_dropped)
                    except TypeError:
                        pr = cmdwin.post_drained(self, cmd_ptr, seconds=2.0)
                    self._settle(0.15)
                    step_after = sum(max(0, int(cv.get("lines") or 0)) for cv in (self.conveyors(c.tag) or []))
                    op_results.append({
                        **op_rec,
                        "ok": step_after < step_before,
                        "can_execute": True,
                        "posted": pr.get("posted"),
                        "drained": pr.get("drained"),
                        "lines_before": step_before,
                        "lines_after": step_after,
                    })
                except Exception as exc:
                    op_results.append({**op_rec, "ok": False, "error": "%s: %s" % (type(exc).__name__, exc)})
            else:
                short_cmd = "RemoveConveyor" if op_rec["kind"] == "remove_conveyor" else "RemoveConveyorLine"
                with self:
                    rv = self._run_cmd(self.cmd.make(short_cmd, **{field_key: int(ph)}))
                self._settle(0.3)
                step_after = sum(max(0, int(cv.get("lines") or 0)) for cv in (self.conveyors(c.tag) or []))
                op_results.append({
                    **op_rec,
                    "ok": bool(step_after < step_before or (rv & 0xFF)),
                    "lines_before": step_before,
                    "lines_after": step_after,
                })

        total_after = sum(max(0, int(cv.get("lines") or 0)) for cv in (self.conveyors(c.tag) or []))
        removed_actual = max(0, total_before - total_after)
        return {
            "ok": removed_actual > 0,
            "lines_before": total_before,
            "lines_after": total_after,
            "removed": removed_actual,
            "requested": want_remove,
            "operations": op_results,
            "error": (None if removed_actual > 0 else "no training lines were removed"),
        }

    def deploy_ready_conveyors(self, tag=None, min_progress=0.995, require_all_lines=True):
        """Push a conveyor's divisions onto the map (the in-game deploy button).

        2026-11-27 (D-11 / D-22 / D-23):
          * 默认进度门槛为 `min_progress=0.995`（仅部署已满训的传送带）；
          * 当 `require_all_lines=True`（默认）且 `min_progress >= 0.95` 时，校验该带最慢的线
            （`min_progress`）也达标，防止 `DeployConveyor` 把同带未满训的线一起强推出去；
          * `attempts` 透出每条传送带的 `template`、`lines`、`series`、`min_progress`、
            `max_progress`、`equipment_ratio`、`stall_reason`、`new_batch`（上一批自动部署后
            新批次从 0 起的清晰标注）。
        """
        def _divs():
            try:
                return int(self.divisions(tag, limit=1)["count"])
            except Exception:
                return None
        before = _divs()
        deployed, attempts = 0, []
        for conv in self.conveyors(tag):
            prog_max = conv.get("progress")
            prog_min = conv.get("min_progress")
            if prog_max is None:
                try:
                    _, prog_max = self._conveyor_progress(conv)
                except Exception:
                    prog_max = None
            if prog_min is None:
                prog_min = prog_max
            base_meta = {
                "conveyor": conv["ptr"],
                "template": conv.get("template"),
                "lines": conv.get("lines"),
                "series": conv.get("series"),
                "progress": prog_max,
                "min_progress": prog_min,
            }
            if conv.get("equipment_ratio") is not None:
                base_meta["equipment_ratio"] = conv.get("equipment_ratio")
            if conv.get("stall_reason"):
                base_meta["stall_reason"] = conv.get("stall_reason")
                if conv.get("stall_detail"):
                    base_meta["stall_detail"] = conv.get("stall_detail")
            if conv.get("new_batch"):
                base_meta["new_batch"] = True
                base_meta["previous_progress"] = conv.get("previous_progress")

            if min_progress > 0:
                check_val = (
                    prog_min
                    if (require_all_lines and min_progress >= 0.95 and prog_min is not None)
                    else prog_max
                )
                if check_val is None or check_val < min_progress:
                    sk = dict(base_meta, skipped=True, need=min_progress)
                    if (prog_max is not None and prog_min is not None
                            and prog_max >= min_progress > prog_min):
                        sk["partial_lines_ready"] = True
                        sk["skip_reason"] = (
                            "DeployConveyor pushes ALL lines in the conveyor; slowest line is %.3f < %.3f "
                            "(fastest line %.3f). Wait for all lines or pass require_all_lines=false."
                            % (prog_min, min_progress, prog_max)
                        )
                    attempts.append(sk)
                    continue
            with self:
                rv = self._run_cmd(self.cmd.make("DeployConveyor",
                                                 conveyor_handle=conv["packed_handle"]))
            attempts.append(dict(
                base_meta,
                run=bool(rv & 0xFF),
                error=getattr(self, "_last_cmd_error", ""),
            ))
            if rv & 0xFF:
                deployed += 1
        if deployed:
            self._settle(0.5)
        after = _divs()
        delta = (after - before) if (before is not None and after is not None) else None
        skipped_n = sum(1 for x in attempts if x.get("skipped"))
        out = {"conveyors": len(attempts), "conveyors_deployed": deployed,
               "skipped_below_threshold": skipped_n,
               "min_progress_used": float(min_progress),
               "require_all_lines": bool(require_all_lines),
               "deployed": delta, "divisions_before": before, "divisions_after": after,
               "attempts": attempts,
               "note": ("DeployConveyor pushes the WHOLE conveyor (all of its lines), "
                        "not a single finished division; default min_progress=0.995 with "
                        "require_all_lines=true only deploys fully finished batches. "
                        "Note: series=0 (infinite) or series>1 conveyors automatically start the "
                        "next batch near progress=0.0 immediately after a finished batch deploys.")}
        if deployed and delta is not None and delta <= 0:
            out["warning"] = ("the command was accepted but no division appeared -- the "
                              "conveyor may still be waiting for equipment")
        return out

    def _pe_create_template_cost(self, cmd):
        """Army XP the engine will charge for this create command.

        0x1C2BD70(cmd, &out) writes the cost into out; CanExecute then does
        `if (army_xp < cost) return false`.  Measured 2026-11-18 on a fresh
        1936 save: Infanterie-Division 8,000,000 / Gebirgs-Brigade 3,000,000 /
        Kavallerie-Brigade 3,000,000 — i.e. 80 / 30 / 30 army XP at 100,000 per
        XP.  It is a COST, not a count, which is exactly why CanExecute looked
        like it "always returns 0": every previous test ran with 0 army XP.
        """
        import cmdwin
        p = self.p
        buf = cmdwin.alloc(self, 0x40)
        try:
            p.write(buf, b"\0" * 0x40)
            self.call(p.base + 0x1C2BD70, (cmd, buf), absolute=True, force=True)
            return int(p.u64(buf))
        finally:
            try:
                cmdwin.free(self, buf)
            except Exception:
                pass

    def _pe_army_xp(self, tag=None):
        """Country army XP in the engine's 1/100000 display units."""
        import templedit
        c = self._c(tag)
        return int(templedit._pe_army_xp(self, int(c.tag_id)))

    def _pe_duplicate_template(self, source_name, tag=None, new_name=None, dry_run=False):
        """Create a new template from an existing one on the PE build.

        2026-11-23 (D-6): 支持 dry_run=True 零消耗询价（实扣 XP 随源模板营数/支援连动态变化），
        并补齐 cmdwin.attach_fake_vtable 防止队列释放命令对象时 CRT free 崩进程。
        """
        import cmdwin
        p = self.p
        c = self._c(tag)
        rows = self.division_templates(c.tag)
        src_ref = None
        for i, r in enumerate(rows):
            if str(source_name) in (str(i), str(r.get("name"))):
                src_ref = int(r["ptr"], 16)
                break
        if src_ref is None:
            return {"ok": False, "error": "template not found: %s" % source_name,
                    "have": [r.get("name") for r in rows]}
        before = len(rows)
        target = new_name or (source_name + " Copy")
        cmd = cmdwin.alloc(self, self.PE_CREATE_SIZE)
        p.write(cmd, b"\0" * self.PE_CREATE_SIZE)
        tagbuf = cmdwin.alloc(self, 0x10)
        p.write(tagbuf, struct.pack("<i", int(c.tag_id)) + b"\0" * 12)
        try:
            self.call(p.base + self.PE_CREATE_CTOR,
                      (cmd, src_ref + 0x18, 0, tagbuf, 0),
                      absolute=True, force=True)
        except Exception as e:
            try:
                cmdwin.free(self, tagbuf)
            except Exception:
                pass
            return {"ok": False, "stage": "ctor", "cmd": hex(cmd),
                    "error": "%s: %s" % (type(e).__name__, e)}
        try:
            cmdwin.free(self, tagbuf)
        except Exception:
            pass
        if p.u64(cmd) != p.base + self.PE_CREATE_VT:
            return {"ok": False, "stage": "ctor", "cmd": hex(cmd),
                    "error": "ctor wrote vptr %#x, expected %#x"
                             % (p.u64(cmd), p.base + self.PE_CREATE_VT)}
        # rename the embedded copy (MSVC std::string: 16-byte SSO/ptr union at +0,
        # size at +0x10, capacity at +0x18 -> 32 bytes total)
        na = cmd + 0x28 + 0x08
        nb = target.encode("utf-8")
        if len(nb) <= 15:
            p.write(na, nb.ljust(16, b"\0") + struct.pack("<QQ", len(nb), 15))
        else:
            sb = cmdwin.alloc(self, len(nb) + 1)
            p.write(sb, nb + b"\0")
            p.write(na, struct.pack("<QQQQ", sb, 0, len(nb), len(nb)))
        cost = self._pe_create_template_cost(cmd)
        xp = self._pe_army_xp(c.tag)
        can = bool(self.call(p.base + self.PE_CREATE_CANEXE, (cmd, 0),
                             absolute=True, force=True) & 0xFF)
        res = {"ok": False, "mode": "pe-create-division-template",
               "cmd": hex(cmd), "source": source_name, "new_name": target,
               "dry_run": bool(dry_run),
               "can_execute": can, "before": before,
               "xp_cost": cost, "xp_cost_display": round(cost / 100000.0, 2),
               "army_xp": xp, "army_xp_display": round(xp / 100000.0, 2),
               "xp_after_estimate": round(max(0.0, (xp - cost) / 100000.0), 2),
               "hint": (
                   "XP 费用由源模板当前营与支援连数量动态决定；如需在复制后改编，"
                   "可先对源模板调 hoi4_template_edit(template=%r, ..., dry_run=true) 预估改编差价，"
                   "确保总 XP 足够再执行。" % source_name
               )}
        if not can:
            if cost > xp:
                # The whole "CanExecute always returns 0" mystery, solved
                # 2026-11-18: this is an ordinary resource gate, not a wall.
                res["blocked_by"] = "army_xp"
                res["error"] = ("creating this template costs %.1f army XP, "
                                "you only have %.1f"
                                % (cost / 100000.0, xp / 100000.0))
                res["hint"] = ("army XP comes from national focuses, an army "
                               "chief with experience_gain_army, and combat — "
                               "hoi4_thresholds shows the pool")
            else:
                res["error"] = ("CCreateDivisionTemplateCommand::CanExecute "
                                "refused although the XP cost is covered "
                                "(%.1f <= %.1f) — unexpected, report it"
                                % (cost / 100000.0, xp / 100000.0))
            res["after"] = len(self.division_templates(c.tag))
            return res
        if dry_run:
            res["ok"] = True
            res["note"] = "dry_run: validated and quoted XP cost only; no command posted"
            return res
        cmdwin.attach_fake_vtable(self, cmd, self.PE_CREATE_VT)
        posted = cmdwin.post_drained(self, cmd, seconds=2.0)
        self._settle(0.3)
        after = self.division_templates(c.tag)
        res.update({"posted": posted, "after": len(after),
                    "templates": [r.get("name") for r in after],
                    "xp_after": self._pe_army_xp(c.tag),
                    "ok": len(after) > before})
        if not res["ok"]:
            res["error"] = "no new template appeared after posting"
        return res

    def duplicate_division_template(self, source_name, tag=None, new_name=None, dry_run=False):
        """Task 2 (2026-11-02 work order): borrow/copy an existing CDivisionTemplate.

        CCreateDivisionTemplateCommand ctor 0x1FDC190 deep-copies the source template
        (embedded at CReferencedDivisionTemplate+0x18) into the command and posts it
        through CCommand::Run -- the exact path the UI 'Duplicate' button uses.
        """
        # 2026-11-17: KILLED A LIVE GAME. The comment below (2026-11-10) said this
        # path was "reverted / stays disabled" but the code was still reachable, so
        # a real campaign called it and hoi4.exe died inside CCreateDivisionTemplateCommand
        # ctor (0x1FDC190) -- process vanished, no crash folder, ReadProcessMemory
        # returned 299 (ERROR_PARTIAL_COPY) inside self.call(). Everything after that
        # call in the session read "hoi4 process not found".
        # Hard-refuse on the PE build until the CDivisionTemplate /
        # CReferencedDivisionTemplate layering is actually verified by disassembly.
        return self._pe_duplicate_template(source_name, tag, new_name, dry_run=dry_run)

    def template_composition(self, tag=None, only=None):
        """Division templates with their real battalion/company makeup."""
        p = self.p
        c = self._c(tag)
        out = []
        # 2026-11-16: the old key_at() probed every 8-byte slot for a
        # libstdc++-shaped std::string. On the PE build CSubUnitDefinition uses
        # an MSVC std::string (SSO buffer +0x18 / size +0x28), so every name
        # came back empty. templedit.subunit_name() now dispatches by platform
        # and handles TNullObject placeholder slots (size 0).
        import templedit

        def counts(tp, arr_off):
            hdr = p.try_read(tp + arr_off, 16)
            if not hdr:
                return {}
            data, cap, size = struct.unpack("<Qii", hdr)
            acc = {}
            for k in range(min(size, 60)):
                e = p.try_read(data + 8 * k, 8)
                if not e:
                    break
                ep = struct.unpack("<Q", e)[0]
                kk = templedit.subunit_name(p, ep)
                if not kk:
                    continue          # TNullObject / empty column slot
                acc[kk] = acc.get(kk, 0) + 1
            return acc

        for i, t in enumerate(_arr(p, c.ptr, CO_DIV_TEMPLATES)):
            name = item_name(p, t + DT_NAME)
            if only is not None and str(only) not in (str(i), str(name)):
                continue
            out.append({
                "index": i,
                "name": name,
                "line": counts(t, 0x68),
                "support": counts(t, 0x88),
                "ptr": hex(t),
            })
        return out

    def subunit_types(self, tag=None, filter_text=""):
        """Battalion types, used when building a division template.

        2026-11-10: CCountry+0x26C0 (CO_SUBUNITS) is an EMPTY array in 1.19.4.1 —
        this tool returned count 0 for every country. Fall back to the live
        CSubUnitDefinition pool (vtable scan anchored on a real template).
        """
        p = self.p
        c = self._c(tag)
        out = []
        for s in _arr(p, c.ptr, CO_SUBUNITS):
            k = item_name(p, s + SU_KEY)
            if k and (not filter_text or filter_text.lower() in k.lower()):
                out.append({"key": k, "category": item_name(p, s + SU_CATEGORY), "ptr": hex(s)})
        if not out:
            try:
                import templedit
                pool = templedit.live_pool(self)
                for k, ptr in sorted(pool.items()):
                    if k and (not filter_text or filter_text.lower() in k.lower()):
                        out.append({"key": k, "category": None, "ptr": hex(ptr),
                                    "source": "live_pool"})
            except Exception:
                pass
        return out

    def theatres(self, tag=None):
        p = self.p
        return [{"ptr": hex(t), "class": self.oi.class_of(t)}
                for t in _arr(p, self._c(tag).ptr, CO_THEATRES)]

    @read_scoped("order_groups", key_fn=lambda self, tag=None: (self._rs_tag_key(tag),))
    def order_groups(self, tag=None):
        """The country's ARMIES and ARMY GROUPS (COrdersGroup)."""
        import armyops
        p = self.p
        co = self.country(tag) if tag else self.player()
        out, seen = [], set()
        for og in armyops.orders_groups(self, co):
            seen.add(og)
            out.append(armyops.og_info(self, og))
            parent = p.u64(og + armyops.OG_PARENT)
            if parent and parent not in seen:
                seen.add(parent)
                out.append(armyops.og_info(self, parent))
        # 2026-11-18: `og_info` reads member_armies from og+0x228+0xC, which on a
        # freshly created army group reported 615 for two armies. Recompute every
        # group's membership from the real parent links (one pass, no guessing),
        # and make a group's division count the sum of its armies.
        kids = {}
        for row in out:
            pg = row.get("parent_group")
            if pg:
                kids.setdefault(pg, []).append(row)
        for row in out:
            if row.get("is_army_group"):
                mine = kids.get(row["ptr"], [])
                row["member_armies"] = len(mine)
                row["member_ptrs"] = [k["ptr"] for k in mine]
                row["divisions"] = sum(int(k.get("divisions") or 0) for k in mine)
                row["members_source"] = "parent_links"
        return {"count": len(out), "groups": out}

    @read_scoped("unassigned_divisions", key_fn=lambda self, tag=None: (self._rs_tag_key(tag),))
    def unassigned_divisions(self, tag=None):
        """Divisions not attached to any army (CArmy*)."""
        co = self.country(tag) if tag else self.player()
        divs = _arr(self.p, co.ptr, CO_ARMIES)
        if not divs:
            return []
        import armyops as _ao
        import fastmem as _fm
        og_off = getattr(_ao, "CARMY_OG", 0xC0)
        batch = _fm.read_batch(self.p, [(d + og_off, 8) for d in divs])
        if batch is not None:
            out = []
            for d, raw in zip(divs, batch):
                if raw is not None and len(raw) == 8:
                    if struct.unpack("<Q", raw)[0] == 0:
                        out.append(d)
                elif self.p.u64(d + og_off) == 0:
                    out.append(d)
            return out
        return [d for d in divs if self.p.u64(d + og_off) == 0]

    def create_army(self, divisions=None, count=None, tag=None):
        """Collect divisions into a new army (the '+' button in the UI).

        The command is QUEUED (CCommand::Run) and only takes effect when the game
        advances a tick. In a paused game the old code reported success while no
        army existed, and a second call in the same paused tick took the SAME
        divisions again and silently did nothing. Both are reported honestly now.
        """
        import armyops_win as armyops
        def _n():
            try:
                return (self.order_groups(tag) or {}).get("count")
            except Exception:
                return None
        before = _n()
        divs = divisions or self.unassigned_divisions(tag)
        if count:
            divs = divs[:count]
        if not divs:
            return {"ok": False, "error": "no divisions to assign"}
        ok, err = armyops.create_army(self, divs)
        after = _n()
        res = {"ok": ok, "error": err, "divisions": len(divs),
               "armies_before": before, "armies_after": after}
        rej = getattr(self, "_last_division_rejected", None)
        if rej:
            res["rejected"] = list(rej[:10])
        return res

    def _resolve_orders_group(self, orders_group, tag=None):
        """将集团军指针（int / '0x...' / 十进制）或集团军名称（如 '第1集团军'）解析为 COrdersGroup* 整型地址。

        2026-11-27 (D-02 根治): 返回 `(og_int, err_msg, candidates)`；解析失败时返回 `(0, err, cands)`。
        """
        if orders_group is None:
            return 0, "orders_group is required", []
        if isinstance(orders_group, int) and not isinstance(orders_group, bool):
            return (int(orders_group), "", []) if orders_group > 0 else (0, "orders_group must be > 0", [])
        s = str(orders_group).strip()
        if not s:
            return 0, "orders_group is required", []
        # 1. 优先识别 0x... 十六进制或纯十进制指针
        if s.lower().startswith("0x"):
            try:
                v = int(s, 16)
                return (v, "", []) if v > 0 else (0, "invalid orders_group pointer %r" % s, [])
            except ValueError:
                pass
        elif s.isdigit():
            v = int(s, 10)
            if v > 0x10000:
                return v, "", []
        # 2. 按集团军名称（含中文名如 "第1集团军"）在 order_groups 中解析
        rows = []
        try:
            og_res = self.order_groups(tag)
            if isinstance(og_res, dict):
                rows = list(og_res.get("groups") or og_res.get("order_groups") or [])
            elif isinstance(og_res, list):
                rows = list(og_res)
        except Exception:
            rows = []
        avail = [
            {"ptr": r.get("ptr"), "name": r.get("name"), "divisions": r.get("divisions"),
             "is_army_group": bool(r.get("is_army_group"))}
            for r in rows if isinstance(r, dict) and r.get("ptr")
        ]
        exact = [r for r in avail if (r.get("name") or "").strip() == s]
        if not exact:
            sl = s.lower()
            exact = [r for r in avail if (r.get("name") or "").strip().lower() == sl]
        if not exact:
            sl = s.lower()
            exact = [r for r in avail if sl in (r.get("name") or "").strip().lower()]
        # 优先普通集团军（非集团军群）避免名称重名歧义
        if len(exact) > 1:
            armies_only = [r for r in exact if not r.get("is_army_group")]
            if len(armies_only) == 1:
                exact = armies_only
        if len(exact) == 1:
            return int(str(exact[0]["ptr"]), 16), "", avail
        if len(exact) > 1:
            return (
                0,
                "ambiguous orders_group name %r matched %d groups; pass explicit hex ptr from candidates"
                % (s, len(exact)),
                exact,
            )
        return (
            0,
            "no orders_group matched %r (pass a hex ptr or army name from inspect topic='armies')" % s,
            avail,
        )

    def add_divisions_to_army(self, orders_group, divisions=None, count=None,
                              from_army=None, order_id=None, tag=None,
                              move_to_province=None, spread=True,
                              max_per_province=None):
        """将未编组师或指定源集团军的师并入已存在的集团军（不建新军）。

        2026-11-23 (C-3 & 复测 11 P2-1 & 2026-11-27 D-02):
          1. `orders_group` 与 `from_army` 同时支持十六进制指针（如 `"0x..."`）与集团军名称（如 `"第1集团军"`）；
          2. `divisions` 与 `from_army` 均省略：把当前未编组师（自动避开伞兵）并入 `orders_group`
             （未指定 `count` 时自动补足至 24 师上限，或全部未编组师）；
          3. 指定 `from_army`：把源集团军的师全部（或前 `count` 个）并入目标军（合并散军）；
          4. 指定 `divisions=[ptr, ...]`：精确并入指定师列表；
          5. 默认仅改归属不改物理位置（返回 `positions_moved: False`）；若同时传入
             `move_to_province=<province_id>`，并军成功后自动调用 `move_divisions(..., spread=spread)`
             把转入的师一步调离原省份（避免继续吃前线补给）。
        """
        og, og_err, og_cands = self._resolve_orders_group(orders_group, tag=tag)
        if not og:
            out_err = {"ok": False, "blocked_by": "invalid_orders_group", "error": og_err or "orders_group is required"}
            if og_cands:
                out_err["available_order_groups"] = og_cands[:20]
            return out_err
        if sys.platform != "win32":
            return {"ok": False, "error": "add_divisions_to_army is currently implemented on Windows PE 1.19.5.0"}
        import armyops_win as _aw
        import divreserve as _dr

        if divisions is not None:
            divs = [int(x, 16) if isinstance(x, str) else int(x) for x in divisions]
        elif from_army is not None:
            src_og, src_err, src_cands = self._resolve_orders_group(from_army, tag=tag)
            if not src_og:
                out_err = {"ok": False, "blocked_by": "invalid_from_army", "error": src_err or "invalid from_army"}
                if src_cands:
                    out_err["available_order_groups"] = src_cands[:20]
                return out_err
            if src_og == og:
                return {"ok": False, "error": "from_army and orders_group are the same army"}
            divs = list(_aw.divisions_of(self, src_og))
        else:
            divs = [d for d in self.unassigned_divisions(tag)
                    if not _dr.is_paradrop_division(self, d)[0]]
            if count is None:
                cur_n = len(_aw.divisions_of(self, og))
                room = max(0, 24 - cur_n)
                if room > 0:
                    divs = divs[:room]
        if count is not None:
            divs = divs[:max(0, int(count))]
        if not divs:
            return {"ok": False, "error": "no eligible divisions to add to army %s" % hex(og)}
        res = _aw.add_divisions_to_army(self, og, divs, order_id=order_id)
        if move_to_province is not None and isinstance(res, dict) and res.get("ok"):
            moved_ptrs = res.get("transferred_divisions") or [hex(d) for d in divs]
            if moved_ptrs:
                mr = self.move_divisions(
                    divisions=moved_ptrs,
                    province=int(move_to_province),
                    tag=tag,
                    spread=bool(spread),
                    max_per_province=max_per_province,
                )
                res["move_result"] = {
                    "ok": bool(mr.get("ok")),
                    "province": mr.get("province"),
                    "spread": bool(mr.get("spread")),
                    "spread_plan": mr.get("spread_plan"),
                    "ordered": mr.get("ordered"),
                    "accepted": mr.get("accepted"),
                    "moving": mr.get("moving"),
                    "blocked_by": mr.get("blocked_by"),
                    "error": mr.get("error"),
                }
                res["positions_moved"] = bool((mr.get("accepted") or 0) > 0)
                if res["positions_moved"]:
                    res["position_note"] = (
                        "transferred %d division(s) and issued move orders toward province %d (spread=%s)"
                        % (len(moved_ptrs), int(move_to_province), bool(spread))
                    )
        return res

    def create_army_group(self, armies=None, tag=None):
        """Collect armies into a new army group (field marshal command)."""
        p = self.p
        import armyops_win as AW
        co = self.country(tag) if tag else None
        mine = [og for og in AW.armies(self, co)
                if not p.u8(og + AW.OG_IS_GROUP) and not p.u64(og + AW.OG_PARENT)]
        if armies is None:
            armies = mine
        if len(armies) < 1:
            return {"ok": False, "error": "no armies to group"}
        ok, err, info = AW.create_army_group(self, armies)
        out = {"ok": ok, "error": err, "armies": len(armies)}
        out.update(info or {})
        return out

    def assign_commander(self, orders_group, character_key=None, leader_ptr=None,
                         allow_unverified=False):
        """Assign a commander to an army or army group.

        Player rules are enforced: only a FIELD MARSHAL may lead an army group, only a
        corps commander. If the character's `visible` condition is not met,
        For example a general not visible under the current government is REFUSED.
        """
        import armyops
        if isinstance(orders_group, str):
            orders_group = int(orders_group, 16)
        if isinstance(leader_ptr, str):
            leader_ptr = int(leader_ptr, 16)
        key = character_key
        if leader_ptr is None:
            for gen in self.generals()["generals"]:
                if gen["key"] == character_key or gen["name"] == character_key:
                    leader_ptr = int(gen["leader_ptr"], 16)
                    key = gen["key"]
                    break
            else:
                return {"ok": False,
                        "error": "找不到该将领: %s (先跑 hoi4_available_generals 看可用的 key)" % character_key}
        ok, why = legality.check_commander(self, orders_group, leader_ptr, key, allow_unverified)
        if not ok:
            return {"ok": False, "error": why, "rejected_by": "player_rules"}
        # CSetArmyLeaderCommand ctor: (this, CArmyLeader*, COrdersGroup*, u8)
        self.send_command("SetArmyLeader",
                          {"arg0": leader_ptr, "arg1": orders_group, "arg2": 0})
        self._settle(0.5)
        after = armyops.og_info(self, orders_group).get("leader_ptr")
        same = bool(after) and int(after, 16) == int(leader_ptr)
        return {"ok": same, "leader_ptr": hex(leader_ptr), "after": after,
                "error": "" if same else "the game did not attach the leader"}

    def division_detail(self, tag=None, limit=200, order_group=None):
        """Divisions one by one, using the game's own field names: organisation, strength,
        experience, entrenchment, fuel, supply, parent army, template, location."""
        import fieldmap, armyops, unitorders
        p = self.p
        fm = fieldmap.field_map(self, "CArmy")
        keep = {"organisation", "strength", "experience", "dig_in", "dig_in_cap",
                "fuel", "max_supply", "army_current_supply_ratio", "out_of_supply_days",
                "str_damage", "org_damage", "killed"}
        if order_group:
            divs = armyops.group_divisions(self, order_group)
        else:
            divs = list(_arr(p, self._c(tag).ptr, CO_ARMIES))
            # Divisions assigned to an army group are no longer in the country
            # array; without this union a fully grouped army reads as empty
            # (measured 1944-06: 225 divisions on the map, 0 returned).
            seen = set(divs)
            try:
                for og in (self.order_groups(tag).get("groups") or []):
                    gptr = og.get("ptr")
                    if isinstance(gptr, str):
                        gptr = int(gptr, 16)
                    if not gptr:
                        continue
                    try:
                        for d in armyops.group_divisions(self, gptr):
                            if d not in seen:
                                seen.add(d)
                                divs.append(d)
                    except Exception:
                        continue
            except Exception:
                pass
        out = []
        lost = 0
        target_divs = divs[:limit]
        import fastmem as _fm
        use_batch = (
            bool(target_divs)
            and getattr(unitorders.province_of, "__module__", "") == "unitorders"
            and getattr(unitorders.movement, "__module__", "") == "unitorders"
        )
        batch_rows = (
            _fm.read_divisions_batch(
                p,
                target_divs,
                off_prov_ptr=unitorders.CARMY_PROVINCE,
                off_prov_id=unitorders.PROV_ID,
                off_path_ptr=unitorders.CARMY_PATH,
                off_path_len=unitorders.CARMY_PATH_SIZE,
                off_tmpl=0x3B8,
                off_og=armyops.CARMY_OG,
                off_handle=unitorders.CARMY_HANDLE,
            )
            if use_batch
            else None
        )
        tmpl_names = {}
        og_names = {}

        def _tmpl_name(t_ptr):
            if not t_ptr:
                return None
            if t_ptr not in tmpl_names:
                tmpl_names[t_ptr] = item_name(p, t_ptr + DT_NAME)
            return tmpl_names[t_ptr]

        def _og_name(og_ptr):
            if not og_ptr:
                return None
            if og_ptr not in og_names:
                og_names[og_ptr] = p.stdstring(og_ptr + armyops.OG_NAME)
            return og_names[og_ptr]

        for i, a in enumerate(target_divs):
            # One division that dies / merges mid-read used to abort the whole
            # loop: 225 divisions on the map became 0 and staff_army trained 108
            # phantom divisions on the empty answer.
            try:
                if batch_rows is not None:
                    br = batch_rows[i]
                    if not a or a < 0x10000 or br is None or not br["vtable"]:
                        lost += 1
                        continue
                    tmpl = br["template"]
                    og = br["og"]
                    row = {
                        "index": i,
                        "ptr": hex(a),
                        "handle": br["handle"],
                        "province": br["prov"],
                        "moving": bool(br["path"]),
                        "template": _tmpl_name(tmpl),
                        "army": _og_name(og),
                        "army_ptr": hex(og) if og else None,
                    }
                else:
                    if not a or a < 0x10000 or not p.u64(a):
                        lost += 1
                        continue
                    tmpl = p.u64(a + 0x3B8)
                    og = p.u64(a + armyops.CARMY_OG)
                    row = {
                        "index": i,
                        "ptr": hex(a),
                        "handle": [p.i32(a + 0x18), p.i32(a + 0x1C)],
                        "province": unitorders.province_of(self, a),
                        "moving": bool(unitorders.movement(self, a)),
                        "template": _tmpl_name(tmpl),
                        "army": _og_name(og),
                        "army_ptr": hex(og) if og else None,
                    }
                row.update(fieldmap.read_fields(self, a, fm, keep))
                out.append(row)
            except Exception:
                lost += 1
                continue
        res = {"count": len(divs), "divisions": out}
        if lost:
            res["skipped"] = lost
        return res

    @read_scoped(
        "divisions",
        key_fn=lambda self, tag=None, limit=200, deep=False: (self._rs_tag_key(tag), int(limit), bool(deep)),
    )
    def divisions(self, tag=None, limit=200, deep=False):
        """The country's divisions (CArmy in the game's code).

        2026-11-16: this used to read `CCountry+0x290` of whatever pointer
        `country(tag)` handed back.  After the 1939.05.07 crash-restart that
        pointer was not Germany's, so the count was 0 while 109 divisions stood
        in the Reich and every land-war tool answered "no divisions".
        Resolution now goes through `divenum`, which matches the tag id the
        CCountry object carries itself, and `deep=True` adds an RTTI fallback
        when the array is empty.  `count` is never silently zero any more: a
        failed resolution raises, and a disagreement with the deep scan is
        reported in `warning`.
        """
        import divenum
        import fastmem as _fm
        import unitorders as _uo
        p = self.p
        c = self._c(tag)
        me = c.tag
        info = divenum.armies(self, me)
        rows = info["armies"]
        count = info["count"]
        source = info["source"]
        warning = None
        if count <= 0 and deep:
            try:
                hits = divenum.deep_scan(self, me)
            except Exception as e:
                hits = []
                warning = "deep scan failed: %r" % (e,)
            if hits:
                warning = ("CCountry+0x290 is empty but %d CArmy object(s) stand in "
                           "provinces of states %s owns" % (len(hits), me))
                rows, count, source = hits, len(hits), "rtti-scan"
        target_rows = rows[:limit]
        batch_rows = (
            _fm.read_divisions_batch(
                p,
                target_rows,
                off_prov_ptr=_uo.CARMY_PROVINCE,
                off_tmpl=0x3B8,
            )
            if target_rows
            else []
        )
        tmpl_names = {}
        out = []
        for i, a in enumerate(target_rows):
            br = batch_rows[i] if i < len(batch_rows) else None
            if br is not None:
                tmpl = br["template"]
                prov_ptr = br["prov_ptr"]
            else:
                tmpl = p.u64(a + 0x3B8)
                prov_ptr = p.u64(a + _uo.CARMY_PROVINCE)
            if tmpl:
                if tmpl not in tmpl_names:
                    tmpl_names[tmpl] = item_name(p, tmpl + DT_NAME)
                tname = tmpl_names[tmpl]
            else:
                tname = None
            out.append({"index": i, "ptr": hex(a),
                        "template": tname,
                        "province_ptr": hex(prov_ptr)})
        res = {"count": int(count), "divisions": out, "source": source,
               "country_ptr": hex(info["ptr"]), "tag_id": info["tid"],
               "verified": bool(info.get("verified"))}
        if warning:
            res["warning"] = warning
        return res

    def army_status(self, tag=None, deep=False):
        """One verified answer to "how many divisions do I have, and where".

        `hoi4_army_status` used to be `divisions()["count"]` plus three other
        reads, so a bad country pointer showed up as a calm `0`.  Any caller
        that gates a declaration of war on this number gets `countable: false`
        instead of a lie.

        `deep=True` adds the RTTI fallback.  It is off by default because the
        first `CArmy` vtable scan of a game process takes ~40 s (afterwards it
        is cached); `hoi4_division_selftest` is the tool that pays that cost on
        purpose, and `war_readiness_check` pays it only when it sees a zero.
        """
        import divenum
        c = self._c(tag)
        me = c.tag
        d = self.divisions(me, limit=1, deep=deep)
        try:
            armies = self.order_groups(me)
        except Exception as e:
            armies = {"count": 0, "groups": [], "error": repr(e)}
        try:
            unassigned = len(self.unassigned_divisions(me))
        except Exception as e:
            unassigned = None
            d.setdefault("warning", "unassigned_divisions failed: %r" % (e,))
        try:
            conveyors = self.conveyors(me)
        except Exception:
            conveyors = []
        res = {
            "tag": me,
            "armies": armies,
            "divisions_count": d["count"],
            "unassigned_divisions": unassigned,
            "conveyors": conveyors,
            "source": d.get("source"),
            "countable": bool(d.get("verified")),
            "country_ptr": d.get("country_ptr"),
            "tag_id": d.get("tag_id"),
            "selftest": divenum.selftest(self, me, deep=deep),
        }
        if d.get("warning"):
            res["warning"] = d["warning"]
        return res

    def move_divisions(self, divisions=None, province=None, orders_group=None, tag=None,
                       mode="move", wait=12.0, limit=60, force=False,
                       spread=False, max_per_province=None):
        import movemicro
        return movemicro.move_divisions(self, divisions, province, orders_group, tag,
                                        mode=mode, wait=wait, limit=limit, force=force,
                                        spread=spread, max_per_province=max_per_province)

    def deploy_to_border(self, target_tag, orders_group=None, include_unassigned=True, tag=None,
                         protect_borders=None, min_other_garrison=0,
                         max_divisions=None, reserve_divisions=0):
        import movemicro
        return movemicro.deploy_to_border(
            self, target_tag, orders_group, include_unassigned, tag,
            protect_borders=protect_borders, min_other_garrison=min_other_garrison,
            max_divisions=max_divisions, reserve_divisions=reserve_divisions,
        )

    def attack_country(self, target_tag, mode="balanced", max_attacks=2, tag=None):
        import movemicro
        return movemicro.attack_country(self, target_tag, mode, max_attacks, tag)

    def plug_border_gaps(self, target_tag, min_garrison=1, max_gaps=None, dry_run=False):
        import movemicro
        return movemicro.plug_border_gaps(self, target_tag, min_garrison,
                                         max_gaps, dry_run)

    def wartime_status(self):
        import movemicro
        return movemicro.wartime_status(self)

    def wartime_step(self, target_tag=None, max_attacks=2, dry_run=False):
        import movemicro
        return movemicro.wartime_step(self, target_tag, max_attacks, dry_run)

    def set_front_directive(self, enemy_tag, stance="balanced_attack",
                            priority_provinces=None, priority_states=None,
                            min_garrison=1, deploy=None, tag=None, battleplan=True,
                            armies=None):
        # 2026-11-17: the wrapper forgot `battleplan`, but mcp/server.py passes it
        # unconditionally, so every hoi4_set_front_directive call died with
        # "MoveMixin.set_front_directive() got an unexpected keyword argument
        # 'battleplan'" -- i.e. the whole land-war entry point was dead on this
        # fork while movemicro itself supported the parameter all along.
        import movemicro
        return movemicro.set_front_directive(self, enemy_tag, stance,
                                             priority_provinces, priority_states,
                                             min_garrison, deploy, tag, battleplan,
                                             armies=armies)

    def front_directives(self):
        import movemicro
        return movemicro.front_directives(self)

    def clear_front_directive(self, enemy_tag=None):
        import movemicro
        return movemicro.clear_front_directive(enemy_tag)

    def tactical_front_tick(self, enemy_tag=None, force=False, dry_run=False):
        import movemicro
        return movemicro.tactical_front_tick(self, enemy_tag, force, dry_run)
