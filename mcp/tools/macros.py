"""四大战略运营宏与参谋部每日例行结算工具。"""
from __future__ import annotations

from .common import TAG_DEFAULT, _BOOL, _STR, game, tool
_STAFF_APPLIED = {"date": None, "move_date": None, "log": []}

@tool("hoi4_rebalance_production",
      "VETERAN MACRO: Automatically rebalance military factories across all lines based on "
      "real equipment stockpiles, surplus lines (e.g. >20k rifles wasting 16 factories), "
      "shortfalls (e.g. 0 artillery), and playbook ratios. Shrinks surplus lines FIRST to "
      "free factories, then tops up deficit lines. Pass dry_run=true to preview.",
      dict(TAG_DEFAULT,
           dry_run={"type": "boolean", "description": "Preview rebalance plan without changing lines (default false)"},
           min_factories_per_line={"type": "integer", "description": "Minimum factories kept on active lines (default 1)"}))
def _rebalance_production(a):
    import macros as _mc
    return _mc.rebalance_production(
        game(),
        tag=a.get("tag"),
        dry_run=bool(a.get("dry_run", False)),
        min_factories_per_line=int(a.get("min_factories_per_line", 1)),
    )


@tool("hoi4_upgrade_template_preset",
      "VETERAN MACRO: One-step division template upgrade using validated veteran presets "
      "(inf_9_1, inf_9_3, inf_9_3_aa, inf_9_0_hold, armor_light_20w, armor_medium_30w). "
      "Auto-selects the main infantry/armor template if omitted, calculates army-wide equipment "
      "deficit, applies CUpdateDivisionTemplateCommand (with real XP check), and when "
      "auto_rebalance=true immediately shifts factories from surplus lines to cover artillery/support/armor deficits. "
      "Defaults to dry_run=true for safe preview; pass dry_run=false to execute.",
      dict(TAG_DEFAULT,
           preset={"type": "string",
                   "description": "Preset key: inf_9_1, inf_9_3 (recommended), inf_9_3_aa, inf_9_0_hold, armor_light_20w, armor_medium_30w"},
           template={"type": "string", "description": "Optional explicit template name (auto-detected if omitted)"},
           auto_rebalance={"type": "boolean", "description": "Auto-rebalance military factories after upgrade (default true)"},
           dry_run={"type": "boolean", "description": "Preview XP cost, equipment deficit & factory rebalance (default true)"}))
def _upgrade_template_preset(a):
    import macros as _mc
    return _mc.upgrade_template_preset(
        game(),
        preset=a.get("preset", "inf_9_3"),
        template=a.get("template"),
        tag=a.get("tag"),
        auto_rebalance=bool(a.get("auto_rebalance", True)),
        dry_run=bool(a.get("dry_run", True)),
    )


@tool("hoi4_plan_front_allocation",
      "VETERAN MACRO: Multi-front strategic force allocation before war. Calls war_escalation_forecast "
      "to detect all bordering co-belligerents (guarantors / faction allies), reserves >=12% homeland "
      "strategic reserve, caps every front at its calibrated real supply capacity (never starving 42 "
      "divisions on 4 provinces), and outputs a verdict (ready / risky / suicidal). Pass execute=true "
      "to deploy safely while protecting secondary co-belligerent borders.",
      dict(TAG_DEFAULT,
           target={"type": "string", "description": "Primary enemy tag (e.g. BEL, POL, FRA)"},
           reserve_ratio={"type": "number", "description": "Fraction of divisions held in homeland reserve (default 0.12)"},
           min_reserve={"type": "integer", "description": "Minimum homeland reserve divisions (default 4)"},
           execute={"type": "boolean", "description": "Execute deployment with secondary-border protection (default false: plan only)"},
           allow_understrength={"type": "boolean", "description": "Allow executing even when verdict is suicidal (default false)"}),
      ["target"])
def _plan_front_allocation(a):
    import macros as _mc
    return _mc.plan_front_allocation(
        game(),
        target_tag=a["target"],
        tag=a.get("tag"),
        reserve_ratio=float(a.get("reserve_ratio", 0.12)),
        min_reserve=int(a.get("min_reserve", 4)),
        execute=bool(a.get("execute", False)),
        allow_understrength=bool(a.get("allow_understrength", False)),
    )


@tool("hoi4_strategic_health",
      "Pure read-only veteran strategic health board: checks division count vs year curve, "
      "main infantry template quality vs idle army XP, production imbalance (rifle surplus vs artillery zero), "
      "and live out-of-supply divisions.",
      TAG_DEFAULT)
def _strategic_health(a):
    import macros as _mc
    return _mc.strategic_health_board(game(), tag=a.get("tag"))


