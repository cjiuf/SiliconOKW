"""Offsets recovered by reverse engineering for HOI4 **1.19.5.0 on Windows**.

This is the MSVC/PE counterpart of `offsets.py` (which describes the Linux ELF
build 1.19.4.1.99a5).  The two builds are *not* the same binary and their
version numbers differ, so nothing here may be mixed with the Linux table.

All values are RVAs relative to the image base: at runtime `base + X`.
Classes are found through MSVC RTTI (`research/windows/rtti_win.py` builds the
full `. ?AV<Class>@@` -> vtable map for the installed exe).

Verified live against the game (see research/WINDOWS_1.19.5.md):

    g_gameState -> CCurrentGameState (2 vptrs), GS_COUNTRIES @0x310 gives 440
    CCountry objects, all with the CCountry vtable; CState owner/controller
    fields verified against GER/ENG/SOV state lists; console `pp` executed
    through thread hijack and really granted political power.
"""

VERSION = "1.19.5.0"
BUILD_ID = "Operation Postern v1.19.5.0.7c6c (c184)"
PE_IMAGE_BASE = 0x140000000
PE_SIZE_OF_IMAGE = 0x38A3000

# ---------------------------------------------------------------- globals
# .data starts at base+0x30FD000; that address is also the CPdxNewDeleteAllocator
# singleton used by every CPdxArray.
G_DEFAULT_ALLOC = 0x30FD000

G_GAMESTATE = 0x33DD6B0          # u64 -> CCurrentGameState*
# 2026-11-21 (P0-1b): the exact clock.  CCurrentGameState+0x468 is an int32 the
# tick loop bumps while the game runs and that stops dead when it is paused.
# 2026-11-22 (P2-1): Clausewitz CGameDate stores 1-based hours as (hour - 1):
#   Start-date 1936.1.1.12 in GS+0x4A0 is 60,759,371 (= (5000+1936)*8760 + 11),
#   and default 1.1.1.1 in GS+0x090 is 43,808,760 (= 5001*8760 + 0).
# Subtracting 43,800,000 left the decoded clock 1 hour behind both game.log
# (`[1936.01.01.12]`) and the HUD (`12:00` / `3日 00:00` when raw hour was 23).
# Subtracting 43,799,999 aligns `date_raw` exactly with HUD and `game.log`.
GS_CLOCK = 0x468
GS_CLOCK_OFF = 43799999
# 2026-11-22 (P1 速度修复): 经反汇编 CCurrentGameState::SetSpeed (0x203F10)、
# 主循环 Tick 间隔计算 (0x206755) 与 Topbar HUD 速度条绘制 (0x192FC41) 证实：
#   CCurrentGameState+0x4BC 为权威游戏速度 int32，引擎范围 0..4（0=1速最慢 .. 4=5速最快）；
#   全局 NGame::GAME_SPEED_SECONDS (0x33E6080) 为 CPdxArray<float>，实机值为
#   (2.0, 0.5, 0.2, 0.1, 0.0) 秒/小时。主循环每帧直读 [gs+0x4BC] 查表推进时钟。
GS_GAME_SPEED = 0x4BC
G_GAME_SPEED_SECONDS = 0x33E6080
# 2026-11-21 (P1-3): production-line vtables.  Classifying a line by its own
# vptr costs one 8-byte read; `oi.class_of()` needs the ObjectIndex, whose first
# access scans the whole writable heap (10-15 s) -- and hoi4_overview paid that
# on every cold process just to name 14 lines.  Measured on pid 32028:
# military 0x29F8520, naval 0x29F86A0 (RVAs).
VT_MIL_LINE = 0x29F8520
VT_NAV_LINE = 0x29F86A0
# CBuildingProductionLine (measured 2026-11-22 on the 6-line construction queue).
# `construction_queue()` classified every line through `oi.class_of()`, which
# builds the ObjectIndex (50 s) -- and wait_for/overview both call it on the first
# poll, so a cold `hoi4_wait_for` blew the 50 s budget.
VT_BUILDING_LINE = 0x29F8268
GS_COMBATS = 0x268               # CCurrentGameState + 0x268 = CPdxArray<CLandCombat*>
                                 #   header (data +0, cap +8, size +0xC).  Was a
                                 #   "Linux-derived guess" kept alive by combat.py's
                                 #   coherence check; verified live 2026-11-20/21
                                 #   (4 then 16 real battles with correct province,
                                 #   terrain and country tags).  Declared here so the
                                 #   reader can stop silently falling back.  See P4-3.
G_CONSOLE_MANAGER = 0x33DD968    # CPdxArray<CConsoleCmd> *inline* (not a pointer!)
G_COUNTRYTAG_DB = None           # no image global; the live DB is inline in CGameState, see GS_TAG_DB
G_MODIFIER_DEFS = 0x33DD1D0      # u64 -> SModifierDef[] (heap, 666 entries).
                                 #   Found by scanning writable memory for 24
                                 #   consecutive records whose int32 at +0x6C
                                 #   equals their index: exactly one candidate.
                                 #   +0x00 std::string "MODIFIER_<NAME>",
                                 #   +0x6C int32 id == index (0..665)
G_STABILITY_BASE = None          # UNVERIFIED

# ------------------------------------------------------------- CGameState
GS_COUNTRIES = 0x310             # CPdxArray<CCountry*>   cap/size 440
GS_TAG2INDEX = 0x340             # CPdxArray<int32>       cap 474 size 443 (identity)
GS_TAG_DB = 0x358                # CPdxArray<std::string> cap 474 size 443, INLINE
                                 #   (0x340 + 0x18: the tag index array is immediately
                                 #    followed by the tag-name array -- verified live,
                                 #    entry 440..442 are the dynamic tags D01..D03)
GS_STATES = 0x2C8                # CPdxArray<CState*>     cap/size 1082
GS_PROVINCES = 0x2B0             # cap/size 13414
GS_MAJORS = 0x328                # cap 9 size 7
GS_ARMIES = 0x640                # CPdxArray<CArmy*>      42 / 35
GS_DIV_TEMPLATES = 0x6F0         # CReferencedDivisionTemplate 711 / 705
GS_EQUIPMENT = 0x708             # CEquipmentVariant      5395 / 4513
GS_PAUSE_HOURS = None            # UNVERIFIED
GS_PLAYER_TAG = 0x520            # u32 player country TAG ID. Verified live by
                                 #   switching tag: 1 (GER) -> 16 (ITA) -> 1 (GER).
                                 #   (0x518 reads 15 = SPR and does NOT follow the
                                 #    player -- do not use it.)
GS_PLAYER_TAG_ALT = None         # UNVERIFIED on PE (explicitly override Linux 0x51C)

# --------------------------------------------------------------- CCountry
CO_SIZE = 0x1710                 # stride between CCountry allocations
CO_TAG_ID = 0x08                 # u32 tag id == country index in this build
CO_NAME = 0x10                   # std::string, LOCALISED ("德意志第三帝国")
CO_ADJECTIVE = 0x30              # std::string
CO_PLAYER = 0x120                # u32 1 == human player (also set on dummy country 0)
CO_STATES = 0x460                # CPdxArray<CState*> owned states (GER 24, ENG 94, SOV 152)
CO_STATES_B = 0x478              # second state list
CO_STATES_C = 0x4A8              # third state list
CO_ARMIES = 0x290                # CPdxArray<CArmy*> 42 / 30
CO_MODIFIERS = 0x5D0             # CPdxArray<CModifier*>
CO_STABILITY_BASE = 0x10D0       # int64 fixed(1e5): BASE stability only.
                                 #   GER 0.70 / FRA 0.45 / ENG 0.596 / SOV 0.50.
                                 #   The UI showed 81% for GER, so modifiers add
                                 #   +11 points; the computed value is not stored.
