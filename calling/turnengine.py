"""方案 2.5 核心引擎：确定性条件简报 + 受限逃生舱 + 事务式 Plan 执行器。

设计目标：
  1. 彻底消灭单会话百工具 Schema（~24k tokens）与无限累积历史对话导致的上下文爆炸，
     把每回合模型输入稳控在 ~3k–5k tokens。
  2. 把所有硬安全规则（前台窗口检查、版本校验、串行写+写后验活、
     解除暂停前 0 师订单检查）全部下沉为代码强制守卫，不再依赖模型“自觉”。
  3. 支持两种使用形态：
     - 独立无工具编排器（calling/orchestrator.py）
     - Compact MCP 网关（mcp/server.py --profile=compact，仅暴露 5 个工具）
"""
from __future__ import annotations

import json
import os
import sys
import time

import session_state as _SS

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = _SS.state_path(_SS.TURN_STATE_NAME)

MAX_ACTIONS_PER_TURN = 15
MAX_INSPECT_QUERIES = 3
MAX_MEMO_CHARS = 800

# 允许的 Plan DSL 高层动词白名单（22 个）
ALLOWED_ACTIONS = {
    # 政治与事件
    "select_event",
    "set_focus",
    "select_decision",
    "add_idea",
    "hire_advisor",
    # 科研与工业
    "fill_research",
    "start_research",
    "add_construction",
    "remove_construction",
    "set_production",
    "upgrade_production",
    "rebalance_production",
    "trade",
    "housekeep",
    # 军队与战争
    "upgrade_template_preset",
    "plan_front_allocation",
    "add_to_army",
    "train_divisions",
    "deploy_ready",
    "purge_orders",
    "reset_battleplans",
    "army_battleplan",
    "justify_wargoal",
    "declare_war",
    # 外交与和会
    "peace_demands",
    "peace_finish",
    # 时钟节奏与红点门禁签收
    "set_game_speed",
    "pause",
    "resume",
    "ack_red_dot",
    # 自动驾驶（Standing Orders v1）
    "set_standing_orders",
    "clear_standing_orders",
    "autopilot_tick",
    "ack_autopilot",
}

# 允许的只读按需深查（逃生舱）主题（20 个，含 autopilot / strategy_catalog / idea_catalog）
ALLOWED_INSPECT_TOPICS = {
    "country",
    "events",
    "selectable_focuses",
    "research_available",
    "advisors",
    "idea_catalog",
    "decisions",
    "armies",
    "war_readiness",
    "combat",
    "stockpile",
    "resources",
    "state",
    "peace",
    "air",
    "navy",
    "staff",
    "health",
    "autopilot",
    "strategy_catalog",
}

# 2026-11-22 (P1-1): 22 个动作的完整参数规范（标准名与公开 MCP 工具完全对齐，
# 同时向后兼容旧内部别名；validate_plan / dry_run / _dispatch_action 共用同一份校验）。
ACTION_SPECS = {
    "select_event": {
        "required": (),
        "optional": ("index", "event_id", "lang"),
        "aliases": {},
        "types": {"index": "pos_int", "event_id": "str", "lang": "str"},
    },
    "set_focus": {
        "required": ("key",),
        "optional": (),
        "aliases": {"focus": "key", "focus_id": "key"},
        "types": {"key": "str"},
    },
    "select_decision": {
        "required": ("key",),
        "optional": (),
        "aliases": {"decision": "key", "decision_id": "key"},
        "types": {"key": "str"},
    },
    "add_idea": {
        "required": ("key",),
        "optional": (),
        "aliases": {"idea_id": "key"},
        "types": {"key": "str"},
    },
    "hire_advisor": {
        "required": ("key",),
        "optional": ("slot",),
        "aliases": {"slot_override": "slot"},
        "types": {"key": "str", "slot": "str"},
    },
    "fill_research": {
        "required": (),
        "optional": ("technologies", "max_attempts"),
        "aliases": {},
        "types": {"technologies": "list_str", "max_attempts": "pos_int"},
    },
    "start_research": {
        "required": ("technology",),
        "optional": ("slot",),
        "aliases": {"tech": "technology"},
        "types": {"technology": "str", "slot": "nonneg_int"},
    },
    "add_construction": {
        "required": ("state", "building"),
        "optional": ("level", "province"),
        "aliases": {"state_id": "state"},
        "types": {
            "state": "pos_int",
            "building": "str",
            "level": "pos_int",
            "province": "nonneg_int",
        },
    },
    "remove_construction": {
        "required": ("index",),
        "optional": (),
        "aliases": {},
        "types": {"index": "nonneg_int"},
    },
    "set_production": {
        "required": ("line", "amount"),
        "optional": (),
        "aliases": {"line_index": "line"},
        "types": {"line": "nonneg_int", "amount": "nonneg_int"},
    },
    "upgrade_production": {
        "required": ("line", "equipment"),
        "optional": ("dry_run",),
        "aliases": {"line_index": "line"},
        "types": {"line": "nonneg_int", "equipment": "str", "dry_run": "bool"},
    },
    "rebalance_production": {
        "required": (),
        "optional": ("dry_run", "min_factories_per_line"),
        "aliases": {},
        "types": {"dry_run": "bool", "min_factories_per_line": "nonneg_int"},
    },
    "upgrade_template_preset": {
        "required": (),
        "optional": ("preset", "template", "auto_rebalance", "dry_run"),
        "aliases": {"template_name": "template"},
        "types": {
            "preset": "str",
            "template": "str",
            "auto_rebalance": "bool",
            "dry_run": "bool",
        },
    },
    "plan_front_allocation": {
        "required": ("target",),
        "optional": ("reserve_ratio", "min_reserve", "execute", "allow_understrength"),
        "aliases": {"target_tag": "target", "enemy_tag": "target"},
        "types": {
            "target": "str",
            "reserve_ratio": "float",
            "min_reserve": "nonneg_int",
            "execute": "bool",
            "allow_understrength": "bool",
        },
    },
    "housekeep": {
        "required": (),
        "optional": ("target_construction", "technologies"),
        "aliases": {},
        "types": {"target_construction": "pos_int", "technologies": "list_str"},
    },
    "trade": {
        "required": ("partner", "resource"),
        "optional": ("amount", "factories", "dry_run"),
        "aliases": {"exporter": "partner", "target": "partner"},
        "types": {"partner": "str", "resource": "str", "amount": "nonneg_int", "factories": "nonneg_int", "dry_run": "bool"},
    },
    "add_to_army": {
        "required": ("orders_group",),
        "optional": ("divisions", "from_army", "count", "order_id"),
        "aliases": {"army": "orders_group", "target_army": "orders_group"},
        "types": {"orders_group": "str", "divisions": "list_str", "from_army": "str", "count": "pos_int", "order_id": "nonneg_int"},
    },
    "train_divisions": {
        "required": (),
        "optional": ("template", "lines", "state"),
        "aliases": {"template_name": "template", "state_id": "state"},
        "types": {"template": "str", "lines": "pos_int", "state": "pos_int"},
    },
    "deploy_ready": {
        "required": (),
        "optional": ("min_progress",),
        "aliases": {},
        "types": {"min_progress": "float"},
    },
    "purge_orders": {
        "required": (),
        "optional": (),
        "aliases": {},
        "types": {},
    },
    "reset_battleplans": {
        "required": (),
        "optional": (),
        "aliases": {},
        "types": {},
    },
    "army_battleplan": {
        "required": ("enemy_tag",),
        "optional": ("force", "wait_seconds", "armies", "split"),
        "aliases": {"target": "enemy_tag", "target_tag": "enemy_tag"},
        "types": {
            "enemy_tag": "str",
            "force": "bool",
            "wait_seconds": "nonneg_int",
            "armies": "list_str",
            "split": "bool",
        },
    },
    "justify_wargoal": {
        "required": ("target",),
        "optional": ("wargoal", "states", "force", "dry_run"),
        "aliases": {"target_tag": "target"},
        "types": {
            "target": "str",
            "wargoal": "str",
            "states": "list_int",
            "force": "bool",
            "dry_run": "bool",
        },
    },
    "declare_war": {
        "required": ("target",),
        "optional": ("dry_run",),
        "aliases": {"target_tag": "target"},
        "types": {"target": "str", "dry_run": "bool"},
    },
    "peace_demands": {
        "required": ("loser",),
        "optional": ("winner", "kinds", "states"),
        "aliases": {},
        "types": {"loser": "str", "winner": "str", "kinds": "list_str", "states": "list_int"},
    },
    "peace_finish": {
        "required": (),
        "optional": ("action",),
        "aliases": {},
        "types": {"action": "str"},
        "enums": {"action": ("done", "pass", "endturn")},
    },
    "set_game_speed": {
        "required": ("speed",),
        "optional": ("measure_seconds",),
        "aliases": {},
        "types": {"speed": "nonneg_int", "measure_seconds": "float"},
    },
    "pause": {
        "required": (),
        "optional": (),
        "aliases": {},
        "types": {},
    },
    "resume": {
        "required": (),
        "optional": (),
        "aliases": {},
        "types": {},
    },
    "ack_red_dot": {
        "required": ("code", "reason"),
        "optional": ("snooze_days", "force"),
        "aliases": {},
        "types": {"code": "str", "reason": "str", "snooze_days": "pos_int", "force": "bool"},
    },
    "set_standing_orders": {
        "required": ("profile",),
        "optional": ("dry_run", "replace"),
        "aliases": {},
        "types": {"profile": "dict", "dry_run": "bool", "replace": "bool"},
    },
    "clear_standing_orders": {
        "required": (),
        "optional": ("reason",),
        "aliases": {},
        "types": {"reason": "str"},
    },
    "autopilot_tick": {
        "required": (),
        "optional": ("dry_run", "ack", "reason", "extend_max_days"),
        "aliases": {},
        "types": {"dry_run": "bool", "ack": "bool", "reason": "str", "extend_max_days": "bool"},
    },
    "ack_autopilot": {
        "required": (),
        "optional": ("reason", "extend_max_days"),
        "aliases": {},
        "types": {"reason": "str", "extend_max_days": "bool"},
    },
}


