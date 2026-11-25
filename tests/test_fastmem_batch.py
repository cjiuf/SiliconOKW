# -*- coding: utf-8 -*-
"""阶段 4 单元测试：hoi4_fastmem 批量结构直读（Rust 原生 vs 纯 Python 回落逐字段一致性）。"""
from __future__ import annotations

import ctypes
import os
import struct
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CALLING = os.path.join(ROOT, "calling")
if CALLING not in sys.path:
    sys.path.insert(0, CALLING)

import fastmem


class _MemProc:
    """基于字典内存页的离线纯 Python 进程替身。"""

    def __init__(self, blobs, pid=4242):
        self.pid = pid
        self.base = 0x7FF600000000
        self.image_size = 0x4000000
        self.be = None
        self._blobs = dict(blobs)

    def read(self, addr, size):
        for b, data in self._blobs.items():
            if b <= addr and addr + size <= b + len(data):
                off = addr - b
                return bytes(data[off:off + size])
        raise OSError(299, f"unreadable {addr:#x}+{size}")

    def try_read(self, addr, size):
        try:
            return self.read(addr, size)
        except OSError:
            return None

    def u64(self, a):
        return struct.unpack("<Q", self.read(a, 8))[0]

    def i64(self, a):
        return struct.unpack("<q", self.read(a, 8))[0]

    def i32(self, a):
        return struct.unpack("<i", self.read(a, 4))[0]


class _SelfWinProc:
    """使用当前进程真实 Win32 句柄的进程适配器（零游戏依赖，直调真实 ReadProcessMemory）。"""

    def __init__(self):
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.GetCurrentProcess.restype = ctypes.c_void_p
        h = int(k32.GetCurrentProcess())

        class _Be:
            pass

        self.be = _Be()
        self.be.h = h
        self.pid = os.getpid()
        self.base = 0
        self.image_size = 0

    def read(self, addr, size):
        if not addr or addr < 0x10000:
            raise OSError(299, f"bad addr {addr:#x}")
        return ctypes.string_at(addr, size)

    def try_read(self, addr, size):
        try:
            return self.read(addr, size)
        except Exception:
            return None

    def u64(self, a):
        return struct.unpack("<Q", self.read(a, 8))[0]

    def i64(self, a):
        return struct.unpack("<q", self.read(a, 8))[0]

    def i32(self, a):
        return struct.unpack("<i", self.read(a, 4))[0]


def _pack_div_blob(
    *,
    vtable=0x7FF601234560,
    handle=(11, 2001),
    order_ptr=0,
    og_ptr=0x20000000,
    prov_ptr=0,
    path_ptr=0,
    path_len=0,
    tmpl_ptr=0x30000000,
    str_raw=22700000,
    org_raw=5340000,
    str_dmg=120000,
    org_dmg=350000,
    oos=2,
):
    buf = bytearray(0x500)
    struct.pack_into("<Q", buf, 0x000, vtable)
    struct.pack_into("<ii", buf, 0x018, handle[0], handle[1])
    struct.pack_into("<Q", buf, 0x020, order_ptr)
    struct.pack_into("<Q", buf, 0x0C0, og_ptr)
    struct.pack_into("<Q", buf, 0x1F0, prov_ptr)
    struct.pack_into("<Q", buf, 0x200, path_ptr)
    struct.pack_into("<i", buf, 0x20C, path_len)
    struct.pack_into("<Q", buf, 0x3B8, tmpl_ptr)
    struct.pack_into("<q", buf, 0x420, str_raw)
    struct.pack_into("<q", buf, 0x428, org_raw)
    struct.pack_into("<q", buf, 0x448, str_dmg)
    struct.pack_into("<q", buf, 0x450, org_dmg)
    struct.pack_into("<i", buf, 0x498, oos)
    return buf


