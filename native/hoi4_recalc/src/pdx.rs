//! Paradox Clausewitz 脚本通用极速零依赖解析原语。
//!
//! 用纯字节/UTF-8 状态机替代 `regex`，零外部依赖、零回溯，同时在词边界 (`\b`)、
//! 注释剥离与大括号配对上与原 Python 解析器 100% 等价。

use std::fs;
use std::path::Path;

/// 读取脚本/本地化文件为 UTF-8 字符串，自动识别 UTF-16 LE/BE 与 UTF-8 BOM。
pub fn read_text_lossy(path: &Path) -> Result<String, String> {
    let raw = fs::read(path).map_err(|e| format!("读取 {} 失败: {}", path.display(), e))?;
    Ok(decode_bytes_lossy(&raw))
}

pub fn decode_bytes_lossy(raw: &[u8]) -> String {
    if raw.len() >= 2 && raw[0] == 0xFF && raw[1] == 0xFE {
        // UTF-16 LE
        let units: Vec<u16> = raw[2..]
            .chunks_exact(2)
            .map(|c| u16::from_le_bytes([c[0], c[1]]))
            .collect();
        return String::from_utf16_lossy(&units);
    }
    if raw.len() >= 2 && raw[0] == 0xFE && raw[1] == 0xFF {
        // UTF-16 BE
        let units: Vec<u16> = raw[2..]
            .chunks_exact(2)
            .map(|c| u16::from_be_bytes([c[0], c[1]]))
            .collect();
        return String::from_utf16_lossy(&units);
    }
    let slice = if raw.len() >= 3 && raw[0] == 0xEF && raw[1] == 0xBB && raw[2] == 0xBF {
        &raw[3..]
    } else {
        raw
    };
    String::from_utf8_lossy(slice).into_owned()
}

/// 等价于 Python `re.sub(r"#[^\n]*", "", text)`：剥离每行 `#` 起的注释并保留换行符。
pub fn strip_comments(text: &str) -> String {
    let bytes = text.as_bytes();
    if !bytes.contains(&b'#') {
        return text.to_string();
    }
    let mut out = Vec::with_capacity(bytes.len());
    let mut i = 0usize;
    let n = bytes.len();
    while i < n {
        if bytes[i] == b'#' {
            while i < n && bytes[i] != b'\n' {
                i += 1;
            }
        } else {
            out.push(bytes[i]);
            i += 1;
        }
    }
    // 仅删掉了 `#` 到 `\n` 的字节段，UTF-8 边界完全保持合法
    String::from_utf8(out).unwrap_or_else(|e| String::from_utf8_lossy(&e.into_bytes()).into_owned())
}

#[inline]
pub fn is_word_byte(b: u8) -> bool {
    b.is_ascii_alphanumeric() || b == b'_'
}

#[inline]
pub fn is_ws(b: u8) -> bool {
    matches!(b, b' ' | b'\t' | b'\r' | b'\n')
}

#[inline]
pub fn skip_ws(b: &[u8], mut i: usize) -> usize {
    while i < b.len() && is_ws(b[i]) {
        i += 1;
    }
    i
}

/// 在 `text[from..]` 中查找满足 `\b<key>\s*=` 的首个位置，返回 `=` 之后的索引。
pub fn find_key_eq(text: &str, key: &str, from: usize, case_insensitive: bool) -> Option<usize> {
    let b = text.as_bytes();
    let kb = key.as_bytes();
    let klen = kb.len();
    if klen == 0 || from + klen > b.len() {
        return None;
    }
    let first_lo = kb[0].to_ascii_lowercase();
    let first_hi = kb[0].to_ascii_uppercase();
    let mut i = from;
    let limit = b.len() - klen;
    while i <= limit {
        let matched_first = if case_insensitive {
            b[i] == first_lo || b[i] == first_hi
        } else {
            b[i] == kb[0]
        };
        if matched_first {
            // 前词边界
            if i == 0 || !is_word_byte(b[i - 1]) {
                let end = i + klen;
                let ok = if case_insensitive {
                    b[i..end].eq_ignore_ascii_case(kb)
                } else {
                    &b[i..end] == kb
                };
                if ok && (end == b.len() || !is_word_byte(b[end])) {
                    let after_ws = skip_ws(b, end);
                    if after_ws < b.len() && b[after_ws] == b'=' {
                        return Some(after_ws + 1);
                    }
                }
            }
        }
        i += 1;
    }
    None
}

