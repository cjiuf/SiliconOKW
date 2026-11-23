# -*- coding: utf-8 -*-
"""2026-11-22 速度 / 科研前台守卫 / 和平会谈选项 修复与需求离线回归测试。

运行：python research/windows/test_speed_research_peace_20261122.py
"""
import os
import struct
import sys
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "calling"))
sys.path.insert(0, os.path.join(ROOT, "mcp"))

import offsets_win as OW
import cmdwin
import game as game_mod
import peacewin as PW
import turnengine as TE

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


# ==================================================================== 1. 速度
class _FakeSpeedProc:
    def __init__(self, initial_speed=2, paused=True, reject_write=False):
        self.pid = 36540
        self.base = 0x7FF6397C0000
        self.gs = 0x1B1E6DEF850
        self.idler = 0x1B210A8E310
        self.speed_arr = 0x1B09BA56B30
        self.speed = initial_speed
        self.paused = 1 if paused else 0
        self.reject_write = reject_write
        self.writes = []

    def u64(self, addr):
        if addr == self.base + OW.G_GAMESTATE:
            return self.gs
        if addr == self.base + OW.G_IDLER:
            return self.idler
        if addr == self.base + OW.G_GAME_SPEED_SECONDS:
            return self.speed_arr
        return 0

    def i32(self, addr):
        if addr == self.gs + OW.GS_GAME_SPEED:
            return self.speed
        if addr == self.base + OW.G_GAME_SPEED_SECONDS + 0x0C:
            return 5
        return 0

    def u8(self, addr):
        if addr == self.idler + 0x6C1:
            return self.paused
        return 0

    def read(self, addr, n):
        if addr == self.speed_arr and n == 20:
            return struct.pack("<5f", 2.0, 0.5, 0.2, 0.1, 0.0)
        return b"\0" * n

    def write(self, addr, data):
        self.writes.append((addr, data))
        if addr == self.gs + OW.GS_GAME_SPEED and not self.reject_write:
            self.speed = struct.unpack("<i", data)[0]
        elif addr == self.idler + 0x6C1:
            self.paused = data[0]


class _FakeSpeedGame:
    def __init__(self, initial_speed=2, paused=True, reject_write=False, ticks_per_call=0):
        self.p = _FakeSpeedProc(initial_speed=initial_speed, paused=paused, reject_write=reject_write)
        self._raw = 16959889
        self._ticks_per_call = ticks_per_call

    def date_raw(self):
        v = self._raw
        self._raw += self._ticks_per_call
        return v


