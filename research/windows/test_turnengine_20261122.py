"""方案 2.5（turnengine + orchestrator + compact MCP）离线回归测试。

运行：python research/windows/test_turnengine_20261122.py
"""
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "calling"))
sys.path.insert(0, os.path.join(ROOT, "mcp"))

import turnengine as TE
import orchestrator as ORCH


class _DummyProc:
    def __init__(self, alive=True, gs_ptr=0x22500000000):
        self.pid = 32028
        self.base = 0x7FF600000000
        self.alive = alive
        self.gs_ptr = gs_ptr

    def read(self, addr, size):
        if not self.alive:
            raise OSError(299, "dead")
        return b"MZ\x90\x00"[:size]

    def u64(self, addr):
        if not self.alive:
            raise OSError(299, "dead")
        return self.gs_ptr


class _DummyGame:
    def __init__(self, focus_current=None, empty_orders=False, alive=True):
        self.p = _DummyProc(alive=alive)
        self.focus_current = focus_current
        self.empty_orders = empty_orders
        self.calls = []
        self._tech_by_name = {"basic_machine_tools": 1}

    def player_tag(self):
        return "GER"

    def date(self):
        return {"text": "1936.12.26.10", "raw": 16967986, "source": "clock (exact)"}

    def overview(self, tag=None):
        return {
            "date": "1936.12.26.10",
            "alerts": ["FOCUS_IDLE", "RESEARCH_IDLE"],
            "factories": {"civ_total": 41, "mil_total": 28, "mil_free": 2,
                          "dock_total": 10, "construction_queue_len": 2},
            "army_summary": {"total_divisions": 34, "unassigned_divisions": 0},
            "country": {
                "tag": "GER", "name": "German Reich",
                "political_power": 805, "political_power_daily": 2.32,
                "command_power": 14.1, "stability": 0.81, "war_support": 0.40,
                "manpower": 1299000, "fuel": 352000,
                "experience": {"army": 20.0, "navy": 12.0, "air": 8.0},
            },
            "focus": {"current": self.focus_current, "name": self.focus_current, "tree_size": 100},
            "research": [
                {"slot": 0, "slot_busy": False, "researching": None, "percent": None},
                {"slot": 1, "slot_busy": True, "researching": "electronic_mechanical_engineering", "percent": 45.0},
            ],
            "construction": [{"state": 64, "building": "arms_factory", "factories": 15, "percent": 32.0}],
            "production": [{"index": 0, "equipment": "infantry_equipment_1", "factories": 10,
                            "class": "CMilitaryProductionLine"}],
            "candidates": {"available_decisions": [
                {"key": "GER_mefo_bills", "cost_pp": 50, "mine": True},
                {"key": "JAP_other", "cost_pp": 25, "mine": False},
            ]},
            "states": 29,
        }

    def selectable_focuses(self, tag=None):
        return {"selectable": [{"key": "GER_rhineland", "name": "莱茵兰"}]}

    def available_research(self, tag=None):
        return ["basic_machine_tools", "construction1", "support_weapons"]

    def country_stats(self, tag=None):
        return {"tag": tag or "GER", "factories": 79, "divisions": 34}

    def war_readiness_check(self, target, tag=None):
        return {"target": target, "ready": False, "missing": ["wargoal"]}

    def order_groups(self, tag=None):
        return [{"ptr": "0x1000"}] if self.empty_orders else []

    def orders_of(self, ptr):
        return [{"id": 7, "assigned_divisions": 0}] if self.empty_orders else []

    def set_focus(self, key, tag=None):
        self.calls.append(("set_focus", key))
        return {"ok": True, "focus": key}

    def fill_all_research_slots(self, techs=None, tag=None):
        self.calls.append(("fill_research", list(techs or [])))
        return {"ok": True, "filled": 1}

    def add_idea(self, key, tag=None):
        self.calls.append(("add_idea", key))
        return {"ok": False, "blocked_by": "war_support", "reason": "needs >= 0.50"}

    def add_construction(self, state_id, building, level=1, province=0, tag=None):
        self.calls.append(("add_construction", state_id, building, level, province))
        return {"ok": True, "state": state_id, "building": building}

    def set_production_amount(self, line_index, amount, tag=None):
        self.calls.append(("set_production", line_index, amount))
        return {"ok": True, "line": line_index, "after": amount}

    def start_research(self, technology, slot=None, tag=None):
        self.calls.append(("start_research", technology, slot))
        return {"ok": True, "technology": technology}

    def train_divisions(self, template_name=None, lines=2, state_id=None, tag=None):
        self.calls.append(("train_divisions", template_name, lines, state_id))
        return {"ok": True}

    def state_detail(self, state_id, tag=None):
        return {"id": state_id, "name": "State_%d" % state_id, "slots_free": 2,
                "buildings": {"infrastructure": 4, "industrial_complex": 2}}


