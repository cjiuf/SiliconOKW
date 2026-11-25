"""统一会话生命周期管理（SessionState）与运行期状态文件目录解析。

阶段 1（WP3）引入本模块解决两大工程痛点：
  1. 分散在各模块的全局缓存（`_VT_SCAN`、`live_pool`、`front_snapshot`、`reddots`、
     `_DMG_HISTORY` 等）统一注册到 `(pid, save_epoch)` 生命周期下，换进程（PID 变化）、
     读旧档/重开新局（`CCurrentGameState*` 地址变化）或手动重置时一处清零；
  2. 运行期落盘文件统一脱离源码目录 `calling/`，收口至 `%LOCALAPPDATA%\\SiliconOKW`
     （支持 `HOI4_STATE_DIR` 环境变量覆盖，与 `winject_frozen.json` 保持完全一致）。
"""
from __future__ import annotations

import os
import tempfile
import threading
import time

# 统一命名的运行态文件名常量，避免多模块各自拼接出错
WINJECT_FROZEN_NAME = "winject_frozen.json"
AIR_ATTACH_NAME = "air_attach.json"
FRONT_WORKER_BEAT_NAME = "front_worker.json"
FRONT_WORKER_LOCK_NAME = "front_worker.lock"
FRONT_DIRECTIVES_NAME = "front_directives.json"
FRONT_SNAPSHOT_NAME = "front_snapshot.json"
REDDOTS_STATE_NAME = "reddots_state.json"
TURN_STATE_NAME = "turn_state.json"
AUTOPILOT_STATE_NAME = "autopilot_state.json"
DATE_ADDR_NAME = "date_addr.json"
PROV_PTRS_NAME = "prov_ptrs.json"
CONSOLE_TABLE_NAME = "console_table.json"
OBJINDEX_NAME = "objindex.json.gz"
TECH_IDS_NAME = "techids.json"
STAFF_SHADOW_NAME = "staff_shadow.jsonl"


def state_dir(create: bool = False) -> str:
    """解析用户可写运行态目录：HOI4_STATE_DIR > %LOCALAPPDATA%\\SiliconOKW > tempdir。"""
    override = (os.environ.get("HOI4_STATE_DIR") or "").strip()
    if override:
        base = override
    else:
        local_app = (os.environ.get("LOCALAPPDATA") or "").strip()
        base = os.path.join(local_app, "SiliconOKW") if local_app else tempfile.gettempdir()
    if create:
        try:
            os.makedirs(base, exist_ok=True)
        except OSError:
            pass
    return base


def state_path(filename: str, create_dir: bool = False) -> str:
    """构造运行态文件的绝对路径。"""
    return os.path.join(state_dir(create=create_dir), filename)


def ensure_parent(path: str) -> str:
    """确保目标文件的父目录存在，返回原路径。"""
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        try:
            os.makedirs(parent, exist_ok=True)
        except OSError:
            pass
    return path


def _probe_pid_and_world(g):
    """纯只读探测 `(pid, world_ptr)`，对离线桩或未附着对象安全降级为 `(pid, None)`。"""
    if g is None:
        return None, None
    p = getattr(g, "p", None)
    pid = getattr(p, "pid", None)
    if pid is not None:
        try:
            pid = int(pid)
        except Exception:
            pid = None
    world = None
    try:
        base = getattr(p, "base", None)
        u64 = getattr(p, "u64", None)
        if base and callable(u64):
            import offsets_win as _ow
            gs_rva = getattr(_ow, "G_GAMESTATE", None)
            if gs_rva:
                val = int(u64(int(base) + int(gs_rva)))
                if val:
                    world = val
    except Exception:
        world = None
    return pid, world


