//! HOI4 Windows 进程内存高速并行扫描与批量读取扩展（PyO3 + Rayon + 原生 Win32 API）。
//!
//! 核心加速手段：
//! 1. 直接调 Win32 `ReadProcessMemory` / `VirtualQueryEx`，释放 Python GIL 并由 Rayon 跑满多核；
//! 2. 每工作线程固定复用单块 4–8 MB `Vec<u8>` 缓冲，全程零反复分配与零二次拷贝；
//! 3. 针对全部落在 `.rdata` 连续区间内的 15k+ 虚表指针，构造常驻 CPU L2 缓存的紧凑 Bitset
//!    实现 O(1) 精确过滤（跨度超限或非 8 字节对齐时自动回落至排序二分查找）；
//! 4. `scan_vtables` 将每个命中的 `Vec<i64>` 以原始字节 `PyBytes` 批量回传 Python，
//!    由 `array('q').frombytes()` 零解析摄入，消除数百万个 Python int 对象的构造开销。

use std::collections::HashMap;
use std::ffi::c_void;
use std::sync::atomic::{AtomicUsize, Ordering};

use pyo3::prelude::*;
use pyo3::types::PyBytes;
use rayon::prelude::*;

// ===================================================================== Win32 FFI
#[cfg(windows)]
mod win32 {
    use super::c_void;

    pub const MEM_COMMIT: u32 = 0x1000;
    pub const MEM_PRIVATE: u32 = 0x20000;
    pub const MEM_IMAGE: u32 = 0x1000000;

    pub const PAGE_READONLY: u32 = 0x02;
    pub const PAGE_READWRITE: u32 = 0x04;
    pub const PAGE_WRITECOPY: u32 = 0x08;
    pub const PAGE_EXECUTE_READ: u32 = 0x20;
    pub const PAGE_EXECUTE_READWRITE: u32 = 0x40;
    pub const PAGE_EXECUTE_WRITECOPY: u32 = 0x80;
    pub const PAGE_GUARD: u32 = 0x100;

    pub const ERROR_PARTIAL_COPY: u32 = 299;
    pub const MAX_USER_ADDR: usize = 0x7FFF_FFFF_FFFF;

    #[repr(C)]
    #[derive(Clone, Copy, Default)]
    pub struct MemoryBasicInformation {
        pub base_address: usize,
        pub allocation_base: usize,
        pub allocation_protect: u32,
        pub _alignment1: u32,
        pub region_size: usize,
        pub state: u32,
        pub protect: u32,
        pub type_: u32,
        pub _alignment2: u32,
    }

    // 编译期校验 x64 MEMORY_BASIC_INFORMATION 精确布局（48 字节）
    #[cfg(target_pointer_width = "64")]
    const _: () = assert!(std::mem::size_of::<MemoryBasicInformation>() == 48);

    #[link(name = "kernel32")]
    extern "system" {
        pub fn ReadProcessMemory(
            h_process: *mut c_void,
            lp_base_address: *const c_void,
            lp_buffer: *mut c_void,
            n_size: usize,
            lp_number_of_bytes_read: *mut usize,
        ) -> i32;

        pub fn VirtualQueryEx(
            h_process: *mut c_void,
            lp_address: *const c_void,
            lp_buffer: *mut MemoryBasicInformation,
            dw_length: usize,
        ) -> usize;

        pub fn GetLastError() -> u32;
    }

    /// 将目标进程 `[addr, addr + len)` 读入调用方复用的 `buf`，兼容 `ERROR_PARTIAL_COPY`。
    #[inline]
    pub fn read_into(handle: usize, addr: u64, buf: &mut [u8]) -> usize {
        if handle == 0 || buf.is_empty() {
            return 0;
        }
        let mut bytes_read: usize = 0;
        let ok = unsafe {
            ReadProcessMemory(
                handle as *mut c_void,
                addr as usize as *const c_void,
                buf.as_mut_ptr() as *mut c_void,
                buf.len(),
                &mut bytes_read,
            )
        };
        if ok != 0 {
            return bytes_read.min(buf.len());
        }
        let err = unsafe { GetLastError() };
        if err == ERROR_PARTIAL_COPY && bytes_read > 0 {
            return bytes_read.min(buf.len());
        }
        0
    }

    #[inline]
    pub fn is_readable(protect: u32) -> bool {
        if (protect & PAGE_GUARD) != 0 {
            return false;
        }
        matches!(
            protect & 0xFF,
            PAGE_READONLY
                | PAGE_READWRITE
                | PAGE_WRITECOPY
                | PAGE_EXECUTE_READ
                | PAGE_EXECUTE_READWRITE
                | PAGE_EXECUTE_WRITECOPY
        )
    }

    #[inline]
    pub fn is_writable(protect: u32) -> bool {
        if (protect & PAGE_GUARD) != 0 {
            return false;
        }
        matches!(
            protect & 0xFF,
            PAGE_READWRITE | PAGE_WRITECOPY | PAGE_EXECUTE_READWRITE | PAGE_EXECUTE_WRITECOPY
        )
    }
}

#[cfg(not(windows))]
mod win32 {
    pub fn read_into(_handle: usize, _addr: u64, _buf: &mut [u8]) -> usize {
        0
    }
}

