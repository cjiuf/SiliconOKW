"""复测 5（长跑 1936->1939）全部 14+ 项缺陷与专题能力的离线回归测试套件。

覆盖清单：
  1.  P0 §2.1  remove_construction 队列 splice 精确左移（index=mid/last/0/n=1）
  2.  P1 §3.1  housekeep_navy 跳过预备舰队 (mission==8 / is_reserve)，连跑 3 次不新建舰队；
               NAVY_NO_MISSION 同步排除预备舰队
  3.  P1 §3.2  TurnLoop.wait 超时/看门狗取消强制 pause(True)；max_days 自适应步长防超冲；
               mcp/server.py _run_with_timeout 看门狗联动 cancel_event + 紧急停表
  4.  P2 §8.2  CSetProductionLineCommand 真实布局/白名单 + set_production_line_equipment
               在 dry_run=True 也跑真实 CanExecute（杜绝假绿灯）+ 真发换装清去重缓存
  5.  P1 §8.4  equipmeta 解析真实游戏目录 + 复测 5 1939.01.20 产线精确识别 line 2 过时
               + 三大时刻提醒（研究完成瞬间 / 产线分配回执 / overview.alerts 防噪音 4 规则）
  6.  P2 §8.5  SURPLUS / AIR_GAP / FUEL_ZERO 三类策略告警 + housekeep_production 换装幂等与 SURPLUS 腾挪
  7.  P2 §3.3  housekeep 把 research 提到 politics 之前 + politics 单步预算保护
  8.  P2 §3.4  housekeep 顶层拆分 failed_steps / skipped_steps / partial / summary
  9.  P3 §1.1  set_game_speed 4 速 speed_cap_mode='unlimited' + speed_note
  10. P3 §3.5  selectable_focuses 去重（selectable 为对象列表，focuses 为 key 列表）
  11. P3 §3.6  focus_progress / current_focus / turnloop.snapshot / hoi4_focus_current 均带真实进度
  12. P3 §3.7  events_win.select_option 的 auto_resolve_option 取首个 applicable=True（无则 None）
  13. P3 §3.8/3.9 fill_all_research_slots 扣除已研与 slot_done 科技、already_researched、
               杜绝单条 ok=True 但槽仍空
  14. P3 §3.10/3.11 conveyors 读 PDXARRAY_SIZE (+0x0C) 对齐 lines_detail；train_divisions(conveyor=...) 往已有带加线
"""
from __future__ import annotations

import os
import struct
import sys
import threading
import time
import types

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CALLING = os.path.join(ROOT, "calling")
MCP_DIR = os.path.join(ROOT, "mcp")
if CALLING not in sys.path:
    sys.path.insert(0, CALLING)
if MCP_DIR not in sys.path:
    sys.path.insert(0, MCP_DIR)

FAILS = []


def check(name, cond, detail=""):
    tag = "PASS" if cond else "FAIL"
    print("  %-4s  %s%s" % (tag, name, ("  [%s]" % (detail,)) if detail != "" else ""))
    if not cond:
        FAILS.append((name, detail))


# =====================================================================
# 1. P0 §2.1 remove_construction 队列 splice 精确左移
# =====================================================================
def test_p0_remove_construction_splice():
    print("\n[1] P0 §2.1 remove_construction 队列 splice 精确左移")
    import game
    import offsets as O

    class FakeMem:
        def __init__(self, ptrs):
            self.ptrs = list(ptrs)
            self.n = len(ptrs)
            self.buf = bytearray(b"".join(struct.pack("<Q", x) for x in ptrs))
            self.ps = 0x100000
            self.data = 0x200000
            self.c_ptr = 0x300000

        def u64(self, addr):
            if addr == self.c_ptr + game.CO_PRODUCTION:
                return self.ps
            if addr == self.ps + game.PS_CONSTRUCTION + O.PDXARRAY_DATA:
                return self.data
            raise KeyError(hex(addr))

        def i32(self, addr):
            if addr == self.ps + game.PS_CONSTRUCTION + O.PDXARRAY_SIZE:
                return self.n
            raise KeyError(hex(addr))

        def read(self, addr, sz):
            assert addr == self.data and sz == 8 * self.n
            return bytes(self.buf[:sz])

        def write(self, addr, blob):
            if addr == self.data:
                assert len(blob) <= len(self.buf)
                self.buf[:len(blob)] = blob
            elif addr == self.ps + game.PS_CONSTRUCTION + O.PDXARRAY_SIZE:
                self.n = struct.unpack("<i", blob)[0]
            else:
                raise KeyError(hex(addr))

        def live_ptrs(self):
            return list(struct.unpack("<%dQ" % self.n, self.buf[:8 * self.n]))

    mem = FakeMem([0xA0, 0xA1, 0xA2, 0xA3, 0xA4])
    g = object.__new__(game.Game)
    g.p = mem
    g._c = lambda tag=None: types.SimpleNamespace(ptr=mem.c_ptr, tag="GER")
    g._settle = lambda s=0.0: None
    g.construction_queue = lambda tag=None: [
        {"index": i, "building": "b_%x" % p, "state": 50 + i}
        for i, p in enumerate(mem.live_ptrs())
    ]

    # 删中间 index=2 -> 应剩 [A0, A1, A3, A4]，且槽 4 清零
    r1 = g.remove_construction(2)
    check("remove index=2 ok & after=4", r1["ok"] and r1["after"] == 4 and r1["removed"] == "b_a2")
    check("remove index=2 pointers intact", mem.live_ptrs() == [0xA0, 0xA1, 0xA3, 0xA4], mem.live_ptrs())
    check("remove index=2 tail zeroed", struct.unpack_from("<Q", mem.buf, 8 * 4)[0] == 0)

    # 删尾部 index=3 -> 应剩 [A0, A1, A3]（旧 bug 会把整个数组写成全 0 崩溃！）
    r2 = g.remove_construction(3)
    check("remove tail index=3 ok & after=3", r2["ok"] and r2["after"] == 3 and r2["removed"] == "b_a4")
    check("remove tail index=3 front preserved (no wipe!)", mem.live_ptrs() == [0xA0, 0xA1, 0xA3], mem.live_ptrs())

    # 删首部 index=0 -> 应剩 [A1, A3]
    r3 = g.remove_construction(0)
    check("remove head index=0 ok", r3["ok"] and mem.live_ptrs() == [0xA1, 0xA3], mem.live_ptrs())

    # 删到空队列（n=1 -> 0，首槽清零）
    g.remove_construction(1)
    r5 = g.remove_construction(0)
    check("remove last item n=1->0 ok", r5["ok"] and mem.n == 0 and struct.unpack_from("<Q", mem.buf, 0)[0] == 0)


