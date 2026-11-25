# -*- coding: utf-8 -*-
"""复测 6（2026-11-23）全部 10 项修复离线专项验证脚本（零游戏写入、秒级执行）。

覆盖：
  1. P0-1  diplomacy.war_escalation_forecast + game.war_readiness_check / declare_war
           阵营/保独/宗主连锁推演、非法外交硬拦、空防强敌硬拦、ack_co_belligerents 放行与预警
  2. P0-2  movemicro.deploy_to_border 其他邻国边境风险 other_fronts_at_risk、补给超载警告、
           held_in_reserve_summary 与 protect_borders / 已交战边境保护
  3. P1-1  引擎精确工厂/船坞账本（CProductionStatus + CShipRefitProductionLine + repair_dockyards）
           与 turnloop.factory_status / reddots.IDLE_DOCK 联动消除假红点
  4. P1-2  CCreateUnitLeaderCommand 的 cmdwin.ARGSPEC/ARG_KIND、0/1/2 映射、Command Power 与 CanExecute
  5. P1-3  hoi4_wait_for(signals=[]) 空列表不被 falsy 吞掉 + wake_on_battle 默认关闭
  6. P1-4  calling/api.py _win_building_counts(core_only=True) 基于 CState+0xB0 真实核心数组
  7. P1-5  events_win 一级国家/州作用域求值（germany.55 实库验证）+ 禁止盲推空条件兜底项 + unresolved_scope 保留
  8. P2-1  game.state_detail 全局回退查找与结构化错误（不抛裸 KeyError）
  9. P2-2  activate_army_battleplans 的 supply_trim.armies_held_back_detail
 10. P2-3  _front_directive_check 暂停态不误报 worker may be stalled
 11. 附带  reddots.ARMY_NO_GENERAL 过滤 field_marshal 仅统计 corps_commander
"""
import os
import sys
import struct
import tempfile
import shutil

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CALLING = os.path.join(ROOT, "calling")
MCP_DIR = os.path.join(ROOT, "mcp")
if CALLING not in sys.path:
    sys.path.insert(0, CALLING)

passed = 0
failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print("  [PASS] %s" % name)
    else:
        failed += 1
        print("  [FAIL] %s  (%s)" % (name, detail))