def _check_arg_type(kind, val):
    """校验并规范化单个参数类型，返回 (ok, coerced_val, err_msg)。"""
    if val is None:
        return True, None, None
    if kind in ("int", "nonneg_int", "pos_int"):
        if isinstance(val, bool) or not isinstance(val, int):
            return False, val, "必须是整数"
        if kind == "nonneg_int" and val < 0:
            return False, val, "必须 >= 0"
        if kind == "pos_int" and val <= 0:
            return False, val, "必须 > 0"
        return True, int(val), None
    if kind == "float":
        if isinstance(val, bool) or not isinstance(val, (int, float)):
            return False, val, "必须是数值"
        return True, float(val), None
    if kind == "str":
        if not isinstance(val, str) or not val.strip():
            return False, val, "必须是非空字符串"
        return True, val.strip(), None
    if kind == "bool":
        if not isinstance(val, bool):
            return False, val, "必须是布尔值"
        return True, val, None
    if kind == "list_str":
        if not isinstance(val, list) or not all(isinstance(x, str) and x.strip() for x in val):
            return False, val, "必须是非空字符串列表"
        return True, [x.strip() for x in val], None
    if kind == "list_int":
        if not isinstance(val, list) or not all(isinstance(x, int) and not isinstance(x, bool) for x in val):
            return False, val, "必须是整数列表"
        return True, [int(x) for x in val], None
    if kind == "dict":
        if not isinstance(val, dict) or not val:
            return False, val, "必须是非空 JSON 对象"
        return True, val, None
    return True, val, None


def _normalize_action_args(op, raw_args, prefix=""):
    """将单条动作的 args 规范化到标准公开参数名并做强校验。"""
    spec = ACTION_SPECS.get(op)
    if spec is None:
        return {}, ["%sop=%r 缺少参数规范" % (prefix, op)]
    if raw_args is None:
        raw_args = {}
    if not isinstance(raw_args, dict):
        return {}, ["%sargs 必须是 JSON 对象" % prefix]

    aliases = spec.get("aliases") or {}
    allowed_canon = set(spec.get("required") or ()) | set(spec.get("optional") or ()) | {"tag"}
    norm = {}
    errors = []

    for k, v in raw_args.items():
        if not isinstance(k, str):
            errors.append("%sargs 键名必须是字符串 (%r)" % (prefix, k))
            continue
        canon = aliases.get(k, k)
        if canon not in allowed_canon:
            allowed_display = sorted(
                (set(spec.get("required") or ()) | set(spec.get("optional") or ()))
            )
            errors.append(
                "%s.%s 不支持参数 %r（允许参数: %s）"
                % (prefix.rstrip("."), op, k, ", ".join(allowed_display) if allowed_display else "无")
            )
            continue
        if canon in norm and norm[canon] != v:
            errors.append("%s.%s 存在冲突的别名参数 %r 与 %r" % (prefix.rstrip("."), op, k, canon))
            continue
        norm[canon] = v

    if "tag" in norm and norm["tag"] is not None:
        ok_t, val_t, err_t = _check_arg_type("str", norm["tag"])
        if not ok_t:
            errors.append("%s.%s.tag %s" % (prefix.rstrip("."), op, err_t))
        else:
            norm["tag"] = val_t

    types_map = spec.get("types") or {}
    enums_map = spec.get("enums") or {}

    for req in spec.get("required") or ():
        if req not in norm or norm[req] is None or (isinstance(norm[req], str) and not norm[req].strip()):
            alias_hints = [ak for ak, av in aliases.items() if av == req]
            hint = ("（兼容别名: %s）" % "/".join(alias_hints)) if alias_hints else ""
            errors.append("%s.%s 缺少必填参数 %r%s" % (prefix.rstrip("."), op, req, hint))

    for k, v in list(norm.items()):
        if k == "tag" or v is None:
            continue
        kind = types_map.get(k)
        if kind:
            ok_v, coerced, err_v = _check_arg_type(kind, v)
            if not ok_v:
                errors.append("%s.%s.%s %s（收到 %r）" % (prefix.rstrip("."), op, k, err_v, v))
                continue
            norm[k] = coerced
        if k in enums_map and norm[k] not in enums_map[k]:
            errors.append(
                "%s.%s.%s=%r 无效（仅支持: %s）"
                % (prefix.rstrip("."), op, k, norm[k], ", ".join(enums_map[k]))
            )

    return norm, errors

# 纯只读/本地配置类，无需前台窗口校验的动作（0 引擎注入）
_NO_FOREGROUND_NEEDED = {
    "ack_red_dot",
    "set_standing_orders",
    "clear_standing_orders",
    "ack_autopilot",
}


# ===================================================================== 状态持久化
def _empty_turn_state():
    return {
        "turn": 0,
        "pid": None,
        "proc_start": 0,
        "tag": None,
        "date_raw": None,
        "memo": "",
        "last_receipt": "",
    }


def _proc_start_ticks(pid):
    """获取进程创建时间指纹，防止重启后同 PID 被系统复用导致旧局状态串味。"""
    if not pid:
        return 0
    if sys.platform == "win32":
        try:
            import ctypes
            import ctypes.wintypes as wt
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            h = k32.OpenProcess(0x1000, False, int(pid))
            if not h:
                return 0
            try:
                ct, et, kt, ut = wt.FILETIME(), wt.FILETIME(), wt.FILETIME(), wt.FILETIME()
                if k32.GetProcessTimes(h, ctypes.byref(ct), ctypes.byref(et),
                                       ctypes.byref(kt), ctypes.byref(ut)):
                    return (ct.dwHighDateTime << 32) | ct.dwLowDateTime
            finally:
                k32.CloseHandle(h)
        except Exception:
            pass
        return 0
    try:
        with open("/proc/%d/stat" % int(pid)) as f:
            return int(f.read().rsplit(")", 1)[1].split()[19])
    except Exception:
        return 0


def _state_belongs_to_session(data, pid=None, proc_start=None, tag=None, date_raw=None):
    """校验持久化状态是否仍属于当前对局（防换进程 / 换国家 / 读旧档 / 换剧本串味）。"""
    if pid is not None:
        if data.get("pid") != pid:
            return False
    if proc_start and data.get("proc_start"):
        if int(data["proc_start"]) != int(proc_start):
            return False
    if tag is not None and data.get("tag") not in (None, tag):
        return False
    if date_raw is not None and data.get("date_raw") is not None:
        prev = int(data["date_raw"])
        cur = int(date_raw)
        # 日期倒退（读取旧档）或骤跳超过 365 游戏日（切换不同年份剧本）视为新局
        if cur < prev or (cur - prev) > 365 * 24:
            return False
    return True


def load_turn_state(path=None, pid=None, proc_start=None, tag=None, date_raw=None):
    """读取跨回合覆写式状态（memo、last_receipt、turn），自动隔离不同进程与存档。"""
    path = STATE_PATH if path is None else path
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            if _state_belongs_to_session(data, pid=pid, proc_start=proc_start,
                                         tag=tag, date_raw=date_raw):
                return data
            return _empty_turn_state()
    except FileNotFoundError:
        return _empty_turn_state()
    except Exception as _e:
        import diag as _diag
        _diag.warn("turnengine.load_turn_state", _e, detail=str(path))
    return _empty_turn_state()


def reset_turn_state(path=None):
    """清空持久化回合状态（测试收尾或手动新开一局时使用）。"""
    path = STATE_PATH if path is None else path
    try:
        if os.path.exists(path):
            os.remove(path)
    except Exception as _e:
        import diag as _diag
        _diag.warn("turnengine.reset_turn_state", _e, detail=str(path))
    return _empty_turn_state()


