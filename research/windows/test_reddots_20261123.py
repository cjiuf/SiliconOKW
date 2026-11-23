# -*- coding: utf-8 -*-
"""红点硬门禁与后台急刹守护 (`calling/reddots.py`) 专项回归测试。

覆盖全部核心验收场景（全部纯离线 mock，零游戏写操作）：
  1. 全部 18 类红点触发与防误报：
     - 严控三项：PENDING_EVENT (可决策事件 / 避开 unresolved_scope / 1 按钮新闻降为 advisory)、
       NO_FOCUS、IDLE_RESEARCH；
     - 决议增量：首次建基线不误报常驻通用决议，出现新决议立刻触发 NEW_DECISION；
       限时倒计时任务触发 ACTIVE_MISSION；
     - 工业产能：EMPTY_CONSTRUCTION、IDLE_CIV（满 15 厂溢出触发，存在 <15 厂项证明榨干绝不误报消费品）、
       IDLE_MIL、IDLE_DOCK；
     - 资源溢流与阶梯复触发：PP (500->800)、陆/海/空 XP (100->250)；
     - 部队编制与扩军失衡：UNASSIGNED_DIVS、ARMY_NO_GENERAL（有空闲陆军将领阻塞，将领池空自动降为 advisory）、
       SURPLUS_NO_TRAINING、DEFICIT_OVER_TRAINING；
     - 海空与策略告警：IDLE_AIR / IDLE_NAVY（和平期 advisory、战时 block，可开关切换）、FUEL_ZERO、OBSOLETE_LINE。
  2. 签收与防死锁：
     - 空 reason 拒绝；
     - 严控三项拒绝普通 ack，force=True 最长豁免 7 天；
     - 普通项默认豁免 30 天，到期或数值跨档/对象变化立即破冷却复触发。
  3. 三路联动闭环：
     - 闸门 A：TurnLoop.wait() 开跑前硬拦截 & 跑表期命中新红点强制停表；
     - 闸门 B：check_unpause_gate() + 一步 ack_red_dots 放行；
     - 闸门 C：RedDotGuard.poll_once() 仅在运行态急刹一次，同一签名绝不重复按暂停；
     - Compact turnengine：briefing 置顶 MUST_RESOLVE、ack_red_dot 无需前台窗口、resume 被红点拦截。
"""
import os
import shutil
import sys
import tempfile
import types

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
CALLING = os.path.join(ROOT, "calling")
MCP_DIR = os.path.join(ROOT, "mcp")
for p in (CALLING, MCP_DIR):
    if p not in sys.path:
        sys.path.insert(0, p)

import reddots as RD

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] %s" % name)
    else:
        FAIL += 1
        print("  [FAIL] %s -- %s" % (name, detail))


class FakeProc:
    def __init__(self, pid=99999):
        self.pid = pid
        self.base = 0x140000000


class FakeCountry:
    def __init__(self, **kw):
        self.tag = kw.get("tag", "GER")
        self.political_power = kw.get("pp", 120.0)
        self.civilian_factories = kw.get("civ", 40)
        self.military_factories = kw.get("mil", 30)
        self.naval_dockyards = kw.get("dock", 10)
        self.manpower = kw.get("manpower", 200000)
        self.fuel = kw.get("fuel", 50000.0)
        self.experience = kw.get("xp", {"army": 20.0, "navy": 10.0, "air": 15.0})

    def summary(self):
        return {
            "tag": self.tag,
            "political_power": self.political_power,
            "civilian_factories": self.civilian_factories,
            "military_factories": self.military_factories,
            "naval_dockyards": self.naval_dockyards,
            "manpower": self.manpower,
            "fuel": self.fuel,
            "experience": self.experience,
        }