# ====================================================================== 1. P0-1
def test_p0_1_war_escalation_and_gates():
    print("\n=== 1. P0-1 宣战阵营/保独/宗主连锁推演与门禁 ===")
    import diplomacy as dip
    import game as gm

    class FakeCountry:
        def __init__(self, tag, tag_id=1):
            self.tag = tag
            self.tag_id = tag_id
            self.ptr = 0x100000 + tag_id * 0x1000

    class MockWarGame:
        GER_SNOWBALL = gm.Game.GER_SNOWBALL

        def __init__(self, fra_divs_on_border=0, fra_has_front=False):
            self._fra_on = fra_divs_on_border
            self._fra_fl = fra_has_front
            self._mock_factions = [
                {"name": "轴心国", "leader": "GER", "members": ["GER", "SLO", "HUN"]},
                {"name": "同盟国", "leader": "ENG", "members": ["ENG", "FRA", "CAN", "AST"]},
            ]
            self._mock_relations = {
                "GER": {"tag": "GER", "faction": self._mock_factions[0], "guaranteed_by": [],
                        "guaranteeing": [], "overlord": None, "subjects": ["SLO"],
                        "non_aggression_pacts": ["SOV"], "military_access_from": [],
                        "military_access_to": [], "at_war_with": ["POL"]},
                "BEL": {"tag": "BEL", "faction": None, "guaranteed_by": ["ENG", "FRA"],
                        "guaranteeing": [], "overlord": None, "subjects": [],
                        "non_aggression_pacts": [], "military_access_from": [],
                        "military_access_to": [], "at_war_with": []},
                "ENG": {"tag": "ENG", "faction": self._mock_factions[1], "guaranteed_by": [],
                        "guaranteeing": ["BEL"], "overlord": None, "subjects": ["RAJ"],
                        "non_aggression_pacts": [], "military_access_from": [],
                        "military_access_to": [], "at_war_with": []},
                "FRA": {"tag": "FRA", "faction": self._mock_factions[1], "guaranteed_by": [],
                        "guaranteeing": ["BEL"], "overlord": None, "subjects": ["SYR"],
                        "non_aggression_pacts": [], "military_access_from": [],
                        "military_access_to": [], "at_war_with": []},
                "CAN": {"tag": "CAN", "faction": self._mock_factions[1], "guaranteed_by": [],
                        "guaranteeing": [], "overlord": "ENG", "subjects": [],
                        "non_aggression_pacts": [], "military_access_from": [],
                        "military_access_to": [], "at_war_with": []},
                "AST": {"tag": "AST", "faction": self._mock_factions[1], "guaranteed_by": [],
                        "guaranteeing": [], "overlord": "ENG", "subjects": [],
                        "non_aggression_pacts": [], "military_access_from": [],
                        "military_access_to": [], "at_war_with": []},
                "RAJ": {"tag": "RAJ", "faction": None, "guaranteed_by": [],
                        "guaranteeing": [], "overlord": "ENG", "subjects": [],
                        "non_aggression_pacts": [], "military_access_from": [],
                        "military_access_to": [], "at_war_with": []},
                "SYR": {"tag": "SYR", "faction": None, "guaranteed_by": [],
                        "guaranteeing": [], "overlord": "FRA", "subjects": [],
                        "non_aggression_pacts": [], "military_access_from": [],
                        "military_access_to": [], "at_war_with": []},
                "SLO": {"tag": "SLO", "faction": self._mock_factions[0], "guaranteed_by": [],
                        "guaranteeing": [], "overlord": "GER", "subjects": [],
                        "non_aggression_pacts": [], "military_access_from": [],
                        "military_access_to": [], "at_war_with": []},
                "HUN": {"tag": "HUN", "faction": self._mock_factions[0], "guaranteed_by": [],
                        "guaranteeing": [], "overlord": None, "subjects": [],
                        "non_aggression_pacts": [], "military_access_from": [],
                        "military_access_to": [], "at_war_with": []},
                "POL": {"tag": "POL", "faction": None, "guaranteed_by": [],
                        "guaranteeing": [], "overlord": None, "subjects": [],
                        "non_aggression_pacts": [], "military_access_from": [],
                        "military_access_to": [], "at_war_with": ["GER"]},
                "SOV": {"tag": "SOV", "faction": None, "guaranteed_by": [],
                        "guaranteeing": [], "overlord": None, "subjects": [],
                        "non_aggression_pacts": ["GER"], "military_access_from": [],
                        "military_access_to": [], "at_war_with": []},
            }
            self._mock_div_counts = {
                "GER": 147, "BEL": 22, "FRA": 129, "ENG": 60, "CAN": 12,
                "AST": 10, "RAJ": 18, "SYR": 2, "SOV": 150, "POL": 40,
            }
            self._mock_majors = {"GER", "FRA", "ENG", "SOV"}

        def _mock_border_exposure(self, me, tags):
            res = {}
            if "FRA" in tags:
                res["FRA"] = {
                    "tag": "FRA", "border_provinces": 34,
                    "divisions_on_border": self._fra_on,
                    "has_front_line": self._fra_fl,
                }
            return res

        def _c(self, tag=None):
            return FakeCountry((tag or "GER").upper())

        def player_tag(self):
            return "GER"

        def divisions(self, tag=None, limit=1, deep=False):
            t = (tag or "GER").upper()
            return {"count": self._mock_div_counts.get(t, 0), "source": "mock", "verified": True}

        def equipment_stockpile(self, tag=None):
            return {"stockpile": [{"equipment": "infantry_equipment_1", "count": 5000}]}

        def completed_focuses(self, tag=None):
            return ["GER_anschluss", "GER_demand_sudetenland", "GER_danzig_or_war"]

        def date(self):
            return {"year": 1940, "month": 11, "day": 15}

        def _border_divisions(self, tgt, tag=None):
            return {"border_provinces": 4, "divisions_count": 147, "divisions_on_border": 56}

        def _front_plan_check(self, tgt, tag=None):
            return {"ok": True, "enemy_tag": tgt, "armies": [{"assigned_divisions": 24, "offensive_lines": 1}]}

        def _front_directive_check(self, tgt):
            return {"ok": True}

    MockWarGame.war_readiness_check = gm.Game.war_readiness_check
    MockWarGame.declare_war = gm.Game.declare_war

    # 1a. FRA 0 驻军 -> 必须硬拦！
    g0 = MockWarGame(fra_divs_on_border=0, fra_has_front=False)
    orig_find = dip.find_wargoal
    orig_just = dip.justifying
    try:
        dip.find_wargoal = lambda g, t, deep=False: {"type": "take_state_focus", "target": t, "ptr": "0x5555"}
        dip.justifying = lambda g: []

        fc = dip.war_escalation_forecast(g0, "BEL", me_tag="GER")
        check("forecast 拉出 ENG/FRA/CAN/AST/RAJ/SYR",
              {"ENG", "FRA", "CAN", "AST", "RAJ", "SYR"}.issubset(set(fc["co_belligerents"])),
              fc["co_belligerents"])
        check("forecast 计算总敌军师数 >= 250",
              fc["total_enemy_divisions_est"] >= 250, fc["total_enemy_divisions_est"])
        check("FRA 0 驻军进入 undefended_new_co_belligerents",
              any(x["tag"] == "FRA" for x in fc["undefended_new_co_belligerents"]), fc)

        wr0 = g0.war_readiness_check("BEL")
        check("FRA 边境 0 驻军时 war_readiness_check 硬拦 (ready=False)",
              not wr0["ready"] and any("FRA" in b and "0 divisions" in b for b in wr0["blockers"]),
              wr0["blockers"])

        # 显式确认 ack_co_belligerents=True 后放行，且带 co_belligerents / guaranteed_by
        wr_ack = g0.war_readiness_check("BEL", ack_co_belligerents=True)
        check("ack_co_belligerents=True 放行空防门禁", wr_ack["ready"], wr_ack["blockers"])
        dw_dry = g0.declare_war("BEL", dry_run=True, ack_co_belligerents=True)
        check("declare_war(dry_run=True) 返回 co_belligerents 含 FRA/ENG",
              dw_dry["ok"] and "FRA" in dw_dry["co_belligerents"] and "ENG" in dw_dry["co_belligerents"],
              dw_dry)

        # 1b. FRA 34 省 12 师且无前线 -> 放行但必须强烈警告 FRA！
        g12 = MockWarGame(fra_divs_on_border=12, fra_has_front=False)
        wr12 = g12.war_readiness_check("BEL")
        check("FRA 34 省 12 师时 ready=True 且 warnings 明确预警 FRA",
              wr12["ready"] and any("FRA" in w and "34 border provinces" in w and "12 divisions" in w
                                    for w in wr12["warnings"]),
              wr12["warnings"])

        # 1c. 外交非法目标（同阵营 HUN / 附庸 SLO / 已交战 POL / 自己 GER）永远硬拦
        for bad_tag, kw in (("HUN", "own faction"), ("SLO", "subject"), ("POL", "already at war"), ("GER", "yourself")):
            wrb = g12.war_readiness_check(bad_tag, ack_co_belligerents=True)
            check("非法宣战目标 %s 被硬拦" % bad_tag,
                  not wrb["ready"] and any(kw in b.lower() for b in wrb["blockers"]),
                  wrb["blockers"])

        # 1d. 互不侵犯条约给 warning
        wrs = g12.war_readiness_check("SOV")
        check("对有互不侵犯条约的 SOV 给 non-aggression warning",
              any("non-aggression" in w.lower() for w in wrs["warnings"]), wrs["warnings"])
    finally:
        dip.find_wargoal = orig_find
        dip.justifying = orig_just


