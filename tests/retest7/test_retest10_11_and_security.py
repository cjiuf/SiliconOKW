# -*- coding: utf-8 -*-
import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from tests.retest7._common import (  # noqa: F401
    AW, DIP, HK, MC, MCP, PW, RD, ROOT, SS, TE, TL,
    _MemProc, check, cmdwin, counts, FAILURES, game_mod,
    json, mock, reset_counts, struct, tempfile, time, types,
)


# ===================================================================== 13. 复测 10 全 12 项专测 (A-1~A-4, B-1~B-8)
def test_retest10_all_fixes():
    print("\n== 13. 复测 10 (A-1~A-4, B-1~B-8): 倒挂删单拆弹 / 进攻线敌区匹配 / 敌国前线隔离 / B组全部缺陷 ==")
    import armyops_win as AW
    import events_win as EW
    import templedit as TE
    import macros as MC
    import reddots as RD
    import housekeep as HK
    import movemicro as MM
    import frontd as FD
    from process import winject as WJ
    import server as MCP
    import game as GM

    class _MemProc10:
        def __init__(self, base=0x7FF600000000):
            self.base = base
            self.pid = 32488
            self.mem = {}

        def va(self, rva):
            return self.base + rva

        def put(self, addr, raw):
            for i, b in enumerate(raw):
                self.mem[addr + i] = b

        def put_u64(self, addr, v):
            self.put(addr, struct.pack("<Q", v))

        def put_i32(self, addr, v):
            self.put(addr, struct.pack("<i", v))

        def read(self, addr, n):
            return bytes(self.mem.get(addr + i, 0) for i in range(n))

        def write(self, addr, raw):
            self.put(addr, raw)

        def u64(self, addr):
            return struct.unpack("<Q", self.read(addr, 8))[0]

        def i32(self, addr):
            return struct.unpack("<i", self.read(addr, 4))[0]

        def i64(self, addr):
            return struct.unpack("<q", self.read(addr, 8))[0]

        def u8(self, addr):
            return self.read(addr, 1)[0]

    # --- A-2: manual_offensive 参数顺序 (root, child) + 倒挂孤儿识别与 0x10B5D50 安全拆弹 ---
    p = _MemProc10()
    og = 0x15d10000000
    root_ptr = 0x15d10001000
    kid_ptr = 0x15d10002000
    # 构造 og+0x98 -> [root_ptr]
    og_ord_arr = 0x15d10003000
    p.put_u64(og_ord_arr, root_ptr)
    p.put_u64(og + AW.OG_ORDERS, og_ord_arr)
    p.put_i32(og + AW.OG_ORDERS + 8, 1)
    p.put_i32(og + AW.OG_ORDERS + 12, 1)
    # root: type=2, id=39, +0x1E0 (Parents) 误挂着倒挂孤儿 kid_ptr，+0x1F8 (Children) 为空
    p.put_u64(root_ptr, p.base + AW.VT_ORDER_INSTANCE)
    p.put_i32(root_ptr + AW.OI_TYPE, 2)
    p.put_i32(root_ptr + AW.OI_ID, 39)
    inv_arr = 0x15d10003100
    p.put_u64(inv_arr, kid_ptr)
    p.put_u64(root_ptr + 0x1E0, inv_arr)
    p.put_i32(root_ptr + 0x1E0 + 8, 1)
    p.put_i32(root_ptr + 0x1E0 + 12, 1)
    # kid: type=1, id=40, assigned=0, executing=1
    p.put_u64(kid_ptr, p.base + AW.VT_ORDER_INSTANCE)
    p.put_i32(kid_ptr + AW.OI_TYPE, 1)
    p.put_i32(kid_ptr + AW.OI_ID, 40)
    p.put_i32(kid_ptr + AW.OI_ASSIGNED + 0xC, 0)
    p.put(kid_ptr + AW.OI_EXECUTING, b"\x01")

    g_a2 = types.SimpleNamespace(p=p, _inj=object())
    kids_read = AW.order_children(g_a2, og)
    check("A-2 order_children 精准标注旧版倒挂孤儿 inverted_parent_link=True",
          len(kids_read) == 1 and kids_read[0]["id"] == 40 and kids_read[0].get("inverted_parent_link") is True,
          kids_read)

    # 验证 delete_order 删父订单 39 时调用 0x10B5D50 解绑倒挂孤儿 40，然后原生删 39 成功
    a2_calls = []
    def _fake_call_a2(addr, args=(), **kw):
        a2_calls.append((addr - p.base, args))
        if addr == p.base + AW.FN_ORDER_DETACH_ALL:
            # 模拟 0x10B5D50 清空 root+0x1E0 链接
            p.put_i32(root_ptr + 0x1E0 + 12, 0)
            return 0
        return 1
    g_a2.call = _fake_call_a2
    orig_post_del = AW._post_delete_order_cmd
    orig_set_exe = AW.set_order_executing
    try:
        def _mock_post_del(g_, og_, oid_):
            if oid_ == 39:
                p.put_i32(og + AW.OG_ORDERS + 12, 0)
                return True, ""
            return False, "COrderDeleteCommand::CanExecute refused (no such order %d)" % oid_
        AW._post_delete_order_cmd = _mock_post_del
        AW.set_order_executing = lambda g_, c_, on=False: p.put(c_ + AW.OI_EXECUTING, b"\x01" if on else b"\x00")
        r_del39 = AW.delete_order(g_a2, og, 39)
        check("A-2 delete_order 遇倒挂孤儿 40 自动 0x10B5D50 解绑并成功删除父订单 39",
              r_del39["ok"] and 40 in r_del39.get("deleted_children", [])
              and 40 in r_del39.get("detached_orphans", [])
              and any(rva == AW.FN_ORDER_DETACH_ALL and a == (kid_ptr,) for rva, a in a2_calls),
              (r_del39, a2_calls))
    finally:
        AW._post_delete_order_cmd = orig_post_del
        AW.set_order_executing = orig_set_exe

    # 验证 manual_offensive 调用 0x10B4EC0 时实参顺序已修正为 (parent=root, child=o)
    p.put_i32(og + AW.OG_ORDERS + 12, 1)
    mo_calls = []
    new_kid = 0x15d10002500
    p.put_u64(new_kid, p.base + AW.VT_ORDER_INSTANCE)
    def _fake_call_mo(addr, args=(), **kw):
        mo_calls.append((addr - p.base, args))
        if addr == p.base + AW.FN_ORDER_FACTORY:
            return new_kid
        return 0
    g_mo = types.SimpleNamespace(p=p, _inj=object(), call=_fake_call_mo)
    orig_mkint = AW._mkintarray
    try:
        AW._mkintarray = lambda g_, vs: 0x15d10009000
        mok, merr, moptr = AW.manual_offensive(g_mo, og, 39, [68, 391])
        sp_call = [a for rva, a in mo_calls if rva == AW.FN_ORDER_SETPARENT]
        check("A-2 manual_offensive 以正确顺序 (root, child) 调用 0x10B4EC0",
              mok and sp_call == [(root_ptr, new_kid)],
              (mok, merr, sp_call))
    finally:
        AW._mkintarray = orig_mkint

    # --- A-1 & A-3: area_matches_front (0xD57D90 连通敌区) + offensive_line 严禁 0 师假阳性 ---
    th = 0x15d10004000
    p.put_u64(og + AW.OG_THEATRE, th)
    p.put_i32(th + 0x48, 1)  # GER idx=1
    comp_allies = 0x15d10005000
    area_lux, area_hol, area_ger = 0x15d10006000, 0x15d10006200, 0x15d10006400
    for idx_a, ap in enumerate((area_lux, area_hol)):
        e0_buf = 0x15d10007000 + idx_a * 0x40
        c8_buf = 0x15d10007100 + idx_a * 0x40
        p.put_i32(e0_buf + 0, -1)
        p.put_i32(e0_buf + 4, 0)  # idx=1 -> slot 0
        p.put_u64(c8_buf, comp_allies)
        p.put_u64(ap + 0xE0, e0_buf)
        p.put_i32(ap + 0xEC, 2)
        p.put_u64(ap + 0xC8, c8_buf)
        p.put_i32(ap + 0xD4, 1)
    p.put_i32(area_ger + 0xEC, 2)
    p.put_i32(area_ger + 0xD4, 0)
    check("A-1/A-3 area_matches_front 放行同连通敌区 (LUX<->HOL)，拒绝己方省 (GER)",
          AW.area_matches_front(p, area_lux, area_hol, og=og) is True
          and AW.area_matches_front(p, area_lux, area_ger, og=og) is False,
          (AW.area_matches_front(p, area_lux, area_hol, og=og),
           AW.area_matches_front(p, area_lux, area_ger, og=og)))

    # Game.offensive_line 对 0 师子命令必须返回 ok=False (绝不假阳性)
    orig_eng_off = AW.engine_offensive
    orig_asg_of = AW.assigned_of
    try:
        AW.engine_offensive = lambda g_, og_, pid_, provs_, divs_=None, enemy_tag=None, depth=1: (
            True, "", {"children": [{"id": 99, "root": 39, "type": 1, "assigned": 0, "executing": 1}]}
        )
        AW.assigned_of = lambda g_, og_, oid_: 22
        g_off = object.__new__(GM.Game)
        r_zero = g_off.offensive_line(og, 39, [68], enemy_tag="HOL")
        check("A-1 Game.offensive_line 子命令 assigned==0 时强制返回 ok=False",
              r_zero["ok"] is False and r_zero.get("blocked_by") == "zero_assigned_offensive_child",
              r_zero)
        AW.engine_offensive = lambda g_, og_, pid_, provs_, divs_=None, enemy_tag=None, depth=1: (
            True, "", {"children": [{"id": 99, "root": 39, "type": 1, "assigned": 22, "executing": 1}]}
        )
        r_fed = g_off.offensive_line(og, 39, [68], enemy_tag="HOL")
        check("A-1 Game.offensive_line 子命令 assigned>0 时返回 ok=True",
              r_fed["ok"] is True and r_fed.get("children_fed") == [99],
              r_fed)
    finally:
        AW.engine_offensive = orig_eng_off
        AW.assigned_of = orig_asg_of

    # --- A-4: plan_front 按敌国子区间切片 + 严禁动马奇诺方向集团军 ---
    og_hol = 0x15d10010000
    og_fra = 0x15d10010200
    ord_hol_p = 0x15d10011000
    ord_fra_p = 0x15d10011200
    p.put(og_hol + AW.OG_IS_GROUP, b"\x00")
    p.put(og_fra + AW.OG_IS_GROUP, b"\x00")
    # ord_hol 面向 HOL 省 3444, 6622, 11346; ord_fra 面向 FRA 省 11640, 6712, 6542
    for op_p, pvs in ((ord_hol_p, [3444, 6622, 11346]), (ord_fra_p, [11640, 6712, 6542])):
        buf = op_p + 0x100
        for i_, pv_ in enumerate(pvs):
            p.put_i32(buf + 4 * i_, pv_)
        p.put_u64(op_p + 0x70, buf)
        p.put_i32(op_p + 0x7C, len(pvs))
    import armyops as AO
    orig_ao_ogs = AO.orders_groups
    orig_ft = AW.fronts_towards
    orig_tp = AW.target_provinces
    orig_aw_ords = AW.orders
    orig_aw_divs = AW.divisions_of
    try:
        AO.orders_groups = lambda g_, c_: [og_hol, og_fra]
        AW.fronts_towards = lambda g_, et_, own_=None: [{
            "front": 0x1cddbee79d0, "front_handle": [66, 10], "section": 1,
            "border_provinces": [3444, 6622, 11346, 9281, 11264, 11360],
            "section_provinces": [11640, 6712, 6542, 3444, 6622, 11346],
            "slice_from": 63636, "slice_to": 100000,
        }]
        AW.target_provinces = lambda g_, et_, depth=1, own_tag=None: [68, 391, 3211]
        AW.orders = lambda g_, o_: (
            [{"id": 37, "type": 2, "ptr": hex(ord_hol_p), "root_front": [66, 10], "root_section": 1, "from": 66666, "to": 100000}]
            if o_ == og_hol else
            [{"id": 46, "type": 2, "ptr": hex(ord_fra_p), "root_front": [66, 10], "root_section": 1, "from": 0, "to": 50000}]
        )
        AW.divisions_of = lambda g_, o_: [0x1] * 12
        g_pf = object.__new__(GM.Game)
        g_pf.p = p
        g_pf._c = lambda tag=None: types.SimpleNamespace(tag="GER")
        g_pf.wars = lambda tag=None: {"enemies": ["HOL", "FRA"]}
        g_pf._supply_front_guard = lambda et, tag=None: {"ok": True, "capacity": 50}
        g_pf.divisions = lambda tag=None: {"divisions": []}
        r_pf = g_pf.plan_front("HOL", dry_run=True)
        plan_rows = (r_pf.get("steps") or {}).get("plan") or []
        check("A-4 plan_front('HOL') 仅选中面向 HOL 的集团军且切片落在 [63636,100000]，不动马奇诺军",
              r_pf["ok"] and len(plan_rows) == 1
              and plan_rows[0]["army"] == hex(og_hol)
              and plan_rows[0]["from"] == 63636 and plan_rows[0]["to"] == 100000,
              r_pf)
    finally:
        AO.orders_groups = orig_ao_ogs
        AW.fronts_towards = orig_ft
        AW.target_provinces = orig_tp
        AW.orders = orig_aw_ords
        AW.divisions_of = orig_aw_divs

    # --- B-1: hoi4_combat_report 默认 compact=True 无 deprecated 副本且 top-10 ---
    import combat as CB
    orig_cb_c, orig_cb_a, orig_cb_adc, orig_mcp_g = CB.combats, CB.activity, CB.active_division_combats, MCP.game
    try:
        big_255 = [{"tag": "GER", "division": hex(0x1000 + i), "org_damage": 255 - i} for i in range(255)]
        CB.combats = lambda g_, only_mine=True: []
        CB.activity = lambda g_, limit=15: {"count": 0, "first_sample": True, "divisions": []}
        CB.active_division_combats = lambda g_, limit=10: {
            "count": 255, "tags": ["GER"], "basis": "org_vs_template_max", "divisions": big_255[:limit]
        }
        MCP.game = lambda: types.SimpleNamespace(player_tag=lambda: "GER")
        r_cr = MCP._combat_report({})
        check("B-1 hoi4_combat_report 默认 compact=True 截断 top-10 且不输出 deprecated active_division_combats",
              "active_division_combats" not in r_cr
              and r_cr["compact"] is True
              and len(r_cr["low_org_divisions"]["divisions"]) == 10
              and r_cr["low_org_divisions"]["truncated"] is True,
              {k: (len(v) if isinstance(v, list) else v) for k, v in r_cr.items() if k != "low_org_divisions"})
    finally:
        CB.combats, CB.activity, CB.active_division_combats, MCP.game = orig_cb_c, orig_cb_a, orig_cb_adc, orig_mcp_g

    # --- B-2: set_production_amount 遇单舰船坞上限缩步打满 9->10 并报 hit_line_cap ---
    line_ptr = 0x15d10020000
    prod_ptr = 0x15d10021000
    lines_arr = 0x15d10022000
    c_ptr = 0x15d10023000
    p.put_u64(c_ptr + GM.CO_PRODUCTION, prod_ptr)
    p.put_u64(lines_arr, line_ptr)
    p.put_u64(prod_ptr + GM.PS_MIL_LINES, lines_arr)
    p.put_i32(prod_ptr + GM.PS_MIL_LINES + 8, 1)
    p.put_i32(prod_ptr + GM.PS_MIL_LINES + 12, 1)
    p.put_i32(line_ptr + GM.ML_FACTORIES, 9)
    p.put_i32(line_ptr + 0xE8, 9)
    g_b2 = object.__new__(GM.Game)
    g_b2.p = p
    g_b2._c = lambda tag=None: types.SimpleNamespace(tag="GER", ptr=c_ptr)
    g_b2.handle_of = lambda ptr_: {"type": 57, "id": 196}
    g_b2.factory_allocation = lambda tag=None: {
        "source": "engine_production_status", "naval_idle": 3, "naval_total_effective": 21,
        "naval_available": 21, "naval_used": 18, "naval_build_used": 18,
        "naval_refit_used": 0, "naval_repair_dockyards": 0, "naval_raw_buildings": 21,
    }
    g_b2.production_lines = lambda tag=None, include_obsolete=True: [{"index": 0, "equipment": "ship_hull_light_2"}]
    g_b2._settle = lambda s=0.1: None
    def _mock_send_b2(cmd_name, fields, tag=None):
        st = int(fields["amount"])
        if st >= 0x80000000:
            st -= 0x100000000
        cur_r = p.i32(line_ptr + 0xE8)
        # 模拟引擎 CanExecute: 驱逐舰最大单线船坞 = 10
        if 0 <= cur_r + st <= 10:
            p.put_i32(line_ptr + 0xE8, cur_r + st)
            p.put_i32(line_ptr + GM.ML_FACTORIES, cur_r + st)
        return {"ok": True}
    g_b2.send_command = _mock_send_b2
    r_b2 = g_b2.set_production_amount(0, 12)
    check("B-2 驱逐舰线 9->12 自动缩步打满到 10，并明确报 hit_line_cap=True（非暂停误报）",
          r_b2["after"] == 10 and r_b2["hit_line_cap"] is True
          and "per-ship dockyard cap" in r_b2["note"]
          and "PAUSED" not in r_b2["note"],
          r_b2)

    # --- B-3: rebalance_production 战斗机权重优先（不被腰斩到 7 厂）---
    g_b3 = types.SimpleNamespace(
        player_tag=lambda: "GER",
        production_lines=lambda tag=None, include_obsolete=False: [
            {"index": 1, "class": "CMilitaryProductionLine", "equipment": "infantry_equipment_1", "factories": 20},
            {"index": 2, "class": "CMilitaryProductionLine", "equipment": "artillery_equipment_2", "factories": 10},
            {"index": 3, "class": "CMilitaryProductionLine", "equipment": "support_equipment_1", "factories": 6},
            {"index": 4, "class": "CMilitaryProductionLine", "equipment": "light_tank_equipment_2", "factories": 8},
            {"index": 6, "class": "CMilitaryProductionLine", "equipment": "fighter_equipment_1", "factories": 16},
            {"index": 7, "class": "CMilitaryProductionLine", "equipment": "CAS_equipment_1", "factories": 4},
        ],
        factory_allocation=lambda tag=None: {"military_available": 68, "military_idle": 4},
        equipment_stockpile=lambda tag=None: {"totals_by_equipment": {
            "infantry_equipment_1": 30000, "artillery_equipment_2": 1000, "support_equipment_1": 800, "light_tank_equipment_2": 500,
        }},
        divisions=lambda tag=None, limit=1: {"count": 70},
    )
    r_b3 = MC.rebalance_production(g_b3, dry_run=True)
    b3_after = {1: 20, 2: 10, 3: 6, 4: 8, 6: 16, 7: 4}
    for ch in (r_b3.get("changes") or []):
        b3_after[int(ch["line"])] = int(ch["target"])
    ft_tgt = b3_after[6]
    cas_tgt = b3_after[7]
    check("B-3 rebalance_production 给战斗机显著高于 CAS 的制空权重（>= 12 厂，不再腰斩到 7）",
          ft_tgt >= 12 and ft_tgt > cas_tgt,
          (ft_tgt, cas_tgt, b3_after))

    # --- B-4: housekeep_divisions 所有军已有 type-2 订单时不触发 activate_army_battleplans ---
    class _FakeHKAllOrdered(HK.HousekeepMixin):
        def __init__(self):
            self.bp_called = 0
            self.p = p
        def unassigned_divisions(self, tag=None): return []
        def order_groups(self, tag=None):
            return {"groups": [{"ptr": hex(og), "is_army_group": False, "divisions": 20, "leader_ptr": "0x1"}]}
        def available_generals(self, tag=None): return {"generals": []}
        def activate_army_battleplans(self, enemy, tag=None, **kw):
            self.bp_called += 1
            return {"ok": True}
    hk_ao = _FakeHKAllOrdered()
    r_hk_ao = hk_ao.housekeep_divisions()
    check("B-4 housekeep_divisions 所有军已有前线时跳过重画前线（bp_called==0）",
          hk_ao.bp_called == 0 and (r_hk_ao.get("battleplans") or {}).get("skipped") == "all_armies_already_have_front_orders",
          (hk_ao.bp_called, r_hk_ao))

    # --- B-5: Clausewitz NOT=NOR + ruling_ideology + _positive_tag 不误匹配 NOT ---
    not_node = [("NOT", [("TAG", "GER"), ("TAG", "ITA"), ("TAG", "FRA")])]
    check("B-5 NOT={TAG=GER TAG=ITA TAG=FRA} 对 GER 求值为 False（NOR 语义）且 _positive_tag 为 False",
          EW._trig(not_node, "GER") is False
          and EW._trigger_positively_names_tag(not_node, "GER") is False
          and EW._trigger_positively_names_tag([("TAG", "GER")], "GER") is True,
          (EW._trig(not_node, "GER"), EW._trigger_positively_names_tag(not_node, "GER")))
    g_gov = types.SimpleNamespace(
        politics=lambda t: {"ruling_ideology": "fascism"},
        country=lambda t: types.SimpleNamespace(state_count=10),
        wars=lambda t: {"enemies": []},
    )
    ctx_gov = EW._EvalCtx(g_gov, "GER")
    ctx_gov._relations["GER"] = {"at_war_with": []}
    check("B-5 has_government=fascism 与 has_capitulated=no 在有 ctx 时精确求值",
          EW._trig([("NOT", [("TAG", "HUN")]), ("has_government", "fascism")], "GER", ctx=ctx_gov) is True
          and EW._trig([("has_capitulated", "no")], "GER", ctx=ctx_gov) is True,
          (EW._trig([("NOT", [("TAG", "HUN")]), ("has_government", "fascism")], "GER", ctx=ctx_gov),
           EW._trig([("has_capitulated", "no")], "GER", ctx=ctx_gov)))

    # --- B-6: templedit 无效支援连必须返回 ok=False，dry_run 返回 applied=False，合法 key 校验 ---
    tptr = 0x15d10030000
    p.put_u64(tptr, p.base + 0x29CC350)
    orig_ta, orig_plan = TE._pe_template_arrays, TE._pe_plan
    try:
        TE._pe_template_arrays = lambda g_, t_: {
            "line": {"ptrs": [0x1], "names": ["infantry"], "size": 1},
            "support": {"ptrs": [0x2], "names": ["engineer"], "size": 1},
        }
        TE._pe_plan = lambda g_, arr_, cnts_, lbl_: (
            (None, "no subunit named 'bogus_company'") if lbl_ == "support" else (None, None)
        )
        g_te = types.SimpleNamespace(p=p, player=lambda: types.SimpleNamespace(tag_id=1))
        r_te_bad = TE._pe_edit_template(g_te, tptr, None, False, {"bogus_company": 1})
        check("B-6 无效支援连返回 ok=False, applied=False, stage='support'（不再假阳性）",
              r_te_bad["ok"] is False and r_te_bad.get("applied") is False
              and r_te_bad.get("stage") == "support" and "bogus_company" in r_te_bad.get("error", ""),
              r_te_bad)
    finally:
        TE._pe_template_arrays, TE._pe_plan = orig_ta, orig_plan
    check("B-6 _is_valid_subunit_key 过滤乱码与 '---'，放行 logistics_company",
          TE._is_valid_subunit_key("logistics_company") is True
          and TE._is_valid_subunit_key("---") is False
          and TE._is_valid_subunit_key("p]]\x01\x00") is False,
          None)

    # --- B-7: ARMY_XP_CAP 首档 150 + 实际消耗 >=10 XP 自动清零 ack_streaks ---
    check("B-7 xp_tiers 首档已校准为 150",
          RD.DEFAULT_CONFIG["xp_tiers"][0] == 150,
          RD.DEFAULT_CONFIG["xp_tiers"])
    orig_scan = RD._scan_raw
    try:
        RD._STATES.clear()
        xp_val = [180.0]
        RD._scan_raw = lambda g_, fast=False: {
            "ok": True, "tag": "GER", "date": "1938.12.01.01", "date_raw": 10000, "at_war": False,
            "items": [{
                "code": "ARMY_XP_CAP", "title": "xp", "detail": {"service": "army", "xp": xp_val[0], "tier": 150},
                "actions": [], "severity": "block", "tier": 150, "signature": "ARMY_XP_CAP:150",
            }],
        }
        g_xp = types.SimpleNamespace(p=types.SimpleNamespace(pid=99999), player_tag=lambda: "GER")
        RD.evaluate(g_xp)
        RD._get_state(g_xp)["ack_streaks"]["ARMY_XP_CAP"] = 2
        xp_val[0] = 165.0  # 玩家真实花了 15 点经验
        RD.evaluate(g_xp)
        check("B-7 玩家实际消耗 >=10 陆军经验后自动重置 ack_streaks['ARMY_XP_CAP']",
              RD._get_state(g_xp)["ack_streaks"].get("ARMY_XP_CAP", 0) == 0,
              RD._get_state(g_xp)["ack_streaks"])
    finally:
        RD._scan_raw = orig_scan
        RD._STATES.clear()

    # --- B-8: hoi4_clear_dirty_injector 与 hoi4_debug_eval 访问 winject ---
    WJ._DIRTY_PIDS.add(32488)
    fake_inj = types.SimpleNamespace(pid=32488, dirty=True)
    orig_mcp_g2, orig_alive2 = MCP.game, MCP._is_pid_alive
    try:
        MCP.game = lambda: types.SimpleNamespace(p=p, _inj=fake_inj)
        MCP._is_pid_alive = lambda pid_: True
        r_ev_wj = MCP._eval({"expr": "winject.dirty_status(32488)['is_dirty']"})
        r_clr = MCP._clear_dirty_injector({"pid": 32488})
        check("B-8 hoi4_debug_eval 可见 winject 且 hoi4_clear_dirty_injector 清空 dirty 标志",
              r_ev_wj is True and r_clr["ok"] is True
              and 32488 in r_clr["cleared_pids"]
              and fake_inj.dirty is False
              and WJ.dirty_status(32488)["is_dirty"] is False,
              (r_ev_wj, r_clr))
    finally:
        WJ._DIRTY_PIDS.discard(32488)
        MCP.game, MCP._is_pid_alive = orig_mcp_g2, orig_alive2


