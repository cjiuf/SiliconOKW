"""
Offsets recovered by reverse engineering for HOI4 1.19.2 (Linux, native).
All values are ELF virtual addresses relative to the image base; at runtime, base + X.

Verified against the game's own UI; see research/FINDINGS.md.
"""

VERSION = "1.19.4.1"
BUILD_ID = "2a79e51358b8b71c09cfb5ddefa0c03649067a4b"

# ---------------------------------------------------------------- global'ler
G_GAMESTATE        = 0x459E9F0
G_COUNTRYTAG_DB    = 0x458A088
G_MODIFIER_DEFS    = 0x4388AC8   # SModifierDef[] (stride 0x78), named modifier definitions
G_CONSOLE_MANAGER  = 0x4652018   # CConsoleCmdManager*
G_DEFAULT_ALLOC    = 0x43591B8   # IPdxAllocator* (default for CPdxArray)
G_STABILITY_BASE   = 0x43E39F0   # NDefines int64, peacetime stability factor

# ------------------------------------------------------------- CGameState
GS_COUNTRIES       = 0x310   # CPdxArray<CCountry*>
GS_TAG2INDEX       = 0x340   # CPdxArray<int32>  (tag id -> country index)
GS_PAUSE_HOURS     = 0x908   # int32, hours left before pausing (pause_in_hours)
GS_PLAYER_TAG      = 0x518   # int32, the PLAYER's tag index
GS_PLAYER_TAG_ALT  = 0x51C

# --------------------------------------------------------------- CCountry
CO_NAME            = 0x10    # std::string - country name ("German Reich")
CO_ADJECTIVE       = 0x30    # std::string, adjective     ("German")
CO_STATES          = 0x460   # CPdxArray<CState*>   (data@+0x460, size@+0x46c)
CO_MODIFIERS       = 0x5B0   # modifier container (CPdxArray<{int32 id; int64 val}>)
CO_MANPOWER_SUB    = 0x328   # 1.19.4: CCountryManpower INLINE subobject (not a pointer).
                             # Disassembly 2026-11-12: console `manpower` (0x1C4AC20)
                             # does `lea rax,[rax+0x328]` then calls 0x21DA4B0, which
                             # updates +0x0C (pool path, the one the console uses) and
                             # +0x18 (direct add/sub path). Which one the top bar shows
                             # still needs one live comparison.
CO_MANPOWER_CUR    = 0x23F0  # 1.19.4: int64 当前可用人力（与顶栏一致，仅玩家国可靠）
CO_ECONOMY         = 0xF40   # CCountryEconomy*
CO_POLITICS        = 0xF68   # CCountryPolitics*
CO_STABILITY_BASE  = 0x10A8  # int64 fixed(1e5)
CO_WARSUPPORT_BASE = 0x10B0  # int64 fixed(1e5)
CO_NUKES           = 0x12D8  # obj -> +0x18 int64
CO_FUEL            = 0x1520  # obj -> +0x08 int64
CO_TAG_ID          = None    # the tag is not stored directly; resolved via tag2index

# CNationalFocusProgress (CCountry+0x1348), 1.19.4.1
# The completed array is NO LONGER at CCountry+0x1C10 (that is 0x100000001 garbage).
FP_CURRENT         = 0x10    # CNationalFocus*
FP_COMPLETED       = 0x40    # CPdxArray<CNationalFocus*>
FP_ROOT_FOCUSES    = 0x70    # CPdxArray<CNationalFocus*>, tree roots (not "selectable")

# -------------------------------------------------------- CCountryEconomy
EC_MIL_FACTORIES   = 0x2D0
EC_NAVAL_DOCKYARDS = 0x330
EC_CIV_FACTORIES   = 0x390
EC_CIV_RESERVED_A  = 0x3D0
EC_CIV_RESERVED_B  = 0x3D8

