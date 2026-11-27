"""第 2 步性能优化专项单测（离线零依赖，覆盖 read_scope / 零分配标量读 / numpy 惰性导入）。"""
from __future__ import annotations

from array import array
import ctypes
import os
import struct
import subprocess
import sys
import threading
import types
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CALLING = os.path.join(ROOT, "calling")
MCP_DIR = os.path.join(ROOT, "mcp")
for _d in (CALLING, MCP_DIR, ROOT):
    if _d not in sys.path:
        sys.path.insert(0, _d)

import api
import mem
import objindex
from process import windows as win_be


class _DummyScopedGame(api.HOI4):
    """不连接真实进程的轻量 HOI4 子类，用于验证 read_scope 全部语义。"""

    def __init__(self):
        self._rs_cache = None
        self._rs_depth = 0
        self.calls = {}
        self.version = 1

    def _bump(self, name):
        self.calls[name] = self.calls.get(name, 0) + 1

    def player_tag(self):
        self._bump("player_tag")
        return "GER"

    @api.read_scoped("lines", key_fn=lambda self, tag=None, flag=True: (self._rs_tag_key(tag), bool(flag)))
    def get_lines(self, tag=None, flag=True):
        self._bump(("lines", tag, flag))
        return [{"idx": 0, "ver": self.version, "tags": {"a", "b"}}]

    @api.read_scoped("mutating", key_fn=lambda self: ())
    def read_while_invalidating(self):
        self._bump("mutating")
        # 模拟读方法内部意外触发写操作导致缓存失效
        self.invalidate_read_scope()
        return {"ver": self.version}


class TestReadScope(unittest.TestCase):
    def test_passthrough_outside_scope(self):
        g = _DummyScopedGame()
        r1 = g.get_lines("GER")
        g.version = 2
        r2 = g.get_lines("GER")
        self.assertEqual(r1[0]["ver"], 1)
        self.assertEqual(r2[0]["ver"], 2)
        self.assertEqual(g.calls[("lines", "GER", True)], 2)

    def test_dedupe_tag_normalization_and_param_isolation(self):
        g = _DummyScopedGame()
        with g.read_scope():
            a = g.get_lines(None, flag=True)
            b = g.get_lines("GER", flag=True)
            c = g.get_lines("ger", flag=True)
            d = g.get_lines("GER", flag=False)
            e = g.get_lines("FRA", flag=True)
        self.assertEqual(a, b)
        self.assertEqual(b, c)
        # None / GER / ger 归一化为同一键，仅读 1 次，且 player_tag 也只查 1 次
        self.assertEqual(g.calls.get("player_tag"), 1)
        self.assertEqual(g.calls.get(("lines", None, True)), 1)
        self.assertNotIn(("lines", "GER", True), g.calls)
        # 不同 flag / 不同 tag 各自独立
        self.assertEqual(g.calls.get(("lines", "GER", False)), 1)
        self.assertEqual(g.calls.get(("lines", "FRA", True)), 1)
        self.assertEqual(d[0]["ver"], 1)
        self.assertEqual(e[0]["ver"], 1)
        # 退出作用域后缓存清空
        self.assertIsNone(g._rs_cache)
        self.assertEqual(g._rs_depth, 0)

    def test_clone_isolation_prevents_cache_pollution(self):
        g = _DummyScopedGame()
        with g.read_scope():
            first = g.get_lines("GER")
            first[0]["ver"] = 999
            first[0]["tags"].add("polluted")
            first.append({"extra": True})
            second = g.get_lines("GER")
        self.assertEqual(len(second), 1)
        self.assertEqual(second[0]["ver"], 1)
        self.assertEqual(second[0]["tags"], {"a", "b"})

    def test_reentrant_nesting_and_exception_cleanup(self):
        g = _DummyScopedGame()
        with self.assertRaises(RuntimeError):
            with g.read_scope():
                _ = g.get_lines("GER")
                with g.read_scope():
                    _ = g.get_lines("GER")
                    self.assertEqual(g._rs_depth, 2)
                # 内层退出后外层仍有效，缓存继续命中
                self.assertEqual(g._rs_depth, 1)
                _ = g.get_lines("GER")
                raise RuntimeError("boom")
        self.assertEqual(g.calls[("lines", "GER", True)], 1)
        self.assertIsNone(g._rs_cache)
        self.assertEqual(g._rs_depth, 0)

    def test_invalidate_read_scope_and_mid_call_invalidation(self):
        g = _DummyScopedGame()
        with g.read_scope():
            r1 = g.get_lines("GER")
            g.version = 7
            g.invalidate_read_scope()
            r2 = g.get_lines("GER")
            self.assertEqual(r1[0]["ver"], 1)
            self.assertEqual(r2[0]["ver"], 7)

            # 读方法内部触发 invalidate 时，不将返回值写入新缓存字典
            m1 = g.read_while_invalidating()
            g.version = 8
            m2 = g.read_while_invalidating()
            self.assertEqual(m1["ver"], 7)
            self.assertEqual(m2["ver"], 8)
            self.assertEqual(g.calls["mutating"], 2)

    def test_enter_read_scope_fallback_for_fake_game(self):
        fake = types.SimpleNamespace(tag="GER")
        with api.enter_read_scope(fake):
            self.assertEqual(fake.tag, "GER")

    def test_new_instance_without_init_safe(self):
        g = _DummyScopedGame.__new__(_DummyScopedGame)
        g.calls = {}
        g.version = 5
        # 未初始化 _rs_cache 时直接透传不报错
        self.assertEqual(g.get_lines("GER")[0]["ver"], 5)
        with g.read_scope():
            self.assertEqual(g.get_lines("GER")[0]["ver"], 5)
            g.version = 6
            self.assertEqual(g.get_lines("GER")[0]["ver"], 5)
        self.assertIsNone(g._rs_cache)