class SessionState:
    """按 `(pid, save_epoch)` 托管各模块全局缓存的线程安全生命周期管理器。"""

    def __init__(self):
        self._lock = threading.RLock()
        self._pid = None
        self._world_ptr = None
        self._manual_epoch = 0
        self._caches = {}  # name -> callable()
        self._last_invalidation = None

    @property
    def save_epoch(self):
        with self._lock:
            return (self._world_ptr, self._manual_epoch)

    @property
    def current_key(self):
        with self._lock:
            return (self._pid, (self._world_ptr, self._manual_epoch))

    def register_cache(self, name: str, clear_fn=None, *, container=None):
        """注册一个缓存清理器。

        传入 `container`（如 dict / set / list）时将调用原对象的 `.clear()`，
        保留对象引用不变以兼容测试直接 monkey-patch 或持有该容器。
        """
        if clear_fn is None and container is None:
            raise ValueError("register_cache 需要提供 clear_fn 或 container")
        if clear_fn is None:
            def _clear_container(c=container):
                c.clear()
            clear_fn = _clear_container
        with self._lock:
            self._caches[str(name)] = clear_fn
        return clear_fn

    def unregister_cache(self, name: str):
        with self._lock:
            self._caches.pop(str(name), None)

    def invalidate(self, reason: str = "manual", *, pid=None, world_ptr=None):
        """递增 `manual_epoch` 并清空所有已注册缓存，返回成功清理的缓存名列表。"""
        with self._lock:
            self._manual_epoch += 1
            if pid is not None:
                self._pid = int(pid)
            if world_ptr is not None:
                self._world_ptr = int(world_ptr) if world_ptr else None
            items = list(self._caches.items())
            cleared = []
            errors = {}
            for name, fn in items:
                try:
                    fn()
                    cleared.append(name)
                except Exception as exc:
                    errors[name] = repr(exc)
            self._last_invalidation = {
                "reason": reason,
                "ts": time.time(),
                "pid": self._pid,
                "world_ptr": self._world_ptr,
                "manual_epoch": self._manual_epoch,
                "cleared": cleared,
                "errors": errors,
            }
            return cleared

    def sync(self, g=None, *, pid=None, world_ptr=None):
        """同步当前游戏会话指纹；若检测到 PID 切换或读档（`world_ptr` 变化），自动一处清零。"""
        if g is not None and (pid is None or world_ptr is None):
            p_pid, p_world = _probe_pid_and_world(g)
            if pid is None:
                pid = p_pid
            if world_ptr is None:
                world_ptr = p_world
        if pid is not None:
            pid = int(pid)
        if world_ptr is not None:
            world_ptr = int(world_ptr) if world_ptr else None

        with self._lock:
            reason = None
            if pid is not None and self._pid is not None and pid != self._pid:
                reason = f"pid_changed:{self._pid}->{pid}"
            elif (world_ptr is not None and self._world_ptr is not None
                  and world_ptr != self._world_ptr):
                reason = f"save_epoch_changed:{hex(self._world_ptr)}->{hex(world_ptr)}"

            if reason is not None:
                self._pid = pid if pid is not None else self._pid
                self._world_ptr = world_ptr
                self.invalidate(reason=reason)
                return True

            if pid is not None and self._pid is None:
                self._pid = pid
            if world_ptr is not None and self._world_ptr is None:
                self._world_ptr = world_ptr
            return False

    def session_key(self, g=None):
        """同步并返回当前 `(pid, save_epoch)` 会话键。"""
        if g is not None:
            self.sync(g)
        return self.current_key

    def snapshot(self):
        """返回当前会话生命周期的纯只读状态快照。"""
        with self._lock:
            return {
                "pid": self._pid,
                "world_ptr": hex(self._world_ptr) if self._world_ptr else None,
                "manual_epoch": self._manual_epoch,
                "registered_caches": sorted(self._caches.keys()),
                "last_invalidation": self._last_invalidation,
            }


SESSION = SessionState()


def register_cache(name: str, clear_fn=None, *, container=None):
    return SESSION.register_cache(name, clear_fn=clear_fn, container=container)


def sync(g=None, *, pid=None, world_ptr=None):
    return SESSION.sync(g=g, pid=pid, world_ptr=world_ptr)


def invalidate(reason: str = "manual", *, pid=None, world_ptr=None):
    return SESSION.invalidate(reason=reason, pid=pid, world_ptr=world_ptr)


def session_key(g=None):
    return SESSION.session_key(g=g)


def snapshot():
    return SESSION.snapshot()
