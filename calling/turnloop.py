"""Turn loop: let the model sleep and wake at the right moment.

Checking the game every second burns tokens; never checking misses openings. This
lets the caller say "do not wake me until one of these happens, but look again
after N days at the latest", and tells it why it woke up.

ALL PURE READS. The one exception is pausing on wake (`pause_on_wake`), which goes
through the same command channel as the player's space key. No injected call is
ever made inside the loop, and paths like `country_stats()` / `g.wars()` that stop
every game thread are avoided, one of those inside a tick loop froze a live game.

Signals, all taken from game state rather than the UI:
  focus         CCountry+0x1348         -> focus finished, or none selected
  research      research_slots()        -> research done, a slot freed
  decisions     decisions()             -> a new decision became available
  production    CProductionStatus+0x58  -> requested factories < available (idle)
  construction  CProductionStatus+0x70  -> construction queue emptied
  deployment    CDeploymentStatus+0x80  -> a division finished training
  war           CWarRelation / battles  -> war declared, new battle, battle lost
  political_power                       -> threshold reached (advisor or decision)
"""
import os
import time

import combat

CO_PRODUCTION  = 0xF40        # CProductionStatus
CO_DEPLOYMENT  = 0xF48        # CDeploymentStatus
PROD_MIL_LINES = 0x58         # CPdxArray<CMilitaryProductionLine*>
PROD_BLD_LINES = 0x70         # CPdxArray<CBuildingProductionLine*>, construction queue
DEP_QUEUE      = 0x80         # CPdxArray<CMilitaryDeployment*>
DEP_PROGRESS   = 0x60         # training progress (fixed point)
DEP_TARGET     = 0x68         # target (fixed point, usually 1.0)

# 统一单次等待实时刹车上限（可通过 HOI4_WAIT_MAX_S 环境变量覆盖，范围 5..120s；
# 默认 26s 以安全避开常见 30s MCP 客户端超时 -32001，60s+ 客户端可设 HOI4_WAIT_MAX_S=38..90）
DEFAULT_WAIT_MAX_S = 26


def wait_max_seconds():
    """读取全工具链统一的 wait 实时上限（HOI4_WAIT_MAX_S，默认 26s，范围 5..120s）。"""
    raw = os.environ.get("HOI4_WAIT_MAX_S")
    if raw is None or str(raw).strip() == "":
        return DEFAULT_WAIT_MAX_S
    try:
        val = int(float(str(raw).strip()))
    except Exception:
        return DEFAULT_WAIT_MAX_S
    return max(5, min(120, val))


def resolve_wait_timeout(requested=None, default=None, max_s=None):
    """统一计算并回显 wait 工具的有效 timeout_seconds，消除多处硬编码互相截断。"""
    cap = int(wait_max_seconds() if max_s is None else max(5, min(120, int(max_s))))
    base_default = cap if default is None else min(int(default), cap)
    req_raw = requested
    if requested is None:
        val = base_default
        req_num = None
    else:
        try:
            req_num = int(float(requested))
            val = req_num
        except Exception:
            req_num = None
            val = base_default
    eff = max(5, min(val, cap))
    return {
        "requested": req_num if req_raw is not None else None,
        "effective": eff,
        "max_s": cap,
        "clamped": bool(req_num is not None and eff != req_num),
    }


ALL_SIGNALS = ("focus", "research", "decisions", "events", "production",
               "construction", "deployment", "war", "peace", "wargoal",
               "political_power", "housekeep")


def _fx(p, a):
    return p.i64(a) / 1e5


def deployment_status(g):
    """Deployment queue: how many divisions are queued, how many are ready."""
    from game import _arr
    p = g.p
    try:
        dep = p.u64(g.player().ptr + CO_DEPLOYMENT)
        q = _arr(p, dep, DEP_QUEUE)
    except Exception:
        return {"queued": 0, "ready": 0}
    ready = 0
    for d in q:
        try:
            prog, target = _fx(p, d + DEP_PROGRESS), _fx(p, d + DEP_TARGET)
            if target > 0 and prog >= target * 0.995:
                ready += 1
        except Exception:
            pass
    return {"queued": len(q), "ready": ready}


