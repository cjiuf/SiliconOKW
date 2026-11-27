# -*- coding: utf-8 -*-
"""2026-11-27 第二批实测缺陷（D-17 ~ D-28）全覆盖离线单元测试。

全部 100% 离线、纯内存、0 进程依赖、0 引擎注入。
"""
import os
import struct
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CALLING = os.path.join(ROOT, "calling")
MCP_DIR = os.path.join(ROOT, "mcp")
for _p in (CALLING, MCP_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)


class _MemProc:
    """极简虚拟内存进程桩，用于精确测试 peacewin / economy / military_land。"""

    def __init__(self, base=0x7FF700000000, pid=99123):
        self.base = base
        self.pid = pid
        self.mem = {}
        self._alloc_cur = 0x18000000000

    def alloc(self, size):
        addr = self._alloc_cur
        self._alloc_cur += (int(size) + 0xFF) & ~0xFF
        self.write(addr, b"\x00" * int(size))
        return addr

    def write(self, addr, data):
        for i, b in enumerate(data):
            self.mem[int(addr) + i] = b

    def read(self, addr, n):
        return bytes(self.mem.get(int(addr) + i, 0) for i in range(int(n)))

    def try_read(self, addr, n):
        return self.read(addr, n)

    def u8(self, addr):
        return self.read(addr, 1)[0]

    def i32(self, addr):
        return struct.unpack("<i", self.read(addr, 4))[0]

    def u32(self, addr):
        return struct.unpack("<I", self.read(addr, 4))[0]

    def i64(self, addr):
        return struct.unpack("<q", self.read(addr, 8))[0]

    def u64(self, addr):
        return struct.unpack("<Q", self.read(addr, 8))[0]

    def stdstring(self, addr):
        ln, cap = struct.unpack("<QQ", self.read(addr + 0x10, 16))
        if ln == 0 or ln > 256 or cap < ln:
            return ""
        if cap >= 16:
            ptr = self.u64(addr)
            return self.read(ptr, ln).decode("utf-8", "replace")
        return self.read(addr, ln).decode("utf-8", "replace")


def _make_peace_fixture():
    import peacewin as PW

    p = _MemProc()
    conf = p.alloc(0x300)
    head = p.alloc(0x40)
    p.write(conf + PW.CONF_SUBMIT_MAP, struct.pack("<QQ", head, 0))
    p.write(conf + 0x1C8, struct.pack("<QQ", p.alloc(0x40), 0))

    def _mk_act(lidx, sid, cost=50):
        a = p.alloc(0x60)
        p.write(a + 8, struct.pack("<i", PW.ACT_TYPE_TAKE_STATE))
        p.write(a + 0x30, struct.pack("<iii", 1, lidx, 1))
        p.write(a + PW.ACT_COST, struct.pack("<i", cost))
        p.write(a + 0x48, struct.pack("<i", sid))
        return a

    hol_acts = {sid: _mk_act(10, sid, 40) for sid in (7, 35, 36, 309, 695)}
    ins_acts = {sid: _mk_act(20, sid, 30) for sid in (334, 335, 672)}

    class _Co:
        def __init__(self, tid):
            self.tag_id = tid
            self.ptr = 0

    class _FakeGame:
        def __init__(self):
            self.p = p
            self.owned = {64, 51}
            self.owners = {s: "HOL" for s in hol_acts}
            self.owners.update({s: "INS" for s in ins_acts})
            self._tags = {"GER": _Co(1), "HOL": _Co(10), "INS": _Co(20)}

        def tags(self):
            return list(self._tags.keys())

        def _c(self, tag=None):
            return self._tags[str(tag or "GER").upper()]

        def country(self, tag):
            return self._c(tag)

        def player_tag(self):
            return "GER"

        def player_tag_id(self):
            return 1

        def states(self, tag=None):
            return [{"id": sid} for sid in sorted(self.owned)]

        def state_detail(self, sid):
            return {"owner": self.owners.get(int(sid), "HOL"), "name": "State_%d" % sid}

        def date(self):
            return {"text": "1936.11.02.01", "raw": 16966681}

    return p, conf, _FakeGame(), hol_acts, ins_acts


