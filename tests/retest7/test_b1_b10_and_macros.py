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


def test_b1_b7_wargoal_direct():
    print("\n== 1. B-1 & B-7: O(1) CDiplomacyStatus+0x80 实时直读 CWarGoal ==")
    p = _MemProc()
    c_ger = 0x200000
    c_bel = 0x210000
    dip_ger = 0x300000
    arr_wg = 0x310000
    wg_ptr = 0x320000
    t_take_state = 0x400000

    p.put_i32(c_ger + 0x08, 1)
    p.put_i32(c_bel + 0x08, 25)
    p.put_u64(c_ger + 0xF88, dip_ger)
    p.put_u64(dip_ger + DIP.DP_WARGOALS, arr_wg)
    p.put_i32(dip_ger + DIP.DP_WARGOALS + 8, 2)
    p.put_i32(dip_ger + DIP.DP_WARGOALS + 12, 1)
    p.put_u64(arr_wg, wg_ptr)
    p.put_u64(wg_ptr + DIP.WG_ID, 0x123456789)
    p.put_i32(wg_ptr + DIP.WG_OWNER, 1)
    p.put_i32(wg_ptr + DIP.WG_TARGET, 25)
    p.put_u64(wg_ptr + DIP.WG_TYPE, t_take_state)

    class _FakeG:
        def __init__(self):
            self.p = p
            self._tags = ["---", "GER"] + ["X%d" % i for i in range(2, 25)] + ["BEL"]
            self.oi = types.SimpleNamespace(
                live_instances=lambda _cls: [],
                deep_instances=lambda _cls: [],
            )

        def _load_tags(self):
            return self._tags

        def country_ptrs(self):
            return [c_ger, c_bel]

        def country(self, tag):
            t = str(tag).upper()
            if t == "GER":
                return types.SimpleNamespace(ptr=c_ger, tag="GER")
            if t == "BEL":
                return types.SimpleNamespace(ptr=c_bel, tag="BEL")
            raise KeyError(t)

        def player(self):
            return self.country("GER")

    fg = _FakeG()
    with mock.patch.object(DIP, "wargoal_types", return_value={"take_state": t_take_state}):
        rows, ok = DIP.country_wargoals_direct(fg, tag="GER", valid_only=True)
        check("country_wargoals_direct O(1) 命中新造 GER->BEL take_state",
              ok and len(rows) == 1 and rows[0]["target_tag"] == "BEL" and rows[0]["wargoal"] == "take_state",
              (ok, rows))
        cb = DIP.find_wargoal(fg, "BEL", source_tag="GER", deep=False)
        check("find_wargoal(deep=False) 即使 oi.live_instances 为空仍命中真实 CB",
              cb is not None and cb["target_tag"] == "BEL" and cb["source"] == "CDiplomacyStatus+0x80",
              cb)
        cb_miss = DIP.find_wargoal(fg, "GER", source_tag="GER", deep=False)
        check("不存在的 CB 返回 None", cb_miss is None)


# ===================================================================== 2. B-2: 补给容量模型校准
def test_b2_supply_model():
    print("\n== 2. B-2: staff_supply 校准系数 + 同州共享折减 + 实测断补封顶 ==")
    check("DEFAULTS.divs_per_infra 已从失真 3.0 校准至 <= 0.8",
          SS.DEFAULTS["divs_per_infra"] <= 0.8 and SS.DEFAULTS["base_divs"] <= 1.5,
          SS.DEFAULTS)

    # 模拟 4 个共享同一州 (state=51) 的比利时边境省，每个 4 级基建，距离枢纽 1 步
    fake_caps = {
        3547: {"prov": 3547, "state": 51, "infra": 4, "cap_divs": 4.2},
        9522: {"prov": 9522, "state": 51, "infra": 4, "cap_divs": 4.2},
        11435: {"prov": 11435, "state": 51, "infra": 4, "cap_divs": 4.2},
        11470: {"prov": 11470, "state": 51, "infra": 4, "cap_divs": 4.0},
    }
    # 实测 3547 已经站了 10 师但补给率跌到 0.15 且断补 30 天 -> feedable = 10*(0.15/0.65) = 2.31
    fake_load = {
        3547: {"prov": 3547, "n": 10, "pct": 0.15, "oos_days": 30.0},
    }
    fake_mm = types.SimpleNamespace(_border_provinces=lambda _g, _e: [3547, 9522, 11435, 11470])
    with mock.patch.dict(sys.modules, {"movemicro": fake_mm}), \
         mock.patch.object(SS, "capacity", return_value=fake_caps), \
         mock.patch.object(SS, "load", return_value=fake_load):
        fb = SS.front_budget(object(), "BEL", "GER")
    check("4 省同州共享折减后总上限大幅低于旧版 41.31（应在 7..14 合理区间）",
          fb["ok"] and 7.0 <= fb["capacity"] <= 14.0, fb)
    check("断补省 3547 被实测值 feedable 封顶并记录在 measured_capped_provinces",
          fb["per_province"][3547] <= 2.4 and len(fb["measured_capped_provinces"]) == 1, fb)