CO_WARSUPPORT_BASE = 0x10D8      # int64 fixed(1e5): BASE war support.
                                 #   GER 0.30 (UI 35%), FRA 0.10, ENG 0.10, SOV 0.30.

# status sub-objects (all RTTI-verified pointers)
CO_TECHNOLOGY_STATUS = 0xF60     # CTechnologyStatus*
CO_PRODUCTION_STATUS = 0xF68     # CProductionStatus*
CO_DEPLOYMENT_STATUS = 0xF70     # CDeploymentStatus*
# Inside CDeploymentStatus: CPdxArray<CMilitaryDeploymentConveyor*> at +0x48 and
# CPdxArray<CMilitaryDeployment> at +0x60.  The Linux build keeps the conveyor
# array at +0x68, which is why reading a single hard-coded offset produced an
# empty conveyor list here.  Verified 2026-11-15 by resolving the element vtable
# of each candidate array to CMilitaryDeploymentConveyor.
CO_DEPLOYMENT_CONVEYORS = 0x48
CO_DEPLOYMENT_QUEUE = 0x60
CO_REINFORCEMENT_STATUS = 0xF78
CO_ARMY_UPGRADES_STATUS = 0xF80
CO_DIPLOMACY_STATUS = 0xF88      # CDiplomacyStatus*
CO_POLITICAL_STATUS = 0xF90      # CPoliticalStatus*  <-- political power lives here
CO_LOGISTICS_STATUS = 0xF98
CO_DECISION_STATUS = 0xFA0
CO_OCCUPATION_STATUS = 0xFD0
CO_COLLABORATION_STATUS = 0xFD8
CO_INTEL = 0xFE8
CO_CHARACTERS = 0xFF0
# 2026-11-26 0 注入 14 国交叉实测证实（1.19.5.0 PE）：
#   +0x1018 = 当前实时首都州 ID（GER=64 柏林, FRA=16, ENG=126, SOV=219, CHI=1037 重庆, HOL=309 苏里南）
#   +0x101C = 原始开局首都州 ID（CHI=1035 南京, HOL=7 荷兰）
CO_CAPITAL_STATE = 0x1018
CO_ORIGINAL_CAPITAL_STATE = 0x101C
CO_RESOURCES = 0x11F8            # CCountryResources*
CO_NUKES = 0x1310                # CPdxArray<CNuke*> 2 / 2

CO_ECONOMY = None                # UNVERIFIED - 1.19.5 moved factories into status blocks
CO_POLITICS = 0xF90              # alias of CO_POLITICAL_STATUS (api.Country.politics)
CO_FOCUS_TREE = 0x1370            # CNationalFocusTree*    (vtable-scan verified 2026-11-13)
CO_CONT_FOCUS_PAL = 0x1378        # CContinuousFocusPalette*
CO_FOCUS_PROGRESS = 0x1380        # CNationalFocusProgress*
CO_FUEL_STATUS = 0x1580           # CCountryFuelStatus*
CO_EXPERIENCE_STATUS = 0x1588     # CCountryExperienceStatus*
CO_BUILDING_STATUS = 0x1350       # CBuildingStatus*
CO_MANPOWER_SUB = 0x328          # embedded CCountryManpower (RTTI-verified: 440
                                 #   instances, exactly one per CCountry)
CO_MANPOWER_CUR = None           # not a single field on this build -- see CSM_*
# CStateManpower is embedded in CState at +0x830 (verified: 1081 instances, every
# one exactly 0x830 past a CState).  Summed over the owned states these reproduce
# the save's manpower_pool exactly.
CSM_OFF = 0x830
CSM_AVAILABLE = 0x18             # int32, the number the top bar shows
CSM_LOCKED = 0x1C                # int32, population that cannot be recruited
CSM_POPULATION = 0x20            # int32
MP_RECRUIT_RATIO = 0x10          # int64 fixed(1e5) on CCountryManpower
CO_FUEL = None                   # UNVERIFIED

# ------------------------------------------------------- CPoliticalStatus
PO_POLITICAL_POWER = 0xE0        # int64 fixed(1e5); verified by differential scan
PO_COMMAND_POWER = 0x560         # int64 fixed(1e5); second accumulator
PO_RULING_SUB = 0xD0             # CPoliticalParty* of the ruling party.
                                 # 2026-11-20 live: GER pol+0xD0 pointed at the
                                 # fascism party (token 20009), inside the
                                 # CPolitics party array at +0x20. Then
                                 # CPoliticalParty+0x18 -> CIdeologyGroup,
                                 # whose key is a std::string at +0x18.

# ----------------------------------------------------------------- CState
CS_SIZE = None                   # UNVERIFIED
CS_ID = 0x10                     # u32 (stale)
ST_ID = 0x58                     # int32 state id (verified against GER state list)
ST_PROVINCES = 0x18              # CPdxArray<CProvince*> (verified: state 5 -> 11)
ST_BUILDINGS = 0x158             # CPdxArray<CBuilding*> (verified by the factory
                                 #   sums: GER 35+28+10 = 73 on the top bar)
ST_RESISTANCE = None             # UNVERIFIED on this build (Linux: +0x278)
ST_COMPLIANCE = None             # UNVERIFIED on this build (Linux: +0x2A8)
CS_OWNER = 0xC8                  # u32 country index
CS_CONTROLLER = 0xCC             # u32 country index

# ------------------------------------------------------- database items
# These are what `game._load_buildings()` needs.  The database itself is found
# through the MSVC RTTI map (`ObjectIndex.singleton("CBuildingDatabase")`); its
# CPdxArray<CBuildingTemplate*> of 51 entries sits at +0x160 (Linux: +0x40).
# Verified live 2026-11-13: every entry carries an int32 type id at +0x08
# (20002 arms_factory, 20013 industrial_complex, 19446 dockyard, ...) and a
# std::string key at +0x270.
BT_ID = 0x08                     # int32 building type id
BT_KEY = 0x270                   # std::string key ("industrial_complex")
BT_NAME = None                   # UNVERIFIED: the template carries no plain
                                 #   display string on this build; callers fall
                                 #   back to the key. (+0x230 holds "<key>_plural".)
BT_NAME_PLURAL = 0x230           # std::string loc key, present on some entries
BLD_DB_ITEMS = 0x160             # CPdxArray<CBuildingTemplate*> inside the db
# CBuilding type ids that are NOT in CBuildingDatabase.  `infrastructure` (12203)
# has no template entry of its own, yet every state carries a CBuilding of that
# type whose level is the state's infrastructure.  Verified against the campaign
# autosave: summing type 12203 over GER's 24 states gives 81, and the save's
# `buildings.infrastructure` total is also 81 (state 5: memory 3, save 3).
BT_EXTRA = {12203: "infrastructure"}
BG_LEVEL = 0x40                  # int16 level on CBuilding


