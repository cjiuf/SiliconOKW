# -*- coding: utf-8 -*-
"""2026-11-23 复测 7 全缺陷根治（B-1~B-10）、最高四速、反糊弄红点门禁与三大老手宏工具专项回归测试。

运行：python research/windows/test_retest7_and_macros_20261123.py
"""
import json
import os
import struct
import sys
import tempfile
import time
import types
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "calling"))
sys.path.insert(0, os.path.join(ROOT, "mcp"))

import armyops_win as AW
import cmdwin
import diplomacy as DIP
import game as game_mod
import housekeep as HK
import macros as MC
import peacewin as PW
import reddots as RD
import server as MCP
import staff_supply as SS
import turnengine as TE
import turnloop as TL

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] %s" % name)
    else:
        FAIL += 1
        print("  [FAIL] %s%s" % (name, (" -- " + str(detail)) if detail else ""))


# ===================================================================== 1. B-1 & B-7: O(1) CWarGoal
class _MemProc:
    def __init__(self):
        self.pid = 77701
        self.base = 0x7FF600000000
        self.mem = {}

    def put_bytes(self, addr, data):
        for i, b in enumerate(data):
            self.mem[addr + i] = b

    def write(self, addr, data):
        self.put_bytes(addr, data)

    def put_u64(self, addr, val):
        self.put_bytes(addr, struct.pack("<Q", val))

    def put_i32(self, addr, val):
        self.put_bytes(addr, struct.pack("<i", val))

    def read(self, addr, n):
        return bytes(self.mem.get(addr + i, 0) for i in range(n))

    def try_read(self, addr, n):
        return self.read(addr, n)

    def stdstring(self, addr):
        n = self.u64(addr + 0x10)
        cap = self.u64(addr + 0x18)
        if not (0 < n < 256):
            return None
        b = self.read(addr, n) if cap <= 15 else self.read(self.u64(addr), n)
        return b.decode("utf-8", "replace")

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
    cache = os.path.join(ROOT, "calling", "front_snapshot.json")
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
              and any(x[0] == hex(kid_ptr) and x[1] is False for x in state_e1["exec_cleared"]),
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


def main():
    test_b1_b7_wargoal_direct()
    test_b2_supply_model()
    test_b3_front_changes()
    test_b4_b9_wait_and_peace()
    test_b5_b6_border_and_assigned()
    test_b8_b10_construction_and_reclaim()
    test_anti_cheese_reddots_and_declare_war()
    test_three_macros_and_health_board()
    test_retest8_fixes()
    test_retest9_c1_to_c9_fixes()
    test_retest9_d1_to_d4_fixes()
    test_retest9_d5_d6_and_e1_to_e6_fixes()
    test_retest10_all_fixes()
    test_retest11_all_fixes()
    test_security_and_hard_bugs()
    print("\nSummary: %d passed, %d failed" % (PASS, FAIL))
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
