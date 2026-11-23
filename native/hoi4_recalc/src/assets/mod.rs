//! `hoi4_recalc assets` 子命令：多线程一键并行构建 HOI4 全部 Paradox 静态表。
//!
//! 覆盖 11 项核心静态表（原版 + 任意 Mod VFS 覆盖 + `replace_path`）：
//!   loc / mapadj / maprivers / mapstates / statemeta / navalregions /
//!   techdefs / focusplans / decisions_meta / equipmeta / evdb

pub mod events;
pub mod loc;
pub mod map;
pub mod scripts;

use crate::vfs::{ModLayer, PdxVfs};
use serde::Serialize;
use std::collections::{BTreeMap, HashSet};
use std::fs;
use std::path::{Path, PathBuf};
use std::sync::Mutex;
use std::time::{Instant, SystemTime, UNIX_EPOCH};

const ALL_TARGETS: [&str; 11] = [
    "loc",
    "mapadj",
    "maprivers",
    "mapstates",
    "statemeta",
    "navalregions",
    "techdefs",
    "focusplans",
    "decisions_meta",
    "equipmeta",
    "evdb",
];

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum OutFormat {
    Pkl,
    Json,
    Both,
}

impl OutFormat {
    fn as_str(self) -> &'static str {
        match self {
            OutFormat::Pkl => "pkl",
            OutFormat::Json => "json",
            OutFormat::Both => "both",
        }
    }
    fn write_pkl(self) -> bool {
        matches!(self, OutFormat::Pkl | OutFormat::Both)
    }
    fn write_json(self) -> bool {
        matches!(self, OutFormat::Json | OutFormat::Both)
    }
}

#[derive(Debug, Clone, Serialize)]
pub struct AssetRecord {
    pub items: usize,
    pub pkl_path: Option<String>,
    pub pkl_bytes: usize,
    pub json_path: Option<String>,
    pub json_bytes: usize,
    pub ms: u128,
}

#[derive(Debug, Clone, Serialize)]
pub struct AssetsManifest {
    pub schema_version: u32,
    pub tool: String,
    pub built_at_unix: u64,
    pub game_dir: String,
    pub lang: String,
    pub format: String,
    pub mods: Vec<ModLayer>,
    pub warnings: Vec<String>,
    pub total_ms: u128,
    pub assets: BTreeMap<String, AssetRecord>,
}

fn write_json_file<T: Serialize>(val: &T, path: &Path) -> Result<usize, String> {
    if let Some(parent) = path.parent() {
        if !parent.as_os_str().is_empty() {
            fs::create_dir_all(parent)
                .map_err(|e| format!("创建目录失败 {}: {}", parent.display(), e))?;
        }
    }
    let s = serde_json::to_string(val).map_err(|e| format!("JSON 序列化失败: {}", e))?;
    let bytes = s.as_bytes();
    fs::write(path, bytes).map_err(|e| format!("写入 {} 失败: {}", path.display(), e))?;
    Ok(bytes.len())
}

fn write_text_file(path: &Path, content: &str) -> Result<(), String> {
    if let Some(parent) = path.parent() {
        if !parent.as_os_str().is_empty() {
            fs::create_dir_all(parent)
                .map_err(|e| format!("创建目录失败 {}: {}", parent.display(), e))?;
        }
    }
    fs::write(path, content).map_err(|e| format!("写入 {} 失败: {}", path.display(), e))
}

fn parse_targets(spec: Option<&str>) -> Result<HashSet<String>, String> {
    let mut set = HashSet::new();
    match spec {
        None => {
            for &t in &ALL_TARGETS {
                set.insert(t.to_string());
            }
        }
        Some(raw) => {
            for tok in raw.split(',').map(|s| s.trim().to_ascii_lowercase()) {
                if tok.is_empty() {
                    continue;
                }
                if tok == "all" {
                    for &t in &ALL_TARGETS {
                        set.insert(t.to_string());
                    }
                    continue;
                }
                if !ALL_TARGETS.contains(&tok.as_str()) {
                    return Err(format!(
                        "未知资产名: `{}`（可选: {}）",
                        tok,
                        ALL_TARGETS.join(",")
                    ));
                }
                set.insert(tok);
            }
            if set.is_empty() {
                return Err("--only 列表为空".to_string());
            }
        }
    }
    Ok(set)
}

