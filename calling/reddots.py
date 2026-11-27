# -*- coding: utf-8 -*-
"""HOI4 红点硬门禁与后台急刹守护引擎（Red-Dot Gatekeeper）。

核心职责：
  1. 100% 纯只读内存巡检全部 18 类关键决策/资源/事件/决议红点（零引擎注入）：
     - 严控三项（STRICT_CODES，默认禁止普通 ack，仅支持 force=True + 必填理由最长 7 天豁免）：
       * PENDING_EVENT     待决可决策事件（自动避开崩档风险 unresolved_scope；1 按钮纯新闻降为提醒）
       * NO_FOCUS          当前无进行中国策
       * IDLE_RESEARCH     存在空闲科研槽
     - 决议增量与限时任务：
       * NEW_DECISION      新出现的本国可见决议（增量触发，避免常驻通用决议死锁）
       * ACTIVE_MISSION    正在倒计时的本国限时任务（days_mission_timeout）
     - 工业产能：
       * EMPTY_CONSTRUCTION 建造队列为空
       * IDLE_CIV           队列全部满 15 厂且有明显盈余可用民工（绝不误报消费品）
       * IDLE_MIL           存在空闲军工厂（>= 1）
       * IDLE_DOCK          存在空闲海军船坞（>= 1）
     - 资源溢流（分档阶梯复触发，避免满仓浪费）：
       * PP_CAP             政治点数 >= 500（阶梯 500/800/1200/1600）
       * ARMY_XP_CAP        陆军经验 >= 100（阶梯 100/250/400/480）
       * NAVY_XP_CAP        海军经验 >= 100（阶梯 100/250/400/480）
       * AIR_XP_CAP         空军经验 >= 100（阶梯 100/250/400/480）
     - 部队编制与扩军失衡：
       * ARMY_NO_GENERAL       存在有师但无将领的集团军（且将领池仍有空闲陆军将领）
       * UNASSIGNED_DIVS       存在未编入集团军的陆军师（>= 1）
       * SURPLUS_NO_TRAINING   无训练队列且步枪>=8000、可用人力>=50000
       * DEFICIT_OVER_TRAINING 训练队列排线>=3 且步枪库存<1000（或核心陆军装备亏空/装备率<0.85）
     - 海空与策略告警（复用既有模块）：
       * IDLE_AIR / IDLE_NAVY  空军联队 / 非预备舰队无任务（默认战时强拦、和平期仅提醒，支持开关）
       * FUEL_ZERO             燃油归零但海空仍在挂任务
       * OBSOLETE_LINE         产线存在高收益换代目标（换装回本 < 45 天）
  2. 三路联动闭环：
     - 闸门 A：hoi4_wait_for 跑表期命中阻塞红点立即强制停表（idler+0x6C1 = 1）；
     - 闸门 B：开表前统一硬拦截（hoi4_pause(false) / hoi4_pause_in_hours / hoi4_wait_for / turnengine.resume）；
     - 闸门 C：轻量后台纯只读急刹守护线程（仅在游戏运行态轻扫，同一签名不重复按暂停，绝不抢窗口焦点）。
  3. 防死锁出口：显式带理由签收（ack_red_dot），到期或数值跨档/新增对象时自动重新激活。
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import time

import session_state as _SS

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = _SS.state_path(_SS.REDDOTS_STATE_NAME)

# 严控五项：默认拒绝普通 ack，必须显式 force=True + 非空 reason，且豁免期 <= 7 游戏日
# CB_AVAILABLE / JUSTIFY_LOST 绝不放进 AUTO_SOLVABLE_CODES，必须由决策者亲自处置
STRICT_CODES = frozenset({
    "PENDING_EVENT",
    "NO_FOCUS",
    "IDLE_RESEARCH",
    "CB_AVAILABLE",
    "JUSTIFY_LOST",
})

# 2026-11-23 (反糊弄升级 / 复测 8 防自锁校准):
# 仅把 100% 确定可由工具 1 步清零的项放入 AUTO_SOLVABLE_CODES；
# IDLE_DOCK 虽已修好修理船坞账本与 housekeep 自动分船坞，但存在 0 船线/全满单线上限等
# 合法边界，保留普通可签收（需具体理由），杜绝任何不可解自锁！
AUTO_SOLVABLE_CODES = frozenset({
    "EMPTY_CONSTRUCTION",
    "IDLE_MIL",
    "UNASSIGNED_DIVS",
    "ARMY_NO_GENERAL",
    "OBSOLETE_LINE",
})

# 仅对“有明确工具可一步消解的资源/训练/工业红点”执行连续续签上限；
# 对 ACTIVE_MISSION / NEW_DECISION / FUEL_ZERO / IDLE_DOCK 等受外部周期或资源制约的项，
# 绝不触发 ack_streak_exceeded 导致整档死锁停表（2026-11-23 C-9 根因修复）。
STREAK_LIMITED_CODES = frozenset(AUTO_SOLVABLE_CODES) | frozenset({
    "PP_CAP",
    "ARMY_XP_CAP",
    "NAVY_XP_CAP",
    "AIR_XP_CAP",
    "SURPLUS_NO_TRAINING",
    "DEFICIT_OVER_TRAINING",
})

# 已知属于被动周期经济/账单计时器（玩家无法手动点掉）的限时任务关键字
_PASSIVE_MISSION_KEYWORDS = (
    "mefo_bills",
    "economy_of_conquest",
    "treaty_renew_mission",
)


def _is_passive_unsolvable_mission(d):
    """判断一个运行中的限时任务是否为不可手动完成的被动周期计时器（如德国 MEFO 账单）。

    2026-11-23 (C-9): 真机实测 GER_mefo_bills_mission 携带
    `is_good=True` 且 `available='has_political_power > 0 hidden_trigger = { always = no }'`，
    玩家字面上不可能主动完成它；若将其视为阻塞红点叠加 max_consecutive_acks=2，
    60 天后德国档必将永久停表死锁。此类任务一律降为 advisory（不阻塞、不耗 ack）。
    """
    if not isinstance(d, dict):
        return False
    key = str(d.get("key") or "").lower()
    if any(kw in key for kw in _PASSIVE_MISSION_KEYWORDS):
        return True
    avail = str(d.get("available") or "").lower()
    has_always_no = bool(re.search(r"\balways\s*=\s*no\b", avail))
    # available 明确含 always = no 且不是恶性危机（is_good != False）-> 被动倒计时器
    if has_always_no and d.get("is_good") is not False:
        return True
    return False


# 进程内缓存最近一次 NO_FOCUS 可选国策候选（消除 fast=True/False 切换导致 candidates 时有时无）
_LAST_FOCUS_CANDS = {}

# 低质量空话套话黑名单（拒绝把 ack_red_dot 当成验证码跳过）
_LOW_EFFORT_REASONS = frozenset({
    "ok", "skip", "later", "ignore", "none", "n/a", "na", "pass", "wait", "defer",
    "跳过", "稍后", "先放着", "暂不处理", "忽略", "无", "好的", "等会", "等一下",
})

# 默认可配置阈值（全部支持运行期 configure() 覆盖）
DEFAULT_CONFIG = {
    "enabled": True,
    "guard_enabled": os.environ.get("HOI4_RED_DOT_GUARD", "1") != "0",
    "guard_poll_seconds": 1.5,
    "peace_air_navy_block": os.environ.get("HOI4_RED_DOT_PEACE_AIR_NAVY", "0") == "1",
    "pp_tiers": (500, 800, 1200, 1600),
    # 2026-11-23 (复测 10 B-7): 100 XP 刚够 1 个陆军学说，稍微攒点准备改主力编制就反复卡死；
    # 校准首档为 150（阶梯 150/280/400/480），既强防囤满 500 浪费，又不逼模型复制废模板烧经验。
    "xp_tiers": (150, 280, 400, 480),
    "idle_mil_min": 1,
    "idle_dock_min": 1,
    "civ_full_line": 15,
    "surplus_no_train_rifles": 8000.0,
    "surplus_no_train_manpower": 50000,
    "deficit_over_train_lines": 3,
    "deficit_rifles_max": 1000.0,
    "deficit_equip_ratio_max": 0.85,
    "obsolete_payback_max_days": 45.0,
    "default_snooze_days": 30,
    "max_snooze_days": 120,
    "strict_max_snooze_days": 7,
    "max_acks_per_day": 4,
    "max_consecutive_acks": 2,
}

CONFIG = dict(DEFAULT_CONFIG)

_LOCK = threading.RLock()
# key = (pid, tag) -> {
#   "acks": {code: {reason, tier, signature, ack_date_raw, expire_date_raw, force, acked_at}},
#   "seen_decisions": set[str],
#   "seen_decisions_init": bool,
#   "seen_news_events": set[str],
#   "braked_sigs": dict[code, sig],
# }
_STATES = {}
_PERSIST_LOADED = False


def _clear_reddots_caches():
    global _PERSIST_LOADED
    _STATES.clear()
    _LAST_FOCUS_CANDS.clear()
    _PERSIST_LOADED = False


_SS.register_cache("reddots._STATES", _clear_reddots_caches)


def _state_key(g):
    pid = getattr(getattr(g, "p", None), "pid", None)
    try:
        tag = g.player_tag()
    except Exception:
        tag = None
    return (int(pid) if pid is not None else 0, str(tag or "?").upper())


def _load_persisted():
    global _PERSIST_LOADED
    if _PERSIST_LOADED:
        return
    _PERSIST_LOADED = True
    if not os.path.isfile(STATE_PATH):
        return
    try:
        raw = json.load(open(STATE_PATH, "r", encoding="utf-8"))
        if not isinstance(raw, dict):
            return
        for k_str, st in (raw.get("states") or {}).items():
            if not isinstance(st, dict) or ":" not in k_str:
                continue
            pid_s, tag = k_str.split(":", 1)
            try:
                key = (int(pid_s), tag)
            except ValueError:
                continue
            _STATES[key] = {
                "acks": dict(st.get("acks") or {}),
                "seen_decisions": set(st.get("seen_decisions") or ()),
                "seen_decisions_init": bool(st.get("seen_decisions_init", False)),
                "seen_news_events": set(st.get("seen_news_events") or ()),
                "braked_sigs": dict(st.get("braked_sigs") or {}),
                "ack_streaks": dict(st.get("ack_streaks") or {}),
                "daily_ack_log": dict(st.get("daily_ack_log") or {}),
                "active_justifying": set(st.get("active_justifying") or ()),
                "lost_justifying": set(st.get("lost_justifying") or ()),
                "last_date_raw": st.get("last_date_raw"),
            }
    except Exception:
        pass


def _save_persisted():
    try:
        payload = {"version": 1, "states": {}}
        for (pid, tag), st in _STATES.items():
            payload["states"]["%d:%s" % (pid, tag)] = {
                "acks": st.get("acks") or {},
                "seen_decisions": sorted(st.get("seen_decisions") or ()),
                "seen_decisions_init": bool(st.get("seen_decisions_init", False)),
                "seen_news_events": sorted(st.get("seen_news_events") or ()),
                "braked_sigs": st.get("braked_sigs") or {},
                "ack_streaks": st.get("ack_streaks") or {},
                "daily_ack_log": st.get("daily_ack_log") or {},
                "active_justifying": sorted(st.get("active_justifying") or ()),
                "lost_justifying": sorted(st.get("lost_justifying") or ()),
                "last_date_raw": st.get("last_date_raw"),
            }
        _SS.ensure_parent(STATE_PATH)
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, STATE_PATH)
    except Exception:
        pass


def _get_state(g):
    with _LOCK:
        _load_persisted()
        k = _state_key(g)
        st = _STATES.get(k)
        if st is None:
            st = {
                "acks": {},
                "seen_decisions": set(),
                "seen_decisions_init": False,
                "seen_news_events": set(),
                "braked_sigs": {},
                "ack_streaks": {},
                "daily_ack_log": {},
                "active_justifying": set(),
                "lost_justifying": set(),
                "last_date_raw": None,
            }
            _STATES[k] = st
        return st


def reset_state(g=None, persist=False):
    """清空内存状态（用于单测或强制重新评估全部红点）。"""
    global _PERSIST_LOADED
    with _LOCK:
        if g is None:
            _STATES.clear()
            _PERSIST_LOADED = True
        else:
            _STATES.pop(_state_key(g), None)
        if persist:
            _save_persisted()


def configure(**kwargs):
    """运行期微调门禁配置并返回当前生效配置。"""
    with _LOCK:
        for k, v in kwargs.items():
            if k in CONFIG and v is not None:
                if k in ("pp_tiers", "xp_tiers") and isinstance(v, (list, tuple)):
                    CONFIG[k] = tuple(int(x) for x in v)
                else:
                    CONFIG[k] = v
        return dict(CONFIG)


def _tier_for(value, tiers):
    """返回 value 跨过的最高阶梯阈值；未达到首档返回 None。"""
    if value is None:
        return None
    hit = None
    for t in tiers:
        if float(value) >= float(t):
            hit = int(t)
    return hit


def _date_info(g):
    date_text, date_raw = None, None
    try:
        d = g.date()
        if isinstance(d, dict):
            date_text = d.get("text")
            date_raw = d.get("raw")
    except Exception:
        pass
    if date_raw is None:
        try:
            date_raw = g.date_raw()
        except Exception:
            pass
    return date_text, date_raw


def _force_pause_safe(g):
    """零注入急刹：优先 session.pause，兜底直写 1 字节 idler+0x6C1 = 1。"""
    try:
        import session as _sess
        p = getattr(g, "p", None)
        if p is not None and sys.platform == "win32":
            idler = p.u64(p.base + _sess.G_IDLER)
            if idler:
                p.write(idler + _sess.IDLER_PAUSED, b"\x01")
                return bool(p.u8(idler + _sess.IDLER_PAUSED))
        ok, _ = _sess.pause(g, True)
        if ok:
            return True
    except Exception:
        pass
    try:
        import session as _sess
        ok, _ = _sess.pause(g, True)
        return bool(ok)
    except Exception:
        return None


def _is_paused_fast(g):
    try:
        import session as _sess
        p = getattr(g, "p", None)
        if p is not None and sys.platform == "win32":
            idler = p.u64(p.base + _sess.G_IDLER)
            if idler:
                return bool(p.u8(idler + _sess.IDLER_PAUSED))
        return bool(_sess.is_paused(g))
    except Exception:
        return None


def _is_at_war(g):
    try:
        import combat
        return bool(combat.enemy_tags(g))
    except Exception:
        return False


def _oi_ready(g, fast=False):
    if getattr(g, "_oi", None) is not None:
        return True
    if fast:
        return False
    try:
        import fastmem as _fm
        if getattr(_fm, "AVAILABLE", False):
            _ = g.oi
            return getattr(g, "_oi", None) is not None
    except Exception:
        pass
    return False


def _make_item(
    code,
    title,
    detail,
    suggested_actions,
    *,
    severity="block",
    tier=1,
    signature=None,
    housekeepable=False,
):
    is_strict = code in STRICT_CODES and severity != "advisory"
    eff_sev = "strict" if is_strict else severity
    return {
        "code": code,
        "severity": eff_sev,
        "tier": tier,
        "signature": str(signature if signature is not None else "%s:%s" % (code, tier)),
        "title": title,
        "detail": detail,
        "can_ack": eff_sev != "advisory",
        "force_ack_only": is_strict,
        "housekeepable": bool(housekeepable),
        "suggested_actions": list(suggested_actions),
    }


def _scan_raw(g, fast=False):
    """纯只读采集全部原始红点（不套 ack 过滤）。"""
    items = []
    try:
        tag = g.player_tag()
        c = g.player()
    except Exception as e:
        return {"ok": False, "error": "player_unreadable: %s" % e, "items": []}

    date_text, date_raw = _date_info(g)
    st = _get_state(g)
    at_war = _is_at_war(g)

    # ------------------------------------------------------------------ 1. 待决事件
    try:
        import events_win as _ew
        try:
            pend = _ew.pending(g, only_player=True) or []
        except TypeError:
            pend = _ew.pending(g) or []
        decisive = []
        advisory_news = []
        unresolved_list = []
        for row in pend:
            eid = row.get("event_id")
            if not eid:
                continue
            inf = _ew.info(eid, lang="zh") or {}
            if inf.get("ok"):
                try:
                    opts = _ew.applicable_options(eid, tag, lang="zh", g=g)
                except TypeError:
                    opts = _ew.applicable_options(eid, tag, lang="zh")
            else:
                opts = []
            kind = inf.get("kind") or "country_event"
            unresolved = bool(opts) and all(o.get("applicable") is None for o in opts)
            entry = {
                "event_id": eid,
                "slot": row.get("slot"),
                "kind": kind,
                "title": inf.get("title") or eid,
                "options": [
                    {
                        "index": o["index"],
                        "name": (o.get("name") or "")[:60],
                        "applicable": o.get("applicable"),
                    }
                    for o in opts[:6]
                ],
            }
            if unresolved:
                unresolved_list.append(entry)
            elif kind == "news_event" and len(opts) <= 1:
                advisory_news.append(entry)
            else:
                decisive.append(entry)

        if decisive:
            eids = sorted(x["event_id"] for x in decisive)
            first = decisive[0]
            items.append(_make_item(
                "PENDING_EVENT",
                "待决事件弹窗 (%d): %s" % (len(decisive), first["title"]),
                {"count": len(decisive), "events": decisive[:5]},
                [
                    "hoi4_event_info(event_id='%s') 查看详情与选项效果" % first["event_id"],
                    "hoi4_select_event_option(event_id='%s', index=<选项序号>, write=True) 做出选择"
                    % first["event_id"],
                ],
                severity="strict",
                tier=len(decisive),
                signature="PENDING_EVENT:" + ",".join(eids),
            ))
        if advisory_news:
            fresh_news = [x for x in advisory_news if x["event_id"] not in st["seen_news_events"]]
            if fresh_news:
                for x in fresh_news:
                    st["seen_news_events"].add(x["event_id"])
                items.append(_make_item(
                    "NEWS_EVENT",
                    "单按钮新闻事件 (%d): %s（可自然结算，不阻塞开表）"
                    % (len(fresh_news), fresh_news[0]["title"]),
                    {"count": len(fresh_news), "events": fresh_news[:3]},
                    ["无需阻塞，13 天自动结算；也可调用 hoi4_select_event_option(write=True) 立即关闭"],
                    severity="advisory",
                    tier=len(fresh_news),
                    signature="NEWS_EVENT:" + ",".join(sorted(x["event_id"] for x in fresh_news)),
                ))
        if unresolved_list:
            items.append(_make_item(
                "EVENT_UNRESOLVED_SCOPE",
                "不可安全写回事件 (%d): %s（须等待 13 天自然超时，不阻塞）"
                % (len(unresolved_list), unresolved_list[0]["event_id"]),
                {"count": len(unresolved_list), "events": unresolved_list[:3]},
                ["切勿写入 unresolved_scope 事件，放行由引擎 13 天自动结算"],
                severity="advisory",
                tier=len(unresolved_list),
                signature="EVENT_UNRESOLVED:" + ",".join(sorted(x["event_id"] for x in unresolved_list)),
            ))
    except Exception:
        pass

    # ------------------------------------------------------------------ 2. 新决议 & 限时任务
    try:
        vis = g.decisions(tag, which="visible", rich=True) or []
        mine_vis = [d for d in vis if d.get("mine", True) and d.get("key")]
        cur_keys = {d["key"] for d in mine_vis}
        if not st["seen_decisions_init"]:
            st["seen_decisions"].update(cur_keys)
            st["seen_decisions_init"] = True
        else:
            fresh_keys = sorted(cur_keys - st["seen_decisions"])
            if fresh_keys:
                by_k = {d["key"]: d for d in mine_vis}
                fresh_rows = [
                    {
                        "key": k,
                        "title": by_k[k].get("title") or k,
                        "category": by_k[k].get("category"),
                        "cost_pp": by_k[k].get("cost_pp"),
                        "days_mission_timeout": by_k[k].get("days_mission_timeout"),
                    }
                    for k in fresh_keys[:8]
                ]
                items.append(_make_item(
                    "NEW_DECISION",
                    "新出现本国可见决议 (%d): %s"
                    % (len(fresh_keys), ", ".join(r["title"] for r in fresh_rows[:3])),
                    {"count": len(fresh_keys), "decisions": fresh_rows},
                    [
                        "hoi4_select_decision(key='%s') 执行决议" % fresh_keys[0],
                        "如暂不需要，调用 hoi4_ack_red_dot(code='NEW_DECISION', reason='...') 签收放行",
                    ],
                    severity="block",
                    tier=len(fresh_keys),
                    signature="NEW_DECISION:" + ",".join(fresh_keys),
                ))
    except Exception:
        pass

    try:
        act = g.decisions(tag, which="active", rich=True) or []
        actionable_missions = []
        passive_missions = []
        for d in act:
            if not (d.get("mine", True) and d.get("key") and d.get("days_mission_timeout")):
                continue
            row_m = {
                "key": d["key"],
                "title": d.get("title") or d["key"],
                "days_mission_timeout": d.get("days_mission_timeout"),
                "cost_pp": d.get("cost_pp"),
                "is_good": d.get("is_good"),
            }
            if _is_passive_unsolvable_mission(d):
                passive_missions.append(row_m)
            else:
                actionable_missions.append(row_m)
        if actionable_missions:
            mkeys = sorted(m["key"] for m in actionable_missions)
            max_to = max(int(m.get("days_mission_timeout") or 30) for m in actionable_missions)
            items.append(_make_item(
                "ACTIVE_MISSION",
                "存在倒计时限时任务 (%d): %s"
                % (len(actionable_missions), ", ".join(m["title"] for m in actionable_missions[:3])),
                {
                    "count": len(actionable_missions),
                    "max_timeout_days": max_to,
                    "missions": actionable_missions[:6],
                },
                [
                    "hoi4_decisions(which='active') 核对限时任务要求并提前处理",
                    "如已安排对策等待倒计时，调用 hoi4_ack_red_dot(code='ACTIVE_MISSION', reason='...') 签收（默认覆盖完整周期 %d 天）"
                    % min(365, max(int(CONFIG["default_snooze_days"]), max_to)),
                ],
                severity="block",
                tier=len(actionable_missions),
                signature="ACTIVE_MISSION:" + ",".join(mkeys),
            ))
        if passive_missions:
            pkeys = sorted(m["key"] for m in passive_missions)
            items.append(_make_item(
                "PASSIVE_MISSION",
                "常驻周期计时器 (%d): %s（不可手动完成，仅提醒不阻塞）"
                % (len(passive_missions), ", ".join(m["title"] for m in passive_missions[:3])),
                {"count": len(passive_missions), "missions": passive_missions[:6]},
                ["此类任务为周期自动结算机制（如 MEFO 账单），无需 ack，也不会阻塞推进"],
                severity="advisory",
                tier=len(passive_missions),
                signature="PASSIVE_MISSION:" + ",".join(pkeys),
            ))
    except Exception:
        pass

    # ------------------------------------------------------------------ 3. 空国策
    try:
        cur_f = g.current_focus()
        if not cur_f:
            cands = []
            cands_source = "none"
            has_any_selectable = True
            fk_key = _state_key(g)
            # 优先读 Game._sel_cache，其次读非 fast 实查，最后回退本进程最近 60s 缓存防抖动
            cached_sel = getattr(g, "_sel_cache", None)
            if cached_sel and isinstance(cached_sel, tuple) and len(cached_sel) >= 2 and isinstance(cached_sel[1], dict):
                sel_data = cached_sel[1]
                cands = list(sel_data.get("focuses") or [])[:8]
                cands_source = "game_sel_cache"
                if sel_data.get("count") == 0:
                    has_any_selectable = False
                elif cands:
                    _LAST_FOCUS_CANDS[fk_key] = (time.time(), list(cands))
            elif not fast and hasattr(g, "selectable_focuses"):
                try:
                    sel_data = g.selectable_focuses(tag) or {}
                    cands = list(sel_data.get("focuses") or [])[:8]
                    cands_source = "live_selectable_focuses"
                    if sel_data.get("count") == 0:
                        has_any_selectable = False
                    elif cands:
                        _LAST_FOCUS_CANDS[fk_key] = (time.time(), list(cands))
                except Exception:
                    pass
            if not cands and fk_key in _LAST_FOCUS_CANDS:
                ts_f, prev_cands = _LAST_FOCUS_CANDS[fk_key]
                if (time.time() - ts_f) <= 60.0:
                    cands = list(prev_cands)
                    cands_source = "recent_cache"
            if has_any_selectable:
                items.append(_make_item(
                    "NO_FOCUS",
                    "当前无进行中国策（白白浪费每日国策进度）",
                    {"candidates": cands, "candidates_source": cands_source},
                    [
                        "hoi4_selectable_focuses() 查看可选国策",
                        "hoi4_set_focus(key='<国策key>') 启动国策",
                    ],
                    severity="strict",
                    tier=1,
                    signature="NO_FOCUS:1",
                ))
    except Exception:
        pass

    # ------------------------------------------------------------------ 4. 空科研槽
    try:
        slots = g.research_slots() or []
        free_slots = [s.get("slot") for s in slots if not s.get("slot_busy")]
        if free_slots:
            has_avail = True
            cands = []
            if not fast and hasattr(g, "available_research"):
                try:
                    ar = g.available_research(tag) or {}
                    recs = ar.get("available") or ar.get("recommended") or []
                    if isinstance(recs, list):
                        if len(recs) == 0 and ar.get("count") == 0:
                            has_avail = False
                        cands = [
                            (x.get("id") or x.get("key") or x.get("name")) if isinstance(x, dict) else str(x)
                            for x in recs[:8]
                        ]
                except Exception:
                    pass
            if has_avail:
                items.append(_make_item(
                    "IDLE_RESEARCH",
                    "存在 %d/%d 个空闲科研槽（槽位 %s）" % (len(free_slots), len(slots), free_slots),
                    {"free_slots": free_slots, "total_slots": len(slots), "candidates": cands},
                    [
                        "hoi4_research_available() 查看可研科技",
                        "hoi4_start_research(technology='<科技id>') 或 hoi4_housekeep() 自动补槽",
                    ],
                    severity="strict",
                    tier=len(free_slots),
                    signature="IDLE_RESEARCH:%d" % len(free_slots),
                    housekeepable=True,
                ))
    except Exception:
        pass

    # ------------------------------------------------------------------ 5. 工业产能（民工/军工/船坞）
    lines = []
    try:
        lines = g.production_lines(tag, include_obsolete=False) or []
    except TypeError:
        try:
            lines = g.production_lines() or []
        except Exception:
            lines = []
    except Exception:
        lines = []

    try:
        bld = g.construction_queue(tag) or []
    except TypeError:
        try:
            bld = g.construction_queue() or []
        except Exception:
            bld = []
    except Exception:
        bld = []

    civ_total = int(getattr(c, "civilian_factories", 0) or 0)
    mil_total = int(getattr(c, "military_factories", 0) or 0)
    dock_total = int(getattr(c, "naval_dockyards", 0) or 0)

    if civ_total > 0:
        if len(bld) == 0:
            items.append(_make_item(
                "EMPTY_CONSTRUCTION",
                "建造队列为空（总民工 %d 座无施工项目）" % civ_total,
                {"civilian_factories": civ_total, "queue_len": 0},
                [
                    "hoi4_add_construction(state=<州id>, building='industrial_complex'|'arms_factory')",
                    "或调用 hoi4_housekeep() 自动补充建造队列",
                ],
                severity="block",
                tier=1,
                signature="EMPTY_CONSTRUCTION:0",
                housekeepable=True,
            ))
        else:
            full_n = int(CONFIG["civ_full_line"])
            facs = [int(x.get("factories") or 0) for x in bld]
            assigned = sum(facs)
            # 精确防误报：只要任意一项分配在 (0, 15) 之间，说明引擎已把全部可用民工榨干（其余均为消费品/贸易）
            all_full = all(f >= full_n for f in facs)
            rem = civ_total - assigned
            if all_full and rem >= max(8, civ_total // 3):
                items.append(_make_item(
                    "IDLE_CIV",
                    "建造队列仅 %d 项且全满 %d 厂（已派 %d/%d 民工，可能有可用民工闲置）"
                    % (len(bld), full_n, assigned, civ_total),
                    {"queue_len": len(bld), "assigned_civ": assigned, "civilian_factories": civ_total},
                    ["hoi4_add_construction(...) 追加建造队列，确保可用民工不闲置"],
                    severity="block",
                    tier=len(bld),
                    signature="IDLE_CIV:%d:%d" % (len(bld), assigned),
                    housekeepable=True,
                ))

    # 2026-11-23 (复测 6 P1-1): 军工/船坞优先采用引擎权威口径 g.factory_allocation(tag)，
    # 避免把占领区非核心合规折算、改装线（CShipRefitProductionLine）或修理船坞误报为 IDLE_DOCK。
    _alloc = None
    if hasattr(g, "factory_allocation"):
        try:
            _alloc = g.factory_allocation(tag)
        except Exception:
            _alloc = None
    if isinstance(_alloc, dict) and _alloc.get("source") == "engine_production_status":
        mil_total = int(_alloc["military_available"])
        mil_used = int(_alloc["military_used"])
        mil_idle = int(_alloc["military_idle"])
        dock_total = int(_alloc["naval_available"])
        dock_used = int(_alloc["naval_used"])
        dock_idle = int(_alloc["naval_idle"])
    else:
        mil_used = sum(
            int(l.get("factories") or 0)
            for l in lines
            if l.get("class") == "CMilitaryProductionLine"
        )
        mil_idle = max(0, mil_total - mil_used)
        dock_used = sum(
            int(l.get("factories") or 0)
            for l in lines
            if l.get("class") in ("CNavalProductionLine", "CShipRefitProductionLine")
        )
        dock_idle = max(0, dock_total - dock_used)
    if mil_idle >= int(CONFIG["idle_mil_min"]):
        items.append(_make_item(
            "IDLE_MIL",
            "存在 %d/%d 座空闲军工厂未分配产线" % (mil_idle, mil_total),
            {"military_idle": mil_idle, "military_total": mil_total, "military_used": mil_used},
            [
                "hoi4_set_production_amount(line=<行号>, amount=<新厂数>) 分配空闲军工",
                "或调用 hoi4_housekeep() 按库存缺口自动补齐",
            ],
            severity="block",
            tier=mil_idle,
            signature="IDLE_MIL:%d" % mil_idle,
            housekeepable=True,
        ))

    if dock_total > 0 and dock_idle >= int(CONFIG["idle_dock_min"]):
        items.append(_make_item(
            "IDLE_DOCK",
            "存在 %d/%d 座空闲海军船坞未分配造船线" % (dock_idle, dock_total),
            {
                "dockyard_idle": dock_idle,
                "dockyard_total": dock_total,
                "dockyard_used": dock_used,
                "repair_dockyards": int((_alloc or {}).get("naval_repair_dockyards") or 0),
                "refit_dockyards": int((_alloc or {}).get("naval_refit_used") or 0),
            },
            [
                "hoi4_production() 查看海军造船线行号",
                "hoi4_set_production_amount(line=<船坞行号>, amount=<新船坞数>) 分配空闲船坞",
            ],
            severity="block",
            tier=dock_idle,
            signature="IDLE_DOCK:%d" % dock_idle,
        ))

    # ------------------------------------------------------------------ 6. PP & 海陆空 XP
    try:
        pp = float(getattr(c, "political_power", 0.0) or 0.0)
        pp_tier = _tier_for(pp, CONFIG["pp_tiers"])
        if pp_tier is not None:
            items.append(_make_item(
                "PP_CAP",
                "政治点数达 %.0f（触发阈值 >= %d，避免堆积浪费）" % (pp, pp_tier),
                {"political_power": round(pp, 1), "tier": pp_tier},
                [
                    "hoi4_available_ideas() 检查是否应切经济法案/征兵法/贸易法",
                    "hoi4_advisors() 雇佣内阁顾问或总参谋部",
                    "hoi4_decisions() 查看高价值决议",
                ],
                severity="block",
                tier=pp_tier,
                signature="PP_CAP:%d" % pp_tier,
            ))
    except Exception:
        pass

    try:
        xp = getattr(c, "experience", None)
        if not isinstance(xp, dict):
            s = c.summary() if hasattr(c, "summary") else {}
            xp = s.get("experience") if isinstance(s, dict) else {}
        xp = xp or {}
        for svc, code, label, spend in (
            ("army", "ARMY_XP_CAP", "陆军经验", "点学说 / 改师编制 / 设计坦克"),
            ("navy", "NAVY_XP_CAP", "海军经验", "点海军学说 / 改舰船设计"),
            ("air", "AIR_XP_CAP", "空军经验", "点空军学说 / 改战机设计"),
        ):
            val = xp.get(svc)
            xt = _tier_for(val, CONFIG["xp_tiers"])
            if xt is not None:
                if svc == "army":
                    acts = [
                        "推荐 1 步高收益方案：upgrade_template_preset(preset='inf_9_3', auto_rebalance=true, dry_run=true) 一键把主力步兵升为 9步3炮+工兵+炮支并自动从超储线挪厂！",
                        "或用 unlock_doctrine(doctrine='<陆军学说>') 解锁学说（如 mobile_warfare / superior_firepower / grand_battleplan / mass_assault）",
                    ]
                elif svc == "navy":
                    acts = [
                        "直接调用 unlock_doctrine(doctrine='<海军学说>') 消耗海军经验（如 fleet_in_being / base_strike / convoy_raiding）",
                        "如确需蓄力设计，调用 ack_red_dot(code='%s', reason='说明具体目标', snooze_days=30)"
                        % code,
                    ]
                else:
                    acts = [
                        "直接调用 unlock_doctrine(doctrine='<空军学说>') 消耗空军经验（如 battlefield_support / operational_integrity / strategic_destruction）",
                        "如确需蓄力设计，调用 ack_red_dot(code='%s', reason='说明具体目标', snooze_days=30)"
                        % code,
                    ]
                items.append(_make_item(
                    code,
                    "%s达 %.1f（>= %d，上限 500，建议及时消耗）" % (label, float(val), xt),
                    {"service": svc, "xp": round(float(val), 1), "tier": xt},
                    acts,
                    severity="block",
                    tier=xt,
                    signature="%s:%d" % (code, xt),
                ))
    except Exception:
        pass

    # ------------------------------------------------------------------ 7. 未编组师 & 无将领集团军
    try:
        unassigned = g.unassigned_divisions(tag) or []
        if len(unassigned) >= 1:
            items.append(_make_item(
                "UNASSIGNED_DIVS",
                "存在 %d 个陆军师未编入集团军" % len(unassigned),
                {"count": len(unassigned)},
                [
                    "hoi4_assign_divisions() 或 hoi4_housekeep() 将空闲师编入集团军",
                ],
                severity="block",
                tier=len(unassigned),
                signature="UNASSIGNED_DIVS:%d" % len(unassigned),
                housekeepable=True,
            ))
    except Exception:
        pass

    try:
        ogs_rows = []
        if sys.platform == "win32" and getattr(g, "p", None) is not None and hasattr(g.p, "u64"):
            try:
                import armyops_win as _aw
                p = g.p
                raw_ogs = _aw.armies(g, c) or []
                for og in raw_ogs:
                    try:
                        is_grp = bool(p.u8(og + _aw.OG_IS_GROUP))
                        if is_grp:
                            continue  # 仅针对带师的一线集团军检查将领，避免把空元帅集团军群混报
                        ndivs = int(p.i32(og + _aw.OG_DIVISIONS + 0x0C) or 0)
                        lp = int(p.u64(og + _aw.OG_LEADER) or 0)
                        ogs_rows.append({
                            "ptr": hex(og),
                            "divisions": ndivs,
                            "leader_ptr": hex(lp) if lp else 0,
                            "is_army_group": False,
                        })
                    except Exception:
                        continue
            except Exception:
                ogs_rows = []
        if not ogs_rows and hasattr(g, "order_groups") and _oi_ready(g, fast=fast):
            ogs_rows = g.order_groups(tag) or []

        if ogs_rows:
            headless = [
                {
                    "ptr": og.get("ptr"),
                    "divisions": int(og.get("divisions") or 0),
                    "is_army_group": bool(og.get("is_army_group")),
                }
                for og in ogs_rows
                if int(og.get("divisions") or 0) > 0
                and not og.get("leader")
                and not og.get("leader_ptr")
                and not og.get("general")
            ]
            if headless:
                used_ptrs = set()
                for og in ogs_rows:
                    lp = og.get("leader_ptr") or og.get("leader")
                    if lp:
                        used_ptrs.add(str(lp).lower())
                free_gens = []
                try:
                    ag = (g.available_generals(tag) or {}).get("generals") or []
                    for r in ag:
                        # 2026-11-23: 普通集团军只能指派 corps_commander（军长），
                        # 不能指派 field_marshal（元帅只管集团军群）或 admiral（海军上将）。
                        rk = r.get("rank")
                        if rk is not None and rk != "corps_commander":
                            continue
                        lp = str(r.get("leader_ptr") or "").lower()
                        if lp and lp not in used_ptrs:
                            free_gens.append({
                                "name": r.get("name") or r.get("key"),
                                "leader_ptr": r.get("leader_ptr"),
                                "rank": r.get("rank"),
                                "attack": r.get("attack"),
                                "defense": r.get("defense"),
                            })
                except Exception:
                    free_gens = []
                if free_gens:
                    items.append(_make_item(
                        "ARMY_NO_GENERAL",
                        "存在 %d 支在编集团军未指派将领（有 %d 名空闲陆军将领可用）"
                        % (len(headless), len(free_gens)),
                        {
                            "headless_armies": headless[:6],
                            "free_generals_count": len(free_gens),
                            "top_free_generals": free_gens[:5],
                        },
                        [
                            "hoi4_assign_commander(...) 为无将领集团军任命指挥官",
                            "或调用 hoi4_housekeep() 自动补齐集团军将领",
                        ],
                        severity="block",
                        tier=len(headless),
                        signature="ARMY_NO_GENERAL:%d:%s"
                        % (len(headless), ",".join(sorted(str(x["ptr"]) for x in headless))),
                        housekeepable=True,
                    ))
                else:
                    items.append(_make_item(
                        "ARMY_NO_GENERAL_POOL_EMPTY",
                        "存在 %d 支无将领集团军，但将领池已空（不阻塞开表）" % len(headless),
                        {"headless_armies": headless[:6], "free_generals_count": 0},
                        ["可花指挥点招募新将领，或合并零散集团军"],
                        severity="advisory",
                        tier=len(headless),
                        signature="ARMY_NO_GENERAL_EMPTY:%d" % len(headless),
                    ))
    except Exception:
        pass

    # ------------------------------------------------------------------ 8. 装备充足未练兵 vs 装备不足仍狂开训练
    stock_totals = {}
    rifles_total = None
    try:
        st_res = g.equipment_stockpile(tag) or {}
        stock_totals = st_res.get("totals_by_equipment") or {}
        rifles_total = sum(
            float(v or 0.0)
            for k, v in stock_totals.items()
            if "infantry_equipment" in str(k).lower()
        )
    except Exception:
        pass

    convs = []
    total_train_lines = 0
    try:
        convs = g.conveyors(tag) or []
        total_train_lines = sum(max(1, int(cv.get("lines") or 1)) for cv in convs)
    except Exception:
        pass

    manpower = None
    try:
        mp_raw = getattr(c, "manpower", None)
        if callable(mp_raw):
            mp_raw = mp_raw()
        if isinstance(mp_raw, (int, float)):
            manpower = float(mp_raw)
        elif hasattr(c, "summary"):
            mp_s = (c.summary() or {}).get("manpower")
            if isinstance(mp_s, (int, float)):
                manpower = float(mp_s)
    except Exception:
        manpower = None

    if (
        rifles_total is not None
        and total_train_lines == 0
        and rifles_total >= float(CONFIG["surplus_no_train_rifles"])
        and (manpower is None or float(manpower) >= float(CONFIG["surplus_no_train_manpower"]))
    ):
        tier = int(rifles_total // 10000) * 10000 or int(CONFIG["surplus_no_train_rifles"])
        items.append(_make_item(
            "SURPLUS_NO_TRAINING",
            "仓库步枪盈余 %.0f（人力 %s）但当前训练队列为空"
            % (rifles_total, ("%dk" % int(manpower / 1000)) if manpower is not None else "?"),
            {
                "rifles_stockpile": round(rifles_total, 1),
                "manpower": int(manpower) if manpower is not None else None,
                "conveyors": len(convs),
                "est_trainable_inf_divs": int(rifles_total // 1000),
            },
            [
                "hoi4_train_divisions(template='<师模板名>', lines=<线数>) 启动新师训练",
                "如刻意囤枪补前线或等待换装模板，调用 hoi4_ack_red_dot(code='SURPLUS_NO_TRAINING', reason='...')",
            ],
            severity="block",
            tier=tier,
            signature="SURPLUS_NO_TRAINING:%d" % tier,
        ))

    if total_train_lines >= int(CONFIG["deficit_over_train_lines"]) and rifles_total is not None:
        low_rifles = rifles_total < float(CONFIG["deficit_rifles_max"])
        zero_core = [
            k for k in ("artillery_equipment", "support_equipment")
            if not any(
                k in str(ek).lower() and float(ev or 0.0) >= 100.0
                for ek, ev in stock_totals.items()
            )
        ]
        # 检查在编部队平均装备率（纯只读，可选）
        avg_eq_ratio = None
        if not low_rifles and hasattr(g, "divisions"):
            try:
                divs = g.divisions(tag) or []
                ratios = [
                    float(d.get("equipment_ratio"))
                    for d in divs
                    if d.get("equipment_ratio") is not None
                ]
                if ratios:
                    avg_eq_ratio = sum(ratios) / len(ratios)
            except Exception:
                pass
        if low_rifles or (
            avg_eq_ratio is not None
            and avg_eq_ratio < float(CONFIG["deficit_equip_ratio_max"])
            and zero_core
        ):
            items.append(_make_item(
                "DEFICIT_OVER_TRAINING",
                "装备库存吃紧（步枪 %.0f）仍开启 %d 条训练队列，会抽干前线补充"
                % (rifles_total, total_train_lines),
                {
                    "training_lines": total_train_lines,
                    "conveyors": len(convs),
                    "rifles_stockpile": round(rifles_total, 1),
                    "avg_equipment_ratio": round(avg_eq_ratio, 3) if avg_eq_ratio is not None else None,
                    "scarce_core_types": zero_core,
                },
                [
                    "调用 cancel_training(lines=<削减线数>) 缩减多余训练队列，或提高步枪/火炮军工配额",
                    "如确需抢训应急部队，调用 ack_red_dot(code='DEFICIT_OVER_TRAINING', reason='...')",
                ],
                severity="block",
                tier=total_train_lines,
                signature="DEFICIT_OVER_TRAINING:%d:%d" % (total_train_lines, int(rifles_total < 200)),
            ))

    # ------------------------------------------------------------------ 9. 海空未指派 & 燃油枯竭 & 高收益换装
    fuel = None
    fuel_critical = False
    try:
        import equipmeta as _em_fuel
        fuel = getattr(c, "fuel", None)
        if fuel is None and hasattr(c, "summary"):
            fuel = (c.summary() or {}).get("fuel")
        fuel_critical = bool(_em_fuel.is_fuel_critical(fuel))
    except Exception:
        fuel_critical = False

    wings_cache = []
    fleets_cache = []
    if _oi_ready(g, fast=fast):
        try:
            # 对齐 calling/game.py: _air_snapshot() 已按玩家过滤，排除不接战略任务的 transport_plane，
            # 以 mission 位掩码为唯一准绳（和平期 mission_on 恒为 0）。
            wings_cache = g._air_snapshot() or []
            combat_wings = [
                w for w in wings_cache
                if (w.get("type") or "") != "transport_plane"
            ]
            idle_w = [
                w for w in combat_wings
                if not (w.get("mission") or w.get("missions"))
            ]
            if idle_w:
                # 燃油枯竭期允许停飞省油，不与 FUEL_ZERO 互相阻塞
                sev = (
                    "advisory" if fuel_critical
                    else ("block" if (at_war or CONFIG["peace_air_navy_block"]) else "advisory")
                )
                items.append(_make_item(
                    "IDLE_AIR",
                    "%s存在 %d/%d 支作战空军联队未指派任务%s" % (
                        "【战时】" if at_war else "【和平】",
                        len(idle_w),
                        len(combat_wings),
                        "（燃油枯竭期允许待命省油）" if fuel_critical else "",
                    ),
                    {
                        "idle_wings": len(idle_w),
                        "total_combat_wings": len(combat_wings),
                        "at_war": at_war,
                        "fuel_critical": fuel_critical,
                    },
                    ["hoi4_air_wings() / hoi4_air_mission(...) 为空闲联队指派制空或对地支援任务"],
                    severity=sev,
                    tier=len(idle_w),
                    signature="IDLE_AIR:%d:%d:%d" % (int(at_war), int(fuel_critical), len(idle_w)),
                ))
        except Exception:
            pass

        try:
            # 对齐 calling/game.py: 优先读 g._navy_snapshot()（含 is_reserve 与 queued_mission +0x4F0）
            fleets_cache = g._navy_snapshot() if hasattr(g, "_navy_snapshot") else []
            fleets_cache = fleets_cache or []
            tfs = [
                t for f in fleets_cache
                if not f.get("is_reserve")
                for t in (f.get("task_forces") or [])
                if not t.get("is_reserve") and int(t.get("mission") or 0) != 8
            ]
            idle_tf = [
                t for t in tfs
                if not (t.get("effective_mission") or t.get("mission") or t.get("queued_mission"))
            ]
            if idle_tf:
                # 燃油枯竭期允许停船省油，不与 FUEL_ZERO 互相阻塞
                sev = (
                    "advisory" if fuel_critical
                    else ("block" if (at_war or CONFIG["peace_air_navy_block"]) else "advisory")
                )
                items.append(_make_item(
                    "IDLE_NAVY",
                    "%s存在 %d/%d 支非预备舰队未指派任务%s" % (
                        "【战时】" if at_war else "【和平】",
                        len(idle_tf),
                        len(tfs),
                        "（燃油枯竭期允许停港省油）" if fuel_critical else "",
                    ),
                    {
                        "idle_task_forces": len(idle_tf),
                        "total_task_forces": len(tfs),
                        "at_war": at_war,
                        "fuel_critical": fuel_critical,
                        "handles": [tf.get("handle") for tf in idle_tf[:6]],
                    },
                    ["hoi4_fleets() / hoi4_naval_mission(...) 为舰队指派护航/巡逻/破交任务"],
                    severity=sev,
                    tier=len(idle_tf),
                    signature="IDLE_NAVY:%d:%d:%d" % (int(at_war), int(fuel_critical), len(idle_tf)),
                    housekeepable=not fuel_critical,
                ))
        except Exception:
            pass

    # FUEL_ZERO（复用 equipmeta.compute_fuel_zero_alert；绝不向模型输出不可执行的 <占位符>）
    try:
        import equipmeta as _em
        fz = _em.compute_fuel_zero_alert(fuel, wings_cache, fleets_cache)
        if fz:
            fz_txt = str(fz)
            if fz_txt.startswith("FUEL_ZERO:"):
                fz_txt = fz_txt[len("FUEL_ZERO:"):].strip()
            exporters = []
            try:
                import trade_win as _tw
                exporters = _tw.suggest_exporters(g, resource="oil", importer_tag=tag, limit=3) or []
            except Exception:
                exporters = []
            if exporters:
                alt = ("（备选卖方: %s）" % ", ".join(exporters[1:])) if len(exporters) > 1 else ""
                oil_act = (
                    "hoi4_trade(partner='%s', resource='oil', amount=8) 紧急进口石油%s，或暂停非必要海空任务"
                    % (exporters[0], alt)
                )
            else:
                oil_act = (
                    "先调用 hoi4_trade_routes() 查看非敌对石油卖方，再用该国 3 字母 TAG 调用 hoi4_trade 进口石油，"
                    "或暂停非必要海空任务"
                )
            items.append(_make_item(
                "FUEL_ZERO",
                fz_txt,
                {
                    "fuel": round(float(fuel or 0.0), 1),
                    "alert": str(fz),
                    "suggested_oil_exporters": exporters,
                },
                [
                    oil_act,
                    "如无可用卖方/民工，调用 hoi4_ack_red_dot(code='FUEL_ZERO', reason='说明节油/炼油对策')",
                ],
                severity="block",
                tier=1,
                signature="FUEL_ZERO:1",
            ))
    except Exception:
        pass

    # ------------------------------------------------------------------ 10. 宣战理由就绪 & 正当化丢失（严控）
    try:
        import diplomacy as _dip
        enemies_now = set()
        try:
            import combat as _cb_wg
            enemies_now = {str(x).upper() for x in (_cb_wg.enemy_tags(g, tag) or ())}
        except Exception:
            enemies_now = set()

        wg_rows, wg_ok = _dip.country_wargoals_fast(g, tag=tag, valid_only=True)
        cb_rows = []
        if wg_ok:
            for r in (wg_rows or []):
                if r.get("target_exists") is False or r.get("valid") is False:
                    continue
                tgt_t = str(r.get("target_tag") or "").upper()
                if not tgt_t or tgt_t == str(tag).upper() or tgt_t in enemies_now:
                    continue
                cb_rows.append({
                    "target": tgt_t,
                    "wargoal": r.get("wargoal") or "wargoal",
                    "ptr": r.get("ptr"),
                })
        cb_rows.sort(key=lambda x: (x["target"], str(x["wargoal"])))
        cb_targets = {x["target"] for x in cb_rows}

        if cb_rows:
            cb_labels = ["%s(%s)" % (x["target"], x["wargoal"]) for x in cb_rows[:5]]
            first_tgt = cb_rows[0]["target"]
            items.append(_make_item(
                "CB_AVAILABLE",
                "有未使用的宣战理由（会过期）(%d): %s" % (len(cb_rows), ", ".join(cb_labels)),
                {"count": len(cb_rows), "wargoals": cb_rows[:8]},
                [
                    "hoi4_war_readiness(target='%s') 核验战备后再决定是否 hoi4_declare_war(target='%s')"
                    % (first_tgt, first_tgt),
                    "如暂不宣战，必须显式调用 hoi4_ack_red_dot(code='CB_AVAILABLE', force=True, reason='说明暂缓宣战理由')",
                ],
                severity="strict",
                tier=len(cb_rows),
                signature="CB_AVAILABLE:" + ",".join("%s:%s" % (x["target"], x["wargoal"]) for x in cb_rows),
            ))

        # 追踪正在正当化 -> 消失但未生成 CB（JUSTIFY_LOST）
        prev_just = st.setdefault("active_justifying", set())
        lost_just = st.setdefault("lost_justifying", set())
        prev_date = st.get("last_date_raw")
        date_jumped_back = (
            prev_date is not None and date_raw is not None and int(date_raw) < int(prev_date) - 24
        )
        if date_jumped_back:
            prev_just.clear()
            lost_just.clear()
        if date_raw is not None:
            st["last_date_raw"] = int(date_raw)

        just_rows = _dip.justifying_fast(g, tag=tag) or []
        cur_just = {str(r.get("target") or "").upper() for r in just_rows if r.get("target")}
        # 重新开造、已有 CB 或已开战的目标从 lost_just 移出
        lost_just.difference_update(cur_just | cb_targets | enemies_now)
        if prev_just:
            disappeared = prev_just - cur_just
            for tgt_d in disappeared:
                if tgt_d not in cb_targets and tgt_d not in enemies_now:
                    lost_just.add(tgt_d)
        prev_just.clear()
        prev_just.update(cur_just)

        if lost_just:
            lost_sorted = sorted(lost_just)
            items.append(_make_item(
                "JUSTIFY_LOST",
                "正当化结束但没有产生宣战理由 (%d): %s" % (len(lost_sorted), ", ".join(lost_sorted[:5])),
                {"targets": lost_sorted},
                [
                    "hoi4_wargoals() 与 hoi4_justifying() 核查宣战理由是否被取消或已过期",
                    "确认后调用 hoi4_ack_red_dot(code='JUSTIFY_LOST', force=True, reason='已确认原因与后续对策')",
                ],
                severity="strict",
                tier=len(lost_sorted),
                signature="JUSTIFY_LOST:" + ",".join(lost_sorted),
            ))
    except Exception:
        pass

    # 高收益 OBSOLETE_LINE（换装回本 < 45 天，复用 equipmeta.analyze_production_lines）
    try:
        if lines and hasattr(g, "_active_variant_names"):
            import equipmeta as _em
            avail_vars = g._active_variant_names(tag)
            analyzed = _em.analyze_production_lines(lines, avail_vars) or {}
            max_pb = float(CONFIG["obsolete_payback_max_days"])
            obs = [
                v for v in analyzed.values()
                if isinstance(v, dict)
                and v.get("obsolete")
                and v.get("upgrade_to")
                and (v.get("payback_days") is None or float(v["payback_days"]) <= max_pb)
            ]
            obs.sort(key=lambda x: float(x.get("payback_days") or 0.0))
            if obs:
                sigs = sorted(
                    "%s:%s->%s" % (x.get("line"), x.get("equipment"), x.get("upgrade_to"))
                    for x in obs
                )
                items.append(_make_item(
                    "OBSOLETE_LINE",
                    "存在 %d 条高收益过时产线（回本 <= %d 天）：%s -> %s"
                    % (
                        len(obs),
                        int(max_pb),
                        obs[0].get("equipment"),
                        obs[0].get("upgrade_to"),
                    ),
                    {"count": len(obs), "lines": obs[:5]},
                    [
                        "hoi4_set_production_line_equipment(line=%s, equipment='%s', dry_run=False) 立即换装"
                        % (obs[0].get("line"), obs[0].get("upgrade_to")),
                        "或调用 hoi4_housekeep() 自动完成高收益换装",
                    ],
                    severity="block",
                    tier=len(obs),
                    signature="OBSOLETE_LINE:" + ",".join(sigs),
                    housekeepable=True,
                ))
    except Exception:
        pass

    return {
        "ok": True,
        "tag": tag,
        "date": date_text,
        "date_raw": date_raw,
        "at_war": at_war,
        "items": items,
    }


def _is_acked(item, acks, now_date_raw):
    """判定该条红点是否在有效签收冷却期内（跨档/签名变化/到期自动失效）。"""
    code = item["code"]
    rec = acks.get(code)
    if not rec:
        return False
    # 1. 到期检查（使用游戏内小时戳）
    exp = rec.get("expire_date_raw")
    if exp is not None and now_date_raw is not None and int(now_date_raw) >= int(exp):
        return False
    # 2. 数值阶梯升级 / 恶化检查
    old_tier = rec.get("tier")
    cur_tier = item.get("tier")
    if isinstance(old_tier, (int, float)) and isinstance(cur_tier, (int, float)):
        if cur_tier > old_tier:
            return False
    # 3. 离散对象集合类（事件/新决议/限时任务/无将领集团军/过时产线/宣战理由）：只要签名变化立即重触发
    if code in (
        "PENDING_EVENT",
        "NEW_DECISION",
        "ACTIVE_MISSION",
        "ARMY_NO_GENERAL",
        "OBSOLETE_LINE",
        "CB_AVAILABLE",
        "JUSTIFY_LOST",
    ):
        if rec.get("signature") != item.get("signature"):
            return False
    return True


def evaluate(g, fast=False):
    """完整评估当前红点，按未签收阻塞项 / 已签收项 / 提醒项分类返回。"""
    with _LOCK:
        raw = _scan_raw(g, fast=fast)
        if not raw.get("ok"):
            return {
                "ok": False,
                "error": raw.get("error"),
                "blocking": [],
                "acked": [],
                "advisory": [],
                "has_blocking": False,
            }
        st = _get_state(g)
        now_raw = raw.get("date_raw")
        acks = st["acks"]

        # 清理已自然消解红点的旧急刹去重标记与连续签收计数
        cur_codes = {it["code"] for it in raw["items"]}
        for old_c in list(st["braked_sigs"].keys()):
            if old_c not in cur_codes:
                st["braked_sigs"].pop(old_c, None)
        streaks = st.setdefault("ack_streaks", {})
        for old_c in list(streaks.keys()):
            if old_c not in cur_codes:
                streaks.pop(old_c, None)

        # 2026-11-23 (复测 10 B-7 根治): 若某 XP 溢流红点（ARMY/NAVY/AIR_XP_CAP）相比上次
        # 记录的经验值实际下降了 >= 10 点（说明玩家确实消耗了经验改编制/点学说/设计装备，
        # 并非空口糊弄），自动清零其连续 ack 计数，避免强迫把经验一口气烧到首档以下。
        last_xp_map = st.setdefault("last_xp_by_code", {})
        for it in raw["items"]:
            ic = it.get("code")
            if ic in ("ARMY_XP_CAP", "NAVY_XP_CAP", "AIR_XP_CAP"):
                cur_xp = (it.get("detail") or {}).get("xp")
                if isinstance(cur_xp, (int, float)):
                    prev_xp = last_xp_map.get(ic)
                    if isinstance(prev_xp, (int, float)) and cur_xp <= prev_xp - 10.0:
                        streaks.pop(ic, None)
                    last_xp_map[ic] = float(cur_xp)
        for old_xc in list(last_xp_map.keys()):
            if old_xc not in cur_codes:
                last_xp_map.pop(old_xc, None)

        # 2026-11-26 (P1-3 根治): DEFICIT_OVER_TRAINING 若训练线数实际下降，或步枪库存较上次
        # 记录显著回升 (>= 250) 或脱离危急 (<200 -> >=200)，说明玩家已采取实质改善而非空口糊弄，
        # 自动重置其连续 ack 计数，避免误判 ack_streak_exceeded 导致死锁。
        last_def = st.setdefault("last_deficit_state", {})
        def_item = next((it for it in raw["items"] if it.get("code") == "DEFICIT_OVER_TRAINING"), None)
        if def_item is not None:
            det = def_item.get("detail") or {}
            cur_tl = det.get("training_lines")
            cur_rf = det.get("rifles_stockpile")
            prev_tl = last_def.get("training_lines")
            prev_rf = last_def.get("rifles_stockpile")
            improved = False
            if isinstance(cur_tl, (int, float)) and isinstance(prev_tl, (int, float)) and cur_tl < prev_tl:
                improved = True
            if isinstance(cur_rf, (int, float)) and isinstance(prev_rf, (int, float)):
                if cur_rf >= prev_rf + 250.0 or (prev_rf < 200.0 and cur_rf >= 200.0):
                    improved = True
            if improved:
                streaks.pop("DEFICIT_OVER_TRAINING", None)
            if isinstance(cur_tl, (int, float)):
                last_def["training_lines"] = int(cur_tl)
            if isinstance(cur_rf, (int, float)):
                last_def["rifles_stockpile"] = float(cur_rf)
        else:
            last_def.clear()

        blocking, acked, advisory = [], [], []
        for it in raw["items"]:
            if it["severity"] == "advisory":
                advisory.append(it)
            elif _is_acked(it, acks, now_raw):
                rec = acks[it["code"]]
                it_copy = dict(it)
                it_copy["ack"] = {
                    "reason": rec.get("reason"),
                    "snooze_days": rec.get("snooze_days"),
                    "expire_date_raw": rec.get("expire_date_raw"),
                }
                acked.append(it_copy)
            else:
                blocking.append(it)

        return {
            "ok": True,
            "tag": raw["tag"],
            "date": raw["date"],
            "date_raw": now_raw,
            "at_war": raw["at_war"],
            "has_blocking": bool(blocking),
            "blocking_count": len(blocking),
            "blocking": blocking,
            "acked": acked,
            "advisory": advisory,
            "guard": guard_status(),
        }


def ack(g, code, reason, snooze_days=None, force=False, persist=True):
    """显式签收单条红点（带反糊弄预算、连续续签上限、严控项与一键可清项保护）。"""
    code = str(code or "").strip().upper()
    reason_s = str(reason or "").strip()
    if not code:
        return {"ok": False, "error": "缺少必填参数 code"}
    if len(reason_s) < 2 or reason_s.lower() in _LOW_EFFORT_REASONS:
        return {
            "ok": False,
            "blocked_by": "low_effort_reason",
            "error": (
                "签收红点必须填写具体战略理由 reason（禁止空话/套话 '%s'），"
                "请说明你为何此刻不处理以及计划何时处理。" % reason_s
            ),
        }
    is_strict = code in STRICT_CODES
    is_auto_solvable = code in AUTO_SOLVABLE_CODES
    if is_strict and not force:
        return {
            "ok": False,
            "blocked_by": "strict_red_dot",
            "code": code,
            "error": (
                "%s 属于严控红点，默认拒绝普通签收；请优先调用对应工具实际解决。"
                "如确有特殊战略原因（例如科技全超前/事件暂留），必须显式传 force=True 且最多豁免 %d 天。"
                % (code, int(CONFIG["strict_max_snooze_days"]))
            ),
        }
    if is_auto_solvable and not force:
        return {
            "ok": False,
            "blocked_by": "auto_solvable_red_dot",
            "code": code,
            "error": (
                "%s 可由工具 1 步直接解决（如 hoi4_housekeep / hoi4_rebalance_production / "
                "hoi4_assign_commander），禁止用普通 ack 糊弄跳过！请直接调用对应工具处理；"
                "确有特殊原因必须传 force=True（最多豁免 %d 天）。"
                % (code, int(CONFIG["strict_max_snooze_days"]))
            ),
        }

    is_short_cap = is_strict or is_auto_solvable
    max_days = (
        int(CONFIG["strict_max_snooze_days"])
        if is_short_cap
        else int(CONFIG["max_snooze_days"])
    )
    default_days = (
        int(CONFIG["strict_max_snooze_days"])
        if is_short_cap
        else int(CONFIG["default_snooze_days"])
    )
    try:
        days = int(snooze_days) if snooze_days is not None else default_days
    except Exception:
        days = default_days
    days = max(1, min(days, max_days))

    with _LOCK:
        ev = evaluate(g, fast=False)
        st = _get_state(g)
        now_raw = ev.get("date_raw")
        day_bucket = str(int(now_raw) // 24) if now_raw is not None else "unknown"
        daily_log = st.setdefault("daily_ack_log", {})
        # 只保留最近日期，防止字典膨胀
        if len(daily_log) > 16:
            for k_old in list(daily_log.keys())[:-8]:
                daily_log.pop(k_old, None)
        today_codes = list(daily_log.get(day_bucket) or [])
        max_per_day = int(CONFIG.get("max_acks_per_day", 4))
        if code not in today_codes and len(today_codes) >= max_per_day:
            return {
                "ok": False,
                "blocked_by": "ack_budget_exceeded",
                "code": code,
                "acked_today": today_codes,
                "max_acks_per_day": max_per_day,
                "error": (
                    "今日已签收 %d 项红点 [%s]，达到单日上限 %d！禁止批量刷 ack_red_dot 绕过门禁，"
                    "请先实际调用工具解决剩余红点（如 hoi4_housekeep / hoi4_upgrade_template_preset / "
                    "hoi4_rebalance_production）。"
                    % (len(today_codes), ", ".join(today_codes), max_per_day)
                ),
            }

        streaks = st.setdefault("ack_streaks", {})
        cur_streak = int(streaks.get(code) or 0)
        max_streak = int(CONFIG.get("max_consecutive_acks", 2))
        # 2026-11-23 (C-9 根因修复): 仅对有工具可直接消解的资源/编组/训练红点启用连续续签拦截；
        # ACTIVE_MISSION / FUEL_ZERO / NEW_DECISION / IDLE_DOCK 等受外部周期或资源供给制约的项
        # 绝不触发 ack_streak_exceeded 导致整档死锁停表。
        if code in STREAK_LIMITED_CODES and cur_streak >= max_streak and not force:
            return {
                "ok": False,
                "blocked_by": "ack_streak_exceeded",
                "code": code,
                "consecutive_acks": cur_streak,
                "max_consecutive_acks": max_streak,
                "error": (
                    "%s 已连续 %d 次靠 ack 推迟而从未实际解决！禁止无限续签，"
                    "请先调用对应工具真正处理（如 ARMY_XP_CAP 调 hoi4_upgrade_template_preset）。"
                    % (code, cur_streak)
                ),
            }
        if code in STREAK_LIMITED_CODES and cur_streak >= max_streak and force:
            days = min(days, int(CONFIG["strict_max_snooze_days"]))

        matched = next(
            (it for it in (ev.get("blocking") or []) + (ev.get("acked") or []) if it["code"] == code),
            None,
        )
        # 2026-11-23 (C-4 根因修复): 限时任务 ACTIVE_MISSION 若未显式传 snooze_days，
        # 自动覆盖该任务的完整倒计时周期（上限 365 天），避免 180 天任务每 30 天强拦一次。
        if code == "ACTIVE_MISSION" and matched:
            mt = (matched.get("detail") or {}).get("max_timeout_days")
            try:
                mt_i = int(mt) if mt else 0
            except Exception:
                mt_i = 0
            if mt_i > 0:
                eff_cap = max(max_days, min(365, mt_i))
                if snooze_days is None:
                    days = max(days, min(365, mt_i))
                else:
                    try:
                        days = max(1, min(int(snooze_days), eff_cap))
                    except Exception:
                        pass
        # 签收 NEW_DECISION 时顺带把当前新决议写入 seen_decisions，避免签收完立刻换签名复触发
        if code == "NEW_DECISION" and matched:
            for d in (matched.get("detail") or {}).get("decisions") or []:
                if d.get("key"):
                    st["seen_decisions"].add(d["key"])
        # 签收 JUSTIFY_LOST 时清理已确认的丢失目标，避免永久卡死
        if code == "JUSTIFY_LOST" and matched:
            for tgt_l in (matched.get("detail") or {}).get("targets") or []:
                st.setdefault("lost_justifying", set()).discard(tgt_l)

        expire_raw = (int(now_raw) + days * 24) if now_raw is not None else None
        rec = {
            "code": code,
            "reason": reason_s,
            "snooze_days": days,
            "force": bool(force),
            "tier": matched["tier"] if matched else 1,
            "signature": matched["signature"] if matched else ("%s:manual" % code),
            "ack_date_raw": now_raw,
            "expire_date_raw": expire_raw,
            "acked_at": round(time.time(), 1),
            "streak": cur_streak + 1,
        }
        st["acks"][code] = rec
        streaks[code] = cur_streak + 1
        if code not in today_codes:
            today_codes.append(code)
            daily_log[day_bucket] = today_codes
        if persist:
            _save_persisted()
        return {
            "ok": True,
            "acked": rec,
            "matched_active": matched is not None,
            "daily_ack_used": len(today_codes),
            "daily_ack_limit": max_per_day,
            "remaining_blocking": [
                x["code"] for x in (ev.get("blocking") or []) if x["code"] != code
            ],
        }


def ack_many(g, ack_list, persist=True):
    """批量签收（供 hoi4_pause(paused=False, ack_red_dots=[...]) 一步签收并放行）。"""
    results = []
    all_ok = True
    for entry in (ack_list or []):
        if not isinstance(entry, dict):
            results.append({"ok": False, "error": "ack 条目必须是 dict"})
            all_ok = False
            continue
        r = ack(
            g,
            code=entry.get("code"),
            reason=entry.get("reason"),
            snooze_days=entry.get("snooze_days"),
            force=bool(entry.get("force", False)),
            persist=False,
        )
        results.append(r)
        if not r.get("ok"):
            all_ok = False
    if persist:
        with _LOCK:
            _save_persisted()
    return {"ok": all_ok, "results": results}


def check_unpause_gate(g, ack_red_dots=None, ensure_paused=True):
    """闸门 B：开表/推进时间前的硬拦截。无阻塞红点返回 None，否则返回阻塞 dict。"""
    if not CONFIG.get("enabled", True):
        return None
    if ack_red_dots:
        ack_res = ack_many(g, ack_red_dots)
        if not ack_res.get("ok"):
            return {
                "ok": False,
                "blocked_by": "red_dot_ack_failed",
                "error": "部分红点签收未通过（严控项需 force=True 且必须填写原因）",
                "ack_results": ack_res["results"],
            }
    ev = evaluate(g, fast=False)
    if not ev.get("has_blocking"):
        return None
    paused = _force_pause_safe(g) if ensure_paused else _is_paused_fast(g)
    blocking = ev["blocking"]
    codes = [x["code"] for x in blocking]
    housekeepable = [x["code"] for x in blocking if x.get("housekeepable")]
    return {
        "ok": False,
        "blocked_by": "red_dots",
        "paused": paused,
        "date": ev.get("date"),
        "blocking_count": len(blocking),
        "blocking_codes": codes,
        "housekeepable_codes": housekeepable,
        "error": (
            "拒绝取消暂停/推进时间：存在 %d 项未处理红点 [%s]。"
            "请先调用对应工具清红点，或对确需战略保留的项调用 hoi4_ack_red_dot(code, reason, snooze_days)。"
            % (len(blocking), ", ".join(codes))
        ),
        "red_dots": blocking,
        "advisory": ev.get("advisory") or [],
    }


def format_briefing_section(ev):
    """为 Compact turnengine 简报生成极简高信号红点置顶段落。"""
    blocking = (ev or {}).get("blocking") or []
    advisory = (ev or {}).get("advisory") or []
    if not blocking and not advisory:
        return []
    out = []
    if blocking:
        out.append("## MUST_RESOLVE（红点门禁，未处理或未 ack_red_dot 禁止推进时间）")
        for it in blocking:
            tag = "[严控]" if it.get("force_ack_only") else ("[可housekeep]" if it.get("housekeepable") else "[待决策]")
            out.append("- %s `%s`: %s" % (tag, it["code"], it["title"]))
            acts = it.get("suggested_actions") or []
            if acts:
                out.append("  -> 建议: %s" % acts[0])
    if advisory:
        brief_adv = "; ".join("%s(%s)" % (x["code"], x["title"]) for x in advisory[:3])
        out.append("- 红点提醒(不阻塞): %s" % brief_adv)
    return out


# ===================================================================== 闸门 C：后台急刹守护
class RedDotGuard:
    """轻量纯只读后台急刹守护线程。

    只在游戏未暂停（运行中）时每 ~1.5s 轻扫一次；一旦命中尚未急刹过的阻塞红点，
    立即直写 1 字节 `idler+0x6C1 = 1` 物理停表，绝不抢窗口焦点、绝不调用引擎注入、
    绝不对同一未变化红点重复按暂停（避免与人类玩家抢空格键）。
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._thread = None
        self._stop = threading.Event()
        self._game_getter = None
        self._busy_check = None
        self.last_pause = None
        self.ticks = 0
        self.brakes = 0

    def start(self, game_getter, busy_check=None):
        with self._lock:
            self._game_getter = game_getter
            self._busy_check = busy_check
            if self._thread is not None and self._thread.is_alive():
                return True
            self._stop.clear()
            t = threading.Thread(target=self._loop, name="hoi4-reddot-guard", daemon=True)
            self._thread = t
            t.start()
            return True

    def stop(self):
        with self._lock:
            self._stop.set()
            t = self._thread
            self._thread = None
        if t is not None and t.is_alive():
            t.join(timeout=2.5)
        return True

    def is_running(self):
        t = self._thread
        return bool(t is not None and t.is_alive() and not self._stop.is_set())

    def poll_once(self, g):
        """单步巡检（也方便单测直接同步验证后台急刹逻辑）。"""
        if not CONFIG.get("enabled", True) or not CONFIG.get("guard_enabled", True):
            return None
        paused = _is_paused_fast(g)
        if paused is not False:
            return None
        ev = evaluate(g, fast=True)
        if not ev.get("has_blocking"):
            return None
        with _LOCK:
            st = _get_state(g)
            fresh = [
                it for it in ev["blocking"]
                if st["braked_sigs"].get(it["code"]) != it["signature"]
            ]
            if not fresh:
                return None
            # 命中新阻塞红点 → 立即零注入 1 字节急刹停表
            did_pause = _force_pause_safe(g)
            for it in fresh:
                st["braked_sigs"][it["code"]] = it["signature"]
            rec = {
                "at_unix": round(time.time(), 1),
                "date": ev.get("date"),
                "paused": did_pause,
                "codes": [x["code"] for x in fresh],
                "titles": [x["title"] for x in fresh[:4]],
            }
            self.last_pause = rec
            self.brakes += 1
            return rec

    def _loop(self):
        while not self._stop.is_set():
            poll = max(0.5, float(CONFIG.get("guard_poll_seconds", 1.5)))
            if self._stop.wait(poll):
                break
            if not CONFIG.get("enabled", True) or not CONFIG.get("guard_enabled", True):
                continue
            try:
                if self._busy_check is not None and self._busy_check():
                    continue
                if self._game_getter is None:
                    continue
                g = self._game_getter()
                if g is None:
                    continue
                self.ticks += 1
                self.poll_once(g)
            except Exception:
                continue