// ===================================================================== 虚表过滤器
/// `.rdata` 紧凑位图或排序二分查找器，用于在热循环中无分配判断 u64 是否为目标虚表。
enum VtableFilter {
    /// 目标虚表只有 1 个时的极速路径（LLVM 自动 SIMD 向量化）
    Single(u64),
    /// 所有目标均 8 字节对齐且跨度 <= 64 MB：常驻 L2 缓存的 O(1) Bitset
    Bitset {
        lo: u64,
        hi: u64,
        bits: Vec<u64>,
    },
    /// 通用回落：范围粗筛 + 排序二分查找
    Sorted {
        lo: u64,
        hi: u64,
        sorted: Vec<u64>,
    },
}

impl VtableFilter {
    const MAX_BITSET_SPAN: u64 = 64 << 20; // 64 MB 跨度 -> 1 MB bitset

    fn build(mut targets: Vec<u64>) -> Option<Self> {
        if targets.is_empty() {
            return None;
        }
        targets.sort_unstable();
        targets.dedup();
        if targets.len() == 1 {
            return Some(Self::Single(targets[0]));
        }
        let lo = targets[0];
        let hi = *targets.last().unwrap();
        let span = hi.saturating_sub(lo);
        let all_aligned8 = targets.iter().all(|&v| ((v - lo) & 7) == 0);
        if all_aligned8 && span <= Self::MAX_BITSET_SPAN {
            let n_slots = ((span >> 3) + 1) as usize;
            let n_words = (n_slots + 63) >> 6;
            let mut bits = vec![0u64; n_words];
            for &v in &targets {
                let idx = ((v - lo) >> 3) as usize;
                bits[idx >> 6] |= 1u64 << (idx & 63);
            }
            return Some(Self::Bitset { lo, hi, bits });
        }
        Some(Self::Sorted {
            lo,
            hi,
            sorted: targets,
        })
    }

    #[inline(always)]
    fn contains(&self, v: u64) -> bool {
        match self {
            Self::Single(t0) => v == *t0,
            Self::Bitset { lo, hi, bits } => {
                if v < *lo || v > *hi {
                    return false;
                }
                let diff = v - *lo;
                if (diff & 7) != 0 {
                    return false;
                }
                let idx = (diff >> 3) as usize;
                unsafe { (*bits.get_unchecked(idx >> 6) & (1u64 << (idx & 63))) != 0 }
            }
            Self::Sorted { lo, hi, sorted } => {
                if v < *lo || v > *hi {
                    return false;
                }
                sorted.binary_search(&v).is_ok()
            }
        }
    }
}

/// 将区域列表按 `align` 对齐并切分成不超过 `chunk_bytes` 的并行任务单元。
fn build_chunk_tasks(regions: &[(u64, u64)], align: u64, chunk_bytes: usize) -> Vec<(u64, usize)> {
    let chunk_u64 = (chunk_bytes as u64) & !(align - 1);
    let mut tasks = Vec::with_capacity(regions.len());
    for &(start, end) in regions {
        if end <= start {
            continue;
        }
        let rem = start % align;
        let mut a = if rem == 0 {
            start
        } else {
            match start.checked_add(align - rem) {
                Some(v) => v,
                None => continue,
            }
        };
        while a < end {
            let avail = end - a;
            let n = avail.min(chunk_u64) & !(align - 1);
            if n == 0 {
                break;
            }
            tasks.push((a, n as usize));
            a += n;
        }
    }
    tasks
}

// ===================================================================== 导出函数
/// 枚举目标进程的可读/可写内存区域（原生 VirtualQueryEx，支持 heap_only 剪枝与相邻合并）。
/// 返回 `[(start, end, mbi_type), ...]`。
#[pyfunction]
#[pyo3(signature = (handle, image_base=0, image_size=0, writable=true, heap_only=true, min_size=0, merge_adjacent=false))]
fn enum_regions(
    py: Python<'_>,
    handle: usize,
    image_base: u64,
    image_size: u64,
    writable: bool,
    heap_only: bool,
    min_size: u64,
    merge_adjacent: bool,
) -> PyResult<Vec<(u64, u64, u32)>> {
    #[cfg(not(windows))]
    {
        let _ = (py, handle, image_base, image_size, writable, heap_only, min_size, merge_adjacent);
        return Ok(Vec::new());
    }
    #[cfg(windows)]
    {
        let out = py.allow_threads(|| {
            let mut regs: Vec<(u64, u64, u32)> = Vec::with_capacity(4096);
            let mut addr: usize = 0;
            let mut mbi = win32::MemoryBasicInformation::default();
            let mbi_size = std::mem::size_of::<win32::MemoryBasicInformation>();
            let img_end = image_base.saturating_add(image_size);

            while addr < win32::MAX_USER_ADDR {
                let got = unsafe {
                    win32::VirtualQueryEx(
                        handle as *mut c_void,
                        addr as *const c_void,
                        &mut mbi,
                        mbi_size,
                    )
                };
                if got == 0 || mbi.region_size == 0 {
                    break;
                }
                let start = mbi.base_address as u64;
                let size = mbi.region_size as u64;
                let end = start.saturating_add(size);

                if mbi.state == win32::MEM_COMMIT
                    && win32::is_readable(mbi.protect)
                    && (!writable || win32::is_writable(mbi.protect))
                    && size >= min_size
                {
                    let keep = if !heap_only {
                        true
                    } else if mbi.type_ == win32::MEM_PRIVATE {
                        true
                    } else if mbi.type_ == win32::MEM_IMAGE && image_size > 0 {
                        start >= image_base && start < img_end
                    } else {
                        false
                    };
                    if keep {
                        if merge_adjacent {
                            if let Some(last) = regs.last_mut() {
                                if last.1 == start && last.2 == mbi.type_ {
                                    last.1 = end;
                                    addr = match mbi.base_address.checked_add(mbi.region_size) {
                                        Some(next) if next > addr => next,
                                        _ => break,
                                    };
                                    continue;
                                }
                            }
                        }
                        regs.push((start, end, mbi.type_));
                    }
                }

                addr = match mbi.base_address.checked_add(mbi.region_size) {
                    Some(next) if next > addr => next,
                    _ => break,
                };
            }
            regs
        });
        Ok(out)
    }
}

