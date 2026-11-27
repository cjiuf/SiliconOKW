"""自动划前线（plan_front）、补给容量护栏与集团军战斗计划批量激活领域 Mixin。"""
from __future__ import annotations

import sys

class BattleplanMixin:
    """自动划前线（plan_front）、补给容量护栏与集团军战斗计划批量激活领域 Mixin。"""

    def _best_front_segment(self, segs, divisions=None):
        """Pick the front segment closest to our own divisions.

        2026-11-20: the old rule was `max(segs, key=len(border_provinces))`.
        After Belgium was annexed that picked the Belgian Congo border with
        French Equatorial Africa -- the longest border on the map -- and sent
        four divisions to state 977 instead of the French border.  The walk is
        land-only, so an overseas front stays at 9999 and is chosen only when
        we really have nothing closer.
        """
        import collections
        import mapdata
        import unitorders
        starts = []
        for d in (divisions or []):
            try:
                pv = unitorders.province_of(self, d)
            except Exception:
                pv = None
            if pv:
                starts.append(pv)
        dist = {}
        if starts:
            adj = mapdata.build_adjacency()
            dist = {s: 0 for s in starts}
            q = collections.deque(starts)
            while q:
                u = q.popleft()
                for v in adj.get(u, ()):
                    if v not in dist:
                        dist[v] = dist[u] + 1
                        q.append(v)
        best, best_key = None, None
        for s in segs:
            if dist:
                d = min((dist.get(p, 9999) for p in s["border_provinces"]), default=9999)
            else:
                d = 0
            key = (d, -len(s["border_provinces"]))
            if best_key is None or key < best_key:
                best, best_key = s, key
        return best or (segs[0] if segs else None)

    def plan_front(self, enemy_tag, tag=None, armies=None, split=True, depth=2,
                   force=False, wait_seconds=0, dry_run=False):
        """Native battle plan with ONE FRONT LINE PER ARMY (2026-11-17).

        Why this exists
        ---------------
        `_win_battleplans` merged every division into a single NEW army and drew a
        single front line over the whole CFront.  In the 1940 campaign that is
        exactly how the German army became one 141-division blob: the engine
        spread it over a handful of border provinces (over-stacked, out of supply,
        attacks refused) while the other theatre stood empty and the French walked
        in.  A player never does that -- a player draws one front line per army.

        This keeps the armies that already exist and gives each one a contiguous
        slice of the front (`from`/`to` are 0..100000 of the section) plus its own
        offensive line, so the engine's own front logic spreads each army along
        ITS slice.  That is what stops the stacking.
        """
        import time
        if sys.platform != "win32":
            return {"ok": False, "error": "plan_front is the Windows native-plan path"}
        import armyops_win as A
        import armyops
        p = self.p
        enemy_tag = str(enemy_tag or "").upper()
        if not enemy_tag:
            return {"ok": False, "error": "no enemy tag"}
        co = self._c(tag)
        out = {"enemy_tag": enemy_tag, "backend": "windows", "steps": {}}
        # 2026-11-18：画前线是和平时期的动作，不再要求「已开战」。旧门禁逼着调用方
        # 先宣战再画线，正好把事情做反了 —— 1939.07.30 那局就是这么在 8 个师、
        # 0 条命令的状态下开打的。`force` 保留它原来的另一个含义：跳过补给闸。
        try:
            enemies = (self.wars(tag) or {}).get("enemies") or []
        except Exception:
            enemies = []
        out["at_war"] = enemy_tag in enemies

        segs = A.fronts_towards(self, enemy_tag, co.tag)
        if not segs:
            return {"ok": False, "error": "no front section faces %s" % enemy_tag}
        # 2026-11-17: the supply guard the Linux path always had. A battle plan
        # commits whole armies to a front; if that front cannot feed them the
        # engine spreads them anyway and every division degrades at once --
        # exactly the "stacked, no supply, cannot attack" state of 1940-07.
        # Instead of refusing (the old behaviour) we only commit as many armies as
        # the front can actually feed, biggest first.
        guard = self._supply_front_guard(enemy_tag, tag)
        out["steps"]["supply"] = guard
        try:
            _cand_divs = [int(d["ptr"], 16) for d in (self.divisions(tag) or {}).get("divisions", [])]
        except Exception:
            _cand_divs = []
        best = self._best_front_segment(segs, _cand_divs)
        sub_from = int(best.get("slice_from", 0) or 0)
        sub_to = int(best.get("slice_to", A.FULL) or A.FULL)
        if sub_to <= sub_from:
            sub_from, sub_to = 0, A.FULL
        out["steps"]["front"] = {"handle": best["front_handle"],
                                 "section": best["section"],
                                 "border": len(best["border_provinces"]),
                                 "slice_range": [sub_from, sub_to],
                                 "sections_facing_enemy": len(segs),
                                 **({"front_area": best["front_area"]} if best.get("front_area") else {})}
        # 2026-11-26 (B1 根治): 用所选 CFront+0x70 敌方控制区按引擎 0xD57D90 语义预过滤目标省；
        # 若 depth=1 过滤后为空（如小国只剩沿线 1 排省），依次回退 depth=0 -> 全部匹配敌方实控省，
        # 彻底杜绝把不匹配/已占领省塞给 CompletePlan 导致 children=[]！
        front_ctx = 0
        try:
            _fp = int(best.get("front") or 0)
            if _fp > 0x10000:
                front_ctx = int(p.u64(_fp + 0x70))
        except Exception:
            front_ctx = 0
        targets = A.target_provinces(self, enemy_tag, depth=1, own_tag=co.tag)
        if front_ctx > 0x10000 and targets:
            _og_hint = None
            try:
                _ogs_h = armyops.orders_groups(self, co)
                if _ogs_h:
                    _og_hint = _ogs_h[0]
            except Exception:
                _og_hint = None
            _filtered = A.filter_front_provinces(self, targets, front_ctx, og=_og_hint)
            if not _filtered:
                _t0 = A.target_provinces(self, enemy_tag, depth=0, own_tag=co.tag)
                _filtered = A.filter_front_provinces(self, _t0, front_ctx, og=_og_hint)
            if not _filtered and hasattr(A, "_live_side_provinces"):
                try:
                    _m_live, _e_live, _ = A._live_side_provinces(self, co.tag, enemy_tag)
                    _filtered = A.filter_front_provinces(self, sorted(_e_live), front_ctx, og=_og_hint)
                except Exception:
                    _filtered = []
            if _filtered:
                targets = _filtered
        out["steps"]["targets"] = targets[:20]
        if not targets:
            return {"ok": False, "error": "no enemy province %d steps in" % depth,
                    "front": out["steps"]["front"]}

        all_og = [og for og in armyops.orders_groups(self, co)
                  if not p.u8(og + A.OG_IS_GROUP)]
        # 2026-11-26 (P1-2 根治): 区分 type==2 陆地前线单与 type in (3, 4) 特种订单（登陆/伞降）。
        # 只要集团军没有 type==2 前线单、仅挂了伞降/登陆单且还有未绑定特种单的自由陆战师，
        # 就绝不把它整军跳过，而是自动排除已绑定特种单的师（保护伞兵不被抢走）并让剩余师正常画前线！
        special_reserved = {}
        special_summary = []

        def _collect_special_reserved(og_ptr):
            res_set = set()
            spec_ords = []
            try:
                ords = A.orders(self, og_ptr) or []
            except Exception:
                ords = []
            for _o in ords:
                if _o.get("type") in (3, 4):
                    spec_ords.append(_o)
                    if hasattr(A, "order_assigned_divisions"):
                        try:
                            res_set |= set(A.order_assigned_divisions(self, og_ptr, _o["id"]) or ())
                        except Exception:
                            pass
            return ords, spec_ords, res_set

        if armies:
            want = {int(a, 16) if isinstance(a, str) else int(a) for a in armies}
            all_og = [og for og in all_og if og in want]
            for og in all_og:
                _, spec_ords, res_set = _collect_special_reserved(og)
                if spec_ords and res_set:
                    special_reserved[og] = res_set
                    special_summary.append({
                        "army": hex(og),
                        "special_orders": [{"id": x.get("id"), "type": x.get("type")} for x in spec_ords],
                        "reserved_special_divisions": len(res_set),
                    })
        else:
            # 2026-11-17: never strip another front. An army that already carries a
            # front order belongs to a different war; only armies with no front orders are
            # freely available.
            # 2026-11-23 (复测 10 A-4 根治): 当所有集团军都已有前线订单（free=[]）时，
            # 严禁把所有军（例如正守 FRA 马奇诺防线的 6 个军）一并抓来重排！只允许重排
            # 现有前线订单**本来就面向本敌国 enemy_tag** 的集团军；若没有任何空闲军也
            # 没有任何面向 enemy_tag 的军，明确拒绝并请调用方显式传 armies=[...]。
            busy = []
            for og in list(all_og):
                ords, spec_ords, res_set = _collect_special_reserved(og)
                if not ords:
                    continue
                has_front = any(x.get("type") == 2 for x in ords)
                if has_front:
                    busy.append(og)
                    continue
                # 无 type==2 前线单，仅有登陆/伞降等特种单：检查是否还有未被特种单占用的自由陆战师
                if spec_ords:
                    try:
                        og_divs = list(A.divisions_of(self, og) or ())
                    except Exception:
                        og_divs = []
                    free_divs = [d for d in og_divs if d not in res_set]
                    if free_divs:
                        special_reserved[og] = res_set
                        special_summary.append({
                            "army": hex(og),
                            "special_orders": [{"id": x.get("id"), "type": x.get("type")} for x in spec_ords],
                            "reserved_special_divisions": len(res_set),
                            "available_front_divisions": len(free_divs),
                        })
                        continue
                    # 整军所有师都已挂在特种单上，不打扰
                    busy.append(og)
                else:
                    busy.append(og)
            free = [og for og in all_og if og not in busy]
            out["steps"]["armies_with_existing_orders"] = [hex(o) for o in busy]
            if special_summary:
                out["steps"]["armies_with_special_orders"] = special_summary
            # 2026-11-23 (复测 11 P1-2 加固): 即使集团军当前没有前线订单（free，例如刚
            # reset_battleplans 后或手工驻防在 FRA 马奇诺边境的 6 个军），只要其师实站
            # 在其他邻国边境且不贴着本敌国 enemy_tag 边境，绝不能被 set_front_directive(enemy_tag)
            # 一把抽去给 HOL/BEL 画切片与进攻线！
            guarded_other = []
            eligible_free = list(free)
            if free:
                try:
                    import mapdata as _md_fr
                    import unitorders as _uo_fr
                    _owners_fr, _ = _md_fr.province_owner_map(self)
                    _adj_fr = _md_fr.build_adjacency()
                    _, _ptype_fr = _md_fr.load_definitions()
                    _me_fr = str(co.tag).upper()
                except Exception:
                    _owners_fr, _adj_fr, _ptype_fr, _me_fr = {}, {}, {}, str(co.tag).upper()
                if _owners_fr and _adj_fr:
                    _keep_free = []
                    _incidental_other = []
                    for og in free:
                        _touch_target = 0
                        _touch_other = set()
                        _other_div_n = 0
                        _elig_div_n = 0
                        _res_s = special_reserved.get(og) or set()
                        try:
                            for _dp in A.divisions_of(self, og):
                                # 2026-11-26 (P2-6 根治): 已绑定特种单（伞降/登陆）的师不计入常规边境驻防判定
                                if _dp in _res_s:
                                    continue
                                _elig_div_n += 1
                                _pv = _uo_fr.province_of(self, _dp)
                                if _pv is None:
                                    continue
                                _nbs = set()
                                for _nb in _adj_fr.get(int(_pv), ()):
                                    if _ptype_fr and _ptype_fr.get(_nb) != "land":
                                        continue
                                    _nbo = _owners_fr.get(_nb)
                                    if _nbo and _nbo != _me_fr:
                                        _nbs.add(str(_nbo).upper())
                                if enemy_tag in _nbs:
                                    _touch_target += 1
                                elif _nbs:
                                    _other_div_n += 1
                                    _touch_other.update(_nbs)
                        except Exception:
                            pass
                        # 2026-11-26 (P2-6 根治): 只有当该军显著驻防在他国边境（例如守马奇诺防线）
                        # 才视为他国边防军禁止自动抽调；绝不因 32 师大军里仅 1 个路过师贴着 BEL 边境
                        # 就整军跳过！门槛：小军(<=4师)至少半数（1师须全员1/1），大军(>=5师)至少 max(3, ceil(40%)) 师。
                        _guard_min = (
                            max(1, (_elig_div_n + 1) // 2)
                            if _elig_div_n <= 4
                            else max(3, (_elig_div_n * 2 + 4) // 5)
                        )
                        if _touch_target == 0 and _elig_div_n > 0 and _other_div_n >= _guard_min:
                            guarded_other.append({
                                "army": hex(og),
                                "bordering": sorted(_touch_other),
                                "border_divisions": _other_div_n,
                                "eligible_divisions": _elig_div_n,
                            })
                        else:
                            if _touch_target == 0 and _other_div_n > 0:
                                _incidental_other.append({
                                    "army": hex(og),
                                    "bordering": sorted(_touch_other),
                                    "border_divisions": _other_div_n,
                                    "eligible_divisions": _elig_div_n,
                                    "threshold": _guard_min,
                                })
                            _keep_free.append(og)
                    eligible_free = _keep_free
                    if _incidental_other:
                        out["steps"]["incidental_other_border_armies"] = _incidental_other
                if guarded_other:
                    out["steps"]["free_armies_guarding_other_borders"] = guarded_other
            if eligible_free:
                all_og = eligible_free
            elif busy:
                border_set = set(best.get("border_provinces") or [])
                best_fh = tuple(best.get("front_handle") or ())
                best_sec = int(best.get("section") or -1)
                facing_enemy = []
                busy_summary = []
                for og in busy:
                    og_ords = []
                    try:
                        og_ords = A.orders(self, og) or []
                    except Exception:
                        og_ords = []
                    matches_enemy = False
                    for _ord in og_ords:
                        if _ord.get("type") != 2:
                            continue
                        # 检查订单自身的己方边境省 (+0x70) 是否落在本敌国边境集合内
                        _op = int(_ord["ptr"], 16) if isinstance(_ord.get("ptr"), str) else int(_ord.get("ptr") or 0)
                        _oprovs = []
                        if _op > 0x10000:
                            try:
                                _d0 = p.u64(_op + 0x70)
                                _n0 = p.i32(_op + 0x7C)
                                if 0x10000 < _d0 < 0x7F0000000000 and 0 < _n0 < 512:
                                    _oprovs = [p.i32(_d0 + 4 * _k) for _k in range(_n0)]
                            except Exception:
                                _oprovs = []
                        if _oprovs and border_set:
                            # 至少一半或 >=2 个省落在目标敌国边境省上，避免纯交界点误抓马奇诺军
                            hit_n = sum(1 for pv in _oprovs if pv in border_set)
                            if hit_n >= 2 or (hit_n > 0 and hit_n * 2 >= len(_oprovs)):
                                matches_enemy = True
                                break
                        elif (tuple(_ord.get("root_front") or ()) == best_fh
                              and int(_ord.get("root_section") or -1) == best_sec):
                            # 无省列表时的回退：必须切片区间与本敌国子区间显著重叠
                            _of = int(_ord.get("from", 0) or 0)
                            _ot = int(_ord.get("to", A.FULL) or A.FULL)
                            _ov = max(0, min(_ot, sub_to) - max(_of, sub_from))
                            _span = max(1, _ot - _of)
                            if _ov * 2 >= _span:
                                matches_enemy = True
                                break
                    busy_summary.append({
                        "army": hex(og),
                        "faces_enemy": matches_enemy,
                        "orders": [o.get("id") for o in og_ords],
                    })
                    if matches_enemy:
                        facing_enemy.append(og)
                out["steps"]["busy_armies_enemy_check"] = busy_summary
                if facing_enemy:
                    all_og = facing_enemy
                    out["steps"]["armies_note"] = (
                        "re-planning %d army(s) already facing %s (isolated from %d army(s) on other fronts)"
                        % (len(facing_enemy), enemy_tag, len(busy) - len(facing_enemy))
                    )
                else:
                    return {
                        "ok": False,
                        "enemy_tag": enemy_tag,
                        "blocked_by": "no_armies_facing_enemy",
                        "error": (
                            "all %d army(s) already hold front orders on other borders and none faces %s; "
                            "refusing to strip other fronts (e.g. Maginot/FRA). Pass explicit armies=[...] "
                            "to redeploy specific armies."
                            % (len(busy), enemy_tag)
                        ),
                        "front": out["steps"]["front"],
                        "busy_armies": busy_summary,
                    }
            elif guarded_other:
                return {
                    "ok": False,
                    "enemy_tag": enemy_tag,
                    "blocked_by": "free_armies_guarding_other_borders",
                    "error": (
                        "all %d free army(s) are physically stationed on other borders (%s) and none "
                        "borders %s; refusing to strip other borders (e.g. Maginot/FRA). Pass explicit "
                        "armies=[...] to commit specific armies."
                        % (
                            len(guarded_other),
                            ", ".join(sorted({t for x in guarded_other for t in x.get("bordering", [])})) or "other",
                            enemy_tag,
                        )
                    ),
                    "front": out["steps"]["front"],
                    "free_armies_guarding_other_borders": guarded_other,
                }
        if special_summary and "armies_with_special_orders" not in out["steps"]:
            out["steps"]["armies_with_special_orders"] = special_summary
        sizes = {}
        for og in all_og:
            try:
                _all_d = A.divisions_of(self, og) or []
                _res_s = special_reserved.get(og)
                sizes[og] = len([d for d in _all_d if d not in _res_s]) if _res_s else len(_all_d)
            except Exception:
                sizes[og] = 0
        all_og = [og for og in all_og if sizes.get(og)]
        if not all_og:
            return {"ok": False, "error": "no army with divisions"}

        # --- supply cap: only commit what this front can feed ------------------
        cap = guard.get("capacity") or 0
        if cap and not force:
            total = sum(sizes[og] for og in all_og)
            if total > cap:
                keep, running = [], 0
                for og in sorted(all_og, key=lambda o: -sizes[o]):
                    if running + sizes[og] <= cap:
                        keep.append(og)
                        running += sizes[og]
                if not keep:                      # nothing fits: send the smallest
                    keep = [min(all_og, key=lambda o: sizes[o])]
                held_list = [o for o in all_og if o not in keep]
                # 2026-11-23 (复测 6 P2-2): 补齐被留作预备队集团军的详细位置与面向邻国，
                # 不再只返回裸指针列表导致无法判断剩余师是否正在守卫其他边境（如法德线）。
                held_detail = []
                kept_detail = []
                try:
                    import mapdata as _md_st
                    import unitorders as _uo_st
                    _owners_st, _ = _md_st.province_owner_map(self)
                    _adj_st = _md_st.build_adjacency()
                    _, _ptype_st = _md_st.load_definitions()
                    _me_u = str(co.tag).upper()
                except Exception:
                    _owners_st, _adj_st, _ptype_st, _me_u = {}, {}, {}, str(co.tag).upper()

                def _army_loc_info(og_ptr):
                    provs = []
                    bordering = set()
                    try:
                        for dptr in A.divisions_of(self, og_ptr):
                            pv = _uo_st.province_of(self, dptr)
                            if pv is not None:
                                provs.append(int(pv))
                                if _owners_st and _adj_st:
                                    for nb in _adj_st.get(pv, ()):
                                        if _ptype_st and _ptype_st.get(nb) != "land":
                                            continue
                                        nbo = _owners_st.get(nb)
                                        if nbo and nbo != _me_u:
                                            bordering.add(nbo)
                    except Exception:
                        pass
                    ord_rows = []
                    try:
                        for _or in (A.orders(self, og_ptr) or []):
                            ord_rows.append({
                                "id": _or.get("id"),
                                "type": _or.get("type"),
                                "assigned": _or.get("assigned_divisions"),
                                "root_front": _or.get("root_front"),
                            })
                    except Exception:
                        ord_rows = []
                    has_ord = bool(ord_rows)
                    from collections import Counter as _Cnt
                    top_p = [pv for pv, _ in _Cnt(provs).most_common(6)]
                    return {
                        "army": hex(og_ptr),
                        "divisions": int(sizes.get(og_ptr, 0)),
                        "has_orders": has_ord,
                        "orders": ord_rows,
                        "provinces_sample": top_p,
                        "bordering": sorted(bordering),
                    }

                for _o in held_list:
                    held_detail.append(_army_loc_info(_o))
                for _o in keep:
                    kept_detail.append(_army_loc_info(_o))
                # 2026-11-23 (复测 9 E-2): 明确列出被留作预备队但仍挂着旧前线订单的集团军，
                # 防止调用方对这些军再调 hoi4_deploy_to_border 导致部队在两条边境来回拉扯。
                held_keep_orders = [
                    {
                        "army": hd["army"],
                        "divisions": hd["divisions"],
                        "orders": hd["orders"],
                        "bordering": hd["bordering"],
                    }
                    for hd in held_detail
                    if hd.get("has_orders")
                ]
                trim_obj = {
                    "capacity": cap, "would_commit": total,
                    "committed": sum(sizes[o] for o in keep),
                    "held_back_divisions": sum(sizes[o] for o in held_list),
                    "armies_kept": [hex(o) for o in keep],
                    "armies_kept_detail": kept_detail,
                    "armies_held_back": [hex(o) for o in held_list],
                    "armies_held_back_detail": held_detail,
                    "held_back_armies_keep_orders": held_keep_orders,
                    "note": "the rest stay in reserve instead of stacking a "
                            "front that cannot supply them",
                }
                if held_keep_orders:
                    trim_obj["warning"] = (
                        "有 %d 支被留作预备队的集团军仍保留原有前线订单（未做改动）：%s。"
                        "若对它们的师执行 hoi4_deploy_to_border 逐师搬运，引擎会按旧前线把部队拉回原边境；"
                        "如需真换防，请先用 hoi4_delete_order 撤掉旧前线订单或显式传 armies=[...] 指定上线集团军。"
                        % (
                            len(held_keep_orders),
                            ", ".join(
                                "%s(%d师, orders=%s)"
                                % (x["army"], x["divisions"], [o["id"] for o in x["orders"]])
                                for x in held_keep_orders[:4]
                            ),
                        )
                    )
                out["steps"]["supply_trim"] = trim_obj
                all_og = keep

        # 2026-11-26 (P2-7 根治): 对齐 steps.supply 口径与实际拟上线师数 committed_divs；
        # 若经过 supply_trim 后连最小单军依然严重超过前线补给容量 (> 1.5x) 且未传 force=True，
        # 明确拒绝硬压上去并提示拆分小集团军或显式传 force=True。
        committed_divs = sum(sizes[og] for og in all_og)
        if isinstance(out["steps"].get("supply"), dict):
            sup = out["steps"]["supply"]
            sup["total_country_divisions"] = sup.get("needed")
            sup["committed"] = committed_divs
            if cap:
                c_ratio = round(committed_divs / float(cap), 2)
                c_ok = committed_divs <= float(cap) * 1.25
                sup["committed_ratio"] = c_ratio
                sup["committed_ok"] = c_ok
                # 若总军力虽大于容量但裁剪后实际投入已满足补给容量，同步将 ok 反映为实际计划合规
                if c_ok and sup.get("ok") is False:
                    sup["ok"] = True
                    sup["note"] = (
                        "country total (%s) exceeds front capacity (%.1f), "
                        "but actual committed divisions (%d) fit within supply budget"
                        % (sup.get("total_country_divisions"), float(cap), committed_divs)
                    )
                elif not c_ok and not force:
                    if committed_divs > float(cap) * 1.5:
                        return {
                            "ok": False,
                            "enemy_tag": enemy_tag,
                            "blocked_by": "supply_over_capacity",
                            "error": (
                                "front towards %s can only supply %.1f divisions, but even the smallest "
                                "candidate army would commit %d divisions (ratio %.2fx > 1.5x). "
                                "Split a smaller army (<= %d divs), upgrade supply hubs, or pass force=true to override."
                                % (enemy_tag, float(cap), committed_divs, c_ratio, max(1, int(float(cap))))
                            ),
                            "steps": out["steps"],
                        }
                    out["supply_warning"] = (
                        "committed %d divisions onto a front with capacity %.1f (ratio %.2fx); "
                        "watch supply attrition or split into smaller armies"
                        % (committed_divs, float(cap), c_ratio)
                    )

        n = len(all_og)
        plan = []
        span = max(1, sub_to - sub_from)
        for i, og in enumerate(all_og):
            p_from = sub_from if not split else int(sub_from + span * i / n)
            p_to = sub_to if not split else int(sub_from + span * (i + 1) / n)
            plan.append({"army": hex(og), "divisions": sizes[og],
                         "from": p_from,
                         "to": p_to})
        out["steps"]["plan"] = plan
        out["steps"]["split"] = bool(split)
        if dry_run:
            out["ok"] = True
            out["dry_run"] = True
            return out

        idler = p.u64(p.base + 0x33DDB08)
        was_paused = bool(idler and p.u8(idler + 0x6C1))
        out["resumed_for_plan"] = was_paused
        if was_paused:
            p.write(idler + 0x6C1, b"\x00")
        try:
            def wait(fn, seconds=8.0):
                t0 = time.time()
                while time.time() - t0 < seconds:
                    v = fn()
                    if v:
                        return v
                    time.sleep(0.25)
                return None

            results = []
            for i, og in enumerate(all_og):
                frm, to = plan[i]["from"], plan[i]["to"]
                divs = list(A.divisions_of(self, og) or ())
                if og in special_reserved:
                    divs = [d for d in divs if d not in special_reserved[og]]
                # 2026-11-20: the ONLY plan shape the engine actually feeds is
                # the one built by CompletePlan (0x18CFC70).  A front order made
                # by draw_front_line + execute_plan/manual_offensive gets a
                # type-1 child whose +0x210 array stays empty forever: the arrow
                # is drawn and never moves a division.  Measured on Austria
                # 1937-04: 17 divisions on the front, 16 game days, ZERO
                # province changes even against an enemy with 0 divisions.
                # CompletePlan builds front+arrow in one engine call and the
                # engine hands the divisions to the child (measured 17/17), and
                # that plan took 17 Austrian provinces.
                def _order_matches_slice(ord_row):
                    if ord_row.get("type") != 2:
                        return False
                    if int(ord_row.get("root_section") or -1) != int(best["section"]):
                        return False
                    # 2026-11-23 (复测 11 P1-2): 多国共享同一 CFront+section（如 FRA/BEL/HOL）时，
                    # 必须校验订单区间与目标敌国区间 [sub_from, sub_to] 显著重叠，绝不把 FRA 段
                    # 前线订单错当 HOL 段复用！
                    of = int(ord_row.get("from", 0) or 0)
                    ot = int(ord_row.get("to", A.FULL) or A.FULL)
                    if ot <= of:
                        return True
                    ov = max(0, min(ot, sub_to) - max(of, sub_from))
                    return ov * 2 >= (ot - of)

                reused = [o for o in A.orders(self, og) if _order_matches_slice(o)]
                root_id = 0
                fresh = []
                info = {}
                ok, err = True, ""
                flags = []
                assign_fix = None
                dropped = None
                if reused:
                    rid = reused[0]["id"]
                    kids0 = [c for c in A.order_children(self, og)
                             if c["root"] == rid
                             and c["type"] == A.ORDER_TYPE_OFFENSIVE
                             and (c.get("assigned") or 0) > 0]
                    if kids0:
                        root_id = rid
                        fresh = [reused[0]]
                        out.setdefault("steps", {}).setdefault(
                            "reused_orders", []).append(
                                {"army": hex(og), "order": rid})
                    else:
                        # A front order with no fed arrow is the dead shape the
                        # 1937 Austrian test exposed.  Drop it and rebuild.
                        dr = self.delete_order(og, rid)
                        dropped = {"army": hex(og), "order": rid,
                                   "ok": dr.get("ok"), "error": dr.get("error"),
                                   "reason": "front order had no fed offensive child"}
                if not fresh:
                    ok, err, info = A.complete_plan(
                        self, og, best["front"], targets,
                        section=best.get("section"), frm=frm, to=to,
                        divisions=divs)
                    oid = int((info or {}).get("order_id") or 0)
                    fresh = [o for o in A.orders(self, og)
                             if o["id"] == oid] if oid else []
                    root_id = oid
                if not fresh:
                    results.append({"army": hex(og), "from": frm, "to": to,
                                    "divisions": len(divs), "ok": False,
                                    "method": "complete_plan",
                                    "error": err or "no front order appeared"})
                    continue
                # CompletePlan assigns the army's divisions to the root itself;
                # top up only when the root is still empty (a reused order that
                # lost its divisions, or a refused engine assign).
                # 2026-11-23 (复测 7 B-6): 区分本军指派师数（root_assigned）与引擎
                # order+0x210 上的共享前线总师数（root_total_assigned）。
                _abr = A.assigned_breakdown(self, og, root_id) if hasattr(A, "assigned_breakdown") else None
                assigned = (_abr["army_assigned"] if isinstance(_abr, dict) else A.assigned_of(self, og, root_id))
                root_total_assigned = (_abr["front_total_assigned"] if isinstance(_abr, dict) else assigned)
                if (assigned or 0) == 0:
                    try:
                        assign_fix = self.assign_divisions_to_front(
                            og, enemy_tag, mode="all", order_ids=[root_id],
                            divisions=(divs if og in special_reserved else None),
                        )
                    except Exception as e:
                        assign_fix = {"ok": False, "error": repr(e)}
                    self._settle(0.5)
                    _abr = A.assigned_breakdown(self, og, root_id) if hasattr(A, "assigned_breakdown") else None
                    assigned = (_abr["army_assigned"] if isinstance(_abr, dict) else A.assigned_of(self, og, root_id))
                    root_total_assigned = (_abr["front_total_assigned"] if isinstance(_abr, dict) else assigned)
                kids = [c for c in A.order_children(self, og)
                        if c["root"] == root_id
                        and c["type"] == A.ORDER_TYPE_OFFENSIVE]
                # The engine feeds the child on the first simulation tick; a
                # short grace period is enough and keeps the old API honest.
                if not any((c.get("assigned") or 0) > 0 for c in kids):
                    deadline = time.time() + 4.0
                    while time.time() < deadline:
                        time.sleep(0.25)
                        kids = [c for c in A.order_children(self, og)
                                if c["root"] == root_id
                                and c["type"] == A.ORDER_TYPE_OFFENSIVE]
                        if any((c.get("assigned") or 0) > 0 for c in kids):
                            break
                time.sleep(0.3)
                flags = A.start_offensives(self, og)
                kids = [c for c in A.order_children(self, og)
                        if c["root"] == root_id
                        and c["type"] == A.ORDER_TYPE_OFFENSIVE]
                kids_fed = [c for c in kids if (c.get("assigned") or 0) > 0]
                # An empty child under an EMPTY root is the 0xB2E62E hazard;
                # under a root that holds divisions it is the normal post-draw
                # state and must be kept (deleting it throws the arrow away).
                purged = []
                for c in kids:
                    if (c.get("assigned") or 0) > 0:
                        continue
                    if (assigned or 0) > 0:
                        continue
                    dr = self.delete_order(og, c["id"])
                    purged.append({"child": c["id"], "ok": dr.get("ok"),
                                   "error": dr.get("error")})
                if purged:
                    out.setdefault("steps", {}).setdefault(
                        "purged_empty_children", []).extend(
                            [dict(p, army=hex(og), root=root_id) for p in purged])
                    kids = [c for c in kids if (c.get("assigned") or 0) > 0]
                if (assigned or 0) == 0:
                    dr = self.delete_order(og, root_id)
                    dropped = {"army": hex(og), "order": root_id,
                               "ok": dr.get("ok"), "error": dr.get("error")}
                    out.setdefault("steps", {}).setdefault(
                        "dropped_empty_orders", []).append(dropped)
                # 2026-11-26 (B1 根治): offensive_ok / execute_ok 必须由实际内存回读的
                # kids_fed 与 executing 派生，禁止硬编码 True 谎报成功！
                offensive_ok = bool(kids_fed)
                if offensive_ok:
                    offensive_error = ""
                elif kids:
                    offensive_error = "offensive child %s has 0 assigned divisions" % ([k["id"] for k in kids],)
                else:
                    offensive_error = (
                        err
                        or (info or {}).get("note")
                        or ("CompletePlan produced root order %d but no type-1 offensive child" % root_id)
                    )
                execute_ok = bool(kids_fed and any(k.get("executing") == 1 for k in kids_fed))
                if execute_ok:
                    execute_error = ""
                elif not kids_fed:
                    execute_error = "no fed offensive child to execute"
                else:
                    execute_error = "offensive child exists but executing flag remained 0"

                results.append({"army": hex(og), "from": frm, "to": to,
                                "divisions": len(divs),
                                "front_order": root_id,
                                "front_ok": bool(ok), "front_error": err,
                                "offensive_ok": offensive_ok, "offensive_error": offensive_error,
                                "children": [k["id"] for k in kids],
                                "children_fed": [k["id"] for k in kids_fed],
                                "children_executing": [k["id"] for k in kids
                                                       if k.get("executing") == 1],
                                "execute_ok": execute_ok, "execute_error": execute_error,
                                "root_assigned": assigned,
                                "root_total_assigned": root_total_assigned,
                                "offensive_assigned": min(
                                    len(divs),
                                    sum(k.get("assigned") or 0 for k in kids_fed),
                                ),
                                "offensive_total_assigned": sum(
                                    k.get("assigned") or 0 for k in kids_fed
                                ),
                                "assign_fix": assign_fix,
                                "dropped_order": dropped,
                                "flags": flags,
                                "method": "complete_plan",
                                # 2026-11-20: ok means the engine really fed the
                                # arrow and it is switched on -- the 1937 test
                                # had every other box ticked while the front
                                # stood still for 16 days.
                                "ok": (bool(fresh) and offensive_ok
                                       and (assigned or 0) > 0
                                       and execute_ok)})
            out["steps"]["armed"] = results
            out["steps"]["children"] = [(c["id"], c["assigned"], c["executing"])
                                        for og in all_og for c in A.order_children(self, og)]
            # 2026-11-18: 全军的空子命令清扫。只清"本次处理过的军"不够 ——
            # 已经有订单的军会被 plan 跳过，它们身上攒下来的空 type-1 子命令
            # 恰恰就是 0xB2E62E 崩溃的实体，必须一起清掉。
            swept = []
            for og in all_og:
                try:
                    kids_all = A.order_children(self, og)
                except Exception:
                    continue
                for c in kids_all:
                    if c["type"] == A.ORDER_TYPE_OFFENSIVE and (c.get("assigned") or 0) == 0:
                        # 2026-11-19: divisions live on the parent front order;
                        # the engine feeds the arrow at execution time.  A child
                        # at 0 under a front order that DOES hold divisions is the
                        # normal post-draw state -- sweeping it deletes the arrow
                        # we just made.  Only orphans are the 0xB2E62E hazard.
                        try:
                            if (A.assigned_of(self, og, c.get("root")) or 0) > 0:
                                continue
                        except Exception:
                            pass
                        dr = self.delete_order(og, c["id"])
                        swept.append({"army": hex(og), "child": c["id"],
                                      "root": c.get("root"),
                                      "ok": dr.get("ok"), "error": dr.get("error")})
            out["steps"]["swept_empty_children"] = swept
            # 2026-11-21 (P1-7): the sweeps above only touch `all_og` -- the armies
            # THIS plan committed.  An army that was filtered out (it had no
            # divisions) can still carry a type-2 front order from an earlier run,
            # and that is exactly the 0-division order the 2026-11-20 playtest
            # found locking the clock (`blocked_by: "empty_orders"`).  Look at
            # every army of the country and say so; drop a childless empty front
            # outright (the safe case) and leave the rest to hoi4_purge_orders,
            # which knows how to clear children first.
            empty_fronts, dropped_fronts = [], []
            try:
                for og in [o for o in armyops.orders_groups(self, co)
                           if not p.u8(o + A.OG_IS_GROUP)]:
                    try:
                        rows = A.orders(self, og)
                    except Exception:
                        continue
                    for o in rows:
                        if o.get("type") != 2:
                            continue
                        if int(o.get("assigned_divisions")
                               or o.get("assigned") or 0) > 0:
                            continue
                        try:
                            kids = [c for c in A.order_children(self, og)
                                    if c.get("root") == o["id"]]
                        except Exception:
                            kids = []
                        rec = {"army": hex(og), "order": o["id"],
                               "children": len(kids)}
                        if not kids:
                            dr = self.delete_order(og, o["id"])
                            rec["deleted"] = bool(dr.get("ok"))
                            rec["error"] = dr.get("error")
                            dropped_fronts.append(rec)
                        else:
                            empty_fronts.append(rec)
            except Exception as e:
                out["steps"]["empty_front_sweep_error"] = repr(e)
            if dropped_fronts:
                out["steps"]["dropped_empty_fronts"] = dropped_fronts
            if empty_fronts:
                out["steps"]["empty_front_orders"] = empty_fronts
                out["warning"] = (
                    "%d 条前线订单 assigned=0 且带子命令，解暂停会被安全阀拦下"
                    "（hoi4.exe+0xB2E62E 崩溃点）——先跑 hoi4_purge_orders。"
                    % len(empty_fronts))
            out["ok"] = bool(results) and all(r["ok"] for r in results)
            if wait_seconds:
                import mapdata
                enemy_provs = set(sum(A.owned_provinces(self, enemy_tag).values(), []))
                t0 = time.time()
                gains = 0
                while time.time() - t0 < float(wait_seconds):
                    cm = mapdata.province_controller_map(self)
                    gains = sum(1 for x in enemy_provs
                                if cm.get(x) == (co.tag if hasattr(co, "tag") else self.player_tag()))
                    if gains:
                        break
                    time.sleep(5)
                out["steps"]["advance"] = {"waited": round(time.time() - t0, 1),
                                           "enemy_provinces_taken": gains,
                                           "date": self.date()["text"]}
            return out
        finally:
            if was_paused and idler:
                p.write(idler + 0x6C1, b"\x01")

    def _supply_front_guard(self, enemy_tag, tag=None, ratio=1.25):
        """How many divisions this front can feed vs how many we would commit.

        Called BEFORE the battle plan is drawn. A plan commits a whole army to a
        front line, and the engine then distributes it over the border provinces
        of that front; if that is more than the front can supply, every division
        on it degrades at once. Returns the numbers either way; the caller
        decides (force=True).
        """
        try:
            import staff_supply as _ss
            fb = _ss.front_budget(self, enemy_tag, tag)
            need = self.divisions(tag, limit=1)["count"]
            cap = fb.get("capacity") or 0
            ok = (not cap) or (need <= cap * ratio)
            return {"ok": ok, "needed": need, "capacity": cap,
                    "ratio": round(need / cap, 2) if cap else None,
                    "per_province": fb.get("per_province"),
                    "border_provinces": len(fb.get("border_provinces") or [])}
        except Exception as e:
            return {"ok": True, "error": repr(e)}

    def _win_battleplans(self, enemy_tag, tag=None, force=False, wait_seconds=0,
                         armies=None, split=True):
        """activate_army_battleplans for hoi4.exe: the chain stage/wden.py proved live.

        create_army -> front line (CFront section facing the enemy) -> offensive
        line under THAT order -> execute -> set the executing flag.

        2026-11-17: when the country already HAS armies this now delegates to
        `plan_front`, which draws one front line per army instead of shoving every
        division into one new army (see plan_front's docstring for the 1940
        post-mortem).  The old merge-everything code stays only as the fallback
        for a country that has divisions but no armies at all.

        Two things this learned the hard way:
          * the engine only hands the divisions to the offensive child while the
            simulation is RUNNING.  Building the plan on a paused game produces a
            child with `assigned == 0` and nothing ever moves, so the method
            unpauses for the duration and restores the pause flag afterwards;
          * "posted" is not "created".  Every step is polled and read back.
        """
        import time
        import armyops_win as A
        p = self.p
        enemy_tag = str(enemy_tag).upper()
        out = {"enemy_tag": enemy_tag, "backend": "windows", "steps": {}}

        # 2026-11-17: per-army front lines whenever the country already has armies.
        import armyops as _ao
        try:
            _co = self._c(tag)
            _have = [og for og in _ao.orders_groups(self, _co)
                     if not p.u8(og + A.OG_IS_GROUP)
                     and len(A.divisions_of(self, og)) > 0]
        except Exception:
            _have = []
        if _have or armies:
            res = self.plan_front(enemy_tag, tag, armies=armies, split=split,
                                  force=force, wait_seconds=wait_seconds)
            res["mode"] = "per-army-front-lines"
            return res

        # 2026-11-18: 同 plan_front —— 和平时期画线正是这条路径存在的理由。
        try:
            out["at_war"] = enemy_tag in ((self.wars(tag) or {}).get("enemies") or [])
        except Exception:
            out["at_war"] = None

        idler = p.u64(p.base + 0x33DDB08)
        was_paused = bool(idler and p.u8(idler + 0x6C1))
        out["resumed_for_plan"] = was_paused
        if was_paused:
            p.write(idler + 0x6C1, b"\x00")
        try:
            segs = A.fronts_towards(self, enemy_tag, tag)
            out["steps"]["fronts"] = [{"handle": s["front_handle"], "section": s["section"],
                                       "border": len(s["border_provinces"])} for s in segs]
            if not segs:
                out.update({"ok": False, "error": "no front section faces %s" % enemy_tag})
                return out
            try:
                _cand_divs = [int(d["ptr"], 16) for d in (self.divisions(tag) or {}).get("divisions", [])]
            except Exception:
                _cand_divs = []
            best = self._best_front_segment(segs, _cand_divs)
            targets = A.target_provinces(self, enemy_tag, depth=1, own_tag=tag)
            out["steps"]["targets"] = targets[:20]
            if not targets:
                out.update({"ok": False, "error": "no enemy province two steps in"})
                return out

            divs = [int(d["ptr"], 16) for d in (self.divisions(tag) or {}).get("divisions", [])]
            if not divs:
                out.update({"ok": False, "error": "no divisions"})
                return out
            # One group only if every division already lives in the same one;
            # otherwise make a new army (this is what stage/wden.py did -- the
            # 1936 save has 4 divisions in an army and 26 floating).
            ogs = {p.u64(d + A.CARMY_OG) for d in divs}
            og = next(iter(ogs)) if len(ogs) == 1 and 0 not in ogs else 0
            if not og:
                ok, err = A.create_army(self, divs)
                out["steps"]["create_army"] = {"ok": ok, "error": err}
                if not ok:
                    out.update({"ok": False, "error": err})
                    return out
                og = p.u64(divs[0] + A.CARMY_OG)
            out["steps"]["orders_group"] = hex(og)
            out["steps"]["divisions"] = len(A.divisions_of(self, og))

            def wait(fn, seconds=6.0):
                t0 = time.time()
                while time.time() - t0 < seconds:
                    v = fn()
                    if v:
                        return v
                    time.sleep(0.25)
                return None

            handle = tuple(best["front_handle"])
            roots = [o for o in A.orders(self, og)
                     if o["type"] == 2 and tuple(o.get("root_front") or ()) == handle]
            if roots:
                root_id = roots[-1]["id"]
                out["steps"]["front_line"] = {"ok": True, "reused": root_id}
            else:
                before = {o["id"] for o in A.orders(self, og)}
                # 2026-11-19: COrderNewRootCommand's raw ctor killed the process
                # six times; the engine's own CompletePlan command builds the
                # same front order and never crashed (3/3 live).
                ok, err, info = A.complete_plan(
                    self, og, best["front"], targets,
                    section=best.get("section"), divisions=divs)
                oid = int((info or {}).get("order_id") or 0)
                fresh = [o for o in A.orders(self, og) if o["id"] == oid] if oid else \
                    wait(lambda: [o for o in A.orders(self, og)
                                  if o["id"] not in before and o["type"] == 2])
                out["steps"]["front_line"] = {
                    "ok": bool(ok and fresh), "error": err,
                    "new_orders": [o["id"] for o in (fresh or [])],
                    "method": "complete_plan"}
                if not fresh:
                    out.update({"ok": False,
                                "error": err or "no front order appeared"})
                    return out
                root_id = fresh[-1]["id"]
                a_ok, a_err = A.assign_to_order(self, og, root_id,
                                                [int(d) for d in divs])
                out["steps"]["front_assign"] = {
                    "ok": bool(a_ok), "error": a_err,
                    "assigned": A.assigned_of(self, og, root_id)}

            kids = [c for c in A.order_children(self, og)
                    if c["root"] == root_id and c["type"] == A.ORDER_TYPE_OFFENSIVE]
            if kids:
                out["steps"]["offensive_line"] = {"ok": True, "reused": [k["id"] for k in kids]}
            else:
                # 2026-11-19: the type-1 child comes from the engine's order
                # factory; COrderNewFrontCommand::Execute bails for front orders
                # this tool built (0x10B8620 returns an empty split list).
                m_ok, m_err, _optr = A.manual_offensive(self, og, root_id, targets)
                kids = wait(lambda: [c for c in A.order_children(self, og)
                                     if c["root"] == root_id
                                     and c["type"] == A.ORDER_TYPE_OFFENSIVE])
                out["steps"]["offensive_line"] = {
                    "ok": bool(m_ok and kids), "error": m_err,
                    "children": [k["id"] for k in (kids or [])]}
                if not kids:
                    out.update({"ok": False,
                                "error": m_err or "no offensive child appeared"})
                    return out

            # 2026-11-17: order_id=0 made this a NO-OP (see armyops_win.execute_plan).
            ok, err, _cmd = A.execute_plan(self, og, root_id, 1)
            time.sleep(0.5)
            out["steps"]["execute"] = {"ok": ok, "error": err,
                                       "order": root_id,
                                       "assigned": A.assigned_of(self, og, root_id)}

            # THE GREEN TICK is the ARROW: the type-2 offensive line carries its
            # own +0x248.  COrderExecuteCommand only sets that byte when it is
            # given an order id AND flag=1 (ctor: [+0x30]=id, [+0x34]=flag,
            # 0x18C8D24), and our helper passes flag=0 -- i.e. `execute(id)` is
            # the toggle OFF.  So set it through the engine's setter instead.
            # Note the delay: the divisions need ~2 game days of planning before
            # the first province falls (measured 2026-11-15, research 70.6).
            arrows = [c for c in A.order_children(self, og)
                      if c["root"] == root_id and c["type"] == 2]
            if arrows:
                arrow_id = arrows[-1]["id"]
                byte = A.set_order_executing(self, arrows[-1]["ptr"], True)
                out["steps"]["activate"] = {"ok": byte == 1, "order": arrow_id}
            out["steps"]["flags"] = A.start_offensives(self, og)
            out["steps"]["children"] = [(c["id"], c["assigned"], c["executing"])
                                        for c in A.order_children(self, og)]
            out["ok"] = all(c[2] == 1 for c in out["steps"]["children"]
                            if c[1] > 0) and bool(out["steps"]["children"])

            if wait_seconds:
                # How long the plan takes to bite.  Measured 2026-11-15 at speed 1:
                # the first enemy province changes hands ~10-11 GAME DAYS after the
                # plan is built -- the divisions have to redeploy onto the front
                # line first.  Every "it never advances" report before that was a
                # window that was simply too short (research 70.6).
                import mapdata
                enemy_provs = set(sum(A.owned_provinces(self, enemy_tag).values(), []))
                t0 = time.time()
                start = None
                gains = 0
                while time.time() - t0 < float(wait_seconds):
                    cm = mapdata.province_controller_map(self)
                    gains = sum(1 for x in enemy_provs if cm.get(x) == (tag or self.player_tag()))
                    if start is None:
                        start = time.time()
                    if gains:
                        break
                    time.sleep(5)
                out["steps"]["advance"] = {
                    "waited": round(time.time() - t0, 1),
                    "enemy_provinces_taken": gains,
                    "date": self.date()["text"],
                }
            return out
        finally:
            if was_paused and idler:
                p.write(idler + 0x6C1, b"\x01")

    def activate_army_battleplans(self, enemy_tag, tag=None, force=False,
                                  wait_seconds=0, armies=None, split=True):
        """One-shot: unassigned divisions -> armies -> front line -> offensive line -> execute.

        2026-11-08 (work order 1108 §3.2). Replaces the run-13 version, which
        assumed an army (COrdersGroup) already existed. A fresh 1936 Germany has
        30 divisions and ZERO armies (`CArmy+0xC0 == 0` everywhere), so the plan
        could never be drawn. This version builds the armies first.

        The Clausewitz command queue is asynchronous: `RUN_CMD` only posts, the
        game main thread executes on its next frame. Therefore every step must
        `detach` (which `create_army`/`draw_front_line`/... already do) and then
        be POLLED from outside the attach session. Never sleep inside `with self:`.
        """
        import time
        if sys.platform == "win32":
            return self._win_battleplans(enemy_tag, tag, force, wait_seconds,
                                         armies=armies, split=split)
        import armyops
        p = self.p
        co = self._c(tag)
        enemy_tag = str(enemy_tag).upper()
        out = {"enemy_tag": enemy_tag, "steps": {}}
        DRAW_ARROW = True   # 2026-11-08: ctor OE2 fixed (0x2332310) + non-NULL darr

        # ---- step -1: supply guard -------------------------------------------
        guard = self._supply_front_guard(enemy_tag, tag)
        out["supply_guard"] = guard
        if not guard.get("ok") and not force:
            out.update({"ok": False, "blocked_by": "supply",
                        "error": ("this front can feed about %s divisions but the plan would "
                                  "commit %s. A battle plan puts a whole army on the front and "
                                  "the engine spreads it over only those border provinces, so "
                                  "everything on it would degrade together. Narrow the front, "
                                  "deploy to a different border, or pass force=true."
                                  % (guard.get("capacity"), guard.get("needed")))})
            return out

        # ---- step 0: build armies out of the unassigned pool -------------------
        # 2026-11-10: paratroopers are exempt from every automatic land order.
        # A parachute division waiting at its air base looked exactly like an
        # idle division, was collected into a new army and given a front line.
        import divreserve as _dr
        unassigned = [d for d in self.unassigned_divisions(tag)
                      if not _dr.is_paradrop_division(self, d)[0]]
        ogs = [og for og in armyops.orders_groups(self, co)
               if not p.u8(og + armyops.OG_IS_GROUP)]
        _keep, _skip = [], []
        for og in ogs:
            try:
                _pd, _why = _dr.army_is_paradrop(self, og)
            except Exception:
                _pd, _why = False, None
            if _pd:
                _skip.append({"army": hex(og), "reason": _why})
            else:
                _keep.append(og)
        ogs = _keep
        out["steps"]["skipped_paradrop_armies"] = _skip
        out["steps"]["before"] = {"unassigned": len(unassigned), "armies": len(ogs)}

        if not ogs and unassigned:
            created = []
            while unassigned:
                batch, unassigned = unassigned[:24], unassigned[24:]
                before_set = set(armyops.orders_groups(self, co))
                r = self.create_army(batch, tag=tag)
                got = None
                for _ in range(20):
                    time.sleep(0.15)
                    now = set(armyops.orders_groups(self, co))
                    if len(now) > len(before_set):
                        got = (now - before_set).pop()
                        break
                created.append({"divisions": len(batch), "ok": r.get("ok"),
                                "error": r.get("error"), "army": hex(got) if got else None})
            out["steps"]["create_army"] = created
            ogs = [og for og in armyops.orders_groups(self, co)
                   if not p.u8(og + armyops.OG_IS_GROUP)]

            # commanders: one per new army, never the same man twice
            try:
                gens = [x for x in self.available_generals(tag).get("generals", [])
                        if x.get("leader_ptr")]
                corps = [x for x in gens if x.get("rank") == "corps_commander"] or gens
                used = {p.u64(o + armyops.OG_LEADER) for o in ogs
                        if p.u64(o + armyops.OG_LEADER)}
                led = []
                for og in ogs:
                    if p.u64(og + armyops.OG_LEADER):
                        continue
                    cand = next((x for x in corps
                                 if int(x["leader_ptr"], 16) not in used), None)
                    if not cand:
                        break
                    lp = int(cand["leader_ptr"], 16)
                    ok_l, err_l = armyops.set_army_leader(self, og, lp)
                    time.sleep(0.15)
                    if ok_l:
                        used.add(lp)
                        led.append({"army": hex(og), "leader": cand.get("name")})
                out["steps"]["leaders"] = led
            except Exception as e:
                out["steps"]["leaders"] = {"error": str(e)}

        if not ogs:
            out.update({"ok": False, "error": "no armies after create_army attempt",
                        "unassigned_count": len(self.unassigned_divisions(tag)),
                        "hint": "the game must be UNPAUSED so CCommandQueue drains"})
            return out

        out["steps"]["after"] = {"armies": len(ogs),
                                 "sizes": [p.i32(o + armyops.OG_DIVISIONS + 0xC) for o in ogs]}

        # ---- step 1: the CFront facing the enemy -------------------------------
        segs = armyops.fronts_towards(self, enemy_tag, co.tag)
        if not segs:
            out.update({"ok": False, "error": "no CFront facing %s" % enemy_tag,
                        "armies": len(ogs)})
            return out
        uniq, seen = [], set()
        for s in segs:
            if s["front"] not in seen:
                seen.add(s["front"])
                uniq.append(s)
        target_provs = armyops.target_line(self, enemy_tag, depth="full", own_tag=co.tag)
        out["steps"]["fronts"] = {"sections": len(segs), "unique_fronts": len(uniq),
                                  "target_provinces": len(target_provs)}
        if not target_provs:
            out.update({"ok": False, "error": "empty offensive target line"})
            return out

        results = []
        for idx, og in enumerate(ogs):
            s = uniq[idx % len(uniq)]
            root_orders = [o for o in armyops.orders_of(self, og) if o["type"] == 2]
            if not root_orders:
                try:
                    # 2026-11-08 (run 17): the divisions MUST be handed to the root
                    # front order AT CREATION. Drawing with `None` produces an order
                    # with assigned_divisions == 0, and COrderAssignCommand cannot
                    # top it up afterwards (verified live: assign_to_order returns
                    # True and the order keeps 0). With the army's divisions in the
                    # ctor argument the same call produced assigned_divisions == 24.
                    _divs = armyops.group_divisions(self, og)
                    ok_f, err_f = armyops.draw_front_line(
                        self, og, s["front"], s["section"], 0, armyops.FULL, _divs, 0)
                except Exception as e:
                    ok_f, err_f = False, "%s: %s" % (type(e).__name__, e)
                if not ok_f:
                    results.append({"army": hex(og), "step": "draw_front_line",
                                    "ok": False, "error": err_f})
                    continue
                for _ in range(20):
                    time.sleep(0.15)
                    root_orders = [o for o in armyops.orders_of(self, og)
                                   if o["type"] == 2]
                    if root_orders:
                        break
            if not root_orders:
                results.append({"army": hex(og), "step": "wait_root_order", "ok": False,
                                "error": "root order never appeared in OG_ORDERS"})
                continue
            parent_id = root_orders[0]["id"]

            # top up to 24 with any divisions still unassigned
            cur = p.i32(og + armyops.OG_DIVISIONS + 0xC)
            spare = [d for d in self.unassigned_divisions(tag)
                     if not _dr.is_paradrop_division(self, d)[0]]
            if cur < 24 and spare:
                try:
                    armyops.assign_to_order(self, og, parent_id, spare[:24 - cur])
                except Exception:
                    pass
                time.sleep(0.15)

            # 2026-11-08 (work order 1108(1) §2.1): the offensive arrow and the
            # green tick are NOT drawn here any more. `COrderNewFrontCommand`
            # (0x232CC20) still SIGSEGVs on argument semantics and
            # `COrderExecuteCommand` dies inside RUN_CMD at 0x20AE17E because the
            # command's vtable slot is still NULL (see `cmd_ok` below) — one bad
            # command poisons the rest of the pipeline. A front line alone is
            # enough for the engine to rail every division of that army to the
            # border and dig in, which is what we actually need before attacking.
            if DRAW_ARROW:
                off = [o for o in armyops.orders_of(self, og) if o["type"] == 1]
                ok_o, err_o = True, ""
                if not off:
                    try:
                        ok_o, err_o = armyops.offensive_line(self, og, parent_id, target_provs)
                    except Exception as e:
                        ok_o, err_o = False, "%s: %s" % (type(e).__name__, e)
                try:
                    ok_e, err_e = armyops.execute_plan(self, og, 0, 0)
                except Exception as e:
                    ok_e, err_e = False, "%s: %s" % (type(e).__name__, e)
            else:
                ok_o, err_o, ok_e, err_e = None, "disabled (arrow not implemented)", \
                                           None, "disabled (arrow not implemented)"
            results.append({"army": hex(og), "root_order_id": parent_id,
                            "divisions": p.i32(og + armyops.OG_DIVISIONS + 0xC),
                            "front_line_ok": True,
                            "offensive_ok": ok_o, "offensive_err": err_o,
                            "execute_ok": ok_e, "execute_err": err_e})

        out["results"] = results
        out["ok"] = bool(results) and all(r.get("front_line_ok") for r in results)
        return out
