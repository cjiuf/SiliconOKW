"""The executor for `front_directives.json` -- the tactical tick, in-process.

Why this module exists
----------------------
`hoi4_set_front_directive` only writes `calling/front_directives.json`.  The
stack relied on a *separate* gateway process (`hoi4_gateway.py`, its
`front_worker` thread) to read that file every 8 s and call
`tactical_front_tick`.  On this Windows box the running stack is
`stage/wx_server.py -> mcp/server.py` and nothing else: there is no gateway, so
nothing ever executed the file.

Live proof, 2026-11-16: `calling/front_directives.json` held a HOL directive
armed at 1939.05.01 with `last_tick: null`, unchanged for four months of game
time, while 112 German divisions sat in Berlin and the Allies walked in.

So the executor now lives in the process that actually exists:

  * one daemon thread, started from `server.main()`;
  * a heartbeat file (`front_worker.json`) so any tool can answer "is anything
    driving the army right now?";
  * an exclusive lock file (`front_worker.lock`) so only ONE of the several MCP
    server processes on this machine ticks -- six were alive at once on
    2026-11-16, all importing the same modules;
  * the worker never injects into a paused game.  That combination
    ("wartime + front_worker every 8 s + injection") is what froze the game in
    the 2026-11-08 runs, and a paused game gains nothing from a tick.

The worker owns its OWN `Game` instance.  Two threads sharing one HOI4 object
would share `_depth` / `_inj`, and the second `attach()` would return without
taking the lock while the first one is mid-way through rewriting registers.
"""
import json
import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
BEAT_PATH = os.path.join(HERE, "front_worker.json")
LOCK_PATH = os.path.join(HERE, "front_worker.lock")
DIRECTIVES_PATH = os.path.join(HERE, "front_directives.json")

POLL = 5.0              # wall clock between heartbeat polls
FIRST_DELAY = 4.0       # let the MCP server finish starting up
STALE_AFTER = 30.0      # heartbeat older than this = worker is gone
MIN_SPACING = 12.0      # never run two tactical ticks closer than this (wall clock)

_started = False
_me = {"pid": None, "owner": False, "ticks": 0, "errors": 0,
       "last_error": None, "last_ts": 0.0, "last_date": None,
       "last_result": None, "state": "init"}
_game = [None]
_game_pid = [None]
_tick_thread = [None]
_tick_started = [0.0]


def _log(*a):
    print("[frontd]", *a, file=sys.stderr, flush=True)


_BEAT_LOCK = threading.Lock()


def _beat(**kw):
    with _BEAT_LOCK:
        _me.update(kw)
        _me["ts"] = time.time()
        snap = dict(_me)
        tmp = "%s.%d.%d.tmp" % (BEAT_PATH, os.getpid(), threading.get_ident())
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(snap, fh, ensure_ascii=False, indent=1)
            for _attempt in range(3):
                try:
                    os.replace(tmp, BEAT_PATH)
                    break
                except PermissionError:
                    time.sleep(0.05)
        except Exception:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except Exception:
                pass


def status():
    """What the worker is doing, without touching the game."""
    try:
        with open(BEAT_PATH, encoding="utf-8") as fh:
            beat = json.load(fh)
    except Exception:
        beat = {}
    now = time.time()
    age = (now - float(beat.get("ts") or 0)) if beat else None
    return {
        "alive": bool(age is not None and age < STALE_AFTER),
        "pid": beat.get("pid"),
        "host": beat.get("host"),
        "owner": bool(beat.get("owner")),
        "state": beat.get("state"),
        "heartbeat_age_s": round(age, 1) if age is not None else None,
        "ticks": beat.get("ticks"),
        "errors": beat.get("errors"),
        "last_error": beat.get("last_error"),
        "last_date": beat.get("last_date"),
        "beat_path": BEAT_PATH,
    }


