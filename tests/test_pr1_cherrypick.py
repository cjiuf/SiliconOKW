# -*- coding: utf-8 -*-
"""PR #1 择优吸收项离线专项单测（A1 / A4 / A5 / A10 / A11）。

覆盖点：
  1. A11: reddots.STRICT_CODES 与 NON_IGNORABLE_CODES 包含 PEACE_CONFERENCE，
          和会开启时 _scan_raw 产出严控阻塞项；TurnLoop.wait 在开跑前即发现
          peace_open 时立刻停表返回 blocked_pre_wait / wake_kind / stall_reason，
          超时停滞同样精确标注 wake_kind / stall_reason；wait_and_brief 透传。
  2. A4:  winject 调用方排行 call_breakdown / _infer_caller / reset_call_count，
          Proc.injections / Proc.injection_breakdown，以及 hoi4_status 返回
          injections_scope 与 injections_by_caller（且不破坏 MCP Schema 契约）。
  3. A1:  stats.build() 进程内缓存命中；overview._air_snapshot 静态游戏时钟延长 TTL；
          build_turn_brief 与 war_readiness_check 透出分阶段 timings_ms。
  4. A5:  divenum.all_countries_divisions 单趟无全堆扫描统计与 non_zero_only 过滤；
          Game.divisions 返回 tag / scope。
  5. A10: housekeep() 与 execute_plan() 回执补全 days_elapsed / paused_before /
          paused_after / unpaused_for_drain，且原未暂停时不误报 unpaused_for_drain。
"""
import os
import sys
import types
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CALLING = os.path.join(ROOT, "calling")
MCP_DIR = os.path.join(ROOT, "mcp")
for _p in (ROOT, CALLING, MCP_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)


class TestA11PeaceBlocking(unittest.TestCase):
    """A11: 和平会议严控红点与推钟前置拦阻。"""

    def test_peace_conference_in_strict_and_non_ignorable(self):
        import reddots as RD

        self.assertIn("PEACE_CONFERENCE", RD.STRICT_CODES)
        self.assertIn("PEACE_CONFERENCE", RD.NON_IGNORABLE_CODES)

    def test_scan_raw_emits_peace_conference_strict_item(self):
        import peacewin as PW
        import reddots as RD

        class _FakeCountry:
            tag = "GER"
            civilian_factories = 30
            military_factories = 28
            naval_dockyards = 10
            political_power = 50.0

            def summary(self):
                return {"civilian_factories": 30, "military_factories": 28, "naval_dockyards": 10}

        class _FakeGame:
            p = types.SimpleNamespace(pid=4242)

            def player_tag(self):
                return "GER"

            def player(self):
                return _FakeCountry()

            def date(self):
                return {"text": "1939.09.10", "raw": 17000000}

            def decisions(self, *a, **kw):
                return []

            def current_focus(self, *a, **kw):
                return {"key": "GER_danzig_or_war"}

            def research_slots(self, *a, **kw):
                return [{"slot": 0, "slot_busy": True}]

            def production_lines(self, *a, **kw):
                return []

            def construction_queue(self, *a, **kw):
                return [{"building": "arms_factory", "factories": 15}]

            def unassigned_divisions(self, *a, **kw):
                return []

            def order_groups(self, *a, **kw):
                return {"groups": []}

            def equipment_stockpile(self, *a, **kw):
                return {"totals_by_equipment": {}}

            def conveyors(self, *a, **kw):
                return []

        with mock.patch.object(PW, "live_conference_readonly", return_value=0x12345678):
            raw = RD._scan_raw(_FakeGame(), fast=True)
        codes = [it["code"] for it in raw.get("items") or ()]
        self.assertIn("PEACE_CONFERENCE", codes)
        pc = next(it for it in raw["items"] if it["code"] == "PEACE_CONFERENCE")
        self.assertEqual(pc["severity"], "strict")
        self.assertTrue(pc["force_ack_only"])
        self.assertEqual(pc["detail"]["conference_ptr"], "0x12345678")

    def test_turnloop_pre_wait_blocks_when_peace_already_open(self):
        import turnloop as TL

        g = types.SimpleNamespace(
            p=types.SimpleNamespace(pid=1111),
            game_speed=lambda: {"speed": 2, "ui_speed": 3},
            date=lambda: {"text": "1939.10.01", "raw": 17000500},
        )
        tl = TL.TurnLoop(g, poll=0.01)
        fake_base = {"date": "1939.10.01", "date_raw": 17000500, "peace_open": True}
        with mock.patch.object(TL, "snapshot", return_value=fake_base), \
             mock.patch.object(TL.TurnLoop, "_force_pause_safe", return_value=True) as fp:
            res = tl.wait(max_days=7, respect_red_dots=False, timeout_seconds=5)
        self.assertTrue(fp.called)
        self.assertTrue(res["blocked_pre_wait"])
        self.assertTrue(res["peace_open"])
        self.assertTrue(res["clock_stalled"])
        self.assertEqual(res["wake_kind"], "peace_conference")
        self.assertEqual(res["stall_reason"], "peace_conference_open")
        self.assertEqual(res["waited_game_days"], 0.0)

    def test_wait_and_brief_forwards_peace_and_stall_fields(self):
        import turnengine as TE
        import turnloop as TL

        fake_wait = {
            "woke_because": ["peace conference already open"],
            "wake_kind": "peace_conference",
            "blocked_pre_wait": True,
            "peace_open": True,
            "clock_stalled": True,
            "stall_reason": "peace_conference_open",
            "waited_game_days": 0.0,
            "waited_real_seconds": 0.0,
            "paused": True,
        }
        fake_brief = {"ok": True, "markdown": "# brief", "approx_tokens": 10}
        with mock.patch.object(TL.TurnLoop, "wait", return_value=fake_wait), \
             mock.patch.object(TE, "build_turn_brief", return_value=fake_brief):
            out = TE.wait_and_brief(types.SimpleNamespace(), max_days=7, timeout_seconds=30)
        wb = out["wait"]
        self.assertEqual(wb["wake_kind"], "peace_conference")
        self.assertTrue(wb["blocked_pre_wait"])
        self.assertTrue(wb["peace_open"])
        self.assertTrue(wb["clock_stalled"])
        self.assertEqual(wb["stall_reason"], "peace_conference_open")