class TestD17D18D19PeaceConference(unittest.TestCase):
    """D-17 (kinds 校验)、D-18 (0 索赔白和平拦截与 at_war_after)、D-19 (per-loser replace)。"""

    def test_d17_peace_demands_kinds_validated_in_plan_and_peacewin(self):
        import turnengine as TE
        import peacewin as PW

        # Plan DSL 静态拦截 kinds=['annex'] 并提示合法值与 'state'
        ok, errs = TE.validate_plan({
            "actions": [
                {"op": "peace_demands", "args": {"loser": "HOL", "kinds": ["annex"], "replace": True}}
            ]
        })
        self.assertFalse(ok)
        joined = " ".join(errs)
        self.assertIn("annex", joined)
        self.assertIn("state", joined)
        self.assertIn("puppet", joined)

        # peacewin.submit_demands 直接调用也不再抛裸 KeyError
        p, conf, g, hol_acts, _ = _make_peace_fixture()
        finfo = {}
        with self.assertRaises(ValueError) as ctx:
            PW.submit_demands(g, conf, 1, 10, kinds=["annex"], filter_info=finfo)
        self.assertIn("state", str(ctx.exception))
        self.assertIn("annex", str(ctx.exception))
        self.assertEqual(finfo.get("blocked_by"), "invalid_peace_kind")
        self.assertIn("state", finfo.get("valid_kinds") or [])

    def test_d18_peace_finish_blocks_empty_demands_and_fixes_at_war_after(self):
        import peacewin as PW
        import cmdwin
        import combat as _cb

        p, conf, g, hol_acts, ins_acts = _make_peace_fixture()
        PW._SUBMITTED_DEMANDS.clear()

        # 1) 0 索赔时默认拒绝关闭和会，绝不白和平
        with mock.patch.object(PW, "live_conference", return_value=conf), \
             mock.patch.object(PW, "execute") as m_exec:
            r_blk = PW.finish(g, 1)
            self.assertFalse(r_blk["ok"])
            self.assertEqual(r_blk["blocked_by"], "empty_peace_demands")
            self.assertTrue(r_blk["conference_still_open"])
            m_exec.assert_not_called()

        # 2) 显式 allow_empty=True 才允许白和平，并正确消除本场和会残余敌国造成的假 at_war_after
        def _fake_done_white(_g, what, _idx):
            p.write(conf + 0xDD, b"\x01")
            p.write(conf + 0x1C8 + 8, struct.pack("<Q", 1))
            p.write(conf + 0x1F8 + 8, struct.pack("<Q", 0))
            return {"ran": what, "result": 1}

        with mock.patch.object(PW, "live_conference", return_value=conf), \
             mock.patch.object(PW, "execute", side_effect=_fake_done_white), \
             mock.patch.object(PW, "summary", return_value={"open": False, "participants": [{"country": 1}, {"country": 10}]}), \
             mock.patch.object(_cb, "enemy_tags", return_value=["HOL"]), \
             mock.patch.object(_cb, "at_war", return_value=True), \
             mock.patch.object(_cb, "combats", return_value=[]):
            r_wp = PW.finish(g, 1, allow_empty=True)
            self.assertTrue(r_wp["white_peace_confirmed"])
            self.assertTrue(r_wp["at_war_after_raw"])
            self.assertFalse(r_wp["at_war_after"])
            self.assertEqual(r_wp["at_war_pending_cleanup_enemies"], ["HOL"])

        # 3) 若仍有未参与本场和会的场外活跃敌国（如 FRA），at_war_after 必须保持 True
        p.write(conf + 0xDD, b"\x00")
        p.write(conf + 0x1C8 + 8, struct.pack("<Q", 0))
        with mock.patch.object(cmdwin, "alloc", side_effect=lambda _g, n: p.alloc(n)), \
             mock.patch.object(PW, "build_actions", return_value=list(hol_acts.values())), \
             mock.patch.object(PW, "live_conference", return_value=conf), \
             mock.patch.object(PW, "execute", side_effect=_fake_done_white), \
             mock.patch.object(PW, "summary", return_value={"open": False, "participants": [{"country": 1}, {"country": 10}]}), \
             mock.patch.object(_cb, "enemy_tags", return_value=["HOL", "FRA"]), \
             mock.patch.object(_cb, "at_war", return_value=True), \
             mock.patch.object(_cb, "combats", return_value=[]):
            PW.submit_demands(g, conf, 1, 10, states=[7, 35])
            r_out = PW.finish(g, 1)
            self.assertTrue(r_out["at_war_after"])
            self.assertEqual(r_out["remaining_enemies_outside_conference"], ["FRA"])

    def test_d19_replace_scoped_to_loser_and_replace_all_warns(self):
        import peacewin as PW
        import cmdwin

        p, conf, g, hol_acts, ins_acts = _make_peace_fixture()

        def _fake_build(_g, lidx, kind="state", winner_idx=None):
            return list(hol_acts.values()) if lidx == 10 else list(ins_acts.values())

        with mock.patch.object(cmdwin, "alloc", side_effect=lambda _g, n: p.alloc(n)), \
             mock.patch.object(PW, "build_actions", side_effect=_fake_build), \
             mock.patch.object(PW, "live_conference", return_value=conf):
            f_hol = {}
            PW.submit_demands(g, conf, 1, 10, replace=True, filter_info=f_hol)
            self.assertEqual(f_hol["queued_states_by_loser"], {"HOL": [7, 35, 36, 309, 695]})

            # 紧接对 INS 传 replace=True：必须保留 HOL 的 5 个州，绝不静默清空！
            f_ins = {}
            _, acts = PW.submit_demands(g, conf, 1, 20, replace=True, filter_info=f_ins)
            self.assertEqual(f_ins["mode"], "replace_loser")
            self.assertEqual(f_ins["queued_states_by_loser"]["HOL"], [7, 35, 36, 309, 695])
            self.assertEqual(f_ins["queued_states_by_loser"]["INS"], [334, 335, 672])
            self.assertEqual(f_ins["preserved_other_losers"], {"HOL": 5})
            self.assertEqual(len(acts), 8)

            # 显式 replace_all=True 才清空其他 loser 并给 warning
            f_all = {}
            _, acts_all = PW.submit_demands(g, conf, 1, 20, states=[334], replace_all=True, filter_info=f_all)
            self.assertEqual(f_all["mode"], "replace_all")
            self.assertEqual(f_all["queued_states_by_loser"], {"INS": [334]})
            self.assertIn("HOL", f_all["dropped_states_by_loser"])
            self.assertIn("warning", f_all)
            self.assertEqual(len(acts_all), 1)


