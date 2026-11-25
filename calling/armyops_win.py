"""Army orders on the Windows build (1.19.5.0), by calling the game's own routines.

Everything here was read out of the binary, not copied from the Linux table:

  * `.?AVCOrdersGroup@@` has two vtables, 0x29CEE58 / 0x29CEED0.  The two
    functions that install them (0x00C3F480, 0x00C3F800) are its constructor and
    copy constructor; COrderGroupCommand::Execute resolves handles and calls
    0x00F702D0, which `new`s 0x230 bytes and runs 0x00C3F480 -- i.e. 0x00F702D0
    is "make one COrdersGroup under this theatre".

  * 0x00C417A0 is what COrderGroupCommand::Execute actually runs for the
    "divisions -> new army" case:

        0x1418d65d4  movzx r9d, byte ptr [rbx + 0x28]   ; flag
        0x1418d65d9  lea r8, [rcx + 0x30]               ; CPdxArray<handle>*
        0x1418d65dd  lea rdx, [rcx + 0x48]              ; CCountryTag* (theatre)
        0x1418d65e1  add rcx, 0x50                      ; CCountryTag* (parent, may be 0)
        0x1418d65e5  call 0x140c417a0

    so its signature is (out_parent_handle, theatre_handle, divisions, flag).
    Inside, it resolves the theatre, calls 0x00F702D0(theatre,0,0,0,0), stores
    the flag at +0x198, then walks the array, resolves each 8-byte handle and
    attaches the division (the resolved pointer is a CArmy sub-object; the base
    is `ptr - 0x10`).

Calling 0x00C417A0 directly is what the command does on its accepted path, so
no command object, no queue and no game tick are needed -- which matters,
because a paused game never drains the command queue.

Verified live 2026-11-14 on PID 32884: 4 German divisions in, one COrdersGroup
back (handle {53,505}, +0x39 = 0 => army, +0x40 = the theatre, +0x50 = the four
divisions, +0x1B8 = 0 => no parent group), and CArmy+0xC0 on all four pointed at
it.  The COrdersGroup field offsets below are the Linux ones and every one of
them still holds on Windows.
"""
import struct
import sys
import time

import cmdwin
import offsets_win as _OW

# --- the routines -----------------------------------------------------------
FN_CREATE_ARMY = _OW.ORDER_FN_CREATE_ARMY   # (handle* out_parent, handle* theatre, CPdxArray<handle>*, u8) -> COrdersGroup*
FN_NEW_OG      = _OW.ORDER_FN_NEW_OG        # (theatre,0,0,0,parent) -> COrdersGroup* -- one group, no divisions
FN_RESOLVE     = _OW.ORDER_FN_RESOLVE       # handle* -> object*

# --- fields (Linux table, all re-verified on 1.19.5.0) ----------------------
CO_THEATRES = _OW.CO_THEATRES               # CCountry -> CPdxArray<CTheatre*>
CARMY_VT = _OW.ORDER_CARMY_VT               # CArmy vtable RVA (1.19.5.0, 1297/1297 divisions)
CARMY_HANDLE = _OW.ORDER_CARMY_HANDLE       # CArmy -> {type, id} (开局师 type=51, 新训师 type=4713, 切勿白名单过滤 type)
CARMY_OG = _OW.ORDER_CARMY_OG               # CArmy -> COrdersGroup*

try:
    import profile as _aw_prof
    if _aw_prof.load():
        CARMY_VT = _aw_prof.anchor("VT_ARMY", CARMY_VT)
except Exception as _e:
    import diag as _diag
    _diag.warn("armyops_win.apply_profile.carmy", _e)

OG_HANDLE      = _OW.ORDER_OG_HANDLE
OG_IS_GROUP    = _OW.ORDER_OG_IS_GROUP
OG_THEATRE     = _OW.ORDER_OG_THEATRE
OG_DIVISIONS   = _OW.ORDER_OG_DIVISIONS     # CPdxArray<CArmy*>
OG_LEADER      = _OW.ORDER_OG_LEADER
OG_ORDERS      = _OW.ORDER_OG_ORDERS        # CPdxArray<COrderInstance*>
OG_NAME        = _OW.ORDER_OG_NAME          # std::string
OG_PARENT      = _OW.ORDER_OG_PARENT
OG_MEMBERS     = _OW.ORDER_OG_MEMBERS

G_DEFAULT_ALLOC = _OW.G_DEFAULT_ALLOC       # the game's CPdxNewDeleteAllocator instance
HANDLE_ORDER_GROUP = 53


def theatre(g, country=None):
    """The country's first CTheatre* (every country has exactly one in 1936)."""
    p = g.p
    co = country if country is not None else g.player()
    if not isinstance(co, int):
        co = co.ptr
    arr = p.u64(co + CO_THEATRES)
    if not arr or p.i32(co + CO_THEATRES + 0xC) < 1:
        return 0
    return p.u64(arr)


def _mkhandlearray(g, divisions):
    """CPdxArray<handle> of the given CArmy pointers.

    The array element is the division's own 8-byte handle (CArmy+0x18), not the
    pointer: 0x00C417A0 resolves every element through 0x142284870.
    """
    p = g.p
    n = len(divisions)
    data = cmdwin.alloc(g, 8 * n)
    p.write(data, b"".join(p.read(int(d) + CARMY_HANDLE, 8) for d in divisions))
    hdr = cmdwin.alloc(g, 0x18)
    p.write(hdr, struct.pack("<QiiQ", data, n, n, p.base + G_DEFAULT_ALLOC))
    return hdr


def _mkptrarray(g, objs):
    """CPdxArray<T*> of raw object pointers (division lists want pointers, not handles)."""
    p = g.p
    n = len(objs)
    data = cmdwin.alloc(g, 8 * n)
    p.write(data, b"".join(struct.pack("<Q", int(o)) for o in objs))
    hdr = cmdwin.alloc(g, 0x18)
    p.write(hdr, struct.pack("<QiiQ", data, n, n, p.base + G_DEFAULT_ALLOC))
    return hdr


def _tagbuf(g, handle_bytes):
    buf = cmdwin.alloc(g, 8)
    g.p.write(buf, handle_bytes)
    return buf


def create_army(g, divisions, parent=None):
    """Collect the given divisions (CArmy*) into a new army. -> (ok, err)."""
    p = g.p
    divs, rejected = validate_division_ptrs(p, divisions)
    g._last_division_rejected = rejected
    if not divs:
        return False, _format_no_valid_divisions(rejected, "create_army")
    own = g._inj is None
    if own:
        g.attach()
    try:
        th = theatre(g)
        if not th:
            return False, "this country has no theatre"
        arr = _mkhandlearray(g, divs)
        thh = _tagbuf(g, p.read(th + OG_HANDLE, 8))
        out = _tagbuf(g, b"\0" * 8)
        og = g.call(p.base + FN_CREATE_ARMY, (out, thh, arr, 0),
                    absolute=True, force=True)
        if not og:
            return False, "the game returned no army object"
        bad = [d for d in divs if p.u64(d + CARMY_OG) != og]
        if bad:
            return False, ("%d of %d divisions were not attached (first %#x -> %#x)"
                           % (len(bad), len(divs), bad[0], p.u64(bad[0] + CARMY_OG)))
        g._last_army = og
        return True, ""
    finally:
        if own:
            g.detach()


def armies(g, country=None):
    """Every COrdersGroup reachable from the country's divisions (armies + groups)."""
    from game import _arr
    p = g.p
    co = country if country is not None else g.player()
    if not isinstance(co, int):
        co = co.ptr
    seen, out = set(), []
    for d in _arr(p, co, 0x290):
        og = p.u64(d + CARMY_OG)
        if og and og not in seen:
            seen.add(og)
            out.append(og)
            parent = p.u64(og + OG_PARENT)
            while parent and parent not in seen:
                seen.add(parent)
                out.append(parent)
                parent = p.u64(parent + OG_PARENT)
    return out


# --- battle plans: CFront and the front-line order ---------------------------
# The front-line order is COrderNewRootCommand, NOT COrderNewFrontCommand: the
# latter is a CHILD front (offensive line) that needs an existing root order.
#
#   CFront        theatre+0x50 (CPdxArray<CFront*>), handle {66,id} at +0x08,
#                 border provinces at +0x20, sections at +0x58;
#                 a section carries an id at +0x08 and provinces at +0x30.
#   0x18C9B20     the 8-arg ctor: (cmd, og, handle_buf, section, from, to,
#                 CPdxArray<CArmy*>*|0, u8 flag) -- verified by the store pattern
#                 `[r14+0x48] = [rdi]`, `[r14+0x50] = ebx`, `[r14+0x58]/[r14+0x60]
#                 = from/to`, `[r14+0x6c] = al`, and by the UI call site 0xFD08F8.
#   Post          what the UI uses (0xFD0880 calls 0x1422C4250 with flag 0).
# Verified live 2026-11-14 (PID 26368): CanExecute 1, Post 1, and the engine
# appended a type-2 order with root_front {66,7} to og+0x98 by itself.
FN_NEW_ROOT_CMD = _OW.ORDER_FN_NEW_ROOT_CMD
NR_VT           = _OW.ORDER_NR_VT           # COrderNewRootCommand vtable this ctor writes
VT_OFFENSIVE    = _OW.ORDER_VT_OFFENSIVE    # COrderNewFrontCommand vtable (the offensive ctor)
NR_CANEXE       = _OW.ORDER_NR_CANEXE
NR_EXECUTE      = _OW.ORDER_NR_EXECUTE
NR_SIZE         = _OW.ORDER_NR_SIZE

CO_FRONTS      = _OW.ORDER_CO_FRONTS
FRONT_HANDLE   = _OW.ORDER_FRONT_HANDLE
FRONT_PROVS    = _OW.ORDER_FRONT_PROVS
FRONT_SECTIONS = _OW.ORDER_FRONT_SECTIONS
SEC_PROVS      = _OW.ORDER_SEC_PROVS
SEC_ID         = _OW.ORDER_SEC_ID
HANDLE_FRONT   = 66
FULL           = 100000


def _ptrs(p, obj, off, limit=4096):
    """Read a CPdxArray<T*> as a list of pointers (empty on a bogus header)."""
    data = p.u64(obj + off)
    cap = p.i32(obj + off + 8)
    size = p.i32(obj + off + 12)
    if not data or not (0 <= size <= cap < limit):
        return []
    return [p.u64(data + 8 * i) for i in range(size)]


def _prov_ids(p, obj, off):
    out = []
    for pv in _ptrs(p, obj, off):
        try:
            out.append(p.i32(pv + 0xA4))
        except Exception:
            pass
    return out


def theatres(g, country=None):
    """Every CTheatre* of the country.

    2026-11-17: only the FIRST theatre was ever scanned, which is why
    `hoi4_border_fronts(FRA)` returned [] on a live German-French war while POL
    showed two fronts -- the western front lives in a different CTheatre.
    """
    p = g.p
    co = country if country is not None else g.player()
    if not isinstance(co, int):
        co = co.ptr
    return _ptrs(p, co, CO_THEATRES)


def fronts(g, country=None):
    """Every CFront of every theatre of the country, with sections and provinces."""
    p = g.p
    out = []
    for th in theatres(g, country):
        for f in _ptrs(p, th, CO_FRONTS):
            handle = list(struct.unpack("<ii", p.read(f + FRONT_HANDLE, 8)))
            secs = []
            for s in _ptrs(p, f, FRONT_SECTIONS):
                secs.append({"ptr": s, "id": p.i32(s + SEC_ID),
                             "provinces": _prov_ids(p, s, SEC_PROVS)})
            out.append({"ptr": f, "handle": handle, "theatre": th,
                        "provinces": _prov_ids(p, f, FRONT_PROVS),
                        "sections": secs})
    return out


def draw_front_line(g, og, front, section=None, frm=0, to=FULL, divisions=None,
                    submit=True):
    """Give an army a front-line order along one CFront section.

    `front` is the CFront pointer (or a dict from fronts()); `section` defaults to
    the first section's id.  Returns (ok, err, cmd).
    """
    p = g.p
    if isinstance(front, dict):
        front = front["ptr"]
    if not front:
        return False, "no CFront", 0
    if section is None:
        secs = _ptrs(p, front, FRONT_SECTIONS)
        if not secs:
            return False, "the front has no sections", 0
        section = p.i32(secs[0] + SEC_ID)
    own = g._inj is None
    if own:
        g.attach()
    try:
        hp = _tagbuf(g, p.read(front + FRONT_HANDLE, 8))
        darr = _mkptrarray(g, [int(d) for d in divisions]) if divisions else 0
        cmd = cmdwin.alloc(g, NR_SIZE)
        p.write(cmd, b"\0" * NR_SIZE)
        g.call(p.base + FN_NEW_ROOT_CMD,
               (cmd, og, hp, int(section), int(frm), int(to), darr, 0),
               absolute=True, force=True)
        if p.i32(cmd + 0x50) != int(section):
            return False, ("section field +0x50 = %d, expected %d"
                           % (p.i32(cmd + 0x50), int(section))), cmd
        if p.read(cmd + 0x48, 8) != p.read(front + FRONT_HANDLE, 8):
            return False, "front handle was not copied into +0x48", cmd
        if not g.call(p.base + NR_CANEXE, (cmd, 0), absolute=True, force=True) & 0xFF:
            return False, "CanExecute false", cmd
        if submit:
            # 2026-11-17: CRASH FIX.  `Post` ends EVERY rejection path with
            # `call [cmd->vt[0]](cmd, 1)` -- the deleting destructor, which frees
            # through the CRT.  The object came from the game's own allocator, so
            # a refusal aborts the process (that is what killed hoi4.exe twice on
            # this exact call, 13:22 and 13:39).  Give it a vtable whose slot 0
            # returns immediately: a refused command then simply leaks.
            cmdwin.attach_fake_vtable(g, cmd, NR_VT)
            pr = cmdwin.post_drained(g, cmd, seconds=2.0)
            if not (pr.get("posted") if isinstance(pr, dict) else pr):
                return False, "Post refused the command", cmd
            # 2026-11-17 (third pass): the ctor's 7th argument -- a CPdxArray of
            # CArmy* -- does NOT attach anything.  Every front line this tool has
            # ever drawn came back with assigned == 0: visible in the UI, not one
            # division marching ("没有单位被分配到这个命令").  COrderAssignCommand
            # is the path that actually fills order+0x210, so run it here and keep
            # draw+assign a single call.
            if divisions:
                before = {o["id"] for o in orders(g, og)}
                oid = 0
                deadline = time.time() + 4.0
                while time.time() < deadline:
                    fresh = [o for o in orders(g, og)
                             if o["id"] not in before and o["type"] == 2]
                    if fresh:
                        oid = fresh[-1]["id"]
                        break
                    time.sleep(0.1)
                if oid:
                    aok, aerr = assign_to_order(g, og, oid,
                                                [int(d) for d in divisions])
                else:
                    aok, aerr = False, "the engine never created the order"
                g._last_front_assign = {"order_id": oid, "ok": bool(aok),
                                        "error": aerr}
        g._last_front_cmd = cmd
        return True, "", cmd
    finally:
        if own:
            g.detach()