def test_speed_fixes():
    print("\n== 1. 游戏速度修复 (gs+0x4BC & CSetGameSpeedCommand) ==")
    check("offsets_win.GS_GAME_SPEED == 0x4BC", OW.GS_GAME_SPEED == 0x4BC)
    check("offsets_win.G_GAME_SPEED_SECONDS == 0x33E6080", OW.G_GAME_SPEED_SECONDS == 0x33E6080)
    check("CSetGameSpeedCommand in WIN_SAFE", "CSetGameSpeedCommand" in cmdwin.WIN_SAFE)
    check("CSetGameSpeedCommand size=0x30 offset=+0x28",
          cmdwin.SIZE_SET_GAME_SPEED == 0x30
          and cmdwin.SPEC_PATCH["CSetGameSpeedCommand"]["size"] == 0x30
          and cmdwin.SPEC_PATCH["CSetGameSpeedCommand"]["fields"][0]["offset"] == 0x28)

    # 暂停态直写：设为 3（UI 4 速，工具硬上限）立即生效、不偷跑时钟
    g1 = _FakeSpeedGame(initial_speed=1, paused=True)
    with mock.patch("time.sleep", lambda _s: None):
        r1 = cmdwin.set_game_speed(g1, 3)
    check("paused set_game_speed(3) ok & verified (UI 4)",
          r1["ok"] and r1["verified"] and r1["speed_before"] == 1 and r1["speed_after"] == 3
          and r1["ui_speed"] == 4 and r1["clamped"] is False and r1["paused"] is True
          and r1["max_engine_speed"] == 3 and r1["max_ui_speed"] == 4
          and r1["seconds_per_hour"] == 0.1 and r1["speed_cap_mode"] == "fixed_interval"
          and r1["measured_rate"] is None and g1.p.paused == 1,
          r1)

    # 传入 4（原 UI 5 速）强制钳位到工具上限 3（UI 4 速）
    g1b = _FakeSpeedGame(initial_speed=1, paused=True)
    with mock.patch("time.sleep", lambda _s: None):
        r1b = cmdwin.set_game_speed(g1b, 4)
    check("set_game_speed(4) clamped to engine 3 (UI 4)",
          r1b["ok"] and r1b["requested"] == 4 and r1b["speed"] == 3 and r1b["engine_speed"] == 3
          and r1b["ui_speed"] == 4 and r1b["clamped"] is True
          and r1b["seconds_per_hour"] == 0.1 and "speed_note" in r1b and "note" in r1b,
          r1b)

    # 传入 5 同样强制钳位到工具上限 3（UI 4 速）
    g2 = _FakeSpeedGame(initial_speed=0, paused=True)
    with mock.patch("time.sleep", lambda _s: None):
        r2 = cmdwin.set_game_speed(g2, 5)
    check("set_game_speed(5) clamped to engine 3 (UI 4)",
          r2["ok"] and r2["requested"] == 5 and r2["speed"] == 3 and r2["engine_speed"] == 3
          and r2["ui_speed"] == 4 and r2["clamped"] is True and r2["seconds_per_hour"] == 0.1,
          r2)

    # 写后回弹检测：不能假报 ok=True
    g3 = _FakeSpeedGame(initial_speed=1, paused=True, reject_write=True)
    with mock.patch("time.sleep", lambda _s: None):
        r3 = cmdwin.set_game_speed(g3, 3)
    check("write rollback detected as ok=False, verified=False",
          (not r3["ok"]) and (not r3["verified"]) and r3["speed_after"] == 1 and "error" in r3,
          r3)

    # 暂停态显式 measure_seconds>0：短解暂停测速并严格恢复暂停
    g4 = _FakeSpeedGame(initial_speed=0, paused=True, ticks_per_call=24)
    tvals = iter([100.0, 102.0])
    with mock.patch("time.sleep", lambda _s: None), \
         mock.patch("time.monotonic", lambda: next(tvals)):
        r4 = cmdwin.set_game_speed(g4, 3, measure_seconds=2.0)
    check("explicit measure_seconds measures rate & restores pause",
          r4["ok"] and r4["verify_mode"] == "memory_and_clock"
          and r4["measured_hours"] == 24 and r4["measured_rate"] == 0.5
          and g4.p.paused == 1,
          r4)

    # 运行态（未暂停）默认自动采样实测速率
    g5 = _FakeSpeedGame(initial_speed=2, paused=False, ticks_per_call=12)
    tvals5 = iter([200.0, 202.2])
    with mock.patch("time.sleep", lambda _s: None), \
         mock.patch("time.monotonic", lambda: next(tvals5)):
        r5 = cmdwin.set_game_speed(g5, 3)
    check("running state auto-samples measured_rate",
          r5["ok"] and r5["verify_mode"] == "memory_and_clock"
          and r5["measured_hours"] == 12 and r5["measured_rate"] is not None
          and r5["measured_rate"] > 0.2 and g5.p.paused == 0,
          r5)

    # 非法范围抛 ValueError
    bad_raised = False
    try:
        cmdwin.set_game_speed(g1, 6)
    except ValueError:
        bad_raised = True
    check("speed=6 raises ValueError", bad_raised)

    # mcp/server.py _emergency_pause_game 使用 _g（修复原 _GAME NameError）
    import server as mcp_server
    g_ep = _FakeSpeedGame(initial_speed=3, paused=False)
    old_g = mcp_server._g
    try:
        mcp_server._g = g_ep
        ep_res = mcp_server._emergency_pause_game()
    finally:
        mcp_server._g = old_g
    check("server._emergency_pause_game pauses _g cleanly",
          ep_res is True and g_ep.p.paused == 1,
          (ep_res, g_ep.p.paused))


