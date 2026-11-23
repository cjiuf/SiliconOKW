//! Paradox 虚拟文件系统（VFS）：支持原版游戏目录 + 多层 Mod 覆盖 + `replace_path` + `dlc_load.json`。
//!
//! 严格对齐 Clausewitz 引擎资源加载顺序：
//! - 基础层为 `game_dir`，其上按加载顺序叠加 `mods[0..N]`（后者覆盖前者）；
//! - 任一 Mod 若对目录 `D` 声明了 `replace_path = "D"`，则屏蔽 `game_dir` 与更低层 Mod 中 `D` 的直接文件；
//! - 同相对路径文件（Windows 下大小写不敏感）由最高层生效；
//! - 支持 `.mod` 描述符文件、直接 Mod 目录（自动探测 `descriptor.mod`）以及 Launcher 的 `dlc_load.json`。

use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, HashSet};
use std::fs;
use std::path::{Path, PathBuf};
use std::time::UNIX_EPOCH;

#[derive(Debug, Clone, Serialize)]
pub struct ModLayer {
    pub name: String,
    pub root: PathBuf,
    pub descriptor: Option<String>,
    pub replace_paths: Vec<String>,
}

#[derive(Debug, Clone)]
pub struct VfsEntry {
    /// 相对于游戏/Mod 根目录的路径（正斜杠），如 `common/decisions/GER.txt`
    pub rel_path: String,
    /// 仅文件名，如 `GER.txt`
    pub file_name: String,
    /// 磁盘绝对路径
    pub disk_path: PathBuf,
    /// 所属层级索引：0 表示 vanilla，1..=N 表示 mods[idx-1]
    #[allow(dead_code)]
    pub layer_index: usize,
}

#[derive(Debug, Clone)]
pub struct PdxVfs {
    pub game_dir: PathBuf,
    pub mods: Vec<ModLayer>,
    pub warnings: Vec<String>,
}

#[derive(Deserialize)]
struct DlcLoadJson {
    #[serde(default)]
    enabled_mods: Vec<String>,
}

fn norm_rel(p: &str) -> String {
    p.replace('\\', "/")
        .trim_matches('/')
        .to_ascii_lowercase()
}

fn parse_mod_descriptor_text(text: &str) -> (Option<String>, Option<String>, Option<String>, Vec<String>) {
    let mut name: Option<String> = None;
    let mut path: Option<String> = None;
    let mut archive: Option<String> = None;
    let mut replace_paths: Vec<String> = Vec::new();

    for raw_line in text.lines() {
        let line = match raw_line.find('#') {
            Some(i) => &raw_line[..i],
            None => raw_line,
        }
        .trim();
        if line.is_empty() {
            continue;
        }
        let Some(eq) = line.find('=') else {
            continue;
        };
        let key = line[..eq].trim().to_ascii_lowercase();
        let rhs = line[eq + 1..].trim();
        let val = if rhs.starts_with('"') {
            rhs[1..].find('"').map(|end| rhs[1..1 + end].to_string())
        } else {
            None
        };
        let Some(val) = val else {
            continue;
        };
        match key.as_str() {
            "name" => name = Some(val),
            "path" => path = Some(val),
            "archive" => archive = Some(val),
            "replace_path" => {
                let nr = norm_rel(&val);
                if !nr.is_empty() && !replace_paths.contains(&nr) {
                    replace_paths.push(nr);
                }
            }
            _ => {}
        }
    }
    (name, path, archive, replace_paths)
}

fn resolve_mod_path(raw: &str, user_dir: Option<&Path>, desc_file: &Path) -> PathBuf {
    let p = PathBuf::from(raw);
    if p.is_absolute() {
        return p;
    }
    if let Some(ud) = user_dir {
        let cand = ud.join(&p);
        if cand.exists() {
            return cand;
        }
    }
    // 常见情况：<user_dir>/mod/foo.mod 中写 path="mod/foo"
    if let Some(mod_dir) = desc_file.parent() {
        if let Some(parent) = mod_dir.parent() {
            let cand = parent.join(&p);
            if cand.exists() {
                return cand;
            }
        }
        let cand = mod_dir.join(&p);
        if cand.exists() {
            return cand;
        }
    }
    p
}

