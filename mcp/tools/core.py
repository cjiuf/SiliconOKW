"""核心总览、进程/会话状态、时间推进、截图、存档比对、通用命令与红点门禁工具。"""
from __future__ import annotations

import offsets as O
import base64
import os
import sys

from .common import TAG, _BOOL, _INT, _STR, game, tool
from .common import (
    MAX_PAUSE_HOURS, PAUSE_COOLDOWN_S, _PAUSE_LAST,
    _CALL,
    _is_pid_alive, _looks_like_dead_read, _refuse_unpause_with_empty_orders,
    _srv, _win_hwnd, screenshot,
)

def _save_dir():
    """Where Paradox keeps HOI4 saves.  Linux uses ~/.local/share; the PE build
    writes to Documents\\Paradox Interactive\\... (OneDrive-redirected too)."""
    import sys
    if sys.platform == "win32":
        home = os.path.expanduser("~")
        for cand in (os.path.join(home, "Documents", "Paradox Interactive",
                                  "Hearts of Iron IV", "save games"),
                     os.path.join(home, "OneDrive", "Documents", "Paradox Interactive",
                                  "Hearts of Iron IV", "save games")):
            if os.path.isdir(cand):
                return cand
        return os.path.join(home, "Documents", "Paradox Interactive",
                            "Hearts of Iron IV", "save games")
    return os.path.expanduser("~/.local/share/Paradox Interactive/Hearts of Iron IV/save games")


@tool("hoi4_overview",
      "Full macro picture for the player (or a given country): date, alerts (red-dot "
      "summary), industry, political power, stability, war support, manpower, active focus, "
      "research slots, active ideas/laws, production lines, construction queue. "
      "The first thing to call each turn. "
      "`alerts` now also carries AIR_NO_MISSION / NAVY_NO_MISSION / UNASSIGNED_DIVS: "
      "if you see AIR_NO_MISSION the air force is doing NOTHING (every wing at mission 0) "
      "-- call hoi4_air_mission or hoi4_housekeep; if you see NAVY_NO_MISSION every task "
      "force is idle -- call hoi4_naval_mission. These two were ignored in every previous "
      "campaign and the Luftwaffe never flew a single sortie. "
      "Also: hoi4_overview -> hoi4_housekeep at the top of every turn covers air, navy, "
      "research, construction, production, politics, recruitment and divisions at once.", TAG)
def _overview(a):
    return game().overview(a.get("tag"))


@tool("hoi4_thresholds",
      "BANKED-RESOURCE ALARMS. Political power (warn at >=1000 -- the cap is 2000, so "
      "everything above that is thrown away) and army / navy / air experience (warn at "
      ">=100 -- one grand-doctrine or air-doctrine step costs about that). Returns the "
      "three XP pools plus `reached`, the list of thresholds currently crossed, and "
      "`spend_hint` for what that pool can actually buy. "
      "The same conditions also show up inside hoi4_overview `alerts` as PP_CAPPING and "
      "XP_READY(<service>), so the per-turn checklist sees them without an extra call.",
      TAG)
def _thresholds(a):
    return game().thresholds(a.get("tag"))


@tool("hoi4_housekeep",
      "ONE-CLICK RED-DOT CLEANUP: fill every empty research slot, fill the construction "
      "queue to >=6 factories, put idle military factories into the main infantry/artillery/"
      "tank lines, spend overflowing political power on positive decisions, fold "
      "unassigned divisions into an army with a commander, give the air wings a mission "
      "AND attach them to the armies, and give every idle fleet a Patrol mission. "
      "Every sub-step is independent and reports its own result.",
      dict(TAG, target_construction=_INT,
           technologies={"type": "array", "items": _STR,
                         "description": "Preferred research tech ids"}))
def _housekeep(a):
    return game().housekeep(a.get("tag"), a.get("target_construction"),
                            a.get("technologies") or [])


def _resolve_injections_count(p):
    """统一读取本会话累计注入次数（兼容 Proc.call_count、Backend.call_count 与 winject 进程计数）。"""
    if p is None:
        return None
    cnt = getattr(p, "call_count", None)
    if cnt is not None:
        return int(cnt)
    be = getattr(p, "be", None)
    be_cnt = getattr(be, "call_count", None) if be is not None else None
    if be_cnt is not None:
        return int(be_cnt)
    if sys.platform == "win32":
        pid = getattr(p, "pid", None)
        if pid is not None:
            try:
                from process import winject as _wj
                return int(_wj.call_count(pid))
            except Exception:
                pass
    return 0


@tool("hoi4_status", "Connection status: process, version, date, country count, "
                    "and how many ptrace/thread-hijack injections this MCP Python process "
                    "has made (`injections_scope='current_python_process'`). `date` is the "
                    "SAME dict hoi4_date returns (both call Game.date()), so the two can "
                    "never disagree -- if they look different, the value changed between "
                    "the calls, not the code path.")
