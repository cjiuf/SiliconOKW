"""陆军师/编制/训练/模板编辑、将领管理、空降、战报与 RageMicro 工具。"""
from __future__ import annotations

from .common import OG, TAG, TAG_DEFAULT, _BOOL, _INT, _STR, game, tool
from game import Game
from .common import _ptr, verified

_rage = None

@tool("hoi4_province_ptr",
      "DEBUG: CProvince* for a province id (calls the engine's GetProvince).",
      {"province": _INT}, ["province"])
def _prov_ptr(a):
    g = game()
    ptr = g.province_ptr(int(a["province"]))
    return {"province": a["province"], "ptr": hex(ptr) if ptr else None}


@tool("hoi4_division_objects",
      "Raw division (CArmy) object list. Use hoi4_armies for army structure.", TAG)
def _divobjs(a):
    return game().armies(a.get("tag"))


@tool("hoi4_division_templates", "Division templates (Infanterie-Division and so on).", TAG)
def _dtempl(a):
    return game().division_templates(a.get("tag"))


@tool("hoi4_subunit_types",
      "Battalion types, used when creating or editing a division template.",
      dict(TAG, filter=_STR))
def _subunits(a):
    out = game().subunit_types(a.get("tag"), a.get("filter", ""))
    return {"count": len(out), "subunits": out}


@tool("hoi4_theatres", "Theatre objects.", TAG)
def _theatres(a):
    # 2026-11-21 (P3-5): used to return a bare [{ptr, class}] list with no hint
    # that nothing else is decoded.  Say so, and give the count.
    rows = game().theatres(a.get("tag"))
    return {"count": len(rows), "theatres": rows,
            "note": ("only the object pointer and RTTI class are decoded on the "
                     "PE build: no name / owner / division list. Use hoi4_armies "
                     "and hoi4_divisions for the order of battle.")}


@tool("hoi4_divisions", "The country's divisions (template + location).", dict(TAG, limit=_INT))
def _divs(a):
    return game().divisions(a.get("tag"), a.get("limit", 200))


@tool("hoi4_characters",
      "The country's characters: generals, admirals, advisors, political leaders.",
      dict(TAG, limit=_INT))
def _chars(a):
    return game().characters(a.get("tag"), a.get("limit", 5000))


@tool("hoi4_army_status",
      "Verified answer to 'how many divisions do I have'. Reads CCountry+0x290 "
      "through an identity-checked country pointer, falls back to an RTTI scan "
      "when that array is empty, and reports `countable:false` instead of a calm "
      "0 when the enumeration cannot be trusted (the 2026-11-16 failure).",
      {"tag": dict(_STR, description="Country tag (empty = the player)"),
       "deep": dict(_BOOL, description="Also run the RTTI fallback scan (~40 s the "
                                       "first time per game process)")})
def _army_status(a):
    return game().army_status(a.get("tag"), a.get("deep", False))


@tool("hoi4_division_selftest",
      "Compare the three independent readings of the division count "
      "(identity pointer / cached tag2index / RTTI deep scan). Run it at the "
      "start of a campaign and after every game restart.",
      {"tag": dict(_STR, description="Country tag (empty = the player)"),
       "deep": dict(_BOOL, description="Also run the RTTI fallback scan (~40 s the "
                                       "first time per game process)")})
def _division_selftest(a):
    import divenum
    return divenum.selftest(game(), a.get("tag"), deep=a.get("deep", True))


@tool("hoi4_train_divisions",
      "Open a division-training conveyor for a template and push its lines onto the map, "
      "or pass `conveyor` (ptr/handle from hoi4_conveyors) to add lines to an existing conveyor. "
      "lines=1..5 (the game silently caps some), state defaults to the capital.",
      dict(TAG, template=_STR, lines=_INT, state=_INT,
           conveyor={"type": "string",
                     "description": "Optional existing conveyor ptr/handle to add lines to instead of creating a new one"}))
def _train_divs(a):
    import verify as _v
    g = game()
    tag = a.get("tag")
    conv = a.get("conveyor")
    before = _v.conveyor_count(g, tag)
    r = g.train_divisions(a.get("template"), a.get("lines", 2), a.get("state"), tag,
                          conveyor=conv)
    if isinstance(r, dict) and (r.get("ok") is False or r.get("mode") == "add_lines_to_existing"):
        return r
    return verified(r, lambda: _v.conveyor_count(g, tag) > before, a.get("template"))


