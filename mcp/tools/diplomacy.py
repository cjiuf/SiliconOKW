"""外交关系、阵营、战争、借口报价/制造、宣战与和平会谈工具。"""
from __future__ import annotations

import os

from .common import TAG, TAG_DEFAULT, _INT, _STR, game, tool

def _country_index(g, tag):
    if not tag:
        return int(g.player_tag_id())
    g._load_tags()
    return int(g._tag2idx[g._tags.index(str(tag).upper())])


@tool("hoi4_relations", "Relation records with other countries.", dict(TAG, limit=_INT))
def _rel(a):
    return game().relations(a.get("tag"), a.get("limit", 60))


@tool("hoi4_faction", "The country's faction.", TAG)
def _fac(a):
    return game().faction(a.get("tag"))


@tool("hoi4_faction_members", "Members of the faction.", TAG)
def _facm(a):
    return game().faction_members(a.get("tag"))


@tool("hoi4_wars", "War status (offensive/defensive/against a major, surrender progress).", TAG)
def _wars(a):
    return game().wars(a.get("tag"))


@tool("hoi4_diplomatic_action_classes",
      "The game's 47 diplomatic action classes (declare war, justify/generate war goal, "
      "guarantee, call to faction, non-aggression, military access, send volunteers, "
      "coup...). CDiplomaticActionCommand carries one of them.")
def _dipacts(a):
    return game().diplomatic_action_classes()


@tool("hoi4_diplomacy_guide",
      "Guide to the diplomatic action classes and how they work.")
def _dipinfo(a):
    return game().diplomatic_action_info()


@tool("hoi4_wargoal_types", "War goal types (annex_everything, take_state, ...).")
def _wargoal_types(a):
    return game().wargoal_types()


@tool("hoi4_justify_wargoal",
      "Start justifying a WAR GOAL against a target. Default type is take_state (conquer a "
      "state) — annex_everything is a script-only goal and is refused by CanExecute in "
      "1.19.4.1. With no states given, the target's states are tried until one is accepted.",
      {"target": {"type": "string"},
       "wargoal": {"type": "string",
                   "description": "take_state (default), take_core_state, topple_government..."},
       "states": {"type": "array", "items": {"type": "integer"},
                  "description": "Target state ids; omit to auto-pick"},
       "dry_run": {"type": "boolean",
                   "description": "Only check CanExecute, do not start the justification"},
       "force": {"type": "boolean",
                 "description": "Bypass the historical-playbook gate (needed for a 1936 opening war)"}},
      ["target"])
def _justify(a):
    return game().justify_wargoal(a["target"], a.get("wargoal", "take_state"),
                                  a.get("states"), a.get("dry_run", False),
                                  a.get("force", False))


@tool("hoi4_justifying",
      "War goals currently being justified, with exact days_left, eta_date and the "
      "remaining PP drain. Reads CTimedWargoalActivity: +0x10 target, +0x14 owner, "
      "+0x18 days left, +0x20 days per day. `states_in_activity` is the state "
      "array the engine reads when the timer hits zero (+0x68/+0x74) -- 0 means "
      "the completion message will crash hoi4.exe at +0xB2E62E; fix it with "
      "hoi4_wargoal_repair.")
def _justifying(a):
    return game().justifying()


@tool("hoi4_wargoal_repair",
      "Repair a timed war goal whose state array is EMPTY. The engine formats the "
      "'war goal finished' message from states[size-1] (CTimedWargoalActivity "
      "+0x68/+0x70/+0x74) and reads [-4] when the list is empty -- hoi4.exe dies "
      "at +0xB2E62E the moment the justification completes (verified twice on "
      "2026-11-19). This allocates a 1-element array from the game's own heap and "
      "writes ptr/capacity/size, so the timer can finish safely.",
      {"target": {"type": "string", "description": "country tag, e.g. HOL"},
       "state": {"type": "integer", "description": "state id to record (default: the target's first state)"},
       **TAG_DEFAULT})
def _wargoal_repair(a):
    g = game()
    tgt = (a.get("target") or "").upper()
    rows = [x for x in g.justifying()
            if str(x.get("target") or "").upper() == tgt]
    if not rows:
        return {"ok": False, "error": "no timed war-goal activity against %s" % tgt}
    out = []
    for r in rows:
        sid = a.get("state") or g._pick_target_state_id(tgt)
        out.append(g.ensure_wargoal_state_array(r, sid))
    return {"ok": all(x.get("ok") for x in out), "target": tgt, "results": out,
            "justifying": g.justifying()}


