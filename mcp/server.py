#!/usr/bin/env python3
"""
Clausewitz-MCP server, the tool layer that lets a model play Hearts of Iron IV
the way a person does.

Two kinds of tool:
  * Play tools (hoi4_*): focus tree, research, production, construction, politics,
    decisions, armies. Actions are posted to the game's own CCommandQueue. The
    same path a mouse click takes. Not cheats: the game still runs CanExecute.
  * Debug tools (hoi4_debug_*): console commands, raw memory, arbitrary function
    calls. These ARE cheats and are kept behind their own prefix so they never get
    used by accident during a normal game.

Transport: JSON-RPC 2.0 over stdio (MCP 2024-11-05). No external dependencies --
the protocol is small enough to speak by hand.
"""
from __future__ import annotations
import base64, io, json, os, re, subprocess, sys, tempfile, traceback

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "calling"))
from game import Game                       # noqa: E402
import offsets as O                         # noqa: E402

import config as _config                # noqa: E402

PROTOCOL = "2024-11-05"
# Resolved from HOI4_PATH / the usual Steam locations, never hardcoded: the doc
# file only feeds the command_help tooltips, so a miss is harmless.
GAME_DIR = _config.game_dir(required=False) or ""
DOC_MD = os.path.join(GAME_DIR, "documentation", "console_commands_documentation.md")

_g: Game | None = None


# --- self-heal (2026-11-02 P1): survive game restart / save reload / db loss ---
import time as _time

def _proc_start_ticks(pid: int) -> int:
    if sys.platform == "win32":
        try:
            import ctypes
            import ctypes.wintypes as wt
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            h = k32.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
            if not h:
                return 0
            try:
                ct, et, kt, ut = wt.FILETIME(), wt.FILETIME(), wt.FILETIME(), wt.FILETIME()
                if k32.GetProcessTimes(h, ctypes.byref(ct), ctypes.byref(et),
                                       ctypes.byref(kt), ctypes.byref(ut)):
                    return (ct.dwHighDateTime << 32) | ct.dwLowDateTime
            finally:
                k32.CloseHandle(h)
        except Exception:
            pass
        return 0
    try:
        with open("/proc/%d/stat" % pid) as f:
            return int(f.read().rsplit(")", 1)[1].split()[19])
    except Exception:
        return 0

_selfheal_pid = None
_selfheal_start = 0


def _stderr(*a, **k):
    """C4 (review 2026-11-07): this process speaks JSON-RPC on stdout. A bare
    print() injects a text line into the pipe and the gateway can no longer
    parse the reply. Every diagnostic goes to stderr."""
    k["file"] = sys.stderr
    k.setdefault("flush", True)
    print(*a, **k)
_selfheal_start = 0

def _selfheal_check(g) -> None:
    """Detect game restart / save reload and rebuild volatile caches in-process.

    Triggers:
      * game pid changed (hoi4-guard restarted the game);
      * /proc/<pid>/stat starttime changed (same pid reused);
    Actions: drop per-pid caches (tags, tag2idx, date addr, objindex scan,
    equipment db, tech db) so the next tool call rebuilds them.
    """
    global _selfheal_pid, _selfheal_start
    pid = getattr(g.p, "pid", None)
    if not pid:
        return
    st = _proc_start_ticks(pid)
    if _selfheal_pid is None:
        _selfheal_pid, _selfheal_start = pid, st
        return
    if pid != _selfheal_pid or st != _selfheal_start:
        _stderr("[self-heal] game pid %s -> %s (start %s -> %s): rebuilding caches"
              % (_selfheal_pid, pid, _selfheal_start, st))
        _selfheal_pid, _selfheal_start = pid, st
        g._tags = None
        g._tag2idx = None
        g._date_addr = None
        g._cmds = None
        g._moddefs = None
        # `Game.oi` is a read-only @property that lazily builds
        # ObjectIndex(g.p, ...) — `g.oi = ObjectIndex(g.p)` raised (wrong arg count)
        # and the exception was swallowed, so g._oi kept pointing at the DEAD
        # process's memory after every game restart. Drop the cache instead; the
        # next access rebuilds it against the new pid.
        if hasattr(g, "_oi"):
            g._oi = None
        try:
            type(g)._VT_SCAN.clear()
        except Exception:
            pass
        # 2026-11-17: _tech_by_name/_tech_by_id must be RESET, not deleted --
        # calling/game.py:_load_tech_db() reads them as plain attributes
        # (`if self._tech_by_name is not None`) and blew up with
        #   AttributeError: 'Game' object has no attribute '_tech_by_name'
        # on the first research call after every game restart.
        for attr in ("_tech_by_name", "_tech_by_id"):
            try:
                setattr(g, attr, None)
            except Exception:
                pass
        for attr in ("_eq_by_id", "_eq_by_name",
                     "_dec_defs", "_idea_defs", "_idea_cache", "_bld_by_key",
                     "_bld_by_id", "_doc_cache_key", "_pdx_newdel_alloc",
                     "_div_tagmap", "_win_date", "_win_date_pid", "_win_date_last"):
            if hasattr(g, attr):
                try:
                    delattr(g, attr)
                except Exception:
                    pass
        try:
            g.reset_front_snapshot()
        except Exception:
            pass
        # 2026-11-19: start the CTechnologyTemplate RTTI scan NOW, in the
        # background, instead of waiting for the first research call to ask for
        # it.  The scan takes ~30-40 s on a fresh process; kicking it here means
        # hoi4_overview / hoi4_research_available usually find it warm, and the
        # first research tool no longer has to block for half a minute.
        try:
            import sys as _sys
            if _sys.platform == "win32":
                g._kick_tech_scan()
        except Exception:
            pass

def _date_backjump(g) -> None:
    """Rescan the date address when the game date jumps BACKWARD > 30 days
    (i.e. another save was loaded)."""
    try:
        d = g.date()
    except Exception:
        return
    prev = getattr(g, "_heal_last_raw", None)
    raw = d.get("raw")
    if prev is not None and raw is not None and raw < prev - 30 * 24:
        _stderr("[self-heal] date backjump %s -> %s: rescanning date address"
              % (prev, raw))
        try:
            type(g)._VT_SCAN.clear()
        except Exception:
            pass
        if hasattr(g, "_oi"):
            g._oi = None
        for attr in ("_div_tagmap", "_win_date", "_win_date_pid", "_win_date_last",
                     "_doctrine_done", "_hk_state_key"):
            if hasattr(g, attr):
                try:
                    delattr(g, attr)
                except Exception:
                    pass
        try:
            g.reset_front_snapshot()
        except Exception:
            pass
        try:
            g.find_date_address(force=True)
        except Exception as e:
            _stderr("[self-heal] date rescan failed: %r" % e)
    if raw is not None:
        g._heal_last_raw = raw


def game() -> Game:
    global _g, _WINJECT_RECOVERY
    _created_fresh = False
    if _g is None:
        _g = Game()
        _created_fresh = True
        # 2026-11-21 (P0-2): a previous server may have died between SuspendThread
        # and ResumeThread, leaving a game thread with a suspend count of 1.  The
        # injector now journals the TIDs it suspends; resume exactly those (pid AND
        # process creation time must match, so a recycled pid is refused).
        try:
            import winject
            _WINJECT_RECOVERY = winject.resume_orphans(_g.p.pid)
            if _WINJECT_RECOVERY.get("found"):
                _stderr("[winject] orphan record: %r" % (_WINJECT_RECOVERY,))
        except Exception as e:
            _WINJECT_RECOVERY = {"error": repr(e)}
            _stderr("[winject] resume_orphans failed: %r" % (e,))
    else:
        try:
            _g.p.read(_g.p.base, 4)
        except OSError:
            # 2026-11-23 (C-2): 旧进程已退出重建 Game() 时，立刻清理陈旧的 _VT_SCAN /
            # _VT_SCAN_RUNNING / _VT_SCAN_EMPTY_AT 并在后台预热科技库扫描。
            try:
                Game._VT_SCAN.clear()
                getattr(Game, "_VT_SCAN_EMPTY_AT", {}).clear()
                getattr(Game, "_VT_SCAN_RUNNING", set()).clear()
            except Exception:
                pass
            _g = Game()
            _created_fresh = True
    try:
        _selfheal_check(_g)
        _date_backjump(_g)
    except Exception as e:
        _stderr("[self-heal] error: %r" % e)
    if _created_fresh and sys.platform == "win32":
        try:
            _g._kick_tech_scan()
        except Exception:
            pass
    return _g


def verified(result, probe, label=""):
    """Attach a read-back verdict to a write tool's result.

    Nothing in the command channel proves the engine accepted an order, so a
    write tool that cannot observe its own effect is reporting hope, not fact.
    `verify.poll` watches the game clock as well as the observable: a frozen
    clock yields `unverified`, never `rejected`, and `unverified` clears `ok`.
    """
    import verify as _v
    state, detail, observed = _v.poll(probe, clock=lambda: game().date()["raw"])
    return _v.attach(result, state, (label + ": " if label else "") + detail, observed)


_doc_cache = None


def command_docs():
    global _doc_cache
    if _doc_cache is None:
        _doc_cache = {}
        try:
            txt = open(DOC_MD, encoding="utf-8", errors="replace").read()
        except OSError:
            return _doc_cache
        for block in re.split(r"\n## ", txt)[1:]:
            name, _, body = block.partition("\n")
            _doc_cache[name.strip()] = body.strip()
    return _doc_cache


def _hoi4_display():
    """The DISPLAY the game window is actually reachable on.

    2026-11-11: this used to take the FIRST pgrep hit, which is the runuser
    wrapper; its environ says DISPLAY=:0 (it is inherited from the shell), so
    every screenshot targeted :0 where no game window exists and then fell
    through to `spectacle`, which is not installed -> FileNotFoundError. Now
    every candidate is probed with xdotool and only a display that can actually
    see the HOI4 window is returned.
    """
    cands = []
    try:
        pids = subprocess.check_output(["pgrep", "-f", "Hearts of Iron IV/hoi4"],
                                       text=True).split()
    except Exception:
        pids = []
    for pid in pids:
        try:
            env = open("/proc/%s/environ" % pid, "rb").read().split(b"\0")
        except Exception:
            continue
        for e in env:
            if e.startswith(b"DISPLAY="):
                d = e.split(b"=", 1)[1].decode()
                if d not in cands:
                    cands.append(d)
    envd = os.environ.get("HOI4_DISPLAY")
    if envd and envd not in cands:
        cands.insert(0, envd)
    for d in list(cands):
        try:
            out = subprocess.run(["xdotool", "search", "--name", "Hearts of Iron"],
                                 capture_output=True, text=True, timeout=5,
                                 env=dict(os.environ, DISPLAY=d)).stdout
            if out.strip():
                return d
        except Exception:
            continue
    return cands[0] if cands else None


def _win_hwnd(title_part="Hearts of Iron"):
    """The game's top-level window handle (0 when it is not running)."""
    import ctypes
    from ctypes import wintypes
    u = ctypes.windll.user32
    found = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _enum(h, _l):
        buf = ctypes.create_unicode_buffer(256)
        u.GetWindowTextW(h, buf, 256)
        if title_part in buf.value and u.IsWindowVisible(h):
            found.append(h)
        return True

    try:
        u.EnumWindows(_enum, 0)
    except Exception:
        return 0
    return found[0] if found else 0


