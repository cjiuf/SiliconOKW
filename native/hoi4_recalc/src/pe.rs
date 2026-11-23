//! 零依赖 PE32+ 解析器：节表、RVA<->文件偏移、SHA256、Build ID、.pdata 函数边界与并行 .text 扫描。

use rayon::prelude::*;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::collections::{HashMap, HashSet};
use std::fs;
use std::path::Path;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Section {
    pub name: String,
    pub va: u32,
    pub vsize: u32,
    pub raw: u32,
    pub rawsize: u32,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PeMeta {
    pub path: String,
    pub file_size: u64,
    pub sha256: String,
    pub image_base: u64,
    pub size_of_image: u32,
    pub timestamp: u32,
    pub build_id: Option<String>,
    pub pdata_functions: usize,
    pub sections: Vec<Section>,
}

pub struct PeImage {
    pub data: Vec<u8>,
    pub meta: PeMeta,
    /// 从 .pdata 解析并按 begin_rva 排序的 (begin_rva, end_rva) 函数区间表
    pub pdata: Vec<(u32, u32)>,
}

#[inline]
pub fn read_u16(buf: &[u8], off: usize) -> u16 {
    u16::from_le_bytes([buf[off], buf[off + 1]])
}

#[inline]
pub fn read_u32(buf: &[u8], off: usize) -> u32 {
    u32::from_le_bytes([buf[off], buf[off + 1], buf[off + 2], buf[off + 3]])
}

#[inline]
pub fn read_i32(buf: &[u8], off: usize) -> i32 {
    i32::from_le_bytes([buf[off], buf[off + 1], buf[off + 2], buf[off + 3]])
}

#[inline]
pub fn read_u64(buf: &[u8], off: usize) -> u64 {
    u64::from_le_bytes([
        buf[off],
        buf[off + 1],
        buf[off + 2],
        buf[off + 3],
        buf[off + 4],
        buf[off + 5],
        buf[off + 6],
        buf[off + 7],
    ])
}

impl PeImage {
    pub fn load<P: AsRef<Path>>(path: P) -> Result<Self, String> {
        let p = path.as_ref();
        let data = fs::read(p).map_err(|e| format!("读取 PE 文件失败 {}: {}", p.display(), e))?;
        if data.len() < 0x100 || &data[0..2] != b"MZ" {
            return Err(format!("不是合法的 PE 文件（缺少 MZ 头）: {}", p.display()));
        }
        let e_lfanew = read_u32(&data, 0x3C) as usize;
        if e_lfanew + 24 > data.len() || &data[e_lfanew..e_lfanew + 4] != b"PE\0\0" {
            return Err("缺少 PE\\0\\0 签名".into());
        }
        let coff = e_lfanew + 4;
        let nsec = read_u16(&data, coff + 2) as usize;
        let timestamp = read_u32(&data, coff + 4);
        let opt_size = read_u16(&data, coff + 16) as usize;
        let opt = coff + 20;
        if opt + opt_size > data.len() {
            return Err("OptionalHeader 超出文件长度".into());
        }
        let magic = read_u16(&data, opt);
        if magic != 0x20B {
            return Err(format!("仅支持 PE32+ (x64)，实际 magic={:#x}", magic));
        }
        let image_base = read_u64(&data, opt + 0x18);
        let size_of_image = read_u32(&data, opt + 0x38);

        let mut sections = Vec::with_capacity(nsec);
        let mut o = opt + opt_size;
        for _ in 0..nsec {
            if o + 40 > data.len() {
                return Err("节表截断".into());
            }
            let raw_name = &data[o..o + 8];
            let end = raw_name.iter().position(|&b| b == 0).unwrap_or(8);
            let name = String::from_utf8_lossy(&raw_name[..end]).into_owned();
            let vsize = read_u32(&data, o + 8);
            let va = read_u32(&data, o + 12);
            let rawsize = read_u32(&data, o + 16);
            let raw = read_u32(&data, o + 20);
            sections.push(Section {
                name,
                va,
                vsize,
                raw,
                rawsize,
            });
            o += 40;
        }

        // 计算 SHA256 指纹
        let mut hasher = Sha256::new();
        hasher.update(&data);
        let digest = hasher.finalize();
        let sha256 = digest.iter().map(|b| format!("{:02x}", b)).collect::<String>();

        // 解析 .pdata (RUNTIME_FUNCTION: { u32 BeginAddress, u32 EndAddress, u32 UnwindData })
        // Win64 MSVC 对 shrink-wrapped 函数会产生多个相邻分块，后续分块的 UNWIND_INFO
        // 携带 UNW_FLAG_CHAININFO (flags bit 2, 即 (ver_flags >> 3) & 0x04 != 0)。
        // 这里将其自动合并回同一父函数的连续区间，避免反汇编被冷热分块提前截断。
        let mut pdata: Vec<(u32, u32)> = Vec::new();
        if let Some(psec) = sections.iter().find(|s| s.name == ".pdata") {
            let start = psec.raw as usize;
            let len = (psec.rawsize as usize).min(data.len().saturating_sub(start));
            let count = len / 12;
            let mut raw_entries: Vec<(u32, u32, bool)> = Vec::with_capacity(count);
            for i in 0..count {
                let off = start + i * 12;
                let b = read_u32(&data, off);
                let e = read_u32(&data, off + 4);
                let uw_rva = read_u32(&data, off + 8);
                if b > 0 && e > b {
                    let mut is_chained = false;
                    // 查找 uw_rva 对应文件偏移读 UNWIND_INFO 首字节 (Version:3 | Flags:5)
                    for s in &sections {
                        let span = s.vsize.max(s.rawsize);
                        if uw_rva >= s.va && uw_rva < s.va.saturating_add(span) {
                            let d = uw_rva - s.va;
                            if d < s.rawsize {
                                let uoff = s.raw as usize + d as usize;
                                if uoff < data.len() {
                                    let flags = data[uoff] >> 3;
                                    is_chained = (flags & 0x04) != 0;
                                }
                            }
                            break;
                        }
                    }
                    raw_entries.push((b, e, is_chained));
                }
            }
            raw_entries.sort_unstable_by_key(|&(b, _, _)| b);
            pdata.reserve(raw_entries.len());
            for (b, e, is_chained) in raw_entries {
                if is_chained {
                    if let Some(last) = pdata.last_mut() {
                        if b <= last.1 + 0x10 {
                            if e > last.1 {
                                last.1 = e;
                            }
                            continue;
                        }
                    }
                }
                pdata.push((b, e));
            }
        }

        let build_id = extract_build_id(&data, &sections);
        let meta = PeMeta {
            path: p.display().to_string(),
            file_size: data.len() as u64,
            sha256,
            image_base,
            size_of_image,
            timestamp,
            build_id,
            pdata_functions: pdata.len(),
            sections,
        };
        Ok(Self { data, meta, pdata })
    }

    pub fn section(&self, name: &str) -> Option<&Section> {
        self.meta.sections.iter().find(|s| s.name == name)
    }

    #[inline]
    pub fn rva2off(&self, rva: u32) -> Option<usize> {
        for s in &self.meta.sections {
            let span = s.vsize.max(s.rawsize);
            if rva >= s.va && rva < s.va.saturating_add(span) {
                let delta = rva - s.va;
                if delta >= s.rawsize {
                    return None;
                }
                let off = s.raw as usize + delta as usize;
                if off < self.data.len() {
                    return Some(off);
                }
                return None;
            }
        }
        None
    }

    #[inline]
    pub fn off2rva(&self, off: usize) -> Option<u32> {
        for s in &self.meta.sections {
            let raw = s.raw as usize;
            let end = raw + s.rawsize as usize;
            if off >= raw && off < end {
                return Some(s.va + (off - raw) as u32);
            }
        }
        None
    }

    #[inline]
    pub fn read(&self, rva: u32, len: usize) -> Option<&[u8]> {
        let off = self.rva2off(rva)?;
        if off + len <= self.data.len() {
            Some(&self.data[off..off + len])
        } else {
            None
        }
    }

    #[inline]
    #[allow(dead_code)]
    pub fn u32_at(&self, rva: u32) -> Option<u32> {
        let b = self.read(rva, 4)?;
        Some(read_u32(b, 0))
    }

    #[inline]
    pub fn u64_at(&self, rva: u32) -> Option<u64> {
        let b = self.read(rva, 8)?;
        Some(read_u64(b, 0))
    }

    #[inline]
    pub fn va_to_rva(&self, va: u64) -> Option<u32> {
        let ib = self.meta.image_base;
        let end = ib.saturating_add(self.meta.size_of_image as u64);
        if va >= ib && va < end {
            Some((va - ib) as u32)
        } else {
            None
        }
    }

    #[inline]
    pub fn is_in_section(&self, rva: u32, name: &str) -> bool {
        if let Some(s) = self.section(name) {
            rva >= s.va && rva < s.va.saturating_add(s.vsize.max(s.rawsize))
        } else {
            false
        }
    }

    /// 二分查找 `.pdata` 获取包含 `rva` 的精确函数范围 `[begin_rva, end_rva)`。
    #[inline]
    pub fn fn_bounds(&self, rva: u32) -> Option<(u32, u32)> {
        let idx = self.pdata.partition_point(|&(b, _)| b <= rva);
        if idx == 0 {
            return None;
        }
        let (b, e) = self.pdata[idx - 1];
        if rva >= b && rva < e {
            Some((b, e))
        } else {
            None
        }
    }

    /// 优先用 `.pdata` 定位函数入口；对于无 `.pdata` 的叶函数，向后寻找最近的
    /// `0xCC` (`int3`) 或 `0xC3` (`ret`) 边界，并用指令解码对齐校验避免误命中立即数。
    pub fn fn_start(&self, rva: u32) -> u32 {
        if let Some((b, _)) = self.fn_bounds(rva) {
            return b;
        }
        if let Some(off) = self.rva2off(rva) {
            let max_back = 0x200usize.min(off);
            for d in 1..max_back {
                let prev = self.data[off - d];
                if prev == 0xCC || prev == 0xC3 {
                    let cand = rva - (d as u32 - 1);
                    // 若退到 0xC3，先确认前字节不是直接相邻的 0xCC，再用正向解码验证指令边界精准落到 rva
                    let len = (rva - cand) as usize + 8;
                    if let Some(seg) = self.read(cand, len) {
                        let mut dec = iced_x86::Decoder::with_ip(
                            64,
                            seg,
                            cand as u64,
                            iced_x86::DecoderOptions::NONE,
                        );
                        let mut aligned = false;
                        while dec.can_decode() {
                            let ip = dec.ip() as u32;
                            if ip == rva {
                                aligned = true;
                                break;
                            }
                            if ip > rva {
                                break;
                            }
                            let ins = dec.decode();
                            if ins.is_invalid() {
                                break;
                            }
                        }
                        if aligned {
                            return cand;
                        }
                    }
                }
            }
        }
        rva
    }

    /// 在指定节中查找字节序列，返回全部命中 RVA。
    pub fn find_bytes_in_section(&self, sec_name: &str, needle: &[u8]) -> Vec<u32> {
        let Some(sec) = self.section(sec_name) else {
            return Vec::new();
        };
        if needle.is_empty() {
            return Vec::new();
        }
        let start = sec.raw as usize;
        let end = (start + sec.rawsize as usize).min(self.data.len());
        let hay = &self.data[start..end];
        let mut out = Vec::new();
        let first = needle[0];
        let mut i = 0usize;
        while i + needle.len() <= hay.len() {
            if let Some(rel) = hay[i..].iter().position(|&b| b == first) {
                let pos = i + rel;
                if pos + needle.len() <= hay.len() && &hay[pos..pos + needle.len()] == needle {
                    out.push(sec.va + pos as u32);
                }
                i = pos + 1;
            } else {
                break;
            }
        }
        out
    }

    /// 在 `.rdata` 中查找 NUL 结尾的 ASCII C 字符串，返回全部命中 RVA。
    pub fn find_cstr_rvas(&self, text: &str) -> Vec<u32> {
        let mut pat = Vec::with_capacity(text.len() + 1);
        pat.extend_from_slice(text.as_bytes());
        pat.push(0);
        self.find_bytes_in_section(".rdata", &pat)
    }

    /// 多核并行单趟扫描 `.text` 中所有 RIP-relative `LEA reg, [rip + disp32]` 指令，
    /// 返回 `{target_rva: [lea_insn_rva, ...]}`（仅保留 `wanted` 中的目标）。
    pub fn scan_text_leas(&self, wanted: &HashSet<u32>) -> HashMap<u32, Vec<u32>> {
        if wanted.is_empty() {
            return HashMap::new();
        }
        let Some(sec) = self.section(".text") else {
            return HashMap::new();
        };
        let start = sec.raw as usize;
        let len = (sec.rawsize as usize).min(self.data.len().saturating_sub(start));
        if len < 7 {
            return HashMap::new();
        }
        let blob = &self.data[start..start + len];
        let base_va = sec.va;
        let chunk_sz = 1usize << 20; // 1 MiB/任务，多核负载均衡
        let n_chunks = (len + chunk_sz - 1) / chunk_sz;

        let partials: Vec<HashMap<u32, Vec<u32>>> = (0..n_chunks)
            .into_par_iter()
            .map(|ci| {
                let lo = ci * chunk_sz;
                let hi = ((ci + 1) * chunk_sz).min(len - 6);
                let mut local: HashMap<u32, Vec<u32>> = HashMap::new();
                let mut j = lo.max(1);
                while j < hi {
                    if blob[j] == 0x8D {
                        let rex = blob[j - 1];
                        if rex == 0x48 || rex == 0x4C || rex == 0x49 {
                            let modrm = blob[j + 1];
                            // ModRM: mod=00, rm=101 -> (modrm & 0xC7) == 0x05
                            if (modrm & 0xC7) == 0x05 {
                                let disp = read_i32(blob, j + 2) as i64;
                                let next_rva = base_va as i64 + (j as i64) + 6;
                                let tgt = next_rva + disp;
                                if tgt >= 0 && tgt <= u32::MAX as i64 {
                                    let tgt32 = tgt as u32;
                                    if wanted.contains(&tgt32) {
                                        let insn_rva = base_va + (j as u32 - 1);
                                        local.entry(tgt32).or_default().push(insn_rva);
                                    }
                                }
                            }
                        }
                    }
                    j += 1;
                }
                local
            })
            .collect();

        let mut merged: HashMap<u32, Vec<u32>> = HashMap::new();
        for map in partials {
            for (k, mut v) in map {
                merged.entry(k).or_default().append(&mut v);
            }
        }
        for v in merged.values_mut() {
            v.sort_unstable();
        }
        merged
    }

    /// 多核并行单趟扫描 `.text` 中所有直接近调用 `E8 <rel32>`，
    /// 返回 `{callee_rva: [call_insn_rva, ...]}`。
    pub fn scan_text_calls(&self, wanted: &HashSet<u32>) -> HashMap<u32, Vec<u32>> {
        if wanted.is_empty() {
            return HashMap::new();
        }
        let Some(sec) = self.section(".text") else {
            return HashMap::new();
        };
        let start = sec.raw as usize;
        let len = (sec.rawsize as usize).min(self.data.len().saturating_sub(start));
        if len < 5 {
            return HashMap::new();
        }
        let blob = &self.data[start..start + len];
        let base_va = sec.va;
        let chunk_sz = 1usize << 20;
        let n_chunks = (len + chunk_sz - 1) / chunk_sz;

        let partials: Vec<HashMap<u32, Vec<u32>>> = (0..n_chunks)
            .into_par_iter()
            .map(|ci| {
                let lo = ci * chunk_sz;
                let hi = ((ci + 1) * chunk_sz).min(len - 5);
                let mut local: HashMap<u32, Vec<u32>> = HashMap::new();
                let mut j = lo;
                while j < hi {
                    if blob[j] == 0xE8 {
                        let rel = read_i32(blob, j + 1) as i64;
                        let rva = base_va as i64 + j as i64;
                        let tgt = rva + 5 + rel;
                        if tgt >= 0 && tgt <= u32::MAX as i64 {
                            let tgt32 = tgt as u32;
                            if wanted.contains(&tgt32) {
                                local.entry(tgt32).or_default().push(rva as u32);
                            }
                        }
                    }
                    j += 1;
                }
                local
            })
            .collect();

        let mut merged: HashMap<u32, Vec<u32>> = HashMap::new();
        for map in partials {
            for (k, mut v) in map {
                merged.entry(k).or_default().append(&mut v);
            }
        }
        for v in merged.values_mut() {
            v.sort_unstable();
        }
        merged
    }
}

/// 从 `.rdata` 提取 HOI4 构建版本字符串（例如 `Operation Postern v1.19.5.0.7c6c`）。
fn extract_build_id(data: &[u8], sections: &[Section]) -> Option<String> {
    let rdata = sections.iter().find(|s| s.name == ".rdata")?;
    let start = rdata.raw as usize;
    let end = (start + rdata.rawsize as usize).min(data.len());
    let hay = &data[start..end];

    // 匹配 " v1.<digits>.<digits>.<digits>" 形态，并回溯到该 C 字符串起始处
    let mut best: Option<String> = None;
    let mut i = 0usize;
    while i + 10 < hay.len() {
        if hay[i] == b'v'
            && hay[i + 1].is_ascii_digit()
            && hay[i + 2] == b'.'
            && hay[i + 3].is_ascii_digit()
        {
            let mut s = i;
            while s > 0 && (i - s) < 64 && hay[s - 1].is_ascii_graphic() || (s > 0 && (i - s) < 64 && hay[s - 1] == b' ') {
                s -= 1;
            }
            let mut e = i;
            while e < hay.len() && (e - s) < 96 && (hay[e].is_ascii_graphic() || hay[e] == b' ') {
                e += 1;
            }
            if let Ok(cand) = std::str::from_utf8(&hay[s..e]) {
                let trimmed = cand.trim();
                // 要求形如 v1.19.5.0.xxxx（至少含 3 个点号）
                if trimmed.matches('.').count() >= 3 && trimmed.len() <= 80 {
                    // 优先保留带版本代号前缀（含空格且较长）的串
                    let is_better = match &best {
                        None => true,
                        Some(prev) => {
                            let cur_has_space = trimmed.contains(' ');
                            let prev_has_space = prev.contains(' ');
                            (cur_has_space && !prev_has_space)
                                || (cur_has_space == prev_has_space && trimmed.len() > prev.len())
                        }
                    };
                    if is_better {
                        best = Some(trimmed.to_string());
                    }
                }
            }
            i = e;
        } else {
            i += 1;
        }
    }
    best
}
