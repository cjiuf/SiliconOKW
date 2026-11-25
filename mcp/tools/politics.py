"""国策、科研、学说、决议、政治理念/顾问与事件弹窗工具。"""
from __future__ import annotations

from .common import TAG, _BOOL, _INT, _STR, game, tool
from .common import _ptr, verified

@tool("hoi4_focus_current",
      "The national focus in progress (including progress/days_left/percent), banked focus days, "
      "and the size of the focus tree.", TAG)
def _focus_cur(a):
    g = game()
    tag = a.get("tag")
    fp = g.focus_progress(tag)
    return {
        "current": fp.get("current"),
        "progress": fp.get("progress"),
        "days_done": fp.get("days_done"),
        "cost_days": fp.get("cost_days"),
        "days_left": fp.get("days_left"),
        "percent": fp.get("percent"),
        "banked_days": fp.get("banked_days"),
        "completed": fp.get("completed"),
        "tree_size": len(g.focus_tree(tag)),
    }


@tool("hoi4_focus_list",
      "Focuses in the country's tree. only_startable=true returns those with no unmet "
      "prerequisite. filter searches key and name.",
      dict(TAG, only_startable=_BOOL, filter=_STR, limit=_INT))
def _focus_list(a):
    g = game()
    out = g.focuses(a.get("tag"), only_startable=a.get("only_startable", False))
    engine_checked = False
    # 2026-11-21 (P2-6): `only_startable` only applies the STATIC prerequisites
    # parsed from the focus files, while hoi4_selectable_focuses asks the engine's
    # own CSetNationalFocusCommand::CanExecute (division ratios, year, war state).
    # The 2026-11-20 playtest saw GER_anschluss listed here and blocked there in
    # the same minute.  When the caller asks for "startable", ask the engine too.
    if a.get("only_startable"):
        try:
            sel = g.selectable_focuses(a.get("tag"))
            allowed = {x["id"] for x in (sel.get("selectable") or [])}
            if sel.get("engine_checked") and allowed:
                out = [x for x in out if x["key"] in allowed]
                engine_checked = True
        except Exception:
            engine_checked = False
    f = (a.get("filter") or "").lower()
    if f:
        out = [x for x in out if f in (x["key"] or "").lower() or f in (x["name"] or "").lower()]
    lim = a.get("limit") or 200
    return {"count": len(out), "engine_checked": engine_checked,
            "focuses": [{k: v for k, v in x.items() if k != "ptr"}
                        for x in out[:lim]]}


@tool("hoi4_set_focus", "Start a national focus (same as clicking it in the focus tree).",
      dict(TAG, key=_STR), ["key"])
def _set_focus(a):
    # 2026-11-21 (续测 P2-2): an unknown key raised KeyError straight out of the
    # focus dictionary, so the caller got a raw Python traceback and a Turkish
    # "HATA:'focus not found: ...'" instead of a usable answer.  Answer like an
    # API: ok:false plus the key that was not found.
    try:
        return game().set_focus(a["key"], a.get("tag"))
    except KeyError as e:
        return {"ok": False, "key": a["key"],
                "error": "找不到该国家焦点: %s (先跑 hoi4_focus_list 或 "
                         "hoi4_selectable_focuses 拿准确的 key)" % (e,)}


@tool("hoi4_set_focus_ptr",
      "Start a national focus by its LIVE pointer (the ptr from hoi4_selectable_focuses). "
      "Use this when hoi4_set_focus returns ok:false for a focus the game lists as selectable.",
      dict(TAG, ptr=_STR), ["ptr"])
def _set_focus_ptr(a):
    return game().set_focus_ptr(_ptr(a["ptr"]), a.get("tag"))


@tool("hoi4_drop_focus", "Cancel the focus in progress.", TAG)
def _drop_focus(a):
    return game().drop_focus(a.get("tag"))


@tool("hoi4_focus_detail",
      "Everything about one focus: prerequisite focuses, mutually exclusive ones, filters.",
      dict(TAG, key=_STR), ["key"])
def _fdetail(a):
    return game().focus_detail(a["key"], a.get("tag"))