def _status(a):
    g = game()
    by_caller = {}
    try:
        if hasattr(g.p, "injection_breakdown"):
            by_caller = g.p.injection_breakdown(top=12) or {}
        elif sys.platform == "win32":
            from process import winject as _wj
            by_caller = _wj.call_breakdown(getattr(g.p, "pid", None), top=12) or {}
    except Exception:
        by_caller = {}
    out = {"pid": g.p.pid, "image_base": hex(g.p.base), "version": O.VERSION,
           "date": g.date(), "player": g.player_tag(), "countries": len(g.tags()),
           "injections": _resolve_injections_count(g.p),
           "injections_scope": "current_python_process",
           "injections_by_caller": by_caller,
           "winject_recovery": _srv()._WINJECT_RECOVERY}
    try:
        sp = g.game_speed()
        out["game_speed"] = sp.get("speed")
        out["game_speed_ui"] = sp.get("ui_speed")
    except Exception:
        pass
    try:
        import session as _sess
        out["paused"] = _sess.is_paused(g)
    except Exception:
        pass
    # 2026-11-19: a minimised window freezes the simulation (5 h wall = 0 game
    # days measured).  Surface it here so no observation is made blind again.
    try:
        import windowstate
        out["window"] = windowstate.state(g.p.pid)
    except Exception as e:
        out["window"] = {"error": repr(e)}
    if sys.platform == "win32":
        try:
            import profile as _prof
            out["profile"] = _prof.summary()
            out["profile_live"] = _prof.verify_live_process(g.p)
        except Exception as e:
            out["profile"] = {"error": repr(e)}
        try:
            import assets as _assets
            out["assets"] = _assets.summary()
        except Exception as e:
            out["assets"] = {"error": repr(e)}
    try:
        import reddots as _rd
        out["red_dots"] = _rd.summary(g)
    except Exception as e:
        out["red_dots"] = {"error": repr(e)}
    return out


@tool("hoi4_get_country",
      "Summary of one country (factories, political power, stability, war support, "
      "manpower...).",
      {"tag": _STR}, ["tag"])
def _get_country(a):
    g = game()
    with g:
        return g.country(a["tag"]).summary()


@tool("hoi4_list_countries", "Country tags; only_majors=true returns a summary of the major powers.",
      {"only_majors": _BOOL, "limit": _INT})
def _list_countries(a):
    g = game()
    if a.get("only_majors"):
        majors = ["GER", "ENG", "SOV", "USA", "JAP", "ITA", "FRA"]
        with g:
            return {t: g.country(t).summary() for t in majors if t in g.tags()}
    tags = g.tags()
    return {"count": len(tags), "tags": tags[:a.get("limit")] if a.get("limit") else tags}


@tool("hoi4_get_modifiers",
      "Every modifier currently active on the country (national spirits, ideas, tech "
      "bonuses). `totals` is the flat sum; `by_source` attributes each modifier to the "
      "national spirit / law that carries it (read from each active CIdea's own modifier "
      "list); `unattributed` lists the ones that come from tech, focus effects, "
      "occupation or difficulty, which are not walked here.", TAG)
def _mods(a):
    c = game().country(a.get("tag") or game().player_tag())
    tot = c.modifiers()
    try:
        src = c.modifier_sources()
    except Exception as e:
        return {"totals": tot, "by_source_error": repr(e)}
    # 2026-11-21 (P1-4): `unattributed` only listed keys ABSENT from by_source,
    # so a key that was only PARTLY explained never showed up.  Measured live:
    #   production_speed_arms_factory_factor  totals 0.35  war_economy 0.20
    #   track_mastery_gain_factor             totals 0.05  four sources = 0.20
    #   unit_motorized_design_cost_factor     in by_source, MISSING from totals
    # Report the arithmetic instead of hiding it: per-key source sum, residual,
    # the partially-attributed list, and keys that exist only in by_source.
    sums, residual = {}, {}
    for k, rows in (src or {}).items():
        try:
            sums[k] = round(sum(float(r.get("value") or 0) for r in rows), 6)
        except Exception:
            continue
    for k, v in tot.items():
        if k not in sums:
            continue
        try:
            d = round(float(v) - sums[k], 6)
        except Exception:
            continue
        if abs(d) > 1e-9:
            residual[k] = d
    return {"totals": tot, "by_source": src, "by_source_sum": sums,
            "residual": residual,
            "unattributed": sorted(k for k in tot if k not in src),
            "partially_attributed": sorted(residual),
            "by_source_only": sorted(k for k in src if k not in tot),
            "note": ("residual = totals - sum(by_source); a non-zero residual is a "
                     "modifier that comes from tech / focus / occupation / "
                     "difficulty, which this reader does not walk.")}


@tool("hoi4_date", "Current in-game date.")
def _date(a):
    return game().date()


@tool("hoi4_set_game_speed",
      "Set game speed (0 = slowest / UI 1-bar .. 3 = UI 4-bar, which is the tool hard cap; "
      "4 and 5 are clamped to engine 3 / UI speed 4 because UI speed 5 runs uncapped on CPU "
      "and overshoots red-dot brakes). Writes CCurrentGameState+0x4BC directly "
      "(0-injection, works while paused or backgrounded) and verifies by readback. "
      "When running (unpaused), also samples live clock rate (~2.2s) for "
      "`measured_rate` (days/sec); pass `measure_seconds` (0..20) to override.",
      {"speed": _INT,
       "measure_seconds": {"type": "number",
                           "description": "Optional clock sampling seconds (0 = skip, "
                                          ">0 = sample live days/sec; if paused, briefly "
                                          "unpauses and restores pause)."},
       "use_command": dict(_BOOL, description="Optional: send CSetGameSpeedCommand instead of direct write.")},
      ["speed"])
def _speed(a):
    return game().set_game_speed(
        a["speed"],
        measure_seconds=a.get("measure_seconds"),
        use_command=bool(a.get("use_command", False)),
    )


@tool("hoi4_pause_in_hours",
      "Run for N in-game hours, then pause. For turn-based play. Hard cap 300 hours "
      "per call, and a 20 s cooldown is enforced after every call (both limits exist "
      "because repeated use crashed the game on 2026-11-17).",
      {"hours": _INT}, ["hours"])
