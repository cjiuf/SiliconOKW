# -*- coding: utf-8 -*-
"""Standing Orders v1（前期运营自动驾驶引擎，PVE 优先，内核地板与策略解耦）。

核心架构（声明 -> 编译 -> 执行 三段分离）：
  1. 显式谓词树（Explicit Predicate Tree）：
     仅接受 `all` / `any` / `not` / `always` 组合节点与单键可观测原子节点，
     严禁 `or_*` 等隐式键名，杜绝静默误判。
  2. 可审计策略注册表（Strategy Registry）：
     所有选州、科研兜底、产线配比、产线换代、PP 议程、编组、选将与事件放行
     公式集中在 `STRATEGY_REGISTRY`，可通过 `strategy_catalog()` 查询，
     禁止在 profile 里混入裸布尔或未登记的魔法策略。
  3. 不可关闭的内核熔断地板（Kernel Floor）：
     `wargoal_or_cb`、`war_or_peace`、`decisive_event`、`pp_reserve_breach`、
     `external_law_or_regime_change`、`max_autopilot_days`、`heartbeat_days`、
     `no_progress` 写死在引擎中，profile 只能收紧/追加（`interrupts_extra`），
     企图关闭地板会在 `compile_profile()` 阶段直接被拒绝。
  4. 阶段机管全局（Phase Machine + sticky）：
     支持单阶段简洁写法或多阶段 `phases` + `defaults` 增量覆盖；进入
     `sticky: true` 阶段后永不回退，避免条件边界抖动。
  5. 不可逆动作硬护栏：
     永不自动宣战/进阵营/改国体/走排他性内战国策、永不花破 `reserve_pp` 预算锁、
     永不把产线压低到 `min_factories` 底线以下。
"""
from __future__ import annotations

import copy
import json
import os
import sys
import threading
import time

import session_state as _SS

STATE_PATH = _SS.state_path(_SS.AUTOPILOT_STATE_NAME)

# ===================================================================== 常量与内核地板
DEFAULT_MAX_AUTOPILOT_DAYS = 180
MIN_MAX_AUTOPILOT_DAYS = 7
MAX_MAX_AUTOPILOT_DAYS = 365

DEFAULT_HEARTBEAT_DAYS = 90
MIN_HEARTBEAT_DAYS = 7
MAX_HEARTBEAT_DAYS = 180

DEFAULT_NO_PROGRESS_LIMIT = 3
MIN_NO_PROGRESS_LIMIT = 2
MAX_NO_PROGRESS_LIMIT = 10

MAX_JOURNAL_ENTRIES = 80

# 不可关闭的内核熔断地板清单（profile 严禁关闭）
KERNEL_FLOOR_RULES = (
    "wargoal_or_cb",
    "war_or_peace",
    "decisive_event",
    "pp_reserve_breach",
    "external_law_or_regime_change",
    "max_autopilot_days",
    "heartbeat_days",
    "no_progress",
)

# 不可逆/政体剧变国策关键字黑名单（除非 profile 显式 allow_exclusive_focus=True 且在 must 中指定）
IRREVERSIBLE_FOCUS_KEYWORDS = (
    "oppose_hitler",
    "civil_war",
    "abdicate",
    "surrender",
    "capitulate",
    "concede_",
    "abandon_",
)

# 核心法案槽位集合（用于检测非本引擎触发的外部法案篡改）
CORE_LAW_SLOTS = frozenset({"economy", "mobilization_laws", "trade_laws"})

# 国策 tier_score 默认优先级与合法层级清单（E5 硬护栏：禁止静默排除 S0_snowball）
DEFAULT_FOCUS_TIER_PREFERENCE = ("S0_playbook", "S0_snowball", "S0_opening", "S_historical")
_VALID_FOCUS_TIERS = frozenset({
    "S0_playbook", "S0_snowball", "S0_opening", "S_historical",
    "A_research_slot", "A_annex", "B_industry", "C_air_naval", "D_other", "Z_filler",
})

# 合法大方向学说 -> 所属军种（与 ResearchMixin.DOCTRINE_LOC 严格对齐）
_DOCTRINE_SERVICE = {
    "new_mobile_warfare": "army",
    "mobile_warfare": "army",
    "superior_firepower": "army",
    "grand_battleplan": "army",
    "mass_assault": "army",
    "special_forces_quantity": "army",
    "special_forces_quality": "army",
    "fleet_in_being": "navy",
    "convoy_raiding": "navy",
    "base_strike": "navy",
    "battlefield_support": "air",
    "operational_integrity": "air",
    "strategic_destruction": "air",
}

# 允许的 profile 顶层键与阶段内配置键
_TOP_LEVEL_KEYS = frozenset({
    "name", "tag", "description", "version",
    "kernel_floor", "defaults", "phases",
    # 单阶段简洁写法允许直接写在顶层的子系统键
    "focus", "research", "construction", "production",
    "pp_policy", "xp_policy", "divisions", "events", "fuel", "interrupts_extra",
})

_STAGE_PROFILE_KEYS = frozenset({
    "focus", "research", "construction", "production",
    "pp_policy", "xp_policy", "divisions", "events", "fuel", "interrupts_extra",
})

# 各子系统允许键白名单（防拼写错误被静默吞掉）
_KERNEL_FLOOR_ALLOWED_KEYS = frozenset({
    "max_autopilot_days", "heartbeat_days", "no_progress_limit",
}) | frozenset(KERNEL_FLOOR_RULES)

_INTERRUPTS_EXTRA_ALLOWED_KEYS = frozenset({
    "on_phase_change",
    "on_focus_queue_empty",
    "on_research_queue_empty",
    "on_construction_queue_empty",
    "on_construction_queue_below",
    "on_pp_near_cap",
    "on_manpower_below",
    "custom_when",
}) | frozenset(KERNEL_FLOOR_RULES)

_FOCUS_ALLOWED_KEYS = frozenset({
    "enabled", "must", "queue", "avoid", "on_blocked",
    "fallback", "tier_preference", "allow_exclusive_focus", "allow_skip_snowball",
})
_RESEARCH_ALLOWED_KEYS = frozenset({
    "enabled", "queue", "never", "max_ahead_years", "fallback_strategy",
})
_CONSTRUCTION_ALLOWED_KEYS = frozenset({
    "enabled", "target_queue_len", "state_strategy", "queue", "rules",
    "count_existing_in_queue",
})
_CONSTRUCTION_QUEUE_ITEM_KEYS = frozenset({
    "id", "building", "count", "state", "when", "until",
})
_CONSTRUCTION_RULE_KEYS = frozenset({
    "building", "when", "until", "max_in_queue",
})
_PRODUCTION_ALLOWED_KEYS = frozenset({
    "enabled", "strategy", "upgrade_strategy",
})
_PP_POLICY_ALLOWED_KEYS = frozenset({
    "enabled", "reserve_pp", "strategy", "agenda",
    "auto_fallback_ideas", "on_agenda_done",
})
_PP_AGENDA_ITEM_KEYS = frozenset({
    "kind", "key", "slot", "cost", "when",
})
_XP_POLICY_ALLOWED_KEYS = frozenset({
    "enabled", "army_template_upgrades", "doctrines",
    "wake_on_weak_template_xp", "wake_on_xp_cap",
})
_XP_TEMPLATE_ITEM_KEYS = frozenset({
    "preset", "template", "min_xp", "auto_rebalance", "when",
})
_XP_DOCTRINE_ITEM_KEYS = frozenset({
    "doctrine", "min_xp", "when",
})
_DIVISIONS_ALLOWED_KEYS = frozenset({
    "enabled", "auto_deploy", "assign_strategy", "general_strategy",
})
_EVENTS_ALLOWED_KEYS = frozenset({"strategy"})
_FUEL_ALLOWED_KEYS = frozenset({
    "peacetime_mode", "critical_threshold", "wake_on_critical",
})


def _check_unknown_keys(raw_dict, allowed, path, errors, skip=()):
    """拒绝任何未登记字段，杜绝拼写错误或废弃键被静默忽略。"""
    unknown = sorted(set(raw_dict.keys()) - set(allowed) - set(skip))
    if unknown:
        errors.append(
            "%s 含未知键 %s（允许键: %s）"
            % (path, ", ".join(repr(k) for k in unknown), ", ".join(sorted(allowed)))
        )

# ===================================================================== 策略注册表
STRATEGY_REGISTRY = {
    "construction_state": {
        "infra_then_slots": {
            "description": "硬前提 slots_free >= min_slots，优先高基础设施（HOI4 每级基建 +20% 建造速度），次按空槽降序。",
            "formula": "filter(slots_free >= min_slots, !excluded) -> sort(-infra, -slots_free, state_id)",
            "params": {
                "min_slots": {"type": "int", "min": 1, "max": 25, "default": 1},
                "exclude_states": {"type": "list_int", "default": []},
                "core_only": {"type": "bool", "default": False},
            },
        },
        "max_free_slots": {
            "description": "硬前提 slots_free >= min_slots，优先空槽最多的州，次按基建降序。",
            "formula": "filter(slots_free >= min_slots, !excluded) -> sort(-slots_free, -infra, state_id)",
            "params": {
                "min_slots": {"type": "int", "min": 1, "max": 25, "default": 1},
                "exclude_states": {"type": "list_int", "default": []},
                "core_only": {"type": "bool", "default": False},
            },
        },
        "explicit": {
            "description": "严格按调用方给定的州 ID 顺序选址（自动跳过满槽州）。",
            "formula": "for sid in states: pick if slots_free >= min_slots",
            "params": {
                "states": {"type": "list_int", "required": True},
                "min_slots": {"type": "int", "min": 1, "max": 25, "default": 1},
            },
        },
    },
    "research_fallback": {
        "none": {
            "description": "科研队列耗尽时不自动选填，保持空槽并唤醒模型决策。",
            "formula": "if queue exhausted -> wake",
            "params": {},
        },
        "on_time_score": {
            "description": "在 ahead_years <= max_ahead_years 且不在 never / XOR 冲突的可研科技中，按引擎综合评分择优。",
            "formula": "filter(ahead_years <= max_ahead_years, !never, !xor_conflict) -> argmax(score, -start_year)",
            "params": {
                "prefer_categories": {"type": "list_str", "default": []},
            },
        },
        "industry_first": {
            "description": "严格不超前（<= max_ahead_years），按 工业 > 电子 > 步兵 > 火炮 > 支援 > 装甲 > 空军 顺序补齐空槽。",
            "formula": "filter(ahead_years <= max_ahead_years, !never, !xor_conflict) -> sort(cat_rank, ahead_years, -score)",
            "params": {},
        },
    },
    "production_alloc": {
        "ratio_hysteresis": {
            "description": "按给定大类比例（inf/art/armor/air）与滞回阈值分配空闲军工，保护 min_factories 底线；空闲军工为 0 时支持按冷却期从严重超储产线（如步枪 >=2.5x 安全线）受控抽厂补紧缺线。",
            "formula": "shortfall(line) = total_mil * ratio[group] * line_share - cur_factories; assign idle; if idle==0 and rebalance_surplus and cooldown passed: reclaim from surplus(>=threshold, >min_factories) -> feed shortfall lines",
            "params": {
                "ratios": {"type": "ratio_map", "default": {"inf": 0.34, "art": 0.21, "armor": 0.20, "air": 0.25}},
                "hysteresis_factories": {"type": "int", "min": 0, "max": 10, "default": 1},
                "min_factories": {"type": "int_map", "default": {}},
                "allocate_dockyards": {"type": "bool", "default": True},
                "rebalance_surplus": {"type": "bool", "default": True},
                "surplus_ratio_threshold": {"type": "float", "min": 1.5, "max": 10.0, "default": 2.5},
                "rebalance_cooldown_days": {"type": "int", "min": 14, "max": 365, "default": 60},
            },
        },
        "keep_current": {
            "description": "不改变现有配比，仅保留当前分配（可选仅分船坞或按换代策略换装）。",
            "formula": "no-op on military ratios; optional dockyard fill",
            "params": {
                "allocate_dockyards": {"type": "bool", "default": True},
            },
        },
    },
    "production_upgrade": {
        "payback": {
            "description": "仅对同 archetype 且回本天数 <= max_payback_days 的过时产线换代，库存低于安全天数时跳过换装保护前线补给。",
            "formula": "upgrade iff obsolete and payback_days <= max_payback_days and stock_days >= min_stockpile_days",
            "params": {
                "max_payback_days": {"type": "float", "min": 1.0, "max": 180.0, "default": 45.0},
                "min_stockpile_days": {"type": "float", "min": 0.0, "max": 365.0, "default": 30.0},
            },
        },
        "never": {
            "description": "绝不自动切换产线装备型号，仅记录过时产线交由模型处理。",
            "formula": "no-op",
            "params": {},
        },
    },
    "pp_agenda": {
        "strict_agenda": {
            "description": "严格按 agenda 声明顺序逐项购买，每项购买前强制校验 pp - cost >= reserve_pp；前一项未完成绝不越过。",
            "formula": "pick first pending item; execute iff when_ok and pp - cost >= reserve_pp",
            "params": {},
        },
        "greedy_agenda": {
            "description": "遍历 agenda 跳过暂不满足 when 或前置条件的项，购买首个可负担且满足 reserve_pp 的项。",
            "formula": "pick first pending item with when_ok and available and pp - cost >= reserve_pp",
            "params": {},
        },
    },
    "division_assign": {
        "merge_existing_then_new": {
            "description": "自动跳过伞兵，按 role_rules 匹配模板关键字，优先并入未满编（< max_per_army）既有集团军，满编则建新军。",
            "formula": "filter(!paradrop) -> match role_rules -> merge army(divisions < max_per_army) else create_army",
            "params": {
                "max_per_army": {"type": "int", "min": 1, "max": 24, "default": 24},
                "role_rules": {"type": "list_dict", "default": []},
            },
        },
        "single_pool": {
            "description": "自动跳过伞兵，所有新下地陆军师统一并入未满编既有集团军，满编则建新军。",
            "formula": "filter(!paradrop) -> merge first non-paradrop army(divisions < max_per_army) else create_army",
            "params": {
                "max_per_army": {"type": "int", "min": 1, "max": 24, "default": 24},
            },
        },
    },
    "general_assign": {
        "trait_then_skill": {
            "description": "按 prefer_traits 命中数降序、次按加权四维技能降序筛选空闲军长（支持 min_skill 门槛与按角色指定特质）。",
            "formula": "filter(available, rank==corps_commander, skill>=min_skill) -> argmax(trait_hits, weighted_skill)",
            "params": {
                "prefer_traits": {"type": "list_str", "default": []},
                "role_prefer_traits": {"type": "str_list_map", "default": {}},
                "min_skill": {"type": "int", "min": 0, "max": 10, "default": 0},
                "weights": {
                    "type": "float_map",
                    "default": {"attack": 1.0, "defense": 1.0, "planning": 0.8, "logistics": 0.8},
                },
            },
        },
        "highest_total_skill": {
            "description": "按攻防计划后勤四维总和降序指派空闲军长。",
            "formula": "filter(available, rank==corps_commander, skill>=min_skill) -> argmax(attack+defense+planning+logistics)",
            "params": {
                "min_skill": {"type": "int", "min": 0, "max": 10, "default": 0},
            },
        },
        "none": {
            "description": "不自动分配将领，留给模型手选。",
            "formula": "no-op",
            "params": {},
        },
    },
    "event_handling": {
        "auto_trivial": {
            "description": "仅自动放行「单一适用选项」或「全适用选项无实质效果差异的纯新闻事件」（绝不触碰 unresolved_scope）；有真实分歧事件立即熔断停表。",
            "formula": "auto iff !unresolved_scope and (single_applicable_option or equivalent_news_options); else KERNEL_FLOOR(decisive_event)",
            "params": {},
        },
        "wake_all": {
            "description": "任何待决事件均不自动处理，立即触发内核熔断唤醒模型。",
            "formula": "always KERNEL_FLOOR(decisive_event)",
            "params": {},
        },
    },
}


def strategy_catalog(domain=None):
    """返回所有已注册策略的语义、公式与参数规范（供 hoi4_inspect(strategy_catalog) 调用）。"""
    if domain:
        d = str(domain).strip().lower()
        if d not in STRATEGY_REGISTRY:
            return {
                "ok": False,
                "error": "未知策略域 %r（可用域: %s）" % (domain, ", ".join(sorted(STRATEGY_REGISTRY))),
                "domains": sorted(STRATEGY_REGISTRY),
            }
        return {"ok": True, "domain": d, "strategies": copy.deepcopy(STRATEGY_REGISTRY[d])}
    return {
        "ok": True,
        "domains": sorted(STRATEGY_REGISTRY),
        "kernel_floor": list(KERNEL_FLOOR_RULES),
        "strategies": copy.deepcopy(STRATEGY_REGISTRY),
    }


# ===================================================================== 会话状态存储
_LOCK = threading.RLock()
_MEM_STATES = {}  # (pid, tag) -> state dict
_PERSIST_LOADED = False


def _clear_autopilot_caches():
    global _PERSIST_LOADED
    with _LOCK:
        _MEM_STATES.clear()
        _PERSIST_LOADED = False


_SS.register_cache("autopilot._MEM_STATES", _clear_autopilot_caches)


def _state_key(g, tag=None):
    pid = getattr(getattr(g, "p", None), "pid", None)
    t = tag
    if not t:
        try:
            t = g.player_tag()
        except Exception:
            t = None
    return (int(pid) if pid is not None else 0, str(t or "?").upper())


def _safe_date(g):
    try:
        d = g.date() or {}
        if isinstance(d, dict):
            return {
                "year": int(d.get("year") or 1936),
                "month": int(d.get("month") or 1),
                "day": int(d.get("day") or 1),
                "raw": d.get("raw"),
                "text": d.get("text"),
            }
    except Exception:
        pass
    raw = None
    try:
        raw = g.date_raw()
    except Exception:
        raw = None
    return {"year": 1936, "month": 1, "day": 1, "raw": raw, "text": None}


def _load_persisted():
    global _PERSIST_LOADED
    if _PERSIST_LOADED:
        return
    _PERSIST_LOADED = True
    if not os.path.isfile(STATE_PATH):
        return
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        if not isinstance(raw, dict):
            return
        for k_str, st in (raw.get("states") or {}).items():
            if not isinstance(st, dict) or ":" not in k_str:
                continue
            pid_s, tag = k_str.split(":", 1)
            try:
                key = (int(pid_s), tag.upper())
            except ValueError:
                continue
            _MEM_STATES[key] = st
    except Exception:
        pass


def _save_persisted():
    try:
        payload = {"version": 1, "states": {}}
        for (pid, tag), st in _MEM_STATES.items():
            payload["states"]["%d:%s" % (pid, tag)] = st
        _SS.ensure_parent(STATE_PATH)
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, STATE_PATH)
    except Exception:
        pass


def _check_session_continuity(g, st):
    """检测是否换档/日期倒退；若已跨局则自动作废旧 Standing Orders。"""
    if not st or not st.get("active"):
        return False
    cur_date = _safe_date(g)
    cur_raw = cur_date.get("raw")
    last_raw = st.get("last_tick_date_raw") or st.get("started_date_raw")
    if cur_raw is not None and last_raw is not None:
        if int(cur_raw) < int(last_raw) or (int(cur_raw) - int(last_raw)) > 365 * 24:
            st["active"] = False
            st["deactivated_reason"] = "save_date_discontinuity"
            _save_persisted()
            return False
    return True


def get_active_state(g, tag=None):
    """获取当前会话生效中的 Standing Orders 状态，无激活则返回 None。"""
    _SS.sync(g)
    with _LOCK:
        _load_persisted()
        k = _state_key(g, tag=tag)
        st = _MEM_STATES.get(k)
        if st is None:
            return None
        if not _check_session_continuity(g, st):
            return None
        return st


def is_active(g, tag=None):
    """判断当前进程/国家是否已激活 Standing Orders。"""
    return get_active_state(g, tag=tag) is not None


def _append_journal(st, date_text, phase, domain, action, ok=True, detail=None):
    rec = {
        "date": date_text,
        "phase": phase,
        "domain": domain,
        "action": action,
        "ok": bool(ok),
    }
    if detail is not None:
        rec["detail"] = detail
    j = st.setdefault("journal", [])
    j.append(rec)
    if len(j) > MAX_JOURNAL_ENTRIES:
        del j[:-MAX_JOURNAL_ENTRIES]
    return rec


# ===================================================================== 显式谓词树
_PRED_ATOMIC_SPECS = {
    "always": "bool",
    "at_war": "bool",
    "date_ge": "date_tuple",
    "date_lt": "date_tuple",
    "civ_ge": "nonneg_int",
    "civ_lt": "nonneg_int",
    "mil_ge": "nonneg_int",
    "mil_lt": "nonneg_int",
    "dock_ge": "nonneg_int",
    "pp_ge": "number",
    "pp_lt": "number",
    "war_support_ge": "unit_float",
    "stability_ge": "unit_float",
    "world_tension_ge": "unit_float",
    "manpower_ge": "nonneg_int",
    "manpower_lt": "nonneg_int",
    "divisions_ge": "nonneg_int",
    "divisions_lt": "nonneg_int",
    "fuel_ge": "number",
    "fuel_lt": "number",
    "army_xp_ge": "number",
    "navy_xp_ge": "number",
    "air_xp_ge": "number",
    "focus_completed": "str",
    "tech_researched": "str",
    "idea_active": "str",
    "enemy_exists": "str",
}

_PRED_COMPOUND_KEYS = frozenset({"all", "any", "not"})


def _check_date_tuple(val):
    if not isinstance(val, (list, tuple)) or len(val) not in (2, 3):
        return False
    if not all(isinstance(x, int) and not isinstance(x, bool) for x in val):
        return False
    yr, mo = val[0], val[1]
    day = val[2] if len(val) == 3 else 1
    return (1900 <= yr <= 2100) and (1 <= mo <= 12) and (1 <= day <= 31)


def validate_predicate(node, path="predicate", errors=None, warnings=None,
                       observable_tension=True):
    """递归静态校验显式谓词树，坚决拒绝隐式 and/or 与 `or_*` 键名。"""
    if errors is None:
        errors = []
    if warnings is None:
        warnings = []
    if not isinstance(node, dict) or not node:
        errors.append("%s 必须是非空 JSON 对象" % path)
        return errors, warnings
    if len(node) > 1:
        bad_or = [k for k in node if str(k).startswith(("or_", "and_"))]
        if bad_or:
            errors.append(
                "%s 检测到禁止的隐式连接键 %s；必须使用显式谓词树 {\"any\": [...]} / {\"all\": [...]}"
                % (path, bad_or)
            )
        else:
            errors.append(
                "%s 包含多个并列键 %s（隐式 AND/OR 语义模糊）；请显式包裹在 {\"all\": [...]} 或 {\"any\": [...]}"
                % (path, sorted(node.keys()))
            )
        return errors, warnings

    key, val = next(iter(node.items()))
    if str(key).startswith(("or_", "and_")):
        errors.append(
            "%s 检测到禁止的隐式连接键 %r；必须改用显式 {\"any\": [...]} / {\"all\": [...]}"
            % (path, key)
        )
        return errors, warnings

    if key in ("all", "any"):
        if not isinstance(val, list) or not val:
            errors.append("%s.%s 必须是非空谓词列表" % (path, key))
            return errors, warnings
        for i, child in enumerate(val):
            validate_predicate(
                child, "%s.%s[%d]" % (path, key, i), errors, warnings,
                observable_tension=observable_tension,
            )
        return errors, warnings

    if key == "not":
        validate_predicate(
            val, "%s.not" % path, errors, warnings,
            observable_tension=observable_tension,
        )
        return errors, warnings

    kind = _PRED_ATOMIC_SPECS.get(key)
    if kind is None:
        errors.append(
            "%s 不支持谓词键 %r（允许键: %s）"
            % (path, key, ", ".join(sorted(set(_PRED_ATOMIC_SPECS) | _PRED_COMPOUND_KEYS)))
        )
        return errors, warnings

    if kind == "bool":
        if not isinstance(val, bool):
            errors.append("%s.%s 必须是布尔值" % (path, key))
    elif kind == "date_tuple":
        if not _check_date_tuple(val):
            errors.append("%s.%s 必须是 [year, month] 或 [year, month, day] 合法日期数组" % (path, key))
    elif kind == "nonneg_int":
        if isinstance(val, bool) or not isinstance(val, int) or val < 0:
            errors.append("%s.%s 必须是非负整数" % (path, key))
    elif kind == "number":
        if isinstance(val, bool) or not isinstance(val, (int, float)):
            errors.append("%s.%s 必须是数值" % (path, key))
    elif kind == "unit_float":
        if isinstance(val, bool) or not isinstance(val, (int, float)) or not (0.0 <= float(val) <= 1.0):
            errors.append("%s.%s 必须是 0.0..1.0 之间的小数（例如 25%% 请写 0.25）" % (path, key))
        elif key == "world_tension_ge" and not observable_tension:
            warnings.append(
                "%s.world_tension_ge: 当前引擎版本暂未解码 live world_tension（返回 None），"
                "该原子在运行时将恒为 False；请搭配 {\"any\": [...]} 备选条件或改用 date_ge / war_support_ge / at_war"
                % path
            )
    elif kind == "str":
        if not isinstance(val, str) or not val.strip():
            errors.append("%s.%s 必须是非空字符串" % (path, key))

    return errors, warnings


