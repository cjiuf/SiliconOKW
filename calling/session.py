"""Session info: single or multiplayer, and which country is being played.

All PURE READS (process_vm_readv), the game is never stopped and there is no
desync risk in multiplayer. The one exception is `pause()`, which goes through the
player's own command channel: the same path as pressing space, subject to whatever
rules the multiplayer session enforces.
"""
import struct
import sys as _sys

import offsets_win as _OW

# PE 1.19.5.0 会话偏移统一来自 offsets_win 单一真源（已接入 profile.py overlay）
G_IDLER = _OW.G_IDLER
G_GAMESTATE = _OW.G_GAMESTATE
IDLER_COUNTRY_NAME = _OW.SESSION_IDLER_COUNTRY_NAME   # std::string, name of the played country
IDLER_LOCAL_PLAYER = _OW.SESSION_IDLER_LOCAL_PLAYER   # std::string, local player name
SESSION_NET = _OW.SESSION_NET                         # CSession+0x58 -> network object (CProxyServer)
CLIENT_FIRST = _OW.SESSION_CLIENT_FIRST               # first remote client name in the network object
CLIENT_STRIDE = _OW.SESSION_CLIENT_STRIDE
GS_MAJORS = _OW.GS_MAJORS                             # major powers (CCountry*)
GS_COMBATS = _OW.GS_COMBATS                           # active land battles (CLandCombat*)
GS_TICK = _OW.GS_CLOCK                                # CCurrentGameState+0x468 实时时钟计数器
IDLER_PAUSED = _OW.SESSION_IDLER_PAUSED               # 1 = paused, 0 = running (SPACE 切换字节)


def _std_string(p, addr):
    if hasattr(p, "stdstring"):
        return p.stdstring(addr)
    try:
        ptr, ln = struct.unpack("<Qq", p.read(addr, 16))
        if 0 <= ln < 256:
            return p.read(ptr, ln).decode("utf8", "replace")
    except OSError:
        pass
    return None


def _idler_name(g):
    """The name at idler+0x7C0. NOTE: this field changes during play. It appears
    to hold whoever acted last. So it is NOT reliable as the local player name."""
    idler = g.p.u64(g.p.base + G_IDLER)
    if not idler:
        return None
    return _std_string(g.p, idler + IDLER_LOCAL_PLAYER)


def country_name(g):
    """The country name cached in the idler: may be stale."""
    idler = g.p.u64(g.p.base + G_IDLER)
    if not idler:
        return None
    return _std_string(g.p, idler + IDLER_COUNTRY_NAME)


def _my_country_name(g):
    try:
        return g.player().name
    except Exception:
        return country_name(g)


def _net_object(g):
    s = g.oi.instances("CSession")
    if not s:
        return None, None
    net = g.p.u64(s[0] + SESSION_NET)
    return net, (g.oi.class_of(net) if net else None)


def remote_players(g, max_clients=16, deep=False):
    """Remote client names in the network object, std::strings 0x700 apart.

    2026-11-21: `deep=False` (the default) returns [] WITHOUT touching
    `g.oi.instances("CSession")` -- that call is a full RTTI scan (40+ s cold) and
    it made `hoi4_session_info` exceed the 50 s per-call budget on every restart.
    Pass deep=True when multiplayer really matters.
    """
    if not deep:
        return []
    net, _ = _net_object(g)
    if not net:
        return []
    out = []
    for k in range(max_clients):
        n = _std_string(g.p, net + CLIENT_FIRST + k * CLIENT_STRIDE)
        if n and n.isprintable() and 1 < len(n) < 40:
            out.append(n)
    return out