def _pause(a):
    """Run N in-game hours, then pause — WITHOUT the debug console.

    2026-11-17: this used to be `g.console("pause_in_hours N")`. That is a
    developer-console command, which the campaign brief forbids, and the game
    crashed on the 1938.06.01 monthly tick after it had been used repeatedly
    (RVA 0xB2E62E, see research §91.5). It is now a plain wall-clock poll on the
    date field plus `session.pause()` (a direct idler+0x6C1 write), so nothing
    foreign is executed inside hoi4.exe.

    2026-11-18: the "single call <= 300 h, wait 20 s after a timeout" rule used
    to be an informal guideline that a model could forget. It is enforced here.
    """
    import time as _t
    import session as _session
    g = game()
    hours = int(a["hours"])
    if hours <= 0:
        return {"result": {"ok": False, "posted": 0, "hours_requested": hours,
                           "error": "hours must be > 0"}, "date": g.date()}
    if hours > MAX_PAUSE_HOURS:
        return {"result": {"ok": False, "posted": 0, "hours_requested": hours,
                           "max_hours": MAX_PAUSE_HOURS,
                           "error": ("hours=%d is over the hard cap of %d per call. "
                                     "Split it into several calls; the cap and the "
                                     "20 s cooldown exist because repeated "
                                     "pause_in_hours calls crashed the game twice on "
                                     "2026-11-17." % (hours, MAX_PAUSE_HOURS))},
                "date": g.date()}
    waited = 0.0
    gap = _t.time() - _PAUSE_LAST[0]
    if gap < PAUSE_COOLDOWN_S:
        waited = round(PAUSE_COOLDOWN_S - gap, 1)
        _t.sleep(waited)
    start = int(g.date()["raw"])
    deadline = start + hours
    _bad = _refuse_unpause_with_empty_orders(g)
    if _bad:
        return {"result": dict(_bad, hours_requested=hours, posted=0),
                "date": g.date()}
    try:
        import reddots as _rd
        _rd_gate = _rd.check_unpause_gate(g, ensure_paused=True)
        if _rd_gate:
            return {"result": dict(_rd_gate, hours_requested=hours, posted=0),
                    "date": g.date()}
    except Exception:
        pass
    ok_un, msg_un = _session.pause(g, False)
    if not ok_un:
        # 2026-11-21 (P0-3): resume can legitimately fail while a modal popup holds
        # the game (the pause byte is 0 but the clock stays frozen).  Say so at
        # once instead of polling a clock that cannot move for 10 s and then
        # reporting "hours_run: 0" as if the run had happened.
        return {"result": {"ok": False, "posted": 0, "hours_requested": hours,
                           "unpause": msg_un,
                           "error": ("could not resume the game: %s"
                                     % (msg_un or "unknown reason"))},
                "date": g.date()}
    t0 = _t.time()
    cur, prev = start, start
    stalled = 0
    # 2026-11-17 rewrite, three problems with the old loop:
    #   * it polled once per SECOND for up to 600 s and held the whole MCP server
    #     hostage the entire time (every other tool timed out);
    #   * `hours` never mapped to game hours -- a single 1-hour poll could span
    #     anything from 1 hour to 37 days depending on the speed setting;
    #   * it could leave the game UNPAUSED (`pause()` returned
    #     "wrote idler+0x6C1 but it reads False") and there was no retry.
    # Now: 0.25 s poll, 120 s wall cap, stall detector, and re-pause is retried.
    # 105 s, not 120: the MCP client gives up at ~110 s and then the call keeps
    # running server-side, so a 120 s cap guarantees a -32001 on every long run
    # and the next write lands on top of a still-running one.
    # 2026-11-21 (P1-1): the 2026-11-20 playtest measured the client abandoning
    # calls after ~60 s, so 105 s still produced -32001 on every long run.  Cap
    # well inside that budget and return a usable partial result instead: the
    # caller can simply call again, and a running write never outlives its reply.
    WALL_CAP = 45.0
    STALL_POLLS = 40          # 10 s with zero game time -> something is blocking
    read_err = None
    while _t.time() - t0 < WALL_CAP:
        try:
            cur = int(g.date()["raw"])
        except Exception as _re:
            read_err = repr(_re)
            break
        if cur >= deadline:
            break
        stalled = stalled + 1 if cur == prev else 0
        prev = cur
        if stalled >= STALL_POLLS:
            break
        _t.sleep(0.25)
    ok_p, msg_p = False, "not attempted"
    pid_now = getattr(getattr(g, "p", None), "pid", None)
    if _is_pid_alive(pid_now):
        for _ in range(3):
            try:
                ok_p, msg_p = _session.pause(g, True)
            except Exception as e:
                ok_p, msg_p = False, repr(e)
            if ok_p or not _is_pid_alive(pid_now):
                break
            _t.sleep(1.0)
    else:
        msg_p = read_err or ("process pid=%s has terminated" % pid_now)
    # stamp the cooldown from the END of the run: the next call must not post a
    # write on top of a call the client already gave up on
    _PAUSE_LAST[0] = _t.time()
    _dt = cur - start
    out = {"ok": bool(ok_p), "hours_requested": hours,
           "hours_run": _dt,
           "days_run": round(_dt / 24.0, 2),
           "unpause": msg_un, "pause": msg_p,
           "waited_for_cooldown_s": waited,
           "wall_cap_hit": _t.time() - t0 >= WALL_CAP,
           "stalled": stalled >= STALL_POLLS}
    if read_err:
        out["read_error"] = read_err
    # 2026-11-19: the date address can change MID-CALL (measured: -499 h and
    # +948 h on a 300 h request).  When that happens the delta is meaningless,
    # so say so instead of reporting a negative or 3x number as if it were real.
    if _dt < 0 or (hours and _dt > hours * 8):
        out["clock_unreliable"] = True
        out["hours_run"] = None
        out["days_run"] = None
        out["hint"] = ("the in-game clock JUMPED during this call (%d -> %d, "
                       "delta %s h on a %d h request): the date address drifted. "
                       "Treat this call as 'advanced an unknown amount' and read "
                       "hoi4_date.source / log_raw before trusting any date."
                       % (start, cur, _dt, hours))
    if out["hours_run"] is not None and out["hours_run"] < hours and not out["stalled"]:
        out.setdefault("hint", ("only %d of %d hours elapsed (wall cap %.0f s). The clock is "
                                "fine -- call again for the rest; the 20 s cooldown is "
                                "automatic." % (out["hours_run"], hours, WALL_CAP)))
    # 2026-11-19: a MINIMISED window freezes the simulation outright -- measured
    # 5 h wall clock = 0 game days, then 100 s after restoring = 47 game days.
    # Several "nothing happened" observations in this project were that, not the
    # engine, so every clock run now reports where the window is.
    try:
        import windowstate
        out["window"] = windowstate.state(pid_now)
    except Exception as _we:
        out["window"] = {"error": repr(_we)}

    # 2026-11-23 (复测 9 E-4): 优先检测进程是否已崩溃/终止（OSError 299 / pid 不再存活 / hwnd=null），
    # 严禁在进程已死时误报“大概是事件弹窗”！
    _win = out.get("window") or {}
    proc_dead = (
        (not _is_pid_alive(pid_now))
        or (
            (_looks_like_dead_read(read_err) or _looks_like_dead_read(msg_p))
            and _win.get("hwnd") is None
        )
    )
    if proc_dead:
        out["ok"] = False
        out["process_dead"] = True
        out["blocked_by"] = "process_dead"
        out["error"] = "hoi4.exe (pid=%s) has terminated or crashed during hoi4_pause_in_hours" % pid_now
        out["hint"] = (
            "HOI4 进程（pid=%s）已退出/崩溃（并非事件弹窗阻塞）。"
            "请检查 Documents/Paradox Interactive/Hearts of Iron IV/crashes 或 Windows 事件日志，"
            "重启游戏并加载最近存档。" % pid_now
        )
        return {"result": out, "date": None}

    if stalled >= STALL_POLLS:
        if _win.get("minimized") or _win.get("visible") is False:
            out["blocked_by"] = ("window_minimized" if _win.get("minimized")
                                 else "window_hidden")
            out["hint"] = ("the HOI4 window is %s -- a minimised/hidden window freezes "
                           "the simulation completely (measured: 5 h wall clock = 0 "
                           "game days, 100 s after restoring = 47 game days). Restore "
                           "the window and call again."
                           % ("minimised" if _win.get("minimized") else "hidden"))
        else:
            out["hint"] = ("the clock did not move for 10 s -- a World-News / event "
                           "dialog is almost certainly open and is holding the clock "
                           "(window focused=%s title=%r). Read `pending(g)` and "
                           "answer it in game."
                           % (_win.get("focused"), _win.get("title")))
    if not ok_p:
        out["hint"] = (out.get("hint", "") +
                       " WARNING: the game may still be RUNNING unpaused.")
    try:
        date_now = g.date()
    except Exception:
        date_now = None
    return {"result": out, "date": date_now}


