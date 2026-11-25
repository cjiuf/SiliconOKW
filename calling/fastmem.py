"""Rust 原生内存扫描扩展 `hoi4_fastmem` 的 Python 桥接与自动优雅降级层。

存在 `calling/hoi4_fastmem.pyd`（Windows 编译产物）时自动走多核 Rust 路径；
不存在或在非 Windows 平台时，`AVAILABLE = False`，上层无缝回落至纯 Python/numpy 实现。
"""
from __future__ import annotations

from array import array
import os
import struct
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


def _disabled_by_env() -> bool:
    return os.environ.get("HOI4_NO_FASTMEM", "").strip().lower() in ("1", "true", "yes", "on")


def _proc_handle(proc):
    """提取真实 Win32 进程句柄（测试替身、禁加速开关或非 Windows 返回 0，自动走 Python 回落）。"""
    if not AVAILABLE or _disabled_by_env():
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


def _unpack_div_tuple(row):
    (
        ptr,
        vtable,
        prov_ptr,
        prov_id,
        path,
        org_raw,
        str_raw,
        oos,
        tmpl_ptr,
        (str_dmg_raw, org_dmg_raw),
        (og_ptr, order_type, (h_type, h_id)),
    ) = row
    return {
        "ptr": int(ptr),
        "vtable": int(vtable),
        "prov_ptr": int(prov_ptr),
        "prov": int(prov_id) if prov_id is not None else None,
        "path": list(path),
        "org_raw": int(org_raw),
        "str_raw": int(str_raw),
        "oos": int(oos),
        "template": int(tmpl_ptr),
        "str_dmg_raw": int(str_dmg_raw),
        "org_dmg_raw": int(org_dmg_raw),
        "og": int(og_ptr),
        "order_type": int(order_type) if order_type is not None else None,
        "handle": [int(h_type), int(h_id)],
    }


def read_divisions_batch_py(
    proc,
    div_ptrs,
    *,
    off_prov_ptr: int = -1,
    off_prov_id: int = -1,
    off_path_ptr: int = -1,
    off_path_len: int = -1,
    off_org: int = -1,
    off_str: int = -1,
    off_oos: int = -1,
    off_tmpl: int = -1,
    off_str_dmg: int = -1,
    off_org_dmg: int = -1,
    off_og: int = -1,
    off_order_ptr: int = -1,
    off_order_type: int = -1,
    off_handle: int = -1,
    max_path_len: int = 256,
):
    """纯 Python 等价回落：逐师读取 `CArmy*` 核心字段与二级指针（用于非 Win32 / 测试替身 / 对账）。"""
    out = []
    clamped_max_path = min(int(max_path_len), 4096)
    for raw_ptr in div_ptrs:
        ptr = int(raw_ptr or 0)
        if ptr <= 0:
            out.append(None)
            continue
        try:
            vtable = proc.u64(ptr)
            org_raw = proc.i64(ptr + off_org) if off_org >= 0 else 0
            str_raw = proc.i64(ptr + off_str) if off_str >= 0 else 0
            oos = proc.i32(ptr + off_oos) if off_oos >= 0 else 0
            tmpl_ptr = proc.u64(ptr + off_tmpl) if off_tmpl >= 0 else 0
            str_dmg_raw = proc.i64(ptr + off_str_dmg) if off_str_dmg >= 0 else 0
            org_dmg_raw = proc.i64(ptr + off_org_dmg) if off_org_dmg >= 0 else 0
            og_ptr = proc.u64(ptr + off_og) if off_og >= 0 else 0
            if off_handle >= 0:
                h_raw = proc.read(ptr + off_handle, 8)
                if not h_raw or len(h_raw) < 8:
                    out.append(None)
                    continue
                h_type, h_id = struct.unpack("<ii", h_raw)
            else:
                h_type, h_id = 0, 0
        except Exception:
            out.append(None)
            continue

        prov_ptr = 0
        prov_id = None
        if off_prov_ptr >= 0:
            try:
                prov_ptr = proc.u64(ptr + off_prov_ptr)
                if prov_ptr and off_prov_id >= 0:
                    prov_id = proc.i32(prov_ptr + off_prov_id)
            except Exception:
                prov_id = None

        path = []
        if off_path_ptr >= 0 and off_path_len >= 0:
            try:
                pptr = proc.u64(ptr + off_path_ptr)
                plen = proc.i32(ptr + off_path_len)
                if pptr and 0 < plen < clamped_max_path:
                    raw = proc.read(pptr, plen * 4)
                    if raw and len(raw) == plen * 4:
                        path = list(struct.unpack("<%di" % plen, raw))
            except Exception:
                path = []

        order_type = None
        if off_order_ptr >= 0 and off_order_type >= 0:
            try:
                optr = proc.u64(ptr + off_order_ptr)
                if optr:
                    order_type = proc.i32(optr + off_order_type)
            except Exception:
                order_type = None

        out.append({
            "ptr": ptr,
            "vtable": int(vtable),
            "prov_ptr": int(prov_ptr),
            "prov": int(prov_id) if prov_id is not None else None,
            "path": path,
            "org_raw": int(org_raw),
            "str_raw": int(str_raw),
            "oos": int(oos),
            "template": int(tmpl_ptr),
            "str_dmg_raw": int(str_dmg_raw),
            "org_dmg_raw": int(org_dmg_raw),
            "og": int(og_ptr),
            "order_type": int(order_type) if order_type is not None else None,
            "handle": [int(h_type), int(h_id)],
        })
    return out


