"""MCP 工具领域子包（拆分自 mcp/server.py）。"""
from __future__ import annotations

import importlib

# 按领域显式列出 10 个工具模块，确保全部 181 工具稳定注册
TOOL_MODULES = (
    "core",
    "politics",
    "economy",
    "military",
    "battleplan",
    "diplomacy",
    "air_navy",
    "macros",
    "compact",
    "debug",
)


def register_all():
    """导入全部 10 个领域工具模块，向 server.TOOLS / server.HANDLERS 注册 181 工具。"""
    loaded = []
    for name in TOOL_MODULES:
        loaded.append(importlib.import_module(f"{__name__}.{name}"))
    return loaded
