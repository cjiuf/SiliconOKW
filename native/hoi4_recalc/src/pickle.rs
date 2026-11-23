//! 零依赖 Python Pickle Protocol 4 编码器。
//!
//! 严格遵循 CPython `Lib/pickle.py` Protocol 4 规范：
//! - 头部 `PROTO 4` + 64 KiB 自动分帧 (`FRAME`)；
//! - 支持 `None` / `bool` / `i64` / `f64` / `str` / `list` / `tuple` / `set` / `dict`；
//! - 超大容器按 CPython 同款 1000 槽分批 `APPENDS` / `SETITEMS` / `ADDITEMS`，避免解码栈暴涨；
//! - 可选字符串驻留 (`MEMOIZE` + `BINGET`/`LONG_BINGET`) 显著压缩高频字典键与枚举值体积。

use std::collections::HashMap;
use std::fs::{self, File};
use std::io::{BufWriter, Write};
use std::path::Path;

const OP_PROTO: u8 = 0x80;
const OP_FRAME: u8 = 0x95;
const OP_STOP: u8 = 0x2E;

const OP_NONE: u8 = 0x4E;
const OP_NEWTRUE: u8 = 0x88;
const OP_NEWFALSE: u8 = 0x89;

const OP_BININT: u8 = 0x4A;
const OP_BININT1: u8 = 0x4B;
const OP_BININT2: u8 = 0x4D;
const OP_LONG1: u8 = 0x8A;
const OP_BINFLOAT: u8 = 0x47;

const OP_SHORT_BINUNICODE: u8 = 0x8C;
const OP_BINUNICODE: u8 = 0x58; // 4-byte u32 length (CPython 'X')
const OP_BINUNICODE8: u8 = 0x8D; // 8-byte u64 length (Protocol 4)

const OP_MARK: u8 = 0x28;
const OP_EMPTY_LIST: u8 = 0x5D;
const OP_APPENDS: u8 = 0x65;
const OP_EMPTY_DICT: u8 = 0x7D;
const OP_SETITEMS: u8 = 0x75;
const OP_EMPTY_SET: u8 = 0x8F;
const OP_ADDITEMS: u8 = 0x90;

const OP_TUPLE2: u8 = 0x86;

const OP_MEMOIZE: u8 = 0x94;
const OP_BINGET: u8 = 0x68;
const OP_LONG_BINGET: u8 = 0x6A;

const FRAME_TARGET: usize = 64 * 1024;
const BATCH_SIZE: usize = 1000;

/// 流式 Python Pickle Protocol 4 写入器。
pub struct PickleWriter {
    out: Vec<u8>,
    frame: Vec<u8>,
    memo: HashMap<String, u32>,
    memo_next: u32,
}

#[allow(dead_code)]
impl PickleWriter {
    pub fn new() -> Self {
        Self::with_capacity(64 * 1024)
    }

    pub fn with_capacity(cap: usize) -> Self {
        let mut out = Vec::with_capacity(cap);
        out.push(OP_PROTO);
        out.push(4);
        Self {
            out,
            frame: Vec::with_capacity(FRAME_TARGET + 4096),
            memo: HashMap::new(),
            memo_next: 0,
        }
    }

    #[inline]
    fn maybe_flush_frame(&mut self) {
        if self.frame.len() >= FRAME_TARGET {
            self.flush_frame();
        }
    }

    fn flush_frame(&mut self) {
        if self.frame.is_empty() {
            return;
        }
        self.out.push(OP_FRAME);
        self.out
            .extend_from_slice(&(self.frame.len() as u64).to_le_bytes());
        self.out.extend_from_slice(&self.frame);
        self.frame.clear();
    }

    #[inline]
    pub fn write_none(&mut self) {
        self.frame.push(OP_NONE);
    }

    #[inline]
    pub fn write_bool(&mut self, v: bool) {
        self.frame.push(if v { OP_NEWTRUE } else { OP_NEWFALSE });
    }