@tool("hoi4_deploy_ready",
      "Push finished (or >= min_progress) conveyor lines onto the map.",
      dict(TAG, min_progress={"type": "number"}, **{}))
def _deploy_ready(a):
    return game().deploy_ready_conveyors(a.get("tag"), float(a.get("min_progress") or 0.0))


@tool("hoi4_conveyor_progress", "Training progress of every conveyor line (0..1).", TAG)
def _cprog(a):
    g = game()
    out = []
    for conv in g.conveyors(a.get("tag")):
        try:
            _, prog = g._conveyor_progress(conv)
        except Exception as e:
            prog = None
        out.append({"ptr": conv["ptr"], "lines": conv.get("lines"), "progress": prog})
    return out


@tool("hoi4_conveyors",
      "Division-training conveyors: pointer, handle, deployment state and its lines.",
      TAG)
def _conveyors(a):
    g = game()
    tag = a.get("tag")
    convs = g.conveyors(tag)
    st = g._homeland_state_id(tag)
    return {"count": len(convs), "conveyors": convs,
            "homeland_state": st,
            "deploy_province": g.deployment_province(st, tag=tag)}


@tool("hoi4_promote_general", "Promote a corps commander to field marshal. Refuses with a reason if the rank is "
      "wrong, and reports whether the promotion actually took.",
      dict(TAG, character=_STR), ["character"])
def _promo(a):
    return game().promote_general(a["character"], a.get("tag"))


@tool("hoi4_recruit_unit_leader",
      "Recruit a new military leader (costs COMMAND POWER, verified via engine CanExecute "
      "and character/CP read-back). type: 0 or 'general'/'corps_commander' (army general, default), "
      "1 or 'field_marshal', 2 or 'admiral'.",
      dict(TAG, type={"description": "0/'general' (default), 1/'field_marshal', or 2/'admiral'"},
           dry_run=_BOOL))
def _recr(a):
    return game().recruit_unit_leader(a.get("type", 0), a.get("tag"),
                                      dry_run=bool(a.get("dry_run", False)))


@tool("hoi4_armies",
      "The country's ARMIES and ARMY GROUPS: name, commander, division count, parent group, "
      "active order count. Get pointers here before creating armies or issuing orders.", TAG_DEFAULT)
def _armies(a):
    return game().order_groups(a.get("tag"))


@tool("hoi4_unassigned_divisions",
      "Divisions not attached to any army (count and pointers).", TAG_DEFAULT)
def _unassigned(a):
    d = game().unassigned_divisions(a.get("tag"))
    return {"count": len(d), "divisions": [hex(x) for x in d]}


@tool("hoi4_create_army",
      "Collect divisions into a new ARMY, exactly like selecting them and pressing the '+' "
      "button. With count, that many unassigned divisions are taken (e.g. 24 then 6 to split "
      "24/6).",
      {"count": {"type": "integer", "description": "How many divisions to take (default: all)"},
       "divisions": {"type": "array", "items": {"type": "string"},
                     "description": "CArmy isaretcileri (opsiyonel)"},
       **TAG_DEFAULT})
def _create_army(a):
    divs = [_ptr(x) for x in a["divisions"]] if a.get("divisions") else None
    return game().create_army(divs, a.get("count"), a.get("tag"))


@tool("hoi4_create_army_group",
      "Collect armies into an ARMY GROUP (field marshal command). Without armies, every army "
      "not already in a group is taken.",
      {"armies": {"type": "array", "items": {"type": "string"},
                  "description": "COrdersGroup isaretcileri (opsiyonel)"}, **TAG_DEFAULT})
def _create_army_group(a):
    arm = [_ptr(x) for x in a["armies"]] if a.get("armies") else None
    return game().create_army_group(arm, a.get("tag"))


@tool("hoi4_assign_commander",
      "Assign a commander to an army or army group. general: character key or name (from "
      "hoi4_generals). Army groups take field marshals, armies take corps commanders; the "
      "game's own rules are enforced.",
      {**OG, "general": {"type": "string", "description": "Character key or name"},
       "allow_unverified": {"type": "boolean",
                            "description": "Ignore visibility conditions that cannot be evaluated"}},
      ["orders_group", "general"])
def _assign_commander(a):
    return game().assign_commander(_ptr(a["orders_group"]), a["general"],
                                   allow_unverified=a.get("allow_unverified", False))


