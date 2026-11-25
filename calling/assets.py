# -*- coding: utf-8 -*-
"""HOI4 Paradox 静态资产一键重建与清单状态管理层（第三期）。

封装原生 Rust 并行构建器 `tools/win/hoi4_recalc.exe assets`：
  - 单条命令 ~0.2-0.5s 重建全部 11 张静态表：
    loc / mapadj / maprivers / mapstates / statemeta / navalregions /
    techdefs / focusplans / decisions_meta / equipmeta / evdb
  - 原生支持 Paradox Mod VFS 覆盖顺序、`replace_path` 与 Launcher `dlc_load.json`；
  - 重建后自动清空进程内各模块缓存，无需重启 MCP 服务即可立即生效。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
RECALC_EXE = os.path.join(ROOT, "tools", "win", "hoi4_recalc.exe")
MANIFEST_PATH = os.path.join(HERE, "assets_manifest.json")


def recalc_available() -> bool:
    return sys.platform == "win32" and os.path.isfile(RECALC_EXE)


def _game_dir(explicit=None):
    if explicit:
        return explicit
    try:
        import config
        return config.game_dir(required=False)
    except Exception:
        return os.environ.get("HOI4_PATH")


def _user_dir(explicit=None):
    if explicit:
        return explicit
    try:
        import config
        return config.user_dir()
    except Exception:
        return os.environ.get("HOI4_USERDIR")


def dlc_load_path(user_dir=None):
    ud = _user_dir(user_dir)
    if not ud:
        return None
    p = os.path.join(ud, "dlc_load.json")
    return p if os.path.isfile(p) else None


def active_dlc_mods(user_dir=None):
    """读取 Paradox Launcher `dlc_load.json` 中当前启用的 Mod 描述符路径列表。"""
    p = dlc_load_path(user_dir)
    if not p:
        return []
    try:
        with open(p, "r", encoding="utf-8", errors="replace") as fh:
            d = json.load(fh)
        mods = d.get("enabled_mods") or []
        return [str(m) for m in mods if m]
    except Exception:
        return []


def manifest(path=None):
    """读取最近一次 `hoi4_recalc assets` 生成的清单 JSON（不存在返回 None）。"""
    p = path or MANIFEST_PATH
    if not os.path.isfile(p):
        return None
    try:
        with open(p, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None


def clear_memory_caches():
    """清空所有已导入静态表模块的进程内缓存，使新生成的 `.pkl` 立即生效。"""
    cleared = []
    specs = (
        ("loc", "_MEM", None),
        ("techdata", "_MEM", None),
        ("focusplan", "_MEM", None),
        ("decisions_meta", "_MEM", None),
        ("navalregions", "_MEM", None),
        ("events_win", "_DB", None),
    )
    for mod_name, attr, val in specs:
        m = sys.modules.get(mod_name)
        if m is not None and hasattr(m, attr):
            setattr(m, attr, val)
            cleared.append(mod_name)

    md = sys.modules.get("mapdata")
    if md is not None and isinstance(getattr(md, "_MEM", None), dict):
        md._MEM.clear()
        cleared.append("mapdata")

    em = sys.modules.get("equipmeta")
    if em is not None and isinstance(getattr(em, "_MEM_CACHE", None), dict):
        em._MEM_CACHE.clear()
        cleared.append("equipmeta")

    sm = sys.modules.get("statemeta")
    if sm is not None and hasattr(sm, "reset_cache"):
        sm.reset_cache()
        cleared.append("statemeta")

    return cleared


def rebuild(
    game_dir=None,
    mods=None,
    use_dlc_load=False,
    user_dir=None,
    lang=None,
    only=None,
    fmt="pkl",
    out_dir=None,
    evdb_out=None,
    manifest_path=None,
    report_path=None,
    timeout=60,
):
    """调用 `hoi4_recalc.exe assets` 多线程重建全部（或指定）静态资产表并刷新内存缓存。"""
    if not os.path.isfile(RECALC_EXE):
        raise FileNotFoundError("hoi4_recalc.exe not found at %s" % RECALC_EXE)

    gd = _game_dir(game_dir)
    if not gd or not os.path.isdir(gd):
        raise FileNotFoundError("HOI4 game directory not found: %s" % gd)

    od = out_dir or HERE
    mpath = manifest_path or os.path.join(od, "assets_manifest.json")

    cmd = [RECALC_EXE, "assets", "--game", gd, "--out-dir", od, "--manifest", mpath]
    if evdb_out:
        cmd += ["--evdb-out", evdb_out]

    ud = _user_dir(user_dir)
    if ud and os.path.isdir(ud):
        cmd += ["--user-dir", ud]

    if use_dlc_load:
        dlp = dlc_load_path(ud)
        if dlp:
            cmd += ["--dlc-load", dlp]

    for m in (mods or ()):
        if m:
            cmd += ["--mod", str(m)]

    if lang:
        cmd += ["--lang", str(lang)]

    if only:
        only_str = ",".join(only) if isinstance(only, (list, tuple, set)) else str(only)
        if only_str.strip():
            cmd += ["--only", only_str]

    if fmt:
        cmd += ["--format", str(fmt)]

    if report_path:
        cmd += ["--report", str(report_path)]

    cp = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    if cp.returncode != 0:
        raise RuntimeError(
            "hoi4_recalc assets failed (rc=%d): %s"
            % (cp.returncode, (cp.stderr or cp.stdout)[-600:])
        )

    cleared = clear_memory_caches()
    mf = manifest(mpath)
    # 自动登记 hoi4_recalc 本次生成的本地 .pkl SHA256，供 safe_cache 校验与 Mod 优先加载
    try:
        import safe_cache
        assets_map = (mf or {}).get("assets") if isinstance(mf, dict) else None
        if isinstance(assets_map, dict):
            for entry in assets_map.values():
                if isinstance(entry, dict):
                    p_out = entry.get("path") or entry.get("pkl")
                    if p_out and str(p_out).lower().endswith(".pkl") and os.path.isfile(p_out):
                        safe_cache.record_pickle_hash(p_out)
    except Exception as _he:
        import diag as _diag
        _diag.warn("assets.rebuild.record_pickle_hash", _he)
    return {
        "ok": True,
        "manifest_path": mpath,
        "manifest": mf,
        "cleared_caches": cleared,
        "stdout": cp.stdout,
        "stderr": cp.stderr,
    }


def summary(user_dir=None):
    """紧凑静态资产状态摘要（供 `hoi4_status` 与诊断脚本展示）。"""
    mf = manifest()
    enabled_mods = active_dlc_mods(user_dir)
    out = {
        "recalc_cli": recalc_available(),
        "manifest_present": mf is not None,
        "dlc_enabled_mods_count": len(enabled_mods),
    }
    if not mf:
        return out
    built_mods = [m.get("name") for m in (mf.get("mods") or []) if isinstance(m, dict)]
    out.update({
        "tool": mf.get("tool"),
        "lang": mf.get("lang"),
        "total_ms": mf.get("total_ms"),
        "built_at_unix": mf.get("built_at_unix"),
        "built_mods": built_mods,
        "assets_count": len(mf.get("assets") or {}),
        "mods_count_match": len(built_mods) == len(enabled_mods),
    })
    return out


def _cli(argv=None):
    ap = argparse.ArgumentParser(description="HOI4 静态资产一键重建与状态工具")
    sub = ap.add_subparsers(dest="cmd")

    p_st = sub.add_parser("status", help="查看当前静态表清单与 Launcher Mod 状态")
    p_st.add_argument("--user-dir", default=None)

    p_rb = sub.add_parser("rebuild", help="调用 hoi4_recalc.exe assets 并行重建全部静态表")
    p_rb.add_argument("--game", default=None)
    p_rb.add_argument("--out-dir", default=None)
    p_rb.add_argument("--evdb-out", default=None)
    p_rb.add_argument("--user-dir", default=None)
    p_rb.add_argument("--use-dlc-load", action="store_true", help="自动读取 dlc_load.json 启用 Mod")
    p_rb.add_argument("--mod", action="append", default=[], help="追加 .mod 文件或 Mod 目录")
    p_rb.add_argument("--lang", default=None, help="主语言（如 english / simp_chinese）")
    p_rb.add_argument("--only", default=None, help="逗号分隔只重建指定表")
    p_rb.add_argument("--format", default="pkl", choices=("pkl", "json", "both"))
    p_rb.add_argument("--report", default=None)

    ns = ap.parse_args(argv)
    if ns.cmd == "status" or not ns.cmd:
        print(json.dumps(summary(getattr(ns, "user_dir", None)), ensure_ascii=False, indent=2))
        return 0
    if ns.cmd == "rebuild":
        res = rebuild(
            game_dir=ns.game,
            mods=ns.mod,
            use_dlc_load=ns.use_dlc_load,
            user_dir=ns.user_dir,
            lang=ns.lang,
            only=ns.only,
            fmt=ns.format,
            out_dir=ns.out_dir,
            evdb_out=ns.evdb_out,
            report_path=ns.report,
        )
        print(res["stdout"].rstrip())
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(_cli())
