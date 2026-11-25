# -*- coding: utf-8 -*-
"""阶段 3 WP0 单元测试：WinCommandPipeline 与 PE 精确版本安全门禁（纯离线零注入）。"""
import os
import struct
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CALLING = os.path.join(ROOT, "calling")
if CALLING not in sys.path:
    sys.path.insert(0, CALLING)

import api as _api
import cmdwin as _cmdwin
import offsets_win as _OW
import profile as _profile
import session as _session
import win_pipeline as _wp


def _build_pe_header(timestamp: int, size_of_image: int, e_lfanew: int = 0x150) -> bytes:
    """构造可供 profile.parse_pe_header 解析的最小合法 PE64 头部字节。"""
    buf = bytearray(4096)
    buf[0:2] = b"MZ"
    struct.pack_into("<I", buf, 0x3C, e_lfanew)
    buf[e_lfanew:e_lfanew + 4] = b"PE\x00\x00"
    # COFF FileHeader: TimeDateStamp 在 +0x08
    struct.pack_into("<I", buf, e_lfanew + 8, int(timestamp))
    # OptionalHeader64 magic=0x20B 在 +0x18，SizeOfImage 在 +0x18 + 0x38 = +0x50
    struct.pack_into("<H", buf, e_lfanew + 0x18, 0x20B)
    struct.pack_into("<I", buf, e_lfanew + 0x50, int(size_of_image))
    return bytes(buf)


class _MockLiveProc:
    """模拟带有真实 PE 头部的活进程（带 _enforce_version_gate=True）。"""

    def __init__(self, pe_bytes: bytes, pid: int = 42424, base: int = 0x7FF610000000):
        self.pid = pid
        self.base = base
        self._pe_bytes = pe_bytes
        self._enforce_version_gate = True
        self.read_calls = 0
        self.writes = []

    def read(self, addr, n):
        if addr == self.base and n == 4096:
            self.read_calls += 1
            return self._pe_bytes
        return b"\x00" * n

    def write(self, addr, data):
        self.writes.append((addr, data))

    def u64(self, addr):
        if addr == self.base + _OW.G_GAMESTATE:
            return 0x12340000
        if addr == self.base + _OW.G_IDLER:
            return 0x12350000
        return 0

    def i32(self, addr):
        return 2

    def u8(self, addr):
        return 1