@unittest.skipUnless(sys.platform == "win32", "Windows Backend 标量读实测仅在 win32 运行")
class TestZeroAllocWinRead(unittest.TestCase):
    def setUp(self):
        # 仅打开当前测试进程自身的只读句柄（绝不触碰外部进程）
        self.h = win_be.k32.OpenProcess(win_be.PROCESS_VM_READ, False, os.getpid())
        self.assertTrue(self.h)
        self.be = win_be.Backend.__new__(win_be.Backend)
        self.be.h = self.h
        self.be.pid = os.getpid()
        self.be._tls = threading.local()

        self.proc = mem.Proc.__new__(mem.Proc)
        self.proc.be = self.be
        self.proc.pid = os.getpid()
        self.proc.base = 0
        self.proc.maps = []

    def tearDown(self):
        if getattr(self, "h", None):
            win_be.k32.CloseHandle(self.h)
            self.h = None

    def test_scalar_and_small_large_reads_match_exact_bytes(self):
        payload = bytearray(128)
        struct.pack_into("<BH", payload, 0, 0xAB, 0xCDEF)
        struct.pack_into("<Ii", payload, 4, 0xDEADBEEF, -123456789)
        struct.pack_into("<Qq", payload, 16, 0x0123456789ABCDEF, -987654321012345)
        struct.pack_into("<fd", payload, 32, 3.25, -12345.625)
        # 构造一个 MSVC SSO std::string @ +48 (len=7, cap=15, "germany")
        payload[48:55] = b"germany"
        struct.pack_into("<QQ", payload, 48 + 16, 7, 15)
        payload[96:128] = bytes(range(32))

        cbuf = (ctypes.c_char * len(payload)).from_buffer(payload)
        addr = ctypes.addressof(cbuf)

        self.assertEqual(self.proc.u8(addr + 0), 0xAB)
        self.assertEqual(self.proc.u16(addr + 1), 0xCDEF)
        self.assertEqual(self.proc.u32(addr + 4), 0xDEADBEEF)
        self.assertEqual(self.proc.i32(addr + 8), -123456789)
        self.assertEqual(self.proc.u64(addr + 16), 0x0123456789ABCDEF)
        self.assertEqual(self.proc.i64(addr + 24), -987654321012345)
        self.assertAlmostEqual(self.proc.f32(addr + 32), 3.25, places=6)
        self.assertAlmostEqual(self.proc.f64(addr + 36), -12345.625, places=9)
        self.assertEqual(self.proc.ptr(addr + 16), 0x0123456789ABCDEF)
        self.assertEqual(self.proc.stdstring(addr + 48), "germany")

        # 小块 (<=64) 与大块 (>64) 均精确等价
        self.assertEqual(self.be.read(addr, 0), b"")
        self.assertEqual(self.be.read(addr, 48), bytes(payload[:48]))
        self.assertEqual(self.be.read(addr, 128), bytes(payload))

        # 非法地址安全抛 OSError，ptr() 返回 None
        with self.assertRaises(OSError):
            self.proc.u64(0x10)
        self.assertIsNone(self.proc.ptr(0x10))

    def test_proc_override_read_bypasses_fast_scalar(self):
        # 实例 monkey-patch p.read 时必须走自定义 read()，保证离线测试替身兼容
        self.proc.read = lambda a, n: struct.pack("<Q", 0xCAFEBABE12345678)[:n]
        self.assertEqual(self.proc.u64(0x10), 0xCAFEBABE12345678)

        class _SubProc(mem.Proc):
            def read(self, a, n):
                return struct.pack("<i", -42)[:n]

        sp = _SubProc.__new__(_SubProc)
        sp.be = self.be
        self.assertEqual(sp.i32(0x10), -42)

    def test_multithread_tls_isolation(self):
        vals = [0x1000000000000000 + i * 0x111111 for i in range(8)]
        arr = (ctypes.c_uint64 * len(vals))(*vals)
        base = ctypes.addressof(arr)
        errs = []

        def worker(idx):
            try:
                want = vals[idx]
                for _ in range(200):
                    got = self.proc.u64(base + idx * 8)
                    if got != want:
                        errs.append((idx, want, got))
                        return
            except Exception as exc:
                errs.append((idx, repr(exc)))

        ts = [threading.Thread(target=worker, args=(i,)) for i in range(len(vals))]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(errs, [])


class TestLazyNumpyImport(unittest.TestCase):
    def test_import_game_and_server_does_not_load_numpy(self):
        code = (
            "import sys; "
            f"sys.path.insert(0, {CALLING!r}); sys.path.insert(0, {MCP_DIR!r}); "
            "import game, turnengine, server; "
            "print('HAS_NUMPY=' + str('numpy' in sys.modules))"
        )
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
        self.assertIn("HAS_NUMPY=False", r.stdout)

    def test_objindex_to_from_rel_roundtrip_without_numpy(self):
        oi = objindex.ObjectIndex.__new__(objindex.ObjectIndex)
        oi.p = types.SimpleNamespace(base=0x7FF600000000)
        src = {
            0x7FF600001000: array("q", [0x18000000100, 0x18000000200, 0x18000000300]),
            0x7FF600002000: array("q"),
        }
        old_np = objindex._NP
        try:
            objindex._NP = None
            rel = oi._to_rel(src)
            back = oi._from_rel(rel)
            self.assertIsNone(objindex._NP)
            for k, arr in src.items():
                self.assertEqual(list(back[k]), list(arr))
        finally:
            objindex._NP = old_np


if __name__ == "__main__":
    unittest.main()