def test_validate_plan():
    ok, errs = TE.validate_plan({"actions": [{"op": "set_focus", "args": {"key": "GER_rhineland"}}]})
    assert ok and not errs

    ok, errs = TE.validate_plan({"actions": [{"op": "hack_memory"}]})
    assert not ok and any("不在白名单" in e for e in errs)

    ok, errs = TE.validate_plan({"actions": [{"op": "pause"}] * (TE.MAX_ACTIONS_PER_TURN + 1)})
    assert not ok

    ok, errs = TE.validate_plan({"inspect": [{"topic": "country"}] * (TE.MAX_INSPECT_QUERIES + 1)})
    assert not ok

    # P1-1: 公开工具参数名（state/line/template/technology/target/slot）与旧别名均应通过校验
    ok, errs = TE.validate_plan({"actions": [
        {"op": "add_construction", "args": {"state": 65, "building": "industrial_complex", "level": 1}},
        {"op": "add_construction", "args": {"state_id": 64, "building": "arms_factory"}},
        {"op": "set_production", "args": {"line": 4, "amount": 2}},
        {"op": "set_production", "args": {"line_index": 3, "amount": 5}},
        {"op": "start_research", "args": {"technology": "construction1", "slot": 0}},
        {"op": "start_research", "args": {"tech": "basic_machine_tools"}},
        {"op": "train_divisions", "args": {"template": "Infanterie", "lines": 2, "state": 64}},
        {"op": "train_divisions", "args": {"template_name": "Infanterie", "state_id": 64}},
        {"op": "hire_advisor", "args": {"key": "GER_hjalmar_schacht", "slot": "political_advisor"}},
        {"op": "justify_wargoal", "args": {"target": "HOL"}},
        {"op": "declare_war", "args": {"target": "POL", "dry_run": True}},
        {"op": "add_idea", "args": {"idea_id": "war_economy"}},
    ]})
    assert ok and not errs, errs

    # P1-1: 缺必填键 / 未知键 / 错误类型 -> 必须在 validate_plan 与 dry_run 直接拦截
    ok, errs = TE.validate_plan({"actions": [
        {"op": "add_construction", "args": {"building": "arms_factory"}},
    ]})
    assert not ok and any("state" in e for e in errs), errs

    ok, errs = TE.validate_plan({"actions": [
        {"op": "set_production", "args": {"line": 1, "bad_arg": 9}},
    ]})
    assert not ok and any("不支持参数" in e for e in errs), errs

    ok, errs = TE.validate_plan({"actions": [
        {"op": "set_production", "args": {"line": True, "amount": 2}},
    ]})
    assert not ok and any("整数" in e for e in errs), errs

    # inspect 校验含新 topic events 与 state
    ok, errs = TE.validate_plan({"inspect": [{"topic": "events"}, {"topic": "state", "state": 64}]})
    assert ok and not errs, errs
    ok, errs = TE.validate_plan({"inspect": [{"topic": "state"}]})
    assert not ok, errs


def test_run_plan_public_args_and_dry_run_strict():
    g = _DummyGame()
    orig_wg = TE._window_guard
    TE._window_guard = lambda _g: (True, None, {"focused": True})
    try:
        # 缺键时 dry_run 也必须拦下，不得假装 [DRY] 成功
        r_bad = TE.execute_plan(
            g,
            {"actions": [{"op": "add_construction", "args": {"building": "arms_factory"}}]},
            dry_run=True, persist_state=False,
        )
        assert r_bad["ok"] is False and r_bad["blocked_by"] == "invalid_plan"

        # 公开名与旧别名混合真执行 -> 全部成功、零 KeyError
        r_ok = TE.execute_plan(
            g,
            {"actions": [
                {"op": "add_construction", "args": {"state": 65, "building": "industrial_complex", "level": 1}},
                {"op": "set_production", "args": {"line": 4, "amount": 2}},
                {"op": "start_research", "args": {"technology": "construction1", "slot": 0}},
                {"op": "train_divisions", "args": {"template": "Infanterie", "lines": 2, "state": 64}},
            ]},
            dry_run=False, persist_state=False,
        )
        assert r_ok["ok"] is True and r_ok["succeeded"] == 4 and r_ok["failed"] == 0, r_ok
        assert ("add_construction", 65, "industrial_complex", 1, 0) in g.calls
        assert ("set_production", 4, 2) in g.calls
        assert ("start_research", "construction1", 0) in g.calls
        assert ("train_divisions", "Infanterie", 2, 64) in g.calls
    finally:
        TE._window_guard = orig_wg