# ===================================================================== 3. B-3: front_changes 跨档基线
def test_b3_front_changes():
    print("\n== 3. B-3: front_changes 跨读档陈旧基线自动重置与清理 ==")
    cache = game_mod.FRONT_SNAPSHOT_PATH
    os.makedirs(os.path.dirname(os.path.abspath(cache)), exist_ok=True)
    old_exists = os.path.exists(cache)
    old_data = open(cache, "rb").read() if old_exists else None
    try:
        # 写入一个 78 分钟前、同 PID、同 1940.12 日期但 150 个省与新局不同的残留基线
        stale_ctrl = {str(i): ("GER" if i <= 200 else "FRA") for i in range(1, 251)}
        with open(cache, "w", encoding="utf-8") as fh:
            json.dump({
                "schema": 2,
                "ts": time.time() - 4700.0,
                "pid": 37800,
                "date": 17002000,
                "player_tag": "GER",
                "control": stale_ctrl,
            }, fh)

        new_ctrl = {i: ("HOL" if i <= 150 else ("GER" if i <= 200 else "FRA")) for i in range(1, 251)}

        class _FG(game_mod.Game):
            def __init__(self):
                self.p = types.SimpleNamespace(pid=37800)

            def date_raw(self):
                return 17002100  # 只比上次晚 4 天，没倒退

            def player_tag(self):
                return "GER"

        fake_md = types.SimpleNamespace(province_controller_map=lambda _g: new_ctrl)
        with mock.patch.dict(sys.modules, {"mapdata": fake_md}):
            res = _FG().front_changes()
        check("同 PID 跨读档 78 分钟残留 150 省差异自动触发 stale_cross_save_baseline 重建",
              res["first_run"] is True and res["reset_reason"] == "stale_cross_save_baseline"
              and res["changed_count"] == 0,
              res)
        check("reset_front_snapshot 能清理磁盘快照",
              game_mod.Game.reset_front_snapshot() is True and not os.path.exists(cache))
    finally:
        if old_exists and old_data is not None:
            with open(cache, "wb") as fh:
                fh.write(old_data)
        elif os.path.exists(cache):
            os.remove(cache)


# ===================================================================== 4. B-4 & B-9: wait_for 与 peace 信号
def test_b4_b9_wait_and_peace():
    print("\n== 4. B-4 & B-9: wait_for 26s 硬上限 + 和会 O(1) 纯只读信号 ==")
    # 测试 peacewin.live_conference_readonly O(1) 纯只读
    p = _MemProc()
    gs = 0x500000
    arr = 0x510000
    conf = 0x520000
    p.put_u64(p.base + 0x33DD6B0, gs)
    p.put_i32(gs + PW.GS_PEACE_CONF + 0x14, 1)
    p.put_u64(gs + PW.GS_PEACE_CONF + 0x08, arr)
    p.put_u64(arr, conf)
    fg = types.SimpleNamespace(p=p, date=lambda: {"text": "1940.06.01.00"})
    check("peacewin.live_conference_readonly O(1) 无注入读出 CPeaceConference*",
          PW.live_conference_readonly(fg) == conf)
    # 计数归 0 返回 None
    p.put_i32(gs + PW.GS_PEACE_CONF + 0x14, 0)
    check("无和会时 live_conference_readonly 返回 None",
          PW.live_conference_readonly(fg) is None)

    # turnloop wake_reasons 监测 peace 从 False -> True
    reasons = TL.wake_reasons(
        {"tag": "GER", "peace_open": False},
        {"tag": "GER", "peace_open": True},
    )
    check("turnloop wake_reasons 在和会开启当帧触发 'peace conference opened'",
          "peace conference opened" in reasons, reasons)
    check("'peace' 已加入 turnloop.ALL_SIGNALS", "peace" in TL.ALL_SIGNALS)

    # MCP hoi4_wait_for 超时上限钳位 <= 26s（避免 30s -32001）
    captured = {}

    class _FakeTL:
        def __init__(self, _g, poll=1.5):
            pass

        def wait(self, **kw):
            captured.update(kw)
            return {"ok": True, "waited_game_days": 0}

    with mock.patch.object(MCP, "game", return_value=object()), \
         mock.patch.object(TL, "TurnLoop", _FakeTL):
        MCP._wait_for({"max_days": 30, "timeout_seconds": 38})
    check("hoi4_wait_for 将 38s 请求钳位至 <= 26s 防 -32001",
          captured.get("timeout_seconds") <= 26, captured)