def _win_screenshot(crop=None, focus=False) -> bytes:
    """Capture the HOI4 window on Windows and return PNG bytes.

    `crop` uses the same "WxH+X+Y" syntax as the X11 path, relative to the game
    window.  The window is only raised when `focus` is true.
    """
    import ctypes
    from ctypes import wintypes
    from PIL import ImageGrab
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass
    u = ctypes.windll.user32
    hwnd = _win_hwnd()
    if not hwnd:
        return b""
    if focus:
        u.ShowWindow(hwnd, 9)
        u.SetForegroundWindow(hwnd)
    class R(ctypes.Structure):
        _fields_ = [("l", wintypes.LONG), ("t", wintypes.LONG),
                    ("r", wintypes.LONG), ("b", wintypes.LONG)]
    r = R()
    if not u.GetWindowRect(hwnd, ctypes.byref(r)):
        return b""
    box = (r.l, r.t, r.r, r.b)
    img = ImageGrab.grab(bbox=box, all_screens=True)
    if crop:
        import re as _re
        m = _re.match(r"^(\d+)x(\d+)\+(-?\d+)\+(-?\d+)$", str(crop).strip())
        if m:
            w, h, x, y = (int(g) for g in m.groups())
            img = img.crop((x, y, x + w, y + h))
    # 2026-11-21 (P2-6): a full 2048x1152 window as PNG is ~5 MB and the client
    # failed to render it.  An UNCROPPED shot is a "what is on screen" overview,
    # so downscale it until it is comfortably deliverable; an explicit `crop` is
    # left at full resolution because that path exists to read small text.
    from PIL import Image
    if not crop and img.width > 1600:
        img = img.resize((1600, max(1, round(img.height * 1600.0 / img.width))),
                         Image.LANCZOS)
    data = b""
    for _ in range(5):
        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        data = buf.getvalue()
        limit = 6_000_000 if crop else 1_800_000
        if len(data) <= limit or img.width <= 640:
            break
        img = img.resize((max(1, img.width * 3 // 4), max(1, img.height * 3 // 4)),
                         Image.LANCZOS)
    return data


def screenshot(crop=None, focus=False) -> bytes:
    """Screenshot of the game.

    By default the HOI4 window is captured DIRECTLY over X11. The window
    is never raised or focused, so it works while the user is in another app.
    If that fails it falls back to spectacle, which captures the whole screen.
    """
    if sys.platform == "win32":
        # BUG-17: the whole body below is X11 (xwd / import / spectacle), so on
        # hoi4.exe it returned b"" and the agent was blind.  Grab the game's own
        # window rectangle with PIL instead -- same thing stage/ui.py does.
        return _win_screenshot(crop, focus)
    out = tempfile.NamedTemporaryFile(suffix=".png", delete=False); out.close()
    got = False
    disp = _hoi4_display()
    # 2026-11-11: `import -window "Hearts of Iron IV"` hangs for the full 30 s
    # timeout on this Xvfb setup (every screenshot costs 30 s and then falls
    # back anyway, so the agent never sees the screen). xwd on the window id is
    # instant, and PIL turns it into a PNG — the same path stage/shot.sh uses.
    if disp and not focus:
        env = dict(os.environ, DISPLAY=disp)
        try:
            wid = subprocess.run(["xdotool", "search", "--name", "Hearts of Iron IV"],
                                 capture_output=True, text=True, timeout=5,
                                 env=env).stdout.split()
            if wid:
                xwd = out.name + ".xwd"
                with open(xwd, "wb") as fh:
                    r = subprocess.run(["xwd", "-id", wid[-1], "-silent"],
                                       stdout=fh, stderr=subprocess.DEVNULL,
                                       timeout=8, env=env)
                if r.returncode == 0 and os.path.getsize(xwd) > 1000:
                    from PIL import Image
                    Image.open(xwd).convert("RGB").save(out.name)
                    got = os.path.getsize(out.name) > 1000
                try:
                    os.unlink(xwd)
                except OSError:
                    pass
        except Exception:
            got = False
    if not got and disp and not focus:
        env = dict(os.environ, DISPLAY=disp)
        try:
            r = subprocess.run(["import", "-silent", "-window", "root", out.name],
                               capture_output=True, timeout=10, env=env)
            got = r.returncode == 0 and os.path.getsize(out.name) > 1000
        except Exception:
            got = False
    if not got:
        if focus:
            _raise_hoi4_window()
        try:
            subprocess.run(["spectacle", "-b", "-n", "-f", "-o", out.name],
                           capture_output=True, timeout=30)
        except Exception:
            pass
    if crop:
        crop_s = str(crop).strip()
        if not re.match(r"^\d+x\d+\+-?\d+\+-?\d+$", crop_s):
            raise ValueError("invalid crop geometry (expected WxH+X+Y): %r" % (crop,))
        subprocess.run(["magick", out.name, "-crop", crop_s, out.name], capture_output=True)
    data = open(out.name, "rb").read()
    try: os.unlink(out.name)
    except OSError: pass
    return data


def _raise_hoi4_window():
    js = tempfile.NamedTemporaryFile("w", suffix=".js", delete=False)
    js.write('var ws = workspace.windowList ? workspace.windowList() : workspace.clientList();\n'
             'for (var i=0;i<ws.length;i++){var w=ws[i];\n'
             '  if(w.resourceClass=="hoi4"){w.minimized=false;workspace.activeWindow=w;break;}}\n')
    js.close()
    name = "hoi4shot%d" % os.getpid()
    for args in (["loadScript", js.name, name], ["start"]):
        subprocess.run(["gdbus", "call", "--session", "--dest", "org.kde.KWin",
                        "--object-path", "/Scripting",
                        "--method", "org.kde.kwin.Scripting." + args[0]] + args[1:],
                       capture_output=True)
    try: os.unlink(js.name)
    except OSError: pass



# --------------------------------------------------------------------- tools
TOOLS, HANDLERS = [], {}
_STR = {"type": "string"}
_INT = {"type": "integer"}
_BOOL = {"type": "boolean"}
NO_ARGS = {"type": "object", "properties": {}}
TAG = {"tag": dict(_STR, description="Country tag (empty = the player)")}


def tool(name, description, props=None, required=None):
    schema = {"type": "object", "properties": props or {}}
    if required:
        schema["required"] = required

    def deco(fn):
        TOOLS.append({"name": name, "description": description, "inputSchema": schema})
        HANDLERS[name] = fn
        return fn
    return deco


# =============================================================== general status
@tool("hoi4_overview",
      "Full macro picture for the player (or a given country): date, alerts (red-dot "
      "summary), industry, political power, stability, war support, manpower, active focus, "
      "research slots, active ideas/laws, production lines, construction queue. "
      "The first thing to call each turn. "
      "`alerts` now also carries AIR_NO_MISSION / NAVY_NO_MISSION / UNASSIGNED_DIVS: "
      "if you see AIR_NO_MISSION the air force is doing NOTHING (every wing at mission 0) "
      "-- call hoi4_air_mission or hoi4_housekeep; if you see NAVY_NO_MISSION every task "
      "force is idle -- call hoi4_naval_mission. These two were ignored in every previous "
      "campaign and the Luftwaffe never flew a single sortie. "
      "Also: hoi4_overview -> hoi4_housekeep at the top of every turn covers air, navy, "
      "research, construction, production, politics, recruitment and divisions at once.", TAG)
def _overview(a):
    return game().overview(a.get("tag"))


@tool("hoi4_thresholds",
      "BANKED-RESOURCE ALARMS. Political power (warn at >=1000 -- the cap is 2000, so "
      "everything above that is thrown away) and army / navy / air experience (warn at "
      ">=100 -- one grand-doctrine or air-doctrine step costs about that). Returns the "
      "three XP pools plus `reached`, the list of thresholds currently crossed, and "
      "`spend_hint` for what that pool can actually buy. "
      "The same conditions also show up inside hoi4_overview `alerts` as PP_CAPPING and "
      "XP_READY(<service>), so the per-turn checklist sees them without an extra call.",
      TAG)
def _thresholds(a):
    return game().thresholds(a.get("tag"))


@tool("hoi4_housekeep",
      "ONE-CLICK RED-DOT CLEANUP: fill every empty research slot, fill the construction "
      "queue to >=6 factories, put idle military factories into the main infantry/artillery/"
      "tank lines, spend overflowing political power on positive decisions, fold "
      "unassigned divisions into an army with a commander, give the air wings a mission "
      "AND attach them to the armies, and give every idle fleet a Patrol mission. "
      "Every sub-step is independent and reports its own result.",
      dict(TAG, target_construction=_INT,
           technologies={"type": "array", "items": _STR,
                         "description": "Preferred research tech ids"}))
def _housekeep(a):
    return game().housekeep(a.get("tag"), a.get("target_construction"),
                            a.get("technologies") or [])


@tool("hoi4_status", "Connection status: process, version, date, country count, "
                    "and how many ptrace injections this session has made. "
                    "`date` is the SAME dict hoi4_date returns (both call Game.date()), "
                    "so the two can never disagree -- if they look different, the "
                    "value changed between the calls, not the code path.",
                    )
def _status(a):
    g = game()
    out = {"pid": g.p.pid, "image_base": hex(g.p.base), "version": O.VERSION,
           "date": g.date(), "player": g.player_tag(), "countries": len(g.tags()),
           "injections": getattr(g.p, "call_count", None),
           "winject_recovery": _WINJECT_RECOVERY}
    try:
        sp = g.game_speed()
        out["game_speed"] = sp.get("speed")
        out["game_speed_ui"] = sp.get("ui_speed")
    except Exception:
        pass
    try:
        import session as _sess
        out["paused"] = _sess.is_paused(g)
    except Exception:
        pass
    # 2026-11-19: a minimised window freezes the simulation (5 h wall = 0 game
    # days measured).  Surface it here so no observation is made blind again.
    try:
        import windowstate
        out["window"] = windowstate.state(g.p.pid)
    except Exception as e:
        out["window"] = {"error": repr(e)}
    if sys.platform == "win32":
        try:
            import profile as _prof
            out["profile"] = _prof.summary()
            out["profile_live"] = _prof.verify_live_process(g.p)
        except Exception as e:
            out["profile"] = {"error": repr(e)}
        try:
            import assets as _assets
            out["assets"] = _assets.summary()
        except Exception as e:
            out["assets"] = {"error": repr(e)}
    try:
        import reddots as _rd
        out["red_dots"] = _rd.summary(g)
    except Exception as e:
        out["red_dots"] = {"error": repr(e)}
    return out


@tool("hoi4_get_country",
      "Summary of one country (factories, political power, stability, war support, "
      "manpower...).",
      {"tag": _STR}, ["tag"])
def _get_country(a):
    g = game()
    with g:
        return g.country(a["tag"]).summary()


@tool("hoi4_list_countries", "Country tags; only_majors=true returns a summary of the major powers.",
      {"only_majors": _BOOL, "limit": _INT})
def _list_countries(a):
    g = game()
    if a.get("only_majors"):
        majors = ["GER", "ENG", "SOV", "USA", "JAP", "ITA", "FRA"]
        with g:
            return {t: g.country(t).summary() for t in majors if t in g.tags()}
    tags = g.tags()
    return {"count": len(tags), "tags": tags[:a.get("limit")] if a.get("limit") else tags}


@tool("hoi4_get_modifiers",
      "Every modifier currently active on the country (national spirits, ideas, tech "
      "bonuses). `totals` is the flat sum; `by_source` attributes each modifier to the "
      "national spirit / law that carries it (read from each active CIdea's own modifier "
      "list); `unattributed` lists the ones that come from tech, focus effects, "
      "occupation or difficulty, which are not walked here.", TAG)
def _mods(a):
    c = game().country(a.get("tag") or game().player_tag())
    tot = c.modifiers()
    try:
        src = c.modifier_sources()
    except Exception as e:
        return {"totals": tot, "by_source_error": repr(e)}
    # 2026-11-21 (P1-4): `unattributed` only listed keys ABSENT from by_source,
    # so a key that was only PARTLY explained never showed up.  Measured live:
    #   production_speed_arms_factory_factor  totals 0.35  war_economy 0.20
    #   track_mastery_gain_factor             totals 0.05  four sources = 0.20
    #   unit_motorized_design_cost_factor     in by_source, MISSING from totals
    # Report the arithmetic instead of hiding it: per-key source sum, residual,
    # the partially-attributed list, and keys that exist only in by_source.
    sums, residual = {}, {}
    for k, rows in (src or {}).items():
        try:
            sums[k] = round(sum(float(r.get("value") or 0) for r in rows), 6)
        except Exception:
            continue
    for k, v in tot.items():
        if k not in sums:
            continue
        try:
            d = round(float(v) - sums[k], 6)
        except Exception:
            continue
        if abs(d) > 1e-9:
            residual[k] = d
    return {"totals": tot, "by_source": src, "by_source_sum": sums,
            "residual": residual,
            "unattributed": sorted(k for k in tot if k not in src),
            "partially_attributed": sorted(residual),
            "by_source_only": sorted(k for k in src if k not in tot),
            "note": ("residual = totals - sum(by_source); a non-zero residual is a "
                     "modifier that comes from tech / focus / occupation / "
                     "difficulty, which this reader does not walk.")}


# =============================================================== NATIONAL FOCUS
@tool("hoi4_focus_current",
      "The national focus in progress (including progress/days_left/percent), banked focus days, "
      "and the size of the focus tree.", TAG)
def _focus_cur(a):
    g = game()
    tag = a.get("tag")
    fp = g.focus_progress(tag)
    return {
        "current": fp.get("current"),
        "progress": fp.get("progress"),
        "days_done": fp.get("days_done"),
        "cost_days": fp.get("cost_days"),
        "days_left": fp.get("days_left"),
        "percent": fp.get("percent"),
        "banked_days": fp.get("banked_days"),
        "completed": fp.get("completed"),
        "tree_size": len(g.focus_tree(tag)),
    }


@tool("hoi4_focus_list",
      "Focuses in the country's tree. only_startable=true returns those with no unmet "
      "prerequisite. filter searches key and name.",
      dict(TAG, only_startable=_BOOL, filter=_STR, limit=_INT))
def _focus_list(a):
    g = game()
    out = g.focuses(a.get("tag"), only_startable=a.get("only_startable", False))
    engine_checked = False
    # 2026-11-21 (P2-6): `only_startable` only applies the STATIC prerequisites
    # parsed from the focus files, while hoi4_selectable_focuses asks the engine's
    # own CSetNationalFocusCommand::CanExecute (division ratios, year, war state).
    # The 2026-11-20 playtest saw GER_anschluss listed here and blocked there in
    # the same minute.  When the caller asks for "startable", ask the engine too.
    if a.get("only_startable"):
        try:
            sel = g.selectable_focuses(a.get("tag"))
            allowed = {x["id"] for x in (sel.get("selectable") or [])}
            if sel.get("engine_checked") and allowed:
                out = [x for x in out if x["key"] in allowed]
                engine_checked = True
        except Exception:
            engine_checked = False
    f = (a.get("filter") or "").lower()
    if f:
        out = [x for x in out if f in (x["key"] or "").lower() or f in (x["name"] or "").lower()]
    lim = a.get("limit") or 200
    return {"count": len(out), "engine_checked": engine_checked,
            "focuses": [{k: v for k, v in x.items() if k != "ptr"}
                        for x in out[:lim]]}


@tool("hoi4_set_focus", "Start a national focus (same as clicking it in the focus tree).",
      dict(TAG, key=_STR), ["key"])
def _set_focus(a):
    # 2026-11-21 (续测 P2-2): an unknown key raised KeyError straight out of the
    # focus dictionary, so the caller got a raw Python traceback and a Turkish
    # "HATA:'focus not found: ...'" instead of a usable answer.  Answer like an
    # API: ok:false plus the key that was not found.
    try:
        return game().set_focus(a["key"], a.get("tag"))
    except KeyError as e:
        return {"ok": False, "key": a["key"],
                "error": "找不到该国家焦点: %s (先跑 hoi4_focus_list 或 "
                         "hoi4_selectable_focuses 拿准确的 key)" % (e,)}


@tool("hoi4_set_focus_ptr",
      "Start a national focus by its LIVE pointer (the ptr from hoi4_selectable_focuses). "
      "Use this when hoi4_set_focus returns ok:false for a focus the game lists as selectable.",
      dict(TAG, ptr=_STR), ["ptr"])
def _set_focus_ptr(a):
    return game().set_focus_ptr(_ptr(a["ptr"]), a.get("tag"))


@tool("hoi4_drop_focus", "Cancel the focus in progress.", TAG)
def _drop_focus(a):
    return game().drop_focus(a.get("tag"))


# ================================================================= RESEARCH
@tool("hoi4_research", "Research slots: what is being researched, progress, speed.",
      dict(TAG, limit=_INT))
def _research(a):
    rows = game().research_slots(a.get("tag"))
    # 2026-11-21 (续测 P2-3): the schema never declared `limit`, so a caller
    # passing limit=1 still got all four slots back and the parameter looked
    # silently ignored.  Declared in the schema and honoured here.
    lim = a.get("limit")
    return rows[:int(lim)] if lim else rows


@tool("hoi4_research_available",
      "Technologies researchable RIGHT NOW. PURE READ: static tech tree (start_year + "
      "prerequisite paths) plus the live researched/occupied slots; zero injected calls. "
      "The final legality check is done by the game's CanExecute when starting research.",
      dict(TAG, filter=_STR))
def _research_avail(a):
    out = game().available_research(a.get("tag"))
    f = (a.get("filter") or "").lower()
    if f:
        out = [x for x in out if f in x.lower()]
    return {"count": len(out), "technologies": out}


@tool("hoi4_start_research",
      "Start researching a technology. Without slot, the first free slot is used.",
      dict(TAG, technology=_STR, slot=_INT), ["technology"])
def _start_research(a):
    return game().start_research(a["technology"], a.get("slot"), a.get("tag"))


@tool("hoi4_fill_research",
      "FILL EVERY EMPTY RESEARCH SLOT. Tries the given technologies first; if one is "
      "refused it automatically tries the next score-sorted candidate until all slots "
      "are really occupied (verified by reading the slot memory back).",
      {"technologies": {"type": "array", "items": _STR,
                        "description": "Preferred tech ids (optional)"},
       "max_attempts": _INT, **TAG})
def _fill_research(a):
    return game().fill_all_research_slots(a.get("technologies") or [],
                                           a.get("tag"), a.get("max_attempts", 30))


@tool("hoi4_researched", "Technologies already researched.", TAG)
def _researched(a):
    out = game().researched(a.get("tag"))
    return {"count": len(out), "technologies": out}


@tool("hoi4_available_ideas",
      "Law candidates pre-filtered IN PYTHON (zero game calls, zero ptrace). "
      "each law is gated by the war_support value in common/ideas (low 0.15, "
      "partial 0.25, war 0.50, extensive_conscription 0.20); the late-war tiers "
      "also require a war. 'legal' is a pre-filter "
      "verdict -- the authoritative CanExecute runs once when hoi4_add_idea posts.",
      dict(TAG, filter=_STR))
def _available_ideas(a):
    out = game().recommended_ideas(a.get("tag"))
    f = (a.get("filter") or "").lower()
    if f:
        out["ideas"] = [i for i in out["ideas"] if f in i["id"].lower()]
    out["count"] = len(out["ideas"])
    return out


# ================================================================== DECISIONS
@tool("hoi4_decisions",
      "Decisions with what a player actually needs: localised title, category, "
      "political-power cost, run time, one-shot flag and a readable slice of the "
      "`available` trigger. which: visible (the decision tab) | takeable (candidate list) | "
      "active (running) | all (every decision valid for the country). "
      "mine_only defaults to true so hoi4_decisions matches hoi4_overview and turnengine; "
      "pass mine_only=false to inspect foreign-scoped rows as well.",
      dict(TAG, which=_STR, filter=_STR, limit=_INT,
           mine_only={"type": "boolean",
                      "description": "Default true: keep only decisions belonging to this country "
                                     "(pass false for raw unfiltered engine list)"},
           rich={"type": "boolean",
                 "description": "false = legacy output (a list of keys)"}))
def _decisions(a):
    g = game()
    which = a.get("which", "visible")
    rows_all = g.decisions(a.get("tag"), which)
    mine_only = a.get("mine_only", True)
    rows = [x for x in rows_all if x.get("mine", True)] if mine_only else list(rows_all)
    f = (a.get("filter") or "").lower()
    if f:
        rows = [x for x in rows
                if f in x["key"].lower() or f in (x.get("title") or "").lower()]
    lim = a.get("limit") or 150
    shown = rows[:lim]
    if not a.get("rich", True):
        keys = [x["key"] for x in shown]
        return {"which": which, "mine_only": bool(mine_only),
                "total_unfiltered": len(rows_all), "count": len(rows),
                "decisions": keys}
    return {"which": which, "mine_only": bool(mine_only),
            "total_unfiltered": len(rows_all), "count": len(rows),
            "decisions": shown, "keys": [x["key"] for x in shown]}


@tool("hoi4_select_decision", "Take or start a decision.", dict(TAG, key=_STR), ["key"])
def _select_decision(a):
    return game().select_decision(a["key"], a.get("tag"))


# ================================================================== POLITICS
@tool("hoi4_politics", "Political power, ruling party support, active ideas and laws.", TAG)
def _politics(a):
    return game().politics(a.get("tag"))


@tool("hoi4_advisors",
      "Advisor / officer-corps roles (political_advisor, army_chief, navy_chief, air_chief, "
      "theorist, high_command) with exact trait modifiers parsed from the game's own files. "
      "army_xp_per_day = experience_gain_army (daily army experience).",
      {"slot": _STR, "tag": _STR})
def _advisors(a):
    import advisors as _adv
    return _adv.advisor_table(a.get("tag") or "GER", a.get("slot"))


@tool("hoi4_ideas_available", "Ideas, laws and advisors defined for the country (narrow with filter).",
      dict(TAG, filter=_STR, limit=_INT))
def _ideas(a):
    g = game()
    out = g.available_ideas(a.get("tag"), a.get("filter", ""))
    lim = a.get("limit") or 150
    return {"count": len(out), "ideas": [x["key"] for x in out[:lim]]}


@tool("hoi4_add_idea", "Apply an idea, law or advisor through the same command a "
      "politics-tab click uses. Mutually-exclusive laws go through CReplaceIdeaCommand "
      "(old law + new law), national spirits through CAddIdeaCommand. CanExecute checks "
      "PP/prerequisites and deducts the cost; the result says method=replace/add.",
      dict(TAG, key=_STR, idea_id=_STR))
def _add_idea(a):
    import verify as _v
    g = game()
    key = a.get("idea_id") or a.get("key")
    if not key:
        return {"ok": False, "error": "missing required parameter: key or idea_id"}
    r = g.add_idea(key, a.get("tag"))
    if isinstance(r, dict) and r.get("ok") is False:
        return r
    return verified(r, lambda: _v.idea_active(g, key, a.get("tag")), key)


@tool("hoi4_province_ptr",
      "DEBUG: CProvince* for a province id (calls the engine's GetProvince).",
      {"province": _INT}, ["province"])
def _prov_ptr(a):
    g = game()
    ptr = g.province_ptr(int(a["province"]))
    return {"province": a["province"], "ptr": hex(ptr) if ptr else None}


@tool("hoi4_hire_advisor",
      "Recruit a political advisor / chief of staff / theorist through "
      "CAddAdvisorCommand (advisors are not CIdeas, so hoi4_add_idea cannot do it). "
      "key = character key (GER_heinz_guderian), idea_token or bare name.",
      dict(TAG, key=_STR, slot=_STR), ["key"])
def _hire_advisor(a):
    return game().hire_advisor(a.get("key"), a.get("tag"), a.get("slot"))


@tool("hoi4_remove_idea", "Remove an active idea.", dict(TAG, key=_STR), ["key"])
def _remove_idea(a):
    return game().remove_idea(a["key"], a.get("tag"))


# =================================================================== PRODUCTION
@tool("hoi4_production",
      "Production lines: equipment, assigned factories, unit cost, daily output.", TAG)
def _production(a):
    return game().production_lines(a.get("tag"))


@tool("hoi4_set_production_amount",
      "Set how many factories a production line gets. Verifies the change actually landed. "
      "When increasing military factories with 0 idle, auto_reclaim=true (default) automatically "
      "reclaims spare factories from severely over-stockpiled surplus lines first and reports `reclaimed_from`.",
      dict(TAG, line=_INT, amount=_INT,
           auto_reclaim={"type": "boolean",
                         "description": "Auto-reclaim factories from surplus lines when idle=0 (default true)"}),
      ["line", "amount"])
def _set_prod(a):
    return game().set_production_amount(
        a["line"], a["amount"], a.get("tag"),
        auto_reclaim=bool(a.get("auto_reclaim", True)),
    )


@tool("hoi4_set_production_line_equipment",
      "SWAP an existing production line to a new equipment variant (CA换装). Keeps "
      "the line's factories; the game resets efficiency as it does in the UI. "
      "Reads the line back and only reports ok when the variant really changed.",
      dict(TAG, line=_INT, equipment=_STR, dry_run=_BOOL), ["line", "equipment"])
def _set_prod_equipment(a):
    return game().set_production_line_equipment(a["line"], a["equipment"],
                                                a.get("tag"), a.get("dry_run", False))


@tool("hoi4_add_production_line",
      "Create a NEW production line for an equipment variant, exactly like clicking "
      "it in the production tab: the game takes 1 free military factory for it. "
      "Goes through the engine's own CAddProductionLineCommand ctor, runs its "
      "CanExecute first and only then posts. Refuses (already_exists) when a line "
      "for that equipment is already there — change its factory count instead. "
      "A brand-new line starts at 0% efficiency, so output_per_day is low at first.",
      dict(TAG, equipment=_STR, dry_run=_BOOL), ["equipment"])
def _add_prod(a):
    return game().create_production_line(a["equipment"], a.get("tag"),
                                         a.get("dry_run", False))


@tool("hoi4_unlock_doctrine",
      "Unlock a 1.19 GRAND DOCTRINE by name (mobile_warfare / superior_firepower / "
      "grand_battleplan / mass_assault / fleet_in_being / convoy_raiding / base_strike "
      "/ battlefield_support / operational_integrity / strategic_destruction / "
      "special_forces_quantity / special_forces_quality). Spends the game's own army/"
      "navy/air XP; success is verified in the country's doctrine store.",
      {"doctrine": dict(_STR, description="Doctrine key"), **TAG}, ["doctrine"])
def _unlock_doctrine(a):
    return game().unlock_doctrine(a["doctrine"], a.get("tag"))


@tool("hoi4_remove_production_line",
      "Remove a production line entirely (the line's factories go back to the pool). "
      "Reports before/after line counts and which equipment the removed line was "
      "making; ok is only true when the list really shrank.",
      dict(TAG, line=_INT), ["line"])
def _rm_prod(a):
    return game().remove_production_line(a["line"], a.get("tag"))


@tool("hoi4_equipment_types", "Equipment types in the game (narrow with filter).",
      {"filter": _STR, "limit": _INT})
def _eq(a):
    out = game().equipment_types(a.get("filter", ""))
    lim = a.get("limit") or 150
    return {"count": len(out), "equipment": out[:lim]}


# =================================================================== CONSTRUCTION
@tool("hoi4_construction", "Construction queue: building, state, state_name, factories, progress, status.", TAG)
def _construction(a):
    return game().construction_queue(a.get("tag"))


@tool("hoi4_building_types", "Buildable building types and their ids.")
def _bt(a):
    return game().building_types()


@tool("hoi4_add_construction",
      "Queue a building (e.g. state=51, building='industrial_complex'). State ids from "
      "hoi4_states, building names from hoi4_building_types.",
      dict(TAG, state=_INT, building=_STR, level=_INT, province=_INT), ["state", "building"])
def _add_c(a):
    return game().add_construction(a["state"], a["building"], a.get("level", 1),
                                   a.get("province", 0), a.get("tag"))


@tool("hoi4_remove_construction", "Remove an item from the construction queue.",
      dict(TAG, index=_INT), ["index"])
def _rm_c(a):
    return game().remove_construction(a["index"], a.get("tag"))


@tool("hoi4_states", "Ids of the states the country owns.", TAG)
def _states(a):
    return {"states": [s["id"] for s in game().states(a.get("tag"))]}


# ==================================================================== TIME
@tool("hoi4_date", "Current in-game date.")
def _date(a):
    return game().date()


@tool("hoi4_set_game_speed",
      "Set game speed (0 = slowest / UI 1-bar .. 3 = UI 4-bar, which is the tool hard cap; "
      "4 and 5 are clamped to engine 3 / UI speed 4 because UI speed 5 runs uncapped on CPU "
      "and overshoots red-dot brakes). Writes CCurrentGameState+0x4BC directly "
      "(0-injection, works while paused or backgrounded) and verifies by readback. "
      "When running (unpaused), also samples live clock rate (~2.2s) for "
      "`measured_rate` (days/sec); pass `measure_seconds` (0..20) to override.",
      {"speed": _INT,
       "measure_seconds": {"type": "number",
                           "description": "Optional clock sampling seconds (0 = skip, "
                                          ">0 = sample live days/sec; if paused, briefly "
                                          "unpauses and restores pause)."},
       "use_command": dict(_BOOL, description="Optional: send CSetGameSpeedCommand instead of direct write.")},
      ["speed"])
def _speed(a):
    return game().set_game_speed(
        a["speed"],
        measure_seconds=a.get("measure_seconds"),
        use_command=bool(a.get("use_command", False)),
    )


# 2026-11-18: the two limits that used to live only in AGENTS.md as advice.
# Two of the three 2026-11-17 crashes came from repeated pause_in_hours calls
# (one of them USER32.dll+0x35fd3), and a call that the client gave up on keeps
# running server-side -- so the next write lands on top of it.
MAX_PAUSE_HOURS = 300
PAUSE_COOLDOWN_S = 20.0
_PAUSE_LAST = [0.0]


def _is_pid_alive(pid):
    """检查目标游戏进程是否仍在运行（Windows OpenProcess+GetExitCodeProcess / POSIX kill(0)）。"""
    try:
        pid = int(pid or 0)
        if pid <= 0:
            return False
    except Exception:
        return False
    if sys.platform == "win32":
        try:
            import ctypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            k32 = ctypes.windll.kernel32
            h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not h:
                return False
            try:
                code = ctypes.c_ulong(0)
                if not k32.GetExitCodeProcess(h, ctypes.byref(code)):
                    return False
                return int(code.value) == STILL_ACTIVE
            finally:
                k32.CloseHandle(h)
        except Exception:
            return True
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _looks_like_dead_read(err_text):
    s = str(err_text or "")
    return any(k in s for k in ("OSError(299", "WinError 299", "ERROR_PARTIAL_COPY", "process not found"))


@tool("hoi4_pause_in_hours",
      "Run for N in-game hours, then pause. For turn-based play. Hard cap 300 hours "
      "per call, and a 20 s cooldown is enforced after every call (both limits exist "
      "because repeated use crashed the game on 2026-11-17).",
      {"hours": _INT}, ["hours"])
def _pause(a):
    """Run N in-game hours, then pause — WITHOUT the debug console.

    2026-11-17: this used to be `g.console("pause_in_hours N")`. That is a
    developer-console command, which the campaign brief forbids, and the game
    crashed on the 1938.06.01 monthly tick after it had been used repeatedly
    (RVA 0xB2E62E, see research §91.5). It is now a plain wall-clock poll on the
    date field plus `session.pause()` (a direct idler+0x6C1 write), so nothing
    foreign is executed inside hoi4.exe.

    2026-11-18: the "single call <= 300 h, wait 20 s after a timeout" rule used
    to be a note in AGENTS.md that a model could forget. It is enforced here.
    """
    import time as _t
    import session as _session
    g = game()
    hours = int(a["hours"])
    if hours <= 0:
        return {"result": {"ok": False, "posted": 0, "hours_requested": hours,
                           "error": "hours must be > 0"}, "date": g.date()}
    if hours > MAX_PAUSE_HOURS:
        return {"result": {"ok": False, "posted": 0, "hours_requested": hours,
                           "max_hours": MAX_PAUSE_HOURS,
                           "error": ("hours=%d is over the hard cap of %d per call. "
                                     "Split it into several calls; the cap and the "
                                     "20 s cooldown exist because repeated "
                                     "pause_in_hours calls crashed the game twice on "
                                     "2026-11-17." % (hours, MAX_PAUSE_HOURS))},
                "date": g.date()}
    waited = 0.0
    gap = _t.time() - _PAUSE_LAST[0]
    if gap < PAUSE_COOLDOWN_S:
        waited = round(PAUSE_COOLDOWN_S - gap, 1)
        _t.sleep(waited)
    start = int(g.date()["raw"])
    deadline = start + hours
    _bad = _refuse_unpause_with_empty_orders(g)
    if _bad:
        return {"result": dict(_bad, hours_requested=hours, posted=0),
                "date": g.date()}
    try:
        import reddots as _rd
        _rd_gate = _rd.check_unpause_gate(g, ensure_paused=True)
        if _rd_gate:
            return {"result": dict(_rd_gate, hours_requested=hours, posted=0),
                    "date": g.date()}
    except Exception:
        pass
    ok_un, msg_un = _session.pause(g, False)
    if not ok_un:
        # 2026-11-21 (P0-3): resume can legitimately fail while a modal popup holds
        # the game (the pause byte is 0 but the clock stays frozen).  Say so at
        # once instead of polling a clock that cannot move for 10 s and then
        # reporting "hours_run: 0" as if the run had happened.
        return {"result": {"ok": False, "posted": 0, "hours_requested": hours,
                           "unpause": msg_un,
                           "error": ("could not resume the game: %s"
                                     % (msg_un or "unknown reason"))},
                "date": g.date()}
    t0 = _t.time()
    cur, prev = start, start
    stalled = 0
    # 2026-11-17 rewrite, three problems with the old loop:
    #   * it polled once per SECOND for up to 600 s and held the whole MCP server
    #     hostage the entire time (every other tool timed out);
    #   * `hours` never mapped to game hours -- a single 1-hour poll could span
    #     anything from 1 hour to 37 days depending on the speed setting;
    #   * it could leave the game UNPAUSED (`pause()` returned
    #     "wrote idler+0x6C1 but it reads False") and there was no retry.
    # Now: 0.25 s poll, 120 s wall cap, stall detector, and re-pause is retried.
    # 105 s, not 120: the MCP client gives up at ~110 s and then the call keeps
    # running server-side, so a 120 s cap guarantees a -32001 on every long run
    # and the next write lands on top of a still-running one.
    # 2026-11-21 (P1-1): the 2026-11-20 playtest measured the client abandoning
    # calls after ~60 s, so 105 s still produced -32001 on every long run.  Cap
    # well inside that budget and return a usable partial result instead: the
    # caller can simply call again, and a running write never outlives its reply.
    WALL_CAP = 45.0
    STALL_POLLS = 40          # 10 s with zero game time -> something is blocking
    read_err = None
    while _t.time() - t0 < WALL_CAP:
        try:
            cur = int(g.date()["raw"])
        except Exception as _re:
            read_err = repr(_re)
            break
        if cur >= deadline:
            break
        stalled = stalled + 1 if cur == prev else 0
        prev = cur
        if stalled >= STALL_POLLS:
            break
        _t.sleep(0.25)
    ok_p, msg_p = False, "not attempted"
    pid_now = getattr(getattr(g, "p", None), "pid", None)
    if _is_pid_alive(pid_now):
        for _ in range(3):
            try:
                ok_p, msg_p = _session.pause(g, True)
            except Exception as e:
                ok_p, msg_p = False, repr(e)
            if ok_p or not _is_pid_alive(pid_now):
                break
            _t.sleep(1.0)
    else:
        msg_p = read_err or ("process pid=%s has terminated" % pid_now)
    # stamp the cooldown from the END of the run: the next call must not post a
    # write on top of a call the client already gave up on
    _PAUSE_LAST[0] = _t.time()
    _dt = cur - start
    out = {"ok": bool(ok_p), "hours_requested": hours,
           "hours_run": _dt,
           "days_run": round(_dt / 24.0, 2),
           "unpause": msg_un, "pause": msg_p,
           "waited_for_cooldown_s": waited,
           "wall_cap_hit": _t.time() - t0 >= WALL_CAP,
           "stalled": stalled >= STALL_POLLS}
    if read_err:
        out["read_error"] = read_err
    # 2026-11-19: the date address can change MID-CALL (measured: -499 h and
    # +948 h on a 300 h request).  When that happens the delta is meaningless,
    # so say so instead of reporting a negative or 3x number as if it were real.
    if _dt < 0 or (hours and _dt > hours * 8):
        out["clock_unreliable"] = True
        out["hours_run"] = None
        out["days_run"] = None
        out["hint"] = ("the in-game clock JUMPED during this call (%d -> %d, "
                       "delta %s h on a %d h request): the date address drifted. "
                       "Treat this call as 'advanced an unknown amount' and read "
                       "hoi4_date.source / log_raw before trusting any date."
                       % (start, cur, _dt, hours))
    if out["hours_run"] is not None and out["hours_run"] < hours and not out["stalled"]:
        out.setdefault("hint", ("only %d of %d hours elapsed (wall cap %.0f s). The clock is "
                                "fine -- call again for the rest; the 20 s cooldown is "
                                "automatic." % (out["hours_run"], hours, WALL_CAP)))
    # 2026-11-19: a MINIMISED window freezes the simulation outright -- measured
    # 5 h wall clock = 0 game days, then 100 s after restoring = 47 game days.
    # Several "nothing happened" observations in this project were that, not the
    # engine, so every clock run now reports where the window is.
    try:
        import windowstate
        out["window"] = windowstate.state(pid_now)
    except Exception as _we:
        out["window"] = {"error": repr(_we)}

    # 2026-11-23 (复测 9 E-4): 优先检测进程是否已崩溃/终止（OSError 299 / pid 不再存活 / hwnd=null），
    # 严禁在进程已死时误报“大概是事件弹窗”！
    _win = out.get("window") or {}
    proc_dead = (
        (not _is_pid_alive(pid_now))
        or (
            (_looks_like_dead_read(read_err) or _looks_like_dead_read(msg_p))
            and _win.get("hwnd") is None
        )
    )
    if proc_dead:
        out["ok"] = False
        out["process_dead"] = True
        out["blocked_by"] = "process_dead"
        out["error"] = "hoi4.exe (pid=%s) has terminated or crashed during hoi4_pause_in_hours" % pid_now
        out["hint"] = (
            "HOI4 进程（pid=%s）已退出/崩溃（并非事件弹窗阻塞）。"
            "请检查 Documents/Paradox Interactive/Hearts of Iron IV/crashes 或 Windows 事件日志，"
            "重启游戏并加载最近存档。" % pid_now
        )
        return {"result": out, "date": None}

    if stalled >= STALL_POLLS:
        if _win.get("minimized") or _win.get("visible") is False:
            out["blocked_by"] = ("window_minimized" if _win.get("minimized")
                                 else "window_hidden")
            out["hint"] = ("the HOI4 window is %s -- a minimised/hidden window freezes "
                           "the simulation completely (measured: 5 h wall clock = 0 "
                           "game days, 100 s after restoring = 47 game days). Restore "
                           "the window and call again."
                           % ("minimised" if _win.get("minimized") else "hidden"))
        else:
            out["hint"] = ("the clock did not move for 10 s -- a World-News / event "
                           "dialog is almost certainly open and is holding the clock "
                           "(window focused=%s title=%r). Read `pending(g)` and "
                           "answer it in game."
                           % (_win.get("focused"), _win.get("title")))
    if not ok_p:
        out["hint"] = (out.get("hint", "") +
                       " WARNING: the game may still be RUNNING unpaused.")
    try:
        date_now = g.date()
    except Exception:
        date_now = None
    return {"result": out, "date": date_now}


@tool("hoi4_screenshot",
      "Screenshot, for visual confirmation. Does NOT raise or focus the game window by "
      "default, so it never interrupts the player. focus=true brings the window forward. "
      "crop example: '620x30+0+0'. An UNCROPPED shot is downscaled to <=1600 px wide and "
      "<=1.8 MB so the client can always render it; pass crop when you need to read small "
      "text (a crop keeps full resolution).",
      {"crop": _STR, "focus": _BOOL})
def _shot(a):
    data = screenshot(a.get("crop"), a.get("focus", False))
    out = {"_image": base64.b64encode(data).decode(), "bytes": len(data)}
    try:
        from PIL import Image as _Im
        import io as _io
        with _Im.open(_io.BytesIO(data)) as _im:
            out["width"], out["height"] = _im.width, _im.height
    except Exception:
        pass
    out["cropped"] = bool(a.get("crop"))
    # 2026-11-21 (P2-6): a minimised window has a 160x30 "iconic" rect, so the
    # capture is a black strip of a few hundred bytes.  Say that instead of
    # handing the caller a picture that looks like a black screen.
    try:
        import ctypes as _ct
        _hwnd = _win_hwnd()
        out["minimized"] = bool(_hwnd and _ct.windll.user32.IsIconic(_hwnd))
    except Exception:
        out["minimized"] = None
    if out.get("minimized"):
        out["hint"] = ("游戏窗口已最小化：Windows 只能截到窗口矩形，所以拿到的是黑条。"
                       "把 HOI4 恢复（点任务栏）后再截图；focus=true 也会恢复窗口，"
                       "但那会把游戏提到前台，默认不做。")
    elif out.get("bytes", 0) < 2000 and not a.get("crop"):
        out["hint"] = ("截图只有 %d 字节，基本是黑屏：窗口可能被别的程序挡住或刚切走。"
                       % out.get("bytes", 0))
    return out


# ====================================================== GENIS RAPOR / STAT
@tool("hoi4_full_report",
      "The wide turn-start report: overview + resources + statistics + armies + division "
      "templates + fleets + command power + selectable focuses + faction.", TAG)
def _full(a):
    return game().full_report(a.get("tag"))


@tool("hoi4_country_stats",
      "Country summary plus the live checks that are cheap and exact: factories, PP, "
      "stability, war support, manpower, divisions, at_war, is_major. On the Windows "
      "build the old ~44 trigger statistics (casualties / surrender_progress / "
      "convoy_threat ...) are NOT available -- the field `unavailable` says which, and "
      "`source` says why. A missing field never means zero.",
      dict(TAG, raw=_BOOL))
def _cstats(a):
    return game().country_stats(a.get("tag"), a.get("raw", False))


@tool("hoi4_resources",
      "Strategic resources: steel, aluminium, rubber, tungsten, chromium, oil, coal. "
      "PE 1.19.5.0: CCountry+0x11F8 -> CCountryResources, seven int64 amounts from "
      "+0x60 at 0x10 stride (fixed point, 100000 = 1 unit). Fixed 2026-11-19 -- "
      "until then CO_RESOURCES pointed at the Linux offset 0x11C0 and every value "
      "read 0.0.", TAG)
def _res(a):
    out = game().resources(a.get("tag"))
    if isinstance(out, dict) and out and not any(out.values()):
        out = dict(out)
        out["warning"] = ("all seven resources read 0.0 -- offsets may have moved "
                          "again; do not trust this as a shortage")
    return out


@tool("hoi4_script_variable",
      "Read ONE dynamic variable by name (e.g. 'command_power', 'manpower_k', "
      "'political_power'). Returns `known` (is the name in the engine's own "
      "registry), `value` when this build has a native reader for it, and "
      "`similar` names when the name does not exist. Call hoi4_dynamic_variables "
      "for the full authoritative list.\n\n"
      "Some names take an @argument and answer 0 WITHOUT it: "
      "'num_equipment_in_armies@infantry_equipment' (equipment held by divisions), "
      "'num_equipment@infantry_equipment' (in the stockpile), "
      "'num_of_civilian_factories_in_cores@GER' (my core states controlled by GER). "
      "An archetype name sums all its variants; an unknown name answers 0, exactly "
      "like the engine.\n\n"
      "via='native' (default) reads pure memory. via='console' additionally falls "
      "back to the developer command `get_var`, which reaches variables that have "
      "no native reader -- but that is a CHEAT path, use it only in tests, and "
      "never for pc_total_score/pc_current_score (they assert outside a peace "
      "conference and kill the process).",
      dict(TAG, name=_STR,
           via=dict(_STR, description="native (default) | console (cheat fallback)")),
      ["name"])
def _svar(a):
    return game().script_variable(a["name"], a.get("tag"), a.get("via") or "native")


@tool("hoi4_dynamic_variables",
      "Every dynamic variable the engine's own registry knows (265 names on "
      "1.19.5.0 PE), read live from the static registry at RVA 0x33DF1D0. "
      "`native` = the subset hoi4_script_variable can return a value for. "
      "Use this instead of guessing a name: the registry is authoritative.",
      TAG)
def _dynvars(a):
    return game().dynamic_variables()


# ================================================================== ARMIES
@tool("hoi4_division_objects",
      "Raw division (CArmy) object list. Use hoi4_armies for army structure.", TAG)
def _divobjs(a):
    return game().armies(a.get("tag"))


@tool("hoi4_division_templates", "Division templates (Infanterie-Division and so on).", TAG)
def _dtempl(a):
    return game().division_templates(a.get("tag"))


@tool("hoi4_subunit_types",
      "Battalion types, used when creating or editing a division template.",
      dict(TAG, filter=_STR))
def _subunits(a):
    out = game().subunit_types(a.get("tag"), a.get("filter", ""))
    return {"count": len(out), "subunits": out}


@tool("hoi4_theatres", "Theatre objects.", TAG)
def _theatres(a):
    # 2026-11-21 (P3-5): used to return a bare [{ptr, class}] list with no hint
    # that nothing else is decoded.  Say so, and give the count.
    rows = game().theatres(a.get("tag"))
    return {"count": len(rows), "theatres": rows,
            "note": ("only the object pointer and RTTI class are decoded on the "
                     "PE build: no name / owner / division list. Use hoi4_armies "
                     "and hoi4_divisions for the order of battle.")}


@tool("hoi4_fleets", "Naval fleets.", TAG)
def _fleets(a):
    return game().fleets(a.get("tag"))


@tool("hoi4_air_wings",
      "The player's air wings with live state: planes in the wing, mission mask "
      "and whether that mission is enabled. Ownership is exact on the PE build: "
      "CAirWing+0x38 -> CAirWingPool+0x40 -> CStrategicAir+0x90 = CCountryTag id, "
      "so this returns only the player's own wings (GER 15 / ENG 36 in 1936) and "
      "includes wings created after game start. Pass all=true for a capped world "
      "census.",
      dict(TAG, all={"type": "boolean"}, limit={"type": "integer"}))
def _wings(a):
    return game().air_wings(a.get("tag"), show_all=bool(a.get("all")),
                            limit=int(a.get("limit") or 0))


@tool("hoi4_attach_air_wings",
      "Attach the player's air wings to the player's armies "
      "(CAttachAirWingToArmyCommand).  Ownership is now exact: "
      "CAirWing+0x38 -> CAirWingPool+0x40 -> CStrategicAir+0x90 = CCountryTag "
      "id, so only wings the player really owns are touched (measured 44 groups "
      "/ 44 distinct tags).  `allow_unverified` is accepted for compatibility "
      "and no longer changes anything.",
      dict(TAG, refresh={"type": "boolean"}, allow_unverified={"type": "boolean"}))
def _attach_air(a):
    # 2026-11-19: the 2026-11-16 "refuses by default" gate is gone. It was a
    # workaround for not knowing the wing -> country link; CStrategicAir+0x90
    # provides it, so both paths now agree and the verified PE path always runs.
    return _air_ger_attach(a)


@tool("hoi4_naval_task_forces",
      "List all naval fleets and their task forces (CTaskForce ptr, handle, ship count).",
      TAG)
def _naval_tfs(a):
    import sys
    g = game()
    if sys.platform == "win32":
        # 2026-11-16: navalops.py is all Linux offsets (fleet +0xB0, TF handle
        # at +0x18 as a pointer). On the PE build the fleet task-force array is
        # at +0xB8 and the TF handle at +0x18 resolves through 0x2284870.
        import navalops_win as NW
        try:
            fleets = NW.naval_fleets(g, a.get("tag"))
        except Exception as e:
            return {"ok": False, "error": repr(e),
                    "note": "PE naval scan failed"}
        tfs = [tf for f in fleets for tf in f["task_forces"]]
        return {
            "ok": True, "count": len(fleets), "fleets": fleets,
            "task_force_count": len(tfs),
            "valid_regions_count": len(NW.valid_regions(g)),
            "note": "PE 1.19.5.0: handle=packed {type,id}, mission=tf+0x374, "
                    "regions=fleet+0x54",
        }
    import navalops
    out = []
    for f in g.fleets(a.get("tag")):
        fptr = int(f["ptr"], 16)
        out.append({
            "fleet_index": f["index"],
            "fleet_name": f["name"],
            "fleet_ptr": f["ptr"],
            "task_forces": [
                {"index": tf["index"], "ptr": hex(tf["ptr"]), "handle": tf["handle"],
                 "ships": tf["ships"]}
                for tf in navalops.fleet_task_forces(g, fptr)
            ],
        })
    return {"count": len(out), "fleets": out}


@tool("hoi4_naval_regions",
      "List the game's strategic naval regions (id, display name, naval terrain, province "
      "count) and whether each one is USABLE right now. hoi4_naval_mission needs numeric "
      "region ids and an out-of-range id is a null dereference inside the engine, so this "
      "is the safe way to pick one. Read-only: the ids come from map/strategicregions/*.txt "
      "and the names from the game's own localisation; 'usable_now' comes from the live "
      "CStrategicNavalRegion table. Filter with name= (substring, case-insensitive) and/or "
      "usable_only=true.",
      {"name": _STR, "usable_only": _BOOL, "limit": _INT})
def _naval_regions(a):
    import navalregions as NR
    g = game()
    valid = None
    live_error = None
    try:
        import navalops_win as NW
        valid = NW.valid_regions(g)
    except Exception as e:
        live_error = "%s: %s" % (type(e).__name__, e)
    rows = NR.catalogue(valid)
    needle = (a.get("name") or "").strip().lower()
    if needle:
        rows = [r for r in rows
                if needle in (r.get("name") or "").lower()
                or needle in (r.get("name_key") or "").lower()]
    if a.get("usable_only"):
        rows = [r for r in rows if r.get("usable_now")]
    lim = int(a.get("limit") or 300)
    out = {"ok": True, "count": len(rows), "regions": rows[:lim],
           "usable_count": (None if valid is None else len(valid)),
           "note": "pass the id from here to hoi4_naval_mission(regions=[...]) "
                   "or hoi4_naval_mission(region_names=['North Sea'])"}
    if live_error:
        out["live_table_error"] = live_error
        out["note"] = ("live region table unreadable (%s) -- ids are still correct, "
                       "but 'usable_now' is null and hoi4_naval_mission will refuse "
                       "until the game is reachable" % live_error)
    return out


@tool("hoi4_naval_mission",
      "Assign a naval mission to a task force and add strategic naval region(s) to its fleet. "
      "mission: 0=None (clear/stop mission), 1=Patrol, 2=Strike Force, 3=Convoy Raiding, "
      "4=Convoy Escort, 9=Naval Invasion Support. On PE, tf_ptr may be a raw CTaskForce pointer, "
      "a packed handle, or the numeric handle id; fleet_ptr likewise. Region ids "
      "are validated against the game's own naval-region table before CanExecute "
      "(an out-of-range id crashes the engine). Call hoi4_naval_regions first to see "
      "the ids and names, or pass region_names=['North Sea'] and let it resolve them. "
      "Use hoi4_naval_remove_regions to unassign regions. dry_run=true builds and "
      "validates but does not post.",
      {"fleet_ptr": _STR, "tf_ptr": _STR, "mission": _INT,
       "regions": {"type": "array", "items": _INT},
       "region_names": {"type": "array", "items": _STR,
                        "description": "Region display names or name keys, resolved "
                                       "through the game's own files (e.g. 'North Sea')"},
       "dry_run": _BOOL},
      ["fleet_ptr", "tf_ptr", "mission"])
def _naval_mission(a):
    import sys
    g = game()

    # Resolve any names to ids up front: a wrong id is a NULL DEREF in the
    # engine's CanExecute (crash #5 on 2026-11-16), so this is the only place a
    # name is allowed to become a number. Shared by the Windows and Linux paths.
    import navalregions as NR
    req_regions = []
    for r in (a.get("regions") or []):
        try:
            req_regions.append(int(r))
        except Exception:
            return {"ok": False, "error": "bad region id: %r" % (r,)}
    for nm in (a.get("region_names") or []):
        rid = NR.lookup(nm)
        if rid is None:
            return {"ok": False,
                    "error": "unknown naval region %r -- call hoi4_naval_regions "
                             "to list the valid names" % (nm,)}
        if rid not in req_regions:
            req_regions.append(rid)

    if sys.platform == "win32":
        import navalops_win as NW
        p = g.p
        dry = bool(a.get("dry_run", False))

        def as_handle(s, kind):
            """raw pointer / packed handle / numeric id -> packed handle."""
            s = str(s)
            if s.startswith("0x") and len(s) > 10:
                ptr = _ptr(s)
                off = NW.FLEET_HANDLE_OFF if kind == "fleet" else NW.TF_HANDLE_OFF
                return p.u64(ptr + off), "raw_ptr"
            v = int(s, 0) if s.startswith("0x") else int(s)
            if v > 0xFFFFFFFF:
                return v, "packed"
            typ = 0x3D if kind == "fleet" else 0x33
            return (v << 32) | typ, "id"

        try:
            tfh, tf_kind = as_handle(a["tf_ptr"], "tf")
            fh, f_kind = as_handle(a["fleet_ptr"], "fleet")
        except Exception as e:
            return {"ok": False, "error": "bad fleet_ptr/tf_ptr: %s" % e}

        # resolve BOTH handles before any CanExecute (crash #4/#5 rule)
        tf_obj = NW.resolve_handle(g, tfh)
        f_obj = NW.resolve_handle(g, fh)
        if not tf_obj or tf_obj < 0x10000 or not f_obj or f_obj < 0x10000:
            return {"ok": False, "error": "fleet/tf handle does not resolve",
                    "tf_handle": hex(tfh), "fleet_handle": hex(fh),
                    "tf_obj": hex(tf_obj or 0), "fleet_obj": hex(f_obj or 0)}

        mission = int(a["mission"])
        cmd = NW.make_set_type(g, [tfh], mission)
        can = NW.can_exec(g, cmd, NW.CANEXEC_SET_TYPE)
        res = {
            "ok": False, "mode": "pe-player-command",
            "tf_handle": hex(tfh), "fleet_handle": hex(fh),
            "tf_input": tf_kind, "fleet_input": f_kind,
            "mission": mission, "mission_name": NW.MISSION_NAMES.get(mission),
            "can_execute": can, "dry_run": dry,
            "set_cmd": hex(cmd),
        }
        # Report region validity even in dry-run (crash #5 guard is the whole
        # point of pre-validating them).
        reg_preview = []
        for reg in req_regions:
            good, why = NW.region_is_valid(g, reg)
            reg_preview.append({"region": reg, "valid": good,
                                "detail": hex(why) if good else why})
        res["region_preview"] = reg_preview
        if not can:
            res["error"] = "CNavalMissionSetTypeCommand::CanExecute refused"
            return res
        if dry:
            res["ok"] = True
            res["note"] = "validated only; no command posted"
            return res

        # post THIS command (not a freshly built one) so nothing is duplicated
        r_post = NW.post(g, cmd, seconds=2.5)
        tf_ptr = tf_obj - 0x10          # resolver returns tf+0x10 on PE
        try:
            mission_after = p.i32(tf_ptr + NW.TF_MISSION_TYPE_OFF)
        except Exception:
            mission_after = None
        r_m = {"ok": bool(r_post.get("posted")) and mission_after == mission,
               "posted": r_post, "mission_after": mission_after,
               "mission": mission, "cmd": hex(cmd)}
        res["set_mission"] = r_m
        r_regs = []
        for reg in req_regions:
            ok, why = NW.region_is_valid(g, reg)
            if not ok:
                r_regs.append({"ok": False, "region": reg, "error": why,
                               "note": "refused before CanExecute (crash #5 guard)"})
                continue
            c = NW.make_add_region(g, fh, reg)
            cr = NW.can_exec(g, c, NW.CANEXEC_ADD_REGION)
            if dry or not cr:
                r_regs.append({"ok": bool(cr) and dry, "region": reg,
                               "can_execute": cr, "cmd": hex(c),
                               "note": "not posted" if dry else "CanExecute refused"})
                continue
            _posted = NW.post(g, c, seconds=2.5)
            r_regs.append({"ok": bool(_posted.get("posted")), "region": reg,
                           "can_execute": cr, "cmd": hex(c), "posted": _posted})
        res["regions"] = r_regs
        # 2026-11-19 (measured): a strategic naval REGION belongs to the FLEET
        # (`fleet+0x54`), not to the task force.  Asking for the same region on
        # a second TF of the same fleet is refused by the engine — that is
        # idempotency, not a failure of the call.  The old code folded it into
        # `ok:false` while ALSO leaving `refused_regions` empty, so the two
        # fields contradicted each other and the caller retried a no-op.
        guarded = [x for x in r_regs if "guard" in (x.get("note") or "")]
        refused = [x["region"] for x in r_regs
                   if not x.get("ok") and x not in guarded]
        res["ok"] = bool(r_m.get("ok"))
        res["refused_regions"] = [x["region"] for x in guarded] + refused
        if refused:
            res["note"] = ("region(s) %s refused — most likely already assigned "
                           "to this fleet (regions are per FLEET, not per task "
                           "force); the mission itself is set" % refused)
        return res
    import navalops
    fptr = _ptr(a["fleet_ptr"])
    tptr = _ptr(a["tf_ptr"])
    with g:
        r_m = navalops.set_mission(g, tptr, int(a["mission"]))
        r_regs = []
        for reg in req_regions:
            r_regs.append(navalops.add_region(g, fptr, int(reg)))
    return {"ok": bool(r_m.get("ok")) and all(x.get("ok") for x in r_regs),
            "set_mission": r_m, "regions": r_regs}


@tool("hoi4_naval_remove_regions",
      "Unassign strategic naval region(s) from a fleet through "
      "CNavalMissionRemoveRegionCommand. fleet_ptr may be a raw CFleet pointer, "
      "a packed handle or the numeric handle id. Region ids are validated "
      "before CanExecute; a region that is not currently assigned is refused "
      "by the engine. When regions_after reaches 0 and clear_mission_when_empty=true "
      "(default), all non-reserve task forces in that fleet also have their mission "
      "reset to 0 (none) so FUEL_ZERO / idle checks stay consistent. dry_run=true only validates.",
      {"fleet_ptr": _STR, "regions": {"type": "array", "items": _INT},
       "clear_mission_when_empty": _BOOL, "dry_run": _BOOL},
      ["fleet_ptr", "regions"])
def _naval_remove_regions(a):
    import sys
    if sys.platform != "win32":
        return {"ok": False, "error": "only implemented on the PE 1.19.5.0 backend"}
    import navalops_win as NW
    g = game()
    p = g.p
    if "fleet_ptr" not in a or a["fleet_ptr"] is None:
        return {"ok": False, "error": "missing required parameter: fleet_ptr"}
    try:
        s = str(a["fleet_ptr"])
        if s.startswith("0x") and len(s) > 10:
            fh = p.u64(_ptr(s) + NW.FLEET_HANDLE_OFF)
            kind = "raw_ptr"
        else:
            v = int(s, 0) if s.startswith("0x") else int(s)
            fh = v if v > 0xFFFFFFFF else ((v << 32) | 0x3D)
            kind = "packed" if v > 0xFFFFFFFF else "id"
    except Exception as e:
        return {"ok": False, "error": "invalid fleet_ptr %r: %s" % (a.get("fleet_ptr"), e)}
    obj = NW.resolve_handle(g, fh)
    if not obj or obj < 0x10000:
        return {"ok": False, "error": "fleet handle does not resolve",
                "handle": hex(fh)}
    ids = [int(x) for x in (a.get("regions") or [])]
    checked, refused = [], []
    for rid in ids:
        ok, why = NW.region_is_valid(g, rid)
        (checked if ok else refused).append(
            rid if ok else {"region": rid, "error": why})
    # CanExecute requires the region to be PRESENT in fleet+0x48. Report that
    # explicitly so a 'refused' result is diagnosable without a crash.
    assigned = []
    n = p.i32(obj + NW.FLEET_REGION_COUNT_OFF)
    d = p.u64(obj + NW.FLEET_REGION_DATA_OFF)
    if d > 0x10000 and n > 0:
        assigned = [p.u64(d + 8*k) for k in range(n)]
    cnt, arr, _ = NW.region_table(g)
    missing = [r for r in checked if p.u64(arr + 8*r) not in assigned]
    cmd = NW.make_remove_regions(g, fh, checked) if checked else 0
    can = NW.can_exec(g, cmd, NW.CANEXEC_REMOVE_REGION) if cmd else None
    res = {
        "ok": False, "mode": "pe-player-command",
        "fleet_handle": hex(fh), "fleet_input": kind,
        "fleet_ptr": hex(obj), "regions_requested": ids,
        "regions_valid": checked, "refused": refused,
        "assigned_region_objects": [hex(x) for x in assigned],
        "not_assigned": missing, "can_execute": can,
        "cmd": hex(cmd) if cmd else None,
        "dry_run": bool(a.get("dry_run", False)),
    }
    if not can:
        res["error"] = ("CNavalMissionRemoveRegionCommand::CanExecute refused "
                        "(every id must already be assigned to this fleet)")
        return res
    if a.get("dry_run", False):
        res["ok"] = True
        res["note"] = "validated only; no command posted"
        return res
    posted = NW.post(g, cmd, seconds=2.5)
    res["posted"] = posted
    try:
        res["regions_after"] = p.i32(obj + NW.FLEET_REGION_COUNT_OFF)
    except Exception:
        pass
    res["ok"] = bool(posted.get("posted"))
    clear_m = bool(a.get("clear_mission_when_empty", True))
    if res["ok"] and clear_m and res.get("regions_after") == 0:
        mc = NW.clear_fleet_missions(g, obj)
        res["mission_cleared"] = bool(mc.get("ok") and mc.get("cleared", 0) > 0)
        res["mission_clear_detail"] = mc
    return res


_AIR_DONE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "..", "calling", "air_attach.json")


def _air_session_fp(g):
    """获取极轻量对局指纹 (pid, gs_ptr)，防止同 PID 读档/重开新局沿用旧 air_attach.json。"""
    pid = getattr(getattr(g, "p", None), "pid", None)
    gs_ptr = None
    if sys.platform == "win32":
        try:
            import offsets_win as _ow
            gs_rva = getattr(_ow, "G_GAMESTATE", None)
            if gs_rva:
                gs_ptr = int(g.p.u64(int(g.p.base) + int(gs_rva)))
        except Exception:
            gs_ptr = None
    return pid, gs_ptr


def _air_done_load(pid=None, gs_ptr=None):
    """(wing, army) handle pairs this tool already attached (scoped to pid + game session)."""
    try:
        with open(_AIR_DONE_PATH, encoding="utf-8") as f:
            d = json.load(f)
        if pid is not None and d.get("pid") is not None and int(d["pid"]) != int(pid):
            return set()
        if gs_ptr is not None and d.get("gs_ptr") is not None and int(d["gs_ptr"]) != int(gs_ptr):
            return set()
        return set(d.get("pairs") or [])
    except Exception:
        return set()


def _air_done_save(pairs, pid=None, gs_ptr=None):
    tmp = "%s.%d.tmp" % (_AIR_DONE_PATH, os.getpid())
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"pid": pid, "gs_ptr": gs_ptr, "pairs": sorted(pairs)[-500:]}, f)
        os.replace(tmp, _AIR_DONE_PATH)
    except Exception:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass


