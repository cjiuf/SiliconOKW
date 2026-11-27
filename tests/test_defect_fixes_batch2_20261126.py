# -*- coding: utf-8 -*-
"""2026-11-26 第二批实测缺陷修复专项单测（纯离线 0 注入）。

覆盖范围：
  - P0-1: justify_wargoal 拒绝未解析理由名（如 'annex'）并禁止未验证返回假 ok=True
  - P0-2: tech_cost_days 使用 BASE_TECH_COST=110，未进 TS_RESEARCHED 绝不报 100%/0.0天
  - P1-3: CRemoveConveyorLineCommand 白名单 + cancel_training + DEFICIT_OVER_TRAINING 进展重置
  - P1-4: turnengine 开放 unlock_doctrine 动作与红点引导
  - P1-5: autopilot ack_autopilot 静默 weak_infantry_template 与可配置提示
  - P2-6: battleplan 放行仅零散 1 师贴他国边境的大集团军、仍保护真正边防军
  - P2-7: battleplan steps.supply 口径与实际 committed 对齐 + 严重超载门禁
  - P2-8: state_detail 非核心占领州不再谎报 "this is a CORE state"
  - P2-9: 船坞线透出 max_dockyards / line_cap 与重巡提示、简报展示 5/5厂
  - P3-10 & P3-11: peace_finish 按 CO_STATES_B (owned) 算净增州 + unrequested + 资源刷新提示
  - P3-12: select_event 支持 option / option_index 参数别名
"""
from __future__ import annotations

import os
import struct
import sys
import types
import unittest
from unittest import mock

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CALLING = os.path.join(REPO, "calling")
if CALLING not in sys.path:
    sys.path.insert(0, CALLING)


class _MemProc:
    """轻量可读写虚拟内存桩（100% 离线）。"""

    def __init__(self, base=0x140000000, pid=99002):
        self.base = base
        self.pid = pid
        self.mem = {}

    def put(self, addr, data: bytes):
        for i, b in enumerate(data):
            self.mem[addr + i] = b

    def put_u64(self, addr, v):
        self.put(addr, struct.pack("<Q", int(v) & 0xFFFFFFFFFFFFFFFF))

    def put_i64(self, addr, v):
        self.put(addr, struct.pack("<q", int(v)))

    def put_i32(self, addr, v):
        self.put(addr, struct.pack("<i", int(v)))

    def put_u8(self, addr, v):
        self.put(addr, bytes([int(v) & 0xFF]))

    def read(self, addr, size):
        return bytes(self.mem.get(addr + i, 0) for i in range(size))

    def write(self, addr, data: bytes):
        self.put(addr, data)

    def u64(self, addr):
        return struct.unpack("<Q", self.read(addr, 8))[0]

    def i64(self, addr):
        return struct.unpack("<q", self.read(addr, 8))[0]

    def i32(self, addr):
        return struct.unpack("<i", self.read(addr, 4))[0]

    def u32(self, addr):
        return struct.unpack("<I", self.read(addr, 4))[0]

    def u8(self, addr):
        return self.mem.get(addr, 0)