impl PdxVfs {
    pub fn new(game_dir: PathBuf) -> Result<Self, String> {
        if !game_dir.is_dir() {
            return Err(format!("游戏目录不存在: {}", game_dir.display()));
        }
        Ok(Self {
            game_dir,
            mods: Vec::new(),
            warnings: Vec::new(),
        })
    }

    /// 从 `.mod` 文件或 Mod 根目录加载一层 Mod 覆盖。
    pub fn add_mod(&mut self, spec: &Path, user_dir: Option<&Path>) -> Result<(), String> {
        if spec.is_dir() {
            let mut name = spec
                .file_name()
                .map(|s| s.to_string_lossy().to_string())
                .unwrap_or_else(|| spec.display().to_string());
            let mut replace_paths = Vec::new();
            let desc = spec.join("descriptor.mod");
            let desc_str = if desc.is_file() {
                if let Ok(txt) = fs::read_to_string(&desc) {
                    let (n, _p, _a, rps) = parse_mod_descriptor_text(&txt);
                    if let Some(n) = n {
                        name = n;
                    }
                    replace_paths = rps;
                }
                Some(desc.display().to_string())
            } else {
                None
            };
            self.mods.push(ModLayer {
                name,
                root: spec.to_path_buf(),
                descriptor: desc_str,
                replace_paths,
            });
            return Ok(());
        }

        if spec.is_file() {
            let txt = fs::read_to_string(spec)
                .map_err(|e| format!("读取 .mod 描述符失败 {}: {}", spec.display(), e))?;
            let (name_opt, path_opt, archive_opt, mut replace_paths) =
                parse_mod_descriptor_text(&txt);
            let name = name_opt.unwrap_or_else(|| {
                spec.file_stem()
                    .map(|s| s.to_string_lossy().to_string())
                    .unwrap_or_else(|| spec.display().to_string())
            });
            if let Some(raw_p) = path_opt {
                let root = resolve_mod_path(&raw_p, user_dir, spec);
                if !root.is_dir() {
                    self.warnings.push(format!(
                        "Mod `{}` 指向目录不存在，已跳过: {}",
                        name,
                        root.display()
                    ));
                    return Ok(());
                }
                // 同时合并目录内 descriptor.mod 的 replace_path（某些 Steam 工坊 Mod 将 replace_path 写在内部）
                let inner_desc = root.join("descriptor.mod");
                if inner_desc.is_file() {
                    if let Ok(itxt) = fs::read_to_string(&inner_desc) {
                        let (_, _, _, irps) = parse_mod_descriptor_text(&itxt);
                        for rp in irps {
                            if !replace_paths.contains(&rp) {
                                replace_paths.push(rp);
                            }
                        }
                    }
                }
                self.mods.push(ModLayer {
                    name,
                    root,
                    descriptor: Some(spec.display().to_string()),
                    replace_paths,
                });
                return Ok(());
            }
            if let Some(arc) = archive_opt {
                self.warnings.push(format!(
                    "Mod `{}` 使用归档 archive=\"{}\"（未解压 zip），当前版本跳过该归档",
                    name, arc
                ));
                return Ok(());
            }
            return Err(format!(
                ".mod 描述符缺少 path/archive 字段: {}",
                spec.display()
            ));
        }

        Err(format!("Mod 路径不存在: {}", spec.display()))
    }

    /// 读取 Paradox Launcher 的 `dlc_load.json`，自动叠加全部 `enabled_mods`。
    pub fn add_from_dlc_load(
        &mut self,
        dlc_load_path: &Path,
        user_dir: Option<&Path>,
    ) -> Result<usize, String> {
        let raw = fs::read_to_string(dlc_load_path)
            .map_err(|e| format!("读取 dlc_load.json 失败 {}: {}", dlc_load_path.display(), e))?;
        let parsed: DlcLoadJson = serde_json::from_str(&raw)
            .map_err(|e| format!("解析 dlc_load.json 失败 {}: {}", dlc_load_path.display(), e))?;
        let base = user_dir
            .map(|p| p.to_path_buf())
            .or_else(|| dlc_load_path.parent().map(|p| p.to_path_buf()))
            .unwrap_or_else(|| PathBuf::from("."));

        let before = self.mods.len();
        for rel in &parsed.enabled_mods {
            let p = PathBuf::from(rel);
            let full = if p.is_absolute() { p } else { base.join(&p) };
            if full.exists() {
                self.add_mod(&full, Some(&base))?;
            } else {
                self.warnings.push(format!(
                    "dlc_load.json 中启用的 Mod 描述符不存在，已跳过: {}",
                    full.display()
                ));
            }
        }
        Ok(self.mods.len() - before)
    }