@tool("hoi4_screenshot",
      "Screenshot, for visual confirmation. Does NOT raise or focus the game window by "
      "default, so it never interrupts the player. focus=true brings the window forward. "
      "crop example: '620x30+0+0'. An UNCROPPED shot is downscaled to <=1600 px wide and "
      "<=1.8 MB so the client can always render it; pass crop when you need to read small "
      "text (a crop keeps full resolution).",
      {"crop": _STR, "focus": _BOOL})
def _shot(a):
    data = screenshot(a.get("crop"), a.get("focus", False))
    out = {"_image": base64.b64encode(data).decode(), "bytes": len(data)}
    try:
        from PIL import Image as _Im
        import io as _io
        with _Im.open(_io.BytesIO(data)) as _im:
            out["width"], out["height"] = _im.width, _im.height
    except Exception:
        pass
    out["cropped"] = bool(a.get("crop"))
    # 2026-11-21 (P2-6): a minimised window has a 160x30 "iconic" rect, so the
    # capture is a black strip of a few hundred bytes.  Say that instead of
    # handing the caller a picture that looks like a black screen.
    try:
        import ctypes as _ct
        _hwnd = _win_hwnd()
        out["minimized"] = bool(_hwnd and _ct.windll.user32.IsIconic(_hwnd))
    except Exception:
        out["minimized"] = None
    if out.get("minimized"):
        out["hint"] = ("游戏窗口已最小化：Windows 只能截到窗口矩形，所以拿到的是黑条。"
                       "把 HOI4 恢复（点任务栏）后再截图；focus=true 也会恢复窗口，"
                       "但那会把游戏提到前台，默认不做。")
    elif out.get("bytes", 0) < 2000 and not a.get("crop"):
        out["hint"] = ("截图只有 %d 字节，基本是黑屏：窗口可能被别的程序挡住或刚切走。"
                       % out.get("bytes", 0))
    return out


