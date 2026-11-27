"""Window state of the game process (PE build).

2026-11-19: a minimised HOI4 freezes the simulation completely -- measured
5 hours wall clock with the window minimised produced **zero** game days, and
100 s after restoring it produced 47 game days.  Several "nothing happened"
observations in this project were that, not the engine.

So every long-running clock call now reports where the window is, and
``pause_in_hours`` turns a stalled clock into ``blocked_by: "window_minimized"``
instead of a mysterious ``stalled: true``.
"""
import ctypes
import sys
from ctypes import wintypes

_IS_WIN = sys.platform == "win32"

if _IS_WIN:
    _u = ctypes.WinDLL("user32", use_last_error=True)
    _u.GetForegroundWindow.restype = wintypes.HWND
    _u.IsIconic.argtypes = [wintypes.HWND]
    _u.IsIconic.restype = wintypes.BOOL
    _u.IsWindowVisible.argtypes = [wintypes.HWND]
    _u.IsWindowVisible.restype = wintypes.BOOL
    _u.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    _u.GetWindowTextW.restype = ctypes.c_int
    _u.GetWindowThreadProcessId.argtypes = [wintypes.HWND,
                                            ctypes.POINTER(wintypes.DWORD)]
    _u.GetWindowThreadProcessId.restype = wintypes.DWORD
    _u.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    _u.GetWindowRect.restype = wintypes.BOOL
    _ENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    _u.EnumWindows.argtypes = [_ENUMPROC, wintypes.LPARAM]
    _u.EnumWindows.restype = wintypes.BOOL


def _title(hwnd):
    buf = ctypes.create_unicode_buffer(512)
    _u.GetWindowTextW(hwnd, buf, 512)
    return buf.value


def _rect(hwnd):
    rc = wintypes.RECT()
    if not _u.GetWindowRect(hwnd, ctypes.byref(rc)):
        return None
    return (int(rc.left), int(rc.top), int(rc.right), int(rc.bottom))


def _windows_of(pid):
    out = []

    def cb(hwnd, _lparam):
        p = wintypes.DWORD()
        _u.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
        if p.value == pid:
            out.append(hwnd)
        return True

    _u.EnumWindows(_ENUMPROC(cb), 0)
    return out


def _pick_main_window(hwnds):
    """优先挑选可见、非最小化且具备真实渲染尺寸的 HOI4 主窗口（规避隐藏/0x0 辅助窗误判 D-05）。"""
    cands = []
    for h in hwnds:
        t = _title(h)
        if not t:
            continue
        is_hoi = "hearts of iron" in t.lower()
        vis = bool(_u.IsWindowVisible(h))
        iconic = bool(_u.IsIconic(h))
        rc = _rect(h)
        w = max(0, rc[2] - rc[0]) if rc else 0
        ht = max(0, rc[3] - rc[1]) if rc else 0
        real_area = (w >= 200 and ht >= 200)
        # 排序权值：HOI4 标题 > 可见 > 真实尺寸 > 非最小化 > 面积 > 标题长度
        score = (
            int(is_hoi),
            int(vis),
            int(real_area),
            int(not iconic),
            w * ht,
            len(t),
        )
        cands.append((score, h, t, vis, iconic, rc))
    if not cands:
        return None
    cands.sort(key=lambda x: x[0], reverse=True)
    return cands[0], len(cands)


def state(pid=None):
    """{pid, title, hwnd, foreground, minimized, visible, focused}."""
    if not _IS_WIN:
        return {"pid": pid, "platform": sys.platform, "foreground": None,
                "minimized": None, "reason": "not windows"}
    hwnds = _windows_of(int(pid)) if pid else []
    picked = _pick_main_window(hwnds)
    if picked is None:
        return {"pid": pid, "hwnd": None, "title": None, "foreground": None,
                "minimized": None, "visible": None, "focused": None,
                "reason": "no titled window for this pid"}
    (_score, best, best_title, vis, iconic, rc), cand_n = picked
    fg = _u.GetForegroundWindow()
    fg_int = int(fg or 0)
    out = {
        "pid": int(pid),
        "hwnd": hex(int(best)),
        "title": best_title,
        "foreground": hex(fg_int),
        "focused": fg_int == int(best),
        "minimized": iconic,
        "visible": vis,
        "window_candidates": cand_n,
    }
    if rc is not None:
        out["rect"] = list(rc)
    if iconic:
        out["hint"] = (
            "HOI4 window is minimized (exclusive fullscreen auto-minimizes on focus loss; "
            "switch HOI4 Display Mode to Borderless Fullscreen to prevent repeated minimization)."
        )
    return out


def blocking(pid=None):
    """None when the window can drive the simulation, else a short reason."""
    s = state(pid)
    if s.get("minimized"):
        return "window_minimized"
    if s.get("visible") is False:
        return "window_hidden"
    return None