def factory_status(g):
    """Detect idle factories.

    2026-11-23 (复测 6 P1-1): 优先使用 g.factory_allocation()（引擎 CProductionStatus
    + CStrategicNavy 修理船坞 + CShipRefitProductionLine 改装线精确口径），
    避免把占领区非核心折算船坞或改装/修理占用船坞误报为空闲船坞。
    """
    c = g.player()
    lines = []
    try:
        lines = g.production_lines()
        mil_lines = len(lines)
    except Exception:
        mil_lines = 0
    try:
        bld = g.construction_queue()
        bld_lines = len(bld)
        civ_used = sum(int(l.get("factories") or 0) for l in bld)
    except Exception:
        bld_lines = civ_used = 0

    alloc = None
    if hasattr(g, "factory_allocation"):
        try:
            alloc = g.factory_allocation()
        except Exception:
            alloc = None
    if isinstance(alloc, dict) and alloc.get("source") == "engine_production_status":
        avail = int(alloc["military_available"])
        requested = int(alloc["military_used"])
        mil_idle = int(alloc["military_idle"])
        nav_avail = int(alloc["naval_available"])
        nav_requested = int(alloc["naval_used"])
        nav_idle = int(alloc["naval_idle"])
        civ_total = int(alloc.get("civilian_factories", c.civilian_factories))
        return {
            "military_available": avail,
            "military_requested": requested,
            "military_idle": mil_idle,
            "naval_available": nav_avail,
            "naval_requested": nav_requested,
            "naval_idle": nav_idle,
            "naval_repair_dockyards": int(alloc.get("naval_repair_dockyards") or 0),
            "naval_refit_dockyards": int(alloc.get("naval_refit_used") or 0),
            "production_lines": mil_lines,
            "civilian_factories": civ_total,
            "civilian_in_construction": civ_used,
            "construction_queue": bld_lines,
            "civilian_idle": max(0, civ_total - civ_used),
            "civilian_idle_note": "includes consumer-goods and trade-export factories (not all free for construction)",
            "source": "engine_production_status",
        }

    try:
        requested = sum(int(l.get("factories") or 0) for l in lines
                        if l.get("class") == "CMilitaryProductionLine")
        nav_requested = sum(int(l.get("factories") or 0) for l in lines
                            if l.get("class") in ("CNavalProductionLine", "CShipRefitProductionLine"))
    except Exception:
        requested = nav_requested = 0
    avail = c.military_factories
    return {"military_available": avail, "military_requested": requested,
            "military_idle": max(0, avail - requested),
            "naval_available": c.naval_dockyards, "naval_requested": nav_requested,
            "naval_idle": max(0, c.naval_dockyards - nav_requested),
            "production_lines": mil_lines,
            "civilian_factories": c.civilian_factories,
            "civilian_in_construction": civ_used,
            "construction_queue": bld_lines,
            "civilian_idle": max(0, c.civilian_factories - civ_used),
            "civilian_idle_note": "includes consumer-goods and trade-export factories (not all free for construction)"}


def snapshot(g, signals=ALL_SIGNALS):
    """Current value of everything being watched. PURE READS."""
    s = {"date": None, "tag": None}
    try:
        # 2026-11-22 (P2-3): `date` used to be the raw integer while every other
        # tool reports "1936.01.07.00".  Keep the number in `date_raw` and give
        # `date` the same text form the rest of the toolset uses.
        _d = g.date()
        s["date"] = _d.get("text")
        s["date_raw"] = _d.get("raw")
    except Exception:
        try:
            s["date_raw"] = g.date_raw()
        except Exception:
            pass
    try:
        s["tag"] = g.player_tag()
    except Exception:
        # If the played country cannot be resolved, no signal can be trusted.
        return s
    if "focus" in signals:
        try:
            f = g.current_focus()
            s["focus"] = f.get("key") if f else None
        except Exception:
            s["focus"] = None
        try:
            fp = g.focus_progress() or {}
            prog_val = fp.get("progress")
            if prog_val is None:
                prog_val = fp.get("raw_0x38")
            s["focus_progress"] = round(float(prog_val or 0.0), 3)
            if fp.get("days_left") is not None:
                s["focus_days_left"] = fp.get("days_left")
            if fp.get("percent") is not None:
                s["focus_percent"] = fp.get("percent")
        except Exception:
            pass
    if "research" in signals:
        try:
            slots = g.research_slots()
            # 2026-11-18: `researching` is a NAME and stays None while the tech
            # table is cold, so a snapshot taken early reported four idle slots
            # that were actually busy. slot_busy is a raw pointer read.
            s["research"] = tuple(
                (sl.get("researching") or "<busy:%d>" % sl.get("slot", -1))
                if sl.get("slot_busy") else None for sl in slots)
            s["research_free"] = sum(1 for sl in slots if not sl.get("slot_busy"))
        except Exception:
            pass
    if "decisions" in signals:
        try:
            # 2026-11-22 (P3-3): `visible` is evaluated globally, so this counted
            # 13 while hoi4_decisions(mine_only=true) showed 9.  Same filter here.
            s["decisions"] = frozenset(
                d.get("key") for d in g.decisions(which="visible", rich=True)
                if d.get("mine", True))
        except Exception:
            pass
    if "events" in signals:
        try:
            import events_win as _ew
            s["events"] = frozenset(
                str(r.get("event_id")) for r in (_ew.pending(g) or [])
                if r.get("event_id"))
        except Exception:
            pass
    if "production" in signals or "construction" in signals:
        try:
            s["factories"] = factory_status(g)
        except Exception:
            pass
    if "deployment" in signals:
        try:
            s["deployment"] = deployment_status(g)
        except Exception:
            pass
    if "war" in signals or "battles" in signals:
        try:
            s["enemies"] = frozenset(combat.enemy_tags(g))
            recs = combat.combats(g, only_mine=True)
            s["combats"] = len(recs)
            s["combat_provinces"] = frozenset(r["province"] for r in recs)
        except Exception:
            pass
    if "peace" in signals or "war" in signals:
        try:
            import peacewin as _pw
            s["peace_open"] = bool(_pw.live_conference_readonly(g))
        except Exception:
            s["peace_open"] = False
    if "wargoal" in signals:
        me = s.get("tag")
        try:
            import diplomacy as _dip
            wg_rows, wg_ok = _dip.country_wargoals_fast(g, tag=me, valid_only=True)
            if wg_ok:
                s["wargoals"] = frozenset(
                    (
                        str(r.get("target_tag") or "").upper(),
                        str(r.get("wargoal") or "wargoal"),
                    )
                    for r in (wg_rows or [])
                    if r.get("target_tag")
                )
        except Exception:
            pass
        try:
            import diplomacy as _dip
            just_rows = _dip.justifying_fast(g, tag=me) or []
            s["justifying"] = frozenset(
                str(r.get("target") or "").upper()
                for r in just_rows
                if r.get("target")
            )
        except Exception:
            pass
    if "political_power" in signals:
        try:
            s["political_power"] = g.player().political_power
        except Exception:
            pass
    if "housekeep" in signals:
        try:
            slots = g.research_slots()
            fs = factory_status(g)
            pp = g.player().political_power
            s["housekeep"] = {
                "empty_research": sum(1 for x in slots if not x.get("slot_busy")),
                "focus": (g.current_focus() or {}).get("key"),
                "pp": pp,
                "military_idle": fs.get("military_idle", 0),
                "construction_queue": fs.get("construction_queue", 0),
                "unassigned": len(g.unassigned_divisions()),
            }
        except Exception:
            pass
    return s


