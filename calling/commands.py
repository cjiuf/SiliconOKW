"""
The player command layer.

Everything you click in the game is built as a CCommand object and pushed through
CCommandQueue::Post, that is how the deterministic multiplayer architecture
works. This module uses the same path: not a cheat, the player's own action channel.

    queue = CInGameIdler::GetCommandQueue()      (vtable +0x90)
    cmd   = operator new(size); Ctor(cmd, ...)
    Post(queue, cmd, 0)                          img+0x2DC1C40
"""
from __future__ import annotations
import struct
import sys

IDLER_GLOBAL   = 0x459E908
IDLER_VT_QUEUE = 0x90     # CInGameIdler::GetCommandQueue()
IDLER_VT_TAG   = 0xC0     # CInGameIdler::GetPlayerTag() -> CCountryTag*
FN_POST        = 0x2F212D0   # CCommandQueue::Post(queue, cmd, int flag) — VERIFIED 2026-11-08: it is the exact call CCommand::Run makes at 0x20AE1A2 after CanExecute. force=1 skips the predicate (needed for CMoveCommand, whose vtable+0x50 is a heavy move routine).
FN_RUN         = 0x20AE0A0   # CCommand::Run(cmd, std::string* err): CanExecute->Post
FN_NEW         = 0x510950

# Every CCommand constructor does the same base initialisation:
#   [+0x08]=0(byte)  [+0x0c]=-1 [+0x10]=-1  [+0x14]=0(u16) [+0x16]=0xffff
#   [+0x18]=0(u16)   [+0x1c]=0 [+0x20]=0    [+0x00]=vptr
# which is why a command can be built without calling its constructor.
BASE_INIT = [
    # observed in CSetNationalFocusCommand ctor @0x1cf6ed0 (1.19.4.1)
    (0x08, "<B", 0), (0x0C, "<i", -1), (0x10, "<q", 0),
    (0x18, "<H", 0xFFFF), (0x1A, "<q", 0), (0x22, "<H", 0),
]