class TestFastmemDivisionsBatch(unittest.TestCase):
    def setUp(self):
        self._old_no_fm = os.environ.get("HOI4_NO_FASTMEM")
        os.environ.pop("HOI4_NO_FASTMEM", None)

    def tearDown(self):
        if self._old_no_fm is None:
            os.environ.pop("HOI4_NO_FASTMEM", None)
        else:
            os.environ["HOI4_NO_FASTMEM"] = self._old_no_fm

    def test_python_fallback_and_edge_cases(self):
        prov1 = bytearray(0x100)
        struct.pack_into("<i", prov1, 0xA4, 6521)
        order_para = bytearray(0x80)
        struct.pack_into("<i", order_para, 0x30, 4)
        path_arr = struct.pack("<3i", 6521, 9496, 3326)

        d1 = _pack_div_blob(
            handle=(7, 101),
            order_ptr=0x50000000,
            prov_ptr=0x40000000,
            path_ptr=0x60000000,
            path_len=3,
            oos=0,
        )
        # 路径长度超限（>=256）应返回空列表；prov_ptr=0 应返回 None
        d2 = _pack_div_blob(
            handle=(7, 102),
            order_ptr=0,
            og_ptr=0,
            prov_ptr=0,
            path_ptr=0x60000000,
            path_len=300,
            oos=5,
        )

        blobs = {
            0x10000000: d1,
            0x10001000: d2,
            0x40000000: prov1,
            0x50000000: order_para,
            0x60000000: path_arr,
        }
        proc = _MemProc(blobs)

        rows = fastmem.read_divisions_batch(
            proc,
            [0x10000000, 0x10001000, 0x0, 0xDEADBEEF],
            off_prov_ptr=0x1F0,
            off_prov_id=0xA4,
            off_path_ptr=0x200,
            off_path_len=0x20C,
            off_org=0x428,
            off_str=0x420,
            off_oos=0x498,
            off_tmpl=0x3B8,
            off_str_dmg=0x448,
            off_org_dmg=0x450,
            off_og=0xC0,
            off_order_ptr=0x20,
            off_order_type=0x30,
            off_handle=0x18,
        )
        self.assertEqual(len(rows), 4)
        self.assertIsNotNone(rows[0])
        self.assertEqual(rows[0]["prov"], 6521)
        self.assertEqual(rows[0]["path"], [6521, 9496, 3326])
        self.assertEqual(rows[0]["order_type"], 4)
        self.assertEqual(rows[0]["handle"], [7, 101])
        self.assertEqual(rows[0]["org_raw"], 5340000)
        self.assertEqual(rows[0]["str_raw"], 22700000)

        self.assertIsNotNone(rows[1])
        self.assertIsNone(rows[1]["prov"])
        self.assertEqual(rows[1]["path"], [])
        self.assertIsNone(rows[1]["order_type"])
        self.assertEqual(rows[1]["og"], 0)
        self.assertEqual(rows[1]["oos"], 5)

        self.assertIsNone(rows[2])
        self.assertIsNone(rows[3])

    @unittest.skipUnless(
        sys.platform == "win32"
        and fastmem.AVAILABLE
        and not fastmem._disabled_by_env()
        and hasattr(fastmem._MOD, "read_divisions_batch"),
        "需要 Windows 编译版 hoi4_fastmem 0.2.0+",
    )
    def test_rust_vs_python_parity_self_process_parallel(self):
        """在真实 Win32 本进程构造 160 师（跨 Rayon 并行阈值 128），验证 Rust 与 Python 100% 逐字段相等。"""
        self.assertTrue(hasattr(fastmem._MOD, "read_divisions_batch"))
        proc = _SelfWinProc()

        # 准备 16 个共享省份（测去重）、4 个共享命令、1 条路径
        keep_refs = []
        prov_addrs = []
        for idx in range(16):
            pb = (ctypes.c_ubyte * 0x100)()
            addr = ctypes.addressof(pb)
            ctypes.memmove(addr + 0xA4, struct.pack("<i", 1000 + idx), 4)
            keep_refs.append(pb)
            prov_addrs.append(addr)

        ord_addrs = []
        for idx in range(4):
            ob = (ctypes.c_ubyte * 0x80)()
            addr = ctypes.addressof(ob)
            ctypes.memmove(addr + 0x30, struct.pack("<i", idx + 1), 4)
            keep_refs.append(ob)
            ord_addrs.append(addr)

        path_raw = struct.pack("<4i", 1001, 1002, 1003, 1004)
        path_buf = (ctypes.c_ubyte * len(path_raw))(*path_raw)
        path_addr = ctypes.addressof(path_buf)
        keep_refs.append(path_buf)

        div_ptrs = []
        for i in range(160):
            blob = _pack_div_blob(
                vtable=0x7FF610000000 + i * 8,
                handle=(52, 5000 + i),
                order_ptr=ord_addrs[i % 4] if (i % 3 == 0) else 0,
                og_ptr=(0x20000000 + (i % 5) * 0x100) if (i % 7 != 0) else 0,
                prov_ptr=prov_addrs[i % 16] if (i % 19 != 0) else 0,
                path_ptr=path_addr if (i % 4 == 0) else 0,
                path_len=4 if (i % 4 == 0) else 0,
                tmpl_ptr=0x30000000 + (i % 6) * 0x80,
                str_raw=20000000 + i * 1000,
                org_raw=4000000 + i * 500,
                str_dmg=i * 100,
                org_dmg=i * 200,
                oos=i % 9,
            )
            cb = (ctypes.c_ubyte * len(blob))(*blob)
            keep_refs.append(cb)
            div_ptrs.append(ctypes.addressof(cb))

        # 混入 0 与非法指针
        div_ptrs.extend([0, 0x40])

        kw = dict(
            off_prov_ptr=0x1F0,
            off_prov_id=0xA4,
            off_path_ptr=0x200,
            off_path_len=0x20C,
            off_org=0x428,
            off_str=0x420,
            off_oos=0x498,
            off_tmpl=0x3B8,
            off_str_dmg=0x448,
            off_org_dmg=0x450,
            off_og=0xC0,
            off_order_ptr=0x20,
            off_order_type=0x30,
            off_handle=0x18,
        )
        rust_rows = fastmem.read_divisions_batch(proc, div_ptrs, use_rust=True, **kw)
        py_rows = fastmem.read_divisions_batch(proc, div_ptrs, use_rust=False, **kw)
        self.assertEqual(rust_rows, py_rows)
        self.assertEqual(len(rust_rows), 162)
        self.assertIsNone(rust_rows[-1])
        self.assertIsNone(rust_rows[-2])

        # 验证 HOI4_NO_FASTMEM=1 禁用原生路径但返回完全一致结果
        os.environ["HOI4_NO_FASTMEM"] = "1"
        self.assertEqual(fastmem._proc_handle(proc), 0)
        env_rows = fastmem.read_divisions_batch(proc, div_ptrs, use_rust=True, **kw)
        self.assertEqual(env_rows, rust_rows)

        # 保持底层 ctypes 缓冲存活至断言结束
        self.assertTrue(len(keep_refs) > 0)


