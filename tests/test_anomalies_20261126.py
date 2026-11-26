#!/usr/bin/env python3
"""Regression tests for all A1-A11 anomalies from HOI4 anomaly report (2026-11-26)."""
from __future__ import annotations

import os
import sys
import tempfile
import types
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CALLING = os.path.join(ROOT, "calling")
MCP_DIR = os.path.join(ROOT, "mcp")
for _p in (CALLING, MCP_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)


class _DummyProc:
    def __init__(self, pid=4321, base=0x140000000):
        self.pid = pid
        self.base = base
        self.i32_map = {}
        self.u64_map = {}
        self.be = types.SimpleNamespace(call_count=0)

    def i32(self, addr):
        return self.i32_map.get(addr, 0)

    def u32(self, addr):
        return self.i32_map.get(addr, 0)

    def u64(self, addr):
        return self.u64_map.get(addr, 0)

    def u8(self, addr):
        return 0

    def read(self, addr, n):
        return b"\x00" * n

    def _inj_count(self):
        return 0


class TestAnomalies20261126(unittest.TestCase):
    # ------------------------------------------------------------------ A1 + A6
    def test_a1_stats_fastpath_stats_cache_overview_dedup_and_brief_timings(self):
        import api
        import stats
        import turnengine
        from domains.overview import OverviewMixin
        from domains.stats_vars import StatsVarsMixin

        # 1) stats.build caches repeated loads
        old_cache = stats._STATS_CACHE
        try:
            stats._STATS_CACHE = None
            calls = []

            def fake_load(path):
                calls.append(path)
                return {"a": (0x1234, "CFooTrigger")}

            with mock.patch("safe_cache.exists", return_value=True), \
                    mock.patch("safe_cache.load", side_effect=fake_load):
                r1 = stats.build()
                r2 = stats.build()
            self.assertEqual(r1, r2)
            self.assertEqual(len(calls), 1)
        finally:
            stats._STATS_CACHE = old_cache

        # 2) country_stats skips _st.build & __enter__ when SCOPE_CALL_ALLOW is empty
        class _StatsHost(StatsVarsMixin):
            def __init__(self):
                self.p = _DummyProc()
                self.gamestate = 0
                self.entered = 0

            def __enter__(self):
                self.entered += 1
                return self

            def __exit__(self, *a):
                return False

            def _c(self, tag=None):
                return types.SimpleNamespace(
                    tag="GER", ptr=0x1000,
                    summary=lambda: {"tag": "GER", "political_power": 50},
                )

            def wars(self, tag=None):
                return {"at_war": False}

        old_allow = api.SCOPE_CALL_ALLOW
        try:
            api.SCOPE_CALL_ALLOW = set()
            sh = _StatsHost()
            with mock.patch("stats.build", side_effect=AssertionError("should not build")):
                cs = sh.country_stats("GER")
            self.assertEqual(sh.entered, 0)
            self.assertEqual(cs.get("tag"), "GER")
            self.assertIn("unavailable", cs)
        finally:
            api.SCOPE_CALL_ALLOW = old_allow

        # 3) overview deduplicates construction_queue, research_slots, unassigned_divisions
        class _OvHost(OverviewMixin):
            def __init__(self):
                self.p = _DummyProc()
                self.counts = {}
                self.production = None

            def _c(self, tag=None):
                return types.SimpleNamespace(
                    tag="GER",
                    ptr=0x1000,
                    summary=lambda: {
                        "tag": "GER",
                        "civilian_factories": 30,
                        "military_factories": 28,
                        "naval_dockyards": 10,
                        "political_power": 120,
                        "fuel": 5000.0,
                        "experience": {"army": 10.0, "navy": 5.0, "air": 5.0},
                    },
                )

            def current_focus(self, tag=None):
                return {"key": "GER_rhineland", "name": "Rhineland"}

            def production_lines(self, tag=None, include_obsolete=True):
                return [{"index": 0, "equipment": "infantry_equipment_1", "factories": 5, "class": "CMilitaryProductionLine"}]

            def construction_queue(self, tag=None):
                self.counts["cons"] = self.counts.get("cons", 0) + 1
                return [{"state": 64, "factories": 15}]

            def unassigned_divisions(self, tag=None):
                self.counts["unassigned"] = self.counts.get("unassigned", 0) + 1
                return []

            def research_slots(self, tag=None):
                self.counts["research"] = self.counts.get("research", 0) + 1
                return [{"slot": 0, "slot_busy": True}]

            def factory_allocation(self, tag=None):
                return {
                    "source": "engine_production_status",
                    "military_available": 24,
                    "military_used": 20,
                    "military_idle": 4,
                    "naval_available": 8,
                    "naval_used": 8,
                    "naval_idle": 0,
                }

            def decisions(self, tag=None, which="visible", rich=False):
                return []

            def focus_tree(self, tag=None):
                return []

            def politics(self, tag=None):
                return {}

            def states(self, tag=None):
                return []

            def date(self):
                return {"text": "1936.02.01", "raw": 1000, "year": 1936}

            def equipment_stockpile(self, tag=None):
                return {"totals_by_equipment": {"infantry_equipment_1": 1000}}

            def divisions(self, tag=None, limit=1, deep=False):
                return {"count": 30, "divisions": []}

            def wars(self, tag=None):
                return {"at_war": False, "enemies": []}

            def _active_variant_names(self, tag=None):
                return []

            def _air_snapshot(self, max_age=25.0):
                return []

            def _navy_snapshot(self):
                return []

        oh = _OvHost()
        with mock.patch("macros.strategic_health_board", return_value={"ok": True, "alerts": []}):
            ov = oh.overview("GER")
        self.assertEqual(oh.counts["cons"], 1)
        self.assertEqual(oh.counts["research"], 1)
        self.assertEqual(oh.counts["unassigned"], 1)
        self.assertEqual(ov["factories"]["mil_free"], 4)

        # 4) build_turn_brief reports timings_ms and passes prefetched
        oh.player_tag = lambda: "GER"
        oh.overview = lambda tag=None: ov
        oh._c = lambda tag=None: types.SimpleNamespace(ptr=0x1000)
        seen_pf = {}

        def fake_eval(g, fast=True, prefetched=None):
            seen_pf["rd"] = prefetched
            return {"ok": True, "blocking": [], "acked": [], "advisory": [], "has_blocking": False}

        def fake_hb(g, tag=None, prefetched=None):
            seen_pf["hb"] = prefetched
            return {"ok": True, "alerts": []}

        with mock.patch("reddots.evaluate", side_effect=fake_eval), \
                mock.patch("macros.strategic_health_board", side_effect=fake_hb), \
                mock.patch("session.is_paused", return_value=True), \
                mock.patch("events_win.open_events", return_value={"events": []}), \
                mock.patch("combat.enemy_tags", return_value=[]), \
                mock.patch("diplomacy.country_wargoals_fast", return_value=([], True)), \
                mock.patch("diplomacy.justifying_fast", return_value=[]), \
                mock.patch("autopilot.status", return_value={"active": False}):
            br = turnengine.build_turn_brief(oh, tag="GER", include_data=True)
        self.assertIn("timings_ms", br)
        self.assertIn("overview", br["timings_ms"])
        self.assertIn("total", br["timings_ms"])
        self.assertEqual(br["brief"]["meta"]["timings_ms"], br["timings_ms"])
        self.assertIsNotNone(seen_pf.get("rd"))
        self.assertIsNotNone(seen_pf.get("hb"))

    def test_a6_mcp_server_clears_call_state_and_stale_prev(self):
        import server as srv

        old_call = dict(srv._CALL)
        try:
            srv._CALL.clear()
            res = srv._run_with_timeout(lambda a: {"ok": True, "val": a["x"]}, {"x": 7}, "hoi4_test_ok")
            self.assertEqual(res, {"ok": True, "val": 7})
            self.assertEqual(srv._CALL, {})

            # Dead thread left in _CALL should be cleared rather than blocking as busy
            import threading
            dead_th = threading.Thread(target=lambda: None)
            dead_th.start()
            dead_th.join()
            srv._CALL.update(thread=dead_th, name="stale", started=0.0)
            res2 = srv._run_with_timeout(lambda a: {"ok": True}, {}, "hoi4_test_after_stale")
            self.assertTrue(res2["ok"])
            self.assertEqual(srv._CALL, {})
        finally:
            srv._CALL.clear()
            srv._CALL.update(old_call)

    # ------------------------------------------------------------------ A2
    def test_a2_justifying_default_no_deep_ctrl_map_cache_and_war_readiness(self):
        import diplomacy
        import mapdata
        import session_state as ss
        from domains.battleplan_orders import BattleplanOrdersMixin

        class _FakeOI:
            def __init__(self):
                self.deep_calls = 0
                self.live_calls = 0

            def instances(self, cls):
                return []

            def live_instances(self, cls):
                self.live_calls += 1
                return []

            def deep_instances(self, cls):
                self.deep_calls += 1
                return []

        oi = _FakeOI()
        g = types.SimpleNamespace(
            p=_DummyProc(),
            oi=oi,
            _oi=oi,
            _tags=["---", "GER", "HOL"],
            _tag2idx={0: 0, 1: 1, 2: 2},
            _load_tags=lambda: None,
            tags=lambda: ["---", "GER", "HOL"],
            country_ptrs=lambda: [0, 0x1000, 0x2000],
            date=lambda: {"raw": 1000, "year": 1936, "text": "1936.02.01"},
        )
        diplomacy.justifying(g)
        self.assertEqual(oi.deep_calls, 0)
        diplomacy.justifying(g, deep=True)
        self.assertGreaterEqual(oi.live_calls + oi.deep_calls, 1)

        # province_controller_map cache & SessionState invalidation
        mapdata._CTRL_MAP_CACHE.clear()
        win_calls = []
        with mock.patch("mapdata._win_controller_map", side_effect=lambda _g, d=None: (win_calls.append(1) or {10: "GER"})), \
                mock.patch("sys.platform", "win32"):
            m1 = mapdata.province_controller_map(g)
            m2 = mapdata.province_controller_map(g)
            self.assertEqual(m1, {10: "GER"})
            self.assertEqual(m2, {10: "GER"})
            self.assertEqual(len(win_calls), 1)
            ss.SESSION.invalidate("test_a2")
            m3 = mapdata.province_controller_map(g)
            self.assertEqual(len(win_calls), 2)
            self.assertEqual(m3, {10: "GER"})
        mapdata._CTRL_MAP_CACHE.clear()

        # war_readiness_check passes limit=1, deep=False to target divisions & returns timings_ms
        class _WRHost(BattleplanOrdersMixin):
            GER_SNOWBALL = ("GER_rhineland", "GER_anschluss")

            def __init__(self):
                self.p = _DummyProc()
                self.div_calls = []

            def _c(self, tag=None):
                return types.SimpleNamespace(tag="GER", ptr=0x1000)

            def divisions(self, tag=None, limit=200, deep=False):
                self.div_calls.append((tag, limit, deep))
                return {"count": 20, "source": "CCountry+0x290", "verified": True}

            def equipment_stockpile(self, tag=None):
                return {"stockpile": [{"equipment": "infantry_equipment_1", "count": 5000}]}

            def completed_focuses(self, tag=None):
                return ["GER_rhineland", "GER_anschluss"]

            def date(self):
                return {"year": 1939, "text": "1939.01.01", "raw": 2000}

            def _border_divisions(self, target_tag, tag=None, owner_by_id=None):
                return {"own_tag": "GER", "enemy_tag": "HOL", "border_provinces": 4,
                        "divisions_on_border": 10, "divisions_count": 20}

            def _front_plan_check(self, target_tag, tag=None):
                return {"ok": True}

            def _front_directive_check(self, target_tag):
                return {"ok": True}

        wr = _WRHost()
        with mock.patch("diplomacy.war_escalation_forecast", return_value={"diplomatic_blockers": [], "diplomatic_warnings": []}), \
                mock.patch("diplomacy.country_wargoals_direct", return_value=([{"target_tag": "HOL"}], True)), \
                mock.patch("diplomacy.justifying", return_value=[]):
            wres = wr.war_readiness_check("HOL", tag="GER")
        self.assertIn(("HOL", 1, False), wr.div_calls)
        self.assertIn("timings_ms", wres)
        self.assertIn("total", wres["timings_ms"])

    # ------------------------------------------------------------------ A3
    def test_a3_advisors_cache_and_politics_hoists_legality_state(self):
        import advisors
        from domains.politics import PoliticsMixin

        advisors.clear_caches()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                cl_dir = os.path.join(tmp, "common", "country_leader")
                ch_dir = os.path.join(tmp, "common", "characters")
                os.makedirs(cl_dir, exist_ok=True)
                os.makedirs(ch_dir, exist_ok=True)
                with open(os.path.join(cl_dir, "00_traits.txt"), "w", encoding="utf-8") as fh:
                    fh.write("@xp_med = 0.25\nleader_traits = {\n\tarmy_chief_test = {\n\t\texperience_gain_army = @xp_med\n\t}\n}\n")
                with open(os.path.join(ch_dir, "GER.txt"), "w", encoding="utf-8") as fh:
                    fh.write("characters = {\n\tGER_test_adv = {\n\t\tadvisor = {\n\t\t\tslot = political_advisor\n\t\t\tidea_token = GER_test_adv_tok\n\t\t\ttraits = { army_chief_test }\n\t\t}\n\t}\n}\n")
                rows1 = advisors.load_advisors("GER", game_dir=tmp)
                self.assertEqual(len(rows1), 1)
                self.assertEqual(rows1[0]["army_xp_per_day"], 0.25)
                # Mutating nothing and removing files proves 2nd call hits cache
                os.remove(os.path.join(ch_dir, "GER.txt"))
                os.remove(os.path.join(cl_dir, "00_traits.txt"))
                rows2 = advisors.load_advisors("GER", game_dir=tmp)
                self.assertEqual(rows1, rows2)
        finally:
            advisors.clear_caches()

        # PoliticsMixin.idea_catalog hoists legality._state outside advisor loop
        class _PolHost(PoliticsMixin):
            def _c(self, tag=None):
                return types.SimpleNamespace(tag="GER", ptr=0x1000)

            def ideas(self, tag=None):
                return {}

        ph = _PolHost()
        fake_advs = [
            {"character": "ADV_%d" % i, "idea_token": "TOK_%d" % i,
             "slot": "political_advisor", "cost": 150,
             "available_block": "has_government = fascism", "visible_block": None,
             "traits": []}
            for i in range(8)
        ]
        state_calls = []

        def fake_state(g, tag=None):
            state_calls.append(tag)
            return {"government": "fascism"}

        with mock.patch("advisors.load_ideas_catalog", return_value={}), \
                mock.patch("advisors.load_advisors", return_value=fake_advs), \
                mock.patch("legality._state", side_effect=fake_state), \
                mock.patch("legality._eval", return_value=True):
            cat = ph.idea_catalog(tag="GER")
        self.assertEqual(cat["total"], 8)
        self.assertEqual(len(state_calls), 1)

    # ------------------------------------------------------------------ A4
    def test_a4_winject_breakdown_mem_and_hoi4_status(self):
        import mem
        from process import winject
        from tools import core as mcp_core

        pid_test = 987654
        winject.reset_call_count(pid_test)
        try:
            winject._CALL_COUNTS[pid_test] = 5
            winject._CALL_BREAKDOWN[pid_test] = {"cmdwin.send": 3, "turnloop.tick": 2}
            bd = winject.call_breakdown(pid_test, top=12)
            self.assertEqual(list(bd.keys()), ["cmdwin.send", "turnloop.tick"])
            self.assertEqual(bd["cmdwin.send"], 3)

            real_p = mem.Proc.__new__(mem.Proc)
            real_p.pid = pid_test
            real_p.be = types.SimpleNamespace(call_count=2)
            with mock.patch("sys.platform", "win32"):
                self.assertEqual(real_p.injections, 5)
                self.assertEqual(real_p.injection_breakdown(top=12), {"cmdwin.send": 3, "turnloop.tick": 2})

            dummy_g = types.SimpleNamespace(
                p=types.SimpleNamespace(
                    pid=pid_test,
                    base=0x140000000,
                    call_count=5,
                    injections=5,
                    injection_breakdown=lambda top=12: winject.call_breakdown(pid_test, top=top),
                ),
                date=lambda: {"text": "1936.01.01"},
                player_tag=lambda: "GER",
                tags=lambda: ["---", "GER"],
                game_speed=lambda: {"speed": 2, "ui_speed": 3},
            )
            fake_srv = types.SimpleNamespace(_WINJECT_RECOVERY=None)
            with mock.patch.object(mcp_core, "game", return_value=dummy_g), \
                    mock.patch.object(mcp_core, "_srv", return_value=fake_srv), \
                    mock.patch("session.is_paused", return_value=True), \
                    mock.patch("windowstate.state", return_value={"focused": True}):
                st = mcp_core._status({})
            self.assertEqual(st["injections"], 5)
            self.assertEqual(st["injections_scope"], "current_python_process")
            self.assertEqual(st["injections_by_caller"], {"cmdwin.send": 3, "turnloop.tick": 2})
        finally:
            winject.reset_call_count(pid_test)

    # ------------------------------------------------------------------ A5
    def test_a5_divisions_tag_scope_and_all_countries_divisions(self):
        import divenum
        from domains.military_land import MilitaryLandMixin

        class _DivHost(MilitaryLandMixin):
            def __init__(self):
                self.p = _DummyProc()

            def _c(self, tag=None):
                t = "GER" if tag is None else str(tag).upper()
                return types.SimpleNamespace(tag=t, ptr=0x1000 if t == "GER" else 0x2000)

        dh = _DivHost()
        with mock.patch("divenum.armies", side_effect=lambda g, tag: {"ptr": 0x1000, "tid": 1, "count": 30, "armies": [], "source": "CCountry+0x290", "verified": True}):
            d_pl = dh.divisions()
            d_co = dh.divisions("FRA")
        self.assertEqual((d_pl["tag"], d_pl["scope"]), ("GER", "player"))
        self.assertEqual((d_co["tag"], d_co["scope"]), ("FRA", "country"))

        # all_countries_divisions single-pass check
        p = _DummyProc()
        # Country ptrs at 0x10000 (GER, tid=1, 30 divs) and 0x20000 (FRA, tid=2, 50 divs)
        p.i32_map[0x10000 + divenum.CO_TAG_ID] = 1
        p.i32_map[0x10000 + divenum.CO_ARMIES + divenum.PDX_SIZE] = 30
        p.i32_map[0x20000 + divenum.CO_TAG_ID] = 2
        p.i32_map[0x20000 + divenum.CO_ARMIES + divenum.PDX_SIZE] = 50
        idx_calls = []
        g_multi = types.SimpleNamespace(
            p=p,
            _tags=["---", "GER", "FRA", "POL"],
            _load_tags=lambda: None,
            tags=lambda: ["---", "GER", "FRA", "POL"],
            country_ptrs=lambda: (idx_calls.append(1) or [0x10000, 0x20000]),
        )
        res = divenum.all_countries_divisions(g_multi)
        self.assertEqual(len(idx_calls), 1)
        self.assertEqual(res["total_divisions"], 80)
        self.assertEqual(res["by_tag"], {"GER": 30, "FRA": 50})
        with self.assertRaises(KeyError) as cm:
            divenum.country(g_multi, "POL")
        self.assertIn("unformed/annexed/absent", str(cm.exception))

    # ------------------------------------------------------------------ A7
    def test_a7_wargoal_quote_baseline_and_repair_reason(self):
        import diplomacy
        from domains.diplomacy_war import DiplomacyWarMixin

        self.assertEqual(diplomacy.WARGOAL_PRODUCTION_TERM, 0.0)
        days = diplomacy._estimate_days(1, 0)
        self.assertEqual(days, 200.0)

        class _WgHost(DiplomacyWarMixin):
            def __init__(self):
                self.p = _DummyProc()

            def _c(self, tag=None):
                t = str(tag or "GER").upper()
                return types.SimpleNamespace(tag=t, tag_id=(10 if t == "POL" else 1), political_power=10.0, ptr=0x1000)

            def country(self, tag=None):
                return self._c(tag)

            def player(self):
                return self._c("GER")

            def states(self, tag=None):
                return [{"id": 85}]

            def date(self):
                return {"text": "1936.02.01", "raw": 1000}

            def justify_wargoal_status(self, tag=None):
                return []

            def wargoals(self, tag=None):
                return []

        wh = _WgHost()
        with mock.patch("diplomacy.justifying_fast", return_value=[]):
            q = wh.wargoal_quote("POL")
        self.assertEqual(q["quote_date"], "1936.02.01")
        self.assertEqual(q["estimated_days"], 200.0)
        self.assertEqual(q["estimated_upfront_pp"], 40.0)
        self.assertIsNone(q["required_pp"])

        with mock.patch("diplomacy.justifying_fast", return_value=[]):
            ref = wh.justify_wargoal("POL", dry_run=False, force=False)
        self.assertFalse(ref["ok"])
        self.assertTrue(ref.get("required_pp_is_estimate"))

        wh._settle = lambda s=0: None
        wh.resolve = lambda what, t=None: 0x5555
        wh.ensure_wargoal_state_array = lambda r, sid: {"ok": True, "action": "repaired", "state": sid}
        wh._pick_target_state_id = lambda t: 85
        seen_deep = []

        def fake_justifying(g, deep=False):
            seen_deep.append(deep)
            if deep:
                return [{"target": "POL", "ptr": "0x7000", "source_class": "CTimedWargoalActivity", "states_in_activity": 0}]
            return []

        with mock.patch("diplomacy.justifying_fast", return_value=[]), \
                mock.patch("diplomacy.justifying", side_effect=fake_justifying), \
                mock.patch("diplomacy.track_activity_ptr"), \
                mock.patch("cmdwin.justify_wargoal", return_value={"ok": True, "posted": True, "act": 0x7000, "cmd": 0x8000}):
            res_ok = wh.justify_wargoal("POL", dry_run=False, force=True)
        self.assertTrue(res_ok["ok"])
        self.assertIn(True, seen_deep)
        self.assertIn("empty states array", res_ok["wargoal_state_repaired"]["reason"])
        self.assertIn("0xB2E62E", res_ok["wargoal_state_repaired"]["reason"])

    # ------------------------------------------------------------------ A8
    def test_a8_surplus_ratio_and_rebalance_reconciliation(self):
        import equipmeta
        import macros

        lines = [
            {"index": 0, "equipment": "infantry_equipment_1", "factories": 18, "class": "CMilitaryProductionLine"},
            {"index": 1, "equipment": "artillery_equipment_1", "factories": 2, "class": "CMilitaryProductionLine"},
        ]
        stock = {"infantry_equipment_1": 25000.0, "artillery_equipment_1": 100.0}
        surplus = equipmeta.compute_surplus_lines(lines, stock, 28, 30)
        self.assertTrue(surplus)
        self.assertEqual(surplus[0]["surplus_ratio"], surplus[0]["ratio"])
        self.assertGreater(surplus[0]["surplus_ratio"], 2.0)

        fake_g = types.SimpleNamespace(
            player_tag=lambda: "GER",
            _c=lambda tag=None: types.SimpleNamespace(tag="GER"),
            country=lambda tag=None: types.SimpleNamespace(
                summary=lambda: {"military_factories": 28}
            ),
            equipment_stockpile=lambda tag=None: {"totals_by_equipment": stock},
            production_lines=lambda tag=None, include_obsolete=False: lines,
            divisions=lambda tag=None, limit=1, deep=False: {"count": 30},
            available_equipment_variants=lambda tag=None: [],
        )
        reb = macros.rebalance_production(fake_g, dry_run=True)
        self.assertTrue(reb["ok"])
        self.assertTrue(reb["rifle_surplus_detected"])
        self.assertGreaterEqual(reb["rifle_surplus_threshold"], 20000)
        self.assertTrue(reb["surplus_lines_detected"])
        self.assertEqual(reb["surplus_lines_detected"][0]["ratio"], surplus[0]["surplus_ratio"])

    # ------------------------------------------------------------------ A9 + A10
    def test_a9_a10_housekeep_and_execute_plan_telemetry(self):
        import turnengine
        from housekeep import HousekeepMixin

        class _HkHost(HousekeepMixin):
            def __init__(self):
                self.p = _DummyProc()
                self.day = 1

            def _c(self, tag=None):
                return types.SimpleNamespace(tag="GER", ptr=0x1000)

            def player(self):
                return types.SimpleNamespace(
                    tag="GER",
                    summary=lambda: {"military_factories": 32, "naval_dockyards": 10},
                )

            def country(self, tag=None):
                return self.player()

            def _upgrade_production_lines(self, tag=None):
                return []

            def _cap_deprioritized_lines(self, tag=None, rules=None):
                return []

            def production_lines(self, tag=None):
                return [{"index": 0, "equipment": "infantry_equipment_1", "factories": 28, "class": "CMilitaryProductionLine"}]

            def factory_allocation(self, tag=None):
                return {
                    "source": "engine_production_status",
                    "military_available": 28,
                    "military_used": 28,
                    "military_idle": 0,
                    "military_reserved": 4,
                    "naval_available": 8,
                    "naval_used": 8,
                    "naval_idle": 0,
                }

            def equipment_stockpile(self, tag=None):
                return {"totals_by_equipment": {}}

            def divisions(self, tag=None, limit=1):
                return {"count": 30}

            def _housekeep_dockyards(self, tag=None):
                return []

            def date(self):
                return {"text": "1936.02.0%d" % self.day, "raw": 1000 + (self.day - 1) * 24}

        hk = _HkHost()
        hp = hk.housekeep_production("GER")
        self.assertTrue(hp["ok"])
        self.assertEqual(hp["total"], 28)
        self.assertEqual(hp["raw_total"], 32)
        self.assertEqual(hp["military_reserved"], 4)
        self.assertEqual(hp["idle"], 0)

        # Idle military factory whose set_production_amount fails reports note
        hk.production_lines = lambda tag=None: [
            {"index": 0, "equipment": "infantry_equipment_1", "factories": 26, "class": "CMilitaryProductionLine"},
            {"index": 1, "equipment": "artillery_equipment_1", "factories": 2, "class": "CMilitaryProductionLine"},
        ]
        hk.factory_allocation = lambda tag=None: {
            "source": "engine_production_status",
            "military_available": 30,
            "military_used": 28,
            "military_idle": 2,
            "military_reserved": 2,
            "naval_available": 8,
            "naval_used": 8,
            "naval_idle": 0,
        }
        hk.set_production_amount = lambda idx, amt, tag=None: {"ok": False, "after": 2, "error": "queue_paused"}
        with mock.patch("session.is_paused", return_value=True):
            hp_fail = hk.housekeep_production("GER")
        self.assertFalse(hp_fail["ok"])
        self.assertIn("queue_paused", hp_fail.get("note", ""))

        # Full housekeep() clock telemetry when clock advances
        for m in ("housekeep_construction", "housekeep_production", "housekeep_doctrines",
                  "housekeep_research", "housekeep_politics", "housekeep_recruitment",
                  "housekeep_divisions", "housekeep_air", "housekeep_navy"):
            if m == "housekeep_construction":
                def _adv(*a, **k):
                    hk.day = 3
                    return {"ok": True}
                setattr(hk, m, _adv)
            elif m != "housekeep_production":
                setattr(hk, m, lambda *a, **k: {"ok": True})
        hk.housekeep_production = lambda *a, **k: {"ok": True}
        with mock.patch("session.is_paused", return_value=True):
            full_hk = hk.housekeep("GER")
        self.assertEqual(full_hk["days_elapsed"], 2.0)
        self.assertEqual(full_hk["date_before"], "1936.02.01")
        self.assertEqual(full_hk["date_after"], "1936.02.03")
        self.assertTrue(full_hk["unpaused_for_drain"])
        self.assertIn("clock_note", full_hk)

        # execute_plan clock/pause telemetry
        g_exec = types.SimpleNamespace(
            p=_DummyProc(),
            date=lambda: {"text": "1936.02.01", "raw": 1000},
            player_tag=lambda: "GER",
        )
        with mock.patch("turnengine._version_guard", return_value=(True, None)), \
                mock.patch("turnengine._process_alive", return_value=True), \
                mock.patch("turnengine._window_guard", return_value=(True, None, {"focused": True})), \
                mock.patch("turnengine._dispatch_action", return_value={"ok": True}), \
                mock.patch("turnengine.save_turn_state"), \
                mock.patch("turnengine.load_turn_state", return_value={}), \
                mock.patch("session.is_paused", return_value=True), \
                mock.patch("autopilot.is_active", return_value=False):
            ep = turnengine.execute_plan(
                g_exec,
                {"actions": [{"op": "set_focus", "args": {"key": "GER_rhineland"}}]},
                dry_run=False,
            )
        self.assertEqual(ep["date_before"], "1936.02.01")
        self.assertEqual(ep["days_elapsed"], 0.0)
        self.assertFalse(ep["unpaused_for_drain"])
        self.assertTrue(ep["paused_before"])

    # ------------------------------------------------------------------ A11
    def test_a11_peace_red_dot_prewait_and_stalled_clock_wakeup(self):
        import reddots
        import turnengine
        import turnloop

        self.assertIn("PEACE_CONFERENCE", reddots.STRICT_CODES)

        g_rd = types.SimpleNamespace(
            player_tag=lambda: "GER",
            player=lambda: types.SimpleNamespace(
                civilian_factories=0, military_factories=0, naval_dockyards=0,
                political_power=50.0, experience={}, fuel=500.0, manpower=100000.0,
            ),
            date=lambda: {"text": "1939.09.20", "raw": 5000},
        )
        with mock.patch("peacewin.live_conference_readonly", return_value=0xBEEF0000):
            ev = reddots.evaluate(
                g_rd,
                fast=True,
                prefetched={
                    "lines": [], "construction": [], "research_slots": [{"slot": 0, "slot_busy": True}],
                    "current_focus": {"key": "GER_danzig"}, "decisions_visible": [],
                    "unassigned": [], "stock_totals": {}, "enemies": [],
                },
            )
        codes = [x["code"] for x in ev["blocking"]]
        self.assertIn("PEACE_CONFERENCE", codes)

        # TurnLoop.wait immediate pre-wait return when peace_open=True
        g_tl = types.SimpleNamespace(
            date=lambda: {"text": "1939.09.20", "raw": 5000},
            game_speed=lambda: {"speed": 2, "ui_speed": 3},
            _pause_safe=lambda: True,
        )
        tl = turnloop.TurnLoop(g_tl, poll=0.01)
        tl._force_pause_safe = lambda g: True
        with mock.patch("turnloop.snapshot", return_value={"date": "1939.09.20", "date_raw": 5000, "peace_open": True}), \
                mock.patch("session.is_paused", return_value=False):
            pw = tl.wait(max_days=10, respect_red_dots=False)
        self.assertTrue(pw["blocked_pre_wait"])
        self.assertTrue(pw["peace_open"])
        self.assertTrue(pw["clock_stalled"])
        self.assertEqual(pw["wake_kind"], "peace_conference")
        self.assertEqual(pw["stall_reason"], "peace_conference_open")

        # TurnLoop.wait timeout at 0 days elapsed emits clock_stalled & stall_reason
        with mock.patch("turnloop.snapshot", return_value={"date": "1939.09.20", "date_raw": 5000, "peace_open": False}), \
                mock.patch("session.is_paused", return_value=True):
            to_res = tl.wait(max_days=10, timeout_seconds=0.02, respect_red_dots=False)
        self.assertTrue(to_res["timed_out"])
        self.assertTrue(to_res["clock_stalled"])
        self.assertEqual(to_res["stall_reason"], "game_paused")
        self.assertEqual(to_res["wake_kind"], "timeout_stalled")
        self.assertIn("real-time safety brake", to_res["woke_because"][0])

        # wait_and_brief propagates wake_kind / clock_stalled / stall_reason / peace_open
        with mock.patch("turnloop.TurnLoop.wait", return_value=pw), \
                mock.patch("turnengine.build_turn_brief", return_value={"markdown": "# ok", "approx_tokens": 2}):
            wb = turnengine.wait_and_brief(g_tl, max_days=7)
        self.assertEqual(wb["wait"]["wake_kind"], "peace_conference")
        self.assertTrue(wb["wait"]["clock_stalled"])
        self.assertEqual(wb["wait"]["stall_reason"], "peace_conference_open")
        self.assertTrue(wb["wait"]["peace_open"])
        self.assertTrue(wb["wait"]["blocked_pre_wait"])


if __name__ == "__main__":
    unittest.main()
