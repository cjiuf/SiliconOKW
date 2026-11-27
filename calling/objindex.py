"""
Scans live memory for RTTI vtable pointers and builds a class -> objects index.

Every game object (CNationalFocusDatabase, CTechnologyDatabase, ...) starts with
a vtable pointer. RTTI gives us the vtable address of all 18,882 classes in the
binary, so scanning memory for those values tells us where each object lives.

A single pass scans ~3.8 GB, which takes a few seconds with numpy.

MEMORY (this file used to OOM-kill the machine):
  * The addresses are stored RELATIVE to the image base, so the cache survives
    a game restart (ASLR moves the base every launch). Keying the cache on the
    pid or on the absolute base threw it away on every restart and forced a
    live rescan.
  * The per-class address lists are numpy/array('q') arrays, not Python lists.
    5.7 M addresses in Python lists cost ~2.6 GB of RSS; in int64 arrays they
    cost ~46 MB. The live scan therefore peaks at a few hundred MB instead of
    several GB, which is what used to OOM-kill the MCP child (10.2 GB) and,
    with it, the ptrace-attached game.
"""
from __future__ import annotations
import bisect
import os, re, struct, sys
import time as _time
from array import array

_NP = None


def _np():
    """惰性加载 numpy：仅在未启用 Rust fastmem 的扫描回落路径中按需导入。"""
    global _NP
    if _NP is None:
        import numpy as _numpy
        _NP = _numpy
    return _NP

# 缓存 Win32 GetProcessMemoryInfo 与 Linux malloc_trim 句柄，避免在扫描循环里重复加载 DLL
_WIN_RSS_FN = None
_WIN_RSS_INIT = False
_LIBC_TRIM_FN = None
_LIBC_TRIM_INIT = False


def _init_win_rss():
    global _WIN_RSS_FN, _WIN_RSS_INIT
    if _WIN_RSS_INIT:
        return _WIN_RSS_FN
    _WIN_RSS_INIT = True
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        import ctypes.wintypes as wt

        class _PMC(ctypes.Structure):
            _fields_ = [("cb", wt.DWORD), ("PageFaultCount", wt.DWORD),
                        ("PeakWorkingSetSize", ctypes.c_size_t),
                        ("WorkingSetSize", ctypes.c_size_t),
                        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                        ("PagefileUsage", ctypes.c_size_t),
                        ("PeakPagefileUsage", ctypes.c_size_t)]

        psapi = ctypes.WinDLL("psapi")
        k32 = ctypes.WinDLL("kernel32")
        k32.GetCurrentProcess.restype = ctypes.c_void_p
        psapi.GetProcessMemoryInfo.argtypes = [ctypes.c_void_p,
                                               ctypes.POINTER(_PMC),
                                               ctypes.c_ulong]
        cur_proc = k32.GetCurrentProcess()
        pmc = _PMC()
        pmc_cb = ctypes.sizeof(_PMC)
        pmc.cb = pmc_cb

        def _get():
            if psapi.GetProcessMemoryInfo(cur_proc, ctypes.byref(pmc), pmc_cb):
                return int(pmc.WorkingSetSize) // (1024 * 1024)
            return 0

        _WIN_RSS_FN = _get
    except Exception:
        _WIN_RSS_FN = None
    return _WIN_RSS_FN


def _read_chunk(proc, addr, buf, n):
    """优先使用 try_read_into 零拷贝读入缓冲；兼容仅提供 try_read 的测试替身。"""
    fn = getattr(proc, "try_read_into", None)
    if fn is not None:
        return fn(addr, buf, n)
    data = proc.try_read(addr, n)
    if not data:
        return 0
    got = len(data)
    buf[:got] = data
    return got