# =====================================================================
# 2. P1 §3.1 housekeep_navy 跳过预备舰队，连跑 3 次不新建舰队
# =====================================================================
def test_p1_housekeep_navy_reserve_guard():
    print("\n[2] P1 §3.1 housekeep_navy 预备舰队保护与幂等验证")
    import game
    import navalops_win as NW

    check("MISSION_RESERVE == 8 & MISSION_NAMES[8] == 'reserve'",
          NW.MISSION_RESERVE == 8 and NW.MISSION_NAMES[8] == "reserve")

    # 模拟 2 支正规舰队（1 个空闲 TF 无海域）+ 1 支预备舰队（mission=8, regions=0）
    state = {
        "fleets": [
            {
                "fleet_index": 0, "fleet_name": "Kriegsmarine", "fleet_ptr": "0x1000",
                "handle": [61, 1], "packed_handle": hex((1 << 32) | 61),
                "regions": 1, "is_reserve": False,
                "task_forces": [
                    {"index": 0, "ptr": "0x2000", "handle": [51, 10],
                     "packed_handle": hex((10 << 32) | 51), "ships": 12,
                     "mission": 1, "mission_name": "patrol", "is_reserve": False, "regions": 1},
                ],
            },
            {
                "fleet_index": 1, "fleet_name": "德意志国第3舰队", "fleet_ptr": "0x1010",
                "handle": [61, 3231], "packed_handle": hex((3231 << 32) | 61),
                "regions": 0, "is_reserve": False,
                "task_forces": [
                    {"index": 0, "ptr": "0x2010", "handle": [61, 3230],
                     "packed_handle": hex((3230 << 32) | 61), "ships": 3,
                     "mission": 0, "mission_name": "none", "is_reserve": False, "regions": 0},
                ],
            },
            {
                "fleet_index": 2, "fleet_name": "德意志国第1舰队", "fleet_ptr": "0x1020",
                "handle": [61, 9000], "packed_handle": hex((9000 << 32) | 61),
                "regions": 0, "is_reserve": True,
                "task_forces": [
                    {"index": 0, "ptr": "0x2020", "handle": [61, 9001],
                     "packed_handle": hex((9001 << 32) | 61), "ships": 4,
                     "mission": 8, "mission_name": "reserve", "is_reserve": True, "regions": 0},
                ],
            },
        ],
        "add_region_calls": [],
        "set_mission_calls": [],
    }

    orig_fleets = NW.naval_fleets
    orig_add_reg = NW.add_region
    orig_set_mis = NW.set_mission
    orig_valid_reg = NW.valid_regions
    try:
        import copy
        NW.naval_fleets = lambda g, tag=None: copy.deepcopy(state["fleets"])
        NW.valid_regions = lambda g: [9]

        def fake_add_region(g, fh, rid, tag=None):
            state["add_region_calls"].append((fh, rid))
            for f in state["fleets"]:
                if int(f["packed_handle"], 16) == int(fh):
                    if f["is_reserve"]:
                        raise AssertionError("MUST NOT touch reserve fleet!")
                    f["regions"] = 1
            return {"ok": True, "regions_after": 1}

        def fake_set_mission(g, tfh, m, tag=None):
            state["set_mission_calls"].append((tfh, m))
            for f in state["fleets"]:
                for tf in f["task_forces"]:
                    if int(tf["packed_handle"], 16) == int(tfh):
                        if tf["is_reserve"]:
                            raise AssertionError("MUST NOT touch reserve TF!")
                        tf["mission"] = m
                        tf["mission_name"] = NW.MISSION_NAMES.get(m)
            return {"ok": True, "mission_after": m}

        NW.add_region = fake_add_region
        NW.set_mission = fake_set_mission

        g = object.__new__(game.Game)
        r1 = g.housekeep_navy()
        r2 = g.housekeep_navy()
        r3 = g.housekeep_navy()

        check("run #1 ok=True, assigned=1, still_idle=[]",
              r1["ok"] and r1["assigned"] == 1 and r1["still_idle"] == [], r1)
        check("run #1 skipped reserve fleet without touching it",
              len(r1["reserve_fleets_skipped"]) == 1
              and r1["reserve_fleets_skipped"][0]["fleet"] == "德意志国第1舰队")
        check("run #2 & #3 ok=True, 0 extra calls, fleet count unchanged (3)",
              r2["ok"] and r3["ok"] and r2["posted"] == 0 and r3["posted"] == 0
              and len(state["fleets"]) == 3
              and len(state["add_region_calls"]) == 1
              and len(state["set_mission_calls"]) == 1)

        # 验证 _light_alerts 的 NAVY_NO_MISSION 也排除预备舰队
        state["fleets"][1]["task_forces"][0]["mission"] = 1
        g._oi = object()
        g._air_snapshot = lambda: []
        g._navy_snapshot = lambda: copy.deepcopy(state["fleets"])
        g._active_variant_names = lambda tag=None: []
        g.research_slots = lambda tag=None: []
        g.construction_queue = lambda tag=None: [{"factories": 15}] * 6
        g.unassigned_divisions = lambda tag=None: []
        g.wars = lambda tag=None: {"enemies": []}
        g.equipment_stockpile = lambda tag=None: {"totals_by_equipment": {}}
        g.divisions = lambda tag=None, limit=1: {"count": 30}
        g._c = lambda tag=None: types.SimpleNamespace(tag="GER")
        g.p = types.SimpleNamespace(pid=1)
        alerts = g._light_alerts("GER", {"political_power": 50, "civilian_factories": 20,
                                         "military_factories": 20, "fuel": 50000}, [], {"key": "x"})
        check("NAVY_NO_MISSION excludes reserve fleet",
              not any(a.startswith("NAVY_NO_MISSION") for a in alerts), alerts)
    finally:
        NW.naval_fleets = orig_fleets
        NW.add_region = orig_add_reg
        NW.set_mission = orig_set_mis
        NW.valid_regions = orig_valid_reg