def test_brief_truncation_counts_and_wait_fields():
    # P2-3: 21 条生产线应报 (21)，列表显示 12 条并带 `… +9 条`
    g = _DummyGame(focus_current="GER_four_year_plan")
    orig_ov = g.overview
    def _ov_21(tag=None):
        d = orig_ov(tag)
        d["production"] = [
            {"index": i, "equipment": "eq_%d" % i, "factories": 1, "class": "CMilitaryProductionLine"}
            for i in range(21)
        ]
        d["construction"] = [
            {"index": i, "state": 64 + i, "building": "industrial_complex",
             "factories": 15 if i == 0 else 0, "progress": 0.1 if i == 0 else 0.0,
             "status": "building" if i == 0 else "queued", "stalled": False}
            for i in range(10)
        ]
        return d
    g.overview = _ov_21
    res = TE.build_turn_brief(g, include_data=True)
    md = res["markdown"]
    assert "**生产线(21)**" in md and "… +9 条" in md, md
    assert "**建造队列(10)**" in md and "… +2 条" in md, md
    assert "@州64" in md and "排队中" in md and "停滞" not in md, md
    assert res["brief"]["production_total"] == 21

    # P3-1: wait_and_brief 必须透传 waited_game_days / waited_real_seconds
    import turnloop
    orig_wait = turnloop.TurnLoop.wait
    try:
        turnloop.TurnLoop.wait = lambda self, **kw: {
            "woke_because": ["real-time safety brake (5s)"],
            "waited_game_days": 0.25,
            "waited_real_seconds": 5.1,
            "paused": True,
        }
        wb = TE.wait_and_brief(g, max_days=14, timeout_seconds=5)
        assert wb["wait"]["days_elapsed"] == 0.25, wb["wait"]
        assert wb["wait"]["waited_s"] == 5.1, wb["wait"]
        assert wb["wait"]["paused"] is True
    finally:
        turnloop.TurnLoop.wait = orig_wait