class TestBatch2DefectFixes(unittest.TestCase):
    # ------------------------------------------------------------------ P0-1
    def test_p0_1_justify_wargoal_rejects_unknown_name_and_unverified_post(self):
        from domains.diplomacy_war import DiplomacyWarMixin

        class _FakeG(DiplomacyWarMixin):
            def __init__(self):
                self.p = _MemProc()
                self._win_wgt_cache_key = (self.p.pid, self.p.base)
                self._win_wgt_cache = {
                    "take_state": 0x17430A7E280,
                    "take_claimed_state": 0x17430A7EC40,
                    "topple_government": 0x17430A7F600,
                }

            def _c(self, tag=None):
                return types.SimpleNamespace(tag="FRA", tag_id=5, ptr=0x20000000000)

            def player(self):
                return types.SimpleNamespace(tag="GER", political_power=200)

            def wargoal_quote(self, target_tag, wargoal, states=None):
                return {"current_pp": 200, "estimated_upfront_pp": 40.0, "estimated_days": 170.0, "daily_pp_drain": 0.2}

            def _settle(self, s=0.1):
                pass

        g = _FakeG()
        # 1) 未知理由名 "annex"：dry_run 与实写均 0 注入硬拒
        r_dry = g.justify_wargoal("FRA", wargoal="annex", dry_run=True)
        self.assertFalse(r_dry["ok"])
        self.assertEqual(r_dry["blocked_by"], "unknown_wargoal")
        self.assertEqual(r_dry["suggested_wargoal"], "take_state")
        self.assertIn("take_state", r_dry["valid_wargoals"])

        with mock.patch("cmdwin.justify_wargoal") as m_post:
            r_live = g.justify_wargoal("FRA", wargoal="annex", dry_run=False)
            self.assertFalse(r_live["ok"])
            self.assertEqual(r_live["blocked_by"], "unknown_wargoal")
            self.assertEqual(r_live["posted"], 0)
            m_post.assert_not_called()

        # 2) 合法理由名 "take_state"，但引擎未产生 CTimedWargoalActivity -> 绝不返回假 ok=True
        with mock.patch("diplomacy.justifying", return_value=[]), \
             mock.patch("cmdwin.justify_wargoal", return_value={"ok": True, "posted": 1, "act": 0x1111, "cmd": 0x2222}):
            r_unv = g.justify_wargoal("FRA", wargoal="take_state", dry_run=False)
            self.assertFalse(r_unv["ok"])
            self.assertFalse(r_unv["verified"])
            self.assertEqual(r_unv["blocked_by"], "justification_not_started")
            self.assertIn("war_readiness", r_unv["note"])

    # ------------------------------------------------------------------ P0-2
    def test_p0_2_research_base_tech_cost_110_and_no_fake_100_percent(self):
        import offsets as O
        import techdata
        from domains.research import ResearchMixin

        p = _MemProc()
        ts = 0x20000100000
        slot_arr = 0x20000200000
        res_arr = 0x20000210000
        slot0 = 0x20000300000
        slot1 = 0x20000300100
        tech0 = 0x20000400000  # concentrated_industry (未研完，进度 219.48 / 220.0)
        tech1 = 0x20000401000  # basic_machine_tools (已进 TS_RESEARCHED)
        tmpl0 = 0x20000500000
        tmpl1 = 0x20000501000

        # 组装 TS_SLOTS (2 个槽) 和 TS_RESEARCHED (仅含 tech1)
        p.put_u64(ts + O.TS_SLOTS + 0x00, slot_arr)
        p.put_i32(ts + O.TS_SLOTS + 0x08, 2)
        p.put_i32(ts + O.TS_SLOTS + 0x0C, 2)
        p.put_u64(slot_arr + 0, slot0)
        p.put_u64(slot_arr + 8, slot1)

        p.put_u64(ts + O.TS_RESEARCHED + 0x00, res_arr)
        p.put_i32(ts + O.TS_RESEARCHED + 0x08, 1)
        p.put_i32(ts + O.TS_RESEARCHED + 0x0C, 1)
        p.put_u64(res_arr + 0, tech1)

        # slot0 -> tech0 (progress 219.48, speed 1.08, cost3e0=200000 -> 220.0)
        p.put_u64(slot0 + O.RS_CURRENT, tech0)
        p.put_i64(slot0 + O.RS_SPEED, 108000)
        p.put_i32(tech0 + O.TE_ID, 101)
        p.put_i64(tech0 + O.TE_PROGRESS, 21948000)
        p.put_i32(tmpl0 + O.TT_RESEARCH_COST, 200000)

        # slot1 -> tech1 (progress 165.0, cost3e0=150000 -> 165.0, 已完成)
        p.put_u64(slot1 + O.RS_CURRENT, tech1)
        p.put_i64(slot1 + O.RS_SPEED, 108000)
        p.put_i32(tech1 + O.TE_ID, 102)
        p.put_i64(tech1 + O.TE_PROGRESS, 16500000)
        p.put_i32(tmpl1 + O.TT_RESEARCH_COST, 150000)

        class _FakeR(ResearchMixin):
            def __init__(self):
                self.p = p
                self._tech_by_name = {"concentrated_industry": tmpl0, "basic_machine_tools": tmpl1}
                self._tech_by_id = {101: "concentrated_industry", 102: "basic_machine_tools"}

            def _load_tech_db(self, wait_s=0.0):
                pass

            def tech_status(self, tag=None):
                return ts

            def production_lines(self, tag=None, include_obsolete=False):
                return []

        g = _FakeR()
        with mock.patch.object(techdata, "BASE_TECH_COST", 110.0):
            self.assertEqual(g.tech_cost_days("concentrated_industry"), 220.0)
            self.assertEqual(g.tech_cost_days("basic_machine_tools"), 165.0)
            slots = g.research_slots()
            # 槽 0：219.48 / 220.0，绝不报 100% 和 0.0 天
            self.assertEqual(slots[0]["required"], 220.0)
            self.assertFalse(slots[0]["slot_done"])
            self.assertLess(slots[0]["percent"], 100.0)
            self.assertGreater(slots[0]["days_remaining"], 0.0)
            # 即使进度 225.0 超过 220.0 但未进 TS_RESEARCHED，也封顶 99.9 / >= 0.1
            p.put_i64(tech0 + O.TE_PROGRESS, 22500000)
            slots2 = g.research_slots()
            self.assertEqual(slots2[0]["percent"], 99.9)
            self.assertEqual(slots2[0]["days_remaining"], 0.1)
            # 槽 1：已进 TS_RESEARCHED -> slot_done=True, 100.0%, 0.0 天
            self.assertTrue(slots[1]["slot_done"])
            self.assertEqual(slots[1]["percent"], 100.0)
            self.assertEqual(slots[1]["days_remaining"], 0.0)

    # ------------------------------------------------------------------ P1-3
    def test_p1_3_cancel_training_and_deficit_streak_reset(self):
        import cmdwin
        import reddots
        import turnengine as TE
        from domains.military_land import MilitaryLandMixin

        self.assertIn("CRemoveConveyorLineCommand", cmdwin.WIN_SAFE)
        self.assertIn("CRemoveConveyorLineCommand", cmdwin.ARGSPEC)
        self.assertIn("CRemoveConveyorLineCommand", cmdwin.SPEC_PATCH)
        self.assertIn("cancel_training", TE.ALLOWED_ACTIONS)

        ok_v, errs_v = TE.validate_plan({
            "actions": [{"op": "cancel_training", "args": {"count": 3, "template_name": "Paratroopers", "dry_run": True}}]
        })
        self.assertTrue(ok_v, errs_v)

        class _FakeMil(MilitaryLandMixin):
            def __init__(self):
                self.p = _MemProc()

            def _c(self, tag=None):
                return types.SimpleNamespace(tag="GER", tag_id=1)

            def conveyors(self, tag=None):
                return [
                    {
                        "ptr": "0x1001", "handle": [64, 19], "packed_handle": (19 << 32) | 64,
                        "template": "Infanterie-Division", "lines": 2,
                        "lines_detail": [
                            {"line_ptr": "0x2001", "progress": 0.70, "handle": [63, 1], "packed_handle": (1 << 32) | 63},
                            {"line_ptr": "0x2002", "progress": 0.65, "handle": [63, 2], "packed_handle": (2 << 32) | 63},
                        ],
                    },
                    {
                        "ptr": "0x1002", "handle": [64, 48], "packed_handle": (48 << 32) | 64,
                        "template": "Paratroopers", "lines": 2,
                        "lines_detail": [
                            {"line_ptr": "0x2003", "progress": 0.55, "handle": [63, 3], "packed_handle": (3 << 32) | 63},
                            {"line_ptr": "0x2004", "progress": 0.20, "handle": [63, 4], "packed_handle": (4 << 32) | 63},
                        ],
                    },
                ]

        gm = _FakeMil()
        # 删 1 条：应优先挑进度最低的 0x2004 (0.20)
        d1 = gm.cancel_training(lines=1, dry_run=True)
        self.assertTrue(d1["ok"])
        self.assertEqual(d1["planned_remove"], 1)
        self.assertEqual(d1["operations"][0]["kind"], "remove_line")
        self.assertEqual(d1["operations"][0]["line_ptr"], "0x2004")

        # 删 Paratroopers 全部 2 条：应合并为 remove_conveyor
        d2 = gm.cancel_training(lines=2, template="Paratroopers", dry_run=True)
        self.assertTrue(d2["ok"])
        self.assertEqual(len(d2["operations"]), 1)
        self.assertEqual(d2["operations"][0]["kind"], "remove_conveyor")

        # reddots: DEFICIT_OVER_TRAINING 步枪库存显著回升 (100 -> 625) 时应清零 ack_streaks
        g_rd = types.SimpleNamespace(p=types.SimpleNamespace(pid=88123))
        raw1 = {
            "ok": True, "tag": "GER", "date": "1936.11.01", "date_raw": 1000, "at_war": False,
            "items": [{
                "code": "DEFICIT_OVER_TRAINING", "title": "t", "severity": "block", "tier": 8,
                "signature": "DEFICIT_OVER_TRAINING:8:1",
                "detail": {"training_lines": 8, "rifles_stockpile": 100.0},
                "suggested_actions": [], "housekeepable": False, "force_ack_only": False,
            }],
        }
        raw2 = {
            "ok": True, "tag": "GER", "date": "1937.02.23", "date_raw": 3000, "at_war": False,
            "items": [{
                "code": "DEFICIT_OVER_TRAINING", "title": "t", "severity": "block", "tier": 8,
                "signature": "DEFICIT_OVER_TRAINING:8:0",
                "detail": {"training_lines": 8, "rifles_stockpile": 625.0},
                "suggested_actions": [], "housekeepable": False, "force_ack_only": False,
            }],
        }
        reddots._STATES.clear()
        with mock.patch.object(reddots, "_scan_raw", return_value=raw1), \
             mock.patch.object(reddots, "_load_persisted"), \
             mock.patch.object(reddots, "_save_persisted"):
            reddots.evaluate(g_rd)
            st = reddots._get_state(g_rd)
            st["ack_streaks"]["DEFICIT_OVER_TRAINING"] = 2
        with mock.patch.object(reddots, "_scan_raw", return_value=raw2), \
             mock.patch.object(reddots, "_load_persisted"), \
             mock.patch.object(reddots, "_save_persisted"):
            reddots.evaluate(g_rd)
            st = reddots._get_state(g_rd)
            self.assertNotIn("DEFICIT_OVER_TRAINING", st["ack_streaks"])

    # ------------------------------------------------------------------ P1-4
    def test_p1_4_unlock_doctrine_in_turnengine(self):
        import turnengine as TE

        self.assertIn("unlock_doctrine", TE.ALLOWED_ACTIONS)
        ok_v, errs_v = TE.validate_plan({"actions": [{"op": "unlock_doctrine", "args": {"key": "fleet_in_being"}}]})
        self.assertTrue(ok_v, errs_v)

        calls = []
        g = types.SimpleNamespace(unlock_doctrine=lambda doc, tag=None: (calls.append((doc, tag)) or {"ok": True, "doctrine": doc}))
        res = TE._dispatch_action(g, "unlock_doctrine", {"key": "fleet_in_being"}, "GER")
        self.assertTrue(res["ok"])
        self.assertEqual(calls, [("fleet_in_being", "GER")])

    # ------------------------------------------------------------------ P1-5
    def test_p1_5_autopilot_ack_silences_weak_template_and_hints_disable(self):
        import autopilot as AP
        import session_state as _SS

        # 确认唤醒文案带 wake_on_weak_template_xp=false 指引，且 ack_autopilot 置 weak_template_warned=True
        g = types.SimpleNamespace(
            p=types.SimpleNamespace(pid=77112),
            player_tag=lambda: "GER",
            date=lambda: {"raw": 16970000, "text": "1937.04.02.19"},
            politics=lambda tag=None: {"political_power": 120.0, "ruling_ideology": "fascism", "active_ideas": []},
        )
        _SS.sync(g)
        AP._MEM_STATES.clear()
        k = AP._state_key(g, tag="GER")
        AP._MEM_STATES[k] = {
            "active": True,
            "profile": {"tag": "GER", "kernel_floor": {"heartbeat_days": 30, "max_autopilot_days": 180}},
            "started_date_raw": 16970000,
            "last_heartbeat_date_raw": 16970000,
            "current_phase": "default",
            "weak_template_warned": False,
            "last_wake_trigger": ["autopilot[interrupt_extra:weak_infantry_template]: ..."],
            "journal": [],
        }
        with mock.patch.object(AP, "_load_persisted"), \
             mock.patch.object(AP, "_save_persisted"):
            r = AP.acknowledge_wakeup(g, tag="GER", reason="defer upgrade")
        self.assertTrue(r["ok"], r)
        self.assertTrue(AP._MEM_STATES[k]["weak_template_warned"])

    # ------------------------------------------------------------------ P2-6 & P2-7
    def test_p2_6_and_p2_7_battleplan_border_threshold_and_supply_alignment(self):
        import armyops
        import armyops_win as A
        import mapdata
        import unitorders
        from domains.battleplan import BattleplanMixin

        p = _MemProc()
        og1 = 0x20000600000  # 32 师（6 伞兵 + 26 陆战师，仅 1 陆战师贴 BEL 边境）
        og2 = 0x20000601000  # 12 师（驻内陆）
        og3 = 0x20000602000  # 10 师（8 师明确驻防 BEL 边境 -> 真正边防军）
        p.put_u8(og1 + A.OG_IS_GROUP, 0)
        p.put_u8(og2 + A.OG_IS_GROUP, 0)
        p.put_u8(og3 + A.OG_IS_GROUP, 0)

        para_divs = [0x30000000100 + i * 8 for i in range(6)]
        og1_reg = [0x30000001000 + i * 8 for i in range(26)]
        og2_divs = [0x30000002000 + i * 8 for i in range(12)]
        og3_divs = [0x30000003000 + i * 8 for i in range(10)]

        # 省 10 与 BEL(省 20) 相邻；省 30 属内陆
        owners = {10: "GER", 20: "BEL", 30: "GER"}
        adj = {10: [20, 30], 20: [10], 30: [10]}
        ptype = {10: "land", 20: "land", 30: "land"}

        def _prov_of(_g, dp):
            if dp == og1_reg[0]:
                return 10  # og1 仅 1/26 贴 BEL
            if dp in og3_divs[:8]:
                return 10  # og3 8/10 贴 BEL
            return 30

        class _FakeBP(BattleplanMixin):
            def __init__(self, cap):
                self.p = p
                self._cap = cap

            def _c(self, tag=None):
                return types.SimpleNamespace(tag="GER", ptr=0x20000000000)

            def wars(self, tag=None):
                return {"enemies": []}

            def divisions(self, tag=None, limit=1):
                return {"count": 44, "divisions": []}

            def _supply_front_guard(self, enemy_tag, tag=None, ratio=1.25):
                return {"ok": False, "needed": 44, "capacity": self._cap, "ratio": round(44 / self._cap, 2)}

        def _orders(_g, og):
            if og == og1:
                return [{"id": 74, "type": 4}]
            return []

        def _divs_of(_g, og):
            return {og1: para_divs + og1_reg, og2: og2_divs, og3: og3_divs}[og]

        def _ord_divs(_g, og, oid):
            return para_divs if (og == og1 and oid == 74) else []

        with mock.patch.object(A, "fronts_towards", return_value=[{
                "front": 0, "front_handle": [1, 1], "section": 0,
                "border_provinces": [100], "slice_from": 0, "slice_to": 100000,
             }]), \
             mock.patch.object(A, "target_provinces", return_value=[200]), \
             mock.patch.object(armyops, "orders_groups", return_value=[og1, og2, og3]), \
             mock.patch.object(A, "orders", side_effect=_orders), \
             mock.patch.object(A, "divisions_of", side_effect=_divs_of), \
             mock.patch.object(A, "order_assigned_divisions", side_effect=_ord_divs), \
             mock.patch.object(mapdata, "province_owner_map", return_value=(owners, {})), \
             mock.patch.object(mapdata, "build_adjacency", return_value=adj), \
             mock.patch.object(mapdata, "load_definitions", return_value=({}, ptype)), \
             mock.patch.object(unitorders, "province_of", side_effect=_prov_of):

            # 场景 1：容量 50 -> og1 (26师) 和 og2 (12师) 均应入选，真正边防军 og3 仍被保护
            g1 = _FakeBP(cap=50.0)
            r1 = g1.plan_front("FRA", dry_run=True)
            self.assertTrue(r1["ok"])
            planned = {x["army"]: x["divisions"] for x in r1["steps"]["plan"]}
            self.assertEqual(planned, {hex(og1): 26, hex(og2): 12})
            self.assertEqual([x["army"] for x in r1["steps"]["free_armies_guarding_other_borders"]], [hex(og3)])
            self.assertEqual([x["army"] for x in r1["steps"]["incidental_other_border_armies"]], [hex(og1)])
            self.assertTrue(r1["steps"]["supply"]["committed_ok"])
            self.assertTrue(r1["steps"]["supply"]["ok"])

            # 场景 2：容量 19.1 -> 自动裁剪只上 og2 (12 师 <= 19.1)，supply.ok 与 committed_ok 同步为 True
            g2 = _FakeBP(cap=19.1)
            r2 = g2.plan_front("FRA", dry_run=True)
            self.assertTrue(r2["ok"])
            self.assertEqual([x["army"] for x in r2["steps"]["plan"]], [hex(og2)])
            self.assertEqual(r2["steps"]["supply"]["committed"], 12)
            self.assertTrue(r2["steps"]["supply"]["committed_ok"])
            self.assertTrue(r2["steps"]["supply"]["ok"])

            # 场景 3：强制只指定 og1 (26 师) 压进容量 10.0 (> 1.5x) 且 force=False -> 硬拦 supply_over_capacity
            g3 = _FakeBP(cap=10.0)
            r3 = g3.plan_front("FRA", armies=[hex(og1)], force=False, dry_run=True)
            self.assertFalse(r3["ok"])
            self.assertEqual(r3["blocked_by"], "supply_over_capacity")

    # ------------------------------------------------------------------ P2-8
    def test_p2_8_state_detail_non_core_occupation_note(self):
        import offsets as O
        from domains.economy_build import EconomyBuildMixin

        p = _MemProc()
        co_ptr = 0x20000700000
        st_arr = 0x20000710000
        st_ptr = 0x20000720000
        core_arr = 0x20000730000

        p.put_u64(co_ptr + O.CO_STATES + 0x00, st_arr)
        p.put_i32(co_ptr + O.CO_STATES + 0x08, 1)
        p.put_i32(co_ptr + O.CO_STATES + 0x0C, 1)
        p.put_u64(st_arr, st_ptr)
        p.put_i32(st_ptr + O.ST_ID, 1058)
        # cores = [97] -> INS (非 GER 核心)
        p.put_u64(st_ptr + 0xB0, core_arr)
        p.put_i32(st_ptr + 0xBC, 1)
        p.put_i32(core_arr, 97)

        class _FakeEB(EconomyBuildMixin):
            def __init__(self):
                self.p = p
                self._bld_by_id = {}

            def _load_buildings(self):
                pass

            def _c(self, tag=None):
                return types.SimpleNamespace(tag="GER", ptr=co_ptr, modifiers=lambda: {})

        g = _FakeEB()
        with mock.patch("combat._country_id_map", return_value={1: "GER", 97: "INS"}), \
             mock.patch("statemeta.state_meta", return_value={"name": "Batavia"}):
            det = g.state_detail(1058, tag="GER")
        self.assertFalse(det["is_core_of_owner"])
        self.assertIn("NON-CORE", det["occupation_note"])
        self.assertNotIn("this is a CORE state", det["occupation_note"])

    # ------------------------------------------------------------------ P2-9
    def test_p2_9_naval_max_dockyards_and_brief_format(self):
        import turnengine as TE

        md = TE._render_brief_markdown({
            "meta": {"date": "1937.05.26.08", "tag": "GER", "paused": True},
            "country": {"name": "Germany"},
            "factories": {"civ_total": 60, "construction_queue_len": 4, "mil_total": 35, "mil_free": 0, "dock_total": 11},
            "army": {"total_divisions": 44, "unassigned_divisions": 0},
            "war": {"at_war": False, "enemies": [], "combats": []},
            "focus": {"current": "GER_four_year_plan", "name": "四年计划"},
            "research": [],
            "production": [
                {"index": 9, "equipment": "ship_hull_cruiser_2", "factories": 5, "max_dockyards": 5, "class": "CNavalProductionLine"},
                {"index": 0, "equipment": "infantry_equipment_1", "factories": 10, "class": "CMilitaryProductionLine"},
            ],
            "production_total": 2,
        })
        self.assertIn("#9[坞]:ship_hull_cruiser_2(5/5厂)", md)
        self.assertIn("#0:infantry_equipment_1(10厂)", md)

    # ------------------------------------------------------------------ P3-10 & P3-11
    def test_p3_10_peace_owned_states_uses_co_states_b_and_refresh_note(self):
        import offsets_win as OW
        import peacewin

        p = _MemProc()
        co_ptr = 0x20000800000
        ctrl_arr = 0x20000810000
        own_arr = 0x20000820000

        def _make_state(addr, sid):
            p.put_i32(addr + OW.ST_ID, sid)
            return addr

        s64 = _make_state(0x20000830000, 64)
        s7 = _make_state(0x20000830100, 7)
        s1022 = _make_state(0x20000830200, 1022)

        # 和会前：CO_STATES(0x460 控制州) 已有 64 + 7；CO_STATES_B(0x478 拥有州) 只有 64
        p.put_u64(co_ptr + OW.CO_STATES, ctrl_arr)
        p.put_i32(co_ptr + OW.CO_STATES + 0x0C, 2)
        p.put_u64(ctrl_arr, s64)
        p.put_u64(ctrl_arr + 8, s7)

        p.put_u64(co_ptr + OW.CO_STATES_B, own_arr)
        p.put_i32(co_ptr + OW.CO_STATES_B + 0x0C, 1)
        p.put_u64(own_arr, s64)

        conf = 0x20000900000
        p.put_u8(conf + 0xDD, 0)

        class _FakePG:
            def __init__(self):
                self.p = p

            def _c(self, tag=None):
                return types.SimpleNamespace(tag="GER", tag_id=1, ptr=co_ptr)

            def tags(self):
                return ["GER"]

            def player_tag(self):
                return "GER"

            def states(self, tag=None):
                return [{"id": 64}, {"id": 7}]

        g = _FakePG()
        before = peacewin._owned_state_ids(g, "GER")
        self.assertEqual(before, {64})

        # 模拟 execute("done") 后拥有州变为 {64, 7, 1022}，且 conf 标记 finalized
        def _fake_exec(_g, what, idx):
            p.put_i32(co_ptr + OW.CO_STATES_B + 0x0C, 3)
            p.put_u64(own_arr + 0, s64)
            p.put_u64(own_arr + 8, s7)
            p.put_u64(own_arr + 16, s1022)
            p.put_u8(conf + 0xDD, 1)
            p.put_u64(conf + 0x1C8 + 8, 1)
            p.put_u64(conf + 0x1F8 + 8, 0)
            return {"ran": what, "result": 1}

        peacewin._SUBMITTED_DEMANDS[(p.pid, conf)] = {"states": [7]}
        with mock.patch.object(peacewin, "live_conference", return_value=conf), \
             mock.patch.object(peacewin, "execute", side_effect=_fake_exec), \
             mock.patch.object(peacewin, "maps", return_value={"0x1f8": 1}):
            fin = peacewin.finish(g, 1)
        self.assertEqual(fin["states_before_count"], 1)
        self.assertEqual(fin["states_after_count"], 3)
        self.assertEqual(fin["net_states_gained"], [7, 1022])
        self.assertEqual(fin["acquired_states"], [7])
        self.assertEqual(fin["unrequested_states_gained"], [1022])
        self.assertIn("resources_refresh_note", fin)

    # ------------------------------------------------------------------ P3-12
    def test_p3_12_select_event_option_alias(self):
        import turnengine as TE

        ok_v, errs_v = TE.validate_plan({"actions": [{"op": "select_event", "args": {"option": 3}}]})
        self.assertTrue(ok_v, errs_v)
        norm, errs = TE._normalize_action_args("select_event", {"option": 3})
        self.assertEqual(errs, [])
        self.assertEqual(norm["index"], 3)

    # ------------------------------------------------------------------ air_transfer
    def test_air_transfer_layout_guards_and_dsl(self):
        import airops_win as AW
        import cmdwin
        import turnengine as TE

        # 1. 白名单与 DSL 校验（含别名字段）
        self.assertIn("CStratAirTransferCommand", cmdwin.WIN_SAFE)
        self.assertIn("air_transfer", TE.ALLOWED_ACTIONS)
        ok_v, errs_v = TE.validate_plan({
            "actions": [{
                "op": "air_transfer",
                "args": {"wing": "transport_plane", "state": 7, "mission": "none", "dry_run": True},
            }]
        })
        self.assertTrue(ok_v, errs_v)

        # 2. make_transfer 精确 72 字节结构体字段验证
        captured = {}

        def _fake_build(game, vt, size, fields):
            captured["vt"] = vt
            captured["size"] = size
            captured["fields"] = dict((off, (fmt, val)) for off, fmt, val in fields)
            return 0x20000ABC000

        wing_h = (5 << 32) | 69
        ab_h = (122 << 32) | 65
        with mock.patch.object(cmdwin, "build_by_vt", side_effect=_fake_build):
            cmd_ptr = AW.make_transfer(None, wing_h, ab_h, to_reserve=False, keep_army=False, region_id=19, mission_mask=1)
        self.assertEqual(cmd_ptr, 0x20000ABC000)
        self.assertEqual(captured["vt"], AW.VT_TRANSFER)
        self.assertEqual(captured["size"], 0x48)
        self.assertEqual(captured["fields"][0x28], ("<Q", wing_h))
        self.assertEqual(captured["fields"][0x30], ("<Q", ab_h))
        self.assertEqual(captured["fields"][0x38], ("<B", 0))
        self.assertEqual(captured["fields"][0x39], ("<B", 0))
        self.assertEqual(captured["fields"][0x3C], ("<i", 19))
        self.assertEqual(captured["fields"][0x40], ("<i", 1))

        # 3. transfer_wings：无机场拦截 / 幂等跳过 / 容量预警 / 实写 CanExecute+post_drained+回读
        p = _MemProc()
        wptr = 0x20000D00000
        g = types.SimpleNamespace(
            p=p,
            _c=lambda tag=None: types.SimpleNamespace(tag="GER", tag_id=1),
        )
        fake_ab7 = {
            "state_id": 7, "state_name": "Holland", "province": 68, "provinces": [68],
            "airbase_ptr": "0x17548722f30", "handle": [65, 122],
            "packed_handle": ab_h, "handle_hex": hex(ab_h),
            "level": 1, "capacity": 200, "current_planes": 150, "free_capacity": 50,
        }
        fake_wings = {
            "ok": True, "count": 2, "wings": [
                {"id": 5, "ptr": hex(wptr), "handle": hex(wing_h), "type": "transport_plane",
                 "planes": 98, "air_base_state": 64, "air_base_province": 9496},
                {"id": 1, "ptr": "0x20000D01000", "handle": hex((1 << 32) | 69), "type": "fighter",
                 "planes": 100, "air_base_state": 7, "air_base_province": 68},
            ],
        }

        with mock.patch.object(AW, "all_airbases", return_value=[fake_ab7]), \
             mock.patch.object(AW, "find_airbase", return_value=None):
            r_noab = AW.transfer_wings(g, "5", target_state=42, dry_run=True)
            self.assertFalse(r_noab["ok"])
            self.assertEqual(r_noab["blocked_by"], "no_airbase_at_target")

        with mock.patch.object(AW, "find_airbase", return_value=fake_ab7), \
             mock.patch.object(AW, "select_wings", return_value=[(5, wing_h), (1, (1 << 32) | 69)]), \
             mock.patch.object(AW, "country_wings", return_value=fake_wings):
            # dry_run: 联队 1 已在州 7 被幂等跳过，联队 5 预计 150+98=248 > 200 触发容量预警
            r_dry = AW.transfer_wings(g, "5,1", target_state=7, dry_run=True)
            self.assertTrue(r_dry["ok"])
            self.assertEqual(r_dry["to_transfer_count"], 1)
            self.assertEqual(r_dry["already_at_target_count"], 1)
            self.assertTrue(r_dry["capacity_estimate"]["overcrowded"])
            self.assertIsNotNone(r_dry["capacity_warning"])

            # 实写模拟：post_drained 写入 wing+0x48 = ab_h，done_fn 命中并回读 verified=True
            def _fake_post(_g, _cmd, seconds=2.0, done_fn=None):
                p.put_u64(wptr + AW.WING_TRANSFER_TARGET, ab_h)
                hit = done_fn() if done_fn else False
                return {"posted": 1, "drained": hit}

            with mock.patch.object(AW, "make_transfer", return_value=0x20000E00000), \
                 mock.patch.object(AW, "can_exec", return_value=True), \
                 mock.patch.object(cmdwin, "post_drained", side_effect=_fake_post):
                r_go = AW.transfer_wings(g, "5,1", target_state=7, dry_run=False)
            self.assertTrue(r_go["ok"])
            self.assertEqual(r_go["transferred_count"], 1)
            self.assertEqual(r_go["already_at_target_count"], 1)
            self.assertTrue(r_go["results"][0]["verified"])
            self.assertEqual(r_go["results"][0]["transfer_target_readback"], hex(ab_h))


if __name__ == "__main__":
    unittest.main()
