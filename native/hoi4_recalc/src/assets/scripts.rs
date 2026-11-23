//! Paradox 核心脚本文本静态表构建模块：
//! - `techdefs.pkl` — `common/technologies/*.txt`
//! - `focusplans.pkl` — `common/national_focus/*.txt` + `common/ai_strategy_plans/*.txt`
//! - `decisions_meta.pkl` — `common/decisions/**/*.txt` (跳过 `categories/`)
//! - `equipmeta.pkl` — `common/units/equipment/*.txt` + `common/technologies/*.txt`

use crate::pdx::{
    all_blocks_after, all_idents_after, collapse_and_truncate, find_all_words, find_key_eq,
    first_block_after, first_bool_after, first_ident_after, first_int_after, is_word_byte, is_ws,
    match_brace_block, read_text_lossy, skip_ws, strip_comments,
};
use crate::pickle::PickleWriter;
use crate::vfs::PdxVfs;
use rayon::prelude::*;
use serde::Serialize;
use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};
use std::path::Path;

pub const FOCUS_SCHEMA: i64 = 2;
pub const DECISIONS_SCHEMA: i64 = 1;
pub const EQUIPMETA_VERSION: i64 = 1;

// =========================================================================
// 1. techdefs.pkl
// =========================================================================

#[derive(Debug, Clone, Serialize)]
pub struct TechDef {
    pub year: i64,
    pub folder: String,
    pub categories: Vec<String>,
    pub xor: Vec<String>,
    pub category: String,
    pub file: String,
    pub parents: Vec<String>,
}

fn tech_file_category(stem_lower: &str) -> Option<&'static str> {
    match stem_lower {
        "infantry" => Some("infantry"),
        "industry" => Some("industry"),
        "electronic_mechanical_engineering" => Some("computing"),
        "armor" | "nsb_armor" => Some("armor"),
        "air_techs" | "bba_air_techs" => Some("air"),
        "artillery" => Some("artillery"),
        "support" => Some("support"),
        "naval" | "mtg_naval" | "mtg_naval_support" => Some("naval"),
        "special_projects_tech" => Some("special"),
        _ => None,
    }
}

fn tech_category(stem: &str, folder: &str, cats: &[String]) -> String {
    if let Some(c) = tech_file_category(&stem.to_ascii_lowercase()) {
        return c.to_string();
    }
    let mut s = folder.to_ascii_lowercase();
    for c in cats {
        s.push(' ');
        s.push_str(&c.to_ascii_lowercase());
    }
    for (kw, val) in [
        ("infantry", "infantry"),
        ("industry", "industry"),
        ("electronic", "computing"),
        ("computing", "computing"),
        ("armor", "armor"),
        ("tank", "armor"),
        ("air", "air"),
        ("fighter", "air"),
        ("bomber", "air"),
        ("artillery", "artillery"),
        ("support", "support"),
        ("naval", "naval"),
    ] {
        if s.contains(kw) {
            return val.to_string();
        }
    }
    "other".to_string()
}

/// 等价于 `techdata._top_blocks`：返回所有 depth-1 `name = { ... }` 块。
fn iter_depth1_blocks(text: &str) -> Vec<(&str, &str)> {
    let b = text.as_bytes();
    let n = b.len();
    let mut out = Vec::new();
    let mut i = 0usize;
    let mut depth = 0usize;
    while i < n {
        let ch = b[i];
        if ch == b'{' {
            depth += 1;
            i += 1;
            continue;
        }
        if ch == b'}' {
            depth = depth.saturating_sub(1);
            i += 1;
            continue;
        }
        if depth == 1 && (ch.is_ascii_alphabetic() || ch == b'_') {
            let mut j = i;
            while j < n && is_word_byte(b[j]) {
                j += 1;
            }
            let name = &text[i..j];
            let k = skip_ws(b, j);
            if k < n && b[k] == b'=' {
                let k2 = skip_ws(b, k + 1);
                if k2 < n && b[k2] == b'{' {
                    if let Some((inner, after)) = match_brace_block(text, k2 + 1) {
                        out.push((name, inner));
                        i = after;
                        continue;
                    } else {
                        // 与 Python 保持一致：未闭合到文件尾
                        out.push((name, &text[k2 + 1..]));
                        break;
                    }
                }
            }
            i = j;
            continue;
        }
        i += 1;
    }
    out
}

