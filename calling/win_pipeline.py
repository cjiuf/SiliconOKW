# -*- coding: utf-8 -*-
"""Windows (PE) 统一发令管道与精确版本门禁。

目标：
1. 将分散在 `cmdwin` / `trade_win` / `events_win` / `armyops_win` / `api.HOI4.attach`
   中的发令前置护栏收口到单一真源：
   - 精确版本指纹校验（fail-closed）：通过 `profile.verify_live_process(proc)` 校验
     运行中 `hoi4.exe` 的 PE `TimeDateStamp` + `SizeOfImage`，非 `exact` 时拒绝一切
     线程注入与进程内存写入，而只读查询保持可用。
   - 统一 `attach_fake_vtable`（slot 0 -> `FN_NOOP_DTOR`，防止引擎丢弃命令时走 CRT
     deleting destructor 导致进程崩溃）。
   - 统一 `CanExecute` 预检、`pre_post_hook` / `post_cleanup_hook`（如事件作用域借用
     与 `finally` 归还）以及 `post_drained` / `enqueue` 生命周期。
2. 保持 100% 向后兼容：所有底层操作仍委托 `cmdwin` 导出函数，不破坏现有单测对
   `cmdwin.post_drained` / `cmdwin.enqueue` 的 monkeypatch 夹具。
"""
from __future__ import annotations

from dataclasses import dataclass, field
import os
import sys
from typing import Any, Callable, Dict, Optional

_ALLOW_OVERRIDE_ENV = "HOI4_ALLOW_UNVERIFIED_VERSION"
_OVERRIDE_WARNED = set()


class VersionMismatchError(RuntimeError):
    """运行中 hoi4.exe PE 指纹与已加载 profile 不一致时抛出的安全拦截异常。"""

    def __init__(self, message: str, verify_result: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.verify_result = dict(verify_result or {})


@dataclass
class CommandResult:
    """Windows 统一命令执行结果结构。"""

    ok: bool
    cmd: int = 0
    can_execute: Optional[bool] = None
    posted: int = 0
    drained: Optional[bool] = None
    blocked_by: str = ""
    error: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "ok": bool(self.ok),
            "cmd": self.cmd,
            "posted": self.posted,
        }
        if self.can_execute is not None:
            out["can_execute"] = self.can_execute
        if self.drained is not None:
            out["drained"] = self.drained
        if self.blocked_by:
            out["blocked_by"] = self.blocked_by
        if self.error:
            out["error"] = self.error
        if self.extra:
            out.update(self.extra)
        return out


def _env_override_enabled() -> bool:
    val = os.environ.get(_ALLOW_OVERRIDE_ENV, "").strip().lower()
    return val in ("1", "true", "yes", "on")


def _extract_proc(game_or_proc: Any) -> Any:
    if hasattr(game_or_proc, "p") and getattr(game_or_proc, "p", None) is not None:
        return game_or_proc.p
    return game_or_proc


def is_live_proc(proc: Any) -> bool:
    """判断对象是否为真实活体进程句柄（区别于离线单测里的轻量 FakeProc）。"""
    if proc is None:
        return False
    if getattr(proc, "_enforce_version_gate", False):
        return True
    if getattr(proc, "be", None) is not None:
        return True
    try:
        import mem as _mem
        if isinstance(proc, _mem.Proc):
            return True
    except Exception:
        pass
    return False


def clear_version_cache(game_or_proc: Any = None) -> None:
    """清除指定对象上的版本核验缓存（用于重连或单元测试重置）。"""
    if game_or_proc is None:
        _OVERRIDE_WARNED.clear()
        return
    for target in (game_or_proc, _extract_proc(game_or_proc)):
        if target is None:
            continue
        try:
            if hasattr(target, "_version_check_cache"):
                delattr(target, "_version_check_cache")
        except Exception:
            pass


def _format_mismatch_message(res: Dict[str, Any]) -> str:
    status = res.get("status", "unknown")
    live = res.get("live") or {}
    prof = res.get("profile") or {}
    parts = [
        "PE 版本安全门禁拦截写入：verify_live_process 返回 status=%r（要求 'exact'）" % status
    ]
    if live:
        parts.append("live=%s" % live)
    if prof:
        parts.append("profile=%s" % prof)
    if res.get("error"):
        parts.append("detail=%s" % res["error"])
    parts.append(
        "只读工具仍可继续使用；请先运行 hoi4_recalc 为当前 hoi4.exe 生成匹配 profile，"
        "或仅在明确知情且自担风险时设置环境变量 %s=1 覆盖。" % _ALLOW_OVERRIDE_ENV
    )
    return " | ".join(parts)