class TestA4InjectionBreakdown(unittest.TestCase):
    """A4: winject 调用方统计、Proc.injections 与 hoi4_status 透出。"""

    def test_winject_call_breakdown_and_reset(self):
        from process import winject as WJ

        pid = 998877
        WJ.reset_call_count(pid)
        try:
            WJ._CALL_COUNTS[pid] = 5
            WJ._CALL_BREAKDOWN[pid] = {"armyops_win.create_army": 3, "peacewin.finish": 2}
            bd = WJ.call_breakdown(pid, top=1)
            self.assertEqual(bd, {"armyops_win.create_army": 3})
            bd_all = WJ.call_breakdown(pid, top=12)
            self.assertEqual(list(bd_all.items()), [("armyops_win.create_army", 3), ("peacewin.finish", 2)])
            self.assertIsInstance(WJ._infer_caller(), str)
        finally:
            WJ.reset_call_count(pid)
        self.assertEqual(WJ.call_count(pid), 0)
        self.assertEqual(WJ.call_breakdown(pid), {})

    def test_proc_injections_alias_and_status_fields(self):
        import mem
        from process import winject as WJ

        p = mem.Proc.__new__(mem.Proc)
        p.pid = 776655
        p.base = 0x7FF600000000
        p.be = types.SimpleNamespace(call_count=2)
        WJ.reset_call_count(p.pid)
        try:
            WJ._CALL_COUNTS[p.pid] = 4
            WJ._CALL_BREAKDOWN[p.pid] = {"turnengine._dispatch_action": 4}
            self.assertEqual(p.injections, 4)
            self.assertEqual(p.injection_breakdown(), {"turnengine._dispatch_action": 4})

            import server as _mcp_srv  # noqa: F401
            import tools.core as core_tools
            fake_g = types.SimpleNamespace(
                p=p,
                date=lambda: {"text": "1936.01.01", "raw": 16970000},
                player_tag=lambda: "GER",
                tags=lambda: ["GER", "FRA"],
                game_speed=lambda: {"speed": 2, "ui_speed": 3},
            )
            with mock.patch.object(core_tools, "game", return_value=fake_g), \
                 mock.patch.object(core_tools, "_srv", return_value=types.SimpleNamespace(_WINJECT_RECOVERY=None)):
                st = core_tools._status({})
            self.assertEqual(st["injections"], 4)
            self.assertEqual(st["injections_scope"], "current_python_process")
            self.assertEqual(st["injections_by_caller"], {"turnengine._dispatch_action": 4})
        finally:
            WJ.reset_call_count(p.pid)