class FakeGame:
    """完全可控的模拟游戏对象，用于精准构造每一种红点与门禁状态。"""

    def __init__(self, **kw):
        self.p = FakeProc(kw.get("pid", 99999))
        self._oi = object()  # 标记索引就绪，让海空/将领检测可跑
        self._tag = kw.get("tag", "GER")
        self._date_raw = kw.get("date_raw", 100000)
        self._date_text = kw.get("date_text", "1936.06.01.00")
        self._country = FakeCountry(tag=self._tag, **kw.get("country", {}))
        self._focus = kw.get("focus", {"key": "GER_four_year_plan"})
        self._sel_focuses = kw.get("sel_focuses", {"count": 3, "focuses": ["GER_rhineland", "GER_anschluss"]})
        self._slots = kw.get("slots", [{"slot": 0, "slot_busy": True, "researching": "t1"}])
        self._avail_research = kw.get("avail_research", {"count": 5, "available": [{"id": "basic_machine_tools"}]})
        # 默认 2 条在建线，第 2 条分配 8 厂 (<15) -> 已榨干全部可用民工，0 闲置
        self._construction = kw.get("construction", [{"factories": 15}, {"factories": 8}])
        self._prod_lines = kw.get("prod_lines", [
            {"index": 0, "class": "CMilitaryProductionLine", "equipment": "infantry_equipment_1", "factories": 30},
            {"index": 1, "class": "CNavalProductionLine", "equipment": "destroyer_1", "factories": 10},
        ])
        self._vis_decisions = kw.get("vis_decisions", [{"key": "improved_worker_conditions", "title": "改善工人条件", "mine": True, "cost_pp": 100}])
        self._act_decisions = kw.get("act_decisions", [])
        self._unassigned = kw.get("unassigned", [])
        self._order_groups = kw.get("order_groups", [{"ptr": "0x1000", "divisions": 12, "leader_ptr": "0x2000"}])
        self._avail_generals = kw.get("avail_generals", {
            "generals": [
                {"name": "Guderian", "leader_ptr": "0x2000", "rank": "corps_commander", "attack": 4, "defense": 4},
                {"name": "Rommel", "leader_ptr": "0x3000", "rank": "corps_commander", "attack": 5, "defense": 3},
            ]
        })
        self._stockpile = kw.get("stockpile", {"totals_by_equipment": {"infantry_equipment_1": 3000.0}})
        self._conveyors = kw.get("conveyors", [{"lines": 1}])
        self._divisions = kw.get("divisions", [{"equipment_ratio": 0.98}])
        self._air = kw.get("air", [])
        self._navy = kw.get("navy", [])
        self._variants = kw.get("variants", ["infantry_equipment_1"])
        self._paused = kw.get("paused", False)
        self.pause_calls = 0

    def player_tag(self):
        return self._tag

    def player(self):
        return self._country

    def date(self):
        return {"raw": self._date_raw, "text": self._date_text}

    def date_raw(self):
        return self._date_raw

    def current_focus(self):
        return self._focus

    def selectable_focuses(self, tag=None):
        return self._sel_focuses

    def research_slots(self):
        return self._slots

    def available_research(self, tag=None):
        return self._avail_research

    def construction_queue(self, tag=None):
        return self._construction

    def production_lines(self, tag=None, include_obsolete=False):
        return self._prod_lines

    def decisions(self, tag=None, which="visible", rich=True):
        return self._act_decisions if which == "active" else self._vis_decisions

    def unassigned_divisions(self, tag=None):
        return self._unassigned

    def order_groups(self, tag=None):
        return self._order_groups

    def available_generals(self, tag=None):
        return self._avail_generals

    def equipment_stockpile(self, tag=None):
        return self._stockpile

    def conveyors(self, tag=None):
        return self._conveyors

    def divisions(self, tag=None):
        return self._divisions

    def _air_snapshot(self):
        return self._air

    def _navy_snapshot(self):
        return self._navy

    def _active_variant_names(self, tag=None):
        return list(self._variants)

    def game_speed(self):
        return {"speed": 4, "seconds_per_hour": 0.01}

    def dismiss_popups_and_unpause(self):
        self._paused = False


