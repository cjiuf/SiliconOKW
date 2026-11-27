# -*- coding: utf-8 -*-
"""2026-11-27 审查修复专项离线单元测试。

覆盖以下核心修复（全部 0 注入、0 活进程、跨平台可运行）：
  1. armyops_win.can_division_paradrop：严格走引擎真值链 CArmy+0x138 -> CSubUnitDefinition+0x5B5，
     绝不越界读 CArmy+0x3B8 (CReferencedDivisionTemplate, 仅 0x270 字节)，绝不用名字启发式覆盖真值 0。
  2. domains/air_navy.py AirNavyMixin.air_mission：
     - mask=0 清零走正规命令（flag1=0, flag2=1），不再因传错 flag2 触发 CanExecute 拒绝；
     - CanExecute 拒绝（如 tac_bomber 挂 air_superiority）严禁绕过规则直写内存，仅纯只读汇报合法任务；
     - 透传 tag 给 select_wings，前置拦截负数 mask。
  3. turnengine.py Plan DSL 与 inspect 清理：
     - 删除存在堆越界写风险的 fix_paradrop_flag / paradrop_flag_by_template 与调试脚手架 paradrop_diag；
     - 将只读模板查询迁入 ALLOWED_INSPECT_TOPICS["templates"]（附基于真实 +0x138+0x5B5 的可伞降师统计）；
     - duplicate_template / edit_template 支持 int_or_str 索引定位、静态拦截空编辑并透传 tag；
     - 锁定 ALLOWED_ACTIONS 与 ACTION_SPECS 严格 1:1 同步，验证 add_to_army 散开扩参透传。
"""
from __future__ import annotations

import inspect
import os
import struct
import sys
import types
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CALLING = os.path.join(ROOT, "calling")
for _p in (ROOT, CALLING):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import airops_win as AW
import armyops_win as AOW
import divreserve as DR
import offsets as O
import offsets_win as OW
import templedit as TE_MOD
import turnengine as TE
from domains.air_navy import AirNavyMixin


class _MemMock:
    """最小内存读/写桩，记录所有 write 调用用于断言零非法内存写。"""

    def __init__(self, u64_map=None, u32_map=None, u8_map=None, raw_map=None, base=0x140000000):
        self.base = base
        self.pid = 42424
        self._u64 = dict(u64_map or {})
        self._u32 = dict(u32_map or {})
        self._u8 = dict(u8_map or {})
        self._raw = dict(raw_map or {})
        self.writes = []

    def u64(self, addr):
        if addr in self._u64:
            return self._u64[addr]
        if addr in self._raw and len(self._raw[addr]) >= 8:
            return struct.unpack_from("<Q", self._raw[addr], 0)[0]
        return 0

    def u32(self, addr):
        if addr in self._u32:
            return self._u32[addr]
        if addr in self._raw and len(self._raw[addr]) >= 4:
            return struct.unpack_from("<I", self._raw[addr], 0)[0]
        return 0

    def i32(self, addr):
        return struct.unpack("<i", struct.pack("<I", self.u32(addr) & 0xFFFFFFFF))[0]

    def u8(self, addr):
        if addr in self._u8:
            return self._u8[addr]
        if addr in self._raw and len(self._raw[addr]) >= 1:
            return self._raw[addr][0]
        raise KeyError(hex(addr))

    def read(self, addr, size):
        if addr in self._raw:
            return self._raw[addr][:size]
        raise KeyError((hex(addr), size))

    def write(self, addr, data):
        self.writes.append((addr, bytes(data)))