def orders(g, og):
    """The army's COrderInstance list as dicts."""
    p = g.p
    out = []
    for o in _ptrs(p, og, OG_ORDERS):
        out.append({
            "ptr": hex(o),
            "id": p.i32(o + 0x244),
            "type": p.i32(o + 0x30),
            "root_front": list(struct.unpack("<ii", p.read(o + 0x258, 8))),
            "root_section": p.i32(o + 0x260),
            "from": p.i64(o + 0x268),
            "to": p.i64(o + 0x270),
            # CPdxArray<CArmy*> at +0x210 (size at +0x21C) -- the count the
            # tooltip's "no unit is assigned to this order" complains about.
            "assigned_divisions": p.i32(o + 0x210 + 0xC),
        })
    return out


# --- the offensive child orders -------------------------------------------
# A front order (type 2) owns its offensive children (type 1) in a
# CPdxArray<COrderInstance*> somewhere inside its own body.  The array offset
# is not fixed, so the scan below walks the first 0x3c0 bytes.  It MUST check
# the child's vtable: without that check every 8-byte slot that happens to look
# like {ptr, cap, size} yields heap noise, and writing to that noise corrupts
# the process (three crashes on 2026-11-14).
VT_ORDER_INSTANCE = _OW.ORDER_VT_ORDER_INSTANCE     # .?AVCOrderInstance@@ vtable
ORDER_TYPE_OFFENSIVE = 1
OI_ASSIGNED = _OW.ORDER_OI_ASSIGNED                 # CPdxArray<CArmy*> of divisions
OI_EXECUTING = _OW.ORDER_OI_EXECUTING               # u8; 0 = the divisions never attack
OI_ID = _OW.ORDER_OI_ID
OI_TYPE = _OW.ORDER_OI_TYPE
FN_ORDER_SET_EXECUTING = _OW.ORDER_FN_SET_EXECUTING  # (order, u8 executing, u8) -> void


def _heap(x):
    return 0x10000000000 < x < 0x7F0000000000


def _child_array(p, root, off, count_off, limit=512):
    """One of a front order's two child arrays, read through its fixed offsets.

    2026-11-19f: `0x10D1280` walks BOTH of them when it propagates an execute
    flag to the type-1 children -- `+0x1E0` (count `+0x1EC`) and `+0x1F8`
    (count `+0x204`).  The old version of order_children only scanned the body
    for something that *looked* like a CPdxArray of order pointers and missed
    engine-created arrows entirely (measured: 3 children visible, then 0, while
    the player watched the arrows advance on screen).
    """
    n = p.i32(root + count_off)
    if not (0 <= n <= limit):
        return []
    data = p.u64(root + off)
    if not _heap(data):
        return []
    out = []
    for i in range(n):
        c = p.u64(data + 8 * i)
        if _heap(c) and not (c & 7):
            out.append(c)
    return out


def order_children(g, og):
    """Type-1 offensive children of every front order of this group.

    2026-11-23 (复测 10 A-2 & 复测 11 P0-1 反汇编实证):
      * COrderInstance::FindOrder (0x10AF270) 仅递归遍历两个子数组：
        `+0x1F8` (count `+0x204`, GetChildren) 与 `+0x2D0` (count `+0x2DC`, SubOrders)。
      * 旧版保留的 `for off in range(0, 0x3C0, 8)` 盲扫会把 `+0x2B8` 等反向/外部关联
        数组中的非子订单（例如第2军 order 37 旁的 195）误认成 owned child，导致
        COrderDeleteCommand::CanExecute(og, 195) 报 `no such order 195` 并永久死锁
        delete_order(37) 与 reset_battleplans()！
      * 现彻底移除盲扫，仅读取精确偏移 `+0x1F8`、`+0x2D0`，以及旧版 manual_offensive
        倒挂 bug 遗留的 `+0x1E0` (标明 `inverted_parent_link=True`)；同时检查
        `child+0x38` (所属 COrdersGroup*)，若指向其它集团军则标明 `foreign_group=True`。
    """
    p = g.p
    og_int = int(og)
    out = []
    for o in orders(g, og):
        if o["type"] != 2:
            continue
        root = int(o["ptr"], 16)
        cands = []
        seen = set()
        # 1) 真实引擎子数组: +0x1F8 (GetChildren) 与 +0x2D0 (SubOrders)
        for src_off, cnt_off, tag_off in (
            (0x1F8, 0x204, "0x1f8"),
            (0x2D0, 0x2DC, "0x2d0"),
            (0x1E0, 0x1EC, "0x1e0"),
        ):
            for c in _child_array(p, root, src_off, cnt_off):
                if c in seen:
                    continue
                seen.add(c)
                cands.append((c, tag_off))
        for child, src_tag in cands:
            try:
                if p.u64(child) - p.base != VT_ORDER_INSTANCE:
                    continue
            except Exception:
                continue
            # 2026-11-19: a hand-drawn plan also stores a 0-division TYPE-2
            # "front segment" inside the root's own body (+0x2d0).  Without
            # this type filter it showed up as a second "offensive line" and
            # empty_order_scan() then refused to unpause over it.
            if p.i32(child + OI_TYPE) != ORDER_TYPE_OFFENSIVE:
                continue
            child_og = 0
            try:
                child_og = int(p.u64(child + 0x38) or 0)
            except Exception:
                child_og = 0
            is_foreign = bool(_heap(child_og) and child_og != og_int)
            row = {
                "root": o["id"],
                "root_ptr": root,
                "ptr": child,
                "id": p.i32(child + OI_ID),
                "type": p.i32(child + OI_TYPE),
                "assigned": p.i32(child + OI_ASSIGNED + 0xC),
                # +0x248 is a BYTE: the three bytes after it are unrelated
                # fields, so an i32 read returns garbage and start_offensives
                # would "re-set" a flag that is already 1 every single time.
                "executing": p.u8(child + OI_EXECUTING),
                "source_offset": src_tag,
            }
            if src_tag == "0x1e0":
                row["inverted_parent_link"] = True
            if is_foreign:
                row["foreign_group"] = True
                row["owner_group"] = hex(child_og)
            out.append(row)
    return out


def set_order_executing(g, child, on=True):
    """Set +0x248 through the engine's own routine.  Returns the byte read back."""
    p = g.p
    child_addr = int(child, 16) if isinstance(child, str) else int(child)
    g.call(p.base + FN_ORDER_SET_EXECUTING, (child_addr, 1 if on else 0, 1),
           absolute=True, force=True)
    return p.u8(child_addr + OI_EXECUTING)


def start_offensives(g, og):
    """Mark every accepting offensive child of this group as executing.

    COrderExecuteCommand (0x18C8C50) creates the children and hands the
    divisions to them, but leaves +0x248 at 0: the plan draws on the map and
    nothing moves.  Verified 2026-11-14 -- with the flag set the same orders
    took six Dutch provinces and the Netherlands capitulated.
    """
    out = []
    for c in order_children(g, og):
        if c["type"] != ORDER_TYPE_OFFENSIVE or c["assigned"] <= 0:
            continue
        if c["executing"] == 1:
            continue
        out.append((c["id"], set_order_executing(g, c["ptr"], True)))
    return out


def _children_of(g, og):
    """Armies whose OG_PARENT is `og`.

    2026-11-18: `army_info` used to report `members` from `og+0x228+0xC`. On a
    freshly created army group that read returned 615 for a group of 2 armies --
    a fake number that would have been trusted. Count the children instead; it
    is O(n) over the country's order groups and cannot invent members.
    """
    p = g.p
    return [x for x in armies(g) if x != og and p.u64(x + OG_PARENT) == og]


def army_info(g, og):
    """One COrdersGroup as a dict, with only fields verified on Windows."""
    p = g.p
    parent = p.u64(og + OG_PARENT)
    is_group = bool(p.u8(og + OG_IS_GROUP))
    kids = _children_of(g, og) if is_group else []
    divs = p.i32(og + OG_DIVISIONS + 0xC)
    if is_group:
        divs = sum(p.i32(k + OG_DIVISIONS + 0xC) for k in kids)
    return {
        "ptr": hex(og),
        "handle": list(struct.unpack("<ii", p.read(og + OG_HANDLE, 8))),
        "is_army_group": is_group,
        "theatre": hex(p.u64(og + OG_THEATRE)) if p.u64(og + OG_THEATRE) else None,
        "divisions": divs,
        "orders": p.i32(og + OG_ORDERS + 0xC),
        "parent": hex(parent) if parent else None,
        "members": len(kids) if is_group else None,
        "member_ptrs": [hex(k) for k in kids] if is_group else None,
    }


# =====================================================================
# M4: the pieces the MCP tool layer needs, moved out of stage/wplan.py
# =====================================================================
# `stage/wplan.py` proved these live (2026-11-14: six Dutch provinces and a
# capitulation; 2026-11-15: the Danish front + offensive).  The tool layer
# cannot import a stage script, so the same calls live here now.

FN_NEW_OFFENSIVE_CMD = _OW.ORDER_FN_NEW_OFFENSIVE_CMD   # COrderNewFrontCommand ctor (the offensive line)
FN_ORDER_FRONT_CTX   = _OW.ORDER_FN_FRONT_CTX           # order -> the frontCtx token `+0xD0` must equal
FN_CANEXE_OFFENSIVE  = _OW.ORDER_FN_CANEXE_OFFENSIVE
OE_CTOR, OE_VT, OE_SIZE = _OW.ORDER_OE_CTOR, _OW.ORDER_OE_VT, _OW.ORDER_OE_SIZE   # COrderExecuteCommand

ST_PROV_ID = _OW.PROV_ID                                # CProvince -> int32 province id


COUNTRY_ARMIES = _OW.CO_ARMIES                          # CCountry -> CPdxArray<CArmy*>
CARMY_GROUP = _OW.ORDER_CARMY_OG                        # CArmy -> COrdersGroup* (verified 2026-11-19)


def country_divisions(g, country=None):
    """Every CArmy* of this country (CCountry+0x290)."""
    p = g.p
    co = country if country is not None else g.player()
    co = int(getattr(co, "ptr", co))
    data = p.u64(co + COUNTRY_ARMIES)
    size = p.i32(co + COUNTRY_ARMIES + 0xC)
    out = []
    for i in range(max(0, min(int(size), 4000))):
        d = p.u64(data + 8 * i)
        if _heap(d):
            out.append(d)
    return out


CARMY_SECONDARY = _OW.ORDER_CARMY_SECONDARY  # og+0x50 holds (CArmy* + 0xB8): a secondary base subobject


def divisions_of(g, og):
    """The CArmy* this orders group owns.

    2026-11-19: `og+0x50` is a `CPdxArray` whose elements are **not** the
    CArmy pointers every other routine wants -- each one is the object's
    +0xB8 secondary base subobject (MSVC multiple inheritance).  Reading the
    {type,id} handle at "+0x18" off those pointers returned {0,0}, so
    `_mkhandlearray` built an array of null handles and
    `COrderAssignCommand::CanExecute` answered "#~ invalid unit reference"
    for every division, no matter how many were passed.

    Verified: og+0x50[0] = 0x1b58ef0be98, primary = 0x1b58ef0bde0 (== -0xB8),
    and primary+0x18 = {51,1}, primary+0xC0 = the group.

    Fallback: filter the country army list (CCountry+0x290) on
    `CArmy+0xC0 == og`, in case the +0xB8 displacement ever changes.
    """
    p = g.p
    out = []
    for raw in _ptrs(p, og, OG_DIVISIONS):
        d = raw - CARMY_SECONDARY
        if _heap(d) and p.u64(d + CARMY_GROUP) == og:
            out.append(d)
    if out:
        return out
    return [d for d in country_divisions(g) if p.u64(d + CARMY_GROUP) == og]


def owned_provinces(g, tag):
    """state id -> [province ids] for the provinces this country OWNS.

    Reads the global state table (GS_STATES) and each state's owner.  Lives here
    so the package needs nothing outside `calling/`.
    """
    import offsets_win as W
    p = g.p
    idx = g.country(tag).tag_id
    arr = p.u64(g.gamestate + W.GS_STATES)
    n = p.i32(g.gamestate + W.GS_STATES + W.PDXARRAY_SIZE)
    out = {}
    for i in range(max(0, min(n, 4096))):
        st = p.u64(arr + 8 * i)
        if not _heap(st):
            continue
        if p.i32(st + W.CS_OWNER) != idx:
            continue
        sid = p.i32(st + W.ST_ID)
        parr = p.u64(st + W.ST_PROVINCES)
        pn = p.i32(st + W.ST_PROVINCES + 0xC)
        ids = []
        for k in range(max(0, min(pn, 512))):
            pr = p.u64(parr + 8 * k)
            if _heap(pr):
                ids.append(p.i32(pr + ST_PROV_ID))
        out[sid] = ids
    return out


def fronts_towards(g, enemy_tag, own_tag=None):
    """Front sections that actually touch `enemy_tag`.

    Same contract as armyops.fronts_towards: a section is only returned when
    some of its provinces belong to us and border the enemy.  The Linux version
    also counts which country most of the section faces; here the CFront list
    comes from the theatre (verified offsets) instead of an RTTI scan, so the
    enemy test is done by walking the static adjacency graph.

    2026-11-23 (复测 10 A-4): 补报 `section_provinces`（完整有序边境省列表）与
    `slice_from`/`slice_to`（本敌国在多国合体前线 section 中实际占据的 0..100000 区间），
    使 plan_front / set_front_directive 绝不越界画到邻国（如从 HOL 画到 FRA 马奇诺）。
    """
    import mapdata
    own_tag = own_tag or g.player_tag()
    mine = set(sum(owned_provinces(g, own_tag).values(), []))
    theirs = set(sum(owned_provinces(g, enemy_tag).values(), []))
    adj = mapdata.build_adjacency()
    out = []
    for f in fronts(g, g.country(own_tag)):
        for si, s in enumerate(f["sections"]):
            sec_provs = list(s["provinces"])
            ids = [x for x in sec_provs if x in mine]
            if not ids:
                continue
            touch = [x for x in ids if any(n in theirs for n in adj.get(x, ()))]
            if not touch:
                continue
            touch_set = set(touch)
            n_sec = len(sec_provs)
            idxs = [i for i, pv in enumerate(sec_provs) if pv in touch_set]
            if idxs and n_sec > 0:
                sub_from = int(FULL * min(idxs) / n_sec)
                sub_to = int(FULL * (max(idxs) + 1) / n_sec)
                sub_from = max(0, min(FULL, sub_from))
                sub_to = max(sub_from, min(FULL, sub_to))
            else:
                sub_from, sub_to = 0, FULL
            out.append({"front": f["ptr"], "front_handle": f["handle"],
                        "section": s["id"], "section_index": si,
                        "section_provinces": sec_provs,
                        "enemy_border_indices": idxs,
                        "slice_from": sub_from,
                        "slice_to": sub_to,
                        "border_provinces": sorted(touch)})
    return out


