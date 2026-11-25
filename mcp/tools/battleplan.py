"""前线、战斗计划订单树、自动划前线与战区微操指令工具。"""
from __future__ import annotations

from .common import OG, TAG_DEFAULT, _BOOL, _INT, _STR, game, tool
from .common import _ptr

@tool("hoi4_border_fronts",
      "Front sections facing a target country and the border provinces in each. Input for "
      "hoi4_draw_front_line.",
      {"enemy_tag": {"type": "string"}, **TAG_DEFAULT}, ["enemy_tag"])
def _border_fronts(a):
    return game().border_fronts(a["enemy_tag"], a.get("tag"))


@tool("hoi4_front_changes",
      "Front-line movement: which map provinces changed owner since the previous call "
      "(from/to country). First call just takes a snapshot. enemy_tag filters to that country.",
      {"enemy_tag": {"type": "string", "description": "Only report changes involving this country"},
       "reset": {"type": "boolean", "description": "Rebuild the baseline snapshot"}})
def _front_changes(a):
    return game().front_changes(a.get("enemy_tag"), bool(a.get("reset")))


@tool("hoi4_draw_front_line",
      "RE-ENABLED 2026-11-08. Gives an army (COrdersGroup) a NATIVE front line along a real "
      "CFront, exactly like the front-line button in the army panel. The engine then holds "
      "and distributes the divisions itself. Pass orders_group (from hoi4_armies) plus "
      "either front_ptr (from hoi4_border_fronts) or enemy_tag "
      "(the first matching front is used). "
      "from/to are 0..100000 slices of the line; omit for the whole front.",
      {**OG, "enemy_tag": {"type": "string"}, "front_ptr": {"type": "string"},
       "section": {"type": "integer"}, "from": {"type": "integer"},
       "to": {"type": "integer"}, "assign": {"type": "string"}},
      ["orders_group"])
def _draw_front(a):
    return game().draw_front_line(
        _ptr(a["orders_group"]), enemy_tag=a.get("enemy_tag"), front_ptr=a.get("front_ptr"),
        section=a.get("section"), frm=a.get("from", 0), to=a.get("to") or 100000,
        assign=a.get("assign") or "proportional")


@tool("hoi4_army_battleplans",
      "One-shot: give EVERY free army its OWN slice of the native FRONT LINE along the CFront "
      "facing enemy_tag (not one line for the whole army), add an OFFENSIVE LINE through the "
      "enemy's territory, EXECUTE each plan and switch the arrows on. The engine then holds and "
      "distributes the divisions itself — no per-division micro, and no over-stacking. "
      "Only as many armies as that front can supply are committed; armies already holding "
      "another front are left alone. Returns per-army results. "
      "PEACETIME DRAWING IS ALLOWED AND EXPECTED (since 2026-11-18): hoi4_declare_war now "
      "requires a front line + offensive line with an assigned army facing the target, so draw "
      "the plan first, read hoi4_orders back until assigned > 0 on every army, then declare.",
      {"enemy_tag": {"type": "string", "description": "Country tag, e.g. HOL"},
       "force": {"type": "boolean",
                 "description": "Ignore the supply cap and any existing orders"},
       "armies": {"type": "array", "items": {"type": "string"},
                  "description": "Explicit COrdersGroup pointers (default: every free army)"},
       "split": {"type": "boolean",
                 "description": "Give each army its own contiguous slice (default true)"},
       "wait_seconds": {"type": "integer",
                        "description": "Poll this long for the first enemy province to change "
                                       "hands. At speed 1 the plan needs ~10 game days (~3 "
                                       "minutes); 0 (default) returns once the plan is built."},
       **TAG_DEFAULT}, ["enemy_tag"])
def _army_battleplans(a):
    return game().activate_army_battleplans(a["enemy_tag"], a.get("tag"),
                                            bool(a.get("force")),
                                            int(a.get("wait_seconds") or 0),
                                            armies=a.get("armies"),
                                            split=a.get("split", True))