@tool("hoi4_full_report",
      "The wide turn-start report: overview + resources + statistics + armies + division "
      "templates + fleets + command power + selectable focuses + faction.", TAG)
def _full(a):
    return game().full_report(a.get("tag"))


@tool("hoi4_country_stats",
      "Country summary plus the live checks that are cheap and exact: factories, PP, "
      "stability, war support, manpower, divisions, at_war, is_major. On the Windows "
      "build the old ~44 trigger statistics (casualties / surrender_progress / "
      "convoy_threat ...) are NOT available -- the field `unavailable` says which, and "
      "`source` says why. A missing field never means zero.",
      dict(TAG, raw=_BOOL))
def _cstats(a):
    return game().country_stats(a.get("tag"), a.get("raw", False))


@tool("hoi4_script_variable",
      "Read ONE dynamic variable by name (e.g. 'command_power', 'manpower_k', "
      "'political_power'). Returns `known` (is the name in the engine's own "
      "registry), `value` when this build has a native reader for it, and "
      "`similar` names when the name does not exist. Call hoi4_dynamic_variables "
      "for the full authoritative list.\n\n"
      "Some names take an @argument and answer 0 WITHOUT it: "
      "'num_equipment_in_armies@infantry_equipment' (equipment held by divisions), "
      "'num_equipment@infantry_equipment' (in the stockpile), "
      "'num_of_civilian_factories_in_cores@GER' (my core states controlled by GER). "
      "An archetype name sums all its variants; an unknown name answers 0, exactly "
      "like the engine.\n\n"
      "via='native' (default) reads pure memory. via='console' additionally falls "
      "back to the developer command `get_var`, which reaches variables that have "
      "no native reader -- but that is a CHEAT path, use it only in tests, and "
      "never for pc_total_score/pc_current_score (they assert outside a peace "
      "conference and kill the process).",
      dict(TAG, name=_STR,
           via=dict(_STR, description="native (default) | console (cheat fallback)")),
      ["name"])
def _svar(a):
    return game().script_variable(a["name"], a.get("tag"), a.get("via") or "native")


@tool("hoi4_dynamic_variables",
      "Every dynamic variable the engine's own registry knows (265 names on "
      "1.19.5.0 PE), read live from the static registry at RVA 0x33DF1D0. "
      "`native` = the subset hoi4_script_variable can return a value for. "
      "Use this instead of guessing a name: the registry is authoritative.",
      TAG)
def _dynvars(a):
    return game().dynamic_variables()


@tool("hoi4_command_help",
      "Spell out the PARAMETERS of a player command: which offset, which type, which object "
      "it expects and where to get the value. Call this before hoi4_send_command.",
      {"name": _STR}, ["name"])
def _chelp(a):
    return game().command_help(a["name"])


@tool("hoi4_player_commands",
      "Every action a player can take (370 CCommand classes) with their field layouts. "
      "Executed through hoi4_send_command. Examples: CDiplomaticActionCommand, "
      "COrderNewFrontCommand, CCreateDivisionTemplateCommand, CDeployAirWingCommand...",
      {"filter": _STR})
def _pcmds(a):
    return game().player_commands(a.get("filter", ""))


@tool("hoi4_send_command",
      "Send ANY player command. Not a cheat: it goes into the game's own command queue. name: "
      "a class from hoi4_player_commands. fields: {'arg0': '<value>', ...}. Values may be "
      "given BY NAME: 'focus:GER_rhineland', 'tech:interwar_artillery', 'idea:war_economy', "
      "'decision:xxx', 'country:FRA', 'state:51', 'building:arms_factory', "
      "'equipment:infantry_equipment_1', 'character:GER_erwin_rommel', 'template:0', "
      "'army:3', 'fleet:0', 'subunit:infantry', 'wargoal:take_state', 'ptr:0x...' or a plain "
      "number. Call hoi4_command_help first to learn the parameters.",
      dict(TAG, name=_STR, fields={"type": "object"}, force=_BOOL), ["name"])
def _sendcmd(a):
    return game().send_command(a["name"], a.get("fields") or {}, a.get("tag"),
                               a.get("force", False))


@tool("hoi4_handle",
      "The in-game handle ({type,id}) of an object. Army and general commands take handles "
      "rather than pointers. kind: character|division (division accepts index or 0x... ptr).",
      {"kind": _STR, "key": _STR, "tag": _STR}, ["kind", "key"])
def _handle(a):
    g = game()
    kind = str(a.get("kind") or "").strip().lower()
    key = a.get("key")
    try:
        if kind == "character":
            return g.character_handle(key, a.get("tag"))
        if kind == "division":
            return g.division_handle(key, a.get("tag"))
        return {"ok": False, "error": "unsupported kind %r (expected 'character' or 'division')" % kind}
    except (ValueError, IndexError, KeyError) as e:
        return {"ok": False, "kind": kind, "key": key, "error": str(e)}


@tool("hoi4_country_subsystems",
      "Every subsystem of a country (research, production, politics, diplomacy, intelligence, "
      "occupation, operations, projects...) with class names and the arrays inside them. The "
      "starting point for exploring an unwrapped mechanic; continue with hoi4_browse_object.", TAG)
def _subsys(a):
    return game().country_subsystems(a.get("tag"))


@tool("hoi4_list_database",
      "List one of the game's item databases: war_goals, occupation_laws, buildings, "
      "technologies, ideas, decisions, equipment, focuses, character_templates, "
      "scripted_diplomatic_actions.",
      {"which": _STR, "filter": _STR, "limit": _INT}, ["which"])