    /// 层列表：0 = game_dir, 1..=N = mods[i-1]
    fn layer_roots(&self) -> Vec<&Path> {
        let mut v = Vec::with_capacity(1 + self.mods.len());
        v.push(self.game_dir.as_path());
        for m in &self.mods {
            v.push(m.root.as_path());
        }
        v
    }

    /// 计算目录 `rel_dir` 生效的起始层索引（受 `replace_path` 拦截）。
    /// 返回 `start_layer`：只有 `layer_idx >= start_layer` 的层能为该目录提供文件。
    fn effective_start_layer(&self, rel_dir: &str) -> usize {
        let key = norm_rel(rel_dir);
        let mut start = 0usize;
        for (i, m) in self.mods.iter().enumerate() {
            if m.replace_paths.iter().any(|rp| rp == &key) {
                start = i + 1;
            }
        }
        start
    }

    /// 查找单个文件（从最高层往下，尊重父目录的 `replace_path`）。
    pub fn resolve_file(&self, rel: &str) -> Option<PathBuf> {
        let norm = rel.replace('\\', "/");
        let parent_dir = norm.rsplit_once('/').map(|(d, _)| d).unwrap_or("");
        let start = self.effective_start_layer(parent_dir);
        let roots = self.layer_roots();
        for idx in (start..roots.len()).rev() {
            let cand = roots[idx].join(&norm);
            if cand.is_file() {
                return Some(cand);
            }
        }
        None
    }

    /// 收集指定单层目录下的全部匹配扩展名文件（已处理 `replace_path` 与同名覆盖，结果按文件名排序）。
    pub fn list_dir_files(&self, rel_dir: &str, ext_lower: &str) -> Vec<VfsEntry> {
        let norm_dir = rel_dir.replace('\\', "/").trim_matches('/').to_string();
        let start = self.effective_start_layer(&norm_dir);
        let roots = self.layer_roots();

        // key: 小写文件名 -> VfsEntry（高层覆盖低层）
        let mut map: BTreeMap<String, VfsEntry> = BTreeMap::new();
        for idx in start..roots.len() {
            let dir = if norm_dir.is_empty() {
                roots[idx].to_path_buf()
            } else {
                roots[idx].join(&norm_dir)
            };
            let Ok(rd) = fs::read_dir(&dir) else {
                continue;
            };
            for ent in rd.flatten() {
                let path = ent.path();
                if !path.is_file() {
                    continue;
                }
                let fname = ent.file_name().to_string_lossy().to_string();
                let fl = fname.to_ascii_lowercase();
                if !ext_lower.is_empty() && !fl.ends_with(ext_lower) {
                    continue;
                }
                let rel_path = if norm_dir.is_empty() {
                    fname.clone()
                } else {
                    format!("{}/{}", norm_dir, fname)
                };
                map.insert(
                    fl,
                    VfsEntry {
                        rel_path,
                        file_name: fname,
                        disk_path: path,
                        layer_index: idx,
                    },
                );
            }
        }

        let mut out: Vec<VfsEntry> = map.into_values().collect();
        out.sort_by(|a, b| a.file_name.cmp(&b.file_name));
        out
    }