@tool("hoi4_wargoal_quote",
      "Quote a war goal BEFORE starting it. On PE 1.19.5.0 this is a PURE-READ "
      "static estimate: current political power, concurrent justifications, "
      "estimated days/completion date and total PP drain from the game's own "
      "00_invasion.txt cost model. required_pp and can_justify_now are null "
      "because writing PP or calling the diplomatic validator crashes hoi4.exe "
      "(crashes #6/#7). On Linux the engine-exact CanExecute binary search is "
      "still used. Nothing is written or posted.",
      {"target": {"type": "string"},
       "wargoal": {"type": "string",
                   "description": "take_state (default), take_core_state, topple_government..."},
       "states": {"type": "array", "items": {"type": "integer"},
                  "description": "Target state ids (omit for the base one-state quote)"},
       "tag": {"type": "string", "description": "Source country (default: the player)"}},
      ["target"])
def _wargoal_quote(a):
    return game().wargoal_quote(a["target"], a.get("wargoal", "take_state"),
                                a.get("states"), a.get("tag"))


@tool("hoi4_wargoals",
      "Every live CWarGoal object (deep scan of all writable memory), so a casus belli "
      "created mid-game is not missed.",
      {"deep": {"type": "boolean", "description": "Full address-space scan (default true)"}})
def _wargoals(a):
    import diplomacy
    g = game()
    return diplomacy.wargoals(g, deep=a.get("deep", True))


@tool("hoi4_diplomatic_actions",
      "Diplomatic action classes that can be sent, plus every action class in the game.")
def _dip_actions(a):
    return game().diplomatic_actions()


@tool("hoi4_send_diplomatic_action",
      "Send a diplomatic action (guarantee, non-aggression pact, military access, "
      "embargo...). dry_run=true only checks whether it would be allowed.",
      {"action_class": {"type": "string", "description": "e.g. CNonAggressionPactAction"},
       "target": {"type": "string"},
       "dry_run": {"type": "boolean"}},
      ["action_class", "target"])
def _send_dip(a):
    return game().send_diplomatic_action(a["action_class"], a["target"], a.get("dry_run", False))


@tool("hoi4_declare_war",
      "Declare war. Runs the full preflight first and REFUSES unless all hard "
      "checks pass: (1) divisions can be counted and are non-zero; (2) diplomatic "
      "legality (not self / already at war / same faction ally / own subject); "
      "(3) at least one division stands on a border province facing the target; "
      "(4) a NATIVE battle plan faces the target (type-2 front line + type-1 "
      "offensive line + assigned > 0); (5) a live casus belli exists against the "
      "target; (6) diplomatic escalation check: inspects the target's faction, "
      "guarantors, overlord/subjects and their faction allies -- refuses if a "
      "newly pulled-in bordering power (>=3 border provinces, >=5 divs or major) "
      "has 0 divisions on that border (unless ack_co_belligerents=true), and "
      "warns on thin/unlined co-belligerent borders. Always returns "
      "co_belligerents, guaranteed_by, target_faction and co_belligerent_borders.",
      {"target": {"type": "string"}, "dry_run": {"type": "boolean"},
       "force": {"type": "boolean",
                 "description": "Skip the ADVISORY war-readiness warnings (rifles, "
                                "division ratio, historical timeline). Hard blockers "
                                "still apply."},
       "ack_co_belligerents": {"type": "boolean",
                               "description": "Explicitly acknowledge undefended borders "
                                              "against co-belligerents (guarantors / "
                                              "faction allies) pulled in by this war."}},
      ["target"])
def _declare_war(a):
    # 2026-11-23 (反糊弄升级): B-1 战争借口已升级为 O(1) 实时权威直读 CDiplomacyStatus+0x80，
    # 不再存在假阴性。MCP 工具层默认禁用 override_checks，防止模型一把跳过全部硬闸送死；
    # 仅在人类调试显式开启 HOI4_ALLOW_OVERRIDE_CHECKS=1 时允许传入。
    want_override = bool(a.get("override_checks", False))
    allow_override = os.environ.get("HOI4_ALLOW_OVERRIDE_CHECKS", "0").strip().lower() in (
        "1", "true", "yes", "on"
    )
    if want_override and not allow_override:
        return {
            "ok": False,
            "blocked_by": "override_checks_disabled",
            "target": str(a["target"]).upper(),
            "error": (
                "override_checks is disabled for autonomous MCP play. "
                "Wargoal verification now uses O(1) live CDiplomacyStatus+0x80 (no fast-list false negative). "
                "Satisfy all hard war-readiness gates (divisions, border garrison, native battleplan, live CB) "
                "instead of bypassing them."
            ),
        }
    return game().declare_war(
        a["target"], a.get("dry_run", False),
        a.get("force", False), want_override if allow_override else False,
        ack_co_belligerents=a.get("ack_co_belligerents", False),
    )


