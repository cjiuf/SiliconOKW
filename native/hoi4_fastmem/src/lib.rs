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

#[pymodule]
fn hoi4_fastmem(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add("VERSION", env!("CARGO_PKG_VERSION"))?;
    m.add("THREADS", rayon::current_num_threads())?;
    m.add_function(wrap_pyfunction!(enum_regions, m)?)?;
    m.add_function(wrap_pyfunction!(scan_vtables, m)?)?;
    m.add_function(wrap_pyfunction!(scan_vptrs_flat, m)?)?;
    m.add_function(wrap_pyfunction!(scan_i32_range, m)?)?;
    m.add_function(wrap_pyfunction!(read_batch, m)?)?;
    Ok(())
}
