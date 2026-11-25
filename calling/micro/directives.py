"""前线战区指令 JSON 持久化、单次战区 Tick 与 frontd 对接。"""
from __future__ import annotations

import json as _json
import os as _os
import sys
import threading as _threading
import time

import session_state as _SS

FRONT_STATE_PATH = _SS.state_path(_SS.FRONT_DIRECTIVES_NAME)
FRONT_STANCES = ("prepare", "hold", "balanced_attack", "blitzkrieg")
TICK_HOURS = 24                      # 1 game day — run 5: 48 h was too slow to plug
_STATE_LOCK = _threading.RLock()


def _front_state_path() -> str:
    """优先使用 movemicro 门面上可能被测试覆写的 FRONT_STATE_PATH。"""
    mod = sys.modules.get("movemicro")
    return getattr(mod, "FRONT_STATE_PATH", FRONT_STATE_PATH) if mod is not None else FRONT_STATE_PATH


def _load_front_state():
    with _STATE_LOCK:
        try:
            with open(_front_state_path(), encoding="utf-8") as fh:
                st = _json.load(fh)
            if isinstance(st, dict) and isinstance(st.get("directives"), dict):
                return st
        except Exception:
            pass
        return {"directives": {}}


def _save_front_state(st):
    with _STATE_LOCK:
        path = _front_state_path()
        _SS.ensure_parent(path)
        tmp = "%s.%d.%d.tmp" % (path, _os.getpid(), _threading.get_ident())
        with open(tmp, "w", encoding="utf-8") as fh:
            _json.dump(st, fh, ensure_ascii=False, indent=1)
        for _attempt in range(3):
            try:
                _os.replace(tmp, path)
                break
            except PermissionError:
                time.sleep(0.05)


def _merge_save_front_state(updated_directives):
    """Merge runtime tick updates back into the latest on-disk front_directives.json
    so concurrent set_front_directive / clear_front_directive calls are never lost."""
    with _STATE_LOCK:
        path = _front_state_path()
        cur = {"directives": {}}
        if _os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as fh:
                    d = _json.load(fh)
                    if isinstance(d, dict) and isinstance(d.get("directives"), dict):
                        cur = d
            except Exception:
                pass
        for k, upd in updated_directives.items():
            if k in cur["directives"]:
                cur["directives"][k].update(upd)
        _SS.ensure_parent(path)
        tmp = "%s.%d.%d.tmp" % (path, _os.getpid(), _threading.get_ident())
        with open(tmp, "w", encoding="utf-8") as fh:
            _json.dump(cur, fh, ensure_ascii=False, indent=1)
        for _attempt in range(3):
            try:
                _os.replace(tmp, path)
                break
            except PermissionError:
                time.sleep(0.05)


def _now_text():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _game_date(g):
    try:
        return g.date()
    except Exception:
        return {}


def _date_raw(g):
    try:
        return int(g.date_raw())
    except Exception:
        return None


_TICK_COUNT = {"n": 0}