def wake_reasons(prev, cur, pp_threshold=None, wake_on_battle=False):
    """Changes between two snapshots that the caller should know about."""
    out = []
    if not prev or not cur or prev.get("tag") != cur.get("tag"):
        if prev and cur and prev.get("tag") != cur.get("tag"):
            out.append("played country changed: %s -> %s" % (prev.get("tag"), cur.get("tag")))
        return out

    if "focus" in prev and "focus" in cur and prev["focus"] != cur["focus"]:
        out.append("focus finished/changed: %s -> %s" % (prev["focus"], cur["focus"]))
    if cur.get("focus") is None and prev.get("focus") is None and "focus" in cur:
        pass  # an empty focus should not fire every tick; only the transition does

    if "research" in prev and "research" in cur:
        done = [t for t in prev["research"] if t and t not in cur["research"]]
        if done:
            out.append("research finished: %s" % ", ".join(done))
        if cur.get("research_free", 0) > prev.get("research_free", 0):
            out.append("research slot freed (%d free)" % cur["research_free"])

    if "decisions" in prev and "decisions" in cur:
        fresh = cur["decisions"] - prev["decisions"]
        if fresh:
            out.append("new decision: %s" % ", ".join(sorted(fresh)[:5]))
    if "events" in prev and "events" in cur:
        fresh_ev = cur["events"] - prev["events"]
        if fresh_ev:
            out.append("new event: %s" % ", ".join(sorted(fresh_ev)[:5]))

    pf, cf = prev.get("factories"), cur.get("factories")
    if pf and cf:
        if cf["military_idle"] > 0 and cf["military_idle"] > pf["military_idle"]:
            out.append("idle military factories: %d" % cf["military_idle"])
        if cf["construction_queue"] == 0 and pf["construction_queue"] > 0:
            out.append("construction queue empty (%d civilian factories idle)" % cf["civilian_factories"])

    pd, cd = prev.get("deployment"), cur.get("deployment")
    if pd and cd and cd["ready"] > pd["ready"]:
        out.append("divisions ready to deploy: %d" % cd["ready"])

    if "enemies" in prev and "enemies" in cur:
        fresh = cur["enemies"] - prev["enemies"]
        ended = prev["enemies"] - cur["enemies"]
        if fresh:
            out.append("WAR: %s" % ", ".join(sorted(fresh)))
        if ended:
            out.append("war ended: %s" % ", ".join(sorted(ended)))
    # 2026-11-23 (复测 7 B-9): 0 注入纯只读 O(1) 监测和平会议，出现第一帧即唤醒并停表
    if cur.get("peace_open") and not prev.get("peace_open"):
        out.append("peace conference opened")
    # 2026-11-25 (长跑实测 P0): 0 注入 O(1) 双向监测宣战理由（CB）与正当化完成/丢失
    fresh_cb_targets = set()
    if "wargoals" in prev and "wargoals" in cur:
        fresh_cb = cur["wargoals"] - prev["wargoals"]
        if fresh_cb:
            for tgt, wg_name in sorted(fresh_cb):
                fresh_cb_targets.add(tgt)
                out.append("wargoal ready (CB available): %s (%s)" % (tgt, wg_name))
    if "justifying" in prev and "justifying" in cur:
        ended_just = prev["justifying"] - cur["justifying"]
        if ended_just:
            cur_wg_targets = {tgt for tgt, _wg in (cur.get("wargoals") or ())}
            cur_enemies = cur.get("enemies") or frozenset()
            for tgt in sorted(ended_just):
                if tgt in cur_wg_targets:
                    if tgt not in fresh_cb_targets:
                        out.append("justification completed -> CB ready: %s" % tgt)
                elif tgt not in cur_enemies:
                    out.append(
                        "JUSTIFY_LOST: justification against %s ended without producing a wargoal"
                        % tgt
                    )
    # 2026-11-23 (复测 6 P1-3): 战时每天都有零星新战斗，默认不再把 "new battle" 当成
    # 必醒事件打断长跑（仍会在新宣战/停战时立刻唤醒）；仅在显式开启 wake_on_battle=True
    # 或 signals 包含 "battles" 时按新战斗唤醒。
    if wake_on_battle and "combat_provinces" in prev and "combat_provinces" in cur:
        fresh = cur["combat_provinces"] - prev["combat_provinces"]
        if fresh:
            out.append("new battle: %s" % ", ".join(str(x) for x in sorted(fresh)[:5]))

    if pp_threshold is not None and cur.get("political_power") is not None:
        if cur["political_power"] >= pp_threshold > (prev.get("political_power") or 0):
            out.append("political power reached %d" % cur["political_power"])
    return out


