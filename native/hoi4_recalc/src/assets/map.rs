//! 地图与地理静态资产构建模块：
//! - `mapadj.pkl` (`dict[int, set[int]]`) — `provinces.bmp` + `definition.csv`
//! - `maprivers.pkl` (`dict[tuple[int, int], "small"|"large"]`) — `rivers.bmp` + `provinces.bmp`
//! - `mapstates.pkl` (`tuple[dict[int, set[int]], dict[int, int]]`) — `history/states/*.txt`
//! - `statemeta.pkl` (`dict`) — `history/states/*.txt` + `common/state_category/*.txt` + `common/buildings/*.txt`
//! - `navalregions.pkl` (`dict`) — `map/strategicregions/*.txt` + `localisation`

use crate::pdx::{
    all_blocks_after, find_all_uints, first_ident_after, first_int_after, first_quoted_after,
    match_brace_block, read_text_lossy, skip_ws, strip_comments,
};
use crate::pickle::PickleWriter;
use crate::vfs::PdxVfs;
use rayon::prelude::*;
use serde::Serialize;
use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};
use std::fs;
use std::path::Path;

const RIVER_MIN_RATIO: f64 = 0.30;
const RIVER_MIN_PIXELS: u32 = 6;

pub const NAVAL_REGIONS_SCHEMA: i64 = 1;
pub const STATE_META_SCHEMA: i64 = 1;

#[derive(Debug, Clone, Serialize)]
pub struct MapBmpOutputs {
    pub adj: BTreeMap<i64, BTreeSet<i64>>,
    pub rivers: BTreeMap<(i64, i64), String>,
    pub width: usize,
    pub height: usize,
}

#[derive(Debug, Clone, Serialize)]
pub struct StateMetaRow {
    pub name_key: Option<String>,
    pub category: Option<String>,
}

#[derive(Debug, Clone, Serialize)]
pub struct StateMetaData {
    pub schema: i64,
    pub states: BTreeMap<i64, StateMetaRow>,
    pub categories: BTreeMap<String, i64>,
    pub slot_buildings: BTreeSet<String>,
}

#[derive(Debug, Clone, Serialize)]
pub struct MapStatesData {
    pub st2p: BTreeMap<i64, BTreeSet<i64>>,
    pub p2st: BTreeMap<i64, i64>,
}