pub fn build_techdefs(vfs: &PdxVfs) -> BTreeMap<String, TechDef> {
    let files = vfs.list_dir_files("common/technologies", ".txt");

    struct RawTech {
        name: String,
        def: TechDef,
        leads_to: Vec<String>,
    }

    let per_file: Vec<Vec<RawTech>> = files
        .par_iter()
        .map(|ent| {
            let Ok(raw) = read_text_lossy(&ent.disk_path) else {
                return Vec::new();
            };
            let stem = ent
                .file_name
                .strip_suffix(".txt")
                .or_else(|| ent.file_name.strip_suffix(".TXT"))
                .unwrap_or(&ent.file_name)
                .to_string();
            let clean = strip_comments(&raw);
            let mut items = Vec::new();
            for (name, body) in iter_depth1_blocks(&clean) {
                let year = first_int_after(body, "start_year", false).unwrap_or(1936);
                let mut folder = String::new();
                for fblk in all_blocks_after(body, "folder") {
                    if let Some(nm) = first_ident_after(fblk, "name", false) {
                        folder = nm.to_string();
                        break;
                    }
                }
                let categories: Vec<String> = first_block_after(body, "categories")
                    .map(|b| find_all_words(b).into_iter().map(|s| s.to_string()).collect())
                    .unwrap_or_default();
                let leads_to: Vec<String> = all_idents_after(body, "leads_to_tech")
                    .into_iter()
                    .map(|s| s.to_string())
                    .collect();
                let mut xor_set: BTreeSet<String> = BTreeSet::new();
                for xblk in all_blocks_after(body, "XOR") {
                    for w in find_all_words(xblk) {
                        if !w.is_empty() {
                            xor_set.insert(w.to_string());
                        }
                    }
                }
                if let Some(mblk) = first_block_after(body, "mutually_exclusive") {
                    for w in find_all_words(mblk) {
                        if !w.is_empty() {
                            xor_set.insert(w.to_string());
                        }
                    }
                }
                let cat = tech_category(&stem, &folder, &categories);
                items.push(RawTech {
                    name: name.to_string(),
                    def: TechDef {
                        year,
                        folder,
                        categories,
                        xor: xor_set.into_iter().collect(),
                        category: cat,
                        file: stem.clone(),
                        parents: Vec::new(),
                    },
                    leads_to,
                });
            }
            items
        })
        .collect();

    let mut techs: BTreeMap<String, TechDef> = BTreeMap::new();
    let mut leads_map: BTreeMap<String, Vec<String>> = BTreeMap::new();
    for list in per_file {
        for it in list {
            leads_map.insert(it.name.clone(), it.leads_to);
            techs.insert(it.name, it.def);
        }
    }

    let mut parents: HashMap<String, BTreeSet<String>> = HashMap::new();
    for (name, children) in &leads_map {
        for ch in children {
            parents.entry(ch.clone()).or_default().insert(name.clone());
        }
    }
    for (name, def) in techs.iter_mut() {
        if let Some(ps) = parents.get(name) {
            def.parents = ps.iter().cloned().collect();
        }
    }
    techs
}

pub fn write_techdefs_pkl(techs: &BTreeMap<String, TechDef>, path: &Path) -> Result<usize, String> {
    let mut pw = PickleWriter::with_capacity(techs.len() * 160 + 8192);
    pw.write_dict_iter(
        techs.iter(),
        |w, k| w.write_str(k),
        |w, d| {
            w.begin_small_dict();
            w.write_str_intern("year");
            w.write_int(d.year);
            w.write_str_intern("folder");
            w.write_str_intern(&d.folder);
            w.write_str_intern("categories");
            w.write_list(&d.categories, |w2, c| w2.write_str_intern(c));
            w.write_str_intern("xor");
            w.write_list(&d.xor, |w2, x| w2.write_str(x));
            w.write_str_intern("category");
            w.write_str_intern(&d.category);
            w.write_str_intern("file");
            w.write_str_intern(&d.file);
            w.write_str_intern("parents");
            w.write_list(&d.parents, |w2, p| w2.write_str(p));
            w.end_small_dict();
        },
    );
    pw.save_to_file(path)
}

// =========================================================================
// 2. focusplans.pkl
// =========================================================================

#[derive(Debug, Clone, Serialize)]
pub struct FocusDef {
    pub id: String,
    pub days: i64,
    pub research_slot: bool,
    pub annex: bool,
    pub wargoal: bool,
    pub industry: bool,
    pub air_naval: bool,
    pub prereq_groups: Vec<Vec<String>>,
    pub mutex: Vec<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub x: Option<i64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub y: Option<i64>,
}

#[derive(Debug, Clone, Serialize)]
pub struct FocusPlansData {
    pub schema: i64,
    pub focuses: BTreeMap<String, FocusDef>,
    pub plans: BTreeMap<String, Vec<String>>,
}

fn parse_focus_block(body: &str) -> Option<(String, FocusDef)> {
    let key = first_ident_after(body, "id", false)?.to_string();
    let cost = first_int_after(body, "cost", false).unwrap_or(10);
    let days = (cost * 7).max(1);
    let low = body.to_ascii_lowercase();
    let research_slot = find_key_eq(&low, "add_research_slot", 0, false).is_some();
    let annex = find_key_eq(&low, "annex_country", 0, false).is_some();
    let wargoal = find_key_eq(&low, "create_wargoal", 0, false).is_some();
    let industry = low.contains("industrial_complex") || low.contains("arms_factory");
    let key_low = key.to_ascii_lowercase();
    let air_naval = [
        "luftwaffe",
        "kriegsmarine",
        "naval",
        "air",
        "bomber",
        "fighter",
        "carrier",
        "submarine",
        "u_boat",
    ]
    .iter()
    .any(|kw| key_low.contains(kw));

    let mut prereq_groups = Vec::new();
    for blk in all_blocks_after(body, "prerequisite") {
        let keys: Vec<String> = all_idents_after(blk, "focus")
            .into_iter()
            .map(|s| s.to_string())
            .collect();
        if !keys.is_empty() {
            prereq_groups.push(keys);
        }
    }
    let mutex: Vec<String> = first_block_after(body, "mutually_exclusive")
        .map(|blk| {
            all_idents_after(blk, "focus")
                .into_iter()
                .map(|s| s.to_string())
                .collect()
        })
        .unwrap_or_default();

    let x = first_int_after(body, "x", true);
    let y = first_int_after(body, "y", true);

    Some((
        key.clone(),
        FocusDef {
            id: key,
            days,
            research_slot,
            annex,
            wargoal,
            industry,
            air_naval,
            prereq_groups,
            mutex,
            x,
            y,
        },
    ))
}