# ===================================================================== 5. B-5 & B-6: 边境驻军与本军指派口径
def test_b5_b6_border_and_assigned():
    print("\n== 5. B-5 & B-6: 统一边境驻军口径 + armyops_win.assigned_breakdown ==")
    # B-5: 确认 diplomacy._border_exposure_for_tags 调用 divenum.armies（而非旧 bug enumerate_divisions）
    fake_md = types.SimpleNamespace(
        province_controller_map=lambda _g: {10: "GER", 11: "GER", 20: "FRA", 30: "POL"},
        build_adjacency=lambda: {10: [20], 11: [30], 20: [10], 30: [11]},
        load_definitions=lambda: ({}, {10: "land", 11: "land", 20: "land", 30: "land"}),
    )
    fake_de = types.SimpleNamespace(armies=lambda _g, _t: {"count": 3, "armies": [101, 102, 103]})
    fake_uo = types.SimpleNamespace(province_of=lambda _g, a: 10 if a in (101, 102) else 11)
    fg = types.SimpleNamespace(p=None)
    with mock.patch.dict(sys.modules, {"mapdata": fake_md, "divenum": fake_de, "unitorders": fake_uo}):
        bm = DIP._border_exposure_for_tags(fg, "GER", ["FRA", "POL"])
        bd_fra = DIP.divisions_on_border(fg, "FRA", me_tag="GER")
    check("B-5 _border_exposure_for_tags 与 divisions_on_border 正确读取 FRA=2 师、POL=1 师（不再恒报 0）",
          bm["FRA"]["divisions_on_border"] == 2 and bm["POL"]["divisions_on_border"] == 1
          and bd_fra["divisions_on_border"] == 2,
          (bm, bd_fra))

    # B-6: 16 师本军挂在 24 师共享前线订单上，assigned_breakdown 精确拆分 16 vs 24
    p = _MemProc()
    og_ptr = 0x600000
    ord_ptr = 0x610000
    arr_ptr = 0x620000
    my_divs = [0x700000 + i * 0x100 for i in range(16)]
    other_divs = [0x800000 + i * 0x100 for i in range(8)]
    all_24 = my_divs + other_divs
    p.put_u64(ord_ptr + 0x210, arr_ptr)
    p.put_i32(ord_ptr + 0x210 + 0xC, 24)
    for i, d in enumerate(all_24):
        p.put_u64(arr_ptr + 8 * i, d)
    g_b6 = types.SimpleNamespace(p=p)
    with mock.patch.object(AW, "divisions_of", return_value=my_divs), \
         mock.patch.object(AW, "orders", return_value=[{"id": 25, "ptr": hex(ord_ptr)}]):
        br = AW.assigned_breakdown(g_b6, og_ptr, 25)
        a_of = AW.assigned_of(g_b6, og_ptr, 25)
    check("B-6 assigned_breakdown 精确拆分本军 16 师 / 前线共享总计 24 师",
          br["army_assigned"] == 16 and br["front_total_assigned"] == 24
          and br["army_divisions"] == 16 and a_of == 16,
          (br, a_of))