class TestD20DeclareWarEscalationAndAnschluss(unittest.TestCase):
    """D-20: 分离战术与战略门禁 + GER_anschluss Gotterdammerung 纠偏。"""

    def test_d20_override_checks_does_not_bypass_guarantor_or_undefended_major(self):
        from domains.battleplan_orders import BattleplanOrdersMixin
        from domains.diplomacy_war import DiplomacyWarMixin
        import diplomacy as _dip

        class _FakeWarGame(BattleplanOrdersMixin, DiplomacyWarMixin):
            GER_SNOWBALL = ("GER_anschluss", "GER_demand_sudetenland", "GER_end_of_czechoslovakia")

            def _c(self, tag=None):
                return type("C", (), {"tag": str(tag or "GER").upper(), "tag_id": 1})()

            def divisions(self, tag=None, limit=1, deep=False):
                return {"count": 50 if (tag or "GER") == "GER" else 3, "source": "mock", "verified": True}

            def equipment_stockpile(self, tag=None):
                return {"stockpile": [{"equipment": "infantry_equipment_1", "count": 3000}]}

            def completed_focuses(self, tag=None):
                return []

            def date(self):
                return {"year": 1937}

            def _border_divisions(self, tgt, tag=None):
                return {"border_provinces": 8, "divisions_on_border": 0, "divisions_count": 50}

            def _front_plan_check(self, tgt, tag=None):
                return {"ok": False, "problem": "no front line faces %s" % tgt}

            def _front_directive_check(self, tgt):
                return {"ok": True}

        g = _FakeWarGame()
        escalation_high = {
            "guaranteed_by": ["ENG", "FRA"],
            "subjects": ["INS"],
            "co_belligerents": ["FRA", "ENG", "INS"],
            "new_co_belligerents": ["FRA", "ENG", "INS"],
            "co_belligerent_details": [
                {"tag": "FRA", "divisions": 75, "is_major": True, "already_at_war": False},
                {"tag": "ENG", "divisions": 57, "is_major": True, "already_at_war": False},
                {"tag": "INS", "divisions": 7, "is_major": False, "already_at_war": False},
            ],
            "co_belligerent_borders": [
                {"tag": "FRA", "border_provinces": 8, "divisions_on_border": 0, "enemy_divisions": 75,
                 "is_major": True, "has_front_line": False, "status": "undefended"},
            ],
            "undefended_new_co_belligerents": [
                {"tag": "FRA", "border_provinces": 8, "divisions_on_border": 0, "enemy_divisions": 75,
                 "is_major": True, "reasons": ["guarantees(HOL)"]},
            ],
            "total_enemy_divisions_est": 188,
            "diplomatic_blockers": [],
            "diplomatic_warnings": ["diplomatic escalation: 16 co-belligerents"],
        }

        with mock.patch.object(_dip, "war_escalation_forecast", return_value=escalation_high):
            # 1) override_checks=True + 布尔 ack_co_belligerents=True 绝不能放行 ENG/FRA 保独与裸边境
            res_blk = g.declare_war("HOL", override_checks=True, ack_co_belligerents=True)
            self.assertFalse(res_blk["ok"])
            self.assertEqual(res_blk["blocked_by"], "strategic_escalation_unconfirmed")
            self.assertEqual(res_blk["tactical_blockers"], [])
            self.assertEqual(res_blk["strategic_escalation_required_ack"], ["ENG", "FRA"])

            # 2) 具名确认 ["ENG", "FRA"] 或 confirm_escalation=True 才放行（用 dry_run=True 验证 ready）
            res_named = g.declare_war("HOL", dry_run=True, override_checks=True, ack_co_belligerents=["ENG", "FRA"])
            self.assertTrue(res_named["ok"])
            res_conf = g.declare_war("HOL", dry_run=True, override_checks=True, confirm_escalation=True)
            self.assertTrue(res_conf["ok"])

        # 3) 1936 无保独、无裸边境的普通附庸（仅 INS 21 师）在 override_checks=True + ack=True 时照常放行
        escalation_low = {
            "guaranteed_by": [],
            "subjects": ["INS"],
            "co_belligerents": ["INS"],
            "new_co_belligerents": ["INS"],
            "co_belligerent_details": [
                {"tag": "INS", "divisions": 21, "is_major": False, "already_at_war": False},
            ],
            "co_belligerent_borders": [],
            "undefended_new_co_belligerents": [],
            "total_enemy_divisions_est": 22,
            "diplomatic_blockers": [],
            "diplomatic_warnings": [],
        }
        with mock.patch.object(_dip, "war_escalation_forecast", return_value=escalation_low):
            res_low = g.declare_war("HOL", dry_run=True, override_checks=True, ack_co_belligerents=True)
            self.assertTrue(res_low["ok"])

    def test_d20_ger_anschluss_not_hidden_at_peace_with_42_divs(self):
        from domains.focus import FocusMixin
        import combat as _cb
        import focusplan

        import domains.focus as _df

        class _FakeFocusGame(FocusMixin):
            def __init__(self):
                self.p = _MemProc()
                self.cptr = self.p.alloc(0x1600)
                self.prog = self.p.alloc(0x80)
                self.p.write(self.cptr + _df.CO_FOCUS_PROGRESS, struct.pack("<Q", self.prog))
                self.f_ptr = self.p.alloc(0x80)
                self._at_war = False

            def _c(self, tag=None):
                return type("C", (), {"tag": "GER", "tag_id": 1, "ptr": self.cptr})()

            def date_raw(self):
                return 16970000

            def date(self):
                return {"year": 1937}

            def focus_tree(self, tag=None):
                return [self.f_ptr]

            def _can_start_focus_cmd(self, ptr, tid):
                return True

        g = _FakeFocusGame()
        fake_meta = {"focuses": {"GER_anschluss": {"prereq_groups": [], "mutex": [], "days": 70}}, "plans": {"GER": []}}
        with mock.patch.object(_df, "_arr", side_effect=lambda p, base, off: [1] * 42 if off == _df.CO_ARMIES else []), \
             mock.patch.object(_df, "item_name", side_effect=lambda p, addr: "GER_anschluss"), \
             mock.patch.object(focusplan, "load", return_value=fake_meta), \
             mock.patch.object(_cb, "at_war", side_effect=lambda _g, _t: g._at_war):
            # 和平期 42 师：不再因过时 54 师阈值静态隐藏 GER_anschluss
            res_peace = g.selectable_focuses("GER")
            self.assertIn("GER_anschluss", res_peace["focuses"])
            # 战时：必须因 has_war=no 被拦截
            g._at_war = True
            g._sel_cache = None
            res_war = g.selectable_focuses("GER")
            self.assertNotIn("GER_anschluss", res_war["focuses"])
            self.assertTrue(any("has_war=no" in b for b in res_war["blocked"]))