# ====================================================================== 2. P0-2
def test_p0_2_deploy_to_border_risk():
    print("\n=== 2. P0-2 deploy_to_border 边境抽空预警与保护 ===")
    import movemicro as mm
    from micro import pathing as mm_path, front_state as mm_fs, directives as mm_dir
    import mapdata as md
    import unitorders as uo
    import diplomacy as dip
    import divreserve as dr
    import staff_supply as ss

    # 造一张迷你地图：
    #   BEL 省: 101, 102
    #   GER-BEL 边境: 1, 2
    #   GER-FRA 边境: 3, 4, 5
    #   FRA 省: 201, 202, 203
    #   内陆: 10
    owners = {1: "GER", 2: "GER", 3: "GER", 4: "GER", 5: "GER", 10: "GER",
              101: "BEL", 102: "BEL", 201: "FRA", 202: "FRA", 203: "FRA"}
    adj = {
        1: [2, 10, 101], 2: [1, 10, 102],
        3: [4, 10, 201], 4: [3, 5, 10, 202], 5: [4, 10, 203],
        10: [1, 2, 3, 4, 5],
        101: [1], 102: [2], 201: [3], 202: [4], 203: [5],
    }
    ptype = {k: "land" for k in owners}

    # 12 个师：3 个站在 FRA 边境 (3,4,5)，9 个站在内陆 (10)
    div_pos = {0x100 + i: (3 + i if i < 3 else 10) for i in range(12)}

    class FakeDeployG:
        _inj = 1

        def player_tag(self): return "GER"
        def order_groups(self, tag=None): return {"groups": []}
        def housekeep_divisions(self, tag=None): return {"ok": True}

    origs = (
        md.province_owner_map, md.build_adjacency, md.load_definitions,
        uo.province_of, dr.filter_out, ss.front_budget,
        mm_path._resolve, mm_path._border_provinces, mm_path._allowed_tags,
        mm_dir._load_front_state, mm_fs._front_state, mm_path._alloc_errbuf, mm_path._run_move,
        dip.country_relations, dip.war_escalation_forecast,
    )
    try:
        md.province_owner_map = lambda g: (owners, {})
        md.build_adjacency = lambda: adj
        md.load_definitions = lambda: ({}, ptype)
        uo.province_of = lambda g, d: div_pos[d]
        dr.filter_out = lambda g, ds: (ds, [])
        # BEL 两省总补给仅 4.0（2+2），12 个师明显超载 > 2*4
        ss.front_budget = lambda g, tgt, tag=None: {"per_province": {1: 2, 2: 2}}
        mm_path._resolve = lambda g, d, og, t: list(div_pos.keys())
        mm_path._border_provinces = lambda g, t: [1, 2] if t == "BEL" else [3, 4, 5]
        mm_path._allowed_tags = lambda g: {"GER"}
        mm_dir._load_front_state = lambda: {}
        mm_fs._front_state = lambda g, t: {}
        mm_path._alloc_errbuf = lambda g: 0
        mm_path._run_move = lambda g, d, dst, flags, eb: (True, 0x999, [dst], None, None)
        dip.country_relations = lambda g, t: {"tag": "GER", "faction": None, "subjects": [], "overlord": None, "at_war_with": []}
        dip.war_escalation_forecast = lambda g, tgt, me_tag=None: {"co_belligerents": ["FRA", "ENG"]}

        # 未保护 FRA 时：返回 other_fronts_at_risk 含 FRA（标注 co_belligerent），且报补给超载与留驻汇总
        r1 = mm.deploy_to_border(FakeDeployG(), "BEL", include_unassigned=False)
        fra_row = next((x for x in r1["other_fronts_at_risk"] if x["tag"] == "FRA"), None)
        check("other_fronts_at_risk 列出 FRA 且标注 is_co_belligerent=True",
              fra_row is not None and fra_row["border_provinces"] == 3 and fra_row["is_co_belligerent"],
              r1["other_fronts_at_risk"])
        check("divisions_ordered > 2*supply_capacity_total 时触发 supply overload 警告",
              r1["supply_capacity_total"] == 4.0 and any("supply overload" in w for w in r1["warnings"]),
              r1["warnings"])
        check("held_in_reserve_summary 交代留驻师所在省与面向邻国",
              r1["held_in_reserve"] == 8 and r1["held_in_reserve_summary"]["total_not_moved"] == 8
              and bool(r1["held_in_reserve_summary"]["top_provinces"]),
              r1["held_in_reserve_summary"])

        # 指定 protect_borders=["FRA"] 时：FRA 边境 3 省驻军一个都不能抽走！
        r2 = mm.deploy_to_border(FakeDeployG(), "BEL", include_unassigned=False, protect_borders=["FRA"])
        fra_row2 = next((x for x in r2["other_fronts_at_risk"] if x["tag"] == "FRA"), None)
        check("protect_borders=['FRA'] 完整保留 FRA 边境 3 师不抽空",
              fra_row2 is not None and fra_row2["divisions_pulled_away"] == 0 and fra_row2["divisions_on_border"] == 3,
              fra_row2)
    finally:
        (md.province_owner_map, md.build_adjacency, md.load_definitions,
         uo.province_of, dr.filter_out, ss.front_budget,
         mm_path._resolve, mm_path._border_provinces, mm_path._allowed_tags,
         mm_dir._load_front_state, mm_fs._front_state, mm_path._alloc_errbuf, mm_path._run_move,
         dip.country_relations, dip.war_escalation_forecast) = origs