# ------------------------------------------------------- CTechnologyTemplate
# The database (CTechnologyDatabase) keeps its templates at +0x48
# (CPdxArray<CTechnologyTemplate*>, cap 711 / size 553; empty slots are
# TNullObject).  The template's script key is a std::string at +0x10
# ("improved_ship_hull_light"), not the Linux +0x08.  Verified live 2026-11-14.
TT_NAME = 0x10
TT_ID = 0x3C

# ------------------------------------------------------ focus / economy

FP_CURRENT = None
FP_COMPLETED = None              # UNVERIFIED on PE (explicitly override Linux 0x40)
FP_ROOT_FOCUSES = None
EC_MIL_FACTORIES = None
EC_NAVAL_DOCKYARDS = None
EC_CIV_FACTORIES = None
EC_CIV_RESERVED_A = None
EC_CIV_RESERVED_B = None

FIXED = 100000

# ------------------------------------------------------------- functions
# Only addresses that were actually used are listed.  Everything else still
# needs to be located on the Windows build.
FN_CONSOLE_EXECUTE = None
FN_CONSOLE_DISPATCH = None
# 2026-11-21 (续测 P0-3, corrected after disassembling it): hoi4.exe+0x1E0310 is
#   NOT a pause setter.  It is a lookup helper:
#       void* f(CIdler* this /rcx/, int id /edx/)
#   It scans the 0xA0-byte entries at [this+0xF8] (count [this+0x104]) comparing
#   [entry+0x98] with edx, returns the matching entry, and returns this+0x110
#   when nothing matches (this+0x1B0 for id == -2).  Called with edx=1 it just
#   hands back a pointer; the pause byte never changes.  That is why every
#   resume attempt "was ignored": there was nothing to ignore.
FN_IDLER_GET_BY_ID = 0x1E0310
# The real pause state is the single byte idler+0x6C1 and the game's own SPACE
# handler toggles it with a plain store.  hoi4.exe+0xE48C2C (rbx = CInGameIdler):
#       cmp  byte ptr [rbx+0x6C1], 0
#       sete al
#       mov  byte ptr [rbx+0x6C1], al
# -> a direct write is the engine's own update, and measured on pid 29864 it
#    holds in both directions (1 freezes the clock, 0 resumes it).
IDLER_PAUSE_WRITE_SITE = 0xE48C36
# Keep the old name as a tombstone so nothing silently calls it as a setter.
FN_IDLER_SET_PAUSED = None
FN_TAG_GET_COUNTRY = None
FN_GET_POLITICS = None
FN_GET_STABILITY = None
FN_GET_WAR_SUPPORT = None
FN_GET_MANPOWER = None
FN_GET_COUNTRY_NAME = None
FN_GET_MODIFIER = None
FN_OPERATOR_NEW = None
FN_OPERATOR_DELETE = None

# console commands that were confirmed by name (see research/windows/wconsole3.py)
CMD_RVA = {
    "test": 0x2A5070,
    "deironman": 0x25FE10,
    "pp": 0x285FF0,
    "manpower": 0x281850,
    "tag": 0x2A4570,
    "annex": 0x256100,
    "whitepeace": 0x2AFEF0,
    "instant_wargoal": 0x27A8B0,
    "observe": 0x285120,
    "research": 0x294310,
}

# ------------------------------------------------------------------ vtables
VT_COUNTRY = 0x2864730
VT_GAMESTATE = (0x279F0F0, 0x279F148)              # base class, no live object
VT_CURRENT_GAMESTATE = (0x279F168, 0x279F1C0)      # the real one
VT_STATE = (0x29B4D58, 0x29B4DA8)
VT_WARGOAL = 0x2A59250

# --------------------------------------------------------------- structures
PDXARRAY_DATA = 0x00
PDXARRAY_CAP = 0x08
PDXARRAY_SIZE = 0x0C
PDXARRAY_ALLOC = 0x10
PDXARRAY_SZ = 0x18

STDSTRING_SZ = 0x20              # MSVC: {char buf[16]; size_t size; size_t cap;}

# ------------------------------------------------------------- command layer
# CInGameIdler instance (a u64 global). The object is real -- reading [idler]
# gives a CInGameIdler vtable -- but the Linux slot numbers do NOT carry over:
# see research/WINDOWS_1.19.5.md §24. vtable+0xB8 returns idler+0x6B8, which
# holds UI views (CTopBar/CAchievementsView), not the command queue.
G_IDLER = 0x33DDB08
IDLER_VT_QUEUE = None            # UNVERIFIED on this build (Linux: 0x90)
IDLER_VT_TAG = None              # UNVERIFIED on this build (Linux: 0xC0)
# vtable+0x08 = Allocate(this, size), vtable+0x10 = Deallocate(this, ptr).
# Verified round-trip through the injector (0x40 bytes alloc + free).
ALLOC_VTABLE = 0x2794550
ALLOC_OFF = 0x30FD000           # the allocator OBJECT lives at base + this

CMD_STRIDE = 0x1C8               # identical to the Linux build
CMD_NAME = 0x08
CMD_ALIAS_COUNT = 0x28
CMD_ALIASES = 0x30
CMD_FUNC = 0x68
CMD_CTX = None                   # UNVERIFIED

SRESULT_SZ = 0x28                # {bool ok; std::string message;}  (size at +0x18, cap at +0x20)
MGR_CMDS = 0x00

MODDEF_STRIDE = 0x78             # same as the Linux build
MODDEF_NAME = 0x00               # std::string, e.g. "MODIFIER_STABILITY_FACTOR"
MODDEF_ID = 0x6C                 # int32; equals the entry's index

DAYS_PER_YEAR = 365
MONTH_LENGTHS = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]

DATE_SIGNATURE_FLOAT = None
DATE_SIG_TO_VALUE = None

# ============================================================================
# 阶段 1 单一真源收敛表（PE 1.19.5.0 按域分组：ORDER / EVENT / PEACE / TRADE /
# CMD / LEADER / COMBAT / AIR / NAVAL / SESSION / GAME）
# ============================================================================

# ------------------------------------------------------- CCountry / CProvince 扩展
CO_THEATRES = 0x168              # CPdxArray<CTheatre*>
CO_OWNED_STATES = 0x478          # CPdxArray<CState*>, OWNED states
CO_DIV_TEMPLATES = 0x1B8         # CPdxArray<CReferencedDivisionTemplate*>
CO_FLEETS = 0x278                # CPdxArray<CFleet*>
CO_COMMAND_POWER = 0x1F0         # int64 x1e5
CO_FOCUS_COMPLETED = 0x1C10      # CPdxArray<CNationalFocus*>
CO_SUBUNITS = 0x26C0             # CPdxArray<CSubUnitDefinition*>
CO_RELATIONS_ARR = 0x2888        # CPdxArray<CRelationStatus*>
CO_PROGRAM_STATUS = 0xFA8        # NProject::CProgramStatus* (PE)
CO_OPERATIONS = 0x1548           # CCountryOperationManager*
CO_PROJECT_POOL = 0x15D8         # NProject::CProjectPool*
CO_EQUIPMENT_TYPES = 0x11E0      # CPdxArray<CEquipmentType*>
CO_TRADE_ROUTES = 0x112B08       # CPdxArray<CResourceDeliveryRoute*>
CO_RESOURCE_ORIGINS = 0x112A90
PROV_ID = 0xA4                   # CProvince -> int32 province id
ST_BUILDING_LEVELS = 0x128       # CState 56-entry building level array
ST_BUILDING_MAX = 0x140
FACTION_MEMBERS = 0x58           # CFaction -> CPdxArray<CCountry*>