# ===================================================================== 6. B-8 & B-10: 建造配额与自动挪厂
def test_b8_b10_construction_and_reclaim():
    print("\n== 6. B-8 & B-10: 炼油厂封顶混合配额 + 满槽缓存 + set_production 自动抽厂 ==")
    HK._REJECTED_STATE_CACHE.clear()

    class _FakeHK(HK.HousekeepMixin):
        def __init__(self):
            self.p = types.SimpleNamespace(pid=88801)
            self.q = []
            self.add_calls = []

        def _c(self, tag=None):
            return types.SimpleNamespace(tag="GER")

        def date(self):
            return {"year": 1940, "month": 5, "day": 1}

        def resources(self, tag=None):
            return {"rubber": -10}

        def player(self):
            return types.SimpleNamespace(summary=lambda: {"fuel": 0.0})

        def researched(self, tag=None):
            return ["synth_oil_experiments"]

        def states(self, tag=None):
            return [{"id": 64}, {"id": 51}, {"id": 55}, {"id": 56}]

        def state_detail(self, sid, tag=None):
            return {
                "id": sid,
                "slots_free": 4,
                "buildings": {"infrastructure": 5, "synthetic_refinery": 0},
            }

        def construction_queue(self, tag=None):
            return list(self.q)

        def add_construction(self, sid, bldg):
            self.add_calls.append((sid, bldg))
            # 模拟州 64 被引擎拒绝
            if sid == 64:
                return {"ok": False, "error": "queue did not grow"}
            self.q.append({"state": sid, "building": bldg})
            return {"ok": True}

    fk = _FakeHK()
    r1 = fk.housekeep_construction(target=6)
    n_synth = sum(1 for x in r1["added"] if x["building"] == "synthetic_refinery")
    n_mil = sum(1 for x in r1["added"] if x["building"] == "arms_factory")
    check("B-8 低燃油 target=6 时最多排 2 座炼油厂，其余 4 座自动排 arms_factory",
          n_synth == 2 and n_mil == 4 and len(r1["added"]) == 6,
          (n_synth, n_mil, r1))

    # 二次调用时满槽州 64 在缓存中直接跳过不再试错
    fk.add_calls.clear()
    fk.q = fk.q[:4]  # 再补 2 个 arms_factory
    r2 = fk.housekeep_construction(target=6)
    tried_64 = [c for c in fk.add_calls if c[0] == 64]
    check("B-8 跨调用短 TTL 缓存直接跳过刚被拒绝的州 64",
          len(tried_64) == 0 and len(r2["added"]) == 2
          and all(x["building"] == "arms_factory" for x in r2["added"]),
          (tried_64, r2))

    # B-10: set_production_amount 在 idle=0 时自动从超储线抽厂
    p = _MemProc()
    c_ptr = 0x900000
    prod_ptr = 0x910000
    arr_lines = 0x920000
    l0, l1 = 0x930000, 0x930100
    p.put_u64(c_ptr + game_mod.CO_PRODUCTION, prod_ptr)
    p.put_u64(prod_ptr + game_mod.PS_MIL_LINES, arr_lines)
    p.put_i32(prod_ptr + game_mod.PS_MIL_LINES + 12, 2)
    p.put_u64(arr_lines, l0)
    p.put_u64(arr_lines + 8, l1)
    p.put_i32(l0 + game_mod.ML_FACTORIES, 16)  # 步枪线 16 厂
    p.put_i32(l1 + game_mod.ML_FACTORIES, 4)   # 炮兵线 4 厂

    class _FakeProdGame(game_mod.Game):
        def __init__(self):
            self.p = p
            self.total_mil = 20

        def _c(self, tag=None):
            return types.SimpleNamespace(ptr=c_ptr, tag="GER")

        def handle_of(self, line_ptr):
            return {"type": 56, "id": 1 if line_ptr == l0 else 2}

        def _settle(self, _s=0.1):
            pass

        def factory_allocation(self, tag=None):
            used = p.i32(l0 + game_mod.ML_FACTORIES) + p.i32(l1 + game_mod.ML_FACTORIES)
            return {"military_available": self.total_mil, "military_idle": max(0, self.total_mil - used)}

        def production_lines(self, tag=None, include_obsolete=False):
            return [
                {"index": 0, "class": "CMilitaryProductionLine", "equipment": "infantry_equipment_1",
                 "factories": p.i32(l0 + game_mod.ML_FACTORIES)},
                {"index": 1, "class": "CMilitaryProductionLine", "equipment": "artillery_equipment_2",
                 "factories": p.i32(l1 + game_mod.ML_FACTORIES)},
            ]

        def equipment_stockpile(self, tag=None):
            return {"totals_by_equipment": {"infantry_equipment_1": 30000.0, "artillery_equipment_2": 0.0}}

        def divisions(self, tag=None, limit=1):
            return {"count": 50}

        def send_command(self, name, args, tag=None):
            lid = args["line_id"] >> 32
            amt = args["amount"]
            if amt >= 0x80000000:
                amt -= 0x100000000
            lp = l0 if lid == 1 else l1
            cur = p.i32(lp + game_mod.ML_FACTORIES)
            idle = self.factory_allocation()["military_idle"]
            if amt > 0:
                add = min(amt, idle)
                p.put_i32(lp + game_mod.ML_FACTORIES, cur + add)
            else:
                p.put_i32(lp + game_mod.ML_FACTORIES, max(0, cur + amt))

    fgp = _FakeProdGame()
    rp = fgp.set_production_amount(1, 9, auto_reclaim=True)
    check("B-10 idle=0 时给炮兵加到 9 厂，自动从超储步枪线抽回 5 厂并返回 reclaimed_from",
          rp["ok"] and rp["after"] == 9 and p.i32(l0 + game_mod.ML_FACTORIES) == 11
          and rp.get("reclaimed_total") == 5 and len(rp.get("reclaimed_from") or []) == 1,
          rp)