@tool("hoi4_war_readiness",
      "The declare_war preflight, without declaring: division count, diplomatic "
      "escalation forecast (target faction, guarantors, overlord/subjects, all "
      "co-belligerents pulled in and border defense exposure against each of them), "
      "target border presence, native front+offensive battle plan, and live casus "
      "belli. Cheap enough to call before every war.",
      {"target": {"type": "string"},
       "ack_co_belligerents": {"type": "boolean",
                               "description": "Evaluate readiness assuming undefended co-belligerent borders are acknowledged"},
       **TAG_DEFAULT}, ["target"])
def _war_readiness(a):
    return game().war_readiness_check(
        a["target"], a.get("tag"),
        ack_co_belligerents=bool(a.get("ack_co_belligerents", False)),
    )


@tool("hoi4_peace_status",
      "Peace conference: whether one is open, the engine's bidding maps, and each "
      "participant's score. Cheap (O(1)) -- safe to poll after a country capitulates.", {})
def _peace_status(a):
    import peacewin
    return peacewin.summary(game())


@tool("hoi4_peace_options",
      "List every state demand the open peace conference offers against one loser "
      "(state id + localised name + action pointer). Cached per (conference, winner, "
      "loser) so repeated calls do not re-allocate engine CPeaceAction objects.",
      {"loser": dict(_STR, description="Country tag that lost, e.g. HOL"),
       "winner": dict(_STR, description="Country that takes (empty = the player)")},
      ["loser"])
def _peace_options(a):
    import peacewin
    g = game()
    with g:
        lose = _country_index(g, a["loser"])
        win = _country_index(g, a.get("winner"))
        res = peacewin.options(g, lose, winner_idx=win)
        if res.get("ok"):
            res["loser"] = str(a["loser"]).upper()
            if a.get("winner"):
                res["winner"] = str(a["winner"]).upper()
        return res


@tool("hoi4_peace_demands",
      "Submit peace-conference demands for the player (or `winner`) against a beaten country. "
      "This is the conference's 提交要求 button: the demands go into the conference's turn map "
      "exactly as if they had been selected and submitted in the window. Omit `states` with "
      "`kinds=state` to take *every* state of the loser, or pass `states=[...]` (from "
      "`hoi4_peace_options`) to demand only a chosen subset.",
      {"loser": dict(_STR, description="Country tag that lost, e.g. HOL"),
       "winner": dict(_STR, description="Country that takes (empty = the player)"),
       "kinds": {"type": "array", "items": {"type": "string",
                                            "enum": ["state", "puppet", "navy", "liberate", "gov"]},
                 "description": "Demand types; default ['state']"},
       "states": {"type": "array", "items": _INT,
                  "description": "Optional subset of state IDs to demand (from hoi4_peace_options)."}},
      ["loser"])
def _peace_demands(a):
    import peacewin
    g = game()
    with g:
        conf = peacewin.live_conference(g)
        if not conf:
            return {"ok": False, "error": "no peace conference is open"}
        win = _country_index(g, a.get("winner"))
        lose = _country_index(g, a["loser"])
        kinds = a.get("kinds") or ["state"]
        states = a.get("states")
        finfo = {}
        try:
            turn, acts = peacewin.submit_demands(
                g, conf, win, lose, kinds, states=states, filter_info=finfo
            )
        except ValueError as e:
            return {"ok": False, "error": str(e), **finfo}
        out = {"ok": True, "turn": turn, "actions": len(acts),
               "detail": [{"type": g.p.i32(x + 8), "winner": g.p.i32(x + 0x30),
                           "loser": g.p.i32(x + 0x34), "state": g.p.i32(x + 0x48)}
                          for x in acts],
               "status": peacewin.summary(g)}
        if states is not None:
            out["requested_states"] = finfo.get("requested_states")
            out["matched_states"] = finfo.get("matched_states")
            if finfo.get("ignored_states"):
                out["ignored_states"] = finfo.get("ignored_states")
        return out


@tool("hoi4_peace_finish",
      "Advance a peace conference: `done` = 确认并退出 (applies the treaty; it does NOT "
      "pass first -- `pass` adds a second turn and the engine then hangs while resizing the "
      "history, research 71), `pass` = 跳过本轮, `endturn` = end this country's turn. "
      "Runs the command body directly because the CCommand queue does not drain while a "
      "conference is open.",
      {"action": {"type": "string", "enum": ["pass", "endturn", "done"]},
       "tag": dict(_STR, description="Country to act for (empty = the player)")},
      ["action"])
def _peace_finish(a):
    import peacewin
    g = game()
    with g:
        if a["action"] == "done":
            # `pass` appends a second turn to +0x1F8 and the engine then resizes the
            # history (2 -> 1) while applying the treaty -- that path hung the game
            # twice on 2026-11-15 (research 71).  Done on its own keeps the count at 1.
            return peacewin.finish(g, _country_index(g, a.get("tag")))
        return peacewin.execute(g, a["action"], _country_index(g, a.get("tag")))
