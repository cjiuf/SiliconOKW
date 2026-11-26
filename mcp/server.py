#!/usr/bin/env python3
"""Clausewitz-MCP server: JSON-RPC 2.0 over stdio 协议引擎与安全/看门狗调度内核。

具体 181 个 `@tool` 工具实现已按领域拆入 `mcp/tools/` 子包；本文件专注于：
  * JSON-RPC 2.0 stdio 传输与请求分发 (`handle` / `main`)
  * 进程连接与单活自愈入口 (`game` / `verified`)
  * 单次调用超时看门狗与零注入紧急停表 (`_run_with_timeout` / `_emergency_pause_game`)
  * Compact/Full 档位路由与 `HOI4_MCP_ALLOW_DEBUG` / `HOI4_MCP_ALLOW_EVAL` 双重安全门禁
"""
from __future__ import annotations

import json
import os
import sys
import traceback

_MCP_DIR = os.path.dirname(os.path.abspath(__file__))
_CALLING_DIR = os.path.join(_MCP_DIR, "..", "calling")
if _MCP_DIR not in sys.path:
    sys.path.insert(0, _MCP_DIR)
if _CALLING_DIR not in sys.path:
    sys.path.insert(0, _CALLING_DIR)

# 无论通过 `python mcp/server.py` (__main__) 还是 `import server` 加载，均确保全局唯一模块实例
sys.modules.setdefault("server", sys.modules[__name__])

from game import Game  # noqa: E402
from tools.common import (  # noqa: E402
    CALL_TIMEOUT_S,
    MAX_PAUSE_HOURS,
    NO_ARGS,
    OG,
    PAUSE_COOLDOWN_S,
    TAG,
    TAG_DEFAULT,
    _BOOL,
    _CALL,
    _INT,
    _PAUSE_LAST,
    _STR,
    _ptr,
    error_response,
    ok_response,
)
from tools.runtime import (  # noqa: E402
    DOC_MD,
    GAME_DIR,
    _date_backjump,
    _is_pid_alive,
    _looks_like_dead_read,
    _proc_start_ticks,
    _refuse_unpause_with_empty_orders,
    _selfheal_check,
    _stderr,
    _win_hwnd,
    _win_screenshot,
    command_docs,
    screenshot,
)

PROTOCOL = "2024-11-05"
_g: Game | None = None
_WINJECT_RECOVERY = None

TOOLS, HANDLERS = [], {}


def tool(name, description, props=None, required=None):
    """注册 MCP 工具并将 handler 同名绑定到 server 模块，向后兼容既有测试夹具。"""
    schema = {"type": "object", "properties": props or {}}
    if required:
        schema["required"] = required

    def deco(fn):
        TOOLS.append({"name": name, "description": description, "inputSchema": schema})
        HANDLERS[name] = fn
        globals().setdefault(fn.__name__, fn)
        return fn
    return deco


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
            from process import winject
            _WINJECT_RECOVERY = winject.resume_orphans(_g.p.pid)
            if _WINJECT_RECOVERY.get("found"):
                _stderr("[winject] orphan record: %r" % (_WINJECT_RECOVERY,))
        except Exception as e:
            _WINJECT_RECOVERY = {"error": repr(e)}
            _stderr("[winject] WARNING: resume_orphans failed: %r" % (e,))
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
# CALL_TIMEOUT_S 与 _CALL 由 tools.common 统一导出并共享同一原位字典引用


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
    if prev is not None and not prev.is_alive():
        _CALL.clear()
        prev = None
    if prev is not None and prev.is_alive():
        _el = _t.time() - _CALL.get("started", _t.time())
        # 2026-11-21 (续测 P1-1): a timed-out call keeps running (a Python thread
        # cannot be cancelled safely -- it is inside ctypes/numpy memory reads and
        # killing it would leave the injector frozen mid-hijack).  So the honest
        # answer is: say exactly which call is holding the game, how long it has
        # been running, and that the only current way out is to let it finish or
        # restart the server.  Long calls SHOULD be bounded at the source (see the
        # probe budget in selectable_focuses and the air/enqueue paths).
        _hint = ("Wait for it, or restart the HOI4 MCP server process -- Python "
                 "cannot cancel an in-flight memory read thread.")
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
    th_ref = []

    def _work():
        try:
            box["out"] = fn(args)
        except BaseException as e:            # noqa: BLE001 - reported to the caller
            box["err"] = e
        finally:
            cur = th_ref[0] if th_ref else threading.current_thread()
            if _CALL.get("thread") is cur:
                _CALL.clear()

    th = threading.Thread(target=_work, daemon=True, name="call:%s" % name)
    th_ref.append(th)
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
            if _CALL.get("thread") is th:
                _CALL.clear()
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
    if _CALL.get("thread") is th:
        _CALL.clear()
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


# 注册全部 10 个领域的 181 个 MCP 工具，并同步暴露少量历史测试直接访问的运行期常量
from tools import register_all as _register_all_tools  # noqa: E402
_register_all_tools()
from tools.air_navy import (  # noqa: E402
    _AIR_DONE_PATH,
    _air_done_load,
    _air_done_save,
    _air_session_fp,
    _live_army_wing_map,
)
from tools.diplomacy import _country_index  # noqa: E402
from tools.macros import _STAFF_APPLIED  # noqa: E402

_COMPAT_EXPORTS = (
    DOC_MD, GAME_DIR, MAX_PAUSE_HOURS, NO_ARGS, OG, PAUSE_COOLDOWN_S,
    TAG, TAG_DEFAULT, _BOOL, _INT, _PAUSE_LAST, _STR, _ptr,
    error_response, ok_response, _is_pid_alive, _looks_like_dead_read,
    _proc_start_ticks, _refuse_unpause_with_empty_orders, _win_hwnd,
    _win_screenshot, command_docs, screenshot, _AIR_DONE_PATH,
    _air_done_load, _air_done_save, _air_session_fp, _live_army_wing_map,
    _country_index, _STAFF_APPLIED,
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
    # 2026-11-16: the tactical tick needs an executor INSIDE this process, so
    # `frontd.start()` spawns one daemon thread; a lock file keeps any other MCP
    # server processes on this machine in standby.
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
