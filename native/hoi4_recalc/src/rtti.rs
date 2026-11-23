//! 多核 MSVC x64 RTTI 全量扫描器：
//! TypeDescriptor (`.?AV...@@`) -> RTTICompleteObjectLocator -> `.rdata`/`.data` vtable RVAs。

use crate::pe::{read_u32, read_u64, PeImage};
use rayon::prelude::*;
use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, HashMap};

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RttiStats {
    pub type_descriptors: usize,
    pub object_locators: usize,
    pub vtables: usize,
    pub classes: usize,
    pub decorated_classes: usize,
}

#[derive(Debug, Clone)]
pub struct RttiMap {
    pub stats: RttiStats,
    /// 精简类名（如 `CState`） -> 排序去重后的 vtable RVA 列表
    pub classes: BTreeMap<String, Vec<u32>>,
    /// 原始 MSVC 修饰名（如 `.?AVCState@@`） -> vtable RVA 列表
    pub decorated: BTreeMap<String, Vec<u32>>,
    /// vtable RVA -> 精简类名
    #[allow(dead_code)]
    pub vt_to_class: HashMap<u32, String>,
}

/// 将 MSVC 修饰名规约为与 `calling/rtti_win.py::plain_name` 完全一致的简明类名：
/// - `.?AVCState@@` -> `CState`
/// - `.?AUFoo@@` -> `Foo`
/// - `.?AV?$T@VArg@@@@` -> `T`
pub fn plain_name(decorated: &str) -> String {
    let s = if let Some(rest) = decorated.strip_prefix(".?AV") {
        rest
    } else if let Some(rest) = decorated.strip_prefix(".?AU") {
        rest
    } else {
        decorated
    };
    if let Some(rest) = s.strip_prefix("?$") {
        return rest.split('@').next().unwrap_or(rest).to_string();
    }
    let first = s.split("@@").next().unwrap_or(s);
    first.split('@').next().unwrap_or(first).to_string()
}

/// 扫描 PE 镜像的全部 MSVC RTTI（与 `research/windows/rtti_win.py` 算法同源，多核加速）。
pub fn scan_rtti(pe: &PeImage) -> RttiMap {
    let data = &pe.data;
    let ib = pe.meta.image_base;
    let ib_end = ib.saturating_add(pe.meta.size_of_image as u64);

    // 1. 扫描 TypeDescriptor（匹配 `.?AV...@@`，与 rtti_win.py 保持 100% 口径对齐）
    let mut tds: HashMap<u32, String> = HashMap::with_capacity(32_768);
    let mut i = 0usize;
    while i + 6 < data.len() {
        if let Some(rel) = data[i..].windows(4).position(|w| w == b".?AV") {
            let start = i + rel;
            i = start + 4;
            let Some(nul_rel) = data[start..data.len().min(start + 320)]
                .iter()
                .position(|&b| b == 0)
            else {
                continue;
            };
            let name_bytes = &data[start..start + nul_rel];
            if name_bytes.len() < 6 || name_bytes.len() > 300 || !name_bytes.ends_with(b"@@") {
                continue;
            }
            let Some(str_rva) = pe.off2rva(start) else {
                continue;
            };
            if str_rva < 0x10 {
                continue;
            }
            let td_rva = str_rva - 0x10;
            let Some(pvf) = pe.u64_at(td_rva) else {
                continue;
            };
            if pvf < ib || pvf >= ib_end {
                continue;
            }
            let name = String::from_utf8_lossy(name_bytes).into_owned();
            tds.insert(td_rva, name);
        } else {
            break;
        }
    }

    // 2. 并行扫描全部节（4 字节对齐）中的 RTTICompleteObjectLocator:
    //    +0x00 signature == 1 (x64)
    //    +0x0C pTypeDescriptor (RVA) in tds
    //    +0x14 pSelf (RVA) == col_rva
    let cols_vec: Vec<(u32, u32)> = pe
        .meta
        .sections
        .par_iter()
        .filter(|s| s.name == ".rdata" || s.name == ".data")
        .flat_map(|s| {
            let start = s.raw as usize;
            let len = (s.rawsize as usize).min(data.len().saturating_sub(start));
            if len < 0x18 {
                return Vec::new();
            }
            let blob = &data[start..start + len];
            let mut found = Vec::new();
            let mut off = 0usize;
            while off + 0x18 <= blob.len() {
                if read_u32(blob, off) == 1 {
                    let col_rva = s.va + off as u32;
                    let self_rva = read_u32(blob, off + 0x14);
                    if self_rva == col_rva {
                        let td_rva = read_u32(blob, off + 0x0C);
                        if tds.contains_key(&td_rva) {
                            found.push((col_rva, td_rva));
                        }
                    }
                }
                off += 4;
            }
            found
        })
        .collect();

    let mut cols: HashMap<u32, u32> = HashMap::with_capacity(cols_vec.len());
    let mut col_va_to_rva: HashMap<u64, u32> = HashMap::with_capacity(cols_vec.len());
    for (col_rva, td_rva) in cols_vec {
        cols.insert(col_rva, td_rva);
        col_va_to_rva.insert(ib + col_rva as u64, col_rva);
    }

    // 3. 并行扫描 `.rdata` 与 `.data`（8 字节对齐）中指向 COL 的槽位 -> 下一槽位 (+8) 即 vtable RVA
    let vt_hits: Vec<(u32, u32)> = pe
        .meta
        .sections
        .par_iter()
        .filter(|s| s.name == ".rdata" || s.name == ".data")
        .flat_map(|s| {
            let start = s.raw as usize;
            let len = (s.rawsize as usize).min(data.len().saturating_sub(start));
            let blob = &data[start..start + len];
            let mut found = Vec::new();
            let mut off = 0usize;
            while off + 8 <= blob.len() {
                let val = read_u64(blob, off);
                if let Some(&col_rva) = col_va_to_rva.get(&val) {
                    let vt_rva = s.va + off as u32 + 8;
                    found.push((vt_rva, col_rva));
                }
                off += 8;
            }
            found
        })
        .collect();

    let mut classes: BTreeMap<String, Vec<u32>> = BTreeMap::new();
    let mut decorated: BTreeMap<String, Vec<u32>> = BTreeMap::new();
    let mut vt_to_class: HashMap<u32, String> = HashMap::with_capacity(vt_hits.len());

    for (vt_rva, col_rva) in vt_hits {
        if let Some(&td_rva) = cols.get(&col_rva) {
            if let Some(dec) = tds.get(&td_rva) {
                let pn = plain_name(dec);
                decorated.entry(dec.clone()).or_default().push(vt_rva);
                classes.entry(pn.clone()).or_default().push(vt_rva);
                vt_to_class.insert(vt_rva, pn);
            }
        }
    }

    for v in classes.values_mut() {
        v.sort_unstable();
        v.dedup();
    }
    for v in decorated.values_mut() {
        v.sort_unstable();
        v.dedup();
    }

    let stats = RttiStats {
        type_descriptors: tds.len(),
        object_locators: cols.len(),
        vtables: vt_to_class.len(),
        classes: classes.len(),
        decorated_classes: decorated.len(),
    };

    RttiMap {
        stats,
        classes,
        decorated,
        vt_to_class,
    }
}

impl RttiMap {
    pub fn first_vt(&self, cls: &str) -> Option<u32> {
        self.classes.get(cls).and_then(|v| v.first().copied())
    }

    pub fn vts(&self, cls: &str) -> Option<&[u32]> {
        self.classes.get(cls).map(|v| v.as_slice())
    }
}