@tool("hoi4_deploy_to_border",
      "High-level: issue per-division CMoveCommand moves across land border provinces "
      "facing target_tag (uses mapadj.pkl + BFS; no province IDs or division handles needed). "
      "IMPORTANT: this tool ONLY moves divisions and does NOT change or remove any army's "
      "existing front-line order -- if an army still holds a front order on another border, "
      "the engine will pull those divisions back to that old front on the next tick. "
      "To truly redeploy an army to a new border, first delete its old front order with "
      "hoi4_delete_order and redraw via hoi4_army_battleplans. Automatically guards active "
      "wartime fronts and protect_borders, and reports armies_with_active_orders warnings.",
      {"target_tag": dict(_STR, description="Target country tag, e.g. POL"),
       "orders_group": _STR,
       "include_unassigned": {"type": "boolean"},
       "protect_borders": {"type": "array", "items": {"type": "string"},
                           "description": "Neighbor tags whose border garrisons must not be stripped (e.g. ['FRA'])"},
       "min_other_garrison": {"type": "integer",
                              "description": "Minimum divisions per province to keep on protected/other borders (default 0; active wartime fronts always keep >=1)"},
       **TAG_DEFAULT}, ["target_tag"])
def _deploy_border(a):
    return game().deploy_to_border(
        a["target_tag"], _ptr(a.get("orders_group")),
        a.get("include_unassigned", True), a.get("tag"),
        protect_borders=a.get("protect_borders"),
        min_other_garrison=int(a.get("min_other_garrison") or 0),
    )


@tool("hoi4_attack_country",
      "High-level: multi-direction attack on target_tag. Finds our divisions adjacent "
      "to its provinces, filters moving/low-org units, and posts CMoveCommand(attack) "
      "with mode careful|balanced|aggressive.",
      {"target_tag": dict(_STR, description="Target country tag, e.g. POL"),
       "mode": _STR, "max_attacks": _INT, **TAG_DEFAULT}, ["target_tag"])
def _attack_country(a):
    return game().attack_country(a["target_tag"], a.get("mode", "balanced"),
                                 a.get("max_attacks", 2), a.get("tag"))


@tool("hoi4_plug_border_gaps",
      "Wartime defense: find every border province facing target_tag that has NO "
      "division and NO division moving in, then send 1-2 reserve divisions from "
      "adjacent provinces (or depth) to plug each hole. Never strips the last "
      "garrison from another border province. Use dry_run=true to plan only.",
      {"target_tag": dict(_STR, description="Enemy country tag, e.g. D04"),
       "min_garrison": _INT,
       "max_gaps": _INT,
       "dry_run": _BOOL, **TAG_DEFAULT}, ["target_tag"])
def _plug_border_gaps(a):
    return game().plug_border_gaps(a["target_tag"], a.get("min_garrison", 1),
                                   a.get("max_gaps"), a.get("dry_run", False))


@tool("hoi4_wartime_step",
      "ONE wartime micro tick, entirely in Python (0 model tokens): collect "
      "unassigned divisions, plug empty border provinces, then mass 6-9 divisions "
      "on 1-2 soft enemy provinces. Keeps a home guard, refuses low-org / low-str / "
      "out-of-supply attackers. Returns accepted_total / refused_total / plugged_gaps; "
      "ok is true only if a command was accepted or a gap was plugged.",
      {"target_tag": dict(_STR, description="Enemy tag; empty = auto-detect"),
       "max_attacks": _INT, "dry_run": _BOOL, **TAG_DEFAULT})
def _wartime_step(a):
    return game().wartime_step(a.get("target_tag"), a.get("max_attacks", 2),
                               a.get("dry_run", False))


@tool("hoi4_wartime_status",
      "Pure read: are we at war, and who is the enemy? Cheap check before a wartime micro loop.",
      {**TAG_DEFAULT})
def _wartime_status(a):
    return game().wartime_status()


