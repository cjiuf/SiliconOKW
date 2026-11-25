"""calling/game.py 与各领域 Mixin 共享的偏移别名、底层读取工具及全局缓存容器。"""
from __future__ import annotations

import os
import struct
import sys

from explore import cstring
import offsets as O
import session_state as _SS

# calling/ 目录与仓库根目录，保证拆入 calling/domains/ 后路径解析与原 game.py 完全一致
CALLING_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO_ROOT = os.path.dirname(CALLING_DIR)
HERE = CALLING_DIR


# ------------------------------------------------------------------ offsets
# CCountry subsystems
# Every one of these is a pointer to a status sub-object, and the Windows build
# moved them (0xF38 -> 0xF60 etc), so prefer the verified Windows table.
CO_TECH_STATUS      = O.CO_TECHNOLOGY_STATUS     # CTechnologyStatus*
CO_PRODUCTION       = O.CO_PRODUCTION_STATUS     # CProductionStatus*
CO_ARMY_UPGRADES    = O.CO_ARMY_UPGRADES_STATUS
CO_DIPLOMACY        = O.CO_DIPLOMACY_STATUS      # CDiplomacyStatus*
CO_POLITICS         = O.CO_POLITICAL_STATUS      # CPoliticalStatus*
CO_DECISIONS        = O.CO_DECISION_STATUS       # CDecisionStatus*
CO_INTEL_AGENCY     = O.CO_INTEL
CO_COUNTRY_INTEL    = getattr(O, "CO_COUNTRY_INTEL", None)
CO_FOCUS_TREE       = O.CO_FOCUS_TREE            # CNationalFocusTree*
CO_CONT_FOCUS_PAL   = O.CO_CONT_FOCUS_PAL
CO_FOCUS_PROGRESS   = O.CO_FOCUS_PROGRESS        # CNationalFocusProgress*
CO_FACTION          = None
CO_STATES           = O.CO_STATES            # CPdxArray<CState*>, CONTROLLED states
CO_OWNED_STATES     = O.CO_OWNED_STATES      # CPdxArray<CState*>, OWNED states
CO_CAPITAL_STATE    = O.CO_CAPITAL_STATE     # int32 state id of the capital (None on PE)

# CNationalFocusTree
FT_FOCUSES          = O.FT_FOCUSES           # CPdxArray<CNationalFocus*>

# CNationalFocus
NF_ID               = O.NF_ID
NF_KEY              = O.NF_KEY               # CString
NF_LOCKEY           = O.NF_LOCKEY
NF_NAME             = O.NF_NAME              # CString (localised)
NF_ICON             = O.NF_ICON
NF_AVAIL_TRIGGER_N  = O.NF_AVAIL_TRIGGER_N
NF_PREREQ           = O.NF_PREREQ            # CPdxArray<CNationalFocusDependency*>
NF_MUTEX            = O.NF_MUTEX
NF_FILTERS          = O.NF_FILTERS

# CNationalFocusProgress
FP_COUNTRY          = O.FOCUS_FP_COUNTRY
FP_CURRENT          = O.FOCUS_FP_CURRENT     # CNationalFocus* (0 = no focus selected)
FP_COMPLETED        = O.FOCUS_FP_COMPLETED   # CPdxArray
FP_ROOT_FOCUSES     = O.FOCUS_FP_ROOT_FOCUSES
FP_DAILY            = O.FOCUS_FP_DAILY

# CTechnologyStatus
TS_RESEARCHED       = O.TS_RESEARCHED        # CPdxArray<CTechnology*>
TS_ALL_TECHS        = O.TS_ALL_TECHS         # CPdxArray<CTechnology*>
TS_SLOTS            = O.TS_SLOTS             # CPdxArray<CResearchSlot*>
TS_PRODUCTION_LINES = O.TS_PRODUCTION_LINES

# CResearchSlot  (0x48 bytes; ctor 0xF4F4F0, dtor frees 0x48)
# The three fields below are exactly the ones `CResearchSlot::Save` (0xF61BF0)
# writes, with the engine's own member tags: +0x20 -> 0x31DE, +0x28 -> 0x34BB,
# +0x38 -> 0x3684.  They do NOT hold the live progress of the current
# technology: +0x20 is the progress carried over when a slot switches tech and
# +0x28 is the required amount computed at that moment (both stay 0/constant
# while a tech runs).  The bar the UI draws comes from the CTechnology object
# (TE_PROGRESS) against the template cost.
RS_OWNER            = O.RS_OWNER
RS_CURRENT          = O.RS_CURRENT           # CTechnology* (0 = empty)
RS_PROGRESS         = O.RS_PROGRESS          # carried-over progress, 1e5 fixed
RS_REQUIRED         = O.RS_REQUIRED          # required for the carried-over progress, 1e5
RS_SPEED            = O.RS_SPEED             # research speed, 1e5 fixed (1.01 = +1%/day)
RS_INDEX            = O.RS_INDEX             # int32 slot index