@tool("hoi4_selectable_focuses",
      "Focuses selectable RIGHT NOW, ranked by the country's own historical AI plan "
      "(common/ai_strategy_plans). Returns recommended/recommended_reason plus a "
      "selectable list with tier (S_historical/A_research_slot/A_annex/B_industry/"
      "C_air_naval) and days. Civil-war self-destruct branches are blocked by default. "
      "On the Windows build every candidate is then asked of the engine's own "
      "CSetNationalFocusCommand::CanExecute (its `available` evaluator: manpower, "
      "rifles, laws, war state, PP), so `verified: true` means the engine agrees. When "
      "a focus is already running the engine refuses everything, the probe is skipped "
      "and the list is the ranked static candidates (verified: false + note).",
      dict(TAG, allow_civil_war={"type": "boolean",
                                 "description": "Allow GER_oppose_hitler branches"}))
def _selfocus(a):
    return game().selectable_focuses(a.get("tag"), bool(a.get("allow_civil_war")))


@tool("hoi4_completed_focuses", "Completed focuses.", TAG)
def _donefocus(a):
    return {"focuses": [f for f in game().completed_focuses(a.get("tag")) if f]}


@tool("hoi4_continuous_focuses",
      "Continuous focuses (the no-completion ones at the bottom left of the tree).", TAG)
def _contfocus(a):
    out = game().continuous_focuses(a.get("tag"))
    return {"count": len(out), "focuses": out}


@tool("hoi4_set_continuous_focus", "Select a continuous focus.", dict(TAG, key=_STR), ["key"])
def _setcont(a):
    return game().set_continuous_focus(a["key"], a.get("tag"))


@tool("hoi4_drop_continuous_focus", "Drop the continuous focus.", TAG)
def _dropcont(a):
    return game().drop_continuous_focus(a.get("tag"))


@tool("hoi4_research", "Research slots: what is being researched, progress, speed.",
      dict(TAG, limit=_INT))
def _research(a):
    rows = game().research_slots(a.get("tag"))
    # 2026-11-21 (续测 P2-3): the schema never declared `limit`, so a caller
    # passing limit=1 still got all four slots back and the parameter looked
    # silently ignored.  Declared in the schema and honoured here.
    lim = a.get("limit")
    return rows[:int(lim)] if lim else rows


@tool("hoi4_research_available",
      "Technologies researchable RIGHT NOW. PURE READ: static tech tree (start_year + "
      "prerequisite paths) plus the live researched/occupied slots; zero injected calls. "
      "The final legality check is done by the game's CanExecute when starting research.",
      dict(TAG, filter=_STR))
def _research_avail(a):
    out = game().available_research(a.get("tag"))
    f = (a.get("filter") or "").lower()
    if f:
        out = [x for x in out if f in x.lower()]
    return {"count": len(out), "technologies": out}


@tool("hoi4_start_research",
      "Start researching a technology. Without slot, the first free slot is used.",
      dict(TAG, technology=_STR, slot=_INT), ["technology"])
def _start_research(a):
    return game().start_research(a["technology"], a.get("slot"), a.get("tag"))


@tool("hoi4_fill_research",
      "FILL EVERY EMPTY RESEARCH SLOT. Tries the given technologies first; if one is "
      "refused it automatically tries the next score-sorted candidate until all slots "
      "are really occupied (verified by reading the slot memory back).",
      {"technologies": {"type": "array", "items": _STR,
                        "description": "Preferred tech ids (optional)"},
       "max_attempts": _INT, **TAG})
def _fill_research(a):
    return game().fill_all_research_slots(a.get("technologies") or [],
                                           a.get("tag"), a.get("max_attempts", 30))


@tool("hoi4_researched", "Technologies already researched.", TAG)
def _researched(a):
    out = game().researched(a.get("tag"))
    return {"count": len(out), "technologies": out}


@tool("hoi4_unlock_doctrine",
      "Unlock a 1.19 GRAND DOCTRINE by name (mobile_warfare / superior_firepower / "
      "grand_battleplan / mass_assault / fleet_in_being / convoy_raiding / base_strike "
      "/ battlefield_support / operational_integrity / strategic_destruction / "
      "special_forces_quantity / special_forces_quality). Spends the game's own army/"
      "navy/air XP; success is verified in the country's doctrine store.",
      {"doctrine": dict(_STR, description="Doctrine key"), **TAG}, ["doctrine"])
def _unlock_doctrine(a):
    return game().unlock_doctrine(a["doctrine"], a.get("tag"))


@tool("hoi4_scientists", "Scientists (for special projects).", dict(TAG, limit=_INT))
def _sci(a):
    return {"scientists": game().scientists(a.get("tag"), a.get("limit", 60))}