@tool("hoi4_generals", "The country's generals and field marshals with their EFFECTIVE skills "
      "(attack/defense/planning/logistics, trait bonuses included).", TAG_DEFAULT)
def _generals(a):
    return game().generals(a.get("tag"))


@tool("hoi4_available_generals",
      "Commanders the player can actually assign right now (visible under the current "
      "government), sorted by skill. Filter with rank: field_marshal / corps_commander / "
      "navy_leader.",
      {"rank": {"type": "string"}, **TAG_DEFAULT})
def _availgen(a):
    return game().available_generals(a.get("tag"), a.get("rank"))


@tool("hoi4_duplicate_division_template",
      "Duplicate an existing division template into a brand-new one. Army XP cost is "
      "computed dynamically by the engine from the source template's CURRENT battalions "
      "and support companies (e.g. a small cavalry/mountain template costs ~30 XP, "
      "a stock 9-inf template ~65-80 XP, and an upgraded 9inf+3art+support template "
      "costs ~100+ XP). Pass dry_run=true first to quote the exact xp_cost without "
      "spending XP. To also estimate follow-up edit cost before duplicating, call "
      "hoi4_template_edit(template=source_name, ..., dry_run=true) first.",
      {"source_name": {"type": "string", "description": "Name of the template to copy"},
       "new_name": {"type": "string", "description": "Name for the new template (default '<source> Copy')"},
       "dry_run": _BOOL,
       **TAG_DEFAULT})
def _dup_template(a):
    g = game()
    # B1: duplicate_division_template() detaches and sleeps 0.25 s so the game can
    # consume the queue; an outer `with g:` guarantees it always fails.
    return g.duplicate_division_template(a["source_name"], a.get("tag"),
                                         a.get("new_name"),
                                         dry_run=bool(a.get("dry_run", False)))


@tool("hoi4_template_edit",
      "Edit a division template through CUpdateDivisionTemplateCommand (engine deep-copy). "
      "IMPORTANT: DEFAULT `dry_run=true` (preview only, returns applied=false and does NOT send "
      "the edit); pass `dry_run=false` to actually apply the change and spend army XP! "
      "line counts are per battalion name: line artillery is \"artillery_brigade\" "
      "(\"artillery\" is the support company), e.g. {\"infantry\":9,\"artillery_brigade\":3}. "
      "Invalid/unknown battalion or support names now fail with ok=false instead of silently posting.",
      {"template": _STR, "line": {"type": "object"}, "support": {"type": "object"},
       "dry_run": {"type": "boolean", "description": "Preview only (DEFAULT true); pass false to apply"},
       "tag": _STR})
def _tmpl_edit(a):
    import templedit
    g = game()
    name = a.get("template")
    tmpls = g.division_templates(a.get("tag"))
    pick = None
    for t in tmpls:
        if str(t.get("index")) == str(name) or t.get("name") == name:
            pick = t; break
    if not pick:
        raise KeyError("no such template: %s" % name)
    return templedit.edit_template(g, int(pick["ptr"], 16), a.get("line"),
                                   a.get("dry_run", True), a.get("support"))


@tool("hoi4_template_composition",
      "Division template makeup: how many battalions of each type (infantry, artillery, "
      "light_armor, motorized...) plus support companies. Pass name/index to filter.",
      {"name": {"type": "string", "description": "Template name or index (optional)"},
       **TAG_DEFAULT})
def _tmpl_comp(a):
    return game().template_composition(a.get("tag"), a.get("name"))


@tool("hoi4_division_detail",
      "Divisions ONE BY ONE, using the game's own field names: organisation, strength (HP), "
      "experience, entrenchment, fuel, supply ratio, days out of supply, damage taken, parent "
      "army, template. With order_group, only that army's divisions.",
      {"order_group": {"type": "string", "description": "COrdersGroup ptr (opsiyonel)"},
       "limit": {"type": "integer"}, **TAG_DEFAULT})
def _divdetail(a):
    return game().division_detail(a.get("tag"), a.get("limit", 200), _ptr(a.get("order_group")))