    #[inline]
    pub fn write_int(&mut self, v: i64) {
        if (0..=255).contains(&v) {
            self.frame.push(OP_BININT1);
            self.frame.push(v as u8);
        } else if (256..=65535).contains(&v) {
            self.frame.push(OP_BININT2);
            self.frame.extend_from_slice(&(v as u16).to_le_bytes());
        } else if (i32::MIN as i64..=i32::MAX as i64).contains(&v) {
            self.frame.push(OP_BININT);
            self.frame.extend_from_slice(&(v as i32).to_le_bytes());
        } else {
            // Two's-complement little-endian minimal representation for LONG1
            let raw = v.to_le_bytes();
            let mut len = raw.len();
            if v >= 0 {
                while len > 1 && raw[len - 1] == 0 && (raw[len - 2] & 0x80) == 0 {
                    len -= 1;
                }
            } else {
                while len > 1 && raw[len - 1] == 0xFF && (raw[len - 2] & 0x80) != 0 {
                    len -= 1;
                }
            }
            self.frame.push(OP_LONG1);
            self.frame.push(len as u8);
            self.frame.extend_from_slice(&raw[..len]);
        }
    }

    #[inline]
    pub fn write_float(&mut self, v: f64) {
        self.frame.push(OP_BINFLOAT);
        self.frame.extend_from_slice(&v.to_be_bytes());
    }

    #[inline]
    pub fn write_str(&mut self, s: &str) {
        let bytes = s.as_bytes();
        if bytes.len() <= 255 {
            self.frame.push(OP_SHORT_BINUNICODE);
            self.frame.push(bytes.len() as u8);
            self.frame.extend_from_slice(bytes);
        } else if bytes.len() <= u32::MAX as usize {
            self.frame.push(OP_BINUNICODE);
            self.frame
                .extend_from_slice(&(bytes.len() as u32).to_le_bytes());
            self.frame.extend_from_slice(bytes);
        } else {
            self.frame.push(OP_BINUNICODE8);
            self.frame
                .extend_from_slice(&(bytes.len() as u64).to_le_bytes());
            self.frame.extend_from_slice(bytes);
        }
    }

    /// 驻留写入高频短字符串（字典字段名、枚举标签、文件名等），大幅降低文件体积。
    pub fn write_str_intern(&mut self, s: &str) {
        if let Some(&idx) = self.memo.get(s) {
            if idx <= 255 {
                self.frame.push(OP_BINGET);
                self.frame.push(idx as u8);
            } else {
                self.frame.push(OP_LONG_BINGET);
                self.frame.extend_from_slice(&idx.to_le_bytes());
            }
            return;
        }
        self.write_str(s);
        let idx = self.memo_next;
        self.memo_next += 1;
        self.frame.push(OP_MEMOIZE);
        self.memo.insert(s.to_string(), idx);
    }

    #[inline]
    pub fn write_opt_str(&mut self, v: Option<&str>) {
        match v {
            Some(s) => self.write_str(s),
            None => self.write_none(),
        }
    }

    #[inline]
    pub fn write_opt_str_intern(&mut self, v: Option<&str>) {
        match v {
            Some(s) => self.write_str_intern(s),
            None => self.write_none(),
        }
    }

    #[inline]
    pub fn write_opt_int(&mut self, v: Option<i64>) {
        match v {
            Some(n) => self.write_int(n),
            None => self.write_none(),
        }
    }

    #[inline]
    pub fn write_opt_bool(&mut self, v: Option<bool>) {
        match v {
            Some(b) => self.write_bool(b),
            None => self.write_none(),
        }
    }

    pub fn write_list<T, F>(&mut self, items: &[T], mut f: F)
    where
        F: FnMut(&mut Self, &T),
    {
        self.frame.push(OP_EMPTY_LIST);
        for chunk in items.chunks(BATCH_SIZE) {
            self.frame.push(OP_MARK);
            for item in chunk {
                f(self, item);
            }
            self.frame.push(OP_APPENDS);
            self.maybe_flush_frame();
        }
    }

    pub fn write_list_iter<I, T, F>(&mut self, iter: I, mut f: F)
    where
        I: IntoIterator<Item = T>,
        F: FnMut(&mut Self, T),
    {
        self.frame.push(OP_EMPTY_LIST);
        let mut in_batch = 0usize;
        for item in iter {
            if in_batch == 0 {
                self.frame.push(OP_MARK);
            }
            f(self, item);
            in_batch += 1;
            if in_batch >= BATCH_SIZE {
                self.frame.push(OP_APPENDS);
                self.maybe_flush_frame();
                in_batch = 0;
            }
        }
        if in_batch > 0 {
            self.frame.push(OP_APPENDS);
            self.maybe_flush_frame();
        }
    }

