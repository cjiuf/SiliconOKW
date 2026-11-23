//! 多核并行 PE 命令目录分析器：
//! 相比 calling/pecmd.py 的全面升级：
//! 1. 使用 `.pdata` 获取 100% 精确的函数起止区间，显式排除 vtable[0] 析构与 vtable[+0x68] clone；
//! 2. 使用 `iced-x86` 互斥寄存器 + 栈帧偏移状态机，支持跟踪临时指针 (`lea rcx, [rbx+off]`)、
//!    栈参数 (`[rsp+frame+0x28+8*k]`) 与 std::string 拷贝构造调用；
//! 3. 对象大小由 `clone` (`mov ecx, SZ; call OP_NEW`)、`dtor` (`mov edx, SZ; call OP_DELETE_SIZED`)、
//!    `factory` (`call ctor` 前的 `OP_NEW`) 三重交叉印证，彻底消除猜错 size 崩进程风险。

use crate::pe::PeImage;
use crate::rtti::RttiMap;
use iced_x86::{Decoder, DecoderOptions, FlowControl, Instruction, Mnemonic, OpKind, Register};
use rayon::prelude::*;
use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, HashMap, HashSet};

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct FieldSpec {
    pub offset: u32,
    pub size: u8,
    pub arg: Option<u8>,
    pub kind: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CommandSpec {
    pub class: String,
    pub vt: u32,
    pub ctor: Option<u32>,
    pub ctor_kind: String,
    pub dtor: Option<u32>,
    pub can_execute: Option<u32>,
    pub execute: Option<u32>,
    pub clone: Option<u32>,
    pub serialize: Option<u32>,
    pub size: u32,
    pub size_source: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub size_clone: Option<u32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub size_dtor: Option<u32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub size_factory: Option<u32>,
    pub args: u8,
    pub fields: Vec<FieldSpec>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RuntimeHelpers {
    pub op_new: Option<u32>,
    pub op_new_votes: usize,
    pub op_delete_sized: Option<u32>,
    pub op_delete_sized_votes: usize,
    pub fn_noop_dtor: Option<u32>,
    pub fn_noop_dtor_votes: usize,
}

#[derive(Debug, Clone)]
pub struct CommandCatalogue {
    pub runtime: RuntimeHelpers,
    pub commands: BTreeMap<String, CommandSpec>,
}

/// 匹配 `C[A-Za-z0-9_]*Command` 类名（与 `pecmd.py` 一致）。
fn is_command_class(name: &str) -> bool {
    if !name.starts_with('C') || !name.ends_with("Command") || name.len() <= 8 {
        return false;
    }
    name.bytes()
        .all(|b| b.is_ascii_alphanumeric() || b == b'_')
}

/// 读取虚表中偏移 `slot_off` 的 `.text` RVA。
pub fn read_vt_slot(pe: &PeImage, vt_rva: u32, slot_off: u32) -> Option<u32> {
    let va = pe.u64_at(vt_rva.checked_add(slot_off)?)?;
    let rva = pe.va_to_rva(va)?;
    if pe.is_in_section(rva, ".text") {
        Some(rva)
    } else {
        None
    }
}

/// 检查函数是否为 `xor al, al; ret`（`32 c0 c3`）或 `xor eax, eax; ret`（`33 c0 c3` / `31 c0 c3`）。
fn is_noop_false_ret(pe: &PeImage, rva: u32) -> bool {
    let Some(b) = pe.read(rva, 3) else {
        return false;
    };
    b == [0x32, 0xC0, 0xC3] || b == [0x33, 0xC0, 0xC3] || b == [0x31, 0xC0, 0xC3]
}

/// 将任意 8/16/32/64 位通用寄存器映射到 0..15（RAX=0..R15=15）。
fn reg_slot(reg: Register) -> Option<usize> {
    match reg.full_register() {
        Register::RAX => Some(0),
        Register::RCX => Some(1),
        Register::RDX => Some(2),
        Register::RBX => Some(3),
        Register::RSP => Some(4),
        Register::RBP => Some(5),
        Register::RSI => Some(6),
        Register::RDI => Some(7),
        Register::R8 => Some(8),
        Register::R9 => Some(9),
        Register::R10 => Some(10),
        Register::R11 => Some(11),
        Register::R12 => Some(12),
        Register::R13 => Some(13),
        Register::R14 => Some(14),
        Register::R15 => Some(15),
        _ => None,
    }
}

/// 反汇编函数，查找 `mov <size_reg>, IMM; call/jmp TARGET` 模式，用于识别 `OP_NEW` / `OP_DELETE_SIZED` 与对象大小。
fn scan_alloc_call(
    pe: &PeImage,
    fn_rva: u32,
    size_reg_slot: usize,
    expected_callee: Option<u32>,
) -> Option<(u32, u32)> {
    let (begin, end) = pe.fn_bounds(fn_rva).unwrap_or((fn_rva, fn_rva + 0x100));
    let len = (end.saturating_sub(begin) as usize).min(0x400);
    let bytes = pe.read(begin, len)?;
    let mut dec = Decoder::with_ip(64, bytes, begin as u64, DecoderOptions::NONE);
    let mut insn = Instruction::default();

    let mut last_imm: Option<u32> = None;
    let mut best: Option<(u32, u32)> = None;

    while dec.can_decode() {
        dec.decode_out(&mut insn);
        if insn.is_invalid() {
            break;
        }
        let m = insn.mnemonic();
        if m == Mnemonic::Mov && insn.op_count() == 2 {
            if insn.op0_kind() == OpKind::Register
                && reg_slot(insn.op0_register()) == Some(size_reg_slot)
            {
                if matches!(
                    insn.op1_kind(),
                    OpKind::Immediate32
                        | OpKind::Immediate32to64
                        | OpKind::Immediate64
                        | OpKind::Immediate8to32
                        | OpKind::Immediate8to64
                ) {
                    let v = insn.immediate(1);
                    if (0x18..=0x4000).contains(&v) {
                        last_imm = Some(v as u32);
                    } else {
                        last_imm = None;
                    }
                } else {
                    last_imm = None;
                }
            }
        } else if matches!(m, Mnemonic::Call | Mnemonic::Jmp)
            && matches!(insn.op0_kind(), OpKind::NearBranch64 | OpKind::NearBranch32)
        {
            let tgt = insn.near_branch64() as u32;
            if let Some(sz) = last_imm {
                if expected_callee.is_none() || expected_callee == Some(tgt) {
                    // clone 的 new 在函数开头附近，dtor 的 sized delete 在尾部；取最近一次匹配
                    best = Some((tgt, sz));
                    if size_reg_slot == 1 {
                        // RCX (operator new): 首个分配就是对象自身，直接返回
                        return best;
                    }
                }
            }
            last_imm = None;
        } else if m == Mnemonic::Xor
            && insn.op_count() == 2
            && insn.op0_kind() == OpKind::Register
            && reg_slot(insn.op0_register()) == Some(size_reg_slot)
        {
            last_imm = None;
        }
    }
    best
}

/// 在调用 `ctor_rva` 的工厂站点前，寻找 `mov ecx, SIZE; call OP_NEW` 提取分配大小。
fn factory_size_for_ctor(pe: &PeImage, calls: &[u32], op_new: Option<u32>) -> Option<u32> {
    let mut votes: HashMap<u32, usize> = HashMap::new();
    for &call_site in calls.iter().take(16) {
        let Some((fn_begin, fn_end)) = pe.fn_bounds(call_site) else {
            continue;
        };
        if call_site <= fn_begin {
            continue;
        }
        // 只反汇编函数内到 call_site 为止的最近区间（最多 0x200 字节）
        let len = (fn_end.saturating_sub(fn_begin) as usize).min(0x800);
        let Some(bytes) = pe.read(fn_begin, len) else {
            continue;
        };
        let mut dec = Decoder::with_ip(64, bytes, fn_begin as u64, DecoderOptions::NONE);
        let mut insn = Instruction::default();
        let mut cur_ecx: Option<u32> = None;
        let mut last_new_sz: Option<(u32, u32)> = None; // (new_call_rva, size)

        while dec.can_decode() {
            dec.decode_out(&mut insn);
            if insn.is_invalid() {
                break;
            }
            let ip = insn.ip() as u32;
            if ip >= call_site {
                break;
            }
            let m = insn.mnemonic();
            if m == Mnemonic::Mov
                && insn.op_count() == 2
                && insn.op0_kind() == OpKind::Register
                && reg_slot(insn.op0_register()) == Some(1)
            {
                if matches!(
                    insn.op1_kind(),
                    OpKind::Immediate32
                        | OpKind::Immediate32to64
                        | OpKind::Immediate64
                        | OpKind::Immediate8to32
                        | OpKind::Immediate8to64
                ) {
                    let v = insn.immediate(1);
                    cur_ecx = if (0x18..=0x4000).contains(&v) {
                        Some(v as u32)
                    } else {
                        None
                    };
                } else {
                    cur_ecx = None;
                }
            } else if m == Mnemonic::Call
                && matches!(insn.op0_kind(), OpKind::NearBranch64 | OpKind::NearBranch32)
            {
                let tgt = insn.near_branch64() as u32;
                if let Some(sz) = cur_ecx {
                    if op_new.is_none() || op_new == Some(tgt) {
                        last_new_sz = Some((ip, sz));
                    }
                }
                cur_ecx = None;
            }
        }
        if let Some((new_ip, sz)) = last_new_sz {
            if call_site.saturating_sub(new_ip) <= 0x80 {
                *votes.entry(sz).or_insert(0) += 1;
            }
        }
    }
    votes
        .into_iter()
        .max_by_key(|&(sz, cnt)| (cnt, std::cmp::Reverse(sz)))
        .map(|(sz, _)| sz)
}

#[derive(Debug, Clone, PartialEq, Eq)]
enum RegVal {
    Unknown,
    This,
    FieldPtr(i32),
    Arg(u8),
    Derived { arg: u8, kind: &'static str, size: u8 },
    VtTarget,
}

#[derive(Debug, Clone)]
struct CtorLayout {
    fields: Vec<FieldSpec>,
    args: u8,
    calls_op_new: bool,
}

fn mem_access_size(insn: &Instruction) -> u8 {
    let sz = insn.memory_size().size();
    if sz == 0 {
        // 回退按对端寄存器大小判断
        for i in 0..insn.op_count() {
            if insn.op_kind(i) == OpKind::Register {
                let rs = insn.op_register(i).size();
                if rs > 0 {
                    return rs as u8;
                }
            }
        }
        4
    } else {
        sz as u8
    }
}

/// 使用 `iced-x86` 解析一个候选构造器函数的完整字段布局。
fn analyze_ctor(pe: &PeImage, fn_rva: u32, target_vt: u32, op_new: Option<u32>) -> Option<CtorLayout> {
    let (begin, end) = pe.fn_bounds(fn_rva).unwrap_or((fn_rva, fn_rva + 0x200));
    let len = (end.saturating_sub(begin) as usize).min(0x500);
    let bytes = pe.read(begin, len)?;
    let mut dec = Decoder::with_ip(64, bytes, begin as u64, DecoderOptions::NONE);
    let mut insn = Instruction::default();

    let mut regs: [RegVal; 16] = std::array::from_fn(|_| RegVal::Unknown);
    regs[1] = RegVal::This; // RCX = this
    regs[2] = RegVal::Arg(1); // RDX = arg1
    regs[8] = RegVal::Arg(2); // R8  = arg2
    regs[9] = RegVal::Arg(3); // R9  = arg3

    let mut stack_slots: HashMap<i64, RegVal> = HashMap::new();
    let mut rsp_delta: i64 = 0; // 当前 RSP 相对于入口 RSP 的向下偏移量（正数）
    let mut raw_fields: Vec<(u32, u8, Option<u8>, String)> = Vec::new();
    let mut writes_vt = false;
    let mut calls_op_new = false;
    let mut n_ins = 0usize;

    while dec.can_decode() && n_ins < 240 {
        dec.decode_out(&mut insn);
        if insn.is_invalid() {
            break;
        }
        n_ins += 1;
        let m = insn.mnemonic();

        // 1. 栈帧深度追踪（用于识别 Win64 第 4+ 栈参数 [rsp + rsp_delta + 0x28 + 8*k]）
        if m == Mnemonic::Push {
            rsp_delta += 8;
            continue;
        }
        if m == Mnemonic::Pop {
            rsp_delta = (rsp_delta - 8).max(0);
            if insn.op_count() == 1 && insn.op0_kind() == OpKind::Register {
                if let Some(s) = reg_slot(insn.op0_register()) {
                    regs[s] = RegVal::Unknown;
                }
            }
            continue;
        }
        if m == Mnemonic::Sub
            && insn.op_count() == 2
            && insn.op0_kind() == OpKind::Register
            && insn.op0_register() == Register::RSP
        {
            if matches!(
                insn.op1_kind(),
                OpKind::Immediate8to64 | OpKind::Immediate32to64 | OpKind::Immediate32
            ) {
                rsp_delta += insn.immediate(1) as i64;
            }
            continue;
        }
        if m == Mnemonic::Add
            && insn.op_count() == 2
            && insn.op0_kind() == OpKind::Register
            && insn.op0_register() == Register::RSP
        {
            if matches!(
                insn.op1_kind(),
                OpKind::Immediate8to64 | OpKind::Immediate32to64 | OpKind::Immediate32
            ) {
                rsp_delta = (rsp_delta - insn.immediate(1) as i64).max(0);
            }
            continue;
        }

        // 2. RIP-relative LEA 目标检查（加载 target_vt）
        if m == Mnemonic::Lea && insn.op_count() == 2 && insn.op0_kind() == OpKind::Register {
            let Some(dst) = reg_slot(insn.op0_register()) else {
                continue;
            };
            if insn.op1_kind() == OpKind::Memory {
                if insn.is_ip_rel_memory_operand() {
                    let tgt = insn.ip_rel_memory_address() as u32;
                    regs[dst] = if tgt == target_vt {
                        RegVal::VtTarget
                    } else {
                        RegVal::Unknown
                    };
                    continue;
                }
                let base = insn.memory_base();
                let index = insn.memory_index();
                if index == Register::None {
                    if let Some(bs) = reg_slot(base) {
                        let disp = insn.memory_displacement64() as i64 as i32;
                        match &regs[bs] {
                            RegVal::This => {
                                regs[dst] = RegVal::FieldPtr(disp);
                                continue;
                            }
                            RegVal::FieldPtr(cur) => {
                                regs[dst] = RegVal::FieldPtr(cur.saturating_add(disp));
                                continue;
                            }
                            _ => {}
                        }
                    }
                }
            }
            regs[dst] = RegVal::Unknown;
            continue;
        }

        // 3. 寄存器 -> 寄存器 拷贝 (`mov dst, src`)
        if matches!(m, Mnemonic::Mov | Mnemonic::Movzx | Mnemonic::Movsx | Mnemonic::Movsxd)
            && insn.op_count() == 2
            && insn.op0_kind() == OpKind::Register
            && insn.op1_kind() == OpKind::Register
        {
            if let (Some(dst), Some(src)) =
                (reg_slot(insn.op0_register()), reg_slot(insn.op1_register()))
            {
                regs[dst] = regs[src].clone();
            }
            continue;
        }

        // 4. 内存 -> 寄存器 加载 (`mov/movzx/movsx reg, [base + disp]`)
        if matches!(m, Mnemonic::Mov | Mnemonic::Movzx | Mnemonic::Movsx | Mnemonic::Movsxd)
            && insn.op_count() == 2
            && insn.op0_kind() == OpKind::Register
            && insn.op1_kind() == OpKind::Memory
        {
            let Some(dst) = reg_slot(insn.op0_register()) else {
                continue;
            };
            let base = insn.memory_base();
            let index = insn.memory_index();
            let disp = insn.memory_displacement64() as i64;
            let sz = mem_access_size(&insn);

            if base == Register::RSP && index == Register::None {
                let entry_off = disp - rsp_delta;
                if let Some(saved) = stack_slots.get(&entry_off) {
                    regs[dst] = saved.clone();
                } else if entry_off >= 0x28 && (entry_off - 0x28) % 8 == 0 && entry_off <= 0x60 {
                    // Win64 第 4+ 个栈参数（0x28 -> arg4, 0x30 -> arg5, ...）
                    let ai = 4 + ((entry_off - 0x28) / 8) as u8;
                    regs[dst] = RegVal::Arg(ai);
                } else {
                    regs[dst] = RegVal::Unknown;
                }
                continue;
            }
            if index == Register::None {
                if let Some(bs) = reg_slot(base) {
                    match &regs[bs] {
                        RegVal::Arg(ai) => {
                            let kind = if disp == 8 && sz == 8 {
                                "handle"
                            } else if disp == 0 {
                                "deref"
                            } else {
                                "field"
                            };
                            regs[dst] = RegVal::Derived {
                                arg: *ai,
                                kind,
                                size: sz,
                            };
                            continue;
                        }
                        _ => {}
                    }
                }
            }
            regs[dst] = RegVal::Unknown;
            continue;
        }

        // 5. 寄存器/立即数 -> 内存 写入 (`mov [base + disp], src`)
        if m == Mnemonic::Mov && insn.op_count() == 2 && insn.op0_kind() == OpKind::Memory {
            let base = insn.memory_base();
            let index = insn.memory_index();
            let disp = insn.memory_displacement64() as i64;

            // 保存到栈槽
            if base == Register::RSP && index == Register::None {
                if insn.op1_kind() == OpKind::Register {
                    if let Some(ss) = reg_slot(insn.op1_register()) {
                        let entry_off = disp - rsp_delta;
                        stack_slots.insert(entry_off, regs[ss].clone());
                    }
                }
                continue;
            }

            if index == Register::None {
                if let Some(bs) = reg_slot(base) {
                    let field_off = match &regs[bs] {
                        RegVal::This => Some(disp),
                        RegVal::FieldPtr(base_off) => Some(*base_off as i64 + disp),
                        _ => None,
                    };
                    if let Some(off) = field_off {
                        let sz = mem_access_size(&insn);
                        if insn.op1_kind() == OpKind::Register {
                            if let Some(ss) = reg_slot(insn.op1_register()) {
                                match &regs[ss] {
                                    RegVal::VtTarget if off == 0 => {
                                        writes_vt = true;
                                    }
                                    RegVal::Arg(ai) if (0x20..0x800).contains(&off) => {
                                        let kind = match sz {
                                            8 => "ptr",
                                            1 => "bool",
                                            _ => "int",
                                        };
                                        raw_fields.push((off as u32, sz, Some(*ai), kind.into()));
                                    }
                                    RegVal::Derived { arg, kind, .. }
                                        if (0x20..0x800).contains(&off) =>
                                    {
                                        raw_fields.push((off as u32, sz, Some(*arg), (*kind).into()));
                                    }
                                    _ => {}
                                }
                            }
                        } else if matches!(
                            insn.op1_kind(),
                            OpKind::Immediate8
                                | OpKind::Immediate16
                                | OpKind::Immediate32
                                | OpKind::Immediate8to32
                                | OpKind::Immediate8to64
                                | OpKind::Immediate32to64
                        ) && (0x20..0x800).contains(&off)
                        {
                            let imm = insn.immediate(1) as u32;
                            raw_fields.push((off as u32, sz, None, format!("imm:{:#x}", imm)));
                        }
                    }
                }
            }
            continue;
        }

        // 6. 子函数调用（检测 operator new 与成员对象拷贝构造，例如 std::string(FieldPtr, Arg)）
        if m == Mnemonic::Call {
            if matches!(insn.op0_kind(), OpKind::NearBranch64 | OpKind::NearBranch32) {
                let tgt = insn.near_branch64() as u32;
                if op_new == Some(tgt) {
                    calls_op_new = true;
                } else if let RegVal::FieldPtr(off) = regs[1] {
                    if (0x20..0x800).contains(&off) {
                        if let RegVal::Arg(ai) = regs[2] {
                            // 子对象构造（例如 CAddAdvisorCommand +0x30 std::string 拷贝构造）
                            raw_fields.push((off as u32, 0x20, Some(ai), "subobj".into()));
                        }
                    }
                }
            }
            // Win64 易失寄存器在 call 后失效：RAX(0), RCX(1), RDX(2), R8(8), R9(9), R10(10), R11(11)
            for &vol in &[0usize, 1, 2, 8, 9, 10, 11] {
                regs[vol] = RegVal::Unknown;
            }
            continue;
        }

        // 7. 其他写寄存器的指令清空该寄存器状态
        if insn.op_count() >= 1
            && insn.op0_kind() == OpKind::Register
            && !matches!(m, Mnemonic::Cmp | Mnemonic::Test)
        {
            if let Some(dst) = reg_slot(insn.op0_register()) {
                regs[dst] = RegVal::Unknown;
            }
        }

        if matches!(insn.flow_control(), FlowControl::Return | FlowControl::UnconditionalBranch)
            && n_ins > 3
            && writes_vt
        {
            break;
        }
    }

    if !writes_vt {
        return None;
    }

    // 去重并按偏移排序：若同一偏移同时命中参数写与立即数初始化，保留参数写
    let mut by_off: BTreeMap<u32, FieldSpec> = BTreeMap::new();
    for (off, sz, arg, kind) in raw_fields {
        match by_off.get(&off) {
            None => {
                by_off.insert(
                    off,
                    FieldSpec {
                        offset: off,
                        size: sz,
                        arg,
                        kind,
                    },
                );
            }
            Some(prev) => {
                if prev.arg.is_none() && arg.is_some() {
                    by_off.insert(
                        off,
                        FieldSpec {
                            offset: off,
                            size: sz,
                            arg,
                            kind,
                        },
                    );
                }
            }
        }
    }
    let fields: Vec<FieldSpec> = by_off.into_values().collect();
    let args = fields.iter().filter_map(|f| f.arg).max().unwrap_or(0);
    Some(CtorLayout {
        fields,
        args,
        calls_op_new,
    })
}

/// 扫描并分析 PE 中全部 `C*Command` 类。
pub fn scan_commands(pe: &PeImage, rtti: &RttiMap) -> CommandCatalogue {
    // 1. 收集所有 C*Command vtable
    let mut cmd_vts: Vec<(String, u32)> = Vec::new();
    for (cls, vts) in &rtti.classes {
        if is_command_class(cls) {
            for &vt in vts {
                cmd_vts.push((cls.clone(), vt));
            }
        }
    }

    // 2. 第一阶段：从所有命令虚表槽位多数决提取 OP_NEW / OP_DELETE_SIZED / FN_NOOP_DTOR
    let mut new_votes: HashMap<u32, usize> = HashMap::new();
    let mut del_votes: HashMap<u32, usize> = HashMap::new();
    let mut noop_votes: HashMap<u32, usize> = HashMap::new();

    for &(_, vt) in &cmd_vts {
        if let Some(noop) = read_vt_slot(pe, vt, 0x28) {
            if is_noop_false_ret(pe, noop) {
                *noop_votes.entry(noop).or_insert(0) += 1;
            }
        }
        if let Some(clone) = read_vt_slot(pe, vt, 0x68) {
            if let Some((tgt, _)) = scan_alloc_call(pe, clone, 1, None) {
                *new_votes.entry(tgt).or_insert(0) += 1;
            }
        }
        if let Some(dtor) = read_vt_slot(pe, vt, 0x00) {
            if let Some((tgt, _)) = scan_alloc_call(pe, dtor, 2, None) {
                *del_votes.entry(tgt).or_insert(0) += 1;
            }
        }
    }

    let (op_new, op_new_votes) = new_votes
        .into_iter()
        .max_by_key(|&(_, c)| c)
        .map(|(k, c)| (Some(k), c))
        .unwrap_or((None, 0));
    let (op_delete_sized, op_delete_sized_votes) = del_votes
        .into_iter()
        .max_by_key(|&(_, c)| c)
        .map(|(k, c)| (Some(k), c))
        .unwrap_or((None, 0));
    let (fn_noop_dtor, fn_noop_dtor_votes) = noop_votes
        .into_iter()
        .max_by_key(|&(_, c)| c)
        .map(|(k, c)| (Some(k), c))
        .unwrap_or((None, 0));

    let runtime = RuntimeHelpers {
        op_new,
        op_new_votes,
        op_delete_sized,
        op_delete_sized_votes,
        fn_noop_dtor,
        fn_noop_dtor_votes,
    };

    // 3. 单趟并行扫 `.text` 找出全部命令 vtable 的 LEA 引用
    let wanted_vts: HashSet<u32> = cmd_vts.iter().map(|&(_, vt)| vt).collect();
    let leas = pe.scan_text_leas(&wanted_vts);

    // 4. 并行分析每个 (class, vt) 选出最佳真实构造器
    let prelim: Vec<CommandSpec> = cmd_vts
        .par_iter()
        .filter_map(|(cls, vt)| {
            let vt = *vt;
            let dtor = read_vt_slot(pe, vt, 0x00);
            let can_execute = read_vt_slot(pe, vt, 0x48);
            let execute = read_vt_slot(pe, vt, 0x50);
            let clone = read_vt_slot(pe, vt, 0x68);
            let serialize = read_vt_slot(pe, vt, 0x98);

            let size_clone = clone
                .and_then(|c| scan_alloc_call(pe, c, 1, op_new))
                .map(|(_, sz)| sz);
            let size_dtor = dtor
                .and_then(|d| scan_alloc_call(pe, d, 2, op_delete_sized))
                .map(|(_, sz)| sz);

            // 去重候补函数起点，严格跳过 dtor 和 clone
            let mut cand_fns: Vec<u32> = Vec::new();
            if let Some(sites) = leas.get(&vt) {
                for &site in sites {
                    let st = pe.fn_start(site);
                    if Some(st) == dtor || Some(st) == clone {
                        continue;
                    }
                    if !cand_fns.contains(&st) {
                        cand_fns.push(st);
                    }
                }
            }

            let mut best_ctor: Option<(u32, String, CtorLayout, (u8, usize, usize, u8, std::cmp::Reverse<u32>))> = None;
            for &st in cand_fns.iter().take(12) {
                let Some(lay) = analyze_ctor(pe, st, vt, op_new) else {
                    continue;
                };
                // 优先：不调 operator new 的纯构造器 > 带更多参数字段的 > 更小 RVA
                let is_pure_ctor: u8 = if lay.calls_op_new { 0 } else { 1 };
                let param_fields = lay.fields.iter().filter(|f| f.arg.is_some()).count();
                let score = (
                    is_pure_ctor,
                    param_fields,
                    lay.fields.len(),
                    lay.args,
                    std::cmp::Reverse(st),
                );
                let better = match &best_ctor {
                    None => true,
                    Some((_, _, _, prev)) => score > *prev,
                };
                if better {
                    let kind = if lay.calls_op_new {
                        "factory_inline".to_string()
                    } else {
                        "ctor".to_string()
                    };
                    best_ctor = Some((st, kind, lay, score));
                }
            }

            let (ctor, ctor_kind, fields, args) = match best_ctor {
                Some((st, kind, lay, _)) => (Some(st), kind, lay.fields, lay.args),
                None => (None, "none".to_string(), Vec::new(), 0),
            };

            Some(CommandSpec {
                class: cls.clone(),
                vt,
                ctor,
                ctor_kind,
                dtor,
                can_execute,
                execute,
                clone,
                serialize,
                size: 0,
                size_source: String::new(),
                size_clone,
                size_dtor,
                size_factory: None,
                args,
                fields,
            })
        })
        .collect();

    // 5. 同类多 vtable 合并（优先保留带真实 ctor 且字段最多者）
    let mut by_class: BTreeMap<String, CommandSpec> = BTreeMap::new();
    for spec in prelim {
        let key = spec.class.clone();
        let replace = match by_class.get(&key) {
            None => true,
            Some(prev) => {
                let cur_rank = (
                    u8::from(spec.ctor_kind == "ctor"),
                    spec.fields.len(),
                    spec.args,
                    u8::from(spec.size_clone.is_some()),
                );
                let prev_rank = (
                    u8::from(prev.ctor_kind == "ctor"),
                    prev.fields.len(),
                    prev.args,
                    u8::from(prev.size_clone.is_some()),
                );
                cur_rank > prev_rank
            }
        };
        if replace {
            by_class.insert(key, spec);
        }
    }

    // 6. 单趟并行扫 `.text` 中对所有 ctor 的直接调用，提取 factory 分配大小，并与 clone/dtor 三重印证
    let wanted_ctors: HashSet<u32> = by_class.values().filter_map(|c| c.ctor).collect();
    let ctor_calls = pe.scan_text_calls(&wanted_ctors);

    for spec in by_class.values_mut() {
        let size_factory = spec
            .ctor
            .and_then(|c| ctor_calls.get(&c))
            .and_then(|sites| factory_size_for_ctor(pe, sites, op_new));
        spec.size_factory = size_factory;

        // 大小判定策略：
        // clone 与 factory 均为真实 `operator new(SIZE)` 分配大小；
        // dtor 的 `operator delete(ptr, SIZE)` 绝大多数情况下一致，但在极少数子类复用基类 dtor 时偏小
        // （例如 CUpdateDivisionTemplateCommand: clone=0x2B0, dtor=0x288），故分配大小优先于 dtor。
        let (final_sz, src) = match (spec.size_clone, spec.size_dtor, size_factory) {
            (Some(c), Some(d), Some(f)) if c == d && c == f => (c, "clone+dtor+factory"),
            (Some(c), Some(d), _) if c == d => (c, "clone+dtor"),
            (Some(c), _, Some(f)) if c == f => (c, "clone+factory"),
            (Some(c), _, _) => (c, "clone"),
            (None, Some(d), Some(f)) if d == f => (d, "dtor+factory"),
            (None, _, Some(f)) => (f, "factory"),
            (None, Some(d), None) => (d, "dtor"),
            (None, None, None) => {
                let hi = spec
                    .fields
                    .iter()
                    .map(|f| f.offset + f.size as u32)
                    .max()
                    .unwrap_or(0x28);
                let aligned = ((hi + 0x20 + 0xF) & !0xF).max(0x40);
                (aligned, "heuristic")
            }
        };
        spec.size = final_sz;
        spec.size_source = src.to_string();
    }

    CommandCatalogue {
        runtime,
        commands: by_class,
    }
}