@tool("hoi4_special_projects", "Special project pool and programme status.", TAG)
def _proj(a):
    return game().special_projects(a.get("tag"))


@tool("hoi4_tech_folders", "Technology and doctrine folders.")
def _folders(a):
    return game().tech_folders()


@tool("hoi4_available_ideas",
      "Law candidates pre-filtered IN PYTHON (zero game calls, zero ptrace). "
      "each law is gated by the war_support value in common/ideas (low 0.15, "
      "partial 0.25, war 0.50, extensive_conscription 0.20); the late-war tiers "
      "also require a war. 'legal' is a pre-filter "
      "verdict -- the authoritative CanExecute runs once when hoi4_add_idea posts.",
      dict(TAG, filter=_STR))
def _available_ideas(a):
    out = game().recommended_ideas(a.get("tag"))
    f = (a.get("filter") or "").lower()
    if f:
        out["ideas"] = [i for i in out["ideas"] if f in i["id"].lower()]
    out["count"] = len(out["ideas"])
    return out


@tool("hoi4_decisions",
      "Decisions with what a player actually needs: localised title, category, "
      "political-power cost, run time, one-shot flag and a readable slice of the "
      "`available` trigger. which: visible (the decision tab) | takeable (candidate list) | "
      "active (running) | all (every decision valid for the country). "
      "mine_only defaults to true so hoi4_decisions matches hoi4_overview and turnengine; "
      "pass mine_only=false to inspect foreign-scoped rows as well.",
      dict(TAG, which=_STR, filter=_STR, limit=_INT,
           mine_only={"type": "boolean",
                      "description": "Default true: keep only decisions belonging to this country "
                                     "(pass false for raw unfiltered engine list)"},
           rich={"type": "boolean",
                 "description": "false = legacy output (a list of keys)"}))
def _decisions(a):
    g = game()
    which = a.get("which", "visible")
    rows_all = g.decisions(a.get("tag"), which)
    mine_only = a.get("mine_only", True)
    rows = [x for x in rows_all if x.get("mine", True)] if mine_only else list(rows_all)
    f = (a.get("filter") or "").lower()
    if f:
        rows = [x for x in rows
                if f in x["key"].lower() or f in (x.get("title") or "").lower()]
    lim = a.get("limit") or 150
    shown = rows[:lim]
    if not a.get("rich", True):
        keys = [x["key"] for x in shown]
        return {"which": which, "mine_only": bool(mine_only),
                "total_unfiltered": len(rows_all), "count": len(rows),
                "decisions": keys}
    return {"which": which, "mine_only": bool(mine_only),
            "total_unfiltered": len(rows_all), "count": len(rows),
            "decisions": shown, "keys": [x["key"] for x in shown]}


@tool("hoi4_select_decision", "Take or start a decision.", dict(TAG, key=_STR), ["key"])
def _select_decision(a):
    return game().select_decision(a["key"], a.get("tag"))


@tool("hoi4_politics", "Political power, ruling party support, active ideas and laws.", TAG)
def _politics(a):
    return game().politics(a.get("tag"))


@tool("hoi4_advisors",
      "Advisor / officer-corps roles (political_advisor, army_chief, navy_chief, air_chief, "
      "theorist, high_command) with exact trait modifiers parsed from the game's own files. "
      "army_xp_per_day = experience_gain_army (daily army experience).",
      {"slot": _STR, "tag": _STR})
def _advisors(a):
    import advisors as _adv
    return _adv.advisor_table(a.get("tag") or "GER", a.get("slot"))


@tool("hoi4_ideas_available", "Ideas, laws and advisors defined for the country (narrow with filter).",
      dict(TAG, filter=_STR, limit=_INT))
def _ideas(a):
    g = game()
    out = g.available_ideas(a.get("tag"), a.get("filter", ""))
    lim = a.get("limit") or 150
    return {"count": len(out), "ideas": [x["key"] for x in out[:lim]]}


@tool("hoi4_add_idea", "Apply an idea, law or advisor through the same command a "
      "politics-tab click uses. Mutually-exclusive laws go through CReplaceIdeaCommand "
      "(old law + new law), national spirits through CAddIdeaCommand. CanExecute checks "
      "PP/prerequisites and deducts the cost; the result says method=replace/add.",
      dict(TAG, key=_STR, idea_id=_STR))