def test_date_clock_alignment_and_slot_capping():
    import offsets_win as OW
    import api
    import staff_industry as SI

    # P2-1: 1936.1.1.12 在引擎内编码为 60759371；减 GS_CLOCK_OFF 应严格为 1936.01.01.12
    assert OW.GS_CLOCK_OFF == 43799999
    assert api.date_to_parts(60759371 - OW.GS_CLOCK_OFF) == (1936, 1, 1, 12)
    # 1936.01.02.23 (tick = (5000+1936)*8760 + 1*24 + 23 = 60759407) -> HUD 1936.01.03.00
    assert api.date_to_parts(60759407 - OW.GS_CLOCK_OFF) == (1936, 1, 3, 0)
    # 1936.12.31.23 -> 1937.01.01.00，总天数差 <= 1
    tick_ny = (5000 + 1936) * 8760 + 364 * 24 + 23
    val_ny = tick_ny - OW.GS_CLOCK_OFF
    assert api.date_to_parts(val_ny) == (1937, 1, 1, 0)
    assert abs((val_ny // 24) - (1936 * 365 + 364)) <= 1
    # log_delta_days 向零截断（-1 小时 -> 0 天，不是 -1 天）
    assert api._sdiv(-1, 24) == 0 and api._sdiv(-25, 24) == -1 and api._sdiv(25, 24) == 1

    # P1-2: staff_industry.construction_plan 严格按 slots_free 截断每州条数
    class _SlotGame:
        def date(self):
            return {"year": 1936, "month": 1, "day": 5, "text": "1936.01.05.12"}
        def states(self, tag=None):
            return [{"id": 65}, {"id": 57}, {"id": 99}]
        def state_detail(self, sid, tag=None):
            if sid == 65:
                return {"id": 65, "buildings": {"infrastructure": 4, "industrial_complex": 7, "arms_factory": 2},
                        "provinces": 10, "slots_free": 1}
            if sid == 57:
                return {"id": 57, "buildings": {"infrastructure": 4, "industrial_complex": 1, "arms_factory": 3},
                        "provinces": 10, "slots_free": 4}
            return {"id": 99, "buildings": {"infrastructure": 5, "industrial_complex": 10},
                    "provinces": 10, "slots_free": 0}
    snap = {"civ": 20, "mil": 28, "queue": [], "lines": [], "mil_used": 28, "mil_idle": 0, "at_war": False}
    plan = SI.construction_plan(_SlotGame(), snap=snap)
    items_65 = [x for x in plan["items"] if x["state"] == 65]
    items_99 = [x for x in plan["items"] if x["state"] == 99]
    items_57 = [x for x in plan["items"] if x["state"] == 57]
    assert len(items_65) == 1, items_65   # 1 空位州绝不塞 2 条
    assert len(items_99) == 0, items_99   # 0 空位满州直接跳过
    assert len(items_57) == 2, items_57   # 4 空位州可输出 2 条候选


def test_compact_strict_call_and_utf8_subprocess():
    import subprocess
    import server

    orig_prof = server._MCP_PROFILE
    orig_strict = server._COMPACT_STRICT
    try:
        # P2-11: compact 模式默认拦截非 5 工具的 tools/call
        server._MCP_PROFILE = "compact"
        server._COMPACT_STRICT = True
        r_block = server.handle({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "hoi4_overview", "arguments": {}},
        })
        assert "error" in r_block and "compact profile" in r_block["error"]["message"], r_block

        # 所有工具 schema 不得再出现嵌套 properties.type 坏结构
        bad = [
            t["name"] for t in server.TOOLS
            if isinstance(((t.get("inputSchema") or {}).get("properties") or {}).get("type"), str)
        ]
        assert not bad, bad
    finally:
        server._MCP_PROFILE = orig_prof
        server._COMPACT_STRICT = orig_strict

    # P1-10: 在显式删除 PYTHONIOENCODING 的干净环境下起 compact 子进程，
    # 验证含中文响应严格按 UTF-8 编码输出（无 GBK 乱码）
    env = dict(os.environ)
    env.pop("PYTHONIOENCODING", None)
    env.pop("PYTHONUTF8", None)
    req_lines = (
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}) + "\n"
        + json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}) + "\n"
        + json.dumps({
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "hoi4_run_plan", "arguments": {
                "dry_run": True,
                "actions": [{"op": "add_construction", "args": {"building": "arms_factory"}}],
            }},
        }) + "\n"
        + json.dumps({
            "jsonrpc": "2.0", "id": 4, "method": "tools/call",
            "params": {"name": "hoi4_overview", "arguments": {}},
        }) + "\n"
    ).encode("utf-8")
    proc = subprocess.run(
        [sys.executable, os.path.join(ROOT, "mcp", "server.py"), "--compact"],
        input=req_lines, capture_output=True, env=env, timeout=20,
    )
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    # 必须严格能以 utf-8 解码（如果仍是 GBK 字节这里必抛 UnicodeDecodeError）
    out_text = proc.stdout.decode("utf-8")
    resps = [json.loads(ln) for ln in out_text.splitlines() if ln.strip()]
    assert len(resps) == 4, resps
    assert len(resps[1]["result"]["tools"]) == 5
    # 第 3 条含中文校验错误信息
    txt3 = resps[2]["result"]["content"][0]["text"]
    assert "invalid_plan" in txt3 and "缺少必填参数" in txt3, txt3
    # 第 4 条被 compact 严格拦截
    assert "error" in resps[3] and "compact profile" in resps[3]["error"]["message"], resps[3]