def _live_army_wing_map(g, armies):
    """2026-11-22 (P3-4): 纯只读遍历每个陆军 COrdersGroup+0x70 (CPdxArray<CAirWing*>)，
    直接从游戏内存建立 {wing_ptr -> army_info} 权威映射，不再依赖易过期的本地缓存。
    """
    from game import _arr
    out = {}
    for og in armies:
        try:
            optr = int(og["ptr"], 16) if isinstance(og.get("ptr"), str) else int(og["ptr"])
            for wp in _arr(g.p, optr, 0x70):
                if wp:
                    out[int(wp)] = {"ptr": optr, "name": og.get("name"), "handle": og.get("handle")}
        except Exception:
            continue
    return out


@tool("hoi4_air_ger_attach",
      "Scan the player's own air wings (ownership via CStrategicAir+0x90) and attach "
      "them round-robin to land armies. Kept as an alias of hoi4_attach_air_wings.",
      TAG)
def _air_ger_attach(a):
    import sys
    g = game()
    if sys.platform == "win32":
        # 2026-11-16: airops.py is all Linux VAs. The PE path uses the MSVC
        # RTTI index + a pure-data CAttachAirWingToArmyCommand (verified live:
        # 13/15 starting wings attached, CanExecute flips 1->0 on re-attach).
        # 2026-11-19: ownership is exact now (CStrategicAir+0x90), so newly
        # created wings are attached too instead of only the id-1 block.
        # 2026-11-22 (P3-4): 优先用 COrdersGroup+0x70 实机内存判已挂载状态，
        # 彻底消除读档/重启后首调报假失败；若首趟全部 CanExecute 拒绝则清缓存+refresh 重试一次。
        import airops_win as AW
        _tag = g._c(a.get("tag")).tag
        _cur_pid, _cur_gs = _air_session_fp(g)
        explicit_refresh = a.get("refresh")

        def _attempt(do_refresh, clear_cache=False):
            info = AW.country_wings(g, _tag, refresh=bool(do_refresh))
            if not info.get("ok") or not info.get("wings"):
                return info, 0
            groups = g.order_groups().get("groups") or []
            armies = [og for og in groups if not og.get("is_army_group")]
            if not armies:
                return {"ok": False, "error": "no land armies to attach air wings to"}, 0
            live_map = _live_army_wing_map(g, armies)
            done = set() if clear_cache else _air_done_load(pid=_cur_pid, gs_ptr=_cur_gs)
            _dirty_done = bool(clear_cache)
            out, skipped = [], []
            can_exec_refusals = 0
            # 不能挂载到陆军集团军的空军机种（如海轰/战轰/海上巡逻机/运输机未手动挂载时）
            _UNATTACHABLE = ("nav_bomber", "naval_bomber", "strat_bomber",
                             "strategic_bomber", "maritime_patrol_plane", "transport_plane")
            unattached_idx = 0
            for w in info["wings"]:
                wptr = int(w["ptr"], 16)
                wh = int(w["handle"], 16)
                live_owner = live_map.get(wptr)
                if live_owner is not None:
                    # 内存实证该联队已挂载在我方陆军集团军（COrdersGroup+0x70），
                    # 幂等视为已完成，不再无谓拆挂或触发注入调用。
                    oh = live_owner.get("handle")
                    if wh and oh:
                        done.add("%d:%d" % (wh, (int(oh[1]) << 32) | (int(oh[0]) & 0xFFFFFFFF)))
                        _dirty_done = True
                    out.append({"id": w["id"], "type": w["type"],
                                "army": live_owner.get("name"),
                                "already_attached": True, "verified_from_memory": True})
                    continue
                if w["type"] in _UNATTACHABLE:
                    skipped.append({"id": w["id"], "type": w["type"],
                                    "reason": "wing_type_unsupported"})
                    continue
                og = armies[unattached_idx % len(armies)]
                unattached_idx += 1
                h = og["handle"]
                army_handle = (int(h[1]) << 32) | (int(h[0]) & 0xFFFFFFFF)
                if not wh or not army_handle:
                    skipped.append({"id": w["id"], "type": w["type"],
                                    "army": og["name"], "reason": "stale_handle"})
                    continue
                try:
                    cmd = AW.make_attach(g, [wh], army_handle)
                    if not AW.can_exec(g, cmd, AW.CANEXEC_ATTACH):
                        can_exec_refusals += 1
                        skipped.append({"id": w["id"], "type": w["type"],
                                        "army": og["name"],
                                        "reason": "can_execute_refused"})
                        continue
                    r = AW.post(g, cmd)
                except Exception as _ae:
                    skipped.append({"id": w["id"], "type": w["type"],
                                    "army": og["name"], "reason": "call_failed",
                                    "error": str(_ae)})
                    continue
                done.add("%d:%d" % (wh, army_handle))
                _dirty_done = True
                out.append({"id": w["id"], "type": w["type"], "army": og["name"],
                            "posted": r.get("posted"), "drained": r.get("drained")})
            if _dirty_done:
                _air_done_save(done, pid=_cur_pid, gs_ptr=_cur_gs)
            res = {"ok": len(out) > 0, "attached_count": len(out),
                   "attached": out, "skipped": skipped,
                   "german_group": info["sa"]}
            if not out:
                res["hint"] = ("No wing could be attached (all non-transport wings refused CanExecute "
                               "and were not found in any COrdersGroup+0x70 array).")
            return res, can_exec_refusals

        res, refusals = _attempt(bool(explicit_refresh), clear_cache=False)
        if not res.get("ok") and refusals > 0 and explicit_refresh is None:
            res_retry, _ = _attempt(True, clear_cache=True)
            res_retry["retried_after_refresh"] = True
            return res_retry
        return res
    import airops, armyops
    c = g._c(a.get("tag"))
    info = airops.ger_air_wings(g)
    if not info.get("ok") or not info.get("wings"):
        return info
    ogs = [og for og in armyops.orders_groups(g, c)
           if not g.p.u8(og + armyops.OG_IS_GROUP)]
    if not ogs:
        return {"ok": False, "error": "no land armies to attach air wings to"}
    w_ptrs = [int(w["ptr"], 16) for w in info["wings"]
              if w.get("type") != "transport_plane"]
    res = airops.attach(g, w_ptrs, ogs)
    res["ok"] = res.get("attached_count", 0) > 0
    return res


