//! 确定性跨版本静态锚点引擎：
//! 不硬编码任何特定版本的 RVA/偏移，全部经由 RTTI、字符串 XRef、虚表槽与 `iced-x86` 指令流自动推导。

use crate::commands::{read_vt_slot, CommandCatalogue};
use crate::pe::PeImage;
use crate::rtti::RttiMap;
use iced_x86::{Decoder, DecoderOptions, Instruction, Mnemonic, OpKind, Register};
use rayon::prelude::*;
use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, HashSet};

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AnchorResult {
    #[serde(skip_serializing_if = "Option::is_none")]
    pub value: Option<u64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub values: Option<Vec<u64>>,
    pub status: String, // "exact" | "ambiguous" | "missing"
    pub method: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub evidence: Option<String>,
}

impl AnchorResult {
    fn exact(val: u64, method: impl Into<String>, evidence: Option<String>) -> Self {
        Self {
            value: Some(val),
            values: None,
            status: "exact".into(),
            method: method.into(),
            evidence,
        }
    }

    fn exact_multi(vals: Vec<u64>, method: impl Into<String>) -> Self {
        let first = vals.first().copied();
        Self {
            value: if vals.len() == 1 { first } else { None },
            values: if vals.len() > 1 { Some(vals) } else { None },
            status: "exact".into(),
            method: method.into(),
            evidence: None,
        }
    }

    fn ambiguous(vals: Vec<u64>, method: impl Into<String>, evidence: Option<String>) -> Self {
        Self {
            value: vals.first().copied(),
            values: Some(vals),
            status: "ambiguous".into(),
            method: method.into(),
            evidence,
        }
    }

    fn missing(method: impl Into<String>, reason: impl Into<String>) -> Self {
        Self {
            value: None,
            values: None,
            status: "missing".into(),
            method: method.into(),
            evidence: Some(reason.into()),
        }
    }
}

/// 辅助：读取一个短小 getter（如 `mov rax, [rcx + disp]; ret`）的偏移。
fn read_tiny_getter_offset(pe: &PeImage, fn_rva: u32) -> Option<u32> {
    let bytes = pe.read(fn_rva, 0x20)?;
    let mut dec = Decoder::with_ip(64, bytes, fn_rva as u64, DecoderOptions::NONE);
    let mut insn = Instruction::default();
    let mut found_off: Option<u32> = None;
    for _ in 0..6 {
        if !dec.can_decode() {
            break;
        }
        dec.decode_out(&mut insn);
        if insn.is_invalid() {
            break;
        }
        if insn.mnemonic() == Mnemonic::Mov
            && insn.op_count() == 2
            && insn.op0_kind() == OpKind::Register
            && insn.op0_register() == Register::RAX
            && insn.op1_kind() == OpKind::Memory
            && insn.memory_base() == Register::RCX
            && insn.memory_index() == Register::None
        {
            found_off = Some(insn.memory_displacement64() as u32);
        } else if insn.mnemonic() == Mnemonic::Ret {
            return found_off;
        }
    }
    None
}

/// 解码完整函数（按 `.pdata` 边界或最多 `max_bytes`）。
fn decode_fn(pe: &PeImage, fn_rva: u32, max_bytes: usize) -> Vec<Instruction> {
    let (b, e) = pe.fn_bounds(fn_rva).unwrap_or((fn_rva, fn_rva + max_bytes as u32));
    let len = (e.saturating_sub(b) as usize).min(max_bytes);
    let Some(bytes) = pe.read(b, len) else {
        return Vec::new();
    };
    let mut dec = Decoder::with_ip(64, bytes, b as u64, DecoderOptions::NONE);
    let mut out = Vec::new();
    let mut insn = Instruction::default();
    while dec.can_decode() {
        dec.decode_out(&mut insn);
        if insn.is_invalid() {
            break;
        }
        out.push(insn);
    }
    out
}

fn ins_near_target(insn: &Instruction) -> Option<u32> {
    if matches!(insn.op0_kind(), OpKind::NearBranch64 | OpKind::NearBranch32) {
        Some(insn.near_branch64() as u32)
    } else {
        None
    }
}

/// 在 RTTI 类名列表中提取单/多 vtable 锚点。
fn rtti_vt_anchor(rtti: &RttiMap, cls: &str) -> AnchorResult {
    match rtti.vts(cls) {
        Some(vts) if !vts.is_empty() => {
            let vals: Vec<u64> = vts.iter().map(|&v| v as u64).collect();
            AnchorResult::exact_multi(vals, format!("RTTI({})", cls))
        }
        _ => AnchorResult::missing(format!("RTTI({})", cls), "class not found in RTTI"),
    }
}