class TestD21SupplyOverloadWarning(unittest.TestCase):
    """D-21: army_battleplan 即使 force=True 超载也要在顶层与回执显式告警。"""

    def test_d21_plan_front_force_overload_sets_warning_and_receipt(self):
        from domains.battleplan import BattleplanMixin
        import turnengine as TE
        import armyops
        import armyops_win as AW

        p = _MemProc()
        og1, og2 = p.alloc(0x100), p.alloc(0x100)

        class _FakeBP(BattleplanMixin):
            def __init__(self):
                self.p = p

            def _c(self, tag=None):
                return type("C", (), {"tag": "GER", "tag_id": 1, "ptr": 0x1000})()

            def wars(self, tag=None):
                return {"enemies": []}

            def _supply_front_guard(self, enemy_tag, tag=None, ratio=1.25):
                return {"ok": False, "needed": 42, "capacity": 15.78, "ratio": 2.66}

            def divisions(self, tag=None, limit=1):
                return {"divisions": []}

            def _best_front_segment(self, segs, divs=None):
                return segs[0]

        g = _FakeBP()
        seg = {"front": 0, "front_handle": [66, 1466], "section": 1, "border_provinces": [1, 2, 3]}
        with mock.patch.object(AW, "fronts_towards", return_value=[seg]), \
             mock.patch.object(AW, "target_provinces", return_value=[101, 102]), \
             mock.patch.object(armyops, "orders_groups", return_value=[og1, og2]), \
             mock.patch.object(AW, "orders", return_value=[]), \
             mock.patch.object(AW, "divisions_of", side_effect=lambda _g, o: [1] * (26 if o == og1 else 10)):
            res = g.plan_front("HOL", force=True, dry_run=True)
            self.assertTrue(res["ok"])
            self.assertTrue(res["supply_overloaded"])
            self.assertIn("SEVERE SUPPLY OVERLOAD", res["supply_warning"])
            self.assertEqual(res["recommended_max_divisions"], 15)

            line = TE._summarize_action_result("army_battleplan", {"enemy_tag": "HOL", "force": True}, res)
            self.assertIn("[WARN: supply_overload", line)
            self.assertIn("2.28x", line)


