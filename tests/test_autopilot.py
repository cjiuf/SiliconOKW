# -*- coding: utf-8 -*-
"""Standing Orders v1（calling/autopilot.py + TurnLoop + TurnEngine）全覆盖离线单测。"""
import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CALLING = os.path.join(ROOT, "calling")
MCP_DIR = os.path.join(ROOT, "mcp")
for _p in (CALLING, MCP_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import api as API
import autopilot as AP
import diplomacy as DIP
import events_win as EW
import reddots as RD
import session as SESS
import turnengine as TE
import turnloop as TL


class _FakeProc:
    def __init__(self, pid=42424):
        self.pid = pid
        self.base = 0x140000000
        self.h = None  # 无真实 OS 句柄 -> 离线测试自动跳过前台窗口拦截

    def read(self, addr, size):
        return b"MZ\x90\x00"[:size]

    def u64(self, addr):
        return 0x20000000


class _FakeCountry:
    def __init__(self, g, tag="GER"):
        self.g = g
        self.tag = tag
        self.ptr = 0x1000

    @property
    def political_power(self):
        return self.g.pp

    @property
    def civilian_factories(self):
        return self.g.civ

    @property
    def military_factories(self):
        return self.g.mil

    @property
    def naval_dockyards(self):
        return self.g.dock

    def manpower(self):
        return self.g.manpower_val

    @property
    def experience(self):
        return dict(self.g.xp_map)

    def summary(self):
        return {
            "tag": self.tag,
            "name": "Germany",
            "political_power": self.g.pp,
            "political_power_daily": self.g.pp_daily,
            "stability": self.g.stability_val,
            "war_support": self.g.war_support_val,
            "manpower": self.g.manpower_val,
            "fuel": self.g.fuel_val,
            "experience": dict(self.g.xp_map),
            "civilian_factories": self.g.civ,
            "military_factories": self.g.mil,
            "naval_dockyards": self.g.dock,
        }


class _FakeGame:
    """支持全子系统读写状态流转的可编程离线 HOI4 桩对象。"""

    def __init__(self, pid=42424, tag="GER"):
        self.p = _FakeProc(pid=pid)
        self._tag = tag
        self._country = _FakeCountry(self, tag=tag)
        self.ymd = (1936, 1, 1)
        self.paused = True
        self.speed = 2

        self.pp = 100.0
        self.pp_daily = 1.5
        self.stability_val = 0.70
        self.war_support_val = 0.60
        self.manpower_val = 600000.0
        self.fuel_val = 25000.0
        self.civ = 35
        self.mil = 20
        self.dock = 10
        self.wt_val = None
        self.xp_map = {"army": 0.0, "navy": 0.0, "air": 0.0}
        self.templates_map = {
            "Infanterie-Division": {
                "name": "Infanterie-Division",
                "line": {"infantry": 9},
                "support": {"engineer": 1, "artillery": 1},
            },
        }
        self.unlocked_doctrines = []
        self.fail_research_techs = set()
        self.fail_research_slots = set()
        self.fail_construction_states = set()
        self.add_construction_calls = []

        self.ruling_ideo = "fascism"
        self.active_ideas = ["partial_economic_mobilisation", "limited_conscription", "export_focus"]
        self.enemies = []
        self.peace_open = False
        self.wargoals = []
        self.justifying_list = []

        # 国策
        self.cur_focus = None
        self.done_focuses = set()
        self.all_focus_keys = [
            "GER_remilitarize_the_rhineland",
            "GER_the_four_year_plan",
            "GER_anschluss",
            "GER_autarky_efforts",
            "GER_oppose_hitler",
        ]
        self.selectable_focus_rows = [
            {"key": "GER_remilitarize_the_rhineland", "id": "GER_remilitarize_the_rhineland",
             "tier": "S0_opening", "score": 1850, "reason": "opening"},
            {"key": "GER_the_four_year_plan", "id": "GER_the_four_year_plan",
             "tier": "S0_opening", "score": 1840, "reason": "industry"},
            {"key": "GER_autarky_efforts", "id": "GER_autarky_efforts",
             "tier": "B_industry", "score": 150, "reason": "industry"},
        ]
        self.blocked_focuses = ["GER_anschluss (wait: divs 34/54, year 1936/1937)"]
        self.fail_focus_set = False

        # 科研
        self._tech_by_name = {
            "basic_machine_tools": 1,
            "construction1": 2,
            "electronic_mechanical_engineering": 3,
            "improved_machine_tools": 4,
            "concentrated_industry": 5,
            "dispersed_industry": 6,
            "advanced_machine_tools": 7,
            "radio": 8,
        }
        self.tech_meta_override = {
            "basic_machine_tools": {"start_year": 1936, "cost_days": 150.0, "category": "industry", "xor": [], "score": 480},
            "construction1": {"start_year": 1936, "cost_days": 150.0, "category": "industry", "xor": [], "score": 470},
            "electronic_mechanical_engineering": {"start_year": 1936, "cost_days": 120.0, "category": "computing", "xor": [], "score": 460},
            "improved_machine_tools": {"start_year": 1937, "cost_days": 150.0, "category": "industry", "xor": [], "score": 100},
            "concentrated_industry": {"start_year": 1936, "cost_days": 150.0, "category": "industry", "xor": ["dispersed_industry"], "score": 450},
            "dispersed_industry": {"start_year": 1936, "cost_days": 150.0, "category": "industry", "xor": ["concentrated_industry"], "score": 440},
            "advanced_machine_tools": {"start_year": 1939, "cost_days": 150.0, "category": "industry", "xor": [], "score": -1000},
            "radio": {"start_year": 1936, "cost_days": 100.0, "category": "computing", "xor": [], "score": 300},
        }
        self.researched_set = set()
        self.slots = [
            {"slot": 0, "researching": None, "slot_busy": False, "slot_done": False},
            {"slot": 1, "researching": None, "slot_busy": False, "slot_done": False},
        ]

        # 州与建造
        self.state_rows = {
            64: {"ok": True, "id": 64, "name": "Brandenburg", "slots_free": 3, "is_core_of_owner": True, "buildings": {"infrastructure": 5}},
            59: {"ok": True, "id": 59, "name": "Hannover", "slots_free": 7, "is_core_of_owner": True, "buildings": {"infrastructure": 3}},
            52: {"ok": True, "id": 52, "name": "Oberbayern", "slots_free": 5, "is_core_of_owner": True, "buildings": {"infrastructure": 4}},
            99: {"ok": True, "id": 99, "name": "FullState", "slots_free": 0, "is_core_of_owner": True, "buildings": {"infrastructure": 5}},
        }
        self.const_queue = []

        # 产线与库存
        self.prod_lines = [
            {"index": 0, "equipment": "infantry_equipment_0", "factories": 8, "class": "CMilitaryProductionLine",
             "obsolete": True, "upgrade_to": "infantry_equipment_1", "payback_days": 30, "output_units_per_day": 50.0},
            {"index": 1, "equipment": "artillery_equipment_1", "factories": 2, "class": "CMilitaryProductionLine",
             "obsolete": False},
            {"index": 2, "equipment": "light_tank_chassis_1", "factories": 2, "class": "CMilitaryProductionLine",
             "obsolete": True, "upgrade_to": "light_tank_chassis_2", "payback_days": 25, "output_units_per_day": 10.0},
            {"index": 3, "equipment": "fighter_equipment_0", "factories": 2, "class": "CMilitaryProductionLine",
             "obsolete": False},
            {"index": 4, "equipment": "submarine_1", "factories": 8, "class": "CNavalProductionLine"},
        ]
        self.stockpile_totals = {
            "infantry_equipment_0": 5000.0,  # 100 天库存 >= 30 天 -> 允许换代
            "light_tank_chassis_1": 50.0,    # 5 天库存 < 30 天 -> 拒绝换代保护前线
        }
        self.dock_alloc_called = 0

        # 部队、传送带与将领
        self.conveyor_rows = [{"ptr": "0xc01", "progress": 1.0, "deployment_state": "ready"}]
        self.deploy_called = 0
        self.unassigned = [
            {"ptr": "0xd1", "template": "Panzer-Division"},
            {"ptr": "0xd2", "template": "Infanterie-Division"},
            {"ptr": "0xd3", "template": "Fallschirmjager-Division"},  # 伞兵必须豁免！
        ]
        self.order_group_rows = [
            {"ptr": "0xa1", "divisions": 10, "is_army_group": False, "leader_ptr": "0x2000", "role": "default"},
        ]
        self.generals_pool = [
            {"key": "GER_inf_gen", "leader_ptr": "0x2001", "rank": "corps_commander",
             "attack": 4, "defense": 4, "planning": 4, "logistics": 4, "skill": 4, "traits": ["infantry_officer"]},
            {"key": "GER_heinz_guderian", "leader_ptr": "0x2002", "rank": "corps_commander",
             "attack": 4, "defense": 3, "planning": 4, "logistics": 3, "skill": 4, "traits": ["panzer_leader", "trickster"]},
        ]

        # 顾问与法案目录
        self.catalog_items = [
            {"key": "war_economy", "kind": "law", "slot": "economy", "cost": 150, "active": False, "available": True, "mutually_exclusive_with": ["partial_economic_mobilisation"]},
            {"key": "partial_economic_mobilisation", "kind": "law", "slot": "economy", "cost": 150, "active": True, "available": True, "mutually_exclusive_with": ["war_economy"]},
            {"key": "GER_martin_bormann", "kind": "advisor", "slot": "political_advisor", "cost": 150, "active": False, "available": True, "traits": ["silent_workhorse"], "mutually_exclusive_with": []},
            {"key": "GER_hjalmar_schacht", "kind": "advisor", "slot": "political_advisor", "cost": 150, "active": False, "available": True, "traits": ["captain_of_industry"], "mutually_exclusive_with": []},
        ]
        self.hired_advisors = set()
        self.taken_decisions = set()
        self.events_pending = []
        self.events_answered = []

    def advance_days(self, days):
        raw = API.parts_to_date(*self.ymd) + int(days) * 24
        yr, mo, day, _hr = API.date_to_parts(raw)
        self.ymd = (yr, mo, day)
        return self.date()

    def date(self):
        raw = API.parts_to_date(*self.ymd)
        return {
            "year": self.ymd[0], "month": self.ymd[1], "day": self.ymd[2],
            "raw": raw, "text": "%04d.%02d.%02d.12" % self.ymd, "source": "fake",
        }

    def date_raw(self):
        return self.date()["raw"]

    def player_tag(self):
        return self._tag

    def player(self):
        return self._country

    def _c(self, tag=None):
        return self._country

    def world_tension(self):
        return self.wt_val

    def game_speed(self):
        return {"speed": self.speed, "ui_speed": self.speed + 1}

    def set_game_speed(self, speed, measure_seconds=None):
        self.speed = min(3, max(0, int(speed)))
        return {"ok": True, "speed": self.speed}

    def politics(self, tag=None):
        return {
            "political_power": self.pp,
            "ruling_ideology": self.ruling_ideo,
            "active_ideas": list(self.active_ideas),
            "available_idea_count": len(self.catalog_items),
        }

    def idea_catalog(self, tag=None, slot=None, filter_text="", include_advisors=True, limit=120):
        items = []
        for it in self.catalog_items:
            row = dict(it)
            row["active"] = (row["key"] in self.active_ideas) or (row["key"] in self.hired_advisors)
            if slot and str(row.get("slot") or "").lower() != str(slot).lower():
                continue
            if filter_text and filter_text.lower() not in row["key"].lower():
                continue
            items.append(row)
        return {"tag": self._tag, "political_power": self.pp, "active_ideas": list(self.active_ideas), "total": len(items), "items": items[:limit]}

    def add_idea(self, key, tag=None):
        rec = next((x for x in self.catalog_items if x["key"] == key), None)
        cost = int((rec or {}).get("cost") or 150)
        if self.pp < cost:
            return {"ok": False, "error": "not enough pp"}
        self.pp -= cost
        for ex in (rec or {}).get("mutually_exclusive_with") or ():
            if ex in self.active_ideas:
                self.active_ideas.remove(ex)
        if key not in self.active_ideas:
            self.active_ideas.append(key)
        return {"ok": True, "idea": key, "pp_after": self.pp}

    def hire_advisor(self, key, tag=None, slot_override=None):
        rec = next((x for x in self.catalog_items if x["key"] == key), None)
        cost = int((rec or {}).get("cost") or 150)
        if self.pp < cost:
            return {"ok": False, "error": "not enough pp"}
        self.pp -= cost
        self.hired_advisors.add(key)
        return {"ok": True, "advisor": key, "pp_after": self.pp}

    def decisions(self, tag=None, which="visible", rich=True):
        return [{"key": "improved_worker_conditions", "mine": True, "cost_pp": 100}]

    def select_decision(self, key, tag=None):
        if self.pp < 50:
            return {"ok": False, "error": "not enough pp"}
        self.pp -= 50
        self.taken_decisions.add(key)
        return {"ok": True, "decision": key}

    def current_focus(self, tag=None):
        return {"key": self.cur_focus, "name": self.cur_focus} if self.cur_focus else None

    def focus_progress(self, tag=None):
        return {"progress": 10.0, "days_left": 60.0, "percent": 14.3} if self.cur_focus else {}

    def completed_focuses(self, tag=None):
        return sorted(self.done_focuses)

    def focuses(self, tag=None):
        return [{"key": k} for k in self.all_focus_keys]

    def selectable_focuses(self, tag=None):
        rows = [r for r in self.selectable_focus_rows if r["key"] not in self.done_focuses and r["key"] != self.cur_focus]
        return {"selectable": rows, "focuses": [r["key"] for r in rows], "blocked": list(self.blocked_focuses)}

    def set_focus(self, key, tag=None):
        if self.fail_focus_set:
            return {"ok": False, "reason": "simulated engine refusal"}
        self.cur_focus = key
        return {"ok": True, "focus": key}

    def researched(self, tag=None):
        return sorted(self.researched_set)

    def research_slots(self, tag=None):
        return [dict(s) for s in self.slots]

    def available_research(self, tag=None):
        return [r["key"] for r in self.available_research_detailed(tag)]

    def available_research_detailed(self, tag=None, max_ahead_years=None):
        cur_frac = self.ymd[0] + (self.ymd[1] - 1) / 12.0 + (self.ymd[2] - 1) / 365.0
        active = {s["researching"] for s in self.slots if s.get("researching") and not s.get("slot_done")}
        out = []
        for k, m in self.tech_meta_override.items():
            if k in self.researched_set or k in active:
                continue
            ahead = round(max(0.0, float(m["start_year"]) - cur_frac), 2)
            if max_ahead_years is not None and ahead > float(max_ahead_years) + 1e-6:
                continue
            out.append({
                "key": k,
                "start_year": m["start_year"],
                "ahead_years": ahead,
                "cost_days": m["cost_days"],
                "category": m["category"],
                "xor": list(m["xor"]),
                "score": m["score"],
            })
        out.sort(key=lambda r: (-r["score"], r["start_year"], r["key"]))
        return out

    def start_research(self, tech, slot=None, tag=None):
        if slot is None:
            free = [s["slot"] for s in self.slots if not s["slot_busy"] or s.get("slot_done")]
            if not free:
                return {"ok": False, "error": "no free slot"}
            slot = free[0]
        if slot in self.fail_research_slots:
            return {
                "ok": False, "tech": tech, "slot": slot,
                "blocked_by": "can_execute_refused", "failure_class": "content",
                "error": "simulated slot_done refusal",
            }
        if tech in self.fail_research_techs:
            return {
                "ok": False, "tech": tech, "slot": slot,
                "blocked_by": "can_execute_refused", "failure_class": "content",
                "error": "CSetResearchCommand::CanExecute refused",
            }
        self.slots[slot] = {"slot": slot, "researching": tech, "slot_busy": True, "slot_done": False}
        return {"ok": True, "tech": tech, "slot": slot}

    def country(self, tag=None):
        return self._country

    def division_templates(self, tag=None):
        return [{"name": k} for k in self.templates_map]

    def template_composition(self, tag=None, only=None):
        if only:
            return [dict(self.templates_map[only])] if only in self.templates_map else []
        return [dict(v) for v in self.templates_map.values()]

    def template_edit(self, name, line=None, support=None, dry_run=False, tag=None):
        if name not in self.templates_map:
            return {"ok": False, "error": "unknown template"}
        if not dry_run:
            self.templates_map[name] = {
                "name": name,
                "line": dict(line or {}),
                "support": dict(support or {}),
            }
            self.xp_map["army"] = max(0.0, float(self.xp_map.get("army", 0.0)) - 35.0)
        return {"ok": True, "template": name}

    def unlock_doctrine(self, doctrine, tag=None):
        self.unlocked_doctrines.append(doctrine)
        return {"ok": True, "doctrine": doctrine}

    def building_types(self):
        return {k: {"id": i} for i, k in enumerate(("industrial_complex", "arms_factory", "dockyard", "infrastructure", "synthetic_refinery"))}

    def states(self, tag=None):
        return [{"id": sid} for sid in self.state_rows]

    def state_detail(self, sid, tag=None):
        return dict(self.state_rows[int(sid)])

    def construction_queue(self, tag=None):
        return [dict(x) for x in self.const_queue]

    def add_construction(self, sid, building, level=1, province=0, tag=None):
        self.add_construction_calls.append((int(sid), building))
        if int(sid) in self.fail_construction_states:
            return {"ok": False, "error": "queue did not grow -- state has no free building slot for this type"}
        sd = self.state_rows.get(int(sid))
        if not sd or int(sd.get("slots_free", 0)) <= 0:
            return {"ok": False, "error": "state full"}
        self.const_queue.append({"index": len(self.const_queue), "state": int(sid), "building": building, "factories": 15 if not self.const_queue else 0})
        return {"ok": True, "state": int(sid), "building": building}

    def production_lines(self, tag=None, include_obsolete=True):
        return [dict(x) for x in self.prod_lines]

    def factory_allocation(self, tag=None):
        mil_used = sum(int(x["factories"]) for x in self.prod_lines if x["class"] == "CMilitaryProductionLine")
        nav_used = sum(int(x["factories"]) for x in self.prod_lines if x["class"] == "CNavalProductionLine")
        return {
            "source": "engine_production_status",
            "civilian_factories": self.civ,
            "military_available": self.mil,
            "military_used": mil_used,
            "military_idle": max(0, self.mil - mil_used),
            "naval_available": self.dock,
            "naval_used": nav_used,
            "naval_idle": max(0, self.dock - nav_used),
        }

    def equipment_stockpile(self, tag=None, limit=40):
        return {"totals_by_equipment": dict(self.stockpile_totals)}

    def set_production_amount(self, line_idx, amount, tag=None, auto_reclaim=True):
        for l in self.prod_lines:
            if l["index"] == int(line_idx):
                l["factories"] = int(amount)
                return {"ok": True, "after": int(amount)}
        return {"ok": False, "error": "line not found"}

    def set_production_line_equipment(self, line_idx, eq, tag=None, dry_run=False):
        for l in self.prod_lines:
            if l["index"] == int(line_idx):
                if not dry_run:
                    l["equipment"] = eq
                    l["obsolete"] = False
                    l["upgrade_to"] = None
                return {"ok": True, "equipment": eq}
        return {"ok": False, "error": "line not found"}

    def _housekeep_dockyards(self, tag=None):
        self.dock_alloc_called += 1
        for l in self.prod_lines:
            if l["class"] == "CNavalProductionLine":
                before = l["factories"]
                add = max(0, self.dock - before)
                if add > 0:
                    l["factories"] = self.dock
                    return [{"line": l["index"], "before": before, "after": self.dock, "granted": add, "ok": True}]
        return []

    def conveyors(self, tag=None):
        return [dict(c) for c in self.conveyor_rows]

    def deploy_ready_conveyors(self, tag=None, min_progress=0.995):
        self.deploy_called += 1
        self.conveyor_rows = []
        return {"ok": True, "deployed": 1}

    def divisions(self, tag=None, limit=1):
        cnt = sum(int(x.get("divisions") or 0) for x in self.order_group_rows) + len(self.unassigned)
        return {"count": cnt}

    def unassigned_divisions(self, tag=None):
        return list(self.unassigned)

    def order_groups(self, tag=None):
        return {"groups": [dict(x) for x in self.order_group_rows]}

    def add_divisions_to_army(self, og, divisions=None, tag=None, **kw):
        og_s = hex(og) if isinstance(og, int) else str(og)
        for grp in self.order_group_rows:
            if grp["ptr"] == og_s:
                ptrs = set(divisions or ())
                self.unassigned = [d for d in self.unassigned if d["ptr"] not in ptrs]
                grp["divisions"] += len(ptrs)
                return {"ok": True, "added": len(ptrs)}
        return {"ok": False, "error": "army not found"}

    def create_army(self, divisions, tag=None):
        ptrs = set(divisions or ())
        self.unassigned = [d for d in self.unassigned if d["ptr"] not in ptrs]
        new_ptr = "0xa%d" % (len(self.order_group_rows) + 1)
        self.order_group_rows.append({"ptr": new_ptr, "divisions": len(ptrs), "is_army_group": False, "leader_ptr": None})
        return {"ok": True, "ptr": new_ptr}

    def available_generals(self, tag=None):
        return {"count": len(self.generals_pool), "generals": [dict(x) for x in self.generals_pool]}

    def assign_commander(self, og, leader_ptr=None, character_key=None, **kw):
        og_s = hex(og) if isinstance(og, int) else str(og)
        lp_s = hex(leader_ptr) if isinstance(leader_ptr, int) else str(leader_ptr)
        for grp in self.order_group_rows:
            if grp["ptr"] == og_s:
                grp["leader_ptr"] = lp_s
                grp["leader_key"] = character_key
                return {"ok": True, "commander": character_key}
        return {"ok": False, "error": "army not found"}

    def overview(self, tag=None):
        fa = self.factory_allocation(tag)
        return {
            "date": self.date()["text"],
            "country": self._country.summary(),
            "factories": {
                "civ_total": fa["civilian_factories"],
                "construction_queue_len": len(self.const_queue),
                "mil_total": fa["military_available"],
                "mil_free": fa["military_idle"],
                "dock_total": fa["naval_available"],
            },
            "states": len(self.state_rows),
            "army_summary": {"total_divisions": self.divisions()["count"], "unassigned_divisions": len(self.unassigned)},
            "focus": {"current": self.cur_focus, "name": self.cur_focus},
            "research": self.research_slots(tag),
            "construction": self.construction_queue(tag),
            "production": self.production_lines(tag),
            "candidates": {"available_decisions": self.decisions(tag)},
            "alerts": [],
        }


class TestStandingOrdersAutopilot(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self._orig_state_path = AP.STATE_PATH
        self._orig_te_path = TE.STATE_PATH
        AP.STATE_PATH = os.path.join(self._td.name, "autopilot_state.json")
        TE.STATE_PATH = os.path.join(self._td.name, "turn_state.json")
        AP._clear_autopilot_caches()
        self.g = _FakeGame()

        # 隔离外部 combat / peacewin / diplomacy / events_win / session 调用
        self._patches = [
            mock.patch("combat.enemy_tags", side_effect=lambda g, tag=None: list(g.enemies)),
            mock.patch("combat.combats", side_effect=lambda g, **kw: []),
            mock.patch("peacewin.live_conference_readonly", side_effect=lambda g: bool(g.peace_open)),
            mock.patch.object(DIP, "country_wargoals_fast", side_effect=lambda g, tag=None, valid_only=True: (list(g.wargoals), True)),
            mock.patch.object(DIP, "justifying_fast", side_effect=lambda g, tag=None: list(g.justifying_list)),
            mock.patch.object(SESS, "is_paused", side_effect=lambda g: bool(g.paused)),
            mock.patch.object(SESS, "pause", side_effect=lambda g, paused=True: (setattr(g, "paused", bool(paused)) or (True, bool(paused)))),
            mock.patch.object(EW, "pending", side_effect=lambda g: list(g.events_pending)),
            mock.patch.object(EW, "open_events", side_effect=lambda g, **kw: {"ok": True, "open": len(g.events_pending), "events": list(g.events_pending)}),
            mock.patch.object(EW, "select_option", side_effect=self._mock_select_event_option),
        ]
        for pt in self._patches:
            pt.start()

    def tearDown(self):
        for pt in reversed(self._patches):
            pt.stop()
        AP._clear_autopilot_caches()
        AP.STATE_PATH = self._orig_state_path
        TE.STATE_PATH = self._orig_te_path
        self._td.cleanup()

    def _mock_select_event_option(self, g, index=None, event_id=None, write=False, **kw):
        ev = next((x for x in g.events_pending if x.get("event_id") == event_id), None)
        if not ev:
            return {"ok": False, "error": "not found"}
        opts = ev.get("options") or []
        unresolved = bool(ev.get("unresolved_scope"))
        if write:
            if unresolved:
                return {"ok": False, "blocked_by": "unresolved_scope"}
            g.events_answered.append((event_id, index))
            g.events_pending = [x for x in g.events_pending if x.get("event_id") != event_id]
            return {"ok": True, "event_id": event_id, "chosen": index}
        return {
            "ok": True,
            "event_id": event_id,
            "options": opts,
            "recommended": ev.get("recommended"),
            "recommendation_basis": ev.get("recommendation_basis"),
            "auto_resolve_option": ev.get("auto_resolve_option"),
            "unresolved_scope": unresolved,
            "recommendation_blocked": bool(ev.get("recommendation_blocked")),
        }

    # 1. 显式谓词树语法与求值
    def test_predicate_tree_rejects_implicit_and_or_and_evaluates_correctly(self):
        errs, _ = AP.validate_predicate({"date_ge": [1937, 10], "or_civ_ge": 70})
        self.assertTrue(any("禁止" in e or "隐式" in e for e in errs), errs)

        errs2, _ = AP.validate_predicate({"date_ge": [1937, 10], "civ_ge": 70})
        self.assertTrue(any("并列键" in e for e in errs2), errs2)

        errs3, _ = AP.validate_predicate({"war_support_ge": 50})
        self.assertTrue(any("0.0..1.0" in e for e in errs3), errs3)

        # 合法嵌套谓词树 + world_tension 不可观测显式警告
        pred = {
            "any": [
                {"all": [{"date_ge": [1937, 10]}, {"civ_ge": 70}]},
                {"world_tension_ge": 0.25},
                {"at_war": True},
            ]
        }
        errs4, warns4 = AP.validate_predicate(pred, observable_tension=False)
        self.assertEqual(errs4, [])
        self.assertTrue(any("world_tension_ge" in w for w in warns4), warns4)

        ctx = {"date_ymd": (1937, 11, 1), "civ_total": 60, "at_war": False, "world_tension": None}
        self.assertFalse(AP.eval_predicate(pred, ctx))
        ctx["civ_total"] = 70
        self.assertTrue(AP.eval_predicate(pred, ctx))
        ctx["civ_total"] = 50
        ctx["at_war"] = True
        self.assertTrue(AP.eval_predicate(pred, ctx))

    # 2. Strategy Registry 与拒绝裸布尔/旧隐式字段
    def test_strategy_registry_and_bare_bool_rejection(self):
        cat = AP.strategy_catalog()
        self.assertTrue(cat["ok"])
        self.assertIn("construction_state", cat["strategies"])
        self.assertIn("infra_then_slots", cat["strategies"]["construction_state"])
        self.assertIn("formula", cat["strategies"]["construction_state"]["infra_then_slots"])

        bad = AP.compile_profile(self.g, {
            "construction": {"state_order": "max_infra"},
            "production": {"auto_upgrade_max_payback_days": 45},
            "divisions": {"auto_assign_general": True, "split_by_role": True},
        })
        self.assertFalse(bad["ok"])
        joined = "\n".join(bad["errors"])
        self.assertIn("state_order", joined)
        self.assertIn("auto_upgrade_max_payback_days", joined)
        self.assertIn("auto_assign_general", joined)
        self.assertIn("split_by_role", joined)

    # 3. 不可关闭的 Kernel Floor 强校验
    def test_kernel_floor_cannot_be_disabled(self):
        for bad_prof in (
            {"disable_kernel_floor": True},
            {"kernel_floor": {"wargoal_or_cb": False}},
            {"kernel_floor": {"max_autopilot_days": 0}},
            {"kernel_floor": {"heartbeat_days": False}},
            {"interrupts_extra": {"wargoal_or_cb": False}},
        ):
            r = AP.set_standing_orders(self.g, bad_prof)
            self.assertFalse(r["ok"], f"应当拒绝关闭地板: {bad_prof}")
            self.assertEqual(r["blocked_by"], "compile_failed")

    # 4. 三段式 compile(dry_run)：错别字 / XOR / 不可逆国策 / PP 曲线 / 选州策略差异
    def test_compile_dry_run_catches_typos_xor_irreversible_and_projects_pp(self):
        bad = AP.compile_profile(self.g, {
            "focus": {"queue": ["GER_typo_nonexistent", "GER_oppose_hitler"]},
            "research": {"queue": ["concentrated_industry", "dispersed_industry", "fake_tech_xyz"]},
            "pp_policy": {"agenda": [{"kind": "law", "key": "nonexistent_law_999"}]},
        })
        self.assertFalse(bad["ok"])
        msg = "\n".join(bad["errors"])
        self.assertIn("GER_typo_nonexistent", msg)
        self.assertIn("GER_oppose_hitler", msg)
        self.assertIn("XOR", msg)
        self.assertIn("fake_tech_xyz", msg)
        self.assertIn("nonexistent_law_999", msg)

        # 合法 dry_run：比较 infra_then_slots 与 max_free_slots 选州差异 + PP 预测
        good_infra = AP.set_standing_orders(self.g, {
            "name": "GER_opening",
            "construction": {
                "state_strategy": {"strategy": "infra_then_slots", "params": {"min_slots": 1}},
                "rules": [{"building": "industrial_complex", "when": {"always": True}}],
            },
            "pp_policy": {
                "reserve_pp": 55,
                "agenda": [
                    {"kind": "advisor", "key": "GER_martin_bormann"},
                    {"kind": "law", "key": "war_economy"},
                ],
            },
        }, dry_run=True)
        self.assertTrue(good_infra["ok"], good_infra)
        self.assertFalse(good_infra["activated"])
        self.assertFalse(AP.is_active(self.g))
        # Brandenburg(64) infra=5 slots=3 应排在 Hannover(59) infra=3 slots=7 之前，满槽 99 应被跳过
        top_ids = [s["state"] for s in good_infra["preview"]["construction_preview"]["top_states"]]
        self.assertEqual(top_ids[0], 64)
        self.assertNotIn(99, top_ids)
        self.assertEqual(good_infra["preview"]["pp_projection"]["total_agenda_cost"], 300)

        good_slots = AP.compile_profile(self.g, {
            "construction": {
                "state_strategy": {"strategy": "max_free_slots", "params": {"min_slots": 1}},
                "rules": [{"building": "industrial_complex", "when": {"always": True}}],
            },
        })
        self.assertTrue(good_slots["ok"])
        top_slots = [s["state"] for s in good_slots["preview"]["construction_preview"]["top_states"]]
        self.assertEqual(top_slots[0], 59)  # Hannover slots_free=7 第一

    # 5. 阶段机 + sticky=True 永不回退
    def test_phase_machine_and_sticky_no_rollback(self):
        self.g.ymd = (1938, 6, 1)
        prof = {
            "name": "multi_phase",
            "phases": [
                {"name": "peace", "enter_when": {"always": True}, "sticky": False, "profile": {}},
                {"name": "prewar", "enter_when": {"date_ge": [1938, 9]}, "sticky": False, "profile": {}},
                {"name": "war", "enter_when": {"at_war": True}, "sticky": True, "profile": {}},
            ],
        }
        r = AP.set_standing_orders(self.g, prof)
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["active_phase"], "peace")

        self.g.ymd = (1938, 10, 1)
        t1 = AP.run_tick(self.g)
        self.assertEqual(t1["phase"], "prewar")
        self.assertTrue(t1["phase_changed"])

        self.g.enemies = ["POL"]
        t2 = AP.run_tick(self.g)
        self.assertEqual(t2["phase"], "war")

        # 和平后 at_war=False，但 war 阶段标记 sticky=True -> 绝不回滚！
        self.g.enemies = []
        t3 = AP.run_tick(self.g)
        self.assertEqual(t3["phase"], "war")

    # 6. 全子系统确定性执行（国策 / 科技超前拦截 / PP 预算锁 / 建造 / 产线换代与配比 / 编组与选将）
    def test_full_subsystems_execution_and_guards(self):
        # 场景 1：focus on_blocked="wake" 对被阻塞的 GER_anschluss 绝不静默跳过
        r0 = AP.set_standing_orders(self.g, {
            "focus": {
                "queue": ["GER_anschluss", "GER_the_four_year_plan"],
                "on_blocked": "wake",
            },
        })
        self.assertTrue(r0["ok"], r0)
        tk0 = AP.run_tick(self.g)
        self.assertIsNone(self.g.cur_focus)
        self.assertTrue(any(w["floor"] == "interrupt_extra:focus_blocked" for w in tk0["wake_triggers"]))

        # 场景 2：全子系统协同运行
        self.g.pp = 180.0  # 180 - 150 = 30 < reserve_pp(55) -> 绝不能买！
        r1 = AP.set_standing_orders(self.g, {
            "name": "GER_1936_full",
            "focus": {
                "must": ["GER_remilitarize_the_rhineland"],
                "queue": ["GER_the_four_year_plan"],
                "on_blocked": "wake",
            },
            "research": {
                "queue": ["advanced_machine_tools", "basic_machine_tools", "construction1"],
                "never": ["radio"],
                "max_ahead_years": 0.5,
                "fallback_strategy": {"strategy": "industry_first"},
            },
            "pp_policy": {
                "reserve_pp": 55,
                "strategy": "strict_agenda",
                "agenda": [
                    {"kind": "advisor", "key": "GER_martin_bormann"},
                    {"kind": "law", "key": "war_economy"},
                ],
                "auto_fallback_ideas": False,
            },
            "construction": {
                "target_queue_len": 3,
                "state_strategy": {"strategy": "infra_then_slots", "params": {"min_slots": 1}},
                "queue": [{"building": "infrastructure", "count": 1, "state": 59}],
                "rules": [
                    {"building": "industrial_complex", "until": {"any": [{"date_ge": [1937, 10]}, {"civ_ge": 70}]}},
                    {"building": "arms_factory", "when": {"always": True}},
                ],
            },
            "production": {
                "strategy": {
                    "strategy": "ratio_hysteresis",
                    "params": {
                        "ratios": {"inf": 0.3, "art": 0.2, "armor": 0.2, "air": 0.3},
                        "min_factories": {"infantry_equipment_1": 6},
                        "allocate_dockyards": True,
                    },
                },
                "upgrade_strategy": {
                    "strategy": "payback",
                    "params": {"max_payback_days": 45.0, "min_stockpile_days": 30.0},
                },
            },
            "divisions": {
                "auto_deploy": True,
                "assign_strategy": {
                    "strategy": "merge_existing_then_new",
                    "params": {
                        "max_per_army": 24,
                        "role_rules": [{"template_contains": ["panzer", "tank"], "role": "armor"}],
                    },
                },
                "general_strategy": {
                    "strategy": "trait_then_skill",
                    "params": {
                        "role_prefer_traits": {"armor": ["panzer_leader"]},
                        "min_skill": 3,
                    },
                },
            },
        })
        self.assertTrue(r1["ok"], r1)

        tk1 = AP.run_tick(self.g)
        self.assertTrue(tk1["ok"])
        # 国策选了 must
        self.assertEqual(self.g.cur_focus, "GER_remilitarize_the_rhineland")
        # 科研跳过了 1939 年超前科技 advanced_machine_tools，填了 1936 年的 basic_machine_tools & construction1
        res_running = {s["researching"] for s in self.g.slots}
        self.assertEqual(res_running, {"basic_machine_tools", "construction1"})
        # PP=180 < 150+55=205 -> 预算锁拦截，1 分没花！
        self.assertEqual(self.g.pp, 180.0)
        self.assertEqual(len(self.g.hired_advisors), 0)
        # 建造：第 1 条显式 infrastructure@59 + 2 条 industrial_complex@64（infra 最高）
        self.assertEqual(len(self.g.const_queue), 3)
        self.assertEqual(self.g.const_queue[0]["building"], "infrastructure")
        self.assertEqual(self.g.const_queue[0]["state"], 59)
        self.assertEqual(self.g.const_queue[1]["building"], "industrial_complex")
        self.assertEqual(self.g.const_queue[1]["state"], 64)
        # 产线换代：步枪库存 100 天 >= 30 天已升级，轻坦库存仅 5 天 < 30 天未升级！
        self.assertEqual(self.g.prod_lines[0]["equipment"], "infantry_equipment_1")
        self.assertEqual(self.g.prod_lines[2]["equipment"], "light_tank_chassis_1")
        # 空闲军工已全部分配，空闲船坞也已分配
        self.assertEqual(self.g.factory_allocation()["military_idle"], 0)
        self.assertEqual(self.g.dock_alloc_called, 1)
        # 部队：已自动下地；伞兵 0xd3 豁免留守；步兵 0xd2 并入 0xa1；装甲 0xd1 独立成军并指派古德里安(panzer_leader)！
        self.assertEqual(self.g.deploy_called, 1)
        self.assertEqual([d["ptr"] for d in self.g.unassigned], ["0xd3"])
        armor_army = next((x for x in self.g.order_group_rows if x["ptr"] != "0xa1"), None)
        self.assertIsNotNone(armor_army)
        self.assertEqual(armor_army.get("leader_key"), "GER_heinz_guderian")

        # 涨 PP 到 215 >= 150 + 55 -> 顺利聘用鲍曼且剩 65 >= 55，绝不接着花破预算买第 2 项
        self.g.pp = 215.0
        self.g.advance_days(15)
        AP.run_tick(self.g)
        self.assertIn("GER_martin_bormann", self.g.hired_advisors)
        self.assertNotIn("war_economy", self.g.active_ideas)
        self.assertGreaterEqual(self.g.pp, 55.0)

        # 民工达 70 -> 建造规则自动从民工切为军工！
        self.g.civ = 70
        self.g.const_queue = []
        AP.run_tick(self.g)
        self.assertTrue(all(x["building"] == "arms_factory" for x in self.g.const_queue))

    # 7. 事件分级：单选项/等价新闻自动放行，真实分歧立即熔断，unresolved_scope 绝不写
    def test_event_classification_auto_trivial_vs_decisive_and_unresolved_scope(self):
        AP.set_standing_orders(self.g, {"events": {"strategy": "auto_trivial"}})
        # 1) 单适用选项 -> 自动放行
        self.g.events_pending = [{
            "event_id": "news.1", "kind": "news_event",
            "options": [
                {"index": 1, "applicable": False, "effects": []},
                {"index": 2, "applicable": True, "effects": []},
            ],
        }]
        tk1 = AP.run_tick(self.g)
        self.assertFalse(tk1["should_wake"])
        self.assertEqual(self.g.events_answered, [("news.1", 2)])

        # 2) 等价纯新闻多选项 -> 自动放行
        self.g.events_pending = [{
            "event_id": "news.2", "kind": "news_event",
            "options": [
                {"index": 1, "applicable": True, "effects": []},
                {"index": 2, "applicable": True, "effects": []},
            ],
        }]
        tk2 = AP.run_tick(self.g)
        self.assertFalse(tk2["should_wake"])
        self.assertEqual(self.g.events_answered[-1], ("news.2", 1))

        # 3) 真实分歧多选项国家事件 -> Kernel Floor decisive_event 立即唤醒
        self.g.events_pending = [{
            "event_id": "germany.55", "kind": "country_event",
            "options": [
                {"index": 1, "applicable": True, "effects": ["annex_country = CZE"]},
                {"index": 2, "applicable": True, "effects": ["add_political_power = -50"]},
            ],
        }]
        tk3 = AP.run_tick(self.g)
        self.assertTrue(tk3["should_wake"])
        self.assertTrue(any(t["floor"] == "decisive_event" for t in tk3["wake_triggers"]))
        self.assertEqual(len(self.g.events_pending), 1)

        # 4) unresolved_scope -> 绝不发写命令！
        self.g.events_pending = [{
            "event_id": "country_capitulated.0", "kind": "news_event",
            "unresolved_scope": True,
            "options": [{"index": 1, "applicable": None, "effects": []}],
        }]
        before_ans = len(self.g.events_answered)
        AP.run_tick(self.g)
        self.assertEqual(len(self.g.events_answered), before_ans)

    # 8. 内核熔断地板运行时验证：PP 击穿 / 外部法案变动 / 心跳 / 硬上限 / no_progress
    def test_kernel_floor_runtime_triggers(self):
        r = AP.set_standing_orders(self.g, {
            "kernel_floor": {"max_autopilot_days": 60, "heartbeat_days": 30, "no_progress_limit": 3},
            "pp_policy": {"reserve_pp": 55},
        })
        self.assertTrue(r["ok"])

        # (a) PP 预算被外部事件击穿
        self.g.pp = 30.0
        tk_pp = AP.run_tick(self.g)
        self.assertTrue(any(t["floor"] == "pp_reserve_breach" for t in tk_pp["wake_triggers"]))
        self.g.pp = 120.0
        AP.run_tick(self.g)  # 恢复基准

        # (b) 外部事件强行改经济法案 -> external_law_or_regime_change
        self.g.active_ideas = ["civilian_economy", "limited_conscription", "export_focus"]
        tk_law = AP.run_tick(self.g)
        self.assertTrue(any(t["floor"] == "external_law_or_regime_change" for t in tk_law["wake_triggers"]))
        # 还原并切到心跳测试
        self.g.active_ideas = ["partial_economic_mobilisation", "limited_conscription", "export_focus"]

        # (c) 强制心跳 heartbeat_days = 30
        self.g.advance_days(30)
        tk_hb = AP.run_tick(self.g)
        self.assertTrue(any(t["floor"] == "heartbeat_days" for t in tk_hb["wake_triggers"]))
        # 模型确认接管后重置心跳，但总上限 max_autopilot_days=60 不重置！
        AP.acknowledge_wakeup(self.g)
        self.g.advance_days(30)
        tk_max = AP.run_tick(self.g)
        self.assertTrue(any(t["floor"] == "max_autopilot_days" for t in tk_max["wake_triggers"]))

        # (d) no_progress：连续 3 次尝试选国策均失败且状态零变化 -> 熔断
        AP.set_standing_orders(self.g, {
            "kernel_floor": {"no_progress_limit": 3},
            "focus": {"queue": ["GER_remilitarize_the_rhineland"]},
        })
        self.g.fail_focus_set = True
        self.assertFalse(any(t["floor"] == "no_progress" for t in AP.run_tick(self.g)["wake_triggers"]))
        self.assertFalse(any(t["floor"] == "no_progress" for t in AP.run_tick(self.g)["wake_triggers"]))
        tk_np = AP.run_tick(self.g)
        self.assertTrue(any(t["floor"] == "no_progress" for t in tk_np["wake_triggers"]))

    # 9. TurnLoop + TurnEngine 端到端集成（自动消化常规事项 + CB 第一帧停表）
    def test_turnloop_and_turnengine_end_to_end_autopilot_and_cb_interrupt(self):
        # 通过 Plan DSL set_standing_orders 激活
        plan = {
            "actions": [{
                "op": "set_standing_orders",
                "args": {
                    "profile": {
                        "name": "GER_e2e",
                        "focus": {"queue": ["GER_remilitarize_the_rhineland", "GER_the_four_year_plan"]},
                        "research": {"queue": ["basic_machine_tools", "construction1"]},
                    }
                },
            }]
        }
        res_plan = TE.execute_plan(self.g, plan)
        self.assertTrue(res_plan["ok"], res_plan)
        self.assertTrue(AP.is_active(self.g))

        # 通过 hoi4_inspect 查询 3 个新增 topic 与增强的 research_available
        insp = TE.inspect_topics(self.g, [
            {"topic": "autopilot"},
            {"topic": "strategy_catalog", "filter": "construction_state"},
            {"topic": "idea_catalog", "slot": "economy"},
        ])
        self.assertTrue(insp["ok"])
        self.assertTrue(insp["results"][0]["data"]["active"])
        self.assertIn("infra_then_slots", insp["results"][1]["data"]["strategies"])
        self.assertTrue(any(x["key"] == "war_economy" for x in insp["results"][2]["data"]["items"]))

        insp_r = TE.inspect_topics(self.g, [{"topic": "research_available"}])
        self.assertTrue(insp_r["ok"])
        self.assertIn("details", insp_r["results"][0]["data"])
        self.assertIn("start_year", insp_r["results"][0]["data"]["details"][0])

        # 简报中渲染 Standing Orders 摘要
        br = TE.build_turn_brief(self.g)
        self.assertIn("自动驾驶 (Standing Orders)", br["markdown"])

        # TurnLoop.wait(autopilot=True, fast_forward=True)：
        # - 开跑前 autopilot 自动点掉第 1 个国策与 2 个科研槽并安全提速解暂停；
        # - 第 1 步模拟第 1 个国策完成 -> autopilot 静默切第 2 个国策，绝不打断等待！
        # - 第 2 步模拟对 POL 的 CB 就绪 -> Kernel Floor 立刻强制停表唤醒！
        step_n = [0]

        def _fake_sleep(_s):
            step_n[0] += 1
            self.g.advance_days(2)
            if step_n[0] == 1:
                self.g.done_focuses.add("GER_remilitarize_the_rhineland")
                self.g.cur_focus = None
            elif step_n[0] == 2:
                self.g.wargoals = [{"target_tag": "POL", "wargoal": "take_state"}]

        with mock.patch("time.sleep", side_effect=_fake_sleep), \
             mock.patch.object(RD, "check_unpause_gate", return_value=None), \
             mock.patch.object(RD, "evaluate", return_value={"has_blocking": False, "blocking": []}):
            tl = TL.TurnLoop(self.g, poll=0.01)
            w = tl.wait(max_days=60, timeout_seconds=10, pause_on_wake=False,
                        respect_red_dots=True, autopilot=True, fast_forward=True)

        self.assertEqual(self.g.speed, 3)  # fast_forward 安全提速至 speed=3 (UI 4 速)
        self.assertEqual(self.g.cur_focus, "GER_the_four_year_plan")  # 自动切了第 2 个国策且未提前醒！
        self.assertEqual(step_n[0], 2)  # 第 1 步未被打断，第 2 步 CB 一出立刻醒！
        self.assertTrue(w.get("wargoal_woke"))
        self.assertTrue(w.get("paused"))
        self.assertIn("autopilot", w)
        self.assertGreaterEqual(w["autopilot"]["actions_taken"], 4)

    # 10. 读旧档/日期倒退自动作废旧 Standing Orders
    def test_session_continuity_auto_deactivates_on_save_backjump(self):
        self.g.ymd = (1937, 6, 1)
        AP.set_standing_orders(self.g, {"name": "old_save_orders"})
        self.assertTrue(AP.is_active(self.g))

        # 模拟玩家读回 1936.01.01 旧档
        self.g.ymd = (1936, 1, 1)
        self.assertFalse(AP.is_active(self.g))
        self.assertFalse(AP.status(self.g)["active"])

    # 11. P1-1: construction.queue[] 严格校验白名单与 when/until 显式谓词树
    def test_p1_1_construction_queue_validates_predicates_and_rejects_unknown_keys(self):
        # 复现 T3：queue 项携带禁止的隐式 or_* 键必须报错拒绝
        bad_or = AP.compile_profile(self.g, {
            "construction": {
                "queue": [{
                    "building": "industrial_complex",
                    "count": 2,
                    "until": {"date_ge": [1937, 10], "or_at_war": True, "or_civ_ge": 70},
                }],
            },
        })
        self.assertFalse(bad_or["ok"])
        self.assertTrue(any("隐式" in e or "禁止" in e for e in bad_or["errors"]), bad_or["errors"])

        # queue 项未知拼写键必须报错拒绝
        bad_key = AP.compile_profile(self.g, {
            "construction": {"queue": [{"building": "industrial_complex", "counts": 2}]},
        })
        self.assertFalse(bad_key["ok"])
        self.assertTrue(any("counts" in e for e in bad_key["errors"]), bad_key["errors"])

        # 合法显式 until 谓词真实生效：civ >= 30 时跳过民工直接排军工
        self.g.civ = 35
        self.g.const_queue = []
        ok_q = AP.set_standing_orders(self.g, {
            "construction": {
                "target_queue_len": 2,
                "queue": [
                    {"building": "industrial_complex", "count": 4, "until": {"civ_ge": 30}},
                    {"building": "arms_factory", "count": 2},
                ],
            },
        })
        self.assertTrue(ok_q["ok"], ok_q)
        AP.run_tick(self.g)
        self.assertEqual(len(self.g.const_queue), 2)
        self.assertTrue(all(x["building"] == "arms_factory" for x in self.g.const_queue))

    # 12. P1-2: 既有建造队列交互（不盲撑爆 target_queue_len + count_existing_in_queue 抵扣）
    def test_p1_2_construction_count_existing_in_queue_and_queue_full_warning(self):
        # 模拟实测存档：玩家已排 6 座民工
        self.g.const_queue = [
            {"index": i, "state": 59, "building": "industrial_complex", "factories": 15 if i == 0 else 0}
            for i in range(6)
        ]
        # 默认 count_existing_in_queue=False：编译期明确给出两条提示且不把队列撑爆到 6+6
        c_def = AP.compile_profile(self.g, {
            "construction": {
                "target_queue_len": 6,
                "queue": [
                    {"building": "industrial_complex", "count": 4},
                    {"building": "arms_factory", "count": 2},
                ],
            },
        })
        self.assertTrue(c_def["ok"])
        wtxt = "\n".join(c_def["warnings"])
        self.assertIn("target_queue_len=6", wtxt)
        self.assertIn("count_existing_in_queue=true", wtxt)
        self.assertEqual(c_def["preview"]["construction_preview"]["will_add_now"], [])

        # 显式 count_existing_in_queue=True：6 座已有民工自动抵扣 4 座民工配额；腾出 1 个空位后立刻排军工！
        ok_ex = AP.set_standing_orders(self.g, {
            "construction": {
                "target_queue_len": 6,
                "count_existing_in_queue": True,
                "queue": [
                    {"building": "industrial_complex", "count": 4},
                    {"building": "arms_factory", "count": 2},
                ],
            },
        })
        self.assertTrue(ok_ex["ok"], ok_ex)
        self.assertEqual(ok_ex["preview"]["construction_preview"]["next_building"], "arms_factory")
        # 模拟 1 座民工完工，队列降为 5
        self.g.const_queue.pop(0)
        AP.run_tick(self.g)
        self.assertEqual(len(self.g.const_queue), 6)
        self.assertEqual(self.g.const_queue[-1]["building"], "arms_factory")

    # 13. P2-1: 单一真源 preview 与真实 dry-run tick 严格一致 + 在研科技 XOR 编译期拦截
    def test_p2_1_preview_matches_dry_run_tick_single_source_of_truth(self):
        # 槽 0 正在研究 concentrated_industry，队列塞入互斥的 dispersed_industry -> 编译期直接拦！
        self.g.slots = [
            {"slot": 0, "researching": "concentrated_industry", "slot_busy": True, "slot_done": False},
            {"slot": 1, "researching": None, "slot_busy": False, "slot_done": False},
        ]
        bad_xor = AP.compile_profile(self.g, {
            "research": {"queue": ["dispersed_industry"]},
        })
        self.assertFalse(bad_xor["ok"])
        self.assertTrue(any("XOR" in e for e in bad_xor["errors"]), bad_xor["errors"])

        # focus 仅配 tier_score 回落（must/queue 为空）+ research 队列仅有 1939 超前科技且 fallback=none：
        # preview 必须真实预告 tier_score 选出的国策，并诚实报告 research 无法填槽！
        comp = AP.compile_profile(self.g, {
            "focus": {"fallback": "tier_score"},
            "research": {"queue": ["advanced_machine_tools"], "max_ahead_years": 0.5, "fallback_strategy": "none"},
        })
        self.assertTrue(comp["ok"], comp)
        self.assertEqual(comp["preview"]["focus_preview"]["next_planned"], ["GER_remilitarize_the_rhineland"])
        self.assertEqual(comp["preview"]["research_preview"]["next_planned"], [])
        self.assertTrue(comp["preview"]["research_preview"]["blocked_or_wake"])
        # 激活后再跑 dry_run tick，动作清单与编译期 preview.next_tick_actions 完全一致！
        AP.set_standing_orders(self.g, {
            "focus": {"fallback": "tier_score"},
            "research": {"queue": ["advanced_machine_tools"], "max_ahead_years": 0.5, "fallback_strategy": "none"},
        })
        tk_dry = AP.run_tick(self.g, dry_run=True)
        self.assertEqual(tk_dry["actions"], comp["preview"]["next_tick_actions"])

    # 14. P2-2: reserve_pp > pp_now 编译告警 + 正常攒 PP 不误醒 + 负/零 PP 日增才熔断 + >2000 硬拒
    def test_p2_2_pp_reserve_warning_and_stalled_nonpositive_gain_wake(self):
        # cost(150) + reserve_pp(1900) > 2000 -> 数学上永不可买，编译直接拒绝
        bad_cap = AP.compile_profile(self.g, {
            "pp_policy": {
                "reserve_pp": 1900,
                "agenda": [{"kind": "law", "key": "war_economy"}],
            },
        })
        self.assertFalse(bad_cap["ok"])
        self.assertTrue(any("2000" in e for e in bad_cap["errors"]), bad_cap["errors"])

        # reserve_pp=400 > pp=338：编译期给清晰 warning（含预计等待天数），运行期正常攒钱绝不瞎叫醒
        self.g.pp = 338.0
        self.g.pp_daily = 1.5
        prof = {
            "pp_policy": {
                "reserve_pp": 400,
                "agenda": [{"kind": "advisor", "key": "GER_martin_bormann"}],
            },
        }
        c = AP.compile_profile(self.g, prof)
        self.assertTrue(c["ok"])
        self.assertTrue(any("reserve_pp=400" in w and "天" in w for w in c["warnings"]), c["warnings"])

        AP.set_standing_orders(self.g, prof)
        for _ in range(5):
            self.g.advance_days(1)
            self.g.pp += 1.5
            tk = AP.run_tick(self.g)
            self.assertFalse(tk["should_wake"])

        # 若政治点日增跌到 <= 0（停滞/负增长）导致永不可能攒够 -> 立刻触发 interrupt_extra:pp_stalled 唤醒
        self.g.pp_daily = -0.2
        tk_stalled = AP.run_tick(self.g)
        self.assertTrue(any(w["floor"] == "interrupt_extra:pp_stalled" for w in tk_stalled["wake_triggers"]))

    # 15. P3-1: interrupts_extra 与各子系统未知键一律硬拒
    def test_p3_1_unknown_keys_rejected_across_interrupts_extra_and_subsystems(self):
        bad = AP.compile_profile(self.g, {
            "kernel_floor": {"max_days_typo": 90},
            "interrupts_extra": {"on_wargoal_or_cb": True},
            "focus": {"bad_focus_field": 1},
            "pp_policy": {"agenda": [{"kind": "law", "key": "war_economy", "bad_item_key": 1}]},
        })
        self.assertFalse(bad["ok"])
        msg = "\n".join(bad["errors"])
        self.assertIn("max_days_typo", msg)
        self.assertIn("on_wargoal_or_cb", msg)
        self.assertIn("bad_focus_field", msg)
        self.assertIn("bad_item_key", msg)

    # 16. E1: 特别工程科技静态过滤 + slot_done 瞬态退避（不冤枉拉黑合法科技/不误累 no_progress）+ 真实空槽连拒 2 次拉黑顺延
    def test_e1_research_special_project_filter_slot_done_defer_and_temp_blacklist(self):
        import techdata as _td
        td = _td.load() or {}
        if "cavity_magnatron" in td:
            self.assertTrue(td["cavity_magnatron"].get("special_project_gated"))

        # (a) 槽 0 刚完成 concentrated_industry（slot_done=True）但引擎尚未清槽（拒绝写入）：
        #     绝不能重复点 concentrated_industry，且尝试 basic_machine_tools 失败后应只退避该槽，
        #     不拉黑 basic_machine_tools、不累加 no_progress_streak；待清槽后立即点上！
        self.g.slots = [
            {"slot": 0, "researching": "concentrated_industry", "slot_busy": True, "slot_done": True, "tech_ptr": "0x999"},
            {"slot": 1, "researching": "construction1", "slot_busy": True, "slot_done": False, "tech_ptr": "0x888"},
        ]
        self.g.fail_research_slots = {0}
        AP.set_standing_orders(self.g, {
            "research": {
                "queue": ["concentrated_industry", "basic_machine_tools"],
                "fallback_strategy": "none",
            },
        })
        t1 = AP.run_tick(self.g)
        self.assertEqual(t1["actions"][0]["action"], "start_research:basic_machine_tools@slot0")
        self.assertFalse(t1["actions"][0]["ok"])
        self.assertTrue(t1["actions"][0]["detail"]["slot_done_transient"])
        self.assertEqual(AP.status(self.g)["no_progress_streak"], 0)
        # 第二次 tick 跳过该未清槽，不反复死磕
        self.g.advance_days(1)
        t2 = AP.run_tick(self.g)
        self.assertEqual(t2["actions"], [])
        # 游戏次日清槽并将 concentrated_industry 记入已研后，未被冤枉拉黑的 basic_machine_tools 立刻点上！
        self.g.fail_research_slots = set()
        self.g.researched_set.add("concentrated_industry")
        self.g.slots[0] = {"slot": 0, "researching": None, "slot_busy": False, "slot_done": False}
        self.g.advance_days(1)
        t3 = AP.run_tick(self.g)
        self.assertEqual(self.g.slots[0]["researching"], "basic_machine_tools")
        self.assertTrue(t3["actions"][0]["ok"])

        # (b) 真实空槽遇到 CanExecute 必拒科技 -> 同槽顺延尝试下一个合法科技，累计 2 次失败进临时黑名单
        self.g.slots = [
            {"slot": 0, "researching": None, "slot_busy": False, "slot_done": False},
        ]
        self.g.fail_research_techs = {"basic_machine_tools"}
        AP.set_standing_orders(self.g, {
            "research": {
                "queue": ["basic_machine_tools", "construction1", "radio"],
                "fallback_strategy": "none",
            },
        })
        t4 = AP.run_tick(self.g)
        # 同一个 tick 内 basic_machine_tools 失败后立刻顺延成功点上 construction1！
        self.assertEqual(self.g.slots[0]["researching"], "construction1")
        self.assertEqual(len(t4["actions"]), 2)
        self.assertFalse(t4["actions"][0]["ok"])
        self.assertTrue(t4["actions"][1]["ok"])
        # 第二次再空槽遇到 basic_machine_tools 失败达 2 次 -> 进 temp_blacklist，第 3 次不再尝试
        self.g.slots[0] = {"slot": 0, "researching": None, "slot_busy": False, "slot_done": False}
        self.g.researched_set.add("construction1")
        t5 = AP.run_tick(self.g)
        self.assertTrue(t5["ok"])
        self.assertEqual(self.g.slots[0]["researching"], "radio")
        st_now = AP.get_active_state(self.g)
        self.assertIn("basic_machine_tools", st_now["research_temp_blacklist"])

    # 17. E2: 心跳与上限可通过 Plan DSL ack_autopilot / autopilot_tick(ack=true) 免前台只读签收解锁
    def test_e2_ack_autopilot_unlocks_heartbeat_and_extend_max_days(self):
        AP.set_standing_orders(self.g, {
            "kernel_floor": {"max_autopilot_days": 60, "heartbeat_days": 30},
        })
        self.g.advance_days(31)
        tk_hb = AP.run_tick(self.g)
        self.assertTrue(any(w["floor"] == "heartbeat_days" for w in tk_hb["wake_triggers"]))

        # 通过 Plan DSL ack_autopilot 签收（0 注入，免前台）
        ack_plan = TE.execute_plan(self.g, {
            "actions": [{"op": "ack_autopilot", "args": {"reason": "巡检无异常，继续巡航"}}],
        })
        self.assertTrue(ack_plan["ok"], ack_plan)
        self.assertTrue(ack_plan["results"][0]["raw"]["acked"])

        # 签收后心跳不再卡死
        tk_after = AP.run_tick(self.g)
        self.assertFalse(any(w["floor"] == "heartbeat_days" for w in tk_after["wake_triggers"]))

        # 推进到 max_autopilot_days 后用 autopilot_tick(ack=true, extend_max_days=true) 续跑一轮
        self.g.advance_days(30)
        tk_max = AP.run_tick(self.g)
        self.assertTrue(any(w["floor"] == "max_autopilot_days" for w in tk_max["wake_triggers"]))
        ext_plan = TE.execute_plan(self.g, {
            "actions": [{"op": "autopilot_tick", "args": {"ack": True, "extend_max_days": True, "reason": "续跑至1938"}}],
        })
        self.assertTrue(ext_plan["ok"], ext_plan)
        self.assertTrue(ext_plan["results"][0]["raw"]["extended_max_days"])
        tk_ext = AP.run_tick(self.g)
        self.assertFalse(any(w["floor"] == "max_autopilot_days" for w in tk_ext["wake_triggers"]))

    # 18. E3: xp_policy 自动升级主力步兵编制 + 显式学说解锁 + 弱编/三军 XP 囤废兜底提醒
    def test_e3_xp_policy_template_upgrade_doctrine_and_fallback_wakes(self):
        # 场景 A：配置 army_template_upgrades + doctrines
        self.g.xp_map = {"army": 85.0, "navy": 120.0, "air": 110.0}
        comp = AP.compile_profile(self.g, {
            "xp_policy": {
                "army_template_upgrades": [
                    {"preset": "inf_9_3", "min_xp": 45, "auto_rebalance": True},
                ],
                "doctrines": [
                    {"doctrine": "battlefield_support", "min_xp": 100},
                ],
            },
        })
        self.assertTrue(comp["ok"], comp)
        self.assertIn("xp_preview", comp["preview"])
        self.assertTrue(any("upgrade_template:inf_9_3" in a for a in comp["preview"]["xp_preview"]["next_actions"]))

        AP.set_standing_orders(self.g, {
            "xp_policy": {
                "army_template_upgrades": [
                    {"preset": "inf_9_3", "min_xp": 45, "auto_rebalance": True},
                ],
                "doctrines": [
                    {"doctrine": "battlefield_support", "min_xp": 100},
                ],
            },
        })
        tk = AP.run_tick(self.g)
        self.assertTrue(tk["ok"])
        self.assertEqual(self.g.templates_map["Infanterie-Division"]["line"].get("artillery_brigade"), 3)
        self.assertIn("battlefield_support", self.g.unlocked_doctrines)

        # 场景 B：未配置升级策略但陆军 XP=60 仍是 9步0炮弱编 + 海军 XP=320 闲置 -> 硬兜底唤醒且不死锁重复
        self.g.templates_map["Infanterie-Division"]["line"] = {"infantry": 9}
        self.g.xp_map = {"army": 60.0, "navy": 320.0, "air": 20.0}
        AP.set_standing_orders(self.g, {"name": "no_xp_cfg"})
        tk_b1 = AP.run_tick(self.g)
        floors_b1 = {w["floor"] for w in tk_b1["wake_triggers"]}
        self.assertIn("interrupt_extra:weak_infantry_template", floors_b1)
        self.assertIn("interrupt_extra:xp_cap", floors_b1)
        # 同一阶梯不重复死锁唤醒
        tk_b2 = AP.run_tick(self.g)
        self.assertFalse(any(w["floor"] in ("interrupt_extra:weak_infantry_template", "interrupt_extra:xp_cap") for w in tk_b2["wake_triggers"]))

    # 19. E4: 被事件退回未完成的国策（如 GER_ascension_of_todt）在 tier_score 兜底中不再重复死循环
    def test_e4_repeated_uncompleted_focus_guarded_in_tier_score(self):
        self.g.all_focus_keys.append("GER_ascension_of_todt")
        self.g.selectable_focus_rows = [
            {"key": "GER_ascension_of_todt", "id": "GER_ascension_of_todt", "tier": "S_historical", "score": 986, "reason": "hist"},
            {"key": "GER_the_four_year_plan", "id": "GER_the_four_year_plan", "tier": "S_historical", "score": 900, "reason": "ind"},
        ]
        AP.set_standing_orders(self.g, {"focus": {"fallback": "tier_score"}})
        AP.run_tick(self.g)
        self.assertEqual(self.g.cur_focus, "GER_ascension_of_todt")

        # 模拟事件 GER_inner_circle.01 选拒绝后 uncomplete_national_focus 退回未完成状态
        self.g.cur_focus = None
        AP.run_tick(self.g)
        # 绝不再重复点 GER_ascension_of_todt，自动顺延到下一个未启动过的国策！
        self.assertEqual(self.g.cur_focus, "GER_the_four_year_plan")

    # 20. E5: tier_preference 排除 S0_snowball 编译硬拦截 + allow_skip_snowball 显式放行与告警
    def test_e5_tier_preference_blocks_dropping_s0_snowball_unless_explicit(self):
        bad = AP.compile_profile(self.g, {
            "focus": {
                "fallback": "tier_score",
                "tier_preference": ["S0_playbook", "S_historical"],
            },
        })
        self.assertFalse(bad["ok"])
        self.assertTrue(any("S0_snowball" in e for e in bad["errors"]), bad["errors"])

        ok_skip = AP.compile_profile(self.g, {
            "focus": {
                "fallback": "tier_score",
                "tier_preference": ["S0_playbook", "S_historical"],
                "allow_skip_snowball": True,
            },
        })
        self.assertTrue(ok_skip["ok"], ok_skip)
        self.assertTrue(any("S0_snowball" in w for w in ok_skip["warnings"]), ok_skip["warnings"])

    # 21. E6 + E10 + 隐藏 bug: 事件 journal 详记选项效果 + 纯新闻放行 + has_idea/has_war_with 求值 + inspect(events) 保留 effects
    def test_e6_e10_event_audit_pure_news_triggers_and_inspect_preserves_effects(self):
        # (a) events_win._trig 支持 has_idea 与 has_war_with 纯只读求值
        self.g.active_ideas = ["war_economy"]
        ctx_ev = EW._EvalCtx(self.g, "GER")
        self.assertFalse(EW._trig([("has_idea", "HOL_aloof_neutrality")], "GER", ctx=ctx_ev))
        self.assertTrue(EW._trig([("has_idea", "war_economy")], "GER", ctx=ctx_ev))

        # (b) 纯 0 副作用 news_event（如 mtg_news.3）：即使他国选项 applicable=None，推荐适用项仍安全放行，且 journal 记齐审计字段
        AP.set_standing_orders(self.g, {"events": {"strategy": "auto_trivial"}})
        self.g.events_pending = [{
            "event_id": "mtg_news.3", "kind": "news_event", "title": "美国中立法案",
            "recommended": 2, "recommendation_basis": "applicable_conditional",
            "auto_resolve_option": 2,
            "options": [
                {"index": 1, "name": "选项1", "applicable": False, "effects": []},
                {"index": 2, "name": "让巨人沉睡。", "applicable": True, "effects": []},
                {"index": 5, "name": "荷兰中立", "applicable": None, "effects": []},
            ],
        }]
        tk_n = AP.run_tick(self.g)
        self.assertFalse(tk_n["should_wake"])
        self.assertEqual(self.g.events_answered[-1], ("mtg_news.3", 2))
        det = tk_n["actions"][0]["detail"]
        self.assertEqual(det["kind"], "news_event")
        self.assertEqual(det["option_name"], "让巨人沉睡。")
        self.assertEqual(det["basis"], "applicable_conditional")

        # (c) 多选项剧情事件 GER_inner_circle.01 仍绝对熔断，且 hoi4_inspect(events) 必须保留 effects 与 auto_resolve_option！
        self.g.events_pending = [{
            "event_id": "GER_inner_circle.01", "kind": "country_event", "title": "核心圈替换",
            "recommended": 1, "recommendation_basis": "applicable_unconditional",
            "auto_resolve_option": 1,
            "options": [
                {"index": 1, "name": "替换1", "applicable": True, "effects": ["set_variable = { GER_ascended_advisor_1 = 1 }"]},
                {"index": 4, "name": "拒绝", "applicable": True, "effects": ["uncomplete_national_focus = { focus = GER_ascension_of_todt }"]},
            ],
        }]
        insp_ev = TE.inspect_topics(self.g, [{"topic": "events"}])
        ev0 = insp_ev["results"][0]["data"]["events"][0]
        self.assertEqual(ev0["auto_resolve_option"], 1)
        self.assertTrue(ev0["options"][1]["effects"])
        tk_ic = AP.run_tick(self.g)
        self.assertTrue(any(w["floor"] == "decisive_event" for w in tk_ic["wake_triggers"]))

        # (d) 非新闻单选项带高危效果（declare_war）仍强制唤醒不静默点击
        self.g.events_pending = [{
            "event_id": "war_ultimatum.1", "kind": "country_event", "title": "最后通牒",
            "options": [{"index": 1, "name": "开战", "applicable": True, "effects": ["declare_war_on = { target = POL }"]}],
        }]
        tk_hi = AP.run_tick(self.g)
        self.assertTrue(any(w["floor"] == "decisive_event" for w in tk_hi["wake_triggers"]))

    # 22. E7: PP 议程留空时编译提示 + PP 逼近上限 (1500 / 1800 / 1950) 阶梯熔断防 2000 顶格浪费
    def test_e7_pp_near_cap_tiered_wakeup_even_with_empty_agenda(self):
        prof = {"pp_policy": {"agenda": [], "on_agenda_done": "hold", "reserve_pp": 250}}
        self.g.pp = 350.0
        comp = AP.compile_profile(self.g, prof)
        self.assertTrue(comp["ok"])
        self.assertTrue(any("agenda 为空" in w for w in comp["warnings"]), comp["warnings"])

        AP.set_standing_orders(self.g, prof)
        self.g.pp = 1550.0
        tk1 = AP.run_tick(self.g)
        self.assertTrue(any(w["floor"] == "interrupt_extra:pp_near_cap" for w in tk1["wake_triggers"]))
        # 同阶梯 (1600 < 1800) 不重复死锁轰炸
        self.g.pp = 1600.0
        tk2 = AP.run_tick(self.g)
        self.assertFalse(any(w["floor"] == "interrupt_extra:pp_near_cap" for w in tk2["wake_triggers"]))
        # 跨入 1800 阶梯再次唤醒
        self.g.pp = 1850.0
        tk3 = AP.run_tick(self.g)
        self.assertTrue(any(w["floor"] == "interrupt_extra:pp_near_cap" for w in tk3["wake_triggers"]))

    # 23. E8 + E11: 建造失败州 30 天冷却剔除（不再反复撞墙）+ 队列耗尽熔断 on_construction_queue_empty
    def test_e8_e11_construction_failed_state_cooldown_and_empty_queue_wake(self):
        # 州 64 (infra 最高) 模拟引擎拒建；首次失败后立刻改建州 52/59 且后续 tick 不再对州 64 撞墙！
        self.g.fail_construction_states = {64}
        self.g.const_queue = []
        AP.set_standing_orders(self.g, {
            "construction": {
                "target_queue_len": 2,
                "state_strategy": {"strategy": "infra_then_slots", "params": {"min_slots": 1}},
                "queue": [{"building": "arms_factory", "count": 1}],
                "rules": [],
            },
            "interrupts_extra": {"on_construction_queue_empty": True},
        })
        AP.run_tick(self.g)
        self.assertEqual(len(self.g.const_queue), 1)
        self.assertEqual(self.g.const_queue[0]["state"], 52)  # 64 失败后立刻转到次高 infra=4 的 52 州
        calls_after_first = len([c for c in self.g.add_construction_calls if c[0] == 64])
        self.assertEqual(calls_after_first, 1)

        # 显式 queue 配额建完且无 rules -> 队列变空后立刻触发 on_construction_queue_empty 唤醒
        self.g.const_queue = []
        self.g.advance_days(5)
        tk_empty = AP.run_tick(self.g)
        self.assertTrue(any(w["floor"] == "interrupt_extra:construction_queue_empty" for w in tk_empty["wake_triggers"]))
        # 冷却期内绝不再试州 64
        self.assertEqual(len([c for c in self.g.add_construction_calls if c[0] == 64]), 1)

    # 24. E9: ratio_hysteresis 在 idle_mil==0 时按冷却期从严重超储步枪线受控抽厂补紧缺火炮线
    def test_e9_production_surplus_rebalance_with_min_floor_and_cooldown(self):
        self.g.mil = 16
        self.g.prod_lines = [
            {"index": 0, "equipment": "infantry_equipment_1", "factories": 12, "class": "CMilitaryProductionLine", "obsolete": False},
            {"index": 1, "equipment": "artillery_equipment_1", "factories": 2, "class": "CMilitaryProductionLine", "obsolete": False},
            {"index": 2, "equipment": "fighter_equipment_0", "factories": 2, "class": "CMilitaryProductionLine", "obsolete": False},
        ]
        # 步枪库存 67,729（严重超储 > 4x 安全线），idle_mil == 0！
        self.g.stockpile_totals = {"infantry_equipment_1": 67729.0, "artillery_equipment_1": 500.0}
        AP.set_standing_orders(self.g, {
            "production": {
                "strategy": {
                    "strategy": "ratio_hysteresis",
                    "params": {
                        "ratios": {"inf": 0.25, "art": 0.45, "armor": 0.0, "air": 0.30},
                        "min_factories": {"inf": 4},
                        "rebalance_surplus": True,
                        "surplus_ratio_threshold": 2.5,
                        "rebalance_cooldown_days": 60,
                    },
                },
                "upgrade_strategy": "never",
            },
        })
        tk1 = AP.run_tick(self.g)
        # 步枪线应受控缩减（不低于 min_factories=4），火炮/空军紧缺线获得增厂且总军工守恒=16！
        inf_f = self.g.prod_lines[0]["factories"]
        art_f = self.g.prod_lines[1]["factories"]
        air_f = self.g.prod_lines[2]["factories"]
        self.assertLess(inf_f, 12)
        self.assertGreaterEqual(inf_f, 4)
        self.assertGreater(art_f, 2)
        self.assertEqual(inf_f + art_f + air_f, 16)
        self.assertTrue(any("rebalance_shrink" in a["action"] for a in tk1["actions"]))
        self.assertTrue(any("rebalance_grow" in a["action"] for a in tk1["actions"]))

        # 冷却期内（+15 天 < 60 天）不再重复拆线保护生产效率
        self.g.advance_days(15)
        tk2 = AP.run_tick(self.g)
        self.assertFalse(any("rebalance_" in a["action"] for a in tk2["actions"]))


if __name__ == "__main__":
    unittest.main()