# --- COrderNewFrontCommand: the raw ctor is OFF BY DEFAULT (2026-11-18) ----
# This function killed hoi4.exe twice on 2026-11-18 (the 15:16 one left no crash
# dump: `ReadProcessMemory(0x1e99e9d0210) GetLastError=299`, raised inside the
# ctor call below).  Two independent defects, both read out of the disassembly
# in research/WINDOWS_1.19.5.md §38.2 / §39:
#
#   1. The contract is 0x18C9840(cmd, og, CPdxArray<i32>* provinces,
#      COrderInstance* parent, <OUT flag>, 0, 0, 0) -- the 5th argument is an
#      OUT-POINTER (the UI passes &[rbp-0x79], §39.3).  The old code passed the
#      CPdxArray<CArmy*> of divisions there, so the ctor wrote its out flag
#      straight into that array's *header* -- i.e. through the heap pointer at
#      [0].  The next dereference of it is exactly the 0x1e99e9d0210 error.
#   2. Nothing was verified afterwards, unlike draw_front_line, which checks
#      +0x48 (front handle) and +0x50 (section) on the same class of object.
#
# CORRECTED 2026-11-19e: the "arg5 is an OUT pointer" half of that diagnosis was
# wrong.  Disassembling 0x18C9840 shows arg5 is a CPdxArray<CArmy*> of divisions
# (the ctor reads [arg5+0xc], walks [arg5] as a pointer array, filters by
# element+0x1D8 == og+0x3c and appends element+0x18 into cmd+0x38).  The old
# crash is attributed to arg9 being un-passed stack garbage, not to arg5.
# Both are now passed explicitly: arg5 = the division array, arg9 = the CFront.
#
# The supported way to get an offensive child is `execute_plan(root_id, 1)`:
# COrderExecuteCommand's own Execute creates the type-1 order and sets the
# executing byte.  plan_front / _win_battleplans use that path.
RAW_OFFENSIVE_CTOR_ALLOWED = False

_PROLOGUES = (
    b"\x48\x89\x5c\x24", b"\x48\x89\x4c\x24", b"\x48\x89\x54\x24",
    b"\x48\x83\xec", b"\x48\x81\xec", b"\x4c\x8b\xdc", b"\x48\x8b\xc4",
    b"\x41\x54", b"\x41\x55", b"\x41\x56", b"\x41\x57",
    b"\x55", b"\x53", b"\x56", b"\x57",
)


def _stores_vtable(g, rva, vt, span=0x180):
    """True when the routine at IMAGE OFFSET `rva` is an entry that stores `vt`.

    Same two-part test as armyops._ctor_guard, kept local so this module stays
    dependency-free: a stale constant either lands inside an unrelated
    instruction (prologue test) or on a real function of another class
    (vtable-store test).
    """
    p = g.p
    try:
        blob = p.read(p.base + int(rva), span)
    except Exception:
        return False
    if not any(blob.startswith(pr) for pr in _PROLOGUES):
        return False
    for i in range(len(blob) - 7):
        if blob[i:i + 3] == b"\x48\x8d\x05":            # lea rax,[rip+d32]
            disp = struct.unpack_from("<i", blob, i + 3)[0]
            if int(rva) + i + 7 + disp == vt:
                return True
        if blob[i:i + 3] == b"\x48\xc7\x01":            # mov qword [rcx], imm32
            if struct.unpack_from("<I", blob, i + 3)[0] == vt:
                return True
        if blob[i:i + 2] == b"\x48\xb8":                # mov rax, imm64
            if struct.unpack_from("<Q", blob, i + 2)[0] == vt:
                return True
    return False


def front_context(g, root_order):
    """The `frontCtx` token `0x10BB5A0(root_order)` returns.

    2026-11-19e (bug journal §33.9/§33.10): `COrderNewFrontCommand::Execute`
    only builds the type-1 child when at least one of the candidate provinces
    satisfies `province+0xD0 == frontCtx` (that is the whole body of
    `0xD57D90`: `cmp [prov+0xD0], frontCtx`).  With no match, `0x10B8A40`
    appends nothing, `splits` stays empty and Execute bails silently.
    """
    p = g.p
    if not root_order:
        return 0
    try:
        return int(g.call(p.base + FN_ORDER_FRONT_CTX, (int(root_order),),
                          absolute=True, force=True) or 0)
    except Exception:
        return 0


def _area_component(p, area_ptr, idx):
    """复现引擎 0xD57DD6: 取 CControllerArea 在国家/战区索引 idx 下的敌对连通组件指针。"""
    if not area_ptr or area_ptr < 0x10000 or idx < 0:
        return 0
    try:
        e0_cnt = int(p.i32(area_ptr + 0xEC))
        if idx >= e0_cnt or e0_cnt <= 0 or e0_cnt > 4096:
            return 0
        e0_data = int(p.u64(area_ptr + 0xE0))
        if e0_data < 0x10000:
            return 0
        slot = int(p.i32(e0_data + 4 * idx))
        if slot < 0:
            return 0
        c8_cnt = int(p.i32(area_ptr + 0xD4))
        if slot >= c8_cnt or c8_cnt <= 0 or c8_cnt > 4096:
            return 0
        c8_data = int(p.u64(area_ptr + 0xC8))
        if c8_data < 0x10000:
            return 0
        return int(p.u64(c8_data + 8 * slot))
    except Exception:
        return 0


def _og_area_index(p, og):
    """复现引擎 0xC450A0(og) -> *arg3 索引（用于 0xD57D90 敌对连通区查询）。"""
    if not og:
        return 0
    try:
        th = int(p.u64(int(og) + OG_THEATRE))
        if th > 0x10000:
            v110 = int(p.i32(th + 0x110))
            if v110 > 0:
                return v110
            v48 = int(p.i32(th + 0x48))
            if v48 > 0:
                return v48
        return max(0, int(p.i32(int(og) + 0x3C)))
    except Exception:
        return 0


def area_matches_front(p, root_area, prov_area, og=None):
    """100% 纯只读复现引擎 0xD57D90(root_area, prov_area, 0xC450A0(og))。

    2026-11-23 (复测 10 A-1/A-3 反汇编 + 真机实证):
      * 0xD57DBF 快路径: `root_area == prov_area` -> True；
      * 0xD57DD6 连通战区路径: 当多个敌国（如西线 LUX + BEL + HOL + FRA）共享
        同一条 CFront 时，CFront+0x70 只指向其中一个敌国（例如 LUX），但四国的
        `area->+0xC8[area->+0xE0[idx]]` 指向同一个敌对连通区指针 -> 全部返回 True；
      * 己方省份（如德国省）的 `+0xD4 == 0` -> 返回 False（会被 0x10B8A40 全部滤空！）。
    """
    if not root_area or not prov_area:
        return False
    if int(root_area) == int(prov_area):
        return True
    idx = _og_area_index(p, og)
    ca = _area_component(p, int(root_area), idx)
    if not ca:
        return False
    cb = _area_component(p, int(prov_area), idx)
    return bool(cb and ca == cb)


def front_context_readonly(g, root_order):
    """纯只读获取前线订单对应的 CControllerArea*（优先走 CFront+0x70，无需注入）。"""
    p = g.p
    if not root_order:
        return 0
    try:
        fh = list(struct.unpack("<ii", p.read(int(root_order) + 0x258, 8)))
        if fh[0] == HANDLE_FRONT and fh[1] > 0:
            for f in fronts(g):
                if f.get("handle") == fh:
                    ctx = int(p.u64(int(f["ptr"]) + 0x70))
                    if ctx > 0x10000:
                        return ctx
    except Exception:
        pass
    return 0


def filter_front_provinces(g, provinces, ctx, og=None):
    """Keep only enemy target provinces accepted by 0xD57D90 for `ctx`."""
    p = g.p
    if not ctx:
        return list(provinces)
    out = []
    for pid in provinces or []:
        try:
            pa = p.u64(g.province_ptr(int(pid)) + 0xD0)
            if area_matches_front(p, ctx, pa, og=og):
                out.append(int(pid))
        except Exception:
            continue
    return out


def offensive_line(g, og, parent_id, provinces, divisions=None, can_execute=True,
                   unsafe=None, front=None):
    """Draw the offensive line (the type-1 child) under one front order.

    `parent_id` is the type-2 order id the line hangs off; pass the id of the
    front order just drawn -- picking "the longest root" silently attaches to a
    leftover order from an earlier war (that is how the Danish run first
    pointed at the Dutch front).

    `front` (2026-11-19d) is the CFront* the order belongs to; it is passed as
    the ctor's NINTH argument.  Measured on a live game: with 0 the ctor leaves
    `cmd+0x30` at {0,0}; with the real CFront (handle {66,7}) it stores {66,7}
    there, CanExecute still returns 1 and Post is accepted.  It does NOT yet
    make Execute produce the child -- that still dies in 0x10B8620 / 0x10BB5A0
    (see the bug journal Sec.33.3) -- but it is a strictly better call.

    **Refuses by default on this build** -- see the block comment above.
    `unsafe=True` runs the raw ctor with the corrected argument list (out
    buffer instead of the division array) and verifies the object before it is
    posted, but the resulting arrow has still NOT been observed to drive attacks.
    """
    if unsafe is None:
        unsafe = RAW_OFFENSIVE_CTOR_ALLOWED
    if not unsafe:
        return (False,
                "COrderNewFrontCommand::Execute bails in its split computation "
                "(0x10B8620 -> 0x10BB5A0 returns NULL), so the queue path cannot "
                "make an arrow. Use armyops_win.manual_offensive() -- "
                "Game.offensive_line() already falls back to it.", 0)
    p = g.p
    if not provinces:
        return False, "no target provinces", 0
    root = None
    for o in orders(g, og):
        if o["type"] == 2 and o["id"] == parent_id:
            root = int(o["ptr"], 16)
            break
    if root is None:
        return False, "no type-2 order with id %s" % parent_id, 0
    # 2026-11-19e: only the provinces that share the root order's front token are
    # accepted by Execute's split computation.  Filtering here is what turned a
    # silent no-op into an engine-built type-1 order with its divisions filled
    # in (bug journal §33.10); without it every arrow we drew was discarded.
    _ctx = front_context(g, root)
    if _ctx:
        _kept = filter_front_provinces(g, provinces, _ctx)
        if not _kept:
            return (False,
                    "none of the %d target provinces shares the front token of "
                    "order %s (0x%X) -- Execute would discard the arrow"
                    % (len(provinces or []), parent_id, _ctx), 0)
        provinces = _kept
    if divisions is None:
        divisions = divisions_of(g, og)
    divisions = [int(d) for d in divisions or []]
    if not divisions:
        # an order whose CPdxArray<CArmy*> is empty is the 0xB2E62E crash
        return False, ("refusing: 0 divisions -- an order with an empty army "
                       "array is the hoi4.exe+0xB2E62E crash"), 0
    if not _stores_vtable(g, FN_NEW_OFFENSIVE_CMD, VT_OFFENSIVE):
        return False, ("ctor 0x%X does not store the COrderNewFrontCommand "
                       "vtable 0x%X on this build -- stale image offset, refusing"
                       % (FN_NEW_OFFENSIVE_CMD, VT_OFFENSIVE)), 0
    own = g._inj is None
    if own:
        g.attach()
    try:
        # arg2: CPdxArray<i32> of province ids (4-byte elements, §39.3)
        path = cmdwin.alloc(g, 4 * len(provinces))
        p.write(path, b"".join(struct.pack("<i", int(x)) for x in provinces))
        pa = cmdwin.alloc(g, 0x18)
        p.write(pa, struct.pack("<QiiQ", path, len(provinces), len(provinces),
                               p.base + G_DEFAULT_ALLOC))
        # 2026-11-19: the ctor takes NINE arguments, not eight.  arg5 is
        # CPdxArray<CArmy*>*, arg9 is an optional object whose handle lands in
        # cmd+0x30.  The old call passed an out-buffer as arg5 and left arg9 as
        # whatever garbage sat on the stack -- a wild pointer straight into the
        # ctor's `[rax+8]` read.  Pass 0 explicitly for both; 0 is safe (the
        # ctor tests them) and the real division list is attached afterwards by
        # COrderAssignCommand.
        cmd = cmdwin.alloc(g, 0x70)          # sizeof COrderNewFrontCommand == 0x70
        p.write(cmd, b"\0" * 0x70)
        # 2026-11-19e: the 5th argument is a CPdxArray<CArmy*> of DIVISIONS, not
        # an out-buffer.  Disassembly of the real ctor (0x18C9840) settles it --
        # arg5 sits at [rsp+0x90] after the prologue and is dereferenced as an
        # array: `r12 = [arg5+0xc]` (count), `rbx = [[arg5] + i*8]` (element),
        # filter `[rbx+0x1D8] == og+0x3c`, then append `[rbx+0x18]` (the packed
        # handle) into `cmd+0x38`.  Live proof: passing the 11 divisions of
        # 第5集团军 leaves `cmd+0x40 (size) == 11`.
        # The Linux implementation the player remembers working did exactly this
        # (`(cmd, og, path, parent, darray, 0,0,0,0)`); the Windows port's
        # "arg5 is an OUT pointer" claim (WINDOWS_1.19.5.md §39.3) was wrong.
        darr = _mkptrarray(g, divisions)
        g.call(p.base + FN_NEW_OFFENSIVE_CMD,
               (cmd, og, pa, root, darr, 0, 0, 0,
                (int(front) if front else 0)), absolute=True, force=True)
        if p.u64(cmd) != p.base + VT_OFFENSIVE:
            return False, ("ctor wrote vptr %#x, expected 0x%X"
                           % (p.u64(cmd), p.base + VT_OFFENSIVE)), cmd
        want_pid = p.i32(root + 0x244)
        got_pid = p.i32(cmd + 0x68)
        # 0x18C99F9 only copies arg4->[0x244] into cmd+0x68 when arg4->[0x38]
        # equals arg1 (the COrdersGroup), and CanExecute tests `+0x68 != 0`.
        if not got_pid:
            return False, ("the ctor left cmd+0x68 (parent order id) at 0 -- "
                           "arg4 was not accepted as the parent order"), cmd
        g._last_offensive_parent = {"command": got_pid, "expected": want_pid}
        if can_execute and not (g.call(p.base + FN_CANEXE_OFFENSIVE, (cmd, 0),
                                       absolute=True, force=True) & 0xFF):
            return False, "CanExecute false", cmd
        # same Post-rejection hazard as draw_front_line: never let Post's drop
        # path run the real deleting destructor on a game-allocator object.
        cmdwin.attach_fake_vtable(g, cmd, VT_OFFENSIVE)
        # 2026-11-19: `post` only ENQUEUES; the command runs when the clock next
        # ticks.  The old code polled for a child while the game sat paused, so
        # it could never see one.  post_drained runs ~2 real seconds of clock and
        # restores the pause byte.
        pr = cmdwin.post_drained(g, cmd, seconds=2.0)
        if not (pr.get("posted") if isinstance(pr, dict) else pr):
            return False, "Post rejected", cmd
        # divisions are NOT a ctor argument -- attach them like the front line
        before = {c["id"] for c in order_children(g, og)}
        oid, deadline = 0, time.time() + 10.0
        while time.time() < deadline:
            fresh = [c for c in order_children(g, og)
                     if c["id"] not in before
                     and c["type"] == ORDER_TYPE_OFFENSIVE]
            if fresh:
                oid = fresh[-1]["id"]
                break
            time.sleep(0.1)
        if oid:
            aok, aerr = assign_to_order(g, og, oid, divisions)
            g._last_offensive_assign = {"order_id": oid, "ok": bool(aok),
                                        "error": aerr}
            return True, "", cmd
        # 2026-11-19: never report success for a command that produced nothing.
        # `Execute` bails silently when its split computation (0x10B8620) finds
        # no section of the parent front in the province list.
        return False, ("engine accepted the command but created no offensive "
                       "line (COrderNewFrontCommand::Execute found no matching "
                       "front section; use manual_offensive instead)"), cmd
    finally:
        if own:
            g.detach()