class TestD22D23ConveyorDiagnostics(unittest.TestCase):
    """D-22 & D-23: conveyors 透出 series / equipment_ratio / stall_reason 及 deploy_ready 保护。"""

    def test_d22_d23_conveyor_special_forces_cap_and_partial_lines_guard(self):
        from domains.military_land import MilitaryLandMixin
        from domains._shared import DT_NAME
        import offsets as O

        p = _MemProc()
        cptr = p.alloc(0x1200)
        dep = p.alloc(0x100)
        p.write(cptr + O.CO_DEPLOYMENT_STATUS, struct.pack("<Q", dep))

        # 构造两条 conveyor：conv1=Paratroopers (0.0, eq=1.0, series=0), conv2=Infanterie (1.0 & 0.4, series=2)
        def _mk_tpl(name):
            t = p.alloc(0x100)
            nb = name.encode("utf-8")
            if len(nb) <= 15:
                p.write(t + DT_NAME, nb.ljust(16, b"\0"))
                p.write(t + DT_NAME + 0x10, struct.pack("<QQ", len(nb), 15))
            else:
                sbuf = p.alloc(len(nb) + 1)
                p.write(sbuf, nb + b"\0")
                p.write(t + DT_NAME, struct.pack("<QQQ", sbuf, 0, len(nb))[:16])
                p.write(t + DT_NAME + 0x10, struct.pack("<QQ", len(nb), len(nb) + 1))
            return t

        def _mk_conv(hid, tpl_ptr, series, prov, line_specs):
            cv = p.alloc(0x100)
            p.write(cv + 0x08, struct.pack("<ii", 64, hid))
            p.write(cv + 0x20, struct.pack("<Q", tpl_ptr))
            p.write(cv + 0x48, struct.pack("<i", series))
            p.write(cv + 0x4C, struct.pack("<i", prov))
            p.write(cv + 0x50, struct.pack("<i", 2))
            l_arr = p.alloc(8 * len(line_specs))
            for idx, (prog_i, eq_i) in enumerate(line_specs):
                ln = p.alloc(0x50)
                md = p.alloc(0xD0)
                p.write(ln + 0x08, struct.pack("<ii", 63, hid * 10 + idx))
                p.write(ln + 0x30, struct.pack("<Q", md))
                p.write(md + 0x60, struct.pack("<I", int(prog_i * 100000)))
                p.write(md + 0x68, struct.pack("<q", int(eq_i * 100000)))
                p.write(md + 0xB0, struct.pack("<i", 3000))
                p.write(l_arr + 8 * idx, struct.pack("<Q", ln))
            p.write(cv + 0x78, struct.pack("<Qii", l_arr, len(line_specs), len(line_specs)))
            return cv

        cv_para = _mk_conv(48, _mk_tpl("Paratroopers"), 0, 6521, [(0.0, 1.0), (0.0, 1.0)])
        cv_inf = _mk_conv(57, _mk_tpl("Infanterie-Division"), 2, 6521, [(1.0, 1.0), (0.4, 0.834)])
        cv_arr = p.alloc(16)
        p.write(cv_arr, struct.pack("<QQ", cv_para, cv_inf))
        p.write(dep + O.CO_DEPLOYMENT_CONVEYORS, struct.pack("<Qii", cv_arr, 2, 2))

        class _FakeMil(MilitaryLandMixin):
            def __init__(self):
                self.p = p

            def _c(self, tag=None):
                return type("C", (), {"tag": "GER", "tag_id": 1, "ptr": cptr})()

            def _estimate_special_forces_cap(self, tag=None):
                return {
                    "total_field_battalions": 400,
                    "special_forces_battalions": 24,
                    "estimated_cap": 24,
                    "at_or_over_cap": True,
                    "by_name": {"paratroopers": {"is_special_forces": True, "total_battalions": 3, "sf_battalions": 3}},
                }

            def divisions(self, tag=None, limit=1):
                return {"count": 42}

        g = _FakeMil()
        convs = g.conveyors("GER")
        self.assertEqual(len(convs), 2)
        c0, c1 = convs[0], convs[1]
        self.assertEqual(c0["template"], "Paratroopers")
        self.assertEqual(c0["series"], 0)
        self.assertEqual(c0["equipment_ratio"], 1.0)
        self.assertEqual(c0["stall_reason"], "special_forces_cap_reached")
        self.assertIn("24", c0["stall_detail"])

        self.assertEqual(c1["series"], 2)
        self.assertAlmostEqual(c1["progress"], 1.0, places=3)
        self.assertAlmostEqual(c1["min_progress"], 0.4, places=3)
        self.assertAlmostEqual(c1["min_equipment_ratio"], 0.834, places=3)

        # deploy_ready 默认 require_all_lines=True：conv2 最慢线仅 0.4，不整带强推，并标明 partial_lines_ready
        dep_res = g.deploy_ready_conveyors("GER", min_progress=0.995)
        self.assertEqual(dep_res["conveyors_deployed"], 0)
        self.assertEqual(dep_res["attempts"][0]["stall_reason"], "special_forces_cap_reached")
        self.assertTrue(dep_res["attempts"][1].get("partial_lines_ready"))