def _scan_regions_for_vptrs(proc, region_list, want_vptrs, max_hits=5000, chunk_size=4 << 20):
    """复用固定缓冲扫描指定区域内任意个 8 字节对齐 vptr；可用时自动切至 Rust 多核扩展。"""
    if not region_list or not want_vptrs:
        return []
    try:
        import fastmem as _fm
        fast = _fm.scan_vptrs_flat(
            proc, region_list, want_vptrs,
            max_hits=max_hits, chunk_mb=max(1, chunk_size >> 20),
        )
        if fast is not None:
            return fast
    except Exception:
        pass
    np = _np()
    targets = np.array(sorted({int(v) for v in want_vptrs}), dtype=np.uint64)
    lo, hi = int(targets[0]), int(targets[-1])
    single = targets.size == 1
    t0 = targets[0]
    buf = bytearray(chunk_size)
    mv = memoryview(buf)
    hits = []
    for rg in region_list:
        a = rg["start"]
        rem = a % 8
        if rem:
            a += 8 - rem
        end = rg["end"]
        while a < end:
            n = min(chunk_size, end - a)
            n -= n % 8
            if n <= 0:
                break
            got = _read_chunk(proc, a, buf, n)
            got -= got % 8
            if got >= 8:
                arr = np.frombuffer(mv[:got], dtype=np.uint64)
                if single:
                    idx = np.nonzero(arr == t0)[0]
                else:
                    cand = np.nonzero((arr >= lo) & (arr <= hi))[0]
                    if cand.size:
                        vals = arr[cand]
                        pos = np.clip(np.searchsorted(targets, vals), 0, targets.size - 1)
                        idx = cand[targets[pos] == vals]
                    else:
                        idx = cand
                if idx.size:
                    room = max_hits - len(hits)
                    if idx.size >= room:
                        hits.extend((np.int64(a) + idx[:room].astype(np.int64) * np.int64(8)).tolist())
                        del arr
                        return hits
                    hits.extend((np.int64(a) + idx.astype(np.int64) * np.int64(8)).tolist())
                del arr
            a += n
    return hits


def _proc_start_ticks(pid: int) -> int:
    """获取进程创建时间指纹，防止重启后同 PID 被操作系统复用导致误读旧缓存。"""
    if not pid:
        return 0
    if sys.platform == "win32":
        try:
            import ctypes
            import ctypes.wintypes as wt
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            h = k32.OpenProcess(0x1000, False, int(pid))
            if not h:
                return 0
            try:
                ct, et, kt, ut = wt.FILETIME(), wt.FILETIME(), wt.FILETIME(), wt.FILETIME()
                if k32.GetProcessTimes(h, ctypes.byref(ct), ctypes.byref(et),
                                       ctypes.byref(kt), ctypes.byref(ut)):
                    return (ct.dwHighDateTime << 32) | ct.dwLowDateTime
            finally:
                k32.CloseHandle(h)
        except Exception:
            pass
        return 0
    try:
        with open("/proc/%d/stat" % int(pid)) as f:
            return int(f.read().rsplit(")", 1)[1].split()[19])
    except Exception:
        return 0


def _plain_cls_name(name: str) -> str:
    """剥离 Linux Itanium 长度前缀（如 '22CNationalFocusDatabase' -> 'CNationalFocusDatabase'）。"""
    return re.sub(r"^\d+", "", name or "")


def _pack_regions(region_set) -> bytes:
    """把 {(start, end), ...} 紧凑序列化为 uint64 扁平字节流（15k 区域 ~240 KB，读写 <2 ms）。"""
    if not region_set:
        return b""
    flat = array("Q")
    for s, e in sorted(region_set):
        flat.append(int(s))
        flat.append(int(e))
    return flat.tobytes()


def _unpack_regions(raw: bytes) -> set:
    if not raw:
        return set()
    flat = array("Q")
    flat.frombytes(raw)
    return {(flat[i], flat[i + 1]) for i in range(0, len(flat) - 1, 2)}