def read_divisions_batch(
    proc,
    div_ptrs,
    *,
    off_prov_ptr: int = -1,
    off_prov_id: int = -1,
    off_path_ptr: int = -1,
    off_path_len: int = -1,
    off_org: int = -1,
    off_str: int = -1,
    off_oos: int = -1,
    off_tmpl: int = -1,
    off_str_dmg: int = -1,
    off_org_dmg: int = -1,
    off_og: int = -1,
    off_order_ptr: int = -1,
    off_order_type: int = -1,
    off_handle: int = -1,
    max_path_len: int = 256,
    use_rust: bool = True,
):
    """批量并行读取师对象核心字段（Rust 原生优先，不可用时自动无缝回落至纯 Python）。

    所有偏移量必须由上层从 `offsets_win` / `unitorders` / `micro.pathing` 等单一真源传入；
    `-1` 表示跳过该字段。返回与 `div_ptrs` 等长的 `[dict | None, ...]`。
    """
    ptrs = [int(x or 0) for x in div_ptrs]
    if not ptrs:
        return []
    h = _proc_handle(proc) if use_rust else 0
    fn = getattr(_MOD, "read_divisions_batch", None) if h else None
    if fn is not None:
        raw_rows = fn(
            h,
            ptrs,
            int(off_prov_ptr),
            int(off_prov_id),
            int(off_path_ptr),
            int(off_path_len),
            int(off_org),
            int(off_str),
            int(off_oos),
            int(off_tmpl),
            int(off_str_dmg),
            int(off_org_dmg),
            int(off_og),
            int(off_order_ptr),
            int(off_order_type),
            int(off_handle),
            int(max_path_len),
        )
        return [_unpack_div_tuple(r) if r is not None else None for r in raw_rows]
    return read_divisions_batch_py(
        proc,
        ptrs,
        off_prov_ptr=off_prov_ptr,
        off_prov_id=off_prov_id,
        off_path_ptr=off_path_ptr,
        off_path_len=off_path_len,
        off_org=off_org,
        off_str=off_str,
        off_oos=off_oos,
        off_tmpl=off_tmpl,
        off_str_dmg=off_str_dmg,
        off_org_dmg=off_org_dmg,
        off_og=off_og,
        off_order_ptr=off_order_ptr,
        off_order_type=off_order_type,
        off_handle=off_handle,
        max_path_len=max_path_len,
    )


def read_state_provinces_batch_py(
    proc,
    state_arr_ptr: int,
    state_count: int,
    *,
    off_state_id: int = -1,
    off_owner: int = -1,
    off_controller: int = -1,
    off_prov_arr: int = -1,
    off_prov_count: int = -1,
    off_prov_id: int = -1,
    max_states: int = 4000,
    max_provs_per_state: int = 400,
):
    """纯 Python 等价回落：批量展开州数组及其省份 ID。"""
    n = max(0, min(int(state_count or 0), int(max_states)))
    arr = int(state_arr_ptr or 0)
    if not arr or n <= 0:
        return []
    expand_provs = (
        off_prov_arr >= 0
        and off_prov_count >= 0
        and off_prov_id >= 0
        and max_provs_per_state > 0
    )
    clamped_max_provs = min(int(max_provs_per_state), 4096)
    out = []
    for i in range(n):
        try:
            st = proc.u64(arr + 8 * i)
            if not st:
                continue
            sid = proc.i32(st + off_state_id) if off_state_id >= 0 else 0
            owner = proc.i32(st + off_owner) if off_owner >= 0 else 0
            ctrl = proc.i32(st + off_controller) if off_controller >= 0 else 0
        except Exception:
            continue
        pids = []
        if expand_provs:
            try:
                pa = proc.u64(st + off_prov_arr)
                pn = proc.i32(st + off_prov_count)
                if pa and 0 < pn <= clamped_max_provs:
                    for k in range(pn):
                        try:
                            pr = proc.u64(pa + 8 * k)
                            if pr:
                                pids.append(int(proc.i32(pr + off_prov_id)))
                        except Exception:
                            continue
            except Exception:
                pass
        out.append((int(st), int(sid), int(owner), int(ctrl), pids))
    return out


def read_state_provinces_batch(
    proc,
    state_arr_ptr: int,
    state_count: int,
    *,
    off_state_id: int = -1,
    off_owner: int = -1,
    off_controller: int = -1,
    off_prov_arr: int = -1,
    off_prov_count: int = -1,
    off_prov_id: int = -1,
    max_states: int = 4000,
    max_provs_per_state: int = 400,
    use_rust: bool = True,
):
    """批量并行展开州数组及其省份 ID（Rust 原生优先，自动回落至纯 Python）。

    返回 `[(state_ptr, state_id, owner_idx, controller_idx, [prov_id, ...]), ...]`，
    所有结构体偏移均由调用方显式传入（`-1` 表示跳过）。
    """
    n = max(0, min(int(state_count or 0), int(max_states)))
    arr = int(state_arr_ptr or 0)
    if not arr or n <= 0:
        return []
    h = _proc_handle(proc) if use_rust else 0
    fn = getattr(_MOD, "read_state_provinces_batch", None) if h else None
    if fn is not None:
        return fn(
            h,
            arr,
            n,
            int(off_state_id),
            int(off_owner),
            int(off_controller),
            int(off_prov_arr),
            int(off_prov_count),
            int(off_prov_id),
            int(max_states),
            int(max_provs_per_state),
        )
    return read_state_provinces_batch_py(
        proc,
        arr,
        n,
        off_state_id=off_state_id,
        off_owner=off_owner,
        off_controller=off_controller,
        off_prov_arr=off_prov_arr,
        off_prov_count=off_prov_count,
        off_prov_id=off_prov_id,
        max_states=max_states,
        max_provs_per_state=max_provs_per_state,
    )