def _lsdb(a):
    return game().list_database(a["which"], a.get("filter", ""), a.get("limit", 200))


@tool("hoi4_browse_object",
      "ADVANCED: dump the fields of an unknown game object with class names and strings. For "
      "discovering new mechanics.",
      {"ptr": _STR, "span": _INT}, ["ptr"])
def _browse(a):
    return game().browse_object(a["ptr"], a.get("span", 0x100))


@tool("hoi4_save_compare",
      "Compare countries FROM A SAVE FILE, without touching the running game. Parses "
      "ironman/binary saves with the game's own token table. Splits factories into CORE and "
      "OCCUPIED, because factories in occupied states only work in proportion to compliance "
      "and the raw total is misleading. Without save, the newest one is used.",
      {"tags": {"type": "array", "items": _STR, "description": "Karsilastirilacak tag'ler"},
       "save": {"type": "string", "description": "File name (optional)"}},
      ["tags"])
def _save_compare(a):
    import glob, os, savereader
    S = _save_dir()
    if a.get("save"):
        path = a["save"] if os.path.isabs(a["save"]) else os.path.join(S, a["save"])
    else:
        files = sorted(glob.glob(S + "/*.hoi4"), key=os.path.getmtime, reverse=True)
        if not files:
            return {"ok": False, "error": "save not found"}
        path = files[0]
    tokens = savereader.load_tokens()
    if not tokens:
        try:
            savereader.dump_tokens(game())
            tokens = savereader.load_tokens()
        except Exception:
            return {"ok": False, "error": "no token table yet; the game must have been open at least once"}
    return {"save": os.path.basename(path), "meta": savereader.meta(path, tokens),
            "countries": savereader.compare(path, [t.upper() for t in a["tags"]], tokens)}


@tool("hoi4_save_list", "Save files and their metadata (player, ideology, date, version).")
def _save_list(a):
    import glob, os, savereader
    S = _save_dir()
    tokens = savereader.load_tokens()
    out = []
    for f in sorted(glob.glob(S + "/*.hoi4"), key=os.path.getmtime, reverse=True)[:12]:
        try:
            rec = {"file": os.path.basename(f),
                   "mb": round(os.path.getsize(f) / 1e6, 1)}
            m = savereader.meta(f, tokens)
            # 2026-11-16: the header stores HOURS with a 5000-year offset, so the
            # tool used to print the raw integer (60795817). Decode it.
            if "date" in m:
                rec["date_raw"] = m["date"]
                m["date"] = savereader.decode_date(m["date"], 43800000) or m["date"]
            rec.update(m)
            out.append(rec)
        except Exception as e:
            out.append({"file": os.path.basename(f), "error": str(e)})
    return {"count": len(out), "saves": out}


@tool("hoi4_session_info",
      "SESSION SUMMARY. Call this first: which country you are playing (my_tag/my_country), "
      "single or multiplayer (is_multiplayer, player_count, players), whether you are at war, "
      "active land combats, date.")
def _session_info(a):
    import session
    return session.info(game())


@tool("hoi4_pause",
      "Pause or resume -- the same state change the player's SPACE key makes. On the "
      "Windows build this is the engine's own pause byte (idler+0x6C1), written the way "
      "the game's SPACE handler writes it; on Linux it goes through CPauseGame. The "
      "clock is sampled before and after (a paused game must show two identical "
      "date_raw() reads), so success is never judged from the flag alone. "
      "When resuming (paused=false), the red-dot gate blocks unpause if unresolved "
      "events/focus/research/PP>=500/XP>=100/idle factories/new decisions exist; "
      "resolve them first or pass ack_red_dots=[{code, reason, snooze_days, force}].",
      {"paused": _BOOL, "who": _STR,
       "ack_red_dots": {"type": "array",
                        "description": "Optional red-dot acknowledgements to apply before resuming",
                        "items": {"type": "object"}}})