class ObjectIndex:
    # 业务层实际查询的核心类集合（覆盖全部数据库、动态实体、海陆空、外交与角色类）。
    # 默认精简模式仅索引此集合（~4 万对象 / <500 KB 缓存），未收录的任意类在调用
    # `instances()` 时自动按需单类扫描并并入缓存；`counts()` / `build(full=True)` 走全量。
    CORE_CLASSES = frozenset({
        # 游戏静态/全局数据库（含 game.DATABASE_ARRAYS）
        "CBuildingDatabase",
        "CTechnologyDatabase",
        "CIdeaDatabase",
        "CDecisionDatabase",
        "CEquipmentDatabase",
        "CNationalFocusDatabase",
        "CCharacterTemplateDatabase",
        "CScriptedDiplomaticActionTemplateDatabase",
        "CWarGoalDatabase",
        "COccupationLawDatabase",
        # 角色/学说/国策/科研
        "CCharacter",
        "CCharacterManager",
        "CScientist",
        "CGrandDoctrineTemplate",
        "CContinuousNationalFocus",
        # 地图/势力/会话
        "CProvince",
        "CFaction",
        "CSession",
        # 陆海空/外交/战争动态类
        "CAirWing",
        "CFront",
        "COrdersGroup",
        "CMilitaryDeploymentConveyor",
        "CWar",
        "CWarRelation",
        "CWarGoal",
        "CWarGoalType",
        "CTimedWargoalActivity",
        "CGenerateWarGoalAction",
    })

    def __init__(self, proc, rtti_map, cache_path=None):
        self.p = proc
        self.rtti = rtti_map                 # {class_name: [vaddr,...]}
        self.cache_path = cache_path
        self._byvptr = None                  # {vptr_runtime: array('q') of addr_runtime}
        self._vptr_class = {}                # {vptr_runtime: class_name}
        for cls, vps in rtti_map.items():
            for v in vps:
                self._vptr_class[proc.va(v)] = cls
        # Signature of the binary the cache belongs to: the RTTI vtable set.
        flat = sorted(v for vps in rtti_map.values() for v in vps)
        self.sig = (len(flat), sum(flat))
        self.ttl_s = 600
        self.built_at = None
        self.regions_digest = None
        self._seen_regions = set()
        self._is_full = False
        self._indexed_vptrs = set()

    def _default_full_mode(self) -> bool:
        v = (os.environ.get("HOI4_OI_FULL") or "").strip().lower()
        return v in ("1", "true", "yes")

    def _target_vptrs(self, full: bool):
        """决定本次扫描覆盖的 vtable 集合：全量模式扫全表，精简模式仅扫核心业务类。"""
        if full or len(self._vptr_class) <= 64:
            return list(self._vptr_class.keys()), True
        want_cls = set(self.CORE_CLASSES)
        extra = os.environ.get("HOI4_OI_EXTRA_CLASSES") or ""
        for item in extra.split(","):
            s = item.strip()
            if s:
                want_cls.add(_plain_cls_name(s))
        picked = [
            vp for vp, nm in self._vptr_class.items()
            if _plain_cls_name(nm) in want_cls
        ]
        if not picked:
            return list(self._vptr_class.keys()), True
        return picked, False

    # ------------------------------------------------------------- heap shape
    def _regions_digest(self):
        """A cheap fingerprint of the writable map: count, total bytes, highest end."""
        try:
            regs = self.p.regions(writable=True)
        except Exception:
            return None
        if not regs:
            return None
        return (len(regs), sum(r["end"] - r["start"] for r in regs), max(r["end"] for r in regs))

    # ------------------------------------------------------------------ cache
    def _to_rel(self, m):
        """将运行时地址转为相对 image_base 偏移（仅在 numpy 已载入时走向量化，避免热命中拉起 numpy）。"""
        base_int = int(self.p.base)
        np_mod = _NP
        out = {}
        for k, v in (m or {}).items():
            if not len(v):
                out[int(k) - base_int] = array("q")
            elif np_mod is not None:
                rel_arr = array("q")
                np_rel = np_mod.asarray(v, dtype=np_mod.int64) - np_mod.int64(base_int)
                rel_arr.frombytes(np_rel.tobytes())
                out[int(k) - base_int] = rel_arr
            else:
                out[int(k) - base_int] = array("q", (int(x) - base_int for x in v))
        return out

    def _from_rel(self, rel):
        base_int = int(self.p.base)
        np_mod = _NP
        out = {}
        for k, v in (rel or {}).items():
            if not len(v):
                out[int(k) + base_int] = array("q")
            elif np_mod is not None:
                abs_arr = array("q")
                np_abs = np_mod.asarray(v, dtype=np_mod.int64) + np_mod.int64(base_int)
                abs_arr.frombytes(np_abs.tobytes())
                out[int(k) + base_int] = abs_arr
            else:
                out[int(k) + base_int] = array("q", (int(x) + base_int for x in v))
        return out

    def _game_session_sig(self):
        """读取极轻量的对局会话指纹（仅 3-4 次内存读，<0.1 ms），防止同 PID 读档/重开新局沿用旧缓存。"""
        if sys.platform != "win32":
            return None, None
        try:
            import offsets_win as _ow
            gs_rva = getattr(_ow, "G_GAMESTATE", None)
            if not gs_rva:
                return None, None
            gs = int(self.p.u64(int(self.p.base) + int(gs_rva)))
            if not (0x10000 < gs < 0x7FFFFFFFFFFF):
                return None, None
            c_rva = getattr(_ow, "GS_COUNTRIES", None)
            c_data = int(self.p.u64(gs + int(c_rva))) if c_rva else 0
            clk_off = getattr(_ow, "GS_CLOCK", None)
            clk = int(self.p.i32(gs + int(clk_off))) if clk_off else None
            return (gs, c_data), clk
        except Exception:
            return None, None

    def _load_cache(self, want_full=False):
        """加载并校验磁盘缓存（绑定 sig + pid + image_base + proc_start + session_sig + mode）。

        不再因为 region 数量小幅变化而整份作废：恢复 `_seen_regions` 后由调用方
        对新增 region 执行毫秒级增量 `refresh()`；同 PID 读档/重开新局或超过 TTL 时重扫。
        """
        if not self.cache_path:
            return None
        try:
            import safe_cache
            if not safe_cache.exists(self.cache_path):
                return None
            data = safe_cache.load(self.cache_path)
        except Exception:
            return None
        if not isinstance(data, dict):
            return None
        sig = data.get("sig")
        if sig is not None and tuple(sig) != tuple(self.sig):
            return None                      # 不同二进制构建
        if data.get("pid") != self.p.pid:
            return None                      # 不同进程
        if data.get("base") is not None and int(data["base"]) != int(self.p.base):
            return None                      # 不同 ASLR 基址
        cur_start = _proc_start_ticks(self.p.pid)
        cached_start = data.get("proc_start")
        if cached_start and cur_start and int(cached_start) != int(cur_start):
            return None                      # PID 被新进程复用
        cur_sess, cur_clk = self._game_session_sig()
        cached_sess = data.get("session_sig")
        if cached_sess is not None and cur_sess is not None and tuple(cached_sess) != tuple(cur_sess):
            return None                      # 同进程内重开新局或读档，CGameState/国家表堆址已换
        cached_clk = data.get("clock_tick")
        if cached_clk is not None and cur_clk is not None and int(cur_clk) < int(cached_clk):
            return None                      # 游戏时钟倒退（读旧档或同剧本重开）
        cached_full = bool(data.get("full", True))
        if want_full and not cached_full:
            return None                      # 请求全量但缓存只是精简版
        ts = data.get("ts")
        if self.ttl_s and ts and (_time.time() - ts) > self.ttl_s:
            return None                      # 超过 TTL：老堆区可能已原地复用对象，重扫更安全
        rel = data.get("rel")
        if rel is None:
            return None
        seen_raw = data.get("seen_regions_bin")
        if seen_raw is None:
            # 兼容旧版缓存：无 region 列表时回退为摘要比对
            if data.get("regions") is not None and data.get("regions") != self._regions_digest():
                return None
            self._seen_regions = set()
        else:
            self._seen_regions = _unpack_regions(seen_raw)

        self.built_at = ts
        self.regions_digest = data.get("regions")
        self._is_full = cached_full
        idx_rvas = data.get("indexed_rvas")
        if idx_rvas is not None:
            self._indexed_vptrs = {int(r) + int(self.p.base) for r in idx_rvas}
        elif cached_full:
            self._indexed_vptrs = set(self._vptr_class.keys())
        else:
            self._indexed_vptrs = {int(k) + int(self.p.base) for k in rel.keys()}
        return self._from_rel(rel)

    def _save_cache(self):
        if not self.cache_path:
            return
        try:
            import safe_cache
            base_int = int(self.p.base)
            sess_sig, clk_tick = self._game_session_sig()
            payload = {
                "sig": self.sig,
                "pid": self.p.pid,
                "base": base_int,
                "proc_start": _proc_start_ticks(self.p.pid),
                "session_sig": sess_sig,
                "clock_tick": clk_tick,
                "full": bool(self._is_full),
                "ts": _time.time(),
                "regions": self.regions_digest,
                "seen_regions_bin": _pack_regions(self._seen_regions),
                "indexed_rvas": None if self._is_full else sorted(
                    int(vp) - base_int for vp in self._indexed_vptrs
                ),
                "n": len(self._byvptr or {}),
                "rel": self._to_rel(self._byvptr),
            }
            safe_cache.save(self.cache_path, payload, fmt="json.gz")
        except Exception as _e:
            try:
                import diag as _diag
                _diag.warn("objindex.save_cache", _e, detail=self.cache_path)
            except Exception:
                pass

    # ------------------------------------------------------------------ scanning
    def _rss_mb(self):
        if sys.platform == "win32":
            # 复用模块级缓存的 GetProcessMemoryInfo 句柄，避免扫描循环内反复加载 DLL
            fn = _init_win_rss()
            if fn is not None:
                try:
                    return fn()
                except Exception:
                    return 0
            return 0
        try:
            with open("/proc/self/status") as fh:
                for line in fh:
                    if line.startswith("VmRSS"):
                        return int(line.split()[1]) // 1024
        except Exception:
            pass
        return 0

    @staticmethod
    def _merge_hits(out, base_addr, cand_idx, vals, targets, max_per_class=None):
        """向量化分组并按 vtable 批量追加到 array('q')，替代百万次 Python 循环。"""
        np = _np()
        idx = np.clip(np.searchsorted(targets, vals), 0, targets.size - 1)
        good = targets[idx] == vals
        if not np.any(good):
            return 0
        g_vals = vals[good]
        g_addrs = np.int64(base_addr) + cand_idx[good].astype(np.int64) * np.int64(8)
        if g_vals.size > 1:
            order = np.argsort(g_vals, kind="mergesort")
            g_vals = g_vals[order]
            g_addrs = g_addrs[order]
        uniq, starts = np.unique(g_vals, return_index=True)
        ends = np.append(starts[1:], g_vals.size)
        added = 0
        for v_u64, s_i, e_i in zip(uniq.tolist(), starts.tolist(), ends.tolist()):
            lst = out.get(v_u64)
            if lst is None:
                lst = out[v_u64] = array("q")
            if max_per_class is not None:
                room = max_per_class - len(lst)
                if room <= 0:
                    continue
                end_i = min(e_i, s_i + room)
            else:
                end_i = e_i
            if end_i > s_i:
                lst.frombytes(g_addrs[s_i:end_i].tobytes())
                added += end_i - s_i
        return added

    def build(self, force=False, max_per_class=200000, progress=None, mem_limit_mb=1200, full=None):
        want_full = self._default_full_mode() if full is None else bool(full)
        if self._byvptr is not None and not force:
            if not want_full or self._is_full:
                return self._byvptr
        if not force:
            cached = self._load_cache(want_full=want_full)
            if cached is not None:
                self._byvptr = cached
                # 缓存命中但 region 列表有增长时，仅增量扫新增 region（毫秒级）而不整份作废
                if self._seen_regions:
                    try:
                        cur_regs = self.p.regions(writable=True)
                        if any((r["start"], r["end"]) not in self._seen_regions for r in cur_regs):
                            self.refresh(refresh_maps=False)
                    except Exception:
                        pass
                return self._byvptr

        if not self._vptr_class:
            self._byvptr = {}
            self._is_full = True
            self._indexed_vptrs = set()
            self.built_at = _time.time()
            self.regions_digest = self._regions_digest()
            return self._byvptr

        target_list, is_full = self._target_vptrs(want_full)
        # 优先走 Rust 多核扩展（.rdata Bitset + Rayon 并行），不可用时优雅回落至 numpy 路径
        regions = self.p.regions(writable=True)
        self._seen_regions = {(r["start"], r["end"]) for r in regions}
        aborted = False
        out = None
        try:
            import fastmem as _fm
            out = _fm.scan_vtables_arrays(
                self.p, regions, target_list,
                max_per_class=max_per_class, chunk_mb=4,
            )
        except Exception:
            out = None

        if out is None:
            np = _np()
            targets = np.array(sorted(target_list), dtype=np.uint64)
            lo, hi = int(targets[0]), int(targets[-1])
            out = {}
            CH = 4 << 20
            RSS_CHECK_STEP = 64 << 20
            buf = bytearray(CH)
            mv = memoryview(buf)
            bytes_since_rss = 0
            for ri, m in enumerate(regions):
                if aborted:
                    break
                a = m["start"]
                rem = a % 8
                if rem:
                    a += 8 - rem
                if progress:
                    progress(ri, len(regions))
                while a < m["end"]:
                    n = min(CH, m["end"] - a)
                    n -= n % 8
                    if n <= 0:
                        break
                    got = _read_chunk(self.p, a, buf, n)
                    got -= got % 8
                    if got >= 8:
                        arr = np.frombuffer(mv[:got], dtype=np.uint64)
                        cand = np.nonzero((arr >= lo) & (arr <= hi))[0]
                        if cand.size:
                            vals = arr[cand]
                            self._merge_hits(out, a, cand, vals, targets, max_per_class)
                            del vals
                        del cand, arr
                    a += n
                    bytes_since_rss += n
                    if bytes_since_rss >= RSS_CHECK_STEP:
                        bytes_since_rss = 0
                        self._trim()
                        if self._rss_mb() > mem_limit_mb:
                            aborted = True
                            break
        self._byvptr = out
        self._is_full = is_full
        self._indexed_vptrs = set(target_list)
        self.built_at = _time.time()
        self.regions_digest = self._regions_digest()
        if not aborted:
            self._save_cache()
        return self._byvptr

    def refresh(self, refresh_maps=True):
        """增量补充上次扫描之后新分配的可写内存区域，避免整份缓存作废重扫。"""
        if self._byvptr is None:
            return self.build()
        if not self._vptr_class:
            return self._byvptr
        if refresh_maps:
            try:
                self.p.refresh_maps()
            except Exception:
                pass
        target_list = (
            list(self._vptr_class.keys())
            if self._is_full or not self._indexed_vptrs
            else list(self._indexed_vptrs)
        )
        try:
            regions = self.p.regions(writable=True)
        except Exception:
            return self._byvptr
        new = [r for r in regions if (r["start"], r["end"]) not in self._seen_regions]
        CH = 4 << 20
        added = 0
        if new:
            fast_new = None
            try:
                import fastmem as _fm
                fast_new = _fm.scan_vtables_arrays(
                    self.p, new, target_list, chunk_mb=4
                )
            except Exception:
                fast_new = None
            if fast_new is not None:
                for vp, addrs in fast_new.items():
                    lst = self._byvptr.get(vp)
                    if lst is None:
                        self._byvptr[vp] = addrs
                    else:
                        lst.extend(addrs)
                    added += len(addrs)
            else:
                np = _np()
                targets = np.array(sorted(target_list), dtype=np.uint64)
                lo, hi = int(targets[0]), int(targets[-1])
                buf = bytearray(CH)
                mv = memoryview(buf)
                for m in new:
                    a = m["start"]
                    rem = a % 8
                    if rem:
                        a += 8 - rem
                    while a < m["end"]:
                        n = min(CH, m["end"] - a)
                        n -= n % 8
                        if n <= 0:
                            break
                        got = _read_chunk(self.p, a, buf, n)
                        got -= got % 8
                        if got >= 8:
                            arr = np.frombuffer(mv[:got], dtype=np.uint64)
                            cand = np.nonzero((arr >= lo) & (arr <= hi))[0]
                            if cand.size:
                                vals = arr[cand]
                                added += self._merge_hits(self._byvptr, a, cand, vals, targets)
                                del vals
                            del cand, arr
                        a += n
        self._seen_regions.update((r["start"], r["end"]) for r in new)
        self.regions_digest = self._regions_digest()
        self.stats = getattr(self, "stats", {})
        self.stats["refresh_added"] = self.stats.get("refresh_added", 0) + added
        self.stats["refresh_regions"] = len(new)
        if new:
            self._save_cache()
        return self._byvptr

    @staticmethod
    def _trim():
        """Return freed pages to the OS so the RSS does not creep up."""
        global _LIBC_TRIM_FN, _LIBC_TRIM_INIT
        if sys.platform == "win32":
            return                      # ReadProcessMemory leaks nothing; no libc here
        if not _LIBC_TRIM_INIT:
            _LIBC_TRIM_INIT = True
            try:
                import ctypes
                _LIBC_TRIM_FN = ctypes.CDLL("libc.so.6").malloc_trim
            except Exception:
                _LIBC_TRIM_FN = None
        if _LIBC_TRIM_FN is not None:
            try:
                _LIBC_TRIM_FN(0)
            except Exception:
                pass

    def _rtti_keys(self, class_name: str):
        """RTTI keys for a class name, in whichever mangling this build uses.

        Linux  : the Itanium name is length-prefixed -> "22CNationalFocusDatabase"
        Windows: the MSVC name is reduced to plain         -> "CNationalFocusDatabase"

        `instances()` accepts either spelling (with or without the length prefix),
        so a caller never has to know which build it is running against.
        """
        if class_name in self.rtti:
            return [class_name]
        plain = re.sub(r"^\d+", "", class_name)
        if plain in self.rtti:
            return [plain]
        cands = [c for c in self.rtti if re.fullmatch(r"\d+" + re.escape(plain), c)]
        return cands[:1]

    # ------------------------------------------------------------------ queries
    # Classes whose instances are CREATED MID-GAME. The index is a snapshot taken
    # when the world loads, so a CWarGoal created by "Danzig or War" in 1939, a new
    # CFront after a conquest, etc. is invisible to it. For these we re-read memory.
    DYNAMIC_CLASSES = {"CWarGoal", "CTimedWargoalActivity", "CFront", "CAirWing",
                       "CMilitaryDeploymentConveyor", "COrdersGroup", "CWar"}

    def live_instances(self, class_name: str, max_hits: int = 5000):
        """Real-time scan for ONE class's vtable.

        Scans the heap regions that already hold instances of this class first (new
        objects are allocated in the same arena), and only falls back to the whole
        writable address space when that finds nothing. Returns object addresses.
        """
        import time as _t
        cache = getattr(self, "_live_cache", None)
        if cache is None:
            cache = self._live_cache = {}
        hit = cache.get(class_name)
        if hit and _t.time() - hit[0] < 2.0:
            return list(hit[1])
        out = self._live_scan(class_name, max_hits)
        cache[class_name] = (_t.time(), list(out))
        return out

    def _live_scan(self, class_name: str, max_hits: int = 5000):
        keys = self._rtti_keys(class_name)
        if not keys:
            return []
        vptrs = [int(self.p.va(v))
                 for k in keys for v in self.rtti.get(k, [])]
        if not vptrs:
            return []
        want = set(vptrs)
        known = []
        m = self.build()
        for vp in want:
            known.extend(m.get(vp, []))
        regs = self.p.regions(writable=True)

        hot = []
        if known:
            # 用二分定位包含已知实例的堆区，把 O(|regs| * |known|) 降到 O(|regs| log |known|)
            known_sorted = sorted(known)
            n_known = len(known_sorted)
            for r in regs:
                i = bisect.bisect_left(known_sorted, r["start"])
                if i < n_known and known_sorted[i] < r["end"]:
                    hot.append(r)
        hits = _scan_regions_for_vptrs(self.p, hot, want, max_hits) if hot else []
        if not hits:
            # B4 (review 2026-11-07): `self.maps` is read once at attach time, so a
            # heap arena mmapped later in the campaign was never scanned and new
            # objects were invisible. Re-read the map before the full fallback.
            try:
                self.p.refresh_maps()
                regs = self.p.regions(writable=True)
            except Exception:
                pass
            hits = _scan_regions_for_vptrs(self.p, regs, want, max_hits)
        return sorted(set(hits))

    def deep_instances(self, class_name: str, max_hits: int = 20000, ttl: float = 5.0):
        """Scan EVERY writable region for this class's vtable.

        `live_instances` restricts itself to the arenas that already held an
        instance when the index was built; an object allocated later in a
        different arena is invisible to it. This walks writable heap memory
        with numpy + reused buffers and caches the result for `ttl` seconds.
        """
        import time as _t
        cache = getattr(self, "_deep_cache", None)
        if cache is None:
            cache = self._deep_cache = {}
        hit = cache.get(class_name)
        if hit and _t.time() - hit[0] < ttl:
            return list(hit[1])
        keys = self._rtti_keys(class_name)
        if not keys:
            return []
        want = [self.p.va(v) for k in keys for v in self.rtti.get(k, [])]
        if not want:
            return []
        try:
            self.p.refresh_maps()
        except Exception:
            pass
        hits = _scan_regions_for_vptrs(self.p, self.p.regions(writable=True), want, max_hits)
        out = sorted(set(hits))
        cache[class_name] = (_t.time(), list(out))
        return out

    def _sanitised(self):
        """若首次拿到的是进档前的半成品空索引，则强制重建一次。"""
        m = self.build()
        if getattr(self, "_sane", False):
            self._maybe_refresh()
            return m
        self._sane = True
        if self._is_full:
            if len(self._vptr_class) >= 8000 and len(m) < 8000:
                m = self.build(force=True, full=True)
        else:
            # 精简模式下不再拿全量 8000 类阈值误杀，仅在核心类全空时重试一次
            if len(self._vptr_class) > 64 and not m:
                m = self.build(force=True, full=False)
        return m

    def _maybe_refresh(self):
        """Keep the index from ageing out of truth.

        Half a ttl in, read the regions the game has grown since the scan (cheap).
        A full ttl in, rescan outright: most allocations reuse existing heap, so a
        region-level top-up alone would keep missing them.
        """
        if not self.ttl_s or not self.built_at:
            return
        age = _time.time() - self.built_at
        if age < self.ttl_s / 2:
            return
        try:
            if age >= self.ttl_s:
                self.build(force=True, full=self._is_full)
            else:
                self.refresh()
        except Exception:
            self.built_at = _time.time()     # do not retry on every single call

    def instances(self, class_name: str):
        """Either the mangled name ('22CNationalFocusDatabase') or the plain one."""
        plain = re.sub(r"^\d+", "", class_name)
        if plain in self.DYNAMIC_CLASSES:
            return self.live_instances(plain)
        m = self._sanitised()
        vptrs = [self.p.va(v) for key in self._rtti_keys(class_name) for v in self.rtti.get(key, [])]
        if not vptrs:
            return []
        # 精简模式下遇到未收录的类：按需单类扫描并并入索引，绝不静默返回空！
        missing = [vp for vp in vptrs if not self._is_full and vp not in self._indexed_vptrs]
        if missing:
            regs = self.p.regions(writable=True)
            fast_map = None
            try:
                import fastmem as _fm
                fast_map = _fm.scan_vtables_arrays(self.p, regs, missing, max_per_class=200000, chunk_mb=4)
            except Exception:
                fast_map = None
            if fast_map is not None:
                for vp in missing:
                    if vp in fast_map:
                        m[vp] = fast_map[vp]
            else:
                for vp in missing:
                    hits = _scan_regions_for_vptrs(self.p, regs, [vp], max_hits=200000)
                    if hits:
                        m[vp] = array("q", hits)
            self._indexed_vptrs.update(missing)
            self._save_cache()
        res = []
        for vp in vptrs:
            res.extend(m.get(vp, []))
        return sorted(set(res))

    def singleton(self, class_name: str):
        inst = self.instances(class_name)
        return inst[0] if inst else None

    def class_of(self, addr: int):
        """Resolve an object's class name from its vptr."""
        v = self.p.try_read(addr, 8)
        if not v or len(v) < 8:
            return None
        vp = struct.unpack("<Q", v)[0]
        c = self._vptr_class.get(vp)
        return re.sub(r"^\d+", "", c) if c else None

    def counts(self, pattern=""):
        # 逆向排障统计明确需要全进程所有类，显式切至 full=True
        m = self.build(full=True)
        out = {}
        for vp, addrs in m.items():
            c = self._vptr_class.get(vp)
            if not c:
                continue
            name = re.sub(r"^\d+", "", c)
            if pattern and pattern.lower() not in name.lower():
                continue
            out[name] = out.get(name, 0) + len(addrs)
        return dict(sorted(out.items(), key=lambda kv: -kv[1]))