/// 给定 `{` 之后的起始偏移 `start`，配对大括号并返回 `(inner_body, pos_after_close)`。
pub fn match_brace_block(text: &str, start: usize) -> Option<(&str, usize)> {
    let b = text.as_bytes();
    let mut i = start;
    let mut depth = 1usize;
    while i < b.len() && depth > 0 {
        match b[i] {
            b'{' => depth += 1,
            b'}' => depth -= 1,
            _ => {}
        }
        i += 1;
    }
    if depth == 0 {
        Some((&text[start..i - 1], i))
    } else {
        None
    }
}

/// 提取首个 `\b<key>\s*=\s*\{ ... \}` 的内部文本。
pub fn first_block_after<'a>(text: &'a str, key: &str) -> Option<&'a str> {
    let b = text.as_bytes();
    let mut pos = 0usize;
    while let Some(after_eq) = find_key_eq(text, key, pos, false) {
        let p = skip_ws(b, after_eq);
        if p < b.len() && b[p] == b'{' {
            if let Some((inner, _)) = match_brace_block(text, p + 1) {
                return Some(inner);
            }
            return None;
        }
        pos = after_eq;
    }
    None
}

/// 提取所有不重叠的 `\b<key>\s*=\s*\{ ... \}` 块内部文本（等价于 `focusplan._all_blocks_after`）。
pub fn all_blocks_after<'a>(text: &'a str, key: &str) -> Vec<&'a str> {
    let b = text.as_bytes();
    let mut out = Vec::new();
    let mut pos = 0usize;
    while let Some(after_eq) = find_key_eq(text, key, pos, false) {
        let p = skip_ws(b, after_eq);
        if p < b.len() && b[p] == b'{' {
            if let Some((inner, end_pos)) = match_brace_block(text, p + 1) {
                out.push(inner);
                pos = end_pos;
                continue;
            } else {
                break;
            }
        }
        pos = after_eq;
    }
    out
}

/// 等价于 `re.search(r"\b<key>\s*=\s*(-?\d+)", text)`。
pub fn first_int_after(text: &str, key: &str, allow_neg: bool) -> Option<i64> {
    let b = text.as_bytes();
    let mut pos = 0usize;
    while let Some(after_eq) = find_key_eq(text, key, pos, false) {
        let mut p = skip_ws(b, after_eq);
        let start = p;
        if allow_neg && p < b.len() && b[p] == b'-' {
            p += 1;
        }
        let dstart = p;
        while p < b.len() && b[p].is_ascii_digit() {
            p += 1;
        }
        if p > dstart {
            if let Ok(v) = text[start..p].parse::<i64>() {
                return Some(v);
            }
        }
        pos = after_eq;
    }
    None
}

/// 等价于 `re.search(r"\b<key>\s*=\s*(yes|no|true|false)", text, re.I)`。
pub fn first_bool_after(text: &str, key: &str) -> Option<bool> {
    let b = text.as_bytes();
    let mut pos = 0usize;
    while let Some(after_eq) = find_key_eq(text, key, pos, true) {
        let p = skip_ws(b, after_eq);
        let mut q = p;
        while q < b.len() && is_word_byte(b[q]) {
            q += 1;
        }
        if q > p {
            let tok = &text[p..q];
            if tok.eq_ignore_ascii_case("yes") || tok.eq_ignore_ascii_case("true") {
                return Some(true);
            }
            if tok.eq_ignore_ascii_case("no") || tok.eq_ignore_ascii_case("false") {
                return Some(false);
            }
        }
        pos = after_eq;
    }
    None
}