class TestFastmemStateProvincesBatch(unittest.TestCase):
    def setUp(self):
        self._old_no_fm = os.environ.get("HOI4_NO_FASTMEM")
        os.environ.pop("HOI4_NO_FASTMEM", None)

    def tearDown(self):
        if self._old_no_fm is None:
            os.environ.pop("HOI4_NO_FASTMEM", None)
        else:
            os.environ["HOI4_NO_FASTMEM"] = self._old_no_fm

    def test_python_fallback_and_win_controller_map(self):
        import mapdata

        # 构造 2 个州：州 10 (ctrl=1 GER, 含省 101/102)、州 20 (ctrl=0 回退 owner=2 FRA, 含省 201) + 1 个空州指针
        p101 = bytearray(0x100); struct.pack_into("<i", p101, 0xA4, 101)
        p102 = bytearray(0x100); struct.pack_into("<i", p102, 0xA4, 102)
        p201 = bytearray(0x100); struct.pack_into("<i", p201, 0xA4, 201)

        pa10 = struct.pack("<3Q", 0x40000000, 0x0, 0x40001000)  # 夹 1 个空省指针
        pa20 = struct.pack("<1Q", 0x40002000)

        st10 = bytearray(0x100)
        struct.pack_into("<i", st10, 0x58, 10)
        struct.pack_into("<i", st10, 0xC8, 2)
        struct.pack_into("<i", st10, 0xCC, 1)
        struct.pack_into("<Q", st10, 0x18, 0x30000000)
        struct.pack_into("<i", st10, 0x24, 3)

        st20 = bytearray(0x100)
        struct.pack_into("<i", st20, 0x58, 20)
        struct.pack_into("<i", st20, 0xC8, 2)
        struct.pack_into("<i", st20, 0xCC, 0)  # controller=0 -> 回退 owner=2
        struct.pack_into("<Q", st20, 0x18, 0x30001000)
        struct.pack_into("<i", st20, 0x24, 1)

        state_arr = struct.pack("<3Q", 0x20000000, 0x0, 0x20001000)
        gs_blob = bytearray(0x300)
        struct.pack_into("<Q", gs_blob, 0x2C8, 0x10000000)
        struct.pack_into("<i", gs_blob, 0x2C8 + 0x0C, 3)

        proc = _MemProc({
            0x08000000: gs_blob,
            0x10000000: state_arr,
            0x20000000: st10,
            0x20001000: st20,
            0x30000000: pa10,
            0x30001000: pa20,
            0x40000000: p101,
            0x40001000: p102,
            0x40002000: p201,
        })

        rows = fastmem.read_state_provinces_batch(
            proc,
            0x10000000,
            3,
            off_state_id=0x58,
            off_owner=0xC8,
            off_controller=0xCC,
            off_prov_arr=0x18,
            off_prov_count=0x24,
            off_prov_id=0xA4,
        )
        self.assertEqual(
            rows,
            [
                (0x20000000, 10, 2, 1, [101, 102]),
                (0x20001000, 20, 2, 0, [201]),
            ],
        )

        class _FakeGame:
            p = proc
            gamestate = 0x08000000
            _tags = ["---", "GER", "FRA"]
            _tag2idx = {0: 0, 1: 1, 2: 2}

            def _load_tags(self):
                pass

        ctrl = mapdata._win_controller_map(
            _FakeGame(),
            divisions=[{"prov": 201, "tag": "GER"}],
        )
        self.assertEqual(ctrl, {101: "GER", 102: "GER", 201: "GER"})

    @unittest.skipUnless(
        sys.platform == "win32"
        and fastmem.AVAILABLE
        and not fastmem._disabled_by_env()
        and hasattr(fastmem._MOD, "read_state_provinces_batch"),
        "需要 Windows 编译版 hoi4_fastmem 0.2.0+",
    )
    def test_rust_vs_python_parity_self_process_states_parallel(self):
        """在真实 Win32 本进程构造 80 州 x 10 省 = 800 省（跨 Rayon 两级并行阈值），验证 100% 逐省一致。"""
        self.assertTrue(hasattr(fastmem._MOD, "read_state_provinces_batch"))
        proc = _SelfWinProc()
        keep_refs = []

        state_ptrs = []
        next_pid = 1
        for s_idx in range(80):
            if s_idx == 17:
                # 混入 1 个空州指针
                state_ptrs.append(0)
                continue
            prov_ptrs = []
            for _ in range(10):
                pb = (ctypes.c_ubyte * 0x100)()
                p_addr = ctypes.addressof(pb)
                ctypes.memmove(p_addr + 0xA4, struct.pack("<i", next_pid), 4)
                next_pid += 1
                keep_refs.append(pb)
                prov_ptrs.append(p_addr)
            pa_raw = struct.pack("<%dQ" % len(prov_ptrs), *prov_ptrs)
            pa_buf = (ctypes.c_ubyte * len(pa_raw))(*pa_raw)
            keep_refs.append(pa_buf)

            st_buf = (ctypes.c_ubyte * 0x100)()
            st_addr = ctypes.addressof(st_buf)
            ctypes.memmove(st_addr + 0x58, struct.pack("<i", s_idx + 1), 4)
            ctypes.memmove(st_addr + 0xC8, struct.pack("<i", (s_idx % 5) + 1), 4)
            ctypes.memmove(st_addr + 0xCC, struct.pack("<i", 0 if (s_idx % 9 == 0) else (s_idx % 7) + 1), 4)
            ctypes.memmove(st_addr + 0x18, struct.pack("<Q", ctypes.addressof(pa_buf)), 8)
            ctypes.memmove(st_addr + 0x24, struct.pack("<i", len(prov_ptrs)), 4)
            keep_refs.append(st_buf)
            state_ptrs.append(st_addr)

        sa_raw = struct.pack("<%dQ" % len(state_ptrs), *state_ptrs)
        sa_buf = (ctypes.c_ubyte * len(sa_raw))(*sa_raw)
        keep_refs.append(sa_buf)
        sa_addr = ctypes.addressof(sa_buf)

        kw = dict(
            off_state_id=0x58,
            off_owner=0xC8,
            off_controller=0xCC,
            off_prov_arr=0x18,
            off_prov_count=0x24,
            off_prov_id=0xA4,
        )
        rust_res = fastmem.read_state_provinces_batch(proc, sa_addr, len(state_ptrs), use_rust=True, **kw)
        py_res = fastmem.read_state_provinces_batch(proc, sa_addr, len(state_ptrs), use_rust=False, **kw)
        self.assertEqual(rust_res, py_res)
        self.assertEqual(len(rust_res), 79)
        self.assertEqual(sum(len(r[4]) for r in rust_res), 790)
        self.assertTrue(len(keep_refs) > 0)


