//! 事件静态库 (`evdb_win.pkl`) 构建模块：并行解析 `events/**/*.txt` 并烘焙中英文文本。
//!
//! 精确对齐 `research/windows/wevdb.py` 的词法器、递归 AST、扁平化与本地化烘焙格式，
//! 确保 `calling/events_win.py` 的 `info()` / `search()` / `news()` 与 `_trig()` 触发器求值 100% 兼容。

use crate::assets::loc::build_loc_for_lang;
use crate::pdx::read_text_lossy;
use crate::pickle::PickleWriter;
use crate::vfs::PdxVfs;
use rayon::prelude::*;
use serde::Serialize;
use std::collections::BTreeMap;
use std::path::Path;

const EVENT_ROOTS: [&str; 5] = [
    "country_event",
    "news_event",
    "state_event",
    "unit_leader_event",
    "operative_leader_event",
];

#[derive(Debug, Clone)]
enum Tok {
    Word(String),
    Str(String),
    Op(String),
    LBrace,
    RBrace,
    Eq,
}

#[derive(Debug, Clone, Serialize)]
#[serde(untagged)]
pub enum PdxVal {
    None,
    Str(String),
    Block(Vec<(String, PdxVal)>),
}

#[derive(Debug, Clone, Serialize)]
pub struct EventOption {
    pub name: PdxVal,
    pub effects: Vec<String>,
    pub ai_chance: String,
    pub trigger: String,
    pub trigger_raw: Vec<(String, PdxVal)>,
    pub name_en: String,
    pub name_zh: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct EventDef {
    pub kind: String,
    pub file: String,
    pub title: PdxVal,
    pub desc: PdxVal,
    pub picture: PdxVal,
    pub major: PdxVal,
    pub is_triggered_only: PdxVal,
    pub hidden: PdxVal,
    pub fire_only_once: PdxVal,
    pub immediate: Vec<String>,
    pub options: Vec<EventOption>,
    pub title_en: String,
    pub desc_en: String,
    pub title_zh: String,
    pub desc_zh: String,
}

fn tokenize(text: &str) -> Vec<Tok> {
    let b = text.as_bytes();
    let n = b.len();
    let mut toks = Vec::with_capacity(n / 6);
    let mut i = 0usize;

    while i < n {
        let c = b[i];
        // 空白 + UTF-8 BOM
        if matches!(c, b' ' | b'\t' | b'\r' | b'\n') {
            i += 1;
            continue;
        }
        if c == 0xEF && i + 2 < n && b[i + 1] == 0xBB && b[i + 2] == 0xBF {
            i += 3;
            continue;
        }
        if c == b'#' {
            while i < n && b[i] != b'\r' && b[i] != b'\n' {
                i += 1;
            }
            continue;
        }
        if c == b'"' {
            let mut j = i + 1;
            let mut buf = String::new();
            while j < n {
                if b[j] == b'\\' && j + 1 < n {
                    let ch1 = text[j..].chars().next().unwrap();
                    buf.push(ch1);
                    j += ch1.len_utf8();
                    if j < n {
                        let ch2 = text[j..].chars().next().unwrap();
                        buf.push(ch2);
                        j += ch2.len_utf8();
                    }
                    continue;
                }
                if b[j] == b'"' {
                    break;
                }
                let ch = text[j..].chars().next().unwrap();
                buf.push(ch);
                j += ch.len_utf8();
            }
            toks.push(Tok::Str(buf));
            i = if j < n { j + 1 } else { n };
            continue;
        }
        if c == b'{' {
            toks.push(Tok::LBrace);
            i += 1;
            continue;
        }
        if c == b'}' {
            toks.push(Tok::RBrace);
            i += 1;
            continue;
        }
        if c == b'=' {
            toks.push(Tok::Eq);
            i += 1;
            continue;
        }
        if matches!(c, b'<' | b'>' | b'!') {
            let start = i;
            while i < n && matches!(b[i], b'<' | b'>' | b'!' | b'=') {
                i += 1;
            }
            toks.push(Tok::Op(text[start..i].to_string()));
            continue;
        }
        let start = i;
        while i < n
            && !matches!(
                b[i],
                b' ' | b'\t' | b'\r' | b'\n' | b'{' | b'}' | b'=' | b'"' | b'#' | b'<' | b'>' | b'!'
            )
        {
            i += 1;
        }
        if i == start {
            // 防死循环
            let ch = text[i..].chars().next().unwrap();
            i += ch.len_utf8();
        }
        toks.push(Tok::Word(text[start..i].to_string()));
    }
    toks
}

fn tok_text(t: &Tok) -> String {
    match t {
        Tok::Word(s) | Tok::Str(s) | Tok::Op(s) => s.clone(),
        Tok::LBrace => "{".to_string(),
        Tok::RBrace => "}".to_string(),
        Tok::Eq => "=".to_string(),
    }
}

fn parse_block(toks: &[Tok], pos: &mut usize) -> Vec<(String, PdxVal)> {
    let mut out = Vec::new();
    while *pos < toks.len() {
        match &toks[*pos] {
            Tok::RBrace => {
                *pos += 1;
                return out;
            }
            Tok::Word(v) | Tok::Str(v) => {
                let key = v.clone();
                *pos += 1;
                if *pos < toks.len() && matches!(toks[*pos], Tok::Eq) {
                    *pos += 1;
                    if *pos >= toks.len() {
                        out.push((key, PdxVal::None));
                        continue;
                    }
                    match &toks[*pos] {
                        Tok::LBrace => {
                            *pos += 1;
                            let blk = parse_block(toks, pos);
                            out.push((key, PdxVal::Block(blk)));
                        }
                        Tok::Op(op) => {
                            let op_s = op.clone();
                            *pos += 1;
                            let v2 = if *pos < toks.len() {
                                let s = tok_text(&toks[*pos]);
                                *pos += 1;
                                PdxVal::Str(s)
                            } else {
                                PdxVal::None
                            };
                            out.push((
                                key,
                                PdxVal::Block(vec![
                                    ("<op>".to_string(), PdxVal::Str(op_s)),
                                    ("<val>".to_string(), v2),
                                ]),
                            ));
                        }
                        other => {
                            let s = tok_text(other);
                            *pos += 1;
                            out.push((key, PdxVal::Str(s)));
                        }
                    }
                } else {
                    out.push((key, PdxVal::None));
                }
            }
            _ => {
                *pos += 1;
            }
        }
    }
    out
}

fn first_val<'a>(block: &'a [(String, PdxVal)], key: &str) -> &'a PdxVal {
    for (k, v) in block {
        if k == key {
            return v;
        }
    }
    &PdxVal::None
}