@tool("hoi4_air_mission",
      "Give the player's air wings a strategic mission. mission_mask: 1=Air "
      "Superiority, 2=CAS, 4=Interception, 8=Strategic Bombing, 16=Naval "
      "Strike, 64=Port Strike, 128=Logistics Strike. wing_ptr accepts one wing "
      "id, a comma list, or a whole type: 'fighters', 'cas', 'tac_bomber', "
      "'nav_bomber', 'all'. On the PE build CStratAirSetMissionCommand writes "
      "both the mission mask and the enabled flag, so no second command is "
      "needed; every wing is read back after the post.",
      {"wing_ptr": _STR, "mission_mask": _INT, "target_strat_air_ptr": _STR,
       "flag1": _BOOL, "flag2": _BOOL},
      ["wing_ptr", "mission_mask"])
def _air_mission(a):
    import sys
    g = game()
    mask = int(a["mission_mask"])
    if sys.platform == "win32":
        # 2026-11-16: PE path. CStratAirSetMissionCommand is verified working
        # (CanExecute=1, wing +0xA0 mission mask AND +0xA4 enabled flag
        # written -> CStratAirEnableMissionCommand is not needed).
        import airops_win as AW
        try:
            sel = AW.select_wings(g, a["wing_ptr"])
        except Exception as e:
            return {"ok": False, "error": str(e)}
        if not sel:
            return {"ok": False, "error": "selector matched no player wing",
                    "selector": a["wing_ptr"]}
        handles = [h for _i, h in sel]
        cmd = AW.make_set_mission(g, handles, mask,
                                  a.get("flag1", True), a.get("flag2", True))
        can = AW.can_exec(g, cmd, AW.CANEXEC_SET_MISSION)
        if not can:
            _names = {1: "air_superiority", 2: "cas", 4: "interception",
                      8: "strategic_bombing", 16: "naval_strike",
                      64: "port_strike", 128: "logistics_strike"}
            return {"ok": False,
                    "error": "CStratAirSetMissionCommand::CanExecute refused",
                    "mission_mask": mask,
                    "mission_name": _names.get(mask, str(mask)),
                    "wings": len(handles),
                    "hint": ("the engine refuses a mission the wing type cannot fly. "
                             "tac_bomber accepts strategic bombing (8) / naval strike "
                             "(16) but NOT air superiority (1); fighter wings accept "
                             "1 / 4; CAS wings accept 2 / 16."),
                    "wing_ids": [i for i, _h in sel], "mission_mask": mask}
        r = AW.post(g, cmd)
        # 2026-11-21 (续测 P1-8): the mask alone never makes a wing fly.  +0xA4 is
        # a PERSISTENT enable flag -- measured: write 1, still 1 after 1.8 s --
        # and nothing in this tool ever set it, so five fighter wings sat at
        # mission=1 / mission_on=false through peace AND through a declared war.
        # airops.enable_mission() cannot be used here (ENABLE_MISSION_CTOR is a
        # Linux ELF offset), so set the flag directly; the engine keeps it.
        try:
            import struct as _st8
            for _w in (AW.german_wings(g).get("wings") or []):
                if (_w.get("mission") or 0) == mask and _w.get("ptr"):
                    g.p.write(int(_w["ptr"], 16) + 0xA4, _st8.pack("<i", 1))
        except Exception:
            pass
        # 2026-11-17: success is judged on the MASK (+0xA0), which is what the
        # player order actually sets. `mission_on` (+0xA4) is the engine's
        # runtime flag: world census at 1936.02 showed 82 wings with a mission
        # and only 2 with +0xA4 == 1 (the two at war), so +0xA4 clearly means
        # "currently generating sorties", not "the player enabled this".
        # Reporting ok:false because 15 peacetime wings are not sortieing was
        # a false negative; it is reported separately now.
        import time as _t
        byid = {}
        for _ in range(6):
            byid = {w["id"]: w for w in AW.german_wings(g)["wings"]}
            if all((byid.get(wid) or {}).get("mission") == mask
                   for wid, _h in sel):
                break
            _t.sleep(0.15)
        rows = []
        for wid, _h in sel:
            w = byid.get(wid) or {}
            st = ({"planes": w.get("planes"), "mission": w.get("mission"),
                   "mission_name": w.get("mission_name"),
                   "mission_on": w.get("mission_on")} if w else {})
            rows.append({"id": wid, "type": w.get("type"), "handle": hex(_h),
                         "readback": st})
        applied = [x for x in rows if (x["readback"] or {}).get("mission") == mask]
        sortieing = [x for x in rows if (x["readback"] or {}).get("mission_on")]
        return {"ok": bool(r.get("posted")) and len(applied) == len(rows),
                "mission_mask": mask, "wings_selected": len(rows),
                "wings_mission_set": len(applied),
                "wings_sortieing_now": len(sortieing),
                "can_execute": can,
                "posted": r.get("posted"), "drained": r.get("drained"),
                "note": ("mission_on (+0xA4) IS the player enable, not a sortie "
                         "counter: writing 1 persisted for 1.8 s, and after this "
                         "tool started setting it, 5/5 fighter wings went from "
                         "mission_on=false (through peace AND war) to true."),
                "wings": rows}
    import airops
    wptr = _ptr(a["wing_ptr"])
    tptr = _ptr(a["target_strat_air_ptr"]) if a.get("target_strat_air_ptr") else 0
    with g:
        r1 = airops.set_mission(g, wptr, mask, a.get("flag1", True), a.get("flag2", True))
        r2 = airops.enable_mission(g, wptr, mask, tptr) if tptr else None
    return {"ok": bool(r1.get("ok")), "set_mission": r1, "enable_mission": r2}


# ============================================================ DIPLOMACY
@tool("hoi4_relations", "Relation records with other countries.", dict(TAG, limit=_INT))
def _rel(a):
    return game().relations(a.get("tag"), a.get("limit", 60))


@tool("hoi4_faction", "The country's faction.", TAG)
def _fac(a):
    return game().faction(a.get("tag"))


# ========================================================== CHARACTERS
@tool("hoi4_divisions", "The country's divisions (template + location).", dict(TAG, limit=_INT))
def _divs(a):
    return game().divisions(a.get("tag"), a.get("limit", 200))


@tool("hoi4_command_help",
      "Spell out the PARAMETERS of a player command: which offset, which type, which object "
      "it expects and where to get the value. Call this before hoi4_send_command.",
      {"name": _STR}, ["name"])
def _chelp(a):
    return game().command_help(a["name"])


@tool("hoi4_characters",
      "The country's characters: generals, admirals, advisors, political leaders.",
      dict(TAG, limit=_INT))
def _chars(a):
    return game().characters(a.get("tag"), a.get("limit", 5000))


@tool("hoi4_army_status",
      "Verified answer to 'how many divisions do I have'. Reads CCountry+0x290 "
      "through an identity-checked country pointer, falls back to an RTTI scan "
      "when that array is empty, and reports `countable:false` instead of a calm "
      "0 when the enumeration cannot be trusted (the 2026-11-16 failure).",
      {"tag": dict(_STR, description="Country tag (empty = the player)"),
       "deep": dict(_BOOL, description="Also run the RTTI fallback scan (~40 s the "
                                       "first time per game process)")})
def _army_status(a):
    return game().army_status(a.get("tag"), a.get("deep", False))


@tool("hoi4_division_selftest",
      "Compare the three independent readings of the division count "
      "(identity pointer / cached tag2index / RTTI deep scan). Run it at the "
      "start of a campaign and after every game restart.",
      {"tag": dict(_STR, description="Country tag (empty = the player)"),
       "deep": dict(_BOOL, description="Also run the RTTI fallback scan (~40 s the "
                                       "first time per game process)")})
def _division_selftest(a):
    import divenum
    return divenum.selftest(game(), a.get("tag"), deep=a.get("deep", True))



@tool("hoi4_train_divisions",
      "Open a division-training conveyor for a template and push its lines onto the map, "
      "or pass `conveyor` (ptr/handle from hoi4_conveyors) to add lines to an existing conveyor. "
      "lines=1..5 (the game silently caps some), state defaults to the capital.",
      dict(TAG, template=_STR, lines=_INT, state=_INT,
           conveyor={"type": "string",
                     "description": "Optional existing conveyor ptr/handle to add lines to instead of creating a new one"}))
def _train_divs(a):
    import verify as _v
    g = game()
    tag = a.get("tag")
    conv = a.get("conveyor")
    before = _v.conveyor_count(g, tag)
    r = g.train_divisions(a.get("template"), a.get("lines", 2), a.get("state"), tag,
                          conveyor=conv)
    if isinstance(r, dict) and (r.get("ok") is False or r.get("mode") == "add_lines_to_existing"):
        return r
    return verified(r, lambda: _v.conveyor_count(g, tag) > before, a.get("template"))


@tool("hoi4_deploy_ready",
      "Push finished (or >= min_progress) conveyor lines onto the map.",
      dict(TAG, min_progress={"type": "number"}, **{}))
def _deploy_ready(a):
    return game().deploy_ready_conveyors(a.get("tag"), float(a.get("min_progress") or 0.0))


@tool("hoi4_conveyor_progress", "Training progress of every conveyor line (0..1).", TAG)
def _cprog(a):
    g = game()
    out = []
    for conv in g.conveyors(a.get("tag")):
        try:
            _, prog = g._conveyor_progress(conv)
        except Exception as e:
            prog = None
        out.append({"ptr": conv["ptr"], "lines": conv.get("lines"), "progress": prog})
    return out


@tool("hoi4_conveyors",
      "Division-training conveyors: pointer, handle, deployment state and its lines.",
      TAG)
def _conveyors(a):
    g = game()
    tag = a.get("tag")
    convs = g.conveyors(tag)
    st = g._homeland_state_id(tag)
    return {"count": len(convs), "conveyors": convs,
            "homeland_state": st,
            "deploy_province": g.deployment_province(st, tag=tag)}


@tool("hoi4_scientists", "Scientists (for special projects).", dict(TAG, limit=_INT))
def _sci(a):
    return {"scientists": game().scientists(a.get("tag"), a.get("limit", 60))}


@tool("hoi4_special_projects", "Special project pool and programme status.", TAG)
def _proj(a):
    return game().special_projects(a.get("tag"))


@tool("hoi4_tech_folders", "Technology and doctrine folders.")
def _folders(a):
    return game().tech_folders()


# ============================================================ MORE FOCUS
@tool("hoi4_focus_detail",
      "Everything about one focus: prerequisite focuses, mutually exclusive ones, filters.",
      dict(TAG, key=_STR), ["key"])
def _fdetail(a):
    return game().focus_detail(a["key"], a.get("tag"))


@tool("hoi4_selectable_focuses",
      "Focuses selectable RIGHT NOW, ranked by the country's own historical AI plan "
      "(common/ai_strategy_plans). Returns recommended/recommended_reason plus a "
      "selectable list with tier (S_historical/A_research_slot/A_annex/B_industry/"
      "C_air_naval) and days. Civil-war self-destruct branches are blocked by default. "
      "On the Windows build every candidate is then asked of the engine's own "
      "CSetNationalFocusCommand::CanExecute (its `available` evaluator: manpower, "
      "rifles, laws, war state, PP), so `verified: true` means the engine agrees. When "
      "a focus is already running the engine refuses everything, the probe is skipped "
      "and the list is the ranked static candidates (verified: false + note).",
      dict(TAG, allow_civil_war={"type": "boolean",
                                 "description": "Allow GER_oppose_hitler branches"}))
def _selfocus(a):
    return game().selectable_focuses(a.get("tag"), bool(a.get("allow_civil_war")))


@tool("hoi4_completed_focuses", "Completed focuses.", TAG)
def _donefocus(a):
    return {"focuses": [f for f in game().completed_focuses(a.get("tag")) if f]}


@tool("hoi4_continuous_focuses",
      "Continuous focuses (the no-completion ones at the bottom left of the tree).", TAG)
def _contfocus(a):
    out = game().continuous_focuses(a.get("tag"))
    return {"count": len(out), "focuses": out}


@tool("hoi4_set_continuous_focus", "Select a continuous focus.", dict(TAG, key=_STR), ["key"])
def _setcont(a):
    return game().set_continuous_focus(a["key"], a.get("tag"))


@tool("hoi4_drop_continuous_focus", "Drop the continuous focus.", TAG)
def _dropcont(a):
    return game().drop_continuous_focus(a.get("tag"))


@tool("hoi4_equipment_stockpile",
      "Equipment counts in the stockpile and producible variants.", dict(TAG, limit=_INT))
def _stock(a):
    return game().equipment_stockpile(a.get("tag"), a.get("limit", 120))


@tool("hoi4_state_detail",
      "One state in detail: province count, resistance, compliance, building levels.",
      dict(TAG, state=_INT), ["state"])
def _sdet(a):
    return game().state_detail(a["state"], a.get("tag"))


@tool("hoi4_states_detail", "Detailed list of owned states.", dict(TAG, limit=_INT))
def _sdets(a):
    return game().states_detail(a.get("tag"), a.get("limit", 40))


@tool("hoi4_idea_categories",
      "Idea and law categories: government (laws), research_production (companies), "
      "military_staff, army_spirit, theorist...", TAG)
def _icat(a):
    return {"categories": game().idea_categories(a.get("tag"))}


@tool("hoi4_decision_detail",
      "One decision in full: script metadata (title, category, cost, run time, "
      "available/visible/activation trigger slices) plus the live object fields.",
      dict(TAG, key=_STR), ["key"])
def _ddet(a):
    return game().decision_detail(a["key"], a.get("tag"))


# ============================================ EVENTS (static db + live popup)
@tool("hoi4_events",
      "LIVE event popup reader: every CEventWindow on screen -> the pending event id, "
      "its localized title/description and ALL its options with effects / ai_chance / "
      "trigger (looked up in the game's own scripts). Empty when no popup is open. "
      "Read-only; CEventWindow+0x68 is the CEvent*, id at CEvent+0x20, options at +0x368.",
      {"lang": dict(_STR, description="zh (default) or en"),
       "sweep": {"type": "boolean",
                 "description": "force a full ~90 s sweep for the popup window "
                                "(otherwise only the cached 8 MB pool is re-scanned)"}})
def _events(a):
    import events_win
    return events_win.open_events(game(), with_db=True, lang=a.get("lang") or "zh",
                                  deep=bool(a.get("sweep")))


@tool("hoi4_event_info",
      "Look up ANY event by id in the game's scripts: title, description, every option "
      "with its effects / ai_chance / trigger, localized. Does not touch the game.",
      {"event_id": dict(_STR, description="e.g. GER_rhineland, news.1, AUS_political_events.102"),
       "lang": dict(_STR, description="zh (default) or en")},
      ["event_id"])
def _einfo(a):
    import events_win
    return events_win.info(a["event_id"], lang=a.get("lang") or "zh")


@tool("hoi4_event_search",
      "Search the event database (6957 events incl. 677 news_event) by id or title.",
      {"query": _STR,
       "lang": dict(_STR, description="zh (default) or en"),
       "limit": {"type": "integer", "description": "max rows (default 25)"}},
      ["query"])
def _esearch(a):
    import events_win
    return events_win.search(a["query"], lang=a.get("lang") or "zh",
                             limit=int(a.get("limit") or 25))


@tool("hoi4_select_event_option",
      "Read or answer a pending event. Without write=True it only reports: every option with "
      "an `applicable` verdict evaluated from its own `trigger`, the recommended index, the "
      "index the game auto-picks after 13 days (always 1), and a built CSelectEventOptionCommand. "
      "With write=True it builds the command through the engine's own factory (0xA590D0), checks "
      "the engine's CanExecute, posts it and verifies the pending event actually went away. "
      "2026-11-18: the earlier crashes were caused by hand-writing a std::string over the "
      "command's embedded CEventScope at +0x38; the factory builds that scope correctly.",
      {"index": {"type": "integer", "description": "which option (1-based); default = recommended"},
       "event_id": dict(_STR, description="only look at this event (default: first pending)"),
       "tag": dict(_STR, description="player tag for the trigger check (default GER)"),
       "lang": dict(_STR, description="zh (default) or en"),
       "write": {"type": "boolean", "description": "actually answer the event (default false = read only)"}})
def _esel(a):
    import events_win
    return events_win.select_option(game(), index=a.get("index"),
                                    event_id=a.get("event_id") or None,
                                    lang=a.get("lang") or "zh",
                                    tag=a.get("tag") or "GER",
                                    write=bool(a.get("write")))


# ============================================ ALL PLAYER COMMANDS (370)
@tool("hoi4_player_commands",
      "Every action a player can take (370 CCommand classes) with their field layouts. "
      "Executed through hoi4_send_command. Examples: CDiplomaticActionCommand, "
      "COrderNewFrontCommand, CCreateDivisionTemplateCommand, CDeployAirWingCommand...",
      {"filter": _STR})
def _pcmds(a):
    return game().player_commands(a.get("filter", ""))


@tool("hoi4_send_command",
      "Send ANY player command. Not a cheat: it goes into the game's own command queue. name: "
      "a class from hoi4_player_commands. fields: {'arg0': '<value>', ...}. Values may be "
      "given BY NAME: 'focus:GER_rhineland', 'tech:interwar_artillery', 'idea:war_economy', "
      "'decision:xxx', 'country:FRA', 'state:51', 'building:arms_factory', "
      "'equipment:infantry_equipment_1', 'character:GER_erwin_rommel', 'template:0', "
      "'army:3', 'fleet:0', 'subunit:infantry', 'wargoal:take_state', 'ptr:0x...' or a plain "
      "number. Call hoi4_command_help first to learn the parameters.",
      dict(TAG, name=_STR, fields={"type": "object"}, force=_BOOL), ["name"])
def _sendcmd(a):
    return game().send_command(a["name"], a.get("fields") or {}, a.get("tag"),
                               a.get("force", False))


@tool("hoi4_handle",
      "The in-game handle ({type,id}) of an object. Army and general commands take handles "
      "rather than pointers. kind: character|division (division accepts index or 0x... ptr).",
      {"kind": _STR, "key": _STR, "tag": _STR}, ["kind", "key"])
def _handle(a):
    g = game()
    kind = str(a.get("kind") or "").strip().lower()
    key = a.get("key")
    try:
        if kind == "character":
            return g.character_handle(key, a.get("tag"))
        if kind == "division":
            return g.division_handle(key, a.get("tag"))
        return {"ok": False, "error": "unsupported kind %r (expected 'character' or 'division')" % kind}
    except (ValueError, IndexError, KeyError) as e:
        return {"ok": False, "kind": kind, "key": key, "error": str(e)}


@tool("hoi4_diplomatic_action_classes",
      "The game's 47 diplomatic action classes (declare war, justify/generate war goal, "
      "guarantee, call to faction, non-aggression, military access, send volunteers, "
      "coup...). CDiplomaticActionCommand carries one of them.")
def _dipacts(a):
    return game().diplomatic_action_classes()


@tool("hoi4_faction_members", "Members of the faction.", TAG)
def _facm(a):
    return game().faction_members(a.get("tag"))


@tool("hoi4_wars", "War status (offensive/defensive/against a major, surrender progress).", TAG)
def _wars(a):
    return game().wars(a.get("tag"))


@tool("hoi4_trade_routes",
      "Resource trade routes (who sells what to whom). PE 1.19.5.0: reads the "
      "country's export book at CCountry+0x11F8 -> +0x728 (one array per "
      "strategic resource) and derives imports by scanning every other "
      "country's book, because a deal is stored on the SUPPLIER's side only. "
      "Returns exports/imports/net per resource. world=true lists every route "
      "on the map (verified: 131 routes world-wide in 1936.03).",
      dict(TAG, limit=_INT, world={"type": "boolean"}))
def _trade(a):
    return game().trade_routes(a.get("tag"), a.get("limit", 40),
                               world=bool(a.get("world")))


@tool("hoi4_trade",
      "Import strategic resources (oil/rubber/tungsten/steel/aluminium/chromium/coal) "
      "from an exporting country via native CCreateTradeCommand (verified by CanExecute "
      "and supplier export-book read-back). Pass amount=8/16/24 (or factories=1/2/3), "
      "or amount=0 to cancel an existing trade.",
      dict(TAG,
           partner={"type": "string", "description": "Exporting country TAG (e.g. ROM, SOV, VEN, SWE)"},
           resource={"type": "string", "description": "oil | rubber | tungsten | steel | aluminium | chromium | coal"},
           amount={"type": "integer", "description": "Total resource units to import (e.g. 8, 16; 0 = cancel)"},
           factories={"type": "integer", "description": "Shortcut: civilian factories to spend (amount = factories * 8)"},
           dry_run=_BOOL),
      ["partner", "resource"])
def _create_trade(a):
    return game().create_trade(
        partner=a["partner"],
        resource=a["resource"],
        amount=a.get("amount"),
        factories=a.get("factories"),
        tag=a.get("tag"),
        dry_run=bool(a.get("dry_run", False)),
    )


@tool("hoi4_country_subsystems",
      "Every subsystem of a country (research, production, politics, diplomacy, intelligence, "
      "occupation, operations, projects...) with class names and the arrays inside them. The "
      "starting point for exploring an unwrapped mechanic; continue with hoi4_browse_object.", TAG)
def _subsys(a):
    return game().country_subsystems(a.get("tag"))


@tool("hoi4_promote_general", "Promote a corps commander to field marshal. Refuses with a reason if the rank is "
      "wrong, and reports whether the promotion actually took.",
      dict(TAG, character=_STR), ["character"])
def _promo(a):
    return game().promote_general(a["character"], a.get("tag"))


@tool("hoi4_recruit_unit_leader",
      "Recruit a new military leader (costs COMMAND POWER, verified via engine CanExecute "
      "and character/CP read-back). type: 0 or 'general'/'corps_commander' (army general, default), "
      "1 or 'field_marshal', 2 or 'admiral'.",
      dict(TAG, type={"description": "0/'general' (default), 1/'field_marshal', or 2/'admiral'"},
           dry_run=_BOOL))
def _recr(a):
    return game().recruit_unit_leader(a.get("type", 0), a.get("tag"),
                                      dry_run=bool(a.get("dry_run", False)))


