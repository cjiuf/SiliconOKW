"""One-click housekeeping: research slots, construction queue, idle military
factories, political power, and unassigned divisions.

Everything goes through the same command path as a player click (RUN_CMD).
Returns an honest per-section result; a failed sub-step never aborts the rest.
"""
from __future__ import annotations

# Set once `recruit_unit_leader` has faulted inside the game (sig=11): the call
# is retried every housekeep otherwise, and one fault per 8 minutes is a crash
# waiting to happen. Process-scoped, never persisted.
_LEADER_FAULTED = [False]

# 2026-11-23 (复测 7 B-8): 进程级满槽/引擎拒绝州短 TTL 缓存（避免 64/42 等满槽州跨次调用反复撞墙）
# key = (pid, tag, building, n_states) -> (timestamp, set[state_id])
import session_state as _SS

_REJECTED_STATE_CACHE = {}
_SS.register_cache("housekeep._REJECTED_STATE_CACHE", container=_REJECTED_STATE_CACHE)
_REJECTED_STATE_TTL_S = 120.0
_MAX_QUEUED_SYNTH_REFINERY = 2
_MAX_OWNED_SYNTH_REFINERY_WAR = 4


class HousekeepMixin:
    def _construction_target(self, tag=None):
        return 6

    def _owned_synth_count(self, tag=None):
        """统计国内已建成的合成炼油厂总数。"""
        total = 0
        try:
            for s in self.states(tag):
                d = self.state_detail(s["id"], tag)
                total += int((d.get("buildings") or {}).get("synthetic_refinery") or 0)
        except Exception:
            return 0
        return total

    def _pick_building(self, tag=None, queued_synth=0):
        """CA industrial timeline (work sheet task 2).

        Before 1937.06: 100% civilian factories, in the highest-infrastructure
        states (the queue order is handled by _state_priority).
        From 1937.06, at war, or when rubber/fuel is short: military factories,
        with a synthetic refinery inserted when the resource balance is negative.

        2026-11-23 (复测 7 B-8): 低燃油/缺橡胶时绝不再把全部建造配额锁死在合成炼油厂
        （14500 IC，比军工贵一倍）。队列中炼油厂达到上限（2 座）或战时国内已有 >=4 座后，
        立即切回 arms_factory。
        """
        d = self.date()
        at_war = False
        try:
            import combat
            at_war = bool(combat.at_war(self))
        except Exception:
            pass
        late = (d["year"], d["month"]) >= (1937, 6) or at_war
        if not late:
            return "industrial_complex"
        if int(queued_synth or 0) >= _MAX_QUEUED_SYNTH_REFINERY:
            return "arms_factory"
        res = {}
        try:
            res = self.resources(tag) or {}
        except Exception:
            pass
        fuel_days = None
        try:
            fuel_days = float(self.player().summary().get("fuel") or 0)
        except Exception:
            pass
        want_synth = (res.get("rubber", 0) < 0 or
                      (fuel_days is not None and fuel_days < 500))
        if want_synth and at_war and self._owned_synth_count(tag) >= _MAX_OWNED_SYNTH_REFINERY_WAR:
            want_synth = False
        if want_synth and self._can_build_synth_refinery(tag):
            return "synthetic_refinery"
        return "arms_factory"

    def _can_build_synth_refinery(self, tag=None):
        """True only when the tech is in and some state still has a free slot."""
        try:
            res = {t.lower() for t in (self.researched(tag) or [])}
        except Exception:
            res = set()
        if not any("synth_oil" in t for t in res):
            return False
        try:
            for s in self.states(tag):
                d = self.state_detail(s["id"], tag)
                b = d.get("buildings") or {}
                if int(b.get("synthetic_refinery") or 0) < 2:
                    return True
        except Exception:
            return False
        return False

    def _state_priority(self, tag=None):
        """Own states, highest infrastructure first (generic; no hard-coded GER ids)."""
        states_list = self.states(tag)
        try:
            key = (self.p.pid, self._c(tag).tag, len(states_list))
        except Exception:
            key = None
        if key is not None and getattr(self, "_hk_state_key", None) == key:
            return list(self._hk_state_rank)
        rank = []
        for s in states_list:
            try:
                d = self.state_detail(s["id"], tag)
                infra = int((d.get("buildings") or {}).get("infrastructure") or 0)
            except Exception:
                infra = 0
            rank.append((-infra, s["id"]))
        rank.sort()
        out = [sid for _, sid in rank]
        self._hk_state_key = key
        self._hk_state_rank = out
        return out

    def housekeep_research(self, tag=None, techs=None):
        return self.fill_all_research_slots(techs or [], tag)

    def housekeep_construction(self, tag=None, target=None):
        import time as _t
        target = int(target or self._construction_target(tag))
        q_now = self.construction_queue(tag)
        before = len(q_now)
        queued_synth_now = sum(1 for row in q_now if row.get("building") == "synthetic_refinery")
        custom_picker = False
        try:
            primary_building = self._pick_building(tag, queued_synth=queued_synth_now)
        except TypeError:
            primary_building = self._pick_building(tag)
            custom_picker = True

        # 2026-11-23 (复测 7 B-8): 混合建造配额计划 —— 当主选为 synthetic_refinery 时，
        # 最多只补到队列总计 2 座炼油厂（单次上限 2），剩余配额全部交给 arms_factory，
        # 绝不反复一口气塞 4~8 座高价炼油厂拖垮军工扩张。
        need_total = max(0, target - before)
        if primary_building == "synthetic_refinery" and not custom_picker:
            synth_quota = max(0, min(need_total, _MAX_QUEUED_SYNTH_REFINERY - queued_synth_now))
            plan_batches = []
            if synth_quota > 0:
                plan_batches.append(("synthetic_refinery", synth_quota))
            if need_total - synth_quota > 0:
                plan_batches.append(("arms_factory", need_total - synth_quota))
        else:
            plan_batches = [(primary_building, need_total)] if need_total > 0 else []

        slot_buildings = set()
        try:
            import statemeta as _sm
            slot_buildings = _sm._load_slot_buildings()
        except Exception:
            slot_buildings = {"industrial_complex", "arms_factory", "dockyard",
                              "synthetic_refinery", "fuel_silo", "rocket_site",
                              "nuclear_reactor"}
        _BLDG_STATE_CAP = {
            "synthetic_refinery": 3,
            "infrastructure": 5,
            "air_base": 10,
            "radar_station": 6,
            "rocket_site": 3,
            "nuclear_reactor": 1,
            "anti_air_building": 5,
            "fuel_silo": 10,
        }
        queued_by_state = {}
        queued_same = {}
        for row in q_now:
            sid_q = row.get("state")
            b_q = row.get("building")
            if sid_q is not None:
                if b_q in slot_buildings:
                    queued_by_state[sid_q] = queued_by_state.get(sid_q, 0) + 1
                if b_q:
                    queued_same[(b_q, sid_q)] = queued_same.get((b_q, sid_q), 0) + 1

        prio = self._state_priority(tag)
        # 预读一次州槽位与建筑映射，供全部批次共享
        state_meta = {}
        for sid in prio:
            try:
                sd = self.state_detail(sid, tag)
                state_meta[sid] = (sd.get("slots_free"), sd.get("buildings") or {})
            except Exception:
                state_meta[sid] = (None, {})

        pid_key = getattr(getattr(self, "p", None), "pid", None)
        try:
            tag_key = self._c(tag).tag
        except Exception:
            tag_key = str(tag or "player")
        now_ts = _t.time()

        added, errors, skipped_full = [], [], []
        q_len = before
        for bldg, batch_need in plan_batches:
            if batch_need <= 0 or q_len >= target:
                continue
            needs_slot = bldg in slot_buildings
            bldg_cap = _BLDG_STATE_CAP.get(bldg)
            cache_k = (pid_key, tag_key, bldg, len(prio))
            cached_ent = _REJECTED_STATE_CACHE.get(cache_k)
            if cached_ent and (now_ts - cached_ent[0]) < _REJECTED_STATE_TTL_S:
                rejected_states = set(cached_ent[1])
            else:
                rejected_states = set()

            avail_slots = {}
            avail_bldg_cap = {}
            for sid in prio:
                sf, bmap = state_meta.get(sid, (None, {}))
                full_here = sid in rejected_states
                if needs_slot:
                    if sf is None:
                        avail_slots[sid] = None
                    else:
                        rem = max(0, int(sf) - queued_by_state.get(sid, 0))
                        avail_slots[sid] = rem
                        if rem <= 0:
                            full_here = True
                if bldg_cap is not None:
                    cur_lv = int(bmap.get(bldg) or 0)
                    rem_c = max(0, int(bldg_cap) - cur_lv - queued_same.get((bldg, sid), 0))
                    avail_bldg_cap[sid] = rem_c
                    if rem_c <= 0:
                        full_here = True
                if full_here and sid not in skipped_full:
                    skipped_full.append(sid)

            batch_added = 0
            for _pass in range(3):
                if batch_added >= batch_need or q_len >= target:
                    break
                pass_added = 0
                for sid in prio:
                    if batch_added >= batch_need or q_len >= target:
                        break
                    if sid in rejected_states:
                        continue
                    if needs_slot:
                        rem = avail_slots.get(sid)
                        if rem is not None and rem <= 0:
                            continue
                    if bldg_cap is not None:
                        rem_c = avail_bldg_cap.get(sid)
                        if rem_c is not None and rem_c <= 0:
                            continue
                    try:
                        res = self.add_construction(sid, bldg)
                    except Exception as e:
                        errors.append({"state": sid, "building": bldg, "error": str(e)})
                        rejected_states.add(sid)
                        continue
                    if res.get("ok"):
                        added.append({"state": sid, "building": bldg})
                        pass_added += 1
                        batch_added += 1
                        q_len += 1
                        if needs_slot:
                            queued_by_state[sid] = queued_by_state.get(sid, 0) + 1
                            if avail_slots.get(sid) is not None:
                                avail_slots[sid] = max(0, avail_slots[sid] - 1)
                        queued_same[(bldg, sid)] = queued_same.get((bldg, sid), 0) + 1
                        if bldg_cap is not None and avail_bldg_cap.get(sid) is not None:
                            avail_bldg_cap[sid] = max(0, avail_bldg_cap[sid] - 1)
                    else:
                        rejected_states.add(sid)
                        if sid not in skipped_full:
                            skipped_full.append(sid)
                        if res.get("error"):
                            errors.append({"state": sid, "building": bldg, "error": res["error"]})
                if pass_added == 0:
                    break
            if rejected_states:
                _REJECTED_STATE_CACHE[cache_k] = (now_ts, set(rejected_states))

        after = len(self.construction_queue(tag))
        return {"ok": after >= target, "before": before, "after": after,
                "target": target, "building": primary_building,
                "plan_batches": [{"building": b, "quota": q} for b, q in plan_batches],
                "added": added,
                "skipped_full_states": skipped_full[:12],
                "errors": errors[:12]}

    # CA standard factory split when nothing is critically short.
    # 2026-11-04: air was 0.20 and the allocator could never reach it (see the
    # deficit allocator below); the historical German build wants ~25% fighters+CAS.
    PROD_RATIO = {"inf": 0.34, "art": 0.21, "armor": 0.20, "air": 0.25}

    @staticmethod
    def _prod_group(equipment):
        eq = (equipment or "").lower()
        if "infantry_equipment" in eq:
            return "inf"
        if "artillery" in eq or "support_equipment" in eq:
            return "art"
        if any(k in eq for k in ("tank", "motorized", "mechanized", "armored_car")):
            return "armor"
        if any(k in eq for k in ("fighter", "cas", "bomber", "naval_bomber")):
            return "air"
        return "other"

    def _eq_family(self, name):
        """'infantry_equipment_2' -> ('infantry_equipment_', 2). ('x', -1) if no tier."""
        import re
        m = re.match(r"^(.*_)(\d+)$", str(name or ""))
        if m:
            return m.group(1), int(m.group(2))
        return str(name or ""), -1

    def _upgrade_production_lines(self, tag=None):
        """Swap obsolete production lines to the newest unlocked variant.

        2026-11-22 (复测 5 §8.4 / §8.7): 改用 equipmeta 基于 HOI4 真实 archetype /
        parent / year / 战斗属性的代差分析（仅做同 archetype 且回本 < 60 天的换代），
        再走修复后的 CSetProductionLineCommand 真实换装；已换过的产线第二次运行不再重复。
        """
        out = {"upgraded": [], "skipped_slow_payback": [], "errors": []}
        try:
            lines = self.production_lines(tag, include_obsolete=False)
            avail = self._active_variant_names(tag)
            import equipmeta as _em
            analysis = _em.analyze_production_lines(lines, avail)
        except Exception as e:
            out["errors"].append(str(e))
            return out
        for l in lines:
            if l.get("class") != "CMilitaryProductionLine":
                continue
            a = analysis.get(l["index"])
            if not a or not a.get("obsolete"):
                continue
            cur = l.get("equipment") or ""
            target = a.get("upgrade_to")
            if not target or target == cur:
                continue
            pb = int(a.get("payback_days") or 999)
            if pb >= 60:
                out["skipped_slow_payback"].append({
                    "line": l["index"], "from": cur, "to": target,
                    "payback_days": pb, "upgrade_gain": a.get("upgrade_gain"),
                })
                continue
            try:
                r = self.set_production_line_equipment(l["index"], target, tag=tag)
            except Exception as e:
                out["errors"].append({"line": l["index"], "from": cur, "to": target, "error": str(e)})
                continue
            rec = {
                "line": l["index"], "from": cur, "to": target,
                "upgrade_gain": a.get("upgrade_gain"),
                "payback_days": pb,
                "retool_cost": a.get("retool_cost"),
                "ok": bool(r.get("ok")),
                "can_execute": r.get("can_execute"),
            }
            if not rec["ok"]:
                rec["error"] = r.get("error")
            out["upgraded"].append(rec)
        return out

    def _stockpile_counts(self, tag=None):
        """按装备名汇总库存总量。

        2026-11-22 (复测 5 §9.3 残留 B): 旧实现遍历 `stockpile` 用 `out[eq] = count`
        直接覆盖而非累加，导致同型号多条 variant（例如德国自产 51,514 + 缴获 2,233 步枪）
        被后一条覆盖成 2,233，进而误触发 `< 3000` 步枪紧急缺口把刚裁掉的工厂全塞回步兵线。
        现优先直读 `totals_by_equipment`，回落时严格使用 `+=` 累加。
        """
        out = {}
        try:
            st = self.equipment_stockpile(tag) or {}
            tots = st.get("totals_by_equipment")
            if isinstance(tots, dict) and tots:
                for k, v in tots.items():
                    if k:
                        out[k] = float(v or 0.0)
                return out
            for row in st.get("stockpile") or []:
                eq = row.get("equipment")
                if eq:
                    out[eq] = out.get(eq, 0.0) + float(row.get("count") or 0.0)
        except Exception:
            pass
        return out

    def _cap_deprioritized_lines(self, tag, rules):
        """Veteran rule: tactical/strategic bombers are a trap.

        Keep at most ONE factory on them; the freed factories go back to the idle
        pool and are handed to fighters/CAS on the next allocation pass.
        """
        want = tuple((rules or {}).get("deprioritize_equipment")
                     or ("tac_bomber", "strategic_bomber", "heavy_fighter"))
        out = []
        try:
            for l in self.production_lines(tag):
                if l.get("class") != "CMilitaryProductionLine":
                    continue
                eq = str(l.get("equipment") or "").lower()
                if not any(w in eq for w in want):
                    continue
                before = int(l.get("factories") or 0)
                if before <= 1:
                    continue
                res = self.set_production_amount(l["index"], 1, tag)
                out.append({"line": l["index"], "equipment": l.get("equipment"),
                            "before": before, "after": res.get("after"),
                            "ok": bool(res.get("ok"))})
        except Exception as e:
            out.append({"error": str(e)})
        return out

    def housekeep_production(self, tag=None):
        """Put every idle military factory to work.

        Priority order (work sheet task 2):
          1. a negative / critically low stockpile of rifles, support or artillery
             gets the next factory (the biggest shortfall first);
          2. otherwise allocate toward the CA ratio 30% infantry / 25% artillery+
             support / 25% tanks+motorised / 20% air.
        """
        upgraded = self._upgrade_production_lines(tag)
        try:
            import playbooks as _pbk0
            _rules0 = (_pbk0.get_playbook(self._c(tag).tag) or {}).get("production_rules") or {}
        except Exception:
            _rules0 = {}
        # Runs even when there is no idle factory: the freed factories show up on
        # the NEXT allocation pass, so the cap must not be gated behind `idle`.
        capped_lines = self._cap_deprioritized_lines(tag, _rules0)
        def _read_mil_budget():
            """统一使用引擎 CProductionStatus 校准后的可用/空闲军工（A10 根治），离线/mock 平滑降级。"""
            ls = self.production_lines(tag)
            ms = [l for l in ls if l.get("class") == "CMilitaryProductionLine"]
            used_l = sum(int(l.get("factories") or 0) for l in ms)
            try:
                if hasattr(self, "factory_allocation"):
                    alloc = self.factory_allocation(tag)
                    if isinstance(alloc, dict) and alloc.get("source") == "engine_production_status":
                        avail_m = int(alloc.get("military_available") or 0)
                        used_m = int(alloc.get("military_used") or used_l)
                        idle_m = int(alloc.get("military_idle") or 0)
                        return ls, ms, avail_m, used_m, idle_m
            except Exception:
                pass
            summ = self.player().summary() if tag in (None, "", "player") else self.country(tag).summary()
            tot_m = int(summ.get("military_factories") or 0)
            return ls, ms, tot_m, used_l, max(0, tot_m - used_l)

        lines, mil, total_mil, used, idle = _read_mil_budget()

        # 2026-11-22 (复测 5 §8.5 / §8.9 #6): 识别严重过剩产线（SURPLUS，如步兵装备库存
        # > 2 倍安全储备且占军工 > 20%），在回执中明确给出 surplus_lines / surplus_factories，
        # 并在存在明显缺口线（如空军/装甲/火炮）时把多余工厂挪出参与再分配。
        surplus_lines = []
        surplus_trimmed = []
        try:
            import equipmeta as _em
            _st_tot = (self.equipment_stockpile(tag) or {}).get("totals_by_equipment") or {}
            _divs_n = int((self.divisions(tag, limit=1) or {}).get("count") or 0)
            surplus_lines = _em.compute_surplus_lines(lines, _st_tot, total_mil, _divs_n)
            _has_non_surplus = any(
                self._prod_group(l.get("equipment")) in ("air", "armor", "art")
                for l in mil
            )
            if surplus_lines and _has_non_surplus:
                for _sr in surplus_lines:
                    if int(_sr.get("surplus_factories") or 0) >= 2:
                        _rtrim = self.set_production_amount(
                            _sr["line"], _sr["suggested_factories"], tag
                        )
                        surplus_trimmed.append({
                            "line": _sr["line"],
                            "equipment": _sr["equipment"],
                            "before": _sr["factories"],
                            "after": _rtrim.get("after"),
                            "surplus_factories": _sr["surplus_factories"],
                            "ok": bool(_rtrim.get("ok")),
                        })
                if surplus_trimmed:
                    lines, mil, total_mil, used, idle = _read_mil_budget()
        except Exception:
            pass

        # No air line at all? Create one before handing out factories, otherwise the
        # 25% air share can never be reached (the 1940 run ended with a single
        # fighter factory for four years).
        created_line = None
        if idle > 0 and not any(self._prod_group(l.get("equipment")) == "air" for l in mil):
            try:
                avail = [v["equipment"] for v in
                         (self.equipment_stockpile(tag).get("available_variants") or [])
                         if v.get("equipment")]
                air = sorted(e for e in avail
                             if "fighter" in e or "cas" in e or "small_plane" in e)
                if air:
                    res = self.create_production_line(air[-1], tag=tag)
                    created_line = {"equipment": air[-1], "ok": bool(res.get("ok")),
                                    "error": res.get("error")}
                    lines, mil, total_mil, used, idle = _read_mil_budget()
            except Exception as e:
                created_line = {"ok": False, "error": str(e)}

        obsolete_remaining = [
            {
                "line": l["index"], "equipment": l.get("equipment"),
                "upgrade_to": l.get("upgrade_to"),
                "upgrade_gain": l.get("upgrade_gain"),
                "payback_days": l.get("payback_days"),
                "suggested_action": l.get("suggested_action"),
            }
            for l in mil if l.get("obsolete")
        ]

        # 2026-11-23 (复测 8 B-12): 补齐真实空闲船坞自动分配（严格使用校准后的 factory_allocation，
        # 已扣除修理船坞），让 IDLE_DOCK 成为真正可由 housekeep 一键解决的红点。
        dockyard_changes = self._housekeep_dockyards(tag)

        if idle <= 0:
            return {"ok": True, "idle": 0, "total": total_mil, "used": used,
                    "changes": [], "dockyard_changes": dockyard_changes,
                    "upgraded": upgraded, "created_line": created_line,
                    "capped": capped_lines,
                    "surplus_lines": surplus_lines,
                    "surplus_factories": sum(int(x.get("surplus_factories") or 0) for x in surplus_lines),
                    "surplus_trimmed": surplus_trimmed,
                    "obsolete_remaining": obsolete_remaining,
                    "note": ("allocated idle dockyards" if dockyard_changes else "no idle military factories")}
        if not mil:
            return {"ok": False, "idle": idle, "total": total_mil, "used": used,
                    "changes": [], "error": "no military production line",
                    "capped": capped_lines}

        # One main line per group: the one that already has the most factories.
        groups = {}
        for l in mil:
            g = self._prod_group(l.get("equipment"))
            if g == "other":
                continue
            if g not in groups or int(l.get("factories") or 0) > int(groups[g].get("factories") or 0):
                groups[g] = l
        if not groups:
            groups = {"inf": mil[0]}

        stock = self._stockpile_counts(tag)
        rifles = sum(v for k, v in stock.items()
                     if k and "infantry_equipment" in k.lower())
        try:
            import playbooks as _pbk
            rules = (_pbk.get_playbook(self._c(tag).tag) or {}).get("production_rules") or {}
        except Exception:
            rules = {}
        ratio_map = dict(self.PROD_RATIO)
        rifle_cap = float(rules.get("rifle_stockpile_soft_cap") or 25000)
        if rules and rifles >= rifle_cap:
            # Veteran rule: 25k rifles in the warehouse means the infantry line is
            # done — shift the next factories into fighters/CAS and armour.
            ratio_map = dict(rules.get("ratios_rifle_surplus") or ratio_map)
        lines, mil, total_mil, used, idle = _read_mil_budget()
        low = {}
        need = (("inf", ("infantry_equipment",), 3000),
                ("art", ("artillery_equipment", "support_equipment"), 1200))
        for g, keys, floor in need:
            if g not in groups:
                continue
            have = sum(stock.get(k, 0) for k in stock if any(x in k.lower() for x in keys))
            if have < floor:
                low[g] = (have, floor)

        total_target = max(total_mil, 1)
        # 2026-11-22 (复测 5 §9.3 残留 B): 凡是本轮被识别为 SURPLUS / 已被裁剪的产线，
        # 绝不允许同一次 housekeep 再把刚刚腾出的空闲工厂塞回该过剩线；同时避开被
        # `_cap_deprioritized_lines` 限为 1 厂的低优轰炸机线，让闲置工厂真实流向缺口线。
        surplus_line_idxs = {int(x["line"]) for x in (surplus_lines or []) if "line" in x}
        surplus_line_idxs.update(int(x["line"]) for x in (surplus_trimmed or []) if "line" in x)
        deprio_want = tuple((_rules0 or {}).get("deprioritize_equipment")
                            or ("tac_bomber", "strategic_bomber", "heavy_fighter"))

        def _is_blocked_from_add(l):
            if int(l.get("index", -1)) in surplus_line_idxs:
                return True
            eq_l = str(l.get("equipment") or "").lower()
            if any(w in eq_l for w in deprio_want):
                return True
            return False

        # Deficit allocation over EVERY eligible line, not one line per group.
        target = {}
        for g, ratio in ratio_map.items():
            glines = [l for l in mil
                      if self._prod_group(l.get("equipment")) == g
                      and not _is_blocked_from_add(l)]
            if not glines:
                continue
            cur = [int(l.get("factories") or 0) for l in glines]
            tot = sum(cur)
            for l, c in zip(glines, cur):
                share = (c / tot) if tot else (1.0 / len(glines))
                target[id(l)] = total_target * ratio * share
        cur_counts = {}
        for l in mil:
            n = int(l.get("factories") or 0)
            cur_counts[id(l)] = n
            target.setdefault(id(l), float(n))

        alloc_pool = [l for l in mil if not _is_blocked_from_add(l)]
        if not alloc_pool:
            alloc_pool = list(mil)

        def shortfall(l):
            base = target[id(l)] - cur_counts[id(l)]
            if low and self._prod_group(l.get("equipment")) in low:
                base += max(1.0, total_target * 0.25)   # immediate shortage first
            return base

        desired = dict(cur_counts)
        left = idle
        while left > 0:
            line = max(alloc_pool, key=shortfall)
            if shortfall(line) <= -0.5:
                # 若所有合资格产线均已达标（如过剩组配额空出），将剩余工厂继续
                # 分配给最高优先缺口组（优先空军战斗机/CAS，其次装甲/火炮），避免闲置。
                non_inf = [x for x in alloc_pool if self._prod_group(x.get("equipment")) in ("air", "armor", "art")]
                if not non_inf:
                    break
                line = max(non_inf, key=shortfall)
            desired[id(line)] = desired[id(line)] + 1
            cur_counts[id(line)] = cur_counts[id(line)] + 1
            left -= 1

        changes = []
        for l in sorted(mil, key=lambda x: desired[id(x)] - int(x.get("factories") or 0),
                        reverse=True):
            before = int(l.get("factories") or 0)
            want = desired[id(l)]
            if want <= before:
                continue
            res = self.set_production_amount(l["index"], want, tag)
            after = int(res.get("after", before))
            ch_row = {
                "line": l["index"], "group": self._prod_group(l.get("equipment")),
                "equipment": l.get("equipment"),
                "before": before, "after": after, "requested": want,
                "granted": after - before, "ok": bool(res.get("ok")),
            }
            if res.get("note") or res.get("error"):
                ch_row["note"] = res.get("note") or res.get("error")
            changes.append(ch_row)
        used_after = sum(int(l.get("factories") or 0) for l in self.production_lines(tag)
                         if l.get("class") == "CMilitaryProductionLine")
        return {"ok": used_after >= total_mil or used_after > used,
                "idle": idle, "total": total_mil, "used": used,
                "used_after": used_after,
                "low_stock": sorted(low),
                "ratio": dict(ratio_map), "capped": capped_lines,
                "upgraded": upgraded, "created_line": created_line,
                "surplus_lines": surplus_lines,
                "surplus_factories": sum(int(x.get("surplus_factories") or 0) for x in surplus_lines),
                "surplus_trimmed": surplus_trimmed,
                "obsolete_remaining": obsolete_remaining,
                "dockyard_changes": dockyard_changes,
                "changes": changes}

    def _housekeep_dockyards(self, tag=None):
        """把真实空闲海军船坞（已扣除修理船坞）自动分配给现有造船线。"""
        changes = []
        try:
            alloc = self.factory_allocation(tag) if hasattr(self, "factory_allocation") else {}
            if not isinstance(alloc, dict) or alloc.get("source") != "engine_production_status":
                return changes
            nav_idle = int(alloc.get("naval_idle") or 0)
            if nav_idle <= 0:
                return changes
            all_lines = self.production_lines(tag) or []
            nav_lines = [l for l in all_lines if l.get("class") == "CNavalProductionLine"]
            if not nav_lines:
                return changes
            # 优先分配给已有进度的护航/潜艇/驱逐舰（单线上限 10），其次巡洋舰/主力舰（单线上限 5）
            def _cap_for(eq_name):
                eq_l = str(eq_name or "").lower()
                if any(k in eq_l for k in ("heavy", "carrier", "battleship", "battlecruiser", "cruiser")):
                    return 5
                return 10

            for l in nav_lines:
                if nav_idle <= 0:
                    break
                cur_f = int(l.get("factories") or 0)
                cap_f = _cap_for(l.get("equipment"))
                room = max(0, cap_f - cur_f)
                if room <= 0:
                    continue
                add = min(nav_idle, room)
                r = self.set_production_amount(l["index"], cur_f + add, tag, auto_reclaim=False)
                after = int(r.get("after", cur_f))
                granted = max(0, after - cur_f)
                if granted > 0:
                    changes.append({
                        "line": l["index"],
                        "equipment": l.get("equipment"),
                        "before": cur_f,
                        "after": after,
                        "granted": granted,
                        "ok": bool(r.get("ok")),
                    })
                    nav_idle -= granted
        except Exception:
            pass
        return changes

    # Each chain is ordered best -> worst; the first one that CanExecute accepts wins.
    LAW_CHAINS = (
        ("war_economy", "partial_economic_mobilisation", "low_economic_mobilisation"),
        ("extensive_conscription", "limited_conscription", "volunteer_only"),
        ("export_focus", "limited_exports", "free_trade"),
    )
    SAFE_DECISIONS = (
        # promises_of_peace removed: it is a -10% war-support decision and the
        # economic-law gates (partial needs 25%) are the thing that keeps a
        # country out of total mobilisation.
        "improved_worker_conditions", "war_bonds",
        "infrastructure_building_slot", "GER_mefo_bills_extension",
        "anti_fascist_raids", "anti_communist_raids", "anti_democratic_raids",
    )
    _DANGER = ("civil_war", "surrender", "abdicate", "concede", "abandon", "oppose",
               "capitulate", "join_war")

    # Industrial concerns, weapon/aircraft designers and theorists. They are ordinary
    # CIdea objects in PL_ALL_IDEAS, so the already-verified AddIdea/ReplaceIdea path
    # applies — 150 PP each. This is how a player spends PP after the laws are maxed.
    PREF_CONCERNS = ("ig_farben", "krupp", "siemens", "rheinmetall", "mauser", "opel",
                     "henschel", "messerschmitt", "junkers", "porsche", "man",
                     "bofors", "skoda")

    def _law_keys(self):
        keys = set()
        for chain in self.LAW_CHAINS:
            keys.update(chain)
        keys.update({"undisturbed_isolation", "isolation", "civilian_economy",
                     "tot_economic_mobilisation", "disarmed_nation", "volunteer_only",
                     "service_by_requirement", "all_adults_serve", "scraping_the_barrel",
                     "closed_economy", "free_trade", "export_focus", "limited_exports"})
        return keys

    def _fill_idea_slots(self, tag=None, max_buys=8, deadline=None):
        """Buy industrial concerns / designers / advisors in empty idea categories.

        Only categories that are currently EMPTY are touched: replacing a minister
        the country already has would need a political-power comparison the tool
        does not have, and replacing a law here would fight LAW_CHAINS.
        """
        import time as _t
        p = self.p
        c = self._c(tag)
        active = self._idea_entries(tag, "active")
        active_keys = {a["key"] for a in active}
        active_cats = {a["category"] for a in active if a.get("category")}
        laws = self._law_keys()
        all_ideas = self._idea_entries(tag, "all")

        def rank(e):
            k = (e.get("key") or "").lower()
            for i, pref in enumerate(self.PREF_CONCERNS):
                if k == pref:
                    return (0, i, k)
            for i, pref in enumerate(self.PREF_CONCERNS):
                if k.startswith(pref) or pref in k:
                    return (1, i, k)
            return (2, 0, k)

        bought = []
        for e in sorted(all_ideas, key=rank):
            if len(bought) >= max_buys:
                break
            if deadline is not None and _t.monotonic() >= deadline:
                break
            if float(c.political_power) < 150:
                break
            k, cat = e.get("key"), e.get("category")
            if not k or not cat or cat in active_cats or k in active_keys or k in laws:
                continue
            try:
                res = self.add_idea(k, tag=tag)
            except Exception as exc:
                bought.append({"idea": k, "ok": False, "error": str(exc)})
                continue
            if res.get("ok"):
                bought.append({"idea": k, "ok": True,
                               "pp_before": res.get("pp_before"),
                               "pp_after": res.get("pp_after")})
                active_cats.add(cat)
                active_keys.add(k)
        return bought

    def housekeep_politics(self, tag=None, budget_s=12.0):
        """Spend political power: law upgrades (with fallback), then decisions.

        2026-11-22 (复测 5 §3.3): 增加单步软时间上限（默认 12s），避免一次 politics
        吃光 40s 预算挤掉后续步骤。
        """
        import time as _t
        t0 = _t.monotonic()
        deadline = t0 + float(budget_s or 12.0)
        budget_trimmed = False
        pol = self.politics(tag)
        pp_before = float(pol["political_power"])
        pp = pp_before
        active = set(pol.get("active_ideas") or [])
        actions = []

        for chain in self.LAW_CHAINS:
            if _t.monotonic() >= deadline:
                budget_trimmed = True
                break
            if pp < 150:
                break
            cur = next((x for x in chain if x in active), None)
            if cur is None:
                # 2026-11-19 (measured): with no member of the chain active the
                # old code fell through the whole `for want in chain` loop and
                # tried to SET the first law of a category the country was not
                # using at all.  On the 1936 run that produced
                #   {"law": "export_focus",  "from": null, "ok": false}
                #   {"law": "limited_exports","from": null, "ok": true}
                # while the country was actually running `autarkic_economy` —
                # i.e. it invented a trade law and reported success for a no-op.
                # Never guess: only upgrade a law the country already has.
                actions.append({"law": None, "chain": list(chain),
                                "skipped": "no active law from this chain"})
                continue
            if cur == chain[0]:
                continue
            for want in chain:
                if want == cur:
                    break
                if want in active:
                    continue
                try:
                    res = self.add_idea(want, tag=tag)
                except Exception as e:
                    res = {"ok": False, "error": str(e)}
                actions.append({"law": want, "from": cur, "ok": bool(res.get("ok")),
                                "error": res.get("error") or res.get("last_error")})
                if res.get("ok"):
                    pp = float(self._c(tag).political_power)
                    active.add(want)
                    break

        if pp >= 150 and _t.monotonic() < deadline:
            try:
                vis = [d["key"] for d in self.decisions(tag, "visible", rich=False)]
            except Exception:
                vis = []
            cands = [d for d in self.SAFE_DECISIONS if d in vis]
            if pp >= 400:
                for d in vis:
                    if d not in cands and not any(b in d.lower() for b in self._DANGER):
                        cands.append(d)
            for dec in cands[:4]:
                if pp < 150:
                    break
                if _t.monotonic() >= deadline:
                    budget_trimmed = True
                    break
                try:
                    res = self.select_decision(dec, tag=tag)
                except Exception as e:
                    res = {"ok": False, "error": str(e)}
                actions.append({"decision": dec, "ok": bool(res.get("ok")),
                                "error": res.get("error")})
                if res.get("ok"):
                    pp = float(self._c(tag).political_power)
        elif pp >= 150:
            budget_trimmed = True

        # Buy the non-law idea slots last: they are a pure PP sink and must not
        # compete with a law upgrade for the same 150 PP.
        if pp >= 150 and _t.monotonic() < deadline:
            try:
                ideas = self._fill_idea_slots(tag=tag, max_buys=4, deadline=deadline)
            except Exception as e:
                ideas = [{"ok": False, "error": str(e)}]
            for row in ideas:
                actions.append({"idea_slot": row.get("idea"), "ok": bool(row.get("ok")),
                                "error": row.get("error")})
            if ideas:
                pp = float(self._c(tag).political_power)
        elif pp >= 150:
            budget_trimmed = True

        # Last resort when every idea slot is full
        if pp >= 600 and not _LEADER_FAULTED[0] and _t.monotonic() < deadline:
            for _ in range(2):
                if pp < 600 or _t.monotonic() >= deadline:
                    if _t.monotonic() >= deadline:
                        budget_trimmed = True
                    break
                try:
                    res = self.recruit_unit_leader(0, tag=tag)
                except Exception as e:
                    res = {"ok": False, "error": str(e)}
                if isinstance(res, dict) and "sig=11" in str(res.get("error") or ""):
                    _LEADER_FAULTED[0] = True
                actions.append({"recruit_leader": "general", "ok": bool(res.get("ok")
                                if isinstance(res, dict) else False),
                                "verified": res.get("verified") if isinstance(res, dict) else None,
                                "error": (res or {}).get("error") if isinstance(res, dict) else None})
                pp = float(self._c(tag).political_power)
                if not isinstance(res, dict) or not res.get("ok"):
                    break

        pp = float(self._c(tag).political_power)
        out = {
            "ok": True, "pp_before": pp_before, "pp": pp,
            "pp_delta": round(pp - pp_before, 2),
            "actions": actions,
            "elapsed_s": round(_t.monotonic() - t0, 2),
            "note": "" if any(a.get("ok") for a in actions) else "no applicable law or decision",
        }
        if budget_trimmed:
            out["budget_trimmed"] = True
            out["note"] = ((out["note"] + "; ") if out["note"] else "") + (
                "hit %.0fs politics step budget; remaining items deferred to next turn"
                % float(budget_s or 12.0)
            )
        return out

    def _conveyor_progress(self, conv):
        """(finished_lines, max_progress) of a conveyor's training lines.

        CMilitaryDeploymentConveyor+0x70 is CPdxArray<CMilitaryDeploymentLine*>;
        each line's CMilitaryDeployment* is at +0x28 and its completion fraction is
        +0x60 (fixed 1e5). A dead conveyor sits at 1.0 forever: the division is
        trained but there is no deployment location, so it never lands.
        """
        p = self.p
        import sys
        from game import _arr
        # `conveyors()` returns dicts; older call sites passed the raw pointer.
        ptr = int(conv["ptr"], 16) if isinstance(conv, dict) else int(conv)
        # 2026-11-17: the line array AND the deployment pointer both moved on the
        # PE build.  Verified live on 1.19.5.0 with hoi4_browse_object:
        #   CMilitaryDeploymentConveyor+0x78  CPdxArray<CMilitaryDeploymentLine*>
        #   CMilitaryDeploymentLine+0x30      CMilitaryDeployment*
        #   CMilitaryDeployment+0x60          u32 progress, fixed 1e5
        #   CMilitaryDeployment+0x68          u64 1e5 (the 100 % denominator)
        # The old Linux offsets (0x70 / line+0x28) read unrelated memory here, so
        # every line reported 0.0 and `hoi4_conveyor_progress` was null for every
        # conveyor.  Measured: 16654 (16.7 %) and 88569 (88.6 %) on two live lines.
        if sys.platform == "win32":
            arr_off, dep_off, prog_off = 0x78, 0x30, 0x60
        else:
            arr_off, dep_off, prog_off = 0x70, 0x28, 0x60
        done, best = 0, 0.0
        for ln in _arr(p, ptr, arr_off):
            md = p.u64(ln + dep_off)
            if not md or md < 0x10000000000:
                continue
            prog = p.u32(md + prog_off) / 1e5
            best = max(best, prog)
            if prog >= 0.999:
                done += 1
        return done, round(best, 3)

    def _repair_conveyors(self, tag=None):
        """Point every existing conveyor at the capital province; delete the dead ones.

        `SetConveyorLocation` takes a PROVINCE id and the engine only accepts it on
        a conveyor that has not started training. A conveyor from an older save has
        lines already, so the call is refused and it stays at "No location set"
        forever — its finished divisions can never deploy (verified: 26./33./30.
        Infanterie-Division stuck at 1/inf for a year).

        Such a conveyor is removed (CRemoveConveyorCommand) and recreated with a
        valid location. The removal is deliberately conservative: it needs a line
        that is 100 % trained in TWO consecutive housekeep calls and no growth in
        the division count, so a healthy conveyor that happens to be at 1.0 for one
        tick (a division about to deploy) is never touched.
        """
        c = self._c(tag)
        from commands import COMMANDS as _CMDS
        want_state = self._homeland_state_id(c.tag)
        want_prov = self.deployment_province(want_state, tag=c.tag)
        hist = getattr(self, "_stuck_hist", None)
        if hist is None:
            hist = self._stuck_hist = {}
        divs = int(self.divisions(c.tag).get("count") or 0)
        out = []
        for conv in self.conveyors(c.tag):
            key = (c.tag, conv["packed_handle"])
            row = {"conveyor": conv["ptr"], "state_id": want_state,
                   "deploy_province": want_prov}
            try:
                with self:
                    rv = self._run_cmd(self.cmd.make("SetConveyorLocation",
                                                     conveyor_handle=conv["packed_handle"],
                                                     province_id=int(want_prov)))
                    row["location_ok"] = bool(rv & 0xFF)
                    row["location_error"] = getattr(self, "_last_cmd_error", "")
                    if "SetConveyorPriority" in _CMDS:
                        rv2 = self._run_cmd(self.cmd.make("SetConveyorPriority",
                                                          conveyor_handle=conv["packed_handle"],
                                                          priority=2))
                        row["priority_ok"] = bool(rv2 & 0xFF)
                done, best = self._conveyor_progress(conv)
                row["finished_lines"] = done
                row["max_progress"] = best
                if not row.get("location_ok") and done:
                    prev = hist.get(key)
                    if prev and prev[0] == divs:
                        with self:
                            rv3 = self._run_cmd(self.cmd.make(
                                "RemoveConveyor", conveyor_handle=conv["packed_handle"]))
                        row["removed"] = bool(rv3 & 0xFF)
                        row["remove_error"] = getattr(self, "_last_cmd_error", "")
                        hist.pop(key, None)
                    else:
                        hist[key] = (divs, done)
                        row["stuck_strike"] = 1
            except Exception as e:
                row["error"] = str(e)
            out.append(row)
        return out

    def housekeep_recruitment(self, tag=None):
        """Keep ONE infantry conveyor at 2-3 lines and push trained divisions to the map.

        Germany's historical focuses gate on army size (Anschluss ~500k men under
        arms, Sudetenland ~725k). The previous run opened 2 conveyors x 5 lines =
        10 divisions training at once against a rifle output of ~26/day: every one
        of them sat at ~10 % equipment for 17 months and not a single division
        finished. One conveyor, 2-3 lines, highest equipment priority and a
        capital-connected deployment state is what actually produces divisions.
        """
        c = self._c(tag)
        out = {"ok": True, "opened": None, "deploy": None}
        try:
            count = int(self.divisions(c.tag).get("count") or 0)
            manpower = float(c.manpower() or 0)
            stock = self._stockpile_counts(tag)
            rifles = sum(v for k, v in stock.items()
                         if k and "infantry_equipment" in k.lower())
            conv = self.conveyors(c.tag)
            out.update(divisions=count, manpower=manpower, rifles=rifles,
                       conveyors=len(conv),
                       homeland_state=self._homeland_state_id(c.tag))
            if conv:
                out["repaired"] = self._repair_conveyors(tag)
                # a dead conveyor is removed by _repair_conveyors; rebuild it now
                conv = self.conveyors(c.tag)
                out["conveyors_after_repair"] = len(conv)
                out["removed"] = [r for r in out["repaired"] if r.get("removed")]
            # lines: 2 while rifles are scarce, 3 once the stockpile can feed them.
            lines = 2 if rifles < 4000 else 3
            # 2026-11-23 (复测 8 B-13 根因修复): 旧逻辑 `else: max_convs, lines = 2, 2`
            # 在步枪库存归 0 时仍允许连开 2 条 2 线 conveyor = 4 条训练带，连续两次 housekeep
            # 就亲手撞爆 reddots.DEFICIT_OVER_TRAINING（>=3 线且步枪 <1000）导致左右互搏卡死！
            # 现改为：步枪 <1000 时最多保留 1 条 2 线训练带（幂等不再叠加）；步枪 <300 且已有
            # 训练带时严禁新开。
            if count >= 95:
                max_convs, lines = 1, 2
            elif rifles >= 12000:
                max_convs, lines = 5, 3
            elif rifles >= 4000:
                max_convs, lines = 4, 3
            elif rifles >= 1200:
                max_convs, lines = 2, 2
            elif rifles >= 300:
                max_convs, lines = 1, 2
            else:
                max_convs, lines = (1 if not conv else len(conv)), 2
                if len(conv) >= 1:
                    max_convs = len(conv)  # 步枪极低且已有训练带 -> 幂等跳过，绝不叠加新带
            # Veteran ceiling: 90-100 fully equipped divisions beat 174 shells at
            # 40 % equipment. Stop opening lines at the cap and trim the surplus.
            try:
                import playbooks as _pbk
                _pb = _pbk.get_playbook(c.tag)
            except Exception:
                _pb = {}
            army_cap = int(_pb.get("army_cap") or 0)
            # B8 (review 2026-11-07): `army_cap` is a flat 110, so recruitment
            # stopped dead in 1939 while the playbook's own year curve asks for
            # 135. Use the curve, never below the flat value.
            try:
                _yr = int(self.date().get("year") or 0)   # WO-009: was g.date() -> NameError swallowed, cap stuck at 110
                _td = int(_pbk.get_target_divisions(c.tag, _yr) or 0)
                if _td > army_cap:
                    army_cap = _td
                    out["army_cap_target"] = _td
            except Exception:
                pass
            per_div = float(_pb.get("rifle_per_division") or 500)
            if army_cap and count >= army_cap:
                max_convs, lines = 0, 2
                out["army_cap"] = count
            # Equipment guard: a division needs ~1,000 rifles. Once the army is
            # already large, more training lines only dilute the stockpile further
            # (174 divisions on 76k rifles = ~40 % equipment could not attack).
            # Early game (fewer than 60 divisions) the guard stays out of the way —
            # the opening army is SUPPOSED to be under-equipped.
            if count >= 60 and rifles < count * per_div:
                max_convs = min(max_convs, max(1, len(conv)))
                out["equipment_guard"] = ("rifles %.0f < divs %d x %.0f -> no new conveyor"
                                          % (rifles, count, per_div))
            if max_convs == 0 and conv:
                removed = []
                for extra in conv:
                    try:
                        with self:
                            rv = self._run_cmd(self.cmd.make(
                                "RemoveConveyor", conveyor_handle=extra["packed_handle"]))
                        removed.append({"conveyor": extra.get("ptr"),
                                        "ok": bool(rv & 0xFF),
                                        "error": getattr(self, "_last_cmd_error", "")})
                    except Exception as e:
                        removed.append({"conveyor": extra.get("ptr"), "ok": False,
                                        "error": str(e)})
                out["trimmed"] = removed
                conv = self.conveyors(c.tag)
            # 2026-11-06: `count < 95` was a leftover from the pre-army_cap era and
            # meant that once the army passed 95 divisions a trimmed/battle-worn
            # army could NEVER open a conveyor again (end of run 4: 102 divisions,
            # 0 conveyors, no replacement stream).
            under_cap = (count < army_cap) if army_cap else (count < 95)
            # 额外总训练线数防爆保护：若步枪 < 1000 且场上总训练线数已 >= 2，严禁再开新带
            cur_total_lines = sum(max(1, int(cv.get("lines") or 1)) for cv in conv)
            over_deficit_cap = (rifles < 1000 and cur_total_lines >= 2)
            if under_cap and manpower > 100000 and len(conv) < max_convs and not over_deficit_cap:
                out["opened"] = self.train_divisions(lines=lines, tag=tag)
                try:
                    conv_after = self.conveyors(c.tag)
                    out["conveyors_after"] = len(conv_after)
                except Exception:
                    out["conveyors_after"] = len(conv) + 1
            else:
                out["conveyors_after"] = len(conv)
                out["skipped"] = ("count=%d manpower=%.0f rifles=%.0f conveyors=%d/%d lines=%d cur_lines=%d"
                                  % (count, manpower, rifles, len(conv), max_convs, lines, cur_total_lines))
        except Exception as e:
            out.update(ok=False, error=str(e))
        try:
            out["deploy"] = self.deploy_ready_conveyors(tag, min_progress=0.995)
        except Exception as e:
            out["deploy"] = {"error": str(e)}
        return out

    def housekeep_divisions(self, tag=None):
        # Re-enabled 2026-11-04: the heap corruption was caused by CPdxArray's
        # allocator field pointing at a VTABLE (0x43591B8) instead of the static
        # CPdxNewDeleteAllocator object (0x4505128) in armyops.ALLOC.
        created = []
        # 2026-11-10: a paratrooper is idle by design. Never collect it into an
        # automatic army -- the paradrop order owns it.
        import divreserve as _dr
        _para = [d for d in self.unassigned_divisions(tag)
                 if _dr.is_paradrop_division(self, d)[0]]
        if _para:
            created.append({"ok": True, "skipped_paratroopers": len(_para)})
        for _round in range(4):
            unassigned = [d for d in self.unassigned_divisions(tag)
                          if not _dr.is_paradrop_division(self, d)[0]]
            if not unassigned:
                break
            # 2026-11-23 (C-3): 优先把新训出的零散未编组师并入已有未满编（<24 师）的非伞兵集团军，
            # 避免每训出 2 个师就新建 1 支零散无将领小军！全部满编或无现有军时才新建军。
            merged_now = False
            if hasattr(self, "add_divisions_to_army"):
                try:
                    cur_groups = [
                        g for g in (self.order_groups(tag).get("groups") or [])
                        if not g.get("is_army_group") and 0 < int(g.get("divisions") or 0) < 24
                    ]
                    # 优先填已有将领且最接近满编的军（避免把 1~2 师塞成一堆散军）
                    cur_groups.sort(
                        key=lambda x: (0 if x.get("leader_ptr") else 1, -int(x.get("divisions") or 0))
                    )
                    for tgt in cur_groups:
                        og_ptr = int(tgt["ptr"], 16)
                        try:
                            if _dr.army_is_paradrop(self, og_ptr)[0]:
                                continue
                        except Exception:
                            pass
                        room = max(0, 24 - int(tgt.get("divisions") or 0))
                        if room <= 0:
                            continue
                        batch = unassigned[:room]
                        mr = self.add_divisions_to_army(og_ptr, divisions=batch, tag=tag)
                        created.append({"merged_into": tgt["ptr"], **mr})
                        if mr.get("ok") and int(mr.get("added") or 0) > 0:
                            merged_now = True
                            break
                        # 2026-11-23 (D-1): 输入师列表本身被校验拒绝时立刻短路，不再重复试剩余集团军
                        if mr.get("rejected"):
                            break
                except Exception:
                    merged_now = False
            if merged_now:
                continue
            before = {g["ptr"] for g in self.order_groups(tag)["groups"]}
            res = self.create_army(unassigned[:24], tag=tag)
            created.append(res)
            if not res.get("ok"):
                break
            for _ in range(12):
                if [g for g in self.order_groups(tag)["groups"] if g["ptr"] not in before]:
                    break
                self._settle(0.25)
        groups = self.order_groups(tag)["groups"]
        gens = [g for g in self.available_generals(tag).get("generals", [])
                if g.get("rank") == "corps_commander"]
        used = {g.get("leader_ptr") for g in groups if g.get("leader_ptr")}
        assigned = []
        for g in groups:
            if g.get("leader_ptr"):
                continue
            cand = next((x for x in gens if x.get("leader_ptr") not in used), None)
            if not cand:
                break
            try:
                cr = self.assign_commander(int(g["ptr"], 16),
                                           leader_ptr=int(cand["leader_ptr"], 16),
                                           character_key=cand["key"])
                ok = bool(cr.get("ok"))
                assigned.append({"army": g["ptr"], "commander": cand["key"],
                                 "ok": ok, "error": cr.get("error")})
                if ok:
                    used.add(cand["leader_ptr"])
            except Exception as e:
                assigned.append({"army": g["ptr"], "commander": cand.get("key"),
                                 "ok": False, "error": str(e)})
        remaining = len([d for d in self.unassigned_divisions(tag)
                         if not _dr.is_paradrop_division(self, d)[0]])
        unled = [g["ptr"] for g in self.order_groups(tag)["groups"] if not g.get("leader_ptr")]
        # The stated goal of this pass is "no unassigned divisions". A new campaign can
        # expose zero parsable corps commanders (CCharacterManager starts empty), so an
        # unled army is a warning, not a failed housekeep; otherwise every call reports
        # false even though all divisions were collected into armies.
        warning = None
        if unled:
            warning = ("%d army(s) have no commander: no available corps commander was "
                       "found (CCharacterManager may still be empty at campaign start)" % len(unled))
        # 2026-11-08 (work order 1108(1) §2.2): a freshly created army has NO front
        # line, so its divisions sit in Berlin until the player opens the army panel.
        # 2026-11-23 (复测 10 B-4/A-4 根治): 仅当确实存在「尚无任何前线订单（无 type==2）」
        # 的非伞兵集团军时才调 activate_army_battleplans 且只传入这些空闲军，绝不再在全军
        # 已有前线时无故触发 plan_front 把所有已布防集团军全员重排一遍！
        battle = None
        try:
            import movemicro
            free_armies = []
            had_any_army = False
            try:
                import armyops_win as _aw_hk
                for _g in (self.order_groups(tag).get("groups") or []):
                    if _g.get("is_army_group") or int(_g.get("divisions") or 0) <= 0:
                        continue
                    _ogp = int(_g["ptr"], 16)
                    try:
                        if _dr.army_is_paradrop(self, _ogp)[0]:
                            continue
                    except Exception:
                        pass
                    had_any_army = True
                    _ords = _aw_hk.orders(self, _ogp) or []
                    if not any(o.get("type") == 2 for o in _ords):
                        free_armies.append(_g["ptr"])
            except Exception:
                free_armies = None

            if had_any_army and free_armies == []:
                battle = {"ok": True, "skipped": "all_armies_already_have_front_orders"}
            else:
                enemy = None
                for d in (movemicro._load_front_state().get("directives") or {}).values():
                    enemy = (d or {}).get("enemy_tag")
                    if enemy:
                        break
                if not enemy:
                    ws = self.wartime_status()
                    enemy = (ws.get("enemies") or [None])[0]
                if enemy:
                    if free_armies:
                        battle = self.activate_army_battleplans(enemy, tag, armies=free_armies)
                    else:
                        battle = self.activate_army_battleplans(enemy, tag)
        except Exception as e:
            battle = {"ok": False, "error": str(e)}
        # 2026-11-23 (复测 8 B-14): 若 battleplans 被触发且返回 ok=False（例如前线被丢弃），
        # divisions 步骤不能再假报 ok=True 混进 succeeded_steps 掩盖问题。
        bp_ok = True if not isinstance(battle, dict) else bool(battle.get("ok", True))
        step_ok = (remaining == 0) and bp_ok
        if not bp_ok:
            bp_warn = "battleplans failed (%s)" % (battle.get("error") or "see battleplans.armed")
            warning = (warning + "; " + bp_warn) if warning else bp_warn
        return {"ok": step_ok,
                "unassigned": len(self.unassigned_divisions(tag)) if remaining else 0,
                "remaining": remaining, "created": created,
                "assigned": assigned, "unled": unled, "warning": warning,
                "battleplans": battle,
                "exempt_paratroopers": len(_para)}

    def housekeep_doctrines(self, tag=None):
        """Unlock the grand doctrines the playbook asks for (once per process/tag)."""
        done = getattr(self, "_doctrine_done", None)
        if done is None:
            done = self._doctrine_done = set()
        try:
            import playbooks
            docs = (playbooks.get_playbook(self._c(tag).tag) or {}).get("grand_doctrines") or []
        except Exception:
            docs = []
        out = []
        pid = getattr(getattr(self, "p", None), "pid", None)
        for d in docs:
            key = (pid, self._c(tag).tag, d)
            if key in done:
                out.append({"doctrine": d, "skipped": "already unlocked"})
                continue
            try:
                r = self.unlock_doctrine(d, tag=tag) or {}
            except Exception as e:
                r = {"ok": False, "error": str(e)}
            out.append({"doctrine": d, "ok": bool(r.get("ok")),
                        "stored": r.get("stored"), "note": r.get("note"),
                        "error": r.get("error")})
            if r.get("ok"):
                done.add(key)
        return {"ok": True, "attempts": out}

    def _current_fuel(self, tag=None):
        """只读获取国家当前燃油量；无法读取时返回 None（不误触低油保护）。"""
        try:
            co = self._c(tag) if hasattr(self, "_c") else (self.player() if hasattr(self, "player") else None)
            if co is None:
                return None
            fuel = getattr(co, "fuel", None)
            if fuel is None and hasattr(co, "summary"):
                fuel = (co.summary() or {}).get("fuel")
            return float(fuel) if fuel is not None else None
        except Exception:
            return None

    def _peacetime_fuel_conserve(self, tag=None):
        """判断当前是否应遵守和平期节油策略（默认和平期 conserve，不主动升空/出海烧油）。

        2026-11-26 (B9 根治): 1936 开局处于和平期时，housekeep 若自动把 14 个空军联队
        置 +0xA4=1 并把海军挂北海巡逻，会在无任何战事时把油库烧穿并反复触发 FUEL_ZERO。
        仅在已开战或 Standing Orders 显式设 fuel.peacetime_mode="normal" 时才自动升空出海；
        若无法读取战争状态（如最小离线 mock 桩），不盲目拦截。
        """
        # 1) 可靠判定当前是否处于战争状态（明确读到才判定，不可读则返回 unknown 放行）
        war_known = False
        at_war = False
        try:
            import combat as _cb
            if hasattr(_cb, "at_war"):
                # 若未 monkey-patch combat.at_war，则需具备底层 p 才调用以免在空桩上误返 False
                _is_patched = getattr(_cb.at_war, "__name__", "") != "at_war"
                if _is_patched or getattr(self, "p", None) is not None:
                    at_war = bool(_cb.at_war(self, tag))
                    war_known = True
        except Exception:
            pass
        if not war_known or not at_war:
            try:
                if hasattr(self, "wars"):
                    _w_fn = getattr(self, "wars")
                    if getattr(self, "p", None) is not None or not hasattr(_w_fn, "__func__"):
                        w = _w_fn(tag)
                        if isinstance(w, dict):
                            war_known = True
                            if w.get("enemies") or w.get("at_war"):
                                at_war = True
            except Exception:
                pass
        if not war_known:
            return False, "unknown"
        if at_war:
            return False, "at_war"
        # 2) 读取 Standing Orders 当前阶段的 fuel.peacetime_mode（默认 conserve）
        mode = "conserve"
        try:
            import autopilot as _ap
            st = _ap.get_active_state(self, tag=tag) if hasattr(_ap, "get_active_state") else None
            if isinstance(st, dict) and st.get("active"):
                prof = st.get("profile") or {}
                cur_ph = st.get("current_phase")
                stages = prof.get("stages") or {}
                st_cfg = stages.get(cur_ph) if isinstance(stages, dict) and cur_ph in stages else prof
                fl = (st_cfg or {}).get("fuel") or {}
                if isinstance(fl, dict) and fl.get("peacetime_mode"):
                    mode = str(fl["peacetime_mode"]).strip().lower()
        except Exception:
            mode = "conserve"
        return (mode != "normal"), mode

    def housekeep_air(self, tag=None, force=False):
        """Air force: give the wings a mission AND attach them to the armies.

        2026-11-17: neither had ever been done in any campaign (the 1940 run ended
        with 15 wings at mission 0 and 0 attached).
        2026-11-23 (复测 9 D-4): 燃油枯竭（<= 10）时跳过挂空军任务，避免与 FUEL_ZERO 互相打架。
        2026-11-26 (B9 根治): 和平期默认遵循节油策略（fuel.peacetime_mode="conserve"），
        仅执行 0 油耗的附着陆军集团军（attach_air_wings_to_armies），不给联队挂作战升空任务；
        开战后或传 force=True 自动正常挂满任务。
        """
        fuel = self._current_fuel(tag)
        try:
            import equipmeta as _em
            if _em.is_fuel_critical(fuel):
                return {
                    "ok": True,
                    "skipped_reason": "fuel_critical",
                    "fuel": round(float(fuel), 1),
                    "note": "检测到当前燃油枯竭（%.1f <= %.0f），已跳过指派空军任务，避免重新触发 FUEL_ZERO"
                            % (float(fuel), _em.FUEL_CRITICAL_THRESHOLD),
                }
        except Exception:
            pass
        conserve, pmode = (False, "forced") if force else self._peacetime_fuel_conserve(tag)
        out = {}
        if conserve:
            out["missions"] = {
                "ok": True,
                "skipped": True,
                "skipped_reason": "peacetime_fuel_conservation",
                "peacetime_mode": pmode,
                "note": "当前处于和平期（peacetime_mode=%s），已跳过升空作战任务以节约燃油；仅执行 0 油耗空军附着陆军。" % pmode,
            }
            out["skipped_reason"] = "peacetime_fuel_conservation"
        else:
            try:
                out["missions"] = self.set_air_missions(tag)
            except Exception as e:
                out["missions"] = {"ok": False, "error": str(e)}
        try:
            out["attach"] = self.attach_air_wings_to_armies(tag)
        except Exception as e:
            out["attach"] = {"ok": False, "error": str(e)}
        out["ok"] = bool((out.get("missions") or {}).get("ok")
                         or (out.get("attach") or {}).get("ok"))
        return out

    def housekeep_navy(self, tag=None, force=False):
        """Navy: give every idle combat task force a Patrol mission.

        2026-11-22 (复测 5 P1 §3.1): 严禁触碰预备舰队（is_reserve / mission==8）。
        反汇编证实 CNavalMissionAddRegionCommand::Execute 对预备舰队调用 0x0DBA810
        将其转正改名为“第N舰队”并把 TF 的 mission 从 8 重置为 0，随后引擎下次下水新船
        又建新预备舰队，导致每次 housekeep 舰队数+1 且永远留下一支 mission=0 的空壳 TF。
        现改为：跳过所有预备舰队，仅对非预备且存在 mission==0 作战 TF 的舰队补海域与任务，
        并保证 fleets[] 与 still_idle 口径完全一致。
        2026-11-23 (复测 9 D-4): 燃油枯竭（<= 10）时严禁自动给舰队补海域与挂巡逻，
        避免覆盖玩家手动停船省油的操作并重新触发 FUEL_ZERO 红点。
        2026-11-26 (B9 根治): 和平期默认遵循节油策略（fuel.peacetime_mode="conserve"），
        不主动派作战舰队出海巡逻，避免开局即烧空德国油库；开战后或传 force=True 照常出击。
        """
        import sys
        out = {"ok": True, "fleets": [], "assigned": 0, "posted": 0, "skipped": [],
               "reserve_fleets_skipped": [], "regions_added": [], "still_idle": []}
        fuel = self._current_fuel(tag)
        try:
            import equipmeta as _em
            if _em.is_fuel_critical(fuel):
                out.update({
                    "ok": True,
                    "skipped_reason": "fuel_critical",
                    "fuel": round(float(fuel), 1),
                    "note": "检测到当前燃油枯竭（%.1f <= %.0f），已跳过为舰队补海区与挂巡逻，避免覆盖停船节油并重新触发 FUEL_ZERO"
                            % (float(fuel), _em.FUEL_CRITICAL_THRESHOLD),
                })
                return out
        except Exception:
            pass
        conserve, pmode = (False, "forced") if force else self._peacetime_fuel_conserve(tag)
        if conserve:
            out.update({
                "ok": True,
                "skipped_reason": "peacetime_fuel_conservation",
                "peacetime_mode": pmode,
                "note": "当前处于和平期（peacetime_mode=%s），已跳过派舰队出海巡逻以节约燃油（开战后或传 force=True 自动恢复）。" % pmode,
            })
            return out
        if sys.platform != "win32":
            out["ok"] = False
            out["error"] = "navy housekeep is the Windows path"
            return out
        try:
            import navalops_win as NW
            fleets = NW.naval_fleets(self, tag)
        except Exception as e:
            out["ok"] = False
            out["error"] = "%s: %s" % (type(e).__name__, e)
            return out

        def _is_reserve_fleet(f):
            if f.get("is_reserve"):
                return True
            tfs_f = f.get("task_forces") or []
            return bool(tfs_f and int(tfs_f[0].get("mission") or 0) == 8)

        def _is_reserve_tf(tf):
            return bool(tf.get("is_reserve") or int(tf.get("mission") or 0) == 8)

        def _eff_mission(tf):
            # 2026-11-22 (复测 5 §9.3 残留 A): 当 TF 正在活动（tf+0x4B8 非 0/4）时，
            # 引擎把任务挂在 tf+0x4F0 (queued_mission)；已排队即已受命，不再误判为空闲。
            return int(tf.get("effective_mission") or tf.get("mission") or tf.get("queued_mission") or 0)

        combat_fleets = []
        for f in fleets:
            if _is_reserve_fleet(f):
                out["reserve_fleets_skipped"].append({
                    "fleet": f.get("fleet_name"),
                    "handle": f.get("handle"),
                    "task_forces": len(f.get("task_forces") or []),
                    "why": "reserve fleet (mission==8); left intact to absorb newly built ships",
                })
                continue
            combat_fleets.append(f)

        _need_region = any(
            not (f.get("regions") or 0)
            and any((not _is_reserve_tf(tf)) and not _eff_mission(tf)
                    for tf in (f.get("task_forces") or []))
            for f in combat_fleets
        )
        _region_pick = None
        if _need_region:
            try:
                import navalregions as NR
                _valid = NW.valid_regions(self)
                _rows = NR.catalogue(_valid)
                _pref = ("north_sea", "baltic_sea", "english_channel",
                         "mediterranean", "atlantic_ocean")
                _by_key = {r.get("name_key"): r for r in _rows if r.get("usable_now")}
                for _k in _pref:
                    if _k in _by_key:
                        _region_pick = int(_by_key[_k]["id"])
                        break
                if _region_pick is None:
                    _use = [r for r in _rows if r.get("usable_now")]
                    if _use:
                        _region_pick = int(_use[0]["id"])
            except Exception:
                _region_pick = None

        def _tfkey(fleet, tf):
            h = tf.get("handle")
            return (fleet, tuple(h) if isinstance(h, (list, tuple)) else h)

        _before = {}
        for f in combat_fleets:
            for tf in (f.get("task_forces") or []):
                if _is_reserve_tf(tf):
                    continue
                _before[_tfkey(f.get("fleet_name"), tf)] = _eff_mission(tf)

        for f in combat_fleets:
            tfs_combat = [tf for tf in (f.get("task_forces") or []) if not _is_reserve_tf(tf)]
            idle_tfs = [tf for tf in tfs_combat if not _eff_mission(tf)]
            row = {"fleet": f.get("fleet_name"), "regions": f.get("regions"),
                   "task_forces": len(tfs_combat), "set": []}
            # 仅当非预备舰队确实有空闲作战 TF 且尚未分配海域时，才补海域
            if idle_tfs and not (f.get("regions") or 0) and _region_pick:
                try:
                    _fh = int(f.get("packed_handle") or 0, 16) \
                        if isinstance(f.get("packed_handle"), str) else f.get("packed_handle")
                    _ar = NW.add_region(self, _fh, _region_pick, tag)
                    if _ar.get("ok"):
                        f["regions"] = _ar.get("regions_after") or 1
                        out["regions_added"].append({"fleet": f.get("fleet_name"),
                                                     "region": _region_pick})
                        row["regions"] = f["regions"]
                except Exception as e:
                    row["region_error"] = "%s: %s" % (type(e).__name__, e)
            for tf in idle_tfs:
                if not (f.get("regions") or 0):
                    out["skipped"].append({"fleet": f.get("fleet_name"),
                                           "tf": tf.get("handle"),
                                           "why": "fleet has no strategic region"})
                    continue
                try:
                    packed = int(tf.get("packed_handle") or 0, 16) \
                        if isinstance(tf.get("packed_handle"), str) else tf.get("packed_handle")
                    r = NW.set_mission(self, packed or tf.get("handle"), 1, tag)
                    row["set"].append({"tf": tf.get("handle"),
                                       "ok": r.get("ok"), "error": r.get("error"),
                                       "mission_after": r.get("mission_after"),
                                       "queued_mission_after": r.get("queued_mission_after"),
                                       "queued": r.get("queued")})
                    out["posted"] += 1
                except Exception as e:
                    row["set"].append({"tf": tf.get("handle"), "ok": False,
                                       "error": "%s: %s" % (type(e).__name__, e)})
            out["fleets"].append(row)

        # ---- truthful delta, from a fresh read (excluding reserve fleets) ----
        try:
            import time as _time
            _deadline = _time.time() + 3.0
            while True:
                _after_rows = [f for f in NW.naval_fleets(self, tag) if not _is_reserve_fleet(f)]
                _after = {}
                for f in _after_rows:
                    for tf in (f.get("task_forces") or []):
                        if _is_reserve_tf(tf):
                            continue
                        _after[_tfkey(f.get("fleet_name"), tf)] = _eff_mission(tf)
                _armed_before = sum(1 for v in _before.values() if v)
                _armed_after = sum(1 for v in _after.values() if v)
                _idle = [k for k, v in _after.items() if not v]
                if not _idle or not out.get("posted") or _time.time() >= _deadline:
                    break
                _time.sleep(0.5)
            out["fleets_count_before"] = len(fleets)
            out["fleets_count_after"] = len(NW.naval_fleets(self, tag))
            out["task_forces_total"] = len(_after)
            out["task_forces_armed"] = _armed_after
            out["task_forces_armed_before"] = _armed_before
            out["assigned"] = max(0, _armed_after - _armed_before)
            out["still_idle"] = [{"fleet": k[0], "tf": list(k[1]) if isinstance(k[1], tuple) else k[1]}
                                 for k in _idle]
            for row in out["fleets"]:
                for s in row["set"]:
                    _k = (row["fleet"], tuple(s["tf"]) if isinstance(s["tf"], (list, tuple)) else s["tf"])
                    if _k in _after:
                        s["mission_after"] = _after[_k]
        except Exception as e:
            out["reread_error"] = "%s: %s" % (type(e).__name__, e)
        if "task_forces_total" in out:
            out["ok"] = (not out.get("still_idle")) and (
                bool(out["task_forces_total"]) or not combat_fleets
            )
        else:
            out["ok"] = (not out["skipped"])
        if out.get("still_idle"):
            _p = False
            try:
                import session as _ss2
                _p = bool(_ss2.is_paused(self))
            except Exception:
                _p = False
            out["note"] = ("%d combat task force(s) still have no mission; %s"
                           % (len(out["still_idle"]),
                              "the game is paused, so queued commands cannot run yet"
                              if _p else
                              "the commands are queued and the engine applies them on the next tick"))
        return out

    def housekeep(self, tag=None, target_construction=None, techs=None, budget_s=40.0):
        import time as _time
        t0 = _time.monotonic()
        budget = float(budget_s or 40.0)
        try:
            _d0 = self.date() if hasattr(self, "date") else {}
        except Exception:
            _d0 = {}
        if not isinstance(_d0, dict):
            _d0 = {}
        out = {"ok": True, "timings": {}}
        budget_exceeded = False
        # 2026-11-22 (复测 5 §3.3 / §3.4):
        # 1) 将最便宜、最关键的 `research` 提前至 `politics` 之前（construction/production/doctrines
        #    之后已完成缓存预热，不再被 26s 的 politics 挤掉）；
        # 2) `politics` 限制单步软预算（默认 12s，且不超过剩余总预算的一半）；
        # 3) 顶层清晰拆分 `failed_steps`、`skipped_steps`、`succeeded_steps`、`partial` 与 `summary`。
        steps = (
            ("construction", lambda: self.housekeep_construction(tag, target_construction)),
            ("production", lambda: self.housekeep_production(tag)),
            ("doctrines", lambda: self.housekeep_doctrines(tag)),
            ("research", lambda: self.housekeep_research(tag, techs)),
            ("politics", lambda: self.housekeep_politics(
                tag, budget_s=min(12.0, max(4.0, (budget - (_time.monotonic() - t0)) * 0.45))
            )),
            ("recruitment", lambda: self.housekeep_recruitment(tag)),
            ("divisions", lambda: self.housekeep_divisions(tag)),
            ("air", lambda: self.housekeep_air(tag)),
            ("navy", lambda: self.housekeep_navy(tag)),
        )
        step_names = [nm for nm, _ in steps]
        for name, fn in steps:
            if (_time.monotonic() - t0) >= max(5.0, budget - 2.0):
                budget_exceeded = True
                out[name] = {"ok": False, "skipped": "time_budget_exceeded",
                             "elapsed_s": round(_time.monotonic() - t0, 2)}
                continue
            ts = _time.monotonic()
            try:
                r = fn()
            except Exception as e:
                import traceback
                r = {"ok": False, "error": str(e),
                     "trace": traceback.format_exc()[-1200:]}
            out["timings"][name] = round(_time.monotonic() - ts, 2)
            out[name] = r
        # 若首次科研因冷库失败（且非环境错误），再补一次重试
        _res_r = out.get("research") or {}
        _res_env_fail = (
            _res_r.get("failure_class") == "environment"
            or _res_r.get("blocked_by") in ("foreground", "process_gone", "timeout", "injector_error")
        )
        if (not budget_exceeded
                and not _res_r.get("ok")
                and not _res_env_fail
                and not _res_r.get("skipped")
                and (_time.monotonic() - t0) < max(5.0, budget - 3.0)):
            ts = _time.monotonic()
            try:
                r = self.housekeep_research(tag, techs)
                out["timings"]["research_retry"] = round(_time.monotonic() - ts, 2)
                out["research_retry"] = r
                if r.get("ok"):
                    out["research"] = r
            except Exception as e:
                out["research_retry"] = {"ok": False, "error": str(e)}

        succeeded_steps = []
        failed_steps = []
        skipped_steps = []
        for nm in step_names:
            sr = out.get(nm)
            if not isinstance(sr, dict):
                continue
            if sr.get("skipped") == "time_budget_exceeded":
                skipped_steps.append(nm)
            elif sr.get("ok"):
                succeeded_steps.append(nm)
            else:
                failed_steps.append(nm)

        all_ok = (not failed_steps) and (not skipped_steps)
        out["ok"] = all_ok
        out["all_ok"] = all_ok
        out["partial"] = bool(succeeded_steps and (failed_steps or skipped_steps))
        out["succeeded_steps"] = succeeded_steps
        out["failed_steps"] = failed_steps
        out["skipped_steps"] = skipped_steps
        # 2026-11-23 (复测 10 B-4): 保留自动花 PP 运营托管，但在顶层摘要明确透明汇报 PP 变动与所购顾问/MIO
        _pol_r = out.get("politics") if isinstance(out.get("politics"), dict) else {}
        _pp_delta = _pol_r.get("pp_delta")
        _pol_bought = [
            x.get("idea_slot") or x.get("law") or x.get("decision")
            for x in (_pol_r.get("actions") or [])
            if isinstance(x, dict) and x.get("ok") and (x.get("idea_slot") or x.get("law") or x.get("decision"))
        ]
        if _pp_delta:
            out["pp_delta"] = _pp_delta
        if _pol_bought:
            out["politics_purchased"] = _pol_bought
        out["summary"] = (
            "%d/%d steps ok" % (len(succeeded_steps), len(step_names))
            + ("; pp_delta=%s (%s)" % (_pp_delta, ", ".join(str(x) for x in _pol_bought)) if _pp_delta else "")
            + ("; failed: %s" % ", ".join(failed_steps) if failed_steps else "")
            + ("; skipped: %s" % ", ".join(skipped_steps) if skipped_steps else "")
        )
        out["elapsed_s"] = round(_time.monotonic() - t0, 2)
        try:
            _d1 = self.date() if hasattr(self, "date") else {}
        except Exception:
            _d1 = {}
        if not isinstance(_d1, dict):
            _d1 = {}
        _h0, _h1 = _d0.get("raw"), _d1.get("raw")
        _adv_h = max(0, int(_h1) - int(_h0)) if (_h0 is not None and _h1 is not None) else 0
        out["date_before"] = _d0.get("text")
        out["date_after"] = _d1.get("text")
        out["clock_advanced_hours"] = _adv_h
        if _adv_h > 0:
            out["clock_note"] = (
                "game clock advanced by %d hour(s) during post_drained command execution "
                "(HOI4 requires unpaused ticks to consume CCommandQueue)" % _adv_h
            )
        if budget_exceeded:
            out["budget_exceeded"] = True
            out["note"] = ("hit %.0fs soft budget (kept safely under the 50s MCP watchdog); "
                           "call hoi4_housekeep once more to finish skipped steps" % budget)
        return out

    def candidates(self, tag=None):
        """Structured IDs the model can use directly, one overview call."""
        out = {}
        try:
            sel = self.selectable_focuses(tag)
            out["recommended_focus"] = sel.get("recommended")
            out["selectable_focuses_top5"] = [x["id"] for x in sel.get("selectable", [])[:5]]
        except Exception:
            out["recommended_focus"] = None
            out["selectable_focuses_top5"] = []
        try:
            slots = self.research_slots(tag)
            out["empty_research_slots"] = [s["slot"] for s in slots if not s.get("slot_busy")]
            out["recommended_techs"] = self.available_research(tag)[:12]
        except Exception:
            out["empty_research_slots"] = []
            out["recommended_techs"] = []
        try:
            out["recommended_build_states"] = self._state_priority(tag)[:4]
        except Exception:
            out["recommended_build_states"] = []
        try:
            out["production_lines"] = [
                {"line_index": l["index"], "equipment": l.get("equipment"),
                 "factories": l.get("factories")}
                for l in self.production_lines(tag)
                if l.get("class") == "CMilitaryProductionLine"][:8]
        except Exception:
            out["production_lines"] = []
        try:
            out["available_decisions"] = [d["key"] for d in self.decisions(tag, "visible", rich=False)][:12]
        except Exception:
            out["available_decisions"] = []
        try:
            out["recommended_ideas"] = [i["id"] for i in self.recommended_ideas(tag)["ideas"]][:10]
        except Exception:
            out["recommended_ideas"] = []
        # 2026-11-06: expose the unconventional-breakout arsenal so the model knows
        # what exists when a front stalls or a minor nation is being played.
        try:
            import playbooks
            enemies = []
            try:
                import combat
                enemies = list(combat.enemy_tags(self) or [])
            except Exception:
                enemies = []
            out["breakout_tactics_guide"] = playbooks.get_breakout_tactics(
                self._c(tag).tag, year=int(self.date().get("year") or 1936),
                active_enemy_tags=enemies)
        except Exception as e:
            out["breakout_tactics_guide"] = {"error": str(e)}
        return out

    def alerts(self, tag=None):
        """Human-readable red-dot summary for hoi4_overview."""
        out = []
        try:
            if not self.current_focus(tag):
                sel = self.selectable_focuses(tag)
                if sel.get("recommended"):
                    out.append("FOCUS_IDLE: 国策槽空置，推荐 %s (%s)" %
                               (sel["recommended"], sel.get("recommended_reason") or ""))
        except Exception:
            pass
        try:
            slots = self.research_slots(tag)
            empty = [s["slot"] for s in slots if not s.get("slot_busy")]
            if empty:
                cands = self.available_research(tag)[:3]
                out.append("RESEARCH_IDLE: %d 个科研槽空置 (slot %s)，推荐 %s" %
                           (len(empty), ", ".join(map(str, empty)), ", ".join(cands)))
        except Exception:
            pass
        try:
            pp = self.politics(tag)["political_power"]
            if pp >= 160:
                out.append("PP_HIGH: 政治点数 %d，可切换法案/任命顾问或点决议" % pp)
        except Exception:
            pass
        try:
            q = len(self.construction_queue(tag))
            if q < 6:
                out.append("CONSTRUCTION_LOW: 建造队列仅 %d 项，民工即将闲置" % q)
        except Exception:
            pass
        try:
            n = len(self.unassigned_divisions(tag))
            if n:
                out.append("UNASSIGNED_DIVS: %d 个师未编入集团军" % n)
        except Exception:
            pass
        try:
            lines = self.production_lines(tag)
            idle_mil = None
            if hasattr(self, "factory_allocation"):
                _fa = self.factory_allocation(tag)
                if isinstance(_fa, dict) and _fa.get("source") == "engine_production_status":
                    idle_mil = int(_fa.get("military_idle") or 0)
            if idle_mil is None:
                total = self._c(tag).summary().get("military_factories") or 0
                used = sum(int(l.get("factories") or 0) for l in lines
                           if l.get("class") == "CMilitaryProductionLine")
                idle_mil = max(0, total - used)
            if idle_mil >= 2:
                out.append("MIL_FACTORY_IDLE: %d 座军工厂闲置未分配" % idle_mil)
        except Exception:
            pass
        return out


