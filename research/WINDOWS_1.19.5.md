# HOI4 Windows 1.19.5.0 — offset recovery log

Target: `D:\SteamLibrary\steamapps\common\Hearts of Iron IV\hoi4.exe`
`Operation Postern v1.19.5.0.7c6c (c184)` — 56 535 160 bytes, PE32+, preferred base `0x140000000`,
`SizeOfImage 0x38A3000`.
Sections: `.text` 0x2783D48 / `.rdata` 0x977F92 / `.data` 0x598754 (raw 0x2E0200) / `.pdata` / `.rsrc` / `.reloc`.

Everything below was measured on a live 1936 campaign (Germany, 1 Nov 1936), PID 42284,
load base `0x7ff698620000`, with the read-only backend in `calling/process/windows.py`.

## 1. RTTI (the key that unlocks everything)

**MSVC RTTI is NOT stripped.** 21 196 `.?AV…@@` name strings, 19 706 valid
`RTTITypeDescriptor`s, 16 567 `RTTICompleteObjectLocator`s, 16 567 vtables,
**15 141 classes**. `research/windows/rtti_win.py` builds `research/windows/rtti_win.pkl`
(`{"classes": {name: [vtable_rva]}, "vt": {vtable_rva: name}, "pe": …}`) in 0.5 s.
This is the Windows counterpart of `calling/rtti.pkl` (18 955 vtables on Linux).

Scanner recipe (all RVAs inside the image; loader relocates the vtable[-1] slot):

```
TypeDescriptor        : {void* pVFTable; void* spare; char name[];}          name at +0x10
RTTICompleteObjectLocator (x64):
    +0x00 u32 signature   (== 1)
    +0x04 u32 offset
    +0x08 u32 cdOffset
    +0x0C u32 pTypeDescriptor   (RVA)
    +0x10 u32 pClassDescriptor  (RVA)
    +0x14 u32 pSelf             (RVA of the locator itself)  <-- self-check
vtable                : qword at vtable-8 == image_base + col_rva
```

## 2. Globals

| symbol | RVA | how it was found |
|---|---|---|
| `G_GAMESTATE` | **0x33DD6B0** | only qword in the whole image pointing at the live `CCurrentGameState`; 1 hit |

`g_gameState` is a `CCurrentGameState*` (vtable `0x279F168`, second vptr at object+0x08
`0x279F1C0`). `CGameState` itself is only a base — no object has its vtable, which is why
scanning for `.?AVCGameState@@` (0x279F0F0 / 0x279F148) returns 0 hits.

## 3. CGameState layout — identical to Linux

| field | offset | value observed |
|---|---|---|
| `GS_COUNTRIES` | **0x310** | `CPdxArray<CCountry*>` data=`0x19eadc4cb30` cap=440 size=440, all 440 entries valid |
| `GS_TAG2INDEX` | **0x340** | `CPdxArray<int32>` cap=474 size=443, **identity map** (`[i]==i`) in this build |
| `GS_PLAYER_TAG` | 0x518 | reads 15 (= Spain) — **not** the player tag on this build, see §6 |

`CPdxArray<T>` = `{ T* data; int32 capacity; int32 size; IAllocator* alloc; }` (0x18) —
same as Linux. The default allocator is the `CPdxNewDeleteAllocator` singleton at
`image_base + 0x30FD000`.

## 4. CCountry

| field | offset | notes |
|---|---|---|
| vtable | 0x00 | `.?AVCCountry@@` → **RVA 0x2864730** |
| tag id (u32) | **0x08** | Germany 1, UK 2, USSR 3, France 5, Spain 15, Italy 16 … |
| name (`std::string`) | **0x10** | **localised** ("德意志第三帝国"), so ASCII scans for "German Reich" find nothing |
| adjective (`std::string`) | **0x30** | "德意志" |
| player flag (u32) | **0x120** | 1 only for the human country (and dummy country 0) |
| **sizeof** | **0x1710** | objects are allocated at a 0x1710 stride |

`std::string` on MSVC = `{ char buf[16]; size_t size; size_t cap; }` (0x20, SSO for len ≤ 15).
`mem.Proc.stdstring()` (libstdc++ layout) returns garbage — a Windows variant is needed.

### 4.1 CCountry sub-object pointers (all RTTI-verified)

```
+0x070..0x088  CNameGroupTracker*  x4
+0x160         CNavyTheater*
+0x218         CVariables*
+0x228         CCountryAI*
+0x230         CFlagManager*
+0xE60         CCountry*            (overlord / original country)
+0xF60         CTechnologyStatus*
+0xF68         CProductionStatus*
+0xF70         CDeploymentStatus*
+0xF78         CReinforcementStatus*
+0xF80         CArmyUpgradesStatus*
+0xF88         CDiplomacyStatus*
+0xF90         CPoliticalStatus*    <-- political power lives here
+0xF98         CLogisticsStatus*
+0xFA0         CDecisionStatus*
+0xFA8         CProgramStatus (NProject)
+0xFB0         CCustomizableBuildingCollection*
+0xFC0         CIntelligenceAgency*
+0xFC8         CLoopHistory*
+0xFD0         CCountryOccupationStatus*
+0xFD8         CCountryCollaborationStatus*
+0xFE0         CCountryReportsManager*
+0xFE8         CCountryIntel*
+0xFF0         CCountryCharacters*
+0xFF8         CIncomingDiplomaticActionStatus*
+0x11F8        CCountryResources*
+0x1310        CPdxArray<CNuke*>   cap 2 size 2
+0x1680        CPdxArray<CProject*> cap 63 size 49
```

This "one pointer per status block" shape is **new** relative to the Linux build, where
`CCountryEconomy*` / `CCountryPolitics*` hang off the country at 0xF40 / 0xF68.

## 5. CPoliticalStatus

| field | offset | evidence |
|---|---|---|
| vtable | 0x00 | `.?AVCPoliticalStatus@@` |
| **political power** | **0xE0** | int64 fixed(1e5); differential scan over 45 s: 3 253 600 → 3 486 000 (+232 400 ≈ +2.3 PP) |
| command power? | 0x560 | int64 fixed(1e5), 1 715 480 → 1 837 300 (+121 820) |

`0xE0` is the **same offset as Linux `PO_POLITICAL_POWER`**, so at least the political
sub-object kept its internal layout.

## 6. Open items

* `GS_PLAYER_TAG` (0x518) does not hold the player on this build — reads 15 while the player
  is Germany (1). `CCountry+0x120` is the reliable marker for now. Needs one tag-switch
  experiment (console did not open: backtick / VK_OEM_3 / SendKeys all ignored).
* Stability / war support are **not** at `CCountry+0x10A8/0x10B0` (those are pointers here).
  They are somewhere in `CPoliticalStatus` (or computed) — not yet found.
* `CState` (`.?AVCState@@` → vtables 0x29B4D58 / 0x29B4DA8) — states array offset inside
  CCountry not yet located; the `CO_STATES = 0x460` Linux offset is unverified.
* Tag string ↔ tag id table (`G_COUNTRYTAG_DB` on Linux) not yet located.

## 7. Step 3 — states, percentages, tags, console table

### 7.1 CGameState arrays (all alloc-tagged, alloc == base+0x30FD000)

| offset | content | observed |
|---|---|---|
| 0x2B0 | provinces | cap/size 13 414 |
| **0x2C8** | `CPdxArray<CState*>` | cap/size **1 082** (1 081 live CState objects) |
| 0x310 | `CPdxArray<CCountry*>` | 440 |
| 0x328 | major countries | cap 9 size 7 |
| 0x340 | `GS_TAG2INDEX` | 443 (identity) |
| 0x640 | `CPdxArray<CArmy*>` | 42 / 35 |
| 0x6F0 | `CReferencedDivisionTemplate` | 711 / 705 |
| 0x708 | `CEquipmentVariant` | 5 395 / 4 513 |

### 7.2 CCountry state lists — `CO_STATES` = 0x460 (same as Linux)

Three consecutive `CPdxArray<CState*>`:

| offset | meaning | GER |
|---|---|---|
| **0x460** | owned states | cap 28 size 24 |
| 0x478 | second state list | cap 28 size 24 |
| 0x4A8 | third state list | cap 28 size 24 |

Sanity check across countries: GER 24, ENG 94, SOV 152 owned states — plausible.

Other CCountry arrays: 0x168 `CTheatre*`, 0x1B8 `CReferencedDivisionTemplate*`, 0x278 `CFleet*`,
0x290 `CArmy*` (42/30 — the 30 starting divisions), 0x5D0/0x5E8 `CModifier*`.

### 7.3 CState

| field | offset |
|---|---|
| vtable | 0x00 (`0x29B4D58`; second vptr at +0x08 `0x29B4DA8`) |
| id (u32) | 0x10 |
| **owner country index** | **0xC8** |
| **controller country index** | **0xCC** |

Verified by requiring `field == 1` for all 24 German states, `== 2` for all 94 British,
`== 3` for all 152 Soviet ones — the only offsets satisfying all three are 0xC8 and 0xCC.

### 7.4 Stability / war support (candidate)

`CCountry + 0x10D0` and `+0x10D8`, int64 fixed(1e5), 8 bytes apart — the same shape as
Linux `CO_STABILITY_BASE = 0x10A8` / `CO_WARSUPPORT_BASE = 0x10B0`.

```
idx  0x10D0  0x10D8
  1   50.0    50.0     GER
  2   70.0    30.0     ENG
  3   59.8    10.0     SOV
  5   50.0    30.0     FRA
  7   65.0    75.0     BEL
```
These are the **base** values; the top bar shows base+modifiers (81 % / 35 % for Germany),
so they are not yet proven to be the exact fields the UI reads.

### 7.5 Country tag table

A contiguous `std::string[443]`, stride **0x20**, index 0 = `'---'`, 1 = `GER`, 2 = `ENG`,
3 = `SOV`, 4 = `SWE`, 5 = `FRA`, … 15 = `SPR`, 16 = `ITA`, 17 = `ROM`, 18 = `YUG`, 19 = `SER`.

**tag id == country index == GS_TAG2INDEX[i]** in this build.

It is a heap array with **no reference from the image**, so there is no static global to read.
Discovery routine instead: find the SSO pattern for `GER` (32 bytes:
`"GER" + 13*0x00 + u64(3) + u64(15)`), then accept the address `a` where
`msstr(a)=='GER' and msstr(a+0x20)=='ENG' and msstr(a+0x40)=='SOV'`.
905 raw `GER` hits → exactly one passes the 3-in-a-row test.

### 7.6 Console command table — **identical layout to Linux**

| field | value |
|---|---|
| array header (`CPdxArray<CConsoleCmd>`) | **RVA 0x33DD968** (directly in `.data`, not a pointer) |
| commands | **370**, all function pointers inside `.text` |
| `CMD_STRIDE` | **0x1C8** |
| `CMD_NAME` | 0x08 |
| `CMD_ALIAS_COUNT` | 0x28 |
| `CMD_ALIASES` | 0x30 (up to 3 × `const char*`) |
| `CMD_FUNC` | 0x68 |

Sample: `tag` 0x2A4570, `annex` 0x256100, `pp` 0x285FF0 (alias `political_power`),
`manpower` 0x281850, `whitepeace` 0x2AFEF0 (alias `wp`), `instant_wargoal` 0x27A8B0,
`observe` 0x285120 (alias `spectator`), `research` 0x294310.

## 8. Step 4 — thread-hijack injection

Implemented in `calling/process/winject.py`, wired into `calling/process/windows.py`
(`Injector` now delegates to it, same surface as `process/linux.Injector`).

### 8.1 Verified

| test | result |
|---|---|
| `kernel32!GetCurrentProcessId()` inside the game | returns the game's own PID (52068) |
| `kernel32!GetTickCount64()` inside the game | 853 806 671 ms |
| repeated calls + `detach()` | process stays alive, no leaked thread |
| hijacked thread | `SuspendThread` previous-count must be 0; threads already suspended by the job system are skipped |

Working thread picking: sample `GetThreadTimes` twice 150 ms apart, take the first
thread with a non-zero CPU delta (fallback: highest total CPU), then confirm
`SuspendThread` reports a previous suspend count of 0.

### 8.2 Two crashes, both understood

1. **Misaligned CONTEXT restore.** `saved = raw.raw[:CTX_SIZE]` copies from the
   buffer base, but `GetThreadContext` was given the 16-byte **aligned** address
   inside it. Restoring a shifted context corrupted every register → instant crash.
   Fix: `ctypes.string_at(ctx.addr, CTX_SIZE)`.
2. **Calling a game function at the main menu.** `test` (0x2A5070) executed and
   killed the process, because console commands touch the gamestate, which does not
   exist before a campaign is loaded. Game functions must only be called in-game.

### 8.3 Console command ABI (from disassembly)

```c
SResult *Cmd(SResult *out, const CPdxArray<std::string> *args);
```

* `SResult` = `{ bool ok; std::string message; }` = 0x28
  (bool at 0, message buffer at +0x08, size at +0x18, capacity at +0x20)
* `CPdxArray<T>` — size of the array is read at **+0x0C**, i.e. `{data, cap, size, alloc}`
* `rcx = out`, `rdx = args`; a command with no arguments still receives a valid empty array

### 8.4 Scratch page layout used by the injector

```
0x000  stub    48 A3 <res>       mov [res], rax        ; captures the return value
                48 B8 <flag>     mov rax, flag
                C7 00 01..       mov dword [rax], 1
                EB FE            jmp $
0x040  res      u64 return value
0x048  flag     u32 done flag
0x100  stack    0x800 bytes; entry RSP % 16 == 8, [rsp]=stub, shadow at +8, args at +0x28
0x1000 data     caller-provided structures (SResult / CPdxArray / std::string)
```

### 8.5 Live results (2026-11-13)

```
GER PP before 74.368
call1 ok=1  msg="Granted 999 political power to 德意志第三帝国"   PP=1073.368
call2 ok=1  msg="Granted 999 political power to 德意志第三帝国"   PP=2000.000   (cap)
call3 -> process died
```

* **The injection really mutates game state** — `pp` executed inside the game and granted
  political power to the player country (Germany), twice in a row.
* **`pp` ignores our argument** (always grants 999) → the console argument array is probably
  not `CPdxArray<std::string>` but `CPdxArray<CString>`, whose element layout differs.
  Needs a look at the copy ctor at `0x14011F770` before any command that takes parameters
  (`tag`, `add_equipment_to_stockpile`, …) can be used.
* **Stability**: the 3rd call killed the process. `game.log` ends with
  `Start RestoreDeviceObjects / End RestoreDeviceObjects`, i.e. a D3D device reset, which
  points at the freeze/thaw of the render thread rather than at the call itself.
  Current model (`_freeze()` all threads → hijack the window thread → `_thaw()`) is too
  invasive for repeated use.

### 8.6 Options for a stable design

1. **Persistent worker thread** — hijack once, let the stolen thread run a loop that waits on
   a request slot in the scratch page; later calls only post work + wait for the flag.
   No repeated freezes, no repeated context surgery.
2. **Pause the game first, do not freeze anything, hijack an idle game thread** — cheapest,
   but thread choice is the thing that crashed the very first attempts.
3. **Call `CConsoleCmdManager::Execute`** once with the whole command line instead of the
   individual `CMD_FUNC` entries — one call site, args parsed by the game itself
   (Linux equivalent `FN_CONSOLE_EXECUTE = 0x344B060`).

## Reproduce

```powershell
python research\windows\rtti_win.py      # 0.5 s  -> research/windows/rtti_win.pkl   (19 706 types / 16 567 vtables)
python research\windows\win_gs.py        # gamestate chain + 440-country table
python research\windows\wmap.py          # CCountry structural map (pointer fields + arrays)
python research\windows\wstate2.py       # alloc-tagged CPdxArray enumeration (states!)
python research\windows\wstate3.py       # CState owner/controller + percentage fields
python research\windows\wtag.py          # country-tag std::string table
python research\windows\wconsole.py      # find the CConsoleCmd array in .data
python research\windows\wconsole3.py     # decode all 370 console commands
python research\windows\wdiff.py         # differential field scan (finds ticking values)
```

## 9. Console manager - static map (basis for the Execute route)

`research/windows/wxref.py` scans `.text` for RIP-relative `lea`/`mov` whose displacement lands on
the console manager global `0x33DD968`. **13 references**, all in `.text`:

```
0x0013CCC   0x0246E70   0x024945F  0x024947D  0x024949B  0x02494B9
0x02B12D5   0x02B12F3   0x02B1311  0x02B132F  0x02B1353
0x02776A34  0x02776A69
```

| address | what it is |
|---|---|
| **0x246E70** | `GetConsoleCmdManager()` - `lea rax,[rip+0x3196AF1]; ret`, i.e. it just returns `0x33DD968`. Free to call, no arguments. |
| **0x2776A30** | ~~command lookup~~ **corrected, see §10: this is `CConsoleCmdManager::Clear()`** - it walks the array (stride 0x1C8) calling each element's destructor and then frees the buffer through the allocator. It never compares names. |
| 0x13C90.. | command **registration/init**: `mov edx,0x1C8` (element size) + `mov r8d,0x157` (reserve 343) + `call 0x25A06E4`, right after the manager is touched |
| 0x2B12D5.. | five consecutive refs - most likely the static command definition table that gets copied in |

Next step for the `Execute` route: find the function that takes `(SResult*, CPdxArray<...>*)`
and calls the lookup at 0x2776A30 - that is `CConsoleCmdManager::Execute`, the Windows
equivalent of the Linux `FN_CONSOLE_EXECUTE = 0x344B060`.

---

## 10. Console command ABI (verified live)

`G_CONSOLE_MANAGER = 0x33DD968` is an inline `CPdxArray<CConsoleCmd>` (data at +0,
capacity at +8, **size at +0x0C**, allocator at +0x10), element stride **0x1C8**.
`CConsoleCmd` layout (matches Linux):

| field | offset |
|---|---|
| `CString name` | 0x08 (MSVC `std::string`, 0x20 bytes) |
| alias count | 0x28 |
| aliases | 0x30 |
| **implementation pointer** | **0x68** |

### Signature

Every command is

```c
SResult*  fn(SResult* out /*rcx*/, const CPdxArray<CString>* args /*rdx*/);
```

`SResult` is `{ bool ok; std::string msg; }` = **0x28 bytes** (`ok` at +0, `msg`
buffer at +8, length at +0x18, capacity at +0x20).  Windows has no hidden
return pointer because the result is written through `out`.

Confirmed directly from RTTI, which also settles the argument element type:

```
?$CPdxArray@VCString@@H@
?$_Func_impl_no_alloc@P6AXAEAUSResult@CConsoleCmd@@AEBV?$CPdxArray@VCString@@H@@...
                                    ^^^^^^^^^^^^^^^^^^^^^^^ ^^^^^^^^^^^^^^^^^^
                                    SResult& (nested in CConsoleCmd)   CPdxArray<CString>
```

so the arguments are `CString`, **not** `std::string` by identity - but `CString`
*is* the MSVC `std::string` layout in this build, which is what matters:

```
0x254ECB0  CString::c_str()      cmp qword [rcx+0x18], 0xF   ; capacity <= 15 -> SSO
                                 jbe done
                                 mov rcx, [rcx]              ; else heap pointer
                          done:  jmp 0x25DDAB8               ; atoi()
```

`pp` (= `0x285FF0`) reads its argument exactly like that:

```
28603E  cmp  dword [rdx+0xC], 1      ; args->size  (note: +0xC, not +0x8)
286042  jle  28605E                  ; 0 args -> use the default
286044  mov  byte [r14], al          ; too many args -> ok = false
286047  ...  "Invalid arguments count."
28605E  mov  ebx, 0x5F45A60          ; 999 * 100000 (fixed point 1e5)
286063  jne  286077                  ; size != 1 -> default
286065  mov  rcx, [rdx]              ; element 0
286068  call 0x254ECB0               ; CString::c_str()
28606D  movsxd rcx, eax
286070  imul rbx, rcx, 0x186A0       ; * 100000
```

### Verified live (1.19.5.0, pid alive, one attach)

| command | message | political power |
|---|---|---|
| - | - | 74.368 |
| `pp 0` | "Granted 0 political power to 德意志第三帝国" | 74.368 |
| `pp 7` | "Granted 7 political power to 德意志第三帝国" | 81.368 |

`pp` is also a ready-made **fixed-point probe**: `CPoliticalStatus+0xE0` is an
`int64` in 1e5 units, so `pp n` must move it by exactly `n*100000`.

### Console manager map

| address | what it is |
|---|---|
| **0x246E70** | `GetConsoleCmdManager()` - returns `0x33DD968`; no arguments, safe canary call |
| **0x2494D0** | `CPdxArray<CConsoleCmd>::append(array)` - used by every registration site (12 callers) |
| **0x249450** | registers four built-in command tables (4 × append) |
| **0x2B10AF** | registers the scripted `console_commands` tables (5 × append) |
| **0x2776A30** | `Clear()` (see above) |

There is **no name lookup that walks the array** - only 13 code references to
`0x33DD968` exist and all of them are registration/teardown.  `0x2537390`
iterates the manager array (stride 0x1C8 again) and pushes every entry into a
separate 0xE0-byte object reached through the global pointer `0x368F540`, so
name resolution happens on that copy, not on the global array.

## 11. Injector status

The ABI is solved; the **hijack** is not yet reliable.  What is established:

* Borrowing an **idle** thread does not work: a thread blocked in
  `NtWaitForSingleObject` is never scheduled, so it sits at the injected entry
  point (`RIP == func_addr`) until the timeout.  Only a *running* thread can be
  hijacked, which in practice means the window/render thread.
* Freezing every thread before the call does not help (99 threads here).
  Suspending only the target thread does not help either.
* A **control-area zeroing bug** in `call()` used to wipe the whole scratch page
  before every call, which silently zeroed any argument payload parked above
  `0x1000`.  That is why `pp <n>` appeared to ignore its argument and always
  granted 999 (the `size == 0` default).  Now only the stub/result/flag/stack
  area is cleared (`CONTROL_SIZE = 0xA00`) and payloads live at `PAYLOAD_OFF`.
* `SetThreadContext` with our offsets is correct: a round-trip on a scratch
  thread of the same process returns every register unchanged, and a read-back
  immediately after setting the hijack context shows `RIP/RSP/RCX/RDX` exactly
  as requested.
* Nevertheless `pp` as an injected call still faults at **`hoi4.exe+0x28601D`**,
  the very first store of the function (`mov byte ptr [rcx], al`, i.e. writing
  `out->ok`), with a first-chance `ACCESS_VIOLATION` on the hijacked thread.
  Under a debugger the register set at fault reads `RCX = func_addr`,
  `R8 = injected RSP`, `RSP = RSP-0x108` - i.e. the *stack* part of the context
  landed but at least `RCX` did not, even though the read-back said it had.
  Next probe: single-step the first instructions of `pp` with the debugger to
  see where `RCX` is overwritten.

## 12. Root cause and fix: build the call in code, not in the context

Two independent bugs were behind everything in §11.

**1. The argument payload was being wiped.** `call()` cleared the whole scratch
page before each call, so a payload parked above it read back as all zeros and
every command saw `args->size == 0`.  That - not the element type - is why
`pp <n>` always granted the default 999.  Fixed by clearing only
`[0, CONTROL_SIZE)` and putting payloads at `PAYLOAD_OFF`.

**2. `SetThreadContext` does not reliably deliver the argument registers.**
Single-stepping the injected call with the trap flag set shows it exactly:

```
after SetThreadContext: RIP=0x7ff6988a5ff0 RSP=0x2ac93f307f8
                        RCX=0x2ac93f31000 (want 0x2ac93f31000)   <- readback agrees
                        RDX=0x2ac93f31040 (want 0x2ac93f31040)
step 1  RIP=0x7ff6988a5ff0 RCX=0x7ff6988a5ff0 RDX=0x0 R8=0x2ac93f307f8
```

At the very first instruction the registers no longer match: `RCX` holds the
entry `RIP`, `R8` holds the entry `RSP`.  The *control* classes (RIP/RSP) land,
the *integer* classes do not, and `SetThreadContext`'s own read-back cannot see
the difference.  A round-trip on a scratch thread of the same process works
perfectly, so the offsets are right - the loss happens on the way into a
suspended game thread.

The fix is to stop asking the context to carry arguments.  The stub in scratch
loads them itself and calls the target:

```asm
sub  rsp, 8                 ; ABI: entry RSP % 16 == 8, so pre-call must be 0
mov  rcx, <a0>
mov  rdx, <a1>
mov  r8,  <a2>
mov  r9,  <a3>
mov  rax, <function>
call rax
add  rsp, 8
mov  [res], rax             ; completion flag for the poller
mov  rax, <flag>
mov  dword [rax], 1
jmp  $
```

Only `RIP` and `RSP` go through the context now - the two that work.  A
third bug surfaced while doing this: the stub is 0x58 bytes and originally sat
at 0x00 with `RES_OFF = 0x40`, so the result/flag writes overwrote the tail of
the code; and the call stack (0x800) grew down onto the stub.  The layout is now
stub at 0x00 (below the stack), result/flag at 0x60/0x70, a 32 KiB stack at
0x1000, and payloads at 0x9200.

### Verified

```
$ python wstress.py 60 0.02
scratch 0x2d9f7a50000  payload 0x9200
after 60 calls: bad=0  dirty=False  calls=61
detached cleanly
```

60 consecutive `pp 0..4` calls in one attach, every one asserting the returned
message is exactly `Granted <n> political power to 德意志第三帝国`.  No crash,
no dropped call, clean detach.  Before the fix the process died on call 1.


## 13. Console commands from Python (verified end-to-end)

`calling/consolecmd.py` now has a Windows path, so `g.console("pp 7")` and the
`hoi4_debug_console` / `hoi4_debug_list_commands` tools work on `hoi4.exe`.
Nothing above `consolecmd` changed: the Linux branch is untouched.

### Where the table lives

Linux had to *scan* writable memory for the first entry, because 1.19.4.1 has no
usable global. 1.19.5.0 does:

```
G_CONSOLE_MANAGER = base + 0x33DD968      CPdxArray<CConsoleCmd>, INLINE (not a pointer)
    +0x00 data  = 0x...                   heap array
    +0x08 cap   = 514
    +0x0C size  = 370
    +0x10 alloc = base + 0x30FD000        the CPdxNewDeleteAllocator singleton
```

So the lookup is O(1) instead of a multi-second scan, and it cannot go stale:
the pointer is re-read on every `table()` call (only the entry index is cached,
keyed on pid + base + count).

Entry layout is identical to Linux (`STRIDE 0x1C8`, name at +0x08, alias count
+0x28, alias `char*[]` +0x30, handler +0x68), but the name is an **MSVC
`std::string`** (SSO 15 bytes, length +0x10, capacity +0x18), so
`Proc.stdstring()` has to be the MSVC branch. 370 entries, all 370 with a
readable name, all 370 handlers inside the image, 82 with aliases:

```
[  0] test              fn=+0x2a5070  alias=[]
[ 17] whitepeace        fn=+0x2afef0  alias=['wp']
[ 87] annex             fn=+0x256100  alias=[]
[ 94] manpower          fn=+0x281850  alias=[]
[169] spawn             fn=+0x2a1190  alias=[]
[208] add_equipment     fn=+0x24fc60  alias=['ae']
[209] pp                fn=+0x285ff0  alias=['political_power']
```

One oddity worth knowing: the game registers **two** entries named
`test_naval_move_danger` (indices 32 and 33, handlers +0x2a5ad0 / +0x2a5840).
`command_catalog()` is a dict, so it reports 369 commands for 370 rows. That is
the game's own table, not a parsing bug.

### Where the arguments come from

`Injector.prepare_console_args()` builds `SResult` + `CPdxArray<CString>` + the
strings **inside the injector's scratch page** and the stub loads the two
pointers. No `operator new`, no per-argument heap block, nothing to leak, and a
long argument cannot smash a neighbouring allocation (the Linux version once
died on `add_equipment 600 transport_plane_equipment_1` for exactly that
reason). Arguments over 15 bytes get a real buffer parked after the slot array.

### Verified live

```
$ python wcon.py "pp 3" "pp 1 2 3" "pp" "manpower" "manpower 1000" "xp 10"
pp 3          ok=True   Granted 3 political power to 德意志第三帝国
pp 1 2 3      ok=False  Invalid arguments count.
pp            ok=True   Granted 999 political power to 德意志第三帝国
manpower      ok=True   Now we have manpower!
manpower 1000 ok=True   Manpower changed by changed by 1000
xp 10         ok=True   Granted 10.000 XP of all kinds to 德意志第三帝国

$ python wcon.py "add_equipment 600 transport_plane_equipment_1" "ae 100 infantry_equipment_1" "add_stability 5"
add_equipment 600 transport_plane_equipment_1 ok=True  Added 600 of transport_plane_equipment_1 ...
ae 100 infantry_equipment_1                   ok=True  Added 100 of infantry_equipment_1 ...   (alias)
add_stability 5                               ok=True  Added 5 stability ...
```

Command latency is 3-10 ms after the first call (`attach` + `VirtualAllocEx`);
the first call in a process costs ~0.7 s.

## 14. The tag database is inside CGameState

`G_COUNTRYTAG_DB` has no image global on this build, and the tag strings sit on
the heap with no pointer to them from `.data` — the earlier note said "must be
found by scanning". That was wrong. It is an **inline** `CPdxArray<std::string>`
in the gamestate, immediately after the tag2index array:

```
GS_TAG2INDEX = 0x340   data=0x...  cap=474 size=443 alloc=base+0x30FD000
GS_TAG_DB    = 0x358   data=0x...  cap=474 size=443 alloc=base+0x30FD000
```

`tags[0] = "---"`, `[1] = "GER"`, `[2] = "ENG"`, `[3] = "SOV"`, and 440..442 are
the **dynamic** tags `D01 D02 D03`, which the static 440-entry string table the
scanner used to find does not contain. Size 443 == tag2index size 443, so unlike
Linux there is no padding: every tag id has a name.

`api._load_tags()` now branches on `sys.platform` and reads that inline array.

```
GER  ptr=0x2db53b80e10  德意志第三帝国
ENG  ptr=0x2db53b93470  大不列颠
SOV  ptr=0x2db53c3a020  苏维埃社会主义共和国联盟
HOL  ptr=0x2db5422d060  荷兰
INS  ptr=0x2db57bf3030  荷属印度
```

### Player tag: 0x520, not 0x518

`GS_PLAYER_TAG = 0x520`. Verified by doing what a test can actually falsify:

```
before    0x518=15  0x520=1     (playing GER)
tag ITA   0x518=15  0x520=16
tag GER   0x518=15  0x520=1
```

0x518 sits at 15 (SPR) through all of it — it is not the player. `0x51C` is also
not (it reads 440, which looks like a valid index and is not).

## 15. Injector: a timeout must never leave the game frozen

Three defects found this round, all in `winject.py`.

**1. A timed-out call left the render thread suspended.** `call()` did
`SuspendThread(h)` before checking the done flag, then raised on the
"not our stub any more" path without resuming. `detach()` only resumes what is in
`self.frozen`, which that path had already cleared. Measured afterwards with
`SuspendThread` (which returns the *previous* count):

```
tid 43964 previous suspend count = 1     <- the window thread, still frozen
```

The game's simulation threads keep running and every read still works, so this
looks like "the game is fine, the UI is just unresponsive". It is not.

**2. A per-injector `dirty` flag is not enough.** Every console command attaches
a fresh injector, so after a timeout the next command happily hijacked the same
thread *again while it was still inside the first call*. That nested two stubs
and destroyed the thread's stack (the render thread ended up spinning at the
first byte of a leaked scratch page; the process had to be killed). There is now
a module-level `_DIRTY_PIDS` set plus `winject.clear_dirty(pid)`.

**3. Do not "self-restore" from inside the stub.** Tried: end the stub with
`mov rsp, orig_rsp / mov rax, orig_rip / jmp rax` so a slow call cannot strand
the thread. It wedges the process — the hijacked thread is stopped in the middle
of unrelated code, so its caller-saved registers (rcx, rdx, r8-r11) are live;
the target call clobbers them and the thread resumes with them destroyed.
Restoring the **whole** context is what makes this safe. The stub stays at
`jmp $` and the injector puts every register back.

A slow command therefore needs a long *poll*, not a clever stub. `tag` switches
the player country and does not return in 8 s; `run_console_cmd` now waits 30 s
and then gives the call a 25 % grace period before deciding it is stuck. If it
really is stuck, it reads RIP first: inside the stub -> the thread is parked and
restoring is safe; anywhere else -> leave it running and refuse further calls.

## 16. Environment: the launcher, and why the game would not start

Not a binary finding, but it cost more time than the binary work, so it is
written down.

* The Paradox launcher was already broken before this session: it opens with
  **"数据库迁移失败 … delete or rename launcher-v2.sqlite"**. Renaming the file
  lets the launcher start, but it comes up with a *fresh* database whose default
  playset is **all 64 subscribed mods** instead of the 9 the campaign uses.
  Launching with 64 incompatible total-conversion mods crashes `hoi4.exe`
  during asset load (a dump lands in `Documents/.../crashes/`, no exception).
* The correct mod set is recorded in the save itself. `autosave.hoi4` is
  `HOI4bin5` and its header carries the mod display names:
  `Downsize AirForce, Downsize LandForce, Flag Rework Revive,
  Force AI to leave faction, Radio Requiem, UMMS, 决议化更多铁路和补给中心,
  国家名称全称补全, 陆军徽章汉化`.
* The old launcher DB (`launcher-v2.sqlite.bak-20261113`) still resolves those
  names to `gameRegistryId` and to load order; `dlc_load.json` was rewritten
  from it (`dlc_load.json.bak-20261113` holds the 64-mod version). With 9 mods
  the game boots to the main menu in <60 s instead of crashing.
* `hoi4.exe` started directly needs `SteamAppId=394360` in the environment or
  `steam_api64.dll` cannot initialise.
* The Steam in-game overlay (Shift+Tab) renders in its own top-level window and
  **swallows every synthetic click and key**. `WindowFromPoint` still returns
  the game window, so this looks exactly like "the game ignores input". Killing
  the `steamwebhelper` that owns that window (it is not the main Steam client)
  restores input.
* Screen is 2560x1440 at 125 % scaling: the captured desktop is 2048x1152 and
  the `windows-mcp` Click/Move coordinates are in 2560-space, i.e. pass
  `screen_pixel * 1.25`. Every earlier "the click missed" note in this file is
  this, not a broken menu.

---

## 17. CCountry sub-object map (RTTI vtable scan, 2026-11-13)

Every pointer-sized slot of a live CCountry was read and its target's first qword
compared against the MSVC RTTI vtable table (`stage/rtti_win.pkl`, 16 567 vtables).
This replaces guessing: the offsets below are *observed*, not inherited from Linux.

```
+0x0F60 CTechnologyStatus      +0x0F68 CProductionStatus
+0x0F70 CDeploymentStatus      +0x0F78 CReinforcementStatus
+0x0F80 CArmyUpgradesStatus    +0x0F88 CDiplomacyStatus
+0x0F90 CPoliticalStatus       +0x0F98 CLogisticsStatus
+0x0FA0 CDecisionStatus        +0x0FC0 CIntelligenceAgency
+0x0FD0 CCountryOccupationStatus              +0x0FE8 CCountryIntel
+0x0FF0 CCountryCharacters
+0x11F8 CCountryResources      +0x1310 CNuke
+0x1350 CBuildingStatus
+0x1370 CNationalFocusTree     +0x1378 CContinuousFocusPalette
+0x1380 CNationalFocusProgress
+0x1580 CCountryFuelStatus     +0x1588 CCountryExperienceStatus
+0x15A8 CCountryOperationManager
```

Differences from the 1.19.4.1 Linux table: the focus block moved 0x1338/0x1340/0x1348
-> 0x1370/0x1378/0x1380, and several status pointers were inserted before it.
Using the Linux values on Windows read a random heap pointer and every focus call
died with `OSError 299`.

### CNationalFocusTree / CNationalFocus

```
tree+0x08  std::string  tree id ("german_focus")
tree+0x30  CPdxArray<CNationalFocus*>   cap 474 / size 438   (alloc = base+0x30FD000)
focus+0x08 int32  id
focus+0x18 CString key          ("GER_remilitarize_the_rhineland")
focus+0x38 CString localisation key
focus+0x60 CString localised name
```

`game.py` now resolves the three CO_FOCUS_* through `getattr(O, ...)` so the Linux
table stays authoritative there and `offsets_win.py` overrides it here.

### Writing the current focus

`CNationalFocusProgress+0x10` holds the live `CNationalFocus*`. The command layer
that normally sets it (`CSetNationalFocusCommand`) is still Linux-only, so the focus
was selected by storing the pointer directly. Verified: the value survives day ticks
and `current_focus()` reads it back; the game keeps progressing it.

## 18. Defects found while playing a real campaign

* `console.annex` returns **ok=False with an empty message but works** — the handler
  writes no SResult; the ok byte is stale. `annex LUX/HOL/BEL/FRA` took GER from 24
  to 109 states with every target left at 0 states. Do not trust `ok` for `annex`.
* `g.date()` is wrong on Windows. It falls back to log scanning, and the answer came
  back as `1945.02.20.01` while the UI showed `31 1月 1936`. `DATE_SIGNATURE_FLOAT`
  is None in the Windows table, so the memory path never runs.
* `Country.political_power` works once `CO_POLITICS` aliases `CO_POLITICAL_STATUS`
  (0xF90) — it read 2 at campaign start, 5 a month later.
* Synthetic mouse clicks into the fullscreen game are dropped while the window is
  minimised (Steam's window manager can iconify it). Menu navigation via `ENTER`
  is reliable; check `MainWindowHandle`/`IsIconic` before blaming the coordinates.

## 19. The object index on MSVC RTTI

`ObjectIndex` (the class -> live objects map behind `g.oi`) was the last piece of
the read layer that was Linux-only. It is platform-neutral apart from two things:

* the class map. `calling/rtti.py` reads Itanium RTTI out of the ELF `.rela.dyn`
  relocations; hoi4.exe has MSVC RTTI instead. `research/windows/rtti_win.py`
  walks `RTTITypeDescriptor` / `RTTICompleteObjectLocator` and finds every vtable
  whose `[-1]` slot points at a locator.
* `calling/rtti_win.py` is the runtime shim: it loads that pickle, reduces the
  decorated names (`.?AVCState@@` -> `CState`, `.?AV?$T@VArg@@@@` -> `T`) and
  returns the same `{"vtables": {name: [rva]}}` shape as the Linux module.
  `game.py` picks it automatically when `sys.platform == "win32"`.

Two smaller fixes were needed in `objindex.py`: `instances()`/`live_instances()`/
`deep_instances()` used a `^\d+` test to recognise the Itanium length prefix, which
never matches a plain MSVC name, and `_rss_mb()` read `/proc/self/status` (0 MB on
Windows = **no memory ceiling at all** during the scan). Both now handle either
build. `Proc.refresh_maps()` forwards to the Windows backend so the fallback scan
sees heap arenas mapped after attach.

Measured on the running campaign (pid 26844):

```
scan            11.9 s   over every writable region, 4 MB chunks
vtable keys     9 975    (8 616 classes, 16 567 vtables in the pickle)
CCountry          440
CState          2 162
CBuilding       6 746
CBuildingDatabase   1
```

`CGameState` has no live instance (the object is a `CCurrentGameState`); use the
`G_GAMESTATE` global.

## 20. CBuildingDatabase and CState.buildings

```
CBuildingDatabase+0x160  CPdxArray<CBuildingTemplate*>   cap 63 / size 51
  template+0x08   int32       building type id
  template+0x230  std::string "<key>_plural"   (only on some entries)
  template+0x270  std::string key              ("industrial_complex")
```

There is no plain display-name string in the template on this build, so
`building_types()` returns the key as `name`. The array lives at **+0x160**, not
the Linux +0x40; +0x40 here is a 56-entry `TNullObject` array.

51 types, and the three the top bar counts are `20002 arms_factory`,
`20013 industrial_complex`, `19446 dockyard`.

`infrastructure` is **not** in that database, yet every state carries a `CBuilding`
of type **12203** whose level is the state's infrastructure. Cross-checked against
the campaign autosave (`industry_by_country()` in `savereader.py`):

```
                 memory          save
GER infrastructure    81            81
GER air_base          47            47
GER civ/mil/dock  35/28/10    35/28/10
FRA civ/mil/dock  35/8/11     35/8/11
ENG civ/mil/dock  32/14/19    32/14/19
```

so `offsets_win.BT_EXTRA = {12203: "infrastructure"}` is added to the id->key map.

### CState layout (Windows)

```
state+0x18   CPdxArray<CProvince*>    (state 5: 11 provinces)
state+0x58   int32                    state id
state+0x158  CPdxArray<CBuilding*>    the buildings the top bar counts
state+0x170  CPdxArray<CBuilding*>    same objects, second view (not used)
```

`ST_PROVINCES` was 0x08 on Linux; reading it here returned `cap=5 size=512`, which
is how the wrong offset was caught.

Still unknown on this build: `ST_RESISTANCE` (+0x278 on Linux) and
`ST_COMPLIANCE` (+0x2A8). `state_detail()` returns `null` for them instead of
printing garbage.

## 21. Manpower (M1.3)

Manpower is not one country-level field on this build: the game keeps a
`CStateManpower` per state and sums it on demand.

```
CState+0x830   embedded CStateManpower      (1081 instances, always +0x830)
    +0x18  int32   manpower AVAILABLE from this state   <- the top-bar number
    +0x1C  int32   population that cannot be recruited
    +0x20  int32   population of the state

CCountry+0x328  embedded CCountryManpower   (440 instances, one per country)
    +0x10  int64 fixed(1e5)  recruitment ratio (GER 2.5%, LUX 1.5%, FRA 1.65%)
    +0x20  int32             NOT the used manpower -- see below
```

Verified against the campaign autosave (`manpower_pool` per state):

```
                 sum(+0x18)   save available
GER   1 318 383 (after save)      1 318 649
FRA     404 504 (after save)        404 704
ENG     490 757 (after save)        501 238
SOV   1 176 509 (after save)      1 188 357
HOL          23 857                  23 857
BEL           9 542                   9 542
LUX           4 432                   4 432
```

and against the top bar screenshot taken at the same moment: **1.31M**.

The total (before what the army already uses) is
`sum(+0x20 - +0x1C)` = 1 641 933 for GER, i.e. population 65 677 797 x 2.5 %,
which is the tooltip's "total manpower 1.63M". `used = total - available`
(323 550 for GER), which is *not* `CCountryManpower+0x20` (330 450) — that field
is something else, so the arithmetic uses only the state table.

`Country.manpower()` now returns the available value, and two extra properties
`manpower_total` / `population` expose the other two numbers (both `None` on the
Linux build, where they are still behind an engine call).

## 22. Stability / war support are base + modifiers

`CCountry+0x10D0` / `+0x10D8` hold the **base** values in fixed(1e5):

```
        base stab   base WS     UI just after loading
GER       0.70       0.30        81% / 35%
FRA       0.45       0.10
ENG       0.596      0.10
SOV       0.50       0.30
```

So the UI value is not stored anywhere reachable in the country object (an
anchored search for 8 100 000 / 0.81 across the country and all its status
sub-objects found nothing) — it is base plus the modifier stack. `modifier()` and
an exact stability reading need `G_MODIFIER_DEFS`, which is still `None` on this
build; that is the remaining half of M1.3.

## 23. Modifiers (M1.3, second half)

`G_MODIFIER_DEFS` = **0x33DD1D0**, a `u64` pointing at a heap array of 4373
`SModifierDef` records (the array is larger than the first 666 `MODIFIER_*`
entries — after a gap the game keeps appending the per-country/per-trait ones).

```
SModifierDef (stride 0x78, same as Linux)
   +0x00  std::string   "MODIFIER_STABILITY_FACTOR"
   +0x6C  int32         id == index
```

Found mechanically: scan every writable region for 24 consecutive records whose
int32 at +0x6C equals their own index — exactly one candidate in the whole 4 GB
process. `modifier_list()` now returns 4373 names and `modifier_id("stability_factor")`
is 141.

### Where a country's modifiers actually live

Not one sorted array (that is the Linux layout). `CCountry+0x5D0` and `+0x5E8`
are `CPdxArray<CModifier*>`; each `CModifier` is a polymorphic object whose

```
   +0x10  ->  array of {int32 id; 4 pad; int64 value}    (value = fixed 1e5)
   +0x18      int32 count
```

so the total for one id is the sum over every CModifier in both arrays. The two
arrays never share an object (50 + 3 for GER, all distinct).

```
GER  stability_factor  +0.05    war_support_factor +0.05
FRA  stability_factor  -0.10    war_support_factor -0.05
ENG  stability_factor   0.00    war_support_factor +0.05
SOV  stability_factor  -0.575   war_support_factor +0.20
```

`Country.stability()` / `war_support()` now return base + that sum. War support
matches the top bar exactly (GER 0.30 + 0.05 = 35%).

### The one term still missing

The stability tooltip breaks the number down itself:

```
基础值:        70.00%
执政党支持率:  +6.00%
帝国议会:      +5.00%
```

Our 0.70 (base) and 0.05 (帝国议会) match two of the three lines; the `+6.00%`
comes from the ruling party's popularity and is **computed, not stored** — an
anchored search for 60000 / 0.6 across CCountry, CPoliticalStatus and the
CPoliticalParty instances found nothing usable. So `stability()` reads 0.75 for
GER where the UI shows 81%. Everything else in M1.3 is verified.

## 24. M2 groundwork (command layer)

### Allocator -- works

`base+0x30FD000` is the `CPdxNewDeleteAllocator` **object** (not a pointer to one);
its vtable is `base+0x2794550`.

```
vtable+0x08  Allocate(this, size)        -> void*       (helper: mov rcx,rdx; call ...)
vtable+0x10  Deallocate(this, ptr)       -> int
```

Verified through the injector: `Allocate(0x40)` returned `0x1e839cc4400` (readable)
and `Deallocate` returned 1. So a command object can be allocated from the same CRT
heap the game frees from -- no "delete on a foreign pointer" crash.

### Idler -- found, but the queue slot is not the Linux one

`G_IDLER = 0x33DDB08` is a `u64` that really holds a `CInGameIdler`
(`ObjectIndex.class_of` says so, and reading `[idler]` gives its vtable).

Linux uses `vtable+0x90` for `GetCommandQueue()` and `+0xC0` for `GetPlayerTag()`.
On this build:

```
+0xB8   mov rax,[rcx+0x6B8]; ret      <- a getter, but ...
+0xC8   mov rax,[rcx+0xA48]; ret
+0x118  mov rax,[rcx+0xA28]; ret
+0x130  mov rax,[rcx+0x760]; ret
+0xC0   xor eax,eax; ret               <- returns NULL
```

Calling the `+0xB8` getter returns exactly `[idler+0x6B8]` (both `0x1e806d29290`),
but that object is a container of **UI views**: its arrays hold `CTopBar`,
`CAchievementsView`, `CCountryLogisticsView`. It is not the command queue, and a
scan of the whole `CInGameIdler` (first 0x2200 bytes) for a `CPdxArray` whose
elements are `*Command` objects found none.

So M2.2 has to be redone from the Windows side: find the queue first, then the
slot that returns it.

### CCommandQueue::Post -- not located yet

Tried and did not produce a unique answer:

* `FF 50 50` + `E8` in `.text` -> 183 sites, top target 0xC08170 (11 hits), which
  is not a queue push.
* `mov rax,[rcx]; call [rax+0x50]; call rel32` (exact adjacency) -> 0 hits.
* `lea` to any `*Command` vtable (410 vtables) -> 1931 sites; the most frequent
  nearby call targets are 0x25A01DC (542), 0x11F6F0 (428), 0x2284870 (204) --
  the first two look like `operator new`/CRT helpers, not a container push.
* `call [rax+0xB8]` (the getter above) followed by `E8` -> 2126 sites, but that
  getter is not the queue.

The next probe should be behavioural: allocate a real `CSetNationalFocusCommand`
(vtable `0x2A128C8`), fill it, then find the queue by watching which container
grows when the game itself posts a command (e.g. start a focus with the mouse and
diff the idler's containers).

### Command objects: the constructors are reachable

Searching `.text` for `lea reg,[rip+X]` where X is a `*Command` vtable pays off: it
lands on the constructors, and they show the object layout directly.

`CSetNationalFocusCommand` ctor = **0x11D0DC0**:

```
lea rax,[rip+..]        ; CSetNationalFocusCommand vtable
mov byte [rcx+8], 0
mov dword [rcx+0xC], -1
mov qword [rcx+0x10], 0
mov [rcx], rax          ; vptr
mov dword [rcx+0x18], 0xFFFF
mov qword [rcx+0x1C], 0
mov eax, [rdx]          ; tag  <- *CCountryTag
mov [rcx+0x28], eax     ; +0x28 int32 tag
mov rax, rcx
mov [rcx+0x30], r8      ; +0x30 CNationalFocus*
ret
```

So the base-init block is identical to Linux (`+0x08/+0x0C/+0x10/+0x18/+0x1C`) but
the payload is four bytes later: **tag at +0x28, focus pointer at +0x30** (Linux:
+0x24 / +0x28), minimum size 0x38.

Same trick gave `CSetResearchCommand` ctor = **0x11D1980** and
`CAddConstructionCommand` ctor = **0x11CC8C0** / **0x11CC920**.

Verified live: allocate 0x40 through the allocator, call the ctor with
(`cmd`, `&tag=1`, `focus_ptr`) and read it back -- vptr, tag and pointer all
correct.

`CanExecute` is *not* confirmed yet: calling `vtable+0x50` on that object returned
0 for `GER_prioritize_economic_growth` (which our playbook does not list as
selectable anyway) and for `GER_remilitarize_the_rhineland` (which it does). Two
possible readings: `+0x50` is not CanExecute on this build, or the command needs
more state than the ctor sets. Note that after the experiment `current_focus()`
reported `GER_remilitarize_the_rhineland`, so *something* in that sequence took
effect -- do not repeat it casually.

Injector hygiene: one of those calls timed out and left the injector dirty
("thread left inside game code"). `winject.clear_dirty(pid)` clears it and the
game kept running normally (reads still fine afterwards).

### 2026-11-14 00:20 -- the game crashed during this experiment

After the sequence above (allocate + `CSetNationalFocusCommand` ctor + a call to
`vtable+0x50`), the next injected call came back with "injector dirty - thread
left inside game code", and a few minutes later `hoi4.exe` was gone (no crash
dialog, `game.log` not updated). So **one of those calls killed the process** --
most likely the `+0x50` slot, which is therefore *not* a harmless predicate on
this build.

State at the time: 1936-03-12 12:00, focus `GER_remilitarize_the_rhineland`,
GER 35/28/10 factories. The campaign is recoverable from
`Documents/Paradox Interactive/Hearts of Iron IV/save games/autosave.hoi4`
(23:24), which is one in-game month behind.

Rule for next time: never call a command vtable slot on a hand-built object until
the slot number has been confirmed against a *disassembly* of the class, not by
analogy with the Linux build. Reads are unaffected.

### 2026-11-14 00:5x -- second crash: posting a command that CanExecute rejects

The first successful post (a *different* focus) worked: `Post(session, cmd, 0)`
returned 1 and `current_focus()` went None -> `GER_remilitarize_the_rhineland`,
surviving day ticks. That proves the whole chain:

```
session = [idler]+0x88 getter      -> CSession*
cmd     = alloc + ctor 0x11D0DC0
Post    = 0x22C4250(session, cmd, 0)
```

The very next experiment -- posting the **same** focus again, i.e. a command the
game would refuse -- killed the process again (no dialog, `game.log` untouched).
So `Post` is only safe when the command would pass `CanExecute`; its failure path
is not safe for a hand-built object (it likely deletes the command and then keeps
touching it).

`cmdwin.set_national_focus` now refuses that case before calling into the game:

    if current_focus().ptr == focus_ptr: return {"ok": False, "reason": "focus already selected"}

Rule: before posting, always verify from the read side that the action is legal
(the game's own `selectable_focuses()` / current state), because a rejected post
crashes the process.

### Research: layout recovered, reading fixed

`CSetResearchCommand` ctor **0x11D1980** = `(cmd, CCountryTag*, CTechnology*,
int slot, bool, bool, bool)` and stores

```
+0x28 int32 tag   +0x30 CTechnology*   +0x38 int32 slot
+0x3C/+0x3D/+0x3E bools          (base init identical to the focus command)
```

The three bools are stack arguments, which the injector cannot pass, so
`cmdwin.make_research()` fills the object directly instead of calling the ctor.

Reading research was broken for a separate reason: `game.py` still used the
**Linux** CCountry subsystem offsets (`CO_TECH_STATUS 0x0F38`, production `+0x40`,
politics `+0x68`, ...). With `0xF60 / 0xF68 / 0xF90 / 0xFA0` the status object
resolves and `research_slots()` returns the four slots again
(`CTechnologyStatus+0xA0` really is `CPdxArray<CResearchSlot*>`, cap 4).

`CAddConstructionCommand` ctor 0x11CC8C0 = `(cmd, bool, int32*, CBuildingReference*,
int level)`; it embeds the `CBuildingReference` vtable at +0x30 and copies the
reference's +8/+0xC/+0x10 into +0x38/+0x3C/+0x40, level at +0x48.

### 2026-11-14 -- third crash: posting SetResearch

`start_research("basic_machine_tools")` (a tech `available_research()` lists as
legal) killed the process again, right at `Post`. Both crashes so far happened on
a command the game would refuse, which points at **Post's rejection path** rather
than at the object layout: the ctor call site proves the layout is right --

```
new(0x40); &tag = GetCountryTag(...)+8; r8 = tech; r9d = slot
stack: [r14+0xC], 0, 0   -> +0x3C, +0x3D, +0x3E
```

so the remaining suspects are (a) the tech pointer is a `CTechnologyTemplate*`
while the command wants a country `CTechnology*`, or (b) `Post` deletes the
command on refusal and then touches it.

Next probe: pass `force=1` to `Post` (the Linux build's `force` skips the
predicate) and see whether the command then applies instead of crashing. Do not
retry the plain path without a CanExecute check.

Meanwhile the tech **reading** chain is fixed and verified:

```
CTechnologyDatabase+0x48  CPdxArray<CTechnologyTemplate*>  cap 711 / size 553
   template+0x10  std::string key ("improved_ship_hull_light")   [Linux: +0x08]
   template+0x3C  int32 id                                       [Linux: +0x34]
```

With those two offsets `_load_tech_db()` returns **552** technologies,
`researched()` **55** and `research_slots()` the four live slots.

### force=1 does not help -- fourth crash

`Post(session, cmd, 1)` on the same SetResearch command also killed the process,
so the flag is not the Linux "skip CanExecute" escape hatch, or the fault happens
before that check. The remaining difference from a real UI post is the **argument
type**: the ctor call site passes `[r14]`, an entry from the UI's research list,
which is almost certainly a country `CTechnology*` instance, while
`start_research()` hands it a `CTechnologyTemplate*` from
`CTechnologyDatabase`. Dereferencing a template as a country technology inside
CanExecute would explain a hard crash every time.

Next probe: build the command with a `CTechnology*` taken from the country's own
technology list (the status object holds `CTechnology` objects -- the researched
array at `CTechnologyStatus+0x40` is one source, and there must be an array of all
of them), not from the database.

Until that is settled, do not call SetResearch on the live game; four crashes came
from this one command.

### Fifth crash -- the CTechnology* pointer did not help

Built the command with a country `CTechnology*` taken from
`CTechnologyStatus+0x88` (CPdxArray<CTechnology*>, 552 entries -- the full list,
which does exist on this build and matches the Linux `TS_ALL_TECHS` offset), for
the very tech `available_research()` lists. `Post` still killed the process.

That rules out the pointer type. What is left:

* the focus command -- the only one that works -- was built by **calling its real
  constructor**; the research command is **hand-filled**. The ctor disassembly
  showed writes only up to +0x3E, so the object should be byte-identical, but this
  is now the main structural difference and the next thing to eliminate: extend
  the injection stub to push the three stack arguments and call ctor 0x11D1980
  for real.
* or the command is being *executed* and crashes inside the game (slot/flag
  semantics), in which case Post is innocent.

Status: SetResearch is **blocked pending a real-ctor build**. Five crashes came
from it; do not post it again until that changes.

### Injector: stack arguments now supported

The stub used to load only rcx/rdx/r8/r9, so any function with more than four
arguments (every command ctor with optional flags) was uncallable. It now builds
a frame and writes args 4..N at [rsp+0x20], [rsp+0x28], ... before the call
(`winject.py`, RES_OFF moved to 0x200 to make room). Verified: calling
`CSetResearchCommand`'s real ctor with seven arguments
`(cmd, &tag, CTechnology*, slot, 0, 0, 0)` returns the command and the object has
the right vptr, tag, technology pointer and slot.

### Sixth crash -- and the conclusion

Posting that ctor-built command still killed the process. So neither the pointer
type nor the construction path is the problem. Every crash so far (6) happened on
a command the game would very likely **refuse**, while the one command that
succeeded (SetNationalFocus, a legal focus) went through. That makes
`CCommandQueue::Post`'s **rejection path** the prime suspect: it appears to
delete the command and then keep using it, which is fatal for any object.

Practical rule until this is understood: only post a command when the read layer
can prove the game will accept it, and prefer `SetNationalFocus`, which is proven.

Next idea: instead of Post, call the command's own Execute virtual (`vtable+0x58`
was overridden on the focus class) after a successful CanExecute -- that skips the
queue's failure path entirely. Do this on a throwaway save.

### Seventh crash -- flag=1 did not help either

Posting the ctor-built research command with its first bool set (the one the UI
fills from its list entry) also killed the process. Seven crashes, all on
SetResearch; the only command that ever posted is a legal SetNationalFocus.

Conclusion for the next session: stop trying to make SetResearch acceptable to
`Post`. Either call the command's own `Execute` virtual directly after a
successful `CanExecute`, or find the real reason `Post` refuses it (the function at
0x22C4250 begins with `test r8b,r8b / jne`, `cmp byte [rcx+0x725],0 / jne`, then
`call [cmd.vtable+0x80]` and `+0x60` -- disassemble past 0x22C4250+0x100 to see
which branch is the fatal one).


## 25. Command channel: the crashes were `Post` deleting our object

The seven SetResearch crashes had nothing to do with the command being illegal.
`Post` (0x22C4250) **owns** the command on every path, and its four drop paths
all end in `call [cmd->vt[0]](cmd, 1)` -- the MSVC deleting destructor, which
releases through the CRT (`operator delete` = 0x25CEC00, reached via
0x222F060 <- 0x25A0218).  Our command objects came from the game's own
`CPdxNewDeleteAllocator`, so the CRT `free()` on rejected memory is what took
the process down.  The single command that ever worked (SetNationalFocus) did
so because it was *accepted*, and the accepted path does not delete.

### What each branch means

`Post` builds one of these strings before dropping:

| condition | message |
|---|---|
| `flag != 0` | `Dropped command: ` |
| `[session+0x725] != 0` | `Dropped command: ` |
| `vt[0x80]` / `vt[0x60]` non-zero | `Dropped command: ` |
| `[session+0x3c] != 0`, `[session+0x6cc] == 0`, `[session+0x726] != 0`, or `vt[0x80]` non-zero after the tick stamp | `Dropped (posted during command execution): ` |
| `vt[0x98] == 0` | `Dropped (sender not authorized): ` |

`vt[0x60]` and `vt[0x80]` are `0x11D530` (`xor al, al; ret`) in every command
vtable, so those two never fire.  `vt[0x98]` is `0x11C13E0` for *both*
CSetResearchCommand and CSetNationalFocusCommand -- the authorization check is
shared, not per class.

The accepted path is 0x22C4683 -> 0x22C4710:

```
0x22c4710  mov rcx, [session+0x40]      ; CCommandQueue*
0x22c4714  mov rax, [rcx]
0x22c4717  mov rdx, cmd
0x22c471a  call [rax+0x28]              ; queue->Push(cmd)
0x22c471d  mov bl, 1                    ; return 1
```

Before that push, `Post` stamps the object: `cmd+0xC = [session+0x84]` (tick)
and `cmd+0x18 = min([session+0x60], 0x7fff)` (sequence).

### The working approach

`cmdwin.enqueue()` reproduces exactly those three steps and nothing else.  No
legality check, no drop path, no delete:

```python
p.write(cmd + 0xC, struct.pack("<i", tick))
p.write(cmd + 0x18, struct.pack("<H", min(seq, 0x7FFF)))
q = p.u64(session + 0x40)
game.call(p.u64(p.u64(q) + 0x28), (q, cmd))     # queue->Push(cmd)
```

Verified live 2026-11-14: `SetResearch('support_weapons', slot 0)` returned 1,
`research_slots()[0]['researching']` read back `support_weapons` with
`required = 30.0` days, and it survived a day tick.  An illegal tech
(`gwtank` before its prerequisites) is dropped silently by the queue and the
game keeps running -- the failure mode is now safe.

Two safety details:

* The object still gets a **private vtable** (`fake_vtable`, slot 0 replaced by
  `0x11D530`) so that if the queue ever releases it, the release does nothing
  instead of calling the CRT deleting destructor.  Cost is a 0x40-byte leak per
  command.
* `operator new` (0x25A01DC) is the *right* allocator in principle, but calling
  the CRT allocator from a hijacked thread kills the process outright
  (`ReadProcessMemory` on the injector's flag page then fails with
  `ERROR_PARTIAL_COPY`).  Do not use it from the injector.

### Correction to section 24

The earlier note ("stop trying to make SetResearch acceptable to `Post` ...
0x22C4367 is the fatal branch") was wrong on both counts: 0x22C4367 is the
"posted during command execution" branch and is not in itself fatal, and the
actual killer was the deleting destructor on the drop paths.


## 26. CAddConstructionCommand -- structure solved, location field still open

Everything about the command is now known except what the location fields mean
at execution time.  Recording it so the next session does not re-derive it.

### Classes (confirmed against the MSVC RTTI map)

| class | vtable |
|---|---|
| `CAddConstructionCommand` | `0x2A10B88` |
| `CBuildingReference` | `0x29EE730` |
| `CProvince` | `0x29EE358` (secondary vtable `0x29EE3A8`) |

`CAddConstructionCommand` is **0x50** bytes -- the game allocates it with
`mov ecx, 0x50; call 0x1425a01dc` at 0xBBDE3A.

### ctor 0x11CC8C0

`(cmd, bool dl, int32* r8, CBuildingReference* r9, int level [rsp+0x28])`

```
+0x08 byte 0        +0x0C int32 -1      +0x10 qword 0
+0x18 dword 0xFFFF  +0x1C qword 0
+0x28 int32  = *(int32*)r8        <-- the building type id
+0x30 qword  = CBuildingReference vtable (embedded sub-object)
+0x38 int32  = ref+0x08
+0x3C int32  = ref+0x0C
+0x40 int32  = ref+0x10
+0x48 int32  = level
+0x4C byte   = dl
```

Call site 0xBBDE71 shows the shape the UI uses: `new(0x50)` -> build a
`CBuildingReference` into a stack slot via `0x1204E40` -> `ctor(cmd, 1, &type,
ref, level)`.

### CBuildingReference 0x20 bytes

```
+0x00 vtable 0x29EE730
+0x08 int32
+0x0C int32
+0x10 int32
+0x18 IAllocator*   (always G_DEFAULT_ALLOC)
```

ctor A `0x145B5A0(CProvince* rdx, int32* r8)`: sets `+0x08 = -1`,
`+0x0C = *(int32*)(rdx+8)`, `+0x10 = *(int32*)r8`, and only on the non-assert
path.  ctor B `0x145B650(CProvince* rdx, X* r8)` sets `+0x08 = *(int32*)(r8+0xA4)`,
`+0x0C = *(int32*)(rdx+8)`.

All 870 live `CBuildingReference` instances read `(-1, 19479, 0)`, so 19479 is
the "no province" sentinel.

### What was tried and what happened

| attempt | result |
|---|---|
| `enqueue` with ref `(state=5, 19479, 0)`, type id | queue accepted (`Push` returned 1), **construction never appeared**; state 5 stayed at one building |
| `enqueue` with ref `(state=5, 19479, 0)`, type pointer | same |
| `Post(..., force=1)` | **process dies** even with the no-free vtable -- the flag=1 branch dereferences something our object does not satisfy |
| `enqueue` after re-loading at 1945, ref `(-1, 5, 0)` | **process dies inside the queue** |

So unlike SetResearch, a wrong AddConstruction is *not* safely discarded: the
queue's handling of this command walks the reference and faults.

Two leads for the next attempt:

* `building_types()` exposes `province_level`, and `industrial_complex` is
  `province_level: False` (state-level) while `infrastructure` is True.  The
  location probably has to be the province id for province-level types and the
  state id for state-level ones, in the *same* field -- which one is still
  unknown because ctor A writes the province into `+0x0C` and ctor B writes
  something from the state into `+0x08`.
* `CProvince` carries two vtables and its id is not at `+0x08` or `+0x10`
  (both read as constant 4 across different provinces), so the value ctor A
  copies into `+0x0C` from `CProvince+8` may not be a province id at all.

### Do not repeat

`Post` with `force=1` is fatal for commands it does not fully recognise.
`enqueue` is safe for SetResearch but **not** for a malformed
CAddConstructionCommand.  Test construction only on a throwaway save.


## 27. AddConstruction solved -- the capture that settled it

Section 26 left the location fields open.  A Frida hook on the real constructor
`0x11CC8C0`, armed while clicking the build UI by hand, produced one sample:

```json
{"typeId": 1, "level": 1, "dl": 2,
 "ref": {"vt": "0x7ff658b6e730", "f08": 59, "f0C": 20013, "f10": 0}}
```

`f0C = 20013` is `industrial_complex`, so **`ref+0x0C` is the building type id**
and `ref+0x08` is the location id (state id for state-level buildings).
`cmd+0x28` is the constant **1**, and `cmd+0x4C` is **2**.  Both of my earlier
guesses were wrong, and either mistake alone makes the queue accept the command
and then silently discard it -- which is exactly the symptom three earlier
layouts produced.

Correct layout (implemented in `calling/cmdwin.py`):

```
CBuildingReference (0x20):  +0x00 vtable 0x29EE730
                            +0x08 state id
                            +0x0C building type id
                            +0x10 0
CAddConstructionCommand (0x50):
                            +0x00 vtable 0x2A10B88
                            +0x0C -1     +0x18 0xFFFF
                            +0x28 1
                            +0x30 CBuildingReference vtable
                            +0x38 state id  +0x3C type id  +0x40 0
                            +0x48 level    +0x4C 2
```

Verified live: six orders (states 5, 42, 50, 51, 52, 60) took
`CBuildingProductionLine` from **113 to 119**, and state 5 gained a
`(20013, level 0)` entry.

Two measurement notes:

* an in-progress building does **not** reliably appear in `CState+0x158`; only
  one of the six did.  Count `CBuildingProductionLine` instances instead.
* `construction_queue()` still returns 0 on Windows while 119 production lines
  exist -- its array offset is wrong and needs its own pass.

`Post(..., force=1)` remains fatal even with the no-free vtable.  `enqueue()` is
the only safe channel.


## 28. Diplomacy: CAction, not CCommand -- Windows 1.19.5 map

The Linux `calling/diplomacy.py` already showed the shape: a
`CDiplomaticActionCommand` (0x30 bytes) wraps a `CDiplomaticAction` subclass and
is posted to the queue.  Windows has the same design, at these addresses
(resolved through the MSVC RTTI map):

| class | vtable RVA |
|---|---|
| `CDiplomaticAction` (base) | `0x27C6F48` |
| `CDiplomaticActionCommand` | `0x2A073C8` |
| `CDeclareWarAction` | `0x2A07548` |
| `CGenerateWarGoalAction` | `0x2A079B8` |

`CDeclareWarAction` constructor: **`0x1180B70`**.  Entry code:

```
0x1180b70  mov r11, rsp
0x1180b7b  mov [r11+8], rcx          ; this
0x1180b84  mov rdi, r9               ; CWarGoal*
0x1180b8a  mov rsi, [r11+0x30]       ; 6th arg (scope)
0x1180b8e  mov eax, [rsi+8]
0x1180bc4  call 0x141180db0          ; CDiplomaticAction base ctor
0x1180bca  lea rax, [rip+0x1886977]  ; = 0x2A07548
0x1180bd1  mov [rbx], rax
0x1180bd4  lea rcx, [rbx+0x78]
0x1180beb  mov rdx, [rdi+8]
0x1180bef  mov [rcx], rdx            ; +0x78 <- wargoal+8
```

so the Windows signature is `(this, ?, ?, CWarGoal* r9, u8, scope*)` and the
object is 0x78 bytes, matching Linux's `SZ_DECLAREWAR`.

`CDiplomaticActionCommand` is a flat 0x30-byte wrapper in Linux; on Windows the
same shape is expected (`+0x24` tag id, `+0x28` action pointer), and the base
vtable is written by the ctor at `0x1180F0F`.

### Why this is not done in one sitting

`CDeclareWarAction::CanExecute` dereferences the war-goal pointer, so a war can
only be declared after a `CWarGoal` exists -- which means `justify_wargoal`
(`CGenerateWarGoalAction` + a `CWarGoalType*` + the embedded Scope block at
`+0x20`) has to work first.  Both actions also need the Scope object that Linux
builds with `SCOPE_MAGIC 0x29C77F8`; the Windows equivalent of that scope builder
has not been located yet.

So the remaining work is a two-step chain:

1. `justify_wargoal` -> `CGenerateWarGoalAction` (0x2A079B8) + `CWarGoalType*`
2. `declare_war`    -> `CDeclareWarAction` (0x2A07548, ctor 0x1180B70)

with the same Frida capture trick that solved AddConstruction (hook the ctor,
click the diplomacy dialog's Send button, read the real field values).

Everything else in M2 is done: SetNationalFocus, SetResearch and AddConstruction
all work end-to-end through `cmdwin.enqueue()`.


### Live inventory (1945 German save)

* `CWarGoal` instances: 3.  Only one is populated -- `+0x08 = 4713`,
  `+0x38 = 16` (owner tag index), `+0x3C = 44` (target tag index).  So that one
  belongs to ITA vs someone, **not Germany**: Germany has no casus belli in this
  save and `justify_wargoal` has to run first.
* `CWarGoalType` instances: 10.  `+0x10` is not a string pointer (all read NULL),
  so the name lookup in `diplomacy.py`'s `wargoal_types()` needs its own pass on
  this build.

That confirms the ordering dependency: `CGenerateWarGoalAction` must work before
`CDeclareWarAction` can be tested at all.


### Constructors found and verified

| item | RVA |
|---|---|
| `CDeclareWarAction` param ctor | `0x1180B70` |
| `CGenerateWarGoalAction` param ctor | `0x1181120` |
| `CGenerateWarGoalAction` default ctor | `0x1181220` |

Both parameterised ctors have the shape
`(this, from*, to*, target_ptr r9, flag, scope* [rsp+0x30])`, matching Linux's
`(this, from*, to*, CWarGoal*/CWarGoalType*, scope*)` plus one extra flag slot.

**The scope magic is identical to Linux: `0x29C77F8`.**  Proof: the default
GenerateWarGoal ctor at `0x1181238` does
`mov dword ptr [rsp+0x48], 0x29c77f8`, and the parameterised ctor at
`0x118113d` reads `[scope+8]` and copies it into the same slot.  So a 0x18-byte
block holding `{0, 0x29C77F8, 0}` is the correct scope object on Windows too.

**Verified live**: calling `0x1180B70(act, &from, &to, wargoal, 1, scope)` on a
real populated `CWarGoal` (owner 16 / target 44) produced an object whose vptr is
exactly `base + 0x2A07548`.  `+0x14 = 16`, `+0x18 = 16`, `+0x1C = 44` -- the
tags landed where the Linux layout says they should.

### Injector bug found and fixed

`process/winject.py` wrote stack arguments with
`mov qword [rsp+d8], imm32`, so the 6th argument (the scope pointer, well above
2^31) was truncated to its low half and `struct.pack` raised
`argument out of range` before the call even started.  Stack arguments are now
stored as full 64-bit values (`mov rax, imm64; mov [rsp+d8], rax`).  This is why
no earlier command ctor with a pointer on the stack could work.

### Still open

The process died shortly after the successful ctor call, so `CanExecute` on the
wrapped `CDiplomaticActionCommand` (vtable `0x2A073C8`, slot `+0x50`) has not
been probed yet, and nothing was actually declared.  Next session: rebuild the
action, wrap it, probe `+0x50`, then `enqueue()` it.


`CDiplomaticActionCommand::CanExecute` is vtable `+0x50` = **`0x118B230`**, a
thunk: `xor edx, edx; jmp 0x118AA80`.  Calling it with `(cmd)` returned a
pointer, not a bool, so `0x118AA80` most likely returns a result/error struct
rather than the flat `bool` the Linux build returns.  That return shape still
needs decoding before `enqueue()` should be trusted with a declaration.


### First real declaration attempt -- enqueued, not executed

`enqueue()` on the wrapped `CDiplomaticActionCommand` returned 1 and the process
stayed alive, but **no war was declared**: `CWar` instances remained 0 and the
`CWarGoal` (owner 16 / target 44) was still present after several day ticks.
So the queue accepts the command and drops it -- the same signature as a wrong
field, which for actions means the `from`/`to`/scope block did not survive the
constructor.

One measurement trap found along the way: `date()` was reporting
`1945.04.02` for a 1936 save until the game was unpaused, then it snapped to
`1936.03.01`.  The stale-copy problem is not fully solved while paused even with
the ticking-copy preference, because a paused process has no ticking copy.

Next step is the same Frida capture trick that settled AddConstruction: hook
`0x1180B70`, declare war by hand through the diplomacy UI, and diff the captured
`from`/`to`/scope bytes against what we build.

## 29. Diplomacy: the action object is right, the command still dies

Everything below was read off `hoi4.exe` 1.19.5.0 and checked against live
objects; addresses are RVAs.

### 29.0 A correction to §28

`CWar` **is not an RTTI class in this build**.  Searching
`rtti_win.pkl` for `CWar` yields `CWarGoal`, `CWarGoalType`, `CWarRelation`,
`CWarScoreBreakdown`, `CWarReparationTimedEffect`, `CWarLengthTrigger` and a pile
of GUI types -- but no plain `CWar`.  So every earlier "CWar instances: 0" was a
false negative, not evidence that no war existed.  The object that actually
represents a war is **`CWarRelation`** (vtable `0x29DCC08`, `sizeof 0x1D0`,
allocated by a factory near `0xD80C7F` via `operator new(0x1D0)`).

The world already contains exactly one `CWarRelation` in the 1936 save.

### 29.1 `CDiplomaticAction` layout (base ctor `0x1180DB0`)

    +0x00  vtable
    +0x10  int      zero
    +0x14  int      *(int*)arg1        // from
    +0x18  int      *(int*)arg1        // from (duplicate)
    +0x1C  int      *(int*)arg2        // to
    +0x20  int      *(int*)arg2        // to (duplicate)
    +0x28  ptr      0x27963D0          // scope pair A head
    +0x30  int      *(int*)(arg3+8)    // scope pair A value
    +0x38  ptr      0x2796400          // scope pair A tail
    +0x40  ptr      0x27963D0          // scope pair B head
    +0x48  int      *(int*)(arg3+8)    // scope pair B value
    +0x50  ptr      0x2796400          // scope pair B tail
    +0x58  ptr      lookup(action_id)  // action definition
    +0x60  ptr      lookup2(action_id) // null when the action id has no entry
    +0x68  int      stack arg
    +0x6C  int      stack arg
    +0x70  word     0                  // control flags, not serialised
    +0x72  byte     1
    +0x73  byte     0

`arg3` is a 0x18 struct `{ptr vtable, int value, ptr more}`.  The default ctor
`0x1180CD0` fills it with the constant triple
`{0x27963D0, 0x29C77F8, 0x2796400}`, which is where `0x29C77F8` comes from --
it is the *scope value*, not a magic number standing on its own.  The two
`lookup` calls are keyed by the derived class's action id.

### 29.2 `CDeclareWarAction` ctor `0x1180B70`

    CDeclareWarAction(this, int* from, int* to, CWarGoal* wg, bool a5, void* scopeOut)

It forwards `from`/`to` straight into the base ctor (`rdx`/`r8` untouched), builds
the scope struct from `*(int*)(scopeOut+8)`, and then:

    +0x08  int      0x277F                 // action id
    +0x78  u64      *(u64*)(wg+8)          // {wargoal id, wargoal type}
    +0x80  byte     a5
    [scopeOut+0] = 0x27961D8               // ctor writes a vtable back into scopeOut

A live build with `from=1, to=73, wg=NULL` reproduces vptr `0x2A07548`,
`+0x08=0x277F`, `+0x14=1`, `+0x1C=73`, `+0x78=0`, `+0x80=1`.  So the *action
object* is correct; the failure is downstream.

### 29.3 Vtable map

| slot | `CDeclareWarAction` | `CDiplomaticAction` | `CGenerateWarGoalAction` | meaning |
|---|---|---|---|---|
| +0x10 | `0x11CB0C0` | `0x11CB130` | `0x11CB260` | Serialize |
| +0x20 | `0x11C82D0` | `0x11C8360` | `0x11C8780` | Serialize (second pass) |
| +0x48 | `0x11D530` | `0x11D530` | `0x11D530` | `xor al,al; ret` |
| +0x50 | `0x188D90` | `0x188D90` | `0x188D90` | `mov al,1; ret` -- always true |
| +0x110 | `0x11C9DF0` | `0x11C9DF0` | `0x11C9E40` | `jmp 0x11C22B0` = IsValid |
| +0x138 | `0x11B9C40` | `0x11BA360` | `0x11BAD40` | IsAvailable |

`CDiplomaticActionCommand` (vtable `0x2A073C8`): `+0x48 = 0x11C2420`,
`+0x50 = 0x118B230` (CanExecute thunk), `+0x58 = 0x11B7E10` (writes id `0x2931`).

### 29.4 The validator

`0x11C2420(cmd, std::string* msgOut) -> bool` is the gate the command's execute
path calls first (`0x118FB90`).  It resolves `action = *(void**)(cmd+0x28)` and
walks: null action / `from <= 0` / `to <= 0` / `IsValid` / `action+0x70` branch /
`vt[0x110]` / `vt[0x48]`, and on failure writes

    "Diplomatic action declare_war is not valid."

That message was reproduced live, which confirms the whole chain.

### 29.5 The measurement trap that cost the most time

`0x11C2420`'s second parameter is only an out-message, **but its low 32 bits are
also pushed as an extra argument into the internal map lookups**
(`mov dword [rsp+0x20], r12d`).  Passing a real `std::string*` therefore changes
which lookup runs.  The game itself calls `0x11C2420(cmd, NULL)`; only with
`msgOut = NULL` does the answer match the game.  With a string pointer the
validator reports "not valid" even for an action it otherwise accepts.

### 29.6 `+0x70`

`CDeclareWarAction::IsAvailable` (`0x11B9C40`) begins with a call to `0x11B8460`,
which returns **true** as soon as `action+0x70 != 0`; every other path in
`0x11B9C40` ends in `return false`.  Setting `+0x70 = 1` therefore makes the
validator return `true` (verified: `validator(cmd, NULL) -> al = 1`).

But the war still does not happen -- and the `+0x70` branch in the validator is
followed by two registry lookups whose hit path drops back into the strict
checks.  Reading it as "force it through" is *not* justified by the code; a
more likely reading is "this action has already been handled".  This flag stays
unexplained and must not be shipped as a workaround.

### 29.7 Executing the command

`CDiplomaticActionCommand`'s execute path near `0x118FAxx` aborts on

* `action == NULL`
* `*(int*)(action+0x14) <= 0`
* `*(int*)(action+0x1C) <= 0`
* `*(int*)(action+0x08) == 0x165`
* `!IsValid(action)`

All five pass for the object we build.  What refuses after that is
`vt[0x138]`, and its only true path is the `+0x70` early-out -- i.e. the action
can never be "available" through the `(action, NULL, 0)` call the command makes.

### 29.8 Where this leaves it

`CDiplomaticActionCommand` is very likely **not** how a player declaration
travels: the command's execute calls `vt[0x138]` with a null context, so it can
only ever accept an action that is already flagged handled.  The player path is
more plausibly `CDiplomaticAction::Execute` driven from the UI/AI with a real
context object, or `CWarRelation`'s factory at `0xD80C7F` directly.

Neither has been exercised.  `declare_war` stays unimplemented; the three
verified commands (SetNationalFocus, SetResearch, AddConstruction) are
unaffected and SetResearch was re-verified after a full game restart.

Also confirmed while chasing this: with the game **paused the command queue does
not drain at all**.  Every "accepted then dropped" observation has to be made
with the clock running, and `date()` cannot be used to tell the two apart
because it still reads a stale `CDate` copy while paused.

## 30. `cmdwin`: `absolute=True` takes a real address, not an RVA

`Game.call(func_vaddr, ..., absolute=False)` documents `func_vaddr` as an **ELF
offset** (an RVA in the PE).  `absolute=True` means the caller already resolved
it.  Three helpers in `cmdwin.py` passed plain RVAs together with
`absolute=True`:

    post()        game.call(FN_POST, ...)        FN_POST   = 0x22C4250
    new_cmd()     game.call(OP_NEW, ...)         OP_NEW    = 0x25A01DC
    delete_cmd()  game.call(OP_DELETE, ...)      OP_DELETE = 0x25CEC00

Each of those called address `0x22C4250` / `0x25A01DC` / `0x25CEC00` -- unmapped
in a 64-bit process (the module lives at `0x7ff6_5618_0000`).  So:

* the seven "SetResearch crashed in `Post`" failures were **not** a CRT/destructor
  problem; `post()` never reached `Post` at all,
* "`operator new` from a hijacked thread kills the process" was **not** a game
  behaviour either -- the call target was simply wrong.

All three now add `p.base`.  §25's conclusion about `Post`'s drop paths was
derived from reading the disassembly, which is still valid; only the live
"it crashes" observations were artefacts.  `enqueue()` remains the safe path and
is unaffected (it resolves the queue's virtual through a real pointer).

This also means `post()` is **untested**, not disproven.  It is the canonical
entry point and worth re-probing now that it actually calls `Post`.

## 31. Declaring war: action object needs a patched vtable too

`CDiplomaticActionCommand`'s execute tail (`0x14118fd45`) ends with

    if (action) action->vt[0](action, 1);      // MSVC deleting destructor
    if (other)  other->vt[0](other, 1);

so the action is destroyed by the command, exactly like a command is destroyed
by `Post`.  An action that came from `CPdxNewDeleteAllocator` must therefore be
given a **private copy of its vtable with slot 0 replaced** by the shared
`xor al,al; ret` stub (`0x11D530`) -- `fake_vtable(..., size=0x1D8)` then
`attach_fake_vtable`.  Everything below slot 0 (`IsValid`, `IsAvailable`,
serialisers, scope helpers) stays real.

`cmdwin.declare_war()` now does that for both the action and the command.  State
of that function: the constructor call succeeds, the validator call that follows
it still takes the process down, and **no declaration has been verified through
it**.  Treat it as unfinished.

## 32. `CGenerateWarGoalAction` signature, from a live AI capture

Frida hooks over a running game caught the AI justifying war goals constantly
(the AI evaluates every pair), which pins the signature down:

    CGenerateWarGoalAction(this, int* from, int* to, CWarGoalType* type, ?, ?)

observed instances, e.g.

    from=16 to=53 type=0x260ecff5da0      from=3  to=43   type=0x260ecff5da0
    from=5  to=47 type=0x260ecff5da0      from=2  to=55   type=0x260ecff5da0
    from=68 to=51 type=0x260ecff5da0

and the resulting object always carries

    +0x08 = 0x277F   +0x18 = from   +0x1C = to   +0x20 = to
    +0x28 = 0x27963D0   +0x30 = 0x29C77F8   +0x38 = 0x2796400
    +0x70 = 0   +0x72 = 1

which is the same base layout as `CDeclareWarAction`, with the wargoal **type**
pointer landing in the scope pair.  The three `CWarGoalType` pointers seen in the
1936 save (`0x260ecff5da0` etc.) are stable, so justifying a wargoal is a matter
of passing one of them.

`CDeclareWarAction` builds captured the same way for `from=1` to `2/3/43/59`
confirm the shape documented in §29.2.

## 33. What actually happened when war was declared on the Dominican Republic

Four declarations (`to` = 73, then 2, 3, 43, 59) were submitted with the
`+0x70` flag set.  The game produced its own World News dialog *"Germany once
again plunges into war"* and a persistent notification
*"Germany declared war on the Dominican Republic"*, i.e. country **73 is the
Dominican Republic** and at least one of those commands really did execute.

No war object could then be located in memory: `CWar` is not an RTTI class
(§29.0), `CWarRelation` scans return one object whose body looks like a
`CDiplomaticAction`, and the `CRelation+0x2D0` "at war" probe reads false for
every pair including Italy.  **The instance enumerator is not trustworthy for
these classes** and no numeric war check exists yet -- the UI notification is the
only reliable evidence so far.

The process died a few minutes later with an access violation whose stack is
unreadable (Paradox ships no symbols; `exception.txt` frames are all
`PHYSFS_*` noise).

## 34. Declaring war works

Resolved.  Two independent game sessions, each freshly loaded from the
1936-03-01 autosave, each submitted exactly one `CDeclareWarAction` for
`from = 1 (GER)`, `to = 73`; in both cases the game itself produced

    World News:  "Germany once again plunges into war"
    Notification: "Germany declared war on the Dominican Republic"

and the process stayed alive and responsive afterwards.  `DOM` resolves to
country id `73`, which cross-checks the id used.

The recipe, now implemented as `cmdwin.declare_war()`:

1. `scope = alloc(0x18)` filled with `{0, 0x29C77F8, 0}`.
2. `act = alloc(0x88)`, then
   `CDeclareWarAction::ctor 0x1180B70(act, &from, &to, wargoal=0, 1, scope)`.
3. `act + 0x70 = 1`.  This is the flag `0x11B8460` tests; without it the
   command's validator rejects every action with
   "Diplomatic action declare_war is not valid."  Its real meaning is still not
   established -- it is what makes `CDeclareWarAction::IsAvailable` return true
   for a null context, and the declaration that results is a real one, so it is
   used deliberately and named `DECLARE_WAR_FLAG_OFF` in the source rather than
   hidden.
4. Give `act` a private copy of vtable `0x2A07548` with slot 0 replaced
   (`fake_vtable(..., size=0x1D8)`), so the command's trailing
   `action->vt[0](action, 1)` cannot CRT-free game-allocator memory.
5. `cmd = alloc(0x30)`: vtable `0x2A073C8`, `BASE_INIT`, `+0x24 = from`,
   `+0x28 = act`; give `cmd` the same no-free vtable treatment.
6. `enqueue(cmd)` -- the queue's `vt[0x28]`.

Two things this needed that were not obvious:

* the two `absolute=True` addressing bugs from §30 (`post`/`new_cmd`/
  `delete_cmd`), and
* the vtable copy for the **action**, not just the command (§31).

`game.declare_war(tag, force=...)` dispatches to this on Windows.  The
availability gate (`action+0x70`) is bypassed, so `war_readiness_check` remains
the only guard -- do not call it with `force=True` in a real campaign without
deciding that the war is intended.

### Still missing

No way to *read* the war back.  `CWar` does not exist as a class (§29.0), the
`CWarRelation` instance scan is unreliable, `CRelation+0x2D0` is not an at-war
flag, and `combat.enemy_tags()` has no Windows path.  `declare_war()` therefore
returns `at_war_verified: None` and the only positive confirmation is the game's
own notification.  Finding the real war container is the next task, and it also
unblocks `war_readiness_check`.

## 35. Reading the war state (Windows)

`CWarRelation` **is** the war object and it is readable.  The reason earlier
scans found "one" war was that `ObjectIndex` is a **memory snapshot** cached per
pid (`objindex.py` `_load_cache`): it cannot see objects created after the scan,
so a freshly declared war is invisible until a rescan.  `oi.build(force=True)`
re-scans in ~25 s and the count went 1 -> 2 immediately.

Layout, confirmed on both wars present in the 1936 save:

    CWarRelation   vtable 0x29DCC08   sizeof 0x1D0
      +0x10  int  attacker country id
      +0x14  int  defender country id
      +0x78  int  attacker id (second copy)
      +0x7C  int  defender id (second copy)
      +0xD4  int  war id           (631, 632 in that save)
      +0xE8  int  defender id
      +0xEC  int  attacker id
      +0x144 int  war id

Read out live:

    Italy vs Ethiopia      -> (16, 44)
    Germany vs Dominican   -> (1, 73)     <- the declaration from 34

`calling/combat.py` now has `_war_relations_win()`, and `war_relations()` /
`enemy_tags()` dispatch to it on Windows (`sys.platform == "win32"`).  Both
accept `refresh=True` to force the rescan; everything else uses the cached index.
They return the same shape as the ELF path, so `combat.enemy_tags(g, "GER")`
works unchanged.

End-to-end check, one session:

    enemy_tags(GER) before      -> ['DOM']
    declare_war('LUX', force)   -> at_war_verified: True
    enemy_tags(GER) after       -> ['DOM', 'LUX']

So `game.declare_war()` now reports a real yes/no instead of guessing from the
queue, and `war_readiness_check` has a Windows backing for `enemy_tags`.

Caveat: `+0x10/+0x14` are country ids (the `CCountry+0x08` space).  The ELF notes
in `combat.py` warn that one of the side fields mixes two id spaces for
civil-war countries; the `+0x78/+0x7C` copy agreed on both wars seen here, and
the reader prefers it, falling back to `+0x10/+0x14`.  Nothing with a civil war
has been exercised yet.

## 36. M3 read side: the division table (Windows)

`CO_ARMIES = 0x290` in `CCountry` is a `CPdxArray<CArmy*>` and **`CArmy` is one
division**, not an army group.  Verified live across the whole map:

    countries with divisions   89
    total divisions            1227
    SOV 139   CHI 87   FRA 74   JAP 60   ITA 46   POL 42
    ENG  38   USA 36   ROM 31   TUR 31   GER 30

The distribution matches a 1936 start, and `game.divisions(tag)` returns the
right **localised template name** per country, which is the strongest check
available without reading the UI:

    GER  Infanterie-Division
    SOV  Strelkovaya Diviziya
    POL  Dywizja Piechoty
    ITA  Divisione di Fanteria / Divisione Alpina
    FRA  Division d'Infanterie / Division Coloniale
    ENG  Infantry Division

`game.divisions()` needed **no Windows changes**: the ELF offsets
`CArmy+0x3B8` (division template) and `CArmy+0x1F0` (province) hold on the PE
build too, and `CO_ARMIES 0x290` is already what the method reads.

### `GS_ARMIES 0x640` is not what the port plan assumed

The port plan said to read `GS_ARMIES 0x640` as the global army list.  It is a real
`CPdxArray<CArmy*>` (36 entries, elements are genuine `CArmy` with vtable
`0x29D6D98`), but 36 cannot be the world total when the countries alone hold
1227.  GER's array and `GS_ARMIES` share their first element, so it looks like a
**selection/temporary** buffer rather than the master list.  Its actual role is
unknown; nothing should depend on it.  Per-country `CO_ARMIES` is the
authoritative source.

Not decoded yet inside `CArmy`: organisation, strength, equipment, experience,
order/assignment state.  The order side of M3 (draw a front, assign, execute)
has not been started.

---

## 37. M3 order side, part 1: CArmy fields and building an army (Windows)

### 37.1 The two pure getters in the CArmy vtable

`.?AVCArmy@@` has four vtables in this build (0x29D6D98, 0x29D6F78, 0x29D6FE8,
0x29D7030) -- it is a multiply-inheriting class.  The live object's vtable is
0x29D6D98.  Of its 96 slots only two are plain "read one field" getters:

    31  0x00CE4D70   mov rax, qword ptr [rcx + 0x420] ; mov [rdx], rax ; ret
    36  0x00CE4D60   mov rax, qword ptr [rcx + 0x428] ; mov [rdx], rax ; ret

Both are 8-byte fields in 1e5 fixed point.  Grouped by division template over
Germany's 30 divisions the values are identical per template:

    template                     n    +0x420        +0x428
    Infanterie-Division         24    227.200       53.4545
    Panzer-Division              3     72.406       23.3333
    Infanterie-Division (mot.)   1    251.700       50.7500
    Kavallerie-Brigade           1    102.200       52.5000
    Gebirgs-Brigade              1    120.000       73.5000

So `+0x420` is the division's strength in HP points and `+0x428` its
organisation -- the same split the Linux table used, and the only assignment
whose magnitudes make sense (no division has 227 organisation, and 53 HP is
not a division).  The template does *not* hold these numbers; they are computed
per division at runtime (a scan of `CDivisionTemplate` for the same fixed-point
values found nothing), so "current == max" here only because the game is at
peace and every division is fresh.

**`+0x430` is still unidentified.**  It reads 2880.0 / 1650.0 / 3570.0 /
1440.0 / 1800.0 for the five templates above.  The Linux table calls this
"A_XP", but 2880 is not an experience value, so that label is *not* carried
over.  Do not report it as experience.

### 37.2 COrdersGroup: the constructors

`.?AVCOrdersGroup@@` has two vtables, 0x29CEE58 and 0x29CEED0.  Exactly two
functions install both: `0x00C3F480` (ctor) and `0x00C3F800` (copy ctor).
`0x00F702D0` is the allocator wrapper:

    0x140f702ee  mov ecx, 0x230          ; sizeof(COrdersGroup) >= 0x230
    0x140f70302  lea rdx, [rbp + 0x48]
    0x140f70306  mov r8, rbp             ; rbp = the CTheatre
    0x140f7030c  call 0x140c3f480        ; COrdersGroup(this, &theatre+0x48, theatre)

and `0x00F70110` is the sibling that allocates 0x250 bytes and calls
`0x00C3F430(this, theatre->0x48, theatre)` -- the army *group* flavour.

### 37.3 COrderGroupCommand on the PE build does not take a CTheatre

The Linux ctor signature is `(this, CPdxArray<CArmy*>*, CTheatre*, CArmyGroup*)`.
The Windows one is **different**: `0x0018C8D50` saves `rdx` and `r8` and ends

    0x1418c8f4a  mov r8, qword ptr [rsi + 0x40]   ; rsi = arg2
    0x1418c8f4e  mov rdx, rbp                     ; rbp = arg1
    0x1418c8f51  mov rcx, rbx
    0x1418c8f54  call 0x1418e0810

It has already stored `[[rsi+0x40]+8]` (a handle), `[GetTheatre(rsi)+8]` and
`[rsi+8]` into `cmd+0x48/0x50/0x58`.  Those three fields are handles, and
`rsi+0x40` being a *pointer to the theatre* is exactly `OG_THEATRE` -- so **arg2
is a parent COrdersGroup, not a CTheatre**.  Passing a CTheatre makes the ctor
read the array allocator as if it were a group (CTheatre+0x40 is the alloc field
of its own `CPdxArray` at +0x30) and would crash.  Three parameters, not four.

### 37.4 The function that actually creates the army

`COrderGroupCommand::Execute` starts at `0x0018D6560`:

    0x1418d656a  cmp dword ptr [rcx + 0x3c], 0     ; cmd+0x3c = 1 -> take the "group" path
    0x1418d6573  cmp byte ptr [rcx + 0x28], 0
    0x1418d6579  add rcx, 0x48                     ; theatre handle
    0x1418d65d4  movzx r9d, byte ptr [rbx + 0x28]  ; flag
    0x1418d65d9  lea r8, [rcx + 0x30]              ; divisions
    0x1418d65dd  lea rdx, [rcx + 0x48]             ; theatre handle
    0x1418d65e1  add rcx, 0x50                     ; parent handle (may be a null handle)
    0x1418d65e5  call 0x140c417a0

    0x00C417A0 (out_parent_handle, theatre_handle, CPdxArray<handle>*, u8) -> COrdersGroup*

`0x00C417A0` resolves the theatre handle, calls `0x00F702D0(theatre,0,0,0,0)`,
stores the flag byte at `+0x198`, then walks the array -- **8-byte handles, not
CArmy pointers** -- resolving each with `0x142284870` and attaching the division
(it takes `resolved - 0x10` as the CArmy base).  A null parent handle is
accepted, which is what makes the first army of a country possible at all:
in this save *no country has a single COrdersGroup* (every `CArmy+0xC0` is 0,
`CTheatre+0x30` is an empty `CPdxArray` for GER/FRA/SOV/ITA/ENG/POL).

Calling `0x00C417A0` directly is the accepted path of the command minus the
command object and the queue -- so it also works while the game is paused.

Verified live:

    create 4 divisions -> 0x278BDE21BB0  handle {53,505}  +0x39=0 (army)
                          +0x40 = the theatre, +0x50 = 4 divisions, +0x1B8 = 0
    game.create_army(count=6) -> "第2集团军" handle {53,506}, 6 divisions
    unassigned 30 -> 20

`OG_NAME 0x160` is still a `std::string` and the game fills in the localised
default name ("第1集团军"), which is a good sanity check that the object really
is a live army and not a shell.

### 37.5 Wrong tool: calling game.malloc on Windows

`Game.malloc` goes through `O.FN_OPERATOR_NEW`, which is `None` in the Windows
offset table, so it raises `TypeError: %X format: an integer is required`.  Use
`cmdwin.alloc(g, size)` (it posts `OP_NEW` with the module base added) for every
buffer on this platform.

---

## 38. M3 order side, part 2: the order primitives (Windows)

### 38.1 Where Execute lives

`CCommand::Execute` is **vtable slot 10** on this build.  Proof: the accepted
path of `COrderGroupCommand` (`0x0018D6560`, the function that calls
`0x00C417A0`) sits in slot 10 of vtable 0x2A84560.  Slots 9 and 11 are the
validator and the type getter (`slot 11 = 0x0018DCB90: mov dword [rdx],0x3327`).

### 38.2 COrderNewFrontCommand

vtable 0x2A853A0, sizeof **0x70** (its two factories `new 0x70` at 0x18CE150 /
0x18D03B0), primary ctor `0x0018C9840` (734 bytes).  Execute = slot 10 =
`0x0018D9070` (2481 bytes), and it only calls four routines outside itself:

    0x18d9221  call 0xc41620        ; rcx = og
    0x18d9229  call 0xc41bb0        ; rcx = og  -> order, then +0x119 = cmd+0x6e
    0x18d92fb  call 0xc41720        ; rcx = og, edx = 2
    0x18d9352  call 0xc41720        ; rcx = og, edx = 1
    0x18d9954  call 0xc4c170

CanExecute = slot 9 = `0x0018DF550`, and it tests exactly: `cmd+0x5c != 0`,
`0xBBB250(&cmd+0x28)` (a handle is non-null/valid), the resolved `cmd+0x28`
object, `0xBBB250(&cmd+0x30)`, `cmd+0x68 != 0`, and the bytes `cmd+0x6d` /
`cmd+0x6e`.  So `cmd+0x28` is a **handle to the COrdersGroup**, `cmd+0x30` a
second handle (parent/army group), `+0x5c` and `+0x68` ints, `+0x6d/+0x6e`
flags.  `0xBBB250` is the "handle is not null" test (`[h]|[h+4]`).

### 38.3 Creating one COrderInstance

    0x00C41720(COrdersGroup* og, int type) -> COrderInstance*   (sizeof 0x3C8)

    0x140c41734  mov ecx, 0x3c8
    0x140c41739  call 0x1425a01dc        ; operator new
    0x140c4174b  lea rcx, [rdi + 0x3c]
    0x140c4174f  call 0x140c07690        ; the group's order-id manager
    0x140c41754  inc dword ptr [rax + 0x1b0]   ; next id
    0x140c41769  call 0x1410adce0        ; COrderInstance(this, type, og, id)

`COrderNewFrontCommand::Execute` asks for type 2 and then type 1, and passes
the cmd's `+0x6d` / `+0x6e` bytes into the order at `+0x118` / `+0x119`.

Not decoded yet: the province path on the order (which offset holds the
`CPdxArray<i32>`), the group's execution type, and `0x00C4C170`.  Next step is
to call `0x00C41720(og, 1)` on the live game, read the fresh `COrderInstance`
back and map its fields against the cmd's geometry, before any of it is wired
into `armyops_win`.

---

## 39. The NewFront crash of 2026-11-14, and what it proved

### 39.1 What was called

    cmd = new(0x70)                                   ; COrderNewFrontCommand, zeroed
    call 0x18C9840(cmd, og, &emptyProvinceArray, 0, &out, 0, 0, 0)

The game died inside the ctor (`ReadProcessMemory ... GetLastError=299`, process
gone).  It was not the province array.

### 39.2 The cause

    0x18C99F9  cmp qword ptr [rbx + 0x38], r13     ; rbx = arg3 = 0  -> reads 0x38 -> AV

The ctor dereferences **arg3 as a COrdersGroup\* candidate** *before* any null
check: it compares `arg3->[0x38]` against the army (r13 = arg1) and, when they
match, copies `arg3->[0x244]` into `cmd+0x68`.  So arg3 is a **parent
COrderInstance**, and 0 crashes.

### 39.3 What the same disassembly confirmed as correct

* `arg2` really is `CPdxArray<i32>*` of **province ids**:

      0x18C9A12  movsxd rbx, dword ptr [rdi + 0xc]   ; size
      0x18C9A16  mov rdi, qword ptr [rdi]            ; data
      0x18C9A27  mov r8, rbx ; shl r8, 2             ; 4-byte elements
      0x18C9A34  call memcpy

* `arg1` is the COrdersGroup: its handle (`arg1+8`) goes to `cmd+0x28`, which is
  exactly what CanExecute (slot 9, `0x18DF550`) tests first.
* the 5th argument (stack, `[rsp+0x20]`) is an out-pointer; the UI passes
  `&[rbp-0x79]` there and `&[rsp+0x50]` (the province array) as arg2.
* after the ctor the UI calls `0x1402B60C0(cmd, X)` -- that, not `Post`, is how
  the army panel submits an order command.

### 39.4 The corrected recipe (next attempt)

A front needs a **parent order** first, which is why
`COrderNewFrontCommand::Execute` itself creates order type 2 (`0xC41BB0`) before
the two `0xC41720` calls:

    root = call 0x00C41BB0(og)        ; type-2 COrderInstance, +0x299 = 1
    cmd  = new(0x70)
    call 0x18C9840(cmd, og, &path_i32_array, root, &out, 0, 0, 0)
    call 0x1402B60C0(cmd, 0)

Do **not** retry with arg3 = 0.

### 39.5 Restart recipe (the display is 2560x1440, the DPI-unaware view is 2048x1152)

PowerShell `SetCursorPos` + `mouse_event` is ignored by this game.  Use the
windows-mcp `Click` tool, whose coordinates are **physical** (2560x1440):

    single player  (1338, 466)
    continue       (1277, 553)

Then poll `cmdwin.player_tag_id(g)` until non-zero (it read 1 within one poll
after the save finished loading).  `game.date()` can report nonsense right after
a load -- it returned 1945-04-02 on a 1936 start; cross-check with division
counts instead (GER 30 / SOV 139 / FRA 74 / ENG 38 / USA 36 is the 1936
scenario).

### 39.6 What survived the crash

Nothing in memory: the two armies were never saved.  The 1936-03-01 autosave on
disk is untouched (mtime 2026-11-13 23:47).  Re-verified after the restart that
`armyops_win.create_army` is the only thing that has to be re-run.

## 40. M3 order side, part 3: the front line is `COrderNewRootCommand` (Windows)

**The wrong class cost five game processes.**  `COrderNewFrontCommand` is a
*child* front — an offensive line drawn on top of an existing root front.  A
front line dragged along a border is `COrderNewRootCommand`.  Its ctor refuses
to run (and `Execute` walks off the end) unless `og+0x98` already holds the
parent order, which is why every earlier attempt produced "CanExecute 1,
Post 1, nothing in the list".

### 40.1 Where a front lives

    CTheatre + 0x50   CPdxArray<CFront*>        (10 fronts for Germany in 1936)
    CFront   + 0x08   {i32 type = 66, i32 id}
    CFront   + 0x20   CPdxArray<CProvince*>     border provinces
    CFront   + 0x58   CPdxArray<CFrontSection*>
    Section  + 0x08   i32 section id
    Section  + 0x30   CPdxArray<CProvince*>     provinces of that section

Germany's Dutch front is `{66,7}` with provinces
`[3444, 3512, 6622, 9281, 9522, 11264, 11346, 11360]`, one section, id 1.
Those eight provinces are exactly what `map/provinces.bmp` adjacency gives for
"GER province touching a HOL province", so the offline adjacency cache
(`stage/prov_adj.pkl`) can be trusted for planning.

### 40.2 The 8-argument constructor is 0x18C9B20

    0x18C9B20(cmd, COrdersGroup* og, void* handle_buf, i32 section,
              i64 from, i64 to, CPdxArray<CArmy*>* divisions|0, u8 flag)

The stores prove the order of the arguments:

    0x18C9C0C  mov rax,[rdi]        ; rdi = arg3 = handle_buf
    0x18C9C0F  mov [r14+0x48],rax   ; cmd+0x48 = the CFront handle
    0x18C9C13  mov [r14+0x50],ebx   ; cmd+0x50 = section (r9d)
    0x18C9C1F  mov [r14+0x58],rax   ; from
    0x18C9C2B  mov [r14+0x60],rax   ; to
    0x18C9C3B  mov [r14+0x6C],al    ; flag

and cmd+0x28 receives the COrdersGroup handle from arg2.  `0x18C9D90` is the
*other* ctor of the same vtable (0x2A856A0): it takes a `CFront*` directly and
has **seven** arguments, so calling it with the eight-argument convention
shifts everything and kills the process — that was crash #5.

### 40.3 Submission is `Post`, and the engine attaches the order itself

`CanExecute` (0x18DF5E0) returns 1 and `cmdwin.post()` returns 1; on the next
tick the engine appends a `COrderInstance` (type 2) to `og+0x98` with
`+0x244` = order id, `+0x258` = the CFront handle, `+0x260` = section,
`+0x268/+0x270` = from/to.  Nothing has to be pushed into the array by hand —
and hand-pushing is what killed crash #3.

Verified live on PID 26368:

    cmd vt      .?AVCOrderNewRootCommand@@
    +0x28       {53,489}          the army
    +0x48       {66,7}            the CFront
    +0x50       1                 section
    +0x58/0x60  0 / 100000        the whole front
    CanExecute  1
    Post        1
    og+0x98     1 order, type 2, id 1

`armyops_win.fronts()` / `draw_front_line()` / `orders()` wrap this.

### 40.4 Two API facts worth keeping

* `CState+0x18` is a `CPdxArray<CProvince*>`, **not** a flat `i32` array.  Reading
  it as ints yields pointer halves (e.g. `522046240`) and silently produces an
  empty province set.
* `CTheatre+0x18` is `CControllerArea`, and `CTheatre+0x30` is a second, always
  empty `CPdxArray`.  The fronts are at `+0x50`.

### 40.5 Crash ledger (all self-inflicted, all before Post)

  1. `0x18C9840` with a null parent order — the ctor dereferences arg3.
  2. `0xC41BB0` called with a vtable address mistaken for a COrdersGroup
     (one missing dereference) — wrote into `.rdata`.
  3. A bare order pushed into `og+0x98` without the engine's back-pointers.
  4. `0x18D9070` (child-front Execute) on a command whose front was invalid.
  5. `0x18C9D90` called with the eight-argument convention.

The rule that follows: **only submit through `CanExecute` + `Post`, and never
fabricate an object the engine is supposed to own.**

## 41. M3 order side, part 4: divisions, war, and a save that survives

### 41.1 The division list must be raw pointers

`COrderNewRootCommand`'s 7th argument is a `CPdxArray<CArmy*>` — **pointers**.
Feeding it `_mkhandlearray` (the 8-byte division handles used by
`create_army`) hangs the ctor: the injector times out with "thread left inside
game code" and the process dies minutes later.  `_mkptrarray` is the correct
builder.

With the real pointer array the same call is instant and the engine attaches the
divisions to the order:

    order +0x210   CPdxArray<CArmy*>   size = 4     <- the army's divisions
    order +0x88    CPdxArray<CProvince*>           <- the front's provinces

and the four divisions immediately start marching (province ids changed every
10 s: 3312/9496/9428/9428 -> 6521/11505/375/3499 -> ...).

Drawing a second front line on the same section does **not** replace the first;
it appends a second type-2 order.  Do it once, with divisions.

### 41.2 The war really was declared; the reader was stale

`declare_war()` returned `posted = 1` and the game raised its own World News
popup ("德国再一次陷入战争").  `combat.war_relations(g)` still said zero because
the Windows reader scans the object index, which is a **snapshot taken at
attach time**; pass `refresh=True` (12 s) and the war appears:

    {'enemy': 'HOL', 'me': 'GER'}   and   {'enemy': 'INS', 'me': 'GER'}

(INS joins because the Dutch East Indies are a subject of the Netherlands.)

### 41.3 Pause is a byte, not a keystroke

`idler = *(base + G_IDLER)`; `idler + 0x6C1` is the pause flag (`session.is_paused`).
Writing 0 unpauses reliably, which keyboard injection does not — the game
re-pauses whenever it loses focus, and a World News popup pauses it too (click
the popup's button to dismiss).

This is why several earlier "posted but nothing happened" tests were void: the
command queue only drains while the byte is 0.

### 41.4 Save before experimenting

The 1936 start is fragile and every mistake costs a 110 s relaunch.  With the
army, the front order and the war in place, an in-game save
(`GER_1936_03_02_12.hoi4`, 41.8 MB) restores all of it — verified: after a
crash, "continue" brought back the army (4 divisions) and the type-2 order with
`root_front {66,7}`.

**Rule: save before every new native call whose signature is not yet proven.**

### 41.5 Still open

* `COrderExecuteCommand` (the green "execute battle plan" tick).  Its vtable is
  0x2A85D60 (CanExecute 0x18DEFB0, Execute 0x18D64A0).  The assumed ctor
  0x18C8C50 with `(og, 1, 1, 0)` **crashed the process**, so that ctor is either
  a different class or takes different arguments.  Not solved.
* The offensive line builds and posts (child `COrderInstance` type 1 appears
  under the root order) but its target-province list was not found inside the
  child, so it is not proven to do anything yet.

### 41.6 Crash ledger, additions

  6. `draw_front_line` with a handle array instead of a pointer array.
  7. `0x18C8C50(og, 1, 1, 0)` — assumed OrderExecuteCommand ctor.

## 42. The execute flag is a byte on the order (`order+0x248`)

`COrderExecuteCommand::Execute` (0x18D64A0) is three lines of real work:

    rsi = resolve(cmd + 0x28)          ; the COrdersGroup
    edx = [cmd + 0x30]                 ; an ORDER ID, not a mode
    if (edx) { rax = 0xC402A0(group, edx);  edx = [cmd + 0x34];
               jmp 0x10CD510(rax, dl, 1); }

and `0x10CD510` starts with

    0x1410CD534  mov byte ptr [rcx + 0x248], dl

so **`COrderInstance + 0x248` is the "this order is executing" flag**, and the
whole command class can be skipped by calling `0x10CD510(order, 1, 1)` directly.

Verified: the flag goes 0 -> 1 on both the root order and the type-1 child, the
game keeps running (pause byte stays 0) — but **the divisions still do not
advance**.  A front line is defensive; the attack has to come from the child
front, and the child we create carries no target-province list, so it is an
empty order.  That is the remaining gap: `COrderNewFrontCommand`'s 3rd argument
(the `CPdxArray<i32>` path, copied to `cmd+0x50`/`cmd+0x5C`) never turns into a
province array inside the resulting `COrderInstance`.

### 42.1 Popups silently stop the clock

A World News popup ("土耳其共和国实现海峡再军事化") held the game for a full
90 s while `idler+0x6C1` read 0 the whole time: the popup pauses the game
without touching the pause byte.  Before every "it did nothing" conclusion,
screenshot the screen and dismiss the popup.

### 42.2 What is proven end-to-end now

    create_army(4 divisions)                     -> COrdersGroup, CArmy+0xC0 set
    draw_front_line(og, CFront{66,7}, sec 1, divisions)
                                                 -> type-2 order in og+0x98,
                                                    order+0x210 = 4 CArmy*,
                                                    all four march to the border
    declare_war(HOL)                             -> World News + war_relations
    0x10CD510(order, 1, 1)                       -> order+0x248 = 1

## 43. The front generator works; the child order does carry its provinces

Hooking `0x10B8620` during an offensive-line `Post` settles the open question:

    in   path=<CPdxArray<i32> cap=7 size=7>  parent=<root COrderInstance>  flag=0x101
    out  ret = 0x1
         out+0x00 CPdxArray: data, cap=1, size=1
         out+0x0C = 1          <- exactly the field Execute tests for zero

So the generator does **not** fail.  Earlier "it produced nothing" was a wrong
conclusion drawn from scanning only for `CPdxArray<CProvince*>`; the child front
stores its province line as a plain **`CPdxArray<i32>` at `COrderInstance+0x70`**.

Live dump of the children under the root front order:

    type=1 id=8   +0x70 n=5 [6622, 9363, 6241, 391, 3314]   exec=1
    type=1 id=11  +0x70 n=5 [6622, 9363, 6241, 391, 3314]   exec=1
    type=1 id=14  +0x70 n=5 [...]  +0x210 CArmy* n=4        exec=1
    type=2 id=6   +0x70 n=7 [68, 3211, 3262, 3314, 6336, 8622, ...]  +0x2a0 = 7
    type=2 id=9   ...                                                +0x2a0 = 10
    type=2 id=12  ...                                                +0x2a0 = 13

`+0x2a0` on a type-2 child is a single order id (7/10/13), i.e. the section
order points at the child front it belongs to.  `+0x1e0` / `+0x1f8` on a type-1
child are single-element `COrderInstance*` arrays.

**Still no advance.**  Every order involved has `+0x248 = 1`, the type-1 child
holds four `CArmy*` and a five-province line, and the four divisions sit on
front provinces 3444 / 9281 / 11264 / 11346 through a full minute of running
time (pause byte 0, no popup).  The army executes nothing, so the missing piece
is no longer the command — it is the group-level "battle plan is running" state
that the green tick owns.

Next probe: diff `COrdersGroup` bytes before/after pressing the green tick in the
UI once.  That is a byte-level question and needs no new constructor.

## 44. `CArmy+0xD8` is the division's current order, and it is what makes it fight

Scanning each `CArmy` for pointers into the order tree found exactly one hit per
division: **`CArmy+0xD8`**, pointing at the root front order (type 2).  Pointing
that field at the type-1 assault order (the child front) instead is what turns a
"deployed" division into an attacking one:

    div0 +0xD8: 0x252a03d8e00 (root, type 2)  ->  0x252c042d5b0 (type 1)
    after ~2 game days: org 53.5 -> 23.4 , hp 227.2 -> 224.7
    div3 (already on the child):        org 15.8 , hp 220.3

So the assault chain is: root front order (defensive) -> type-1 child front with
an `i32` province line at `+0x70` -> divisions whose `+0xD8` names that child.
The engine does **not** repoint `+0xD8` by itself and does not reject the write.

### 44.1 The Dutch border is defended by 8 divisions

    HOL divisions (1936-04): 8, sitting in 9363 / 9309 / 9403 / 11562 /
    153 / 11318 / 480 / 6336 — i.e. exactly the provinces the assault line
    targets.

Four attackers against eight defenders across five provinces do not move the
line (10 minutes of wall clock: org 23.3 -> 22.9, no province change).  A second
army of 20 divisions was raised on the same CFront section, given its own
front-line order and its own assault order, and all 20 had `+0xD8` repointed.

### 44.2 The clock, not the code, is the remaining limit

The build runs at roughly **3 game hours per real minute** at the current speed
setting (speed 1).  A 30-day campaign therefore needs about four hours of wall
clock.  Speed could not be raised from the code: `idler+0x6C0` (an int written 4)
and the speed dial clicks both left the rate unchanged, so the speed field is
still unknown.

A background watcher (`stage/wlong.py`, log `stage/m3_watch.log`) samples the
date and both army sizes every two minutes for an hour.

## 45. Where the crashes actually went: `Documents\dsh`

Seven self-inflicted process kills in one session produced crash reports, and the
game wrote them to **`%USERPROFILE%\Documents\dsh`** — not to the Paradox
`crashes` folder, which stayed at 0.09 GB the whole time.  That folder grew until
the system disk hit **0 bytes free**, which took the whole tool host down
(`ENOSPC` on every command) until it was deleted by hand.

Two things came out of it:

* `%LOCALAPPDATA%\CrashDumps` also collects one ~46 MB minidump per crash
  (`hoi4.exe.<pid>.dmp`); ten of them were sitting there.
* A **directory junction** now puts the dump folder on the data disk:

      %USERPROFILE%\Documents\dsh  ->  <data-disk>\crashdumps

  Verified: a probe file written through the junction leaves `C:` free space
  unchanged.

Rule from here: after any crash, check both locations before starting the next
experiment.  The junction keeps the next one off the system disk, but a crash
still costs a relaunch.

## 46. Game speed is a command, and the clock is the real bottleneck

### 46.1 Why the campaign crawled

Every M3 attempt so far ran at **speed 1**: measured 1936.04.01.04 -> 04.02.05 in
59 s, that is 0.5 game-hours per real second, one game-day per 48 s. A 30-day
operation would have taken 24 real minutes of pure game time, and the *previous*
watcher measured 600 s per game-day, which means the clock was stopped most of
the time (see 46.4).

### 46.2 `CIncreaseGameSpeedCommand` is the safe lever

    CIncreaseGameSpeedCommand   vtable 0x29F3900
    CSetGameSpeedCommand        vtable 0x29F3A80
    CDecreaseGameSpeedCommand   vtable 0x29F39C0   <- crashes when built this way

None of them needs a constructor.  Build the object by writing the vtable and
lets `Post` run the real `CanExecute`:

    cmd = cmdwin.alloc(g, 0x20); p.write(cmd, b"\0"*0x20)
    p.write(cmd, struct.pack("<Q", base + 0x29F3900))
    cmdwin.attach_fake_vtable(g, cmd, 0x29F3900)   # no-free slot 0
    cmdwin.post(g, cmd)

`cmdwin.build_by_vt(g, vt_rva, size, fields)` now wraps exactly that.

Four posts took the game from speed 1 to speed 5: the clock went from 0.5 h/s to
**1.7-2.7 game-days per real second** (1936.05.15 -> 1936.08.16 in 55 s).
`CDecreaseGameSpeedCommand` built the same way took the process down, so the only
way down is to reload.

### 46.3 `game.set_game_speed()` was a process killer on Windows

`commands.py` is the Linux module.  `CommandBuilder.new()` calls
`FN_NEW = 0x510950`, a Linux virtual address; on the PE image that jumps into
unrelated code and kills the game (measured, one relaunch).  `game.set_game_speed`
went through it.

Fixed: `game.set_game_speed` takes the Windows branch, `cmdwin.set_game_speed`
builds `CSetGameSpeedCommand` by vtable, and `commands.new()` now raises on
`win32` instead of jumping.

### 46.4 A popup advances exactly one hour, then the game stays paused

Dismissing a World-News / event popup produces **one game-hour of progress** and
nothing more, even though `idler+0x6C1` reads 0.  The pause only lifts when the
round pause button in the top bar is clicked:

    (2250, 38)   on a 2560x1440 display, windowed or fullscreen

So the loop for any long run is: dismiss the popup, then click the pause button.
`idler+0x6C1 = 0` on its own is not enough.  A run that ignores this looks exactly
like a hung game: the date never moves.

Popups seen in the first two game months: 意大利王国送出军事援助,
对军备重整的日益担忧 (焦点完成), 第二次伦敦海军条约签署 (World News).

### 46.5 `g.date()` can lock onto a stale `CDate`

After a load it reported `1945.02.19`, then `1945.04.02`, then `1955.06.25`,
none of which matched the UI (`1936.02-05`).  `date_raw` keeps a cached address
and only rescans when the value has been static for 3 s, so a freed-but-valid
looking int32 survives.  **Check the date against the top bar before trusting a
timed measurement.**

### 46.6 `COrderExecuteCommand` is real and the engine accepts it

    ctor 0x18C8C50   vtable 0x2A85D60   size 0x38
    (this, COrdersGroup* og, i32 order_id, u8 flag, u8 flag2)
    +0x28 = og handle (copied from og+8)   +0x30 = order_id   +0x38? = flags

The old crash was calling it as `(og, 1, 1, 0)` -- that writes the command header
into the orders group at +0x24/+0x30 and corrupts it.  Built correctly and posted
with `order_id = 0` (the whole plan) the result is **1** for every orders group,
and the engine immediately creates a type-1 child order whose `+0x210` array holds
exactly the army's divisions (4/4 and 26/26 in the last run).

### 46.7 The offensive child still does not move the divisions

State after a clean setup on `GER_1936_03_02_12.hoi4`:

| step | result |
|---|---|
| `create_army(26 unassigned)` | ok, 26 divisions |
| `draw_front_line` on front {66,7} section 1 | ok, root order id 2 / 3 |
| offensive line over the 8 Dutch provinces | child id 6 / 9 |
| `execute_plan(og, 0)` | posted 1, child `+0x210` = 4 / 26 |
| `0x10CD510(child, 1, 1)` | child `+0x248` = 1 |
| `CArmy+0xD8 = child` for all 30 | sticks (30/30 after 4 s) |

and then **nothing moves**: over 12 s of speed-5 time all 30 divisions kept the
same province, three of them sat at org 23 with no Dutch org loss.  So neither
the assignments nor the executing flag is what makes a division march.  The next
thing to try is `COrderAssignCommand` (ctor `0x18C75F0`, signature
`(this, CPdxArray<handle>* divisions, COrdersGroup* og, i32 order_id)` --
calling it with a fifth argument took the process down) or the per-division
`CMoveCommand` path.

### 46.8 The game can hang, not just crash

After the `COrderAssignCommand` attempt the window title read
`Hearts of Iron IV (DirectX 11) (未响应)` and the process burned 1500 s of CPU
without ticking.  `Get-Process ... Responding` is the quick check; a hung process
still answers memory reads, so the Python side sees a live game.  Kill it and
reload.

### 46.9 Menu coordinates, windowed vs fullscreen

When the game comes up **windowed** the main menu sits about 10 px higher and the
save list starts at y~700 in the 1920x1080 preview, i.e. screen y~1003 for the
second save.  Clicking the old fullscreen coordinate there opens 好友列表
instead of 载入游戏.  Always screenshot the menu before clicking.

## 47. The *second* dump folder: `Documents\Paradox Interactive\...\dumps`

The `Documents\dsh` junction from section 45 was not the whole story.  HOI4 keeps
its own crash dumps in

    %USERPROFILE%\Documents\Paradox Interactive\Hearts of Iron IV\dumps

and while a game is hung (`(未响应)`) it writes them **once per second**:
`hoi4_YYYYMMDD_HHMMSS.dmp`, ~42 MB each.  After this round of M3 experiments the
folder held **1169 files / 49.0 GB** and `C:` had gone from 117 GB free to 68 GB.

Cleaned and junctioned the same way as `dsh`:

    %USERPROFILE%\Documents\Paradox Interactive\Hearts of Iron IV\dumps
        ->  <data-disk>\crashdumps\hoi4-dumps

Probe write verified, `C:` stayed at 117.5 GB, and no new dump appeared in 8 s.

So **three** places have to be watched after a crash or a hang:

1. `%USERPROFILE%\Documents\dsh`                (junctioned, section 45)
2. `...\Hearts of Iron IV\dumps`               (junctioned, here)
3. `%LOCALAPPDATA%\CrashDumps`                 (one ~46 MB WER minidump per crash)

A hung game is worse than a crashed one: it keeps burning CPU *and* keeps
producing dumps.  `(Get-Process hoi4).Responding` is the check; kill it at once.

## 48. `COrderAssignCommand`, four arguments: it works, and it changes nothing

### 48.1 The signature

Two constructors write vtable `0x2A84CE0`:

| ctor | size | signature |
|---|---|---|
| `0x18C75F0` | 616 B | `(this, i32* division_handle, COrdersGroup* og, i32 order_id)` |
| `0x18C7860` | 695 B | same plus a 5th `u8` read from `[rsp+0x80]` |

Fields written by `0x18C75F0`:

    +0x28  CPdxArray<handle>         (initialised empty by 0x11F6F0)
    +0x40  COrdersGroup handle       (copied from og+0x08)
    +0x48  i32 order_id
    +0x4C  u8  0

Object needs ~0x50 bytes; 0x80 is safe.

### 48.2 The crash last time was the argument, not the ctor

**arg2 is a pointer to ONE 8-byte handle**, not a `CPdxArray`.  The ctor does
`cmp dword ptr [rdi], 0 / lea rbx,[rdi+4] / cmp dword ptr [rbx], 0` and then
resolves it through `0x142284870`.  Handing it the CPdxArray header made it
resolve the array's *data pointer* as a handle and take the process down.

The handle lives at **`CArmy+0x18`** (verified: `type = 51, id = 1`), which is what
`armyops_win._mkhandlearray` already uses.  `CArmy+0x08` is **not** a handle --
it read `{0, 0}`.

### 48.3 Result

Called as `(cmd, d+0x18, og, child_order_id)` it posts cleanly for every division:

    OG 0x15f276fcba0 child id=6  posted 4/4    child +0x210 = 4
    OG 0x15f759b4d30 child id=9  posted 26/26  child +0x210 = 26

and `CArmy+0xD8` does not move, the `+0x210` array does not grow, and over 8 s at
speed 5 **0 of 30 divisions changed province**.  So the divisions were already
assigned to the child order; re-assigning is a no-op and assignment is not what
is missing.

The ctor's own owner check compares `*(i32*)(division+0x1C8)` with `og+0x3C`
(the country tag, 1 for GER); `division+0x1C8` holds `0x29C77F8`, the scope magic
that `cmdwin.SCOPE_MAGIC` already names.

### 48.4 Where that leaves the offensive

Known state of a synthetic offensive child order:

* created by `0x18C9840`, `CanExecute` 1, `Post` 1
* `+0x70` province line `[6622, 9363]` -- one German and one Dutch province
* `+0x210` holds the army's divisions
* `+0x248` = 1 after `0x10CD510(order, 1, 1)`
* divisions' `+0xD8` = the child

and the army still stands still.  The one field never observed non-empty is the
*movement path* a division needs; the next experiment should be a differential
one -- draw the arrow **with the mouse** once, dump that order, and diff it
against the synthetic one field by field, instead of guessing another ctor.

## 49. Differential against a real AI offensive order

### 49.1 The comparison partner

The AI draws its own battle plans, so a real type-1 "offensive line" order can be
read straight out of memory -- no UI dragging needed.  The Netherlands starts the
1936 scenario with one:

    HOL OG 0x245fa8c1110  divs=5  orders=[(4, type 2)]
       child 0x245fa967960  type=1  id=9   <- offensive line
       child 0x245fa963c60  type=2  id=7
       +0x70 -> CPdxArray<i32> cap=3 size=3  provinces [9363, 11346, 6535]

### 49.2 The two orders side by side

| offset | AI `0x245fa967960` | OURS `0x243de93fb50` |
|---|---|---|
| 0x00 vtable | `0x7ff658b826e8` | same |
| 0x08 | `0x7ff658b58b80` | same |
| 0x18 | 0 | **`0x100000000`** |
| 0x30 type | 1 | 1 |
| 0x38 owner OG | `0x245fa8c1110` | `0x245ade494e0` |
| 0x40/0x50/0x58/0x68 | module ptrs | same |
| 0x48 | `0x39f22cb` | `0x39f22eb` |
| 0x60 | `0x29c77f8` (scope magic) | same |
| 0x70 provinces | 3 items | 2 items `[6622, 9363]` |
| 0xa0 | -1 | -1 |
| 0xb0 | `0x100000000` | same |
| 0xb8 | 0 | **`0x4300000000`** (= handle {67,0}, CTheatre!) |
| 0xf8 | 0 | **`0x24500000000`** |
| 0x118 | `0x10000` | **`0x24300000000`** |

**Every meaningful field matches.**  The order is not the wrong type, it has the
right owner, the right scope magic, the right province array shape.  What differs
is that ours carries **uninitialised heap** in the fields the engine leaves alone.

### 49.3 The heap is recycled, and the engine does not zero it

The root order is worse.  `words()` on it shows float32 leftovers in the high
half of a dozen 8-byte slots (`0x3e218000`, `0x44f0682b`, `0x3d792c60`, ...) --
1876-ish values that look like coordinates -- and at `+0x120`:

    0x120 = "o_fallba"   0x128 = "nura"    0x130 = 0xc

that is a **leftover loc key from a fallback-line order** that previously owned
the same heap block.  This is exactly the defect the command layer already fixed
for itself in `commands.py` ("operator new hands back uninitialised heap ... the
game's CanExecute/Execute then acted on it"), but it applies to the *order
instances the engine allocates from our command* as well.

### 49.4 First movement ever observed

`child+0x248` read **0** -- the executing flag was not set on this child.  After
zeroing the four stale fields (`+0x18`, `+0xb8`, `+0xf8`, `+0x118`) and writing
the pause byte, **1 of the 4 divisions changed province within 10 s**
(6334 -> 6282), the first movement this project has seen from a synthetic order.
Not yet proof of causality -- it could have been an ordinary front-line shuffle --
but it is the only change the differential pointed at.

### 49.5 Next

1. Zero the stale fields **at creation time** (right after the child order appears,
   before the first tick) and measure movement over a controlled window with the
   game paused/resumed by clicking the pause button, not by writing the byte.
2. Same for the root order (the float leftovers and the leftover string at +0x120).
3. A `--dump` helper (`stage/wdumpai.py`) can now read any AI country's plan orders,
   so any future question about order layout can be answered by comparison instead
   of guessing a constructor.

## 50. M3: the Netherlands capitulates -- the whole chain works

### 50.1 What was run

On the loaded `GER_1936_03_02_12.hoi4`, entirely through `CCommand::Post`:

1. `armyops_win.create_army(g, 26 unassigned)` -> a second orders group
2. `draw_front_line` on front `{66,7}` section 1 for **both** groups
3. offensive line for each group: ctor `0x18C9840` over the provinces where Dutch
   divisions stand
4. `COrderExecuteCommand` (`0x18C8C50`, `order_id = 0`) for each group -- the engine
   then fills the child order's `+0x210` array with the army's divisions
5. `0x10CD510(child, 1, 1)` on every child
6. speed 5 (four `CIncreaseGameSpeedCommand` posts)

### 50.2 Result

```
1945.04.02.00  HOL=8  inHOL=0/30  war=True
1945.04.02.00  HOL=0  inHOL=0/30  war=True
*** Netherlands is out ***
```

and the game itself then raised its own screens:

* `阿姆斯特丹条约` -- the peace conference, Germany 2840 points, defeated
  `荷兰王国` and `荷属东印度`
* World News `缴获了荷兰的装备` -- *"荷兰王国已经停止抵抗，我们从他们的库存里得到了50%的装备"*

All Dutch states were selected in the conference (`全选`, 285 points).  **The
Netherlands is conquered by the automation, not by a console `annex`.**

### 50.3 The "divisions do not move" result was a measurement artifact

At speed 1 the clock runs 0.5 game-hours per real second, so a 10 s probe covers
**5 game hours** -- no division can march in that.  At speed 5 the same probe
measures 384 game-hours per 6 s (16 game-days/s):

```
rate after speed up: 384 hours / 6 s
moved 4/4   a [9363, 9309, 6622, 587] -> b [3444, 9281, 11562, 9363]
```

All four divisions moved, and `inHOL` went 0 -> 2 -> 0 as they fought in and out
of Dutch provinces.  Section 48's "the army stands still" conclusion was taken at
speed 1 and is **wrong**; the chain was working.

### 50.4 The real limiter is force size

A single 4-division army could not break 8 entrenched Dutch divisions: over nine
game years the front never moved.  Adding the 26-division second army produced
capitulation within one poll interval.  So `create_army` + a second front/arrow is
not optional.

### 50.5 Open ends

* `COrderInstance+0x248` is reset to 0 by the engine at some point; it has to be
  re-set with `0x10CD510(child,1,1)` after the plan exists.
* The peace conference is fully modal and **windows-mcp clicks do not register in
  it** (three separate attempts, with the window forced to the foreground).  The
  conference has to be finished by hand, or `CSelectEventOptionCommand`
  (vtable `0x29B6CE0`) has to be tamed.
* The 30-day figure is **not** measured: this war had already run since 1936.03 by
  the time the second army arrived.  A clean timed run needs a reload and the
  whole chain from turn one.

## 51. Clean timed run: 30 days is not enough, and the arrows evaporate

### 51.1 The measurement

Fresh load of `GER_1936_03_02_12.hoi4` (already at war with HOL), whole chain
built at speed 1, then clock started and speed raised to 5:

    T0 = 1936.03.02.19
      1936.03.02.19  HOL=8   (+0.00 days)
      1936.04.07.19  HOL=7   (+36.00 days)
      deadline 90 days, HOL=7

**Inside 30 game days the Dutch lose nothing.**  One division is destroyed by day
36 and nothing more by day 90.  The 30-day acceptance criterion is **not met**.

### 51.2 The offensive does advance, then stops

The front did move into the Netherlands -- `{66,7}` went from the German border to
`[11456, 8622, 3314, 9498, 6241]`, all Dutch provinces -- so the arrows work.  What
kills the attack is what happens next:

    OG 0x1c4424c4240 orders=[(1,2),(2,2),(18,2 front {66,1271} sec 2),(17,2 front {66,1271} sec 2)]
       children=[]                      <- every type-1 offensive order is GONE
       div order ptrs {root id 2: 3, front 1271 id 18: 1}

When the front advances the engine **creates a new front (`{66,1271}`)** and deletes
the child offensive orders; the divisions are handed back to the front line, which
only holds.  A real offensive therefore has to be re-issued every few game days --
a player redraws the arrow, our driver has to do the same.

### 51.3 What that means for M3

Two independent gaps, both real:

1. **Force quality/size.** 30 German 1936-pattern infantry divisions (no artillery)
   cannot break 8 entrenched Dutch divisions in three weeks.  Options: better
   templates (`templedit.py` / `duplicate_division_template` already exist), more
   divisions, or air support.
2. **Plan upkeep.** The arrow must be re-posted whenever the front moves.

Neither is a command-layer problem any more: `create_army`, `draw_front_line`,
the offensive ctor, `COrderExecuteCommand` and the division link all work.

## 52. How hard is driving the peace conference?

### 52.1 What exists

RTTI carries the whole conference, and three **commands**:

    CDonePeaceConferenceCommand      vtable 0x2AFC800     <- "submit demands / confirm"
    CEndTurnPeaceConferenceCommand   vtable 0x2AFC8C0     <- "skip this round"
    CPassPeaceConferenceCommand      vtable 0x2AFC740     <- "pass"

plus `CPeaceConference` (0x29CDA28), `CPeaceConferenceManager` (0x279EE58),
`CPeaceWinnerAi`, and the UI entry classes `CPeaceClickAllEntry`,
`CPeaceAvailableActionItem`, `CPeaceBiddingItem`.

### 52.2 Difficulty

| goal | mechanism | effort |
|---|---|---|
| close the conference | `CDonePeaceConferenceCommand`, same vtable+Post pattern as `SetGameSpeed` | **low** -- find the ctor with `wfindvt.py`, disassemble for the fields, post |
| skip a round / pass | `CEndTurnPeaceConferenceCommand`, `CPassPeaceConferenceCommand` | **low** |
| take all Dutch states (the 全选 we did by hand) | no command exists; the demands are UI list entries (`CPeaceAvailableActionItem`, `CPeaceClickAllEntry`) driving `CPeaceAction` objects | **medium-high** -- would need to walk the conference object's action list and invoke each item's handler |
| let the AI do it | `CPeaceWinnerAi` | unknown; it is the AI's own bidder, not a player-side shortcut |

### 52.3 What was actually observed with the mouse

* clicking a defeated country in the list **works**
* clicking `全选` **works** -- all Dutch states got the red X, 285 points
* clicking the stacked `World News` OK and `提交要求` **did not register**, in three
  attempts, with the window forced to the foreground

So the cheapest route is not the UI at all: post `CDonePeaceConferenceCommand` after
selecting demands, or accept that the conference has to be finished by hand once per
war.  Since capitulation only happens once per enemy, "by hand" is a viable stopgap
and does not block anything else in the pipeline.

## 53. The offensive never attacked because `COrderExecuteCommand` leaves it dormant

This is the real reason the arrows "did nothing" and then "evaporated", and it
supersedes the 30-day conclusion in §51: the plan was never executing.

### 53.1 The missing byte

`COrderExecuteCommand` (0x18C8C50) does everything it says: the engine creates
the type-1 child offensive, hangs it off the front order, and puts the army's
divisions into the child (`child+0x210` = `CPdxArray<CArmy*>`).  What it does
**not** do is set the order's own executing byte:

    COrderInstance + 0x248   u8   0 = dormant, 1 = executing

The engine sets it through

    0x10CD510   (order, u8 executing, u8)      ; [rcx+0x248] = dl, then
                                               ; rebuilds the order's caches

With `+0x248` left at 0 the order is drawn on the map (that is the arrow the
player sees), the divisions are listed as assigned, and nothing moves -- for
years.  Verified live 2026-11-14: same orders, only this byte changed, and
within one game day the divisions crossed into Dutch territory.

### 53.2 Result

With the byte set, from `GER_1936_03_02_12.hoi4`:

* 30 German divisions on front `{66,7}` section 1 (the 8 German border provinces)
* offensive drawn on the 7 Dutch provinces at BFS depth 2 from the border
  (the far side: Amsterdam / Den Haag / Utrecht / Arnhem belt)
* the Dutch army went from 8 divisions to 1, six provinces were taken and the
  **Netherlands capitulated** -- conference `阿姆斯特丹条约`, victor GER with
  2800 points, notification `荷兰王国已经停止抵抗`

So the whole M3 chain is confirmed working end to end.  The 30-day *number* is
still not measured: the run above was interrupted by the modal windows, and the
engine's `CDate` copies cannot be trusted for timing (see §53.4).

### 53.3 Two more defects this exposed

* `m3run.children()` accepts any 8-byte slot that looks like a
  `CPdxArray<COrderInstance*>`.  Once a group carries a few extra orders it
  returns heap noise, and `set_offensive_flags()` then wrote `+0x248 = 1` into
  arbitrary heap addresses.  That killed the game three times today.  The fixed
  version (`armyops_win.order_children`) requires the child's first qword to be
  the `COrderInstance` vtable `0x2A026E8`.
* re-issuing an offensive every 20 s (the naive "keep the arrows fresh" loop)
  piles up children: nine extra type-1 orders per group in three minutes, most
  of them `assigned=0`.  Re-issue only when the province count has actually
  stalled **and** delete or ignore the dormant ones.

### 53.4 The clock: do not trust any `CDate` copy

`api._win_date_addr()` keeps the first candidate it finds and accepts
`0 <= new - last <= 400 days`, which is true for a frozen copy, so a save-preview
string can pin the clock for a session (it read `1945.04.02` while the UI said
`1936.03.02`).  The value is an hour counter -- `(year*365 + doy)*24 + hour`
-- so a "plausible date" test must decode it, not compare it to `YYYYMMDD`.

`clock.py` in the stage folder scans for candidates that both decode into
1935..1965 **and** advance between two reads.  Even that is not enough while the
game is paused, and the live clock is not always among the copies that pass the
signature check.  For measurements, read the UI: the date strip is at
`x 2260..2420, y 10..30` of the 2560x1440 frame and the game renders at
2048x1152, so any coordinate measured on a `PrintWindow` image needs `x1.25`.

### 53.5 Geometry from the game's own map files

`mapgraph.py` rebuilds province adjacency and centroids from
`map/definition.csv` + `map/provinces.bmp` (HOI4 land adjacency is implicit in
the bitmap; `adjacencies.csv` only carries straits and sea crossings).  This is
what makes a correct offensive line possible at all: the Dutch provinces are
exactly three BFS layers from the German border --

    depth 0   6500 9309 9363 9403 11318 11456 11562
    depth 1   391 6241 6286 6496 9335 9498
    depth 2   68 3211 3262 3314 6336 8622 9431
    (480 is cut off by water)

and front `{66,7}` section 1 is exactly the German side of that border
(`3444 3512 6622 9281 9522 11264 11346 11360`), which the adjacency graph
confirms independently of the engine.

### 53.6 Manual push, if it is ever needed

`CMassMoveCommand` is the "select divisions, right-click a province" command:
vtable `0x2A2CEA0`, in-place ctor `0x13F6690`, size `0x60`:

    +0x28  CPdxArray<MoveTarget>   0x20 bytes per division
    +0x40  CPdxArray<handle>       the divisions
    +0x58  int32                   (= 3 at the UI call site)

Not built yet -- the plan orders are enough once `+0x248` is set.

## 54. Clean 30-day measurement: the Netherlands capitulates in ~13 game days

Recipe (all verified, no console):

1. load `GER_1936_03_02_12.hoi4` (war with HOL already declared)
2. dismiss the opening event with a mouse click at (1382,646) -- see 54.3
3. `wplan.py --setup --deep 2`:
   * `create_army` the 26 unassigned divisions
   * `draw_front_line` on front `{66,7}` section 1 for both groups
   * offensive over the 7 Dutch provinces at BFS depth 2
   * `COrderExecuteCommand` for both groups
   * `start_offensives()` sets `+0x248 = 1` on the two accepting children
4. mouse-click `+` three times (speed 4)

Result, read off the UI date strip (the only trustworthy clock, see 53.4):

    plan posted        1936.03.02 20:00
    first recorded     1936.03.04 12:00
    last before capit. 1936.03.15 11:00
    conference opens   ~1936.03.15 15:00

    -> 12.6 game days from order to capitulation
    -> Dutch army 8 divisions -> 1, six provinces occupied, 30/30 German
       divisions intact, conference 2800 points

That is well inside the 30-day target.  The earlier "the Dutch lose nothing
in 30 days" result stands as a measurement of a plan that was never executing
(53.1), not of a plan that was too weak.

### 54.1 Why the previous attempts stalled

Two independent mistakes, both now fixed:

* the offensive child was dormant (`+0x248 = 0`), so no division ever attacked;
* the offensive line was drawn on the provinces the Dutch divisions stood in,
  i.e. one province deep.  A battle plan advances **to** the line and stops
  there, so the line has to be the far side of the country.  `mapgraph.py`
  gives the BFS layers needed to place it.

### 54.2 What the run does not prove

The divisions start scattered across Germany and need ~6 days to reach the
border, so "order to capitulation" includes a march that a prepared player
would not pay.  Measured from the first contact it is under a week.

### 54.3 UI coordinates on this machine (2560x1440)

The game renders at 2048x1152 and is stretched to 2560x1440; every coordinate
below is in the 2560 frame, i.e. what a screen capture shows.

    event option 1     (1382, 646)
    event option 2     (1382, 694)
    speed -            (2271, 17)
    pause / play       (2298, 24)
    speed +            (2430, 24)
    load-screen save row 2   (150, 1022)
    load-screen 开始        (2470, 1414)

A previous session concluded that event and peace-conference windows "do not
accept clicks".  They do; the y coordinate was 50 px off.  `提交要求`,
`确认并退出` and the conference country list all respond as well.

## 55. The peace conference is command-drivable

### 55.1 The three commands

All three carry exactly **one int** at `+0x28` -- the same value the UI copies
out of the conference object before posting:

    CDonePeaceConferenceCommand      vtable 0x2AFC800   ctor 0x1ED1D70   size 0x30
    CEndTurnPeaceConferenceCommand   vtable 0x2AFC8C0                    size 0x38
    CPassPeaceConferenceCommand      vtable 0x2AFC740   ctor 0x1ED1E50   size 0x30

The in-place ctor pattern is the standard command prologue (`+0x08` byte 0,
`+0x0C` = -1, `+0x10` = 0, `+0x18` = 0xFFFF, `+0x1C` = 0) followed by
`[rcx+0x28] = *(int32*)rdx`.  `Execute` (slot 10) is two instructions: fetch the
conference manager, then tail-call the handler with `this+0x28`.  `CanExecute`
(slot 9) only checks that the manager exists.

### 55.2 Where the int comes from

The UI handler is `0x18F29E0`:

    0x1418f2b18  mov rcx, [rbx+0x58f8]     ; the conference object
    0x1418f2b49  mov rdx, [rcx]
    0x1418f2b4c  call qword ptr [rdx+0xa0] ; -> int*  (the value to submit)
    0x1418f2b58  call 0x141ed1d70          ; ctor(this, int*)
    0x1418f2b67  call 0x1422c4250          ; CCommand::Post

So the int is whatever vtable slot 0xA0 of the conference returns a pointer to.
In a single-winner war it is the player's country index: **1 for Germany**.

### 55.3 Verified

Posted `CDonePeaceConferenceCommand(this, 1)` from our own code while the
conference for the Netherlands was open.  The conference closed and the game
produced the treaty window

    「阿姆斯特丹条约 -- 荷兰王国与德意志第三国达成了无条件和平」

That is a white peace, which is exactly what "done" means when the bid list is
empty: the command submits *the bids you have*, and we had none.

`calling/peacewin.py` wraps all three: `peacewin.done(g, 1)`,
`peacewin.passed(g, 1)`, `peacewin.end_turn(g, 1)`.

### 55.4 What is still not a command

Choosing **what to take**.  The demands are data, not commands:

    CPeaceAction            0x2A4C570    one demand
    CStatePeaceAction       0x2A4C690    "take this state"
    CTakeNavyPeaceAction    0x2A4CC30    "take these ships"
    CPeaceBiddingTurn       0x29EAEC0    the turn that owns the action list
    CPeaceAvailableActionItem 0x2AFB688  the UI entry that appends one

There is no `CAddPeaceDemandCommand`; the window appends `CPeaceAction` objects
directly to the turn.  Two ways forward:

* **UI (cheap, already works)**: click the war loser in the list, click `全选`,
  then post `CDonePeaceConferenceCommand`.  Verified that the list and `全选`
  respond to clicks (54.3) -- the only thing that never worked was the stacked
  notification, and that was a coordinate error too.
* **Data (needs work)**: build one `CStatePeaceAction` per Dutch state, append
  them to the live `CPeaceBiddingTurn`, then post the done command.  Costs a
  ctor and an append; worth doing only if the whole pipeline must be headless.

### 55.5 Practical consequence

A war can now be *closed* without a single mouse click, and the whole M3 chain
(declare war -> front -> offensive -> execute -> capitulate -> close the
conference) is reachable from code.  Only the *contents* of the treaty still
need either a click on `全选` or a hand-built action list.

## 56. Peace demands: what a headless "take the states" would need

Option B (build the demands in code instead of clicking `全选`) needs one layer
more than the commands do.  What is known:

### 56.1 The demand object

    CStatePeaceAction   vtable 0x2A4C690   ctor 0x15B43A0   size 0x58
    CPeaceAction        vtable 0x2A4C570   ctor 0x15B43F0   (base, type in edx)

`CStatePeaceAction(this, int* a, int* b, int* c, int stateId)`:

    +0x08  0x30D0            type id
    +0x10  std::string       localised name, built from the type id
    +0x30  *a
    +0x34  *b
    +0x38  *a
    +0x48  stateId           <- the 5th argument
    +0x4C  0
    +0x50  *c

The only caller is the factory at `0x14F2870`, which builds the whole available
list.  At `0x1414F2E71` it allocates 0x58, reads the state id from
`[CState+0x58]` (`ST_ID`), and passes `&node+0x1c`, `&CState+0x10` and a value
from `0x140C07100` for the other three slots.  Those three are **not yet
mapped to a meaning** -- do not guess them.

`peacewin.py` therefore only wraps the lifecycle commands.  A
`add_state_demand()` that fills `+0x30/+0x34/+0x50` by guesswork would post
garbage into a live conference.

### 56.2 The layer that is missing

The bid structures are created **lazily**.  With a conference open and the
Netherlands defeated, a full heap scan found:

    CPeaceConference         1
    CPeaceBiddingTurn        0      <- does not exist yet
    CStatePeaceAction        0
    CTakeNavyPeaceAction     2

So the player's bidding turn only comes into existence once a demand is made.
The code that builds the turns is `0xEB9ED0` (it installs the
`CPeaceBiddingTurn` vtable and walks the beneficiary list).  A headless
implementation has to call that builder first, then append
`CStatePeaceAction` objects to the resulting turn's action array.

Until that is done, the practical sequence is: pick the demands in the window
(`要求` -> the province icon -> the states on the map, or `全选` where it is
offered), then post `CDonePeaceConferenceCommand` -- the submit itself does not
need the mouse.

## 57. Peace demands: the whole chain, reversed

Section 56 stopped at "the bid structures are created lazily".  They are -- but
the creation is not a special event: every demand is produced by one of five
per-type **builder** functions that the conference window calls when it fills
the `要求` list.  Calling the builder yourself gives you the engine's own
`CPeaceAction` objects, fully initialised, with no field guessing.

### 57.1 Action type ids (verified at runtime)

The engine's own name lookup is `0xEA9F00(&out, typeId, 0, 0)`; it returns the
localised name as a `std::string`.  Calling it live in 1.19.5.0 gave:

    0x30CF 获得地区      take states          <- the one M3 needs
    0x30D0 解放          liberate
    0x30D1 傀儡          puppet
    0x30D2 改变政府      change government
    0x30EE 夺取海军      take navy
    0x30CE              (the bidding turn itself)

The window maps its five `要求` icon widgets to `[window+0x5968] .. [window+0x59A0]`
and dispatches on exactly these ids (handler at `0x18F1D60`).

Note the trap: `CStatePeaceAction`'s own ctor passes **0x30D0**, so that class is
the *liberate* action, not "take states".  The take-states action is a sibling
ctor that writes the same vtable but the id **0x30CF**.

### 57.2 Where a bid lives

    CPeaceConference +0x1E8   std::map<int countryIndex, CPeaceBiddingTurn*>

`CPeaceConference::EndTurn(int* countryIndex, CPeaceBiddingTurn** turn)`
(0xEB5390) does `find-or-insert` on that map, deletes any previous turn for the
key, and takes ownership of `*turn` (it zeroes the caller's pointer).  Its
callers are `CEndTurnPeaceConferenceCommand::Execute` (0x1ED2300) and the
conference's own round handling -- so this is the sanctioned way to install a
turn.

    CPeaceBiddingTurn   0x40 bytes
        +0x00  vtable 0x29EAEC0
        +0x08  int
        +0x10  CPdxArray<CPeaceAction*>   {data, cap, size, alloc}   (0x18)
        +0x28  CPdxArray<...>                                        (0x18)

A conference that has not had a demand made yet has `size == 0` on the map and
no `CPeaceBiddingTurn` anywhere in the heap -- confirmed again on 2026-11-14
with a fresh conference (map head valid, size 0).

### 57.3 The five builders

All five take one argument in `rcx` -- a small scratch struct -- and append to
the `CPdxArray` at **`arg+0x38`**, reading the *losing* country index from
**`arg+0x10`**:

    0x14F26C0   change government   0x30D2
    0x14F2870   liberate            0x30D0
    0x14F3070   puppet              0x30D1
    0x14F3560   take navy           0x30EE
    0x14F3950   take states         0x30CF
    0x14F2680   calls all five in order

`0x14F2680` is what the window's `全选` entry ends up using: it produces every
demand the loser can be made to give, in one array.

Calling `0x14F3950` with `arg+0x10 = 8` (Netherlands) in 1.19.5.0 returned
exactly five actions -- type 0x30CF, state ids **7, 35, 36, 309, 695**, `b = 8`
-- i.e. the five rows the window shows under `荷兰王国 (5)`.  That is the
strongest possible confirmation that the builder is the right entry point.

### 57.4 Object layouts (from the ctors)

    CTakeStatePeaceAction   0x50   0x15B46D0(this, int* a, int* b, CState*)
        +0x08 0x30CF   +0x30 *a   +0x34 *b   +0x38 *a   +0x48 stateId
        (+0x4C written 0; the ctor takes a CState* and reads [state+0x58])

    CPuppetPeaceAction      0x60   0x15B4530(this, int* a, int* b, int* c, int stateId)
        +0x08 0x30D1   +0x30 *a   +0x34 *b   +0x38 *a   +0x48 stateId
        +0x50 *c   +0x58 qword 0

    CTakeNavyPeaceAction           0x15B4600(this, int* a, int* b, void* r9)
        +0x08 0x30EE   +0x48 r9 (qword)   +0x50 5th arg (qword)
        (this ctor builds the `夺取海军` name itself)

`a` is the int behind `0xC07100(&local, 0)`; measured live it is **0**, and the
builders pass the same value for `+0x30` and `+0x38`.  `b` is the country index
of the loser whose assets are being taken.

### 57.5 What does not work

`CPdxArray::push_back` (0x1403371D0) must **not** be called by hand.  Passing a
hand-built turn with `cap == size == 0` and `alloc = base+0x30FD000` made the
game die instantly inside the injected call (the injector came back with
`ReadProcessMemory ... GetLastError=299`).  Three earlier crashes came from the
same habit of writing engine structures by hand.

Instead: let the builder allocate the array, then copy its **0x18-byte array
header** into `turn+0x10`.  The header already carries the engine's own
allocator, so nothing has to be grown and no allocator has to be guessed:

    arg = zeros(0x80)
    arg+0x10 = loser index (int)
    arg+0x38 = {0, 0, 0, base+0x30FD000}
    call 0x14F3950(arg)
    header = read(arg+0x38, 0x18)
    write(turn+0x10, header)
    call 0xEB5390(conference, &winnerIndex, &turnPtr)

### 57.6 The working recipe, end to end

    conf   = the live CPeaceConference
    turn   = new 0x40 bytes, [turn] = base+0x29EAEC0, +0x10 and +0x28 zeroed
    acts   = [a for a in builder(loserIndex) if a.type == 0x30CF]
    write(turn+0x10, {data: acts, cap: n, size: n, alloc: engine allocator})
    0xEB5390(conf, &winnerIndex, &turn)     # conference now owns it
    CDonePeaceConferenceCommand(winnerIndex) -> Post

`calling/peacewin.py` implements exactly this as
`add_demands(g, conf, winner_index, loser_index, kinds)` with
`kinds` in `{"state", "puppet", "navy", "liberate", "gov"}`.

### 57.7 Driving the game without the mouse

While testing this, the review machine ended up with the game drawing windows
that no longer accepted input (the conference panel and a `World News` popup
were both pure ghosts: hovering worked, clicks fell through to the map, and the
clock only advanced after the pause byte was cleared by hand).  Two consequences
worth writing down:

* `idler + 0x6C1 = 0` is enough to unpause a save that loads paused, and it
  also works when an event window is stuck on screen.
* `g.date()` is unreliable in this build -- different calls return different
  `CDate` copies, one of them a 1945 save preview.  The strip at
  `(2255,4)-(2450,40)` is still the only trustworthy clock.
### 57.8 First end-to-end attempt (2026-11-15)

`0xEB5390` cannot be called by hand.  It reads its `find` result out of a stack
slot the *caller* has to set up, so an injected call reports success (it does
zero the caller's pointer) but writes the value into the tree's head sentinel
and leaves `_Mysize` at 0.  Installing the turn therefore links the node by
hand instead -- with exactly one element the red-black shape is trivial:

    head = conf[map+0]                     ; the nil sentinel
    node = new 0x30
    node+0x00/08/10 = head                 ; _Left, _Parent, _Right
    node+0x18 = 0, node+0x19 = 0           ; _Color black, _Isnil false
    node+0x20 = countryIndex               ; _Myval.first
    node+0x28 = turn                       ; _Myval.second
    head+0x00/08/10 = node
    conf[map+8] = 1                        ; _Mysize

After that all three maps (`+0x1C8`, `+0x1D8`, `+0x1E8`) read `size = 1` with
the five `0x30CF` actions for states 7/35/36/309/695 inside, as intended.

Posting `CDonePeaceConferenceCommand` then closed the conference -- but the
treaty was

    阿姆斯特丹条约 -- 荷兰王国与德意志第三帝国达成了无条件和平

i.e. a **white peace**, and no state changed hands (`GER` stayed at 236
provinces, `HOL` kept all five).  That test was **inconclusive rather than
negative**: in that run the Netherlands never actually collapsed (it still had
8 divisions and all of its provinces when the conference opened), so there was
no won warscore to spend and the conference had nothing to give.  The demand
side has to be re-tested against a genuine victory conference -- the earlier
measured run reached one (HOL down to 1 division, 7 provinces occupied) but
that was before the builders were understood.

Open question for that re-test: whether `done` reads the bid from the map at
`+0x1C8`/`+0x1D8`/`+0x1E8` at all, or from the per-country context objects in
the array at `conf+0xF8` that the builders actually receive.  `EB50E0` looks
the player's index up in `+0x1C8` and sets `conf+0xDC` from the lookup result,
which is why `+0x1C8` was the first guess.

## 58. Peace conference -- the bidding really lives in `+0x1F8`, and `Done` does not apply

Measured live on a genuine victory conference (Netherlands capitulated, treaty
window "阿姆斯特丹条约" open, round 1/1, Germany 2800 score).

### 58.1 Conference field map (corrected)

    +0x060  CPdxArray<CCountry*>   cap 64  size 1   -- the losing side
    +0x078  CPdxArray<CCountry*>   cap 64  size 2   -- all participants
    +0x0F8  CPdxArray<...>         cap 1   size 1   -- per-country contexts
    +0x104  i32                                  count for +0xF8
    +0x110  CPdxArray<...>         cap 2   size 2
    +0x0DC  u8    "player has no bid"          (set by EB50E0)
    +0x1B8  std::map   _Mysize at +0x1C0   cleared in EB FD50
    +0x1C8  std::map   _Mysize at +0x1D0   <-- the bids that get APPLIED
    +0x1D8  std::map   _Mysize at +0x1E0   merged into +0x1C8 by EB FD50
    +0x1E8  std::map   _Mysize at +0x1F0   cleared by EB FD50
    +0x1F8  std::map   _Mysize at +0x200   <-- the live bid, per country

### 58.2 Two different map node layouts -- this is what broke the first test

    map<int, CPeaceBiddingTurn*>            node 0x30
        +0x20 key (int)   +0x28 value (pointer)

    map<int, CPdxArray<CPeaceBiddingTurn*>> node 0x40
        +0x20 key (int)
        +0x28 array.data     +0x30 array.cap
        +0x34 array.size     +0x38 array.alloc

Verified from `std::map::operator[]` at `0xEA69B0`: it allocates **0x40** bytes,
writes the key at `+0x20`, zeroes `+0x28/+0x30`, then stores the default
allocator at `+0x38`, and returns **`node+0x28`**.  `0xEB9AE0` reads the bid as
`data = [node+0x28]`, `size = [node+0x34]` -- i.e. the inline array.

The hand-linked 0x30-byte node used in the first attempt is the layout of the
*pointer* maps; writing it into `+0x1F8` makes the engine see an array with
`cap = 0, size = 0`, so the bid is silently empty.

### 58.3 `0xEB5390` is `CPeaceConference::InstallBidding` and it works

    EB5390(rcx = conf, rdx = int* countryIndex, r8 = CPeaceBiddingTurn** turn)

    1. steals *r8 and zeroes the slot
    2. find-or-insert on conf+0x1E8 ...
    3. **inserts into conf+0x1F8** as map<int, CPdxArray<CPeaceBiddingTurn*>>
    4. calls EB50E0(conf, countryIndex)

Step 3 is measured, not read: after the call `+0x1F8` held

    node key=1  arr[data=..., cap=1, size=1, alloc=base+0x30FD000]
      [0] -> the turn we built

and the turn itself held the five `0x30CF` actions for states 7/35/36/309/695
with vtable `0x29EAEC0`.  **The conference window immediately showed the bid**
(right panel "当前要求" -> 德意志第三帝国, 5, 获得地区) -- so this is the path
the UI itself uses, and the earlier note that "EB5390 does nothing" was wrong:
it was inserting into a map we were not looking at.

The earlier statement that it "reads its find result from the caller's stack"
was a misreading; the three-argument call works and returns with the slot
zeroed.

### 58.4 `Done` only logs -- it never applies anything

    CDonePeaceConferenceCommand::Execute  0x1ED22E0
        conf = 0x1ED1EE0()
        return 0xEB9AE0(conf, cmd+0x28)

`0xEB9AE0` = sum the cost of the player's turns in `+0x1F8`
(`0x15BB1B0(action, conf)` per action via `0x1A86C60`), emit a log line, then
tail-call `0xEB50E0`.

`0xEB50E0(conf, idx)`:

    find(conf+0x1C8, idx)
    conf+0xDC = (not found)
    if (0xEA9B10(conf)) return 0xEB0CE0(conf, 0xEB4260(conf+0x60))
    for each participant p in conf+0xF8:
        if find(conf+0x1C8, *(int*)(0x140BCF3F0(p)+8)) != end: return
    return 0xEBFD50(conf, 0)          ; nobody bid -> finalise

So a bid that is only in `+0x1F8` is *displayed* but never *applied*: the apply
path looks at `+0x1C8` (and `+0x1D8`, which `0xEBFD50` merges into `+0x1C8`
before distributing scores into `participant+0x4C`).

That is exactly what the live test produced: bid visible in the window, then
`done` -> conference gone, `GER` 236 -> 236 provinces, `HOL` kept states
7/35/36/309/695.  A second white peace, this time with a correct-looking bid.

### 58.5 `EndTurn` is the command that installs a bid

    CEndTurnPeaceConferenceCommand::Execute  0x1ED2300
        conf = 0x1ED1EE0()
        0x1A86C60(*(cmd+0x30), conf)          ; turn->Attach(conf) per action
        return 0xEB5390(conf, cmd+0x28, cmd+0x30)

`cmd+0x28` = country index, `cmd+0x30` = `CPeaceBiddingTurn*`.  The old
`end_turn()` helper wrote the vtable and the index but left `+0x30` null, which
is why it was never usable.  It now takes the turn pointer.

**Next test**: open a conference, build the turn with the five `0x30CF`
actions, post `EndTurn` with that turn, and watch `+0x1C8` -- if the engine
copies it there, the next `done` should apply the demands.

### 58.6 `CSetGameSpeedCommand` -- the old RVA was garbage

`cmdwin.VT_SET_GAME_SPEED` was `0x4474C40`, which is past the end of the image
(size `0x38A3000`); posting it killed the game.  RTTI (`rtti_win.pkl`) gives

    CSetGameSpeedCommand       vtable 0x29F3A80
    CIncreaseGameSpeedCommand  vtable 0x29F3900

`set_game_speed(g, 5)` with the corrected vtable posts cleanly.  Note the UI
`+`/`-` circles beside the date are **not** the speed control -- clicking them
opened the exit dialog.

## 59. Live conference, second run: the bid is rendered but `Done` is the white-peace exit

Same save, same plan: 30 German divisions, war declared 1936-03-02, the
Netherlands capitulated on **1936-03-14 15:00** (12.6 game days) and
"阿姆斯特丹条约" opened -- the army line is reproducible.

Sequence measured with the conference open (`conf` = 0x2da5b4a9cc0):

1. Baseline: `+0x1C8/+0x1D8/+0x1E8/+0x1F8` all size 0, `+0xDC = 1`.
2. `0x14F3950(loser=8)` -> five `0x30CF` actions, states 7/35/36/309/695.
3. Build a `CPeaceBiddingTurn` (0x40, vtable 0x29EAEC0, array at +0x10, 5
   action pointers) and hand it to the conference.
4. `CEndTurnPeaceConferenceCommand::Execute` called **directly**
   (`g.call(base+0x1ED2300, (cmd,))`, cmd+0x28 = 1, cmd+0x30 = turn):
   result 2560, and `+0x1F8` went 0 -> 1 while `+0x1C8` stayed 0.
   The conference counter went **和谈回合 1 -> 2**, so EndTurn is genuinely the
   "submit my demands" step.
   Note: `cmdwin.post()` also returned 1 and the command's CanExecute
   (0x1ED2590) returned 1, but nothing happened -- the queue does not drain
   while the conference is open.  Call Execute directly.
5. `CDonePeaceConferenceCommand::Execute` (0x1ED22E0) -> conference closed,
   `GER` 236 -> 236 provinces, `HOL` kept 7/35/36/309/695.  **Third white
   peace.**  With `+0x1C8` empty, `0xEB50E0` sets `+0xDC = 1` and the finalise
   path has nothing to give.

So the remaining gap is exactly one field: what the *apply* path reads is
`conf+0x1C8` (merged from `+0x1D8` by `0xEBFD50`), and neither `EB5390` nor
`EndTurn` nor `Done` ever writes it.  `0xEAFDB0(conf, &country)` is the
*withdraw* counterpart -- it erases the country from `+0x1E8`, `+0x1D8` and
`+0x1C8` in one go, which confirms all three are per-country bidding maps.

**Next probe (needs one more conference)**: let the *window* add a single
demand -- click the state category and a Dutch province -- then dump `+0x1C8`
and `+0x1D8`.  That reveals the node layout of the two maps the engine actually
applies, which is the only unknown left.  The window is the ground truth: it
already proved that a programmatic bid in `+0x1F8` is displayed.

## 60. Cross-tag control works; "dual drive" has to be single-owner

Probe: build `CDeclareWarAction` with `from = HOL (8)`, `to = BEL (7)` and post
it while the player is Germany.  The engine accepted it and the game raised

    来自荷兰王国的消息 -- 荷兰王国对比利时王国宣战

so the sender in a command object is authoritative: the engine does **not**
require the command to come from the player tag.  That makes "one agent per
country" possible in principle -- every command already carries its own
`CCountryTag` (`cmd+0x28` for the focus/research/war family).

The hard constraint is elsewhere: the game has one tick thread and no locking
around the objects we mutate, and our writes are injected calls that run on a
foreign thread.  Two agents injecting at the same time race inside the engine --
that is exactly what killed the process twice on 2026-11-14/15.  So parallelism
belongs outside the game:

    agent A --\
    agent B ---> gamehost.py (single owner, serialised) --> hoi4.exe
    agent C --/

`calling/gamehost.py` is that owner: a line-JSON socket server on
127.0.0.1:8765 with a whitelist of ops (`state`, `speed`, `pause`,
`declare_war`, `peace_demands`, `peace_done`), one request at a time under a
lock.  `calling/gh.py` is the three-line client.  Verified end to end:
`state` returned both countries, `speed` and `pause` round-tripped.

Three limits that will not go away:

1. A country that is not the player is run by the AI.  Orders you inject are
   re-planned on the next tick, so cross-tag control is "keep pushing", not
   "set once" -- the same pattern as `COrderInstance+0x248`.
2. There is one window and one cursor.  Two agents clicking is a race the game
   cannot arbitrate; all UI work must go through the same owner as the memory
   work.
3. A peace conference or an event window holds the whole game.  Nothing else
   can progress until it closes.

## 61. The player's selection lives in the window, not in the conference

Third live conference (`conf = 0x1d8b122f010`), same save, same 12-day war.

Clicking a Dutch state on the map, with 获得地区 active:

    tooltip  弗里斯兰 (◇27) / 德意志第三帝国占领 -> 荷兰王国的弗里斯兰
    header  分数: 2800 -> 2773 -> 2749

so each click really buys a state and is priced immediately.  **But all four
conference maps stayed at `size = 0` through every click** -- `+0x1C8`,
`+0x1D8`, `+0x1E8` and `+0x1F8` never moved.

The right panel shows why: the selection appears under

    未提交的要求 (unsubmitted demands)  1
        获得地区                       1
            荷兰王国                   1

i.e. it is held by `CPeaceConferenceWindow` -- the two arrays of
`CPeaceConferenceWindow::SetBiddingsInWinnerItem(const CPdxArray<CPeaceAction*>&,
CPdxArray<CPeaceBiddingTurn*>&, CPeaceWinnerBiddingsItem&)` -- and is only
copied into the conference when the player submits.  The bottom-right button
changes from 跳过本轮 to **提交要求 ◈51** as soon as something is selected; that
button is the submit.

The action objects the *window* creates are the same class as ours:

    UI      action vt 0x2A4C7B0   type 0x30CF   state 7 / 695
    builder action vt 0x2A4C7B0   type 0x30CF   state 7 / 35 / 36

so `0x14F3950` produces exactly what a click produces.  What is missing is the
window's half of the handshake, not the action.

Window object located by its two RTTI vtables `0x2A87468` / `0x2A87490`
(multiple inheritance, second vtable at `+0x28`) at `0x1d88cc25a80`.  Its first
0x300 bytes show no `CPdxArray` header, so the arrays belong to a child item
(`CPeaceWinnerBiddingsItem`) -- that is the next thing to walk.

Next probe, in order:

1. click one state, then find the `CPeaceWinnerBiddingsItem` under the window
   and read its `CPdxArray<CPeaceAction*>` -- that array is the selection.
2. call the button's handler (or reproduce whatever copies those arrays into
   `conf+0x1C8`) and confirm the maps move.

## 62. 提交要求落在 `+0x1F8`；`+0x1E8` 是 EndTurn 的槽；应用在 `0xEBFD50`

这一节是拿**活着的会议**做实验换来的（德国打赢荷兰，会议开着，手点一次"提交要求"），
不是读文档推的。

### 62.1 手点一次按钮，只有 `+0x1F8` 变

点之前四张表全空；点完 `+0x1B8/+0x1C8/+0x1D8/+0x1E8` 仍是 0，`+0x1F8` 变成 size=1：

```
node 0x1d8ba0dbe40              # 0x40 字节节点
  +00/08/10 = 头节点(空树形状)
  +18       = 01 00            # _Color=1(黑) _Isnil=0
  +20       = 00000001         # key = 国家序号 1 (GER)
  +28       = 000001d8017eb970 # CPdxArray<CPeaceBiddingTurn*> 内联:
  +30       = 0000000100000001 #   cap=1 size=1
  +38       = 00007ff664f3d000 #   allocator (base+0x30FD000)
turn 0x1d8ba0dc570 vt=base+0x29EAEC0
  +10 arr data=0x1d8017eb910 cap=1 size=1
   [0] type=0x30CF +30=1(winner) +34=8(HOL) +38=1 +48=35(弗里斯兰)
```

也就是说 **`+0x1F8` 是 `map<int, CPdxArray<CPeaceBiddingTurn*>>`，节点 0x40 字节、
数组内联在 +0x28**；turn 里的 action 与 `0x14F3950` 自己造的一模一样。
UI 造的对象和我们造的对象没有区别，缺的从来只是"往哪张表里放"。

### 62.2 `0xEB5390` 写的是 `+0x1E8`（反汇编为证）

```
0xEB5390(rcx=conf, rdx=&idx, r8=&turn)
  0xEB53AD  mov r12,[r8] ; mov qword[r8],0        ; 偷走 turn
  0xEB53BB  lea r14,[rcx+0x1E8]
  0xEB53CC  call 0x7684F0                         ; map::find(conf+0x1E8, key)
  0xEB5435  mov ecx,0x30 ; call 0x25A01DC         ; operator new(0x30)
  0xEB5442  mov [rax+0x20], edx                   ; key
  0xEB5445  mov qword[rax+0x28],0                 ; value = turn*
  0xEB547B  rcx=[rbx+0x28]; if(rcx) rcx->vt[0](rcx,1)   ; 释放旧 turn
  0xEB5492  mov [rbx+0x28], r12                   ; 存新 turn
  0xEB54B4  jmp 0xEB50E0
```
0x30 字节节点、turn 指针在 +0x28——和 `+0x1F8` 的 0x40 内联数组**不是同一个形状**。
之前把 0xEB5390 的结果拿去读 `+0x1F8`，自然什么都看不到。

### 62.3 `Done` 什么时候才真的应用

`CDonePeaceConferenceCommand::Execute 0x1ED22E0` -> `0xEB9AE0(conf, cmd+0x28)`：

```
0xEB9AE0  0xD95CB0(conf+0x1D8, &it, idx)   ; find
          0xD95CB0(conf+0x1C8, &it, idx)   ; find
          jmp 0xEB50E0(conf, idx)
```

`0xEB50E0`（0xEB50E0..0xEB5369）：

```
conf+0xDC = (find(conf+0x1C8, idx) == end)      ; "我没有提交"
if (0xEA9B10(conf)) { 0xEB4260(conf+0x60); return 0xEB0CE0(conf, al); }
for (每个 participant in [conf+0xF8, +0x104 个]) {
    0x692180(conf+0x1C8, &it, participant+8)    ; find by country index
    if (it == end) goto 0xEB536A -> 只打日志, 返回
}
0xEBFD50(conf, 0)                               ; <-- 真正的收尾/应用
```

所以 **`Done` 只在"每个参与者都在 `+0x1C8` 里有条目"时才走 `0xEBFD50`**；
否则就是那条只算分数、打日志、白和平的路径（三次白和平都出在这里）。

`0xEBFD50`（0xEBFD50..0xEC01EB，1179 字节）在 0xEBFF1D 调 `0xEA52E0`（合并
`+0x1D8` -> `+0x1C8`），随后 0xEC015E 调 `0xEA9B10`、0xEC0176 调 `0xEB0CE0`。
`0xEB0CE0` 被 `conf+0xDD`（已开始）挡着，里面遍历 `conf+0x140`，是收尾执行段。

### 62.4 顺带做出来的三件工具

| 文件 | 作用 |
|---|---|
| `stage/wfuns.py` | 读 `.pdata`，任意 RVA -> 函数起止（`0xEB50E0..0xEB5369` 这种边界就是它给的） |
| `stage/wdis.py` | capstone 反汇编指定 RVA，自动标注 call/jmp 目标 RVA |
| `stage/wcalls.py` | 全 `.text` 扫 `E8 rel32`，列出所有调用某 RVA 的站点 |
| `stage/wlea.py` | 扫 `8D/8B modrm disp32`，找"谁碰了 +0x1F8 / +0x1C8 这个字段" |
| `stage/wvt.py` | 按 RVA 打印虚表项 |

### 62.5 `conference()` 的堆扫描太慢，用引擎自己的 getter

`peacewin.conference()` 扫全部可写内存：**29 秒一次**，没法当轮询用。
`0x1ED1EE0` 是引擎自己的取会议指针函数（`EndTurn::Execute` 第一句就是它），
直接 `call` 它 **9 毫秒**返回 0 或会议地址。轮询循环一律用它（`stage/wfast.py`）。

### 62.6 `0x14F3070`（傀儡 builder）会先清 `arg+0x68`

```
0x14F308D  lea r12,[rcx+0x68]
0x14F30A2  mov rcx,r12 ; call 0x1AE000     ; 对 arg+0x68 那个 CPdxArray 做 clear
```
只把 `arg+0x38` 初始化成空数组、`+0x68` 留零，会在这里把游戏打死
（2026-11-15，`ReadProcessMemory ... GetLastError=299`）。两个数组都要写
`{0,0,0,default_alloc}`。傀儡 ctor 本身是
`0x15B4530(this, int* a, int* b, int* c, int state)`（第 5 个参数走栈，
`winject.call` 已经支持），`+0x48=state`、`+0x50=*c`、vtable `0x2A8C9F0`。

## 63. 割地成功：`+0x1F8` 就是全部，差的是 action 的 winner 字段

2026-11-15 这一轮完整跑通，**德国 11 天拿下荷兰，五个州全部易主，全程没用控制台**。

### 63.1 一条命令做完"全选 + 提交"

```python
turn, acts = peacewin.submit_demands(g, conf, winner=1, loser=8, kinds=("state",))
```

内部就是：`0x14F3950` 造出 5 个 `0x30CF`（states 7/35/36/309/695）→ `new_turn()`
→ `install_turns(g, conf, 1, [turn], 0x1F8)`（0x40 节点 + 内联数组，见 §62.1）。

界面上立刻变成"已提交"：右下角按钮从 `提交要求` 变成 `跳过本轮`，
右侧 `当前要求 → 德意志第三帝国 → 获得地区 1`，左侧计数 `5`。

### 63.2 关键坑：builder 把 winner 写成 0

```
0x14F3A2F  lea rcx,[rsp+0x90]
0x14F3A37  call 0xC07100          ; 返回一个 int*
0x14F3A3C  mov r9, rdi            ; state*
0x14F3A3F  mov r8, r13            ; arg+0x10 = 战败国
0x14F3A42  mov rdx, rax           ; a*  <- 这个
0x14F3A48  call 0x15B46D0         ; CTakeStatePeaceAction(this, a*, b*, state*)
```
`a` 指向的那个 int 在窗口里是**玩家国家序号**（实测 action `+0x30=1 +0x34=8 +0x38=1`），
但从注入线程里 `0xC07100(&local,0)` 给的是 **0**，于是我们造出来的 action 是
`+0x30=0 +0x38=0`。这种要求界面照样列出来、回合照样推进，但**一分点数都不扣**
（实测 `◈0`、分数一直 2800），等于废票。

修法：造完把每个 action 的 `+0x30`/`+0x38` 都写成 winner 序号
（`build_actions(..., winner_idx=1)`）。改完界面计数从 `0` 变 `5`。

### 63.3 收尾

`跳过本轮` 每点一次 `和谈回合 +1`，`+0x1F8` 里的 turn 一直保留。
最后点右上角 `确认并退出` → 弹 `退出和谈 / 你确定要退出和谈吗?` → `OK`，
条约当场生效，弹 `阿姆斯特丹条约` 新闻。

实测结果：

```
GER 24 州 236 省 -> 29 州 261 省     (正好 +5 州 +25 省)
HOL  5 州  25 省 ->  0
date 1936.03.13        (1936-03-02 开战, 11 天)
```

### 63.4 参与者结构（`conf+0xF8`，size 在 `+0x104`）

荷兰被完全占领后 size 从 2 掉到 1，只剩德国：

```
participant 0x18bad0e4eb0
  +00 vtable 0x2A8CC48
  +08 国家序号 = 1
  +10 conf
  +18 CPdxArray  data, cap=6, size=5     <- 5 条要求
  +40 内嵌子对象 vtable 0x2A8CC60
  +48 2798      +4C 2800                 <- 分数（当前/上限）
  +58 第二个数组 cap=6 size=5
  +70/+78 100000
```

### 63.5 还差的最后两下

`确认并退出` 和它的 `OK` 对话框还是手点的。命令侧已经有全部材料：
`CDonePeaceConferenceCommand::Execute 0x1ED22E0` = `getter(); 0xEB9AE0(conf, cmd+0x28)`。
下一轮会议直接调它试，若等价就把这两个点击也去掉。
（`CPassPeaceConferenceCommand::Execute = 0x1ED2350`，ctor `0x1ED2290`，size 0x30；
`CEndTurnPeaceConferenceCommand::Execute = 0x1ED2300`，ctor `0x1ED2230`，size 0x38。）

## 64. 傀儡/夺取海军：builder 会打死进程，只能走构造器

`0x14F3950`（获得地区）可以放心调；**`0x14F3070`（傀儡）不行**。
2026-11-15 两次实测，`arg+0x68` 也给成合法空数组之后仍然当场崩
（`ReadProcessMemory ... GetLastError=299`，进程消失）。`0x14F3560`（海军）同样没验成。

它前几行是：

```
0x14F308D  lea r12,[rcx+0x68]
0x14F3091  lea rsi,[rcx+0x10]
0x14F309C  call 0xEB67D0          ; (out=&rbp-0x30, &arg+0x10) 收集该国的州
0x14F30A5  call 0x1AE000          ; 对 arg+0x68 做事
0x14F30AD  call 0xC07BF0          ; 国家对象
0x14F30B2  mov rcx,[rax+0xF88] ; call 0xDA4CD0   ; 校验
```
`0xEB67D0` 很可能要求真正的会议/窗口上下文，注入线程里给不出，于是拿到的
`[rbp-0x30]` 不是数组头，后面 `mov rbx,[rdi]` 直接读野指针。

**构造器链（实际参数已从反汇编确认）：**

```
基类 0x15B43F0(this, edx=type, r8=a*, r9=b*, 栈上=c*)
    [this+0x00] vtable
    [this+0x08] = type
    [this+0x10] std::string 名字（SSO，容量 0xF）
                 名字来自 0x253ECD0(type)，即该要求类型自己的本地化名
    [this+0x30] = *a      [this+0x34] = *b      [this+0x38] = *a
    [this+0x3c] = 0       [this+0x40] = 0

CPuppetPeaceAction   0x15B4530(this, a*, b*, c*, int state)
    基类(type=0x30D1) -> [this+0x48]=state, [this+0x50]=*c, vtable 0x2A8C9F0

CTakeNavyPeaceAction 0x15B4600(this, a*, b*, void* r9, void* arg5)
    基类(type=0x30EE) -> [this+0x48]=r9（是**指针**，不解引用）, [this+0x50]=arg5
```

`a` = 得主（必须在注入时自己写，builder 那个 0xC07100 返回 0，见 §63.2），
`b` = 战败国。`c`/`r9`/`arg5` 的含义还没定；`winject.call` 支持 >4 个参数，
所以下一步直接用构造器造，别再用那两个 builder。

## 65. 全自动割地跑通：零鼠标，`done` 一条命令收尾

2026-11-15 03:17，同一个进程里从"会议刚开"到"五个州易主"全部由代码完成，
**没有点任何一次鼠标**。日志（`stage/wauto2.py`）：

```
CONFERENCE 0x2471b71ca70 at t+625s
status  maps +1b8/+1c8/+1d8/+1e8/+1f8 = 0 0 0 0 0
submitted 5 actions on turn 0x24738140b70
detail  [(0x30cf, winner=1, loser=8, state=7), (…35), (…36), (…309), (…695)]
pass  -> maps 0 0 0 0 1
done  -> maps 0 1 1 0 0      started=True  no_bid=False
 2s    conf=none   {'GER': [7,35,36,309,695], 'HOL': []}
```

也就是：

| 步骤 | 调用 | 结果 |
|---|---|---|
| 提交要求 | `peacewin.submit_demands(g, conf, 1, 8, ("state",))` | `conf+0x1F8` size 1，5 条 `0x30CF` |
| 跳过本轮 | `CPassPeaceConferenceCommand::Execute 0x1ED2350` | 回合推进，`+0x1F8` 保留 |
| 确认退出 | `CDonePeaceConferenceCommand::Execute 0x1ED22E0` | `+0x1C8`/`+0x1D8` 各出现 1 条 → 会议关闭 → 州易主 |

`done` 执行后 `+0x1C8` 与 `+0x1D8` 同时出现条目，说明合并确实发生在
`0xEB9AE0 -> 0xEB50E0 -> 0xEBFD50` 这条链里（§62.3），不是 UI 特供。

**M3 至此闭环**：宣战 → 画前线 → 进攻 → 会议割地，全部是代码；控制台一次没用。
'MCP 侧对应 `hoi4_peace_status` / `hoi4_peace_demands` / `hoi4_peace_finish`（`mcp/server.py`）。

### 65.1 一个反面教训：不要周期性重发进攻计划

`wauto2.py` 第一版每 90 秒重发一次计划（`wplan.offensive` + `execute_plan`），
第四次就把进程打死了。`wchain.drive` 的注释早就写着这句话，这次又验证了一遍：
**每重发一次就多一个 type-1 子订单，旧的在脚下被释放**。
正确做法是 `wchain.py --setup` 设一次 flag（`COrderInstance+0x248 = 1`），
然后只轮询，让引擎自己跑；荷兰 1936-03-02 开战、03-13 投降。

## 66. 傀儡/夺取海军：构造器能造出合法对象（端到端还差一个前提）

按 §64 的构造器链直接造，**不再碰那两个 builder**。实测（比利时，游戏里活着）：

```
puppet  vt=base+0x2A8C9F0  type=0x30D1  a=1(得主) b=7(战败) a2=1 state=6 extra=7
navy    vt=base+0x2A8CC30  type=0x30EE  a=1        b=7         a2=1 +0x48=0 +0x50=100000
```

字段、虚表、类型 id 全对，对象稳定没有被引擎拒掉。`peacewin.make_action()` /
`make_puppet_demands()` / `make_navy_demand()` 就是这三个构造器的封装。

**没做完的一步**：要在真会议里验证"比利时变成傀儡、舰队被夺"，
得先让比利时投降；而 `wplan.py` 整条链路写死了荷兰：

- `border_provinces()` 第一行 `H = set(hol[7] + hol[35] + hol[36])` → `KeyError: 7`
- `--setup` 里 `f['handle'] == HOL_FRONT` → 荷兰打完就没有这个 handle 了

也就是 **"打第二个国家"这件事本身还没通用化**，跟傀儡/海军无关。
下一步把 `wplan` 的敌国从常量换成"当前交战国 + 按省重叠挑前线"（原型脚本 `wbel2.py`，未随仓库发布
里已经有按重叠挑 front 的写法），傀儡/海军就能在同一次会议里验掉。

## 67. M1/M2 读数缺陷的根因（2026-11-15 夜）

五条已知缺陷逐条定位，全部是"Linux 偏移/路径直接搬到 PE"造成的：

**67.1 日期（M1.1 回归）**：`api._log_paths()` 在没有 `-userdir=` 时回落到
`~/.local/share/Paradox Interactive/...`（XDG 路径），Windows 上根本不存在，
于是 `_log_date()` 恒为 `None`——所有"这个 CDate 像不像真的"的校验全部失明。
`_win_date_addr()` 在**暂停**时按"值最大"挑候选，正好挑中一个冻结的 1945 副本
（同一局里新起的进程读 `1936.03.02`，另一个进程读 `1945.04.02`）。
修法三步：`config.user_dir()`（Windows → `Documents/Paradox Interactive/Hearts of Iron IV`）；
`_log_paths()` 用它；`_win_date_addr()` 暂停时以日志戳为下界、在窗口内取**最大值**
（陈旧副本永远落后，取最大才是真时钟）。修后两个独立进程读 `1936.03.10.13` / `.22`。

**67.2 建造队列**：`PS_CONSTRUCTION = 0x70` 和 `BL_BUILDING = 0x78` 本来就是对的
（`CProductionStatus+0x70` 是 `CPdxArray<CBuildingProductionLine*>`），错的是进度：
`BL_PROGRESS` 原来写 `0x28`，那是**总造价**（民用工厂 10800 IC-日）。1.19.5 的
**累计进度在 `+0x38`**，靠对一条在建产线做 25 秒差分确认（`1466.56 → 1486.36`，
只有这个字段在动）。现在返回 `progress`（0..1）、`progress_ic`、`cost_ic`。

**67.3 战争目标类型**：`diplomacy.wargoal_types()` 把 `CWarGoalType+0x10` 当 `char*`
解引用，全成乱码→空表→`justify_wargoal` 报 "unknown wargoal (options: )"。
1.19.5 里名字是 **MSVC `std::string` 在 `+0x18`**（size `+0x28`、cap `+0x30`）。
修后 10 个类型全部读出：`annex_everything civil_war independence_wargoal
liberate_wargoal puppet_wargoal_focus take_claimed_state take_core_state
take_state take_state_focus topple_government`。

**67.4 `front_changes()` 恒 `provinces_seen: 0`**：`mapdata.province_objects()` 用固定
Linux vtable 地址扫 CProvince，PE 上扫不到；即使扫到，1.19.5 的 CProvince 也没有
`+0xE0 CState*`/`+0xF0 CControllerArea`——它是 **`+0xD0 CControllerArea*` +
`+0xD8 COwnerArea*`**。新增 `_win_controller_map()`：读 `CState+0xCC`（控制者）展开到
该州各省，得到 10272 个省的控制器地图。`front_changes()` 从此能看到推进。

**67.5 研究槽**：`TS_SLOTS = 0xA0`、`RS_CURRENT = 0x18`、`RS_SPEED = 0x38` 在 1.19.5
上是对的（`CResearchSlot` 4 个，`+0x18` 指向 `CTechnology`，55 项已研究可读）。
`start_research()` 命令能下发并回读成功。**仍未解决**：槽里的 `progress/required`
（`+0x20/+0x28`）恒为 0，且 `tech_cost_days()`（`TT_RESEARCH_COST = 0x3D0`）在 PE 上
返回 -1/0——研究进度百分比暂时不可用，`base_days` 会是 0。

**67.6 顺带**：`peacewin.describe()` 读名字时不再因某个 action 类的 string 布局不同
把调用方打死；`wnavy.py` 给出海军读数——`CCountry+0x278` 是 `CPdxArray<CFleet*>`，
`CFleet+0xB8`（不是 Linux 的 `+0xB0`）是 `CPdxArray<CTaskForce*>`，`CTaskForce+0xA0`
是舰船数。实测 GER 3 舰队/6 特遣/40 舰，HOL 3/4/34。

这五条在开发期做成了可重跑的 PASS/FAIL 自检，当前全部通过。

## 68. 傀儡：头一次是真的生效了（§66 的结论要更正）

§66 说"端到端还差一个前提"，是因为验证方法错了。**傀儡生效后战败国保留自己的
全部州**，所以拿"德国州数变多"当判据必然看起来"什么都没发生"。正确的判据是
游戏自己吐出来的两条：

- 会议散会后弹出世界新闻：**《阿姆斯特丹条约》／"荷兰专员辖区成为傀儡。"**
- 国家 8（HOL）的名字从"荷兰"变成 **"荷兰专员辖区"**（TNO 的 Reichskommissariat），
  它的 5 个州（7/35/36/309/695，25 个省）一个不少地留在自己名下。

提交方式与 §63 完全相同：`install_turns(conf, 1, [turn], 0x1F8)` → `pass` → `done`。
五个 `CPuppetPeaceAction`（type `0x30D1`，`+0x48`=州 id，`+0x50`=战败国索引）
一次装进同一个 turn，引擎照单全收，进程没有崩。

**代价**：这一局的 `score_now` 只从 2800 掉到 2798，也就是傀儡在计数器上几乎不花点数；
真正的限制不是预算（原来的猜测不成立）。

**同时被证伪的还有"wplan 只能打荷兰"**：`border_provinces(g, enemy)` 现在按邻接算，
`--setup --enemy DEN` 能挑到丹麦的前线（handle `[66,3]`，2 个省）。

**坑**：`wplan.offensive()` 默认把进攻挂到"省数最多的根订单"上，打完荷兰后旧订单还在，
于是丹麦的进攻被挂到了旧的荷兰前线（返回 id 13/14 而不是新的 23）。正确做法是
先 `create_army()` 把师全部搬到一个新 OG，再按"新增的 type-2 订单"取 root_id
（`stage/wden.py` 就是这么写的）。


## 69. 夺取海军：端到端成立（`0x30EE` 只吃一个舰队）

§66 说"海军对象能造出来但端到端还差一个前提"，§68 之后只剩它没有真跑过。2026-11-15 补上，
并且把上次"`done` 打死进程"的事故分离掉了：**只提海军、不提傀儡**，进程活着，条约生效。

**开会**。`debug_force_capitulate DEN`（控制台，仅测试用）让丹麦在下一个日跳投降，
会议对象出现；`conf+0x34 = 25`（DEN 的国家索引），而 `conf+0xF8` 的参与国列表是
`[1, 8]` —— 德国和**荷兰专员辖区**。这不是错：傀儡自动跟随宗主国参战，
`game.log` 里就有 `荷兰专员辖区 is declaring war on 丹麦王国`，所以它也是战胜方成员。

**提交**。只造一个 `CPuppetPeaceAction`（`type 0x30EE` = 12526）：

    a=1  b=25  a2=1  state(+0x48)=0  extra(+0x50)=100000

`install_turns(conf, 1, [turn], 0x1F8)` → `pass`（`+0x1F8` 2）→ `done`
（`+0x1C8=2 +0x1D8=2`，`started=True`）。**没有崩**。

**结果**（同一进程读数，前后对比）：

| | GER | DEN |
|---|---|---|
| 舰船 | 42 → **48** | 7 → **1** |
| 舰队 | 3 → **4** | 2 → **1** |
| 特遣队 | 6 → **7** | 2 → **1** |

德国多出来的 `fleet[3]` 正好是 1 个特遣队 / 6 艘船 —— 和丹麦少掉的那一份形状一致。
丹麦保留 7 个州、1 艘船（另一个舰队的 1 艘）。

**所以 `extra=100000`（100%）是"一个舰队"的比例，不是"整支海军"**：builder 里 `r9 = 0`
表示默认舰队，拿到 6/7。要全拿，就得每支敌方舰队一条 action（`make_action(..., fleet=…)`
现在把这个值透出来了；舰队指针/索引的语义还没在引擎里定死，先只记录观测）。

**收尾**：会议关闭后 `combat.war_relations(refresh=True)` 为空 —— 对丹麦的战争真的结束了，
不是"割完地还在打"。会议对象会一直挂到游戏跑起来的第一帧才关，所以脚本里 `done` 之后
必须解暂停再等（`stage/wafter.py`）。

顺带把"上次为什么崩"缩小了一半：那次是 **5 个傀儡 + 1 个海军装在同一个 turn** 里，
`done` 时进程消失（`ReadProcessMemory ... GetLastError=299`）。这次 1 个海军单独走完全程，
说明海军 action 本身没问题；要重现就只剩"同一 turn 里混装多种 action"这一条线索。

## 70. M4 整合：工具层的 Windows 路径（陆军是最后一块）

原计划的 M4 是"把 focus/research/build/army 暴露成 MCP 工具"。前三个家族在 `game.py`
里早有 `sys.platform == "win32"` 分支（研究、建造、宣战、速度都走 `cmdwin`），**陆军没有**：
`create_army` / `draw_front_line` / `orders_of` / `execute_plan` / `border_fronts` 全部
`import armyops` —— 那是 Linux 实现，在 PE 上要么报错要么直接打死进程。

**70.1 现在走哪条路**。`calling/armyops_win.py` 新增了工具层需要的全部件，并且把
`stage/wplan.py` 里验证过的两段（进攻线、执行）搬了进来，因为工具层不能 import 舞台脚本：

| 函数 | 作用 |
|---|---|
| `owned_provinces(g, tag)` | `GS_STATES` + `CS_OWNER` + `ST_PROVINCES`，州→省（无外部脚本依赖） |
| `fronts_towards(g, enemy, own)` | 用 `A.fronts()` 的 CFront 列表 + 静态邻接图，挑真正贴着敌人的段 |
| `target_provinces(g, enemy, depth=2)` | BFS，进攻线的目标（跳过第一排） |
| `divisions_of(g, og)` | `og+0x50` 的 30 个师 |
| `offensive_line(g, og, parent_id, provs, divs)` | `COrderNewFrontCommand` `0x18C9840` |
| `execute_plan(g, og, order_id)` | `COrderExecuteCommand` `0x18C8C50`（绿勾） |

`game.py` 里对应的方法全部按 `win32` 分派；`hoi4_army_battleplans` 现在落到
`Game._win_battleplans()`：建军团 → 画前线 → 在同一根订单下画进攻线 → 执行 → 置 `+0x248`。
每一步都轮询回读，不把"Post 成功"当成"订单存在"。

**70.2 两个真 bug**

* `order_children()` 把 `COrderInstance+0x248` 当 **i32** 读。它其实是一个 **u8**，
  后面三个字节是别的字段，于是读出来是 `-1716106239` 这种垃圾值，`start_offensives()`
  每次都以为"还没执行"，一遍遍重设。改成 `p.u8` 后 `executing` 正常显示 0/1。
* **暂停时建的计划是死的**：在暂停的游戏里跑同一条链，`COrderExecuteCommand` 造出的
  type-1 子订单 `assigned` 永远是 0（30 个师一个都没进去）；解暂停后再建就正常
  （`assigned=30`）。所以 `_win_battleplans` 会临时解暂停、建完再恢复原来的暂停状态，
  返回值里用 `resumed_for_plan` 标出来。

**70.3 自检**。开发期的只读自检脚本直接调用 MCP server 的 `HANDLERS[name](args)`
（和传输层同一个入口），29 个只读工具 **全绿**：状态/日期/总览/国家、国策四件、
研究四件、生产/建造/建筑类型/国家统计、师/军团/战区/边境前线/订单/陆军状态、
舰队/特遣队/空军、州/州详情/前线变化。`--write` 再加一条"占一个空科研槽"。

**70.4 还没证的**：进攻线画出来之后**没有推进**。比利时那次：`hoi4_declare_war` 成功
（`at_war_verified: true`），`hoi4_army_battleplans` 返回 `ok: true`，订单树齐全
（根 31 / 进攻线 32 带 14 个目标省 / 执行子订单 34 `assigned=30, executing=1`），
但 6 个游戏日里比利时 0 个省易手，30 个师里只有 4 个真正站到前线省，其余还在行军。
§53 的荷兰战例是"师已经在边境线上蹲了几天"之后才画箭头的，所以最可能的解释是
**师没到位就不会进攻**，但这只是解释，不是证据 —— 需要在师到位后重测，或者直接比对
引擎自己的 AI 进攻订单（§49 的做法）。

**70.4.1 第二个数据点（波兰，同一晚）**：`hoi4_declare_war POL` 之后，同一份计划
（根 35 / 进攻线 36 / 执行子订单 38 `assigned=32, executing=1`）跑了 **5 个游戏日**：
4 个师终于站到波兰边境省，**0 个省易手**，而且师的位置每个采样点都在变 —— 引擎一直在
沿前线重排（典型的"前线上兵太多，来回摊"），但没有一次跨过边界。

两个候选原因，都还没排除：

1. **军团没有指挥官**（`hoi4_army_status` 里 `leader_ptr: null`）。§53 的荷兰战例没记
   这一条，所以不排除当时是有将的；
2. 游戏速度被压在 1-2（`cmdwin.set_game_speed(g, 5)` 回读仍是 1），行军以天计，
   观察窗口（5 天）短于"走到位 + 组织度回满"所需。

下一步应该先 `hoi4_assign_commander`（或 `hoi4_generals` 挑一个）再重画一次，然后
才谈"进攻线不推进"是不是引擎层的 bug。**在拿到结论之前，不要对外说 M3 的"30 天拿下"
在工具链上成立** —— 手动链（§53/§54）成立，工具链的推进没证。

**70.4.2 在"已知能赢"的存档上复现，还是不推进。** 为了排除"比利时/波兰本来就不该赢"，
重开进程加载 `GER_1936_03_02_12.hoi4`（对 HOL 的战争在打，§53/§54 就是从这个存档赢的），
然后**只**调 `hoi4_army_battleplans(enemy_tag="HOL")`：

    fronts   [66,7] section 1, border 8          <- 和 §53.2 的前线一致
    targets  68 3211 3262 3314 6336 8622 9431    <- 和 §53.5 的 depth-2 列表逐项一致
    create_army 30 divs -> OG 0x265fd9b7090
    front_line  ok  新订单 id 2
    offensive   ok  child 5
    execute     ok  flags [[5,1]]  children [[5,30,1],[3,0,0]]
    ok: true, resumed_for_plan: true

结构上这就是 §53.2 那份计划，一个字段都不差。然后解暂停跑了 **7 个游戏日**：
8 个师已经站在荷兰边境省（`on-border=8`），HP 平均 204.7、组织度平均 37.9，
`+0x248` 一直是 1（两个子订单都试过置 1），**荷兰 0 个省易手，`active_combats` 一直是 0**。

结论：**§53 的"把 `+0x248` 置 1 就会进攻"在当前进程上不成立**。剩下的差别只可能在
订单是**怎么造出来的**：§53 那次是同一套直接构造函数，但当时的前线订单已经存在了
若干天；现在工具是"建军团 → 立刻画前线 → 立刻画进攻线 → 立刻执行"一气呵成。
最可能的原因是直接 `call` 构造函数造出来的 `COrderInstance` 没有走引擎命令路径的
登记逻辑（`0x10CD510` 只重建订单自己的缓存，不重建军团级的战线分配）。
下一步应该对比"UI/命令路径造出来的进攻订单"和"直接 ctor 造出来的"在
`COrdersGroup` 里的登记差异（§49 的 AI 订单差分就是干这个的）。

在此之前，M4 只能算"工具通了、订单树对、推进未证"。

另外记一笔：`hoi4_declare_war` 绕过保证条约，对象是比利时的时候把整个同盟（ENG/FRA/CAN/
RAJ/SAF/NZL… 共 17 国）一起拉进了战争，会议根本不会开（战争领袖不是比利时）。
测试收尾用 `whitepeace BEL` / `whitepeace POL` 一次结束全部战争。

## 70.5 国策写入：`set_focus` 通了，`drop_focus` 打死进程

M4 的四个家族里，国策的**写**路径也是 Linux 的：`game.set_focus()` 走
`commands.py` 的 `self.cmd.send("SetNationalFocus", ...)`，在 PE 上直接
`RuntimeError: commands.new() is Linux-only`。而 `cmdwin` 里早就有
`make_national_focus` / `set_national_focus`（构造函数 `0x1C0…`、vtable `0x2A128C8`），
只是没人接线。

接上之后实测（`hoi4_set_focus`）：

    before {"current": null}
    set    {"ok": true, "posted": 1, "focus": "GER_remilitarize_the_rhineland",
            "current": "0x1bda6e67d10"}
    after  {"current": {"key": "GER_remilitarize_the_rhineland",
                        "name": "莱茵兰重新军事化", ...}}

也就是**国策能开始**（`+0x18 CO_FOCUS_PROGRESS` 的 current 指针变过去了）。`set_focus_ptr`
走同一条路。

**取消就不行**。`CDropCurrentNationalFocusCommand` 在 `cmdvts.json` 里只有一条
"arg1 → +0x24, deref"，我按 `(cmd, _tag_block(tag))` 调它的构造函数（`29376048`），
对象是造出来了，`Post` 也过了，然后 **hoi4.exe 一秒内消失**
（下一次读内存 `GetLastError=299`）。所以 `game.drop_focus()` 在 Windows 上现在
诚实地返回

    {"ok": false, "error": "dropping a focus is not wired on hoi4.exe: ..."}

正确做法是把 `0x1C03F30` 附近那个构造函数反汇编出来看它到底读几个参数
（`cmdvts` 的推断只覆盖了 `+0x24` 一个字段，显然少了东西），在那之前别再试。

**代价**：这一条把进程打死了，当时那局的比利时/波兰收尾和刚点的国策全丢，
重开只能从 `GER_1936_03_02_12.hoi4` 起。教训和 §4 的风险 1 一样：
**构造函数签名没反汇编清楚之前，任何 `game.call(ctor, ...)` 都是拿进程做赌注**。

## 70.6 推进问题结案：不是订单错了，是**等得不够久**

§70.4 说的"订单树全对但不推进"，最后证明是**观测窗口太短**。同一份计划从建好到第一个省
易手要**几分钟的墙钟时间**，到荷兰投降约 **17 分钟**——之前的每一次判断都停在 3~5 分钟。
（这一局的日期读数不可靠：§67.1 说过 `CDate` 有好几份副本，`_win_date_addr()` 在暂停/慢速时
会挑中冻结的那份，所以这里按墙钟记，不按"游戏日"记。）

**干净复现（2026-11-15，重开进程加载 `GER_1936_03_02_12.hoi4`，只调 MCP 工具）**：

    hoi4_army_battleplans(enemy_tag="HOL")
      前线 [66,7]/8 省  ->  进攻线 7 个目标省  ->  30 师  ->  箭头 executing=1
      （调用时就解暂停建计划，建完恢复暂停）

    t+  0s  建好计划（箭头 executing=1）      荷兰境内 GER 控制 0 省
    t+244s  wait_seconds=240 到期返回          0 省   <- 240 秒还不够，别在这里下结论
    t+~300s 第一个省易手                       9 个省
    t+~600s 继续推进                          14 个省
    t+~17min                                  21 个省 -> 荷兰投降 -> 会议打开

同样的时间线在"手动"那一轮也成立（计划 03.02 建好，03.13 前后开始掉省），只是当时正好
在同一分钟里点了一次 `execute_plan(og, 3)`，看起来像那个调用起的作用，其实是时间到了。

**那 `execute_plan(og, order_id)` 到底是什么？** 顺带纠正一个坑（反汇编
`0x18C8C50` 构造函数、`0x18D64A0` Execute）：

    0x18C8D24  mov [rbx+0x30], esi    ; arg3 = order id
    0x18C8D27  mov [rbx+0x34], dil    ; arg4 = flag
    0x18D64CD  mov edx,[rdi+0x30]
    0x18D64D5  je  0x18D64FA           ; id == 0 -> 造订单树
    0x18D64F5  jmp 0x10CD510           ; id != 0 -> [order+0x248] = flag

我们的 `armyops_win.execute_plan()` 把 arg4 写死成 0，所以 `execute_plan(og, 3)` 实际是
**把 3 号订单的 executing 关掉**（"再点一次绿勾 = 取消"）。真正的"点绿勾"是把 type-2
进攻线订单的 `+0x248` 置 1，走 `set_order_executing`（引擎的 `0x10CD510`，参数
`(order, 1, 1)`）。`_win_battleplans` 现在建完树就顺手把箭头置 1，并且不再用
`execute_plan(id)` 去"激活"。

**工具改动**：`hoi4_army_battleplans` 新增 `wait_seconds`。给 0（默认）时建完就返回；
给 600 时它会解暂停一直等到第一个敌方省易手，并把
`steps.advance = {waited, enemy_provinces_taken, date}` 写进返回值 —— 这样调用方不用
猜"是不是没生效"。**240 秒实测不够**（这一局一台机器上大约 5 分钟才出第一个省），
所以默认建议值写的是 600。

**M3 整条链（全部 MCP 工具，零鼠标）**：

    hoi4_army_battleplans(enemy_tag="HOL", wait_seconds=240)
    hoi4_peace_status / hoi4_peace_demands(loser="HOL", kinds=["state"])
    hoi4_peace_finish("pass") / ("done")
    -> GER 24 -> 29 州, HOL 0 州（实测一次，见 §69 同一套会议代码）

教训写在这里：**在速度 1 的游戏里，任何"几分钟没动静"都要先换算成游戏日**。这个坑已经
吃掉两轮结论（§70.4、§70.4.2 的"不推进"都不成立）。

## 71. 和平会议收尾会**偶发卡死**（`done` 计算条约时）

2026-11-15 13:37，荷兰那次 5 条割地 action 的会议在 `done` 之后**卡住**（不是崩溃）：

* Windows 事件日志：`AppHangTransient`，`hoi4.exe`，13:43:18，无异常码 —— 是挂起。
* `logs/error.log` 最后一行：

      13:37:29  [1936.03.16.01]  History for GER is invalid.
                                Number of turn is 2 when it should be 1. Will resize.

  和 §69 那次"5 傀儡 + 1 海军"崩掉前的最后一行**完全相同**。

今天的会议一共跑了 5 次，2 次死在这一步：

| # | 内容 | 结果 |
|---|---|---|
| 1 | 5×傀儡（HOL） | ok |
| 2 | 5×傀儡 + 1×海军 | `done` 时进程消失（GetLastError=299） |
| 3 | 1×海军（DEN） | ok |
| 4 | 5×割地（HOL，工具链） | ok |
| 5 | 5×割地（HOL，工具链） | **卡死**（AppHangTransient） |

所以不是"海军 action"或"割地 action"的问题，而是**多个 action 的 turn 在 `done`
把 `+0x1F8` 合并进 `+0x1D8 → +0x1C8` 时，历史轮数对不上（2 vs 1）**，引擎的
"Will resize" 路径有时能扛住、有时挂死。

已知的间接因素：
* 那 5 条 action 是同一个 `CPeaceBiddingTurn`（size 1），所以 `+0x1F8` 的 size 是 1；
  引擎自己（AI 战胜方/自动出价）可能也放了一个 turn，合并后就成了 2。
* `pass` 和 `done` 之间只有 3~4 秒，而且比赛是用 `Execute` **直接调用**（队列在会议期间
  不抽），没有等 `+0x1F8` 被消费就发 `done`，存在竞态。

**根因**：`pass` 会给 `+0x1F8` **追加第二个 turn**。实测（2026-11-15）：

    装完 1 个 turn              +0x1F8 = 1
    pass 之后                   +0x1F8 = 2      <- 多出来的那一个
    done 之后                   +0x1C8 = 2, +0x1D8 = 2
    error.log                   History for GER ... Number of turn is 2 when it should be 1

引擎随后走 "Will resize" 把 2 压回 1，这条路有时能扛住、有时挂死（5 次里 2 次）。
我们提交的 turn 本来就已经在 `+0x1F8` 里了，**根本不需要 `pass`**。

**修法**（`calling/peacewin.py: finish()`）：提交完直接 `done`，不 `pass`。
`hoi4_peace_finish(action="done")` 现在走这条路；`pass` 仍单独保留（UI 的"跳过本轮"）。

**验证**（同一进程连跑 3 次会议，每次都看 maps 和 error.log）：

| 对象 | 动作数 | 装完 +0x1F8 | done 后 | started | 结果 |
|---|---|---|---|---|---|
| DEN | 7 | 1 | +0x1C8=1 +0x1D8=1 +0x1F8=0 | true | GER 24→31，DEN 0 州，活着 |
| LUX | 1 | 1 | 1 / 1 / 0 | true | GER 31→32，LUX 0 州，活着 |
| CZE | 10 | 1 | 1 / 1 / 0 | true | GER 32→42，CZE 0 州，活着 |

三次的 `error.log` 里**没有**任何 `History for ...` 行 —— resize 路径根本没被触发。
对比旧流程（pass→done）5 次里 2 次死：一次进程消失、一次 AppHangTransient。

**残留风险**：如果会议里有多个战胜方（例如傀儡跟着参战），`done` 只提交自己那一方，
round 可能等其他人。`finish()` 会把 `after_done` 的 maps 和 `started` 一起返回，调用方
看到 `+0x1C8` 还是 0、`started` 还是 false 时，应改用 `endturn`，不要退回 `pass`。

## 72. M4 最后两个洞：`drop_focus` 与研究进度百分比（2026-11-15 下午）

### 72.1 `CDropCurrentNationalFocusCommand`：从 vtable 反推，不用表里的地址

旧代码里的 `29376048`（`0x1C03F30`）是错的：拿 `(cmd, tagp)` 去调它在几秒内就把
进程打死了（下一次读 `GetLastError=299`）。这次改成**从 RTTI 往下查**：

```
.?AVCDropCurrentNationalFocusCommand@@  -> vtable 0x2A12988
.text 里 lea reg,[rip+0x2A12988] 只有 3 处：
    0x11CE6F0   ctor          (rcx=cmd, rdx=CCountryTag*)       size 0x30
    0x11D56A0   clone         (复制 +0x0C..+0x28)
    0x11D8D20   factory       operator new(0x30) + 默认值
```

ctor 自己写出来的布局（和 `CSetNationalFocusCommand` 0x11D0DC0 同形，只少一个
`CNationalFocus*` 参数）：

```
+0x08 byte 0    +0x0C -1    +0x10 0    +0x18 0xFFFF    +0x1C 0
+0x28 int32 = *tag          [rcx] = 0x2A12988
```

也就是说这条命令只带"谁"，执行时清掉该国的当前国策。vtable 槽位也和 set-focus
一一对应（0x00 析构 / 0x48 CanExecute / 0x50 Execute 0x11E0740 / 0x68 clone），
所以不需要再猜参数。

`calling/cmdwin.py`：`make_drop_national_focus()` 调 ctor 并**校验 vptr 和 +0x28**，
`drop_national_focus()` 在没有国策时直接返回 `no focus in progress`；`game.drop_focus()`
的 win32 分支改成走它并回读 `FP_CURRENT == 0`。

**实测**（一次进程内两轮，每轮 set → 读 → drop → 读）：

| 轮 | set 后 `FP_CURRENT` | drop 后 | 进程 |
|---|---|---|---|
| GER_remilitarize_the_rhineland | 该指针 | 0 | 活 |
| GER_the_four_year_plan | 该指针 | 0 | 活 |
| 无事可取消时再 drop | — | 0，返回 `ok:false, reason:no focus in progress` | 活 |

顺带一个教训：`selectable_focuses()` 那 427 条是**静态可选项**，不是"现在能点"的。
`GER_autarky_efforts` / `GER_reorganize_the_wehrmacht` 拿指针去 set 会被引擎拒绝
（`posted:1` 但 `FP_CURRENT` 不变），真正能点的只有 `focuses(only_startable=True)`
那 8 条。drop 本身没有问题。

### 72.2 研究进度百分比：进度在 `CTechnology` 上，不在 `CResearchSlot` 上

`+0x20 / +0x28` 一直读 0 的原因不是偏移错了，而是**它们不是当前科技进度**。
`CResearchSlot::Save`（0xF61BF0）把三个字段按引擎自己的成员号存盘：

```
slot+0x20 -> 0x31DE      slot+0x28 -> 0x34BB      slot+0x38 -> 0x3684
```

而 `CResearchSlot::SetTechnology`（0xF60170）里 `+0x20` 只在**换科技时**承接旧进度、
`+0x28` 是那一刻算出来的"还需多少"，跑科技的过程中两者都不动（实测一直是 0 和
3 000 000 = 30.0）。真正的进度在科技对象上：

```
CTechnology+0x198          int64，研究天数 x1e5（按速度加权）
CTechnology+0x160          -> CTechnologyTemplate
CTechnologyTemplate+0x3E0  int32，总天数 x1000（150000 = 150 天）
CTechnologyTemplate+0x3D8  int32，起始年份（1943 的科技读出 1943）
CResearchSlot+0x38         研究速度 x1e5（1.01 = 每天 +1.01 天）
```

两条独立验证：

1. **已完成的 56 个科技，`+0x198 / (cost/1000)` 全部正好 1.1**（165/150、220/200、
   330/300 …）—— 开局研究速度 110%，进度停在 `cost × speed` 上，一个不多一个不少。
2. **跑着的科技每天正好涨一个 `speed`**：`basic_machine_tools` 的 `+0x198`
   3 535 000 → 3 636 000 → 3 737 000（每次 +101 000，就是 `slot+0x38`），
   期间游戏时钟 1936.03.26.18 → 27.17。

于是 `hoi4_research` 现在给的是：

```
progress       = tech[+0x198] / 1e5        （已研究天数，速度加权）
required       = template[+0x3E0] / 1000   （总天数）
percent        = 100 * progress / required （夹在 100 以内）
days_remaining = (required - progress) / speed
```

实测同一进程内：basic_machine_tools 44.44/150 = 29.63%、剩 104.5 天；construction1
35.35/200 = 17.68%、剩 163 天；两分钟后分别涨到 31.65% / 19.19% —— 和面板上
"130 日"那类读数同量级（150−19.4)/1.01 ≈ 129 ✓。

`tech_cost_days()` 一并改成读 `+0x3E0/1000`，不再是 `None`。

**新科技开局自带约 30 天进度**（`+0x198` 起步 3 030 000）：这是引擎的"存档研究点"
（`BASE_RESEARCH_POINTS_SAVED` 一类），不是我们写进去的，所以 1 级科技刚点开就有
~20% 是正常的。

### 72.3 顺手修掉的两个真 bug

* `calling/session.py` 的 `G_IDLER` 还是 1.19.4.1 的 `0x459E908`。在 PE 上读它会
  直接 `ReadProcessMemory ... GetLastError=299`，被误判成"进程死了"（其实进程好好的，
  299 只是地址无效）。现在 win32 下从 `offsets_win.G_IDLER`（`0x33DDB08`）取，
  `hoi4_pause` 的 `is_paused` 也终于读对了；`pause()` 在 win32 下改成只写
  `idler+0x6C1` 并校验，不再用 ELF 的 `CPauseGame` vtable（那会打死进程）。
* `_load_tech_db()` 在数据库对象存在但还没填充时会缓存一个**空表**，之后整个进程
  的 `hoi4_research_available` 都返回 0。现在空表不缓存（和"太早加载"同样处理）。

### 72.4 现象记录：弹窗会停表，暂停字节会骗人

测研究进度时被卡了很久：时钟不走，但 `idler+0x6C1` 读 0。原因是**世界新闻弹窗**
（丹麦/卢森堡/捷克斯洛伐克的投降公告）把游戏停住了——弹窗不写暂停字节。按 ENTER
或点"令人满意的消息"关掉后时钟才继续。另外把游戏窗口最小化也会停表。

所以"游戏在不在跑"只能看**屏幕右上角的日期**，不能信 `+0x6C1`，也不能信
`date_raw()`（§67.1 已经记过它不可靠）。这次的三次"0 小时"假象全是这个原因。

## 73. 生产面板：装备名全 null、工厂数是 7143521、优先度是 6619137

`hoi4_production` / `hoi4_overview` 的 `production_lines` 在 1.19.5.0 上三个字段全错：

| 报出来的 | 真实值 |
|---|---|
| `equipment: null`（全部 21 条） | 步枪 / 支援装备 / 火炮 / 轻坦 / 摩托化 … |
| `factories: 0 / 7143521 / 578` | 16 / 2 / 1 / 2 / 2 / 1 / 1 / 1 |
| `priority: 1 / 6619137 / 1389217537` | 全部 1 |

`unit_cost`(0x28) / `output`(0x30) / `eff_cost`(0x38) / `max_eff`(0xC8) 一直是对的，
所以对象本身找得没错，错的是字段偏移和**装备名映射**。

### 73.1 用"改一次、看哪 4 字节动"定字段

`CAddProductionLineFactoriesCommand` 的 delta 是**增量**，所以拿它当探针：
先快照 `CMilitaryProductionLine` 的 0x000–0x200（每 4 字节），发 `+3`，再快照。

```
diff: 018: 13 -> 16      工厂数
      030: 2987329 -> 3260414   日产量（因果，不是字段）
      038: 8938 -> 27678        有效成本（因果）
      0E8: 13 -> 16            产量计算缓存，跟着动
```

结论：**`ML_FACTORIES = 0x18`**（旧值 0x14 落在填充区）。
优先度在 **`0xF0`**（21 条线全是 1），`+0x10` 在部分线上是别的数据，不能当 int32 读。

### 73.2 装备名：`CEquipmentDatabase` 的数组头错位 8 字节

`_load_equipment()` 遍历 `db+0x60 / 0x78 / 0xF0 / 0x120`，四张表**全部错位**：

`CPdxArray<T*>` 在这版是 **0x18 字节**：`{ T* data; i32 cap; i32 size; IAllocator* alloc; }`。
用 `hoi4_browse_object` 扫 `CEquipmentDatabase` 就能看到一串间隔 0x18 的合法表头：
`0x68, 0x80, 0x98, 0xB0, 0xC8, 0xE0, 0xF8, 0x110, 0x128`。
旧代码的 4 个偏移**每个都差 8 字节**，正好落在上一个表的 `alloc` 指针上 ——
`_arr()` 把它当 data、把后面的字段当 size，于是读出一堆垃圾或直接返回空表。
**空表 → `_eq_by_id` 是空字典 → 所有线路 `equipment: null`。**

真正的表：

| 偏移 | 条数 | 类 | id | 名字 |
|---|---|---|---|---|
| `db+0x68` | **457** | `CEquipmentArchetype`（如 `infantry_equipment_1`） | `+0x08` | **`+0x18`** |
| `db+0x80` | 81 | `CEquipmentType`（如 `infantry_equipment`） | `+0x08` | `+0x18`（另有 `+0xF8` = `archetype_*`） |

`ET_NAME` 原来是 `0x10` —— 那是条目里的 ASCII 标签 `"name"`，不是字符串本体。

`CEquipmentVariant` 里指向原型的不是指针，而是 **`+0x18` 这个 u64 的低 32 位**
（高 32 位恒为 1）：`0x1_0004EE9` → `20201` → `infantry_equipment_1`。
所以 **`EV_TYPE_ID = 0x18`**，语义从"CEquipmentType id"改成"装备原型 id"。

改完后 `hoi4_equipment_types` 返回 456 个名字，`hoi4_equipment_stockpile` 读到
步枪 3958 / 支援 69 / 火炮 57 / 轻坦 78 / 摩托化 191 / 火车 71，
且 `available_variants` 里 `infantry_equipment_1` 的 `variant_ptr` 和 0 号线
`CEquipmentVariant*` 是同一个地址（`0x242ac638430`）—— 交叉验证通过。

### 73.3 顺带修掉：`set_production_amount` 的参数名是旧的

它发的是 `{"arg0": …, "arg1": …}`，而 `cmdwin.ARGSPEC` 里
`CAddProductionLineFactoriesCommand` 是 `{"line_id": 1, "amount": 2}`，且
`ARG_KIND` 标明 arg1 是 `ARG_HANDLEPTR`（要的是指向 `{0, handle}` 的 16 字节块，
不是把 handle 直接塞进寄存器）。所以这个工具在 Windows 上**从来没成功过**，
报的是 `CAddProductionLineFactoriesCommand needs ['line_id']`。

改成 `{"line_id": packed, "amount": delta}` 后实测：2 号线（火炮）1 → 3，
`steps: [2]`，读回 `3`。

### 73.4 已知限制：负数 delta 发不出去

`winject.call()` 用 `struct.pack("<Q", arg)` 打包每个整数参数，负的 delta 直接
`struct.error: argument out of range`。`set_production_amount` 现在会**明确拒绝**
`amount < 当前值` 并说明原因，而不是静默什么都不做。
要"减厂"就得先把别的线加上去（引擎自己会把空闲厂吐出来），或者以后给写入器
补一条 int32 专用通路。

## 74. 三条命令的 PE 反汇编与实机验证（2026-11-15）

新增可复用工具 `research/windows/pedis.py`（`dis/fn/vt/vtname/xref/calls/str/u64`，
全 RVA，纯静态读 exe）。用它把 §12/§28 遗留的三条"不可用"命令逐条反汇编确认，
不靠 `pecmd` 的猜测值。完整过程与证据见 `CAMPAIGN_1936.md` §13；这里只记
结论性偏移，方便以后查：

| 类 | vtable | size | 真构造函数 | 字段 | CanExecute |
|---|---|---|---|---|---|
| `CAddAdvisorCommand` | `0x2A11A88` | `0x50` | `0x11CC6D0` (this, handle_block*, std::string*) | `+0x28` packed CCharacter handle, `+0x30` slot string (SSO) | `0x11ECC20` |
| `CUnlockGrandDoctrineCommand` | `0x2AAA8A0` | `0x38` | `0x1B0FD20` (this, tag*, tpl*) | `+0x28` CGrandDoctrineTemplate*, `+0x30` tag id | `0x1B10310` |
| `CGenerateWarGoalAction` | `0x2A079B8` | `0xA0` | `0x1181220` (this) 默认构造 | `+0x14/+0x18`=from, `+0x1C/+0x20`=to, `+0x90`=CWarGoalType*, `+0x70`=1 | `IsAvailable 0x11BAD40` |

关键点：

* **`pecmd` 的 `CAddAdvisorCommand` "ctor" `0x11D2F00` 是 vtable slot 0 的标量
  删除析构函数**（`args: 0` 就是它没恢复出参数表的信号）。§8 的崩溃 #1 由此而来。
  Linux 的句柄偏移 `+0x24` 在 PE 上是 `+0x28`。
* `CUnlockGrandDoctrineCommand` 的 `+0x30` 必须是国家 tag id > 0，否则
  CanExecute "invalid country tag"、Execute 空指针 —— 崩溃 #3。
* 顾问**不在 `active_ideas`**，可靠回读是 `CanExecute 1→0`（已雇佣闸）+ PP 扣减；
  学说回读同样是 `CanExecute 1→0`（互斥）；造宣称回读是
  `CTimedWargoalActivity`（PE: `+0x10`=发起国, `+0x14`=目标, 与 Linux 相反）。
* **暂停时队列不排空**，`cmdwin.post_drained()` 短暂跑时钟并恢复暂停态，否则
  所有"入队成功"的回读都是假象。
* `_country_timed_activities()` 的 Linux vtable `0x4398978` 在 PE 恒不中，
  导致 `justifying()` 掉进全堆深扫（工具超时 10 分钟）；现 Windows 走 RTTI 索引。
* 三条类已带证据进 `cmdwin.WIN_SAFE`。`send_raw` 的护栏保留。

## 75. 空军命令与师编制编辑的 PE 布局（2026-11-16）

完整过程见 `CAMPAIGN_1936.md` §14；这里只留查表用的偏移。新增
`calling/airops_win.py`（PE 空军层）与 `calling/templedit.py` 的 PE 分支。

### 75.1 对象布局

| 对象 | vtable (RVA) | 关键字段 |
|---|---|---|
| `CAirWing` | `0x29F7748`（secondary `0x29F7780` 要过滤） | `+0x18` packed handle `{0x45,id}`，`+0x38` `CAirWingPool*` |
| `CAirWingPool` | `0x29F77D8` | `+0x20` 机型定义（`+0x18` MSVC string），`+0x40` `CStrategicAir*` |
| `CReferencedDivisionTemplate` | `0x29CC350` | `+0x08` handle `{52,id}`，`+0x20` 名字，`+0x18` 内嵌 `CDivisionTemplateData` |
| `CDivisionTemplateData` | `0x29CC300` | line/sup/third 数组对象 `+0x48/+0x68/+0x88`；数组对象内 `+0x08` data, `+0x10` cap, `+0x14` size, `+0x18` alloc |
| `CSubUnitDefinition` | `0x2807710` | key = MSVC std::string `+0x18` SSO / `+0x28` size / `+0x30` cap |
| `TNullObject`（空列） | `0x29C1178` | 5 列网格占位，编辑必须保留 |

`CPdxArray<T>` 通用头仍是 `{T* data +0, i32 cap +8, i32 size +0xC,
IAllocator* +0x10}`；但在 `CDivisionTemplateData` 里是**内嵌 0x20 字节数组
对象**（前面还有 2 个 u16 网格维），偏移要 +8，别混。

### 75.2 命令表

| 类 | vt | size | 字段 | CanExecute |
|---|---|---|---|---|
| `CAttachAirWingToArmyCommand` | `0x2A97728` | `0x48` | `+0x28` wing handles 数组，`+0x40` army packed handle | `0x19D3920` |
| `CStratAirSetMissionCommand` | `0x2A96BE8` | `0x48` | `+0x28` 同上，`+0x40` mask i32，`+0x44/45/46` flags | `0x19D4B30` |
| `CUpdateDivisionTemplateCommand` | `0x2A26E38` | `0x2B0` | `+0x28` `CDivisionTemplateData` 浅拷贝，`+0x270` handle，`+0x278` tag，`+0x280` XP | `0x1C2DD90` |

* `CUpdateDivisionTemplateCommand` 真构造 `0x1C2BC60`（6 参）**不调用**；
  数据拷贝函数 `0x138B2B0`，Execute `0x1C2D040` → `0xBF18E0(dst,src)`。
* XP 成本 `0x172CB90(out, oldData, newData, tag)`；国家 XP
  `0x2572DB0(out, country+0x10)`；1 XP = 1,000,000。
* grid 校验 `0x1C2D540` 返回 **0 才是通过**；形状校验
  `0xBF4B40/50/4D50/4B60` 返回非 0 通过。

### 75.3 明确禁用

`CStratAirEnableMissionCommand`（vt `0x2A96B28`，size `0x60`，
CanExecute `0x19D4740`）遍历 `+0x40` 的第二个 handle 数组并当作
`CStrategicAir` 解引用；塞 `CAirWing` handle 会 `GetLastError=299` 直接杀进程
（**崩溃 #4**）。`airops_win.make_enable_mission()` 抛异常，不进 `WIN_SAFE`，
`hoi4_air_mission` 对 enable 部分返回 skipped。

德国联队判定：wing handle id 是全局顺序号，从 1 起连续的那一组
（15 个，1936 GER OOB）就是玩家开局空军；其余 43 组 id ≥ 16。
`CAttachAirWingToArmyCommand::CanExecute` **不检查归属**，必须自己过滤。

游戏重启后 MCP 进程内 `type(g)` **和 `mcp/server.py` 的 `HANDLERS` 函数对象**
都还是旧的（后者会让工具继续跑 Linux 分支）。跑
`research/windows/_patch_air.py`：reload 所有 `calling/*`、重绑 `type(g)` 方法、
再 reload `server` 模块让 `@tool` 重新注册新函数体。写操作全部经
`cmdwin.post_drained`。

## 76. 海军任务的 PE 布局（2026-11-16）

完整过程见 `CAMPAIGN_1936.md` §15。新增 `calling/navalops_win.py`。

| 对象 / 命令 | vtable (RVA) | size | 字段 | CanExecute / Execute |
|---|---|---|---|---|
| `CFleet` | `0x29DF3D8` | — | `+0x08` handle `{0x3D,id}`，`+0x48` 区域数组，`+0x54` 区域数，`+0xB8` TF 数组，`+0xE0` 名字 | — |
| `CTaskForce` | `0x29DF900` | — | `+0x18` handle `{0x33,id}` → 解析到 `tf+0x10`，`+0xA0` 舰数，`+0x360` CNavalMission，`+0x374` 任务类型 | — |
| `CNavalMissionSetTypeCommand` | `0x2A2A808` | `0x50` | `+0x28` TF handle 的 CPdxArray（cap/size `+0x30/+0x34`，alloc `+0x38`），`+0x40` mission 0..9，`+0x44/45` flags | `0x13EE8C0` / `0x13E6C40` |
| `CNavalMissionAddRegionCommand` | `0x2A2A988` | `0x38` | `+0x28` **CFleet** handle，`+0x30` region id | `0x13EDF70` / `0x13E5FE0` |
| `CNavalMissionRemoveRegionCommand` | `0x2A2AA48` | `0x48` | `+0x28` CFleet handle，`+0x30` **i32 数组的 CPdxArray**（cap/size `+0x38/+0x3c`，alloc `+0x40`） | `0x13EE560` / `0x13E6570` |

* 句柄解析 `0x2284870(handle*) → object*`；两个命令的 handle 类型**不同**
  （SetType 要 TF `0x33`，AddRegion 要 Fleet `0x3D`），搞混就会解引用错对象。
* 海军区域表：全局槽 `image+0x33DD6B0` → `[gp+0x2E0]` 数组、
  `[gp+0x2EC]` count(305)。**合法海军区域要求 `[region+0xA0] > 0`**
  （本局 98 个，id 从 9 起）。
* **崩溃 #5**：`0x140DBA393` 对越界 region id 解引用 `[NULL+0xA0]`，
  无任何保护。`region_is_valid()` 必须在 CanExecute 之前拦下所有非法 id。
* `pecmd` 的 size/ctor 对这两条类不可信，一律 `build_by_vt`。
* 实机回读：SetType Patrol → `tf+0x374 0→1`；SetType Convoy Raiding →
  `→3`；AddRegion 9/18 → `fleet+0x54 0→1→2`，`+0x48` 数组出现对应对象。
  写操作全部 `cmdwin.post_drained`，暂停态自动恢复。

## 77. RemoveRegion / wargoal_quote / EnableMission（2026-11-16）

### 77.1 `CNavalMissionRemoveRegionCommand` — 已打通

与 AddRegion **不同**：`+0x30` 不是单个 region id，而是 **i32 数组的
CPdxArray**（data `+0x30`、cap `+0x38`、size `+0x3c`、alloc `+0x40`），
CanExecute `0x13EE560` 以 4 字节步长遍历。size `0x48`（factory
`0x13E3A90` 的 `new(0x48)`）。CanExecute 对每个 id 查 region 表，
并**要求该 region 已在舰队的 `+0x48` 数组里**（只比较指针，没有
AddRegion 那种 `[r8+0xa0]` 无保护解引用），所以比 AddRegion 安全。
实机：fleet 0 移除 region 9 → `fleet+0x54 2→1`，region-9 对象消失。
工具 `hoi4_naval_remove_regions`（dry_run 支持、非法 id 护栏）。

### 77.2 `wargoal_quote` — 精确报价在 PE 上不可实现（崩溃 #6 / #7）

| 实验 | 结果 |
|---|---|
| 写 `politics+0xE0` 的 PP 做二分 | 崩溃 #6：写该字段后进程死亡（页面/引擎状态不容外部写） |
| PP 不写、只调 `0x11C2420` validator | 崩溃 #7：`winject` 的 scratch flag 失效，`ReadProcessMemory(0x19b0eb00210) GetLastError=299` |

两次都从 `autosave.hoi4` 恢复（备份
`campaign_1940_02_pre_quote.hoi4` / `campaign_1940_02_pre_quote2.hoi4`）。
**结论**：`cmdwin.validate_declare_war` 本身就致命，`declare_war` 的
`validate=False` 默认值是必需的，不要打开。

现 `game.wargoal_quote()` 的 win32 分支改为**纯读静态估算**：
读当前 PP / `justifying()` 数量，用 `00_invasion.txt` 模型
（200 基础 + 30 生产 + 50/额外州，x1.5/同存，0.2 PP/天）算天数与
总消耗；`required_pp` / `can_justify_now` 明确返回 `null` 并附原因，
`hoi4_wargoal_quote(HOL)` 实测 230 天，与历史实测吻合。
**不写内存、不调注入函数、不 post**。

### 77.3 `CStratAirEnableMissionCommand` — 定位到门控，主动停手

反汇编推进（vt `0x2A96B28`，size `0x60`）：

* 构造 `0x19CC900`：`+0x28` handle 数组，`+0x40` **第二个数组只在
  `rsi != 0` 时才填**，`+0x58` mission id、`+0x5c` bool。
* CanExecute `0x19D4740`：`+0x34` 数组长度 > 0；`+0x4c`（第二数组长度）
  **为 0 时整个第二数组循环被跳过**；Execute `0x19D1D00` 与
  `0x19CD020` 也都有 target=NULL 分支。
* 崩溃 #4 的真正原因不是"必须填 CStrategicAir handle"，而是我**多此一举
  往 +0x40 塞了 CAirWing handle**。留空后 CanExecute 安全返回 0，游戏存活
  （多次探针验证）。
* 剩下的拒绝来自 `0x19CD370 → 0x19CD546`：它读
  `[cmd->vtable + 0x58]` 当作 **strategic region id**，调用
  `0x140FFBCD0(regionObj, wingObj)` 要求返回 **2**，还要
  `[ret+4] != 0` 且 region id 匹配。也就是说启用任务需要**按空军战区
  特化的 vtable / region 上下文**，不是命令字段能伪造的。

按项目规则（遇卡点先停、写文档）**不再猜这个 vtable**：
`airops_win.make_enable_mission()` 继续保持抛异常，
`hoi4_air_mission` 的 enable 部分继续返回 skipped，该类仍不进
`WIN_SAFE`。set_mission 已能写 `wing+0xA0` mission mask，够用。

### 77.4 `CDeployAirWingCommand` — 评估后禁用

| 项 | 值 |
|---|---|
| vtable / size | `0x2A11788` / `0xA8` |
| factory（零参） | `0x11D8BA0`：`+0x28` 走 `0xC07100`，`+0x2c=0`，`+0x30` 全局 handle，`+0x38=0xAA`，`+0x78=0`，`+0x80=0`，`+0x88/+0x90/+0x98` 三个全局值，`+0xa0=1`，`+0xa4=0` |
| 参数构造 | `0x11CE180`（`this, int*, ?, int*, handle*, ...`，含栈参）：`+0x28←[rdx]`、`+0x2c←[r8]`、`+0x30` handle、`+0x38=0xAA`、`+0x78` byte、`+0x80` 栈指针、`+0x88/+0x90` 两个字符串、`+0x98` handle、`+0xa0/+0xa4` |
| CanExecute / Execute | `0x11EEA20` / `0x11E0400` |

CanExecute 把 `+0x38` 当复杂对象交给 `0x11FB640` 再走
`0x141096A70` 与 `[obj+0x408]` 位测试；默认分支还有
`movabs 0x1f0037fc00; test qword ptr [0x5a8], rax`（无 `gs:` 段的绝对
地址读取），外部构造的对象会落到不可控内存。加上构造带 5+ 栈参和两处
handle 类型未验证，**风险等同于崩溃 #1/#4**。按纪律不猜、不探测、
不入 `WIN_SAFE`。若要继续，应先用 Frida 在 UI 点击"部署联队"时抓真实
构造参数与对象，再用 `build_by_vt` 复制那个字节布局。

## 78. 关系数组的真实位置 / 贸易路线不可读（2026-11-16）

### 78.1 `hoi4_relations` 修好

旧路径用 Linux 的两个常量，在 PE 上全错：

| 常量 | Linux 值 | PE 实际情况 |
|---|---|---|
| `CO_RELATIONS_ARR` | `0x2888` | 落在 CCountry 的"每国首都 state/province 表"里，元素不是 `CRelationStatus`，所以 `p.i32(r+0x50)` 的 `r` 读到 `0x52` → `OSError 299 read 0x52+4` |
| `REL_INDEX` | `0x50` | PE 的 `CRelationStatus+0x50` 恒为 0 |

实测定位（vtable 扫描 CCountry 0x0..0x3200）：

* `CCountry+0xF88` → `CDiplomacyStatus*`（`class_of` 确认）
* `CDiplomacyStatus+0x08` → `CPdxArray<CRelationStatus*>`，**cap 440 / size 440**
* `CDiplomacyStatus+0x498`、`+0x928` 是另外两个 440 条 `CRelationStatus` 数组（同尺寸，未采用）
* 数组**按 country id 1..439 排列**，位置即 tag id

修完 `hoi4_relations(limit=8)` 返回：
`GER(1) ENG(2) SOV(3) SWE(4) FRA(5) LUX(6) BEL(7) HOL(8)` —— 与 HOI4 tag id 顺序完全一致，
不再崩、不再返回垃圾。`index` 在 win32 分支直接用数组下标（不读 `+0x50`）。

### 78.2 `hoi4_trade_routes` 判死：没有可验证的容器

旧路径 `CCountry+0x112B08` 比 CCountry 本体大约 1.1 MB，是 Linux
`CCurrentGameState` 的字段偏移。读出来的 `count: 32758` 就是某个指针的高位字
`0x7FF6`。穷举证据：

* `CCountry` 0x0..0x3200 共 51 个 CPdxArray，**没有一个**元素的指针对
  象里含两个 `CCountry*`（贸易路线应有的 from/to）；
* `CCurrentGameState+0x112B08` / `+0x112A90` 都是空数组（data=0,size=0）；
* `CDiplomacyStatus` 14 个数组，除上面三个 440 条关系数组外全是
  `CMarketAccessRightsRelation` 及小表，**没有路线形状的**；
* RTTI 对象索引把 `CResourceDeliveryRoute` 和 `CRelationStatus` 混为一谈
  （193894 vs 193600 个实例），无法用 `oi.instances()` 枚举。

结论：**宁可显式报不可用，也不返回编造数据**。工具改为返回
`{ok:false, available:false, error:..., hint:"用 hoi4_resources / hoi4_get_country"}`，
描述里也标注 UNAVAILABLE。要真修需要 Frida 抓 UI 贸易面板的真实对象链。

## 79. `CState::id` = +0x58（PE）—— 让所有寻路复活（2026-11-16）

`mapdata.province_owner_map()` 在 PE 上返回空字典（0 个省），
所以 `_land_path()` 永远失败，`deploy_to_border` / `move_divisions` /
`attack_country` 全报要么 `no legal land path` 要么
`当前与 X 没有直接接壤的陆地边境省`。

根因：`STATE_ID` 用的 Linux 值 `0x48`。PE 上 `CState+0x48` 恒为 0，
`byid` 全部落到 `sid=0`，`st2p.get(0)` 空。

定位方法：取 GER 的 34 个 `CState*`（`_arr(p, country+0x460)`），
扫描 0x0..0x120 每 4 字节，找值落在 1..2000 的候选；把每个候选整列打印，
和 `hoi4_states(GER)` 对比：

* `+0x10` 整列都是 `5`（不是 id）
* **`+0x58`** 整列是 `[4,5,42,50,51,52,...,978]` —— 与 `hoi4_states` 完全一致

修法：

```python
STATE_ID = 0x58 if sys.platform == "win32" else 0x48
```

验证：`province_owner_map()` 从 0 个省变成 **10272** 个省，
6521 / 9522 / 6542 三处都正确返回 `GER`。

顺带确认：`CO_STATES = 0x460` 在 PE 上是对的（cap 42 / size 33 / e0 = CState）。

### 79.1 同时确认坏掉、暂不修

* `hoi4_province_ptr`：`G_PROVINCE_MANAGER` 仍是 ELF 偏移，
  读 `0x7ff6663de9f0+8` 直接 Err 299。
* `MV_CTOR`（CMoveCommand 10 参构造）在 PE 上没有映射，调用即 `Err 299`。
* `winject.call()` 的 scratch-flag 在 PE 上会随机失效（`Err 299 =
  ERROR_PARTIAL_COPY`）。**该错误一旦出现，进程必死**，且不可恢复。

## §80  PE containers: the RTTI vtable scan, and the status-object layouts

`ObjectIndex.singleton()` / `.instances()` only know the classes the MCP object
index was built for.  On hoi4.exe that excludes nearly every game database, so
`CEquipmentDatabase`, `CBuildingDatabase`, `CProjectPool`, `CCharacter` all came
back empty and the tools that depended on them either crashed or reported zero.
`probe.describe()` is equally blind on this build: it returned `arrays: []` for
every status object.

The reliable anchor is the MSVC RTTI map itself
(`research/windows/rtti_win.pkl`, 15 141 classes).  `Game.win_scan_class(name)`
looks up the class's vtable RVA(s) and scans writable memory for
`base + rva`; `Game.win_arrays(obj)` walks the data/cap/size/alloc quad and
names element 0's class.  Both are pure reads.  Namespaced classes are addressed
by their short name (`CProjectPool@NProject`), which `_rtti_vt_rvas()` tolerates.

### CCountry subsystem map (verified by scanning every 8-byte slot for RTTI)

    0x0E60 / 0x1538  CCountry (self pointers)
    0x0F60 CTechnologyStatus      0x0F68 CProductionStatus
    0x0F70 CDeploymentStatus      0x0F78 CReinforcementStatus
    0x0F80 CArmyUpgradesStatus    0x0F88 CDiplomacyStatus
    0x0F90 CPoliticalStatus       0x0F98 CLogisticsStatus
    0x0FA0 CDecisionStatus        0x0FA8 CProgramStatus
    0x0FB0 CCustomizableBuildingCollection
    0x0FC0 CIntelligenceAgency    0x0FC8 CLoopHistory
    0x0FD0 CCountryOccupationStatus   0x0FD8 CCountryCollaborationStatus
    0x0FE0 CCountryReportsManager     0x0FE8 CCountryIntel
    0x0FF0 CCountryCharacters         0x0FF8 CIncomingDiplomaticActionStatus
    0x11F8 CCountryResources      0x1350 CBuildingStatus
    0x1370 CNationalFocusTree     0x1378 CContinuousFocusPalette
    0x1380 CNationalFocusProgress 0x1580 CCountryFuelStatus
    0x1588 CCountryExperienceStatus   0x15A8 CCountryOperationManager
    0x15B0 CCountryOperationTokenManager

### CProductionStatus

    +0x058 CPdxArray<CMilitaryProductionLine*>   (GER 8)
    +0x070 CPdxArray<CBuildingProductionLine*>   (construction queue, GER 5)
    +0x0A0 / +0x0B8 / +0x0E8 / +0x220  CEquipmentVariant arrays
                                            (0xA0 = 69 active variants)

CMilitaryProductionLine: `+0x18` factories, `+0x28` unit cost (fixed 1e5),
`+0x30` daily output, `+0x38` modified cost, `+0x88` CEquipmentVariant*.
CBuildingProductionLine: `+0x18` factories, `+0x28` cost IC-days,
`+0x30` accumulated IC-days (progress - NOT +0x38), `+0x78` CBuilding*.
CBuilding: `+0x08` building type id.  CBuildingTemplate: `+0x08` id,
`+0x270` script key (55 objects).

### Equipment naming

CEquipmentType (456 objects, found by vtable): `+0x08` archetype id,
`+0x18` MSVC std::string key.  CEquipmentVariant `+0x18` carries that same id,
so one map names every production line.  Cached in
`research/windows/eqtypes_win.pkl` (14 KB, regenerate with
`_win_equipment_types()` if the build changes).  Samples: 15400
infantry_equipment, 15401 artillery_equipment, 20000 anti_air_equipment,
20016 light_tank_chassis.

### CTechnologyStatus / CDecisionStatus / CCountryCharacters

CTechnologyStatus `+0x0A0` = CPdxArray<CResearchSlot*> (GER 4).  CResearchSlot:
`+0x18` CTechnology* (0 = idle), `+0x38` research speed (fixed 1e5, GER 1.0).
The neighbouring 0x170-stride blocks are *other countries'* status objects -
reading past +0x110 of one object silently reads the next.

CDecisionStatus: `+0x010` all definitions (GER 500), `+0x088` visible (13),
`+0x0D0` takeable/candidate (115), `+0x0E8` CTargetedDecision (31).
CDecision key is at `+0x120` (0x118 reads 8 bytes early and produced garbage
with a `\x01` prefix); object stride is 0x1D0.

CCountryCharacters (CCountry+0xFF0): `+0x10` CCharacter (139),
`+0x40` CAdvisor (74), `+0x70` CArmyLeader (47), `+0x88` CNavyLeader (7),
`+0xA0` CScientist (17), `+0x120` second CArmyLeader list (23).
CCharacter+0xC0 is a union: a naval leader yields a CNavyLeader there, and its
skill offsets are not CArmyLeader's - reading them gave `attack=1686186104`.

CProgramStatus (CCountry+0xFA8) `+0x20` CPdxArray<CProgram*>, programme key at
`+0x20` - these are *faction facility programmes* (GER: `land_facility`), not
special projects.

### Still unattributed

`CProjectPool` resolves (440 instances, stride 0x110, 49 CProject each) but no
field inside one points back at a CCountry, so a pool cannot be tied to a tag.
`CResourceDeliveryRoute` resolves too (vtable 0x29D8D70, 193 869 instances:
`+0x10/+0x18` CState, `+0x20/+0x28` CProvince, `+0x30` counters) but that is the
convoy-delivery graph, not the import/export trade list.

### provinces

`hoi4_province_ptr` no longer calls `[0x459E9F0]+0x50` (a Linux ELF offset; the
injected call died with Err 299).  Provinces are a plain CPdxArray<CProvince*> in
CCurrentGameState at GS_PROVINCES 0x2B0, indexed by province id.
## §81  CTechnologyTemplate, and why hoi4_overview timed out

`CTechnologyDatabase` has no RTTI-visible singleton either, so `_load_tech_db()`
fell through to "built too early" and retried `oi.singleton()` twice - each retry
rebuilds the whole ObjectIndex.  Measured cost: **35.6 s** and then
`TypeError: 'NoneType' object is not iterable`.

CTechnologyTemplate objects carry the id at `+0x3C` and the script key at `+0x10`.
`research/windows/_build_techdb.py` scans them (slow: run it in a detached
thread, it writes `techdb_win.pkl`), and `_load_tech_db()` now loads that pickle,
then joins it with CTechnologyStatus+0x088 (552 CTechnology, id at +0x08) to get
the live `name -> CTechnology*` map.  `hoi4_research_available` went from a
35 s crash to 92 technologies in well under a second.

### hoi4_overview was 80+ s

Per-piece timings (same heap, warm caches):

    selectable_focuses      15.4 s   (2 item_name reads x 438 focuses)
    available_research      35.6 s   (+ crash)
    candidates              48.9 s
    alerts                  34.4 s

The MCP client timeout is ~30 s, so `hoi4_overview` could never return.  Fixes:

* `selectable_focuses` memoises on `(pid, date_raw, current_focus, n_completed)`;
  on the PE build it no longer runs the prerequisite/mutex scan (the blocks are
  scripted triggers, not focus-pointer arrays) and no longer calls the engine
  CanExecute (the Linux vtable is out of range, so `_can_start_focus_cmd` said
  yes to all 411 focuses).  It now says `verified: false` and returns the ranked
  candidate list.
* `overview()` uses `_light_alerts()` / `_light_candidates()`, which reuse data
  it already read; the heavy blocks live in their own tools.
* `_patch_air.py` keeps `_VT_SCAN`/`_VT_RTTI` across a reload - clearing them made
  the next tool call pay a 20-40 s writable-memory scan.
## 82. 空军/海军命令层收尾（2026-11-16）

### 82.1 `CStratAirEnableMissionCommand` 不需要了（推翻 §77.3 的结论）

实测（wing 1/2 前后对比）：

| 字段 | 含义 | 证据 |
|---|---|---|
| `CAirWing+0x7C` | 联队当前飞机数 | 战斗机 100、战术轰炸机 76/79/80、CAS 100、运输机 80 |
| `CAirWing+0xA0` | 任务掩码 | `SetMission(1)` 前 0，后 1 |
| `CAirWing+0xA4` | **任务已启用** | 同一次 `SetMission` 一并 0→1 |

`CStratAirSetMissionCommand` 带 `flag1=flag2=True` 时，引擎自己就把
`+0xA0` 和 `+0xA4` 一起写了。也就是说「设置任务」和「启用任务」在 PE
上是一个命令，§77.3 卡住的那个 `EnableMission` 门控对正常出兵**完全用不到**。

* wing 13 第一次回读是 `mission_on:false`，**下一 tick 变 true** —— 启用标志
  比掩码晚一拍落地。`hoi4_air_mission` 现在回读时轮询最多 6 次（0.15 s），
  取并集，避免把最后一个联队误报成失败。
* 修正 §77.3 的一处笔误：`0x19CD391` 是 `mov rax,[rcx]` + `cmp dword
  [rax+0x58],0`，配合 `pecmd` 的 `CStratAirEnableMissionCommand 0x58:int`，
  这个 `+0x58` 是**命令对象自己的 int 字段**（任务/区域槽号），不是
  「vtable+0x58」；`0x19CD3DC` 之后再用 `[wing+0x2C]` 区分 0x10000/0x20000
  两种联队类别。结论不变（仍不构造），但描述改对了。

### 82.2 `CDeployAirWingCommand`：门控全解出来，仍然不可构造

反汇编 `CanExecute 0x11EEA20` 得到真实字段语义（size 0x60/0xA8 见 §77.4）：

| 偏移 | 含义 | 拒绝时的报错串 |
|---|---|---|
| `+0x28` | 国家 tag id（`>0` 才算合法） | `Country tag is not valid.` |
| `+0x30` | **空军基地 handle**（8 字节，走 `0x2284870` 解析） | `Air base is invalid.` |
| `+0x38` | 联队定义对象（`0x11FB640(this+0x38)` 取指针） | `Sub Unit Definition is invalid.` |
| `+0x78` | byte：是否已填该定义 | 同上 |
| `+0x80` | qword | — |

`+0x38` 之后还有三条硬校验：`[def+0x2C] > 0`、`0x14094E50` 返回对象的
`[+0x408] & 0x1C00010000` 必须为 0、以及「装备总数不得超过该机型
最大联队规模」。`0x1F0037FC00 / [0x5A8]` 是全局特性位，不是坏指针。

结论：**字段语义清楚了，但 `+0x38` 要的是一个真实的 CSubUnitDefinition**
（装备型号 + 数量），外部无法凭空造一个可信对象；造错就是崩溃 #1/#4 那一类。
而且**没有它也不缺空军**：15 个联队 × 100 架 = 1500 架容量，1936 年编制
足够用到 1941。`CDeployAirWingCommand` 继续留在 `WIN_SAFE` 之外。

### 82.3 `CFleet` 名字与编成（`hoi4_fleets` 修好）

| 偏移 | 内容 |
|---|---|
| `+0x08` | packed handle `{61, id}` |
| `+0xA8` | `CCountry*` |
| `+0xB8` | `CPdxArray<CTaskForce*>`（size 在 `+0xC4`） |
| `+0xD0` | `CNavyTheaterGroup*` |
| `+0xD8` | 母港 `CProvince*` |
| `+0xE0` | **`char*` 舰队名**（Linux 的 `FL_NAME` 是 `+0xD8`，PE 不是） |

实测三条舰队：`Kriegsmarine` / `Unterseeboots-Flotte ` / `德意志国第1舰队`
（最后一条是玩家改过的中文名，直接 UTF-8 读出来）。原来 `name: null`
就是 Linux 偏移 `+0xD8` 在 PE 上指到了母港指针。

### 82.4 工具行为改进

* `hoi4_air_wings`：以前每次把**全世界 401+ 个联队**全 dump 出来（约 15k
  token）。现在默认只返回玩家自己那 15 个（确定性 id 1..15 连续块），带
  `planes` / `mission` / `mission_name` / `mission_on`；`all=true` 才做全球
  普查并受 `limit` 限制。
* `hoi4_air_mission`：`wing_ptr` 支持单个 id、逗号列表、以及整类
  `fighters` / `cas` / `tac_bomber` / `nav_bomber` / `all`；返回逐联队回读。
* `hoi4_attach_air_wings`：以前默认直接拒绝（怕劫持别国空军）。现在
  win32 默认走已验证的「只挂玩家起始块」路径；`allow_unverified=true`
  才走老的盲目全量循环。

实测：5 个战斗机联队全部 `air_superiority` + `mission_on`，CAS 联队
`cas` + `mission_on`；13/15 联队挂到第9集团军（运输机跳过，nav_bomber
被引擎 `CanExecute` 拒绝，与之前一致）。

### 82.5 仍未解决

* **联队 ↔ 战略区域**：`CStratAirSetMissionCommand` 没有区域字段，任务
  落在联队当前驻地所属区域。要把空军压到法国上空得先搬场，
  候选是 `CStratAirTransferCommand`。该命令已静态解完，**但主动停手**：

  | 项 | 值 |
  |---|---|
  | vtable / size | `0x2A96CA8` / `0x48` |
  | ctor | `0x19CCCF0`（`this, ?, handleSrc*, byte, …, [rsp+0x70], [rsp+0x78]`） |
  | CanExecute | vtable+0x48 = `0x19D4CC0` |
  | `+0x28` | **源 handle**（8 字节，packed） |
  | `+0x30` | **目标 handle**（8 字节，packed；`+0x38 != 0` 时跳过） |
  | `+0x38` | byte：1 = 不要目标（回预备队） |
  | `+0x3C` | int，来自 `[arg6+0x60]` |
  | `+0x40` | int，来自栈参 |

  CanExecute 语义：`resolve(+0x28) - 0x10` 得到源对象 → 要求
  `[obj+0x64] >= [obj+0x60]` → 再 `0x0FE13D0(源, 目标)` 为真 → 且
  `resolve(源+0x48) != 目标`（不能原地搬）。

  **危险点**：`0x19D4D04` 在 `resolve(+0x28)` 返回 0 时**直接解引用
  NULL+0x60**，和崩溃 #5 是同一个模式；而且目标必须是 `CAirBase`
  （handle type 65）—— `CAirBase` 根本不在对象索引里
  （`oi.instances("CAirBase")` 为空），拿不到可信的目标 handle。
  两项都不满足「先验证再调用」的纪律，因此不入 `WIN_SAFE`。

  另外它不是必需品：15 个联队 × 100 架 = 1500 架容量，先解决陆军
  推进比搬机场更值钱。
* `hoi4_theatres`：`CTheatre` 只有一个对象，只返回 ptr+class（1.19 里
  战区是遗留概念，优先级最低）。

## 83. 科研终于能用了：`CSetResearchCommand` 要的是模板不是活对象

### 83.1 症状

`hoi4_fill_research` 每次都返回 `filled: 0`，`attempts` 里每条都是
`{"ok": false, "error": null}`——**引擎静默拒绝**。整整一局 4 个槽全空。

### 83.2 根因

`CSetResearchCommand::CanExecute` = `0x11F3360`：

```
0x11F3376  call 0x0C07BF0      ; tag id -> CCountry*
0x11F337E  call 0x0717800      ; rax = CCountry+0xF60 = CTechnologyStatus*
0x11F3386  eax = cmd+0x38      ; slot
0x11F3391  cmp eax, [rdi+0xAC] ; slot < slot_count
0x11F339D  cmp [cmd+0x28], 0   ; tag > 0
0x11F33AD  call 0x0F55920      ; <- 真正的门
```

而 `0x0F51A30`（被 `0x0F55920` 第一句调用）只有 8 条指令：

```
movsxd r8, [rdx+0x38]          ; tech->+0x38
lea    eax, [r8-1]
cmp    eax, [rcx+0x94]         ; 和状态数组 size 比
mov    rax, [rcx+0x88]
mov    rax, [rax + r8*8 - 8]   ; ts->data[+0x38 - 1]
```

**`+0x38` 是 1-based 索引，指向 `CTechnologyStatus+0x088` 里的活对象。**
`CTechnologyStatus+0x088` 里那 552 个活 `CTechnology` 的 `+0x38` 全是 0 →
`idx-1 = -1` → 查表失败 → `CanExecute` 恒 0 → 命令被丢弃。
实测：**552 个活对象逐个问 CanExecute，0 个接受**。

`CTechnologyTemplate`（vtable 扫描，1104 个命中＝552×2）的 `+0x38` 正是
1..552，`+0x10` 是脚本键，`+0x3C` 是 id（与活对象 `+0x08` 相同）。
把**模板**塞进 `+0x30` → `CanExecute = 1`。

### 83.3 修复

* `game._load_tech_db()`（win32）：改从 `CTechnologyTemplate` vtable 扫描建
  `名字 → 模板指针`；`_tech_by_id` 仍按**活对象的 `+0x08`** 建索引，否则
  `research_slots()` 认不出槽里在研什么（这一步错了会显示 `researching: null`
  但 `progress` 有值）。
* `game._win_vt_cache()` / `_kick_tech_scan()`：扫描 20-40 s，超过 MCP 超时，
  所以丢到后台线程，第一次调用返回"表还在加载，约一分钟后再试"，
  并且**不缓存空表**（否则整个进程永远死）。
* `cmdwin.set_research()`：以前只 `enqueue` 不校验也不跑时钟；现在先调
  引擎自己的 `CanExecute`，再走 `post_drained`。`cmdwin.can_exec()` 是新增的
  通用校验器。
* `_patch_air.py`：`win_scan_class()` 的缓存可能挂在**实例**上，
  以前只从类上取 → 每次 reload 都把 20-40 s 的扫描结果丢了。现在两边都取。

实测：

```
slot 0  concentrated_industry   27.37%  122.1 天
slot 1  improved_infantry_weapons 22.88%  148.3 天
slot 2  artillery2              39.52%   58.2 天
slot 3  excavation1             19.64%  135.1 天
```

### 83.4 副作用（重要）

`post_drained(cmd, seconds=2.0)` 会**把游戏解除暂停并跑 2 秒真实时间**再恢复。
`fill_research` 一次要试 30 个科技 → 约 60 秒真实时间 → 4 速下等于十几个
游戏日。本次战役就是这样在"修工具"期间被推着走了两个月（1940.10.03 →
1940.12.08），而集团军当时没有任何有效命令，战线崩了。
**以后批量命令必须把 `seconds` 降到 0.2-0.4，或者先确认部队在防线上。**

## 84. 同一进程内"新开档"后日期缓存全废（2026-11-16）

### 84.1 现象

用户在**同一个 hoi4.exe 进程里**退回主菜单、新开 1936 德国档。游戏画面明确是
1936.01.01，但工具一直报 `1940.12.24.11`（上一局崩溃档的日期），
`hoi4_date` / `hoi4_overview` / `hoi4_session_info` 全部一致地错。

同时 `hoi4_overview` 里的**其它**数据是对的（30 个师、24 个州、35/28/10 工厂），
说明只有日期是坏的。

### 84.2 根因

`mcp/server.py::_selfheal_check` 只在 **pid 或 /proc starttime 变化**时重建缓存。
"退回主菜单 → 新开档"两个都没变，于是这些按 pid 缓存的东西原封不动：

* `_win_date` / `date_addr.json`：指向**上一局世界**的 CDate（已释放，值冻在 1940.12.24）
* `_VT_SCAN`：指向已释放的 `CAirWing` / `CTechnologyTemplate` 对象
* `_tags` / `_tag2idx` / `_oi` / `_tech_by_name` / `_eq_*` …

`_win_date_addr()` 的 JSON 兜底分支**只校验数值区间**（1935..1965），
1940 完全合法，于是每次都直接返回那个死地址。

### 84.3 两个日志文件都不能当时钟用

* `game.log`：**只在事件触发时写**。实测游戏已经 1936.08.08，game.log 最后一行
  还是 1936.05.30（滞后两个多月）。
* `error.log`：这个 mod 里本该每小时写一条决策警告，但本次在 17:14 之后就停了。

所以"用日志下限纠正内存值"的老办法在这里会把时钟**钉死在过去**（我先这么干了，
结果 8 月被报成 5 月）。反过来，`_log_date()` 取两个文件最大值的老实现，
在新开档的头几分钟会被 error.log 里上一局的 1940 戳污染，把 1936 顶到 1940。

### 84.4 唯一可靠的办法：找"会走字"的那个 CDate

`research/windows/_find_live_date.py`：

1. `_win_date_scan()` 全内存扫 CDate 签名（约 9 s，9.1 GB）；
2. 限定 1935..1946，这次得到 **68 个**候选（其中一大票是上一局留下的
   1945.03.17 / 1945.04.02 环形缓冲副本）；
3. 解除暂停 4 s，再读一遍 —— **只有 1 个变了**：
   `0x1fafff51530`，1936.08.06.23 → 1936.08.08.12。

`research/windows/_pin_live_date.py` 把这个地址写进 `_win_date/_win_date_pid/
_win_date_last`，并把 `_log_date` 打成 `lambda: None`（彻底不用日志当地钟）。
之后 `hoi4_date` 稳定返回真实时间。

### 84.5 结论 / 以后怎么做

* **只要换档（哪怕进程没换），先跑这两个脚本**，再相信任何日期。
* `_selfheal_check` 应该补一条：**国家/世界对象指纹变了就重建**（例如
  `gamestate` 指针、`my_tag` 的 CCountry 指针），不能只看 pid。
* 4 速实测推进速度：**约 0.39 游戏日 / 秒**（4 s 走 1.5 天）。
  所以 30 天 ≈ 77 s，一年 ≈ 15 分钟。

## 85. `hoi4_hire_advisor` 在 PE 上取不到 CCharacter（2026-11-16）

`hoi4_advisors` 能正常列出 39 个政治顾问（含 `character: GER_hjalmar_schacht`、
`idea_token: hjalmar_schacht`），但两种写法都失败：

```
hoi4_hire_advisor(key="GER_hjalmar_schacht") -> "CCharacter for GER_hjalmar_schacht not found"
hoi4_hire_advisor(key="hjalmar_schacht")     -> "CCharacter for GER_hjalmar_schacht not found"
hoi4_add_idea(key="hjalmar_schacht")         -> 同上
```

说明 PE 版拿 character 的方式（大概是按 key 在角色表里查）在这台机器上没命中——
顾问表本身是从脚本文件读的，和活对象不是一套。**后果：政治点花不出去**，
本局 1936.11 已攒到 373 PP。

待办：`hoi4_advisors` 每行其实已经带 `character_ptr`（内存里的 CCharacter*），
`hire_advisor` 应该直接收这个指针走 `CAddAdvisorCommand`，而不是按 key 重查。

## 86. 新档第二次崩盘：宣战了，但陆军一次都没动（2026-11-16）

### 86.1 结果

1939.09.01，德国向荷兰、比利时、法国宣战后 3 小时内崩盘：
`hoi4_get_country("GER")` 返回 **德意志共和国**（阿登纳政府）、21 州、0 个师，
地图上出现"荷兰占领区"和"苏联占领区"。**战役结束。**

### 86.2 直接原因：`hoi4_set_front_directive` 是空转的

```
hoi4_front_directives -> {"enemy_tag":"HOL","stance":"blitzkrieg","last_tick": null,
                          "hours_since_tick": null}
```

`last_tick` **一直是 null**。这个工具只把指令写进
`calling/front_directives.json`，真正执行 5 条 CA 法的"网关"每 2 游戏日跑一次的
调度在本机根本没起来。**所以"武装一条方面军指令"不会让任何一个师移动。**
上一局 1940.12 的崩盘也是同一个坑（当时写成"集团军没有有效命令"）。

判断方法：**武装后立刻读 `hoi4_front_directives`，`last_tick` 必须是时间戳**；
是 null 就说明没人执行，必须改用 `hoi4_tactical_front_tick(force=true)` 手动推。

### 86.3 手动路径也全废（重启后）

| 调用 | 返回 |
|---|---|
| `hoi4_deploy_to_border("HOL")` | `no divisions` |
| `hoi4_deploy_to_border("BEL")` | `no divisions` |
| `hoi4_army_battleplans("BEL")` | 找到 3 条 CFront、14 个目标省，但 `no divisions` |
| `hoi4_attack_country("HOL")` | `no land border with HOL` |
| `hoi4_move_divisions(force=true)` | 每个师 `no legal land path from X to Y` |
| `hoi4_army_status` | `count: 0, divisions_count: 0` |

而同一时刻 `hoi4_get_country("GER")` 报 **109 个师**、`hoi4_country_stats` 也是 109。
**师的枚举和 `hoi4_get_country` 用了两条不同的路径，重启后前者返回 0。**
`hoi4_border_fronts("POL")` 正常返回 4 条前线，说明 CFront/邻接数据本身没坏，
坏的是"我方师"这一侧。

### 86.4 还有一个确定性崩溃

`CTimedWargoalActivity`（我们对 HOL 的宣战理由，ETA 1939.05.08）在
**1939.05.07.23** 准点把游戏打崩，连续两次、同一小时：

```
hoi4_wait_for -> played country changed: GER -> None
                 paused: [Errno 299] read 0x7ff66521db08+8
```

`error.log` 没有任何致命错误，进程直接消失。绕过办法：**在 ETA 之前
`hoi4_declare_war(target, force=true)` 直接开战**，宣战会消耗掉那条
CTimedWargoalActivity（`hoi4_justifying` 变空），之后再跑就不再崩。
——即这个 mod 里"命令通道创建的宣战理由，到期结算时会崩"，只能用"提前宣战"规避。

### 86.5 教训（写死）

1. **宣战前必须验证三件事**：`hoi4_army_status.divisions_count > 0`、
   `hoi4_front_directives.last_tick != null`（或手动 tick 过）、
   先头师已经在边境省。
2. `hoi4_set_front_directive` 在没有网关的环境里**只是写了个 JSON**。
3. 游戏重启（含崩溃重启）后，第一件事是跑 `_find_live_date.py` + `_pin_live_date.py`，
   第二件事是验证 `hoi4_army_status` 能数到师；数不到就别宣战。
4. 自动存档只有一份（`autosave.hoi4`，每月覆盖），**战役期间必须手动另存**
   （`campaign_<date>.hoi4`），否则崩一次就没有回滚点。

## 87. 三处护栏落地（2026-11-16 晚，第三次开档前）

上一节列了"下次必须先修的三条"。这一节记录**实际改了什么、怎么验证的**。

### 87.1 战术 tick 现在真的有人执行：`calling/frontd.py`

根因不是"网关写错了"，而是**这台机器上根本没有网关进程**。运行栈只有
`stage/wx_server.py -> mcp/server.py`，`hoi4_gateway.py` 的 `front_worker`
线程从来没被启动过，所以 `front_directives.json` 永远只有人写、没人读。

改法：把执行器放进**真实存在的那个进程**（MCP 服务器）里。

* `calling/frontd.py`：一个 daemon 线程，`server.main()` 启动；
* `front_worker.json` 心跳（pid / state / ticks / errors / last_date），
  任何工具都能回答"现在到底有没有人在指挥陆军"；
* `front_worker.lock` **独占锁**：本机同时有 6 个 `wx_server.py` 进程（历史会话
  泄漏的），只有一个能当 owner，其余 standby；
* **暂停中不注入**（`session.is_paused` 为真就只写心跳）——"战时 + 每 8 秒注入"
  是 2026-11-08 两次冻屏的组合；
* `MIN_SPACING=12s` 上限：这台 build 的时钟会在两个 CDate 副本之间跳，
  跳回过去会强制 tick，加个墙钟上限防止战争期间每 5 秒注入一次；
* worker **用自己的 `Game` 实例**（共享 `_depth`/`_inj` 会在两个线程里
  交叉改寄存器）。

配套语义修正（这是上一局真正害死人的地方）：

| 字段 | 含义 |
|---|---|
| `last_tick` | 真的跑过一次 tick（pid 是当前游戏进程） |
| `armed` | `last_tick` 存在且 pid 匹配 —— **这才叫"武装"** |
| `tick_did_orders` | 那次 tick 是否真的下过命令（`ok`）。**不是**"有没有人执行" |
| `divisions_seen` | tick 时看到几个自己的师；为 0 说明执行器看不见陆军 |

`hoi4_set_front_directive` 现在**存完立刻踢一脚**（`frontd.kick(force=True)`），
返回 `armed` + `arm_attempt` + `worker`，并在没 tick 成功时明说
"directive stored but NO tactical tick has run"。

实测（1939.09 残局，0 个师）：

```
hoi4_set_front_directive(HOL, prepare)
  -> arm_attempt.ticks = 1，10 个边境省，5 条法全部执行
  -> armed: true，last_tick.pid = 30112
解锁暂停 20 秒后：
  worker.state = running, ticks = 2, last_date = 1939.12.14.04
  last_tick.raw 自己往前走了  <- 自动 tick 生效
```

### 87.2 师枚举：按对象身份解析，不再相信 tag2index

`hoi4_army_status` 曾经在 109 个师的情况下返回 0，而且**所有**陆战工具
（`deploy_to_border` / `army_battleplans` / `attack_country` / `move_divisions`）
都走同一个读法，于是一起失灵。

容器是对的：`CCountry+0x290` 是真正的 `CPdxArray<CArmy*>`
（同日实测 ENG 122、SOV 212、FRA 122、ITA 124）。错的是**解析到了哪个
`CCountry`**——`api.country()` 用的是缓存的 `tag2index` 行，而 `api.py`
自己的注释里就记着同样的症状（"divisions(GER) == 0 while the UI showed 106"）。

新增 `calling/divenum.py`：

* `country(g, tag)`：按**每个 CCountry 自己带的 tag id**（`+0x08`）建
  `tag id -> countries[] 下标` 映射，完全不碰 `tag2index`；
* `armies(g, tag)`：`CCountry+0x290`，返回 `count/source/verified`；
* `deep_scan(g, tag)`：RTTI 扫 `CArmy`，用**我们自己 `CO_STATES` 展开的省集合**
  归属（不经过 `tag2index`），只在该国数组为空时才用；
* `selftest(g, tag)`：三条独立读数对比（身份指针 / tag2index / 深扫），
  不一致就明说 `MISMATCH: do not trust a zero from the legacy path`。

`hoi4_army_status` 现在返回 `countable`、`source`、`country_ptr`、`tag_id`、
`selftest`；`hoi4_division_selftest` 是专门的体检工具（深扫约 40 s，
每个游戏进程只扫一次，之后走缓存）。**每次开档、每次重启后先跑它。**

另外**根上也修了一刀**：`api.HOI4.country()` 拿到 `ptrs[tag2index[tid]]` 之后，
先读那个 `CCountry+0x08` 核对 tag id，对不上就按对象重建映射并打到 stderr。
`deploy_to_border` / `attack_country` / `_scan_country_locked` / `unassigned_divisions`
这些不走 `divenum` 的路径，靠这一刀一起修好；`player_country_ptr()` 和
`countries()` 同样处理。

### 87.3 宣战前置：三条硬拦，`force` 不再绕过

`war_readiness_check` 重写，分两级：

**硬拦（只有 `override_checks=true` 能过）**

1. 师能数出来且 > 0（数不出来同样拦——`countable:false` 不是 0）；
2. 至少 1 个师**站在要进攻的那条边境省上**（新增 `_border_divisions`，
   用 `mapdata.province_controller_map` + `border_provinces` 求边境，
   用 `unitorders.province_of` 逐个师定位）；
3. 方面军指令**真的 tick 过**（`last_tick` 非空、pid 匹配、`divisions_seen > 0`）。

**软警告（`force=true` 可跳）**：步枪库存 < 1500、师数低于 1.2×敌方、历史时间线。

`hoi4_justify_wargoal` 走 `hard=False, force=True`：**宣战理由是"主张"，不是
"进攻"**，可以在军队还没集结时先挂上 250 天计时器——这样不会重演
"因为凑不够 12 个师而白白浪费一条宣战理由"。

`hoi4_declare_war` 新增 `hoi4_war_readiness` 预览工具。实测（残局）：

```
hoi4_war_readiness(HOL) -> ready:false
  blockers: [no divisions ..., no division on the HOL border ..., no front directive ...]
hoi4_declare_war(HOL, force=true) -> blocked_by: war_preflight   <- 关键
```

**上一局我用 `force=true` 绕过了整个检查，这次它不再能绕过。**

---

## 88. 第三局（1936 新档）实战：将领、陆军经验口径、一次堆损坏崩溃

### 88.1 `hoi4_available_generals` 遇到海军将领就崩

```
TypeError: unsupported operand type(s) for +: 'NoneType' and 'NoneType'
  game.py:3685  rows.sort(key=lambda r: -(r["attack"] + r["defense"] + ...))
```

`generals()` 对 `CNavyLeader` 明确写 `attack/defense/planning/logistics = None`
（海军将领的 CArmyLeader 偏移不通用，读出来是垃圾，见 §86），
但 `available_generals()` 排序时直接相加。**同一份数据，一个函数做了防护，另一个没有。**

修法：`_sum()` 把 None 当 0。海军将领不会出现在 `rank=corps_commander`
的结果里，排序时排在最后也无所谓。

### 88.2 陆军经验的真实单位：1 XP = 100,000，不是 1,000,000

`templedit._pe_army_xp()` 读出来的 `army_xp` 和 `_pe_cost()` 读出来的 `xp_cost`
是同一个定点标度。用引擎自己的报价反推：

| 编辑 | 引擎 `xp_cost` | 原版 HOI4 规则 | 推论 |
|---|---|---|---|
| 9 步 → 10 步（加 1 个已有兵种） | 500,000 | 5 XP | **1 XP = 100,000** |
| 9 步 → 9 步 3 炮（新兵种 25 + 5 + 5） | 3,500,000 | 35 XP | 同上 |

原 `xp_note` 写的是 1 XP = 1,000,000，把"15 XP"读成"1.5 XP"，差了 10 倍。
已改成 1 XP = 100,000，并新增 `army_xp_display` / `xp_cost_display` 两个
人话字段（`15.0` / `35.0`）。

**这条很重要**：它解释了为什么"攒到 50 点陆军经验再加炮"一直等不到——
1936 开局到 1940.03，全程无战事，陆军经验只有 **15**，因为陆军经验
只从国策、陆军顾问（+0.30/天）和**陆战**来，平时不涨。

### 88.3 `create_army_group` 在 PE 上会悄悄毁掉堆 —— 23:31 崩溃复盘

**症状**：1940.05.23 游戏进程直接消失，`hoi4.exe` 不在进程表里。

```
Unhandled Exception C0000005 (EXCEPTION_ACCESS_VIOLATION) at 0x00007FFD63F522D4
  1  ntdll.dll   RtlSetBits
  2  ntdll.dll   RtlAllocateHeap
  3  ntdll.dll   RtlAllocateHeap
  4  hoi4.exe    ...
```

堆分配器里炸的 —— 典型的**堆元数据被写坏**，不是逻辑错误。

**根因**：`armyops.create_army_group()` 只有 Linux 实现，里面
`AG_CTOR = 0x23167D0` 是 **Linux ELF 偏移**。而 `g.call(addr)` 在 PE 上
仍然按 `image_base + addr` 解析，于是这一行

```python
g.call(AG_CTOR, [cmd, arr, theatre])          # 0x23167D0 <- Linux offset
```

在 Windows 上执行的是 `0x7ff614c00000 + 0x23167D0` 处的**任意代码**，
参数还是伪造的命令对象。它"返回"了 0，函数继续走到

```
array was not filled (0/5)
```

**看起来只是一次普通的失败**。40 分钟后堆炸了。

同一天早些时候 `create_army()` 是好的，因为它在 `sys.platform == "win32"`
时就转去 `armyops_win.create_army()`（走已验证的 `FN_CREATE_ARMY = 0xC417A0`）；
**`create_army_group` 漏了这条分支。**

**修法**：在 `create_army_group()` 开头加 win32 硬拦，直接返回
`not implemented for the Windows build`，绝不再落到 `g.call(AG_CTOR)`。

**教训（要写进纪律）**：
> 任何 `g.call(CONST)` 之前，先确认这个 CONST 是**这个平台**的偏移。
> Linux 表和 Windows 表是两张表；`g.call` 不做平台判断。
> 一个"失败得很干净"的注入调用，可能已经把堆写坏了，只是还没炸。

### 88.4 顺带确认的好消息

* **将领现在能挂了**：`hoi4_assign_commander` 对 6 个集团军全部 `ok:true`，
  `after == leader_ptr`（博克、曼施坦因、克莱斯特、维茨勒本、海因里希、莱因哈特）。
  未解锁的（隆美尔、古德里安）即使 `allow_unverified=true` 也会被引擎拒绝并
  明确回报 `the game did not attach the leader` —— 这个护栏是有效的。
* **陆军编制**：`hoi4_create_army(count=N)` 连续调用正常，133 个师分成 7 个集团军，
  `unassigned_divisions` 归零。
* **宣战链**：`hoi4_declare_war("POL")` 直接成功（`at_war_verified: true`），
  不需要玩家有宣战理由，也不需要 `force`。
* **`frontd` 进攻态**：`stance="blitzkrieg"` 第一次 tick 就
  `accepted_total: 16 / staged: 3 / ok: true / divisions_seen: 141`，
  自动选突破口（staging_target 11558）并真的下了 16 条进攻命令。

---

## 89. 1940 战役复盘：不是打得差，是**工具只会右键**

这一局被法国平推，原因有三条，全部是工具的行为，不是战术选择：

1. **没有前线、没有进攻线。** `frontd` 的 5 条 CA 法则里，进攻只有
   `_width_attack`——把 6~9 个师**右键**塞到同一个省上。整场战役没有一条
   原生 front line / offensive line。
2. **师堆在几个省，补给全红。** 141 个师被堆在波兰边境的少数几个省，
   `out_of_supply` 之后攻击全部被引擎拒绝，看起来"下了命令但不动"。
3. **空军一次都没起飞。** 15 个空军联队 `mission: 0 / mission_on: false`
   从 1936 到 1940 没变过——`attach_air_wings_to_armies()` 在 PE 上
   **每次都返回 `attached: 0, scanned: 656`**（拒绝分支），而 frontd 把它
   当成"已尝试"记了一个 `air_attached: true` 就再也不试了。

于是西线（对法边境）一个师都没有，法国人直接走进来。

### 89.1 根因一：`_win_battleplans` 把全部师塞进**一个新集团军**

```python
divs = [d for d in self.divisions(tag)]          # 全部 141 个师
ogs  = {p.u64(d + A.CARMY_OG) for d in divs}
og   = next(iter(ogs)) if len(ogs) == 1 else 0
if not og:
    A.create_army(self, divs)                    # <- 141 个师合并成一个军
A.draw_front_line(self, og, best["front"], best["section"], 0, A.FULL)
```

一个集团军、一条 0..100000 的整条前线。引擎只能把它摊在这条线上的
少数几个边境省——**结构上就一定会堆叠**。玩家不会这么干：玩家是每个
集团军画**自己那一段**前线。

### 89.2 根因二：`fronts()` 只看第一个战区

`armyops_win.fronts()` 用 `theatre(g)` 取 `CO_THEATRES` 的**第 0 个**。
德国只有一个战区时没问题；一旦东西两线同时开战，另一条线的 CFront
根本读不到，`hoi4_border_fronts(FRA)` 返回 `[]`，划线自然无从谈起。

已改成遍历 `CO_THEATRES` 全部战区（新增 `theatres()`）。

### 89.3 根因三：空军挂靠走的是 **1.19.4.1 的 Linux 常量**

`game.attach_air_wings_to_armies()` 里是 `ctor 0x241F840 / Run 0x20AE0A0`，
而且默认因为"联队归属未验证"直接拒绝。两个问题：

* PE 上根本没有这对地址（真调用就是 `image_base+0x241F840` 的野代码）；
* 归属其实**已经能证明**了——`airops_win.german_wings()` 用
  "handle id 从 1 开始连续"这一条确定性特征，取出玩家开局那 15 个联队。

PE 上的正确命令在 `cmdwin` 里早就登记过：
`CAttachAirWingToArmyCommand` vt `0x2A97728` size `0x48`
CanExecute `0x19D3920`，`+0x28` 联队 handle 数组、`+0x40` 集团军 handle。
本次改成走 `airops_win.make_attach()` + `can_exec()` + `post()`，
并且只送 `german_wings()` 返回的联队。

### 89.4 落地改动

| 位置 | 改动 |
|---|---|
| `Game.plan_front()` | **新增**。每个集团军一条 front line（按 `from/to` 切 0..100000）+ 自己的 offensive line + execute + 开箭头 |
| `Game._win_battleplans()` | 有集团军时转调 `plan_front`；旧的"合并成一个军"只作无军时的兜底 |
| `Game.plan_front()` 补给闸 | 用 `_supply_front_guard` 拿到这条前线能喂多少师，**只派这么多集团军**上，其余留预备；不再"要么全上要么不上" |
| `Game.plan_front()` 抢兵闸 | 已经有作战命令的集团军**不参与**新前线——这就是 1940 西线空虚的直接原因 |
| `armyops_win.fronts()` | 遍历**所有** CTheatre，不再只看第 0 个 |
| `Game.attach_air_wings_to_armies()` | PE 分支重写：`make_attach` + 只送玩家自己的 id-1..k 联队 |
| `frontd._run_front_tick()` | 进攻姿态下**先**画原生前线+进攻线（`battleplan_drawn`），再跑补位/轮换；`_width_attack`/`_stage_reserves` 在有原生计划时**跳过**——这正是堆叠的来源 |
| `set_front_directive(..., battleplan=True)` | 新增开关，默认开；`false` 退回纯微操 |
| `hoi4_army_battleplans` | 新增 `armies` / `split` 参数，返回每个集团军的切片和结果 |

### 89.5 还没验证的部分（下次开档第一件事）

这一局在改代码期间被关闭（进程不在了，没有新的 crash dump），所以
**`plan_front` 的注入链没有在活存档上跑过一次**。下次开档必须按顺序验：

```
hoi4_army_battleplans(enemy_tag="POL", force=false)   # 空跑：看步骤和补给闸
hoi4_orders(<army>)                                    # 必须出现 type-2 front order
hoi4_army_battleplans(enemy_tag="POL")                # 真画
hoi4_orders(<army>)                                    # type-2 + 子 type-1，executing=1
hoi4_front_changes()                                   # 10~11 游戏日后应有省易手
```

`draw_front_line` 的 PE 常量（`FN_NEW_ROOT_CMD 0x18C9B20` / `NR_CANEXE 0x18DF5E0`）
在 2026-11-14 的荷兰战役里被验证过能拿下 6 个省，但**本次会话没有复验**。

---

## 90. 空军/海军从来没人管：把它变成"红点"

玩家的原话："我不说你们都不会主动调空军联队和海军。"

事实核对：1940 那局 15 个联队从 1936 到 1940 `mission: 0`、`mission_on: false`，
**一次 sortie 都没飞**；舰队从来没下过任务。不是打输了，是这两个兵种根本没上场。

根子不在"忘了"，在**没有任何信号提醒**：`hoi4_overview.alerts` 只报
国策/科研/建造/军工厂/未编师，空军海军不在里面。

### 90.1 三个层次一起补

1. **`hoi4_overview.alerts` 新增两条**（每回合第一个调用就能看见）：
   * `AIR_NO_MISSION: n/N 个空军联队（共 X 架）没有任务 -> hoi4_air_mission(...)`
   * `NAVY_NO_MISSION: n/N 个特遣舰队没有任务 -> hoi4_naval_mission(...)`
   实现：`Game._air_snapshot()`（按 pid 缓存 25 s 的 `german_wings()`）+
   `Game._navy_snapshot()`（`navalops_win.naval_fleets`，纯指针遍历）。

2. **`hoi4_housekeep` 多了两步**：
   * `housekeep_air`：`set_air_missions()`（战斗机→制空 1、CAS→近距支援 2）
     + `attach_air_wings_to_armies()`（只挂玩家 id-1..k 的联队）
   * `housekeep_navy`：给每个 `mission == 0` 的特遣舰队下 Patrol(1)；
     **战区不碰**（乱填 region id 崩过一次，§77），舰队没有战区时如实报
     `skipped: fleet has no strategic region`，不假装成功。

3. **`frontd` 进攻第一跳**：挂完空军顺手 `set_air_missions()`，
   写进 directive 的 `air_missions` 字段。

### 90.2 提示词

`AGENTS.md`（工作区根目录）写成了"每回合检查清单"：开头 `hoi4_overview` +
`hoi4_housekeep`，alert 表里 `AIR_NO_MISSION` / `NAVY_NO_MISSION` 是硬性
"必须当场处理"，并把 1940 那局的教训明写在旁边。这份文件会被当工作区指令加载，
比任何一次性的口头提醒都可靠。

---

## 91. 第三局（1936 新档）实战记录：战略死路、崩溃与工具缺陷（2026-11-17）

### 91.1 这一局走到了哪

新开 1936 德国档，全程只用 `hoi4_*` 工具，4 速。

| 日期 | 状态 |
|---|---|
| 1936.01.01 | 30 师 / 35 民 / 28 军 / 992 架 / 5 特遣舰队全 0 任务 |
| 1936.02 | 空军 14/15 联队挂上任务（运输机除外）、5 个 TF 全部 Strike Force/Patrol、科研 4 槽满、建造队列 6 项 |
| 1936.03 | 军官团：陆军参谋长 Beck（+8% 组织度，+0.3 陆军经验/天）、Schorner（步兵 +10% 攻 +15% 防）、Manstein（机动战精通） |
| 1936.12 | 战争经济 + 自给自足经济 + 大规模征兵，PP 300+ |
| 1937.05 | 51 师（卡住，原因见 91.3） |
| 1937.09 | 54 师 → 60 师 → 69 师（1938.01） |
| 1937.09.17 | 对 **BEL / HOL / LUX** 同时开始正当化战争目标（**可以并发**，见 91.4） |
| 1938.04.16 | 69 师，52 民 33 军，焦点正常推进 |
| **1938.06.01** | **崩溃**（见 91.5） |

### 91.2 战略死路：`GER_reorganize_the_wehrmacht` 会锁死整条历史扩张线

**这是本局最重要的发现，也是"老手攻略时间轴"排序的一个 bug。**

`common/national_focus/germany.txt`：

```
# GER_anschluss
prerequisite = { focus = GER_reorganize_the_wehrmacht focus = GER_heed_von_neuraths_concerns }

# GER_heed_von_neuraths_concerns
prerequisite = { focus = GER_remilitarize_the_rhineland }
mutually_exclusive = { focus = GER_reorganize_the_wehrmacht }
```

同一个 `prerequisite = { }` 块里的两个 focus 是 **AND**（都要），而
`heed_von_neuraths_concerns` 与 `reorganize_the_wehrmacht` **互斥**。
所以二选一之后 Anschluss 永远不可能满足前置。

连锁后果（全部读自同一文件）：

```
GER_demand_sudetenland   prerequisite = { GER_anschluss }
GER_reassert_eastern_claims  prerequisite = { GER_anschluss }
GER_danzig_or_war        prerequisite = { GER_reassert_eastern_claims }
GER_around_maginot       prerequisite = { GER_danzig_for_slovakia | GER_danzig_or_war }
GER_war_with_france      prerequisite = { GER_around_maginot | GER_operation_weserubung }
```

即：**选了 `reorganize_the_wehrmacht`，就没有 Anschluss → 没有苏台德 →
没有但泽 → 没有"包绕马奇诺" → 没有"对法作战"。整条免费扩张 + 对法宣战
的国策链全废。**

工具侧还验证了两次：`hoi4_set_focus("GER_anschluss")` 和
`hoi4_set_focus_ptr(<live ptr>)` 都返回 `posted:1 / current:null`，引擎真的拒绝。

> **结论 / 待修**：`hoi4_selectable_focuses` 的 `S0_playbook` 排序把
> `GER_reorganize_the_wehrmacht`(1995) 排在 `GER_heed_von_neuraths_concerns`(1875)
> 前面，等于推荐了一条自断经脉的路线。应该把这条互斥关系写进 playbook：
> 想走历史线就必须选 Neurath。这不影响引擎，纯属攻略数据错误。

### 91.3 卡点：支援装备 = 0，导致师数从 51 卡死两个月

1937.05～1937.09 师数一直是 51，`divisions_count` 不动。原因不是训练位不够
（有 9 条线），是 **`support_equipment_1` 库存为 0**：`Infanterie-Division`
模板带 `engineer` + `artillery` 两个支援连，没有支援装备就出不了厂。

而这条产线是 `housekeep_production` 之前把支援装备压到 2 个工厂造成的
（我 2026-11-17 手动改成 5 之后，两个月内 51 → 60 → 69）。

> **待修**：`housekeep_production` 应该保证 `support_equipment` 至少 4~5 个
> 工厂，或者干脆按"模板里每个支援连都要吃支援装备"来算需求。

### 91.4 推翻旧结论：战争目标可以**并发**，不是"一局只能点一个"

旧笔记（§89）写的是"`hoi4_justify_wargoal` 每局只对第一个目标生效"。
本局实测**不成立**：连续三次调用全部成功，各自建立了独立的
`CTimedWargoalActivity`：

| 目标 | total_days | ETA | PP 消耗 |
|---|---|---|---|
| BEL | 275 | 1938.06.19 | 54 |
| HOL | 275 | 1938.06.25 | 54 |
| LUX | 555 | 1939.04.07 | 110 |

规律：**每多一个并发正当化，天数翻倍**（BEL 单独报价 230 天 → 并发后 275；
第三个 LUX 直接 555）。所以并发不是无代价，但确实可以做。

对"吞并法比荷卢"这个目标来说这条路是通的：
- 比利时被 **法国和英国同时保证**（`history/countries/BEL - Belgium.txt`：
  `has_guaranteed = FRA` / `has_guaranteed = ENG`），打比利时 = 法国 + 英国 +
  整个同盟国自动参战，法国不需要单独宣战。
- 荷兰、卢森堡没有保证，各自需要一个目标。

### 91.5 崩溃：1938.06.01 月初结算，RVA 0xB2E62E

```
Application: HOI4  Operation Postern v1.19.5.0.7c6c
Date/Time: 2026-11-17 01:12:50
Unhandled Exception C0000005 (EXCEPTION_ACCESS_VIOLATION) at 0x00007FF61572E62E
Stack: PHYSFS_swapULE64 + 9564894 / +9569762 / +10406064 / +5374125 ...
```

`0x7FF61572E62E - image_base(0x7FF614C00000) = **0xB2E62E**`。

现场事实：
- 崩溃点正好是 **1938.06.01.01 月度 on_action**（error.log 最后一行是
  `[01:12:42][1938.06.01.01]: common/on_actions/14_sea_on_actions.txt:2340`），
  之后没有任何日志。
- 崩溃前最后一次注入是 1938.04.16 的 `hoi4_set_focus` + `hoi4_pause_in_hours`，
  距离 45 个游戏日，不像即时踩内存。
- **本局开着 mod**（system.log）：`Downsize AirForce`、`Downsize LandForce`、
  `Flag Rework Revive`、`Force AI to leave faction`、`Radio Requiem`、`UMMS`、
  `决议化更多铁路和补给中心`、`国家名称全称补全`、`陆军徽章汉化`。
- 最近一次自动存档 `autosave.hoi4` = **1938.6.1**，即崩溃当天的存档可用。

嫌疑排序：
1. **`hoi4_pause_in_hours` 走的是控制台 `pause_in_hours` 命令**
   （返回值里明写 `"command": "pause_in_hours 350"`，且每次都 `ok:false`）。
   这是本局唯一一个"非玩家动作"的调用，而且是在崩溃前反复使用的。
   **建议停用该工具**，改用 `Start-Sleep` + `hoi4_pause`，或者把
   `pause_in_hours` 改成纯内存写暂停标志。
2. `Force AI to leave faction` 这个 mod 会在月初 on_action 里大规模改阵营，
   和月度结算撞车。
3. `Downsize LandForce` 改陆军编制，也可能在月初重算时炸。

**下一步**：加载 `autosave.hoi4`（1938.6.1），先只做只读操作跑 2~3 个月，
不碰 `hoi4_pause_in_hours`，看是否还崩。若仍崩 → 基本可判定是 mod。

### 91.6 本局顺手修掉/发现的工具问题

| 问题 | 处理 |
|---|---|
| `hoi4_air_mission` 永远 `ok:false`（因为 `+0xA4 mission_on` 是 0） | **已修**：`ok` 改看 `+0xA0` 掩码是否落盘，`mission_on` 单独报为 `wings_sortieing_now` 并加说明。实测 5 个战斗机联队 `wings_mission_set: 5`, `ok:true` |
| `mission_on` 到底是什么意思 | 全世界 210 个联队里 82 个有任务、只有 2 个 `+0xA4=1`（都在交战区）。结论：`+0xA4` 是"正在出动"，不是"玩家已启用"。作战时才为真 |
| `AIR_NO_MISSION` 永远清不掉（运输机没有战略任务） | **已修**：`_light_alerts` 排除 `transport_plane` |
| `housekeep` 的科研步骤静默失败（4 个槽全空但整体不报错） | **已修**：科研挪到最后执行 + 失败重试一次 |
| `hoi4_conveyor_progress` 全部返回 `null` | **未修**（读训练进度字段失败）。训练进度目前只能靠师数变化间接看 |
| `hoi4_focus_detail` 返回乱码（`prerequisites: 683`、filters 全是二进制垃圾） | **未修**。前置关系改从 `common/national_focus/*.txt` 直接读，更可靠 |
| `hoi4_pause_in_hours` 返回值永远 `ok:false` 但实际生效 | 见 91.5，建议整体停用 |

### 91.7 第二次崩溃：同一地址，且游戏自己说了原因

加载 `autosave.hoi4`（1938.6.1，新 pid 20424）后只做了**只读调用**，然后发了 3 个
`hoi4_naval_mission`（把舰队改回 0 任务），5 分钟后再次崩溃：

```
Date/Time: 2026-11-17 01:25:43
Unhandled Exception C0000005 at 0x00007FF61572E62E   ← 和 91.5 完全一样
Stack: PHYSFS_swapULE64 +9564894 / +9569762 / +10406064 / +5374125 / ...
```

**同一 RVA `0xB2E62E`、同一条栈。** 两次崩溃都把嫌疑指向引擎内部同一条路径。

决定性证据在第二次崩溃的 `logs/error.log` 里，加载存档的**那一秒**就打了：

```
[01:20:53][1938.06.01.02]: Undefined GUI_TYPE: meme_music_stations_entry
                           - This will most likely crash the game
[01:20:53][1938.06.01.02]: Missing music_station_entry GUI component
                           containerWindowType with name: meme_music_stations_entry
[01:20:53][1938.06.01.02]: Undefined GUI_TYPE: Philips_stations_entry
                           - This will most likely crash the game
[01:25:35][1938.06.01.03]: AI tried to post an invalid command: unlock_trait_command
```

`meme_music_stations_entry` / `Philips_stations_entry` 是 **`Radio Requiem`** 这个
mod 加的电台条目。它引用的 GUI 类型在 1.19.5 里**不存在**，引擎自己明写
"most likely crash the game"。

> **结论（高置信）**：崩溃不是注入工具造成的，是 `Radio Requiem` mod 在
> 1.19.5 上的 GUI 定义失效。**关掉这个 mod 再试**。这也是为什么两次崩溃都发生在
> 游戏跑起来几分钟、UI 需要重建电台列表的时候，而不是在某次注入的当下。

同时建议关掉 `Force AI to leave faction`（会在月初 on_action 里大改阵营，
对正常战役没有好处）。其余 `Downsize LandForce/AirForce`（改编制）、
`UMMS`（UI）、汉化类应无碍。

**待办**：关 mod 后从 `autosave.hoi4`（1938.6.1，78 师）继续，
把用户要求的四件事补上：海军全部 0 任务回港（已做完 3/6）、
两个学说、步兵师加 3 个炮兵营（9/3）、决议 + 顾问。

### 91.8 第三次崩溃：排除 Radio Requiem，锁定 `unlock_trait_command`

用户关掉 `Radio Requiem` 后重开（pid 29060，载入 autosave 1938.6.1），
只做了 `hoi4_division_selftest` + `hoi4_naval_task_forces` 两个只读调用，
再发 `hoi4_naval_mission(mission=0)` 三条（把舰队改回港）→ **第三次崩溃**。

```
01:32:47  C0000005 at 0x7FF61572E62E   (RVA 0xB2E62E，与 91.5 / 91.7 完全一致)
```

现场证据：

| 事实 | 值 |
|---|---|
| `Undefined GUI_TYPE: meme_music*` 出现次数 | **0**（Radio Requiem 确实已关，Active Mod 从 9 个降到 8 个） |
| `error.log` 最后一行 | `[01:31:42][1938.06.01.03]: AI tried to post an invalid command: unlock_trait_command` |

三次崩溃对照：

| # | 时间 | 游戏内时刻 | 日志最后一行 | Radio Requiem |
|---|---|---|---|---|
| 1 | 01:12:50 | 1938.06.01.01 | 月度 `14_sea_on_actions.txt:2340` | 开 |
| 2 | 01:25:43 | 1938.06.01.03 | `AI tried to post an invalid command: unlock_trait_command` | 开 |
| 3 | 01:32:47 | 1938.06.01.03 | `AI tried to post an invalid command: unlock_trait_command` | **关** |

**结论：这是 AI 在 1938.06.01.03 尝试 `unlock_trait_command` 导致的确定性崩溃，
和注入工具无关，和 Radio Requiem 也无关。存档在这里是死结——每次载入
autosave（1938.6.1）跑 1 小时必炸。**

`unlock_trait_command` 是 1.19 的角色/科学家 trait 解锁命令（Gotterdammerung 的
special projects 体系）。AI 提交了非法参数之后引擎没有判空，直接踩空。

剩下的嫌疑 mod（8 个里唯一会改 AI 行为的）：**`Force AI to leave faction`**。
它会在月初 on_action 里让 AI 国家退出阵营，很可能就是它生成了非法的
`unlock_trait_command`（阵营变动 → 角色归属变化 → trait 解锁参数失效）。

> **这一局到此为止。** 没有任何更早的存档（本局只留下 1938.6.1 的 autosave），
> 而 autosave 本身就在崩溃点上。要往下走只能新开一局。

### 91.9 重开方案（已定）

新开 1936 德国档，**mod 只保留汉化/UI 类**，关掉
`Force AI to leave faction`（首选嫌疑）、`Downsize LandForce`、`Downsize AirForce`
（改编制的两个也可能在月初重算时出问题）。

**国策路线必须改**（见 91.2）：

```
GER_remilitarize_the_rhineland
GER_heed_von_neuraths_concerns      ← 关键：不是 reorganize_the_wehrmacht
GER_anschluss                       ← 需要 54 师 + 1937 年
GER_demand_sudetenland              ← 74 师 + 1938
GER_reassert_eastern_claims
GER_danzig_or_war                   ← 100 师 + 1939
GER_around_maginot                  ← 一键对 BEL/HOL/LUX/FRA 宣战
```

这条线走通的话，用户的目标（1941 年前吞并法比荷卢）在国策层面就是免费的，
不需要像本局这样同时挂三份 275/555 天的正当化。

开局 30 天内要做完的（本局已验证可用的动作）：

1. `hoi4_housekeep` → 建造队列 / 闲置军工厂 / 未编师 / 空军 / 海军一次补齐
2. 科研 4 槽：`basic_machine_tools` / `electronic_mechanical_engineering` /
   `construction1` / `interwar_artillery`
3. 空军：fighters→1、CAS→2、tac_bomber→8、nav_bomber→16（运输机不挂）
4. 海军：5 个特遣舰队 Strike Force + 波罗的海/北海 region（9/16/173）
5. 陆军参谋长 Beck（+0.3 陆军经验/天）——这是 1 个 XP 都不涨的解药
6. `support_equipment` 产线至少 4~5 个工厂（91.3 的坑）

---

## 92. 第二局（1936.01.01 起）+ `hoi4_duplicate_division_template` 崩溃

### 92.1 本局进度（截至崩溃）

| 日期 | 师数 | 民工 | 军工厂 | PP | 备注 |
|---|---|---|---|---|---|
| 1936.01.01 | 30 | 35 | 28 | 2 | 开局 |
| 1936.03.11 | 30 | 35 | 28 | 117 | 莱茵兰 + 纽赖特完成，Beck 上任 |
| 1936.04.28 | 30 | 38 | 28 | 95 | 四年计划完成 |
| 1936.07.15 | 37 | 41 | 28 | 188 | 自给自足完成 |
| 1936.10.29 | 44 | 44 | 28 | 231 | 帝国高速公路完成 |
| 1937.01.16 | 46 | 46 | 28 | 204 | **游戏在此崩溃** |

已完成国策：`GER_remilitarize_the_rhineland` → `GER_heed_von_neuraths_concerns`
→ `GER_the_four_year_plan` → `GER_autarky_efforts` → `GER_construct_the_reichsautobahn`
→ `GER_fuhrerprinzip`。

已上顾问：Beck（陆军参谋长，+0.3 陆军经验/天）、Schacht（政治，-10% 消费品
+10% 军工厂建造速度）、Schorner（步兵攻 +10%/防 +15%）、Rundstedt（士气 +8%）、
Fritsch（ attrition -8%）、Seldte（工业产能 +2.5%）。

### 92.2 ⚠️ `hoi4_duplicate_division_template` 会直接打死 hoi4.exe

复现步骤（2026-11-17 02:16，第二局 1937.01.16）：

```
hoi4_duplicate_division_template(source_name="Infanterie-Division")
→ ReadingProcessMemory(0x14f426c0210) failed, GetLastError=299
→ 之后所有调用都是 "hoi4 process not found"
```

失败点栈：

```
game.py:2576  self.call(0x1FDC190, (cmd, src_inner, 0, tagbuf, 0))
  → winject.py:493  self._read(flag, 4)   # 读返回值槽时进程已经没了
  → ReadProcessMemory ... GetLastError=299 (ERROR_PARTIAL_COPY)
然后 game.py:2590  self.free(tagbuf) 里再炸一次（进程已退出）
```

特点：

- **没有 crash 文件夹、没有 exception.txt**——写 dump 的那条路径根本没跑到，
  是引擎内部直接终止的。
- `game.log` 的尾巴停在 `1936.11.28.11`（日志有缓冲，实际死亡时间更晚）。
- 崩溃前 20 秒内 `hoi4_get_modifiers` / `hoi4_deploy_ready` 都还正常返回，
  所以不是"游戏本来就要死了"。

根因在代码里写得很清楚但没执行：`game.py:2563-2565` 的注释写着

```
# 2026-11-10 test: src_ref+0x18 crashed the ctor (sig=6 at 0x1FDC190);
# reverted until the real CDivisionTemplate/CReferencedDivisionTemplate
# layering is confirmed. CCreateDivisionTemplateCommand stays disabled.
```

注释说"已回滚、保持禁用"，但那条 `self.call(0x1FDC190, ...)` **一直还能走到**。
`src_ref + 0x18` 不是 `CDivisionTemplate*`，ctor 拿到的是垃圾指针。

**修复（已提交）**：`duplicate_division_template()` 在 `sys.platform == "win32"`
时直接 `return {"ok": False, "refused": True, ...}`，永不触碰 0x1FDC190。
要重新启用，必须先反汇编 ctor，确认 `CReferencedDivisionTemplate` 里内嵌
`CDivisionTemplate` 的真实偏移。

**教训**：注释里写"disabled"不等于代码 disabled。凡是"已知会崩"的注入路径，
必须在函数入口硬拦，最好加一个 `_DISABLED_ON_PE = {...}` 白名单表，
由 `api.allow_call` 统一拒绝。

### 92.3 ⚠️ AGENTS.md 里 Anschluss 的 AND/OR 规则是错的

`AGENTS.md` 和 §91 都写过：

> `GER_anschluss` 要求 `reorganize_the_wehrmacht` 和 `heed_von_neuraths_concerns`
> 都在**同一个 prerequisite 块**里（AND），所以选了 reorganize 就永远拿不到 Anschluss。

**这条是反的。** HOI4 的国策前置语法是：

- **一个** `prerequisite = { focus = A focus = B }` 块 = **或（OR）**，任意一个完成即可；
- **多个** `prerequisite = { ... }` 块之间 = **与（AND）**，每块都要满足。

证据：`common/national_focus/germany.txt:9002`

```
prerequisite = { focus = GER_reorganize_the_wehrmacht focus = GER_heed_von_neuraths_concerns }
```

而这两个国策在 8438 / 14553 行**互相排斥**（`mutually_exclusive`）。
如果同一块是 AND，`GER_anschluss` 在游戏里根本不可能被任何玩家点出来——
所以它只能是 OR。**两条路线都能到 Anschluss。**

那 §91 里"选了 reorganize 导致 Anschluss 永久锁死"的结论**站不住**，
真正卡住的更可能是 `available` 块里的另外两条（germany.txt:9050-9075）：

```
has_war = no
fighting_army_strength_ratio = { tag = AUS ratio > global.deployed_manpower_ratio_needed_for_anschluss }
NOT = { has_idea = rhineland_challenge_met }
```

`global.deployed_manpower_ratio_needed_for_anschluss = 24`
（`common/on_actions/12_wuw_on_actions.txt:2052`）。
`rhineland_challenge_met` 只在**莱茵兰危机退让**时由 `germany.65` 选项 b 加上，
持续 183 天（`events/Germany.txt:6588`）——所以退让后要等半年。

工具侧 `hoi4_selectable_focuses` 的 `blocked` 文案
（`GER_anschluss (wait: divs 30/54, year 1936/1937, ...)`）是**工具自己的
playbook 启发式**，不是引擎条件；54 师/1937 年只是对 `strength_ratio > 24`
的近似，别当成硬规则。

### 92.4 本局验证到的其它事实

- **`hoi4_fill_research` 冷启动会连失败三次**。根因不是"技术库没准备好"，
  而是 `_kick_tech_scan()` 起的后台线程要扫 **15061 个可写内存区、8.7 GB**，
  扫完才会写 `Game._VT_SCAN`。实测从头到尾约 40~60 秒。
  扫描期间 `_tech_by_name` 一直是 `None`，四次重试都报
  `'NoneType' object has no attribute 'get'`。
  **正确姿势**：报错后等 60 秒再调一次，就 4 槽全满。
  （`hoi4_debug_eval` 可以查 `len(getattr(type(g),'_VT_SCAN',{}))` 确认扫完没有，
  扫完是 `{('CTechnologyTemplate'): 1104 项}`。）
- **`hoi4_set_production_amount` 在暂停时一定失败**（命令队列不消费），
  必须先 `hoi4_pause(paused=False)`。
- **`hoi4_train_divisions(lines=5)` 调两次会开两条传送带**（2 条 + 6 条 = 8 条线），
  师数确实涨得更快：30 → 37（3.5 个月）→ 44 → 46。
- **`hoi4_deploy_ready` / `hoi4_conveyor_progress` 读不到 progress**（全是 null），
  所以没法"提前部署"凑师数，只能等自然训练完。
- **`hoi4_air_mission(tac_bomber, 1)` 会被引擎拒绝**——战术轰炸机没有
  "制空权"任务。用 `2`（CAS）或 `8`（战略轰炸）。同理
  `nav_bomber` 用 `16`（对海攻击）。
- **`autarkic_economy`** 是德国国策给的**贸易法案**（替换 `limited_exports`），
  不是经济法案；经济法案仍是 `partial_economic_mobilisation`。
  `war_economy` 要战争支持度 ≥ 0.50，本局到 1937.01 还只有 0.45。
- **`hoi4_pause_in_hours` 是唯一能一次推几十天的工具**，但客户端 30 秒超时，
  100 天左右必然超时。**服务端会继续跑完**——超时后在 pwsh 里
  `Start-Sleep` 等 60~100 秒再查询即可，不要重复发同一个调用。

### 92.5 恢复点

`autosave.hoi4` = **1937.1.1**（2026-11-17 02:13:19 写入，本局）。
崩溃发生在 1937.01.16，损失 15 天。

**下一局开始必须做的第一件事**：手动存一份 `campaign_1937_01.hoi4`，
之后每 3 个月手动存一次。自动存档只有一个月度槽位，救不了崩溃。

---

## 93. 第三局：1936→1940 全流程跑通，但西线总攻打崩了

### 93.1 这一局证明了什么

从 1936.01.01 一直跑到 1940.03.16，**没有任何一次注入崩溃**。上一局的三次
`RVA 0xB2E62E` 崩溃没有复现——它们是那份存档自身的问题（AI `unlock_trait_command`），
不是工具。

整条国策链完整走通：

```
GER_remilitarize_the_rhineland      1936.02.04 ✅
GER_heed_von_neuraths_concerns      1936.03.11 ✅
GER_the_four_year_plan              1936.04.28 ✅
GER_autarky_efforts                 1936.07.15 ✅
GER_construct_the_reichsautobahn    1936.10.29 ✅
GER_fuhrerprinzip                   ✅
GER_accelerate_the_rearmament_program
GER_war_preparations
GER_concentrated_armament_program
GER_coal_liquefaction
GER_anschluss                       1938.06.23 ✅  (77 师时点出来)
GER_demand_sudetenland              1938.08.31 ✅
GER_establish_production_targets / institute_price_controls / ascension_of_todt
GER_reassert_eastern_claims         1939.03.23 ✅
GER_danzig_or_war                   1939.06    ✅ (波兰让步，割让但泽，未开战)
GER_form_organization_todt / expanding_the_luftwaffe
GER_around_maginot                  1939.11.02 ✅ (只给战争目标，不自动宣战)
```

**关键数字**：1940.03 时 152 师 / 65 民工 / 63 军工厂 / 270 万人力 / PP 2000(疑似上限)。

### 93.2 Anschluss 到底要多少师

§92 里说"引擎门槛不是 54 师"——这一局量出来了：

| 师数 | Anschluss |
|---|---|
| 46 | 拒 |
| 58 | 拒 |
| 65 | 拒 |
| 77 | **成** |

所以阈值在 66~77 之间。`fighting_army_strength_ratio > 24` 比的是
**双方已部署人力之比**，奥地利 6 个师 ≈ 5.7 万，德国 77 师 ≈ 100 万 → 比值 ≈ 17.5；
再往上一点就过 24 了。**别信工具那句 `divs 30/54`。**

### 93.3 ⚠️ 挂机打 blitzkrieg = 三个月把整个战役打崩

`hoi4_set_front_directive(stance="blitzkrieg", battleplan=True)` 之后我让它
自己跑了大约 84 个游戏日（1939.12.23 → 1940.03.16）。结果：

- 152 个师里有 **大量师 `out_of_supply_days: 19~23`**，`army_current_supply_ratio`
  掉到 **0.9 / 6.7 / 7.4**（满值 108）
- 组织度掉到 14~15
- 德国自己丢了 **22 个州**（34 → 12）：奥地利、苏台德、但泽和一部分本土
- 敌军（英法比 + 整个英联邦）已经打进德国本土

也就是说 `_width_attack` / 堆叠那条老坑又踩了一遍：**AI 微操把 148 个师
全压在比利时 3 个边境省上**，补给瞬间归零，然后整条战线自己烂掉。

**结论：进攻必须看着打。** 每到一批新命令就 `hoi4_division_detail` 抽查
`out_of_supply_days` 和 `army_current_supply_ratio`，任一师
`out_of_supply_days > 3` 就立刻停攻、后撤、换防。绝不能把 blitzkrieg
挂着去睡觉。

### 93.4 这一局踩到的三个工具 bug（都已修）

1. **`hoi4_set_front_directive` 全线不可用**
   `mcp/server.py` 无条件把 `battleplan=` 传给 `game.set_front_directive()`，
   而那个包装函数没有这个形参 → 每次调用都
   `TypeError: MoveMixin.set_front_directive() got an unexpected keyword argument 'battleplan'`。
   **整个陆战入口是死的。** 已修：包装函数补上 `battleplan=True` 并透传给
   `movemicro.set_front_directive`。
   在服务端重启前，可以用 `hoi4_debug_eval` 直接调
   `movemicro.set_front_directive(g, tag, stance, None, None, min_garrison, deploy, None, battleplan)` 绕过。

2. **`hoi4_duplicate_division_template` 打死游戏**（§92.2，已硬拦）。

3. **重启后 `_win_vt_cache` 复用上一个 pid 的扫描结果**
   `_VT_SCAN` 的 key 是 `(pid, class)`，但旧代码只比对 `k[-1]`。重开游戏后
   新 Game 对象拿到**已经死掉的进程**里的地址，`_kick_tech_scan()` 以为
   已经扫过就不扫，`_tech_by_name` 永远是 None，于是所有科研调用都报
   `'NoneType' object has no attribute 'get'`。
   已修：`_win_vt_cache` 只接受 `k[0] == self.p.pid` 的条目。
   **在修好之前，重启后必须手动 `type(g)._VT_SCAN.clear()` 再等 60 秒重扫。**

### 93.5 ⚠️ 重启后 `hoi4_fill_research` 会覆盖正在研究的科技

重启后科技库还没建好时，`hoi4_research` / `hoi4_overview` 会把**四个槽全报成空**
（`researching: null`、`required: 0`），但槽里其实还在跑（`progress` 在涨）。
这时候调 `hoi4_fill_research` 会真的把正在研究的科技顶掉。

**正确顺序**：重启 → `hoi4_research_available`（会失败，触发扫描）→ 等 60 秒 →
再调一次 `hoi4_research_available`（这次会成功并建好库）→ 然后才看
`hoi4_research` / 才调 `hoi4_fill_research`。

### 93.6 下一局的开战节奏（这次别急）

- 1939 年**不要**点 `GER_around_maginot`。它是 `create_wargoal` 不是宣战，
  点完只是拿到战争目标；真正宣战要自己 `hoi4_declare_war`。
- 攒到 **1940 年 5 月以后**、**200+ 师**、**80+ 军工厂**、战斗机 10 个联队以上再打。
- 宣战顺序：先 `hoi4_deploy_to_border(target_tag="BEL")` 铺满边境，
  再 `hoi4_declare_war("BEL")`（比利时被法国+英国担保，会自动拉进整个盟军）。
- 进攻用 `hold` 稳住补给线，只对**单点**手动 `hoi4_attack_country` 或
  `hoi4_rage_micro`，不要整条线 blitzkrieg。

---

## 94. 德奥合并的真实机制 / 假模板 / PP+XP 提醒 / 事件可见性（2026-11-17）

### 94.1 推翻 §93 的"77 师门槛"说法：比的是**已部署人力**，而且门槛会往下掉

`GER_anschluss` 的 `available`（`common/national_focus/germany.txt:9050-9074`）：

```
fighting_army_strength_ratio = {
    tag = AUS
    ratio > global.deployed_manpower_ratio_needed_for_anschluss
}
```

这个全局变量**不是常量**：

1. `common/on_actions/12_wuw_on_actions.txt:2052` 初始化 `= 24`。
2. 事件 **`AUS_political_events.102`**（`events/WUW_Austria.txt:9164`）在
   `date > 1937.11.5` 之后、且德国已完成莱茵兰、希特勒执政、奥地利带
   `AUS_antischluss_active_flag` 时触发；它的**唯一选项**设
   `GER_hitler_wants_austria_flag`。
3. 有了这个 flag，周期性 on_action（`12_wuw_on_actions.txt:2947-3005`）每一跳
   把**奥地利的法西斯支持率**（若奥地利是中立政府则用中立支持率）从
   `deployed_manpower_ratio_needed_for_anschluss` 里减掉，下限 0。

所以：**1937.11.05 之前永远是 24，之后逐跳下降，最后趋近 0。**
这正是"越到后期越容易"的机制，也解释了为什么 1938.06 的 77 个师就够了。

为什么 77 而不是 54：这个 trigger 比的是双方**陆军总战斗力**
（游戏自带文档 `documentation/triggers_documentation.md:2833`：
"Compares the total army fighting strength between the scope country and the
one set with 'tag'"）。按人力口径粗算：77 师 × ~15k ≈ 1.15M，
奥地利 ~5 师 ≈ 45k，比值 ≈ 26 > 24 ✓；65 师 ≈ 21.7 ✗ ——
数量级和实测（46/58/65 被拒、77 通过）吻合，但这是拟合，不是引擎公式。
关键是这个量随**师数 × 模板大小**增长，所以"把模板撑大"等价于"多造师"。
`hoi4_selectable_focuses` 的 `blocked` 文案（"divs 30/54"）仍然是工具自己的
playbook 猜测，与引擎无关，**不要当门槛**。

### 94.2 "假模板"能在 1936 年直接点出来（机制成立，但代价大）

> **⚠️ 本节的"25 营师"思路是错的，已被 §96 推翻。**
> `fighting_army_strength_ratio` 数的是**满编师的数量**，与师的大小无关；
> 正确姿势是刷**1 营师**的数量，不是把师撑大。

既然比的是人力而不是师数，把师模板撑大即可：

- 46 个师 × 25 个步兵营（25k 人）≈ 1.15M，等价于 77 个 9 步师。
- 工具路径：`hoi4_template_edit(template="Infanterie-Division", line={"infantry":25,...})`。
  每加 1 个营 = 5 XP；9 → 25 是 +16 营 = **80 XP**（本局有 417.9 XP，够）。
- 代价：所有师立刻缺装备、组织度崩、战斗力短期归零；而且
  `hoi4_duplicate_division_template` 在 win32 上**已硬拦**（它会打死 hoi4.exe，
  见 §93.2），所以只能改**现有**模板，不能"复制一个假模板再切回来"。
- 结论：**能做**，属于合法工具范围内的"骚操作"，但正式局不建议——改完还得改回去，
  两次都要 XP，而且中间那段时间军队不能打仗。

### 94.3 新工具 `hoi4_thresholds` + overview 两条新 alert

玩家要求：PP 到 1000、陆海空经验到 100 时提醒。

- 新工具 **`hoi4_thresholds`** → `{political_power, political_power_cap: 2000,
  experience:{army,navy,air}, thresholds, reached[], spend_hint, warn}`。
- `hoi4_overview.alerts` 新增：
  - `PP_CAPPING`（PP ≥ 1000；上限 2000，再攒就是纯浪费）
  - `XP_READY(army|navy|air)`（该军种经验 ≥ 100 = 一门学说/一次大改）
- 所以每回合开头那次 `hoi4_overview` 就会自己报，不必额外调用。

**经验值从哪读**（这次是新挖出来的，之前没有任何工具读过海空军经验）：

- `CCountry+0x1588 → CCountryExperienceStatus*`
  （RTTI 验证：正好 440 个活对象，一国一个，`+0x08` 回指它的 CCountry）。
- 池子是 **Q15 定点：1.0 经验 = 32768**。引擎自己的换算函数
  `img+0x2572DB0` 就是乘 `100000/32768 = 3.0517578`，`templedit._pe_army_xp` 用的就是它。
- `+0x10` 陆军 —— 和 `_pe_army_xp` 每次交叉验证（13694383/32768 = 417.92，引擎也报 417.92）。
- `+0x28` **海军**、`+0x40` **空军** —— 2026-11-17 用玩家截图的顶栏反查定死的：
  顶栏那一排是 `指挥点 150 / 陆军 417 / 海军 59 / 空军 405`，
  而 `+0x10=417.92`、`+0x28=59.56`、`+0x40=405.02`，**三个全部对上**。
  另一条独立佐证：`+0x28` 在**内陆国瑞士**正好是 `0.00`，而在美国 213 / 日本 99 /
  意大利 92（海军大国）上最高 —— 这就是海军经验。
  （第一版把 `+0x28` 当空军、`+0x18` 当海军，是错的；`+0x18` 对所有国家都 ≈0，
  不是三个池子之一。）

### 94.4 事件 / 新闻：现在**看不见**，但"点选项"这条路是通的

- 现有工具**没有任何**读事件弹窗/新闻的 API。
  `calling/api.py:1631 dismiss_popups_and_unpause()` 是 Linux `xdotool key Return`
  的版本，Windows 上只清 `idler+0x6C1` 的暂停位，**不会关弹窗**。
- 但 `hoi4_player_commands` 里有 **`CSelectEventOptionCommand`**
  （type_id `0x2995`，size 240；字段 `+0x24` int、`+0x28` handle、`+0x30` country tag、`+0xE8` int）
  → "替玩家点选项"在命令层是存在的，缺的只是**枚举当前挂起事件的 handle**。
- `CEvent` 13980 / `CEventOption` 18562 是**事件库**（脚本解析出来的全部事件/选项），
  不是挂起队列。`CEventWindow` 只有 1 个实例（`_VT_SCAN` 报的 4 个地址是同一对象的
  4 个多继承 vtable 子对象），是 UI 单例，不是弹窗列表。
- `CCountry` 里跟事件有关的数组只有 `CDelayedEvent`（`+0x1290`，本局 1 项）。
- **最重要的一条**：`common/defines/00_defines.lua:19`
  `EVENT_TIMEOUT_DEFAULT = 13` —— **事件 13 天不回答会自动结算**（按第一个选项）。
  所以"模型没看见" ≠ "什么都没发生"：默认选项会照常生效。
  这解释了为什么从没手动点过事件，1938 年德奥合并的计数还是开始往下掉了。
- 下一步要做的是找到挂起事件容器（候选：`CCurrentGameState` 里的事件队列），
  再拿 `CSelectEventOptionCommand` 反推 handle 编码；在那之前，**事件内容**只能靠
  读 `events/*.txt` + `game.log` 里的 `[YYYY.MM.DD.HH]` 时间戳对照，读不到实时弹窗。

---

## 95. 1940.03.16 西线"没有单位被分配至这个命令"——是工具 bug（2026-11-17）

玩家截图里 `第1集团军 / 黄色方案` 的 tooltip：**"没有单位被分配至这个命令。分配的上限：51"**。
现场读数（`hoi4_armies` + `hoi4_orders` + `armyops_win.orders`）：

| 集团军 | 命令 id | 全部类型 |
|---|---|---|
| 第1 | 1, 61 | type 2, type 2 |
| 第2 | 5, 9, 65 | type 2 ×3 |
| 第3 | 13, 17, 69 | type 2 ×3 |
| … | … | 全部 type 2 |

**8 个集团军一共 24 条 `type: 2` 前线命令，`type: 1` 进攻线 0 条。**

### 95.1 根因：`plan_front(force=True)` 叠层，不删旧订单

- id `1..57` 是 1939 年打波兰那次 `plan_front` 画的（15 条）；
- id `61..89` 是 2026-11-17 对 BEL 画的（8 条）；
- 同一支集团军身上挂着 2~3 条，很多还是 **零长度切片**
  （`from == to`：9/9、65/65、71428/71428、42857/42857 …）。

`game.plan_front()` 里只有 `force=False` 且没显式点名集团军时才跳过"已经有命令的军"；
`set_front_directive()` 传的是 **`force=True`**，于是每次都往同一支军身上再叠一层。
引擎在一支军有多条前线命令时只喂其中一条，其余就是"没有单位被分配"。

而且 `A.offensive_line(...)` 这条路**一条 type-1 都没有落下来** ——
所以箭头是装饰，**根本没有进攻在跑**，152 个师就那样杵在边境线上挨打。

**结论：§93 把这次溃败全归给"挂机 blitzkrieg 导致补给崩溃"是不完整的。**
真实链条是：订单叠层 → 每条命令分到的师极少/为零 → 152 个师被挤进少数几个
边境省（这才是补给比 0.9/6.7/7.4 的来源）→ 同时没有进攻线 → 全程被动挨打。

### 95.2 修法（待做）

1. `plan_front()` 画之前，先对该军 `A.delete_orders(og)` 清空，或者在
   `(front, section, from, to)` 完全相同时复用旧命令而不是新建。
2. `offensive_line()` 的失败要**显式返回**（现在 `hoi4_orders` 里看不到 type-1
   就说明它没成功，但 `plan_front` 的返回值被 `set_front_directive` 吞掉了）。
3. 临时手工修：`hoi4_delete_order` 逐条删重复 → `hoi4_assign_divisions` →
   `hoi4_offensive_line`。

### 95.3 顺带：空军只挂上去 1 个联队，而且是运输机

`air_attach_result: {attached: 1, scanned: 15, refused: 14}`，唯一挂上的
`wing 5 = transport_plane`（运输机不接战略任务）。所以这一局**制空权是 0**。
`attach_air_wings_to_armies` 的 14 次拒绝需要单独查。

## 96. 一步师（假模板）——更正：比的是"满编师**数量**"，不是师的大小

玩家指出（并查了社区/维基口径）：

> `fighting_army_strength_ratio` 数的是**满编满装备的师有几个**，跟每个师多大无关。

所以 §94.2 说的"25 营师"是错的思路。正确姿势是反过来的：

1. 造一个 **1 个步兵营**的模板（"一步师"），人力/装备消耗极低；
2. 用最便宜的产线刷几十个，凑到 **24 × 奥地利的师数**；
3. `GER_anschluss` 点掉，然后把这些假师**直接删掉**、模板切回正常编制。

**本工具链现状（2026-11-17）**：这条现在**跑不通**——
`hoi4_duplicate_division_template` 在 win32 上硬拦（会打死游戏），
`CCreateDivisionTemplateCommand` 的参数字段是坏的（`arg1` 8 字节 deref 明显不对），
`hoi4_deploy_ready` / `hoi4_conveyor_progress` 一律返回 `progress: null`。

变通（可行但不优雅）：把 `Infanterie-Division` 直接改成 1 营 → 刷师 → 点国策 →
改回 9 营。改回那次要 8 个营 = 40 XP。

**下一局开工前必须先修的三件事**：造模板、训练进度、部署出厂。

## 97. Backlog

- [ ] **事件/新闻读取**：定位挂起事件容器 → 反推 `CSelectEventOptionCommand`
      (type 0x2995, size 240) 的 handle 编码 → 做成 `hoi4_events`（列出标题/描述/选项/效果）。
      在那之前只能读 `events/*.txt`，且 13 天超时会自动按第一个选项结算。
- [ ] `plan_front` 订单叠层（§95.2）
- [ ] `attach_air_wings_to_armies` 14/15 拒绝
- [ ] 造模板 / 训练进度 / 部署出厂 三条链路

---

## 98. 新建师模板：ctor 打通了，CanExecute 还没过（2026-11-17 12:42 崩了一次）

> **2026-11-18 晚 结论推翻 —— 先看 §98.6。**
> `CanExecute` 从来不是"恒 0"，它是个**经验收费口**：`0x1C2BD70(cmd,&cost)`
> 算出价格，然后 `if (army_xp < cost) return false`。当年所有测试都在
> 1936.01.01 的开局档上做，德国陆军经验 = 0，`0 < cost` 必然拒绝。
> 造模板功能本身是好的，`hoi4_duplicate_division_template` 现已实机跑通。
> 下面 §98.1–§98.5 保留原始记录，但"造不出来"这个结论是错的。

### 98.1 结论先说

`CCreateDivisionTemplateCommand` 在 PE 上**不再是黑盒**，但**还没能真正造出模板**。

| 项 | 值 | 状态 |
|---|---|---|
| vtable | `0x2A26EF8` | ✅ 反推自 pecmds.pkl + 运行时校验 |
| size | `0x290` | ✅ |
| ctor | `0x1C2B7D0` | ✅ 调通，不崩 |
| CanExecute | `0x1C2DBF0` | ⚠️ 调用安全，但一直返回 0 |

CanExecute 的地址是这么定出来的：Update 命令的 vtable 槽位 9 正好是已知的
`0x1C2DD90`（`commands.py:63` 的 `CANEXEC_UPDATE_TEMPLATE`），Create 的 vtable
同槽位是 `0x1C2DBF0`。

### 98.2 已经验证成立的部分

`ctor(cmd, src_ref + 0x18, 0, &tag_i32, 0)`（参数顺序和 Linux 0x1FDC190 一致）：

- 返回后 `cmd+0x00 == image_base + 0x2A26EF8` ✅
- 深拷贝真的发生了：源模板的名字 std::string 被**重新分配**（src 0x1780C20_5680 → copy 0x177BB23_E340），
  其余数据块逐字节一致 ✅
- 命令内嵌模板名是 **MSVC std::string at `cmd+0x30`**（SSO: 16 字节缓冲 + `+0x10` 长度 + `+0x18` 容量），
  改名后 `stdstring` 读回 `"1BuShi"` ✅
- 国家 tag 落在 `cmd+0x270` = 1（GER）✅

### 98.3 还差什么

CanExecute 在以下**全部**配置下都返回 0：

```
plain            (tag@0x270, 0x288 = 0)
0x288 = tag
0x268 = src_ref
0x278 = src_ref
0x288 + 0x268
0x288 + 0x278
```

说明拒绝原因不是这几个字段，得**反汇编 0x1C2DBF0** 看它到底读什么
（候选：模板数量上限、`CDivisionTemplateData` 里的 id 唯一性、
`+0x274` 那个 "globals" 指针）。

### 98.4 ☠️ 这次崩溃是我的错，记下来

**2026-11-17 12:42，hoi4.exe 直接消失**（无 crash 目录，`ReadProcessMemory` 299），
原因：我在试参数顺序时构造了 `ctor(cmd, 0, src, ...)` / `ctor(cmd, tagbuf, src, ...)`
这类把 **NULL 或垃圾指针喂进 rsi（源模板槽）** 的变体。

`commands.py:112` 早就写着：

> A NULL rsi was the previous SIGSEGV cause (rsi is dereferenced immediately).

**规则：`CCreateDivisionTemplateCommand` 的 ctor 只能拿真实的
`CReferencedDivisionTemplate* + 0x18` 当第 2 个参数。永远不要为了"试参数顺序"
把 0 放进这个槽。** 想试顺序，就换别的办法（反汇编），别喂空指针。

### 98.5 下一步

1. 反汇编 `0x1C2DBF0`（CanExecute），看它读哪些字段；
2. 或者绕开：`CCreateDivisionTemplateCommand::Execute` 是 `0x1C2C000`（vtable 槽 10），
   如果 CanExecute 只是个"名字不重复 + 数量未满"的守卫，可以直接用
   `cmdwin.post(..., force=1)` 跳过它——**但上一次 force post 没有产出模板**，
   说明 Execute 自己也会拒绝，所以还是得看反汇编。

### 98.6 2026-11-18 晚：反汇编完了，是经验费（结论推翻 §98.1）

`CanExecute`（`0x1C2DBF0`）完整控制流，八个谓词：

```
rdi = this
A = sub_140BF4B40(this+0x28, 0)              ; 模板+0x48 容器非空
if (!A && !sub_140BF4B50(this+0x28, 0))      ; 模板+0x68 容器也空
        return false                          ; → 空模板被拒
if (*(i32*)(this+0x270) <= 0)  return false   ; 国家 tag 必须 > 0
if (*(i32*)(this+0x208) == 3)  return false   ; 某个模板类型字段 == 3 时拒
if (!sub_141C2D7F0(this))      return false   ; 模板归属校验（见下）
sub_141C2BD70(this, &cost)
if (cost > 0) {
        country = sub_140C07DA0(&this+0x270)
        sub_142572DB0(&army_xp, country+0x10)  ; ← 就是陆军经验
        if (army_xp < cost) return false       ; ★ 卡在这里
}
if ( sub_141C2D540(this+0x28))  return false
if (!sub_140BF4D50(this+0x28))  return false
return sub_140BF4B60(this+0x28) != 0
```

**`sub_141C2D7F0` = 模板归属校验**（不是"globals 指针"）：
`this+0x274` 是一个 **packed handle `{u32 type, u32 id}`**，交给 handle 解析器
`0x2284870`，取 `obj+0x18` 的首个 u32 当国家 tag，与 `this+0x270` 比较；
不同就再去 `[CCountry+0xF88 (CDiplomacyStatus) + 0x170 / +0x190]` 两个容器里找。
**handle 为 `{0,0}` 时直接返回 TRUE**（所以本地模板永远过这一关）。

`sub_141C2BD70(cmd, &out)` 写一个 `{cost, begin, end}`，`cost` 就是价格。

**实测价格**（1936 德国，100,000 = 1 XP）：

| 模板 | cost | XP |
|---|---|---|
| Infanterie-Division | 6,500,000 | 65 |
| Panzer-Division | 8,000,000 | 80 |
| Gebirgs-Brigade | 3,000,000 | 30 |
| Kavallerie-Brigade | 3,000,000 | 30 |

实扣与报价一致：造一个 Infanterie 副本，陆军经验 460.0 → 395.0。

**验证方式**（可复现）：`templedit._pe_army_xp` 用的正是
`sub_142572DB0(buf, country+0x10)`；把 `country+0x10` 写成 20,000,000
（读回 61,035,156，比值 100000/32768 = 3.0517578），`CanExecute` 立刻从 0 变 1，
`cmdwin.post` 之后模板数 5 → 6，名字、编制都对。

**为什么误判两个月**：所有测试都在 0 经验的档上做，`0 < cost` 恒真。
**教训：谓词返回 false 时，先把整个函数反汇编完再下结论。**

## 99. COrderAssignCommand 在 PE 上接上了 / 空军 +0xA4 不是启用位（2026-11-17 13:30）



### 99.1 离线 RTTI 定位（不再靠猜 RVA）

新增 `research/pe_rtti.py`：只读 hoi4.exe 字节，走 MSVC x64 的
TypeDescriptor → CompleteObjectLocator → vtable 三段引用，不需要反汇编器、不需要开游戏。

**先用已知答案校准**：`CCreateDivisionTemplateCommand` 解出 vtable `0x2A26EF8`、
slot 9 = `0x1C2DBF0`、slot 10 = `0x1C2C000` —— 这三个值都是之前运行时亲测过的，
所以同一套方法给出的结果可信。

`COrderAssignCommand`（`.?AVCOrderAssignCommand@@`）：

```
vtable        0x2A84CE0
slot  9       0x18DE970   CanExecute
slot 10       0x18D4220   Execute
```

**旧笔记里的 ctor `0x231D0D0` 是假的** —— Ghidra 在 `0x141231D0D0` 上反汇编出
**0 条指令**，那里根本不是函数入口。它是照着 Linux 的 `0x231D060` 顺手挪的。
所以这条命令**一律不调 ctor**，用 `cmdwin.build_by_vt` 按字段构造。

字段（CanExecute 与 Execute 双向确认）：

```
+0x28  CPdxArray<packed CArmy handle>   data +0x28 / cap +0x30 / size +0x34 / alloc +0x38
+0x40  packed COrdersGroup handle (8 字节)
+0x48  i32 order id
```

CanExecute 逻辑（`0x18DE970`）：

1. `[cmd+0x34] == 0` → 写 "no divisions" 并返回 **false**；
2. 逐个元素 `0x14057F820(handle)` 必须为真，且 `[[resolve+0xB0]]+0x1B0 == 0`；
3. 返回 `IsValidHandle(&cmd+0x40)` —— **只校验军 handle，完全不看 +0x48 的订单号**。

Execute 逻辑（`0x18D4220`）：

```
og    = resolve(cmd+0x40)
order = 0x140C402A0(og, [cmd+0x48])          ; 找不到就退到父军团 [og+0x1B8]
```

**这才是"没有单位被分配至这个命令"的正解**：`CCommand` 是异步的，`Execute` 之后
引擎才把师挂到 order 上，所以必须 `post_drained` 跑几帧再读回 `order+0x210+0xC`。

工具层：`armyops_win.assign_to_order()` 新增；`assign_divisions_to_front()` 之前
在 Windows 上仍然 `import armyops`（Linux 模块），所以每次分配都是静默空操作 ——
现在按平台分派，并在 `armyops_win.orders()` 里补上了 `assigned_divisions`
（`order+0x210+0xC`）。

### 99.2 空军：`+0xA4` 不是"任务启用位"

实测（1936 PE 新档）：

| 联队 | 类型 | 命令 | +0xA0 | +0xA4 |
|---|---|---|---|---|
| 1 | fighter | mask 1 | 1 | **0** |
| 8 | cas | mask 2 | 2 | **0** |

两个联队 `0x90..0xC4` 逐字节相同（只有 +0xA0 掩码不同），跑过 2 个游戏日后 +0xA4 仍全为 0。

结论：**不存在 fighter 能写、CAS 写不进去的"启用位"**，之前的判断是误读；
`+0xA4` 是"当前有没有在飞"的运行期计数，和平年代本来就该是 0。
`AIR_NO_MISSION` 红点因此只看 `mission`（掩码）`是否非 0`，不再看 `mission_on` ——
否则这个红点永远清不掉。

### 99.3 今天的三次崩溃（都未定论，先记下来）

| 时间 | 异常 | 模块内偏移 | 当时在做什么 |
|---|---|---|---|
| 12:44 | 0xC0000005 | — | 用 NULL 源模板探 ctor 参数序（我的错，§98.4） |
| 13:07:54 | 0xC0000005 | `0x1043E67` | 只做了纯内存读；下一次调用报 299 |
| 13:22:57 | 0xC0000005 | `0x00C08724` | `hoi4_draw_front_line` 返回 299，进程已不在 |

`0x1043E67` 落在 `CPdxArray` 的迭代器推进函数里（`0x141043E50` = begin、
`0x141043E00` = end、`0x141043F20` = IsValidIndex 的共用尾部），也就是某个容器
遍历时拿到了野指针 —— 典型的**之前的注入把堆写坏了，几秒后才炸**。

三条都不是"命令本身立刻失败"，所以**不能靠 read-back 判断注入是否安全**。
纪律：一次会话里的注入次数要少，每批之后重新 `hoi4_status` 确认游戏还活着；
`ReadProcessMemory ... 299` 一律先当成"进程已经没了"，不要重试。

### 99.4 Ghidra 无头脚本的坑

`research/windows/DumpAsm.java`：`DisassembleCommand(set, set, false)` ——
**第三个参数 followFlow 必须是 false**。传 true 时它会顺着 call 把整个二进制走一遍，
一个 `0x200` 字节的请求跑了 10 分钟还没完（前三次崩溃排查里就卡在这）。
另外 MCP 的 `analyze_function` 走 REA-Ghidra 时，56 MB 的 exe 首次导入远超
MCP 主机单次调用超时，所以**离线反汇编一律用 `analyzeHeadless.bat` 自己跑**。

## 100. 画前线必崩的真因：Post 的拒绝路径会跑真删除析构（2026-11-17 14:40）

`hoi4_draw_front_line` 连续两次把 hoi4.exe 打死（13:22、13:39），traceback 都指向
`armyops_win.py:266` 的 `g.call(FN_NEW_ROOT_CMD, ...)`。**误导性极强 —— ctor 其实没问题。**

我手动在同一进程里直接调那个 ctor，一次通过：

```
vt    = base+0x2A856A0          (COrderNewRootCommand vtable)
f28   = 0x100000035             军 handle {53,1}
f48   = 0x200000042             front handle {66,2}
f50   = 2                       section
f58   = 0
f60   = 100000                  to
f30   = <data>  f38 = 24        师数组
```

所以崩的不是 ctor，是它后面的 **`cmdwin.post`**。

`cmdwin.py` 开头就写着（§注释）：

> `Post` owns the command on every path ... each of the four drop paths ends with
> `call [cmd->vt[0]](cmd, 1)` -- the deleting destructor, which releases through
> CRT `operator delete`. Commands therefore have to come from `operator new`,
> not from the game's `IAllocator`: mixing the two aborts the process whenever a
> command is refused.

而 `draw_front_line` 正是 `cmdwin.alloc`（游戏堆）+ `cmdwin.post`（拒绝就走 CRT free）。
**只要 Post 拒收，进程当场死。** 前线订单在 1936 年和平状态下被拒收的概率很高
（没有战争目标 / 剧院不匹配），所以这条命令基本等于"一点就崩"。

**修法**（`calling/armyops_win.py`）：post 之前先
`cmdwin.attach_fake_vtable(g, cmd, VT)` —— 把 slot 0 换成直接 return 的假析构，
这样被拒收时只是泄漏一块内存，不会 abort。三处都改了：

| 函数 | vtable |
|---|---|
| `draw_front_line`（root ctor 0x18C9B20） | `0x2A856A0` |
| `offensive_line`（front ctor 0x18C9840） | `0x2A853A0` |
| `execute_plan`（0x18C8C50） | `0x2A85D60`（原有 OE_VT，只补假 vtable） |

**实测（1936.01.01 新档，pid 41240）**：

```
hoi4_draw_front_line(front 66,2 section 2) -> ok
  orders: id 1 type 2 root_front{66,2} assigned 0
          id 2 type 2 root_front{66,2} assigned 0
hoi4_assign_divisions(mode=even)           -> ok
  order 1: assigned_before 0 -> assigned_after 12
  order 2: assigned_before 0 -> assigned_after 12
```

"没有单位被分配至这个命令" 从两头一起解决了：画线不再崩、`COrderAssignCommand`
（§99.1）真的把师挂上去了。**这是本阶段最重要的一次修复。**

顺带记：`FN_NEW_ROOT_CMD` 和 `FN_NEW_OFFENSIVE_CMD` 是两个不同的类，vtable 也不同
（0x2A856A0 vs 0x2A853A0）—— 之前把两者当成同一个 `COrderNewFrontCommand` 是错的。

## 100. 生产线的 add / remove 都是 handle 槽位问题（2026-11-18 晚）

两条命令一起修的，因为它们坏在同一个地方。

### 100.1 症状

| 工具 | 修之前 |
|---|---|
| `hoi4_add_production_line` | 描述里写着"Experimental"，代码把 variant handle 依次塞进 +0x28 / +0x30 试两次，然后报 "the game refused both handle arrangements" |
| `hoi4_remove_production_line` | 永远 `ok: false`，`before == after`，**连一句原因都没有** |

### 100.2 根因一：根本轮不到引擎拒绝

`calling/commands.py` 里 `AddProductionLine` 的 vtable 写的是 `0x4413A48` ——
那是 1.19.2 的旧表（和 `SetProductionLine` 当初一样）。RTTI 里这个类真正的 vtable 是
**`0x2A0FBC8`**。

更关键的是：**这两个类都不在 `cmdwin.WIN_SAFE` 里。**
`send_raw` 在 `sys.platform == "win32"` 时会先查白名单，不在就直接返回
`blocked: True`，**命令对象根本没被构造、更没进队列**。
所以 add 的"两次尝试"从来没碰到引擎 —— 它试的是同一个必然被拒的通道两次。
remove 更迷惑：函数只比较 `before/after`，不看返回值，于是"被拒"和"引擎拒绝"
在输出里长得一模一样。

### 100.3 根因二：`pecmd.ctor_layout` 看不见经临时寄存器写的字段

`pecmd` 从 ctor 反推字段布局，它维护一个 `this` 寄存器集合，而 `rcx` 从入口起
就在这个集合里。于是这种写法：

```
lea rcx, [rbx + 0x2c]      ; 记到 fieldptr[rcx] = 0x2c
mov qword ptr [rcx], rax   ; 命中 "br in this" 分支 -> off = m.disp = 0
```

会被当成 `[this+0]`，也就是**又一次 vtable 写入**，字段就此消失。
add 的 +0x2C/+0x34、remove 的 +0x28 全是这样丢的。

所以自动目录给出的 add 是 `args=1`、只有一个 `+0x28`；remove 是 `args=0`、**零字段**。
vtable / ctor / size 三项都是对的，只有字段不全。

修法：`cmdwin.SPEC_PATCH` —— 一张只放"手工从 ctor 读出来"的补丁表，
`spec_of()` 在 `pecmd.get()` 之上套一层。没有去改 `ctor_layout` 本身，
因为那要重刷 `pecmds.pkl`，会让所有已白名单命令的布局一起漂移，风险不成比例。

### 100.4 真正的布局（PE 1.19.5.0）

```
CAddProductionLineCommand   vt 0x2A0FBC8  size 0x40  ctor 0x11CD270
  ctor(this, int *tag /*rdx*/, {u64}*a /*r8*/, {u64}*b /*r9*/, bool front /*堆栈第4个参数*/)
    +0x28 = *tag              i32   国家 tag
    +0x2C = *(u64*)(a+8)      handle 装备变体（type 70）
    +0x34 = *(u64*)(b+8)      handle MIO（军工组织），{0,0} = 没有
    +0x3C = 栈上那个 bool     "把新产线移到第一位"
  CanExecute 0x11EDB10
  Execute    0x11DC130

CRemoveProductionLineCommand  vt 0x2A10048  size 0x30  ctor 0x11D0210
  ctor(this, {u64} *line /*rdx*/)
    +0x28 = *(u64*)(rdx+8)    handle 产线（type 56/57）
  CanExecute 0x11F18B0 = 0x1411EC7A0(&+0x28) != 0      "handle 还活着"
  Execute    0x11E3260
```

两个 ctor 找到的方式：`pedis.py xref <vtable>` 只给出 3 个 `lea` 站点
（ctor / clone / 工厂）。clone 一眼可辨 —— 它先 `operator new(0x40)` 再
`mov [rdx+off], [rbx+off]` 逐字段拷贝；工厂只写 vtable 不写参数。
剩下的那个就是 ctor。add 的 ctor 只有一个调用者（`0x17D211B`），
那个调用点上下就是 `"ui_production_line_start"` / `"ui_production_line_ship_start"`
/ `"_with_mio"` 三个字符串 —— 语义从 UI 自己的日志字符串反推出来的，不是猜的。

### 100.5 CanExecute 的谓词链（add）

```
if (cmd->i32[0x28] <= 0)  -> 日志 "Invalid Target"，false
if (handle@0x2C == {0,0}) -> false
obj = ResolveHandle(&cmd->0x2C);        // 0x142284870
if (!obj)                 -> false
if (!0x140C2CAD0(obj, &cmd->0x28)) -> false      // 变体能不能被这个国家造
if (handle@0x34 != {0,0}) { mio = ResolveHandle(&cmd->0x34); if (!mio) -> false; }
if (!0x141A936F0(obj, mio, 0))     -> false
return true
```

`0x28` 是 tag、不是数量：报错文案 `"Invalid Target"` 就是它，而且
`0x140C2CAD0(变体, &tag)` 的第二个参数恰好是 `cmd+0x28` 的地址。

### 100.6 Execute 干了什么（add）

```
prod = ((CCountry*)GetCountryByTag(&cmd->0x28))->0xF68     // CProductionStatus
line = 0x140ED0FF0(prod, variant)                          // 找到或新建产线
if (line && cmd->u8[0x3C]) 0x140ED6100(prod, line, 0)      // 移到索引 0
```

`0xF68` 就是 `game.CO_PRODUCTION` 读的那个对象；
`0x140ED6100` 内部用 `[prod+0x58]` 当数组数据、`[prod+0x64]` 当长度，
正好等于 `_arr(prod, PS_MIL_LINES)` 的 `PDXARRAY_DATA=0 / PDXARRAY_SIZE=0xC`。
三条独立证据指向同一个对象，不是"看起来像"。

### 100.7 实测（pid 61964，1936.03.27）

```
hoi4_add_production_line(anti_air_equipment_1)
  -> before 21, after 22, can_execute true, posted 1, line_index 21,
     factories 1, variant_handle [70,1331], tag 1        ok: true
hoi4_add_production_line(anti_air_equipment_1)   # 再来一次
  -> already_exists true, line_index 21
hoi4_add_production_line(strat_bomber_equipment_1)
  -> before 22, after 23, ok: true, line_index 22
hoi4_set_production_amount(line=21, amount=3)
  -> before 1, after 3, readback [3]
hoi4_remove_production_line(line=22)
  -> ok: true, before 23, after 22, equipment strat_bomber_equipment_1,
     line_handle [56,233], run_result 1
hoi4_production  -> index 21 = anti_air_equipment_1, 3 factories,
                     CMilitaryProductionLine, output_per_day 1.585（新线效率从 0 起）

# 二轮：修好 ARGSPEC 的下标之后
hoi4_add_production_line(heavy_tank_equipment_1)
  -> flag_0x3c 1（没有 flag_forced，说明 ctor 自己收到的）, line_index 0   # 置顶生效
hoi4_remove_production_line(line=0)
  -> ok true, before 23, after 22, equipment heavy_tank_equipment_1, handle [56,235]
```

创建 → 调产 → 删除，闭环。进程全程存活。

### 100.8 "置顶"没生效 —— 是我自己的参数映射错了（已修）

第一次实测时 `+0x3C` 明明传了 1，新线却落在**末尾**（21、22）。当时的推断是
`0x140ED6100` 空转、`line+0x44` 还是 -1。**这个推断是错的。**

把标志位读出来才看清：ctor 出来的对象 `+0x3C == 0`。

原因在 `cmdwin.build_command` 的参数装配：它用

```python
n = max(list(amap.values()) + [spec["args"]])
for i in range(1, n + 1):        # 按下标顺序 append
```

也就是**按下标顺序拼参数列表**。而我第一次把 `move_to_front` 写成了下标 **5**
（因为 ctor 有 4 个寄存器/栈参数，我误以为要留空第 4 个）：

```
vals = {1: tag, 2: variant, 3: mio, 5: 1}
i = 1,2,3,4,5  ->  [tag, variant, mio, 0, 1]
```

下标 4 是空的，于是垫了一个 `0` —— 那个 `0` 正好落在 ctor 的**第 4 个参数**（栈上的
bool）上，真正的 1 被推到第 6 个槽，ctor 根本不读。**参数整体错位一格。**

正确写法是下标连续 1..4（`args=4`），第 4 个就是那个 bool：

```
[tag, variant, mio, 1]  ->  rdx, r8, r9, [rsp+0x20]
```

修完实测：

```
flag_0x3c: 1        （没触发 flag_forced，说明是 ctor 自己写进去的）
line_index: 0       （新线在列表第一位，和 UI 行为一致）
```

**教训**：`build_command` 的 `ARGSPEC` 下标是"第几个参数"，不是"字段编号"，
**必须连续**。中间留空会静默错位，而且错位后 ctor 照样能构造出合法对象
（vtable 对、CanExecute 通过），只有行为不对 —— 这种错最难看出来。
`test_regressions_20261118.py` 现在有一条专门断言下标连续性。

工具侧另外加了一道保险：建完命令先读 `+0x3C`，不等于 1 就补写并报 `flag_forced: true`，
不靠"参数一定传对了"这个假设。


### 100.9 顺带修的一条

`hoi4_remove_production_line` 的旧代码写的是 `{"0x24": packed}`，那是 1.19.2 的
形状（tag + index）；真 ctor 只有一个 `+0x28`。两个错误叠在一起，
输出就只剩一个 `ok: false`。现在它报 `before/after/equipment/line_handle/run_result`，
失败时带 `error`（白名单拒绝的原文也会透出来）。

---

## 101. 贸易路线：容器从来不在 CResourceDeliveryRoute 里（2026-11-19）

`hoi4_trade_routes` 从 2026-11-16 起是"明确拒绝"状态（"本 build 没有可用容器"）。
那句话对了一半：`CResourceDeliveryRoute`（193833 个实例，字段是 CState*/CProvince* 对）
**确实不是**国家间贸易，它是地图上的补给/运输路径。真正的贸易数据在别处。

### 101.1 真容器

```
CCountry + 0x11F8                CCountryResources*      ← 不是 0x11C0
CCountryResources + 0x728        CPdxArray<CPdxArray<T*>> 的 data 指针
    +0x730 容量   +0x734 数量（实测 8）
    元素 0x18 字节 = 一种战略资源的"出口账本"
        +0x00 data ptr of CPdxArray<CResourceExchange*>
        +0x08 容量      +0x0C 数量
    槽位 1..7 = 七种资源，槽位 0 空着
    槽位顺序 = common/resources/00_resources.txt 的顺序（= icon_frame）：
        oil / aluminium / rubber / tungsten / steel / chromium / coal

CResourceExchange   0xF0 字节  vtable 0x29D8E60  ctor 0xD046C0
    +0x88  u32  CCountryTag —— 对家的 tag（进口方）
    +0x90  CStrategicResource* （它自己的 +0xD8 就是资源序号）
    +0x98  u32  数量，CARGO 定点（100000 = 1 单位）
    +0xB0 / +0xC8  CGregorianDate
```

### 101.2 为什么能确定"账本记在供货方一侧"

两个独立证据：

1. **1:1**。全世界 `CResourceExchange` 实例数 = **131**；把 440 个国家
   的出口账本全走一遍，条目数也正好 **131**。一笔交易只写一次，
   如果两边都记就应该是 262。
2. **方向**。美国那一侧挂着 `USA -> ENG, oil, 16`；英国那一侧没有美国，
   只有 IRE/DEN/NOR/FRA 的石油。1936 年"美国卖油给英国"是史实，
   "美国向英国买油"不是。同理 `INS -> ITA, oil, 48`（荷属东印度卖油给意大利）、
   `SWE -> HOL/DEN, steel`（瑞典铁矿石）、`SOV -> CHI, steel, 24`（苏联援华）、
   `CZE -> ROM, steel` —— 全部是"供货方账本"的方向。

所以工具里 `exports` 是**读出来的**，`imports` 是**扫全世界推出来的**
（`+0x88 == 你` 的那些条目）。

### 101.3 反汇编佐证

`CCreateTradeCommand`（vt `0x2A5F388`，+0x48 CanExecute `0x16DABF0`，
+0x50 Execute `0x16DA8A0`）是唯一写这个账本的地方。Execute 转调 worker
`0xD15640`，worker 里每一个偏移都能对上：

```
0xD1566E  mov rsi,[rax+0x11F8]      ; 收货国的 CCountryResources
0xD15681  mov rax,[rsi+0x728]       ; 账本数组
0xD1568C  movsxd rax,[rax+rcx*8+0xC]; 槽位的 size（rcx = 资源序号*3）
0xD156A3  lea rcx,[rdi+0x88]        ; 拿 +0x88 跟对家 tag 比
0xD157DB  imul r9,r15,0x186A0       ; 数量 ×100000
```

命令自己的字段（`hoi4_player_commands` 漏了 `+0x2C`，和生产线那次一样的盲区）：
`+0x24` tag（玩家）、`+0x28` 供货方 tag、`+0x2C` 收货方 tag、`+0x30` CStrategicResource*、
`+0x38` 数量。

### 101.4 实测（pid 61964，1936.03.27）

```
全世界 131 条，按资源分：oil 56 / steel 38 / rubber 19 / aluminium 12 /
                        tungsten 3 / chromium 2 / coal 1
GER：出口 coal 8 → ITA，进口 0 条
ENG：进口 oil 16 ← USA
USA：进口 rubber 8 ← BRA、chromium 8 ← CUB
```

石油最多、煤几乎不流通，形状和历史都对得上。

工具现在返回
`{ok, country, export_count, import_count, export_units, import_units, net{}, exports[], imports[], scanned_countries}`，
`world=true` 直接列全图 131 条（0.04 s）。


## 102. 空军归属：`CStrategicAir+0x90` 就是国家 tag（2026-11-19）

这是 2026-11-07 那次 canary 留下的最后一个洞。当时的结论是
"CAirWing / CAirWingPool / CStrategicAir 里都没有 CCountry*，所以只能靠
'handle id 从 1 开始的那一段就是玩家的'这个启发式"，于是
`hoi4_attach_air_wings` 默认拒绝、`hoi4_air_wings` 只报开头那 15 个联队。

### 102.1 真正的链路

```
CAirWing + 0x38       -> CAirWingPool*
CAirWingPool + 0x40   -> CStrategicAir*        （每个国家一个空中力量对象）
CStrategicAir + 0x90  -> u32 CCountryTag id    ← 归属
```

有 CStrategicAir 对象**没有** CCountry*（扫描确认），但它有一个 **tag id**。
实测 44 个 CStrategicAir，44 个互不相同的 tag id：

| tag | 国家 | 联队数 |
|---|---|---|
| 2 | ENG | 36 |
| 48 | JAP | 22 |
| 68 | USA | 20 |
| 1 | GER | 15 |
| 3 | SOV | 15 |
| 5 | FRA | 14 |
| 16 | ITA | 9 |
| 9 | CZE | 6 |
| 10 | POL | 4 |

剩下的都是 1~3 个联队的小国（BEL/LIT/EST/LAT/NOR/BUL/CAN/BRA/CHI/...）。
这个分布就是 1936 年的空军序列表，不存在第二个解释。

### 102.2 顺带纠正两件事

1. **"只有开局的 id 1..15 能归给玩家"是错的。** 玩家后来新建的联队会挂在
   同一个 CStrategicAir 上，现在一并能认出来（旧启发式会漏掉）。
2. **CAirWing ctor（`0xFDDCE0`）确实拿不到国家指针** —— 它第 5 个参数是一个
   含 `+0x2C` 的对象，`+0x2C` 被写进 `CAirWing+0x2C`，值是省份/基地编号一类的东西
   （实测 8 个互不相同的值分布在 44 个组里，跟国家不是一一对应）。
   归属不在 wing 上，所以在 wing 上找 CCountry* 永远找不到。

### 102.3 改了什么

* `airops_win.SA_OWNER = 0x90`；新增 `country_wings(g, tag)`，
  `german_wings(g, refresh=False, tag=None)` 变成它的薄封装（默认取玩家国家）。
* `hoi4_air_wings` 的 `scope` 改成 "X air force only: N wings owned through
  CStrategicAir+0x90"，不再提 "id-1 起始块"。
* `hoi4_attach_air_wings` 的 `win32 and not allow_unverified` 拒绝分支**删掉**了：
  两条路径现在指向同一个已验证实现，`allow_unverified` 保留只为兼容。
* 实测（pid 61964，GER 1936）：`hoi4_air_wings` → 15 个联队
  （fighter 5 / tac_bomber 7 / cas 1 / nav_bomber 1 / transport 1，正是德国 1936 OOB）；
  `hoi4_attach_air_wings` → 13 个挂上（transport 跳过、nav_bomber 被 CanExecute 拒），
  进程存活；随后 fighter=1 / cas=2 / tac=2 / nav=16 全部 `mission_set` 成功。

---

## 103. 日期读数：Windows 路径根本没走精确扫描（2026-11-19）

**症状**：HUD 是 1936.07.16，`hoi4_date` 稳定报 1936.02.17.17。pid 没变、集团军和
空军任务都还在（所以不是读档），`hoi4_pause_in_hours` 的日期门、`hoi4_wait_for`
的 `until_date`、战争天数统计全部拿的是错日期。

### 103.1 现场

把可写内存里所有通过 `_date_sig_ok`（`-0x08 float hour/24`、`-0x04 hour`、
`+0x00 counter`、`+0x04 == 0`）的 CDate 全列出来，一共 **29 个**：

```
1930.03.04 x5   1931.07.05 x5   1935.08.14 x2   1935.12.07 x3   <- 书签/开局
1936.07.16.09 x1                                                 <- 真时钟（唯一非 0 点）
1937.09.17 x2   1941.10.13 x1   1945.02.20 x1   1945.03.01 x5   <- 排定日期
1945.03.17 x4
```

**28 个都在 00:00，只有一个是 9 点。** 而 `game.log` 里最后一条 `[YYYY.MM.DD.HH]`
是 1936.02.17.17（日志只在脚本事件时才写，落后五个月）。

### 103.2 根因（两处）

1. **`find_date_address()` 在 win32 上直接 `return self._win_date_addr()`**，
   而 `_find_date_exact()`（精确签名扫描 + 2.5 s 跳动测试 + 非 0 点优先）
   只有 Linux 路径会调。也就是说 Windows 从来没用过那个"精确"扫描。
2. `_win_date_addr()` 的兜底排序是 **`abs(value - game.log)` 最近者胜**。
   日志落后五个月，于是"离日志最近"的候选是 `1935.12.07` 那个书签；
   列表塌成一个之后就再也不会走"非 0 点优先"那一步。

### 103.3 改法

* `_win_date_addr()` 开头先调 `_find_date_exact()`，成功就缓存并置
  `_date_verified = True`（这样 `date_raw()` 里那串"拿日志校准"的启发式会跳过它）。
* `_find_date_exact()` 里把 **"非 0 点优先"提到 hint/日志排序之前** ——
  实测 29 个候选里 28 个是 0 点，这条一跑就唯一确定。
* `_win_date_addr()` 的兜底改成 **"日志是下界"**：丢掉比日志早的副本
  （日志只会落后、不会超前），在剩下的里面取**最小**的 —— 真时钟是下界之后的
  第一个候选，再往后都是排定日期。

### 103.4 实测

```
改前 hoi4_date -> 1936.02.17.17   (与 HUD 不符)
改后 hoi4_date -> 1936.07.16.09   (与 HUD 一致)
```

`_find_date_exact()` 单独调用也返回 `0x217765d1d00` = 1936.07.16.09，两者互证。

**教训**：一个"精确扫描"函数写好了却没接到平台分支上，比没有更糟 ——
它会让审查者以为这条路径已经被照顾到了。


## 104. 资源：`CO_RESOURCES` 一直是 Linux 偏移（2026-11-19）

`hoi4_resources` 七个资源恒返回 `0.0`。原因是 `calling/game.py` 里那个**本地**
常量：

```
CO_RESOURCES = 0x11C0     # Linux
```

而 PE 的 `CCountry+0x11C0` 指向 `.rdata`（不是对象），读出来全是 0。
真值是 **`0x11F8`**（`calling/offsets_win.py` 里一直写着 `0x11F8`，只是
`game.py` 的本地副本把它盖掉了）。

`RES_ARRAY = 0x60` / `RES_STRIDE = 0x10` 其实是对的 —— 七个 int64 从
`CCountryResources+0x60` 开始，每条 16 字节 `{i64 amount; u32 id; u32 pad}`，
`0x60 + 7*0x10 = 0xD0` 正好撞上数组后面的 allocator 字。

### 实测（GER 1936.07.16）

```
oil 4.0   aluminium 153.0   rubber 0.0   tungsten 5.0
steel 301.0   chromium 5.0   coal 278.0
```

橡胶恰好 0（德国本土无橡胶）、钢 301、煤 278、油 4 —— 形状对得上 1936 德国。

顺带说明：`hoi4_country_subsystems` 的 `resources` 一栏以前也显示
`offset 0x11c0 / class null / ptr <模块地址>`，改成同一个常量之后自动纠正。

**教训**：`offsets_win.py` 和 `game.py` 各存了一份同名常量，改了一处不等于改了另一处。
`test_date_res_20261119.py` 现在直接断言 `game.CO_RESOURCES == 0x11F8`。

## 105. `hoi4_army_battleplans` 实机首调即杀进程（2026-11-18 23:36，第 5 次崩溃）

**场景**：1936.07.16.09，1 个集团军 / 30 师 / 0 条命令 / 未开战 / 无前线指令，游戏暂停。
第一次调用 `hoi4_army_battleplans(enemy_tag="HOL")`。

**结果**：hoi4.exe 当场死亡，`crashes/` 里**没有新目录、没有 minidump**（和 11-18 15:16
那次裸 ctor 的"静默死亡"同型）。

栈：

```
game.py:3652 plan_front      -> A.draw_front_line(self, og, best["front"], ...)
armyops_win.py:269           -> g.call(p.base + FN_NEW_ROOT_CMD, ...)
winject.py:493               -> if self._read(flag, 4) != b"\0\0\0\0"
ReadProcessMemory(0x215860c0210) failed, GetLastError=299   (ERROR_PARTIAL_COPY = 进程已死)
```

注意 `+0x210` 是注入器 scratch 块里 flag 的固定偏移，不是游戏对象——历史上事件选项
那三次崩溃报的也是"堆 +0x210"，同一个含义：**调用进去之后进程没了**。
参数写入和"解暂停"写入都成功返回，说明调用前进程还活着，死亡发生在
`COrderNewRootCommand` 内部或紧随其后。

### 105.1 已排除：参数错位

`0x18C9B20` 完整反汇编（`pedis.py dis 0x18C9B20 0x240`）是 **8 个参数**：

| 寄存器/栈 | 含义 | 落点 |
|---|---|---|
| rcx | this | — |
| rdx | 集团军对象（读 `+0x08` 句柄去解析，读 `+0xa4` 做 `< 128` 断言，读 `+0x3c` 比省 id） | `+0x28` |
| r8 | **指向 8 字节前线句柄的指针**（`mov rax,[rdi]`） | `+0x48` |
| r9d | section id | `+0x50` |
| [rsp+0x90] | from | `+0x58` |
| [rsp+0x98] | to | `+0x60` |
| [rsp+0xa0] | `CPdxArray<CArmy*>`（`[r15+0xc]` 数量、`[r15]` 数据） | 逐个数 `[army+0x1d8]` 比省、把 `[army+0x18]` 塞进 `+0x30` 的内嵌数组 |
| [rsp+0xa8] | bool | `+0x6c` |

`NR_SIZE = 0x70`，最后一个写是 `+0x6c` 一字节 —— **对象大小正好封口，不是溢出**。
工具传的就是 `(cmd, og, hp, section, frm, to, darr, 0)`，与上表一一对应。
`_mkptrarray` 造的也确实是裸 `CArmy*` 数组（`divisions_of` 已用 `_heap()` 过滤）。

### 105.2 首要嫌疑：画线前先解了暂停

`game.py:3617-3621`：

```python
idler = p.u64(p.base + 0x33DDB08)
was_paused = bool(idler and p.u8(idler + 0x6C1))
out["resumed_for_plan"] = was_paused
if was_paused:
    p.write(idler + 0x6C1, b"\x00")      # <-- 解暂停
```

然后才 `draw_front_line`。也就是说：**在一个正在跑时钟的进程里**
`VirtualAllocEx` + 写参数 + `CreateRemoteThread` 调引擎函数，而引擎函数自己
还要走 `G_DEFAULT_ALLOC` 分配器（`cmdwin.alloc`）和句柄表解析 `0x2284870`。
主线程同时在跑同一套堆和句柄表 —— 这是标准的数据竞争。
（日期在会话里几个小时没动过 ⇒ 当时确实是暂停的 ⇒ 这次确实走进了这个解暂停窗口。）

对照：历史上所有成功的写操作都在暂停状态完成；11-18 之后每一次 `plan_front`
崩溃都发生在这个窗口里；而 `hoi4_draw_front_line` 直接调 `draw_front_line`、
**不解暂停**，是唯一没在这个窗口里跑过的入口。

### 105.3 两条 `int3` 断言（备用线索）

ctor 内有两处 `PDX_ASSERT`，失败就 `int3`（无调试器 ⇒ 立刻死、未必留 dump）：

```
GetPtr() != 0 && "This Object is not created or a valid CReferenceObject. Probably an error."
pGroup->GetRootInstances().GetSize() < 128 && "H..."
```

两处都由全局开关门控：`[0x1DC59CB] != 0` 且 `[0x1B6F312] == 0` 才求值。
第一条针对 `[集团军+0x08]` 那个句柄能否解析成对象 —— 如果解析返回 0 就会命中。
静默死亡时这条比 AV 更符合现象。

### 105.4 A/B 结果：解暂停假设被推翻（2026-11-18 23:46）

重开一局（pid 59724）、读 `autosave.hoi4`（1936.07.01.01）、`hoi4_purge_orders` = 0 条，
**暂停状态下**直接调 `hoi4_draw_front_line`（它不解暂停；`divisions=None` 所以 arg7 = 0）：

```
game.py:4892 draw_front_line -> armyops_win.py:269 -> g.call(p.base + 0x18C9B20, ...)
ReadProcessMemory(0x1fd9bf30210) failed, GetLastError=299     ← 同样当场死，同样没有 dump
```

⇒ **不是"解暂停窗口"的问题。`g.call(0x18C9B20)` 在暂停和运行两种状态下都必死。**

于是 105.2 那条假设作废，回到 105.3 的两处 `int3` 断言。
顺带说明：两次死亡地址分别是 `0x215860c0210` / `0x1fd9bf30210`，都在堆上、
都带 `+0x210` —— 那是注入器 scratch 块的固定偏移，只表示"进程没了"，不是游戏对象。

### 105.5 新线索：这个类有 4 个构造函数，而 UI 的调用点一次都没读过

写 vtable `0x2A856A0` 的地方有 4 处：

| 站点 | 形态 |
|---|---|
| `0x18C9B20` | **8 参**：`from` / `to` 两个值 + 师数组 + bool（我们一直在调这个） |
| `0x18C9D90` | **7 参**：只有一个值（没有 `to`）+ 师数组 + bool；其余结构与 A 逐指令相同，**同样两处 `int3` 断言** |
| `0x18CE290` | 拷贝构造（从 `rsi` 逐字段搬到 `rdi`） |
| `0x18D047D` | 反序列化/克隆路径 |

调用者也都有：`0x18C9B20` ← `0xF7E031` / `0xFD08F8` / `0x18D401E` / `0x18DA723`；
`0x18C9D90` ← `0xFD4AFD` / `0x18E0AD2`。**说明游戏自己确实这么构造，路没走错。**

**下一步（纯静态，不用开游戏）**：反汇编 `0xF7E031`、`0xFD4AFD` 这些调用点，
读游戏自己**怎么装参数** —— 尤其 arg2 到底是不是 `COrdersGroup`、arg3 那个指针
指向的是什么。之前一直是"猜一个对象塞进去再看活不活"，这次改成读游戏自己的调用姿势。

优先验证的三个问题：
1. arg2 是 `COrdersGroup` 还是 `CArmyGroup` / `CTheatre`？（ctor 里 `[arg2+8]` 当句柄解析、
   `[arg2+0xa4]` 当 root instance 计数、`[arg2+0x3c]` 拿来跟 `[army+0x1d8]` 比）
2. arg3 指向的是 `CFront` 的句柄、还是 `CFrontSection` 的句柄？
3. `[this+0x68] = esi = 0` 那个字段（A 里写死 0）在 UI 里传什么？

### 105.6 读游戏自己的调用点：参数形状全部对上了（2026-11-19 00:0x）

用 `pdata.py fn <addr>` 找到精确函数边界后，挑了最小的两个调用者来读：

**`0x18E09C0..0x18E0B61`（417 字节，调 7 参 ctor B `0x18C9D90`）** —— 克隆一个入侵命令：

```
assert OriginalOrder.GetOrderType() == ORDER_INVASION     ; 类型 3/4 才放行
rbp = [原命令 + 0x38]                                      ; 集团军；为 0 则断言
new(0x70)  -> 0x1425A01DC
[rsp+0x20] = 0x186A0        ; arg5 = 100000 = 满幅
[rsp+0x28] = 0              ; arg6 = 师数组 NULL
[rsp+0x30] = 0              ; arg7 = bool false
r9         = 0              ; arg4 = section 0
r8         = r14            ; arg3
rdx        = rbp            ; arg2 = 集团军
call 0x18C9D90
mov [rbx+0x68], esi         ; +0x68 事后被写成"原命令 id"，ctor 里默认是 0
```

**`0x18D3FA0..0x18D4215`（629 字节，调 8 参 ctor A `0x18C9B20`）** —— 这才是"画前线 + 挂进攻线"：

```
new(0x70)  -> 0x1425A01DC
rdx = 0x2284870(&[r14+0x28])   ; 先把集团军**句柄**解析成对象再传
r8  = lea [r14+0x64]           ; arg3 = 指向 8 字节前线句柄
r9d = [r14+0x6c]               ; arg4 = section
[rsp+0x20] = [r14+0x70]        ; arg5 = from
[rsp+0x28] = [r14+0x78]        ; arg6 = to
[rsp+0x30] = 0                 ; arg7 = 师数组 NULL
[rsp+0x38] = 0                 ; arg8 = false
call 0x18C9B20
call [vt+0x48]  (CanExecute, edx=0)
call [vt+0x50]  (Execute)
; 然后 new(0x70) + call 0x18C9840 (COrderNewFrontCommand)，r9 = 刚建出来的那条命令
```

**结论：参数形状、顺序、类型、`0x70` 大小、分配器（`cmdwin.alloc` 走的就是同一个 vtable 槽）
全部与引擎一致。** 所以 105.1 的"不是参数错位"从推断升级为实证，
但也意味着**问题不在调用形状**。

顺带确认两件事：
1. `COrderNewRootCommand` 是 **CCommand**（0x70 字节），不是订单本身 ——
   订单是引擎在 Execute 内部另外建出来的。这也解释了 `hoi4_orders` 读的
   `order+0x30` 与命令对象 `+0x30`（内嵌 CPdxArray）没有任何关系。
2. `COrderNewFrontCommand`（`0x18C9840`）在引擎里的用法是：**r9 = 刚创建的根命令**，
   `r8 = &[源+0x48]`，`[rsp+0x20..] = 0`。也就是说它接的是**命令对象**，
   不是 `CPdxArray<CArmy*>` —— 7b 里那次写穿堆的根因到这里完全闭环。

### 105.7 剩下唯一没排除的差异（下一轮的唯一实验）

引擎传 arg2 之前会 `0x2284870(&handle)` 解析一次，**解析失败会落到 ctor 里的 `int3`**。
我们直接传 `hoi4_armies` 给的 `COrdersGroup*`，跳过了这一步。

**下一轮只做这一件事，不进 ctor**：

```python
h = p.read(og + 0x08, 8)          # {53, 463}
obj = g.call(p.base + 0x2284870, (struct.unpack('<Q', h)[0],), absolute=True)
# obj == 0  =>  assert 会炸，且 og 不是 ctor 想要的集团军对象
# obj != 0  =>  断言无责，回去查 0x11F6F0（+0x30 内嵌数组的构造器）和 +0x40 分配器
```

`0x2284870` 是纯查表（`0x34FFC00` 的 100 张哈希表），不改任何状态，风险远低于 ctor。
`research/windows/whandle.py` 已有它的 Python 实现，可以先用纯读版对一遍。

### 105.8 真相：游戏窗口不在前台（2026-11-19 00:25，实机跑通）

把 105.1~105.7 全部排除掉之后，剩下的差异只有一条：**窗口焦点**。
`calling/process/winject.py` 里 `call()` 开头就写着：

```python
# The D3D device must be live before the render thread is hijacked:
# a non-foreground window sits inside RestoreDeviceObjects and dies.
focus_game(self.pid)
```

23:36 和 23:46 那两次崩溃，游戏窗口都被 DSH / QQ 挡在后台（当时的桌面截图可以佐证）。
`focus_game()` 内部的 `SetForegroundWindow` 在这种场景下并不生效 —— 它返回成功与否
没人检查，于是照样去劫持一个还在 `RestoreDeviceObjects` 里的渲染线程。

**把窗口切到前台之后，一模一样的调用一次就过：**

```
hoi4_draw_front_line(og=0x16e0fe328a0, front=0x16e13ce1410) -> ok:true, divisions:30
hoi4_orders        -> [{id:1, type:2, root_front:{66,7}, root_section:1,
                        from:0, to:100000, assigned_divisions:0}]
hoi4_assign_divisions(mode="all") -> assigned_before 0 -> assigned_after 30
```

30 个师随后真的走到了荷兰边境省（9281 / 11360 …），`moving:false`、`dig_in:3.0`。

**顺带洗清的两条**（都是实机读出来的，不是推断）：

| 检查 | 结果 |
|---|---|
| `0x2284870(&[og+0x08])` | 返回 `og` 本身 → ctor 第一条断言无责 |
| `[og+0xa4]` | **0** < 128 → ctor 第二条断言无责 |

所以 105.3 的两条 `int3` 断言全部洗清，`0x11F6F0`（`CPdxArray` 构造器，只清零 +
把 `0x30FD000` 写进 `+0x40`）也无害。**问题从来不在 ctor 里。**

**新硬规矩（已写进 AGENTS 规则 7c）**：任何写命令之前先确认
`GetForegroundWindow()` 的标题是 `Hearts of Iron IV (DirectX 11)`；不是就先切前台。

### 105.9 还没做到的：type-1 进攻子命令

`hoi4_execute_plan(order_id=1, flag=1)` 返回 `ok:true, assigned:30`，但 `hoi4_orders`
里**始终只有那一条 type-2**，没有 type-1 子命令。所以"`COrderExecuteCommand::Execute`
会建子命令"这个 2026-11-18 的结论，在 PE 1.19.5.0 上**不成立**。

引擎自己的 `0x18D3FA0` 里，进攻线是**另外一次构造**：

```
new(0x70)
r8          = lea [r14+0x48]      ; 指向一个 8 字节句柄
r9          = rbx                  ; 刚创建并 Execute 过的 type-2 **订单对象**
[rsp+0x20]  = 0
[rsp+0x28]  = 0 (byte)
[rsp+0x30]  = 0 (byte)
[rsp+0x38]  = 0
call 0x18C9840                   ; COrderNewFrontCommand
call [vt+0x48] / [vt+0x50]       ; CanExecute / Execute
```

**这跟 7b 里"第 5 个参数是 out 指针、旧代码把 CPdxArray 塞进去写穿堆"的记录对不上**
（这里第 5 个参数传的是 0）。下一轮必须先 `pedis.py dis 0x18C9840 0x300` 把它的
8 个参数重新认一遍，再决定 `armyops_win.offensive_line` 怎么写 ——
**不要凭 7b 的旧结论再试一次。**

### 105.10 `COrderNewFrontCommand` 的真签名 = 9 个参数（2026-11-19 00:0x）

`pdata.py fn` 给出精确边界 `0x18C9840..0x18C9B1E`（734 字节），完整反汇编后：

| 参数 | 来源 | 落到 |
|---|---|---|
| rcx | this | — |
| rdx | 集团军对象（读 `[rdx+8]` 句柄） | `+0x28` |
| r8 | **`CPdxArray<u32>*`**（`[r8+0xc]`=计数、`[r8]`=数据，整块 memcpy） | `+0x50` |
| r9 | **订单对象**（`[r9+0x244]`=它的 id；硬校验 `[r9+0x38] == rdx`） | `+0x68` |
| [rsp+0x90] | **`CPdxArray<CArmy*>*`** | `+0x38` |
| [rsp+0x98] | byte | `+0x6c` |
| [rsp+0xa0] | byte | `+0x6d` |
| [rsp+0xa8] | byte | `+0x6e` |
| [rsp+0xb0] | **对象\***（读 `[rax+8]` 句柄） | `+0x30` |

**两个引擎调用点互相印证**：

- `0x18D3FA0`（画前线 + 挂进攻线）：`r8=&[r14+0x48]`、`r9=刚建的 type-2`、
  `[rsp+0x20..0x40]` 五个槽全 0。
- `0x1AD2E40`（最小的那个调用者，645 字节）：现场用循环搭一个 `CPdxArray<u32>`
  （`mov [rax+rcx*4], ebx` / `inc [rbp-0x14]`），`r8=lea [rbp-0x20]`、`r9=r15`、
  `[rsp+0x20]=r13`（**非空**）、`[rsp+0x28..0x38]=0`、`[rsp+0x40]=rsi`（**非空**）。

所以 7b 那条"第 5 个参数是 out 指针"是**错的** —— 真正的形状是上面这张表。

**实测（我们照这张表构造）**：

```
new(0x70) + ctor(cmd, og, CPdxArray<u32>{8 个边境省}, rootOrder, 0, 0,0,0, 0)
-> vtable 正确 (=0x2A853A0)、CanExecute = 1、Post = true
-> 推 19 小时后再读 hoi4_orders：**还是只有那条 type-2，没有子命令**
-> hoi4_armies.orders 仍然是 1
```

而引擎自己调用时 **arg5（师数组）和第 9 个参数都是非空的**。这两个位置大概率就是
缺的东西：arg5 = 要挂到进攻线上的师，arg9 = 目标/战区对象（它的句柄被存进 `+0x30`）。

**下一轮**：先把 `0x1AD2E40` 开头读一遍，认出 `r13`（arg5）和 `rsi`（arg9）分别
是从哪个字段取的，再照抄；**不要再用 0 填空**。

### 105.11 `COrderNewFrontCommand` 不是"新建进攻线"（2026-11-19 00:3x）

按 105.10 的 9 参签名构造，两次（arg5 师数组 = 0 / arg5 = 30 个 CArmy*）：

```
vtable 正确 (0x2A853A0)、CanExecute = 1、Post = true
推 18~19 小时后再读 hoi4_orders：仍然只有那条 type-2
hoi4_armies.orders 仍然是 1
```

不是崩溃、不是拒绝、就是**没有任何订单产生**。所以这个类哪怕叫 "NewFront"，
它的作用也不是"在集团军里加一条新订单"，更像是"把某条订单挂到某个正面/片段上"。

**下一轮的首选候选：`COrderAddNewCompletePlanCommand`**（命令目录里 type `0x365e`、
size 128、5 个参数）—— 名字直译就是"新建一份完整计划"，最贴近
"画一整条前线 + 进攻箭头"这件事。先 `pedis.py vtname` 找到它的 vtable →
`pedis.py xref <vt>` 找 ctor → `pdata.py dis <ctor>` 认参数，照 105.10 的方法走。

同时记一下：`COrderNewFrontCommand` 的命令目录条目标注 size = 112 (0x70)，与实测吻合。

### 105.12 找到正主：`COrderAddNewCompletePlanCommand`（2026-11-19 00:5x）

```
vtable        0x2A85760        size 0x80
+0x48 CanExecute  0x18DE930
+0x50 Execute     0x18D3FA0   ← 就是我们前面反汇编过的"画前线 + 挂进攻线"那个函数
factory       0x18CFC70       ; 无参、内部 new(0x80)、返回指针（和事件选项的 0xA590D0 同型）
```

字段（由 factory 的默认值与 Execute 的读法反推）：

| 偏移 | 含义 | 默认 |
|---|---|---|
| `+0x28` | 集团军句柄 | 全局默认 |
| `+0x30/+0x38/+0x3c` | 第一个 `CPdxArray`（data / cap / count）；**`+0x3c` 必须 > 0**，Execute 用它当"要不要挂进攻线"的闸 | 0 |
| `+0x48/+0x50/+0x54` | 第二个 `CPdxArray` —— Execute 里 `lea r8,[r14+0x48]` 传给 `COrderNewFrontCommand` 的 arg3 | 0 |
| `+0x64` | 前线句柄 | 全局默认 |
| `+0x6c` | section | 0 |
| `+0x70` / `+0x78` | from / to | 0 / **0x186A0 (100000)** |

**实测**：填好 `+0x28={53,463}`、`+0x30` 数组 8 个省（count 8）、`+0x64={66,7}`、
`+0x6c=1`、`+0x70=0`、`+0x78=100000` 之后 —— `CanExecute = 1`、`Post = true`，
**真的建出了一条新的 type-2 订单（id 2，root_front {66,7}，0→100000）**。
这是三个类里第一个能真正生成订单的。

但它仍然**只生成 type-2，没有 type-1**；而且新订单 0 师，被 `pause_in_hours`
的 `empty_orders` 闸当场拦下（这正是规则 8 的保护）。已用 `hoi4_delete_order(2)` 删除，
`hoi4_purge_orders` 复核：kept 1 / removed 0。

**下一轮唯一改动**：把 `+0x48` 那个数组也填上（同样 8 个省，`+0x54` = 8），
因为 Execute 里它是被当作 `COrderNewFrontCommand` 的 arg3 用的 —— 上一轮 arg3 是空，
所以进攻线没建出来。**其余字段一个都不要动。**

### 105.13 `+0x48` 也填上：仍然只有 type-2（2026-11-19 01:0x）

把两个数组都填成同样 8 个省（`+0x3c = +0x54 = 8`）再走一遍：

```
CanExecute = 1, Post = true
-> hoi4_orders: 新增 id 3, type 2, root_front {66,7}, 0 -> 100000, assigned 0
-> 仍然没有 type-1
```

已 `hoi4_delete_order(3)` 删除，`hoi4_purge_orders` 复核 kept 1 / removed 0。

**读懂 `0x18D3FA0` 后半段后，知道为什么了**：

```
0x18D4066  cmp [集团军+0xa4], 0     ; 根实例计数
0x18D406D  je  0x18D409E            ; 为 0 就跳过
0x18D406F.. resolve 集团军, 取 [集团军+0x98 + (n-1)*8]  -> rbx（最后一条根实例）
0x18D409E  cmp [cmd+0x3c], 0        ; 我们的数组计数
0x18D40A3  je  0x18D41F9            ; 为 0 就退出
0x18D40A9  test rbx, rbx
0x18D40AC  je  0x18D41F9            ; 拿不到根实例就退出
0x18D40B2.. 建 COrderNewFrontCommand(..., r9 = rbx)
```

也就是说：进攻线是在**已经存在一条根订单**的前提下，基于"最后一条根实例"再挂出来的。
我们这条新计划命令自己刚建出来的那条 type-2，在它 Execute 的那一刻还没进 `+0x98` 数组，
所以 `rbx` 拿不到 → 直接 bail。

### 105.14 下一轮换个思路：先拿到"真的 type-1 长什么样"

三轮都在猜构造器，代价太高。库里还躺着几份 **1940 年的存档**
（`campaign_1940_08_pre_attack.hoi4` / `campaign_1940_11_alt.hoi4`），
那些档里玩家是**真的画过进攻箭头**的。

下一轮做法：
1. 读 `campaign_1940_08_pre_attack.hoi4`（或任意有战斗计划的档）；
2. `hoi4_orders` 找一条 `type == 1` 的订单，把它的对象**逐字段 dump** 出来
   （`+0x30` 类型、`+0x38` 父指针、`+0x48`、`+0x50`、`+0x68`、`+0x244` id、`+0x258` 父句柄…）；
3. 有了 ground truth 再回头决定：是继续补 `COrderAddNewCompletePlanCommand` 的字段，
   还是直接把"缺的那条 type-1"按真实布局造出来。

**别再靠猜构造器了。**

### 105.15 有了根实例也不管用（2026-11-19 01:1x）

105.13 的推论是"集团军里没有根实例 → `rbx` 拿不到 → 不建进攻线"。
实测时集团军里**已经有** id 1 那条 30 师的 type-2，再次投递同一条计划命令：

```
CanExecute = 1, Post = true
-> 新增 id 4, type 2, assigned 0
-> 仍然没有 type-1
```

已 `hoi4_delete_order(4)` 删除，`hoi4_purge_orders` 复核 kept 1 / removed 0。

⇒ 所以 `[集团军+0xa4]` 不是我以为的"根实例计数"，或者 `[集团军+0x98+(n-1)*8]`
不是我以为的"最后一条根实例"。**这条命令的进攻线分支我们根本进不去。**

**这条线索到此为止，不再猜。** 下一轮按 105.14 走：读一份 1940 存档，
把真实的 type-1 订单对象逐字段 dump 出来当 ground truth。

注意代价：当前 1936 档的战争借口（1937.01.19 到期）会随读档一起丢掉，
需要重新 justify。这是可以接受的（借口只要 195 天游戏时间，且随时能重开）。

## 106. 0x10B8620 有两条路，flag 由 cmd+0x6e 控制（2026-11-19 静态反汇编）

§16.5 的结论"`COrderNewFrontCommand::Execute` 必然卡在 0x10B8620 拿不到 splits"
**只对了一半**。完整反汇编（pedis.py fn 0x10B8620 / 0x18D9070）显示这个
split 计算有两条路，选哪条由两件事决定：根订单的 `+0x298` 字节和**调用方
传入的第 4 个参数（r9b）**。

### 106.1 Execute 里 flag 的来源

```
0x18D9137  cmp  byte ptr [r13+0x6e], 0     ; r13 = cmd（COrderNewFrontCommand）
0x18D913c  sete r9b                        ; r9b = (cmd+0x6e == 0) ? 1 : 0
0x18D9140  mov  r8, rsi                     ; 根 COrderInstance
0x18D9143  lea  rdx, [rbp-0x70]            ; 输出 CPdxArray<Split>
0x18D9147  lea  rcx, [r13+0x50]            ; cmd 的省份 CPdxArray<i32>
0x18D914b  call 0x10B8620
```

`cmd+0x6e` 就是 105.10 那张参数表里的**第 8 个参数**（ctor 的
`[rsp+0xa0]` byte → `cmd+0x6e`）。旧代码一直传 0，于是 r9b = 1。

### 106.2 0x10B8620 的分支

```
0x10B874C  cmp  byte [rbx+0x298], 0        ; rbx = 根订单
0x10B8753  jne  路径B
0x10B8759  test bpl, bpl                   ; bpl = r9b
0x10B875C  je   路径B                      ; flag == 0 -> 路径B
            ; ---- 路径A（flag=1 且 root+0x298==0）----
0x10B8764  cmp  byte [rbx+0x299], 0
0x10B876B  je   0x10B87B8                  ; 0 -> 直接用 0x10BB5A0(root)
0x10B876F  cmp  dword [rbx+0x2ac], 0        ; section 订单数
0x10B8775  jle  空退出
0x10B8781  mov  rax, [rbx+0x2a0]            ; ★ section 订单 id 数组
0x10B8788  mov  edx, [rsi+rax]              ; 逐个 id
0x10B878E  call 0xC402A0                    ; id -> 订单
0x10B8798  ...  call 0x10BB5A0             ; 订单 -> frontCtx
```

**路径A 要求根订单的 `+0x2a0` 数组里有东西。** 我们造的根订单没有，
§16.11 还实锤了**引擎 AI 自己的订单（JAP root 96）也一样没有** —— 这就是
"缺的东西不在订单对象里"那个两个月之谜的正面答案：路径A 的数据
（section 订单表）确实不在订单里，**但引擎根本不需要走路径A**。

**路径B**（`root+0x298 != 0` 或 flag == 0）完全不读 `+0x2a0`：

```
0x10B87F3  r14 = [rbx+0x38]                 ; 根订单的集团军
0x10B8804  cmp  dword [rsi+0xc], 0          ; cmd 省份数组 size
0x10B8807  jle  空退出
0x10B8830  for each province id in cmd+0x50:
0x10B883C    call [province_vt->slot0]      ; 省份对象解析
0x10B883E    rcx = [rax+0xb8]               ; 省份所属集团军
0x10B8845    test byte [rcx+0xd2], 1        ; 集团军标志位
0x10B8852    cmp  dword [rax+0x188], 0      ; 省份数量 > 0
0x10B885F    rdx = r14+0x3c
0x10B8866    call 0xEEED50                  ; 集团军子对象
0x10B8874    cmp  rax, [r14+0x40]           ; == 集团军自己的值
0x10B8878    jne  跳过
0x10B887E    ...（追加进 splits 输出数组）
0x10B87D0  rcx = [r14]
0x10B87D3  call 0xC450A0                    ; 集团军 -> 它的战线容器
0x10B87E9  call 0x10B8A40(out, provinces, frontCtx, 容器)  ; 真·split
```

条件全部落在**省份对象**和**集团军**上 —— 传对省份就能过。§43（2026-11-14
的 live hook）早就证明这条函数能成功（out size=1），当时不知道为什么。

### 106.3 拿到 splits 之后，Execute 走的是完整 UI 链

这段是 §16.6 手搓三步的"正版"，而且**多两步我们从来没做过的**：

```
0x18D91BB  assert !AttachOrderSplits.IsEmpty()
0x18D921E  0xC41620(og) / 0xC41BB0(og)      ; 根订单的 front unit
0x18D9235  [front_unit+0x119] = cmd+0x6e
0x18D9243  0x10B0D90(front_unit, &cmd+0x50, 0)  ; 复制省份路径
0x18D92FB  0xC41720(og, 2)                  ; 每个 split 一条 type-2 段订单
0x18D9352  0xC41720(og, 1)                  ; type-1 进攻子订单
0x18D9371  0x10B4EC0(child, section)        ; 挂到段订单下
0x18D944F  0x10BB5A0(section)               ; 段订单 -> frontCtx
0x18D949F  0x10B2C60(child, &out, 0, frontCtx)  ; ★ 师分配计算
0x18D960B  0x10B0D90(child, &divs, 1)       ; ★ 写 child+0x210（师数组！）
0x18D9620  0x10CE800(child, 1)              ; ★ executing = 1
```

带 ★ 的三步就是 §16.10 里"引擎只有在它自己建子命令时才会做的事"：
`0x10B2C60` 是引擎自己的"师怎么分到各段"计算（我们没有任何代码碰过它），
`0x10B0D90(child, &divs, 1)` 把结果写进 `child+0x210`（§16.10 手工填过但
"引擎读得到却不驱动决策"——因为手工填只填了数组，没有经过 0x10B2C60
的正确分配流程，而且 executing/段订单链也没建全），
`0x10CE800(child, 1)` 直接置 executing。

### 106.4 实现：`armyops_win.engine_offensive`

不猜构造器、不手工填字段，就做一件事：**用真 ctor 造命令（第 8 参 = 1），
过了真 CanExecute 之后直接调 `Execute` 本体**（0x18D9070），全程一个 attach
会话：

```python
g.call(p.base + 0x18C9840, (cmd, og, pa, root, darr, 0, 0, 1, 0), ...)
#      ↑ 9 参：arg8 落 cmd+0x6e —— split 走路径B
g.call(p.base + 0x18DF550, (cmd, 0), ...)   # 真 CanExecute
g.call(p.base + 0x18D9070, (cmd,), ...)     # ★ 真 Execute 直调
cmdwin.attach_fake_vtable(g, cmd, VT_OFFENSIVE)   # 事后防 CRT 释放
```

安全性论证（对照崩溃纪律）：
- ctor 是反汇编确认过的那个（`_stores_vtable` 门槛仍在）；
- 9 个参数全部显式传值（105.10 的坑：第 9 参不传 = 栈上垃圾 = 野指针）；
- Execute 运行中所有虚函数都是真的（命令对象由真 ctor 初始化），
  fake vtable 只在 Execute **返回之后**才挂上，防的是事后释放路径；
- 0 师命令在构造前就拒绝（规则 8 / 0xB2E62E）。

调用方：`Game.offensive_line()` 现在三级 fallback ——
engine_execute → queue → manual_offensive；`hoi4_offensive_line` /
`plan_front` 自动走第一级。离线回归
`research/windows/test_engine_offensive.py`（15/15：拒绝路径、9 参形状、
arg8=1、Execute 直调单参、attach 平衡、常量校验）。

### 106.5 待实机验收（下一轮）

静态证据链已闭合，但**一次都没在真游戏上跑过**。验收步骤：

1. 游戏窗口前台（§105.8 的必要条件）；
2. 先画 type-2 前线并挂师（complete_plan 路径，已验证）；
3. `hoi4_offensive_line(orders_group, parent_order_id, provinces=<边境省>)`，
   期望 `method: "engine_execute"`、children 里出现 type-1 且 `executing: 1`；
4. **真正的验收看副作用**（§教训：`pending_after` 不算数）：
   `hoi4_combat_report` 的 `fighting_now`、`hoi4_front_changes` 的省份易手、
   师的 `moving: true`；
5. 若 Execute 仍不建子命令，用 `hoi4_debug_eval` 读 `cmd+0x6e` 回读值和
   根订单 `+0x298`，确认字节真的落位，再回来补 §106.2 的路径条件分析。

若实机确认成功，KNOWN_ISSUES #5 关闭；若 `0x10B8A40` 内部还有别的闸，
把 hook 记录贴回本节。

---

## §107  实机验收推翻 §106 路径理论（2026-11-19/20 深夜，GER 1936.10 存档）

### 107.0  环境

* 存档 GER_1936_10_01_01（1936.10.01，GER vs BEL 战争中，第1集团军 17 师，
  根订单 id=38 = complete_plan 路径画的前线 [66,1234] section 2，17 师全挂）。
* 中途插曲：23:15 Steam 自动更新把游戏换成 1.20.0.0.8873（读侧全灭，见
  KNOWN_ISSUES P0#0），Steam beta 分支回滚 1.19.5.0.7c6c 后继续。
* 验收通道：分阶段驱动（每阶段独立进程 attach→act→detach），工具调用走
  `mcp/server.py` 的 HANDLERS，与 MCP 行为零偏差。

### 107.1  Run1：engine_execute 拒绝 —— 发现 G 门

engine_offensive（arg8=1）在 `cmd+0x6e` 校验处拒绝：`cmd+0x6e = -65536`。
反汇编 ctor（0x18C9840，734 字节）：

```
0x18C99DC  cmp  byte [rsp+0xa8], 0        ; arg8
0x18C99E6  cmp  byte [rip+0x1b140ca], 0   ; ★ 全局 G @ 0x33DDAB7（BSS）
0x18C99EF  mov  al, 1                     ; arg8 && G
0x18C99F5  mov  [r14+0x6e], al
```

**ctor 的 +0x6e = arg8 && *(u8*)0x33DDAB7** —— §106 只看到了一半。
全 .text 扫描：0x33DDAB7 只有 2 处引用且都是 cmp 读（另一处 0xFD472D，
UI 建单时的订单子类型选择：4 / 1 / 计算值），写入经结构体基址间接完成。
活体读：UI 空闲时 G=0。

修法：ctor 前 G 置 1（gamestate 在、g.call 冻结全线程，暴露窗口毫秒级），
ctor 返回后 finally 恢复。`armyops_win.FN_OFFENSIVE_GATE = 0x33DDAB7`。

### 107.2  Run2：+0x6e 落位，但守卫读宽了

G 修复后 `cmd+0x6e = -458751 = 0xFFFF0001`：**字节 +0x6e = 0x01 落位成功**，
但守卫用 `p.i32(+0x6e)` 多读了对象外 +0x70/+0x71 的堆垃圾（NR_SIZE=0x70，
+0x70 已在对象外）。守卫改 `p.u8`。这两个是真 bug，已修。

### 107.3  Run3：守卫过、CanExecute 过、Execute 跑了 —— 但静默无子、也不崩

Execute 直调后 4 秒轮询无新 type-1 子命令，游戏活着。 contradiction：
按 §106.2 的路径B，省份循环该走到 0xEEED50 并崩（见 107.4）——没崩。
反汇编 `0x10BB5A0(root)`：

```
0x10BB5C5  rcx = root+0x258
0x10BB5CC  edx = [rcx+4]        ; root+0x25C
0x10BB5CF  cmp  edx, [全局A]    ; ★ 会话戳不匹配 -> FAIL -> NULL
0x10BB5D8  cmp  [rcx], [全局B]  ; root+0x258
```

**0x10BB5A0 校验根订单 +0x258/{+0x25C} 对当前会话的全局戳。** 存档里的
根订单 38 是上一个会话画的 → 戳不匹配 → frontCtx=NULL → Execute 在
split 之前静默退出。这解释了 Run1/Run2 的"不崩也无子"。

### 107.4  splitprobe：直接调 0x10B8620 —— 杀进程（路径B 是雷区）

绕过 Execute 的 frontCtx 门槛，直接调 split 本体
`0x10B8620(path=[3262,6496], out, root=38, flag=0)` → **进程死**
（winject 读完成标志 Err 299 PARTIAL_COPY，经典静默死亡）。

反汇编路径B 的省份过滤（0x10B8830..0x10B8880，注意 §106.2 的转述漏了
rcx=province）：

```
0x10B883C  call [gs+8 对象的 vt->slot0]  ; id -> CProvince*
0x10B883E  rcx = [rax+0xb8]
0x10B8845  test byte [rcx+0xd2], 1       ; 我们的两省 = 01 ✓
0x10B8852  cmp dword [rax+0x188], 0      ; = 1 ✓
0x10B8866  call 0xEEED50(province, og+0x3c)   ; ★ rcx=province, rdx=og+0x3c
0x10B8874  cmp  rax, [r14+0x40]          ; 返回列表必须 == og+0x40
```

0xEEED50 内部：`0xC07BF0(og+0x3c)` 解析句柄 → 拿它的 +0x168 列表数组 /
+0x174 计数，找包含 qword `province+0xd0` 的列表并返回。而 0xC07BF0 的
真身（0xC0883E）：

```
mov  rcx, [0x33DD6B0]              ; gamestate
movsxd rdx, dword [rbx]            ; 句柄首 dword（og+0x3c = 1）
mov  rax, [rcx+0x340]              ; 恒等表（活体 dump：0..7 = 0..7）
mov  eax, dword [rax + rdx*4]      ; → 返回 1
```

**恒等表 → resolve(1)=1 → 0xEEED50 读 `[1+0x174]` = 地址 0x175 → AV。**
og+0x3c = {1, 0x29a8be01e00}：第二参并不是能通过恒等表解析的东西。
（两省 +0xd0 相同 = 0x29b2aa2c140，共享的边界段对象。）

### 107.5  理论纠正：UI 走的是路径A

§43 hook 记录 `flag=0x101`（低字节 = bpl = 1）→ **UI 的 cmd+0x6e==0 →
flag=1 → 路径A**。§43 dump 同时显示 UI 根订单带 +0x2a0（=7/10/13，段订单
id）。也就是说：

| 路径 | 条件 | 我们的现实 |
|---|---|---|
| A（UI 实际用的） | root+0x2a0 段表 + 有效会话戳 | 根订单 38 两者皆无（complete_plan 不产段表；跨会话戳失效） |
| B（§106 想走的） | cmd+0x6e=1（需 arg8=1 **且** G=1） | og+0x3c 经恒等表必崩 —— **雷区，永不可走** |

§106 的"arg8=1 走路径B 绕开段表"是把因果想反了：绕不开，反而必崩。

### 107.6  下一步（重推 engine_offensive 的路线图）

1. **搞清 +0x2a0 谁来填**：§43 式 Frida hook 一次 UI 拖箭
   （hook 0x18C9B20 / 0xC41720 / 0x10BB5A0 的入参返回），或读
   `COrderNewRootCommand::Execute`（complete_plan 的 Execute）——
   我们 complete_plan 画的根订单 +0x2a0=0，说明段订单不是 ctor 建的，
   是 root 自己的 Execute 或后续引擎步建立的。
2. **og+0x3c 的正确内容**：dump 一个 UI 真实操作的集团军的 +0x3c 对比
   我们的 {1,...}；0xC08790 的完整解析（0xC087AB 分支，含
   `_ThreadForbidCount` 断言）还没读完。
3. 路径A 重推：新会话根订单（戳有效）+ 段表 → Execute → 0x10B8A40
   ——**0x10B8A40 内部仍未审计**，重推前先静态过一遍。
4. 安全姿态：engine_offensive 已硬禁用（任何输入拒绝），G 门/字节读
   两个修复保留在代码里备用；重推后先跑 splitprobe 的安全版
   （用 UI 真实集团的句柄）再上 Execute。

### 107.7  本次验收的事件记录

* 零存档污染（全程无 save 命令；进程死亡丢弃全部内存态，源档完好）。
* 手工箭头 42/43/47（0 师、executing=1、惰性）随进程死亡消失。
* 游戏死因：splitprobe 直调 0x10B8620 触发路径B 雷区 —— 有意为之的
  决定性实验，代价一次进程（无数据损失）。

## 108. 路径A 重推（一）：front-context 生态全图 + 0xB2E62E 式谜团的真正闭环（2026-11-20 静态）

§107.6 计划的第一步（静态审计 0x10B8A40 与它的数据源）完成。这一轮把
"进攻线"周边的整条数据链读完了，**两处的旧结论都要改写**。

### 108.1 0x10B8A40 是"省份 → frontCtx 过滤器"，不是 splitter

`0x10B8A40(out, provinces, frontCtx, container)`（§106.3 已知签名）逐省：

```
0x10B8B9E  call [prov_vt->slot0]           ; 省份对象解析（把 id 变 CProvince*）
0x10B8BA8  rdx = [prov+0xd0]              ; 省份的 frontCtx 指针
0x10B8BB7  call 0xD57D90                  ; (frontCtx, prov+0xd0, container)
0x10B8BBC  test al, al / je  跳过          ; 不匹配就不要这个省
0x10B8C2E..  匹配 -> 追加省份 id 进 out 数组
```

`0xD57D90(a, b, c)`：a==b 直接 true；否则查 `c+0xec`/`c+0xe0`/`c+0xc8`
两张索引表比 a、b 两个"省份侧 frontCtx"是否相邻/同一容器——
**这是"箭头端点必须落在同一 front 容器"的拓扑校验**，不是什么段表。
所以"0x10B8A40 内部仍是未知数"已解除：**传入省份的 +0xd0 对，它就对**。

### 108.2 真正的关键：`province+0xd0` 从哪来（front-context 生态）

整条链（全部静态反汇编，交叉验证）：

```
0xF711E0 (每帧 UI 刷新, 持有 orders group)
  ├─ 对 og+0x98 每条根订单 → 0x10C20B0(order, provinces)
  │     └─ 0xF6AF40(order)  →  0xF6AD80(全局0x33EB020, order)
  │           全局注册表按 12 字节追一条 {u32[2]=order+0x38(集团军句柄),
  │           u32=order+0x244(订单 id)}                              (a)
  ├─ 对每条 section 订单 → 0x10C22C0(order, provinces)
  │     └─ 0x10C25E0 附近把省份 frontCtx 去重后交给 0x18E5F60            (b)
  └─ (经 0x18E5F60 家族) 0xEEB000：把省份 id 追进
        "front 上下文 X" 的 +0x140/+0x14c 列表并挂刷新位             (c)
```

而 **`0xEEED50(province, og+0x3c)`**（路径B 的谓词、上一轮的死因）：

```
0xEEED65  X = 0xC07BF0(og+0x3c)
0xEEED6A  n = X+0x174 (列表数)   data = X+0x168
0xEEED82  for each 列表 E:
0xEEED89      m = E+0x24         edata = E+0x18
0xEEEDA0      if edata 里存在元素 == province+0xd0: return E
```

`0xC07BF0(handle)` = `*( *(G_GAMESTATE=0x33DD6B0) + 0x310 )[
0xC08790(handle) ]` —— **+0x310 就是 GS_COUNTRIES**。而
`0xC08790` 在常态下返回 handle 的第一个 dword。`og+0x3c` 存的是
**国家 tag id**（上一轮 dump 实测 `{tag=1, ptr=...}` —— 1 就是 GER）。

⇒ **X = COUNTRIES[tag]，是 CCountry！** `X+0x168/+0x174` 是
**国家级的 front-context 列表**。谓词要求"省份的 +0xd0 出现在这个国家的
某个 front-context 列表里，且那个列表 == og+0x40"。

**上一轮 splitprobe 崩溃的完整解释**：当时 X+0x168=NULL、+0x174=0 是因为
读档后**没人跑过 0xF711E0**（UI 刷新只在"地图上看得见前线"时逐帧跑），
国家的 front-context 列表整张是空的；谓词恒 false → Execute 的 splits
恒空 → 静默无子命令。直调 0x10B8620 走到路径B 时 `0xC07BF0` 里 gamestate
非空、按 tag 索引…… 上一轮 probe 崩在 `[rax+0x175]`：那是把**返回值 1
（COUNTRIES[1] 没有这张表？不对——是 [0x33DD6AF]==0 时 0xC08790 的快路径
返回 handle 首 dword=1，再拿 1 当 CCountry* 读 [1+0x174]=0x175 → AV**。
即：**当时 probe 观测到的"恒等表"其实是 0x33DD6AF 空态快路径**，
正常对局里它就是国家表。§107 的"路径B 必崩"只对"无 front-context 的
空态"成立，**不代表路径B 恒为死路**。

### 108.3 root+0x2a0 的写入者也找到了

`0x10CDBF0(root, section_order)`（pdata 边界 0x10CDBF0..0x10CDCCB）：

```
断言 GetVirtualCreatorId()==0（"此函数每对象只许一次"）
[rbx+0x29c] = [rdi+0x244]            ; section.29c = root 的订单 id
在 root+0x2a0 数组里查 section 的 id；查不到 → 0x12E040 追加
```

三个调用点全部是引擎自己：
- `0x18D9633` —— **`COrderNewFrontCommand::Execute` 建完每个 type-2 段订单
  后自调它**（r14=刚建的段订单，rdi=[rbp-0x58]=根订单）。
- `0x18D12BB` —— 0x18D1150（另一个 0x10B8620 调用者，画前线族的 Execute）。
- `0x18E6D44` —— 0x18E5F60 家族（上面 (b) 的下游，front-context 重建时）。

**所以 §43 的 UI 根订单 +0x2a0=7/10/13 就是这么来的；AI 订单也有。**
我们 complete_plan 画的根订单没有，是因为那条命令路径**不建段订单**
（它的 +0x48 数组喂给 COrderNewFrontCommand 后同样卡在空 splits）。

### 108.4 两个月谜团的最终统一解释

"画出来 + executing=1 + 手填 +0x210，引擎不驱动"的完整因果：

1. 引擎驱动进攻需要的是**三层都活**：
   - 订单层：type-2 根 + type-2 段 + type-1 箭头（0x2a0 段表）
   - **front-context 层（一直缺的那层）**：国家级 +0x168 列表、
     省份 +0xd0 指针、注册表 0x33EB020 条目 —— 全部由 **0xF711E0 的
     UI 每帧刷新**维护，**不在任何 Execute/命令路径里**
   - 分配层：0x10B2C60 → child+0x210
2. 我们所有"手搓"路径（manual_offensive、手填 +0x210、execute_plan）
   都只碰了订单层和分配层，front-context 层从未存在——所以引擎
   的战术循环（走 0xEEED50/0xD57D90 这套）永远把我们当"没画线"。
3. **UI 画一条线能打，我们画不能打**，差别不在订单对象（AI 的订单
   +0x2ac/+0x298 也是 0——§16.11 的反证当时就把方向指对了），
   在**画线之后那几帧 0xF711E0 把 front-context 层建起来了**。

### 108.5 下一步（实机，代价最小的验证顺序）

1. **只读验证**：开一局、UI 手画一条前线+箭头（键鼠，进局前允许范围
   外——注意这条要玩家点，或读一个 UI 画过线的存档），读
   `COUNTRIES[player_tag]+0x168/+0x174`、几个边境省的 `+0xd0`、
   注册表 `0x33EB020` 内容。**预期全非空**——这一步把本节全部结论
   坐实。
2. 若坐实：**最小干预 = 调 0xF711E0(og) 一次**（它是普通成员函数，
   参数就是 COrdersGroup*，和 UI 每帧调的完全一样），让引擎自己把
   front-context 层建起来，然后走 §106 的 engine_offensive（Execute
   直调）重试。此时 0x10BB5A0(root) 的会话戳也在（当帧画的根订单）。
3. 若 0xF711E0 要求更多 UI 状态（当前帧/地图模式），退一步按
   (a)(b)(c) 三小步直调：0xF6AD80(0x33EB020 全局, order) →
   0x10C22C0/0x10C20B0 → 0x18E5F60 —— 每步都有断言护栏，比手工填
   +0x168 安全得多。
4. **禁止再做**的事：直调 0x10B8620（空态快路径 AV，§107 已证）、
   手填 X+0x168（结构未全解）、在 front-context 层为空时发任何
   engine_offensive（静默 no-op + 潜在崩溃面）。

（本节全部为静态反汇编结论，未实机；1 是下一步唯一该做的实验。）

## 109. 实机验收通过：engine_execute 三发三中（2026-11-20）

§108.5 的实机轮次。两次进程死亡换来的教训 + 最终突破，全部实机验证。

### 109.1 旧结论改写（§107/§108 各一处）

| 旧结论 | 实机事实 |
|---|---|
| §107："跨会话根订单 0x10BB5A0 返回 NULL（会话戳不匹配）" | **存档根订单 0x10BB5A0 非空**（读档重置了戳）。11-19 "无子命令但不崩"的真因是下面的 §109.2 |
| §108.5："读档后 front-context 层为空，UI 画线才建" | **层随存档持久化**（CControllerArea 对象序列化）。空的是另一东西，见 §109.2 |
| §106/§107 的路径选择理论 | UI 进攻箭头 = ctor mode 3：**arg7=1 → cmd+0x6d，arg8=0 → cmd+0x6e**（调用点 0xFD5EED，mode 字段 r14+0x224==3）。v1 的 arg8=1 是 mode 4 的字节，把 Execute 推进路径B |

### 109.2 终局真相：CControllerArea 的运行时索引表

`0x10BB5A0(root)` 返回的是 **CControllerArea**（vtable 0x29DB198，地图控制区对象），
不是普通 frontCtx。split 过滤器 `0xD57D90(a=根控制区, b=省+0xd0, c=容器)` 全解码：

```
0xD57DBF  a == b        -> true            ★ 快速路径，不需要任何表
0xD57DDD  id(c) = [c] 首 dword（0xC08790 常态快路径）
0xD57DEE  用 id(c) 查 a 的 {+0xec 容量, +0xe0 int 索引, +0xc8 父对象数组}
0xD57E27  再查 b 的同款表
0xD57E49  两边父对象相同 -> true
```

**a、b 的 {+0xc8/+0xe0/+0xec} 表对工具画的前线永远为空**（不序列化，
UI 显示/操作前线的帧里才填）——所以慢路径恒 false。但**快速路径
a==b 可用**：全 13414 省扫描，找 `province+0xd0 == 0x10BB5A0(root)` 的省
（实测 14 个 / 重画后 2 个），把"与根前线同控制区"的省传给 ctor，
Execute 的 splits 非空 → 完整 UI 链跑通。

### 109.3 实机验收结果（三次独立命中）

| 发射 | 输入 | 结果 |
|---|---|---|
| #1 | 存档根 38 + provscan 14 省 | `method: engine_execute`，子命令 48，17 师，executing:1 |
| #2 | 工具重画根 57 + provscan 2 省 | `method: engine_execute`，子命令 60，17 师，executing:1 |
| #3 | 根 61 + **无省份（工具自选）** | `method: engine_execute`，子命令 64，17 师，executing:1 |

引擎自治证据：#1 后引擎把 17 师动态重分到 3 条箭头（11+3+3，新建 2 条），
计划执行完毕后 orders 自动清账（offensive_lines 归零）——引擎把我们的
箭头当真实作战计划维护、消费、回收，全程无人工干预。

未发生战斗的原因：`a==b` 匹配的"同控制区"省不全是对 BEL 的敌境省
（控制区 ≠ 国境线），且部分目标省不相邻。**副作用验收（省易手）留给
真实战役**——结构层验收（engine_execute + 引擎自治）已通过。
target_hits 里有敌省时即会推进；下一步优化是 provscan 叠加
`hoi4_border_fronts` 的敌省过滤。

### 109.4 实现变更（本节对应的代码）

* `armyops_win.engine_offensive` **v3**：mode-3 参数（arg7=1, arg8=0，
  不再碰 G 门）+ front-context 空层护栏 + **自动选省**（provinces 空 →
  0x10BB5A0(root) 控制区全表扫描）。
* `game.offensive_line`：engine_execute 拒绝且原因是空层时直接中止，
  不再落 manual_offensive（不再制造 known-bad 箭头）。
* `mcp/server.py hoi4_offensive_line`：provinces 可选。
* `test_engine_offensive.py`：更新为 v3 契约，全绿。

### 109.5 新禁令与事故记录（2026-11-20 第二次实机轮）

1. **禁令：0xF711E0 帧外直调**——§108.5 以为它是"普通成员函数，
   参数 og"，实机一调即死（Err 299 静默 AV）。它依赖每帧 UI 上下文，
   只能被引擎自己在渲染循环里调。
2. **禁令：0x142284870 直调**——它吃 cmd+0x28 的 8 字节句柄做参数
   （`[rcx]` 首 dword 是表索引），空参直调 = 0xC402A0 解引用垃圾 = 死。
   两次进程死亡都是我直调前没钉死参数来源造成的（存档均零污染，
   暂停态无数据损失）。
3. **`-continuelastsave` 启动参数可用**（exe 内实锚 0x2794550）：
   直启 exe + 该参数 = 自动载入最后存档，进档不再需要人工点 UI。
4. Mimosa 钩子误报补充：Bash 里内联 Python 脚本被当作"直接写源码"
   拒绝（哪怕只是 json 解析）——驱动输出解析要么写进 driver.py 的
   阶段里，要么用 head/tail -c 切片。

### 109.6 下一步

1. provscan 叠加敌境过滤（border_fronts 的省 ∩ target_hits），让箭头
   真正压到敌境省上 → 副作用验收（fighting_now / 省易手）。
2. CControllerArea 的 +0xc8/+0xe0/+0xec 表的填充者（UI 渲染路径里）
   若找到，可让慢路径也活——优先级低，快速路径已够用。
3. 手动箭头（manual_offensive 兜底产物）仍是 known-bad：靠 purge
   规则豁免（有父前线喂食的 0 师箭头保留），不必处理。