# =====================================================================
# 3. P1 §3.2 wait_for 超时强制暂停 + stop_event 协同取消 + max_days 防超冲
# =====================================================================
def test_p1_wait_for_pause_and_overshoot():
    print("\n[3] P1 §3.2 wait_for 超时停表、协同取消与 max_days 防超冲")
    import turnloop
    import session

    class FakeClockGame:
        def __init__(self, dps=4.0):
            self.t0 = time.monotonic()
            self.dps = dps
            self.paused_flag = False
            self.pause_calls = 0
            self.start_h = 16986112
            self.frozen_h = self.start_h

        def _now_h(self):
            if self.paused_flag:
                return self.frozen_h
            el = time.monotonic() - self.t0
            self.frozen_h = self.start_h + int(el * self.dps * 24)
            return self.frozen_h

        def date(self):
            h = self._now_h()
            return {"raw": h, "text": "raw:%d" % h}

        def player_tag(self):
            return "GER"

        def game_speed(self):
            return {"speed": 4, "seconds_per_hour": 0.0}

        def current_focus(self):
            return {"key": "GER_rhineland"}

        def focus_progress(self):
            return {"progress": 35.0, "days_left": 35.0, "percent": 50.0}

    orig_pause = session.pause
    orig_is_paused = session.is_paused
    try:
        def fake_pause(g, want=True):
            g.pause_calls += 1
            g.paused_flag = bool(want)
            return True, g.paused_flag

        session.pause = fake_pause
        session.is_paused = lambda g: g.paused_flag

        # 3a. 超时（pause_on_wake=False 时仍必须强制暂停！）
        fg1 = FakeClockGame(dps=4.0)
        tl1 = turnloop.TurnLoop(fg1, poll=0.5)
        r_to = tl1.wait(max_days=200, signals=("focus",), timeout_seconds=0.35, pause_on_wake=False)
        check("timeout forces pause even when pause_on_wake=False",
              r_to.get("timed_out") is True and r_to.get("paused") is True
              and fg1.paused_flag is True and fg1.pause_calls >= 1, r_to)
        check("snapshot focus_progress is non-zero (35.0)",
              r_to["state"].get("focus_progress") == 35.0
              and r_to["state"].get("focus_days_left") == 35.0, r_to["state"])

        # 3b. 看门狗 stop_event 协同取消（<0.4s 立即退出并暂停）
        fg2 = FakeClockGame(dps=4.0)
        tl2 = turnloop.TurnLoop(fg2, poll=2.0)
        ev = threading.Event()
        threading.Timer(0.15, ev.set).start()
        t0 = time.monotonic()
        r_can = tl2.wait(max_days=200, signals=("focus",), timeout_seconds=30,
                         pause_on_wake=False, stop_event=ev)
        dt = time.monotonic() - t0
        check("stop_event cancels wait_for promptly (<0.8s) and pauses game",
              dt < 0.8 and r_can.get("paused") is True and fg2.paused_flag is True,
              "dt=%.2fs paused=%s" % (dt, r_can.get("paused")))

        # 3c. max_days 防超冲（请求 4 天 @ 4 天/秒，动态步长应把超冲控制在 <= 1.5 天）
        fg3 = FakeClockGame(dps=4.0)
        tl3 = turnloop.TurnLoop(fg3, poll=4.0)
        r_days = tl3.wait(max_days=4, signals=("focus",), timeout_seconds=10, pause_on_wake=True)
        check("max_days=4 @ 4 dps overshoot <= 1.5d (adaptive poll)",
              4.0 <= r_days["waited_game_days"] <= 5.5,
              "waited=%.2f days" % r_days["waited_game_days"])
    finally:
        session.pause = orig_pause
        session.is_paused = orig_is_paused


