# -*- coding: utf-8 -*-
"""阶段 3 WP1 单元测试：winject 双页分离 W^X 内存布局与页保护状态机验证（纯离线）。"""
import ctypes
import os
import struct
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CALLING = os.path.join(ROOT, "calling")
if CALLING not in sys.path:
    sys.path.insert(0, CALLING)

from process import winject as W


class TestWinjectWX(unittest.TestCase):
    def test_no_rwx_in_source_and_page_separation_invariants(self):
        import ast
        src_path = os.path.join(CALLING, "process", "winject.py")
        with open(src_path, "r", encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), filename=src_path)
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        self.assertNotIn("PAGE_EXECUTE_READWRITE", names, "winject.py AST 不得出现 PAGE_EXECUTE_READWRITE")
        self.assertFalse(hasattr(W, "PAGE_EXECUTE_READWRITE"))
        self.assertEqual(W.PAGE_READWRITE, 0x04)
        self.assertEqual(W.PAGE_EXECUTE_READ, 0x20)
        self.assertEqual(W.STUB_OFF, 0x00)
        self.assertEqual(W.CODE_PAGE_SIZE, 0x1000)
        self.assertEqual(W.DATA_OFF, 0x1000)
        # res/flag/stack 必须全部落在 Page 1 及之后（与 Page 0 代码页物理隔离）
        self.assertGreaterEqual(W.RES_OFF, W.DATA_OFF)
        self.assertGreaterEqual(W.FLAG_OFF, W.DATA_OFF)
        self.assertGreaterEqual(W.STACK_OFF, W.DATA_OFF + 0x1000)
        self.assertEqual(W.PAYLOAD_OFF, W.CONTROL_SIZE)
        self.assertGreater(W.SCRATCH_SIZE - W.PAYLOAD_OFF, 0x10000)

    def test_wx_state_machine_across_multiple_calls(self):
        """使用带严格页保护校验的虚拟内存模型，连续执行两次 call 验证 RW <-> RX 切换与零 RWX。"""
        base_addr = 0x20000000
        mem = bytearray(W.SCRATCH_SIZE)
        # 按 4 KiB 页跟踪权限
        page_prots = {}
        protect_log = []
        flush_log = []

        def _page_range(addr, size):
            start = (addr - base_addr) // W.PAGE_SIZE
            end = (addr - base_addr + size - 1) // W.PAGE_SIZE
            return range(start, end + 1)

        class _FakeK32:
            def OpenProcess(self, access, inherit, pid):
                return 0x1111

            def CloseHandle(self, h):
                return 1

            def VirtualAllocEx(self, h, addr, size, alloc_type, prot):
                # 严禁分配 RWX
                if prot == 0x40:
                    raise AssertionError("forbidden PAGE_EXECUTE_READWRITE allocation")
                if prot != W.PAGE_READWRITE:
                    raise AssertionError("expected initial PAGE_READWRITE, got %#x" % prot)
                for p in range(size // W.PAGE_SIZE):
                    page_prots[p] = prot
                return base_addr

            def VirtualProtectEx(self, h, addr_ptr, size, new_prot, old_prot_ptr):
                addr = addr_ptr.value if hasattr(addr_ptr, "value") else int(addr_ptr)
                if new_prot == 0x40:
                    raise AssertionError("forbidden PAGE_EXECUTE_READWRITE transition")
                pages = list(_page_range(addr, size))
                # 只能切 Page 0 代码页
                assert pages == [0], "VirtualProtectEx touched non-code page: %s" % pages
                old = page_prots[0]
                if old_prot_ptr is not None:
                    old_prot_ptr._obj.value = old
                page_prots[0] = new_prot
                protect_log.append((addr - base_addr, size, old, new_prot))
                return 1

            def VirtualFreeEx(self, h, addr_ptr, size, free_type):
                return 1

            def FlushInstructionCache(self, h, addr_ptr, size):
                addr = addr_ptr.value if hasattr(addr_ptr, "value") else int(addr_ptr)
                flush_log.append((addr - base_addr, size))
                return 1

            def WriteProcessMemory(self, h, addr_ptr, buf, size, written_ptr):
                addr = addr_ptr.value if hasattr(addr_ptr, "value") else int(addr_ptr)
                for p in _page_range(addr, size):
                    if page_prots.get(p) != W.PAGE_READWRITE:
                        raise AssertionError(
                            "WriteProcessMemory AV on page %d (prot=%#x)" % (p, page_prots.get(p, 0))
                        )
                raw = ctypes.string_at(buf, size)
                off = addr - base_addr
                mem[off:off + size] = raw
                if written_ptr is not None:
                    written_ptr._obj.value = size
                return 1

            def ReadProcessMemory(self, h, addr_ptr, buf, size, read_ptr):
                addr = addr_ptr.value if hasattr(addr_ptr, "value") else int(addr_ptr)
                off = addr - base_addr
                ctypes.memmove(buf, bytes(mem[off:off + size]), size)
                if read_ptr is not None:
                    read_ptr._obj.value = size
                return 1

            def OpenThread(self, rights, inherit, tid):
                return 0x2222

            def SuspendThread(self, h):
                return 0

            def ResumeThread(self, h):
                # 线程恢复并执行 stub 时，断言严格 W^X 不变量
                if thread_state["active_call"]:
                    assert page_prots[0] == W.PAGE_EXECUTE_READ, (
                        "code page must be PAGE_EXECUTE_READ when executing, got %#x" % page_prots[0]
                    )
                    for p in range(1, W.SCRATCH_SIZE // W.PAGE_SIZE):
                        assert page_prots[p] == W.PAGE_READWRITE, (
                            "data/stack page %d must stay PAGE_READWRITE, got %#x" % (p, page_prots[p])
                        )
                    # 验证 stub 内嵌的 res/flag 绝对地址落在 Page 1 数据页
                    stub_bytes = bytes(mem[:256])
                    res_target = base_addr + W.RES_OFF
                    flag_target = base_addr + W.FLAG_OFF
                    assert (b"\x48\xa3" + struct.pack("<Q", res_target)) in stub_bytes
                    assert (b"\x48\xb8" + struct.pack("<Q", flag_target)) in stub_bytes
                    # 模拟 stub 执行完毕写 res 与 flag 到 Page 1 数据页
                    ret_val = thread_state["next_ret"]
                    struct.pack_into("<Q", mem, W.RES_OFF, ret_val)
                    struct.pack_into("<I", mem, W.FLAG_OFF, 1)
                    thread_state["active_call"] = False
                return 1

            def GetThreadContext(self, h, ctx_ptr):
                ctypes.memmove(ctx_ptr, bytes(thread_state["ctx"]), W.CTX_SIZE)
                return 1

            def SetThreadContext(self, h, ctx_ptr):
                raw = ctypes.string_at(ctx_ptr, W.CTX_SIZE)
                thread_state["ctx"] = bytearray(raw)
                rip = struct.unpack_from("<Q", raw, W.OFF_RIP)[0]
                if rip == base_addr + W.STUB_OFF:
                    thread_state["active_call"] = True
                return 1

        orig_ctx = bytearray(W.CTX_SIZE)
        struct.pack_into("<Q", orig_ctx, W.OFF_RIP, 0x7FF600123456)
        struct.pack_into("<Q", orig_ctx, W.OFF_RSP, 0x0014FF00)
        thread_state = {"ctx": bytearray(orig_ctx), "active_call": False, "next_ret": 0xCAFEBABE}

        inj = W.Injector(pid=99999, tid=777)
        with mock.patch.object(W, "k32", _FakeK32()), \
             mock.patch.object(W, "focus_game", return_value=True), \
             mock.patch.object(W, "_write_frozen", lambda *a, **k: None), \
             mock.patch.object(W, "clear_frozen", lambda *a, **k: None):
            inj.attach()
            # 在 PAYLOAD_OFF 放置哨兵载荷，验证 call() 不会误擦除载荷区
            sentinel_payload = b"PAYLOAD_INTACT_1234"
            inj._write(base_addr + W.PAYLOAD_OFF, sentinel_payload)

            r1 = inj.call(0x7FF800001000, (1, 2, 3, 4, 5, 6))
            self.assertEqual(r1, 0xCAFEBABE)
            self.assertEqual(page_prots[0], W.PAGE_EXECUTE_READ)
            self.assertEqual(bytes(mem[W.PAYLOAD_OFF:W.PAYLOAD_OFF + len(sentinel_payload)]), sentinel_payload)

            # 第二次调用：Page 0 当前为 RX，必须先切回 RW 写入新 stub，再切回 RX！
            thread_state["next_ret"] = 0x12345678
            r2 = inj.call(0x7FF800002000, (9,))
            self.assertEqual(r2, 0x12345678)
            self.assertEqual(page_prots[0], W.PAGE_EXECUTE_READ)
            self.assertEqual(bytes(mem[W.PAYLOAD_OFF:W.PAYLOAD_OFF + len(sentinel_payload)]), sentinel_payload)

            # 核对 VirtualProtectEx 状态转换序列：
            # 第 1 次：RW -> RX
            # 第 2 次：RX -> RW -> RX
            transitions = [(old, new) for _o, _sz, old, new in protect_log]
            self.assertEqual(transitions, [
                (W.PAGE_READWRITE, W.PAGE_EXECUTE_READ),
                (W.PAGE_EXECUTE_READ, W.PAGE_READWRITE),
                (W.PAGE_READWRITE, W.PAGE_EXECUTE_READ),
            ])
            # 核对 FlushInstructionCache 仅刷 Page 0 代码页
            self.assertEqual(flush_log, [(0, W.CODE_PAGE_SIZE), (0, W.CODE_PAGE_SIZE)])
            inj.detach()
            self.assertIsNone(inj.scratch)
            self.assertIsNone(inj._code_prot)

    @unittest.skipUnless(
        sys.platform == "win32" and os.environ.get("HOI4_TEST_WIN64_WX_LIVE") == "1",
        "仅在 Windows 且显式设置 HOI4_TEST_WIN64_WX_LIVE=1 时运行真实子进程 CPU W^X 硬件测试",
    )
    def test_real_win64_child_process_wx_roundtrip(self):
        import ctypes.wintypes as wt
        import subprocess

        child_code = (
            "import ctypes\n"
            "print(ctypes.windll.kernel32.GetCurrentThreadId(), flush=True)\n"
            "x = 0\n"
            "while True:\n"
            "    x = (x + 1) & 0xFFFF\n"
        )
        proc = subprocess.Popen([sys.executable, "-c", child_code], stdout=subprocess.PIPE, text=True)
        try:
            tid = int(proc.stdout.readline().strip())
            pid = proc.pid
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.GetModuleHandleW.argtypes = [wt.LPCWSTR]
            k32.GetModuleHandleW.restype = ctypes.c_void_p
            k32.GetProcAddress.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
            k32.GetProcAddress.restype = ctypes.c_void_p
            hmod = k32.GetModuleHandleW("kernel32.dll")
            fn_pid = k32.GetProcAddress(hmod, b"GetCurrentProcessId")
            fn_tick = k32.GetProcAddress(hmod, b"GetTickCount64")

            class _MBI(ctypes.Structure):
                _fields_ = [
                    ("BaseAddress", ctypes.c_void_p),
                    ("AllocationBase", ctypes.c_void_p),
                    ("AllocationProtect", wt.DWORD),
                    ("__alignment1", wt.DWORD),
                    ("RegionSize", ctypes.c_size_t),
                    ("State", wt.DWORD),
                    ("Protect", wt.DWORD),
                    ("Type", wt.DWORD),
                    ("__alignment2", wt.DWORD),
                ]

            k32.VirtualQueryEx.argtypes = [
                ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(_MBI), ctypes.c_size_t
            ]
            k32.VirtualQueryEx.restype = ctypes.c_size_t

            def _qprot(h, addr):
                m = _MBI()
                self.assertGreater(
                    k32.VirtualQueryEx(h, ctypes.c_void_p(addr), ctypes.byref(m), ctypes.sizeof(m)),
                    0,
                )
                return m.Protect

            inj = W.Injector(pid, tid=tid)
            try:
                with mock.patch.object(W, "focus_game", return_value=True):
                    inj.attach()
                    self.assertEqual(_qprot(inj.h, inj.scratch + W.STUB_OFF), W.PAGE_READWRITE)
                    self.assertEqual(_qprot(inj.h, inj.scratch + W.DATA_OFF), W.PAGE_READWRITE)
                    r1 = inj.call(fn_pid, ())
                    self.assertEqual(r1, pid)
                    self.assertEqual(_qprot(inj.h, inj.scratch + W.STUB_OFF), W.PAGE_EXECUTE_READ)
                    self.assertEqual(_qprot(inj.h, inj.scratch + W.DATA_OFF), W.PAGE_READWRITE)
                    r2 = inj.call(fn_tick, ())
                    self.assertGreater(r2, 0)
                    self.assertEqual(_qprot(inj.h, inj.scratch + W.STUB_OFF), W.PAGE_EXECUTE_READ)
                    self.assertEqual(_qprot(inj.h, inj.scratch + W.DATA_OFF), W.PAGE_READWRITE)
            finally:
                inj.detach()
            self.assertIsNone(proc.poll())
        finally:
            proc.terminate()
            proc.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
