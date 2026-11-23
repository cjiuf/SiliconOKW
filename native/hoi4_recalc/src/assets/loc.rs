//! 本地化表 (`loc.pkl`) 构建模块：并行解析 `localisation/<lang>/*.yml` 及 `replace/*.yml`。
//!
//! 精确对齐 `calling/loc.py` Schema 1，并支持 Mod VFS 覆盖顺序：
//!   1. `localisation/<lang>/*.yml`（主目录）
//!   2. `localisation/<lang>/replace/*.yml`（现代 HOI4 Mod 常用覆盖目录）
//!   3. `localisation/replace/*.yml`（经典覆盖目录）

use crate::pdx::read_text_lossy;
use crate::pickle::PickleWriter;
use crate::vfs::{PdxVfs, VfsEntry};
use rayon::prelude::*;
use serde::Serialize;
use std::collections::BTreeMap;
use std::path::Path;

pub const LOC_SCHEMA: i64 = 1;

#[derive(Debug, Clone, Serialize)]
pub struct LocData {
    pub schema: i64,
    pub lang: String,
    pub files: usize,
    pub count: usize,
    pub keys: BTreeMap<String, String>,
}

/// 等价于 Python `r'^\s*([A-Za-z0-9_.\-]+):\d*\s*"(.*)"\s*$'` + `replace('\\"', '"')`。
fn parse_loc_line(line: &str) -> Option<(&str, String)> {
    let trimmed = line.trim();
    if trimmed.is_empty() || trimmed.starts_with('#') {
        return None;
    }
    let bytes = trimmed.as_bytes();
    let mut i = 0usize;
    let n = bytes.len();
    while i < n
        && (bytes[i].is_ascii_alphanumeric()
            || bytes[i] == b'_'
            || bytes[i] == b'.'
            || bytes[i] == b'-')
    {
        i += 1;
    }
    if i == 0 || i >= n || bytes[i] != b':' {
        return None;
    }
    let key = &trimmed[..i];
    i += 1; // skip ':'
    while i < n && bytes[i].is_ascii_digit() {
        i += 1;
    }
    while i < n && (bytes[i] == b' ' || bytes[i] == b'\t') {
        i += 1;
    }
    if i >= n || bytes[i] != b'"' {
        return None;
    }
    let val_start = i + 1;
    // `trimmed` 末尾已去掉空白，因此必须以 `"` 结尾（对应 `"(.*)"\s*$` 贪婪匹配到最后一个引号）
    if n <= val_start || bytes[n - 1] != b'"' {
        return None;
    }
    let raw_val = &trimmed[val_start..n - 1];
    let val = if raw_val.contains("\\\"") {
        raw_val.replace("\\\"", "\"")
    } else {
        raw_val.to_string()
    };
    Some((key, val))
}

fn parse_single_yml(entry: &VfsEntry, expected_lang_header: Option<&str>) -> Vec<(String, String)> {
    let Ok(text) = read_text_lossy(&entry.disk_path) else {
        return Vec::new();
    };
    // 在通用 `localisation/replace` 中过滤其他语言文件（检查首个非空非注释行 `l_<lang>:`）
    if let Some(expect) = expected_lang_header {
        for line in text.lines() {
            let t = line.trim();
            if t.is_empty() || t.starts_with('#') {
                continue;
            }
            if t.starts_with("l_") && t.ends_with(':') {
                if !t.eq_ignore_ascii_case(expect) {
                    return Vec::new();
                }
            }
            break;
        }
    }
    let mut out = Vec::with_capacity(256);
    for line in text.lines() {
        if let Some((k, v)) = parse_loc_line(line) {
            out.push((k.to_string(), v));
        }
    }
    out
}

/// 解析指定语言目录（含任意子目录，兼容 Kaiserreich/TNO 等大修 Mod）及其 replace 目录的全部本地化键值。
pub fn build_loc_for_lang(vfs: &PdxVfs, requested_lang: Option<&str>) -> LocData {
    let lang = match requested_lang {
        Some(l) if !l.trim().is_empty() => l.trim().to_string(),
        _ => vfs
            .list_languages()
            .into_iter()
            .next()
            .unwrap_or_else(|| "english".to_string()),
    };

    let main_dir = format!("localisation/{}", lang);
    // 递归收集 localisation/<lang> 下全部子目录（跳过 replace 子目录，留待后续高优先级覆盖）
    let main_files = vfs.walk_dir_files(&main_dir, ".yml", &["replace"]);
    let sub_rep_dir = format!("localisation/{}/replace", lang);
    let sub_rep_files = vfs.walk_dir_files(&sub_rep_dir, ".yml", &[]);
    let root_rep_files = vfs.walk_dir_files("localisation/replace", ".yml", &[]);

    let n_main = main_files.len();
    let lang_header = format!("l_{}:", lang);

    // 合并为有序解析批次，保持覆盖优先级：main -> <lang>/replace -> replace（后者校验 l_<lang>:）
    let all_entries: Vec<(&VfsEntry, bool)> = main_files
        .iter()
        .map(|e| (e, false))
        .chain(sub_rep_files.iter().map(|e| (e, false)))
        .chain(root_rep_files.iter().map(|e| (e, true)))
        .collect();

    // 并行解析每个 .yml 文件，保持输出槽位与 all_entries 顺序严格一致
    let per_file: Vec<Vec<(String, String)>> = all_entries
        .par_iter()
        .map(|&(e, check_hdr)| {
            let hdr = if check_hdr {
                Some(lang_header.as_str())
            } else {
                None
            };
            parse_single_yml(e, hdr)
        })
        .collect();

    let mut keys: BTreeMap<String, String> = BTreeMap::new();
    for pairs in per_file {
        for (k, v) in pairs {
            keys.insert(k, v);
        }
    }

    let count = keys.len();
    LocData {
        schema: LOC_SCHEMA,
        lang,
        files: n_main,
        count,
        keys,
    }
}

/// 写出 `loc.pkl`（与 `calling/loc.py` 100% 同构，附加 `source` 标记供 Mod/多语言缓存识别）。
pub fn write_loc_pkl(data: &LocData, path: &Path) -> Result<usize, String> {
    let mut pw = PickleWriter::with_capacity(data.count * 96 + 65536);
    pw.begin_small_dict();
    pw.write_str_intern("schema");
    pw.write_int(data.schema);
    pw.write_str_intern("lang");
    pw.write_str(&data.lang);
    pw.write_str_intern("files");
    pw.write_int(data.files as i64);
    pw.write_str_intern("count");
    pw.write_int(data.count as i64);
    pw.write_str_intern("source");
    pw.write_str("hoi4_recalc");
    pw.write_str_intern("keys");
    pw.write_dict_iter(
        data.keys.iter(),
        |w, k| w.write_str(k),
        |w, v| w.write_str(v),
    );
    pw.end_small_dict();
    pw.save_to_file(path)
}
