"""MCP 服务端运行时支撑：进程自愈、日期回跳检测、窗口截图、空单护栏与命令文档加载。"""
from __future__ import annotations

import io
import os
import re
import sys

import config as _config

GAME_DIR = _config.game_dir(required=False) or ""
DOC_MD = os.path.join(GAME_DIR, "documentation", "console_commands_documentation.md")


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

def _selfheal_check(g) -> None:
    """Detect game restart / save reload and rebuild volatile caches in-process.

    Triggers:
      * game pid changed (game process restarted);
      * process start time changed (same pid reused);
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
        old_pid = _selfheal_pid
        _stderr("[self-heal] game pid %s -> %s (start %s -> %s): rebuilding caches"
              % (old_pid, pid, _selfheal_start, st))
        _selfheal_pid, _selfheal_start = pid, st
        try:
            import session_state as _ss
            _ss.invalidate(reason=f"selfheal_pid:{old_pid}->{pid}", pid=pid)
        except Exception as _e:
            import diag as _diag
            _diag.warn("mcp.runtime.reattach.invalidate_session", _e)
        try:
            import win_pipeline as _wp
            _wp.clear_version_cache(g)
        except Exception as _e:
            import diag as _diag
            _diag.warn("mcp.runtime.reattach.clear_version_cache", _e)
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
        except Exception as _e:
            import diag as _diag
            _diag.warn("mcp.runtime.reattach.clear_vt_scan", _e)
        # 2026-11-17: _tech_by_name/_tech_by_id must be RESET, not deleted --
        # calling/game.py:_load_tech_db() reads them as plain attributes
        # (`if self._tech_by_name is not None`) and blew up with
        #   AttributeError: 'Game' object has no attribute '_tech_by_name'
        # on the first research call after every game restart.
        for attr in ("_tech_by_name", "_tech_by_id"):
            try:
                setattr(g, attr, None)
            except Exception as _e:
                import diag as _diag
                _diag.warn("mcp.runtime.reattach.reset_attr", _e, detail=attr)
        for attr in ("_eq_by_id", "_eq_by_name",
                     "_dec_defs", "_idea_defs", "_idea_cache", "_bld_by_key",
                     "_bld_by_id", "_doc_cache_key", "_pdx_newdel_alloc",
                     "_div_tagmap", "_win_date", "_win_date_pid", "_win_date_last"):
            if hasattr(g, attr):
                try:
                    delattr(g, attr)
                except Exception as _e:
                    import diag as _diag
                    _diag.warn("mcp.runtime.reattach.del_attr", _e, detail=attr)
        try:
            g.reset_front_snapshot()
        except Exception as _e:
            import diag as _diag
            _diag.warn("mcp.runtime.reattach.reset_front", _e)
        # 2026-11-19: start the CTechnologyTemplate RTTI scan NOW, in the
        # background, instead of waiting for the first research call to ask for
        # it.  The scan takes ~30-40 s on a fresh process; kicking it here means
        # hoi4_overview / hoi4_research_available usually find it warm, and the
        # first research tool no longer has to block for half a minute.
        try:
            import sys as _sys
            if _sys.platform == "win32":
                g._kick_tech_scan()
        except Exception as _e:
            import diag as _diag
            _diag.warn("mcp.runtime.reattach.kick_tech_scan", _e)

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
            import session_state as _ss
            _ss.invalidate(
                reason=f"date_backjump:{prev}->{raw}",
                pid=getattr(getattr(g, "p", None), "pid", None),
            )
        except Exception as _e:
            import diag as _diag
            _diag.warn("mcp.runtime.backjump.invalidate_session", _e)
        try:
            import win_pipeline as _wp
            _wp.clear_version_cache(g)
        except Exception as _e:
            import diag as _diag
            _diag.warn("mcp.runtime.backjump.clear_version_cache", _e)
        try:
            type(g)._VT_SCAN.clear()
        except Exception as _e:
            import diag as _diag
            _diag.warn("mcp.runtime.backjump.clear_vt_scan", _e)
        if hasattr(g, "_oi"):
            g._oi = None
        for attr in ("_div_tagmap", "_win_date", "_win_date_pid", "_win_date_last",
                     "_doctrine_done", "_hk_state_key"):
            if hasattr(g, attr):
                try:
                    delattr(g, attr)
                except Exception as _e:
                    import diag as _diag
                    _diag.warn("mcp.runtime.backjump.del_attr", _e, detail=attr)
        try:
            g.reset_front_snapshot()
        except Exception as _e:
            import diag as _diag
            _diag.warn("mcp.runtime.backjump.reset_front", _e)
        try:
            g.find_date_address(force=True)
        except Exception as e:
            _stderr("[self-heal] date rescan failed: %r" % e)
    if raw is not None:
        g._heal_last_raw = raw

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
    """截取 Windows 游戏窗口画面（原 Linux X11/xdotool/KWin 死路径已移除）。"""
    if sys.platform != "win32":
        raise NotImplementedError("Linux X11 screenshot path removed; Windows PE only.")
    return _win_screenshot(crop, focus)

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