# ------------------------------------------------------- ORDER_* (armyops_win / unitorders)
ORDER_FN_CREATE_ARMY = 0x00C417A0
ORDER_FN_NEW_OG = 0x00F702D0
ORDER_FN_RESOLVE = 0x142284870   # 绝对 VA (PE_IMAGE_BASE + 0x2284870)
ORDER_CARMY_VT = 0x29D6D98
ORDER_CARMY_HANDLE = 0x18
ORDER_CARMY_OG = 0xC0
ORDER_CARMY_SECONDARY = 0xB8
ORDER_CARMY_TEMPLATE = 0x138        # CArmy -> CDivisionTemplate* (0x10B3763 实证)
ORDER_DT_CAN_PARADROP = 0x5B5       # CDivisionTemplate + 0x5B5 u8 (1=伞兵师, 42 师 6/36 活进程实证)
ORDER_CARMY_PROVINCE = 0x1F0
ORDER_CARMY_PATH = 0x200
ORDER_CARMY_PATH_SIZE = 0x20C
ORDER_OG_VT = 0x29CEDC0             # COrdersGroup 主虚表 RVA (活进程实证 0x29CEDC0)
ORDER_OG_VT_SECONDARY = 0x29CEE58   # COrdersGroup 次虚表 RVA (含 0x29CEED0)
ORDER_OG_HANDLE = 0x08
ORDER_OG_IS_GROUP = 0x39
ORDER_OG_THEATRE = 0x40
ORDER_OG_DIVISIONS = 0x50
ORDER_OG_LEADER = 0x88
ORDER_OG_ORDERS = 0x98
ORDER_OG_NAME = 0x160
ORDER_OG_PARENT = 0x1B8
ORDER_OG_MEMBERS = 0x228
ORDER_FN_NEW_ROOT_CMD = 0x18C9B20
ORDER_NR_VT = 0x2A856A0
ORDER_VT_OFFENSIVE = 0x2A853A0
ORDER_NR_CANEXE = 0x18DF5E0
ORDER_NR_EXECUTE = 0x18D9A30
ORDER_NR_SIZE = 0x70
ORDER_CO_FRONTS = 0x50
ORDER_FRONT_HANDLE = 0x08
ORDER_FRONT_PROVS = 0x20
ORDER_FRONT_SECTIONS = 0x58
ORDER_SEC_PROVS = 0x30
ORDER_SEC_ID = 0x08
ORDER_VT_ORDER_INSTANCE = 0x2A026E8
ORDER_OI_ASSIGNED = 0x210
ORDER_OI_EXECUTING = 0x248
ORDER_OI_ID = 0x244
ORDER_OI_TYPE = 0x30
ORDER_FN_SET_EXECUTING = 0x10CD510
ORDER_FN_NEW_OFFENSIVE_CMD = 0x18C9840
ORDER_FN_FRONT_CTX = 0x10BB5A0
ORDER_FN_CANEXE_OFFENSIVE = 0x18DF550
ORDER_OE_CTOR = 0x18C8C50
ORDER_OE_VT = 0x2A85D60
ORDER_OE_SIZE = 0x38
ORDER_FN_OFFENSIVE_EXECUTE = 0x18D9070
ORDER_FN_OFFENSIVE_GATE = 0x33DDAB7
ORDER_FN_PLAN_FACTORY = 0x18CFC70
ORDER_VT_COMPLETE_PLAN = 0x2A85760
ORDER_FN_PLAN_CANEXE = 0x18DE930
ORDER_PLAN_SIZE = 0x80
ORDER_PLAN_ARMY = 0x28
ORDER_PLAN_UNITS = 0x30
ORDER_PLAN_PROVS = 0x48
ORDER_PLAN_FRONT = 0x64
ORDER_PLAN_SECT = 0x6C
ORDER_PLAN_FROM = 0x70
ORDER_PLAN_TO = 0x78
ORDER_FN_FACTORY = 0x00C41720
ORDER_FN_SETPARENT = 0x0010B4EC0
ORDER_FN_SETPATH = 0x0010B0D90
ORDER_FN_DETACH_ALL = 0x0010B5D50
ORDER_FN_UI_FRONTCTX_REFRESH = 0xF711E0
ORDER_DIV_SUBOBJ_OFF = 0xB8
ORDER_VT_ASSIGN = 0x2A84CE0
ORDER_CANEXEC_ASSIGN = 0x18DE970
ORDER_SIZE_ASSIGN = 0x58
ORDER_FN_DELETE_CTOR = 0x18C89D0
ORDER_VT_DELETE = 0x2A84C20
ORDER_CANEXEC_DELETE = 0x18DEF50
ORDER_SIZE_DELETE = 0x38
ORDER_FN_ARMY_GROUP_CTOR = 0x18C6840
ORDER_ARMY_GROUP_VT = 0x2A846E0
ORDER_ARMY_GROUP_SIZE = 0x80
ORDER_MV_CTOR = 0x13F6C30
ORDER_MV_SIZE = 0x88
ORDER_ACT_OFF = 0x28
ORDER_UNIT_MOVE_TOKEN = 0x3648
# 2026-11-26 (PE 1.19.5.0 伞降双命令静态反汇编实证):
#   COrderSetParadropSourceCommand (size 0x50): +0x28 og_handle, +0x30 order_id(0=新建type4),
#     +0x34 src_prov_id, +0x38 CPdxArray<CArmy handle>
#   COrderSetParadropTargetCommand (size 0x38): +0x28 og_handle, +0x30 order_id, +0x34 tgt_prov_id
ORDER_VT_PARADROP_SOURCE = 0x2A86420
ORDER_SIZE_PARADROP_SOURCE = 0x50
ORDER_FN_PARADROP_SOURCE_FACTORY = 0x18D0880
ORDER_FN_PARADROP_SOURCE_CTOR = 0x18CAB60
ORDER_CANEXEC_PARADROP_SOURCE = 0x18DFA50
ORDER_EXEC_PARADROP_SOURCE = 0x18DAC20
ORDER_VT_PARADROP_TARGET = 0x2A864E0
ORDER_SIZE_PARADROP_TARGET = 0x38
ORDER_FN_PARADROP_TARGET_FACTORY = 0x18D08F0
ORDER_FN_PARADROP_TARGET_CTOR = 0x18CAD40
ORDER_CANEXEC_PARADROP_TARGET = 0x18DFCE0
ORDER_EXEC_PARADROP_TARGET = 0x18DAF00
ORDER_FN_PARADROP_SRC_VALID = 0x10BF1A0
ORDER_FN_PARADROP_TGT_VALID = 0x10BE700