# CTechnology (per-country state object)
TE_ID               = O.TE_ID
TE_TEMPLATE         = O.TE_TEMPLATE          # CTechnologyTemplate* (vtable 0x29B9F10)
TE_PROGRESS         = O.TE_PROGRESS          # int64 research days x1e5, speed-weighted
# CTechnologyTemplate (global definition)
TT_NAME             = O.TT_NAME              # CString / std::string
TT_ID               = O.TT_ID
TT_YEAR             = O.TT_YEAR              # int32 start year (advanced_ship_hull_light -> 1943)
TT_RESEARCH_COST    = O.TT_RESEARCH_COST     # int32, days x1000 (150000 = 150 days)

# CProductionStatus
PS_LINES            = O.PS_LINES             # CPdxArray<CMilitaryProductionLine*/CNavalProductionLine*>
PS_MIL_LINES        = O.PS_MIL_LINES         # CPdxArray<CMilitaryProductionLine*>
PS_CONSTRUCTION     = O.PS_CONSTRUCTION      # CPdxArray<CBuildingProductionLine*>, construction queue
PS_VARIANTS_ALL     = O.PS_VARIANTS_ALL
PS_VARIANTS_ACTIVE  = O.PS_VARIANTS_ACTIVE

# CMilitaryProductionLine / CNavalProductionLine
ML_OWNER            = O.ML_OWNER
ML_PRIORITY         = O.ML_PRIORITY          # int32 (1.19.5; +0x10 holds unrelated data on some lines)
ML_FACTORIES        = O.ML_FACTORIES         # int32, assigned factory count (1.19.5)
ML_UNIT_COST        = O.ML_UNIT_COST         # int64 x1e5, unit production cost (IC)
ML_OUTPUT           = O.ML_OUTPUT            # int64 x1e5, daily output
ML_EFF_COST         = O.ML_EFF_COST          # int64 x1e5, modified cost
ML_MAX_EFFICIENCY   = O.ML_MAX_EFFICIENCY    # int32, maximum efficiency (%)
ML_VARIANT          = O.ML_VARIANT           # CEquipmentVariant*
ML_TOTAL_PRODUCED   = O.ML_TOTAL_PRODUCED

# CBuildingProductionLine (construction)
BL_OWNER            = O.BL_OWNER
BL_PROGRESS         = O.BL_PROGRESS          # int64 accumulated IC (x1e5, PE 1.19.5.0)
BL_RATE             = O.BL_RATE              # int64 current output in IC per day (x1e5)
BL_COST             = O.BL_COST              # int64 total cost in IC-days (10800 for a civ factory)
BL_BUILDING         = O.BL_BUILDING          # CBuilding*

# CEquipmentVariant
EV_TYPE_ID          = O.EV_TYPE_ID
# CEquipmentType
ET_ID               = O.ET_ID                # entry of CEquipmentDatabase+0x68
ET_NAME             = O.ET_NAME              # CString (1.19.5)
# CBuildingTemplate
BT_ID               = O.BT_ID
BT_NAME             = O.BT_NAME              # display name (None if the build has none)
BT_KEY              = O.BT_KEY               # key ("industrial_complex")
# CBuilding (example)
BG_TYPE_ID          = O.BG_TYPE_ID
# CState
ST_ID               = O.ST_ID                # int32, state id
# CBuildingReference { vptr; int32 state; int32 building_type; int32 province; }
BUILDING_REF_VTABLE = O.CMD_VT_BUILDING_REF

# CPoliticalStatus
PL_PARTIES          = O.PL_PARTIES           # CPdxArray<CPoliticalParty*>
PL_ALL_IDEAS        = O.PL_ALL_IDEAS         # CPdxArray<CIdea*>  (every idea valid for the country)
PL_ACTIVE_IDEAS     = O.PL_ACTIVE_IDEAS      # CPdxArray<CIdea*>, active spirits, laws and advisors
PL_RULING           = O.PL_RULING
PL_PP               = O.PL_PP