/// 等价于 `focusplan.DATE_RE = re.compile(r"\b([A-Z][A-Za-z0-9_]{2,})\b")`。
fn find_plan_focus_tokens(body: &str) -> Vec<String> {
    let b = body.as_bytes();
    let n = b.len();
    let mut out = Vec::new();
    let mut i = 0usize;
    while i < n {
        if b[i].is_ascii_uppercase() && (i == 0 || !is_word_byte(b[i - 1])) {
            let start = i;
            i += 1;
            while i < n && is_word_byte(b[i]) {
                i += 1;
            }
            if i - start >= 3 {
                out.push(body[start..i].to_string());
            }
            continue;
        }
        i += 1;
    }
    out
}

pub fn build_focusplans(vfs: &PdxVfs) -> FocusPlansData {
    let focus_files = vfs.list_dir_files("common/national_focus", ".txt");

    let per_focus_file: Vec<Vec<(String, FocusDef)>> = focus_files
        .par_iter()
        .map(|ent| {
            let Ok(raw) = read_text_lossy(&ent.disk_path) else {
                return Vec::new();
            };
            let clean = strip_comments(&raw);
            let mut list = Vec::new();
            for (name, body) in iter_depth1_blocks(&clean) {
                if name == "focus" || name == "shared_focus" {
                    if let Some(pair) = parse_focus_block(body) {
                        list.push(pair);
                    }
                }
            }
            list
        })
        .collect();

    let mut focuses: BTreeMap<String, FocusDef> = BTreeMap::new();
    for list in per_focus_file {
        for (k, v) in list {
            focuses.insert(k, v);
        }
    }

    // 解析 AI 策略规划：每个 3 字母 TAG 选取 historical > default > 其他
    let plan_files = vfs.list_dir_files("common/ai_strategy_plans", ".txt");
    let mut by_tag: BTreeMap<String, Vec<&crate::vfs::VfsEntry>> = BTreeMap::new();
    for ent in &plan_files {
        if let Some((prefix, _)) = ent.file_name.split_once('_') {
            if prefix.len() == 3 {
                by_tag
                    .entry(prefix.to_ascii_uppercase())
                    .or_default()
                    .push(ent);
            }
        }
    }

    let chosen_plans: Vec<(String, &crate::vfs::VfsEntry)> = by_tag
        .into_iter()
        .filter_map(|(tag, mut cands)| {
            cands.sort_by(|a, b| {
                let al = a.file_name.to_ascii_lowercase();
                let bl = b.file_name.to_ascii_lowercase();
                let ra = if al.contains("historical") {
                    0
                } else if al.contains("default") {
                    1
                } else {
                    2
                };
                let rb = if bl.contains("historical") {
                    0
                } else if bl.contains("default") {
                    1
                } else {
                    2
                };
                (ra, al).cmp(&(rb, bl))
            });
            cands.first().map(|&e| (tag, e))
        })
        .collect();

    let parsed_plans: Vec<(String, Vec<String>)> = chosen_plans
        .par_iter()
        .filter_map(|(tag, ent)| {
            let raw = read_text_lossy(&ent.disk_path).ok()?;
            let clean = strip_comments(&raw);
            let mut out = Vec::new();
            let mut seen: HashSet<String> = HashSet::new();
            for body in all_blocks_after(&clean, "ai_national_focuses") {
                for tok in find_plan_focus_tokens(body) {
                    if seen.insert(tok.clone()) {
                        out.push(tok);
                    }
                }
            }
            if out.is_empty() {
                None
            } else {
                Some((tag.clone(), out))
            }
        })
        .collect();

    let mut plans: BTreeMap<String, Vec<String>> = BTreeMap::new();
    for (tag, order) in parsed_plans {
        plans.insert(tag, order);
    }

    FocusPlansData {
        schema: FOCUS_SCHEMA,
        focuses,
        plans,
    }
}

pub fn write_focusplans_pkl(data: &FocusPlansData, path: &Path) -> Result<usize, String> {
    let mut pw = PickleWriter::with_capacity(data.focuses.len() * 140 + 32768);
    pw.begin_small_dict();
    pw.write_str_intern("schema");
    pw.write_int(data.schema);
    pw.write_str_intern("focuses");
    pw.write_dict_iter(
        data.focuses.iter(),
        |w, k| w.write_str(k),
        |w, f| {
            w.begin_small_dict();
            w.write_str_intern("id");
            w.write_str(&f.id);
            w.write_str_intern("days");
            w.write_int(f.days);
            w.write_str_intern("research_slot");
            w.write_bool(f.research_slot);
            w.write_str_intern("annex");
            w.write_bool(f.annex);
            w.write_str_intern("wargoal");
            w.write_bool(f.wargoal);
            w.write_str_intern("industry");
            w.write_bool(f.industry);
            w.write_str_intern("air_naval");
            w.write_bool(f.air_naval);
            w.write_str_intern("prereq_groups");
            w.write_list(&f.prereq_groups, |w2, grp| {
                w2.write_list(grp, |w3, s| w3.write_str(s));
            });
            w.write_str_intern("mutex");
            w.write_list(&f.mutex, |w2, s| w2.write_str(s));
            if let Some(xv) = f.x {
                w.write_str_intern("x");
                w.write_int(xv);
            }
            if let Some(yv) = f.y {
                w.write_str_intern("y");
                w.write_int(yv);
            }
            w.end_small_dict();
        },
    );
    pw.write_str_intern("plans");
    pw.write_dict_iter(
        data.plans.iter(),
        |w, tag| w.write_str(tag),
        |w, list| w.write_list(list, |w2, s| w2.write_str(s)),
    );
    pw.end_small_dict();
    pw.save_to_file(path)
}