def ensure_live_version(
    game_or_proc: Any,
    *,
    force: bool = False,
    allow_override: bool = True,
    only_if_live: bool = False,
    check_pe: Optional[bool] = None,
) -> Dict[str, Any]:
    """校验活体 `hoi4.exe` PE 指纹是否与当前加载 Profile 精确匹配（fail-closed）。

    参数：
        game_or_proc: `HOI4`/`Game` 实例或 `Proc` 实例。
        force: 调用方显式要求跳过拦截（仅用于受控诊断路径）。
        allow_override: 是否允许通过环境变量 `HOI4_ALLOW_UNVERIFIED_VERSION=1` 绕过。
        only_if_live: 为 True 时仅对真实进程句柄（`mem.Proc` / `be is not None` /
            `_enforce_version_gate=True`）强制执行，放行离线单测中的纯内存 Fake 对象。
        check_pe: 是否执行 Windows PE 校验；为 None 时默认 `sys.platform == "win32"`。
    """
    if check_pe is None:
        check_pe = (sys.platform == "win32")
    if not check_pe:
        return {"ok": True, "status": "non_win32_skipped"}

    proc = _extract_proc(game_or_proc)
    if only_if_live and not is_live_proc(proc):
        return {"ok": True, "status": "offline_mock_skipped"}

    if force:
        return {"ok": True, "status": "forced", "override": True}

    if allow_override and _env_override_enabled():
        warn_key = (getattr(proc, "pid", None), getattr(proc, "base", None))
        if warn_key not in _OVERRIDE_WARNED:
            _OVERRIDE_WARNED.add(warn_key)
            try:
                sys.stderr.write(
                    "[win_pipeline] WARNING: %s=1 已启用，跳过 PE 精确版本门禁 (pid=%r)\n"
                    % (_ALLOW_OVERRIDE_ENV, warn_key[0])
                )
            except Exception:
                pass
        return {"ok": True, "status": "env_override", "override": True}

    if proc is None:
        res = {"ok": False, "status": "no_proc", "error": "missing proc handle"}
        raise VersionMismatchError(_format_mismatch_message(res), verify_result=res)

    pid = getattr(proc, "pid", None)
    base = getattr(proc, "base", None)
    cache_key = (pid, base) if (isinstance(pid, int) and pid > 0 and isinstance(base, int) and base > 0) else None
    cache = getattr(proc, "_version_check_cache", None)
    if cache_key is not None and isinstance(cache, dict) and cache_key in cache:
        return cache[cache_key]

    import profile as _prof
    res = _prof.verify_live_process(proc)
    if res.get("ok") and res.get("status") == "exact":
        if cache_key is not None:
            try:
                proc._version_check_cache = {cache_key: res}
            except Exception:
                pass
        return res

    raise VersionMismatchError(_format_mismatch_message(res), verify_result=res)