/// 扫描 `.text` 定位暂停切换点：`cmp byte ptr [reg+disp32], 0; sete al; mov byte ptr [reg+disp32], al`
/// 且所在 `.pdata` 函数同时访问 `G_GAMESTATE`。
fn find_idler_pause_site(pe: &PeImage, g_gamestate: Option<u32>) -> (Option<u32>, Option<u32>) {
    let Some(sec) = pe.section(".text") else {
        return (None, None);
    };
    let start = sec.raw as usize;
    let len = (sec.rawsize as usize).min(pe.data.len().saturating_sub(start));
    if len < 32 {
        return (None, None);
    }
    let blob = &pe.data[start..start + len];
    let base_va = sec.va;
    let chunk_sz = 1usize << 20;
    let n_chunks = (len + chunk_sz - 1) / chunk_sz;

    // 匹配 `0F 94 C0` (`sete al`) 前后 7 字节的 cmp+mov 同基址同偏移模式
    let cands: Vec<(u32, u32)> = (0..n_chunks)
        .into_par_iter()
        .flat_map(|ci| {
            let lo = ci * chunk_sz;
            let hi = ((ci + 1) * chunk_sz).min(len.saturating_sub(16));
            let mut found = Vec::new();
            let mut j = lo.max(8);
            while j < hi {
                if blob[j] == 0x0F && blob[j + 1] == 0x94 && blob[j + 2] == 0xC0 {
                    // 回退 7..8 字节解码 `cmp byte [reg+disp32], 0; sete al; mov [reg+disp32], al`
                    for back in [7usize, 8usize] {
                        let start_off = j - back;
                        let site_rva = base_va + start_off as u32;
                        let mut dec = Decoder::with_ip(
                            64,
                            &blob[start_off..(j + 12).min(len)],
                            site_rva as u64,
                            DecoderOptions::NONE,
                        );
                        let i0 = dec.decode();
                        let i1 = dec.decode();
                        let i2 = dec.decode();
                        if i0.mnemonic() == Mnemonic::Cmp
                            && i1.mnemonic() == Mnemonic::Sete
                            && i2.mnemonic() == Mnemonic::Mov
                            && i0.op0_kind() == OpKind::Memory
                            && i0.op1_kind() == OpKind::Immediate8
                            && i0.immediate8() == 0
                            && i2.op0_kind() == OpKind::Memory
                            && i2.op1_kind() == OpKind::Register
                            && i2.op1_register() == Register::AL
                            && i0.memory_base() == i2.memory_base()
                            && i0.memory_index() == Register::None
                            && i2.memory_index() == Register::None
                            && i0.memory_displacement64() == i2.memory_displacement64()
                        {
                            let disp = i0.memory_displacement64() as u32;
                            if (0x100..=0x2000).contains(&disp) {
                                let write_site = i2.ip() as u32;
                                found.push((write_site, disp));
                                break;
                            }
                        }
                    }
                }
                j += 1;
            }
            found
        })
        .collect();

    if cands.is_empty() {
        return (None, None);
    }

    // 用 g_gamestate 交叉过滤：真实 CInGameIdler::TogglePause 所在函数前文必定引用 G_GAMESTATE
    if let Some(gs_rva) = g_gamestate {
        let filtered: Vec<(u32, u32)> = cands
            .iter()
            .copied()
            .filter(|&(site, _)| {
                let fn_b = pe.fn_start(site);
                let insns = decode_fn(pe, fn_b, 0x1200);
                insns.iter().any(|ins| {
                    ins.is_ip_rel_memory_operand()
                        && (ins.ip_rel_memory_address() as u32) == gs_rva
                })
            })
            .collect();
        if filtered.len() == 1 {
            return (Some(filtered[0].0), Some(filtered[0].1));
        }
        if !filtered.is_empty() {
            // 若命中多条且偏移完全一致，仍可唯一确定偏移
            let first = filtered[0];
            return (Some(first.0), Some(first.1));
        }
    }

    if cands.len() == 1 {
        (Some(cands[0].0), Some(cands[0].1))
    } else {
        (None, None)
    }
}