def _pause_game(a):
    import session
    import sys as _sys
    g = game()
    if not a.get("paused", True):
        bad = _refuse_unpause_with_empty_orders(g)
        if bad:
            return bad
        try:
            import reddots as _rd
            gate = _rd.check_unpause_gate(
                g,
                ack_red_dots=a.get("ack_red_dots"),
                ensure_paused=True,
            )
            if gate:
                return gate
        except Exception:
            pass
    ok, msg = session.pause(g, a.get("paused", True), a.get("who"))
    # 2026-11-21 (P3-1 / P3-2): `{"ok": true, "error": "already paused"}` put a
    # success flag next to an error string, and the method (a direct write to
    # idler+0x6C1, not CPauseGame) was buried in that same string.  Separate the
    # three facts: did the call succeed, did anything CHANGE, and how was it done.
    # 2026-11-21 (second pass): `changed` was `ok and not msg`, but every path
    # returns a message, so it was ALWAYS false even when the byte really flipped.
    # "already ..." is the only message that means nothing changed.
    _m = str(msg or "")
    out = {"ok": bool(ok), "changed": bool(ok and not _m.startswith("already")),
           "requested": bool(a.get("paused", True))}
    if msg:
        if str(msg).startswith("already "):
            out["note"] = str(msg)          # a no-op, not a failure
        elif str(msg).startswith("unverified"):
            # 2026-11-21 (P0-3): the byte is set but this process's date reader is
            # pinned to game.log, so no clock witness exists.  Say that instead of
            # either claiming a popup or silently returning a bare ok.
            out["note"] = str(msg)
            out["clock_verified"] = False
        else:
            out["warning"] = str(msg)
    if ok and not msg:
        out["clock_verified"] = True
    out["method"] = ("idler+0x6C1 (the byte the SPACE key toggles)"
                     if _sys.platform == "win32" else "CPauseGame")
    if _sys.platform == "win32":
        out["multiplayer_note"] = ("Windows PE: pause and resume are the single byte "
                                   "idler+0x6C1, written exactly the way the game's own "
                                   "SPACE handler writes it (hoi4.exe+0xE48C36: "
                                   "'cmp [rbx+0x6C1],0 / sete al / mov [rbx+0x6C1],al'). "
                                   "Both directions verified on 1.19.5.0 PE (current pid %s): "
                                   "1 freezes the clock, 0 lets it run. Success is judged by the "
                                   "clock, not the flag, so a frozen clock with the flag "
                                   "already 0 is reported as a second pause source (event "
                                   "popup / menu) instead of a fake success; when the "
                                   "process's date reader is itself stuck on game.log the "
                                   "reply carries clock_verified:false rather than inventing "
                                   "either verdict. In a real multiplayer session the host "
                                   "owns this state." % getattr(getattr(g, "p", None), "pid", "?"))
        if (not ok) and (not a.get("paused", True)):
            # 2026-11-21 (P0-3 completeness): the flag is 0 but the clock is frozen.
            # That is the ONE failure mode the byte cannot clear, because something
            # else is holding the pause: a country-event popup, a mod's news window
            # or an open menu.  Name it instead of leaving a bare warning, and point
            # at the tool that can actually answer it.
            out["blocked_by"] = "modal_window"
            try:
                import events_win
                _pend = events_win.pending(g)
            except Exception:
                _pend = []
            if _pend:
                out["blocked_by"] = "event_popup"
                out["pending_events"] = [r.get("event_id") for r in _pend][:8]
                out["hint"] = ("有 %d 个事件弹窗待回答，游戏被它们自己暂停了（不是玩家暂停）。"
                               "用 hoi4_events 查看，再用 hoi4_select_event_option(write=true) 回答。"
                               % len(_pend))
            else:
                out["hint"] = ("暂停字节已经是 0，但时钟仍不动：屏幕上有一个模态窗口（新闻/"
                               "事件弹窗或菜单）压着游戏。它不在 CGameState 的待回答事件数组里，"
                               "工具无法代答——请关掉这个窗口，然后重新调用 hoi4_pause(false)。")
    return out


@tool("hoi4_world_snapshot",
      "Current value of every watched signal: focus, research slots, available decisions, "
      "idle military factories, construction queue, divisions ready to deploy, wars and "
      "battles, political power. PURE READS. Never stops the game, no desync risk in "
      "multiplayer. hoi4_wait_for compares these snapshots to work out why it woke up.")
def _world_snapshot(a):
    import turnloop
    return turnloop._public(turnloop.snapshot(game()))


@tool("hoi4_wait_for",
      "SLEEP AND WAKE AT THE RIGHT MOMENT. Say 'do not disturb me until X' and this watches "
      "the game and returns when something happens, instead of burning tokens checking every "
      "second. Wakes on: focus completed, research finished or a slot freed, a new decision "
      "became available, military factories went idle, construction queue emptied, a division "
      "is ready to deploy, WAR was declared, a new battle started, political power threshold. "
      "Even if none of that happens it ALWAYS returns when max_days runs out, there is no "
      "path that waits forever. If the game is paused, timeout_seconds is the real-time "
      "safety brake. The reply says what woke it and the current state.",
      {"max_days": {"type": "integer",
                    "description": "Wake after at most this many IN-GAME days (default 7)"},
       "until_date": {"type": "array", "items": {"type": "integer"},
                      "description": "[year, month, day]. Sleep until this date"},
       "signals": {"type": "array", "items": {"type": "string"},
                   "description": "Signals to watch: focus, research, decisions, events, production, "
                                  "construction, deployment, war (war declared/ended), peace (peace conference opened), "
                                  "wargoal (new CB available or justification ended/lost; always force-pauses), "
                                  "battles, political_power, housekeep. Pass [] to wait purely until max_days / until_date "
                                  "(no signal wakes except red dots if respect_red_dots=true)."},
       "wake_on_battle": {"type": "boolean",
                          "description": "Wake on every new individual land battle (default false; "
                                         "war declaration/end still wakes under 'war')"},
       "political_power": {"type": "integer",
                           "description": "Wake when political power reaches this value"},
       "poll_seconds": {"type": "number", "description": "Poll interval in seconds (default 1.5; auto-tightened near max_days to avoid overshoot)"},
       "timeout_seconds": {"type": "integer",
                           "description": "Real-time safety brake in seconds, clamped to min(requested, HOI4_WAIT_MAX_S) "
                                          "(default 26s, shared with hoi4_wait_and_brief; set HOI4_WAIT_MAX_S=38..90 for longer clients). "
                                          "Actual effective value is echoed in effective_timeout_seconds."},
       "pause_on_wake": {"type": "boolean",
                         "description": "Pause on wake (timeout, red dots, peace conference, and wargoal/CB always pause unconditionally)"},
       "wartime_micro": {"type": "boolean",
                         "description": "While at war, run one local wartime_step every 3 game "
                                        "days without waking the model; only report after >=4 "
                                        "province changes or 15 days"},
       "wartime_enemy": {"type": "string",
                         "description": "Enemy tag for wartime_micro (empty = auto-detect)"},
       "wartime_interval_days": {"type": "integer",
                                 "description": "Game days between wartime micro ticks (default 3)"},
       "wartime_report_days": {"type": "integer",
                               "description": "Force a report after this many war days (default 15)"},
       "respect_red_dots": {"type": "boolean",
                            "description": "Enforce red-dot gate & auto-pause during this wait (default true; also bypassed globally when hoi4_red_dot_guard enabled=false)"},
       "autopilot": {"type": "boolean",
                     "description": "Enable Standing Orders autopilot during wait (omit = auto when active; false = bypass)"},
       "fast_forward": {"type": "boolean",
                        "description": "Safely set speed <= 3 (UI 4-bar cap) and unpause after pre-run checks"}})