// =========================================================================
// 3. decisions_meta.pkl
// =========================================================================

const DECISION_MARKERS: [&str; 11] = [
    "available",
    "visible",
    "complete_effect",
    "activation",
    "days_remove",
    "fire_only_once",
    "targets",
    "days_mission_timeout",
    "remove_effect",
    "cancel_effect",
    "is_good",
];

#[derive(Debug, Clone, Serialize)]
pub struct DecisionMeta {
    pub key: String,
    pub category: Option<String>,
    pub cost_pp: Option<i64>,
    pub days_remove: Option<i64>,
    pub days_mission_timeout: Option<i64>,
    pub days_re_enable: Option<i64>,
    pub fire_only_once: Option<bool>,
    pub is_good: Option<bool>,
    pub has_available: bool,
    pub has_visible: bool,
    pub has_activation: bool,
    pub available: Option<String>,
    pub visible: Option<String>,
    pub activation: Option<String>,
    pub file: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct DecisionsMetaData {
    pub schema: i64,
    pub files: usize,
    pub count: usize,
    pub decisions: BTreeMap<String, DecisionMeta>,
}

fn parse_decisions_file(text: &str, file_name: &str) -> Vec<(String, DecisionMeta)> {
    let clean = strip_comments(text);
    let b = clean.as_bytes();
    let n = b.len();
    let mut stack: Vec<(&str, usize)> = Vec::new();
    let mut out = Vec::new();

    for i in 0..n {
        if b[i] == b'{' {
            // 向左寻找 `name`（等价于 decisions_meta._walk）
            let mut j = i as isize - 1;
            while j >= 0 && matches!(b[j as usize], b' ' | b'\t' | b'\r' | b'\n' | b'=') {
                j -= 1;
            }
            let mut k = j;
            while k >= 0
                && (b[k as usize].is_ascii_alphanumeric()
                    || matches!(b[k as usize], b'_' | b'.' | b'-'))
            {
                k -= 1;
            }
            let name = if j >= 0 && k < j {
                &clean[(k + 1) as usize..(j + 1) as usize]
            } else {
                ""
            };
            stack.push((name, i + 1));
        } else if b[i] == b'}' {
            if let Some((key, start)) = stack.pop() {
                if key.is_empty() || !key.as_bytes()[0].is_ascii_alphabetic() {
                    continue;
                }
                let body = &clean[start..i];
                let low = body.to_ascii_lowercase();
                if !DECISION_MARKERS
                    .iter()
                    .any(|mk| find_key_eq(&low, mk, 0, false).is_some())
                {
                    continue;
                }
                if key.to_ascii_lowercase().contains("category") && !low.contains("available") {
                    continue;
                }
                let category = stack.last().map(|s| s.0.to_string());
                let avail_blk = first_block_after(body, "available");
                let vis_blk = first_block_after(body, "visible");
                let act_blk = first_block_after(body, "activation");
                out.push((
                    key.to_string(),
                    DecisionMeta {
                        key: key.to_string(),
                        category,
                        cost_pp: first_int_after(body, "cost", true),
                        days_remove: first_int_after(body, "days_remove", true),
                        days_mission_timeout: first_int_after(body, "days_mission_timeout", true),
                        days_re_enable: first_int_after(body, "days_re_enable", true),
                        fire_only_once: first_bool_after(body, "fire_only_once"),
                        is_good: first_bool_after(body, "is_good"),
                        has_available: avail_blk.is_some(),
                        has_visible: vis_blk.is_some(),
                        has_activation: act_blk.is_some(),
                        available: avail_blk.map(|s| collapse_and_truncate(s, 180)),
                        visible: vis_blk.map(|s| collapse_and_truncate(s, 180)),
                        activation: act_blk.map(|s| collapse_and_truncate(s, 180)),
                        file: file_name.to_string(),
                    },
                ));
            }
        }
    }
    out
}

pub fn build_decisions_meta(vfs: &PdxVfs) -> DecisionsMetaData {
    let files = vfs.walk_dir_files("common/decisions", ".txt", &["categories"]);
    let n_files = files.len();

    let per_file: Vec<Vec<(String, DecisionMeta)>> = files
        .par_iter()
        .map(|ent| {
            let Ok(raw) = read_text_lossy(&ent.disk_path) else {
                return Vec::new();
            };
            parse_decisions_file(&raw, &ent.file_name)
        })
        .collect();

    let mut decisions: BTreeMap<String, DecisionMeta> = BTreeMap::new();
    for list in per_file {
        for (k, v) in list {
            decisions.insert(k, v);
        }
    }
    let count = decisions.len();
    DecisionsMetaData {
        schema: DECISIONS_SCHEMA,
        files: n_files,
        count,
        decisions,
    }
}

pub fn write_decisions_meta_pkl(data: &DecisionsMetaData, path: &Path) -> Result<usize, String> {
    let mut pw = PickleWriter::with_capacity(data.count * 260 + 32768);
    pw.begin_small_dict();
    pw.write_str_intern("schema");
    pw.write_int(data.schema);
    pw.write_str_intern("files");
    pw.write_int(data.files as i64);
    pw.write_str_intern("count");
    pw.write_int(data.count as i64);
    pw.write_str_intern("decisions");
    pw.write_dict_iter(
        data.decisions.iter(),
        |w, k| w.write_str(k),
        |w, d| {
            w.begin_small_dict();
            w.write_str_intern("key");
            w.write_str(&d.key);
            w.write_str_intern("category");
            w.write_opt_str_intern(d.category.as_deref());
            w.write_str_intern("cost_pp");
            w.write_opt_int(d.cost_pp);
            w.write_str_intern("days_remove");
            w.write_opt_int(d.days_remove);
            w.write_str_intern("days_mission_timeout");
            w.write_opt_int(d.days_mission_timeout);
            w.write_str_intern("days_re_enable");
            w.write_opt_int(d.days_re_enable);
            w.write_str_intern("fire_only_once");
            w.write_opt_bool(d.fire_only_once);
            w.write_str_intern("is_good");
            w.write_opt_bool(d.is_good);
            w.write_str_intern("has_available");
            w.write_bool(d.has_available);
            w.write_str_intern("has_visible");
            w.write_bool(d.has_visible);
            w.write_str_intern("has_activation");
            w.write_bool(d.has_activation);
            w.write_str_intern("available");
            w.write_opt_str(d.available.as_deref());
            w.write_str_intern("visible");
            w.write_opt_str(d.visible.as_deref());
            w.write_str_intern("activation");
            w.write_opt_str(d.activation.as_deref());
            w.write_str_intern("file");
            w.write_str_intern(&d.file);
            w.end_small_dict();
        },
    );
    pw.end_small_dict();
    pw.save_to_file(path)
}

// =========================================================================
// 4. equipmeta.pkl
// =========================================================================

const EQUIP_STAT_FIELDS: [&str; 15] = [
    "soft_attack",
    "hard_attack",
    "breakthrough",
    "defense",
    "ap_attack",
    "armor_value",
    "air_attack",
    "air_defence",
    "air_agility",
    "air_ground_attack",
    "air_bombing",
    "naval_strike_attack",
    "maximum_speed",
    "reliability",
    "build_cost_ic",
];

#[derive(Debug, Clone, Serialize)]
pub struct EquipmentMeta {
    pub key: String,
    pub file: String,
    pub is_archetype: bool,
    pub is_buildable: bool,
    pub archetype: String,
    pub parent: Option<String>,
    pub year: Option<i64>,
    pub visual_level: Option<i64>,
    pub priority: Option<i64>,
    pub interface_category: Option<String>,
    pub types: Vec<String>,
    pub stats: BTreeMap<String, f64>,
}

#[derive(Debug, Clone, Serialize)]
pub struct EquipMetaData {
    pub version: i64,
    pub equipments: BTreeMap<String, EquipmentMeta>,
    pub tech_to_eq: BTreeMap<String, Vec<String>>,
    pub eq_to_tech: BTreeMap<String, String>,
    pub game_dir: String,
    pub eq_sig: (usize, i64),
    pub tech_sig: (usize, i64),
    pub mods: Vec<String>,
}

fn strip_comments_compact(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    for line in text.lines() {
        let s = match line.find('#') {
            Some(idx) => &line[..idx],
            None => line,
        }
        .trim();
        if !s.is_empty() {
            if !out.is_empty() {
                out.push('\n');
            }
            out.push_str(s);
        }
    }
    out
}

fn is_ident_ext(b: u8) -> bool {
    b.is_ascii_alphanumeric() || matches!(b, b'_' | b'.' | b'-')
}

/// 等价于 `equipmeta._iter_top_blocks(cleaned, container_key)`。
fn iter_container_children<'a>(body: &'a str, container_key: &str) -> Vec<(&'a str, &'a str)> {
    let Some(container) = first_block_after(body, container_key) else {
        return Vec::new();
    };
    let b = container.as_bytes();
    let n = b.len();
    let mut out = Vec::new();
    let mut i = 0usize;
    let mut depth = 1usize;

    while i < n && depth > 0 {
        let ch = b[i];
        if ch == b'}' {
            depth -= 1;
            i += 1;
            continue;
        }
        if is_ws(ch) {
            i += 1;
            continue;
        }
        if depth == 1 {
            if ch == b'@' {
                while i < n && b[i] != b'\n' {
                    i += 1;
                }
                continue;
            }
            if is_ident_ext(ch) {
                let start = i;
                let mut j = i;
                while j < n && is_ident_ext(b[j]) {
                    j += 1;
                }
                let k = skip_ws(b, j);
                if k < n && b[k] == b'=' {
                    let k2 = skip_ws(b, k + 1);
                    if k2 < n && b[k2] == b'{' {
                        let key = &container[start..j];
                        if let Some((inner, after)) = match_brace_block(container, k2 + 1) {
                            out.push((key, inner));
                            i = after;
                            continue;
                        } else {
                            out.push((key, &container[k2 + 1..]));
                            break;
                        }
                    }
                }
            }
        }
        if ch == b'{' {
            depth += 1;
        }
        i += 1;
    }
    out
}

