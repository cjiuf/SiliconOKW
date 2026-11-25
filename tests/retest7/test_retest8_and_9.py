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


# ===================================================================== 9. 复测 8 (B-11 ~ B-16)
def test_retest8_fixes():
    print("\n== 9. 复测 8 (B-11~B-16): 修理船坞list索引 / +0xB8前线交集 / 训练幂等 / AI事件过滤 / 空军自愈 ==")

    # B-14a: order+0x210 存放的是 CArmy* + 0xB8 子对象指针 -> 交集必须等于 6（绝不归 0 误删！）
    p = _MemProc()
    og_ptr = 0x600000
    ord_ptr = 0x610000
    arr_ptr = 0x620000
    my_6 = [0x700000 + i * 0x200 for i in range(6)]
    p.put_u64(ord_ptr + 0x210, arr_ptr)
    p.put_i32(ord_ptr + 0x210 + 0xC, 6)
    for i, d in enumerate(my_6):
        p.put_u64(arr_ptr + 8 * i, d + AW.ORDER_DIV_SUBOBJ_OFF)  # +0xB8!
    g_b14 = types.SimpleNamespace(p=p)
    with mock.patch.object(AW, "divisions_of", return_value=my_6), \
         mock.patch.object(AW, "orders", return_value=[{"id": 5, "ptr": hex(ord_ptr)}]):
        br = AW.assigned_breakdown(g_b14, og_ptr, 5)
    check("B-14 order+0x210 子对象(+0xB8) 精确匹配本军 6/6 师（不再归 0 误删前线）",
          br["army_assigned"] == 6 and br["front_total_assigned"] == 6, br)

    # B-12a: factory_allocation 兼容 self._tag2idx 为 list，准确读出 sn[1]+0x1A4 = 3 座修理船坞 -> idle = 0
    p2 = _MemProc()
    gs = 0x500000
    snm = 0x510000
    sn_arr = 0x520000
    sn_ger = 0x530000
    c_ptr = 0x540000
    prod_ptr = 0x550000
    p2.put_u64(gs + 0x698, snm)
    p2.put_u64(snm + 0x08, sn_arr)
    p2.put_i32(snm + 0x14, 10)
    p2.put_u64(sn_arr + 1 * 8, sn_ger)
    p2.put_i32(sn_ger + 0x1A4, 3)  # 德国 3 座修理船坞!
    p2.put_u64(c_ptr + game_mod.CO_PRODUCTION, prod_ptr)
    p2.put_bytes(prod_ptr + 0x2D0, struct.pack("<q", 28 * 100000))
    p2.put_i32(prod_ptr + 0x2F0, 28)
    p2.put_bytes(prod_ptr + 0x330, struct.pack("<q", 10 * 100000))
    p2.put_i32(prod_ptr + 0x350, 7)

    class _FakeFA(game_mod.Game):
        def __init__(self):
            self.p = p2
            self._tag2idx = [0, 1, 2, 3]  # 真实类型为 list!

        @property
        def gamestate(self):
            return gs

        def _load_tags(self):
            pass

        def _c(self, tag=None):
            return types.SimpleNamespace(
                ptr=c_ptr, tag="GER", tag_id=1,
                civilian_factories=35, military_factories=28, naval_dockyards=10,
            )

        def production_lines(self, tag=None, include_obsolete=False):
            return []

    fa = _FakeFA().factory_allocation("GER")
    check("B-12 factory_allocation 兼容 list _tag2idx 精准读出 3 修理船坞 -> naval_available=7, naval_idle=0",
          fa["naval_repair_dockyards"] == 3 and fa["naval_available"] == 7 and fa["naval_idle"] == 0,
          fa)

    # B-11: paused=True 且 idle=0 时，set_production_amount 绝不误报 "game is PAUSED"
    arr_l = 0x560000
    l0 = 0x570000
    p2.put_u64(prod_ptr + game_mod.PS_MIL_LINES, arr_l)
    p2.put_i32(prod_ptr + game_mod.PS_MIL_LINES + 12, 1)
    p2.put_u64(arr_l, l0)
    p2.put_i32(l0 + game_mod.ML_FACTORIES, 1)
    p2.put_i32(l0 + 0xE8, 1)

    class _FakePauseProd(_FakeFA):
        def handle_of(self, _lp):
            return {"type": 56, "id": 8}

        def send_command(self, *a, **k):
            pass

        def _settle(self, _s=0.1):
            pass

    fake_sess = types.SimpleNamespace(is_paused=lambda _g: True)
    with mock.patch.dict(sys.modules, {"session": fake_sess}):
        r_b11 = _FakePauseProd().set_production_amount(0, 3, auto_reclaim=False)
    check("B-11 暂停且 idle=0 时明确报 not enough spare，绝不误报 game is PAUSED",
          (not r_b11["ok"]) and "not enough spare" in r_b11["note"]
          and "PAUSED" not in r_b11["note"] and r_b11["idle_factories"] == 0,
          r_b11)

    # B-12b: IDLE_DOCK 已移出 AUTO_SOLVABLE_CODES，普通理由可直接 ack 不再自锁
    check("B-12 IDLE_DOCK 不在 AUTO_SOLVABLE_CODES（杜绝船坞自锁）",
          "IDLE_DOCK" not in RD.AUTO_SOLVABLE_CODES)

    # B-13: housekeep_recruitment 步枪归 0 且已有 1 条 2 线 conveyor 时幂等跳过，绝不再开第 2 条
    class _FakeRec(HK.HousekeepMixin):
        def __init__(self):
            self.opened = 0
            self._convs = [{"ptr": "0x1", "packed_handle": 1, "lines": 2}]

        def _c(self, tag=None):
            return types.SimpleNamespace(tag="GER", manpower=lambda: 1200000)

        def divisions(self, tag=None):
            return {"count": 30}

        def _stockpile_counts(self, tag=None):
            return {"infantry_equipment_1": 0.0}

        def conveyors(self, tag=None):
            return list(self._convs)

        def _homeland_state_id(self, tag=None):
            return 64

        def _repair_conveyors(self, tag=None):
            return []

        def date(self):
            return {"year": 1936}

        def train_divisions(self, lines=2, tag=None):
            self.opened += 1
            self._convs.append({"ptr": "0x2", "packed_handle": 2, "lines": lines})
            return {"ok": True}

        def deploy_ready_conveyors(self, tag=None, min_progress=0.995):
            return {"ok": True}

    frec = _FakeRec()
    rr1 = frec.housekeep_recruitment()
    rr2 = frec.housekeep_recruitment()
    check("B-13 步枪=0 且已有 1 条 2 线训练带时，连续 housekeep 幂等跳过绝不再开第 2 条",
          frec.opened == 0 and rr1["opened"] is None and rr2["opened"] is None
          and rr1["conveyors_after"] == 1,
          (frec.opened, rr1))

    # B-14b: housekeep_divisions 在 battleplans.ok=False 时如实返回 ok=False
    class _FakeDivHK(HK.HousekeepMixin):
        def unassigned_divisions(self, tag=None):
            return []

        def order_groups(self, tag=None):
            return {"groups": [{"ptr": "0x1000", "leader_ptr": 0x2000}]}

        def available_generals(self, tag=None):
            return {"generals": []}

        def wartime_status(self):
            return {"enemies": ["FRA"]}

        def activate_army_battleplans(self, enemy, tag=None):
            return {"ok": False, "error": "dropped empty front"}

    fake_mm = types.SimpleNamespace(_load_front_state=lambda: {"directives": {}})
    with mock.patch.dict(sys.modules, {"movemicro": fake_mm}):
        d_res = _FakeDivHK().housekeep_divisions()
    check("B-14 housekeep_divisions 在 battleplans.ok=False 时诚实报 ok=False",
          d_res["ok"] is False and "battleplans failed" in (d_res.get("warning") or ""), d_res)

    # B-16: events_win.pending(only_player=True) 按 CEventScope+0x08 过滤掉 AI 国家事件（如 TUR）
    import events_win as EW
    p3 = _MemProc()
    gs3 = 0x700000
    pend_arr = 0x710000
    ev_ger = 0x720000
    ev_tur = 0x721000
    sc_ger = 0x730000
    sc_tur = 0x731000
    p3.put_u64(p3.base + EW.GS_HANDLE_RVA, gs3)
    p3.put_u64(gs3 + EW.GS_PENDING, pend_arr)
    p3.put_i32(gs3 + EW.GS_PENDING_N, 2)
    # record 0: GER (tag_id=1)
    p3.put_i32(pend_arr + 0x00, 10)
    p3.put_u64(pend_arr + 0x08, ev_ger)
    p3.put_u64(pend_arr + 0x10, sc_ger)
    p3.put_u64(sc_ger, p3.base + EW.SCOPE_VT)
    p3.put_i32(sc_ger + 0x08, 1)
    # record 1: TUR (tag_id=55, AI)
    r1_addr = pend_arr + EW.PEND_STRIDE
    p3.put_i32(r1_addr + 0x00, 11)
    p3.put_u64(r1_addr + 0x08, ev_tur)
    p3.put_u64(r1_addr + 0x10, sc_tur)
    p3.put_u64(sc_tur, p3.base + EW.SCOPE_VT)
    p3.put_i32(sc_tur + 0x08, 55)

    g_ev = types.SimpleNamespace(
        p=p3,
        _tags=["---", "GER"] + ["X"] * 53 + ["TUR"],
        player=lambda: types.SimpleNamespace(tag="GER", tag_id=1),
    )
    with mock.patch.object(EW, "read_event_obj", side_effect=lambda _g, ev: {
        "event_id": "germany.4" if ev == ev_ger else "bftb_turkey.2"
    }):
        mine = EW.pending(g_ev, only_player=True)
        all_ev = EW.pending(g_ev, only_player=False)
    check("B-16 pending(only_player=True) 精准过滤 AI 土耳其事件 bftb_turkey.2，仅留 GER 事件",
          len(mine) == 1 and mine[0]["event_id"] == "germany.4" and mine[0]["owner_tag"] == "GER"
          and len(all_ev) == 2,
          (mine, all_ev))