fn default_evdb_path(out_dir: &Path) -> PathBuf {
    // 若 out_dir 指向仓库的 `calling/` 目录，则自动把 evdb_win.pkl 写到 `../research/windows/evdb_win.pkl`
    if let Some(parent) = out_dir.parent() {
        let rw = parent.join("research").join("windows");
        if rw.is_dir() {
            return rw.join("evdb_win.pkl");
        }
    }
    out_dir.join("evdb_win.pkl")
}

fn render_assets_report(m: &AssetsManifest) -> String {
    let mut s = String::new();
    s.push_str("# HOI4 静态资产重建报告 (`hoi4_recalc assets`)\n\n");
    s.push_str(&format!("- 工具版本：`{}`\n", m.tool));
    s.push_str(&format!("- 游戏目录：`{}`\n", m.game_dir));
    s.push_str(&format!("- 主语言：`{}`\n", m.lang));
    s.push_str(&format!("- 输出格式：`{}`\n", m.format));
    s.push_str(&format!("- 启用 Mod 数：`{}`\n", m.mods.len()));
    for (idx, md) in m.mods.iter().enumerate() {
        s.push_str(&format!(
            "  - [{}] `{}` (`{}`), `replace_path` = {} 项\n",
            idx + 1,
            md.name,
            md.root.display(),
            md.replace_paths.len()
        ));
    }
    s.push_str(&format!("- 总耗时：**{} ms**\n\n", m.total_ms));
    s.push_str("| 资产 | 条目数 | Pickle 大小 | JSON 大小 | 耗时 |\n");
    s.push_str("| --- | ---: | ---: | ---: | ---: |\n");
    for (name, r) in &m.assets {
        let pkb = if r.pkl_bytes > 0 {
            format!("{:.1} KB", r.pkl_bytes as f64 / 1024.0)
        } else {
            "-".to_string()
        };
        let jkb = if r.json_bytes > 0 {
            format!("{:.1} KB", r.json_bytes as f64 / 1024.0)
        } else {
            "-".to_string()
        };
        s.push_str(&format!(
            "| `{}` | {} | {} | {} | {} ms |\n",
            name, r.items, pkb, jkb, r.ms
        ));
    }
    if !m.warnings.is_empty() {
        s.push_str("\n## 警告\n\n");
        for w in &m.warnings {
            s.push_str(&format!("- {}\n", w));
        }
    }
    s
}