class TestD24WarEconomyStrictGreater(unittest.TestCase):
    """D-24: war_economy 等经济法案使用严格大于 `>` 而非 `>=`，并校验非独裁开战条件。"""

    def test_d24_war_economy_requires_strictly_gt_050_and_ideology_check(self):
        from domains.politics import PoliticsMixin

        class _Co:
            def __init__(self, ws, pp=545):
                self.tag = "GER"
                self.political_power = pp
                self._ws = ws

            def summary(self):
                return {"political_power": self.political_power, "war_support": self._ws}

        class _Pol(PoliticsMixin):
            pass

        pol = _Pol()
        # war_support == 0.50（实测 1936.10.24 值）：严格大于 0.50 不满足，必须拦！
        r_eq = pol._idea_block_reason("war_economy", _Co(0.50), "GER", at_war=False, ideology="fascism")
        self.assertIsNotNone(r_eq)
        self.assertIn("> 0.50", r_eq)

        # war_support == 0.51 且 fascism：允许！
        r_gt = pol._idea_block_reason("war_economy", _Co(0.51), "GER", at_war=False, ideology="fascism")
        self.assertIsNone(r_gt)

        # war_support == 0.60 但 democratic 且和平：必须要求开战！
        r_dem = pol._idea_block_reason("war_economy", _Co(0.60), "USA", at_war=False, ideology="democratic")
        self.assertIsNotNone(r_dem)
        self.assertIn("war", r_dem.lower())