def _run_front_tick(g, directive, dry_run=False):
    from .front_state import _classify_roles, _front_state, _launch_provs, _lock_valid
    from .tactics import (
        _abort_losing_attacks,
        _fill_reserve_hubs,
        _forward_rear_divisions,
        _garrison_captured,
        _org_cycle,
        _pinning_attack,
        _plug_state,
        _stage_reserves,
        _update_captured_tracker,
        _width_attack,
    )
    enemy = directive["enemy_tag"]
    stance = directive.get("stance") or "balanced_attack"
    # 2026-11-08 (run 20): _front_state/_classify_roles are pure memory reads
    # (process_vm_readv). They used to run INSIDE `with g:`, i.e. with every game
    # thread frozen, for hundreds of milliseconds every 8 s. Move them out; only
    # the actual command posting happens under the freeze.
    st = _front_state(g, enemy)
    roles = _classify_roles(g, st)

    # ---------------------------------------------------------------- 2026-11-17
    # Two things this tick got WRONG for the whole 1940 campaign. Both are now
    # handled here, BEFORE the C++ laws and OUTSIDE `with g:` -- plan_front and
    # the air-attach command both need the game's own command queue to drain,
    # which cannot happen while every game thread is ptrace-frozen.
    #
    #  * air:  attach_air_wings_to_armies() used to refuse on Windows
    #          (`attached: 0, scanned: 656`), so the Luftwaffe never flew a sortie.
    #  * land: the tick only ever right-clicked individual divisions. 141 of them
    #          piled into a few Polish border provinces (out of supply, attacks
    #          refused) and the French border was left empty. A player draws ONE
    #          FRONT LINE + ONE OFFENSIVE LINE PER ARMY and lets the engine spread
    #          them along that army's own slice of the front.
    if not dry_run and stance in ("balanced_attack", "blitzkrieg"):
        if not directive.get("air_attached"):
            try:
                air_res = g.attach_air_wings_to_armies()
            except Exception as e:
                air_res = {"error": "%s: %s" % (type(e).__name__, e)}
            directive["air_attached"] = True
            directive["air_attach_result"] = {
                "attached": air_res.get("attached_count", 0),
                "scanned": air_res.get("scanned_wings", 0),
                "refused": air_res.get("refused"),
                "error": air_res.get("error"),
                "wings": (air_res.get("attached") or [])[:8],
            }
            # 2026-11-17: attaching alone does not make the wings fly. Give them
            # a mission too (fighters -> air superiority, CAS -> close support).
            try:
                directive["air_missions"] = g.set_air_missions()
            except Exception as e:
                directive["air_missions"] = {"error": "%s: %s" % (type(e).__name__, e)}
        if directive.get("battleplan", True) and not directive.get("battleplan_drawn"):
            try:
                _dir_armies = directive.get("armies") or None
                _force_bp = bool(directive.get("force_battleplan", False))
                bp = g.plan_front(enemy, armies=_dir_armies, split=True, force=_force_bp)
            except Exception as e:
                bp = {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}
            steps = (bp or {}).get("steps") or {}
            directive["battleplan_drawn"] = bool((bp or {}).get("ok"))
            directive["battleplan_result"] = {
                "ok": (bp or {}).get("ok"),
                "error": (bp or {}).get("error"),
                "mode": (bp or {}).get("mode"),
                "front": steps.get("front"),
                "armies": steps.get("armed"),
            }
    with g:
        # Resolve the sticky Schwerpunkt BEFORE the holding laws run, so the
        # massing point is protected from _plug_state/_org_cycle this tick.
        # Run 9: protect the launch provinces of ALL staging axes (up to 3).
        raw_targets = [int(q) for q in (directive.get("staging_targets") or [])]
        legacy_t = directive.get("staging_target")
        if legacy_t and int(legacy_t) not in raw_targets:
            raw_targets.insert(0, int(legacy_t))
        valid_targets = [int(q) for q in raw_targets if _lock_valid(st, int(q))][:3]
        locked_lps = set()
        if valid_targets:
            directive["staging_targets"] = valid_targets
            directive["staging_target"] = valid_targets[0]
            st["locked_target"] = valid_targets[0]
            for q in valid_targets:
                locked_lps.update(_launch_provs(st, q))
            st["locked_launch"] = sorted(locked_lps)
            directive["staging_ticks"] = int(directive.get("staging_ticks") or 0) + 1
        else:
            directive["staging_targets"] = []
            directive["staging_target"] = None
            directive["staging_ticks"] = 0
            st["locked_target"] = None
            st["locked_launch"] = []
        # Run 10: track the last 5 game days of captures and immediately push the
        # idle former-launch stacks into them (no cooldown) BEFORE the holding
        # laws run, so a fresh tile is never left to a single spent division.
        captured = _update_captured_tracker(g, st, directive)
        garr = _garrison_captured(g, st, directive, target_garrison=3,
                                  max_moves=16, dry_run=dry_run)
        gaps = _plug_state(g, st, int(directive.get("min_garrison") or 1),
                           dry_run=dry_run, directive=directive)
        hubfill = _fill_reserve_hubs(g, st, dry_run=dry_run)
        cycle = _org_cycle(g, st, int(directive.get("min_garrison") or 1),
                           dry_run=dry_run, locked=st.get("locked_launch"))
        # Only an attacking front pulls the rear forward; a `hold` front must not
        # drain the other theatre (that is the 2026-11-05 Siegfried-line lesson).
        # Run 8: it now runs LAST, with a 24-division budget, because a fast
        # advance leaves 2-division garrisons in every province behind the new
        # line and the old 4/tick cap could never catch up.
        fwd = {"ok": True, "moved": 0, "note": "hold stance"}
        attack = {"targets": [], "accepted_total": 0, "refused_total": 0, "skipped": []}
        pin = {"pins": [], "pinned": 0}
        staged = {"ok": True, "staged": 0}
        if stance in ("balanced_attack", "blitzkrieg"):
            # 2026-11-17: the air attach and the native battle plan now run at the
            # TOP of this function (outside `with g:`), see the block above.
            # The old `battleplan_enabled` opt-in drew ONE front line for the whole
            # army; never fall back to it silently.
            if (not dry_run and directive.get("battleplan_enabled")
                    and not directive.get("battleplan_activated")):
                directive["battleplan_activated"] = True
                try:
                    directive["battleplan_optin_result"] = g.activate_army_battleplans(enemy)
                except Exception as e:
                    directive["battleplan_optin_result"] = {"error": str(e)}
            d2 = dict(directive)
            if valid_targets:
                # Run 9: all active axes are priority provinces for the scorer.
                d2["priority_provinces"] = list(valid_targets) + [
                    p for p in (directive.get("priority_provinces") or [])
                    if p not in valid_targets]
            bp_on = bool(directive.get("battleplan_drawn"))
            if bp_on:
                # 2026-11-17: the native front line + offensive line is the attack
                # now. The engine spreads each army over its own slice, respecting
                # width and supply. Massing extra divisions on top of that is
                # exactly what turned the 1940 front into a supply-dead blob.
                attack = {"targets": [], "accepted_total": 0, "skipped": "battleplan"}
            else:
                attack = _width_attack(g, st, d2, stance, dry_run=dry_run)
            used_ptrs = {int(x["division"], 16)
                         for t in (attack.get("targets") or [])
                         for x in (t.get("results") or [])
                         if x.get("ok") and x.get("division")}
            if used_ptrs:
                for rows in st["ours_by"].values():
                    for row in rows:
                        if row["ptr"] in used_ptrs:
                            row["moving"] = True
            if bp_on:
                pin = {"pins": [], "pinned": 0, "skipped": "battleplan"}
            else:
                pin = _pinning_attack(g, st, directive, attack, max_pins=3, dry_run=dry_run)
            for _p in (pin.get("pins") or ()):
                try:
                    used_ptrs.add(int(_p["division"], 16))
                except Exception:
                    pass
            for rows in st["ours_by"].values():
                for row in rows:
                    if row["ptr"] in used_ptrs:
                        row["moving"] = True
            # T1 (review 2026-11-07): NEVER call `st = _front_state(g, enemy)`
            # here. This whole block runs inside `with g:`, i.e. every game thread
            # is ptrace-frozen, so the CCommandQueue has not been consumed yet:
            # a fresh scan reads every division we just ordered as moving=False,
            # wipes st["staying"]/st["incoming"] and hands the same divisions to
            # `_stage_reserves` / `_forward_rear_divisions`, which overwrite the
            # attack orders in the SAME tick. The old "refresh so staging sees
            # them as busy" comment was exactly backwards — `moving=True` below
            # is the correct way to make them visible to the later laws.
            if bp_on:
                staged = {"ok": True, "staged": 0, "skipped": "battleplan"}
                fwd = {"ok": True, "moved": 0, "skipped": "battleplan"}
            else:
                staged = _stage_reserves(g, st, directive, dry_run=dry_run,
                                         max_axes=2, max_stage_per_axis=6)
                fwd = _forward_rear_divisions(g, st, directive, max_moves=10,
                                              dry_run=dry_run)
            if staged.get("staged"):
                attack["deferred"] = ("staging %d division(s) towards %s"
                                      % (staged["staged"], staged.get("target")))
            if staged.get("attacked"):
                attack["accepted_total"] = (int(attack.get("accepted_total") or 0)
                                            + int(staged["attacked"]))
                attack.setdefault("targets", []).append({
                    "province": staged.get("target"),
                    "results": staged.get("moves", []),
                    "from_staging": True,
                })
        abort = _abort_losing_attacks(g, st, directive, dry_run=dry_run)
    # 2026-11-06: the MCP child reached 9.9 GB RSS and was OOM-killed during a run.
    # The front tick builds several 13k-entry province dicts per call; CPython keeps
    # that arena. A periodic full collect keeps the child alive through a long war.
    _TICK_COUNT["n"] = _TICK_COUNT.get("n", 0) + 1
    if _TICK_COUNT["n"] % 10 == 0:
        import gc
        gc.collect()
    return {
        "ok": bool(gaps.get("plugged_gaps") or attack.get("accepted_total")
                   or cycle.get("moved") or pin.get("pinned") or staged.get("staged")
                   or fwd.get("moved") or hubfill.get("moved")),
        "enemy": enemy, "stance": stance, "date": _game_date(g).get("text"),
        "border_provinces": len(st["border"]),
        "exempt_paratroopers": st.get("reserved_count", 0),
        "roles": roles["counts"],
        "required_garrison": st.get("required") or {},
        "plugged_gaps": gaps.get("plugged_gaps", 0),
        "still_empty": gaps.get("still_empty", []),
        "org_rotated": cycle.get("moved", 0),
        "forwarded": fwd.get("moved", 0),
        "reserve_hubs": st.get("reserve_hubs") or [],
        "reserve_hub_moved": hubfill.get("moved", 0),
        "reserve_hub_garrison": {str(h): st["staying"].get(h, 0)
                                 for h in (st.get("reserve_hubs") or [])},
        "schwerpunkte": staged.get("schwerpunkte", 0) or attack.get("schwerpunkte", 0),
        "empty_grabs": attack.get("empty_grabs", 0),
        "staged": staged.get("staged", 0),
        "staging_towards": staged.get("target"),
        "staging_axes": staged.get("axes", []),
        "staging_targets": directive.get("staging_targets") or [],
        "recent_captured": sorted(captured)[:20],
        "garrisoned": garr.get("reinforced", 0),
        "staging_ticks": directive.get("staging_ticks"),
        "locked_launch": st.get("locked_launch") or [],
        "staging_detail": {"needed": staged.get("needed"),
                           "ready": staged.get("ready"),
                           "waiting": staged.get("waiting")},
        "accepted_total": attack.get("accepted_total", 0),
        "refused_total": attack.get("refused_total", 0),
        "attacks": attack.get("targets", []),
        "pinned": pin.get("pinned", 0),
        "pins": pin.get("pins", []),
        "aborted": abort.get("aborted", 0),
        "cancelled": abort.get("cancelled", []),
        "skipped": attack.get("skipped", []),
        "dry_run": bool(dry_run),
        "laws": {"hold": gaps, "org_cycle": cycle, "staging": staged,
                 "forward": fwd, "attack": attack, "pinning": pin, "abort": abort,
                 "reserve_hubs": hubfill},
    }