@tool("hoi4_diplomacy_guide",
      "Guide to the diplomatic action classes and how they work.")
def _dipinfo(a):
    return game().diplomatic_action_info()


@tool("hoi4_list_database",
      "List one of the game's item databases: war_goals, occupation_laws, buildings, "
      "technologies, ideas, decisions, equipment, focuses, character_templates, "
      "scripted_diplomatic_actions.",
      {"which": _STR, "filter": _STR, "limit": _INT}, ["which"])
def _lsdb(a):
    return game().list_database(a["which"], a.get("filter", ""), a.get("limit", 200))


@tool("hoi4_browse_object",
      "ADVANCED: dump the fields of an unknown game object with class names and strings. For "
      "discovering new mechanics.",
      {"ptr": _STR, "span": _INT}, ["ptr"])
def _browse(a):
    return game().browse_object(a["ptr"], a.get("span", 0x100))


# ============================================================ DEBUG / HILE
def _ptr(v):
    """Pointer argument: accepts a 0x... string or an integer."""
    if v is None:
        return None
    if isinstance(v, str):
        return int(v, 16) if v.lower().startswith("0x") else int(v)
    return int(v)


# =============================================================== armies and battle plans
TAG = {"tag": {"type": "string", "description": "Country tag (default: the player)"}}
OG = {"orders_group": {"type": "string",
      "description": "COrdersGroup pointer, the ptr from hoi4_armies output (e.g. \"0x5606b3212a40\")"}}


@tool("hoi4_armies",
      "The country's ARMIES and ARMY GROUPS: name, commander, division count, parent group, "
      "active order count. Get pointers here before creating armies or issuing orders.", TAG)
def _armies(a):
    return game().order_groups(a.get("tag"))


@tool("hoi4_unassigned_divisions",
      "Divisions not attached to any army (count and pointers).", TAG)
def _unassigned(a):
    d = game().unassigned_divisions(a.get("tag"))
    return {"count": len(d), "divisions": [hex(x) for x in d]}


@tool("hoi4_create_army",
      "Collect divisions into a new ARMY, exactly like selecting them and pressing the '+' "
      "button. With count, that many unassigned divisions are taken (e.g. 24 then 6 to split "
      "24/6).",
      {"count": {"type": "integer", "description": "How many divisions to take (default: all)"},
       "divisions": {"type": "array", "items": {"type": "string"},
                     "description": "CArmy isaretcileri (opsiyonel)"},
       **TAG})
def _create_army(a):
    divs = [_ptr(x) for x in a["divisions"]] if a.get("divisions") else None
    return game().create_army(divs, a.get("count"), a.get("tag"))


@tool("hoi4_create_army_group",
      "Collect armies into an ARMY GROUP (field marshal command). Without armies, every army "
      "not already in a group is taken.",
      {"armies": {"type": "array", "items": {"type": "string"},
                  "description": "COrdersGroup isaretcileri (opsiyonel)"}, **TAG})
def _create_army_group(a):
    arm = [_ptr(x) for x in a["armies"]] if a.get("armies") else None
    return game().create_army_group(arm, a.get("tag"))


@tool("hoi4_assign_commander",
      "Assign a commander to an army or army group. general: character key or name (from "
      "hoi4_generals). Army groups take field marshals, armies take corps commanders; the "
      "game's own rules are enforced.",
      {**OG, "general": {"type": "string", "description": "Character key or name"},
       "allow_unverified": {"type": "boolean",
                            "description": "Ignore visibility conditions that cannot be evaluated"}},
      ["orders_group", "general"])
def _assign_commander(a):
    return game().assign_commander(_ptr(a["orders_group"]), a["general"],
                                   allow_unverified=a.get("allow_unverified", False))


@tool("hoi4_generals", "The country's generals and field marshals with their EFFECTIVE skills "
      "(attack/defense/planning/logistics, trait bonuses included).", TAG)
def _generals(a):
    return game().generals(a.get("tag"))


@tool("hoi4_border_fronts",
      "Front sections facing a target country and the border provinces in each. Input for "
      "hoi4_draw_front_line.",
      {"enemy_tag": {"type": "string"}, **TAG}, ["enemy_tag"])
def _border_fronts(a):
    return game().border_fronts(a["enemy_tag"], a.get("tag"))


@tool("hoi4_front_changes",
      "Front-line movement: which map provinces changed owner since the previous call "
      "(from/to country). First call just takes a snapshot. enemy_tag filters to that country.",
      {"enemy_tag": {"type": "string", "description": "Only report changes involving this country"},
       "reset": {"type": "boolean", "description": "Rebuild the baseline snapshot"}})
def _front_changes(a):
    return game().front_changes(a.get("enemy_tag"), bool(a.get("reset")))


@tool("hoi4_duplicate_division_template",
      "Duplicate an existing division template into a brand-new one. Army XP cost is "
      "computed dynamically by the engine from the source template's CURRENT battalions "
      "and support companies (e.g. a small cavalry/mountain template costs ~30 XP, "
      "a stock 9-inf template ~65-80 XP, and an upgraded 9inf+3art+support template "
      "costs ~100+ XP). Pass dry_run=true first to quote the exact xp_cost without "
      "spending XP. To also estimate follow-up edit cost before duplicating, call "
      "hoi4_template_edit(template=source_name, ..., dry_run=true) first.",
      {"source_name": {"type": "string", "description": "Name of the template to copy"},
       "new_name": {"type": "string", "description": "Name for the new template (default '<source> Copy')"},
       "dry_run": _BOOL,
       **TAG})
def _dup_template(a):
    g = game()
    # B1: duplicate_division_template() detaches and sleeps 0.25 s so the game can
    # consume the queue; an outer `with g:` guarantees it always fails.
    return g.duplicate_division_template(a["source_name"], a.get("tag"),
                                         a.get("new_name"),
                                         dry_run=bool(a.get("dry_run", False)))

@tool("hoi4_template_edit",
      "Edit a division template through CUpdateDivisionTemplateCommand (engine deep-copy). "
      "IMPORTANT: DEFAULT `dry_run=true` (preview only, returns applied=false and does NOT send "
      "the edit); pass `dry_run=false` to actually apply the change and spend army XP! "
      "line counts are per battalion name: line artillery is \"artillery_brigade\" "
      "(\"artillery\" is the support company), e.g. {\"infantry\":9,\"artillery_brigade\":3}. "
      "Invalid/unknown battalion or support names now fail with ok=false instead of silently posting.",
      {"template": _STR, "line": {"type": "object"}, "support": {"type": "object"},
       "dry_run": {"type": "boolean", "description": "Preview only (DEFAULT true); pass false to apply"},
       "tag": _STR})
def _tmpl_edit(a):
    import templedit
    g = game()
    name = a.get("template")
    tmpls = g.division_templates(a.get("tag"))
    pick = None
    for t in tmpls:
        if str(t.get("index")) == str(name) or t.get("name") == name:
            pick = t; break
    if not pick:
        raise KeyError("no such template: %s" % name)
    return templedit.edit_template(g, int(pick["ptr"], 16), a.get("line"),
                                   a.get("dry_run", True), a.get("support"))


@tool("hoi4_template_composition",
      "Division template makeup: how many battalions of each type (infantry, artillery, "
      "light_armor, motorized...) plus support companies. Pass name/index to filter.",
      {"name": {"type": "string", "description": "Template name or index (optional)"},
       **TAG})
def _tmpl_comp(a):
    return game().template_composition(a.get("tag"), a.get("name"))


@tool("hoi4_draw_front_line",
      "RE-ENABLED 2026-11-08. Gives an army (COrdersGroup) a NATIVE front line along a real "
      "CFront, exactly like the front-line button in the army panel. The engine then holds "
      "and distributes the divisions itself. Pass orders_group (from hoi4_armies) plus "
      "either front_ptr (from hoi4_border_fronts) or enemy_tag "
      "(the first matching front is used). "
      "from/to are 0..100000 slices of the line; omit for the whole front.",
      {**OG, "enemy_tag": {"type": "string"}, "front_ptr": {"type": "string"},
       "section": {"type": "integer"}, "from": {"type": "integer"},
       "to": {"type": "integer"}, "assign": {"type": "string"}},
      ["orders_group"])
def _draw_front(a):
    return game().draw_front_line(
        _ptr(a["orders_group"]), enemy_tag=a.get("enemy_tag"), front_ptr=a.get("front_ptr"),
        section=a.get("section"), frm=a.get("from", 0), to=a.get("to") or 100000,
        assign=a.get("assign") or "proportional")


@tool("hoi4_army_battleplans",
      "One-shot: give EVERY free army its OWN slice of the native FRONT LINE along the CFront "
      "facing enemy_tag (not one line for the whole army), add an OFFENSIVE LINE through the "
      "enemy's territory, EXECUTE each plan and switch the arrows on. The engine then holds and "
      "distributes the divisions itself — no per-division micro, and no over-stacking. "
      "Only as many armies as that front can supply are committed; armies already holding "
      "another front are left alone. Returns per-army results. "
      "PEACETIME DRAWING IS ALLOWED AND EXPECTED (since 2026-11-18): hoi4_declare_war now "
      "requires a front line + offensive line with an assigned army facing the target, so draw "
      "the plan first, read hoi4_orders back until assigned > 0 on every army, then declare.",
      {"enemy_tag": {"type": "string", "description": "Country tag, e.g. HOL"},
       "force": {"type": "boolean",
                 "description": "Ignore the supply cap and any existing orders"},
       "armies": {"type": "array", "items": {"type": "string"},
                  "description": "Explicit COrdersGroup pointers (default: every free army)"},
       "split": {"type": "boolean",
                 "description": "Give each army its own contiguous slice (default true)"},
       "wait_seconds": {"type": "integer",
                        "description": "Poll this long for the first enemy province to change "
                                       "hands. At speed 1 the plan needs ~10 game days (~3 "
                                       "minutes); 0 (default) returns once the plan is built."},
       **TAG}, ["enemy_tag"])
def _army_battleplans(a):
    return game().activate_army_battleplans(a["enemy_tag"], a.get("tag"),
                                            bool(a.get("force")),
                                            int(a.get("wait_seconds") or 0),
                                            armies=a.get("armies"),
                                            split=a.get("split", True))


@tool("hoi4_deploy_to_border",
      "High-level: issue per-division CMoveCommand moves across land border provinces "
      "facing target_tag (uses mapadj.pkl + BFS; no province IDs or division handles needed). "
      "IMPORTANT: this tool ONLY moves divisions and does NOT change or remove any army's "
      "existing front-line order -- if an army still holds a front order on another border, "
      "the engine will pull those divisions back to that old front on the next tick. "
      "To truly redeploy an army to a new border, first delete its old front order with "
      "hoi4_delete_order and redraw via hoi4_army_battleplans. Automatically guards active "
      "wartime fronts and protect_borders, and reports armies_with_active_orders warnings.",
      {"target_tag": dict(_STR, description="Target country tag, e.g. POL"),
       "orders_group": _STR,
       "include_unassigned": {"type": "boolean"},
       "protect_borders": {"type": "array", "items": {"type": "string"},
                           "description": "Neighbor tags whose border garrisons must not be stripped (e.g. ['FRA'])"},
       "min_other_garrison": {"type": "integer",
                              "description": "Minimum divisions per province to keep on protected/other borders (default 0; active wartime fronts always keep >=1)"},
       **TAG}, ["target_tag"])
def _deploy_border(a):
    return game().deploy_to_border(
        a["target_tag"], _ptr(a.get("orders_group")),
        a.get("include_unassigned", True), a.get("tag"),
        protect_borders=a.get("protect_borders"),
        min_other_garrison=int(a.get("min_other_garrison") or 0),
    )


@tool("hoi4_attack_country",
      "High-level: multi-direction attack on target_tag. Finds our divisions adjacent "
      "to its provinces, filters moving/low-org units, and posts CMoveCommand(attack) "
      "with mode careful|balanced|aggressive.",
      {"target_tag": dict(_STR, description="Target country tag, e.g. POL"),
       "mode": _STR, "max_attacks": _INT, **TAG}, ["target_tag"])
def _attack_country(a):
    return game().attack_country(a["target_tag"], a.get("mode", "balanced"),
                                 a.get("max_attacks", 2), a.get("tag"))


@tool("hoi4_plug_border_gaps",
      "Wartime defense: find every border province facing target_tag that has NO "
      "division and NO division moving in, then send 1-2 reserve divisions from "
      "adjacent provinces (or depth) to plug each hole. Never strips the last "
      "garrison from another border province. Use dry_run=true to plan only.",
      {"target_tag": dict(_STR, description="Enemy country tag, e.g. D04"),
       "min_garrison": _INT,
       "max_gaps": _INT,
       "dry_run": _BOOL, **TAG}, ["target_tag"])
def _plug_border_gaps(a):
    return game().plug_border_gaps(a["target_tag"], a.get("min_garrison", 1),
                                   a.get("max_gaps"), a.get("dry_run", False))


@tool("hoi4_wartime_step",
      "ONE wartime micro tick, entirely in Python (0 model tokens): collect "
      "unassigned divisions, plug empty border provinces, then mass 6-9 divisions "
      "on 1-2 soft enemy provinces. Keeps a home guard, refuses low-org / low-str / "
      "out-of-supply attackers. Returns accepted_total / refused_total / plugged_gaps; "
      "ok is true only if a command was accepted or a gap was plugged.",
      {"target_tag": dict(_STR, description="Enemy tag; empty = auto-detect"),
       "max_attacks": _INT, "dry_run": _BOOL, **TAG})
def _wartime_step(a):
    return game().wartime_step(a.get("target_tag"), a.get("max_attacks", 2),
                               a.get("dry_run", False))


@tool("hoi4_wartime_status",
      "Pure read: are we at war, and who is the enemy? Cheap check before a wartime micro loop.",
      {**TAG})
def _wartime_status(a):
    return game().wartime_status()


@tool("hoi4_set_front_directive",
      "CAMPAIGN ORDER (the only thing the LLM should send for land war). Arm a "
      "persistent front directive for one enemy: stance hold | balanced_attack | "
      "blitzkrieg | prepare, optional priority_provinces (+300 score) and "
      "priority_states (+200), min_garrison 1-2. Ticks once immediately and "
      "reports `armed` -- true only when a tactical tick really ran. The in-process "
      "frontd worker then keeps running the 5 CA laws (role split, gap plugging + "
      "org rotation, width-exact attack, pinning, losing-attack abort) with 0 model "
      "tokens. stance=prepare also deploys the army to the border. "
      "Since 2026-11-17 the first attacking tick also draws a NATIVE front line + "
      "offensive line for every free army (one slice of the front per army, only as "
      "many armies as the front can supply) and attaches the air force to those "
      "armies; pass battleplan=false to keep the pure per-division micro tick.",
      {"enemy_tag": dict(_STR, description="Enemy tag, e.g. ENG / D04 / AUTO"),
       "stance": dict(_STR, description="prepare | hold | balanced_attack | blitzkrieg"),
       "priority_provinces": {"type": "array", "items": {"type": "integer"},
                              "description": "Province ids to take first (+300)"},
       "priority_states": {"type": "array", "items": {"type": "integer"},
                           "description": "State ids to take first (+200)"},
       "min_garrison": dict(_INT, description="Home guards per border province (1-2)"),
       "deploy": dict(_BOOL, description="Force deploy_to_border now"),
       "battleplan": dict(_BOOL, description="Draw native front+offensive lines "
                                              "first (default true; isolated to enemy_tag's slice)"),
       "armies": {"type": "array", "items": {"type": "string"},
                  "description": "Optional explicit army (COrdersGroup) hex ptrs to commit; "
                                 "if omitted, only free armies or armies already facing enemy_tag are used"},
       **TAG}, ["enemy_tag"])
def _set_front_directive(a):
    return game().set_front_directive(a["enemy_tag"], a.get("stance", "balanced_attack"),
                                      a.get("priority_provinces"), a.get("priority_states"),
                                      a.get("min_garrison", 1), a.get("deploy"),
                                      battleplan=a.get("battleplan", True),
                                      armies=a.get("armies"))


@tool("hoi4_front_directives",
      "Read the armed front directives: per directive `armed` (a tick really ran "
      "against this game pid) plus the frontd worker heartbeat. `armed:false` "
      "means nothing is commanding the army, whatever the JSON says.",
      {**TAG})
def _front_directives(a):
    return game().front_directives()


@tool("hoi4_tactical_front_tick",
      "Run the 5-law CA tactical tick now. The in-process frontd worker calls this "
      "by itself while the game is running; use this for a manual or dry_run "
      "inspection. force=true ignores the 1-game-day gate.",
      {"enemy_tag": dict(_STR, description="Only this enemy (default: all)"),
       "force": dict(_BOOL, description="Ignore the 2-game-day gate"),
       "dry_run": dict(_BOOL, description="Plan only, post no commands"),
       **TAG})
def _tactical_front_tick(a):
    return game().tactical_front_tick(a.get("enemy_tag"), a.get("force", False),
                                      a.get("dry_run", False))


@tool("hoi4_clear_front_directive",
      "Disarm one front directive (or all when enemy_tag is empty).",
      {"enemy_tag": dict(_STR, description="Enemy tag; empty clears all"), **TAG})
def _clear_front_directive(a):
    return game().clear_front_directive(a.get("enemy_tag"))


@tool("hoi4_move_divisions",
      "ROUTE A: order divisions to MOVE to or ATTACK a province - exactly the player's right "
      "click on the map. Give division pointers (from hoi4_division_objects), or an "
      "orders_group, or neither (the whole country). Pass `spread=true` (optional "
      "`max_per_province`, default 8) to automatically distribute a whole army across nearby "
      "friendly provinces around `province` without tripping the 12-division stacking guard or "
      "collapsing local supply. Verify with moving/path in the reply.",
      {"province": {"type": "integer", "description": "Target (or center) province id"},
       "divisions": {"type": "array", "items": {"type": "string"},
                     "description": "CArmy pointers (optional)"},
       "orders_group": {"type": "string", "description": "Take every division of this army (optional)"},
       "mode": {"type": "string", "description": "move (default, UI flags) or attack"},
       "wait": {"type": "number", "description": "Seconds to poll for the movement path (default 12)"},
       "limit": {"type": "integer", "description": "Max divisions (default 60)"},
       "force": {"type": "boolean",
                 "description": "Ignore the stacking guard (12+ divisions in one province)"},
       "spread": {"type": "boolean",
                  "description": "Auto-spread divisions across nearby friendly provinces around `province` (default false)"},
       "max_per_province": {"type": "integer",
                            "description": "Max divisions per province when spread=true (default 8, max 12 unless force=true)"},
       **TAG},
      ["province"])
def _move_divisions(a):
    return game().move_divisions(a.get("divisions"), a.get("province"), _ptr(a.get("orders_group")),
                                 a.get("tag"), a.get("mode", "move"), a.get("wait", 12.0),
                                 a.get("limit", 60), a.get("force", False),
                                 spread=bool(a.get("spread", False)),
                                 max_per_province=a.get("max_per_province"))


@tool("hoi4_division_detail",
      "Divisions ONE BY ONE, using the game's own field names: organisation, strength (HP), "
      "experience, entrenchment, fuel, supply ratio, days out of supply, damage taken, parent "
      "army, template. With order_group, only that army's divisions.",
      {"order_group": {"type": "string", "description": "COrdersGroup ptr (opsiyonel)"},
       "limit": {"type": "integer"}, **TAG})
def _divdetail(a):
    return game().division_detail(a.get("tag"), a.get("limit", 200), _ptr(a.get("order_group")))


@tool("hoi4_assign_divisions",
      "Distribute an army's divisions across its front orders. mode: proportional (by front "
      "length, default), even, all (every division to every order), none (leave them free for "
      "manual assignment). With divisions, only those are assigned.",
      {**OG, "enemy_tag": {"type": "string", "description": "Which border the orders belong to"},
       "mode": {"type": "string"},
       "divisions": {"type": "array", "items": {"type": "string"},
                     "description": "CArmy pointers (optional, for assigning a group at once)"}},
      ["orders_group"])
def _assign_divs(a):
    divs = [_ptr(x) for x in a["divisions"]] if a.get("divisions") else None
    return game().assign_divisions_to_front(_ptr(a["orders_group"]), a.get("enemy_tag"),
                                            a.get("mode", "proportional"), divs)


@tool("hoi4_add_to_army",
      "Add unassigned divisions (or divisions from another army via from_army) into an EXISTING "
      "army (orders_group) without creating a new army. NOTE: by default this only changes army "
      "ownership and does NOT move divisions off their current provinces (`positions_moved: false`); "
      "pass `move_to_province` (with default `spread=true`) to transfer AND relocate them away "
      "from an overloaded front in one step. When orders_group already has a front order and "
      "order_id is omitted, divisions attach to that front order via COrderAssignCommand.",
      {**OG,
       "divisions": {"type": "array", "items": {"type": "string"},
                     "description": "CArmy pointers (optional; default = unassigned non-paradrop divisions)"},
       "from_army": {"type": "string",
                     "description": "Optional source COrdersGroup ptr to merge/transfer divisions from"},
       "count": {"type": "integer",
                 "description": "Optional max number of divisions to move (default: fill up to 24)"},
       "order_id": {"type": "integer",
                    "description": "Optional target order id inside orders_group (0 = army only)"},
       "move_to_province": {"type": "integer",
                            "description": "Optional province id to relocate the transferred divisions to right away"},
       "spread": {"type": "boolean",
                  "description": "When move_to_province is set, spread across nearby friendly provinces (default true)"},
       "max_per_province": {"type": "integer",
                            "description": "Max divisions per province when moving with spread=true (default 8)"},
       **TAG},
      ["orders_group"])
def _add_to_army(a):
    divs = [_ptr(x) for x in a["divisions"]] if a.get("divisions") else None
    return game().add_divisions_to_army(
        _ptr(a["orders_group"]),
        divisions=divs,
        count=a.get("count"),
        from_army=_ptr(a["from_army"]) if a.get("from_army") else None,
        order_id=a.get("order_id"),
        tag=a.get("tag"),
        move_to_province=a.get("move_to_province"),
        spread=bool(a.get("spread", True)),
        max_per_province=a.get("max_per_province"),
    )


@tool("hoi4_available_generals",
      "Commanders the player can actually assign right now (visible under the current "
      "government), sorted by skill. Filter with rank: field_marshal / corps_commander / "
      "navy_leader.",
      {"rank": {"type": "string"}, **TAG})
def _availgen(a):
    return game().available_generals(a.get("tag"), a.get("rank"))


@tool("hoi4_orders",
      "Active battle plan orders of an army or army group. Each type-2 front order "
      "carries its own `children` list: the type-1 OFFENSIVE lines (the arrows). "
      "2026-11-19: they are NOT separate roots -- the group array only holds front "
      "orders, and a hand-drawn arrow appears solely under its parent. `executing` "
      "is the engine's +0x248 byte (0 = drawn but not running).",
      OG, ["orders_group"])
def _orders(a):
    import sys as _sys
    g = game()
    og = _ptr(a["orders_group"])
    rows = g.orders_of(og)
    if _sys.platform == "win32" and isinstance(rows, list):
        import armyops_win as A
        kids = A.order_children(g, og)
        by_root = {}
        for c in kids:
            by_root.setdefault(c["root"], []).append(c)
        for r in rows:
            mine = by_root.get(r.get("id"), [])
            r["offensive_lines"] = len(mine)
            r["children"] = [
                {
                    "id": c["id"],
                    "ptr": hex(c["ptr"]),
                    "type": c["type"],
                    "assigned_divisions": c["assigned"],
                    "executing": c["executing"],
                    **({"source_offset": c["source_offset"]} if c.get("source_offset") else {}),
                    **({"inverted_parent_link": True} if c.get("inverted_parent_link") else {}),
                    **({"foreign_group": True, "owner_group": c.get("owner_group")} if c.get("foreign_group") else {}),
                }
                for c in mine
            ]
            if r.get("type") == 2:
                # 2026-11-21 (P1-8): the 2026-11-20 playtest read
                # `assigned_divisions: 30` as "30 divisions are standing on the
                # front" and then found only 3 of them on a border province.
                # This number is the engine's assignment array -- the divisions
                # the order OWNS -- never their positions.
                r["assigned_semantics"] = ("divisions the order owns, not divisions "
                                           "standing on the border; positions are in "
                                           "hoi4_division_detail")
    return rows


@tool("hoi4_execute_plan",
      "EXECUTE or HALT a battle plan (the green tick / stop button in the UI). "
      "Pass executing=false (or flag=0) to switch to DEFENSE without deleting the front line "
      "or moving divisions: pass a type-1 child order_id (from hoi4_orders[].children[].id) "
      "to stop a SINGLE offensive arrow while leaving the parent front intact, pass a type-2 "
      "root order_id to stop that front's arrows, or pass order_id=0 with executing=false to "
      "halt ALL offensive orders of this army. Default executing=true.",
      {**OG,
       "order_id": {"type": "integer",
                    "description": "Type-1 arrow id, type-2 front id, or 0 for whole army (default 0)"},
       "executing": {"type": "boolean",
                     "description": "true = execute/attack (default), false = halt/defend"},
       "flag": {"type": "integer",
                "description": "Alias for executing: 1 = execute, 0 = halt"}},
      ["orders_group"])
def _exec_plan(a):
    og = _ptr(a["orders_group"])
    oid = int(a.get("order_id", 0) or 0)
    if a.get("executing") is not None:
        return game().execute_plan(og, oid, executing=bool(a.get("executing")))
    if a.get("flag") is not None:
        return game().execute_plan(og, oid, flag=int(a.get("flag")))
    return game().execute_plan(og, oid, flag=1)