def _date_ymd(val):
    if len(val) == 2:
        return (int(val[0]), int(val[1]), 1)
    return (int(val[0]), int(val[1]), int(val[2]))


def eval_predicate(node, ctx):
    """在只读上下文快照 `ctx` 上确定性求值显式谓词树。"""
    if not isinstance(node, dict) or len(node) != 1:
        return False
    key, val = next(iter(node.items()))
    if key == "always":
        return bool(val)
    if key == "all":
        return all(eval_predicate(ch, ctx) for ch in (val or ()))
    if key == "any":
        return any(eval_predicate(ch, ctx) for ch in (val or ()))
    if key == "not":
        return not eval_predicate(val, ctx)
    if key == "at_war":
        return bool(ctx.get("at_war")) is bool(val)
    if key == "date_ge":
        cur = ctx.get("date_ymd") or (1936, 1, 1)
        return tuple(cur) >= _date_ymd(val)
    if key == "date_lt":
        cur = ctx.get("date_ymd") or (1936, 1, 1)
        return tuple(cur) < _date_ymd(val)
    if key == "civ_ge":
        return int(ctx.get("civ_total") or 0) >= int(val)
    if key == "civ_lt":
        return int(ctx.get("civ_total") or 0) < int(val)
    if key == "mil_ge":
        return int(ctx.get("mil_total") or 0) >= int(val)
    if key == "mil_lt":
        return int(ctx.get("mil_total") or 0) < int(val)
    if key == "dock_ge":
        return int(ctx.get("dock_total") or 0) >= int(val)
    if key == "pp_ge":
        return float(ctx.get("political_power") or 0.0) >= float(val)
    if key == "pp_lt":
        return float(ctx.get("political_power") or 0.0) < float(val)
    if key == "war_support_ge":
        ws = ctx.get("war_support")
        return ws is not None and float(ws) >= float(val)
    if key == "stability_ge":
        st = ctx.get("stability")
        return st is not None and float(st) >= float(val)
    if key == "world_tension_ge":
        wt = ctx.get("world_tension")
        return wt is not None and float(wt) >= float(val)
    if key == "manpower_ge":
        return float(ctx.get("manpower") or 0.0) >= float(val)
    if key == "manpower_lt":
        return float(ctx.get("manpower") or 0.0) < float(val)
    if key == "divisions_ge":
        return int(ctx.get("divisions") or 0) >= int(val)
    if key == "divisions_lt":
        return int(ctx.get("divisions") or 0) < int(val)
    if key == "fuel_ge":
        f = ctx.get("fuel")
        return f is not None and float(f) >= float(val)
    if key == "fuel_lt":
        f = ctx.get("fuel")
        return f is not None and float(f) < float(val)
    if key == "army_xp_ge":
        v = ctx.get("army_xp")
        return v is not None and float(v) >= float(val)
    if key == "navy_xp_ge":
        v = ctx.get("navy_xp")
        return v is not None and float(v) >= float(val)
    if key == "air_xp_ge":
        v = ctx.get("air_xp")
        return v is not None and float(v) >= float(val)
    if key == "focus_completed":
        return str(val) in (ctx.get("completed_focuses") or ())
    if key == "tech_researched":
        return str(val) in (ctx.get("researched") or ())
    if key == "idea_active":
        return str(val) in (ctx.get("active_ideas") or ())
    if key == "enemy_exists":
        return str(val).upper() in (ctx.get("enemies") or ())
    return False


# ===================================================================== 上下文采集（100% 纯只读）
def collect_context(g, tag=None):
    """纯只读采集 Standing Orders 评估所需的全量轻量上下文（0 注入）。"""
    d = _safe_date(g)
    my_tag = tag
    try:
        c = g._c(tag) if hasattr(g, "_c") else g.player()
        my_tag = getattr(c, "tag", None) or my_tag
    except Exception:
        c = None
    if not my_tag:
        try:
            my_tag = g.player_tag()
        except Exception:
            my_tag = "GER"

    summary = {}
    if c is not None and hasattr(c, "summary"):
        try:
            summary = c.summary() or {}
        except Exception:
            summary = {}

    def _num(attr, key, default=0.0):
        v = summary.get(key)
        if v is None and c is not None:
            v = getattr(c, attr, None)
            if callable(v):
                try:
                    v = v()
                except Exception:
                    v = None
        return default if v is None else v

    # 工厂数据优先复用 turnloop.factory_status
    fac = {}
    try:
        import turnloop as _tl
        fac = _tl.factory_status(g) or {}
    except Exception:
        fac = {}

    civ_total = int(fac.get("civilian_factories") if fac.get("civilian_factories") is not None else _num("civilian_factories", "civilian_factories", 0))
    mil_total = int(fac.get("military_available") if fac.get("military_available") is not None else _num("military_factories", "military_factories", 0))
    dock_total = int(fac.get("naval_available") if fac.get("naval_available") is not None else _num("naval_dockyards", "naval_dockyards", 0))

    # 战争与敌国
    enemies = set()
    try:
        import combat as _cb
        enemies = {str(x).upper() for x in (_cb.enemy_tags(g, my_tag) or ())}
    except Exception:
        enemies = set()

    # 政治：active_ideas / ruling_ideology / pp
    pol = {}
    try:
        pol = g.politics(my_tag) or {}
    except Exception:
        pol = {}
    active_ideas = set(pol.get("active_ideas") or ())
    ruling_ideology = pol.get("ruling_ideology")
    pp_val = pol.get("political_power")
    if pp_val is None:
        pp_val = _num("political_power", "political_power", 0.0)
    pp_daily = summary.get("political_power_daily")

    # 国策
    cur_focus = None
    try:
        cf = g.current_focus(my_tag)
        cur_focus = cf.get("key") if isinstance(cf, dict) else cf
    except Exception:
        cur_focus = None
    completed_focuses = set()
    try:
        completed_focuses = set(g.completed_focuses(my_tag) or ())
    except Exception:
        completed_focuses = set()

    # 科技
    researched = set()
    try:
        researched = set(g.researched(my_tag) or ())
    except Exception:
        researched = set()
    research_slots = []
    try:
        research_slots = g.research_slots(my_tag) or []
    except Exception:
        research_slots = []

    # 陆军师数
    div_count = 0
    try:
        dv = g.divisions(my_tag, limit=1) or {}
        div_count = int(dv.get("count") or 0)
    except Exception:
        try:
            arm = g.armies(my_tag) or {}
            div_count = int(arm.get("count") or 0)
        except Exception:
            div_count = 0

    # 陆/海/空三军经验（纯只读 0 注入）
    xp_map = summary.get("experience") if isinstance(summary.get("experience"), dict) else None
    if xp_map is None and c is not None:
        raw_xp = getattr(c, "experience", None)
        if callable(raw_xp):
            try:
                raw_xp = raw_xp()
            except Exception:
                raw_xp = None
        if isinstance(raw_xp, dict):
            xp_map = raw_xp
    xp_map = xp_map or {}
    army_xp = float(xp_map["army"]) if xp_map.get("army") is not None else None
    navy_xp = float(xp_map["navy"]) if xp_map.get("navy") is not None else None
    air_xp = float(xp_map["air"]) if xp_map.get("air") is not None else None

    # 世界紧张度（可能为 None）
    wt = None
    if hasattr(g, "world_tension"):
        try:
            wt = g.world_tension()
        except Exception:
            wt = None

    return {
        "tag": str(my_tag).upper(),
        "date": d.get("text"),
        "date_raw": d.get("raw"),
        "date_ymd": (d["year"], d["month"], d["day"]),
        "year": d["year"],
        "month": d["month"],
        "day": d["day"],
        "political_power": float(pp_val or 0.0),
        "political_power_daily": float(pp_daily) if pp_daily is not None else None,
        "stability": _num("stability", "stability", None),
        "war_support": _num("war_support", "war_support", None),
        "manpower": float(_num("manpower", "manpower", 0.0) or 0.0),
        "fuel": _num("fuel", "fuel", None),
        "army_xp": army_xp,
        "navy_xp": navy_xp,
        "air_xp": air_xp,
        "civ_total": civ_total,
        "mil_total": mil_total,
        "mil_idle": int(fac.get("military_idle") or 0),
        "dock_total": dock_total,
        "dock_idle": int(fac.get("naval_idle") or 0),
        "construction_queue_len": int(fac.get("construction_queue") or 0),
        "at_war": bool(enemies),
        "enemies": enemies,
        "ruling_ideology": ruling_ideology,
        "active_ideas": active_ideas,
        "current_focus": cur_focus,
        "completed_focuses": completed_focuses,
        "researched": researched,
        "research_slots": research_slots,
        "divisions": div_count,
        "world_tension": wt,
    }


def _extract_core_laws(active_ideas):
    """从当前 active_ideas 中提取经济 / 征兵 / 贸易三大核心法案，用于检测外部变动。"""
    try:
        import advisors as _adv
        cat = _adv.load_ideas_catalog()
    except Exception:
        cat = {}
    out = {}
    for k in sorted(active_ideas or ()):
        meta = cat.get(k)
        if meta and meta.get("slot") in CORE_LAW_SLOTS:
            out[meta["slot"]] = k
    return out


# ===================================================================== 策略校验与规范化
def _normalize_strategy(domain, raw, default_name, path, errors):
    """校验并规范化单个策略配置为 `{"strategy": name, "params": {...}}`。

    严格拒绝裸布尔（如 `True`/`False`）或未在 STRATEGY_REGISTRY 注册的策略名。
    """
    reg = STRATEGY_REGISTRY[domain]
    if raw is None:
        spec = reg[default_name]
        params = {k: copy.deepcopy(v.get("default")) for k, v in spec["params"].items() if "default" in v}
        return {"strategy": default_name, "params": params}
    if isinstance(raw, bool):
        errors.append(
            "%s 禁止使用裸布尔值 %r；请改为显式策略对象 {\"strategy\": \"<name>\", \"params\": {...}} "
            "（可用策略: %s，详见 hoi4_inspect(topic='strategy_catalog')）"
            % (path, raw, ", ".join(sorted(reg)))
        )
        return {"strategy": default_name, "params": {}}
    if isinstance(raw, str):
        name = raw.strip()
        user_params = {}
    elif isinstance(raw, dict):
        name = str(raw.get("strategy") or "").strip()
        if not name:
            errors.append(
                "%s 缺少字段 'strategy'（可用策略: %s）" % (path, ", ".join(sorted(reg)))
            )
            return {"strategy": default_name, "params": {}}
        extra_keys = set(raw.keys()) - {"strategy", "params"}
        if "params" in raw and not isinstance(raw["params"], dict):
            errors.append("%s.params 必须是 JSON 对象" % path)
            return {"strategy": name, "params": {}}
        # 允许简写把参数放在同级，但显式校验是否都在该策略 params 里
        user_params = dict(raw.get("params") or {})
        for ek in extra_keys:
            user_params[ek] = raw[ek]
    else:
        errors.append("%s 必须是策略名字符串或 {\"strategy\": ..., \"params\": ...} 对象" % path)
        return {"strategy": default_name, "params": {}}

    if name not in reg:
        errors.append(
            "%s 未知策略 %r（域 %s 仅支持: %s）"
            % (path, name, domain, ", ".join(sorted(reg)))
        )
        return {"strategy": default_name, "params": {}}

    spec = reg[name]
    pspec = spec["params"]
    norm_params = {}
    for k, v in user_params.items():
        if k not in pspec:
            errors.append(
                "%s 策略 %r 不支持参数 %r（允许参数: %s）"
                % (path, name, k, ", ".join(sorted(pspec)) or "无")
            )
            continue
        pdef = pspec[k]
        ok_p, coerced, err_p = _check_param(pdef, v)
        if not ok_p:
            errors.append("%s.%s %s" % (path, k, err_p))
        else:
            norm_params[k] = coerced

    for k, pdef in pspec.items():
        if k not in norm_params:
            if pdef.get("required"):
                errors.append("%s 策略 %r 缺少必填参数 %r" % (path, name, k))
            elif "default" in pdef:
                norm_params[k] = copy.deepcopy(pdef["default"])

    return {"strategy": name, "params": norm_params}


def _check_param(pdef, val):
    kind = pdef["type"]
    if kind == "int":
        if isinstance(val, bool) or not isinstance(val, int):
            return False, val, "必须是整数"
        if "min" in pdef and val < pdef["min"]:
            return False, val, "不能小于 %s" % pdef["min"]
        if "max" in pdef and val > pdef["max"]:
            return False, val, "不能大于 %s" % pdef["max"]
        return True, int(val), None
    if kind == "float":
        if isinstance(val, bool) or not isinstance(val, (int, float)):
            return False, val, "必须是数值"
        fv = float(val)
        if "min" in pdef and fv < pdef["min"]:
            return False, val, "不能小于 %s" % pdef["min"]
        if "max" in pdef and fv > pdef["max"]:
            return False, val, "不能大于 %s" % pdef["max"]
        return True, fv, None
    if kind == "bool":
        if not isinstance(val, bool):
            return False, val, "必须是布尔值"
        return True, val, None
    if kind == "list_int":
        if not isinstance(val, list) or not all(isinstance(x, int) and not isinstance(x, bool) for x in val):
            return False, val, "必须是整数列表"
        return True, [int(x) for x in val], None
    if kind == "list_str":
        if not isinstance(val, list) or not all(isinstance(x, str) and x.strip() for x in val):
            return False, val, "必须是非空字符串列表"
        return True, [x.strip() for x in val], None
    if kind == "list_dict":
        if not isinstance(val, list) or not all(isinstance(x, dict) for x in val):
            return False, val, "必须是对象列表"
        return True, copy.deepcopy(val), None
    if kind == "ratio_map":
        if not isinstance(val, dict) or not val:
            return False, val, "必须是非空比例映射（如 {\"inf\": 0.35, \"art\": 0.2, \"armor\": 0.2, \"air\": 0.25}）"
        out = {}
        for rk, rv in val.items():
            if rk not in ("inf", "art", "armor", "air"):
                return False, val, "包含未知装备分组 %r（仅支持 inf/art/armor/air）" % rk
            if isinstance(rv, bool) or not isinstance(rv, (int, float)) or float(rv) < 0:
                return False, val, "分组 %r 比例必须是非负数值" % rk
            out[rk] = float(rv)
        s = sum(out.values())
        if s <= 0:
            return False, val, "比例之和必须大于 0"
        return True, {k: round(v / s, 4) for k, v in out.items()}, None
    if kind == "int_map":
        if not isinstance(val, dict):
            return False, val, "必须是键值整数映射"
        out = {}
        for rk, rv in val.items():
            if isinstance(rv, bool) or not isinstance(rv, int) or rv < 0:
                return False, val, "%r 必须是非负整数" % rk
            out[str(rk)] = int(rv)
        return True, out, None
    if kind == "float_map":
        if not isinstance(val, dict):
            return False, val, "必须是键值数值映射"
        out = {}
        for rk, rv in val.items():
            if isinstance(rv, bool) or not isinstance(rv, (int, float)) or float(rv) < 0:
                return False, val, "%r 必须是非负数值" % rk
            out[str(rk)] = float(rv)
        return True, out, None
    if kind == "str_list_map":
        if not isinstance(val, dict):
            return False, val, "必须是 角色->字符串列表 映射"
        out = {}
        for rk, rv in val.items():
            if not isinstance(rv, list) or not all(isinstance(x, str) and x.strip() for x in rv):
                return False, val, "%r 必须是非空字符串列表" % rk
            out[str(rk)] = [x.strip() for x in rv]
        return True, out, None
    return True, val, None


# ===================================================================== 阶段合并与解析
def _deep_merge(base, override):
    out = copy.deepcopy(base) if isinstance(base, dict) else {}
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _extract_phases(profile, errors):
    """将单阶段或 `phases` 多阶段 profile 统一规范化为 phases 列表。"""
    defaults = {}
    if "defaults" in profile:
        if not isinstance(profile["defaults"], dict):
            errors.append("defaults 必须是 JSON 对象")
        else:
            defaults = copy.deepcopy(profile["defaults"])

    top_inline = {k: copy.deepcopy(profile[k]) for k in _STAGE_PROFILE_KEYS if k in profile}
    if "phases" in profile:
        raw_phases = profile["phases"]
        if isinstance(raw_phases, dict):
            # 兼容 dict 写法：{"peace": {...}, "war": {...}}
            phase_list = []
            for pname, pbody in raw_phases.items():
                if not isinstance(pbody, dict):
                    errors.append("phases.%s 必须是 JSON 对象" % pname)
                    continue
                item = dict(pbody)
                item.setdefault("name", str(pname))
                phase_list.append(item)
            raw_phases = phase_list
        if not isinstance(raw_phases, list) or not raw_phases:
            errors.append("phases 必须是非空列表或命名阶段映射")
            return []
        merged_defaults = _deep_merge(defaults, top_inline)
        out = []
        for idx, ph in enumerate(raw_phases):
            if not isinstance(ph, dict):
                errors.append("phases[%d] 必须是 JSON 对象" % idx)
                continue
            pname = str(ph.get("name") or ("phase_%d" % idx)).strip()
            enter_when = ph.get("enter_when")
            if enter_when is None:
                enter_when = {"always": True} if idx == 0 else None
            if enter_when is None:
                errors.append("phases[%d](%s) 缺少 enter_when 显式谓词树" % (idx, pname))
            sticky = ph.get("sticky", False)
            if not isinstance(sticky, bool):
                errors.append("phases[%d](%s).sticky 必须是布尔值" % (idx, pname))
                sticky = False
            sub = ph.get("profile") if isinstance(ph.get("profile"), dict) else {
                k: v for k, v in ph.items() if k in _STAGE_PROFILE_KEYS
            }
            unknown_ph = set(ph.keys()) - {"name", "enter_when", "sticky", "profile"} - _STAGE_PROFILE_KEYS
            if unknown_ph:
                errors.append("phases[%d](%s) 含未知键: %s" % (idx, pname, ", ".join(sorted(unknown_ph))))
            out.append({
                "name": pname,
                "enter_when": enter_when,
                "sticky": sticky,
                "profile": _deep_merge(merged_defaults, sub),
            })
        return out

    # 单阶段简洁写法
    return [{
        "name": str(profile.get("name") or "default").strip() or "default",
        "enter_when": {"always": True},
        "sticky": False,
        "profile": _deep_merge(defaults, top_inline),
    }]


def resolve_active_phase(compiled_profile, ctx, prev_idx=0, sticky_floor=-1):
    """按优先级（后声明覆盖前声明）计算当前生效阶段，并遵守 sticky 不回退规则。"""
    phases = compiled_profile.get("phases") or []
    if not phases:
        return 0, None, sticky_floor
    floor = max(-1, int(sticky_floor))
    matched_idx = None
    # 从后往前扫描，首个满足 enter_when 且 >= sticky_floor 的阶段胜出
    for idx in range(len(phases) - 1, -1, -1):
        if idx < floor:
            break
        if eval_predicate(phases[idx]["enter_when"], ctx):
            matched_idx = idx
            break
    if matched_idx is None:
        matched_idx = max(floor, min(prev_idx, len(phases) - 1))
        if matched_idx < 0:
            matched_idx = 0
    if phases[matched_idx].get("sticky"):
        floor = max(floor, matched_idx)
    return matched_idx, phases[matched_idx], floor


# ===================================================================== 编译器（declare -> compile(dry_run)）
def _validate_kernel_floor_config(raw_kf, raw_profile, errors):
    """解析并强校验不可关闭的 Kernel Floor 参数。企图关闭任何地板直接报硬错！"""
    # 拒绝任何显式试图关闭内核地板的顶层或子级字段
    for bad_key in ("disable_kernel_floor", "ignore_kernel_floor", "no_kernel_floor"):
        if raw_profile.get(bad_key):
            errors.append("安全硬约束：禁止通过 %r 关闭内核熔断地板（Kernel Floor）" % bad_key)
    kf = {}
    if raw_kf is not None:
        if not isinstance(raw_kf, dict):
            errors.append("kernel_floor 必须是 JSON 对象")
            raw_kf = {}
    else:
        raw_kf = {}

    for rule_name in KERNEL_FLOOR_RULES:
        if rule_name in raw_kf and raw_kf[rule_name] is False:
            errors.append("安全硬约束：kernel_floor.%s 不可关闭（必须保持启用）" % rule_name)
    _check_unknown_keys(raw_kf, _KERNEL_FLOOR_ALLOWED_KEYS, "kernel_floor", errors)

    def _bound_int(key, default, lo, hi):
        v = raw_kf.get(key, default)
        if v is False or v is None or isinstance(v, bool) or not isinstance(v, int):
            errors.append("kernel_floor.%s 必须是 [%d..%d] 之间的整数（不可关闭）" % (key, lo, hi))
            return default
        if v < lo or v > hi:
            errors.append("kernel_floor.%s=%d 超出允许范围 [%d..%d]" % (key, v, lo, hi))
            return max(lo, min(hi, v))
        return int(v)

    kf["max_autopilot_days"] = _bound_int(
        "max_autopilot_days", DEFAULT_MAX_AUTOPILOT_DAYS,
        MIN_MAX_AUTOPILOT_DAYS, MAX_MAX_AUTOPILOT_DAYS,
    )
    kf["heartbeat_days"] = _bound_int(
        "heartbeat_days", DEFAULT_HEARTBEAT_DAYS,
        MIN_HEARTBEAT_DAYS, MAX_HEARTBEAT_DAYS,
    )
    kf["no_progress_limit"] = _bound_int(
        "no_progress_limit", DEFAULT_NO_PROGRESS_LIMIT,
        MIN_NO_PROGRESS_LIMIT, MAX_NO_PROGRESS_LIMIT,
    )
    return kf


def _load_static_focus_keys(g, tag):
    """获取已知国策 key 集合（优先 live focus_tree，其次 focusplan 静态文件）。"""
    keys = set()
    try:
        for f in g.focuses(tag) or ():
            if isinstance(f, dict) and f.get("key"):
                keys.add(f["key"])
            elif isinstance(f, str):
                keys.add(f)
    except Exception:
        pass
    if not keys:
        try:
            sf = g.selectable_focuses(tag) or {}
            for r in (sf.get("selectable") or ()):
                if isinstance(r, dict) and (r.get("key") or r.get("id")):
                    keys.add(r.get("key") or r.get("id"))
        except Exception:
            pass
    try:
        import focusplan as _fp
        fmeta = (_fp.load() or {}).get("focuses") or {}
        if fmeta:
            keys.update(fmeta.keys())
    except Exception:
        pass
    return keys


def _load_static_tech_defs(g):
    """获取科技定义字典（合并 techdata 与 live _tech_by_name）。"""
    defs = {}
    try:
        import techdata as _td
        defs = dict(_td.load() or {})
    except Exception:
        defs = {}
    live_names = getattr(g, "_tech_by_name", None)
    if isinstance(live_names, dict):
        for k in live_names:
            defs.setdefault(k, {"year": 1936, "xor": [], "parents": [], "category": "other"})
    return defs