class TestCanDivisionParadrop(unittest.TestCase):
    """验证 can_division_paradrop 严格读取 CArmy+0x138 -> CSubUnitDefinition+0x5B5。"""

    def test_true_and_false_strictly_from_carmy_0x138(self):
        div_para = 0x20000000
        subdef_para = 0x21000000
        ref_custom = 0x22000000  # 普通/自定义名模板，且越界处故意填垃圾字节 0x66

        div_broken_fallschirm = 0x30000000
        subdef_broken = 0x31000000
        ref_fallschirm = 0x32000000  # 名字含 Fallschirm 且越界处故意填 1，但引擎真值 +0x138+0x5B5 == 0

        p = _MemMock(
            u64_map={
                div_para + OW.ORDER_CARMY_TEMPLATE: subdef_para,
                div_para + DR.CDIV_TEMPLATE: ref_custom,
                div_broken_fallschirm + OW.ORDER_CARMY_TEMPLATE: subdef_broken,
                div_broken_fallschirm + DR.CDIV_TEMPLATE: ref_fallschirm,
            },
            u8_map={
                subdef_para + OW.ORDER_DT_CAN_PARADROP: 1,
                ref_custom + 0x18 + OW.ORDER_DT_CAN_PARADROP: 0x66,
                subdef_broken + OW.ORDER_DT_CAN_PARADROP: 0,
                ref_fallschirm + OW.ORDER_DT_CAN_PARADROP: 1,
                ref_fallschirm + 0x18 + OW.ORDER_DT_CAN_PARADROP: 1,
            },
        )

        self.assertIs(AOW.can_division_paradrop(p, div_para), True)
        self.assertIs(
            AOW.can_division_paradrop(p, div_broken_fallschirm),
            False,
            "引擎聚合 CSubUnitDefinition+0x5B5 == 0 时必须诚实返回 False，绝不可被模板名或越界字节覆盖",
        )

    def test_illegal_byte_or_invalid_ptr_returns_none_or_false(self):
        div_bad_byte = 0x40000000
        subdef_bad = 0x41000000
        div_no_subdef = 0x42000000
        p = _MemMock(
            u64_map={
                div_bad_byte + OW.ORDER_CARMY_TEMPLATE: subdef_bad,
                div_no_subdef + OW.ORDER_CARMY_TEMPLATE: 0,
            },
            u8_map={
                subdef_bad + OW.ORDER_DT_CAN_PARADROP: 0x66,
            },
        )
        self.assertIs(AOW.can_division_paradrop(p, 0), False)
        self.assertIsNone(AOW.can_division_paradrop(p, div_bad_byte))
        self.assertIsNone(AOW.can_division_paradrop(p, div_no_subdef))

    def test_source_has_no_oob_0x3b8_or_name_heuristic(self):
        import ast
        src = inspect.getsource(AOW.can_division_paradrop)
        tree = ast.parse(src)
        fn = tree.body[0]
        # 剔除 docstring 后仅检查可执行 AST 节点中的常量
        body_nodes = fn.body[1:] if (
            fn.body and isinstance(fn.body[0], ast.Expr)
            and isinstance(getattr(fn.body[0], "value", None), ast.Constant)
            and isinstance(fn.body[0].value.value, str)
        ) else fn.body
        consts = []
        for stmt in body_nodes:
            for node in ast.walk(stmt):
                if isinstance(node, ast.Constant):
                    consts.append(node.value)
        self.assertNotIn(0x3B8, consts, "可执行体不得硬编码 CReferencedDivisionTemplate 偏移 0x3B8")
        str_consts = [c.lower() for c in consts if isinstance(c, str)]
        for bad in ("fallschirm", "paratroop", "伞兵"):
            self.assertFalse(any(bad in s for s in str_consts), f"可执行体不得残留模板名启发式 {bad!r}")


class _FakeAirGame(AirNavyMixin):
    def __init__(self, p):
        self.p = p

    def _c(self, tag=None):
        return types.SimpleNamespace(tag=str(tag or "GER").upper(), tag_id=1)