class TestWinPipelineAndVersionGate(unittest.TestCase):
    def setUp(self):
        self._old_env = os.environ.get("HOI4_ALLOW_UNVERIFIED_VERSION")
        os.environ.pop("HOI4_ALLOW_UNVERIFIED_VERSION", None)
        _wp.clear_version_cache()
        prof = _profile.load()
        self.assertIsNotNone(prof, "默认 profile 应当可加载")
        pe = prof["pe"]
        self.good_ts = int(pe["timestamp"])
        self.good_sz = int(pe["size_of_image"])
        self.good_hdr = _build_pe_header(self.good_ts, self.good_sz)
        self.bad_hdr = _build_pe_header(self.good_ts + 1, self.good_sz)

    def tearDown(self):
        if self._old_env is None:
            os.environ.pop("HOI4_ALLOW_UNVERIFIED_VERSION", None)
        else:
            os.environ["HOI4_ALLOW_UNVERIFIED_VERSION"] = self._old_env
        _wp.clear_version_cache()

    def test_exact_match_passes_and_caches(self):
        proc = _MockLiveProc(self.good_hdr)
        r1 = _wp.ensure_live_version(proc, check_pe=True)
        r2 = _wp.ensure_live_version(proc, check_pe=True)
        self.assertTrue(r1["ok"] and r1["status"] == "exact")
        self.assertEqual(r1, r2)
        self.assertEqual(proc.read_calls, 1, "同一 (pid, base) 应缓存 exact 核验结果")

        # PID 变化后应重新读取 PE 头校验
        proc.pid = 42425
        _wp.ensure_live_version(proc, check_pe=True)
        self.assertEqual(proc.read_calls, 2)

    def test_mismatch_fails_closed(self):
        proc = _MockLiveProc(self.bad_hdr)
        with self.assertRaises(_wp.VersionMismatchError) as ctx:
            _wp.ensure_live_version(proc, check_pe=True)
        self.assertEqual(ctx.exception.verify_result.get("status"), "mismatch")
        self.assertFalse(ctx.exception.verify_result.get("ok"))

    def test_invalid_header_fails_closed(self):
        proc = _MockLiveProc(b"\x00" * 4096)
        with self.assertRaises(_wp.VersionMismatchError) as ctx:
            _wp.ensure_live_version(proc, check_pe=True)
        self.assertEqual(ctx.exception.verify_result.get("status"), "invalid_pe_header")

    def test_env_override_allows_bypass_with_warning(self):
        proc = _MockLiveProc(self.bad_hdr)
        os.environ["HOI4_ALLOW_UNVERIFIED_VERSION"] = "1"
        r = _wp.ensure_live_version(proc, check_pe=True)
        self.assertTrue(r["ok"])
        self.assertTrue(r.get("override"))
        self.assertEqual(r.get("status"), "env_override")

    def test_hoi4_attach_blocks_before_lock_or_injector(self):
        """版本不匹配时 HOI4.attach 必须在拿锁与创建 Injector 之前直接拦截。"""
        h = object.__new__(_api.HOI4)
        h.p = _MockLiveProc(self.bad_hdr)
        h._depth = 0
        h._inj = None
        h._lock = None

        with mock.patch("api.Injector") as mock_inj, \
             mock.patch("xlock.FileLock") as mock_lock:
            with self.assertRaises(_wp.VersionMismatchError):
                h.attach()
            mock_lock.assert_not_called()
            mock_inj.assert_not_called()
            self.assertEqual(h._depth, 0)

    def test_direct_write_paths_blocked_on_mismatch(self):
        """直写内存路径（set_game_speed / session.pause）对版本漂移活进程 fail-closed。"""
        proc = _MockLiveProc(self.bad_hdr)
        g = type("LiveLikeGame", (), {"p": proc})()

        with self.assertRaises(_wp.VersionMismatchError):
            _cmdwin.set_game_speed(g, 3)
        self.assertEqual(proc.writes, [], "被版本门禁拦截后严禁写入任何内存")

        with self.assertRaises(_wp.VersionMismatchError):
            _session.pause(g, paused=True)
        self.assertEqual(proc.writes, [], "被版本门禁拦截后严禁写入暂停字节")

        # 纯只读 get_game_speed 必须保持放行
        self.assertEqual(_cmdwin.get_game_speed(g), 2)

    def test_pipeline_run_lifecycle_and_finally_cleanup(self):
        """验证 WinCommandPipeline.run 的 attach_fake_vtable / CanExecute / dry_run / finally 清理。"""
        proc = _MockLiveProc(self.good_hdr)
        events = []

        class _FakeGame:
            def __init__(self):
                self.p = proc
                self._inj = None
                self.attached = 0
                self.detached = 0

            def attach(self):
                self.attached += 1
                self._inj = object()

            def detach(self):
                self.detached += 1
                self._inj = None

        g = _FakeGame()
        pipe = _wp.WinCommandPipeline(g, check_pe=True)

        # 1) CanExecute 拒绝 -> 不入队，返回 blocked_by
        with mock.patch.object(_cmdwin, "fake_vtable", return_value=0x9000), \
             mock.patch.object(_cmdwin, "can_exec", return_value=False), \
             mock.patch.object(_cmdwin, "post_drained") as m_post:
            r = pipe.run(
                build_fn=lambda _g: 0x5000,
                vt_rva=0x2A11548,
                can_exec_rva=0x11F2000,
            )
            self.assertFalse(r.ok)
            self.assertEqual(r.blocked_by, "can_execute_refused")
            self.assertFalse(r.can_execute)
            m_post.assert_not_called()
            self.assertEqual(g.attached, 1)
            self.assertEqual(g.detached, 1)
            # 验证已挂接 fake_vtable 到 cmd + 0
            self.assertIn((0x5000, struct.pack("<Q", 0x9000)), proc.writes)

        # 2) dry_run=True -> 不入队，返回 can_execute
        with mock.patch.object(_cmdwin, "can_exec", return_value=True), \
             mock.patch.object(_cmdwin, "post_drained") as m_post:
            r = pipe.run(
                build_fn=lambda _g: 0x5000,
                can_exec_rva=0x11F2000,
                dry_run=True,
            )
            self.assertTrue(r.ok)
            self.assertTrue(r.can_execute)
            self.assertEqual(r.posted, 0)
            self.assertTrue(r.extra.get("dry_run"))
            m_post.assert_not_called()

        # 3) post_drained 抛异常时 post_cleanup_hook 与 detach 仍必须执行
        def _pre(_g, _cmd):
            events.append(("pre", _cmd))
            return "orig_scope_bytes"

        def _cleanup(_g, _cmd, ctx):
            events.append(("cleanup", _cmd, ctx))

        with mock.patch.object(_cmdwin, "post_drained", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                pipe.run(
                    build_fn=lambda _g: 0x6000,
                    pre_post_hook=_pre,
                    post_cleanup_hook=_cleanup,
                )
        self.assertEqual(events, [("pre", 0x6000), ("cleanup", 0x6000, "orig_scope_bytes")])
        self.assertEqual(g.attached, g.detached)
        self.assertIsNone(g._inj)

    def test_turnengine_version_guard_blocks_pe_mismatch_before_actions(self):
        """turnengine.execute_plan 应在首个动作前由精确 PE 版本门禁结构化拦截 version_mismatch。"""
        import profile as _prof
        import turnengine as _te

        class _LiveProc:
            pid = 42429
            base = 0x7FF600000000
            _enforce_version_gate = True

            def u64(self, _a):
                # 即便 G_GAMESTATE 看起来在合法堆范围，也必须被 PE 指纹失配拦下！
                return 0x22500000000

            def read(self, _a, size):
                return b"MZ\x90\x00"[:size]

        class _LiveGame:
            p = _LiveProc()
            called = False

            def set_focus(self, *_a, **_kw):
                self.called = True
                return {"ok": True}

        g = _LiveGame()
        plan = {"actions": [{"op": "set_focus", "args": {"key": "GER_rhineland"}}]}
        with mock.patch.object(
            _prof,
            "verify_live_process",
            return_value={
                "status": "mismatch",
                "live": {"timestamp": 0x70000000, "size_of_image": 0x3900000},
                "profile": {"timestamp": 0x69E60681, "size_of_image": 0x38A3000},
            },
        ):
            res = _te.execute_plan(g, plan, dry_run=False, persist_state=False)
        self.assertFalse(res["ok"])
        self.assertEqual(res.get("blocked_by"), "version_mismatch")
        self.assertIn("mismatch", res.get("error", ""))
        self.assertFalse(g.called)

    def test_runtime_selfheal_and_backjump_invalidate_session_state_and_version_cache(self):
        """runtime._selfheal_check 与 _date_backjump 必须联动清空 SessionState 与版本核验缓存。"""
        import session_state as _ss
        mcp_dir = os.path.join(ROOT, "mcp")
        if mcp_dir not in sys.path:
            sys.path.insert(0, mcp_dir)
        from tools import runtime as _rt

        sentinel = {"stale": 1}
        _ss.register_cache("test.runtime_cleanup_sentinel", container=sentinel)
        old_pid, old_start = _rt._selfheal_pid, _rt._selfheal_start
        try:
            class _Proc:
                def __init__(self, pid):
                    self.pid = pid
                    self.base = 0x7FF600000000
                    self._version_check_cache = ((pid, self.base), {"ok": True})

            class _Game:
                _VT_SCAN = {(111, "CFoo"): [0x1000]}

                def __init__(self, pid, raw_date):
                    self.p = _Proc(pid)
                    self._raw = raw_date
                    self._version_check_cache = ((pid, self.p.base), {"ok": True})
                    self._oi = object()
                    self._tags = ["GER"]
                    self._tag2idx = {0: 0}
                    self._date_addr = 0x1234
                    self._cmds = {}
                    self._moddefs = {}

                def date(self):
                    return {"raw": self._raw}

                def reset_front_snapshot(self):
                    pass

                def find_date_address(self, force=False):
                    pass

                def _kick_tech_scan(self):
                    pass

            # 1) 换 PID -> _selfheal_check 联动清空 SessionState 与版本核验缓存
            g1 = _Game(1001, 20000)
            _rt._selfheal_pid, _rt._selfheal_start = 1000, 123
            with mock.patch.object(_rt, "_proc_start_ticks", return_value=456):
                _rt._selfheal_check(g1)
            self.assertEqual(sentinel, {}, "_selfheal_check 必须清空 SessionState 注册缓存")
            self.assertFalse(hasattr(g1, "_version_check_cache"))
            self.assertFalse(hasattr(g1.p, "_version_check_cache"))

            # 2) 同 PID 日期回跳 > 30 天（哪怕 world_ptr 未变）-> _date_backjump 联动清缓存
            sentinel["stale_after_reload"] = 2
            g2 = _Game(1001, 20000)
            _rt._date_backjump(g2)  # 记录初始 raw=20000
            self.assertEqual(sentinel.get("stale_after_reload"), 2)
            g2._raw = 20000 - 40 * 24
            _rt._date_backjump(g2)
            self.assertEqual(sentinel, {}, "_date_backjump 必须清空 SessionState 注册缓存")
            self.assertFalse(hasattr(g2, "_version_check_cache"))
            self.assertFalse(hasattr(g2.p, "_version_check_cache"))
        finally:
            _ss.SESSION.unregister_cache("test.runtime_cleanup_sentinel")
            _rt._selfheal_pid, _rt._selfheal_start = old_pid, old_start

    def test_techids_cache_isolated_outside_calling_and_migrates_legacy(self):
        """ResearchMixin.TECH_IDS_CACHE 必须落在运行态目录，并平滑迁移旧 calling/techids.pkl。"""
        import tempfile
        import safe_cache as _sc
        from domains.research import ResearchMixin

        calling_dir = os.path.normcase(os.path.abspath(CALLING))
        tech_path = os.path.normcase(os.path.abspath(ResearchMixin.TECH_IDS_CACHE))
        self.assertFalse(
            tech_path.startswith(calling_dir + os.sep),
            f"TECH_IDS_CACHE 不应留在 calling/ 源码目录: {ResearchMixin.TECH_IDS_CACHE}",
        )

        with tempfile.TemporaryDirectory() as td:
            legacy = os.path.join(td, "legacy", "techids.pkl")
            modern = os.path.join(td, "state", "techids.json")
            _sc.save(legacy, {"schema": 1, "ids": {1: "basic_machine_tools", 2: "infantry_weapons"}}, fmt="json")

            obj = object.__new__(ResearchMixin)
            obj.TECH_IDS_CACHE = modern
            obj._LEGACY_TECH_IDS_CACHE = legacy

            got = obj._tech_ids_from_disk()
            self.assertEqual(got, {1: "basic_machine_tools", 2: "infantry_weapons"})
            self.assertTrue(os.path.isfile(modern), "读取旧缓存后应自动迁入新运行态路径")


if __name__ == "__main__":
    unittest.main()