# name -> (vtable ELF offset, object size, type id, fields)
# fields: [(offset, struct format, meaning)]
COMMANDS = {
    # ---- national focus ----
    # 2026-11-10 (review WO-005): ctor recovered by cmdctors.pkl; CommandLayer
    # .set_national_focus() does spec["ctor"] and used to raise KeyError.
    "SetNationalFocus":   dict(vt=0x4416DD8, size=0x30, type=0x33BD, ctor=0x1CF6ED0,
        fields=[(0x24, "<i", "tag"), (0x28, "<Q", "focus_ptr")]),
    "DropCurrentNationalFocus": dict(vt=0x4416EA0, size=0x28, type=0x37B1,
        fields=[(0x24, "<i", "tag")]),
    "BypassNationalFocus": dict(vt=0x4416F68, size=0x30, type=0x37AA,
        fields=[(0x24, "<i", "tag"), (0x28, "<Q", "focus_ptr")]),
    "SetContinuousFocus": dict(vt=0x4417030, size=0x30, type=0x36E0,
        fields=[(0x24, "<i", "tag"), (0x28, "<Q", "focus_ptr")]),
    "DropContinuousFocus": dict(vt=0x44170F8, size=0x28, type=0x36E2,
        fields=[(0x24, "<i", "tag")]),
    # ---- research ----
    "SetResearch":        dict(vt=0x44154D8, size=0x38, type=0x2F70,
        fields=[(0x24, "<i", "tag"), (0x28, "<Q", "tech_ptr"),
                (0x30, "<i", "slot"), (0x34, "<B", "flag")]),
    # ---- decisions ----
    "SelectDecision":     dict(vt=0x4417288, size=0x30, type=0x3809,
        fields=[(0x24, "<i", "tag"), (0x28, "<Q", "decision_ptr")]),
    # ---- ideas, laws, advisors ----
    "AddIdea":            dict(vt=0x44158C0, size=0x38, type=0x300F,
        fields=[(0x24, "<i", "tag"), (0x28, "<Q", "idea_ptr"), (0x30, "<B", "flag")]),
    "RemoveIdea":         dict(vt=0x4415BE0, size=0x38, type=0x3010,
        fields=[(0x24, "<i", "tag"), (0x28, "<Q", "idea_ptr"), (0x30, "<B", "flag")]),
    # CReplaceIdeaCommand ctor @0x1CED0E0 (1.19.4.1): (cmd, tag*, old CIdea*, new CIdea*)
    # vtable +0x50 = CanExecute @0x1CED690, +0x58 = Execute @0x1CED150.
    # Both pointers must be country idea instances from PL_ACTIVE_IDEAS / PL_ALL_IDEAS,
    # and both must share the same category object at CIdea+0xA78.
    "CReplaceIdea":       dict(vt=0x4415D70, size=0x38, type=0x32D9,
        fields=[(0x24, "<i", "tag"), (0x28, "<Q", "old_ptr"), (0x30, "<Q", "new_ptr")]),
    # ---- construction ----
    "AddConstruction":    dict(vt=0x4414AB0, size=0x48, type=0x2F77,
        # CBuildingReference @+0x28: +0x08 = province (0xFFFFFFFF for a state-level
        # building), +0x0C = building type id, +0x10 = state id. The ctor copies the
        # 8 bytes at ref+8 into +0x30 and ref+0x10 into +0x38 (0x1CDEAA0).
        fields=[(0x24, "<i", "tag"), (0x28, "<Q", "ref_vptr"), (0x30, "<i", "province"),
                (0x34, "<i", "building_type"), (0x38, "<i", "state"),
                (0x40, "<i", "level"), (0x44, "<B", "flag")]),
    "RemoveConstruction": dict(vt=0x4414B78, size=0x30, type=0x3320,
        fields=[(0x24, "<i", "tag"), (0x2C, "<i", "index")]),
    # ---- production ----
    # 2026-11-10 (review WO-010): 0x4268BE8 was the 1.19.2 vtable; the real class
    # in cmdfields.pkl is CSetProductionLineAmountToProduceCommand
    # (vt 0x4415410, size 0x30, ctor 0x1CE41D0).
    "SetProductionLineAmount": dict(vt=0x4415410, size=0x30, type=0x2FE1, ctor=0x1CE41D0,
        fields=[(0x24, "<i", "tag"), (0x28, "<i", "line"), (0x2C, "<i", "amount")]),
    "AddProductionLineFactories": dict(vt=0x4415348, size=0x30, type=0x2F75,
        fields=[(0x24, "<Q", "line_id"), (0x2C, "<i", "amount")]),
    "RemoveProductionLine": dict(vt=0x4413EF8, size=0x30, type=0x3B83,
        fields=[(0x24, "<i", "tag"), (0x2C, "<i", "line")]),
    # Real ctor takes ONE handle at 0x24 (cmdctors: size 8, kind handle). The
    # tag+index entry above is a stale 1.19.2 shape and overflows struct.pack.
    "RemoveProductionLineByHandle": dict(vt=0x4413EF8, size=0x30, type=0x3B83,
        fields=[(0x24, "<Q", "line_id")]),
    # CSetProductionLineCommand ctor 0x1BE8DB0: (cmd, line handle, variant handle).
    # vtable from RTTI N25CSetProductionLineCommandE = 0x4413E30 (the commands.txt
    # column was a stale 1.19.2 table and pointed into gcc_except_table).
    "SetProductionLine": dict(vt=0x4413E30, size=0x38, type=0x2F6E,
        fields=[(0x24, "<Q", "line_id"), (0x2C, "<Q", "variant_id")]),
    # CAddProductionLineCommand ctor 0x1BE4EF0: (cmd, CCountryTag*, handle, handle)
    # vtable 0x4413A48. The two handles are still unverified; do not use blindly.
    "AddProductionLine": dict(vt=0x4413A48, size=0x40, type=0x332D,
        fields=[(0x24, "<i", "tag"), (0x28, "<Q", "arg0"), (0x30, "<Q", "arg1")]),
    # NDoctrines::CUnlockGrandDoctrineCommand, vtable 0x4360C28, ctor
    # 0xB231B0: (cmd, CCountryTag* rsi, CGrandDoctrineTemplate* rdx).
    # So +0x28 = template ptr, +0x30 = country tag id (NOT an int argument).
    "UnlockGrandDoctrine": dict(vt=0x4360C28, size=0x38, type=0x0,
        fields=[(0x28, "<Q", "doctrine_ptr"), (0x30, "<i", "tag")]),
    # CCreateDivisionTemplateCommand vt 0x4457F40, size 0x290.
    # Real ctor 0x1FDC190 (disassembled 2026-11-02):
    #   0x1FDC190(cmd, rsi=src CDivisionTemplate*, rdx=0, rcx=&tag_i32, r8=0)
    #   - 0x761410 DEEP-COPIES the source CDivisionTemplate into cmd+0x28 (embedded);
    #     it reads src+0x08/+0x10 = the template's name CString, then arrays, then
    #     0x763000 keyed appends, and only ever dereferences rsi and rcx.
    #   - [rcx] (i32 tag) -> cmd+0x270; then +0x274 = globals; +0x280 = rdx; +0x288 = [rcx].
    #   - A NULL rsi was the previous SIGSEGV cause (rsi is dereferenced immediately).
    #   - The 3 real UI call sites always pass: rsi = an existing CDivisionTemplate*
    #     (embedded at CReferencedDivisionTemplate+0x18), rdx = 0, rcx = &int(tag), r8 = 0.
    # BUILD ONLY via build_division_template_from(); plain make() cannot deep-copy.
    "CreateDivisionTemplateFrom": dict(vt=0x4457F40, size=0x290, ctor=0x1FDC190,
        type=0x2FA5),
    # ---- division recruitment / training conveyors -------------------------
    # All four are flat 0x30-byte structs: a {type,id} handle at +0x24 and one
    # int32 at +0x2c (except Deploy, which takes only the handle). Verified by
    # disassembling each ctor in 1.19.4.1:
    #   CCreateConveyorCommand        vt=0x4462348 ctor 0x2204c50/+0x2204ca0
    #   CAddConveyorLineCommand       vt=0x4462988 ctor 0x2207360
    #   CSetConveyorLocationCommand   vt=0x44625A0 ctor 0x2205ac0
    #   CSetConveyorPriorityCommand   vt=0x44627F8 ctor 0x2206d90 (default priority 1)
    #   CDeployConveyorCommand        vt=0x4462A50 ctor 0x22076f0
    # (addresses re-checked against the 1.19.4.1 ELF: the class list in rtti_index.csv
    #  gives 27CSetConveyorPriorityCommand -> 0x44627f8; the ctor writes the {type,id}
    #  handle at +0x24 and arg2 (esi/edx) at +0x2c, exactly like Location.)
    "CreateConveyor":     dict(vt=0x4462348, size=0x30, type=0x34A7,
        fields=[(0x24, "<Q", "template_handle"), (0x2C, "<i", "priority")]),
    # The one the "Train" dialog actually uses: it carries the deployment STATE, so
    # the conveyor is born with a valid location (no "No location set").
    #   ctor 0x2205130 (default) / 0x2205180 (this, handle*, edx, ecx, r8d, r9d)
    #     +0x2c = r9d   +0x30 = r8d   +0x34 = edx   +0x38 = ecx
    #   Execute 0x2205360: [+0x2c] > 0, [+0x30] > 0 and 0 <= [+0x34] < state_count,
    #   then CDeploymentStatus::Create(..., edx=[+0x30])  -> +0x34 is the state id.
    "CreateConveyorExtended": dict(vt=0x4462410, size=0x40, type=0x34B5,
        fields=[(0x24, "<Q", "template_handle"), (0x2C, "<i", "priority"),
                (0x30, "<i", "lines"), (0x34, "<i", "state_id"),
                (0x38, "<i", "flag")]),
    "AddConveyorLine":    dict(vt=0x4462988, size=0x30, type=0x34AC,
        fields=[(0x24, "<Q", "conveyor_handle"), (0x2C, "<i", "count")]),
    "SetConveyorLocation": dict(vt=0x44625A0, size=0x30, type=0x34A9,
        fields=[(0x24, "<Q", "conveyor_handle"), (0x2C, "<i", "province_id")]),
    "SetConveyorPriority": dict(vt=0x44627F8, size=0x30, type=0x34AA,
        fields=[(0x24, "<Q", "conveyor_handle"), (0x2C, "<i", "priority")]),
    "SetConveyorSeries":  dict(vt=0x44628C0, size=0x30, type=0x34AB,
        fields=[(0x24, "<Q", "conveyor_handle"), (0x2C, "<i", "series")]),
    "DeployConveyor":     dict(vt=0x4462A50, size=0x30, type=0x3520,
        fields=[(0x24, "<Q", "conveyor_handle")]),
    # Deleting a dead conveyor: same flat 0x30 layout, handle at +0x24.
    # rtti_index.csv: 22CRemoveConveyorCommand -> 0x4462CA8; ctor 0x2208020 writes
    # only +0x24 (the {type,id} handle).
    "RemoveConveyor":     dict(vt=0x4462CA8, size=0x30, type=0x3521,
        fields=[(0x24, "<Q", "conveyor_handle")]),
    # ---- game speed ----
    "SetGameSpeed":       dict(vt=0x44756C0, size=0x28, type=0x2F42,
        fields=[(0x24, "<i", "speed")]),
}