# CDecisionStatus
DS_ALL              = O.DS_ALL               # CPdxArray<CDecision*>   every definition (GER 500)
DS_ACTIVE           = O.DS_ACTIVE            # CPdxArray<CDecision*>   takeable / candidate (GER 115)
DS_VISIBLE          = O.DS_VISIBLE           # CPdxArray<CDecision*>   the country's visible decisions (GER 13)
DS_TARGETED         = O.DS_TARGETED          # CPdxArray<CTargetedDecision*> (GER 31)
DS_RUNNING          = O.DS_RUNNING

# CDiplomacyStatus
DP_RELATIONS        = O.DP_RELATIONS         # CPdxArray<CRelationStatus*>  (one per country)

# item name offsets
IDEA_NAME           = O.IDEA_NAME
DECISION_NAME       = O.DECISION_NAME        # MSVC std::string key (PE 1.19.5.0)

# the game's own check functions
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
FRONT_SNAPSHOT_PATH = _SS.state_path(_SS.FRONT_SNAPSHOT_NAME)
OBJINDEX_CACHE_PATH = _SS.state_path(_SS.OBJINDEX_NAME)

_SS.register_cache("game._AIR_SNAP", lambda: _AIR_SNAP.update(pid=None, ts=0.0, rows=None))
_SS.register_cache("game._CONV_PROGRESS", container=_CONV_PROGRESS)


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



CO_THEATRES         = O.CO_THEATRES
CO_DIV_TEMPLATES    = O.CO_DIV_TEMPLATES
CO_FLEETS           = O.CO_FLEETS
CO_ARMIES           = O.CO_ARMIES
CO_COMMAND_POWER    = O.CO_COMMAND_POWER
CO_RESOURCES        = O.CO_RESOURCES
CO_FOCUS_COMPLETED  = O.CO_FOCUS_COMPLETED
CO_SUBUNITS         = O.CO_SUBUNITS
CO_RELATIONS_ARR    = O.CO_RELATIONS_ARR
CO_PROGRAM_STATUS   = O.CO_PROGRAM_STATUS
CO_OCCUPATION       = O.CO_OCCUPATION_STATUS
CO_OPERATIONS       = O.CO_OPERATIONS
CO_PROJECT_POOL     = O.CO_PROJECT_POOL
CO_EQUIPMENT_TYPES  = O.CO_EQUIPMENT_TYPES

# CCountryResources: seven 16-byte {int64 amount; int32 id; ...} records from +0x60
RES_ARRAY           = O.RES_ARRAY
RES_STRIDE          = O.RES_STRIDE
RESOURCE_NAMES = ["oil", "aluminium", "rubber", "tungsten", "steel", "chromium", "coal"]

# CReferencedDivisionTemplate
DT_NAME             = O.DT_NAME
# CSubUnitDefinition
SU_KEY              = O.SU_KEY
SU_CATEGORY         = O.SU_CATEGORY
# CFleet
FL_NAME             = O.FL_NAME
# CCharacter
CH_NAME             = O.LEADER_CH_NAME     # 1.19.5 (was 0x20; CCharacter grew when CScientist landed)
CH_KEY              = O.LEADER_CH_KEY      # 1.19.5 (was 0x40)
# CRelationStatus
REL_INDEX           = O.REL_INDEX

FN_DYNAMIC_VAR      = 0x2D8EE60   # void GetVariable(out, const char* NAME, scope, int)


_VERIFIED_NAMES = set()
try:
    from commands import COMMANDS as _CMDS
    _VERIFIED_NAMES = {"C" + k + "Command" for k in _CMDS}
except Exception:
    pass


ST_PROVINCES        = O.ST_PROVINCES           # CPdxArray<CProvince*>
ST_BUILDING_LEVELS  = O.ST_BUILDING_LEVELS     # 56-entry building level array
ST_BUILDING_MAX     = O.ST_BUILDING_MAX
ST_BUILDINGS        = O.ST_BUILDINGS           # CPdxArray<CBuilding*>
BG_LEVEL            = O.BG_LEVEL               # int16, building level
ST_RESISTANCE       = O.ST_RESISTANCE          # int64 x1e5, None = unknown
ST_COMPLIANCE       = O.ST_COMPLIANCE          # int64 x1e5, None = unknown


HANDLE_OFFSETS = {          # class -> offset of the handle inside the object
    "CCharacter": 0x08,
    "CArmy": 0x18,          # CArmy is a division
}


CO_TRADE_ROUTES     = O.CO_TRADE_ROUTES      # CPdxArray<CResourceDeliveryRoute*>
CO_RESOURCE_ORIGINS = O.CO_RESOURCE_ORIGINS
FACTION_MEMBERS     = O.FACTION_MEMBERS      # CPdxArray<CCountry*>