class TestAirMissionRuleGate(unittest.TestCase):
    """验证 air_mission 走正规命令清零、且 CanExecute 拒绝时绝不绕过规则硬写内存。"""

    def test_clear_mission_mask_zero_passes_flag1_0_flag2_1_and_tag(self):
        wptr = 0x50000000
        p = _MemMock(u32_map={wptr + OW.AIR_WING_ALLOWED_MISSIONS: 0x0440})
        g = _FakeAirGame(p)
        captured = {}

        def _fake_select(game, spec, tag=None):
            captured["select"] = (spec, tag)
            return [(5, (5 << 32) | 69)]

        def _fake_make(game, handles, mask, f1, f2):
            captured["make"] = (list(handles), mask, f1, f2)
            return 0x60000000

        def _fake_cw(game, tag=None):
            return {
                "ok": True,
                "wings": [{"id": 5, "ptr": hex(wptr), "type": "transport_plane", "mission": 0, "mission_on": False}],
            }

        with mock.patch.object(sys, "platform", "win32"), \
             mock.patch.object(AW, "select_wings", side_effect=_fake_select), \
             mock.patch.object(AW, "make_set_mission", side_effect=_fake_make), \
             mock.patch.object(AW, "can_exec", return_value=True), \
             mock.patch.object(AW, "post", return_value={"posted": 1, "drained": True}) as m_post, \
             mock.patch.object(AW, "country_wings", side_effect=_fake_cw):
            res = g.air_mission(wings="transport", mission=0, tag="GER")

        self.assertTrue(res["ok"])
        self.assertEqual(captured["select"], ("transport", "GER"))
        self.assertEqual(captured["make"][1:], (0, 0, 1), "mask=0 必须传 flag1=0, flag2=1 (clear_all)")
        m_post.assert_called_once()

    def test_can_execute_refused_never_writes_memory_and_reports_allowed_missions(self):
        wptr = 0x51000000
        # 实测德国 tac_bomber 的 CAirWing+0x30 = 0x091A（不含 bit0 = air_superiority）
        p = _MemMock(u32_map={wptr + OW.AIR_WING_ALLOWED_MISSIONS: 0x091A})
        g = _FakeAirGame(p)

        def _fake_cw(game, tag=None):
            return {
                "ok": True,
                "wings": [{"id": 3, "ptr": hex(wptr), "type": "tac_bomber", "mission": 0, "mission_on": False}],
            }

        with mock.patch.object(sys, "platform", "win32"), \
             mock.patch.object(AW, "select_wings", return_value=[(3, (3 << 32) | 69)]), \
             mock.patch.object(AW, "make_set_mission", return_value=0x61000000), \
             mock.patch.object(AW, "can_exec", return_value=False), \
             mock.patch.object(AW, "post") as m_post, \
             mock.patch.object(AW, "country_wings", side_effect=_fake_cw):
            res = g.air_mission(wings="3", mission="air_superiority", tag="GER")

        self.assertFalse(res["ok"])
        self.assertFalse(res["can_execute"])
        self.assertEqual(res["blocked_by"], "mission_not_allowed")
        self.assertEqual(p.writes, [], "CanExecute 拒绝时严禁绕过引擎规则直接写进程内存")
        m_post.assert_not_called()
        self.assertEqual(len(res["incompatible_wings"]), 1)
        inc = res["incompatible_wings"][0]
        self.assertEqual(inc["type"], "tac_bomber")
        self.assertIn("cas", inc["allowed_missions"])
        self.assertIn("strategic_bombing", inc["allowed_missions"])
        self.assertNotIn("air_superiority", inc["allowed_missions"])
        self.assertIn("cannot perform", res["error"])

    def test_negative_mask_rejected_before_command(self):
        p = _MemMock()
        g = _FakeAirGame(p)
        with mock.patch.object(sys, "platform", "win32"), \
             mock.patch.object(AW, "make_set_mission") as m_make:
            res = g.air_mission(wings="all", mission=-1)
        self.assertFalse(res["ok"])
        self.assertEqual(res["blocked_by"], "invalid_mission")
        m_make.assert_not_called()
        self.assertEqual(p.writes, [])

    def test_decode_mission_mask_and_offsets_single_source(self):
        self.assertEqual(O.AIR_WING_ALLOWED_MISSIONS, 0x30)
        self.assertEqual(AW.WING_ALLOWED_MISSIONS, 0x30)
        fighter_ms = AW.decode_mission_mask(0x0805)
        self.assertIn("air_superiority", fighter_ms)
        self.assertIn("interception", fighter_ms)
        self.assertIn("training", fighter_ms)
        tac_ms = AW.decode_mission_mask(0x091A)
        self.assertNotIn("air_superiority", tac_ms)
        self.assertIn("cas", tac_ms)
        self.assertIn("strategic_bombing", tac_ms)
        self.assertIn("naval_strike", tac_ms)