# --- one engine command that creates the front line AND the offensive arrow -
# 2026-11-19: `COrderAddNewCompletePlanCommand` (type 0x365e) is the engine's own
# "draw a whole battle plan" command.  Its Execute (0x18D3FA0) first builds a
# COrderNewRootCommand (type-2 front line), executes it, then -- when the command
# carries a non-empty province array -- builds a COrderNewFrontCommand (type-1
# offensive) on top of the root instance it just created.  That is exactly the
# pair the UI produces when you drag a front line and then an arrow, and it is
# the only path that has never killed the process on this build.
FN_PLAN_FACTORY  = _OW.ORDER_FN_PLAN_FACTORY        # no-arg factory: new(0x80) + ctor, -> cmd
VT_COMPLETE_PLAN = _OW.ORDER_VT_COMPLETE_PLAN
FN_PLAN_CANEXE   = _OW.ORDER_FN_PLAN_CANEXE
PLAN_SIZE        = _OW.ORDER_PLAN_SIZE

PLAN_ARMY   = _OW.ORDER_PLAN_ARMY                   # handle {u32 type, u32 id} of the COrdersGroup
# 2026-11-23 (C-1 进程崩溃根因实锤): COrderAddNewCompletePlanCommand::Execute (0x18D3FA0)
# 在 0x18D4176 直接把 &cmd+0x30 传给 COrderAssignCommand ctor (0x18C7DF0)，后者按 8 字节步长
# 把每个元素当作 CArmy 的 {type=51, id} 句柄调用 ResolveHandle (0x2284870)！
# 旧代码把 4 字节省份 ID 数组塞进 +0x30，导致越界读堆并命中非 CArmy 句柄，读 [obj-0x10+0x1D8]
# 作为国家 tag_id 索引 tag_table 时在 0xC08724 触发 C0000005 秒杀 hoi4.exe！
PLAN_UNITS  = _OW.ORDER_PLAN_UNITS                  # CPdxArray<handle> (8B CArmy handles); +0x3c count > 0
PLAN_IDS    = PLAN_UNITS                            # 向后兼容旧常量名
PLAN_PROVS  = _OW.ORDER_PLAN_PROVS                  # CPdxArray<u32>; handed to COrderNewFrontCommand
PLAN_FRONT  = _OW.ORDER_PLAN_FRONT                  # handle of the CFront
PLAN_SECT   = _OW.ORDER_PLAN_SECT
PLAN_FROM   = _OW.ORDER_PLAN_FROM
PLAN_TO     = _OW.ORDER_PLAN_TO


def validate_division_ptrs(p, divs):
    """校验并返回合法 CArmy* 指针列表与被拒详情 `(valid, rejected)`。

    2026-11-23 (复测 9 D-1 & D-3):
      * 绝不能按 `htype == 51` 白名单过滤！开局历史师句柄为 `{51, id}`，运行时训练出的新师
        句柄为 `{4713, id}`（实测全球 1297 师均有效），两者都能被 0x2284870 正常解析；
      * 正确不变量是：(1) 非空句柄 `htype > 0 and hid > 0`；(2) 若 `[d+0x00]` 落在主模块
        虚表区间内，则其虚表 RVA 必须等于 `CARMY_VT (0x29D6D98)`（零注入精准拦截 `+0xB8`
        子对象指针或非 CArmy 脏指针，且兼容未写入虚表的离线 mock）。
    """
    out = []
    rejected = []
    seen = set()
    base = int(getattr(p, "base", 0) or 0)
    for raw in (divs or ()):
        try:
            d = int(raw, 16) if isinstance(raw, str) else int(raw)
        except Exception:
            rejected.append({"ptr": str(raw), "reason": "invalid pointer format"})
            continue
        if not d:
            rejected.append({"ptr": "0x0", "reason": "null pointer"})
            continue
        if d in seen:
            continue
        # 若可读到指向主模块虚表段的 vptr，严检其必须是 CArmy 基类虚表
        if base > 0:
            try:
                vp = int(p.u64(d) or 0)
            except Exception as e:
                rejected.append({"ptr": hex(d), "reason": "unreadable memory: %s" % e})
                continue
            if base < vp < base + 0x20000000:
                rva = vp - base
                if rva != CARMY_VT:
                    hint = ""
                    try:
                        if int(p.u64(d - ORDER_DIV_SUBOBJ_OFF) or 0) == base + CARMY_VT:
                            hint = " (looks like CArmy+0xB8 subobject pointer)"
                    except Exception:
                        pass
                    rejected.append({
                        "ptr": hex(d),
                        "vptr_rva": hex(rva),
                        "reason": "vptr 0x%X != CArmy 0x%X%s" % (rva, CARMY_VT, hint),
                    })
                    continue
        try:
            hb = p.read(d + CARMY_HANDLE, 8)
            htype, hid = struct.unpack("<II", hb)
        except Exception as e:
            rejected.append({"ptr": hex(d), "reason": "cannot read CArmy handle (+0x18): %s" % e})
            continue
        if htype <= 0 or hid <= 0:
            rejected.append({
                "ptr": hex(d),
                "handle": {"type": int(htype), "id": int(hid)},
                "reason": "empty CArmy handle {type=%d, id=%d}" % (htype, hid),
            })
            continue
        seen.add(d)
        out.append(d)
    return out, rejected


def _format_no_valid_divisions(rejected, context=""):
    """统一拼接无可用师的诊断错误信息（D-3）。"""
    prefix = "no usable divisions" + ((" for %s" % context) if context else "")
    if not rejected:
        return prefix + " (empty input)"
    first = rejected[0]
    return "%s (%d rejected; first %s: %s)" % (
        prefix, len(rejected), first.get("ptr"), first.get("reason")
    )


def _valid_division_ptrs(p, divs):
    """兼容包装：仅返回合法 CArmy* 指针列表。"""
    valid, _ = validate_division_ptrs(p, divs)
    return valid


def _mkintarray(g, values):
    """CPdxArray<u32> header the game can read (0x18 bytes incl. allocator)."""
    p = g.p
    vals = [int(v) for v in values]
    n = len(vals)
    data = cmdwin.alloc(g, 4 * max(n, 1))
    p.write(data, b"".join(struct.pack("<i", v) for v in vals))
    hdr = cmdwin.alloc(g, 0x18)
    p.write(hdr, struct.pack("<QiiQ", data if n else 0, n, n,
                             p.base + G_DEFAULT_ALLOC))
    return hdr


def complete_plan(g, og, front, provinces, section=None, frm=0, to=FULL,
                  divisions=None, assign=True):
    """Create a front line + offensive line in one engine command.

    Returns (ok, err, info).  `info` carries order_id / children / assigned so a
    caller can verify what the engine really built instead of trusting Post.
    """
    p = g.p
    if isinstance(front, dict):
        front = front["ptr"]
    if not front:
        return False, "no CFront", {}
    provs = [int(x) for x in (provinces or [])]
    if not provs:
        return False, "no provinces -- the offensive branch bails on an empty array", {}
    if section is None:
        secs = _ptrs(p, front, FRONT_SECTIONS)
        if not secs:
            return False, "the front has no sections", {}
        section = p.i32(secs[0] + SEC_ID)

    # +0x30 必须存放真实的 8 字节 CArmy 句柄数组（若调用方未传，则读该集团军实有师列表）
    raw_divs = divisions if divisions is not None else divisions_of(g, og)
    valid_divs, rejected = validate_division_ptrs(p, raw_divs)
    if not valid_divs:
        return False, _format_no_valid_divisions(rejected, "CompletePlan (+0x30)"), {
            "rejected": rejected[:10],
        }

    own = g._inj is None
    if own:
        g.attach()
    cmd = 0
    try:
        cmd = g.call(p.base + FN_PLAN_FACTORY, (), absolute=True, force=True)
        if not cmd or p.u64(cmd) != p.base + VT_COMPLETE_PLAN:
            return False, ("factory returned %r (vptr %r), expected vtable %#x"
                           % (cmd, p.u64(cmd) if cmd else None,
                              p.base + VT_COMPLETE_PLAN)), {}
        a1 = _mkhandlearray(g, valid_divs)
        a2 = _mkintarray(g, provs)
        p.write(cmd + PLAN_ARMY, p.read(og + OG_HANDLE, 8))
        p.write(cmd + PLAN_UNITS, p.read(a1, 0x18))
        p.write(cmd + PLAN_PROVS, p.read(a2, 0x18))
        p.write(cmd + 0x60, b"\x00")
        p.write(cmd + PLAN_FRONT, p.read(front + FRONT_HANDLE, 8))
        p.write(cmd + PLAN_SECT, struct.pack("<i", int(section)))
        p.write(cmd + PLAN_FROM, struct.pack("<q", int(frm)))
        p.write(cmd + PLAN_TO, struct.pack("<q", int(to)))
        if not (g.call(p.base + FN_PLAN_CANEXE, (cmd, 0),
                       absolute=True, force=True) & 0xFF):
            return False, "CanExecute false", {"command": cmd}
        before = {o["id"] for o in orders(g, og)}
        cmdwin.attach_fake_vtable(g, cmd, VT_COMPLETE_PLAN)
        posted = cmdwin.post_drained(g, cmd, seconds=2.0)
        oid = 0
        deadline = time.time() + 5.0
        while time.time() < deadline:
            fresh = [o for o in orders(g, og) if o["id"] not in before]
            if fresh:
                oid = fresh[-1]["id"]
                break
            time.sleep(0.15)
        info = {"command": cmd, "posted": posted, "order_id": oid,
                "section": int(section), "provinces": len(provs),
                "units_passed": len(valid_divs)}
        if not oid:
            info["note"] = ("the command was accepted but produced no order -- "
                            "the split computation found nothing on that front")
            return False, "no order appeared", info
        if assign and valid_divs:
            # Execute 自身已用 +0x30 调过 COrderAssignCommand；仅当实读仍为 0 时才兜底补指派
            cur_assigned = assigned_of(g, og, oid) or 0
            if cur_assigned <= 0:
                ok_a, err_a = assign_to_order(g, og, oid, valid_divs)
                info["assign_ok"] = bool(ok_a)
                info["assign_error"] = err_a
            else:
                info["assign_ok"] = True
                info["assign_error"] = ""
            info["assigned"] = assigned_of(g, og, oid)
        kids = [c for c in order_children(g, og) if c["root"] == oid]
        info["children"] = kids
        info["offensive"] = [c["id"] for c in kids if c["type"] == ORDER_TYPE_OFFENSIVE]
        return True, "", info
    finally:
        if own:
            g.detach()


FN_ORDER_FACTORY = _OW.ORDER_FN_FACTORY       # (COrdersGroup*, int type) -> new COrderInstance*
# 2026-11-23 (复测 10 A-2 反汇编实证): 0x10B4EC0(parent, child) 把 child 加入 parent+0x1F8
# (GetChildren) 并把 parent 加入 child+0x1E0 (GetParents)。旧版 manual_offensive 把两个参数
# 写反成了 (child, parent)，导致箭头倒挂进 root+0x1E0，引擎 C402A0/10AF270 永远查不到也删不掉！
FN_ORDER_SETPARENT = _OW.ORDER_FN_SETPARENT   # (parent, child) -> void  (AddChild / LinkOrder)
FN_ORDER_SETPATH = _OW.ORDER_FN_SETPATH       # (order, CPdxArray<u32>*, int) -- copies the province path
FN_ORDER_DETACH_ALL = _OW.ORDER_FN_DETACH_ALL  # (order) -> void  (从两端 +0x1E0/+0x1F8/+0x2B8/+0x2D0 安全解绑)