/// 多核并行全量/多类虚表扫描（专供 `ObjectIndex.build` / `refresh`）。
///
/// 返回 `{vtable_u64: bytes_of_sorted_int64_addrs}`，Python 侧可直接 `array('q').frombytes()`
/// 零解析摄入，避免创建数百万个 Python int 对象。
#[pyfunction]
#[pyo3(signature = (handle, regions, targets, max_per_vtable=200000, chunk_mb=4))]
fn scan_vtables(
    py: Python<'_>,
    handle: usize,
    regions: Vec<(u64, u64)>,
    targets: Vec<u64>,
    max_per_vtable: usize,
    chunk_mb: usize,
) -> PyResult<HashMap<u64, Py<PyBytes>>> {
    let filter = match VtableFilter::build(targets) {
        Some(f) => f,
        None => return Ok(HashMap::new()),
    };
    let chunk_bytes = chunk_mb.clamp(1, 32) << 20;

    let merged: HashMap<u64, Vec<i64>> = py.allow_threads(|| {
        let tasks = build_chunk_tasks(&regions, 8, chunk_bytes);
        if tasks.is_empty() {
            return HashMap::new();
        }
        // 每工作线程仅分配 1 块复用缓冲区 + 1 个本地 HashMap，扫描完再归并（<=20 个 map）
        let partials: Vec<HashMap<u64, Vec<i64>>> = tasks
            .into_par_iter()
            .fold(
                || (vec![0u8; chunk_bytes], HashMap::<u64, Vec<i64>>::new()),
                |(mut buf, mut local), (addr, len)| {
                    let got = win32::read_into(handle, addr, &mut buf[..len]);
                    let usable = got & !7;
                    if usable >= 8 {
                        let slice = &buf[..usable];
                        for (i, chunk) in slice.chunks_exact(8).enumerate() {
                            let v = u64::from_le_bytes([
                                chunk[0], chunk[1], chunk[2], chunk[3],
                                chunk[4], chunk[5], chunk[6], chunk[7],
                            ]);
                            if filter.contains(v) {
                                let entry = local.entry(v).or_default();
                                if entry.len() < max_per_vtable {
                                    entry.push((addr + (i as u64) * 8) as i64);
                                }
                            }
                        }
                    }
                    (buf, local)
                },
            )
            .map(|(_buf, local)| local)
            .collect();

        let mut total: HashMap<u64, Vec<i64>> = HashMap::new();
        for map in partials {
            for (k, mut vec) in map {
                let entry = total.entry(k).or_default();
                if entry.is_empty() {
                    *entry = vec;
                } else {
                    entry.append(&mut vec);
                }
            }
        }
        // 并行对每个类的命中地址排序并截断上限
        let mut pairs: Vec<(u64, Vec<i64>)> = total.into_iter().collect();
        pairs.par_iter_mut().for_each(|(_k, addrs)| {
            addrs.sort_unstable();
            if addrs.len() > max_per_vtable {
                addrs.truncate(max_per_vtable);
            }
        });
        pairs.into_iter().collect()
    });

    let mut py_out: HashMap<u64, Py<PyBytes>> = HashMap::with_capacity(merged.len());
    for (k, addrs) in merged {
        let raw = unsafe {
            std::slice::from_raw_parts(addrs.as_ptr() as *const u8, addrs.len() * 8)
        };
        py_out.insert(k, PyBytes::new(py, raw).unbind());
    }
    Ok(py_out)
}