@tool("hoi4_offensive_line",
      "Draw or disable a REAL OFFENSIVE LINE (the arrow inside a front line) under an existing "
      "type-2 front order. Pass `enabled=false` to HALT all active offensive arrows under "
      "`parent_order_id` without deleting the parent front line or moving divisions! "
      "When drawing (`enabled=true`, default): `provinces` must be ENEMY target provinces "
      "(NOT our own border provinces from hoi4_border_fronts!). Better yet, pass `enemy_tag` "
      "(e.g. 'HOL') and omit `provinces` so the tool automatically selects enemy target "
      "provinces accepted by the engine's 0xD57D90 enemy-area matcher. Uses the real "
      "COrderNewFrontCommand ctor + Execute (0x18D9070) and assigns divisions to the created "
      "type-1 child. Refuses to return ok=true if the resulting arrow has assigned=0 (never "
      "falls back silently to broken decorative arrows).",
      {**OG, "parent_order_id": {"type": "integer",
                                 "description": "id of the type-2 front order (from hoi4_orders)"},
       "provinces": {"type": "array", "items": {"type": "integer"},
                     "description": "ENEMY target province ids (optional when enemy_tag is passed)"},
       "enemy_tag": {"type": "string",
                     "description": "Enemy tag (e.g. HOL/BEL) to auto-select enemy target provinces"},
       "depth": {"type": "integer", "description": "BFS depth into enemy territory (default 1)"},
       "enabled": {"type": "boolean",
                   "description": "true = draw & execute offensive arrow (default); false = halt arrows under parent_order_id"},
       **TAG},
      ["orders_group", "parent_order_id"])
def _offensive_line(a):
    import sys as _sys
    g = game()
    if _sys.platform != "win32":
        return {"ok": False, "error": "hoi4_offensive_line is implemented for the Windows "
                                      "backend only (armyops_win)"}
    import armyops_win as A
    og = _ptr(a["orders_group"])
    provs = [int(x) for x in (a.get("provinces") or [])]
    enemy_tag = a.get("enemy_tag")
    depth = int(a.get("depth") or 1)
    enabled = bool(a.get("enabled", True))
    res = g.offensive_line(og, int(a["parent_order_id"]), provs, None,
                           enemy_tag=enemy_tag, depth=depth, enabled=enabled)
    res["backend"] = res.get("method") or "unknown"
    kids = [c for c in A.order_children(g, og) if not c.get("foreign_group")]
    res["children"] = [(c["id"], c["assigned"], c["executing"]) for c in kids]
    res["children_executing"] = [c["id"] for c in kids if c.get("executing") == 1]
    return res


@tool("hoi4_delete_order",
      "Safely delete a battle-plan order on PE 1.19.5.0 (uses cmdwin.alloc + CanExecute "
      "+ fake vtable + post_drained). When deleting a type-2 front order that has type-1 "
      "offensive children, it first clears executing flags and deletes all children, "
      "refusing to delete the parent if any child cannot be removed (prevents orphan "
      "children and crashes). Returns deleted_children.",
      {**OG, "order_id": {"type": "integer"}}, ["orders_group", "order_id"])
def _delete_order(a):
    return game().delete_order(_ptr(a["orders_group"]), a["order_id"])


@tool("hoi4_purge_orders",
      "Delete EVERY battle-plan order that holds 0 divisions -- front orders (type-2) "
      "AND their type-1 offensive children. The engine reads data[size-1] of the "
      "assignment array without checking size, so a 0-division order is a delayed "
      "ACCESS_VIOLATION at hoi4.exe+0xB2E62E (confirmed twice: 2026-11-17 and "
      "2026-11-18 15:10). Run it right after loading a save written before 2026-11-18 "
      "and before any war. Only empty orders are touched.",
      {**TAG})
def _purge_orders(a):
    return game().purge_empty_orders(a.get("tag"))


@tool("hoi4_reset_battleplans",
      "Delete EVERY battle-plan order (type-2 front orders AND their type-1 offensive "
      "children) on every army, then report what is left. Use this after loading a save: "
      "a front order that still has its executing byte set but no offensive children is "
      "an empty CPdxArray and hoi4.exe dies at +0xB2E62E as soon as the clock moves. "
      "Purging only the 0-division orders is NOT enough (2026-11-18 crash #5). Redraw a "
      "fresh plan with hoi4_army_battleplans afterwards.",
      {**TAG})
def _reset_battleplans(a):
    return game().reset_battleplans(a.get("tag"))


@tool("hoi4_paradrop",
      "Create or reuse a PARADROP order (battle-plan kind 4) on an orders group. order_id=0 "
      "(default) makes the engine allocate the type-4 order and store the source province; "
      "order_id=<id> reuses that order. The target is attached afterwards and the engine only "
      "accepts a land province that is not the source and lies inside paradrop range of a "
      "transport wing.",
      {**OG, "order_id": {"type": "integer",
                          "description": "0 = create a new type-4 order, else reuse this id"},
       "source": {"type": "integer",
                  "description": "source province id; omit to auto-pick the province whose air "
                                 "base holds this country's transport wing (required by the "
                                 "engine: 0x22EEFE0 computes range from that air base)"},
       "target": {"type": "integer", "description": "target province id"},
       "divisions": {"type": "array", "items": _INT,
                     "description": "division pointers (from hoi4_divisions) to assign"},
       "confirm": {"type": "boolean"}},
      ["orders_group", "target"])
def _paradrop(a):
    import armyops
    src = a.get("source")
    divs = [_ptr(x) for x in (a.get("divisions") or [])] or None
    ok, err = armyops.paradrop(game(), _ptr(a["orders_group"]),
                               None if src is None else int(src), int(a["target"]),
                               divs,
                               int(a.get("order_id") or 0), bool(a.get("confirm")))
    return {"ok": bool(ok), "error": err}


@tool("hoi4_paradrop_reserved",
      "List the divisions every automatic land dispatcher refuses to touch: paratrooper "
      "templates, divisions currently executing a type-4 order, and every division of an army "
      "that owns a paradrop order. This is the exemption set used by the front laws, "
      "activate_army_battleplans, housekeep_divisions and deploy_to_border.",
      {"tag": {"type": "string", "description": "country tag, default player"}})
def _paradrop_reserved(a):
    import divreserve
    return divreserve.reserved(game(), (a.get("tag") or None))


@tool("hoi4_paradrop_targets",
      "List the province ids the ENGINE accepts as a paradrop target for this army: land, not "
      "the source, and inside the range of the transport wing stationed in the source air "
      "base. Uses COrderInstance::CanSetProvinceTarget (0x22EEFE0), i.e. exactly the check "
      "COrderSetParadropTargetCommand::CanExecute uses.",
      {**OG, "source": {"type": "integer",
                        "description": "source province id; omit to auto-pick the transport "
                                       "air base province"},
       "limit": {"type": "integer", "description": "highest province id to scan (default 2000)"},
       "tag": {"type": "string", "description": "optional: only provinces this country controls"}},
      ["orders_group"])
def _paradrop_targets(a):
    import armyops
    src = a.get("source")
    out = armyops.paradrop_targets(game(), _ptr(a["orders_group"]),
                                   None if src is None else int(src),
                                   int(a.get("limit") or 2000))
    tag = (a.get("tag") or "").upper()
    if tag:
        try:
            import mapdata
            ctrl = mapdata.province_controller_map(game())
            out = [p for p in out if ctrl.get(p) == tag]
        except Exception:
            pass
    return {"count": len(out), "provinces": out}


# =============================================================== save analysis
# 2026-11-17: the @tool decorator used to sit on _save_dir() instead of
# _save_compare(), so the registered tool called a 0-arg helper with 1 arg and
# every hoi4_save_compare call died with
#   TypeError: _save_dir() takes 0 positional arguments but 1 was given
def _save_dir():
    """Where Paradox keeps HOI4 saves.  Linux uses ~/.local/share; the PE build
    writes to Documents\\Paradox Interactive\\... (OneDrive-redirected too)."""
    import sys
    if sys.platform == "win32":
        home = os.path.expanduser("~")
        for cand in (os.path.join(home, "Documents", "Paradox Interactive",
                                  "Hearts of Iron IV", "save games"),
                     os.path.join(home, "OneDrive", "Documents", "Paradox Interactive",
                                  "Hearts of Iron IV", "save games")):
            if os.path.isdir(cand):
                return cand
        return os.path.join(home, "Documents", "Paradox Interactive",
                            "Hearts of Iron IV", "save games")
    return os.path.expanduser("~/.local/share/Paradox Interactive/Hearts of Iron IV/save games")


@tool("hoi4_save_compare",
      "Compare countries FROM A SAVE FILE, without touching the running game. Parses "
      "ironman/binary saves with the game's own token table. Splits factories into CORE and "
      "OCCUPIED, because factories in occupied states only work in proportion to compliance "
      "and the raw total is misleading. Without save, the newest one is used.",
      {"tags": {"type": "array", "items": _STR, "description": "Karsilastirilacak tag'ler"},
       "save": {"type": "string", "description": "File name (optional)"}},
      ["tags"])
def _save_compare(a):
    import glob, os, savereader
    S = _save_dir()
    if a.get("save"):
        path = a["save"] if os.path.isabs(a["save"]) else os.path.join(S, a["save"])
    else:
        files = sorted(glob.glob(S + "/*.hoi4"), key=os.path.getmtime, reverse=True)
        if not files:
            return {"ok": False, "error": "save not found"}
        path = files[0]
    tokens = savereader.load_tokens()
    if not tokens:
        try:
            savereader.dump_tokens(game())
            tokens = savereader.load_tokens()
        except Exception:
            return {"ok": False, "error": "no token table yet; the game must have been open at least once"}
    return {"save": os.path.basename(path), "meta": savereader.meta(path, tokens),
            "countries": savereader.compare(path, [t.upper() for t in a["tags"]], tokens)}


@tool("hoi4_save_list", "Save files and their metadata (player, ideology, date, version).")
def _save_list(a):
    import glob, os, savereader
    S = _save_dir()
    tokens = savereader.load_tokens()
    out = []
    for f in sorted(glob.glob(S + "/*.hoi4"), key=os.path.getmtime, reverse=True)[:12]:
        try:
            rec = {"file": os.path.basename(f),
                   "mb": round(os.path.getsize(f) / 1e6, 1)}
            m = savereader.meta(f, tokens)
            # 2026-11-16: the header stores HOURS with a 5000-year offset, so the
            # tool used to print the raw integer (60795817). Decode it.
            if "date" in m:
                rec["date_raw"] = m["date"]
                m["date"] = savereader.decode_date(m["date"], 43800000) or m["date"]
            rec.update(m)
            out.append(rec)
        except Exception as e:
            out.append({"file": os.path.basename(f), "error": str(e)})
    return {"count": len(out), "saves": out}


# =============================================================== session and multiplayer
@tool("hoi4_session_info",
      "SESSION SUMMARY. Call this first: which country you are playing (my_tag/my_country), "
      "single or multiplayer (is_multiplayer, player_count, players), whether you are at war, "
      "active land combats, date.")
def _session_info(a):
    import session
    return session.info(game())


def _refuse_unpause_with_empty_orders(g):
    """Blocking dict when a 0-division battle-plan order exists, else None.

    2026-11-18 crash #4: the 1936.08 autosave CONTAINS three empty type-1
    children left behind by the 15:10 crash. Loading it and starting the clock
    killed hoi4.exe at 0xB2E62E within ~15 s (dump hoi4_20261118_153843).
    The engine reads data[size-1] of an empty CPdxArray, so any unpause with an
    empty order on the map is a guaranteed crash -- refuse instead.
    """
    try:
        empties = g.empty_order_scan()
    except Exception:
        return None          # cannot scan: do not block the player
    if not empties:
        return None
    return {"ok": False, "blocked_by": "empty_orders",
            "error": ("refusing to start the clock: %d battle-plan order(s) hold 0 "
                      "divisions. Run hoi4_purge_orders first -- hoi4.exe dies at "
                      "hoi4.exe+0xB2E62E as soon as the clock moves (dump "
                      "hoi4_20261118_153843)." % len(empties)),
            "empty_orders": empties[:12]}


@tool("hoi4_pause",
      "Pause or resume -- the same state change the player's SPACE key makes. On the "
      "Windows build this is the engine's own pause byte (idler+0x6C1), written the way "
      "the game's SPACE handler writes it; on Linux it goes through CPauseGame. The "
      "clock is sampled before and after (a paused game must show two identical "
      "date_raw() reads), so success is never judged from the flag alone. "
      "When resuming (paused=false), the red-dot gate blocks unpause if unresolved "
      "events/focus/research/PP>=500/XP>=100/idle factories/new decisions exist; "
      "resolve them first or pass ack_red_dots=[{code, reason, snooze_days, force}].",
      {"paused": _BOOL, "who": _STR,
       "ack_red_dots": {"type": "array",
                        "description": "Optional red-dot acknowledgements to apply before resuming",
                        "items": {"type": "object"}}})
def _pause_game(a):
    import session
    import sys as _sys
    g = game()
    if not a.get("paused", True):
        bad = _refuse_unpause_with_empty_orders(g)
        if bad:
            return bad
        try:
            import reddots as _rd
            gate = _rd.check_unpause_gate(
                g,
                ack_red_dots=a.get("ack_red_dots"),
                ensure_paused=True,
            )
            if gate:
                return gate
        except Exception:
            pass
    ok, msg = session.pause(g, a.get("paused", True), a.get("who"))
    # 2026-11-21 (P3-1 / P3-2): `{"ok": true, "error": "already paused"}` put a
    # success flag next to an error string, and the method (a direct write to
    # idler+0x6C1, not CPauseGame) was buried in that same string.  Separate the
    # three facts: did the call succeed, did anything CHANGE, and how was it done.
    # 2026-11-21 (second pass): `changed` was `ok and not msg`, but every path
    # returns a message, so it was ALWAYS false even when the byte really flipped.
    # "already ..." is the only message that means nothing changed.
    _m = str(msg or "")
    out = {"ok": bool(ok), "changed": bool(ok and not _m.startswith("already")),
           "requested": bool(a.get("paused", True))}
    if msg:
        if str(msg).startswith("already "):
            out["note"] = str(msg)          # a no-op, not a failure
        elif str(msg).startswith("unverified"):
            # 2026-11-21 (P0-3): the byte is set but this process's date reader is
            # pinned to game.log, so no clock witness exists.  Say that instead of
            # either claiming a popup or silently returning a bare ok.
            out["note"] = str(msg)
            out["clock_verified"] = False
        else:
            out["warning"] = str(msg)
    if ok and not msg:
        out["clock_verified"] = True
    out["method"] = ("idler+0x6C1 (the byte the SPACE key toggles)"
                     if _sys.platform == "win32" else "CPauseGame")
    if _sys.platform == "win32":
        out["multiplayer_note"] = ("Windows PE: pause and resume are the single byte "
                                   "idler+0x6C1, written exactly the way the game's own "
                                   "SPACE handler writes it (hoi4.exe+0xE48C36: "
                                   "'cmp [rbx+0x6C1],0 / sete al / mov [rbx+0x6C1],al'). "
                                   "Both directions verified live on pid 29864: 1 freezes "
                                   "the clock, 0 lets it run. Success is judged by the "
                                   "clock, not the flag, so a frozen clock with the flag "
                                   "already 0 is reported as a second pause source (event "
                                   "popup / menu) instead of a fake success; when the "
                                   "process's date reader is itself stuck on game.log the "
                                   "reply carries clock_verified:false rather than inventing "
                                   "either verdict. In a real multiplayer session the host "
                                   "owns this state.")
        if (not ok) and (not a.get("paused", True)):
            # 2026-11-21 (P0-3 completeness): the flag is 0 but the clock is frozen.
            # That is the ONE failure mode the byte cannot clear, because something
            # else is holding the pause: a country-event popup, a mod's news window
            # or an open menu.  Name it instead of leaving a bare warning, and point
            # at the tool that can actually answer it.
            out["blocked_by"] = "modal_window"
            try:
                import events_win
                _pend = events_win.pending(g)
            except Exception:
                _pend = []
            if _pend:
                out["blocked_by"] = "event_popup"
                out["pending_events"] = [r.get("event_id") for r in _pend][:8]
                out["hint"] = ("有 %d 个事件弹窗待回答，游戏被它们自己暂停了（不是玩家暂停）。"
                               "用 hoi4_events 查看，再用 hoi4_select_event_option(write=true) 回答。"
                               % len(_pend))
            else:
                out["hint"] = ("暂停字节已经是 0，但时钟仍不动：屏幕上有一个模态窗口（新闻/"
                               "事件弹窗或菜单）压着游戏。它不在 CGameState 的待回答事件数组里，"
                               "工具无法代答——请关掉这个窗口，然后重新调用 hoi4_pause(false)。")
    return out


@tool("hoi4_combat_report",
      "ACTIVE BATTLES: every live CLandCombat with its province, terrain, both sides' "
      "countries, division counts and organisation/HP pools. `count` is the real battle "
      "count (CGameState+0x260 CCombatManager -> +0x08 CPdxArray<CLandCombat*>), and "
      "all=true returns every battle in the world, not just ours. Defaults to compact=true "
      "(top-10 low_org_divisions, top-15 activity divisions, deprecated alias omitted) so the "
      "output stays well under 3k tokens; pass compact=false or low_org_limit=N for full detail. "
      "Nothing here is a cheat: pure reads.",
      {"all": _BOOL,
       "compact": {"type": "boolean", "description": "Compact output (default true, <3k tokens)"},
       "low_org_limit": {"type": "integer", "description": "Max low_org_divisions rows (default 10 in compact mode)"}})
def _combat_report(a):
    import combat
    g = game()
    compact = bool(a.get("compact", True))
    low_lim = int(a.get("low_org_limit") or (10 if compact else 80))
    low_lim = max(0, min(low_lim, 200))
    act_lim = 15 if compact else 40
    recs = combat.combats(g, only_mine=not a.get("all", False))
    out = {"count": len(recs),
           "compact": compact,
           "source": "CGameState+0x260 CCombatManager +0x08 -> CPdxArray<CLandCombat*>",
           "damage_rates_available": bool(combat.DAMAGE_RATES),
           "combats": [{k: v for k, v in r.items() if not k.startswith("_")} for r in recs]}
    try:
        out["activity"] = combat.activity(g, limit=act_lim)
    except Exception as e:
        out["activity_error"] = repr(e)
    _adc_raw = {}
    try:
        _adc_raw = combat.active_division_combats(g, limit=low_lim) or {}
        if compact:
            _divs = list(_adc_raw.get("divisions") or [])[:low_lim]
            out["low_org_divisions"] = {
                "count": int(_adc_raw.get("count") or len(_divs)),
                "showing": len(_divs),
                "truncated": int(_adc_raw.get("count") or 0) > len(_divs),
                "basis": _adc_raw.get("basis"),
                "tags": _adc_raw.get("tags"),
                "divisions": _divs,
            }
        else:
            out["low_org_divisions"] = _adc_raw
    except Exception as e:
        out["fallback_error"] = str(e)
    _act = out.get("activity") or {}
    if _act.get("first_sample") or _adc_raw.get("basis") != "delta":
        out["fighting_now"] = None
        _act["hint"] = ("first sample for this process -- call hoi4_combat_report "
                        "again after the clock runs to get kill/damage deltas")
    else:
        _kills = [r for r in (_act.get("divisions") or [])
                  if (r.get("killed_delta") or 0) > 0]
        out["fighting_now"] = bool(_kills)
        out["killing_now"] = {"divisions": len(_kills),
                              "killed_delta": sum(r["killed_delta"] for r in _kills)}
        out["taking_damage"] = {"divisions": _act.get("engaged") or 0,
                                "note": ("org/str fell -- out-of-supply attrition "
                                         "looks identical here, so only killed_delta>0 "
                                         "counts as fighting")}
    if not recs:
        out["note"] = ("no battle involves %s right now -- check `all: true` to see "
                       "every battle in the world" % g.player_tag())
    elif not combat.DAMAGE_RATES:
        out["note"] = ("the battle list is real, but the damage-rate fields are not "
                       "recovered on the Windows build: verdicts stay `unknown` and "
                       "`activity[].killed_delta` is the live fighting signal. The "
                       "micro engine refuses to act on a battle it cannot score.")
    return out


# =============================================================== rage micro
_rage = None


@tool("hoi4_rage_micro",
      "COMBAT MICRO ENGINE, written in code, so it costs no model tokens. Runs once per in- "
      "game day (a full tick is ~50 ms). Reads the game's own damage rates, smooths them with "
      "an EMA, and divides each side's organisation pool by them to work out who breaks "
      "first. Terrain, river, fort, entrenchment and stacking penalties all use the "
      "coefficients from the game's own files. Searches the province graph for ENCIRCLEMENTS "
      "(which 1-3 tiles, if taken, cut the enemy line) and reduces starving pockets "
      "(out_of_supply_days>=3). Cancels attacks that are being lost, scores adjacent enemy "
      "provinces and masses every ready division on a target when the odds justify it, pins "
      "enemy stacks that would otherwise reinforce, walks idle divisions to the nearest "
      "front, and halts broken or unsupplied ones. It will not re-target a division that is "
      "already marching. Orders are not battle-plan lines: each division gets its own right- "
      "click order (CMoveCommand). With too few samples, or when neither side is close to "
      "breaking, it does nothing. Everything goes through the player command channel, so it "
      "is valid in multiplayer and visible to other players. action: start | stop | status | "
      "tick | config.",
      {"action": _STR,
       "interval": {"type": "number", "description": "Tick interval in seconds (used when daily=false)"},
       "org_withdraw": {"type": "number", "description": "Withdraw below this fraction of peak organisation (0.35)"},
       "org_resume": {"type": "number", "description": "Resume attacking above this fraction (0.70)"},
       "supply_days_limit": _INT,
       "cancel_edge": {"type": "number", "description": "Break off the attack when org_edge falls below this (0.80)"},
       "attack_ratio": {"type": "number", "description": "Strength ratio required to attack (1.15)"},
       "attack_org": {"type": "number", "description": "Divisions below this organisation fill do not attack (0.55)"},
       "pin": {"type": "boolean", "description": "Pin adjacent enemy stacks that would REINFORCE an ongoing battle (true)"},
       "pin_ratio": {"type": "number", "description": "Minimum ratio to pin (0.80), the aim is to tie them down, not to win"},
       "advance": {"type": "boolean", "description": "Walk idle divisions with no adjacent enemy to the nearest front (true)"},
       "advance_depth": {"type": "integer", "description": "How many provinces deep the front search may go (25)"},
       "cut_ratio": {"type": "number", "description": "Threshold for an attack that CLOSES a pocket (0.90), worth some risk"},
       "reduce_ratio": {"type": "number", "description": "Threshold for reducing a starving pocket (1.05)"},
       "daily": {"type": "boolean", "description": "Run once per IN-GAME DAY (true); false uses a fixed second interval"},
       "poll": {"type": "number", "description": "How often to poll for a day change, in seconds (0.25)"},
       "ema_alpha": {"type": "number", "description": "EMA smoothing factor for damage rates (0.30)"},
       "min_samples": {"type": "integer", "description": "Minimum samples before deciding (3)"},
       "decisive_within": {"type": "integer", "description": "Do nothing if neither side breaks within this many ticks (72)"},
       "max_actions_per_tick": _INT,
       "dry_run": {"type": "boolean", "description": "true: report the decisions only, send no orders"}},
      ["action"])
def _rage_micro(a):
    global _rage
    import ragemicro
    act = a["action"]
    if _rage is None or act == "config":
        kw = {k: a[k] for k in ("interval", "org_withdraw", "org_resume",
                                "supply_days_limit", "dry_run", "cancel_edge",
                                "attack_ratio", "attack_org", "pin", "pin_ratio",
                                "advance", "advance_depth", "cut_ratio",
                                "reduce_ratio", "daily", "poll", "ema_alpha",
                                "min_samples", "decisive_within",
                                "max_actions_per_tick") if k in a}
        if _rage is not None and _rage.running():
            _rage.stop()
        # Give RageMicro its own Game() handle (like frontd._worker_game) so its
        # background thread never shares _depth/_inj with the main MCP thread.
        _rage = ragemicro.RageMicro(Game(), **kw)
        if act == "config":
            return {"ok": True, "config": _rage.status()}
    if act == "start":
        started = _rage.start()
        return {"ok": True, "started": started, "status": _rage.status()}
    if act == "stop":
        _rage.stop()
        return {"ok": True, "status": _rage.status()}
    if act == "tick":
        done = _rage.tick()
        return {"ok": True, "actions": done, "status": _rage.status()}
    if act == "status":
        return _rage.status()
    return {"ok": False, "error": "action: start|stop|status|tick|config"}


# =============================================================== turn loop
@tool("hoi4_world_snapshot",
      "Current value of every watched signal: focus, research slots, available decisions, "
      "idle military factories, construction queue, divisions ready to deploy, wars and "
      "battles, political power. PURE READS. Never stops the game, no desync risk in "
      "multiplayer. hoi4_wait_for compares these snapshots to work out why it woke up.")
def _world_snapshot(a):
    import turnloop
    return turnloop._public(turnloop.snapshot(game()))