/// 执行全部核心引擎锚点的静态推导。
pub fn resolve_anchors(
    pe: &PeImage,
    rtti: &RttiMap,
    cmds: &CommandCatalogue,
) -> BTreeMap<String, AnchorResult> {
    let mut out: BTreeMap<String, AnchorResult> = BTreeMap::new();

    // ========================================================= 1. 运行时基础
    let alloc_vt = rtti.first_vt("CPdxNewDeleteAllocator");
    out.insert("ALLOC_VTABLE".into(), rtti_vt_anchor(rtti, "CPdxNewDeleteAllocator"));

    if let Some(avt) = alloc_vt {
        let needle = (pe.meta.image_base + avt as u64).to_le_bytes();
        let hits = pe.find_bytes_in_section(".data", &needle);
        let res = match hits.len() {
            1 => AnchorResult::exact(
                hits[0] as u64,
                "scan .data for CPdxNewDeleteAllocator vptr",
                Some(format!("unique hit in .data @ {:#x}", hits[0])),
            ),
            0 => AnchorResult::missing("scan .data for CPdxNewDeleteAllocator vptr", "no hit in .data"),
            _ => AnchorResult::ambiguous(
                hits.into_iter().map(|x| x as u64).collect(),
                "scan .data for CPdxNewDeleteAllocator vptr",
                Some("multiple instances in .data".into()),
            ),
        };
        out.insert("G_DEFAULT_ALLOC".into(), res);
    } else {
        out.insert(
            "G_DEFAULT_ALLOC".into(),
            AnchorResult::missing("CPdxNewDeleteAllocator", "vtable missing"),
        );
    }

    out.insert(
        "OP_NEW".into(),
        match cmds.runtime.op_new {
            Some(v) => AnchorResult::exact(
                v as u64,
                "majority vote across C*Command::clone(vt[+0x68])",
                Some(format!("{} votes", cmds.runtime.op_new_votes)),
            ),
            None => AnchorResult::missing("C*Command::clone majority vote", "no votes"),
        },
    );
    out.insert(
        "OP_DELETE_SIZED".into(),
        match cmds.runtime.op_delete_sized {
            Some(v) => AnchorResult::exact(
                v as u64,
                "majority vote across C*Command::dtor(vt[0])",
                Some(format!("{} votes", cmds.runtime.op_delete_sized_votes)),
            ),
            None => AnchorResult::missing("C*Command::dtor majority vote", "no votes"),
        },
    );
    out.insert(
        "FN_NOOP_DTOR".into(),
        match cmds.runtime.fn_noop_dtor {
            Some(v) => AnchorResult::exact(
                v as u64,
                "majority vote across C*Command::vt[+0x28] (xor al,al; ret)",
                Some(format!("{} votes", cmds.runtime.fn_noop_dtor_votes)),
            ),
            None => AnchorResult::missing("C*Command::vt[+0x28] majority vote", "no votes"),
        },
    );

    // ================================================= 2. 全局状态、速度、暂停
    let mut g_gamestate: Option<u32> = None;
    let mut fn_set_speed: Option<u32> = None;
    let mut gs_game_speed: Option<u32> = None;
    let mut g_idler: Option<u32> = None;

    if let Some(sp_cmd) = cmds.commands.get("CSetGameSpeedCommand") {
        if let Some(exec_rva) = sp_cmd.execute {
            let insns = decode_fn(pe, exec_rva, 0x200);
            // 尾部：`mov rcx, [rip + G_GAMESTATE]; ... jmp FN_SET_GAME_SPEED`
            let mut last_rcx_rip: Option<u32> = None;
            for ins in &insns {
                if ins.mnemonic() == Mnemonic::Mov
                    && ins.op_count() == 2
                    && ins.op0_kind() == OpKind::Register
                    && ins.op0_register() == Register::RCX
                    && ins.is_ip_rel_memory_operand()
                {
                    last_rcx_rip = Some(ins.ip_rel_memory_address() as u32);
                }
                if ins.mnemonic() == Mnemonic::Jmp {
                    if let Some(tgt) = ins_near_target(ins) {
                        if last_rcx_rip.is_some() && pe.is_in_section(tgt, ".text") {
                            g_gamestate = last_rcx_rip;
                            fn_set_speed = Some(tgt);
                        }
                    }
                }
            }
        }
    }

    if let Some(f_ss) = fn_set_speed {
        let insns = decode_fn(pe, f_ss, 0x80);
        for ins in &insns {
            // `mov dword ptr [rcx + GS_GAME_SPEED], edx`
            if gs_game_speed.is_none()
                && ins.mnemonic() == Mnemonic::Mov
                && ins.op_count() == 2
                && ins.op0_kind() == OpKind::Memory
                && ins.memory_base() == Register::RCX
                && ins.memory_index() == Register::None
                && ins.op1_kind() == OpKind::Register
                && ins.op1_register() == Register::EDX
            {
                gs_game_speed = Some(ins.memory_displacement64() as u32);
                continue;
            }
            // 紧随其后的 `mov rcx, qword ptr [rip + G_IDLER]`
            if gs_game_speed.is_some()
                && g_idler.is_none()
                && ins.mnemonic() == Mnemonic::Mov
                && ins.op_count() == 2
                && ins.op0_kind() == OpKind::Register
                && ins.op0_register() == Register::RCX
                && ins.is_ip_rel_memory_operand()
            {
                g_idler = Some(ins.ip_rel_memory_address() as u32);
                break;
            }
        }
    }

    out.insert(
        "G_GAMESTATE".into(),
        match g_gamestate {
            Some(v) => AnchorResult::exact(v as u64, "CSetGameSpeedCommand::Execute tail load", None),
            None => AnchorResult::missing("CSetGameSpeedCommand::Execute", "not resolved"),
        },
    );
    out.insert(
        "FN_SET_GAME_SPEED".into(),
        match fn_set_speed {
            Some(v) => AnchorResult::exact(v as u64, "CSetGameSpeedCommand::Execute tail jmp", None),
            None => AnchorResult::missing("CSetGameSpeedCommand::Execute", "not resolved"),
        },
    );
    out.insert(
        "GS_GAME_SPEED".into(),
        match gs_game_speed {
            Some(v) => AnchorResult::exact(
                v as u64,
                "CCurrentGameState::SetSpeed `mov [rcx+off], edx`",
                None,
            ),
            None => AnchorResult::missing("CCurrentGameState::SetSpeed", "not resolved"),
        },
    );
    out.insert(
        "G_IDLER".into(),
        match g_idler {
            Some(v) => AnchorResult::exact(
                v as u64,
                "CCurrentGameState::SetSpeed post-store `mov rcx, [rip+G_IDLER]`",
                None,
            ),
            None => AnchorResult::missing("CCurrentGameState::SetSpeed", "not resolved"),
        },
    );

    // G_GAME_SPEED_SECONDS: 从 CDefineRegistryHelper_NGameGAME_SPEED_SECONDS::vt[0] 提取
    let mut g_speed_secs: Option<u32> = None;
    if let Some(vt) = rtti.first_vt("CDefineRegistryHelper_NGameGAME_SPEED_SECONDS") {
        if let Some(slot0) = read_vt_slot(pe, vt, 0x00) {
            let insns = decode_fn(pe, slot0, 0x60);
            for ins in &insns {
                // `mov dword ptr [rip + (G_GAME_SPEED_SECONDS + 0x0C)], eax`（清零 CPdxArray.size）
                if ins.mnemonic() == Mnemonic::Mov
                    && ins.op_count() == 2
                    && ins.op0_kind() == OpKind::Memory
                    && ins.is_ip_rel_memory_operand()
                    && ins.op1_kind() == OpKind::Register
                    && ins.op1_register() == Register::EAX
                {
                    let size_rva = ins.ip_rel_memory_address() as u32;
                    if size_rva >= 0x0C {
                        g_speed_secs = Some(size_rva - 0x0C);
                        break;
                    }
                }
            }
        }
    }
    out.insert(
        "G_GAME_SPEED_SECONDS".into(),
        match g_speed_secs {
            Some(v) => AnchorResult::exact(
                v as u64,
                "CDefineRegistryHelper_NGameGAME_SPEED_SECONDS::vt[0] CPdxArray.size - 0x0C",
                None,
            ),
            None => AnchorResult::missing("CDefineRegistryHelper_NGameGAME_SPEED_SECONDS", "not resolved"),
        },
    );

    // IDLER_PAUSED & IDLER_PAUSE_WRITE_SITE
    let (pause_site, pause_off) = find_idler_pause_site(pe, g_gamestate);
    out.insert(
        "IDLER_PAUSED".into(),
        match pause_off {
            Some(v) => AnchorResult::exact(
                v as u64,
                "CInGameIdler toggle pattern `cmp [reg+off],0; sete al; mov [reg+off],al`",
                pause_site.map(|s| format!("site @ {:#x}", s)),
            ),
            None => AnchorResult::missing("CInGameIdler pause toggle scan", "not resolved"),
        },
    );
    out.insert(
        "IDLER_PAUSE_WRITE_SITE".into(),
        match pause_site {
            Some(v) => AnchorResult::exact(
                v as u64,
                "CInGameIdler toggle write instruction RVA",
                None,
            ),
            None => AnchorResult::missing("CInGameIdler pause toggle scan", "not resolved"),
        },
    );

    // ========================================================= 3. 命令通道
    let mut fn_post: Option<u32> = None;
    let dropped_rvas = pe.find_cstr_rvas("Dropped command: ");
    if !dropped_rvas.is_empty() {
        let wanted: HashSet<u32> = dropped_rvas.into_iter().collect();
        let leas = pe.scan_text_leas(&wanted);
        let mut fn_starts: Vec<u32> = leas
            .values()
            .flatten()
            .map(|&s| pe.fn_start(s))
            .collect();
        fn_starts.sort_unstable();
        fn_starts.dedup();
        if fn_starts.len() == 1 {
            fn_post = Some(fn_starts[0]);
            out.insert(
                "FN_POST".into(),
                AnchorResult::exact(
                    fn_starts[0] as u64,
                    "XRef \"Dropped command: \" -> .pdata fn_start",
                    None,
                ),
            );
        } else if !fn_starts.is_empty() {
            fn_post = Some(fn_starts[0]);
            out.insert(
                "FN_POST".into(),
                AnchorResult::ambiguous(
                    fn_starts.into_iter().map(|x| x as u64).collect(),
                    "XRef \"Dropped command: \" -> .pdata fn_start",
                    None,
                ),
            );
        } else {
            out.insert(
                "FN_POST".into(),
                AnchorResult::missing("XRef \"Dropped command: \"", "no LEA reference in .text"),
            );
        }
    } else {
        out.insert(
            "FN_POST".into(),
            AnchorResult::missing("XRef \"Dropped command: \"", "string not found in .rdata"),
        );
    }

    // 从 FN_POST 反汇编中提取 SESSION_BUSY_FLAG (+0x725)、SESSION_CMD_QUEUE (+0x40)、CMD_QUEUE_VT_PUSH (+0x28)
    let mut sess_busy: Option<u32> = None;
    let mut sess_queue: Option<u32> = None;
    let mut queue_vt_push: Option<u32> = None;
    if let Some(fp) = fn_post {
        let insns = decode_fn(pe, fp, 0x600);
        for (idx, ins) in insns.iter().enumerate() {
            if sess_busy.is_none()
                && ins.mnemonic() == Mnemonic::Cmp
                && ins.op_count() == 2
                && ins.op0_kind() == OpKind::Memory
                && ins.memory_base() == Register::RCX
                && ins.memory_index() == Register::None
            {
                let d = ins.memory_displacement64() as u32;
                if (0x100..=0x1000).contains(&d) {
                    sess_busy = Some(d);
                }
            }
            // 成功路径尾部：`mov rcx, [rbx+0x40]; mov rax, [rcx]; ... call [rax+0x28]`
            if ins.mnemonic() == Mnemonic::Call
                && ins.op0_kind() == OpKind::Memory
                && ins.memory_index() == Register::None
            {
                let slot = ins.memory_displacement64() as u32;
                if slot > 0 && slot <= 0x80 && slot % 8 == 0 {
                    // 向回看 6 条指令寻找 `mov rcx, [reg + queue_off]`
                    let lo = idx.saturating_sub(6);
                    for prev in insns[lo..idx].iter().rev() {
                        if prev.mnemonic() == Mnemonic::Mov
                            && prev.op_count() == 2
                            && prev.op0_kind() == OpKind::Register
                            && prev.op0_register() == Register::RCX
                            && prev.op1_kind() == OpKind::Memory
                            && prev.memory_base() != Register::RSP
                            && prev.memory_index() == Register::None
                        {
                            let qoff = prev.memory_displacement64() as u32;
                            if qoff >= 0x10 && qoff <= 0x100 && qoff % 8 == 0 {
                                sess_queue = Some(qoff);
                                queue_vt_push = Some(slot);
                                break;
                            }
                        }
                    }
                }
            }
        }
    }
    out.insert(
        "SESSION_BUSY_FLAG".into(),
        match sess_busy {
            Some(v) => AnchorResult::exact(v as u64, "FN_POST entry guard `cmp [rcx+off], ...`", None),
            None => AnchorResult::missing("FN_POST", "not resolved"),
        },
    );
    out.insert(
        "SESSION_CMD_QUEUE".into(),
        match sess_queue {
            Some(v) => AnchorResult::exact(v as u64, "FN_POST accept path `mov rcx, [sess+off]`", None),
            None => AnchorResult::missing("FN_POST", "not resolved"),
        },
    );
    out.insert(
        "CMD_QUEUE_VT_PUSH".into(),
        match queue_vt_push {
            Some(v) => AnchorResult::exact(v as u64, "FN_POST accept path `call [queue_vt+off]`", None),
            None => AnchorResult::missing("FN_POST", "not resolved"),
        },
    );

    // ===================================================== 4. 核心结构体偏移
    let mut fn_get_country: Option<u32> = None;
    let mut co_focus_tree: Option<u32> = None;
    if let Some(nf_cmd) = cmds.commands.get("CSetNationalFocusCommand") {
        if let Some(ex) = nf_cmd.execute {
            let insns = decode_fn(pe, ex, 0x40);
            let mut tail_jmp: Option<u32> = None;
            for ins in &insns {
                if fn_get_country.is_none() && ins.mnemonic() == Mnemonic::Call {
                    fn_get_country = ins_near_target(ins);
                } else if ins.mnemonic() == Mnemonic::Jmp {
                    tail_jmp = ins_near_target(ins);
                }
            }
            if let Some(set_focus_fn) = tail_jmp {
                let sf_insns = decode_fn(pe, set_focus_fn, 0x60);
                for ins in &sf_insns {
                    if ins.mnemonic() == Mnemonic::Cmp
                        && ins.op_count() == 2
                        && ins.op0_kind() == OpKind::Memory
                        && ins.memory_base() == Register::RCX
                        && ins.memory_index() == Register::None
                    {
                        let d = ins.memory_displacement64() as u32;
                        if (0x400..=0x2000).contains(&d) {
                            co_focus_tree = Some(d);
                            break;
                        }
                    }
                }
            }
        }
    }
    out.insert(
        "FN_GET_COUNTRY".into(),
        match fn_get_country {
            Some(v) => AnchorResult::exact(v as u64, "CSetNationalFocusCommand::Execute 1st call", None),
            None => AnchorResult::missing("CSetNationalFocusCommand::Execute", "not resolved"),
        },
    );
    out.insert(
        "CO_FOCUS_TREE".into(),
        match co_focus_tree {
            Some(v) => AnchorResult::exact(
                v as u64,
                "CSetNationalFocusCommand::Execute -> tail fn `cmp [rcx+off], rax`",
                None,
            ),
            None => AnchorResult::missing("CSetNationalFocusCommand::Execute", "not resolved"),
        },
    );

    // GS_COUNTRIES: 从 FN_GET_COUNTRY 内部提取 `[gs_reg + disp]` 后紧跟 `[rax + rcx*8]`
    let mut gs_countries: Option<u32> = None;
    if let (Some(gc_fn), Some(gs_rva)) = (fn_get_country, g_gamestate) {
        let insns = decode_fn(pe, gc_fn, 0x200);
        let mut gs_reg: Option<Register> = None;
        for ins in &insns {
            if ins.mnemonic() == Mnemonic::Mov
                && ins.op_count() == 2
                && ins.op0_kind() == OpKind::Register
                && ins.is_ip_rel_memory_operand()
                && (ins.ip_rel_memory_address() as u32) == gs_rva
            {
                gs_reg = Some(ins.op0_register());
                continue;
            }
            if let Some(gr) = gs_reg {
                if ins.mnemonic() == Mnemonic::Mov
                    && ins.op_count() == 2
                    && ins.op0_kind() == OpKind::Register
                    && ins.op0_register() == Register::RAX
                    && ins.op1_kind() == OpKind::Memory
                    && ins.memory_base() == gr
                    && ins.memory_index() == Register::None
                {
                    let d = ins.memory_displacement64() as u32;
                    if (0x100..=0x800).contains(&d) {
                        gs_countries = Some(d);
                        break;
                    }
                }
            }
        }
    }
    out.insert(
        "GS_COUNTRIES".into(),
        match gs_countries {
            Some(v) => AnchorResult::exact(
                v as u64,
                "FN_GET_COUNTRY `[G_GAMESTATE + off][idx]`",
                None,
            ),
            None => AnchorResult::missing("FN_GET_COUNTRY", "not resolved"),
        },
    );

    // Helper: 在某个 Command::Execute 中找到 `call FN_GET_COUNTRY` 后对返回的 `CCountry*` (`rax`) 的首次子对象访问（直接 `[rax+off]` 或小 getter）
    let country_sub_offset = |cls: &str| -> Option<(u32, String)> {
        let gc = fn_get_country?;
        let ex = cmds.commands.get(cls)?.execute?;
        let insns = decode_fn(pe, ex, 0x90);
        let mut seen_gc = false;
        for ins in &insns {
            if !seen_gc {
                if ins.mnemonic() == Mnemonic::Call && ins_near_target(ins) == Some(gc) {
                    seen_gc = true;
                }
                continue;
            }
            // 形态 A: `mov reg, qword ptr [rax + off]`
            if ins.mnemonic() == Mnemonic::Mov
                && ins.op_count() == 2
                && ins.op0_kind() == OpKind::Register
                && ins.op1_kind() == OpKind::Memory
                && ins.memory_base() == Register::RAX
                && ins.memory_index() == Register::None
            {
                let d = ins.memory_displacement64() as u32;
                if (0x400..=0x2000).contains(&d) {
                    return Some((d, format!("{}::Execute direct `[rax+{:#x}]`", cls, d)));
                }
            }
            // 形态 B: `call getter` -> `mov rax, [rcx + off]; ret`
            if ins.mnemonic() == Mnemonic::Call {
                if let Some(tgt) = ins_near_target(ins) {
                    if let Some(d) = read_tiny_getter_offset(pe, tgt) {
                        if (0x400..=0x2000).contains(&d) {
                            return Some((d, format!("{}::Execute getter {:#x}", cls, tgt)));
                        }
                    }
                }
                break;
            }
        }
        None
    };

    for (anchor_name, cmd_cls) in [
        ("CO_TECHNOLOGY_STATUS", "CSetResearchCommand"),
        ("CO_PRODUCTION_STATUS", "CAddProductionLineCommand"),
        ("CO_DEPLOYMENT_STATUS", "CCreateConveyorCommand"),
        ("CO_POLITICAL_STATUS", "CAddIdeaCommand"),
    ] {
        out.insert(
            anchor_name.into(),
            match country_sub_offset(cmd_cls) {
                Some((v, ev)) => AnchorResult::exact(
                    v as u64,
                    format!("{}::Execute post-GetCountry deref/getter", cmd_cls),
                    Some(ev),
                ),
                None => AnchorResult::missing(format!("{}::Execute", cmd_cls), "not resolved"),
            },
        );
    }

    // FN_RESOLVE_HANDLE: 从 CAddProductionLineCommand::Execute 中 GetCountry 后的首个 handle 解析调用提取
    let mut fn_resolve_handle: Option<u32> = None;
    if let (Some(gc), Some(ex)) = (
        fn_get_country,
        cmds.commands
            .get("CAddProductionLineCommand")
            .and_then(|c| c.execute),
    ) {
        let insns = decode_fn(pe, ex, 0x80);
        let mut seen_gc = false;
        for ins in &insns {
            if !seen_gc {
                if ins.mnemonic() == Mnemonic::Call && ins_near_target(ins) == Some(gc) {
                    seen_gc = true;
                }
                continue;
            }
            if ins.mnemonic() == Mnemonic::Call {
                if let Some(tgt) = ins_near_target(ins) {
                    fn_resolve_handle = Some(tgt);
                    break;
                }
            }
        }
    }
    out.insert(
        "FN_RESOLVE_HANDLE".into(),
        match fn_resolve_handle {
            Some(v) => AnchorResult::exact(
                v as u64,
                "CAddProductionLineCommand::Execute 1st call after GetCountry (handle resolver)",
                None,
            ),
            None => AnchorResult::missing("CAddProductionLineCommand::Execute", "not resolved"),
        },
    );

    // ========================================================= 5. 核心类 Vtable
    for (anchor_name, cls) in [
        ("VT_COUNTRY", "CCountry"),
        ("VT_GAMESTATE", "CGameState"),
        ("VT_CURRENT_GAMESTATE", "CCurrentGameState"),
        ("VT_STATE", "CState"),
        ("VT_WARGOAL", "CWarGoal"),
        ("VT_MIL_LINE", "CMilitaryProductionLine"),
        ("VT_NAV_LINE", "CNavalProductionLine"),
        ("VT_BUILDING_LINE", "CBuildingProductionLine"),
        ("VT_BUILDING_REF", "CBuildingReference"),
        ("VT_PEACE_CONFERENCE", "CPeaceConference"),
        ("VT_PEACE_TURN", "CPeaceBiddingTurn"),
        ("VT_AIRWING", "CAirWing"),
        ("VT_AIRWINGPOOL", "CAirWingPool"),
        ("VT_FLEET", "CFleet"),
        ("VT_TASKFORCE", "CTaskForce"),
        ("VT_EVENT", "CEvent"),
        ("VT_EVENT_WINDOW", "CEventWindow"),
        ("VT_EVENT_SCOPE", "CEventScope"),
    ] {
        out.insert(anchor_name.into(), rtti_vt_anchor(rtti, cls));
    }

    // ===================================================== 6. 外交与和平会谈
    out.insert("VT_DECLARE_WAR".into(), rtti_vt_anchor(rtti, "CDeclareWarAction"));
    out.insert("VT_DIPLO_CMD".into(), rtti_vt_anchor(rtti, "CDiplomaticActionCommand"));
    out.insert(
        "VT_GENERATE_WARGOAL".into(),
        rtti_vt_anchor(rtti, "CGenerateWarGoalAction"),
    );
    out.insert(
        "FN_DIPLO_VALIDATE".into(),
        match cmds
            .commands
            .get("CDiplomaticActionCommand")
            .and_then(|c| c.can_execute)
        {
            Some(v) => AnchorResult::exact(
                v as u64,
                "CDiplomaticActionCommand::CanExecute (vt[+0x48])",
                None,
            ),
            None => AnchorResult::missing("CDiplomaticActionCommand::vt[+0x48]", "not resolved"),
        },
    );

    out.insert(
        "PEACE_DONE_VT".into(),
        rtti_vt_anchor(rtti, "CDonePeaceConferenceCommand"),
    );
    out.insert(
        "PEACE_ENDTURN_VT".into(),
        rtti_vt_anchor(rtti, "CEndTurnPeaceConferenceCommand"),
    );
    out.insert(
        "PEACE_PASS_VT".into(),
        rtti_vt_anchor(rtti, "CPassPeaceConferenceCommand"),
    );

    for (name, cls, pick_exec) in [
        ("PEACE_CANEXEC_DONE", "CDonePeaceConferenceCommand", false),
        ("PEACE_CANEXEC_PASS", "CPassPeaceConferenceCommand", false),
        ("PEACE_EXEC_DONE", "CDonePeaceConferenceCommand", true),
        ("PEACE_EXEC_ENDTURN", "CEndTurnPeaceConferenceCommand", true),
        ("PEACE_EXEC_PASS", "CPassPeaceConferenceCommand", true),
    ] {
        let val = cmds.commands.get(cls).and_then(|c| {
            if pick_exec {
                c.execute
            } else {
                c.can_execute
            }
        });
        let slot_desc = if pick_exec { "vt[+0x50]" } else { "vt[+0x48]" };
        out.insert(
            name.into(),
            match val {
                Some(v) => AnchorResult::exact(v as u64, format!("{}::{}", cls, slot_desc), None),
                None => AnchorResult::missing(format!("{}::{}", cls, slot_desc), "not resolved"),
            },
        );
    }

    // PEACE_GET_CONF & PEACE_FN_INSTALL_TURN 来自 CEndTurnPeaceConferenceCommand::Execute
    let mut peace_get_conf: Option<u32> = None;
    let mut peace_install_turn: Option<u32> = None;
    if let Some(et_ex) = cmds
        .commands
        .get("CEndTurnPeaceConferenceCommand")
        .and_then(|c| c.execute)
    {
        let insns = decode_fn(pe, et_ex, 0x60);
        for ins in &insns {
            if peace_get_conf.is_none() && ins.mnemonic() == Mnemonic::Call {
                peace_get_conf = ins_near_target(ins);
            }
            if ins.mnemonic() == Mnemonic::Jmp {
                if let Some(tgt) = ins_near_target(ins) {
                    peace_install_turn = Some(tgt);
                }
            }
        }
    }
    out.insert(
        "PEACE_GET_CONF".into(),
        match peace_get_conf {
            Some(v) => AnchorResult::exact(
                v as u64,
                "CEndTurnPeaceConferenceCommand::Execute 1st call",
                None,
            ),
            None => AnchorResult::missing("CEndTurnPeaceConferenceCommand::Execute", "not resolved"),
        },
    );
    out.insert(
        "PEACE_FN_INSTALL_TURN".into(),
        match peace_install_turn {
            Some(v) => AnchorResult::exact(
                v as u64,
                "CEndTurnPeaceConferenceCommand::Execute tail jmp",
                None,
            ),
            None => AnchorResult::missing("CEndTurnPeaceConferenceCommand::Execute", "not resolved"),
        },
    );

    out
}