    /// 递归收集指定目录下的全部匹配扩展名文件，支持跳过指定目录名（如 `categories`），
    /// 并对每一级子目录独立遵循 `replace_path`。
    pub fn walk_dir_files(
        &self,
        rel_dir: &str,
        ext_lower: &str,
        skip_dir_names_lower: &[&str],
    ) -> Vec<VfsEntry> {
        let norm_root = rel_dir.replace('\\', "/").trim_matches('/').to_string();
        let roots = self.layer_roots();

        // 第一步：发现所有层级下出现过的相对子目录
        let mut rel_dirs: Vec<String> = Vec::new();
        let mut seen_dirs: HashSet<String> = HashSet::new();
        let mut queue: Vec<String> = vec![norm_root.clone()];
        seen_dirs.insert(norm_root.to_ascii_lowercase());

        while let Some(cur_rel) = queue.pop() {
            rel_dirs.push(cur_rel.clone());
            for root in &roots {
                let d = if cur_rel.is_empty() {
                    root.to_path_buf()
                } else {
                    root.join(&cur_rel)
                };
                let Ok(rd) = fs::read_dir(&d) else {
                    continue;
                };
                for ent in rd.flatten() {
                    let p = ent.path();
                    if !p.is_dir() {
                        continue;
                    }
                    let dname = ent.file_name().to_string_lossy().to_string();
                    let dl = dname.to_ascii_lowercase();
                    if skip_dir_names_lower.iter().any(|s| *s == dl) {
                        continue;
                    }
                    let sub_rel = if cur_rel.is_empty() {
                        dname
                    } else {
                        format!("{}/{}", cur_rel, dname)
                    };
                    let key = sub_rel.to_ascii_lowercase();
                    if seen_dirs.insert(key) {
                        queue.push(sub_rel);
                    }
                }
            }
        }

        rel_dirs.sort();

        // 第二步：逐子目录按 effective_start_layer 收集文件，高层覆盖低层
        let mut map: BTreeMap<String, VfsEntry> = BTreeMap::new();
        for d_rel in &rel_dirs {
            let start = self.effective_start_layer(d_rel);
            for idx in start..roots.len() {
                let dir = if d_rel.is_empty() {
                    roots[idx].to_path_buf()
                } else {
                    roots[idx].join(d_rel)
                };
                let Ok(rd) = fs::read_dir(&dir) else {
                    continue;
                };
                for ent in rd.flatten() {
                    let path = ent.path();
                    if !path.is_file() {
                        continue;
                    }
                    let fname = ent.file_name().to_string_lossy().to_string();
                    let fl = fname.to_ascii_lowercase();
                    if !ext_lower.is_empty() && !fl.ends_with(ext_lower) {
                        continue;
                    }
                    let rel_path = if d_rel.is_empty() {
                        fname.clone()
                    } else {
                        format!("{}/{}", d_rel, fname)
                    };
                    let rel_key = rel_path.to_ascii_lowercase();
                    map.insert(
                        rel_key,
                        VfsEntry {
                            rel_path,
                            file_name: fname,
                            disk_path: path,
                            layer_index: idx,
                        },
                    );
                }
            }
        }

        let mut out: Vec<VfsEntry> = map.into_values().collect();
        out.sort_by(|a, b| a.rel_path.cmp(&b.rel_path));
        out
    }

    /// 列出 `localisation/` 下可用的语言目录名（English 优先，排除 `replace`）。
    pub fn list_languages(&self) -> Vec<String> {
        let mut set: HashSet<String> = HashSet::new();
        for root in self.layer_roots() {
            let d = root.join("localisation");
            let Ok(rd) = fs::read_dir(&d) else {
                continue;
            };
            for ent in rd.flatten() {
                if !ent.path().is_dir() {
                    continue;
                }
                let name = ent.file_name().to_string_lossy().to_string();
                if name.eq_ignore_ascii_case("replace") {
                    continue;
                }
                set.insert(name);
            }
        }
        let mut langs: Vec<String> = set.into_iter().collect();
        langs.sort_by(|a, b| {
            let ea = if a.to_ascii_lowercase().starts_with("english") { 0 } else { 1 };
            let eb = if b.to_ascii_lowercase().starts_with("english") { 0 } else { 1 };
            (ea, a).cmp(&(eb, b))
        });
        langs
    }

    /// 计算目录文件数与最大 mtime（秒），用于与 `equipmeta.py` 的 `_dir_sig` 保持完全兼容。
    pub fn dir_sig(&self, rel_dir: &str, ext_lower: &str) -> (usize, i64) {
        let files = self.list_dir_files(rel_dir, ext_lower);
        let mut max_mtime = 0i64;
        for f in &files {
            if let Ok(meta) = fs::metadata(&f.disk_path) {
                if let Ok(mt) = meta.modified() {
                    if let Ok(dur) = mt.duration_since(UNIX_EPOCH) {
                        let secs = dur.as_secs() as i64;
                        if secs > max_mtime {
                            max_mtime = secs;
                        }
                    }
                }
            }
        }
        (files.len(), max_mtime)
    }
}