def info(g):
    """The session summary to read when connecting."""
    p = g.p
    gs = p.u64(p.base + G_GAMESTATE)
    from game import _arr
    tagof = {}
    for t in g.tags():
        try:
            tagof[g.country(t).ptr] = t
        except Exception:
            pass
    try:
        majors = [tagof.get(x) for x in _arr(p, gs, GS_MAJORS)]
    except Exception:
        majors = []
    names = remote_players(g)
    extra = _idler_name(g)
    if extra and extra not in names:
        names.append(extra)
    # 2026-11-21: `_net_object()` calls `g.oi.instances("CSession")`, i.e. a full
    # RTTI scan of the process.  On a cold server that is 40+ s, and it is what
    # made hoi4_session_info blow the 50 s watchdog after every restart -- twice,
    # because remote_players() and this line each called it.  The class name is
    # cosmetic; do not pay a process scan for it.
    net, netcls = None, None
    me = g.player_tag()
    try:
        import combat as _cb
        at_war = _cb.at_war(g)      # pure read; g.wars() would freeze the game
    except Exception:
        at_war = None
    try:
        combats = len(_arr(p, gs, GS_COMBATS))
    except Exception:
        combats = 0
    return {
        "my_tag": me,
        # Take the name from CCountry: idler+0x620 is a CACHE and goes stale when a
        # civil war moves the player to a different country.
        "my_country": _my_country_name(g),
        "is_multiplayer": len(names) > 1,
        # 2026-11-21 (续测 P2-11): 0 read as "there are zero players", which is
        # nonsense in singleplayer.  The network scan is skipped on PE, so the
        # count is genuinely UNKNOWN, not zero -- report null.
        "player_count": len(names) if names else None,
        "players": sorted(names),
        "network_class": netcls,
        "majors": majors,
        "at_war": at_war,
        "active_land_combats": combats,
        "date": g.date()["text"],
        "note": ("Player names are read from the network object (CSession+0x58) at "
                 "0x700 intervals. Which of them is the local player CANNOT be told; "
                 "the country you are playing is definitive in my_tag/my_country. "
                 "2026-11-21: the network object scan is skipped by default (it was a "
                 "40 s RTTI scan and blew the per-call budget); is_multiplayer is "
                 "therefore derived from the idler name only."),
    }


def is_paused(g):
    """Is the game paused right now? PURE READ (idler+0x6C1)."""
    idler = g.p.u64(g.p.base + G_IDLER)
    if not idler:
        return False
    return bool(g.p.u8(idler + IDLER_PAUSED))


# 2026-11-21 (P0-3): has any clock witness in THIS process ever moved?  The date
# reader can degrade to the game.log stamp, and a frozen reading is only evidence
# of a paused game once the witness has been seen alive at least once.
_CLOCK = {"live": False}