# =====================================================================
# 4 & 5 & 6. §8 专题：equipmeta + CSetProductionLineCommand + 三告警 + housekeep 换装/过剩
# =====================================================================
def test_s8_obsolete_retool_and_strategy_alerts():
    print("\n[4-6] §8 装备代差、CSetProductionLineCommand 假绿灯根治与三类告警")
    import cmdwin
    import equipmeta
    import game

    # 4a. CSetProductionLineCommand 在 cmdwin 注册正确
    spec = cmdwin.spec_of("CSetProductionLineCommand")
    check("CSetProductionLineCommand in WIN_SAFE & ARGSPEC & ARG_KIND",
          "CSetProductionLineCommand" in cmdwin.WIN_SAFE
          and "CSetProductionLineCommand" in cmdwin.ARGSPEC
          and "CSetProductionLineCommand" in cmdwin.ARG_KIND)
    check("CSetProductionLineCommand layout matches PE 1.19.5.0",
          spec["vt"] == 0x2A0FF88 and spec["ctor"] == 0x11D1550
          and spec["size"] == 0x38 and cmdwin.CANEXEC_SET_PROD_LINE == 0x11F2C30, spec)

    # 4b. set_production_line_equipment dry_run=True 必须跑真实 CanExecute（不再假绿灯！）
    g = object.__new__(game.Game)
    cur_eq_id = [101]
    can_ret = [0]
    freed = []

    class FakeP:
        pid = 99999
        base = 0x140000000

        def u64(self, a):
            if a == 0x100000 + game.CO_PRODUCTION:
                return 0x200000
            if a == 0x300000 + game.ML_VARIANT:
                return 0x400000 if cur_eq_id[0] == 101 else 0x400001
            if a == 0x200000 + game.PS_MIL_LINES:
                return 0x500000
            if a == 0x500000:
                return 0x300000
            return 0

        def i32(self, a):
            if a == 0x200000 + game.PS_MIL_LINES + 0x0C:
                return 1
            if a == 0x400000 + game.EV_TYPE_ID:
                return 101
            if a == 0x400001 + game.EV_TYPE_ID:
                return 102
            return 0

        def read(self, a, n):
            if a == 0x500000 and n == 8:
                return struct.pack("<Q", 0x300000)
            return b"\0" * n

    g.p = FakeP()
    g._c = lambda tag=None: types.SimpleNamespace(ptr=0x100000, tag="GER", tag_id=1)
    g._load_equipment = lambda: None
    g._eq_by_id = {101: "artillery_equipment_1", 102: "artillery_equipment_2"}
    g._variant_for = lambda eq, tag=None: (0x400001, {"type": 70, "id": 5291}) if eq == "artillery_equipment_2" else (None, None)
    g.handle_of = lambda ptr: {"type": 56, "id": 3}
    g._settle = lambda s=0.0: None
    g.call = lambda addr, args, **kw: can_ret[0] if addr == FakeP.base + cmdwin.CANEXEC_SET_PROD_LINE else 1

    orig_build = cmdwin.build_command
    orig_free = cmdwin.free
    orig_post = cmdwin.post_drained
    try:
        cmdwin.build_command = lambda gm, cls, kw: 0xDEAD00
        cmdwin.free = lambda gm, ptr: freed.append(ptr)

        def fake_post(gm, cmd, seconds=2.0):
            cur_eq_id[0] = 102
            return {"posted": 1, "drained": True}

        cmdwin.post_drained = fake_post

        # CanExecute=0 -> dry_run=True 必须返回 ok=False（杜绝假绿灯！）
        can_ret[0] = 0
        r_dry_refuse = g.set_production_line_equipment(0, "artillery_equipment_2", dry_run=True)
        check("dry_run=True honors CanExecute=False (no false green!)",
              r_dry_refuse["ok"] is False and r_dry_refuse["can_execute"] is False
              and r_dry_refuse.get("blocked_by") == "can_execute_refused", r_dry_refuse)

        # CanExecute=1 -> dry_run=True 返回 ok=True 且不改产线
        can_ret[0] = 1
        r_dry_ok = g.set_production_line_equipment(0, "artillery_equipment_2", dry_run=True)
        check("dry_run=True with CanExecute=True returns ok=True without mutating",
              r_dry_ok["ok"] is True and r_dry_ok["can_execute"] is True
              and r_dry_ok["after"] == "artillery_equipment_1", r_dry_ok)

        # CanExecute=1 -> dry_run=False 真发换装成功
        r_real = g.set_production_line_equipment(0, "artillery_equipment_2", dry_run=False)
        check("dry_run=False switches line to artillery_equipment_2",
              r_real["ok"] is True and r_real["after"] == "artillery_equipment_2", r_real)
        check("dry_run freed temporary command objects", len(freed) >= 2)
    finally:
        cmdwin.build_command = orig_build
        cmdwin.free = orig_free
        cmdwin.post_drained = orig_post

    # 5. equipmeta 静态解析 + 三时刻 + 防噪音 4 规则
    data = equipmeta.load()
    check("equipmeta loaded > 200 equipments & > 100 tech->eq",
          len(data["equipments"]) > 200 and len(data["tech_to_eq"]) > 100,
          "%d eq, %d tech" % (len(data["equipments"]), len(data["tech_to_eq"])))
    check("artillery1 unlocks artillery_equipment_2",
          "artillery_equipment_2" in equipmeta.tech_unlocks("artillery1", data))

    lines_1939 = [
        {"index": 0, "equipment": "infantry_equipment_1", "factories": 10, "output_per_day": 40.95,
         "output_units_per_day": 81.9, "unit_cost": 0.5, "class": "CMilitaryProductionLine"},
        {"index": 1, "equipment": "support_equipment_1", "factories": 7, "output_per_day": 28.665,
         "output_units_per_day": 7.16, "unit_cost": 4.0, "class": "CMilitaryProductionLine"},
        {"index": 2, "equipment": "artillery_equipment_1", "factories": 7, "output_per_day": 28.665,
         "output_units_per_day": 8.19, "unit_cost": 3.5, "class": "CMilitaryProductionLine"},
        {"index": 3, "equipment": "light_tank_equipment_2", "factories": 2, "output_per_day": 8.19,
         "output_units_per_day": 0.91, "unit_cost": 9.0, "class": "CMilitaryProductionLine"},
        {"index": 5, "equipment": "fighter_equipment_1", "factories": 8, "output_per_day": 8.19,
         "output_units_per_day": 0.3412, "unit_cost": 24.0, "class": "CMilitaryProductionLine"},
    ]
    avail_1939 = [
        {"equipment": "infantry_equipment_1"}, {"equipment": "support_equipment_1"},
        {"equipment": "artillery_equipment_2"}, {"equipment": "light_tank_equipment_2"},
        {"equipment": "fighter_equipment_1"},
    ]

    # 时刻 ①：artillery1 完成瞬间
    t1_hits = equipmeta.check_tech_unlocks_obsolete(["artillery1"], lines_1939, avail_1939)
    check("moment #1 tech unlock detects ONLY line 2 artillery_equipment_2",
          len(t1_hits) == 1 and t1_hits[0]["line"] == 2
          and t1_hits[0]["upgrade_to"] == "artillery_equipment_2", t1_hits)

    # 时刻 ③：overview 防噪音规则（首次报 1 条、二次去重为 0、换装后清位）
    equipmeta.clear_obsolete_seen(pid=7777, tag="GER")
    al1, all1, sup1 = equipmeta.collect_obsolete_alerts(lines_1939, avail_1939, pid=7777, tag="GER")
    check("overview #1 emits exact 1 OBSOLETE_LINE(line 2) with 3 elements",
          len(al1) == 1 and "OBSOLETE_LINE(line 2)" in al1[0]
          and "artillery_equipment_2" in al1[0]
          and "回本" in al1[0]
          and "hoi4_set_production_line_equipment(line=2" in al1[0], al1)
    al2, _, sup2 = equipmeta.collect_obsolete_alerts(lines_1939, avail_1939, pid=7777, tag="GER")
    check("overview #2 dedupes same obsolete line (0 alerts, 1 suppressed)",
          len(al2) == 0 and sup2 == 1, (al2, sup2))
    # 换装后告警清位消失
    lines_upgraded = [dict(x) for x in lines_1939]
    lines_upgraded[2]["equipment"] = "artillery_equipment_2"
    al3, all3, _ = equipmeta.collect_obsolete_alerts(lines_upgraded, avail_1939, pid=7777, tag="GER")
    check("after upgrade, OBSOLETE_LINE clears completely",
          len(al3) == 0 and len(all3) == 0)

    # >3 条过时线自动截断 +N more
    equipmeta.clear_obsolete_seen(pid=7778, tag="GER")
    many_lines = [
        {"index": i, "equipment": "artillery_equipment_1", "factories": 5, "output_per_day": 20.0,
         "unit_cost": 3.5, "class": "CMilitaryProductionLine"}
        for i in range(5)
    ]
    al_many, _, _ = equipmeta.collect_obsolete_alerts(many_lines, avail_1939, pid=7778, tag="GER")
    check("max 3 OBSOLETE_LINE alerts + '+2 more' summary",
          len(al_many) == 4 and "+2 more" in al_many[-1], al_many)

    # 6. 三类告警 SURPLUS / AIR_GAP / FUEL_ZERO
    surplus = equipmeta.compute_surplus_lines(
        lines_1939, {"infantry_equipment_1": 53747.0}, total_mil=42, div_count=117
    )
    check("SURPLUS detects line 0 rifles (53747 stock, 10/42 mil)",
          len(surplus) == 1 and surplus[0]["line"] == 0 and surplus[0]["surplus_factories"] >= 3, surplus)

    wings = [
        {"type": "fighter", "planes": 478, "mission": 1},
        {"type": "transport_plane", "planes": 80, "mission": 0},
    ]
    air_gaps = equipmeta.compute_air_gap_alerts(wings, lines_1939)
    check("AIR_GAP detects fighter low monthly replacement (<5%)",
          len(air_gaps) == 1 and "AIR_GAP(fighter)" in air_gaps[0], air_gaps)

    fz = equipmeta.compute_fuel_zero_alert(0.0, wings, [])
    check("FUEL_ZERO triggers when fuel=0 and active wings > 0",
          fz is not None and fz.startswith("FUEL_ZERO:"), fz)
    check("FUEL_ZERO silent when fuel is healthy",
          equipmeta.compute_fuel_zero_alert(50000.0, wings, []) is None)