def set_front_directive(g, enemy_tag, stance="balanced_attack", priority_provinces=None,
                        priority_states=None, min_garrison=1, deploy=None, tag=None,
                        battleplan=True, armies=None):
    """Store the model's campaign order for one enemy; optionally deploy first.

    `battleplan=True` (the default since 2026-11-17) makes the first tactical tick
    draw a NATIVE front line + offensive line for free armies (or armies already
    facing `enemy_tag`, or explicit `armies=[...]`) on `enemy_tag`'s own slice of
    the front -- never stripping armies guarding other borders (e.g. FRA Maginot).
    Pass battleplan=False to get the pure micro tick.
    """
    stance = str(stance or "balanced_attack").lower()
    if stance not in FRONT_STANCES:
        return {"ok": False, "error": "stance must be one of %s" % (FRONT_STANCES,)}
    enemy_tag = str(enemy_tag or "").upper()
    if enemy_tag in ("AUTO", "?", ""):
        try:
            import combat
            tags = combat.enemy_tags(g)
            enemy_tag = (tags[0] if tags else "").upper()
        except Exception:
            enemy_tag = ""
    if not enemy_tag:
        return {"ok": False, "error": "no enemy tag (pass enemy_tag or start a war first)"}
    st = _load_front_state()
    _armies_norm = [str(x) for x in armies] if armies else None
    rec = {
        "enemy_tag": enemy_tag, "stance": stance,
        "priority_provinces": [int(x) for x in (priority_provinces or [])],
        "priority_states": [int(x) for x in (priority_states or [])],
        "min_garrison": max(1, min(2, int(min_garrison or 1))),
        "battleplan": bool(battleplan),
        "armies": _armies_norm,
        "updated": _now_text(), "date": _game_date(g).get("text"),
    }
    prev = st["directives"].get(enemy_tag) or {}
    rec["last_tick"] = prev.get("last_tick")
    if prev.get("battleplan_drawn"):
        # a re-issued directive keeps the plan that is already on the map
        rec["battleplan_drawn"] = prev.get("battleplan_drawn")
        rec["battleplan_result"] = prev.get("battleplan_result")
    if prev.get("air_attached"):
        rec["air_attached"] = True
        rec["air_attach_result"] = prev.get("air_attach_result")
    st["directives"][enemy_tag] = rec
    _save_front_state(st)
    dep = None
    if deploy is None:
        deploy = (stance == "prepare")
    if deploy:
        try:
            from .pathing import deploy_to_border
            dep = deploy_to_border(g, enemy_tag, tag=tag)
        except Exception as e:
            dep = {"ok": False, "error": str(e)}

    # 2026-11-16: storing the directive is NOT arming it.  Nothing on this box
    # executed front_directives.json (there is no gateway process here), so
    # `armed: true` used to mean "a JSON file exists" while the army stood still
    # for four months.  Tick once right now and report what actually happened.
    arm = {"ok": False, "error": "not attempted"}
    try:
        import frontd
        arm = frontd.kick(g, force=True)
        worker = frontd.status()
    except Exception as e:
        worker = {"error": "%s: %s" % (type(e).__name__, e)}
    st2 = _load_front_state()
    rec2 = (st2.get("directives") or {}).get(enemy_tag) or {}
    lt = rec2.get("last_tick") or {}
    pid_now = getattr(getattr(g, "p", None), "pid", None)
    armed = bool(lt and (lt.get("pid") in (None, pid_now)))
    return {"ok": True, "directive": rec2 or rec,
            "armed": armed, "arm_attempt": arm, "worker": worker,
            "last_tick": lt or None,
            "divisions_seen": lt.get("divisions_seen") if lt else None,
            "stored_at": FRONT_STATE_PATH,
            "note": (None if armed else
                     "directive stored but NO tactical tick has run: nothing is "
                     "driving the army yet. Check `worker.alive`; if it is false the "
                     "frontd thread is not running in any MCP server process.")}