# ------------------------------------------------------- CCountryPolitics
PO_POLITICAL_POWER = 0xE0    # int64 fixed(1e5)
PO_RULING_SUB      = 0xD0    # obj -> +0x88 int64 fixed(1e5), ruling party support

FIXED = 100000               # Clausewitz fixed-point scale

# ------------------------------------------------------------- functions
FN_CONSOLE_EXECUTE = 0x344B060  # SResult Execute(SResult*, CConsoleCmdManager*, CPdxArray<string>*)
FN_CONSOLE_DISPATCH= 0x344B080  # same, without bumping the counter
FN_TAG_GET_COUNTRY = 0xD77A90   # CCountry* CCountryTag::GetCountry(CCountryTag*)
FN_GET_POLITICS    = 0xD0CCE0   # CCountryPolitics* CCountry::GetPolitics(CCountry*)
FN_GET_STABILITY   = 0xD36470   # int64 CCountry::GetStability(CCountry*, void* scope=0)
FN_GET_WAR_SUPPORT = 0xD378D0   # int64 CCountry::GetWarSupport(CCountry*, void* scope=0)
FN_GET_MANPOWER    = 0x20B36E0  # int64 GetManpower(CCountry* + 0x328)
FN_GET_COUNTRY_NAME= 0xD78310   # std::string* GetCountryName(CCountryTag*)  == country+0x10
FN_GET_MODIFIER    = 0x2111840  # int64 GetModifier(container, int32 modifierId)
FN_OPERATOR_NEW    = 0x510950  # void* operator new(size_t)
FN_OPERATOR_DELETE = 0x510260  # void  operator delete(void*)

# --------------------------------------------------------------- structures
# CPdxArray<T,int> { T* data; int32 capacity; int32 size; IAllocator* alloc; }  = 0x18
PDXARRAY_DATA = 0x00; PDXARRAY_CAP = 0x08; PDXARRAY_SIZE = 0x0C; PDXARRAY_ALLOC = 0x10
PDXARRAY_SZ   = 0x18

# libstdc++ std::string { char* p; size_t n; char sso[16]; } = 0x20
STDSTRING_SZ = 0x20

# CConsoleCmd entry (array stride 0x1C8):
#   +0x00 flag  +0x08 std::string name  +0x28 alias count  +0x30..+0x40 const char* alias[3]
#   +0x68 function pointer  +0x78 context
CMD_STRIDE      = 0x1C8
CMD_NAME        = 0x08
CMD_ALIAS_COUNT = 0x28
CMD_ALIASES     = 0x30
CMD_FUNC        = 0x68
CMD_CTX         = 0x78

# SResult { bool ok; std::string message; } = 0x28
SRESULT_SZ = 0x28

# CConsoleCmdManager { CPdxArray<CConsoleCmd> cmds; ... std::function _IsRelease@0x18 ... }
MGR_CMDS = 0x00

# SModifierDef (stride 0x78): +0x00 std::string loc_key, +0x6C int32 id
MODDEF_STRIDE = 0x78
MODDEF_NAME   = 0x00
MODDEF_ID     = 0x6C

# CDate: hours = (year*365 + dayOfYear) * 24 + hour; the value is an int32 at CDate+0x08
DAYS_PER_YEAR = 365
MONTH_LENGTHS = [31,28,31,30,31,30,31,31,30,31,30,31]

# Date signature in memory: float(1/24)=0x3D2AAAAB, int32 date at +8, 0 at +12
DATE_SIGNATURE_FLOAT = 0x3D2AAAAB
DATE_SIG_TO_VALUE    = 8

# ---------------------------------------------------------------- platform
# On Windows the MSVC build has a completely different layout and a different
# version number; offsets_win.py carries the 1.19.5.0 table and overrides every
# name above.  Importing this module on Windows therefore yields the Windows
# table, and no caller has to care.
import sys as _sys
if _sys.platform == "win32":                       # pragma: no cover
    from offsets_win import *                      # noqa: F401,F403