# ===================================================================== 7. 反糊弄红点门禁 & 宣战锁
def test_anti_cheese_reddots_and_declare_war():
    print("\n== 7. 反糊弄红点门禁 + MCP 封锁 override_checks ==")
    tmp_dir = tempfile.mkdtemp(prefix="hoi4_rd_retest7_")
    old_sp = RD.STATE_PATH
    RD.STATE_PATH = os.path.join(tmp_dir, "rd.json")
    RD.reset_state(persist=False)
    RD.configure(**RD.DEFAULT_CONFIG)

    fake_eval = {
        "ok": True,
        "tag": "GER",
        "date": "1940.06.01.00",
        "date_raw": 17000000,
        "at_war": False,
        "has_blocking": True,
        "blocking": [
            {"code": "IDLE_MIL", "tier": 3, "signature": "IDLE_MIL:3", "detail": {}},
            {"code": "ARMY_XP_CAP", "tier": 100, "signature": "ARMY_XP_CAP:100", "detail": {}},
            {"code": "PP_CAP", "tier": 500, "signature": "PP_CAP:500", "detail": {}},
            {"code": "NAVY_XP_CAP", "tier": 100, "signature": "NAVY_XP_CAP:100", "detail": {}},
            {"code": "AIR_XP_CAP", "tier": 100, "signature": "AIR_XP_CAP:100", "detail": {}},
            {"code": "FUEL_ZERO", "tier": 1, "signature": "FUEL_ZERO:1", "detail": {}},
        ],
        "acked": [],
        "advisory": [],
    }
    fg = types.SimpleNamespace(p=types.SimpleNamespace(pid=77709), player_tag=lambda: "GER")
    try:
        with mock.patch.object(RD, "evaluate", return_value=fake_eval):
            r_low = RD.ack(fg, "PP_CAP", "稍后", persist=False)
            check("空话套话理由 '稍后' 被 low_effort_reason 拒签",
                  not r_low["ok"] and r_low.get("blocked_by") == "low_effort_reason", r_low)

            r_auto = RD.ack(fg, "IDLE_MIL", "先把军工留着以后再说", persist=False)
            check("一键可清项 IDLE_MIL 拒绝普通 ack (auto_solvable_red_dot)",
                  not r_auto["ok"] and r_auto.get("blocked_by") == "auto_solvable_red_dot", r_auto)

            # 单日最多 4 次 ack，第 5 条触发 ack_budget_exceeded（封死连刷 11 条！）
            ok1 = RD.ack(fg, "PP_CAP", "留 PP 准备切战时经济法案", persist=False)
            ok2 = RD.ack(fg, "ARMY_XP_CAP", "等下回合升级 9步3炮编制", persist=False)
            ok3 = RD.ack(fg, "NAVY_XP_CAP", "攒海军经验点潜艇学说", persist=False)
            ok4 = RD.ack(fg, "AIR_XP_CAP", "攒空军经验改战斗机引擎", persist=False)
            r_5th = RD.ack(fg, "FUEL_ZERO", "等下个月炼油厂完工再补油", persist=False)
            check("单日连续第 5 条签收被 ack_budget_exceeded 硬拦",
                  all(x["ok"] for x in (ok1, ok2, ok3, ok4))
                  and (not r_5th["ok"]) and r_5th.get("blocked_by") == "ack_budget_exceeded",
                  r_5th)

            # 同项连续续签第 3 次（跨日但从未解决）被 ack_streak_exceeded 硬拦
            fake_eval["date_raw"] += 35 * 24
            ok_s2 = RD.ack(fg, "ARMY_XP_CAP", "再攒 10 天准备改中坦编制", persist=False)
            fake_eval["date_raw"] += 35 * 24
            r_s3 = RD.ack(fg, "ARMY_XP_CAP", "继续推迟不改编制", persist=False)
            check("ARMY_XP_CAP 连续第 3 次续签被 ack_streak_exceeded 硬拦",
                  ok_s2["ok"] and (not r_s3["ok"]) and r_s3.get("blocked_by") == "ack_streak_exceeded",
                  (ok_s2, r_s3))
    finally:
        RD.STATE_PATH = old_sp
        RD.reset_state(persist=False)

    # MCP hoi4_declare_war schema 不暴露 override_checks，且强传被拦截
    tool_map = {t["name"]: t for t in MCP.TOOLS}
    dw_props = tool_map["hoi4_declare_war"]["inputSchema"]["properties"]
    check("MCP hoi4_declare_war schema 已移除 override_checks",
          "override_checks" not in dw_props, list(dw_props.keys()))
    with mock.patch.dict(os.environ, {"HOI4_ALLOW_OVERRIDE_CHECKS": "0"}):
        r_ov = MCP._declare_war({"target": "FRA", "override_checks": True})
    check("强传 override_checks=True 被 override_checks_disabled 硬拒",
          not r_ov["ok"] and r_ov.get("blocked_by") == "override_checks_disabled", r_ov)


