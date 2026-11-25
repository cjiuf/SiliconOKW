# -*- coding: utf-8 -*-
"""阶段 3 WP2 单元测试：calling/diag.py 与核心高危路径静默异常可观测性验证。"""
import io
import os
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CALLING = os.path.join(ROOT, "calling")
if CALLING not in sys.path:
    sys.path.insert(0, CALLING)

import diag as _diag
import events_win as _events
import turnengine as _te


class TestDiagAndSilentExceptGovernance(unittest.TestCase):
    def setUp(self):
        self._old_quiet = os.environ.get("HOI4_DIAG_QUIET")
        self._old_verbose = os.environ.get("HOI4_DIAG_VERBOSE")
        os.environ.pop("HOI4_DIAG_QUIET", None)
        os.environ.pop("HOI4_DIAG_VERBOSE", None)
        _diag.reset()

    def tearDown(self):
        for name, val in (("HOI4_DIAG_QUIET", self._old_quiet),
                          ("HOI4_DIAG_VERBOSE", self._old_verbose)):
            if val is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = val
        _diag.reset()

    def test_diag_writes_only_stderr_never_stdout(self):
        out_buf = io.StringIO()
        err_buf = io.StringIO()
        with mock.patch.object(sys, "stdout", out_buf), mock.patch.object(sys, "stderr", err_buf):
            _diag.warn("unit.test.tag", RuntimeError("disk full"), detail="path=/tmp/a")
        self.assertEqual(out_buf.getvalue(), "", "diag 严禁向 stdout 写入任何字符")
        self.assertIn("[hoi4-diag] unit.test.tag: RuntimeError: disk full", err_buf.getvalue())
        self.assertIn("path=/tmp/a", err_buf.getvalue())
        st = _diag.stats()
        self.assertEqual(st["total"], 1)
        self.assertEqual(st["by_key"].get("unit.test.tag:RuntimeError"), 1)

    def test_diag_throttling_and_suppressed_count(self):
        err_buf = io.StringIO()
        fake_now = [100.0]
        with mock.patch.object(sys, "stderr", err_buf), \
             mock.patch("diag.time.monotonic", side_effect=lambda: fake_now[0]):
            _diag.warn("throttle.tag", ValueError("bad"), interval=30.0)
            fake_now[0] = 105.0
            _diag.warn("throttle.tag", ValueError("bad"), interval=30.0)
            fake_now[0] = 110.0
            _diag.warn("throttle.tag", ValueError("bad"), interval=30.0)
            # 窗口内仅输出 1 行，但计数应为 3
            self.assertEqual(len(err_buf.getvalue().splitlines()), 1)
            self.assertEqual(_diag.stats()["total"], 3)
            # 超过节流窗口后再次输出，携带 suppressed=2
            fake_now[0] = 135.0
            _diag.warn("throttle.tag", ValueError("bad"), interval=30.0)
            lines = err_buf.getvalue().splitlines()
            self.assertEqual(len(lines), 2)
            self.assertIn("[suppressed=2]", lines[1])

    def test_critical_paths_record_diagnostics_instead_of_silent_pass(self):
        err_buf = io.StringIO()

        class _BrokenProc:
            def write(self, addr, data):
                raise OSError("AccessDenied")

        g = type("G", (), {"p": _BrokenProc()})()
        with mock.patch.object(sys, "stderr", err_buf):
            # 1) events_win.scope_copy_out 写回失败不再静默吞掉
            _events.scope_copy_out(g, 0x123456, b"\x00" * 16)
            # 2) turnengine.save_turn_state 写盘失败不再静默吞掉
            import tempfile
            with tempfile.TemporaryDirectory() as td:
                bad_path = os.path.join(td, "state.json")
                with mock.patch("builtins.open", side_effect=PermissionError("readonly")):
                    _te.save_turn_state({"turn": 1}, path=bad_path)

        tags = [e["tag"] for e in _diag.recent(limit=10)]
        self.assertIn("events_win.scope_copy_out", tags)
        self.assertIn("turnengine.save_turn_state", tags)

    def test_armyops_set_order_executing_accepts_hex_string_ptr(self):
        """回归锁死 WP2 诊断日志揪出的隐性 TypeError：set_order_executing 需兼容十六进制字符串指针。"""
        import armyops_win as AW

        calls = []

        class _P:
            base = 0x100000

            def u8(self, addr):
                return 0 if addr == 0x200000 + AW.OI_EXECUTING else 1

        class _G:
            p = _P()

            def call(self, fn, args, **kw):
                calls.append((fn, args))
                return 0

        res = AW.set_order_executing(_G(), "0x200000", False)
        self.assertEqual(res, 0)
        self.assertEqual(calls[0][1], (0x200000, 0, 1))


if __name__ == "__main__":
    unittest.main()