fn parse_kv_line(line: &str, kv: &mut HashMap<String, String>) {
    if !line.contains('=') {
        return;
    }
    let b = line.as_bytes();
    let n = b.len();
    let mut i = 0usize;
    while i < n {
        if is_ident_ext(b[i]) && (i == 0 || !is_word_byte(b[i - 1])) {
            let ks = i;
            while i < n && is_ident_ext(b[i]) {
                i += 1;
            }
            let ke = i;
            let p = skip_ws(b, ke);
            if p < n && b[p] == b'=' {
                let vs = skip_ws(b, p + 1);
                let mut ve = vs;
                while ve < n && !is_ws(b[ve]) && !matches!(b[ve], b'=' | b'{' | b'}') {
                    ve += 1;
                }
                if ve > vs {
                    let k = &line[ks..ke];
                    let v = line[vs..ve].trim_matches('"');
                    kv.insert(k.to_string(), v.to_string());
                    i = ve;
                    continue;
                }
            }
            continue;
        }
        i += 1;
    }
}

fn top_level_kv_and_lists(block: &str) -> (HashMap<String, String>, HashMap<String, Vec<String>>) {
    let mut kv: HashMap<String, String> = HashMap::new();
    let mut lists: HashMap<String, Vec<String>> = HashMap::new();
    let b = block.as_bytes();
    let n = b.len();
    let mut depth = 0usize;
    let mut i = 0usize;
    let mut buf = String::new();

    while i < n {
        let ch = b[i];
        if ch == b'{' {
            if depth == 0 {
                let prefix = buf.trim().to_string();
                buf.clear();
                let lkey = prefix.strip_suffix('=').and_then(|lhs| {
                    let t = lhs.trim_end();
                    let tb = t.as_bytes();
                    let mut k = tb.len();
                    while k > 0 && is_ident_ext(tb[k - 1]) {
                        k -= 1;
                    }
                    if k < tb.len() {
                        Some(t[k..].to_string())
                    } else {
                        None
                    }
                });
                let (inner, after) = match match_brace_block(block, i + 1) {
                    Some(x) => x,
                    None => (&block[i + 1..], n),
                };
                if let Some(lk) = lkey {
                    if !inner.contains('{') && !inner.contains('=') {
                        let items: Vec<String> =
                            inner.split_whitespace().map(|s| s.to_string()).collect();
                        if !items.is_empty() {
                            lists.insert(lk, items);
                        }
                    }
                }
                i = after;
                continue;
            }
            depth += 1;
            i += 1;
            continue;
        }
        if ch == b'}' {
            depth = depth.saturating_sub(1);
            i += 1;
            continue;
        }
        if depth == 0 {
            if ch == b'\n' {
                parse_kv_line(buf.trim(), &mut kv);
                buf.clear();
            } else {
                // UTF-8 字符推进
                let c = block[i..].chars().next().unwrap();
                buf.push(c);
                i += c.len_utf8();
                continue;
            }
        }
        i += 1;
    }
    if !buf.is_empty() {
        parse_kv_line(buf.trim(), &mut kv);
    }
    (kv, lists)
}

