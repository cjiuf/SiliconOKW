"""MCP 工具共享 Schema 常量、标准响应结构辅助与主引擎实时穿透转发器。

通过 `_srv()` 访问唯一的 `server` 主模块对象，确保离线测试对 `server.game`、
`server._is_pid_alive`、`server._refuse_unpause_with_empty_orders`、`server._PAUSE_LAST`
等符号的 monkey-patch 实时穿透到所有工具模块。
"""
from __future__ import annotations

import sys

_STR = {"type": "string"}
_INT = {"type": "integer"}
_BOOL = {"type": "boolean"}
NO_ARGS = {"type": "object", "properties": {}}
TAG = {"tag": dict(_STR, description="Country tag (empty = the player)")}
TAG_DEFAULT = {"tag": {"type": "string", "description": "Country tag (default: the player)"}}
OG = {"orders_group": {"type": "string",
      "description": 'COrdersGroup pointer, the ptr from hoi4_armies output (e.g. "0x5606b3212a40")'}}

MAX_PAUSE_HOURS = 300
PAUSE_COOLDOWN_S = 20.0
_PAUSE_LAST = [0.0]

CALL_TIMEOUT_S = 50.0
_CALL = {"thread": None, "name": None, "started": 0.0, "cancel": None}


def _srv():
    """返回当前进程内唯一的 MCP server 模块对象。"""
    return sys.modules["server"]


def ok_response(**fields) -> dict:
    """构造标准成功响应字典（保留所有附加业务字段）。"""
    out = {"ok": True}
    out.update(fields)
    return out


def error_response(error: str, blocked_by: str | None = None, **fields) -> dict:
    """构造标准失败/拦截响应字典。"""
    out = {"ok": False, "error": str(error)}
    if blocked_by is not None:
        out["blocked_by"] = blocked_by
    out.update(fields)
    return out


def _ptr(v):
    """将 0x... 十六进制字符串或十进制整数规范化为指针整数（与基线语义完全一致）。"""
    if v is None:
        return None
    if isinstance(v, str):
        s = v.strip()
        return int(s, 16) if s.lower().startswith("0x") else int(s)
    return int(v)


def tool(name, description, props=None, required=None):
    return _srv().tool(name, description, props=props, required=required)


def game():
    return _srv().game()


def verified(result, probe, label=""):
    return _srv().verified(result, probe, label=label)


def _is_pid_alive(pid: int) -> bool:
    return _srv()._is_pid_alive(pid)


def _looks_like_dead_read(exc) -> bool:
    return _srv()._looks_like_dead_read(exc)


def _refuse_unpause_with_empty_orders(g):
    return _srv()._refuse_unpause_with_empty_orders(g)


def _win_hwnd(title_part="Hearts of Iron"):
    return _srv()._win_hwnd(title_part)


def screenshot(crop=None, focus=False) -> bytes:
    return _srv().screenshot(crop, focus)


def command_docs():
    return _srv().command_docs()