class TestPlanDslCleanupAndTemplatesInspect(unittest.TestCase):
    """验证 Plan DSL 清理、模板编辑/复制参数与只读 inspect(topic='templates')。"""

    def test_dangerous_and_readonly_ops_removed_and_specs_synced(self):
        for removed in ("fix_paradrop_flag", "paradrop_flag_by_template", "paradrop_diag", "template_info"):
            self.assertNotIn(removed, TE.ALLOWED_ACTIONS)
            self.assertNotIn(removed, TE.ACTION_SPECS)
            ok, errs = TE.validate_plan({"actions": [{"op": removed, "args": {}}]})
            self.assertFalse(ok)
            self.assertTrue(any(removed in e for e in errs))
        self.assertEqual(
            TE.ALLOWED_ACTIONS,
            set(TE.ACTION_SPECS.keys()),
            "ALLOWED_ACTIONS 与 ACTION_SPECS 必须严格 1:1 同步",
        )
        self.assertIn("templates", TE.ALLOWED_INSPECT_TOPICS)

    def test_duplicate_and_edit_template_int_or_str_validation_and_dispatch(self):
        # 1. 支持整型 index 定位
        ok_dup, errs_dup = TE.validate_plan({
            "actions": [
                {"op": "duplicate_template", "args": {"source": 0, "name": "Para 2", "dry_run": True}},
                {"op": "edit_template", "args": {"template": 0, "line_counts": {"infantry": 9}, "dry_run": True}},
            ]
        })
        self.assertTrue(ok_dup, f"应放行整型 index 定位: {errs_dup}")

        # 2. edit_template 同时缺少 line 和 support 在静态校验期即被拦截
        ok_empty, errs_empty = TE.validate_plan({
            "actions": [{"op": "edit_template", "args": {"template": "Infanterie-Division", "dry_run": True}}]
        })
        self.assertFalse(ok_empty)
        self.assertTrue(any("line" in e and "support" in e for e in errs_empty))

        # 3. dispatch 正确透传 tag 给 templedit.edit_template 与 duplicate_division_template
        class _DummyG:
            def __init__(self):
                self.dup_calls = []

            def division_templates(self, tag=None):
                return [{"index": 0, "name": "Infanterie-Division", "ptr": "0x12340000"}]

            def duplicate_division_template(self, source, tag=None, new_name=None, dry_run=False):
                self.dup_calls.append((source, tag, new_name, dry_run))
                return {"ok": True, "source": source, "new_name": new_name, "dry_run": dry_run}

        g = _DummyG()
        r_dup = TE._dispatch_action(g, "duplicate_template", {"source": 0, "name": "TestCopy", "dry_run": True}, "GER")
        self.assertTrue(r_dup["ok"])
        self.assertEqual(g.dup_calls, [("0", "GER", "TestCopy", True)])

        with mock.patch.object(TE_MOD, "edit_template", return_value={"ok": True, "dry_run": True}) as m_edit:
            r_ed = TE._dispatch_action(
                g,
                "edit_template",
                {"template": 0, "line_counts": {"infantry": 9}, "support_counts": {"engineer": 1}, "dry_run": True},
                "GER",
            )
        self.assertTrue(r_ed["ok"])
        m_edit.assert_called_once_with(g, 0x12340000, {"infantry": 9}, True, {"engineer": 1}, tag="GER")

    def test_add_to_army_spread_and_province_alias(self):
        ok, errs = TE.validate_plan({
            "actions": [
                {
                    "op": "add_to_army",
                    "args": {
                        "army": "0x1000",
                        "count": 4,
                        "province": 6521,
                        "spread": False,
                        "max_per_province": 2,
                    },
                }
            ]
        })
        self.assertTrue(ok, f"add_to_army 扩参应通过校验: {errs}")

        calls = []

        class _ArmyG:
            def add_divisions_to_army(self, **kw):
                calls.append(kw)
                return {"ok": True}

        res = TE._dispatch_action(
            _ArmyG(),
            "add_to_army",
            {"army": "0x1000", "count": 4, "province": 6521, "spread": False, "max_per_province": 2},
            "GER",
        )
        self.assertTrue(res["ok"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["orders_group"], "0x1000")
        self.assertEqual(calls[0]["move_to_province"], 6521)
        self.assertIs(calls[0]["spread"], False)
        self.assertEqual(calls[0]["max_per_province"], 2)

    def test_inspect_templates_readonly_counts_real_paradrop_divisions(self):
        co_ptr = 0x70000000
        arr_data = 0x70010000
        ref_para_ok = 0x71000000
        ref_para_broken = 0x71000270
        d1, d2, d3 = 0x72000000, 0x72001000, 0x72002000
        sd1, sd2, sd3 = 0x73000000, 0x73001000, 0x73002000

        # CO_ARMIES CPdxArray<CArmy*> {data, cap, size}
        p = _MemMock(
            u64_map={
                co_ptr + OW.CO_ARMIES: arr_data,
                arr_data + 0: d1,
                arr_data + 8: d2,
                arr_data + 16: d3,
                d1 + DR.CDIV_TEMPLATE: ref_para_ok,
                d2 + DR.CDIV_TEMPLATE: ref_para_ok,
                d3 + DR.CDIV_TEMPLATE: ref_para_broken,
                d1 + OW.ORDER_CARMY_TEMPLATE: sd1,
                d2 + OW.ORDER_CARMY_TEMPLATE: sd2,
                d3 + OW.ORDER_CARMY_TEMPLATE: sd3,
            },
            u32_map={
                co_ptr + OW.CO_ARMIES + 8: 4,
                co_ptr + OW.CO_ARMIES + 12: 3,
            },
            u8_map={
                sd1 + OW.ORDER_DT_CAN_PARADROP: 1,
                sd2 + OW.ORDER_DT_CAN_PARADROP: 1,
                sd3 + OW.ORDER_DT_CAN_PARADROP: 0,  # 名字含 Fallschirm 但引擎真值为 0
            },
            raw_map={
                co_ptr + OW.CO_ARMIES: struct.pack("<Qii", arr_data, 4, 3),
                arr_data: struct.pack("<QQQ", d1, d2, d3),
            },
        )

        class _InspectG:
            def __init__(self):
                self.p = p

            def player_tag(self):
                return "GER"

            def _c(self, tag=None):
                return types.SimpleNamespace(tag="GER", ptr=co_ptr)

            def template_composition(self, tag=None, only=None):
                all_rows = [
                    {
                        "index": 0,
                        "name": "Airborne Elite",
                        "line": {"paratrooper": 6},
                        "support": {"engineer": 1},
                        "ptr": hex(ref_para_ok),
                    },
                    {
                        "index": 1,
                        "name": "Fallschirm-Heavy",
                        "line": {"paratrooper": 6, "artillery_brigade": 1},
                        "support": {},
                        "ptr": hex(ref_para_broken),
                    },
                ]
                if only is None:
                    return all_rows
                return [r for r in all_rows if str(only) in (str(r["index"]), r["name"])]

        g = _InspectG()
        res = TE.inspect_topics(g, [{"topic": "templates"}], tag="GER")
        self.assertTrue(res["ok"])
        item = res["results"][0]["data"]
        self.assertTrue(item["ok"])
        self.assertEqual(item["count"], 2)
        by_name = {r["name"]: r for r in item["templates"]}
        self.assertEqual(by_name["Airborne Elite"]["divisions_using"], 2)
        self.assertEqual(by_name["Airborne Elite"]["paradrop_capable_divisions"], 2)
        self.assertEqual(by_name["Fallschirm-Heavy"]["divisions_using"], 1)
        self.assertEqual(by_name["Fallschirm-Heavy"]["paradrop_capable_divisions"], 0)
        self.assertEqual(p.writes, [], "templates inspect 必须全程 0 写入")

        # 测试 filter 过滤
        res_flt = TE.inspect_topics(g, [{"topic": "templates", "filter": "airborne"}], tag="GER")
        self.assertEqual(res_flt["results"][0]["data"]["count"], 1)
        self.assertEqual(res_flt["results"][0]["data"]["templates"][0]["name"], "Airborne Elite")


if __name__ == "__main__":
    unittest.main()