@tool("hoi4_set_front_directive",
      "CAMPAIGN ORDER (the only thing the LLM should send for land war). Arm a "
      "persistent front directive for one enemy: stance hold | balanced_attack | "
      "blitzkrieg | prepare, optional priority_provinces (+300 score) and "
      "priority_states (+200), min_garrison 1-2. Ticks once immediately and "
      "reports `armed` -- true only when a tactical tick really ran. The in-process "
      "frontd worker then keeps running the 5 CA laws (role split, gap plugging + "
      "org rotation, width-exact attack, pinning, losing-attack abort) with 0 model "
      "tokens. stance=prepare also deploys the army to the border. "
      "Since 2026-11-17 the first attacking tick also draws a NATIVE front line + "
      "offensive line for every free army (one slice of the front per army, only as "
      "many armies as the front can supply) and attaches the air force to those "
      "armies; pass battleplan=false to keep the pure per-division micro tick.",
      {"enemy_tag": dict(_STR, description="Enemy tag, e.g. ENG / D04 / AUTO"),
       "stance": dict(_STR, description="prepare | hold | balanced_attack | blitzkrieg"),
       "priority_provinces": {"type": "array", "items": {"type": "integer"},
                              "description": "Province ids to take first (+300)"},
       "priority_states": {"type": "array", "items": {"type": "integer"},
                           "description": "State ids to take first (+200)"},
       "min_garrison": dict(_INT, description="Home guards per border province (1-2)"),
       "deploy": dict(_BOOL, description="Force deploy_to_border now"),
       "battleplan": dict(_BOOL, description="Draw native front+offensive lines "
                                              "first (default true; isolated to enemy_tag's slice)"),
       "armies": {"type": "array", "items": {"type": "string"},
                  "description": "Optional explicit army (COrdersGroup) hex ptrs to commit; "
                                 "if omitted, only free armies or armies already facing enemy_tag are used"},
       **TAG_DEFAULT}, ["enemy_tag"])
def _set_front_directive(a):
    return game().set_front_directive(a["enemy_tag"], a.get("stance", "balanced_attack"),
                                      a.get("priority_provinces"), a.get("priority_states"),
                                      a.get("min_garrison", 1), a.get("deploy"),
                                      battleplan=a.get("battleplan", True),
                                      armies=a.get("armies"))


@tool("hoi4_front_directives",
      "Read the armed front directives: per directive `armed` (a tick really ran "
      "against this game pid) plus the frontd worker heartbeat. `armed:false` "
      "means nothing is commanding the army, whatever the JSON says.",
      {**TAG_DEFAULT})
def _front_directives(a):
    return game().front_directives()


@tool("hoi4_tactical_front_tick",
      "Run the 5-law CA tactical tick now. The in-process frontd worker calls this "
      "by itself while the game is running; use this for a manual or dry_run "
      "inspection. force=true ignores the 1-game-day gate.",
      {"enemy_tag": dict(_STR, description="Only this enemy (default: all)"),
       "force": dict(_BOOL, description="Ignore the 2-game-day gate"),
       "dry_run": dict(_BOOL, description="Plan only, post no commands"),
       **TAG_DEFAULT})
def _tactical_front_tick(a):
    return game().tactical_front_tick(a.get("enemy_tag"), a.get("force", False),
                                      a.get("dry_run", False))


@tool("hoi4_clear_front_directive",
      "Disarm one front directive (or all when enemy_tag is empty).",
      {"enemy_tag": dict(_STR, description="Enemy tag; empty clears all"), **TAG_DEFAULT})
def _clear_front_directive(a):
    return game().clear_front_directive(a.get("enemy_tag"))


@tool("hoi4_move_divisions",
      "ROUTE A: order divisions to MOVE to or ATTACK a province - exactly the player's right "
      "click on the map. Give division pointers (from hoi4_division_objects), or an "
      "orders_group, or neither (the whole country). Pass `spread=true` (optional "
      "`max_per_province`, default 8) to automatically distribute a whole army across nearby "
      "friendly provinces around `province` without tripping the 12-division stacking guard or "
      "collapsing local supply. Verify with moving/path in the reply.",
      {"province": {"type": "integer", "description": "Target (or center) province id"},
       "divisions": {"type": "array", "items": {"type": "string"},
                     "description": "CArmy pointers (optional)"},
       "orders_group": {"type": "string", "description": "Take every division of this army (optional)"},
       "mode": {"type": "string", "description": "move (default, UI flags) or attack"},
       "wait": {"type": "number", "description": "Seconds to poll for the movement path (default 12)"},
       "limit": {"type": "integer", "description": "Max divisions (default 60)"},
       "force": {"type": "boolean",
                 "description": "Ignore the stacking guard (12+ divisions in one province)"},
       "spread": {"type": "boolean",
                  "description": "Auto-spread divisions across nearby friendly provinces around `province` (default false)"},
       "max_per_province": {"type": "integer",
                            "description": "Max divisions per province when spread=true (default 8, max 12 unless force=true)"},
       **TAG_DEFAULT},
      ["province"])
def _move_divisions(a):
    return game().move_divisions(a.get("divisions"), a.get("province"), _ptr(a.get("orders_group")),
                                 a.get("tag"), a.get("mode", "move"), a.get("wait", 12.0),
                                 a.get("limit", 60), a.get("force", False),
                                 spread=bool(a.get("spread", False)),
                                 max_per_province=a.get("max_per_province"))