# ------------------------------------------------------- EVENT_* (events_win)
EVENT_EVT_VTS = (0x2A16008, 0x2A16060)
EVENT_WND_VTS = (0x2A20830, 0x2A20858, 0x2A20890, 0x2A208A8)
EVENT_WND_OFF_EVENT = 0x68
EVENT_OFF_ID = 0x20
EVENT_OFF_OPTS = 0x368
EVENT_OFF_COUNT = 0x374
EVENT_GS_PENDING = 0x560
EVENT_GS_PENDING_N = 0x56C
EVENT_PEND_STRIDE = 0x38
EVENT_SEL_VT = 0x29B6CE0
EVENT_SEL_SIZE = 0xF0
EVENT_SEL_CTOR = 0xA590D0
EVENT_SEL_CANEXEC = 0x15D66C0
EVENT_SEL_EXECUTE = 0x15D53E0
EVENT_SEL_SCOPE_OFF = 0x38
EVENT_SEL_SCOPE_SIZE = 0xB0
EVENT_SCOPE_VT = 0x2852168

# ------------------------------------------------------- PEACE_* (peacewin)
PEACE_CANEXEC_DONE = 0x1ED23A0
PEACE_CANEXEC_PASS = 0x1ED2A00
PEACE_DONE_VT = 0x2AFC800
PEACE_DONE_CTOR = 0x1ED1D70
PEACE_DONE_SIZE = 0x30
PEACE_ENDTURN_VT = 0x2AFC8C0
PEACE_ENDTURN_SIZE = 0x38
PEACE_PASS_VT = 0x2AFC740
PEACE_PASS_CTOR = 0x1ED1E50
PEACE_PASS_SIZE = 0x30
PEACE_EXEC_DONE = 0x1ED22E0
PEACE_EXEC_ENDTURN = 0x1ED2300
PEACE_EXEC_PASS = 0x1ED2350
PEACE_CONF_TURN_MAP = 0x1E8
PEACE_CONF_BID_MAP = 0x1C8
PEACE_CONF_SUBMIT_MAP = 0x1F8
PEACE_VT_TURN = 0x29EAEC0
PEACE_TURN_SIZE = 0x40
PEACE_FN_INSTALL_TURN = 0xEB5390
PEACE_FN_PUSH_BACK = 0x1403371D0
PEACE_BUILD_CHANGE_GOV = 0x14F26C0
PEACE_BUILD_LIBERATE = 0x14F2870
PEACE_BUILD_PUPPET = 0x14F3070
PEACE_BUILD_NAVY = 0x14F3560
PEACE_BUILD_STATES = 0x14F3950
PEACE_BUILD_ALL = 0x14F2680
PEACE_VT_CONFERENCE = 0x29CDA28
PEACE_GET_CONF = 0x1ED1EE0
PEACE_GS_CONF = 0x4E0
PEACE_PUPPET_CTOR = 0x15B4530
PEACE_NAVY_CTOR = 0x15B4600
PEACE_ACT_COST = 0x40               # CPeaceAction + 0x40 i32 分值 (0x1A86D10 TurnCost 实证)
PEACE_ACT_TYPE_TAKE_STATE = 0x30CF  # CTakeStatePeaceAction type id (0xEA9F00 实证)

# ------------------------------------------------------- TRADE_* (trade_win)
TRADE_BOOK = 0x728
TRADE_BOOK_CAP = 0x730
TRADE_BOOK_SIZE = 0x734
TRADE_SLOT_STRIDE = 0x18
TRADE_EX_PARTNER = 0x88
TRADE_EX_RESOURCE = 0x90
TRADE_EX_AMOUNT = 0x98
TRADE_EX_SIZE = 0xF0
TRADE_RES_INDEX = 0xD8
TRADE_VT_CREATE = 0x2A5F388
TRADE_SIZE_CREATE = 0x40
TRADE_CANEXEC_CREATE = 0x16DABF0

# ------------------------------------------------------- CMD_* (cmdwin)
CMD_FN_POST = 0x22C4250
CMD_FN_GET_COUNTRY = 0xC07690
CMD_FN_RESOLVE_HANDLE = 0x2284870
CMD_FN_SET_GAME_SPEED = 0x203F10
CMD_QUEUE_VT_PUSH = 0x28
CMD_SESSION_BUSY_FLAG = 0x725
CMD_SESSION_QUEUE = 0x40
CMD_IDLER_VT_SESSION = 0x88
CMD_IDLER_VT_TAG = 0xA0
CMD_CTOR_SET_NATIONAL_FOCUS = 0x11D0DC0
CMD_CTOR_SET_RESEARCH = 0x11D1980
CMD_CTOR_ADD_CONSTRUCTION = 0x11CC8C0
CMD_VT_ADD_CONSTRUCTION = 0x2A10B88
CMD_VT_BUILDING_REF = 0x29EE730
CMD_OP_NEW = 0x25A01DC
CMD_OP_DELETE = 0x25CEC00
CMD_OP_DELETE_SIZED = 0x25A0218
CMD_FN_NOOP_DTOR = 0x11D530
CMD_VT_ADD_ADVISOR = 0x2A11A88
CMD_ADD_ADVISOR_SIZE = 0x50
CMD_CTOR_ADD_ADVISOR = 0x11CC6D0
CMD_VT_UNLOCK_DOCTRINE = 0x2AAA8A0
CMD_UNLOCK_DOCTRINE_SIZE = 0x38
CMD_CTOR_UNLOCK_DOCTRINE = 0x1B0FD20
CMD_CANEXEC_ADD_ADVISOR = 0x11ECC20
CMD_CANEXEC_UNLOCK_DOCTRINE = 0x1B10310
CMD_CANEXEC_SET_RESEARCH = 0x11F3360
CMD_CTOR_DECLARE_WAR = 0x1180B70
CMD_VT_DECLARE_WAR = 0x2A07548
CMD_VT_DIPLO_CMD = 0x2A073C8
CMD_FN_DIPLO_VALIDATE = 0x11C2420
CMD_SCOPE_MAGIC = 0x29C77F8
CMD_DECLARE_WAR_FLAG_OFF = 0x70
CMD_VT_SET_GAME_SPEED = 0x29F3A80
CMD_CTOR_SET_GAME_SPEED = 0xF84510
CMD_SIZE_SET_GAME_SPEED = 0x30
CMD_CANEXEC_SET_GAME_SPEED = 0xF85FB0
CMD_VT_INCREASE_GAME_SPEED = 0x29F3900
CMD_VT_GENERATE_WARGOAL = 0x2A079B8
CMD_CTOR_GENERATE_WARGOAL_DEFAULT = 0x1181220
CMD_GENERATE_WARGOAL_SIZE = 0xA0
CMD_CTOR_DROP_NATIONAL_FOCUS = 0x11CE6F0
CMD_VT_DROP_NATIONAL_FOCUS = 0x2A12988
CMD_SIZE_DROP_NATIONAL_FOCUS = 0x30
CMD_VT_SET_NATIONAL_FOCUS = 0x2A128C8
CMD_VT_SET_RESEARCH = 0x2A11548
CMD_VT_SET_PROD_LINE = 0x2A0FF88
CMD_CTOR_SET_PROD_LINE = 0x11D1550
CMD_CANEXEC_SET_PROD_LINE = 0x11F2C30
CMD_SIZE_SET_PROD_LINE = 0x38
# 2026-11-26 (P1-3 PE 1.19.5.0 反汇编实证): 减训练线 / 删传送带命令
CMD_VT_REMOVE_CONVEYOR_LINE = 0x2AC08D0
CMD_CTOR_REMOVE_CONVEYOR_LINE = 0x1C2F020
CMD_CANEXEC_REMOVE_CONVEYOR_LINE = 0x1C31940
CMD_SIZE_REMOVE_CONVEYOR_LINE = 0x30
CMD_VT_REMOVE_CONVEYOR = 0x2AC0690
CMD_CTOR_REMOVE_CONVEYOR = 0x1C2EF50
CMD_CANEXEC_REMOVE_CONVEYOR = 0x1C316E0
CMD_SIZE_REMOVE_CONVEYOR = 0x30