fn first_block<'a>(block: &'a [(String, PdxVal)], key: &str) -> &'a [(String, PdxVal)] {
    for (k, v) in block {
        if k == key {
            if let PdxVal::Block(b) = v {
                return b;
            }
            return &[];
        }
    }
    &[]
}

fn flat_str(block: &[(String, PdxVal)]) -> String {
    let mut parts = Vec::with_capacity(block.len());
    for (k, v) in block {
        if k == "<op>" {
            continue;
        }
        match v {
            PdxVal::Block(sub) => parts.push(format!("{} = {{ {} }}", k, flat_str(sub))),
            PdxVal::Str(s) => parts.push(format!("{} = {}", k, s)),
            PdxVal::None => parts.push(k.clone()),
        }
    }
    parts.join(" ")
}

fn flat_lines(block: &[(String, PdxVal)]) -> Vec<String> {
    let mut out = Vec::with_capacity(block.len());
    for (k, v) in block {
        if k == "<op>" {
            continue;
        }
        match v {
            PdxVal::Block(sub) => {
                let has_non_op = sub.iter().any(|(sk, _)| sk != "<op>");
                if has_non_op {
                    out.push(format!("{} = {{ {} }}", k, flat_str(sub)));
                } else {
                    let ops: Vec<&str> = sub
                        .iter()
                        .filter_map(|(sk, sv)| {
                            if sk == "<op>" {
                                if let PdxVal::Str(s) = sv {
                                    return Some(s.as_str());
                                }
                            }
                            None
                        })
                        .collect();
                    out.push(format!("{} {}", k, ops.join(" ")));
                }
            }
            PdxVal::Str(s) => out.push(format!("{} = {}", k, s)),
            PdxVal::None => out.push(format!("{} = None", k)),
        }
    }
    out
}

