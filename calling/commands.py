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
import os

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
    # 2026-11-26 (P1-3): 单条训练线移除命令（Windows PE 1.19.5.0 经反汇编实证：
    # vt=0x2AC08D0, ctor=0x1C2F020, size=0x30, +0x28 接收单条 line 的 {type=63,id} 句柄）。
    "RemoveConveyorLine": dict(vt=0x4462C00, size=0x30, type=0x3522,
        fields=[(0x24, "<Q", "line_handle")]),
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


_CALLING_DIR = os.path.dirname(os.path.abspath(__file__))


class CommandLayer:
    def __init__(self, game):
        self.g = game
        self.p = game.p
        self._queue = None

    # ---------------------------------------------------------------- basics
    def new(self, size: int) -> int:
        """从游戏内置 CPdxNewDeleteAllocator 分配命令内存（Windows PE 1.19.5.0）。"""
        import cmdwin
        return cmdwin.alloc(self.g, int(size))

    def post(self, cmd_ptr: int, force: int = 0):
        """将已构造的 CCommand 入队（Windows PE 统一走 cmdwin.enqueue）。"""
        inv = getattr(self.g, "invalidate_read_scope", None)
        if callable(inv):
            inv()
        import cmdwin
        rv = cmdwin.enqueue(self.g, cmd_ptr)
        self.last_error = ""
        return rv

    # ---------------------------------------------- prebuilt command catalogues
    def catalog(self):
        """读取预构建的命令字段表（calling/cmdfields.json）。"""
        if getattr(self, "_auto", None) is None:
            import safe_cache
            cache_p = os.path.join(_CALLING_DIR, "cmdfields.pkl")
            self._auto = safe_cache.load(cache_p) if safe_cache.exists(cache_p) else {}
        return self._auto

    def ctor_catalog(self):
        """读取预构建的命令构造函数表（calling/cmdctors.json）。"""
        if getattr(self, "_ctors", None) is None:
            import safe_cache
            cache_p = os.path.join(_CALLING_DIR, "cmdctors.pkl")
            self._ctors = safe_cache.load(cache_p) if safe_cache.exists(cache_p) else {}
        return self._ctors

    def spec_for(self, name: str):
        """返回静态命令字段描述（优先已校验表，其次预构建 JSON 目录）。"""
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
        """在 Windows PE 白名单门禁下构造并投递任意已验证 CCommand。"""
        import cmdwin
        if not cmdwin.win_safe(name):
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

    # ------------------------------------------------------- generic builder
    def make(self, name: str, **fields):
        """经由 pecmd/cmdwin 构造 Windows PE 命令对象。"""
        import cmdwin
        return cmdwin.build_command(self.g, name, fields)

    def send(self, name: str, force=0, **fields):
        """构造命令并入队。"""
        ptr = self.make(name, **fields)
        self.post(ptr, force)
        return ptr

    # ---------------------------------------------------------- convenience
    def set_national_focus(self, focus_ptr: int, tag_id: int = None):
        """为本国或指定国家启动国策。"""
        import cmdwin
        tid = self.g.player_tag_id() if tag_id is None else int(tag_id)
        return cmdwin.set_national_focus(self.g, focus_ptr, tid)