class TestFastmemTempleditScan(unittest.TestCase):
    def setUp(self):
        self._old_no_fm = os.environ.get("HOI4_NO_FASTMEM")
        os.environ.pop("HOI4_NO_FASTMEM", None)

    def tearDown(self):
        if self._old_no_fm is None:
            os.environ.pop("HOI4_NO_FASTMEM", None)
        else:
            os.environ["HOI4_NO_FASTMEM"] = self._old_no_fm

    @unittest.skipUnless(sys.platform == "win32" and fastmem.AVAILABLE, "需要 Windows 编译版 hoi4_fastmem.pyd")
    def test_templedit_scan_pool_range_rust_vs_python_self_process(self):
        """验证 templedit._scan_pool_range 的 SSO 短名、堆长名、非法名与重复名在 Rust/Python 路径完全一致。"""
        import templedit

        proc = _SelfWinProc()
        keep_refs = []

        # 构造一块 8KB 内存，包含 8 字节对齐的若干伪 CSubUnitDefinition 实例
        arena = (ctypes.c_ubyte * 8192)()
        raw_base = ctypes.addressof(arena)
        aligned = raw_base + ((8 - (raw_base % 8)) % 8)
        keep_refs.append(arena)

        # 使用进程内唯一标记虚表值，避免意外撞上当前 Python 堆上其他数据
        unique_vt = 0x7FF6CAFEBABE1238

        def _write_subunit(off, key_str):
            b = key_str.encode("utf-8")
            ctypes.memmove(aligned + off, struct.pack("<Q", unique_vt), 8)
            if len(b) <= 15:
                padded = b.ljust(16, b"\0")
                ctypes.memmove(aligned + off + 0x18, padded, 16)
                ctypes.memmove(aligned + off + 0x28, struct.pack("<QQ", len(b), 15), 16)
            else:
                sb = (ctypes.c_ubyte * (len(b) + 1))(*(b + b"\0"))
                keep_refs.append(sb)
                ctypes.memmove(aligned + off + 0x18, struct.pack("<Q", ctypes.addressof(sb)), 8)
                ctypes.memmove(aligned + off + 0x28, struct.pack("<QQ", len(b), len(b)), 16)

        _write_subunit(0x000, "infantry")                 # SSO 短名 (8B)
        _write_subunit(0x080, "artillery_briga")          # SSO 临界 (15B)
        _write_subunit(0x100, "logistics_company")        # 堆长名 (17B)
        _write_subunit(0x180, "signal_company_extra")     # 堆长名 (20B)
        _write_subunit(0x200, "---")                      # 非法 key，必须过滤
        _write_subunit(0x280, "infantry")                 # 重复 key，必须保留首个地址 0x000

        proc.regions = lambda writable=True: [{"start": aligned, "end": aligned + 0x1000}]
        needle = struct.pack("<Q", unique_vt)

        # 1) Rust 路径（带 [lo, hi] 区间裁剪）
        rust_found = templedit._scan_pool_range(proc, needle, lo=aligned, hi=aligned + 0x1000)

        # 2) 纯 Python 路径（通过 HOI4_NO_FASTMEM=1 强制降级）
        os.environ["HOI4_NO_FASTMEM"] = "1"
        py_found = templedit._scan_pool_range(proc, needle, lo=aligned, hi=aligned + 0x1000)

        self.assertEqual(rust_found, py_found)
        self.assertEqual(
            set(rust_found.keys()),
            {"infantry", "artillery_briga", "logistics_company", "signal_company_extra"},
        )
        self.assertEqual(rust_found["infantry"], aligned + 0x000)
        self.assertTrue(len(keep_refs) > 0)


