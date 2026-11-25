# -*- coding: utf-8 -*-
"""阶段 3 WP4 契约测试：MCP 181 工具 Schema / Handler / 双门禁 / Stdio JSON-RPC 契约守护。"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CALLING = os.path.join(ROOT, "calling")
MCP_DIR = os.path.join(ROOT, "mcp")
for p in (CALLING, MCP_DIR):
    if p not in sys.path:
        sys.path.insert(0, p)

import game as game_mod
import server as MCP


class TestMCPSchemaContract(unittest.TestCase):
    def test_tools_and_handlers_strict_bijection_and_valid_json_schema(self):
        tools = list(MCP.TOOLS)
        handlers = dict(MCP.HANDLERS)
        names = [t.get("name") for t in tools]
        self.assertEqual(len(names), len(set(names)), "TOOLS 中不得存在重复工具名")
        self.assertEqual(len(names), 181, f"全量 MCP 工具总数应锁定为 181，当前={len(names)}")
        self.assertEqual(set(names), set(handlers.keys()), "TOOLS 与 HANDLERS 必须 1:1 严格对齐")

        for t in tools:
            name = t["name"]
            self.assertTrue(callable(handlers[name]), f"handler 不可调用: {name}")
            self.assertIsInstance(t.get("description"), str, f"缺少 description: {name}")
            self.assertTrue(t["description"].strip(), f"description 不可为空: {name}")
            schema = t.get("inputSchema")
            self.assertIsInstance(schema, dict, f"inputSchema 必须是 dict: {name}")
            self.assertEqual(schema.get("type"), "object", f"inputSchema.type 必须为 object: {name}")
            props = schema.get("properties")
            self.assertIsInstance(props, dict, f"inputSchema.properties 必须是 dict: {name}")
            req = schema.get("required") or []
            self.assertIsInstance(req, list, f"inputSchema.required 必须是 list: {name}")
            for rk in req:
                self.assertIn(rk, props, f"required 字段 {rk!r} 未在 properties 中声明: {name}")

    def test_debug_and_eval_dual_gate_and_compact_profile(self):
        orig_prof = MCP._MCP_PROFILE
        orig_dbg, orig_ev = MCP._ALLOW_DEBUG, MCP._ALLOW_EVAL
        linux_filtered = len(MCP.LINUX_ONLY_TOOL_NAMES) if sys.platform == "win32" else 0
        debug_total = sum(1 for t in MCP.TOOLS if t["name"].startswith("hoi4_debug_"))
        try:
            # 1) 默认 full：0 debug 泄漏（Windows 174 / Linux 175）
            MCP._MCP_PROFILE = "full"
            MCP._ALLOW_DEBUG, MCP._ALLOW_EVAL = False, False
            full_names = [t["name"] for t in MCP._active_tools()]
            self.assertEqual(len(full_names), 181 - linux_filtered - debug_total)
            self.assertFalse(any(n.startswith("hoi4_debug_") for n in full_names))

            # 2) 仅开 DEBUG：暴露非 eval 的 debug 工具，仍隐藏 hoi4_debug_eval（Windows 179 / Linux 180）
            MCP._ALLOW_DEBUG, MCP._ALLOW_EVAL = True, False
            dbg_names = [t["name"] for t in MCP._active_tools()]
            self.assertEqual(len(dbg_names), 181 - linux_filtered - 1)
            self.assertNotIn("hoi4_debug_eval", dbg_names)

            # 3) 双开关全开：暴露平台可用全部工具含 hoi4_debug_eval（Windows 180 / Linux 181）
            MCP._ALLOW_DEBUG, MCP._ALLOW_EVAL = True, True
            all_names = [t["name"] for t in MCP._active_tools()]
            self.assertEqual(len(all_names), 181 - linux_filtered)
            self.assertIn("hoi4_debug_eval", all_names)

            # 4) compact 模式：严格 5 工具
            MCP._MCP_PROFILE = "compact"
            MCP._ALLOW_DEBUG, MCP._ALLOW_EVAL = False, False
            compact_names = [t["name"] for t in MCP._active_tools()]
            self.assertEqual(len(compact_names), 5)
            for req_tool in (
                "hoi4_status",
                "hoi4_turn_brief",
                "hoi4_inspect",
                "hoi4_run_plan",
                "hoi4_wait_and_brief",
            ):
                self.assertIn(req_tool, compact_names)
        finally:
            MCP._MCP_PROFILE = orig_prof
            MCP._ALLOW_DEBUG, MCP._ALLOW_EVAL = orig_dbg, orig_ev

    def test_game_facade_core_methods_intact(self):
        required_methods = [
            "overview", "current_focus", "selectable_focuses", "set_focus",
            "production_lines", "construction_queue", "research_slots", "start_research",
            "armies", "create_army", "plan_front", "delete_order",
            "diplomatic_actions", "declare_war", "justify_wargoal",
            "air_wings", "fleets", "resolve", "reset_caches", "reset_front_snapshot",
        ]
        for m in required_methods:
            self.assertTrue(hasattr(game_mod.Game, m), f"Game 缺少核心公开方法: {m}")
            self.assertTrue(callable(getattr(game_mod.Game, m)), f"Game.{m} 不可调用")

    def test_stdio_jsonrpc_handshake_clean_stdout(self):
        """端到端启动 mcp/server.py 子进程，验证 Full 与 --compact 模式 stdout 帧纯净。"""
        server_py = os.path.join(MCP_DIR, "server.py")
        payload = (
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}) + "\n"
            + json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}) + "\n"
        )
        with tempfile.TemporaryDirectory() as td:
            env = dict(os.environ)
            env["HOI4_STATE_DIR"] = td
            env["HOI4_NO_PICKLE"] = "1"
            env.pop("HOI4_MCP_ALLOW_DEBUG", None)
            env.pop("HOI4_MCP_ALLOW_EVAL", None)
            for args, expected_cnt in (([], 174), (["--compact"], 5)):
                cp = subprocess.run(
                    [sys.executable, server_py] + args,
                    input=payload,
                    env=env,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    timeout=15,
                )
                self.assertEqual(cp.returncode, 0, f"server.py {args} exited {cp.returncode}: {cp.stderr}")
                lines = [l for l in cp.stdout.splitlines() if l.strip()]
                self.assertEqual(len(lines), 2, f"stdout 必须仅含 2 条 JSON-RPC 响应，实际={len(lines)}")
                r1, r2 = json.loads(lines[0]), json.loads(lines[1])
                self.assertEqual(r1.get("id"), 1)
                self.assertEqual(r2.get("id"), 2)
                tools_list = r2["result"]["tools"]
                self.assertEqual(len(tools_list), expected_cnt)
                self.assertFalse(any(t["name"].startswith("hoi4_debug_") for t in tools_list))


if __name__ == "__main__":
    unittest.main()