# ====================================================================== 3. P1-1 & 11. reddots
def test_p1_1_factory_allocation_and_reddots():
    print("\n=== 3. P1-1 引擎工厂船坞账本与消除假 IDLE_DOCK & ARMY_NO_GENERAL 过滤元帅 ===")
    import turnloop as tl
    import reddots as rd

    tmp_dir = tempfile.mkdtemp(prefix="hoi4_retest6_")
    orig_state_path = rd.STATE_PATH
    try:
        rd.STATE_PATH = os.path.join(tmp_dir, "rd_state.json")
        rd.reset_state(None)

        class FakeC:
            tag = "GER"
            civilian_factories = 40
            military_factories = 30
            naval_dockyards = 20   # 原始建筑 20，但占领折算后引擎有效仅 14！
            political_power = 100
            experience = {"army": 20.0, "navy": 10.0, "air": 5.0}
            fuel = 50000.0

        class FakeG:
            p = type("P", (), {"pid": 99991})()
            def player(self): return FakeC()
            def player_tag(self): return "GER"
            def date(self): return {"text": "1940.11.01.12", "raw": 17000000}
            def current_focus(self): return {"key": "GER_around_maginot"}
            def research_slots(self): return [{"slot": 0, "slot_busy": True, "researching": "t1"}]
            def decisions(self, tag=None, which="visible", rich=True): return []
            def construction_queue(self, tag=None): return [{"factories": 12}]
            def production_lines(self, tag=None, include_obsolete=True):
                return [
                    {"index": 0, "class": "CMilitaryProductionLine", "factories": 30, "equipment": "infantry_equipment_1"},
                    {"index": 1, "class": "CNavalProductionLine", "factories": 13, "equipment": "ship_hull_light_2"},
                    {"index": 2, "class": "CShipRefitProductionLine", "factories": 1, "equipment": "ship_hull_cruiser_1"},
                ]
            def factory_allocation(self, tag=None):
                return {
                    "source": "engine_production_status",
                    "civilian_factories": 40,
                    "military_available": 30, "military_used": 30, "military_idle": 0,
                    "naval_raw_buildings": 20, "naval_total_effective": 14,
                    "naval_available": 14, "naval_used": 14,
                    "naval_build_used": 13, "naval_refit_used": 1,
                    "naval_repair_dockyards": 0, "naval_idle": 0,
                }
            def unassigned_divisions(self, tag=None): return []
            def order_groups(self, tag=None):
                return [{"ptr": "0x1", "divisions": 10, "leader_ptr": 0, "is_army_group": False}]
            def available_generals(self, tag=None):
                # 只有 field_marshal 可用，没有 corps_commander
                return {"generals": [
                    {"name": "Kesselring", "leader_ptr": "0x200", "rank": "field_marshal", "attack": 3, "defense": 4},
                ]}
            def equipment_stockpile(self, tag=None): return {"stockpile": []}
            def deployment_conveyors(self, tag=None): return []

        g = FakeG()
        fs = tl.factory_status(g)
        check("turnloop.factory_status 使用引擎精确口径 naval_idle==0 & refit==1",
              fs["naval_available"] == 14 and fs["naval_idle"] == 0 and fs["naval_refit_dockyards"] == 1,
              fs)

        # 屏蔽 events_win.pending（免连真实游戏）
        import events_win as ew
        orig_pend = ew.pending
        try:
            ew.pending = lambda _g: []
            ev = rd.evaluate(g, fast=True)
        finally:
            ew.pending = orig_pend
        codes = {x["code"] for x in ev.get("blocking") or []}
        check("reddots 不再对占领折算/改装船坞误报 IDLE_DOCK", "IDLE_DOCK" not in codes, codes)
        check("reddots.ARMY_NO_GENERAL 仅剩 field_marshal 时不误报给普通集团军",
              "ARMY_NO_GENERAL" not in codes, codes)
    finally:
        rd.STATE_PATH = orig_state_path
        rd.reset_state(None)
        shutil.rmtree(tmp_dir, ignore_errors=True)