class TestD25RedDotSelectiveIgnore(unittest.TestCase):
    """D-25: ignore_red_dots 精细静默非严控工业噪声，同时严守严控与危机红线。"""

    def test_d25_ignore_codes_filters_industrial_only_and_protects_strict(self):
        import reddots as RD

        fake_raw = {
            "ok": True,
            "tag": "GER",
            "date": "1936.12.01.06",
            "date_raw": 16967400,
            "at_war": False,
            "items": [
                RD._make_item("IDLE_MIL", "1 idle mil", {}, [], severity="block", tier=1, signature="IDLE_MIL:1"),
                RD._make_item("IDLE_DOCK", "2 idle dock", {}, [], severity="block", tier=2, signature="IDLE_DOCK:2"),
                RD._make_item("NO_FOCUS", "no focus", {}, [], severity="block", tier=1, signature="NO_FOCUS:1"),
            ],
        }
        fake_g = type("G", (), {"p": type("P", (), {"pid": 777})(), "player_tag": lambda self: "GER"})()
        with mock.patch.object(RD, "_scan_raw", return_value=fake_raw):
            ev = RD.evaluate(fake_g, fast=True, ignore_codes=["IDLE_MIL", "IDLE_DOCK", "NO_FOCUS"])
            self.assertEqual([x["code"] for x in ev["blocking"]], ["NO_FOCUS"])
            self.assertEqual(sorted(ev["ignored_codes"]), ["IDLE_DOCK", "IDLE_MIL"])
            self.assertEqual(ev["unignorable_codes"], ["NO_FOCUS"])

        # 仅剩工业项且全部被 ignore_codes 静默 -> has_blocking=False，可顺畅长跑
        fake_raw_ind = dict(fake_raw, items=fake_raw["items"][:2])
        with mock.patch.object(RD, "_scan_raw", return_value=fake_raw_ind):
            ev2 = RD.evaluate(fake_g, fast=True, ignore_codes=["IDLE_MIL", "IDLE_DOCK"])
            self.assertFalse(ev2["has_blocking"])


class TestD26HousekeepReceiptAndDoctrineXP(unittest.TestCase):
    """D-26: housekeep 顶层回执直出 PP/法案/编师/船坞/空军副作用，学说拒绝透出 XP。"""

    def test_d26_housekeep_summary_and_receipt_surface_side_effects(self):
        from housekeep import HousekeepMixin
        import turnengine as TE

        class _FakeHK(HousekeepMixin):
            def date(self):
                return {"raw": 16969000, "text": "1937.03.10.10"}

            def housekeep_construction(self, tag=None, target=None):
                return {"ok": True}

            def housekeep_production(self, tag=None):
                return {"ok": True, "changes": [], "dockyard_changes": [{"ok": True, "before": 1, "after": 3}]}

            def housekeep_doctrines(self, tag=None):
                return {"ok": True, "attempts": []}

            def housekeep_research(self, tag=None, techs=None):
                return {"ok": True}

            def housekeep_politics(self, tag=None, budget_s=12.0):
                return {
                    "ok": True,
                    "pp_delta": -299.0,
                    "actions": [
                        {"law": "war_economy", "ok": True},
                        {"idea_slot": "krupp", "ok": True},
                    ],
                }

            def housekeep_recruitment(self, tag=None):
                return {"ok": True, "deploy": {"deployed": 0}}

            def housekeep_divisions(self, tag=None):
                return {"ok": True, "created": [{"ok": True, "added": 8}]}

            def housekeep_air(self, tag=None):
                return {"ok": True, "attach": {"attached_count": 13}}

            def housekeep_navy(self, tag=None):
                return {"ok": True}

        res = _FakeHK().housekeep("GER")
        self.assertIn("pp_delta=-299.0 (war_economy, krupp)", res["summary"])
        self.assertIn("dock_assigned=+2", res["summary"])
        self.assertIn("divs_assigned=8", res["summary"])
        self.assertIn("air_attached=13", res["summary"])

        receipt = TE._summarize_action_result("housekeep", {}, res)
        self.assertIn("-299.0", receipt)
        self.assertIn("war_economy", receipt)
        self.assertIn("divs_assigned=8", receipt)

    def test_d26_unlock_doctrine_refusal_reports_current_xp(self):
        from domains.research import ResearchMixin
        import cmdwin

        class _FakeDoc(ResearchMixin):
            def _doctrine_templates(self):
                return {"GRAND_DOCTRINE_MOBILE_WARFARE": 0x123456}

            def _c(self, tag=None):
                return type("C", (), {"tag_id": 1, "experience": {"army": 35.5, "navy": 177.0, "air": 12.0}})()

        with mock.patch.object(cmdwin, "can_unlock_doctrine", return_value=False):
            r = _FakeDoc().unlock_doctrine("mobile_warfare")
            self.assertFalse(r["ok"])
            self.assertEqual(r["xp_service"], "army")
            self.assertEqual(r["current_xp"], 35.5)
            self.assertIn("35.5", r["likely_reason"])
            self.assertIn("100", r["likely_reason"])