def clear_dirty(pid=None):
    """清理本进程内 frontd 持有 Game 实例以及 winject._DIRTY_PIDS 的 dirty 标志（B-8）。"""
    cleared_pids = []
    worker_inj_cleared = False
    try:
        from process import winject as _wj
        cleared_pids = _wj.clear_dirty(pid)
    except Exception:
        cleared_pids = []
    wg = _game[0]
    if wg is not None:
        inj = getattr(wg, "_inj", None)
        if inj is not None and getattr(inj, "dirty", False):
            if pid is None or getattr(inj, "pid", None) == int(pid):
                inj.dirty = False
                worker_inj_cleared = True
        # 如果上一次错误是 injector dirty，把 worker 状态恢复为 ready
        if "injector is dirty" in str(_me.get("last_error") or ""):
            _beat(state="ready", last_error=None)
    return {
        "ok": True,
        "cleared_pids": cleared_pids,
        "worker_injector_cleared": worker_inj_cleared,
        "worker_game_pid": _game_pid[0],
    }


def directives():
    try:
        with open(DIRECTIVES_PATH, encoding="utf-8") as fh:
            st = json.load(fh)
        return st.get("directives") or {}
    except Exception:
        return {}


def _take_lock():
    fh = open(LOCK_PATH, "a+b")
    try:
        if os.name == "nt":
            import msvcrt
            fh.seek(0)
            if fh.seek(0, os.SEEK_END) == 0:
                fh.write(b"\0")
                fh.flush()
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fh
    except Exception:
        try:
            fh.close()
        except Exception:
            pass
        return None


def _live_hoi4_pid():
    """The pid hoi4.exe actually has right now (None when it cannot be read).

    2026-11-17: this is the bug that made the worker useless after a game restart.
    `Proc.pid` is NOT refreshed when the handle re-resolves `hoi4.exe` by name, so
    `pid == _game_pid[0]` stayed true across a restart and the worker kept a Game
    whose cached country pointer belonged to the dead process -- the tick then
    reported "could not determine the played country (tag index 0, name '---')"
    with divisions_seen 0, and the front stood still.
    """
    if os.name != "nt":
        return None
    try:
        import subprocess
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq hoi4.exe", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=8).stdout or ""
    except Exception:
        return None
    for line in out.splitlines():
        line = line.strip()
        if not line.lower().startswith('"hoi4.exe"'):
            continue
        parts = [p.strip('"') for p in line.split('","')]
        if len(parts) >= 2:
            try:
                return int(parts[1])
            except Exception:
                pass
    return None


def _worker_game():
    """A Game handle owned by this thread (never the server's shared one)."""
    from game import Game
    live = _live_hoi4_pid()
    if _game[0] is not None:
        try:
            same_pid = (live is None) or (live == _game_pid[0])
            if same_pid:
                _game[0].p.read(_game[0].p.base, 4)  # raises when the pid is dead
                # Detect same-PID save reload (date backjump > 30 days) and reset caches
                try:
                    _dr = _game[0].date_raw()
                    _prev_dr = getattr(_game[0], "_frontd_last_raw", None)
                    if _prev_dr is not None and _dr is not None and _dr < _prev_dr - 30 * 24:
                        _game[0].reset_caches()
                        _log("detected date backjump %s -> %s; reset frontd caches" % (_prev_dr, _dr))
                    if _dr is not None:
                        _game[0]._frontd_last_raw = _dr
                except Exception:
                    pass
                return _game[0]
        except Exception:
            pass
    _game[0] = None
    g = Game()
    _game[0] = g
    _game_pid[0] = g.p.pid
    _log("attached to pid %s" % g.p.pid)
    return g


def _paused(g):
    try:
        import session
        return bool(session.is_paused(g))
    except Exception:
        return False


def _run_tick(g):
    """The actual tactical tick, run in its own thread (see _step)."""
    try:
        res = g.tactical_front_tick(None, False, False)
    except Exception as e:
        _me["errors"] = int(_me.get("errors") or 0) + 1
        _beat(state="error", last_error="%s: %s" % (type(e).__name__, e))
        return
    ticks = (res or {}).get("ticks") or []
    real = [t for t in ticks if not t.get("skipped")]
    _me["ticks"] = int(_me.get("ticks") or 0) + len(real)
    if res and res.get("ok") is False:
        errs = [str(t.get("error")) for t in ticks if t.get("error")]
        if errs:
            _me["errors"] = int(_me.get("errors") or 0) + 1
            _beat(state="tick-failed", last_error="; ".join(errs)[:300],
                  last_date=(res or {}).get("date"))
            return
    if real:
        _log("tick: %s" % json.dumps(
            [{k: t.get(k) for k in ("enemy", "plugged_gaps", "accepted_total",
                                    "refused_total", "pinned", "aborted", "ok")}
             for t in real], ensure_ascii=False)[:400])
    _beat(state="running" if real else "waiting",
          last_date=(res or {}).get("date"),
          last_result=real[-1] if real else None,
          tick_seconds=round(time.time() - (_tick_started[0] or time.time()), 1))