def _wait_for(a):
    import turnloop
    tl = turnloop.TurnLoop(game(), poll=a.get("poll_seconds", 1.5))
    until = a.get("until_date")
    tm = turnloop.resolve_wait_timeout(requested=a.get("timeout_seconds"))
    # 2026-11-23 (复测 6 P1-3): 显式传 signals=[] 必须保持空元组 ()，
    # 不可用 `a.get("signals") or ALL_SIGNALS` 把空列表 falsy 吞回全量信号。
    _raw_sigs = a.get("signals")
    _signals = turnloop.ALL_SIGNALS if _raw_sigs is None else tuple(_raw_sigs)
    return tl.wait(max_days=a.get("max_days", 7),
                   until_date=tuple(until) if until else None,
                   signals=_signals,
                   pp_threshold=a.get("political_power"),
                   timeout_seconds=tm["effective"],
                   pause_on_wake=a.get("pause_on_wake", False),
                   wartime_micro=a.get("wartime_micro", False),
                   wartime_enemy=a.get("wartime_enemy"),
                   wartime_interval_days=a.get("wartime_interval_days", 3),
                   wartime_report_days=a.get("wartime_report_days", 15),
                   stop_event=_CALL.get("cancel"),
                   respect_red_dots=bool(a.get("respect_red_dots", True)),
                   wake_on_battle=bool(a.get("wake_on_battle", False)),
                   timeout_meta=tm,
                   autopilot=a.get("autopilot"),
                   fast_forward=bool(a.get("fast_forward", False)))


@tool("hoi4_red_dots",
      "Inspect all active red-dot gate blockers (pending events, new decisions/missions, "
      "empty focus/research, idle civ/mil/dockyard, PP>=500, Army/Navy/Air XP>=100, "
      "armies without general, unassigned divisions, surplus-no-training / deficit-over-training, "
      "wartime idle air/navy, fuel zero, high-payback obsolete production lines), "
      "currently snoozed acknowledgements, advisories, and background brake guard status.",
      {"fast": _BOOL})
def _red_dots(a):
    import reddots as _rd
    return _rd.evaluate(game(), fast=bool(a.get("fast", False)))


@tool("hoi4_ack_red_dot",
      "Explicitly acknowledge/snooze a red-dot gate blocker with a concrete strategic reason "
      "so the clock can resume. Strict items (PENDING_EVENT, NO_FOCUS, IDLE_RESEARCH) refuse "
      "normal ack and require force=true (max 7 game days). Other items snooze for 30 days by "
      "default and automatically re-arm if values escalate to the next tier or new objects appear.",
      {"code": {"type": "string", "description": "Red-dot code, e.g. PP_CAP / ARMY_XP_CAP / NEW_DECISION"},
       "reason": {"type": "string", "description": "Required strategic reason for deferring this red dot"},
       "snooze_days": {"type": "integer", "description": "Game days to snooze (default 30; strict items capped at 7)"},
       "force": {"type": "boolean", "description": "Required true to force-snooze strict items (PENDING_EVENT/NO_FOCUS/IDLE_RESEARCH)"}},
      ["code", "reason"])
def _ack_red_dot(a):
    import reddots as _rd
    return _rd.ack(
        game(),
        code=a["code"],
        reason=a["reason"],
        snooze_days=a.get("snooze_days"),
        force=bool(a.get("force", False)),
    )


@tool("hoi4_red_dot_guard",
      "Inspect or configure the red-dot gatekeeper and background read-only brake guard. "
      "`enabled` is the master switch for ALL blocking gates (unpause gate, wait_for/wait_and_brief "
      "polling gate, and background brake thread) without affecting read-only hoi4_red_dots inspection; "
      "`guard_enabled` toggles only the background brake thread; per-call `respect_red_dots=false` on "
      "hoi4_wait_for/hoi4_wait_and_brief overrides only that wait.",
      {"enabled": {"type": "boolean", "description": "Master switch for all red-dot unpause/wait/background blocking (hoi4_red_dots read-only scan remains active)"},
       "guard_enabled": {"type": "boolean", "description": "Enable/disable only the background auto-pause thread"},
       "peace_air_navy_block": {"type": "boolean", "description": "Also block peacetime idle air/navy (default false: wartime only)"},
       "guard_poll_seconds": {"type": "number", "description": "Background guard poll interval in seconds (default 1.5)"}})
def _red_dot_guard(a):
    import reddots as _rd
    cfg_updates = {}
    for k in ("enabled", "guard_enabled", "peace_air_navy_block", "guard_poll_seconds"):
        if k in a and a[k] is not None:
            cfg_updates[k] = a[k]
    if cfg_updates:
        _rd.configure(**cfg_updates)
    if a.get("guard_enabled") is False:
        _rd.stop_guard()
    elif a.get("guard_enabled") is True:
        _rd.start_guard(lambda: _srv()._g, busy_check=lambda: bool(_CALL.get("name")))
    return {"ok": True, "guard": _rd.guard_status()}