# ===================================================================== 8. 三大老手宏工具 + 战略看板 + Compact
def test_three_macros_and_health_board():
    print("\n== 8. 三大老手宏工具 + 战略体检看板 + Compact 5 工具兼容 ==")

    class _MacroGame:
        def __init__(self):
            self.lines = [
                {"index": 0, "class": "CMilitaryProductionLine", "equipment": "infantry_equipment_1", "factories": 16},
                {"index": 1, "class": "CMilitaryProductionLine", "equipment": "artillery_equipment_2", "factories": 4},
                {"index": 2, "class": "CMilitaryProductionLine", "equipment": "support_equipment_1", "factories": 3},
                {"index": 3, "class": "CMilitaryProductionLine", "equipment": "fighter_equipment_1", "factories": 6},
                {"index": 4, "class": "CMilitaryProductionLine", "equipment": "tac_bomber_equipment_1", "factories": 3},
            ]
            self.tpl_comp = {"line": {"infantry": 9}, "support": {"engineer": 1}}
            self.edit_calls = []

        def player_tag(self):
            return "GER"

        def date(self):
            return {"year": 1940, "month": 6, "day": 1}

        def country(self, tag=None):
            return types.SimpleNamespace(
                tag=str(tag or "GER").upper(),
                military_factories=32,
                experience={"army": 291.0, "navy": 50.0, "air": 80.0},
            )

        def factory_allocation(self, tag=None):
            used = sum(x["factories"] for x in self.lines)
            return {"military_available": 32, "military_used": used, "military_idle": max(0, 32 - used)}

        def production_lines(self, tag=None, include_obsolete=False):
            return [dict(x) for x in self.lines]

        def equipment_stockpile(self, tag=None):
            return {"totals_by_equipment": {
                "infantry_equipment_1": 25037.0,
                "artillery_equipment_2": 0.0,
                "support_equipment_1": 150.0,
            }}

        def divisions(self, tag=None, limit=500):
            if str(tag or "GER").upper() != "GER":
                return {"count": 120, "divisions": []}
            return {
                "count": 111,
                "divisions": [{"template": "Infanterie-Division"} for _ in range(111)],
            }

        def division_templates(self, tag=None):
            return [{"name": "Infanterie-Division"}, {"name": "Panzer-Division"}]

        def template_composition(self, name, tag=None):
            if "Panzer" in name:
                return {"line": {"light_armor": 4, "motorized": 2}, "support": {}}
            return dict(self.tpl_comp)

        def template_edit(self, name, line=None, support=None, dry_run=False, tag=None):
            self.edit_calls.append((name, line, support, dry_run))
            if not dry_run:
                self.tpl_comp = {"line": dict(line or {}), "support": dict(support or {})}
            return {"ok": True, "template": name, "after": self.tpl_comp}

        def set_production_amount(self, idx, amount, tag=None, auto_reclaim=True):
            bf = self.lines[idx]["factories"]
            self.lines[idx]["factories"] = int(amount)
            return {"ok": True, "line": idx, "before": bf, "after": int(amount)}

    mg = _MacroGame()

    # 8.1 战略体检看板
    with mock.patch.object(SS, "load", return_value={3547: {"n": 19, "pct": 0.15, "oos_days": 30.0}}):
        hb = MC.strategic_health_board(mg, "GER")
    hcodes = {a["code"] for a in hb["alerts"]}
    check("战略体检看板同时揪出弱编/产能倒挂/扩军落后/断补危机",
          {"WEAK_INFANTRY_TEMPLATE", "PRODUCTION_IMBALANCE", "ARMY_UNDER_TARGET", "SUPPLY_CRISIS"} <= hcodes,
          hcodes)
    hb_md = "\n".join(MC.format_health_board_markdown(hb))
    check("战略体检 Markdown 渲染包含具体宏工具建议",
          "hoi4_upgrade_template_preset" in hb_md and "hoi4_rebalance_production" in hb_md, hb_md)

    # 8.2 宏 1: rebalance_production
    rb_dry = MC.rebalance_production(mg, "GER", dry_run=True)
    inf_after = next((c["target"] for c in rb_dry["plan"] if c["line"] == 0), mg.lines[0]["factories"])
    art_after = next((c["target"] for c in rb_dry["plan"] if c["line"] == 1), mg.lines[1]["factories"])
    tac_after = next((c["target"] for c in rb_dry["plan"] if c["line"] == 4), mg.lines[4]["factories"])
    check("rebalance_production 自动削减超储步枪与低优轰炸机、大幅加码火炮",
          inf_after < 16 and tac_after <= 1 and art_after > 4 and rb_dry["reclaimed_factories"] > 0,
          rb_dry["plan"])

    # 8.3 宏 2: upgrade_template_preset (dry_run + 真正执行联动挪厂)
    up_dry = MC.upgrade_template_preset(mg, preset="inf_9_3", dry_run=True, auto_rebalance=True)
    check("upgrade_template_preset(dry_run=true) 自动选中 Infanterie-Division 并估算火炮缺口与挪厂预览",
          up_dry["ok"] and up_dry["template"] == "Infanterie-Division"
          and up_dry["target"]["line"]["artillery_brigade"] == 3
          and "production_rebalance_preview" in up_dry
          and len(mg.edit_calls) == 0,
          up_dry)

    up_run = MC.upgrade_template_preset(mg, preset="inf_9_3", dry_run=False, auto_rebalance=True)
    check("upgrade_template_preset(dry_run=false) 一步完成 9/3 换编 + 自动从步枪线挪厂给火炮",
          up_run["ok"] and mg.tpl_comp["line"]["artillery_brigade"] == 3
          and mg.lines[0]["factories"] < 16 and mg.lines[1]["factories"] > 6
          and sum(x["factories"] for x in mg.lines) == 32,
          (mg.tpl_comp, mg.lines))

    # 8.4 宏 3: plan_front_allocation (111 师撞 401 师 -> suicidal 并拒绝 execute=True)
    fake_forecast = {
        "co_belligerents": ["FRA", "ENG", "POL"],
        "guaranteed_by": ["ENG", "FRA"],
        "total_enemy_divisions_est": 401,
        "diplomatic_blockers": [],
        "diplomatic_warnings": [],
        "co_belligerent_borders": [
            {"tag": "FRA", "border_provinces": 8, "enemy_divisions": 126,
             "is_major": True, "divisions_on_border": 0, "has_front_line": False, "reasons": ["guarantees(BEL)"]},
            {"tag": "POL", "border_provinces": 20, "enemy_divisions": 60,
             "is_major": False, "divisions_on_border": 10, "has_front_line": True, "reasons": ["same_faction"]},
        ],
    }
    with mock.patch.object(DIP, "war_escalation_forecast", return_value=fake_forecast), \
         mock.patch.object(DIP, "divisions_on_border", return_value={"border_provinces": 4, "divisions_on_border": 0}), \
         mock.patch.object(SS, "front_budget", side_effect=lambda _g, e, _t: {
             "capacity": 12.0 if e == "BEL" else (24.0 if e == "FRA" else 36.0),
             "measured_capped_provinces": [],
         }):
        pfa = MC.plan_front_allocation(mg, "BEL", "GER", execute=True)
    check("plan_front_allocation 对 111 师撞 401 师判为 suicidal 并拦截 execute=True",
          (not pfa["ok"]) and pfa["verdict"] == "suicidal"
          and pfa.get("blocked_by") == "suicidal_allocation"
          and pfa["reserve_divisions"] >= 13
          and pfa["fronts"][0]["allocated_divisions"] <= 12,
          pfa)

    # 8.5 TurnEngine DSL 校验 & Compact 仍保持 5 工具
    plan_ok, plan_errs = TE.validate_plan({
        "actions": [
            {"op": "rebalance_production", "args": {"dry_run": True}},
            {"op": "upgrade_template_preset", "args": {"preset": "inf_9_3", "auto_rebalance": True, "dry_run": True}},
            {"op": "plan_front_allocation", "args": {"target": "BEL", "execute": False}},
        ],
        "inspect": [{"topic": "health"}],
    })
    check("turnengine Plan DSL 完整通过三大宏动作与 health inspect 校验",
          plan_ok and not plan_errs, (plan_ok, plan_errs))
    check("Compact 模式依然严格保持 5 工具", len(MCP.COMPACT_TOOL_NAMES) == 5)
    tmap = {t["name"]: t for t in MCP.TOOLS}
    check("MCP Full 模式已注册 4 个老手宏/体检工具",
          {"hoi4_rebalance_production", "hoi4_upgrade_template_preset",
           "hoi4_plan_front_allocation", "hoi4_strategic_health"} <= set(tmap))