class CommandLayer:
    def __init__(self, game):
        self.g = game
        self.p = game.p
        self._queue = None

    # ---------------------------------------------------------------- basics
    def idler(self) -> int:
        v = self.p.u64(self.g.va(IDLER_GLOBAL))
        if not v:
            raise RuntimeError("no CInGameIdler (not inside a game)")
        return v

    def queue(self, refresh=False) -> int:
        if self._queue and not refresh:
            return self._queue
        idl = self.idler()
        fn = self.p.u64(self.p.u64(idl) + IDLER_VT_QUEUE)
        self._queue = self.g.call(fn, (idl,), absolute=True)
        return self._queue

    def player_tag_ptr(self) -> int:
        idl = self.idler()
        fn = self.p.u64(self.p.u64(idl) + IDLER_VT_TAG)
        return self.g.call(fn, (idl,), absolute=True)

    def new(self, size: int) -> int:
        # FN_NEW is a Linux VA. On the PE build this jumps into unrelated code
        # and kills the process (measured 2026-11-14); the Windows path allocates
        # from the game's own CPdxNewDeleteAllocator singleton instead, which is
        # exactly what cmdwin does for the commands it builds by hand.
        if sys.platform == "win32":
            import cmdwin
            return cmdwin.alloc(self.g, int(size))
        return self.g.call(FN_NEW, (size,))

    def _alloc_stdstring(self) -> int:
        """A valid EMPTY libstdc++ std::string on the game's heap.

        Layout (32 bytes): +0 _M_p, +8 _M_string_length, +16 _M_local_buf[16].
        `_M_p` MUST point at the object's own SSO buffer. The old code zeroed all
        32 bytes, so _M_p was NULL; when CCommand::Run wrote an error message into
        it, libstdc++ either dereferenced NULL or freed a wild pointer. That is the
        "command occasionally refused / crashed" bug in the law and focus paths.
        """
        s = self.g.malloc(0x20)
        self.p.write(s, struct.pack("<QQ", s + 0x10, 0) + b"\0" * 16)
        return s

    def _read_stdstring(self, s: int) -> str:
        txt = ""
        try:
            ptr = self.p.u64(s)
            ln = self.p.u64(s + 8)
            if ptr and 0 < ln < 512:
                raw = self.p.try_read(ptr, ln) or b""
                txt = raw.decode("utf-8", "replace")
        except Exception:
            pass
        return txt

    def _free_stdstring(self, s: int):
        """Free the string's heap buffer (if it grew past SSO), then the string."""
        try:
            ptr = self.p.u64(s)
            if ptr and ptr != s + 0x10:
                self.g.free(ptr)
            self.g.free(s)
        except Exception:
            pass

    def post(self, cmd_ptr: int, force: int = 0):
        """Put a command on the queue, the way the game does it.

        CCommandQueue::Post used to be called directly here. Its address moved in
        1.19.4.1 and the stale one is refused by the injector, which broke
        hoi4_send_command / hoi4_declare_war. CCommand::Run (0x20AE0A0, verified)
        does CanExecute -> Post internally, so calling Run is both correct and
        the same path a player's click takes.
        """
        if sys.platform == "win32":
            # PE: CCommand::Run does not exist at this address, and Post's drop
            # paths call the command's deleting destructor.  `enqueue` stamps the
            # session tick/seq and hands the object straight to the queue, the
            # same route every working cmdwin command takes.
            import cmdwin
            rv = cmdwin.enqueue(self.g, cmd_ptr)
            self.last_error = ""
            return rv
        if force:
            # Forced posts bypass CanExecute; Run would refuse them.
            return self.g.call(FN_POST, (self.queue(), cmd_ptr, force))
        errbuf = self._alloc_stdstring()
        try:
            rv = self.g.call(FN_RUN, (cmd_ptr, errbuf))
            self.last_error = self._read_stdstring(errbuf)
        finally:
            self._free_stdstring(errbuf)
        return rv

    # ---------------------------------------------- automatic command catalogue
    def catalog(self):
        """Field layout of all 370 commands, extracted automatically from the binary."""
        if getattr(self, "_auto", None) is None:
            import cmdauto
            self._auto = cmdauto.build()
        return self._auto

    def ctor_catalog(self):
        """Field layouts recovered from parameterised constructors (more complete)."""
        if getattr(self, "_ctors", None) is None:
            import cmdctor
            self._ctors = cmdctor.build()
        return self._ctors

    def spec_for(self, name: str):
        """Return the hand-verified spec if there is one, otherwise the extracted one.

        Names arrive both as the C++ class ("CAddProductionLineFactoriesCommand") and
        as the short key ("AddProductionLineFactories"). The auto catalog in
        cmdsigs.pkl is 1.19.2 data, so a C-prefixed name that matches a verified
        short key MUST use the verified spec, otherwise a stale vtable is posted and
        the game faults inside CCommandQueue::Post.
        """
        if name in COMMANDS:
            return COMMANDS[name], "verified"
        if len(name) > 8 and name.startswith("C") and name.endswith("Command"):
            short = name[1:-7]
            if short in COMMANDS:
                return COMMANDS[short], "verified(alias)"
        auto = self.catalog()
        key = name if name in auto else ("C" + name + "Command")
        if key not in auto:
            for k in auto:
                if k.lower() == ("c" + name + "command").lower() or k.lower() == name.lower():
                    key = k
                    break
        if key not in auto:
            raise KeyError("unknown command: %s" % name)
        a = auto[key]
        ctors = self.ctor_catalog().get(key)
        fields = []
        if ctors and ctors.get("fields"):
            # constructor-derived layout (more reliable): offset, size, arg, kind
            FMT = {8: "<q", 4: "<i", 2: "<h", 1: "<B"}
            i = 0
            for f in ctors["fields"]:
                off, sz, kind = f["offset"], f["size"], f["kind"]
                if off == 0x24 and kind in ("deref",) and sz == 4:
                    nm = "tag"
                else:
                    nm = "arg%d" % i
                    i += 1
                fields.append((off, FMT.get(sz, "<q"), nm))
        else:
            for off, kind, _n in a["fields"]:
                fmt = {"tag": "<i", "int": "<i", "bool": "<B", "ptr": "<Q", "obj": "<Q"}[kind]
                fields.append((off, fmt, ("tag" if kind == "tag" and off == 0x24
                                          else "f%#x" % off)))
        _sz = a["size"] or 0x80
        if _sz < 0x30:
            _sz = 0x80
        return dict(vt=a["vtable"], size=_sz, type=a["type_id"],
                    fields=fields, auto=True, name=key), "auto"

    def send_raw(self, name: str, fields: dict, force=0):
        """Build ANY CCommand and post it to the queue.
        fields: {"tag":1, "0x28": <value>, ...}  (offset keys may be hex strings)"""
        if sys.platform == "win32":
            import cmdwin
            if not cmdwin.win_safe(name):
                # 2026-11-15: three separate commands built from pecmd data that was
                # never verified took hoi4.exe down (CAddAdvisorCommand's guessed
                # ctor, CGenerateWarGoalAction, CUnlockGrandDoctrineCommand).  A wrong
                # vtable/size/ctor produces an object the engine executes as code, so
                # "post it and see" is not a recoverable experiment.
                return {"command": name, "ptr": None, "run_result": 0,
                        "blocked": True,
                        "error": "%s is not on the windows verified-command list. "
                                 "Its pecmd layout/vtable has never been confirmed "
                                 "against the PE build and posting it has killed the "
                                 "game before. Add it to cmdwin.WIN_SAFE only after "
                                 "the layout is proven." % name}
            named = {k: v for k, v in fields.items()
                     if not (isinstance(k, str) and (k.startswith("0x") or k.isdigit()))}
            offs = {k: v for k, v in fields.items() if k not in named}
            if named or not offs:
                cmd = cmdwin.build_command(self.g, name, named)
            else:
                spec = cmdwin.spec_of(name)
                blob_fields = []
                for k, v in offs.items():
                    off = int(str(k), 16) if str(k).startswith("0x") else int(k)
                    hit = [f for f in spec["fields"] if f["offset"] == off]
                    sz = hit[0]["size"] if hit else 8
                    blob_fields.append((off, {8: "<Q", 4: "<i", 2: "<h",
                                              1: "<B"}[sz], int(v)))
                cmd = cmdwin.build_by_vt(self.g, spec["vt"], spec["size"], blob_fields)
            rv = self.post(cmd, force)
            return {"command": name, "ptr": hex(cmd), "run_result": rv & 0xFF,
                    "error": getattr(self, "last_error", "")}
        spec, src = self.spec_for(name)
        size = spec["size"] or 0x80
        ptr = self.new(size)
        # operator new hands back uninitialised heap. Reading it back and only
        # overwriting the known fields left padding full of whatever the previous
        # owner wrote there, which the game's CanExecute/Execute then acted on.
        blob = bytearray(size)
        struct.pack_into("<Q", blob, 0, self.g.va(spec["vt"]))
        for off, fmt, val in BASE_INIT:
            struct.pack_into(fmt, blob, off, val)
        known = {f[2]: f for f in spec["fields"]}
        byoff = {f[0]: f for f in spec["fields"]}
        # arg0, arg1 ... map to the non-tag fields, in order
        ordered = [f for f in spec["fields"] if f[2] != "tag"]
        for i, f in enumerate(ordered):
            known.setdefault("arg%d" % i, f)
        for k, v in fields.items():
            if k in known:
                off, fmt, _ = known[k]
            else:
                ks = str(k)
                fmt_override = None
                if "@" in ks:                      # "i32@0x24", "i64@0x28", "u8@0x30"
                    tname, _, ks = ks.partition("@")
                    fmt_override = {"i8": "<b", "u8": "<B", "i16": "<h", "u16": "<H",
                                    "i32": "<i", "u32": "<I", "i64": "<q",
                                    "u64": "<Q", "ptr": "<Q"}.get(tname)
                try:
                    off = int(ks, 16) if ks.startswith("0x") else int(ks)
                except (TypeError, ValueError):
                    raise KeyError("command %s has no field '%s'. Valid: %s"
                                   % (name, k, sorted(known)))
                fmt = fmt_override or byoff.get(off, (off, "<i", ""))[1]
            if off + struct.calcsize(fmt) > size:
                raise ValueError("offset %#x exceeds the command size (%#x)" % (off, size))
            struct.pack_into(fmt, blob, off, int(v))
        self.p.write(ptr, bytes(blob))
        rv = self.post(ptr, force)
        out = {"command": spec.get("name", name), "source": src, "ptr": hex(ptr),
               "size": size, "fields": {k: hex(int(v)) for k, v in fields.items()}}
        if isinstance(rv, int):
            out["run_result"] = rv & 0xFF
            out["error"] = getattr(self, "last_error", "")
        return out

    # ------------------------------------------------------- generic builder
    def make(self, name: str, **fields):
        """Build a command object directly, without calling its constructor."""
        if sys.platform == "win32":
            # PE build: the layout (and often the constructor itself) comes from
            # `pecmd`, recovered out of MSVC RTTI + the ctor's own stores.
            import cmdwin
            return cmdwin.build_command(self.g, name, fields)
        spec = COMMANDS[name]
        ptr = self.new(spec["size"])
        blob = bytearray(spec["size"])   # all-zero: no stale heap in padding fields
        struct.pack_into("<Q", blob, 0, self.g.va(spec["vt"]))
        for off, fmt, val in BASE_INIT:
            struct.pack_into(fmt, blob, off, val)
        known = {f[2]: f for f in spec["fields"]}
        for k, v in fields.items():
            if k not in known:
                raise KeyError("command %s has no field '%s' (%s)" % (name, k, list(known)))
            off, fmt, _ = known[k]
            struct.pack_into(fmt, blob, off, v)
        self.p.write(ptr, bytes(blob))
        return ptr

    def send(self, name: str, force=0, **fields):
        """Build a command and post it. The same path as a player's click."""
        ptr = self.make(name, **fields)
        self.post(ptr, force)
        return ptr

    # ------------------------------------------------------- constructor-based
    def build(self, name: str, *ctor_args, scratch_setup=None):
        """Build the command by calling its constructor, and return the pointer."""
        spec = COMMANDS[name]
        cmd = self.new(spec["size"])
        args = (cmd,) + tuple(ctor_args)
        import api
        api.allow_call(spec["ctor"])   # the constructor cmdctor recovered statically
        self.g.call(spec["ctor"], args, scratch_writer=scratch_setup)
        return cmd

    def issue(self, name: str, *ctor_args, scratch_setup=None, force=0):
        cmd = self.build(name, *ctor_args, scratch_setup=scratch_setup)
        self.post(cmd, force)
        return cmd

    # ---------------------------------------------------------- convenience
    def set_national_focus(self, focus_ptr: int, tag_id: int = None):
        """Select a national focus for the player, or for the given tag."""
        if sys.platform == "win32":
            # The PE build keeps the same command objects but different addresses;
            # cmdwin builds through the real constructor and posts through the
            # session's Post (see calling/cmdwin.py).
            import cmdwin
            tid = self.g.player_tag_id() if tag_id is None else int(tag_id)
            return cmdwin.set_national_focus(self.g, focus_ptr, tid)
        if tag_id is None:
            tagp = self.player_tag_ptr()
        else:
            tagp = None
        spec = COMMANDS["SetNationalFocus"]
        cmd = self.new(spec["size"])
        holder = {}

        def setup(scr):
            holder["scr"] = scr
            if tagp is None:
                self.p.write(scr + 0x800, struct.pack("<i", tag_id))
                return (cmd, scr + 0x800, focus_ptr)
            return (cmd, tagp, focus_ptr)

        import api
        api.allow_call(spec["ctor"])
        self.g.call(spec["ctor"], scratch_writer=setup)
        self.post(cmd)
        return cmd