@tool("hoi4_assign_divisions",
      "Distribute an army's divisions across its front orders. mode: proportional (by front "
      "length, default), even, all (every division to every order), none (leave them free for "
      "manual assignment). With divisions, only those are assigned.",
      {**OG, "enemy_tag": {"type": "string", "description": "Which border the orders belong to"},
       "mode": {"type": "string"},
       "divisions": {"type": "array", "items": {"type": "string"},
                     "description": "CArmy pointers (optional, for assigning a group at once)"}},
      ["orders_group"])
def _assign_divs(a):
    divs = [_ptr(x) for x in a["divisions"]] if a.get("divisions") else None
    return game().assign_divisions_to_front(_ptr(a["orders_group"]), a.get("enemy_tag"),
                                            a.get("mode", "proportional"), divs)


@tool("hoi4_orders",
      "Active battle plan orders of an army or army group. Each type-2 front order "
      "carries its own `children` list: the type-1 OFFENSIVE lines (the arrows). "
      "2026-11-19: they are NOT separate roots -- the group array only holds front "
      "orders, and a hand-drawn arrow appears solely under its parent. `executing` "
      "is the engine's +0x248 byte (0 = drawn but not running).",
      OG, ["orders_group"])
def _orders(a):
    import sys as _sys
    g = game()
    og = _ptr(a["orders_group"])
    rows = g.orders_of(og)
    if _sys.platform == "win32" and isinstance(rows, list):
        import armyops_win as A
        kids = A.order_children(g, og)
        by_root = {}
        for c in kids:
            by_root.setdefault(c["root"], []).append(c)
        for r in rows:
            mine = by_root.get(r.get("id"), [])
            r["offensive_lines"] = len(mine)
            r["children"] = [
                {
                    "id": c["id"],
                    "ptr": hex(c["ptr"]),
                    "type": c["type"],
                    "assigned_divisions": c["assigned"],
                    "executing": c["executing"],
                    **({"source_offset": c["source_offset"]} if c.get("source_offset") else {}),
                    **({"inverted_parent_link": True} if c.get("inverted_parent_link") else {}),
                    **({"foreign_group": True, "owner_group": c.get("owner_group")} if c.get("foreign_group") else {}),
                }
                for c in mine
            ]
            if r.get("type") == 2:
                # 2026-11-21 (P1-8): the 2026-11-20 playtest read
                # `assigned_divisions: 30` as "30 divisions are standing on the
                # front" and then found only 3 of them on a border province.
                # This number is the engine's assignment array -- the divisions
                # the order OWNS -- never their positions.
                r["assigned_semantics"] = ("divisions the order owns, not divisions "
                                           "standing on the border; positions are in "
                                           "hoi4_division_detail")
    return rows


@tool("hoi4_execute_plan",
      "EXECUTE or HALT a battle plan (the green tick / stop button in the UI). "
      "Pass executing=false (or flag=0) to switch to DEFENSE without deleting the front line "
      "or moving divisions: pass a type-1 child order_id (from hoi4_orders[].children[].id) "
      "to stop a SINGLE offensive arrow while leaving the parent front intact, pass a type-2 "
      "root order_id to stop that front's arrows, or pass order_id=0 with executing=false to "
      "halt ALL offensive orders of this army. Default executing=true.",
      {**OG,
       "order_id": {"type": "integer",
                    "description": "Type-1 arrow id, type-2 front id, or 0 for whole army (default 0)"},
       "executing": {"type": "boolean",
                     "description": "true = execute/attack (default), false = halt/defend"},
       "flag": {"type": "integer",
                "description": "Alias for executing: 1 = execute, 0 = halt"}},
      ["orders_group"])
def _exec_plan(a):
    og = _ptr(a["orders_group"])
    oid = int(a.get("order_id", 0) or 0)
    if a.get("executing") is not None:
        return game().execute_plan(og, oid, executing=bool(a.get("executing")))
    if a.get("flag") is not None:
        return game().execute_plan(og, oid, flag=int(a.get("flag")))
    return game().execute_plan(og, oid, flag=1)