pub fn cmd_assets(args: &[String]) -> Result<i32, String> {
    let mut game_dir: Option<PathBuf> = None;
    let mut out_dir: Option<PathBuf> = None;
    let mut evdb_out: Option<PathBuf> = None;
    let mut user_dir: Option<PathBuf> = None;
    let mut dlc_load: Option<PathBuf> = None;
    let mut mod_specs: Vec<PathBuf> = Vec::new();
    let mut lang_opt: Option<String> = None;
    let mut only_opt: Option<String> = None;
    let mut fmt = OutFormat::Pkl;
    let mut manifest_path: Option<PathBuf> = None;
    let mut report_path: Option<PathBuf> = None;

    let mut i = 0usize;
    while i < args.len() {
        match args[i].as_str() {
            "--game" => {
                i += 1;
                game_dir = args.get(i).map(PathBuf::from);
            }
            "--out-dir" => {
                i += 1;
                out_dir = args.get(i).map(PathBuf::from);
            }
            "--evdb-out" => {
                i += 1;
                evdb_out = args.get(i).map(PathBuf::from);
            }
            "--user-dir" => {
                i += 1;
                user_dir = args.get(i).map(PathBuf::from);
            }
            "--dlc-load" => {
                i += 1;
                dlc_load = args.get(i).map(PathBuf::from);
            }
            "--mod" => {
                i += 1;
                let p = args
                    .get(i)
                    .ok_or_else(|| "--mod 缺少路径参数".to_string())?;
                mod_specs.push(PathBuf::from(p));
            }
            "--lang" => {
                i += 1;
                lang_opt = args.get(i).cloned();
            }
            "--only" => {
                i += 1;
                only_opt = args.get(i).cloned();
            }
            "--format" => {
                i += 1;
                let s = args
                    .get(i)
                    .ok_or_else(|| "--format 缺少参数 (pkl|json|both)".to_string())?;
                fmt = match s.to_ascii_lowercase().as_str() {
                    "pkl" => OutFormat::Pkl,
                    "json" => OutFormat::Json,
                    "both" => OutFormat::Both,
                    other => return Err(format!("未知 --format 值: {}（支持 pkl|json|both）", other)),
                };
            }
            "--manifest" => {
                i += 1;
                manifest_path = args.get(i).map(PathBuf::from);
            }
            "--report" => {
                i += 1;
                report_path = args.get(i).map(PathBuf::from);
            }
            "-h" | "--help" => {
                eprintln!(
                    "用法:\n\
                     \x20 hoi4_recalc assets --game <game_dir> [--out-dir <calling_dir>]\n\
                     \x20                    [--evdb-out <evdb_win.pkl>] [--mod <dir_or_.mod>]...\n\
                     \x20                    [--dlc-load <dlc_load.json>] [--user-dir <user_dir>]\n\
                     \x20                    [--lang <english|simp_chinese|...>]\n\
                     \x20                    [--only <loc,mapadj,maprivers,mapstates,statemeta,navalregions,techdefs,focusplans,decisions_meta,equipmeta,evdb>]\n\
                     \x20                    [--format <pkl|json|both>] [--manifest <manifest.json>] [--report <report.md>]"
                );
                return Ok(2);
            }
            other => return Err(format!("未知参数: {}", other)),
        }
        i += 1;
    }

    let gdir = game_dir.ok_or_else(|| "缺少必填参数 --game <game_dir>".to_string())?;
    let odir = out_dir.unwrap_or_else(|| PathBuf::from("."));
    fs::create_dir_all(&odir)
        .map_err(|e| format!("创建输出目录失败 {}: {}", odir.display(), e))?;
    let evdb_path = evdb_out.unwrap_or_else(|| default_evdb_path(&odir));

    let targets = parse_targets(only_opt.as_deref())?;
    let want = |k: &str| targets.contains(k);

    let t_total = Instant::now();
    let mut vfs = PdxVfs::new(gdir.clone())?;
    if let Some(ref dl) = dlc_load {
        vfs.add_from_dlc_load(dl, user_dir.as_deref())?;
    }
    for ms in &mod_specs {
        vfs.add_mod(ms, user_dir.as_deref())?;
    }

    let primary_lang = lang_opt.clone().unwrap_or_else(|| {
        vfs.list_languages()
            .into_iter()
            .next()
            .unwrap_or_else(|| "english".to_string())
    });

    let records: Mutex<BTreeMap<String, AssetRecord>> = Mutex::new(BTreeMap::new());
    let first_err: Mutex<Option<String>> = Mutex::new(None);
    let set_err = |msg: String| {
        let mut g = first_err.lock().unwrap();
        if g.is_none() {
            *g = Some(msg);
        }
    };

    let want_loc = want("loc");
    let want_naval = want("navalregions");
    let want_evdb = want("evdb");
    let want_adj = want("mapadj");
    let want_riv = want("maprivers");
    let want_states = want("mapstates");
    let want_statemeta = want("statemeta");
    let want_tech = want("techdefs");
    let want_focus = want("focusplans");
    let want_dec = want("decisions_meta");
    let want_eq = want("equipmeta");

    let need_primary_loc = want_loc || want_naval || (want_evdb && (primary_lang == "english" || primary_lang == "simp_chinese"));
    let need_extra_en = want_evdb && primary_lang != "english";
    let need_extra_zh = want_evdb && primary_lang != "simp_chinese";

    let mut loc_primary: Option<(loc::LocData, u128)> = None;
    let mut loc_extra_en: Option<BTreeMap<String, String>> = None;
    let mut loc_extra_zh: Option<BTreeMap<String, String>> = None;

    // 阶段一：全部无依赖任务多核并行解析 + 直接落盘
    rayon::scope(|s| {
        if need_primary_loc {
            s.spawn(|_| {
                let t0 = Instant::now();
                let data = loc::build_loc_for_lang(&vfs, Some(&primary_lang));
                let parse_ms = t0.elapsed().as_millis();
                if want_loc {
                    let mut pkl_path = None;
                    let mut pkl_bytes = 0usize;
                    let mut json_path = None;
                    let mut json_bytes = 0usize;
                    if fmt.write_pkl() {
                        let p = odir.join("loc.pkl");
                        match loc::write_loc_pkl(&data, &p) {
                            Ok(sz) => {
                                pkl_bytes = sz;
                                pkl_path = Some(p.display().to_string());
                            }
                            Err(e) => set_err(e),
                        }
                    }
                    if fmt.write_json() {
                        let p = odir.join("loc.json");
                        match write_json_file(&data, &p) {
                            Ok(sz) => {
                                json_bytes = sz;
                                json_path = Some(p.display().to_string());
                            }
                            Err(e) => set_err(e),
                        }
                    }
                    records.lock().unwrap().insert(
                        "loc".to_string(),
                        AssetRecord {
                            items: data.count,
                            pkl_path,
                            pkl_bytes,
                            json_path,
                            json_bytes,
                            ms: t0.elapsed().as_millis(),
                        },
                    );
                }
                loc_primary = Some((data, parse_ms));
            });
        }

        if need_extra_en {
            s.spawn(|_| {
                loc_extra_en = Some(loc::build_loc_for_lang(&vfs, Some("english")).keys);
            });
        }

        if need_extra_zh {
            s.spawn(|_| {
                loc_extra_zh = Some(loc::build_loc_for_lang(&vfs, Some("simp_chinese")).keys);
            });
        }

        if want_adj || want_riv {
            s.spawn(|_| {
                let t0 = Instant::now();
                match map::build_map_bmps(&vfs, want_adj, want_riv) {
                    Ok(out) => {
                        let scan_ms = t0.elapsed().as_millis();
                        if want_adj {
                            let tw = Instant::now();
                            let mut pkl_path = None;
                            let mut pkl_bytes = 0usize;
                            let mut json_path = None;
                            let mut json_bytes = 0usize;
                            if fmt.write_pkl() {
                                let p = odir.join("mapadj.pkl");
                                match map::write_mapadj_pkl(&out.adj, &p) {
                                    Ok(sz) => {
                                        pkl_bytes = sz;
                                        pkl_path = Some(p.display().to_string());
                                    }
                                    Err(e) => set_err(e),
                                }
                            }
                            if fmt.write_json() {
                                let p = odir.join("mapadj.json");
                                match write_json_file(&out.adj, &p) {
                                    Ok(sz) => {
                                        json_bytes = sz;
                                        json_path = Some(p.display().to_string());
                                    }
                                    Err(e) => set_err(e),
                                }
                            }
                            records.lock().unwrap().insert(
                                "mapadj".to_string(),
                                AssetRecord {
                                    items: out.adj.len(),
                                    pkl_path,
                                    pkl_bytes,
                                    json_path,
                                    json_bytes,
                                    ms: scan_ms + tw.elapsed().as_millis(),
                                },
                            );
                        }
                        if want_riv {
                            let tw = Instant::now();
                            let mut pkl_path = None;
                            let mut pkl_bytes = 0usize;
                            let mut json_path = None;
                            let mut json_bytes = 0usize;
                            if fmt.write_pkl() {
                                let p = odir.join("maprivers.pkl");
                                match map::write_maprivers_pkl(&out.rivers, &p) {
                                    Ok(sz) => {
                                        pkl_bytes = sz;
                                        pkl_path = Some(p.display().to_string());
                                    }
                                    Err(e) => set_err(e),
                                }
                            }
                            if fmt.write_json() {
                                let p = odir.join("maprivers.json");
                                let jmap: BTreeMap<String, &String> = out
                                    .rivers
                                    .iter()
                                    .map(|((a, b), v)| (format!("{},{}", a, b), v))
                                    .collect();
                                match write_json_file(&jmap, &p) {
                                    Ok(sz) => {
                                        json_bytes = sz;
                                        json_path = Some(p.display().to_string());
                                    }
                                    Err(e) => set_err(e),
                                }
                            }
                            records.lock().unwrap().insert(
                                "maprivers".to_string(),
                                AssetRecord {
                                    items: out.rivers.len(),
                                    pkl_path,
                                    pkl_bytes,
                                    json_path,
                                    json_bytes,
                                    ms: scan_ms + tw.elapsed().as_millis(),
                                },
                            );
                        }
                    }
                    Err(e) => set_err(e),
                }
            });
        }

        if want_states || want_statemeta {
            s.spawn(|_| {
                let t0 = Instant::now();
                let (mstates, smeta) = map::build_states_and_meta(&vfs);
                let scan_ms = t0.elapsed().as_millis();
                if want_states {
                    let tw = Instant::now();
                    let mut pkl_path = None;
                    let mut pkl_bytes = 0usize;
                    let mut json_path = None;
                    let mut json_bytes = 0usize;
                    if fmt.write_pkl() {
                        let p = odir.join("mapstates.pkl");
                        match map::write_mapstates_pkl(&mstates, &p) {
                            Ok(sz) => {
                                pkl_bytes = sz;
                                pkl_path = Some(p.display().to_string());
                            }
                            Err(e) => set_err(e),
                        }
                    }
                    if fmt.write_json() {
                        let p = odir.join("mapstates.json");
                        match write_json_file(&mstates, &p) {
                            Ok(sz) => {
                                json_bytes = sz;
                                json_path = Some(p.display().to_string());
                            }
                            Err(e) => set_err(e),
                        }
                    }
                    records.lock().unwrap().insert(
                        "mapstates".to_string(),
                        AssetRecord {
                            items: mstates.st2p.len(),
                            pkl_path,
                            pkl_bytes,
                            json_path,
                            json_bytes,
                            ms: scan_ms + tw.elapsed().as_millis(),
                        },
                    );
                }
                if want_statemeta {
                    let tw = Instant::now();
                    let mut pkl_path = None;
                    let mut pkl_bytes = 0usize;
                    let mut json_path = None;
                    let mut json_bytes = 0usize;
                    if fmt.write_pkl() {
                        let p = odir.join("statemeta.pkl");
                        match map::write_statemeta_pkl(&smeta, &p) {
                            Ok(sz) => {
                                pkl_bytes = sz;
                                pkl_path = Some(p.display().to_string());
                            }
                            Err(e) => set_err(e),
                        }
                    }
                    if fmt.write_json() {
                        let p = odir.join("statemeta.json");
                        match write_json_file(&smeta, &p) {
                            Ok(sz) => {
                                json_bytes = sz;
                                json_path = Some(p.display().to_string());
                            }
                            Err(e) => set_err(e),
                        }
                    }
                    records.lock().unwrap().insert(
                        "statemeta".to_string(),
                        AssetRecord {
                            items: smeta.states.len(),
                            pkl_path,
                            pkl_bytes,
                            json_path,
                            json_bytes,
                            ms: scan_ms + tw.elapsed().as_millis(),
                        },
                    );
                }
            });
        }

        if want_tech {
            s.spawn(|_| {
                let t0 = Instant::now();
                let data = scripts::build_techdefs(&vfs);
                let mut pkl_path = None;
                let mut pkl_bytes = 0usize;
                let mut json_path = None;
                let mut json_bytes = 0usize;
                if fmt.write_pkl() {
                    let p = odir.join("techdefs.pkl");
                    match scripts::write_techdefs_pkl(&data, &p) {
                        Ok(sz) => {
                            pkl_bytes = sz;
                            pkl_path = Some(p.display().to_string());
                        }
                        Err(e) => set_err(e),
                    }
                }
                if fmt.write_json() {
                    let p = odir.join("techdefs.json");
                    match write_json_file(&data, &p) {
                        Ok(sz) => {
                            json_bytes = sz;
                            json_path = Some(p.display().to_string());
                        }
                        Err(e) => set_err(e),
                    }
                }
                records.lock().unwrap().insert(
                    "techdefs".to_string(),
                    AssetRecord {
                        items: data.len(),
                        pkl_path,
                        pkl_bytes,
                        json_path,
                        json_bytes,
                        ms: t0.elapsed().as_millis(),
                    },
                );
            });
        }

        if want_focus {
            s.spawn(|_| {
                let t0 = Instant::now();
                let data = scripts::build_focusplans(&vfs);
                let mut pkl_path = None;
                let mut pkl_bytes = 0usize;
                let mut json_path = None;
                let mut json_bytes = 0usize;
                if fmt.write_pkl() {
                    let p = odir.join("focusplans.pkl");
                    match scripts::write_focusplans_pkl(&data, &p) {
                        Ok(sz) => {
                            pkl_bytes = sz;
                            pkl_path = Some(p.display().to_string());
                        }
                        Err(e) => set_err(e),
                    }
                }
                if fmt.write_json() {
                    let p = odir.join("focusplans.json");
                    match write_json_file(&data, &p) {
                        Ok(sz) => {
                            json_bytes = sz;
                            json_path = Some(p.display().to_string());
                        }
                        Err(e) => set_err(e),
                    }
                }
                records.lock().unwrap().insert(
                    "focusplans".to_string(),
                    AssetRecord {
                        items: data.focuses.len(),
                        pkl_path,
                        pkl_bytes,
                        json_path,
                        json_bytes,
                        ms: t0.elapsed().as_millis(),
                    },
                );
            });
        }

        if want_dec {
            s.spawn(|_| {
                let t0 = Instant::now();
                let data = scripts::build_decisions_meta(&vfs);
                let mut pkl_path = None;
                let mut pkl_bytes = 0usize;
                let mut json_path = None;
                let mut json_bytes = 0usize;
                if fmt.write_pkl() {
                    let p = odir.join("decisions_meta.pkl");
                    match scripts::write_decisions_meta_pkl(&data, &p) {
                        Ok(sz) => {
                            pkl_bytes = sz;
                            pkl_path = Some(p.display().to_string());
                        }
                        Err(e) => set_err(e),
                    }
                }
                if fmt.write_json() {
                    let p = odir.join("decisions_meta.json");
                    match write_json_file(&data, &p) {
                        Ok(sz) => {
                            json_bytes = sz;
                            json_path = Some(p.display().to_string());
                        }
                        Err(e) => set_err(e),
                    }
                }
                records.lock().unwrap().insert(
                    "decisions_meta".to_string(),
                    AssetRecord {
                        items: data.count,
                        pkl_path,
                        pkl_bytes,
                        json_path,
                        json_bytes,
                        ms: t0.elapsed().as_millis(),
                    },
                );
            });
        }

        if want_eq {
            s.spawn(|_| {
                let t0 = Instant::now();
                let data = scripts::build_equipmeta(&vfs);
                let mut pkl_path = None;
                let mut pkl_bytes = 0usize;
                let mut json_path = None;
                let mut json_bytes = 0usize;
                if fmt.write_pkl() {
                    let p = odir.join("equipmeta.pkl");
                    match scripts::write_equipmeta_pkl(&data, &p) {
                        Ok(sz) => {
                            pkl_bytes = sz;
                            pkl_path = Some(p.display().to_string());
                        }
                        Err(e) => set_err(e),
                    }
                }
                if fmt.write_json() {
                    let p = odir.join("equipmeta.json");
                    match write_json_file(&data, &p) {
                        Ok(sz) => {
                            json_bytes = sz;
                            json_path = Some(p.display().to_string());
                        }
                        Err(e) => set_err(e),
                    }
                }
                records.lock().unwrap().insert(
                    "equipmeta".to_string(),
                    AssetRecord {
                        items: data.equipments.len(),
                        pkl_path,
                        pkl_bytes,
                        json_path,
                        json_bytes,
                        ms: t0.elapsed().as_millis(),
                    },
                );
            });
        }
    });

    // 阶段二：复用已解析好的本地化字典，并行构建 navalregions 与 evdb
    let primary_keys = loc_primary.as_ref().map(|(d, _)| &d.keys);
    let en_keys = if primary_lang == "english" {
        primary_keys
    } else {
        loc_extra_en.as_ref()
    };
    let zh_keys = if primary_lang == "simp_chinese" {
        primary_keys
    } else {
        loc_extra_zh.as_ref()
    };

    rayon::scope(|s| {
        if want_naval {
            s.spawn(|_| {
                let t0 = Instant::now();
                let data = map::build_naval_regions(&vfs, primary_keys);
                let mut pkl_path = None;
                let mut pkl_bytes = 0usize;
                let mut json_path = None;
                let mut json_bytes = 0usize;
                if fmt.write_pkl() {
                    let p = odir.join("navalregions.pkl");
                    match map::write_naval_regions_pkl(&data, &p) {
                        Ok(sz) => {
                            pkl_bytes = sz;
                            pkl_path = Some(p.display().to_string());
                        }
                        Err(e) => set_err(e),
                    }
                }
                if fmt.write_json() {
                    let p = odir.join("navalregions.json");
                    match write_json_file(&data, &p) {
                        Ok(sz) => {
                            json_bytes = sz;
                            json_path = Some(p.display().to_string());
                        }
                        Err(e) => set_err(e),
                    }
                }
                records.lock().unwrap().insert(
                    "navalregions".to_string(),
                    AssetRecord {
                        items: data.regions.len(),
                        pkl_path,
                        pkl_bytes,
                        json_path,
                        json_bytes,
                        ms: t0.elapsed().as_millis(),
                    },
                );
            });
        }

        if want_evdb {
            s.spawn(|_| {
                let t0 = Instant::now();
                let db = events::build_evdb(&vfs, en_keys, zh_keys);
                let mut pkl_path = None;
                let mut pkl_bytes = 0usize;
                let mut json_path = None;
                let mut json_bytes = 0usize;
                if fmt.write_pkl() {
                    match events::write_evdb_pkl(&db, &evdb_path) {
                        Ok(sz) => {
                            pkl_bytes = sz;
                            pkl_path = Some(evdb_path.display().to_string());
                        }
                        Err(e) => set_err(e),
                    }
                }
                if fmt.write_json() {
                    let p = evdb_path.with_extension("json");
                    match write_json_file(&db, &p) {
                        Ok(sz) => {
                            json_bytes = sz;
                            json_path = Some(p.display().to_string());
                        }
                        Err(e) => set_err(e),
                    }
                }
                records.lock().unwrap().insert(
                    "evdb".to_string(),
                    AssetRecord {
                        items: db.len(),
                        pkl_path,
                        pkl_bytes,
                        json_path,
                        json_bytes,
                        ms: t0.elapsed().as_millis(),
                    },
                );
            });
        }
    });

    if let Some(err) = first_err.into_inner().unwrap() {
        return Err(err);
    }

    let total_ms = t_total.elapsed().as_millis();
    let built_at_unix = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0);

    let manifest = AssetsManifest {
        schema_version: 1,
        tool: format!("hoi4_recalc/{}", env!("CARGO_PKG_VERSION")),
        built_at_unix,
        game_dir: gdir.display().to_string(),
        lang: primary_lang,
        format: fmt.as_str().to_string(),
        mods: vfs.mods,
        warnings: vfs.warnings,
        total_ms,
        assets: records.into_inner().unwrap(),
    };

    let mpath = manifest_path.unwrap_or_else(|| odir.join("assets_manifest.json"));
    let mjson = serde_json::to_string_pretty(&manifest)
        .map_err(|e| format!("序列化 manifest 失败: {}", e))?;
    write_text_file(&mpath, &mjson)?;

    println!(
        "[hoi4_recalc] Assets rebuilt: game={} mods={} lang={} format={} total={}ms",
        manifest.game_dir,
        manifest.mods.len(),
        manifest.lang,
        manifest.format,
        manifest.total_ms
    );
    for (name, rec) in &manifest.assets {
        println!(
            "  - {:15} items={:<7} pkl={:>7.1}KB  ({}ms)",
            name,
            rec.items,
            rec.pkl_bytes as f64 / 1024.0,
            rec.ms
        );
    }
    for w in &manifest.warnings {
        eprintln!("[hoi4_recalc] WARN: {}", w);
    }
    println!("[hoi4_recalc] Wrote manifest: {}", mpath.display());

    if let Some(rp) = report_path {
        let md = render_assets_report(&manifest);
        write_text_file(&rp, &md)?;
        println!("[hoi4_recalc] Wrote report: {}", rp.display());
    }

    Ok(0)
}