# ====================================================================== 4. P1-2
def test_p1_2_recruit_unit_leader():
    print("\n=== 4. P1-2 recruit_unit_leader 参数映射、Command Power 与 CanExecute ===")
    import cmdwin as cw
    import game as gm

    check("cmdwin.ARGSPEC 已注册 CCreateUnitLeaderCommand(tag=1, leader_type=2)",
          cw.ARGSPEC.get("CCreateUnitLeaderCommand") == {"tag": 1, "leader_type": 2},
          cw.ARGSPEC.get("CCreateUnitLeaderCommand"))
    check("cmdwin.ARG_KIND 已注册 CCreateUnitLeaderCommand(1=tagptr, 2=int)",
          cw.ARG_KIND.get("CCreateUnitLeaderCommand") == {1: cw.ARG_TAGPTR, 2: cw.ARG_INT},
          cw.ARG_KIND.get("CCreateUnitLeaderCommand"))

    class FakeC:
        tag = "GER"
        tag_id = 1
        political_power = 1500.0
        def __init__(self, cp): self.command_power = cp

    class FakeRecG:
        CH_ARMY_LEADER = 0xC0
        def __init__(self, cp_before=40.0, cp_after=20.0, can_exec_ret=1, add_char=True):
            self._cobj = FakeC(cp_before)
            self._cp_after = cp_after
            self._can_exec_ret = can_exec_ret
            self._add_char = add_char
            self._chars = [0xAAA]
            self.built = None
            self.p = type("P", (), {"base": 0x7FF600000000, "u64": lambda s, a: 0, "i32": lambda s, a: 1})()
        def _c(self, tag=None): return self._cobj
        def all_characters(self, tag=None): return list(self._chars)
        def call(self, addr, args, absolute=True, force=True): return self._can_exec_ret
        def _settle(self, s=0.2):
            if self._add_char:
                self._chars.append(0xBBB)
            self._cobj.command_power = self._cp_after
        def _extract_char_key_and_name(self, ptr): return ("GER_new_gen", "新将领")

    FakeRecG.recruit_unit_leader = gm.Game.recruit_unit_leader

    orig_spec = cw.spec_of
    orig_build = cw.build_command
    orig_post = cw.post_drained
    try:
        cw.spec_of = lambda cls: {"class": "CCreateUnitLeaderCommand", "can_execute": 0x11EE9D0}
        last_fields = {}
        def fake_build(g, cls, fields):
            last_fields.clear()
            last_fields.update(fields)
            return 0xCAFE00
        cw.build_command = fake_build
        cw.post_drained = lambda g, cmd, seconds=1.5: {"posted": 1, "drained": True}

        # 4a. type=0 ('general') 映射到引擎 engine_type=1 (corps_commander) 且成功扣 CP
        g_ok = FakeRecG(cp_before=50.0, cp_after=30.0, can_exec_ret=1, add_char=True)
        r_ok = g_ok.recruit_unit_leader(0)
        check("type=0 (general) 映射到 engine_type=1 且 tag=1",
              last_fields == {"tag": 1, "leader_type": 1} and r_ok["ok"] and r_ok["verified"]
              and r_ok["role"] == "corps_commander" and r_ok["command_power_spent"] == 20.0,
              (last_fields, r_ok))

        # 4b. type='field_marshal' 映射到 engine_type=0
        g_fm = FakeRecG(cp_before=50.0, cp_after=30.0, can_exec_ret=1, add_char=True)
        r_fm = g_fm.recruit_unit_leader("field_marshal")
        check("type='field_marshal' 映射到 engine_type=0",
              last_fields == {"tag": 1, "leader_type": 0} and r_fm["role"] == "field_marshal",
              (last_fields, r_fm))

        # 4c. CanExecute 拒绝 -> ok=False, blocked_by="can_execute_refused"
        g_no = FakeRecG(cp_before=5.0, cp_after=5.0, can_exec_ret=0, add_char=False)
        r_no = g_no.recruit_unit_leader(0)
        check("CanExecute 拒绝时返回 ok=False 且 blocked_by=can_execute_refused",
              not r_no["ok"] and r_no.get("blocked_by") == "can_execute_refused", r_no)

        # 4d. 投递后无任何变化 -> 绝不再发假 ok=True
        g_noop = FakeRecG(cp_before=50.0, cp_after=50.0, can_exec_ret=1, add_char=False)
        r_noop = g_noop.recruit_unit_leader(0)
        check("角色与 CP 均未变时严格返回 ok=False (不再假成功)",
              not r_noop["ok"] and not r_noop["verified"] and r_noop.get("blocked_by") == "no_effect_observed",
              r_noop)
    finally:
        cw.spec_of = orig_spec
        cw.build_command = orig_build
        cw.post_drained = orig_post


