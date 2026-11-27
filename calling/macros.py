# -*- coding: utf-8 -*-
"""老手级原子化宏工具与战略体检看板（Macro Tools & Strategic Health Board）。

设计初衷（2026-11-23 复测 7 后架构升级）：
  大模型不擅长在 140 个碎工具里手工做数值配平与空间拓扑算账（如改 9步3炮编制后算火炮缺口
  再从超储步枪产线抽厂，或多线开战时给次要边境与本土预备队留兵并严守真实补给上限）。
  本模块把三类最容易被大模型玩蠢的高耦合操作封装成「1 步原子宏 + 确定性算账」：
    1. rebalance_production      —— 按库存/编制缺口与老手比例自动从超储线挪厂给紧缺线；
    2. upgrade_template_preset   —— 一键套用经验证的经典师编制，并自动算缺口联动挪厂；
    3. plan_front_allocation     —— 按保独联军、真实补给上限与强制预备队划分多线兵力；
    4. strategic_health_board    —— 纯只读战略体检红字看板（嵌入回合简报与 overview）。
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Tuple


# ===================================================================== 经典编制预设库
TEMPLATE_PRESETS: Dict[str, Dict[str, Any]] = {
    "inf_9_1": {
        "name": "9步1炮过渡填线师 (21w)",
        "role": "infantry",
        "width": 21,
        "line": {"infantry": 9, "artillery_brigade": 1},
        "support": {"engineer": 1, "artillery": 1},
        "approx_xp": 25,
        "summary": "前期炮兵产能尚低时的性价比过渡主力，自带炮兵软攻与工兵堑壕。",
    },
    "inf_9_3": {
        "name": "9步3炮主力步炮师 (27w)",
        "role": "infantry",
        "width": 27,
        "line": {"infantry": 9, "artillery_brigade": 3},
        "support": {"engineer": 1, "artillery": 1},
        "approx_xp": 45,
        "summary": "高软攻/低补给摊薄的泛用攻坚步兵师，用质量替代人海解决前线断补。",
    },
    "inf_9_3_aa": {
        "name": "9步3炮+防空主力师 (27w)",
        "role": "infantry",
        "width": 27,
        "line": {"infantry": 9, "artillery_brigade": 3},
        "support": {"engineer": 1, "artillery": 1, "anti_air": 1},
        "approx_xp": 55,
        "summary": "在 9/3 基础上加防空支援连，大幅减免敌方制空/CAS 伤害并补穿甲。",
    },
    "inf_9_0_hold": {
        "name": "9步纯填线守备师 (18w)",
        "role": "infantry",
        "width": 18,
        "line": {"infantry": 9},
        "support": {"engineer": 1},
        "approx_xp": 15,
        "summary": "低造价次要防线/海岸守备模板。",
    },
    "armor_light_20w": {
        "name": "6轻坦4摩步初期突击师 (20w)",
        "role": "armor",
        "width": 20,
        "line": {"light_armor": 6, "motorized": 4},
        "support": {"engineer": 1, "artillery": 1},
        "approx_xp": 35,
        "summary": "1936-1939 初期高速穿插包饺子矛头。",
    },
    "armor_medium_30w": {
        "name": "8中坦7摩步主力装甲矛头 (30w)",
        "role": "armor",
        "width": 30,
        "line": {"medium_armor": 8, "motorized": 7},
        "support": {"engineer": 1, "artillery": 1, "logistics_company": 1},
        "approx_xp": 60,
        "summary": "1939-1942 破线主力装甲师，兼顾组织度、突破与后勤省补给。",
    },
}


def _safe_tag(g, tag=None):
    if tag and str(tag).lower() != "player":
        return str(tag).upper()
    try:
        return str(g.player_tag()).upper()
    except Exception:
        try:
            return str(g.player().tag).upper()
        except Exception:
            return "GER"


def _prod_family(equipment: str) -> str:
    """将装备名归类为生产配平族：inf / art / support / aa / armor / air / other。"""
    eq = str(equipment or "").lower()
    if "infantry_equipment" in eq:
        return "inf"
    if "artillery" in eq:
        return "art"
    if "support_equipment" in eq:
        return "support"
    if "anti_air" in eq:
        return "aa"
    if "anti_tank" in eq:
        return "at"
    if any(k in eq for k in ("tank", "armor", "motorized", "mechanized", "armored_car")):
        return "armor"
    if any(k in eq for k in ("fighter", "cas", "bomber", "airframe", "plane")):
        return "air"
    return "other"


def _sum_stockpile_by_family(totals: Dict[str, float]) -> Dict[str, float]:
    out: Dict[str, float] = {}
    for eq, cnt in (totals or {}).items():
        fam = _prod_family(eq)
        out[fam] = out.get(fam, 0.0) + float(cnt or 0.0)
    return out


def _normalize_extra_demand(extra_demand: Optional[Dict[str, float]]) -> Dict[str, float]:
    """把按装备 key 或族名传入的新增缺口统一归并到家族维度。"""
    out: Dict[str, float] = {}
    for k, v in (extra_demand or {}).items():
        fv = float(v or 0.0)
        if fv <= 0:
            continue
        fam = k if k in ("inf", "art", "support", "aa", "at", "armor", "air") else _prod_family(k)
        out[fam] = out.get(fam, 0.0) + fv
    return out


# ===================================================================== 宏 1：自动产线挪厂配平
def rebalance_production(
    g,
    tag=None,
    dry_run: bool = False,
    target_ratios: Optional[Dict[str, float]] = None,
    extra_demand: Optional[Dict[str, float]] = None,
    min_factories_per_line: int = 1,
) -> Dict[str, Any]:
    """按库存缺口 + 编制需求 + 老手目标比例自动重平衡全部军工产线。

    步骤：
      1. 读全部 CMilitaryProductionLine、精确军工上限与当前装备库存；
      2. 识别低优先轰炸机线（封顶 1 厂）与严重超储产线（如步枪 >2 倍安全线）；
      3. 识别紧缺族（火炮/支援/防空/步枪/装甲库存过低或带 extra_demand 新增缺口）；
      4. 计算守恒的目标厂数分配，确保总和 <= 可用军工、每条有效线 >= min_factories_per_line；
      5. dry_run=False 时先缩减超储线释放军工，再给紧缺线加厂，彻底解决 B-10“无空闲厂加不上去”。
    """
    me = _safe_tag(g, tag)
    try:
        all_lines = g.production_lines(me, include_obsolete=False) or []
    except TypeError:
        all_lines = g.production_lines(me) or []
    mil_lines = [l for l in all_lines if l.get("class") == "CMilitaryProductionLine"]
    if not mil_lines:
        return {"ok": False, "tag": me, "error": "no military production lines found"}

    # 精确总军工与空闲军工
    alloc = {}
    if hasattr(g, "factory_allocation"):
        try:
            alloc = g.factory_allocation(me) or {}
        except Exception:
            alloc = {}
    used_now = sum(int(l.get("factories") or 0) for l in mil_lines)
    if isinstance(alloc, dict) and alloc.get("military_available") is not None:
        total_mil = int(alloc["military_available"])
        idle_now = int(alloc.get("military_idle") or max(0, total_mil - used_now))
    else:
        try:
            total_mil = int(g.country(me).military_factories)
        except Exception:
            total_mil = used_now
        idle_now = max(0, total_mil - used_now)

    # 装备库存与师数
    st_tot: Dict[str, float] = {}
    try:
        st = g.equipment_stockpile(me) or {}
        if isinstance(st.get("totals_by_equipment"), dict) and st["totals_by_equipment"]:
            st_tot = {k: float(v or 0.0) for k, v in st["totals_by_equipment"].items()}
        else:
            for row in st.get("stockpile") or []:
                eq = row.get("equipment")
                if eq:
                    st_tot[eq] = st_tot.get(eq, 0.0) + float(row.get("count") or 0.0)
    except Exception:
        st_tot = {}
    fam_stock = _sum_stockpile_by_family(st_tot)

    try:
        divs_n = int((g.divisions(me, limit=1) or {}).get("count") or 0)
    except Exception:
        divs_n = 0
    divs_eff = max(divs_n, 24)

    # 引入 equipmeta 超储识别与老手剧本规则
    surplus_by_line = {}
    try:
        import equipmeta as _em
        for sr in _em.compute_surplus_lines(all_lines, st_tot, total_mil, divs_n) or []:
            if "line" in sr:
                surplus_by_line[int(sr["line"])] = sr
    except Exception:
        pass

    deprioritize = ("tac_bomber", "strategic_bomber", "heavy_fighter")
    rifle_soft_cap = 20000.0
    pb_ratios = {"inf": 0.22, "art": 0.20, "support": 0.10, "aa": 0.06, "armor": 0.18, "air": 0.24}
    try:
        import playbooks as _pbk
        rules = (_pbk.get_playbook(me) or {}).get("production_rules") or {}
        if rules.get("deprioritize_equipment"):
            deprioritize = tuple(rules["deprioritize_equipment"])
        if rules.get("rifle_stockpile_soft_cap"):
            rifle_soft_cap = float(rules["rifle_stockpile_soft_cap"])
    except Exception:
        pass

    # 若步枪明显超储（绝对库存达标，或步兵装备线已命中 equipmeta.compute_surplus_lines），
    # 压低步枪目标权重并抬高火炮/装甲/空军权重（2026-11-26 A8 统一口径）
    rifles_now = fam_stock.get("inf", 0.0)
    inf_line_surplus = any(
        _prod_family(sr.get("equipment")) == "inf"
        for sr in surplus_by_line.values()
    )
    rifle_surplus = (rifles_now >= max(rifle_soft_cap, divs_eff * 150.0)) or inf_line_surplus
    if target_ratios:
        ratios = {str(k).lower(): float(v) for k, v in target_ratios.items() if float(v) >= 0}
    elif rifle_surplus:
        ratios = {"inf": 0.12, "art": 0.24, "support": 0.11, "aa": 0.07, "armor": 0.22, "air": 0.24}
    else:
        ratios = dict(pb_ratios)

    # 结合当前库存缺口与外部新增缺口（如刚改 9/3 编制后的炮兵缺口）计算各族紧急加权
    ext_dem = _normalize_extra_demand(extra_demand)
    safe_floor = {
        "inf": max(3000.0, divs_eff * 40.0),
        "art": max(400.0, divs_eff * 14.0),
        "support": max(350.0, divs_eff * 10.0),
        "aa": max(150.0, divs_eff * 4.0),
        "armor": max(200.0, divs_eff * 5.0),
    }
    urgency: Dict[str, float] = {}
    missing_line_warnings: List[str] = []
    present_fams = {_prod_family(l.get("equipment")) for l in mil_lines}

    for fam in ("inf", "art", "support", "aa", "armor"):
        cur_s = fam_stock.get(fam, 0.0)
        want_s = safe_floor[fam] + ext_dem.get(fam, 0.0)
        gap = max(0.0, want_s - cur_s)
        if gap > 0:
            # 缺口越大紧急度越高（最多把该族目标权重翻倍+额外倾斜）
            ratio_gap = gap / max(safe_floor[fam], 1.0)
            urgency[fam] = min(2.2, 1.0 + 0.55 * math.log1p(ratio_gap))
            if fam not in present_fams and (ext_dem.get(fam, 0.0) > 0 or fam in ("inf", "art", "support")):
                missing_line_warnings.append(
                    "缺口家族 %s（缺口约 %.0f）当前无对应军工产线，建议用 hoi4_add_production_line 开线"
                    % (fam, gap)
                )
        elif fam == "inf" and rifle_surplus:
            urgency[fam] = 0.55
        else:
            urgency[fam] = 1.0

    # 按实际存在的军工产线归一化分配目标厂数
    min_fl = max(0, int(min_factories_per_line))
    line_meta = []
    for l in mil_lines:
        idx = int(l["index"])
        eq = str(l.get("equipment") or "")
        fam = _prod_family(eq)
        cur_f = int(l.get("factories") or 0)
        is_depr = any(w in eq.lower() for w in deprioritize)
        line_meta.append({
            "line": idx,
            "equipment": eq,
            "family": fam,
            "before": cur_f,
            "deprioritized": is_depr,
            "surplus_info": surplus_by_line.get(idx),
        })

    # 先固定低优先轰炸机线为 <= 1 厂
    fixed_budget = 0
    active_lines = []
    target_map: Dict[int, int] = {}
    for m in line_meta:
        if m["deprioritized"]:
            tf = min(m["before"], 1) if m["before"] > 0 else 0
            target_map[m["line"]] = tf
            fixed_budget += tf
        else:
            active_lines.append(m)

    pool = max(0, total_mil - fixed_budget)
    if not active_lines:
        return {
            "ok": True,
            "tag": me,
            "dry_run": dry_run,
            "total_military_factories": total_mil,
            "idle_before": idle_now,
            "changes": [],
            "note": "only deprioritized lines present",
        }

    # 为每条活跃线先保底 min_fl 厂（若军工总数不够则按 1 或 0 降级）
    eff_min = min_fl if pool >= len(active_lines) * min_fl else (1 if pool >= len(active_lines) else 0)
    for m in active_lines:
        target_map[m["line"]] = eff_min
    rem_pool = pool - eff_min * len(active_lines)

    # 计算每条活跃线的综合权重：
    # 2026-11-23 (复测 10 B-3 逻辑优化): 维持自动比例配平（不放水），但修正空军族内部
    # 均分导致的“战斗机 16 厂被腰斩到 7 厂”问题——战斗机 (fighter) 在空军族内权重高于
    # CAS/其它辅助机种（约 7:3），且提升战斗机制空默认占比，确保主战制空权不被误砍。
    fam_subweight_sum: Dict[str, float] = {}
    line_subweight: Dict[int, float] = {}
    for m in active_lines:
        fam = m["family"]
        eq_l = str(m.get("equipment") or "").lower()
        sw = 1.0
        if fam == "air":
            if "fighter" in eq_l and "heavy_fighter" not in eq_l:
                sw = 2.4
            elif "cas" in eq_l:
                sw = 1.0
            else:
                sw = 0.6
        line_subweight[m["line"]] = sw
        fam_subweight_sum[fam] = fam_subweight_sum.get(fam, 0.0) + sw

    weights: List[Tuple[int, float]] = []
    for m in active_lines:
        fam = m["family"]
        base_w = ratios.get(fam, 0.06)
        # 若为德国等主流陆空强国，默认空军权重略向制空倾斜（0.24 -> 0.28 当未显式覆盖）
        if fam == "air" and not target_ratios and total_mil >= 40:
            base_w = max(base_w, 0.28)
        share = line_subweight[m["line"]] / max(0.01, fam_subweight_sum.get(fam, 1.0))
        w = base_w * urgency.get(fam, 1.0) * share
        # 若某条线本身被 equipmeta 标为严重超储，进一步压权重防回流（2026-11-26 A8 兼容 ratio/surplus_ratio）
        _sinfo = m["surplus_info"] or {}
        _sratio = _sinfo.get("ratio") if _sinfo.get("ratio") is not None else _sinfo.get("surplus_ratio")
        if _sinfo and float(_sratio or 0) >= 2.0:
            w *= 0.45
        weights.append((m["line"], max(0.01, w)))

    w_sum = sum(w for _, w in weights) or 1.0
    # 精确分配剩余军工（最大余数法保证总和 100% 守恒 == pool）
    exact_extra = [(idx, rem_pool * (w / w_sum)) for idx, w in weights]
    floored = {idx: int(math.floor(val)) for idx, val in exact_extra}
    leftover = rem_pool - sum(floored.values())
    remainders = sorted(
        [(val - floored[idx], idx) for idx, val in exact_extra],
        reverse=True,
    )
    for _, idx in remainders[:max(0, leftover)]:
        floored[idx] += 1
    for idx, add_n in floored.items():
        target_map[idx] += add_n

    # 若单个步兵超储线计算后仍高于 suggested_factories 且有其它急缺线，给急缺线再挪一档
    # 生成变更列表：先缩后扩
    changes = []
    for m in line_meta:
        idx = m["line"]
        bf = m["before"]
        af = int(target_map[idx])
        if af == bf:
            continue
        reason_parts = []
        if m["deprioritized"] and af < bf:
            reason_parts.append("压减低优先轰炸机")
        elif m["surplus_info"] and af < bf:
            _si = m["surplus_info"]
            _sr_v = _si.get("ratio") if _si.get("ratio") is not None else _si.get("surplus_ratio")
            reason_parts.append("超储线挪厂(库存率%.1fx)" % float(_sr_v or 0))
        elif m["family"] == "inf" and rifle_surplus and af < bf:
            reason_parts.append("步枪库存%.0f超储抽厂" % rifles_now)
        elif ext_dem.get(m["family"], 0) > 0 and af > bf:
            reason_parts.append("补新编制缺口(+%.0f)" % ext_dem[m["family"]])
        elif urgency.get(m["family"], 1.0) > 1.05 and af > bf:
            reason_parts.append("补紧缺库存(当前%.0f)" % fam_stock.get(m["family"], 0.0))
        else:
            reason_parts.append("按军工比例配平")
        changes.append({
            "line": idx,
            "equipment": m["equipment"],
            "family": m["family"],
            "before": bf,
            "target": af,
            "delta": af - bf,
            "reason": "; ".join(reason_parts),
        })

    shrinks = sorted([c for c in changes if c["delta"] < 0], key=lambda x: x["delta"])
    grows = sorted([c for c in changes if c["delta"] > 0], key=lambda x: -x["delta"])
    ordered_plan = shrinks + grows

    applied = []
    if not dry_run and ordered_plan:
        for ch in ordered_plan:
            try:
                r = g.set_production_amount(ch["line"], ch["target"], tag=me, auto_reclaim=False)
            except TypeError:
                r = g.set_production_amount(ch["line"], ch["target"], tag=me)
            applied.append({
                "line": ch["line"],
                "equipment": ch["equipment"],
                "before": ch["before"],
                "target": ch["target"],
                "after": r.get("after"),
                "ok": bool(r.get("ok")),
                "note": r.get("note") or r.get("error") or "",
            })

    reclaimed_planned = sum(-c["delta"] for c in shrinks)
    allocated_planned = sum(c["delta"] for c in grows)
    return {
        "ok": True if dry_run or not ordered_plan else all(a.get("ok") for a in applied),
        "tag": me,
        "dry_run": bool(dry_run),
        "total_military_factories": total_mil,
        "idle_before": idle_now,
        "rifle_surplus_detected": rifle_surplus,
        "stockpile_by_family": {k: round(v, 1) for k, v in sorted(fam_stock.items())},
        "extra_demand_by_family": {k: round(v, 1) for k, v in sorted(ext_dem.items())},
        "reclaimed_factories": reclaimed_planned,
        "allocated_factories": allocated_planned,
        "plan": ordered_plan,
        "applied": applied,
        "warnings": missing_line_warnings,
    }


# ===================================================================== 宏 2：一键经典编制升级 + 产线联动
def _read_template_comp(g, tpl_name: str, tag: str) -> Dict[str, Any]:
    """兼容真机 `g.template_composition(tag, only=name) -> [dict]` 与离线 mock 返回 dict。"""
    res = None
    try:
        res = g.template_composition(tag, only=tpl_name)
    except TypeError:
        try:
            res = g.template_composition(tpl_name, tag)
        except Exception:
            res = None
    except Exception:
        res = None
    if isinstance(res, list):
        for row in res:
            if isinstance(row, dict) and row.get("name") == tpl_name:
                return row
        for row in res:
            if isinstance(row, dict):
                return row
        return {}
    if isinstance(res, dict):
        return res
    return {}


def _pick_template_for_role(g, tag: str, role: str, template_name: Optional[str] = None) -> Tuple[Optional[str], List[str]]:
    """自动匹配最合适的主力步兵/装甲模板名。"""
    tpls = []
    try:
        tpls = g.division_templates(tag) or []
    except Exception:
        tpls = []
    names = [str(t.get("name") or "") for t in tpls if t.get("name")]
    if template_name:
        if template_name in names:
            return template_name, names
        low = template_name.lower()
        for nm in names:
            if nm.lower() == low or low in nm.lower():
                return nm, names
        return template_name, names

    # 一次性读取全部模板构成（比逐个调用快且兼容真机）
    comp_by_name: Dict[str, Dict[str, Any]] = {}
    try:
        all_c = g.template_composition(tag)
        if isinstance(all_c, list):
            for row in all_c:
                if isinstance(row, dict) and row.get("name"):
                    comp_by_name[row["name"]] = row
    except Exception:
        pass

    # 统计当前在野各模板师数，优先选现役最多的同类模板
    div_counts: Dict[str, int] = {}
    try:
        dres = g.divisions(tag, limit=500) or {}
        for d in dres.get("divisions") or []:
            tn = d.get("template")
            if tn:
                div_counts[tn] = div_counts.get(tn, 0) + 1
    except Exception:
        pass

    scored = []
    for nm in names:
        comp = comp_by_name.get(nm) or _read_template_comp(g, nm, tag)
        line = comp.get("line") or {}
        inf_n = int(line.get("infantry") or 0)
        mot_n = int(line.get("motorized") or 0) + int(line.get("mechanized") or 0)
        arm_n = sum(int(line.get(k) or 0) for k in ("light_armor", "medium_armor", "heavy_armor", "modern_armor"))
        low = nm.lower()
        if role == "infantry":
            # 排除摩托化/装甲/纯骑兵/特种模板（如 "Infanterie-Division (mot.)"）
            if mot_n > 0 or "(mot" in low or "motor" in low:
                continue
            if arm_n > 0 or int(line.get("cavalry") or 0) >= 4:
                continue
            if inf_n <= 0 and not any(k in low for k in ("infanter", "infantr", "rifle")):
                continue
            score = (
                div_counts.get(nm, 0) * 100
                + inf_n * 5
                + (25 if ("infanter" in low or "infantr" in low) else 0)
            )
            scored.append((score, nm))
        elif role == "armor":
            if arm_n <= 0 and not any(k in low for k in ("panzer", "armor", "tank")):
                continue
            score = div_counts.get(nm, 0) * 100 + arm_n * 10 + (20 if "panzer" in low or "armor" in low else 0)
            scored.append((score, nm))
    if scored:
        scored.sort(reverse=True)
        return scored[0][1], names
    return (names[0] if names else None), names


def _estimate_template_xp_cost(before_line: Dict[str, int], before_supp: Dict[str, int],
                               after_line: Dict[str, int], after_supp: Dict[str, int]) -> int:
    """估算 HOI4 改编制陆军经验消耗（常规营增/删 5 XP，新兵种列 +20 XP，支援连 10 XP）。"""
    cost = 0
    for k in set(before_line) | set(after_line):
        b = int(before_line.get(k) or 0)
        a = int(after_line.get(k) or 0)
        if a != b:
            cost += abs(a - b) * 5
            if b == 0 and a > 0:
                cost += 20
    for k in set(before_supp) | set(after_supp):
        b = int(before_supp.get(k) or 0)
        a = int(after_supp.get(k) or 0)
        if a != b:
            cost += abs(a - b) * 10
    return cost


def upgrade_template_preset(
    g,
    preset: str = "inf_9_3",
    template: Optional[str] = None,
    tag=None,
    auto_rebalance: bool = True,
    dry_run: bool = True,
) -> Dict[str, Any]:
    """一键将主力模板升级为老手预设，并自动计算装备缺口与联动挪厂。"""
    me = _safe_tag(g, tag)
    pkey = str(preset or "").strip().lower()
    if pkey not in TEMPLATE_PRESETS:
        return {
            "ok": False,
            "tag": me,
            "error": "unknown preset '%s'" % preset,
            "available_presets": {
                k: {"name": v["name"], "line": v["line"], "support": v["support"]}
                for k, v in TEMPLATE_PRESETS.items()
            },
        }
    spec = TEMPLATE_PRESETS[pkey]
    tpl_name, all_tpls = _pick_template_for_role(g, me, spec["role"], template_name=template)
    if not tpl_name:
        return {"ok": False, "tag": me, "error": "no matching template found", "templates": all_tpls}

    before_comp = _read_template_comp(g, tpl_name, me)
    if not before_comp:
        return {"ok": False, "tag": me, "template": tpl_name, "error": "cannot read template composition"}

    b_line = {k: int(v) for k, v in (before_comp.get("line") or {}).items() if int(v or 0) > 0}
    b_supp = {k: int(v) for k, v in (before_comp.get("support") or {}).items() if int(v or 0) > 0}
    a_line = dict(spec["line"])
    a_supp = dict(spec["support"])

    already_matches = (b_line == a_line and b_supp == a_supp)

    # 读取当前陆军经验
    army_xp = None
    try:
        c = g.country(me)
        xp_d = getattr(c, "experience", None)
        if not isinstance(xp_d, dict) and hasattr(c, "summary"):
            xp_d = (c.summary() or {}).get("experience")
        if isinstance(xp_d, dict) and xp_d.get("army") is not None:
            army_xp = round(float(xp_d["army"]), 1)
    except Exception:
        army_xp = None

    est_xp = 0 if already_matches else _estimate_template_xp_cost(b_line, b_supp, a_line, a_supp)

    # 计算该模板在野师数与全军换装后的装备净缺口
    tpl_divs = 0
    try:
        dres = g.divisions(me, limit=500) or {}
        tpl_divs = sum(1 for d in (dres.get("divisions") or []) if d.get("template") == tpl_name)
    except Exception:
        tpl_divs = 0
    scale_divs = max(tpl_divs, 12)

    eq_delta_per_div: Dict[str, int] = {}
    eq_total_added: Dict[str, int] = {}
    net_deficit: Dict[str, float] = {}
    try:
        import staff_army as _sa
        need_before = _sa.template_needs({"line": b_line, "support": b_supp}).get("equipment") or {}
        need_after = _sa.template_needs({"line": a_line, "support": a_supp}).get("equipment") or {}
        st_tot = (g.equipment_stockpile(me) or {}).get("totals_by_equipment") or {}
        fam_stock = _sum_stockpile_by_family(st_tot)
        for eq in sorted(set(need_before) | set(need_after)):
            diff = int(need_after.get(eq, 0)) - int(need_before.get(eq, 0))
            if diff != 0:
                eq_delta_per_div[eq] = diff
            if diff > 0:
                tot_need = diff * scale_divs
                eq_total_added[eq] = tot_need
                fam = _prod_family(eq)
                cur_have = fam_stock.get(fam, 0.0)
                if tot_need > cur_have:
                    net_deficit[eq] = round(tot_need - cur_have, 1)
    except Exception:
        pass

    out: Dict[str, Any] = {
        "ok": True,
        "tag": me,
        "preset": pkey,
        "preset_name": spec["name"],
        "template": tpl_name,
        "divisions_using_template": tpl_divs,
        "dry_run": bool(dry_run),
        "already_matches": already_matches,
        "before": {"line": b_line, "support": b_supp},
        "target": {"line": a_line, "support": a_supp},
        "army_xp": army_xp,
        "estimated_xp_cost": est_xp,
        "equipment_delta_per_division": eq_delta_per_div,
        "equipment_added_total_est": eq_total_added,
        "net_equipment_deficit_est": net_deficit,
    }

    if already_matches:
        out["note"] = "template '%s' already matches preset '%s'" % (tpl_name, pkey)
        if auto_rebalance and net_deficit:
            out["production_rebalance"] = rebalance_production(
                g, tag=me, dry_run=dry_run, extra_demand=net_deficit
            )
        return out

    if army_xp is not None and army_xp < est_xp:
        out["xp_warning"] = (
            "army XP %.1f may be below estimated cost %d (engine CanExecute will decide exact cost)"
            % (army_xp, est_xp)
        )

    if dry_run:
        if auto_rebalance:
            out["production_rebalance_preview"] = rebalance_production(
                g, tag=me, dry_run=True, extra_demand=net_deficit
            )
        out["note"] = "dry_run preview only; pass dry_run=false to apply template upgrade and rebalance factories"
        return out

    # 真正执行改编制（真机走 templedit.edit_template -> CUpdateDivisionTemplateCommand；
    # 离线单测无 ptr 时回退 g.template_edit）
    tpl_ptr = before_comp.get("ptr")
    if not tpl_ptr:
        try:
            for t_row in (g.division_templates(me) or []):
                if t_row.get("name") == tpl_name and t_row.get("ptr"):
                    tpl_ptr = t_row["ptr"]
                    break
        except Exception:
            tpl_ptr = None

    if tpl_ptr:
        try:
            import templedit as _te
            ptr_int = int(tpl_ptr, 16) if isinstance(tpl_ptr, str) else int(tpl_ptr)
            edit_res = _te.edit_template(g, ptr_int, a_line, False, a_supp)
        except Exception as e:
            edit_res = {"ok": False, "error": repr(e)}
    elif hasattr(g, "template_edit"):
        try:
            edit_res = g.template_edit(
                tpl_name, line=a_line, support=a_supp, dry_run=False, tag=me
            )
        except TypeError:
            edit_res = g.template_edit(tpl_name, line=a_line, support=a_supp, dry_run=False)
        except Exception as e:
            edit_res = {"ok": False, "error": repr(e)}
    else:
        edit_res = {"ok": False, "error": "cannot resolve template pointer for '%s'" % tpl_name}

    out["template_edit"] = edit_res
    if not edit_res.get("ok"):
        out["ok"] = False
        out["error"] = edit_res.get("error") or "template_edit failed"
        return out

    if auto_rebalance:
        out["production_rebalance"] = rebalance_production(
            g, tag=me, dry_run=False, extra_demand=net_deficit
        )
    return out


# ===================================================================== 宏 3：战略多线兵力划分与补给防抽空部署
def plan_front_allocation(
    g,
    target_tag: str,
    tag=None,
    reserve_ratio: float = 0.12,
    min_reserve: int = 4,
    execute: bool = False,
    allow_understrength: bool = False,
) -> Dict[str, Any]:
    """按目标国+保独联军+真实补给上限+强制预备队一次性规划多线兵力（可选落地部署）。

    硬原则：
      1. 自动调用 war_escalation_forecast 查出全部会拉入的接壤敌国（绝不漏掉 FRA/POL 等保独国）；
      2. 强制保留本土战略预备队（默认 12%，至少 4 师，最多 1/3 兵力），杜绝本土真空被偷家；
      3. 先保次要敌国边境最低守备（每省 >=1 师，受真实补给上限封顶），剩余再配给主攻方向；
      4. 主攻方向严禁超过 staff_supply.front_budget 校准后的补给上限；
      5. 总兵力严重不足或战力比 < 0.45 时直接给出 `suicidal` 结论并拦截 `execute=True`。
    """
    me = _safe_tag(g, tag)
    tgt = str(target_tag or "").upper()
    if not tgt:
        return {"ok": False, "error": "missing target_tag"}

    import diplomacy as _dip
    import staff_supply as _ss

    diplo = _dip.war_escalation_forecast(g, tgt, me_tag=me) or {}
    try:
        my_divs = int((g.divisions(me, limit=1) or {}).get("count") or 0)
    except Exception:
        my_divs = 0

    # 收集目标国 + 全部陆路接壤潜在参战国
    primary_bd = _dip.divisions_on_border(g, tgt, me_tag=me)
    primary_provs = int(primary_bd.get("border_provinces") or 0)
    primary_cur = int(primary_bd.get("divisions_on_border") or 0)
    primary_fb = _ss.front_budget(g, tgt, me) or {}
    primary_cap = float(primary_fb.get("capacity") or max(2.0, primary_provs * 2.5))

    sec_fronts = []
    for cb in diplo.get("co_belligerent_borders") or []:
        ftag = str(cb.get("tag") or "").upper()
        if not ftag or ftag == tgt:
            continue
        bprovs = int(cb.get("border_provinces") or 0)
        if bprovs <= 0:
            continue
        fb = _ss.front_budget(g, ftag, me) or {}
        fcap = float(fb.get("capacity") or max(2.0, bprovs * 2.5))
        edivs = int(cb.get("enemy_divisions") or 0)
        # 守备底线：强敌/主要国家至少每省 2 师，中小国每省 1 师，均受真实补给上限约束
        per_tile_min = 2 if (cb.get("is_major") or edivs >= 40) else 1
        min_guard = int(min(math.floor(fcap), max(1, bprovs * per_tile_min)))
        sec_fronts.append({
            "tag": ftag,
            "role": "secondary_defense",
            "border_provinces": bprovs,
            "enemy_divisions": edivs,
            "is_major": bool(cb.get("is_major")),
            "current_divisions_on_border": int(cb.get("divisions_on_border") or 0),
            "supply_capacity": round(fcap, 2),
            "recommended_divisions": min_guard,
            "has_front_line": bool(cb.get("has_front_line")),
            "reasons": cb.get("reasons") or [],
        })

    # 本土战略预备队配额
    r_ratio = max(0.05, min(0.35, float(reserve_ratio)))
    reserve_target = min(my_divs // 3, max(int(min_reserve), int(round(my_divs * r_ratio)))) if my_divs >= 6 else 0
    usable = max(0, my_divs - reserve_target)

    sec_need_total = sum(f["recommended_divisions"] for f in sec_fronts)
    # 主攻方向推荐：不超过真实补给上限，且优先保证 >= 每省 2 师（上限取 floor(primary_cap)）
    primary_cap_int = max(1, int(math.floor(primary_cap))) if primary_provs > 0 else 0
    primary_ideal = min(primary_cap_int, max(primary_provs * 2, int(round(primary_cap_int * 0.9)))) if primary_provs > 0 else 0

    rem_after_sec = max(0, usable - sec_need_total)
    primary_alloc = min(primary_cap_int, rem_after_sec)
    extra_reserve = max(0, rem_after_sec - primary_alloc)
    total_reserve = reserve_target + extra_reserve

    # 汇总最低需求与战力评估
    min_total_required = reserve_target + sec_need_total + min(primary_cap_int, max(primary_provs, 4))
    shortfall = max(0, min_total_required - my_divs)

    total_enemy = int(diplo.get("total_enemy_divisions_est") or 0)
    if total_enemy <= 0:
        try:
            total_enemy = int((g.divisions(tgt, limit=1) or {}).get("count") or 0)
        except Exception:
            total_enemy = 0
    force_ratio = round(my_divs / float(max(1, total_enemy)), 2) if total_enemy > 0 else None

    raw_blockers = list(diplo.get("diplomatic_blockers") or [])
    # 2026-11-26 (B2 根治): 已处于交战态（already at war）不是部署阻断，
    # 不得把战中重新分配兵力一刀切判成 verdict="suicidal"！剥离为 info 标注。
    already_at_war = False
    blockers = []
    notes = []
    for _blk in raw_blockers:
        if str(_blk or "").strip().lower().startswith("already at war"):
            already_at_war = True
            notes.append(str(_blk))
        else:
            blockers.append(_blk)
    warnings = list(diplo.get("diplomatic_warnings") or [])

    if primary_provs <= 0:
        blockers.append("no land border with %s" % tgt)
    if shortfall > 0:
        warnings.append(
            "兵力缺口 %d 师：当前 %d 师，但多线最低守备(%d) + 主攻(%d) + 本土预备队(%d) 至少需 %d 师"
            % (shortfall, my_divs, sec_need_total, min(primary_cap_int, max(primary_provs, 4)),
               reserve_target, min_total_required)
        )
    if force_ratio is not None and force_ratio < 0.45 and total_enemy >= 60:
        warnings.append(
            "战力严重悬殊：%s %d 师 vs 联军合计 %d 师（比值 %.2f），盲目宣战大概率溃败"
            % (me, my_divs, total_enemy, force_ratio)
        )
    for sf in sec_fronts:
        if sf["current_divisions_on_border"] < sf["recommended_divisions"]:
            warnings.append(
                "次要参战国 %s 边境防线偏薄：当前 %d 师，建议至少 %d 师（补给上限 %.1f）"
                % (sf["tag"], sf["current_divisions_on_border"], sf["recommended_divisions"], sf["supply_capacity"])
            )

    if blockers or (force_ratio is not None and force_ratio < 0.45 and total_enemy >= 60) or shortfall > max(6, my_divs * 0.25):
        verdict = "suicidal"
    elif shortfall > 0 or (force_ratio is not None and force_ratio < 0.75) or any(
        sf["current_divisions_on_border"] < sf["recommended_divisions"] for sf in sec_fronts
    ):
        verdict = "risky"
    else:
        verdict = "ready"

    fronts = [
        {
            "tag": tgt,
            "role": "primary_offense",
            "border_provinces": primary_provs,
            "current_divisions_on_border": primary_cur,
            "supply_capacity": round(primary_cap, 2),
            "ideal_divisions": primary_ideal,
            "allocated_divisions": primary_alloc,
            "measured_capped_provinces": primary_fb.get("measured_capped_provinces") or [],
        }
    ] + sec_fronts

    as_of_date = None
    try:
        _d_now = g.date() or {}
        as_of_date = _d_now.get("text") or (
            "%04d.%02d.%02d" % (int(_d_now["year"]), int(_d_now["month"]), int(_d_now["day"]))
            if all(k in _d_now for k in ("year", "month", "day")) else None
        )
    except Exception:
        as_of_date = None

    out: Dict[str, Any] = {
        "ok": True,
        "tag": me,
        "target_tag": tgt,
        "as_of_date": as_of_date,
        "snapshot_note": (
            "本评估为当前日期(%s)的即时快照；外交保独(guaranteed_by)与阵营联军规模"
            "会随世界紧张度升高动态变化，宣战前务必重新运行 hoi4_plan_front_allocation "
            "或 hoi4_war_readiness 复核。" % (as_of_date or "unknown")
        ),
        "verdict": verdict,
        "my_divisions": my_divs,
        "total_enemy_divisions_est": total_enemy,
        "force_ratio": force_ratio,
        "shortfall_divisions": shortfall,
        "reserve_divisions": total_reserve,
        "reserve_ratio_actual": round(total_reserve / float(max(1, my_divs)), 2),
        "fronts": fronts,
        "co_belligerents": diplo.get("co_belligerents") or [],
        "guaranteed_by": diplo.get("guaranteed_by") or [],
        "already_at_war": already_at_war,
        "blockers": blockers,
        "warnings": warnings,
        "notes": notes,
        "executed": False,
    }

    if execute:
        if verdict == "suicidal" and not allow_understrength:
            out["ok"] = False
            out["blocked_by"] = "suicidal_allocation"
            out["error"] = (
                "拒绝执行部署：当前评估为 suicidal（我方 %d 师 vs 联军 %d 师，缺口 %d 师）。"
                "请先用 hoi4_upgrade_template_preset 提升师质量并扩军，或显式传 allow_understrength=true。"
                % (my_divs, total_enemy, shortfall)
            )
            return out
        # 落地部署主攻方向，严格透传主攻补给预算 (primary_alloc) 与本土预备队配额 (total_reserve)，
        # 并自动保护所有次要参战国边境不被抽空
        protect_tags = [sf["tag"] for sf in sec_fronts]
        try:
            try:
                dep_res = g.deploy_to_border(
                    tgt,
                    tag=me,
                    protect_borders=protect_tags,
                    min_other_garrison=2 if any(sf["is_major"] for sf in sec_fronts) else 1,
                    max_divisions=primary_alloc,
                    reserve_divisions=total_reserve,
                )
            except TypeError:
                dep_res = g.deploy_to_border(
                    tgt,
                    tag=me,
                    protect_borders=protect_tags,
                    min_other_garrison=2 if any(sf["is_major"] for sf in sec_fronts) else 1,
                )
        except Exception as e:
            dep_res = {"ok": False, "error": repr(e)}
        out["executed"] = True
        out["deploy_result"] = dep_res
        out["ok"] = bool(dep_res.get("ok"))
    return out


# ===================================================================== 战略体检红字看板
def strategic_health_board(g, tag=None) -> Dict[str, Any]:
    """纯只读老手战略体检看板（嵌入回合简报顶部与 overview），直接把隐性胜负手标红。"""
    me = _safe_tag(g, tag)
    alerts: List[Dict[str, Any]] = []
    metrics: Dict[str, Any] = {"tag": me}

    try:
        d = g.date() or {}
        year = int(d.get("year") or 1936)
    except Exception:
        year = 1936
    metrics["year"] = year

    # 1. 师数与年份曲线体检
    divs_n = 0
    div_rows = []
    try:
        dres = g.divisions(me, limit=500) or {}
        divs_n = int(dres.get("count") or 0)
        div_rows = dres.get("divisions") or []
    except Exception:
        pass
    metrics["divisions"] = divs_n

    target_divs = None
    try:
        import playbooks as _pbk
        pb = _pbk.get_playbook(me) or {}
        tmap = pb.get("target_divisions_by_year") or {}
        if year in tmap:
            target_divs = int(tmap[year])
        elif str(year) in tmap:
            target_divs = int(tmap[str(year)])
    except Exception:
        pass
    metrics["target_divisions_for_year"] = target_divs
    if target_divs and divs_n < int(target_divs * 0.80):
        alerts.append({
            "code": "ARMY_UNDER_TARGET",
            "severity": "warn",
            "title": "扩军落后于 %d 年目标：当前 %d 师 / 目标 %d 师（缺 %d 师）"
                     % (year, divs_n, target_divs, target_divs - divs_n),
            "action": "确保步枪/火炮产线充足后持续排训练队列扩军，切勿弱兵硬开多线战争",
        })

    # 2. 主力步兵编制质量 & 陆军 XP 闲置体检
    army_xp = 0.0
    try:
        c = g.country(me)
        xp_d = getattr(c, "experience", None)
        if not isinstance(xp_d, dict) and hasattr(c, "summary"):
            xp_d = (c.summary() or {}).get("experience")
        if isinstance(xp_d, dict):
            army_xp = float(xp_d.get("army") or 0.0)
    except Exception:
        army_xp = 0.0
    metrics["army_xp"] = round(army_xp, 1)

    main_inf_tpl, _ = _pick_template_for_role(g, me, "infantry")
    if main_inf_tpl:
        try:
            comp = _read_template_comp(g, main_inf_tpl, me)
            line = comp.get("line") or {}
            supp = comp.get("support") or {}
            inf_n = int(line.get("infantry") or 0)
            art_n = int(line.get("artillery_brigade") or 0)
            has_eng = int(supp.get("engineer") or 0) > 0
            has_sart = int(supp.get("artillery") or 0) > 0
            metrics["main_infantry_template"] = {
                "name": main_inf_tpl,
                "infantry": inf_n,
                "artillery_brigade": art_n,
                "support": sorted(k for k, v in supp.items() if int(v or 0) > 0),
            }
            if art_n == 0 and army_xp >= 35.0:
                rec_preset = "inf_9_3" if army_xp >= 45.0 else "inf_9_1"
                alerts.append({
                    "code": "WEAK_INFANTRY_TEMPLATE",
                    "severity": "high",
                    "title": "主力步兵 `%s` 仍是 %d步0炮弱编（陆军XP=%.0f 闲置！）"
                             % (main_inf_tpl, inf_n, army_xp),
                    "action": "建议立即调 hoi4_upgrade_template_preset(preset='%s', auto_rebalance=true, dry_run=false) 一步换编并自动挪厂"
                              % rec_preset,
                })
            elif (not has_eng or not has_sart) and army_xp >= 25.0:
                alerts.append({
                    "code": "MISSING_SUPPORT_COMPANIES",
                    "severity": "warn",
                    "title": "主力步兵 `%s` 缺少关键支援连(工兵/炮兵)，陆军XP=%.0f"
                             % (main_inf_tpl, army_xp),
                    "action": "调 hoi4_upgrade_template_preset(preset='inf_9_3', auto_rebalance=true, dry_run=true) 查看升级方案",
                })
        except Exception:
            pass

    # 3. 装备库存与产线倒挂体检（如步枪超储占大厂 vs 火炮归零）
    try:
        st_tot = (g.equipment_stockpile(me) or {}).get("totals_by_equipment") or {}
        fam_stock = _sum_stockpile_by_family(st_tot)
        try:
            pls = g.production_lines(me, include_obsolete=False) or []
        except TypeError:
            pls = g.production_lines(me) or []
        mil_lines = [l for l in pls if l.get("class") == "CMilitaryProductionLine"]
        inf_fac = sum(int(l.get("factories") or 0) for l in mil_lines if _prod_family(l.get("equipment")) == "inf")
        art_fac = sum(int(l.get("factories") or 0) for l in mil_lines if _prod_family(l.get("equipment")) == "art")
        rifles = fam_stock.get("inf", 0.0)
        arts = fam_stock.get("art", 0.0)
        metrics["key_stockpile"] = {"inf": round(rifles, 0), "art": round(arts, 0),
                                    "support": round(fam_stock.get("support", 0.0), 0)}
        metrics["key_factories"] = {"inf": inf_fac, "art": art_fac}
        if rifles >= 15000 and inf_fac >= 10 and (arts < 400 or art_fac < 6):
            alerts.append({
                "code": "PRODUCTION_IMBALANCE",
                "severity": "high",
                "title": "产能严重倒挂：步枪积压 %.0f 仍占 %d 厂，火炮库存仅 %.0f（%d 厂）"
                         % (rifles, inf_fac, arts, art_fac),
                "action": "立即调 hoi4_rebalance_production(dry_run=false) 一键从超储步枪线抽厂补火炮/支援/空军",
            })
    except Exception:
        pass

    # 4. 全国断补体检（纯读 division_detail，发现断补师立刻标红）
    try:
        import staff_supply as _ss
        ld = _ss.load(g, me, limit=300) or {}
        oos_divs = sum(int(r.get("n") or 0) for r in ld.values() if (r.get("oos_days") or 0) >= 2)
        oos_provs = sum(1 for r in ld.values() if (r.get("oos_days") or 0) >= 2)
        metrics["out_of_supply_divisions"] = oos_divs
        metrics["out_of_supply_provinces"] = oos_provs
        if oos_divs >= 3:
            alerts.append({
                "code": "SUPPLY_CRISIS",
                "severity": "high",
                "title": "前线断补危机：%d 个师（%d 省）已断补 >=2 天，组织度将暴跌"
                         % (oos_divs, oos_provs),
                "action": "不要继续往前线塞师！调 hoi4_plan_front_allocation 查看真实补给上限并把多余师撤作后方预备队",
            })
    except Exception:
        pass

    return {"ok": True, "tag": me, "metrics": metrics, "alerts": alerts}


def format_health_board_markdown(board: Dict[str, Any]) -> List[str]:
    """把战略体检看板格式化为紧凑 Markdown 行列表（供 turnengine 简报置顶）。"""
    alerts = (board or {}).get("alerts") or []
    metrics = (board or {}).get("metrics") or {}
    if not alerts and not metrics.get("main_infantry_template"):
        return []
    out = ["## 战略体检看板 (Staff Health)"]
    mt = metrics.get("main_infantry_template")
    if mt:
        out.append(
            "- 主力步兵 `%s`: %d步%d炮 (支援: %s) | 陆军XP: %s | 师数: %s%s"
            % (
                mt.get("name"), mt.get("infantry", 0), mt.get("artillery_brigade", 0),
                ",".join(mt.get("support") or []) or "无",
                metrics.get("army_xp", "?"),
                metrics.get("divisions", "?"),
                ("/目标%s" % metrics["target_divisions_for_year"]) if metrics.get("target_divisions_for_year") else "",
            )
        )
    for al in alerts[:5]:
        sev = "[红灯]" if al.get("severity") == "high" else "[提示]"
        out.append("- %s `%s`: %s" % (sev, al.get("code"), al.get("title")))
        if al.get("action"):
            out.append("  -> 建议: %s" % al["action"])
    return out
