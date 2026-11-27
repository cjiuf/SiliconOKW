"""Compact 档位 TurnEngine 高层聚合工具（简报、深度查询、Plan DSL 执行与推进）。"""
from __future__ import annotations

from .common import TAG_DEFAULT, _BOOL, _INT, _STR, game, tool

@tool(
    "hoi4_turn_brief",
    "Generate a compact Markdown turn briefing (pure read, ~2 KB / ~700 tokens). "
    "Includes date/window/alerts, economy, focus/research/production/construction, "
    "pending events (conditional), selectable focuses when idle, active combats at war, "
    "and session-scoped memo/last_receipt. Pass include_data=true only if raw JSON dict is needed.",
    dict(
        TAG_DEFAULT,
        memo=_STR,
        include_focus_candidates=_BOOL,
        include_research_candidates=_BOOL,
        include_data=_BOOL,
        reset_state=_BOOL,
    ),
    [],
)
def _turn_brief(a):
    import turnengine as TE
    return TE.build_turn_brief(
        game(),
        tag=a.get("tag"),
        memo=a.get("memo"),
        include_focus_candidates=a.get("include_focus_candidates", True),
        include_research_candidates=a.get("include_research_candidates", True),
        include_data=bool(a.get("include_data", False)),
        reset_state=bool(a.get("reset_state", False)),
    )


@tool(
    "hoi4_inspect",
    "Bounded read-only escape hatch (max 3 queries per call) for deep inspection "
    "when hoi4_turn_brief lacks detail. Allowed topics (20): country, events, "
    "selectable_focuses, research_available (includes start_year/ahead_years/cost_days), "
    "advisors, idea_catalog (unified laws/ideas/advisors with slot & cost), decisions, "
    "armies, war_readiness, combat, stockpile, resources, state, peace, air, navy, staff, "
    "health, autopilot (active Standing Orders & journal), strategy_catalog (auditable strategy formulas).",
    dict(
        TAG_DEFAULT,
        queries={
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "topic": {"type": "string"},
                    "tag": {"type": "string"},
                    "target": {"type": "string"},
                    "filter": {"type": "string"},
                    "slot": {"type": "string"},
                    "state": {"type": "integer"},
                    "state_id": {"type": "integer"},
                    "which": {"type": "string"},
                    "mine_only": {"type": "boolean"},
                    "lang": {"type": "string"},
                    "sweep": {"type": "boolean"},
                    "compact": {"type": "boolean"},
                    "full": {"type": "boolean"},
                    "all": {"type": "boolean"},
                },
                "required": ["topic"],
            },
        },
    ),
    ["queries"],
)
def _turn_inspect(a):
    import turnengine as TE
    return TE.inspect_topics(game(), a.get("queries") or [], tag=a.get("tag"))


@tool(
    "hoi4_run_plan",
    "Validate and serially execute a structured Turn Plan DSL (up to 15 high-level "
    "actions) with strict arg validation (dry_run validates identically), foreground-window "
    "guard, version guard, post-action liveness check, empty-order guard before resume, "
    "persisted memo, and compact receipt. Arg names align with public tools (state, line, "
    "template, technology, target, slot) while keeping legacy aliases.",
    dict(
        TAG_DEFAULT,
        memo=_STR,
        dry_run=_BOOL,
        actions={
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "op": {"type": "string"},
                    "args": {"type": "object"},
                    "on_error": {"type": "string", "enum": ["continue", "abort"]},
                },
                "required": ["op"],
            },
        },
    ),
    ["actions"],
)
def _turn_execute_plan(a):
    import turnengine as TE
    plan = {"actions": a.get("actions") or []}
    if "memo" in a:
        plan["memo"] = a.get("memo")
    ok_v, errs_v = TE.validate_plan(plan)
    if not ok_v:
        return {"ok": False, "blocked_by": "invalid_plan", "errors": errs_v}
    return TE.execute_plan(game(), plan, dry_run=bool(a.get("dry_run")), tag=a.get("tag"))


@tool(
    "hoi4_wait_and_brief",
    "Sleep until the next game signal or max_days (auto-pauses on wake by default) "
    "and immediately return the new turn's compact briefing in one round-trip. "
    "When Standing Orders are active (set via hoi4_run_plan set_standing_orders), autopilot "
    "deterministically handles queued focus/research/construction/production/PP/divisions/trivial events "
    "while enforcing the non-disableable Kernel Floor (CB/war/peace/decisive events/PP reserve/heartbeat/max_days/no_progress). "
    "timeout_seconds is clamped to min(requested, HOI4_WAIT_MAX_S) (default 26s; set HOI4_WAIT_MAX_S <= client MCP timeout - 4s "
    "to avoid client -32001 disconnect while the game keeps advancing) "
    "and echoed in wait.timeout_seconds. "
    "Pass respect_red_dots=false to bypass the red-dot gate for this wait only (wargoal/peace/Kernel Floor still force-pause).",
    dict(
        TAG_DEFAULT,
        max_days=_INT,
        timeout_seconds=_INT,
        pause_on_wake=_BOOL,
        respect_red_dots=dict(
            _BOOL,
            description="Enforce red-dot gate during this wait (default true; also disabled globally when hoi4_red_dot_guard enabled=false)",
        ),
        autopilot=dict(
            _BOOL,
            description="Enable Standing Orders autopilot during wait (omit = auto when Standing Orders active; false = bypass for this wait)",
        ),
        fast_forward=dict(
            _BOOL,
            description="Set speed <= 3 (UI 4-bar cap) and unpause automatically after pre-run checks; Kernel Floor still polls continuously",
        ),
        include_data=_BOOL,
        signals={"type": "array", "items": {"type": "string"}},
        until_date={"type": "array", "items": {"type": "integer"}},
    ),
    [],
)
def _turn_wait_and_brief(a):
    import turnengine as TE
    return TE.wait_and_brief(
        game(),
        max_days=a.get("max_days", 14),
        until_date=a.get("until_date"),
        signals=a.get("signals"),
        timeout_seconds=a.get("timeout_seconds"),
        pause_on_wake=a.get("pause_on_wake", True),
        respect_red_dots=a.get("respect_red_dots", True),
        tag=a.get("tag"),
        include_data=bool(a.get("include_data", False)),
        autopilot=a.get("autopilot"),
        fast_forward=bool(a.get("fast_forward", False)),
    )