class TurnLoop:
    """The sleep and wake loop.

    wait() returns when a watched signal changes, when the requested date arrives,
    or when `max_days` runs out, whichever comes first. It ALWAYS returns; there
    is no path that waits forever.
    """

    def __init__(self, game, poll=1.5, pause_on_wake=False):
        self.g = game
        self.poll = float(poll)
        self.pause_on_wake = bool(pause_on_wake)
        self.last = None

    def snapshot(self, signals=ALL_SIGNALS):
        return snapshot(self.g, signals)

    @staticmethod
    def _force_pause_safe(g):
        """超时或被看门狗取消时无条件安全停表（优先 session.pause，失败兜底直写 1 字节）。"""
        try:
            import session as _sess
            ok, _after = _sess.pause(g, True)
            if ok:
                return True
        except Exception:
            pass
        try:
            import session as _sess
            p = getattr(g, "p", None)
            if p is not None:
                idler = p.u64(p.base + _sess.G_IDLER)
                if idler:
                    p.write(idler + _sess.IDLER_PAUSED, b"\x01")
                    return bool(p.u8(idler + _sess.IDLER_PAUSED))
        except Exception:
            pass
        return None

    def wait(self, max_days=7, until_date=None, signals=ALL_SIGNALS,
             pp_threshold=None, poll=None, timeout_seconds=900,
             pause_on_wake=None, wartime_micro=False, wartime_enemy=None,
             wartime_interval_days=3, wartime_report_days=15,
             stop_event=None, respect_red_dots=True, wake_on_battle=False,
             housekeep_interval_days=10, timeout_meta=None,
             autopilot=None, autopilot_interval_days=1,
             fast_forward=False, fast_forward_speed=3):
        """Wait for a signal, for the date, or for max_days: whichever first.

        max_days       : in-game days. Even with "do not disturb me", it ALWAYS
                         wakes at the end of this window.
        until_date     : (year, month, day). Sleep until this date.
        timeout_seconds: real-time safety brake. While the game is paused the game
                         clock never moves, and we must not wait forever.
        wartime_micro  : while at war, run one local wartime_step every
                         `wartime_interval_days` game days; do not wake the model
                         unless the front moved >=4 provinces or 15 days passed.
        autopilot      : None (default) = auto-enable when Standing Orders are active
                         in this session; True = enable if active; False = bypass.
        fast_forward   : when True, safely set speed <= 3 (UI 4-bar cap) and unpause
                         after pre-run checks; Kernel Floor still polls every cycle.
        """
        import api
        g = self.g

        # 2026-11-25 (Standing Orders v1): 检查自动驾驶是否启用；若启用则在闸门 B 前
        # 先消化可由 Standing Orders 合法处理的空国策/空科研/建造/军工/新兵/无分歧事件，
        # 命中内核熔断地板时直接强制停表返回。
        ap_mod = None
        ap_on = False
        ap_ticks = 0
        ap_actions_total = 0
        ap_wake_triggers = []
        if autopilot is not False:
            try:
                import autopilot as _ap
                if _ap.is_active(g):
                    ap_mod = _ap
                    ap_on = True
            except Exception:
                ap_mod = None
                ap_on = False

        if ap_on and ap_mod is not None:
            pre_ap = ap_mod.run_tick(g)
            ap_ticks += 1
            ap_actions_total += len(pre_ap.get("actions") or ())
            if pre_ap.get("wake_triggers"):
                ap_wake_triggers = list(pre_ap["wake_triggers"])
                paused_pre = self._force_pause_safe(g)
                ap_st_now = ap_mod.status(g)
                return {
                    "ok": bool(pre_ap.get("ok", True)),
                    "woke_because": [
                        "autopilot[%s]: %s" % (t.get("floor"), t.get("detail"))
                        for t in ap_wake_triggers[:5]
                    ],
                    "waited_game_days": 0.0,
                    "waited_real_seconds": 0.0,
                    "timed_out": False,
                    "paused": paused_pre,
                    "autopilot_woke": True,
                    "autopilot": {
                        "active": True,
                        "phase": ap_st_now.get("current_phase"),
                        "ticks": ap_ticks,
                        "actions_taken": ap_actions_total,
                        "wake_triggers": ap_wake_triggers,
                        "days_elapsed": ap_st_now.get("days_elapsed"),
                        "journal_tail": ap_st_now.get("journal_tail", [])[-8:],
                    },
                }

        # 2026-11-23: 闸门 A/B 开跑前硬拦截 —— 若当前已有未解决且未签收的阻塞红点
        # （待决事件/空国策/空科研/PP>=500/XP>=100/空闲工厂/新决议/限时任务等），
        # 直接强制停表并返回红点清单，严禁带病偷跑。
        if respect_red_dots:
            try:
                import reddots as _rd
                gate = _rd.check_unpause_gate(g, ensure_paused=True)
                if gate:
                    gate["woke_because"] = [
                        "red_dot_gate: " + ", ".join(gate.get("blocking_codes") or [])
                    ]
                    gate["waited_game_days"] = 0.0
                    gate["waited_real_seconds"] = 0.0
                    gate["timed_out"] = False
                    return gate
            except Exception:
                pass

        # fast_forward: 过闸门后安全提速（上限 speed=3 即 UI 4 速）并自动解暂停
        if fast_forward:
            try:
                if hasattr(g, "set_game_speed"):
                    g.set_game_speed(min(3, max(0, int(fast_forward_speed or 3))), measure_seconds=0)
            except Exception:
                pass
            try:
                import session as _sess_ff
                if _sess_ff.is_paused(g):
                    _sess_ff.pause(g, False)
            except Exception:
                pass

        poll = float(poll if poll is not None else self.poll)
        signals = tuple(signals)
        if wartime_micro:
            signals = tuple(sorted(set(signals) | {"war"}))
        if ap_on:
            # 自动驾驶期间强制保留内核地板所需信号（CB/战争/和会/事件/国策/科研/产线/建造/新兵）
            signals = tuple(sorted(set(signals) | {
                "wargoal", "war", "peace", "events",
                "focus", "research", "production", "construction", "deployment",
            }))
        base = snapshot(g, signals)
        start_hours = base.get("date_raw")
        target_hours = None
        if until_date:
            target_hours = api.parts_to_date(*until_date)
        deadline_hours = (start_hours + int(max_days) * 24) if start_hours is not None else None
        if target_hours is not None:
            deadline_hours = target_hours if deadline_hours is None else min(deadline_hours, target_hours)
        # In war the game can run at speed 5, which is ~4.5 game days per real
        # second, so a "max_days" window can expire in a couple of seconds and
        # the 3-day micro tick would only ever fire ONCE. When wartime_micro is
        # on, the window is counted in MICRO TICKS instead: the loop keeps
        # ticking every `wartime_interval_days` game days until it has done
        # `max_days` of them.
        war_ticks_target = int(max_days) if wartime_micro else None

        t_start = time.time()
        prev = base
        last_housekeep_date = start_hours
        last_ap_tick_h = start_hours
        war_enemy = (wartime_enemy or "").upper() or None
        last_war_tick = start_hours
        last_war_report = start_hours
        war_changed = 0
        war_steps = 0
        war_last = None
        war_baseline = False
        # 2026-11-11: the game pauses itself on every "NATIONAL FOCUS COMPLETED"
        # / research-finished popup. wait() then slept until the 900 s real-time
        # brake and reported "the game may be paused", which is exactly how the
        # previous run deadlocked for hours. Clear it here instead: three
        # consecutive polls with a frozen clock while paused means a popup is
        # holding the game, not that the caller wanted a pause.
        stuck_paused = 0
        auto_unpaused = 0
        last_stamp = base.get("date")
        # 2026-11-22 (复测 5 §3.2): 动态估算游戏流速（天/秒），临近 max_days 时自动收紧
        # 轮询步长到 0.2~0.5s，避免 4 速下一次 sleep 4s 直接超冲 10+ 游戏日；同时每步
        # 受剩余 timeout_seconds 与 stop_event 协同取消约束，绝不越过看门狗继续狂奔。
        est_dps = 3.5
        try:
            sp = g.game_speed() or {}
            sph = sp.get("seconds_per_hour")
            if sph and sph > 0:
                est_dps = max(0.2, 1.0 / (sph * 24.0))
            elif sp.get("speed") == 4:
                est_dps = 4.0
        except Exception:
            pass
        cur = base
        while True:
            rem_wall = float(timeout_seconds) - (time.time() - t_start)
            if rem_wall <= 0 or (stop_event is not None and stop_event.is_set()):
                break
            step = poll
            cur_h = cur.get("date_raw")
            if war_ticks_target is None and deadline_hours is not None and cur_h is not None:
                rem_days = max(0.0, (deadline_hours - cur_h) / 24.0)
                if rem_days <= 0:
                    step = 0.0
                else:
                    # 临近目标日期时把步长压缩至剩余预计耗时的一半，最少 0.2s
                    step = min(step, max(0.2, (rem_days / max(est_dps, 1.5)) * 0.45))
            step = min(step, max(0.05, rem_wall))
            if step > 0:
                if stop_event is not None:
                    if stop_event.wait(step):
                        break
                else:
                    time.sleep(step)
            if stop_event is not None and stop_event.is_set():
                break
            cur = snapshot(g, signals)
            _el = max(time.time() - t_start, 0.1)
            if cur.get("date_raw") is not None and start_hours is not None:
                _prog_days = (cur["date_raw"] - start_hours) / 24.0
                if _prog_days >= 0.5:
                    est_dps = max(0.2, _prog_days / _el)
            try:
                import session as _sess
                if _sess.is_paused(g):
                    if cur.get("date") == last_stamp:
                        stuck_paused += 1
                    else:
                        stuck_paused = 0
                    last_stamp = cur.get("date")
                    if stuck_paused >= 3:
                        # 2026-11-23: 若当前是因为红点门禁/待决事件暂停，绝不擅自解暂停！
                        # 2026-11-25: 同步检查 reddots.CONFIG['enabled'] 总开关，避免关掉总开关后闸门 A 仍误拦。
                        _has_rd = False
                        if respect_red_dots:
                            try:
                                import reddots as _rd
                                if _rd.CONFIG.get("enabled", True):
                                    _has_rd = bool(_rd.evaluate(g, fast=True).get("has_blocking"))
                            except Exception:
                                _has_rd = False
                        if not _has_rd:
                            g.dismiss_popups_and_unpause()
                            auto_unpaused += 1
                        stuck_paused = 0
                else:
                    stuck_paused = 0
                    last_stamp = cur.get("date")
            except Exception:
                pass
            reasons = wake_reasons(
                prev, cur, pp_threshold,
                wake_on_battle=bool(wake_on_battle or ("battles" in signals)),
            )

            # 2026-11-25 (Standing Orders v1): 自动驾驶循环内调度 + 不可关闭 Kernel Floor 高频巡检。
            # 若出现常规运营变更（国策/科研/建造/军工/新兵/事件）或到达 tick 周期，先交由
            # autopilot 确定性消化；仅当命中 Kernel Floor、硬信号或剩余阻塞红点时才停表唤醒模型。
            ap_floor_woke = False
            if ap_on and ap_mod is not None:
                _ROUTINE_PREFIXES = (
                    "focus finished/changed:",
                    "research finished:",
                    "research slot freed",
                    "idle military factories:",
                    "construction queue empty",
                    "divisions ready to deploy:",
                    "new event:",
                )
                routine_hits = [r for r in reasons if r.startswith(_ROUTINE_PREFIXES)]
                hard_reasons = [r for r in reasons if not r.startswith(_ROUTINE_PREFIXES)]
                now_h_ap = cur.get("date_raw")
                ap_interval_h = max(24, int(autopilot_interval_days or 1) * 24)
                ap_due = (
                    bool(routine_hits)
                    or (now_h_ap is not None and last_ap_tick_h is not None
                        and (now_h_ap - last_ap_tick_h) >= ap_interval_h)
                )
                if ap_due:
                    ap_res = ap_mod.run_tick(g, prev_snap=prev, cur_snap=cur)
                    ap_ticks += 1
                    ap_actions_total += len(ap_res.get("actions") or ())
                    if now_h_ap is not None:
                        last_ap_tick_h = now_h_ap
                    if ap_res.get("actions"):
                        # 动作执行后刷新快照，确保下方红点巡检读取到最新已消解状态
                        cur = snapshot(g, signals)
                    if ap_res.get("wake_triggers"):
                        ap_floor_woke = True
                        ap_wake_triggers = list(ap_res["wake_triggers"])
                        for _t in ap_wake_triggers[:5]:
                            hard_reasons.append("autopilot[%s]: %s" % (_t.get("floor"), _t.get("detail")))
                    # 常规运营原因若已被自动驾驶平稳接管，则不再唤醒模型；只保留硬原因
                    reasons = hard_reasons
                else:
                    # 非 tick 轮次依然 0 注入高速检查 Kernel Floor（CB / 开战 / 分歧事件 / 心跳 / 上限）
                    ap_st_cur = ap_mod.get_active_state(g)
                    fl_hits = ap_mod.check_kernel_floor(g, ap_st_cur, prev_snap=prev, cur_snap=cur)
                    if fl_hits:
                        ap_floor_woke = True
                        ap_wake_triggers = list(fl_hits)
                        for _t in ap_wake_triggers[:5]:
                            reasons.append("autopilot[%s]: %s" % (_t.get("floor"), _t.get("detail")))

            # 2026-11-23: 闸门 A 跑表期红点巡检 —— 任意新阻塞红点立即强制停表唤醒模型
            # 2026-11-25: 尊重全局开关 reddots.CONFIG['enabled']，使 hoi4_red_dot_guard(enabled=false)
            # 与闸门 B/C 语义完全统一。
            red_dot_blocking = []
            if respect_red_dots:
                try:
                    import reddots as _rd
                    if _rd.CONFIG.get("enabled", True):
                        _rd_ev = _rd.evaluate(g, fast=True)
                        if _rd_ev.get("has_blocking"):
                            red_dot_blocking = _rd_ev.get("blocking") or []
                            for _it in red_dot_blocking[:5]:
                                reasons.append("red_dot[%s]: %s" % (_it["code"], _it["title"]))
                except Exception:
                    pass
            # 2026-11-22 (复测 5 §8.4 时刻 ①): 科研完成的瞬间立刻比对新解锁装备是否
            # 导致现有产线过时，把 OBSOLETE_LINE 提醒直接附进唤醒原因和返回体。
            obsolete_unlocked = []
            if "research" in prev and "research" in cur:
                done_techs = [t for t in prev["research"] if t and t not in cur["research"]]
                if done_techs:
                    try:
                        import equipmeta as _em
                        _lines = g.production_lines(include_obsolete=False)
                        _avail = g._active_variant_names()
                        obsolete_unlocked = _em.check_tech_unlocks_obsolete(
                            done_techs, _lines, _avail
                        )
                        for _it in obsolete_unlocked[:3]:
                            reasons.append(_em.format_obsolete_alert(_it))
                    except Exception:
                        pass
            if (not ap_on) and "housekeep" in signals and cur.get("housekeep"):
                h = cur["housekeep"]
                hk_hours = max(24, int(housekeep_interval_days or 10) * 24)
                due = (last_housekeep_date is None or
                       (cur.get("date_raw") is not None and cur["date_raw"] - last_housekeep_date >= hk_hours))
                hot = (h.get("empty_research", 0) > 0 or not h.get("focus") or
                       h.get("pp", 0) >= 180 or h.get("military_idle", 0) >= 2 or
                       h.get("construction_queue", 0) <= 1 or h.get("unassigned", 0) >= 3)
                if hot and due:
                    reasons.append("housekeep: empty_research=%s focus=%s pp=%s mil_idle=%s "
                                   "construction=%s unassigned=%s" %
                                   (h.get("empty_research"), h.get("focus"), h.get("pp"),
                                    h.get("military_idle"), h.get("construction_queue"),
                                    h.get("unassigned")))
                    last_housekeep_date = cur.get("date_raw")
            now_h = cur.get("date_raw")
            if wartime_micro and now_h is not None:
                enemies = sorted(cur.get("enemies") or [])
                enemy = war_enemy or (enemies[0] if enemies else None)
                if enemy:
                    if not war_baseline:
                        try:
                            # Do not wipe a baseline the caller just reset; front_changes
                            # itself rebuilds on a new pid / older date.
                            g.front_changes(enemy, reset=False)
                        except Exception:
                            pass
                        war_baseline = True
                    due = (last_war_tick is None or
                           now_h - last_war_tick >= int(wartime_interval_days) * 24)
                    if due:
                        try:
                            fc = g.front_changes(enemy, reset=False)
                            war_changed += int(fc.get("changed_count") or 0)
                        except Exception:
                            pass
                        try:
                            war_last = g.wartime_step(enemy, max_attacks=2)
                            war_steps += 1
                        except Exception as e:
                            war_last = {"ok": False, "error": str(e)}
                        last_war_tick = now_h
                        if war_changed >= 4:
                            reasons.append("wartime front changed: %d provinces (step %s)" %
                                           (war_changed, war_steps))
                    # In wartime mode the window is counted in MICRO TICKS, not in
                    # game days: at speed 5 the game advances ~4.5 days per real
                    # second, so a game-day window would fire on every tick and
                    # end the wait after one step.
                    if war_ticks_target is not None:
                        report_every = max(1, int(wartime_report_days))
                        if war_steps and war_steps % report_every == 0:
                            reasons.append("wartime report: %d ticks, %d province changes" %
                                           (war_steps, war_changed))
                            last_war_report = now_h
                            war_changed = 0
                    elif (last_war_report is not None and war_steps and
                            now_h - last_war_report >= int(wartime_report_days) * 24):
                        reasons.append("wartime report: %d days, %d steps, %d province changes" %
                                       (int((now_h - last_war_report) / 24), war_steps, war_changed))
                        last_war_report = now_h
                        war_changed = 0
            if war_ticks_target is not None:
                # Wartime mode: the window is a number of micro ticks.
                if war_steps >= war_ticks_target:
                    reasons.append("wartime window done (%d micro ticks)" % war_steps)
            elif now_h is not None and deadline_hours is not None and now_h >= deadline_hours:
                reasons.append("time is up (%s)" % ("target date" if target_hours is not None
                                                    and now_h >= target_hours else
                                                    "%d days" % max_days))
            timed_out = (time.time() - t_start >= timeout_seconds)
            cancelled = bool(stop_event is not None and stop_event.is_set())
            if timed_out:
                reasons.append("real-time safety brake (%ds), paused to prevent background drift"
                               % int(timeout_seconds))
            if cancelled:
                reasons.append("cancelled by server watchdog, paused to prevent background drift")
            if reasons:
                self.last = cur
                want_pause = self.pause_on_wake if pause_on_wake is None else pause_on_wake
                # 2026-11-22 (复测 5 P1 §3.2): 超时或被看门狗中断时无条件暂停；
                # 2026-11-23: 命中红点门禁或和平会议开启同样无条件强制停表！
                # 2026-11-25: 宣战理由就绪/正当化结束同样无条件强制停表，防止长跑期 CB 过期！
                peace_woke = bool(cur.get("peace_open") and not prev.get("peace_open"))
                wargoal_woke = bool(
                    ("wargoals" in prev and "wargoals" in cur and (cur["wargoals"] - prev["wargoals"]))
                    or ("justifying" in prev and "justifying" in cur and (prev["justifying"] - cur["justifying"]))
                )
                if want_pause or timed_out or cancelled or red_dot_blocking or peace_woke or wargoal_woke or ap_floor_woke:
                    paused = self._force_pause_safe(g)
                else:
                    try:
                        import session
                        paused = bool(session.is_paused(g))
                    except Exception:
                        paused = None
                sp_now = {}
                try:
                    sp_now = g.game_speed() or {}
                except Exception:
                    sp_now = {}
                ret = {
                    "woke_because": reasons,
                    "timed_out": bool(timed_out or cancelled),
                    "effective_timeout_seconds": timeout_seconds,
                    "waited_game_days": round(((now_h or start_hours or 0)
                                               - (start_hours or 0)) / 24.0, 2),
                    "waited_real_seconds": round(time.time() - t_start, 1),
                    "date": (g.date().get("text") if isinstance(getattr(g, "date", None) and g.date(), dict) else None),
                    "paused": paused,
                    "game_speed": sp_now.get("speed"),
                    "game_speed_ui": sp_now.get("ui_speed"),
                    "auto_unpaused": auto_unpaused,
                    "wartime": {"steps": war_steps, "changed_since_report": war_changed,
                                "last_tick": war_last} if wartime_micro else None,
                    "state": _public(cur),
                }
                if ap_on and ap_mod is not None:
                    ap_st_ret = ap_mod.status(g)
                    ret["autopilot"] = {
                        "active": bool(ap_st_ret.get("active")),
                        "phase": ap_st_ret.get("current_phase"),
                        "ticks": ap_ticks,
                        "actions_taken": ap_actions_total,
                        "wake_triggers": ap_wake_triggers,
                        "days_elapsed": ap_st_ret.get("days_elapsed"),
                        "journal_tail": ap_st_ret.get("journal_tail", [])[-8:],
                    }
                    if ap_floor_woke:
                        ret["autopilot_woke"] = True
                if isinstance(timeout_meta, dict):
                    ret["requested_timeout_seconds"] = timeout_meta.get("requested")
                    ret["timeout_max_seconds"] = timeout_meta.get("max_s")
                    ret["timeout_clamped"] = bool(timeout_meta.get("clamped"))
                if wargoal_woke:
                    ret["wargoal_woke"] = True
                    ret["wargoals"] = sorted(cur.get("wargoals") or ())
                if sp_now.get("speed") == 4:
                    ret["speed_warning"] = (
                        "game is currently at UI speed 5 (uncapped CPU); call "
                        "hoi4_set_game_speed(speed=3) to cap at UI speed 4"
                    )
                if red_dot_blocking:
                    ret["red_dot_paused"] = True
                    ret["red_dot_codes"] = [x["code"] for x in red_dot_blocking]
                    ret["red_dots"] = red_dot_blocking
                if obsolete_unlocked:
                    ret["obsolete_lines"] = obsolete_unlocked
                return ret
            prev = cur

        # 退出 while 循环（rem_wall <= 0 或 stop_event 在 sleep 期间被触发）→ 强制停表返回
        self.last = cur
        paused = self._force_pause_safe(g)
        now_h = cur.get("date_raw")
        cancelled = bool(stop_event is not None and stop_event.is_set())
        sp_end = {}
        try:
            sp_end = g.game_speed() or {}
        except Exception:
            sp_end = {}
        reason_txt = (
            "cancelled by server watchdog, paused to prevent background drift"
            if cancelled else
            "real-time safety brake (%ds), paused to prevent background drift" % int(timeout_seconds)
        )
        out_to = {
            "woke_because": [reason_txt],
            "timed_out": True,
            "effective_timeout_seconds": timeout_seconds,
            "waited_game_days": round(((now_h or start_hours or 0)
                                       - (start_hours or 0)) / 24.0, 2),
            "waited_real_seconds": round(time.time() - t_start, 1),
            "date": (g.date().get("text") if isinstance(getattr(g, "date", None) and g.date(), dict) else None),
            "paused": paused,
            "game_speed": sp_end.get("speed"),
            "game_speed_ui": sp_end.get("ui_speed"),
            "auto_unpaused": auto_unpaused,
            "wartime": {"steps": war_steps, "changed_since_report": war_changed,
                        "last_tick": war_last} if wartime_micro else None,
            "state": _public(cur),
        }
        if isinstance(timeout_meta, dict):
            out_to["requested_timeout_seconds"] = timeout_meta.get("requested")
            out_to["timeout_max_seconds"] = timeout_meta.get("max_s")
            out_to["timeout_clamped"] = bool(timeout_meta.get("clamped"))
        if ap_on and ap_mod is not None:
            ap_st_to = ap_mod.status(g)
            out_to["autopilot"] = {
                "active": bool(ap_st_to.get("active")),
                "phase": ap_st_to.get("current_phase"),
                "ticks": ap_ticks,
                "actions_taken": ap_actions_total,
                "wake_triggers": ap_wake_triggers,
                "days_elapsed": ap_st_to.get("days_elapsed"),
                "journal_tail": ap_st_to.get("journal_tail", [])[-8:],
            }
        return out_to


def _public(s):
    """Make a snapshot JSON-friendly (frozenset -> list)."""
    out = {}
    for k, v in s.items():
        if isinstance(v, (frozenset, set)):
            out[k] = sorted(v)[:20]
            out[k + "_count"] = len(v)
        elif isinstance(v, tuple):
            out[k] = list(v)
        else:
            out[k] = v
    return out