def clear_front_directive(enemy_tag=None):
    st = _load_front_state()
    if enemy_tag:
        st["directives"].pop(str(enemy_tag).upper(), None)
    else:
        st["directives"] = {}
    _save_front_state(st)
    return {"ok": True, "directives": sorted(st["directives"])}


def front_directives(g=None):
    """Directives plus the only two facts that matter: is a tick driving them,
    and is anything alive that will keep ticking them."""
    st = _load_front_state()
    pid_now = getattr(getattr(g, "p", None), "pid", None) if g is not None else None
    out = []
    for tag, d in st["directives"].items():
        row = dict(d)
        lt = row.get("last_tick") or {}
        row["hours_since_tick"] = None
        if g is not None and lt.get("raw"):
            try:
                _cur_raw = _date_raw(g)
                if _cur_raw is not None:
                    delta = _cur_raw - lt["raw"]
                    if delta < 0:
                        # The clock read jumped backwards (a stale CDate copy, or a save
                        # loaded over the top).  A negative age is not "fresh": report
                        # nothing and let the caller see the date instead.
                        row["hours_since_tick"] = None
                        row["clock_warning"] = ("clock reads %d game hours EARLIER than "
                                                "the last tick" % delta)
                    else:
                        row["hours_since_tick"] = delta
            except Exception:
                pass
        # 2026-11-16: "directive exists" != "army is being driven".
        # `armed` answers the second question: a tick really ran against THIS game
        # process.  `tick_did_orders` is a different fact -- whether that tick had
        # anything to order -- and must never be confused with it.
        row["ticked"] = bool(lt)
        row["tick_pid_matches"] = bool(lt and (lt.get("pid") in (None, pid_now)))
        row["armed"] = bool(lt and row["tick_pid_matches"])
        row["tick_did_orders"] = (lt.get("ok") if lt else None)
        row["divisions_seen"] = lt.get("divisions_seen") if lt else None
        if not lt:
            row["armed_reason"] = "last_tick is null: the directive has never run"
        elif not row["tick_pid_matches"]:
            row["armed_reason"] = ("last_tick is from game pid %s, current pid is %s"
                                   % (lt.get("pid"), pid_now))
        else:
            row["armed_reason"] = None
        out.append(row)
    try:
        import frontd
        worker = frontd.status()
    except Exception as e:
        worker = {"error": "%s: %s" % (type(e).__name__, e)}
    res = {"ok": True, "count": len(out), "directives": out,
           "armed_count": sum(1 for r in out if r["armed"]),
           "worker": worker, "state_path": FRONT_STATE_PATH}
    if out and not res["armed_count"]:
        res["warning"] = ("no armed directive: %d stored, none ticked. "
                          "The army is NOT being commanded." % len(out))
    elif out and not worker.get("alive"):
        res["warning"] = ("directives are armed but the frontd worker is not alive "
                          "(heartbeat %s s old): nothing will tick them." %
                          worker.get("heartbeat_age_s"))
    return res


