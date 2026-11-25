# -*- coding: utf-8 -*-
"""轻量级诊断日志与节流错误聚合模块（严禁输出到 stdout）。

设计约束：
1. MCP 服务端使用 stdout 承载 JSON-RPC 2.0 协议帧，因此本模块**只允许向 sys.stderr 输出**，
   绝不写 stdout。
2. 对高频循环（对象读取、发令重试、状态持久化）中的容错异常按 `(tag, exc_type)` 节流
   （默认每 60s 输出一条并汇总 `suppressed=N`），既彻底消除静默吞异常盲区，又不刷屏。
3. 同时维护线程安全的最近 200 条结构化环形缓冲区与聚合计数器，供排障与单测断言。
"""
from __future__ import annotations

from collections import deque
import os
import sys
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

_LOCK = threading.Lock()
_RECENT: deque = deque(maxlen=200)
_COUNTS: Dict[Tuple[str, str], int] = {}
_LAST_EMIT: Dict[Tuple[str, str], float] = {}
_SUPPRESSED: Dict[Tuple[str, str], int] = {}


def _flag_enabled(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def warn(
    tag: str,
    exc: Any,
    *,
    detail: str = "",
    interval: float = 60.0,
) -> Dict[str, Any]:
    """记录一次被降级处理的异常并按节流窗口向 stderr 输出结构化摘要。"""
    exc_type = type(exc).__name__ if isinstance(exc, BaseException) else "warning"
    exc_msg = str(exc)
    now = time.time()
    mono = time.monotonic()
    key = (str(tag), exc_type)
    entry: Dict[str, Any] = {
        "ts": round(now, 3),
        "tag": str(tag),
        "type": exc_type,
        "error": exc_msg[:300],
    }
    if detail:
        entry["detail"] = str(detail)[:200]

    should_emit = False
    suppressed_n = 0
    with _LOCK:
        _RECENT.append(entry)
        _COUNTS[key] = _COUNTS.get(key, 0) + 1
        verbose = _flag_enabled("HOI4_DIAG_VERBOSE")
        quiet = _flag_enabled("HOI4_DIAG_QUIET")
        last = _LAST_EMIT.get(key)
        if not quiet and (verbose or last is None or (mono - last) >= max(0.0, float(interval))):
            should_emit = True
            suppressed_n = _SUPPRESSED.pop(key, 0)
            _LAST_EMIT[key] = mono
        elif not quiet:
            _SUPPRESSED[key] = _SUPPRESSED.get(key, 0) + 1

    if should_emit:
        parts = [f"[hoi4-diag] {tag}: {exc_type}: {entry['error']}"]
        if detail:
            parts.append(f"({entry['detail']})")
        if suppressed_n > 0:
            parts.append(f"[suppressed={suppressed_n}]")
        line = " ".join(parts) + "\n"
        try:
            sys.stderr.write(line)
        except Exception:
            # stderr 不可写时不得拖垮主调用链
            pass
    return entry


def stats() -> Dict[str, Any]:
    """返回聚合诊断计数快照。"""
    with _LOCK:
        by_key = {f"{t}:{et}": cnt for (t, et), cnt in sorted(_COUNTS.items())}
        total = sum(_COUNTS.values())
        return {"total": total, "by_key": by_key}


def recent(limit: int = 50, tag: Optional[str] = None) -> List[Dict[str, Any]]:
    """返回最近的结构化诊断记录（按时间正序）。"""
    with _LOCK:
        items = list(_RECENT)
    if tag is not None:
        items = [x for x in items if x.get("tag") == tag]
    if limit is not None and limit >= 0:
        items = items[-int(limit):]
    return items


def reset() -> None:
    """清空内存诊断缓冲与计数器（用于重连或单元测试隔离）。"""
    with _LOCK:
        _RECENT.clear()
        _COUNTS.clear()
        _LAST_EMIT.clear()
        _SUPPRESSED.clear()