#[derive(Debug, Clone, Serialize)]
pub struct NavalRegionRow {
    pub id: i64,
    pub name_key: String,
    pub naval_terrain: Option<String>,
    pub provinces: i64,
    pub name: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct NavalRegionsData {
    pub schema: i64,
    pub regions: BTreeMap<i64, NavalRegionRow>,
    pub by_name: BTreeMap<String, i64>,
}

#[inline]
fn read_u16_le(b: &[u8], off: usize) -> u16 {
    u16::from_le_bytes([b[off], b[off + 1]])
}

#[inline]
fn read_u32_le(b: &[u8], off: usize) -> u32 {
    u32::from_le_bytes([b[off], b[off + 1], b[off + 2], b[off + 3]])
}

#[inline]
fn read_i32_le(b: &[u8], off: usize) -> i32 {
    i32::from_le_bytes([b[off], b[off + 1], b[off + 2], b[off + 3]])
}

#[inline]
fn pack_edge(a: i32, b: i32) -> u64 {
    let (lo, hi) = if a < b { (a as u32, b as u32) } else { (b as u32, a as u32) };
    ((lo as u64) << 32) | (hi as u64)
}

#[inline]
fn unpack_edge(k: u64) -> (i64, i64) {
    ((k >> 32) as u32 as i64, (k & 0xFFFF_FFFF) as u32 as i64)
}

/// 读取 `map/definition.csv`，构造 `rgb24 -> province_id` 的 O(1) 扁平查找表（长度 2^24）。
fn load_rgb_lut(vfs: &PdxVfs) -> Result<Vec<i32>, String> {
    let def_path = vfs
        .resolve_file("map/definition.csv")
        .ok_or_else(|| "找不到 map/definition.csv".to_string())?;
    let raw = fs::read(&def_path)
        .map_err(|e| format!("读取 {} 失败: {}", def_path.display(), e))?;
    // definition.csv 为 latin-1 / ASCII 兼容文本，逐字节行解析即可
    let mut lut = vec![-1i32; 1usize << 24];
    for line in raw.split(|&c| c == b'\n') {
        let s = String::from_utf8_lossy(line);
        let trimmed = s.trim();
        if trimmed.is_empty() {
            continue;
        }
        let mut parts = trimmed.split(';');
        let (Some(p0), Some(p1), Some(p2), Some(p3)) =
            (parts.next(), parts.next(), parts.next(), parts.next())
        else {
            continue;
        };
        // 与 mapdata.py 一致：要求至少 8 列
        if parts.count() < 4 {
            continue;
        }
        let (Ok(pid), Ok(r), Ok(g), Ok(b)) = (
            p0.parse::<i32>(),
            p1.parse::<u32>(),
            p2.parse::<u32>(),
            p3.parse::<u32>(),
        ) else {
            continue;
        };
        if r <= 255 && g <= 255 && b <= 255 {
            let rgb = ((r << 16) | (g << 8) | b) as usize;
            lut[rgb] = pid;
        }
    }
    Ok(lut)
}

/// 一趟并行扫描 `provinces.bmp` (+ 可选 `rivers.bmp`)，同时产出 `mapadj` 与 `maprivers`。
pub fn build_map_bmps(
    vfs: &PdxVfs,
    want_adj: bool,
    want_rivers: bool,
) -> Result<MapBmpOutputs, String> {
    let lut = load_rgb_lut(vfs)?;

    let prov_path = vfs
        .resolve_file("map/provinces.bmp")
        .ok_or_else(|| "找不到 map/provinces.bmp".to_string())?;
    let prov_raw = fs::read(&prov_path)
        .map_err(|e| format!("读取 {} 失败: {}", prov_path.display(), e))?;
    if prov_raw.len() < 54 {
        return Err("provinces.bmp 文件头不完整".to_string());
    }
    let p_off = read_u32_le(&prov_raw, 10) as usize;
    let pw = read_i32_le(&prov_raw, 18);
    let ph_raw = read_i32_le(&prov_raw, 22);
    let p_bpp = read_u16_le(&prov_raw, 28);
    if p_bpp != 24 || pw <= 0 || ph_raw == 0 {
        return Err(format!(
            "provinces.bmp 格式不受支持: {}x{} bpp={}",
            pw, ph_raw, p_bpp
        ));
    }
    let w = pw as usize;
    let h = ph_raw.unsigned_abs() as usize;
    let p_flip = ph_raw > 0;
    let p_stride = (w * 3 + 3) & !3;
    if p_off + p_stride * h > prov_raw.len() {
        return Err("provinces.bmp 像素数据被截断".to_string());
    }

    // 并行解码 provinces.bmp -> 顶向下 province_id 矩阵
    let mut ids = vec![-1i32; w * h];
    ids.par_chunks_mut(w).enumerate().for_each(|(y, dst_row)| {
        let src_y = if p_flip { h - 1 - y } else { y };
        let row_bytes = &prov_raw[p_off + src_y * p_stride..p_off + src_y * p_stride + w * 3];
        for x in 0..w {
            let base = x * 3;
            let b = row_bytes[base] as usize;
            let g = row_bytes[base + 1] as usize;
            let r = row_bytes[base + 2] as usize;
            dst_row[x] = lut[(r << 16) | (g << 8) | b];
        }
    });
    drop(lut);
    drop(prov_raw);

    // 若需要 rivers，解码 rivers.bmp 为顶向下 uint8 矩阵
    let riv_opt: Option<Vec<u8>> = if want_rivers {
        let riv_path = vfs
            .resolve_file("map/rivers.bmp")
            .ok_or_else(|| "找不到 map/rivers.bmp".to_string())?;
        let riv_raw = fs::read(&riv_path)
            .map_err(|e| format!("读取 {} 失败: {}", riv_path.display(), e))?;
        if riv_raw.len() < 54 {
            return Err("rivers.bmp 文件头不完整".to_string());
        }
        let r_off = read_u32_le(&riv_raw, 10) as usize;
        let rw = read_i32_le(&riv_raw, 18) as usize;
        let rh_raw = read_i32_le(&riv_raw, 22);
        let rh = rh_raw.unsigned_abs() as usize;
        let r_bpp = read_u16_le(&riv_raw, 28);
        if r_bpp != 8 || rw != w || rh != h {
            return Err(format!(
                "rivers.bmp 尺寸或色深与 provinces.bmp 不匹配: {}x{} bpp={} vs {}x{}",
                rw, rh, r_bpp, w, h
            ));
        }
        let r_flip = rh_raw > 0;
        let r_stride = (w + 3) & !3;
        if r_off + r_stride * h > riv_raw.len() {
            return Err("rivers.bmp 像素数据被截断".to_string());
        }
        let mut riv = vec![255u8; w * h];
        riv.par_chunks_mut(w).enumerate().for_each(|(y, dst_row)| {
            let src_y = if r_flip { h - 1 - y } else { y };
            let src_row = &riv_raw[r_off + src_y * r_stride..r_off + src_y * r_stride + w];
            dst_row.copy_from_slice(src_row);
        });
        Some(riv)
    } else {
        None
    };

    // 按行分条带并行统计邻接边与跨河像素比例
    let strip_rows = 64usize;
    let n_strips = (h + strip_rows - 1) / strip_rows;

    let strip_results: Vec<(HashSet<u64>, HashMap<u64, (u32, u32, u32)>)> = (0..n_strips)
        .into_par_iter()
        .map(|si| {
            let y0 = si * strip_rows;
            let y1 = (y0 + strip_rows).min(h);
            let mut local_adj: HashSet<u64> = HashSet::with_capacity(if want_adj { 2048 } else { 0 });
            let mut local_riv: HashMap<u64, (u32, u32, u32)> =
                HashMap::with_capacity(if want_rivers { 1024 } else { 0 });
            let riv_slice = riv_opt.as_deref();

            for y in y0..y1 {
                let row_off = y * w;
                let row = &ids[row_off..row_off + w];
                let rrow = riv_slice.map(|rv| &rv[row_off..row_off + w]);

                // 水平相邻 (x, x+1)
                for x in 0..(w - 1) {
                    let a = row[x];
                    let b = row[x + 1];
                    if a != b && a >= 0 && b >= 0 {
                        let key = pack_edge(a, b);
                        if want_adj {
                            local_adj.insert(key);
                        }
                        if let Some(rr) = rrow {
                            let ra = rr[x];
                            let rb = rr[x + 1];
                            let is_r = ra <= 11 || rb <= 11;
                            let is_b = (7..=11).contains(&ra) || (7..=11).contains(&rb);
                            let ent = local_riv.entry(key).or_insert((0, 0, 0));
                            ent.0 += 1;
                            if is_r {
                                ent.1 += 1;
                                if is_b {
                                    ent.2 += 1;
                                }
                            }
                        }
                    }
                }

                // 东西环绕 (w-1, 0)：与 mapdata.py 保持一致，仅计入 mapadj
                if want_adj && w > 1 {
                    let a = row[w - 1];
                    let b = row[0];
                    if a != b && a >= 0 && b >= 0 {
                        local_adj.insert(pack_edge(a, b));
                    }
                }

                // 垂直相邻 (y, y+1)
                if y + 1 < h {
                    let next_off = (y + 1) * w;
                    let nrow = &ids[next_off..next_off + w];
                    let nrrow = riv_slice.map(|rv| &rv[next_off..next_off + w]);
                    for x in 0..w {
                        let a = row[x];
                        let b = nrow[x];
                        if a != b && a >= 0 && b >= 0 {
                            let key = pack_edge(a, b);
                            if want_adj {
                                local_adj.insert(key);
                            }
                            if let (Some(rr), Some(nrr)) = (rrow, nrrow) {
                                let ra = rr[x];
                                let rb = nrr[x];
                                let is_r = ra <= 11 || rb <= 11;
                                let is_b = (7..=11).contains(&ra) || (7..=11).contains(&rb);
                                let ent = local_riv.entry(key).or_insert((0, 0, 0));
                                ent.0 += 1;
                                if is_r {
                                    ent.1 += 1;
                                    if is_b {
                                        ent.2 += 1;
                                    }
                                }
                            }
                        }
                    }
                }
            }
            (local_adj, local_riv)
        })
        .collect();

    let mut adj: BTreeMap<i64, BTreeSet<i64>> = BTreeMap::new();
    let mut merged_riv: HashMap<u64, (u32, u32, u32)> = HashMap::new();

    for (la, lr) in strip_results {
        if want_adj {
            for k in la {
                let (a, b) = unpack_edge(k);
                adj.entry(a).or_default().insert(b);
                adj.entry(b).or_default().insert(a);
            }
        }
        if want_rivers {
            for (k, (t, r, b)) in lr {
                let ent = merged_riv.entry(k).or_insert((0, 0, 0));
                ent.0 += t;
                ent.1 += r;
                ent.2 += b;
            }
        }
    }

    let mut rivers: BTreeMap<(i64, i64), String> = BTreeMap::new();
    if want_rivers {
        for (k, (total, r_cnt, big_cnt)) in merged_riv {
            if r_cnt >= RIVER_MIN_PIXELS && (r_cnt as f64) / (total as f64) >= RIVER_MIN_RATIO {
                let pair = unpack_edge(k);
                let kind = if big_cnt * 2 >= r_cnt { "large" } else { "small" };
                rivers.insert(pair, kind.to_string());
            }
        }
    }

    Ok(MapBmpOutputs {
        adj,
        rivers,
        width: w,
        height: h,
    })
}

pub fn write_mapadj_pkl(adj: &BTreeMap<i64, BTreeSet<i64>>, path: &Path) -> Result<usize, String> {
    let mut pw = PickleWriter::with_capacity(adj.len() * 32 + 16384);
    pw.write_dict_iter(
        adj.iter(),
        |w, &k| w.write_int(k),
        |w, nb| w.write_set_iter(nb.iter().copied(), |w2, v| w2.write_int(v)),
    );
    pw.save_to_file(path)
}

pub fn write_maprivers_pkl(
    rivers: &BTreeMap<(i64, i64), String>,
    path: &Path,
) -> Result<usize, String> {
    let mut pw = PickleWriter::with_capacity(rivers.len() * 20 + 8192);
    pw.write_dict_iter(
        rivers.iter(),
        |w, &(a, b)| w.write_tuple2(|w1| w1.write_int(a), |w2| w2.write_int(b)),
        |w, kind| w.write_str_intern(kind),
    );
    pw.save_to_file(path)
}

/// 联合扫描 `history/states/*.txt`、`common/state_category/*.txt`、`common/buildings/*.txt`，
/// 一次同时产出 `mapstates` 与 `statemeta`。
pub fn build_states_and_meta(vfs: &PdxVfs) -> (MapStatesData, StateMetaData) {
    let state_files = vfs.list_dir_files("history/states", ".txt");

    // 并行解析每个州文件
    struct ParsedState {
        id: i64,
        provs: Vec<i64>,
        name_key: Option<String>,
        category: Option<String>,
    }

    let parsed: Vec<Option<ParsedState>> = state_files
        .par_iter()
        .map(|ent| {
            let text = read_text_lossy(&ent.disk_path).ok()?;
            let clean = strip_comments(&text);
            let sid = first_int_after(&clean, "id", false)?;
            let provs = first_block_after_simple(&clean, "provinces")
                .map(find_all_uints)
                .unwrap_or_default();
            let name_key = first_quoted_after(&clean, "name", false).map(|s| s.to_string());
            let category = first_quoted_after(&clean, "state_category", false)
                .or_else(|| first_ident_after(&clean, "state_category", true))
                .map(|s| s.trim().to_string())
                .filter(|s| !s.is_empty());
            Some(ParsedState {
                id: sid,
                provs,
                name_key,
                category,
            })
        })
        .collect();

    let mut st2p: BTreeMap<i64, BTreeSet<i64>> = BTreeMap::new();
    let mut p2st: BTreeMap<i64, i64> = BTreeMap::new();
    let mut states_meta: BTreeMap<i64, StateMetaRow> = BTreeMap::new();

    for item in parsed.into_iter().flatten() {
        if !item.provs.is_empty() {
            let mut set = BTreeSet::new();
            for p in item.provs {
                set.insert(p);
                p2st.insert(p, item.id);
            }
            st2p.insert(item.id, set);
        }
        states_meta.insert(
            item.id,
            StateMetaRow {
                name_key: item.name_key,
                category: item.category,
            },
        );
    }

    // 解析 common/state_category/*.txt
    let cat_files = vfs.list_dir_files("common/state_category", ".txt");
    let mut categories: BTreeMap<String, i64> = BTreeMap::new();
    for ent in &cat_files {
        if let Ok(text) = read_text_lossy(&ent.disk_path) {
            let clean = strip_comments(&text);
            if let Some(slots) = first_int_after(&clean, "local_building_slots", true) {
                let stem = ent
                    .file_name
                    .strip_suffix(".txt")
                    .or_else(|| ent.file_name.strip_suffix(".TXT"))
                    .unwrap_or(&ent.file_name);
                categories.insert(stem.to_string(), slots);
            }
        }
    }

    // 解析 common/buildings/*.txt 提取 shares_slots = yes
    let bld_files = vfs.list_dir_files("common/buildings", ".txt");
    let mut slot_buildings: BTreeSet<String> = BTreeSet::new();
    for ent in &bld_files {
        if let Ok(text) = read_text_lossy(&ent.disk_path) {
            let clean = strip_comments(&text);
            for (name, body) in iter_named_blocks(&clean) {
                if name == "buildings" {
                    for (sub, sub_body) in iter_named_blocks(body) {
                        if has_shares_slots_yes(sub_body) {
                            slot_buildings.insert(sub.to_string());
                        }
                    }
                } else if has_shares_slots_yes(body) {
                    slot_buildings.insert(name.to_string());
                }
            }
        }
    }

    (
        MapStatesData { st2p, p2st },
        StateMetaData {
            schema: STATE_META_SCHEMA,
            states: states_meta,
            categories,
            slot_buildings,
        },
    )
}

fn first_block_after_simple<'a>(text: &'a str, key: &str) -> Option<&'a str> {
    crate::pdx::first_block_after(text, key)
}