def _install_stubs():
    """用可控桩隔离外部模块（events_win / navalops_win / combat / session / equipmeta）。"""
    state = {
        "events_pending": [],
        "event_infos": {},
        "event_opts": {},
        "fleets": [],
        "enemies": [],
        "obsolete": [],
        "fuel_zero": None,
    }

    mod_ev = types.ModuleType("events_win")
    mod_ev.pending = lambda g: list(state["events_pending"])
    mod_ev.info = lambda eid, lang="zh": state["event_infos"].get(eid, {"ok": True, "kind": "country_event", "title": eid})
    mod_ev.applicable_options = lambda eid, tag, lang="zh": state["event_opts"].get(eid, [{"index": 1, "name": "A", "applicable": True}, {"index": 2, "name": "B", "applicable": True}])

    mod_nw = types.ModuleType("navalops_win")
    mod_nw.task_forces = lambda g, tag=None: list(state["fleets"])

    mod_cb = types.ModuleType("combat")
    mod_cb.enemy_tags = lambda g, tag=None: list(state["enemies"])
    mod_cb.combats = lambda g, only_mine=True, me=None: []

    mod_sess = types.ModuleType("session")
    mod_sess.is_paused = lambda g: bool(getattr(g, "_paused", False))
    def _pause(g, paused=True, who=None):
        g._paused = bool(paused)
        if paused:
            g.pause_calls += 1
        return True, ""
    mod_sess.pause = _pause

    mod_em = types.ModuleType("equipmeta")
    mod_em.compute_fuel_zero_alert = lambda fuel, wings, fleets: state["fuel_zero"]
    mod_em.analyze_production_lines = lambda lines, avail, game_dir=None: {
        int(x.get("line", i)): dict(x, obsolete=True)
        for i, x in enumerate(state["obsolete"])
    }
    mod_em.check_tech_unlocks_obsolete = lambda *a, **k: []

    saved = {}
    for name, mod in (
        ("events_win", mod_ev),
        ("navalops_win", mod_nw),
        ("combat", mod_cb),
        ("session", mod_sess),
        ("equipmeta", mod_em),
    ):
        saved[name] = sys.modules.get(name)
        sys.modules[name] = mod

    orig_pause_safe = RD._force_pause_safe
    orig_is_paused = RD._is_paused_fast
    def _fake_force_pause(g):
        g._paused = True
        g.pause_calls += 1
        return True
    RD._force_pause_safe = _fake_force_pause
    RD._is_paused_fast = lambda g: bool(getattr(g, "_paused", False))

    def restore():
        for name, old in saved.items():
            if old is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = old
        RD._force_pause_safe = orig_pause_safe
        RD._is_paused_fast = orig_is_paused

    return state, restore


