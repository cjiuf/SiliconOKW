"""Per-division orders: the same command as a player's RIGHT CLICK.

Not a battle plan line: select a division, right-click a province. In the game
this is `CMoveCommand`, carrying an embedded `CUnitMoveAction`:

  CMoveCommand (vtable 0x42CA2D0)
     +0x28  CUnitMoveAction
              +0x08  0x3648 = the "unit_move_action" token
              +0x0C  the division's handle (from CArmy+0x18)
              +0x18  CPdxArray<i32>  target province path (first element = target)
              +0x50..0x5C  flags
  ctor 0x253A600(this, CArmy*, i32 province, u8, i32, u8, u8,u8,u8,u8)
  NOTE: vtable+0x50 is NOT a predicate here (see below), never call it.

The flags are taken verbatim from one of the game's own call sites: (1,1,0,0,0,0,1).
The command is POSTed to the queue (the player's path), so it works in multiplayer.
"""
import struct, sys
import offsets_win as _OW

# PE 1.19.5.0: CMoveCommand::CMoveCommand(this, CArmy*, province, ...)
MV_CTOR   = _OW.ORDER_MV_CTOR
# WARNING: vtable+0x50 (0x23E9A70) is NOT "CanExecute". It jumps to 0x23E8310, which is
# a 140-line routine that touches game state and checks _ThreadForbidCount.
# Calling it in a live game froze it. Validation is done by FIELD CHECKS only,
# an invalid order is refused by the game anyway.

MV_CANEXE = None
MV_SIZE   = _OW.ORDER_MV_SIZE

ACT_OFF        = _OW.ORDER_ACT_OFF
ACT_TOKEN      = ACT_OFF + 0x08     # 0x3648
# PE layout (dumped 2026-11-16 from a real construction): the handle sits at
# cmd+0x38 (type) / +0x3C (id), i.e. ACT_OFF+0x10, not the Linux ACT_OFF+0x0C.
ACT_DIV_HANDLE = (ACT_OFF + 0x10) if sys.platform == "win32" else (ACT_OFF + 0x0C)
ACT_PATH       = ACT_OFF + 0x18     # data,cap,size,alloc
UNIT_MOVE_TOKEN = _OW.ORDER_UNIT_MOVE_TOKEN

CARMY_HANDLE   = _OW.ORDER_CARMY_HANDLE
CARMY_PROVINCE = _OW.ORDER_CARMY_PROVINCE
PROV_ID        = _OW.PROV_ID


def build_move(g, division, province, flags=(1, 1, 0, 0, 0, 0, 1), path=None,
               use_first_hop=False):
    """Build the command and VERIFY its fields. Returns (cmd_ptr, error).

    `path` is the full land path WITHOUT the source province: [next_hop, ..., dst].

    ONE HOP AT A TIME.  The constructor writes its own one-element
    `CPdxArray<i32>` into cmd+0x18; an earlier version replaced that data pointer
    with a buffer from `operator new`, and the engine then ran `free()` on memory
    that did not come from its pool allocator -> `free(): invalid pointer` /
    `stack smashing` after 10-20 minutes of play.  The array is now left exactly
    as the ctor wrote it: for a multi-hop path the ctor gets `path[0]` (the next
    adjacent province), and the 2-game-day tactical tick issues the next hop when
    the division arrives.  A 1-hop move (the common case) is unchanged.
    """
    p = g.p
    # --- SIGSEGV (rax=0x0) guard, run 8 ---------------------------------------
    # A division that died or went off-map between the scan and the order still
    # has a valid-looking CArmy*; MV_CTOR then dereferences province == NULL and
    # the game dies with sig=11 rax=0x0 (seen once on 1939.12.19). Check the
    # object here, six reads, and refuse instead of crashing the campaign.
    if not division or division < 0x10000:
        return None, "invalid division pointer"
    if not p.u64(division):
        return None, "division object is dead (no vtable)"
    if not p.u64(division + CARMY_PROVINCE):
        return None, "division off-map (province == NULL)"
    if sys.platform == "win32":
        try:
            _h = g.handle_of(division)
            hid = int(_h["id"])
            want = struct.pack("<ii", int(_h["type"]), hid)
        except Exception:
            raw_h = p.try_read(division + CARMY_HANDLE, 8)
            if not raw_h:
                return None, "division handle unreadable"
            _htype, hid = struct.unpack("<ii", raw_h)
            want = raw_h
    else:
        raw_h = p.try_read(division + CARMY_HANDLE, 8)
        if not raw_h:
            return None, "division handle unreadable"
        _htype, hid = struct.unpack("<ii", raw_h)
        want = raw_h
    if hid <= 0:
        return None, "division handle invalid"
    if p.i64(division + 0x420) <= 0:      # CArmy+0x420 = strength (HP)
        return None, "division has zero strength (dead or disbanding)"
    path = [int(x) for x in (path or [])]
    if path and path[-1] != int(province):
        return None, "path does not end at the target province"
    # Run 8: hand the FINAL province to the engine and let its own pathfinder
    # walk the whole way, exactly like a player's right click. The old code
    # forced path[0], i.e. one hop per 2-day tick, which is why a division took
    # weeks to reach a front three provinces away. `use_first_hop=True` is the
    # fallback for the (rare) orders the engine refuses to path itself.
    hop = (path[0] if path else int(province)) if use_first_hop else int(province)
    cmd = g.malloc(MV_SIZE)
    p.write(cmd, b"\0" * MV_SIZE)
    c4, a5, a6, s1, s2, s3, s4 = flags
    g.call(MV_CTOR, [cmd, division, hop, c4, a5, a6, s1, s2, s3, s4], force=True)
    if p.i32(cmd + ACT_TOKEN) != UNIT_MOVE_TOKEN:
        return None, "wrong action token (%#x)" % p.i32(cmd + ACT_TOKEN)
    if p.read(cmd + ACT_DIV_HANDLE, 8) != want:
        return None, "division handle was not written"
    d, cap, n, _al = struct.unpack("<QiiQ", p.read(cmd + ACT_PATH, 0x18))
    if n < 1 or not d:
        return None, "target path is empty"
    if p.i32(d) != hop:
        return None, "target province was not written (%d)" % p.i32(d)
    if sys.platform == "win32":
        # CMoveCommand came from cmdwin.alloc (the game's CPdxNewDeleteAllocator).
        # Attach a no-op deleting destructor vtable (0x2A2CDE0) so queue disposal
        # or rejection does not call CRT operator delete and abort hoi4.exe.
        import cmdwin
        cmdwin.attach_fake_vtable(g, cmd, 0x2A2CDE0)
    return cmd, None