def _add_idea(a):
    import verify as _v
    g = game()
    key = a.get("idea_id") or a.get("key")
    if not key:
        return {"ok": False, "error": "missing required parameter: key or idea_id"}
    r = g.add_idea(key, a.get("tag"))
    if isinstance(r, dict) and r.get("ok") is False:
        return r
    return verified(r, lambda: _v.idea_active(g, key, a.get("tag")), key)


@tool("hoi4_hire_advisor",
      "Recruit a political advisor / chief of staff / theorist through "
      "CAddAdvisorCommand (advisors are not CIdeas, so hoi4_add_idea cannot do it). "
      "key = character key (GER_heinz_guderian), idea_token or bare name.",
      dict(TAG, key=_STR, slot=_STR), ["key"])
def _hire_advisor(a):
    return game().hire_advisor(a.get("key"), a.get("tag"), a.get("slot"))


@tool("hoi4_remove_idea", "Remove an active idea.", dict(TAG, key=_STR), ["key"])
def _remove_idea(a):
    return game().remove_idea(a["key"], a.get("tag"))


@tool("hoi4_idea_categories",
      "Idea and law categories: government (laws), research_production (companies), "
      "military_staff, army_spirit, theorist...", TAG)
def _icat(a):
    return {"categories": game().idea_categories(a.get("tag"))}


@tool("hoi4_decision_detail",
      "One decision in full: script metadata (title, category, cost, run time, "
      "available/visible/activation trigger slices) plus the live object fields.",
      dict(TAG, key=_STR), ["key"])
def _ddet(a):
    return game().decision_detail(a["key"], a.get("tag"))


@tool("hoi4_events",
      "LIVE event popup reader: every CEventWindow on screen -> the pending event id, "
      "its localized title/description and ALL its options with effects / ai_chance / "
      "trigger (looked up in the game's own scripts). Empty when no popup is open. "
      "Read-only; CEventWindow+0x68 is the CEvent*, id at CEvent+0x20, options at +0x368.",
      {"lang": dict(_STR, description="zh (default) or en"),
       "sweep": {"type": "boolean",
                 "description": "force a full ~90 s sweep for the popup window "
                                "(otherwise only the cached 8 MB pool is re-scanned)"}})
def _events(a):
    import events_win
    return events_win.open_events(game(), with_db=True, lang=a.get("lang") or "zh",
                                  deep=bool(a.get("sweep")))


@tool("hoi4_event_info",
      "Look up ANY event by id in the game's scripts: title, description, every option "
      "with its effects / ai_chance / trigger, localized. Does not touch the game.",
      {"event_id": dict(_STR, description="e.g. GER_rhineland, news.1, AUS_political_events.102"),
       "lang": dict(_STR, description="zh (default) or en")},
      ["event_id"])
def _einfo(a):
    import events_win
    return events_win.info(a["event_id"], lang=a.get("lang") or "zh")


@tool("hoi4_event_search",
      "Search the event database (6957 events incl. 677 news_event) by id or title.",
      {"query": _STR,
       "lang": dict(_STR, description="zh (default) or en"),
       "limit": {"type": "integer", "description": "max rows (default 25)"}},
      ["query"])
def _esearch(a):
    import events_win
    return events_win.search(a["query"], lang=a.get("lang") or "zh",
                             limit=int(a.get("limit") or 25))


@tool("hoi4_select_event_option",
      "Read or answer a pending event. Without write=True it only reports: every option with "
      "an `applicable` verdict evaluated from its own `trigger`, the recommended index, the "
      "index the game auto-picks after 13 days (always 1), and a built CSelectEventOptionCommand. "
      "With write=True it builds the command through the engine's own factory (0xA590D0), checks "
      "the engine's CanExecute, posts it and verifies the pending event actually went away. "
      "2026-11-18: the earlier crashes were caused by hand-writing a std::string over the "
      "command's embedded CEventScope at +0x38; the factory builds that scope correctly.",
      {"index": {"type": "integer", "description": "which option (1-based); default = recommended"},
       "event_id": dict(_STR, description="only look at this event (default: first pending)"),
       "tag": dict(_STR, description="player tag for the trigger check (default GER)"),
       "lang": dict(_STR, description="zh (default) or en"),
       "write": {"type": "boolean", "description": "actually answer the event (default false = read only)"}})
def _esel(a):
    import events_win
    return events_win.select_option(game(), index=a.get("index"),
                                    event_id=a.get("event_id") or None,
                                    lang=a.get("lang") or "zh",
                                    tag=a.get("tag") or "GER",
                                    write=bool(a.get("write")))