/// 多核并行单类/少数类虚表扫描（专供 `win_scan_class` / `_scan_vtable` / `live_instances` / `deep_instances`）。
/// 返回去重升序的对象地址列表。
#[pyfunction]
#[pyo3(signature = (handle, regions, targets, max_hits=500000, chunk_mb=4))]
fn scan_vptrs_flat(
    py: Python<'_>,
    handle: usize,
    regions: Vec<(u64, u64)>,
    targets: Vec<u64>,
    max_hits: usize,
    chunk_mb: usize,
) -> PyResult<Vec<i64>> {
    let filter = match VtableFilter::build(targets) {
        Some(f) => f,
        None => return Ok(Vec::new()),
    };
    if max_hits == 0 {
        return Ok(Vec::new());
    }
    let chunk_bytes = chunk_mb.clamp(1, 32) << 20;

    let out = py.allow_threads(|| {
        let tasks = build_chunk_tasks(&regions, 8, chunk_bytes);
        if tasks.is_empty() {
            return Vec::new();
        }
        let hit_counter = AtomicUsize::new(0);
        let chunks: Vec<Vec<i64>> = tasks
            .into_par_iter()
            .fold(
                || (vec![0u8; chunk_bytes], Vec::<i64>::new()),
                |(mut buf, mut local), (addr, len)| {
                    if hit_counter.load(Ordering::Relaxed) >= max_hits {
                        return (buf, local);
                    }
                    let got = win32::read_into(handle, addr, &mut buf[..len]);
                    let usable = got & !7;
                    if usable >= 8 {
                        let mut added = 0usize;
                        for (i, chunk) in buf[..usable].chunks_exact(8).enumerate() {
                            let v = u64::from_le_bytes([
                                chunk[0], chunk[1], chunk[2], chunk[3],
                                chunk[4], chunk[5], chunk[6], chunk[7],
                            ]);
                            if filter.contains(v) {
                                local.push((addr + (i as u64) * 8) as i64);
                                added += 1;
                            }
                        }
                        if added > 0 {
                            hit_counter.fetch_add(added, Ordering::Relaxed);
                        }
                    }
                    (buf, local)
                },
            )
            .map(|(_buf, local)| local)
            .collect();

        let total_len: usize = chunks.iter().map(|v| v.len()).sum();
        let mut all = Vec::with_capacity(total_len);
        for mut v in chunks {
            all.append(&mut v);
        }
        all.sort_unstable();
        all.dedup();
        if all.len() > max_hits {
            all.truncate(max_hits);
        }
        all
    });
    Ok(out)
}

/// 多核并行 4 字节对齐 `i32` 区间扫描（专供 `_find_date_exact` / `_win_date_scan` / `find_date_address`）。
#[pyfunction]
#[pyo3(signature = (handle, regions, lo, hi, max_hits=500000, chunk_mb=4))]
fn scan_i32_range(
    py: Python<'_>,
    handle: usize,
    regions: Vec<(u64, u64)>,
    lo: i32,
    hi: i32,
    max_hits: usize,
    chunk_mb: usize,
) -> PyResult<Vec<i64>> {
    if lo > hi || max_hits == 0 {
        return Ok(Vec::new());
    }
    let chunk_bytes = chunk_mb.clamp(1, 32) << 20;

    let out = py.allow_threads(|| {
        let tasks = build_chunk_tasks(&regions, 4, chunk_bytes);
        if tasks.is_empty() {
            return Vec::new();
        }
        let hit_counter = AtomicUsize::new(0);
        let chunks: Vec<Vec<i64>> = tasks
            .into_par_iter()
            .fold(
                || (vec![0u8; chunk_bytes], Vec::<i64>::new()),
                |(mut buf, mut local), (addr, len)| {
                    if hit_counter.load(Ordering::Relaxed) >= max_hits {
                        return (buf, local);
                    }
                    let got = win32::read_into(handle, addr, &mut buf[..len]);
                    let usable = got & !3;
                    if usable >= 4 {
                        let mut added = 0usize;
                        for (i, chunk) in buf[..usable].chunks_exact(4).enumerate() {
                            let v = i32::from_le_bytes([chunk[0], chunk[1], chunk[2], chunk[3]]);
                            if v >= lo && v <= hi {
                                local.push((addr + (i as u64) * 4) as i64);
                                added += 1;
                            }
                        }
                        if added > 0 {
                            hit_counter.fetch_add(added, Ordering::Relaxed);
                        }
                    }
                    (buf, local)
                },
            )
            .map(|(_buf, local)| local)
            .collect();

        let total_len: usize = chunks.iter().map(|v| v.len()).sum();
        let mut all = Vec::with_capacity(total_len);
        for mut v in chunks {
            all.append(&mut v);
        }
        all.sort_unstable();
        if all.len() > max_hits {
            all.truncate(max_hits);
        }
        all
    });
    Ok(out)
}

/// 批量分散读取：单次释放 GIL 完成数千个对象的 `ReadProcessMemory`，消除 Python->ctypes 往返。
#[pyfunction]
fn read_batch(
    py: Python<'_>,
    handle: usize,
    requests: Vec<(u64, usize)>,
) -> PyResult<Vec<Option<Py<PyBytes>>>> {
    let raw_results: Vec<Option<Vec<u8>>> = py.allow_threads(|| {
        if requests.len() >= 256 {
            requests
                .into_par_iter()
                .map(|(addr, size)| {
                    if size == 0 {
                        return Some(Vec::new());
                    }
                    let mut buf = vec![0u8; size];
                    let got = win32::read_into(handle, addr, &mut buf);
                    if got == 0 {
                        None
                    } else {
                        buf.truncate(got);
                        Some(buf)
                    }
                })
                .collect()
        } else {
            requests
                .into_iter()
                .map(|(addr, size)| {
                    if size == 0 {
                        return Some(Vec::new());
                    }
                    let mut buf = vec![0u8; size];
                    let got = win32::read_into(handle, addr, &mut buf);
                    if got == 0 {
                        None
                    } else {
                        buf.truncate(got);
                        Some(buf)
                    }
                })
                .collect()
        }
    });

    Ok(raw_results
        .into_iter()
        .map(|opt| opt.map(|v| PyBytes::new(py, &v).unbind()))
        .collect())
}