# ------------------------------------------------------- LEADER_* (armyops_win / legality / game)
LEADER_AL_ATTACK = 0xF58
LEADER_AL_DEFENSE = 0xF68
LEADER_AL_PLANNING = 0xF78
LEADER_AL_LOGISTICS = 0xF88
LEADER_AL_NAME = 0x40
LEADER_AL_TAG = 0x120
LEADER_AL_RANK = 0xE18
LEADER_CH_NAME = 0x28
LEADER_CH_KEY = 0x48
LEADER_PARTY_IDEOLOGY_TOKEN = 0x08
LEADER_PARTY_IDEO_GROUP = 0x18
LEADER_IDEO_GROUP_KEY = 0x18

# ------------------------------------------------------- COMBAT_* (combat)
COMBAT_LC_SIDE_A = 0x28
COMBAT_LC_SIDE_B = 0x30
COMBAT_LC_PROVINCE = 0x38
COMBAT_LC_TERRAIN = 0x48
COMBAT_CB_GROUND_STR = 0x158
COMBAT_CB_GROUND_ORG = 0x160
COMBAT_CB_AIR_STR = 0x148
COMBAT_CB_AIR_ORG = 0x150
COMBAT_A_HANDLE = 0x18
COMBAT_A_STR = 0x420
COMBAT_A_ORG = 0x428
COMBAT_A_XP = 0x430
COMBAT_A_OOS = 0x498
COMBAT_A_TAG = 0x1D8
COMBAT_A_MANPOWER = 0x3D0
COMBAT_A_STR_DAMAGE = 0x448
COMBAT_A_ORG_DAMAGE = 0x450
COMBAT_A_KILLED = 0x678
COMBAT_A_TMPL = 0x3B8
COMBAT_ATTACK_PROV = 0x1F0
COMBAT_CO_ARMIES_IN_COMBAT = 0x12F0
COMBAT_DS_WAR_RELATIONS = 0x20
COMBAT_WR_FIRST = 0x0C
COMBAT_WR_SECOND = 0x10
COMBAT_WR_INSTIGATOR = 0x198
COMBAT_WR_THREAT = 0x190
COMBAT_WR_WIN_VTABLE = 0x29DCC08
COMBAT_WR_WIN_A = 0x78
COMBAT_WR_WIN_B = 0x7C
COMBAT_WR_WIN_A2 = 0x10
COMBAT_WR_WIN_B2 = 0x14

# ------------------------------------------------------- AIR_* (airops_win)
AIR_VT_WING = (0x29F7748, 0x29F7780)
AIR_VT_WING_MAIN = 0x29F7748
AIR_VT_WING_SECONDARY = 0x29F7780
AIR_VT_WINGPOOL = 0x29F77D8
AIR_GS_MGR = 0x690                  # CCurrentGameState + 0x690 -> CStrategicAirManager*
AIR_SAM_STRAT_AIRS = 0x30           # CStrategicAirManager + 0x30 -> CPdxArray<CStrategicAir*>[tag_idx]
AIR_SAM_LAND_AIRBASE_PROVS = 0x48   # CStrategicAirManager + 0x48 -> CPdxArray<i32>[state_id] 陆地机场省表 (0xCB0DA2)
AIR_SA_AIRBASE_MGR = 0x88           # CStrategicAir + 0x88 -> CAirBaseManager*
AIR_ABM_AIRBASES = 0x90             # CAirBaseManager + 0x90 -> CPdxArray<CAirBase*>[state_id]
AIR_AB_COUNTRIES = 0x30             # CAirBase + 0x30 -> CPdxArray<CAirBaseCountry*>[tag_idx]
AIR_ABC_POOLS = 0xB8                # CAirBaseCountry + 0xB8 -> CPdxArray<CAirWingPool*>
AIR_AWP_DEF = 0x20                  # CAirWingPool + 0x20 -> CAirWingDefinition*
AIR_AWP_WINGS = 0x28                # CAirWingPool + 0x28 -> CPdxArray<CAirWing*>
AIR_AWDEF_MISSION_FLAGS = 0x5A8     # CAirWingDefinition + 0x5A8 u64 任务位掩码
AIR_AWDEF_PARADROP_BIT = 0x100000   # bit 20: 支持伞降/运输机 (0xCA9F40 实证)
AIR_SA_OWNER = 0x90
AIR_WING_MIN_0x60 = 0x60            # CAirWing 可用下限 (0xFE2B90: [+0x64] >= [+0x60])
AIR_WING_CUR_0x64 = 0x64            # CAirWing 当前可用计数
AIR_WING_BUSY_0x79 = 0x79           # CAirWing 忙碌/转移标志 (0=就绪)
AIR_WING_PLANES = 0x7C
AIR_WING_MISSION = 0xA0
AIR_WING_MISSION_ON = 0xA4
AIR_WING_RANGE = 0x500              # CAirWing + 0x500 i64 航程固定点 (/100000.0 km, 0xCAA200 实证)
# 2026-11-26 (PE 1.19.5.0 CStratAirTransferCommand 反汇编实证):
#   vt=0x2A96CA8, ctor=0x19CCCF0, clone=0x19CF3F0 (size 0x48), CanExecute=0x19D4CC0, Execute=0x19D2090
#   +0x28 u64 wing_handle (type 69), +0x30 u64 target_airbase_handle (type 65),
#   +0x38 u8 to_reserve, +0x39 u8 keep_army_attachment (ctor=0), +0x3C i32 region_id, +0x40 i32 mission_mask
#   Execute -> CAirWing::TransferTo (0xFEBFD0) 立即将目标 CAirBase 句柄写入 wing+0x48，region/mission 写入 +0x70/+0x74
AIR_WING_TRANSFER_TARGET = 0x48
AIR_WING_TRANSFER_REGION = 0x70
AIR_WING_TRANSFER_MISSION = 0x74
AIR_VT_ATTACH = 0x2A97728
AIR_SIZE_ATTACH = 0x48
AIR_CANEXEC_ATTACH = 0x19D3920
AIR_VT_SET_MISSION = 0x2A96BE8
AIR_SIZE_SET_MISSION = 0x48
AIR_CANEXEC_SET_MISSION = 0x19D4B30
AIR_VT_ENABLE_MISSION = 0x2A96B28
AIR_SIZE_ENABLE_MISSION = 0x60
AIR_CANEXEC_ENABLE_MISSION = 0x19D4740
AIR_VT_TRANSFER = 0x2A96CA8
AIR_SIZE_TRANSFER = 0x48
AIR_CANEXEC_TRANSFER = 0x19D4CC0
AIR_EXEC_TRANSFER = 0x19D2090

