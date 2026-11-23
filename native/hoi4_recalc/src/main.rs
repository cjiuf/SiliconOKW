//! HOI4 Windows PE 离线版本重算独立 CLI (`hoi4_recalc`)。
//!
//! 用法：
//!   hoi4_recalc scan --exe <path\to\hoi4.exe> [--out <profile.json>]
//!                    [--rtti-out <rtti.json>] [--report <report.md>] [--verify-1195]
//!   hoi4_recalc diff --old <old_profile.json> --new <new_profile.json> [--out <diff.md>]
//!   hoi4_recalc assets --game <game_dir> [--out-dir <calling_dir>] [--mod <dir_or_.mod>]...
//!                      [--dlc-load <dlc_load.json>] [--lang <english|simp_chinese>]

mod anchors;
mod assets;
mod baseline;
mod commands;
mod pdx;
mod pe;
mod pickle;
mod rtti;
mod vfs;

use baseline::{render_diff, render_report, verify_1195, Profile};
use serde::Serialize;
use std::collections::BTreeMap;
use std::env;
use std::fs;
use std::path::Path;
use std::process;
use std::time::Instant;

#[derive(Serialize)]
struct RttiJsonExport<'a> {
    pe: &'a pe::PeMeta,
    stats: &'a rtti::RttiStats,
    vtables: &'a BTreeMap<String, Vec<u32>>,
    decorated: &'a BTreeMap<String, Vec<u32>>,
}

fn usage() -> ! {
    eprintln!(
        "HOI4 Windows PE 离线版本重算与静态资产构建工具 (hoi4_recalc v{})\n\
         \n\
         用法:\n\
         \x20 hoi4_recalc scan   --exe <hoi4.exe> [--out <profile.json>] [--rtti-out <rtti.json>]\n\
         \x20                    [--report <report.md>] [--verify-1195]\n\
         \x20 hoi4_recalc diff   --old <a.json> --new <b.json> [--out <diff.md>]\n\
         \x20 hoi4_recalc assets --game <game_dir> [--out-dir <calling_dir>] [--evdb-out <evdb_win.pkl>]\n\
         \x20                    [--mod <dir_or_.mod>]... [--dlc-load <dlc_load.json>] [--user-dir <dir>]\n\
         \x20                    [--lang <english|simp_chinese>] [--only <list>] [--format <pkl|json|both>]\n\
         \x20                    [--manifest <manifest.json>] [--report <report.md>]",
        env!("CARGO_PKG_VERSION")
    );
    process::exit(2);
}

fn write_text(path: &str, content: &str) -> Result<(), String> {
    if let Some(parent) = Path::new(path).parent() {
        if !parent.as_os_str().is_empty() {
            fs::create_dir_all(parent)
                .map_err(|e| format!("创建目录失败 {}: {}", parent.display(), e))?;
        }
    }
    fs::write(path, content).map_err(|e| format!("写入文件失败 {}: {}", path, e))
}