class TestFastmemWiringIntegration(unittest.TestCase):
    """验证热路径接线层（front_state / unitorders / mapdata）在真 Win32 句柄下 Rust vs Python 一致及异常降级。"""

    def setUp(self):
        self._old_no_fm = os.environ.get("HOI4_NO_FASTMEM")
        os.environ.pop("HOI4_NO_FASTMEM", None)

    def tearDown(self):
        if self._old_no_fm is None:
            os.environ.pop("HOI4_NO_FASTMEM", None)
        else:
            os.environ["HOI4_NO_FASTMEM"] = self._old_no_fm

    @unittest.skipUnless(sys.platform == "win32" and fastmem.AVAILABLE, "需要 Windows 编译版 hoi4_fastmem.pyd")
    def test_front_state_enemy_presence_and_fallback_guard(self):
        from unittest import mock
        import divreserve
        import mapdata
        import unitorders
        from micro import front_state

        proc = _SelfWinProc()
        keep_refs = []

        # 构造 1 省 + 1 伞兵命令 + 1 路径 + 2 师 + 1 国家 CPdxArray<CArmy*>
        pb = (ctypes.c_ubyte * 0x100)()
        ctypes.memmove(ctypes.addressof(pb) + 0xA4, struct.pack("<i", 6521), 4)
        keep_refs.append(pb)

        ob = (ctypes.c_ubyte * 0x80)()
        ctypes.memmove(ctypes.addressof(ob) + 0x30, struct.pack("<i", 4), 4)
        keep_refs.append(ob)

        path_raw = struct.pack("<2i", 6521, 9496)
        pbuf = (ctypes.c_ubyte * len(path_raw))(*path_raw)
        keep_refs.append(pbuf)

        b1 = _pack_div_blob(
            prov_ptr=ctypes.addressof(pb),
            order_ptr=ctypes.addressof(ob),
            path_ptr=ctypes.addressof(pbuf),
            path_len=2,
            oos=1,
            og_ptr=0,
        )
        b2 = _pack_div_blob(
            prov_ptr=ctypes.addressof(pb),
            order_ptr=0,
            path_ptr=0,
            path_len=0,
            oos=3,
            og_ptr=0x20000000,
        )
        c1 = (ctypes.c_ubyte * len(b1))(*b1)
        c2 = (ctypes.c_ubyte * len(b2))(*b2)
        keep_refs.extend([c1, c2])

        arr_raw = struct.pack("<2Q", ctypes.addressof(c1), ctypes.addressof(c2))
        arr_buf = (ctypes.c_ubyte * len(arr_raw))(*arr_raw)
        keep_refs.append(arr_buf)

        co_buf = (ctypes.c_ubyte * 0x400)()
        co_addr = ctypes.addressof(co_buf)
        ctypes.memmove(co_addr + 0x290, struct.pack("<Qii", ctypes.addressof(arr_buf), 2, 2), 16)
        keep_refs.append(co_buf)

        class _C:
            ptr = co_addr

        class _G:
            p = proc

            def _c(self, _tag):
                return _C()

            def country(self, _tag):
                return _C()

        g = _G()
        # 1) Rust 路径 vs HOI4_NO_FASTMEM=1 纯 Python 路径逐字段一致
        with mock.patch.object(divreserve, "template_name", return_value="Infanterie"):
            r_rust = front_state._scan_country_locked(g, "GER")
            ep_rust = unitorders.enemy_presence(g, ["GER"])

            os.environ["HOI4_NO_FASTMEM"] = "1"
            r_py = front_state._scan_country_locked(g, "GER")
            ep_py = unitorders.enemy_presence(g, ["GER"])
            os.environ.pop("HOI4_NO_FASTMEM", None)

        self.assertEqual(r_rust, r_py)
        self.assertEqual(ep_rust, ep_py)
        self.assertTrue(r_rust[0]["reserved"])
        self.assertEqual(r_rust[0]["reserved_reason"], "order:paradrop")
        self.assertFalse(r_rust[1]["reserved"])

        # 2) 批量调用异常时自动降级回逐项扫描，绝不整帧崩溃
        with mock.patch.object(divreserve, "template_name", return_value="Infanterie"), \
             mock.patch.object(fastmem, "read_divisions_batch", side_effect=RuntimeError("inject_boom")):
            r_fb = front_state._scan_country_locked(g, "GER")
        self.assertEqual(r_fb, r_py)

        # 3) _win_controller_map 批读异常时自动降级回逐州循环
        st_buf = (ctypes.c_ubyte * 0x100)()
        st_addr = ctypes.addressof(st_buf)
        pa_raw = struct.pack("<Q", ctypes.addressof(pb))
        pa_buf = (ctypes.c_ubyte * len(pa_raw))(*pa_raw)
        ctypes.memmove(st_addr + 0xCC, struct.pack("<i", 1), 4)
        ctypes.memmove(st_addr + 0x18, struct.pack("<Qii", ctypes.addressof(pa_buf), 1, 1), 16)
        sa_raw = struct.pack("<Q", st_addr)
        sa_buf = (ctypes.c_ubyte * len(sa_raw))(*sa_raw)
        gs_buf = (ctypes.c_ubyte * 0x300)()
        ctypes.memmove(ctypes.addressof(gs_buf) + 0x2C8, struct.pack("<Qii", ctypes.addressof(sa_buf), 1, 1), 16)
        keep_refs.extend([st_buf, pa_buf, sa_buf, gs_buf])

        class _MapGame:
            p = proc
            gamestate = ctypes.addressof(gs_buf)
            _tags = ["---", "GER"]
            _tag2idx = {0: 0, 1: 1}

            def _load_tags(self):
                pass

        with mock.patch.object(fastmem, "read_state_provinces_batch", side_effect=RuntimeError("inject_boom")):
            ctrl_fb = mapdata._win_controller_map(_MapGame())
        self.assertEqual(ctrl_fb, {6521: "GER"})
        self.assertTrue(len(keep_refs) > 0)


if __name__ == "__main__":
    unittest.main()