fn fallback_family(name: &str) -> String {
    if let Some((prefix, suf)) = name.rsplit_once('_') {
        if !suf.is_empty() && suf.bytes().all(|c| c.is_ascii_digit()) {
            return prefix.to_string();
        }
    }
    name.to_string()
}

/// 从 `enable_equipments = { ... }` 块中提取顶层裸装备标识符，
/// 自动跳过 `limit = { NOT = { has_dlc = "..." } }` 等嵌套条件块与 `k = v` 键值。
fn extract_bare_tokens(blk: &str) -> Vec<String> {
    let b = blk.as_bytes();
    let n = b.len();
    let mut out: Vec<String> = Vec::new();
    let mut i = 0usize;

    while i < n {
        let ch = b[i];
        if is_ws(ch) {
            i += 1;
            continue;
        }
        if ch == b'{' {
            // 跳过匿名嵌套块
            i = match match_brace_block(blk, i + 1) {
                Some((_, after)) => after,
                None => n,
            };
            continue;
        }
        if ch == b'}' {
            i += 1;
            continue;
        }
        if ch == b'"' {
            // 跳过引号字符串
            i += 1;
            while i < n && b[i] != b'"' {
                if b[i] == b'\\' && i + 1 < n {
                    i += 2;
                } else {
                    i += 1;
                }
            }
            if i < n {
                i += 1;
            }
            continue;
        }
        if is_ident_ext(ch) {
            let start = i;
            while i < n && is_ident_ext(b[i]) {
                i += 1;
            }
            let tok = &blk[start..i];
            let p = skip_ws(b, i);
            if p < n && b[p] == b'=' {
                // `key = { ... }` 或 `key = value`：完整跳过右值
                let q = skip_ws(b, p + 1);
                if q < n && b[q] == b'{' {
                    i = match match_brace_block(blk, q + 1) {
                        Some((_, after)) => after,
                        None => n,
                    };
                } else if q < n && b[q] == b'"' {
                    let mut r = q + 1;
                    while r < n && b[r] != b'"' {
                        if b[r] == b'\\' && r + 1 < n {
                            r += 2;
                        } else {
                            r += 1;
                        }
                    }
                    i = if r < n { r + 1 } else { n };
                } else {
                    let mut r = q;
                    while r < n && !is_ws(b[r]) && !matches!(b[r], b'{' | b'}') {
                        r += 1;
                    }
                    i = r;
                }
                continue;
            }
            if !out.iter().any(|x| x == tok) {
                out.push(tok.to_string());
            }
            continue;
        }
        i += 1;
    }
    out
}