# --- the engine's own Execute, called directly (2026-11-19) ------------------
# `COrderNewFrontCommand::Execute` (0x18D9070) is NOT unreachable for our front
# orders -- the whole "it always bails in 0x10B8620" conclusion came from one
# misunderstanding of the 4th argument.  Full disassembly (see
# research/WINDOWS_1.19.5.md §106):
#
#   0x18D9137  cmp  byte [cmd+0x6e], 0
#   0x18D913c  sete r9b                      ; r9b = (cmd+0x6e == 0)
#   0x18D9140  r8  = rsi                     ; the root COrderInstance
#   0x18D9143  rdx = &[rbp-0x70]             ; the output CPdxArray<Split>
#   0x18D9147  rcx = &cmd+0x50               ; the province CPdxArray<i32>
#   0x18D914b  call 0x10B8620                ; compute the splits
#
# and inside 0x10B8620 the branch is
#
#   cmp  byte [root+0x298], 0
#   jne  path_B
#   test bpl, bpl             ; bpl = r9b = the flag we control
#   je   path_B               ; flag == 0 -> path B
#   ... path A: needs root+0x2a0 (section order-id array) -> empty for our
#       orders AND for the AI's own orders -> the historical failure
#
# path B (root+0x298 != 0 OR flag == 0) never touches +0x2a0: it filters the
# provinces we passed by province+0xb8 (vtable->slot0 call), +0x188 (count>0)
# and `group+0x3c == group+0x40` (which `0xC450a0(og)` answers), then calls
# `0x10B8A40(out, provinces, frontCtx, og)` which is the same splitter §43
# hooked live in 2026-11-14 and SAW succeed (out size 1).
#
# So: build the command with the REAL ctor with arg8 (the byte landing at
# cmd+0x6e) set to 1, and call Execute through the engine's own CanExecute +
# direct function call.  A positive `splits.size` then walks the exact
# chain the engine uses when the UI drags the arrow:
#
#   0x18D91BB  !AttachOrderSplits.IsEmpty() assert
#   0x18D921E  0xC41620(og) / 0xC41BB0(og)   -> the root order's "front unit"
#   0x18D9235  child+0x119 = cmd+0x6e
#   0x18D9243  0x10B0D90(that, &cmd+0x50, 0) -> copies the province path
#   0x18D9253  resolve(cmd+0x30) if set       -> r15 (secondary target)
#   0x18D92FB  0xC41720(og, 2)               -> type-2 section order per split
#   0x18D9352  0xC41720(og, 1)               -> the type-1 offensive child
#   0x18D9371  0x10B4EC0(child, section)     -> nest under the section order
#   0x18D944F  0x10BB5A0(section)           -> the split's frontCtx
#   0x18D949F  0x10B2C60(child, &out, 0, frontCtx) -> THE DIVISION SPLIT
#   0x18D960B  0x10B0D90(child, &divs, 1)   -> writes child+0x210 (!!)
#   0x18D9620  0x10CE800(child, 1)          -> sets executing=1
#
# `0x10B2C60` is the piece nothing else in this file ever calls: the engine's
# own "how do the assigned divisions distribute over the split segments".
FN_OFFENSIVE_CANEXE = _OW.ORDER_FN_CANEXE_OFFENSIVE     # (cmd, 0) -> bool  (vtable +0x48)
FN_OFFENSIVE_EXECUTE = _OW.ORDER_FN_OFFENSIVE_EXECUTE   # (cmd)    -> the real Execute

# 2026-11-19 live run: the ctor stores `cmd+0x6e = arg8 && *(u8*)0x33DDAB7`
# (disasm 0x18C99DC..0x18C99F5).  arg8=1 alone is NOT enough -- the global
# byte (BSS, zero-init, read nowhere else except one UI order-typing site at
# 0xFD472D) must ALSO be 1.  v1 wrote it around the ctor; v2 (§109) passes
# arg8=0 (the UI's own mode-3 bytes), so cmd+0x6e is 0 regardless and the
# gate stays untouched -- the constant is kept for documentation/future use.
FN_OFFENSIVE_GATE = _OW.ORDER_FN_OFFENSIVE_GATE

# §108.5 guard constants (统一来自 offsets_win 单一真源)
G_GAMESTATE  = _OW.G_GAMESTATE
GS_COUNTRIES = _OW.GS_COUNTRIES
# UI per-frame front-context refresh for one orders group (§108): the ONLY
# maintainer of the layer.  Experiment entry for tool-drawn groups.
FN_UI_FRONTCTX_REFRESH = _OW.ORDER_FN_UI_FRONTCTX_REFRESH


def _infer_root_target_provinces(g, root, og, ctx=0, enemy_tag=None, depth=1):
    """为前线根订单推导可被 0xD57D90 接受的敌方目标省列表。"""
    p = g.p
    if enemy_tag:
        try:
            cand = target_provinces(g, str(enemy_tag).upper(), depth=max(1, int(depth or 1)))
            if ctx:
                kept = filter_front_provinces(g, cand, ctx, og=og)
                if kept:
                    return kept[:64]
            if cand:
                return cand[:64]
        except Exception:
            pass
    # 从 root+0x70 己方边境省的相邻省中寻找同连通敌区的敌方省
    try:
        import mapdata
        data = p.u64(int(root) + 0x70)
        cnt = p.i32(int(root) + 0x7C)
        root_provs = ([p.i32(data + 4 * i) for i in range(cnt)]
                      if 0x10000 < data < 0x7F0000000000 and 0 < cnt < 512 else [])
        if root_provs:
            adj = mapdata.build_adjacency()
            owners, _ = mapdata.province_owner_map(g)
            me = str(g.player_tag() or "").upper()
            nbs = []
            seen = set()
            for rpid in root_provs:
                for nb in adj.get(rpid, ()):
                    if nb in seen:
                        continue
                    ow = owners.get(nb)
                    if not ow or ow == me:
                        continue
                    seen.add(nb)
                    if ctx:
                        try:
                            pa = p.u64(g.province_ptr(int(nb)) + 0xD0)
                            if not area_matches_front(p, ctx, pa, og=og):
                                continue
                        except Exception:
                            continue
                    nbs.append(int(nb))
            if nbs:
                return nbs[:64]
    except Exception:
        pass
    # 兜底：扫描所有与 root 同连通敌区（area_matches_front）的省
    if ctx:
        try:
            gs = p.u64(p.va(G_GAMESTATE))
            tbl = p.u64(gs + 0x2B0)
            n = p.i32(gs + 0x2BC)
            out = []
            if tbl and 0 < n <= 20000:
                for pid in range(n):
                    ptr = p.u64(tbl + 8 * pid)
                    if ptr > 0x10000 and area_matches_front(p, ctx, p.u64(ptr + 0xD0), og=og):
                        out.append(pid)
                        if len(out) >= 64:
                            break
            if out:
                return out
        except Exception:
            pass
    return []


def engine_offensive(g, og, root_id, provinces, divisions=None,
                     write_executing=True, enemy_tag=None, depth=1):
    """Create and run a REAL COrderNewFrontCommand (type-1 offensive arrow).

    2026-11-23 (复测 10 A-1 / A-3 根因修复):
      * COrderNewFrontCommand 的 `provinces` 参数（cmd+0x50）是**敌方目标省**，绝不是
        hoi4_border_fronts 返回的己方边境省！若传入己方边境省，0x10B8A40 -> 0xD57D90
        会把所有省滤空导致 Execute 静默退出不生成子订单。
      * 当传入的省经 area_matches_front (0xD57D90 复现) 全被滤空时：若指定了 enemy_tag，
        自动改用该敌国的目标省；否则直接返回明确报错拒绝静默退回空壳箭头。
      * Execute 产出原生 type-1 箭头（挂在 root+0x1F8）后，若 assigned 仍为 0，自动调用
        原生 assign_to_order(g, og, child_id, divisions) 补齐分师（与 CompletePlan::Execute
        0x18D4185 完全一致），并回读确认真实 assigned。
    """
    raw_provs = [int(x) for x in (provinces or [])]
    provs = list(raw_provs)
    p = g.p
    # §108 guard: the front-context layer must exist for the group's country.
    tag = p.i32(og + 0x3c)
    gs = p.u64(p.va(G_GAMESTATE))
    ctable = p.u64(gs + GS_COUNTRIES)
    cc = p.u64(ctable + 8 * tag) if ctable and 0 <= tag < 440 else 0
    fc_cnt = p.i32(cc + 0x174) if cc else 0
    if fc_cnt <= 0:
        return False, ("front-context layer empty (COUNTRIES[tag=%d]+0x168 "
                       "count=0): the engine treats the group as having no "
                       "drawn line (§108.4); draw the front via the UI on "
                       "this group, or run the 0xF711E0 refresh experiment "
                       "first" % tag), {}
    root = None
    for o in orders(g, og):
        if o["type"] == 2 and o["id"] == root_id:
            root = int(o["ptr"], 16)
            break
    if root is None:
        return False, "no type-2 order with id %s" % root_id, {}

    # 优先纯只读获取前线对应的敌方 CControllerArea* (CFront+0x70)，必要时回退 0x10BB5A0
    a_ptr = front_context_readonly(g, root)
    if not a_ptr:
        try:
            a_ptr = int(g.call(p.base + 0x10BB5A0, (root,), absolute=True,
                               force=True) or 0) & 0xFFFFFFFFFFFFFFFF
        except Exception:
            a_ptr = 0

    auto_replaced_note = None
    if provs and a_ptr:
        kept = filter_front_provinces(g, provs, a_ptr, og=og)
        if kept:
            provs = kept
        elif enemy_tag:
            inferred = _infer_root_target_provinces(
                g, root, og, ctx=a_ptr, enemy_tag=enemy_tag, depth=depth
            )
            if inferred:
                auto_replaced_note = (
                    "caller passed %d non-enemy/own-border province(s) rejected by 0xD57D90; "
                    "auto-replaced with %d enemy target province(s) of %s"
                    % (len(provs), len(inferred), str(enemy_tag).upper())
                )
                provs = inferred
            else:
                return False, (
                    "none of the %d passed provinces is in the enemy controller area of "
                    "front order %s (0x%X), and no target provinces found for %s -- pass "
                    "ENEMY target provinces (not our own border provinces)"
                    % (len(provs), root_id, a_ptr, enemy_tag)
                ), {"passed_provinces": raw_provs[:12], "front_area": hex(a_ptr)}
        else:
            return False, (
                "none of the %d passed provinces is in the enemy controller area of "
                "front order %s (0x%X) -- `provinces` must be ENEMY target provinces "
                "(not our own border provinces from hoi4_border_fronts); pass "
                "enemy_tag='HOL' or enemy province ids"
                % (len(provs), root_id, a_ptr)
            ), {"passed_provinces": raw_provs[:12], "front_area": hex(a_ptr)}

    if not provs:
        provs = _infer_root_target_provinces(
            g, root, og, ctx=a_ptr, enemy_tag=enemy_tag, depth=depth
        )
        if not provs:
            return False, ("no enemy target province matches the root front's "
                           "controller area (0x%x); pass enemy_tag or enemy target "
                           "province ids" % a_ptr), {}
        if len(provs) > 64:
            provs = provs[:64]
    if not _stores_vtable(g, FN_NEW_OFFENSIVE_CMD, VT_OFFENSIVE):
        return False, ("ctor 0x%X does not store the COrderNewFrontCommand "
                       "vtable 0x%X on this build -- stale image offset, refusing"
                       % (FN_NEW_OFFENSIVE_CMD, VT_OFFENSIVE)), {}
    own = g._inj is None
    if own:
        g.attach()
    try:
        # arg3: CPdxArray<i32> of province ids
        path = cmdwin.alloc(g, 4 * len(provs))
        p.write(path, b"".join(struct.pack("<i", x) for x in provs))
        pa = cmdwin.alloc(g, 0x18)
        p.write(pa, struct.pack("<QiiQ", path, len(provs), len(provs),
                                p.base + G_DEFAULT_ALLOC))
        # arg5: CPdxArray<CArmy*> of the divisions the ctor packs into cmd+0x38.
        if divisions is None:
            divisions = divisions_of(g, og)
        divisions, rejected = validate_division_ptrs(p, divisions)
        darr = _mkptrarray(g, divisions) if divisions else 0
        # NOTE: a 0-division cmd is refused below; keep the gate BEFORE build.
        if not divisions:
            return False, _format_no_valid_divisions(rejected, "engine_offensive"), {
                "rejected": rejected[:10],
            }
        before = {c["id"] for c in order_children(g, og)}
        cmd = cmdwin.alloc(g, 0x70)
        p.write(cmd, b"\0" * 0x70)
        # NINE arguments, replicating the UI's offensive drag (mode 3, call
        # site 0xFD5EED): arg7=1 -> cmd+0x6d, arg8=0 -> cmd+0x6e (= arg8 && G,
        # so the G gate stays untouched on this path), arg9=0 -> cmd+0x30
        # keeps the ctor's default handle. Execute's flag = (cmd+0x6e == 0) = 1
        # -> split path A (0x10BB5A0 frontCtx -> 0x10B8A40 -> 0xD57D90).
        g.call(p.base + FN_NEW_OFFENSIVE_CMD,
               (cmd, og, pa, root, darr, 0, 1, 0, 0),
               absolute=True, force=True)
        if p.u64(cmd) != p.base + VT_OFFENSIVE:
            return False, ("ctor wrote vptr %#x, expected 0x%X"
                           % (p.u64(cmd), p.base + VT_OFFENSIVE)), {}
        if not p.i32(cmd + 0x68):
            return False, ("the ctor left cmd+0x68 (parent order id) at 0 -- "
                           "arg4 was not accepted as the parent order"), {}
        if p.u8(cmd + 0x6d) != 1 or p.u8(cmd + 0x6e) != 0:
            return False, ("cmd+0x6d=%d cmd+0x6e=%d, expected 1/0 -- the "
                           "mode-3 bytes were not stored"
                           % (p.u8(cmd + 0x6d), p.u8(cmd + 0x6e))), {}
        if not g.call(p.base + FN_OFFENSIVE_CANEXE, (cmd, 0),
                      absolute=True, force=True) & 0xFF:
            return False, "CanExecute false", {}
        # Execute directly.  The object was built by the real ctor and every
        # virtual it may run mid-flight is real; afterwards we swap in the
        # no-free vtable so nothing can CRT-free it.
        g.call(p.base + FN_OFFENSIVE_EXECUTE, (cmd,),
               absolute=True, force=True)
        cmdwin.attach_fake_vtable(g, cmd, VT_OFFENSIVE)
        # Read back what the engine produced.
        deadline = time.time() + 4.0
        fresh = []
        while time.time() < deadline:
            fresh = [c for c in order_children(g, og)
                     if c["id"] not in before
                     and c["type"] == ORDER_TYPE_OFFENSIVE
                     and not c.get("inverted_parent_link")]
            if fresh:
                break
            time.sleep(0.1)
        info = {"command": hex(cmd), "children": fresh,
                "target_provinces": provs[:20],
                "children_ids": [c["id"] for c in fresh],
                "children_executing": [c["id"] for c in fresh
                                       if c.get("executing") == 1]}
        if auto_replaced_note:
            info["auto_replaced_provinces"] = auto_replaced_note
        if not fresh:
            info["note"] = ("Execute ran but produced no type-1 child; "
                            "verify the target provinces are reachable from this front order")
            return False, "Execute produced no offensive child", info
        # 若原生 type-1 箭头刚生成时 assigned == 0，按 CompletePlan::Execute (0x18D4185)
        # 做法通过 COrderAssignCommand 把师直接挂到该 type-1 子命令 ID 上（它已在 root+0x1F8 内，
        # C402A0 可直接定位）
        if not any((c.get("assigned") or 0) > 0 for c in fresh) and divisions:
            kid_id = int(fresh[-1]["id"])
            a_ok, a_err = assign_to_order(g, og, kid_id, divisions)
            info["child_assign"] = {"order_id": kid_id, "ok": bool(a_ok), "error": a_err}
            t_asg = time.time() + 2.0
            while time.time() < t_asg:
                fresh = [c for c in order_children(g, og)
                         if c["id"] in {x["id"] for x in fresh}]
                if any((c.get("assigned") or 0) > 0 for c in fresh):
                    break
                time.sleep(0.1)
        if write_executing:
            for c in fresh:
                if c.get("executing") != 1:
                    set_order_executing(g, c["ptr"], True)
        all_kids = order_children(g, og)
        info["children"] = all_kids
        info["children_ids"] = [c["id"] for c in all_kids]
        info["children_fed"] = [c["id"] for c in all_kids
                                if c.get("root") == root_id and (c.get("assigned") or 0) > 0]
        info["children_executing"] = [c["id"] for c in all_kids if c.get("executing") == 1]
        return True, "", info
    finally:
        if own:
            g.detach()