def _load_idea_and_advisor_lookup(g, tag):
    """构建法案/理念/顾问统一查表（key_lower -> rec）。"""
    lookup = {}
    try:
        if hasattr(g, "idea_catalog"):
            cat = g.idea_catalog(tag, limit=600) or {}
            for it in cat.get("items") or ():
                k = str(it.get("key") or "")
                if k:
                    lookup[k.lower()] = it
                tok = str(it.get("idea_token") or "")
                if tok:
                    lookup.setdefault(tok.lower(), it)
    except Exception:
        pass
    try:
        import advisors as _adv
        for k, meta in (_adv.load_ideas_catalog() or {}).items():
            lookup.setdefault(k.lower(), {
                "key": k,
                "kind": "law" if meta.get("is_law") else "idea",
                "slot": meta.get("slot"),
                "cost": int(meta.get("cost") if meta.get("cost") is not None else 150),
                "mutually_exclusive_with": meta.get("mutually_exclusive_with") or [],
            })
        for a in (_adv.load_advisors(tag) or ()):
            ck = str(a.get("character") or "")
            tok = str(a.get("idea_token") or ck)
            rec = {
                "key": ck,
                "idea_token": tok,
                "kind": "advisor",
                "slot": a.get("slot") or "political_advisor",
                "cost": int(a.get("cost") if a.get("cost") is not None else 150),
            }
            if ck:
                lookup.setdefault(ck.lower(), rec)
            if tok:
                lookup.setdefault(tok.lower(), rec)
    except Exception:
        pass
    return lookup


def _load_known_decisions(g, tag):
    keys = set()
    try:
        for which in ("visible", "takeable"):
            for d in g.decisions(tag, which=which, rich=False) or ():
                if isinstance(d, dict) and d.get("key"):
                    keys.add(d["key"])
    except Exception:
        pass
    try:
        import decisions_meta as _dm
        if hasattr(_dm, "load"):
            keys.update((_dm.load() or {}).keys())
    except Exception:
        pass
    return keys


def _is_irreversible_focus(key):
    kl = str(key or "").lower()
    return any(kw in kl for kw in IRREVERSIBLE_FOCUS_KEYWORDS)


def _compile_stage_profile(g, st_raw, stage_path, ctx, refs, errors, warnings):
    """编译并规范化单个阶段的完整策略配置，同时执行静态存在性与冲突校验。"""
    norm = {}
    obs_wt = ctx.get("world_tension") is not None
    focus_keys = refs["focus_keys"]
    tech_defs = refs["tech_defs"]
    idea_lookup = refs["idea_lookup"]
    dec_keys = refs["decision_keys"]
    bldg_keys = refs["building_keys"]

    # 禁止企图在 interrupts_extra 关闭任何内核地板，并严格拦截未知键（防拼错熔断名被静默忽略）
    ie_raw = st_raw.get("interrupts_extra") or {}
    if not isinstance(ie_raw, dict):
        errors.append("%s.interrupts_extra 必须是 JSON 对象" % stage_path)
        ie_raw = {}
    else:
        for rk in KERNEL_FLOOR_RULES:
            if ie_raw.get(rk) is False:
                errors.append(
                    "%s.interrupts_extra.%s=false 被拒绝：内核熔断地板不可关闭"
                    % (stage_path, rk)
                )
        _check_unknown_keys(ie_raw, _INTERRUPTS_EXTRA_ALLOWED_KEYS, "%s.interrupts_extra" % stage_path, errors)
        for bk in ("on_phase_change", "on_focus_queue_empty", "on_research_queue_empty", "on_construction_queue_empty"):
            if bk in ie_raw and not isinstance(ie_raw[bk], bool):
                errors.append("%s.interrupts_extra.%s 必须是布尔值" % (stage_path, bk))
        if "on_construction_queue_below" in ie_raw and ie_raw["on_construction_queue_below"] is not None:
            cb = ie_raw["on_construction_queue_below"]
            if isinstance(cb, bool) or not isinstance(cb, int) or cb < 0 or cb > 30:
                errors.append("%s.interrupts_extra.on_construction_queue_below 必须是 0..30 之间的整数" % stage_path)
        if "on_pp_near_cap" in ie_raw and ie_raw["on_pp_near_cap"] is not None:
            pc = ie_raw["on_pp_near_cap"]
            if not isinstance(pc, bool):
                if not isinstance(pc, (int, float)) or not (200.0 <= float(pc) <= 2000.0):
                    errors.append("%s.interrupts_extra.on_pp_near_cap 必须是布尔值或 200..2000 之间的数值" % stage_path)
        if "on_manpower_below" in ie_raw and ie_raw["on_manpower_below"] is not None:
            mv = ie_raw["on_manpower_below"]
            if isinstance(mv, bool) or not isinstance(mv, (int, float)) or float(mv) < 0:
                errors.append("%s.interrupts_extra.on_manpower_below 必须是非负数值" % stage_path)
        if "custom_when" in ie_raw and ie_raw["custom_when"] is not None:
            validate_predicate(
                ie_raw["custom_when"], "%s.interrupts_extra.custom_when" % stage_path,
                errors, warnings, observable_tension=obs_wt,
            )
    norm["interrupts_extra"] = copy.deepcopy(ie_raw)

    # 1) Focus
    f_raw = st_raw.get("focus") or {}
    if not isinstance(f_raw, dict):
        errors.append("%s.focus 必须是 JSON 对象" % stage_path)
        f_raw = {}
    else:
        _check_unknown_keys(f_raw, _FOCUS_ALLOWED_KEYS, "%s.focus" % stage_path, errors)
    f_must = list(f_raw.get("must") or ())
    f_queue = list(f_raw.get("queue") or ())
    f_avoid = list(f_raw.get("avoid") or ())
    f_on_blocked = str(f_raw.get("on_blocked") or "wake").strip().lower()
    if f_on_blocked not in ("wake", "wait", "skip"):
        errors.append("%s.focus.on_blocked=%r 无效（仅支持 wake/wait/skip）" % (stage_path, f_on_blocked))
    f_fallback = str(f_raw.get("fallback") or "none").strip().lower()
    if f_fallback not in ("none", "tier_score"):
        errors.append("%s.focus.fallback=%r 无效（仅支持 none/tier_score）" % (stage_path, f_fallback))
    allow_excl = bool(f_raw.get("allow_exclusive_focus", False))
    for lst_name, lst in (("must", f_must), ("queue", f_queue), ("avoid", f_avoid)):
        if not all(isinstance(x, str) and x.strip() for x in lst):
            errors.append("%s.focus.%s 必须是非空字符串列表" % (stage_path, lst_name))
            continue
        for fk in lst:
            if focus_keys and fk not in focus_keys:
                errors.append("%s.focus.%s 中不存在国策 key %r（请核对拼写或版本差异）" % (stage_path, lst_name, fk))
            if lst_name in ("must", "queue") and _is_irreversible_focus(fk):
                if not (lst_name == "must" and allow_excl):
                    errors.append(
                        "%s.focus.%s 包含不可逆/排他分支国策 %r；硬护栏默认禁止自动选择"
                        "（若确需执行，请放入 must 并显式设 allow_exclusive_focus=true）"
                        % (stage_path, lst_name, fk)
                    )
    overlap_avoid = (set(f_must) | set(f_queue)) & set(f_avoid)
    if overlap_avoid:
        errors.append("%s.focus 冲突：%s 同时出现在 avoid 与 must/queue" % (stage_path, sorted(overlap_avoid)))
    allow_skip_snowball = bool(f_raw.get("allow_skip_snowball", False))
    raw_tiers = f_raw.get("tier_preference")
    if raw_tiers is not None:
        if not isinstance(raw_tiers, list) or not raw_tiers or not all(isinstance(x, str) and x.strip() for x in raw_tiers):
            errors.append("%s.focus.tier_preference 必须是非空字符串列表" % stage_path)
            tier_pref = list(DEFAULT_FOCUS_TIER_PREFERENCE)
        else:
            tier_pref = [x.strip() for x in raw_tiers]
            bad_tiers = [t for t in tier_pref if t not in _VALID_FOCUS_TIERS]
            if bad_tiers:
                errors.append(
                    "%s.focus.tier_preference 包含未知层级 %s（合法层级: %s）"
                    % (stage_path, bad_tiers, ", ".join(sorted(_VALID_FOCUS_TIERS)))
                )
            missing_def = [t for t in DEFAULT_FOCUS_TIER_PREFERENCE if t not in tier_pref]
            if f_fallback == "tier_score" and "S0_snowball" in missing_def and not allow_skip_snowball:
                errors.append(
                    "%s.focus.tier_preference 排除了核心吞并雪球层级 'S0_snowball'（德奥合并 / 苏台德 / 捷克 / 但泽等主线将不会被自动执行）；"
                    "请保留 'S0_snowball' 或省略 tier_preference 使用默认层级（若确需走非史实路线请显式设 allow_skip_snowball=true）"
                    % stage_path
                )
            elif missing_def:
                warnings.append(
                    "%s.focus.tier_preference 排除了默认关键层级 %s，对应类别国策在 tier_score 兜底中将降为最低优先级"
                    % (stage_path, missing_def)
                )
    else:
        tier_pref = list(DEFAULT_FOCUS_TIER_PREFERENCE)
    norm["focus"] = {
        "enabled": bool(f_raw.get("enabled", bool(f_must or f_queue or f_fallback != "none"))),
        "must": f_must,
        "queue": f_queue,
        "avoid": f_avoid,
        "on_blocked": f_on_blocked,
        "fallback": f_fallback,
        "tier_preference": tier_pref,
        "allow_exclusive_focus": allow_excl,
        "allow_skip_snowball": allow_skip_snowball,
    }

    # 2) Research
    r_raw = st_raw.get("research") or {}
    if not isinstance(r_raw, dict):
        errors.append("%s.research 必须是 JSON 对象" % stage_path)
        r_raw = {}
    else:
        _check_unknown_keys(r_raw, _RESEARCH_ALLOWED_KEYS, "%s.research" % stage_path, errors)
    r_queue = list(r_raw.get("queue") or ())
    r_never = list(r_raw.get("never") or ())
    max_ahead = r_raw.get("max_ahead_years", 0.5)
    if isinstance(max_ahead, bool) or not isinstance(max_ahead, (int, float)) or float(max_ahead) < 0:
        errors.append("%s.research.max_ahead_years 必须是非负数值" % stage_path)
        max_ahead = 0.5
    r_fb = _normalize_strategy(
        "research_fallback", r_raw.get("fallback_strategy"),
        "none", "%s.research.fallback_strategy" % stage_path, errors,
    )
    if not all(isinstance(x, str) and x.strip() for x in r_queue):
        errors.append("%s.research.queue 必须是非空字符串列表" % stage_path)
    if not all(isinstance(x, str) and x.strip() for x in r_never):
        errors.append("%s.research.never 必须是非空字符串列表" % stage_path)
    overlap_r = set(r_queue) & set(r_never)
    if overlap_r:
        errors.append("%s.research 冲突：%s 同时出现在 queue 与 never" % (stage_path, sorted(overlap_r)))
    # 存在性与 XOR 互斥冲突校验（同时覆盖已研科技 + 当前所有科研槽科技（含刚满 100% 的 slot_done）+ 同队列科技）
    active_techs_now = {
        s.get("researching")
        for s in (ctx.get("research_slots") or ())
        if s.get("researching")
    }
    planned_set = set(r_queue) | set(ctx.get("researched") or ()) | active_techs_now
    for tk in r_queue:
        if tech_defs and tk not in tech_defs:
            errors.append("%s.research.queue 中不存在科技 key %r" % (stage_path, tk))
            continue
        tinfo = tech_defs.get(tk) or {}
        if tinfo.get("special_project_gated"):
            warnings.append(
                "%s.research.queue 中科技 %r 需要先完成对应特别工程（special_project）才可研发，未完成前将被引擎拒绝并自动退避"
                % (stage_path, tk)
            )
        if tinfo.get("always_no"):
            warnings.append(
                "%s.research.queue 中科技 %r 在脚本中标注 allow={always=no}（仅由国策/事件授予），无法通过常规科研槽研发"
                % (stage_path, tk)
            )
        xors = set(tinfo.get("xor") or ())
        bad_xor = sorted(xors & planned_set)
        if bad_xor:
            errors.append(
                "%s.research.queue 中科技 %r 与已研/在研或同队列科技 %s 存在 XOR 互斥冲突"
                % (stage_path, tk, bad_xor)
            )
        sy = int(tinfo.get("start_year") or tinfo.get("year") or 1936)
        if sy - int(ctx.get("year") or 1936) > float(max_ahead) + 2.0:
            warnings.append(
                "%s.research.queue 中科技 %r 为 %d 年科技，远超当前年份 %d + max_ahead_years=%.2f（将被延迟至对应年份再点）"
                % (stage_path, tk, sy, int(ctx.get("year") or 1936), float(max_ahead))
            )
    norm["research"] = {
        "enabled": bool(r_raw.get("enabled", bool(r_queue or r_fb["strategy"] != "none"))),
        "queue": r_queue,
        "never": r_never,
        "max_ahead_years": float(max_ahead),
        "fallback_strategy": r_fb,
    }

    # 3) Construction
    c_raw = st_raw.get("construction") or {}
    if not isinstance(c_raw, dict):
        errors.append("%s.construction 必须是 JSON 对象" % stage_path)
        c_raw = {}
    if "state_order" in c_raw:
        errors.append(
            "%s.construction.state_order 已废弃；请改用显式策略字段 state_strategy: "
            "{\"strategy\": \"infra_then_slots\"|\"max_free_slots\"|\"explicit\", \"params\": {...}}"
            % stage_path
        )
    if isinstance(c_raw, dict):
        _check_unknown_keys(c_raw, _CONSTRUCTION_ALLOWED_KEYS, "%s.construction" % stage_path, errors, skip=("state_order",))
    c_strat = _normalize_strategy(
        "construction_state", c_raw.get("state_strategy"),
        "infra_then_slots", "%s.construction.state_strategy" % stage_path, errors,
    )
    target_q = c_raw.get("target_queue_len", 6)
    if isinstance(target_q, bool) or not isinstance(target_q, int) or not (1 <= target_q <= 30):
        errors.append("%s.construction.target_queue_len 必须是 1..30 之间的整数" % stage_path)
        target_q = 6
    count_existing = c_raw.get("count_existing_in_queue", False)
    if not isinstance(count_existing, bool):
        errors.append("%s.construction.count_existing_in_queue 必须是布尔值" % stage_path)
        count_existing = False
    c_queue = []
    for idx_q, item in enumerate(c_raw.get("queue") or ()):
        if not isinstance(item, dict) or not item.get("building"):
            errors.append("%s.construction.queue[%d] 必须是含 building 的对象" % (stage_path, idx_q))
            continue
        _check_unknown_keys(item, _CONSTRUCTION_QUEUE_ITEM_KEYS, "%s.construction.queue[%d]" % (stage_path, idx_q), errors)
        bname = str(item["building"]).strip()
        if bldg_keys and bname not in bldg_keys:
            errors.append("%s.construction.queue[%d] 未知建筑类型 %r" % (stage_path, idx_q, bname))
        cnt = item.get("count", 1)
        if isinstance(cnt, bool) or not isinstance(cnt, int) or cnt < 1:
            errors.append("%s.construction.queue[%d].count 必须是正整数" % (stage_path, idx_q))
            cnt = 1
        sid = item.get("state")
        if sid is not None and (isinstance(sid, bool) or not isinstance(sid, int) or sid <= 0):
            errors.append("%s.construction.queue[%d].state 必须是正整数州 ID" % (stage_path, idx_q))
            sid = None
        q_when = item.get("when")
        q_until = item.get("until")
        if q_when is not None:
            validate_predicate(q_when, "%s.construction.queue[%d].when" % (stage_path, idx_q),
                               errors, warnings, observable_tension=obs_wt)
        if q_until is not None:
            validate_predicate(q_until, "%s.construction.queue[%d].until" % (stage_path, idx_q),
                               errors, warnings, observable_tension=obs_wt)
        c_queue.append({
            "id": str(item.get("id") or ("q%d_%s" % (idx_q, bname))),
            "building": bname,
            "count": cnt,
            "state": sid,
            "when": q_when,
            "until": q_until,
        })
    c_rules = []
    for idx_r, rule in enumerate(c_raw.get("rules") or ()):
        if not isinstance(rule, dict) or not rule.get("building"):
            errors.append("%s.construction.rules[%d] 必须是含 building 的对象" % (stage_path, idx_r))
            continue
        _check_unknown_keys(rule, _CONSTRUCTION_RULE_KEYS, "%s.construction.rules[%d]" % (stage_path, idx_r), errors)
        bname = str(rule["building"]).strip()
        if bldg_keys and bname not in bldg_keys:
            errors.append("%s.construction.rules[%d] 未知建筑类型 %r" % (stage_path, idx_r, bname))
        # 优先支持 when，兼容 until（显式谓词树：not until）
        when_pred = rule.get("when")
        until_pred = rule.get("until")
        if when_pred is not None:
            validate_predicate(when_pred, "%s.construction.rules[%d].when" % (stage_path, idx_r),
                               errors, warnings, observable_tension=obs_wt)
        if until_pred is not None:
            validate_predicate(until_pred, "%s.construction.rules[%d].until" % (stage_path, idx_r),
                               errors, warnings, observable_tension=obs_wt)
        if when_pred is None and until_pred is None:
            when_pred = {"always": True}
        c_rules.append({
            "building": bname,
            "when": when_pred,
            "until": until_pred,
            "max_in_queue": int(rule.get("max_in_queue")) if rule.get("max_in_queue") is not None else None,
        })
    c_enabled = bool(c_raw.get("enabled", bool(c_queue or c_rules)))
    if c_enabled and c_queue and not c_rules:
        warnings.append(
            "%s.construction 配置了显式 queue 但 rules 为空：显式配额建完后将无后续补位规则，届时队列耗尽将触发唤醒"
            "（如需无缝续建请添加兜底 rules，例如 arms_factory / industrial_complex）"
            % stage_path
        )
    norm["construction"] = {
        "enabled": c_enabled,
        "target_queue_len": target_q,
        "count_existing_in_queue": count_existing,
        "state_strategy": c_strat,
        "queue": c_queue,
        "rules": c_rules,
    }

    # 4) Production
    p_raw = st_raw.get("production") or {}
    if not isinstance(p_raw, dict):
        errors.append("%s.production 必须是 JSON 对象" % stage_path)
        p_raw = {}
    if "auto_upgrade_max_payback_days" in p_raw:
        errors.append(
            "%s.production.auto_upgrade_max_payback_days 已废弃；请改用 upgrade_strategy: "
            "{\"strategy\": \"payback\", \"params\": {\"max_payback_days\": 45, \"min_stockpile_days\": 30}}"
            % stage_path
        )
    if isinstance(p_raw, dict):
        _check_unknown_keys(p_raw, _PRODUCTION_ALLOWED_KEYS, "%s.production" % stage_path, errors, skip=("auto_upgrade_max_payback_days",))
    p_alloc = _normalize_strategy(
        "production_alloc", p_raw.get("strategy"),
        "ratio_hysteresis", "%s.production.strategy" % stage_path, errors,
    )
    p_upg = _normalize_strategy(
        "production_upgrade", p_raw.get("upgrade_strategy"),
        "payback", "%s.production.upgrade_strategy" % stage_path, errors,
    )
    # 校验 min_factories 总和是否超过当前军工总数（提前预警挤空风险）
    min_facs = p_alloc["params"].get("min_factories") or {}
    if min_facs and sum(min_facs.values()) > int(ctx.get("mil_total") or 0):
        warnings.append(
            "%s.production.min_factories 合计 %d 超过当前军工总数 %d，部分底线需等新建军工后生效"
            % (stage_path, sum(min_facs.values()), int(ctx.get("mil_total") or 0))
        )
    norm["production"] = {
        "enabled": bool(p_raw.get("enabled", "strategy" in p_raw or "upgrade_strategy" in p_raw)),
        "strategy": p_alloc,
        "upgrade_strategy": p_upg,
    }

    # 5) PP Policy
    pp_raw = st_raw.get("pp_policy") or {}
    if not isinstance(pp_raw, dict):
        errors.append("%s.pp_policy 必须是 JSON 对象" % stage_path)
        pp_raw = {}
    else:
        _check_unknown_keys(pp_raw, _PP_POLICY_ALLOWED_KEYS, "%s.pp_policy" % stage_path, errors)
    reserve_pp = pp_raw.get("reserve_pp", 55)
    if isinstance(reserve_pp, bool) or not isinstance(reserve_pp, (int, float)) or float(reserve_pp) < 0:
        errors.append("%s.pp_policy.reserve_pp 必须是非负数值" % stage_path)
        reserve_pp = 55
    elif float(reserve_pp) > 2000.0:
        errors.append("%s.pp_policy.reserve_pp=%.0f 超过 HOI4 政治点数硬上限 2000" % (stage_path, float(reserve_pp)))
    pp_strat = _normalize_strategy(
        "pp_agenda", pp_raw.get("strategy"),
        "strict_agenda", "%s.pp_policy.strategy" % stage_path, errors,
    )
    on_done = str(pp_raw.get("on_agenda_done") or "save_and_wake").strip().lower()
    if on_done not in ("save_and_wake", "hold"):
        errors.append("%s.pp_policy.on_agenda_done=%r 无效（仅支持 save_and_wake/hold）" % (stage_path, on_done))
    auto_fb = bool(pp_raw.get("auto_fallback_ideas", False))
    if auto_fb:
        warnings.append("%s.pp_policy.auto_fallback_ideas=true 会自动消费非议程项目（仍受 reserve_pp 保护），请确认有意开启" % stage_path)

    agenda_norm = []
    for idx_a, item in enumerate(pp_raw.get("agenda") or ()):
        if not isinstance(item, dict):
            errors.append("%s.pp_policy.agenda[%d] 必须是 JSON 对象" % (stage_path, idx_a))
            continue
        _check_unknown_keys(item, _PP_AGENDA_ITEM_KEYS, "%s.pp_policy.agenda[%d]" % (stage_path, idx_a), errors)
        kind = str(item.get("kind") or "").strip().lower()
        key = str(item.get("key") or "").strip()
        if kind not in ("law", "idea", "advisor", "decision"):
            errors.append("%s.pp_policy.agenda[%d].kind=%r 无效（仅支持 law/idea/advisor/decision）" % (stage_path, idx_a, kind))
            continue
        if not key:
            errors.append("%s.pp_policy.agenda[%d] 缺少 key" % (stage_path, idx_a))
            continue
        when_p = item.get("when")
        if when_p is not None:
            validate_predicate(when_p, "%s.pp_policy.agenda[%d].when" % (stage_path, idx_a),
                               errors, warnings, observable_tension=obs_wt)
        est_cost = item.get("cost")
        slot = item.get("slot")
        if kind in ("law", "idea", "advisor"):
            rec = idea_lookup.get(key.lower())
            if idea_lookup and rec is None:
                errors.append("%s.pp_policy.agenda[%d] 未在 idea_catalog 找到 %s key %r" % (stage_path, idx_a, kind, key))
            elif rec is not None:
                if est_cost is None:
                    est_cost = int(rec.get("cost") if rec.get("cost") is not None else 150)
                if slot is None:
                    slot = rec.get("slot")
        elif kind == "decision":
            if dec_keys and key not in dec_keys:
                warnings.append("%s.pp_policy.agenda[%d] 决议 %r 当前未在可见/静态表发现，若为后期解锁请配合 when 谓词" % (stage_path, idx_a, key))
            if est_cost is None:
                est_cost = 50
        if est_cost is None:
            est_cost = 150
        if float(est_cost) + float(reserve_pp) > 2000.0:
            errors.append(
                "%s.pp_policy.agenda[%d](%s): cost(%d) + reserve_pp(%.0f) > 2000（HOI4 PP 上限），数学上永不可能购买"
                % (stage_path, idx_a, key, int(est_cost), float(reserve_pp))
            )
        agenda_norm.append({
            "kind": kind,
            "key": key,
            "slot": slot,
            "cost": int(est_cost),
            "when": when_p,
        })
    if not agenda_norm and (pp_raw or float(ctx.get("political_power") or 0.0) >= 300.0):
        warnings.append(
            "%s.pp_policy.agenda 为空：自动驾驶期间不会消费政治点数（容易积压至 2000 顶格浪费）；"
            "请先用 hoi4_inspect(topic='idea_catalog') 与 hoi4_inspect(topic='decisions') 补齐可用顾问/设计商/决议，"
            "并在 PP 接近 1500/1800/1950 时注意系统自动阶梯唤醒"
            % stage_path
        )
    norm["pp_policy"] = {
        "enabled": bool(pp_raw.get("enabled", bool(agenda_norm))),
        "reserve_pp": float(reserve_pp),
        "strategy": pp_strat,
        "agenda": agenda_norm,
        "auto_fallback_ideas": auto_fb,
        "on_agenda_done": on_done,
    }

    # 5b) XP Policy（第 9 子系统：陆军经典编制自动升级 + 显式学说解锁 + 弱编/囤废兜底唤醒）
    xp_raw = st_raw.get("xp_policy") or {}
    if not isinstance(xp_raw, dict):
        errors.append("%s.xp_policy 必须是 JSON 对象" % stage_path)
        xp_raw = {}
    else:
        _check_unknown_keys(xp_raw, _XP_POLICY_ALLOWED_KEYS, "%s.xp_policy" % stage_path, errors)

    try:
        import macros as _mc
        valid_presets = dict(_mc.TEMPLATE_PRESETS)
    except Exception:
        valid_presets = {
            "inf_9_1": {"approx_xp": 25},
            "inf_9_3": {"approx_xp": 45},
            "inf_9_3_aa": {"approx_xp": 55},
            "inf_9_0_hold": {"approx_xp": 15},
            "armor_light_20w": {"approx_xp": 35},
            "armor_medium_30w": {"approx_xp": 60},
        }

    xp_upgrades = []
    for idx_u, item in enumerate(xp_raw.get("army_template_upgrades") or ()):
        if not isinstance(item, dict):
            errors.append("%s.xp_policy.army_template_upgrades[%d] 必须是 JSON 对象" % (stage_path, idx_u))
            continue
        _check_unknown_keys(item, _XP_TEMPLATE_ITEM_KEYS, "%s.xp_policy.army_template_upgrades[%d]" % (stage_path, idx_u), errors)
        preset = str(item.get("preset") or "").strip().lower()
        if not preset or preset not in valid_presets:
            errors.append(
                "%s.xp_policy.army_template_upgrades[%d].preset=%r 无效（可用预设: %s）"
                % (stage_path, idx_u, preset, ", ".join(sorted(valid_presets)))
            )
            continue
        tpl_opt = item.get("template")
        if tpl_opt is not None and (not isinstance(tpl_opt, str) or not tpl_opt.strip()):
            errors.append("%s.xp_policy.army_template_upgrades[%d].template 必须是非空字符串" % (stage_path, idx_u))
            tpl_opt = None
        min_xp = item.get("min_xp", int(valid_presets[preset].get("approx_xp", 25)))
        if isinstance(min_xp, bool) or not isinstance(min_xp, (int, float)) or float(min_xp) < 0:
            errors.append("%s.xp_policy.army_template_upgrades[%d].min_xp 必须是非负数值" % (stage_path, idx_u))
            min_xp = 25.0
        when_p = item.get("when")
        if when_p is not None:
            validate_predicate(when_p, "%s.xp_policy.army_template_upgrades[%d].when" % (stage_path, idx_u),
                               errors, warnings, observable_tension=obs_wt)
        xp_upgrades.append({
            "preset": preset,
            "template": tpl_opt.strip() if isinstance(tpl_opt, str) else None,
            "min_xp": float(min_xp),
            "auto_rebalance": bool(item.get("auto_rebalance", True)),
            "when": when_p,
        })

    xp_doctrines = []
    for idx_d, item in enumerate(xp_raw.get("doctrines") or ()):
        if isinstance(item, str):
            item = {"doctrine": item}
        if not isinstance(item, dict):
            errors.append("%s.xp_policy.doctrines[%d] 必须是学说名字符串或 JSON 对象" % (stage_path, idx_d))
            continue
        _check_unknown_keys(item, _XP_DOCTRINE_ITEM_KEYS, "%s.xp_policy.doctrines[%d]" % (stage_path, idx_d), errors)
        doc = str(item.get("doctrine") or "").strip().lower()
        if not doc or doc not in _DOCTRINE_SERVICE:
            errors.append(
                "%s.xp_policy.doctrines[%d].doctrine=%r 无效（可用学说: %s）"
                % (stage_path, idx_d, doc, ", ".join(sorted(_DOCTRINE_SERVICE)))
            )
            continue
        min_xp = item.get("min_xp", 100.0)
        if isinstance(min_xp, bool) or not isinstance(min_xp, (int, float)) or float(min_xp) < 0:
            errors.append("%s.xp_policy.doctrines[%d].min_xp 必须是非负数值" % (stage_path, idx_d))
            min_xp = 100.0
        when_p = item.get("when")
        if when_p is not None:
            validate_predicate(when_p, "%s.xp_policy.doctrines[%d].when" % (stage_path, idx_d),
                               errors, warnings, observable_tension=obs_wt)
        xp_doctrines.append({
            "doctrine": doc,
            "service": _DOCTRINE_SERVICE[doc],
            "min_xp": float(min_xp),
            "when": when_p,
        })

    def _parse_xp_thresh(raw_val, default_val, field_name):
        if raw_val is False or raw_val == 0:
            return 0.0
        if raw_val is True or raw_val is None:
            return float(default_val)
        if isinstance(raw_val, (int, float)) and 0.0 <= float(raw_val) <= 500.0:
            return float(raw_val)
        errors.append("%s.xp_policy.%s 必须是布尔值或 0..500 之间的数值" % (stage_path, field_name))
        return float(default_val)

    wake_weak = _parse_xp_thresh(xp_raw.get("wake_on_weak_template_xp"), 50.0, "wake_on_weak_template_xp")
    wake_cap = _parse_xp_thresh(xp_raw.get("wake_on_xp_cap"), 150.0, "wake_on_xp_cap")
    xp_enabled = bool(xp_raw.get("enabled", True))
    if not xp_upgrades and not xp_doctrines and (st_raw.get("xp_policy") is None or xp_enabled):
        warnings.append(
            "%s.xp_policy 未配置 army_template_upgrades 或 doctrines：长跑期间不会自动升级师编制或点学说"
            "（推荐配 army_template_upgrades=[{\"preset\":\"inf_9_3\",\"min_xp\":45}] 消除 9步0炮弱编；"
            "弱编/经验满仓兜底唤醒默认保持开启）"
            % stage_path
        )
    norm["xp_policy"] = {
        "enabled": xp_enabled,
        "army_template_upgrades": xp_upgrades,
        "doctrines": xp_doctrines,
        "wake_on_weak_template_xp": wake_weak,
        "wake_on_xp_cap": wake_cap,
    }

    # 6) Divisions
    d_raw = st_raw.get("divisions") or {}
    if not isinstance(d_raw, dict):
        errors.append("%s.divisions 必须是 JSON 对象" % stage_path)
        d_raw = {}
    for legacy_bool in ("auto_assign_general", "split_by_role"):
        if legacy_bool in d_raw:
            errors.append(
                "%s.divisions.%s 已废弃（禁止裸布尔隐藏公式）；请改用 assign_strategy / general_strategy 对象"
                % (stage_path, legacy_bool)
            )
    if isinstance(d_raw, dict):
        _check_unknown_keys(d_raw, _DIVISIONS_ALLOWED_KEYS, "%s.divisions" % stage_path, errors, skip=("auto_assign_general", "split_by_role"))
    d_assign = _normalize_strategy(
        "division_assign", d_raw.get("assign_strategy"),
        "merge_existing_then_new", "%s.divisions.assign_strategy" % stage_path, errors,
    )
    d_gen = _normalize_strategy(
        "general_assign", d_raw.get("general_strategy"),
        "trait_then_skill", "%s.divisions.general_strategy" % stage_path, errors,
    )
    norm["divisions"] = {
        "enabled": bool(d_raw.get("enabled", bool(d_raw))),
        "auto_deploy": bool(d_raw.get("auto_deploy", True)),
        "assign_strategy": d_assign,
        "general_strategy": d_gen,
    }

    # 7) Events
    e_raw = st_raw.get("events") or {}
    if not isinstance(e_raw, dict):
        errors.append("%s.events 必须是 JSON 对象" % stage_path)
        e_raw = {}
    else:
        _check_unknown_keys(e_raw, _EVENTS_ALLOWED_KEYS, "%s.events" % stage_path, errors)
    e_strat = _normalize_strategy(
        "event_handling", e_raw.get("strategy"),
        "auto_trivial", "%s.events.strategy" % stage_path, errors,
    )
    norm["events"] = {"strategy": e_strat}

    # 8) Fuel
    fl_raw = st_raw.get("fuel") or {}
    if not isinstance(fl_raw, dict):
        errors.append("%s.fuel 必须是 JSON 对象" % stage_path)
        fl_raw = {}
    else:
        _check_unknown_keys(fl_raw, _FUEL_ALLOWED_KEYS, "%s.fuel" % stage_path, errors)
    pmode = str(fl_raw.get("peacetime_mode") or "conserve").strip().lower()
    if pmode not in ("conserve", "normal"):
        errors.append("%s.fuel.peacetime_mode=%r 无效（仅支持 conserve/normal）" % (stage_path, pmode))
    crit = fl_raw.get("critical_threshold", 50.0)
    if isinstance(crit, bool) or not isinstance(crit, (int, float)) or float(crit) < 0:
        errors.append("%s.fuel.critical_threshold 必须是非负数值" % stage_path)
        crit = 50.0
    norm["fuel"] = {
        "peacetime_mode": pmode,
        "critical_threshold": float(crit),
        "wake_on_critical": bool(fl_raw.get("wake_on_critical", True)),
    }

    return norm