fn has_shares_slots_yes(body: &str) -> bool {
    matches!(crate::pdx::first_bool_after(body, "shares_slots"), Some(true))
}

/// 提取 `text` 顶层（不含嵌套子块）的 `name = { ... }` 块列表，等价于 `statemeta._blocks`。
fn iter_named_blocks(text: &str) -> Vec<(&str, &str)> {
    let b = text.as_bytes();
    let n = b.len();
    let mut out = Vec::new();
    let mut i = 0usize;
    while i < n {
        if b[i].is_ascii_alphabetic() || b[i] == b'_' {
            let start = i;
            i += 1;
            while i < n && (b[i].is_ascii_alphanumeric() || b[i] == b'_') {
                i += 1;
            }
            let name = &text[start..i];
            let p = skip_ws(b, i);
            if p < n && b[p] == b'=' {
                let q = skip_ws(b, p + 1);
                if q < n && b[q] == b'{' {
                    if let Some((inner, after)) = match_brace_block(text, q + 1) {
                        out.push((name, inner));
                        i = after;
                        continue;
                    }
                }
            }
            continue;
        }
        i += 1;
    }
    out
}

pub fn write_mapstates_pkl(data: &MapStatesData, path: &Path) -> Result<usize, String> {
    let mut pw = PickleWriter::with_capacity(data.p2st.len() * 12 + 16384);
    pw.write_tuple2(
        |w1| {
            w1.write_dict_iter(
                data.st2p.iter(),
                |w, &sid| w.write_int(sid),
                |w, ps| w.write_set_iter(ps.iter().copied(), |w2, p| w2.write_int(p)),
            );
        },
        |w2| {
            w2.write_dict_iter(
                data.p2st.iter(),
                |w, &pid| w.write_int(pid),
                |w, &sid| w.write_int(sid),
            );
        },
    );
    pw.save_to_file(path)
}