fn cmd_scan(args: &[String]) -> Result<i32, String> {
    let mut exe_path: Option<String> = None;
    let mut out_path: Option<String> = None;
    let mut rtti_out: Option<String> = None;
    let mut report_path: Option<String> = None;
    let mut do_verify = false;

    let mut i = 0;
    while i < args.len() {
        match args[i].as_str() {
            "--exe" => {
                i += 1;
                exe_path = args.get(i).cloned();
            }
            "--out" => {
                i += 1;
                out_path = args.get(i).cloned();
            }
            "--rtti-out" => {
                i += 1;
                rtti_out = args.get(i).cloned();
            }
            "--report" => {
                i += 1;
                report_path = args.get(i).cloned();
            }
            "--verify-1195" => {
                do_verify = true;
            }
            "-h" | "--help" => usage(),
            other => return Err(format!("未知参数: {}", other)),
        }
        i += 1;
    }

    let exe = exe_path.ok_or_else(|| "缺少必填参数 --exe <path\\to\\hoi4.exe>".to_string())?;
    let t_total = Instant::now();

    let t0 = Instant::now();
    let img = pe::PeImage::load(&exe)?;
    let ms_pe = t0.elapsed().as_millis();

    let t1 = Instant::now();
    let rtti_map = rtti::scan_rtti(&img);
    let ms_rtti = t1.elapsed().as_millis();

    let t2 = Instant::now();
    let cmd_cat = commands::scan_commands(&img, &rtti_map);
    let ms_cmd = t2.elapsed().as_millis();

    let t3 = Instant::now();
    let anchor_map = anchors::resolve_anchors(&img, &rtti_map, &cmd_cat);
    let ms_anc = t3.elapsed().as_millis();

    let total_ms = t_total.elapsed().as_millis();

    let profile = Profile {
        schema_version: 1,
        tool: format!("hoi4_recalc/{}", env!("CARGO_PKG_VERSION")),
        pe: img.meta.clone(),
        rtti_stats: rtti_map.stats.clone(),
        anchors: anchor_map,
        commands_count: cmd_cat.commands.len(),
        commands: cmd_cat.commands,
    };

    println!(
        "[hoi4_recalc] PE={} build={} size={:#x} pdata={}",
        profile.pe.path,
        profile.pe.build_id.as_deref().unwrap_or("<unknown>"),
        profile.pe.size_of_image,
        profile.pe.pdata_functions
    );
    println!(
        "[hoi4_recalc] RTTI: classes={} vtables={} | Commands: {} | Anchors: {}",
        profile.rtti_stats.classes,
        profile.rtti_stats.vtables,
        profile.commands_count,
        profile.anchors.len()
    );
    println!(
        "[hoi4_recalc] Timings: pe={}ms rtti={}ms cmds={}ms anchors={}ms total={}ms",
        ms_pe, ms_rtti, ms_cmd, ms_anc, total_ms
    );

    if let Some(op) = &out_path {
        let json = serde_json::to_string_pretty(&profile)
            .map_err(|e| format!("序列化 profile JSON 失败: {}", e))?;
        write_text(op, &json)?;
        println!("[hoi4_recalc] Wrote profile: {} ({} bytes)", op, json.len());
    }

    if let Some(rp) = &rtti_out {
        let exp = RttiJsonExport {
            pe: &profile.pe,
            stats: &profile.rtti_stats,
            vtables: &rtti_map.classes,
            decorated: &rtti_map.decorated,
        };
        let json = serde_json::to_string(&exp)
            .map_err(|e| format!("序列化 RTTI JSON 失败: {}", e))?;
        write_text(rp, &json)?;
        println!("[hoi4_recalc] Wrote RTTI: {} ({} bytes)", rp, json.len());
    }

    let verify_res = if do_verify {
        let v = verify_1195(&profile);
        println!(
            "[hoi4_recalc] Verify 1.19.5.0: {}/{} EXACT (drifted={}, missing={})",
            v.exact, v.total, v.drifted, v.missing
        );
        for it in &v.items {
            if it.status != "EXACT" {
                println!(
                    "  [{}] {}: {} expected={} actual={} ({})",
                    it.status, it.category, it.name, it.expected, it.actual, it.note
                );
            }
        }
        Some(v)
    } else {
        None
    };

    if let Some(rep) = &report_path {
        let md = render_report(&profile, total_ms, verify_res.as_ref());
        write_text(rep, &md)?;
        println!("[hoi4_recalc] Wrote report: {}", rep);
    }

    if let Some(v) = verify_res {
        if v.drifted > 0 || v.missing > 0 {
            return Ok(1);
        }
    }
    Ok(0)
}

fn cmd_diff(args: &[String]) -> Result<i32, String> {
    let mut old_p: Option<String> = None;
    let mut new_p: Option<String> = None;
    let mut out_p: Option<String> = None;

    let mut i = 0;
    while i < args.len() {
        match args[i].as_str() {
            "--old" => {
                i += 1;
                old_p = args.get(i).cloned();
            }
            "--new" => {
                i += 1;
                new_p = args.get(i).cloned();
            }
            "--out" => {
                i += 1;
                out_p = args.get(i).cloned();
            }
            "-h" | "--help" => usage(),
            other => return Err(format!("未知参数: {}", other)),
        }
        i += 1;
    }

    let old_path = old_p.ok_or_else(|| "缺少 --old <a.json>".to_string())?;
    let new_path = new_p.ok_or_else(|| "缺少 --new <b.json>".to_string())?;
    let old_txt = fs::read_to_string(&old_path).map_err(|e| format!("读取 {} 失败: {}", old_path, e))?;
    let new_txt = fs::read_to_string(&new_path).map_err(|e| format!("读取 {} 失败: {}", new_path, e))?;
    let old_prof: Profile =
        serde_json::from_str(&old_txt).map_err(|e| format!("解析 {} 失败: {}", old_path, e))?;
    let new_prof: Profile =
        serde_json::from_str(&new_txt).map_err(|e| format!("解析 {} 失败: {}", new_path, e))?;

    let md = render_diff(&old_prof, &new_prof);
    if let Some(op) = out_p {
        write_text(&op, &md)?;
        println!("[hoi4_recalc] Wrote diff report: {}", op);
    } else {
        print!("{}", md);
    }
    Ok(0)
}

fn main() {
    let args: Vec<String> = env::args().skip(1).collect();
    if args.is_empty() {
        usage();
    }
    let res = match args[0].as_str() {
        "scan" => cmd_scan(&args[1..]),
        "diff" => cmd_diff(&args[1..]),
        "assets" => assets::cmd_assets(&args[1..]),
        "-h" | "--help" => usage(),
        other => Err(format!("未知子命令: {}（支持 scan / diff / assets）", other)),
    };
    match res {
        Ok(code) => process::exit(code),
        Err(e) => {
            eprintln!("[hoi4_recalc] ERROR: {}", e);
            process::exit(1);
        }
    }
}