def _select_states_for_building(g, tag, building, state_strategy, count=5, already_queued=None):
    """按选州策略排序并返回可建造目标州列表（纯只读）。"""
    strat = state_strategy["strategy"]
    params = state_strategy["params"]
    min_slots = int(params.get("min_slots") or 1)
    exclude = set(params.get("exclude_states") or ())
    core_only = bool(params.get("core_only", False))
    queued_map = dict(already_queued or {})

    try:
        import statemeta as _sm
        slot_buildings = _sm._load_slot_buildings()
    except Exception:
        slot_buildings = {
            "industrial_complex", "arms_factory", "dockyard",
            "synthetic_refinery", "fuel_silo", "rocket_site", "nuclear_reactor",
        }
    needs_slot = building in slot_buildings

    rows = []
    if strat == "explicit":
        state_ids = list(params.get("states") or ())
        for order_i, sid in enumerate(state_ids):
            if sid in exclude:
                continue
            try:
                sd = g.state_detail(int(sid), tag) or {}
            except Exception:
                continue
            if sd.get("ok") is False:
                continue
            sf = sd.get("slots_free")
            rem = None if sf is None else max(0, int(sf) - int(queued_map.get(int(sid), 0)))
            if needs_slot and rem is not None and rem < min_slots:
                continue
            infra = int((sd.get("buildings") or {}).get("infrastructure") or 0)
            rows.append({
                "state": int(sid),
                "name": sd.get("name"),
                "infra": infra,
                "slots_free": rem,
                "_order": order_i,
            })
        return rows[:count]

    try:
        owned = g.states(tag) or []
    except Exception:
        owned = []
    for s in owned:
        sid = int(s["id"])
        if sid in exclude:
            continue
        try:
            sd = g.state_detail(sid, tag) or {}
        except Exception:
            continue
        if sd.get("ok") is False:
            continue
        if core_only and sd.get("is_core_of_owner") is False:
            continue
        sf = sd.get("slots_free")
        rem = None if sf is None else max(0, int(sf) - int(queued_map.get(sid, 0)))
        if needs_slot and rem is not None and rem < min_slots:
            continue
        infra = int((sd.get("buildings") or {}).get("infrastructure") or 0)
        rows.append({
            "state": sid,
            "name": sd.get("name"),
            "infra": infra,
            "slots_free": rem if rem is not None else 0,
        })

    if strat == "max_free_slots":
        rows.sort(key=lambda r: (-int(r["slots_free"]), -int(r["infra"]), int(r["state"])))
    else:
        # infra_then_slots（默认：HOI4 基建决定 +20%/级建造速度）
        rows.sort(key=lambda r: (-int(r["infra"]), -int(r["slots_free"]), int(r["state"])))
    return rows[:count]


def _init_construction_queue_done(g, st_cfg, tag):
    """当 construction.count_existing_in_queue=True 时，用当前建造队列已有同建筑数抵扣显式 queue 初始额度。"""
    c_cfg = (st_cfg or {}).get("construction") or {}
    if not c_cfg.get("count_existing_in_queue") or not c_cfg.get("queue"):
        return {}
    try:
        q_now = g.construction_queue(tag) or []
    except Exception:
        q_now = []
    avail_by_bldg = {}
    for row in q_now:
        b = row.get("building")
        if b:
            avail_by_bldg[b] = avail_by_bldg.get(b, 0) + 1
    done_init = {}
    for qitem in c_cfg.get("queue") or ():
        b = qitem["building"]
        have = int(avail_by_bldg.get(b) or 0)
        if have > 0:
            used = min(have, int(qitem["count"]))
            done_init[qitem["id"]] = used
            avail_by_bldg[b] = have - used
    return done_init


def _run_subsystems_pass(g, st, st_cfg, ctx, dry_run=False):
    """单一真源：按固定顺序执行 8 个子系统，供 compile_profile 预览与 run_tick 共用。"""
    all_actions = []
    wake_triggers = []
    per_domain = {}
    for name, fn in (
        ("events", _tick_events),
        ("focus", _tick_focus),
        ("research", _tick_research),
        ("pp_policy", _tick_pp_policy),
        ("xp_policy", _tick_xp_policy),
        ("construction", _tick_construction),
        ("production", _tick_production),
        ("divisions", _tick_divisions),
    ):
        sub = fn(g, st, st_cfg, ctx, dry_run=dry_run)
        acts = list(sub.get("actions") or ())
        wks = list(sub.get("wake") or ())
        per_domain[name] = {"actions": acts, "wake": wks}
        all_actions.extend(acts)
        wake_triggers.extend(wks)
    return all_actions, wake_triggers, per_domain


def compile_profile(g, profile, tag=None):
    """三段式第二步：对 Standing Orders 进行静态 + 实时只读编译与单一真源 dry-run 预测。

    返回：
      - `ok`: bool（存在任意 `errors` 即为 False，绝不静默激活）
      - `errors`: 硬错误列表
      - `warnings`: 提醒/潜在风险列表
      - `compiled`: 规范化后的可执行结构（含 kernel_floor 与 phases）
      - `preview`: 基于真实子系统 dry-run 生成的下一 tick 动作清单、PP 曲线与各子系统预测
    """
    errors = []
    warnings = []
    if not isinstance(profile, dict) or not profile:
        return {"ok": False, "errors": ["profile 必须是非空 JSON 对象"], "warnings": []}

    unknown_top = set(profile.keys()) - _TOP_LEVEL_KEYS
    if unknown_top:
        # 单独检测企图关闭内核地板的恶意/误写键
        for bad_k in sorted(unknown_top):
            if "kernel_floor" in str(bad_k) or bad_k in KERNEL_FLOOR_RULES:
                errors.append("安全硬约束：不允许通过顶层键 %r 篡改或关闭内核熔断地板" % bad_k)
            else:
                errors.append("profile 含未知顶层键 %r（允许键: %s）" % (bad_k, ", ".join(sorted(_TOP_LEVEL_KEYS))))

    ctx = collect_context(g, tag=tag)
    my_tag = str(profile.get("tag") or ctx["tag"]).upper()
    if profile.get("tag") and my_tag != ctx["tag"]:
        warnings.append("profile.tag=%s 与当前玩家国家 %s 不一致，将按当前国家 %s 运行" % (my_tag, ctx["tag"], ctx["tag"]))
        my_tag = ctx["tag"]

    kernel_floor = _validate_kernel_floor_config(profile.get("kernel_floor"), profile, errors)
    raw_phases = _extract_phases(profile, errors)

    # 一次性收集引用查表（纯只读 0 注入）
    bldg_keys = set()
    try:
        bt = g.building_types() or {}
        bldg_keys = set(bt.keys())
    except Exception:
        bldg_keys = {
            "industrial_complex", "arms_factory", "dockyard", "infrastructure",
            "synthetic_refinery", "fuel_silo", "air_base", "anti_air_building",
            "radar_station", "supply_node", "bunker", "coastal_bunker",
        }
    refs = {
        "focus_keys": _load_static_focus_keys(g, my_tag),
        "tech_defs": _load_static_tech_defs(g),
        "idea_lookup": _load_idea_and_advisor_lookup(g, my_tag),
        "decision_keys": _load_known_decisions(g, my_tag),
        "building_keys": bldg_keys,
    }

    obs_wt = ctx.get("world_tension") is not None
    compiled_phases = []
    for idx, ph in enumerate(raw_phases):
        ppath = "phases[%d](%s)" % (idx, ph["name"])
        if ph.get("enter_when") is not None:
            validate_predicate(
                ph["enter_when"], "%s.enter_when" % ppath,
                errors, warnings, observable_tension=obs_wt,
            )
        norm_stage = _compile_stage_profile(g, ph["profile"], ppath, ctx, refs, errors, warnings)
        compiled_phases.append({
            "name": ph["name"],
            "enter_when": copy.deepcopy(ph["enter_when"]),
            "sticky": bool(ph.get("sticky", False)),
            "profile": norm_stage,
        })

    compiled = {
        "name": str(profile.get("name") or ("standing_orders_%s" % my_tag)),
        "tag": my_tag,
        "description": str(profile.get("description") or ""),
        "kernel_floor": kernel_floor,
        "phases": compiled_phases,
    }

    # 单一真源 dry-run 预览（用临时虚拟状态直接跑 7 子系统 dry_run=True，零写游戏、零污染真实状态）
    preview = {}
    if compiled_phases and not errors:
        ph_idx, active_ph, sticky_floor = resolve_active_phase(compiled, ctx)
        st_cfg = active_ph["profile"]
        preview["active_phase"] = active_ph["name"]
        preview["active_phase_index"] = ph_idx
        preview["kernel_floor"] = dict(kernel_floor)

        sim_st = {
            "active": True,
            "profile": compiled,
            "started_date_raw": ctx.get("date_raw"),
            "last_heartbeat_date_raw": ctx.get("date_raw"),
            "last_tick_date_raw": ctx.get("date_raw"),
            "current_phase": active_ph["name"],
            "current_phase_index": ph_idx,
            "sticky_floor_index": sticky_floor,
            "no_progress_streak": 0,
            "last_pp": ctx.get("political_power"),
            "known_ruling_ideology": ctx.get("ruling_ideology"),
            "known_core_laws": _extract_core_laws(ctx.get("active_ideas")),
            "construction_queue_done": _init_construction_queue_done(g, st_cfg, my_tag),
            "pp_agenda_done": [],
            "focus_blocked_reasons": {},
            "army_roles": {},
            "journal": [],
        }
        sim_actions, sim_wakes, sim_by_dom = _run_subsystems_pass(g, sim_st, st_cfg, ctx, dry_run=True)
        sim_floor = check_kernel_floor(g, sim_st, ctx=ctx)
        seen_sw = {(w.get("floor"), w.get("detail")) for w in sim_wakes}
        for fl in sim_floor:
            if (fl.get("floor"), fl.get("detail")) not in seen_sw:
                seen_sw.add((fl.get("floor"), fl.get("detail")))
                sim_wakes.append(fl)

        preview["next_tick_actions"] = sim_actions
        preview["next_tick_wake_triggers"] = sim_wakes

        # PP 预算曲线预测 + reserve_pp > pp_now 显式告警
        pp_cfg = st_cfg["pp_policy"]
        pp_now = float(ctx.get("political_power") or 0.0)
        pp_daily_raw = ctx.get("political_power_daily")
        pp_daily = float(pp_daily_raw) if (pp_daily_raw is not None and float(pp_daily_raw) > 0) else 1.0
        reserve = float(pp_cfg["reserve_pp"])
        if reserve > pp_now:
            if pp_daily_raw is not None and float(pp_daily_raw) <= 0:
                warnings.append(
                    "pp_policy.reserve_pp=%.0f 高于当前 PP=%.1f 且当前 PP 日增 %.2f <= 0，PP 议程将处于停滞状态"
                    % (reserve, pp_now, float(pp_daily_raw))
                )
            else:
                wait_rec = round((reserve - pp_now) / pp_daily, 1)
                warnings.append(
                    "pp_policy.reserve_pp=%.0f 高于当前 PP=%.1f（按 %.2f PP/日 预计需 %.1f 天恢复至保留线以上，此前不执行 PP 议程）"
                    % (reserve, pp_now, pp_daily, wait_rec)
                )
        sim_pp = pp_now
        cum_days = 0.0
        pp_curve = []
        for item in pp_cfg["agenda"]:
            if item["key"] in ctx["active_ideas"]:
                pp_curve.append({"key": item["key"], "status": "already_active", "cost": item["cost"]})
                continue
            need = float(item["cost"]) + reserve
            if sim_pp >= need:
                wait_d = 0.0
                sim_pp -= float(item["cost"])
            else:
                deficit = need - sim_pp
                wait_d = round(deficit / float(pp_daily), 1)
                cum_days += wait_d
                sim_pp = reserve
            pp_curve.append({
                "key": item["key"],
                "kind": item["kind"],
                "cost": item["cost"],
                "est_wait_days": wait_d,
                "est_cum_days": round(cum_days, 1),
                "pp_after_est": round(sim_pp, 1),
            })
        preview["pp_projection"] = {
            "pp_now": round(pp_now, 1),
            "pp_daily_used": round(float(pp_daily), 2),
            "reserve_pp": reserve,
            "total_agenda_cost": sum(int(x["cost"]) for x in pp_cfg["agenda"] if x["key"] not in ctx["active_ideas"]),
            "items": pp_curve,
        }

        # 建造预览（结合真实当前队列长度、并发上限与 dry-run 实际动作）
        c_cfg = st_cfg["construction"]
        try:
            q_live = g.construction_queue(my_tag) or []
        except Exception:
            q_live = []
        cur_q_len = len(q_live)
        target_q_len = int(c_cfg["target_queue_len"])
        if c_cfg["enabled"]:
            if cur_q_len >= target_q_len:
                warnings.append(
                    "construction: 当前已有 %d 条在建队列 >= target_queue_len=%d，需等空位腾出后才会追加新建筑（若要立即追加请调高 target_queue_len）"
                    % (cur_q_len, target_q_len)
                )
            if c_cfg["queue"] and not c_cfg["count_existing_in_queue"]:
                existing_counts = {}
                for row in q_live:
                    b = row.get("building")
                    if b:
                        existing_counts[b] = existing_counts.get(b, 0) + 1
                q0 = c_cfg["queue"][0]
                ex_n = int(existing_counts.get(q0["building"]) or 0)
                if ex_n >= int(q0["count"]):
                    warnings.append(
                        "construction.queue[0](%s x%d): 当前建造队列中已有 %d 座同类在建；"
                        "默认 count_existing_in_queue=false 会额外再建 %d 座后才进入下一队列项"
                        "（若希望把既有在建计入配额并直接轮到后续项，请设 count_existing_in_queue=true）"
                        % (q0["building"], int(q0["count"]), ex_n, int(q0["count"]))
                    )
        first_bldg = None
        for qit in c_cfg["queue"]:
            w_ok = True if qit.get("when") is None else eval_predicate(qit["when"], ctx)
            u_ok = False if qit.get("until") is None else eval_predicate(qit["until"], ctx)
            if w_ok and not u_ok and int(sim_st["construction_queue_done"].get(qit["id"]) or 0) < int(qit["count"]):
                first_bldg = qit["building"]
                break
        if first_bldg is None and c_cfg["rules"]:
            for rl in c_cfg["rules"]:
                w_ok = True if rl.get("when") is None else eval_predicate(rl["when"], ctx)
                u_ok = False if rl.get("until") is None else eval_predicate(rl["until"], ctx)
                if w_ok and not u_ok:
                    first_bldg = rl["building"]
                    break
        if first_bldg:
            queued_by_st_live = {}
            for row in q_live:
                if row.get("state") is not None:
                    sid_r = int(row["state"])
                    queued_by_st_live[sid_r] = queued_by_st_live.get(sid_r, 0) + 1
            preview["construction_preview"] = {
                "next_building": first_bldg,
                "current_queue_len": cur_q_len,
                "target_queue_len": target_q_len,
                "will_add_now": [a["action"] for a in sim_by_dom["construction"]["actions"]],
                "state_strategy": c_cfg["state_strategy"]["strategy"],
                "top_states": _select_states_for_building(
                    g, my_tag, first_bldg, c_cfg["state_strategy"], count=5, already_queued=queued_by_st_live,
                ),
            }

        # 国策预览（与 _tick_focus dry-run 严格同源，含 tier_score 回落与阻塞原因）
        f_cfg = st_cfg["focus"]
        f_dry_acts = [a["action"] for a in sim_by_dom["focus"]["actions"]]
        f_dry_wakes = [w["detail"] for w in sim_by_dom["focus"]["wake"]]
        preview["focus_preview"] = {
            "current": ctx.get("current_focus"),
            "next_planned": [a.split(":", 1)[1] for a in f_dry_acts if ":" in a],
            "next_actions": f_dry_acts,
            "blocked_or_wake": f_dry_wakes,
            "on_blocked": f_cfg["on_blocked"],
            "fallback": f_cfg["fallback"],
        }

        # 科研预览（与 _tick_research dry-run 严格同源，真实反映 ahead_years / XOR / 可研过滤）
        r_cfg = st_cfg["research"]
        r_dry_acts = [a["action"] for a in sim_by_dom["research"]["actions"]]
        r_dry_wakes = [w["detail"] for w in sim_by_dom["research"]["wake"]]
        preview["research_preview"] = {
            "free_slots": sum(1 for s in ctx.get("research_slots") or () if not s.get("slot_busy") or s.get("slot_done")),
            "next_planned": [a.split(":", 1)[1].split("@", 1)[0] for a in r_dry_acts if ":" in a],
            "next_actions": r_dry_acts,
            "blocked_or_wake": r_dry_wakes,
            "max_ahead_years": r_cfg["max_ahead_years"],
            "fallback_strategy": r_cfg["fallback_strategy"]["strategy"],
        }

        # XP 策略预览（与 _tick_xp_policy dry-run 严格同源）
        xp_cfg = st_cfg.get("xp_policy") or {}
        xp_dry_acts = [a["action"] for a in (sim_by_dom.get("xp_policy") or {}).get("actions") or ()]
        xp_dry_wakes = [w["detail"] for w in (sim_by_dom.get("xp_policy") or {}).get("wake") or ()]
        preview["xp_preview"] = {
            "army_xp": ctx.get("army_xp"),
            "navy_xp": ctx.get("navy_xp"),
            "air_xp": ctx.get("air_xp"),
            "planned_upgrades": [u["preset"] for u in (xp_cfg.get("army_template_upgrades") or ())],
            "planned_doctrines": [d["doctrine"] for d in (xp_cfg.get("doctrines") or ())],
            "next_actions": xp_dry_acts,
            "blocked_or_wake": xp_dry_wakes,
        }

        # 预计首个必然醒来节点
        preview["expected_first_wakeup"] = {
            "heartbeat_days": kernel_floor["heartbeat_days"],
            "max_autopilot_days": kernel_floor["max_autopilot_days"],
            "always_wake_on": list(KERNEL_FLOOR_RULES),
        }

    return {
        "ok": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
        "compiled": compiled if not errors else None,
        "preview": preview,
    }