pub fn build_equipmeta(vfs: &PdxVfs) -> EquipMetaData {
    let eq_files = vfs.list_dir_files("common/units/equipment", ".txt");

    #[derive(Clone)]
    struct RawEq {
        key: String,
        file: String,
        is_archetype: bool,
        is_buildable: bool,
        archetype: Option<String>,
        parent: Option<String>,
        year: Option<i64>,
        visual_level: Option<i64>,
        priority: Option<i64>,
        interface_category: Option<String>,
        types: Vec<String>,
        stats: BTreeMap<String, f64>,
    }

    let per_eq_file: Vec<Vec<RawEq>> = eq_files
        .par_iter()
        .map(|ent| {
            let Ok(raw) = read_text_lossy(&ent.disk_path) else {
                return Vec::new();
            };
            let cleaned = strip_comments_compact(&raw);
            let mut out = Vec::new();
            for (key, blk) in iter_container_children(&cleaned, "equipments") {
                let (kv, lists) = top_level_kv_and_lists(blk);
                let is_archetype = kv
                    .get("is_archetype")
                    .map(|v| v.eq_ignore_ascii_case("yes"))
                    .unwrap_or(false);
                let is_buildable = kv
                    .get("is_buildable")
                    .map(|v| !v.eq_ignore_ascii_case("no"))
                    .unwrap_or(true);
                let types = if let Some(lst) = lists.get("type") {
                    lst.clone()
                } else if let Some(t) = kv.get("type") {
                    vec![t.clone()]
                } else {
                    Vec::new()
                };
                let parse_i = |k: &str| -> Option<i64> {
                    kv.get(k)
                        .and_then(|s| s.parse::<f64>().ok())
                        .map(|f| f.round() as i64)
                };
                let mut stats = BTreeMap::new();
                for sf in EQUIP_STAT_FIELDS {
                    if let Some(fv) = kv.get(sf).and_then(|s| s.parse::<f64>().ok()) {
                        stats.insert(sf.to_string(), fv);
                    }
                }
                out.push(RawEq {
                    key: key.to_string(),
                    file: ent.file_name.clone(),
                    is_archetype,
                    is_buildable,
                    archetype: kv.get("archetype").cloned(),
                    parent: kv.get("parent").cloned(),
                    year: parse_i("year"),
                    visual_level: parse_i("visual_level"),
                    priority: parse_i("priority"),
                    interface_category: kv.get("interface_category").cloned(),
                    types,
                    stats,
                });
            }
            out
        })
        .collect();

    let mut raw_eq: BTreeMap<String, RawEq> = BTreeMap::new();
    for list in per_eq_file {
        for r in list {
            raw_eq.insert(r.key.clone(), r);
        }
    }

    // 继承链合并：archetype -> parent -> self
    let mut equipments: BTreeMap<String, EquipmentMeta> = BTreeMap::new();
    for (key, rec) in &raw_eq {
        let mut chain: Vec<&RawEq> = Vec::new();
        let mut seen: HashSet<&str> = HashSet::new();
        let mut cur: Option<&str> = Some(key.as_str());
        while let Some(ck) = cur {
            if !seen.insert(ck) {
                break;
            }
            let Some(node) = raw_eq.get(ck) else {
                break;
            };
            chain.push(node);
            cur = if let Some(ref p) = node.parent {
                Some(p.as_str())
            } else if let Some(ref a) = node.archetype {
                if !seen.contains(a.as_str()) {
                    Some(a.as_str())
                } else {
                    None
                }
            } else {
                None
            };
        }
        chain.reverse();
        let mut merged_stats: BTreeMap<String, f64> = BTreeMap::new();
        let mut merged_year: Option<i64> = None;
        let mut merged_cat: Option<String> = None;
        let mut merged_types: Vec<String> = Vec::new();
        let mut merged_arch: Option<String> = rec
            .archetype
            .clone()
            .or_else(|| if rec.is_archetype { Some(key.clone()) } else { None });
        for node in chain {
            for (sk, &sv) in &node.stats {
                merged_stats.insert(sk.clone(), sv);
            }
            if node.year.is_some() {
                merged_year = node.year;
            }
            if node.interface_category.is_some() {
                merged_cat = node.interface_category.clone();
            }
            if !node.types.is_empty() {
                merged_types = node.types.clone();
            }
            if merged_arch.is_none() {
                if node.archetype.is_some() {
                    merged_arch = node.archetype.clone();
                } else if node.is_archetype {
                    merged_arch = Some(node.key.clone());
                }
            }
        }
        equipments.insert(
            key.clone(),
            EquipmentMeta {
                key: key.clone(),
                file: rec.file.clone(),
                is_archetype: rec.is_archetype,
                is_buildable: rec.is_buildable,
                archetype: merged_arch.unwrap_or_else(|| fallback_family(key)),
                parent: rec.parent.clone(),
                year: merged_year,
                visual_level: rec.visual_level,
                priority: rec.priority,
                interface_category: merged_cat,
                types: merged_types,
                stats: merged_stats,
            },
        );
    }

    // 解析科技 enable_equipments
    let tech_files = vfs.list_dir_files("common/technologies", ".txt");
    let per_tech: Vec<Vec<(String, Vec<String>)>> = tech_files
        .par_iter()
        .map(|ent| {
            let Ok(raw) = read_text_lossy(&ent.disk_path) else {
                return Vec::new();
            };
            let cleaned = strip_comments_compact(&raw);
            let mut out = Vec::new();
            for (tkey, blk) in iter_container_children(&cleaned, "technologies") {
                let mut found: Vec<String> = Vec::new();
                for eblk in all_blocks_after(blk, "enable_equipments") {
                    for tok in extract_bare_tokens(eblk) {
                        if !found.iter().any(|x| x == &tok) {
                            found.push(tok);
                        }
                    }
                }
                if !found.is_empty() {
                    out.push((tkey.to_string(), found));
                }
            }
            out
        })
        .collect();

    let mut tech_to_eq: BTreeMap<String, Vec<String>> = BTreeMap::new();
    let mut eq_to_tech: BTreeMap<String, String> = BTreeMap::new();
    for list in per_tech {
        for (tkey, eqs) in list {
            for eq in &eqs {
                eq_to_tech.entry(eq.clone()).or_insert_with(|| tkey.clone());
            }
            tech_to_eq.insert(tkey, eqs);
        }
    }

    let eq_sig = vfs.dir_sig("common/units/equipment", ".txt");
    let tech_sig = vfs.dir_sig("common/technologies", ".txt");

    EquipMetaData {
        version: EQUIPMETA_VERSION,
        equipments,
        tech_to_eq,
        eq_to_tech,
        game_dir: vfs.game_dir.display().to_string(),
        eq_sig,
        tech_sig,
        mods: vfs.mods.iter().map(|m| m.name.clone()).collect(),
    }
}