class TestRetest7B1B10AndMacros(unittest.TestCase):
    def test_b1_b7_wargoal_direct(self):
        reset_counts()
        globals()["test_b1_b7_wargoal_direct"]()
        _p, _f = counts()
        self.assertEqual(_f, 0, f"test_b1_b7_wargoal_direct failed: {FAILURES}")

    def test_b2_supply_model(self):
        reset_counts()
        globals()["test_b2_supply_model"]()
        _p, _f = counts()
        self.assertEqual(_f, 0, f"test_b2_supply_model failed: {FAILURES}")

    def test_b3_front_changes(self):
        reset_counts()
        globals()["test_b3_front_changes"]()
        _p, _f = counts()
        self.assertEqual(_f, 0, f"test_b3_front_changes failed: {FAILURES}")

    def test_b4_b9_wait_and_peace(self):
        reset_counts()
        globals()["test_b4_b9_wait_and_peace"]()
        _p, _f = counts()
        self.assertEqual(_f, 0, f"test_b4_b9_wait_and_peace failed: {FAILURES}")

    def test_b5_b6_border_and_assigned(self):
        reset_counts()
        globals()["test_b5_b6_border_and_assigned"]()
        _p, _f = counts()
        self.assertEqual(_f, 0, f"test_b5_b6_border_and_assigned failed: {FAILURES}")

    def test_b8_b10_construction_and_reclaim(self):
        reset_counts()
        globals()["test_b8_b10_construction_and_reclaim"]()
        _p, _f = counts()
        self.assertEqual(_f, 0, f"test_b8_b10_construction_and_reclaim failed: {FAILURES}")

    def test_anti_cheese_reddots_and_declare_war(self):
        reset_counts()
        globals()["test_anti_cheese_reddots_and_declare_war"]()
        _p, _f = counts()
        self.assertEqual(_f, 0, f"test_anti_cheese_reddots_and_declare_war failed: {FAILURES}")

    def test_three_macros_and_health_board(self):
        reset_counts()
        globals()["test_three_macros_and_health_board"]()
        _p, _f = counts()
        self.assertEqual(_f, 0, f"test_three_macros_and_health_board failed: {FAILURES}")


if __name__ == "__main__":
    unittest.main()
