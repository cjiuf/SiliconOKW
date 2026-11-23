"""2026-11-22 内存扫描性能优化与正确性修复的离线回归测试（不依赖游戏进程）。

运行：python research/windows/test_memscan_20261122.py
"""
import os
import struct
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "calling"))

import mem
import objindex
import api
import game


class _FakeBackend:
    def __init__(self, image_base, image_size, blobs):
        self.pid = 12345
        self.image_base = image_base
        self.image_size = image_size
        self.blobs = blobs  # {start_addr: bytes}
        self.read_into_calls = 0

    def regions(self):
        return []

    def read(self, addr, size):
        for base, data in self.blobs.items():
            if base <= addr < base + len(data):
                off = addr - base
                return data[off:off + size]
        raise OSError(299, "bad addr")

    def read_into(self, addr, buf, size=None):
        self.read_into_calls += 1
        data = self.read(addr, len(buf) if size is None else size)
        buf[:len(data)] = data
        return len(data)


def _make_proc(maps, blobs, image_base=0x7FF600000000, image_size=0x4000000):
    p = object.__new__(mem.Proc)
    p.exe_name = "hoi4.exe"
    p.be = _FakeBackend(image_base, image_size, blobs)
    p.pid = 12345
    p.base = image_base
    p.image_size = image_size
    p.maps = maps
    return p


def test_regions_heap_filter():
    base = 0x7FF600000000
    maps = [
        # 1. MEM_PRIVATE 可写堆 -> 保留
        dict(start=0x10000000, end=0x10100000, perms="rw-", path="", type=0x20000),
        # 2. 主程序 hoi4.exe 的 MEM_IMAGE .data 段 -> 保留
        dict(start=base + 0x3000000, end=base + 0x3100000, perms="rw-",
             path=r"C:\Steam\hoi4.exe", type=0x1000000),
        # 3. 主程序只读 .rdata 段 -> writable=True 排除，writable=False 保留
        dict(start=base + 0x2000000, end=base + 0x2100000, perms="r--",
             path=r"C:\Steam\hoi4.exe", type=0x1000000),
        # 4. 其它 DLL 的 MEM_IMAGE 可写段 -> heap_only=True 排除
        dict(start=0x7FFA00000000, end=0x7FFA00010000, perms="rw-",
             path=r"C:\Windows\System32\d3d11.dll", type=0x1000000),
        # 5. MEM_MAPPED (DX11/文件映射) -> heap_only=True 排除，heap_only=False 保留
        dict(start=0x20000000, end=0x28000000, perms="rw-", path="", type=0x40000),
        # 6. PAGE_GUARD -> 排除
        dict(start=0x30000000, end=0x30001000, perms="---g", path="", type=0x20000),
    ]
    p = _make_proc(maps, {}, image_base=base, image_size=0x4000000)

    r_default = p.regions(writable=True)
    assert [r["start"] for r in r_default] == [0x10000000, base + 0x3000000], r_default

    r_all_w = p.regions(writable=True, heap_only=False)
    assert len(r_all_w) == 4, r_all_w

    r_ro = p.regions(writable=False)
    assert len(r_ro) == 5, r_ro

    # Linux 风格 region（无 type 字段）不受影响
    linux_maps = [
        dict(start=0x1000, end=0x2000, perms="rw-p", path="[heap]"),
        dict(start=0x3000, end=0x4000, perms="rw-p", path="/usr/lib/foo.so"),
    ]
    p_lin = _make_proc(linux_maps, {})
    assert len(p_lin.regions(writable=True)) == 2


def test_objindex_build_refresh_and_alignment():
    base = 0x7FF600000000
    vt_a = 0x1000
    vt_b = 0x2000
    pa = base + vt_a
    pb = base + vt_b

    # 构造 4KB 模拟堆页：包含对齐命中 + 1 个未对齐伪命中（偏移 +0x13）+ 紧随其后的真命中（偏移 +0x20）
    blob1 = bytearray(4096)
    struct.pack_into("<Q", blob1, 0x00, pa)
    struct.pack_into("<Q", blob1, 0x13, pb)   # 未对齐伪命中（不重叠 0x00/0x20），必须忽略！
    struct.pack_into("<Q", blob1, 0x20, pb)   # 真命中，不能被跳步漏掉！
    struct.pack_into("<Q", blob1, 0x40, pa)
    struct.pack_into("<Q", blob1, 0x80, pb)

    # 区域 2：模拟稍后增长的新堆页（测试 refresh）
    blob2 = bytearray(4096)
    struct.pack_into("<Q", blob2, 0x20, pa)

    r1 = dict(start=0x10000000, end=0x10001000, perms="rw-", path="", type=0x20000)
    r2 = dict(start=0x10002000, end=0x10003000, perms="rw-", path="", type=0x20000)

    p = _make_proc([r1], {r1["start"]: bytes(blob1), r2["start"]: bytes(blob2)}, image_base=base)
    oi = objindex.ObjectIndex(p, {"ClassA": [vt_a], "ClassB": [vt_b]})
    idx = oi.build(force=True)
    assert list(idx[pa]) == [0x10000000, 0x10000040], list(idx[pa])
    assert list(idx[pb]) == [0x10000020, 0x10000080], list(idx[pb])
    assert p.be.read_into_calls > 0

    # 验证 refresh() 仅扫新增区域并正确合并
    p.maps = [r1, r2]
    oi.refresh()
    assert list(oi._byvptr[pa]) == [0x10000000, 0x10000040, 0x10002020]

    # 验证 deep_instances 和 live_instances
    assert oi.live_instances("ClassB") == [0x10000020, 0x10000080]
    assert oi.deep_instances("ClassA") == [0x10000000, 0x10000040, 0x10002020]

    # 验证空 RTTI 表不崩溃
    oi_empty = objindex.ObjectIndex(p, {})
    assert oi_empty.build(force=True) == {}