def set_standing_orders(g, profile, dry_run=False, replace=True, tag=None):
    """声明、编译并在通过后激活 Standing Orders。编译存在任何 error 即拒绝激活。"""
    comp = compile_profile(g, profile, tag=tag)
    if not comp["ok"]:
        return {
            "ok": False,
            "blocked_by": "compile_failed",
            "dry_run": bool(dry_run),
            "errors": comp["errors"],
            "warnings": comp["warnings"],
        }
    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "activated": False,
            "warnings": comp["warnings"],
            "preview": comp["preview"],
        }

    ctx = collect_context(g, tag=tag)
    compiled = comp["compiled"]
    ph_idx, active_ph, sticky_floor = resolve_active_phase(compiled, ctx)
    now_raw = ctx.get("date_raw")
    k = _state_key(g, tag=ctx["tag"])

    with _LOCK:
        _load_persisted()
        old = _MEM_STATES.get(k) if not replace else None
        old_q_done = dict((old or {}).get("construction_queue_done") or {})
        init_q_done = _init_construction_queue_done(g, active_ph["profile"] if active_ph else {}, ctx["tag"])
        for qk, qv in init_q_done.items():
            old_q_done.setdefault(qk, qv)
        st = {
            "active": True,
            "profile": compiled,
            "compiled_at_date": ctx.get("date"),
            "started_date_raw": now_raw,
            "last_heartbeat_date_raw": now_raw,
            "last_tick_date_raw": now_raw,
            "current_phase": active_ph["name"] if active_ph else "default",
            "current_phase_index": ph_idx,
            "sticky_floor_index": sticky_floor,
            "days_elapsed": 0.0,
            "ticks": 0,
            "no_progress_streak": 0,
            "last_state_fingerprint": None,
            "last_pp": ctx.get("political_power"),
            "known_ruling_ideology": ctx.get("ruling_ideology"),
            "known_core_laws": _extract_core_laws(ctx.get("active_ideas")),
            "construction_queue_done": old_q_done,
            "construction_failed_states": {},
            "pp_agenda_done": list((old or {}).get("pp_agenda_done") or []),
            "pp_cap_warned_tier": 0.0,
            "xp_upgrades_done": list((old or {}).get("xp_upgrades_done") or []),
            "xp_doctrines_done": list((old or {}).get("xp_doctrines_done") or []),
            "xp_doctrine_fails": {},
            "xp_cap_warned_tier": {},
            "weak_template_warned": False,
            "focus_blocked_reasons": {},
            "focus_started_counts": {},
            "research_slot_done_skip": {},
            "research_fail_counts": {},
            "research_temp_blacklist": [],
            "last_prod_rebalance_date_raw": None,
            "journal": [],
            "last_wake_trigger": None,
            "updated_at": time.time(),
        }
        _append_journal(
            st, ctx.get("date"), st["current_phase"], "system",
            "activated:%s" % compiled["name"], ok=True,
            detail={"phases": [p["name"] for p in compiled["phases"]], "kernel_floor": compiled["kernel_floor"]},
        )
        _MEM_STATES[k] = st
        _save_persisted()

    return {
        "ok": True,
        "dry_run": False,
        "activated": True,
        "name": compiled["name"],
        "active_phase": st["current_phase"],
        "kernel_floor": compiled["kernel_floor"],
        "warnings": comp["warnings"],
        "preview": comp["preview"],
    }


def clear_standing_orders(g, reason="", tag=None):
    """停用并清空当前会话的 Standing Orders。"""
    k = _state_key(g, tag=tag)
    with _LOCK:
        _load_persisted()
        st = _MEM_STATES.get(k)
        was_active = bool(st and st.get("active"))
        if st is not None:
            st["active"] = False
            st["deactivated_reason"] = str(reason or "manual_clear")
            st["updated_at"] = time.time()
            _save_persisted()
    return {"ok": True, "was_active": was_active, "reason": str(reason or "manual_clear")}


def acknowledge_wakeup(g, tag=None, reason=None, extend_max_days=False):
    """模型收到心跳或熔断唤醒并检查完毕后签收放行（0 引擎注入、免前台窗口）。

    默认重置心跳基准时间（last_heartbeat_date_raw）与连续无进展计数（no_progress_streak）；
    若显式传 extend_max_days=True 且已接近/达到 max_autopilot_days，则以当前日期为新起点
    续跑一轮（仍受原 max_autopilot_days 硬上限约束，绝不关闭地板）。
    """
    st = get_active_state(g, tag=tag)
    if not st:
        return {"ok": False, "active": False, "error": "当前会话未激活 Standing Orders"}
    d = _safe_date(g)
    compiled = st["profile"]
    kf = compiled["kernel_floor"]
    extended = False
    with _LOCK:
        if d.get("raw") is not None:
            st["last_heartbeat_date_raw"] = d["raw"]
            if extend_max_days:
                st["started_date_raw"] = d["raw"]
                st["days_elapsed"] = 0.0
                extended = True
        st["no_progress_streak"] = 0
        st["last_wake_trigger"] = None
        if reason:
            _append_journal(
                st, d.get("text"), st.get("current_phase"), "system",
                "ack_wakeup" + (":extend_max" if extended else ""),
                ok=True,
                detail={"reason": str(reason)[:200], "extend_max_days": extended},
            )
        st["updated_at"] = time.time()
        _save_persisted()
    cur_raw = d.get("raw")
    start_raw = st.get("started_date_raw")
    days_el = round(max(0.0, (int(cur_raw) - int(start_raw)) / 24.0), 2) if (cur_raw is not None and start_raw is not None) else 0.0
    return {
        "ok": True,
        "active": True,
        "acked": True,
        "date": d.get("text"),
        "reason": str(reason or "manual_checkin_ok"),
        "extended_max_days": extended,
        "days_elapsed": days_el,
        "days_until_heartbeat": float(kf["heartbeat_days"]),
        "days_until_max": round(max(0.0, float(kf["max_autopilot_days"]) - days_el), 1),
    }


# ===================================================================== 事件分级器
_HIGH_IMPACT_EVENT_EFFECT_KEYWORDS = (
    "declare_war",
    "annex_country",
    "start_civil_war",
    "set_politics",
    "create_faction",
    "add_to_faction",
    "white_peace",
    "uncomplete_national_focus",
    "puppet =",
)


def _norm_effects(effects):
    if not effects:
        return ()
    return tuple(sorted(str(x).strip() for x in effects if str(x).strip()))


def _brief_effects(effects, limit=4):
    out = []
    for x in (effects or ())[:limit]:
        s = " ".join(str(x).split())
        if s:
            out.append(s[:120])
    return out


def _has_high_impact_effects(effects):
    joined = " ".join(str(x).lower() for x in (effects or ()))
    return any(kw in joined for kw in _HIGH_IMPACT_EVENT_EFFECT_KEYWORDS)


def classify_pending_events(g, tag=None):
    """对当前所有待决事件进行纯只读分级：trivial_single / trivial_news / decisive / unresolved_scope。

    2026-11-25 (E6 / E10):
      - 附带 kind / option_name / option_effects / basis 供 journal 完整事后审计；
      - 对纯 0 副作用 news_event（所有非 False 选项效果全空且存在明确 applicable=True 的推荐项）
        安全归为 trivial_equivalent_news，消除因无关国家 trigger 未求值导致的纯新闻误熔断；
      - 对所有多选项 country_event 或含战争/吞并/内战等重大效果的单选项事件严守 decisive 熔断底线。
    """
    out = []
    try:
        import events_win as _ew
        raw = _ew.open_events(g, with_db=True, lang="zh", deep=False)
        ev_list = raw.get("events") if isinstance(raw, dict) else (raw or [])
    except Exception:
        return out
    my_tag = str(tag or "GER").upper()
    for ev in ev_list or ():
        eid = ev.get("event_id") or ev.get("id")
        if not eid:
            continue
        ev_kind = str(ev.get("kind") or "country_event")
        ev_opt_by_idx = {
            o["index"]: o
            for o in (ev.get("options") or ())
            if isinstance(o, dict) and o.get("index") is not None
        }
        sel = None
        try:
            import events_win as _ew
            sel = _ew.select_option(g, event_id=eid, lang="zh", tag=my_tag, write=False)
        except Exception:
            sel = None
        if not isinstance(sel, dict):
            out.append({"event_id": eid, "kind": ev_kind, "title": ev.get("title"), "classification": "decisive", "reason": "uninspected"})
            continue
        if sel.get("unresolved_scope"):
            out.append({
                "event_id": eid,
                "kind": ev_kind,
                "title": ev.get("title"),
                "classification": "unresolved_scope",
                "reason": "unresolved_scope (never write; let 13-day timeout resolve)",
            })
            continue
        opts = sel.get("options") or []
        sel_by_idx = {o["index"]: o for o in opts if isinstance(o, dict) and o.get("index") is not None}
        app_true = [o for o in opts if o.get("applicable") is True]
        has_unknown = any(o.get("applicable") is None for o in opts)

        def _opt_audit(idx_val):
            so = sel_by_idx.get(idx_val) or {}
            eo = ev_opt_by_idx.get(idx_val) or {}
            oname = str(so.get("name") or eo.get("name") or "")[:80]
            oeffs = list(eo.get("effects") or ())
            return oname, _brief_effects(oeffs), oeffs

        # 1) 纯风味新闻（news_event）：即使存在与本国无关的适用性未知选项，只要所有非 False 选项
        #    均无任何游戏效果且存在明确 applicable=True 的推荐选项，即可安全自动放行。
        rec_idx = sel.get("recommended")
        rec_basis = sel.get("recommendation_basis")
        if (
            ev_kind == "news_event"
            and not sel.get("recommendation_blocked")
            and rec_idx is not None
            and rec_basis in ("applicable_positive_tag", "applicable_conditional", "applicable_unconditional", "unique_positive_tag_partial_trigger")
        ):
            rec_opt = sel_by_idx.get(rec_idx) or {}
            non_false_idxs = [o["index"] for o in opts if o.get("applicable") is not False and o.get("index") is not None]
            non_false_effs = [_norm_effects((ev_opt_by_idx.get(i) or {}).get("effects")) for i in non_false_idxs]
            if (rec_opt.get("applicable") is True or rec_basis == "unique_positive_tag_partial_trigger") and all(len(ef) == 0 for ef in non_false_effs):
                oname, obrief, _ = _opt_audit(rec_idx)
                out.append({
                    "event_id": eid,
                    "kind": ev_kind,
                    "title": ev.get("title"),
                    "classification": "trivial_equivalent_news",
                    "auto_index": rec_idx,
                    "option_name": oname,
                    "option_effects": obrief,
                    "basis": rec_basis,
                    "reason": "no-effect news_event (%s)" % rec_basis,
                })
                continue

        if sel.get("recommendation_blocked") or has_unknown:
            out.append({
                "event_id": eid,
                "kind": ev_kind,
                "title": ev.get("title"),
                "classification": "decisive",
                "reason": "options contain unevaluated triggers or blocked recommendation",
            })
            continue
        if len(app_true) == 1:
            idx_one = app_true[0]["index"]
            oname, obrief, oraw = _opt_audit(idx_one)
            if ev_kind != "news_event" and _has_high_impact_effects(oraw):
                out.append({
                    "event_id": eid,
                    "kind": ev_kind,
                    "title": ev.get("title"),
                    "classification": "decisive",
                    "applicable_count": 1,
                    "option_name": oname,
                    "option_effects": obrief,
                    "reason": "single-option non-news event carries high-impact effects",
                })
                continue
            out.append({
                "event_id": eid,
                "kind": ev_kind,
                "title": ev.get("title"),
                "classification": "trivial_single_option",
                "auto_index": idx_one,
                "option_name": oname,
                "option_effects": obrief,
                "basis": "single_applicable",
                "reason": "only 1 applicable option",
            })
            continue
        if len(app_true) > 1 and ev_kind == "news_event":
            # 核对静态 DB 中的 effects 是否完全等价（例如全空纯新闻）
            eff_map = {}
            for o in ev.get("options") or ():
                if o.get("index") is not None:
                    eff_map[o["index"]] = _norm_effects(o.get("effects"))
            eff_sets = {eff_map.get(o["index"], ()) for o in app_true}
            if len(eff_sets) <= 1:
                idx_pick = rec_idx if (rec_idx in {o["index"] for o in app_true}) else app_true[0]["index"]
                oname, obrief, _ = _opt_audit(idx_pick)
                out.append({
                    "event_id": eid,
                    "kind": ev_kind,
                    "title": ev.get("title"),
                    "classification": "trivial_equivalent_news",
                    "auto_index": idx_pick,
                    "option_name": oname,
                    "option_effects": obrief,
                    "basis": "equivalent_news",
                    "reason": "news_event with equivalent effects across all applicable options",
                })
                continue
        out.append({
            "event_id": eid,
            "kind": ev_kind,
            "title": ev.get("title"),
            "classification": "decisive",
            "applicable_count": len(app_true),
            "reason": "multiple distinct applicable options",
        })
    return out


# ===================================================================== 内核熔断地板检查（纯只读）
def check_kernel_floor(g, st, ctx=None, prev_snap=None, cur_snap=None):
    """检查不可关闭的 Kernel Floor 与 interrupts_extra；返回触发列表（非空即必须立即停表唤醒）。"""
    if not st or not st.get("active"):
        return []
    compiled = st["profile"]
    kf = compiled["kernel_floor"]
    ctx = ctx or collect_context(g, tag=compiled.get("tag"))
    ph_idx = int(st.get("current_phase_index") or 0)
    phases = compiled.get("phases") or []
    st_cfg = phases[ph_idx]["profile"] if 0 <= ph_idx < len(phases) else {}

    triggers = []

    # 1) wargoal / CB 出现或正当化消失无 CB
    if prev_snap and cur_snap:
        if "wargoals" in prev_snap and "wargoals" in cur_snap:
            fresh_wg = cur_snap["wargoals"] - prev_snap["wargoals"]
            if fresh_wg:
                triggers.append({
                    "floor": "wargoal_or_cb",
                    "detail": "CB ready: %s" % ", ".join("%s(%s)" % x for x in sorted(fresh_wg)),
                })
        if "justifying" in prev_snap and "justifying" in cur_snap:
            ended_j = prev_snap["justifying"] - cur_snap["justifying"]
            if ended_j:
                cur_wg_t = {x[0] for x in (cur_snap.get("wargoals") or ())}
                cur_en = cur_snap.get("enemies") or frozenset()
                for tgt in sorted(ended_j):
                    if tgt not in cur_wg_t and tgt not in cur_en:
                        triggers.append({
                            "floor": "wargoal_or_cb",
                            "detail": "JUSTIFY_LOST against %s" % tgt,
                        })
                    elif tgt in cur_wg_t:
                        triggers.append({
                            "floor": "wargoal_or_cb",
                            "detail": "justification completed -> CB ready: %s" % tgt,
                        })

    # 2) 开战 / 停战 / 和会开启
    if prev_snap and cur_snap:
        if "enemies" in prev_snap and "enemies" in cur_snap:
            new_war = cur_snap["enemies"] - prev_snap["enemies"]
            end_war = prev_snap["enemies"] - cur_snap["enemies"]
            if new_war:
                triggers.append({"floor": "war_or_peace", "detail": "WAR declared: %s" % ", ".join(sorted(new_war))})
            if end_war:
                triggers.append({"floor": "war_or_peace", "detail": "WAR ended: %s" % ", ".join(sorted(end_war))})
        if cur_snap.get("peace_open") and not prev_snap.get("peace_open"):
            triggers.append({"floor": "war_or_peace", "detail": "peace conference opened"})

    # 3) 待决分歧事件（遵循 event_handling 策略，但任何 decisive 事件不可绕过）
    ev_strat = ((st_cfg.get("events") or {}).get("strategy") or {}).get("strategy", "auto_trivial")
    ev_rows = classify_pending_events(g, tag=ctx["tag"])
    for ev in ev_rows:
        cls = ev["classification"]
        if cls == "decisive":
            triggers.append({
                "floor": "decisive_event",
                "detail": "decisive event %s (%s)" % (ev["event_id"], ev.get("reason")),
            })
        elif ev_strat == "wake_all" and cls in ("trivial_single_option", "trivial_equivalent_news"):
            triggers.append({
                "floor": "decisive_event",
                "detail": "event %s (events.strategy=wake_all)" % ev["event_id"],
            })

    # 4) PP 预算锁被外部击穿
    reserve_pp = float((st_cfg.get("pp_policy") or {}).get("reserve_pp") or 0.0)
    cur_pp = float(ctx.get("political_power") or 0.0)
    prev_pp = st.get("last_pp")
    if reserve_pp > 0 and prev_pp is not None and float(prev_pp) >= reserve_pp and cur_pp < reserve_pp:
        triggers.append({
            "floor": "pp_reserve_breach",
            "detail": "PP dropped below reserve_pp (%.1f -> %.1f < %.1f)" % (float(prev_pp), cur_pp, reserve_pp),
        })

    # 5) 政体或核心法案被外部事件/国策改变
    known_ideo = st.get("known_ruling_ideology")
    cur_ideo = ctx.get("ruling_ideology")
    if known_ideo and cur_ideo and cur_ideo != known_ideo:
        triggers.append({
            "floor": "external_law_or_regime_change",
            "detail": "ruling ideology changed externally: %s -> %s" % (known_ideo, cur_ideo),
        })
    known_laws = st.get("known_core_laws") or {}
    cur_laws = _extract_core_laws(ctx.get("active_ideas"))
    for sl, old_k in known_laws.items():
        new_k = cur_laws.get(sl)
        if new_k and new_k != old_k:
            triggers.append({
                "floor": "external_law_or_regime_change",
                "detail": "core law [%s] changed externally: %s -> %s" % (sl, old_k, new_k),
            })

    # 6) 硬天花板 max_autopilot_days & 强制心跳 heartbeat_days
    now_raw = ctx.get("date_raw")
    start_raw = st.get("started_date_raw")
    hb_raw = st.get("last_heartbeat_date_raw") or start_raw
    if now_raw is not None and start_raw is not None:
        total_days = max(0.0, (int(now_raw) - int(start_raw)) / 24.0)
        if total_days >= float(kf["max_autopilot_days"]):
            triggers.append({
                "floor": "max_autopilot_days",
                "detail": "reached max_autopilot_days ceiling (%.1f / %d days) -- inspect state then call ack_autopilot(extend_max_days=true, reason=...) or set_standing_orders" % (total_days, kf["max_autopilot_days"]),
            })
    if now_raw is not None and hb_raw is not None:
        since_hb = max(0.0, (int(now_raw) - int(hb_raw)) / 24.0)
        if since_hb >= float(kf["heartbeat_days"]):
            triggers.append({
                "floor": "heartbeat_days",
                "detail": "mandatory heartbeat check-in (%.1f / %d days) -- if no changes needed, call ack_autopilot(reason=...) to resume" % (since_hb, kf["heartbeat_days"]),
            })

    # 7) 连续无进展熔断
    if int(st.get("no_progress_streak") or 0) >= int(kf["no_progress_limit"]):
        triggers.append({
            "floor": "no_progress",
            "detail": "no progress for %d consecutive active ticks -- resolve blockage or call ack_autopilot(reason=...)" % int(st["no_progress_streak"]),
        })

    # 8) 燃油枯竭保护与 interrupts_extra 扩展条件
    fl_cfg = st_cfg.get("fuel") or {}
    if fl_cfg.get("wake_on_critical") and ctx.get("fuel") is not None:
        if float(ctx["fuel"]) <= float(fl_cfg.get("critical_threshold") or 50.0):
            triggers.append({
                "floor": "interrupt_extra:fuel_critical",
                "detail": "fuel critical (%.1f <= %.1f)" % (float(ctx["fuel"]), float(fl_cfg.get("critical_threshold") or 50.0)),
            })

    ie = st_cfg.get("interrupts_extra") or {}
    # 9) PP 满仓阶梯保护（E7：即使 pp_policy.agenda 为空或 on_agenda_done=hold 也防止 2000 顶格浪费）
    raw_pp_cap = ie.get("on_pp_near_cap", 1500.0)
    if raw_pp_cap is not False:
        base_cap = 1500.0 if (raw_pp_cap is True or raw_pp_cap is None) else float(raw_pp_cap)
        tiers = sorted({t for t in (base_cap, 1800.0, 1950.0) if t >= base_cap - 1e-6})
        warned_tier = float(st.get("pp_cap_warned_tier") or 0.0)
        if cur_pp < base_cap:
            st["pp_cap_warned_tier"] = 0.0
            warned_tier = 0.0
        else:
            hit_tier = max((t for t in tiers if cur_pp >= t), default=0.0)
            if hit_tier > warned_tier:
                pp_cfg_now = st_cfg.get("pp_policy") or {}
                ag_now = pp_cfg_now.get("agenda") or []
                done_s = set(st.get("pp_agenda_done") or ())
                act_i = set(ctx.get("active_ideas") or ())
                rem_ag = [x for x in ag_now if x.get("key") not in done_s and x.get("key") not in act_i]
                # 仅在议程为空/已全部做完、或 PP 已经极其逼近上限 (>=1800) 时触发提醒
                if (not rem_ag) or cur_pp >= 1800.0:
                    st["pp_cap_warned_tier"] = hit_tier
                    triggers.append({
                        "floor": "interrupt_extra:pp_near_cap",
                        "detail": "PP reached %.0f (>= %.0f / 2000 cap, pending_agenda=%d) -- inspect idea_catalog & decisions for advisors/companies/decisions or call ack_autopilot"
                                  % (cur_pp, hit_tier, len(rem_ag)),
                    })

    if ie.get("on_manpower_below") is not None and float(ctx.get("manpower") or 0.0) < float(ie["on_manpower_below"]):
        triggers.append({
            "floor": "interrupt_extra:manpower_low",
            "detail": "manpower %.0f < %s" % (float(ctx.get("manpower") or 0.0), ie["on_manpower_below"]),
        })
    if ie.get("custom_when") is not None and eval_predicate(ie["custom_when"], ctx):
        triggers.append({
            "floor": "interrupt_extra:custom_when",
            "detail": "custom_when predicate became true",
        })

    return triggers