@tool("hoi4_add_to_army",
      "Add unassigned divisions (or divisions from another army via from_army) into an EXISTING "
      "army (orders_group) without creating a new army. NOTE: by default this only changes army "
      "ownership and does NOT move divisions off their current provinces (`positions_moved: false`); "
      "pass `move_to_province` (with default `spread=true`) to transfer AND relocate them away "
      "from an overloaded front in one step. When orders_group already has a front order and "
      "order_id is omitted, divisions attach to that front order via COrderAssignCommand.",
      {**OG,
       "divisions": {"type": "array", "items": {"type": "string"},
                     "description": "CArmy pointers (optional; default = unassigned non-paradrop divisions)"},
       "from_army": {"type": "string",
                     "description": "Optional source COrdersGroup ptr to merge/transfer divisions from"},
       "count": {"type": "integer",
                 "description": "Optional max number of divisions to move (default: fill up to 24)"},
       "order_id": {"type": "integer",
                    "description": "Optional target order id inside orders_group (0 = army only)"},
       "move_to_province": {"type": "integer",
                            "description": "Optional province id to relocate the transferred divisions to right away"},
       "spread": {"type": "boolean",
                  "description": "When move_to_province is set, spread across nearby friendly provinces (default true)"},
       "max_per_province": {"type": "integer",
                            "description": "Max divisions per province when moving with spread=true (default 8)"},
       **TAG_DEFAULT},
      ["orders_group"])
def _add_to_army(a):
    divs = [_ptr(x) for x in a["divisions"]] if a.get("divisions") else None
    return game().add_divisions_to_army(
        _ptr(a["orders_group"]),
        divisions=divs,
        count=a.get("count"),
        from_army=_ptr(a["from_army"]) if a.get("from_army") else None,
        order_id=a.get("order_id"),
        tag=a.get("tag"),
        move_to_province=a.get("move_to_province"),
        spread=bool(a.get("spread", True)),
        max_per_province=a.get("max_per_province"),
    )


@tool("hoi4_paradrop",
      "Create or reuse a PARADROP order (battle-plan kind 4) on an orders group. order_id=0 "
      "(default) makes the engine allocate the type-4 order and store the source province; "
      "order_id=<id> reuses that order. The target is attached afterwards and the engine only "
      "accepts a land province that is not the source and lies inside paradrop range of a "
      "transport wing.",
      {**OG, "order_id": {"type": "integer",
                          "description": "0 = create a new type-4 order, else reuse this id"},
       "source": {"type": "integer",
                  "description": "source province id; omit to auto-pick the province whose air "
                                 "base holds this country's transport wing (required by the "
                                 "engine: 0x22EEFE0 computes range from that air base)"},
       "target": {"type": "integer", "description": "target province id"},
       "divisions": {"type": "array", "items": _INT,
                     "description": "division pointers (from hoi4_divisions) to assign"},
       "confirm": {"type": "boolean"}},
      ["orders_group", "target"])
def _paradrop(a):
    import armyops
    src = a.get("source")
    divs = [_ptr(x) for x in (a.get("divisions") or [])] or None
    ok, err = armyops.paradrop(game(), _ptr(a["orders_group"]),
                               None if src is None else int(src), int(a["target"]),
                               divs,
                               int(a.get("order_id") or 0), bool(a.get("confirm")))
    return {"ok": bool(ok), "error": err}


@tool("hoi4_paradrop_reserved",
      "List the divisions every automatic land dispatcher refuses to touch: paratrooper "
      "templates, divisions currently executing a type-4 order, and every division of an army "
      "that owns a paradrop order. This is the exemption set used by the front laws, "
      "activate_army_battleplans, housekeep_divisions and deploy_to_border.",
      {"tag": {"type": "string", "description": "country tag, default player"}})
def _paradrop_reserved(a):
    import divreserve
    return divreserve.reserved(game(), (a.get("tag") or None))


@tool("hoi4_paradrop_targets",
      "List the province ids the ENGINE accepts as a paradrop target for this army: land, not "
      "the source, and inside the range of the transport wing stationed in the source air "
      "base. Uses COrderInstance::CanSetProvinceTarget (0x22EEFE0), i.e. exactly the check "
      "COrderSetParadropTargetCommand::CanExecute uses.",
      {**OG, "source": {"type": "integer",
                        "description": "source province id; omit to auto-pick the transport "
                                       "air base province"},
       "limit": {"type": "integer", "description": "highest province id to scan (default 2000)"},
       "tag": {"type": "string", "description": "optional: only provinces this country controls"}},
      ["orders_group"])