def test_build_turn_brief_and_inspect():
    g = _DummyGame(focus_current=None)
    res = TE.build_turn_brief(g, memo="1937 工业扩张", last_receipt="[OK] fill_research()")
    assert res["ok"] is True
    assert "brief" not in res, "默认精简返回不应附带 6KB 冗余 brief dict"
    md = res["markdown"]
    assert "GER_rhineland" in md
    assert "basic_machine_tools" in md
    assert "1937 工业扩张" in md
    assert "JAP_other" not in md  # 非 mine 决议已过滤
    assert res["approx_tokens"] < 2000

    res_full = TE.build_turn_brief(g, memo="x", include_data=True)
    assert "brief" in res_full and res_full["brief"]["meta"]["tag"] == "GER"

    # inspect_topics 最多 3 条截断与白名单拦截
    ir = TE.inspect_topics(g, [
        {"topic": "country", "tag": "SOV"},
        {"topic": "war_readiness", "target": "HOL"},
        {"topic": "bogus_topic"},
        {"topic": "resources"},  # 第 4 条应被截断
    ])
    assert ir["ok"] is True and ir["truncated"] is True and ir["count"] == 3
    assert ir["results"][0]["ok"] is True and ir["results"][0]["data"]["tag"] == "SOV"
    assert ir["results"][1]["ok"] is True and ir["results"][1]["data"]["target"] == "HOL"
    assert ir["results"][2]["ok"] is False


def test_execute_plan_guards_and_abort():
    g = _DummyGame()
    orig_wg = TE._window_guard
    try:
        # 1. 非前台窗口应硬拒写
        TE._window_guard = lambda _g: (False, "window_not_foreground", {"focused": False})
        r = TE.execute_plan(g, {"actions": [{"op": "set_focus", "args": {"key": "GER_rhineland"}}]},
                            persist_state=False)
        assert r["ok"] is False and r["blocked_by"] == "window_not_foreground"
        assert g.calls == []

        # 2. 前台正常：continue vs abort
        TE._window_guard = lambda _g: (True, None, {"focused": True})
        r = TE.execute_plan(g, {
            "memo": "短备忘",
            "actions": [
                {"op": "add_idea", "args": {"key": "war_economy"}, "on_error": "continue"},
                {"op": "set_focus", "args": {"key": "GER_rhineland"}},
            ],
        }, persist_state=False)
        assert r["ok"] is True and r["executed"] == 2 and r["succeeded"] == 1 and r["failed"] == 1
        assert "[FAIL] add_idea" in r["receipt_text"] and "[OK] set_focus" in r["receipt_text"]

        g.calls.clear()
        r_abort = TE.execute_plan(g, {
            "actions": [
                {"op": "add_idea", "args": {"key": "war_economy"}, "on_error": "abort"},
                {"op": "set_focus", "args": {"key": "GER_rhineland"}},
            ],
        }, persist_state=False)
        assert r_abort["ok"] is False and r_abort["aborted"] is True and r_abort["executed"] == 1
        assert g.calls == [("add_idea", "war_economy")]

        # 3. 进程在动作中死亡 -> 立刻熔断
        g2 = _DummyGame()
        def _kill_on_focus(key, tag=None):
            g2.p.alive = False
            return {"ok": True}
        g2.set_focus = _kill_on_focus
        r_dead = TE.execute_plan(g2, {
            "actions": [
                {"op": "set_focus", "args": {"key": "GER_rhineland"}},
                {"op": "fill_research", "args": {}},
            ],
        }, persist_state=False)
        assert r_dead["ok"] is False and r_dead["aborted"] is True
        assert "process_dead" in r_dead["abort_reason"] and r_dead["executed"] == 1

        # 4. 存在 0 师订单时 resume 被硬拒
        g3 = _DummyGame(empty_orders=True)
        r_res = TE.execute_plan(g3, {"actions": [{"op": "resume"}]}, persist_state=False)
        assert r_res["failed"] == 1 and "empty_orders" in r_res["receipt_text"]

        # 5. dry_run 不触碰游戏且不要求前台
        TE._window_guard = lambda _g: (False, "window_minimized", {"minimized": True})
        r_dry = TE.execute_plan(g, {"actions": [{"op": "set_focus", "args": {"key": "X"}}]},
                                dry_run=True, persist_state=False)
        assert r_dry["ok"] is True and r_dry["dry_run"] is True
    finally:
        TE._window_guard = orig_wg