def _step():
    dirs = directives()
    if not dirs:
        _beat(state="idle", last_error=None)
        return
    # A tick can take tens of seconds (the first one of a process builds the map
    # and template caches).  It runs in its own thread so the heartbeat keeps
    # beating: a silent worker and a working-but-busy worker must not look the
    # same, that is exactly how the 1939 campaign went unnoticed.
    if _tick_thread[0] is not None and _tick_thread[0].is_alive():
        _beat(state="ticking",
              tick_seconds=round(time.time() - (_tick_started[0] or time.time()), 1))
        return
    # The game's own 1-day gate lives inside tactical_front_tick, but the clock
    # read on this build oscillates between two CDate copies: when it jumps
    # backwards the tick is forced.  Cap the rate here so a confused clock cannot
    # turn into an injection every 5 seconds during a war.
    newest = 0.0
    for d in dirs.values():
        lt = d.get("last_tick") or {}
        try:
            newest = max(newest, float(lt.get("wall_ts") or 0))
        except Exception:
            pass
    if newest and (time.time() - newest) < MIN_SPACING:
        _beat(state="waiting", last_error=None)
        return
    try:
        g = _worker_game()
    except Exception as e:
        _beat(state="no-game", last_error="%s: %s" % (type(e).__name__, e))
        return
    if _paused(g):
        _beat(state="paused")
        return
    _tick_started[0] = time.time()
    t = threading.Thread(target=_run_tick, args=(g,), name="frontd-tick", daemon=True)
    _tick_thread[0] = t
    _beat(state="ticking", tick_seconds=0.0)
    t.start()


def _loop():
    time.sleep(FIRST_DELAY)
    lock = _take_lock()
    if lock is None:
        # Never overwrite the live owner's front_worker.json from a standby process.
        _log("another frontd owner is alive; this process stays standby")
        return
    # 2026-11-18：`pid` 是**宿主 MCP 服务端进程**的 pid，不是一个独立 worker——
    # frontd 只是那个进程里的一个 daemon 线程（main() 里 frontd.start()）。
    # 曾经把这个 pid 当"残留 worker"Stop-Process，结果把整个 MCP 服务端杀了，
    # 之后每个调用都是 "Not connected"。清理前线指令请改 front_directives.json，
    # 不要杀这个 pid。
    _beat(owner=True, pid=os.getpid(), host="mcp-server-thread", state="started")
    _log("owner of %s (pid %s)" % (LOCK_PATH, os.getpid()))
    while True:
        try:
            _step()
        except Exception as e:                       # never let the thread die
            _me["errors"] = int(_me.get("errors") or 0) + 1
            try:
                _beat(state="loop-error", last_error="%s: %s" % (type(e).__name__, e))
            except Exception:
                pass
        time.sleep(POLL)


def start():
    """Start the single owner thread (idempotent)."""
    global _started
    if _started:
        return status()
    _started = True
    t = threading.Thread(target=_loop, name="frontd", daemon=True)
    t.start()
    return {"started": True, "thread": t.name}


def kick(g, force=True):
    """Tick once right now, from the calling thread, and report what happened.

    `hoi4_set_front_directive` uses this so "armed" means "a tick really ran",
    not "a JSON file was written".  The caller's own `Game` is used on purpose:
    it is already attached (or cheap to attach) and this is an explicit user
    action, i.e. the same thing `hoi4_tactical_front_tick(force=true)` does.
    """
    try:
        res = g.tactical_front_tick(None, force, False)
    except Exception as e:
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}
    ticks = (res or {}).get("ticks") or []
    real = [t for t in ticks if not t.get("skipped")]
    return {"ok": True, "date": (res or {}).get("date"),
            "ticks": len(real), "did_orders": bool(res.get("ok")),
            "detail": real, "skipped": len(ticks) - len(real)}
