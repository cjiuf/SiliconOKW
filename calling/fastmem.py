"""Rust 原生内存扫描扩展 `hoi4_fastmem` 的 Python 桥接与自动优雅降级层。

存在 `calling/hoi4_fastmem.pyd`（Windows 编译产物）时自动走多核 Rust 路径；
不存在或在非 Windows 平台时，`AVAILABLE = False`，上层无缝回落至纯 Python/numpy 实现。
"""
from __future__ import annotations

from array import array
import sys

_MOD = None
_LOAD_ERROR = None

if sys.platform == "win32":
    try:
        import hoi4_fastmem as _MOD  # type: ignore
    except Exception as _e:
        _MOD = None
        _LOAD_ERROR = repr(_e)

AVAILABLE = _MOD is not None
VERSION = getattr(_MOD, "VERSION", None) if _MOD is not None else None
THREADS = getattr(_MOD, "THREADS", 0) if _MOD is not None else 0


def _proc_handle(proc):
    """提取真实 Win32 进程句柄（测试替身或非 Windows 返回 0，自动走 Python 回落）。"""
    if not AVAILABLE:
        return 0
    be = getattr(proc, "be", None)
    h = getattr(be, "h", None)
    if isinstance(h, int) and h > 0:
        return h
    return 0


def _region_pairs(region_list):
    return [(int(r["start"]), int(r["end"])) for r in region_list if r["end"] > r["start"]]


def scan_vtables_arrays(proc, region_list, targets, max_per_class=200000, chunk_mb=4):
    """多核全量/多类虚表扫描 -> `{vptr_u64: array('q')}`，不可用时返回 None。"""
    h = _proc_handle(proc)
    if not h:
        return None
    pairs = _region_pairs(region_list)
    if not pairs or not targets:
        return {}
    raw_map = _MOD.scan_vtables(
        h, pairs, [int(t) for t in targets], int(max_per_class), int(chunk_mb)
    )
    out = {}
    for vp, raw_bytes in raw_map.items():
        arr = array("q")
        arr.frombytes(raw_bytes)
        out[int(vp)] = arr
    return out


def scan_vptrs_flat(proc, region_list, targets, max_hits=500000, chunk_mb=4):
    """多核单类/少数类虚表扫描 -> 升序去重地址列表，不可用时返回 None。"""
    h = _proc_handle(proc)
    if not h:
        return None
    pairs = _region_pairs(region_list)
    if not pairs or not targets:
        return []
    return _MOD.scan_vptrs_flat(
        h, pairs, [int(t) for t in targets], int(max_hits), int(chunk_mb)
    )


def scan_i32_range(proc, region_list, lo, hi, max_hits=500000, chunk_mb=4):
    """多核 4 字节对齐 i32 范围扫描 -> 升序地址列表，不可用时返回 None。"""
    h = _proc_handle(proc)
    if not h:
        return None
    pairs = _region_pairs(region_list)
    if not pairs:
        return []
    return _MOD.scan_i32_range(
        h, pairs, int(lo), int(hi), int(max_hits), int(chunk_mb)
    )


def read_batch(proc, requests):
    """批量分散读取 `[(addr, size), ...]` -> `[bytes | None, ...]`，不可用时返回 None。"""
    h = _proc_handle(proc)
    if not h:
        return None
    return _MOD.read_batch(h, [(int(a), int(s)) for a, s in requests])


def enum_regions(proc, writable=True, heap_only=True, min_size=0, merge_adjacent=False):
    """原生 VirtualQueryEx 区域枚举 -> `[(start, end, type), ...]`，不可用时返回 None。"""
    h = _proc_handle(proc)
    if not h:
        return None
    return _MOD.enum_regions(
        h,
        int(getattr(proc, "base", 0) or 0),
        int(getattr(proc, "image_size", 0) or 0),
        bool(writable),
        bool(heap_only),
        int(min_size),
        bool(merge_adjacent),
    )