def pause(g, paused=True, who=None):
    """Pause or resume -- the same state change the player's SPACE key makes.

    The engine keeps the whole state in ONE byte, idler+0x6C1 (1 = paused,
    0 = running), and its own toggle writes that byte directly.  Disassembled on
    1.19.5.0 at hoi4.exe+0xE48C2C (rbx = the CInGameIdler):

        cmp  byte ptr [rbx+0x6C1], 0
        sete al
        mov  byte ptr [rbx+0x6C1], al

    So a plain byte write is not a trick around the engine -- it is literally what
    the engine does.  Measured on pid 29864 (2026-11-21): writing 1 froze the
    clock for as long as the byte stayed 1, writing 0 let the clock run again, and
    the tick loop never reset the byte in either direction.

    History (do not regress): an earlier revision concluded the byte was an
    "output" and switched to calling hoi4.exe+0x1E0310.  That address is a lookup
    helper -- `idler->GetById(int)`, which scans the 0xA0-byte entries at
    [idler+0xF8] comparing [entry+0x98] against edx and returns idler+0x110 when
    nothing matches -- so it cannot pause anything.  That is why the pause tool
    reported ok=false and why resume "never worked".

    Verification is the CLOCK, not the flag -- but only while the clock can be
    trusted.  `date_raw()` degrades to the game.log stamp (written roughly every
    12 s) whenever its CDate address is stale, and a stale clock must never be
    allowed to invent a failure: that is exactly what made the previous build
    roll back a pause that had worked.  `_clock_moves()` therefore also records
    whether ANY witness in this process has ever moved, and pause() has three
    outcomes instead of two:
      * clock moved          -> the game is running (fact)
      * frozen + live clock  -> paused, or a second pause source (event popup /
                                menu) still holds it (fact)
      * frozen + stale clock -> the byte is all we know; say "unverified"
                                instead of inventing a popup that may not exist
    """
    want = bool(paused)
    import time as _t

    if _sys.platform == "win32":
        import win_pipeline as _wp
        _wp.ensure_live_version(g, only_if_live=True)
        p = g.p
        idler = p.u64(p.base + G_IDLER)
        if not idler:
            return False, "no idler (still on the menu?)"

        def _clock_moves(sec=1.5):
            """Did the game tick at all?  Three witnesses, cheapest first.

            PRIMARY: CGameState+0x468, an integer that the tick loop bumps while
            the game runs and that stops dead the moment it is paused.  Measured
            on pid 29864 (2026-11-21): 60779703 -> 60779709 over 3 s at speed 3,
            then 60779712 / 60779712 / 60779712 for 4.5 s with idler+0x6C1 = 1,
            then 60779715 -> 60779718 after the byte went back to 0.  It needs no
            date parsing, no game.log and no scan.

            Then the verified CDate (fast when its address is really live), then
            `date_raw()` once at the end -- the date reader degrades to the
            game.log stamp, which is written only every ~12 s, so it is the last
            resort and never the only witness.
            """
            addr = None
            if getattr(g, "_date_verified", False):
                addr = getattr(g, "_date_addr", None) or getattr(g, "_win_date", None)

            def _direct():
                if addr:
                    try:
                        return p.i32(addr)
                    except Exception:
                        pass
                return None

            def _tick():
                try:
                    gs = p.u64(p.base + G_GAMESTATE)
                    return p.i32(gs + GS_TICK) if gs else None
                except Exception:
                    return None

            b_t = _tick()
            b_d = _direct()
            b_r = g.date_raw()
            t0 = _t.time()
            while _t.time() - t0 < sec:
                _t.sleep(0.2)
                n_t = _tick()
                if b_t is not None and n_t is not None and n_t != b_t:
                    _CLOCK["live"] = True
                    return True
                n_d = _direct()
                if b_d is not None and n_d is not None and n_d != b_d:
                    _CLOCK["live"] = True
                    return True
            if g.date_raw() != b_r:
                _CLOCK["live"] = True
                return True
            return False

        cur = bool(p.u8(idler + IDLER_PAUSED))
        # Establish that this process HAS a live clock before we stop it: pausing
        # is precisely what makes the clock stand still, so without this probe the
        # first pause of a fresh server process could never be verified.
        if not _CLOCK["live"] and not cur:
            _clock_moves(0.7)
        if cur == want:
            if want:
                if not _clock_moves(0.7):
                    return True, "already paused"
            elif _clock_moves(0.7):
                return True, "already running"
        p.write(idler + IDLER_PAUSED, b"\x01" if want else b"\x00")
        _t.sleep(0.15)
        got = bool(p.u8(idler + IDLER_PAUSED))
        if got != want:
            return False, ("wrote idler+0x6C1=%d but it reads %d -- another pause "
                           "source flipped it straight back" % (int(want), int(got)))
        moved = _clock_moves(1.0)
        if not _CLOCK["live"]:
            return True, ("unverified: pause byte is %d, but this process's date "
                          "reader is stuck on game.log, so the clock cannot confirm "
                          "it" % int(want))
        if want and moved:
            return False, ("idler+0x6C1 reads 1 but the clock is still running "
                           "-- the write did not take effect")
        if (not want) and not moved:
            return False, ("idler+0x6C1 reads 0 but the clock is still frozen "
                           "-- a second pause source (event popup / menu / focus "
                           "loss) still holds the game")
        return True, ""

    raise NotImplementedError(
        "Linux CPauseGame/xdotool pause path has been removed; use Windows PE."
    )