# =====================================================================
# 7 & 8. P2 §3.3 / §3.4 housekeep 顺序（research 在 politics 前）与顶层状态拆分
# =====================================================================
def test_p2_housekeep_order_and_top_status():
    print("\n[7-8] P2 §3.3/§3.4 housekeep 执行顺序、politics 预算与顶层状态拆分")
    import game

    order = []
    g = object.__new__(game.Game)
    g.housekeep_construction = lambda tag, tc: (order.append("construction"), {"ok": True})[1]
    g.housekeep_production = lambda tag: (order.append("production"), {"ok": True})[1]
    g.housekeep_doctrines = lambda tag: (order.append("doctrines"), {"ok": True})[1]
    g.housekeep_research = lambda tag, techs: (order.append("research"), {"ok": True})[1]
    g.housekeep_politics = lambda tag, budget_s=12.0: (order.append("politics"), {"ok": True, "budget_s": budget_s})[1]
    g.housekeep_recruitment = lambda tag: (order.append("recruitment"), {"ok": True})[1]
    g.housekeep_divisions = lambda tag: (order.append("divisions"), {"ok": True})[1]
    g.housekeep_air = lambda tag: (order.append("air"), {"ok": True})[1]
    g.housekeep_navy = lambda tag: (order.append("navy"), {"ok": False, "error": "mock navy"})[1]

    res = g.housekeep()
    check("research runs BEFORE politics",
          order.index("research") < order.index("politics"), order)
    check("top-level splits failed_steps / partial / summary honestly",
          res["ok"] is False and res["partial"] is True
          and res["failed_steps"] == ["navy"]
          and len(res["succeeded_steps"]) == 8
          and "8/9 steps ok; failed: navy" in res["summary"], res)


