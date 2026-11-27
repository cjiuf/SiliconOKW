"""战线变动快照、订单树单步操作/空单清零及战备前线核验领域 Mixin。"""
from __future__ import annotations

import session_state as _SS
import sys

from ._shared import FRONT_SNAPSHOT_PATH, _wargoal_gate

class BattleplanOrdersMixin:
    """战线变动快照、订单树单步操作/空单清零及战备前线核验领域 Mixin。"""

    @staticmethod
    def reset_front_snapshot():
        """删除磁盘上的 front_snapshot.json 基线（供换档/读档/换进程时调用，杜绝跨局串味）。"""
        import os
        cache = FRONT_SNAPSHOT_PATH
        try:
            if os.path.exists(cache):
                os.remove(cache)
                return True
        except Exception:
            pass
        return False

    def front_changes(self, enemy_tag=None, reset=False):
        """Province CONTROL changes since the previous call: the actual front movement.

        Uses mapdata.province_controller_map(), not state ownership. In a civil war
        (and any war before a peace treaty) occupation only changes the province
        controller; the old owner-map version reported changed_count=0 while a
        state was being overrun (2026-11-02 UK test).
        """
        import json, os, time
        import mapdata
        control = mapdata.province_controller_map(self)
        cache = FRONT_SNAPSHOT_PATH
        try:
            now_date = self.date_raw()
        except Exception:
            now_date = None
        try:
            pid = self.p.pid
        except Exception:
            pid = None
        try:
            cur_tag = self.player_tag()
        except Exception:
            cur_tag = None
        _, (_cur_world, _) = _SS.session_key(self)
        et = enemy_tag.upper() if enemy_tag else None
        prev = None
        if os.path.exists(cache) and not reset:
            try:
                with open(cache) as fh:
                    prev = json.load(fh)
            except Exception:
                prev = None
        reset_reason = None
        reset_detail = None
        if prev:
            prev_ts = prev.get("ts")
            age_s = (time.time() - float(prev_ts)) if prev_ts is not None else 0.0
            if prev.get("schema") != 2:
                reset_reason = "legacy_baseline"
            elif prev.get("pid") is None or prev.get("date") is None:
                reset_reason = "legacy_baseline"
            elif pid is not None and str(prev.get("pid")) != str(pid):
                reset_reason = "pid_changed"
            elif _cur_world and prev.get("world_ptr") and int(prev["world_ptr"]) != int(_cur_world):
                reset_reason = "save_epoch_changed"
            elif cur_tag and prev.get("player_tag") and str(prev.get("player_tag")).upper() != str(cur_tag).upper():
                reset_reason = "player_tag_changed"
            elif now_date is not None and prev.get("date") is not None and now_date < prev["date"]:
                reset_reason = "date_went_backwards"
            elif (
                age_s > 1800.0
                and now_date is not None
                and prev.get("date") is not None
                and abs(int(now_date) - int(prev["date"])) < 60 * 24
            ):
                # 2026-11-23 (复测 7 B-3): 同 PID 跨读档且新局恰好跑回与上一局相近日期时
                # （如上一局 1940.12 -> 读 1936 新档 -> 78 分钟后在 1940.12 首次调用），
                # 仅靠 now_date < prev["date"] 拦不住；若基线已陈旧 >30 分钟且全球差异省 >120，
                # 判定为跨档残留基线并自动重建，避免一口气报出 387 条上一局领土幽灵变动。
                po0 = prev.get("control") or {}
                raw_diff = sum(1 for k, v in control.items() if po0.get(str(k)) != v)
                if raw_diff >= 120:
                    reset_reason = "stale_cross_save_baseline"
            if reset_reason:
                reset_detail = {"reason": reset_reason,
                                "prev_pid": prev.get("pid"), "pid": pid,
                                "prev_world_ptr": prev.get("world_ptr"), "world_ptr": _cur_world,
                                "prev_date": prev.get("date"), "date": now_date,
                                "prev_tag": prev.get("player_tag"), "player_tag": cur_tag,
                                "prev_ts": prev.get("ts")}
                prev = None
        now = {"schema": 2, "ts": time.time(), "pid": pid, "world_ptr": _cur_world,
               "date": now_date,
               "player_tag": cur_tag,
               "enemy_tag": et,
               "control": {str(k): v for k, v in control.items()}}
        changed = []
        if prev and prev.get("control"):
            po = prev["control"]
            for k, v in now["control"].items():
                old = po.get(k)
                if old != v:
                    changed.append({"province": int(k), "from": old, "to": v})
        try:
            _SS.ensure_parent(cache)
            with open(cache, "w") as fh:
                json.dump(now, fh)
        except Exception:
            pass
        if et:
            changed = [c for c in changed if et in ((c.get("from") or ""), (c.get("to") or ""))]
        return {
            "changed_count": len(changed),
            "changed": changed[:300],
            "provinces_seen": len(control),
            "first_run": prev is None,
            "reset_reason": reset_reason,
            "reset_detail": reset_detail,
            "pid": pid,
            "date": now_date,
            "age_seconds": round(time.time() - prev["ts"], 1) if prev else None,
        }

    def border_fronts(self, enemy_tag, tag=None):
        """Front sections facing the target country, for drawing a line."""
        import armyops_win
        segs = armyops_win.fronts_towards(self, enemy_tag, tag)
        return [{"front_ptr": hex(s["front"]), "front_handle": s["front_handle"],
                 "section": s["section"],
                 "border_provinces": s["border_provinces"]}
                for s in segs]

    def draw_front_line(self, orders_group, enemy_tag=None, front_ptr=None,
                        section=None, frm=0, to=100000, assign="proportional"):
        """Give an army a front line order. With enemy_tag, the WHOLE border is covered."""
        # PE 1.19.5.0 通过 COrderNewRootCommand 直接同步入队挂师
        import armyops_win
        divs = armyops_win.divisions_of(self, orders_group)
        if front_ptr is not None:
            fp = int(front_ptr, 16) if isinstance(front_ptr, str) else int(front_ptr)
            ok, err, _cmd = armyops_win.draw_front_line(
                self, orders_group, fp, section, frm, to, divisions=divs)
            return {"ok": ok, "error": err, "divisions": len(divs)}
        if not enemy_tag:
            return {"ok": False, "error": "enemy_tag or front_ptr is required"}
        seen, res, new_ids = set(), [], []
        for s in armyops_win.fronts_towards(self, enemy_tag):
            fk = tuple(s["front_handle"])
            if fk in seen:
                continue
            seen.add(fk)
            before = {o["id"] for o in armyops_win.orders(self, orders_group)}
            ok, err, _cmd = armyops_win.draw_front_line(
                self, orders_group, s["front"], s["section"], frm, to,
                divisions=divs)
            fresh = [o["id"] for o in armyops_win.orders(self, orders_group)
                     if o["id"] not in before and o["id"] not in new_ids]
            if ok and not fresh:
                ok = False
                err = "the command posted but no order appeared"
            new_ids += fresh
            res.append({"front": s["front_handle"], "section": s["section"],
                        "ok": ok, "error": err,
                        "weight": len(s["border_provinces"]),
                        "new_orders": fresh})
        return {"ok": all(r["ok"] for r in res) if res else True,
                "segments": res, "new_order_ids": new_ids,
                "divisions": len(divs)}

    def assign_divisions_to_front(self, orders_group, enemy_tag=None, mode="proportional",
                                  divisions=None, order_ids=None):
        """Distribute an army's divisions across its front orders.

        mode: "proportional" (by front section length, the default),
              "even", "all" (every division to every order), or "none".
        Use mode="none" to leave divisions unassigned for manual placement later.
        """
        import armyops
        p = self.p
        self._assign_note = None
        if mode == "none":
            return {"ok": True, "note": "divisions left unassigned"}
        if sys.platform == "win32":
            # 2026-11-17: the whole PE path lived in armyops_win (order ids at
            # +0x244, assigned list at +0x210) while this function still called
            # the Linux module, so every assignment was a silent no-op and the
            # order kept saying "no unit is assigned to this order".
            import armyops_win as _aw
            orders = _aw.orders(self, orders_group)
            _assign = _aw.assign_to_order
        else:
            orders = armyops.orders_of(self, orders_group)
            _assign = armyops.assign_to_order
        if order_ids:
            # 2026-11-11: match by the ids the draw call actually produced. Matching by
            # `(root_front, section)` handle silently dropped every order (the engine
            # stores a different root handle than the CFront we drew on) and produced
            # the misleading "no front order to assign to".
            want_ids = {int(x) for x in order_ids}
            orders = [o for o in orders if o["id"] in want_ids]
            weights = [1] * len(orders)
        elif enemy_tag:
            want = {(tuple(s["front_handle"]), s["section"]): len(s["border_provinces"])
                    for s in armyops.fronts_towards(self, enemy_tag)}
            matched = [o for o in orders
                       if (tuple(o["root_front"]), o["root_section"]) in want]
            if matched:
                orders = matched
                weights = [want[(tuple(o["root_front"]), o["root_section"])] for o in orders]
            else:
                # 2026-11-11: the engine re-sections a front when it is drawn, so the
                # handle/section pair recorded at draw time no longer matches the
                # surviving order and every division was refused with the misleading
                # "no front order to assign to". An army's own orders are the right
                # target anyway; keep them and say so.
                self._assign_note = ("front handle filter matched nothing; used all %d "
                                     "order(s) of this army" % len(orders))
                weights = [1] * len(orders)
        else:
            weights = [1] * len(orders)
        if not orders:
            return {"ok": False, "error": "no front order to assign to"}
        divs = list(divisions) if divisions else armyops.group_divisions(self, orders_group)
        if not divs:
            return {"ok": False, "error": "the army has no divisions"}
        n = len(divs)
        if mode == "all":
            shares = [divs] * len(orders)
        else:
            if mode == "even":
                weights = [1] * len(orders)
            tot = sum(weights) or 1
            counts, used = [], 0
            for i, w in enumerate(weights):
                k = n - used if i == len(weights) - 1 else max(1, round(n * w / tot))
                k = min(k, n - used - (len(weights) - 1 - i))
                counts.append(max(0, k)); used += counts[-1]
            shares, at = [], 0
            for k in counts:
                shares.append(divs[at:at + k]); at += k
        def _orders_now():
            return (_aw.orders(self, orders_group) if sys.platform == "win32"
                    else armyops.orders_of(self, orders_group))

        def _pick(rows, want_id, want_root):
            """The order to actually write to.

            2026-11-18 (BUG-C): right after a line is drawn the engine renumbers or
            re-sections the order, so the id cached a second ago no longer exists.
            `COrderAssignCommand::Execute` then falls back to the parent group
            (`[og+0x1B8]`) and the assignment lands nowhere -- the exact
            "the command was accepted but the order still holds 0 divisions"
            seen on the 19-division army of the 1939.07.30 run. Re-resolving the
            survivor is what makes the second attempt work.
            """
            for x in rows:
                if x["id"] == want_id:
                    return x
            if want_root:
                for x in rows:
                    if x.get("type") == 2 and tuple(x.get("root_front") or ()) == want_root:
                        return x
            for x in rows:
                if x.get("type") == 2:
                    return x
            return None

        out = []
        for o, share in zip(orders, shares):
            if not share:
                continue
            want_root = tuple(int(x) for x in (o.get("root_front") or ()))
            before = after = None
            err = ""
            tries = 0
            for tries in range(1, 3):
                tgt = _pick(_orders_now(), o["id"], want_root)
                if tgt is None:
                    err = ("the order disappeared and this army has no front order left "
                           "to assign to")
                    self._settle(0.8)
                    continue
                if before is None:
                    before = tgt.get("assigned_divisions")
                ok, cerr = _assign(self, orders_group, tgt["id"], share)
                self._settle(0.6 if tries == 1 else 1.4)
                fresh = _pick(_orders_now(), tgt["id"], want_root)
                after = (fresh or {}).get("assigned_divisions")
                if ok and (after or 0) > 0:
                    err = ""
                    break
                err = cerr or ("the command was accepted but the order still holds "
                               "%s divisions" % (after,))
            row = {"order_id": o["id"], "divisions": len(share), "tries": tries,
                   "ok": not err, "error": err,
                   "assigned_before": before, "assigned_after": after}
            if err:
                row["hint"] = ("hoi4_army_battleplans now auto-assigns and deletes an "
                               "order it cannot fill; or hoi4_delete_order + redraw. "
                               "A front order with 0 divisions can crash the engine "
                               "(AGENTS.md rule 8)")
            out.append(row)
        res = {"ok": all(x["ok"] for x in out), "mode": mode, "orders": out}
        note = getattr(self, "_assign_note", None)
        if note:
            res["note"] = note
            self._assign_note = None
        return res

    def orders_of(self, orders_group):
        import armyops_win
        return armyops_win.orders(self, orders_group)

    def execute_plan(self, orders_group, order_id=0, flag=1, executing=None):
        """Execute or halt a battle plan (or a single type-1 offensive arrow).

        2026-11-23 (复测 11 P0-2): 经 PE 反汇编 `COrderExecuteCommand::Execute (0x18D64A0)`
        与 `COrderInstance::SetExecuting (0x10CD510)` 实证：
          * `executing=False` (或 `flag=0`) + `order_id=<type-1 child id>`：精准关闭单条进攻箭头，
            完全不动父 type-2 前线订单和师；
          * `executing=False` (或 `flag=0`) + `order_id=<type-2 root id>`：关闭该前线及全部子箭头；
          * `executing=False` (或 `flag=0`) + `order_id=0`：原生 `0x18E5D50(og, 0)` 全军转守；
          * `executing=True` (或 `flag=1`)：开启进攻计划。
        """
        eff_exec = bool(executing) if executing is not None else bool(int(flag))
        import armyops_win
        return armyops_win.set_plan_executing(
            self, orders_group, order_id=int(order_id or 0), executing=eff_exec
        )

    def offensive_line(self, orders_group, parent_order_id, provinces=None,
                       divisions=None, enemy_tag=None, depth=1,
                       allow_manual_fallback=False, enabled=True):
        """Draw or disable an offensive line under one front order (the arrow inside the front).

        2026-11-23 (复测 10 A-1 / A-2 / A-3 & 复测 11 P0-2 根治):
          0. 当 `enabled=False` 时，走原生 `set_plan_executing(og, parent_order_id, executing=False)`
             精准关停该前线下所有活跃 type-1 进攻箭头，完全不动父前线订单和师；
          1. 主路径 `armyops_win.engine_offensive` 接收**敌方目标省**（不是己方边境省）
             或直接传 `enemy_tag` 自动推导，调用真实 `COrderNewFrontCommand` ctor +
             `Execute (0x18D9070)`（通过 0xD57D90 敌对连通区匹配）并给产出的 type-1
             子命令原生分师；
          2. 默认关闭 `manual_offensive` 静默回退（`allow_manual_fallback=False`）：
             旧版静默退回单层空壳箭头、既不进攻还把 0x10B4EC0 参数写反导致父订单永久删不掉；
          3. 无论走哪条路径，最终必须实读核验该父订单下至少有 1 条 type-1 进攻箭头
             `assigned > 0`；若 `assigned == 0`，**必须返回 `ok: False`**（绝不假阳性骗人）！
        """
        if sys.platform == "win32":
            import armyops_win
            if enabled is False:
                res_off = armyops_win.set_plan_executing(
                    self, orders_group, order_id=int(parent_order_id), executing=False
                )
                res_off["method"] = "disable_offensive"
                return res_off
            e_ok, e_err, e_info = armyops_win.engine_offensive(
                self, orders_group, parent_order_id, provinces, divisions,
                enemy_tag=enemy_tag, depth=depth)
            if e_ok:
                kids = [c for c in (e_info.get("children") or [])
                        if c.get("root") == int(parent_order_id)
                        and c.get("type") == armyops_win.ORDER_TYPE_OFFENSIVE]
                fed_kids = [c for c in kids if (c.get("assigned") or 0) > 0]
                root_assigned = armyops_win.assigned_of(
                    self, orders_group, parent_order_id)
                out = {
                    "ok": bool(fed_kids),
                    "error": "" if fed_kids else (
                        "offensive arrow was built by engine_execute but has assigned_divisions=0 "
                        "(divisions will stay entrenched and NOT attack); use hoi4_plan_front or "
                        "hoi4_attack_country instead"
                    ),
                    "method": "engine_execute",
                    "target_provinces": (e_info or {}).get("target_provinces"),
                    "children": [(c["id"], c["assigned"], c["executing"]) for c in kids],
                    "children_fed": [c["id"] for c in fed_kids],
                    "children_executing": [c["id"] for c in kids if c.get("executing") == 1],
                    "root_assigned": root_assigned,
                }
                if (e_info or {}).get("auto_replaced_provinces"):
                    out["auto_replaced_provinces"] = e_info["auto_replaced_provinces"]
                if not fed_kids:
                    out["blocked_by"] = "zero_assigned_offensive_child"
                return out

            skipped = "engine_execute: %s" % e_err
            if "front-context layer empty" in (e_err or ""):
                return {"ok": False, "error": e_err, "method": "none",
                        "skipped": skipped,
                        "hint": ("draw the front via the UI on this group, or "
                                 "use hoi4_plan_front / hoi4_attack_country")}
            ok, err, _cmd = armyops_win.offensive_line(
                self, orders_group, parent_order_id, provinces, divisions)
            if ok:
                kids = [c for c in armyops_win.order_children(self, orders_group)
                        if c.get("root") == int(parent_order_id)
                        and c.get("type") == armyops_win.ORDER_TYPE_OFFENSIVE]
                fed_kids = [c for c in kids if (c.get("assigned") or 0) > 0]
                return {
                    "ok": bool(fed_kids),
                    "error": "" if fed_kids else "queue offensive_line produced 0-assigned child",
                    "blocked_by": None if fed_kids else "zero_assigned_offensive_child",
                    "method": "queue",
                    "children": [(c["id"], c["assigned"], c["executing"]) for c in kids],
                    "children_fed": [c["id"] for c in fed_kids],
                    "skipped": skipped,
                }
            if not allow_manual_fallback:
                return {
                    "ok": False,
                    "error": e_err or err or "engine_execute produced no offensive arrow",
                    "blocked_by": "engine_offensive_failed",
                    "method": "none",
                    "skipped": "; ".join(x for x in (skipped, err) if x),
                    "hint": (
                        "pass ENEMY target provinces (or enemy_tag='HOL') rather than own border "
                        "provinces; or use hoi4_plan_front(enemy_tag=..., armies=[...]) / "
                        "hoi4_attack_country(target_tag=...) to drive real attacks"
                    ),
                }
            m_ok, m_err, optr = armyops_win.manual_offensive(
                self, orders_group, parent_order_id, provinces)
            if not m_ok:
                return {"ok": False, "error": e_err or err or m_err,
                        "method": "none",
                        "skipped": "; ".join(x for x in (skipped, err) if x)}
            kids = [c for c in armyops_win.order_children(self, orders_group)
                    if c["type"] == armyops_win.ORDER_TYPE_OFFENSIVE
                    and c["ptr"] == optr]
            oid = kids[0]["id"] if kids else None
            fed = bool(kids and (kids[0].get("assigned") or 0) > 0)
            return {
                "ok": fed,
                "error": "" if fed else (
                    "manual_offensive created a decorative type-1 order with assigned_divisions=0 "
                    "(engine does not attack on manual_offensive arrows); use hoi4_plan_front "
                    "or hoi4_attack_country"
                ),
                "blocked_by": None if fed else "zero_assigned_offensive_child",
                "method": "manual_offensive",
                "order_ptr": optr,
                "order_id": oid,
                "children": [(c["id"], c["assigned"], c["executing"]) for c in kids],
                "children_fed": [c["id"] for c in kids if (c.get("assigned") or 0) > 0],
                "skipped": "; ".join(x for x in (skipped, err) if x),
            }
        raise NotImplementedError("offensive_line is Windows-only for now")

    def delete_order(self, orders_group, order_id, allow_child=False, tag=None):
        if isinstance(orders_group, str) and hasattr(self, "_resolve_orders_group"):
            og_i, og_err, og_cands = self._resolve_orders_group(orders_group, tag=tag)
            if not og_i:
                out_e = {"ok": False, "blocked_by": "invalid_orders_group", "error": og_err}
                if og_cands:
                    out_e["available_order_groups"] = og_cands[:20]
                return out_e
            orders_group = og_i
        # 2026-11-23 (复测 9 E-1 根治): Windows 1.19.5.0 统一走 armyops_win.delete_order
        # （cmdwin.alloc + CanExecute + attach_fake_vtable + 先停/清 type-1 子命令并回读核验）
        import armyops_win as _A
        return _A.delete_order(self, orders_group, order_id, allow_child=allow_child)

    def empty_order_scan(self, tag=None):
        """Read-only: every battle-plan order that holds 0 divisions.

        2026-11-18 (crash #4): `hoi4_pause(paused=False)` and
        `hoi4_pause_in_hours` both start the clock. The 1936.08 autosave
        *contains* three empty type-1 children left behind by the 15:10 crash,
        so loading it and unpausing killed hoi4.exe at 0xB2E62E within ~15 s
        (dump `hoi4_20261118_153843`, 15:38:44). Both unpause paths now refuse
        while this returns anything.
        """
        if sys.platform != "win32":
            return []
        import armyops as _ao
        import armyops_win as A
        c = self._c(tag)
        out = []
        for og in [x for x in _ao.orders_groups(self, c)
                   if not self.p.u8(x + A.OG_IS_GROUP)]:
            try:
                rows = A.orders(self, og)
                kids = A.order_children(self, og)
            except Exception:
                continue
            fed = {o["id"] for o in rows
                   if (o.get("assigned_divisions") or 0) > 0}
            for k in kids:
                if k.get("foreign_group"):
                    continue
                if (k.get("assigned") or 0) == 0 and (k.get("root") not in fed):
                    # 2026-11-19: same exemption as purge_empty_orders and the
                    # two plan_front sweep sites.  A type-1 ARROW under a front
                    # order that holds divisions is the state the UI itself
                    # leaves after the player draws one; the engine feeds it from
                    # the parent when the plan runs.  Refusing to start the clock
                    # for that state made every offensive line unusable -- the
                    # arrow was created, then this guard blocked the very tick
                    # that would have filled it.
                    out.append({"army": hex(og), "order": k["id"],
                                "type": k.get("type"), "kind": "child"})
            for o in rows:
                if (o.get("assigned_divisions") or 0) == 0:
                    out.append({"army": hex(og), "order": o["id"],
                                "type": o.get("type"), "kind": "root"})
        return out

    def reset_battleplans(self, tag=None):
        """Delete EVERY battle-plan order (children first, then roots), all armies.

        2026-11-18 (crash #5): the 1936.08 autosave carried three type-2 front
        orders whose type-1 children were gone. `purge_empty_orders` removed the
        empty children and `empty_order_scan()` then reported a clean list, yet
        unpausing still died at `hoi4.exe+0xB2E62E` ~90 s later -- because a ROOT
        order that still carries `executing` (u8 at +0x248) but has **no**
        children is the same empty `CPdxArray` read. Deleting only the empty
        orders is therefore not enough after a load: drop the whole order tree
        and draw one fresh plan.
        """
        if sys.platform != "win32":
            return {"ok": False, "error": "reset_battleplans is Windows-only"}
        import armyops as _ao
        import armyops_win as A
        c = self._c(tag)
        ogs = [og for og in _ao.orders_groups(self, c)
               if not self.p.u8(og + A.OG_IS_GROUP)]
        deleted, errors = [], []
        for og in ogs:
            try:
                rows = A.orders(self, og)
                kids = A.order_children(self, og)
            except Exception as e:
                errors.append({"army": hex(og), "error": repr(e)})
                continue
            def _live_ids(_og):
                try:
                    _r_now = A.orders(self, _og) or []
                except Exception:
                    _r_now = []
                try:
                    _k_now = A.order_children(self, _og) or []
                except Exception:
                    _k_now = []
                return ({int(x.get("id") or -1) for x in _r_now},
                        {int(x.get("id") or -1) for x in _k_now})

            for k in kids:
                if k.get("foreign_group"):
                    continue
                kid_id = int(k["id"])
                _cur_r, _cur_k = _live_ids(og)
                if kid_id not in _cur_k and kid_id not in _cur_r:
                    deleted.append({"army": hex(og), "order": kid_id, "kind": "child",
                                    "already_removed": True})
                    continue
                # 2026-11-23 (E-1): 走 PE 原生安全 armyops_win.delete_order(..., allow_child=True)
                try:
                    r = A.delete_order(self, og, kid_id, allow_child=True)
                except Exception as e:
                    r = {"ok": False, "error": repr(e)}
                (deleted if r.get("ok") else errors).append(
                    {"army": hex(og), "order": kid_id, "kind": "child",
                     **({} if r.get("ok") else {"error": r.get("error")})})
            for o in rows:
                oid = int(o["id"])
                _cur_r, _cur_k = _live_ids(og)
                if oid not in _cur_r and oid not in _cur_k:
                    deleted.append({"army": hex(og), "order": oid, "kind": "root",
                                    "type": o.get("type"), "already_removed": True})
                    continue
                r = self.delete_order(og, oid)
                (deleted if r.get("ok") else errors).append(
                    {"army": hex(og), "order": oid, "kind": "root",
                     "type": o.get("type"),
                     **({} if r.get("ok") else {"error": r.get("error")})})
        left = []
        try:
            left = self.empty_order_scan(tag)
        except Exception:
            pass
        all_ok = (not errors) and (not left)
        return {"ok": all_ok, "armies": len(ogs), "deleted_count": len(deleted),
                "failed_count": len(errors),
                "deleted": deleted, "errors": errors,
                "orders_left": left,
                "note": (
                    "every battle-plan order is gone; draw a fresh plan with "
                    "hoi4_army_battleplans before any war"
                    if all_ok else
                    "reset_battleplans incomplete: %d error(s), %d order(s) left "
                    "(check `errors` and `orders_left` before unpausing)"
                    % (len(errors), len(left))
                )}

    def purge_empty_orders(self, tag=None):
        """删掉所有 0 师的战斗计划命令（type-2 根订单 **和** type-1 进攻子命令）。

        2026-11-18：`hoi4.exe+0xB2E62E` 是 `movsxd rdx,[rax+rcx*4-4]` —— 对空
        `CPdxArray` 取 `data[size-1]`，size=0 时读出 `[-4]` → ACCESS_VIOLATION。
        一条不挂师的命令，它的师列表正好就是空数组。所以**任何** 0 师命令
        （根命令或进攻子命令）都是一颗延时炸弹，读档之后应该先扫一遍。

        只删空命令，不动有师的；先删子命令再删根命令，避免留下孤儿子命令。
        """
        if sys.platform != "win32":
            return {"ok": False, "error": "purge_empty_orders is Windows-only"}
        import armyops as _ao
        import armyops_win as A
        c = self._c(tag)
        ogs = [og for og in _ao.orders_groups(self, c)
               if not self.p.u8(og + A.OG_IS_GROUP)]
        removed, kept = [], 0
        for og in ogs:
            try:
                rows = A.orders(self, og)
                kids = A.order_children(self, og)
            except Exception as e:
                removed.append({"army": hex(og), "error": repr(e)})
                continue
            fed_roots = {o["id"] for o in rows
                         if (o.get("assigned_divisions") or 0) > 0}
            empty_kids = [k["id"] for k in kids
                          if not k.get("foreign_group")
                          and (k.get("assigned") or 0) == 0
                          # 2026-11-19: a 0-division type-1 ARROW under a front
                          # order that DOES hold divisions is the normal state the
                          # UI leaves behind after drawing one -- the engine feeds
                          # it from the parent when the plan runs.  Purging those
                          # deleted the arrow we had just created (this is the
                          # same fix plan_front needed at both of its sweep sites;
                          # this third copy was missed).
                          and (k.get("root") not in fed_roots)]
            empty_roots = [o["id"] for o in rows
                           if (o.get("assigned_divisions") or 0) == 0]
            kept += len([o for o in rows
                         if (o.get("assigned_divisions") or 0) > 0])
            empty_kid_set = set(empty_kids)
            # 2026-11-26 (B7 根治): 删除一条子订单或父订单时，引擎可能会级联移除同树下的
            # 其他订单（例如删完父子链中的一条后另一条同步消失）。每步删除前先实时回读内存
            # 中的现存订单 ID，已不在树中则视为已被级联移除，不再发多余删单造成假失败！
            for oid in empty_kids + empty_roots:
                is_child = oid in empty_kid_set
                try:
                    cur_roots = {int(x.get("id") or -1) for x in (A.orders(self, og) or [])}
                    cur_kids = {int(x.get("id") or -1) for x in (A.order_children(self, og) or [])}
                except Exception:
                    cur_roots, cur_kids = None, None
                if cur_roots is not None and oid not in cur_roots and oid not in cur_kids:
                    removed.append({"army": hex(og), "order": oid,
                                    "kind": "child" if is_child else "root",
                                    "ok": True, "already_removed": True, "error": ""})
                    continue
                r = self.delete_order(og, oid, allow_child=is_child)
                removed.append({"army": hex(og), "order": oid,
                                "kind": "child" if is_child else "root",
                                "ok": r.get("ok"), "error": r.get("error")})
        # 2026-11-21 (P2-8): `removed_count` used to be len(removed), i.e. it
        # counted ATTEMPTS -- a run that deleted 1 of 3 orders and failed twice
        # reported `removed_count: 3` and the caller believed the sweep was
        # clean.  This sweep exists to prove a crash hazard is gone, so "how many
        # were really deleted" and "how many could not be" must be separate
        # numbers, and `ok` must say whether any attempt failed.
        _deleted = sum(1 for r in removed if r.get("ok") is True)
        _failed = len(removed) - _deleted
        left = []
        try:
            left = self.empty_order_scan(tag) or []
        except Exception:
            left = []
        return {"ok": (_failed == 0 and not left), "armies": len(ogs), "kept_orders": kept,
                "removed_count": _deleted, "attempted_count": len(removed),
                "failed_count": _failed, "removed": removed, "orders_left": left,
                "note": ("0-division orders crash the engine at hoi4.exe+0xB2E62E; "
                         "this list should be empty before any war")}

    def _border_divisions(self, target_tag, tag=None):
        """How many of our divisions are physically on the border we would attack.

        This is the check the 2026-11-16 campaign needed and did not have: the
        war-readiness gate only counted divisions inside the country, so 112
        divisions parked in Berlin passed it and then watched the Netherlands
        walk in.
        """
        import divenum
        import mapdata
        import unitorders
        c = self._c(tag)
        me = c.tag
        tgt = str(target_tag).upper()
        out = {"own_tag": me, "enemy_tag": tgt, "border_provinces": 0,
               "border_sample": [], "divisions_on_border": 0, "divisions_count": 0}
        try:
            cmap = mapdata.province_controller_map(self)
            border = mapdata.border_provinces(self, me, tgt, owner_by_id=cmap)
        except Exception as e:
            out["error"] = "border map failed: %r" % (e,)
            return out
        out["border_provinces"] = len(border)
        out["border_sample"] = border[:16]
        try:
            rows = divenum.armies(self, me)
        except Exception as e:
            out["error"] = "division list failed: %r" % (e,)
            return out
        out["divisions_count"] = rows["count"]
        bset = set(border)
        on = []
        for a in rows["armies"]:
            try:
                pv = unitorders.province_of(self, a)
            except Exception:
                continue
            if pv in bset:
                on.append(pv)
        out["divisions_on_border"] = len(on)
        return out

    def _front_directive_check(self, target_tag):
        """Is a real tactical tick driving the army against this enemy?

        `hoi4_set_front_directive` only writes a JSON file.  Unless something
        executes it, `last_tick` stays null -- which is exactly what happened
        for the whole of the 1939 campaign.  Treating a stored directive as an
        armed one is what let the army stand still while war was declared.
        """
        import movemicro
        tgt = str(target_tag).upper()
        try:
            rows = (movemicro.front_directives(self) or {}).get("directives") or []
        except Exception as e:
            return {"ok": False, "error": repr(e)}
        row = None
        for r in rows:
            if str(r.get("enemy_tag") or "").upper() == tgt:
                row = r
                break
        info = {"directive": bool(row), "stance": (row or {}).get("stance"),
                "last_tick": (row or {}).get("last_tick"),
                "hours_since_tick": (row or {}).get("hours_since_tick")}
        if not row:
            info["problem"] = ("no front directive armed for %s -- run "
                               "hoi4_set_front_directive(enemy_tag=%s) first" % (tgt, tgt))
            return info
        lt = row.get("last_tick") or {}
        pid = getattr(self.p, "pid", None)
        if not lt:
            info["problem"] = ("directive stored but NEVER ticked (last_tick is null): "
                               "nothing is driving the army. Start the frontd worker or "
                               "call hoi4_tactical_front_tick(force=true)")
            return info
        if lt.get("pid") is not None and pid is not None and int(lt["pid"]) != int(pid):
            info["problem"] = ("last_tick belongs to game pid %s, current pid is %s -- "
                               "stale tick from a previous process" % (lt.get("pid"), pid))
            return info
        seen = lt.get("divisions_seen")
        if seen is not None and int(seen) <= 0:
            info["problem"] = ("the last tick ran but saw 0 of our divisions, so it has "
                               "nothing to command -- the army is not on the map for the "
                               "executor (check hoi4_army_status / hoi4_division_selftest)")
            return info
        info["ok"] = True
        age = row.get("hours_since_tick")
        # 2026-11-23 (复测 6 P2-3): 游戏当前处于暂停态时，后台 worker 本就无法推时钟，
        # 不应误报 "worker may be stalled" 干扰战前/战中决策。
        paused_now = None
        try:
            import session as _sess_fd
            paused_now = bool(_sess_fd.is_paused(self))
        except Exception:
            paused_now = None
        info["game_paused"] = paused_now
        if age is not None and age > 72:
            if paused_now:
                info["paused_note"] = (
                    "game is currently paused; last tick was %s game hours ago "
                    "(not stalled while paused)" % age
                )
            else:
                info["warning"] = (
                    "last tick was %s game hours ago; the worker may be stalled" % age
                )
        return info

    def _front_plan_check(self, target_tag, tag=None):
        """原生战役计划检查：前线 + 进攻线 + 至少一个集团军真的挂在上面。

        2026-11-18。1939.07.30 那一局在「波兰边境 8 个师、西线 0 个师」的状态下
        就宣了战，因为当时唯一的硬门槛是 frontd 指令——而那条指令从来没 tick 过。
        真正能证明「有部队会去打」的，是引擎自己持有的那条战役计划：

          1. 存在一条 type-2 前线命令，且它的 (front, section) 正对目标国；
          2. 这条前线命令下面至少有一个 type-1 进攻子命令 —— 只有前线只会「守住」，
             永远不会进攻；
          3. 至少有一个集团军真的把师指派到了这条命令上（`assigned_divisions > 0`），
             而不只是把线画出来。

        画线本身是和平时期动作（`hoi4_army_battleplans` 的 "not at war" 门禁已经
        拆掉，就是为这个）：开战前一天准备好的计划，才是开战第一天和和平最后一天
        的区别。
        """
        tgt = str(target_tag).upper()
        info = {"enemy_tag": tgt, "ok": False, "armies": []}
        if sys.platform != "win32":
            # 非 Windows 平台没有 armyops_win，不去挡一个我们读不了的东西。
            info["ok"] = True
            info["skipped"] = "native battle-plan check is Windows-only"
            return info
        import armyops_win as A
        import armyops as _ao
        try:
            co = self._c(tag)
            ogs = [og for og in _ao.orders_groups(self, co)
                   if not self.p.u8(og + A.OG_IS_GROUP)]
        except Exception as e:
            info["problem"] = "cannot enumerate armies: %r" % (e,)
            return info
        try:
            segs = A.fronts_towards(self, tgt, co.tag)
        except Exception as e:
            info["problem"] = "cannot enumerate front sections: %r" % (e,)
            return info
        facing = {tuple(int(x) for x in s["front_handle"]) for s in segs}
        info["sections_facing"] = len(facing)
        if not facing:
            info["problem"] = ("no front section of %s touches %s -- there is no border "
                               "to draw a front line on" % (co.tag, tgt))
            return info
        best = None
        for og in ogs:
            try:
                rows = A.orders(self, og)
            except Exception:
                continue
            kids = None
            for o in rows:
                if o["type"] != 2:
                    continue
                rf = tuple(int(x) for x in (o.get("root_front") or ()))
                if rf not in facing:
                    continue
                if kids is None:
                    try:
                        kids = A.order_children(self, og)
                    except Exception:
                        kids = []
                mine = [c for c in kids if c["root"] == o["id"] and c["type"] == 1]
                row = {"army": hex(og),
                       "order": o["id"],
                       "assigned_divisions": int(o.get("assigned_divisions") or 0),
                       "offensive_lines": len(mine),
                       "offensive_assigned": sum(int(c.get("assigned") or 0) for c in mine)}
                info["armies"].append(row)
                if row["assigned_divisions"] > 0 and row["offensive_lines"] > 0:
                    best = row
        if best:
            info["ok"] = True
            info["plan"] = best
            return info
        if not info["armies"]:
            info["problem"] = ("no front line faces %s -- draw it BEFORE declaring with "
                               "hoi4_army_battleplans(enemy_tag=%s) (peacetime drawing is "
                               "allowed on purpose)" % (tgt, tgt))
        elif all(r["assigned_divisions"] <= 0 for r in info["armies"]):
            info["problem"] = ("the front line against %s exists but NO army is assigned to "
                               "it (assigned_divisions == 0) -- run hoi4_assign_divisions("
                               "orders_group=..., enemy_tag=%s) and read hoi4_orders back "
                               "until assigned > 0" % (tgt, tgt))
        else:
            info["problem"] = ("the front line against %s has divisions but NO offensive "
                               "line -- a front alone only holds, it never attacks. Add the "
                               "arrow with hoi4_offensive_line(orders_group=..., "
                               "parent_order_id=...)" % (tgt,))
        return info

    def war_readiness_check(self, target_tag, tag=None, force=False, hard=True,
                            ack_co_belligerents=False, confirm_escalation=False):
        """Would a human press this button? Divisions, rifles, border, front plan,
        wargoal, and diplomatic escalation (factions / guarantees / subjects).

        Returns {"ready": bool, "blockers": [...], "tactical_blockers": [...],
                 "strategic_blockers": [...], "warnings": [...]}.
        Nothing is written to the game; every number comes from the same reads
        the rest of the tool uses.

        2026-11-27 (D-20 根治):
          明确拆分「战术门禁」（`tactical_blockers`：边境驻军/前线计划/战争借口，可由
          `hard=False` / `override_checks=True` 跳过）与「战略门禁」（`strategic_blockers`：
          外交非法 + 高危战略升级：列强/外部保独 `guaranteed_by`、主要国裸边境、或拉入主要国且
          联军总师数压倒性超出我军）。
          * 泛泛的 `override_checks=True` 或布尔 `ack_co_belligerents=True` 不再静默放行
            列强保独或主要国裸边境这类 8.5x 级联世界大战！
          * 需通过 `confirm_escalation=True` 或具名列表 `ack_co_belligerents=["ENG","FRA"]`
            覆盖所有高危新增保独/裸边境主要国方可放行。
        """
        import time as _time
        _t0 = _time.perf_counter()
        _timings_ms = {}
        c = self._c(tag)
        me = c.tag
        tgt_tag = str(target_tag).upper()
        _t = _time.perf_counter()
        div = self.divisions(me, limit=1, deep=False)
        my_divs = int(div.get("count") or 0)
        try:
            enemy_divs = int((self.divisions(tgt_tag, limit=1, deep=False) or {}).get("count") or 0)
        except Exception:
            enemy_divs = 0
        _timings_ms["divisions"] = round((_time.perf_counter() - _t) * 1000.0, 2)
        rifles = 0.0
        try:
            for row in (self.equipment_stockpile(me) or {}).get("stockpile") or []:
                if "infantry_equipment" in str(row.get("equipment") or "").lower():
                    rifles += float(row.get("count") or 0)
        except Exception:
            pass
        try:
            completed = set(self.completed_focuses(me) or [])
        except Exception:
            completed = set()
        try:
            year = int(self.date().get("year") or 0)
        except Exception:
            year = 0

        tactical_blockers, strategic_blockers, warnings = [], [], []
        checks = {"divisions": {"count": my_divs, "source": div.get("source"),
                                "verified": div.get("verified")}}
        if div.get("warning"):
            checks["divisions"]["warning"] = div["warning"]

        # 解析 ack_co_belligerents：支持 bool（普通附庸/小国连带）或具名国家列表（确认保独/列强）
        ack_named_tags = set()
        ack_bool = False
        if isinstance(ack_co_belligerents, (list, tuple, set)):
            ack_named_tags = {str(x).strip().upper() for x in ack_co_belligerents if str(x).strip()}
            ack_bool = bool(ack_named_tags)
        else:
            ack_bool = bool(ack_co_belligerents)
        esc_confirmed = bool(confirm_escalation)

        # ---- 2026-11-23 (复测 6 P0-1) + 2026-11-27 (D-20): 实时外交连锁推演与战略门禁拆分
        _t = _time.perf_counter()
        diplo = {}
        try:
            import diplomacy as _dip_fc
            diplo = _dip_fc.war_escalation_forecast(self, tgt_tag, me_tag=me) or {}
        except Exception as _de:
            diplo = {"error": repr(_de)}
        _timings_ms["diplomacy"] = round((_time.perf_counter() - _t) * 1000.0, 2)
        checks["diplomacy"] = diplo
        for _db in diplo.get("diplomatic_blockers") or []:
            strategic_blockers.append(_db)
        for _dw in diplo.get("diplomatic_warnings") or []:
            warnings.append(_dw)

        new_cbs_set = {str(t).upper() for t in (diplo.get("new_co_belligerents") or [])}
        new_guarantors = [str(t).upper() for t in (diplo.get("guaranteed_by") or []) if str(t).upper() in new_cbs_set]
        undefended_list = list(diplo.get("undefended_new_co_belligerents") or [])
        undefended_majors = [u for u in undefended_list if u.get("is_major")]
        new_major_cbs = [
            str(r.get("tag") or "").upper()
            for r in (diplo.get("co_belligerent_details") or [])
            if (not r.get("already_at_war")) and r.get("is_major")
        ]
        total_enemy_est = int(diplo.get("total_enemy_divisions_est") or enemy_divs)

        # 高危灾难性战略升级判定（D-20 精准门禁，不误伤正常均衡大战）：
        #   条件 A: 新增保独国或新增主要国参战，且联军总敌军 >= 我军 2 倍（如 50 师撞 188 师 3.76x）
        #   条件 B: 新增 >= 2 条无防御裸边境（如 FRA + AST + MAL 3 条空门）
        #   条件 C: 新增保独国 + 存在主要国 0 驻军裸边境 + 联军总兵力 > 我军 1.8 倍
        # 普通单条裸边境仍走 strategic_blockers 拦阻（不被 override_checks 静默绕过），
        # 但非灾难级时允许显式 ack_co_belligerents=True 放行；灾难级必须传 confirm_escalation=True
        # 或具名列表 ack_co_belligerents=["ENG", "FRA", ...] 明确覆盖高危国！
        overwhelming_coalition = bool(
            (new_guarantors or new_major_cbs)
            and my_divs > 0
            and total_enemy_est >= max(my_divs * 2, my_divs + 50)
        )
        multi_undefended = len(undefended_list) >= 2
        guarantor_with_undefended_major = bool(
            new_guarantors and undefended_majors
            and my_divs > 0 and total_enemy_est >= int(my_divs * 1.8)
        )
        severe_esc_tags = []
        if overwhelming_coalition or multi_undefended or guarantor_with_undefended_major:
            for _st in list(new_guarantors) + [str(u.get("tag") or "").upper() for u in undefended_majors]:
                if _st and _st not in severe_esc_tags:
                    severe_esc_tags.append(_st)
            if overwhelming_coalition:
                for _mt in new_major_cbs:
                    if _mt and _mt not in severe_esc_tags:
                        severe_esc_tags.append(_mt)
            if multi_undefended:
                for u in undefended_list:
                    _ut = str(u.get("tag") or "").upper()
                    if _ut and _ut not in severe_esc_tags:
                        severe_esc_tags.append(_ut)

        unconfirmed_severe = [t for t in severe_esc_tags if t not in ack_named_tags]
        if severe_esc_tags and (not esc_confirmed) and unconfirmed_severe:
            _undef_desc = ", ".join(
                "%s(0 divisions on %d border provinces)" % (u.get("tag"), int(u.get("border_provinces") or 0))
                for u in undefended_list
            ) or "none"
            _esc_msg = (
                "STRATEGIC ESCALATION GUARD: declaring on %s triggers catastrophic escalation "
                "(guaranteed_by=%s, new_co_belligerents=%d, est. %d enemy divs vs your %d, "
                "undefended_borders=[%s]). Generic override_checks/ack_co_belligerents=true does NOT "
                "auto-approve overwhelming guarantor coalitions or multi-front undefended borders -- pass "
                "confirm_escalation=true or ack_co_belligerents=%s to explicitly confirm."
                % (
                    tgt_tag,
                    new_guarantors or [],
                    len(new_cbs_set),
                    total_enemy_est,
                    my_divs,
                    _undef_desc,
                    sorted(severe_esc_tags),
                )
            )
            if hard or not force:
                strategic_blockers.append(_esc_msg)
        elif (not ack_bool) and (not esc_confirmed) and (hard or not force):
            for _ub in undefended_list:
                strategic_blockers.append(
                    "undefended co-belligerent border: declaring on %s pulls in %s (%s, %d enemy divs) "
                    "which shares %d border provinces with %s, but 0 divisions guard that border -- "
                    "deploy defense first or pass ack_co_belligerents=true to proceed anyway"
                    % (tgt_tag, _ub["tag"], ", ".join(_ub.get("reasons") or ["ally"])[:80],
                       int(_ub.get("enemy_divisions") or 0),
                       int(_ub.get("border_provinces") or 0), me)
                )

        # ---- hard check 1: can we count an army at all, and is there one?
        if not my_divs:
            tactical_blockers.append(
                "no divisions: %s reports 0 (source=%s). Either the army really is "
                "gone or the enumeration is broken -- in both cases a war is lost "
                "before it starts. Run hoi4_division_selftest(deep=true) to tell the "
                "two apart" % (me, div.get("source")))

        # ---- hard checks 2+3: only when the caller wants them
        if hard:
            _t = _time.perf_counter()
            bd = self._border_divisions(tgt_tag, tag=tag)
            _timings_ms["border"] = round((_time.perf_counter() - _t) * 1000.0, 2)
            checks["border"] = bd
            if bd.get("error"):
                tactical_blockers.append("cannot verify the border: %s" % bd["error"])
            elif not bd.get("border_provinces"):
                tactical_blockers.append("no land border province owned by %s touches %s"
                                         % (me, tgt_tag))
            elif not bd.get("divisions_on_border"):
                tactical_blockers.append(
                    "no division on the %s border: %d divisions exist (%d border "
                    "provinces) but none stands on one of them -- deploy first"
                    % (tgt_tag, bd.get("divisions_count") or 0,
                       bd.get("border_provinces") or 0))
            # 2026-11-18: 硬门槛从「frontd 指令 tick 过」换成「原生前线 + 进攻线 +
            # 至少一个集团军已指派」。旧门槛只证明有个后台线程在算，不证明地图上
            # 有任何人会去打仗——1939.07.30 就是这么在 8 个师的状态下宣的战。
            _t = _time.perf_counter()
            fp = self._front_plan_check(tgt_tag, tag=tag)
            _timings_ms["front_plan"] = round((_time.perf_counter() - _t) * 1000.0, 2)
            checks["front_plan"] = fp
            if not fp.get("ok"):
                tactical_blockers.append(fp.get("problem") or
                                         "no battle plan with an offensive line faces %s" % tgt_tag)
            # ---- hard check 4 (2026-11-18 / 2026-11-23 复测 7 B-1 & B-7 根治):
            # 必须有战争借口。Windows 1.19.5 已打通 O(1) 实时直读 CDiplomacyStatus+0x80
            # （0.2ms，100% 包含新造 CB 与国策 CB），不再被旧 fast list 漏新 arena 坑死！
            # 若底层结构读不到（非 Windows 或异常）且未走权威路径，绝不伪报 "missing" 骗模型重造 200 天。
            _t = _time.perf_counter()
            _cb, _just, _werr = None, [], None
            _wg_source = "unknown"
            _direct_authoritative = False
            try:
                import diplomacy as _dip
                if hasattr(_dip, "country_wargoals_direct"):
                    _drows, _dok = _dip.country_wargoals_direct(self, tag=me, valid_only=True)
                    if _dok:
                        _direct_authoritative = True
                        _wg_source = "CDiplomacyStatus+0x80 (O(1) live)"
                        _cb = next(
                            (w for w in _drows
                             if str(w.get("target_tag") or "").upper() == tgt_tag),
                            None,
                        )
                        if _cb is None:
                            try:
                                _itgt = _dip.tag_index(self, tgt_tag)
                                _cb = next(
                                    (w for w in _drows if w.get("target_index") == _itgt),
                                    None,
                                )
                            except Exception:
                                pass
                if _cb is None and not _direct_authoritative:
                    # 兼容非 Windows 或离线单测 mock find_wargoal：调用 find_wargoal(self, tgt_tag, deep=None)
                    try:
                        _cb = _dip.find_wargoal(self, tgt_tag, deep=None)
                    except TypeError:
                        _cb = _dip.find_wargoal(self, tgt_tag)
                    _wg_source = "find_wargoal(deep=None)"
            except Exception as _e:
                _werr = repr(_e)
                _dip = None
            if _dip is not None:
                try:
                    # 2026-11-26 (长跑 §12 性能根治): 优先走毫秒级 0-deep-scan 的 justifying_fast，
                    # 消除无正当化时 _dip.justifying() 触发 3.8GB 全堆扫描导致 war_readiness 耗时 3-4s。
                    # 若单测显式 monkey-patch 了 _dip.justifying，则自动尊重单测桩。
                    if getattr(_dip.justifying, "__name__", "") != "justifying":
                        _jrows = _dip.justifying(self)
                    elif hasattr(_dip, "justifying_fast"):
                        _jrows = _dip.justifying_fast(self, tag=me)
                    else:
                        _jrows = _dip.justifying(self)
                    _just = [j for j in (_jrows or ())
                             if str((j or {}).get("target") or "").upper() == tgt_tag]
                except Exception:
                    _just = []
            _timings_ms["wargoal"] = round((_time.perf_counter() - _t) * 1000.0, 2)
            _prob, _state = _wargoal_gate(tgt_tag, _cb, _just)
            checks["wargoal"] = {
                "state": _state,
                "casus_belli": _cb,
                "justifying": _just,
                "error": _werr,
                "authoritative": bool(_direct_authoritative or (_cb is not None)),
                "source": _wg_source,
            }
            if _werr:
                checks["wargoal"]["state"] = "unknown"
                tactical_blockers.append(
                    "cannot verify a casus belli against %s (%s) -- tool lookup error, "
                    "do NOT re-justify blindly; verify with hoi4_wargoals first"
                    % (tgt_tag, _werr)
                )
            elif _prob:
                tactical_blockers.append(_prob)
            # frontd 指令降级为提示：它管的是逐师微操，不再是宣战的前置条件。
            fd = self._front_directive_check(tgt_tag)
            checks["front_directive"] = fd
            if not fd.get("ok"):
                warnings.append(fd.get("problem") or "front directive is not ticking")
            elif fd.get("warning"):
                warnings.append(fd["warning"])

        # ---- advisory (skippable with force=True)
        min_divs = max(12 if me == "GER" else 8, int(enemy_divs * 1.2))
        if my_divs < min_divs:
            warnings.append("thin army: have %d, want >= %d (%s has %d)"
                            % (my_divs, min_divs, tgt_tag, enemy_divs))
        elif total_enemy_est > enemy_divs and my_divs < int(total_enemy_est * 0.8):
            warnings.append(
                "coalition imbalance: %s has %d divisions against est. %d total enemy divisions "
                "(%s %d + co-belligerents %d)"
                % (me, my_divs, total_enemy_est, tgt_tag, enemy_divs,
                   max(0, total_enemy_est - enemy_divs))
            )
        if rifles < 1500:
            warnings.append("low infantry_equipment stockpile: %.0f (want >= 1500)"
                            % rifles)
        if (me == "GER" and year < 1939
                and "GER_danzig_or_war" not in completed
                and not all(f in completed for f in self.GER_SNOWBALL[:2])):
            have = [f for f in self.GER_SNOWBALL[:2] if f in completed]
            warnings.append("historical timeline: %s not done (year=%d, done=%s) -- a minor "
                            "war goal before that is allowed, it just raises world tension"
                            % (" + ".join(self.GER_SNOWBALL[:2]), year, have))

        _timings_ms["total"] = round((_time.perf_counter() - _t0) * 1000.0, 2)
        blockers = list(strategic_blockers) + list(tactical_blockers)
        ready = not blockers
        return {
            "ready": ready,
            "blockers": blockers,
            "tactical_blockers": tactical_blockers,
            "strategic_blockers": strategic_blockers,
            "strategic_escalation_required_ack": sorted(unconfirmed_severe) if severe_esc_tags else [],
            "warnings": warnings,
            "advisory_only": bool(warnings) and not blockers,
            # kept for older callers
            "reasons": blockers + ([] if force else warnings),
            "checks": checks,
            "my_divisions": my_divs,
            "enemy_divisions": enemy_divs,
            "total_enemy_divisions_est": total_enemy_est,
            "target_faction": diplo.get("target_faction"),
            "guaranteed_by": diplo.get("guaranteed_by") or [],
            "overlord": diplo.get("overlord"),
            "subjects": diplo.get("subjects") or [],
            "co_belligerents": diplo.get("co_belligerents") or [],
            "new_co_belligerents": diplo.get("new_co_belligerents") or [],
            "co_belligerent_borders": diplo.get("co_belligerent_borders") or [],
            "infantry_equipment_stockpile": round(rifles, 1),
            "year": year,
            "completed_snowball": sorted(f for f in self.GER_SNOWBALL if f in completed),
            "timings_ms": _timings_ms,
        }

    def paradrop_targets(self, orders_group=None, source_province=None, limit=2000,
                         target_tag=None, tag=None, max_range_km=None):
        """纯只读枚举伞降可达目标陆地省（0 注入，PE 1.19.5.0 原生实现）。"""
        import armyops
        return armyops.paradrop_targets(
            self, orders_group=orders_group, source_province=source_province,
            limit=limit, target_tag=target_tag, tag=tag, max_range_km=max_range_km,
        )

    def paradrop(self, orders_group, source_province=None, target_province=0,
                 divisions=None, order_id=0, confirm=False, wait=8.0, dry_run=False,
                 replace_existing=False, allow_duplicate=False, tag=None):
        """创建/更新并可选激活伞降订单（type-4，PE 1.19.5.0 双命令原生实现）。"""
        if isinstance(orders_group, str) and hasattr(self, "_resolve_orders_group"):
            og_i, og_err, og_cands = self._resolve_orders_group(orders_group, tag=tag)
            if not og_i:
                out_e = {"ok": False, "blocked_by": "invalid_orders_group", "error": og_err}
                if og_cands:
                    out_e["available_order_groups"] = og_cands[:20]
                return out_e
            orders_group = og_i
        import armyops
        res = armyops.paradrop(
            self, orders_group, source_province, target_province,
            divisions=divisions, order_id=order_id, confirm=confirm,
            wait=wait, dry_run=dry_run,
            replace_existing=replace_existing, allow_duplicate=allow_duplicate,
        )
        if isinstance(res, tuple):
            if len(res) == 3:
                ok, err, info = res
                out = dict(info or {})
                out["ok"] = bool(ok)
                out["error"] = err or ""
                return out
            ok, err = res
            return {"ok": bool(ok), "error": err or ""}
        return res