def _paradrop_targets(a):
    import armyops
    src = a.get("source")
    out = armyops.paradrop_targets(game(), _ptr(a["orders_group"]),
                                   None if src is None else int(src),
                                   int(a.get("limit") or 2000))
    tag = (a.get("tag") or "").upper()
    if tag:
        try:
            import mapdata
            ctrl = mapdata.province_controller_map(game())
            out = [p for p in out if ctrl.get(p) == tag]
        except Exception:
            pass
    return {"count": len(out), "provinces": out}


@tool("hoi4_combat_report",
      "ACTIVE BATTLES: every live CLandCombat with its province, terrain, both sides' "
      "countries, division counts and organisation/HP pools. `count` is the real battle "
      "count (CGameState+0x260 CCombatManager -> +0x08 CPdxArray<CLandCombat*>), and "
      "all=true returns every battle in the world, not just ours. Defaults to compact=true "
      "(top-10 low_org_divisions, top-15 activity divisions, deprecated alias omitted) so the "
      "output stays well under 3k tokens; pass compact=false or low_org_limit=N for full detail. "
      "Nothing here is a cheat: pure reads.",
      {"all": _BOOL,
       "compact": {"type": "boolean", "description": "Compact output (default true, <3k tokens)"},
       "low_org_limit": {"type": "integer", "description": "Max low_org_divisions rows (default 10 in compact mode)"}})
def _combat_report(a):
    import combat
    g = game()
    compact = bool(a.get("compact", True))
    low_lim = int(a.get("low_org_limit") or (10 if compact else 80))
    low_lim = max(0, min(low_lim, 200))
    act_lim = 15 if compact else 40
    recs = combat.combats(g, only_mine=not a.get("all", False))
    out = {"count": len(recs),
           "compact": compact,
           "source": "CGameState+0x260 CCombatManager +0x08 -> CPdxArray<CLandCombat*>",
           "damage_rates_available": bool(combat.DAMAGE_RATES),
           "combats": [{k: v for k, v in r.items() if not k.startswith("_")} for r in recs]}
    try:
        out["activity"] = combat.activity(g, limit=act_lim)
    except Exception as e:
        out["activity_error"] = repr(e)
    _adc_raw = {}
    try:
        _adc_raw = combat.active_division_combats(g, limit=low_lim) or {}
        if compact:
            _divs = list(_adc_raw.get("divisions") or [])[:low_lim]
            out["low_org_divisions"] = {
                "count": int(_adc_raw.get("count") or len(_divs)),
                "showing": len(_divs),
                "truncated": int(_adc_raw.get("count") or 0) > len(_divs),
                "basis": _adc_raw.get("basis"),
                "tags": _adc_raw.get("tags"),
                "divisions": _divs,
            }
        else:
            out["low_org_divisions"] = _adc_raw
    except Exception as e:
        out["fallback_error"] = str(e)
    _act = out.get("activity") or {}
    if _act.get("first_sample") or _adc_raw.get("basis") != "delta":
        out["fighting_now"] = None
        _act["hint"] = ("first sample for this process -- call hoi4_combat_report "
                        "again after the clock runs to get kill/damage deltas")
    else:
        _kills = [r for r in (_act.get("divisions") or [])
                  if (r.get("killed_delta") or 0) > 0]
        out["fighting_now"] = bool(_kills)
        out["killing_now"] = {"divisions": len(_kills),
                              "killed_delta": sum(r["killed_delta"] for r in _kills)}
        out["taking_damage"] = {"divisions": _act.get("engaged") or 0,
                                "note": ("org/str fell -- out-of-supply attrition "
                                         "looks identical here, so only killed_delta>0 "
                                         "counts as fighting")}
    if not recs:
        out["note"] = ("no battle involves %s right now -- check `all: true` to see "
                       "every battle in the world" % g.player_tag())
    elif not combat.DAMAGE_RATES:
        out["note"] = ("the battle list is real, but the damage-rate fields are not "
                       "recovered on the Windows build: verdicts stay `unknown` and "
                       "`activity[].killed_delta` is the live fighting signal. The "
                       "micro engine refuses to act on a battle it cannot score.")
    return out