# ===================================================================== 子系统确定性执行官
def _tick_events(g, st, st_cfg, ctx, dry_run=False):
    """按事件策略自动处理无分歧单选项/等价新闻事件；有分歧事件返回 wake 信号，journal 完整记录选项效果。"""
    out = {"actions": [], "wake": []}
    strat = ((st_cfg.get("events") or {}).get("strategy") or {}).get("strategy", "auto_trivial")
    rows = classify_pending_events(g, tag=ctx["tag"])
    for ev in rows:
        cls = ev["classification"]
        if cls in ("trivial_single_option", "trivial_equivalent_news") and strat == "auto_trivial":
            idx = ev["auto_index"]
            audit = {
                "kind": ev.get("kind"),
                "title": ev.get("title"),
                "option_name": ev.get("option_name"),
                "effects": ev.get("option_effects") or [],
                "basis": ev.get("basis") or cls,
            }
            if dry_run:
                out["actions"].append({
                    "domain": "events",
                    "action": "auto_select:%s#%d" % (ev["event_id"], idx),
                    "ok": True,
                    "dry_run": True,
                    "detail": audit,
                })
                continue
            try:
                import events_win as _ew
                res = _ew.select_option(g, index=idx, event_id=ev["event_id"], lang="zh", tag=ctx["tag"], write=True)
                ok = bool(isinstance(res, dict) and res.get("ok"))
                det = dict(audit)
                if not ok and isinstance(res, dict) and res.get("error"):
                    det["error"] = res.get("error")
                out["actions"].append({
                    "domain": "events",
                    "action": "auto_select:%s#%d" % (ev["event_id"], idx),
                    "ok": ok,
                    "detail": det,
                })
            except Exception as exc:
                det = dict(audit)
                det["error"] = repr(exc)
                out["actions"].append({
                    "domain": "events",
                    "action": "auto_select:%s#%d" % (ev["event_id"], idx),
                    "ok": False,
                    "detail": det,
                })
        elif cls == "decisive" or (strat == "wake_all" and cls != "unresolved_scope"):
            out["wake"].append({"floor": "decisive_event", "detail": "event %s (%s)" % (ev["event_id"], ev.get("reason"))})
    return out


def _tick_focus(g, st, st_cfg, ctx, dry_run=False):
    """按 must -> queue -> fallback 顺序推进国策；默认不静默跳过被阻塞的关键国策。"""
    out = {"actions": [], "wake": []}
    f_cfg = st_cfg.get("focus") or {}
    if not f_cfg.get("enabled") or ctx.get("current_focus"):
        return out

    completed = ctx.get("completed_focuses") or set()
    avoid = set(f_cfg.get("avoid") or ())
    allow_excl = bool(f_cfg.get("allow_exclusive_focus", False))

    try:
        sf = g.selectable_focuses(ctx["tag"]) or {}
    except Exception:
        sf = {}
    sel_rows = sf.get("selectable") or []
    sel_by_key = {(r.get("key") or r.get("id")): r for r in sel_rows if isinstance(r, dict)}
    blocked_list = sf.get("blocked") or []

    def _blocked_reason(fk):
        for b in blocked_list:
            if str(b).startswith(fk):
                return str(b)
        if hasattr(g, "focus_block_reason"):
            try:
                r = g.focus_block_reason(fk, ctx["tag"])
                if r:
                    return r
            except Exception:
                pass
        return "not selectable right now"

    ordered = [k for k in (f_cfg.get("must") or ()) if k not in completed and k not in avoid]
    ordered += [k for k in (f_cfg.get("queue") or ()) if k not in completed and k not in avoid and k not in ordered]

    picked = None
    on_blocked = f_cfg.get("on_blocked", "wake")
    blocked_reasons_state = st.setdefault("focus_blocked_reasons", {})
    started_counts = st.setdefault("focus_started_counts", {})

    for fk in ordered:
        if _is_irreversible_focus(fk) and not (fk in (f_cfg.get("must") or ()) and allow_excl):
            out["wake"].append({"floor": "irreversible_guard", "detail": "refused irreversible focus %s" % fk})
            return out
        # E4: 若显式计划中的国策已被 autopilot 启动 >=2 次却仍未进入 completed_focuses
        # （例如被事件 uncomplete_national_focus 退回），暂停交由模型确认防止无限循环。
        if int(started_counts.get(fk) or 0) >= 2:
            out["wake"].append({
                "floor": "interrupt_extra:focus_repeated_guard",
                "detail": "focus %s was already started %d times yet remains uncompleted (likely reset by event); pausing for review"
                          % (fk, int(started_counts[fk])),
            })
            return out
        if fk in sel_by_key or (not sel_rows and hasattr(g, "set_focus")):
            picked = fk
            break
        why = _blocked_reason(fk)
        prev_why = blocked_reasons_state.get(fk)
        if prev_why != why:
            blocked_reasons_state[fk] = why
            _append_journal(st, ctx.get("date"), st.get("current_phase"), "focus", "blocked:%s" % fk, ok=True, detail=why)
        if on_blocked == "wake":
            out["wake"].append({"floor": "interrupt_extra:focus_blocked", "detail": "focus %s blocked: %s" % (fk, why)})
            return out
        if on_blocked == "wait":
            # 显式等待条件就绪，跳过本次不选替代国策
            return out
        # on_blocked == "skip": 记录日志并尝试下一项
        _append_journal(st, ctx.get("date"), st.get("current_phase"), "focus", "skipped_blocked:%s" % fk, ok=True, detail=why)

    skipped_repeated = []
    if picked is None and f_cfg.get("fallback") == "tier_score":
        tier_pref = list(f_cfg.get("tier_preference") or ())
        cands = []
        for r in sel_rows:
            rk = r.get("key") or r.get("id")
            if not rk or rk in avoid or _is_irreversible_focus(rk):
                continue
            # E4: 在 tier_score 自动兜底中跳过曾由 autopilot 启动过但又被事件退回/可重复的国策
            # （如核心圈满员后被 GER_inner_circle.01 选拒绝退回的 GER_ascension_of_todt），杜绝连点 3 次死循环。
            if int(started_counts.get(rk) or 0) >= 1:
                skipped_repeated.append(rk)
                continue
            cands.append(r)
        if skipped_repeated and not dry_run:
            _append_journal(
                st, ctx.get("date"), st.get("current_phase"), "focus",
                "skip_repeated:%s" % ",".join(skipped_repeated[:4]),
                ok=True,
                detail="skipped previously started yet uncompleted focus(es) in tier_score fallback",
            )
        if cands:
            def _rank(r):
                t = r.get("tier") or ""
                ti = tier_pref.index(t) if t in tier_pref else len(tier_pref)
                return (ti, -int(r.get("score") or 0), str(r.get("key") or r.get("id")))
            cands.sort(key=_rank)
            picked = cands[0].get("key") or cands[0].get("id")

    if picked is None:
        if skipped_repeated:
            out["wake"].append({
                "floor": "interrupt_extra:focus_repeated_guard",
                "detail": "only previously-started uncompleted focus(es) remain selectable: %s" % ", ".join(skipped_repeated[:5]),
            })
        elif (st_cfg.get("interrupts_extra") or {}).get("on_focus_queue_empty", True):
            out["wake"].append({"floor": "interrupt_extra:focus_queue_empty", "detail": "no planned focus selectable"})
        return out

    if dry_run:
        out["actions"].append({"domain": "focus", "action": "set_focus:%s" % picked, "ok": True, "dry_run": True})
        return out
    try:
        res = g.set_focus(picked, ctx["tag"])
        ok = bool(isinstance(res, dict) and res.get("ok"))
        if ok:
            started_counts[picked] = int(started_counts.get(picked) or 0) + 1
        out["actions"].append({
            "domain": "focus",
            "action": "set_focus:%s" % picked,
            "ok": ok,
            "detail": (res.get("reason") or res.get("error")) if isinstance(res, dict) and not ok else None,
        })
    except Exception as exc:
        out["actions"].append({"domain": "focus", "action": "set_focus:%s" % picked, "ok": False, "detail": repr(exc)})
    return out


def _tick_research(g, st, st_cfg, ctx, dry_run=False):
    """按 queue -> fallback_strategy 填补空闲科研槽，严格校验 max_ahead_years、never、XOR 与失败退避。

    2026-11-25 (E1 根因修复):
      1. `active` 包含所有槽内科技（含刚 100% 尚未清槽的 `slot_done`），杜绝重复选择已完成科技；
      2. 区分真实空槽 (`not slot_busy`) 与瞬态完成槽 (`slot_busy and slot_done`)：后者尝试替换
         一旦失败即记入 `research_slot_done_skip` 等待游戏引擎次日自然清槽，且绝不冤枉拉黑合法科技；
      3. 真实空槽上某科技连续内容级拒绝达到 2 次（如缺前置/特殊工程门控）时记入会话临时黑名单
         `research_temp_blacklist`，且同槽内立即顺延尝试下一候选（每槽上限 3 次）；环境错误立刻中止。
    """
    out = {"actions": [], "wake": []}
    r_cfg = st_cfg.get("research") or {}
    if not r_cfg.get("enabled"):
        return out

    slots = ctx.get("research_slots") or []
    slot_done_skip = st.setdefault("research_slot_done_skip", {})
    fail_counts = st.setdefault("research_fail_counts", {})
    temp_bl_list = st.setdefault("research_temp_blacklist", [])
    temp_bl = set(temp_bl_list)

    # 清理已经清槽或切换了科技指针的 slot_done 临时跳过记录
    free_slot_objs = []
    for s in slots:
        sid = s.get("slot")
        sid_k = str(sid)
        busy = bool(s.get("slot_busy"))
        done = bool(s.get("slot_done"))
        done_sig = str(s.get("tech_ptr") or s.get("researching") or "done")
        if not busy:
            slot_done_skip.pop(sid_k, None)
            free_slot_objs.append({"slot": sid, "is_slot_done": False, "done_sig": None})
        elif done:
            if slot_done_skip.get(sid_k) == done_sig:
                continue  # 引擎尚未清槽，等下一游戏日自然释放，避免死循环与误触发 no_progress
            free_slot_objs.append({"slot": sid, "is_slot_done": True, "done_sig": done_sig})
        else:
            slot_done_skip.pop(sid_k, None)

    if not free_slot_objs:
        return out

    researched = set(ctx.get("researched") or ())
    # 关键：包含 slot_done 槽中的科技，绝不把刚 100% 完成的科技重新放进候选！
    active = {s.get("researching") for s in slots if s.get("researching")}
    never = set(r_cfg.get("never") or ()) | temp_bl
    max_ahead = float(r_cfg.get("max_ahead_years", 0.5))

    # 拉取带年份的详细可研科技目录
    avail_detailed = []
    if hasattr(g, "available_research_detailed"):
        try:
            avail_detailed = g.available_research_detailed(ctx["tag"], max_ahead_years=max_ahead) or []
        except Exception:
            avail_detailed = []
    if not avail_detailed:
        # 兼容极简 FakeGame（仅有 available_research）
        try:
            import techdata as _td
            defs = _td.load() or {}
            yr_now = int(ctx.get("year") or 1936)
            mo_now = int(ctx.get("month") or 1)
            cur_frac = yr_now + (max(1, mo_now) - 1) / 12.0
            for nm in g.available_research(ctx["tag"]) or ():
                ti = _td.tech_info(nm, defs=defs) or {"start_year": yr_now, "category": "other", "xor": []}
                if ti.get("special_project_gated"):
                    continue
                ahead = max(0.0, float(ti["start_year"]) - cur_frac)
                if ahead <= max_ahead + 1e-6:
                    avail_detailed.append({
                        "key": nm,
                        "start_year": ti["start_year"],
                        "ahead_years": round(ahead, 2),
                        "category": ti["category"],
                        "xor": ti["xor"],
                        "score": 0,
                    })
        except Exception:
            avail_detailed = []

    avail_map = {r["key"]: r for r in avail_detailed if r.get("key")}
    fb = r_cfg.get("fallback_strategy") or {"strategy": "none", "params": {}}

    def _xor_ok(tk):
        info = avail_map.get(tk) or {}
        xors = set(info.get("xor") or ())
        return not (xors & (researched | active))

    def _next_candidate(tried_this_tick):
        for tk in r_cfg.get("queue") or ():
            if tk in researched or tk in active or tk in never or tk in tried_this_tick:
                continue
            if tk not in avail_map:
                continue
            if not _xor_ok(tk):
                continue
            return tk
        if fb["strategy"] != "none":
            cands = [
                r for r in avail_detailed
                if r["key"] not in researched and r["key"] not in active
                and r["key"] not in never and r["key"] not in tried_this_tick
                and _xor_ok(r["key"])
            ]
            if cands:
                if fb["strategy"] == "industry_first":
                    cat_ord = {"industry": 0, "computing": 1, "infantry": 2, "artillery": 3, "support": 4, "armor": 5, "air": 6, "naval": 7}
                    cands.sort(key=lambda r: (cat_ord.get(r.get("category"), 9), float(r.get("ahead_years") or 0.0), -int(r.get("score") or 0), r["key"]))
                else:
                    pref_cats = set((fb.get("params") or {}).get("prefer_categories") or ())
                    cands.sort(key=lambda r: (
                        0 if (pref_cats and r.get("category") in pref_cats) else 1,
                        -int(r.get("score") or 0),
                        float(r.get("ahead_years") or 0.0),
                        r["key"],
                    ))
                return cands[0]["key"]
        return None

    _ENV_FAIL_CODES = frozenset({"foreground", "process_gone", "timeout", "injector_error"})
    tried_this_tick = set()
    env_aborted = False

    for slot_meta in free_slot_objs:
        if env_aborted:
            break
        slot_id = slot_meta["slot"]
        is_done_slot = slot_meta["is_slot_done"]
        max_slot_tries = 1 if is_done_slot else 3
        filled_slot = False

        for _try_i in range(max_slot_tries):
            pick = _next_candidate(tried_this_tick)
            if pick is None:
                if not is_done_slot and (st_cfg.get("interrupts_extra") or {}).get("on_research_queue_empty", True):
                    out["wake"].append({
                        "floor": "interrupt_extra:research_queue_empty",
                        "detail": "research slot %s idle and no eligible tech in queue" % slot_id,
                    })
                env_aborted = True
                break

            if dry_run:
                active.add(pick)
                out["actions"].append({
                    "domain": "research",
                    "action": "start_research:%s@slot%s" % (pick, slot_id),
                    "ok": True,
                    "dry_run": True,
                })
                filled_slot = True
                break

            tried_this_tick.add(pick)
            try:
                res = g.start_research(pick, slot=slot_id, tag=ctx["tag"])
            except Exception as exc:
                res = {"ok": False, "blocked_by": "exception", "error": repr(exc)}

            ok = bool(isinstance(res, dict) and res.get("ok"))
            blocked_by = res.get("blocked_by") if isinstance(res, dict) else None
            fail_cls = res.get("failure_class") if isinstance(res, dict) else None
            err_msg = res.get("error") if isinstance(res, dict) else None
            detail = None
            if not ok:
                detail = {
                    "blocked_by": blocked_by or "rejected",
                    "failure_class": fail_cls or "content",
                    "error": err_msg or "start_research refused",
                    "slot_done_transient": bool(is_done_slot),
                }

            out["actions"].append({
                "domain": "research",
                "action": "start_research:%s@slot%s" % (pick, slot_id),
                "ok": ok,
                "detail": detail,
            })

            if ok:
                active.add(pick)
                fail_counts.pop(pick, None)
                slot_done_skip.pop(str(slot_id), None)
                filled_slot = True
                break

            # 环境错误（前台焦点丢失/进程消失/超时）：立即中止，不拉黑任何科技
            if fail_cls == "environment" or blocked_by in _ENV_FAIL_CODES:
                env_aborted = True
                break

            # 瞬态 slot_done 槽拒绝替换：退避该槽等待引擎次日自然清槽，绝不冤枉拉黑科技！
            if is_done_slot:
                slot_done_skip[str(slot_id)] = slot_meta["done_sig"]
                _append_journal(
                    st, ctx.get("date"), st.get("current_phase"), "research",
                    "defer_slot_done:slot%s" % slot_id, ok=True,
                    detail="slot %s still holding finished tech (%s); waiting for engine clear" % (slot_id, slot_meta["done_sig"]),
                )
                break

            # 真实空槽上的内容拒绝：计数退避，连拒 2 次进会话临时黑名单并同槽立即换下一个候选
            cnt = int(fail_counts.get(pick) or 0) + 1
            fail_counts[pick] = cnt
            if cnt >= 2 or blocked_by in ("already_researched", "mutually_exclusive", "unknown_tech"):
                if pick not in temp_bl:
                    temp_bl.add(pick)
                    temp_bl_list.append(pick)
                    never.add(pick)
                    _append_journal(
                        st, ctx.get("date"), st.get("current_phase"), "research",
                        "temp_blacklist:%s" % pick, ok=True,
                        detail={"blocked_by": blocked_by or "rejected", "fails": cnt, "error": err_msg},
                    )
        if not filled_slot and is_done_slot:
            # 瞬态未清槽的退避不构成整轮阻塞，继续检查其余真实空槽
            continue
    return out


def _tick_pp_policy(g, st, st_cfg, ctx, dry_run=False):
    """按 reserve_pp 硬锁与 strict_agenda / greedy_agenda 执行政治法案、顾问与决议议程。"""
    out = {"actions": [], "wake": []}
    pp_cfg = st_cfg.get("pp_policy") or {}
    if not pp_cfg.get("enabled"):
        return out

    agenda = pp_cfg.get("agenda") or []
    if not agenda:
        return out

    reserve = float(pp_cfg.get("reserve_pp") or 0.0)
    strat = (pp_cfg.get("strategy") or {}).get("strategy", "strict_agenda")
    pp_now = float(ctx.get("political_power") or 0.0)
    active_ideas = set(ctx.get("active_ideas") or ())
    done_list = st.setdefault("pp_agenda_done", [])
    done_set = set(done_list)

    pending = [it for it in agenda if it["key"] not in done_set and it["key"] not in active_ideas]
    if not pending:
        if pp_cfg.get("on_agenda_done") == "save_and_wake" and pp_now >= reserve + 150.0:
            out["wake"].append({"floor": "interrupt_extra:pp_agenda_done", "detail": "PP agenda completed; banked PP=%.0f >= reserve+150" % pp_now})
        return out

    for item in pending:
        # 谓词前提
        if item.get("when") is not None and not eval_predicate(item["when"], ctx):
            if strat == "strict_agenda":
                break
            continue

        cost = float(item.get("cost") or 150)
        # 硬预算锁：绝不花破 reserve_pp！
        if pp_now - cost < reserve - 1e-6:
            pp_daily = ctx.get("political_power_daily")
            # 正常攒 PP（pp_daily > 0）时不打断巡航；仅在 PP 停滞/负增长导致数学上永不可能攒够时唤醒提醒
            if pp_daily is not None and float(pp_daily) <= 0.0:
                out["wake"].append({
                    "floor": "interrupt_extra:pp_stalled",
                    "detail": "PP daily gain is %.2f (<=0); cannot reach %s cost %.0f + reserve %.0f (now %.0f)"
                              % (float(pp_daily), item["key"], cost, reserve, pp_now),
                })
            if strat == "strict_agenda":
                break
            continue

        kind = item["kind"]
        key = item["key"]
        if dry_run:
            pp_now -= cost
            out["actions"].append({"domain": "pp_policy", "action": "%s:%s" % (kind, key), "ok": True, "dry_run": True})
            continue

        try:
            if kind == "advisor":
                res = g.hire_advisor(key, ctx["tag"], slot_override=item.get("slot"))
            elif kind in ("law", "idea"):
                res = g.add_idea(key, ctx["tag"])
            elif kind == "decision":
                res = g.select_decision(key, ctx["tag"])
            else:
                res = {"ok": False, "error": "unsupported kind %r" % kind}
            ok = bool(isinstance(res, dict) and res.get("ok"))
            out["actions"].append({
                "domain": "pp_policy",
                "action": "%s:%s" % (kind, key),
                "ok": ok,
                "detail": (res.get("reason") or res.get("error")) if isinstance(res, dict) and not ok else None,
            })
            if ok:
                done_list.append(key)
                done_set.add(key)
                active_ideas.add(key)
                # 同步登记自有法案变更，避免误报 external_law_or_regime_change
                st["known_core_laws"] = _extract_core_laws(active_ideas)
                try:
                    pp_now = float((g.politics(ctx["tag"]) or {}).get("political_power", pp_now - cost))
                except Exception:
                    pp_now = max(0.0, pp_now - cost)
                st["last_pp"] = pp_now
            else:
                if strat == "strict_agenda":
                    break
        except Exception as exc:
            out["actions"].append({"domain": "pp_policy", "action": "%s:%s" % (kind, key), "ok": False, "detail": repr(exc)})
            if strat == "strict_agenda":
                break

    return out