# =====================================================================
# 9-14. P3 全套小口径回归
# =====================================================================
def test_p3_small_fixes():
    print("\n[9-14] P3 全部小口径修复验证")
    import events_win as EW
    import game
    import offsets as O

    # 12. events_win auto_resolve_option 取首个 applicable=True
    orig_pending = EW.pending
    orig_app = EW.applicable_options
    orig_make = EW.make_select_option
    orig_can = EW.can_execute
    orig_info = EW.info
    try:
        EW.pending = lambda g: [{"slot": 0, "event_id": "bftb_news.201", "event_ptr": "0x1000"}]
        EW.applicable_options = lambda eid, tag, lang="english": [
            {"index": 1, "applicable": False, "trigger": "tag = SOV"},
            {"index": 2, "applicable": False, "trigger": "OR = { ENG TUR }"},
            {"index": 3, "applicable": True, "trigger": "OR = { GER ITA }"},
        ]
        EW.info = lambda eid, lang="english": {}
        EW.make_select_option = lambda g, s, p, i, n: 0x2000
        EW.can_execute = lambda g, c: True
        r_ev = EW.select_option(None, tag="GER", write=False)
        check("auto_resolve_option picks first applicable (3, not hardcoded 1)",
              r_ev["auto_resolve_option"] == 3 and r_ev["recommended"] == 3, r_ev)

        EW.applicable_options = lambda eid, tag, lang="english": [
            {"index": 1, "applicable": False, "trigger": "tag = SOV"},
        ]
        r_ev_none = EW.select_option(None, tag="GER", write=False)
        check("auto_resolve_option is None when no option applicable",
              r_ev_none["auto_resolve_option"] is None, r_ev_none)
    finally:
        EW.pending = orig_pending
        EW.applicable_options = orig_app
        EW.make_select_option = orig_make
        EW.can_execute = orig_can
        EW.info = orig_info

    # 13. fill_all_research_slots 扣除已完成与 slot_done 科技 + 拒绝伪 ok
    g = object.__new__(game.Game)
    g.p = types.SimpleNamespace(pid=None)
    g._load_tech_db = lambda wait_s=0.0: None
    g._tech_by_name = {"oil_processing": 0x1, "excavation2": 0x2, "rubber_processing": 0x3}
    g._check_foreground_guard = lambda: None
    g.researched = lambda tag=None: ["oil_processing", "rubber_processing"]
    slots_state = [
        {"slot": 0, "researching": "oil_processing", "slot_busy": True, "slot_done": True, "tech_ptr": "0x1"},
    ]
    g.research_slots = lambda tag=None: [dict(x) for x in slots_state]
    g.available_research = lambda tag=None: ["oil_processing", "rubber_processing", "excavation2"]
    started = []

    def fake_start(tech, slot=None, tag=None):
        started.append(tech)
        slots_state[0] = {"slot": 0, "researching": tech, "slot_busy": True, "slot_done": False, "tech_ptr": "0x2"}
        return {"ok": True, "slot": slot, "researching": tech, "slot_busy": True, "slot_done": False}

    g.start_research = fake_start
    rf = g.fill_all_research_slots(preferred_techs=["oil_processing", "rubber_processing", "excavation2"])
    check("fill_research filters out researched & slot_done techs (never re-picks oil_processing)",
          started == ["excavation2"] and rf["ok"] is True, (started, rf))

    # start_research 直接拦截已研科技返回 already_researched
    g2 = object.__new__(game.Game)
    g2._load_tech_db = lambda wait_s=0.0: None
    g2._tech_by_name = {"radio": 0x10}
    g2._c = lambda tag=None: types.SimpleNamespace(tag="GER", tag_id=1)
    g2.researched = lambda tag=None: ["radio"]
    g2.research_slots = lambda tag=None: [{"slot": 0, "slot_busy": False, "slot_done": False}]
    rr = g2.start_research("radio", slot=0)
    check("start_research returns blocked_by='already_researched' for done tech",
          rr["ok"] is False and rr.get("blocked_by") == "already_researched", rr)

    # 14. conveyors 读 PDXARRAY_SIZE (+0x0C) 而非 cap (+0x08)
    import inspect
    src_game = (
        inspect.getsource(game.Game.conveyors)
        + inspect.getsource(game.Game.selectable_focuses)
    )
    check("conveyors reads PDXARRAY_SIZE (+0x0C) for lines",
          "_arr_size = p.i32(conv + arr_off + O.PDXARRAY_SIZE)" in src_game)
    check("selectable_focuses deduplicates focuses to key list",
          '"focuses": [r["id"] for r in rows[:60]]' in src_game)

    # 9. cmdwin.set_game_speed speed_cap_mode & speed_note
    src_cmd = open(os.path.join(CALLING, "cmdwin.py"), encoding="utf-8").read()
    check("cmdwin.set_game_speed reports speed_cap_mode & speed_note",
          '"speed_cap_mode":' in src_cmd and 'out["speed_note"] =' in src_cmd)