# ===================================================================== 10. 复测 9 (C-1 ~ C-9)
def test_retest9_c1_to_c9_fixes():
    print("\n== 10. 复测 9 (C-1~C-9): CompletePlan句柄 / MEFO防死锁 / 动态将领 / 科技空缓存 / 并军 / 贸易 / 口径 ==")

    # C-1 & D-1: complete_plan 必须往 +0x30 写入 8 字节 CArmy 句柄（兼容开局 type=51 与新训 type=4713），
    # 绝不能写 4 字节省份 ID，且拦截空句柄与错误虚表（含 +0xB8 子对象指针）
    p_c1 = _MemProc()
    og_c1 = 0x15c00001000
    front_c1 = 0x15c00002000
    div1 = 0x15c00003000
    div2 = 0x15c00003400
    bad_div = 0x15c00003800
    p_c1.put_u64(div1, p_c1.base + AW.CARMY_VT)
    p_c1.put_bytes(div1 + AW.CARMY_HANDLE, struct.pack("<II", 51, 101))
    p_c1.put_u64(div2, p_c1.base + AW.CARMY_VT)
    p_c1.put_bytes(div2 + AW.CARMY_HANDLE, struct.pack("<II", 4713, 5573))
    p_c1.put_u64(bad_div, p_c1.base + 0x2A00000)  # 非 CArmy 虚表
    p_c1.put_bytes(bad_div + AW.CARMY_HANDLE, struct.pack("<II", 0, 0))
    valid_d, rej_d = AW.validate_division_ptrs(p_c1, [div1, bad_div, div2, div1])
    check("C-1/D-1 validate_division_ptrs 同时放行开局 {51,N} 与新训 {4713,N} 并精准拒脏对象",
          valid_d == [div1, div2] and len(rej_d) == 1 and "vptr" in rej_d[0]["reason"],
          (valid_d, rej_d))
    # 无合法师句柄时 complete_plan 直接安全拒绝，绝不调用工厂或写脏指针，并透传 rejected 原因
    g_c1_bad = types.SimpleNamespace(p=p_c1, _inj=object())
    ok_bad, err_bad, info_bad = AW.complete_plan(g_c1_bad, og_c1, front_c1, [1001, 1002], section=1, divisions=[bad_div])
    check("C-1/D-3 complete_plan 在无合法师句柄时直接安全拒绝并携带 rejected 诊断",
          (not ok_bad) and "no usable divisions" in err_bad and len(info_bad.get("rejected") or []) == 1,
          (ok_bad, err_bad, info_bad))

    # C-9 & C-4: GER_mefo_bills_mission 降级为 PASSIVE_MISSION (advisory)；
    # 真正限时危机 ACTIVE_MISSION 默认覆盖完整倒计时周期，且不触发 ack_streak_exceeded 死锁
    RD.reset_state()

    class _FakeMefoGame:
        p = types.SimpleNamespace(pid=9981)
        _date_raw = 16960000

        def player_tag(self):
            return "GER"

        def player(self):
            return types.SimpleNamespace(
                tag="GER", tag_id=1, civilian_factories=35, military_factories=28,
                naval_dockyards=10, political_power=100.0, experience={"army": 10, "navy": 5, "air": 5},
                fuel=50000.0, manpower=lambda: 500000,
            )

        def date(self):
            return {"text": "1936.06.01.01", "raw": self._date_raw}

        def current_focus(self):
            return {"key": "GER_four_year_plan"}

        def research_slots(self):
            return [{"slot": 0, "slot_busy": True}]

        def production_lines(self, tag=None, include_obsolete=False):
            return []

        def construction_queue(self, tag=None):
            return [{"factories": 15}]

        def factory_allocation(self, tag=None):
            return {
                "source": "engine_production_status",
                "military_available": 28, "military_used": 28, "military_idle": 0,
                "naval_available": 7, "naval_used": 7, "naval_idle": 0,
            }

        def unassigned_divisions(self, tag=None):
            return []

        def equipment_stockpile(self, tag=None):
            return {"totals_by_equipment": {"infantry_equipment_1": 2000.0}}

        def conveyors(self, tag=None):
            return [{"lines": 2}]

        def decisions(self, tag=None, which="visible", rich=True):
            if which == "active":
                return [
                    {
                        "key": "GER_mefo_bills_mission",
                        "title": "MEFO Bills Due",
                        "days_mission_timeout": 180,
                        "is_good": True,
                        "available": "has_political_power > 0 hidden_trigger = { always = no }",
                        "mine": True,
                    },
                ]
            return []

    gm = _FakeMefoGame()
    ev_mefo = RD.evaluate(gm, fast=True)
    b_codes = [x["code"] for x in ev_mefo["blocking"]]
    a_codes = [x["code"] for x in ev_mefo["advisory"]]
    check("C-9 德国 GER_mefo_bills_mission 降为 PASSIVE_MISSION (advisory)，绝不阻塞停表",
          "ACTIVE_MISSION" not in b_codes and "PASSIVE_MISSION" in a_codes, (b_codes, a_codes))

    # C-4: 真正可处理的 120 天限时危机，签收默认覆盖 120 天（不再 30 天一拦），且连续签收 3 次不触发死锁
    class _FakeCrisisGame(_FakeMefoGame):
        p = types.SimpleNamespace(pid=9982)

        def decisions(self, tag=None, which="visible", rich=True):
            if which == "active":
                return [{
                    "key": "demob_economic_mission",
                    "title": "Demobilize Our Economy",
                    "days_mission_timeout": 120,
                    "is_good": False,
                    "available": "OR = { has_unsupported_economic_law = no has_war = yes }",
                    "mine": True,
                }]
            return []

    gc = _FakeCrisisGame()
    ev_c = RD.evaluate(gc, fast=True)
    check("C-4 恶性可解危机 demob_economic_mission 仍进入 ACTIVE_MISSION 阻塞项",
          any(x["code"] == "ACTIVE_MISSION" for x in ev_c["blocking"]), ev_c["blocking"])
    ack1 = RD.ack(gc, "ACTIVE_MISSION", "准备开战前先维持战时经济法", persist=False)
    check("C-4 签收 120 天限时危机默认 snooze_days 自动覆盖完整 120 天（不再每30天一拦）",
          ack1["ok"] and ack1["acked"]["snooze_days"] == 120, ack1)
    # 模拟 120 天后连续第 2 次、第 3 次签收，绝不触发 ack_streak_exceeded 停表死锁
    gc._date_raw += 125 * 24
    ack2 = RD.ack(gc, "ACTIVE_MISSION", "推迟第2轮等待宣战", persist=False)
    gc._date_raw += 125 * 24
    ack3 = RD.ack(gc, "ACTIVE_MISSION", "推迟第3轮等待宣战", persist=False)
    check("C-9 ACTIVE_MISSION 不受 max_consecutive_acks=2 死锁封杀（第3次仍可正常签收）",
          ack2["ok"] and ack3["ok"], (ack2, ack3))

    # C-2: win_scan_class 扫出 [] 时绝不把空列表永久写入 Game._VT_SCAN
    class _FakeScanGame(game_mod.Game):
        def __init__(self):
            self.p = types.SimpleNamespace(pid=77654, base=0x140000000, regions=lambda writable=True: [])

        def _rtti_vt_rvas(self, cls_name):
            return [0x123456]

    fsg = _FakeScanGame()
    game_mod.Game._VT_SCAN.pop((77654, "CTechnologyTemplate"), None)
    getattr(game_mod.Game, "_VT_SCAN_EMPTY_AT", {}).pop((77654, "CTechnologyTemplate"), None)
    import objindex as OI
    with mock.patch.object(OI, "_scan_regions_for_vptrs", return_value=[]):
        h0 = fsg.win_scan_class("CTechnologyTemplate", use_cache=True)
    check("C-2 win_scan_class 扫出空列表时绝不写入 _VT_SCAN，_win_vt_cache 保持 None 允许重试",
          h0 == [] and (77654, "CTechnologyTemplate") not in game_mod.Game._VT_SCAN
          and fsg._win_vt_cache("CTechnologyTemplate") is None,
          (h0, fsg._win_vt_cache("CTechnologyTemplate")))
    with mock.patch.object(OI, "_scan_regions_for_vptrs", return_value=[0x20500001000]):
        h1 = fsg.win_scan_class("CTechnologyTemplate", use_cache=False)
    check("C-2 重启加载完成后再次扫描立刻恢复非空并缓存（无需 mcp_load）",
          h1 == [0x20500001000] and fsg._win_vt_cache("CTechnologyTemplate") == [0x20500001000],
          h1)
    game_mod.Game._VT_SCAN.pop((77654, "CTechnologyTemplate"), None)

    # C-8: generals() 直读 CCountryCharacters+0x70，动态招募无 GER_ 前缀将领 100% 可见且 available=True
    p_c8 = _MemProc()
    co_ptr = 0x15a00001000
    cc_ptr = 0x15a00002000
    arr70 = 0x15a00003000
    al_hist = 0x15a00004000
    al_dyn = 0x15a00006000
    p_c8.put_u64(co_ptr + 0xFF0, cc_ptr)
    p_c8.put_u64(cc_ptr + 0x70, arr70)
    p_c8.put_i32(cc_ptr + 0x78, 2)
    p_c8.put_i32(cc_ptr + 0x7C, 2)
    p_c8.put_u64(arr70 + 0, al_hist)
    p_c8.put_u64(arr70 + 8, al_dyn)

    def _put_sso(mp, addr, text):
        b = text.encode("utf-8")
        if len(b) <= 15:
            mp.put_bytes(addr, b + b"\0" * (16 - len(b)))
            mp.put_u64(addr + 0x10, len(b))
            mp.put_u64(addr + 0x18, 15)
        else:
            heap_s = addr + 0x1800
            mp.put_bytes(heap_s, b + b"\0")
            mp.put_u64(addr, heap_s)
            mp.put_u64(addr + 0x10, len(b))
            mp.put_u64(addr + 0x18, max(31, len(b)))

    import legality as LEG
    _put_sso(p_c8, al_hist + 0x20, "GER_heinz_guderian")
    _put_sso(p_c8, al_hist + 0x40, "Guderian")
    p_c8.put_i32(al_hist + LEG.AL_RANK, 1)
    p_c8.put_i32(al_hist + game_mod.Game.AL_ATTACK, 4)
    p_c8.put_i32(al_hist + game_mod.Game.AL_DEFENSE, 4)
    p_c8.put_i32(al_hist + game_mod.Game.AL_PLANNING, 4)
    p_c8.put_i32(al_hist + game_mod.Game.AL_LOGISTICS, 3)

    _put_sso(p_c8, al_dyn + 0x20, "Emil Pickelhaubt")
    _put_sso(p_c8, al_dyn + 0x40, "Emil Pickelhaubt")
    p_c8.put_i32(al_dyn + LEG.AL_RANK, 1)
    p_c8.put_i32(al_dyn + game_mod.Game.AL_ATTACK, 1)
    p_c8.put_i32(al_dyn + game_mod.Game.AL_DEFENSE, 1)
    p_c8.put_i32(al_dyn + game_mod.Game.AL_PLANNING, 1)
    p_c8.put_i32(al_dyn + game_mod.Game.AL_LOGISTICS, 1)

    class _FakeGenGame(game_mod.Game):
        def __init__(self):
            self.p = p_c8

        def _c(self, tag=None):
            return types.SimpleNamespace(ptr=co_ptr, tag="GER", tag_id=1)

        def player(self):
            return self._c()

        def country(self, tag):
            return self._c(tag)

    fg_c8 = _FakeGenGame()
    with mock.patch.object(LEG, "leader_available", return_value=(True, "")):
        gens_c8 = fg_c8.generals("GER")
    dyn_row = next((r for r in gens_c8["generals"] if r["key"] == "Emil Pickelhaubt"), None)
    check("C-8 generals() 直读 +0x70 能完整看见动态将领 Emil Pickelhaubt（available=True, dynamic=True）",
          gens_c8["count"] == 2 and dyn_row is not None
          and dyn_row["rank"] == "corps_commander" and dyn_row["dynamic"] is True and dyn_row["available"],
          gens_c8)

    # C-3: housekeep_divisions 优先把新师并入已有未满编集团军，绝不盲目建散军
    class _FakeMergeHK(HK.HousekeepMixin):
        def __init__(self):
            self.unass = [0x15a10001000, 0x15a10002000]
            self.merged_calls = []
            self.created_calls = []

        def unassigned_divisions(self, tag=None):
            return list(self.unass)

        def order_groups(self, tag=None):
            return {"groups": [
                {"ptr": "0x15a20001000", "divisions": 18, "is_army_group": False, "leader_ptr": "0x15a30001000"},
            ]}

        def add_divisions_to_army(self, og, divisions=None, tag=None):
            self.merged_calls.append((hex(og), list(divisions or [])))
            self.unass = [d for d in self.unass if d not in (divisions or [])]
            return {"ok": True, "added": len(divisions or [])}

        def create_army(self, divs, tag=None):
            self.created_calls.append(list(divs))
            return {"ok": True}

        def available_generals(self, tag=None):
            return {"generals": []}

        def wartime_status(self):
            return {"enemies": []}

    fmhk = _FakeMergeHK()
    fake_mm = types.SimpleNamespace(_load_front_state=lambda: {"directives": {}})
    fake_dr = types.SimpleNamespace(
        is_paradrop_division=lambda _g, _d: (False, ""),
        army_is_paradrop=lambda _g, _og: (False, ""),
    )
    with mock.patch.dict(sys.modules, {"movemicro": fake_mm, "divreserve": fake_dr}):
        mres = fmhk.housekeep_divisions("GER")
    check("C-3 housekeep_divisions 优先将 2 个未编组师并入现有 18 师未满编军而不新建散军",
          len(fmhk.merged_calls) == 1 and len(fmhk.created_calls) == 0 and mres["remaining"] == 0,
          (fmhk.merged_calls, fmhk.created_calls, mres))

    # C-3 & C-5: MCP 工具与 TurnEngine DSL 均已注册 hoi4_add_to_army & hoi4_trade
    tmap = {t["name"]: t for t in MCP.TOOLS}
    check("C-3 & C-5 MCP 已注册 hoi4_add_to_army 与 hoi4_trade（FUEL_ZERO 真正有解）",
          {"hoi4_add_to_army", "hoi4_trade"} <= set(tmap))
    ok_dsl, errs_dsl = TE.validate_plan({
        "actions": [
            {"op": "add_to_army", "args": {"orders_group": "0x15a20001000", "count": 2}},
            {"op": "trade", "args": {"partner": "ROM", "resource": "oil", "amount": 8, "dry_run": True}},
        ]
    })
    check("C-3 & C-5 TurnEngine Plan DSL 校验通过 add_to_army 与 trade",
          ok_dsl and not errs_dsl, (ok_dsl, errs_dsl))

    # C-6: _decision_tag_scope 精准识别外邦 TAG_ 前缀（放行 MTG_），且 _doctrine_templates 不缓存空表
    known = frozenset({"GER", "JAP", "SIA", "ENG", "USA", "SOV"})
    check("C-6 _decision_tag_scope 将 JAP_/SIA_ 判为他国，同时放行 MTG_ DLC 前缀",
          game_mod.Game._decision_tag_scope(None, None, key="JAP_sue_for_peace_nuke_mission", known_tags=known) == "JAP"
          and game_mod.Game._decision_tag_scope(None, None, key="SIA_standing_our_ground_mission", known_tags=known) == "SIA"
          and game_mod.Game._decision_tag_scope(None, None, key="MTG_treaty_renew_mission", known_tags=known) is None)
    # _doctrine_templates 冷启动 oi 为空时回退 win_scan_class 且不把 {} 写进缓存
    class _FakeDocGame(game_mod.Game):
        def __init__(self):
            self.p = types.SimpleNamespace(pid=88123, base=0x140000000, stdstring=lambda _a: "GRAND_DOCTRINE_MOBILE_WARFARE")
            self._oi = types.SimpleNamespace(instances=lambda _c: [])
            self.scan_called = 0

        @property
        def oi(self):
            return self._oi

        def win_scan_class(self, cls_name, use_cache=True):
            self.scan_called += 1
            return [0x15b00001000] if self.scan_called >= 2 else []

    fdoc = _FakeDocGame()
    d0 = fdoc._doctrine_templates()
    cache_after_d0 = getattr(fdoc, "_doc_cache", None)
    d1 = fdoc._doctrine_templates()
    cache_after_d1 = getattr(fdoc, "_doc_cache", None)
    check("C-6 _doctrine_templates 冷启动扫空不缓存 {}，二次调用回退 win_scan_class 成功命中并缓存",
          d0 == {} and cache_after_d0 is None
          and "GRAND_DOCTRINE_MOBILE_WARFARE" in d1
          and cache_after_d1 == d1 and fdoc.scan_called == 2,
          (d0, cache_after_d0, d1, fdoc.scan_called))