def _tick_xp_policy(g, st, st_cfg, ctx, dry_run=False):
    """第 9 子系统（E3）：自动执行陆军经典编制升级、显式大方向学说解锁，以及弱编/三军 XP 囤废硬兜底唤醒。"""
    out = {"actions": [], "wake": []}
    xp_cfg = st_cfg.get("xp_policy") or {}
    if not xp_cfg.get("enabled", True):
        return out

    army_xp = ctx.get("army_xp")
    navy_xp = ctx.get("navy_xp")
    air_xp = ctx.get("air_xp")
    xp_by_svc = {"army": army_xp, "navy": navy_xp, "air": air_xp}
    spent_services = set()

    # 1) 陆军经典编制预设升级（复用 macros.upgrade_template_preset，自动算缺口并从超储线挪厂）
    done_upgs = st.setdefault("xp_upgrades_done", [])
    upgs = xp_cfg.get("army_template_upgrades") or []
    pending_inf_upg = False
    if upgs:
        import macros as _mc
        for item in upgs:
            uid = "%s@%s" % (item["preset"], item.get("template") or "auto")
            if uid in done_upgs:
                continue
            if item["preset"].startswith("inf_"):
                pending_inf_upg = True
            if item.get("when") is not None and not eval_predicate(item["when"], ctx):
                continue
            if army_xp is None or float(army_xp) < float(item.get("min_xp") or 0.0):
                continue
            try:
                prev = _mc.upgrade_template_preset(
                    g,
                    preset=item["preset"],
                    template=item.get("template"),
                    tag=ctx["tag"],
                    auto_rebalance=bool(item.get("auto_rebalance", True)),
                    dry_run=True,
                )
            except Exception:
                prev = {"ok": False}
            if not isinstance(prev, dict) or not prev.get("ok"):
                continue
            if prev.get("already_matches"):
                if not dry_run:
                    done_upgs.append(uid)
                continue
            est_xp = float(prev.get("estimated_xp_cost") or 0.0)
            if float(army_xp) < max(float(item.get("min_xp") or 0.0), est_xp):
                continue
            tpl_lbl = prev.get("template") or item.get("template") or "auto"
            if dry_run:
                out["actions"].append({
                    "domain": "xp_policy",
                    "action": "upgrade_template:%s(%s)" % (item["preset"], tpl_lbl),
                    "ok": True,
                    "dry_run": True,
                    "detail": {"estimated_xp": est_xp, "auto_rebalance": bool(item.get("auto_rebalance", True))},
                })
                spent_services.add("army")
                army_xp = max(0.0, float(army_xp) - est_xp)
                xp_by_svc["army"] = army_xp
                continue
            try:
                res = _mc.upgrade_template_preset(
                    g,
                    preset=item["preset"],
                    template=item.get("template"),
                    tag=ctx["tag"],
                    auto_rebalance=bool(item.get("auto_rebalance", True)),
                    dry_run=False,
                )
                ok = bool(isinstance(res, dict) and res.get("ok"))
                reb = res.get("production_rebalance") if isinstance(res, dict) else None
                det = {
                    "template": res.get("template") if isinstance(res, dict) else tpl_lbl,
                    "estimated_xp": est_xp,
                    "rebalanced_factories": (reb or {}).get("allocated_factories") if isinstance(reb, dict) else None,
                }
                if not ok and isinstance(res, dict):
                    det["error"] = res.get("error")
                out["actions"].append({
                    "domain": "xp_policy",
                    "action": "upgrade_template:%s(%s)" % (item["preset"], det["template"]),
                    "ok": ok,
                    "detail": det,
                })
                if ok:
                    done_upgs.append(uid)
                    st["weak_template_warned"] = False
                    spent_services.add("army")
                    army_xp = max(0.0, float(army_xp) - est_xp)
                    xp_by_svc["army"] = army_xp
            except Exception as exc:
                out["actions"].append({
                    "domain": "xp_policy",
                    "action": "upgrade_template:%s" % item["preset"],
                    "ok": False,
                    "detail": repr(exc),
                })

    # 2) 显式声明的大方向学说解锁（绝不盲目 cheapest_first 乱锁路线）
    done_docs = st.setdefault("xp_doctrines_done", [])
    doc_fails = st.setdefault("xp_doctrine_fails", {})
    for item in xp_cfg.get("doctrines") or ():
        doc = item["doctrine"]
        svc = item.get("service") or _DOCTRINE_SERVICE.get(doc, "army")
        if doc in done_docs or int(doc_fails.get(doc) or 0) >= 2:
            continue
        if item.get("when") is not None and not eval_predicate(item["when"], ctx):
            continue
        cur_svc_xp = xp_by_svc.get(svc)
        need_xp = float(item.get("min_xp") or 100.0)
        if cur_svc_xp is None or float(cur_svc_xp) < need_xp:
            continue
        if not hasattr(g, "unlock_doctrine"):
            continue
        if dry_run:
            out["actions"].append({
                "domain": "xp_policy",
                "action": "unlock_doctrine:%s" % doc,
                "ok": True,
                "dry_run": True,
                "detail": {"service": svc, "min_xp": need_xp},
            })
            spent_services.add(svc)
            xp_by_svc[svc] = max(0.0, float(cur_svc_xp) - need_xp)
            continue
        try:
            res = g.unlock_doctrine(doc, ctx["tag"])
            ok = bool(isinstance(res, dict) and res.get("ok"))
            out["actions"].append({
                "domain": "xp_policy",
                "action": "unlock_doctrine:%s" % doc,
                "ok": ok,
                "detail": (res.get("error") or res.get("note")) if isinstance(res, dict) and not ok else {"service": svc},
            })
            if ok:
                done_docs.append(doc)
                doc_fails.pop(doc, None)
                spent_services.add(svc)
                xp_by_svc[svc] = max(0.0, float(cur_svc_xp) - need_xp)
            else:
                doc_fails[doc] = int(doc_fails.get(doc) or 0) + 1
        except Exception as exc:
            doc_fails[doc] = int(doc_fails.get(doc) or 0) + 1
            out["actions"].append({
                "domain": "xp_policy",
                "action": "unlock_doctrine:%s" % doc,
                "ok": False,
                "detail": repr(exc),
            })

    # 3) 弱编硬兜底提醒：陆军 XP 充足但未配置升级且主力步兵仍是 0 炮弱编 -> 主动提醒一次
    wake_weak = float(xp_cfg.get("wake_on_weak_template_xp") or 0.0)
    if (
        wake_weak > 0
        and not pending_inf_upg
        and "army" not in spent_services
        and xp_by_svc.get("army") is not None
        and float(xp_by_svc["army"]) >= wake_weak
        and not st.get("weak_template_warned")
        and hasattr(g, "division_templates")
        and hasattr(g, "template_composition")
    ):
        try:
            import macros as _mc
            tpl_nm, _ = _mc._pick_template_for_role(g, ctx["tag"], "infantry")
            comp = _mc._read_template_comp(g, tpl_nm, ctx["tag"]) if tpl_nm else {}
            line_b = comp.get("line") or {}
            inf_n = int(line_b.get("infantry") or 0)
            art_n = int(line_b.get("artillery_brigade") or 0)
            if tpl_nm and inf_n >= 6 and art_n == 0:
                if not dry_run:
                    st["weak_template_warned"] = True
                out["wake"].append({
                    "floor": "interrupt_extra:weak_infantry_template",
                    "detail": "main infantry '%s' is still %d-inf 0-art with army_xp=%.1f -- run upgrade_template_preset(preset='inf_9_3', auto_rebalance=true) or configure xp_policy.army_template_upgrades"
                              % (tpl_nm, inf_n, float(xp_by_svc["army"])),
                })
        except Exception:
            pass

    # 4) 三军 XP 囤废阶梯兜底提醒（150 / 280 / 400 / 480，越阶触发、回落重置，绝不死锁）
    wake_cap = float(xp_cfg.get("wake_on_xp_cap") or 0.0)
    if wake_cap > 0:
        cap_tiers = sorted({t for t in (wake_cap, 280.0, 400.0, 480.0) if t >= wake_cap - 1e-6})
        warned_map = st.setdefault("xp_cap_warned_tier", {})
        for svc in ("army", "navy", "air"):
            val = xp_by_svc.get(svc)
            if val is None:
                continue
            fv = float(val)
            if fv < wake_cap:
                if not dry_run:
                    warned_map[svc] = 0.0
                continue
            if svc in spent_services:
                continue
            hit = max((t for t in cap_tiers if fv >= t), default=0.0)
            prev_t = float(warned_map.get(svc) or 0.0)
            if hit > prev_t:
                if not dry_run:
                    warned_map[svc] = hit
                out["wake"].append({
                    "floor": "interrupt_extra:xp_cap",
                    "detail": "%s_xp reached %.1f (>= %.0f / 500 cap) with no xp_policy action -- spend XP on template/doctrine/design or call ack_autopilot"
                              % (svc, fv, hit),
                })

    return out


def _tick_construction(g, st, st_cfg, ctx, dry_run=False):
    """先消化显式 construction.queue，再按显式谓词树 rules 与 state_strategy 补齐建造队列。

    2026-11-25 (E8 / E11):
      - 维护会话级失败州冷却表 `construction_failed_states`（30 天冷却）与本 tick 局部剔除集，
        单州入队失败后立即从该建筑的候选州剔除，杜绝反复对满槽/受限州撞墙失败；
      - 补齐 `interrupts_extra.on_construction_queue_empty` / `on_construction_queue_below`：
        当显式配额与 rules 全部耗尽（或无可用州）导致队列空/低于阈值时主动熔断唤醒模型。
    """
    out = {"actions": [], "wake": []}
    c_cfg = st_cfg.get("construction") or {}
    if not c_cfg.get("enabled"):
        return out

    target_len = int(c_cfg.get("target_queue_len") or 6)
    try:
        q_now = g.construction_queue(ctx["tag"]) or []
    except Exception:
        q_now = []
    cur_len = len(q_now)
    if cur_len >= target_len:
        return out

    # 统计当前队列已占用的州槽位与同建筑数
    queued_by_state = {}
    queued_by_bldg = {}
    for row in q_now:
        sid = row.get("state")
        b = row.get("building")
        if sid is not None:
            queued_by_state[int(sid)] = queued_by_state.get(int(sid), 0) + 1
        if b:
            queued_by_bldg[b] = queued_by_bldg.get(b, 0) + 1

    # 清理已超过 30 天冷却期的失败州记录
    now_raw = ctx.get("date_raw")
    failed_states = st.setdefault("construction_failed_states", {})
    if now_raw is not None and failed_states:
        cd_h = 30 * 24
        for fk in list(failed_states.keys()):
            ts_h = failed_states.get(fk)
            if ts_h is None or (int(now_raw) - int(ts_h)) >= cd_h or int(now_raw) < int(ts_h):
                failed_states.pop(fk, None)

    failed_this_tick = set()
    q_done = st.setdefault("construction_queue_done", {})
    strat = c_cfg["state_strategy"]

    def _excluded_for(bname):
        ex = set((strat.get("params") or {}).get("exclude_states") or ())
        pref = "%s:" % bname
        for fk in failed_states:
            if fk.startswith(pref):
                try:
                    ex.add(int(fk[len(pref):]))
                except ValueError:
                    pass
        for fb, fsid in failed_this_tick:
            if fb == bname:
                ex.add(int(fsid))
        return ex

    def _add_one(bname, explicit_sid=None):
        nonlocal cur_len
        if explicit_sid is not None:
            sid_e = int(explicit_sid)
            if (bname, sid_e) in failed_this_tick or ("%s:%d" % (bname, sid_e)) in failed_states:
                return False
            sids = [sid_e]
        else:
            ex_all = _excluded_for(bname)
            eff_strat = {
                "strategy": strat["strategy"],
                "params": dict(strat.get("params") or {}, exclude_states=sorted(ex_all)),
            }
            cands = _select_states_for_building(g, ctx["tag"], bname, eff_strat, count=8, already_queued=queued_by_state)
            sids = [c["state"] for c in cands]
        for sid in sids:
            if dry_run:
                cur_len += 1
                queued_by_state[sid] = queued_by_state.get(sid, 0) + 1
                queued_by_bldg[bname] = queued_by_bldg.get(bname, 0) + 1
                out["actions"].append({"domain": "construction", "action": "add:%s@state%d" % (bname, sid), "ok": True, "dry_run": True})
                return True
            try:
                res = g.add_construction(sid, bname, tag=ctx["tag"])
                ok = bool(isinstance(res, dict) and res.get("ok"))
                out["actions"].append({
                    "domain": "construction",
                    "action": "add:%s@state%d" % (bname, sid),
                    "ok": ok,
                    "detail": res.get("error") if isinstance(res, dict) and not ok else None,
                })
                if ok:
                    cur_len += 1
                    queued_by_state[sid] = queued_by_state.get(sid, 0) + 1
                    queued_by_bldg[bname] = queued_by_bldg.get(bname, 0) + 1
                    return True
                # 入队失败：立即把 (building, state) 记入冷却黑名单，杜绝重复撞墙
                failed_this_tick.add((bname, int(sid)))
                failed_states["%s:%d" % (bname, int(sid))] = int(now_raw) if now_raw is not None else 0
            except Exception as exc:
                failed_this_tick.add((bname, int(sid)))
                failed_states["%s:%d" % (bname, int(sid))] = int(now_raw) if now_raw is not None else 0
                out["actions"].append({"domain": "construction", "action": "add:%s@state%d" % (bname, sid), "ok": False, "detail": repr(exc)})
        return False

    # 1) 显式 queue 优先（支持显式谓词树 when / until；条件不满足或达到 until 时跳过该项进下一项）
    for qitem in c_cfg.get("queue") or ():
        if cur_len >= target_len:
            break
        w_ok = True if qitem.get("when") is None else eval_predicate(qitem["when"], ctx)
        u_ok = False if qitem.get("until") is None else eval_predicate(qitem["until"], ctx)
        if not (w_ok and not u_ok):
            continue
        qid = qitem["id"]
        done_n = int(q_done.get(qid) or 0)
        want_n = int(qitem["count"])
        while done_n < want_n and cur_len < target_len:
            if _add_one(qitem["building"], explicit_sid=qitem.get("state")):
                done_n += 1
                if not dry_run:
                    q_done[qid] = done_n
            else:
                break

    # 2) 按显式谓词树 rules 补齐至 target_len
    rules = c_cfg.get("rules") or []
    guard_steps = 0
    while cur_len < target_len and rules and guard_steps < target_len:
        guard_steps += 1
        chosen_bldg = None
        for rl in rules:
            w_ok = True if rl.get("when") is None else eval_predicate(rl["when"], ctx)
            u_ok = False if rl.get("until") is None else eval_predicate(rl["until"], ctx)
            if not (w_ok and not u_ok):
                continue
            cap = rl.get("max_in_queue")
            if cap is not None and queued_by_bldg.get(rl["building"], 0) >= int(cap):
                continue
            chosen_bldg = rl["building"]
            break
        if not chosen_bldg:
            break
        if not _add_one(chosen_bldg):
            break

    # 3) E8: 队列耗尽或跌破低水位且无可用 queue/rules/州可补时主动熔断唤醒
    if cur_len < target_len:
        ie = st_cfg.get("interrupts_extra") or {}
        on_empty = ie.get("on_construction_queue_empty", True)
        below_n = ie.get("on_construction_queue_below")
        if on_empty and cur_len == 0:
            out["wake"].append({
                "floor": "interrupt_extra:construction_queue_empty",
                "detail": "construction queue is empty and no eligible queue/rules/state left to add",
            })
        elif below_n is not None and cur_len < int(below_n):
            out["wake"].append({
                "floor": "interrupt_extra:construction_queue_empty",
                "detail": "construction queue len %d < %d and no eligible queue/rules/state left to add"
                          % (cur_len, int(below_n)),
            })

    return out


def _prod_group(equipment):
    eq = str(equipment or "").lower()
    if "infantry_equipment" in eq:
        return "inf"
    if "artillery" in eq or "support_equipment" in eq or "anti_air" in eq or "anti_tank" in eq:
        return "art"
    if any(k in eq for k in ("tank", "motorized", "mechanized", "armored_car")):
        return "armor"
    if any(k in eq for k in ("fighter", "cas", "bomber", "plane")):
        return "air"
    return "other"