def run_tests():
    state, restore = _install_stubs()
    tmp_dir = tempfile.mkdtemp(prefix="hoi4_reddots_test_")
    orig_state_path = RD.STATE_PATH
    RD.STATE_PATH = os.path.join(tmp_dir, "reddots_state.json")
    RD.reset_state(persist=False)
    RD.configure(**RD.DEFAULT_CONFIG)
    try:
        print("== 1. 干净局零误报（含民工消费品不误报 & 首连常驻决议不误报） ==")
        g = FakeGame()
        ev = RD.evaluate(g, fast=False)
        check("干净局无任何阻塞红点", ev["ok"] and not ev["has_blocking"], str(ev.get("blocking")))
        check("消费品民工未误报 IDLE_CIV", "IDLE_CIV" not in [x["code"] for x in ev["blocking"]])
        check("首次连接常驻决议未误报 NEW_DECISION", "NEW_DECISION" not in [x["code"] for x in ev["blocking"]])

        print("== 2. 事件（可决策 / 1按钮新闻 / unresolved_scope）与决议增量/限时任务 ==")
        state["events_pending"] = [
            {"event_id": "germany.4", "slot": 1},
            {"event_id": "news.1", "slot": 2},
            {"event_id": "country_capitulated.0", "slot": 3},
        ]
        state["event_infos"] = {
            "germany.4": {"ok": True, "kind": "country_event", "title": "德奥合并"},
            "news.1": {"ok": True, "kind": "news_event", "title": "全球新闻"},
            "country_capitulated.0": {"ok": True, "kind": "country_event", "title": "投降播报"},
        }
        state["event_opts"] = {
            "germany.4": [{"index": 1, "name": "吞并", "applicable": True}, {"index": 2, "name": "放弃", "applicable": True}],
            "news.1": [{"index": 1, "name": "知道了", "applicable": True}],
            "country_capitulated.0": [{"index": 1, "name": "收到", "applicable": None}],
        }
        # 新增一个本国决议 + 一个限时倒计时任务
        g._vis_decisions.append({"key": "GER_mefo_extend", "title": "延长MEFO票据", "mine": True, "cost_pp": 50})
        g._act_decisions = [{"key": "GER_mefo_payment", "title": "MEFO兑付倒计时", "mine": True, "days_mission_timeout": 90}]

        ev = RD.evaluate(g, fast=False)
        bcodes = {x["code"] for x in ev["blocking"]}
        acodes = {x["code"] for x in ev["advisory"]}
        check("可决策国家事件触发严控 PENDING_EVENT", "PENDING_EVENT" in bcodes, str(bcodes))
        check("1按钮新闻与 unresolved_scope 降级为 advisory 不阻塞",
              "NEWS_EVENT" in acodes and "EVENT_UNRESOLVED_SCOPE" in acodes, str(acodes))
        check("新增本国决议触发 NEW_DECISION，限时任务触发 ACTIVE_MISSION",
              "NEW_DECISION" in bcodes and "ACTIVE_MISSION" in bcodes, str(bcodes))

        # 严控三项拒绝普通 ack，允许 force=True 最长 7 天
        r_deny = RD.ack(g, "PENDING_EVENT", "等一下再点", snooze_days=30, force=False, persist=False)
        check("PENDING_EVENT 拒绝普通 ack", not r_deny["ok"] and r_deny.get("blocked_by") == "strict_red_dot")
        r_empty = RD.ack(g, "NEW_DECISION", " ", persist=False)
        check("空理由拒绝签收", not r_empty["ok"])
        r_force = RD.ack(g, "PENDING_EVENT", "等待前线落位再决定", snooze_days=30, force=True, persist=False)
        check("PENDING_EVENT 带 force=True 签收成功且上限截断为 7 天",
              r_force["ok"] and r_force["acked"]["snooze_days"] == 7, str(r_force))

        # 弹出新事件 -> 签名变化立刻破冷却复触发！
        state["events_pending"].append({"event_id": "germany.5", "slot": 4})
        state["event_infos"]["germany.5"] = {"ok": True, "kind": "country_event", "title": "苏台德危机"}
        state["event_opts"]["germany.5"] = [{"index": 1, "name": "要求割让", "applicable": True}]
        ev2 = RD.evaluate(g, fast=False)
        check("新事件弹出立即破除旧 PENDING_EVENT 冷却",
              "PENDING_EVENT" in {x["code"] for x in ev2["blocking"]})

        # 清理事件与决议进入下一组测试
        state["events_pending"] = []
        g._act_decisions = []
        RD.ack(g, "NEW_DECISION", "暂不延长MEFO", persist=False)

        print("== 3. 空国策/空科研/工业空闲/PP500/XP100 与阶梯复触发 ==")
        g._focus = None
        g._slots = [{"slot": 0, "slot_busy": False, "researching": None}]
        g._construction = []
        g._prod_lines = [
            {"index": 0, "class": "CMilitaryProductionLine", "equipment": "infantry_equipment_1", "factories": 28},
            {"index": 1, "class": "CNavalProductionLine", "equipment": "destroyer_1", "factories": 8},
        ]
        g._country.political_power = 520.0
        g._country.experience = {"army": 165.0, "navy": 155.0, "air": 160.0}
        ev = RD.evaluate(g, fast=False)
        bcodes = {x["code"] for x in ev["blocking"]}
        for expect in ("NO_FOCUS", "IDLE_RESEARCH", "EMPTY_CONSTRUCTION", "IDLE_MIL", "IDLE_DOCK",
                       "PP_CAP", "ARMY_XP_CAP", "NAVY_XP_CAP", "AIR_XP_CAP"):
            check("触发红点 %s" % expect, expect in bcodes, str(bcodes))

        # 满 15 厂溢出民工测试
        g._construction = [{"factories": 15}]  # 总民工 40，仅 1 项 15 厂，剩余 25 >= 13 -> IDLE_CIV
        ev_civ = RD.evaluate(g, fast=False)
        check("队列全满 15 厂且余量充足时触发 IDLE_CIV",
              "IDLE_CIV" in {x["code"] for x in ev_civ["blocking"]})

        # 签收 PP_CAP (500 档) -> 暂时消解；涨到 810 (800 档) -> 自动复触发！
        RD.ack(g, "PP_CAP", "攒PP切战时经济", snooze_days=30, persist=False)
        ev_ack = RD.evaluate(g, fast=False)
        check("签收 PP_CAP 后进入 acked 不再阻塞",
              "PP_CAP" not in {x["code"] for x in ev_ack["blocking"]}
              and "PP_CAP" in {x["code"] for x in ev_ack["acked"]})
        g._country.political_power = 810.0
        ev_esc = RD.evaluate(g, fast=False)
        check("PP 涨至 810 跨越 800 档自动破冷却复触发",
              "PP_CAP" in {x["code"] for x in ev_esc["blocking"]})

        # 签收到期复触发：ARMY_XP_CAP 115 签收 10 天，推进 10 天后自动重武装
        RD.ack(g, "ARMY_XP_CAP", "等改中坦编制", snooze_days=10, persist=False)
        check("ARMY_XP_CAP 签收期内静默",
              "ARMY_XP_CAP" not in {x["code"] for x in RD.evaluate(g, fast=False)["blocking"]})
        g._date_raw += 10 * 24
        check("ARMY_XP_CAP 到期后自动复触发",
              "ARMY_XP_CAP" in {x["code"] for x in RD.evaluate(g, fast=False)["blocking"]})

        print("== 4. 将领/未编组/扩军失衡/海空战时和平/燃油枯竭/过时产线 ==")
        g2 = FakeGame(pid=99998)
        g2._unassigned = [{"id": 1}, {"id": 2}]
        g2._order_groups = [{"ptr": "0x5000", "divisions": 10, "leader_ptr": 0}]  # 无将领集团军
        g2._conveyors = []
        g2._stockpile = {"totals_by_equipment": {"infantry_equipment_1": 12000.0}}
        g2._air = [
            {"id": 1, "type": "fighter", "planes": 100, "mission": 0},
            {"id": 2, "type": "transport_plane", "planes": 80, "mission": 0},  # 运输机不计
        ]
        g2._navy = [{
            "name": "Kriegsmarine",
            "is_reserve": False,
            "task_forces": [
                {"handle": [61, 1], "is_reserve": False, "effective_mission": 0, "mission": 0, "ships": 12},
                {"handle": [61, 2], "is_reserve": True, "effective_mission": 8, "mission": 8, "ships": 4},
            ],
        }]
        state["enemies"] = []  # 和平期
        state["fuel_zero"] = {"fuel": 0.0, "active_missions": 3}
        state["obsolete"] = [{"line": 0, "equipment": "artillery_equipment_1", "upgrade_to": "artillery_equipment_2", "payback_days": 18.0}]

        ev = RD.evaluate(g2, fast=False)
        bcodes = {x["code"] for x in ev["blocking"]}
        acodes = {x["code"] for x in ev["advisory"]}
        check("无将领集团军+未编组师+盈余未训练+燃油枯竭+高收益过时产线均阻塞",
              {"ARMY_NO_GENERAL", "UNASSIGNED_DIVS", "SURPLUS_NO_TRAINING", "FUEL_ZERO", "OBSOLETE_LINE"} <= bcodes,
              str(bcodes))
        check("和平期海空未指派默认降为 advisory 不硬拦",
              "IDLE_AIR" in acodes and "IDLE_NAVY" in acodes and "IDLE_AIR" not in bcodes,
              "b=%s a=%s" % (bcodes, acodes))

        # 切换战时 -> IDLE_AIR / IDLE_NAVY 立即升格为阻塞
        state["enemies"] = ["POL"]
        ev_war = RD.evaluate(g2, fast=False)
        check("战时 IDLE_AIR 与 IDLE_NAVY 自动升级为硬阻塞",
              {"IDLE_AIR", "IDLE_NAVY"} <= {x["code"] for x in ev_war["blocking"]})

        # 将领池已空 -> ARMY_NO_GENERAL 自动降为 advisory 防死锁
        g2._avail_generals = {"generals": []}
        ev_ng = RD.evaluate(g2, fast=False)
        check("将领池已空时 ARMY_NO_GENERAL 自动降为 advisory 不死锁",
              "ARMY_NO_GENERAL" not in {x["code"] for x in ev_ng["blocking"]}
              and "ARMY_NO_GENERAL_POOL_EMPTY" in {x["code"] for x in ev_ng["advisory"]})

        # 装备不足仍开一堆训练队列 -> DEFICIT_OVER_TRAINING
        g2._conveyors = [{"lines": 4}]
        g2._stockpile = {"totals_by_equipment": {"infantry_equipment_1": 200.0}}
        ev_def = RD.evaluate(g2, fast=False)
        check("装备亏空仍开 4 条训练队列触发 DEFICIT_OVER_TRAINING",
              "DEFICIT_OVER_TRAINING" in {x["code"] for x in ev_def["blocking"]})

        print("== 5. 三路闸门：wait_for 强停 / 开表拦截 / 后台急刹守护 / turnengine 集成 ==")
        state["enemies"] = []
        state["fleets"] = []
        state["fuel_zero"] = None
        state["obsolete"] = []
        g3 = FakeGame(pid=99997, paused=False, country={"pp": 550.0})

        # 闸门 C：后台守护 poll_once 在运行态急刹停表，同一签名二次不再重复停表
        guard = RD.RedDotGuard()
        b1 = guard.poll_once(g3)
        check("后台守护命中 PP_CAP 立即零注入急刹停表",
              b1 is not None and "PP_CAP" in b1["codes"] and g3._paused is True and g3.pause_calls == 1,
              str(b1))
        g3._paused = False  # 模拟人类手动按空格继续跑
        b2 = guard.poll_once(g3)
        check("同一红点签名后台守护绝不重复按暂停抢空格",
              b2 is None and g3.pause_calls == 1)

        # 闸门 B：开表前硬拦截 + 一步 ack_red_dots 放行
        gate1 = RD.check_unpause_gate(g3)
        check("开表硬拦截阻止带 PP_CAP 取消暂停",
              gate1 is not None and gate1["blocked_by"] == "red_dots" and "PP_CAP" in gate1["blocking_codes"])
        gate2 = RD.check_unpause_gate(g3, ack_red_dots=[{"code": "PP_CAP", "reason": "攒PP等战时经济"}])
        check("一步 ack_red_dots 签收后顺利放行开表", gate2 is None)

        # 闸门 A：TurnLoop.wait() 遇到未处理红点直接拦停返回
        import turnloop
        g4 = FakeGame(pid=99996, paused=False, focus=None)
        tl = turnloop.TurnLoop(g4, poll=0.05)
        wres = tl.wait(max_days=5, timeout_seconds=5)
        check("TurnLoop.wait 开跑前被空国策硬拦并强制停表",
              wres.get("blocked_by") == "red_dots" and wres.get("paused") is True
              and "NO_FOCUS" in (wres.get("blocking_codes") or []),
              str(wres))

        # Compact turnengine 集成：ack_red_dot 合法动作 + briefing 置顶 MUST_RESOLVE + resume 被红点拦截
        import turnengine as TE
        ok_p, errs_p = TE.validate_plan({"actions": [{"op": "ack_red_dot", "args": {"code": "PP_CAP", "reason": "攒钱"}}]})
        check("turnengine Plan DSL 合法支持 ack_red_dot 且不需前台窗口",
              ok_p and "ack_red_dot" in TE._NO_FOREGROUND_NEEDED, str(errs_p))
        r_resume = TE._dispatch_action(g4, "resume", {}, tag="GER")
        check("turnengine resume 被空国策红点门禁拦截",
              r_resume.get("blocked_by") == "red_dots", str(r_resume))
        rd_sec = RD.format_briefing_section(RD.evaluate(g4, fast=True))
        check("briefing 渲染包含 MUST_RESOLVE 与 NO_FOCUS",
              any("MUST_RESOLVE" in ln for ln in rd_sec) and any("NO_FOCUS" in ln for ln in rd_sec))

        # MCP server Compact 模式仍严格保持 5 工具不变
        import server as srv
        orig_prof = srv._MCP_PROFILE
        try:
            srv._MCP_PROFILE = "compact"
            check("Compact 模式严格维持 5 工具不受新 Full 工具影响", len(srv._active_tools()) == 5)
            srv._MCP_PROFILE = "full"
            full_names = {t["name"] for t in srv._active_tools()}
            check("Full 模式暴露 hoi4_red_dots / hoi4_ack_red_dot / hoi4_red_dot_guard",
                  {"hoi4_red_dots", "hoi4_ack_red_dot", "hoi4_red_dot_guard"} <= full_names)
        finally:
            srv._MCP_PROFILE = orig_prof

    finally:
        RD.STATE_PATH = orig_state_path
        RD.reset_state(persist=False)
        RD.configure(**RD.DEFAULT_CONFIG)
        restore()
        shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    run_tests()
    print("\nSummary: %d passed, %d failed" % (PASS, FAIL))
    sys.exit(0 if FAIL == 0 else 1)
