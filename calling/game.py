"""
The macro game layer. What a player actually interacts with.

No cheats: reads come straight from memory, and actions go through the game's own
CCommand queue, the same path a mouse click takes. Eligibility is checked by
calling the game's own functions.
"""
from __future__ import annotations
import os, struct, sys, time
from typing import Optional

from api import HOI4, Country
from commands import CommandLayer

from explore import cstring
import offsets as O
if sys.platform == "win32":
    # MSVC RTTI (PE) instead of Itanium RTTI (ELF); same {"vtables": ...} shape.
    import rtti_win as R
else:
    import rtti as R
from objindex import ObjectIndex

HERE = os.path.dirname(os.path.abspath(__file__))

# ------------------------------------------------------------------ offsets
# CCountry subsystems
# Every one of these is a pointer to a status sub-object, and the Windows build
# moved them (0xF38 -> 0xF60 etc), so prefer the verified Windows table.
CO_TECH_STATUS      = getattr(O, "CO_TECHNOLOGY_STATUS", 0x0F38)   # CTechnologyStatus*
CO_PRODUCTION       = getattr(O, "CO_PRODUCTION_STATUS", 0x0F40)   # CProductionStatus*
CO_ARMY_UPGRADES    = getattr(O, "CO_ARMY_UPGRADES_STATUS", 0x0F58)
CO_DIPLOMACY        = getattr(O, "CO_DIPLOMACY_STATUS", 0x0F60)    # CDiplomacyStatus*
CO_POLITICS         = getattr(O, "CO_POLITICAL_STATUS", 0x0F68)    # CPoliticalStatus*
CO_DECISIONS        = getattr(O, "CO_DECISION_STATUS", 0x0F78)     # CDecisionStatus*
CO_INTEL_AGENCY     = getattr(O, "CO_INTEL", getattr(O, "CO_INTELLIGENCE_AGENCY", 0x0F98))
CO_COUNTRY_INTEL    = getattr(O, "CO_COUNTRY_INTEL", None if sys.platform == "win32" else 0x0FC0)
CO_FOCUS_TREE       = getattr(O, "CO_FOCUS_TREE", 0x1338)   # CNationalFocusTree*
CO_CONT_FOCUS_PAL   = getattr(O, "CO_CONT_FOCUS_PAL", 0x1340)
CO_FOCUS_PROGRESS   = getattr(O, "CO_FOCUS_PROGRESS", 0x1348)   # CNationalFocusProgress*
CO_FACTION          = None if sys.platform == "win32" else 0x2B00
CO_STATES           = getattr(O, "CO_STATES", 0x460)   # CPdxArray<CState*>, CONTROLLED states
CO_OWNED_STATES     = 0x478                            # CPdxArray<CState*>, OWNED states
CO_CAPITAL_STATE    = getattr(O, "CO_CAPITAL_STATE", 0xFF0)   # int32 state id of the capital (None on PE)

# CNationalFocusTree
FT_FOCUSES          = 0x30     # CPdxArray<CNationalFocus*>

# CNationalFocus
NF_ID               = 0x08
NF_KEY              = 0x18     # CString
NF_LOCKEY           = 0x38
NF_NAME             = 0x60     # CString (localised)
NF_ICON             = 0x88
NF_AVAIL_TRIGGER_N  = 0x2DC
NF_PREREQ           = 0x568    # CPdxArray<CNationalFocusDependency*>
NF_MUTEX            = 0x580
NF_FILTERS          = 0x5D8

# CNationalFocusProgress
FP_COUNTRY          = 0x08
FP_CURRENT          = 0x10     # CNationalFocus* (0 = no focus selected)
FP_COMPLETED        = 0x40     # CPdxArray (1.19.4.1; was 0x20)
FP_ROOT_FOCUSES     = 0x70     # CPdxArray, the tree's root focuses (not 'selectable')
FP_DAILY            = 0x38

# CTechnologyStatus
TS_RESEARCHED       = 0x40     # CPdxArray<CTechnology*>
TS_ALL_TECHS        = 0x88     # CPdxArray<CTechnology*>
TS_SLOTS            = 0xA0     # CPdxArray<CResearchSlot*>
TS_PRODUCTION_LINES = 0x2B8

# CResearchSlot  (0x48 bytes; ctor 0xF4F4F0, dtor frees 0x48)
# The three fields below are exactly the ones `CResearchSlot::Save` (0xF61BF0)
# writes, with the engine's own member tags: +0x20 -> 0x31DE, +0x28 -> 0x34BB,
# +0x38 -> 0x3684.  They do NOT hold the live progress of the current
# technology: +0x20 is the progress carried over when a slot switches tech and
# +0x28 is the required amount computed at that moment (both stay 0/constant
# while a tech runs).  The bar the UI draws comes from the CTechnology object
# (TE_PROGRESS) against the template cost.
RS_OWNER            = 0x10
RS_CURRENT          = 0x18     # CTechnology* (0 = empty)
RS_PROGRESS         = 0x20     # carried-over progress, 1e5 fixed
RS_REQUIRED         = 0x28     # required for the carried-over progress, 1e5
RS_SPEED            = 0x38     # research speed, 1e5 fixed (1.01 = +1%/day)
RS_INDEX            = 0x40     # int32 slot index

# CTechnology (per-country state object)
TE_ID               = 0x08
TE_TEMPLATE         = 0x160    # CTechnologyTemplate* (vtable 0x29B9F10)
TE_PROGRESS         = 0x198    # int64 research days x1e5, speed-weighted
# CTechnologyTemplate (global definition)
TT_NAME             = getattr(O, "TT_NAME", 0x08)   # CString / std::string
TT_ID               = getattr(O, "TT_ID", 0x34)
TT_YEAR             = 0x3D8    # int32 start year (advanced_ship_hull_light -> 1943)
TT_RESEARCH_COST    = 0x3E0    # int32, days x1000 (150000 = 150 days)

# CProductionStatus
PS_LINES            = 0x58     # CPdxArray<CMilitaryProductionLine*/CNavalProductionLine*>
PS_MIL_LINES        = 0x58     # CPdxArray<CMilitaryProductionLine*>
PS_CONSTRUCTION     = 0x70     # CPdxArray<CBuildingProductionLine*>, construction queue
PS_VARIANTS_ALL     = 0xA0
PS_VARIANTS_ACTIVE  = 0xB8

# CMilitaryProductionLine / CNavalProductionLine
ML_OWNER            = 0x20
ML_PRIORITY         = 0xF0     # int32 (1.19.5; +0x10 holds unrelated data on some lines)
ML_FACTORIES        = 0x18     # int32, assigned factory count (1.19.5). Verified by
                               # sending a +3 delta and watching this field move 13 -> 16.
ML_UNIT_COST        = 0x28     # int64 x1e5, unit production cost (IC)
ML_OUTPUT           = 0x30     # int64 x1e5, daily output
ML_EFF_COST         = 0x38     # int64 x1e5, modified cost
ML_MAX_EFFICIENCY   = 0xC8     # int32, maximum efficiency (%)
ML_VARIANT          = 0x88     # CEquipmentVariant*
ML_TOTAL_PRODUCED   = 0xF8

# CBuildingProductionLine (construction)
BL_OWNER            = 0x20
# 2026-11-19: +0x30 is the line's CURRENT IC-per-DAY rate, not the accumulated
# work.  The old comment here claimed "+0x38 is a date/string" -- it is not.
# Measured by raw byte diff over 50 in-game hours on two live lines:
#   line A  15 factories:  +0x38  90,000,000 -> 126,000,000   (+360 IC in 2.08 d)
#   line B  14 factories:  +0x38  94,500,000 -> 132,300,000   (+378 IC in 2.08 d)
#   both:                  +0x30  unchanged (180.0 / 189.0 IC per day, x1e5)
# That is why the tool showed "frozen at 141.75 IC for 110 days" while a
# factory really completed: it was reading the rate.  +0x38 is the accumulator.
BL_PROGRESS         = 0x38     # int64 accumulated IC (x1e5, PE 1.19.5.0)
BL_RATE             = 0x30     # int64 current output in IC per day (x1e5)
BL_COST             = 0x28     # int64 total cost in IC-days (10800 for a civ factory)
BL_BUILDING         = 0x78     # CBuilding* (1.19.4.1; was 0x70 in 1.19.2)

# CEquipmentVariant
EV_TYPE_ID          = 0x18     # u32 -> equipment ARCHETYPE id, i.e. the low half of the
                               # u64 at CEquipmentVariant+0x18. It is looked up in
                               # CEquipmentDatabase+0x68 (457 entries in 1.19.5).
                               # 20201 = infantry_equipment_1.
# CEquipmentType
ET_ID               = 0x08     # entry of CEquipmentDatabase+0x68
ET_NAME             = 0x18     # CString (1.19.5; +0x10 held the ASCII "name" tag)
# CBuildingTemplate
BT_ID               = 0x08
BT_NAME             = getattr(O, "BT_NAME", 0x1E8)   # display name (None if the build has none)
BT_KEY              = getattr(O, "BT_KEY", 0x248)    # key ("industrial_complex")
# CBuilding (example)
BG_TYPE_ID          = 0x08
# CState
ST_ID               = getattr(O, "ST_ID", 0x48)   # int32, state id
# CBuildingReference { vptr; int32 state; int32 building_type; int32 province; }
BUILDING_REF_VTABLE = 0x4348A90   # CBuildingReference vtable, 1.19.4.1

# CPoliticalStatus
PL_PARTIES          = 0x20     # CPdxArray<CPoliticalParty*>
PL_ALL_IDEAS        = 0x38     # CPdxArray<CIdea*>  (every idea valid for the country)
PL_ACTIVE_IDEAS     = 0x50     # CPdxArray<CIdea*>, active spirits, laws and advisors
PL_RULING           = 0xD0
PL_PP               = 0xE0

# CDecisionStatus
DS_ALL              = 0x10     # CPdxArray<CDecision*>   every definition (GER 500)
DS_ACTIVE           = 0xD0     # CPdxArray<CDecision*>   takeable / candidate (GER 115)
DS_VISIBLE          = 0x88     # CPdxArray<CDecision*>   the country's visible decisions (GER 13)
DS_TARGETED         = 0xE8     # CPdxArray<CTargetedDecision*> (GER 31)
# 2026-11-22 (P1-4): decisions CURRENTLY IN PROGRESS.  Records are
# {vtable, CDecision*, ...}; measured live HOL/BEL/TUR/POR carry
# `seize_some_trains_woo` (days_remove 180) and GER carries nothing -- exactly the
# UI's "active" tab.  The old code mapped "active" to +0xD0, which is the
# candidate list (100+ rows while nothing was running).
DS_RUNNING          = 0x148

# CDiplomacyStatus
DP_RELATIONS        = 0x08     # CPdxArray<CRelationStatus*>  (one per country)

# item name offsets
IDEA_NAME           = 0x18
DECISION_NAME       = 0x120    # MSVC std::string key (PE 1.19.5.0; +0x118 read 8 bytes early)

# the game's own check functions
FN_GET_TECH_STATUS  = 0xD1FE30   # CTechnologyStatus* CCountry::GetTechnologyStatus()
FN_CAN_RESEARCH     = None  # 1.19.2 adresi (0x11CBF10) bu build'de gecersiz:
                            # cagirmak oyunu SIGSEGV ile dusuruyor. Yeniden bulunana kadar None.


def item_name(p, addr):
    """Name fields are sometimes CString and sometimes std::string. Try both."""
    return cstring(p, addr) or p.stdstring(addr) or None


def _count_by(rows, key):
    """{value: count} over one dict key (used for wing type histograms)."""
    out = {}
    for r in rows:
        v = r.get(key)
        if v is not None:
            out[v] = out.get(v, 0) + 1
    return out


# cache for Game._air_snapshot: player air wings, keyed by game pid
_AIR_SNAP = {"pid": None, "ts": 0.0, "rows": None}

# last training progress seen per conveyor, keyed by (pid, conveyor ptr).
# 2026-11-21 (P2-4): a conveyor that finishes a batch restarts at ~0.008; without
# remembering the previous value that looks like the progress counter broke.
_CONV_PROGRESS = {}


def _arr(p, obj, off, limit=100000):
    """CPdxArray<T*> -> list of pointers.

    `n` comes straight out of game memory. If the caller passes the wrong offset
    (or a stale object after a reload) n can be a garbage int32 and `p.read(d, 8*n)`
    asks for gigabytes: that is what OOM-killed the MCP child with 11 GB RSS during
    a long housekeep loop. Anything larger than `limit` is treated as "not an array
    we understand" and ignored.
    """
    try:
        n = p.i32(obj + off + O.PDXARRAY_SIZE)
        d = p.u64(obj + off + O.PDXARRAY_DATA)
        if n <= 0 or not d or n > limit or d < 0x10000:
            return []
        return list(struct.unpack("<%dQ" % n, p.read(d, 8 * n)))
    except OSError:
        # wrong offset / not an array of pointers: report empty instead of dying
        return []


def _count(p, obj, off):
    return p.i32(obj + off + O.PDXARRAY_SIZE)


import legality


def _wargoal_gate(target_tag, cb, justifying=None):
    """(problem, state) for declare_war's hard check 4 -- pure, unit-testable.

    2026-11-18: kept at MODULE level, not as a @staticmethod. `game.py` flattens
    its mixins with `setattr(Game, name, getattr(MacroMixin, name))` (line ~6536),
    and `getattr` on a staticmethod returns the plain function -- so a static
    method declared in MacroMixin comes back BOUND (self counts as the first
    argument) and `self._wargoal_gate(a, b, c)` dies with
    "takes from 2 to 3 positional arguments but 4 were given".

    A declaration with no casus belli is a cheat on this build: the Windows
    channel (`cmdwin.declare_war` -> CDeclareWarAction) bypasses the engine's own
    availability gate at `action+0x70`, so nothing except this check stands
    between "I feel like it" and a war the game would have refused.
    `cb` is the row `diplomacy.find_wargoal()` returned, or None.
    """
    tgt = str(target_tag or "").upper()
    if cb and isinstance(cb, dict) and cb.get("ptr"):
        return None, "have_cb"
    rows = []
    for j in (justifying or []):
        if not isinstance(j, dict):
            continue
        if str(j.get("target") or "").upper() == tgt:
            rows.append(j)
    if rows:
        rows.sort(key=lambda r: float("inf") if r.get("days_left") is None else float(r["days_left"]))
        j = rows[0]
        left = j.get("days_left")
        return ("no casus belli against %s YET: the justification is still "
                "running (%s days left, ETA %s). Declaring now would be a war "
                "without a war goal -- the Windows channel bypasses the "
                "engine's own availability gate, so the tool refuses. Wait for "
                "it, then confirm with hoi4_wargoals."
                % (tgt, left if left is not None else "?",
                   j.get("eta_date") or "?")), "justifying"
    return ("no casus belli against %s: call hoi4_justify_wargoal(target=%s) "
            "and wait for it to finish (hoi4_justifying shows days_left and "
            "eta_date). Declaring without one is a de-facto cheat -- the "
            "Windows action channel bypasses the engine's own "
            "wargoal-availability gate." % (tgt, tgt)), "missing"


class Game(HOI4):
    """The HOI4 handle plus the macro game mechanics."""

    def __init__(self, pid=None):
        super().__init__(pid)
        self.cmd = CommandLayer(self)
        self._oi = None
        self._tech_by_name = None
        self._tech_by_id = None
        self._idea_by_name = None
        self._decision_by_name = None

    # ------------------------------------------------------------ plumbing
    @property
    def oi(self) -> ObjectIndex:
        # 2026-11-06: the index holds absolute heap addresses. When the game is
        # restarted (crash reload, fresh campaign) and the MCP process survives, the
        # cached index still points at the OLD process: CTechnologyDatabase /
        # CBuildingDatabase resolved to None and every research/construction call
        # died with `TypeError: unsupported operand type(s) for +: 'NoneType' and 'int'`.
        #
        # 2026-11-18: the pid is NOT enough.  In-game "Restart game" rebuilds the
        # whole world inside the SAME process (pid unchanged), so a pid-only cache
        # handed out dead CAirWing pointers from the previous campaign --
        # hoi4_air_mission then posted handles that no longer resolved, and the
        # engine died at hoi4.exe+0xC08724 (country lookup over a bad pointer).
        # Fingerprint the world with the CCurrentGameState pointer, which is
        # recreated on every "Restart game".
        pid = None
        try:
            pid = self.p.pid
        except Exception:
            pass
        world = None
        try:
            world = self.p.u64(self.va(getattr(O, "G_GAMESTATE", 0x33DD6B0)))
        except Exception:
            world = None
        if (self._oi is None
                or getattr(self, "_oi_pid", None) != pid
                or (world and getattr(self, "_oi_world", None)
                    and world != self._oi_world)):
            self._oi = ObjectIndex(self.p, R.build()["vtables"],
                                   os.path.join(HERE, "objindex.pkl"))
            self._oi.build()
            self._oi_pid = pid
            self._oi_world = world
            # Clear class-level vtable scan cache too so same-PID restarts never reuse freed pointers
            type(self)._VT_SCAN.clear()
            # per-process caches derived from the index
            self._tech_by_name = None
            self._tech_by_id = None
        return self._oi

    def reset_caches(self):
        """Drop every world-derived cache -- call after in-game "Restart game".

        `hoi4_debug_eval("g.reset_caches()")` replaces the old
        `type(g)._VT_SCAN.clear()` ritual and ALSO invalidates the RTTI object
        index, which is the cache that actually goes stale across a restart
        (the pid does not change, so `oi` would happily reuse dead pointers).
        """
        type(self)._VT_SCAN = {}
        self._oi = None
        self._oi_pid = None
        self._oi_world = None
        self._tech_by_name = None
        self._tech_by_id = None
        self._idea_by_name = None
        self._decision_by_name = None
        self._eq_by_id = None
        self._bld_by_key = None
        self._bld_by_id = None
        self._adv_pool_cache = None
        self._faction_objects_cache = None
        self._tags = None
        self._tag2idx = None
        self._tags_pid = None
        self._moddefs = None
        self._cmds = None
        self._date_retry_at = 0
        # Only CACHE ATTRIBUTES here.  `_win_date_addr` is a METHOD -- nulling it
        # shadowed the method and every date read died with
        # `TypeError: 'NoneType' object is not callable` (2026-11-18, right after
        # the first version of this method shipped).
        #
        # 2026-11-18 (BUG-H): the DATE caches are part of this too.  Loading the
        # 1936.08 save into the same process left `hoi4_date` reporting
        # 1936.01.01 forever: `api.date_raw`'s monotonic guards compare against
        # `_date_last` / `_date_high`, and a save load is exactly the one event
        # where the clock may jump backwards or forwards by months. The forward
        # guard then reverted the live 1936.07.31 reading to the old 1936.01.01
        # value ("more than 30 days between two reads is only believable when
        # game.log agrees" -- and game.log had stopped being written at load).
        for attr in ("_date_exact_cache", "_date_addr", "_date_last", "_date_high",
                     "_date_static", "_date_verified", "_date_pid",
                     "_date_unverified", "_win_date", "_win_date_pid",
                     "_win_date_last"):
            try:
                setattr(self, attr, None)
            except Exception:
                pass
        try:
            self._dead_date_addrs = set()
        except Exception:
            pass
        return {"cleared": True}

    def rescan_objects(self):
        self.oi.build(force=True)

    @staticmethod
    def _settle(seconds=0.25):
        """Short wait so the game can process a command after it is queued."""
        time.sleep(seconds)

    def _c(self, tag) -> Country:
        return self.player() if tag in (None, "", "player") else self.country(tag)

    # ============================================================= FOCUSES
    def focus_tree(self, tag=None):
        c = self._c(tag)
        tree = self.p.u64(c.ptr + CO_FOCUS_TREE)
        return _arr(self.p, tree, FT_FOCUSES) if tree else []

    def _focus_info(self, f):
        p = self.p
        info = {
            "key": item_name(p, f + NF_KEY),
            "name": item_name(p, f + NF_NAME),
            "id": p.i32(f + NF_ID),
            "ptr": f,
            "prerequisites": _count(p, f, NF_PREREQ),
            "mutually_exclusive": _count(p, f, NF_MUTEX),
        }
        # 2026-11-19: on the PE build NF_PREREQ / NF_MUTEX do not point at focus
        # arrays (the tool printed `prerequisites: 475` and `mutually_exclusive:
        # 475` for every focus, which is a heap count, not a relationship). The
        # script files are the authority, and focusplan already parses them.
        if sys.platform == "win32" and info["key"]:
            try:
                import focusplan
                m = (focusplan.load().get("focuses") or {}).get(info["key"]) or {}
                if m:
                    groups = [g for g in (m.get("prereq_groups") or []) if g]
                    info["prereq_groups"] = groups
                    info["prerequisites"] = len(groups)
                    info["mutually_exclusive"] = len(m.get("mutex") or [])
                    info["relationship_source"] = "common/national_focus"
            except Exception:
                pass
        return info

    def _focus_prereq_groups(self, f):
        """Each prerequisite block is an OR-group of focus keys; all blocks are AND."""
        p = self.p
        out = []
        for d in _arr(p, f, NF_PREREQ):
            group = []
            for ff in _arr(p, d, 0x08):
                k = item_name(p, ff + NF_KEY)
                if k:
                    group.append(k)
            out.append(group)
        return out

    def _focus_mutex_keys(self, f):
        p = self.p
        out = []
        for x in _arr(p, f, NF_MUTEX):
            k = item_name(p, x + NF_KEY)
            if k:
                out.append(k)
            else:
                for y in _arr(p, x, 0x08):
                    kk = item_name(p, y + NF_KEY)
                    if kk:
                        out.append(kk)
        return out

    def focuses(self, tag=None, only_startable=False):
        """Focus list.  only_startable=True now means what a player means by it.

        2026-11-19: the old filter was `if info["prerequisites"]: continue`, i.e.
        "this focus has at least one prerequisite object" — which is true for
        every non-root focus AND false for every root one, regardless of what is
        already completed.  So the tool listed the 1936 opening focuses as
        "startable" in 1938, long after they were done, and hid every focus whose
        prerequisites were actually satisfied.

        The real predicate is: not completed, not the focus in progress, every
        `prerequisite` block satisfied (OR inside a block, AND across blocks),
        and no completed mutually-exclusive focus.  On the PE build those blocks
        are not readable from CNationalFocus, so they come from the parsed
        script files (focusplan schema 2), which is also what hoi4_focus_detail
        uses.
        """
        out = []
        completed = set()
        cur = 0
        static = None
        if only_startable:
            completed = set(self.completed_focuses(tag))
            prog = self.p.u64(self._c(tag).ptr + CO_FOCUS_PROGRESS)
            cur = self.p.u64(prog + FP_CURRENT) if prog else 0
            if sys.platform == "win32":
                try:
                    import focusplan
                    static = focusplan.load().get("focuses", {})
                except Exception:
                    static = {}
        for f in self.focus_tree(tag):
            info = self._focus_info(f)
            if only_startable:
                k = info["key"]
                if not k or k in completed or (cur and f == cur):
                    continue
                if static is not None:
                    m = static.get(k) or {}
                    groups = [g for g in (m.get("prereq_groups") or []) if g]
                    if any(not any(g in completed for g in grp) for grp in groups):
                        continue
                    if any(x in completed for x in (m.get("mutex") or [])):
                        continue
                elif info["prerequisites"]:
                    continue
            info["completed"] = info["key"] in completed
            out.append(info)
        return out

    def focus_block_reason(self, key, tag=None):
        """Why `key` cannot be started right now (None when nothing blocks it).

        Used by hoi4_set_focus when the engine takes the command but does not
        actually switch the focus — before this the tool answered
        `ok: false, posted: 1` with no explanation at all.
        """
        completed = set(self.completed_focuses(tag))
        if key in completed:
            return "already completed"
        prog = self.p.u64(self._c(tag).ptr + CO_FOCUS_PROGRESS)
        cur = self.p.u64(prog + FP_CURRENT) if prog else 0
        if cur:
            cur_key = item_name(self.p, cur + NF_KEY)
            if cur_key and cur_key != key:
                return ("another focus is already in progress (%s); "
                        "hoi4_drop_focus first" % cur_key)
        try:
            import focusplan
            m = (focusplan.load().get("focuses") or {}).get(key) or {}
        except Exception:
            m = {}
        groups = [g for g in (m.get("prereq_groups") or []) if g]
        unmet = [grp for grp in groups if not any(g in completed for g in grp)]
        if unmet:
            return ("prerequisites not met: " + " AND ".join(
                "(" + " OR ".join(grp) + ")" for grp in unmet))
        mx = [x for x in (m.get("mutex") or []) if x in completed]
        if mx:
            return "mutually exclusive with completed focus %s" % ", ".join(mx)
        return None

    def _wait_until(self, fn, timeout=4.0, step=0.25):
        """Komut kuyruga dusup islenene kadar bekle (Post asenkron)."""
        import time as _t
        end = _t.time() + timeout
        while _t.time() < end:
            try:
                if fn():
                    return True
            except Exception:
                pass
            _t.sleep(step)
        return False

    def _focus_cost_days(self, key):
        if not key:
            return None
        try:
            import focusplan
            m = (focusplan.load().get("focuses") or {}).get(key) or {}
            days = m.get("days")
            if days is not None and float(days) > 0:
                return float(days)
        except Exception:
            pass
        return 70.0

    def current_focus(self, tag=None):
        c = self._c(tag)
        prog = self.p.u64(c.ptr + CO_FOCUS_PROGRESS)
        if not prog:
            return None
        cur = self.p.u64(prog + FP_CURRENT)
        if not cur:
            return None
        info = self._focus_info(cur)
        info["completed_count"] = _count(self.p, prog, FP_COMPLETED)
        # 2026-11-22 (复测 5 §3.6): CFocusProgress+0x38 (fixed 1e5) 保存当前国策已推进天数，
        # 结合脚本耗时即可算出 days_left 与完成百分比，不再让模型盲等 70 天。
        try:
            days_done = round(self.p.i64(prog + 0x38) / float(O.FIXED), 3)
            cost_days = self._focus_cost_days(info.get("key"))
            info["progress"] = days_done
            info["days_done"] = round(days_done, 2)
            info["cost_days"] = cost_days
            if cost_days and cost_days > 0:
                info["days_left"] = round(max(0.0, cost_days - days_done), 2)
                info["percent"] = round(min(100.0, days_done / cost_days * 100.0), 1)
        except Exception:
            pass
        return info

    def find_focus(self, key, tag=None):
        for f in self.focus_tree(tag):
            if item_name(self.p, f + NF_KEY) == key:
                return f
        return None

    def set_focus(self, key, tag=None):
        c = self._c(tag)
        f = self.find_focus(key, tag)
        if not f:
            raise KeyError("focus not found: %s" % key)
        if sys.platform == "win32":
            import cmdwin
            r = cmdwin.set_national_focus(self, f, c.tag_id)
            self._settle()
            prog = self.p.u64(c.ptr + CO_FOCUS_PROGRESS)
            got = self.p.u64(prog + FP_CURRENT) if prog else 0
            r.update({"focus": key, "current": hex(got) if got else None,
                      "ok": bool(r.get("ok")) and got == f})
            if not r["ok"]:
                reason = r.get("reason") or self.focus_block_reason(key, tag)
                r["reason"] = reason or (
                    "the engine took the command but did not switch the focus; "
                    "its `available` trigger (manpower / army ratio / political "
                    "power / war state) is the remaining candidate — the same "
                    "focus keeps failing until that condition changes")
            return r
        with self:
            self.cmd.send("SetNationalFocus", tag=c.tag_id, focus_ptr=f)
        self._settle()

        def _ok():
            prog = self.p.u64(c.ptr + CO_FOCUS_PROGRESS)
            return bool(prog) and self.p.u64(prog + FP_CURRENT) == f
        ok = self._wait_until(_ok)
        return {"ok": ok, "focus": key}

    def set_focus_ptr(self, focus_ptr, tag=None):
        """Start a national focus by its LIVE tree pointer.

        hoi4_selectable_focuses returns pointers read straight out of the running focus
        tree, while hoi4_set_focus resolves the key through a cached lookup that goes
        stale after a game restart. For focuses the game lists as selectable but
        set_focus refuses (Anschluss / inner-circle focuses), post the pointer directly.
        """
        c = self._c(tag)
        f = int(focus_ptr)
        if sys.platform == "win32":
            import cmdwin
            p = self.p          # 2026-11-20: this branch referenced a bare `p`
                                # that only exists in the POSIX path below, so
                                # the tool raised NameError every single time.
            r = cmdwin.set_national_focus(self, f, c.tag_id)
            self._settle()
            prog = p.u64(c.ptr + CO_FOCUS_PROGRESS)
            got = p.u64(prog + FP_CURRENT) if prog else 0
            r.update({"focus_ptr": hex(f), "current": hex(got) if got else None,
                      "ok": bool(r.get("ok")) and got == f})
            if not r["ok"] and not r.get("reason"):
                k = item_name(p, f + NF_KEY)
                r["reason"] = self.focus_block_reason(k, tag) if k else (
                    "the pointer does not resolve to a focus in the live tree")
            return r
        with self:
            self.cmd.send("SetNationalFocus", tag=c.tag_id, focus_ptr=f)
        self._settle()

        def _ok():
            prog = self.p.u64(c.ptr + CO_FOCUS_PROGRESS)
            return bool(prog) and self.p.u64(prog + FP_CURRENT) == f
        ok = self._wait_until(_ok)
        return {"ok": ok, "focus_ptr": hex(f), "focus": self.p.u64(self.p.u64(c.ptr + CO_FOCUS_PROGRESS) + FP_CURRENT) == f}

    def drop_focus(self, tag=None):
        c = self._c(tag)
        before = self.current_focus(tag)
        # 2026-11-22 (P3-3): 无进行中国策时直接返回带 reason 的结构化拒绝，
        # 不浪费 _settle() 也不遗漏 reason 字段。
        if not before:
            return {"ok": False, "posted": 0, "was": None, "current": None,
                    "reason": "no focus in progress"}
        if sys.platform == "win32":
            import cmdwin
            # 2026-11-20: `was` came from cmdwin, which reports the PLAYER's
            # current focus -- dropping LUX's focus reported Germany's.  Read it
            # for the country actually being asked about.
            r = cmdwin.drop_national_focus(self, c.tag_id)
            self._settle()
            prog = self.p.u64(c.ptr + CO_FOCUS_PROGRESS)
            got = self.p.u64(prog + FP_CURRENT) if (prog and FP_CURRENT is not None) else 0
            ok = bool(r.get("ok")) and got == 0
            r.update({"was": before.get("key") if isinstance(before, dict) else None,
                      "current": hex(got) if got else None,
                      "ok": ok})
            if not ok and not r.get("reason") and not r.get("error"):
                r["reason"] = "focus remained active after drop command (game may be paused)"
            return r
        with self:
            self.cmd.send("DropCurrentNationalFocus", tag=c.tag_id)
        self._settle()

        def _ok():
            prog = self.p.u64(c.ptr + CO_FOCUS_PROGRESS)
            return bool(prog) and self.p.u64(prog + FP_CURRENT) == 0
        ok = self._wait_until(_ok)
        out = {"ok": ok, "was": before.get("key") if isinstance(before, dict) else None}
        if not ok:
            out["reason"] = "focus remained active after drop command"
        return out

    # ========================================================== RESEARCH
    def _win_vt_cache(self, class_name):
        """The cached RTTI vtable-scan hits for one class, or None if absent.

        _VT_SCAN is keyed by (pid, class name); the key shape has changed before,
        so match on the last tuple element instead of assuming a position --
        BUT never return another process's hits.

        2026-11-17: the old version matched on `k[-1]` alone, so after a game
        restart the new Game object picked up the DEAD pid's array addresses.
        `_kick_tech_scan()` then thought the scan was already cached, never
        re-ran it, `_load_tech_db()` got stale pointers, `_tech_by_name` stayed
        None for the whole process and every research call died with
        "'NoneType' object has no attribute 'get'".  Research looked "broken"
        after every restart because of this.
        """
        cache = getattr(type(self), "_VT_SCAN", None) or {}
        pid = getattr(getattr(self, "p", None), "pid", None)
        if pid is not None:
            direct = cache.get((pid, class_name))
            # 2026-11-23 (C-2 根因修复): 空列表 [] 绝不能当成“已就绪缓存”，
            # 否则冷启动/重启瞬间扫出 0 条会永久锁死科技库直到 mcp_load 重启进程！
            if direct:
                return direct
        # 后台线程可能并发写入 _VT_SCAN，使用快照列表遍历避免 RuntimeError
        for k, v in list(cache.items()):
            if not (isinstance(k, tuple) and k and k[-1] == class_name):
                continue
            if len(k) >= 2 and isinstance(k[0], int) and pid is not None:
                if k[0] != pid:
                    continue          # hits belong to a previous game process
            if v:
                return v
        return None

    def _kick_tech_scan(self):
        """Start the CTechnologyTemplate scan in the background (20-40 s)."""
        if self._win_vt_cache("CTechnologyTemplate") is not None:
            return False
        pid = getattr(getattr(self, "p", None), "pid", None)
        run_key = (pid, "CTechnologyTemplate")
        flag = getattr(type(self), "_VT_SCAN_RUNNING", None)
        if flag is None:
            flag = set()
            type(self)._VT_SCAN_RUNNING = flag
        if run_key in flag:
            return False
        import threading

        def _work():
            try:
                self.win_scan_class("CTechnologyTemplate")
            except Exception:
                pass
            finally:
                flag.discard(run_key)

        flag.add(run_key)
        threading.Thread(target=_work, daemon=True).start()
        return True

    TECH_IDS_CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  "techids.pkl")

    def _tech_ids_from_disk(self):
        """Provisional live-tech-id -> name table (stable across processes)."""
        try:
            import pickle
            with open(self.TECH_IDS_CACHE, "rb") as f:
                d = pickle.load(f)
            ids = (d or {}).get("ids") or {}
            return {int(k): v for k, v in ids.items()} if ids else None
        except Exception:
            return None

    def _save_tech_ids(self, byid):
        try:
            import pickle
            with open(self.TECH_IDS_CACHE, "wb") as f:
                pickle.dump({"schema": 1, "ids": dict(byid)}, f, protocol=4)
        except Exception:
            pass

    def _load_tech_db(self, wait_s=0.0):
        """Build the technology name/id tables.

        2026-11-18: `wait_s` makes the call BLOCK until the background
        CTechnologyTemplate RTTI scan lands instead of returning a cold table.
        Before this, every caller had to retry by hand: `hoi4_research_available`
        raised "technology database not ready ... retry this call" and stayed
        broken for the whole turn, `hoi4_fill_research` found no candidates, and
        `start_research` could not resolve the id -> template pointer at all.

        2026-11-19: the id -> name half of that table only depends on the game's
        own data (a live CTechnology carries the template id at +0x08, the
        template carries it at +0x3C), so it is cached to disk and used
        provisionally until the RTTI scan rebuilds it.  Before this, the first
        `hoi4_research` after every reload printed `researching: null` for all
        four slots even though `slot_busy` was true.
        """
        # 2026-11-17: _selfheal_check() (server.py) DELETES these attributes when
        # the game pid changes, so a plain `self._tech_by_name` raised
        #   AttributeError: 'Game' object has no attribute '_tech_by_name'
        # on the first research call after every reload.  Read them defensively
        # and re-seed the defaults.
        if getattr(self, "_tech_by_name", None) is not None:
            return
        if not hasattr(self, "_tech_by_name"):
            self._tech_by_name = None
        if not hasattr(self, "_tech_by_id"):
            self._tech_by_id = None
        if self._tech_by_id is None and sys.platform == "win32":
            provisional = self._tech_ids_from_disk()
            if provisional:
                self._tech_by_id = provisional
                self._tech_ids_provisional = True
        p = self.p
        if sys.platform == "win32":
            # CTechnologyDatabase has no RTTI-visible singleton on hoi4.exe; the
            # old path retried oi.singleton() twice, rebuilt the whole ObjectIndex
            # and burned 35 s before failing with
            # "TypeError: 'NoneType' object is not iterable".
            #
            # 2026-11-16: CSetResearchCommand wants a **CTechnologyTemplate***,
            # not a live CTechnology.  Its CanExecute (0x11F3360) calls
            # 0x0F51A30, which is literally
            #     return ts->data[tech->+0x38 - 1];      // +0x38 is 1-BASED
            # The live objects in CTechnologyStatus+0x088 all have +0x38 == 0,
            # so every command built from one was refused in C++ *silently*
            # (measured: 0 of 552 accepted, which is why research has never
            # worked on this build and all four slots stayed empty).
            # A CTechnologyTemplate has +0x38 = 1..552 and +0x10 = script key;
            # feeding the template gives CanExecute = 1.
            self._kick_tech_scan()
            if wait_s:
                import time as _time
                _deadline = _time.time() + float(wait_s)
                pid = getattr(getattr(self, "p", None), "pid", None)
                run_key = (pid, "CTechnologyTemplate")
                last_retry = 0.0
                while (_time.time() < _deadline and
                       self._win_vt_cache("CTechnologyTemplate") is None):
                    running = getattr(type(self), "_VT_SCAN_RUNNING", set())
                    # 2026-11-23 (C-2): 若后台冷扫因刚重启/加载中扫出空表已退出，
                    # 主动间隔重扫，绝不傻等到超时或永久死锁在空科技库！
                    if run_key not in running and (_time.time() - last_retry) >= 2.5:
                        last_retry = _time.time()
                        try:
                            self.win_scan_class("CTechnologyTemplate", use_cache=False)
                        except Exception:
                            pass
                        if self._win_vt_cache("CTechnologyTemplate") is not None:
                            break
                    _time.sleep(0.5)
            tmpl = self._win_vt_cache("CTechnologyTemplate")
            byname, byid = {}, {}
            if tmpl:
                id2name = {}
                for h in tmpl:
                    try:
                        nm = item_name(p, h + 0x10)
                    except Exception:
                        continue
                    if not nm or not (1 < len(nm) < 60):
                        continue
                    if not all(32 < ord(ch) < 127 for ch in nm):
                        continue
                    if nm in byname:
                        continue
                    i38 = p.i32(h + 0x38)
                    if not (1 <= i38 <= 4096):
                        continue          # not a real template object
                    byname[nm] = h
                    id2name.setdefault(p.i32(h + 0x3C), nm)
                # _tech_by_id stays keyed by the id a LIVE CTechnology carries
                # at +0x08 (== template +0x3C); research_slots() needs that to
                # name the technology sitting in a slot.
                ts = p.u64(self._c(None).ptr + CO_TECH_STATUS)
                for t in (_arr(p, ts, 0x88) if ts else []):
                    k = p.i32(t + 0x08)
                    nm = id2name.get(k)
                    if nm:
                        byid[k] = nm
            self._tech_by_name = byname
            if byid:
                self._tech_by_id = byid
                self._save_tech_ids(byid)
            elif getattr(self, "_tech_by_id", None):
                # Cold scan: keep the provisional id -> name table loaded from
                # disk instead of clobbering it with {} (that is what made
                # `hoi4_research` print `researching: null` after every reload).
                pass
            else:
                self._tech_by_id = byid
            self._tech_scan_pending = not byname
            if not byname:
                # Nothing cached yet: leave the table unloaded so the next call
                # retries instead of staying dead for the whole process.
                self._tech_by_name = None
            return
        db = self.oi.singleton("CTechnologyDatabase")
        if not db:
            self._oi = None
            db = self.oi.singleton("CTechnologyDatabase")
        if not db:
            # Built too early (main menu / campaign still loading): do NOT cache an
            # empty table, otherwise research stays dead for the whole process.
            self._tech_by_name = None
            self._tech_by_id = {}
            self._oi = None
            return
        byname, byid = {}, {}
        for off in (0x48, 0x78, 0xD8):
            for t in _arr(p, db, off):
                nm = item_name(p, t + TT_NAME)
                if nm:
                    byname[nm] = t
                    byid[p.i32(t + TT_ID)] = nm
        if not byname:
            # The database object exists but is still empty (it is filled after the
            # campaign finishes loading).  Caching {} here made every later lookup
            # come back empty for the rest of the process -- hoi4_research_available
            # then reported count 0 for good.
            self._tech_by_name = None
            self._tech_by_id = {}
            self._oi = None
            return
        self._tech_by_name, self._tech_by_id = byname, byid

    def tech_name(self, tech_obj):
        """CTechnology* (country object) -> technology name"""
        self._load_tech_db()
        return (self._tech_by_id or {}).get(self.p.i32(tech_obj + TE_ID))

    def tech_status(self, tag=None):
        return self.p.u64(self._c(tag).ptr + CO_TECH_STATUS)

    def tech_cost_days(self, tech_name):
        """Base research time of a technology, in days.

        `CTechnologyTemplate+0x3E0` holds days x1000 on the PE build (verified:
        basic_machine_tools 150000 -> "130日"-class values, synth_oil_experiments
        300000, improved_light_tank_chassis 200000).  The old 0x3D0 is not the
        cost: it reads -1 for most techs (0x3D8 is the start year).
        """
        self._load_tech_db()
        t = (self._tech_by_name or {}).get(tech_name)
        if not t:
            return None
        raw = self.p.i32(t + TT_RESEARCH_COST)
        if not (0 < raw < 100000 * 1000):
            return None
        return round(raw / 1000.0, 1)

    def tech_progress(self, tech_obj):
        """Research days already invested in a CTechnology, speed-weighted."""
        if not tech_obj:
            return 0.0
        return self.p.i64(tech_obj + TE_PROGRESS) / float(O.FIXED)

    def research_slots(self, tag=None):
        """What each research slot is doing, with the UI's own numbers.

        Progress lives on the technology, not on the slot:
            progress_days = CTechnology+0x198 / 1e5
            cost_days     = CTechnologyTemplate+0x3E0 / 1000
            speed         = CResearchSlot+0x38 / 1e5   (days gained per day)
        Verified two ways on 1.19.5.0 (2026-11-15):
          * every tech the country starts with shows progress == cost * 1.1
            (56/56 at exactly 1.1, the start-of-game research speed);
          * a running tech gains exactly `speed` x 1e5 per daily tick
            (basic_machine_tools 3535000 -> 3636000 -> ... , speed 101000).
        """
        p = self.p
        # 2026-11-17: without this the very first read after a save load prints
        # `researching: null, progress: 0` for all four slots -- the slots are
        # actually busy, only _tech_by_id (the id -> name map) is not built yet.
        # hoi4_overview then fired a FALSE "RESEARCH_IDLE: 4 slots empty" alert.
        self._load_tech_db()
        ts = self.tech_status(tag)
        out = []
        for i, s in enumerate(_arr(p, ts, TS_SLOTS)):
            cur = p.u64(s + RS_CURRENT)
            nm = self.tech_name(cur) if cur else None
            speed = p.i64(s + RS_SPEED) / float(O.FIXED) or 1.0
            prog_days = self.tech_progress(cur) if cur else 0.0
            cost_days = self.tech_cost_days(nm) if nm else None
            # 2026-11-21 (P2-13): was `... if cost_days else 0.0`, so a slot with
            # 55.35 days done and `required_known: false` reported `percent: 0.0`
            # -- indistinguishable from "just started" for anything that sorts or
            # thresholds on it.  Unknown must stay unknown (None), the same way
            # `required` already does.
            pct = round(100.0 * prog_days / cost_days, 2) if cost_days else None
            rec = {
                "slot": i,
                "researching": nm,
                # slot_busy is the authoritative "is this slot occupied" flag:
                # it is a raw pointer read and does NOT depend on the tech-name
                # database being warm.  Never call a slot idle because the NAME
                # is missing.
                "slot_busy": bool(cur),
                # 2026-11-19 (measured): on the tick a technology finishes, the
                # slot still holds the pointer and the old name while
                # percent == 100 / days_remaining == 0 for one or more reads.
                # `fill_research` and the RESEARCH_IDLE alert both keyed off
                # slot_busy and therefore missed the free slot. Report it
                # separately instead of lying about slot_busy.
                "slot_done": bool(cur and cost_days and prog_days >= cost_days * 0.999),
                "tech_ptr": ("0x%x" % cur) if cur else None,
                "progress": round(prog_days, 3),          # days of research done
                # 2026-11-20 (P2 #12): `required: 0.0` looked like "this costs
                # nothing" while it really meant "CTechnologyTemplate is not warm
                # yet".  null says unknown; `required_known` makes it explicit.
                "required": round(cost_days, 3) if cost_days else None,  # days to do
                "required_known": bool(cost_days),
                "percent": None if pct is None else min(pct, 100.0),
                "speed": speed,
                "base_days": cost_days,
            }
            if cur and nm is None:
                rec["name_unknown"] = ("technology database not warm yet -- "
                                       "the slot IS occupied, retry in ~90 s")
            if cost_days:
                rec["days_remaining"] = round(max(cost_days - prog_days, 0.0) / speed, 1)
            # 2026-11-21 (P3-4): `speed` below 1.0 is the ahead-of-time penalty (the
            # tech's historical year has not arrived), not "research got slower".
            # The 2026-11-20 playtest read 0.296 and assumed a bug.
            if speed < 0.999:
                rec["speed_note"] = ("below 1.0 = ahead-of-time penalty: this tech's "
                                     "historical year has not arrived yet; "
                                     "days_remaining already accounts for it")
            out.append(rec)
        # 2026-11-22 (复测 5 §8.4 时刻 ①): 当某槽刚完成科研（slot_done=True）时，
        # 立刻比对新解锁装备是否导致现有产线过时，实现零延迟提醒。
        done_techs = [r["researching"] for r in out if r.get("slot_done") and r.get("researching")]
        if done_techs:
            try:
                import equipmeta as _em
                _lines = self.production_lines(tag, include_obsolete=False)
                _avail = self._active_variant_names(tag)
                _hits = _em.check_tech_unlocks_obsolete(done_techs, _lines, _avail)
                if _hits:
                    _by_tech = {}
                    for _tk in done_techs:
                        _u = set(_em.tech_unlocks(_tk))
                        _matched = [h for h in _hits if h.get("upgrade_to") in _u]
                        if _matched:
                            _by_tech[_tk] = _matched
                    for r in out:
                        if r.get("slot_done") and r.get("researching") in _by_tech:
                            r["unlocks_obsolete_lines"] = _by_tech[r["researching"]]
                            r["obsolete_alerts"] = [
                                _em.format_obsolete_alert(h) for h in _by_tech[r["researching"]]
                            ]
            except Exception:
                pass
        return out

    def researched(self, tag=None):
        return sorted(filter(None, (self.tech_name(t)
                                    for t in _arr(self.p, self.tech_status(tag), TS_RESEARCHED))))

    def available_research(self, tag=None):
        """Technologies researchable by the static tree, with ZERO injected calls.

        Pure memory read for the live state (researched set + occupied slots) and
        common/technologies/*.txt for start_year / prerequisites.  The final
        legality check is still done by the game's own CanExecute inside RUN_CMD
        when hoi4_start_research posts SetResearch.
        """
        import re, techdata
        # 2026-11-16: this used to iterate `self._tech_by_name` without loading it,
        # so the very first call of a session died with
        # "'NoneType' object is not iterable" and four research slots sat empty
        # until a hoi4_start_research happened to warm the cache.  Load it here and
        # say so when the 30 s vtable scan is still running.
        self._load_tech_db(wait_s=60)
        if self._tech_by_name is None:
            raise RuntimeError(
                "technology database still not ready after waiting 30 s: the "
                "CTechnologyTemplate vtable scan has not finished (it is ~20-40 s "
                "on this build, longer on a cold ObjectIndex). Call "
                "hoi4_status once, wait a few seconds and retry.")
        defs = techdata.load()
        current_year = self.date()["year"]
        max_year = current_year + 1
        researched = set(self.researched(tag))
        active = {s["researching"] for s in self.research_slots(tag) if s.get("researching")}
        my_tag = self._c(tag).tag
        order = {"infantry": 0, "industry": 1, "computing": 2,
                 "armor": 3, "air": 4, "artillery": 5,
                 "support": 6, "naval": 7}
        rows = []
        for nm in self._tech_by_name:
            d = defs.get(nm)
            if not d or d.get("category") not in order:
                continue
            if nm in researched or nm in active:
                continue
            if d.get("always_no"):
                continue
            # Focus-granted modifier techs have no folder; they are not in the tree.
            if not d.get("folder"):
                continue
            allow_tags = d.get("allow_tags") or []
            if allow_tags and my_tag not in allow_tags:
                continue
            # GER_xxx is ours; any other uppercase tag prefix is foreign content.
            m = re.match(r"^([A-Z][A-Za-z0-9]{1,})_", nm)
            if m and m.group(1) != my_tag:
                continue
            if int(d.get("year") or 1936) > max_year:
                continue
            parents = d.get("parents") or []
            if parents and not any(par in researched for par in parents):
                continue
            year = int(d.get("year") or 1936)
            score = techdata.score_tech(nm, d.get("folder", ""), d.get("categories", []),
                                        year, current_year, tag=my_tag)
            rows.append((score, year, nm))
        rows.sort(key=lambda r: (-r[0], r[1], r[2]))
        return [nm for _, _, nm in rows]

    def _check_foreground_guard(self):
        """纯只读检查游戏窗口是否处于前台（绝不调用 SetForegroundWindow 抢焦点）。

        仅在 Windows 真实进程上拦截；返回 None 表示通过，否则返回 (blocked_by, error, ws)。
        """
        if sys.platform != "win32":
            return None
        proc = getattr(self, "p", None)
        pid = getattr(proc, "pid", None)
        if not pid or not isinstance(pid, int):
            return None
        try:
            import windowstate as _ws
            ws = _ws.state(pid)
        except Exception:
            return None
        # 纯离线 mock 测试对象无真实句柄且找不到该 pid 的窗口，不误拦截
        if ws.get("hwnd") is None:
            if getattr(proc, "h", None):
                return (
                    "foreground",
                    "game window not found for pid %d: refusing to hijack the render thread." % pid,
                    ws,
                )
            return None
        if ws.get("minimized"):
            return (
                "foreground",
                "game window is minimized (pid %d): refusing to hijack the render thread. "
                "Restore and focus 'Hearts of Iron IV' and retry." % pid,
                ws,
            )
        if ws.get("focused") is False:
            return (
                "foreground",
                "game window is not in the foreground (pid %d): refusing to hijack the "
                "render thread. Bring 'Hearts of Iron IV' to the foreground and retry." % pid,
                ws,
            )
        return None

    @staticmethod
    def _classify_research_failure(res=None, exc=None):
        """区分科研失败类型：('environment', blocked_by) 必须中止，('content', blocked_by) 才可换候选。"""
        _ENV_CODES = {"foreground", "process_gone", "timeout", "injector_error"}
        if isinstance(res, dict):
            bb = res.get("blocked_by")
            if bb in _ENV_CODES:
                return ("environment", bb)
            if bb:
                return ("content", str(bb))
        msg = ""
        if exc is not None:
            msg = "%s: %s" % (type(exc).__name__, exc)
        elif isinstance(res, dict) and res.get("error"):
            msg = str(res.get("error"))
        low = msg.lower()
        if ("not in the foreground" in low or "restoredeviceobjects" in low
                or "window_minimized" in low or "window_hidden" in low
                or "window is minimized" in low or "window not found" in low):
            return ("environment", "foreground")
        if (isinstance(exc, TimeoutError) or "-32001" in low
                or "timed out" in low or "timeout" in low):
            return ("environment", "timeout")
        if (isinstance(exc, OSError) or "getlasterror=299" in low
                or "readprocessmemory" in low or "writeprocessmemory" in low
                or "openprocess" in low or "no cingameidler" in low
                or "process" in low and ("died" in low or "not found" in low or "exited" in low)):
            return ("environment", "process_gone")
        if exc is not None and type(exc).__name__ == "CallError":
            return ("environment", "injector_error")
        if "mutually exclusive" in low or "xor" in low:
            return ("content", "mutually_exclusive")
        if "canexecute refused" in low or "can_execute" in low:
            return ("content", "can_execute_refused")
        if "no free research slot" in low:
            return ("content", "no_free_slot")
        if isinstance(exc, KeyError) or "no such technology" in low:
            return ("content", "unknown_tech")
        if isinstance(res, dict) and res.get("can_execute") is False:
            return ("content", "can_execute_refused")
        return ("content", "rejected")

    def start_research(self, tech_name, slot=None, tag=None):
        self._load_tech_db(wait_s=60)
        c = self._c(tag)
        tmpl = (self._tech_by_name or {}).get(tech_name)
        if not tmpl:
            if getattr(self, "_tech_scan_pending", False):
                raise RuntimeError(
                    "technology table still loading: the CTechnologyTemplate "
                    "RTTI scan takes 20-40 s. Retry in about a minute.")
            raise KeyError("no such technology: %s" % tech_name)
        if slot is None:
            slots = self.research_slots(tag)
            # slot_busy is the authoritative occupancy flag; `researching` is
            # None whenever the name table is cold (2026-11-18).
            free = [s["slot"] for s in slots
                    if not s.get("slot_busy") or s.get("slot_done")]
            if not free:
                return {"ok": False, "blocked_by": "no_free_slot",
                        "failure_class": "content", "error": "no free research slot"}
            slot = free[0]
        # 2026-11-22 (复测 5 §3.8): 提前拦截已研究完的科技（包括槽内 slot_done=True
        # 刚完成但尚未清槽的科技），明确返回 already_researched 而非含糊的 not_occupied。
        try:
            _researched_now = set(self.researched(tag))
        except Exception:
            _researched_now = set()
        if tech_name in _researched_now:
            return {"ok": False, "tech": tech_name, "slot": slot,
                    "blocked_by": "already_researched",
                    "failure_class": "content",
                    "error": "%s is already researched" % tech_name}

        prev_slot_state = None
        try:
            _pre_slots = self.research_slots(tag)
            if 0 <= slot < len(_pre_slots):
                prev_slot_state = _pre_slots[slot]
        except Exception:
            prev_slot_state = None
        if (prev_slot_state and prev_slot_state.get("slot_done")
                and prev_slot_state.get("researching") == tech_name):
            return {"ok": False, "tech": tech_name, "slot": slot,
                    "blocked_by": "already_researched",
                    "failure_class": "content",
                    "error": "%s just finished in slot %d (slot_done=True)" % (tech_name, slot)}

        # 2026-11-19 (cheat-level, measured live): `CSetResearchCommand::CanExecute`
        # does NOT enforce the script `XOR = { ... }` block — only the UI's
        # `available` predicate does.  fill_research therefore started
        # concentrated_industry AND dispersed_industry at the same time, which no
        # player can do.  Refuse here.
        try:
            import techdata as _td
            _info = (_td.load() or {}).get(tech_name) or {}
            _xor = _info.get("xor") or []
            if _xor:
                _done = _researched_now
                _active = {s["researching"] for s in self.research_slots(tag)
                           if s.get("researching")}
                _bad = sorted(set(_xor) & (_done | _active))
                if _bad:
                    return {"ok": False, "tech": tech_name, "slot": slot,
                            "blocked_by": "mutually_exclusive",
                            "failure_class": "content",
                            "conflicts_with": _bad,
                            "error": (
                                "%s is mutually exclusive with %s (XOR block in "
                                "common/technologies). The engine would accept it "
                                "but a player cannot. Nothing was sent."
                                % (tech_name, ", ".join(_bad)))}
        except Exception:
            pass
        if sys.platform == "win32":
            # Real command channel on the PE build: hand-built CSetResearchCommand
            # + Post(session, cmd). See calling/cmdwin.py.
            import cmdwin
            try:
                res = cmdwin.set_research(self, tmpl, slot, c.tag_id)
            except Exception as exc:
                fcls, fbb = self._classify_research_failure(exc=exc)
                return {
                    "ok": False,
                    "tech": tech_name,
                    "slot": slot,
                    "blocked_by": fbb,
                    "failure_class": fcls,
                    "posted": 0,
                    "error": str(exc),
                }
            if res.get("can_execute") is False:
                return {
                    "ok": False,
                    "tech": tech_name,
                    "slot": slot,
                    "blocked_by": "can_execute_refused",
                    "failure_class": "content",
                    "can_execute": False,
                    "posted": 0,
                    "error": res.get("error") or "CSetResearchCommand::CanExecute refused",
                }
            self._settle()
            after = self.research_slots(tag)
            # 2026-11-22 (复测 5 §3.8 / §3.9): 严格校验槽位确实被新科技占住：
            # 1) 必须 slot_busy=True 且 slot_done=False（防止把刚完成未清槽的旧科技误判成 ok）；
            # 2) 若发送前该槽已有旧科技且处于 slot_done，必须看到 tech_ptr 变更；
            # 3) 二次回读 0.15s 防止瞬态未清槽造成的假 ok（随后槽又变空）。
            def _slot_matches(got):
                if not got or not got.get("slot_busy") or got.get("slot_done"):
                    return False
                if not (got.get("researching") in (None, tech_name)):
                    return False
                if prev_slot_state and prev_slot_state.get("slot_done"):
                    if (prev_slot_state.get("tech_ptr")
                            and got.get("tech_ptr") == prev_slot_state.get("tech_ptr")):
                        return False
                return True

            got = after[slot]
            ok = _slot_matches(got)
            if ok:
                self._settle(0.15)
                after = self.research_slots(tag)
                got = after[slot]
                ok = _slot_matches(got)
            out = {"ok": ok,
                   "slot": slot,
                   "researching": got.get("researching"),
                   "slot_busy": got.get("slot_busy"),
                   "slot_done": got.get("slot_done"),
                   "can_execute": res.get("can_execute"),
                   "posted": res.get("posted"),
                   "drained": res.get("drained"),
                   "error": res.get("error")}
            if not ok:
                try:
                    if tech_name in set(self.researched(tag)):
                        out["failure_class"] = "content"
                        out["blocked_by"] = "already_researched"
                        out["error"] = out.get("error") or ("%s is already researched" % tech_name)
                        return out
                except Exception:
                    pass
                fcls, fbb = self._classify_research_failure(res=out)
                out["failure_class"] = fcls
                out["blocked_by"] = fbb if fbb != "rejected" else "not_occupied"
            return out
        with self:
            if FN_CAN_RESEARCH is not None and \
                    not self.call(FN_CAN_RESEARCH, (self.tech_status(tag), tmpl, 0)) & 0xFF:
                return {"ok": False, "blocked_by": "can_execute_refused",
                        "failure_class": "content",
                        "error": "%s su an arastirilamaz" % tech_name}
            self.cmd.send("SetResearch", tag=c.tag_id, tech_ptr=tmpl, slot=slot, flag=0)
        self._settle()

        def _ok():
            s = self.research_slots(tag)[slot]
            return bool(s.get("slot_busy")) and \
                s.get("researching") in (None, tech_name)
        ok = self._wait_until(_ok)
        after = self.research_slots(tag)
        out = {"ok": ok, "slot": slot, "researching": after[slot]["researching"],
               "slot_busy": after[slot].get("slot_busy")}
        if not ok:
            out["blocked_by"] = "not_occupied"
            out["failure_class"] = "content"
        return out

    def fill_all_research_slots(self, preferred_techs=None, tag=None, max_attempts=30):
        """Fill EVERY empty research slot, trying preferred techs first then the
        score-sorted available list.  After each command the slot memory is read
        back; content rejections (XOR / CanExecute) move to the next candidate,
        whereas environment errors (foreground guard / dead process / timeout)
        abort immediately without degrading to lower-priority technologies."""
        slots = self.research_slots(tag)
        # 2026-11-21 (P2-12): housekeep could report "4 slots still empty" after a
        # whole run.  The candidate list comes from the technology DATABASE, which
        # is cold right after a server start, and an empty candidate list makes
        # this a silent no-op.  Warm it explicitly first (cheap when already warm).
        try:
            self._load_tech_db(wait_s=5.0)
        except Exception:
            pass
        # 2026-11-18: occupancy must come from slot_busy, never from the tech NAME.
        # `researching` is None whenever the name table is cold, which made this
        # tool believe four busy slots were empty and post four more researches.
        # 2026-11-19: a slot whose tech hit 100 % still reports busy for a tick or
        # two; include those (slot_done) so the free slot is not missed.
        empty = [s["slot"] for s in slots
                 if not s.get("slot_busy") or s.get("slot_done")]
        empty_before = list(empty)
        attempts = []
        preferred = [str(x) for x in (preferred_techs or []) if x]
        attempted = set()
        if not empty:
            return {"ok": True, "empty_before": [], "empty_after": [],
                    "filled": 0, "slots": slots, "attempts": attempts}
        # 2026-11-22 (P3 前台守卫修复): 入口先做一次纯只读前台检查，
        # 游戏不在前台时立刻拒绝、一个槽都不填，绝不尝试候选也不静默降级。
        fg_guard = self._check_foreground_guard()
        if fg_guard is not None:
            fg_code, fg_err, fg_ws = fg_guard
            return {
                "ok": False,
                "aborted": True,
                "blocked_by": fg_code,
                "failure_class": "environment",
                "error": fg_err,
                "window": fg_ws,
                "empty_before": empty_before,
                "empty_after": empty_before,
                "filled": 0,
                "slots": slots,
                "attempts": attempts,
            }
        # 2026-11-16: a failed tech-db scan must not abort the whole fill -- the
        # preferred list is still worth trying, and start_research warms the cache
        # itself.
        try:
            avail = self.available_research(tag)
            avail_err = None
        except Exception as e:
            avail, avail_err = [], "%s: %s" % (type(e).__name__, e)
        if not avail:
            # 2026-11-21 (P2-12): one more attempt after actually waiting for the
            # background scan.  Before this, a cold process returned zero
            # candidates and the whole step was a silent no-op that still left
            # `ok` true in some paths.
            try:
                self._load_tech_db(wait_s=10.0)
                avail = self.available_research(tag)
                avail_err = None
            except Exception as e:
                avail_err = "%s: %s" % (type(e).__name__, e)
        # 2026-11-22 (复测 5 §3.8): 候选池（含调用方显式传入的 preferred）必须先减去
        # 已研究全集以及当前任何科研槽中正在/刚完成（slot_done）的科技，避免把刚 100%
        # 完成的科技（如 oil_processing）重复再选一遍。
        try:
            already_done = set(self.researched(tag))
        except Exception:
            already_done = set()
        in_slots = {s.get("researching") for s in slots if s.get("researching")}
        exclude_techs = already_done | in_slots
        candidates = [
            x for x in (preferred + [y for y in avail if y not in preferred])
            if x not in exclude_techs
        ]
        if not candidates:
            return {"ok": False, "empty_before": empty_before, "empty_after": empty_before,
                    "filled": 0, "slots": slots, "attempts": attempts,
                    "available_error": avail_err,
                    "error": avail_err or "no candidate technology to try"}
        aborted_reason = None
        aborted_error = None
        for slot in list(empty):
            for tech in candidates:
                if tech in attempted:
                    continue
                attempted.add(tech)
                exc_caught = None
                try:
                    res = self.start_research(tech, slot=slot, tag=tag)
                except Exception as e:
                    exc_caught = e
                    fcls, fbb = self._classify_research_failure(exc=e)
                    res = {"ok": False, "blocked_by": fbb,
                           "failure_class": fcls, "error": str(e)}
                ok = bool(res.get("ok"))
                # 2026-11-22 (复测 5 §3.9): 以目标槽真实占用（slot_busy=True 且 slot_done=False）
                # 为最终准绳，防止单条 attempt 报 ok=True 但最终槽仍空。
                if ok and ("slot_busy" in res or "slot_done" in res):
                    if not res.get("slot_busy") or res.get("slot_done"):
                        ok = False
                        res = dict(res)
                        res["ok"] = False
                        res.setdefault("blocked_by", "not_occupied")
                        res.setdefault("failure_class", "content")
                if ok:
                    r_class, r_code = "ok", None
                else:
                    r_class = res.get("failure_class")
                    r_code = res.get("blocked_by")
                    if not r_class or not r_code:
                        r_class, r_code = self._classify_research_failure(
                            res=res, exc=exc_caught)
                entry = {
                    "slot": slot,
                    "tech": tech,
                    "ok": ok,
                    "reason_class": r_class,
                    "blocked_by": r_code,
                    "error": res.get("error"),
                }
                attempts.append(entry)
                if ok:
                    break
                # 2026-11-22 (P3): 环境错误（前台焦点丢失/进程消失/超时/注入失败）
                # 必须立即中止整个 fill，绝不把高优科技降级成低分候选。
                if r_class == "environment":
                    aborted_reason = r_code
                    aborted_error = res.get("error") or ("aborted due to %s" % r_code)
                    break
                if len(attempts) >= max_attempts:
                    break
            if aborted_reason is not None or len(attempts) >= max_attempts:
                break
        try:
            after = self.research_slots(tag)
            empty_after = [s["slot"] for s in after
                           if not s.get("slot_busy") or s.get("slot_done")]
        except Exception:
            after = slots
            empty_after = list(empty_before)
        out = {"ok": (not empty_after) and (aborted_reason is None),
               "empty_before": empty_before,
               "empty_after": empty_after,
               "filled": max(0, len(empty_before) - len(empty_after)),
               "tech_db_warm": self._tech_by_name is not None,
               "slots": after, "attempts": attempts}
        if aborted_reason is not None:
            out["aborted"] = True
            out["blocked_by"] = aborted_reason
            out["failure_class"] = "environment"
            out["error"] = aborted_error
        if avail_err:
            out["available_error"] = avail_err
        return out

    # =========================================================== DECISIONS
    def _run_cmd(self, cmd):
        """CCommand::Run (CanExecute -> Post) with a valid libstdc++ std::string sink.

        Five call sites used to do `errbuf = malloc(0x20); write(b"\\0"*0x20)`. That
        leaves _M_p = NULL, so the moment CanExecute wrote a rejection message the
        game either dereferenced NULL or freed a wild pointer — the "law/decision
        randomly refused or crashed" bug. One helper, one correct SSO string.
        """
        if sys.platform == "win32":
            # PE: 0x20AE0A0 is a Linux VA and the error sink is a libstdc++
            # std::string.  `cmdwin.enqueue` stamps the session tick/seq and hands
            # the object to the queue, which is the path every working Windows
            # command (focus / research / construction / war) takes.
            import cmdwin
            rv = cmdwin.enqueue(self, cmd)
            self._last_cmd_error = ""
            return rv
        s = self.malloc(0x20)
        self.p.write(s, struct.pack("<QQ", s + 0x10, 0) + b"\0" * 16)
        try:
            rv = self.call(0x20AE0A0, (cmd, s))
            self._last_cmd_error = self._read_stdstring_at(s)
        finally:
            try:
                ptr = self.p.u64(s)
                if ptr and ptr != s + 0x10:
                    self.free(ptr)
                self.free(s)
            except Exception:
                pass
        return rv

    def _read_stdstring_at(self, s):
        try:
            ptr, ln = self.p.u64(s), self.p.u64(s + 8)
            if ptr and 0 < ln < 512:
                return (self.p.try_read(ptr, ln) or b"").decode("utf-8", "replace")
        except Exception:
            pass
        return ""

    def _decision_name(self, d):
        return item_name(self.p, d + DECISION_NAME)

    def decisions(self, tag=None, which="visible", rich=True):
        """Decisions, optionally with everything a player needs to judge one.

        2026-11-19: this used to return `{key, ptr}` and nothing else, so the
        tool was unusable — "GER_plan_z" tells you nothing about what it costs,
        whether it is a debug decision, or when it can be taken.  The script key
        is still the identity, but the row now carries the localised title, the
        category, the political-power cost, the run time and a readable slice of
        the `available` trigger (parsed from common/decisions, see
        decisions_meta.py).  `ptr` stays for hoi4_select_decision.

        2026-11-22 (P1-4): `which` now means what it says.
          * "visible"  — the country's visible tab        (CDecisionStatus+0x88)
          * "takeable" — everything the engine lists as candidate (+0xD0)
          * "active"   — decisions CURRENTLY IN PROGRESS   (+0x148)
          * "all"      — every definition                  (+0x10)
        Before this, "active" pointed at +0xD0, which is the candidate list: it
        returned 100+ rows at a point where nothing was running, and the UI's own
        "active" tab was empty.  +0x148 holds {vtable, CDecision*, ...} records;
        measured live, HOL/BEL/TUR/POR all carry `seize_some_trains_woo`
        (days_remove 180) there and GER carries nothing.
        """
        c = self._c(tag)
        me = c.tag
        ds = self.p.u64(c.ptr + CO_DECISIONS)
        if which == "active":
            rows = []
            known_tags = self._known_country_tags()
            for item in self._running_decisions(ds):
                nm = item["key"]
                row = {"key": nm, "ptr": item["ptr"], "running": True}
                if rich:
                    row.update(self.decision_meta(nm))
                    vt = self._decision_tag_scope(row.get("visible"),
                                                  row.get("available"),
                                                  key=nm, known_tags=known_tags)
                    row["visible_tag"] = vt
                    row["mine"] = (vt is None or vt == me)
                rows.append(row)
            return rows
        off = {"all": DS_ALL, "takeable": DS_ACTIVE,
               "visible": DS_VISIBLE}[which]
        known_tags = self._known_country_tags() if rich else None
        out = []
        for d in _arr(self.p, ds, off):
            nm = self._decision_name(d)
            if not nm:
                continue
            row = {"key": nm, "ptr": d}
            if rich:
                row.update(self.decision_meta(nm))
                vt = self._decision_tag_scope(row.get("visible"), row.get("available"),
                                              key=nm, known_tags=known_tags)
                row["visible_tag"] = vt
                # CDecisionStatus+0x88 is NOT strictly "this country's tab": the
                # engine also lists decisions that belong to another tag (their
                # `visible` block starts with `tag = ENG`, or key starts with `JAP_`/`SIA_`).
                row["mine"] = (vt is None or vt == me)
            out.append(row)
        if rich and which == "visible":
            out.sort(key=lambda r: (not r.get("mine"), r["key"]))
        return out

    def _running_decisions(self, ds):
        """Decisions in progress for one country (CDecisionStatus+0x148).

        The records are {vtable, CDecision*, ...} with a stride wider than 8, and
        the exact stride has not been disassembled; instead of guessing it, the
        block is scanned 8 bytes at a time for pointers that resolve to a decision
        name.  That is stride-independent and skips the vtable/padding words.
        """
        p = self.p
        out, seen = [], set()
        try:
            data = p.u64(ds + DS_RUNNING)
            size = p.i32(ds + DS_RUNNING + 12)
        except Exception:
            return out
        if not data or not (0 < size <= 512):
            return out
        for off in range(0, size * 0x20, 8):
            try:
                v = p.u64(data + off)
            except Exception:
                break
            if not v or v in seen:
                continue
            nm = self._decision_name(v)
            if nm:
                seen.add(v)
                out.append({"key": nm, "ptr": v})
        return out

    def _known_country_tags(self):
        """当前游戏存在的全部 3 字母国家 TAG 集合（用于识别 JAP_/SIA_ 等外邦决议前缀，避开 MTG/DLC）。"""
        cached = getattr(self, "_known_tags_set", None)
        if cached:
            return cached
        try:
            tags = {str(t).upper() for t in (self.tags() or ()) if t and len(str(t)) == 3}
        except Exception:
            tags = set()
        if tags:
            self._known_tags_set = frozenset(tags)
            return self._known_tags_set
        return frozenset()

    @staticmethod
    def _decision_tag_scope(*triggers, key=None, known_tags=None):
        """The country a decision is scoped to, looking at triggers and TAG_ key prefix."""
        for vis in triggers:
            if not vis:
                continue
            m = re.search(r"\b(?:original_tag|tag)\s*=\s*([A-Z]{3})\b", vis)
            if m:
                return m.group(1)
        # 2026-11-23 (C-6): 如 JAP_sue_for_peace_nuke_mission / SIA_standing_our_ground_mission
        # 在 visible/available 里没写 tag=XXX，但 key 前缀本身是明确的他国 3 字母 TAG；
        # 仅当该前缀属于真实国家 TAG 集合（自动排除 MTG/AAT/BBA/NSB/DLC 等）时归属该国。
        if key:
            km = re.match(r"^([A-Z]{3})_", str(key))
            if km:
                pref = km.group(1)
                if known_tags and pref in known_tags:
                    return pref
        return None

    @staticmethod
    def _decision_visible_tag(vis):
        """The country a decision's `visible` trigger is scoped to, if any."""
        if not vis:
            return None
        m = re.search(r"\b(?:original_tag|tag)\s*=\s*([A-Z]{3})\b", vis)
        return m.group(1) if m else None

    def decision_meta(self, key):
        """Static metadata for one decision key (title / cost / trigger slice)."""
        try:
            import decisions_meta
            m = decisions_meta.get(key) or {}
        except Exception:
            m = {}
        try:
            import loc
            title = loc.title(key)
        except Exception:
            title = None
        return {
            "title": title,
            "category": m.get("category"),
            "cost_pp": m.get("cost_pp"),
            "days_remove": m.get("days_remove"),
            "days_mission_timeout": m.get("days_mission_timeout"),
            "fire_only_once": m.get("fire_only_once"),
            "is_good": m.get("is_good"),
            "has_available": m.get("has_available"),
            "available": m.get("available"),
            "visible": m.get("visible"),
            "meta_source": "common/decisions" if m else "not in script files",
        }

    def decision_definitions(self):
        """key -> CDecision* from the game's own CDecisionDatabase (4187 entries)."""
        if getattr(self, "_dec_defs", None) is not None:
            return self._dec_defs
        p = self.p
        db = self.oi.singleton("CDecisionDatabase")
        out = {}
        for it in _arr(p, db, 0x28):
            nm = item_name(p, it + DECISION_NAME)
            if nm:
                out[nm] = it
        self._dec_defs = out
        return out

    def select_decision(self, key, tag=None):
        """Take a decision and PROVE it ran.

        2026-11-22 (P1-2): this used to return `ok` = the command's own return
        code, so a decision the engine refused (GER_plan_z, whose `visible` needs a
        focus that was never completed) came back as `ok:true, error:""` with
        nothing done.  "Visible" is not "available".  A decision only counts as
        taken when something observable changes: political power moves, or the
        decision shows up in the country's in-progress list (+0x148).  Otherwise
        the call fails and says why.
        """
        c = self._c(tag)
        target = None
        source = None
        for which in ("visible", "takeable", "all"):
            for d in self.decisions(c.tag, which, rich=False):
                if d["key"] == key:
                    target = d["ptr"]; source = which; break
            if target:
                break
        if not target:
            # Generic decisions (improved_worker_conditions, infrastructure_building_slot,
            # ...) are in the database but not in the country's "visible" array until a
            # category becomes visible. The command takes the definition pointer.
            target = self.decision_definitions().get(key)
            source = "database"
        if not target:
            raise KeyError("decision not found: %s" % key)
        pp0 = c.political_power
        try:
            running0 = {r["key"] for r in self.decisions(c.tag, "active", rich=False)}
        except Exception:
            running0 = set()
        with self:
            cmd = self.cmd.make("SelectDecision", tag=c.tag_id, decision_ptr=target)
            # CCommand::Run: CanExecute -> Post(queue, cmd, 1), the click path.
            rv = self._run_cmd(cmd)
        ran = bool(rv & 0xFF)
        for _ in range(8):
            self._settle(0.35)
            pp1 = c.political_power
            if pp1 != pp0:
                return {"ok": True, "decision": key, "source": source, "ran": ran,
                        "verified_by": "political_power",
                        "political_power_before": pp0, "political_power_after": pp1}
            try:
                running1 = {r["key"] for r in self.decisions(c.tag, "active", rich=False)}
            except Exception:
                running1 = running0
            if key in running1:
                return {"ok": True, "decision": key, "source": source, "ran": ran,
                        "verified_by": "active_list",
                        "political_power_before": pp0, "political_power_after": pp1}
        meta = self.decision_meta(key) or {}
        why = []
        if meta.get("available"):
            why.append("its `available` trigger is: %s" % meta["available"][:220])
        return {"ok": False, "decision": key, "source": source, "ran": ran,
                "verified_by": None,
                "error": ("the engine did not run it: political power, the in-progress "
                          "list and the decision itself are all unchanged after ~3 s. "
                          "A decision can be VISIBLE and still not takeable -- check "
                          "hoi4_decision_detail(key).available. " + " ".join(why)).strip(),
                "political_power_before": pp0,
                "political_power_after": c.political_power}

    # ==================================================== POLITICS AND IDEAS
    def _idea_entries(self, tag=None, which="all"):
        """Country idea instances: key, pointer, and the law-category object at +0xAA0.

        PL_ALL_IDEAS and PL_ACTIVE_IDEAS point at per-country CIdea instances. They are
        the correct operands for CReplaceIdeaCommand; CIdeaDatabase entries are only
        definitions and are not accepted as a law replacement operand.
        """
        p = self.p
        c = self._c(tag)
        pol = p.u64(c.ptr + CO_POLITICS)
        off = PL_ALL_IDEAS if which == "all" else PL_ACTIVE_IDEAS
        out = []
        for i in _arr(p, pol, off):
            nm = item_name(p, i + IDEA_NAME)
            if nm:
                out.append({"key": nm, "ptr": i, "category": p.u64(i + 0xAA0)})
        return out

    def politics(self, tag=None):
        p = self.p
        c = self._c(tag)
        pol = p.u64(c.ptr + CO_POLITICS)
        active = self._idea_entries(tag, "active")
        ruling_ideo = None
        try:
            import legality as _leg
            ruling_ideo = _leg.ruling_ideology(self, c.tag)
        except Exception:
            ruling_ideo = None
        return {
            "political_power": c.political_power,
            "ruling_party_popularity": c.ruling_party_popularity,
            "ruling_ideology": ruling_ideo,
            "active_ideas": [a["key"] for a in active],
            "active_idea_ptrs": active,
            "available_idea_count": _count(p, pol, PL_ALL_IDEAS),
            "party_count": _count(p, pol, PL_PARTIES),
        }

    def available_ideas(self, tag=None, filter_text=""):
        out = []
        for e in self._idea_entries(tag, "all"):
            if not filter_text or filter_text.lower() in e["key"].lower():
                out.append(e)
        return out

    def _find_idea(self, key, tag=None):
        for e in self._idea_entries(tag, "all"):
            if e["key"] == key:
                return e
        return None

    def _active_idea_in_category(self, category, tag=None):
        if not category:
            return None
        for e in self._idea_entries(tag, "active"):
            if e["category"] == category:
                return e
        return None

    def _replace_cmd(self, old, new, tag=None):
        c = self._c(tag)
        return self.cmd.make("CReplaceIdea", tag=c.tag_id,
                             old_ptr=old["ptr"], new_ptr=new["ptr"])

    def _free_cmd(self, cmd_ptr):
        """Return a command object to the game's heap.

        `cmd.make()` allocates with the game's operator new. A command that is
        only used for a dry-run CanExecute is never posted, so the game never
        frees it. Leaking one per probe used to add ~500 MB of game memory per
        hoi4_overview call, which OOM-killed the whole box (the MCP child hit
        7.8 GB and the attached game died with it).
        """
        if not cmd_ptr:
            return
        try:
            self.free(cmd_ptr)
        except Exception:
            pass

    def _replace_can_execute(self, old, new, tag=None):
        """Call CReplaceIdeaCommand::CanExecute (vtable +0x50) without posting.

        Safe as a dry run: it only reads the command, country and both idea objects.
        The command object is freed again right after the check.
        """
        if sys.platform == "win32":
            return False
        import api
        api.allow_call(0x1CED690)  # CReplaceIdeaCommand::CanExecute, disassembled above
        cmd = self._replace_cmd(old, new, tag)
        try:
            fn = self.p.u64(self.va(0x4415D70) + 0x50)
            return bool(self.call(fn, (cmd, 0), absolute=True) & 0xFF)
        finally:
            self._free_cmd(cmd)

    def replace_idea(self, key, tag=None):
        """Replace the current law in the same category, using the game's own command."""
        c = self._c(tag)
        new = self._find_idea(key, tag)
        if not new:
            raise KeyError("idea not found: %s" % key)
        old = self._active_idea_in_category(new["category"], tag)
        if not old:
            raise KeyError("no active idea in category for: %s" % key)
        before = c.political_power
        # 2026-11-22 (P1-3): work out WHY it would be refused before we post, so a
        # silent failure can be explained.  Live case: war_economy came back
        # run:true / error:"" / active:false while the real reason was PP -30
        # against a 150 PP law -- the caller could only guess.
        why = self._idea_block_reason(key, c, tag)
        with self:
            cmd = self._replace_cmd(old, new, tag)
            rv = self._run_cmd(cmd)
        # The law change lands on a later tick: poll instead of one 0.25s nap.
        active = set()
        for _ in range(10):
            self._settle(0.3)
            active = set(self.politics(c.tag)["active_ideas"])
            if key in active:
                break
        out = {"ok": key in active, "idea": key, "old": old["key"],
               "method": "replace", "run": bool(rv & 0xFF),
               "error": getattr(self, "_last_cmd_error", ""),
               "pp_before": before, "pp_after": c.political_power,
               "active": key in active}
        if key not in active:
            out["reason"] = why or ("the engine refused the law change "
                                    "(CanExecute said no; nothing else changed)")
        return out

    def _idea_block_reason(self, key, c, tag=None):
        """Best-effort explanation of why a law change would be refused."""
        reasons = []
        try:
            s = c.summary()
            pp = int(s.get("political_power") or 0)
            ws = float(s.get("war_support") or 0)
        except Exception:
            return None
        gate = self._LAW_GATE.get(key)
        if gate:
            need_ws, need_war = gate
            if need_war:
                try:
                    at_war = bool(self.wars(tag or c.tag).get("at_war"))
                except Exception:
                    at_war = False
                if not at_war:
                    reasons.append("needs a war (this law is war-only)")
            if ws < need_ws:
                reasons.append("needs war_support >= %.2f (have %.2f)" % (need_ws, ws))
            if pp < 150:
                reasons.append("costs 150 political power (have %d)" % pp)
        return "; ".join(reasons) or None

    def can_replace_idea(self, key, tag=None):
        new = self._find_idea(key, tag)
        if not new:
            return {"id": key, "legal": False, "error": "idea not found"}
        old = self._active_idea_in_category(new["category"], tag)
        if not old:
            return {"id": key, "legal": False, "error": "no active idea in category"}
        with self:
            ok = self._replace_can_execute(old, new, tag)
        return {"id": key, "legal": ok, "current": old["key"],
                "method": "replace", "ptr": new["ptr"]}

    # Law pre-filter table: law -> (min war support, needs war).  PURE PYTHON.
    # The game's own CanExecute still runs exactly once, inside CCommand::Run,
    # when hoi4_add_idea / hoi4_replace_idea actually posts the command.  This
    # table only decides what to ADVERTISE, so no ptrace injection is needed to
    # build the candidate list.
    # Values read from this build's own script files (2026-11-12):
    #   common/ideas/_economic.txt  low 0.15 / partial 0.25 / war 0.50 / tot 0.80
    #   common/ideas/_manpower.txt  limited 0.10 / extensive 0.20 / service 0.60
    #                               all_adults 0.70 / scraping 0.85
    # The old table advertised low/partial at 0.0, so `hoi4_available_ideas` called
    # a law legal that CReplaceIdeaCommand::CanExecute then refused.
    _LAW_GATE = {
        "free_trade": (0.0, False),
        "export_focus": (0.0, False),
        "limited_exports": (0.0, False),
        "civilian_economy": (0.0, False),
        "low_economic_mobilisation": (0.15, False),
        "partial_economic_mobilisation": (0.25, False),
        "war_economy": (0.50, False),
        "extensive_conscription": (0.20, False),
        "autarkic_economy": (0.50, False),
        "service_by_requirement": (0.60, True),
        "closed_economy": (0.0, True),
        "tot_economic_mobilisation": (0.80, True),
        "totaler_krieg_economy": (0.80, True),
        "all_adults_serve": (0.70, True),
        "all_men_serve": (0.80, True),
        "scraping_the_barrel": (0.85, True),
        "total_mobilization": (0.80, True),
    }

    # Law hierarchy. `recommended_ideas` used to advertise ANY law of the same
    # category that was not active -- including civilian_economy while the country
    # was already on partial_economic_mobilisation. An agent that followed the
    # recommendation downgraded the country's economy.
    _LAW_RANK = {
        "undisturbed_isolation": 0, "isolation": 1,
        "civilian_economy": 2, "low_economic_mobilisation": 3,
        "partial_economic_mobilisation": 4, "war_economy": 5,
        "autarkic_economy": 5, "tot_economic_mobilisation": 6,
        "totaler_krieg_economy": 7,
        "disarmed_nation": 0, "volunteer_only": 1, "limited_conscription": 2,
        "extensive_conscription": 3, "service_by_requirement": 4,
        "all_adults_serve": 5, "all_men_serve": 5, "scraping_the_barrel": 6,
    }

    def recommended_ideas(self, tag=None, force=False):
        """Law candidates, pre-filtered IN PYTHON -- zero game calls, zero ptrace.

        `hoi4_overview` used to probe every candidate law with a real
        `CReplaceIdeaCommand::CanExecute` injection (~15 command allocations per
        cold call, dozens of stop/start cycles on the main thread).  That was the
        "overview takes several seconds" complaint AND the biggest source of
        `free(): invalid pointer`: every probe allocated a command in the game's
        heap.  The list below is now built from memory reads only:

          * free_trade / export_focus / limited_exports / partial_economic_mobilisation
            are always advertised when they are not the active law;
          * war_economy / extensive_conscription need war_support >= 0.50 or a war;
          * the late-war tiers additionally require a war and higher war support.

        `legal` is therefore a PRE-FILTER verdict, not a verdict from the game.
        The authoritative check is the single CanExecute that CCommand::Run does
        when the law is actually changed; a refusal shows up as ok=false there.
        """
        c = self._c(tag)
        active = {a["key"] for a in self._idea_entries(tag, "active")}
        summary = c.summary()
        ws = float(summary.get("war_support") or 0.0)
        try:
            import combat
            at_war = bool(combat.at_war(self, c.tag))
        except Exception:
            at_war = False
        order = ["free_trade", "export_focus", "limited_exports", "autarkic_economy",
                 "closed_economy", "civilian_economy", "low_economic_mobilisation",
                 "partial_economic_mobilisation", "war_economy",
                 "tot_economic_mobilisation", "totaler_krieg_economy",
                 "extensive_conscription", "service_by_requirement", "all_adults_serve",
                 "scraping_the_barrel", "all_men_serve", "total_mobilization"]
        have = {i["key"]: i for i in self._idea_entries(tag, "all")}
        out, blocked = [], []
        for k in order:
            if k not in have or k in active:
                continue
            new = have[k]
            old = self._active_idea_in_category(new["category"], tag)
            if not old:
                continue
            cur_rank = self._LAW_RANK.get(old["key"])
            new_rank = self._LAW_RANK.get(k)
            if cur_rank is not None and new_rank is not None and new_rank <= cur_rank:
                blocked.append({"id": k, "why": "would downgrade the current law %s"
                                                % old["key"]})
                continue
            need_ws, need_war = self._LAW_GATE.get(k, (0.50, False))
            if need_war and not at_war:
                blocked.append({"id": k, "why": "needs a war"})
                continue
            if ws < need_ws:
                blocked.append({"id": k, "why": "needs war_support >= %.2f (have %.2f)"
                                                   % (need_ws, ws)})
                continue
            out.append({"id": k, "ptr": new["ptr"], "cost_pp": 150,
                        "affordable": c.political_power >= 150,
                        "legal": True, "current": old["key"],
                        "method": "replace", "prefilter": True})
        return {"political_power": c.political_power, "ideas": out,
                "blocked": blocked, "war_support": ws, "at_war": at_war,
                "cached": False, "source": "python_prefilter"}

    def idea_definitions(self):
        """CIdeaDatabase definition pointers (the global idea/law/advisor definitions)."""
        if getattr(self, "_idea_defs", None) is not None:
            return self._idea_defs
        db = self.oi.singleton("CIdeaDatabase")
        out = {}
        if db:
            for x in _arr(self.p, db, 0x68):
                k = item_name(self.p, x + IDEA_NAME)
                if k:
                    out[k] = x
        self._idea_defs = out
        return out

    def add_idea(self, key, tag=None):
        """Apply an idea through the same command a politics-tab click uses.

        Laws and mutually-exclusive advisors are CReplaceIdeaCommand: the country
        already owns one idea in that category, so AddIdea would fail the category
        check. National spirits are CAddIdeaCommand. The branch is chosen from the
        country's own idea list, not guessed from the key.
        """
        c = self._c(tag)
        active = {a["key"] for a in self._idea_entries(tag, "active")}
        if key in active:
            return {"ok": True, "idea": key, "method": "already_active",
                    "run": True, "active": True,
                    "pp_before": c.political_power, "pp_after": c.political_power}
        new = self._find_idea(key, tag)
        if new and new["category"] and self._active_idea_in_category(new["category"], tag):
            return self.replace_idea(key, tag)

        # National spirits / first advisor in an empty category: CAddIdeaCommand.
        ptr = self.idea_definitions().get(key) or (new["ptr"] if new else None)
        if not ptr:
            # V2-04: advisors are not CIdeas at all — route them to CAddAdvisorCommand.
            try:
                hired = self.hire_advisor(key, tag)
            except Exception as e:
                hired = {"ok": False, "attempted": False, "error": repr(e)}
            if hired.get("attempted"):
                return hired
            raise KeyError("idea not found: %s" % key)
        before = c.political_power
        with self:
            cmd = self.cmd.make("AddIdea", tag=c.tag_id,
                                idea_ptr=ptr, flag=1)
            rv = self._run_cmd(cmd)
        self._settle()
        active2 = set(self.politics(c.tag)["active_ideas"])
        out = {"ok": key in active2, "idea": key, "method": "add",
               "run": bool(rv & 0xFF), "error": getattr(self, "_last_cmd_error", ""),
               "pp_before": before,
               "pp_after": c.political_power, "active": key in active2}
        if key not in active2:
            # 2026-11-22 (P1-3): run:true + error:"" + active:false is unreadable
            # on its own.  Say that the engine refused it and why we think so.
            out["reason"] = (self._idea_block_reason(key, c, tag)
                             or "the engine refused the idea (nothing changed; "
                                "national spirits cannot be added this way -- "
                                "advisors go through hoi4_hire_advisor)")
        return out

    def remove_idea(self, key, tag=None):
        p = self.p
        c = self._c(tag)
        pol = p.u64(c.ptr + CO_POLITICS)
        for i in _arr(p, pol, PL_ACTIVE_IDEAS):
            if item_name(p, i + IDEA_NAME) == key:
                with self:
                    self.cmd.send("RemoveIdea", tag=c.tag_id, idea_ptr=i, flag=1)
                self._settle()
                return {"ok": key not in self.politics(c.tag)["active_ideas"], "idea": key}
        raise KeyError("not an active idea: %s" % key)

    # ============================================== PE RTTI vtable scanning
    # `ObjectIndex.singleton()` / `.instances()` only see the classes the MCP
    # object index was built for; on the PE build that excludes almost every
    # game database (CEquipmentDatabase, CBuildingDatabase, CProjectPool, ...).
    # The MSVC RTTI map in research/windows/rtti_win.pkl knows the vtable RVA of
    # 15k classes, so a live object can always be found by scanning writable
    # memory for `base + vtable_rva`.  That is a pure read — no injection.
    _VT_RTTI = None
    _VT_SCAN = {}

    def _rtti_classes(self):
        if Game._VT_RTTI is None:
            import pickle
            root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            with open(os.path.join(root, "research", "windows", "rtti_win.pkl"), "rb") as _rf:
                Game._VT_RTTI = pickle.load(_rf)["classes"]
        return Game._VT_RTTI

    def _rtti_vt_rvas(self, class_name):
        """vtable RVAs for a class, tolerating namespace-mangled RTTI names.

        `rtti_win.pkl` keys are the raw MSVC type descriptors: a global class is
        ".?AVCFoo@@", but a namespaced one is ".?AVCFoo@NFoo@@".
        """
        cls = self._rtti_classes()
        out = list(cls.get(".?AV%s@@" % class_name) or [])
        if not out:
            pref = ".?AV%s@" % class_name
            for k, v in cls.items():
                if k.startswith(pref):
                    out.extend(v)
        return out

    def win_scan_class(self, class_name, use_cache=True):
        """Every live object whose vtable belongs to `class_name` (PE only).

        2026-11-23 (C-2 根因修复): 严禁将空扫描结果 `[]` 永久写入 `Game._VT_SCAN`。
        游戏重启或刚进主菜单时扫描可能暂时返回 0 条，旧代码缓存 `[]` 后导致
        `_win_vt_cache()` 永远返回 `[]`（is not None），科技库/模板库整会话死锁，
        只能重启 MCP 进程（mcp_load）才能救活。现在仅缓存非空命中；空结果只做 2s 防抖。
        """
        import time as _time
        p = self.p
        key = (p.pid, class_name)
        if use_cache:
            cached = Game._VT_SCAN.get(key)
            if cached:
                return cached
            empty_at = getattr(Game, "_VT_SCAN_EMPTY_AT", {}).get(key)
            if empty_at and (_time.monotonic() - empty_at) < 2.0:
                return []
        rvas = self._rtti_vt_rvas(class_name)
        if not rvas:
            return []
        from objindex import _scan_regions_for_vptrs
        want = [p.base + r for r in rvas]
        hits = _scan_regions_for_vptrs(
            p, p.regions(writable=True), want, max_hits=10_000_000, chunk_size=8 << 20
        )
        if hits:
            Game._VT_SCAN[key] = hits
            getattr(Game, "_VT_SCAN_EMPTY_AT", {}).pop(key, None)
        else:
            Game._VT_SCAN.pop(key, None)
            if not hasattr(Game, "_VT_SCAN_EMPTY_AT"):
                Game._VT_SCAN_EMPTY_AT = {}
            Game._VT_SCAN_EMPTY_AT[key] = _time.monotonic()
        return hits

    def win_arrays(self, obj, span=0x800, limit=400000):
        """CPdxArray containers inside `obj` (PE build).

        `probe.describe()` returns [] for every status object on the PE build —
        its element-class logic does not match the MSVC layouts — so this walks
        the data/cap/size/alloc quad manually and names element 0's class.
        """
        p = self.p
        out = []
        for off in range(0, span, 8):
            try:
                data = p.u64(obj + off)
            except Exception:
                break
            if not (0x10000 < data < 0x7FFFFFFFFFFF):
                continue
            try:
                cap = p.i32(obj + off + 8)
                size = p.i32(obj + off + 12)
            except Exception:
                continue
            if not (0 <= size <= cap <= limit) or size == 0:
                continue
            elem = None
            try:
                elem = self.oi.class_of(data)          # inline element objects
                if not elem:
                    e0 = p.u64(data)                    # array of pointers
                    if 0x10000 < e0 < 0x7FFFFFFFFFFF:
                        elem = self.oi.class_of(e0)
            except Exception:
                elem = None
            out.append({"offset": off, "size": size, "cap": cap, "elem": elem})
        return out

    def _win_equipment_types(self):
        """{equipment archetype id: script key} on the PE build.

        CEquipmentType carries the id at +0x08 and the key as an MSVC std::string
        at +0x18 (verified live: 456 objects, 15400 infantry_equipment,
        15401 artillery_equipment, 20000 anti_air_equipment).  CEquipmentVariant
        +0x18 holds exactly that id, so this one map is enough to name any
        production line.  Cached in research/windows/eqtypes_win.pkl.
        """
        import pickle
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        path = os.path.join(root, "research", "windows", "eqtypes_win.pkl")
        try:
            with open(path, "rb") as _ef:
                d = pickle.load(_ef)
            if len(d) > 100:
                return d
        except Exception:
            pass
        d = {}
        for t in self.win_scan_class("CEquipmentType"):
            nm = item_name(self.p, t + 0x18)
            if nm:
                d[self.p.i32(t + 0x08)] = nm
        if len(d) > 100:
            try:
                with open(path, "wb") as _ef:
                    pickle.dump(d, _ef)
            except Exception:
                pass
        return d

    # ============================================================= PRODUCTION
    def _load_equipment(self):
        if getattr(self, "_eq_by_id", None) is not None:
            return
        p = self.p
        # 2026-11-21 (P1-3): on the PE build the id->key map comes from a small
        # pickle.  Asking `oi.singleton("CEquipmentDatabase")` FIRST made the
        # ObjectIndex walk the whole address space (10-15 s) just to fail and
        # fall back to this map -- and every cold `hoi4_overview` paid it.
        if sys.platform == "win32":
            byid = self._win_equipment_types()
            if byid:
                self._eq_by_id = byid
                self._eq_by_name = {v: k for k, v in byid.items()}
                return
        db = self.oi.singleton("CEquipmentDatabase")
        if not db and sys.platform == "win32":
            byid = self._win_equipment_types()
            self._eq_by_id = byid
            self._eq_by_name = {v: k for k, v in byid.items()}
            return
        if not db:
            self._eq_by_id, self._eq_by_name = {}, {}
            return
        byid, byname = {}, {}
        # CEquipmentDatabase+0x68 is CPdxArray<CEquipmentArchetype*>: 457 entries in
        # 1.19.5, id at +0x08, name at +0x18. The old list (0x60/0x78/0xF0/0x120) was
        # the 1.19.2 layout and every entry landed 8 bytes off the real CPdxArray
        # header, so this map came out EMPTY and every production line reported
        # equipment: null while the variant pointer itself was correct all along.
        for off in (0x68,):
            for t in _arr(p, db, off):
                if not t:
                    continue
                nm = item_name(p, t + ET_NAME)
                if nm:
                    byid[p.i32(t + ET_ID)] = nm
                    byname[nm] = t
        self._eq_by_id, self._eq_by_name = byid, byname

    def equipment_types(self, filter_text=""):
        self._load_equipment()
        f = filter_text.lower()
        return sorted(n for n in self._eq_by_name if not f or f in n.lower())

    def _active_variant_names(self, tag=None):
        """纯内存读取本国已解锁可生产的装备型号名列表（< 1ms）。"""
        p = self.p
        self._load_equipment()
        ps = p.u64(self._c(tag).ptr + CO_PRODUCTION)
        out = []
        seen = set()
        for v in _arr(p, ps, PS_VARIANTS_ACTIVE):
            nm = self._eq_by_id.get(p.i32(v + EV_TYPE_ID))
            if nm and nm not in seen:
                seen.add(nm)
                out.append(nm)
        return out

    def production_lines(self, tag=None, include_obsolete=True):
        p = self.p
        self._load_equipment()
        ps = p.u64(self._c(tag).ptr + CO_PRODUCTION)
        out = []
        # 2026-11-21 (P1-3): name the line from its own vptr.  `self.oi.class_of`
        # forces the ObjectIndex to build (10-15 s of heap scanning) on its first
        # call -- that is what made a cold hoi4_overview blow the 50 s budget.
        # The two vtables cover every line; anything else still goes through RTTI.
        _vt_names = {}
        for _nm, _attr in (("CMilitaryProductionLine", "VT_MIL_LINE"),
                           ("CNavalProductionLine", "VT_NAV_LINE")):
            _rva = getattr(O, _attr, None)
            if _rva:
                _vt_names[p.base + _rva] = _nm
        for i, l in enumerate(_arr(p, ps, PS_LINES)):
            v = p.u64(l + ML_VARIANT)
            eid = p.i32(v + EV_TYPE_ID) if v else None
            cost = p.i64(l + ML_UNIT_COST) / O.FIXED
            icpd = p.i64(l + ML_OUTPUT) / O.FIXED
            _cls = _vt_names.get(p.u64(l))
            if _cls is None:
                _cls = self.oi.class_of(l)
            out.append({
                "index": i,
                "equipment": self._eq_by_id.get(eid),
                "factories": p.i32(l + ML_FACTORIES),
                # 2026-11-21 (续测 P2-8): three lines sit permanently at 0 factories
                # (18/19/20 in a 1936 GER save) and therefore read
                # output_per_day 0.0.  They are real, empty line objects the player
                # left behind -- not a reader bug -- so flag them instead of letting
                # a caller mistake them for dead/stuck production.
                "idle": p.i32(l + ML_FACTORIES) == 0,
                "unit_cost": round(cost, 3),
                # +0x38 is NOT a cost.  It is the IC already invested in the unit
                # currently on the line: every observed value is < unit_cost and
                # the four submarine lines (all unit_cost 440) read 42.31 /
                # 438.31 / 394.31 / 350.31, i.e. hulls at different completion
                # stages.  The old name `effective_cost` implied a modified price
                # and was simply wrong.
                "unit_progress_ic": round(p.i64(l + ML_EFF_COST) / O.FIXED, 3),
                # +0x30 is IC per day for the whole line, not units per day:
                # one dockyard line reads 2.935 = 2.5 base x 1.174, and
                # industrial_capacity_dockyard_factor is exactly +0.174.
                "output_per_day": round(icpd, 3),
                "output_units_per_day": (round(icpd / cost, 4) if cost > 0 else None),
                "max_efficiency": p.i32(l + ML_MAX_EFFICIENCY),
                "priority": p.i32(l + ML_PRIORITY),
                "class": _cls,
            })
        # 2026-11-22 (复测 5 §8.4 时刻 ②): 每条军工产线透出 variant_year / obsolete /
        # upgrade_to，过时产线附带 upgrade_gain / retool_cost / payback_days / suggested_action。
        if include_obsolete and out:
            try:
                import equipmeta as _em
                avail = []
                seen_v = set()
                for v in _arr(p, ps, PS_VARIANTS_ACTIVE):
                    nm = self._eq_by_id.get(p.i32(v + EV_TYPE_ID))
                    if nm and nm not in seen_v:
                        seen_v.add(nm)
                        avail.append(nm)
                analysis = _em.analyze_production_lines(out, avail)
                for row in out:
                    a = analysis.get(row["index"])
                    if not a:
                        continue
                    row["archetype"] = a.get("archetype")
                    row["variant_year"] = a.get("variant_year")
                    row["obsolete"] = bool(a.get("obsolete"))
                    row["upgrade_to"] = a.get("upgrade_to")
                    if a.get("obsolete"):
                        row["upgrade_to_year"] = a.get("upgrade_to_year")
                        row["upgrade_gain"] = a.get("upgrade_gain")
                        row["retool_cost"] = a.get("retool_cost")
                        row["payback_days"] = a.get("payback_days")
                        row["suggested_action"] = a.get("suggested_action")
            except Exception:
                pass
        return out

    def factory_allocation(self, tag=None):
        """引擎精确口径的军工 / 船坞分配账本（纯只读）。

        2026-11-23 (复测 6 P1-1): 依据 CProductionStatus::UpdateFactories (0xede8c0)
        与 GetFreeNavalDockyards (0xeda420) 反汇编结果，直接读取引擎实时维护的
        真实可用数（已计入核心+占领合规+傀儡上供+离岸工厂+受损预留+舰船改装线
        CShipRefitProductionLine [type 71]+修理船坞 CStrategicNavy+0x1A4），
        彻底消除过去用原始建筑总和减 type-57 产线造成的假空闲船坞与清不掉的 IDLE_DOCK 红点。
        """
        p = self.p
        c = self._c(tag)
        prod = 0
        lines = []
        try:
            prod = p.u64(c.ptr + CO_PRODUCTION)
            lines = _arr(p, prod, PS_MIL_LINES) if prod else []
        except Exception:
            prod, lines = 0, []

        mil_lines_used = 0
        nav_lines_used = 0
        refit_lines_used = 0
        mil_lines_req = 0
        nav_lines_req = 0
        refit_lines_req = 0
        for ln in lines:
            try:
                ht = self.handle_of(ln)["type"]
                used = max(0, int(p.i32(ln + ML_FACTORIES))) + max(0, int(p.i32(ln + 0x1C)))
                req = max(0, int(p.i32(ln + 0xE8))) if sys.platform == "win32" else used
            except Exception:
                continue
            if ht == 56:
                mil_lines_used += used
                mil_lines_req += req
            elif ht == 57:
                nav_lines_used += used
                nav_lines_req += req
            elif ht == 71:
                refit_lines_used += used
                refit_lines_req += req

        raw_mil = int(getattr(c, "military_factories", 0) or 0)
        raw_dock = int(getattr(c, "naval_dockyards", 0) or 0)
        raw_civ = int(getattr(c, "civilian_factories", 0) or 0)

        # Windows 1.19.5.0 权威引擎口径（0xede8c0 / 0xeda420）
        if sys.platform == "win32" and prod:
            try:
                mil_tot = int(p.i64(prod + 0x2D0) // 100000)
                mil_res = max(0, int(p.i32(prod + 0x308)))
                mil_used_eng = max(0, int(p.i32(prod + 0x2F0)))
                dock_tot = int(p.i64(prod + 0x330) // 100000)
                dock_res = max(0, int(p.i32(prod + 0x368)))
                dock_used_eng = max(0, int(p.i32(prod + 0x350)))

                # CGameState + 0x698 (CStrategicNavyManager) -> +0x08[tag_idx] -> +0x1A4 修理占用船坞
                # 2026-11-23 (复测 8 B-12 根因修复): self._tag2idx 是 list 而非 dict！
                # 旧代码写 self._tag2idx.get(...) 每次抛 AttributeError 被吞成 0，导致把 1936 德国
                # 3 座修理船坞误报为 3 座空闲船坞，触发不可解 IDLE_DOCK 假死锁。
                repair_dockyards = 0
                try:
                    gs = self.gamestate
                    snm = p.u64(gs + 0x698) if gs else 0
                    if snm:
                        arr_d = p.u64(snm + 0x08)
                        arr_n = int(p.i32(snm + 0x14))
                        self._load_tags()
                        t2i = getattr(self, "_tag2idx", None)
                        if isinstance(t2i, dict):
                            tidx = int(t2i.get(c.tag_id, c.tag_id))
                        elif isinstance(t2i, (list, tuple)) and 0 <= c.tag_id < len(t2i):
                            tidx = int(t2i[c.tag_id])
                        else:
                            tidx = int(c.tag_id)
                        if arr_d and 0 <= tidx < arr_n:
                            sn = p.u64(arr_d + tidx * 8)
                            if sn and 0x10000 < sn < 0x7FFFFFFFFFFF:
                                rd = int(p.i32(sn + 0x1A4))
                                if 0 <= rd <= 500:
                                    repair_dockyards = rd
                except Exception:
                    repair_dockyards = 0

                if (0 <= mil_tot <= 5000 and 0 <= dock_tot <= 5000
                        and mil_used_eng <= mil_tot + 64 and dock_used_eng <= dock_tot + 64):
                    mil_avail = max(0, mil_tot - mil_res)
                    mil_idle = max(0, mil_avail - mil_used_eng)
                    dock_avail = max(0, dock_tot - dock_res - repair_dockyards)
                    dock_idle = max(0, dock_avail - dock_used_eng)
                    return {
                        "source": "engine_production_status",
                        "civilian_factories": raw_civ,
                        "military_raw_buildings": raw_mil,
                        "military_available": mil_avail,
                        "military_used": mil_used_eng,
                        "military_requested": mil_lines_req,
                        "military_reserved": mil_res,
                        "military_idle": mil_idle,
                        "naval_raw_buildings": raw_dock,
                        "naval_total_effective": dock_tot,
                        "naval_available": dock_avail,
                        "naval_used": dock_used_eng,
                        "naval_build_used": nav_lines_used,
                        "naval_refit_used": refit_lines_used,
                        "naval_repair_dockyards": repair_dockyards,
                        "naval_reserved": dock_res,
                        "naval_requested": nav_lines_req + refit_lines_req,
                        "naval_idle": dock_idle,
                    }
            except Exception:
                pass

        # 非 Windows 或读取异常时优雅降级（同时计入 type 57 造船线 + type 71 改装线）
        nav_used_sum = nav_lines_used + refit_lines_used
        return {
            "source": "lines_sum_fallback",
            "civilian_factories": raw_civ,
            "military_raw_buildings": raw_mil,
            "military_available": raw_mil,
            "military_used": mil_lines_used,
            "military_requested": mil_lines_req,
            "military_reserved": 0,
            "military_idle": max(0, raw_mil - mil_lines_used),
            "naval_raw_buildings": raw_dock,
            "naval_total_effective": raw_dock,
            "naval_available": raw_dock,
            "naval_used": nav_used_sum,
            "naval_build_used": nav_lines_used,
            "naval_refit_used": refit_lines_used,
            "naval_repair_dockyards": 0,
            "naval_reserved": 0,
            "naval_requested": nav_lines_req + refit_lines_req,
            "naval_idle": max(0, raw_dock - nav_used_sum),
        }

    def set_production_amount(self, line_index: int, amount: int, tag=None, auto_reclaim: bool = True):
        """Set how many FACTORIES a production line gets (the +/- buttons in the UI).

        NOTE. This used to silently do nothing: there is no command called
        "SetProductionLineAmount", and the result was never checked before returning
        {"ok": True}. The right command is `CAddProductionLineFactoriesCommand`:
        arg0 is the line's HANDLE (type 56) and arg1 is a DELTA, not an absolute
        count. The delta is computed here, and afterwards the line's factory field
        (CMilitaryProductionLine+0x18) is read back to report whether it really
        changed.

        2026-11-23 (复测 7 B-10): 当陆/空军工产线（type 56）需要加厂但空闲军工不足，且
        `auto_reclaim=True`（默认）时，自动从库存严重超储（`equipmeta.compute_surplus_lines`）
        的产线安全抽调缺口厂数（每条源线至少保留 1 厂），再补给目标线，返回 `reclaimed_from`。
        """
        p = self.p
        c = self._c(tag)
        prod = p.u64(c.ptr + CO_PRODUCTION)
        lines = _arr(p, prod, PS_MIL_LINES)
        if not (0 <= line_index < len(lines)):
            return {"ok": False, "error": "no such line: %d (of %d)" % (line_index, len(lines))}
        line = lines[line_index]
        before = p.i32(line + ML_FACTORIES)
        # 2026-11-23 (复测 8 B-11/B-12): 反汇编证实 CAddProductionLineFactoriesCommand 修改的是
        # line+0xE8 (requested_factories)，再由 UpdateFactories 把有效空闲工厂分配到 line+0x18
        # (assigned_factories)。同步读取 requested_before 以便区分“命令未被消费（暂停）”与
        # “命令已消费但引擎无空闲工厂（如船坞全被修理占满）”，并在缩减时一并清掉灰格请求。
        req_before = before
        has_req_field = False
        if sys.platform == "win32":
            try:
                _rb = int(p.i32(line + 0xE8))
                if 0 <= _rb <= 200 and (_rb >= before or before == 0):
                    req_before = _rb
                    has_req_field = True
            except Exception:
                pass
        delta = int(amount) - before
        req_delta = int(amount) - req_before
        if delta == 0 and req_delta == 0:
            return {"ok": True, "line": line_index, "factories": before,
                    "requested_factories": req_before, "note": "already set"}
        h = self.handle_of(line)
        # 56 = 陆/空军工产线，57 = 海军造船线，71 = 舰船改装线（CShipRefitProductionLine）
        if h["type"] not in (56, 57, 71):
            return {"ok": False, "error": "expected a production line handle (56/57/71), got %d" % h["type"]}

        is_naval = h["type"] in (57, 71)
        fac_kind = "dockyards" if is_naval else "military factories"

        reclaimed_from = []
        if auto_reclaim and delta > 0 and h["type"] == 56:
            try:
                alloc0 = self.factory_allocation(c.tag) if hasattr(self, "factory_allocation") else {}
                idle0 = int(alloc0.get("military_idle")) if isinstance(alloc0, dict) and alloc0.get("military_idle") is not None else None
            except Exception:
                idle0 = None
            if idle0 is not None and idle0 < delta:
                need_reclaim = delta - max(0, idle0)
                try:
                    import equipmeta as _em_rc
                    pls_now = self.production_lines(c.tag, include_obsolete=False) or []
                    st_tot = (self.equipment_stockpile(c.tag) or {}).get("totals_by_equipment") or {}
                    divs_n = int((self.divisions(c.tag, limit=1) or {}).get("count") or 0)
                    tot_mil = int(alloc0.get("military_available") or sum(
                        int(x.get("factories") or 0) for x in pls_now
                        if x.get("class") == "CMilitaryProductionLine"
                    ))
                    surplus = _em_rc.compute_surplus_lines(pls_now, st_tot, tot_mil, divs_n) or []
                    for sr in surplus:
                        if need_reclaim <= 0:
                            break
                        s_idx = int(sr.get("line", -1))
                        if s_idx == line_index or s_idx < 0:
                            continue
                        cur_f = int(sr.get("factories") or 0)
                        sugg_f = max(1, int(sr.get("suggested_factories") or 1))
                        avail_take = max(0, cur_f - sugg_f)
                        if avail_take <= 0:
                            continue
                        take = min(need_reclaim, avail_take)
                        rr = self.set_production_amount(
                            s_idx, cur_f - take, tag=tag, auto_reclaim=False
                        )
                        actual_taken = max(0, cur_f - int(rr.get("after", cur_f)))
                        if actual_taken > 0:
                            reclaimed_from.append({
                                "line": s_idx,
                                "equipment": sr.get("equipment"),
                                "before": cur_f,
                                "after": int(rr.get("after", cur_f - actual_taken)),
                                "reclaimed": actual_taken,
                                "surplus_ratio": sr.get("surplus_ratio"),
                            })
                            need_reclaim -= actual_taken
                except Exception:
                    pass

        # 发令前先检查真实空闲工厂：若需要加厂但空闲明确为 0，绝不盲目往 +0xE8 叠灰格，
        # 也不误导成“游戏暂停”。
        pre_alloc = {}
        pre_idle = None
        try:
            pre_alloc = self.factory_allocation(c.tag)
            if isinstance(pre_alloc, dict) and pre_alloc.get("source") == "engine_production_status":
                pre_idle = int(pre_alloc["naval_idle"] if is_naval else pre_alloc["military_idle"])
        except Exception:
            pre_alloc, pre_idle = {}, None

        steps = []
        tray = []
        hit_line_cap = False
        if not (delta > 0 and pre_idle == 0):
            cur_assigned = before
            cur_req = req_before
            max_step = 5
            for _ in range(32):
                # 缩减或清理灰格时以 requested 为准；增加时以 assigned 为准（受 requested 同步推进）
                ref = cur_req if (has_req_field and int(amount) <= cur_req) else cur_assigned
                if cur_assigned == int(amount) and (not has_req_field or cur_req == int(amount)):
                    break
                step = int(amount) - ref
                if step == 0:
                    break
                step = max(-max_step, min(max_step, step))
                self.send_command("AddProductionLineFactories",
                                  {"line_id": (h["id"] << 32) | (h["type"] & 0xFFFFFFFF),
                                   "amount": step if step >= 0 else (step & 0xFFFFFFFF)},
                                  tag)
                self._settle(0.18)
                nxt_assigned = p.i32(line + ML_FACTORIES)
                nxt_req = p.i32(line + 0xE8) if has_req_field else nxt_assigned
                steps.append(step)
                tray.append(nxt_assigned)
                if nxt_assigned == cur_assigned and (not has_req_field or nxt_req == cur_req):
                    # 2026-11-23 (复测 10 B-2 PE 反汇编实锤):
                    # CAddProductionLineFactoriesCommand::CanExecute (0x11EDCC0) 严格校验
                    # `requested + step <= line->GetMaxFactories()`（驱逐舰/潜艇 10、主力舰 5、
                    # 运输船 15、陆军线 150）。若 cur=9 试图 step=+3 -> 12 > 10，CanExecute 直接
                    # 拒绝整条命令！此时自动将步长缩为 1 再试，既能精准打满单线上限（9 -> 10），
                    # 又能准确区分“撞单舰船坞上限”与“游戏暂停命令队列未消费”。
                    if abs(step) > 1:
                        max_step = 1
                        continue
                    if step > 0 and is_naval:
                        hit_line_cap = True
                    break
                if step > 0 and nxt_assigned == cur_assigned:
                    break          # requested 已涨但实际分配没涨 -> 已无可用空闲工厂，立即停手防叠灰格
                cur_assigned = nxt_assigned
                cur_req = nxt_req

        after = p.i32(line + ML_FACTORIES)
        req_after = p.i32(line + 0xE8) if has_req_field else after
        paused = None
        try:
            import session as _s
            paused = bool(_s.is_paused(self))
        except Exception:
            pass
        alloc = {}
        try:
            alloc = self.factory_allocation(c.tag)
            idle_fac = int(alloc["naval_idle"] if is_naval else alloc["military_idle"])
        except Exception:
            alloc = pre_alloc
            idle_fac = pre_idle
        note = ""
        if after != int(amount):
            extra_dock = ""
            if is_naval and alloc:
                extra_dock = (
                    " (effective_dockyards=%s, repair=%s, available_for_build=%s, "
                    "build_used=%s, refit_used=%s, idle=%s, raw_buildings=%s)"
                    % (alloc.get("naval_total_effective"), alloc.get("naval_repair_dockyards"),
                       alloc.get("naval_available"), alloc.get("naval_build_used"),
                       alloc.get("naval_refit_used"), alloc.get("naval_idle"),
                       alloc.get("naval_raw_buildings"))
                )
            cmd_consumed = (after != before) or (has_req_field and req_after != req_before)
            if hit_line_cap and delta > 0 and is_naval:
                note = (
                    "line %d hit the per-ship dockyard cap at %d (requested %d; CAddProductionLineFactoriesCommand::CanExecute "
                    "refuses new_req > GetMaxFactories() — capital ships cap at 5, screens/subs at 10, convoys at 15). "
                    "Put the remaining idle dockyards (%s) on another naval line or open a new naval line%s"
                    % (line_index, after, int(amount), idle_fac, extra_dock)
                )
            elif delta > 0 and (idle_fac == 0 or (after > before)):
                note = ("only reached %d of %d — not enough spare %s "
                        "(idle=%s%s; the engine only hands out effective free factories)"
                        % (after, int(amount), fac_kind, idle_fac, extra_dock))
            elif paused and (idle_fac is None or idle_fac > 0) and not cmd_consumed:
                note = ("the game is PAUSED and the command queue was not consumed "
                        "(idle=%s%s); unpause briefly or retry" % (idle_fac, extra_dock))
            else:
                note = ("only reached %d of %d — not enough spare %s%s "
                        "(the engine only hands out effective free factories, or line hit per-ship dockyard cap)"
                        % (after, int(amount), fac_kind, extra_dock))
        out = {"ok": after == int(amount), "line": line_index,
               "before": before, "after": after, "requested": int(amount),
               "idle_factories": idle_fac,
               "hit_line_cap": bool(hit_line_cap),
               "steps": steps, "readback": tray, "paused": paused, "note": note}
        if has_req_field:
            out["requested_before"] = req_before
            out["requested_after"] = req_after
        if reclaimed_from:
            out["reclaimed_from"] = reclaimed_from
            out["reclaimed_total"] = sum(int(x["reclaimed"]) for x in reclaimed_from)
        if is_naval and alloc:
            out["dockyard_breakdown"] = {
                "effective": alloc.get("naval_total_effective"),
                "used_total": alloc.get("naval_used"),
                "build_used": alloc.get("naval_build_used"),
                "refit_used": alloc.get("naval_refit_used"),
                "repair_dockyards": alloc.get("naval_repair_dockyards"),
                "idle": alloc.get("naval_idle"),
            }
        # 2026-11-22 (复测 5 §8.4 时刻 ②): 模型在调整工厂数那一刻，若该线正在生产
        # 过时型号，直接在返回体中附带 obsolete / upgrade_to / upgrade_gain / suggested_action。
        try:
            _pls = self.production_lines(c.tag, include_obsolete=True)
            if 0 <= line_index < len(_pls):
                _row = _pls[line_index]
                out["equipment"] = _row.get("equipment")
                if _row.get("obsolete"):
                    out["obsolete"] = True
                    out["upgrade_to"] = _row.get("upgrade_to")
                    out["upgrade_gain"] = _row.get("upgrade_gain")
                    out["retool_cost"] = _row.get("retool_cost")
                    out["payback_days"] = _row.get("payback_days")
                    out["suggested_action"] = _row.get("suggested_action")
        except Exception:
            pass
        return out

    def _variant_for(self, equipment, tag=None):
        """(variant_ptr, handle) of an ACTIVE equipment variant we can produce."""
        p = self.p
        self._load_equipment()
        prod = p.u64(self._c(tag).ptr + CO_PRODUCTION)
        for v in _arr(p, prod, PS_VARIANTS_ACTIVE):
            if self._eq_by_id.get(p.i32(v + EV_TYPE_ID)) == equipment:
                return v, self.handle_of(v)
        return None, None

    def set_production_line_equipment(self, line_index: int, equipment: str, tag=None,
                                      dry_run=False):
        """SWAP an existing production line to another equipment variant (CA换装).

        2026-11-22 (复测 5 P2 §8.2): 修复两大缺陷：
          1) 旧实现在 Windows 上仍调用 1.19.2 Linux 的 `send_command("SetProductionLine",
             {"0x24": ..., "0x2C": ...})`，被 WIN_SAFE 静默拒绝导致永远换装失败；
             现通过 `CSetProductionLineCommand` 真实 PE 构造器（vt 0x2A0FF88,
             ctor 0x11D1550, size 0x38, +0x28 line handle, +0x30 variant handle）构建。
          2) `dry_run=True` 以前不调用 `CanExecute`，会发假绿灯；现在无论 dry_run
             真假都调用引擎自身的 `CSetProductionLineCommand::CanExecute (0x11F2C30)`，
             并把 `can_execute` 如实透出。
        """
        p = self.p
        c = self._c(tag)
        self._load_equipment()
        prod = p.u64(c.ptr + CO_PRODUCTION)
        lines = _arr(p, prod, PS_MIL_LINES)
        if not (0 <= line_index < len(lines)):
            return {"ok": False, "error": "no such line: %d (of %d)" % (line_index, len(lines))}
        line = lines[line_index]
        before_name = self._eq_by_id.get(p.i32(p.u64(line + ML_VARIANT) + EV_TYPE_ID))
        var, vh = self._variant_for(equipment, tag)
        if var is None:
            avail = sorted({self._eq_by_id.get(p.i32(v + EV_TYPE_ID))
                            for v in _arr(p, prod, PS_VARIANTS_ACTIVE)} - {None})
            return {"ok": False, "error": "no active variant for %s" % equipment,
                    "available": avail[:40]}
        lh = self.handle_of(line)
        if lh["type"] != 56:
            return {"ok": False, "error": "expected military line handle 56, got %d" % lh["type"]}
        if vh["type"] != 70:
            return {"ok": False, "error": "expected equipment variant handle 70, got %d" % vh["type"]}
        packed_line = (lh["id"] << 32) | (lh["type"] & 0xFFFFFFFF)
        packed_var = (vh["id"] << 32) | (vh["type"] & 0xFFFFFFFF)
        out = {"line": line_index, "before": before_name, "requested": equipment,
               "line_handle": [lh["type"], lh["id"]],
               "variant_handle": [vh["type"], vh["id"]],
               "dry_run": bool(dry_run)}
        if before_name == equipment:
            out.update(ok=True, can_execute=True, after=before_name, note="already producing %s" % equipment)
            return out

        if sys.platform == "win32":
            import cmdwin
            cmd = None
            try:
                cmd = cmdwin.build_command(
                    self, "CSetProductionLineCommand",
                    {"line": packed_line, "variant": packed_var},
                )
                out["cmd"] = hex(cmd)
                can = bool(self.call(
                    p.base + cmdwin.CANEXEC_SET_PROD_LINE, (cmd, 0),
                    absolute=True, force=True,
                ) & 0xFF)
                out["can_execute"] = can
                if not can:
                    try:
                        allowed = bool(self.call(
                            p.base + self.PE_ADD_LINE_VARIANT_OK,
                            (var, cmd + 0x28), absolute=True, force=True,
                        ) & 0xFF)
                        out["variant_allowed"] = allowed
                    except Exception:
                        pass
                    try:
                        cmdwin.free(self, cmd)
                    except Exception:
                        pass
                    out.update(
                        ok=False,
                        after=before_name,
                        blocked_by="can_execute_refused",
                        error="CSetProductionLineCommand::CanExecute refused switching line %d (%s -> %s)"
                              % (line_index, before_name, equipment),
                    )
                    return out
                if dry_run:
                    try:
                        cmdwin.free(self, cmd)
                    except Exception:
                        pass
                    out.update(ok=True, after=before_name)
                    return out
                posted = cmdwin.post_drained(self, cmd, seconds=2.0)
                out["posted"] = posted
            except Exception as exc:
                if cmd:
                    try:
                        cmdwin.free(self, cmd)
                    except Exception:
                        pass
                out.update(ok=False, after=before_name, error="%s: %s" % (type(exc).__name__, exc))
                return out

            self._settle(0.25)
            after_name = self._eq_by_id.get(p.i32(p.u64(line + ML_VARIANT) + EV_TYPE_ID))
            out["after"] = after_name
            out["ok"] = (after_name == equipment)
            if out["ok"]:
                try:
                    import equipmeta as _em
                    _em.clear_obsolete_seen(pid=getattr(p, "pid", None), tag=c.tag, line_index=line_index)
                except Exception:
                    pass
            else:
                out["error"] = "CSetProductionLineCommand posted but line variant remained %s" % after_name
            return out

        if dry_run:
            out.update(ok=True, after=before_name)
            return out
        self.send_command("SetProductionLine",
                          {"0x24": packed_line, "0x2C": packed_var}, tag)
        self._settle(0.4)
        after_name = self._eq_by_id.get(p.i32(p.u64(line + ML_VARIANT) + EV_TYPE_ID))
        out["after"] = after_name
        out["ok"] = after_name == equipment
        if not out["ok"]:
            out["error"] = "the game did not switch the line (CanExecute refused or the handle was wrong)"
        return out

    # CAddProductionLineCommand (PE 1.19.5.0): vt 0x2A0FBC8, size 0x40,
    # ctor 0x11CD270, CanExecute 0x11EDB10, Execute 0x11DC130.
    PE_ADD_LINE_VT = 0x2A0FBC8
    PE_ADD_LINE_CANEXE = 0x11EDB10
    PE_ADD_LINE_VARIANT_OK = 0x140C2CAD0   # (CEquipmentVariant*, int *tag) -> bool

    def create_production_line(self, equipment: str, tag=None, dry_run=False):
        """Create a brand-new production line for an equipment variant.

        This used to be a coin flip: the command's two handle slots were never
        identified, so the old code posted `(variant, 0)` and `(0, variant)` and
        hoped.  Neither is what the engine wants.  The real ctor is

            0x11CD270(this, int *countryTag, {u64} *variant, {u64} *mio, bool front)

        with the country tag at +0x28, the equipment variant handle at +0x2C and
        an optional military-industrial-organisation handle at +0x34.  Verified
        by disassembling CanExecute (which prints "Invalid Target" when +0x28 is
        <= 0, then resolves +0x2C and asks the variant whether the country may
        build it) and Execute (which reads +0x28, takes [CCountry+0xF68] -- the
        very CProductionStatus this module already reads -- and calls
        0x140ED0FF0(prod, variant) to find-or-create the line, then
        0x140ED6100(prod, line, 0) to move it to the top when +0x3C is set).
        """
        import cmdwin
        p = self.p
        c = self._c(tag)
        self._load_equipment()
        prod = p.u64(c.ptr + CO_PRODUCTION)
        before_ptrs = _arr(p, prod, PS_MIL_LINES)
        before = len(before_ptrs)
        var, vh = self._variant_for(equipment, tag)
        if var is None:
            avail = sorted({self._eq_by_id.get(p.i32(v + EV_TYPE_ID))
                            for v in _arr(p, prod, PS_VARIANTS_ACTIVE)} - {None})
            return {"ok": False, "error": "no active variant for %s" % equipment,
                    "available": avail[:40]}
        if vh["type"] != 70:
            return {"ok": False,
                    "error": "expected equipment variant handle 70, got %d" % vh["type"]}
        # Is a line for this equipment already there?  Execute's find-or-create
        # would just hand back that same line, which reads as "nothing happened".
        for i, ln in enumerate(before_ptrs):
            if p.u64(ln + ML_VARIANT) == var:
                return {"ok": False, "already_exists": True, "line_index": i,
                        "before": before, "equipment": equipment,
                        "error": "a production line for %s already exists (line %d); "
                                 "change its factory count instead"
                                 % (equipment, i)}
        packed = (vh["id"] << 32) | (vh["type"] & 0xFFFFFFFF)
        out = {"before": before, "requested": equipment, "dry_run": bool(dry_run),
               "variant_handle": [vh["type"], vh["id"]], "tag_id": int(c.tag_id)}
        if dry_run:
            out.update(ok=True)
            return out
        cmd = cmdwin.build_command(self, "CAddProductionLineCommand",
                                   {"tag": int(c.tag_id),
                                    "variant_handle": packed,
                                    "mio_handle": 0,
                                    "move_to_front": 1})
        out["cmd"] = hex(cmd)
        # The "move to the top" bool is the one ctor argument that travels on the
        # stack.  Read it back instead of trusting the call: if a future ARGSPEC
        # drift drops it again, the line would just quietly land at the bottom.
        out["flag_0x3c"] = p.u8(cmd + 0x3C)
        if out["flag_0x3c"] != 1:
            p.write(cmd + 0x3C, b"\x01")
            out["flag_forced"] = True
            out["flag_0x3c"] = p.u8(cmd + 0x3C)
        can = self.call(p.base + self.PE_ADD_LINE_CANEXE, (cmd,),
                        absolute=True, force=True) & 0xFF
        out["can_execute"] = bool(can)
        if not can:
            # Narrow it down without guessing: the same predicate CanExecute uses.
            allowed = self.call(p.base + self.PE_ADD_LINE_VARIANT_OK,
                                (var, cmd + 0x28), absolute=True, force=True) & 0xFF
            out["variant_allowed_for_country"] = bool(allowed)
            out["error"] = ("CAddProductionLineCommand::CanExecute refused"
                            + ("" if allowed else
                               " -- %s is not producible by this country "
                               "(not researched / embargoed)" % equipment))
            return out
        posted = cmdwin.post(self, cmd)
        self._settle(0.4)
        after_lines = _arr(p, p.u64(self._c(tag).ptr + CO_PRODUCTION), PS_MIL_LINES)
        new = [x for x in after_lines if x not in before_ptrs]
        out["posted"] = posted
        out["after"] = len(after_lines)
        if not new:
            out["ok"] = False
            out["error"] = ("the command was accepted but no new line appeared -- "
                            "execute did not create one")
            return out
        line = new[0]
        name = self._eq_by_id.get(p.i32(p.u64(line + ML_VARIANT) + EV_TYPE_ID))
        out.update(ok=name == equipment, equipment=name,
                   line_index=after_lines.index(line),
                   factories=p.i32(line + ML_FACTORIES),
                   units=p.i32(line + ML_FACTORIES))
        if not out["ok"]:
            out["error"] = ("a line appeared but it is for %s, not %s"
                            % (name, equipment))
        else:
            # 2026-11-22 (复测 5 §8.4 时刻 ②): 若新开的装备本身已有更新的已解锁型号，立刻提醒
            try:
                _pls = self.production_lines(c.tag, include_obsolete=True)
                _li = out.get("line_index")
                if isinstance(_li, int) and 0 <= _li < len(_pls) and _pls[_li].get("obsolete"):
                    _row = _pls[_li]
                    out["obsolete"] = True
                    out["upgrade_to"] = _row.get("upgrade_to")
                    out["upgrade_gain"] = _row.get("upgrade_gain")
                    out["retool_cost"] = _row.get("retool_cost")
                    out["payback_days"] = _row.get("payback_days")
                    out["suggested_action"] = _row.get("suggested_action")
            except Exception:
                pass
        return out

    def remove_production_line(self, line_index: int, tag=None):
        """Remove a production line entirely.

        CRemoveProductionLineCommand (PE 1.19.5.0): vt 0x2A10048, size 0x30,
        ctor 0x11D0210 takes ONE argument -- an object carrying the line handle
        at +8 -- and stores it at +0x28.  The old call passed a country tag at
        +0x24 and an index at +0x2C (a stale 1.19.2 shape), and the command was
        never on the windows whitelist, so it was refused before it was even
        built and this function reported a bare `ok: false`.
        """
        p = self.p
        c = self._c(tag)
        self._load_equipment()
        prod = p.u64(c.ptr + CO_PRODUCTION)
        lines = _arr(p, prod, PS_MIL_LINES)
        if not (0 <= line_index < len(lines)):
            return {"ok": False, "error": "no such line: %d (of %d)"
                    % (line_index, len(lines))}
        line = lines[line_index]
        h = self.handle_of(line)
        # 56 = land/air production line, 57 = NAVAL production line. Both are valid.
        if h["type"] not in (56, 57):
            return {"ok": False, "error": "expected a production line handle (56/57), got %d"
                    % h["type"]}
        name = self._eq_by_id.get(p.i32(p.u64(line + ML_VARIANT) + EV_TYPE_ID))
        before = len(lines)
        packed = (h["id"] << 32) | (h["type"] & 0xFFFFFFFF)
        res = self.send_command("CRemoveProductionLineCommand",
                                {"line_handle": packed}, tag)
        self._settle(0.4)
        after = len(_arr(p, p.u64(self._c(tag).ptr + CO_PRODUCTION), PS_MIL_LINES))
        out = {"ok": after < before, "before": before, "after": after,
               "equipment": name, "line_handle": [h["type"], h["id"]],
               "run_result": (res or {}).get("run_result")}
        if not out["ok"]:
            out["error"] = (res or {}).get("error") or \
                "the game accepted the command but the line is still there"
        return out

    # =========================================================== GRAND DOCTRINES
    # 1.19 stores grand doctrines outside the CCommand catalog as
    # NDoctrines::CGrandDoctrineTemplate objects (vtable 0x447EFA0), and the
    # database is lazily created, so ObjectIndex (writable-only) sees none of
    # them. Scan every readable region for the vtable once per process.
    DOCTRINE_LOC = {
        "new_mobile_warfare": "GRAND_DOCTRINE_MOBILE_WARFARE",
        "mobile_warfare": "GRAND_DOCTRINE_MOBILE_WARFARE",
        "superior_firepower": "GRAND_DOCTRINE_SUPERIOR_FIREPOWER",
        "grand_battleplan": "GRAND_DOCTRINE_GRAND_BATTLEPLAN",
        "mass_assault": "GRAND_DOCTRINE_MASS_ASSAULT",
        "fleet_in_being": "GRAND_DOCTRINE_FLEET_IN_BEING",
        "convoy_raiding": "GRAND_DOCTRINE_CONVOY_RAIDING",
        "base_strike": "GRAND_DOCTRINE_BASE_STRIKE",
        "battlefield_support": "GRAND_DOCTRINE_BATTLEFIELD_SUPPORT",
        "operational_integrity": "GRAND_DOCTRINE_OPERATIONAL_INTEGRITY",
        "strategic_destruction": "GRAND_DOCTRINE_STRATEGIC_DESTRUCTION",
        "special_forces_quantity": "GRAND_DOCTRINE_SPECIAL_FORCES_QUANTITY",
        "special_forces_quality": "GRAND_DOCTRINE_SPECIAL_FORCES_QUALITY",
    }

    def _doctrine_templates(self):
        # 2026-11-15: these 12 objects are STATIC for the whole process lifetime
        # (game-data templates, loaded once at boot). The original cache key was
        # (pid, base) but callers also cleared _doc_cache_key on module reload,
        # which forced oi.instances() -> `_sanitised()` -> a full multi-GB heap
        # rescan on a stale/expired objindex cache. That rescan is what timed out
        # hoi4_unlock_doctrine (x3). Keep the first non-empty result forever for
        # this pid/base; only a genuinely empty result may be retried.
        key = (self.p.pid, self.p.base)
        cached = getattr(self, "_doc_cache", None)
        if getattr(self, "_doc_cache_key", None) == key and cached:
            return cached
        found = {}
        if sys.platform == "win32":
            # The Linux scan below looks for the vtable 0x447EFA0, which is a Linux
            # VA; on the PE build it matches nothing and every doctrine lookup failed
            # ("template not found").  There are exactly 12 CGrandDoctrineTemplate
            # objects and their script key is a std::string at +0x28
            # ("GRAND_DOCTRINE_MOBILE_WARFARE" ...), which is also what DOCTRINE_LOC
            # holds -- so the object index answers it directly.
            # 2026-11-23 (C-6 学说冷启动修复): 冷启动/换档瞬间 oi.instances() 可能返回空；
            # 若为空立刻回退单类虚表直扫 win_scan_class("CGrandDoctrineTemplate", use_cache=False)，
            # 且严禁把空 dict `{}` 写入 _doc_cache 锁死后续调用！
            objs = []
            try:
                objs = list(self.oi.instances("CGrandDoctrineTemplate") or [])
            except Exception:
                objs = []
            if not objs:
                try:
                    objs = list(self.win_scan_class("CGrandDoctrineTemplate", use_cache=False) or [])
                except Exception:
                    objs = []
            for obj in objs:
                try:
                    nm = self.p.stdstring(obj + 0x28)
                except Exception:
                    nm = None
                if nm:
                    found[nm] = obj
            if found:
                self._doc_cache_key = key
                self._doc_cache = found
            return found
        target = self.p.va(0x447EFA0)
        for m in self.p.regions(writable=False):
            a = m["start"]
            while a < m["end"]:
                n = min(4 << 20, m["end"] - a)
                n -= n % 8
                if n <= 0:
                    break
                buf = self.p.try_read(a, n)
                if buf:
                    for i in range(0, len(buf) - 7, 8):
                        if struct.unpack_from("<Q", buf, i)[0] == target:
                            obj = a + i
                            nm = item_name(self.p, obj + 0x20)
                            if nm:
                                found[nm] = obj
                a += n
            if len(found) >= 12:
                break
        self._doc_cache_key = key
        self._doc_cache = found
        return found

    def unlock_doctrine(self, doctrine, tag=None):
        """Unlock a grand doctrine (mobile_warfare / superior_firepower / ...).

        The command is NDoctrines::CUnlockGrandDoctrineCommand (vtable 0x4360C28,
        +0x28 = CGrandDoctrineTemplate*, +0x30 = 0). Success is verified by
        finding the template pointer in the country's doctrine store at
        CCountry+0x15E0, not by trusting the command's return value.
        """
        loc = self.DOCTRINE_LOC.get(str(doctrine or "").lower())
        if not loc:
            return {"ok": False, "error": "unknown doctrine: %s" % doctrine,
                    "available": sorted(self.DOCTRINE_LOC)}
        templates = self._doctrine_templates()
        tpl = templates.get(loc)
        if not tpl:
            return {"ok": False, "error": "template not found: %s" % loc,
                    "templates": sorted(templates)}
        if sys.platform == "win32":
            # 2026-11-15 (crash #3): send_command() built CUnlockGrandDoctrineCommand
            # from an unverified pecmd layout with only +0x28 set. CanExecute
            # (0x1B10310) rejects +0x30 <= 0 ("#~ invalid country tag") and Execute
            # dereferences a null country, which killed hoi4.exe. The PE layout is
            # now disassembled (see cmdwin.make_unlock_doctrine): +0x28 = doctrine
            # template ptr, +0x30 = country tag id, vtable 0x2AAA8A0, size 0x38.
            import cmdwin
            tag_id = int(self._c(tag).tag_id)
            can0 = cmdwin.can_unlock_doctrine(self, tpl, tag_id)
            if can0 is False:
                return {"ok": False, "doctrine": doctrine, "localization": loc,
                        "template": hex(tpl), "can_execute": 0,
                        "error": ("the game's own CUnlockGrandDoctrineCommand::"
                                  "CanExecute refuses %s: its prerequisite is not met, "
                                  "a land doctrine is already assigned, or there is "
                                  "not enough army XP. Try grand_battleplan / "
                                  "mass_assault / superior_firepower." % loc)}
            cmd = cmdwin.make_unlock_doctrine(self, tpl, tag_id)
            # A paused game never drains the queue (§29.8) -- run the clock
            # briefly and restore the pause state before reading anything back.
            res = cmdwin.post_drained(self, cmd)
            res.update(cmd=hex(cmd), verified=True)
            rv = int(res.get("posted") or 0)
            can1 = cmdwin.can_unlock_doctrine(self, tpl, tag_id)
            # CanExecute 1 -> 0 proves the doctrine was assigned (mutual
            # exclusivity now blocks re-assigning it). CCountry+0x15E0 is a
            # stale Linux offset and always reads empty on PE, so it is dropped.
            applied = bool(can0 is True and can1 is False)
            accepted = bool(rv & 0xFF)
            return {"ok": applied or accepted, "doctrine": doctrine,
                    "localization": loc, "template": hex(tpl),
                    "can_execute_before": can0, "can_execute_after": can1,
                    "assigned": applied, "accepted": accepted, "command": res,
                    "note": (None if applied else
                             "command posted but CanExecute did not flip; the "
                             "doctrine may already be assigned or a popup held "
                             "the clock (drained=%s)" % res.get("drained"))}
        res = self.send_command("UnlockGrandDoctrine", {"0x28": tpl}, tag)
        self._settle(0.6)
        stored = False
        try:
            sub = self.p.u64(self._c(tag).ptr + 0x15E0)
            if sub:
                data = self.p.read(sub, 0x2000)
                stored = any(struct.unpack_from("<Q", data, i)[0] == tpl
                             for i in range(0, len(data) - 7, 8))
        except Exception:
            pass
        accepted = bool((res or {}).get("run_result") or 0)
        return {"ok": bool(stored) or accepted, "doctrine": doctrine,
                "localization": loc, "template": hex(tpl), "stored": stored,
                "accepted": accepted, "command": res}

    # Note: duplicate_division_template is provided by MacroMixin (supports PE & Linux).

    # =========================================================== CONSTRUCTION
    def _bld_cache_path(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        return os.path.join(root, "research", "windows", "blddb_win.pkl")

    def _bld_cache_load(self):
        """{id: (key, province_level)} from disk, or None.

        2026-11-21 (P1-3): the building table is 55 immutable templates, but the
        only way to read it live is `win_scan_class("CBuildingTemplate")`, an
        8.5 GB byte scan -- and `_load_buildings` used to reach it through
        `oi.singleton("CBuildingDatabase")`, which builds the whole ObjectIndex
        (50 s measured) before falling back.  Every cold hoi4_overview paid it
        for a table that never changes.  Commands only need `id`, so the id/key
        half is cached on disk the same way `eqtypes_win.pkl` already is.
        """
        import pickle
        try:
            with open(self._bld_cache_path(), "rb") as fh:
                d = pickle.load(fh)
            if isinstance(d, dict) and len(d) > 10:
                return d
        except Exception:
            pass
        return None

    def _bld_cache_save(self, byid, bykey):
        import pickle
        try:
            d = {int(bid): (key, bool(bykey.get(key, {}).get("province_level")))
                 for bid, key in byid.items()}
            with open(self._bld_cache_path(), "wb") as fh:
                pickle.dump(d, fh)
        except Exception:
            pass

    def _load_buildings(self):
        if getattr(self, "_bld_by_key", None) is not None:
            return
        p = self.p
        if sys.platform == "win32":
            cached = self._bld_cache_load()
            if cached:
                bykey, byid = {}, {}
                for bid, val in cached.items():
                    key, plev = val if isinstance(val, tuple) else (val, False)
                    bykey[key] = {"id": int(bid), "name": key, "ptr": 0,
                                  "province_level": bool(plev)}
                    byid[int(bid)] = key
                for bid, key in getattr(O, "BT_EXTRA", {}).items():
                    bykey.setdefault(key, {"id": bid, "name": key, "ptr": 0,
                                           "province_level": True})
                    byid[bid] = key
                self._bld_by_key, self._bld_by_id = bykey, byid
                return
        db = self.oi.singleton("CBuildingDatabase")
        if not db:
            self._oi = None
            db = self.oi.singleton("CBuildingDatabase")
        if not db and sys.platform == "win32":
            # PE 1.19.5.0: CBuildingDatabase has no RTTI-visible singleton. The 55
            # CBuildingTemplate objects are found by vtable instead; id at +0x08,
            # script key (MSVC std::string) at +0x270. Verified live: 20002
            # arms_factory, 20013 industrial_complex, 22395 coastal_bunker.
            bykey, byid = {}, {}
            for t in self.win_scan_class("CBuildingTemplate"):
                key = item_name(p, t + BT_KEY)
                if key:
                    bid = p.i32(t + BT_ID)
                    bykey[key] = {"id": bid, "name": key, "ptr": t,
                                  "province_level": p.i32(t + 0x294) > 0}
                    byid[bid] = key
            for bid, key in getattr(O, "BT_EXTRA", {}).items():
                bykey.setdefault(key, {"id": bid, "name": key, "ptr": 0,
                                       "province_level": True})
                byid[bid] = key
            if len(byid) > 10:
                self._bld_cache_save(byid, bykey)
            self._bld_by_key, self._bld_by_id = bykey, byid
            return
        if not db:
            self._bld_by_key = None
            self._bld_by_id = {}
            self._oi = None
            return
        bykey, byid = {}, {}
        for t in _arr(p, db, getattr(O, "BLD_DB_ITEMS", 0x40)):
            key = item_name(p, t + BT_KEY)
            if key:
                bid = p.i32(t + BT_ID)
                name = item_name(p, t + BT_NAME) if BT_NAME else None
                bykey[key] = {"id": bid, "name": name or key, "ptr": t,
                              "province_level": p.i32(t + 0x294) > 0}
                byid[bid] = key
        # Type ids the database does not describe (Windows: `infrastructure`).
        for bid, key in getattr(O, "BT_EXTRA", {}).items():
            bykey.setdefault(key, {"id": bid, "name": key, "ptr": 0,
                                   "province_level": True})
            byid[bid] = key
        self._bld_by_key, self._bld_by_id = bykey, byid

    def building_types(self):
        self._load_buildings()
        return {k: {"id": v["id"], "name": v["name"], "province_level": v["province_level"]}
                for k, v in sorted(self._bld_by_key.items())}

    def construction_queue(self, tag=None):
        p = self.p
        self._load_buildings()
        c_obj = self._c(tag)
        ps = p.u64(c_obj.ptr + CO_PRODUCTION)
        # 2026-11-22 (P3-4): 预建本国 CBuilding* -> (state_id, state_buildings_map) 反查表，
        # 仅遍历本国自有州（~24 州 / ~72 指针，< 1ms），精确补齐每条建造线的 state / state_name / slots_free。
        bld_to_state = {}
        state_blds = {}
        try:
            for s in _arr(p, c_obj.ptr, CO_STATES):
                sid = p.i32(s + ST_ID)
                bmap = {}
                for bp in _arr(p, s, ST_BUILDINGS):
                    bld_to_state[bp] = sid
                    nm = self._bld_by_id.get(p.i32(bp + BG_TYPE_ID))
                    if nm:
                        bmap[nm] = struct.unpack("<h", p.read(bp + BG_LEVEL, 2))[0]
                state_blds[sid] = bmap
        except Exception:
            pass
        _slot_buildings = set()
        _slot_bonus = None
        _meta_cache = {}
        try:
            import statemeta as _sm
            _slot_buildings = _sm._load_slot_buildings()
            try:
                _slot_bonus = (c_obj.modifiers() or {}).get("global_building_slots_factor")
            except Exception:
                _slot_bonus = None
        except Exception:
            _sm = None

        out = []
        prev_funded_total = 0
        # NOTE: this array is MIXED, alongside CBuildingProductionLine it also holds
        # CRailwayProductionLine, where +0x70 is NOT a building pointer (reading it
        # produced garbage and an EFAULT). Separate each element by its class.
        for i, l in enumerate(_arr(p, ps, PS_CONSTRUCTION)):
            # 2026-11-22: classify by vptr.  `oi.class_of()` forced the ObjectIndex
            # build (50 s) on the first construction read, which is inside the
            # first snapshot of every cold hoi4_wait_for.
            _vtb = getattr(O, "VT_BUILDING_LINE", None)
            cls = "CBuildingProductionLine" if (_vtb and p.u64(l) == p.base + _vtb) else None
            if cls is None:
                cls = self.oi.class_of(l)
            fac = p.i32(l + 0x18)
            row = {"index": i, "class": cls, "factories": fac}
            if cls == "CBuildingProductionLine":
                prog = p.i64(l + BL_PROGRESS) / O.FIXED
                cost = p.i64(l + BL_COST) / O.FIXED
                rate = p.i64(l + BL_RATE) / O.FIXED
                b = p.u64(l + BL_BUILDING)
                # 2026-11-22: was `self.oi.class_of(b) == "CBuilding"`, which is
                # another ObjectIndex build; the type id lookup already proves the
                # pointer is a building (a bad pointer raises and is dropped).
                bld_name = None
                if b:
                    try:
                        bld_name = self._bld_by_id.get(p.i32(b + BG_TYPE_ID))
                    except Exception:
                        pass
                sid = bld_to_state.get(b) if b else None
                sname = None
                sfree = None
                if sid is not None and _sm is not None:
                    if sid not in _meta_cache:
                        try:
                            _meta_cache[sid] = _sm.state_meta(sid, state_blds.get(sid), _slot_bonus)
                        except Exception:
                            _meta_cache[sid] = {}
                    _m = _meta_cache[sid]
                    sname = _m.get("name")
                    sfree = _m.get("slots_free")

                # 区分“正在建造 / 排队等民工 / 满槽或无民工真停滞”：
                # 当国家可用民工已被前面建造线吃满（如 15+6=21）而本州仍有空槽时，
                # 0 厂属于正常队列等待（status="queued", stalled=False），不应误判为卡死。
                no_slot = (
                    bld_name in _slot_buildings
                    and sfree is not None
                    and int(sfree) <= 0
                )
                if fac > 0:
                    status = "building"
                    stalled = False
                    stall_reason = None
                elif no_slot:
                    status = "stalled_no_slot"
                    stalled = True
                    stall_reason = "state %s has no free building slots" % sid
                elif prev_funded_total > 0:
                    status = "queued"
                    stalled = False
                    stall_reason = None
                else:
                    status = "stalled_no_cic"
                    stalled = True
                    stall_reason = "no civilian factories allocated to queue head"

                row.update({
                    "state": sid,
                    "state_name": sname,
                    "state_slots_free": sfree,
                    "building": bld_name,
                    "progress": round(prog / cost, 4) if cost > 0 else 0.0,
                    "progress_ic": round(prog, 2),
                    "cost_ic": round(cost, 2),
                    "ic_per_day": round(rate, 2),
                    "days_left": (round(max(0.0, (cost - prog) / rate), 1)
                                  if rate > 0 else None),
                    "status": status,
                    "stalled": stalled,
                })
                if status == "queued":
                    row["waiting_for_cic"] = True
                if stall_reason:
                    row["stall_reason"] = stall_reason
                prev_funded_total += max(0, fac)
            else:
                # CRailwayProductionLine -- different class, offsets not verified.
                row.update({"state": None, "state_name": None,
                            "progress": None, "progress_ic": None, "cost_ic": None,
                            "ic_per_day": None, "days_left": None,
                            "building": ("railway" if cls == "CRailwayProductionLine"
                                         else None),
                            "status": "unknown",
                            "layout_verified": False})
                prev_funded_total += max(0, fac)
            out.append(row)
        return out

    def states(self, tag=None):
        p = self.p
        return [{"id": p.i32(s + ST_ID), "ptr": s} for s in _arr(p, self._c(tag).ptr, CO_STATES)]

    def add_construction(self, state_id: int, building: str, level: int = 1,
                         province: int = 0, tag=None):
        """Queue a building: the same as using the construction tab."""
        self._load_buildings()
        c = self._c(tag)
        b = self._bld_by_key.get(building)
        if not b:
            raise KeyError("no such building type: %s (see building_types)" % building)
        if sys.platform == "win32":
            # Real command channel on the PE build.  The queue IS readable now
            # (PS_CONSTRUCTION + BL_PROGRESS were fixed on 2026-11-19), so report
            # the line the engine really created instead of trusting the post.
            #
            # `level` goes straight into CAddConstructionCommand+0x48.  Disassembly
            # of Execute (0x11DAEF0) shows `cmp [rdi+0x48], r15d / jle ...`, i.e.
            # it is the TARGET LEVEL of that building slot, not "how many levels to
            # queue": the engine builds one 7200-IC step at a time until the slot
            # reaches it.  A player clicking the build button sends 1, which is also
            # this default.  Measured: level=2 on a fresh slot still produced a
            # single queue line with cost_ic 7200.
            import cmdwin
            before = self.construction_queue(c.tag)
            r = cmdwin.add_construction(self, int(state_id), b["id"], level)
            self._settle()
            after = self.construction_queue(c.tag)
            new = [q for q in after if q.get("building") == building
                   and q["index"] >= len(before)]
            out = {"ok": bool(r.get("ok")) and len(after) > len(before),
                   "state": int(state_id), "building": building,
                   "type_id": b["id"], "level_sent": level,
                   "queue_before": len(before), "queue_after": len(after),
                   "levels_queued": max(0, len(after) - len(before)),
                   "cmd": r.get("cmd")}
            if new:
                out["line"] = new[0]
                # 2026-11-21 (P1-5): `ok` only ever meant "the queue grew".  When
                # every civilian factory is already busy the engine still creates
                # the line -- with 0 factories and stalled=true -- and the
                # 2026-11-20 playtest read that as success.  Say which one it is
                # instead of leaving ok:true and stalled:true side by side.
                _ln = new[0]
                try:
                    _fac = int(_ln.get("factories") or 0)
                except Exception:
                    _fac = 0
                if _fac <= 0 or _ln.get("stalled"):
                    out["queued"] = True
                    out["progressing"] = False
                    out["stalled"] = bool(_ln.get("stalled"))
                    out["warning"] = (
                        "queued, but this line holds %d civilian factor%s -- it "
                        "will not progress until CIC frees up. ok=true means the "
                        "queue grew, not that it is building."
                        % (_fac, "y" if _fac == 1 else "ies"))
                else:
                    out["progressing"] = True
            if level != 1:
                out["note"] = ("level is the slot's target level, not a repeat "
                               "count; one queue line was created (cost_ic is a "
                               "single level).")
            if not out["ok"]:
                out["error"] = ("the game accepted the pointer but the queue did "
                                "not grow -- usually the state has no free "
                                "building slot for this type")
            return out
        before = len(self.construction_queue(c.tag))
        # CBuildingReference (1.19.4.1, verified by disassembly and live test):
        #   +0x08 = location id   (state id for a state-level building, province id
        #                          for a province-level one)
        #   +0x0C = building type id
        #   +0x10 = 0
        # The AddConstruction ctor copies ref+8..ref+0x10 into cmd+0x30..+0x3C.
        loc = int(province or 0)
        if loc <= 0:
            loc = int(state_id)
        with self:
            cmd = self.cmd.make("AddConstruction", tag=c.tag_id,
                                ref_vptr=self.va(BUILDING_REF_VTABLE),
                                province=loc, building_type=b["id"], state=0,
                                level=level, flag=1)
            # CCommand::Run: CanExecute -> Post(queue, cmd, 1), the click path.
            rv = self._run_cmd(cmd)
        if not (rv & 0xFF):
            return {"ok": False, "before": before, "after": before,
                    "error": "the game refused the construction order (CanExecute false)"}
        # The command is asynchronous: the construction line appears on the next tick.
        q = self.construction_queue(c.tag)
        for _ in range(12):
            if len(q) > before:
                break
            self._settle(0.5)
            q = self.construction_queue(c.tag)
        return {"ok": len(q) > before, "before": before, "after": len(q), "queue": q}

    def remove_construction(self, index: int, tag=None):
        """Remove one queued construction line. Returns ok only if the queue shrank.

        2026-11-20: `CRemoveConstructionCommand` (vtable 0x2A10C48, CanExecute
        0x11F17C0, Execute 0x11E2F30) is REFUSED by its own CanExecute on this
        build for every field combination that could be constructed from the
        published layout -- it wants an 8-byte value at +0x28 that neither the
        country tag (1) nor the line handle ({59,130}) satisfies (the only value
        that passed was {59,1}, and that Execute then ignored).  See
        KNOWN_ISSUES P1 #15.

        So the queue is edited directly instead: CProductionStatus+PS_CONSTRUCTION
        is a CPdxArray<CProductionLine*>; shift the pointers after `index` left by
        one and decrement the count.  Verified live 2026-03-05: the line vanishes,
        its 15 civilian factories go back to idle, and the engine does NOT
        re-create it on later ticks (checked after 4 in-game days).
        """
        p = self.p
        c = self._c(tag)
        ps = p.u64(c.ptr + CO_PRODUCTION)
        off = PS_CONSTRUCTION
        n = p.i32(ps + off + O.PDXARRAY_SIZE)
        data = p.u64(ps + off + O.PDXARRAY_DATA)
        before = self.construction_queue(c.tag)
        if not (0 <= index < n) or not data:
            return {"ok": False, "before": len(before),
                    "error": "no such queue line: %d (of %d)" % (index, n)}
        row = before[index] if index < len(before) else {}
        if n > 1:
            raw = p.read(data, 8 * n)
            # 2026-11-22 (复测 5 P0 §2.1): 正确 splice 是保留前 index 项、把 [index+1:] 左移 1 格、
            # 尾部仅清零 1 个指针槽 (8 字节)。旧写法 `raw[8*(index+1):] + b"\0"*(8*(index+1))`
            # 在 index>0 时会直接抹掉前 index 条产线并在尾部留空指针，导致引擎遍历 AV 崩档。
            new_buf = raw[:8 * index] + raw[8 * (index + 1):] + b"\0" * 8
            p.write(data, new_buf)
        else:
            p.write(data, b"\0" * 8)
        p.write(ps + off + O.PDXARRAY_SIZE, struct.pack("<i", n - 1))
        self._settle(0.2)
        after = self.construction_queue(c.tag)
        return {"ok": len(after) < len(before),
                "before": len(before), "after": len(after),
                "removed": row.get("building"), "removed_state": row.get("state"),
                "state_line": index,
                "method": "queue-array",
                "note": "edited the queue array directly: the engine's "
                        "CRemoveConstructionCommand rejects this build "
                        "(KNOWN_ISSUES P1 #15)"}

    # ========================================================== DIPLOMACY
    def relation_count(self, tag=None):
        return _count(self.p, self.p.u64(self._c(tag).ptr + CO_DIPLOMACY), DP_RELATIONS)

    # ============================================================== HIZ
    def game_speed(self):
        """纯只读获取当前游戏速度（0..4，对应 UI 1~5 格）。"""
        if sys.platform == "win32":
            import cmdwin
            sp = cmdwin.get_game_speed(self)
            tbl = cmdwin.game_speed_seconds(self)
            sph = tbl[sp] if (sp is not None and 0 <= sp < len(tbl)) else None
            return {
                "speed": sp,
                "ui_speed": (sp + 1) if (sp is not None and 0 <= sp <= 4) else None,
                "seconds_per_hour": sph,
            }
        return {"speed": None, "ui_speed": None, "seconds_per_hour": None}

    def set_game_speed(self, speed: int, measure_seconds=None, use_command: bool = False):
        """设置游戏速度（工具层最高允许 engine 3 = UI 4 速；传 4/5 自动钳位至 3），并返回回读/速率验证结果。"""
        speed = int(speed)
        if not 0 <= speed <= 5:
            raise ValueError("speed must be between 0 and 5")
        if sys.platform == "win32":
            import cmdwin
            return cmdwin.set_game_speed(
                self, speed, measure_seconds=measure_seconds, use_command=use_command
            )
        capped = min(speed, 3)
        with self:
            self.cmd.send("SetGameSpeed", speed=capped)
        return {"ok": True, "requested": speed, "speed": capped,
                "engine_speed": capped, "ui_speed": capped + 1,
                "max_engine_speed": 3, "max_ui_speed": 4,
                "clamped": capped != speed}

    # ============================================================= SUMMARY
    def _air_snapshot(self, max_age=25.0):
        """Player air wings, cached per game pid.

        2026-11-17: the user's complaint was that nothing ever reminded the model
        to touch the air force -- in the 1940 run all 15 wings sat at mission 0
        from 1936 to 1940.  `hoi4_overview` now reports that, but a CAirWing RTTI
        scan is expensive, so it is cached for a few seconds and keyed by pid.
        """
        import time
        pid = getattr(getattr(self, "p", None), "pid", None)
        if (_AIR_SNAP.get("pid") == pid and _AIR_SNAP.get("rows") is not None
                and (time.time() - float(_AIR_SNAP.get("ts") or 0)) < max_age):
            return _AIR_SNAP["rows"]
        rows = []
        try:
            if sys.platform == "win32":
                import airops_win as AW
                rows = AW.german_wings(self).get("wings") or []
            else:
                import airops
                rows = airops.scan_wings(self) or []
        except Exception:
            rows = []
        _AIR_SNAP.update(pid=pid, ts=time.time(), rows=rows)
        return rows

    def _navy_snapshot(self):
        """Player fleets + task-force missions (pointer walk, no RTTI)."""
        try:
            import navalops_win as NW
            fleets = NW.naval_fleets(self)
        except Exception:
            return []
        return fleets or []

    def _light_alerts(self, tag, summary, lines, cur):
        """Alerts for hoi4_overview, without the 15 s focus ranking.

        `alerts()` calls selectable_focuses() and available_research(); together
        they pushed hoi4_overview past the MCP client timeout on the PE build
        (measured 34 s for alerts alone, 49 s for candidates).  Everything here
        is already-read data.
        """
        out = []
        try:
            if not cur:
                out.append("FOCUS_IDLE: 国策槽空置（用 hoi4_selectable_focuses 取候选）")
        except Exception:
            pass
        try:
            slots = self.research_slots(tag)
            empty = [s["slot"] for s in slots if not s.get("slot_busy")]
            if empty:
                out.append("RESEARCH_IDLE: %d 个科研槽空置 (slot %s)"
                           % (len(empty), ", ".join(map(str, empty))))
        except Exception:
            pass
        try:
            pp = summary.get("political_power") or 0
            if pp >= 160:
                out.append("PP_HIGH: 政治点数 %d，可切换法案/任命顾问或点决议" % pp)
        except Exception:
            pass
        # --- 2026-11-17: the two resources that silently go to waste --------
        # Political power stops at 2000 and XP past 100 is only worth anything
        # once it is spent (a second grand doctrine costs 100 army XP).  The
        # player asked for an explicit reminder instead of having to remember
        # to look at the top bar.
        try:
            if pp >= 1000:
                out.append("PP_CAPPING: 政治点数 %d（>=1000，上限 2000，再攒就浪费 "
                           "-> 切法案 / 雇佣军官团顾问 / 点决议）" % pp)
        except Exception:
            pass
        try:
            xp = summary.get("experience") or {}
            for _svc, _label, _spend in (
                    ("army", "陆军", "第二门 Grand Doctrine / 改师编制"),
                    ("navy", "海军", "海军学说 / 改舰船设计"),
                    ("air", "空军", "空军学说 / 改飞机设计")):
                _v = xp.get(_svc)
                if _v is not None and _v >= 100:
                    out.append("XP_READY(%s): %s经验 %.1f（>=100，可花在%s）"
                               % (_svc, _label, _v, _spend))
        except Exception:
            pass
        try:
            q = len(self.construction_queue(tag))
            civ = summary.get("civilian_factories") or 0
            if q == 0:
                # 2026-11-17 (third pass): a 1939 save came back with 66 civilian
                # factories building nothing.  "建造队列仅 0 项" reads like a
                # rounding error; an empty queue with 66 idle factories does not.
                out.append("CIV_FACTORY_IDLE: 建造队列为空，%d 座民工全部闲置 "
                           "-> hoi4_add_construction(building='arms_factory'|"
                           "'industrial_complex', state=...)" % civ)
            else:
                # 2026-11-19 (measured): the queue was 8 lines deep while 17-40
                # civilian factories sat idle, and `hoi4_overview` said nothing
                # because it only looked at "queue empty".  The per-line
                # `progress`/`progress_ic` readout is ALSO unreliable on 1.19.5
                # (line 0 stayed at 141.75 IC / 1.3% for 110 game days during
                # which a factory actually finished), so the factory ledger is
                # the only trustworthy signal here.
                _assigned = sum(int(l.get("factories") or 0)
                                for l in self.construction_queue(tag))
                _idle = int(civ) - _assigned
                # 2026-11-21 (续测 P2-9): `civ` is the RAW built level, which
                # includes the factories permanently locked to consumer goods --
                # they can never be assigned to construction.  So `civ - assigned`
                # is NOT "idle".  Measured: 21 of 35 working while the alert said
                # "14 idle".  Only complain when almost nothing is assigned, and
                # word it as "not building" instead of "idle".
                if _assigned == 0 or _idle >= max(10, int(civ) // 2):
                    out.append("CONSTRUCTION_STALLED: 队列 %d 项，但只有 %d/%d 座民工投入建造"
                               "（其余含消费品工厂，不能计入闲置）-> 检查队列里的州是否还能造，"
                               "并用 hoi4_state_detail 核对真实进度" % (q, _assigned, civ))
                elif q < 6:
                    out.append("CONSTRUCTION_LOW: 建造队列仅 %d 项，民工即将闲置" % q)
        except Exception:
            pass
        try:
            n = len(self.unassigned_divisions(tag))
            if n:
                out.append("UNASSIGNED_DIVS: %d 个师未编入集团军" % n)
        except Exception:
            pass
        try:
            total = summary.get("military_factories") or 0
            used = sum(int(l.get("factories") or 0) for l in lines
                       if l.get("class") == "CMilitaryProductionLine")
            if total - used >= 2:
                out.append("MIL_FACTORY_IDLE: %d 座军工厂闲置未分配"
                           " -> hoi4_set_production_amount / hoi4_add_production_line"
                           % (total - used))
        except Exception:
            pass
        # --- 2026-11-17 (third pass): the last two blind spots -----------------
        # Dockyards were never counted anywhere, and a front order with
        # `assigned == 0` is the single most expensive silent failure in this
        # codebase: the line is drawn, the UI shows it, and not one division ever
        # marches.  Both are cheap reads on data already in hand.
        try:
            dock = summary.get("naval_dockyards") or 0
            used = sum(int(l.get("factories") or 0) for l in lines
                       if l.get("class") == "CNavalProductionLine")
            if dock and dock - used >= 2:
                out.append("DOCKYARD_IDLE: %d/%d 座船坞闲置未分配"
                           " -> hoi4_set_production_amount(line=..., amount=...)"
                           % (dock - used, dock))
        except Exception:
            pass
        try:
            if sys.platform == "win32":
                import armyops_win as _aw
                empty_fronts = []
                for _og in _aw.armies(self):
                    try:
                        for _o in _aw.orders(self, _og):
                            # 2026-11-19: armyops_win.orders() calls this field
                            # `assigned_divisions`; the old `assigned` read was
                            # always None -> 0, so EVERY front order was reported
                            # as "assigned=0" (a false crash-hazard alert).
                            if _o.get("type") == 2 and not int(
                                    _o.get("assigned_divisions")
                                    or _o.get("assigned") or 0):
                                empty_fronts.append(_o.get("id"))
                    except Exception:
                        continue
                if empty_fronts:
                    out.append("FRONT_NO_DIVISIONS: %d 条前线命令 assigned=0"
                               "（线画了但一个师都没挂上去）"
                               " -> hoi4_assign_divisions(mode='even')"
                               % len(empty_fronts))
        except Exception:
            pass
        # --- 2026-11-17: air and navy were NEVER touched by any run ----------
        # (1940 campaign: 15 wings at mission 0 from 1936 to 1940, and no fleet
        # ever got a mission.) These two alerts exist so the model cannot forget.
        # 2026-11-22 (P3-3): 启用 Rust fastmem 后精简索引冷建仅 ~0.7-1.4s（缓存命中 10ms），
        # 可直接预热，彻底消除新进程首次调用必吐的过期 “首次约 40 s” 告警；仅在无 fastmem
        # 的慢速回落模式下延迟跳过，并给出准确文案。
        _idx_ready = getattr(self, "_oi", None) is not None
        if not _idx_ready:
            try:
                import fastmem as _fm
                if getattr(_fm, "AVAILABLE", False):
                    _ = self.oi
                    _idx_ready = getattr(self, "_oi", None) is not None
            except Exception:
                pass
        if not _idx_ready:
            out.append("AIR_NAVY_DEFERRED: 对象索引尚未建立（未启用 fastmem 时首次扫描较慢），本次跳过空军/海军任务检查 -> 先调 hoi4_air_wings 或 hoi4_inspect(air) 预热索引，下一次 overview 就会带上它们")
        _wings_cache = []
        _fleets_cache = []
        try:
            if not _idx_ready:
                raise RuntimeError("object index not built yet")
            _all_w = self._air_snapshot() or []
            _wings_cache = _all_w
            wings = [w for w in _all_w
                     if (w.get("type") or "") != "transport_plane"]
            if wings:
                # transport planes never take a strategic mission (they only
                # fly paradrop/supply orders), so counting them made the alert
                # permanently impossible to clear -- seen 2026-11-17.
                # An earlier revision also required `mission_on` (+0xA4), on the
                # theory that it was the player's enable bit.  Measured on a live
                # 1936 PE campaign: fighter wing 1 -> mask 1 and CAS wing 8 ->
                # mask 2 leave +0xA4 at 0 for BOTH, and the two wings are
                # byte-identical over 0x90..0xC4 apart from the mask itself.  Two
                # game days later +0xA4 was still 0 everywhere.  So there is no
                # fighter/CAS asymmetry and +0xA4 is a runtime "sorties airborne"
                # counter, which is legitimately 0 in peacetime.  Keying the red
                # dot off it made the alert permanently unclearable.
                idle = [w for w in wings if not w.get("mission")]
                planes = sum(int(w.get("planes") or 0) for w in wings)
                if idle:
                    _n_tr = sum(1 for w in _all_w
                                if (w.get("type") or "") == "transport_plane")
                    _tr_planes = sum(int(w.get("planes") or 0) for w in _all_w
                                     if (w.get("type") or "") == "transport_plane")
                    # 2026-11-21: the counts here are "wings that can take a
                    # strategic mission", which is why they differ from
                    # hoi4_air_wings: transport wings are excluded on purpose
                    # (they never fly a strategic mission).  Say so, otherwise
                    # the two tools look like they disagree -- measured
                    # 14/923 here vs 15/1003 there, and the delta was exactly
                    # the single 80-plane transport wing.
                    out.append("AIR_NO_MISSION: %d/%d 个可执行战略任务的空军联队（共 %d 架）"
                               "没有任务%s -> hoi4_air_mission(wing_ptr='fighters', "
                               "mission_mask=1) 或 hoi4_attach_air_wings"
                               % (len(idle), len(wings), planes,
                                  "；另有 %d 个运输机联队（%d 架）不计（运输机不接战略任务）"
                                  % (_n_tr, _tr_planes) if _n_tr else ""))
        except Exception:
            pass
        try:
            if not _idx_ready:
                raise RuntimeError("object index not built yet")
            fleets = self._navy_snapshot()
            _fleets_cache = fleets or []
            # 2026-11-22 (复测 5 P1 §3.1): 排除预备舰队（is_reserve / mission==8），
            # 预备舰队用于自动承接新造舰船，不是缺任务的作战特遣舰队。
            tfs = [
                (f, t) for f in _fleets_cache
                if not f.get("is_reserve")
                for t in (f.get("task_forces") or [])
                if not t.get("is_reserve") and int(t.get("mission") or 0) != 8
            ]
            if tfs:
                # 2026-11-22 (复测 5 §9.3 残留 A): 正在执行活动（返港/整修）且已在 tf+0x4F0
                # 挂上 queued_mission 的特遣舰队已受命，不再误报 NAVY_NO_MISSION。
                idle = [
                    (f, t) for f, t in tfs
                    if not (t.get("effective_mission") or t.get("mission") or t.get("queued_mission"))
                ]
                if idle:
                    out.append("NAVY_NO_MISSION: %d/%d 个特遣舰队没有任务 "
                               "-> hoi4_naval_mission(fleet_ptr, tf_ptr, mission, regions)"
                               % (len(idle), len(tfs)))
        except Exception:
            pass
        # --- 2026-11-22 (复测 5 §8 专题): 装备代差 / 库存过剩 / 空军缺口 / 燃油归零 ----
        try:
            import equipmeta as _em
            _avail_v = self._active_variant_names(tag)
            _pid = getattr(getattr(self, "p", None), "pid", None)
            _tag_s = self._c(tag).tag
            _obs_alerts, _all_obs, _obs_sup = _em.collect_obsolete_alerts(
                lines, _avail_v, pid=_pid, tag=_tag_s,
                dedupe=True, max_alerts=3, max_payback_days=60,
            )
            out.extend(_obs_alerts)
        except Exception:
            pass
        try:
            import equipmeta as _em
            _st_tot = (self.equipment_stockpile(tag) or {}).get("totals_by_equipment") or {}
            _total_mil = int(summary.get("military_factories") or 0)
            _divs_n = int((self.divisions(tag, limit=1) or {}).get("count") or 0)
            for _sr in _em.compute_surplus_lines(lines, _st_tot, _total_mil, _divs_n)[:2]:
                out.append(_sr["alert"])
        except Exception:
            pass
        try:
            import equipmeta as _em
            if _wings_cache:
                for _ag in _em.compute_air_gap_alerts(_wings_cache, lines)[:2]:
                    out.append(_ag)
        except Exception:
            pass
        try:
            import equipmeta as _em
            _fz = _em.compute_fuel_zero_alert(summary.get("fuel"), _wings_cache, _fleets_cache)
            if _fz:
                out.append(_fz)
        except Exception:
            pass
        # 2026-11-23: 挂载老手战略体检红灯（弱编+XP闲置 / 产能倒挂 / 前线断补危机）
        try:
            import macros as _mc
            _hb = _mc.strategic_health_board(self, tag=tag) or {}
            for _hal in (_hb.get("alerts") or []):
                if _hal.get("severity") == "high":
                    out.append("%s: %s (%s)" % (_hal.get("code"), _hal.get("title"), _hal.get("action") or ""))
                    if len(out) >= 14:
                        break
        except Exception:
            pass
        # 2026-11-21 (P2-15): the 1940 campaign was lost because every division sat
        # in Poland and NOTHING stood on the French border for seven months, and no
        # alert said so -- war_readiness only answers when asked about that country.
        # Check every enemy, every turn: at war + a border + zero divisions on it.
        try:
            _enemies = (self.wars(tag) or {}).get("enemies") or []
            if _enemies:
                import armyops_win as _aw
                _co2 = self._c(tag)
                try:
                    _myprov = set()
                    for _d in (self.divisions(tag, limit=400).get("divisions") or []):
                        _pp = int(_d.get("province_ptr") or "0x0", 16)
                        if _pp:
                            _myprov.add(self.p.i32(_pp + 0xA4))
                except Exception:
                    _myprov = set()
                _undef = []
                for _e in _enemies:
                    try:
                        _seg = _aw.fronts_towards(self, _e, _co2.tag)
                    except Exception:
                        continue
                    _border = set()
                    for _s in _seg:
                        _border.update(_s.get("border_provinces") or [])
                    if _border and not (_border & _myprov):
                        _undef.append({"enemy": _e, "border_provinces": len(_border)})
                if _undef:
                    out.append(
                        "BORDER_UNDEFENDED: 交战国方向边境一个师都没有 —— %s"
                        " -> hoi4_deploy_to_border(target_tag=...)"
                        % ", ".join("%s(%d 省)" % (x["enemy"], x["border_provinces"])
                                    for x in _undef))
        except Exception:
            pass
        return out

    def _light_candidates(self, tag, lines, dec_rows=None):
        """Cheap candidates block: no focus ranking, no tech-tree scan."""
        out = {}
        try:
            slots = self.research_slots(tag)
            out["empty_research_slots"] = [s["slot"] for s in slots
                                           if not s.get("slot_busy")
                                           or s.get("slot_done")]
        except Exception:
            out["empty_research_slots"] = []
        try:
            out["production_lines"] = [
                {"line_index": l["index"], "equipment": l.get("equipment"),
                 "factories": l.get("factories")}
                for l in lines if l.get("class") == "CMilitaryProductionLine"][:8]
        except Exception:
            out["production_lines"] = []
        try:
            # 2026-11-20 (P2 #11): bare keys said nothing about what a decision
            # costs or how long it takes.  The rich rows are already parsed and
            # cached in decisions_meta.pkl, so this costs one extra dict per row.
            # 2026-11-21 (续测 P2-10): the list mixed in decisions that belong to
            # OTHER countries (ENG_abdication_crisis, JAP_implementing_unos...)
            # because "visible" is evaluated globally and only the `visible` trigger
            # carries a `tag = X` guard.  `mine` is the parser's verdict on that
            # guard, so drop everything that is not ours.
            out["available_decisions"] = [
                {"key": d["key"], "title": d.get("title"),
                 "cost_pp": d.get("cost_pp"),
                 "days": d.get("days_remove") or d.get("days_mission_timeout"),
                 "category": d.get("category"), "mine": d.get("mine", True)}
                for d in (dec_rows if dec_rows is not None
                          else self.decisions(tag, "visible", rich=True))
                if d.get("mine", True)][:12]
        except Exception:
            out["available_decisions"] = []
        out["note"] = ("heavy blocks (focus ranking / tech list / build states / ideas) "
                       "moved to hoi4_selectable_focuses, hoi4_research_available, "
                       "hoi4_states_detail and hoi4_advisors")
        return out

    def overview(self, tag=None):
        c = self._c(tag)
        cur = self.current_focus(c.tag)
        summary = c.summary()
        lines = self.production_lines(c.tag)
        used_mil = sum(int(l.get("factories") or 0) for l in lines
                       if l.get("class") == "CMilitaryProductionLine")
        alerts = self._light_alerts(c.tag, summary, lines, cur)
        cons = self.construction_queue(c.tag)
        armies_n = len(_arr(self.p, c.ptr, CO_ARMIES))
        unassigned = len(self.unassigned_divisions(c.tag))
        dec = self.decisions(c.tag, "visible", rich=True)
        # 2026-11-22 (P3-1): `decisions_visible` counted every globally-visible
        # decision (13) while `candidates.available_decisions` was already
        # tag-filtered (9), so one overview carried two different "decision"
        # numbers with no way to tell them apart.  Report both, named.  The rows
        # are now parsed once here and handed to _light_candidates.
        dec_mine = [d for d in dec if d.get("mine", True)]
        cand = self._light_candidates(c.tag, lines, dec_rows=dec)
        tree = self.focus_tree(c.tag)
        research = self.research_slots(c.tag)
        politics = self.politics(c.tag)
        prod = lines
        states = self.states(c.tag)
        focus_block = {
            "current": cur["key"] if cur else None,
            "name": cur["name"] if cur else None,
            "tree_size": len(tree),
        }
        if cur:
            for _fk in ("progress", "days_done", "cost_days", "days_left", "percent"):
                if _fk in cur:
                    focus_block[_fk] = cur[_fk]
        return {
            "date": self.date()["text"],
            "alerts": alerts,
            "factories": {"civ_total": summary.get("civilian_factories"),
                          "mil_total": summary.get("military_factories"),
                          "mil_free": max(0, (summary.get("military_factories") or 0) - used_mil),
                          "dock_total": summary.get("naval_dockyards"),
                          "construction_queue_len": len(cons)},
            "army_summary": {"total_divisions": armies_n,
                             "unassigned_divisions": unassigned},
            "candidates": cand,
            "country": summary,
            "focus": focus_block,
            "research": research,
            "politics": politics,
            "decisions_visible": len(dec_mine),
            "decisions_visible_all": len(dec),
            "decisions_visible_mine": len(dec_mine),
            "production": prod,
            "obsolete_lines_count": sum(1 for l in prod if l.get("obsolete")),
            "construction": cons,
            "states": len(states),
        }

    # ------------------------------------------------------------------
    #  Thresholds the player asked to be warned about
    #  (2026-11-17: "PP 点到 1000 和海陆空经验到 100 的时候能不能加个工具提醒")
    # ------------------------------------------------------------------
    THRESHOLDS = {"political_power": 1000, "army": 100, "navy": 100, "air": 100}
    SPEND_HINT = {
        "political_power": "切法案 / 雇佣军官团顾问 / 点决议 / 装备设计",
        "army": "第二门 Grand Doctrine（100 XP）/ 改师编制",
        "navy": "海军学说 / 改舰船设计",
        "air": "空军学说 / 改飞机设计",
    }

    def thresholds(self, tag=None):
        """Political power and the three experience pools, with the alarms.

        Pure reads.  `reached` is what the per-turn checklist and the player
        care about: PP>=1000 (the cap is 2000, so banking further is waste) and
        army/navy/air experience >=100 (one doctrine step).
        """
        c = self._c(tag)
        s = c.summary()
        pp = s.get("political_power") or 0
        xp = s.get("experience") or {}
        reached = []
        if pp >= self.THRESHOLDS["political_power"]:
            reached.append("political_power>=1000")
        for k in ("army", "navy", "air"):
            v = xp.get(k)
            if v is not None and v >= self.THRESHOLDS[k]:
                reached.append("%s_xp>=100" % k)
        return {
            "date": self.date()["text"],
            "tag": c.tag,
            "political_power": pp,
            "political_power_cap": 2000,
            "experience": xp,
            "thresholds": dict(self.THRESHOLDS),
            "reached": reached,
            "spend_hint": dict(self.SPEND_HINT),
            "warn": bool(reached),
        }

# ============================================================================
#  EXTENDED MACRO LAYER
#  Resources, armies, air, navy, diplomacy, characters, projects, statistics,
#  and generic access to all 370 player commands.
# ============================================================================

# --- extra CCountry offsets: arrays living directly inside CCountry ----------
CO_THEATRES         = 0x0168   # CPdxArray<CTheatre*>
CO_DIV_TEMPLATES    = 0x01B8   # CPdxArray<CReferencedDivisionTemplate*>
CO_FLEETS           = 0x0278   # CPdxArray<CFleet*>
CO_ARMIES           = 0x0290   # CPdxArray<CArmy*>
CO_COMMAND_POWER    = 0x01F0   # int64 x1e5
CO_RESOURCES        = 0x11F8   # CCountryResources*  (0x11C0 was the Linux field:
                               # on PE it holds a .rdata pointer, which is why
                               # hoi4_resources returned 0.0 for every resource)
CO_FOCUS_COMPLETED  = 0x1C10   # CPdxArray<CNationalFocus*>
CO_SUBUNITS         = 0x26C0   # CPdxArray<CSubUnitDefinition*>
CO_RELATIONS_ARR    = 0x2888   # CPdxArray<CRelationStatus*>  (one per country)
CO_PROGRAM_STATUS   = 0x0FA8 if sys.platform == "win32" else 0x0F80   # NProject::CProgramStatus*
CO_OCCUPATION       = getattr(O, "CO_OCCUPATION_STATUS", 0x0FA8)       # CCountryOccupationStatus*
CO_OPERATIONS       = 0x1548   # CCountryOperationManager*
CO_PROJECT_POOL     = 0x15D8   # NProject::CProjectPool*
CO_EQUIPMENT_TYPES  = 0x11E0   # CPdxArray<CEquipmentType*>

# CCountryResources: seven 16-byte {int64 amount; int32 id; ...} records from +0x60
RES_ARRAY           = 0x60
RES_STRIDE          = 0x10
RESOURCE_NAMES = ["oil", "aluminium", "rubber", "tungsten", "steel", "chromium", "coal"]

# CReferencedDivisionTemplate
DT_NAME             = 0x20
# CSubUnitDefinition
SU_KEY              = 0x10
SU_CATEGORY         = 0x60
# CFleet
FL_NAME             = 0xD8
# CCharacter
CH_NAME             = 0x28     # 1.19.5 (was 0x20; CCharacter grew when CScientist landed)
CH_KEY              = 0x48     # 1.19.5 (was 0x40)
# CRelationStatus
REL_INDEX           = 0x50

FN_DYNAMIC_VAR      = 0x2D8EE60   # void GetVariable(out, const char* NAME, scope, int)


class MacroMixin:
    """Extra read and action capabilities mixed into Game."""

    # ---------------------------------------------------------- helpers
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

    # ======================================================== ISTATISTIKLER
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

    # 2026-11-20 (P1 #6) -- the name side is SOLVED, the value side is mapped.
    #
    # Ledger (every address re-derived against 1.19.5.0 PE, base 0x7ff6eb560000;
    # RVAs are what you pass to a disassembler, absolute VAs are base + RVA):
    #
    #   * 0x22AEAB0 is NOT a variable registry -- it is plain LOCALISATION lookup
    #     ("token -> localised text"): it also answers GER_anschluss -> "德奥合并".
    #     It survives only as `loc_label`, which is a label, never a validation.
    #
    #   * The REAL registry is the static object at RVA 0x33DF1D0:
    #         +0x00  float max_load_factor      (1.0)
    #         +0x08  std::list sentinel
    #         +0x10  element count              (measured: 265)
    #         +0x18  bucket vector              +0x30 mask 0x1FF / +0x38 0x200
    #     Node: +0x00 next, +0x08 prev, +0x10 key std::string, +0x30 a 16-byte
    #     {vftable, capture} delegate, +0x40 string, +0x60 bool.
    #     Walking it live yields the lowercase names -- political_power, manpower,
    #     command_power, num_owned_states, ... -- i.e. exactly what the game dumps
    #     into documentation/dynamic_variables_documentation.md.  Authoritative.
    #
    #   * 0x1D5170 is _Hash::find for that container (mask/bucket prologue, 265
    #     entries).  Scanning .text for every `lea` to 0x33DF1D0 returns only 11
    #     sites: one static init (0x2FF54), eight registration sites in the
    #     0x71A080 / 0x71A270 / ... family, and two that feed the .md writer
    #     (0x732CF6 / 0x732D10 -> 0xC1D0C0; JSON wrapper 0xC1D180).
    #     => the registry is WRITE + ENUMERATE ONLY.  Nothing looks a name up in
    #     it at run time, which is why hunting the "formatter entry point" ends
    #     here: the per-variable delegate stored in the node is never called by
    #     name.
    #
    #   * Value evaluation lives elsewhere.  The engine's own entry is the
    #     developer-console command `get_var <name>` (impl RVA 0x277500; it
    #     resolves through 0x58C100 and formats through 0xC1D230).  That is a
    #     console command and it evaluates against the CURRENT UI SCOPE, so it is
    #     neither a pure-read path nor a reliable oracle for country values.
    #
    # Therefore on win32 `script_variable` reports `known` from the authoritative
    # registry, a native `value` for the country-scope names below, and an
    # explicit reason for everything else.
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

    # ============================================================ RESOURCES
    def resources(self, tag=None):
        """Strategic resources: production and available amount."""
        p = self.p
        r = p.u64(self._c(tag).ptr + CO_RESOURCES)
        if not r:
            return {}
        out = {}
        for i, nm in enumerate(RESOURCE_NAMES):
            a = r + RES_ARRAY + i * RES_STRIDE
            out[nm] = round(p.i64(a) / O.FIXED, 2)
        return out

    # =============================================================== ARMIES
    def armies(self, tag=None):
        p = self.p
        c = self._c(tag)
        out = []
        for i, a in enumerate(_arr(p, c.ptr, CO_ARMIES)):
            out.append({"index": i, "ptr": hex(a), "class": self.oi.class_of(a)})
        return {"count": len(out), "armies": out}

    def division_templates(self, tag=None):
        p = self.p
        c = self._c(tag)
        out = []
        for i, t in enumerate(_arr(p, c.ptr, CO_DIV_TEMPLATES)):
            out.append({"index": i, "name": item_name(p, t + DT_NAME), "ptr": hex(t)})
        return out

    # ============================================ DIVISION TRAINING (CONVEYORS)
    def _template_handle(self, t_ptr):
        """Packed {type,id} handle of a CDivisionTemplate (it lives at +0x08)."""
        raw = self.p.read(t_ptr + 0x08, 8)
        t, i = struct.unpack("<ii", raw)
        return raw, t, i, (i << 32) | (t & 0xFFFFFFFF)

    def conveyors(self, tag=None):
        """Live division-training conveyors.

        CCountry+0xF48 -> CDeploymentStatus, its CPdxArray<CMilitaryDeploymentConveyor*>
        at +0x68. Verified live 2026-11-04: after CreateConveyor the array went from
        0 to 1 and the element's class resolves to CMilitaryDeploymentConveyor.
        (An `oi.instances()` scan cannot be used here — the index is a snapshot taken
        before the conveyor existed.)
        """
        p = self.p
        c = self._c(tag)
        out = []
        # Windows puts CDeploymentStatus at +0xF70 (offsets_win); the Linux table has
        # no name for it and has always used +0xF48. Reading the Linux offset on the PE
        # build returned a null pointer, so conveyors() was empty for ever and
        # train_divisions reported "no conveyor appeared" even when one had been made.
        dep_off = getattr(O, "CO_DEPLOYMENT_STATUS", 0xF48)
        conv_off = getattr(O, "CO_DEPLOYMENT_CONVEYORS", 0x68)
        dep = p.u64(c.ptr + dep_off)
        if not dep:
            return out
        for conv in _arr(p, dep, conv_off):
            try:
                h_type, h_id = struct.unpack("<ii", p.read(conv + 0x08, 8))
            except Exception:
                continue
            row = {"ptr": hex(conv), "handle": [h_type, h_id],
                   "packed_handle": (h_id << 32) | (h_type & 0xFFFFFFFF)}
            # CMilitaryDeploymentConveyor.  The field map is build-specific:
            #   Linux 1.19.4.1: +0x18 template  +0x48 lines  +0x70 line array  +0x88 priority
            #   PE    1.19.5.0: +0x20 template  +0x50 priority  +0x78 line array
            #                   +0x4C deploy province (int32)
            # Verified 2026-11-15 by creating a conveyor and matching +0x20 against the
            # template pointer hoi4_division_templates returns.
            try:
                if sys.platform == "win32":
                    t_off, prio_off, arr_off = 0x20, 0x50, 0x78
                    row["deploy_province"] = p.i32(conv + 0x4C)
                else:
                    t_off, prio_off, arr_off = 0x18, 0x88, 0x70
                row["priority"] = p.i32(conv + prio_off)
                row["template"] = item_name(p, p.u64(conv + t_off) + DT_NAME)
                # 2026-11-22 (复测 5 §3.10): CPdxArray 布局为 {data +0, cap +8, size +0x0C}；
                # 旧代码读 `arr_off + 8` 读成了容量 (cap=6) 而非实际长度 (size=5)，导致
                # `lines: 6` 与 `lines_detail` 5 条长期不一致。现改为读 PDXARRAY_SIZE (+0x0C)。
                _arr_cap = p.i32(conv + arr_off + 8)
                _arr_size = p.i32(conv + arr_off + O.PDXARRAY_SIZE)
                row["lines"] = _arr_size
                row["lines_capacity"] = _arr_cap
                # 2026-11-21 (P2-5 / P2-4): the tool promised "deployment state and
                # its lines" and returned a bare integer.  Walk the line array once
                # and report each line's progress, the conveyor's deployment state,
                # and whether it just started a NEW batch -- a progress drop from
                # 0.991 to 0.008 is the next batch, not corruption.
                _rows, _done, _best = [], 0, 0.0
                for _ln in _arr(p, conv, arr_off):
                    _md = p.u64(_ln + 0x30 if sys.platform == "win32" else _ln + 0x28)
                    if not _md or _md < 0x10000000000:
                        continue
                    _pr = p.u32(_md + 0x60) / 1e5
                    _rows.append({"line_ptr": hex(_ln), "progress": round(_pr, 3),
                                  "done": _pr >= 0.999})
                    _best = max(_best, _pr)
                    _done += 1 if _pr >= 0.999 else 0
                row["lines_detail"] = _rows
                if len(_rows) != _arr_size:
                    row["lines_mismatch"] = {"array_size": _arr_size, "detail_count": len(_rows)}
                row["progress"] = round(_best, 3)
                row["lines_done"] = _done
                row["deployment_state"] = (
                    "no_location" if not row.get("deploy_province")
                    else "ready" if _rows and _done >= len(_rows)
                    else "training")
                _prev = _CONV_PROGRESS.get((p.pid, conv))
                if _prev is not None and _best + 0.01 < _prev and _best < 0.25:
                    row["new_batch"] = True
                    row["previous_progress"] = _prev
                _CONV_PROGRESS[(p.pid, conv)] = _best
            except Exception:
                pass
            out.append(row)
        return out

    _VP_CACHE = {}

    def deployment_province(self, state_id=None, tag=None) -> int:
        """A province inside `state_id` that can host a deployment (VP province first).

        CSetConveyorLocationCommand does NOT take a state id: its Execute bounds-checks
        the value against the game's PROVINCE count (13414), and a state id such as 64
        is refused while Berlin's province 6521 is accepted. That single wrong unit is
        why every conveyor stayed at "No location set" and 100 %-trained divisions
        never appeared on the map.
        """
        import mapdata
        st = int(state_id or self._homeland_state_id(tag))
        key = (mapdata.GAME, st)
        if key in self._VP_CACHE:
            return self._VP_CACHE[key]
        import glob, os as _os, re
        provs = sorted((mapdata._load_state_provinces_raw()[0] or {}).get(st) or [])
        best = None
        for fn in glob.glob(_os.path.join(mapdata.GAME, "history", "states", "%d-*.txt" % st)):
            try:
                txt = open(fn, encoding="utf-8", errors="replace").read()
            except OSError:
                continue
            m = re.search(r"victory_points\s*=\s*\{([^}]*)\}", txt)
            if not m:
                continue
            pairs = []
            for line in m.group(1).splitlines():
                line = line.split("#")[0].strip()
                parts = line.split()
                if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
                    pairs.append((int(parts[1]), int(parts[0])))
            if pairs:
                best = max(pairs)[1]
        if best is None:
            best = provs[0] if provs else 6521
        self._VP_CACHE[key] = best
        return best

    def _homeland_state_id(self, tag=None) -> int:
        """A deployment state that is connected to the capital by land.

        HOI4 refuses to deploy a finished division into a state that has no land
        connection to the capital: East Prussia (state 5 / 763, behind the Polish
        corridor in 1936) is the classic trap — the training conveyor fills to 100 %
        and nothing ever appears on the map. `CCountry+0xFF0` is the capital STATE
        id, so the capital's own state is always the safe answer. The hard-coded
        German list is only a fallback for the case where the capital is occupied.
        """
        p = self.p
        c = self._c(tag)
        try:
            own = {int(s["id"]) for s in self.states(c.tag)}
        except Exception:
            own = set()
        cap = 0
        if CO_CAPITAL_STATE is not None:
            try:
                cap = p.i32(c.ptr + CO_CAPITAL_STATE)
            except Exception:
                cap = 0
        if cap and (not own or cap in own):
            return cap
        if c.tag == "GER":
            # Brandenburg 64 = Berlin; then the other core states west of the corridor.
            for pref in (64, 59, 60, 52, 51, 55, 42, 57, 65):
                if pref in own:
                    return pref
        # last resort: an owned state that is not one of the two East Prussian ids
        rest = sorted(x for x in own if x not in (5, 763))
        if rest:
            return rest[0]
        return 64

    def train_divisions(self, template_name=None, lines=2, state_id=None, tag=None,
                        conveyor=None):
        """Open a division-training conveyor and push its first line onto the map.

        If `conveyor` (hex pointer or packed handle of an existing conveyor from
        `hoi4_conveyors`) is given, adds `lines` parallel training lines to THAT
        existing conveyor instead of creating a brand-new conveyor (复测 5 §3.11).

        Three things killed the 1936 run here (2 conveyors x 5 lines, 17 months,
        0 divisions):
          * the deployment state was `states("GER")[0]` = East Prussia, an exclave
            behind the Polish corridor, so a finished division could never deploy;
          * 10 parallel lines competed for ~26 rifles/day, so no single division ever
            reached 100 % equipment;
          * the conveyor ran at normal equipment priority.
        The defaults are therefore: capital state, 2 lines, priority 2 (highest),
        unlimited series. Raise `lines` only when the rifle stockpile is large.
        """
        p = self.p
        c = self._c(tag)
        from commands import COMMANDS as _CMDS
        # 2026-11-22 (复测 5 §3.11): 支持指定已有传送带指针直接加线，不再被迫新建传送带
        if conveyor is not None and str(conveyor).strip():
            want = str(conveyor).strip().lower()
            existing = self.conveyors(c.tag)
            hit = None
            for cv in existing:
                if (str(cv.get("ptr") or "").lower() == want
                        or str(cv.get("packed_handle") or "") == want
                        or hex(int(cv.get("packed_handle") or 0)).lower() == want):
                    hit = cv
                    break
            if hit is None:
                return {"ok": False, "error": "no existing conveyor matches %r" % conveyor,
                        "conveyors": [x.get("ptr") for x in existing]}
            add_n = max(1, int(lines or 1))
            lines_before = int(hit.get("lines") or 0)
            with self:
                rv = self._run_cmd(self.cmd.make("AddConveyorLine",
                                                 conveyor_handle=hit["packed_handle"],
                                                 count=add_n))
            self._settle(0.4)
            after_rows = [x for x in self.conveyors(c.tag) if x.get("ptr") == hit.get("ptr")]
            lines_after = int(after_rows[0].get("lines") or lines_before) if after_rows else lines_before
            return {
                "ok": bool(lines_after > lines_before or (rv & 0xFF)),
                "mode": "add_lines_to_existing",
                "conveyor": hit.get("ptr"),
                "conveyor_handle": hit.get("handle"),
                "template": hit.get("template"),
                "added_requested": add_n,
                "lines_before": lines_before,
                "lines_after": lines_after,
                "error": getattr(self, "_last_cmd_error", "") if lines_after <= lines_before else "",
            }
        tmpls = self.division_templates(c.tag)
        if not tmpls:
            return {"ok": False, "error": "no division templates found"}
        pick = None
        for t in tmpls:
            if template_name and (t.get("name") == template_name
                                  or str(t.get("index")) == str(template_name)):
                pick = t
                break
        if not pick:
            pick = next((t for t in tmpls if "Infanterie" in (t.get("name") or "")
                         or "Infantry" in (t.get("name") or "")), tmpls[0])
        t_ptr = int(pick["ptr"], 16)
        _, h_type, h_id, packed = self._template_handle(t_ptr)
        if h_type <= 0:
            return {"ok": False, "error": "template handle looks invalid: %d" % h_type}
        before = {x["ptr"] for x in self.conveyors(c.tag)}
        with self:
            cmd = self.cmd.make("CreateConveyor", template_handle=packed, priority=2)
            rv = self._run_cmd(cmd)
        self._settle(0.6)
        new = [x for x in self.conveyors(c.tag) if x["ptr"] not in before]
        if not new:
            return {"ok": False, "run": bool(rv & 0xFF), "template": pick.get("name"),
                    "template_handle": [h_type, h_id],
                    "error": getattr(self, "_last_cmd_error", "") or "no conveyor appeared"}
        conv = new[0]
        ch = conv["packed_handle"]
        if state_id is None or int(state_id) in (5, 763):
            state_id = self._homeland_state_id(c.tag)
        prov = self.deployment_province(state_id, tag=c.tag)
        errors = []
        with self:
            rv_loc = self._run_cmd(self.cmd.make("SetConveyorLocation",
                                                 conveyor_handle=ch, province_id=int(prov)))
            if not (rv_loc & 0xFF):
                errors.append("location: %s" % (getattr(self, "_last_cmd_error", "") or "refused"))
            if "SetConveyorPriority" in _CMDS:
                rv_pri = self._run_cmd(self.cmd.make("SetConveyorPriority",
                                                     conveyor_handle=ch, priority=2))
                if not (rv_pri & 0xFF):
                    errors.append("priority: %s" % (getattr(self, "_last_cmd_error", "") or "refused"))
            if "SetConveyorSeries" in _CMDS:
                self._run_cmd(self.cmd.make("SetConveyorSeries",
                                            conveyor_handle=ch, series=0))
            if int(lines) > 1:
                self._run_cmd(self.cmd.make("AddConveyorLine",
                                            conveyor_handle=ch, count=int(lines) - 1))
        self._settle(0.4)
        return {"ok": True, "template": pick.get("name"), "template_handle": [h_type, h_id],
                "conveyor": conv["ptr"], "conveyor_handle": conv["handle"],
                "lines": int(lines), "state_id": int(state_id),
                "deploy_province": int(prov),
                "errors": errors,
                "deploy_error": getattr(self, "_last_cmd_error", "")}

    def deploy_ready_conveyors(self, tag=None, min_progress=0.0):
        """Push a conveyor's divisions onto the map (the in-game deploy button).

        2026-11-18: this used to report `deployed: <how many conveyors it poked>`,
        which is NOT a division count. Live test: 3 conveyors of 6 lines each with
        min_progress=0.3 returned `deployed: 3` while the division count went
        30 -> 45. It also deploys the WHOLE conveyor (every line), not "the
        finished division" the old docstring claimed. Both are now reported as
        they are: `deployed` is the real DIVISION delta, `lines` says how many
        divisions each conveyor pushed, and `conveyors_deployed` keeps the old
        meaning under a name that cannot be mistaken for divisions.
        """
        def _divs():
            # 2026-11-21 (P2-3): this used to read CCountry+0x290 by hand, and the
            # 2026-11-20 playtest got divisions_before: 35 here against
            # divisions_count: 30 from hoi4_army_status in the same minute.  Use
            # the SAME verified reader army_status uses, so they cannot disagree.
            try:
                return int(self.divisions(tag, limit=1)["count"])
            except Exception:
                return None
        before = _divs()
        deployed, attempts = 0, []
        for conv in self.conveyors(tag):
            prog = None
            if min_progress > 0:
                try:
                    _, prog = self._conveyor_progress(conv)
                except Exception:
                    prog = None
                if prog is None or prog < min_progress:
                    attempts.append({"conveyor": conv["ptr"], "skipped": True,
                                     "progress": prog, "need": min_progress})
                    continue
            with self:
                rv = self._run_cmd(self.cmd.make("DeployConveyor",
                                                 conveyor_handle=conv["packed_handle"]))
            attempts.append({"conveyor": conv["ptr"], "lines": conv.get("lines"),
                             "run": bool(rv & 0xFF),
                             "error": getattr(self, "_last_cmd_error", "")})
            if rv & 0xFF:
                deployed += 1
        if deployed:
            self._settle(0.5)
        after = _divs()
        delta = (after - before) if (before is not None and after is not None) else None
        out = {"conveyors": len(attempts), "conveyors_deployed": deployed,
               "deployed": delta, "divisions_before": before, "divisions_after": after,
               "attempts": attempts,
               "note": ("DeployConveyor pushes the WHOLE conveyor (all of its lines), "
                        "not a single finished division; use min_progress to gate it")}
        if deployed and delta is not None and delta <= 0:
            out["warning"] = ("the command was accepted but no division appeared -- the "
                              "conveyor may still be waiting for equipment")
        return out

    @staticmethod
    def reset_front_snapshot():
        """删除磁盘上的 front_snapshot.json 基线（供换档/读档/换进程时调用，杜绝跨局串味）。"""
        import os
        cache = os.path.join(os.path.dirname(os.path.abspath(__file__)), "front_snapshot.json")
        try:
            if os.path.exists(cache):
                os.remove(cache)
                return True
        except Exception:
            pass
        return False

    def front_changes(self, enemy_tag=None, reset=False):
        """Province CONTROL changes since the previous call: the actual front movement.

        Uses mapdata.province_controller_map(), not state ownership. In a civil war
        (and any war before a peace treaty) occupation only changes the province
        controller; the old owner-map version reported changed_count=0 while a
        state was being overrun (2026-11-02 UK test).
        """
        import json, os, time
        import mapdata
        control = mapdata.province_controller_map(self)
        cache = os.path.join(os.path.dirname(os.path.abspath(__file__)), "front_snapshot.json")
        try:
            now_date = self.date_raw()
        except Exception:
            now_date = None
        try:
            pid = self.p.pid
        except Exception:
            pid = None
        try:
            cur_tag = self.player_tag()
        except Exception:
            cur_tag = None
        et = enemy_tag.upper() if enemy_tag else None
        prev = None
        if os.path.exists(cache) and not reset:
            try:
                with open(cache) as fh:
                    prev = json.load(fh)
            except Exception:
                prev = None
        reset_reason = None
        reset_detail = None
        if prev:
            prev_ts = prev.get("ts")
            age_s = (time.time() - float(prev_ts)) if prev_ts is not None else 0.0
            if prev.get("schema") != 2:
                reset_reason = "legacy_baseline"
            elif prev.get("pid") is None or prev.get("date") is None:
                reset_reason = "legacy_baseline"
            elif pid is not None and str(prev.get("pid")) != str(pid):
                reset_reason = "pid_changed"
            elif cur_tag and prev.get("player_tag") and str(prev.get("player_tag")).upper() != str(cur_tag).upper():
                reset_reason = "player_tag_changed"
            elif now_date is not None and prev.get("date") is not None and now_date < prev["date"]:
                reset_reason = "date_went_backwards"
            elif (
                age_s > 1800.0
                and now_date is not None
                and prev.get("date") is not None
                and abs(int(now_date) - int(prev["date"])) < 60 * 24
            ):
                # 2026-11-23 (复测 7 B-3): 同 PID 跨读档且新局恰好跑回与上一局相近日期时
                # （如上一局 1940.12 -> 读 1936 新档 -> 78 分钟后在 1940.12 首次调用），
                # 仅靠 now_date < prev["date"] 拦不住；若基线已陈旧 >30 分钟且全球差异省 >120，
                # 判定为跨档残留基线并自动重建，避免一口气报出 387 条上一局领土幽灵变动。
                po0 = prev.get("control") or {}
                raw_diff = sum(1 for k, v in control.items() if po0.get(str(k)) != v)
                if raw_diff >= 120:
                    reset_reason = "stale_cross_save_baseline"
            if reset_reason:
                reset_detail = {"reason": reset_reason,
                                "prev_pid": prev.get("pid"), "pid": pid,
                                "prev_date": prev.get("date"), "date": now_date,
                                "prev_tag": prev.get("player_tag"), "player_tag": cur_tag,
                                "prev_ts": prev.get("ts")}
                prev = None
        now = {"schema": 2, "ts": time.time(), "pid": pid, "date": now_date,
               "player_tag": cur_tag,
               "enemy_tag": et,
               "control": {str(k): v for k, v in control.items()}}
        changed = []
        if prev and prev.get("control"):
            po = prev["control"]
            for k, v in now["control"].items():
                old = po.get(k)
                if old != v:
                    changed.append({"province": int(k), "from": old, "to": v})
        try:
            with open(cache, "w") as fh:
                json.dump(now, fh)
        except Exception:
            pass
        if et:
            changed = [c for c in changed if et in ((c.get("from") or ""), (c.get("to") or ""))]
        return {
            "changed_count": len(changed),
            "changed": changed[:300],
            "provinces_seen": len(control),
            "first_run": prev is None,
            "reset_reason": reset_reason,
            "reset_detail": reset_detail,
            "pid": pid,
            "date": now_date,
            "age_seconds": round(time.time() - prev["ts"], 1) if prev else None,
        }

    # ---- PE 1.19.5.0: create a division template -------------------------
    # CCreateDivisionTemplateCommand
    #   vtable 0x2A26EF8  size 0x290  ctor 0x1C2B7D0   (pecmds.pkl ctor 29538256)
    #   CanExecute = vtable slot 9 = 0x1C2DBF0, read out of the live table: the
    #   Update command's slot 9 is its known CanExecute 0x1C2DD90, and the same
    #   index in Create's table holds 0x1C2DBF0.
    # Ctor contract, from the 1.19.4.1 disassembly recorded in commands.py:105-115
    # (the PE build keeps the same CReferencedDivisionTemplate+0x18 layering that
    # templedit.py already relies on):
    #     ctor(cmd, src CDivisionTemplate*, 0, &tag_i32, 0)
    #       -> 0x761410 deep-copies the source into cmd+0x28 (embedded)
    #       -> [tag] -> cmd+0x270, +0x280 = rdx (0), +0x288 = [tag]
    # The copy keeps the SOURCE name, so the embedded std::string at
    # (cmd+0x28)+0x08 is rewritten before posting -- same trick as the Linux path.
    PE_CREATE_CTOR   = 0x1C2B7D0
    PE_CREATE_VT     = 0x2A26EF8
    PE_CREATE_CANEXE = 0x1C2DBF0
    PE_CREATE_SIZE   = 0x290

    def _pe_create_template_cost(self, cmd):
        """Army XP the engine will charge for this create command.

        0x1C2BD70(cmd, &out) writes the cost into out; CanExecute then does
        `if (army_xp < cost) return false`.  Measured 2026-11-18 on a fresh
        1936 save: Infanterie-Division 8,000,000 / Gebirgs-Brigade 3,000,000 /
        Kavallerie-Brigade 3,000,000 — i.e. 80 / 30 / 30 army XP at 100,000 per
        XP.  It is a COST, not a count, which is exactly why CanExecute looked
        like it "always returns 0": every previous test ran with 0 army XP.
        """
        import cmdwin
        p = self.p
        buf = cmdwin.alloc(self, 0x40)
        try:
            p.write(buf, b"\0" * 0x40)
            self.call(p.base + 0x1C2BD70, (cmd, buf), absolute=True, force=True)
            return int(p.u64(buf))
        finally:
            try:
                cmdwin.free(self, buf)
            except Exception:
                pass

    def _pe_army_xp(self, tag=None):
        """Country army XP in the engine's 1/100000 display units."""
        import templedit
        c = self._c(tag)
        return int(templedit._pe_army_xp(self, int(c.tag_id)))

    def _pe_duplicate_template(self, source_name, tag=None, new_name=None, dry_run=False):
        """Create a new template from an existing one on the PE build.

        2026-11-23 (D-6): 支持 dry_run=True 零消耗询价（实扣 XP 随源模板营数/支援连动态变化），
        并补齐 cmdwin.attach_fake_vtable 防止队列释放命令对象时 CRT free 崩进程。
        """
        import cmdwin
        p = self.p
        c = self._c(tag)
        rows = self.division_templates(c.tag)
        src_ref = None
        for i, r in enumerate(rows):
            if str(source_name) in (str(i), str(r.get("name"))):
                src_ref = int(r["ptr"], 16)
                break
        if src_ref is None:
            return {"ok": False, "error": "template not found: %s" % source_name,
                    "have": [r.get("name") for r in rows]}
        before = len(rows)
        target = new_name or (source_name + " Copy")
        cmd = cmdwin.alloc(self, self.PE_CREATE_SIZE)
        p.write(cmd, b"\0" * self.PE_CREATE_SIZE)
        tagbuf = cmdwin.alloc(self, 0x10)
        p.write(tagbuf, struct.pack("<i", int(c.tag_id)) + b"\0" * 12)
        try:
            self.call(p.base + self.PE_CREATE_CTOR,
                      (cmd, src_ref + 0x18, 0, tagbuf, 0),
                      absolute=True, force=True)
        except Exception as e:
            try:
                cmdwin.free(self, tagbuf)
            except Exception:
                pass
            return {"ok": False, "stage": "ctor", "cmd": hex(cmd),
                    "error": "%s: %s" % (type(e).__name__, e)}
        try:
            cmdwin.free(self, tagbuf)
        except Exception:
            pass
        if p.u64(cmd) != p.base + self.PE_CREATE_VT:
            return {"ok": False, "stage": "ctor", "cmd": hex(cmd),
                    "error": "ctor wrote vptr %#x, expected %#x"
                             % (p.u64(cmd), p.base + self.PE_CREATE_VT)}
        # rename the embedded copy (MSVC std::string: 16-byte SSO/ptr union at +0,
        # size at +0x10, capacity at +0x18 -> 32 bytes total)
        na = cmd + 0x28 + 0x08
        nb = target.encode("utf-8")
        if len(nb) <= 15:
            p.write(na, nb.ljust(16, b"\0") + struct.pack("<QQ", len(nb), 15))
        else:
            sb = cmdwin.alloc(self, len(nb) + 1)
            p.write(sb, nb + b"\0")
            p.write(na, struct.pack("<QQQQ", sb, 0, len(nb), len(nb)))
        cost = self._pe_create_template_cost(cmd)
        xp = self._pe_army_xp(c.tag)
        can = bool(self.call(p.base + self.PE_CREATE_CANEXE, (cmd, 0),
                             absolute=True, force=True) & 0xFF)
        res = {"ok": False, "mode": "pe-create-division-template",
               "cmd": hex(cmd), "source": source_name, "new_name": target,
               "dry_run": bool(dry_run),
               "can_execute": can, "before": before,
               "xp_cost": cost, "xp_cost_display": round(cost / 100000.0, 2),
               "army_xp": xp, "army_xp_display": round(xp / 100000.0, 2),
               "xp_after_estimate": round(max(0.0, (xp - cost) / 100000.0), 2),
               "hint": (
                   "XP 费用由源模板当前营与支援连数量动态决定；如需在复制后改编，"
                   "可先对源模板调 hoi4_template_edit(template=%r, ..., dry_run=true) 预估改编差价，"
                   "确保总 XP 足够再执行。" % source_name
               )}
        if not can:
            if cost > xp:
                # The whole "CanExecute always returns 0" mystery, solved
                # 2026-11-18: this is an ordinary resource gate, not a wall.
                res["blocked_by"] = "army_xp"
                res["error"] = ("creating this template costs %.1f army XP, "
                                "you only have %.1f"
                                % (cost / 100000.0, xp / 100000.0))
                res["hint"] = ("army XP comes from national focuses, an army "
                               "chief with experience_gain_army, and combat — "
                               "hoi4_thresholds shows the pool")
            else:
                res["error"] = ("CCreateDivisionTemplateCommand::CanExecute "
                                "refused although the XP cost is covered "
                                "(%.1f <= %.1f) — unexpected, report it"
                                % (cost / 100000.0, xp / 100000.0))
            res["after"] = len(self.division_templates(c.tag))
            return res
        if dry_run:
            res["ok"] = True
            res["note"] = "dry_run: validated and quoted XP cost only; no command posted"
            return res
        cmdwin.attach_fake_vtable(self, cmd, self.PE_CREATE_VT)
        posted = cmdwin.post_drained(self, cmd, seconds=2.0)
        self._settle(0.3)
        after = self.division_templates(c.tag)
        res.update({"posted": posted, "after": len(after),
                    "templates": [r.get("name") for r in after],
                    "xp_after": self._pe_army_xp(c.tag),
                    "ok": len(after) > before})
        if not res["ok"]:
            res["error"] = "no new template appeared after posting"
        return res

    def duplicate_division_template(self, source_name, tag=None, new_name=None, dry_run=False):
        """Task 2 (2026-11-02 work order): borrow/copy an existing CDivisionTemplate.

        CCreateDivisionTemplateCommand ctor 0x1FDC190 deep-copies the source template
        (embedded at CReferencedDivisionTemplate+0x18) into the command and posts it
        through CCommand::Run -- the exact path the UI 'Duplicate' button uses.
        """
        # 2026-11-17: KILLED A LIVE GAME. The comment below (2026-11-10) said this
        # path was "reverted / stays disabled" but the code was still reachable, so
        # a real campaign called it and hoi4.exe died inside CCreateDivisionTemplateCommand
        # ctor (0x1FDC190) -- process vanished, no crash folder, ReadProcessMemory
        # returned 299 (ERROR_PARTIAL_COPY) inside self.call(). Everything after that
        # call in the session read "hoi4 process not found".
        # Hard-refuse on the PE build until the CDivisionTemplate /
        # CReferencedDivisionTemplate layering is actually verified by disassembly.
        if sys.platform == "win32":
            return self._pe_duplicate_template(source_name, tag, new_name, dry_run=dry_run)
        import struct as _s
        c = self._c(tag)
        arr = c.ptr + 0x1B8                      # CPdxArray<CReferencedDivisionTemplate*>
        d = self.p.u64(arr)
        n = min(self.p.i32(arr + 0x0C), 64)
        src_ref = None
        for i in range(n):
            t = self.p.u64(d + i * 8)
            nm = item_name(self.p, t + 0x20)
            if nm == source_name:
                src_ref = t
                break
        if src_ref is None:
            return {"ok": False, "error": "template not found: %s" % source_name,
                    "have": [item_name(self.p, self.p.u64(d + i * 8) + 0x20)
                             for i in range(n)]}
        # 2026-11-10 test: src_ref+0x18 crashed the ctor (sig=6 at 0x1FDC190);
        # reverted until the real CDivisionTemplate/CReferencedDivisionTemplate
        # layering is confirmed. CCreateDivisionTemplateCommand stays disabled.
        src_inner = src_ref + 0x18
        target_name = new_name or (source_name + " Copy")
        import api
        api.allow_call(0x1FDC190)
        with self:
            cmd = self.cmd.new(0x290)
            self.p.write(cmd, b"\0" * 0x290)
            tagbuf = self.malloc(16)
            try:
                self.p.write(tagbuf, _s.pack("<i", c.tag_id) + b"\0" * 12)
                self.call(0x1FDC190, (cmd, src_inner, 0, tagbuf, 0))
                # V2-01: the deep-copied template keeps the source name, so
                # CanExecute rejects the duplicate. Rewrite the embedded
                # std::string at (cmd+0x28)+0x08 before running.
                na = cmd + 0x28 + 0x08
                nb = target_name.encode("utf-8")
                if len(nb) <= 15:
                    self.p.write(na, _s.pack("<QQ", na + 0x10, len(nb)) + nb.ljust(16, b"\0"))
                else:
                    sb = self.malloc(len(nb) + 1)
                    self.p.write(sb, nb + b"\0")
                    self.p.write(na, _s.pack("<QQQ", sb, len(nb), len(nb) + 1))
                rv = self._run_cmd(cmd)
            finally:
                self.free(tagbuf)
        self._settle()
        arr2 = c.ptr + 0x1B8
        n2 = self.p.i32(arr2 + 0x0C)
        names = [item_name(self.p, self.p.u64(self.p.u64(arr2) + i * 8) + 0x20)
                 for i in range(min(n2, 64))]
        return {"ok": bool(rv & 0xFF) or n2 > n, "run": bool(rv & 0xFF),
                "new_name": target_name,
                "templates_before": n, "templates_after": n2,
                "templates": names,
                "error": getattr(self, "_last_cmd_error", "")}

    def template_composition(self, tag=None, only=None):
        """Division templates with their real battalion/company makeup."""
        p = self.p
        c = self._c(tag)
        out = []
        # 2026-11-16: the old key_at() probed every 8-byte slot for a
        # libstdc++-shaped std::string. On the PE build CSubUnitDefinition uses
        # an MSVC std::string (SSO buffer +0x18 / size +0x28), so every name
        # came back empty. templedit.subunit_name() now dispatches by platform
        # and handles TNullObject placeholder slots (size 0).
        import templedit

        def counts(tp, arr_off):
            hdr = p.try_read(tp + arr_off, 16)
            if not hdr:
                return {}
            data, cap, size = struct.unpack("<Qii", hdr)
            acc = {}
            for k in range(min(size, 60)):
                e = p.try_read(data + 8 * k, 8)
                if not e:
                    break
                ep = struct.unpack("<Q", e)[0]
                kk = templedit.subunit_name(p, ep)
                if not kk:
                    continue          # TNullObject / empty column slot
                acc[kk] = acc.get(kk, 0) + 1
            return acc

        for i, t in enumerate(_arr(p, c.ptr, CO_DIV_TEMPLATES)):
            name = item_name(p, t + DT_NAME)
            if only is not None and str(only) not in (str(i), str(name)):
                continue
            out.append({
                "index": i,
                "name": name,
                "line": counts(t, 0x68),
                "support": counts(t, 0x88),
                "ptr": hex(t),
            })
        return out

    def subunit_types(self, tag=None, filter_text=""):
        """Battalion types, used when building a division template.

        2026-11-10: CCountry+0x26C0 (CO_SUBUNITS) is an EMPTY array in 1.19.4.1 —
        this tool returned count 0 for every country. Fall back to the live
        CSubUnitDefinition pool (vtable scan anchored on a real template).
        """
        p = self.p
        c = self._c(tag)
        out = []
        for s in _arr(p, c.ptr, CO_SUBUNITS):
            k = item_name(p, s + SU_KEY)
            if k and (not filter_text or filter_text.lower() in k.lower()):
                out.append({"key": k, "category": item_name(p, s + SU_CATEGORY), "ptr": hex(s)})
        if not out:
            try:
                import templedit
                pool = templedit.live_pool(self)
                for k, ptr in sorted(pool.items()):
                    if k and (not filter_text or filter_text.lower() in k.lower()):
                        out.append({"key": k, "category": None, "ptr": hex(ptr),
                                    "source": "live_pool"})
            except Exception:
                pass
        return out

    def theatres(self, tag=None):
        p = self.p
        return [{"ptr": hex(t), "class": self.oi.class_of(t)}
                for t in _arr(p, self._c(tag).ptr, CO_THEATRES)]

    # ============================================================ DONANMA
    def fleets(self, tag=None):
        p = self.p
        out = []
        c = self._c(tag)
        for i, f in enumerate(_arr(p, c.ptr, CO_FLEETS)):
            row = {"index": i, "ptr": hex(f), "name": None}
            if sys.platform == "win32":
                # PE 1.19.5: CFleet+0xE0 is a char* to the fleet name (measured:
                # "Kriegsmarine", "Unterseeboots-Flotte", "德意志国第1舰队").
                # +0xA8 CCountry*, +0xD0 CNavyTheaterGroup*, +0xD8 home CProvince*,
                # +0xC0 CPdxArray<CTaskForce*> (size at +0xCC).
                try:
                    np = p.u64(f + 0xE0)
                    row["name"] = p.cstr(np) if np else None
                except Exception:
                    pass
                try:
                    row["country"] = hex(p.u64(f + 0xA8) or 0)
                    row["navy_group"] = hex(p.u64(f + 0xD0) or 0)
                    row["home_port"] = hex(p.u64(f + 0xD8) or 0)
                    row["task_force_count"] = p.i32(f + 0xB8 + 0x0C)
                except Exception:
                    pass
            else:
                row["name"] = item_name(p, f + FL_NAME)
            out.append(row)
        return out

    # ============================================================ AIR
    def air_wings(self, tag=None, refresh=False, show_all=False, limit=0):
        """All CAirWing instances in live memory.

        Run 11 (work order 1107): the old version kept only the wings whose first
        0x200 bytes contained the raw CCountry* pointer. CAirWing does not store
        that pointer, so every wing in the game was filtered out and the air force
        never took part in a single run. Ownership is now left to the command
        layer: CAttachAirWingToArmyCommand::CanExecute refuses a foreign wing in
        C++ and returns false instead of corrupting anything.
        """
        if refresh:
            self.oi.build(force=True)
        wings = self.oi.instances("CAirWing")
        # 2026-11-08: the vtable scan returned every object TWICE, 0x10 apart
        # (434 hits for ~217 wings) because a CAirWing carries the same vtable
        # at +0x00 and at +0x10. Keep only the lower address of each pair, and
        # require a readable handle at +0x18 so a stray vtable word in data
        # cannot be mistaken for a wing.
        import struct as _st
        raw = sorted(set(wings))
        hs = set(raw)
        wings = [w for w in raw if (w - 0x10) not in hs]
        good = []
        for w in wings:
            try:
                _t, _i = _st.unpack("<ii", self.p.read(w + 0x18, 8))
            except Exception:
                continue
            if 0 < _i < 100000:
                good.append(w)
        # 2026-11-16 (PE): airops.scan_wings() scans the LINUX vtable
        # 0x440E200, which matches nothing on hoi4.exe -- that is why this
        # tool reported 0 wings while 280 real CAirWing objects existed.
        # airops_win reads them through the MSVC RTTI index instead.
        if sys.platform == "win32":
            try:
                import airops_win as AW
                me = self._c(tag).tag
                wings = AW.all_wings(self, refresh=refresh)
                owned = AW.country_wings(self, me, wings=wings)
                types = {}
                for w in wings:
                    types[w["type"]] = types.get(w["type"], 0) + 1
                # 2026-11-22 (P2-2): 仅当显式传 all=True 时返回全世界统计；
                # 传 tag="ITA" 等非玩家国家时一律通过 country_wings(self, me) 精确过滤。
                if show_all:
                    # 2026-11-16: the old code dumped all 401 wings on every
                    # call (~15k tokens). Default is now the player's own
                    # block; the whole world is opt-in and capped.
                    cap = int(limit or 80)
                    rows = []
                    for w in wings[:cap]:
                        row = {"ptr": hex(w["ptr"]), "id": w["handle"] >> 32,
                               "handle": hex(w["handle"]), "type": w["type"],
                               "sa": hex(w["sa"])}
                        row.update(AW.wing_state(self, w["ptr"]))
                        rows.append(row)
                    return {"count": len(wings),
                            "groups": len(AW.wing_groups(wings)),
                            "types": types, "returned": len(rows),
                            "truncated": len(wings) > len(rows),
                            "ownership": "global census (all=true)",
                            "wings": rows}
                owned_rows = owned.get("wings") or []
                cap = int(limit or 0)
                ret_rows = owned_rows[:cap] if cap > 0 else owned_rows
                return {"tag": me,
                        "count": owned.get("count", 0),
                        "returned": len(ret_rows),
                        "truncated": len(owned_rows) > len(ret_rows),
                        "total_in_world": len(wings),
                        "groups_in_world": len(AW.wing_groups(wings)),
                        "ownership": "CStrategicAir+0x90 = CCountryTag id "
                                     "(exact: CAirWing+0x38 -> pool+0x40 -> sa)",
                        "scope": "%s air force only: %d wings owned through "
                                 "CStrategicAir+0x90; pass all=true for the "
                                 "world census" % (me, owned.get("count", 0)),
                        "types": _count_by(owned_rows, "type"),
                        "wings": ret_rows}
            except Exception as e:
                return {"count": len(good), "raw_hits": len(raw),
                        "scan_error": repr(e),
                        "wings": [{"ptr": hex(w)} for w in good[:300]]}
        # --- Linux path (unchanged) ---
        try:
            import airops
            rich = airops.scan_wings(self)
            types = {}
            for w in rich:
                types[w["type"]] = types.get(w["type"], 0) + 1
            return {"count": len(rich), "raw_hits": len(raw), "types": types,
                    "wings": [{"ptr": hex(w["ptr"]), "id": w["id"],
                               "type": w["type"], "pool": hex(w["pool"]),
                               "sa": hex(w["sa"])} for w in rich[:300]]}
        except Exception as e:
            return {"count": len(good), "raw_hits": len(raw), "scan_error": repr(e),
                    "wings": [{"ptr": hex(w)} for w in good[:300]]}

    def set_air_missions(self, tag=None, fighter_mask=1, cas_mask=2):
        """Give the player's own wings a sensible default mission.

        2026-11-17: nothing in the whole stack ever set an air mission -- the 1940
        campaign ended with all 15 wings at `mission: 0` since 1936.  Fighters go
        to Air Superiority (1), CAS to Close Air Support (2); transports and naval
        bombers are left alone.  One command per type, no ctor calls.
        """
        if sys.platform != "win32":
            return {"ok": False, "error": "set_air_missions is the Windows path"}
        import airops_win as AW
        try:
            rows = AW.german_wings(self).get("wings") or []
        except Exception as e:
            return {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}
        out = []
        # 2026-11-17: this used to cover only `fighter` and `cas`, so the seven
        # tac_bomber wings and the naval bomber were never given a mission at all.
        # (A later revision also ran a "repair" pass for wings whose +0xA4 was 0,
        # on the theory that it was the enable bit -- it is not; see _light_alerts.)
        groups = [("fighter", int(fighter_mask)), ("cas", int(cas_mask)),
                  ("tac_bomber", int(cas_mask)), ("nav_bomber", 16)]
        wanted = {}
        for typ, mask in groups:
            for r in rows:
                if r.get("type") == typ:
                    wanted[int(r["handle"], 16)] = mask
        # repair: mask present but the wing is not actually flying
        by_mask = {}
        for h, m in wanted.items():
            by_mask.setdefault(m, []).append(h)
        for mask, handles in sorted(by_mask.items()):
            try:
                cmd = AW.make_set_mission(self, handles, mask, 1, 1)
                if not AW.can_exec(self, cmd, AW.CANEXEC_SET_MISSION):
                    out.append({"mask": mask, "ok": False, "wings": len(handles),
                                "error": "CStratAirSetMissionCommand::CanExecute refused"})
                    continue
                AW.post(self, cmd)
                # 2026-11-21 (续测 P1-8): the mask alone never makes a wing fly.
                # +0xA4 is a PERSISTENT enable flag (measured: written 1, still 1
                # after 1.8 s) and nothing in this tool ever set it, so five
                # fighters sat at mission=1 / mission_on=false through peace AND
                # through a declared war.  airops.enable_mission() cannot be used
                # here: ENABLE_MISSION_CTOR is a Linux ELF offset.  Set the flag
                # directly -- the engine keeps it (same file, WING_MISSION_ON).
                try:
                    import struct as _st8
                    for _w in (AW.german_wings(self).get("wings") or []):
                        if (_w.get("mission") or 0) == mask and _w.get("ptr"):
                            self.p.write(int(_w["ptr"], 16) + 0xA4, _st8.pack("<i", 1))
                except Exception as _e8:
                    out.append({"mask": mask, "enable_error": repr(_e8)[:120]})
                out.append({"mask": mask, "ok": True, "wings": len(handles)})
            except Exception as e:
                out.append({"mask": mask, "ok": False,
                            "error": "%s: %s" % (type(e).__name__, e)})
        # read back: a wing with no mask at all is the only thing we can prove
        # (see the +0xA4 measurement note in _light_alerts; it is not an enable bit)
        try:
            after = AW.german_wings(self).get("wings") or []
            still = [{"wing": r["id"], "type": r.get("type"),
                      "mask": r.get("mission")} for r in after
                     if r.get("type") != "transport_plane"
                     and not r.get("mission")]
        except Exception:
            still = None
        return {"ok": any(x.get("ok") for x in out), "results": out,
                "still_grounded": still}

    def attach_air_wings_to_armies(self, tag=None, refresh_oi=False,
                                   allow_unverified=False, wings=None):
        """Attach the player's air wings to the player's armies (round-robin).

        2026-11-17: the Windows build had NO working path here at all.  The body
        below still used the 1.19.4.1 Linux constants (ctor 0x241F840, Run
        0x20AE0A0) AND refused by default because wing ownership could not be
        proven -- so `attach_air_wings_to_armies()` returned
        `attached: 0, scanned: 656` every single tick of the 1940 campaign and the
        Luftwaffe never flew.  (Calling that ctor on PE would also have executed
        garbage at image_base+0x241F840.)

        The PE build has a verified command builder instead:
        `airops_win.make_attach` -> CAttachAirWingToArmyCommand
        (vt 0x2A97728, size 0x48, CanExecute 0x19D3920, +0x28 = wing handle
        array, +0x40 = army handle).  Ownership is proven by `german_wings()`
        (the deterministic id-1..k starting block), so only the player's own
        aircraft are ever handed to the command.
        """
        import api, armyops
        if getattr(self, "_air_attach_broken", False):
            return {"ok": False, "skipped": "circuit_breaker"}
        c = self._c(tag)
        ogs = [og for og in armyops.orders_groups(self, c)
               if not self.p.u8(og + armyops.OG_IS_GROUP)]
        if not ogs:
            return {"ok": False, "error": "no armies (COrdersGroup) to attach air wings to"}

        if sys.platform == "win32":
            import airops_win as AW
            _tag = c.tag
            # 2026-11-22 (P3-4): 实机证实 COrdersGroup+0x70 是 CPdxArray<CAirWing*>，
            # 直接从游戏内存读取当前已挂载映射，彻底消除读档/重启后缓存过期导致的假失败。
            def _live_wing_army_map(og_list):
                m = {}
                for _og in og_list:
                    try:
                        for _wp in _arr(self.p, _og, 0x70):
                            if _wp:
                                m[int(_wp)] = int(_og)
                    except Exception:
                        continue
                return m

            def _run_pass(do_refresh):
                ger = AW.country_wings(self, _tag, refresh=bool(do_refresh))
                if not ger.get("ok"):
                    return {"ok": False, "attached_count": 0, "scanned_wings": 0,
                            "error": ger.get("error") or "no player air-wing block"}
                rows = ger["wings"]
                if wings:
                    keep = {i for i, _h in AW.select_wings(self, wings)}
                    rows = [r for r in rows if r["id"] in keep]
                live_map = _live_wing_army_map(ogs)
                attached, refused = [], []
                done = getattr(self, "_air_attached", None)
                if done is None:
                    done = self._air_attached = set()
                _unattachable = ("nav_bomber", "naval_bomber", "strat_bomber",
                                 "strategic_bomber", "maritime_patrol_plane", "transport_plane")
                unattached_idx = 0
                for r in rows:
                    w = int(r["ptr"], 16)
                    cur_og = live_map.get(w)
                    if cur_og is not None:
                        # 内存实证已挂在我方某陆军集团军上，幂等直接通过，不做无谓改挂与注入
                        attached.append({
                            "wing": r["id"], "type": r["type"],
                            "army": hex(cur_og), "already_attached": True,
                            "verified_from_memory": True,
                        })
                        done.add((w, cur_og))
                        continue
                    if r.get("type") in _unattachable:
                        refused.append({"wing": r["id"], "type": r["type"],
                                        "reason": "wing_type_unsupported"})
                        continue
                    og = ogs[unattached_idx % len(ogs)]
                    unattached_idx += 1
                    try:
                        hw = self.p.u64(w + 0x18)          # packed CAirWing handle
                        hog = self.p.u64(og + 0x08)        # packed COrdersGroup handle
                        if not hw or not hog:
                            refused.append({"wing": r["id"], "reason": "stale_handle"})
                            continue
                        cmd = AW.make_attach(self, [hw], hog)
                        if not AW.can_exec(self, cmd, AW.CANEXEC_ATTACH):
                            refused.append({
                                "wing": r["id"], "type": r["type"],
                                "army": hex(og), "reason": "can_execute_refused",
                            })
                            continue
                        res = AW.post(self, cmd)
                        done.add((w, og))
                        attached.append({"wing": r["id"], "type": r["type"],
                                         "army": hex(og), "posted": res.get("posted")})
                    except Exception as e:
                        refused.append({"wing": r["id"], "reason": "error",
                                        "error": "%s: %s" % (type(e).__name__, e)})
                return {"ok": bool(attached), "backend": "windows",
                        "scanned_wings": len(rows), "attached_count": len(attached),
                        "refused": len(refused), "attached": attached,
                        "refused_detail": refused[:12]}

            res_out = _run_pass(refresh_oi)
            # 若首次全部拒绝且未显式刷新索引，清本地缓存并 refresh=True 重试一次自愈
            if not res_out.get("ok") and not refresh_oi and res_out.get("scanned_wings", 0) > 0:
                self._air_attached = set()
                res_retry = _run_pass(True)
                res_retry["retried_after_refresh"] = True
                return res_retry
            return res_out
        wings_all = self.oi.instances("CAirWing")
        if not wings_all and refresh_oi:
            self.oi.build(force=True)
            wings_all = self.oi.instances("CAirWing")
        if not wings_all:
            return {"ok": False, "error": "no CAirWing in the object index",
                    "scanned_wings": 0, "attached_count": 0}
        wings = wings_all
        if not allow_unverified:
            return {"ok": False, "skipped": "wing ownership unverified",
                    "scanned_wings": len(wings), "attached_count": 0,
                    "note": "see the 2026-11-07 canary: no owner field found in "
                            "CAirWing/CAirWingPool; the command accepts foreign wings"}
        already = getattr(self, "_attached_wings_set", None)
        if already is None or getattr(self, "_attached_wings_pid", None) != self.p.pid:
            already = set()
            self._attached_wings_set = already
            self._attached_wings_pid = self.p.pid
        api.allow_call(0x241F840)
        api.allow_call(0x20AE0A0)
        attached, refused = [], 0
        try:
            with self:
                errbuf = self.malloc(0x28)
                for w in wings:
                    if w in already:
                        continue
                    if self.oi.class_of(w) != "CAirWing":
                        continue
                    og = ogs[len(attached) % len(ogs)]
                    cmd = self.cmd.new(0x48)
                    self.p.write(cmd, b"\0" * 0x48)
                    self.call(0x241F840, (cmd, w, og, 0))
                    # C1 (review 2026-11-07): errbuf is a libstdc++ std::string.
                    # Writing 0x20 zero bytes set _M_p = NULL, so the moment
                    # CCommand::Run wrote a refusal reason libstdc++ dereferenced
                    # NULL -> SIGSEGV. A valid SSO empty string is
                    # _M_p = this+0x10, _M_string_length = 0, _M_local_buf[0] = 0.
                    self.p.write(errbuf, struct.pack("<QQB", errbuf + 0x10, 0, 0))
                    rv = self.call(0x20AE0A0, (cmd, errbuf))
                    if rv & 0xFF:
                        already.add(w)
                        attached.append({"wing": hex(w), "army": hex(og)})
                    else:
                        refused += 1
        except Exception as e:
            self._air_attach_broken = True
            return {"ok": False, "error": str(e), "attached_count": len(attached),
                    "scanned_wings": len(wings), "refused": refused}
        return {"ok": bool(attached) or bool(already),
                "scanned_wings": len(wings),
                "attached_count": len(attached),
                "refused": refused,
                "total_attached": len(already),
                "attached": attached[:40]}

    def _best_front_segment(self, segs, divisions=None):
        """Pick the front segment closest to our own divisions.

        2026-11-20: the old rule was `max(segs, key=len(border_provinces))`.
        After Belgium was annexed that picked the Belgian Congo border with
        French Equatorial Africa -- the longest border on the map -- and sent
        four divisions to state 977 instead of the French border.  The walk is
        land-only, so an overseas front stays at 9999 and is chosen only when
        we really have nothing closer.
        """
        import collections
        import mapdata
        import unitorders
        starts = []
        for d in (divisions or []):
            try:
                pv = unitorders.province_of(self, d)
            except Exception:
                pv = None
            if pv:
                starts.append(pv)
        dist = {}
        if starts:
            adj = mapdata.build_adjacency()
            dist = {s: 0 for s in starts}
            q = collections.deque(starts)
            while q:
                u = q.popleft()
                for v in adj.get(u, ()):
                    if v not in dist:
                        dist[v] = dist[u] + 1
                        q.append(v)
        best, best_key = None, None
        for s in segs:
            if dist:
                d = min((dist.get(p, 9999) for p in s["border_provinces"]), default=9999)
            else:
                d = 0
            key = (d, -len(s["border_provinces"]))
            if best_key is None or key < best_key:
                best, best_key = s, key
        return best or (segs[0] if segs else None)

    def plan_front(self, enemy_tag, tag=None, armies=None, split=True, depth=2,
                   force=False, wait_seconds=0, dry_run=False):
        """Native battle plan with ONE FRONT LINE PER ARMY (2026-11-17).

        Why this exists
        ---------------
        `_win_battleplans` merged every division into a single NEW army and drew a
        single front line over the whole CFront.  In the 1940 campaign that is
        exactly how the German army became one 141-division blob: the engine
        spread it over a handful of border provinces (over-stacked, out of supply,
        attacks refused) while the other theatre stood empty and the French walked
        in.  A player never does that -- a player draws one front line per army.

        This keeps the armies that already exist and gives each one a contiguous
        slice of the front (`from`/`to` are 0..100000 of the section) plus its own
        offensive line, so the engine's own front logic spreads each army along
        ITS slice.  That is what stops the stacking.
        """
        import time
        if sys.platform != "win32":
            return {"ok": False, "error": "plan_front is the Windows native-plan path"}
        import armyops_win as A
        import armyops
        p = self.p
        enemy_tag = str(enemy_tag or "").upper()
        if not enemy_tag:
            return {"ok": False, "error": "no enemy tag"}
        co = self._c(tag)
        out = {"enemy_tag": enemy_tag, "backend": "windows", "steps": {}}
        # 2026-11-18：画前线是和平时期的动作，不再要求「已开战」。旧门禁逼着调用方
        # 先宣战再画线，正好把事情做反了 —— 1939.07.30 那局就是这么在 8 个师、
        # 0 条命令的状态下开打的。`force` 保留它原来的另一个含义：跳过补给闸。
        try:
            enemies = (self.wars(tag) or {}).get("enemies") or []
        except Exception:
            enemies = []
        out["at_war"] = enemy_tag in enemies

        segs = A.fronts_towards(self, enemy_tag, co.tag)
        if not segs:
            return {"ok": False, "error": "no front section faces %s" % enemy_tag}
        # 2026-11-17: the supply guard the Linux path always had. A battle plan
        # commits whole armies to a front; if that front cannot feed them the
        # engine spreads them anyway and every division degrades at once --
        # exactly the "stacked, no supply, cannot attack" state of 1940-07.
        # Instead of refusing (the old behaviour) we only commit as many armies as
        # the front can actually feed, biggest first.
        guard = self._supply_front_guard(enemy_tag, tag)
        out["steps"]["supply"] = guard
        try:
            _cand_divs = [int(d["ptr"], 16) for d in (self.divisions(tag) or {}).get("divisions", [])]
        except Exception:
            _cand_divs = []
        best = self._best_front_segment(segs, _cand_divs)
        sub_from = int(best.get("slice_from", 0) or 0)
        sub_to = int(best.get("slice_to", A.FULL) or A.FULL)
        if sub_to <= sub_from:
            sub_from, sub_to = 0, A.FULL
        out["steps"]["front"] = {"handle": best["front_handle"],
                                 "section": best["section"],
                                 "border": len(best["border_provinces"]),
                                 "slice_range": [sub_from, sub_to],
                                 "sections_facing_enemy": len(segs)}
        targets = A.target_provinces(self, enemy_tag, depth=1, own_tag=co.tag)
        out["steps"]["targets"] = targets[:20]
        if not targets:
            return {"ok": False, "error": "no enemy province %d steps in" % depth,
                    "front": out["steps"]["front"]}

        all_og = [og for og in armyops.orders_groups(self, co)
                  if not p.u8(og + A.OG_IS_GROUP)]
        if armies:
            want = {int(a, 16) if isinstance(a, str) else int(a) for a in armies}
            all_og = [og for og in all_og if og in want]
        else:
            # 2026-11-17: never strip another front. An army that already carries a
            # front order belongs to a different war; only armies with no orders are
            # freely available.
            # 2026-11-23 (复测 10 A-4 根治): 当所有集团军都已有前线订单（free=[]）时，
            # 严禁把所有军（例如正守 FRA 马奇诺防线的 6 个军）一并抓来重排！只允许重排
            # 现有前线订单**本来就面向本敌国 enemy_tag** 的集团军；若没有任何空闲军也
            # 没有任何面向 enemy_tag 的军，明确拒绝并请调用方显式传 armies=[...]。
            busy = []
            for og in list(all_og):
                try:
                    if A.orders(self, og):
                        busy.append(og)
                except Exception:
                    pass
            free = [og for og in all_og if og not in busy]
            out["steps"]["armies_with_existing_orders"] = [hex(o) for o in busy]
            # 2026-11-23 (复测 11 P1-2 加固): 即使集团军当前没有前线订单（free，例如刚
            # reset_battleplans 后或手工驻防在 FRA 马奇诺边境的 6 个军），只要其师实站
            # 在其他邻国边境且不贴着本敌国 enemy_tag 边境，绝不能被 set_front_directive(enemy_tag)
            # 一把抽去给 HOL/BEL 画切片与进攻线！
            guarded_other = []
            eligible_free = list(free)
            if free:
                try:
                    import mapdata as _md_fr
                    import unitorders as _uo_fr
                    _owners_fr, _ = _md_fr.province_owner_map(self)
                    _adj_fr = _md_fr.build_adjacency()
                    _, _ptype_fr = _md_fr.load_definitions()
                    _me_fr = str(co.tag).upper()
                except Exception:
                    _owners_fr, _adj_fr, _ptype_fr, _me_fr = {}, {}, {}, str(co.tag).upper()
                if _owners_fr and _adj_fr:
                    _keep_free = []
                    for og in free:
                        _touch_target = 0
                        _touch_other = set()
                        _other_div_n = 0
                        try:
                            for _dp in A.divisions_of(self, og):
                                _pv = _uo_fr.province_of(self, _dp)
                                if _pv is None:
                                    continue
                                _nbs = set()
                                for _nb in _adj_fr.get(int(_pv), ()):
                                    if _ptype_fr and _ptype_fr.get(_nb) != "land":
                                        continue
                                    _nbo = _owners_fr.get(_nb)
                                    if _nbo and _nbo != _me_fr:
                                        _nbs.add(str(_nbo).upper())
                                if enemy_tag in _nbs:
                                    _touch_target += 1
                                elif _nbs:
                                    _other_div_n += 1
                                    _touch_other.update(_nbs)
                        except Exception:
                            pass
                        # 只要该军完全不贴着 enemy_tag 边境，且至少 1 个师明确驻防在他国边境，
                        # 视为守他国边境的驻防军（例如正对 FRA 马奇诺的军），禁止自动抽调。
                        if _touch_target == 0 and _other_div_n > 0:
                            guarded_other.append({
                                "army": hex(og),
                                "bordering": sorted(_touch_other),
                                "border_divisions": _other_div_n,
                            })
                        else:
                            _keep_free.append(og)
                    eligible_free = _keep_free
                if guarded_other:
                    out["steps"]["free_armies_guarding_other_borders"] = guarded_other
            if eligible_free:
                all_og = eligible_free
            elif busy:
                border_set = set(best.get("border_provinces") or [])
                best_fh = tuple(best.get("front_handle") or ())
                best_sec = int(best.get("section") or -1)
                facing_enemy = []
                busy_summary = []
                for og in busy:
                    og_ords = []
                    try:
                        og_ords = A.orders(self, og) or []
                    except Exception:
                        og_ords = []
                    matches_enemy = False
                    for _ord in og_ords:
                        if _ord.get("type") != 2:
                            continue
                        # 检查订单自身的己方边境省 (+0x70) 是否落在本敌国边境集合内
                        _op = int(_ord["ptr"], 16) if isinstance(_ord.get("ptr"), str) else int(_ord.get("ptr") or 0)
                        _oprovs = []
                        if _op > 0x10000:
                            try:
                                _d0 = p.u64(_op + 0x70)
                                _n0 = p.i32(_op + 0x7C)
                                if 0x10000 < _d0 < 0x7F0000000000 and 0 < _n0 < 512:
                                    _oprovs = [p.i32(_d0 + 4 * _k) for _k in range(_n0)]
                            except Exception:
                                _oprovs = []
                        if _oprovs and border_set:
                            # 至少一半或 >=2 个省落在目标敌国边境省上，避免纯交界点误抓马奇诺军
                            hit_n = sum(1 for pv in _oprovs if pv in border_set)
                            if hit_n >= 2 or (hit_n > 0 and hit_n * 2 >= len(_oprovs)):
                                matches_enemy = True
                                break
                        elif (tuple(_ord.get("root_front") or ()) == best_fh
                              and int(_ord.get("root_section") or -1) == best_sec):
                            # 无省列表时的回退：必须切片区间与本敌国子区间显著重叠
                            _of = int(_ord.get("from", 0) or 0)
                            _ot = int(_ord.get("to", A.FULL) or A.FULL)
                            _ov = max(0, min(_ot, sub_to) - max(_of, sub_from))
                            _span = max(1, _ot - _of)
                            if _ov * 2 >= _span:
                                matches_enemy = True
                                break
                    busy_summary.append({
                        "army": hex(og),
                        "faces_enemy": matches_enemy,
                        "orders": [o.get("id") for o in og_ords],
                    })
                    if matches_enemy:
                        facing_enemy.append(og)
                out["steps"]["busy_armies_enemy_check"] = busy_summary
                if facing_enemy:
                    all_og = facing_enemy
                    out["steps"]["armies_note"] = (
                        "re-planning %d army(s) already facing %s (isolated from %d army(s) on other fronts)"
                        % (len(facing_enemy), enemy_tag, len(busy) - len(facing_enemy))
                    )
                else:
                    return {
                        "ok": False,
                        "enemy_tag": enemy_tag,
                        "blocked_by": "no_armies_facing_enemy",
                        "error": (
                            "all %d army(s) already hold front orders on other borders and none faces %s; "
                            "refusing to strip other fronts (e.g. Maginot/FRA). Pass explicit armies=[...] "
                            "to redeploy specific armies."
                            % (len(busy), enemy_tag)
                        ),
                        "front": out["steps"]["front"],
                        "busy_armies": busy_summary,
                    }
            elif guarded_other:
                return {
                    "ok": False,
                    "enemy_tag": enemy_tag,
                    "blocked_by": "free_armies_guarding_other_borders",
                    "error": (
                        "all %d free army(s) are physically stationed on other borders (%s) and none "
                        "borders %s; refusing to strip other borders (e.g. Maginot/FRA). Pass explicit "
                        "armies=[...] to commit specific armies."
                        % (
                            len(guarded_other),
                            ", ".join(sorted({t for x in guarded_other for t in x.get("bordering", [])})) or "other",
                            enemy_tag,
                        )
                    ),
                    "front": out["steps"]["front"],
                    "free_armies_guarding_other_borders": guarded_other,
                }
        sizes = {}
        for og in all_og:
            try:
                sizes[og] = len(A.divisions_of(self, og))
            except Exception:
                sizes[og] = 0
        all_og = [og for og in all_og if sizes.get(og)]
        if not all_og:
            return {"ok": False, "error": "no army with divisions"}

        # --- supply cap: only commit what this front can feed ------------------
        cap = guard.get("capacity") or 0
        if cap and not force:
            total = sum(sizes[og] for og in all_og)
            if total > cap:
                keep, running = [], 0
                for og in sorted(all_og, key=lambda o: -sizes[o]):
                    if running + sizes[og] <= cap:
                        keep.append(og)
                        running += sizes[og]
                if not keep:                      # nothing fits: send the smallest
                    keep = [min(all_og, key=lambda o: sizes[o])]
                held_list = [o for o in all_og if o not in keep]
                # 2026-11-23 (复测 6 P2-2): 补齐被留作预备队集团军的详细位置与面向邻国，
                # 不再只返回裸指针列表导致无法判断剩余师是否正在守卫其他边境（如法德线）。
                held_detail = []
                kept_detail = []
                try:
                    import mapdata as _md_st
                    import unitorders as _uo_st
                    _owners_st, _ = _md_st.province_owner_map(self)
                    _adj_st = _md_st.build_adjacency()
                    _, _ptype_st = _md_st.load_definitions()
                    _me_u = str(co.tag).upper()
                except Exception:
                    _owners_st, _adj_st, _ptype_st, _me_u = {}, {}, {}, str(co.tag).upper()

                def _army_loc_info(og_ptr):
                    provs = []
                    bordering = set()
                    try:
                        for dptr in A.divisions_of(self, og_ptr):
                            pv = _uo_st.province_of(self, dptr)
                            if pv is not None:
                                provs.append(int(pv))
                                if _owners_st and _adj_st:
                                    for nb in _adj_st.get(pv, ()):
                                        if _ptype_st and _ptype_st.get(nb) != "land":
                                            continue
                                        nbo = _owners_st.get(nb)
                                        if nbo and nbo != _me_u:
                                            bordering.add(nbo)
                    except Exception:
                        pass
                    ord_rows = []
                    try:
                        for _or in (A.orders(self, og_ptr) or []):
                            ord_rows.append({
                                "id": _or.get("id"),
                                "type": _or.get("type"),
                                "assigned": _or.get("assigned_divisions"),
                                "root_front": _or.get("root_front"),
                            })
                    except Exception:
                        ord_rows = []
                    has_ord = bool(ord_rows)
                    from collections import Counter as _Cnt
                    top_p = [pv for pv, _ in _Cnt(provs).most_common(6)]
                    return {
                        "army": hex(og_ptr),
                        "divisions": int(sizes.get(og_ptr, 0)),
                        "has_orders": has_ord,
                        "orders": ord_rows,
                        "provinces_sample": top_p,
                        "bordering": sorted(bordering),
                    }

                for _o in held_list:
                    held_detail.append(_army_loc_info(_o))
                for _o in keep:
                    kept_detail.append(_army_loc_info(_o))
                # 2026-11-23 (复测 9 E-2): 明确列出被留作预备队但仍挂着旧前线订单的集团军，
                # 防止调用方对这些军再调 hoi4_deploy_to_border 导致部队在两条边境来回拉扯。
                held_keep_orders = [
                    {
                        "army": hd["army"],
                        "divisions": hd["divisions"],
                        "orders": hd["orders"],
                        "bordering": hd["bordering"],
                    }
                    for hd in held_detail
                    if hd.get("has_orders")
                ]
                trim_obj = {
                    "capacity": cap, "would_commit": total,
                    "committed": sum(sizes[o] for o in keep),
                    "held_back_divisions": sum(sizes[o] for o in held_list),
                    "armies_kept": [hex(o) for o in keep],
                    "armies_kept_detail": kept_detail,
                    "armies_held_back": [hex(o) for o in held_list],
                    "armies_held_back_detail": held_detail,
                    "held_back_armies_keep_orders": held_keep_orders,
                    "note": "the rest stay in reserve instead of stacking a "
                            "front that cannot supply them",
                }
                if held_keep_orders:
                    trim_obj["warning"] = (
                        "有 %d 支被留作预备队的集团军仍保留原有前线订单（未做改动）：%s。"
                        "若对它们的师执行 hoi4_deploy_to_border 逐师搬运，引擎会按旧前线把部队拉回原边境；"
                        "如需真换防，请先用 hoi4_delete_order 撤掉旧前线订单或显式传 armies=[...] 指定上线集团军。"
                        % (
                            len(held_keep_orders),
                            ", ".join(
                                "%s(%d师, orders=%s)"
                                % (x["army"], x["divisions"], [o["id"] for o in x["orders"]])
                                for x in held_keep_orders[:4]
                            ),
                        )
                    )
                out["steps"]["supply_trim"] = trim_obj
                all_og = keep

        n = len(all_og)
        plan = []
        span = max(1, sub_to - sub_from)
        for i, og in enumerate(all_og):
            p_from = sub_from if not split else int(sub_from + span * i / n)
            p_to = sub_to if not split else int(sub_from + span * (i + 1) / n)
            plan.append({"army": hex(og), "divisions": sizes[og],
                         "from": p_from,
                         "to": p_to})
        out["steps"]["plan"] = plan
        out["steps"]["split"] = bool(split)
        if dry_run:
            out["ok"] = True
            out["dry_run"] = True
            return out

        idler = p.u64(p.base + 0x33DDB08)
        was_paused = bool(idler and p.u8(idler + 0x6C1))
        out["resumed_for_plan"] = was_paused
        if was_paused:
            p.write(idler + 0x6C1, b"\x00")
        try:
            def wait(fn, seconds=8.0):
                t0 = time.time()
                while time.time() - t0 < seconds:
                    v = fn()
                    if v:
                        return v
                    time.sleep(0.25)
                return None

            results = []
            for i, og in enumerate(all_og):
                frm, to = plan[i]["from"], plan[i]["to"]
                divs = A.divisions_of(self, og)
                # 2026-11-20: the ONLY plan shape the engine actually feeds is
                # the one built by CompletePlan (0x18CFC70).  A front order made
                # by draw_front_line + execute_plan/manual_offensive gets a
                # type-1 child whose +0x210 array stays empty forever: the arrow
                # is drawn and never moves a division.  Measured on Austria
                # 1937-04: 17 divisions on the front, 16 game days, ZERO
                # province changes even against an enemy with 0 divisions.
                # CompletePlan builds front+arrow in one engine call and the
                # engine hands the divisions to the child (measured 17/17), and
                # that plan took 17 Austrian provinces.
                def _order_matches_slice(ord_row):
                    if ord_row.get("type") != 2:
                        return False
                    if int(ord_row.get("root_section") or -1) != int(best["section"]):
                        return False
                    # 2026-11-23 (复测 11 P1-2): 多国共享同一 CFront+section（如 FRA/BEL/HOL）时，
                    # 必须校验订单区间与目标敌国区间 [sub_from, sub_to] 显著重叠，绝不把 FRA 段
                    # 前线订单错当 HOL 段复用！
                    of = int(ord_row.get("from", 0) or 0)
                    ot = int(ord_row.get("to", A.FULL) or A.FULL)
                    if ot <= of:
                        return True
                    ov = max(0, min(ot, sub_to) - max(of, sub_from))
                    return ov * 2 >= (ot - of)

                reused = [o for o in A.orders(self, og) if _order_matches_slice(o)]
                root_id = 0
                fresh = []
                info = {}
                ok, err, ok2, err2, ok3, err3 = True, "", True, "", True, ""
                flags = []
                assign_fix = None
                dropped = None
                if reused:
                    rid = reused[0]["id"]
                    kids0 = [c for c in A.order_children(self, og)
                             if c["root"] == rid
                             and c["type"] == A.ORDER_TYPE_OFFENSIVE
                             and (c.get("assigned") or 0) > 0]
                    if kids0:
                        root_id = rid
                        fresh = [reused[0]]
                        out.setdefault("steps", {}).setdefault(
                            "reused_orders", []).append(
                                {"army": hex(og), "order": rid})
                    else:
                        # A front order with no fed arrow is the dead shape the
                        # 1937 Austrian test exposed.  Drop it and rebuild.
                        dr = self.delete_order(og, rid)
                        dropped = {"army": hex(og), "order": rid,
                                   "ok": dr.get("ok"), "error": dr.get("error"),
                                   "reason": "front order had no fed offensive child"}
                if not fresh:
                    ok, err, info = A.complete_plan(
                        self, og, best["front"], targets,
                        section=best.get("section"), frm=frm, to=to,
                        divisions=divs)
                    oid = int((info or {}).get("order_id") or 0)
                    fresh = [o for o in A.orders(self, og)
                             if o["id"] == oid] if oid else []
                    root_id = oid
                if not fresh:
                    results.append({"army": hex(og), "from": frm, "to": to,
                                    "divisions": len(divs), "ok": False,
                                    "method": "complete_plan",
                                    "error": err or "no front order appeared"})
                    continue
                # CompletePlan assigns the army's divisions to the root itself;
                # top up only when the root is still empty (a reused order that
                # lost its divisions, or a refused engine assign).
                # 2026-11-23 (复测 7 B-6): 区分本军指派师数（root_assigned）与引擎
                # order+0x210 上的共享前线总师数（root_total_assigned）。
                _abr = A.assigned_breakdown(self, og, root_id) if hasattr(A, "assigned_breakdown") else None
                assigned = (_abr["army_assigned"] if isinstance(_abr, dict) else A.assigned_of(self, og, root_id))
                root_total_assigned = (_abr["front_total_assigned"] if isinstance(_abr, dict) else assigned)
                if (assigned or 0) == 0:
                    try:
                        assign_fix = self.assign_divisions_to_front(
                            og, enemy_tag, mode="all", order_ids=[root_id])
                    except Exception as e:
                        assign_fix = {"ok": False, "error": repr(e)}
                    self._settle(0.5)
                    _abr = A.assigned_breakdown(self, og, root_id) if hasattr(A, "assigned_breakdown") else None
                    assigned = (_abr["army_assigned"] if isinstance(_abr, dict) else A.assigned_of(self, og, root_id))
                    root_total_assigned = (_abr["front_total_assigned"] if isinstance(_abr, dict) else assigned)
                kids = [c for c in A.order_children(self, og)
                        if c["root"] == root_id
                        and c["type"] == A.ORDER_TYPE_OFFENSIVE]
                # The engine feeds the child on the first simulation tick; a
                # short grace period is enough and keeps the old API honest.
                if not any((c.get("assigned") or 0) > 0 for c in kids):
                    deadline = time.time() + 4.0
                    while time.time() < deadline:
                        time.sleep(0.25)
                        kids = [c for c in A.order_children(self, og)
                                if c["root"] == root_id
                                and c["type"] == A.ORDER_TYPE_OFFENSIVE]
                        if any((c.get("assigned") or 0) > 0 for c in kids):
                            break
                time.sleep(0.3)
                flags = A.start_offensives(self, og)
                kids = [c for c in A.order_children(self, og)
                        if c["root"] == root_id
                        and c["type"] == A.ORDER_TYPE_OFFENSIVE]
                kids_fed = [c for c in kids if (c.get("assigned") or 0) > 0]
                # An empty child under an EMPTY root is the 0xB2E62E hazard;
                # under a root that holds divisions it is the normal post-draw
                # state and must be kept (deleting it throws the arrow away).
                purged = []
                for c in kids:
                    if (c.get("assigned") or 0) > 0:
                        continue
                    if (assigned or 0) > 0:
                        continue
                    dr = self.delete_order(og, c["id"])
                    purged.append({"child": c["id"], "ok": dr.get("ok"),
                                   "error": dr.get("error")})
                if purged:
                    out.setdefault("steps", {}).setdefault(
                        "purged_empty_children", []).extend(
                            [dict(p, army=hex(og), root=root_id) for p in purged])
                    kids = [c for c in kids if (c.get("assigned") or 0) > 0]
                if (assigned or 0) == 0:
                    dr = self.delete_order(og, root_id)
                    dropped = {"army": hex(og), "order": root_id,
                               "ok": dr.get("ok"), "error": dr.get("error")}
                    out.setdefault("steps", {}).setdefault(
                        "dropped_empty_orders", []).append(dropped)
                results.append({"army": hex(og), "from": frm, "to": to,
                                "divisions": len(divs),
                                "front_order": root_id,
                                "front_ok": bool(ok), "front_error": err,
                                "offensive_ok": bool(ok2), "offensive_error": err2,
                                "children": [k["id"] for k in kids],
                                "children_fed": [k["id"] for k in kids_fed],
                                "children_executing": [k["id"] for k in kids
                                                       if k.get("executing") == 1],
                                "execute_ok": bool(ok3), "execute_error": err3,
                                "root_assigned": assigned,
                                "root_total_assigned": root_total_assigned,
                                "offensive_assigned": min(
                                    len(divs),
                                    sum(k.get("assigned") or 0 for k in kids_fed),
                                ),
                                "offensive_total_assigned": sum(
                                    k.get("assigned") or 0 for k in kids_fed
                                ),
                                "assign_fix": assign_fix,
                                "dropped_order": dropped,
                                "flags": flags,
                                "method": "complete_plan",
                                # 2026-11-20: ok means the engine really fed the
                                # arrow and it is switched on -- the 1937 test
                                # had every other box ticked while the front
                                # stood still for 16 days.
                                "ok": (bool(fresh) and bool(kids_fed)
                                       and (assigned or 0) > 0
                                       and any(k.get("executing") == 1
                                               for k in kids_fed))})
            out["steps"]["armed"] = results
            out["steps"]["children"] = [(c["id"], c["assigned"], c["executing"])
                                        for og in all_og for c in A.order_children(self, og)]
            # 2026-11-18: 全军的空子命令清扫。只清"本次处理过的军"不够 ——
            # 已经有订单的军会被 plan 跳过，它们身上攒下来的空 type-1 子命令
            # 恰恰就是 0xB2E62E 崩溃的实体，必须一起清掉。
            swept = []
            for og in all_og:
                try:
                    kids_all = A.order_children(self, og)
                except Exception:
                    continue
                for c in kids_all:
                    if c["type"] == A.ORDER_TYPE_OFFENSIVE and (c.get("assigned") or 0) == 0:
                        # 2026-11-19: divisions live on the parent front order;
                        # the engine feeds the arrow at execution time.  A child
                        # at 0 under a front order that DOES hold divisions is the
                        # normal post-draw state -- sweeping it deletes the arrow
                        # we just made.  Only orphans are the 0xB2E62E hazard.
                        try:
                            if (A.assigned_of(self, og, c.get("root")) or 0) > 0:
                                continue
                        except Exception:
                            pass
                        dr = self.delete_order(og, c["id"])
                        swept.append({"army": hex(og), "child": c["id"],
                                      "root": c.get("root"),
                                      "ok": dr.get("ok"), "error": dr.get("error")})
            out["steps"]["swept_empty_children"] = swept
            # 2026-11-21 (P1-7): the sweeps above only touch `all_og` -- the armies
            # THIS plan committed.  An army that was filtered out (it had no
            # divisions) can still carry a type-2 front order from an earlier run,
            # and that is exactly the 0-division order the 2026-11-20 playtest
            # found locking the clock (`blocked_by: "empty_orders"`).  Look at
            # every army of the country and say so; drop a childless empty front
            # outright (the safe case) and leave the rest to hoi4_purge_orders,
            # which knows how to clear children first.
            empty_fronts, dropped_fronts = [], []
            try:
                for og in [o for o in armyops.orders_groups(self, co)
                           if not p.u8(o + A.OG_IS_GROUP)]:
                    try:
                        rows = A.orders(self, og)
                    except Exception:
                        continue
                    for o in rows:
                        if o.get("type") != 2:
                            continue
                        if int(o.get("assigned_divisions")
                               or o.get("assigned") or 0) > 0:
                            continue
                        try:
                            kids = [c for c in A.order_children(self, og)
                                    if c.get("root") == o["id"]]
                        except Exception:
                            kids = []
                        rec = {"army": hex(og), "order": o["id"],
                               "children": len(kids)}
                        if not kids:
                            dr = self.delete_order(og, o["id"])
                            rec["deleted"] = bool(dr.get("ok"))
                            rec["error"] = dr.get("error")
                            dropped_fronts.append(rec)
                        else:
                            empty_fronts.append(rec)
            except Exception as e:
                out["steps"]["empty_front_sweep_error"] = repr(e)
            if dropped_fronts:
                out["steps"]["dropped_empty_fronts"] = dropped_fronts
            if empty_fronts:
                out["steps"]["empty_front_orders"] = empty_fronts
                out["warning"] = (
                    "%d 条前线订单 assigned=0 且带子命令，解暂停会被安全阀拦下"
                    "（hoi4.exe+0xB2E62E 崩溃点）——先跑 hoi4_purge_orders。"
                    % len(empty_fronts))
            out["ok"] = bool(results) and all(r["ok"] for r in results)
            if wait_seconds:
                import mapdata
                enemy_provs = set(sum(A.owned_provinces(self, enemy_tag).values(), []))
                t0 = time.time()
                gains = 0
                while time.time() - t0 < float(wait_seconds):
                    cm = mapdata.province_controller_map(self)
                    gains = sum(1 for x in enemy_provs
                                if cm.get(x) == (co.tag if hasattr(co, "tag") else self.player_tag()))
                    if gains:
                        break
                    time.sleep(5)
                out["steps"]["advance"] = {"waited": round(time.time() - t0, 1),
                                           "enemy_provinces_taken": gains,
                                           "date": self.date()["text"]}
            return out
        finally:
            if was_paused and idler:
                p.write(idler + 0x6C1, b"\x01")

    def _supply_front_guard(self, enemy_tag, tag=None, ratio=1.25):
        """How many divisions this front can feed vs how many we would commit.

        Called BEFORE the battle plan is drawn. A plan commits a whole army to a
        front line, and the engine then distributes it over the border provinces
        of that front; if that is more than the front can supply, every division
        on it degrades at once. Returns the numbers either way; the caller
        decides (force=True).
        """
        try:
            import staff_supply as _ss
            fb = _ss.front_budget(self, enemy_tag, tag)
            need = self.divisions(tag, limit=1)["count"]
            cap = fb.get("capacity") or 0
            ok = (not cap) or (need <= cap * ratio)
            return {"ok": ok, "needed": need, "capacity": cap,
                    "ratio": round(need / cap, 2) if cap else None,
                    "per_province": fb.get("per_province"),
                    "border_provinces": len(fb.get("border_provinces") or [])}
        except Exception as e:
            return {"ok": True, "error": repr(e)}

    def _win_battleplans(self, enemy_tag, tag=None, force=False, wait_seconds=0,
                         armies=None, split=True):
        """activate_army_battleplans for hoi4.exe: the chain stage/wden.py proved live.

        create_army -> front line (CFront section facing the enemy) -> offensive
        line under THAT order -> execute -> set the executing flag.

        2026-11-17: when the country already HAS armies this now delegates to
        `plan_front`, which draws one front line per army instead of shoving every
        division into one new army (see plan_front's docstring for the 1940
        post-mortem).  The old merge-everything code stays only as the fallback
        for a country that has divisions but no armies at all.

        Two things this learned the hard way:
          * the engine only hands the divisions to the offensive child while the
            simulation is RUNNING.  Building the plan on a paused game produces a
            child with `assigned == 0` and nothing ever moves, so the method
            unpauses for the duration and restores the pause flag afterwards;
          * "posted" is not "created".  Every step is polled and read back.
        """
        import time
        import armyops_win as A
        p = self.p
        enemy_tag = str(enemy_tag).upper()
        out = {"enemy_tag": enemy_tag, "backend": "windows", "steps": {}}

        # 2026-11-17: per-army front lines whenever the country already has armies.
        import armyops as _ao
        try:
            _co = self._c(tag)
            _have = [og for og in _ao.orders_groups(self, _co)
                     if not p.u8(og + A.OG_IS_GROUP)
                     and len(A.divisions_of(self, og)) > 0]
        except Exception:
            _have = []
        if _have or armies:
            res = self.plan_front(enemy_tag, tag, armies=armies, split=split,
                                  force=force, wait_seconds=wait_seconds)
            res["mode"] = "per-army-front-lines"
            return res

        # 2026-11-18: 同 plan_front —— 和平时期画线正是这条路径存在的理由。
        try:
            out["at_war"] = enemy_tag in ((self.wars(tag) or {}).get("enemies") or [])
        except Exception:
            out["at_war"] = None

        idler = p.u64(p.base + 0x33DDB08)
        was_paused = bool(idler and p.u8(idler + 0x6C1))
        out["resumed_for_plan"] = was_paused
        if was_paused:
            p.write(idler + 0x6C1, b"\x00")
        try:
            segs = A.fronts_towards(self, enemy_tag, tag)
            out["steps"]["fronts"] = [{"handle": s["front_handle"], "section": s["section"],
                                       "border": len(s["border_provinces"])} for s in segs]
            if not segs:
                out.update({"ok": False, "error": "no front section faces %s" % enemy_tag})
                return out
            try:
                _cand_divs = [int(d["ptr"], 16) for d in (self.divisions(tag) or {}).get("divisions", [])]
            except Exception:
                _cand_divs = []
            best = self._best_front_segment(segs, _cand_divs)
            targets = A.target_provinces(self, enemy_tag, depth=1, own_tag=tag)
            out["steps"]["targets"] = targets[:20]
            if not targets:
                out.update({"ok": False, "error": "no enemy province two steps in"})
                return out

            divs = [int(d["ptr"], 16) for d in (self.divisions(tag) or {}).get("divisions", [])]
            if not divs:
                out.update({"ok": False, "error": "no divisions"})
                return out
            # One group only if every division already lives in the same one;
            # otherwise make a new army (this is what stage/wden.py did -- the
            # 1936 save has 4 divisions in an army and 26 floating).
            ogs = {p.u64(d + A.CARMY_OG) for d in divs}
            og = next(iter(ogs)) if len(ogs) == 1 and 0 not in ogs else 0
            if not og:
                ok, err = A.create_army(self, divs)
                out["steps"]["create_army"] = {"ok": ok, "error": err}
                if not ok:
                    out.update({"ok": False, "error": err})
                    return out
                og = p.u64(divs[0] + A.CARMY_OG)
            out["steps"]["orders_group"] = hex(og)
            out["steps"]["divisions"] = len(A.divisions_of(self, og))

            def wait(fn, seconds=6.0):
                t0 = time.time()
                while time.time() - t0 < seconds:
                    v = fn()
                    if v:
                        return v
                    time.sleep(0.25)
                return None

            handle = tuple(best["front_handle"])
            roots = [o for o in A.orders(self, og)
                     if o["type"] == 2 and tuple(o.get("root_front") or ()) == handle]
            if roots:
                root_id = roots[-1]["id"]
                out["steps"]["front_line"] = {"ok": True, "reused": root_id}
            else:
                before = {o["id"] for o in A.orders(self, og)}
                # 2026-11-19: COrderNewRootCommand's raw ctor killed the process
                # six times; the engine's own CompletePlan command builds the
                # same front order and never crashed (3/3 live).
                ok, err, info = A.complete_plan(
                    self, og, best["front"], targets,
                    section=best.get("section"), divisions=divs)
                oid = int((info or {}).get("order_id") or 0)
                fresh = [o for o in A.orders(self, og) if o["id"] == oid] if oid else \
                    wait(lambda: [o for o in A.orders(self, og)
                                  if o["id"] not in before and o["type"] == 2])
                out["steps"]["front_line"] = {
                    "ok": bool(ok and fresh), "error": err,
                    "new_orders": [o["id"] for o in (fresh or [])],
                    "method": "complete_plan"}
                if not fresh:
                    out.update({"ok": False,
                                "error": err or "no front order appeared"})
                    return out
                root_id = fresh[-1]["id"]
                a_ok, a_err = A.assign_to_order(self, og, root_id,
                                                [int(d) for d in divs])
                out["steps"]["front_assign"] = {
                    "ok": bool(a_ok), "error": a_err,
                    "assigned": A.assigned_of(self, og, root_id)}

            kids = [c for c in A.order_children(self, og)
                    if c["root"] == root_id and c["type"] == A.ORDER_TYPE_OFFENSIVE]
            if kids:
                out["steps"]["offensive_line"] = {"ok": True, "reused": [k["id"] for k in kids]}
            else:
                # 2026-11-19: the type-1 child comes from the engine's order
                # factory; COrderNewFrontCommand::Execute bails for front orders
                # this tool built (0x10B8620 returns an empty split list).
                m_ok, m_err, _optr = A.manual_offensive(self, og, root_id, targets)
                kids = wait(lambda: [c for c in A.order_children(self, og)
                                     if c["root"] == root_id
                                     and c["type"] == A.ORDER_TYPE_OFFENSIVE])
                out["steps"]["offensive_line"] = {
                    "ok": bool(m_ok and kids), "error": m_err,
                    "children": [k["id"] for k in (kids or [])]}
                if not kids:
                    out.update({"ok": False,
                                "error": m_err or "no offensive child appeared"})
                    return out

            # 2026-11-17: order_id=0 made this a NO-OP (see armyops_win.execute_plan).
            ok, err, _cmd = A.execute_plan(self, og, root_id, 1)
            time.sleep(0.5)
            out["steps"]["execute"] = {"ok": ok, "error": err,
                                       "order": root_id,
                                       "assigned": A.assigned_of(self, og, root_id)}

            # THE GREEN TICK is the ARROW: the type-2 offensive line carries its
            # own +0x248.  COrderExecuteCommand only sets that byte when it is
            # given an order id AND flag=1 (ctor: [+0x30]=id, [+0x34]=flag,
            # 0x18C8D24), and our helper passes flag=0 -- i.e. `execute(id)` is
            # the toggle OFF.  So set it through the engine's setter instead.
            # Note the delay: the divisions need ~2 game days of planning before
            # the first province falls (measured 2026-11-15, research 70.6).
            arrows = [c for c in A.order_children(self, og)
                      if c["root"] == root_id and c["type"] == 2]
            if arrows:
                arrow_id = arrows[-1]["id"]
                byte = A.set_order_executing(self, arrows[-1]["ptr"], True)
                out["steps"]["activate"] = {"ok": byte == 1, "order": arrow_id}
            out["steps"]["flags"] = A.start_offensives(self, og)
            out["steps"]["children"] = [(c["id"], c["assigned"], c["executing"])
                                        for c in A.order_children(self, og)]
            out["ok"] = all(c[2] == 1 for c in out["steps"]["children"]
                            if c[1] > 0) and bool(out["steps"]["children"])

            if wait_seconds:
                # How long the plan takes to bite.  Measured 2026-11-15 at speed 1:
                # the first enemy province changes hands ~10-11 GAME DAYS after the
                # plan is built -- the divisions have to redeploy onto the front
                # line first.  Every "it never advances" report before that was a
                # window that was simply too short (research 70.6).
                import mapdata
                enemy_provs = set(sum(A.owned_provinces(self, enemy_tag).values(), []))
                t0 = time.time()
                start = None
                gains = 0
                while time.time() - t0 < float(wait_seconds):
                    cm = mapdata.province_controller_map(self)
                    gains = sum(1 for x in enemy_provs if cm.get(x) == (tag or self.player_tag()))
                    if start is None:
                        start = time.time()
                    if gains:
                        break
                    time.sleep(5)
                out["steps"]["advance"] = {
                    "waited": round(time.time() - t0, 1),
                    "enemy_provinces_taken": gains,
                    "date": self.date()["text"],
                }
            return out
        finally:
            if was_paused and idler:
                p.write(idler + 0x6C1, b"\x01")

    def activate_army_battleplans(self, enemy_tag, tag=None, force=False,
                                  wait_seconds=0, armies=None, split=True):
        """One-shot: unassigned divisions -> armies -> front line -> offensive line -> execute.

        2026-11-08 (work order 1108 §3.2). Replaces the run-13 version, which
        assumed an army (COrdersGroup) already existed. A fresh 1936 Germany has
        30 divisions and ZERO armies (`CArmy+0xC0 == 0` everywhere), so the plan
        could never be drawn. This version builds the armies first.

        The Clausewitz command queue is asynchronous: `RUN_CMD` only posts, the
        game main thread executes on its next frame. Therefore every step must
        `detach` (which `create_army`/`draw_front_line`/... already do) and then
        be POLLED from outside the attach session. Never sleep inside `with self:`.
        """
        import time
        if sys.platform == "win32":
            return self._win_battleplans(enemy_tag, tag, force, wait_seconds,
                                         armies=armies, split=split)
        import armyops
        p = self.p
        co = self._c(tag)
        enemy_tag = str(enemy_tag).upper()
        out = {"enemy_tag": enemy_tag, "steps": {}}
        DRAW_ARROW = True   # 2026-11-08: ctor OE2 fixed (0x2332310) + non-NULL darr

        # ---- step -1: supply guard -------------------------------------------
        guard = self._supply_front_guard(enemy_tag, tag)
        out["supply_guard"] = guard
        if not guard.get("ok") and not force:
            out.update({"ok": False, "blocked_by": "supply",
                        "error": ("this front can feed about %s divisions but the plan would "
                                  "commit %s. A battle plan puts a whole army on the front and "
                                  "the engine spreads it over only those border provinces, so "
                                  "everything on it would degrade together. Narrow the front, "
                                  "deploy to a different border, or pass force=true."
                                  % (guard.get("capacity"), guard.get("needed")))})
            return out

        # ---- step 0: build armies out of the unassigned pool -------------------
        # 2026-11-10: paratroopers are exempt from every automatic land order.
        # A parachute division waiting at its air base looked exactly like an
        # idle division, was collected into a new army and given a front line.
        import divreserve as _dr
        unassigned = [d for d in self.unassigned_divisions(tag)
                      if not _dr.is_paradrop_division(self, d)[0]]
        ogs = [og for og in armyops.orders_groups(self, co)
               if not p.u8(og + armyops.OG_IS_GROUP)]
        _keep, _skip = [], []
        for og in ogs:
            try:
                _pd, _why = _dr.army_is_paradrop(self, og)
            except Exception:
                _pd, _why = False, None
            if _pd:
                _skip.append({"army": hex(og), "reason": _why})
            else:
                _keep.append(og)
        ogs = _keep
        out["steps"]["skipped_paradrop_armies"] = _skip
        out["steps"]["before"] = {"unassigned": len(unassigned), "armies": len(ogs)}

        if not ogs and unassigned:
            created = []
            while unassigned:
                batch, unassigned = unassigned[:24], unassigned[24:]
                before_set = set(armyops.orders_groups(self, co))
                r = self.create_army(batch, tag=tag)
                got = None
                for _ in range(20):
                    time.sleep(0.15)
                    now = set(armyops.orders_groups(self, co))
                    if len(now) > len(before_set):
                        got = (now - before_set).pop()
                        break
                created.append({"divisions": len(batch), "ok": r.get("ok"),
                                "error": r.get("error"), "army": hex(got) if got else None})
            out["steps"]["create_army"] = created
            ogs = [og for og in armyops.orders_groups(self, co)
                   if not p.u8(og + armyops.OG_IS_GROUP)]

            # commanders: one per new army, never the same man twice
            try:
                gens = [x for x in self.available_generals(tag).get("generals", [])
                        if x.get("leader_ptr")]
                corps = [x for x in gens if x.get("rank") == "corps_commander"] or gens
                used = {p.u64(o + armyops.OG_LEADER) for o in ogs
                        if p.u64(o + armyops.OG_LEADER)}
                led = []
                for og in ogs:
                    if p.u64(og + armyops.OG_LEADER):
                        continue
                    cand = next((x for x in corps
                                 if int(x["leader_ptr"], 16) not in used), None)
                    if not cand:
                        break
                    lp = int(cand["leader_ptr"], 16)
                    ok_l, err_l = armyops.set_army_leader(self, og, lp)
                    time.sleep(0.15)
                    if ok_l:
                        used.add(lp)
                        led.append({"army": hex(og), "leader": cand.get("name")})
                out["steps"]["leaders"] = led
            except Exception as e:
                out["steps"]["leaders"] = {"error": str(e)}

        if not ogs:
            out.update({"ok": False, "error": "no armies after create_army attempt",
                        "unassigned_count": len(self.unassigned_divisions(tag)),
                        "hint": "the game must be UNPAUSED so CCommandQueue drains"})
            return out

        out["steps"]["after"] = {"armies": len(ogs),
                                 "sizes": [p.i32(o + armyops.OG_DIVISIONS + 0xC) for o in ogs]}

        # ---- step 1: the CFront facing the enemy -------------------------------
        segs = armyops.fronts_towards(self, enemy_tag, co.tag)
        if not segs:
            out.update({"ok": False, "error": "no CFront facing %s" % enemy_tag,
                        "armies": len(ogs)})
            return out
        uniq, seen = [], set()
        for s in segs:
            if s["front"] not in seen:
                seen.add(s["front"])
                uniq.append(s)
        target_provs = armyops.target_line(self, enemy_tag, depth="full", own_tag=co.tag)
        out["steps"]["fronts"] = {"sections": len(segs), "unique_fronts": len(uniq),
                                  "target_provinces": len(target_provs)}
        if not target_provs:
            out.update({"ok": False, "error": "empty offensive target line"})
            return out

        results = []
        for idx, og in enumerate(ogs):
            s = uniq[idx % len(uniq)]
            root_orders = [o for o in armyops.orders_of(self, og) if o["type"] == 2]
            if not root_orders:
                try:
                    # 2026-11-08 (run 17): the divisions MUST be handed to the root
                    # front order AT CREATION. Drawing with `None` produces an order
                    # with assigned_divisions == 0, and COrderAssignCommand cannot
                    # top it up afterwards (verified live: assign_to_order returns
                    # True and the order keeps 0). With the army's divisions in the
                    # ctor argument the same call produced assigned_divisions == 24.
                    _divs = armyops.group_divisions(self, og)
                    ok_f, err_f = armyops.draw_front_line(
                        self, og, s["front"], s["section"], 0, armyops.FULL, _divs, 0)
                except Exception as e:
                    ok_f, err_f = False, "%s: %s" % (type(e).__name__, e)
                if not ok_f:
                    results.append({"army": hex(og), "step": "draw_front_line",
                                    "ok": False, "error": err_f})
                    continue
                for _ in range(20):
                    time.sleep(0.15)
                    root_orders = [o for o in armyops.orders_of(self, og)
                                   if o["type"] == 2]
                    if root_orders:
                        break
            if not root_orders:
                results.append({"army": hex(og), "step": "wait_root_order", "ok": False,
                                "error": "root order never appeared in OG_ORDERS"})
                continue
            parent_id = root_orders[0]["id"]

            # top up to 24 with any divisions still unassigned
            cur = p.i32(og + armyops.OG_DIVISIONS + 0xC)
            spare = [d for d in self.unassigned_divisions(tag)
                     if not _dr.is_paradrop_division(self, d)[0]]
            if cur < 24 and spare:
                try:
                    armyops.assign_to_order(self, og, parent_id, spare[:24 - cur])
                except Exception:
                    pass
                time.sleep(0.15)

            # 2026-11-08 (work order 1108(1) §2.1): the offensive arrow and the
            # green tick are NOT drawn here any more. `COrderNewFrontCommand`
            # (0x232CC20) still SIGSEGVs on argument semantics and
            # `COrderExecuteCommand` dies inside RUN_CMD at 0x20AE17E because the
            # command's vtable slot is still NULL (see `cmd_ok` below) — one bad
            # command poisons the rest of the pipeline. A front line alone is
            # enough for the engine to rail every division of that army to the
            # border and dig in, which is what we actually need before attacking.
            if DRAW_ARROW:
                off = [o for o in armyops.orders_of(self, og) if o["type"] == 1]
                ok_o, err_o = True, ""
                if not off:
                    try:
                        ok_o, err_o = armyops.offensive_line(self, og, parent_id, target_provs)
                    except Exception as e:
                        ok_o, err_o = False, "%s: %s" % (type(e).__name__, e)
                try:
                    ok_e, err_e = armyops.execute_plan(self, og, 0, 0)
                except Exception as e:
                    ok_e, err_e = False, "%s: %s" % (type(e).__name__, e)
            else:
                ok_o, err_o, ok_e, err_e = None, "disabled (arrow not implemented)", \
                                           None, "disabled (arrow not implemented)"
            results.append({"army": hex(og), "root_order_id": parent_id,
                            "divisions": p.i32(og + armyops.OG_DIVISIONS + 0xC),
                            "front_line_ok": True,
                            "offensive_ok": ok_o, "offensive_err": err_o,
                            "execute_ok": ok_e, "execute_err": err_e})

        out["results"] = results
        out["ok"] = bool(results) and all(r.get("front_line_ok") for r in results)
        return out

    # ========================================================== DIPLOMACY
    def relations(self, tag=None, limit=60):
        """Relation records with other countries (array index = country index)."""
        p = self.p
        c = self._c(tag)
        self._load_tags()
        ptrs = self.country_ptrs()
        idx_to_tag = {}
        for tid, t in enumerate(self._tags):
            if t and t != "---":
                idx_to_tag[self._tag2idx[tid]] = t
        # --- PE 1.19.5.0 layout, verified 2026-11-16 -----------------------
        # CCountry+0xF88         -> CDiplomacyStatus*      (class_of agrees)
        # CDiplomacyStatus+0x08  -> CPdxArray<CRelationStatus*>  cap 440/size 440
        # The Linux values (CO_RELATIONS_ARR 0x2888, REL_INDEX 0x50) are stale
        # on hoi4.exe: 0x2888 points into the per-country state/province
        # registry, so the old path died on `read 0x52+4`, and +0x50 of a
        # PE CRelationStatus is 0. The array position IS the country index.
        if sys.platform == "win32":
            dip = p.u64(c.ptr + self.WIN_CO_DIPLO_STATUS)
            rows = _arr(p, dip, self.WIN_DIPLO_RELATIONS_OFF) if dip else []
        else:
            rows = _arr(p, c.ptr, CO_RELATIONS_ARR)
        out = []
        for i, r in enumerate(rows):
            t = idx_to_tag.get(i)
            if not t:
                continue
            if sys.platform == "win32":
                idx = i
            else:
                idx = p.i32(r + REL_INDEX)
            out.append({"tag": t, "ptr": hex(r), "index": idx})
            if len(out) >= limit:
                break
        return out

    # PE 1.19.5.0 diplomacy layout (see relations() above).
    WIN_CO_DIPLO_STATUS = 0xF88
    WIN_DIPLO_RELATIONS_OFF = 0x08

    FACTION_VT_RVA = 0x4353758      # NFactions::CFaction vtable, 1.19.4.1

    def _faction_objects(self):
        """Every live NFactions::CFaction.

        2026-11-23 (复测 6 P0-1): Windows 1.19.5.0 优先 O(1) 直读
        `CGameState + 0x3F8 -> CFactionSystem + 0x20`（零扫描、永远实时反映
        战中新加入阵营如 BEL 入同盟国），仅在读取失败时回退全内存 vtable 扫描。
        """
        if sys.platform == "win32":
            try:
                p = self.p
                gs = self.gamestate
                if gs:
                    fs = p.u64(gs + 0x3F8)
                    if fs and 0x10000 < fs < 0x7FFFFFFFFFFF:
                        arr = _arr(p, fs, 0x20)
                        valid = [x for x in arr if 0x10000 < x < 0x7FFFFFFFFFFF]
                        if len(valid) == len(arr) and len(arr) <= 128:
                            return valid
            except Exception:
                pass
        pid = self.p.pid
        cache = getattr(self, "_faction_objects_cache", None)
        if cache and cache[0] == pid:
            return cache[1]
        if sys.platform == "win32":
            hits = self.win_scan_class("CFaction")
            self._faction_objects_cache = (pid, hits)
            return hits
        needle = struct.pack("<Q", self.p.base + self.FACTION_VT_RVA)
        hits = []
        for m in self.p.regions(writable=True):
            a, b = m["start"], m["end"]
            pos = a
            while pos < b:
                n = min(8 << 20, b - pos)
                try:
                    data = self.p.read(pos, n)
                except Exception:
                    pos += n
                    continue
                st = 0
                while True:
                    i = data.find(needle, st)
                    if i < 0:
                        break
                    hits.append(pos + i)
                    st = i + 8
                pos += n
        hits = sorted(set(hits))
        self._faction_objects_cache = (pid, hits)
        return hits

    def _faction_ptr(self, tag=None):
        """The CFaction this country belongs to, or 0.

        2026-11-23: Windows 下 _faction_objects() 已是 O(1) 实时直读 CFactionSystem，
        不再对单国永久缓存，确保战中新加入/退出阵营立即可见。
        """
        p = self.p
        c = self._c(tag)
        if sys.platform != "win32":
            cache = getattr(self, "_faction_ptr_cache", None)
            key = (p.pid, c.tag)
            if cache and cache[0] == key:
                return cache[1]
        found = 0
        for f in self._faction_objects():
            try:
                if c.ptr in _arr(p, f, FACTION_MEMBERS):
                    found = f
                    break
            except Exception:
                continue
        if sys.platform != "win32":
            self._faction_ptr_cache = ((p.pid, c.tag), found)
        return found

    def faction(self, tag=None):
        p = self.p
        f = self._faction_ptr(tag)
        if not f:
            return None
        nm = None
        for off in range(0, 0x200, 8):
            s = item_name(p, f + off)
            if s and s.isprintable() and 2 < len(s) < 48 and " " not in s[:1]:
                nm = s
                break
        members = []
        for m in _arr(p, f, FACTION_MEMBERS):
            cc = self.country_by_ptr(m)
            members.append(cc.tag if cc else hex(m))
        leader = 0
        try:
            leader = p.u64(f + FACTION_MEMBERS - 8)
        except Exception:
            pass
        lc = self.country_by_ptr(leader) if leader else None
        return {"ptr": hex(f), "name": nm, "class": self.oi.class_of(f),
                "members": members,
                "leader": lc.tag if lc else (members[0] if members else None)}

    # ========================================================== CHARACTERS
    VT_ARMY_LEADER = 0x43D5890   # 11CArmyLeader vtable, 1.19.4.1 (rtti_index.csv)

    def _extract_char_key_and_name(self, ch: int):
        """(key, name) of a CCharacter, tolerant of the 1.19.x layout shifts.

        CH_KEY/CH_NAME are correct for 1.19.4.1 (checked live), but the fallback scan
        keeps this working if a future patch inserts a field before them — the key is
        always a `TAG_something` token and the name a printable string.
        """
        p = self.p
        key = item_name(p, ch + CH_KEY)
        name = item_name(p, ch + CH_NAME)
        if key and 2 < len(key) < 64 and "_" in key:
            return key, (name or key)
        found_key, found_name = None, None
        for off in range(0x10, 0x78, 8):
            s = item_name(p, ch + off)
            if not s or not s.isprintable() or not (3 <= len(s) <= 64):
                continue
            head = s.split("_", 1)[0]
            if "_" in s and 2 <= len(head) <= 4 and head.isalpha() and head.isupper():
                found_key = s
            elif found_name is None:
                found_name = s
        return found_key, (found_name or found_key or key)

    def _find_army_leader_ptr(self, ch: int) -> int:
        """CArmyLeader* inside a CCharacter, found by its vtable, not by an offset.

        `CH_ARMY_LEADER = 0xC0` is where 1.19.4.1 keeps it, but that is exactly the
        kind of offset that moves when a class grows (CScientist was added in this
        version). Matching the CArmyLeader vtable over +0x50..+0x150 is immune to it.
        """
        p = self.p
        raw = p.try_read(ch + 0x50, 0x100)
        if not raw:
            return 0
        for i in range(0, len(raw) - 7, 8):
            ptr = struct.unpack_from("<Q", raw, i)[0]
            if not (0x10000 < ptr < 0x7FFFFFFFFFFF):
                continue
            head = p.try_read(ptr + 8, 4)
            if head and struct.unpack("<i", head)[0] == 4713:
                return ptr
        return 0


    VT_ADVISOR = 0x4349AD8   # 8CAdvisor vtable, 1.19.4.1 (rtti.pkl)

    def _advisor_pool(self):
        """{character_key: CAdvisor*} for the live process.

        Probed 2026-11-10: a CAdvisor is NOT stored inside its CCharacter (a
        +0x000..+0x220 dump only shows CCharacterTemplate / CUnitMedalStore).
        Advisors live in their own array; each one carries its character key as a
        std::string at +0x30 (e.g. "GER_werner_von_blomberg"). So collect every
        object whose vtable is 8CAdvisor and index it by that string.
        """
        p = self.p
        cache = getattr(self, "_adv_pool_cache", None)
        if cache and cache[0] == p.pid and cache[1]:
            return cache[1]
        if sys.platform == "win32":
            found = {}
            for addr in self.win_scan_class("CAdvisor"):
                key = p.stdstring(addr + 0x30)
                if key:
                    found[key] = addr
            self._adv_pool_cache = (p.pid, found)
            return found
        target = p.va(self.VT_ADVISOR)
        needle = struct.pack("<Q", target)
        found = {}
        for m in p.regions(writable=True):
            a, b = m["start"], m["end"]
            cur = a
            while cur < b:
                n = min(4 << 20, b - cur)
                try:
                    buf = p.try_read(cur, n)
                except Exception:
                    buf = None
                if buf:
                    k = 0
                    while True:
                        k = buf.find(needle, k)
                        if k < 0:
                            break
                        addr = cur + k
                        try:
                            key = p.stdstring(addr + 0x30)
                        except Exception:
                            key = None
                        if key and 3 < len(key) < 64 and key not in found:
                            found[key] = addr
                        k += 8
                cur += n
        if found:
            self._adv_pool_cache = (p.pid, found)
        return found

    def find_advisor(self, key, tag=None):
        """CAdvisor* for a character key / idea token / bare name."""
        pool = self._advisor_pool()
        if not pool:
            return 0
        k = (key or "").lower()
        for cand in (k, k.upper()):
            if pool.get(cand):
                return pool[cand]
        for name, addr in pool.items():
            nl = name.lower()
            if nl == k or nl.endswith("_" + k):
                return addr
        return 0

    def _find_advisor_ptr(self, ch: int) -> int:
        """Kept for compatibility: advisors are not inside the character."""
        return 0

    VT_CCHARACTER = 0x4349390   # 10CCharacter vtable, 1.19.4.1 (rtti.pkl)

    G_PROVINCE_MANAGER = 0x459E9F0   # global -> manager singleton (vtable+0x50 = GetProvince(id))
    VT_PROVINCE_MGR    = 0x50        # slot offset inside the manager vtable

    def province_ptr(self, province_id):
        """CProvince* for a province id, via the engine's own GetProvince(id).

        Found 2026-11-10 by disassembling 0xda7410:
            rax = [0x459e9f0]            ; manager singleton
            fn  = [[rax]+0x50]           ; virtual GetProvince(int)
        This is exactly the object COrderSetParadropTargetCommand wants as arg1.

        PE 1.19.5.0: G_PROVINCE_MANAGER is a Linux ELF offset, so the injected
        call crashed (Err 299).  The live province table is a plain
        CPdxArray<CProvince*> inside CCurrentGameState (GS_PROVINCES, cap 13414)
        and is indexed by province id, so the pointer is a pure read now.
        """
        p = self.p
        if sys.platform == "win32":
            gs = p.u64(self.va(getattr(O, "G_GAMESTATE", 0x33DD6B0)))
            if not gs:
                return 0
            arr = _arr(p, gs, getattr(O, "GS_PROVINCES", 0x2B0))
            if 0 <= int(province_id) < len(arr):
                return arr[int(province_id)]
            return 0
        mgr = p.u64(self.va(self.G_PROVINCE_MANAGER))
        if not mgr:
            return 0
        vt = p.u64(mgr)
        if not vt:
            return 0
        fn = p.u64(vt + self.VT_PROVINCE_MGR)
        if not fn:
            return 0
        import api
        api.allow_call(fn - self.base)
        # thiscall: rdi = manager singleton, esi = province id
        return self.call(fn - self.base, (mgr, int(province_id)))

    def _scan_vtable(self, vt_off):
        """All live objects whose first qword is `va(vt_off)`, cached per pid.

        2026-11-10: objindex.pkl is bound to ONE game process; after a restart
        every address in it is dead, which is why hoi4_generals returned 0 here.
        Scanning the writable heap for the class vtable is pid-proof.
        """
        p = self.p
        cache = getattr(self, "_vt_scan_cache", None)
        if cache and cache[0] == p.pid and vt_off in cache[1]:
            return cache[1][vt_off]
        from objindex import _scan_regions_for_vptrs
        target = p.va(vt_off)
        found = _scan_regions_for_vptrs(
            p, p.regions(writable=True), [target], max_hits=10_000_000, chunk_size=4 << 20
        )
        if not hasattr(self, "_vt_scan_cache") or self._vt_scan_cache[0] != p.pid:
            self._vt_scan_cache = (p.pid, {})
        self._vt_scan_cache[1][vt_off] = found
        return found

    def characters(self, tag=None, limit=5000):
        """The country's characters: generals, admirals, advisors, leaders."""
        p = self.p
        c = self._c(tag)
        pref = (c.tag + "_").lower()
        out = []
        try:
            chars = self.all_characters(c.tag)
        except Exception:
            chars = []
        if not chars:
            # `_scan_vtable` is a no-op on the PE build, and CCharacterManager+0x10
            # is empty right after a save is loaded (the manager repopulates later),
            # so a freshly restarted session found zero characters.  The object index
            # always has them: 12k CCharacter instances are scanned in ~12 s.
            try:
                chars = list(self.oi.instances("CCharacter"))
            except Exception:
                chars = []
        if not chars:
            chars = self._scan_vtable(self.VT_CCHARACTER)
        for ch in chars:
            k, nm = self._extract_char_key_and_name(ch)
            if k and k.lower().startswith(pref):
                out.append({"key": k, "name": nm, "ptr": hex(ch)})
                if len(out) >= limit:
                    break
        return {"count": len(out), "characters": out}

    # ============================================================ GENERALS
    CH_ARMY_LEADER = 0xA8   # CArmyLeader*, set for generals and admirals (1.19.5; was 0xC0)
    # Skills live at +0xF58/+0xF68/+0xF78/+0xF88 in 1.19.5 — the whole CArmyLeader
    # block moved down 0x100 bytes when the class grew.
    AL_SKILL       = 0x28
    AL_ATTACK      = 0xF58
    AL_DEFENSE     = 0xF68
    AL_PLANNING    = 0xF78
    AL_LOGISTICS   = 0xF88

    def _character_manager(self):
        return self.oi.singleton("CCharacterManager")

    def _country_leader_ptrs(self, tag=None):
        """PE 1.19.5.0: 直接从 CCountryCharacters (CCountry+0xFF0) 读本国全量陆/海军将领指针。

        2026-11-23 (C-8 真机实证):
          * CCountryCharacters+0x70 = CPdxArray<CArmyLeader*>（纯净本国陆军将领，含静态历史、
            归国如 CHI_alexander_von_falkenhausen、以及 CCreateUnitLeaderCommand 动态招募的
            随机名将领如 'Emil Pickelhaubt'！）；
          * CCountryCharacters+0x88 = CPdxArray<CNavyLeader*>（纯净本国海军将领）；
          * 每名将领自身 +0x20 存 script key，+0x40 存本地化显示名。
        """
        p = self.p
        c = self._c(tag)
        if sys.platform != "win32":
            return {"army": [], "navy": []}
        cc = p.u64(c.ptr + 0xFF0)
        if not cc or not (0x10000000000 < cc < 0x7F0000000000):
            return {"army": [], "navy": []}
        army = [x for x in _arr(p, cc, 0x70) if 0x10000000000 < x < 0x7F0000000000]
        navy = [x for x in _arr(p, cc, 0x88) if 0x10000000000 < x < 0x7F0000000000]
        return {"army": army, "navy": navy}

    def all_characters(self, tag=None):
        p = self.p
        c = self._c(tag)
        pref = (c.tag + "_").lower()
        out = []
        # PE 1.19.5.0: CCountry+0xFF0 is CCountryCharacters:
        #   +0x10  记录数组（实机验证每条占 16 字节：{CCharacter*, flags}，旧代码用 _arr 按 8 字节
        #          步长直接读，只拿到前半且混入 56 个非指针整数，丢掉了莫德尔/勃洛姆堡等 14 名将领！）
        #   +0x40  CAdvisor*, +0x70 CArmyLeader*, +0x88 CNavyLeader*, +0xA0 CScientist*
        # 2026-11-23 (C-8): CCountryCharacters 本身已严格按国家分桶，严禁再用 `startswith(pref)`
        # 把动态招募的随机名将领（如 'Emil Pickelhaubt'）和归国将领（如 CHI_falkenhausen）砍掉！
        if sys.platform == "win32":
            cc = p.u64(c.ptr + 0xFF0)
            if cc and (0x10000000000 < cc < 0x7F0000000000):
                data = p.u64(cc + 0x10)
                cap = p.i32(cc + 0x18)
                sz = p.i32(cc + 0x1C)
                if data and 0 < sz <= cap <= 8192:
                    # 实测 16 字节步长 {ptr, u64_flags}；按 16 字节遍历并容错检查
                    seen = set()
                    try:
                        raw = p.read(data, sz * 16)
                        for idx in range(sz):
                            v0 = struct.unpack_from("<Q", raw, idx * 16)[0]
                            if 0x10000000000 < v0 < 0x7F0000000000 and v0 not in seen:
                                seen.add(v0)
                                out.append(v0)
                    except Exception:
                        for v in _arr(p, cc, 0x10):
                            if 0x10000000000 < v < 0x7F0000000000 and v not in seen:
                                seen.add(v)
                                out.append(v)
                if out:
                    return out
        pool = []
        mgr = self._character_manager()
        if mgr:
            pool = _arr(p, mgr, 0x10)
        if not pool:
            try:
                pool = list(self.oi.instances("CCharacter"))
            except Exception:
                pool = []
        for ch in pool:
            if not (0x10000000000 < ch < 0x7F0000000000):
                continue
            k = item_name(p, ch + CH_KEY)
            if not k:
                k, _nm = self._extract_char_key_and_name(ch)
            if k and k.lower().startswith(pref):
                out.append(ch)
        return out

    def generals(self, tag=None):
        """The country's generals and admirals: name, skills, pointer."""
        p = self.p
        c = self._c(tag)
        out = []
        seen_leaders = set()

        # 2026-11-23 (C-8 根因修复): Windows 上优先 O(1) 直读 CCountryCharacters+0x70/+0x88，
        # 100% 包含所有静态历史将领 + 动态招募将领（无 TAG_ 前缀）+ 归国将领，且不依赖冷启动 OI！
        if sys.platform == "win32":
            # 建立 al_ptr -> CCharacter* 反查表（用于补充 character_ptr）
            al_to_ch = {}
            try:
                for ch in self.all_characters(tag):
                    try:
                        al0 = p.u64(ch + self.CH_ARMY_LEADER)
                        if 0x10000000000 < al0 < 0x7F0000000000:
                            al_to_ch[al0] = ch
                    except Exception:
                        continue
            except Exception:
                pass

            lptrs = self._country_leader_ptrs(tag)
            pref = (c.tag + "_").lower()
            static_defs = legality.character_defs(c.tag) if hasattr(legality, "character_defs") else {}

            for al in lptrs.get("army") or []:
                if al in seen_leaders:
                    continue
                seen_leaders.add(al)
                ch = al_to_ch.get(al)
                k = item_name(p, al + 0x20) or ""
                nm = item_name(p, al + 0x40) or ""
                if (not k or not nm) and ch:
                    ck, cnm = self._extract_char_key_and_name(ch)
                    k = k or ck or ""
                    nm = nm or cnm or ""
                if not k:
                    k = nm or ("leader_%x" % al)
                if not nm:
                    nm = k
                rk = legality.RANKS.get(p.i32(al + legality.AL_RANK), "?")
                is_dyn = (k not in static_defs) and (not k.lower().startswith(pref))
                out.append({
                    "key": k,
                    "name": nm,
                    "character_ptr": hex(ch) if ch else None,
                    "leader_ptr": hex(al),
                    "attack": p.i32(al + self.AL_ATTACK),
                    "defense": p.i32(al + self.AL_DEFENSE),
                    "planning": p.i32(al + self.AL_PLANNING),
                    "logistics": p.i32(al + self.AL_LOGISTICS),
                    "rank": rk,
                    "dynamic": is_dyn,
                })

            for nl in lptrs.get("navy") or []:
                if nl in seen_leaders:
                    continue
                seen_leaders.add(nl)
                ch = al_to_ch.get(nl)
                k = item_name(p, nl + 0x20) or ""
                nm = item_name(p, nl + 0x40) or ""
                if (not k or not nm) and ch:
                    ck, cnm = self._extract_char_key_and_name(ch)
                    k = k or ck or ""
                    nm = nm or cnm or ""
                if not k:
                    k = nm or ("admiral_%x" % nl)
                if not nm:
                    nm = k
                is_dyn = (k not in static_defs) and (not k.lower().startswith(pref))
                out.append({
                    "key": k,
                    "name": nm,
                    "character_ptr": hex(ch) if ch else None,
                    "leader_ptr": hex(nl),
                    "attack": None,
                    "defense": None,
                    "planning": None,
                    "logistics": None,
                    "rank": "admiral",
                    "skills_verified": False,
                    "dynamic": is_dyn,
                })

        if not out:
            try:
                chars = self.all_characters(tag)
            except Exception:
                chars = []
            if not chars:
                chars = [int(row["ptr"], 16)
                         for row in self.characters(tag, limit=5000)["characters"]]
            for ch in chars:
                al = p.u64(ch + self.CH_ARMY_LEADER)
                if not al:
                    al = self._find_army_leader_ptr(ch)
                if not al or al in seen_leaders:
                    continue
                seen_leaders.add(al)
                if sys.platform == "win32":
                    cls = self.oi.class_of(al)
                    if cls == "CNavyLeader":
                        key, nm = self._extract_char_key_and_name(ch)
                        out.append({"key": key, "name": nm, "character_ptr": hex(ch),
                                    "leader_ptr": hex(al), "attack": None, "defense": None,
                                    "planning": None, "logistics": None,
                                    "rank": "admiral", "skills_verified": False})
                        continue
                    if cls != "CArmyLeader":
                        continue
                key, nm = self._extract_char_key_and_name(ch)
                out.append({
                    "key": key,
                    "name": nm,
                    "character_ptr": hex(ch),
                    "leader_ptr": hex(al),
                    "attack": p.i32(al + self.AL_ATTACK),
                    "defense": p.i32(al + self.AL_DEFENSE),
                    "planning": p.i32(al + self.AL_PLANNING),
                    "logistics": p.i32(al + self.AL_LOGISTICS),
                    "rank": legality.RANKS.get(p.i32(al + legality.AL_RANK), "?"),
                })

        # mark the commanders the player can actually use right now
        for row in out:
            ok, why = legality.leader_available(self, row["key"], tag)
            row["available"] = ok
            if not ok:
                row["unavailable_reason"] = why
        avail = [r for r in out if r["available"]]
        return {"count": len(out), "available_count": len(avail), "generals": out}

    def available_generals(self, tag=None, rank=None):
        """Only commanders that can be assigned right now, as the player sees them."""
        rows = [r for r in self.generals(tag)["generals"] if r["available"]]
        if rank:
            rows = [r for r in rows if r["rank"] == rank]
        # Admirals carry None skills (see generals()); they must not poison the sort.
        def _sum(r):
            return sum(v or 0 for v in (r.get("attack"), r.get("defense"),
                                        r.get("planning"), r.get("logistics")))
        rows.sort(key=lambda r: -_sum(r))
        return {"count": len(rows), "generals": rows}

    # ---------------------------------------------------------- armies and orders
    def order_groups(self, tag=None):
        """The country's ARMIES and ARMY GROUPS (COrdersGroup)."""
        import armyops
        p = self.p
        co = self.country(tag) if tag else self.player()
        out, seen = [], set()
        for og in armyops.orders_groups(self, co):
            seen.add(og)
            out.append(armyops.og_info(self, og))
            parent = p.u64(og + armyops.OG_PARENT)
            if parent and parent not in seen:
                seen.add(parent)
                out.append(armyops.og_info(self, parent))
        # 2026-11-18: `og_info` reads member_armies from og+0x228+0xC, which on a
        # freshly created army group reported 615 for two armies. Recompute every
        # group's membership from the real parent links (one pass, no guessing),
        # and make a group's division count the sum of its armies.
        kids = {}
        for row in out:
            pg = row.get("parent_group")
            if pg:
                kids.setdefault(pg, []).append(row)
        for row in out:
            if row.get("is_army_group"):
                mine = kids.get(row["ptr"], [])
                row["member_armies"] = len(mine)
                row["member_ptrs"] = [k["ptr"] for k in mine]
                row["divisions"] = sum(int(k.get("divisions") or 0) for k in mine)
                row["members_source"] = "parent_links"
        return {"count": len(out), "groups": out}

    def unassigned_divisions(self, tag=None):
        """Divisions not attached to any army (CArmy*)."""
        co = self.country(tag) if tag else self.player()
        return [d for d in _arr(self.p, co.ptr, 0x290)
                if self.p.u64(d + 0xC0) == 0]

    def create_army(self, divisions=None, count=None, tag=None):
        """Collect divisions into a new army (the '+' button in the UI).

        The command is QUEUED (CCommand::Run) and only takes effect when the game
        advances a tick. In a paused game the old code reported success while no
        army existed, and a second call in the same paused tick took the SAME
        divisions again and silently did nothing. Both are reported honestly now.
        """
        if sys.platform == "win32":
            import armyops_win as armyops
        else:
            import armyops
        def _n():
            try:
                return (self.order_groups(tag) or {}).get("count")
            except Exception:
                return None
        before = _n()
        divs = divisions or self.unassigned_divisions(tag)
        if count:
            divs = divs[:count]
        if not divs:
            return {"ok": False, "error": "no divisions to assign"}
        ok, err = armyops.create_army(self, divs)
        after = _n()
        res = {"ok": ok, "error": err, "divisions": len(divs),
               "armies_before": before, "armies_after": after}
        rej = getattr(self, "_last_division_rejected", None)
        if rej:
            res["rejected"] = list(rej[:10])
        if sys.platform == "win32":
            # armyops_win calls 0xC417A0 directly, so the army exists the moment
            # the call returns -- no queue, no tick, nothing to wait for.
            return res
        if ok and before is not None and after is not None and after <= before:
            res["queued"] = True
            res["note"] = ("army count %s -> %s: the command is queued and lands on the "
                           "next game tick; do not issue a second create_army in the "
                           "same paused tick, it would take the same divisions again"
                           % (before, after))
        return res

    def add_divisions_to_army(self, orders_group, divisions=None, count=None,
                              from_army=None, order_id=None, tag=None,
                              move_to_province=None, spread=True,
                              max_per_province=None):
        """将未编组师或指定源集团军的师并入已存在的集团军（不建新军）。

        2026-11-23 (C-3 & 复测 11 P2-1):
          1. `divisions` 与 `from_army` 均省略：把当前未编组师（自动避开伞兵）并入 `orders_group`
             （未指定 `count` 时自动补足至 24 师上限，或全部未编组师）；
          2. 指定 `from_army`：把源集团军的师全部（或前 `count` 个）并入目标军（合并散军）；
          3. 指定 `divisions=[ptr, ...]`：精确并入指定师列表；
          4. 默认仅改归属不改物理位置（返回 `positions_moved: False`）；若同时传入
             `move_to_province=<province_id>`，并军成功后自动调用 `move_divisions(..., spread=spread)`
             把转入的师一步调离原省份（避免继续吃前线补给）。
        """
        og = int(orders_group, 16) if isinstance(orders_group, str) else int(orders_group or 0)
        if not og:
            return {"ok": False, "error": "orders_group is required"}
        if sys.platform != "win32":
            return {"ok": False, "error": "add_divisions_to_army is currently implemented on Windows PE 1.19.5.0"}
        import armyops_win as _aw
        import divreserve as _dr

        if divisions is not None:
            divs = [int(x, 16) if isinstance(x, str) else int(x) for x in divisions]
        elif from_army is not None:
            src_og = int(from_army, 16) if isinstance(from_army, str) else int(from_army)
            if src_og == og:
                return {"ok": False, "error": "from_army and orders_group are the same army"}
            divs = list(_aw.divisions_of(self, src_og))
        else:
            divs = [d for d in self.unassigned_divisions(tag)
                    if not _dr.is_paradrop_division(self, d)[0]]
            if count is None:
                cur_n = len(_aw.divisions_of(self, og))
                room = max(0, 24 - cur_n)
                if room > 0:
                    divs = divs[:room]
        if count is not None:
            divs = divs[:max(0, int(count))]
        if not divs:
            return {"ok": False, "error": "no eligible divisions to add to army %s" % hex(og)}
        res = _aw.add_divisions_to_army(self, og, divs, order_id=order_id)
        if move_to_province is not None and isinstance(res, dict) and res.get("ok"):
            moved_ptrs = res.get("transferred_divisions") or [hex(d) for d in divs]
            if moved_ptrs:
                mr = self.move_divisions(
                    divisions=moved_ptrs,
                    province=int(move_to_province),
                    tag=tag,
                    spread=bool(spread),
                    max_per_province=max_per_province,
                )
                res["move_result"] = {
                    "ok": bool(mr.get("ok")),
                    "province": mr.get("province"),
                    "spread": bool(mr.get("spread")),
                    "spread_plan": mr.get("spread_plan"),
                    "ordered": mr.get("ordered"),
                    "accepted": mr.get("accepted"),
                    "moving": mr.get("moving"),
                    "blocked_by": mr.get("blocked_by"),
                    "error": mr.get("error"),
                }
                res["positions_moved"] = bool((mr.get("accepted") or 0) > 0)
                if res["positions_moved"]:
                    res["position_note"] = (
                        "transferred %d division(s) and issued move orders toward province %d (spread=%s)"
                        % (len(moved_ptrs), int(move_to_province), bool(spread))
                    )
        return res

    def create_army_group(self, armies=None, tag=None):
        """Collect armies into a new army group (field marshal command)."""
        p = self.p
        if sys.platform == "win32":
            import armyops_win as AW
            co = self.country(tag) if tag else None
            mine = [og for og in AW.armies(self, co)
                    if not p.u8(og + AW.OG_IS_GROUP) and not p.u64(og + AW.OG_PARENT)]
        else:
            import armyops
            mine = [og for og in armyops.orders_groups(
                        self, self.country(tag) if tag else None)
                    if not p.u8(og + armyops.OG_IS_GROUP)
                    and not p.u64(og + armyops.OG_PARENT)]
        if armies is None:
            armies = mine
        if len(armies) < 1:
            return {"ok": False, "error": "no armies to group"}
        if sys.platform == "win32":
            ok, err, info = AW.create_army_group(self, armies)
            out = {"ok": ok, "error": err, "armies": len(armies)}
            out.update(info or {})
            return out
        import armyops
        ok, err = armyops.create_army_group(self, armies)
        return {"ok": ok, "error": err, "armies": len(armies)}

    def assign_commander(self, orders_group, character_key=None, leader_ptr=None,
                         allow_unverified=False):
        """Assign a commander to an army or army group.

        Player rules are enforced: only a FIELD MARSHAL may lead an army group, only a
        corps commander. If the character's `visible` condition is not met,
        For example a general not visible under the current government is REFUSED.
        """
        import armyops
        if isinstance(orders_group, str):
            orders_group = int(orders_group, 16)
        if isinstance(leader_ptr, str):
            leader_ptr = int(leader_ptr, 16)
        key = character_key
        if leader_ptr is None:
            for gen in self.generals()["generals"]:
                if gen["key"] == character_key or gen["name"] == character_key:
                    leader_ptr = int(gen["leader_ptr"], 16)
                    key = gen["key"]
                    break
            else:
                return {"ok": False,
                        "error": "找不到该将领: %s (先跑 hoi4_available_generals 看可用的 key)" % character_key}
        ok, why = legality.check_commander(self, orders_group, leader_ptr, key, allow_unverified)
        if not ok:
            return {"ok": False, "error": why, "rejected_by": "player_rules"}
        if sys.platform == "win32":
            # armyops.set_army_leader is built on the Linux call path (g.malloc +
            # a fixed 1.19.4.1 ctor VA), which does not exist on the PE build.
            # CSetArmyLeaderCommand's ctor is (this, CArmyLeader*, COrdersGroup*, u8),
            # so the three fields are leader / army / flag -- and cmdwin already knows
            # how to build it through the real ctor.
            self.send_command("SetArmyLeader",
                              {"arg0": leader_ptr, "arg1": orders_group, "arg2": 0})
            self._settle(0.5)
            after = armyops.og_info(self, orders_group).get("leader_ptr")
            same = bool(after) and int(after, 16) == int(leader_ptr)
            return {"ok": same, "leader_ptr": hex(leader_ptr), "after": after,
                    "error": "" if same else "the game did not attach the leader"}
        ok, err = armyops.set_army_leader(self, orders_group, leader_ptr)
        return {"ok": ok, "error": err}

    def border_fronts(self, enemy_tag, tag=None):
        """Front sections facing the target country, for drawing a line."""
        if sys.platform == "win32":
            import armyops_win
            segs = armyops_win.fronts_towards(self, enemy_tag, tag)
            return [{"front_ptr": hex(s["front"]), "front_handle": s["front_handle"],
                     "section": s["section"],
                     "border_provinces": s["border_provinces"]}
                    for s in segs]
        import armyops
        segs = armyops.fronts_towards(self, enemy_tag, tag)
        return [{"front_ptr": hex(s["front"]), "front_handle": s["front_handle"],
                 "section": s["section"], "border_provinces": s["border_provinces"]}
                for s in segs]

    def draw_front_line(self, orders_group, enemy_tag=None, front_ptr=None,
                        section=None, frm=0, to=100000, assign="proportional"):
        """Give an army a front line order. With enemy_tag, the WHOLE border is covered."""
        if sys.platform == "win32":
            # The PE build draws a front line by calling COrderNewRootCommand with
            # a CFront* and a CPdxArray<CArmy*>; the Linux arm-then-verify dance
            # (settle + poll for the queued order) does not apply, because the
            # engine appends the order synchronously inside Post.
            import armyops_win
            divs = armyops_win.divisions_of(self, orders_group)
            if front_ptr is not None:
                fp = int(front_ptr, 16) if isinstance(front_ptr, str) else int(front_ptr)
                ok, err, _cmd = armyops_win.draw_front_line(
                    self, orders_group, fp, section, frm, to, divisions=divs)
                return {"ok": ok, "error": err, "divisions": len(divs)}
            if not enemy_tag:
                return {"ok": False, "error": "enemy_tag or front_ptr is required"}
            seen, res, new_ids = set(), [], []
            for s in armyops_win.fronts_towards(self, enemy_tag):
                fk = tuple(s["front_handle"])
                if fk in seen:
                    continue
                seen.add(fk)
                before = {o["id"] for o in armyops_win.orders(self, orders_group)}
                ok, err, _cmd = armyops_win.draw_front_line(
                    self, orders_group, s["front"], s["section"], frm, to,
                    divisions=divs)
                fresh = [o["id"] for o in armyops_win.orders(self, orders_group)
                         if o["id"] not in before and o["id"] not in new_ids]
                if ok and not fresh:
                    ok = False
                    err = "the command posted but no order appeared"
                new_ids += fresh
                res.append({"front": s["front_handle"], "section": s["section"],
                            "ok": ok, "error": err,
                            "weight": len(s["border_provinces"]),
                            "new_orders": fresh})
            return {"ok": all(r["ok"] for r in res) if res else True,
                    "segments": res, "new_order_ids": new_ids,
                    "divisions": len(divs)}
        import armyops
        res = []
        if front_ptr is not None:
            fp = int(front_ptr, 16) if isinstance(front_ptr, str) else int(front_ptr)
            ok, err = armyops.draw_front_line(self, orders_group, fp, section, frm, to)
            return {"ok": ok, "error": err}
        if not enemy_tag:
            return {"ok": False, "error": "enemy_tag or front_ptr is required"}
        have = {(tuple(o["root_front"]), o["root_section"])
                for o in armyops.orders_of(self, orders_group)}
        segs = armyops.fronts_towards(self, enemy_tag)
        # 2026-11-08: the 7-arg COrderNewRootCommand ctor takes a CFront* and
        # covers the WHOLE front, so one order per CFront is enough. Drawing one
        # per section would stack duplicate orders on the same front.
        seen = set()
        known = {o["id"] for o in armyops.orders_of(self, orders_group)}
        new_ids = []
        for s in segs:
            fk = tuple(s["front_handle"])
            if fk in seen:
                continue
            seen.add(fk)
            key = (fk, s["section"])
            if key in have:
                res.append({"front": s["front_handle"], "section": s["section"],
                            "ok": True, "error": "already exists"})
                continue
            ok, err = armyops.draw_front_line(self, orders_group, s["front"], None, frm, to)
            # 2026-11-11: CCommand::Run returning true only means "queued for this frame".
            # Round 3 got 31 x ok:true here and the army still had zero orders, so the
            # next call died with "no front order to assign to" — a fake success.
            # Poll the army's order array and refuse to report an order that the
            # engine never created.
            fresh = []
            if ok:
                for _ in range(6):
                    self._settle(0.25)
                    now = armyops.orders_of(self, orders_group)
                    fresh = [o for o in now
                             if o["id"] not in known and o["id"] not in new_ids]
                    if fresh:
                        break
                if not fresh:
                    ok = False
                    err = ((err + "; ") if err else "") + (
                        "the command was accepted but the engine created no order")
            for o in fresh:
                new_ids.append(o["id"])
            res.append({"front": s["front_handle"], "section": s["section"],
                        "ok": ok, "error": err, "weight": len(s["border_provinces"]),
                        "new_orders": [o["id"] for o in fresh]})
        out = {"ok": all(r["ok"] for r in res) if res else True, "segments": res,
               "new_order_ids": new_ids}
        if assign and assign != "none":
            out["assignment"] = self.assign_divisions_to_front(
                orders_group, enemy_tag, mode=assign, order_ids=new_ids or None)
        return out

    def assign_divisions_to_front(self, orders_group, enemy_tag=None, mode="proportional",
                                  divisions=None, order_ids=None):
        """Distribute an army's divisions across its front orders.

        mode: "proportional" (by front section length, the default),
              "even", "all" (every division to every order), or "none".
        Use mode="none" to leave divisions unassigned for manual placement later.
        """
        import armyops
        p = self.p
        self._assign_note = None
        if mode == "none":
            return {"ok": True, "note": "divisions left unassigned"}
        if sys.platform == "win32":
            # 2026-11-17: the whole PE path lived in armyops_win (order ids at
            # +0x244, assigned list at +0x210) while this function still called
            # the Linux module, so every assignment was a silent no-op and the
            # order kept saying "no unit is assigned to this order".
            import armyops_win as _aw
            orders = _aw.orders(self, orders_group)
            _assign = _aw.assign_to_order
        else:
            orders = armyops.orders_of(self, orders_group)
            _assign = armyops.assign_to_order
        if order_ids:
            # 2026-11-11: match by the ids the draw call actually produced. Matching by
            # `(root_front, section)` handle silently dropped every order (the engine
            # stores a different root handle than the CFront we drew on) and produced
            # the misleading "no front order to assign to".
            want_ids = {int(x) for x in order_ids}
            orders = [o for o in orders if o["id"] in want_ids]
            weights = [1] * len(orders)
        elif enemy_tag:
            want = {(tuple(s["front_handle"]), s["section"]): len(s["border_provinces"])
                    for s in armyops.fronts_towards(self, enemy_tag)}
            matched = [o for o in orders
                       if (tuple(o["root_front"]), o["root_section"]) in want]
            if matched:
                orders = matched
                weights = [want[(tuple(o["root_front"]), o["root_section"])] for o in orders]
            else:
                # 2026-11-11: the engine re-sections a front when it is drawn, so the
                # handle/section pair recorded at draw time no longer matches the
                # surviving order and every division was refused with the misleading
                # "no front order to assign to". An army's own orders are the right
                # target anyway; keep them and say so.
                self._assign_note = ("front handle filter matched nothing; used all %d "
                                     "order(s) of this army" % len(orders))
                weights = [1] * len(orders)
        else:
            weights = [1] * len(orders)
        if not orders:
            return {"ok": False, "error": "no front order to assign to"}
        divs = list(divisions) if divisions else armyops.group_divisions(self, orders_group)
        if not divs:
            return {"ok": False, "error": "the army has no divisions"}
        n = len(divs)
        if mode == "all":
            shares = [divs] * len(orders)
        else:
            if mode == "even":
                weights = [1] * len(orders)
            tot = sum(weights) or 1
            counts, used = [], 0
            for i, w in enumerate(weights):
                k = n - used if i == len(weights) - 1 else max(1, round(n * w / tot))
                k = min(k, n - used - (len(weights) - 1 - i))
                counts.append(max(0, k)); used += counts[-1]
            shares, at = [], 0
            for k in counts:
                shares.append(divs[at:at + k]); at += k
        def _orders_now():
            return (_aw.orders(self, orders_group) if sys.platform == "win32"
                    else armyops.orders_of(self, orders_group))

        def _pick(rows, want_id, want_root):
            """The order to actually write to.

            2026-11-18 (BUG-C): right after a line is drawn the engine renumbers or
            re-sections the order, so the id cached a second ago no longer exists.
            `COrderAssignCommand::Execute` then falls back to the parent group
            (`[og+0x1B8]`) and the assignment lands nowhere -- the exact
            "the command was accepted but the order still holds 0 divisions"
            seen on the 19-division army of the 1939.07.30 run. Re-resolving the
            survivor is what makes the second attempt work.
            """
            for x in rows:
                if x["id"] == want_id:
                    return x
            if want_root:
                for x in rows:
                    if x.get("type") == 2 and tuple(x.get("root_front") or ()) == want_root:
                        return x
            for x in rows:
                if x.get("type") == 2:
                    return x
            return None

        out = []
        for o, share in zip(orders, shares):
            if not share:
                continue
            want_root = tuple(int(x) for x in (o.get("root_front") or ()))
            before = after = None
            err = ""
            tries = 0
            for tries in range(1, 3):
                tgt = _pick(_orders_now(), o["id"], want_root)
                if tgt is None:
                    err = ("the order disappeared and this army has no front order left "
                           "to assign to")
                    self._settle(0.8)
                    continue
                if before is None:
                    before = tgt.get("assigned_divisions")
                ok, cerr = _assign(self, orders_group, tgt["id"], share)
                self._settle(0.6 if tries == 1 else 1.4)
                fresh = _pick(_orders_now(), tgt["id"], want_root)
                after = (fresh or {}).get("assigned_divisions")
                if ok and (after or 0) > 0:
                    err = ""
                    break
                err = cerr or ("the command was accepted but the order still holds "
                               "%s divisions" % (after,))
            row = {"order_id": o["id"], "divisions": len(share), "tries": tries,
                   "ok": not err, "error": err,
                   "assigned_before": before, "assigned_after": after}
            if err:
                row["hint"] = ("hoi4_army_battleplans now auto-assigns and deletes an "
                               "order it cannot fill; or hoi4_delete_order + redraw. "
                               "A front order with 0 divisions can crash the engine "
                               "(AGENTS.md rule 8)")
            out.append(row)
        res = {"ok": all(x["ok"] for x in out), "mode": mode, "orders": out}
        note = getattr(self, "_assign_note", None)
        if note:
            res["note"] = note
            self._assign_note = None
        return res

    def division_detail(self, tag=None, limit=200, order_group=None):
        """Divisions one by one, using the game's own field names: organisation, strength,
        experience, entrenchment, fuel, supply, parent army, template, location."""
        import fieldmap, armyops, unitorders
        p = self.p
        fm = fieldmap.field_map(self, "CArmy")
        keep = {"organisation", "strength", "experience", "dig_in", "dig_in_cap",
                "fuel", "max_supply", "army_current_supply_ratio", "out_of_supply_days",
                "str_damage", "org_damage", "killed"}
        if order_group:
            divs = armyops.group_divisions(self, order_group)
        else:
            divs = list(_arr(p, self._c(tag).ptr, CO_ARMIES))
            # Divisions assigned to an army group are no longer in the country
            # array; without this union a fully grouped army reads as empty
            # (measured 1944-06: 225 divisions on the map, 0 returned).
            seen = set(divs)
            try:
                for og in (self.order_groups(tag).get("groups") or []):
                    gptr = og.get("ptr")
                    if isinstance(gptr, str):
                        gptr = int(gptr, 16)
                    if not gptr:
                        continue
                    try:
                        for d in armyops.group_divisions(self, gptr):
                            if d not in seen:
                                seen.add(d)
                                divs.append(d)
                    except Exception:
                        continue
            except Exception:
                pass
        out = []
        lost = 0
        for i, a in enumerate(divs[:limit]):
            # One division that dies / merges mid-read used to abort the whole
            # loop: 225 divisions on the map became 0 and staff_army trained 108
            # phantom divisions on the empty answer.
            try:
                if not a or a < 0x10000 or not p.u64(a):
                    lost += 1
                    continue
                tmpl = p.u64(a + 0x3B8)
                og = p.u64(a + armyops.CARMY_OG)
                row = {"index": i, "ptr": hex(a),
                       "handle": [p.i32(a + 0x18), p.i32(a + 0x1C)],
                       "province": unitorders.province_of(self, a),
                       "moving": bool(unitorders.movement(self, a)),
                       "template": item_name(p, tmpl + DT_NAME) if tmpl else None,
                       "army": (p.stdstring(og + armyops.OG_NAME) if og else None),
                       "army_ptr": hex(og) if og else None}
                row.update(fieldmap.read_fields(self, a, fm, keep))
                out.append(row)
            except Exception:
                lost += 1
                continue
        res = {"count": len(divs), "divisions": out}
        if lost:
            res["skipped"] = lost
        return res

    def orders_of(self, orders_group):
        if sys.platform == "win32":
            import armyops_win
            return armyops_win.orders(self, orders_group)
        import armyops
        return armyops.orders_of(self, orders_group)

    def execute_plan(self, orders_group, order_id=0, flag=1, executing=None):
        """Execute or halt a battle plan (or a single type-1 offensive arrow).

        2026-11-23 (复测 11 P0-2): 经 PE 反汇编 `COrderExecuteCommand::Execute (0x18D64A0)`
        与 `COrderInstance::SetExecuting (0x10CD510)` 实证：
          * `executing=False` (或 `flag=0`) + `order_id=<type-1 child id>`：精准关闭单条进攻箭头，
            完全不动父 type-2 前线订单和师；
          * `executing=False` (或 `flag=0`) + `order_id=<type-2 root id>`：关闭该前线及全部子箭头；
          * `executing=False` (或 `flag=0`) + `order_id=0`：原生 `0x18E5D50(og, 0)` 全军转守；
          * `executing=True` (或 `flag=1`)：开启进攻计划。
        """
        eff_exec = bool(executing) if executing is not None else bool(int(flag))
        if sys.platform == "win32":
            import armyops_win
            return armyops_win.set_plan_executing(
                self, orders_group, order_id=int(order_id or 0), executing=eff_exec
            )
        import armyops
        ok, err = armyops.execute_plan(self, orders_group, order_id)
        return {"ok": ok, "error": err, "executing": eff_exec}

    def offensive_line(self, orders_group, parent_order_id, provinces=None,
                       divisions=None, enemy_tag=None, depth=1,
                       allow_manual_fallback=False, enabled=True):
        """Draw or disable an offensive line under one front order (the arrow inside the front).

        2026-11-23 (复测 10 A-1 / A-2 / A-3 & 复测 11 P0-2 根治):
          0. 当 `enabled=False` 时，走原生 `set_plan_executing(og, parent_order_id, executing=False)`
             精准关停该前线下所有活跃 type-1 进攻箭头，完全不动父前线订单和师；
          1. 主路径 `armyops_win.engine_offensive` 接收**敌方目标省**（不是己方边境省）
             或直接传 `enemy_tag` 自动推导，调用真实 `COrderNewFrontCommand` ctor +
             `Execute (0x18D9070)`（通过 0xD57D90 敌对连通区匹配）并给产出的 type-1
             子命令原生分师；
          2. 默认关闭 `manual_offensive` 静默回退（`allow_manual_fallback=False`）：
             旧版静默退回单层空壳箭头、既不进攻还把 0x10B4EC0 参数写反导致父订单永久删不掉；
          3. 无论走哪条路径，最终必须实读核验该父订单下至少有 1 条 type-1 进攻箭头
             `assigned > 0`；若 `assigned == 0`，**必须返回 `ok: False`**（绝不假阳性骗人）！
        """
        if sys.platform == "win32":
            import armyops_win
            if enabled is False:
                res_off = armyops_win.set_plan_executing(
                    self, orders_group, order_id=int(parent_order_id), executing=False
                )
                res_off["method"] = "disable_offensive"
                return res_off
            e_ok, e_err, e_info = armyops_win.engine_offensive(
                self, orders_group, parent_order_id, provinces, divisions,
                enemy_tag=enemy_tag, depth=depth)
            if e_ok:
                kids = [c for c in (e_info.get("children") or [])
                        if c.get("root") == int(parent_order_id)
                        and c.get("type") == armyops_win.ORDER_TYPE_OFFENSIVE]
                fed_kids = [c for c in kids if (c.get("assigned") or 0) > 0]
                root_assigned = armyops_win.assigned_of(
                    self, orders_group, parent_order_id)
                out = {
                    "ok": bool(fed_kids),
                    "error": "" if fed_kids else (
                        "offensive arrow was built by engine_execute but has assigned_divisions=0 "
                        "(divisions will stay entrenched and NOT attack); use hoi4_plan_front or "
                        "hoi4_attack_country instead"
                    ),
                    "method": "engine_execute",
                    "target_provinces": (e_info or {}).get("target_provinces"),
                    "children": [(c["id"], c["assigned"], c["executing"]) for c in kids],
                    "children_fed": [c["id"] for c in fed_kids],
                    "children_executing": [c["id"] for c in kids if c.get("executing") == 1],
                    "root_assigned": root_assigned,
                }
                if (e_info or {}).get("auto_replaced_provinces"):
                    out["auto_replaced_provinces"] = e_info["auto_replaced_provinces"]
                if not fed_kids:
                    out["blocked_by"] = "zero_assigned_offensive_child"
                return out

            skipped = "engine_execute: %s" % e_err
            if "front-context layer empty" in (e_err or ""):
                return {"ok": False, "error": e_err, "method": "none",
                        "skipped": skipped,
                        "hint": ("draw the front via the UI on this group, or "
                                 "use hoi4_plan_front / hoi4_attack_country")}
            ok, err, _cmd = armyops_win.offensive_line(
                self, orders_group, parent_order_id, provinces, divisions)
            if ok:
                kids = [c for c in armyops_win.order_children(self, orders_group)
                        if c.get("root") == int(parent_order_id)
                        and c.get("type") == armyops_win.ORDER_TYPE_OFFENSIVE]
                fed_kids = [c for c in kids if (c.get("assigned") or 0) > 0]
                return {
                    "ok": bool(fed_kids),
                    "error": "" if fed_kids else "queue offensive_line produced 0-assigned child",
                    "blocked_by": None if fed_kids else "zero_assigned_offensive_child",
                    "method": "queue",
                    "children": [(c["id"], c["assigned"], c["executing"]) for c in kids],
                    "children_fed": [c["id"] for c in fed_kids],
                    "skipped": skipped,
                }
            if not allow_manual_fallback:
                return {
                    "ok": False,
                    "error": e_err or err or "engine_execute produced no offensive arrow",
                    "blocked_by": "engine_offensive_failed",
                    "method": "none",
                    "skipped": "; ".join(x for x in (skipped, err) if x),
                    "hint": (
                        "pass ENEMY target provinces (or enemy_tag='HOL') rather than own border "
                        "provinces; or use hoi4_plan_front(enemy_tag=..., armies=[...]) / "
                        "hoi4_attack_country(target_tag=...) to drive real attacks"
                    ),
                }
            m_ok, m_err, optr = armyops_win.manual_offensive(
                self, orders_group, parent_order_id, provinces)
            if not m_ok:
                return {"ok": False, "error": e_err or err or m_err,
                        "method": "none",
                        "skipped": "; ".join(x for x in (skipped, err) if x)}
            kids = [c for c in armyops_win.order_children(self, orders_group)
                    if c["type"] == armyops_win.ORDER_TYPE_OFFENSIVE
                    and c["ptr"] == optr]
            oid = kids[0]["id"] if kids else None
            fed = bool(kids and (kids[0].get("assigned") or 0) > 0)
            return {
                "ok": fed,
                "error": "" if fed else (
                    "manual_offensive created a decorative type-1 order with assigned_divisions=0 "
                    "(engine does not attack on manual_offensive arrows); use hoi4_plan_front "
                    "or hoi4_attack_country"
                ),
                "blocked_by": None if fed else "zero_assigned_offensive_child",
                "method": "manual_offensive",
                "order_ptr": optr,
                "order_id": oid,
                "children": [(c["id"], c["assigned"], c["executing"]) for c in kids],
                "children_fed": [c["id"] for c in kids if (c.get("assigned") or 0) > 0],
                "skipped": "; ".join(x for x in (skipped, err) if x),
            }
        raise NotImplementedError("offensive_line is Windows-only for now")

    def delete_order(self, orders_group, order_id, allow_child=False):
        if sys.platform == "win32":
            # 2026-11-23 (复测 9 E-1 根治): Windows 1.19.5.0 统一走 armyops_win.delete_order
            # （cmdwin.alloc + CanExecute + attach_fake_vtable + 先停/清 type-1 子命令并回读核验），
            # 彻底废除调用 Linux armyops.delete_order（g.malloc 无 fake_vtable 释放崩堆）。
            import armyops_win as _A
            return _A.delete_order(self, orders_group, order_id, allow_child=allow_child)
        import armyops
        ok, err = armyops.delete_order(self, orders_group, order_id)
        return {"ok": ok, "error": err}

    def empty_order_scan(self, tag=None):
        """Read-only: every battle-plan order that holds 0 divisions.

        2026-11-18 (crash #4): `hoi4_pause(paused=False)` and
        `hoi4_pause_in_hours` both start the clock. The 1936.08 autosave
        *contains* three empty type-1 children left behind by the 15:10 crash,
        so loading it and unpausing killed hoi4.exe at 0xB2E62E within ~15 s
        (dump `hoi4_20261118_153843`, 15:38:44). Both unpause paths now refuse
        while this returns anything.
        """
        if sys.platform != "win32":
            return []
        import armyops as _ao
        import armyops_win as A
        c = self._c(tag)
        out = []
        for og in [x for x in _ao.orders_groups(self, c)
                   if not self.p.u8(x + A.OG_IS_GROUP)]:
            try:
                rows = A.orders(self, og)
                kids = A.order_children(self, og)
            except Exception:
                continue
            fed = {o["id"] for o in rows
                   if (o.get("assigned_divisions") or 0) > 0}
            for k in kids:
                if k.get("foreign_group"):
                    continue
                if (k.get("assigned") or 0) == 0 and (k.get("root") not in fed):
                    # 2026-11-19: same exemption as purge_empty_orders and the
                    # two plan_front sweep sites.  A type-1 ARROW under a front
                    # order that holds divisions is the state the UI itself
                    # leaves after the player draws one; the engine feeds it from
                    # the parent when the plan runs.  Refusing to start the clock
                    # for that state made every offensive line unusable -- the
                    # arrow was created, then this guard blocked the very tick
                    # that would have filled it.
                    out.append({"army": hex(og), "order": k["id"],
                                "type": k.get("type"), "kind": "child"})
            for o in rows:
                if (o.get("assigned_divisions") or 0) == 0:
                    out.append({"army": hex(og), "order": o["id"],
                                "type": o.get("type"), "kind": "root"})
        return out

    def reset_battleplans(self, tag=None):
        """Delete EVERY battle-plan order (children first, then roots), all armies.

        2026-11-18 (crash #5): the 1936.08 autosave carried three type-2 front
        orders whose type-1 children were gone. `purge_empty_orders` removed the
        empty children and `empty_order_scan()` then reported a clean list, yet
        unpausing still died at `hoi4.exe+0xB2E62E` ~90 s later -- because a ROOT
        order that still carries `executing` (u8 at +0x248) but has **no**
        children is the same empty `CPdxArray` read. Deleting only the empty
        orders is therefore not enough after a load: drop the whole order tree
        and draw one fresh plan.
        """
        if sys.platform != "win32":
            return {"ok": False, "error": "reset_battleplans is Windows-only"}
        import armyops as _ao
        import armyops_win as A
        c = self._c(tag)
        ogs = [og for og in _ao.orders_groups(self, c)
               if not self.p.u8(og + A.OG_IS_GROUP)]
        deleted, errors = [], []
        for og in ogs:
            try:
                rows = A.orders(self, og)
                kids = A.order_children(self, og)
            except Exception as e:
                errors.append({"army": hex(og), "error": repr(e)})
                continue
            for k in kids:
                if k.get("foreign_group"):
                    continue
                # 2026-11-23 (E-1): 走 PE 原生安全 armyops_win.delete_order(..., allow_child=True)
                try:
                    r = A.delete_order(self, og, k["id"], allow_child=True)
                except Exception as e:
                    r = {"ok": False, "error": repr(e)}
                (deleted if r.get("ok") else errors).append(
                    {"army": hex(og), "order": k["id"], "kind": "child",
                     **({} if r.get("ok") else {"error": r.get("error")})})
            for o in rows:
                r = self.delete_order(og, o["id"])
                (deleted if r.get("ok") else errors).append(
                    {"army": hex(og), "order": o["id"], "kind": "root",
                     "type": o.get("type"),
                     **({} if r.get("ok") else {"error": r.get("error")})})
        left = []
        try:
            left = self.empty_order_scan(tag)
        except Exception:
            pass
        all_ok = (not errors) and (not left)
        return {"ok": all_ok, "armies": len(ogs), "deleted_count": len(deleted),
                "failed_count": len(errors),
                "deleted": deleted, "errors": errors,
                "orders_left": left,
                "note": (
                    "every battle-plan order is gone; draw a fresh plan with "
                    "hoi4_army_battleplans before any war"
                    if all_ok else
                    "reset_battleplans incomplete: %d error(s), %d order(s) left "
                    "(check `errors` and `orders_left` before unpausing)"
                    % (len(errors), len(left))
                )}

    def purge_empty_orders(self, tag=None):
        """删掉所有 0 师的战斗计划命令（type-2 根订单 **和** type-1 进攻子命令）。

        2026-11-18：`hoi4.exe+0xB2E62E` 是 `movsxd rdx,[rax+rcx*4-4]` —— 对空
        `CPdxArray` 取 `data[size-1]`，size=0 时读出 `[-4]` → ACCESS_VIOLATION。
        一条不挂师的命令，它的师列表正好就是空数组。所以**任何** 0 师命令
        （根命令或进攻子命令）都是一颗延时炸弹，读档之后应该先扫一遍。

        只删空命令，不动有师的；先删子命令再删根命令，避免留下孤儿子命令。
        """
        if sys.platform != "win32":
            return {"ok": False, "error": "purge_empty_orders is Windows-only"}
        import armyops as _ao
        import armyops_win as A
        c = self._c(tag)
        ogs = [og for og in _ao.orders_groups(self, c)
               if not self.p.u8(og + A.OG_IS_GROUP)]
        removed, kept = [], 0
        for og in ogs:
            try:
                rows = A.orders(self, og)
                kids = A.order_children(self, og)
            except Exception as e:
                removed.append({"army": hex(og), "error": repr(e)})
                continue
            fed_roots = {o["id"] for o in rows
                         if (o.get("assigned_divisions") or 0) > 0}
            empty_kids = [k["id"] for k in kids
                          if not k.get("foreign_group")
                          and (k.get("assigned") or 0) == 0
                          # 2026-11-19: a 0-division type-1 ARROW under a front
                          # order that DOES hold divisions is the normal state the
                          # UI leaves behind after drawing one -- the engine feeds
                          # it from the parent when the plan runs.  Purging those
                          # deleted the arrow we had just created (this is the
                          # same fix plan_front needed at both of its sweep sites;
                          # this third copy was missed).
                          and (k.get("root") not in fed_roots)]
            empty_roots = [o["id"] for o in rows
                           if (o.get("assigned_divisions") or 0) == 0]
            kept += len([o for o in rows
                         if (o.get("assigned_divisions") or 0) > 0])
            for oid in empty_kids + empty_roots:
                r = self.delete_order(og, oid, allow_child=(oid in empty_kids))
                removed.append({"army": hex(og), "order": oid,
                                "kind": "child" if oid in empty_kids else "root",
                                "ok": r.get("ok"), "error": r.get("error")})
        # 2026-11-21 (P2-8): `removed_count` used to be len(removed), i.e. it
        # counted ATTEMPTS -- a run that deleted 1 of 3 orders and failed twice
        # reported `removed_count: 3` and the caller believed the sweep was
        # clean.  This sweep exists to prove a crash hazard is gone, so "how many
        # were really deleted" and "how many could not be" must be separate
        # numbers, and `ok` must say whether any attempt failed.
        _deleted = sum(1 for r in removed if r.get("ok") is True)
        _failed = len(removed) - _deleted
        return {"ok": _failed == 0, "armies": len(ogs), "kept_orders": kept,
                "removed_count": _deleted, "attempted_count": len(removed),
                "failed_count": _failed, "removed": removed,
                "note": ("0-division orders crash the engine at hoi4.exe+0xB2E62E; "
                         "this list should be empty before any war")}

    # ---------------------------------------------------------- diplomacy
    def wargoal_types(self):
        import diplomacy
        return sorted(diplomacy.wargoal_types(self))

    # Germany's historical snowball: Anschluss (~12 divisions + 20 factories) and
    # the Sudetenland / end of Czechoslovakia (~40k rifles + 35 factories) are free.
    # Declaring war in 1936 throws that away and fights with 52 rifles.
    GER_SNOWBALL = ("GER_anschluss", "GER_demand_sudetenland", "GER_end_of_czechoslovakia")
    # (置顶分数, 最低师数, 最低年份, 是否要求和平) — the manpower gates are the game's
    # own: Anschluss needs >500k men under arms (~54 divisions), Sudetenland >725k
    # (~74). Below the gate the focus is reported in `blocked`, never recommended.
    GER_SNOWBALL_CHAIN = {
        "GER_anschluss":               (2000, 54, 1937, True),
        "GER_demand_sudetenland":      (1990, 74, 1938, True),
        "GER_first_vienna_award":      (1985,  0, 1938, True),
        # 1.19.4.1 calls it GER_fate_of_czechoslovakia (the 1.19.2 text said
        # GER_end_of_czechoslovakia, which does not exist in this focus tree).
        "GER_fate_of_czechoslovakia":  (1980,  0, 1938, True),
        "GER_end_of_czechoslovakia":   (1980,  0, 1938, True),
        # GER_danzig_for_slovakia is mutually_exclusive with GER_danzig_or_war
        # (germany.txt:11777). Taking it in 1938.09 is exactly what locked the Polish
        # casus belli in every 2026-11-05 run — it is blacklisted, never scored.
        # GER_demand_memel does not exist in 1.19.4.1: the Memel/Danzig claims come
        # from GER_reassert_eastern_claims, whose only prerequisite is GER_anschluss.
        "GER_reassert_eastern_claims": (1975,  0, 1939, True),
        "GER_molotov_ribbentrop_pact": (1965,  0, 1939, True),
        "GER_mol_rib_pact":            (1965,  0, 1939, True),
        # has_army_manpower > 949999 = ~100 divisions in the field.
        "GER_danzig_or_war":           (1955, 100, 1939, True),
    }
    # Repeatable filler focuses that would otherwise win every empty slot.
    GER_LOW_PRIORITY = {"GER_the_triumphant_will": -100}
    GER_OPENING_TOP = {
        "GER_remilitarize_the_rhineland": 1850,
        "GER_rhineland":                  1850,
        # Anschluss is gated on GER_heed_von_neuraths_concerns OR
        # GER_reorganize_the_wehrmacht (focus_detail, checked live 2026-11-05).
        # Without one of them the annexation chain can NEVER become selectable —
        # that is why 28 focuses passed and Anschluss never appeared once.
        "GER_reorganize_the_wehrmacht":   1880,
        "GER_heed_von_neuraths_concerns": 1875,
        "GER_the_four_year_plan":         1840,
        "GER_four_year_plan":             1840,
        "GER_autarky_efforts":            1830,
        "GER_construct_the_reichsautobahn": 1820,
        "GER_kdf_wagen_factories":        1810,
        "GER_extra_tech_slot":            1800,
    }

    def _border_divisions(self, target_tag, tag=None):
        """How many of our divisions are physically on the border we would attack.

        This is the check the 2026-11-16 campaign needed and did not have: the
        war-readiness gate only counted divisions inside the country, so 112
        divisions parked in Berlin passed it and then watched the Netherlands
        walk in.
        """
        import divenum
        import mapdata
        import unitorders
        c = self._c(tag)
        me = c.tag
        tgt = str(target_tag).upper()
        out = {"own_tag": me, "enemy_tag": tgt, "border_provinces": 0,
               "border_sample": [], "divisions_on_border": 0, "divisions_count": 0}
        try:
            cmap = mapdata.province_controller_map(self)
            border = mapdata.border_provinces(self, me, tgt, owner_by_id=cmap)
        except Exception as e:
            out["error"] = "border map failed: %r" % (e,)
            return out
        out["border_provinces"] = len(border)
        out["border_sample"] = border[:16]
        try:
            rows = divenum.armies(self, me)
        except Exception as e:
            out["error"] = "division list failed: %r" % (e,)
            return out
        out["divisions_count"] = rows["count"]
        bset = set(border)
        on = []
        for a in rows["armies"]:
            try:
                pv = unitorders.province_of(self, a)
            except Exception:
                continue
            if pv in bset:
                on.append(pv)
        out["divisions_on_border"] = len(on)
        return out

    def _front_directive_check(self, target_tag):
        """Is a real tactical tick driving the army against this enemy?

        `hoi4_set_front_directive` only writes a JSON file.  Unless something
        executes it, `last_tick` stays null -- which is exactly what happened
        for the whole of the 1939 campaign.  Treating a stored directive as an
        armed one is what let the army stand still while war was declared.
        """
        import movemicro
        tgt = str(target_tag).upper()
        try:
            rows = (movemicro.front_directives(self) or {}).get("directives") or []
        except Exception as e:
            return {"ok": False, "error": repr(e)}
        row = None
        for r in rows:
            if str(r.get("enemy_tag") or "").upper() == tgt:
                row = r
                break
        info = {"directive": bool(row), "stance": (row or {}).get("stance"),
                "last_tick": (row or {}).get("last_tick"),
                "hours_since_tick": (row or {}).get("hours_since_tick")}
        if not row:
            info["problem"] = ("no front directive armed for %s -- run "
                               "hoi4_set_front_directive(enemy_tag=%s) first" % (tgt, tgt))
            return info
        lt = row.get("last_tick") or {}
        pid = getattr(self.p, "pid", None)
        if not lt:
            info["problem"] = ("directive stored but NEVER ticked (last_tick is null): "
                               "nothing is driving the army. Start the frontd worker or "
                               "call hoi4_tactical_front_tick(force=true)")
            return info
        if lt.get("pid") is not None and pid is not None and int(lt["pid"]) != int(pid):
            info["problem"] = ("last_tick belongs to game pid %s, current pid is %s -- "
                               "stale tick from a previous process" % (lt.get("pid"), pid))
            return info
        seen = lt.get("divisions_seen")
        if seen is not None and int(seen) <= 0:
            info["problem"] = ("the last tick ran but saw 0 of our divisions, so it has "
                               "nothing to command -- the army is not on the map for the "
                               "executor (check hoi4_army_status / hoi4_division_selftest)")
            return info
        info["ok"] = True
        age = row.get("hours_since_tick")
        # 2026-11-23 (复测 6 P2-3): 游戏当前处于暂停态时，后台 worker 本就无法推时钟，
        # 不应误报 "worker may be stalled" 干扰战前/战中决策。
        paused_now = None
        try:
            import session as _sess_fd
            paused_now = bool(_sess_fd.is_paused(self))
        except Exception:
            paused_now = None
        info["game_paused"] = paused_now
        if age is not None and age > 72:
            if paused_now:
                info["paused_note"] = (
                    "game is currently paused; last tick was %s game hours ago "
                    "(not stalled while paused)" % age
                )
            else:
                info["warning"] = (
                    "last tick was %s game hours ago; the worker may be stalled" % age
                )
        return info

    def _front_plan_check(self, target_tag, tag=None):
        """原生战役计划检查：前线 + 进攻线 + 至少一个集团军真的挂在上面。

        2026-11-18。1939.07.30 那一局在「波兰边境 8 个师、西线 0 个师」的状态下
        就宣了战，因为当时唯一的硬门槛是 frontd 指令——而那条指令从来没 tick 过。
        真正能证明「有部队会去打」的，是引擎自己持有的那条战役计划：

          1. 存在一条 type-2 前线命令，且它的 (front, section) 正对目标国；
          2. 这条前线命令下面至少有一个 type-1 进攻子命令 —— 只有前线只会「守住」，
             永远不会进攻；
          3. 至少有一个集团军真的把师指派到了这条命令上（`assigned_divisions > 0`），
             而不只是把线画出来。

        画线本身是和平时期动作（`hoi4_army_battleplans` 的 "not at war" 门禁已经
        拆掉，就是为这个）：开战前一天准备好的计划，才是开战第一天和和平最后一天
        的区别。
        """
        tgt = str(target_tag).upper()
        info = {"enemy_tag": tgt, "ok": False, "armies": []}
        if sys.platform != "win32":
            # 非 Windows 平台没有 armyops_win，不去挡一个我们读不了的东西。
            info["ok"] = True
            info["skipped"] = "native battle-plan check is Windows-only"
            return info
        import armyops_win as A
        import armyops as _ao
        try:
            co = self._c(tag)
            ogs = [og for og in _ao.orders_groups(self, co)
                   if not self.p.u8(og + A.OG_IS_GROUP)]
        except Exception as e:
            info["problem"] = "cannot enumerate armies: %r" % (e,)
            return info
        try:
            segs = A.fronts_towards(self, tgt, co.tag)
        except Exception as e:
            info["problem"] = "cannot enumerate front sections: %r" % (e,)
            return info
        facing = {tuple(int(x) for x in s["front_handle"]) for s in segs}
        info["sections_facing"] = len(facing)
        if not facing:
            info["problem"] = ("no front section of %s touches %s -- there is no border "
                               "to draw a front line on" % (co.tag, tgt))
            return info
        best = None
        for og in ogs:
            try:
                rows = A.orders(self, og)
            except Exception:
                continue
            kids = None
            for o in rows:
                if o["type"] != 2:
                    continue
                rf = tuple(int(x) for x in (o.get("root_front") or ()))
                if rf not in facing:
                    continue
                if kids is None:
                    try:
                        kids = A.order_children(self, og)
                    except Exception:
                        kids = []
                mine = [c for c in kids if c["root"] == o["id"] and c["type"] == 1]
                row = {"army": hex(og),
                       "order": o["id"],
                       "assigned_divisions": int(o.get("assigned_divisions") or 0),
                       "offensive_lines": len(mine),
                       "offensive_assigned": sum(int(c.get("assigned") or 0) for c in mine)}
                info["armies"].append(row)
                if row["assigned_divisions"] > 0 and row["offensive_lines"] > 0:
                    best = row
        if best:
            info["ok"] = True
            info["plan"] = best
            return info
        if not info["armies"]:
            info["problem"] = ("no front line faces %s -- draw it BEFORE declaring with "
                               "hoi4_army_battleplans(enemy_tag=%s) (peacetime drawing is "
                               "allowed on purpose)" % (tgt, tgt))
        elif all(r["assigned_divisions"] <= 0 for r in info["armies"]):
            info["problem"] = ("the front line against %s exists but NO army is assigned to "
                               "it (assigned_divisions == 0) -- run hoi4_assign_divisions("
                               "orders_group=..., enemy_tag=%s) and read hoi4_orders back "
                               "until assigned > 0" % (tgt, tgt))
        else:
            info["problem"] = ("the front line against %s has divisions but NO offensive "
                               "line -- a front alone only holds, it never attacks. Add the "
                               "arrow with hoi4_offensive_line(orders_group=..., "
                               "parent_order_id=...)" % (tgt,))
        return info

    def war_readiness_check(self, target_tag, tag=None, force=False, hard=True,
                            ack_co_belligerents=False):
        """Would a human press this button? Divisions, rifles, border, front plan,
        wargoal, and diplomatic escalation (factions / guarantees / subjects).

        Returns {"ready": bool, "blockers": [...], "warnings": [...]}.
        Nothing is written to the game; every number comes from the same reads
        the rest of the tool uses.

        `hard=False` skips the position checks -- used by
        `justify_wargoal`, which only starts a 250-day clock and must be
        possible long before the army is deployed.  `declare_war` always runs
        with hard=True: a war you cannot drive is a lost campaign, and
        2026-11-16 proved it twice.

        硬拦（hard=True 时）：
          1. 师数可读且 > 0；
          2. 外交合法性：不可对自己、已交战国、同阵营盟友或己方附庸/宗主宣战；
          3. 目标国边境上真的站着至少一个师；
          4. 目标国方向有一条原生前线命令 + 至少一条进攻线 + 至少一个集团军
             已指派到它上面（`_front_plan_check`）；
          5. **有战争借口**（`_wargoal_gate`）；
          6. **阵营/保独/宗主连锁防空门闸**（2026-11-23 复测 6 P0-1）：若宣战将
             拉入新的接壤参战强敌（>=3 接壤省且拥有 >=5 师或为主要国家），而我方
             该段边境驻军完全为 0，默认硬拦，防止盲宣小国导致主战线空门崩盘
             （显式传 `ack_co_belligerents=True` 或 `override_checks=True` 可放行；
             若已有驻军但较薄弱/缺前线，则写入高醒目 `warnings` 与 `reasons`）。
        """
        c = self._c(tag)
        me = c.tag
        tgt_tag = str(target_tag).upper()
        div = self.divisions(me, limit=1, deep=False)
        my_divs = int(div.get("count") or 0)
        try:
            enemy_divs = int((self.divisions(tgt_tag) or {}).get("count") or 0)
        except Exception:
            enemy_divs = 0
        rifles = 0.0
        try:
            for row in (self.equipment_stockpile(me) or {}).get("stockpile") or []:
                if "infantry_equipment" in str(row.get("equipment") or "").lower():
                    rifles += float(row.get("count") or 0)
        except Exception:
            pass
        try:
            completed = set(self.completed_focuses(me) or [])
        except Exception:
            completed = set()
        try:
            year = int(self.date().get("year") or 0)
        except Exception:
            year = 0

        blockers, warnings = [], []
        checks = {"divisions": {"count": my_divs, "source": div.get("source"),
                                "verified": div.get("verified")}}
        if div.get("warning"):
            checks["divisions"]["warning"] = div["warning"]

        # ---- 2026-11-23 (复测 6 P0-1): 实时外交连锁推演（阵营 / 保独 / 宗主 / 附庸 / 接壤空防）
        diplo = {}
        try:
            import diplomacy as _dip_fc
            diplo = _dip_fc.war_escalation_forecast(self, tgt_tag, me_tag=me) or {}
        except Exception as _de:
            diplo = {"error": repr(_de)}
        checks["diplomacy"] = diplo
        for _db in diplo.get("diplomatic_blockers") or []:
            blockers.append(_db)
        for _dw in diplo.get("diplomatic_warnings") or []:
            warnings.append(_dw)
        if hard and not ack_co_belligerents:
            for _ub in diplo.get("undefended_new_co_belligerents") or []:
                blockers.append(
                    "undefended co-belligerent border: declaring on %s pulls in %s (%s, %d enemy divs) "
                    "which shares %d border provinces with %s, but 0 divisions guard that border -- "
                    "deploy defense first or pass ack_co_belligerents=true to proceed anyway"
                    % (tgt_tag, _ub["tag"], ", ".join(_ub.get("reasons") or ["ally"])[:80],
                       int(_ub.get("enemy_divisions") or 0),
                       int(_ub.get("border_provinces") or 0), me)
                )

        # ---- hard check 1: can we count an army at all, and is there one?
        if not my_divs:
            blockers.append(
                "no divisions: %s reports 0 (source=%s). Either the army really is "
                "gone or the enumeration is broken -- in both cases a war is lost "
                "before it starts. Run hoi4_division_selftest(deep=true) to tell the "
                "two apart" % (me, div.get("source")))

        # ---- hard checks 2+3: only when the caller wants them
        if hard:
            bd = self._border_divisions(tgt_tag, tag=tag)
            checks["border"] = bd
            if bd.get("error"):
                blockers.append("cannot verify the border: %s" % bd["error"])
            elif not bd.get("border_provinces"):
                blockers.append("no land border province owned by %s touches %s"
                                % (me, tgt_tag))
            elif not bd.get("divisions_on_border"):
                blockers.append(
                    "no division on the %s border: %d divisions exist (%d border "
                    "provinces) but none stands on one of them -- deploy first"
                    % (tgt_tag, bd.get("divisions_count") or 0,
                       bd.get("border_provinces") or 0))
            # 2026-11-18: 硬门槛从「frontd 指令 tick 过」换成「原生前线 + 进攻线 +
            # 至少一个集团军已指派」。旧门槛只证明有个后台线程在算，不证明地图上
            # 有任何人会去打仗——1939.07.30 就是这么在 8 个师的状态下宣的战。
            fp = self._front_plan_check(tgt_tag, tag=tag)
            checks["front_plan"] = fp
            if not fp.get("ok"):
                blockers.append(fp.get("problem") or
                                "no battle plan with an offensive line faces %s" % tgt_tag)
            # ---- hard check 4 (2026-11-18 / 2026-11-23 复测 7 B-1 & B-7 根治):
            # 必须有战争借口。Windows 1.19.5 已打通 O(1) 实时直读 CDiplomacyStatus+0x80
            # （0.2ms，100% 包含新造 CB 与国策 CB），不再被旧 fast list 漏新 arena 坑死！
            # 若底层结构读不到（非 Windows 或异常）且未走权威路径，绝不伪报 "missing" 骗模型重造 200 天。
            _cb, _just, _werr = None, [], None
            _wg_source = "unknown"
            _direct_authoritative = False
            try:
                import diplomacy as _dip
                if hasattr(_dip, "country_wargoals_direct"):
                    _drows, _dok = _dip.country_wargoals_direct(self, tag=me, valid_only=True)
                    if _dok:
                        _direct_authoritative = True
                        _wg_source = "CDiplomacyStatus+0x80 (O(1) live)"
                        _cb = next(
                            (w for w in _drows
                             if str(w.get("target_tag") or "").upper() == tgt_tag),
                            None,
                        )
                        if _cb is None:
                            try:
                                _itgt = _dip.tag_index(self, tgt_tag)
                                _cb = next(
                                    (w for w in _drows if w.get("target_index") == _itgt),
                                    None,
                                )
                            except Exception:
                                pass
                if _cb is None and not _direct_authoritative:
                    # 兼容非 Windows 或离线单测 mock find_wargoal：调用 find_wargoal(self, tgt_tag, deep=None)
                    try:
                        _cb = _dip.find_wargoal(self, tgt_tag, deep=None)
                    except TypeError:
                        _cb = _dip.find_wargoal(self, tgt_tag)
                    _wg_source = "find_wargoal(deep=None)"
            except Exception as _e:
                _werr = repr(_e)
                _dip = None
            if _dip is not None:
                try:
                    _just = [j for j in _dip.justifying(self)
                             if str((j or {}).get("target") or "").upper() == tgt_tag]
                except Exception:
                    _just = []
            _prob, _state = _wargoal_gate(tgt_tag, _cb, _just)
            checks["wargoal"] = {
                "state": _state,
                "casus_belli": _cb,
                "justifying": _just,
                "error": _werr,
                "authoritative": bool(_direct_authoritative or (_cb is not None)),
                "source": _wg_source,
            }
            if _werr:
                checks["wargoal"]["state"] = "unknown"
                blockers.append(
                    "cannot verify a casus belli against %s (%s) -- tool lookup error, "
                    "do NOT re-justify blindly; verify with hoi4_wargoals first"
                    % (tgt_tag, _werr)
                )
            elif _prob:
                blockers.append(_prob)
            # frontd 指令降级为提示：它管的是逐师微操，不再是宣战的前置条件。
            fd = self._front_directive_check(tgt_tag)
            checks["front_directive"] = fd
            if not fd.get("ok"):
                warnings.append(fd.get("problem") or "front directive is not ticking")
            elif fd.get("warning"):
                warnings.append(fd["warning"])

        # ---- advisory (skippable with force=True)
        total_enemy_est = int(diplo.get("total_enemy_divisions_est") or enemy_divs)
        min_divs = max(12 if me == "GER" else 8, int(enemy_divs * 1.2))
        if my_divs < min_divs:
            warnings.append("thin army: have %d, want >= %d (%s has %d)"
                            % (my_divs, min_divs, tgt_tag, enemy_divs))
        elif total_enemy_est > enemy_divs and my_divs < int(total_enemy_est * 0.8):
            warnings.append(
                "coalition imbalance: %s has %d divisions against est. %d total enemy divisions "
                "(%s %d + co-belligerents %d)"
                % (me, my_divs, total_enemy_est, tgt_tag, enemy_divs,
                   max(0, total_enemy_est - enemy_divs))
            )
        if rifles < 1500:
            warnings.append("low infantry_equipment stockpile: %.0f (want >= 1500)"
                            % rifles)
        if (me == "GER" and year < 1939
                and "GER_danzig_or_war" not in completed
                and not all(f in completed for f in self.GER_SNOWBALL[:2])):
            have = [f for f in self.GER_SNOWBALL[:2] if f in completed]
            warnings.append("historical timeline: %s not done (year=%d, done=%s) -- a minor "
                            "war goal before that is allowed, it just raises world tension"
                            % (" + ".join(self.GER_SNOWBALL[:2]), year, have))

        ready = not blockers
        return {
            "ready": ready,
            "blockers": blockers,
            "warnings": warnings,
            "advisory_only": bool(warnings) and not blockers,
            # kept for older callers
            "reasons": blockers + ([] if force else warnings),
            "checks": checks,
            "my_divisions": my_divs,
            "enemy_divisions": enemy_divs,
            "total_enemy_divisions_est": total_enemy_est,
            "target_faction": diplo.get("target_faction"),
            "guaranteed_by": diplo.get("guaranteed_by") or [],
            "overlord": diplo.get("overlord"),
            "subjects": diplo.get("subjects") or [],
            "co_belligerents": diplo.get("co_belligerents") or [],
            "new_co_belligerents": diplo.get("new_co_belligerents") or [],
            "co_belligerent_borders": diplo.get("co_belligerent_borders") or [],
            "infantry_equipment_stockpile": round(rifles, 1),
            "year": year,
            "completed_snowball": sorted(f for f in self.GER_SNOWBALL if f in completed),
        }


    # --- war goal state guard (2026-11-19) --------------------------------
    # hoi4.exe+0xB2E62E, twice in one night, identical stacks:
    #     movsxd rcx, [rsi+0x74]      ; CTimedWargoalActivity: state count
    #     mov    rax, [rsi+0x68]      ; state array data
    #     movsxd rdx, [rax+rcx*4-4]   ; states[size-1]   -> reads [-4] when empty
    #     ... str: "DIPLOMACY_MESSAGE_TIMED_WARGOAL_FINISH_TITLE" / STATE / OWNER
    # i.e. the *completion* of a timed war goal formats its message from the
    # last state in that array.  `hoi4_justify_wargoal(target=HOL)` with no
    # explicit states produced an activity with size == 0, so the 195th day
    # was a guaranteed crash.  Two guards below: pick a state when the caller
    # names none, and repair the array right after the activity appears.

    def _pick_target_state_id(self, target_tag):
        """First state owned by `target_tag` (used when the caller names none)."""
        try:
            for st in _arr(self.p, self.country(target_tag).ptr, CO_STATES):
                sid = self.p.i32(st + ST_ID)
                if sid > 0:
                    return sid
        except Exception:
            return None
        return None

    def ensure_wargoal_state_array(self, activity, state_id=None):
        """Make sure a CTimedWargoalActivity carries a non-empty state array.

        Returns a small report; `repaired` says whether the array had to be
        rebuilt.  The buffer comes from the game's own allocator so the engine
        can free it later, and the header is written as the engine's
        CPdxArray layout: +0x68 data / +0x70 capacity / +0x74 size.
        """
        if not activity:
            return {"ok": False, "why": "no activity"}
        p = self.p
        raw = activity.get("ptr")
        try:
            a = int(raw, 16) if isinstance(raw, str) else int(raw)
        except Exception:
            return {"ok": False, "why": "unreadable activity pointer"}
        try:
            size = p.i32(a + 0x74)
            cap = p.i32(a + 0x70)
            data = p.u64(a + 0x68)
        except Exception as e:
            return {"ok": False, "why": "read failed: %r" % (e,)}
        if size > 0 and 0x10000000000 < data < 0x7F0000000000:
            return {"ok": True, "repaired": False, "size": size, "capacity": cap}
        if state_id is None:
            return {"ok": False, "repaired": False, "size": size,
                    "why": "state array is empty and no state id was available"}
        import cmdwin as _cw
        buf = _cw.alloc(self, 16)
        p.write(buf, struct.pack("<i", int(state_id)))
        p.write(a + 0x68, struct.pack("<Q", buf))
        p.write(a + 0x70, struct.pack("<i", 1))
        p.write(a + 0x74, struct.pack("<i", 1))
        return {"ok": True, "repaired": True, "size": p.i32(a + 0x74),
                "capacity": p.i32(a + 0x70), "data": hex(buf), "state": int(state_id)}

    def justify_wargoal(self, target_tag, wargoal="take_state", states=None,
                        dry_run=False, force=False, tag=None):
        """Start justifying a war goal, gated by war_readiness_check unless force=True.

        2026-11-23 (D-5): 补齐 `tag=None` 可选形参（对齐 turnengine.run_plan 的 justify_wargoal 动作），
        避免在短上下文 Plan DSL 下抛 TypeError。

        We only choose the TARGET, and for types like take_state WHICH STATES;
        the duration and political power cost are decided by the game.
        Default is `take_state` (what the diplomacy dialog opens with): the
        script-only `annex_everything` never passes CanExecute in 1.19.4.1.
        """
        import diplomacy
        _ = tag  # Windows 命令通道按当前玩家会话发起，保留形参兼容 Plan DSL 与多国调用方
        if sys.platform == "win32":
            # 2026-11-15: the old path called diplomacy._new_action_param ->
            # g.call(ctor, ...) on a PE backend whose setregs is not implemented;
            # winject then read a scratch flag it never wrote and hoi4.exe died
            # with GetLastError=299. The PE channel is now native (see
            # cmdwin.justify_wargoal): CGenerateWarGoalAction's own default ctor
            # builds the object, wrapped in CDiplomaticActionCommand -- the exact
            # same two-object shell that declares real wars (§34).
            if dry_run:
                # 2026-11-22 (P1-1): a dry run must show the PP verdict too -- that
                # is the whole point of asking before you commit.
                _q = {}
                try:
                    _q = self.wargoal_quote(target_tag, wargoal, states)
                except Exception:
                    pass
                _pp = _q.get("current_pp")
                _need = _q.get("estimated_total_pp")
                return {"ok": True, "dry_run": True, "posted": 0,
                        "target": target_tag, "wargoal": wargoal,
                        "political_power": _pp,
                        "estimated_total_pp": _need,
                        "would_refuse": (None if (_pp is None or _need is None)
                                         else bool(_pp < float(_need))),
                        "quote": _q,
                        "note": "dry_run: no command built or posted"}
            # No duplicate justification: one at a time, same as the Linux path.
            try:
                for _j in diplomacy.justifying(self):
                    if _j.get("source_class") != "CTimedWargoalActivity":
                        continue
                    if str(_j.get("target") or "").upper() == str(target_tag).upper():
                        return {"ok": False, "blocked_by": "already_justifying",
                                "error": "already justifying a war goal against %s" % target_tag,
                                "justifying": _j}
            except Exception:
                pass
            # 2026-11-22 (P1-1): the UI refuses a justification you cannot pay for
            # ("not enough political power"); CGenerateWarGoalAction's command
            # channel does not.  Measured live: PP 2 + a 40 PP quote drove the
            # country to **-36**, where no law / advisor / decision could be taken
            # for weeks while the justification itself barely moved.  Every number
            # needed is a pure read, so refuse and show them.
            _warn = None
            try:
                _q = self.wargoal_quote(target_tag, wargoal, states)
            except Exception:
                _q = {}
            try:
                _pp = int(self.player().political_power)
            except Exception:
                _pp = None
            _need = _q.get("estimated_total_pp")
            if _need is not None and _pp is not None and _pp < float(_need):
                if not force:
                    return {"ok": False, "blocked_by": "political_power",
                            "error": ("justifying against %s costs about %.0f political "
                                      "power (%s days x %.2f/day) and you have %d. The game's "
                                      "UI refuses this; the raw command path does not, and "
                                      "taking it leaves the country at negative PP for weeks. "
                                      "Wait for PP, pick a cheaper goal, or pass force=true."
                                      % (target_tag, float(_need),
                                         _q.get("estimated_days"),
                                         float(_q.get("daily_pp_drain") or 0), _pp)),
                            "political_power": _pp, "required_pp": _need,
                            "quote": _q}
                _warn = ("force=true: PP %d is below the ~%.0f this justification "
                         "drains; PP will go negative" % (_pp, float(_need)))
            import cmdwin
            c = self._c(target_tag)
            # Resolve the CWarGoalType* for the requested wargoal. take_state is
            # the diplomacy dialog default; the default ctor already installs a
            # valid type, so an unresolved name just leaves that one in place.
            wgt = 0
            try:
                wgt = int(self.resolve("wargoal:" + str(wargoal), c.tag) or 0)
            except Exception:
                wgt = 0
            # 2026-11-19 (crash #7, 05:59): the activity's state array starts
            # EMPTY, and the engine formats the "war goal finished" message from
            # states[size-1] (+0x68/+0x70/+0x74) -- size 0 reads [-4] and the
            # game dies at hoi4.exe+0xB2E62E the moment the counter reaches
            # zero.  With the console's `instant_wargoal` the counter is already
            # zero on the first frame, so the old 2 s drain lost that race
            # 100% of the time (verified: exception.txt -> base+0xB2E62E).
            # => drain only a few frames, then put a state id into the array
            #    BEFORE the clock is allowed to move again.
            r = cmdwin.justify_wargoal(self, c.tag_id, wgt, seconds=0.05)
            self._settle(0.6)
            mine = None
            repaired = None
            for _ in range(10):
                self._settle(0.35)
                try:
                    for _j in diplomacy.justifying(self):
                        if _j.get("source_class") != "CTimedWargoalActivity":
                            continue
                        if str(_j.get("target") or "").upper() == str(target_tag).upper():
                            mine = _j
                            break
                except Exception:
                    pass
                if mine:
                    break
            if mine and int(mine.get("states_in_activity") or 0) <= 0:
                try:
                    repaired = self.ensure_wargoal_state_array(
                        mine, self._pick_target_state_id(target_tag))
                except Exception as _e:
                    repaired = {"ok": False, "why": repr(_e)}
                if repaired and repaired.get("ok"):
                    try:
                        for _j in diplomacy.justifying(self):
                            if str(_j.get("target") or "").upper() == str(target_tag).upper():
                                mine = _j
                                break
                    except Exception:
                        pass
            return {"ok": bool(r.get("ok")), "posted": r.get("posted"),
                    "warning": _warn,
                    "target": target_tag, "target_id": c.tag_id,
                    "wargoal": wargoal, "wargoal_type_ptr": hex(wgt) if wgt else None,
                    "action": hex(r.get("act") or 0), "cmd": hex(r.get("cmd") or 0),
                    "started": mine, "verified": mine is not None,
                    "wargoal_state_repaired": repaired,
                    "note": (None if mine else
                             "command posted; the timed activity appears on the next "
                             "unpaused frame — call hoi4_justifying to confirm")}
        readiness = None
        if not force:
            try:
                import playbooks as _pb
                # NOTE: justify_wargoal has no `tag` parameter — using `tag` here
                # raised NameError, the except swallowed it and the whole veteran
                # gate silently disappeared (caught 2026-11-05 by calling it live).
                pbk = _pb.get_playbook(self.player().tag)
            except Exception:
                pbk = {}
            forbid = int(pbk.get("forbid_manual_justify_before_year") or 0)
            fallback = int(pbk.get("justify_fallback_year") or forbid)
            # 2026-11-06: a minor nation, or a major switched to `cheese_breakout`,
            # may justify freely — the historical no-justify rule only applies to
            # the seven majors in "historical" mode.
            try:
                import playbooks as _pbm
                if _pbm.get_playbook_mode(self.player().tag) != "historical":
                    forbid = 0
            except Exception:
                pass
            if forbid and not force:
                year = 0
                try:
                    year = int(self.date().get("year") or 0)
                except Exception:
                    pass
                done = set()
                try:
                    done = set(self.completed_focuses() or [])
                except Exception:
                    pass
                # 1936-1938: never. From the fallback year on: only when the focus
                # chain failed to deliver a casus belli (e.g. Poland accepted Danzig,
                # ai_chance 10/11 -> the ~9% branch that grants no wargoal).
                allowed = (year >= fallback
                           and any(f in done for f in ("GER_danzig_or_war",))
                           ) or year >= forbid
                if not allowed:
                    return {
                        "ok": False,
                        "blocked_by": "veteran_playbook",
                        "error": pbk.get("manual_justify_note", "manual justify forbidden"),
                        "hint": ("Historical line: use the focus tree. For a deliberate "
                                 "1936 opening (e.g. the Dutch/East-Indies rubber plan) pass "
                                 "force=true — the engine accepts it immediately; the "
                                 "playbook gate is advisory, not a game rule."),
                        "year": year,
                        "completed": sorted(f for f in ("GER_danzig_or_war",) if f in done),
                    }
            # 2026-11-16: justifying is a CLAIM, not an attack -- it starts a
            # 250-day clock and must be possible long before the army is
            # deployed.  Only the advisory half of the readiness check applies;
            # the border/front-tick blockers belong to declare_war.
            readiness = self.war_readiness_check(target_tag, hard=False, force=True)
            if readiness.get("blockers"):
                return {"ok": False, "blocked_by": "war_readiness_check",
                        "error": "; ".join(readiness["blockers"]),
                        "readiness": readiness}
        sp = []
        if states:
            want = {int(x) for x in states}
            for tag in (target_tag,):
                for st in _arr(self.p, self.country(tag).ptr, CO_STATES):
                    if self.p.i32(st + ST_ID) in want:
                        sp.append(st)
            missing = want - {self.p.i32(x + ST_ID) for x in sp}
            if missing:
                return {"ok": False, "error": "these states do not belong to the target: %s" % sorted(missing)}
        # 2026-11-19: the activity must never be born with an empty state array
        # (see the crash note above the helpers).  With no explicit states we
        # record the target's first state -- the engine only needs *a* state to
        # point the completion message at.
        guard_state_id = self.p.i32(sp[0] + ST_ID) if sp else None
        auto_state = None
        if not sp:
            auto_state = self._pick_target_state_id(target_tag)
            if auto_state is not None:
                guard_state_id = auto_state
                for st in _arr(self.p, self.country(target_tag).ptr, CO_STATES):
                    if self.p.i32(st + ST_ID) == auto_state:
                        sp = [st]
                        break
        # 2026-11-11: CGenerateWarGoalAction::CanExecute refuses silently when the
        # country cannot pay the engine's political-power price, and every caller only
        # saw "CanExecute refused every attempt" (three runs blamed the whole command).
        # Ask the engine for the quote first and report the real blocker.
        if not dry_run:
            try:
                for _j in diplomacy.justifying(self):
                    if _j.get("source_class") != "CTimedWargoalActivity":
                        continue        # a queued CGenerateWarGoalAction is not a justification
                    if str(_j.get("target") or "").upper() == str(target_tag).upper():
                        return {"ok": False, "blocked_by": "already_justifying",
                                "error": "already justifying a war goal against %s" % target_tag,
                                "justifying": _j}
            except Exception:
                pass
            try:
                quote = self.wargoal_quote(target_tag, wargoal, states)
            except Exception as _e:
                quote = None
            if (quote and quote.get("ok") and quote.get("can_justify_now") is False
                    and (quote.get("pp_shortfall") or 0) > 0):
                return {"ok": False, "blocked_by": "political_power",
                        "error": "not enough political power: %s" % (quote.get("note") or ""),
                        "current_pp": quote.get("current_pp"),
                        "required_pp": quote.get("required_pp"),
                        "pp_shortfall": quote.get("pp_shortfall"),
                        "quote": quote,
                        "states": [self.p.i32(x + ST_ID) for x in sp]}
        try:
            _pp0 = self.politics().get("political_power")
        except Exception:
            _pp0 = None
        ok, err = diplomacy.justify_wargoal(self, target_tag, wargoal, sp, dry_run=dry_run)
        _pp_spent = None
        mine = None
        if ok and not dry_run:
            # The engine accepts at most one justification at a time and refuses the
            # second one at execution time, long after CCommand::Run returned true.
            # Round 3 reported ok:true for commands that never became an activity.
            for _ in range(10):
                # A tick must run BEFORE justifying() is asked: the activity only
                # exists once the loop consumed the command, and the first query
                # used to cache an empty answer (objindex negative cache 2s/5s).
                self._settle(0.35)
                for _j in diplomacy.justifying(self):
                    if _j.get("source_class") != "CTimedWargoalActivity":
                        continue
                    if str(_j.get("target") or "").upper() == str(target_tag).upper():
                        mine = _j
                        break
                if mine:
                    break
        # 2026-11-19 guard #2: repair the state array the instant the activity
        # exists, so a 195-day timer can never end on an empty CPdxArray.
        state_guard = None
        if mine and not dry_run:
            try:
                state_guard = self.ensure_wargoal_state_array(mine, guard_state_id)
            except Exception as _e:
                state_guard = {"ok": False, "why": repr(_e)}
        # The PP is charged on a later tick too: read it AFTER the poll.
        try:
            _pp1 = self.politics().get("political_power")
            _pp_spent = (round(float(_pp0) - float(_pp1), 1)
                         if (_pp0 is not None and _pp1 is not None) else None)
        except Exception:
            _pp_spent = None
        if ok and not dry_run and not mine:
                try:
                    import session as _sess
                    _paused = _sess.is_paused(self)
                except Exception:
                    _paused = False
                if _paused:
                    # The command sits in the queue; the engine builds the activity on
                    # the next unpaused frame. Failing here made callers retry and leak
                    # one CGenerateWarGoalAction per attempt (round 4: six of them).
                    return {"ok": True, "queued": True,
                            "note": ("the game is PAUSED: the justification is queued and "
                                     "will start on the next unpaused frame - call "
                                     "hoi4_justifying after unpausing to confirm"),
                            "wargoal": wargoal,
                            "states": [self.p.i32(x + ST_ID) for x in sp]}
                # 2026-11-13: with the `instant_wargoal` cheat on there is no timed
                # activity at all - the CB is created on the same tick. A live
                # CWarGoal for (me, target) proves the justification completed, so
                # report success instead of "the engine charged PP and dropped it".
                try:
                    _cb = diplomacy.find_wargoal(self, target_tag)
                except Exception:
                    _cb = None
                if _cb:
                    return {"ok": True, "instant": True, "wargoal": wargoal,
                            "casus_belli": _cb, "pp_spent": _pp_spent,
                            "states": [self.p.i32(x + ST_ID) for x in sp]}
                return {"ok": False,
                        "blocked_by": "engine_dropped" if (_pp_spent or 0) > 0 else "not_started",
                        "pp_spent": _pp_spent,
                        "error": (("the engine charged %.1f PP but created no timed war-goal "
                                   "activity for %s — the justification action was dropped "
                                   "after the cost was taken" % (_pp_spent, target_tag))
                                  if (_pp_spent or 0) > 0 else
                                  ("the command was queued but no timed war-goal activity "
                                   "for %s appeared (the engine allows one justification at "
                                   "a time — check hoi4_justifying)" % target_tag)),
                        "wargoal": wargoal,
                        "states": [self.p.i32(x + ST_ID) for x in sp],
                        "active": diplomacy.justifying(self)}
        return {"ok": ok, "error": err, "wargoal": wargoal, "dry_run": bool(dry_run),
                "pp_spent": _pp_spent,
                "states": [self.p.i32(x + ST_ID) for x in sp],
                "readiness": readiness,
                "state_guard": state_guard,
                # live progress straight from CTimedWargoalActivity (days_left/eta_date)
                "justifying": (diplomacy.justifying(self) if ok and not dry_run else [])}

    def wargoal_quote(self, target_tag, wargoal="take_state", states=None, tag=None):
        """对 target_tag 正当化指定州要多少 PP、还差多少、大概多少天（引擎级精确 PP）。"""
        import diplomacy
        if sys.platform == "win32":
            # PE 1.19.5.0: an ENGINE-EXACT quote is impossible without killing
            # the process. Two independent experiments (2026-11-16, crashes #6
            # and #7) proved both halves fatal:
            #   * writing the PP field politics+0xE0 -> process dies
            #     (the page / engine state does not tolerate external writes);
            #   * even with PP untouched, calling the command validator
            #     0x11C2420 (cmdwin.validate_declare_war) hits winject's stale
            #     scratch flag and takes the game down.
            # So the PE path is PURE READ + the game's own static cost model.
            # No injected calls, no writes, no post. `required_pp` stays None
            # and the reply says exactly why.
            import diplomacy as _D
            try:
                src = tag or self.player().tag
                _c = self.country(src)
                cur_pp = _c.political_power
                tgt = self.country(target_tag)
                acts = [x for x in _D.justifying(self)
                        if x.get("from") == src and
                        x.get("source_class") == "CTimedWargoalActivity"]
                concurrent = len(acts)
                state_ids = [int(x) for x in (states or [])]
                if not state_ids and wargoal in _D.WARGOAL_NEEDS_STATES:
                    state_ids = [s["id"] for s in self.states(target_tag)[:1]] \
                        if hasattr(self, "states") else [0]
                n = max(1, len(state_ids))
                days = _D._estimate_days(n, concurrent)
                total_pp = round(days * _D.WARGOAL_DAILY_PP, 1)
                return {
                    "ok": True, "mode": "pe-static-estimate",
                    "source": src, "target": target_tag, "wargoal": wargoal,
                    "states": state_ids,
                    "current_pp": cur_pp,
                    "required_pp": None,
                    "pp_shortfall": None,
                    "required_pp_note": (
                        "engine-exact PP threshold unavailable on PE: writing "
                        "political power and calling the diplomatic validator "
                        "both crash hoi4.exe (crashes #6/#7, 2026-11-16)"),
                    "can_justify_now": None,
                    "can_justify_now_note": (
                        "validator 0x11C2420 is fatal on this build; use "
                        "hoi4_justify_wargoal (the proven command path) to test"),
                    "allowed_by_rules": None,
                    "daily_pp_drain": _D.WARGOAL_DAILY_PP,
                    "concurrent_justifications": concurrent,
                    "estimated_days": days,
                    "estimated_completion_date": _D._add_game_days(self, days),
                    "estimated_total_pp": total_pp,
                    "estimate_source": ("00_invasion.txt: 200 base + 30 production "
                                        "+ 50/extra state, x1.5 per concurrent "
                                        "justification; 0.2 PP/day"),
                    "note": "pure-read estimate; nothing was written or posted",
                }
            except Exception as e:
                return {"ok": False, "unsupported_on_windows": True,
                        "error": "PE static quote failed: %s" % e,
                        "target": target_tag, "wargoal": wargoal}
        sp, sids = [], []
        if states:
            want = {int(x) for x in states}
            for st in _arr(self.p, self.country(target_tag).ptr, CO_STATES):
                sid = self.p.i32(st + ST_ID)
                if sid in want:
                    sp.append(st)
                    sids.append(sid)
            missing = want - set(sids)
            if missing:
                return {"ok": False,
                        "error": "these states do not belong to %s: %s" % (target_tag, sorted(missing))}
        return diplomacy.wargoal_quote(self, target_tag, wargoal, sp, sids, source_tag=tag)

    def justifying(self):
        import diplomacy
        return diplomacy.justifying(self)

    def diplomatic_actions(self):
        import diplomacy, diploscan
        cat = diplomacy.action_catalog()
        # 2026-11-13: action_catalog() already unwraps diploscan's "_all" map, so
        # cat["_all"] raised KeyError on every call. Read the raw build for the
        # complete class list instead.
        raw = diploscan.build()
        allmap = (raw or {}).get("_all") or {}
        return {"sendable": sorted(k for k in cat if k != "_all"),
                "all_classes": sorted(allmap)}

    def send_diplomatic_action(self, action_class, target_tag, dry_run=False):
        import diplomacy
        if sys.platform == "win32":
            return {"ok": False,
                    "blocked_by": "unsupported_platform",
                    "platform": "win32",
                    "unsupported_on_windows": True,
                    "alternatives": ["hoi4_justify_wargoal", "hoi4_declare_war"],
                    "error": "diplomatic action objects are built through the Linux "
                             "call path (setregs is missing on the PE backend); "
                             "only hoi4_justify_wargoal / hoi4_declare_war have native Windows channels.",
                    "action": action_class, "target": target_tag}
        ok, err = diplomacy.send_action(self, action_class, target_tag, dry_run=dry_run)
        return {"ok": ok, "error": err}

    def declare_war(self, target_tag, dry_run=False, force=False, override_checks=False,
                    ack_co_belligerents=False):
        """Declare war -- behind a preflight covering army, border, battle plan,
        casus belli, AND diplomatic escalation (factions / guarantees / subjects).

        `force` skips advisory warnings (rifles, division ratio, timeline).
        `ack_co_belligerents=True` acknowledges undefended co-belligerent borders
        (e.g. a guarantor/faction ally with 0 divisions on our shared border).
        `override_checks=True` bypasses the hard military/wargoal preflight gates.
        Note: diplomatic illegality (self, already at war, same faction ally,
        own subject/overlord) is still checked unless override_checks=True.
        """
        readiness = self.war_readiness_check(
            target_tag, force=force,
            hard=not override_checks,
            ack_co_belligerents=bool(ack_co_belligerents or override_checks),
        )
        _diplo_summary = {
            "target_faction": readiness.get("target_faction"),
            "guaranteed_by": readiness.get("guaranteed_by") or [],
            "overlord": readiness.get("overlord"),
            "subjects": readiness.get("subjects") or [],
            "co_belligerents": readiness.get("co_belligerents") or [],
            "new_co_belligerents": readiness.get("new_co_belligerents") or [],
            "co_belligerent_borders": readiness.get("co_belligerent_borders") or [],
            "total_enemy_divisions_est": readiness.get("total_enemy_divisions_est"),
        }
        if not readiness["ready"] and not dry_run:
            return {"ok": False, "blocked_by": "war_preflight",
                    "error": "; ".join(readiness["blockers"] or readiness["warnings"]),
                    "readiness": readiness,
                    **_diplo_summary,
                    "hint": ("Check readiness.blockers: draw battle plan with "
                             "hoi4_army_battleplans(enemy_tag=%s), verify casus belli, "
                             "and guard borders of any co-belligerents (guarantors / "
                             "faction allies) listed in co_belligerent_borders. "
                             "ack_co_belligerents=true confirms undefended co-belligerent "
                             "borders; override_checks=true bypasses hard gates."
                             % str(target_tag).upper())}
        import diplomacy
        import combat
        import time as _t
        if sys.platform == "win32":
            if dry_run:
                # BUG-08 (playtest 2026-11-15): dry_run used to fall through to the
                # real declaration -- a "probe" started the Dutch war. A dry run now
                # only evaluates the player-side gate and posts nothing.
                return {"ok": bool(readiness.get("ready")), "dry_run": True,
                        "posted": 0, "target": target_tag,
                        "readiness": readiness,
                        **_diplo_summary,
                        "warnings": readiness.get("warnings") or [],
                        "note": "dry_run: no command was built or posted"}
            # Real action channel on the PE build: CDeclareWarAction wrapped in a
            # CDiplomaticActionCommand.  The availability gate (action+0x70) is
            # bypassed, so war_readiness_check stays the only guard.
            import cmdwin
            c = self._c(target_tag)
            r = cmdwin.declare_war(self, c.tag_id)
            self._settle()
            # combat.war_relations scans live CWarRelation objects, but the object
            # index is a snapshot -- refresh=True is what makes a just-declared war
            # visible (a full rescan costs ~25 s, hence one shot rather than a poll).
            verified = None
            if r.get("ok"):
                try:
                    _t.sleep(2.0)
                    verified = target_tag.upper() in combat.enemy_tags(
                        self, self.player_tag(), refresh=True)
                except Exception:
                    verified = None
            return {"ok": bool(r.get("ok")), "posted": r.get("posted"),
                    "target": target_tag, "target_id": c.tag_id,
                    "action": r.get("act"), "cmd": r.get("cmd"),
                    "at_war_verified": verified,
                    **_diplo_summary,
                    "warnings": readiness.get("warnings") or []}
        cb = None if dry_run else diplomacy.find_wargoal(self, target_tag)
        ok, err = diplomacy.declare_war(self, target_tag, dry_run=dry_run)
        # 2026-11-13: CCommand::Run only queues the action, so a `true` return means
        # "CanExecute accepted and the command was posted", not "war exists". Poll
        # the war-relation table so the caller never has to guess.
        verified = None
        if ok and not dry_run:
            for _ in range(10):
                _t.sleep(1.0)
                try:
                    if target_tag in combat.enemy_tags(self):
                        verified = True
                        break
                except Exception:
                    pass
            if verified is None:
                verified = False
        return {"ok": ok, "error": err, "readiness": readiness,
                "wargoal": cb, "at_war_verified": verified,
                **_diplo_summary,
                "warnings": readiness.get("warnings") or []}

    def country_characters(self, tag=None):
        """All of the country's characters: leaders, generals, admirals, advisors."""
        p = self.p
        out = []
        for ch in self.all_characters(tag):
            out.append({"key": item_name(p, ch + CH_KEY),
                        "name": item_name(p, ch + CH_NAME),
                        "is_military_leader": bool(p.u64(ch + self.CH_ARMY_LEADER)),
                        "ptr": hex(ch)})
        return {"count": len(out), "characters": out}

    def divisions(self, tag=None, limit=200, deep=False):
        """The country's divisions (CArmy in the game's code).

        2026-11-16: this used to read `CCountry+0x290` of whatever pointer
        `country(tag)` handed back.  After the 1939.05.07 crash-restart that
        pointer was not Germany's, so the count was 0 while 109 divisions stood
        in the Reich and every land-war tool answered "no divisions".
        Resolution now goes through `divenum`, which matches the tag id the
        CCountry object carries itself, and `deep=True` adds an RTTI fallback
        when the array is empty.  `count` is never silently zero any more: a
        failed resolution raises, and a disagreement with the deep scan is
        reported in `warning`.
        """
        import divenum
        p = self.p
        c = self._c(tag)
        me = c.tag
        info = divenum.armies(self, me)
        rows = info["armies"]
        count = info["count"]
        source = info["source"]
        warning = None
        if count <= 0 and deep:
            try:
                hits = divenum.deep_scan(self, me)
            except Exception as e:
                hits = []
                warning = "deep scan failed: %r" % (e,)
            if hits:
                warning = ("CCountry+0x290 is empty but %d CArmy object(s) stand in "
                           "provinces of states %s owns" % (len(hits), me))
                rows, count, source = hits, len(hits), "rtti-scan"
        out = []
        for i, a in enumerate(rows[:limit]):
            tmpl = p.u64(a + 0x3B8)
            out.append({"index": i, "ptr": hex(a),
                        "template": item_name(p, tmpl + DT_NAME) if tmpl else None,
                        "province_ptr": hex(p.u64(a + 0x1F0))})
        res = {"count": int(count), "divisions": out, "source": source,
               "country_ptr": hex(info["ptr"]), "tag_id": info["tid"],
               "verified": bool(info.get("verified"))}
        if warning:
            res["warning"] = warning
        return res

    def army_status(self, tag=None, deep=False):
        """One verified answer to "how many divisions do I have, and where".

        `hoi4_army_status` used to be `divisions()["count"]` plus three other
        reads, so a bad country pointer showed up as a calm `0`.  Any caller
        that gates a declaration of war on this number gets `countable: false`
        instead of a lie.

        `deep=True` adds the RTTI fallback.  It is off by default because the
        first `CArmy` vtable scan of a game process takes ~40 s (afterwards it
        is cached); `hoi4_division_selftest` is the tool that pays that cost on
        purpose, and `war_readiness_check` pays it only when it sees a zero.
        """
        import divenum
        c = self._c(tag)
        me = c.tag
        d = self.divisions(me, limit=1, deep=deep)
        try:
            armies = self.order_groups(me)
        except Exception as e:
            armies = {"count": 0, "groups": [], "error": repr(e)}
        try:
            unassigned = len(self.unassigned_divisions(me))
        except Exception as e:
            unassigned = None
            d.setdefault("warning", "unassigned_divisions failed: %r" % (e,))
        try:
            conveyors = self.conveyors(me)
        except Exception:
            conveyors = []
        res = {
            "tag": me,
            "armies": armies,
            "divisions_count": d["count"],
            "unassigned_divisions": unassigned,
            "conveyors": conveyors,
            "source": d.get("source"),
            "countable": bool(d.get("verified")),
            "country_ptr": d.get("country_ptr"),
            "tag_id": d.get("tag_id"),
            "selftest": divenum.selftest(self, me, deep=deep),
        }
        if d.get("warning"):
            res["warning"] = d["warning"]
        return res


    # ============================================================ PROJECTS
    def scientists(self, tag=None, limit=60):
        p = self.p
        c = self._c(tag)
        pref = (c.tag + "_").lower()
        out = []
        if sys.platform == "win32":
            # oi.instances("CScientist") is empty on the PE build; the country's
            # own roster is CCountryCharacters+0xA0 (GER 17) plus +0xE8 (4).
            cc = p.u64(c.ptr + 0xFF0)
            pool = list(_arr(p, cc, 0xA0)) + list(_arr(p, cc, 0xE8)) if cc else []
        else:
            pool = list(self.oi.instances("CScientist"))
        for s in pool:
            nm = None
            for off in range(0, 0x120, 8):
                v = item_name(p, s + off)
                if v and v.isprintable() and 2 < len(v) < 48:
                    nm = v
                    break
            if nm and (not pref or nm.lower().startswith(pref) or True):
                out.append({"name": nm, "ptr": hex(s)})
            if len(out) >= limit:
                break
        return out

    def special_projects(self, tag=None):
        """Special-project pool.

        2026-11-16: CO_PROJECT_POOL / CO_PROGRAM_STATUS read a null pointer and an
        unrelated CArmyUpgradesStatus on the PE build, and
        'CSpecialProjectsConsumer' (the real owner) is not reachable from
        CCountry (scanned 0x0..0x3200, 0 hits). Report that instead of handing
        back `pool: null` plus a bare pointer string.
        """
        p = self.p
        c = self._c(tag)
        if sys.platform == "win32":
            # PE 1.19.5.0: CCountry has no CProjectPool pointer (CCountry+0xFA8 is
            # CProgramStatus, which holds *faction facility programmes* such as
            # "land_facility", not special projects).  CProjectPool is an RTTI
            # class, so it is located by vtable scan instead.
            pools = self.win_scan_class("CProjectPool")
            prog = p.u64(c.ptr + 0xFA8)
            res = {"ok": True, "available": True, "source": "pe-rtti vtable scan",
                   "pool": None, "pool_instances": len(pools),
                   "program_status": hex(prog) if prog else None,
                   "program_status_class": self.oi.class_of(prog) if prog else None,
                   "programs": []}
            for pr in _arr(p, prog, 0x20) if prog else []:
                nm = item_name(p, pr + 0x20)
                res["programs"].append({"program": nm, "ptr": hex(pr)})
            if pools:
                res["note"] = ("CProjectPool resolves by vtable (%d live instances, one per "
                               "country) but no field inside it points back at a CCountry, so "
                               "a pool cannot be attributed to a tag yet. `programs` is the "
                               "verified part: CCountry+0xFA8 CProgramStatus -> CPdxArray"
                               "<CProgram*> at +0x20, program key at +0x20." % len(pools))
            return res
        pool = p.u64(c.ptr + CO_PROJECT_POOL)
        prog = p.u64(c.ptr + CO_PROGRAM_STATUS)
        if sys.platform == "win32" and not pool:
            return {"ok": False, "available": False, "pool": None,
                    "program_status": None,
                    "error": "special_projects has no verified container on the PE build",
                    "detail": "CCountry+CO_PROJECT_POOL is null and "
                              "CSpecialProjectsConsumer is not reachable from CCountry",
                    "program_status_class": self.oi.class_of(prog) if prog else None}
        res = {"pool": hex(pool) if pool else None, "program_status": hex(prog) if prog else None}
        if pool:
            from probe import describe
            res["pool_arrays"] = [r for r in describe(p, pool, self.oi, span=0x200) if r["size"]]
        return res

    # ============================================================ DOCTRINE
    def tech_folders(self):
        """Technology and doctrine folders."""
        p = self.p
        db = self.oi.singleton("CTechnologyDatabase")
        out = []
        for f in _arr(p, db, 0xA8):
            nm = None
            for off in range(0, 0x200, 8):
                v = item_name(p, f + off)
                if v and v.isprintable() and 2 < len(v) < 48:
                    nm = v
                    break
            out.append({"name": nm, "ptr": hex(f)})
        return out

    # ========================================================= FOCUS DETAIL
    def focus_detail(self, key, tag=None):
        """Everything about one focus: prerequisites, mutual exclusions, filters.

        2026-11-18: prerequisites and mutual exclusions used to be read out of
        the live CFocus object through NF_PREREQ / NF_MUTEX, whose offsets were
        never verified on the PE build -- the result was binary garbage
        (`prerequisite_keys` full of mojibake). The authoritative source is the
        game's own script, so parse that (focusplan) and use memory only for the
        live pointer / name / completion state.
        """
        import focusplan
        stat = (focusplan.load().get("focuses") or {}).get(key)
        try:
            f = self.find_focus(key, tag)
        except Exception:
            f = None
        if not stat and not f:
            raise KeyError("no such focus: %s" % key)
        info = {}
        if f:
            try:
                info = dict(self._focus_info(f) or {})
            except Exception:
                info = {}
        info.setdefault("id", key)
        info.setdefault("key", key)
        groups = (stat or {}).get("prereq_groups") or []
        info["prerequisite_groups"] = groups          # outer list = AND, inner = OR
        info["prerequisite_keys"] = [x for g in groups for x in g]
        info["prerequisite_logic"] = (
            " AND ".join("(%s)" % " OR ".join(g) for g in groups) if groups else "")
        info["mutually_exclusive_keys"] = (stat or {}).get("mutex") or []
        if stat:
            info["days"] = stat.get("days")
            info["resolution_1936"] = bool(stat.get("research_slot"))
        info["source"] = "script" if stat else "memory"
        info["ptr"] = hex(f) if f else None
        return info

    def focus_progress(self, tag=None):
        p = self.p
        prog = p.u64(self._c(tag).ptr + CO_FOCUS_PROGRESS)
        cur = self.current_focus(tag)
        days_done = round(p.i64(prog + 0x38) / float(O.FIXED), 3) if prog else 0.0
        cost_days = cur.get("cost_days") if isinstance(cur, dict) else None
        if cur and not cost_days:
            cost_days = self._focus_cost_days(cur.get("key"))
        days_left = (
            round(max(0.0, cost_days - days_done), 2)
            if (cur and cost_days and cost_days > 0)
            else None
        )
        pct = (
            round(min(100.0, days_done / cost_days * 100.0), 1)
            if (cur and cost_days and cost_days > 0)
            else None
        )
        # 2026-11-22 (复测 5 §3.6): 补齐 progress / days_done / cost_days / days_left / percent，
        # 修复 turnloop.snapshot() 读 `progress` 恒得 0.0 且无处读取国策进度的问题；保留 raw_0x38 兼容。
        return {
            "current": cur,
            "progress": days_done,
            "days_done": round(days_done, 2),
            "cost_days": cost_days,
            "days_left": days_left,
            "percent": pct,
            "banked_days": round(days_done, 2) if not cur else 0.0,
            "raw_0x38": days_done if prog else None,
            "completed": _count(p, prog, FP_COMPLETED) if prog else 0,
        }

    # 2026-11-20 (P0 #4): CSetNationalFocusCommand::CanExecute on hoi4.exe.
    # vtable +0x48 — the same slot CAddAdvisorCommand / CSetResearchCommand use.
    # Disassembled at 0x11F2940: it reads cmd+0x30 (the CNationalFocus*) and
    # cmd+0x28 (the CCountryTag), resolves the tag through CCountries::Get
    # (0xC07BF0), then tail-calls the focus's own `available` evaluator
    # (focus->vt[0x48]) with the country context.  Pure read — the game runs it
    # on every focus-button repaint, so calling it cannot start anything.
    # NOTE: vtable+0x50 (0x11E63E0) is a DIFFERENT, weaker predicate
    # (`country->CanSelectFocus`) which answers 0 for every focus while another
    # one is already running; probing that one was the first guess and it was
    # wrong.
    VCAN_FOCUS_WIN = 0x11F2940

    def _can_start_focus_win(self, focus_ptr, tag_id):
        """Engine truth on the PE build.  None = the probe itself failed."""
        import cmdwin as W
        try:
            cmd = W.make_national_focus(self, int(focus_ptr), int(tag_id))
        except Exception:
            return None
        try:
            return bool(self.call(self.p.base + self.VCAN_FOCUS_WIN, (cmd, 0),
                                  absolute=True, force=True) & 0xFF)
        except Exception:
            return None
        finally:
            try:
                W.free(self, cmd)
            except Exception:
                pass

    def _can_start_focus_cmd(self, focus_ptr, tag_id):
        """Would the ENGINE let this country start this focus right now?

        CSetNationalFocusCommand::CanExecute is slot +0x50 of its vtable
        (0x4416DD8, 1.19.4.1) — the same entry CCommand::Run calls before it
        posts. Verified 2026-11-11 against the live game: it returned 1 for
        GER_befriend_japan and 0 for GER_legion_condor, and set_focus_ptr()
        then succeeded / failed exactly the same way.
        """
        if sys.platform == "win32":
            return self._can_start_focus_win(focus_ptr, tag_id)
        import api, commands
        try:
            spec = commands.COMMANDS["SetNationalFocus"]
            vt = self.va(spec["vt"])
            if vt < self.p.base or vt > self.p.base + 0x3900000:
                return True          # platform without the Linux command vtable
            can_fn = self.p.u64(vt + 0x50) - self.p.base
        except OSError:
            return True
        api.allow_call(can_fn)
        cmd = self.cmd.make("SetNationalFocus", tag=int(tag_id),
                            focus_ptr=int(focus_ptr))
        try:
            with self:
                return bool(self.call(can_fn, (cmd, 0)) & 0xFF)
        except Exception:
            return True          # never hide a focus because of a probe error
        finally:
            try:
                self._free_cmd(cmd)
            except Exception:
                pass

    def selectable_focuses(self, tag=None, allow_civil_war=False):
        """Selectable focuses, ranked by the country's own historical AI plan.

        `selectable` is still the set that passes the live prerequisite/mutex check;
        the ranking comes from common/ai_strategy_plans (ai_national_focuses) plus
        static reward flags from common/national_focus.  No game calls.
        """
        import focusplan
        p = self.p
        c = self._c(tag)
        prog = p.u64(c.ptr + CO_FOCUS_PROGRESS)
        if not prog:
            return {"recommended": None, "recommended_reason": "", "count": 0,
                    "selectable": [], "focuses": [], "blocked": []}
        completed = set()
        for f in _arr(p, prog, FP_COMPLETED):
            k = item_name(p, f + NF_KEY)
            if k:
                completed.add(k)
        current = p.u64(prog + FP_CURRENT)
        # The full ranking costs ~15 s on the PE build (two item_name reads per
        # focus over 438 focuses), and hoi4_overview asks for it twice (alerts ->
        # candidates).  Memoise on the three things that can change the answer.
        # 2026-11-20: the key did not contain the COUNTRY, so asking about one
        # country and then another on the same day returned the first country's
        # focus list.  Two different trees, one cache slot.
        ckey = (p.pid, c.tag, self.date_raw(), current, len(completed))
        cached = getattr(self, "_sel_cache", None)
        if cached and cached[0] == ckey and not allow_civil_war:
            return cached[1]

        blacklist = set()
        try:
            import playbooks
            blacklist = playbooks.blacklist(c.tag)
        except Exception:
            blacklist = set()
        if c.tag == "GER" and not allow_civil_war:
            blacklist |= {"GER_oppose_hitler", "GER_oppose_hitler_ww"}

        data = focusplan.load()
        meta = data.get("focuses", {})
        plan = data.get("plans", {}).get(c.tag, [])
        plan_pos = {k: i for i, k in enumerate(plan)}
        # The historical AI plan puts GER_anschluss at #25 and GER_demand_sudetenland
        # at #29: following it literally spends 3.6 years on propaganda/industry and
        # never annexes anything (28 focuses, 0 snowball, measured 2026-11-05).
        # These two tables jump the annexation chain to the top as soon as the
        # country can actually pass its manpower/peace conditions.
        my_divs = len(_arr(p, c.ptr, CO_ARMIES))
        try:
            yr = int(self.date().get("year") or 0)
        except Exception:
            yr = 0
        try:
            import combat as _cb
            at_war = bool(_cb.at_war(self, c.tag))
        except Exception:
            at_war = False
        rows, blocked = [], []
        try:
            import playbooks as _pb
            pb_scores = _pb.focus_scores(c.tag, yr, my_divs, at_war)
        except Exception:
            pb_scores = {}
        for f in self.focus_tree(tag):
            k = item_name(p, f + NF_KEY)
            if not k or k in completed or f == current:
                continue
            if k in blacklist:
                blocked.append(k)
                continue
            ok = True
            if sys.platform == "win32":
                # 2026-11-19 (live): PE cannot read the prerequisite/mutex blocks
                # from CNationalFocus, and this branch used to just SKIP the
                # filter -- so `selectable` was the whole tree (427 of 438 on a
                # 1936 save).  Use the parsed script data (focusplan schema 2),
                # the same source hoi4_focus_list(only_startable) already trusts.
                m0 = meta.get(k) or {}
                groups = [g for g in (m0.get("prereq_groups") or []) if g]
                if any(not any(g in completed for g in grp) for grp in groups):
                    ok = False
                if any(x in completed for x in (m0.get("mutex") or [])):
                    ok = False
            else:
                for group in self._focus_prereq_groups(f):
                    if group and not any(g in completed for g in group):
                        ok = False
                        break
                if any(m in completed for m in self._focus_mutex_keys(f)):
                    ok = False
            if not ok:
                continue
            m = meta.get(k, {})
            score, tier, reason = 0, "D_other", ""
            if k in plan_pos:
                score = 1000 - min(plan_pos[k], 100)
                tier = "S_historical"
                reason = "历史主线优先顺位 #%d" % (plan_pos[k] + 1)
            elif m.get("research_slot"):
                score, tier, reason = 300, "A_research_slot", "奖励额外科研槽"
            elif m.get("annex") or m.get("wargoal"):
                score, tier, reason = 300, "A_annex", "政治吞并/宣战目标"
            elif m.get("industry"):
                score, tier, reason = 150, "B_industry", "工业主线（民工/军工）"
            elif m.get("air_naval"):
                score, tier, reason = 0, "C_air_naval", "空军/海军支线，默认排后"
            if c.tag == "GER":
                sb = self.GER_SNOWBALL_CHAIN.get(k)
                if sb:
                    sb_score, min_d, min_y, need_peace = sb
                    if (need_peace and at_war) or my_divs < min_d or yr < min_y:
                        blocked.append("%s (wait: divs %d/%d, year %d/%d, at_war=%s)"
                                       % (k, my_divs, min_d, yr, min_y, at_war))
                        continue
                    score, tier = sb_score, "S0_snowball"
                    reason = "德国史实吞并雪球核心主线 (divs=%d >= %d)" % (my_divs, min_d)
                elif k in self.GER_OPENING_TOP:
                    score, tier, reason = (self.GER_OPENING_TOP[k], "S0_opening",
                                           "德国开局核心工业/政治主线")
                elif k in self.GER_LOW_PRIORITY:
                    score, tier, reason = (self.GER_LOW_PRIORITY[k], "Z_filler",
                                           "可重复填充国策，让位给吞并链")
            if k in pb_scores and pb_scores[k] > score:
                score, tier, reason = (pb_scores[k], "S0_playbook", "老手攻略时间轴")
            rows.append({
                "id": k, "key": k, "name": item_name(p, f + NF_NAME),
                "ptr": hex(f), "tier": tier, "score": score, "days": m.get("days", 70),
                "reason": reason, "_plan": plan_pos.get(k, 999),
            })
        # 2026-11-11: the prerequisite/mutex scan above cannot evaluate the
        # `available` trigger block (GER_anschluss needs 250k manpower and
        # rifles; GER_currency_reforms needs a law), which is why the tool used
        # to advertise focuses the engine then refused — the previous run spent
        # dozens of turns clicking them. Ask the engine instead. Only while no
        # focus is running: with one in progress the engine refuses everything
        # else, so the answer would be a useless wall of "no".
        engine_checked = False
        engine_unknown = 0
        if rows and not current:
            keep = []
            # 2026-11-21 (续测 P1-7): this loop fires one g.call (freeze + hijack)
            # per candidate.  It measured >50 s and blew the watchdog, because
            # each probe also sampled the date twice at 13.7 s a go.  The date is
            # cheap now, but the probes are still injections: bound them by wall
            # clock so hoi4_selectable_focuses can never exceed the budget.  Once
            # the deadline passes the remaining candidates are returned as
            # unverified instead of being silently dropped.
            import time as _tf
            _deadline = _tf.time() + 8.0
            for r in rows:
                if _tf.time() > _deadline:
                    engine_unknown += 1
                    keep.append(r)
                    continue
                v = self._can_start_focus_cmd(int(r["ptr"], 16), c.tag_id)
                if v is None:
                    engine_unknown += 1
                    keep.append(r)
                elif v:
                    keep.append(r)
                else:
                    blocked.append("%s (engine: available-trigger / manpower / "
                                   "political-power gate not met)" % r["id"])
            rows = keep
            engine_checked = (engine_unknown == 0)
        rows.sort(key=lambda r: (-r["score"], r["_plan"], r["id"]))
        for r in rows:
            r.pop("_plan", None)
        recommended = rows[0]["id"] if rows else None
        res = {
            "recommended": recommended,
            "recommended_reason": rows[0]["reason"] if rows else "",
            "count": len(rows),
            "engine_checked": engine_checked,
            "focus_in_progress": item_name(p, current + NF_KEY) if current else None,
            "selectable": rows[:60],
            # 2026-11-22 (复测 5 §3.5): 旧代码把完整对象列表在 `selectable` 和 `focuses`
            # 重复返回了两遍（体积翻倍）；现保留 `selectable` 为完整对象列表，`focuses`
            # 改为仅 key 列表兼顾旧调用方且消除冗余。
            "focuses": [r["id"] for r in rows[:60]],
            "blocked": blocked[:40],
        }
        if sys.platform == "win32":
            # 2026-11-20 (P0 #4): the engine check runs here now.  Prerequisites
            # and mutexes stay on the parsed script data (CNationalFocus does not
            # expose them as a pointer array), but everything that survives that
            # filter is asked of CSetNationalFocusCommand::CanExecute, so the
            # `available` trigger (manpower / rifles / laws / war state / PP) is
            # the engine's answer and not a guess.
            res["verified"] = bool(engine_checked)
            if engine_checked:
                res["note"] = ("PE build: candidates passed the script "
                               "prerequisite/mutex filter AND the engine's own "
                               "CSetNationalFocusCommand::CanExecute (its "
                               "`available` evaluator), through a dry-run command "
                               "that is never posted.")
            elif current:
                res["note"] = ("PE build: a focus is already in progress, so the "
                               "engine refuses every other focus and the CanExecute "
                               "probe is skipped. This is the ranked static "
                               "candidate list; re-run once the slot is free to get "
                               "an engine-verified set.")
            else:
                res["note"] = ("PE build: %d candidate(s) could not be probed "
                               "(the CanExecute call failed) and are listed "
                               "unverified; the rest are engine-checked."
                               % engine_unknown)
            res["count"] = len(rows)
            res["truncated"] = max(0, len(rows) - 60)
        if not allow_civil_war:
            self._sel_cache = (ckey, res)
        return res

    def completed_focuses(self, tag=None):
        p = self.p
        prog = p.u64(self._c(tag).ptr + CO_FOCUS_PROGRESS)
        if not prog:
            return []
        return [item_name(p, f + NF_KEY) for f in _arr(p, prog, FP_COMPLETED)]

    def continuous_focuses(self, tag=None):
        """The 1.19.4.1 continuous-focus catalogue (no completion needed).

        CO_CONT_FOCUS_PAL (+0x1340) is a CContinuousFocusPalette -- a GUI object
        with a std::string at +0x08 and one selection slot at +0x00 -- and holds
        no focus list at all, which is why the old find_arrays() walk returned []
        for every country. The real catalogue is the global
        CContinuousNationalFocus database (14 definitions, key at +0x18, display
        name at +0x38). Verified live 2026-11-11.
        """
        p = self.p
        out = []
        for d in self.oi.instances("CContinuousNationalFocus"):
            k = item_name(p, d + 0x18)
            if not k:
                continue
            out.append({"key": k, "id": item_name(p, d + 0x58) or k,
                        "name": item_name(p, d + 0x38),
                        "ptr": hex(d), "generic": k.startswith("continuous_")})
        out.sort(key=lambda r: (not r["generic"], r["key"]))
        return out

    def set_continuous_focus(self, key, tag=None):
        """Select a continuous focus, and tell the truth about the result.

        2026-11-11: the definition pointer is what the old code passed, and the
        engine refuses it (CanExecute 0) -- the operand has to be the country's
        own CContinuousFocusItem, which this build only materialises while the
        political screen is built, so it cannot be fabricated from Python. The
        tool therefore reports `queued` from CCommand::Run and never claims
        `ok` without it, instead of the old unconditional {"ok": True}.
        """
        c = self._c(tag)
        rows = self.continuous_focuses(c.tag)
        row = next((r for r in rows if key in (r["key"], r["id"])), None)
        if row is None:
            raise KeyError("no such continuous focus: %s (available: %s)"
                           % (key, ", ".join(r["key"] for r in rows)))
        if not row["generic"]:
            return {"ok": False, "focus": key, "queued": False,
                    "error": "%s is a country-specific continuous focus and is "
                             "not offered to %s" % (key, c.tag)}
        cmd = self.cmd.make("SetContinuousFocus", tag=c.tag_id,
                            focus_ptr=int(row["ptr"], 16))
        rv = self.cmd.post(cmd)
        self._settle(0.4)
        ok = bool(rv & 0xFF) if isinstance(rv, int) else False
        return {"ok": ok, "focus": key, "queued": ok, "verified": False,
                "error": "" if ok else
                ("engine refused CSetContinuousFocusCommand::CanExecute: this "
                 "build wants the per-country CContinuousFocusItem, not the "
                 "definition pointer; select it in the UI instead")}

    def drop_continuous_focus(self, tag=None):
        c = self._c(tag)
        cmd = self.cmd.make("DropContinuousFocus", tag=c.tag_id)
        rv = self.cmd.post(cmd)
        self._settle(0.4)
        return {"ok": bool(rv & 0xFF) if isinstance(rv, int) else False,
                "error": getattr(self.cmd, "last_error", "")}

    # ================================================== GENERIC COMMAND ACCESS
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

    # Note: send_command is provided by ResolverMixin (with name/handle resolution).

    # ======================================================= EQUIPMENT STOCKPILE
    PS_STOCKPILE = 0x220   # CPdxArray<{CEquipmentVariant*; int64 count x1e5}>

    def equipment_stockpile(self, tag=None, limit=120):
        """Equipment counts in the stockpile, per VARIANT (not per equipment name).

        2026-11-21 (P2-11): the 2026-11-20 playtest read
            infantry_equipment_0: 975
            infantry_equipment_0: 188
            infantry_equipment_1: 24505 / 4435 / 100
        and could not tell whether those were separate variants or double
        counting -- `equipment` is the ARCHETYPE name, and one archetype owns one
        row per variant (captured / lend-leased / obsolete marks).  Keep one row
        per variant, name the variant pointer, and sum per archetype so "how many
        rifles do I have" has an answer.
        """
        p = self.p
        self._load_equipment()
        ps = p.u64(self._c(tag).ptr + CO_PRODUCTION)
        n = p.i32(ps + self.PS_STOCKPILE + O.PDXARRAY_SIZE)
        base = p.u64(ps + self.PS_STOCKPILE + O.PDXARRAY_DATA)
        out = []
        totals = {}
        if base and n:
            raw = p.try_read(base, 16 * n) or b""
            for i in range(min(n, len(raw) // 16, limit)):
                vp, cnt = struct.unpack_from("<qq", raw, i * 16)
                nm = self._eq_by_id.get(p.i32(vp + EV_TYPE_ID)) if vp else None
                val = round(cnt / O.FIXED, 1)
                out.append({"equipment": nm, "variant_ptr": hex(vp) if vp else None,
                            "count": val})
                if nm:
                    totals[nm] = round(totals.get(nm, 0.0) + val, 1)
        out.sort(key=lambda r: (str(r.get("equipment")), -float(r.get("count") or 0)))
        # variants that can be produced
        variants = []
        for v in _arr(p, ps, PS_VARIANTS_ACTIVE)[:limit]:
            nm = self._eq_by_id.get(p.i32(v + EV_TYPE_ID))
            if nm:
                variants.append({"equipment": nm, "variant_ptr": hex(v)})
        return {"stockpile": out, "totals_by_equipment": totals,
                "available_variants": variants,
                "note": ("one row per VARIANT; several rows can share an "
                         "`equipment` name (captured / lend-lease / obsolete). "
                         "Use totals_by_equipment for the real count.")}

    # ================================================ VERITABANI GEZGINI
    DATABASE_ARRAYS = {
        "war_goals": ("CWarGoalDatabase", 0x40, None),
        "occupation_laws": ("COccupationLawDatabase", 0x58, None),
        "buildings": ("CBuildingDatabase", getattr(O, "BLD_DB_ITEMS", 0x40), BT_KEY),
        "technologies": ("CTechnologyDatabase", 0x48, TT_NAME),
        "ideas": ("CIdeaDatabase", 0x68, IDEA_NAME),
        "decisions": ("CDecisionDatabase", 0x28, DECISION_NAME),
        "equipment": ("CEquipmentDatabase", 0x68, ET_NAME),
        "focuses": ("CNationalFocusDatabase", 0x58, NF_KEY),
        "character_templates": ("CCharacterTemplateDatabase", 0x28, None),
        "scripted_diplomatic_actions": ("CScriptedDiplomaticActionTemplateDatabase", 0x160, None),
    }

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

    # ============================================================== FULL REPORT
    def full_report(self, tag=None):
        """The wide status report to read at the start of a turn."""
        c = self._c(tag)
        base = self.overview(c.tag)
        base["resources"] = self.resources(c.tag)
        # B2/C2 (review 2026-11-07): `country_stats` is built on the 1.19.2
        # stats.pkl table and is now refused by `_scope_call`; calling it here
        # killed the process. Report the refusal instead of the numbers.
        try:
            base["stats"] = self.country_stats(c.tag)
        except Exception as e:
            base["stats"] = {}
            base["stats_unavailable"] = str(e)
        base["armies"] = self.armies(c.tag)["count"]
        base["division_templates"] = [t["name"] for t in self.division_templates(c.tag)]
        base["fleets"] = [f["name"] for f in self.fleets(c.tag)]
        if sys.platform == "win32" and getattr(O, "PO_COMMAND_POWER", None) is not None and c.politics:
            base["command_power"] = self.p.i64(c.politics + O.PO_COMMAND_POWER) / O.FIXED
        else:
            base["command_power"] = self.p.i64(c.ptr + CO_COMMAND_POWER) / O.FIXED
        # B2: `selectable_focuses` returns a dict, so `for f in ...` iterated the
        # KEYS and `f["key"]` raised TypeError: string indices must be integers.
        _sf = self.selectable_focuses(c.tag)
        _sel = _sf.get("selectable", []) if isinstance(_sf, dict) else (_sf or [])
        base["selectable_focuses"] = [
            (f["key"] if isinstance(f, dict) else str(f)) for f in _sel
        ]
        base["completed_focuses"] = [f for f in self.completed_focuses(c.tag) if f]
        base["faction"] = self.faction(c.tag)
        base["stockpile"] = self.equipment_stockpile(c.tag)["stockpile"]
        return base


_VERIFIED_NAMES = set()
try:
    from commands import COMMANDS as _CMDS
    _VERIFIED_NAMES = {"C" + k + "Command" for k in _CMDS}
except Exception:
    pass

# mix into Game
for _n in dir(MacroMixin):
    if not _n.startswith("__"):
        setattr(Game, _n, getattr(MacroMixin, _n))


# ============================================================================
#  STATE DETAIL, DECISION AND IDEA DETAIL, AND ACTION WRAPPERS
# ============================================================================
ST_PROVINCES        = getattr(O, "ST_PROVINCES", 0x08)     # CPdxArray<CProvince*>
ST_BUILDING_LEVELS  = 0x128    # 56-entry building level array
ST_BUILDING_MAX     = 0x140
ST_BUILDINGS        = getattr(O, "ST_BUILDINGS", 0x158)    # CPdxArray<CBuilding*>
BG_LEVEL            = getattr(O, "BG_LEVEL", 0x40)         # int16, building level
ST_RESISTANCE       = getattr(O, "ST_RESISTANCE", 0x278)   # int64 x1e5, None = unknown
ST_COMPLIANCE       = getattr(O, "ST_COMPLIANCE", 0x2A8)   # int64 x1e5, None = unknown


class MacroMixin2:
    # ============================================================= STATES
    def state_detail(self, state_id: int, tag=None):
        p = self.p
        self._load_buildings()
        # 2026-11-23 (复测 6 P2-1): 传了 tag 时先查该国 CO_STATES；找不到时自动回退全局
        # 遍历所有国家，避免查傀儡/占领/他国州（如 state 9 归属 RKH）直接抛裸 KeyError。
        # 全局仍找不到时返回结构化错误 JSON，不再向 MCP 客户端吐 traceback。
        _found = None
        _owner_tag = None
        req_tag = str(tag).upper() if tag else None
        if req_tag:
            try:
                _co = self._c(req_tag)
                for s in _arr(p, _co.ptr, CO_STATES):
                    if p.i32(s + ST_ID) == state_id:
                        _found = s
                        _owner_tag = _co.tag
                        break
            except Exception:
                _found = None
        if _found is None:
            try:
                for c in self.countries():
                    for s in _arr(p, c.ptr, CO_STATES):
                        if p.i32(s + ST_ID) == state_id:
                            _found = s
                            _owner_tag = c.tag
                            break
                    if _found:
                        break
            except Exception:
                _found = None
        if _found is None:
            return {
                "ok": False,
                "error": "state not found: %s" % state_id,
                "state": int(state_id),
                "requested_tag": req_tag,
            }
        s = _found
        blds = {}
        for b in _arr(p, s, ST_BUILDINGS):
            nm = self._bld_by_id.get(p.i32(b + BG_TYPE_ID))
            if nm:
                blds[nm] = struct.unpack("<h", p.read(b + BG_LEVEL, 2))[0]
        # 读出真实核心国家列表（Windows: CState + 0xB0 CPdxArray<int32>）
        cores = None
        if sys.platform == "win32":
            try:
                cd = p.u64(s + 0xB0)
                cn = p.i32(s + 0xBC)
                if cd and 0 < cn <= 128:
                    import combat as _cb_sd
                    id2tag = _cb_sd._country_id_map(self)
                    vals = struct.unpack("<%di" % cn, p.read(cd, 4 * cn))
                    cores = [id2tag.get(v, str(v)) for v in vals]
                elif cn == 0:
                    cores = []
            except Exception:
                cores = None
        res = {
            "ok": True,
            "id": state_id,
            "owner": _owner_tag,
            "ptr": hex(s),
            "buildings": blds,
            "provinces": _count(p, s, ST_PROVINCES),
            "cores": cores,
            "is_core_of_owner": (bool(_owner_tag in cores) if (cores is not None and _owner_tag) else None),
            "resistance": (round(p.i64(s + ST_RESISTANCE) / O.FIXED, 3)
                           if ST_RESISTANCE else None),
            "compliance": (round(p.i64(s + ST_COMPLIANCE) / O.FIXED, 3)
                           if ST_COMPLIANCE else None),
            "building_status_ptr": hex(s + 0x120),
        }
        if req_tag and _owner_tag and req_tag != str(_owner_tag).upper():
            res["requested_tag"] = req_tag
            res["owner_note"] = (
                "state %s is owned/controlled by %s, not %s (resolved via global fallback)"
                % (state_id, _owner_tag, req_tag)
            )
        # 2026-11-21 (P3-6): null is not "the reader failed" -- a CORE state has
        # no resistance/compliance at all.  Say so instead of leaving two nulls.
        if res.get("resistance") is None and res.get("compliance") is None:
            res["occupation_note"] = (
                "null = no occupation data: this is a CORE state of the country "
                "that owns it. resistance/compliance only exist for non-core "
                "occupied territory.")
        # 2026-11-21 (P1-6): id + building map alone cannot answer "which
        # state is 42" or "can I still build here".  Both come from the
        # game's own data files (history/states, common/state_category,
        # common/buildings) -- see calling/statemeta.py.
        try:
            import statemeta
            _bonus = None
            try:
                _bonus = (self._c(_owner_tag or tag).modifiers() or {}).get(
                    "global_building_slots_factor")
            except Exception:
                _bonus = None
            res.update(statemeta.state_meta(state_id, blds, _bonus))
        except Exception as _e:
            res["state_meta_error"] = repr(_e)
        return res

    def states_detail(self, tag=None, limit=40):
        out = []
        for s in self.states(tag)[:limit]:
            try:
                out.append(self.state_detail(s["id"], tag))
            except Exception:
                pass
        return out

    # ======================================================== IDEAS AND DECISIONS
    def idea_categories(self, tag=None):
        """Idea, law and advisor categories: law slots and advisor roles."""
        p = self.p
        db = self.oi.singleton("CIdeaDatabase")
        out = []
        for c in _arr(p, db, 0x98):
            nm = None
            for o in range(0, 0x200, 8):
                v = item_name(p, c + o)
                if v and v.isprintable() and 2 < len(v) < 48:
                    nm = v
                    break
            out.append(nm)
        return [x for x in out if x]

    def decision_detail(self, key, tag=None):
        p = self.p
        for which in ("visible", "active", "all"):
            for d in self.decisions(tag, which, rich=False):
                if d["key"] == key:
                    out = {"key": key, "list": which, "ptr": hex(d["ptr"])}
                    out.update(self.decision_meta(key))
                    out["fields"] = self.browse_object(d["ptr"], 0x80)["fields"][:16]
                    return out
        raise KeyError("no such decision: %s" % key)

    # ======================================================= COMMAND SHORTCUTS
    #  All of these go through the player command queue (not cheats).
    def _sc(self, name, tag=None, **kw):
        return self.send_command(name, kw, tag)

    # --- armies and divisions ---
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

    # --- battle plans (macro orders) ---
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

    # --- air ---
    def deploy_air_wing(self, tag=None, **kw):
        return self._sc("CDeployAirWingCommand", tag, **kw)

    def air_set_mission(self, tag=None, **kw):
        return self._sc("CStratAirSetMissionCommand", tag, **kw)

    def air_enable_mission(self, tag=None, **kw):
        return self._sc("CStratAirEnableMissionCommand", tag, **kw)

    def air_transfer(self, tag=None, **kw):
        return self._sc("CStratAirTransferCommand", tag, **kw)

    # --- navy ---
    def create_fleet(self, tag=None, **kw):
        return self._sc("CCreateFleetCommand", tag, **kw)

    def naval_mission_set_type(self, tag=None, **kw):
        return self._sc("CNavalMissionSetTypeCommand", tag, **kw)

    def naval_mission_set_regions(self, tag=None, **kw):
        return self._sc("CNavalMissionSetRegionsCommand", tag, **kw)

    def set_fleet_home_base(self, tag=None, **kw):
        return self._sc("CSetFleetHomeBaseCommand", tag, **kw)

    # --- diplomacy and trade ---
    def diplomatic_action(self, tag=None, **kw):
        return self._sc("CDiplomaticActionCommand", tag, **kw)

    def incoming_diplomatic_action(self, tag=None, **kw):
        return self._sc("CIncomingDiplomaticActionActingCommand", tag, **kw)

    def create_faction(self, tag=None, **kw):
        return self._sc("CCreateFactionCommand", tag, **kw)

    def create_trade(self, tag=None, **kw):
        return self._sc("CCreateTradeCommand", tag, **kw)

    def release_country(self, tag=None, **kw):
        return self._sc("CReleaseCountryCommand", tag, **kw)

    def set_occupation_policy(self, tag=None, **kw):
        return self._sc("CSetOccupationPolicyCommand", tag, **kw)

    # --- politics, advisors, companies ---
    def add_advisor(self, tag=None, **kw):
        return self._sc("CAddAdvisorCommand", tag, **kw)

    def hire_advisor(self, key, tag=None, slot_override=None):
        """Recruit a political advisor / chief of staff / theorist.

        1.19.4.1 CAddAdvisorCommand ctor 0x1CEB5C0, disassembled 2026-11-10:
            mov rax,[rsi+0x8] ; mov [rdi+0x24],rax  -> arg1 = CAdvisor* (handle at +0x08)
            mov r12,[rdx] ; mov rbx,[rdx+8] ...     -> arg2 = std::string* (slot name)
        Then CCommand::Run (CanExecute -> Post) through _run_cmd.
        """
        import advisors as _adv, api
        c = self._c(tag)
        rows = _adv.load_advisors(c.tag)
        k = (key or "").lower()
        row = None
        for r in rows:
            names = {r["character"].lower(), r["idea_token"].lower(),
                     r["character"].lower().replace(c.tag.lower() + "_", "")}
            if k in names:
                row = r
                break
        if row is None:
            return {"ok": False, "attempted": False,
                    "error": "no advisor named %r for %s" % (key, c.tag)}
        # 2026-11-12: refuse BEFORE injecting when the character's own script
        # trigger is provably unmet. legality._eval already implements the small
        # trigger subset used in common/characters; anything it cannot resolve
        # lands in `unknown` and we fall through to the engine as before.
        try:
            import legality as _leg
            _st = _leg._state(self, c.tag)
            for _bname in ("visible_block", "available_block"):
                _blk = row.get(_bname)
                if not _blk:
                    continue
                _unk = []
                if not _leg._eval(_blk, _st, _unk) and not _unk:
                    return {"ok": False, "attempted": False,
                            "advisor": row["character"], "slot": row.get("slot"),
                            "error": "%s condition unmet: {%s}"
                                     % (_bname, " ".join(_blk.split())[:160])}
        except Exception:
            pass
        ch = 0
        for r in (self.characters(c.tag) or {}).get("characters", []):
            ck = (r.get("key") or "").lower()
            if ck == row["character"].lower() or ck.endswith(
                    row["character"].lower().replace(c.tag.lower() + "_", "")):
                ch = int(r["ptr"], 16)
                break
        if not ch and sys.platform != "win32":
            # 2026-11-16: only the Linux path needs the CCharacter pointer here.
            # The PE path asks `character_handle()` for a packed handle, and
            # `self.characters()` is truncated (67 of ~200 entries: it stops at
            # "hermann_goring"), so every advisor after H was refused with
            # "CCharacter not found" while 2000 PP sat at the cap.
            return {"ok": False, "attempted": True,
                    "error": "CCharacter for %s not found" % row["character"]}
        # CanExecute does __dynamic_cast(resolved_handle, CReferenceObject,
        # CCharacter) — the argument is the PERSON, not the CAdvisor. Confirmed by
        # reading the typeinfo at 0x43493e8 ("10CCharacter") and 0x44d8fa0
        # ("16CReferenceObject") that 0x1CEBC00 passes to __dynamic_cast.
        adv = ch

        slot = slot_override if slot_override is not None else (row.get("slot") or "")

        if sys.platform == "win32":
            # The PE build has neither g.malloc nor the Linux ctor VA, but cmdwin
            # knows CAddAdvisorCommand's real ctor — (this, handleptr, std::string*)
            # at 0x11D2B40 — so the hire goes through the same command queue a
            # politics-tab click uses. Success is read back from the live idea list,
            # not from the post return value: the hire lands on a game tick.
            token = (row.get("idea_token") or "").lower()
            cost = int(row.get("cost") or 0)
            try:
                pp0 = int(self.player().summary().get("political_power") or 0)
            except Exception:
                pp0 = 0
            pp1 = pp0
            if cost and pp0 < cost:
                return {"ok": False, "attempted": False, "advisor": row["character"],
                        "slot": slot, "cost": cost, "political_power": pp0,
                        "error": "not enough political power: have %d, need %d"
                                 % (pp0, cost)}
            r = None
            import cmdwin as _cw
            try:
                _h = self.character_handle(row["character"], c.tag)
                _packed = (_h["id"] << 32) | (_h["type"] & 0xFFFFFFFF)
                # 2026-11-15: PE disassembly (cmdwin.make_add_advisor) proved
                #   vtable 0x2A11A88, size 0x50, packed handle at +0x28,
                #   slot std::string at +0x30. pecmd's "ctor" 0x11D2F00 is the
                #   deleting destructor (vtable slot 0) -- NEVER call it. The
                #   old +0x24 offset was Linux's; CanExecute (0x11ECC20) reads
                #   +0x28, which is why the command posted but never applied.
                #   The built object is byte-identical to the real ctor's output.
                can0 = _cw.can_add_advisor(self, _packed, slot)
                if can0 is False:
                    return {"ok": False, "attempted": True, "advisor": row["character"],
                            "idea_token": token, "slot": slot, "cost": cost,
                            "political_power": pp0, "can_execute": 0,
                            "error": ("the game's own CAddAdvisorCommand::CanExecute "
                                      "rejects this hire (slot %r): the character is "
                                      "already hired, that slot is full, its script "
                                      "trigger is unmet, or the cost cannot be paid. "
                                      "Pick another advisor." % slot)}
                _cmd = _cw.make_add_advisor(self, _packed, slot)
                # A paused game never drains the queue: post with a brief clock
                # run, then restore the pause state.
                r = _cw.post_drained(self, _cmd)
                r["handle"] = hex(_packed)
            except Exception as _e:
                return {"ok": False, "attempted": True, "advisor": row["character"],
                        "slot": slot, "error": "could not build CAddAdvisorCommand: %s" % _e}
            # Read-back: advisors do NOT appear in active_ideas (they live in a
            # separate character-slot list), so the reliable proof is the
            # CanExecute flip: 1 (hireable) -> 0 (already hired) after Execute.
            can1 = _cw.can_add_advisor(self, _packed, slot)
            try:
                pp1 = int(self.player().summary().get("political_power") or 0)
            except Exception:
                pp1 = pp0
            # 2026-11-19 (measured): `cost` is None for every advisor that has no
            # explicit price in the script, so `bool(cost and pp1 < pp0)` reported
            # pp_spent=False while the hire really charged 148 PP (154 -> 6).
            # Derive the charge from the PP ledger instead of from `cost`.
            _delta = int(pp1) - int(pp0)
            pp_spent_amount = -_delta if _delta < 0 else 0
            pp_spent = bool(pp_spent_amount > 0)
            # 2026-11-21 (P1-3): the 2026-11-20 playtest saw `cost: 100` but only
            # 93 PP gone and called the tool inconsistent.  `cost` is the scripted
            # price; `pp_spent_amount` is the OBSERVED balance drop, and the drain
            # runs the clock for a few game days, so the PP earned meanwhile
            # shrinks the drop.  Say that instead of leaving two numbers to argue.
            pp_delta_note = None
            if cost and pp_spent_amount and abs(int(cost) - int(pp_spent_amount)) >= 1:
                pp_delta_note = (
                    "cost is the scripted price (%d) while pp_spent_amount is the "
                    "observed balance drop (%d): the command drain runs the clock "
                    "for a few game days, so political power earned in the meantime "
                    "reduces the visible drop."
                    % (int(cost), int(pp_spent_amount)))
            applied = bool((can0 is True and can1 is False) or pp_spent)
            err = ""
            if not applied:
                err = ("CAddAdvisorCommand was queued (rv=%s) but the hire did not "
                       "land: CanExecute is still %s and PP went %d -> %d. The game "
                       "may have been held by a popup (drained=%s). Check the "
                       "available/visible trigger; a slot holds three advisors."
                       % (r.get("posted"), can1, pp0, pp1, r.get("drained")))
            return {"ok": applied, "attempted": True, "advisor": row["character"],
                    "idea_token": token, "slot": slot,
                    "cost": cost if cost else (pp_spent_amount or None),
                    "political_power_before": pp0, "political_power_after": pp1,
                    "can_execute_before": can0, "can_execute_after": can1,
                    "pp_spent": pp_spent, "pp_spent_amount": pp_spent_amount,
                    "pp_delta_note": pp_delta_note,
                    "drained": r.get("drained"),
                    "rv": r.get("posted"), "error": err}
        nb = slot.encode("utf-8")
        sp = self.malloc(0x20)
        if len(nb) <= 15:
            self.p.write(sp, struct.pack("<QQ", sp + 0x10, len(nb)) + nb.ljust(16, b"\0"))
        else:
            b = self.malloc(len(nb) + 1)
            self.p.write(b, nb + b"\0")
            self.p.write(sp, struct.pack("<QQQ", b, len(nb), len(nb) + 1))

        # 2026-11-11 (disassembly of 0x1CEB5C0): the ctor takes EXACTLY three
        # arguments (this, CCharacter*, std::string*). It reads [rsi+0x08] into
        # cmd+0x24 and deep-copies the string at rdx into cmd+0x30; rcx/r8 are
        # never touched, so the "missing regs" theory in the work order was
        # wrong. The real defects were (a) no read-back at all (ok came straight
        # from CanExecute while the effect lands on the NEXT game tick, so a
        # later observer saw PP move and blamed a failed hire for it) and
        # (b) no reason string when CanExecute refused.
        cost = int(row.get("cost") or 0)
        try:
            pp0 = int(self.player().summary().get("political_power") or 0)
        except Exception:
            pp0 = 0
        if cost and pp0 < cost:
            return {"ok": False, "attempted": False, "advisor": row["character"],
                    "slot": slot, "cost": cost, "political_power": pp0,
                    "error": "not enough political power: have %d, need %d"
                             % (pp0, cost)}

        api.allow_call(0x1CEB5C0)
        with self:
            cmd = self.cmd.new(0x50)
            self.p.write(cmd, b"\0" * 0x50)
            self.call(0x1CEB5C0, (cmd, adv, sp))
            rv = self._run_cmd(cmd)
            err = getattr(self, "_last_cmd_error", "")
        queued = bool(rv & 0xFF)

        # Read back: the hire is applied by the game loop, not by the call.
        token = (row.get("idea_token") or "").lower()
        pp1 = pp0
        applied = False
        for _ in range(16):
            self._settle(0.25)
            try:
                pp1 = int(self.player().summary().get("political_power") or 0)
            except Exception:
                pass
            if (cost and pp1 <= pp0 - cost) or (not cost and pp1 != pp0):
                applied = True
                break
            if not queued:
                break
        if not err and not queued:
            # 2026-11-11: PP was checked above, so a refusal here is never about money.
            # The old text sent two agents hunting for political power that they already had.
            err = ("engine refused CAddAdvisorCommand::CanExecute (slot %r). Political power "
                   "was sufficient (%d >= %d). A political_advisor slot holds THREE advisors, "
                   "so 'the slot is full' is not the explanation: this character's own "
                   "available/visible trigger is unmet (Todt/Speer need their focus, Schacht "
                   "loses his after some focuses). Pick another advisor; retrying this one "
                   "fails the same way." % (slot, pp0, cost))
        return {"ok": bool(queued and (applied or not cost)),
                "attempted": True, "queued": queued, "applied": applied,
                "advisor": row["character"], "idea_token": token, "slot": slot,
                "cost": cost, "political_power_before": pp0,
                "political_power_after": pp1, "advisor_ptr": hex(adv),
                "rv": rv & 0xFF, "error": err}

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

    # --- special projects and science ---
    def recruit_scientist(self, tag=None, **kw):
        return self._sc("CRecruitScientistCommand", tag, **kw)

    def attach_scientist(self, tag=None, **kw):
        return self._sc("CAttachScientistCommand", tag, **kw)

    def start_project(self, tag=None, **kw):
        return self._sc("CStartProjectCommand", tag, **kw)

    def stop_project(self, tag=None, **kw):
        return self._sc("CStopProjectCommand", tag, **kw)

    # --- intelligence and operations ---
    def create_intelligence_agency(self, tag=None, **kw):
        return self._sc("CIntelligenceAgencyCreationCommand", tag, **kw)

    def recruit_operative(self, tag=None, **kw):
        return self._sc("CRecruitOperativeCommand", tag, **kw)

    def launch_operation(self, tag=None, **kw):
        return self._sc("CLaunchOperationCommand", tag, **kw)

    # --- more production ---
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


for _n in dir(MacroMixin2):
    if not _n.startswith("__"):
        setattr(Game, _n, getattr(MacroMixin2, _n))


# ============================================================================
#  RESOLVING VALUES BY NAME: so callers never have to handle raw pointers
#  "focus:GER_rhineland", "tech:interwar_artillery", "country:FRA", "state:51" ...
# ============================================================================
class ResolverMixin:
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
    }

    def resolve(self, spec, tag=None):
        """Turn a 'kind:value' reference into a pointer or id.
        A number, or 0x..., is returned unchanged."""
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
            which = "war_goals" if kind == "wargoal" else "occupation_laws"
            cls, off, _ = self.DATABASE_ARRAYS[which]
            db = self.oi.singleton(cls)
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

    # ------------------------------------------------------- command help
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


for _n in dir(ResolverMixin):
    if not _n.startswith("__"):
        setattr(Game, _n, getattr(ResolverMixin, _n))


# ============================================================================
#  HANDLES, DIPLOMATIC ACTION CLASSES, AND LATER ADDITIONS
#  The game refers to units and characters by a {int32 type, int32 id} "handle"
#  rather than a pointer. In commands these are written as two adjacent int32s.
# ============================================================================
HANDLE_OFFSETS = {          # class -> offset of the handle inside the object
    "CCharacter": 0x08,
    "CArmy": 0x18,          # CArmy is a division
}


class HandleMixin:
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

    # ------------------------------------------------------------ diplomacy
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
        import os, pickle, re
        here = os.path.dirname(os.path.abspath(__file__))
        pkl = os.path.join(here, "..", "research", "windows", "rtti_win.pkl")
        if not os.path.exists(pkl):
            return {"ok": False, "source": "pe-rtti",
                    "error": "rtti_win.pkl is missing; run "
                             "research/windows/rtti_win.py once to regenerate it"}
        with open(pkl, "rb") as fh:
            d = pickle.load(fh)
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
        if sys.platform == "win32":
            return self._win_action_classes()
        import rtti as _R
        from elf import ELF
        e = ELF(__import__("config").binary_path())
        m = _R.build()
        rel, _s = _R.all_relocs(e)
        ti = m["typeinfo"]
        out = []
        for slot, name in ti.items():
            b = rel.get(slot + 16)
            if b in ti and ti[b] == "17CDiplomaticAction":
                nm = re.sub(r"^\d+", "", name)
                vp = None
                for k, vs in m["vtables"].items():
                    if k == name:
                        vp = vs[0]
                out.append({"class": nm, "vtable": hex(vp) if vp else None})
        return {"count": len(out), "actions": sorted(out, key=lambda x: x["class"])}


import re  # noqa: E402
for _n in dir(HandleMixin):
    if not _n.startswith("__"):
        setattr(Game, _n, getattr(HandleMixin, _n))


# handle: register the reference kind with the resolver
_orig_resolve = Game.resolve


def _resolve2(self, spec, tag=None):
    if isinstance(spec, str) and spec.startswith(("charhandle:", "divhandle:")):
        kind, _, val = spec.partition(":")
        h = (self.character_handle(val, tag) if kind == "charhandle"
             else self.division_handle(val, tag))
        return (h["id"] << 32) | (h["type"] & 0xFFFFFFFF)   # i64: [tip|id]
    return _orig_resolve(self, spec, tag)


Game.resolve = _resolve2
Game.REF_TYPES = dict(Game.REF_TYPES,
                      charhandle="Character handle (write as i64: i64@<offset>)",
                      divhandle="Division handle (write as i64)")


# ============================================================================
#  LATER ADDITIONS: faction members, trade, intelligence, occupation, subsystem browser
# ============================================================================
CO_TRADE_ROUTES     = 0x112B08   # CPdxArray<CResourceDeliveryRoute*>
CO_RESOURCE_ORIGINS = 0x112A90
FACTION_MEMBERS     = 0x58       # CPdxArray<CCountry*>


class ExtraMixin:
    def faction_members(self, tag=None):
        info = self.faction(tag)
        if not info:
            return {"faction": None, "members": []}
        return {"faction": info.get("name"), "leader": info.get("leader"),
                "members": info.get("members") or []}

    def create_trade(self, partner, resource, amount=None, factories=None,
                     tag=None, dry_run=False):
        """建立、调整或取消从 partner 进口 resource 的贸易协定（PE 1.19.5.0 原生 CCreateTradeCommand）。

        2026-11-23 (C-5):
          * `partner`: 出口国 TAG（如 'ROM' / 'SOV' / 'VEN' / 'SWE'）
          * `resource`: 资源名（'oil' / 'rubber' / 'tungsten' / 'steel' / 'aluminium' / 'chromium' / 'coal'）
          * `amount`: 目标进口总单位数（如 8 = 1 民工，16 = 2 民工，0 = 取消贸易）
          * `factories`: 民工快捷参数（与 amount 二选一，自动换算为 factories * 8）
        """
        if sys.platform != "win32":
            return {"ok": False, "error": "create_trade is currently implemented on Windows PE 1.19.5.0"}
        if amount is None:
            if factories is not None:
                amount = int(factories) * 8
            else:
                amount = 8
        import trade_win as TW
        return TW.create_trade(
            self,
            exporter_tag=partner,
            resource=resource,
            amount=int(amount),
            importer_tag=tag,
            dry_run=bool(dry_run),
        )

    def trade_routes(self, tag=None, limit=40, world=False):
        """Resource trade routes (exports / imports) for one country.

        2026-11-16: the PE path used to read CCountry+0x112B08, a Linux
        CCurrentGameState offset that lands ~1.1 MB past the end of the object
        and produced `count: 32758` bogus rows, so the tool was disabled with
        "no verified container".

        2026-11-19: the container was found, just not where CResourceDeliveryRoute
        lives.  The real book is CCountry+0x11F8 (CCountryResources) +0x728: one
        CPdxArray<CResourceExchange*> per strategic resource, holding the
        country's EXPORT deals.  See calling/trade_win.py for the full offset
        table and the 131-objects/131-entries proof that a deal is stored only
        on the supplier's side (imports are derived by scanning the world).
        """
        if sys.platform == "win32":
            import trade_win as TW
            try:
                if world:
                    rows = TW.world_trades(self)
                    return {"ok": True, "available": True, "world": True,
                            "count": len(rows),
                            "returned": min(len(rows), int(limit or 40)),
                            "routes": rows[:int(limit or 40)],
                            "resource_index_order": TW.RESOURCE_NAMES}
                c = self._c(tag)
                info = TW.country_trades(self, tag)
                exp, imp = info["exports"], info["imports"]
                net = {}
                for r in exp:
                    net[r["resource"]] = net.get(r["resource"], 0) - r["amount"]
                for r in imp:
                    net[r["resource"]] = net.get(r["resource"], 0) + r["amount"]
                lim = int(limit or 40)
                return {"ok": True, "available": True, "country": c.tag,
                        "export_count": len(exp), "import_count": len(imp),
                        "export_units": round(sum(r["amount"] for r in exp), 2),
                        "import_units": round(sum(r["amount"] for r in imp), 2),
                        "net": {k: round(v, 2) for k, v in sorted(net.items())},
                        "exports": exp[:lim], "imports": imp[:lim],
                        "truncated": len(exp) > lim or len(imp) > lim,
                        "scanned_countries": info["scanned_countries"],
                        "direction_note": "a deal is stored on the SUPPLIER's "
                                          "side only; `exports` is read, "
                                          "`imports` is derived by scanning "
                                          "every other country's export book",
                        "resource_index_order": TW.RESOURCE_NAMES}
            except Exception as e:
                return {"ok": False, "available": False, "routes": [],
                        "error": "%s: %s" % (type(e).__name__, e)}
        p = self.p
        out = []
        for r in _arr(p, self._c(tag).ptr, CO_TRADE_ROUTES)[:limit]:
            out.append({"ptr": hex(r), "class": self.oi.class_of(r)})
        return {"count": _count(p, self._c(tag).ptr, CO_TRADE_ROUTES), "routes": out}

    def wars(self, tag=None):
        """Wars we are in. PURE READS ONLY.

        This used to call `country_stats()`, which ptrace-calls ~45 trigger
        functions recovered from a 1.19.2 scan; the game died within 10 s every
        time `hoi4_wars` was invoked. `combat.enemy_tags` reads the CWarRelation
        objects straight out of the country's diplomacy status instead.
        """
        import combat
        c = self._c(tag)
        enemies = combat.enemy_tags(self, c.tag)
        try:
            recs = combat.combats(self, only_mine=True, me=c.tag)
        except Exception:
            recs = []
        mine = 0
        for r in recs:
            mine += (r.get("us") or {}).get("front") or 0
        # 2026-11-20 (P0 #3): `CCountry+0x12F0` reads 0 for EVERY country on the
        # PE build, including Italy with six divisions in combat and Ethiopia
        # with one -- verified live against the real battle list.  Reporting a
        # field that is dead everywhere is worse than not reporting it, so the
        # count is derived from the battle list and the dead field is dropped.
        return {"at_war": bool(enemies), "enemies": enemies,
                "active_combats": len(recs),
                "divisions_in_combat": mine,
                # 2026-11-21 (P1-8): hoi4_combat_report and this tool now count the
                # SAME list, so a difference can only be scope (all=true) or timing.
                # Name the source so a future mismatch is traceable instead of
                # looking like two tools disagreeing about reality.
                "active_combats_source": "combat.combats(only_mine=True, me=%s)" % c.tag,
                "armies_in_combat": None,
                "armies_in_combat_note": "CCountry+0x12F0 is dead on the PE build "
                                         "(0 for ITA/ETH while they had 6/1 divisions "
                                         "fighting); use divisions_in_combat"}

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

    def promote_general(self, character_key, tag=None):
        """Promote a corps commander to field marshal. Costs command power.

        NOTE: this used to silently do nothing. The command was given a CCharacter
        handle (type 73). The CanExecute of CPromoteUnitLeaderCommand
        (img+0x23F51A0) resolves the handle and does `cmpl $0x1, 0xD7C(obj)`,
        does. +0xD7C is CArmyLeader's RANK field (0=field marshal, 1=corps
        commander, 2=admiral). So the command wants a CArmyLeader (type 4713) and
        with the wrong type the compare never matched. The same mistake crashed the
        crashed the game once before (see FINDINGS).
        """
        p = self.p
        ch = None
        for c in self.all_characters(tag):
            if item_name(p, c + CH_KEY) == character_key:
                ch = c
                break
        if ch is None:
            raise KeyError("no such character: %s" % character_key)
        lp = p.u64(ch + 0xC0)
        if not lp:
            return {"ok": False, "error": "%s is not an army leader (no CArmyLeader)"
                                          % character_key}
        h = self.handle_of(lp)
        if h["type"] != 4713:
            return {"ok": False, "error": "beklenen CArmyLeader handle'i (4713), gelen %d"
                                          % h["type"]}
        rank = p.i32(lp + 0xD7C)
        if rank != 1:
            names = {0: "already a field marshal", 1: "a corps commander", 2: "an admiral"}
            return {"ok": False, "rank": rank,
                    "error": "only a CORPS COMMANDER can be promoted to field marshal; %s %s"
                             % (character_key, names.get(rank, "rank %d" % rank))}
        res = self.send_command("CPromoteUnitLeaderCommand",
                                {"arg0": (h["id"] << 32) | (h["type"] & 0xFFFFFFFF)}, tag)
        try:
            new_rank = p.i32(lp + 0xD7C)
        except Exception:
            new_rank = None
        if isinstance(res, dict):
            res["rank_before"] = rank
            res["rank_after"] = new_rank
            res["promoted"] = (new_rank == 0)
            if new_rank == rank:
                res["note"] = ("Rank did not change, the game refused. The most likely reason "
                               "is insufficient command power, which it rejects silently.")
        return res

    def recruit_unit_leader(self, leader_type=0, tag=None, dry_run=False):
        """Recruit a military leader (costs COMMAND POWER, not political power).

        公开参数约定（保持向后兼容）：
          - 0 / 'general' / 'corps_commander' -> 陆军军长（引擎 CCreateUnitLeaderCommand +0x2C = 1）
          - 1 / 'field_marshal'               -> 陆军元帅（引擎 CCreateUnitLeaderCommand +0x2C = 0）
          - 2 / 'admiral' / 'navy_leader'     -> 海军上将（引擎 CCreateUnitLeaderCommand +0x2C = 2）

        2026-11-23 (复测 6 P1-2): 经 PE 反汇编确认三大根因并根治：
          1. CCreateUnitLeaderCommand::ctor (0x11CDF70) 需两个参数：(CCountryTag* tag, int engine_type)，
             以前缺失 ARGSPEC 且传 {"arg0": 0} 导致 +0x28 tag_id==0 被 CanExecute (0x11EE9D0) 秒拒；
          2. 引擎实际枚举为 0=field_marshal / 1=corps_commander / 2=admiral，此处显式映射，
             确保默认 type=0 真正招出可分配给集团军的军长（corps_commander）；
          3. 消耗资源是 Command Power（CCountry+0x1F0）而非 PP；发令前实调 CanExecute，
             发令后仅在角色列表或 CP 真实变动时才返回 ok=True，杜绝假成功。
        """
        # 解析请求类型到 (role, engine_type_code)
        _str_map = {
            "general": ("corps_commander", 1, 0),
            "corps_commander": ("corps_commander", 1, 0),
            "field_marshal": ("field_marshal", 0, 1),
            "marshal": ("field_marshal", 0, 1),
            "admiral": ("admiral", 2, 2),
            "navy_leader": ("admiral", 2, 2),
        }
        _int_map = {
            0: ("corps_commander", 1),
            1: ("field_marshal", 0),
            2: ("admiral", 2),
        }
        if isinstance(leader_type, str):
            key_s = leader_type.strip().lower()
            if key_s in _str_map:
                role, engine_type, pub_type = _str_map[key_s]
            elif key_s.isdigit() and int(key_s) in _int_map:
                pub_type = int(key_s)
                role, engine_type = _int_map[pub_type]
            else:
                return {"ok": False, "blocked_by": "invalid_leader_type",
                        "error": "unknown leader_type %r (expected 0/general, 1/field_marshal, 2/admiral)" % (leader_type,)}
        else:
            pub_type = int(leader_type)
            if pub_type not in _int_map:
                return {"ok": False, "blocked_by": "invalid_leader_type",
                        "error": "leader_type must be 0 (general), 1 (field_marshal), or 2 (admiral), got %r" % (leader_type,)}
            role, engine_type = _int_map[pub_type]

        c = self._c(tag)
        try:
            cp_before = c.command_power
        except Exception:
            cp_before = None
        try:
            pp_before = round(float(c.political_power), 2)
        except Exception:
            pp_before = None
        try:
            _lp0 = self._country_leader_ptrs(c.tag) if sys.platform == "win32" else {}
            leaders_before = set(_lp0.get("army") or []) | set(_lp0.get("navy") or [])
        except Exception:
            leaders_before = set()
        try:
            chars_before = set(self.all_characters(c.tag))
        except Exception:
            chars_before = set()

        can_exec = None
        if sys.platform == "win32":
            import cmdwin
            spec = cmdwin.spec_of("CCreateUnitLeaderCommand")
            cmd = cmdwin.build_command(
                self, "CCreateUnitLeaderCommand",
                {"tag": int(c.tag_id), "leader_type": int(engine_type)},
            )
            # 实调引擎原生 CanExecute (0x11EE9D0) 进行只读预检
            try:
                can_exec_rva = spec.get("can_execute")
                if can_exec_rva:
                    can_exec = bool(self.call(self.p.base + int(can_exec_rva),
                                              (cmd, 0), absolute=True, force=True) & 0xFF)
            except Exception:
                can_exec = None
            if dry_run:
                return {
                    "ok": bool(can_exec is not False),
                    "dry_run": True,
                    "can_execute": can_exec,
                    "leader_type": pub_type,
                    "role": role,
                    "engine_type_code": engine_type,
                    "command_power_before": cp_before,
                    "pp_before": pp_before,
                    "cmd": hex(cmd),
                }
            if can_exec is False:
                return {
                    "ok": False,
                    "verified": False,
                    "blocked_by": "can_execute_refused",
                    "can_execute": False,
                    "leader_type": pub_type,
                    "role": role,
                    "engine_type_code": engine_type,
                    "command_power_before": cp_before,
                    "pp_before": pp_before,
                    "cmd": hex(cmd),
                    "error": ("engine CanExecute refused CCreateUnitLeaderCommand "
                              "(command_power=%s; recruiting a leader requires sufficient command power)"
                              % cp_before),
                }
            res = cmdwin.post_drained(self, cmd, seconds=1.5)
            res = dict(res) if isinstance(res, dict) else {"raw": res}
            res["cmd"] = hex(cmd)
            self._settle(0.2)
        else:
            if dry_run:
                return {"ok": True, "dry_run": True, "leader_type": pub_type,
                        "role": role, "engine_type_code": engine_type}
            res = self.send_command("CCreateUnitLeaderCommand",
                                    {"tag": int(c.tag_id), "leader_type": int(engine_type)}, tag)
            self._settle(0.35)

        try:
            cp_after = c.command_power
        except Exception:
            cp_after = None
        try:
            pp_after = round(float(c.political_power), 2)
        except Exception:
            pp_after = None
        cp_spent = (
            round(max(0.0, float(cp_before) - float(cp_after)), 2)
            if (cp_before is not None and cp_after is not None)
            else None
        )
        pp_spent = (
            round(max(0.0, float(pp_before) - float(pp_after)), 2)
            if (pp_before is not None and pp_after is not None)
            else None
        )

        new_ptr = None
        new_char_ptr = None
        leader_key = None
        leader_name = None
        new_rank = None
        new_stats = None
        # 2026-11-23 (C-8 根因修复): 优先直接对比 CCountryCharacters+0x70/+0x88 中的真实
        # CArmyLeader*/CNavyLeader* 差集，100% 捕获动态招募将领并返回可直接用于
        # hoi4_assign_commander 的 leader_ptr、key 与本地化名！
        try:
            if sys.platform == "win32":
                _lp1 = self._country_leader_ptrs(c.tag)
                army_after = list(_lp1.get("army") or [])
                navy_after = list(_lp1.get("navy") or [])
                new_army = [x for x in army_after if x not in leaders_before]
                new_navy = [x for x in navy_after if x not in leaders_before]
                if new_army:
                    new_ptr = new_army[-1]
                    leader_key = item_name(self.p, new_ptr + 0x20) or None
                    leader_name = item_name(self.p, new_ptr + 0x40) or leader_key
                    try:
                        new_rank = legality.RANKS.get(self.p.i32(new_ptr + legality.AL_RANK))
                        new_stats = {
                            "attack": self.p.i32(new_ptr + self.AL_ATTACK),
                            "defense": self.p.i32(new_ptr + self.AL_DEFENSE),
                            "planning": self.p.i32(new_ptr + self.AL_PLANNING),
                            "logistics": self.p.i32(new_ptr + self.AL_LOGISTICS),
                        }
                    except Exception:
                        pass
                elif new_navy:
                    new_ptr = new_navy[-1]
                    leader_key = item_name(self.p, new_ptr + 0x20) or None
                    leader_name = item_name(self.p, new_ptr + 0x40) or leader_key
                    new_rank = "admiral"
        except Exception:
            pass
        if new_ptr is None:
            try:
                chars_after = list(self.all_characters(c.tag))
                new_list = [ch for ch in chars_after if ch not in chars_before]
                if new_list:
                    new_char_ptr = new_list[-1]
                    try:
                        leader_key = item_name(self.p, new_char_ptr + CH_KEY) or None
                    except Exception:
                        leader_key = None
                    try:
                        k2, nm2 = self._extract_char_key_and_name(new_char_ptr)
                        leader_key = leader_key or k2
                        leader_name = nm2 or leader_key
                    except Exception:
                        leader_name = leader_key
                    try:
                        lp = self.p.u64(new_char_ptr + self.CH_ARMY_LEADER)
                        if lp:
                            new_ptr = lp
                            new_rank = legality.RANKS.get(self.p.i32(lp + legality.AL_RANK))
                    except Exception:
                        pass
            except Exception:
                pass

        out = dict(res) if isinstance(res, dict) else {"raw": res}
        verified = bool(
            new_ptr is not None
            or new_char_ptr is not None
            or (cp_spent is not None and cp_spent > 0.0)
            or (pp_spent is not None and pp_spent > 0.0)
        )
        out.update({
            "ok": verified,
            "verified": verified,
            "can_execute": can_exec,
            "leader_type": pub_type,
            "role": role,
            "engine_type_code": engine_type,
            "command_power_before": cp_before,
            "command_power_after": cp_after,
            "command_power_spent": cp_spent,
            "pp_before": pp_before,
            "pp_after": pp_after,
            "pp_spent": pp_spent,
            "leader_ptr": hex(new_ptr) if new_ptr else None,
            "character_ptr": hex(new_char_ptr) if new_char_ptr else None,
            "leader_key": leader_key,
            "leader_name": leader_name,
            "leader_rank": new_rank,
            "stats": new_stats,
        })
        if not verified:
            out["blocked_by"] = out.get("blocked_by") or "no_effect_observed"
            out["error"] = (
                "command posted but neither character count nor command_power changed "
                "(command_power=%s)" % cp_after
            )
        return out

    def diplomatic_action_info(self):
        """A guide to the diplomatic actions."""
        # 2026-11-22 (P2-2): Windows _win_action_classes() 返回 `classes`，Linux 返回 `actions`。
        cls_info = self.diplomatic_action_classes() or {}
        actions = cls_info.get("actions")
        if actions is None:
            actions = cls_info.get("classes") or []
        try:
            incoming = self.command_help("CIncomingDiplomaticActionActingCommand")
        except Exception as e:
            incoming = {"error": repr(e)}
        out = {
            "ok": bool(cls_info.get("ok", True)),
            "count": len(actions),
            "actions": actions,
            "how": "CDiplomaticActionCommand carries a CDiplomaticAction at +0x28. "
                   "These are polymorphic structures of 0x90+ bytes containing embedded sub-objects; "
                   "generic diplomatic actions are not wrapped on Windows (use hoi4_justify_wargoal / "
                   "hoi4_declare_war). To answer incoming diplomatic offers, "
                   "CIncomingDiplomaticActionActingCommand can be used.",
            "incoming_command": incoming,
        }
        if cls_info.get("source"):
            out["source"] = cls_info["source"]
        if cls_info.get("note"):
            out["note"] = cls_info["note"]
        return out


for _n in dir(ExtraMixin):
    if not _n.startswith("__"):
        setattr(Game, _n, getattr(ExtraMixin, _n))


class MoveMixin:
    """Route A: per-division province orders (the player's right click)."""

    def move_divisions(self, divisions=None, province=None, orders_group=None, tag=None,
                       mode="move", wait=12.0, limit=60, force=False,
                       spread=False, max_per_province=None):
        import movemicro
        return movemicro.move_divisions(self, divisions, province, orders_group, tag,
                                        mode=mode, wait=wait, limit=limit, force=force,
                                        spread=spread, max_per_province=max_per_province)

    def deploy_to_border(self, target_tag, orders_group=None, include_unassigned=True, tag=None,
                         protect_borders=None, min_other_garrison=0):
        import movemicro
        return movemicro.deploy_to_border(
            self, target_tag, orders_group, include_unassigned, tag,
            protect_borders=protect_borders, min_other_garrison=min_other_garrison,
        )

    def attack_country(self, target_tag, mode="balanced", max_attacks=2, tag=None):
        import movemicro
        return movemicro.attack_country(self, target_tag, mode, max_attacks, tag)

    def plug_border_gaps(self, target_tag, min_garrison=1, max_gaps=None, dry_run=False):
        import movemicro
        return movemicro.plug_border_gaps(self, target_tag, min_garrison,
                                         max_gaps, dry_run)

    def wartime_status(self):
        import movemicro
        return movemicro.wartime_status(self)

    def wartime_step(self, target_tag=None, max_attacks=2, dry_run=False):
        import movemicro
        return movemicro.wartime_step(self, target_tag, max_attacks, dry_run)

    # --- fifth stage: campaign directive + CA-level tactical tick ---
    def set_front_directive(self, enemy_tag, stance="balanced_attack",
                            priority_provinces=None, priority_states=None,
                            min_garrison=1, deploy=None, tag=None, battleplan=True,
                            armies=None):
        # 2026-11-17: the wrapper forgot `battleplan`, but mcp/server.py passes it
        # unconditionally, so every hoi4_set_front_directive call died with
        # "MoveMixin.set_front_directive() got an unexpected keyword argument
        # 'battleplan'" -- i.e. the whole land-war entry point was dead on this
        # fork while movemicro itself supported the parameter all along.
        import movemicro
        return movemicro.set_front_directive(self, enemy_tag, stance,
                                             priority_provinces, priority_states,
                                             min_garrison, deploy, tag, battleplan,
                                             armies=armies)

    def front_directives(self):
        import movemicro
        return movemicro.front_directives(self)

    def clear_front_directive(self, enemy_tag=None):
        import movemicro
        return movemicro.clear_front_directive(enemy_tag)

    def tactical_front_tick(self, enemy_tag=None, force=False, dry_run=False):
        import movemicro
        return movemicro.tactical_front_tick(self, enemy_tag, force, dry_run)


# Copy the raw descriptors, not the bound attributes: getattr() unwraps
# staticmethod/classmethod/property, which silently turned HousekeepMixin's
# static `_prod_group(equipment)` into a bound method and made every
# housekeep production pass die with "takes 1 positional argument but 2".
for _n in dir(MoveMixin):
    if not _n.startswith("__"):
        setattr(Game, _n, MoveMixin.__dict__.get(_n, getattr(MoveMixin, _n)))


from housekeep import HousekeepMixin

for _n in dir(HousekeepMixin):
    if not _n.startswith("__"):
        setattr(Game, _n, HousekeepMixin.__dict__.get(_n, getattr(HousekeepMixin, _n)))