class TestA1StatsAirSnapAndTimings(unittest.TestCase):
    """A1: stats 缓存、_air_snapshot 静态时钟延寿、简报与宣战预检 timings_ms。"""

    def test_stats_build_in_process_cache(self):
        import safe_cache
        import stats

        old_cache = stats._STATS_CACHE
        stats._STATS_CACHE = None
        try:
            with mock.patch.object(safe_cache, "exists", return_value=True), \
                 mock.patch.object(safe_cache, "load", return_value={"has_war": (0x1000, "CHasWarTrigger")}) as mload:
                r1 = stats.build()
                r2 = stats.build()
            self.assertEqual(r1, r2)
            self.assertEqual(mload.call_count, 1)
            self.assertFalse(hasattr(stats, "_dis"))
        finally:
            stats._STATS_CACHE = old_cache

    def test_air_snapshot_extends_ttl_on_static_clock(self):
        import airops_win as AW
        from domains import overview as OV

        old_snap = dict(OV._AIR_SNAP)
        OV._AIR_SNAP.clear()
        try:
            class _FakeOv(OV.OverviewMixin):
                p = types.SimpleNamespace(pid=3344)

                def date(self):
                    return {"raw": 17001234, "text": "1938.01.01"}

            inst = _FakeOv()
            with mock.patch.object(AW, "german_wings", return_value={"wings": [{"id": 1}]}) as gw:
                rows1 = inst._air_snapshot(max_age=25.0)
                # 模拟已过去 60 秒但 date_raw 没变 -> 命中 120s 静态时钟 TTL
                OV._AIR_SNAP["ts"] -= 60.0
                rows2 = inst._air_snapshot(max_age=25.0)
            self.assertEqual(rows1, [{"id": 1}])
            self.assertEqual(rows2, [{"id": 1}])
            self.assertEqual(gw.call_count, 1)
        finally:
            OV._AIR_SNAP.clear()
            OV._AIR_SNAP.update(old_snap)

    def test_build_turn_brief_and_war_readiness_emit_timings_ms(self):
        import turnengine as TE
        from domains.battleplan_orders import BattleplanOrdersMixin

        class _FakeG(BattleplanOrdersMixin):
            GER_SNOWBALL = ("GER_anschluss", "GER_demand_sudetenland")
            p = types.SimpleNamespace(pid=5555)

            def _c(self, tag=None):
                return types.SimpleNamespace(tag=(tag or "GER").upper())

            def overview(self, tag=None):
                return {
                    "date": "1936.01.01",
                    "country": {"tag": "GER", "political_power": 100.0},
                    "factories": {"civ_total": 30, "mil_total": 28, "mil_free": 0, "dock_total": 10},
                    "army_summary": {"total_divisions": 30, "unassigned_divisions": 0},
                    "focus": {"current": "GER_rhineland"},
                    "research": [{"slot": 0, "slot_busy": True}],
                    "construction": [],
                    "production": [],
                    "candidates": {"available_decisions": []},
                    "alerts": [],
                }

            def date(self):
                return {"text": "1936.01.01", "raw": 16970000, "year": 1936}

            def player_tag(self):
                return "GER"

            def divisions(self, tag=None, limit=1, deep=False):
                return {"count": 30 if (tag or "GER") == "GER" else 10, "verified": True}

            def equipment_stockpile(self, tag=None):
                return {"stockpile": [{"equipment": "infantry_equipment_1", "count": 5000}]}

            def completed_focuses(self, tag=None):
                return []

        fg = _FakeG()
        br = TE.build_turn_brief(fg, include_data=True, include_focus_candidates=False, include_research_candidates=False)
        self.assertIn("timings_ms", br)
        self.assertIn("total", br["timings_ms"])
        self.assertIn("overview", br["timings_ms"])
        self.assertEqual(br["timings_ms"], br["brief"]["meta"]["timings_ms"])

        wr = fg.war_readiness_check("HOL", hard=False)
        self.assertIn("timings_ms", wr)
        self.assertIn("divisions", wr["timings_ms"])
        self.assertIn("diplomacy", wr["timings_ms"])
        self.assertIn("total", wr["timings_ms"])