# ====================================================================== 5. P1-3
def test_p1_3_wait_for_signals_and_battle():
    print("\n=== 5. P1-3 wait_for(signals=[]) 与 wake_on_battle 默认关闭 ===")
    import turnloop as tl

    import inspect
    if MCP_DIR not in sys.path:
        sys.path.insert(0, MCP_DIR)
    import server as _mcp_srv
    srv_src = inspect.getsource(_mcp_srv._wait_for)
    check("mcp/server.py 不再使用 falsy `a.get('signals') or turnloop.ALL_SIGNALS`",
          'a.get("signals") or turnloop.ALL_SIGNALS' not in srv_src
          and "turnloop.ALL_SIGNALS if _raw_sigs is None else tuple(_raw_sigs)" in srv_src)

    prev = {"tag": "GER", "enemies": frozenset({"BEL"}), "combat_provinces": frozenset({100})}
    cur_battle = {"tag": "GER", "enemies": frozenset({"BEL"}), "combat_provinces": frozenset({100, 200, 300})}
    cur_new_war = {"tag": "GER", "enemies": frozenset({"BEL", "FRA"}), "combat_provinces": frozenset({100})}

    check("默认 wake_on_battle=False 时新战斗不打断 wait_for",
          tl.wake_reasons(prev, cur_battle) == [], tl.wake_reasons(prev, cur_battle))
    check("默认 wake_on_battle=False 时新宣战仍立即唤醒",
          any("WAR: FRA" in r for r in tl.wake_reasons(prev, cur_new_war)),
          tl.wake_reasons(prev, cur_new_war))
    check("显式 wake_on_battle=True 时新战斗正常唤醒",
          any("new battle:" in r for r in tl.wake_reasons(prev, cur_battle, wake_on_battle=True)),
          tl.wake_reasons(prev, cur_battle, wake_on_battle=True))

    # 验证 TurnLoop.wait(signals=()) 不会因为战斗或信号中断，直走到 max_days
    class FakeClockG:
        def __init__(self): self.h = 1000
        def date(self):
            self.h += 24 * 5
            return {"raw": self.h, "text": "1940.12.%02d.00" % (1 + (self.h - 1000) // 24)}
        def player_tag(self): return "GER"
        def game_speed(self): return {"speed": 4, "seconds_per_hour": 0.01}

    import session as sess
    orig_paused = sess.is_paused
    try:
        sess.is_paused = lambda g: False
        loop = tl.TurnLoop(FakeClockG(), poll=0.01)
        res = loop.wait(max_days=10, signals=(), poll=0.01, timeout_seconds=5, respect_red_dots=False)
        check("TurnLoop.wait(signals=()) 走满 max_days=10 再唤醒",
              res["waited_game_days"] >= 10.0 and any("time is up" in r for r in res["woke_because"]),
              res)
    finally:
        sess.is_paused = orig_paused


# ====================================================================== 6. P1-4
def test_p1_4_core_factories():
    print("\n=== 6. P1-4 core_factories 基于 CState+0xB0 真实核心数组 ===")
    import api

    # 用虚拟内存字典模拟 3 个州：
    #   s1 (GER 核心 [1, 195], 故意把 +0x2F0=0): 30 civ, 20 mil, 5 dock
    #   s2 (非核心仅 FRA [5], 故意把 +0x2F0=1): 1 civ, 1 mil, 2 dock
    #   s3 (无核心 [], +0x2F0=0):               3 civ, 2 mil, 0 dock
    mem = {}
    def put(addr, data):
        for i, b in enumerate(data):
            mem[addr + i] = b

    class FakeProc:
        def read(self, addr, n):
            return bytes(mem.get(addr + i, 0) for i in range(n))
        def u64(self, addr):
            return struct.unpack("<Q", self.read(addr, 8))[0]
        def i32(self, addr):
            return struct.unpack("<i", self.read(addr, 4))[0]

    def make_bld(addr, tid, lvl):
        put(addr + 0x08, struct.pack("<i", tid))
        put(addr + 0x40, struct.pack("<h", lvl))

    def make_state(addr, cores, blds, fake_2f0):
        put(addr + 0x2F0, struct.pack("<i", fake_2f0))
        if cores:
            c_arr = addr + 0x1000
            put(c_arr, struct.pack("<%di" % len(cores), *cores))
            put(addr + 0xB0, struct.pack("<Qii", c_arr, len(cores), len(cores)))
        b_arr = addr + 0x2000
        b_ptrs = []
        for idx, (tid, lvl) in enumerate(blds):
            bp = addr + 0x3000 + idx * 0x80
            make_bld(bp, tid, lvl)
            b_ptrs.append(bp)
        put(b_arr, struct.pack("<%dQ" % len(b_ptrs), *b_ptrs))
        put(addr + 0x158, struct.pack("<Qii", b_arr, len(b_ptrs), len(b_ptrs)))

    s1, s2, s3 = 0x100000, 0x110000, 0x120000
    make_state(s1, [1, 195], [(20013, 30), (20002, 20), (19446, 5)], fake_2f0=0)
    make_state(s2, [5], [(20013, 1), (20002, 1), (19446, 2)], fake_2f0=1)
    make_state(s3, [], [(20013, 3), (20002, 2)], fake_2f0=0)

    co_ptr = 0x200000
    st_arr = 0x210000
    put(st_arr, struct.pack("<3Q", s1, s2, s3))
    put(co_ptr + 0x460, struct.pack("<Qii", st_arr, 3, 3))

    fg = type("FG", (), {"p": FakeProc()})()
    c = api.Country(fg, "GER", 1, co_ptr)
    raw = c._win_building_counts(core_only=False)
    core = c._win_building_counts(core_only=True)
    check("raw 工厂数 = (34, 23, 7)", raw == (34, 23, 7), raw)
    check("core 工厂数 = (30, 20, 5)（不受失效的 +0x2F0=1 干扰，不再塌成 1/1）",
          core == (30, 20, 5), core)


# ====================================================================== 7. P1-5
def test_p1_5_event_trigger_evaluator():
    print("\n=== 7. P1-5 事件选项跨国/州作用域求值与禁止盲推空条件兜底 ===")
    import events_win as ew

    # 7a. 离线静态模式 (g=None)：germany.55 选项 1~3/5 为 None，选项 4 空条件 True -> 绝不能推荐 4！
    opts_static = ew.applicable_options("germany.55", "GER", lang="zh", g=None)
    check("germany.55 静态模式选项 1 为 None、选项 4 为 True",
          len(opts_static) >= 4 and opts_static[0]["applicable"] is None and opts_static[3]["applicable"] is True,
          opts_static)

    orig_pend = ew.pending
    orig_make = ew.make_select_option
    orig_can = ew.can_execute
    try:
        ew.pending = lambda _g: [{"slot": 1, "event_id": "germany.55", "event_ptr": "0x1000"}]
        ew.make_select_option = lambda _g, s, p, i, n: 0x2000
        ew.can_execute = lambda _g, c: True

        r_static = ew.select_option(None, tag="GER", write=False)
        check("静态模式下 select_option(germany.55) 拒绝盲推选项 4 (recommended=None, recommendation_blocked=True)",
              r_static["recommended"] is None and r_static["recommendation_blocked"] is True,
              r_static)
        r_static_w = ew.select_option(None, tag="GER", write=True)
        check("静态模式下 write=True 且未指定 index 时被 recommendation_blocked 拦截",
              not r_static_w["ok"] and r_static_w.get("blocked_by") == "recommendation_blocked",
              r_static_w)

        # 7b. 模拟 1940.07.01 活游戏状态：GER 和平，CZE 和平且控制 9/75/70/71，664 不归 HUN
        class FakeEvG:
            _mock_state_owners = {9: "CZE", 75: "CZE", 70: "CZE", 71: "CZE", 73: "CZE", 664: "CZE"}
            _mock_relations = {
                "GER": {"at_war_with": [], "overlord": None, "faction": None},
                "CZE": {"at_war_with": [], "overlord": None, "faction": None},
                "SLO": {"at_war_with": [], "overlord": None, "faction": None},
                "RUT": {"at_war_with": [], "overlord": None, "faction": None},
                "HUN": {"at_war_with": [], "overlord": None, "faction": None},
            }

        g_live = FakeEvG()
        opts_live = ew.applicable_options("germany.55", "GER", lang="zh", g=g_live)
        check("活游戏模式下 germany.55 选项 1 求值为 True",
              opts_live[0]["applicable"] is True, opts_live[0])
        check("活游戏模式下 germany.55 选项 2 求值为 True",
              opts_live[1]["applicable"] is True, opts_live[1])
        check("活游戏模式下 germany.55 选项 3 求值为 False (664 不归 HUN)",
              opts_live[2]["applicable"] is False, opts_live[2])

        r_live = ew.select_option(g_live, tag="GER", write=False)
        check("活游戏模式下 select_option(germany.55) 推荐选项 1（绝不是 4）",
              r_live["recommended"] == 1 and not r_live["recommendation_blocked"],
              r_live)

        # 7c. 全选项 None 的 unresolved_scope 安全闸保持有效
        ew.pending = lambda _g: [{"slot": 2, "event_id": "SEA_japan_news_events.02", "event_ptr": "0x1000"}]
        orig_app = ew.applicable_options
        try:
            ew.applicable_options = lambda eid, tag, lang="zh", g=None: [
                {"index": 1, "name": "x", "applicable": None, "trigger": "has_global_flag = foo"}
            ]
            r_unres = ew.select_option(g_live, tag="GER", write=True)
            check("全选项 None 时 unresolved_scope 安全闸仍严格拒写",
                  not r_unres["ok"] and r_unres.get("blocked_by") == "unresolved_scope",
                  r_unres)
        finally:
            ew.applicable_options = orig_app
    finally:
        ew.pending = orig_pend
        ew.make_select_option = orig_make
        ew.can_execute = orig_can


# ====================================================================== 8. P2-1
def test_p2_1_state_detail_fallback():
    print("\n=== 8. P2-1 state_detail 全局回退与结构化错误 ===")
    import game as gm
    import offsets as O

    mem = {}
    def put(addr, data):
        for i, b in enumerate(data):
            mem[addr + i] = b

    class FakeProc:
        def read(self, addr, n): return bytes(mem.get(addr + i, 0) for i in range(n))
        def u64(self, addr): return struct.unpack("<Q", self.read(addr, 8))[0]
        def i32(self, addr): return struct.unpack("<i", self.read(addr, 4))[0]
        def i64(self, addr): return struct.unpack("<q", self.read(addr, 8))[0]

    # state 9 归属 RKH，不在 GER 列表里
    st9 = 0x300000
    put(st9 + gm.ST_ID, struct.pack("<i", 9))
    put(st9 + 0xB0, struct.pack("<Qii", 0, 0, 0))
    ger_c = type("C", (), {"tag": "GER", "ptr": 0x400000, "modifiers": lambda s: {}})()
    rkh_c = type("C", (), {"tag": "RKH", "ptr": 0x410000, "modifiers": lambda s: {}})()
    put(rkh_c.ptr + gm.CO_STATES, struct.pack("<Qii", 0x420000, 1, 1))
    put(0x420000, struct.pack("<Q", st9))

    class FakeSDG:
        p = FakeProc()
        _bld_by_id = {}
        def _load_buildings(self): pass
        def _c(self, tag=None): return ger_c if str(tag).upper() == "GER" else rkh_c
        def countries(self): return [ger_c, rkh_c]

    FakeSDG.state_detail = gm.MacroMixin2.state_detail
    g = FakeSDG()

    r9 = g.state_detail(9, tag="GER")
    check("state_detail(9, tag='GER') 自动回退全局找到 RKH 的 state 9",
          r9.get("ok") is True and r9.get("owner") == "RKH" and r9.get("requested_tag") == "GER"
          and "global fallback" in str(r9.get("owner_note") or ""),
          r9)

    r_miss = g.state_detail(99999, tag="GER")
    check("state_detail(99999) 返回结构化 JSON 错误而不抛裸 KeyError",
          isinstance(r_miss, dict) and r_miss.get("ok") is False and "state not found" in r_miss.get("error", ""),
          r_miss)


# ====================================================================== 9 & 10. P2-2 & P2-3
def test_p2_2_and_p2_3():
    print("\n=== 9 & 10. P2-2 supply_trim 明细与 P2-3 暂停态不误报 stalled ===")
    import game as gm
    import movemicro as mm
    import session as sess

    import inspect
    g_src = (
        inspect.getsource(gm.Game.plan_front)
        + inspect.getsource(gm.Game.activate_army_battleplans)
    )
    check("activate_army_battleplans 输出 armies_held_back_detail 含 divisions/provinces_sample/bordering",
          '"armies_held_back_detail": held_detail' in g_src
          and '"bordering": sorted(bordering)' in g_src)

    class FakeFDG:
        p = type("P", (), {"pid": 37800})()
    FakeFDG._front_directive_check = gm.Game._front_directive_check

    orig_fd = mm.front_directives
    orig_paused = sess.is_paused
    try:
        mm.front_directives = lambda g: {"directives": [{
            "enemy_tag": "BEL", "stance": "blitzkrieg",
            "last_tick": {"pid": 37800, "divisions_seen": 50},
            "hours_since_tick": 188,
        }]}
        sess.is_paused = lambda g: True
        r_paused = FakeFDG()._front_directive_check("BEL")
        check("暂停态下 hours_since_tick=188 不报 worker may be stalled 警告",
              r_paused["ok"] and "warning" not in r_paused and r_paused.get("game_paused") is True
              and "paused_note" in r_paused,
              r_paused)

        sess.is_paused = lambda g: False
        r_running = FakeFDG()._front_directive_check("BEL")
        check("运行态下 hours_since_tick=188 正常报 worker may be stalled 警告",
              r_running["ok"] and "stalled" in str(r_running.get("warning") or ""),
              r_running)
    finally:
        mm.front_directives = orig_fd
        sess.is_paused = orig_paused


def main():
    test_p0_1_war_escalation_and_gates()
    test_p0_2_deploy_to_border_risk()
    test_p1_1_factory_allocation_and_reddots()
    test_p1_2_recruit_unit_leader()
    test_p1_3_wait_for_signals_and_battle()
    test_p1_4_core_factories()
    test_p1_5_event_trigger_evaluator()
    test_p2_1_state_detail_fallback()
    test_p2_2_and_p2_3()
    print("\n=== SUMMARY: %d passed, %d failed ===" % (passed, failed))
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
