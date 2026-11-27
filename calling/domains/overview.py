"""全局态势总览、轻量告警、资源阈值提示、完整报告与游戏速度控制领域 Mixin。"""
from __future__ import annotations

import offsets as O
import sys

from ._shared import (
    CO_ARMIES,
    CO_COMMAND_POWER,
    CO_DIPLOMACY,
    DP_RELATIONS,
    _AIR_SNAP,
    _arr,
    _count,
)

class OverviewMixin:
    """全局态势总览、轻量告警、资源阈值提示、完整报告与游戏速度控制领域 Mixin。"""

    THRESHOLDS = {"political_power": 1000, "army": 100, "navy": 100, "air": 100}
    SPEND_HINT = {
        "political_power": "切法案 / 雇佣军官团顾问 / 点决议 / 装备设计",
        "army": "第二门 Grand Doctrine（100 XP）/ 改师编制",
        "navy": "海军学说 / 改舰船设计",
        "air": "空军学说 / 改飞机设计",
    }

    def relation_count(self, tag=None):
        return _count(self.p, self.p.u64(self._c(tag).ptr + CO_DIPLOMACY), DP_RELATIONS)

    def game_speed(self):
        """纯只读获取当前游戏速度（0..4，对应 UI 1~5 格）。"""
        if sys.platform == "win32":
            import cmdwin
            sp = cmdwin.get_game_speed(self)
            tbl = cmdwin.game_speed_seconds(self)
            sph = tbl[sp] if (sp is not None and 0 <= sp < len(tbl)) else None
            return {
                "speed": sp,
                "ui_speed": (sp + 1) if (sp is not None and 0 <= sp <= 4) else None,
                "seconds_per_hour": sph,
            }
        return {"speed": None, "ui_speed": None, "seconds_per_hour": None}

    def set_game_speed(self, speed: int, measure_seconds=None, use_command: bool = False):
        """设置游戏速度（工具层最高允许 engine 3 = UI 4 速；传 4/5 自动钳位至 3），并返回回读/速率验证结果。"""
        speed = int(speed)
        if not 0 <= speed <= 5:
            raise ValueError("speed must be between 0 and 5")
        if sys.platform == "win32":
            import cmdwin
            return cmdwin.set_game_speed(
                self, speed, measure_seconds=measure_seconds, use_command=use_command
            )
        capped = min(speed, 3)
        with self:
            self.cmd.send("SetGameSpeed", speed=capped)
        return {"ok": True, "requested": speed, "speed": capped,
                "engine_speed": capped, "ui_speed": capped + 1,
                "max_engine_speed": 3, "max_ui_speed": 4,
                "clamped": capped != speed}

    def _air_snapshot(self, max_age=25.0):
        """Player air wings, cached per game pid.

        2026-11-17: the user's complaint was that nothing ever reminded the model
        to touch the air force -- in the 1940 run all 15 wings sat at mission 0
        from 1936 to 1940.  `hoi4_overview` now reports that, but a CAirWing RTTI
        scan is expensive, so it is cached for a few seconds and keyed by pid.
        """
        import time
        pid = getattr(getattr(self, "p", None), "pid", None)
        if (_AIR_SNAP.get("pid") == pid and _AIR_SNAP.get("rows") is not None
                and (time.time() - float(_AIR_SNAP.get("ts") or 0)) < max_age):
            return _AIR_SNAP["rows"]
        rows = []
        try:
            import airops_win as AW
            rows = AW.german_wings(self).get("wings") or []
        except Exception:
            rows = []
        _AIR_SNAP.update(pid=pid, ts=time.time(), rows=rows)
        return rows

    def _navy_snapshot(self):
        """Player fleets + task-force missions (pointer walk, no RTTI)."""
        try:
            import navalops_win as NW
            fleets = NW.naval_fleets(self)
        except Exception:
            return []
        return fleets or []

    def _factory_counts(self, tag, summary, lines):
        """统一权威工厂口径（N1 根治）：优先使用引擎 CProductionStatus 校准后的可用/空闲数，
        避免占领区合规折算（如 73 建 - 3 折损 = 70 可用）与 reddots 口径打架；离线/mock 平滑降级。"""
        raw_mil = int((summary or {}).get("military_factories") or 0)
        raw_dock = int((summary or {}).get("naval_dockyards") or 0)
        used_mil_l = sum(int(l.get("factories") or 0) for l in (lines or ())
                         if l.get("class") == "CMilitaryProductionLine")
        used_dock_l = sum(int(l.get("factories") or 0) for l in (lines or ())
                          if l.get("class") == "CNavalProductionLine")
        try:
            if hasattr(self, "factory_allocation"):
                alloc = self.factory_allocation(tag)
                if isinstance(alloc, dict) and alloc.get("source") == "engine_production_status":
                    mil_avail = int(alloc.get("military_available") or 0)
                    mil_used = int(alloc.get("military_used") or 0)
                    mil_idle = int(alloc.get("military_idle") or 0)
                    dock_avail = int(alloc.get("naval_available") or 0)
                    dock_used = int(alloc.get("naval_used") or 0)
                    dock_idle = int(alloc.get("naval_idle") or 0)
                    return {
                        "source": "engine_production_status",
                        "mil_total": mil_avail,
                        "mil_raw": int(alloc.get("military_raw_buildings") or raw_mil),
                        "mil_used": mil_used,
                        "mil_free": mil_idle,
                        "dock_total": dock_avail,
                        "dock_raw": int(alloc.get("naval_raw_buildings") or raw_dock),
                        "dock_used": dock_used,
                        "dock_free": dock_idle,
                    }
        except Exception:
            pass
        return {
            "source": "summary_fallback",
            "mil_total": raw_mil,
            "mil_raw": raw_mil,
            "mil_used": used_mil_l,
            "mil_free": max(0, raw_mil - used_mil_l),
            "dock_total": raw_dock,
            "dock_raw": raw_dock,
            "dock_used": used_dock_l,
            "dock_free": max(0, raw_dock - used_dock_l),
        }

    def _light_alerts(self, tag, summary, lines, cur, _cons=None, _slots=None, _fcounts=None, _hb_out=None):
        """Alerts for hoi4_overview, without the 15 s focus ranking.

        `alerts()` calls selectable_focuses() and available_research(); together
        they pushed hoi4_overview past the MCP client timeout on the PE build
        (measured 34 s for alerts alone, 49 s for candidates).  Everything here
        is already-read data.
        """
        out = []
        try:
            if not cur:
                out.append("FOCUS_IDLE: 国策槽空置（用 hoi4_selectable_focuses 取候选）")
        except Exception:
            pass
        try:
            slots = _slots if _slots is not None else self.research_slots(tag)
            empty = [s["slot"] for s in slots if not s.get("slot_busy")]
            if empty:
                out.append("RESEARCH_IDLE: %d 个科研槽空置 (slot %s)"
                           % (len(empty), ", ".join(map(str, empty))))
        except Exception:
            pass
        try:
            pp = summary.get("political_power") or 0
            if pp >= 160:
                out.append("PP_HIGH: 政治点数 %d，可切换法案/任命顾问或点决议" % pp)
        except Exception:
            pass
        # --- 2026-11-17: the two resources that silently go to waste --------
        # Political power stops at 2000 and XP past 100 is only worth anything
        # once it is spent (a second grand doctrine costs 100 army XP).  The
        # player asked for an explicit reminder instead of having to remember
        # to look at the top bar.
        try:
            if pp >= 1000:
                out.append("PP_CAPPING: 政治点数 %d（>=1000，上限 2000，再攒就浪费 "
                           "-> 切法案 / 雇佣军官团顾问 / 点决议）" % pp)
        except Exception:
            pass
        try:
            xp = summary.get("experience") or {}
            for _svc, _label, _spend in (
                    ("army", "陆军", "第二门 Grand Doctrine / 改师编制"),
                    ("navy", "海军", "海军学说 / 改舰船设计"),
                    ("air", "空军", "空军学说 / 改飞机设计")):
                _v = xp.get(_svc)
                if _v is not None and _v >= 100:
                    out.append("XP_READY(%s): %s经验 %.1f（>=100，可花在%s）"
                               % (_svc, _label, _v, _spend))
        except Exception:
            pass
        try:
            cons_rows = _cons if _cons is not None else self.construction_queue(tag)
            q = len(cons_rows)
            civ = summary.get("civilian_factories") or 0
            if q == 0:
                # 2026-11-17 (third pass): a 1939 save came back with 66 civilian
                # factories building nothing.  "建造队列仅 0 项" reads like a
                # rounding error; an empty queue with 66 idle factories does not.
                out.append("CIV_FACTORY_IDLE: 建造队列为空，%d 座民工全部闲置 "
                           "-> hoi4_add_construction(building='arms_factory'|"
                           "'industrial_complex', state=...)" % civ)
            else:
                # 2026-11-19 (measured): the queue was 8 lines deep while 17-40
                # civilian factories sat idle, and `hoi4_overview` said nothing
                # because it only looked at "queue empty".  The per-line
                # `progress`/`progress_ic` readout is ALSO unreliable on 1.19.5
                # (line 0 stayed at 141.75 IC / 1.3% for 110 game days during
                # which a factory actually finished), so the factory ledger is
                # the only trustworthy signal here.
                _assigned = sum(int(l.get("factories") or 0) for l in cons_rows)
                _idle = int(civ) - _assigned
                # 2026-11-21 (续测 P2-9): `civ` is the RAW built level, which
                # includes the factories permanently locked to consumer goods --
                # they can never be assigned to construction.  So `civ - assigned`
                # is NOT "idle".  Measured: 21 of 35 working while the alert said
                # "14 idle".  Only complain when almost nothing is assigned, and
                # word it as "not building" instead of "idle".
                if _assigned == 0 or _idle >= max(10, int(civ) // 2):
                    out.append("CONSTRUCTION_STALLED: 队列 %d 项，但只有 %d/%d 座民工投入建造"
                               "（其余含消费品工厂，不能计入闲置）-> 检查队列里的州是否还能造，"
                               "并用 hoi4_state_detail 核对真实进度" % (q, _assigned, civ))
                elif q < 6:
                    out.append("CONSTRUCTION_LOW: 建造队列仅 %d 项，民工即将闲置" % q)
        except Exception:
            pass
        try:
            n = len(self.unassigned_divisions(tag))
            if n:
                out.append("UNASSIGNED_DIVS: %d 个师未编入集团军" % n)
        except Exception:
            pass
        fc = _fcounts if isinstance(_fcounts, dict) else self._factory_counts(tag, summary, lines)
        try:
            mil_free = int(fc.get("mil_free") or 0)
            if mil_free >= 2:
                out.append("MIL_FACTORY_IDLE: %d 座军工厂闲置未分配"
                           " -> hoi4_set_production_amount / hoi4_add_production_line"
                           % mil_free)
        except Exception:
            pass
        # --- 2026-11-17 (third pass): the last two blind spots -----------------
        # Dockyards were never counted anywhere, and a front order with
        # `assigned == 0` is the single most expensive silent failure in this
        # codebase: the line is drawn, the UI shows it, and not one division ever
        # marches.  Both are cheap reads on data already in hand.
        try:
            dock = int(fc.get("dock_total") or 0)
            dock_free = int(fc.get("dock_free") or 0)
            if dock and dock_free >= 2:
                out.append("DOCKYARD_IDLE: %d/%d 座船坞闲置未分配"
                           " -> hoi4_set_production_amount(line=..., amount=...)"
                           % (dock_free, dock))
        except Exception:
            pass
        try:
            if sys.platform == "win32":
                import armyops_win as _aw
                empty_fronts = []
                for _og in _aw.armies(self):
                    try:
                        for _o in _aw.orders(self, _og):
                            # 2026-11-19: armyops_win.orders() calls this field
                            # `assigned_divisions`; the old `assigned` read was
                            # always None -> 0, so EVERY front order was reported
                            # as "assigned=0" (a false crash-hazard alert).
                            if _o.get("type") == 2 and not int(
                                    _o.get("assigned_divisions")
                                    or _o.get("assigned") or 0):
                                empty_fronts.append(_o.get("id"))
                    except Exception:
                        continue
                if empty_fronts:
                    out.append("FRONT_NO_DIVISIONS: %d 条前线命令 assigned=0"
                               "（线画了但一个师都没挂上去）"
                               " -> hoi4_assign_divisions(mode='even')"
                               % len(empty_fronts))
        except Exception:
            pass
        # --- 2026-11-17: air and navy were NEVER touched by any run ----------
        # (1940 campaign: 15 wings at mission 0 from 1936 to 1940, and no fleet
        # ever got a mission.) These two alerts exist so the model cannot forget.
        # 2026-11-22 (P3-3): 启用 Rust fastmem 后精简索引冷建仅 ~0.7-1.4s（缓存命中 10ms），
        # 可直接预热，彻底消除新进程首次调用必吐的过期 “首次约 40 s” 告警；仅在无 fastmem
        # 的慢速回落模式下延迟跳过，并给出准确文案。
        _idx_ready = getattr(self, "_oi", None) is not None
        if not _idx_ready:
            try:
                import fastmem as _fm
                if getattr(_fm, "AVAILABLE", False):
                    _ = self.oi
                    _idx_ready = getattr(self, "_oi", None) is not None
            except Exception:
                pass
        if not _idx_ready:
            out.append("AIR_NAVY_DEFERRED: 对象索引尚未建立（未启用 fastmem 时首次扫描较慢），本次跳过空军/海军任务检查 -> 先调 hoi4_air_wings 或 hoi4_inspect(air) 预热索引，下一次 overview 就会带上它们")
        _wings_cache = []
        _fleets_cache = []
        try:
            if not _idx_ready:
                raise RuntimeError("object index not built yet")
            _all_w = self._air_snapshot() or []
            _wings_cache = _all_w
            wings = [w for w in _all_w
                     if (w.get("type") or "") != "transport_plane"]
            if wings:
                # transport planes never take a strategic mission (they only
                # fly paradrop/supply orders), so counting them made the alert
                # permanently impossible to clear -- seen 2026-11-17.
                # An earlier revision also required `mission_on` (+0xA4), on the
                # theory that it was the player's enable bit.  Measured on a live
                # 1936 PE campaign: fighter wing 1 -> mask 1 and CAS wing 8 ->
                # mask 2 leave +0xA4 at 0 for BOTH, and the two wings are
                # byte-identical over 0x90..0xC4 apart from the mask itself.  Two
                # game days later +0xA4 was still 0 everywhere.  So there is no
                # fighter/CAS asymmetry and +0xA4 is a runtime "sorties airborne"
                # counter, which is legitimately 0 in peacetime.  Keying the red
                # dot off it made the alert permanently unclearable.
                idle = [w for w in wings if not w.get("mission")]
                planes = sum(int(w.get("planes") or 0) for w in wings)
                if idle:
                    _n_tr = sum(1 for w in _all_w
                                if (w.get("type") or "") == "transport_plane")
                    _tr_planes = sum(int(w.get("planes") or 0) for w in _all_w
                                     if (w.get("type") or "") == "transport_plane")
                    # 2026-11-21: the counts here are "wings that can take a
                    # strategic mission", which is why they differ from
                    # hoi4_air_wings: transport wings are excluded on purpose
                    # (they never fly a strategic mission).  Say so, otherwise
                    # the two tools look like they disagree -- measured
                    # 14/923 here vs 15/1003 there, and the delta was exactly
                    # the single 80-plane transport wing.
                    out.append("AIR_NO_MISSION: %d/%d 个可执行战略任务的空军联队（共 %d 架）"
                               "没有任务%s -> hoi4_air_mission(wing_ptr='fighters', "
                               "mission_mask=1) 或 hoi4_attach_air_wings"
                               % (len(idle), len(wings), planes,
                                  "；另有 %d 个运输机联队（%d 架）不计（运输机不接战略任务）"
                                  % (_n_tr, _tr_planes) if _n_tr else ""))
        except Exception:
            pass
        try:
            if not _idx_ready:
                raise RuntimeError("object index not built yet")
            fleets = self._navy_snapshot()
            _fleets_cache = fleets or []
            # 2026-11-22 (复测 5 P1 §3.1): 排除预备舰队（is_reserve / mission==8），
            # 预备舰队用于自动承接新造舰船，不是缺任务的作战特遣舰队。
            tfs = [
                (f, t) for f in _fleets_cache
                if not f.get("is_reserve")
                for t in (f.get("task_forces") or [])
                if not t.get("is_reserve") and int(t.get("mission") or 0) != 8
            ]
            if tfs:
                # 2026-11-22 (复测 5 §9.3 残留 A): 正在执行活动（返港/整修）且已在 tf+0x4F0
                # 挂上 queued_mission 的特遣舰队已受命，不再误报 NAVY_NO_MISSION。
                idle = [
                    (f, t) for f, t in tfs
                    if not (t.get("effective_mission") or t.get("mission") or t.get("queued_mission"))
                ]
                if idle:
                    out.append("NAVY_NO_MISSION: %d/%d 个特遣舰队没有任务 "
                               "-> hoi4_naval_mission(fleet_ptr, tf_ptr, mission, regions)"
                               % (len(idle), len(tfs)))
        except Exception:
            pass
        # --- 2026-11-22 (复测 5 §8 专题): 装备代差 / 库存过剩 / 空军缺口 / 燃油归零 ----
        try:
            import equipmeta as _em
            _avail_v = self._active_variant_names(tag)
            _pid = getattr(getattr(self, "p", None), "pid", None)
            _tag_s = self._c(tag).tag
            _obs_alerts, _all_obs, _obs_sup = _em.collect_obsolete_alerts(
                lines, _avail_v, pid=_pid, tag=_tag_s,
                dedupe=True, max_alerts=3, max_payback_days=60,
            )
            out.extend(_obs_alerts)
        except Exception:
            pass
        try:
            import equipmeta as _em
            _st_tot = (self.equipment_stockpile(tag) or {}).get("totals_by_equipment") or {}
            _total_mil = int(summary.get("military_factories") or 0)
            _divs_n = int((self.divisions(tag, limit=1) or {}).get("count") or 0)
            for _sr in _em.compute_surplus_lines(lines, _st_tot, _total_mil, _divs_n)[:2]:
                out.append(_sr["alert"])
        except Exception:
            pass
        try:
            import equipmeta as _em
            if _wings_cache:
                for _ag in _em.compute_air_gap_alerts(_wings_cache, lines)[:2]:
                    out.append(_ag)
        except Exception:
            pass
        try:
            import equipmeta as _em
            _fz = _em.compute_fuel_zero_alert(summary.get("fuel"), _wings_cache, _fleets_cache)
            if _fz:
                out.append(_fz)
        except Exception:
            pass
        # 2026-11-23: 挂载老手战略体检红灯（弱编+XP闲置 / 产能倒挂 / 前线断补危机）
        try:
            import macros as _mc
            _hb = _mc.strategic_health_board(self, tag=tag) or {}
            if isinstance(_hb_out, dict):
                _hb_out["value"] = _hb
            for _hal in (_hb.get("alerts") or []):
                if _hal.get("severity") == "high":
                    out.append("%s: %s (%s)" % (_hal.get("code"), _hal.get("title"), _hal.get("action") or ""))
                    if len(out) >= 14:
                        break
        except Exception:
            pass
        # 2026-11-21 (P2-15): the 1940 campaign was lost because every division sat
        # in Poland and NOTHING stood on the French border for seven months, and no
        # alert said so -- war_readiness only answers when asked about that country.
        # Check every enemy, every turn: at war + a border + zero divisions on it.
        try:
            _enemies = (self.wars(tag) or {}).get("enemies") or []
            if _enemies:
                import armyops_win as _aw
                _co2 = self._c(tag)
                try:
                    _myprov = set()
                    for _d in (self.divisions(tag, limit=400).get("divisions") or []):
                        _pp = int(_d.get("province_ptr") or "0x0", 16)
                        if _pp:
                            _myprov.add(self.p.i32(_pp + 0xA4))
                except Exception:
                    _myprov = set()
                _undef = []
                for _e in _enemies:
                    try:
                        _seg = _aw.fronts_towards(self, _e, _co2.tag)
                    except Exception:
                        continue
                    _border = set()
                    for _s in _seg:
                        _border.update(_s.get("border_provinces") or [])
                    if _border and not (_border & _myprov):
                        _undef.append({"enemy": _e, "border_provinces": len(_border)})
                if _undef:
                    out.append(
                        "BORDER_UNDEFENDED: 交战国方向边境一个师都没有 —— %s"
                        " -> hoi4_deploy_to_border(target_tag=...)"
                        % ", ".join("%s(%d 省)" % (x["enemy"], x["border_provinces"])
                                    for x in _undef))
        except Exception:
            pass
        return out

    def _light_candidates(self, tag, lines, dec_rows=None):
        """Cheap candidates block: no focus ranking, no tech-tree scan."""
        out = {}
        try:
            slots = self.research_slots(tag)
            out["empty_research_slots"] = [s["slot"] for s in slots
                                           if not s.get("slot_busy")
                                           or s.get("slot_done")]
        except Exception:
            out["empty_research_slots"] = []
        try:
            out["production_lines"] = [
                {"line_index": l["index"], "equipment": l.get("equipment"),
                 "factories": l.get("factories")}
                for l in lines if l.get("class") == "CMilitaryProductionLine"][:8]
        except Exception:
            out["production_lines"] = []
        try:
            # 2026-11-20 (P2 #11): bare keys said nothing about what a decision
            # costs or how long it takes.  The rich rows are already parsed and
            # cached in decisions_meta.pkl, so this costs one extra dict per row.
            # 2026-11-21 (续测 P2-10): the list mixed in decisions that belong to
            # OTHER countries (ENG_abdication_crisis, JAP_implementing_unos...)
            # because "visible" is evaluated globally and only the `visible` trigger
            # carries a `tag = X` guard.  `mine` is the parser's verdict on that
            # guard, so drop everything that is not ours.
            out["available_decisions"] = [
                {"key": d["key"], "title": d.get("title"),
                 "cost_pp": d.get("cost_pp"),
                 "days": d.get("days_remove") or d.get("days_mission_timeout"),
                 "category": d.get("category"), "mine": d.get("mine", True)}
                for d in (dec_rows if dec_rows is not None
                          else self.decisions(tag, "visible", rich=True))
                if d.get("mine", True)][:12]
        except Exception:
            out["available_decisions"] = []
        out["note"] = ("heavy blocks (focus ranking / tech list / build states / ideas) "
                       "moved to hoi4_selectable_focuses, hoi4_research_available, "
                       "hoi4_states_detail and hoi4_advisors")
        return out

    def overview(self, tag=None):
        import time as _t_ov
        c = self._c(tag)
        cur = self.current_focus(c.tag)
        summary = c.summary()
        lines = self.production_lines(c.tag)
        cons = self.construction_queue(c.tag)
        research = self.research_slots(c.tag)
        fcounts = self._factory_counts(c.tag, summary, lines)
        hb_box = {}
        alerts = self._light_alerts(
            c.tag, summary, lines, cur,
            _cons=cons, _slots=research, _fcounts=fcounts, _hb_out=hb_box,
        )
        if hb_box.get("value"):
            try:
                self._last_overview_hb = (c.tag, _t_ov.monotonic(), hb_box["value"])
            except Exception:
                pass
        armies_n = len(_arr(self.p, c.ptr, CO_ARMIES))
        unassigned = len(self.unassigned_divisions(c.tag))
        dec = self.decisions(c.tag, "visible", rich=True)
        # 2026-11-22 (P3-1): `decisions_visible` counted every globally-visible
        # decision (13) while `candidates.available_decisions` was already
        # tag-filtered (9), so one overview carried two different "decision"
        # numbers with no way to tell them apart.  Report both, named.  The rows
        # are now parsed once here and handed to _light_candidates.
        dec_mine = [d for d in dec if d.get("mine", True)]
        cand = self._light_candidates(c.tag, lines, dec_rows=dec)
        tree = self.focus_tree(c.tag)
        politics = self.politics(c.tag)
        prod = lines
        states = self.states(c.tag)
        focus_block = {
            "current": cur["key"] if cur else None,
            "name": cur["name"] if cur else None,
            "tree_size": len(tree),
        }
        if cur:
            for _fk in ("progress", "days_done", "cost_days", "days_left", "percent"):
                if _fk in cur:
                    focus_block[_fk] = cur[_fk]
        return {
            "date": self.date()["text"],
            "alerts": alerts,
            "factories": {"civ_total": summary.get("civilian_factories"),
                          "mil_total": fcounts["mil_total"],
                          "mil_raw_buildings": fcounts["mil_raw"],
                          "mil_used": fcounts["mil_used"],
                          "mil_free": fcounts["mil_free"],
                          "dock_total": fcounts["dock_total"],
                          "dock_raw_buildings": fcounts["dock_raw"],
                          "dock_used": fcounts["dock_used"],
                          "dock_free": fcounts["dock_free"],
                          "factory_source": fcounts["source"],
                          "construction_queue_len": len(cons)},
            "army_summary": {"total_divisions": armies_n,
                             "unassigned_divisions": unassigned},
            "candidates": cand,
            "country": summary,
            "focus": focus_block,
            "research": research,
            "politics": politics,
            "decisions_visible": len(dec_mine),
            "decisions_visible_all": len(dec),
            "decisions_visible_mine": len(dec_mine),
            "production": prod,
            "obsolete_lines_count": sum(1 for l in prod if l.get("obsolete")),
            "construction": cons,
            "states": len(states),
        }

    def thresholds(self, tag=None):
        """Political power and the three experience pools, with the alarms.

        Pure reads.  `reached` is what the per-turn checklist and the player
        care about: PP>=1000 (the cap is 2000, so banking further is waste) and
        army/navy/air experience >=100 (one doctrine step).
        """
        c = self._c(tag)
        s = c.summary()
        pp = s.get("political_power") or 0
        xp = s.get("experience") or {}
        reached = []
        if pp >= self.THRESHOLDS["political_power"]:
            reached.append("political_power>=1000")
        for k in ("army", "navy", "air"):
            v = xp.get(k)
            if v is not None and v >= self.THRESHOLDS[k]:
                reached.append("%s_xp>=100" % k)
        return {
            "date": self.date()["text"],
            "tag": c.tag,
            "political_power": pp,
            "political_power_cap": 2000,
            "experience": xp,
            "thresholds": dict(self.THRESHOLDS),
            "reached": reached,
            "spend_hint": dict(self.SPEND_HINT),
            "warn": bool(reached),
        }

    def full_report(self, tag=None):
        """The wide status report to read at the start of a turn."""
        c = self._c(tag)
        base = self.overview(c.tag)
        base["resources"] = self.resources(c.tag)
        # B2/C2 (review 2026-11-07): `country_stats` is built on the 1.19.2
        # stats.pkl table and is now refused by `_scope_call`; calling it here
        # killed the process. Report the refusal instead of the numbers.
        try:
            base["stats"] = self.country_stats(c.tag)
        except Exception as e:
            base["stats"] = {}
            base["stats_unavailable"] = str(e)
        base["armies"] = self.armies(c.tag)["count"]
        base["division_templates"] = [t["name"] for t in self.division_templates(c.tag)]
        base["fleets"] = [f["name"] for f in self.fleets(c.tag)]
        if sys.platform == "win32" and getattr(O, "PO_COMMAND_POWER", None) is not None and c.politics:
            base["command_power"] = self.p.i64(c.politics + O.PO_COMMAND_POWER) / O.FIXED
        else:
            base["command_power"] = self.p.i64(c.ptr + CO_COMMAND_POWER) / O.FIXED
        # B2: `selectable_focuses` returns a dict, so `for f in ...` iterated the
        # KEYS and `f["key"]` raised TypeError: string indices must be integers.
        _sf = self.selectable_focuses(c.tag)
        _sel = _sf.get("selectable", []) if isinstance(_sf, dict) else (_sf or [])
        base["selectable_focuses"] = [
            (f["key"] if isinstance(f, dict) else str(f)) for f in _sel
        ]
        base["completed_focuses"] = [f for f in self.completed_focuses(c.tag) if f]
        base["faction"] = self.faction(c.tag)
        base["stockpile"] = self.equipment_stockpile(c.tag)["stockpile"]
        return base