# =====================================================================
# 15-17. §9.3 / §9.5 三项实测残留的精准回归（queued_mission / SURPLUS 不回填 / 满州不重试）
# =====================================================================
def test_s9_residuals():
    print("\n[15-17] §9.3/§9.5 三项实测残留回归（海军 queued_mission / SURPLUS 真实挪厂 / 满州拉黑）")
    import copy
    import game
    import navalops_win as NW

    # 15 (#18): activity=1 且 queued_mission=1 的作战 TF 视为已受命，不再报 still_idle / NAVY_NO_MISSION
    fleets_q = [
        {
            "fleet_index": 0, "fleet_name": "德意志国第2舰队", "fleet_ptr": "0x1000",
            "handle": [61, 1634], "packed_handle": hex((1634 << 32) | 61),
            "regions": 1, "is_reserve": False,
            "task_forces": [
                {"index": 0, "ptr": "0x2000", "handle": [61, 1633],
                 "packed_handle": hex((1633 << 32) | 61), "ships": 16,
                 "mission": 0, "mission_name": "none",
                 "activity": 1, "queued_mission": 1, "queued_mission_name": "patrol",
                 "effective_mission": 1, "is_reserve": False, "regions": 1},
            ],
        }
    ]
    orig_fl = NW.naval_fleets
    orig_sm = NW.set_mission
    try:
        set_calls = []
        NW.naval_fleets = lambda gm, tag=None: copy.deepcopy(fleets_q)
        NW.set_mission = lambda gm, h, m, tag=None: (set_calls.append((h, m)), {"ok": True})[1]
        g = object.__new__(game.Game)
        rn = g.housekeep_navy()
        check("#18 queued_mission=1 (activity=1) TF is treated as armed (ok=True, 0 posts, still_idle=[])",
              rn["ok"] is True and rn["posted"] == 0 and rn["still_idle"] == [] and not set_calls, rn)

        g._oi = object()
        g._air_snapshot = lambda: []
        g._navy_snapshot = lambda: copy.deepcopy(fleets_q)
        g._active_variant_names = lambda tag=None: []
        g.research_slots = lambda tag=None: []
        g.construction_queue = lambda tag=None: [{"factories": 15}] * 6
        g.unassigned_divisions = lambda tag=None: []
        g.wars = lambda tag=None: {"enemies": []}
        g.equipment_stockpile = lambda tag=None: {"totals_by_equipment": {}}
        g.divisions = lambda tag=None, limit=1: {"count": 30}
        g._c = lambda tag=None: types.SimpleNamespace(tag="GER")
        g.p = types.SimpleNamespace(pid=2)
        al = g._light_alerts("GER", {"political_power": 50, "civilian_factories": 20,
                                     "military_factories": 20, "fuel": 50000}, [], {"key": "x"})
        check("#18 NAVY_NO_MISSION clears when TF has queued_mission=1",
              not any(a.startswith("NAVY_NO_MISSION") for a in al), al)
    finally:
        NW.naval_fleets = orig_fl
        NW.set_mission = orig_sm

    # 16 (#19): _stockpile_counts 累加不覆盖 + SURPLUS 裁出的 4 厂流向缺口线，绝不回填 line 0
    g2 = object.__new__(game.Game)
    g2.equipment_stockpile = lambda tag=None: {
        "stockpile": [
            {"equipment": "infantry_equipment_1", "count": 51514.0},
            {"equipment": "infantry_equipment_1", "count": 2233.0},
            {"equipment": "artillery_equipment_1", "count": 4443.0},
            {"equipment": "support_equipment_1", "count": 3403.0},
        ],
        "totals_by_equipment": {
            "infantry_equipment_1": 53747.0,
            "artillery_equipment_1": 4443.0,
            "support_equipment_1": 3403.0,
        },
        "available_variants": [],
    }
    sc = g2._stockpile_counts()
    check("#19 _stockpile_counts sums multi-variant rifles (53747, not overwritten 2233)",
          sc.get("infantry_equipment_1") == 53747.0, sc)

    # 模拟完整 housekeep_production：line 0 (10厂步枪) 应裁到 6 厂，4 厂全部给空军/装甲，不回填 line 0！
    live_lines = [
        {"index": 0, "equipment": "infantry_equipment_1", "factories": 10, "class": "CMilitaryProductionLine"},
        {"index": 1, "equipment": "support_equipment_1", "factories": 7, "class": "CMilitaryProductionLine"},
        {"index": 2, "equipment": "artillery_equipment_2", "factories": 7, "class": "CMilitaryProductionLine"},
        {"index": 3, "equipment": "light_tank_equipment_2", "factories": 4, "class": "CMilitaryProductionLine"},
        {"index": 4, "equipment": "fighter_equipment_1", "factories": 8, "class": "CMilitaryProductionLine"},
        {"index": 5, "equipment": "CAS_equipment_1", "factories": 5, "class": "CMilitaryProductionLine"},
        {"index": 6, "equipment": "tac_bomber_equipment_1", "factories": 1, "class": "CMilitaryProductionLine"},
    ]
    g2._upgrade_production_lines = lambda tag=None: {"upgraded": [], "skipped_slow_payback": [], "errors": []}
    g2._cap_deprioritized_lines = lambda tag, rules: []
    g2.production_lines = lambda tag=None, include_obsolete=True: copy.deepcopy(live_lines)
    g2.player = lambda: types.SimpleNamespace(summary=lambda: {"military_factories": 42})
    g2._c = lambda tag=None: types.SimpleNamespace(tag="GER")
    g2.divisions = lambda tag=None, limit=1: {"count": 117}

    def fake_set_amt(idx, amt, tag=None):
        before = live_lines[idx]["factories"]
        live_lines[idx]["factories"] = amt
        return {"ok": True, "before": before, "after": amt}

    g2.set_production_amount = fake_set_amt
    hp = g2.housekeep_production()
    line0_final = live_lines[0]["factories"]
    reback_to_0 = any(ch["line"] == 0 for ch in hp.get("changes") or [])
    tac_added = any(ch["line"] == 6 for ch in hp.get("changes") or [])
    check("#19 SURPLUS trimmed line 0 stays at 6 (never refilled back to 10!)",
          line0_final == 6 and not reback_to_0 and not tac_added
          and sum(l["factories"] for l in live_lines) == 42,
          {"line0": line0_final, "trimmed": hp.get("surplus_trimmed"), "changes": hp.get("changes")})

    # 17 (#20): housekeep_construction 预过滤满上限州 + 拒绝州绝不跨 pass 重复尝试
    g3 = object.__new__(game.Game)
    g3.construction_queue = lambda tag=None: [
        {"state": 54, "building": "synthetic_refinery"},
    ]
    g3._pick_building = lambda tag=None: "synthetic_refinery"
    g3._state_priority = lambda tag=None: [54, 59, 60]
    g3.state_detail = lambda sid, tag=None: {
        54: {"slots_free": 4, "buildings": {"synthetic_refinery": 2}},  # 2+1=3 已达上限，应直接跳过 0 次尝试
        59: {"slots_free": 3, "buildings": {"synthetic_refinery": 1}},  # 模拟引擎拒绝，只尝试 1 次不跨 pass 重试
        60: {"slots_free": 5, "buildings": {"synthetic_refinery": 0}},  # 成功
    }[sid]
    attempts_by_state = {}

    def fake_add_con(sid, bldg):
        attempts_by_state[sid] = attempts_by_state.get(sid, 0) + 1
        if sid == 59:
            return {"ok": False, "error": "CanExecute refused"}
        return {"ok": True}

    g3.add_construction = fake_add_con
    hc = g3.housekeep_construction(target=4)
    check("#20 construction skips capped state 54 (0 tries) and never retries rejected state 59 (>1 try)",
          attempts_by_state.get(54, 0) == 0 and attempts_by_state.get(59, 0) == 1
          and attempts_by_state.get(60, 0) == 3,
          {"attempts": attempts_by_state, "skipped": hc.get("skipped_full_states")})


def main():
    test_p0_remove_construction_splice()
    test_p1_housekeep_navy_reserve_guard()
    test_p1_wait_for_pause_and_overshoot()
    test_s8_obsolete_retool_and_strategy_alerts()
    test_p2_housekeep_order_and_top_status()
    test_p3_small_fixes()
    test_s9_residuals()
    print("\nSummary:", "ALL PASS" if not FAILS else "%d FAILURES" % len(FAILS))
    if FAILS:
        for nm, dt in FAILS:
            print("  FAILED:", nm, dt)
        sys.exit(1)


if __name__ == "__main__":
    main()