# ===================================================================== 11. 复测 9 补充 (D-1 ~ D-4)
def test_retest9_d1_to_d4_fixes():
    print("\n== 11. 复测 9 补充 (D-1~D-4): 4713新师句柄 / 空海区清任务 / 统一拒因 / 零油停船防互搏 ==")
    import equipmeta as EM
    import navalops_win as NW

    # D-1 & D-3: 新训师 {4713, N} 与开局师 {51, N} 均放行；空句柄与 +0xB8 子对象指针被精准拒绝并给出明确原因
    p = _MemProc()
    d_open = 0x15d00001000
    d_new = 0x15d00002000
    d_zero = 0x15d00003000
    p.put_u64(d_open, p.base + AW.CARMY_VT)
    p.put_bytes(d_open + AW.CARMY_HANDLE, struct.pack("<II", 51, 12))
    p.put_u64(d_new, p.base + AW.CARMY_VT)
    p.put_bytes(d_new + AW.CARMY_HANDLE, struct.pack("<II", 4713, 5573))
    p.put_u64(d_new + AW.ORDER_DIV_SUBOBJ_OFF, p.base + 0x29D7000)  # 模拟 +0xB8 子对象虚表
    p.put_u64(d_zero, p.base + AW.CARMY_VT)
    p.put_bytes(d_zero + AW.CARMY_HANDLE, struct.pack("<II", 0, 0))

    val, rej = AW.validate_division_ptrs(p, [d_open, d_new, d_new + AW.ORDER_DIV_SUBOBJ_OFF, d_zero])
    check("D-1/D-3 放行 {51,12} 与 {4713,5573}，并精准指出 +0xB8 子对象指针与空句柄原因",
          val == [d_open, d_new]
          and len(rej) == 2
          and any("subobject" in r["reason"] for r in rej)
          and any("empty CArmy handle" in r["reason"] for r in rej),
          (val, rej))

    # D-3: create_army 与 add_divisions_to_army 对同源无效师返回统一错误前缀并携带 rejected
    og_ptr = 0x15d00009000
    p.put_bytes(og_ptr + AW.OG_IS_GROUP, b"\x00")
    g_d3 = types.SimpleNamespace(p=p, _inj=object())
    ok_ca, err_ca = AW.create_army(g_d3, [d_zero])
    res_add = AW.add_divisions_to_army(g_d3, og_ptr, [d_zero])
    check("D-3 create_army 与 add_divisions_to_army 统一文案并携带 rejected 细节",
          (not ok_ca) and err_ca.startswith("no usable divisions")
          and len(getattr(g_d3, "_last_division_rejected", [])) == 1
          and (not res_add["ok"]) and res_add["error"].startswith("no usable divisions")
          and len(res_add.get("rejected") or []) == 1,
          (err_ca, res_add))

    # D-1 附带优化: housekeep_divisions 遇 rejected 或建军失败立刻短路，绝不循环 4x8=32 次
    class _FakeShortCircuitHK(HK.HousekeepMixin):
        def __init__(self):
            self.add_calls = 0
            self.create_calls = 0

        def unassigned_divisions(self, tag=None):
            return [d_zero]

        def order_groups(self, tag=None):
            return {"groups": [
                {"ptr": hex(0x15d00010000 + i * 0x100), "is_army_group": False, "divisions": 2, "leader_ptr": "0x1"}
                for i in range(6)
            ]}

        def add_divisions_to_army(self, og, divisions=None, tag=None):
            self.add_calls += 1
            return {"ok": False, "error": "no usable divisions", "rejected": [{"ptr": hex(d_zero), "reason": "empty"}]}

        def create_army(self, divs, tag=None):
            self.create_calls += 1
            return {"ok": False, "error": "no usable divisions", "rejected": [{"ptr": hex(d_zero), "reason": "empty"}]}

        def available_generals(self, tag=None):
            return {"generals": []}

        def wartime_status(self):
            return {"enemies": []}

    fsc = _FakeShortCircuitHK()
    r_sc = fsc.housekeep_divisions()
    check("D-1 housekeep_divisions 遇输入师被拒立刻短路（add=1, create=1，不再 4 轮 x 6 军刷爆）",
          fsc.add_calls == 1 and fsc.create_calls == 1 and not r_sc["ok"],
          (fsc.add_calls, fsc.create_calls, r_sc))

    # D-2: FUEL_ZERO 对 regions==0 停港舰队不误报，对 regions>0 正常报；hoi4_naval_mission 文档写明 0=None
    fleets_no_region = [{
        "is_reserve": False, "regions": 0,
        "task_forces": [{"is_reserve": False, "mission": 1, "effective_mission": 1, "regions": 0}],
    }]
    fleets_with_region = [{
        "is_reserve": False, "regions": 1,
        "task_forces": [{"is_reserve": False, "mission": 1, "effective_mission": 1, "regions": 1}],
    }]
    check("D-2 compute_fuel_zero_alert 对 regions==0 停港舰队静默，对 regions>0 正常告警",
          EM.compute_fuel_zero_alert(0.0, [], fleets_no_region) is None
          and EM.compute_fuel_zero_alert(0.0, [], fleets_with_region) is not None,
          (EM.compute_fuel_zero_alert(0.0, [], fleets_no_region),
           EM.compute_fuel_zero_alert(0.0, [], fleets_with_region)))
    tmap = {t["name"]: t for t in MCP.TOOLS}
    check("D-2 hoi4_naval_mission 工具文档已明确写明 mission 0=None 清空任务",
          "0=None" in (tmap["hoi4_naval_mission"].get("description") or ""),
          tmap["hoi4_naval_mission"].get("description"))

    # D-2: remove_regions 在 regions_after == 0 时自动调用 clear_fleet_missions 清任务
    orig_valid = NW.region_is_valid
    orig_mk_rem = NW.make_remove_regions
    orig_can = NW.can_exec
    orig_post = NW.post
    orig_resolve = NW.resolve_handle
    orig_clear = NW.clear_fleet_missions
    clear_called = []
    try:
        fp_mock = 0x15d00020000
        p.put_i32(fp_mock + NW.FLEET_REGION_COUNT_OFF, 0)
        NW.region_is_valid = lambda g, r: (True, 0x1)
        NW.make_remove_regions = lambda g, fh, ids: 0x1234
        NW.can_exec = lambda g, c, rva: True
        NW.post = lambda g, c, seconds=2.5: {"posted": True}
        NW.resolve_handle = lambda g, fh: fp_mock
        NW.clear_fleet_missions = lambda g, fp: (clear_called.append(fp), {
            "ok": True, "attempted": 2, "cleared": 2, "task_forces": []
        })[1]
        rr = NW.remove_regions(types.SimpleNamespace(p=p), (1 << 32) | 0x3D, [9])
        check("D-2 remove_regions 在 regions_after==0 时自动批量清空 TF 任务并回报 mission_cleared=True",
              rr["ok"] and rr["regions_after"] == 0 and rr.get("mission_cleared") is True and clear_called == [fp_mock],
              (rr, clear_called))
    finally:
        NW.region_is_valid = orig_valid
        NW.make_remove_regions = orig_mk_rem
        NW.can_exec = orig_can
        NW.post = orig_post
        NW.resolve_handle = orig_resolve
        NW.clear_fleet_missions = orig_clear

    # D-4: 燃油枯竭（0 油）时 housekeep_navy / housekeep_air 跳过挂任务，且不与 FUEL_ZERO 互相打架
    class _FakeZeroFuelHK(HK.HousekeepMixin):
        def __init__(self, fuel_val):
            self._fuel = fuel_val
            self.air_called = 0

        def _c(self, tag=None):
            return types.SimpleNamespace(tag="GER", fuel=self._fuel)

        def set_air_missions(self, tag=None):
            self.air_called += 1
            return {"ok": True}

        def attach_air_wings_to_armies(self, tag=None):
            return {"ok": True}

    fhk0 = _FakeZeroFuelHK(0.0)
    rn0 = fhk0.housekeep_navy()
    ra0 = fhk0.housekeep_air()
    check("D-4 燃油=0 时 housekeep_navy 与 housekeep_air 自动跳过挂任务（ok=True, assigned=0）",
          rn0["ok"] and rn0["assigned"] == 0 and rn0.get("skipped_reason") == "fuel_critical"
          and ra0["ok"] and ra0.get("skipped_reason") == "fuel_critical" and fhk0.air_called == 0,
          (rn0, ra0, fhk0.air_called))