# ============================================================ 2. 科研前台守卫
class _DummyResearchGame:
    """复用 Game 真实的前台检查、失败分类与 fill_all_research_slots 逻辑。"""
    _check_foreground_guard = game_mod.Game._check_foreground_guard
    _classify_research_failure = staticmethod(game_mod.Game._classify_research_failure)
    fill_all_research_slots = game_mod.Game.fill_all_research_slots

    def __init__(self, slots, avail, start_fn):
        class _P:
            pid = 36540
            h = 1234
        self.p = _P()
        self._slots = [dict(s) for s in slots]
        self._avail = list(avail)
        self._start_fn = start_fn
        self._tech_by_name = {t: 1 for t in avail}
        self.started_calls = []

    def _load_tech_db(self, wait_s=5.0):
        pass

    def research_slots(self, tag=None):
        return [dict(s) for s in self._slots]

    def available_research(self, tag=None):
        return list(self._avail)

    def start_research(self, tech, slot=None, tag=None):
        self.started_calls.append((slot, tech))
        res = self._start_fn(self, tech, slot)
        if res.get("ok"):
            self._slots[slot]["slot_busy"] = True
            self._slots[slot]["researching"] = tech
        return res


def test_research_foreground_guard():
    print("\n== 2. fill_research 前台守卫与环境错误立即中止 ==")
    # 2a. 窗口不在前台：入口直接拒绝，一个槽都不填、一次 start_research 都不调
    slots = [
        {"slot": 0, "slot_busy": False, "researching": None},
        {"slot": 1, "slot_busy": False, "researching": None},
    ]
    g_bg = _DummyResearchGame(
        slots,
        ["construction2", "concentrated_industry2", "oil_processing"],
        lambda _self, _t, _s: {"ok": True},
    )
    fake_ws_bg = {"pid": 36540, "hwnd": "0x1234", "title": "Hearts of Iron IV",
                  "focused": False, "minimized": False, "visible": True}
    with mock.patch("windowstate.state", return_value=fake_ws_bg):
        r_bg = g_bg.fill_all_research_slots()
    check("background window rejected upfront: ok=False, blocked_by=foreground, 0 attempts",
          (not r_bg["ok"]) and r_bg.get("blocked_by") == "foreground"
          and r_bg.get("failure_class") == "environment"
          and r_bg["filled"] == 0 and r_bg["attempts"] == []
          and len(g_bg.started_calls) == 0,
          r_bg)

    # 2b. 入口在前台，但第 1 个 XOR 拒绝后，第 2 个焦点抖动触发 CallError -> 立即中止，绝不降级到 oil_processing
    class _FakeCallError(RuntimeError):
        pass

    def _flaky_start(_self, tech, slot):
        if tech == "dispersed_industry":
            return {
                "ok": False,
                "blocked_by": "mutually_exclusive",
                "failure_class": "content",
                "error": "dispersed_industry is mutually exclusive with concentrated_industry (XOR)",
            }
        if tech == "construction2":
            raise _FakeCallError(
                "game window is not in the foreground (pid 36540): refusing to hijack the render thread"
            )
        return {"ok": True}

    g_jitter = _DummyResearchGame(
        slots,
        ["dispersed_industry", "construction2", "concentrated_industry2", "oil_processing"],
        _flaky_start,
    )
    fake_ws_fg = {"pid": 36540, "hwnd": "0x1234", "title": "Hearts of Iron IV",
                  "focused": True, "minimized": False, "visible": True}
    with mock.patch("windowstate.state", return_value=fake_ws_fg):
        r_j = g_jitter.fill_all_research_slots()
    check("mid-loop foreground loss aborts without degrading to oil_processing",
          (not r_j["ok"]) and r_j.get("aborted") is True
          and r_j.get("blocked_by") == "foreground"
          and r_j.get("failure_class") == "environment"
          and len(r_j["attempts"]) == 2
          and r_j["attempts"][0]["reason_class"] == "content"
          and r_j["attempts"][0]["blocked_by"] == "mutually_exclusive"
          and r_j["attempts"][1]["reason_class"] == "environment"
          and r_j["attempts"][1]["blocked_by"] == "foreground"
          and "oil_processing" not in [t for _, t in g_jitter.started_calls],
          r_j)

    # 2c. 纯内容拒绝（XOR + CanExecute）正常顺延并填满所有槽
    def _content_start(_self, tech, slot):
        if tech == "dispersed_industry":
            return {"ok": False, "blocked_by": "mutually_exclusive",
                    "failure_class": "content", "error": "XOR"}
        if tech == "locked_tech":
            return {"ok": False, "blocked_by": "can_execute_refused",
                    "failure_class": "content", "error": "CSetResearchCommand::CanExecute refused"}
        return {"ok": True}

    g_ok = _DummyResearchGame(
        slots,
        ["dispersed_industry", "locked_tech", "construction2", "concentrated_industry2"],
        _content_start,
    )
    with mock.patch("windowstate.state", return_value=fake_ws_fg):
        r_ok = g_ok.fill_all_research_slots()
    check("content rejections fall through cleanly and fill both slots with reason_class",
          r_ok["ok"] and r_ok["filled"] == 2 and len(r_ok["attempts"]) == 4
          and r_ok["attempts"][0]["reason_class"] == "content"
          and r_ok["attempts"][1]["reason_class"] == "content"
          and r_ok["attempts"][2]["reason_class"] == "ok"
          and r_ok["attempts"][3]["reason_class"] == "ok",
          r_ok)

    # 2d. 满槽时即使后台也直接成功返回（纯只读）
    busy_slots = [{"slot": 0, "slot_busy": True, "researching": "construction2"}]
    g_full = _DummyResearchGame(busy_slots, ["oil_processing"], lambda *a: {"ok": True})
    with mock.patch("windowstate.state", return_value=fake_ws_bg):
        r_full = g_full.fill_all_research_slots()
    check("all slots already busy succeeds read-only even when backgrounded",
          r_full["ok"] and r_full["filled"] == 0, r_full)