def test_game_scan_vtable_alignment_and_kick_pid_isolation():
    base = 0x7FF600000000
    vt_off = 0x3300
    target = base + vt_off
    # 故意在 offset 4 放一个未对齐匹配，紧接着 offset 8 放一个真实 8 字节对齐匹配：
    # 旧版 _scan_vtable 命中 offset 4 后做 i += 8 跳到 12，既误报 4 又漏掉 8！
    blob = bytearray(256)
    struct.pack_into("<Q", blob, 4, target)
    struct.pack_into("<Q", blob, 16, target)
    r1 = dict(start=0x20000000, end=0x20000100, perms="rw-", path="", type=0x20000)
    p = _make_proc([r1], {r1["start"]: bytes(blob)}, image_base=base)

    g = object.__new__(game.Game)
    g.p = p
    hits = g._scan_vtable(vt_off)
    assert hits == [0x20000010], hits

    # 测试 _kick_tech_scan 跨 pid 不被旧 pid 遗留状态锁死
    game.Game._VT_SCAN = {}
    game.Game._VT_SCAN_RUNNING = {(99999, "CTechnologyTemplate")}
    called = []
    g.win_scan_class = lambda cls, **kw: called.append((g.p.pid, cls)) or []
    # _kick_tech_scan 在守护线程中跑，验证返回 True（没被旧 pid 99999 挡住）
    assert g._kick_tech_scan() is True
    import time
    for _ in range(40):
        if called:
            break
        time.sleep(0.02)
    assert called == [(12345, "CTechnologyTemplate")], called


def test_api_malloc_trim_no_exception_on_win32():
    # Windows 下应直接 no-op，不能抛异常
    api._malloc_trim()
    api._malloc_trim()


def test_objindex_focused_mode_on_demand_and_incremental_cache():
    import tempfile
    base = 0x7FF600000000
    # 构造 >64 个 RTTI 类以触发默认精简模式（核心类 + 未收录自定义类 + 70 个噪声类）
    rtti = {
        "CBuildingDatabase": [0x1000],
        "22CNationalFocusDatabase": [0x1010],  # 测 Linux Itanium 前缀兼容
        "CCustomUnlisted": [0x1020],           # 不在 CORE_CLASSES 中
    }
    for i in range(70):
        rtti["CNoiseClass%d" % i] = [0x2000 + i * 0x10]

    blob1 = bytearray(4096)
    struct.pack_into("<Q", blob1, 0x00, base + 0x1000)  # CBuildingDatabase
    struct.pack_into("<Q", blob1, 0x10, base + 0x1010)  # CNationalFocusDatabase
    struct.pack_into("<Q", blob1, 0x20, base + 0x1020)  # CCustomUnlisted
    struct.pack_into("<Q", blob1, 0x30, base + 0x2000)  # CNoiseClass0

    blob2 = bytearray(4096)
    struct.pack_into("<Q", blob2, 0x40, base + 0x1000)  # 新增 region 里的对象

    r1 = dict(start=0x10000000, end=0x10001000, perms="rw-", path="", type=0x20000)
    r2 = dict(start=0x10002000, end=0x10003000, perms="rw-", path="", type=0x20000)

    with tempfile.TemporaryDirectory() as td:
        cpath = os.path.join(td, "oi.pkl")
        p = _make_proc([r1], {r1["start"]: bytes(blob1), r2["start"]: bytes(blob2)}, image_base=base)
        oi = objindex.ObjectIndex(p, rtti, cache_path=cpath)
        m = oi.build(force=True)
        # 精简模式下不索引噪声类，但核心类（含 Itanium 前缀）必须命中
        assert oi._is_full is False
        assert (base + 0x1000) in m and (base + 0x1010) in m
        assert (base + 0x2000) not in m

        # 未收录类按需扫描并返回真实地址（绝不返回空）
        assert oi.instances("CCustomUnlisted") == [0x10000020]

        # 模拟堆增长新增 r2：新建 ObjectIndex 命中缓存且仅增量扫 r2，不整份作废
        p.maps = [r1, r2]
        p.be.read_into_calls = 0
        oi2 = objindex.ObjectIndex(p, rtti, cache_path=cpath)
        m2 = oi2.build()
        assert list(m2[base + 0x1000]) == [0x10000000, 0x10002040]
        assert p.be.read_into_calls == 1, "应只扫新增的 1 个 region，不应全扫"

        # 换 pid 或 image_base 时必须拒绝旧缓存
        p_other = _make_proc([r1], {r1["start"]: bytes(blob1)}, image_base=base + 0x100000)
        oi_other = objindex.ObjectIndex(p_other, rtti, cache_path=cpath)
        assert oi_other._load_cache() is None

        # counts() 应自动升到全量模式，能统计到 CNoiseClass0
        cnt = oi2.counts("CNoiseClass0")
        assert cnt.get("CNoiseClass0") == 1, cnt


def main():
    tests = [
        test_regions_heap_filter,
        test_objindex_build_refresh_and_alignment,
        test_game_scan_vtable_alignment_and_kick_pid_isolation,
        test_api_malloc_trim_no_exception_on_win32,
        test_objindex_focused_mode_on_demand_and_incremental_cache,
    ]
    for fn in tests:
        fn()
        print("OK  ", fn.__name__)
    print("\n%d/%d passed" % (len(tests), len(tests)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