pub fn write_equipmeta_pkl(data: &EquipMetaData, path: &Path) -> Result<usize, String> {
    let mut pw = PickleWriter::with_capacity(data.equipments.len() * 240 + 16384);
    pw.begin_small_dict();
    pw.write_str_intern("version");
    pw.write_int(data.version);
    pw.write_str_intern("source");
    pw.write_str("hoi4_recalc");
    pw.write_str_intern("mods");
    pw.write_list(&data.mods, |w, m| w.write_str(m));
    // 与 equipmeta._dir_sig 100% 同构的 sig 元组：(game_dir, ((eq_n, eq_mt), (tech_n, tech_mt)))
    pw.write_str_intern("sig");
    pw.write_tuple2(
        |w1| w1.write_str(&data.game_dir),
        |w2| {
            w2.write_tuple2(
                |wa| {
                    wa.write_tuple2(
                        |x| x.write_int(data.eq_sig.0 as i64),
                        |y| y.write_int(data.eq_sig.1),
                    )
                },
                |wb| {
                    wb.write_tuple2(
                        |x| x.write_int(data.tech_sig.0 as i64),
                        |y| y.write_int(data.tech_sig.1),
                    )
                },
            )
        },
    );
    pw.write_str_intern("equipments");
    pw.write_dict_iter(
        data.equipments.iter(),
        |w, k| w.write_str(k),
        |w, eq| {
            w.begin_small_dict();
            w.write_str_intern("key");
            w.write_str(&eq.key);
            w.write_str_intern("file");
            w.write_str_intern(&eq.file);
            w.write_str_intern("is_archetype");
            w.write_bool(eq.is_archetype);
            w.write_str_intern("is_buildable");
            w.write_bool(eq.is_buildable);
            w.write_str_intern("archetype");
            w.write_str_intern(&eq.archetype);
            w.write_str_intern("parent");
            w.write_opt_str(eq.parent.as_deref());
            w.write_str_intern("year");
            w.write_opt_int(eq.year);
            w.write_str_intern("visual_level");
            w.write_opt_int(eq.visual_level);
            w.write_str_intern("priority");
            w.write_opt_int(eq.priority);
            w.write_str_intern("interface_category");
            w.write_opt_str_intern(eq.interface_category.as_deref());
            w.write_str_intern("types");
            w.write_list(&eq.types, |w2, t| w2.write_str_intern(t));
            w.write_str_intern("stats");
            w.write_dict_iter(
                eq.stats.iter(),
                |w2, sk| w2.write_str_intern(sk),
                |w2, &sv| w2.write_float(sv),
            );
            w.end_small_dict();
        },
    );
    pw.write_str_intern("tech_to_eq");
    pw.write_dict_iter(
        data.tech_to_eq.iter(),
        |w, k| w.write_str(k),
        |w, list| w.write_list(list, |w2, s| w2.write_str(s)),
    );
    pw.write_str_intern("eq_to_tech");
    pw.write_dict_iter(
        data.eq_to_tech.iter(),
        |w, k| w.write_str(k),
        |w, v| w.write_str(v),
    );
    pw.end_small_dict();
    pw.save_to_file(path)
}