def _tick_production(g, st, st_cfg, ctx, dry_run=False):
    """按 upgrade_strategy（payback）换代，再按 ratio_hysteresis 分配空闲军工与船坞。"""
    out = {"actions": [], "wake": []}
    p_cfg = st_cfg.get("production") or {}
    if not p_cfg.get("enabled"):
        return out

    try:
        lines = g.production_lines(ctx["tag"]) or []
    except Exception:
        lines = []
    mil = [l for l in lines if l.get("class") == "CMilitaryProductionLine"]

    # 1) 换代策略
    upg_cfg = p_cfg.get("upgrade_strategy") or {"strategy": "never", "params": {}}
    if upg_cfg["strategy"] == "payback" and mil:
        max_pb = float((upg_cfg.get("params") or {}).get("max_payback_days", 45.0))
        min_days_sup = float((upg_cfg.get("params") or {}).get("min_stockpile_days", 30.0))
        stock_totals = {}
        try:
            st_raw = g.equipment_stockpile(ctx["tag"]) or {}
            stock_totals = st_raw.get("totals_by_equipment") or {}
        except Exception:
            stock_totals = {}

        for ln in mil:
            if not ln.get("obsolete") or not ln.get("upgrade_to"):
                continue
            pb = ln.get("payback_days")
            if pb is None or float(pb) > max_pb:
                continue
            eq = str(ln.get("equipment") or "")
            out_day = float(ln.get("output_units_per_day") or 0.0)
            have_stock = float(stock_totals.get(eq) or 0.0)
            # 若已有日产数据且设定了安全备货天数护栏，库存不足则暂不换代以防断供
            if min_days_sup > 0 and out_day > 0 and have_stock < out_day * min_days_sup:
                continue
            target_eq = ln["upgrade_to"]
            if dry_run:
                out["actions"].append({"domain": "production", "action": "upgrade:#%s %s->%s" % (ln["index"], eq, target_eq), "ok": True, "dry_run": True})
                continue
            try:
                res = g.set_production_line_equipment(ln["index"], target_eq, tag=ctx["tag"])
                ok = bool(isinstance(res, dict) and res.get("ok"))
                out["actions"].append({
                    "domain": "production",
                    "action": "upgrade:#%s %s->%s" % (ln["index"], eq, target_eq),
                    "ok": ok,
                    "detail": res.get("error") if isinstance(res, dict) and not ok else None,
                })
            except Exception as exc:
                out["actions"].append({"domain": "production", "action": "upgrade:#%s" % ln["index"], "ok": False, "detail": repr(exc)})

    # 2) 军工空闲分配与受控超储抽厂（ratio_hysteresis）
    alloc_cfg = p_cfg.get("strategy") or {"strategy": "keep_current", "params": {}}
    params = alloc_cfg.get("params") or {}
    if alloc_cfg["strategy"] == "ratio_hysteresis" and mil:
        total_mil = int(ctx.get("mil_total") or sum(int(l.get("factories") or 0) for l in mil))
        used_mil = sum(int(l.get("factories") or 0) for l in mil)
        idle_mil = max(0, total_mil - used_mil)
        ratios = params.get("ratios") or {"inf": 0.34, "art": 0.21, "armor": 0.20, "air": 0.25}
        hyst = float(params.get("hysteresis_factories", 1))
        min_facs = params.get("min_factories") or {}

        # 按大类计算每条产线的目标厂数与底线保护
        target = {}
        for grp, ratio in ratios.items():
            glines = [l for l in mil if _prod_group(l.get("equipment")) == grp]
            if not glines:
                continue
            cur_sum = sum(int(l.get("factories") or 0) for l in glines)
            for l in glines:
                share = (int(l.get("factories") or 0) / cur_sum) if cur_sum > 0 else (1.0 / len(glines))
                target[l["index"]] = total_mil * float(ratio) * share

        cur_counts = {l["index"]: int(l.get("factories") or 0) for l in mil}

        def _min_floor(l):
            eq = str(l.get("equipment") or "")
            grp = _prod_group(eq)
            return max(int(min_facs.get(eq, 0)), int(min_facs.get(grp, 0)))

        if idle_mil > 0:
            desired = dict(cur_counts)
            left = idle_mil
            # 先补齐低于 min_factories 的产线
            for l in mil:
                if left <= 0:
                    break
                fl = _min_floor(l)
                if desired[l["index"]] < fl:
                    add = min(left, fl - desired[l["index"]])
                    desired[l["index"]] += add
                    left -= add

            # 再按目标缺口逐厂分配（有缺口优先填缺口；若均达标则优先补给配比最高的大类，不浪费闲置军工）
            while left > 0:
                best = max(
                    mil,
                    key=lambda l: (
                        float(target.get(l["index"], desired[l["index"]])) - desired[l["index"]],
                        float(ratios.get(_prod_group(l.get("equipment")), 0.0)),
                    ),
                )
                sf = float(target.get(best["index"], desired[best["index"]])) - desired[best["index"]]
                if sf <= -hyst:
                    # 所有产线均已超出滞回上限，仍将剩余闲置军工投入最高配比组防 IDLE_MIL
                    best = max(
                        mil,
                        key=lambda l: (
                            float(ratios.get(_prod_group(l.get("equipment")), 0.0)),
                            -desired[l["index"]],
                        ),
                    )
                desired[best["index"]] += 1
                left -= 1

            for l in mil:
                idx_l = l["index"]
                if desired[idx_l] <= cur_counts[idx_l]:
                    continue
                if dry_run:
                    out["actions"].append({
                        "domain": "production",
                        "action": "set_line:#%d %s %d->%d" % (idx_l, l.get("equipment"), cur_counts[idx_l], desired[idx_l]),
                        "ok": True,
                        "dry_run": True,
                    })
                    continue
                try:
                    res = g.set_production_amount(idx_l, desired[idx_l], ctx["tag"])
                    ok = bool(isinstance(res, dict) and res.get("ok"))
                    out["actions"].append({
                        "domain": "production",
                        "action": "set_line:#%d %s %d->%d" % (idx_l, l.get("equipment"), cur_counts[idx_l], desired[idx_l]),
                        "ok": ok,
                        "detail": res.get("error") if isinstance(res, dict) and not ok else None,
                    })
                except Exception as exc:
                    out["actions"].append({"domain": "production", "action": "set_line:#%d" % idx_l, "ok": False, "detail": repr(exc)})

        elif params.get("rebalance_surplus", True) and len(mil) >= 2:
            # 2026-11-25 (E9): 空闲军工为 0 时按冷却期从严重超储产线受控抽厂补紧缺线（先减后加、严守 min_factories）。
            now_raw = ctx.get("date_raw")
            last_reb = st.get("last_prod_rebalance_date_raw")
            cd_h = int(params.get("rebalance_cooldown_days", 60)) * 24
            cd_ok = (last_reb is None or now_raw is None or (int(now_raw) - int(last_reb)) >= cd_h)
            if cd_ok:
                thresh = float(params.get("surplus_ratio_threshold", 2.5))
                stock_t = {}
                try:
                    sr_raw = g.equipment_stockpile(ctx["tag"]) or {}
                    stock_t = sr_raw.get("totals_by_equipment") or {}
                except Exception:
                    stock_t = {}

                surplus_ratios = {}
                divs_n = int(ctx.get("divisions") or 0)
                try:
                    import equipmeta as _em
                    for srow in _em.compute_surplus_lines(lines, stock_t, total_mil, divs_n) or ():
                        if "line" in srow and srow.get("surplus_ratio") is not None:
                            surplus_ratios[int(srow["line"])] = float(srow["surplus_ratio"])
                except Exception:
                    pass
                # 兜底：若 equipmeta 未覆写（或离线 mock 仅提供 totals_by_equipment），对步枪做直接安全储备倍比评估
                inf_safe = max(12000.0, max(divs_n, 24) * 150.0)
                inf_stock = sum(float(v or 0.0) for k, v in stock_t.items() if _prod_group(k) == "inf")
                if inf_stock >= inf_safe * thresh:
                    inf_ratio = round(inf_stock / inf_safe, 2)
                    for l in mil:
                        if _prod_group(l.get("equipment")) == "inf":
                            surplus_ratios[int(l["index"])] = max(surplus_ratios.get(int(l["index"]), 0.0), inf_ratio)

                # 计算可抽厂的严重超储源线与存在明显缺口的非超储接收线
                donors = []
                for l in mil:
                    idx_l = int(l["index"])
                    sr = float(surplus_ratios.get(idx_l) or 0.0)
                    cur_f = cur_counts[idx_l]
                    floor_f = max(1, _min_floor(l))
                    room = cur_f - floor_f
                    if sr >= thresh and room >= 1:
                        # 每次克制最多抽走可抽余额的一半（至少 1 厂），避免效率暴跌
                        max_cut = max(1, room // 2)
                        donors.append((sr, idx_l, l, cur_f, max_cut))

                receivers = []
                for l in mil:
                    idx_l = int(l["index"])
                    if float(surplus_ratios.get(idx_l) or 0.0) >= 1.8:
                        continue
                    cur_f = cur_counts[idx_l]
                    gap = float(target.get(idx_l, cur_f)) - cur_f
                    if gap >= max(1.0, hyst):
                        receivers.append((gap, idx_l, l, cur_f, int(round(gap))))

                if donors and receivers:
                    donors.sort(key=lambda x: (-x[0], -x[4]))
                    receivers.sort(key=lambda x: (-x[0], x[3]))
                    avail_reclaim = sum(d[4] for d in donors)
                    need_total = sum(r[4] for r in receivers)
                    budget = min(avail_reclaim, need_total)
                    if budget > 0:
                        shrink_plan = []
                        rem_s = budget
                        for sr, idx_l, l, cur_f, max_cut in donors:
                            if rem_s <= 0:
                                break
                            cut = min(max_cut, rem_s)
                            shrink_plan.append((idx_l, l, cur_f, cur_f - cut, sr))
                            rem_s -= cut

                        grow_plan = []
                        rem_g = budget
                        for gap, idx_l, l, cur_f, need_n in receivers:
                            if rem_g <= 0:
                                break
                            add = min(need_n, rem_g)
                            grow_plan.append((idx_l, l, cur_f, cur_f + add, gap))
                            rem_g -= add

                        reclaimed_actual = 0
                        # 先减超储线释放军工
                        for idx_l, l, bf, af, sr in shrink_plan:
                            act_str = "rebalance_shrink:#%d %s %d->%d" % (idx_l, l.get("equipment"), bf, af)
                            if dry_run:
                                reclaimed_actual += (bf - af)
                                out["actions"].append({
                                    "domain": "production", "action": act_str, "ok": True,
                                    "dry_run": True, "detail": {"surplus_ratio": round(sr, 2)},
                                })
                                continue
                            try:
                                res = g.set_production_amount(idx_l, af, ctx["tag"])
                                ok = bool(isinstance(res, dict) and res.get("ok"))
                                if ok:
                                    reclaimed_actual += (bf - af)
                                out["actions"].append({
                                    "domain": "production", "action": act_str, "ok": ok,
                                    "detail": {"surplus_ratio": round(sr, 2), **({"error": res.get("error")} if isinstance(res, dict) and not ok else {})},
                                })
                            except Exception as exc:
                                out["actions"].append({"domain": "production", "action": act_str, "ok": False, "detail": repr(exc)})

                        # 再按实际释放出的军工数补给紧缺线
                        to_give = reclaimed_actual
                        grew_any = False
                        for idx_l, l, bf, af, gap in grow_plan:
                            if to_give <= 0:
                                break
                            add = min(af - bf, to_give)
                            tgt_f = bf + add
                            act_str = "rebalance_grow:#%d %s %d->%d" % (idx_l, l.get("equipment"), bf, tgt_f)
                            if dry_run:
                                to_give -= add
                                grew_any = True
                                out["actions"].append({
                                    "domain": "production", "action": act_str, "ok": True,
                                    "dry_run": True, "detail": {"target_gap": round(gap, 1)},
                                })
                                continue
                            try:
                                res = g.set_production_amount(idx_l, tgt_f, ctx["tag"])
                                ok = bool(isinstance(res, dict) and res.get("ok"))
                                if ok:
                                    to_give -= add
                                    grew_any = True
                                out["actions"].append({
                                    "domain": "production", "action": act_str, "ok": ok,
                                    "detail": {"target_gap": round(gap, 1), **({"error": res.get("error")} if isinstance(res, dict) and not ok else {})},
                                })
                            except Exception as exc:
                                out["actions"].append({"domain": "production", "action": act_str, "ok": False, "detail": repr(exc)})

                        if not dry_run and (reclaimed_actual > 0 or grew_any) and now_raw is not None:
                            st["last_prod_rebalance_date_raw"] = int(now_raw)

    # 3) 空闲船坞自动分配（复用 HousekeepMixin._housekeep_dockyards）
    if params.get("allocate_dockyards", True) and int(ctx.get("dock_idle") or 0) > 0:
        if dry_run:
            out["actions"].append({"domain": "production", "action": "allocate_dockyards:%d" % int(ctx["dock_idle"]), "ok": True, "dry_run": True})
        elif hasattr(g, "_housekeep_dockyards"):
            try:
                dc = g._housekeep_dockyards(ctx["tag"]) or []
                for ch in dc:
                    out["actions"].append({
                        "domain": "production",
                        "action": "dockyard:#%s +%s" % (ch.get("line"), ch.get("granted")),
                        "ok": bool(ch.get("ok")),
                    })
            except Exception as exc:
                out["actions"].append({"domain": "production", "action": "allocate_dockyards", "ok": False, "detail": repr(exc)})

    return out


def _parse_ptr(val):
    """将形如 '0x1234' 的十六进制指针字符串解析为 int，非十六进制标识则原样返回。"""
    if isinstance(val, int):
        return val
    if isinstance(val, str) and val.lower().startswith("0x"):
        try:
            return int(val, 16)
        except ValueError:
            return val
    return val


def _division_template_name(g, div_ptr):
    """只读读取师模板名（兼容 mock 字典或整数指针）。"""
    if isinstance(div_ptr, dict):
        return str(div_ptr.get("template") or div_ptr.get("name") or "")
    try:
        import divreserve as _dr
        tmpl = g.p.u64(int(div_ptr) + _dr.CDIV_TEMPLATE)
        return _dr.template_name(g, tmpl) or ""
    except Exception:
        return ""


def _match_division_role(tmpl_name, role_rules):
    tl = str(tmpl_name or "").lower()
    for rule in role_rules or ():
        kw = rule.get("template_contains") or rule.get("if_template_contains") or []
        if isinstance(kw, str):
            kw = [kw]
        if any(str(k).lower() in tl for k in kw if k):
            return str(rule.get("role") or rule.get("army") or "default")
    return "default"


def _tick_divisions(g, st, st_cfg, ctx, dry_run=False):
    """自动部署满训师、按 role_rules 编入集团军（豁免伞兵）、按 general_strategy 选将。"""
    out = {"actions": [], "wake": []}
    d_cfg = st_cfg.get("divisions") or {}
    if not d_cfg.get("enabled"):
        return out

    # 1) 自动部署训练完成的传送带
    if d_cfg.get("auto_deploy", True) and hasattr(g, "deploy_ready_conveyors"):
        need_deploy = False
        try:
            for cv in g.conveyors(ctx["tag"]) or ():
                if cv.get("deployment_state") == "ready" or float(cv.get("progress") or 0.0) >= 0.995:
                    need_deploy = True
                    break
        except Exception:
            need_deploy = False
        if need_deploy:
            if dry_run:
                out["actions"].append({"domain": "divisions", "action": "deploy_ready", "ok": True, "dry_run": True})
            else:
                try:
                    dr = g.deploy_ready_conveyors(ctx["tag"], min_progress=0.995)
                    ok = bool(isinstance(dr, dict) and dr.get("ok", True))
                    out["actions"].append({"domain": "divisions", "action": "deploy_ready", "ok": ok})
                except Exception as exc:
                    out["actions"].append({"domain": "divisions", "action": "deploy_ready", "ok": False, "detail": repr(exc)})

    # 2) 未编组师自动编入集团军（严格豁免伞兵）
    assign_cfg = d_cfg.get("assign_strategy") or {"strategy": "merge_existing_then_new", "params": {}}
    max_per_army = int((assign_cfg.get("params") or {}).get("max_per_army") or 24)
    role_rules = (assign_cfg.get("params") or {}).get("role_rules") or [] if assign_cfg["strategy"] == "merge_existing_then_new" else []

    raw_unassigned = []
    try:
        raw_unassigned = list(g.unassigned_divisions(ctx["tag"]) or ())
    except Exception:
        raw_unassigned = []

    eligible_divs = []
    for dptr in raw_unassigned:
        try:
            import divreserve as _dr
            if isinstance(dptr, dict):
                tname = str(dptr.get("template") or "")
                is_para = any(k in tname.lower() for k in _dr.PARA_NAME_KEYS)
                ptr_val = dptr.get("ptr")
            else:
                is_para = _dr.is_paradrop_division(g, dptr)[0]
                tname = _division_template_name(g, dptr)
                ptr_val = dptr
            if is_para:
                continue
            role = _match_division_role(tname, role_rules)
            eligible_divs.append({"ptr": ptr_val, "template": tname, "role": role})
        except Exception:
            eligible_divs.append({"ptr": dptr, "template": "", "role": "default"})

    army_roles = st.setdefault("army_roles", {})  # army_ptr_str -> role
    if eligible_divs:
        by_role = {}
        for item in eligible_divs:
            by_role.setdefault(item["role"], []).append(item["ptr"])

        for role, dptrs in by_role.items():
            remaining = list(dptrs)
            try:
                groups = list((g.order_groups(ctx["tag"]) or {}).get("groups") or ())
            except Exception:
                groups = []
            # 优先匹配同角色未满编非伞兵集团军；default 角色兼容未标记集团军
            cand_groups = []
            for grp in groups:
                if grp.get("is_army_group"):
                    continue
                cnt = int(grp.get("divisions") or 0)
                if not (0 < cnt < max_per_army):
                    continue
                gptr = str(grp.get("ptr") or "")
                g_role = army_roles.get(gptr) or str(grp.get("role") or "default")
                if role != "default" and g_role != role:
                    continue
                cand_groups.append(grp)
            cand_groups.sort(key=lambda x: (0 if x.get("leader_ptr") else 1, -int(x.get("divisions") or 0)))

            for grp in cand_groups:
                if not remaining:
                    break
                room = max(0, max_per_army - int(grp.get("divisions") or 0))
                if room <= 0:
                    continue
                batch = remaining[:room]
                if dry_run:
                    out["actions"].append({"domain": "divisions", "action": "merge:%d(%s)->%s" % (len(batch), role, grp.get("ptr")), "ok": True, "dry_run": True})
                    remaining = remaining[len(batch):]
                    continue
                if hasattr(g, "add_divisions_to_army"):
                    try:
                        og_arg = _parse_ptr(grp["ptr"])
                        mr = g.add_divisions_to_army(og_arg, divisions=batch, tag=ctx["tag"])
                        ok = bool(isinstance(mr, dict) and mr.get("ok"))
                        out["actions"].append({"domain": "divisions", "action": "merge:%d(%s)->%s" % (len(batch), role, grp.get("ptr")), "ok": ok})
                        if ok:
                            army_roles[str(grp.get("ptr"))] = role
                            remaining = remaining[len(batch):]
                    except Exception as exc:
                        out["actions"].append({"domain": "divisions", "action": "merge:%s" % grp.get("ptr"), "ok": False, "detail": repr(exc)})

            while remaining:
                batch = remaining[:max_per_army]
                remaining = remaining[len(batch):]
                if dry_run:
                    out["actions"].append({"domain": "divisions", "action": "create_army:%d(%s)" % (len(batch), role), "ok": True, "dry_run": True})
                    continue
                try:
                    before_ptrs = {str(x.get("ptr")) for x in ((g.order_groups(ctx["tag"]) or {}).get("groups") or ())}
                    cr = g.create_army(batch, tag=ctx["tag"])
                    ok = bool(isinstance(cr, dict) and cr.get("ok"))
                    out["actions"].append({"domain": "divisions", "action": "create_army:%d(%s)" % (len(batch), role), "ok": ok})
                    if ok:
                        after_groups = (g.order_groups(ctx["tag"]) or {}).get("groups") or []
                        for ng in after_groups:
                            if str(ng.get("ptr")) not in before_ptrs:
                                army_roles[str(ng.get("ptr"))] = role
                except Exception as exc:
                    out["actions"].append({"domain": "divisions", "action": "create_army:%s" % role, "ok": False, "detail": repr(exc)})
                    break

    # 3) 按 general_strategy 自动指派空闲军长
    gen_cfg = d_cfg.get("general_strategy") or {"strategy": "none", "params": {}}
    if gen_cfg["strategy"] != "none":
        gparams = gen_cfg.get("params") or {}
        min_skill = int(gparams.get("min_skill") or 0)
        weights = gparams.get("weights") or {"attack": 1.0, "defense": 1.0, "planning": 0.8, "logistics": 0.8}
        base_traits = [t.lower() for t in (gparams.get("prefer_traits") or ())]
        role_traits_map = gparams.get("role_prefer_traits") or {}

        try:
            groups = list((g.order_groups(ctx["tag"]) or {}).get("groups") or ())
        except Exception:
            groups = []
        unled = [grp for grp in groups if not grp.get("is_army_group") and int(grp.get("divisions") or 0) > 0 and not grp.get("leader_ptr")]
        if unled:
            used_leaders = {grp.get("leader_ptr") for grp in groups if grp.get("leader_ptr")}
            try:
                gens = [
                    x for x in ((g.available_generals(ctx["tag"]) or {}).get("generals") or ())
                    if x.get("rank") == "corps_commander" and x.get("leader_ptr") not in used_leaders
                ]
            except Exception:
                gens = []
            try:
                import advisors as _adv
                trait_db = _adv.load_leader_traits(ctx["tag"])
            except Exception:
                trait_db = {}

            def _gen_traits(gen_row):
                if gen_row.get("traits"):
                    return {str(t).lower() for t in gen_row["traits"]}
                rec = trait_db.get(str(gen_row.get("key") or "").lower()) or {}
                return {str(t).lower() for t in (rec.get("traits") or ())}

            def _gen_skill(gen_row):
                if gen_row.get("skill") is not None:
                    return int(gen_row["skill"])
                rec = trait_db.get(str(gen_row.get("key") or "").lower()) or {}
                if rec.get("skill") is not None:
                    return int(rec["skill"])
                return max(
                    int(gen_row.get("attack") or 0),
                    int(gen_row.get("defense") or 0),
                    int(gen_row.get("planning") or 0),
                    int(gen_row.get("logistics") or 0),
                )

            for grp in unled:
                cands = [x for x in gens if x.get("leader_ptr") not in used_leaders and _gen_skill(x) >= min_skill]
                if not cands:
                    break
                g_role = army_roles.get(str(grp.get("ptr"))) or str(grp.get("role") or "default")
                want_traits = set(base_traits) | {str(t).lower() for t in (role_traits_map.get(g_role) or ())}

                if gen_cfg["strategy"] == "trait_then_skill":
                    def _score_g(x):
                        hits = len(_gen_traits(x) & want_traits)
                        wsum = (
                            float(x.get("attack") or 0) * float(weights.get("attack", 1.0))
                            + float(x.get("defense") or 0) * float(weights.get("defense", 1.0))
                            + float(x.get("planning") or 0) * float(weights.get("planning", 0.8))
                            + float(x.get("logistics") or 0) * float(weights.get("logistics", 0.8))
                        )
                        return (hits, wsum, str(x.get("key") or ""))
                else:
                    def _score_g(x):
                        tot = sum(int(x.get(k) or 0) for k in ("attack", "defense", "planning", "logistics"))
                        return (0, float(tot), str(x.get("key") or ""))

                best_g = max(cands, key=_score_g)
                if dry_run:
                    used_leaders.add(best_g["leader_ptr"])
                    out["actions"].append({"domain": "divisions", "action": "assign_general:%s->%s" % (best_g.get("key"), grp.get("ptr")), "ok": True, "dry_run": True})
                    continue
                try:
                    og_arg = _parse_ptr(grp["ptr"])
                    lp_arg = _parse_ptr(best_g["leader_ptr"])
                    ar = g.assign_commander(og_arg, leader_ptr=lp_arg, character_key=best_g.get("key"))
                    ok = bool(isinstance(ar, dict) and ar.get("ok"))
                    out["actions"].append({"domain": "divisions", "action": "assign_general:%s->%s" % (best_g.get("key"), grp.get("ptr")), "ok": ok})
                    if ok:
                        used_leaders.add(best_g["leader_ptr"])
                except Exception as exc:
                    out["actions"].append({"domain": "divisions", "action": "assign_general:%s" % best_g.get("key"), "ok": False, "detail": repr(exc)})

    return out


# ===================================================================== 统一 Tick 调度
def _state_fingerprint(ctx):
    """计算用于 no_progress 检测的轻量状态指纹。"""
    return (
        ctx.get("date_raw"),
        ctx.get("current_focus"),
        len(ctx.get("completed_focuses") or ()),
        tuple(s.get("researching") for s in (ctx.get("research_slots") or ())),
        ctx.get("construction_queue_len"),
        ctx.get("mil_idle"),
        round(float(ctx.get("political_power") or 0.0), 0),
        len(ctx.get("active_ideas") or ()),
        ctx.get("divisions"),
    )


def _can_write_live(g):
    """纯只读前置护栏：实机 Windows 写命令前检查窗口是否在前台（离线 FakeGame 自动放行）。"""
    if sys.platform != "win32":
        return True, None
    proc = getattr(g, "p", None)
    pid = getattr(proc, "pid", None)
    # 离线 FakeProc 无真实 OS 句柄 (proc.h) 时直接放行单测
    if not pid or not isinstance(pid, int) or not getattr(proc, "h", None):
        return True, None
    try:
        import windowstate as _ws
        ws = _ws.state(pid)
    except Exception:
        return True, None
    if ws.get("minimized"):
        return False, "game window is minimized (pid %d); paused autopilot safely" % pid
    if ws.get("focused") is False:
        return False, "game window is not in foreground (pid %d); paused autopilot safely" % pid
    return True, None


def run_tick(g, tag=None, dry_run=False, prev_snap=None, cur_snap=None):
    """执行一次确定性 Standing Orders tick，返回结构化回执与是否需要熔断唤醒。"""
    st = get_active_state(g, tag=tag)
    if not st:
        return {"ok": True, "active": False, "actions": [], "wake_triggers": []}

    compiled = st["profile"]
    my_tag = tag or compiled.get("tag")
    ctx = collect_context(g, tag=my_tag)

    # 实机非 dry_run 写前前台保护：窗口切出/最小化时绝不盲目劫持线程，直接熔断停表
    if not dry_run:
        fg_ok, fg_err = _can_write_live(g)
        if not fg_ok:
            trig = {"floor": "window_not_foreground", "detail": fg_err}
            st["last_wake_trigger"] = trig
            _append_journal(st, ctx.get("date"), st.get("current_phase"), "kernel_floor", "wake:window_not_foreground", ok=False, detail=fg_err)
            _save_persisted()
            return {
                "ok": False,
                "active": True,
                "blocked_by": "window_not_foreground",
                "error": fg_err,
                "phase": st.get("current_phase"),
                "actions": [],
                "wake_triggers": [trig],
                "should_wake": True,
            }

    # 1) 阶段机判定与切换（遵守 sticky 不回退）
    old_idx = int(st.get("current_phase_index") or 0)
    old_name = st.get("current_phase")
    new_idx, active_ph, new_floor = resolve_active_phase(
        compiled, ctx, prev_idx=old_idx, sticky_floor=int(st.get("sticky_floor_index", -1)),
    )
    phase_changed = (new_idx != old_idx)
    st_cfg = active_ph["profile"]
    wake_triggers = []

    if not dry_run:
        st["current_phase_index"] = new_idx
        st["current_phase"] = active_ph["name"]
        st["sticky_floor_index"] = new_floor
        if phase_changed:
            _append_journal(
                st, ctx.get("date"), active_ph["name"], "phase",
                "transition:%s->%s" % (old_name, active_ph["name"]),
                ok=True, detail={"sticky": active_ph.get("sticky")},
            )
            if (st_cfg.get("interrupts_extra") or {}).get("on_phase_change"):
                wake_triggers.append({
                    "floor": "interrupt_extra:phase_changed",
                    "detail": "phase transitioned: %s -> %s" % (old_name, active_ph["name"]),
                })

    # 2) 通过单一真源执行 7 个子系统（dry_run=True 时深拷贝状态防任何副作用污染）
    work_st = copy.deepcopy(st) if dry_run else st
    fp_before = _state_fingerprint(ctx)
    all_actions, sub_wakes, _ = _run_subsystems_pass(g, work_st, st_cfg, ctx, dry_run=dry_run)
    if not dry_run:
        for act in all_actions:
            _append_journal(
                st, ctx.get("date"), active_ph["name"],
                act["domain"], act["action"], ok=act.get("ok", True), detail=act.get("detail"),
            )
    wake_triggers.extend(sub_wakes)

    # 3) 刷新上下文并更新无进展计数（仅在有尝试执行动作但全失败且状态零变化时累加！）
    ctx_after = collect_context(g, tag=my_tag) if (not dry_run and all_actions) else ctx
    fp_after = _state_fingerprint(ctx_after)
    attempted = len(all_actions)
    succeeded = sum(1 for a in all_actions if a.get("ok"))

    if not dry_run:
        st["ticks"] = int(st.get("ticks") or 0) + 1
        if ctx_after.get("date_raw") is not None:
            st["last_tick_date_raw"] = ctx_after["date_raw"]
            if st.get("started_date_raw") is not None:
                st["days_elapsed"] = round(max(0.0, (int(ctx_after["date_raw"]) - int(st["started_date_raw"])) / 24.0), 2)
        hard_attempts = [
            a for a in all_actions
            if not (isinstance(a.get("detail"), dict) and a["detail"].get("slot_done_transient"))
        ]
        if hard_attempts:
            hard_ok = sum(1 for a in hard_attempts if a.get("ok"))
            if hard_ok == 0 and fp_after == fp_before:
                st["no_progress_streak"] = int(st.get("no_progress_streak") or 0) + 1
            else:
                st["no_progress_streak"] = 0
        else:
            # 正常巡航无待办动作（或仅遇到瞬态 slot_done 退避）时如果时钟在前进则清零 streak
            if st.get("last_state_fingerprint") is not None and fp_after != st.get("last_state_fingerprint"):
                st["no_progress_streak"] = 0
        st["last_state_fingerprint"] = fp_after

    # 4) 执行不可关闭的 Kernel Floor 终检
    floor_hits = check_kernel_floor(g, st, ctx=ctx_after, prev_snap=prev_snap, cur_snap=cur_snap)
    # 合并去重
    seen_trig = {(t.get("floor"), t.get("detail")) for t in wake_triggers}
    for hit in floor_hits:
        sig = (hit.get("floor"), hit.get("detail"))
        if sig not in seen_trig:
            seen_trig.add(sig)
            wake_triggers.append(hit)

    if not dry_run:
        st["last_pp"] = ctx_after.get("political_power")
        st["updated_at"] = time.time()
        if wake_triggers:
            st["last_wake_trigger"] = wake_triggers[0]
            _append_journal(
                st, ctx_after.get("date"), active_ph["name"], "kernel_floor",
                "wake:%s" % wake_triggers[0]["floor"], ok=True, detail=wake_triggers[0]["detail"],
            )
        _save_persisted()

    return {
        "ok": True,
        "active": True,
        "dry_run": bool(dry_run),
        "phase": active_ph["name"],
        "phase_changed": phase_changed,
        "days_elapsed": st.get("days_elapsed", 0.0),
        "ticks": st.get("ticks", 0),
        "attempted": attempted,
        "succeeded": succeeded,
        "actions": all_actions,
        "wake_triggers": wake_triggers,
        "should_wake": bool(wake_triggers),
    }


def status(g, tag=None, journal_limit=15):
    """只读查看当前会话的 Standing Orders 状态、阶段、内核地板与最近 journal。"""
    st = get_active_state(g, tag=tag)
    if not st:
        return {"ok": True, "active": False, "note": "当前会话未激活 Standing Orders"}
    compiled = st["profile"]
    kf = compiled["kernel_floor"]
    d = _safe_date(g)
    days_elapsed = st.get("days_elapsed", 0.0)
    since_hb = 0.0
    if d.get("raw") is not None:
        if st.get("started_date_raw") is not None:
            days_elapsed = round(max(0.0, (int(d["raw"]) - int(st["started_date_raw"])) / 24.0), 2)
        hb_raw = st.get("last_heartbeat_date_raw") or st.get("started_date_raw")
        if hb_raw is not None:
            since_hb = round(max(0.0, (int(d["raw"]) - int(hb_raw)) / 24.0), 2)
    return {
        "ok": True,
        "active": True,
        "name": compiled.get("name"),
        "tag": compiled.get("tag"),
        "current_phase": st.get("current_phase"),
        "phases": [p["name"] for p in compiled.get("phases") or ()],
        "sticky_floor_index": st.get("sticky_floor_index"),
        "compiled_at_date": st.get("compiled_at_date"),
        "days_elapsed": days_elapsed,
        "days_until_max": round(max(0.0, float(kf["max_autopilot_days"]) - days_elapsed), 1),
        "days_until_heartbeat": round(max(0.0, float(kf["heartbeat_days"]) - since_hb), 1),
        "ticks": st.get("ticks", 0),
        "no_progress_streak": st.get("no_progress_streak", 0),
        "kernel_floor": dict(kf),
        "last_wake_trigger": st.get("last_wake_trigger"),
        "journal_tail": list((st.get("journal") or [])[-int(journal_limit or 15):]),
    }


def format_briefing_section(st_summary):
    """将激活的 Standing Orders 摘要渲染为 1~2 行高密度 Markdown（未激活时不占 token）。"""
    if not isinstance(st_summary, dict) or not st_summary.get("active"):
        return []
    lines = [
        "- **自动驾驶 (Standing Orders)**: `%s` | 阶段=`%s` | 已跑 %s/%s天 | 距心跳 %s天 | ticks=%s"
        % (
            st_summary.get("name") or "default",
            st_summary.get("current_phase") or "?",
            st_summary.get("days_elapsed", 0),
            (st_summary.get("kernel_floor") or {}).get("max_autopilot_days", DEFAULT_MAX_AUTOPILOT_DAYS),
            st_summary.get("days_until_heartbeat", "?"),
            st_summary.get("ticks", 0),
        )
    ]
    tail = st_summary.get("journal_tail") or []
    recent_ops = [
        "%s:%s" % (x.get("domain"), x.get("action"))
        for x in tail[-5:]
        if x.get("domain") not in ("system",)
    ]
    if recent_ops:
        lines.append("  - 最近自动执行: " + " | ".join(recent_ops))
    return lines