@tool("hoi4_offensive_line",
      "Draw or disable a REAL OFFENSIVE LINE (the arrow inside a front line) under an existing "
      "type-2 front order. Pass `enabled=false` to HALT all active offensive arrows under "
      "`parent_order_id` without deleting the parent front line or moving divisions! "
      "When drawing (`enabled=true`, default): `provinces` must be ENEMY target provinces "
      "(NOT our own border provinces from hoi4_border_fronts!). Better yet, pass `enemy_tag` "
      "(e.g. 'HOL') and omit `provinces` so the tool automatically selects enemy target "
      "provinces accepted by the engine's 0xD57D90 enemy-area matcher. Uses the real "
      "COrderNewFrontCommand ctor + Execute (0x18D9070) and assigns divisions to the created "
      "type-1 child. Refuses to return ok=true if the resulting arrow has assigned=0 (never "
      "falls back silently to broken decorative arrows).",
      {**OG, "parent_order_id": {"type": "integer",
                                 "description": "id of the type-2 front order (from hoi4_orders)"},
       "provinces": {"type": "array", "items": {"type": "integer"},
                     "description": "ENEMY target province ids (optional when enemy_tag is passed)"},
       "enemy_tag": {"type": "string",
                     "description": "Enemy tag (e.g. HOL/BEL) to auto-select enemy target provinces"},
       "depth": {"type": "integer", "description": "BFS depth into enemy territory (default 1)"},
       "enabled": {"type": "boolean",
                   "description": "true = draw & execute offensive arrow (default); false = halt arrows under parent_order_id"},
       **TAG_DEFAULT},
      ["orders_group", "parent_order_id"])
def _offensive_line(a):
    import sys as _sys
    g = game()
    if _sys.platform != "win32":
        return {"ok": False, "error": "hoi4_offensive_line is implemented for the Windows "
                                      "backend only (armyops_win)"}
    import armyops_win as A
    og = _ptr(a["orders_group"])
    provs = [int(x) for x in (a.get("provinces") or [])]
    enemy_tag = a.get("enemy_tag")
    depth = int(a.get("depth") or 1)
    enabled = bool(a.get("enabled", True))
    res = g.offensive_line(og, int(a["parent_order_id"]), provs, None,
                           enemy_tag=enemy_tag, depth=depth, enabled=enabled)
    res["backend"] = res.get("method") or "unknown"
    kids = [c for c in A.order_children(g, og) if not c.get("foreign_group")]
    res["children"] = [(c["id"], c["assigned"], c["executing"]) for c in kids]
    res["children_executing"] = [c["id"] for c in kids if c.get("executing") == 1]
    return res


@tool("hoi4_delete_order",
      "Safely delete a battle-plan order on PE 1.19.5.0 (uses cmdwin.alloc + CanExecute "
      "+ fake vtable + post_drained). When deleting a type-2 front order that has type-1 "
      "offensive children, it first clears executing flags and deletes all children, "
      "refusing to delete the parent if any child cannot be removed (prevents orphan "
      "children and crashes). Returns deleted_children.",
      {**OG, "order_id": {"type": "integer"}}, ["orders_group", "order_id"])
def _delete_order(a):
    return game().delete_order(_ptr(a["orders_group"]), a["order_id"])


@tool("hoi4_purge_orders",
      "Delete EVERY battle-plan order that holds 0 divisions -- front orders (type-2) "
      "AND their type-1 offensive children. The engine reads data[size-1] of the "
      "assignment array without checking size, so a 0-division order is a delayed "
      "ACCESS_VIOLATION at hoi4.exe+0xB2E62E (confirmed twice: 2026-11-17 and "
      "2026-11-18 15:10). Run it right after loading a save written before 2026-11-18 "
      "and before any war. Only empty orders are touched.",
      {**TAG_DEFAULT})
def _purge_orders(a):
    return game().purge_empty_orders(a.get("tag"))


@tool("hoi4_reset_battleplans",
      "Delete EVERY battle-plan order (type-2 front orders AND their type-1 offensive "
      "children) on every army, then report what is left. Use this after loading a save: "
      "a front order that still has its executing byte set but no offensive children is "
      "an empty CPdxArray and hoi4.exe dies at +0xB2E62E as soon as the clock moves. "
      "Purging only the 0-division orders is NOT enough (2026-11-18 crash #5). Redraw a "
      "fresh plan with hoi4_army_battleplans afterwards.",
      {**TAG_DEFAULT})
def _reset_battleplans(a):
    return game().reset_battleplans(a.get("tag"))