# ===================================================================== 12. 复测 9 续 (D-5/D-6 & E-1~E-6)
def test_retest9_d5_d6_and_e1_to_e6_fixes():
    print("\n== 12. 复测 9 续 (D-5/D-6 & E-1~E-6): run_plan签名 / 复制询价 / 删单防崩 / 借口天数 / 死进程判别 ==")
    import inspect
    import diplomacy as DIP
    import cmdwin as CW

    # D-5: Game.justify_wargoal 接收 tag=None，且 turnengine 走 justify_wargoal 不抛 TypeError
    sig_jw = inspect.signature(game_mod.Game.justify_wargoal)
    check("D-5 Game.justify_wargoal 签名已支持 tag=None（对齐 turnengine Plan DSL）",
          "tag" in sig_jw.parameters, str(sig_jw))

    class _FakeRunPlanGame(game_mod.Game):
        def __init__(self):
            pass

        def wargoal_quote(self, target_tag, wargoal="take_state", states=None):
            return {"current_pp": 120, "estimated_total_pp": 43.0, "estimated_days": 215.0}

    frpg = _FakeRunPlanGame()
    r_dsl = TE._dispatch_action(frpg, "justify_wargoal", {"target": "HOL", "wargoal": "take_state", "dry_run": True}, "GER")
    check("D-5 turnengine._dispatch_action('justify_wargoal') 携带 tag 不再抛 TypeError",
          r_dsl.get("ok") is True and r_dsl.get("target") == "HOL" and r_dsl.get("dry_run") is True,
          r_dsl)

    # D-6: hoi4_duplicate_division_template 暴露 dry_run 与动态价格说明，dry_run=true 零消耗返回真实 XP 成本
    tmap = {t["name"]: t for t in MCP.TOOLS}
    dup_tool = tmap["hoi4_duplicate_division_template"]
    check("D-6 hoi4_duplicate_division_template 已支持 dry_run 参数并更正动态 XP 费用文档",
          "dry_run" in (dup_tool.get("inputSchema", {}).get("properties") or {})
          and "100+" in (dup_tool.get("description") or ""),
          dup_tool.get("description"))

    # E-1: armyops_win.delete_order 删带 type-1 子命令(assigned=0, executing=1)的父订单时，
    # 先清 executing + 先删子命令再删父订单 + 全部走 attach_fake_vtable + post_drained
    og_e1 = 0x15e00001000
    root_ptr = 0x15e00002000
    kid_ptr = 0x15e00002400
    state_e1 = {
        "roots": [{"id": 37, "type": 2, "ptr": hex(root_ptr), "assigned_divisions": 31}],
        "kids": [{"id": 38, "type": 1, "root": 37, "ptr": hex(kid_ptr), "assigned": 0, "executing": 1}],
        "exec_cleared": [],
        "posted_oids": [],
        "fake_vt_attached": 0,
    }
    p_e1 = _MemProc()
    orig_orders = AW.orders
    orig_kids = AW.order_children
    orig_set_exec = AW.set_order_executing
    orig_alloc = CW.alloc
    orig_fake_vt = CW.attach_fake_vtable
    orig_post = CW.post_drained
    try:
        AW.orders = lambda g, og: list(state_e1["roots"])
        AW.order_children = lambda g, og: list(state_e1["kids"])
        AW.set_order_executing = lambda g, ptr, on=True: (state_e1["exec_cleared"].append((ptr, on)), True)[1]
        alloc_seq = [0x15e00005000]

        def _fake_alloc(g, sz):
            a = alloc_seq[0]
            alloc_seq[0] += 0x100
            return a

        CW.alloc = _fake_alloc
        CW.attach_fake_vtable = lambda g, cmd, vt: (state_e1.__setitem__("fake_vt_attached", state_e1["fake_vt_attached"] + 1), 0)[1]

        def _fake_post(g, cmd, seconds=2.0):
            oid = p_e1.i32(cmd + 0x30)
            state_e1["posted_oids"].append(oid)
            state_e1["kids"] = [k for k in state_e1["kids"] if k["id"] != oid]
            state_e1["roots"] = [r for r in state_e1["roots"] if r["id"] != oid]
            return {"posted": True}

        CW.post_drained = _fake_post

        def _fake_call(fn, args, absolute=True, force=True):
            if fn == p_e1.base + AW.FN_ORDER_DELETE_CTOR:
                cmd, _og, oid = args
                p_e1.put_u64(cmd, p_e1.base + AW.VT_ORDER_DELETE)
                p_e1.put_i32(cmd + 0x30, oid)
                return cmd
            if fn == p_e1.base + AW.CANEXEC_ORDER_DELETE:
                return 1
            return 0

        g_e1 = types.SimpleNamespace(p=p_e1, _inj=object(), call=_fake_call)
        # 先验证单独删子命令仍受保护
        r_child_block = AW.delete_order(g_e1, og_e1, 38)
        check("E-1 单独删除 type-1 子命令(allow_child=False)仍被安全拦阻",
              not r_child_block["ok"] and r_child_block.get("blocked_by") == "child_delete_unsafe",
              r_child_block)

        # 再验证删父订单 37：先清 executing -> 先删子命令 38 -> 再删父订单 37，全走 fake_vtable
        r_del = AW.delete_order(g_e1, og_e1, 37)
        check("E-1 delete_order 删父订单 37 先停后删子命令 38，走 fake_vtable 并彻底清零",
              r_del["ok"] and r_del["deleted_children"] == [38]
              and state_e1["posted_oids"] == [38, 37]
              and state_e1["fake_vt_attached"] == 2
              and r_del["remaining_orders"] == 0 and r_del["remaining_children"] == 0
              and any(x[0] in (kid_ptr, hex(kid_ptr)) and x[1] is False for x in state_e1["exec_cleared"]),
              (r_del, state_e1))

        # 若子命令删不掉，绝不能继续删父订单留下孤儿
        state_e1["roots"] = [{"id": 37, "type": 2, "ptr": hex(root_ptr), "assigned_divisions": 31}]
        state_e1["kids"] = [{"id": 38, "type": 1, "root": 37, "ptr": hex(kid_ptr), "assigned": 0, "executing": 1}]
        state_e1["posted_oids"].clear()
        CW.post_drained = lambda g, cmd, seconds=2.0: {"posted": False}
        r_refuse = AW.delete_order(g_e1, og_e1, 37)
        check("E-1 子命令删除失败时严禁删除父订单（杜绝孤儿 type-1 炸引擎）",
              (not r_refuse["ok"]) and r_refuse.get("blocked_by") == "child_delete_failed"
              and len(state_e1["roots"]) == 1,
              (r_refuse, state_e1["roots"]))
    finally:
        AW.orders = orig_orders
        AW.order_children = orig_kids
        AW.set_order_executing = orig_set_exec
        CW.alloc = orig_alloc
        CW.attach_fake_vtable = orig_fake_vt
        CW.post_drained = orig_post

    # E-6: CTimedWargoalActivity +0x28 单位直接就是游戏日（210/215 -> 剩余 5 天，不再误除以 3 报 145 天）
    p_e6 = _MemProc()
    act1 = 0x15e00010000
    act2 = 0x15e00011000
    FIXED = 100000
    # 模拟 1937.10.24 启动、已走 210 天 / 总 215 天（1938.05.22 实测现场）
    p_e6.put_i32(act1 + 0x10, 1)                 # src GER
    p_e6.put_i32(act1 + 0x14, 8)                 # tgt HOL
    p_e6.put_u64(act1 + 0x18, 215 * FIXED)       # total = 215.0 days
    p_e6.put_u64(act1 + 0x20, 1 * FIXED)         # per_day = 1.0
    p_e6.put_u64(act1 + 0x28, 210 * FIXED)       # done = 210.0 days (NOT 8-hour ticks!)
    p_e6.put_i32(act1 + 0x38, 0)
    p_e6.put_i32(act1 + 0x50, 0)
    p_e6.put_i32(act1 + 0x74, 1)
    # 第二条更慢的活动（剩余 80 天），测试多活动自动按 days_left 升序排列
    p_e6.put_i32(act2 + 0x10, 1)
    p_e6.put_i32(act2 + 0x14, 8)
    p_e6.put_u64(act2 + 0x18, 215 * FIXED)
    p_e6.put_u64(act2 + 0x20, 1 * FIXED)
    p_e6.put_u64(act2 + 0x28, 135 * FIXED)
    p_e6.put_i32(act2 + 0x38, 0)
    p_e6.put_i32(act2 + 0x50, 0)
    p_e6.put_i32(act2 + 0x74, 1)

    g_e6 = types.SimpleNamespace(
        p=p_e6,
        oi=types.SimpleNamespace(instances=lambda cls: [act2, act1] if cls == "CTimedWargoalActivity" else []),
        tags=lambda: ["GER", "HOL"],
        date_raw=lambda: 16980000,
        country=lambda t: types.SimpleNamespace(tag=t, tag_id=(1 if t == "GER" else 8), political_power=0.5),
    )
    orig_ti = DIP.tag_index
    try:
        DIP.tag_index = lambda g, t: 1 if t == "GER" else 8
        jrows = DIP.justifying(g_e6)
    finally:
        DIP.tag_index = orig_ti
    check("E-6 justifying 严格按官方 0xb2c09d/0xb2adb0 读出 210/215 -> 剩余 5.0 天且检测 PP 不足暂停与升序排列",
          len(jrows) == 2
          and jrows[0]["progress_days"] == 210.0
          and jrows[0]["days_left"] == 5.0
          and jrows[0]["paused_by_pp"] is True
          and jrows[1]["days_left"] == 80.0,
          jrows)

    # E-4: hoi4_pause_in_hours 进程已死(OSError 299 / pid 不存活)直报 process_dead=True，绝不误报“事件弹窗”
    import session as _sess_e4
    import windowstate as _ws_e4
    orig_game_fn = MCP.game
    orig_sess_pause = _sess_e4.pause
    orig_ws_state = _ws_e4.state
    orig_alive = MCP._is_pid_alive
    orig_refuse = MCP._refuse_unpause_with_empty_orders
    orig_pause_last = MCP._PAUSE_LAST[0]
    try:
        MCP._PAUSE_LAST[0] = 0.0
        MCP._refuse_unpause_with_empty_orders = lambda g: None
        calls_d = [0]

        class _DeadGame:
            p = types.SimpleNamespace(pid=999999)

            def date(self):
                calls_d[0] += 1
                if calls_d[0] <= 1:
                    return {"raw": 16980000, "text": "1938.05.22.01"}
                raise OSError(299, "read 0x7ff69854db08+8")

        MCP.game = lambda: _DeadGame()
        _sess_e4.pause = lambda g, on: (True, "ok") if not on else (False, "OSError(299, 'read')")
        _ws_e4.state = lambda pid: {"hwnd": None, "reason": "no titled window for this pid"}
        MCP._is_pid_alive = lambda pid: False
        # 跳过红点阻塞以直达轮询死亡路径
        orig_gate = RD.check_unpause_gate
        try:
            RD.check_unpause_gate = lambda g, ensure_paused=True: None
            r_dead = MCP._pause({"hours": 24})
        finally:
            RD.check_unpause_gate = orig_gate
        res_dead = r_dead.get("result") or {}
        check("E-4 hoi4_pause_in_hours 进程已死时直报 process_dead=True，绝不误报事件弹窗",
              res_dead.get("ok") is False
              and res_dead.get("process_dead") is True
              and res_dead.get("blocked_by") == "process_dead"
              and "World-News" not in (res_dead.get("hint") or ""),
              r_dead)
    finally:
        MCP.game = orig_game_fn
        _sess_e4.pause = orig_sess_pause
        _ws_e4.state = orig_ws_state
        MCP._is_pid_alive = orig_alive
        MCP._refuse_unpause_with_empty_orders = orig_refuse
        MCP._PAUSE_LAST[0] = orig_pause_last

    # E-3 & E-5: deploy_to_border 工具说明写清不碰前线订单；plan_front_allocation 带 as_of_date 与 snapshot_note
    check("E-3 hoi4_deploy_to_border 描述已写明仅逐师移动、不改前线订单",
          "does NOT change or remove any army's existing front-line order" in (tmap["hoi4_deploy_to_border"].get("description") or ""),
          tmap["hoi4_deploy_to_border"].get("description"))

    import macros as _mc_e5
    import staff_supply as _ss_e5
    orig_wef = DIP.war_escalation_forecast
    orig_dob = DIP.divisions_on_border
    orig_fb = _ss_e5.front_budget
    try:
        DIP.war_escalation_forecast = lambda g, tgt, me_tag=None: {
            "guaranteed_by": [], "co_belligerents": [], "co_belligerent_borders": [],
            "total_enemy_divisions_est": 10, "diplomatic_blockers": [], "diplomatic_warnings": [],
        }
        DIP.divisions_on_border = lambda g, tgt, me_tag=None: {
            "border_provinces": 5, "divisions_on_border": 5,
        }
        _ss_e5.front_budget = lambda g, tgt, me=None: {
            "capacity": 12.0, "measured_capped_provinces": [],
        }
        g_e5 = types.SimpleNamespace(
            player_tag=lambda: "GER",
            date=lambda: {"year": 1938, "month": 2, "day": 25, "text": "1938.02.25.01"},
            divisions=lambda tag, limit=500: {"count": 40, "divisions": []},
        )
        r_e5 = _mc_e5.plan_front_allocation(g_e5, "HOL")
        check("E-5 plan_front_allocation 返回包含 as_of_date 与动态保独提醒 snapshot_note",
              r_e5.get("as_of_date") == "1938.02.25.01"
              and "guaranteed_by" in (r_e5.get("snapshot_note") or ""),
              (r_e5.get("as_of_date"), r_e5.get("snapshot_note")))
    finally:
        DIP.war_escalation_forecast = orig_wef
        DIP.divisions_on_border = orig_dob
        _ss_e5.front_budget = orig_fb



class TestRetest8And9(unittest.TestCase):
    def test_retest8_fixes(self):
        reset_counts()
        globals()["test_retest8_fixes"]()
        _p, _f = counts()
        self.assertEqual(_f, 0, f"test_retest8_fixes failed: {FAILURES}")

    def test_retest9_c1_to_c9_fixes(self):
        reset_counts()
        globals()["test_retest9_c1_to_c9_fixes"]()
        _p, _f = counts()
        self.assertEqual(_f, 0, f"test_retest9_c1_to_c9_fixes failed: {FAILURES}")

    def test_retest9_d1_to_d4_fixes(self):
        reset_counts()
        globals()["test_retest9_d1_to_d4_fixes"]()
        _p, _f = counts()
        self.assertEqual(_f, 0, f"test_retest9_d1_to_d4_fixes failed: {FAILURES}")

    def test_retest9_d5_d6_and_e1_to_e6_fixes(self):
        reset_counts()
        globals()["test_retest9_d5_d6_and_e1_to_e6_fixes"]()
        _p, _f = counts()
        self.assertEqual(_f, 0, f"test_retest9_d5_d6_and_e1_to_e6_fixes failed: {FAILURES}")


if __name__ == "__main__":
    unittest.main()