# ============================================================ 3. 和平会谈选项
class _FakePeaceProc:
    def __init__(self, state_map):
        self.pid = 36540
        self.state_map = state_map  # act_ptr -> state_id
        self.writes = []

    def i32(self, addr):
        act = addr - 0x48
        if act in self.state_map:
            return self.state_map[act]
        return 0

    def write(self, addr, data):
        self.writes.append((addr, data))


class _FakePeaceGame:
    def __init__(self, state_map, names):
        self.p = _FakePeaceProc(state_map)
        self._names = names

    def player_tag_id(self):
        return 1

    def state_detail(self, sid, tag=None):
        return {"id": sid, "name": self._names.get(sid, "State %d" % sid)}


def test_peace_options_and_subset():
    print("\n== 3. 和平会谈选项缓存 + 子集索取 + Compact 联动 ==")
    hol_acts = {
        0x20001000: 7,
        0x20001080: 35,
        0x20001100: 36,
        0x20001180: 309,
        0x20001200: 695,
    }
    hol_names = {7: "Holland", 35: "Brabant", 36: "Friesland",
                 309: "Suriname", 695: "Curaçao"}
    g = _FakePeaceGame(hol_acts, hol_names)
    build_calls = []

    def _fake_build(game_obj, loser_idx, kind="state", winner_idx=None):
        build_calls.append((loser_idx, kind, winner_idx))
        return list(hol_acts.keys())

    def _fake_describe(game_obj, act):
        return {"act": act, "type": 0x30CF, "name": "获得地区",
                "a": 1, "b": 10, "a2": 1, "state": hol_acts[act], "extra": 0}

    # 3a. 无会议时干净返回 ok=False
    with mock.patch.object(PW, "live_conference", return_value=None):
        r0 = PW.options(g, 10)
    check("no conference returns ok=False without crashing",
          (not r0["ok"]) and "no peace conference" in r0["error"], r0)

    # 3b. 同一次会议连调 10 次 -> build_actions 只执行 1 次，winner_idx 为真值 1
    with mock.patch.object(PW, "live_conference", return_value=0x1B00001000), \
         mock.patch.object(PW, "build_actions", side_effect=_fake_build), \
         mock.patch.object(PW, "describe", side_effect=_fake_describe):
        first = PW.options(g, 10)
        rest = [PW.options(g, 10) for _ in range(9)]
    check("first call builds 5 HOL options with names and real winner_idx=1",
          first["ok"] and first["count"] == 5 and first["cached"] is False
          and [o["state"] for o in first["options"]] == [7, 35, 36, 309, 695]
          and first["options"][0]["name"] == "Holland"
          and build_calls == [(10, "state", 1)],
          (first, build_calls))
    check("9 repeated calls hit cache (0 extra build_actions allocations)",
          all(r["ok"] and r["cached"] is True and r["count"] == 5 for r in rest)
          and len(build_calls) == 1,
          len(build_calls))

    # 3c. 新会议指针变化 -> 缓存自动失效重建
    with mock.patch.object(PW, "live_conference", return_value=0x1B00009999), \
         mock.patch.object(PW, "build_actions", side_effect=_fake_build), \
         mock.patch.object(PW, "describe", side_effect=_fake_describe):
        r_new = PW.options(g, 10)
    check("new conference pointer invalidates cache cleanly",
          r_new["ok"] and r_new["cached"] is False and len(build_calls) == 2,
          (r_new.get("cached"), len(build_calls)))

    # 3d. 会议关闭后自动清缓存并返回 ok=False
    with mock.patch.object(PW, "live_conference", return_value=None):
        r_closed = PW.options(g, 10)
    check("closed conference clears cache and returns ok=False",
          (not r_closed["ok"]) and len(getattr(g, "_peace_opts_cache", {})) == 0,
          r_closed)

    # 3e. 危险 builder kind ('puppet'/'navy') 被拒绝
    r_bad_kind = PW.options(g, 10, kind="puppet")
    check("unsafe kind='puppet' blocked in peace_options",
          (not r_bad_kind["ok"]) and "not safe" in r_bad_kind["error"], r_bad_kind)

    # 3f. submit_demands 支持 states 子集过滤，且全不匹配时拒交空 turn
    installed = []
    with mock.patch.object(PW, "build_actions", side_effect=_fake_build), \
         mock.patch.object(PW, "new_turn", return_value=0x30001000), \
         mock.patch.object(PW, "install_turns", side_effect=lambda *a: installed.append(a)), \
         mock.patch.object(PW, "_allocator", return_value=0x7FF63C8BD000), \
         mock.patch.object(cmdwin, "alloc", return_value=0x30002000):
        finfo = {}
        turn, acts = PW.submit_demands(
            g, 0x1B00001000, 1, 10, kinds=("state",),
            states=[7, 35, 9999], filter_info=finfo,
        )
    check("submit_demands subsets states=[7,35,9999] -> matches 2, ignores 9999",
          len(acts) == 2 and finfo["matched_states"] == [7, 35]
          and finfo["ignored_states"] == [9999] and len(installed) == 1,
          (acts, finfo))

    empty_err = False
    with mock.patch.object(PW, "build_actions", side_effect=_fake_build):
        try:
            PW.submit_demands(g, 0x1B00001000, 1, 10, kinds=("state",), states=[8888])
        except ValueError:
            empty_err = True
    check("submit_demands refuses when none of requested states match", empty_err)