    pub fn write_set_iter<I, T, F>(&mut self, iter: I, mut f: F)
    where
        I: IntoIterator<Item = T>,
        F: FnMut(&mut Self, T),
    {
        self.frame.push(OP_EMPTY_SET);
        let mut in_batch = 0usize;
        for item in iter {
            if in_batch == 0 {
                self.frame.push(OP_MARK);
            }
            f(self, item);
            in_batch += 1;
            if in_batch >= BATCH_SIZE {
                self.frame.push(OP_ADDITEMS);
                self.maybe_flush_frame();
                in_batch = 0;
            }
        }
        if in_batch > 0 {
            self.frame.push(OP_ADDITEMS);
            self.maybe_flush_frame();
        }
    }

    pub fn write_dict_iter<I, K, V, FK, FV>(&mut self, iter: I, mut fk: FK, mut fv: FV)
    where
        I: IntoIterator<Item = (K, V)>,
        FK: FnMut(&mut Self, K),
        FV: FnMut(&mut Self, V),
    {
        self.frame.push(OP_EMPTY_DICT);
        let mut in_batch = 0usize;
        for (k, v) in iter {
            if in_batch == 0 {
                self.frame.push(OP_MARK);
            }
            fk(self, k);
            fv(self, v);
            in_batch += 1;
            if in_batch >= BATCH_SIZE / 2 {
                self.frame.push(OP_SETITEMS);
                self.maybe_flush_frame();
                in_batch = 0;
            }
        }
        if in_batch > 0 {
            self.frame.push(OP_SETITEMS);
            self.maybe_flush_frame();
        }
    }

    /// 开始一个已知固定键数的小字典（配合 `kv_*` 与 `end_dict` 使用）。
    #[inline]
    pub fn begin_small_dict(&mut self) {
        self.frame.push(OP_EMPTY_DICT);
        self.frame.push(OP_MARK);
    }

    #[inline]
    pub fn end_small_dict(&mut self) {
        self.frame.push(OP_SETITEMS);
        self.maybe_flush_frame();
    }

    #[inline]
    pub fn write_tuple2<F1: FnOnce(&mut Self), F2: FnOnce(&mut Self)>(&mut self, f1: F1, f2: F2) {
        f1(self);
        f2(self);
        self.frame.push(OP_TUPLE2);
    }

    pub fn finish(mut self) -> Vec<u8> {
        self.frame.push(OP_STOP);
        self.flush_frame();
        self.out
    }

    pub fn save_to_file(self, path: &Path) -> Result<usize, String> {
        if let Some(parent) = path.parent() {
            if !parent.as_os_str().is_empty() {
                fs::create_dir_all(parent)
                    .map_err(|e| format!("创建目录失败 {}: {}", parent.display(), e))?;
            }
        }
        let bytes = self.finish();
        // 先写临时文件再原子替换，避免半写文件污染缓存
        let mut tmp_os = path.as_os_str().to_os_string();
        tmp_os.push(".tmp");
        let tmp = std::path::PathBuf::from(tmp_os);
        {
            let f = File::create(&tmp)
                .map_err(|e| format!("创建临时文件失败 {}: {}", tmp.display(), e))?;
            let mut bw = BufWriter::new(f);
            bw.write_all(&bytes)
                .map_err(|e| format!("写入 pickle 失败 {}: {}", tmp.display(), e))?;
            bw.flush()
                .map_err(|e| format!("刷盘失败 {}: {}", tmp.display(), e))?;
        }
        // Windows 上 rename 覆盖已有文件需先删除目标或使用 fs::rename 后备
        if path.exists() {
            let _ = fs::remove_file(path);
        }
        fs::rename(&tmp, path).map_err(|e| {
            let _ = fs::remove_file(&tmp);
            format!("替换目标文件失败 {}: {}", path.display(), e)
        })?;
        Ok(bytes.len())
    }
}
