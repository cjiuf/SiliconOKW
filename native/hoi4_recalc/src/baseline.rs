//! 1.19.5.0 权威基线对账与跨版本 Profile Diff 报告生成。

use crate::anchors::AnchorResult;
use crate::commands::CommandSpec;
use crate::pe::PeMeta;
use crate::rtti::RttiStats;
use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Profile {
    pub schema_version: u32,
    pub tool: String,
    pub pe: PeMeta,
    pub rtti_stats: RttiStats,
    pub anchors: BTreeMap<String, AnchorResult>,
    pub commands_count: usize,
    pub commands: BTreeMap<String, CommandSpec>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CheckItem {
    pub category: String,
    pub name: String,
    pub expected: String,
    pub actual: String,
    pub status: String, // "EXACT" | "DRIFTED" | "MISSING"
    pub note: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct VerifySummary {
    pub exact: usize,
    pub drifted: usize,
    pub missing: usize,
    pub total: usize,
    pub items: Vec<CheckItem>,
}

fn fmt_vals(vals: &[u64]) -> String {
    if vals.len() == 1 {
        format!("{:#x}", vals[0])
    } else {
        let s: Vec<String> = vals.iter().map(|v| format!("{:#x}", v)).collect();
        format!("[{}]", s.join(", "))
    }
}

fn actual_vals(a: &AnchorResult) -> Vec<u64> {
    if let Some(vs) = &a.values {
        vs.clone()
    } else if let Some(v) = a.value {
        vec![v]
    } else {
        Vec::new()
    }
}

/// 将 `Profile` 与已知 1.19.5.0 (Operation Postern v1.19.5.0.7c6c) 权威基线逐项对账。
pub fn verify_1195(profile: &Profile) -> VerifySummary {
    let mut items = Vec::new();

    // 1. 核心锚点权威基线（来源：offsets_win.py / cmdwin.py / peacewin.py）
    let anchor_expected: Vec<(&str, Vec<u64>)> = vec![
        ("ALLOC_VTABLE", vec![0x2794550]),
        ("G_DEFAULT_ALLOC", vec![0x30FD000]),
        ("OP_NEW", vec![0x25A01DC]),
        ("OP_DELETE_SIZED", vec![0x25A0218]),
        ("FN_NOOP_DTOR", vec![0x11D530]),
        ("G_GAMESTATE", vec![0x33DD6B0]),
        ("FN_SET_GAME_SPEED", vec![0x203F10]),
        ("GS_GAME_SPEED", vec![0x4BC]),
        ("G_IDLER", vec![0x33DDB08]),
        ("G_GAME_SPEED_SECONDS", vec![0x33E6080]),
        ("IDLER_PAUSED", vec![0x6C1]),
        ("IDLER_PAUSE_WRITE_SITE", vec![0xE48C36]),
        ("FN_POST", vec![0x22C4250]),
        ("SESSION_BUSY_FLAG", vec![0x725]),
        ("SESSION_CMD_QUEUE", vec![0x40]),
        ("CMD_QUEUE_VT_PUSH", vec![0x28]),
        ("FN_GET_COUNTRY", vec![0xC07690]),
        ("FN_RESOLVE_HANDLE", vec![0x2284870]),
        ("GS_COUNTRIES", vec![0x310]),
        ("CO_TECHNOLOGY_STATUS", vec![0xF60]),
        ("CO_PRODUCTION_STATUS", vec![0xF68]),
        ("CO_DEPLOYMENT_STATUS", vec![0xF70]),
        ("CO_POLITICAL_STATUS", vec![0xF90]),
        ("CO_FOCUS_TREE", vec![0x1370]),
        ("VT_COUNTRY", vec![0x2864730]),
        ("VT_GAMESTATE", vec![0x279F0F0, 0x279F148]),
        ("VT_CURRENT_GAMESTATE", vec![0x279F168, 0x279F1C0]),
        ("VT_STATE", vec![0x29B4D58, 0x29B4DA8]),
        ("VT_WARGOAL", vec![0x2A59250]),
        ("VT_MIL_LINE", vec![0x29F8520, 0x29F8630]),
        ("VT_NAV_LINE", vec![0x29F86A0, 0x29F87B0]),
        ("VT_BUILDING_LINE", vec![0x29F8268]),
        ("VT_BUILDING_REF", vec![0x29EE730]),
        ("VT_PEACE_CONFERENCE", vec![0x29CDA28]),
        ("VT_PEACE_TURN", vec![0x29EAEC0]),
        ("VT_AIRWING", vec![0x29F7748, 0x29F7780]),
        ("VT_AIRWINGPOOL", vec![0x29F77D8]),
        ("VT_FLEET", vec![0x29DF3D8, 0x29DF430]),
        ("VT_TASKFORCE", vec![0x29DF900, 0x29DFAE0, 0x29DFB50, 0x29DFB98]),
        ("VT_EVENT", vec![0x2A16008, 0x2A16060]),
        ("VT_EVENT_WINDOW", vec![0x2A20830, 0x2A20858, 0x2A20890, 0x2A208A8]),
        ("VT_EVENT_SCOPE", vec![0x2852168]),
        ("VT_DECLARE_WAR", vec![0x2A07548]),
        ("VT_DIPLO_CMD", vec![0x2A073C8]),
        ("VT_GENERATE_WARGOAL", vec![0x2A079B8]),
        ("FN_DIPLO_VALIDATE", vec![0x11C2420]),
        ("PEACE_DONE_VT", vec![0x2AFC800]),
        ("PEACE_ENDTURN_VT", vec![0x2AFC8C0]),
        ("PEACE_PASS_VT", vec![0x2AFC740]),
        ("PEACE_CANEXEC_DONE", vec![0x1ED23A0]),
        ("PEACE_CANEXEC_PASS", vec![0x1ED2A00]),
        ("PEACE_EXEC_DONE", vec![0x1ED22E0]),
        ("PEACE_EXEC_ENDTURN", vec![0x1ED2300]),
        ("PEACE_EXEC_PASS", vec![0x1ED2350]),
        ("PEACE_GET_CONF", vec![0x1ED1EE0]),
        ("PEACE_FN_INSTALL_TURN", vec![0xEB5390]),
    ];

    for (name, exp) in anchor_expected {
        let exp_s = fmt_vals(&exp);
        match profile.anchors.get(name) {
            None => items.push(CheckItem {
                category: "anchor".into(),
                name: name.into(),
                expected: exp_s,
                actual: "-".into(),
                status: "MISSING".into(),
                note: "not emitted".into(),
            }),
            Some(a) => {
                let got = actual_vals(a);
                if got.is_empty() {
                    items.push(CheckItem {
                        category: "anchor".into(),
                        name: name.into(),
                        expected: exp_s,
                        actual: "-".into(),
                        status: "MISSING".into(),
                        note: a.evidence.clone().unwrap_or_default(),
                    });
                } else if got == exp {
                    items.push(CheckItem {
                        category: "anchor".into(),
                        name: name.into(),
                        expected: exp_s,
                        actual: fmt_vals(&got),
                        status: "EXACT".into(),
                        note: a.method.clone(),
                    });
                } else {
                    items.push(CheckItem {
                        category: "anchor".into(),
                        name: name.into(),
                        expected: exp_s,
                        actual: fmt_vals(&got),
                        status: "DRIFTED".into(),
                        note: a.method.clone(),
                    });
                }
            }
        }
    }

    // 2. 经实机反汇编验证过的核心命令 (vt, ctor_opt, size, can_execute_opt)
    let cmd_expected: Vec<(&str, u32, Option<u32>, u32, Option<u32>)> = vec![
        ("CSetGameSpeedCommand", 0x29F3A80, Some(0xF84510), 0x30, Some(0xF85FB0)),
        ("CSetNationalFocusCommand", 0x2A128C8, Some(0x11D0DC0), 0x38, Some(0x11F2940)),
        ("CDropCurrentNationalFocusCommand", 0x2A12988, Some(0x11CE6F0), 0x30, None),
        ("CSetResearchCommand", 0x2A11548, Some(0x11D1980), 0x40, Some(0x11F3360)),
        ("CAddConstructionCommand", 0x2A10B88, Some(0x11CC8C0), 0x50, None),
        // 下面三项是旧 pecmd.py 曾算错/漏算的经典历史坑，检验 Rust 新解析器是否全部纠偏！
        ("CAddAdvisorCommand", 0x2A11A88, Some(0x11CC6D0), 0x50, Some(0x11ECC20)),
        ("CUnlockGrandDoctrineCommand", 0x2AAA8A0, Some(0x1B0FD20), 0x38, Some(0x1B10310)),
        ("CAddProductionLineCommand", 0x2A0FBC8, Some(0x11CD270), 0x40, Some(0x11EDB10)),
        ("CSetProductionLineCommand", 0x2A0FF88, Some(0x11D1550), 0x38, Some(0x11F2C30)),
        ("CRemoveProductionLineCommand", 0x2A10048, Some(0x11D0210), 0x30, Some(0x11F18B0)),
        ("CAttachAirWingToArmyCommand", 0x2A97728, None, 0x48, Some(0x19D3920)),
        ("CStratAirSetMissionCommand", 0x2A96BE8, None, 0x48, Some(0x19D4B30)),
        // 注：真实编译器 sizeof 由 clone(operator new) 与 dtor(sized delete) 双重证实：
        // CUpdateDivisionTemplateCommand = 0x288（旧手写 0x2B0 含 0x28 冗余安全垫）；
        // CNavalMissionSetTypeCommand = 0x48（旧手写 0x50 含 8B 冗余安全垫）。
        ("CUpdateDivisionTemplateCommand", 0x2A26E38, None, 0x288, Some(0x1C2DD90)),
        ("CNavalMissionSetTypeCommand", 0x2A2A808, None, 0x48, Some(0x13EE8C0)),
        ("CNavalMissionAddRegionCommand", 0x2A2A988, None, 0x38, Some(0x13EDF70)),
        ("CNavalMissionRemoveRegionCommand", 0x2A2AA48, None, 0x48, Some(0x13EE560)),
        ("CDonePeaceConferenceCommand", 0x2AFC800, Some(0x1ED1D70), 0x30, Some(0x1ED23A0)),
        ("CPassPeaceConferenceCommand", 0x2AFC740, Some(0x1ED1E50), 0x30, Some(0x1ED2A00)),
        ("CSelectEventOptionCommand", 0x29B6CE0, None, 0xF0, Some(0x15D66C0)),
    ];

    for (cls, exp_vt, exp_ctor, exp_sz, exp_ce) in cmd_expected {
        let exp_desc = format!(
            "vt={:#x} size={:#x}{}{}",
            exp_vt,
            exp_sz,
            exp_ctor
                .map(|c| format!(" ctor={:#x}", c))
                .unwrap_or_default(),
            exp_ce
                .map(|c| format!(" ce={:#x}", c))
                .unwrap_or_default()
        );
        match profile.commands.get(cls) {
            None => items.push(CheckItem {
                category: "command".into(),
                name: cls.into(),
                expected: exp_desc,
                actual: "-".into(),
                status: "MISSING".into(),
                note: "command missing".into(),
            }),
            Some(c) => {
                let got_desc = format!(
                    "vt={:#x} size={:#x}({}) ctor={} ce={}",
                    c.vt,
                    c.size,
                    c.size_source,
                    c.ctor.map(|x| format!("{:#x}", x)).unwrap_or("-".into()),
                    c.can_execute
                        .map(|x| format!("{:#x}", x))
                        .unwrap_or("-".into())
                );
                let ok_vt = c.vt == exp_vt;
                let ok_sz = c.size == exp_sz;
                let ok_ctor = exp_ctor.map(|ec| c.ctor == Some(ec)).unwrap_or(true);
                let ok_ce = exp_ce.map(|ec| c.can_execute == Some(ec)).unwrap_or(true);
                let status = if ok_vt && ok_sz && ok_ctor && ok_ce {
                    "EXACT"
                } else {
                    "DRIFTED"
                };
                let mut notes = Vec::new();
                if !ok_vt {
                    notes.push("vt mismatch");
                }
                if !ok_sz {
                    notes.push("size mismatch");
                }
                if !ok_ctor {
                    notes.push("ctor mismatch");
                }
                if !ok_ce {
                    notes.push("can_execute mismatch");
                }
                let note = if notes.is_empty() {
                    format!("args={} fields={}", c.args, c.fields.len())
                } else {
                    notes.join(", ")
                };
                items.push(CheckItem {
                    category: "command".into(),
                    name: cls.into(),
                    expected: exp_desc,
                    actual: got_desc,
                    status: status.into(),
                    note,
                });
            }
        }
    }

    let exact = items.iter().filter(|i| i.status == "EXACT").count();
    let drifted = items.iter().filter(|i| i.status == "DRIFTED").count();
    let missing = items.iter().filter(|i| i.status == "MISSING").count();
    VerifySummary {
        exact,
        drifted,
        missing,
        total: items.len(),
        items,
    }
}

/// 生成面向人类的 Markdown 重算与对账审计报告。
pub fn render_report(profile: &Profile, elapsed_ms: u128, verify: Option<&VerifySummary>) -> String {
    let mut s = String::new();
    s.push_str("# HOI4 PE 离线重算报告 (`hoi4_recalc`)\n\n");
    s.push_str(&format!("- **PE 文件**: `{}`\n", profile.pe.path));
    s.push_str(&format!(
        "- **Build ID**: `{}`\n",
        profile.pe.build_id.as_deref().unwrap_or("<unknown>")
    ));
    s.push_str(&format!(
        "- **ImageBase / SizeOfImage / Timestamp**: `{:#x}` / `{:#x}` / `{:#x}`\n",
        profile.pe.image_base, profile.pe.size_of_image, profile.pe.timestamp
    ));
    s.push_str(&format!("- **SHA256**: `{}`\n", profile.pe.sha256));
    s.push_str(&format!(
        "- **扫描统计**: `.pdata` 函数 **{}**，RTTI 类 **{}**（vtable **{}**），命令目录 **{}** 个，耗时 **{} ms**\n\n",
        profile.pe.pdata_functions,
        profile.rtti_stats.classes,
        profile.rtti_stats.vtables,
        profile.commands_count,
        elapsed_ms
    ));

    if let Some(v) = verify {
        s.push_str(&format!(
            "## 1. 1.19.5.0 基线对账结果：**{} / {} EXACT**（DRIFTED={}, MISSING={}）\n\n",
            v.exact, v.total, v.drifted, v.missing
        ));
        s.push_str("| 类别 | 符号 | 状态 | 基线预期 | 静态重算结果 | 说明 |\n");
        s.push_str("|---|---|---|---|---|---|\n");
        for it in &v.items {
            s.push_str(&format!(
                "| {} | `{}` | **{}** | `{}` | `{}` | {} |\n",
                it.category, it.name, it.status, it.expected, it.actual, it.note
            ));
        }
        s.push('\n');
    }

    s.push_str("## 2. 静态推导锚点全表\n\n");
    s.push_str("| 锚点 | 状态 | 值 | 推导策略 |\n");
    s.push_str("|---|---|---|---|\n");
    for (k, a) in &profile.anchors {
        let vals = actual_vals(a);
        let vs = if vals.is_empty() {
            "-".to_string()
        } else {
            fmt_vals(&vals)
        };
        s.push_str(&format!("| `{}` | {} | `{}` | {} |\n", k, a.status, vs, a.method));
    }
    s
}

/// 对比两个版本的 `Profile`，生成跨版本迁移 Diff 报告。
pub fn render_diff(old: &Profile, new: &Profile) -> String {
    let mut s = String::new();
    s.push_str("# HOI4 PE 跨版本重算 Diff 报告\n\n");
    s.push_str(&format!(
        "- **旧版**: `{}` (`SizeOfImage={:#x}`, `sha256={}...`)\n",
        old.pe.build_id.as_deref().unwrap_or("<unknown>"),
        old.pe.size_of_image,
        &old.pe.sha256[..12.min(old.pe.sha256.len())]
    ));
    s.push_str(&format!(
        "- **新版**: `{}` (`SizeOfImage={:#x}`, `sha256={}...`)\n\n",
        new.pe.build_id.as_deref().unwrap_or("<unknown>"),
        new.pe.size_of_image,
        &new.pe.sha256[..12.min(new.pe.sha256.len())]
    ));

    s.push_str("## 1. 锚点变化\n\n");
    s.push_str("| 锚点 | 状态 | 旧值 | 新值 |\n|---|---|---|---|\n");
    let mut all_keys: Vec<String> = old.anchors.keys().chain(new.anchors.keys()).cloned().collect();
    all_keys.sort();
    all_keys.dedup();
    let mut changed_anchors = 0usize;
    for k in all_keys {
        let ov = old.anchors.get(&k).map(actual_vals).unwrap_or_default();
        let nv = new.anchors.get(&k).map(actual_vals).unwrap_or_default();
        if ov != nv {
            changed_anchors += 1;
            let st = if nv.is_empty() {
                "MISSING"
            } else if ov.is_empty() {
                "ADDED"
            } else {
                "DRIFTED"
            };
            s.push_str(&format!(
                "| `{}` | **{}** | `{}` | `{}` |\n",
                k,
                st,
                if ov.is_empty() { "-".into() } else { fmt_vals(&ov) },
                if nv.is_empty() { "-".into() } else { fmt_vals(&nv) }
            ));
        }
    }
    if changed_anchors == 0 {
        s.push_str("| *(无变化)* | SAME | - | - |\n");
    }

    s.push_str("\n## 2. 命令布局/大小/字段变化（高危检查）\n\n");
    s.push_str("| 命令类 | 变化类型 | 旧 (size/args/fields) | 新 (size/args/fields) |\n|---|---|---|---|\n");
    let mut all_cmds: Vec<String> = old.commands.keys().chain(new.commands.keys()).cloned().collect();
    all_cmds.sort();
    all_cmds.dedup();
    let mut changed_cmds = 0usize;
    for k in all_cmds {
        match (old.commands.get(&k), new.commands.get(&k)) {
            (Some(o), Some(n)) => {
                let o_sig: Vec<(u32, u8, Option<u8>)> =
                    o.fields.iter().map(|f| (f.offset, f.size, f.arg)).collect();
                let n_sig: Vec<(u32, u8, Option<u8>)> =
                    n.fields.iter().map(|f| (f.offset, f.size, f.arg)).collect();
                if o.size != n.size || o_sig != n_sig {
                    changed_cmds += 1;
                    s.push_str(&format!(
                        "| `{}` | **ABI_CHANGED** | `{:#x}` / {} / {} | `{:#x}` / {} / {} |\n",
                        k, o.size, o.args, o.fields.len(), n.size, n.args, n.fields.len()
                    ));
                }
            }
            (Some(o), None) => {
                changed_cmds += 1;
                s.push_str(&format!(
                    "| `{}` | **REMOVED** | `{:#x}` / {} / {} | - |\n",
                    k, o.size, o.args, o.fields.len()
                ));
            }
            (None, Some(n)) => {
                changed_cmds += 1;
                s.push_str(&format!(
                    "| `{}` | **ADDED** | - | `{:#x}` / {} / {} |\n",
                    k, n.size, n.args, n.fields.len()
                ));
            }
            (None, None) => {}
        }
    }
    if changed_cmds == 0 {
        s.push_str("| *(所有命令 size 与字段布局完全一致)* | SAME | - | - |\n");
    }
    s
}