def test_orchestrator_bounded_inspect():
    g = _DummyGame()
    orig_wg = TE._window_guard
    TE._window_guard = lambda _g: (True, None, {"focused": True})
    try:
        seq = [
            ({"memo": "先查荷兰", "inspect": [{"topic": "war_readiness", "target": "HOL"}], "actions": []},
             {"prompt_tokens": 1800, "completion_tokens": 80}),
            ({"memo": "缺借口先点国策", "inspect": [],
              "actions": [{"op": "set_focus", "args": {"key": "GER_rhineland"}}]},
             {"prompt_tokens": 2100, "completion_tokens": 90}),
        ]
        seen_briefs = []
        def _fake_llm(sp, ub):
            seen_briefs.append(ub)
            return seq.pop(0)

        out = ORCH.run_one_turn(g, _fake_llm, "sys", dry_run=True, max_inspect_rounds=1)
        assert out["ok"] is True and out["llm_calls"] == 2
        assert "补充情报 (inspect)" in seen_briefs[1]
        assert "HOL" in seen_briefs[1]

        # 即使模型一直要 inspect，也绝不超过 max_inspect_rounds + 1 = 2 次调用
        calls = [0]
        def _loop_llm(sp, ub):
            calls[0] += 1
            return {"memo": "loop", "inspect": [{"topic": "country", "tag": "SOV"}], "actions": []}, {}
        out2 = ORCH.run_one_turn(g, _loop_llm, "sys", dry_run=True, max_inspect_rounds=1)
        assert calls[0] == 2 and out2["llm_calls"] == 2
    finally:
        TE._window_guard = orig_wg


def test_compact_mcp_profile_and_extract_json():
    import server
    orig_prof = server._MCP_PROFILE
    try:
        server._MCP_PROFILE = "full"
        full_tools = server._active_tools()
        full_names = [t["name"] for t in full_tools]
        assert len(full_names) == len(set(full_names)), "full 模式存在重名工具！"
        full_chars = len(json.dumps(full_tools, ensure_ascii=False))

        server._MCP_PROFILE = "compact"
        compact_tools = server._active_tools()
        compact_chars = len(json.dumps(compact_tools, ensure_ascii=False))

        assert len(compact_tools) == 5, [t["name"] for t in compact_tools]
        assert set(t["name"] for t in compact_tools) == set(server.COMPACT_TOOL_NAMES)
        assert compact_chars < 6000 and compact_chars * 10 < full_chars, (compact_chars, full_chars)
    finally:
        server._MCP_PROFILE = orig_prof

    parsed = ORCH.extract_json_object("```json\n{\"actions\": [], \"memo\": \"ok\"}\n```")
    assert parsed == {"actions": [], "memo": "ok"}

    # 状态持久化原子写、长度截断与跨进程/换档/换国隔离
    with tempfile.TemporaryDirectory() as td:
        sp = os.path.join(td, "state.json")
        TE.save_turn_state({
            "turn": 3, "pid": 32028, "proc_start": 11111,
            "tag": "GER", "date_raw": 16967986,
            "memo": "x" * 2000, "last_receipt": "[OK]",
        }, path=sp)
        # 同进程、同国、正常推进 7 天 -> 保留
        loaded = TE.load_turn_state(path=sp, pid=32028, proc_start=11111, tag="GER", date_raw=16967986 + 7 * 24)
        assert loaded["turn"] == 3 and len(loaded["memo"]) == TE.MAX_MEMO_CHARS
        # 换 pid -> 清零
        assert TE.load_turn_state(path=sp, pid=99999, proc_start=11111, tag="GER", date_raw=16967986)["turn"] == 0
        # 同 pid 但 proc_start 变了（PID 复用）-> 清零
        assert TE.load_turn_state(path=sp, pid=32028, proc_start=22222, tag="GER", date_raw=16967986)["turn"] == 0
        # 换国 -> 清零
        assert TE.load_turn_state(path=sp, pid=32028, proc_start=11111, tag="SOV", date_raw=16967986)["turn"] == 0
        # 读旧档（date_raw 倒退 1 小时）-> 清零
        assert TE.load_turn_state(path=sp, pid=32028, proc_start=11111, tag="GER", date_raw=16967985)["turn"] == 0
        # 换剧本（跳超过 365 天）-> 清零
        assert TE.load_turn_state(path=sp, pid=32028, proc_start=11111, tag="GER", date_raw=16967986 + 400 * 24)["turn"] == 0
        # reset_turn_state 清理文件
        TE.reset_turn_state(path=sp)
        assert not os.path.exists(sp)


