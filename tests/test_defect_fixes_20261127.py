# -*- coding: utf-8 -*-
"""2026-11-27 本局实测缺陷清单（D-01 ~ D-16）全覆盖离线回归测试。

纯离线、0 注入、跨平台运行，逐项锁死本轮所有修复：
  - D-01: set_production 支持 equipment 名定位与 (line + equipment) 防错行保护；
          create_production_line 默认 move_to_front=False（不强制插到 index 0）并回显 lines_snapshot；
  - D-02: add_divisions_to_army / delete_order / paradrop / offensive_line 支持中文集团军名解析；
  - D-03 & D-04: wait_and_brief 预留 brief_headroom_s、>50s 给 timeout_warning，rpc_error 强制字符串化；
  - D-05: windowstate._pick_main_window 优先真实可见大尺寸主渲染窗，跳过隐藏/0x0 辅助窗；
  - D-06: combat._war_relations_win 过滤已彻底被吞并的 0 州 0 师幽灵敌国；
  - D-07: CRemoveConveyorCommand (0x2AC0610) & CRemoveConveyorLineCommand (0x2AC0850) 虚表单一真源锁定；
  - D-08: Plan DSL 开放 drop_focus，focus_block_reason 提示兼容 drop_focus；
  - D-09: autopilot on_blocked="skip" 暂缓国策并记录 focus_deferred，条件满足后自动恢复优先执行；
          set_focus 失败时附带脚本 available 条件摘要；
  - D-10: selectable_focuses 在 engine_checked=False 时透出 recommended_verified=False 与 engine_checked_note；
  - D-11 & D-12: deploy_ready 默认门槛 0.995（防无参强推未训完传送带），train_divisions 支持 series 参数；
  - D-14: air_transfer 区分 order_verified / arrived / in_transit，paradrop 预检提示转场途中运输机；
  - D-15: Plan DSL 开放 delete_order 且硬锁 allow_child=False；
  - D-16: paradrop 预检清晰透出 divisions_source / divisions_to_assign / division_ptrs_to_assign。
"""
from __future__ import annotations

import struct
import sys
import types
import unittest
from unittest import mock