def manual_offensive(g, og, root_id, provinces, executing=True):
    """Build a type-1 order with the engine's own order factory (legacy fallback).

    2026-11-23 (复测 10 A-2 根治):
      * 修正 0x10B4EC0(parent, child) 参数顺序：第一参数是 parent (root)，第二参数是
        child (o)！旧版传反成 (o, root)，导致子命令倒挂在 root+0x1E0，引擎 C402A0 永远
        找不到也删不掉！
      * 注意：此路径仅建立单层节点、不含引擎 3 层目标线与师分配，正常进攻请走
        engine_offensive 或 complete_plan。
    """
    p = g.p
    root = None
    for o in orders(g, og):
        if o["id"] == root_id:
            root = int(o["ptr"], 16)
            break
    if root is None:
        return False, "no order with id %s" % root_id, 0
    provs = [int(x) for x in (provinces or [])]
    if not provs:
        return False, "no provinces", 0
    own = g._inj is None
    if own:
        g.attach()
    try:
        o = g.call(p.base + FN_ORDER_FACTORY, (og, 1), absolute=True, force=True)
        if not o or p.u64(o) != p.base + VT_ORDER_INSTANCE:
            return False, ("order factory returned %r (vptr %r)"
                           % (o, p.u64(o) if o else None)), 0
        # 正确顺序: 0x10B4EC0(parent=root, child=o) -> 把 o 放进 root+0x1F8 (Children)
        g.call(p.base + FN_ORDER_SETPARENT, (root, o), absolute=True, force=True)
        pa = _mkintarray(g, provs)
        g.call(p.base + FN_ORDER_SETPATH, (o, pa, 1), absolute=True, force=True)
        if executing:
            p.write(o + OI_EXECUTING, b"\x01")
        return True, "", o
    finally:
        if own:
            g.detach()


# 2026-11-23 (复测 8 B-14 实机抓现行): order+0x210 数组中存放的元素是 CArmy 内部偏移
# +0xB8 的多重继承子对象指针（实测 0x15b0fa91998 - 0x15b0fa918e0 == 0xB8），而非 CArmy 首址！
ORDER_DIV_SUBOBJ_OFF = _OW.ORDER_DIV_SUBOBJ_OFF


def assigned_breakdown(g, og, order_id):
    """精确区分「本集团军指派到该订单的师数」与「引擎 order+0x210 挂的总师数」。

    2026-11-23 (复测 7 B-6 & 复测 8 B-14):
      1. 多军共享前线/元帅集团军群时，order+0x210 会包含同前线其它军的师；
      2. 真机实测 order+0x210 元素为 CArmy* + 0xB8（COrderable 子对象指针），因此匹配
         本军 divisions_of(g, og) 时须同时接受 `ptr in og_divs` 与 `(ptr - 0xB8) in og_divs`，
         防止交集恒为 0 导致把正常前线误判为空订单删掉。
    """
    p = g.p
    og_divs = set()
    try:
        og_divs = {int(d) for d in (divisions_of(g, og) or ())}
    except Exception:
        og_divs = set()
    for o in orders(g, og):
        if o["id"] == order_id:
            optr = int(o["ptr"], 16)
            total = int(p.i32(optr + 0x210 + 0xC))
            army_n = total
            if 0 < total <= 2000:
                try:
                    data = p.u64(optr + 0x210)
                    if 0x10000 < data < 0x7F0000000000:
                        ord_raw = struct.unpack("<%dQ" % total, p.read(data, 8 * total))
                        if og_divs:
                            matched = set()
                            for x in ord_raw:
                                if x in og_divs:
                                    matched.add(x)
                                elif (x - ORDER_DIV_SUBOBJ_OFF) in og_divs:
                                    matched.add(x - ORDER_DIV_SUBOBJ_OFF)
                            army_n = len(matched)
                        else:
                            army_n = len(set(ord_raw))
                except Exception:
                    if og_divs:
                        army_n = min(total, len(og_divs))
            elif og_divs and total > len(og_divs):
                army_n = len(og_divs)
            return {
                "army_assigned": int(army_n),
                "front_total_assigned": int(total),
                "army_divisions": len(og_divs),
            }
    return None


def assigned_of(g, og, order_id):
    """How many divisions of THIS army are put on this order (`order+0x210` ∩ `og+0x50`)."""
    br = assigned_breakdown(g, og, order_id)
    if br is None:
        return None
    return br["army_assigned"]


# --- COrderAssignCommand (PE) ---------------------------------------------
# Recovered 2026-11-17 fully offline, without touching the running game:
#   research/pe_rtti.py  ".?AVCOrderAssignCommand@@" -> vtable 0x2A84CE0
#   (the same resolver returns 0x2A26EF8 with slot 9 = 0x1C2DBF0 for
#    CCreateDivisionTemplateCommand, both of which were confirmed live first,
#    so the method is validated)
#   slot  9 = 0x18DE970   CanExecute
#   slot 10 = 0x18D4220   Execute
#
# CanExecute:
#     [cmd+0x34] == 0                       -> "no divisions" and FALSE
#     for each packed handle in cmd+0x28:   resolve() != NULL and
#                                           [[obj+0xB0]]+0x1B0 == 0
#     return IsValidHandle(&cmd+0x40)       <-- the COrdersGroup handle
# The order id at +0x48 is NOT validated by CanExecute.
#
# Execute:
#     og    = resolve(cmd+0x40)
#     order = 0x140C402A0(og, [cmd+0x48]); falls back to the parent group
#             at [og+0x1B8] when the id is not found on this group
#
# The Linux numbers in armyops.py (ctor 0x231D060 / CanExecute 0x21ED460) match
# nothing on hoi4.exe, and the ctor 0x231D0D0 the notes carried disassembles to
# ZERO instructions -- it is not a function entry at all.  Nothing here calls a
# ctor: the object is built from the vtable with build_by_vt.
VT_ORDER_ASSIGN = _OW.ORDER_VT_ASSIGN
CANEXEC_ORDER_ASSIGN = _OW.ORDER_CANEXEC_ASSIGN
SIZE_ORDER_ASSIGN = _OW.ORDER_SIZE_ASSIGN


def assign_to_order(g, orders_group, order_id, divisions):
    """Put divisions on a battle-plan order (or into an existing army when order_id=0)."""
    p = g.p
    divs, rejected = validate_division_ptrs(p, divisions)
    g._last_division_rejected = rejected
    if not divs:
        return False, _format_no_valid_divisions(rejected, "assign_to_order")
    own = g._inj is None
    if own:
        g.attach()
    try:
        handle = p.read(int(orders_group) + OG_HANDLE, 8)
        if handle == b"\0" * 8:
            return False, "orders group has an empty handle"
        arr = _mkhandlearray(g, divs)
        cmd = cmdwin.build_by_vt(g, VT_ORDER_ASSIGN, SIZE_ORDER_ASSIGN, [
            (0x28, "<Q", p.u64(arr)),          # array data
            (0x30, "<i", p.i32(arr + 0x08)),   # array capacity
            (0x34, "<i", p.i32(arr + 0x0C)),   # array size (count)
            (0x38, "<Q", p.u64(arr + 0x10)),   # allocator
            (0x40, "<8s", handle),             # COrdersGroup handle
            (0x48, "<i", int(order_id)),
        ])
        if not cmdwin.can_exec(g, cmd, CANEXEC_ORDER_ASSIGN):
            return False, "COrderAssignCommand::CanExecute refused"
        r = cmdwin.post_drained(g, cmd)
        if not r.get("posted"):
            return False, "Post rejected"
        return True, ""
    finally:
        if own:
            g.detach()


def add_divisions_to_army(g, orders_group, divisions, order_id=None):
    """将指定师（未编组或来自其他集团军）原生并入已存在的集团军 `orders_group`。

    2026-11-23 (C-3): 经 PE 反汇编 `COrderAssignCommand::Execute (0x18D4220)` 证实：
    该命令本身就是引擎官方的“把任意师并入现有 COrdersGroup”路径（0x18D4AD8 普通军分支 ->
    0x18D4D81 遍历师列表 -> 校验 CArmy+0xC0 -> 调用 AddArmy 并入目标军并清理变空的旧军）。
      * 若目标军已有前线订单且未指定 order_id，自动传首个 type-2 前线 id（一步并入且上前线）；
      * 若目标军尚无订单，传 order_id=0（纯并入该集团军）；
      * 提交后逐师回读 `CArmy+0xC0 == orders_group` 核验真实结果。
    """
    p = g.p
    og = int(orders_group, 16) if isinstance(orders_group, str) else int(orders_group or 0)
    if not og:
        return {"ok": False, "error": "invalid orders_group pointer"}
    try:
        if p.u8(og + OG_IS_GROUP):
            return {"ok": False, "error": "target orders_group is an army group (field marshal), not an army"}
    except Exception as e:
        return {"ok": False, "error": "cannot read target orders_group: %s" % e}

    valid, rejected = validate_division_ptrs(p, divisions)
    if not valid:
        return {
            "ok": False,
            "error": _format_no_valid_divisions(rejected, "add_divisions_to_army"),
            "rejected": rejected[:10],
        }

    already = [d for d in valid if p.u64(d + CARMY_OG) == og]
    todo = [d for d in valid if p.u64(d + CARMY_OG) != og]
    before_n = len(divisions_of(g, og))
    if not todo:
        return {
            "ok": True,
            "orders_group": hex(og),
            "requested": len(valid),
            "added": 0,
            "already_in_army": len(already),
            "divisions_before": before_n,
            "divisions_after": before_n,
            "order_id": order_id,
            "positions_moved": False,
            "position_note": (
                "division ownership unchanged and map positions were NOT moved; "
                "pass move_to_province (or call hoi4_move_divisions with spread=true) to relocate"
            ),
            "transferred_divisions": [hex(d) for d in already],
        }

    eff_oid = 0
    if order_id is not None:
        eff_oid = int(order_id)
    else:
        try:
            roots = [o for o in orders(g, og) if o.get("type") == 2]
            if roots:
                eff_oid = int(roots[0]["id"])
        except Exception:
            eff_oid = 0

    ok, err = assign_to_order(g, og, eff_oid, todo)
    # 实读每个师的 CArmy+0xC0 核验真正落地数量
    landed = []
    still_out = []
    deadline = time.time() + 2.0
    while True:
        landed = [d for d in todo if p.u64(d + CARMY_OG) == og]
        still_out = [d for d in todo if p.u64(d + CARMY_OG) != og]
        if len(landed) == len(todo) or not ok or time.time() >= deadline:
            break
        time.sleep(0.1)
    after_n = len(divisions_of(g, og))
    pos_note = (
        "attached to target army's front order %d (engine will walk them to that front line when unpaused); "
        "map positions were NOT moved immediately by this call" % eff_oid
        if eff_oid
        else (
            "only army ownership changed (order_id=0); divisions stay in their current provinces and still draw "
            "local supply -- pass move_to_province (or call hoi4_move_divisions with spread=true) to relocate them"
        )
    )
    return {
        "ok": bool(ok and len(landed) > 0),
        "error": err if not ok else ("" if not still_out else "%d of %d division(s) did not attach" % (len(still_out), len(todo))),
        "orders_group": hex(og),
        "order_id": eff_oid,
        "requested": len(valid),
        "added": len(landed),
        "already_in_army": len(already),
        "failed": len(still_out),
        "divisions_before": before_n,
        "divisions_after": after_n,
        "positions_moved": False,
        "position_note": pos_note,
        "transferred_divisions": [hex(d) for d in landed],
    }


def execute_plan(g, og, order_id=0, flag=0):
    """The green tick: COrderExecuteCommand toggles execution on one order or whole army.

    2026-11-23 (复测 11 P0-2 PE 反汇编实证):
      `COrderExecuteCommand::Execute` (0x18D64A0):
        rsi = resolve(cmd+0x28)       ; COrdersGroup*
        edx = [cmd+0x30]              ; order_id
        if (edx != 0):
            order = 0xC402A0(rsi, edx)  ; 递归搜索 +0x1F8 / +0x2D0，支持 type-2 根与 type-1 子箭头
            0x10CD510(order, [cmd+0x34], 1)
        else:
            0x18E5D50(rsi, [cmd+0x34])  ; 整军全部前线与进攻线开关！
      `COrderInstance::SetExecuting` (0x10CD510):
        * flag=1: 设本节点 +0x248=1，向上把 parent(+0x1E0) 设 1，若 type==1 把目标线设 1；
        * flag=0: 设本节点 +0x248=0，向下把 children(+0x1F8) 全部设 0（传 type-1 子订单 ID
          时精准关闭该进攻箭头及其目标线，完全不动父前线订单与师！）。
    """
    p = g.p
    own = g._inj is None
    if own:
        g.attach()
    try:
        cmd = cmdwin.alloc(g, OE_SIZE)
        p.write(cmd, b"\0" * OE_SIZE)
        g.call(p.base + OE_CTOR, (cmd, og, int(order_id), int(flag), 0),
               absolute=True, force=True)
        if p.u64(cmd) != p.base + OE_VT:
            return False, "ctor wrote no vtable", 0
        # never let Post's rejection path run the real deleting destructor
        cmdwin.attach_fake_vtable(g, cmd, OE_VT)
        pr = cmdwin.post_drained(g, cmd, seconds=2.0)
        if not (pr.get("posted") if isinstance(pr, dict) else pr):
            return False, "Post rejected", cmd
        return True, "", cmd
    finally:
        if own:
            g.detach()


