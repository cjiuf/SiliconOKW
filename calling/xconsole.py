#!/usr/bin/env python3
"""X11 console driver for HOI4 on Xvfb (2026-11-10).

Why this file exists
--------------------
Every earlier run of this project recorded "the keyboard never reaches the game":
`xdotool key` looked like a no-op, `pp 100` changed nothing, and the console was
declared unreachable. That conclusion was wrong.

  * XTEST was always working -- `xev` on the same display received every key with
    `synthetic NO`.
  * What was missing is a **window manager**. With no WM on the display the game
    window never received a FocusIn, so SDL never considered itself focused and
    threw every key away. A stale `_NET_SUPPORTING_WM_CHECK` property left behind
    by a killed WM made it look as if one were still running.
  * The console key is plain `grave` (keycode 49, unshifted). Sending
    `asciitilde` (shift+`) also toggles it, but it inserts a literal `~` into the
    input line, so the next command arrives as `~manpower 100000` -> "Unknown
    command". That single character cost an hour: the console text buffer showed
    `7e 6d 61 6e 70 6f 77 65 72` ("~manpower").

With `matchbox-window-manager` running and the window activated, `space` pauses
the game and `xdotool key asciitilde` opens the console. Measured live on
1.19.4.1, 2026-11-10: typing `manpower 100000` three times moved the manpower
pool by +300000 and the console echoed "Manpower changed by 100000".

The console panel is detected from a screenshot (near-black box top-left vs the
map: mean luminance 49 open / 105 closed on the measurement run), so a leftover
open console can never swallow the next command as map hotkeys.
"""
from __future__ import annotations
import os
import subprocess
import time

DISPLAY = os.environ.get("HOI4_DISPLAY") or ":1"
RUNUSER = ["runuser", "-u", "snow", "--", "env",
           "PATH=/usr/local/bin:/usr/bin:/bin",
           "DISPLAY=%s" % DISPLAY, "XDG_RUNTIME_DIR=/run/user/1001"]

CONSOLE_KEY = "grave"           # keycode 49, unshifted: toggles WITHOUT inserting a character
WINDOW_NAME = "Hearts of Iron IV"
PANEL_BOX = (30, 200, 380, 420)  # x0, y0, x1, y1 -- inside the console panel
PANEL_DARK = 45.0                # measured: open 20.4, closed 67.9 (night) / 105 (day)

_wm_pid = None


def _run(argv, timeout=20, capture=False, stdout=None):
    kw = dict(timeout=timeout, stderr=subprocess.DEVNULL, env=dict(os.environ, DISPLAY=DISPLAY))
    if capture:
        kw["stdout"] = subprocess.PIPE
        kw["text"] = True
    elif stdout is not None:
        kw["stdout"] = stdout
    else:
        kw["stdout"] = subprocess.DEVNULL
    return subprocess.run(argv, **kw)


def ensure_wm():
    """Start matchbox if no window manager runs. Without one the engine drops every key."""
    global _wm_pid
    try:
        r = _run(["pgrep", "-f", "matchbox-window-manager"], capture=True, timeout=5)
        if r.stdout.strip():
            return True
    except Exception:
        pass
    exe = "/usr/bin/matchbox-window-manager"
    if not os.path.exists(exe):
        return False
    try:
        _wm_pid = subprocess.Popen(
            RUNUSER + [exe, "-use_titlebar", "no"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env=dict(os.environ, DISPLAY=DISPLAY))
        time.sleep(1.5)
        return True
    except Exception:
        return False


def window_id():
    r = _run(RUNUSER + ["xdotool", "search", "--name", WINDOW_NAME], capture=True, timeout=10)
    ids = [x.strip() for x in (r.stdout or "").splitlines() if x.strip()]
    return ids[-1] if ids else ""


def focus(wid=None):
    wid = wid or window_id()
    if not wid:
        return ""
    _run(RUNUSER + ["xdotool", "windowactivate", "--sync", wid], timeout=15)
    _run(RUNUSER + ["xdotool", "windowfocus", "--sync", wid], timeout=15)
    time.sleep(0.25)
    return wid


def key(*names):
    _run(RUNUSER + ["xdotool", "key", "--clearmodifiers", *names], timeout=20)


def type_text(text, delay=90):
    _run(RUNUSER + ["xdotool", "type", "--delay", str(delay), "--clearmodifiers", text], timeout=90)


def screenshot(path="/tmp/_xconsole.png"):
    """Grab the root window and convert it to PNG. Returns the path (or '' on failure)."""
    xwd = path + ".xwd"
    with open(xwd, "wb") as f:
        _run(["xwd", "-root", "-silent"], timeout=15, stdout=f)
    try:
        import xwd2png
        xwd2png.convert(xwd, path)
        return path
    except Exception:
        return ""


def console_visible():
    """True when the console panel is on screen."""
    png = screenshot()
    if not png:
        return False
    try:
        from PIL import Image
        im = Image.open(png).convert("L")
        crop = im.crop(PANEL_BOX)
        data = list(crop.getdata())
        return (sum(data) / max(1, len(data))) < PANEL_DARK
    except Exception:
        return False


def run(command, settle=1.2, attempts=2):
    """Type one console command and execute it. Returns a dict describing what happened."""
    out = {"command": command, "wm": ensure_wm(), "window": "", "was_open": None,
           "ok": False, "opened": None, "closed": None, "note": ""}
    wid = focus()
    out["window"] = wid
    if not wid:
        out["note"] = "game window not found"
        return out

    # Peace-treaty / event popups steal the keyboard: HOI4 shows "Shortcut: ENTER"
    # under their OK button, so one Enter dismisses whatever is on top. Measured
    # 2026-11-10: with a TREATY OF XINING popup open the console opened but every
    # typed command was swallowed; after this Enter the same command applied.
    key("Return")
    time.sleep(0.8)

    was_open = console_visible()
    out["was_open"] = was_open
    if was_open:
        key(CONSOLE_KEY)
        time.sleep(0.8)
        if console_visible():                 # still open -> one more toggle
            key(CONSOLE_KEY)
            time.sleep(0.8)

    opened = False
    for _ in range(max(1, attempts)):
        key(CONSOLE_KEY)
        time.sleep(settle)
        if console_visible():
            opened = True
            break
        time.sleep(0.6)
    out["opened"] = opened
    if not opened:
        out["note"] = "console did not open (window not focused?)"
        return out

    type_text(command)
    time.sleep(0.35)
    key("Return")
    time.sleep(1.0)
    key(CONSOLE_KEY)                          # close
    time.sleep(0.4)
    out["closed"] = not console_visible()
    if not out["closed"]:
        key(CONSOLE_KEY)
        time.sleep(0.4)
        out["closed"] = not console_visible()
    out["ok"] = True
    return out


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "visible":
        ensure_wm()
        print("console_visible =", console_visible(), "window =", window_id())
    elif len(sys.argv) > 2 and sys.argv[1] == "run":
        print(run(" ".join(sys.argv[2:])))
    else:
        print(__doc__)