import os
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CALLING = os.path.join(ROOT, "calling")
MCP_DIR = os.path.join(ROOT, "mcp")
for _p in (ROOT, CALLING, MCP_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import airops_win as AW
import armyops_win as AOW
import autopilot as AP
import cmdwin
import combat
import offsets as O
import offsets_win as OW
import server as MCP_SERVER
import turnengine as TE
import windowstate
from domains.economy import EconomyMixin
from domains.focus import FocusMixin
from domains.military_land import MilitaryLandMixin


class _BufMem:
    """简易字节可读内存桩，支持 u64 / u32 / i32 / u8 / read / write。"""

    def __init__(self, base=0x140000000):
        self.base = base
        self.pid = 13184
        self.mem = {}
        self.writes = []

    def put(self, addr, data: bytes):
        for i, b in enumerate(data):
            self.mem[addr + i] = b

    def put_u64(self, addr, v):
        self.put(addr, struct.pack("<Q", int(v) & 0xFFFFFFFFFFFFFFFF))

    def put_i32(self, addr, v):
        self.put(addr, struct.pack("<i", int(v)))

    def put_u32(self, addr, v):
        self.put(addr, struct.pack("<I", int(v) & 0xFFFFFFFF))

    def put_u8(self, addr, v):
        self.put(addr, bytes([int(v) & 0xFF]))

    def read(self, addr, n):
        out = bytearray(n)
        for i in range(n):
            if (addr + i) not in self.mem:
                raise KeyError(hex(addr + i))
            out[i] = self.mem[addr + i]
        return bytes(out)

    def u64(self, addr):
        return struct.unpack("<Q", self.read(addr, 8))[0]

    def i32(self, addr):
        return struct.unpack("<i", self.read(addr, 4))[0]

    def u32(self, addr):
        return struct.unpack("<I", self.read(addr, 4))[0]

    def u8(self, addr):
        return self.read(addr, 1)[0]

    def write(self, addr, data: bytes):
        self.writes.append((addr, bytes(data)))
        self.put(addr, bytes(data))


class TestD01ProductionSafeLocator(unittest.TestCase):
    """D-01: set_production 按装备定位 + line/equipment 防错行校验 + Plan DSL 校验。"""

    def _make_econ_game(self):
        p = _BufMem()
        co_ptr = 0x100000
        prod_ptr = 0x200000
        arr_ptr = 0x210000
        ln0, ln1, ln2 = 0x300000, 0x301000, 0x302000
        v0, v1, v2 = 0x400000, 0x401000, 0x402000

        p.put_u64(co_ptr + OW.CO_PRODUCTION_STATUS, prod_ptr)
        # PS_MIL_LINES CPdxArray {data, cap, size}
        p.put(prod_ptr + OW.PS_MIL_LINES, struct.pack("<Qii", arr_ptr, 4, 3))
        p.put(arr_ptr, struct.pack("<QQQ", ln0, ln1, ln2))

        for ln, var, eq_id, facs, hid in (
            (ln0, v0, 101, 0, 1),
            (ln1, v1, 102, 11, 2),
            (ln2, v2, 103, 6, 3),
        ):
            p.put_u64(ln + OW.ML_VARIANT, var)
            p.put_i32(ln + OW.ML_FACTORIES, facs)
            p.put_i32(ln + 0xE8, facs)
            p.put_i32(var + OW.EV_TYPE_ID, eq_id)
            p.put(ln + 0x08, struct.pack("<ii", 56, hid))

        class _EconG(EconomyMixin):
            def __init__(self):
                self.p = p
                self._eq_by_id = {
                    101: "transport_plane_equipment_1",
                    102: "infantry_equipment_1",
                    103: "support_equipment_1",
                }
                self.sent = []

            def _c(self, tag=None):
                return types.SimpleNamespace(tag="GER", tag_id=1, ptr=co_ptr)

            def _load_equipment(self):
                return None

            def handle_of(self, ptr):
                t, i = struct.unpack("<ii", self.p.read(ptr + 0x08, 8))
                return {"type": t, "id": i}

            def factory_allocation(self, tag=None):
                return {"source": "engine_production_status", "military_idle": 10, "naval_idle": 0}

            def send_command(self, name, payload, tag=None):
                self.sent.append((name, payload))
                hid = payload["line_id"] >> 32
                step = struct.unpack("<i", struct.pack("<I", payload["amount"] & 0xFFFFFFFF))[0]
                target_ln = {1: ln0, 2: ln1, 3: ln2}[hid]
                cur = self.p.i32(target_ln + OW.ML_FACTORIES)
                self.p.put_i32(target_ln + OW.ML_FACTORIES, cur + step)
                self.p.put_i32(target_ln + 0xE8, cur + step)

            def _settle(self, *_a, **_kw):
                pass

            def production_lines(self, tag=None, include_obsolete=False):
                return [
                    {"index": 0, "equipment": "transport_plane_equipment_1", "factories": self.p.i32(ln0 + OW.ML_FACTORIES)},
                    {"index": 1, "equipment": "infantry_equipment_1", "factories": self.p.i32(ln1 + OW.ML_FACTORIES)},
                    {"index": 2, "equipment": "support_equipment_1", "factories": self.p.i32(ln2 + OW.ML_FACTORIES)},
                ]

        return _EconG(), p, (ln0, ln1, ln2)

    def test_line_equipment_mismatch_blocks_instead_of_cutting_rifles(self):
        g, p, (_ln0, ln1, _ln2) = self._make_econ_game()
        # 复现 D-01：调用方以为 line=1 是 support_equipment_1，实际已偏移成 infantry_equipment_1
        res = g.set_production_amount(line_index=1, amount=3, equipment="support_equipment_1")
        self.assertFalse(res["ok"])
        self.assertEqual(res["blocked_by"], "production_line_mismatch")
        self.assertEqual(res["actual_equipment"], "infantry_equipment_1")
        self.assertEqual(res["matching_lines"][0]["line"], 2)
        self.assertEqual(p.i32(ln1 + OW.ML_FACTORIES), 11, "步枪产线必须丝毫未动")
        self.assertEqual(g.sent, [])

    def test_locate_by_equipment_name_immune_to_index_shift(self):
        g, p, (ln0, _ln1, _ln2) = self._make_econ_game()
        res = g.set_production_amount(equipment="transport_plane_equipment_1", amount=3)
        self.assertTrue(res["ok"])
        self.assertEqual(res["line"], 0)
        self.assertEqual(res["after"], 3)
        self.assertEqual(p.i32(ln0 + OW.ML_FACTORIES), 3)

    def test_plan_dsl_set_production_allows_equipment_only_and_blocks_empty(self):
        ok1, e1 = TE.validate_plan({"actions": [{"op": "set_production", "args": {"equipment": "support_equipment_1", "amount": 4}}]})
        self.assertTrue(ok1, str(e1))
        ok2, e2 = TE.validate_plan({"actions": [{"op": "set_production", "args": {"line": 1, "equipment": "support_equipment_1", "amount": 4}}]})
        self.assertTrue(ok2, str(e2))
        ok3, _e3 = TE.validate_plan({"actions": [{"op": "set_production", "args": {"amount": 4}}]})
        self.assertFalse(ok3, "line 和 equipment 全缺时必须在静态校验期拦截")


class TestD02OrdersGroupNameResolution(unittest.TestCase):
    """D-02: add_to_army / delete_order / paradrop 支持中文集团军名解析。"""

    def test_resolve_chinese_army_name_and_ambiguous_guard(self):
        class _ArmyG(MilitaryLandMixin):
            def order_groups(self, tag=None):
                return {
                    "count": 2,
                    "groups": [
                        {"ptr": "0x18e7697c240", "name": "第1集团军", "divisions": 24, "is_army_group": False},
                        {"ptr": "0x18e7697f600", "name": "伞兵军", "divisions": 6, "is_army_group": False},
                    ],
                }

        g = _ArmyG()
        og, err, _ = g._resolve_orders_group("第1集团军")
        self.assertEqual(og, 0x18E7697C240)
        self.assertEqual(err, "")

        og2, err2, _ = g._resolve_orders_group("0x18e7697f600")
        self.assertEqual(og2, 0x18E7697F600)
        self.assertEqual(err2, "")

        og_miss, err_miss, cands = g._resolve_orders_group("不存在的军")
        self.assertEqual(og_miss, 0)
        self.assertIn("no orders_group matched", err_miss)
        self.assertEqual(len(cands), 2)


class TestD03D04TimeoutAndErrorSerialization(unittest.TestCase):
    """D-03 & D-04: wait_and_brief 预留简报余量 + >50s 警告 + rpc_error 强制字符串化。"""

    def test_rpc_error_always_stringifies_message(self):
        r = MCP_SERVER.rpc_error(1, -32001, {"detail": "timed out"})
        self.assertIsInstance(r["error"]["message"], str)
        self.assertIn("timed out", r["error"]["message"])

    def test_wait_and_brief_reserves_brief_headroom_and_warns_above_50s(self):
        seen_wait_timeout = []

        class _FakeTL:
            def __init__(self, g, poll=1.5):
                pass

            def wait(self, **kw):
                seen_wait_timeout.append(kw["timeout_seconds"])
                return {"ok": True, "woke_because": "timeout", "waited_game_days": 5, "waited_real_seconds": 44.0, "paused": True}

        with mock.patch.dict(os.environ, {"HOI4_WAIT_MAX_S": "120"}), \
             mock.patch("turnloop.TurnLoop", _FakeTL), \
             mock.patch.object(TE, "build_turn_brief", return_value={"markdown": "# brief", "approx_tokens": 100}):
            r50 = TE.wait_and_brief(object(), timeout_seconds=50)
            r100 = TE.wait_and_brief(object(), timeout_seconds=100)

        self.assertEqual(seen_wait_timeout[0], 44, "timeout_seconds=50 应预留 6s 给 build_turn_brief")
        self.assertEqual(r50["wait"]["brief_headroom_seconds"], 6)
        self.assertNotIn("timeout_warning", r50["wait"])
        self.assertEqual(seen_wait_timeout[1], 94)
        self.assertIn("timeout_warning", r100["wait"])


class TestD05WindowPicker(unittest.TestCase):
    """D-05: 优先挑选可见且具备真实尺寸的 HOI4 主窗口，避免辅助/隐藏窗误报 window_minimized。"""

    def test_pick_main_window_prefers_visible_large_hoi4_window(self):
        titles = {101: "Hearts of Iron IV", 202: "Hearts of Iron IV"}
        vis = {101: False, 202: True}
        iconic = {101: True, 202: False}
        rects = {101: (0, 0, 0, 0), 202: (0, 0, 1920, 1080)}

        fake_u = types.SimpleNamespace(
            IsWindowVisible=lambda h: vis[h],
            IsIconic=lambda h: iconic[h],
        )
        with mock.patch.object(windowstate, "_u", fake_u, create=True), \
             mock.patch.object(windowstate, "_title", side_effect=lambda h: titles[h]), \
             mock.patch.object(windowstate, "_rect", side_effect=lambda h: rects[h]):
            picked = windowstate._pick_main_window([101, 202])

        self.assertIsNotNone(picked)
        (_score, best, _t, best_vis, best_iconic, _rc), n = picked
        self.assertEqual(best, 202)
        self.assertTrue(best_vis)
        self.assertFalse(best_iconic)
        self.assertEqual(n, 2)


class TestD06AnnexedGhostWarRelation(unittest.TestCase):
    """D-06: 刚被和平条约彻底吞并的敌国（0 控制州/0 拥有州/0 师，但 cap>0 证明曾存在）不应导致 at_war=True。"""

    def test_annexed_zero_state_enemy_filtered_while_active_enemy_kept(self):
        p = _BufMem()
        ger_ptr, hol_ptr, fra_ptr = 0x100000, 0x110000, 0x120000
        dip_ptr, arr_ptr = 0x200000, 0x210000
        wr_hol, wr_fra = 0x300000, 0x301000

        # GER id=1, HOL id=8 (annexed), FRA id=5 (alive)
        p.put_i32(ger_ptr + 8, 1)
        p.put_u64(ger_ptr + OW.CO_DIPLOMACY_STATUS, dip_ptr)
        p.put(dip_ptr + OW.COMBAT_DS_WAR_RELATIONS, struct.pack("<Qii", arr_ptr, 4, 2))
        p.put(arr_ptr, struct.pack("<QQ", wr_hol, wr_fra))

        p.put_i32(wr_hol + OW.COMBAT_WR_WIN_A2, 1)
        p.put_i32(wr_hol + OW.COMBAT_WR_WIN_B2, 8)
        p.put_i32(wr_fra + OW.COMBAT_WR_WIN_A2, 1)
        p.put_i32(wr_fra + OW.COMBAT_WR_WIN_B2, 5)

        # HOL 已全吞：size=0 但 cap>0
        p.put(hol_ptr + OW.CO_STATES, struct.pack("<Qii", 0x500000, 8, 0))
        p.put(hol_ptr + OW.CO_STATES_B, struct.pack("<Qii", 0x501000, 8, 0))
        p.put(hol_ptr + OW.CO_ARMIES, struct.pack("<Qii", 0x502000, 16, 0))
        # FRA 仍存活：owned=20
        p.put(fra_ptr + OW.CO_STATES, struct.pack("<Qii", 0x510000, 32, 20))
        p.put(fra_ptr + OW.CO_STATES_B, struct.pack("<Qii", 0x511000, 32, 20))
        p.put(fra_ptr + OW.CO_ARMIES, struct.pack("<Qii", 0x512000, 64, 50))

        c_map = {
            "GER": types.SimpleNamespace(ptr=ger_ptr, tag="GER"),
            "HOL": types.SimpleNamespace(ptr=hol_ptr, tag="HOL"),
            "FRA": types.SimpleNamespace(ptr=fra_ptr, tag="FRA"),
        }

        class _WarG:
            def __init__(self):
                self.p = p
                self.oi = types.SimpleNamespace(class_of=lambda _a: "CWarRelation")

            def country(self, t):
                return c_map[t]

            def player_tag(self):
                return "GER"

        g = _WarG()
        with mock.patch.object(combat, "_country_id_map", return_value={1: "GER", 8: "HOL", 5: "FRA"}):
            rels = combat._war_relations_win(g, "GER")
        self.assertEqual([r["enemy"] for r in rels], ["FRA"], "已彻底吞并的 HOL 残骸必须被过滤，存活的 FRA 必须保留")


class TestD07RemoveConveyorVtable(unittest.TestCase):
    """D-07: CRemoveConveyorCommand 与 CRemoveConveyorLineCommand 虚表地址严格锁定为 PE 1.19.5.0 实证值。"""

    def test_vtable_rvas_match_pe_ctors(self):
        self.assertEqual(OW.CMD_VT_REMOVE_CONVEYOR, 0x2AC0610)
        self.assertEqual(OW.CMD_VT_REMOVE_CONVEYOR_LINE, 0x2AC0850)
        self.assertEqual(O.CMD_VT_REMOVE_CONVEYOR, 0x2AC0610)
        self.assertEqual(O.CMD_VT_REMOVE_CONVEYOR_LINE, 0x2AC0850)
        self.assertEqual(cmdwin.VT_REMOVE_CONVEYOR, 0x2AC0610)
        self.assertEqual(cmdwin.VT_REMOVE_CONVEYOR_LINE, 0x2AC0850)


class TestD08D09D10FocusAndAutopilot(unittest.TestCase):
    """D-08 / D-09 / D-10: drop_focus 接入 DSL、on_blocked='skip' 暂缓重试语义、selectable_focuses 未验证提示。"""

    def test_drop_focus_in_plan_dsl_and_dispatch(self):
        self.assertIn("drop_focus", TE.ALLOWED_ACTIONS)
        self.assertIn("drop_focus", TE.ACTION_SPECS)
        ok, errs = TE.validate_plan({"actions": [{"op": "drop_focus", "args": {}}]})
        self.assertTrue(ok, str(errs))

        called = []

        class _FG:
            def drop_focus(self, tag=None):
                called.append(tag)
                return {"ok": True, "was": "GER_coal_liquefaction"}

        res = TE._dispatch_action(_FG(), "drop_focus", {}, "GER")
        self.assertTrue(res["ok"])
        self.assertEqual(called, ["GER"])

    def test_autopilot_on_blocked_skip_defers_and_recovers_later(self):
        st = {"current_phase": "p1", "journal": []}
        st_cfg = {
            "focus": {
                "enabled": True,
                "must": ["GER_anschluss", "GER_coal_liquefaction"],
                "queue": [],
                "avoid": [],
                "on_blocked": "skip",
                "fallback": "none",
                "allow_exclusive_focus": False,
            },
            "interrupts_extra": {},
        }

        class _APG:
            def __init__(self, selectable):
                self._sel = selectable
                self.picked = []

            def current_focus(self, tag=None):
                return None

            def completed_focuses(self, tag=None):
                return []

            def selectable_focuses(self, tag=None):
                return {"selectable": [{"key": k, "id": k} for k in self._sel], "blocked": []}

            def focus_block_reason(self, k, tag=None):
                return "available trigger not met"

            def set_focus(self, k, tag=None):
                self.picked.append(k)
                return {"ok": True}

        # 第 1 次：GER_anschluss 暂被阻塞 -> 暂缓并先选 GER_coal_liquefaction
        g1 = _APG(["GER_coal_liquefaction"])
        r1 = AP._tick_focus(g1, st, st_cfg, {"tag": "GER", "date": "1937.03.31"})
        self.assertEqual(g1.picked, ["GER_coal_liquefaction"])
        self.assertIn("GER_anschluss", st.get("focus_deferred") or [])
        self.assertTrue(any("temporarily deferred" in str(j.get("detail")) for j in st["journal"]))

        # 第 2 次：GER_anschluss 变为可选 -> 自动优先恢复执行 GER_anschluss（绝未被永久跳过！）
        g2 = _APG(["GER_anschluss", "GER_coal_liquefaction"])
        r2 = AP._tick_focus(g2, st, st_cfg, {"tag": "GER", "date": "1937.10.04"})
        self.assertEqual(g2.picked, ["GER_anschluss"])
        self.assertNotIn("GER_anschluss", st.get("focus_deferred") or [])
        self.assertTrue(any(j.get("action") == "recovered_deferred:GER_anschluss" for j in st["journal"]))

    def test_focus_available_snippet_parses_real_script_or_gracefully_none(self):
        # 无论是否安装了本机游戏目录，调用不得抛异常
        snip = FocusMixin._focus_available_snippet("GER_anschluss")
        if snip is not None:
            self.assertIn("has_war", snip)


class TestD11D12D14D15D16TrainingAirParadropOrders(unittest.TestCase):
    """D-11 / D-12 / D-14 / D-15 / D-16 回归覆盖。"""

    def test_deploy_ready_default_threshold_is_0995_and_explicit_zero_honored(self):
        seen = []

        class _DG:
            def deploy_ready_conveyors(self, tag=None, min_progress=0.995):
                seen.append((tag, min_progress))
                return {"deployed": 0, "min_progress_used": min_progress}

        TE._dispatch_action(_DG(), "deploy_ready", {}, "GER")
        TE._dispatch_action(_DG(), "deploy_ready", {"min_progress": 0.0}, "GER")
        TE._dispatch_action(_DG(), "deploy_ready", {"min_progress": 0.2}, "GER")
        self.assertEqual(seen, [("GER", 0.995), ("GER", 0.0), ("GER", 0.2)])

    def test_train_divisions_series_accepted_in_plan_dsl(self):
        ok, errs = TE.validate_plan({
            "actions": [{"op": "train_divisions", "args": {"template": "Paratroopers", "lines": 6, "state": 64, "series": 1}}]
        })
        self.assertTrue(ok, str(errs))
        seen = []

        class _TG:
            def train_divisions(self, template_name=None, lines=2, state_id=None, tag=None, series=0):
                seen.append((template_name, lines, state_id, tag, series))
                return {"ok": True, "series": series}

        TE._dispatch_action(_TG(), "train_divisions", {"template": "Paratroopers", "lines": 6, "state": 64, "series": 1}, "GER")
        self.assertEqual(seen, [("Paratroopers", 6, 64, "GER", 1)])

    def test_delete_order_in_plan_dsl_locks_allow_child_false(self):
        self.assertIn("delete_order", TE.ALLOWED_ACTIONS)
        self.assertIn("delete_order", TE.ACTION_SPECS)
        self.assertEqual(TE.ALLOWED_ACTIONS, set(TE.ACTION_SPECS.keys()))
        ok, errs = TE.validate_plan({"actions": [{"op": "delete_order", "args": {"army": "第1集团军", "id": 27}}]})
        self.assertTrue(ok, str(errs))

        calls = []

        class _OG:
            def _resolve_orders_group(self, spec, tag=None):
                return (0x18E7697C240, "", []) if spec == "第1集团军" else (0, "not found", [])

            def delete_order(self, og, oid, allow_child=False):
                calls.append((og, oid, allow_child))
                return {"ok": True}

        r = TE._dispatch_action(_OG(), "delete_order", {"army": "第1集团军", "id": 27}, "GER")
        self.assertTrue(r["ok"])
        self.assertEqual(calls, [(0x18E7697C240, 27, False)])

    def test_paradrop_reports_in_transit_transports_and_divisions_source(self):
        p = _BufMem()
        og = 0x800000
        d1, d2 = 0x810000, 0x811000
        sd1, sd2 = 0x820000, 0x821000
        p.put(og + OW.ORDER_OG_HANDLE, struct.pack("<ii", 53, 1))
        for d, sd, hid in ((d1, sd1, 10), (d2, sd2, 11)):
            p.put(d + OW.ORDER_CARMY_HANDLE, struct.pack("<ii", 51, hid))
            p.put_u64(d + OW.ORDER_CARMY_TEMPLATE, sd)
            p.put_u8(sd + OW.ORDER_DT_CAN_PARADROP, 1)

        fake_src_meta = {
            "ok": True,
            "airbase_scan_ok": True,
            "range_km": 1000.0,
            "total_transport_planes": 100,
            "transport_wings": [
                {
                    "id": 5, "type": "transport_plane", "planes": 100,
                    "air_base_state": 64, "air_base_province": 9496,
                    "transferring": True, "transferring_to_state": 51, "transferring_to_province": 6570,
                }
            ],
            "sources": [{"state_id": 51, "province": 6570, "all_provinces": [6570], "transport_planes": 0, "available_paradrop_planes": 0}],
            "ready_sources": [{"state_id": 64, "province": 9496, "available_paradrop_planes": 100}],
        }

        class _PG:
            def __init__(self):
                self.p = p

        with mock.patch.object(sys, "platform", "win32"), \
             mock.patch.object(AOW, "validate_orders_group_ptr", return_value=(True, "", {})), \
             mock.patch.object(AOW, "divisions_of", return_value=[d1, d2]), \
             mock.patch.object(AOW, "validate_division_ptrs", return_value=([d1, d2], [])), \
             mock.patch.object(AOW, "paradrop_sources", return_value=fake_src_meta), \
             mock.patch("mapdata.load_definitions", return_value=({}, {11506: "land"})), \
             mock.patch("mapdata.province_distance_km", return_value=420.0):
            ok, err, info = AOW.paradrop(_PG(), og, source_province=6570, target_province=11506, dry_run=True)

        self.assertFalse(ok)
        self.assertEqual(info["blocked_by"], "insufficient_transports_at_source")
        self.assertEqual(info["divisions_source"], "army_all_paratroopers")
        self.assertEqual(info["divisions_to_assign"], 2)
        self.assertEqual(len(info["division_ptrs_to_assign"]), 2)
        self.assertEqual(len(info.get("in_transit_to_source") or []), 1)
        self.assertIn("转场途中", err)


if __name__ == "__main__":
    unittest.main()