# ------------------------------------------------------- NAVAL_* (navalops_win)
NAVAL_VT_SET_TYPE = 0x2A2A808
NAVAL_SIZE_SET_TYPE = 0x50
NAVAL_CANEXEC_SET_TYPE = 0x13EE8C0
NAVAL_EXEC_SET_TYPE = 0x13E6C40
NAVAL_VT_ADD_REGION = 0x2A2A988
NAVAL_SIZE_ADD_REGION = 0x38
NAVAL_CANEXEC_ADD_REGION = 0x13EDF70
NAVAL_EXEC_ADD_REGION = 0x13E5FE0
NAVAL_VT_REMOVE_REGION = 0x2A2AA48
NAVAL_SIZE_REMOVE_REGION = 0x48
NAVAL_CANEXEC_REMOVE_REGION = 0x13EE560
NAVAL_EXEC_REMOVE_REGION = 0x13E6570
NAVAL_FLEET_VTS = (0x29DF3D8, 0x29DF430)
NAVAL_FLEET_VT = 0x29DF3D8
NAVAL_TASKFORCE_VTS = (0x29DF900, 0x29DFAE0, 0x29DFB50, 0x29DFB98)
NAVAL_TASKFORCE_VT = 0x29DF900
NAVAL_FLEET_HANDLE_OFF = 0x08
NAVAL_FLEET_NAME_OFF = 0xE0
NAVAL_FLEET_REGION_COUNT_OFF = 0x54
NAVAL_FLEET_REGION_DATA_OFF = 0x48
NAVAL_FLEET_TF_ARRAY_OFF = 0xB8
NAVAL_TF_HANDLE_OFF = 0x18
NAVAL_TF_SHIP_COUNT_OFF = 0xA0
NAVAL_TF_MISSION_TYPE_OFF = 0x374
NAVAL_TF_MISSION_OBJ_OFF = 0x360
NAVAL_TF_ACTIVITY_OFF = 0x4B8
NAVAL_TF_QUEUED_MISSION_OFF = 0x4F0
NAVAL_REGION_ARRAY_OFF = 0x2E0
NAVAL_REGION_COUNT_OFF = 0x2EC
NAVAL_REGION_VALID_OFF = 0xA0

# ------------------------------------------------------- SESSION_* (session)
SESSION_IDLER_COUNTRY_NAME = 0x620
SESSION_IDLER_LOCAL_PLAYER = 0x7C0
SESSION_NET = 0x58
SESSION_CLIENT_FIRST = 0x348
SESSION_CLIENT_STRIDE = 0x700
SESSION_IDLER_PAUSED = 0x6C1

# ------------------------------------------------------- GAME 子结构域偏移 (game.py)
FT_FOCUSES = 0x30
NF_ID = 0x08
NF_KEY = 0x18
NF_LOCKEY = 0x38
NF_NAME = 0x60
NF_ICON = 0x88
NF_AVAIL_TRIGGER_N = 0x2DC
NF_PREREQ = 0x568
NF_MUTEX = 0x580
NF_FILTERS = 0x5D8
# 注意：保留上方 FP_CURRENT/FP_COMPLETED/FP_ROOT_FOCUSES = None 墓碑不覆盖，
# 供 game.py 内部结构体访问的偏移独立命名为 FOCUS_FP_*。
FOCUS_FP_COUNTRY = 0x08
FOCUS_FP_CURRENT = 0x10
FOCUS_FP_COMPLETED = 0x40
FOCUS_FP_ROOT_FOCUSES = 0x70
FOCUS_FP_DAILY = 0x38
TS_RESEARCHED = 0x40
TS_ALL_TECHS = 0x88
TS_SLOTS = 0xA0
TS_PRODUCTION_LINES = 0x2B8
RS_OWNER = 0x10
RS_CURRENT = 0x18
RS_PROGRESS = 0x20
RS_REQUIRED = 0x28
RS_SPEED = 0x38
RS_INDEX = 0x40
TE_ID = 0x08
TE_TEMPLATE = 0x160
TE_PROGRESS = 0x198
TT_YEAR = 0x3D8
TT_RESEARCH_COST = 0x3E0
PS_LINES = 0x58
PS_MIL_LINES = 0x58
PS_CONSTRUCTION = 0x70
PS_VARIANTS_ALL = 0xA0
PS_VARIANTS_ACTIVE = 0xB8
ML_OWNER = 0x20
ML_PRIORITY = 0xF0
ML_FACTORIES = 0x18
ML_UNIT_COST = 0x28
ML_OUTPUT = 0x30
ML_EFF_COST = 0x38
ML_MAX_EFFICIENCY = 0xC8
ML_VARIANT = 0x88
ML_TOTAL_PRODUCED = 0xF8
BL_OWNER = 0x20
BL_PROGRESS = 0x38
BL_RATE = 0x30
BL_COST = 0x28
BL_BUILDING = 0x78
EV_TYPE_ID = 0x18
ET_ID = 0x08
ET_NAME = 0x18
BG_TYPE_ID = 0x08
PL_PARTIES = 0x20
PL_ALL_IDEAS = 0x38
PL_ACTIVE_IDEAS = 0x50
PL_RULING = 0xD0
PL_PP = 0xE0
DS_ALL = 0x10
DS_ACTIVE = 0xD0
DS_VISIBLE = 0x88
DS_TARGETED = 0xE8
DS_RUNNING = 0x148
DP_RELATIONS = 0x08
IDEA_NAME = 0x18
DECISION_NAME = 0x120
RES_ARRAY = 0x60
RES_STRIDE = 0x10
DT_NAME = 0x20
SU_KEY = 0x10
SU_CATEGORY = 0x60
FL_NAME = 0xD8
REL_INDEX = 0x50