def test_retest11_all_fixes():
    print("\n== 14. 复测 11 (P0-1/P0-2/P1-2/P2-1/P2-2/lar_news.1) 回归验证 ==")
    import armyops_win as AW
    import armyops as AO
    import events_win as EW
    import movemicro as MM
    import mapdata as MD
    import unitorders as UO
    import game as G_MOD
    import server as MCP

    class _Mem:
        def __init__(self, base=0x140000000):
            self.base = base
            self.pid = 32488
            self.buf = {}
        def put(self, addr, data: bytes):
            for i, b in enumerate(data):
                self.buf[addr + i] = b
        def put_u64(self, addr, v):
            self.put(addr, struct.pack("<Q", v))
        def put_i32(self, addr, v):
            self.put(addr, struct.pack("<i", v))
        def put_i64(self, addr, v):
            self.put(addr, struct.pack("<q", v))
        def read(self, addr, n):
            return bytes(self.buf.get(addr + i, 0) for i in range(n))
        def u64(self, addr):
            return struct.unpack("<Q", self.read(addr, 8))[0]
        def i32(self, addr):
            return struct.unpack("<i", self.read(addr, 4))[0]
        def i64(self, addr):
            return struct.unpack("<q", self.read(addr, 8))[0]
        def u8(self, addr):
            return self.read(addr, 1)[0]
        def write(self, addr, data: bytes):
            self.put(addr, data)

    # --- P0-1: order_children 移除 0..0x3C0 盲扫（不再误抓 +0x2B8 里的 195 卡死 delete_order(37)） ---
    p = _Mem()
    og = 0x15e10001000
    other_og = 0x15e10001800
    root37 = 0x15e10002000
    ext195 = 0x15e10002400    # 落在 root+0x2B8 的外部/反向关联订单（非 +0x1F8 / +0x2D0）
    foreign_kid = 0x15e10002800  # 落在 +0x1F8 但属于别的集团军 (owner_group != og)
    real_kid55 = 0x15e10002C00   # 真实本军子订单 (+0x1F8)

    ord_arr = 0x15e10003000
    p.put_u64(ord_arr, root37)
    p.put_u64(og + AW.OG_ORDERS, ord_arr)
    p.put_i32(og + AW.OG_ORDERS + 8, 1)
    p.put_i32(og + AW.OG_ORDERS + 12, 1)

    p.put_u64(root37, p.base + AW.VT_ORDER_INSTANCE)
    p.put_i32(root37 + AW.OI_TYPE, 2)
    p.put_i32(root37 + AW.OI_ID, 37)
    p.put_u64(root37 + 0x38, og)
    p.put_i32(root37 + AW.OI_ASSIGNED + 0xC, 25)

    # 把 ext195 放进 +0x2B8（反向关联数组，不在 FindOrder 0x10AF270 遍历范围内）
    arr_2b8 = 0x15e10003200
    p.put_u64(arr_2b8, ext195)
    p.put_u64(root37 + 0x2B8, arr_2b8)
    p.put_i32(root37 + 0x2B8 + 8, 1)
    p.put_i32(root37 + 0x2B8 + 12, 1)
    p.put_u64(ext195, p.base + AW.VT_ORDER_INSTANCE)
    p.put_i32(ext195 + AW.OI_TYPE, 1)
    p.put_i32(ext195 + AW.OI_ID, 195)
    p.put_i32(ext195 + AW.OI_ASSIGNED + 0xC, 3)
    p.put_u64(ext195 + 0x38, other_og)

    # 把 foreign_kid 放进 +0x1F8（模拟跨军引用，child+0x38 == other_og）
    arr_1f8 = 0x15e10003400
    p.put_u64(arr_1f8, foreign_kid)
    p.put_u64(root37 + 0x1F8, arr_1f8)
    p.put_i32(root37 + 0x1F8 + 8, 2)
    p.put_i32(root37 + 0x1F8 + 12, 1)
    p.put_u64(foreign_kid, p.base + AW.VT_ORDER_INSTANCE)
    p.put_i32(foreign_kid + AW.OI_TYPE, 1)
    p.put_i32(foreign_kid + AW.OI_ID, 196)
    p.put_i32(foreign_kid + AW.OI_ASSIGNED + 0xC, 4)
    p.put_u64(foreign_kid + 0x38, other_og)

    g_p01 = types.SimpleNamespace(p=p, _inj=object())
    kids_37 = AW.order_children(g_p01, og)
    check("P0-1 order_children 彻底忽略 +0x2B8 的外部订单 195，且为跨军节点 196 标明 foreign_group",
          all(k["id"] != 195 for k in kids_37)
          and len(kids_37) == 1 and kids_37[0]["id"] == 196 and kids_37[0].get("foreign_group") is True,
          kids_37)

    # 验证 delete_order(og, 37) 不会被 195 / 196 卡死，顺利删掉父订单 37
    orig_post_del = AW._post_delete_order_cmd
    orig_set_exe = AW.set_order_executing
    try:
        posted_del_ids = []
        def _mock_del37(g_, og_, oid_):
            posted_del_ids.append(oid_)
            if oid_ == 37:
                p.put_i32(og + AW.OG_ORDERS + 12, 0)
                return True, ""
            return False, "COrderDeleteCommand::CanExecute refused (no such order %d)" % oid_
        AW._post_delete_order_cmd = _mock_del37
        AW.set_order_executing = lambda g_, c_, on=False: p.put(c_ + AW.OI_EXECUTING, b"\x01" if on else b"\x00")
        r_del37 = AW.delete_order(g_p01, og, 37)
        check("P0-1 delete_order(37) 不再被 +0x2B8 外部订单或跨军节点死锁，一次删除成功",
              r_del37["ok"] is True and posted_del_ids == [37] and r_del37["remaining_orders"] == 0,
              (r_del37, posted_del_ids))
    finally:
        AW._post_delete_order_cmd = orig_post_del
        AW.set_order_executing = orig_set_exe

    # --- P0-2: 单条 type-1 进攻线 / 前线 / 整军转守开关 ---
    p.put_i32(og + AW.OG_ORDERS + 12, 1)
    p.put_u64(arr_1f8, real_kid55)
    p.put_i32(root37 + 0x1F8 + 12, 1)
    p.put_u64(real_kid55, p.base + AW.VT_ORDER_INSTANCE)
    p.put_i32(real_kid55 + AW.OI_TYPE, 1)
    p.put_i32(real_kid55 + AW.OI_ID, 55)
    p.put_i32(real_kid55 + AW.OI_ASSIGNED + 0xC, 22)
    p.put_u64(real_kid55 + 0x38, og)
    p.put(real_kid55 + AW.OI_EXECUTING, b"\x01")

    orig_exec_plan = AW.execute_plan
    orig_set_exe2 = AW.set_order_executing
    try:
        exec_calls = []
        def _fake_exec_plan(g_, og_, oid_=0, flag_=0):
            exec_calls.append((oid_, flag_))
            if oid_ in (55, 37, 0):
                p.put(real_kid55 + AW.OI_EXECUTING, b"\x01" if flag_ else b"\x00")
            return True, "", 0x9999
        AW.execute_plan = _fake_exec_plan
        AW.set_order_executing = lambda g_, c_, on=False: p.put(c_ + AW.OI_EXECUTING, b"\x01" if on else b"\x00")

        # 1) 单条 type-1 子订单 55 关停 (executing=False)
        r_halt55 = AW.set_plan_executing(g_p01, og, order_id=55, executing=False)
        check("P0-2 单条 type-1 进攻线 55 可精准转守 (executing=0)，父前线与 22 师完全不动",
              r_halt55["ok"] is True and r_halt55["scope"] == "child"
              and r_halt55["executing"] is False and r_halt55["root_intact"] is True
              and r_halt55["children_executing"] == [] and r_halt55["assigned"] == 22
              and exec_calls[-1] == (55, 0),
              (r_halt55, exec_calls))

        # 2) 重新开启再测 hoi4_offensive_line(enabled=False) 与 order_id=0 全军转守
        p.put(real_kid55 + AW.OI_EXECUTING, b"\x01")
        g_mock_p02 = G_MOD.Game.__new__(G_MOD.Game)
        g_mock_p02.p = p
        g_mock_p02._inj = object()
        r_off_dis = g_mock_p02.offensive_line(og, 37, enabled=False)
        check("P0-2 Game.offensive_line(enabled=False) 关闭父前线下所有进攻箭头并保持父订单完整",
              r_off_dis["ok"] is True and r_off_dis["method"] == "disable_offensive"
              and r_off_dis["children_executing"] == [] and r_off_dis["root_intact"] is True,
              r_off_dis)

        p.put(real_kid55 + AW.OI_EXECUTING, b"\x01")
        r_army_halt = g_mock_p02.execute_plan(og, order_id=0, executing=False)
        check("P0-2 Game.execute_plan(order_id=0, executing=False) 走原生 order_id=0 全军转守",
              r_army_halt["ok"] is True and r_army_halt["scope"] == "army"
              and exec_calls[-1] == (0, 0) and r_army_halt["children_executing"] == [],
              (r_army_halt, exec_calls))

        # 3) hoi4_orders 返回的 children 必须带 id 字段
        orig_mcp_g = MCP.game
        try:
            MCP.game = lambda: g_mock_p02
            ord_view = MCP._orders({"orders_group": hex(og)})
            check("P0-2 hoi4_orders[].children 包含子订单 id 字段 (便于直接传给 hoi4_execute_plan)",
                  len(ord_view) == 1 and ord_view[0]["children"][0]["id"] == 55,
                  ord_view)
        finally:
            MCP.game = orig_mcp_g
    finally:
        AW.execute_plan = orig_exec_plan
        AW.set_order_executing = orig_set_exe2

    # --- P1-2: plan_front 拒绝抽调驻防在 FRA 马奇诺的无订单 free 军，且只用 HOL/后方军 ---
    og_maginot = 0x15e20001000
    og_hol = 0x15e20002000
    div_fra = 0x15e20003000
    div_hol = 0x15e20003400
    p2 = _Mem()
    p2.put(og_maginot + AW.OG_IS_GROUP, b"\x00")
    p2.put(og_hol + AW.OG_IS_GROUP, b"\x00")

    g_p12 = G_MOD.Game.__new__(G_MOD.Game)
    g_p12.p = p2
    g_p12._inj = object()
    g_p12._c = lambda tag=None: types.SimpleNamespace(ptr=0x15e20000100, tag="GER", tag_id=1)
    g_p12.wars = lambda tag=None: {"enemies": ["HOL", "FRA"]}
    g_p12._supply_front_guard = lambda enemy, tag=None: {"capacity": 40}
    g_p12.divisions = lambda tag=None: {"divisions": []}

    orig_ft = AW.fronts_towards
    orig_tp = AW.target_provinces
    orig_ogs = AO.orders_groups
    orig_ords = AW.orders
    orig_divs = AW.divisions_of
    orig_pom = MD.province_owner_map
    orig_adj = MD.build_adjacency
    orig_ld = MD.load_definitions
    orig_po = UO.province_of
    try:
        AW.fronts_towards = lambda g_, e_, o_=None: [{
            "front": 0x15e20009000, "front_handle": [66, 10], "section": 0,
            "border_provinces": [101], "slice_from": 63636, "slice_to": 100000,
        }]
        AW.target_provinces = lambda g_, e_, depth=1, own_tag=None: [201]
        AO.orders_groups = lambda g_, co_: [og_maginot, og_hol]
        AW.orders = lambda g_, og_: []   # 两个军都无订单 (free!)
        AW.divisions_of = lambda g_, og_: [div_fra] if og_ == og_maginot else [div_hol]
        # 101 贴 HOL(201); 102 贴 FRA(301)
        MD.province_owner_map = lambda g_: ({101: "GER", 102: "GER", 201: "HOL", 301: "FRA"}, {})
        MD.build_adjacency = lambda: {101: [201], 201: [101], 102: [301], 301: [102]}
        MD.load_definitions = lambda: ({}, {101: "land", 102: "land", 201: "land", 301: "land"})
        UO.province_of = lambda g_, d_: 102 if d_ == div_fra else 101

        r_p12 = g_p12.plan_front("HOL", dry_run=True)
        planned_armies = [x["army"] for x in (r_p12.get("steps") or {}).get("plan", [])]
        guarded = (r_p12.get("steps") or {}).get("free_armies_guarding_other_borders") or []
        check("P1-2 plan_front('HOL') 自动排除正驻防 FRA 马奇诺的无订单军，只给 HOL 军画切片",
              r_p12["ok"] is True and planned_armies == [hex(og_hol)]
              and len(guarded) == 1 and guarded[0]["army"] == hex(og_maginot),
              (r_p12.get("ok"), planned_armies, guarded))

        # 若仅有马奇诺军时必须明确拒绝而非硬抽
        AO.orders_groups = lambda g_, co_: [og_maginot]
        r_p12_blk = g_p12.plan_front("HOL", dry_run=True)
        check("P1-2 全空闲军均在守 FRA 边境时 plan_front('HOL') 拒抽并返回 free_armies_guarding_other_borders",
              r_p12_blk["ok"] is False and r_p12_blk.get("blocked_by") == "free_armies_guarding_other_borders",
              r_p12_blk)
    finally:
        AW.fronts_towards = orig_ft
        AW.target_provinces = orig_tp
        AO.orders_groups = orig_ogs
        AW.orders = orig_ords
        AW.divisions_of = orig_divs
        MD.province_owner_map = orig_pom
        MD.build_adjacency = orig_adj
        MD.load_definitions = orig_ld
        UO.province_of = orig_po

    # --- P2-2: move_divisions(spread=True) 自动把 20 个师分散到周边多省 ---
    p3 = _Mem()
    co3 = 0x15e30000100
    divs_20 = [0x15e30001000 + i * 0x100 for i in range(20)]
    d_arr3 = 0x15e30005000
    for i, dp in enumerate(divs_20):
        p3.put_u64(d_arr3 + 8 * i, dp)
    p3.put_u64(co3 + 0x290, d_arr3)
    p3.put_i32(co3 + 0x290 + 8, 20)
    p3.put_i32(co3 + 0x290 + 12, 20)

    g_p22 = types.SimpleNamespace(
        p=p3, _inj=object(),
        _c=lambda tag=None: types.SimpleNamespace(ptr=co3, tag="GER"),
        player=lambda: types.SimpleNamespace(ptr=co3, tag="GER"),
        faction_members=lambda: {"members": ["GER"]},
    )
    orig_pom2, orig_adj2, orig_ld2, orig_po2, orig_bm = (
        MD.province_owner_map, MD.build_adjacency, MD.load_definitions,
        UO.province_of, UO.build_move,
    )
    try:
        # 构造 4 个连通己方后方省: 6549 <-> 6550 <-> 6551 <-> 6552, 初始 20 师全在 9001
        MD.province_owner_map = lambda g_: (
            {9001: "GER", 6549: "GER", 6550: "GER", 6551: "GER", 6552: "GER"}, {}
        )
        MD.build_adjacency = lambda: {
            9001: [6549], 6549: [9001, 6550], 6550: [6549, 6551],
            6551: [6550, 6552], 6552: [6551],
        }
        MD.load_definitions = lambda: (
            {}, {9001: "land", 6549: "land", 6550: "land", 6551: "land", 6552: "land"}
        )
        UO.province_of = lambda g_, d_: 9001
        UO.build_move = lambda g_, d_, dst_, flags_, path=None: (0xABC0, "")

        # 1) 不开 spread 时撞 stacking 保护，且错误提示推荐 spread=true
        r_stk = MM.move_divisions(g_p22, divisions=divs_20, province=6549, post=False)
        check("P2-2 move_divisions 默认撞 stacking 时明确推荐 spread=true",
              r_stk["ok"] is False and r_stk.get("blocked_by") == "stacking"
              and "spread=true" in r_stk.get("error", ""),
              r_stk)

        # 2) 开 spread=True, max_per_province=6 时 20 师按 6/6/6/2 自动分散到 4 省
        r_spr = MM.move_divisions(
            g_p22, divisions=divs_20, province=6549, post=False,
            spread=True, max_per_province=6,
        )
        sp_vals = list((r_spr.get("spread_plan") or {}).values())
        check("P2-2 move_divisions(spread=True, max_per_province=6) 自动分散 20 师到多省且无省超上限",
              r_spr["ok"] is True and r_spr.get("spread") is True
              and sum(sp_vals) == 20 and max(sp_vals) <= 6 and len(sp_vals) >= 4,
              (r_spr.get("ok"), r_spr.get("spread_plan")))
    finally:
        MD.province_owner_map = orig_pom2
        MD.build_adjacency = orig_adj2
        MD.load_definitions = orig_ld2
        UO.province_of = orig_po2
        UO.build_move = orig_bm

    # --- P2-1: add_divisions_to_army 默认标明 positions_moved=False，传 move_to_province 一步移位 ---
    g_p21 = G_MOD.Game.__new__(G_MOD.Game)
    g_p21.p = p3
    g_p21._inj = object()
    orig_aw_add = AW.add_divisions_to_army
    try:
        AW.add_divisions_to_army = lambda g_, og_, divs_, order_id=None: {
            "ok": True, "orders_group": hex(og_), "added": len(divs_),
            "positions_moved": False, "position_note": "not moved",
            "transferred_divisions": [hex(d) for d in divs_],
        }
        mv_seen = []
        g_p21.move_divisions = lambda **kw: (mv_seen.append(kw) or {
            "ok": True, "province": kw["province"], "spread": kw["spread"],
            "spread_plan": {str(kw["province"]): len(kw["divisions"])},
            "ordered": len(kw["divisions"]), "accepted": len(kw["divisions"]), "moving": 0,
        })
        r_no_mv = g_p21.add_divisions_to_army(0x15e40001000, divisions=divs_20[:3])
        r_with_mv = g_p21.add_divisions_to_army(
            0x15e40001000, divisions=divs_20[:3], move_to_province=6549, spread=True
        )
        check("P2-1 add_divisions_to_army 默认返回 positions_moved=False，传 move_to_province 一步联动 move_divisions",
              r_no_mv["ok"] is True and r_no_mv.get("positions_moved") is False
              and r_with_mv["ok"] is True and r_with_mv.get("positions_moved") is True
              and len(mv_seen) == 1 and mv_seen[0]["province"] == 6549 and mv_seen[0]["spread"] is True,
              (r_no_mv.get("positions_moved"), r_with_mv.get("positions_moved"), mv_seen))
    finally:
        AW.add_divisions_to_army = orig_aw_add

    # --- P2-3: lar_news.1 在 SPA/SPB 动态别名暂不可解时仍精准推荐德国法西斯选项 3 ---
    g_lar = types.SimpleNamespace(
        p=_Mem(),
        _mock_exists={"GER": True},
        politics=lambda tag=None: {"ruling_ideology": "fascism"},
        country=lambda tag: (_ for _ in ()).throw(KeyError(tag)),
    )
    orig_pend = EW.pending
    orig_make = EW.make_select_option
    orig_ce = EW.can_execute
    try:
        EW.pending = lambda g_: [{"slot": 1, "event_id": "lar_news.1", "event_ptr": "0x15e50001000"}]
        EW.make_select_option = lambda g_, slot_, eptr_, idx_, name_: 0x15e50002000
        EW.can_execute = lambda g_, cmd_: True
        r_lar = EW.select_option(g_lar, event_id="lar_news.1", tag="GER", write=False)
        check("P2-3 lar_news.1 对 GER 法西斯精准推荐选项 3 (lar_news.1.c) 而非 null",
              r_lar["ok"] is True and r_lar["recommended"] == 3
              and r_lar.get("recommendation_basis") == "unique_positive_tag_partial_trigger"
              and r_lar["unresolved_scope"] is False,
              (r_lar.get("recommended"), r_lar.get("recommendation_basis"),
               [(o["index"], o["applicable"]) for o in r_lar.get("options", [])]))
    finally:
        EW.pending = orig_pend
        EW.make_select_option = orig_make
        EW.can_execute = orig_ce