pub fn write_statemeta_pkl(data: &StateMetaData, path: &Path) -> Result<usize, String> {
    let mut pw = PickleWriter::with_capacity(data.states.len() * 48 + 8192);
    pw.begin_small_dict();
    pw.write_str_intern("schema");
    pw.write_int(data.schema);
    pw.write_str_intern("source");
    pw.write_str("hoi4_recalc");
    pw.write_str_intern("states");
    pw.write_dict_iter(
        data.states.iter(),
        |w, &sid| w.write_int(sid),
        |w, row| {
            w.begin_small_dict();
            w.write_str_intern("name_key");
            w.write_opt_str(row.name_key.as_deref());
            w.write_str_intern("category");
            w.write_opt_str_intern(row.category.as_deref());
            w.end_small_dict();
        },
    );
    pw.write_str_intern("categories");
    pw.write_dict_iter(
        data.categories.iter(),
        |w, k| w.write_str_intern(k),
        |w, &v| w.write_int(v),
    );
    pw.write_str_intern("slot_buildings");
    pw.write_set_iter(data.slot_buildings.iter(), |w, s| w.write_str_intern(s));
    pw.end_small_dict();
    pw.save_to_file(path)
}

/// 解析 `map/strategicregions/*.txt` 并结合本地化字典生成 `navalregions`。
pub fn build_naval_regions(
    vfs: &PdxVfs,
    loc_keys: Option<&BTreeMap<String, String>>,
) -> NavalRegionsData {
    let files = vfs.list_dir_files("map/strategicregions", ".txt");

    let per_file: Vec<Vec<(i64, Option<String>, Option<String>, i64)>> = files
        .par_iter()
        .map(|ent| {
            let Ok(raw) = read_text_lossy(&ent.disk_path) else {
                return Vec::new();
            };
            let clean = strip_comments(&raw);
            let mut rows = Vec::new();
            for body in all_blocks_after(&clean, "strategic_region") {
                let Some(rid) = first_int_after(body, "id", false) else {
                    continue;
                };
                let name_key = first_quoted_after(body, "name", true).map(|s| s.to_string());
                let naval_terrain =
                    first_ident_after(body, "naval_terrain", true).map(|s| s.to_string());
                let prov_cnt = first_block_after_simple(body, "provinces")
                    .map(|p| p.split_whitespace().count() as i64)
                    .unwrap_or(0);
                rows.push((rid, name_key, naval_terrain, prov_cnt));
            }
            rows
        })
        .collect();

    // 若调用方未提供 loc_keys（如 `--only navalregions`），临时解析默认语言 localisation
    let owned_loc = if loc_keys.is_none() {
        Some(crate::assets::loc::build_loc_for_lang(vfs, None).keys)
    } else {
        None
    };
    let names = loc_keys.or(owned_loc.as_ref());

    let mut regions: BTreeMap<i64, NavalRegionRow> = BTreeMap::new();
    for rows in per_file {
        for (rid, nk_opt, naval_terrain, provinces) in rows {
            let name_key = nk_opt.unwrap_or_else(|| format!("STRATEGICREGION_{}", rid));
            let name = names
                .and_then(|m| m.get(&name_key))
                .cloned()
                .unwrap_or_else(|| name_key.clone());
            regions.insert(
                rid,
                NavalRegionRow {
                    id: rid,
                    name_key,
                    naval_terrain,
                    provinces,
                    name,
                },
            );
        }
    }

    let mut by_name: BTreeMap<String, i64> = BTreeMap::new();
    for (&rid, rec) in &regions {
        by_name
            .entry(rec.name.trim().to_lowercase())
            .or_insert(rid);
        by_name
            .entry(rec.name_key.trim().to_lowercase())
            .or_insert(rid);
    }

    NavalRegionsData {
        schema: NAVAL_REGIONS_SCHEMA,
        regions,
        by_name,
    }
}

pub fn write_naval_regions_pkl(data: &NavalRegionsData, path: &Path) -> Result<usize, String> {
    let mut pw = PickleWriter::with_capacity(data.regions.len() * 128 + 8192);
    pw.begin_small_dict();
    pw.write_str_intern("schema");
    pw.write_int(data.schema);
    pw.write_str_intern("regions");
    pw.write_dict_iter(
        data.regions.iter(),
        |w, &rid| w.write_int(rid),
        |w, rec| {
            w.begin_small_dict();
            w.write_str_intern("id");
            w.write_int(rec.id);
            w.write_str_intern("name_key");
            w.write_str(&rec.name_key);
            w.write_str_intern("naval_terrain");
            w.write_opt_str_intern(rec.naval_terrain.as_deref());
            w.write_str_intern("provinces");
            w.write_int(rec.provinces);
            w.write_str_intern("name");
            w.write_str(&rec.name);
            w.end_small_dict();
        },
    );
    pw.write_str_intern("by_name");
    pw.write_dict_iter(
        data.by_name.iter(),
        |w, k| w.write_str(k),
        |w, &rid| w.write_int(rid),
    );
    pw.end_small_dict();
    pw.save_to_file(path)
}