def tactical_front_tick(g, enemy_tag=None, force=False, dry_run=False):
    """Run the five CA laws for every armed directive, at most once per 2 game days."""
    st = _load_front_state()
    if not st["directives"]:
        return {"ok": True, "ticks": [],
                "note": "no front directive; call hoi4_set_front_directive"}
    keys = [str(enemy_tag).upper()] if enemy_tag else list(st["directives"])
    raw = _date_raw(g)
    out = []
    for key in keys:
        d = st["directives"].get(key)
        if not d:
            out.append({"enemy": key, "ok": False, "error": "no directive for this tag"})
            continue
        lt = d.get("last_tick") or {}
        last = lt.get("raw")
        pid_now = getattr(getattr(g, "p", None), "pid", None)
        last_pid = lt.get("pid")
        _reset_campaign_flags = False
        if last_pid is not None and pid_now is not None and int(last_pid) != int(pid_now):
            # New game process (reload / restart): the old raw belongs to another
            # timeline and must never gate the new one.
            last = None
            lt = {}
            _reset_campaign_flags = True
        if last is not None and raw is not None and raw < last:
            # Older save loaded in the same process.
            last = None
            lt = {}
            _reset_campaign_flags = True
        if _reset_campaign_flags:
            # Clear sticky per-campaign flags so a restarted/reloaded game redraws
            # battleplans and re-attaches air wings instead of stalling forever.
            d["battleplan_drawn"] = False
            d["air_attached"] = False
            d.pop("known_own_provs", None)
            d.pop("captured_at", None)
            d.pop("staging_targets", None)
            d.pop("staging_target", None)
            d.pop("staging_ticks", None)
        # Honour the 2-game-day gate only for a plausible forward delta.
        # Only the 1-game-day gate is honoured here, and only for a plausible
        # forward delta. 2026-11-06 (run 6): the clock reading itself froze at
        # 1939.05 for 40 minutes; the front then skipped EVERY tick while the
        # real date was 1939.09. A wall-clock escape hatch guarantees command —
        # but only while the game is actually running (never spam a paused game).
        wall_elapsed = time.time() - float(lt.get("wall_ts") or 0)
        gate = bool(not force and not dry_run and raw is not None and last
                    and raw >= last and raw - last < TICK_HOURS)
        if gate and wall_elapsed >= 8.0:
            try:
                import session as _sess
                running = not _sess.is_paused(g)
            except Exception:
                running = True
            gate = not running
        if gate:
            out.append({"enemy": key, "ok": True, "skipped": True,
                        "hours_until_next": TICK_HOURS - (raw - last),
                        "date": _game_date(g).get("text")})
            continue
        try:
            res = _run_front_tick(g, d, dry_run=dry_run)
        except Exception as e:
            res = {"ok": False, "enemy": key,
                   "error": "%s: %s" % (type(e).__name__, e),
                   "date": _game_date(g).get("text")}
        if not dry_run:
            _roles = res.get("roles") or {}
            d["last_tick"] = {
                "pid": pid_now, "raw": raw, "date": res.get("date"),
                "ts": time.time(), "wall_ts": time.time(),
                "plugged_gaps": res.get("plugged_gaps", 0),
                "accepted_total": res.get("accepted_total", 0),
                "refused_total": res.get("refused_total", 0),
                "staged": res.get("staged", 0),
                "pinned": res.get("pinned", 0),
                "aborted": res.get("aborted", 0),
                # `ok` here means "this tick actually issued at least one order".
                # A tick that ran but had nothing to order reports ok:false, so it
                # must NOT be read as "the executor is dead".
                "ok": res.get("ok"),
                "divisions_seen": (int(_roles.get("spearhead") or 0)
                                   + int(_roles.get("line_holder") or 0)),
                "border_provinces": res.get("border_provinces"),
                "error": res.get("error"),
            }
        out.append(res)
    if not dry_run:
        _merge_save_front_state({k: st["directives"][k] for k in keys if k in st["directives"]})
    return {"ok": any(t.get("ok") for t in out), "date": _game_date(g).get("text"),
            "ticks": out}


