"""军工/船坞产线、装备库存、建造队列、州详情、资源与贸易工具。"""
from __future__ import annotations

from .common import TAG, _BOOL, _INT, _STR, game, tool

@tool("hoi4_production",
      "Production lines: equipment, assigned factories, unit cost, daily output.", TAG)
def _production(a):
    return game().production_lines(a.get("tag"))


@tool("hoi4_set_production_amount",
      "Set how many factories a production line gets. Verifies the change actually landed. "
      "When increasing military factories with 0 idle, auto_reclaim=true (default) automatically "
      "reclaims spare factories from severely over-stockpiled surplus lines first and reports `reclaimed_from`.",
      dict(TAG, line=_INT, amount=_INT,
           auto_reclaim={"type": "boolean",
                         "description": "Auto-reclaim factories from surplus lines when idle=0 (default true)"}),
      ["line", "amount"])
def _set_prod(a):
    return game().set_production_amount(
        a["line"], a["amount"], a.get("tag"),
        auto_reclaim=bool(a.get("auto_reclaim", True)),
    )


@tool("hoi4_set_production_line_equipment",
      "SWAP an existing production line to a new equipment variant (CA换装). Keeps "
      "the line's factories; the game resets efficiency as it does in the UI. "
      "Reads the line back and only reports ok when the variant really changed.",
      dict(TAG, line=_INT, equipment=_STR, dry_run=_BOOL), ["line", "equipment"])
def _set_prod_equipment(a):
    return game().set_production_line_equipment(a["line"], a["equipment"],
                                                a.get("tag"), a.get("dry_run", False))


@tool("hoi4_add_production_line",
      "Create a NEW production line for an equipment variant, exactly like clicking "
      "it in the production tab: the game takes 1 free military factory for it. "
      "Goes through the engine's own CAddProductionLineCommand ctor, runs its "
      "CanExecute first and only then posts. Refuses (already_exists) when a line "
      "for that equipment is already there — change its factory count instead. "
      "A brand-new line starts at 0% efficiency, so output_per_day is low at first.",
      dict(TAG, equipment=_STR, dry_run=_BOOL), ["equipment"])
def _add_prod(a):
    return game().create_production_line(a["equipment"], a.get("tag"),
                                         a.get("dry_run", False))


@tool("hoi4_remove_production_line",
      "Remove a production line entirely (the line's factories go back to the pool). "
      "Reports before/after line counts and which equipment the removed line was "
      "making; ok is only true when the list really shrank.",
      dict(TAG, line=_INT), ["line"])
def _rm_prod(a):
    return game().remove_production_line(a["line"], a.get("tag"))


@tool("hoi4_equipment_types", "Equipment types in the game (narrow with filter).",
      {"filter": _STR, "limit": _INT})
def _eq(a):
    out = game().equipment_types(a.get("filter", ""))
    lim = a.get("limit") or 150
    return {"count": len(out), "equipment": out[:lim]}


@tool("hoi4_equipment_stockpile",
      "Equipment counts in the stockpile and producible variants.", dict(TAG, limit=_INT))
def _stock(a):
    return game().equipment_stockpile(a.get("tag"), a.get("limit", 120))


@tool("hoi4_construction", "Construction queue: building, state, state_name, factories, progress, status.", TAG)
def _construction(a):
    return game().construction_queue(a.get("tag"))


@tool("hoi4_building_types", "Buildable building types and their ids.")
def _bt(a):
    return game().building_types()


@tool("hoi4_add_construction",
      "Queue a building (e.g. state=51, building='industrial_complex'). State ids from "
      "hoi4_states, building names from hoi4_building_types.",
      dict(TAG, state=_INT, building=_STR, level=_INT, province=_INT), ["state", "building"])
def _add_c(a):
    return game().add_construction(a["state"], a["building"], a.get("level", 1),
                                   a.get("province", 0), a.get("tag"))


@tool("hoi4_remove_construction", "Remove an item from the construction queue.",
      dict(TAG, index=_INT), ["index"])
def _rm_c(a):
    return game().remove_construction(a["index"], a.get("tag"))


@tool("hoi4_states", "Ids of the states the country owns.", TAG)
def _states(a):
    return {"states": [s["id"] for s in game().states(a.get("tag"))]}


@tool("hoi4_state_detail",
      "One state in detail: province count, resistance, compliance, building levels.",
      dict(TAG, state=_INT), ["state"])
def _sdet(a):
    return game().state_detail(a["state"], a.get("tag"))


@tool("hoi4_states_detail", "Detailed list of owned states.", dict(TAG, limit=_INT))
def _sdets(a):
    return game().states_detail(a.get("tag"), a.get("limit", 40))


@tool("hoi4_resources",
      "Strategic resources: steel, aluminium, rubber, tungsten, chromium, oil, coal. "
      "PE 1.19.5.0: CCountry+0x11F8 -> CCountryResources, seven int64 amounts from "
      "+0x60 at 0x10 stride (fixed point, 100000 = 1 unit). Fixed 2026-11-19 -- "
      "until then CO_RESOURCES pointed at the Linux offset 0x11C0 and every value "
      "read 0.0.", TAG)
def _res(a):
    out = game().resources(a.get("tag"))
    if isinstance(out, dict) and out and not any(out.values()):
        out = dict(out)
        out["warning"] = ("all seven resources read 0.0 -- offsets may have moved "
                          "again; do not trust this as a shortage")
    return out


@tool("hoi4_trade_routes",
      "Resource trade routes (who sells what to whom). PE 1.19.5.0: reads the "
      "country's export book at CCountry+0x11F8 -> +0x728 (one array per "
      "strategic resource) and derives imports by scanning every other "
      "country's book, because a deal is stored on the SUPPLIER's side only. "
      "Returns exports/imports/net per resource. world=true lists every route "
      "on the map (verified: 131 routes world-wide in 1936.03).",
      dict(TAG, limit=_INT, world={"type": "boolean"}))
def _trade(a):
    return game().trade_routes(a.get("tag"), a.get("limit", 40),
                               world=bool(a.get("world")))


@tool("hoi4_trade",
      "Import strategic resources (oil/rubber/tungsten/steel/aluminium/chromium/coal) "
      "from an exporting country via native CCreateTradeCommand (verified by CanExecute "
      "and supplier export-book read-back). Pass amount=8/16/24 (or factories=1/2/3), "
      "or amount=0 to cancel an existing trade.",
      dict(TAG,
           partner={"type": "string", "description": "Exporting country TAG (e.g. ROM, SOV, VEN, SWE)"},
           resource={"type": "string", "description": "oil | rubber | tungsten | steel | aluminium | chromium | coal"},
           amount={"type": "integer", "description": "Total resource units to import (e.g. 8, 16; 0 = cancel)"},
           factories={"type": "integer", "description": "Shortcut: civilian factories to spend (amount = factories * 8)"},
           dry_run=_BOOL),
      ["partner", "resource"])
def _create_trade(a):
    return game().create_trade(
        partner=a["partner"],
        resource=a["resource"],
        amount=a.get("amount"),
        factories=a.get("factories"),
        tag=a.get("tag"),
        dry_run=bool(a.get("dry_run", False)),
    )