// ===================================================================== 结构体批读辅助
/// 单个师批量读取返回元组类型（<=12 元以直接满足 PyO3 `IntoPyObject`，全字段偏移由 Python 传入）：
/// `(ptr, vtable, prov_ptr, prov_id, path, org_raw, str_raw, oos, tmpl_ptr, (str_dmg_raw, org_dmg_raw), (og_ptr, order_type, (handle_type, handle_id)))`
type DivRecordTuple = (
    u64,
    u64,
    u64,
    Option<i32>,
    Vec<i32>,
    i64,
    i64,
    i32,
    u64,
    (i64, i64),
    (u64, Option<i32>, (i32, i32)),
);

#[inline]
fn read_u64_field(handle: usize, base: u64, buf: &[u8], off: i64) -> Option<u64> {
    if off < 0 {
        return Some(0);
    }
    let o = off as usize;
    if o.checked_add(8).map_or(false, |end| end <= buf.len()) {
        return Some(u64::from_le_bytes([
            buf[o], buf[o + 1], buf[o + 2], buf[o + 3],
            buf[o + 4], buf[o + 5], buf[o + 6], buf[o + 7],
        ]));
    }
    let mut tmp = [0u8; 8];
    if win32::read_into(handle, base.wrapping_add(off as u64), &mut tmp) == 8 {
        Some(u64::from_le_bytes(tmp))
    } else {
        None
    }
}

#[inline]
fn read_i64_field(handle: usize, base: u64, buf: &[u8], off: i64) -> Option<i64> {
    if off < 0 {
        return Some(0);
    }
    let o = off as usize;
    if o.checked_add(8).map_or(false, |end| end <= buf.len()) {
        return Some(i64::from_le_bytes([
            buf[o], buf[o + 1], buf[o + 2], buf[o + 3],
            buf[o + 4], buf[o + 5], buf[o + 6], buf[o + 7],
        ]));
    }
    let mut tmp = [0u8; 8];
    if win32::read_into(handle, base.wrapping_add(off as u64), &mut tmp) == 8 {
        Some(i64::from_le_bytes(tmp))
    } else {
        None
    }
}

#[inline]
fn read_i32_field(handle: usize, base: u64, buf: &[u8], off: i64) -> Option<i32> {
    if off < 0 {
        return Some(0);
    }
    let o = off as usize;
    if o.checked_add(4).map_or(false, |end| end <= buf.len()) {
        return Some(i32::from_le_bytes([buf[o], buf[o + 1], buf[o + 2], buf[o + 3]]));
    }
    let mut tmp = [0u8; 4];
    if win32::read_into(handle, base.wrapping_add(off as u64), &mut tmp) == 4 {
        Some(i32::from_le_bytes(tmp))
    } else {
        None
    }
}

#[inline]
fn read_handle_field(handle: usize, base: u64, buf: &[u8], off: i64) -> Option<(i32, i32)> {
    if off < 0 {
        return Some((0, 0));
    }
    let o = off as usize;
    if o.checked_add(8).map_or(false, |end| end <= buf.len()) {
        let h_type = i32::from_le_bytes([buf[o], buf[o + 1], buf[o + 2], buf[o + 3]]);
        let h_id = i32::from_le_bytes([buf[o + 4], buf[o + 5], buf[o + 6], buf[o + 7]]);
        return Some((h_type, h_id));
    }
    let mut tmp = [0u8; 8];
    if win32::read_into(handle, base.wrapping_add(off as u64), &mut tmp) == 8 {
        let h_type = i32::from_le_bytes([tmp[0], tmp[1], tmp[2], tmp[3]]);
        let h_id = i32::from_le_bytes([tmp[4], tmp[5], tmp[6], tmp[7]]);
        Some((h_type, h_id))
    } else {
        None
    }
}