_GUARD = RedDotGuard()


def start_guard(game_getter, busy_check=None):
    return _GUARD.start(game_getter, busy_check=busy_check)


def stop_guard():
    return _GUARD.stop()


def guard_status():
    return {
        "enabled": bool(CONFIG.get("enabled", True)),
        "guard_enabled": bool(CONFIG.get("guard_enabled", True)),
        "thread_alive": _GUARD.is_running(),
        "poll_seconds": float(CONFIG.get("guard_poll_seconds", 1.5)),
        "peace_air_navy_block": bool(CONFIG.get("peace_air_navy_block", False)),
        "ticks": _GUARD.ticks,
        "brakes": _GUARD.brakes,
        "last_pause": _GUARD.last_pause,
    }


def summary(g):
    """极简摘要（附带在 hoi4_status / hoi4_overview 顶层，提示模型是否被红点急刹停表）。"""
    ev = evaluate(g, fast=True)
    return {
        "enabled": bool(CONFIG.get("enabled", True)),
        "has_blocking": bool(ev.get("has_blocking")),
        "blocking_count": int(ev.get("blocking_count") or 0),
        "blocking_codes": [x["code"] for x in (ev.get("blocking") or [])],
        "advisory_codes": [x["code"] for x in (ev.get("advisory") or [])],
        "acked_codes": [x["code"] for x in (ev.get("acked") or [])],
        "guard_last_pause": _GUARD.last_pause,
    }
