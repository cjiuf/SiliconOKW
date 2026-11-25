# -*- coding: utf-8 -*-
"""1936 长跑实测问题专项单测（离线纯内存，覆盖 §1~§8 修复与防回归）。"""
import os
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CALLING = os.path.join(ROOT, "calling")
MCP_DIR = os.path.join(ROOT, "mcp")
for _p in (CALLING, MCP_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import reddots as RD
import turnengine as TE
import turnloop as TL
from process import winject as WJ
import server as MCP
from tools import core as CORE_TOOLS
from domains.diplomacy_war import DiplomacyWarMixin


class _FakeCountry:
    def __init__(self, tag="GER", pp=100.0, fuel=5000.0):
        self.tag = tag
        self.ptr = 0x100000
        self.tag_id = 1
        self.political_power = pp
        self.fuel = fuel
        self.civilian_factories = 20
        self.military_factories = 20
        self.naval_dockyards = 5
        self.experience = {"army": 10.0, "navy": 5.0, "air": 5.0}

    def summary(self):
        return {
            "tag": self.tag,
            "political_power": self.political_power,
            "fuel": self.fuel,
            "experience": dict(self.experience),
            "manpower": 500000,
        }


class _FakeProc:
    def __init__(self, pid=20120, base=0x7FF600000000):
        self.pid = pid
        self.base = base


class _FakeGame(DiplomacyWarMixin):
    def __init__(self, pid=20120, pp=100.0, fuel=5000.0):
        self.p = _FakeProc(pid=pid)
        self._cobj = _FakeCountry("GER", pp=pp, fuel=fuel)
        self._date_raw = 17000000
        self._paused = False
        self._enemies = []
        self._mock_wargoals = []
        self._mock_justifying = []
        self._res_by_tag = {
            "GER": {"oil": 0.0},
            "ROM": {"oil": 48.0},
            "SOV": {"oil": 120.0},
        }
        self._oi = None

    def player_tag(self):
        return self._cobj.tag

    def player(self):
        return self._cobj

    def _c(self, tag=None):
        return self._cobj

    def country(self, tag):
        return _FakeCountry(str(tag).upper(), pp=self._cobj.political_power, fuel=self._cobj.fuel)

    def tags(self):
        return list(self._res_by_tag.keys())

    def resources(self, tag=None):
        t = str(tag or self._cobj.tag).upper()
        return dict(self._res_by_tag.get(t, {}))

    def states(self, tag=None):
        return [{"id": 7}]

    def date_raw(self):
        return self._date_raw

    def date(self):
        return {"raw": self._date_raw, "text": "1936.08.18.12", "year": 1936, "month": 8, "day": 18}

    def game_speed(self):
        return {"speed": 3, "ui_speed": 4, "seconds_per_hour": 0.08}

    def current_focus(self, tag=None):
        return {"key": "GER_remilitarize_the_rhineland", "name": "Rhineland"}

    def focus_progress(self, tag=None):
        return {"progress": 30.0, "days_left": 40.0, "percent": 42.9}

    def research_slots(self, tag=None):
        return [{"slot": 0, "slot_busy": True, "researching": "basic_machine_tools", "percent": 50.0}]

    def decisions(self, tag=None, which="visible", rich=True):
        return []

    def production_lines(self, tag=None, include_obsolete=True):
        return [{"index": 0, "equipment": "infantry_equipment_1", "factories": 20, "class": "CMilitaryProductionLine"}]

    def construction_queue(self, tag=None):
        return [{"index": 0, "building": "industrial_complex", "state": 64, "factories": 10}]

    def unassigned_divisions(self, tag=None):
        return []

    def equipment_stockpile(self, tag=None, limit=120):
        return {"totals_by_equipment": {"infantry_equipment_1": 2000.0}}

    def conveyors(self, tag=None):
        return []

    def _active_variant_names(self, tag=None):
        return ["infantry_equipment_1"]

    def _air_snapshot(self):
        return []

    def _navy_snapshot(self):
        return []

    def overview(self, tag=None):
        return {
            "date": "1936.08.18.12",
            "alerts": [],
            "country": self._cobj.summary(),
            "factories": {"civ_total": 20, "construction_queue_len": 1, "mil_total": 20, "mil_free": 0, "dock_total": 5},
            "states": 20,
            "army_summary": {"total_divisions": 40, "unassigned_divisions": 0},
            "focus": {"current": "GER_remilitarize_the_rhineland", "name": "Rhineland"},
            "research": self.research_slots(tag),
            "construction": self.construction_queue(tag),
            "production": self.production_lines(tag),
            "candidates": {"available_decisions": []},
        }


class TestLongRunFixes(unittest.TestCase):
    def setUp(self):
        RD.reset_state(None, persist=False)
        RD.configure(enabled=True, guard_enabled=False)

    def tearDown(self):
        RD.reset_state(None, persist=False)
        RD.configure(enabled=True)

    # ---- §1: CB_AVAILABLE / JUSTIFY_LOST 双向唤醒 + 红点严控 + 简报显式行 ----
    def test_wargoal_signal_wakes_and_force_pauses_on_new_cb_and_justify_lost(self):
        g = _FakeGame()
        g._mock_justifying = [{"from": "GER", "target": "HOL", "source_class": "CTimedWargoalActivity", "days_left": 5}]
        g._mock_wargoals = []

        # 场景 A：正当化完成 -> 出现 CB
        prev = TL.snapshot(g, signals=("wargoal",))
        g._mock_justifying = []
        g._mock_wargoals = [{"owner_tag": "GER", "target_tag": "HOL", "wargoal": "take_state", "valid": True, "ptr": "0x1234"}]
        cur = TL.snapshot(g, signals=("wargoal",))
        reasons = TL.wake_reasons(prev, cur)
        self.assertTrue(any("HOL" in r and "take_state" in r for r in reasons), f"未命中新 CB 唤醒: {reasons}")

        # 场景 B：正当化消失且没有新 CB -> JUSTIFY_LOST
        g._mock_justifying = [{"from": "GER", "target": "BEL", "source_class": "CTimedWargoalActivity", "days_left": 1}]
        g._mock_wargoals = []
        prev2 = TL.snapshot(g, signals=("wargoal",))
        g._mock_justifying = []
        cur2 = TL.snapshot(g, signals=("wargoal",))
        reasons2 = TL.wake_reasons(prev2, cur2)
        self.assertTrue(any("JUSTIFY_LOST" in r and "BEL" in r for r in reasons2), f"未命中 JUSTIFY_LOST: {reasons2}")

        # 场景 C：TurnLoop.wait 在关掉红点且 pause_on_wake=False 时，新 CB 仍强制停表
        g2 = _FakeGame()
        g2._mock_wargoals = []
        ticks = [0]

        def _adv_wargoals(tag=None):
            ticks[0] += 1
            if ticks[0] >= 2:
                return [{"owner_tag": "GER", "target_tag": "HOL", "wargoal": "take_state", "valid": True, "ptr": "0x9999"}]
            return []

        g2._mock_wargoals = _adv_wargoals
        tl = TL.TurnLoop(g2, poll=0.01, pause_on_wake=False)
        with mock.patch.object(TL.TurnLoop, "_force_pause_safe", return_value=True) as mp:
            res = tl.wait(
                max_days=30,
                signals=("wargoal",),
                poll=0.01,
                timeout_seconds=2.0,
                pause_on_wake=False,
                respect_red_dots=False,
            )
            self.assertTrue(res.get("wargoal_woke"))
            self.assertTrue(res.get("paused"))
            mp.assert_called_once()

    def test_reddots_cb_available_and_justify_lost_are_strict_and_not_auto_solvable(self):
        self.assertIn("CB_AVAILABLE", RD.STRICT_CODES)
        self.assertIn("JUSTIFY_LOST", RD.STRICT_CODES)
        self.assertNotIn("CB_AVAILABLE", RD.AUTO_SOLVABLE_CODES)
        self.assertNotIn("JUSTIFY_LOST", RD.AUTO_SOLVABLE_CODES)

        g = _FakeGame()
        g._mock_wargoals = [{"owner_tag": "GER", "target_tag": "HOL", "wargoal": "take_state", "valid": True, "ptr": "0x5555"}]
        with mock.patch("combat.enemy_tags", return_value=[]):
            ev = RD.evaluate(g, fast=True)
            codes = [x["code"] for x in ev["blocking"]]
            self.assertIn("CB_AVAILABLE", codes)
            # 普通 ack 必须拒绝
            deny = RD.ack(g, "CB_AVAILABLE", "稍后等装甲师到位再宣战", force=False, persist=False)
            self.assertFalse(deny["ok"])
            self.assertEqual(deny.get("blocked_by"), "strict_red_dot")

            # JUSTIFY_LOST：先观测正在正当化，再消失且无 CB
            g._mock_wargoals = []
            g._mock_justifying = [{"from": "GER", "target": "BEL", "source_class": "CTimedWargoalActivity", "days_left": 10}]
            RD.evaluate(g, fast=True)
            g._mock_justifying = []
            g._date_raw += 24
            ev2 = RD.evaluate(g, fast=True)
            codes2 = [x["code"] for x in ev2["blocking"]]
            self.assertIn("JUSTIFY_LOST", codes2)
            # 强制签收后应清掉已确认目标
            ok_ack = RD.ack(g, "JUSTIFY_LOST", "已核查并改为重新正当化", force=True, persist=False)
            self.assertTrue(ok_ack["ok"])
            ev3 = RD.evaluate(g, fast=True)
            self.assertNotIn("JUSTIFY_LOST", [x["code"] for x in ev3["blocking"]])

    def test_turn_brief_renders_cb_and_justifying_lines(self):
        g = _FakeGame()
        g._mock_wargoals = [{"owner_tag": "GER", "target_tag": "HOL", "wargoal": "take_state", "valid": True, "ptr": "0x5555"}]
        g._mock_justifying = [{"from": "GER", "target": "BEL", "source_class": "CTimedWargoalActivity", "days_left": 120, "eta_date": "1936.12.16"}]
        with mock.patch("combat.enemy_tags", return_value=[]):
            res = TE.build_turn_brief(g, include_data=True)
        md = res["markdown"]
        self.assertIn("宣战理由 (CB)", md)
        self.assertIn("HOL(take_state)", md)
        self.assertIn("正当化中", md)
        self.assertIn("BEL(剩120天, ETA 1936.12.16)", md)

    # ---- §2: 红点总开关与 compact 档 respect_red_dots ----
    def test_red_dot_master_switch_disables_turnloop_gate_without_breaking_readonly_eval(self):
        g = _FakeGame()
        g._mock_wargoals = [{"owner_tag": "GER", "target_tag": "HOL", "wargoal": "take_state", "valid": True, "ptr": "0x1111"}]
        RD.configure(enabled=False)

        with mock.patch("combat.enemy_tags", return_value=[]):
            # 只读 evaluate 仍然报告红点（不误伤诊断）
            ev = RD.evaluate(g, fast=True)
            self.assertTrue(ev["has_blocking"])
            # 但 TurnLoop.wait 不应再在闸门 A/B 拦截（日期正常走满 max_days）
            tl = TL.TurnLoop(g, poll=0.01)

            def _tick_date():
                g._date_raw += 24 * 5
                return {"raw": g._date_raw, "text": "1936.08.23.12"}

            g.date = _tick_date
            res = tl.wait(max_days=3, signals=(), poll=0.01, timeout_seconds=2.0, respect_red_dots=True)
            self.assertNotEqual(res.get("blocked_by"), "red_dots")
            self.assertFalse(res.get("red_dot_paused"))

    def test_wait_and_brief_forwards_respect_red_dots_and_echoes_timeout(self):
        g = _FakeGame()
        captured = {}

        def _fake_wait(self_tl, **kw):
            captured.update(kw)
            return {
                "ok": True,
                "woke_because": ["time is up (14 days)"],
                "waited_game_days": 14.0,
                "waited_real_seconds": 1.2,
                "effective_timeout_seconds": kw["timeout_seconds"],
                "paused": True,
            }

        with mock.patch.object(TL.TurnLoop, "wait", _fake_wait), \
             mock.patch.dict(os.environ, {"HOI4_WAIT_MAX_S": "38"}, clear=False):
            out = TE.wait_and_brief(g, max_days=14, timeout_seconds=110, respect_red_dots=False)
        self.assertFalse(captured["respect_red_dots"])
        self.assertEqual(captured["timeout_seconds"], 38)
        self.assertEqual(out["wait"]["timeout_seconds"], 38)
        self.assertEqual(out["wait"]["requested_timeout_seconds"], 110)
        self.assertEqual(out["wait"]["timeout_max_seconds"], 38)
        self.assertTrue(out["wait"]["timeout_clamped"])

    # ---- §3: 统一 timeout_seconds 解析 ----
    def test_resolve_wait_timeout_env_and_clamping(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("HOI4_WAIT_MAX_S", None)
            r0 = TL.resolve_wait_timeout(requested=110)
            self.assertEqual(r0["effective"], 26)
            self.assertEqual(r0["max_s"], 26)
            self.assertTrue(r0["clamped"])
        with mock.patch.dict(os.environ, {"HOI4_WAIT_MAX_S": "60"}, clear=False):
            r1 = TL.resolve_wait_timeout(requested=110)
            self.assertEqual(r1["effective"], 60)
            self.assertEqual(r1["max_s"], 60)
            self.assertTrue(r1["clamped"])
            r2 = TL.resolve_wait_timeout(requested=25)
            self.assertEqual(r2["effective"], 25)
            self.assertFalse(r2["clamped"])
            r3 = TL.resolve_wait_timeout(requested=None)
            self.assertEqual(r3["effective"], 60)
            self.assertFalse(r3["clamped"])

    # ---- §5: wargoal_quote 与 justify_wargoal 口径一致 ----
    def test_wargoal_quote_and_justify_pp_consistency(self):
        g = _FakeGame(pp=3.0)
        with mock.patch.object(sys, "platform", "win32"):
            q = g.wargoal_quote("HOL", "take_state", states=[7])
            self.assertTrue(q["ok"])
            self.assertIsNone(q["required_pp"])
            self.assertEqual(q["estimated_upfront_pp"], q["estimated_total_pp"])
            self.assertFalse(q["estimated_can_justify_now"])
            self.assertGreater(q["estimated_pp_shortfall"], 0)

            j = g.justify_wargoal("HOL", "take_state", states=[7], force=False)
            self.assertFalse(j["ok"])
            self.assertEqual(j["blocked_by"], "political_power")
            self.assertEqual(j["estimated_upfront_pp"], q["estimated_upfront_pp"])
            self.assertIn("force=true", j["error"])

    # ---- §6: hoi4_status.injections 统计不再为 None ----
    def test_status_injections_tracks_winject_and_backend_counts(self):
        WJ.reset_call_count(20120)
        WJ._CALL_COUNTS[20120] = 18
        try:
            p = _FakeProc(pid=20120)
            self.assertEqual(CORE_TOOLS._resolve_injections_count(p), 18)
            p.call_count = 25
            self.assertEqual(CORE_TOOLS._resolve_injections_count(p), 25)
        finally:
            WJ.reset_call_count(20120)

    # ---- §7: hoi4_pause 不再硬编码 pid 29864 ----
    def test_pause_multiplayer_note_uses_live_pid(self):
        g = _FakeGame(pid=20120)
        with mock.patch.object(MCP, "game", return_value=g), \
             mock.patch("session.pause", return_value=(True, "")):
            res = MCP.HANDLERS["hoi4_pause"]({"paused": True})
        note = res.get("multiplayer_note") or ""
        self.assertNotIn("29864", note)
        self.assertIn("20120", note)

    # ---- §8: FUEL_ZERO 给出真实石油出口国且不含占位符 ----
    def test_fuel_zero_suggestion_uses_real_exporters_and_no_placeholders(self):
        g = _FakeGame(fuel=0.0)
        g._oi = object()
        g._air_snapshot = lambda: [{"type": "fighter", "mission": 1}]
        with mock.patch("combat.enemy_tags", return_value=[]), \
             mock.patch("trade_win.world_trades", return_value=[]):
            ev = RD.evaluate(g, fast=False)
        fz = next((x for x in ev["blocking"] if x["code"] == "FUEL_ZERO"), None)
        self.assertIsNotNone(fz)
        act0 = fz["suggested_actions"][0]
        self.assertNotIn("<", act0)
        self.assertIn("partner='SOV'", act0)

        # 无任何可用出口国时同样不得出现 <...> 占位符
        g._res_by_tag = {"GER": {"oil": 0.0}}
        with mock.patch("combat.enemy_tags", return_value=[]), \
             mock.patch("trade_win.world_trades", return_value=[]):
            ev2 = RD.evaluate(g, fast=False)
        fz2 = next((x for x in ev2["blocking"] if x["code"] == "FUEL_ZERO"), None)
        self.assertIsNotNone(fz2)
        act0_empty = fz2["suggested_actions"][0]
        self.assertNotIn("<", act0_empty)
        self.assertIn("hoi4_trade_routes()", act0_empty)


if __name__ == "__main__":
    unittest.main()