def set_plan_executing(g, og, order_id=0, executing=True):
    """精准开启或关闭单条 type-1 进攻线、单条 type-2 前线子树、或整军全部进攻线（P0-2）。

    返回结构化核验结果（含 `scope`、`executing`、`children_after`、`root_intact`），
    关停单条 type-1 箭头时绝不删除父前线订单、绝不改动分配的师。
    """
    og_int = int(og, 16) if isinstance(og, str) else int(og or 0)
    if not og_int:
        return {"ok": False, "error": "invalid orders_group pointer"}
    oid = int(order_id or 0)
    want = 1 if executing else 0

    roots_before = orders(g, og_int) or []
    kids_before = [k for k in (order_children(g, og_int) or []) if not k.get("foreign_group")]
    kid_by_id = {int(k["id"]): k for k in kids_before}
    root_by_id = {int(r["id"]): r for r in roots_before}

    if oid != 0:
        if oid in kid_by_id:
            scope = "child"
            target_ptrs = [kid_by_id[oid]["ptr"]]
        elif oid in root_by_id:
            scope = "root"
            target_ptrs = [int(root_by_id[oid]["ptr"], 16)] + [
                k["ptr"] for k in kids_before if int(k.get("root") or 0) == oid
            ]
        else:
            return {
                "ok": False,
                "error": "order %d not found on orders_group %s" % (oid, hex(og_int)),
                "order_id": oid,
                "orders_group": hex(og_int),
                "known_roots": sorted(root_by_id.keys()),
                "known_children": sorted(kid_by_id.keys()),
            }
    else:
        scope = "army"
        target_ptrs = [k["ptr"] for k in kids_before]

    # 1) 优先走引擎原生 COrderExecuteCommand (0x18D64A0)
    # 注意：order_id=0 + flag=1 时历史上单前线需传 root_id 以确保触发分师/启攻，
    # 而 flag=0（全军转守）走 order_id=0 原生 0x18E5D50(og, 0) 可一次关掉全军所有前线与箭头。
    cmd_oid = oid
    if cmd_oid == 0 and want == 1:
        type2_roots = [r for r in roots_before if r.get("type") == 2]
        if type2_roots:
            cmd_oid = int(type2_roots[-1]["id"])

    ok, err, _cmd = execute_plan(g, og_int, cmd_oid, want)

    # 2) 回读核验目标订单的 executing 字节；若因暂停态/引擎未向下传播残留旧值，
    #    通过引擎原生 0x10CD510(ptr, want, 1) 安全同步并再次回读。
    def _read_state():
        kids_now = [k for k in (order_children(g, og_int) or []) if not k.get("foreign_group")]
        roots_now = orders(g, og_int) or []
        return roots_now, kids_now

    def _matches(kids_now):
        by_id = {int(k["id"]): k for k in kids_now}
        if scope == "child":
            row = by_id.get(oid)
            return bool(row is not None and int(row.get("executing") or 0) == want)
        if scope == "root":
            mine = [k for k in kids_now if int(k.get("root") or 0) == oid]
            if not mine:
                return bool(ok)
            return all(int(k.get("executing") or 0) == want for k in mine)
        # scope == "army"
        if not kids_now:
            return bool(ok)
        if want == 0:
            return all(int(k.get("executing") or 0) == 0 for k in kids_now)
        return any(int(k.get("executing") or 0) == 1 for k in kids_now)

    roots_after, kids_after = _read_state()
    direct_sync = False
    if ok and not _matches(kids_after) and target_ptrs:
        own = g._inj is None
        if own:
            g.attach()
        try:
            for ptr in target_ptrs:
                try:
                    set_order_executing(g, int(ptr), bool(want))
                    direct_sync = True
                except Exception as _e:
                    import diag as _diag
                    _diag.warn("armyops_win.direct_sync_executing", _e, detail="ptr=%#x" % int(ptr))
        finally:
            if own:
                g.detach()
        roots_after, kids_after = _read_state()

    verified = _matches(kids_after)
    assigned_val = None
    if scope == "child":
        cur_k = next((k for k in kids_after if int(k["id"]) == oid), None)
        assigned_val = cur_k.get("assigned") if cur_k else None
    elif cmd_oid:
        assigned_val = assigned_of(g, og_int, cmd_oid)

    out = {
        "ok": bool(ok and verified),
        "error": err if not ok else ("" if verified else "executing byte did not reach target=%d" % want),
        "orders_group": hex(og_int),
        "order_id": cmd_oid if scope == "army" and want == 1 else oid,
        "scope": scope,
        "executing": bool(want),
        "flag": want,
        "assigned": assigned_val,
        "root_count_before": len(roots_before),
        "root_count_after": len(roots_after),
        "root_intact": len(roots_after) >= len(roots_before),
        "children_after": [(k["id"], k["assigned"], k["executing"]) for k in kids_after],
        "children_executing": [k["id"] for k in kids_after if k.get("executing") == 1],
    }
    if direct_sync:
        out["direct_sync"] = True
    return out


# --- COrderDeleteCommand (PE 1.19.5.0) ------------------------------------
# 2026-11-23 (复测 9 E-1 根治):
#   旧实现走 calling/armyops.py 的 Linux 路径：用 g.malloc 分配对象、不做
#   cmdwin.attach_fake_vtable、不做 CanExecute、也不先清理 type-1 子命令。
#   引擎 CCommandQueue 销毁命令时调真实 deleting destructor -> CRT free 非法堆指针，
#   或者删父订单时遗留 executing=1 / assigned=0 的子命令，导致下一 tick 崩进程。
#   已通过 pedis.py 核实 1.19.5.0 偏移：
#     ctor       = 0x18C89D0  (this, COrdersGroup*, i32 order_id)
#     vtable     = 0x2A84C20  (COrderDeleteCommand)
#     CanExecute = 0x18DEF50  (slot 9)
#     Execute    = 0x18D6050  (slot 10)
FN_ORDER_DELETE_CTOR = _OW.ORDER_FN_DELETE_CTOR
VT_ORDER_DELETE      = _OW.ORDER_VT_DELETE
CANEXEC_ORDER_DELETE = _OW.ORDER_CANEXEC_DELETE
SIZE_ORDER_DELETE    = _OW.ORDER_SIZE_DELETE

try:
    import profile as _aw_prof_del
    if _aw_prof_del.load():
        VT_ORDER_DELETE = _aw_prof_del.command_field("COrderDeleteCommand", "vt", VT_ORDER_DELETE)
        CANEXEC_ORDER_DELETE = _aw_prof_del.command_field("COrderDeleteCommand", "can_execute", CANEXEC_ORDER_DELETE)
        SIZE_ORDER_DELETE = max(SIZE_ORDER_DELETE, int(_aw_prof_del.command_field("COrderDeleteCommand", "size", SIZE_ORDER_DELETE)))
except Exception as _e:
    import diag as _diag
    _diag.warn("armyops_win.apply_profile.delete", _e)


def _post_delete_order_cmd(g, og, order_id):
    """构造并安全提交一条 COrderDeleteCommand（alloc + CanExecute + fake_vtable + post_drained）。"""
    p = g.p
    oid = int(order_id)
    own = g._inj is None
    if own:
        g.attach()
    try:
        cmd = cmdwin.alloc(g, SIZE_ORDER_DELETE)
        p.write(cmd, b"\0" * SIZE_ORDER_DELETE)
        g.call(p.base + FN_ORDER_DELETE_CTOR, (cmd, int(og), oid),
               absolute=True, force=True)
        vt = p.u64(cmd)
        if vt != p.base + VT_ORDER_DELETE:
            return False, "COrderDeleteCommand ctor wrote vptr %#x, expected %#x" % (vt, p.base + VT_ORDER_DELETE)
        got_oid = p.i32(cmd + 0x30)
        if got_oid != oid:
            return False, "COrderDeleteCommand order_id mismatch (+0x30=%d, want %d)" % (got_oid, oid)
        if not (g.call(p.base + CANEXEC_ORDER_DELETE, (cmd, 0),
                       absolute=True, force=True) & 0xFF):
            return False, "COrderDeleteCommand::CanExecute refused (no such order %d)" % oid
        # 绝不让队列释放路径对游戏分配器对象调用真实 CRT deleting destructor
        cmdwin.attach_fake_vtable(g, cmd, VT_ORDER_DELETE)
        pr = cmdwin.post_drained(g, cmd, seconds=2.0)
        if not (pr.get("posted") if isinstance(pr, dict) else pr):
            return False, "Post rejected COrderDeleteCommand"
        return True, ""
    finally:
        if own:
            g.detach()


def _detach_inverted_orphan_child(g, og, child_row):
    """安全解绑旧版 manual_offensive 倒挂在 root+0x1E0 中的 type-1 孤儿子命令（复测 10 A-2）。

    仅在同时满足以下全部严格不变量时触发：
      1. child_row 指针有效且虚表 == VT_ORDER_INSTANCE；
      2. child 类型为 1 (ORDER_TYPE_OFFENSIVE) 且 assigned == 0；
      3. child 确实处于倒挂态：位于 root+0x1E0 (Parents) 且不在 root+0x1F8 (Children)，
         因此引擎 COrdersGroup::FindOrder (0xC402A0 -> 0x10AF270) 永远无法按 id 查到它；
      4. 先清 executing=0，再调用引擎原生 COrderInstance::DetachAll (0x10B5D50) 安全
         断开双向数组链接（+0x1E0/+0x1F8/+0x2B8/+0x2D0），不贸然调 CRT delete 避开跨堆释放风险。
    """
    p = g.p
    cptr = int(child_row.get("ptr") or 0)
    rptr = int(child_row.get("root_ptr") or 0)
    if not cptr or not rptr:
        return False, "missing child/root pointer for inverted orphan detach"
    try:
        if p.u64(cptr) - p.base != VT_ORDER_INSTANCE:
            return False, "child vptr mismatch"
        if p.i32(cptr + OI_TYPE) != ORDER_TYPE_OFFENSIVE:
            return False, "child is not type-1 offensive order"
        if p.i32(cptr + OI_ASSIGNED + 0xC) != 0:
            return False, "refusing to detach non-empty inverted child (assigned > 0)"
        in_real = (
            cptr in _child_array(p, rptr, 0x1F8, 0x204)
            or cptr in _child_array(p, rptr, 0x2D0, 0x2DC)
        )
        in_inv = cptr in _child_array(p, rptr, 0x1E0, 0x1EC)
        if in_real or not in_inv:
            return False, "child is not an inverted +0x1E0 orphan"
    except Exception as e:
        return False, "failed to verify inverted orphan: %s" % e

    own = g._inj is None
    if own:
        g.attach()
    try:
        try:
            p.write(cptr + OI_EXECUTING, b"\x00")
        except Exception as _e:
            import diag as _diag
            _diag.warn("armyops_win.detach_orphan.clear_exec", _e, detail="cptr=%#x" % int(cptr))
        g.call(p.base + FN_ORDER_DETACH_ALL, (cptr,), absolute=True, force=True)
    finally:
        if own:
            g.detach()

    # 回读确认已从 root+0x1E0 彻底解绑
    try:
        still_inv = cptr in _child_array(p, rptr, 0x1E0, 0x1EC)
    except Exception:
        still_inv = False
    if still_inv:
        return False, "0x10B5D50 returned but child still linked in root+0x1E0"
    return True, ""


