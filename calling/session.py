"""Session info: single or multiplayer, and which country is being played.

All PURE READS (process_vm_readv), the game is never stopped and there is no
desync risk in multiplayer. The one exception is `pause()`, which goes through the
player's own command channel: the same path as pressing space, subject to whatever
rules the multiplayer session enforces.
"""
import struct
import sys as _sys

# 1.19.4.1 (f584) globals. These used to be the 1.19.2 addresses, which made
# is_paused() read a random byte (0xF4 -> always "paused") and date_raw() never
# self-heal. Keep them in one place; commands.py uses IDLER_GLOBAL = 0x459E908.
G_IDLER      = 0x459E908
G_GAMESTATE  = 0x459E9F0
# The PE build is a different binary: reading the ELF offsets there fails outright
# (`ReadProcessMemory ... GetLastError=299`) and that made hoi4_pause / hoi4_resume
# throw on Windows.  Take the verified 1.19.5.0 values when they exist.
if _sys.platform == "win32":
    try:
        import offsets_win as _OW
        G_IDLER = getattr(_OW, "G_IDLER", G_IDLER)
        G_GAMESTATE = getattr(_OW, "G_GAMESTATE", G_GAMESTATE)
    except ImportError:
        pass
IDLER_COUNTRY_NAME = 0x620    # std::string, name of the played country
IDLER_LOCAL_PLAYER = 0x7C0    # std::string, local player name
SESSION_NET  = 0x58           # CSession+0x58 -> network object (CProxyServer)
CLIENT_FIRST = 0x348          # first remote client name in the network object
CLIENT_STRIDE = 0x700
GS_MAJORS    = 0x328          # major powers (CCountry*)
GS_COMBATS   = 0x268          # active land battles (CLandCombat*)
# 2026-11-21 (P0-3): an integer at CCurrentGameState+0x468 that the tick loop
# bumps while the game runs and that stops dead the moment it is paused.
# Measured on pid 29864: 60779703 -> 60779709 over 3 s at speed 3, then identical
# for 4.5 s with idler+0x6C1 = 1, then 60779715 -> 60779718 after the byte went
# back to 0.  This is the pause verifier's primary witness -- no date parsing,
# no game.log, no scan.
GS_TICK      = 0x468

IDLER_PAUSED = 0x6C1          # 1 = paused, 0 = running (the byte SPACE toggles).
                              # +0x6C3 is a DIFFERENT flag: measured 0 both while
                              # running and while paused on pid 29864 -- it does
                              # not mirror +0x6C1.
if _sys.platform == "win32":
    try:
        import profile as _prof
        IDLER_PAUSED = _prof.anchor("IDLER_PAUSED", IDLER_PAUSED)
    except Exception:
        pass

# CPauseGame: vtable 0x4460D70 (1.19.4.1), object size 0x50.
# Verified by disassembling the constructors (0x21BAE00 default, 0x21BAE60 copy):
#   +0x28 std::string _M_p  -> +0x38 (SSO buffer)      +0x30 _M_string_length
#   +0x38 char[16] local buffer                        +0x48 u8 "pause"  +0x49 u8 flag
# The old PAUSE_CTOR 0x2095400 was a 1.19.2 address and segfaulted on every call,
# which is why hoi4_pause had to fall back to xdotool. No constructor call now.
VT_PAUSE_GAME = 0x4460D70
PAUSE_SIZE    = 0x50


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

    def running_proof():
        """The clock moved -> really running (the pause flag alone lies at start)."""
        before = g.date_raw()
        _t.sleep(1.5)
        return g.date_raw() != before

    if is_paused(g) == want:
        if want:
            return True, "already paused"
        if running_proof():
            return True, "already running"
    ok, err = _toggle(g, who, want)
    if ok:
        for _ in range(10):
            _t.sleep(0.05)
            if is_paused(g) == want:
                if want or running_proof():
                    return True, ""
                break
    # 1.19.4.1: CPauseGame(+0x48=1) pauses correctly, but +0x48=0 does NOT resume
    # (the engine ignores the resume through this command; verified live 2026-11-04
    # and again 2026-11-05 on a fresh campaign). Fall back to the player's own UI:
    # the clock/pause icon in the top bar. Keyboard events never reach the SDL
    # window under this headless X server, but mouse clicks do.
    if not want:
        for attempt in (_click_pause_button, _key_toggle):
            if attempt():
                for _ in range(14):
                    _t.sleep(0.2)
                    if running_proof():
                        return True, ""
    return False, err or ("pause command accepted but the state did not change")


def _click_pause_button(display=None):
    """Click the pause icon in the top bar (window-relative, right-aligned)."""
    import os, subprocess
    disp = display or os.environ.get("HOI4_DISPLAY") or ":1"
    env = dict(os.environ, DISPLAY=disp)
    try:
        ids = subprocess.run(["xdotool", "search", "--name", "Hearts of Iron"],
                             capture_output=True, text=True, env=env, timeout=10).stdout.split()
        if not ids:
            return False
        wid = ids[-1]
        geo = subprocess.run(["xdotool", "getwindowgeometry", "--shell", wid],
                             capture_output=True, text=True, env=env, timeout=10).stdout
        width = 1280
        for line in geo.splitlines():
            if line.startswith("WIDTH="):
                width = int(line.split("=", 1)[1])
        subprocess.run(["xdotool", "mousemove", "--window", wid, str(width - 290), "22"],
                       timeout=10, env=env)
        subprocess.run(["xdotool", "click", "1"], timeout=10, env=env)
        return True
    except Exception:
        return False


def _key_toggle(display=None):
    """Press space in the game window (works where the command path does not)."""
    import os, subprocess
    disp = display or os.environ.get("HOI4_DISPLAY") or ":1"
    try:
        ids = subprocess.run(["xdotool", "search", "--name", "Hearts of Iron"],
                             capture_output=True, text=True,
                             env=dict(os.environ, DISPLAY=disp), timeout=10).stdout.split()
        if ids:
            subprocess.run(["xdotool", "windowactivate", ids[0]], timeout=10,
                           env=dict(os.environ, DISPLAY=disp),
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["xdotool", "key", "space"], timeout=10,
                       env=dict(os.environ, DISPLAY=disp),
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception:
        return False


def _toggle(g, who=None, want=True):
    """Build CPauseGame (0x50 bytes, vtable 0x4460D70) directly in memory.

    Pure struct construction, no constructor call: the 1.19.2 ctor address
    (0x2095400) is not a function in this binary.
    """
    import struct as _s
    from commands import BASE_INIT
    p = g.p
    who = (who or (_idler_name(g) or "AI")).encode("utf-8", "replace")[:15]
    own = g._inj is None
    if own:
        g.attach()
    try:
        cmd = g.malloc(PAUSE_SIZE)
        blob = bytearray(PAUSE_SIZE)
        _s.pack_into("<Q", blob, 0x00, p.base + VT_PAUSE_GAME)
        for off, fmt, val in BASE_INIT:
            _s.pack_into(fmt, blob, off, val)
        _s.pack_into("<QQ", blob, 0x28, cmd + 0x38, len(who))
        blob[0x38:0x38 + len(who)] = who
        blob[0x48] = 1 if want else 0
        blob[0x49] = 0
        p.write(cmd, bytes(blob))
        rv = g.cmd.post(cmd)
        if not (rv & 0xFF):
            return False, getattr(g.cmd, "last_error", "") or "CCommand::Run refused CPauseGame"
        return True, ""
    finally:
        if own:
            g.detach()