@tool("hoi4_rage_micro",
      "COMBAT MICRO ENGINE, written in code, so it costs no model tokens. Runs once per in- "
      "game day (a full tick is ~50 ms). Reads the game's own damage rates, smooths them with "
      "an EMA, and divides each side's organisation pool by them to work out who breaks "
      "first. Terrain, river, fort, entrenchment and stacking penalties all use the "
      "coefficients from the game's own files. Searches the province graph for ENCIRCLEMENTS "
      "(which 1-3 tiles, if taken, cut the enemy line) and reduces starving pockets "
      "(out_of_supply_days>=3). Cancels attacks that are being lost, scores adjacent enemy "
      "provinces and masses every ready division on a target when the odds justify it, pins "
      "enemy stacks that would otherwise reinforce, walks idle divisions to the nearest "
      "front, and halts broken or unsupplied ones. It will not re-target a division that is "
      "already marching. Orders are not battle-plan lines: each division gets its own right- "
      "click order (CMoveCommand). With too few samples, or when neither side is close to "
      "breaking, it does nothing. Everything goes through the player command channel, so it "
      "is valid in multiplayer and visible to other players. action: start | stop | status | "
      "tick | config.",
      {"action": _STR,
       "interval": {"type": "number", "description": "Tick interval in seconds (used when daily=false)"},
       "org_withdraw": {"type": "number", "description": "Withdraw below this fraction of peak organisation (0.35)"},
       "org_resume": {"type": "number", "description": "Resume attacking above this fraction (0.70)"},
       "supply_days_limit": _INT,
       "cancel_edge": {"type": "number", "description": "Break off the attack when org_edge falls below this (0.80)"},
       "attack_ratio": {"type": "number", "description": "Strength ratio required to attack (1.15)"},
       "attack_org": {"type": "number", "description": "Divisions below this organisation fill do not attack (0.55)"},
       "pin": {"type": "boolean", "description": "Pin adjacent enemy stacks that would REINFORCE an ongoing battle (true)"},
       "pin_ratio": {"type": "number", "description": "Minimum ratio to pin (0.80), the aim is to tie them down, not to win"},
       "advance": {"type": "boolean", "description": "Walk idle divisions with no adjacent enemy to the nearest front (true)"},
       "advance_depth": {"type": "integer", "description": "How many provinces deep the front search may go (25)"},
       "cut_ratio": {"type": "number", "description": "Threshold for an attack that CLOSES a pocket (0.90), worth some risk"},
       "reduce_ratio": {"type": "number", "description": "Threshold for reducing a starving pocket (1.05)"},
       "daily": {"type": "boolean", "description": "Run once per IN-GAME DAY (true); false uses a fixed second interval"},
       "poll": {"type": "number", "description": "How often to poll for a day change, in seconds (0.25)"},
       "ema_alpha": {"type": "number", "description": "EMA smoothing factor for damage rates (0.30)"},
       "min_samples": {"type": "integer", "description": "Minimum samples before deciding (3)"},
       "decisive_within": {"type": "integer", "description": "Do nothing if neither side breaks within this many ticks (72)"},
       "max_actions_per_tick": _INT,
       "dry_run": {"type": "boolean", "description": "true: report the decisions only, send no orders"}},
      ["action"])
def _rage_micro(a):
    global _rage
    import ragemicro
    act = a["action"]
    if _rage is None or act == "config":
        kw = {k: a[k] for k in ("interval", "org_withdraw", "org_resume",
                                "supply_days_limit", "dry_run", "cancel_edge",
                                "attack_ratio", "attack_org", "pin", "pin_ratio",
                                "advance", "advance_depth", "cut_ratio",
                                "reduce_ratio", "daily", "poll", "ema_alpha",
                                "min_samples", "decisive_within",
                                "max_actions_per_tick") if k in a}
        if _rage is not None and _rage.running():
            _rage.stop()
        # Give RageMicro its own Game() handle (like frontd._worker_game) so its
        # background thread never shares _depth/_inj with the main MCP thread.
        _rage = ragemicro.RageMicro(Game(), **kw)
        if act == "config":
            return {"ok": True, "config": _rage.status()}
    if act == "start":
        started = _rage.start()
        return {"ok": True, "started": started, "status": _rage.status()}
    if act == "stop":
        _rage.stop()
        return {"ok": True, "status": _rage.status()}
    if act == "tick":
        done = _rage.tick()
        return {"ok": True, "actions": done, "status": _rage.status()}
    if act == "status":
        return _rage.status()
    return {"ok": False, "error": "action: start|stop|status|tick|config"}