def delete_order(g, orders_group, order_id, allow_child=False):
    """安全删除集团军的一条战斗计划订单（PE 1.19.5.0 原生路径，E-1 & A-2 根治）。

    关键保障：
      1. 全部命令均经 cmdwin.alloc + CanExecute + attach_fake_vtable + post_drained，
         杜绝 g.malloc 无 fake_vtable 被 CRT free 炸堆；
      2. 若删除的是 type-2 父前线订单且挂着 type-1 子命令：先将子/父订单 executing 归 0，
         再逐个清理全部子命令并回读确认清空；
      3. 若子命令是旧版 manual_offensive 倒挂进 root+0x1E0 的 0 师 type-1 孤儿（引擎
         C402A0 只扫 +0x1F8 导致 CanExecute 报 no such order），自动通过引擎原生
         0x10B5D50 (DetachAll) 安全解绑，使父订单彻底恢复可删除；
      4. 单独删一条 type-1 子命令且 allow_child=False 时保持 child_delete_unsafe 防护。
    """
    og = int(orders_group, 16) if isinstance(orders_group, str) else int(orders_group or 0)
    oid = int(order_id)
    if not og:
        return {"ok": False, "error": "invalid orders_group pointer", "order_id": oid}

    cur_orders = []
    cur_kids = []
    try:
        cur_orders = orders(g, og) or []
    except Exception:
        cur_orders = []
    try:
        cur_kids = order_children(g, og) or []
    except Exception:
        cur_kids = []

    kid_by_id = {int(k["id"]): k for k in cur_kids if k.get("id") is not None}
    kid_ids = set(kid_by_id.keys())
    if oid in kid_ids and not allow_child:
        return {
            "ok": False,
            "order_id": oid,
            "blocked_by": "child_delete_unsafe",
            "error": (
                "deleting a type-1 child on the PE build wipes the parent order's "
                "ENTIRE child array (measured 2026-11-19: 3 arrows -> 0). Delete "
                "the parent front order instead (which safely cleans its children "
                "first), or call hoi4_reset_battleplans."
            ),
        }

    def _to_ptr_int(v):
        return int(v, 16) if isinstance(v, str) else int(v)

    # 如果调用方明确要求删一个旧版倒挂孤儿 type-1（allow_child=True），先试原生命令，失败则安全解绑
    if oid in kid_ids and allow_child:
        k_self = kid_by_id[oid]
        if k_self.get("ptr"):
            try:
                set_order_executing(g, _to_ptr_int(k_self["ptr"]), False)
            except Exception as _e:
                import diag as _diag
                _diag.warn("armyops_win.delete_order.stop_self_child", _e, detail="ptr=%r" % k_self.get("ptr"))
        if k_self.get("inverted_parent_link"):
            ok_d, err_d = _detach_inverted_orphan_child(g, og, k_self)
            rem_orders = orders(g, og) or []
            rem_kids = order_children(g, og) or []
            return {
                "ok": bool(ok_d),
                "error": err_d,
                "order_id": oid,
                "orders_group": hex(og),
                "detached_inverted_orphan": bool(ok_d),
                "deleted_children": [oid] if ok_d else [],
                "remaining_orders": len(rem_orders),
                "remaining_children": len(rem_kids),
            }

    # 查找挂在当前父订单下的所有本军自有子命令（排除跨军共享引用）
    owned_kids = [
        k for k in cur_kids
        if int(k.get("root") or 0) == oid and not k.get("foreign_group")
    ]
    # 先把相关子命令与父订单本身的 executing 字节置 0，防止删除间隙被模拟线程调度
    for k in owned_kids:
        if k.get("ptr"):
            try:
                set_order_executing(g, _to_ptr_int(k["ptr"]), False)
            except Exception as _e:
                import diag as _diag
                _diag.warn("armyops_win.delete_order.stop_child", _e, detail="ptr=%r" % k.get("ptr"))
    for o in cur_orders:
        if int(o.get("id") or -1) == oid and o.get("ptr"):
            try:
                set_order_executing(g, _to_ptr_int(o["ptr"]), False)
            except Exception as _e:
                import diag as _diag
                _diag.warn("armyops_win.delete_order.stop_root", _e, detail="ptr=%r" % o.get("ptr"))

    deleted_children = []
    detached_orphans = []
    child_errors = []
    for k in owned_kids:
        kid_id = int(k["id"])
        # 删前一个子命令时引擎可能已顺带清空同父所有子命令，先回读避免无谓失败
        try:
            still_present = any(
                int(x["id"]) == kid_id and not x.get("foreign_group")
                for x in (order_children(g, og) or [])
            )
        except Exception:
            still_present = True
        if not still_present:
            deleted_children.append(kid_id)
            continue
        if k.get("inverted_parent_link"):
            ok_d, err_d = _detach_inverted_orphan_child(g, og, k)
            if ok_d:
                deleted_children.append(kid_id)
                detached_orphans.append(kid_id)
                continue
            child_errors.append({
                "order_id": kid_id,
                "source_offset": k.get("source_offset"),
                "inverted_parent_link": True,
                "assigned": k.get("assigned"),
                "error": err_d,
            })
            continue
        ok_k, err_k = _post_delete_order_cmd(g, og, kid_id)
        if ok_k:
            deleted_children.append(kid_id)
        else:
            # 若 CanExecute 报 no such order 且实际属于倒挂孤儿，兜底安全解绑
            err_d = ""
            if "no such order" in str(err_k or "").lower():
                ok_d, err_d = _detach_inverted_orphan_child(g, og, k)
                if ok_d:
                    deleted_children.append(kid_id)
                    detached_orphans.append(kid_id)
                    continue
            ce = {
                "order_id": kid_id,
                "source_offset": k.get("source_offset"),
                "assigned": k.get("assigned"),
                "error": err_k,
            }
            if err_d:
                ce["detach_error"] = err_d
            child_errors.append(ce)

    if owned_kids:
        # 轮询确认该父订单下的所有本军自有子命令已真正消失
        deadline_c = time.time() + 2.0
        left_owned = []
        while True:
            try:
                left_owned = [
                    int(x["id"]) for x in (order_children(g, og) or [])
                    if int(x.get("root") or 0) == oid and not x.get("foreign_group")
                ]
            except Exception:
                left_owned = []
            if not left_owned or time.time() >= deadline_c:
                break
            time.sleep(0.1)
        if left_owned:
            return {
                "ok": False,
                "order_id": oid,
                "blocked_by": "child_delete_failed",
                "deleted_children": deleted_children,
                "detached_orphans": detached_orphans,
                "remaining_owned_children": left_owned,
                "child_errors": child_errors,
                "error": (
                    "refusing to delete parent order %d: child order(s) %s could not "
                    "be removed first (would leave orphan type-1 children and risk crash)"
                    % (oid, left_owned)
                ),
            }

    # 若删的本来就是子命令且上面步骤尚未删掉，直接删 oid
    ok_p, err_p = _post_delete_order_cmd(g, og, oid)
    if not ok_p:
        return {
            "ok": False,
            "order_id": oid,
            "error": err_p,
            "deleted_children": deleted_children,
            "detached_orphans": detached_orphans,
        }

    # 回读确认目标订单已从内存中消失
    deadline_p = time.time() + 2.0
    rem_orders = []
    rem_kids = []
    gone = False
    while True:
        try:
            rem_orders = orders(g, og) or []
            rem_kids = order_children(g, og) or []
        except Exception:
            rem_orders, rem_kids = [], []
        in_roots = any(int(x.get("id") or -1) == oid for x in rem_orders)
        in_kids = any(int(x.get("id") or -1) == oid for x in rem_kids)
        if (not in_roots) and (not in_kids):
            gone = True
            break
        if time.time() >= deadline_p:
            break
        time.sleep(0.1)

    res = {
        "ok": bool(gone),
        "error": "" if gone else ("COrderDeleteCommand posted but order %d still present" % oid),
        "order_id": oid,
        "orders_group": hex(og),
        "deleted_children": deleted_children,
        "remaining_orders": len(rem_orders),
        "remaining_children": len(rem_kids),
    }
    if detached_orphans:
        res["detached_orphans"] = detached_orphans
    return res


def target_provinces(g, enemy_tag, depth=2, own_tag=None):
    """Enemy provinces at BFS `depth` from our border -- the offensive line's targets.

    Same rule as stage/wplan.py: start at the enemy provinces that touch us and
    walk inwards, keeping everything at least `depth` steps in (depth=2 skips the
    first row, which the front line already covers).
    """
    import collections
    import mapdata
    own_tag = own_tag or g.player_tag()
    mine = set(sum(owned_provinces(g, own_tag).values(), []))
    theirs = set(sum(owned_provinces(g, enemy_tag).values(), []))
    adj = mapdata.build_adjacency()
    dist = {x: 0 for x in theirs if any(n in mine for n in adj.get(x, ()))}
    q = collections.deque(dist)
    while q:
        u = q.popleft()
        for v in adj.get(u, ()):
            if v in theirs and v not in dist:
                dist[v] = dist[u] + 1
                q.append(v)
    return sorted(x for x, d in dist.items() if d >= depth)


# ====================================================== ARMY GROUPS (PE 1.19.5.0)
# 2026-11-18: `armyops.create_army_group` used to refuse on Windows because the
# only constant we had (AG_CTOR = 0x23167D0) is a LINUX offset -- calling it on
# the PE build executed arbitrary code and blew the heap up ~40 minutes later.
# The PE constructor is now decoded from hoi4.exe instead of guessed:
#
#   CArmyGroupCommand::CArmyGroupCommand(this, array, theatre)   @ 0x18C6840
#     mov  rdi, rdx                     ; arg2 = the array
#     ...
#     lea  rcx, [rbx+0x30] ; mov rdx, rdi ; call 0x18C5440   ; copy the array
#     mov  rdx, [r8+8]  -> [rbx+0x28]   ; arg3's handle (the theatre)
#     mov  [rbx], <0x2A846E0>           ; vtable
#
# That is the same (array, theatre) shape the Linux AG_CTOR takes, so the
# argument ORDER is no longer a guess. Three independent gates still apply:
#   1. the object's vtable is read back and must be 0x2A846E0;
#   2. the command is QUEUED (the engine's own CanExecute runs first) -- a
#      refused command does nothing instead of corrupting anything;
#   3. the caller verifies the army count really grew before reporting ok.
FN_ARMY_GROUP_CTOR = _OW.ORDER_FN_ARMY_GROUP_CTOR
ARMY_GROUP_VT = _OW.ORDER_ARMY_GROUP_VT
ARMY_GROUP_SIZE = _OW.ORDER_ARMY_GROUP_SIZE


def create_army_group(g, armies, theatre_ptr=None):
    """Collect COrdersGroup* armies into one army group. -> (ok, err, info)."""
    import sys
    armies = [int(a) for a in (armies or [])]
    if not armies:
        return False, "no armies to group", {}
    if sys.platform != "win32":
        return False, "create_army_group/PE called on a non-Windows build", {}
    p = g.p
    th = int(theatre_ptr) if theatre_ptr else theatre(g)
    if not th:
        return False, "this country has no theatre", {}
    for og in armies:
        if p.u8(og + OG_IS_GROUP):
            return False, "%#x is already an army group" % og, {}
        if p.u64(og + OG_PARENT):
            return False, "%#x is already inside an army group" % og, {}
    own = g._inj is None
    if own:
        g.attach()
    cmd = 0
    try:
        arr = _mkptrarray(g, armies)
        cmd = cmdwin.alloc(g, ARMY_GROUP_SIZE)
        p.write(cmd, b"\0" * ARMY_GROUP_SIZE)
        g.call(p.base + FN_ARMY_GROUP_CTOR, (cmd, arr, th),
               absolute=True, force=True)
        vt = p.u64(cmd)
    finally:
        if own:
            g.detach()
    if vt != p.base + ARMY_GROUP_VT:
        return False, ("ctor did not produce a CArmyGroupCommand "
                       "(vptr %#x, want %#x)" % (vt, p.base + ARMY_GROUP_VT)), \
               {"cmd": hex(cmd) if cmd else None}
    cmdwin.attach_fake_vtable(g, cmd, ARMY_GROUP_VT)
    posted = cmdwin.post_drained(g, cmd, seconds=2.5)
    info = {"cmd": hex(cmd), "posted": posted,
            "ctor": hex(FN_ARMY_GROUP_CTOR), "vt_ok": True}
    # Which army group now owns them?
    parents = {}
    for og in armies:
        parents[hex(og)] = p.u64(og + OG_PARENT)
    info["parent_after"] = {k: ("0x%x" % v if v else None) for k, v in parents.items()}
    groups = [v for v in parents.values() if v]
    if not groups:
        return False, "the game did not create an army group (no parent was set)", info
    if len(set(groups)) != 1:
        return False, "armies ended up under %d different parents" % len(set(groups)), info
    grp = groups[0]
    info["group_ptr"] = hex(grp)
    info["group_is_group"] = bool(p.u8(grp + OG_IS_GROUP))
    kids = _children_of(g, grp)
    info["group_members"] = len(kids)
    info["group_member_ptrs"] = [hex(k) for k in kids]
    ok = info["group_is_group"] and len(kids) == len(armies)
    return ok, ("" if ok else "parent object is not an army group"), info


# ==============================================================================
# 从 calling/armyops.py 纯搬运的共享只读查询与将领技能接口（阶段 1 Move-Only）
# ==============================================================================
AL_ATTACK = _OW.LEADER_AL_ATTACK
AL_DEFENSE = _OW.LEADER_AL_DEFENSE
AL_PLANNING = _OW.LEADER_AL_PLANNING
AL_LOGISTICS = _OW.LEADER_AL_LOGISTICS
AL_NAME = _OW.LEADER_AL_NAME
AL_TAG = _OW.LEADER_AL_TAG


def orders_groups(g, country=None):
    """通过国家师列表收集所属集团军/集团军群（COrdersGroup*）。"""
    from game import _arr
    p = g.p
    co = country.ptr if country is not None else g.player().ptr
    seen, out = set(), []
    for d in _arr(p, co, 0x290):
        og = p.u64(d + CARMY_OG)
        if og and og not in seen:
            seen.add(og)
            out.append(og)
    return out


def og_info(g, og):
    """读取单个 COrdersGroup 的基础只读摘要。"""
    p = g.p
    ldr = p.u64(og + OG_LEADER)
    name = ""
    try:
        name = p.stdstring(og + OG_NAME)
    except Exception:
        pass
    return {
        "ptr": hex(og),
        "handle": list(struct.unpack("<ii", p.read(og + OG_HANDLE, 8))),
        "name": name,
        "is_army_group": bool(p.u8(og + OG_IS_GROUP)),
        "divisions": p.i32(og + OG_DIVISIONS + 0xC),
        "leader_ptr": hex(ldr) if ldr else None,
        "leader": (p.cstr(p.u64(ldr + 0x40)) if ldr else None),
        "parent_group": hex(p.u64(og + OG_PARENT)) if p.u64(og + OG_PARENT) else None,
        "member_armies": (p.i32(og + OG_MEMBERS + 0xC) if p.u8(og + OG_IS_GROUP) else None),
        "orders": p.i32(og + OG_ORDERS + 0xC),
    }


def leader_skills(g, army_leader):
    """读取 CArmyLeader 的有效四维属性（含特质加成）。"""
    p = g.p
    return {
        "attack": p.i32(army_leader + AL_ATTACK),
        "defense": p.i32(army_leader + AL_DEFENSE),
        "planning": p.i32(army_leader + AL_PLANNING),
        "logistics": p.i32(army_leader + AL_LOGISTICS),
    }


def orders_of(g, orders_group):
    """读取集团军当前挂载的 COrderInstance 列表（简版字段）。"""
    from game import _arr
    p = g.p
    out = []
    for o in _arr(p, orders_group, OG_ORDERS):
        out.append({
            "ptr": hex(o),
            "id": p.i32(o + 0x244),
            "type": p.i32(o + 0x30),          # 1=进攻线 2=前线 3=登陆 4=空降
            "assigned_divisions": p.i32(o + 0x21C),
            "root_front": [p.i32(o + 0x258), p.i32(o + 0x25C)],
            "root_section": p.i32(o + 0x260),
            "from": p.i64(o + 0x268),
            "to": p.i64(o + 0x270),
            "fallback": bool(p.u8(o + 0x298)),
        })
    return out


def group_divisions(g, orders_group, tag=None):
    """返回指定集团军（或集团军群下属全部集团军）的 CArmy* 指针列表。"""
    from game import _arr
    p = g.p
    members = {orders_group}
    if p.u8(orders_group + OG_IS_GROUP):
        members |= set(_arr(p, orders_group, OG_MEMBERS))
    co = (g.country(tag) if tag else g.player()).ptr
    out = []
    for d in _arr(p, co, 0x290):
        og = p.u64(d + CARMY_OG)
        if og in members or (og and p.u64(og + OG_PARENT) in members):
            out.append(d)
    return out


def target_line(g, enemy_tag, depth="full", own_tag=None):
    """计算敌国领土内用于进攻线的目标省份列表。"""
    import mapdata
    from collections import deque
    if own_tag is None:
        own_tag = g.player().tag
    owners, _ = mapdata.province_owner_map(g)
    adj = mapdata.build_adjacency()
    _, ptype = mapdata.load_definitions()
    enemy = {pid for pid, t in owners.items() if t == enemy_tag and ptype.get(pid) == "land"}
    if not enemy:
        return []
    start = [pid for pid in enemy
             if any(owners.get(n) == own_tag and ptype.get(n) == "land" for n in adj.get(pid, ()))]
    if not start:
        start = sorted(enemy)[:1]
    dist = {p0: 0 for p0 in start}
    q = deque(start)
    while q:
        cur = q.popleft()
        for n in adj.get(cur, ()):
            if n in enemy and n not in dist:
                dist[n] = dist[cur] + 1
                q.append(n)
    if depth == "full":
        top = max(dist.values())
        return sorted(p0 for p0, d in dist.items() if d == top)
    depth = int(depth)
    return sorted(p0 for p0, d in dist.items() if d == depth) or sorted(dist, key=dist.get)[-1:]