/// 批量并行读取一组师对象 (`CArmy*`) 的核心属性与二级指针（所在省 ID、移动路径、当前命令类型）。
///
/// 设计约束：
/// - 所有结构体偏移均由 Python 层从单一真源（`offsets_win` / `unitorders` / `micro.pathing`）传入；
///   传入 `-1` 表示跳过该字段，Rust 层零 PE 偏移硬编码。
/// - 单对象主结构在偏移跨度 <= 16 KB 时仅做 1 次连续 `ReadProcessMemory`（跨页部分读自动单字段回落），
///   并对 `prov_ptr` / `order_ptr` 二级指针先去重再批量并行解引用，大幅减少内核往返。
#[pyfunction]
#[pyo3(signature = (
    handle,
    div_ptrs,
    off_prov_ptr = -1,
    off_prov_id = -1,
    off_path_ptr = -1,
    off_path_len = -1,
    off_org = -1,
    off_str = -1,
    off_oos = -1,
    off_tmpl = -1,
    off_str_dmg = -1,
    off_org_dmg = -1,
    off_og = -1,
    off_order_ptr = -1,
    off_order_type = -1,
    off_handle = -1,
    max_path_len = 256
))]
#[allow(clippy::too_many_arguments)]
fn read_divisions_batch(
    py: Python<'_>,
    handle: usize,
    div_ptrs: Vec<u64>,
    off_prov_ptr: i64,
    off_prov_id: i64,
    off_path_ptr: i64,
    off_path_len: i64,
    off_org: i64,
    off_str: i64,
    off_oos: i64,
    off_tmpl: i64,
    off_str_dmg: i64,
    off_org_dmg: i64,
    off_og: i64,
    off_order_ptr: i64,
    off_order_type: i64,
    off_handle: i64,
    max_path_len: usize,
) -> PyResult<Vec<Option<DivRecordTuple>>> {
    if handle == 0 || div_ptrs.is_empty() {
        return Ok(vec![None; div_ptrs.len()]);
    }

    // 计算需要读取的主对象连续跨度（上限 16 KB 时走单块 RPM，超限时自动单字段读）
    let mut max_end: usize = 8; // 始终读取 +0 的 vtable
    for &(off, sz) in &[
        (off_prov_ptr, 8usize),
        (off_path_ptr, 8),
        (off_path_len, 4),
        (off_org, 8),
        (off_str, 8),
        (off_oos, 4),
        (off_tmpl, 8),
        (off_str_dmg, 8),
        (off_org_dmg, 8),
        (off_og, 8),
        (off_order_ptr, 8),
        (off_handle, 8),
    ] {
        if off >= 0 {
            if let Some(end) = (off as usize).checked_add(sz) {
                if end > max_end {
                    max_end = end;
                }
            }
        }
    }
    let single_block = max_end <= 0x4000;
    let buf_cap = if single_block { max_end } else { 0 };
    let clamped_max_path = max_path_len.min(4096);

    struct PrimaryRow {
        ptr: u64,
        vtable: u64,
        prov_ptr: u64,
        path: Vec<i32>,
        org_raw: i64,
        str_raw: i64,
        oos: i32,
        tmpl_ptr: u64,
        str_dmg_raw: i64,
        org_dmg_raw: i64,
        og_ptr: u64,
        order_ptr: u64,
        handle_type: i32,
        handle_id: i32,
    }

    let read_one = |ptr: u64, buf: &mut Vec<u8>, pbuf: &mut Vec<u8>| -> Option<PrimaryRow> {
        if ptr < 0x10000 {
            return None;
        }
        let got = if single_block {
            if buf.len() < buf_cap {
                buf.resize(buf_cap, 0);
            }
            let n = win32::read_into(handle, ptr, &mut buf[..buf_cap]);
            if n == 0 {
                return None;
            }
            n
        } else {
            0
        };
        let valid: &[u8] = if got > 0 { &buf[..got] } else { &[] };

        let vtable = read_u64_field(handle, ptr, valid, 0)?;
        let org_raw = read_i64_field(handle, ptr, valid, off_org)?;
        let str_raw = read_i64_field(handle, ptr, valid, off_str)?;
        let oos = read_i32_field(handle, ptr, valid, off_oos)?;
        let tmpl_ptr = read_u64_field(handle, ptr, valid, off_tmpl)?;
        let str_dmg_raw = read_i64_field(handle, ptr, valid, off_str_dmg)?;
        let org_dmg_raw = read_i64_field(handle, ptr, valid, off_org_dmg)?;
        let og_ptr = read_u64_field(handle, ptr, valid, off_og)?;
        let (handle_type, handle_id) = read_handle_field(handle, ptr, valid, off_handle)?;

        // 省指针/路径/命令指针与 Python 侧独立 try/except 保持一致：读取失败仅降级为空而非丢弃整师
        let prov_ptr = read_u64_field(handle, ptr, valid, off_prov_ptr).unwrap_or(0);
        let path_ptr = read_u64_field(handle, ptr, valid, off_path_ptr).unwrap_or(0);
        let path_len = read_i32_field(handle, ptr, valid, off_path_len).unwrap_or(0);
        let order_ptr = read_u64_field(handle, ptr, valid, off_order_ptr).unwrap_or(0);

        let mut path = Vec::new();
        if off_path_ptr >= 0
            && off_path_len >= 0
            && path_ptr >= 0x10000
            && path_len > 0
            && (path_len as usize) < clamped_max_path
        {
            let n = path_len as usize;
            let need = n * 4;
            if pbuf.len() < need {
                pbuf.resize(need, 0);
            }
            if win32::read_into(handle, path_ptr, &mut pbuf[..need]) == need {
                path.reserve(n);
                for ch in pbuf[..need].chunks_exact(4) {
                    path.push(i32::from_le_bytes([ch[0], ch[1], ch[2], ch[3]]));
                }
            }
        }

        Some(PrimaryRow {
            ptr,
            vtable,
            prov_ptr,
            path,
            org_raw,
            str_raw,
            oos,
            tmpl_ptr,
            str_dmg_raw,
            org_dmg_raw,
            og_ptr,
            order_ptr,
            handle_type,
            handle_id,
        })
    };

    let out = py.allow_threads(|| {
        // 阶段 1：读取每个师的主结构块与移动路径
        let primaries: Vec<Option<PrimaryRow>> = if div_ptrs.len() >= 128 {
            div_ptrs
                .into_par_iter()
                .map_init(
                    || (vec![0u8; buf_cap], Vec::<u8>::with_capacity(256)),
                    |(buf, pbuf), ptr| read_one(ptr, buf, pbuf),
                )
                .collect()
        } else {
            let mut buf = vec![0u8; buf_cap];
            let mut pbuf = Vec::<u8>::with_capacity(256);
            div_ptrs
                .into_iter()
                .map(|ptr| read_one(ptr, &mut buf, &mut pbuf))
                .collect()
        };

        // 阶段 2a：对所在省指针去重后批量读 province_id
        let prov_map: HashMap<u64, i32> = if off_prov_ptr >= 0 && off_prov_id >= 0 {
            let mut uniq: Vec<u64> = primaries
                .iter()
                .filter_map(|opt| opt.as_ref().map(|r| r.prov_ptr))
                .filter(|&p| p >= 0x10000)
                .collect();
            uniq.sort_unstable();
            uniq.dedup();
            let read_pid = |pp: u64| -> Option<(u64, i32)> {
                let mut tmp = [0u8; 4];
                if win32::read_into(handle, pp.wrapping_add(off_prov_id as u64), &mut tmp) == 4 {
                    Some((pp, i32::from_le_bytes(tmp)))
                } else {
                    None
                }
            };
            if uniq.len() >= 128 {
                uniq.into_par_iter().filter_map(read_pid).collect()
            } else {
                uniq.into_iter().filter_map(read_pid).collect()
            }
        } else {
            HashMap::new()
        };

        // 阶段 2b：对当前命令指针去重后批量读 order_type
        let order_map: HashMap<u64, i32> = if off_order_ptr >= 0 && off_order_type >= 0 {
            let mut uniq: Vec<u64> = primaries
                .iter()
                .filter_map(|opt| opt.as_ref().map(|r| r.order_ptr))
                .filter(|&p| p >= 0x10000)
                .collect();
            uniq.sort_unstable();
            uniq.dedup();
            let read_ot = |op: u64| -> Option<(u64, i32)> {
                let mut tmp = [0u8; 4];
                if win32::read_into(handle, op.wrapping_add(off_order_type as u64), &mut tmp) == 4 {
                    Some((op, i32::from_le_bytes(tmp)))
                } else {
                    None
                }
            };
            if uniq.len() >= 128 {
                uniq.into_par_iter().filter_map(read_ot).collect()
            } else {
                uniq.into_iter().filter_map(read_ot).collect()
            }
        } else {
            HashMap::new()
        };

        // 阶段 3：保持原始顺序组装输出元组
        primaries
            .into_iter()
            .map(|opt| {
                opt.map(|r| {
                    let prov_id = if r.prov_ptr >= 0x10000 {
                        prov_map.get(&r.prov_ptr).copied()
                    } else {
                        None
                    };
                    let order_type = if r.order_ptr >= 0x10000 {
                        order_map.get(&r.order_ptr).copied()
                    } else {
                        None
                    };
                    (
                        r.ptr,
                        r.vtable,
                        r.prov_ptr,
                        prov_id,
                        r.path,
                        r.org_raw,
                        r.str_raw,
                        r.oos,
                        r.tmpl_ptr,
                        (r.str_dmg_raw, r.org_dmg_raw),
                        (r.og_ptr, order_type, (r.handle_type, r.handle_id)),
                    )
                })
            })
            .collect()
    });

    Ok(out)
}