@tool("hoi4_wait_for",
      "SLEEP AND WAKE AT THE RIGHT MOMENT. Say 'do not disturb me until X' and this watches "
      "the game and returns when something happens, instead of burning tokens checking every "
      "second. Wakes on: focus completed, research finished or a slot freed, a new decision "
      "became available, military factories went idle, construction queue emptied, a division "
      "is ready to deploy, WAR was declared, a new battle started, political power threshold. "
      "Even if none of that happens it ALWAYS returns when max_days runs out, there is no "
      "path that waits forever. If the game is paused, timeout_seconds is the real-time "
      "safety brake. The reply says what woke it and the current state.",
      {"max_days": {"type": "integer",
                    "description": "Wake after at most this many IN-GAME days (default 7)"},
       "until_date": {"type": "array", "items": {"type": "integer"},
                      "description": "[year, month, day]. Sleep until this date"},
       "signals": {"type": "array", "items": {"type": "string"},
                   "description": "Signals to watch: focus, research, decisions, events, production, "
                                  "construction, deployment, war (war declared/ended), peace (peace conference opened), "
                                  "battles, political_power. Pass [] to wait purely until max_days / until_date "
                                  "(no signal wakes except red dots if respect_red_dots=true)."},
       "wake_on_battle": {"type": "boolean",
                          "description": "Wake on every new individual land battle (default false; "
                                         "war declaration/end still wakes under 'war')"},
       "political_power": {"type": "integer",
                           "description": "Wake when political power reaches this value"},
       "poll_seconds": {"type": "number", "description": "Poll interval in seconds (default 1.5; auto-tightened near max_days to avoid overshoot)"},
       "timeout_seconds": {"type": "integer",
                           "description": "Real-time safety brake in seconds (default 24, "
                                          "capped at 26 so the call cleanly pauses and returns "
                                          "JSON well before typical 30s MCP client timeouts and the 50s server watchdog)"},
       "pause_on_wake": {"type": "boolean",
                         "description": "Pause on wake (timeout always pauses unconditionally to prevent background drift)"},
       "wartime_micro": {"type": "boolean",
                         "description": "While at war, run one local wartime_step every 3 game "
                                        "days without waking the model; only report after >=4 "
                                        "province changes or 15 days"},
       "wartime_enemy": {"type": "string",
                         "description": "Enemy tag for wartime_micro (empty = auto-detect)"},
       "wartime_interval_days": {"type": "integer",
                                 "description": "Game days between wartime micro ticks (default 3)"},
       "wartime_report_days": {"type": "integer",
                               "description": "Force a report after this many war days (default 15)"},
       "respect_red_dots": {"type": "boolean",
                            "description": "Enforce red-dot gate & auto-pause on events/decisions/PP/XP/idle factories/empty focus/research (default true)"}})
def _wait_for(a):
    import turnloop
    tl = turnloop.TurnLoop(game(), poll=a.get("poll_seconds", 1.5))
    until = a.get("until_date")
    # 2026-11-23 (复测 7 B-4): 将 wait_for 软上限从 38s 收紧至 26s（默认 24s），
    # 彻底避开常见 MCP 客户端 30s 请求超时引发的 `-32001` 协议中断，确保超时必返回结构化 JSON 且停表。
    CLIENT_BUDGET_S = min(26, max(5, int(CALL_TIMEOUT_S - 12)))
    DEFAULT_WAIT_S = min(24, CLIENT_BUDGET_S)
    try:
        _ts = int(a.get("timeout_seconds") or DEFAULT_WAIT_S)
    except Exception:
        _ts = DEFAULT_WAIT_S
    _ts = max(5, min(_ts, CLIENT_BUDGET_S))
    # 2026-11-23 (复测 6 P1-3): 显式传 signals=[] 必须保持空元组 ()，
    # 不可用 `a.get("signals") or ALL_SIGNALS` 把空列表 falsy 吞回全量信号。
    _raw_sigs = a.get("signals")
    _signals = turnloop.ALL_SIGNALS if _raw_sigs is None else tuple(_raw_sigs)
    return tl.wait(max_days=a.get("max_days", 7),
                   until_date=tuple(until) if until else None,
                   signals=_signals,
                   pp_threshold=a.get("political_power"),
                   timeout_seconds=_ts,
                   pause_on_wake=a.get("pause_on_wake", False),
                   wartime_micro=a.get("wartime_micro", False),
                   wartime_enemy=a.get("wartime_enemy"),
                   wartime_interval_days=a.get("wartime_interval_days", 3),
                   wartime_report_days=a.get("wartime_report_days", 15),
                   stop_event=_CALL.get("cancel"),
                   respect_red_dots=bool(a.get("respect_red_dots", True)),
                   wake_on_battle=bool(a.get("wake_on_battle", False)))


@tool("hoi4_red_dots",
      "Inspect all active red-dot gate blockers (pending events, new decisions/missions, "
      "empty focus/research, idle civ/mil/dockyard, PP>=500, Army/Navy/Air XP>=100, "
      "armies without general, unassigned divisions, surplus-no-training / deficit-over-training, "
      "wartime idle air/navy, fuel zero, high-payback obsolete production lines), "
      "currently snoozed acknowledgements, advisories, and background brake guard status.",
      {"fast": _BOOL})
def _red_dots(a):
    import reddots as _rd
    return _rd.evaluate(game(), fast=bool(a.get("fast", False)))


@tool("hoi4_ack_red_dot",
      "Explicitly acknowledge/snooze a red-dot gate blocker with a concrete strategic reason "
      "so the clock can resume. Strict items (PENDING_EVENT, NO_FOCUS, IDLE_RESEARCH) refuse "
      "normal ack and require force=true (max 7 game days). Other items snooze for 30 days by "
      "default and automatically re-arm if values escalate to the next tier or new objects appear.",
      {"code": {"type": "string", "description": "Red-dot code, e.g. PP_CAP / ARMY_XP_CAP / NEW_DECISION"},
       "reason": {"type": "string", "description": "Required strategic reason for deferring this red dot"},
       "snooze_days": {"type": "integer", "description": "Game days to snooze (default 30; strict items capped at 7)"},
       "force": {"type": "boolean", "description": "Required true to force-snooze strict items (PENDING_EVENT/NO_FOCUS/IDLE_RESEARCH)"}},
      ["code", "reason"])
def _ack_red_dot(a):
    import reddots as _rd
    return _rd.ack(
        game(),
        code=a["code"],
        reason=a["reason"],
        snooze_days=a.get("snooze_days"),
        force=bool(a.get("force", False)),
    )


@tool("hoi4_red_dot_guard",
      "Inspect or configure the red-dot gatekeeper and background read-only brake guard "
      "(enable/disable gate or background guard, toggle peacetime air/navy blocking, tune poll interval).",
      {"enabled": {"type": "boolean", "description": "Master switch for red-dot unpause/wait blocking"},
       "guard_enabled": {"type": "boolean", "description": "Enable/disable background auto-pause thread"},
       "peace_air_navy_block": {"type": "boolean", "description": "Also block peacetime idle air/navy (default false: wartime only)"},
       "guard_poll_seconds": {"type": "number", "description": "Background guard poll interval in seconds (default 1.5)"}})
def _red_dot_guard(a):
    import reddots as _rd
    cfg_updates = {}
    for k in ("enabled", "guard_enabled", "peace_air_navy_block", "guard_poll_seconds"):
        if k in a and a[k] is not None:
            cfg_updates[k] = a[k]
    if cfg_updates:
        _rd.configure(**cfg_updates)
    if a.get("guard_enabled") is False:
        _rd.stop_guard()
    elif a.get("guard_enabled") is True:
        _rd.start_guard(lambda: _g, busy_check=lambda: bool(_CALL.get("name")))
    return {"ok": True, "guard": _rd.guard_status()}


# =============================================================== veteran macros
@tool("hoi4_rebalance_production",
      "VETERAN MACRO: Automatically rebalance military factories across all lines based on "
      "real equipment stockpiles, surplus lines (e.g. >20k rifles wasting 16 factories), "
      "shortfalls (e.g. 0 artillery), and playbook ratios. Shrinks surplus lines FIRST to "
      "free factories, then tops up deficit lines. Pass dry_run=true to preview.",
      dict(TAG,
           dry_run={"type": "boolean", "description": "Preview rebalance plan without changing lines (default false)"},
           min_factories_per_line={"type": "integer", "description": "Minimum factories kept on active lines (default 1)"}))
def _rebalance_production(a):
    import macros as _mc
    return _mc.rebalance_production(
        game(),
        tag=a.get("tag"),
        dry_run=bool(a.get("dry_run", False)),
        min_factories_per_line=int(a.get("min_factories_per_line", 1)),
    )


@tool("hoi4_upgrade_template_preset",
      "VETERAN MACRO: One-step division template upgrade using validated veteran presets "
      "(inf_9_1, inf_9_3, inf_9_3_aa, inf_9_0_hold, armor_light_20w, armor_medium_30w). "
      "Auto-selects the main infantry/armor template if omitted, calculates army-wide equipment "
      "deficit, applies CUpdateDivisionTemplateCommand (with real XP check), and when "
      "auto_rebalance=true immediately shifts factories from surplus lines to cover artillery/support/armor deficits. "
      "Defaults to dry_run=true for safe preview; pass dry_run=false to execute.",
      dict(TAG,
           preset={"type": "string",
                   "description": "Preset key: inf_9_1, inf_9_3 (recommended), inf_9_3_aa, inf_9_0_hold, armor_light_20w, armor_medium_30w"},
           template={"type": "string", "description": "Optional explicit template name (auto-detected if omitted)"},
           auto_rebalance={"type": "boolean", "description": "Auto-rebalance military factories after upgrade (default true)"},
           dry_run={"type": "boolean", "description": "Preview XP cost, equipment deficit & factory rebalance (default true)"}))
def _upgrade_template_preset(a):
    import macros as _mc
    return _mc.upgrade_template_preset(
        game(),
        preset=a.get("preset", "inf_9_3"),
        template=a.get("template"),
        tag=a.get("tag"),
        auto_rebalance=bool(a.get("auto_rebalance", True)),
        dry_run=bool(a.get("dry_run", True)),
    )


@tool("hoi4_plan_front_allocation",
      "VETERAN MACRO: Multi-front strategic force allocation before war. Calls war_escalation_forecast "
      "to detect all bordering co-belligerents (guarantors / faction allies), reserves >=12% homeland "
      "strategic reserve, caps every front at its calibrated real supply capacity (never starving 42 "
      "divisions on 4 provinces), and outputs a verdict (ready / risky / suicidal). Pass execute=true "
      "to deploy safely while protecting secondary co-belligerent borders.",
      dict(TAG,
           target={"type": "string", "description": "Primary enemy tag (e.g. BEL, POL, FRA)"},
           reserve_ratio={"type": "number", "description": "Fraction of divisions held in homeland reserve (default 0.12)"},
           min_reserve={"type": "integer", "description": "Minimum homeland reserve divisions (default 4)"},
           execute={"type": "boolean", "description": "Execute deployment with secondary-border protection (default false: plan only)"},
           allow_understrength={"type": "boolean", "description": "Allow executing even when verdict is suicidal (default false)"}),
      ["target"])
def _plan_front_allocation(a):
    import macros as _mc
    return _mc.plan_front_allocation(
        game(),
        target_tag=a["target"],
        tag=a.get("tag"),
        reserve_ratio=float(a.get("reserve_ratio", 0.12)),
        min_reserve=int(a.get("min_reserve", 4)),
        execute=bool(a.get("execute", False)),
        allow_understrength=bool(a.get("allow_understrength", False)),
    )


@tool("hoi4_strategic_health",
      "Pure read-only veteran strategic health board: checks division count vs year curve, "
      "main infantry template quality vs idle army XP, production imbalance (rifle surplus vs artillery zero), "
      "and live out-of-supply divisions.",
      TAG)
def _strategic_health(a):
    import macros as _mc
    return _mc.strategic_health_board(game(), tag=a.get("tag"))


# =============================================================== diplomacy
@tool("hoi4_wargoal_types", "War goal types (annex_everything, take_state, ...).")
def _wargoal_types(a):
    return game().wargoal_types()


@tool("hoi4_justify_wargoal",
      "Start justifying a WAR GOAL against a target. Default type is take_state (conquer a "
      "state) — annex_everything is a script-only goal and is refused by CanExecute in "
      "1.19.4.1. With no states given, the target's states are tried until one is accepted.",
      {"target": {"type": "string"},
       "wargoal": {"type": "string",
                   "description": "take_state (default), take_core_state, topple_government..."},
       "states": {"type": "array", "items": {"type": "integer"},
                  "description": "Target state ids; omit to auto-pick"},
       "dry_run": {"type": "boolean",
                   "description": "Only check CanExecute, do not start the justification"},
       "force": {"type": "boolean",
                 "description": "Bypass the historical-playbook gate (needed for a 1936 opening war)"}},
      ["target"])
def _justify(a):
    return game().justify_wargoal(a["target"], a.get("wargoal", "take_state"),
                                  a.get("states"), a.get("dry_run", False),
                                  a.get("force", False))


@tool("hoi4_justifying",
      "War goals currently being justified, with exact days_left, eta_date and the "
      "remaining PP drain. Reads CTimedWargoalActivity: +0x10 target, +0x14 owner, "
      "+0x18 days left, +0x20 days per day. `states_in_activity` is the state "
      "array the engine reads when the timer hits zero (+0x68/+0x74) -- 0 means "
      "the completion message will crash hoi4.exe at +0xB2E62E; fix it with "
      "hoi4_wargoal_repair.")
def _justifying(a):
    return game().justifying()


@tool("hoi4_wargoal_repair",
      "Repair a timed war goal whose state array is EMPTY. The engine formats the "
      "'war goal finished' message from states[size-1] (CTimedWargoalActivity "
      "+0x68/+0x70/+0x74) and reads [-4] when the list is empty -- hoi4.exe dies "
      "at +0xB2E62E the moment the justification completes (verified twice on "
      "2026-11-19). This allocates a 1-element array from the game's own heap and "
      "writes ptr/capacity/size, so the timer can finish safely.",
      {"target": {"type": "string", "description": "country tag, e.g. HOL"},
       "state": {"type": "integer", "description": "state id to record (default: the target's first state)"},
       **TAG})
def _wargoal_repair(a):
    g = game()
    tgt = (a.get("target") or "").upper()
    rows = [x for x in g.justifying()
            if str(x.get("target") or "").upper() == tgt]
    if not rows:
        return {"ok": False, "error": "no timed war-goal activity against %s" % tgt}
    out = []
    for r in rows:
        sid = a.get("state") or g._pick_target_state_id(tgt)
        out.append(g.ensure_wargoal_state_array(r, sid))
    return {"ok": all(x.get("ok") for x in out), "target": tgt, "results": out,
            "justifying": g.justifying()}


@tool("hoi4_wargoal_quote",
      "Quote a war goal BEFORE starting it. On PE 1.19.5.0 this is a PURE-READ "
      "static estimate: current political power, concurrent justifications, "
      "estimated days/completion date and total PP drain from the game's own "
      "00_invasion.txt cost model. required_pp and can_justify_now are null "
      "because writing PP or calling the diplomatic validator crashes hoi4.exe "
      "(crashes #6/#7). On Linux the engine-exact CanExecute binary search is "
      "still used. Nothing is written or posted.",
      {"target": {"type": "string"},
       "wargoal": {"type": "string",
                   "description": "take_state (default), take_core_state, topple_government..."},
       "states": {"type": "array", "items": {"type": "integer"},
                  "description": "Target state ids (omit for the base one-state quote)"},
       "tag": {"type": "string", "description": "Source country (default: the player)"}},
      ["target"])
def _wargoal_quote(a):
    return game().wargoal_quote(a["target"], a.get("wargoal", "take_state"),
                                a.get("states"), a.get("tag"))


@tool("hoi4_wargoals",
      "Every live CWarGoal object (deep scan of all writable memory), so a casus belli "
      "created mid-game is not missed.",
      {"deep": {"type": "boolean", "description": "Full address-space scan (default true)"}})
def _wargoals(a):
    import diplomacy
    g = game()
    return diplomacy.wargoals(g, deep=a.get("deep", True))


@tool("hoi4_diplomatic_actions",
      "Diplomatic action classes that can be sent, plus every action class in the game.")
def _dip_actions(a):
    return game().diplomatic_actions()


@tool("hoi4_send_diplomatic_action",
      "Send a diplomatic action (guarantee, non-aggression pact, military access, "
      "embargo...). dry_run=true only checks whether it would be allowed.",
      {"action_class": {"type": "string", "description": "e.g. CNonAggressionPactAction"},
       "target": {"type": "string"},
       "dry_run": {"type": "boolean"}},
      ["action_class", "target"])
def _send_dip(a):
    return game().send_diplomatic_action(a["action_class"], a["target"], a.get("dry_run", False))


@tool("hoi4_declare_war",
      "Declare war. Runs the full preflight first and REFUSES unless all hard "
      "checks pass: (1) divisions can be counted and are non-zero; (2) diplomatic "
      "legality (not self / already at war / same faction ally / own subject); "
      "(3) at least one division stands on a border province facing the target; "
      "(4) a NATIVE battle plan faces the target (type-2 front line + type-1 "
      "offensive line + assigned > 0); (5) a live casus belli exists against the "
      "target; (6) diplomatic escalation check: inspects the target's faction, "
      "guarantors, overlord/subjects and their faction allies -- refuses if a "
      "newly pulled-in bordering power (>=3 border provinces, >=5 divs or major) "
      "has 0 divisions on that border (unless ack_co_belligerents=true), and "
      "warns on thin/unlined co-belligerent borders. Always returns "
      "co_belligerents, guaranteed_by, target_faction and co_belligerent_borders.",
      {"target": {"type": "string"}, "dry_run": {"type": "boolean"},
       "force": {"type": "boolean",
                 "description": "Skip the ADVISORY war-readiness warnings (rifles, "
                                "division ratio, historical timeline). Hard blockers "
                                "still apply."},
       "ack_co_belligerents": {"type": "boolean",
                               "description": "Explicitly acknowledge undefended borders "
                                              "against co-belligerents (guarantors / "
                                              "faction allies) pulled in by this war."}},
      ["target"])
def _declare_war(a):
    # 2026-11-23 (反糊弄升级): B-1 战争借口已升级为 O(1) 实时权威直读 CDiplomacyStatus+0x80，
    # 不再存在假阴性。MCP 工具层默认禁用 override_checks，防止模型一把跳过全部硬闸送死；
    # 仅在人类调试显式开启 HOI4_ALLOW_OVERRIDE_CHECKS=1 时允许传入。
    want_override = bool(a.get("override_checks", False))
    allow_override = os.environ.get("HOI4_ALLOW_OVERRIDE_CHECKS", "0").strip().lower() in (
        "1", "true", "yes", "on"
    )
    if want_override and not allow_override:
        return {
            "ok": False,
            "blocked_by": "override_checks_disabled",
            "target": str(a["target"]).upper(),
            "error": (
                "override_checks is disabled for autonomous MCP play. "
                "Wargoal verification now uses O(1) live CDiplomacyStatus+0x80 (no fast-list false negative). "
                "Satisfy all hard war-readiness gates (divisions, border garrison, native battleplan, live CB) "
                "instead of bypassing them."
            ),
        }
    return game().declare_war(
        a["target"], a.get("dry_run", False),
        a.get("force", False), want_override if allow_override else False,
        ack_co_belligerents=a.get("ack_co_belligerents", False),
    )


@tool("hoi4_war_readiness",
      "The declare_war preflight, without declaring: division count, diplomatic "
      "escalation forecast (target faction, guarantors, overlord/subjects, all "
      "co-belligerents pulled in and border defense exposure against each of them), "
      "target border presence, native front+offensive battle plan, and live casus "
      "belli. Cheap enough to call before every war.",
      {"target": {"type": "string"},
       "ack_co_belligerents": {"type": "boolean",
                               "description": "Evaluate readiness assuming undefended co-belligerent borders are acknowledged"},
       **TAG}, ["target"])
def _war_readiness(a):
    return game().war_readiness_check(
        a["target"], a.get("tag"),
        ack_co_belligerents=bool(a.get("ack_co_belligerents", False)),
    )


# ------------------------------------------------------------ peace conference
def _country_index(g, tag):
    if not tag:
        return int(g.player_tag_id())
    g._load_tags()
    return int(g._tag2idx[g._tags.index(str(tag).upper())])


@tool("hoi4_peace_status",
      "Peace conference: whether one is open, the engine's bidding maps, and each "
      "participant's score. Cheap (O(1)) -- safe to poll after a country capitulates.", {})
def _peace_status(a):
    import peacewin
    return peacewin.summary(game())


@tool("hoi4_peace_options",
      "List every state demand the open peace conference offers against one loser "
      "(state id + localised name + action pointer). Cached per (conference, winner, "
      "loser) so repeated calls do not re-allocate engine CPeaceAction objects.",
      {"loser": dict(_STR, description="Country tag that lost, e.g. HOL"),
       "winner": dict(_STR, description="Country that takes (empty = the player)")},
      ["loser"])
def _peace_options(a):
    import peacewin
    g = game()
    with g:
        lose = _country_index(g, a["loser"])
        win = _country_index(g, a.get("winner"))
        res = peacewin.options(g, lose, winner_idx=win)
        if res.get("ok"):
            res["loser"] = str(a["loser"]).upper()
            if a.get("winner"):
                res["winner"] = str(a["winner"]).upper()
        return res


@tool("hoi4_peace_demands",
      "Submit peace-conference demands for the player (or `winner`) against a beaten country. "
      "This is the conference's 提交要求 button: the demands go into the conference's turn map "
      "exactly as if they had been selected and submitted in the window. Omit `states` with "
      "`kinds=state` to take *every* state of the loser, or pass `states=[...]` (from "
      "`hoi4_peace_options`) to demand only a chosen subset.",
      {"loser": dict(_STR, description="Country tag that lost, e.g. HOL"),
       "winner": dict(_STR, description="Country that takes (empty = the player)"),
       "kinds": {"type": "array", "items": {"type": "string",
                                            "enum": ["state", "puppet", "navy", "liberate", "gov"]},
                 "description": "Demand types; default ['state']"},
       "states": {"type": "array", "items": _INT,
                  "description": "Optional subset of state IDs to demand (from hoi4_peace_options)."}},
      ["loser"])
def _peace_demands(a):
    import peacewin
    g = game()
    with g:
        conf = peacewin.live_conference(g)
        if not conf:
            return {"ok": False, "error": "no peace conference is open"}
        win = _country_index(g, a.get("winner"))
        lose = _country_index(g, a["loser"])
        kinds = a.get("kinds") or ["state"]
        states = a.get("states")
        finfo = {}
        try:
            turn, acts = peacewin.submit_demands(
                g, conf, win, lose, kinds, states=states, filter_info=finfo
            )
        except ValueError as e:
            return {"ok": False, "error": str(e), **finfo}
        out = {"ok": True, "turn": turn, "actions": len(acts),
               "detail": [{"type": g.p.i32(x + 8), "winner": g.p.i32(x + 0x30),
                           "loser": g.p.i32(x + 0x34), "state": g.p.i32(x + 0x48)}
                          for x in acts],
               "status": peacewin.summary(g)}
        if states is not None:
            out["requested_states"] = finfo.get("requested_states")
            out["matched_states"] = finfo.get("matched_states")
            if finfo.get("ignored_states"):
                out["ignored_states"] = finfo.get("ignored_states")
        return out


@tool("hoi4_peace_finish",
      "Advance a peace conference: `done` = 确认并退出 (applies the treaty; it does NOT "
      "pass first -- `pass` adds a second turn and the engine then hangs while resizing the "
      "history, research 71), `pass` = 跳过本轮, `endturn` = end this country's turn. "
      "Runs the command body directly because the CCommand queue does not drain while a "
      "conference is open.",
      {"action": {"type": "string", "enum": ["pass", "endturn", "done"]},
       "tag": dict(_STR, description="Country to act for (empty = the player)")},
      ["action"])
def _peace_finish(a):
    import peacewin
    g = game()
    with g:
        if a["action"] == "done":
            # `pass` appends a second turn to +0x1F8 and the engine then resizes the
            # history (2 -> 1) while applying the treaty -- that path hung the game
            # twice on 2026-11-15 (research 71).  Done on its own keeps the count at 1.
            return peacewin.finish(g, _country_index(g, a.get("tag")))
        return peacewin.execute(g, a["action"], _country_index(g, a.get("tag")))


@tool("hoi4_debug_console",
      "CHEAT/DEBUG: run a developer console command (383 of them). Do NOT use in normal play "
      "-- it is not something a player can do.",
      {"command": _STR}, ["command"])
def _console(a):
    g = game()
    with g:
        return g.console(a["command"]).to_dict()


@tool("hoi4_debug_list_commands", "CHEAT/DEBUG: console command list plus the game's own documentation.",
      {"filter": _STR, "with_docs": _BOOL, "limit": _INT})
def _list_cmds(a):
    g = game()
    cat = g.command_catalog()
    f = (a.get("filter") or "").lower()
    docs = command_docs() if a.get("with_docs") else {}
    out = {}
    for name, info in sorted(cat.items()):
        # 2026-11-13: command_catalog() drops the "aliases" key for commands whose
        # CConsoleCmd entry has none, which made this tool raise KeyError: 'aliases'
        # for every caller.
        _al = info.get("aliases") or []
        if f and f not in (name + " " + " ".join(_al)).lower():
            continue
        rec = dict(info)
        if docs:
            rec["doc"] = docs.get(name, "")
        out[name] = rec
        if a.get("limit") and len(out) >= a["limit"]:
            break
    return {"count": len(out), "commands": out}


@tool("hoi4_debug_call_function",
      "CHEAT/DEBUG: call a function inside the game via ptrace. address = ELF offset. "
      "DANGEROUS: calling a function whose signature is unknown with guessed arguments WILL "
      "crash the game (this has happened). Only addresses on the verified list are allowed; "
      "for anything else, recover the signature by disassembly first, then pass force=true.",
      {"address": _STR, "args": {"type": "array", "items": _INT},
       "signed": _BOOL, "absolute": _BOOL, "force": _BOOL}, ["address"])
def _call_fn(a):
    g = game()
    r = g.call(int(a["address"], 0), a.get("args", []), signed=a.get("signed", False),
               absolute=a.get("absolute", False), force=a.get("force", False))
    return {"result": r, "hex": hex(r & 0xFFFFFFFFFFFFFFFF)}


@tool("hoi4_clear_dirty_injector",
      "Clear the winject dirty flag after a timed-out game call has settled (recovers "
      "frontd and tool calls stuck on 'injector is dirty: ... call winject.clear_dirty(PID)'). "
      "Verifies the game process is still alive before clearing both winject._DIRTY_PIDS, "
      "the main Game injector, and frontd's worker injector.",
      {"pid": {"type": "integer", "description": "Optional game PID (defaults to current hoi4.exe PID)"}})