def test_sweep_fixes_20261122():
    import game
    import server

    # P2-3: Windows 下 tools/list 默认隐藏 Linux 专属工具 hoi4_send_diplomatic_action
    orig_prof = server._MCP_PROFILE
    orig_show = server._SHOW_UNSUPPORTED
    try:
        server._MCP_PROFILE = "full"
        server._SHOW_UNSUPPORTED = False
        names = {t["name"] for t in server._active_tools()}
        if sys.platform == "win32":
            assert "hoi4_send_diplomatic_action" not in names
        server._SHOW_UNSUPPORTED = True
        assert "hoi4_send_diplomatic_action" in {t["name"] for t in server._active_tools()}
    finally:
        server._MCP_PROFILE = orig_prof
        server._SHOW_UNSUPPORTED = orig_show

    # 统一缺参拦截（不再吐土耳其语 HATA / KeyError traceback）
    orig_prof2 = server._MCP_PROFILE
    try:
        server._MCP_PROFILE = "full"
        r_miss = server.handle({
            "jsonrpc": "2.0", "id": 9, "method": "tools/call",
            "params": {"name": "hoi4_add_construction", "arguments": {}},
        })
        assert not r_miss["result"].get("isError")
        body = json.loads(r_miss["result"]["content"][0]["text"])
        assert body["ok"] is False and body["blocked_by"] == "missing_parameter"
        assert set(body["missing"]) == {"state", "building"}, body
    finally:
        server._MCP_PROFILE = orig_prof2

    # P2-2: diplomatic_action_info 兼容 Windows `classes` 键与 Linux `actions` 键
    class _FakeDip(game.ExtraMixin):
        def diplomatic_action_classes(self):
            return {"ok": True, "source": "pe-rtti", "count": 2,
                    "classes": [{"name": "CDeclareWarAction", "vtables": ["0x1"]}]}
        def command_help(self, name):
            return {"name": name}
    di = _FakeDip().diplomatic_action_info()
    assert di["ok"] is True and di["count"] == 1 and di["actions"][0]["name"] == "CDeclareWarAction"

    # P3-3: drop_focus 无国策在跑时返回带 reason 的结构化结果
    class _FakeDrop:
        def _c(self, tag=None):
            return type("C", (), {"tag_id": 1, "ptr": 0x1000})()
        def current_focus(self, tag=None):
            return None
    df = game.Game.drop_focus(_FakeDrop())
    assert df["ok"] is False and df["reason"] == "no focus in progress", df

    # compact inspect(staff) 瘦身 + 长告警截断
    compact = TE._compact_staff_report(
        ind={"at_war": False, "problems": ["p1"], "production": {"mil_idle": 0, "proposal": []},
             "construction": {"civ_gap": 1.5, "mil_gap": -2.0, "queue_len": 2, "want": 2,
                              "items": [{"state": i, "building": "arms_factory", "slots_free": 3, "extra": "x" * 80}
                                        for i in range(10)]}},
        arm={"divisions": 36, "in_training": 4, "cap": 30, "binding": "year", "problems": ["over"],
             "actions": [{"action": "disband", "count": 6, "divisions": [{"ptr": hex(i)} for i in range(6)]}]},
        foc={"running": {"key": "GER_x"}, "action": None},
    )
    assert compact["compact"] is True
    assert len(compact["industry"]["construction"]["top_items"]) == 4
    assert "divisions" not in compact["army"]["actions_summary"][0]
    assert len(json.dumps(compact, ensure_ascii=False)) < 800

    # 长告警截断
    g = _DummyGame(focus_current="GER_x")
    orig_ov = g.overview
    def _long_alert_ov(tag=None):
        d = orig_ov(tag)
        d["alerts"] = ["CONSTRUCTION_STALLED: " + "很长的告警说明" * 30]
        return d
    g.overview = _long_alert_ov
    tb = TE.build_turn_brief(g)
    alert_line = [ln for ln in tb["markdown"].splitlines() if "告警" in ln][0]
    assert len(alert_line) < 115 and alert_line.endswith("…"), alert_line


def main():
    tests = [
        test_validate_plan,
        test_run_plan_public_args_and_dry_run_strict,
        test_build_turn_brief_and_inspect,
        test_brief_truncation_counts_and_wait_fields,
        test_execute_plan_guards_and_abort,
        test_orchestrator_bounded_inspect,
        test_date_clock_alignment_and_slot_capping,
        test_compact_mcp_profile_and_extract_json,
        test_compact_strict_call_and_utf8_subprocess,
        test_sweep_fixes_20261122,
    ]
    for fn in tests:
        fn()
        print("OK  ", fn.__name__)
    print("\n%d/%d passed" % (len(tests), len(tests)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
