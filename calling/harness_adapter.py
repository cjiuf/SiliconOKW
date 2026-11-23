"""A GameAdapter for Xocas12/hoi4-agent-harness, backed by this engine.

The harness owns the clock, the wake rule, the brief and the reflex/planner
split.  This adapter is the only place that knows the game is read through
`ReadProcessMemory` and written through `CCommandQueue` — which is the whole
point of putting it here: the harness's own adapters are savegame/logtail/UI
driven, and each of them can act but not see, or see but not act.

    pip install git+https://github.com/Xocas12/hoi4-agent-harness
    from harness_adapter import ClausewitzAdapter
    env = HOI4Env(adapter=ClausewitzAdapter())

Field mapping is deliberately tolerant: the harness's `GameState` gains fields
between releases, so `_state()` passes only the names that exist in the
installed dataclass and drops the rest.  Anything we cannot read is listed in
`unknown_fields`, which is how the harness tells "I saw zero" apart from "I
could not see".

This module is not imported by the MCP server; it is the seam for the gateway.
It has not been exercised end to end against the harness yet: `read_state()`
and the actions below need one live session to confirm field names.
"""
from __future__ import annotations

import dataclasses
import os
import sys
import time
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import verify as _verify                      # noqa: E402
import session as _session                    # noqa: E402


def _types():
    try:
        from hoi4_harness import types as T
        return T
    except Exception:
        return None


def _state(**kw):
    """Build a GameState, ignoring fields this harness version does not have."""
    T = _types()
    if T is not None and hasattr(T, "GameState"):
        known = {f.name for f in dataclasses.fields(T.GameState)}
        return T.GameState(**{k: v for k, v in kw.items() if k in known})
    return SimpleNamespace(**kw)


def _result(ok, message, action="", changed=None, error_kind=None):
    T = _types()
    if T is not None and hasattr(T, "ActionResult"):
        return T.ActionResult(ok=ok, message=message, action=action,
                              changed=changed or {}, error_kind=error_kind)
    return {"ok": ok, "message": message, "action": action,
            "changed": changed or {}, "error_kind": error_kind}


class ClausewitzAdapter:
    """Reads the live process; every action goes through the game's own queue."""

    supported_actions = frozenset({
        "set_focus", "start_research", "set_production_amount",
        "add_idea", "advance_time", "note",
    })

    def __init__(self, game=None, read_only=False):
        from game import Game
        self.g = game or Game()
        self.g.read_only = read_only
        self._unknown = []

    # ---------------------------------------------------------------- contract
    def info(self):
        T = _types()
        fields = dict(name="clausewitz", readable=True, writable=True,
                      clock_control=True,
                      notes="live process; reads are free, a write stops the game "
                            "threads for one attach")
        if T is not None and hasattr(T, "AdapterInfo"):
            return T.AdapterInfo(**fields)
        return SimpleNamespace(**fields)

    def read_state(self):
        g = self.g
        ov = g.overview()
        date = g.date()
        country = ov.get("country") or {}
        fac = ov.get("factories") or {}
        pol = ov.get("politics") or {}
        unknown = []

        def rows(key, mapper):
            try:
                return [mapper(r) for r in (ov.get(key) or [])]
            except Exception:
                unknown.append(key)
                return []

        state = _state(
            date="%04d.%02d.%02d" % (date["year"], date["month"], date["day"]),
            country=country.get("tag"),
            political_power=pol.get("political_power"),
            stability=country.get("stability"),
            war_support=country.get("war_support"),
            manpower=country.get("manpower"),
            civilian_factories=fac.get("civ_total"),
            military_factories=fac.get("mil_total"),
            dockyards=fac.get("dock_total"),
            national_focus=(ov.get("focus") or {}).get("current"),
            research=rows("research", lambda s: {
                "index": s.get("slot"), "technology": s.get("researching"),
                "days_remaining": s.get("days_left")}),
            production=rows("production", lambda r: {
                "equipment": r.get("equipment"), "factories": r.get("factories"),
                "efficiency": r.get("efficiency"),
                "output_per_day": r.get("output_per_day")}),
            divisions=rows("divisions", lambda d: d) or self._divisions(),
            unknown_fields=unknown,
            events=[],
        )
        self._unknown = unknown
        return state

    def _divisions(self):
        try:
            return [{"template": d.get("template"), "count": 1,
                     "location": str(d.get("province")),
                     "strength": d.get("strength", 1.0)}
                    for d in self.g.divisions()]
        except Exception:
            return []

    def acknowledge_events(self, events):
        return None

    # -------------------------------------------------------------- clock
    def pause(self):
        _session.pause(self.g, True)

    def resume(self):
        _session.pause(self.g, False)

    def advance(self, days=1):
        """Run the clock for `days`, then pause again. Returns the new date."""
        target = self.g.date()["raw"] + int(days) * 24
        self.resume()
        deadline = time.time() + max(30.0, int(days) * 12.0)
        try:
            while time.time() < deadline:
                if self.g.date()["raw"] >= target:
                    break
                time.sleep(0.5)
        finally:
            try:
                self.pause()
            except Exception:
                pass
        return self.read_state().date

    # ------------------------------------------------------------ actions
    def apply(self, call):
        name = getattr(call, "action", None) or (call or {}).get("action")
        args = getattr(call, "args", None) or (call or {}).get("args") or {}
        fn = getattr(self, "_do_" + str(name), None)
        if fn is None:
            return _result(False, "action %r is not supported by the memory adapter"
                           % (name,), name, error_kind="unsupported")
        try:
            return fn(args)
        except Exception as e:                 # an adapter fault is a failed action
            return _result(False, "%s: %s" % (type(e).__name__, e), name,
                           error_kind="rejected")

    # Each action applies, then reads its effect back. `verify.poll` treats a
    # frozen clock as unverified rather than as success, so a paused game cannot
    # manufacture an ok.
    def _do_set_focus(self, args):
        key = args.get("focus") or args.get("key")
        r = self.g.set_focus(key)
        return _result(bool(r.get("ok")), str(r), "set_focus", r)

    def _do_start_research(self, args):
        r = self.g.start_research(args["technology"])
        return _result(bool(r.get("ok")), str(r), "start_research", r)

    def _do_set_production_amount(self, args):
        r = self.g.set_production_amount(args["line"], args["amount"])
        return _result(bool(r.get("ok")), str(r), "set_production_amount", r)

    def _do_add_idea(self, args):
        key = args.get("idea") or args.get("key") or args.get("idea_id")
        r = self.g.add_idea(key)
        state, detail, _ = _verify.poll(
            lambda: _verify.idea_active(self.g, key),
            clock=lambda: self.g.date()["raw"])
        return _result(state == _verify.VERIFIED, detail, "add_idea", r,
                       None if state == _verify.VERIFIED else "rejected")

    def _do_advance_time(self, args):
        d = self.advance(args.get("days", 1))
        return _result(True, "advanced to %s" % d, "advance_time", {"date": d})

    def _do_note(self, args):
        return _result(True, args.get("text", ""), "note")