def save_turn_state(state, path=None):
    """原子覆写回合状态文件。"""
    path = STATE_PATH if path is None else path
    _SS.ensure_parent(path)
    tmp = path + ".tmp"
    clean = dict(state or {})
    memo = str(clean.get("memo") or "")
    if len(memo) > MAX_MEMO_CHARS:
        clean["memo"] = memo[:MAX_MEMO_CHARS]
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(clean, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception as _e:
        import diag as _diag
        _diag.warn("turnengine.save_turn_state", _e, detail=str(path))


def _estimate_tokens(text):
    """区分 ASCII 与非 ASCII（中文）估算 token 数，更贴近现代 BPE 分词器。"""
    if not text:
        return 0
    ascii_n = sum(1 for ch in text if ord(ch) < 128)
    non_ascii_n = len(text) - ascii_n
    return max(1, int(round(ascii_n / 4.0 + non_ascii_n * 0.7)))


# ===================================================================== 内部守卫
def _process_alive(g):
    """最轻量的进程存活探测（读 PE 头 4 字节，0 注入）。"""
    try:
        data = g.p.read(g.p.base, 4)
        return bool(data and len(data) == 4)
    except Exception:
        return False


def _window_guard(g):
    """检查 Windows 游戏窗口状态；绝不强行抢焦点（硬约束 #7）。

    返回 (ok: bool, reason: str | None, win_state: dict)。
    """
    if sys.platform != "win32":
        return True, None, {"platform": sys.platform}
    try:
        import windowstate
        ws = windowstate.state(getattr(g.p, "pid", None))
    except Exception as e:
        return True, None, {"error": repr(e)}
    if ws.get("minimized"):
        return False, "window_minimized", ws
    if ws.get("visible") is False:
        return False, "window_hidden", ws
    if ws.get("focused") is False:
        return False, "window_not_foreground", ws
    return True, None, ws


def _version_guard(g):
    """确认当前进程的内存布局与 1.19.5 PE 精确指纹匹配（防止 Steam 升 1.20 后误写）。

    优先调用 `win_pipeline.ensure_live_version(g, only_if_live=True)` 执行 PE
    TimeDateStamp + SizeOfImage 精确核验（离线单测 FakeProc 自动放行），
    通过后再校验 `G_GAMESTATE` 指针合理性作为双保险。
    """
    if sys.platform != "win32":
        return True, None
    try:
        import win_pipeline as _wp
        _wp.ensure_live_version(g, only_if_live=True)
    except Exception as e:
        return False, str(e)
    try:
        import offsets_win
        gs_rva = getattr(offsets_win, "G_GAMESTATE", None)
        if gs_rva:
            gs = g.p.u64(g.p.base + gs_rva)
            if not (0x10000 < gs < 0x7FFFFFFFFFFF):
                return False, "G_GAMESTATE 指针异常 (%#x)，疑似游戏版本与 1.19.5 偏移表失配" % gs
    except Exception as e:
        return False, "版本/偏移自检失败: %r" % (e,)
    return True, None


def _has_empty_orders(g, tag=None):
    """纯只读检查是否存在 0 师前线订单（硬约束 #6，防 0xB2E62E 崩溃）。"""
    try:
        groups = g.order_groups(tag)
        if isinstance(groups, dict):
            rows = groups.get("groups") or groups.get("order_groups") or []
        else:
            rows = groups or []
        empty = []
        for og in rows:
            ptr = og.get("ptr") if isinstance(og, dict) else og
            if isinstance(ptr, str):
                ptr = int(ptr, 16) if ptr.startswith("0x") else int(ptr)
            if not ptr:
                continue
            orders = g.orders_of(ptr) or []
            for o in orders:
                if int(o.get("assigned_divisions") or 0) == 0:
                    empty.append(o.get("id"))
        return empty
    except Exception:
        return []


# ===================================================================== 紧凑简报器
def _safe_call(fn, default=None):
    try:
        return fn()
    except Exception:
        return default


def _fmt_num(v, digits=1):
    if v is None:
        return "?"
    try:
        f = float(v)
        if abs(f - round(f)) < 1e-6:
            return str(int(round(f)))
        return ("%." + str(digits) + "f") % f
    except Exception:
        return str(v)


def _render_brief_markdown(data):
    """将结构化简报渲染成高密度 Markdown（相比等量 JSON 节省 ~35-45% tokens）。"""
    lines = []
    meta = data["meta"]
    c = data["country"]
    fac = data["factories"]
    army = data["army"]

    lines.append(
        "# HOI4 回合简报 | %s | %s (%s) | %s"
        % (meta["date"], c.get("name") or meta["tag"], meta["tag"],
           "暂停" if meta.get("paused") else "运行中")
    )
    win = meta.get("window") or {}
    if sys.platform == "win32" and win:
        wstat = "前台" if win.get("focused") else ("最小化" if win.get("minimized") else "非前台(禁止写命令)")
        lines.append("- 窗口: %s | 时钟源: %s" % (wstat, meta.get("date_source") or "?"))

    # 2026-11-23: 红点门禁置顶渲染（放在最高注意力区）
    rd_eval = data.get("red_dots")
    if isinstance(rd_eval, dict):
        try:
            import reddots as _rd
            rd_lines = _rd.format_briefing_section(rd_eval)
            if rd_lines:
                lines.extend(rd_lines)
        except Exception:
            pass

    # 2026-11-23: 老手战略体检看板置顶渲染（弱编/产能倒挂/断补危机/扩军差距）
    health_board = data.get("health_board")
    if isinstance(health_board, dict):
        try:
            import macros as _mc
            hb_lines = _mc.format_health_board_markdown(health_board)
            if hb_lines:
                lines.extend(hb_lines)
        except Exception:
            pass

    # 2026-11-25: Standing Orders 自动驾驶摘要（未激活时不占 token）
    ap_summary = data.get("autopilot")
    if isinstance(ap_summary, dict) and ap_summary.get("active"):
        try:
            import autopilot as _ap
            ap_lines = _ap.format_briefing_section(ap_summary)
            if ap_lines:
                lines.extend(ap_lines)
        except Exception:
            pass

    if data.get("alerts"):
        # 单条长告警（如带长操作指引的提示）截断到 90 字，防止在 compact 简报中喧宾夺主
        slim_alerts = []
        for a in data["alerts"]:
            s = str(a).strip()
            if len(s) > 90:
                s = s[:88].rstrip() + "…"
            slim_alerts.append(s)
        lines.append("- **告警**: " + " | ".join(slim_alerts))
    else:
        lines.append("- 告警: 无")

    # 核心国家数值一行化（仅展示可用字段，不输出无意义的 ?）
    xp = c.get("experience") or {}
    pp_str = _fmt_num(c.get("political_power"), 0)
    if c.get("political_power_daily") is not None:
        pp_str += "(+%s/日)" % _fmt_num(c.get("political_power_daily"), 2)
    parts = ["PP=%s" % pp_str]
    if c.get("command_power") is not None:
        parts.append("CP=%s" % _fmt_num(c.get("command_power"), 1))
    parts.extend([
        "稳定=%s%%" % _fmt_num((c.get("stability") or 0) * 100, 1),
        "战支=%s%%" % _fmt_num((c.get("war_support") or 0) * 100, 1),
        "人力=%sk" % _fmt_num((c.get("manpower") or 0) / 1000.0, 0),
        "燃油=%sk" % _fmt_num((c.get("fuel") or 0) / 1000.0, 0),
        "XP(陆/海/空)=%s/%s/%s" % (
            _fmt_num(xp.get("army"), 1),
            _fmt_num(xp.get("navy"), 1),
            _fmt_num(xp.get("air"), 1),
        ),
    ])
    lines.append("- **国力**: " + " | ".join(parts))
    lines.append(
        "- **工业**: 民工=%s(在建线=%s) | 军工=%s(闲置=%s) | 船坞=%s | 州=%s"
        % (
            fac.get("civ_total"), fac.get("construction_queue_len"),
            fac.get("mil_total"), fac.get("mil_free"),
            fac.get("dock_total"), data.get("states_count"),
        )
    )
    lines.append(
        "- **陆军**: 总师=%s | 未编师=%s | 战争=%s%s"
        % (
            army.get("total_divisions"),
            army.get("unassigned_divisions"),
            "是" if data["war"].get("at_war") else "否",
            (" 敌国=" + ",".join(data["war"].get("enemies") or []))
            if data["war"].get("enemies") else "",
        )
    )
    # 宣战理由 (CB) 与正当化进度（显式展示，防止长跑遗漏 CB 过期）
    wgs_b = data.get("wargoals") or []
    if wgs_b:
        wg_items = [
            "%s(%s)" % (x.get("target") or x.get("target_tag"), x.get("wargoal") or "wargoal")
            for x in wgs_b[:6]
        ]
        lines.append("- **宣战理由 (CB)**: %s（注意会过期！）" % ", ".join(wg_items))
    just_b = data.get("justifying") or []
    if just_b:
        j_items = []
        for x in just_b[:4]:
            dl = x.get("days_left")
            eta = x.get("eta_date")
            extra = []
            if dl is not None:
                extra.append("剩%s天" % _fmt_num(dl, 0))
            if eta:
                extra.append("ETA %s" % eta)
            j_items.append("%s(%s)" % (x.get("target"), ", ".join(extra)) if extra else str(x.get("target")))
        lines.append("- **正当化中**: %s" % ", ".join(j_items))

    # 国策
    foc = data["focus"]
    if foc.get("current"):
        lines.append("- **国策**: 进行中 `%s` (%s)" % (foc["current"], foc.get("name") or ""))
    else:
        lines.append("- **国策**: **空闲（必须选择）**")
        cands = data.get("selectable_focuses") or []
        cands_total = int(data.get("selectable_focuses_total") or len(cands))
        if cands:
            shown = cands[:18]
            items = []
            for f in shown:
                if isinstance(f, dict):
                    items.append("`%s`(%s)" % (f.get("key"), f.get("name") or ""))
                else:
                    items.append("`%s`" % f)
            extra = cands_total - len(shown)
            tail_more = (" … +%d 条" % extra) if extra > 0 else ""
            lines.append("  - 可选国策(%d): %s%s" % (cands_total, ", ".join(items), tail_more))

    # 科研（严格以 slot_busy 判空槽）
    slots = data.get("research") or []
    free_slots = sum(1 for s in slots if not s.get("slot_busy"))
    slot_desc = []
    for s in slots:
        if not s.get("slot_busy"):
            slot_desc.append("槽%s:空" % s.get("slot"))
        else:
            nm = s.get("researching") or "<进行中>"
            pct = s.get("percent")
            slot_desc.append("槽%s:%s(%s%%)" % (s.get("slot"), nm, _fmt_num(pct, 0) if pct is not None else "?"))
    lines.append("- **科研**(空槽=%d/%d): %s" % (free_slots, len(slots), " | ".join(slot_desc)))
    if free_slots > 0 and data.get("research_candidates"):
        lines.append("  - 推荐可选科技: " + ", ".join(data["research_candidates"][:20]))

    # 建造与生产摘要（2026-11-22 P2-3: 标题用真实未截断总数，超限时明确标出 `… +N 条`）
    cons = data.get("construction") or []
    cons_total = int(data.get("construction_total") or len(cons))
    if cons:
        shown_c = cons[:8]
        c_items = []
        for x in shown_c:
            pct = x.get("percent")
            if pct is None and x.get("progress") is not None:
                pct = float(x["progress"]) * 100.0
            st_part = ("@州%s" % x["state"]) if x.get("state") is not None else ("#%s" % x.get("index", "?"))
            tail = ["%s厂" % x.get("factories", 0)]
            if pct is not None:
                tail.append("%s%%" % _fmt_num(pct, 0))
            if x.get("days_left") is not None:
                tail.append("剩%s天" % _fmt_num(x.get("days_left"), 1))
            if x.get("stalled"):
                tail.append("停滞")
            elif x.get("status") == "queued" or x.get("waiting_for_cic"):
                tail.append("排队中")
            c_items.append("%s%s(%s)" % (x.get("building") or "?", st_part, ",".join(tail)))
        extra_c = cons_total - len(shown_c)
        more_c = (" | … +%d 条" % extra_c) if extra_c > 0 else ""
        lines.append("- **建造队列(%d)**: %s%s" % (cons_total, " | ".join(c_items), more_c))
    else:
        lines.append("- **建造队列**: **空**")

    prod = data.get("production") or []
    prod_total = int(data.get("production_total") or len(prod))
    if prod:
        shown_p = prod[:12]
        p_items = [
            "#%s:%s(%s厂)" % (x.get("index"), x.get("equipment") or "?", x.get("factories", 0))
            for x in shown_p
        ]
        extra_p = prod_total - len(shown_p)
        more_p = (" | … +%d 条" % extra_p) if extra_p > 0 else ""
        lines.append("- **生产线(%d)**: %s%s" % (prod_total, " | ".join(p_items), more_p))

    # 决议（仅 mine）
    decs = data.get("decisions_mine") or []
    decs_total = int(data.get("decisions_mine_total") or len(decs))
    if decs:
        shown_d = decs[:10]
        d_items = []
        for d in shown_d:
            meta_parts = []
            if d.get("cost_pp") is not None:
                meta_parts.append("%sPP" % _fmt_num(d.get("cost_pp"), 0))
            if d.get("days") is not None:
                meta_parts.append("%s天" % _fmt_num(d.get("days"), 0))
            suffix = ("(" + ",".join(meta_parts) + ")") if meta_parts else ""
            d_items.append("`%s`%s" % (d.get("key"), suffix))
        extra_d = decs_total - len(shown_d)
        more_d = (" … +%d 条" % extra_d) if extra_d > 0 else ""
        lines.append("- **可用决议(本国 %d)**: %s%s" % (decs_total, ", ".join(d_items), more_d))

    # 条件展开 1：待答事件（高优先级，13 天自动选 1！）
    events = data.get("events") or []
    events_total = int(data.get("events_total") or len(events))
    if events:
        lines.append("\n## ⚠️ 待处理事件(%d)（13 天未答将自动选选项 1）" % events_total)
        shown_e = events[:5]
        for ev in shown_e:
            lines.append("- **%s**: %s" % (ev.get("id"), ev.get("title") or ""))
            for opt in (ev.get("options") or [])[:5]:
                app = opt.get("applicable")
                tag_str = "适用" if app is True else ("不适用" if app is False else "未知")
                lines.append("  - 选项 %s [%s]: %s" % (opt.get("index"), tag_str, opt.get("label") or opt.get("name") or ""))
        extra_e = events_total - len(shown_e)
        if extra_e > 0:
            lines.append("- … 另有 %d 个待处理事件（可用 `hoi4_inspect(events)` 查看）" % extra_e)

    # 条件展开 2：活跃战斗（仅在开战或有战斗时展开）
    combats = data["war"].get("combats") or []
    if combats:
        lines.append("\n## 活跃地面战斗 (%d)" % len(combats))
        for cb in combats[:12]:
            us = cb.get("us") or {}
            them = cb.get("them") or {}
            lines.append(
                "- 省%s(%s): 我方%s师(org=%s) vs 敌方%s师(org=%s)"
                % (
                    cb.get("province"), cb.get("terrain") or "?",
                    us.get("front", "?"), _fmt_num(us.get("org"), 0),
                    them.get("front", "?"), _fmt_num(them.get("org"), 0),
                )
            )

    # 长期战略备忘与上回合执行回执
    if data.get("memo"):
        lines.append("\n## 战略备忘 (memo)\n" + str(data["memo"]).strip())
    if data.get("last_receipt"):
        lines.append("\n## 上回合回执\n" + str(data["last_receipt"]).strip())

    # 条件展开 3：本回合逃生舱深查结果
    if data.get("extra_inspect"):
        lines.append("\n## 本回合补充情报 (inspect)")
        lines.append("```json\n" + json.dumps(data["extra_inspect"], ensure_ascii=False, indent=1, default=str)[:4000] + "\n```")

    return "\n".join(lines)


def build_turn_brief(g, tag=None, memo=None, last_receipt=None, extra_inspect=None,
                     include_focus_candidates=True, include_research_candidates=True,
                     include_data=False, reset_state=False):
    """纯只读构建本回合紧凑简报。

    默认 `include_data=False`：仅返回高密度 `markdown`（~1.8-2.2 KB），避免 MCP 响应
    携带双份 6 KB 结构化副本造成 4 倍 token 膨胀；需要程序化消费原结构时传 `include_data=True`。
    """
    t0 = time.perf_counter()
    _timings = {}
    if reset_state:
        reset_turn_state()
    _t = time.perf_counter()
    ov = g.overview(tag)
    _timings["overview"] = round((time.perf_counter() - _t) * 1000.0, 2)
    d_info = _safe_call(lambda: g.date(), {}) or {}
    my_tag = tag or _safe_call(lambda: g.player_tag(), None) or (ov.get("country") or {}).get("tag")
    cur_pid = getattr(getattr(g, "p", None), "pid", None)
    cur_start = _proc_start_ticks(cur_pid)
    cur_date_raw = d_info.get("raw")

    paused = None
    try:
        import session
        paused = bool(session.is_paused(g))
    except Exception:
        pass

    _, _, win = _window_guard(g)

    # 读取持久化状态中的 memo / last_receipt（严格校验进程、国家与日期连续性）
    persisted = load_turn_state(
        pid=cur_pid, proc_start=cur_start, tag=my_tag, date_raw=cur_date_raw
    )
    if memo is None:
        memo = persisted.get("memo") or ""
    if last_receipt is None:
        last_receipt = persisted.get("last_receipt") or ""

    # 1. 待处理事件（纯读 O(1)，附只读 applicable 评估）
    _t = time.perf_counter()
    events_compact = []
    events_total = 0
    try:
        import events_win
        ev_raw = events_win.open_events(g, with_db=True, lang="zh", deep=False)
        ev_list = ev_raw.get("events") if isinstance(ev_raw, dict) else (ev_raw or [])
        events_total = len(ev_list or [])
        for ev in (ev_list or [])[:5]:
            eid = ev.get("event_id") or ev.get("id")
            app_by_idx = {}
            if eid:
                sel_ro = _safe_call(
                    lambda _e=eid: events_win.select_option(
                        g, event_id=_e, lang="zh", tag=my_tag or "GER", write=False
                    ),
                    None,
                )
                if isinstance(sel_ro, dict):
                    for so in sel_ro.get("options") or []:
                        if isinstance(so, dict) and so.get("index") is not None:
                            app_by_idx[so["index"]] = so.get("applicable")
            opts = []
            for o in (ev.get("options") or [])[:5]:
                idx = o.get("index")
                app = o.get("applicable") if "applicable" in o else app_by_idx.get(idx)
                opts.append({
                    "index": idx,
                    "name": o.get("name"),
                    "label": o.get("text") or o.get("title") or o.get("name"),
                    "applicable": app,
                })
            events_compact.append({
                "id": eid,
                "title": ev.get("title") or ev.get("name"),
                "options": opts,
            })
    except Exception:
        pass
    _timings["events"] = round((time.perf_counter() - _t) * 1000.0, 2)

    # 2. 战争与活跃战斗（纯读，避免触发 30s 扫描）
    _t = time.perf_counter()
    war_info = {"at_war": False, "enemies": [], "combats": []}
    try:
        import combat
        enemies = list(combat.enemy_tags(g, my_tag) or [])
        war_info["enemies"] = enemies
        war_info["at_war"] = bool(enemies)
        if enemies:
            recs = combat.combats(g, only_mine=True, me=my_tag) or []
            war_info["combats"] = [
                {
                    "province": r.get("province"),
                    "terrain": r.get("terrain"),
                    "us": r.get("us"),
                    "them": r.get("them"),
                }
                for r in recs[:12]
            ]
    except Exception:
        pass
    _timings["combat"] = round((time.perf_counter() - _t) * 1000.0, 2)

    # 2.5 宣战理由 (CB) 与正当化进度（纯只读 O(1)/毫秒级，绝不触发深扫）
    _t = time.perf_counter()
    wargoals_brief = []
    justifying_brief = []
    try:
        import diplomacy as _dip
        enemies_set = {str(x).upper() for x in (war_info.get("enemies") or ())}
        wg_rows, wg_ok = _dip.country_wargoals_fast(g, tag=my_tag, valid_only=True)
        if wg_ok:
            for r in (wg_rows or []):
                tgt_t = str(r.get("target_tag") or "").upper()
                if not tgt_t or tgt_t == str(my_tag or "").upper() or tgt_t in enemies_set:
                    continue
                wargoals_brief.append({
                    "target": tgt_t,
                    "wargoal": r.get("wargoal") or "wargoal",
                    "ptr": r.get("ptr"),
                })
        for j in (_dip.justifying_fast(g, tag=my_tag) or []):
            justifying_brief.append({
                "target": j.get("target"),
                "days_left": j.get("days_left"),
                "eta_date": j.get("eta_date"),
            })
    except Exception:
        pass
    _timings["wargoals"] = round((time.perf_counter() - _t) * 1000.0, 2)

    # 3. 条件展开：国策空闲时自动列候选
    _t = time.perf_counter()
    focus_info = ov.get("focus") or {}
    selectable_focuses = []
    if include_focus_candidates and not focus_info.get("current"):
        sf = _safe_call(lambda: g.selectable_focuses(my_tag), None)
        if isinstance(sf, dict):
            selectable_focuses = sf.get("selectable") or sf.get("focuses") or []
        elif isinstance(sf, list):
            selectable_focuses = sf
    _timings["selectable_focuses"] = round((time.perf_counter() - _t) * 1000.0, 2)

    # 4. 条件展开：科研槽空闲且科技库已就绪时列候选（不阻塞冷扫描）
    _t = time.perf_counter()
    research_slots = ov.get("research") or []
    free_research = sum(1 for s in research_slots if not s.get("slot_busy"))
    research_candidates = []
    if include_research_candidates and free_research > 0:
        if getattr(g, "_tech_by_name", None):
            av = _safe_call(lambda: g.available_research(my_tag), None) or []
            research_candidates = list(av[:25])
    _timings["research_candidates"] = round((time.perf_counter() - _t) * 1000.0, 2)

    # 5. 精简本国决议列表
    cand_block = ov.get("candidates") or {}
    dec_mine = [
        d for d in (cand_block.get("available_decisions") or [])
        if isinstance(d, dict) and d.get("mine", True)
    ]

    # 精简 country 字段，去掉超大冗余字段；顺手补读纯内存 command_power（0 注入）
    c_raw = ov.get("country") or {}
    country_slim = {
        k: c_raw.get(k)
        for k in (
            "tag", "name", "political_power", "political_power_daily",
            "command_power", "command_power_daily", "stability", "war_support",
            "manpower", "fuel", "experience",
        )
        if k in c_raw
    }
    if "command_power" not in country_slim:
        cp = _safe_call(lambda: g.p.i64(g._c(my_tag).ptr + 0x1F0) / 1e5, None)
        if cp is not None and 0.0 <= cp <= 1000.0:
            country_slim["command_power"] = round(cp, 2)

    cons_all = ov.get("construction") or []
    prod_all = ov.get("production") or []
    _prefetched = {
        "date": d_info,
        "lines": prod_all,
        "construction": cons_all,
        "research_slots": research_slots,
        "current_focus": ({"key": focus_info["current"]} if focus_info.get("current") else None),
        "summary": c_raw,
        "unassigned_count": (ov.get("army_summary") or {}).get("unassigned_divisions"),
        "divisions_n": (ov.get("army_summary") or {}).get("total_divisions"),
        "enemies": war_info.get("enemies"),
    }

    _t = time.perf_counter()
    rd_eval = None
    try:
        import reddots as _rd
        try:
            rd_eval = _rd.evaluate(g, fast=True, prefetched=_prefetched)
        except TypeError:
            rd_eval = _rd.evaluate(g, fast=True)
    except Exception:
        rd_eval = None
    _timings["red_dots"] = round((time.perf_counter() - _t) * 1000.0, 2)

    _t = time.perf_counter()
    health_board = None
    try:
        import macros as _mc
        try:
            health_board = _mc.strategic_health_board(g, tag=my_tag, prefetched=_prefetched)
        except TypeError:
            health_board = _mc.strategic_health_board(g, tag=my_tag)
    except Exception:
        health_board = None
    _timings["health_board"] = round((time.perf_counter() - _t) * 1000.0, 2)

    _t = time.perf_counter()
    ap_status = None
    try:
        import autopilot as _ap
        _st_ap = _ap.status(g, tag=my_tag, journal_limit=6)
        if isinstance(_st_ap, dict) and _st_ap.get("active"):
            ap_status = _st_ap
    except Exception:
        ap_status = None
    _timings["autopilot"] = round((time.perf_counter() - _t) * 1000.0, 2)

    data = {
        "meta": {
            "date": ov.get("date") or d_info.get("text"),
            "date_raw": d_info.get("raw"),
            "date_source": d_info.get("source"),
            "tag": my_tag,
            "pid": getattr(getattr(g, "p", None), "pid", None),
            "paused": paused,
            "window": {
                "focused": win.get("focused"),
                "minimized": win.get("minimized"),
                "visible": win.get("visible"),
            } if isinstance(win, dict) else {},
            "elapsed_s": round(time.perf_counter() - t0, 3),
            "timings_ms": _timings,
        },
        "red_dots": rd_eval,
        "health_board": health_board,
        "autopilot": ap_status,
        "alerts": list(ov.get("alerts") or []),
        "country": country_slim,
        "factories": ov.get("factories") or {},
        "states_count": ov.get("states"),
        "army": ov.get("army_summary") or {},
        "focus": focus_info,
        "selectable_focuses": selectable_focuses[:25],
        "selectable_focuses_total": len(selectable_focuses),
        "research": research_slots,
        "research_candidates": research_candidates,
        "construction": cons_all[:12],
        "construction_total": len(cons_all),
        "production": [
            {
                "index": p.get("index"),
                "equipment": p.get("equipment"),
                "factories": p.get("factories"),
                "class": p.get("class"),
            }
            for p in prod_all[:16]
        ],
        "production_total": len(prod_all),
        "decisions_mine": dec_mine[:12],
        "decisions_mine_total": len(dec_mine),
        "events": events_compact,
        "events_total": events_total,
        "war": war_info,
        "wargoals": wargoals_brief,
        "justifying": justifying_brief,
        "memo": memo,
        "last_receipt": last_receipt,
        "extra_inspect": extra_inspect,
    }
    _t = time.perf_counter()
    md = _render_brief_markdown(data)
    _timings["render"] = round((time.perf_counter() - _t) * 1000.0, 2)
    _timings["total"] = round((time.perf_counter() - t0) * 1000.0, 2)
    data["meta"]["elapsed_s"] = round((_timings["total"] / 1000.0), 3)
    out = {
        "ok": True,
        "markdown": md,
        "approx_tokens": _estimate_tokens(md),
        "elapsed_s": data["meta"]["elapsed_s"],
        "timings_ms": _timings,
    }
    if include_data:
        out["brief"] = data
    return out


# ===================================================================== 受限逃生舱
def inspect_topics(g, queries, max_queries=MAX_INSPECT_QUERIES, tag=None):
    """按需只读深查（每回合最多 1 次调用，单次最多 3 条声明式查询）。"""
    if not isinstance(queries, list) or not queries:
        return {"ok": False, "error": "queries 必须是非空列表，每项含 topic"}
    truncated = len(queries) > max_queries
    items = queries[:max_queries]
    results = []
    for q in items:
        if not isinstance(q, dict) or not q.get("topic"):
            results.append({"ok": False, "error": "查询项缺少 topic", "query": q})
            continue
        topic = str(q["topic"]).strip().lower()
        if topic not in ALLOWED_INSPECT_TOPICS:
            results.append({
                "ok": False, "topic": topic,
                "error": "不支持的 topic；允许值: " + ", ".join(sorted(ALLOWED_INSPECT_TOPICS)),
            })
            continue
        qtag = q.get("tag") or tag
        try:
            res = _run_inspect_one(g, topic, q, qtag)
            results.append({"ok": True, "topic": topic, "data": res})
        except Exception as e:
            results.append({"ok": False, "topic": topic, "error": repr(e)})
    return {"ok": True, "truncated": truncated, "count": len(results), "results": results}


def _run_inspect_one(g, topic, q, tag):
    if topic == "country":
        return g.country_stats(tag)
    if topic == "events":
        import events_win
        lang = q.get("lang") or "zh"
        deep = bool(q.get("sweep") or q.get("deep"))
        res = events_win.open_events(g, with_db=True, lang=lang, deep=deep)
        if not isinstance(res, dict):
            return res
        my_tag = tag or _safe_call(lambda: g.player_tag(), None) or "GER"
        enriched = []
        for ev in (res.get("events") or [])[:5]:
            ev_copy = dict(ev)
            eid = ev.get("event_id") or ev.get("id")
            if eid:
                sel_ro = _safe_call(
                    lambda _e=eid: events_win.select_option(
                        g, event_id=_e, lang=lang, tag=my_tag, write=False
                    ),
                    None,
                )
                if isinstance(sel_ro, dict):
                    ev_copy["recommended"] = sel_ro.get("recommended")
                    ev_copy["recommendation_basis"] = sel_ro.get("recommendation_basis")
                    ev_copy["recommendation_blocked"] = sel_ro.get("recommendation_blocked")
                    ev_copy["auto_resolve_option"] = sel_ro.get("auto_resolve_option")
                    ev_copy["auto_option"] = sel_ro.get("auto_resolve_option")
                    ev_copy["unresolved_scope"] = sel_ro.get("unresolved_scope")
                    # 2026-11-25: 合并而非覆写 options，保留静态 DB 中的 effects / ai_chance / name_key，
                    # 避免模型查事件时丢失选项效果（例如 GER_inner_circle.01 动态占位符事件）。
                    if sel_ro.get("options"):
                        orig_by_idx = {
                            o.get("index"): dict(o)
                            for o in (ev_copy.get("options") or ())
                            if isinstance(o, dict) and o.get("index") is not None
                        }
                        merged_opts = []
                        for so in sel_ro["options"]:
                            idx_o = so.get("index")
                            base_o = orig_by_idx.get(idx_o, {})
                            base_o.update(so)
                            merged_opts.append(base_o)
                        ev_copy["options"] = merged_opts
            enriched.append(ev_copy)
        out_ev = dict(res)
        out_ev["events"] = enriched
        return out_ev
    if topic == "selectable_focuses":
        return g.selectable_focuses(tag)
    if topic == "research_available":
        flt = str(q.get("filter") or "").lower()
        details = None
        if hasattr(g, "available_research_detailed"):
            try:
                details = g.available_research_detailed(tag) or []
            except Exception:
                details = None
        if details is not None:
            if flt:
                details = [x for x in details if flt in str(x.get("key") or "").lower() or flt in str(x.get("category") or "").lower()]
            names = [x["key"] for x in details]
            return {"count": len(details), "technologies": names[:40], "details": details[:40]}
        out = g.available_research(tag) or []
        if flt:
            out = [x for x in out if flt in str(x).lower()]
        return {"count": len(out), "technologies": out[:40]}
    if topic == "advisors":
        if hasattr(g, "available_advisors"):
            rows = g.available_advisors(tag)
            return rows[:30] if isinstance(rows, list) else rows
        if hasattr(g, "idea_catalog"):
            return g.idea_catalog(tag, slot=q.get("slot"), filter_text=q.get("filter") or "", include_advisors=True, limit=40)
        return g.politics(tag)
    if topic == "idea_catalog":
        if hasattr(g, "idea_catalog"):
            return g.idea_catalog(tag, slot=q.get("slot"), filter_text=q.get("filter") or "", include_advisors=True, limit=60)
        return g.politics(tag)
    if topic == "autopilot":
        import autopilot as _ap
        return _ap.status(g, tag=tag, journal_limit=20)
    if topic == "strategy_catalog":
        import autopilot as _ap
        return _ap.strategy_catalog(domain=q.get("filter") or q.get("slot"))
    if topic == "decisions":
        which = q.get("which") or "visible"
        rows = g.decisions(tag, which=which, rich=True) or []
        if q.get("mine_only", True):
            rows = [d for d in rows if isinstance(d, dict) and d.get("mine", True)]
        return rows[:25]
    if topic == "armies":
        arm = g.armies(tag)
        ogs = _safe_call(lambda: g.order_groups(tag), None)
        return {"armies": arm, "order_groups": ogs}
    if topic == "war_readiness":
        target = q.get("target")
        if not target:
            raise ValueError("war_readiness 需要参数 target（目标国家 tag）")
        return g.war_readiness_check(str(target).upper(), tag)
    if topic == "combat":
        import combat
        recs = combat.combats(g, only_mine=not bool(q.get("all"))) or []
        return {"count": len(recs), "combats": recs[:20]}
    if topic == "stockpile":
        st = g.equipment_stockpile(tag, limit=40)
        return st
    if topic == "resources":
        return g.resources(tag)
    if topic == "state":
        sid = q.get("state") if q.get("state") is not None else q.get("state_id")
        if sid is None:
            raise ValueError("state 需要参数 state（或 state_id）")
        return g.state_detail(int(sid), tag)
    if topic == "peace":
        import peacewin
        out = peacewin.summary(g)
        loser = q.get("loser")
        if loser and out.get("open"):
            lose_idx = g.country(str(loser).upper()).tag_id
            win_tag = q.get("winner")
            win_idx = g.country(str(win_tag).upper()).tag_id if win_tag else int(g.player_tag_id())
            with g:
                opts = peacewin.options(g, lose_idx, winner_idx=win_idx)
            if opts.get("ok"):
                opts["loser"] = str(loser).upper()
            out["options"] = opts
        return out
    if topic == "air":
        return g.air_wings(tag, limit=30)
    if topic == "navy":
        import navalops_win
        fleets = navalops_win.naval_fleets(g, tag)
        return {"count": len(fleets), "fleets": fleets[:20]}
    if topic == "staff":
        # 严格只读参谋建议（绝不写档）；默认返回紧凑摘要（~1 KB，避免 8.5 KB 胖包撑爆 compact 上下文），
        # 需要原始全量结构时传 compact=False 或 full=True。
        import staff_industry, staff_army, staff_focus
        ind = _safe_call(lambda: staff_industry.plan(g, tag))
        arm = _safe_call(lambda: staff_army.plan(g, tag))
        foc = _safe_call(lambda: staff_focus.plan(g, tag))
        want_compact = bool(q.get("compact", True)) and not bool(q.get("full", False))
        if not want_compact:
            return {"industry": ind, "army": arm, "focus": foc}
        return _compact_staff_report(ind, arm, foc)
    if topic == "health":
        import macros as _mc
        return _mc.strategic_health_board(g, tag=tag)
    raise ValueError("unknown topic: %s" % topic)


def _compact_staff_report(ind, arm, foc):
    """将三大参谋建议压缩为高密度决策摘要（~1 KB，相比原始 8.5 KB 节省 ~85%）。"""
    out = {"compact": True}
    if isinstance(ind, dict):
        prod = ind.get("production") or {}
        cons = ind.get("construction") or {}
        out["industry"] = {
            "at_war": ind.get("at_war"),
            "problems": ind.get("problems") or [],
            "mil_idle": prod.get("mil_idle"),
            "production_proposal": (prod.get("proposal") or [])[:4],
            "construction": {
                "civ_gap": cons.get("civ_gap"),
                "mil_gap": cons.get("mil_gap"),
                "queue_len": cons.get("queue_len"),
                "want": cons.get("want"),
                "skip": cons.get("skip"),
                "top_items": [
                    {"state": x.get("state"), "building": x.get("building"),
                     "slots_free": x.get("slots_free")}
                    for x in (cons.get("items") or [])[:4]
                ],
            },
        }
    else:
        out["industry"] = ind
    if isinstance(arm, dict):
        out["army"] = {
            "divisions": arm.get("divisions"),
            "in_training": arm.get("in_training"),
            "cap": arm.get("cap"),
            "binding": arm.get("binding"),
            "problems": arm.get("problems") or [],
            "actions_summary": [
                {"action": a.get("action"), "count": a.get("count")}
                for a in (arm.get("actions") or [])[:4]
                if isinstance(a, dict)
            ],
        }
    else:
        out["army"] = arm
    if isinstance(foc, dict):
        out["focus"] = {
            "running": foc.get("running"),
            "action": foc.get("action"),
            "problems": foc.get("problems") or [],
        }
    else:
        out["focus"] = foc
    return out


# ===================================================================== Plan 校验与执行器
def validate_plan(plan):
    """静态校验强模型输出的 Plan DSL 结构、参数规范与安全上限。"""
    errors = []
    if not isinstance(plan, dict):
        return False, ["plan 必须是 JSON 对象"]

    actions = plan.get("actions")
    if actions is None:
        actions = []
    if not isinstance(actions, list):
        errors.append("actions 必须是列表")
        actions = []
    if len(actions) > MAX_ACTIONS_PER_TURN:
        errors.append("actions 超过单回合上限 %d（收到 %d）" % (MAX_ACTIONS_PER_TURN, len(actions)))

    for i, act in enumerate(actions):
        if not isinstance(act, dict):
            errors.append("actions[%d] 必须是对象" % i)
            continue
        op = act.get("op")
        if op not in ALLOWED_ACTIONS:
            errors.append("actions[%d].op=%r 不在白名单内" % (i, op))
        else:
            _, arg_errs = _normalize_action_args(op, act.get("args"), prefix="actions[%d]." % i)
            errors.extend(arg_errs)
        on_err = act.get("on_error", "continue")
        if on_err not in ("continue", "abort"):
            errors.append("actions[%d].on_error=%r 仅支持 continue/abort" % (i, on_err))

    insp = plan.get("inspect")
    if insp is not None:
        if not isinstance(insp, list):
            errors.append("inspect 必须是列表")
        elif len(insp) > MAX_INSPECT_QUERIES:
            errors.append("inspect 超过上限 %d" % MAX_INSPECT_QUERIES)
        else:
            for i, q in enumerate(insp):
                if not isinstance(q, dict) or not q.get("topic"):
                    errors.append("inspect[%d] 必须是含 topic 的对象" % i)
                    continue
                topic = str(q["topic"]).strip().lower()
                if topic not in ALLOWED_INSPECT_TOPICS:
                    errors.append("inspect[%d].topic=%r 不在白名单内" % (i, topic))
                elif topic == "war_readiness" and not q.get("target"):
                    errors.append("inspect[%d].war_readiness 缺少参数 target" % i)
                elif topic == "state" and q.get("state") is None and q.get("state_id") is None:
                    errors.append("inspect[%d].state 缺少参数 state（或 state_id）" % i)

    memo = plan.get("memo")
    if memo is not None and not isinstance(memo, str):
        errors.append("memo 必须是字符串")

    return len(errors) == 0, errors


def _dispatch_action(g, op, args, tag):
    """将单条高层 Plan 动词映射到 Game/Housekeep/Events/Session 真实调用。"""
    norm, errs = _normalize_action_args(op, args)
    if errs:
        return {"ok": False, "blocked_by": "invalid_args", "error": "; ".join(errs)}
    args = norm
    t = args.get("tag") or tag
    if op == "select_event":
        import events_win
        return events_win.select_option(
            g,
            index=args.get("index"),
            event_id=args.get("event_id") or None,
            lang=args.get("lang") or "zh",
            tag=t or "GER",
            write=True,
        )
    if op == "set_focus":
        return g.set_focus(args["key"], t)
    if op == "select_decision":
        return g.select_decision(args["key"], t)
    if op == "add_idea":
        return g.add_idea(args["key"], t)
    if op == "hire_advisor":
        return g.hire_advisor(args["key"], t, slot_override=args.get("slot"))
    if op == "fill_research":
        return g.fill_all_research_slots(
            args.get("technologies") or [],
            t,
            max_attempts=int(args.get("max_attempts") or 30),
        )
    if op == "start_research":
        return g.start_research(args["technology"], slot=args.get("slot"), tag=t)
    if op == "add_construction":
        return g.add_construction(
            int(args["state"]),
            str(args["building"]),
            level=int(args.get("level", 1)),
            province=int(args.get("province", 0)),
            tag=t,
        )
    if op == "remove_construction":
        return g.remove_construction(int(args["index"]), t)
    if op == "set_production":
        return g.set_production_amount(int(args["line"]), int(args["amount"]), t)
    if op == "upgrade_production":
        return g.set_production_line_equipment(
            int(args["line"]),
            str(args["equipment"]),
            tag=t,
            dry_run=bool(args.get("dry_run", False)),
        )
    if op == "rebalance_production":
        import macros as _mc
        return _mc.rebalance_production(
            g,
            tag=t,
            dry_run=bool(args.get("dry_run", False)),
            min_factories_per_line=int(args.get("min_factories_per_line", 1)),
        )
    if op == "upgrade_template_preset":
        import macros as _mc
        return _mc.upgrade_template_preset(
            g,
            preset=args.get("preset", "inf_9_3"),
            template=args.get("template"),
            tag=t,
            auto_rebalance=bool(args.get("auto_rebalance", True)),
            dry_run=bool(args.get("dry_run", False)),
        )
    if op == "plan_front_allocation":
        import macros as _mc
        return _mc.plan_front_allocation(
            g,
            target_tag=str(args["target"]).upper(),
            tag=t,
            reserve_ratio=float(args.get("reserve_ratio", 0.12)),
            min_reserve=int(args.get("min_reserve", 4)),
            execute=bool(args.get("execute", False)),
            allow_understrength=bool(args.get("allow_understrength", False)),
        )
    if op == "housekeep":
        return g.housekeep(t, args.get("target_construction"), args.get("technologies") or [])
    if op == "trade":
        return g.create_trade(
            partner=str(args["partner"]).upper(),
            resource=str(args["resource"]).lower(),
            amount=args.get("amount"),
            factories=args.get("factories"),
            tag=t,
            dry_run=bool(args.get("dry_run", False)),
        )
    if op == "add_to_army":
        return g.add_divisions_to_army(
            orders_group=args["orders_group"],
            divisions=args.get("divisions"),
            count=args.get("count"),
            from_army=args.get("from_army"),
            order_id=args.get("order_id"),
            tag=t,
        )
    if op == "train_divisions":
        return g.train_divisions(
            template_name=args.get("template"),
            lines=int(args.get("lines", 2)),
            state_id=args.get("state"),
            tag=t,
        )
    if op == "deploy_ready":
        return g.deploy_ready_conveyors(t, float(args.get("min_progress") or 0.0))
    if op == "purge_orders":
        return g.purge_empty_orders(t)
    if op == "reset_battleplans":
        return g.reset_battleplans(t)
    if op == "army_battleplan":
        return g.activate_army_battleplans(
            str(args["enemy_tag"]).upper(),
            t,
            bool(args.get("force", False)),
            int(args.get("wait_seconds") or 0),
            armies=args.get("armies"),
            split=bool(args.get("split", True)),
        )
    if op == "justify_wargoal":
        return g.justify_wargoal(
            str(args["target"]).upper(),
            wargoal=args.get("wargoal", "take_state"),
            states=args.get("states"),
            force=bool(args.get("force", False)),
            dry_run=bool(args.get("dry_run", False)),
            tag=t,
        )
    if op == "declare_war":
        # 硬规则：绝不通过 Plan DSL 开启 override_checks
        return g.declare_war(
            str(args["target"]).upper(),
            dry_run=bool(args.get("dry_run", False)),
            force=False,
            override_checks=False,
            ack_co_belligerents=bool(args.get("ack_co_belligerents", False)),
        )
    if op == "peace_demands":
        import peacewin
        lose_idx = g.country(str(args["loser"]).upper()).tag_id
        win_tag = args.get("winner")
        win_idx = g.country(str(win_tag).upper()).tag_id if win_tag else int(g.player_tag_id())
        kinds = args.get("kinds") or ["state"]
        states = args.get("states")
        with g:
            conf = peacewin.live_conference(g)
            if not conf:
                return {"ok": False, "error": "no peace conference is open"}
            finfo = {}
            try:
                turn, acts = peacewin.submit_demands(
                    g, conf, win_idx, lose_idx, kinds, states=states, filter_info=finfo
                )
            except ValueError as e:
                return {"ok": False, "error": str(e), **finfo}
            out = {"ok": True, "turn": turn, "actions": len(acts),
                   "loser": str(args["loser"]).upper(), "status": peacewin.summary(g)}
            if states is not None:
                out["matched_states"] = finfo.get("matched_states")
                if finfo.get("ignored_states"):
                    out["ignored_states"] = finfo.get("ignored_states")
            return out
    if op == "peace_finish":
        import peacewin
        act = str(args.get("action") or "done")
        cidx = int(g.player_tag_id())
        with g:
            if act == "done":
                return peacewin.finish(g, cidx)
            return peacewin.execute(g, act, cidx)
    if op == "set_game_speed":
        return g.set_game_speed(
            int(args["speed"]),
            measure_seconds=args.get("measure_seconds"),
        )
    if op == "pause":
        import session
        return session.pause(g, paused=True)
    if op == "ack_red_dot":
        import reddots as _rd
        return _rd.ack(
            g,
            code=args["code"],
            reason=args["reason"],
            snooze_days=args.get("snooze_days"),
            force=bool(args.get("force", False)),
        )
    if op == "set_standing_orders":
        import autopilot as _ap
        return _ap.set_standing_orders(
            g,
            profile=args["profile"],
            dry_run=bool(args.get("dry_run", False)),
            replace=bool(args.get("replace", True)),
            tag=t,
        )
    if op == "clear_standing_orders":
        import autopilot as _ap
        return _ap.clear_standing_orders(g, reason=args.get("reason") or "", tag=t)
    if op == "autopilot_tick":
        import autopilot as _ap
        if bool(args.get("ack", False)):
            ack_res = _ap.acknowledge_wakeup(
                g,
                tag=t,
                reason=args.get("reason") or "autopilot_tick(ack=true)",
                extend_max_days=bool(args.get("extend_max_days", False)),
            )
            if bool(args.get("dry_run", False)):
                tick_res = _ap.run_tick(g, tag=t, dry_run=True)
                tick_res["ack"] = ack_res
                return tick_res
            return ack_res
        return _ap.run_tick(g, tag=t, dry_run=bool(args.get("dry_run", False)))
    if op == "ack_autopilot":
        import autopilot as _ap
        return _ap.acknowledge_wakeup(
            g,
            tag=t,
            reason=args.get("reason") or "manual_checkin_ok",
            extend_max_days=bool(args.get("extend_max_days", False)),
        )
    if op == "resume":
        empty = _has_empty_orders(g, t)
        if empty:
            return {
                "ok": False,
                "blocked_by": "empty_orders",
                "empty_order_ids": empty,
                "error": "存在 0 师订单，拒绝解除暂停（请先执行 purge_orders）",
            }
        try:
            import reddots as _rd
            gate = _rd.check_unpause_gate(g, ensure_paused=True)
            if gate:
                return gate
        except Exception as _e:
            import diag as _diag
            _diag.warn("turnengine.resume.unpause_gate", _e)
        import session
        return session.pause(g, paused=False)
    raise ValueError("unsupported op: %s" % op)


def _summarize_action_result(op, args, res):
    """把单条执行结果压缩成一行极简回执，节省下回合 token。"""
    if not isinstance(res, dict):
        return "[OK] %s -> %s" % (op, str(res)[:80])
    ok = res.get("ok")
    blocked = res.get("blocked_by")
    err = res.get("error") or res.get("reason")
    arg_brief = ",".join(
        "%s=%s" % (k, v)
        for k, v in (args or {}).items()
        if k not in ("tag", "on_error")
    )[:60]
    if ok is False or blocked or err:
        why = blocked or err or "failed"
        return "[FAIL] %s(%s): %s" % (op, arg_brief, str(why)[:100])
    return "[OK] %s(%s)" % (op, arg_brief)


def execute_plan(g, plan, dry_run=False, tag=None, persist_state=True):
    """事务式串行执行 Plan DSL，内置前台守卫、版本守卫、写后进程验活与紧凑回执。"""
    ok, errs = validate_plan(plan)
    if not ok:
        return {"ok": False, "blocked_by": "invalid_plan", "errors": errs}

    raw_actions = plan.get("actions") or []
    actions = []
    for a in raw_actions:
        norm_args, _ = _normalize_action_args(a["op"], a.get("args"))
        actions.append({
            "op": a["op"],
            "args": norm_args,
            "on_error": a.get("on_error", "continue"),
        })
    memo = plan.get("memo")

    # 版本守卫（防止 1.20 误写）
    v_ok, v_err = _version_guard(g)
    if not v_ok:
        return {"ok": False, "blocked_by": "version_mismatch", "error": v_err}

    if dry_run:
        preview = [
            "[DRY] %s(%s)" % (a["op"], ",".join("%s=%s" % (k, v) for k, v in (a.get("args") or {}).items()))
            for a in actions
        ]
        return {
            "ok": True,
            "dry_run": True,
            "planned_count": len(actions),
            "receipt_text": "\n".join(preview) if preview else "(无动作)",
            "memo": memo,
        }

    # 进程存活前置检查
    if not _process_alive(g):
        return {"ok": False, "blocked_by": "process_dead", "error": "游戏进程已不可读"}

    # 前台窗口守卫：有任何实写引擎命令时强制检查（不抢焦点）；
    # 纯配置/签收/干跑（set_standing_orders、clear_standing_orders、ack_autopilot、
    # autopilot_tick dry_run 或 ack=true）免前台。
    need_fg = any(
        (a["op"] not in _NO_FOREGROUND_NEEDED)
        and not (
            a["op"] == "autopilot_tick"
            and (bool((a.get("args") or {}).get("dry_run")) or bool((a.get("args") or {}).get("ack")))
        )
        for a in actions
    )
    if need_fg:
        fg_ok, fg_reason, ws = _window_guard(g)
        if not fg_ok:
            return {
                "ok": False,
                "blocked_by": fg_reason,
                "window": ws,
                "error": "写命令前游戏窗口必须位于前台且非最小化（请手动切回游戏窗口后重试）",
            }

    _d_before = _safe_call(lambda: g.date(), {}) or {}
    _date_before = _d_before.get("text") if isinstance(_d_before, dict) else None
    _raw_before = _d_before.get("raw") if isinstance(_d_before, dict) else None
    _paused_before = None
    try:
        import session as _sess_exec
        _paused_before = bool(_sess_exec.is_paused(g))
    except Exception:
        _paused_before = None

    results = []
    receipt_lines = []
    aborted = False
    abort_reason = None

    for idx, act in enumerate(actions):
        op = act["op"]
        args = dict(act.get("args") or {})
        on_err = act.get("on_error", "continue")
        t0 = time.perf_counter()
        try:
            res = _dispatch_action(g, op, args, tag)
        except Exception as e:
            res = {"ok": False, "error": repr(e)}
        dt = round(time.perf_counter() - t0, 3)

        # 硬规则：每条写命令之后立刻验活（纯读 4 字节）
        alive = _process_alive(g)
        line = _summarize_action_result(op, args, res)
        if not alive:
            line += " [PROCESS_DEAD!]"
        receipt_lines.append(line)
        results.append({
            "index": idx,
            "op": op,
            "ok": bool(alive and (not isinstance(res, dict) or (res.get("ok", True) is not False and not res.get("blocked_by")))),
            "alive": alive,
            "elapsed_s": dt,
            "summary": line,
            "raw": res,
        })
        if not alive:
            aborted = True
            abort_reason = "process_dead_after_%s" % op
            break
        if not results[-1]["ok"] and on_err == "abort":
            aborted = True
            abort_reason = "action_failed_%s" % op
            break

    receipt_text = "\n".join(receipt_lines) if receipt_lines else "(本回合无写动作)"

    # 覆写式保存回合状态（绑定 pid + proc_start + tag + date_raw，防止换档/重启串味）
    if persist_state:
        cur_pid = getattr(getattr(g, "p", None), "pid", None)
        cur_start = _proc_start_ticks(cur_pid)
        cur_tag = tag or _safe_call(lambda: g.player_tag(), None)
        cur_date_raw = (_safe_call(lambda: g.date(), {}) or {}).get("raw")
        st = load_turn_state(
            pid=cur_pid, proc_start=cur_start, tag=cur_tag, date_raw=cur_date_raw
        )
        st["turn"] = int(st.get("turn") or 0) + 1
        st["pid"] = cur_pid
        st["proc_start"] = cur_start
        st["tag"] = cur_tag
        st["date_raw"] = cur_date_raw
        if memo is not None:
            st["memo"] = memo
        st["last_receipt"] = receipt_text
        st["updated_at"] = time.time()
        save_turn_state(st)

    # 模型亲自执行完有效动作后刷新自动驾驶心跳与无进展计数（不重置 max_autopilot_days 总上限；
    # 若本回合仅调用 ack_autopilot 已完成显式签收则不重复覆盖）
    if results and not aborted and any(r["op"] != "ack_autopilot" for r in results):
        try:
            import autopilot as _ap
            if _ap.is_active(g, tag=tag):
                _ap.acknowledge_wakeup(g, tag=tag)
        except Exception:
            pass

    _d_after = _safe_call(lambda: g.date(), {}) or {}
    _date_after = _d_after.get("text") if isinstance(_d_after, dict) else None
    _raw_after = _d_after.get("raw") if isinstance(_d_after, dict) else None
    _paused_after = None
    try:
        import session as _sess_exec
        _paused_after = bool(_sess_exec.is_paused(g))
    except Exception:
        _paused_after = None
    _days_elapsed = (
        round((float(_raw_after) - float(_raw_before)) / 24.0, 2)
        if isinstance(_raw_before, (int, float)) and isinstance(_raw_after, (int, float))
        else 0.0
    )
    _unpaused_for_drain = bool(
        (_paused_before and (_paused_after is False or _days_elapsed > 0))
        or _days_elapsed > 0
    )

    ok_count = sum(1 for r in results if r["ok"])
    return {
        "ok": not aborted,
        "aborted": aborted,
        "abort_reason": abort_reason,
        "executed": len(results),
        "succeeded": ok_count,
        "failed": len(results) - ok_count,
        "receipt_text": receipt_text,
        "results": results,
        "date_before": _date_before,
        "date_after": _date_after,
        "days_elapsed": _days_elapsed,
        "paused_before": _paused_before,
        "paused_after": _paused_after,
        "unpaused_for_drain": _unpaused_for_drain,
    }


# ===================================================================== 等钟 + 简报合一
def wait_and_brief(g, max_days=14, until_date=None, signals=None,
                   timeout_seconds=None, pause_on_wake=True,
                   respect_red_dots=True, tag=None, include_data=False,
                   autopilot=None, fast_forward=False):
    """睡到下一个唤醒信号（并自动暂停），醒来后立即附带新一回合紧凑简报。"""
    import turnloop
    tl = turnloop.TurnLoop(g, poll=1.5)
    tm = turnloop.resolve_wait_timeout(requested=timeout_seconds)
    sigs = turnloop.ALL_SIGNALS if signals is None else tuple(signals)
    wait_res = tl.wait(
        max_days=int(max_days or 14),
        until_date=tuple(until_date) if until_date else None,
        signals=sigs,
        timeout_seconds=tm["effective"],
        pause_on_wake=bool(pause_on_wake),
        respect_red_dots=bool(respect_red_dots),
        timeout_meta=tm,
        autopilot=autopilot,
        fast_forward=bool(fast_forward),
    )
    brief_res = build_turn_brief(g, tag=tag, include_data=include_data)
    wait_block = {
        "woke_because": wait_res.get("woke_because"),
        "days_elapsed": wait_res.get("waited_game_days", wait_res.get("days_elapsed")),
        "waited_s": wait_res.get("waited_real_seconds", wait_res.get("waited_s")),
        "timeout_seconds": wait_res.get("effective_timeout_seconds", tm["effective"]),
        "requested_timeout_seconds": tm["requested"],
        "timeout_max_seconds": tm["max_s"],
        "timeout_clamped": tm["clamped"],
        "paused": wait_res.get("paused"),
        "blocked_by": wait_res.get("blocked_by"),
        "red_dot_paused": wait_res.get("red_dot_paused"),
        "red_dot_codes": wait_res.get("red_dot_codes") or wait_res.get("blocking_codes"),
    }
    if wait_res.get("wargoal_woke"):
        wait_block["wargoal_woke"] = True
        wait_block["wargoals"] = wait_res.get("wargoals")
    for _wk in ("wake_kind", "clock_stalled", "stall_reason", "peace_open", "blocked_pre_wait"):
        if _wk in wait_res and wait_res.get(_wk) is not None:
            wait_block[_wk] = wait_res.get(_wk)
    if wait_res.get("autopilot") is not None:
        wait_block["autopilot"] = wait_res.get("autopilot")
        if wait_res.get("autopilot_woke"):
            wait_block["autopilot_woke"] = True
    out = {
        "ok": wait_res.get("ok", True),
        "wait": wait_block,
        "markdown": brief_res["markdown"],
        "approx_tokens": brief_res["approx_tokens"],
    }
    if wait_res.get("blocked_by"):
        out["blocked_by"] = wait_res["blocked_by"]
    if include_data and "brief" in brief_res:
        out["brief"] = brief_res["brief"]
    return out