def _clear_dirty_injector(a):
    from process import winject as _wj
    g = game()
    pid = a.get("pid")
    if pid is None:
        pid = getattr(getattr(g, "p", None), "pid", None)
    before = _wj.dirty_status(pid)
    alive = _is_pid_alive(pid) if pid is not None else None
    if alive is False:
        return {
            "ok": False,
            "pid": pid,
            "process_alive": False,
            "before": before,
            "error": "process %s is no longer running; restart hoi4.exe instead of clearing dirty" % pid,
        }
    cleared_pids = _wj.clear_dirty(pid)
    main_inj_cleared = False
    inj = getattr(g, "_inj", None)
    if inj is not None and getattr(inj, "dirty", False):
        if pid is None or getattr(inj, "pid", None) == int(pid):
            inj.dirty = False
            main_inj_cleared = True
    frontd_res = None
    try:
        import frontd as _fd
        frontd_res = _fd.clear_dirty(pid)
    except Exception as e:
        frontd_res = {"ok": False, "error": str(e)}
    after = _wj.dirty_status(pid)
    return {
        "ok": True,
        "pid": pid,
        "process_alive": alive,
        "before": before,
        "after": after,
        "cleared_pids": cleared_pids,
        "main_injector_cleared": main_inj_cleared,
        "frontd": frontd_res,
    }


@tool("hoi4_debug_eval",
      "CHEAT/DEBUG: evaluate a Python expression inside the MCP process, with `g` bound to "
      "the live Game object, `p` to its process reader, `O` to offsets, `winject` to the "
      "Windows injector module, and struct/json available. Reverse-engineering only.",
      {"expr": _STR}, ["expr"])
def _eval(a):
    import struct as _struct
    g = game()
    _wj = None
    try:
        from process import winject as _wj
    except Exception:
        _wj = None
    ns = {"g": g, "p": g.p, "O": O, "struct": _struct, "json": json, "winject": _wj}
    expr = a["expr"]
    if expr.startswith("@"):
        expr = io.open(expr[1:], encoding="utf-8").read()
    if "\n" in expr:
        exec(compile(expr, "<eval>", "exec"), ns)
        out = ns.get("out")
        try:
            json.dumps(out)
            return out
        except Exception:
            return {"repr": repr(out)[:4000]}
    val = eval(expr, ns)
    try:
        json.dumps(val)
        return val
    except Exception:
        return {"repr": repr(val)[:2000]}


@tool("hoi4_debug_read_memory",
      "CHEAT/DEBUG: raw memory read. type: "
      "u8|u16|u32|i32|u64|i64|f32|f64|bytes|cstr|stdstring",
      {"address": _STR, "type": _STR, "size": _INT, "relative": _BOOL}, ["address"])
def _read_mem(a):
    g = game()
    addr = int(a["address"], 0)
    if a.get("relative"):
        addr = g.va(addr)
    t = a.get("type", "u64")
    p = g.p
    if t == "bytes":
        return {"hex": p.read(addr, a.get("size", 32)).hex()}
    fn = {"u8": p.u8, "u16": p.u16, "u32": p.u32, "i32": p.i32, "u64": p.u64,
          "i64": p.i64, "f32": p.f32, "f64": p.f64, "cstr": p.cstr,
          "stdstring": p.stdstring}.get(t)
    if not fn:
        raise ValueError("unknown type: %s" % t)
    v = fn(addr)
    return {"value": v, "hex": hex(v) if isinstance(v, int) else None}


@tool("hoi4_debug_object_index",
      "CHEAT/DEBUG: count object classes in memory using RTTI (for exploration).",
      {"filter": _STR, "limit": _INT})
def _objidx(a):
    g = game()
    c = g.oi.counts(a.get("filter", ""))
    lim = a.get("limit") or 40
    return dict(list(c.items())[:lim])


# ----------------------------------------------------------------- MCP loop
def rpc_result(i, r): return {"jsonrpc": "2.0", "id": i, "result": r}
def rpc_error(i, c, m): return {"jsonrpc": "2.0", "id": i, "error": {"code": c, "message": m}}


# ---------------------------------------------------------------- watchdog
# 2026-11-21 (P0-2): the whole server is ONE `for line in sys.stdin` loop, so a
# tool that blocks (a 3.8 GB scan, an engine command that never returns, a stuck
# injector) froze every later call: the client gave up at ~60 s and everything
# after that was -32001 until the process was killed.  That is also how a
# half-suspended hoi4.exe was left behind.
#
# Now every tools/call runs in a worker thread with a hard budget.  If it blows
# the budget the caller gets a real answer instead of silence, and the NEXT call
# is refused while the worker still touches game memory -- interleaving two
# injector users is what corrupts the process, so "busy" must be an error, not a
# queue.
CALL_TIMEOUT_S = 50.0
_CALL = {"thread": None, "name": None, "started": 0.0, "cancel": None}


def _emergency_pause_game():
    """零注入 1 字节直写紧急停表（仅在看门狗超时兜底使用，无线程劫持/锁冲突风险）。"""
    try:
        g = _g
        if g is None:
            return None
        import session as _sess
        p = getattr(g, "p", None)
        if p is None:
            return None
        idler = p.u64(p.base + _sess.G_IDLER)
        if not idler:
            return None
        p.write(idler + _sess.IDLER_PAUSED, b"\x01")
        return bool(p.u8(idler + _sess.IDLER_PAUSED))
    except Exception:
        return None


def _run_with_timeout(fn, args, name):
    import threading
    import time as _t
    prev = _CALL.get("thread")
    if prev is not None and prev.is_alive():
        _el = _t.time() - _CALL.get("started", _t.time())
        # 2026-11-21 (续测 P1-1): a timed-out call keeps running (a Python thread
        # cannot be cancelled safely -- it is inside ctypes/numpy memory reads and
        # killing it would leave the injector frozen mid-hijack).  So the honest
        # answer is: say exactly which call is holding the game, how long it has
        # been running, and that the only current way out is to let it finish or
        # restart the server.  Long calls SHOULD be bounded at the source (see the
        # probe budget in selectable_focuses and the air/enqueue paths).
        _hint = ("Wait for it, or restart the MCP server (Stop-Process the wx_server "
                 "python pid, then mcp_load('hoi4')) -- Python cannot cancel it.")
        if _el > 120:
            _hint = ("It has been running for %.0f s, far past the %.0f s budget: "
                     "restart the MCP server." % (_el, CALL_TIMEOUT_S))
        return {"ok": False, "busy": True,
                "running_call": _CALL.get("name"), "running_s": round(_el, 1),
                "error": ("a previous tool call is still running (%s, %.0fs). Refusing "
                          "to interleave game-memory access; %s"
                          % (_CALL.get("name"), _el, _hint))}
    box = {}
    cancel_ev = threading.Event()

    def _work():
        try:
            box["out"] = fn(args)
        except BaseException as e:            # noqa: BLE001 - reported to the caller
            box["err"] = e

    th = threading.Thread(target=_work, daemon=True, name="call:%s" % name)
    _CALL.update(thread=th, name=name, started=_t.time(), cancel=cancel_ev)
    th.start()
    th.join(CALL_TIMEOUT_S)
    if th.is_alive():
        # 2026-11-22 (复测 5 P1 §3.2): 立刻置位取消事件通知长循环（如 hoi4_wait_for）中止，
        # 并无条件通过零注入 1 字节直写暂停游戏，杜绝客户端收到 timeout 后后台白烧几十游戏日。
        cancel_ev.set()
        paused_now = _emergency_pause_game()
        th.join(1.5)
        if not th.is_alive() and "out" in box:
            out = box["out"]
            if isinstance(out, dict):
                out.setdefault("paused", paused_now)
                out.setdefault("timed_out", True)
            return out
        return {"ok": False, "timeout": True, "ran_in_background": True,
                "cancel_requested": True,
                "paused": paused_now,
                "error": ("%s exceeded the %.0fs per-call budget and is STILL RUNNING "
                          "server-side (game paused=%s to prevent clock drift). Do not "
                          "retry until it ends -- the next call will be refused while it "
                          "holds the game." % (name, CALL_TIMEOUT_S, paused_now))}
    if "err" in box:
        raise box["err"]
    return box.get("out")


def handle(msg):
    method, req_id = msg.get("method"), msg.get("id")
    params = msg.get("params") or {}
    if method == "initialize":
        return rpc_result(req_id, {"protocolVersion": PROTOCOL, "capabilities": {"tools": {}},
                                   "serverInfo": {"name": "hoi4", "version": "2.0.0"}})
    if method in ("notifications/initialized", "notifications/cancelled"):
        return None
    if method == "ping":
        return rpc_result(req_id, {})
    if method == "tools/list":
        return rpc_result(req_id, {"tools": _active_tools()})
    if method == "tools/call":
        tname = params.get("name")
        if _MCP_PROFILE == "compact" and _COMPACT_STRICT and tname not in COMPACT_TOOL_NAMES:
            return rpc_error(
                req_id,
                -32601,
                "tool '%s' is disabled in compact profile (allowed: %s). "
                "Use hoi4_inspect / hoi4_run_plan instead, or set HOI4_MCP_COMPACT_STRICT=0."
                % (tname, ", ".join(COMPACT_TOOL_NAMES)),
            )
        if tname == "hoi4_debug_eval" and not (_ALLOW_DEBUG and _ALLOW_EVAL):
            return rpc_error(
                req_id,
                -32601,
                "tool 'hoi4_debug_eval' is disabled by default for security. "
                "Set both HOI4_MCP_ALLOW_DEBUG=1 and HOI4_MCP_ALLOW_EVAL=1 only for local RE debugging.",
            )
        if isinstance(tname, str) and tname.startswith("hoi4_debug_") and not _ALLOW_DEBUG:
            return rpc_error(
                req_id,
                -32601,
                "debug/cheat tool '%s' is disabled by default. "
                "Set HOI4_MCP_ALLOW_DEBUG=1 only for local reverse-engineering."
                % tname,
            )
        fn = HANDLERS.get(tname)
        if not fn:
            return rpc_error(req_id, -32601, "unknown tool: %s" % tname)
        call_args = params.get("arguments") or {}
        if not isinstance(call_args, dict):
            call_args = {}
        # 2026-11-22: 统一按工具 schema.required 预检缺失必填参数，
        # 返回结构化 ok:false + missing_parameter，消灭土耳其语 HATA/KeyError 栈回溯。
        req_keys = _tool_required_keys(tname)
        missing = [k for k in req_keys if k not in call_args or call_args.get(k) is None]
        if missing:
            out = {
                "ok": False,
                "blocked_by": "missing_parameter",
                "tool": tname,
                "missing": missing,
                "error": "missing required parameter(s): %s" % ", ".join(missing),
            }
            return rpc_result(req_id, {"content": [{
                "type": "text",
                "text": json.dumps(out, ensure_ascii=False, indent=1),
            }]})
        try:
            out = _run_with_timeout(fn, call_args, tname)
        except Exception as e:
            return rpc_result(req_id, {"content": [{"type": "text",
                "text": "ERROR: %s\n%s" % (e, traceback.format_exc())}], "isError": True})
        content = []
        if isinstance(out, dict) and "_image" in out:
            content.append({"type": "image", "data": out.pop("_image"), "mimeType": "image/png"})
        content.append({"type": "text",
                        "text": json.dumps(out, ensure_ascii=False, indent=1, default=str)})
        return rpc_result(req_id, {"content": content})
    if req_id is None:
        return None
    return rpc_error(req_id, -32601, "unsupported method: %s" % method)


# ============================================================== staff layer
_STAFF_APPLIED = {"date": None, "move_date": None, "log": []}
@tool("hoi4_staff_tick",
      "Staff policy layer, one entry point. SUPPLY: how many divisions each province "
      "can actually feed (infrastructure, supply hub, railway, victory points - all "
      "from the game's own map and history files), which provinces are over capacity "
      "and where the surplus should go. detail=full returns the per-province table; "
      "every tick is appended to calling/staff_shadow.jsonl so the coefficients can "
      "be fitted against the engine's own supply ratios. "
      "READ-ONLY BY DEFAULT: without apply=true this only reports plans "
      "(live:false, applied_this_tick:false) and changes NOTHING - not supply, not "
      "industry, not the army, not the focus. apply=true performs all four, at most "
      "once per game day, and never while paused.",
      dict(TAG, detail=_STR, apply=_BOOL), [])
def _staff_tick(a):
    """One call, three policies, and (in live mode) one apply per game day.

    Applying on every call would re-issue the same queue/production/move
    commands every 15 seconds: CCommand::Run only queues, so a paused game would
    accumulate duplicates and a second create_army in the same tick would take
    the same divisions again. Hence: apply at most once per game day, and never
    while the game is paused.

    2026-11-22 (P1-5): `live` defaulted to True, so a plain
    `hoi4_staff_tick(detail="summary")` -- described as "shadow mode, reports and
    changes nothing" -- queued two factories, folded four divisions into an army
    AND SET A NATIONAL FOCUS (GER_reorganize_the_wehrmacht, mutually exclusive
    with the Neurath branch).  Only SUPPLY was ever shadowed.  The default is now
    read-only; apply=true is the only way to write.
    """
    import staff_supply as ss
    g = game()
    try:
        date = g.date().get("text")
    except Exception:
        date = None
    shadow = bool(ss.cfg().get("shadow", True))
    want_apply = a.get("apply")
    # 2026-11-20 (P1 #9): `hoi4_staff_tick(apply=true)` used to be a no-op for
    # the SUPPLY moves because supply.shadow in staff_config.json was consulted
    # as a veto instead of as the default.  An explicit apply now decides:
    # apply=false keeps shadow (report only), apply=true performs the moves.
    # 2026-11-22 (P1-5): and an ABSENT apply means report only as well.
    if want_apply is None:
        live = False
    else:
        live = bool(want_apply)
        shadow = not live
    paused = True
    try:
        import session as _sess
        paused = bool(_sess.is_paused(g))
    except Exception:
        paused = True
    # supply.shadow must only stop the supply MOVES. It used to stop the whole
    # policy layer, so the 1944-04 mitigation froze industry, army and focus as
    # well: 1944-08..1947 the country sat at 2000 PP with an empty focus and no
    # construction while the staff reported "ok".
    due_moves = bool(live and not shadow and date
                     and date != _STAFF_APPLIED.get("move_date") and not paused)
    due = bool(live and date and date != _STAFF_APPLIED["date"] and not paused)
    _tag = a.get("tag")
    rep = ss.plan(g, _tag, apply_moves=due_moves)
    try:
        rep["logged"] = ss.shadow_log(rep, date)
    except Exception as e:
        rep["logged"] = "error: %r" % (e,)
    if (a.get("detail") or "summary") != "full":
        rep.pop("provinces", None)
        rep["overloaded"] = rep.get("overloaded", [])[:8]
        rep["moves"] = rep.get("moves", [])[:12]
    rep["date"] = date
    rep["live"] = live
    rep["applied_this_tick"] = due
    rep["applied_moves_this_tick"] = due_moves
    rep["supply_shadow"] = shadow
    if due:
        _STAFF_APPLIED["date"] = date
    if due_moves:
        _STAFF_APPLIED["move_date"] = date
    try:
        import staff_industry as si
        ind = si.plan(g, _tag)
        if due:
            ind["applied"] = si.apply(g, _tag, ind)
        rep["industry"] = ind
    except Exception as e:
        rep["industry"] = {"ok": False, "error": repr(e)}
    try:
        import staff_army as sa
        army = sa.plan(g, _tag)
        if due:
            army["applied"] = sa.apply(g, _tag, army)
        rep["army"] = army
    except Exception as e:
        rep["army"] = {"ok": False, "error": repr(e)}
    try:
        import staff_focus as sf
        foc = sf.plan(g, _tag)
        if due:
            foc["applied"] = sf.apply(g, foc, _tag)
        rep["focus"] = foc
    except Exception as e:
        rep["focus"] = {"ok": False, "error": repr(e)}
    # 2026-11-22 (P1-5): say out loud why nothing was written.
    rep["paused"] = bool(paused)
    if not live:
        rep["blocked_by"] = "read_only"
        rep["hint"] = ("report only; pass apply=true to let the staff queue "
                       "construction, regroup armies and set the focus")
    elif paused:
        rep["blocked_by"] = "paused"
        rep["hint"] = "the game is paused; apply runs on the first tick with the clock running"
    return rep


# ============================================================== turn engine (2.5)
COMPACT_TOOL_NAMES = (
    "hoi4_turn_brief",
    "hoi4_inspect",
    "hoi4_run_plan",
    "hoi4_wait_and_brief",
    "hoi4_status",
)
# 2026-11-22 (P2-3): 仅在 Linux 实现了原生后端的工具，Windows 下默认从 tools/list 隐藏
# （若强制 tools/call 仍由底层返回 ok:false + blocked_by:"unsupported_platform"）。
LINUX_ONLY_TOOL_NAMES = ("hoi4_send_diplomatic_action",)
_MCP_PROFILE = os.environ.get("HOI4_MCP_PROFILE", "full").strip().lower()
_COMPACT_STRICT = os.environ.get("HOI4_MCP_COMPACT_STRICT", "1").strip().lower() not in (
    "0", "false", "no", "off"
)
_SHOW_UNSUPPORTED = os.environ.get("HOI4_MCP_SHOW_UNSUPPORTED", "0").strip().lower() in (
    "1", "true", "yes", "on"
)
# 安全默认：hoi4_debug_*（控制台/任意函数调用/内存读）默认关闭，防止 Full 模式下被误用或提示词注入；
# hoi4_debug_eval（Python exec/eval）需同时显式开启 HOI4_MCP_ALLOW_DEBUG=1 与 HOI4_MCP_ALLOW_EVAL=1。
_ALLOW_DEBUG = os.environ.get("HOI4_MCP_ALLOW_DEBUG", "0").strip().lower() in (
    "1", "true", "yes", "on"
)
_ALLOW_EVAL = os.environ.get("HOI4_MCP_ALLOW_EVAL", "0").strip().lower() in (
    "1", "true", "yes", "on"
)
_TOOL_REQUIRED_CACHE = None


def _tool_required_keys(name):
    global _TOOL_REQUIRED_CACHE
    if _TOOL_REQUIRED_CACHE is None or len(_TOOL_REQUIRED_CACHE) != len(TOOLS):
        _TOOL_REQUIRED_CACHE = {
            t["name"]: tuple((t.get("inputSchema") or {}).get("required") or ())
            for t in TOOLS
        }
    return _TOOL_REQUIRED_CACHE.get(name, ())


def _active_tools():
    """按运行档位、平台与安全开关返回 tools/list：compact 模式仅暴露 5 个高层聚合工具（~1.5k tokens）。"""
    if _MCP_PROFILE == "compact":
        want = set(COMPACT_TOOL_NAMES)
        return [t for t in TOOLS if t.get("name") in want]
    hide = set()
    if sys.platform == "win32" and not _SHOW_UNSUPPORTED:
        hide.update(LINUX_ONLY_TOOL_NAMES)
    out = []
    for t in TOOLS:
        nm = t.get("name") or ""
        if nm in hide:
            continue
        if nm == "hoi4_debug_eval" and not (_ALLOW_DEBUG and _ALLOW_EVAL):
            continue
        if nm.startswith("hoi4_debug_") and not _ALLOW_DEBUG:
            continue
        out.append(t)
    return out


@tool(
    "hoi4_turn_brief",
    "Generate a compact Markdown turn briefing (pure read, ~2 KB / ~700 tokens). "
    "Includes date/window/alerts, economy, focus/research/production/construction, "
    "pending events (conditional), selectable focuses when idle, active combats at war, "
    "and session-scoped memo/last_receipt. Pass include_data=true only if raw JSON dict is needed.",
    dict(
        TAG,
        memo=_STR,
        include_focus_candidates=_BOOL,
        include_research_candidates=_BOOL,
        include_data=_BOOL,
        reset_state=_BOOL,
    ),
    [],
)
def _turn_brief(a):
    import turnengine as TE
    return TE.build_turn_brief(
        game(),
        tag=a.get("tag"),
        memo=a.get("memo"),
        include_focus_candidates=a.get("include_focus_candidates", True),
        include_research_candidates=a.get("include_research_candidates", True),
        include_data=bool(a.get("include_data", False)),
        reset_state=bool(a.get("reset_state", False)),
    )


@tool(
    "hoi4_inspect",
    "Bounded read-only escape hatch (max 3 queries per call) for deep inspection "
    "when hoi4_turn_brief lacks detail. Allowed topics (17): country, events, "
    "selectable_focuses, research_available, advisors, decisions, armies, war_readiness, "
    "combat, stockpile, resources, state, peace, air, navy, staff, health.",
    dict(
        TAG,
        queries={
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "topic": {"type": "string"},
                    "tag": {"type": "string"},
                    "target": {"type": "string"},
                    "filter": {"type": "string"},
                    "state": {"type": "integer"},
                    "state_id": {"type": "integer"},
                    "which": {"type": "string"},
                    "mine_only": {"type": "boolean"},
                    "lang": {"type": "string"},
                    "sweep": {"type": "boolean"},
                    "compact": {"type": "boolean"},
                    "full": {"type": "boolean"},
                    "all": {"type": "boolean"},
                },
                "required": ["topic"],
            },
        },
    ),
    ["queries"],
)
def _turn_inspect(a):
    import turnengine as TE
    return TE.inspect_topics(game(), a.get("queries") or [], tag=a.get("tag"))


@tool(
    "hoi4_run_plan",
    "Validate and serially execute a structured Turn Plan DSL (up to 15 high-level "
    "actions) with strict arg validation (dry_run validates identically), foreground-window "
    "guard, version guard, post-action liveness check, empty-order guard before resume, "
    "persisted memo, and compact receipt. Arg names align with public tools (state, line, "
    "template, technology, target, slot) while keeping legacy aliases.",
    dict(
        TAG,
        memo=_STR,
        dry_run=_BOOL,
        actions={
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "op": {"type": "string"},
                    "args": {"type": "object"},
                    "on_error": {"type": "string", "enum": ["continue", "abort"]},
                },
                "required": ["op"],
            },
        },
    ),
    ["actions"],
)
def _turn_execute_plan(a):
    import turnengine as TE
    plan = {"actions": a.get("actions") or []}
    if "memo" in a:
        plan["memo"] = a.get("memo")
    ok_v, errs_v = TE.validate_plan(plan)
    if not ok_v:
        return {"ok": False, "blocked_by": "invalid_plan", "errors": errs_v}
    return TE.execute_plan(game(), plan, dry_run=bool(a.get("dry_run")), tag=a.get("tag"))


@tool(
    "hoi4_wait_and_brief",
    "Sleep until the next game signal or max_days (auto-pauses on wake by default) "
    "and immediately return the new turn's compact briefing in one round-trip.",
    dict(
        TAG,
        max_days=_INT,
        timeout_seconds=_INT,
        pause_on_wake=_BOOL,
        include_data=_BOOL,
        signals={"type": "array", "items": {"type": "string"}},
        until_date={"type": "array", "items": {"type": "integer"}},
    ),
    [],
)
def _turn_wait_and_brief(a):
    import turnengine as TE
    return TE.wait_and_brief(
        game(),
        max_days=a.get("max_days", 14),
        until_date=a.get("until_date"),
        signals=a.get("signals"),
        timeout_seconds=a.get("timeout_seconds", 45),
        pause_on_wake=a.get("pause_on_wake", True),
        tag=a.get("tag"),
        include_data=bool(a.get("include_data", False)),
    )


def _ensure_utf8_stdio():
    """2026-11-22 (P1-10): 强制 stdin/stdout/stderr 使用 UTF-8，防止 Windows 管道默认 cp936(GBK) 乱码。"""
    for _stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(_stream, "reconfigure"):
            try:
                _stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


def main():
    global _MCP_PROFILE
    _ensure_utf8_stdio()
    for arg in sys.argv[1:]:
        if arg in ("--compact", "--profile=compact"):
            _MCP_PROFILE = "compact"
        elif arg.startswith("--profile="):
            _MCP_PROFILE = arg.split("=", 1)[1].strip().lower()
    _stderr("[profile] %s (%d tools exposed, strict=%s)" % (
        _MCP_PROFILE, len(_active_tools()), _COMPACT_STRICT if _MCP_PROFILE == "compact" else False
    ))
    _stderr("[watchdog] per-call budget %.0fs (P0-2)" % CALL_TIMEOUT_S)
    # 2026-11-21: a background prewarm of the date address was tried here and
    # REMOVED again -- it competed with the first real tool call for the same
    # game memory and pushed hoi4_overview past the 50 s budget.  The cold-start
    # cost is already gone: the repeated scanning was the bug (see the
    # `_date_scan_at` gate in api.py), so one scan now fits comfortably inside
    # the budget.
    # 2026-11-16: the tactical tick needs an executor INSIDE this process.  The
    # stack used to rely on hoi4_gateway.py's front_worker thread, which is not
    # part of the running Windows stack -- so front_directives.json was written
    # and never executed, and the army never moved.  `frontd.start()` spawns one
    # daemon thread; a lock file keeps the other MCP server processes on this
    # machine in standby.
    try:
        import frontd
        _st = frontd.start()
        _stderr("[frontd] %s" % (_st,))
    except Exception as _e:
        _stderr("[frontd] start failed: %r" % (_e,))
    # 2026-11-23: 启动纯只读后台红点急刹守护线程（仅在游戏已连接且运行态轻扫，
    # 命中新阻塞红点直写 1 字节 idler+0x6C1=1 停表，零引擎注入、不抢窗口焦点）。
    try:
        import reddots as _rd
        if _rd.CONFIG.get("guard_enabled", True):
            _rd.start_guard(lambda: _g, busy_check=lambda: bool(_CALL.get("name")))
            _stderr("[reddots] guard started (poll=%.1fs)" % _rd.CONFIG.get("guard_poll_seconds", 1.5))
    except Exception as _e:
        _stderr("[reddots] guard start failed: %r" % (_e,))
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        r = handle(msg)
        if r is not None:
            sys.stdout.write(json.dumps(r, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
