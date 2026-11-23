"""Rust PyO3 扫描扩展 `hoi4_fastmem` 与 `calling/fastmem.py` 回归测试。

特点：无需游戏进程也能直接用当前 Python 进程自身句柄（`GetCurrentProcess`）
对本进程内的真实 `ctypes` 内存缓冲区执行端到端 Win32 `ReadProcessMemory` 校验！

运行：python research/windows/test_fastmem_20261122.py
"""
import ctypes
import os
import struct
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "calling"))

import fastmem


def test_bridge_graceful_fallback():
    """无有效 Win32 句柄时（测试替身或非 Windows）应返回 None，无缝回落至 Python 路径。"""
    class _MockProc:
        base = 0x7FF600000000
        image_size = 0x1000000
        be = None

    p = _MockProc()
    assert fastmem.scan_vtables_arrays(p, [{"start": 0x1000, "end": 0x2000}], [0x1000]) is None
    assert fastmem.scan_vptrs_flat(p, [{"start": 0x1000, "end": 0x2000}], [0x1000]) is None
    assert fastmem.scan_i32_range(p, [{"start": 0x1000, "end": 0x2000}], 1, 10) is None
    assert fastmem.read_batch(p, [(0x1000, 8)]) is None
    assert fastmem.enum_regions(p) is None


def test_native_win32_self_process():
    """用当前进程自身句柄真调 Win32 ReadProcessMemory / VirtualQueryEx 校验 5 个导出原语。"""
    if sys.platform != "win32" or not fastmem.AVAILABLE:
        print("SKIP test_native_win32_self_process (fastmem not compiled)")
        return

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.GetCurrentProcess.restype = ctypes.c_void_p
    h = int(k32.GetCurrentProcess())

    class _SelfBe:
        def __init__(self, handle):
            self.h = handle

    class _SelfProc:
        def __init__(self, handle):
            self.be = _SelfBe(handle)
            self.base = 0
            self.image_size = 0

    proc = _SelfProc(h)

    # 1. enum_regions 应能读到本进程大量 MEM_PRIVATE 可写区
    regs = fastmem.enum_regions(proc, writable=True, heap_only=True, merge_adjacent=True)
    assert isinstance(regs, list) and len(regs) > 10, len(regs)

    # 2. 在本进程内存构造一个 64KB 缓冲，填入虚表指针与 i32 测试值
    raw_buf = (ctypes.c_ubyte * 65536)()
    base_addr = ctypes.addressof(raw_buf)
    # 保证从 8 字节对齐处开始写
    pad = (8 - (base_addr % 8)) % 8
    aligned_base = base_addr + pad

    vt1 = 0x7FF610001000
    vt2 = 0x7FF610002080
    vt_far = 0x7FF680000000  # 跨度 > 64MB，顺带测 Sorted 回落路径！

    def _put_u64(off, val):
        b = struct.pack("<Q", val)
        ctypes.memmove(aligned_base + off, b, 8)

    def _put_i32(off, val):
        b = struct.pack("<i", val)
        ctypes.memmove(aligned_base + off, b, 4)

    _put_u64(0x000, vt1)
    _put_u64(0x013, vt2)   # 未对齐伪命中，必须被忽略！
    _put_u64(0x020, vt2)
    _put_u64(0x400, vt1)
    _put_u64(0x800, vt_far)

    _put_i32(0x1000, 16967986)
    _put_i32(0x1004, 16968000)
    _put_i32(0x1008, 12345)

    test_reg = [{"start": aligned_base, "end": aligned_base + 0x2000}]

    # 3. scan_vtables_arrays（Bitset 路径：vt1 + vt2 跨度 < 64MB）
    res_bs = fastmem.scan_vtables_arrays(proc, test_reg, [vt1, vt2], max_per_class=100)
    assert list(res_bs[vt1]) == [aligned_base + 0x000, aligned_base + 0x400], res_bs
    assert list(res_bs[vt2]) == [aligned_base + 0x020], res_bs

    # 4. scan_vtables_arrays（Sorted 回落路径：含 vt_far 跨度 > 64MB）
    res_sorted = fastmem.scan_vtables_arrays(proc, test_reg, [vt1, vt2, vt_far], max_per_class=100)
    assert list(res_sorted[vt_far]) == [aligned_base + 0x800], res_sorted
    assert list(res_sorted[vt1]) == [aligned_base + 0x000, aligned_base + 0x400]

    # 5. scan_vptrs_flat（单目标 SIMD 路径 & 多目标路径）
    flat1 = fastmem.scan_vptrs_flat(proc, test_reg, [vt1], max_hits=100)
    assert flat1 == [aligned_base + 0x000, aligned_base + 0x400], flat1
    flat_all = fastmem.scan_vptrs_flat(proc, test_reg, [vt1, vt2], max_hits=100)
    assert flat_all == [aligned_base + 0x000, aligned_base + 0x020, aligned_base + 0x400], flat_all

    # 6. scan_i32_range
    i32_hits = fastmem.scan_i32_range(proc, test_reg, 16967900, 16968100, max_hits=100)
    assert i32_hits == [aligned_base + 0x1000, aligned_base + 0x1004], i32_hits

    # 7. read_batch
    batch = fastmem.read_batch(proc, [
        (aligned_base + 0x000, 8),
        (aligned_base + 0x1000, 4),
        (0x10, 8),  # 非法地址应返回 None 而非崩溃
    ])
    assert len(batch) == 3
    assert struct.unpack("<Q", batch[0])[0] == vt1
    assert struct.unpack("<i", batch[1])[0] == 16967986
    assert batch[2] is None


def main():
    tests = [
        test_bridge_graceful_fallback,
        test_native_win32_self_process,
    ]
    for fn in tests:
        fn()
        print("OK  ", fn.__name__)
    print("\n%d/%d passed (fastmem.AVAILABLE=%s, VERSION=%s, THREADS=%s)"
          % (len(tests), len(tests), fastmem.AVAILABLE, fastmem.VERSION, fastmem.THREADS))
    return 0


if __name__ == "__main__":
    sys.exit(main())