# ------------------------------------------------- data-driven profile overlay
# 第二期数据驱动解耦：优先从 `calling/profile.py`（由 native/hoi4_recalc 静态重算生成）
# 加载与当前 hoi4.exe 指纹匹配的锚点；缺失或离线环境下自动保留上方 1.19.5.0 常量回落。
try:
    import profile as _prof
    _p_pe = (_prof.load() or {}).get("pe") or {}
    if _p_pe.get("build_id"):
        BUILD_ID = _p_pe["build_id"]
    if _p_pe.get("image_base"):
        PE_IMAGE_BASE = int(_p_pe["image_base"])
    if _p_pe.get("size_of_image"):
        PE_SIZE_OF_IMAGE = int(_p_pe["size_of_image"])
    G_DEFAULT_ALLOC = _prof.anchor("G_DEFAULT_ALLOC", G_DEFAULT_ALLOC)
    ALLOC_OFF = G_DEFAULT_ALLOC
    ALLOC_VTABLE = _prof.anchor("ALLOC_VTABLE", ALLOC_VTABLE)
    G_GAMESTATE = _prof.anchor("G_GAMESTATE", G_GAMESTATE)
    G_IDLER = _prof.anchor("G_IDLER", G_IDLER)
    GS_GAME_SPEED = _prof.anchor("GS_GAME_SPEED", GS_GAME_SPEED)
    G_GAME_SPEED_SECONDS = _prof.anchor("G_GAME_SPEED_SECONDS", G_GAME_SPEED_SECONDS)
    GS_COUNTRIES = _prof.anchor("GS_COUNTRIES", GS_COUNTRIES)
    CO_TECHNOLOGY_STATUS = _prof.anchor("CO_TECHNOLOGY_STATUS", CO_TECHNOLOGY_STATUS)
    CO_PRODUCTION_STATUS = _prof.anchor("CO_PRODUCTION_STATUS", CO_PRODUCTION_STATUS)
    CO_DEPLOYMENT_STATUS = _prof.anchor("CO_DEPLOYMENT_STATUS", CO_DEPLOYMENT_STATUS)
    CO_POLITICAL_STATUS = _prof.anchor("CO_POLITICAL_STATUS", CO_POLITICAL_STATUS)
    CO_POLITICS = CO_POLITICAL_STATUS
    CO_FOCUS_TREE = _prof.anchor("CO_FOCUS_TREE", CO_FOCUS_TREE)
    IDLER_PAUSE_WRITE_SITE = _prof.anchor("IDLER_PAUSE_WRITE_SITE", IDLER_PAUSE_WRITE_SITE)
    VT_COUNTRY = _prof.anchor("VT_COUNTRY", VT_COUNTRY)
    VT_GAMESTATE = _prof.anchor_list("VT_GAMESTATE", VT_GAMESTATE)
    VT_CURRENT_GAMESTATE = _prof.anchor_list("VT_CURRENT_GAMESTATE", VT_CURRENT_GAMESTATE)
    VT_STATE = _prof.anchor_list("VT_STATE", VT_STATE)
    VT_WARGOAL = _prof.anchor("VT_WARGOAL", VT_WARGOAL)
    VT_MIL_LINE = _prof.anchor("VT_MIL_LINE", VT_MIL_LINE)
    VT_NAV_LINE = _prof.anchor("VT_NAV_LINE", VT_NAV_LINE)
    VT_BUILDING_LINE = _prof.anchor("VT_BUILDING_LINE", VT_BUILDING_LINE)
    # 其余 35 个 profile 锚点同步 overlay 到分组真源
    SESSION_IDLER_PAUSED = _prof.anchor("IDLER_PAUSED", SESSION_IDLER_PAUSED)
    CMD_FN_POST = _prof.anchor("FN_POST", CMD_FN_POST)
    CMD_FN_GET_COUNTRY = _prof.anchor("FN_GET_COUNTRY", CMD_FN_GET_COUNTRY)
    CMD_FN_RESOLVE_HANDLE = _prof.anchor("FN_RESOLVE_HANDLE", CMD_FN_RESOLVE_HANDLE)
    CMD_FN_SET_GAME_SPEED = _prof.anchor("FN_SET_GAME_SPEED", CMD_FN_SET_GAME_SPEED)
    CMD_QUEUE_VT_PUSH = _prof.anchor("CMD_QUEUE_VT_PUSH", CMD_QUEUE_VT_PUSH)
    CMD_SESSION_BUSY_FLAG = _prof.anchor("SESSION_BUSY_FLAG", CMD_SESSION_BUSY_FLAG)
    CMD_SESSION_QUEUE = _prof.anchor("SESSION_CMD_QUEUE", CMD_SESSION_QUEUE)
    CMD_OP_NEW = _prof.anchor("OP_NEW", CMD_OP_NEW)
    CMD_OP_DELETE_SIZED = _prof.anchor("OP_DELETE_SIZED", CMD_OP_DELETE_SIZED)
    CMD_FN_NOOP_DTOR = _prof.anchor("FN_NOOP_DTOR", CMD_FN_NOOP_DTOR)
    CMD_VT_BUILDING_REF = _prof.anchor("VT_BUILDING_REF", CMD_VT_BUILDING_REF)
    CMD_VT_DECLARE_WAR = _prof.anchor("VT_DECLARE_WAR", CMD_VT_DECLARE_WAR)
    CMD_VT_DIPLO_CMD = _prof.anchor("VT_DIPLO_CMD", CMD_VT_DIPLO_CMD)
    CMD_FN_DIPLO_VALIDATE = _prof.anchor("FN_DIPLO_VALIDATE", CMD_FN_DIPLO_VALIDATE)
    CMD_VT_GENERATE_WARGOAL = _prof.anchor("VT_GENERATE_WARGOAL", CMD_VT_GENERATE_WARGOAL)
    EVENT_EVT_VTS = _prof.anchor_list("VT_EVENT", EVENT_EVT_VTS)
    EVENT_WND_VTS = _prof.anchor_list("VT_EVENT_WINDOW", EVENT_WND_VTS)
    EVENT_SCOPE_VT = _prof.anchor("VT_EVENT_SCOPE", EVENT_SCOPE_VT)
    PEACE_CANEXEC_DONE = _prof.anchor("PEACE_CANEXEC_DONE", PEACE_CANEXEC_DONE)
    PEACE_CANEXEC_PASS = _prof.anchor("PEACE_CANEXEC_PASS", PEACE_CANEXEC_PASS)
    PEACE_DONE_VT = _prof.anchor("PEACE_DONE_VT", PEACE_DONE_VT)
    PEACE_ENDTURN_VT = _prof.anchor("PEACE_ENDTURN_VT", PEACE_ENDTURN_VT)
    PEACE_PASS_VT = _prof.anchor("PEACE_PASS_VT", PEACE_PASS_VT)
    PEACE_EXEC_DONE = _prof.anchor("PEACE_EXEC_DONE", PEACE_EXEC_DONE)
    PEACE_EXEC_ENDTURN = _prof.anchor("PEACE_EXEC_ENDTURN", PEACE_EXEC_ENDTURN)
    PEACE_EXEC_PASS = _prof.anchor("PEACE_EXEC_PASS", PEACE_EXEC_PASS)
    PEACE_FN_INSTALL_TURN = _prof.anchor("PEACE_FN_INSTALL_TURN", PEACE_FN_INSTALL_TURN)
    PEACE_GET_CONF = _prof.anchor("PEACE_GET_CONF", PEACE_GET_CONF)
    PEACE_VT_CONFERENCE = _prof.anchor("VT_PEACE_CONFERENCE", PEACE_VT_CONFERENCE)
    PEACE_VT_TURN = _prof.anchor("VT_PEACE_TURN", PEACE_VT_TURN)
    AIR_VT_WING = _prof.anchor_list("VT_AIRWING", AIR_VT_WING)
    if len(AIR_VT_WING) >= 2:
        AIR_VT_WING_MAIN, AIR_VT_WING_SECONDARY = AIR_VT_WING[0], AIR_VT_WING[1]
    AIR_VT_WINGPOOL = _prof.anchor("VT_AIRWINGPOOL", AIR_VT_WINGPOOL)
    NAVAL_FLEET_VTS = _prof.anchor_list("VT_FLEET", NAVAL_FLEET_VTS)
    if NAVAL_FLEET_VTS:
        NAVAL_FLEET_VT = NAVAL_FLEET_VTS[0]
    NAVAL_TASKFORCE_VTS = _prof.anchor_list("VT_TASKFORCE", NAVAL_TASKFORCE_VTS)
    if NAVAL_TASKFORCE_VTS:
        NAVAL_TASKFORCE_VT = NAVAL_TASKFORCE_VTS[0]
except Exception:
    pass