def test_security_and_hard_bugs():
    print("\n== 15. 公开仓安全审计与硬 Bug 回归验证 ==")
    import diplomacy as DP
    import turnengine as TE
    import macros as MC
    import server as MCP
    from process import winject as WJ

    # 1. diplomacy.justify_wargoal / declare_war 在 Windows 上干净返回提示，不抛 NameError
    ok_jw, msg_jw = DP.justify_wargoal(None, "POL")
    ok_dw, msg_dw = DP.declare_war(None, "POL")
    check("diplomacy.justify_wargoal/declare_war 不再因缺 import sys 抛 NameError",
          (ok_jw is False and "cmdwin" in msg_jw)
          and (ok_dw is False and "cmdwin" in msg_dw),
          ((ok_jw, msg_jw), (ok_dw, msg_dw)))

    # 2. turnengine._run_inspect_one(topic="health") 不再因未定义变量 t 抛 NameError
    orig_shb = MC.strategic_health_board
    captured = {}
    try:
        def _fake_shb(g_, tag=None):
            captured["tag"] = tag
            return {"ok": True, "tag": tag}
        MC.strategic_health_board = _fake_shb
        r_hp = TE._run_inspect_one(object(), "health", {"topic": "health"}, "GER")
        check("turnengine inspect topic='health' 正确透传 tag 且不抛 NameError",
              r_hp == {"ok": True, "tag": "GER"} and captured.get("tag") == "GER",
              (r_hp, captured))
    finally:
        MC.strategic_health_board = orig_shb

    # 3. MCP 服务端默认隐藏并拒绝全部 hoi4_debug_*（特别是 hoi4_debug_eval）
    orig_prof = MCP._MCP_PROFILE
    orig_dbg, orig_ev = MCP._ALLOW_DEBUG, MCP._ALLOW_EVAL
    try:
        MCP._MCP_PROFILE = "full"
        MCP._ALLOW_DEBUG = False
        MCP._ALLOW_EVAL = False
        names = {t["name"] for t in MCP._active_tools()}
        check("默认 Full 模式 tools/list 隐藏全部 hoi4_debug_* 工具",
              not any(n.startswith("hoi4_debug_") for n in names),
              sorted(n for n in names if n.startswith("hoi4_debug_")))
        resp_eval = MCP.handle({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "hoi4_debug_eval", "arguments": {"expr": "1+1"}},
        })
        resp_con = MCP.handle({
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "hoi4_debug_console", "arguments": {"command": "pp 1"}},
        })
        resp_unk = MCP.handle({
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "hoi4_no_such_tool", "arguments": {}},
        })
        check("默认 Full 模式 tools/call 拒绝 hoi4_debug_eval / hoi4_debug_console 且错误为英文",
              resp_eval.get("error", {}).get("code") == -32601
              and "disabled" in resp_eval["error"]["message"]
              and resp_con.get("error", {}).get("code") == -32601
              and "disabled" in resp_con["error"]["message"]
              and resp_unk.get("error", {}).get("message") == "unknown tool: hoi4_no_such_tool",
              (resp_eval, resp_con, resp_unk))

        # 仅开 HOI4_MCP_ALLOW_DEBUG=1 仍不允许 hoi4_debug_eval，需双开关
        MCP._ALLOW_DEBUG = True
        MCP._ALLOW_EVAL = False
        names_dbg = {t["name"] for t in MCP._active_tools()}
        resp_eval2 = MCP.handle({
            "jsonrpc": "2.0", "id": 4, "method": "tools/call",
            "params": {"name": "hoi4_debug_eval", "arguments": {"expr": "1+1"}},
        })
        check("仅开启 ALLOW_DEBUG 不放行 hoi4_debug_eval（需同时开启 ALLOW_EVAL）",
              "hoi4_debug_console" in names_dbg
              and "hoi4_debug_eval" not in names_dbg
              and resp_eval2.get("error", {}).get("code") == -32601,
              (resp_eval2, "hoi4_debug_eval" in names_dbg))
    finally:
        MCP._MCP_PROFILE = orig_prof
        MCP._ALLOW_DEBUG, MCP._ALLOW_EVAL = orig_dbg, orig_ev

    # 4. winject.FROZEN_FILE 位于用户可写目录而非源码目录
    src_dir = os.path.dirname(os.path.abspath(WJ.__file__))
    check("winject.FROZEN_FILE 不再写入源码目录",
          os.path.dirname(os.path.abspath(WJ.FROZEN_FILE)) != src_dir,
          (WJ.FROZEN_FILE, src_dir))



class TestRetest10And11Security(unittest.TestCase):
    def test_retest10_all_fixes(self):
        reset_counts()
        globals()["test_retest10_all_fixes"]()
        _p, _f = counts()
        self.assertEqual(_f, 0, f"test_retest10_all_fixes failed: {FAILURES}")

    def test_retest11_all_fixes(self):
        reset_counts()
        globals()["test_retest11_all_fixes"]()
        _p, _f = counts()
        self.assertEqual(_f, 0, f"test_retest11_all_fixes failed: {FAILURES}")

    def test_security_and_hard_bugs(self):
        reset_counts()
        globals()["test_security_and_hard_bugs"]()
        _p, _f = counts()
        self.assertEqual(_f, 0, f"test_security_and_hard_bugs failed: {FAILURES}")


if __name__ == "__main__":
    unittest.main()