/// 等价于 `re.search(r"\b<key>\s*=\s*([A-Za-z0-9_]+)", text)`。
pub fn first_ident_after<'a>(text: &'a str, key: &str, alpha_start: bool) -> Option<&'a str> {
    let b = text.as_bytes();
    let mut pos = 0usize;
    while let Some(after_eq) = find_key_eq(text, key, pos, false) {
        let p = skip_ws(b, after_eq);
        if p < b.len()
            && if alpha_start {
                b[p].is_ascii_alphabetic() || b[p] == b'_'
            } else {
                is_word_byte(b[p])
            }
        {
            let mut q = p + 1;
            while q < b.len() && is_word_byte(b[q]) {
                q += 1;
            }
            return Some(&text[p..q]);
        }
        pos = after_eq;
    }
    None
}

/// 等价于 `re.search(r'\b<key>\s*=\s*"([^"]*)"', text)`（若 `non_empty` 为 true 则要求引号内非空）。
pub fn first_quoted_after<'a>(text: &'a str, key: &str, non_empty: bool) -> Option<&'a str> {
    let b = text.as_bytes();
    let mut pos = 0usize;
    while let Some(after_eq) = find_key_eq(text, key, pos, false) {
        let p = skip_ws(b, after_eq);
        if p < b.len() && b[p] == b'"' {
            let start = p + 1;
            if let Some(rel_end) = text[start..].find('"') {
                if !non_empty || rel_end > 0 {
                    return Some(&text[start..start + rel_end]);
                }
            }
        }
        pos = after_eq;
    }
    None
}

/// 等价于 `re.findall(r"\b<key>\s*=\s*([A-Za-z0-9_]+)", text)`。
pub fn all_idents_after<'a>(text: &'a str, key: &str) -> Vec<&'a str> {
    let b = text.as_bytes();
    let mut out = Vec::new();
    let mut pos = 0usize;
    while let Some(after_eq) = find_key_eq(text, key, pos, false) {
        let p = skip_ws(b, after_eq);
        if p < b.len() && is_word_byte(b[p]) {
            let mut q = p + 1;
            while q < b.len() && is_word_byte(b[q]) {
                q += 1;
            }
            out.push(&text[p..q]);
            pos = q;
            continue;
        }
        pos = after_eq;
    }
    out
}

/// 提取字符串中所有 `[A-Za-z0-9_]+` token。
pub fn find_all_words(text: &str) -> Vec<&str> {
    let b = text.as_bytes();
    let mut out = Vec::new();
    let mut i = 0usize;
    while i < b.len() {
        if is_word_byte(b[i]) {
            let start = i;
            i += 1;
            while i < b.len() && is_word_byte(b[i]) {
                i += 1;
            }
            out.push(&text[start..i]);
        } else {
            i += 1;
        }
    }
    out
}

/// 提取字符串中所有连续十进制非负整数（等价于 `re.findall(r"\d+", text)`）。
pub fn find_all_uints(text: &str) -> Vec<i64> {
    let b = text.as_bytes();
    let mut out = Vec::new();
    let mut i = 0usize;
    while i < b.len() {
        if b[i].is_ascii_digit() {
            let start = i;
            i += 1;
            while i < b.len() && b[i].is_ascii_digit() {
                i += 1;
            }
            if let Ok(v) = text[start..i].parse::<i64>() {
                out.push(v);
            }
        } else {
            i += 1;
        }
    }
    out
}

/// 将任意空白段折叠为单个空格并截断至 `max_chars` 字符（超长补 `"..."`）。
pub fn collapse_and_truncate(text: &str, max_chars: usize) -> String {
    let mut joined = String::with_capacity(text.len().min(max_chars * 2 + 16));
    for (idx, part) in text.split_whitespace().enumerate() {
        if idx > 0 {
            joined.push(' ');
        }
        joined.push_str(part);
    }
    if joined.chars().count() <= max_chars {
        return joined;
    }
    let mut cut: String = joined.chars().take(max_chars).collect();
    cut.push_str("...");
    cut
}
