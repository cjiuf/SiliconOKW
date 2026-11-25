"""受双环境变量门禁保护的调试/逆向工具与注入器脏状态清理工具。"""
from __future__ import annotations

import offsets as O
import io
import json

from .common import _BOOL, _INT, _STR, game, tool
from .common import _is_pid_alive, command_docs

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
