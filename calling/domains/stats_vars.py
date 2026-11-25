"""国家基础统计、世界紧张度与原生/动态脚本变量读取领域 Mixin。"""
from __future__ import annotations

import offsets as O
import struct
import sys

from ._shared import (
    BG_LEVEL,
    BG_TYPE_ID,
    CO_ARMIES,
    CO_COMMAND_POWER,
    CO_OWNED_STATES,
    CO_STATES,
    FN_DYNAMIC_VAR,
    ST_BUILDINGS,
    _arr,
)

class StatsVarsMixin:
    """国家基础统计、世界紧张度与原生/动态脚本变量读取领域 Mixin。"""

    PE_VAR_LABEL = 0x22AEAB0
    PE_VAR_REGISTRY = 0x33DF1D0

    #: Names that must never be pushed through the console `get_var`: evaluating
    #: them outside their subsystem trips an engine assert and exits the process.
    #: Confirmed 2026-11-20 (see KNOWN_ISSUES #6): a sweep that reached
    #: pc_total_score killed hoi4.exe with "Peace conference trigger is called
    #: when there is no active peace conference".
    CONSOLE_DENY = ("pc_total_score", "pc_current_score")

    # lower-case registry names that have a native country-scope reader here
    NATIVE_VARS = ("political_power", "manpower", "manpower_k", "fuel", "fuel_k",
                   "stability", "num_owned_states", "num_controlled_states",
                   "num_core_states", "num_divisions",
                   "num_of_civilian_factories", "num_of_military_factories",
                   "num_of_naval_factories", "num_of_factories", "num_of_nukes",
                   "num_armies", "num_researched_technologies", "amount_research_slots",
                   "max_manpower", "max_available_manpower", "num_faction_members",
                   "num_of_available_civilian_factories",
                   "num_of_civilian_factories_available_for_projects",
                   "num_of_available_military_factories",
                   "num_of_available_naval_factories",
                   "num_deployed_planes", "num_ships", "num_equipment",
                   "num_of_owned_factories", "num_of_controlled_factories",
                   "num_battalions",
                   "fuel_ratio", "max_fuel_k", "original_research_slots",
                   "highest_party_popularity",
                   "political_power_daily", "command_power_daily",
                   "command_power", "army_experience", "navy_experience",
                   "air_experience", "num_subjects", "num_equipment_in_armies",
                   "num_equipment_in_armies_k", "num_occupied_states",
                   "num_of_civilian_factories_in_cores",
                   "num_of_military_factories_in_cores",
                   "num_of_naval_factories_in_cores")

    def _scope_call(self, fn, tag=None, extra_this=True, signed=True):
        """Call the game's country-scoped getters: f(this, scope) -> int64"""
        c = self._c(tag)
        p = self.p

        def w(scr):
            p.write(scr, b"\0" * 0x400)
            p.write(scr + 0x208, struct.pack("<i", c.tag_id))   # scope = scr+0x200
            return (scr, scr + 0x200)
        import api
        # C2 (review 2026-11-07): `stats.pkl` was scanned against 1.19.2 and its
        # 45 addresses are WILD in 1.19.4.1. `api.allow_call(fn)` here re-opened
        # exactly the hole `api._load_stat_calls()` closed on 2026-11-04: the
        # injected call ran past the end of .text and SIGSEGV'd the game (that is
        # how hoi4_wars / hoi4_full_report kept killing the process). A getter
        # must be re-derived AND re-verified against this binary before it is
        # added to api.SCOPE_CALL_ALLOW.
        if fn not in api.SCOPE_CALL_ALLOW:
            raise api.CallError(
                "Refusing unverified 1.19.2 country-scope address %#x in 1.19.4.1" % fn)
        return self.call(fn, scratch_writer=w, signed=signed)

    def country_stats(self, tag=None, raw=False):
        """~44 country statistics gathered from the game's own trigger functions."""
        import stats as _st
        table = _st.build()
        FIXED_KEYS = {"command_power", "command_power_daily", "political_power_daily",
                      "surrender_progress", "convoy_threat", "naval_mine_danger"}
        # Bool triggers return "actual == expected", and the expected field is zero, so
        # the result comes back inverted. The right value is not (rax & 1). Checked
        # against is_major, is_in_faction, has_war and has_capitulated; all agree.
        BOOL_PREFIX = ("has_", "is_", "exists", "country_has")
        # These want an extra parameter (id/tag), so they are not reliable here:
        SKIP = ("owns_", "days_since", "has_decision", "has_rule", "has_terrain",
                "has_opinion_modifier", "has_country_leader_ideology",
                "has_completed_national_focus", "has_active_timed_decision",
                "country_has_cosmetic_tag", "has_attache")
        out = {}
        with self:
            for name, (fn, cls) in sorted(table.items()):
                if not raw and name.startswith(SKIP):
                    continue
                try:
                    v = self._scope_call(fn, tag)
                except Exception:
                    continue
                if raw:
                    out[name] = v
                elif name in FIXED_KEYS:
                    out[name] = round(v / O.FIXED, 4)
                elif name.startswith(BOOL_PREFIX):
                    out[name] = not bool(v & 1)
                elif -10 ** 9 < v < 10 ** 9:
                    out[name] = v
        if not out:
            # 2026-11-11: stats.build() points at 1.19.2 trigger addresses and every
            # call is refused, so this tool returned {} for EVERY country — the agent
            # had no way to size up an opponent (it never knew France had 127
            # divisions). Fall back to the memory-only country summary plus the live
            # division count, and say so.
            try:
                out = dict(self._c(tag).summary() or {})
            except Exception:
                out = {}
            try:
                out["divisions"] = len(_arr(self.p, self._c(tag).ptr, CO_ARMIES))
            except Exception:
                pass
            # 2026-11-22 (P2-2): the description promised ~44 trigger statistics
            # but the body only ever returned `summary()`.  Add the three that are
            # cheap and real (memory reads), and NAME the ones that are not, so
            # "casualties: missing" can never be read as "no casualties".
            try:
                out["at_war"] = bool(self.wars(tag).get("at_war"))
            except Exception:
                out["at_war"] = None
            try:
                _maj = set(_arr(self.p, self.gamestate, O.GS_MAJORS))
                out["is_major"] = self._c(tag).ptr in _maj
            except Exception:
                out["is_major"] = None
            out["unavailable"] = [
                "casualties", "surrender_progress", "convoy_threat",
                "naval_mine_danger", "days_since_capitulated",
            ]
            out["unavailable_reason"] = (
                "these come from the 1.19.2 trigger table (stats.pkl), which is "
                "disabled on this build; the fields above are live memory reads")
            out["source"] = ("memory-only: country summary + at_war / is_major / "
                             "divisions; manpower is only reliable for the player "
                             "country. NOT the 44-trigger statistics the tool name "
                             "suggests")
        return out

    def _pe_var_buf(self):
        b = getattr(self, "_var_buf", None)
        if b:
            return b
        b = self.malloc(0x400)
        self.p.write(b, b"\0" * 0x400)
        self._var_buf = b
        return b

    def _pe_dynamic_label(self, name):
        """Localised text for `name` (a loc lookup, NOT a variable registry)."""
        buf = self._pe_var_buf()
        # The buffer starts with a std::string; re-zeroing it keeps the SSO form.
        # A label longer than 15 bytes would heap-allocate and that block is not
        # freed -- labels are short, and one tool call makes one string.
        self.p.write(buf, b"\0" * 0x40)
        self.p.write(buf + 0x100, name.encode("utf-8") + b"\0")
        self.call(self.p.base + self.PE_VAR_LABEL, (buf, buf + 0x100, 0, 0),
                  absolute=True, force=True)
        return self.p.stdstring(buf)

    def _msvc_string(self, addr, maxlen=64):
        """Read an MSVC std::string at `addr`; None when the layout does not fit."""
        p = self.p
        raw = p.try_read(addr, 0x20)
        if not raw:
            return None
        size = struct.unpack_from("<Q", raw, 0x10)[0]
        cap = struct.unpack_from("<Q", raw, 0x18)[0]
        if size > maxlen or cap < size:
            return None
        ptr = addr if cap <= 0xF else struct.unpack_from("<Q", raw, 0)[0]
        data = p.try_read(ptr, size)
        if not data:
            return None
        return data.decode("utf-8", "replace")

    def _pe_var_names(self):
        """Authoritative dynamic-variable names, walked from PE_VAR_REGISTRY.

        This is the only trustworthy answer to "is <name> a real variable?" on
        the PE build -- 0x22AEAB0 cannot answer it (it is localisation lookup).
        Cached per process id because the registry is built once at startup and
        only ever appended to.
        """
        pid = getattr(self.p, "pid", None)
        hit = getattr(self, "_var_names_cache", None)
        if hit and hit[0] == pid:
            return hit[1]
        names = []
        try:
            hdr = self.p.try_read(self.p.base + self.PE_VAR_REGISTRY, 0x40)
            if hdr:
                sent = struct.unpack_from("<Q", hdr, 0x08)[0]
                node, seen = sent, set()
                while len(names) < 400:
                    raw = self.p.try_read(node, 8)
                    if not raw:
                        break
                    node = struct.unpack("<Q", raw)[0]
                    if node == sent or node in seen:
                        break
                    seen.add(node)
                    s = self._msvc_string(node + 0x10)
                    if s:
                        names.append(s)
        except Exception:
            names = []
        self._var_names_cache = (pid, names)
        return names

    def world_tension(self):
        """World tension as a 0..1 fraction, or None while its source is unknown.

        2026-11-20: the COEFFICIENT is known -- `WAR_SUPPORT_TENSION_IMPACT = 0.4`
        in common/defines/00_defines.lua ("total impact of world tension"), and
        the in-game tooltip decomposes to base 30% + fleet 5% + tension 5.91%,
        i.e. 14.775% that day, so the formula reproduces the engine exactly.
        What is missing is the live value: CWorldThreat / CThreatSource (2
        instances each) carry no 0.05..0.35 float in their first 0x400 bytes and
        CCountry holds no cached copy, so this returns None rather than 0.
        """
        return None

    def _native_var(self, name, c):
        """Value of a documented country-scope dynamic variable, read natively.

        Keys are the lower-case registry names; the UPPER_CASE spellings the tool
        used to take (ARMY_EXPERIENCE ...) still work because we lower() first.
        """
        key = (name or "").strip().lower()
        s = c.summary() or {}
        exp = c.experience or {}
        if key == "manpower":
            return s.get("manpower")
        if key == "manpower_k":
            m = s.get("manpower")
            return None if m is None else round(m / 1000.0, 3)
        if key == "fuel":
            return s.get("fuel")
        if key == "fuel_k":
            f = s.get("fuel")
            return None if f is None else round(f / 1000.0, 3)
        if key == "num_divisions":
            return len(_arr(self.p, c.ptr, CO_ARMIES))
        if key == "command_power":
            try:
                return round(self.p.i64(c.ptr + CO_COMMAND_POWER) / O.FIXED, 5)
            except Exception:
                return None
        if key == "num_of_factories":
            civ, mil = s.get("civilian_factories"), s.get("military_factories")
            dock = s.get("naval_dockyards")
            if civ is None or mil is None or dock is None:
                return None
            return civ + mil + dock
        # Derived from readers that are already verified elsewhere in this class;
        # each of these can be cross-checked against its own tool.
        if key == "num_armies":
            # CO_ARMIES holds CArmy = DIVISIONS (that is why num_divisions reads
            # the same array).  The player-visible "armies" are COrdersGroup, so
            # count those instead -- cross-checked against hoi4_armies.
            try:
                return self.order_groups(c.tag)["count"]
            except Exception:
                return None
        if key == "num_researched_technologies":
            try:
                return len(self.researched(c.tag) or [])
            except Exception:
                return None
        if key == "amount_research_slots":
            try:
                return len(self.research_slots(c.tag) or [])
            except Exception:
                return None
        if key == "max_manpower":
            # manpower_total = the pool before army/air/navy usage (api.py).
            try:
                return c.manpower_total
            except Exception:
                return None
        if key == "max_available_manpower":
            return s.get("manpower")
        if key == "num_of_available_civilian_factories" or \
                key == "num_of_civilian_factories_available_for_projects":
            # Idle civilian factories = total minus what the construction queue holds.
            try:
                used = sum(int(q.get("factories") or 0)
                           for q in self.construction_queue(c.tag))
                return max(0, (s.get("civilian_factories") or 0) - used)
            except Exception:
                return None
        if key in ("num_of_available_military_factories",
                   "num_of_available_naval_factories"):
            cls = ("CMilitaryProductionLine"
                   if key == "num_of_available_military_factories"
                   else "CNavalProductionLine")
            total = (s.get("military_factories")
                     if key == "num_of_available_military_factories"
                     else s.get("naval_dockyards"))
            try:
                used = sum(int(l.get("factories") or 0)
                           for l in self.production_lines(c.tag)
                           if l.get("class") == cls)
                return max(0, (total or 0) - used)
            except Exception:
                return None
        if key == "num_deployed_planes":
            try:
                aw = self.air_wings(c.tag)
                wings = aw.get("wings") if isinstance(aw, dict) else aw
                return sum(int(w.get("planes") or 0) for w in (wings or []))
            except Exception:
                return None
        if key == "num_ships":
            try:
                import navalops_win as NW
                fleets = NW.naval_fleets(self, c.tag)
                return sum(int(t.get("ships") or 0)
                           for f in fleets for t in (f.get("task_forces") or []))
            except Exception:
                return None
        if key == "num_equipment" or key.startswith("num_equipment@"):
            # PARAMETERISED: `num_equipment@<equipment type>` = that type in the
            # country's stockpile.  The engine answers 0 without the argument
            # (verified), so the bare name must not claim the stockpile total --
            # hoi4_equipment_stockpile is the tool for the whole list.
            # Verified: num_equipment@infantry_equipment = 903 = our infantry row.
            base, _, arg = key.partition("@")
            if not arg:
                return 0.0
            import eqprobe
            try:
                tot = eqprobe.stock_by_type(self, c.tag)
                val, _info = eqprobe.resolve(self, tot, arg)
            except Exception:
                return None
            return val
        if key in ("num_owned_states", "num_controlled_states", "num_core_states",
                   "num_occupied_states"):
            # 2026-11-20 (corrected): CCountry holds TWO state lists.
            #   +0x460 (= CO_STATES) is the CONTROLLED set
            #   +0x478 is the OWNED set
            # Peacetime GER has 29/29 so the old single-list reader happened to
            # agree with the engine.  The 1936 Italy-Ethiopia war separates them:
            #   ITA owns 30 and controls 31 (one occupied Ethiopian state)
            #   ETH owns 10 and controls 9
            # `num_occupied_states` is therefore controlled - owned, which is
            # exactly "states occupied by this nation" as the engine's own
            # dynamic-variable documentation words it.
            # CState+0x2F0 == 1 still marks a NON-CORE (annexed) state.
            try:
                controlled = _arr(self.p, c.ptr, CO_STATES)
                owned = _arr(self.p, c.ptr, CO_OWNED_STATES)
                noncore = sum(1 for s in owned if self.p.i32(s + 0x2F0) == 1)
            except Exception:
                return None
            if key == "num_controlled_states":
                return len(controlled)
            if key == "num_owned_states":
                return len(owned)
            if key == "num_core_states":
                return len(owned) - noncore
            mine = set(owned)
            return sum(1 for s in controlled if s not in mine)
        if key.startswith("num_of_") and key.split("@", 1)[0].endswith("_factories_in_cores"):
            # PARAMETERISED, @<Tag|ROOT> is mandatory (no argument -> the engine
            # answers 0).  Per the engine's own documentation: "the number of
            # <civ/mil/naval> factories on CORE states of the current country
            # scope, on those states that are under control of @Tag".
            # Control is taken by intersecting with the tag's controlled list, so
            # no controller field on CState is needed.
            base, _, arg = key.partition("@")
            bname = {"num_of_civilian_factories_in_cores": "industrial_complex",
                     "num_of_military_factories_in_cores": "arms_factory",
                     "num_of_naval_factories_in_cores": "dockyard"}.get(base)
            if not arg or bname is None:
                return 0.0
            try:
                self._load_buildings()
                ctl_tag = c.tag if arg.lower() in ("root", "this") else arg.upper()
                ctl = set(_arr(self.p, self._c(ctl_tag).ptr, CO_STATES))
                owned = _arr(self.p, c.ptr, CO_OWNED_STATES)
                total = 0
                for s in owned:
                    if s not in ctl or self.p.i32(s + 0x2F0) == 1:
                        continue
                    for b in _arr(self.p, s, ST_BUILDINGS):
                        if self._bld_by_id.get(self.p.i32(b + BG_TYPE_ID)) == bname:
                            total += struct.unpack("<h", self.p.read(b + BG_LEVEL, 2))[0]
            except Exception:
                return None
            return total
        if key in ("num_of_owned_factories", "num_of_controlled_factories"):
            # Engine: 91 for GER = civ 46 + mil 32 + dock 13 (get_var agrees).
            civ, mil = s.get("civilian_factories"), s.get("military_factories")
            dock = s.get("naval_dockyards")
            if civ is None or mil is None or dock is None:
                return None
            return civ + mil + dock
        if key == "num_battalions":
            # Counts LINE battalions plus SUPPORT companies: GER 30 divisions
            # = 253 line + 62 support = 315, which is exactly what get_var says.
            try:
                comp = {t["name"]: t for t in self.template_composition(c.tag)}
                total = 0
                for d in (self.divisions(c.tag) or {}).get("divisions") or []:
                    t = comp.get(d.get("template"))
                    if not t:
                        continue
                    total += sum((t.get("line") or {}).values())
                    total += sum((t.get("support") or {}).values())
                return total
            except Exception:
                return None
        if key == "original_research_slots":
            try:
                return len(self.research_slots(c.tag) or [])
            except Exception:
                return None
        if key == "max_fuel_k":
            try:
                return round(c.fuel_capacity / 1000.0, 3)
            except Exception:
                return None
        if key == "fuel_ratio":
            # 0.72093 from our own pool vs the engine's 0.72086: the RATIO is
            # right even though both absolute numbers are scaled (see
            # KNOWN_ISSUES: fuel reads ~3.05x low on this build).
            try:
                cap = c.fuel_capacity
                return round(c.fuel / cap, 5) if cap else None
            except Exception:
                return None
        if key == "highest_party_popularity":
            # CPoliticalStatus+0x20 is CPdxArray<CPoliticalParty*>; each party's
            # popularity is +0x88 fixed 1e5*100.  GER: .35/.10/.40/.15 = 1.00,
            # max .40 == get_var highest_party_popularity.
            try:
                arr = _arr(self.p, c.politics, 0x20)
                vals = [self.p.i64(v + 0x88) / (O.FIXED * 100) for v in arr if v]
                return round(max(vals), 5) if vals else None
            except Exception:
                return None
        if key == "num_faction_members":
            try:
                return len((self.faction_members(c.tag) or {}).get("members") or [])
            except Exception:
                return None
        # Daily incomes: base x (1 + the country's modifier).  Both were fitted
        # against the engine on 2026-11-20 and match to five decimals:
        #   political_power_daily 2.324   = 2.0 * (1 + 0.162)
        #   command_power_daily   0.33091 = 0.4 * (1 - 0.17271)
        # The bases (2.0 / 0.4) are constants of this build; if a future patch
        # changes them the value will be visibly off rather than silently wrong,
        # because the modifier term is read live.
        if key == "political_power_daily":
            # base 2 x (1 + factors) MINUS the running national focus, which costs
            # 1 PP/day (the UI lists it as "莱茵兰重新军事化 -1").
            # GER 1936.03 with the Rhineland focus: 2 x (1 + 0.162) - 1 = 1.324,
            # the panel says +1.32.
            try:
                v = 2.0 * (1.0 + c._win_modifier_value("political_power_factor"))
                try:
                    if (self.current_focus(c.tag) or {}).get("key"):
                        v -= 1.0
                except Exception:
                    pass
                return round(v, 5)
            except Exception:
                return None
        if key == "command_power_daily":
            try:
                return round(0.4 * (1.0 + c._win_modifier_value("command_power_gain_mult")), 5)
            except Exception:
                return None
        if key == "num_subjects":
            # CDiplomacyStatus+0x20 also carries subject relations.  Each entry's
            # +0x10 is the OVERLORD and +0x14 the subject, and the very same
            # object sits in the subject's own array too, so counting only the
            # rows whose +0x10 is us is what makes this "my subjects".
            # Verified 2026-11-20 against the 1936 setup (every one of these is
            # a real subject pair in vanilla):
            #   GER 0, ITA 0, RAJ 0 (it is ENG's subject, not an overlord),
            #   JAP 1 (MAN), FRA 2 (LEB, SYR), ENG 11 (AST CAN NZL SAF OMA RAJ
            #   JOR PAL MAL BRM KUW).
            try:
                dip = self.p.u64(c.ptr + O.CO_DIPLOMACY_STATUS)
                arr = _arr(self.p, dip, 0x20) if dip else []
                me = self.p.i32(c.ptr + 8)
                return sum(1 for a in arr
                           if self.p.i32(a + 0x10) == me
                           and "Subject" in (self.oi.class_of(a) or ""))
            except Exception:
                return None
        if key.startswith("num_equipment_in_armies"):
            # PARAMETERISED: `num_equipment_in_armies@<equipment type>`; without
            # the @argument the engine itself answers 0.  Summed from each
            # division's own equipment array (CArmy+0x368) and verified against
            # `get_var` on GER 1936.03.01:
            #   infantry 24900, support 889, artillery 346, motorized 605,
            #   light_tank_equipment_1 692  -- all exact.
            import eqprobe
            base, _, arg = key.partition("@")
            if not arg:
                return 0.0
            try:
                tot = eqprobe.army_equipment(self, c.tag)
                val, _info = eqprobe.resolve(self, tot, arg)
            except Exception:
                return None
            if base.endswith("_k"):
                return round(val / 1000.0, 5)
            return val
        exps = {"army_experience": "army", "navy_experience": "navy",
                "air_experience": "air"}
        if key in exps:
            return exp.get(exps[key])
        direct = {"political_power": "political_power", "stability": "stability",
                  "war_support": "war_support",
                  "num_of_civilian_factories": "civilian_factories",
                  "num_of_military_factories": "military_factories",
                  "num_of_naval_factories": "naval_dockyards",
                  "num_of_nukes": "nukes"}
        if key == "divisions":                 # legacy alias of num_divisions
            return len(_arr(self.p, c.ptr, CO_ARMIES))
        if key in direct:
            return s.get(direct[key])
        return None

    def dynamic_variables(self):
        """Every dynamic-variable name in the engine's own registry (PE build)."""
        if sys.platform != "win32":
            return {"ok": False,
                    "error": "the registry walk is implemented for the PE build only"}
        names = self._pe_var_names()
        if not names:
            return {"ok": False,
                    "error": "registry at %#x unreadable" % self.PE_VAR_REGISTRY}
        return {"ok": True, "count": len(names), "names": sorted(names),
                "native": sorted(self.NATIVE_VARS),
                "source": "static registry RVA %#x" % self.PE_VAR_REGISTRY,
                "note": "authoritative: this is the same list the game writes into "
                        "documentation/dynamic_variables_documentation.md"}

    def _console_var(self, name):
        """Ask the engine's own `get_var` for a value (console, opt-in only).

        Only the developer console exposes a generic evaluator on the PE build,
        so this is NOT part of the pure-read path and is never used unless the
        caller explicitly asks for it.  `get_var` answers a plain number for the
        variables it can resolve and echoes the current UI scope ("State (id)")
        for the ones it cannot, so the scope form is reported as unresolved
        instead of being mistaken for a value.
        """
        try:
            msg = (self.console("get_var " + name).to_dict() or {}).get("message") or ""
        except Exception as e:                                  # noqa: BLE001
            return {"ok": False, "error": repr(e)}
        text = msg.strip()
        eq = text.split("=", 1)
        if len(eq) != 2:
            return {"ok": False, "raw": text, "error": "unexpected get_var output"}
        payload = eq[1].strip()
        if payload.endswith(")") and "(" in payload:
            return {"ok": False, "raw": text,
                    "error": "get_var could not resolve this name and echoed the "
                             "current UI scope instead of a value"}
        try:
            return {"ok": True, "value": float(payload), "raw": text,
                    "source": "console-get_var"}
        except ValueError:
            return {"ok": False, "raw": text, "error": "non-numeric get_var output"}

    def script_variable(self, name: str, tag=None, via="native"):
        """Read a value through the game's dynamic variable resolver (e.g. 'ARMY_EXPERIENCE').

        2026-11-16: FN_DYNAMIC_VAR is still the Linux ELF offset (0x2D8EE60) and
        `g.call()` refuses it as "not on the verified-call list". Calling a
        guessed PE address here would crash the game, so the win32 branch says
        so instead of throwing a CallError traceback at the caller.
        """
        if sys.platform == "win32":
            c = self._c(tag)
            key = (name or "").strip()
            low = key.lower()
            names = self._pe_var_names()
            # `num_equipment_in_armies@infantry_equipment` is the parameterised
            # form of a registry name, so the registry check has to look at the
            # part before the @.
            base_low = low.split("@", 1)[0]
            known = base_low in names if names else None
            label = None
            label_error = None
            try:
                label = self._pe_dynamic_label(key)
                if label == key:        # no loc entry -> nothing to show
                    label = None
            except Exception as e:
                label_error = repr(e)
            v = self._native_var(key, c)
            console_try = None
            if via == "console":
                # Explicitly asked for the engine's own answer, so it wins over
                # the native reader (they differ where the native reader is a
                # documented approximation, e.g. stability misses the ruling-party
                # popularity term: 0.75 here vs 0.81 in the engine).
                if low in self.CONSOLE_DENY:
                    return {"ok": False, "name": key, "known": known,
                            "error": "refused: evaluating this variable outside its "
                                     "subsystem trips an engine assert and kills the "
                                     "process (see KNOWN_ISSUES #6)"}
                console_try = self._console_var(key)
                if console_try.get("ok"):
                    return {"ok": True, "name": key, "value": console_try["value"],
                            "known": known, "loc_label": label,
                            "registry_entries": len(names) or None,
                            "source": console_try["source"],
                            "native_value": v,
                            "note": "opt-in console path: value came from the developer "
                                    "command get_var, not a pure memory read"}
            if v is not None:
                out = {"ok": True, "name": key, "value": v, "known": known,
                       "loc_label": label, "source": "native-memory",
                       "registry_entries": len(names) or None,
                       "note": "value read from the country object; the engine's own "
                               "name->value lookup is console-only on the PE build "
                               "(see KNOWN_ISSUES #6)"}
                if known is False:
                    # WAR_SUPPORT / DIVISIONS predate the registry walk; they are
                    # not registry names but the quantity is the same one.
                    out["alias"] = True
                    out["note"] = ("legacy alias: this is not a registry name on the "
                                   "PE build, but it maps to the same quantity")
                return out
            out = {"ok": False, "available": False, "name": key, "known": known,
                   "registry_entries": len(names) or None,
                   "loc_label": label,
                   "mapped": list(self.NATIVE_VARS)}
            if console_try is not None:
                out["console"] = console_try
            if known:
                out["error"] = "no native reader for this dynamic variable"
                out["detail"] = ("the name IS in the authoritative registry at "
                                 "0x33DF1D0; it is scope-dependent (state/leader/etc.) "
                                 "or simply not mapped yet")
            elif known is False:
                out["error"] = ("not a documented dynamic variable: the engine's "
                                "registry does not contain this name")
                near = [n for n in names if low and (low in n or n in low)][:12]
                if near:
                    out["similar"] = near
            else:
                out["error"] = "no native mapping for this dynamic variable"
            if label_error:
                out["label_error"] = label_error
            return out
        c = self._c(tag)
        p = self.p
        hold = {}

        def w(scr):
            hold["s"] = scr
            p.write(scr, b"\0" * 0x200)
            p.write(scr + 0x208, struct.pack("<i", c.tag_id))
            p.write(scr + 0x100, name.encode() + b"\0")
            return (scr + 0x180, scr + 0x100, scr + 0x200, 0)
        r = self.call(FN_DYNAMIC_VAR, scratch_writer=w, signed=True)
        raw = p.read(hold["s"] + 0x180, 16)
        return {"rax": r, "out": struct.unpack("<qq", raw)}