def test_mcp_and_turnengine_wiring():
    print("\n== 4. MCP 工具注册与 TurnEngine Plan DSL 校验 ==")
    import server as srv
    tool_map = {t["name"]: t for t in srv.TOOLS}
    check("hoi4_peace_options registered with required=['loser']",
          "hoi4_peace_options" in tool_map
          and tool_map["hoi4_peace_options"]["inputSchema"].get("required") == ["loser"])
    check("hoi4_peace_demands schema exposes optional 'states'",
          "states" in tool_map["hoi4_peace_demands"]["inputSchema"]["properties"])
    check("hoi4_set_game_speed schema exposes measure_seconds",
          "measure_seconds" in tool_map["hoi4_set_game_speed"]["inputSchema"]["properties"])

    ok_p, errs_p = TE.validate_plan({
        "actions": [
            {"op": "set_game_speed", "args": {"speed": 4, "measure_seconds": 0}},
            {"op": "peace_demands", "args": {"loser": "HOL", "states": [7, 35, 36]}},
        ],
        "memo": "test",
    })
    check("turnengine validates set_game_speed & peace_demands(states)",
          ok_p and not errs_p, errs_p)


if __name__ == "__main__":
    test_speed_fixes()
    test_research_foreground_guard()
    test_peace_options_and_subset()
    test_mcp_and_turnengine_wiring()
    print("\nSummary: %d passed, %d failed" % (PASS, FAIL))
    sys.exit(1 if FAIL else 0)