class WinCommandPipeline:
    """Windows 命令统一构造、校验与投递管道。"""

    def __init__(
        self,
        game: Any,
        *,
        require_exact_version: bool = True,
        only_if_live: bool = False,
        check_pe: Optional[bool] = None,
    ):
        self.game = game
        self.require_exact_version = bool(require_exact_version)
        self.only_if_live = bool(only_if_live)
        self.check_pe = check_pe

    def verify(self, *, force: bool = False) -> Dict[str, Any]:
        if not self.require_exact_version:
            return {"ok": True, "status": "gate_disabled"}
        return ensure_live_version(
            self.game,
            force=force,
            only_if_live=self.only_if_live,
            check_pe=self.check_pe,
        )

    def attach_fake_vtable(self, cmd: int, vt_rva: int, size: int = 0xB0) -> int:
        """为命令对象挂接 slot 0 = NOOP_DTOR 的私有虚表，防止丢弃时 CRT 崩溃。"""
        import cmdwin
        fake = cmdwin.fake_vtable(self.game, int(vt_rva), size=int(size))
        import struct
        self.game.p.write(int(cmd), struct.pack("<Q", fake))
        return fake

    def can_exec(self, cmd: int, can_exec_rva: int) -> bool:
        import cmdwin
        return bool(cmdwin.can_exec(self.game, int(cmd), int(can_exec_rva)))

    def enqueue(self, cmd: int, *, tick: Optional[int] = None, seq: Optional[int] = None) -> int:
        self.verify()
        import cmdwin
        return cmdwin.enqueue(self.game, int(cmd), tick=tick, seq=seq)

    def post_drained(
        self,
        cmd: int,
        *,
        seconds: float = 2.0,
        done_fn: Optional[Callable[[], bool]] = None,
    ) -> Dict[str, Any]:
        self.verify()
        import cmdwin
        if done_fn is not None:
            return cmdwin.post_drained(self.game, int(cmd), seconds=seconds, done_fn=done_fn)
        return cmdwin.post_drained(self.game, int(cmd), seconds=seconds)

    def run(
        self,
        *,
        build_fn: Callable[[Any], int],
        vt_rva: Optional[int] = None,
        fake_vt_size: int = 0xB0,
        can_exec_rva: Optional[int] = None,
        can_exec_fn: Optional[Callable[[Any, int], bool]] = None,
        pre_post_hook: Optional[Callable[[Any, int], Any]] = None,
        post_cleanup_hook: Optional[Callable[[Any, int, Any], None]] = None,
        drain: bool = True,
        seconds: float = 2.0,
        done_fn: Optional[Callable[[], bool]] = None,
        dry_run: bool = False,
        readback_fn: Optional[Callable[[Any, int, Any], Dict[str, Any]]] = None,
    ) -> CommandResult:
        """执行完整的 Windows 命令发送生命周期。"""
        self.verify()

        g = self.game
        own_attach = getattr(g, "_inj", None) is None and hasattr(g, "attach") and hasattr(g, "detach")
        if own_attach:
            g.attach()
        try:
            cmd = int(build_fn(g))
            if vt_rva is not None:
                self.attach_fake_vtable(cmd, vt_rva, size=fake_vt_size)

            can_ok: Optional[bool] = None
            if can_exec_fn is not None:
                can_ok = bool(can_exec_fn(g, cmd))
            elif can_exec_rva is not None:
                can_ok = self.can_exec(cmd, can_exec_rva)

            if dry_run:
                return CommandResult(
                    ok=(True if can_ok is None else bool(can_ok)),
                    cmd=cmd,
                    can_execute=can_ok,
                    posted=0,
                    extra={"dry_run": True},
                )

            if can_ok is False:
                return CommandResult(
                    ok=False,
                    cmd=cmd,
                    can_execute=False,
                    posted=0,
                    blocked_by="can_execute_refused",
                    error="CanExecute refused",
                )

            hook_ctx = None
            hook_ran = False
            try:
                if pre_post_hook is not None:
                    hook_ctx = pre_post_hook(g, cmd)
                    hook_ran = True
                if drain:
                    post_res = self.post_drained(cmd, seconds=seconds, done_fn=done_fn)
                    posted = int(bool(post_res.get("posted") if isinstance(post_res, dict) else post_res))
                    drained = post_res.get("drained") if isinstance(post_res, dict) else None
                else:
                    post_res = self.enqueue(cmd)
                    posted = int(bool(post_res))
                    drained = None
            finally:
                if hook_ran and post_cleanup_hook is not None:
                    post_cleanup_hook(g, cmd, hook_ctx)

            extra: Dict[str, Any] = {}
            if isinstance(post_res, dict):
                extra["post_result"] = post_res
            ok = bool(posted)
            err = ""
            if readback_fn is not None:
                rb = readback_fn(g, cmd, post_res) or {}
                if isinstance(rb, dict):
                    if "ok" in rb:
                        ok = bool(rb["ok"])
                    if rb.get("error"):
                        err = str(rb["error"])
                    for k, v in rb.items():
                        if k not in ("ok", "error"):
                            extra[k] = v

            return CommandResult(
                ok=ok,
                cmd=cmd,
                can_execute=can_ok,
                posted=posted,
                drained=drained,
                error=err,
                extra=extra,
            )
        finally:
            if own_attach:
                g.detach()