fn loc_lookup_event(map: &BTreeMap<String, String>, key: &PdxVal) -> String {
    let PdxVal::Str(k) = key else {
        return String::new();
    };
    if k.is_empty() {
        return String::new();
    }
    match map.get(k) {
        Some(v) => {
            if v.contains("\\n") {
                v.replace("\\n", "\n")
            } else {
                v.clone()
            }
        }
        None => String::new(),
    }
}

/// 并行构建全量事件数据库（含英文 & 简体中文烘焙）。
pub fn build_evdb(
    vfs: &PdxVfs,
    cached_en: Option<&BTreeMap<String, String>>,
    cached_zh: Option<&BTreeMap<String, String>>,
) -> BTreeMap<String, EventDef> {
    let files = vfs.walk_dir_files("events", ".txt", &[]);

    // 并行确保 en 和 zh 两份本地化可用（若主流程已解析 english，则复用，零重复读盘）
    let (owned_en, owned_zh) = rayon::join(
        || {
            if cached_en.is_none() {
                Some(build_loc_for_lang(vfs, Some("english")).keys)
            } else {
                None
            }
        },
        || {
            if cached_zh.is_none() {
                Some(build_loc_for_lang(vfs, Some("simp_chinese")).keys)
            } else {
                None
            }
        },
    );
    let empty_loc = BTreeMap::new();
    let loc_en = cached_en.or(owned_en.as_ref()).unwrap_or(&empty_loc);
    let loc_zh = cached_zh.or(owned_zh.as_ref()).unwrap_or(&empty_loc);

    let per_file: Vec<Vec<(String, EventDef)>> = files
        .par_iter()
        .map(|ent| {
            let Ok(text) = read_text_lossy(&ent.disk_path) else {
                return Vec::new();
            };
            let toks = tokenize(&text);
            let mut pos = 0usize;
            let tree = parse_block(&toks, &mut pos);
            // Windows 风格相对路径（与 wevdb.py os.path.relpath 保持一致）
            let rel_file = ent.rel_path.replace('/', "\\");
            let mut out = Vec::new();

            for (kind, body_val) in tree {
                if !EVENT_ROOTS.contains(&kind.as_str()) {
                    continue;
                }
                let PdxVal::Block(body) = body_val else {
                    continue;
                };
                let PdxVal::Str(eid) = first_val(&body, "id") else {
                    continue;
                };
                if eid.is_empty() {
                    continue;
                }
                let eid = eid.clone();

                let mut options = Vec::new();
                for (ok, ov) in &body {
                    if ok != "option" {
                        continue;
                    }
                    let PdxVal::Block(ob) = ov else {
                        continue;
                    };
                    let name = first_val(ob, "name").clone();
                    let mut effects = Vec::new();
                    for (ek, ev) in ob {
                        if matches!(
                            ek.as_str(),
                            "name"
                                | "ai_chance"
                                | "trigger"
                                | "original_recipient_only"
                                | "highlight_states"
                        ) {
                            continue;
                        }
                        match ev {
                            PdxVal::Block(sub) => {
                                effects.push(format!("{} = {{ {} }}", ek, flat_str(sub)));
                            }
                            PdxVal::Str(s) => effects.push(format!("{} = {}", ek, s)),
                            PdxVal::None => effects.push(ek.clone()),
                        }
                    }
                    let ai_blk = first_block(ob, "ai_chance");
                    let trig_blk = first_block(ob, "trigger");
                    let name_en = loc_lookup_event(loc_en, &name);
                    let name_zh = loc_lookup_event(loc_zh, &name);
                    options.push(EventOption {
                        name,
                        effects,
                        ai_chance: flat_str(ai_blk),
                        trigger: flat_str(trig_blk),
                        trigger_raw: trig_blk.to_vec(),
                        name_en,
                        name_zh,
                    });
                }

                let title = first_val(&body, "title").clone();
                let desc = first_val(&body, "desc").clone();
                let title_en = loc_lookup_event(loc_en, &title);
                let desc_en = loc_lookup_event(loc_en, &desc);
                let title_zh = loc_lookup_event(loc_zh, &title);
                let desc_zh = loc_lookup_event(loc_zh, &desc);

                out.push((
                    eid,
                    EventDef {
                        kind,
                        file: rel_file.clone(),
                        title,
                        desc,
                        picture: first_val(&body, "picture").clone(),
                        major: first_val(&body, "major").clone(),
                        is_triggered_only: first_val(&body, "is_triggered_only").clone(),
                        hidden: first_val(&body, "hidden").clone(),
                        fire_only_once: first_val(&body, "fire_only_once").clone(),
                        immediate: flat_lines(first_block(&body, "immediate")),
                        options,
                        title_en,
                        desc_en,
                        title_zh,
                        desc_zh,
                    },
                ));
            }
            out
        })
        .collect();

    let mut db: BTreeMap<String, EventDef> = BTreeMap::new();
    for list in per_file {
        for (eid, ev) in list {
            db.insert(eid, ev);
        }
    }
    db
}