@tool("hoi4_staff_tick",
      "Staff policy layer, one entry point. SUPPLY: how many divisions each province "
      "can actually feed (infrastructure, supply hub, railway, victory points - all "
      "from the game's own map and history files), which provinces are over capacity "
      "and where the surplus should go. detail=full returns the per-province table; "
      "every tick is appended to staff_shadow.jsonl in the session state directory so the coefficients can "
      "be fitted against the engine's own supply ratios. "
      "READ-ONLY BY DEFAULT: without apply=true this only reports plans "
      "(live:false, applied_this_tick:false) and changes NOTHING - not supply, not "
      "industry, not the army, not the focus. apply=true performs all four, at most "
      "once per game day, and never while paused.",
      dict(TAG_DEFAULT, detail=_STR, apply=_BOOL), [])
def _staff_tick(a):
    """One call, three policies, and (in live mode) one apply per game day.

    Applying on every call would re-issue the same queue/production/move
    commands every 15 seconds: CCommand::Run only queues, so a paused game would
    accumulate duplicates and a second create_army in the same tick would take
    the same divisions again. Hence: apply at most once per game day, and never
    while the game is paused.

    2026-11-22 (P1-5): `live` defaulted to True, so a plain
    `hoi4_staff_tick(detail="summary")` -- described as "shadow mode, reports and
    changes nothing" -- queued two factories, folded four divisions into an army
    AND SET A NATIONAL FOCUS (GER_reorganize_the_wehrmacht, mutually exclusive
    with the Neurath branch).  Only SUPPLY was ever shadowed.  The default is now
    read-only; apply=true is the only way to write.
    """
    import staff_supply as ss
    g = game()
    try:
        date = g.date().get("text")
    except Exception:
        date = None
    shadow = bool(ss.cfg().get("shadow", True))
    want_apply = a.get("apply")
    # 2026-11-20 (P1 #9): `hoi4_staff_tick(apply=true)` used to be a no-op for
    # the SUPPLY moves because supply.shadow in staff_config.json was consulted
    # as a veto instead of as the default.  An explicit apply now decides:
    # apply=false keeps shadow (report only), apply=true performs the moves.
    # 2026-11-22 (P1-5): and an ABSENT apply means report only as well.
    if want_apply is None:
        live = False
    else:
        live = bool(want_apply)
        shadow = not live
    paused = True
    try:
        import session as _sess
        paused = bool(_sess.is_paused(g))
    except Exception:
        paused = True
    # supply.shadow must only stop the supply MOVES. It used to stop the whole
    # policy layer, so the 1944-04 mitigation froze industry, army and focus as
    # well: 1944-08..1947 the country sat at 2000 PP with an empty focus and no
    # construction while the staff reported "ok".
    due_moves = bool(live and not shadow and date
                     and date != _STAFF_APPLIED.get("move_date") and not paused)
    due = bool(live and date and date != _STAFF_APPLIED["date"] and not paused)
    _tag = a.get("tag")
    rep = ss.plan(g, _tag, apply_moves=due_moves)
    try:
        rep["logged"] = ss.shadow_log(rep, date)
    except Exception as e:
        rep["logged"] = "error: %r" % (e,)
    if (a.get("detail") or "summary") != "full":
        rep.pop("provinces", None)
        rep["overloaded"] = rep.get("overloaded", [])[:8]
        rep["moves"] = rep.get("moves", [])[:12]
    rep["date"] = date
    rep["live"] = live
    rep["applied_this_tick"] = due
    rep["applied_moves_this_tick"] = due_moves
    rep["supply_shadow"] = shadow
    if due:
        _STAFF_APPLIED["date"] = date
    if due_moves:
        _STAFF_APPLIED["move_date"] = date
    try:
        import staff_industry as si
        ind = si.plan(g, _tag)
        if due:
            ind["applied"] = si.apply(g, _tag, ind)
        rep["industry"] = ind
    except Exception as e:
        rep["industry"] = {"ok": False, "error": repr(e)}
    try:
        import staff_army as sa
        army = sa.plan(g, _tag)
        if due:
            army["applied"] = sa.apply(g, _tag, army)
        rep["army"] = army
    except Exception as e:
        rep["army"] = {"ok": False, "error": repr(e)}
    try:
        import staff_focus as sf
        foc = sf.plan(g, _tag)
        if due:
            foc["applied"] = sf.apply(g, foc, _tag)
        rep["focus"] = foc
    except Exception as e:
        rep["focus"] = {"ok": False, "error": repr(e)}
    # 2026-11-22 (P1-5): say out loud why nothing was written.
    rep["paused"] = bool(paused)
    if not live:
        rep["blocked_by"] = "read_only"
        rep["hint"] = ("report only; pass apply=true to let the staff queue "
                       "construction, regroup armies and set the focus")
    elif paused:
        rep["blocked_by"] = "paused"
        rep["hint"] = "the game is paused; apply runs on the first tick with the clock running"
    return rep
