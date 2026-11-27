"""名称与句柄解析（含 charhandle/divhandle）、通用命令下发、数据库浏览与底层命令封装 Mixin。"""
from __future__ import annotations

import offsets as O
import struct

from ._shared import (
    BT_KEY,
    CALLING_DIR,
    CH_KEY,
    CO_ARMIES,
    CO_ARMY_UPGRADES,
    CO_CONT_FOCUS_PAL,
    CO_COUNTRY_INTEL,
    CO_DECISIONS,
    CO_DIPLOMACY,
    CO_FACTION,
    CO_FOCUS_PROGRESS,
    CO_FOCUS_TREE,
    CO_INTEL_AGENCY,
    CO_OCCUPATION,
    CO_OPERATIONS,
    CO_POLITICS,
    CO_PRODUCTION,
    CO_PROGRAM_STATUS,
    CO_PROJECT_POOL,
    CO_RESOURCES,
    CO_TECH_STATUS,
    DECISION_NAME,
    ET_ID,
    ET_NAME,
    HANDLE_OFFSETS,
    IDEA_NAME,
    NF_KEY,
    TT_NAME,
    _VERIFIED_NAMES,
    _arr,
    item_name,
)

class ResolverMixin:
    """名称与句柄解析（含 charhandle/divhandle）、通用命令下发、数据库浏览与底层命令封装 Mixin。"""

    DATABASE_ARRAYS = {
        "war_goals": ("CWarGoalDatabase", 0x40, None),
        "occupation_laws": ("COccupationLawDatabase", 0x58, None),
        "buildings": ("CBuildingDatabase", O.BLD_DB_ITEMS, BT_KEY),
        "technologies": ("CTechnologyDatabase", 0x48, TT_NAME),
        "ideas": ("CIdeaDatabase", 0x68, IDEA_NAME),
        "decisions": ("CDecisionDatabase", 0x28, DECISION_NAME),
        "equipment": ("CEquipmentDatabase", 0x68, ET_NAME),
        "focuses": ("CNationalFocusDatabase", 0x58, NF_KEY),
        "character_templates": ("CCharacterTemplateDatabase", 0x28, None),
        "scripted_diplomatic_actions": ("CScriptedDiplomaticActionTemplateDatabase", 0x160, None),
    }

    REF_TYPES = {
        "focus": "Focus key (hoi4_focus_list / hoi4_selectable_focuses)",
        "tech": "Technology name (hoi4_research_available)",
        "idea": "Idea, law or advisor key (hoi4_ideas_available)",
        "decision": "Decision key (hoi4_decisions)",
        "building": "Building key (hoi4_building_types) -> id",
        "equipment": "Equipment name (hoi4_equipment_types) -> id",
        "country": "Country tag (GER/FRA/...) -> tag id",
        "state": "State id",
        "character": "Character key (hoi4_characters / hoi4_generals)",
        "template": "Division template: index or name (hoi4_division_templates)",
        "army": "Army index (hoi4_armies)",
        "fleet": "Fleet index or name (hoi4_fleets)",
        "subunit": "Battalion type key (hoi4_subunit_types)",
        "wargoal": "War goal name (hoi4_list_database war_goals)",
        "occupationlaw": "Occupation law name",
        "int": "A plain integer",
        "ptr": "Ham pointer (0x...)",
            "charhandle": "Character handle (write as i64: i64@<offset>)",
        "divhandle":  "Division handle (write as i64)",
    }

    OBJECT_BY_NAMEOFF = {
        0x08: "CTechnologyTemplate / CEquipmentType / CBuilding (tech: / equipment: / building:)",
        0x10: "CEquipmentType / CSubUnitDefinition (equipment: / subunit:)",
        0x18: "CNationalFocus / CIdea (focus: / idea:)",
        0x20: "CCharacter / CReferencedDivisionTemplate (character: / template:)",
        0xD8: "CFleet (fleet:)",
        0x118: "CDecision (decision:)",
        0x248: "CBuildingTemplate (building:)",
    }

    KIND_HELP = {
        "handle": "A unit or character HANDLE: 'charhandle:GER_x' or 'divhandle:<index>' "
                  "(or get one with hoi4_handle; written as an i64)",
        "ptr": "An object pointer: 'focus:', 'tech:', 'idea:', 'decision:', 'template:', "
               "a reference such as 'subunit:', 'fleet:', 'character:' or 'wargoal:'",
        "int": "Integer",
        "bool": "0 / 1",
        "deref": "The object's first int32, usually a CCountryTag. Pass 'country:FRA'",
    }

    COUNTRY_SUBSYSTEMS = {
        "technology": CO_TECH_STATUS, "production": CO_PRODUCTION,
        "army_upgrades": CO_ARMY_UPGRADES, "diplomacy": CO_DIPLOMACY,
        "politics": CO_POLITICS, "decisions": CO_DECISIONS,
        "intelligence_agency": CO_INTEL_AGENCY, "country_intel": CO_COUNTRY_INTEL,
        "focus_tree": CO_FOCUS_TREE, "focus_progress": CO_FOCUS_PROGRESS,
        "continuous_focus_palette": CO_CONT_FOCUS_PAL, "faction": CO_FACTION,
        "resources": CO_RESOURCES, "occupation": CO_OCCUPATION,
        "operations": CO_OPERATIONS, "project_pool": CO_PROJECT_POOL,
        "program_status": CO_PROGRAM_STATUS,
    }

    def player_commands(self, filter_text="", with_params=True):
        """All 370 player commands, with their parameter layouts."""
        cat = self.cmd.catalog()
        ctors = self.cmd.ctor_catalog()
        f = filter_text.lower()
        out = {}
        for k, v in sorted(cat.items()):
            if f and f not in k.lower():
                continue
            rec = {"size": v.get("size"),
                   "type_id": hex(v["type_id"]) if v["type_id"] else None,
                   "verified": k in _VERIFIED_NAMES,
                   "has_params": bool(ctors.get(k, {}).get("fields"))}
            if with_params:
                try:
                    rec["parameters"] = self.command_help(k)["parameters"]
                except Exception:
                    rec["parameters"] = []
            out[k] = rec
        return {"count": len(out),
                "note": "use hoi4_send_command to fill in the parameters; "
                        "values can be given by name, such as 'focus:xxx' / 'tech:xxx' / 'charhandle:xxx'",
                "commands": out}

    def list_database(self, which: str, filter_text="", limit=200):
        """List any of the game's item databases by name."""
        if which not in self.DATABASE_ARRAYS:
            return {"error": "unknown database",
                    "available": sorted(self.DATABASE_ARRAYS)}
        cls, off, nameoff = self.DATABASE_ARRAYS[which]
        db = self.oi.singleton(cls)
        if not db:
            return {"error": "%s 在内存里找不到（类名可能拼错，或该对象还没创建）" % cls}
        p = self.p
        out = []
        f = filter_text.lower()
        for it in _arr(p, db, off):
            nm = item_name(p, it + nameoff) if nameoff is not None else None
            if nm is None:
                for o in range(0, 0x300, 8):
                    v = item_name(p, it + o)
                    if v and v.isprintable() and 2 < len(v) < 48 and not v.startswith("/"):
                        nm = v
                        break
            if nm and (not f or f in nm.lower()):
                out.append(nm)
            if len(out) >= limit:
                break
        return {"database": which, "count": len(out), "items": out}

    def browse_object(self, ptr, span=0x100):
        """ADVANCED: dump an unknown object's fields, resolving classes and names."""
        p = self.p
        if isinstance(ptr, str):
            ptr = int(ptr, 16)
        rows = []
        d = p.try_read(ptr, span)
        if not d:
            return {"error": "okunamadi"}
        for i in range(0, len(d) - 8, 8):
            v = struct.unpack_from("<Q", d, i)[0]
            row = {"off": hex(i), "u64": hex(v), "i32": struct.unpack_from("<i", d, i)[0]}
            c = self.oi.class_of(v) if 0x10000 < v < 0x7FFFFFFFFFFF else None
            if c:
                row["class"] = c
            s = item_name(p, ptr + i)
            if s and s.isprintable() and 1 < len(s) < 60:
                row["str"] = s
            rows.append(row)
        return {"ptr": hex(ptr), "class": self.oi.class_of(ptr), "fields": rows}

    def country_subsystems(self, tag=None):
        """Every subsystem of CCountry: class plus the arrays inside it.
        To explore an unwrapped mechanic, continue from here with hoi4_browse_object."""
        from probe import describe
        p = self.p
        c = self._c(tag)
        out = {}
        unsupported = []
        for name, off in sorted(self.COUNTRY_SUBSYSTEMS.items()):
            # 2026-11-22 (P2-1): PE 上未验证的偏移为 None，跳过而不是做 int + None 抛 TypeError。
            if off is None:
                unsupported.append(name)
                out[name] = {"offset": None, "supported": False,
                             "reason": "offset unverified on this build"}
                continue
            try:
                ptr = p.u64(c.ptr + off)
            except Exception as e:
                out[name] = {"offset": hex(off), "error": repr(e)}
                continue
            if not ptr:
                out[name] = None
                continue
            try:
                arrs = [{"offset": r["off"], "size": r["size"], "elem": r["elem"]}
                        for r in describe(p, ptr, self.oi, span=0x300) if r["size"]]
            except Exception:
                arrs = []
            out[name] = {"offset": hex(off), "ptr": hex(ptr),
                         "class": self.oi.class_of(ptr), "arrays": arrs[:12]}
        if unsupported:
            out["_unsupported"] = unsupported
        return out

    def resolve(self, spec, tag=None):
        """Turn a 'kind:value' reference into a pointer or id.
        A number, or 0x..., is returned unchanged."""
        if isinstance(spec, str) and spec.startswith(("charhandle:", "divhandle:")):
            kind, _, val = spec.partition(":")
            h = (self.character_handle(val, tag) if kind == "charhandle"
                 else self.division_handle(val, tag))
            return (h["id"] << 32) | (h["type"] & 0xFFFFFFFF)   # i64: [tip|id]
        if isinstance(spec, int):
            return spec
        s = str(spec).strip()
        if s.startswith("0x"):
            return int(s, 16)
        if ":" not in s:
            return int(s)
        kind, _, val = s.partition(":")
        kind = kind.lower()
        p = self.p
        c = self._c(tag)
        if kind == "int":
            return int(val)
        if kind == "ptr":
            return int(val, 0)
        if kind == "focus":
            f = self.find_focus(val, c.tag)
            if not f:
                for x in self.continuous_focuses(c.tag):
                    if x["key"] == val:
                        return int(x["ptr"], 16)
            if not f:
                raise KeyError("no such focus: %s" % val)
            return f
        if kind == "tech":
            self._load_tech_db()
            t = (self._tech_by_name or {}).get(val)
            if not t:
                raise KeyError("no such technology: %s" % val)
            return t
        if kind == "idea":
            for i in self.available_ideas(c.tag):
                if i["key"] == val:
                    return i["ptr"]
            raise KeyError("no such idea: %s" % val)
        if kind == "decision":
            for which in ("visible", "active", "all"):
                for d in self.decisions(c.tag, which, rich=False):
                    if d["key"] == val:
                        return d["ptr"]
            raise KeyError("no such decision: %s" % val)
        if kind == "building":
            self._load_buildings()
            b = self._bld_by_key.get(val)
            if not b:
                raise KeyError("no such building: %s" % val)
            return b["id"]
        if kind == "equipment":
            self._load_equipment()
            t = self._eq_by_name.get(val)
            if not t:
                raise KeyError("no such equipment: %s" % val)
            # PE builds map name -> archetype id, Linux names map to the template ptr.
            return t if isinstance(t, int) else p.i32(t + ET_ID)
        if kind == "country":
            self._load_tags()
            v = val.upper()
            if v not in self._tags:
                raise KeyError("no such tag: %s" % v)
            return self._tags.index(v)
        if kind == "state":
            return int(val)
        if kind == "character":
            for ch in self.characters(c.tag, limit=5000)["characters"]:
                if ch["key"] == val or ch["name"] == val:
                    return int(ch["ptr"], 16)
            raise KeyError("no such character: %s" % val)
        if kind == "template":
            tl = self.division_templates(c.tag)
            if val.isdigit():
                return int(tl[int(val)]["ptr"], 16)
            for t in tl:
                if t["name"] == val:
                    return int(t["ptr"], 16)
            raise KeyError("no such template: %s" % val)
        if kind == "army":
            al = _arr(p, c.ptr, CO_ARMIES)
            return al[int(val)]
        if kind == "fleet":
            fl = self.fleets(c.tag)
            if val.isdigit():
                return int(fl[int(val)]["ptr"], 16)
            for f in fl:
                if f["name"] == val:
                    return int(f["ptr"], 16)
            raise KeyError("no such fleet: %s" % val)
        if kind == "subunit":
            for s in self.subunit_types(c.tag):
                if s["key"] == val:
                    return int(s["ptr"], 16)
            raise KeyError("no such battalion: %s" % val)
        if kind in ("wargoal", "occupationlaw"):
            if kind == "wargoal" and hasattr(self, "_win_wargoal_types_map"):
                wmap = self._win_wargoal_types_map()
                if wmap:
                    if val in wmap:
                        return wmap[val]
                    raise KeyError("no such wargoal: %s (valid: %s)" % (val, ", ".join(sorted(wmap))))
            which = "war_goals" if kind == "wargoal" else "occupation_laws"
            cls, off, _ = self.DATABASE_ARRAYS[which]
            db = self.oi.singleton(cls)
            if db:
                for it in _arr(p, db, off):
                    nm = None
                    for o in range(0, 0x300, 8):
                        v2 = item_name(p, it + o)
                        if v2 and v2.isprintable() and 2 < len(v2) < 48:
                            nm = v2
                            break
                    if nm == val:
                        return it
            raise KeyError("no such %s: %s" % (kind, val))
        raise KeyError("unknown reference kind: %s (valid: %s)"
                       % (kind, sorted(self.REF_TYPES)))

    def send_command(self, name: str, fields: dict = None, tag=None, force=False):
        """Send ANY player command.
        field values may be 'focus:GER_x', 'tech:y', 'country:FRA', 'state:51',
        'character:GER_z', 'ptr:0x...' or a plain number.

        SAFETY: writing raw offsets into a command whose layout is unverified can
        crash the game, so that case requires force=True."""
        c = self._c(tag)
        f = {}
        for k, v in (fields or {}).items():
            f[k] = self.resolve(v, c.tag)
        spec0, src0 = self.cmd.spec_for(name)
        raw_keys = [k for k in f if "@" in str(k) or str(k).startswith("0x")]
        known_offs = {fl[0] for fl in spec0["fields"]}
        risky = [k for k in raw_keys
                 if (int(str(k).split("@")[-1], 16) if "0x" in str(k) else -1) not in known_offs]
        if risky and not force:
            return {"ok": False, "blocked": True,
                    "error": "This command's field layout is unverified; writing to unknown offsets "
                             "can crash the game: %s" % risky,
                    "hint": "Check the parameters with hoi4_command_help. "
                            "Pass force=true to try anyway.",
                    "known_offsets": [hex(o) for o in sorted(known_offs)]}
        # do the pointer fields actually point at a real object?
        for fl in spec0["fields"]:
            if fl[1] != "<Q":
                continue
            key = fl[2]
            val = f.get(key)
            if val is None:
                continue
            if val and not (0x10000 < val < 0x7FFFFFFFFFFF):
                return {"ok": False, "blocked": True,
                        "error": "field %s is not a valid object pointer: %#x" % (key, val)}
        spec, _src = self.cmd.spec_for(name)
        has_tag = any(fl[2] == "tag" for fl in spec["fields"])
        if has_tag and "tag" not in f and "0x24" not in f:
            f["tag"] = c.tag_id
        with self:
            r = self.cmd.send_raw(name, f)
        self._settle()
        return r

    def command_help(self, name: str):
        """Explain a player command's parameters in a form a caller can act on."""
        spec, src = self.cmd.spec_for(name)
        key = spec.get("name", name)
        ctors = self.cmd.ctor_catalog().get(key, {})
        bykind = {f["offset"]: f for f in ctors.get("fields", [])}
        auto = self.cmd.catalog().get(key, {})
        nameoffs = {o: n for o, k, n in auto.get("fields", [])}
        params = []
        for off, fmt, fname in spec["fields"]:
            if fname == "tag":
                params.append({"name": "tag", "offset": hex(off), "type": "CCountryTag",
                               "note": "filled in automatically (the player's country); "
                                       "use 'country:FRA' for another country"})
                continue
            f = bykind.get(off, {})
            kind = f.get("kind", "")
            if kind.startswith("field"):
                kind = "ptr"
            hint = self.OBJECT_BY_NAMEOFF.get(nameoffs.get(off, 0))
            params.append({
                "name": fname, "offset": hex(off),
                "bytes": {"<q": 8, "<Q": 8, "<i": 4, "<h": 2, "<B": 1}.get(fmt, 8),
                "kind": kind or ("ptr" if fmt in ("<Q", "<q") else "int"),
                "ctor_arg": f.get("arg"),
                "expects": hint or self.KIND_HELP.get(kind, "number"),
            })
        return {"command": key, "source": src, "size": spec.get("size"),
                "parameters": params,
                "usage": "hoi4_send_command(name='%s', fields={'arg0': '<value>', ...})" % key,
                "reference_types": self.REF_TYPES}

    def handle_of(self, ptr):
        """Return an object's (type, id) handle."""
        if isinstance(ptr, str):
            ptr = int(ptr, 16)
        cls = self.oi.class_of(ptr)
        off = HANDLE_OFFSETS.get(cls, 0x08)
        a, b = struct.unpack("<ii", self.p.read(ptr + off, 8))
        return {"class": cls, "type": a, "id": b, "ptr": hex(ptr)}

    def character_handle(self, key, tag=None):
        """Packed CCharacter handle for a character key.

        2026-11-17: `hoi4_hire_advisor` failed with "no such character:
        GER_walther_funk" while `hoi4_advisors` listed him, because the country
        pool (CCountryCharacters+0x10) does not contain every advisor and the old
        code only looked there. Exact match -> case-insensitive match -> whole
        object index, in that order.
        """
        p = self.p
        want = str(key).strip()
        low = want.lower()

        def _key(ch):
            k = item_name(p, ch + CH_KEY)
            if not k:
                try:
                    k, _nm = self._extract_char_key_and_name(ch)
                except Exception:
                    k = None
            return k

        chars = self.all_characters(tag)
        for ch in chars:
            if _key(ch) == want:
                return self.handle_of(ch)
        for ch in chars:
            k = _key(ch)
            if k and k.lower() == low:
                return self.handle_of(ch)
        try:
            pool = list(self.oi.instances("CCharacter"))
        except Exception:
            pool = []
        for ch in pool:
            k = _key(ch)
            if k and (k == want or k.lower() == low):
                return self.handle_of(ch)
        raise KeyError("no such character: %s" % key)

    def division_handle(self, index_or_ptr, tag=None):
        """解析师对象句柄：既支持数组下标（如 0 / '3'），也支持十六进制/整数 CArmy* 指针（如 '0x1b26...'）。"""
        divs = _arr(self.p, self._c(tag).ptr, CO_ARMIES)
        is_hex = isinstance(index_or_ptr, str) and index_or_ptr.strip().lower().startswith("0x")
        if isinstance(index_or_ptr, str):
            val = int(index_or_ptr.strip(), 0)
        else:
            val = int(index_or_ptr)
        if is_hex or val >= 0x10000:
            res = self.handle_of(val)
            try:
                res["index"] = divs.index(val)
            except ValueError:
                pass
            return res
        if not (0 <= val < len(divs)):
            raise IndexError("division index %d out of range (0..%d)" % (val, len(divs) - 1))
        res = self.handle_of(divs[val])
        res["index"] = val
        return res

    def _win_action_classes(self):
        """PE fallback for diplomatic_action_classes().

        The Linux path walks Itanium RTTI + .rela.dyn to build the *exact*
        CDiplomaticAction subclass graph. On hoi4.exe that needs the MSVC
        base-class chain, which we have not recovered; instead read the
        TypeDescriptor table that research/windows/rtti_win.py already dumps
        (15141 classes) and keep `.?AVC*Action@@`. That set is a SUPERSET —
        it also contains effect/trigger classes — so the answer is labelled
        as such rather than pretending to be the hierarchy.
        """
        import os, re
        import safe_cache
        here = CALLING_DIR
        pkl = os.path.join(here, "..", "research", "windows", "rtti_win.pkl")
        if not safe_cache.exists(pkl):
            return {"ok": False, "source": "pe-rtti",
                    "error": "rtti_win cache is missing; run "
                             "research/windows/rtti_win.py once to regenerate it"}
        d = safe_cache.load(pkl)
        seen = {}
        for key, vts in (d.get("classes") or {}).items():
            m = re.match(r"^\.\?AV(.+)@@$", key)
            if not m:
                continue
            name = m.group(1)
            if not re.match(r"^C[A-Za-z0-9_]*Action$", name):
                continue
            seen.setdefault(name, [hex(v) for v in (vts or [])])
        names = sorted(seen)
        return {"ok": True, "source": "pe-rtti", "count": len(names),
                "classes": [{"name": n, "vtables": seen[n]} for n in names],
                "note": "Enumerated from the MSVC TypeDescriptor table. This is a "
                        "superset of the real CDiplomaticAction subclasses: the "
                        "base-class chain was not recovered, so effect/trigger "
                        "classes whose name also ends in 'Action' are included."}

    def diplomatic_action_classes(self):
        """The game's 47 diplomatic action classes (CDiplomaticAction subclasses).
        CDiplomaticActionCommand carries one of them at +0x28."""
        return self._win_action_classes()

    def _sc(self, name, tag=None, **kw):
        return self.send_command(name, kw, tag)

    def create_division_template(self, tag=None, **kw):
        return self._sc("CCreateDivisionTemplateCommand", tag, **kw)

    def update_division_template(self, tag=None, **kw):
        return self._sc("CUpdateDivisionTemplateCommand", tag, **kw)

    def set_army_template(self, tag=None, **kw):
        return self._sc("CSetArmyTemplateCommand", tag, **kw)

    def queue_unit_action(self, tag=None, **kw):
        return self._sc("CQueueUnitActionCommand", tag, **kw)

    def set_army_leader(self, tag=None, **kw):
        return self._sc("CSetArmyLeaderCommand", tag, **kw)

    def learn_trait(self, tag=None, **kw):
        return self._sc("CLearnTraitCommand", tag, **kw)

    def promote_unit_leader(self, tag=None, **kw):
        return self._sc("CPromoteUnitLeaderCommand", tag, **kw)

    def create_unit_leader(self, tag=None, **kw):
        return self._sc("CCreateUnitLeaderCommand", tag, **kw)

    def order_new_front(self, tag=None, **kw):
        return self._sc("COrderNewFrontCommand", tag, **kw)

    def order_new_fallback(self, tag=None, **kw):
        return self._sc("COrderNewFallbackCommand", tag, **kw)

    def order_execute(self, tag=None, **kw):
        return self._sc("COrderExecuteCommand", tag, **kw)

    def order_delete(self, tag=None, **kw):
        return self._sc("COrderDeleteCommand", tag, **kw)

    def order_delete_all(self, tag=None, **kw):
        return self._sc("COrderDeleteAllCommand", tag, **kw)

    def order_set_path(self, tag=None, **kw):
        return self._sc("COrderSetPathCommand", tag, **kw)

    def order_assign(self, tag=None, **kw):
        return self._sc("COrderAssignCommand", tag, **kw)

    def order_add_complete_plan(self, tag=None, **kw):
        return self._sc("COrderAddNewCompletePlanCommand", tag, **kw)

    def army_group(self, tag=None, **kw):
        return self._sc("CArmyGroupCommand", tag, **kw)

    def deploy_air_wing(self, tag=None, **kw):
        return self._sc("CDeployAirWingCommand", tag, **kw)

    def air_set_mission(self, tag=None, **kw):
        return self._sc("CStratAirSetMissionCommand", tag, **kw)

    def air_enable_mission(self, tag=None, **kw):
        return self._sc("CStratAirEnableMissionCommand", tag, **kw)

    def create_fleet(self, tag=None, **kw):
        return self._sc("CCreateFleetCommand", tag, **kw)

    def naval_mission_set_type(self, tag=None, **kw):
        return self._sc("CNavalMissionSetTypeCommand", tag, **kw)

    def naval_mission_set_regions(self, tag=None, **kw):
        return self._sc("CNavalMissionSetRegionsCommand", tag, **kw)

    def set_fleet_home_base(self, tag=None, **kw):
        return self._sc("CSetFleetHomeBaseCommand", tag, **kw)

    def diplomatic_action(self, tag=None, **kw):
        return self._sc("CDiplomaticActionCommand", tag, **kw)

    def incoming_diplomatic_action(self, tag=None, **kw):
        return self._sc("CIncomingDiplomaticActionActingCommand", tag, **kw)

    def create_faction(self, tag=None, **kw):
        return self._sc("CCreateFactionCommand", tag, **kw)

    def release_country(self, tag=None, **kw):
        return self._sc("CReleaseCountryCommand", tag, **kw)

    def set_occupation_policy(self, tag=None, **kw):
        return self._sc("CSetOccupationPolicyCommand", tag, **kw)

    def add_advisor(self, tag=None, **kw):
        return self._sc("CAddAdvisorCommand", tag, **kw)

    def remove_advisor(self, tag=None, **kw):
        return self._sc("CRemoveAdvisorCommand", tag, **kw)

    def replace_idea_raw(self, tag=None, **kw):
        return self._sc("CReplaceIdeaCommand", tag, **kw)

    def promote_to_country_leader(self, tag=None, **kw):
        return self._sc("CPromoteToCountryLeaderCommand", tag, **kw)

    def set_industrial_org_task(self, tag=None, **kw):
        return self._sc("CSetIndustrialOrganisationTaskCommand", tag, **kw)

    def set_design_team(self, tag=None, **kw):
        return self._sc("CSetDesignTeamCommand", tag, **kw)

    def recruit_scientist(self, tag=None, **kw):
        return self._sc("CRecruitScientistCommand", tag, **kw)

    def attach_scientist(self, tag=None, **kw):
        return self._sc("CAttachScientistCommand", tag, **kw)

    def start_project(self, tag=None, **kw):
        return self._sc("CStartProjectCommand", tag, **kw)

    def stop_project(self, tag=None, **kw):
        return self._sc("CStopProjectCommand", tag, **kw)

    def create_intelligence_agency(self, tag=None, **kw):
        return self._sc("CIntelligenceAgencyCreationCommand", tag, **kw)

    def recruit_operative(self, tag=None, **kw):
        return self._sc("CRecruitOperativeCommand", tag, **kw)

    def launch_operation(self, tag=None, **kw):
        return self._sc("CLaunchOperationCommand", tag, **kw)

    def add_production_line(self, tag=None, **kw):
        return self._sc("CAddProductionLineCommand", tag, **kw)

    def set_production_priority(self, tag=None, **kw):
        return self._sc("CSetProductionLinePriorityCommand", tag, **kw)

    def update_equipment_variant(self, tag=None, **kw):
        return self._sc("CUpdateEquipmentVariantCommand", tag, **kw)

    def convert_factory(self, tag=None, **kw):
        return self._sc("CConvertFactoryCommand", tag, **kw)

    def set_garrison_template(self, tag=None, **kw):
        return self._sc("CSetCountryGarrisonTemplateCommand", tag, **kw)

    def set_supply_capital_node(self, tag=None, **kw):
        return self._sc("CSetSupplyCapitalNodeCommand", tag, **kw)

    def select_event_option(self, tag=None, **kw):
        return self._sc("CSelectEventOptionCommand", tag, **kw)