fn write_pdx_val(pw: &mut PickleWriter, v: &PdxVal) {
    match v {
        PdxVal::None => pw.write_none(),
        PdxVal::Str(s) => pw.write_str(s),
        PdxVal::Block(pairs) => {
            pw.write_list(pairs, |w, (k, val)| {
                w.write_tuple2(|w1| w1.write_str(k), |w2| write_pdx_val(w2, val));
            });
        }
    }
}

pub fn write_evdb_pkl(db: &BTreeMap<String, EventDef>, path: &Path) -> Result<usize, String> {
    let mut pw = PickleWriter::with_capacity(db.len() * 1400 + 65536);
    pw.write_dict_iter(
        db.iter(),
        |w, eid| w.write_str(eid),
        |w, e| {
            w.begin_small_dict();
            w.write_str_intern("kind");
            w.write_str_intern(&e.kind);
            w.write_str_intern("file");
            w.write_str_intern(&e.file);
            w.write_str_intern("title");
            write_pdx_val(w, &e.title);
            w.write_str_intern("desc");
            write_pdx_val(w, &e.desc);
            w.write_str_intern("picture");
            write_pdx_val(w, &e.picture);
            w.write_str_intern("major");
            write_pdx_val(w, &e.major);
            w.write_str_intern("is_triggered_only");
            write_pdx_val(w, &e.is_triggered_only);
            w.write_str_intern("hidden");
            write_pdx_val(w, &e.hidden);
            w.write_str_intern("fire_only_once");
            write_pdx_val(w, &e.fire_only_once);
            w.write_str_intern("immediate");
            w.write_list(&e.immediate, |w2, s| w2.write_str(s));
            w.write_str_intern("options");
            w.write_list(&e.options, |w2, o| {
                w2.begin_small_dict();
                w2.write_str_intern("name");
                write_pdx_val(w2, &o.name);
                w2.write_str_intern("effects");
                w2.write_list(&o.effects, |w3, s| w3.write_str(s));
                w2.write_str_intern("ai_chance");
                w2.write_str(&o.ai_chance);
                w2.write_str_intern("trigger");
                w2.write_str(&o.trigger);
                w2.write_str_intern("trigger_raw");
                w2.write_list(&o.trigger_raw, |w3, (k, val)| {
                    w3.write_tuple2(|wa| wa.write_str(k), |wb| write_pdx_val(wb, val));
                });
                w2.write_str_intern("name_en");
                w2.write_str(&o.name_en);
                w2.write_str_intern("name_zh");
                w2.write_str(&o.name_zh);
                w2.end_small_dict();
            });
            w.write_str_intern("title_en");
            w.write_str(&e.title_en);
            w.write_str_intern("desc_en");
            w.write_str(&e.desc_en);
            w.write_str_intern("title_zh");
            w.write_str(&e.title_zh);
            w.write_str_intern("desc_zh");
            w.write_str(&e.desc_zh);
            w.end_small_dict();
        },
    );
    pw.save_to_file(path)
}