class TestD27ClockReloadedSave(unittest.TestCase):
    """D-27: 回档后 CGameState+0x468 精确时钟不被旧 game.log 误判为 stale/suspect。"""

    def test_d27_gs_clock_behind_old_log_is_not_suspect(self):
        import api

        g = object.__new__(api.HOI4)
        g._date_debug = {"gs_clock": True, "pick_reason": None}
        g._date_verified = True
        g._log_date = lambda: 16983410       # 旧局留在 game.log 的 1938.09.30
        g._log_age_s = lambda: 5652.7
        g.date_raw = lambda: 16966465        # 回档后的真实活时钟 1936.10.24.01

        d = g.date()
        self.assertFalse(d["suspect"])
        self.assertTrue(d["reloaded_save_detected"])
        self.assertTrue(d["source"].startswith("clock (CGameState+0x468, exact"))
        self.assertNotIn("lower_bound", d)

        # 对照：非精确时钟（gs_clock=False）落后旧日志仍必须标 stale / suspect=True
        g._date_debug = {"gs_clock": False, "pick_reason": "scan"}
        d_stale = g.date()
        self.assertTrue(d_stale["suspect"])
        self.assertIn("stale (BEHIND game.log", d_stale["source"])


class TestD28ProductionLineRequestedSync(unittest.TestCase):
    """D-28: create_production_line 透出 requested_factories，set_production 等待 UpdateFactories 结算。"""

    def test_d28_set_production_waits_for_update_factories_assignment(self):
        from domains.economy import EconomyMixin
        from domains._shared import CO_PRODUCTION, PS_MIL_LINES, ML_FACTORIES, ML_VARIANT, EV_TYPE_ID

        p = _MemProc()
        cptr = p.alloc(0x1000)
        prod = p.alloc(0x400)
        line = p.alloc(0x120)
        var = p.alloc(0x40)
        p.write(cptr + CO_PRODUCTION, struct.pack("<Q", prod))
        l_arr = p.alloc(16)
        p.write(l_arr, struct.pack("<Q", line))
        p.write(prod + PS_MIL_LINES, struct.pack("<Qii", l_arr, 1, 1))
        p.write(line + ML_VARIANT, struct.pack("<Q", var))
        p.write(var + EV_TYPE_ID, struct.pack("<i", 999))
        p.write(line + ML_FACTORIES, struct.pack("<i", 1))
        p.write(line + 0xE8, struct.pack("<i", 1))

        settle_calls = [0]

        class _FakeEco(EconomyMixin):
            def __init__(self):
                self.p = p
                self._eq_by_id = {999: "transport_plane_equipment_1"}
                self._eq_by_name = {"transport_plane_equipment_1": 999}

            def _c(self, tag=None):
                return type("C", (), {"tag": "GER", "tag_id": 1, "ptr": cptr})()

            def _load_equipment(self):
                pass

            def handle_of(self, ptr):
                return {"type": 56, "id": 16}

            def factory_allocation(self, tag=None):
                return {"source": "engine_production_status", "military_idle": 2, "military_available": 30}

            def send_command(self, name, payload, tag=None):
                # 模拟命令瞬间只涨了 requested(+0xE8)=3，assigned(+0x18) 延迟到第 2 次 settle 才刷新为 3
                p.write(line + 0xE8, struct.pack("<i", 3))

            def _settle(self, sec=0.18):
                settle_calls[0] += 1
                if settle_calls[0] >= 2:
                    p.write(line + ML_FACTORIES, struct.pack("<i", 3))

            def production_lines(self, tag=None, include_obsolete=True):
                return [{"index": 0, "equipment": "transport_plane_equipment_1", "factories": p.i32(line + ML_FACTORIES)}]

        g = _FakeEco()
        res = g.set_production_amount(equipment="transport_plane_equipment_1", amount=3, auto_reclaim=False)
        self.assertTrue(res["ok"])
        self.assertEqual(res["before"], 1)
        self.assertEqual(res["after"], 3)
        self.assertEqual(res["requested_after"], 3)


if __name__ == "__main__":
    unittest.main()