class TestA5AllCountriesDivisionsAndScope(unittest.TestCase):
    """A5: divenum.all_countries_divisions 单趟汇总与 divisions() scope 字段。"""

    def test_all_countries_divisions_single_pass(self):
        import divenum as DE

        class _FakeProc:
            def __init__(self, counts):
                self._counts = counts

            def i32(self, addr):
                return self._counts[addr]

        base_ger = 0x100000
        base_fra = 0x200000
        base_lux = 0x300000
        off = DE.CO_ARMIES + DE.PDX_SIZE
        fp = _FakeProc({base_ger + off: 45, base_fra + off: 60, base_lux + off: 0})
        fg = types.SimpleNamespace(p=fp, _tags=["---", "GER", "FRA", "LUX"])
        with mock.patch.object(DE, "_index_by_tag", return_value=({1: 0, 2: 1, 3: 2}, [base_ger, base_fra, base_lux])):
            res = DE.all_countries_divisions(fg)
            res_nz = DE.all_countries_divisions(fg, non_zero_only=True)
        self.assertEqual(res["total_divisions"], 105)
        self.assertEqual(res["countries_count"], 3)
        self.assertEqual(res["by_tag"], {"FRA": 60, "GER": 45, "LUX": 0})
        self.assertEqual(res["top"][0], {"tag": "FRA", "count": 60})
        self.assertEqual(res_nz["by_tag"], {"FRA": 60, "GER": 45})


class TestA10ClockReceiptAndUnpausedForDrain(unittest.TestCase):
    """A10: housekeep() 与 execute_plan() 时钟与暂停状态回执（含不误报回归）。"""

    def test_execute_plan_unpaused_for_drain_not_false_positive_when_already_running(self):
        import session as SESS
        import turnengine as TE

        dates = iter([
            {"text": "1936.01.01", "raw": 16970000},
            {"text": "1936.01.02", "raw": 16970024},
        ])
        fg = types.SimpleNamespace(
            p=types.SimpleNamespace(pid=1234),
            date=lambda: next(dates),
            player_tag=lambda: "GER",
        )
        with mock.patch.object(TE, "_version_guard", return_value=(True, "")), \
             mock.patch.object(TE, "_process_alive", return_value=True), \
             mock.patch.object(TE, "_window_guard", return_value=(True, "", {})), \
             mock.patch.object(TE, "_dispatch_action", return_value={"ok": True}), \
             mock.patch.object(SESS, "is_paused", side_effect=[False, False]):
            r_running = TE.execute_plan(fg, {"actions": [{"op": "pause", "args": {}}]}, persist_state=False)
        self.assertEqual(r_running["days_elapsed"], 1.0)
        self.assertFalse(r_running["paused_before"])
        self.assertFalse(r_running["paused_after"])
        self.assertFalse(r_running["unpaused_for_drain"])

        dates2 = iter([
            {"text": "1936.01.01", "raw": 16970000},
            {"text": "1936.01.01", "raw": 16970002},
        ])
        fg2 = types.SimpleNamespace(
            p=types.SimpleNamespace(pid=1234),
            date=lambda: next(dates2),
            player_tag=lambda: "GER",
        )
        with mock.patch.object(TE, "_version_guard", return_value=(True, "")), \
             mock.patch.object(TE, "_process_alive", return_value=True), \
             mock.patch.object(TE, "_window_guard", return_value=(True, "", {})), \
             mock.patch.object(TE, "_dispatch_action", return_value={"ok": True}), \
             mock.patch.object(SESS, "is_paused", side_effect=[True, True]):
            r_drained = TE.execute_plan(fg2, {"actions": [{"op": "pause", "args": {}}]}, persist_state=False)
        self.assertEqual(r_drained["clock_advanced_hours"], 2)
        self.assertTrue(r_drained["paused_before"])
        self.assertTrue(r_drained["paused_after"])
        self.assertTrue(r_drained["unpaused_for_drain"])


if __name__ == "__main__":
    unittest.main()