/// 单个州批量读取返回元组：`(state_ptr, state_id, owner_idx, controller_idx, prov_ids)`
type StateProvTuple = (u64, i32, i32, i32, Vec<i32>);

/// 批量并行读取 `CCurrentGameState` 州数组及其下属省份 ID（两级指针批读，Rust 层零偏移硬编码）。
///
/// 所有偏移参数由 Python 层从 `offsets_win` / `mapdata` 传入；传入 `-1` 表示跳过该字段。
/// 严格按原始 `0..state_count` 与州内 `0..prov_count` 顺序返回，保证与双层 Python 循环逐省完全一致。
#[pyfunction]
#[pyo3(signature = (
    handle,
    state_arr_ptr,
    state_count,
    off_state_id = -1,
    off_owner = -1,
    off_controller = -1,
    off_prov_arr = -1,
    off_prov_count = -1,
    off_prov_id = -1,
    max_states = 4000,
    max_provs_per_state = 400
))]
#[allow(clippy::too_many_arguments)]
fn read_state_provinces_batch(
    py: Python<'_>,
    handle: usize,
    state_arr_ptr: u64,
    state_count: usize,
    off_state_id: i64,
    off_owner: i64,
    off_controller: i64,
    off_prov_arr: i64,
    off_prov_count: i64,
    off_prov_id: i64,
    max_states: usize,
    max_provs_per_state: usize,
) -> PyResult<Vec<StateProvTuple>> {
    let n = state_count.min(max_states);
    if handle == 0 || state_arr_ptr < 0x10000 || n == 0 {
        return Ok(Vec::new());
    }

    let mut max_end: usize = 0;
    for &(off, sz) in &[
        (off_state_id, 4usize),
        (off_owner, 4),
        (off_controller, 4),
        (off_prov_arr, 8),
        (off_prov_count, 4),
    ] {
        if off >= 0 {
            if let Some(end) = (off as usize).checked_add(sz) {
                if end > max_end {
                    max_end = end;
                }
            }
        }
    }
    let single_block = max_end > 0 && max_end <= 0x4000;
    let buf_cap = if single_block { max_end } else { 0 };
    let expand_provs = off_prov_arr >= 0 && off_prov_count >= 0 && off_prov_id >= 0 && max_provs_per_state > 0;
    let clamped_max_provs = max_provs_per_state.min(4096);

    struct StateStage1 {
        ptr: u64,
        state_id: i32,
        owner: i32,
        controller: i32,
        prov_ptrs: Vec<u64>,
    }

    let out = py.allow_threads(|| {
        // 读取顶层 CState* 指针数组
        let mut raw_states = vec![0u8; n * 8];
        let got_states = win32::read_into(handle, state_arr_ptr, &mut raw_states) & !7;
        if got_states < 8 {
            return Vec::new();
        }
        let state_ptrs: Vec<u64> = raw_states[..got_states]
            .chunks_exact(8)
            .map(|c| u64::from_le_bytes([c[0], c[1], c[2], c[3], c[4], c[5], c[6], c[7]]))
            .collect();

        let read_state = |st: u64, buf: &mut Vec<u8>, pbuf: &mut Vec<u8>| -> Option<StateStage1> {
            if st < 0x10000 {
                return None;
            }
            let got = if single_block {
                if buf.len() < buf_cap {
                    buf.resize(buf_cap, 0);
                }
                let m = win32::read_into(handle, st, &mut buf[..buf_cap]);
                if m == 0 {
                    return None;
                }
                m
            } else {
                0
            };
            let valid: &[u8] = if got > 0 { &buf[..got] } else { &[] };
            let state_id = read_i32_field(handle, st, valid, off_state_id)?;
            let owner = read_i32_field(handle, st, valid, off_owner)?;
            let controller = read_i32_field(handle, st, valid, off_controller)?;

            let mut prov_ptrs = Vec::new();
            if expand_provs {
                if let (Some(pa), Some(pn)) = (
                    read_u64_field(handle, st, valid, off_prov_arr),
                    read_i32_field(handle, st, valid, off_prov_count),
                ) {
                    if pa >= 0x10000 && pn > 0 && (pn as usize) <= clamped_max_provs {
                        let need = (pn as usize) * 8;
                        if pbuf.len() < need {
                            pbuf.resize(need, 0);
                        }
                        let got_p = win32::read_into(handle, pa, &mut pbuf[..need]) & !7;
                        if got_p >= 8 {
                            prov_ptrs.reserve(got_p / 8);
                            for c in pbuf[..got_p].chunks_exact(8) {
                                let pr = u64::from_le_bytes([c[0], c[1], c[2], c[3], c[4], c[5], c[6], c[7]]);
                                if pr >= 0x10000 {
                                    prov_ptrs.push(pr);
                                }
                            }
                        }
                    }
                }
            }

            Some(StateStage1 {
                ptr: st,
                state_id,
                owner,
                controller,
                prov_ptrs,
            })
        };

        // 阶段 1：并行读取所有 CState 头与其 CProvince* 数组（Rayon collect 严格保持原序）
        let stages: Vec<StateStage1> = if state_ptrs.len() >= 64 {
            state_ptrs
                .into_par_iter()
                .map_init(
                    || (vec![0u8; buf_cap], Vec::<u8>::with_capacity(512)),
                    |(buf, pbuf), st| read_state(st, buf, pbuf),
                )
                .filter_map(|x| x)
                .collect()
        } else {
            let mut buf = vec![0u8; buf_cap];
            let mut pbuf = Vec::<u8>::with_capacity(512);
            state_ptrs
                .into_iter()
                .filter_map(|st| read_state(st, &mut buf, &mut pbuf))
                .collect()
        };

        // 阶段 2：去重并多核并行读取全部 CProvince* 的 province_id
        let prov_map: HashMap<u64, i32> = if expand_provs {
            let total_provs: usize = stages.iter().map(|s| s.prov_ptrs.len()).sum();
            let mut uniq: Vec<u64> = Vec::with_capacity(total_provs);
            for s in &stages {
                uniq.extend_from_slice(&s.prov_ptrs);
            }
            uniq.sort_unstable();
            uniq.dedup();
            let read_pid = |pr: u64| -> Option<(u64, i32)> {
                let mut tmp = [0u8; 4];
                if win32::read_into(handle, pr.wrapping_add(off_prov_id as u64), &mut tmp) == 4 {
                    Some((pr, i32::from_le_bytes(tmp)))
                } else {
                    None
                }
            };
            if uniq.len() >= 128 {
                uniq.into_par_iter().filter_map(read_pid).collect()
            } else {
                uniq.into_iter().filter_map(read_pid).collect()
            }
        } else {
            HashMap::new()
        };

        // 阶段 3：保序组装结果
        stages
            .into_iter()
            .map(|s| {
                let mut pids = Vec::with_capacity(s.prov_ptrs.len());
                for pr in s.prov_ptrs {
                    if let Some(&pid) = prov_map.get(&pr) {
                        pids.push(pid);
                    }
                }
                (s.ptr, s.state_id, s.owner, s.controller, pids)
            })
            .collect()
    });

    Ok(out)
}

#[pymodule]
fn hoi4_fastmem(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add("VERSION", env!("CARGO_PKG_VERSION"))?;
    m.add("THREADS", rayon::current_num_threads())?;
    m.add_function(wrap_pyfunction!(enum_regions, m)?)?;
    m.add_function(wrap_pyfunction!(scan_vtables, m)?)?;
    m.add_function(wrap_pyfunction!(scan_vptrs_flat, m)?)?;
    m.add_function(wrap_pyfunction!(scan_i32_range, m)?)?;
    m.add_function(wrap_pyfunction!(read_batch, m)?)?;
    m.add_function(wrap_pyfunction!(read_divisions_batch, m)?)?;
    m.add_function(wrap_pyfunction!(read_state_provinces_batch, m)?)?;
    Ok(())
}