def move(g, division, province, post=True):
    """Order a division to move to or attack a province (a right click)."""
    own = g._inj is None
    if own: g.attach()
    try:
        cmd, err = build_move(g, division, province)
        if err:
            return False, err
        if not post:
            return True, "command built and its fields verified (not sent)"
        # 2026-11-09: force=1 (Post(queue,cmd,1)) skips CanExecute, and the engine
        # then CRASHES a few minutes later on a malformed CMoveCommand -- verified
        # twice tonight (30 moves -> dead in <5 min, both with 14 GB free).
        # Go back to CCommand::Run: it validates the command and refuses the bad
        # ones instead of queueing them.
        g.cmd.post(cmd)
        return True, ""
    finally:
        if own: g.detach()


def halt(g, division, post=True):
    """Halt a division by ordering it to the province it is already in."""
    p = g.p
    pr = p.u64(division + CARMY_PROVINCE)
    if not pr:
        return False, "could not read the division's province"
    return move(g, division, p.i32(pr + PROV_ID), post=post)


def province_of(g, division):
    """Province id of a division, or None when the pointer is stale.

    2026-11-11: hoi4_move_divisions died with EIO in the middle of a batch when
    a division entered combat between hoi4_division_detail and the move — the
    CArmy pointer was freed and `g.p.i32(pr+PROV_ID)` threw ProcessLookupError.
    One bad pointer must not abort the other 59 orders, so every read here is
    now guarded and a stale division is reported as "no province".
    """
    try:
        pr = g.p.u64(division + CARMY_PROVINCE)
    except Exception:
        return None
    if not pr:
        return None
    try:
        return g.p.i32(pr + PROV_ID)
    except Exception:
        return None


def enemy_presence(g, enemy_tags):
    """province_id -> {'divisions':n,'org':x,'hp':y}: where the enemy divisions are."""
    import fastmem as _fm
    from game import CO_ARMIES, _arr
    p = g.p
    div_ptrs = []
    for t in enemy_tags:
        try:
            c = g.country(t)
        except Exception:
            continue
        div_ptrs.extend(_arr(p, c.ptr, CO_ARMIES))
    out = {}
    if not div_ptrs:
        return out
    rows = _fm.read_divisions_batch(
        p,
        div_ptrs,
        off_prov_ptr=CARMY_PROVINCE,
        off_prov_id=PROV_ID,
        off_org=0x428,
        off_str=0x420,
    )
    for r in rows:
        if r is None or r["prov"] is None:
            continue
        pid = r["prov"]
        e = out.setdefault(pid, {"divisions": 0, "org": 0.0, "hp": 0.0})
        e["divisions"] += 1
        e["org"] += r["org_raw"] / 1e5
        e["hp"] += r["str_raw"] / 1e5
    return out

CARMY_PATH      = _OW.ORDER_CARMY_PATH       # CPdxArray<i32>, the movement path
CARMY_PATH_SIZE = _OW.ORDER_CARMY_PATH_SIZE  # last element is the destination province


def movement(g, division):
    """The division's active path. Empty means it is standing still; the last element
    is the destination.

    PURE READ. The array at `CArmy+0x200` fills when an order is posted and empties
    on arrival. Re-targeting a marching division every tick cancels its path and
    leaves it oscillating in place, so the micro engine checks here first.
    """
    p = g.p
    try:
        ptr = p.u64(division + CARMY_PATH)
        n = p.i32(division + CARMY_PATH_SIZE)
        if not ptr or not (0 < n < 256):
            return []
        return [p.i32(ptr + i * 4) for i in range(n)]
    except Exception:
        return []


def destination(g, division):
    """Destination of the march, or None if it is not moving."""
    path = movement(g, division)
    return path[-1] if path else None





