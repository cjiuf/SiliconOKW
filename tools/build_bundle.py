# -*- coding: utf-8 -*-
"""Build the distributable zip of the Windows HOI4 tool.

Layout mirrors the repo so `mcp/server.py` still does `import game` after
`srv.add_to_path(calling)`, and the research scripts keep their relative paths.

Included
  calling/            core call layer + reverse-engineering helpers + command tables
  mcp/                MCP server
  research/           reverse-engineering scripts, notes, console_commands.json
  research/windows/   PE 1.19.5.0 disassembly/scan tools + RTTI symbol table
  docs/               playbook + bug journal (the reversal record)

Excluded on purpose (all regenerable, listed in MANIFEST as such)
  *.pkl bigger than 1 MB, except research/windows/rtti_win.pkl (the symbol table)
  __pycache__, .git, *.bak_*, *.lock, runtime jsonl
"""
import hashlib
import os
import shutil
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NAME = sys.argv[1] if len(sys.argv) > 1 else "SiliconOKW-win-1.19.5-20261123"
OUT = os.path.join(ROOT, "dist")
STAGE = os.path.join(OUT, NAME)
ZIP = os.path.join(OUT, NAME + ".zip")

KEEP_BIG_PKL = {"research/windows/rtti_win.pkl"}
SKIP_DIRS = {"__pycache__", ".git", "dist", ".idea", ".vscode", "target", "profiles"}
SKIP_EXT = {".pyc", ".pyo", ".lock"}
SKIP_SUFFIX = (".bak_20261118", ".bak", ".jsonl", ".tmp")
SKIP_FILES = {
    "calling/_peek_adj.py",
    "calling/turn_state.json",
    "calling/turn_state.json.tmp",
    "calling/reddots_state.json",
    "calling/reddots_state.json.tmp",
    "calling/front_snapshot.json",
    "calling/front_directives.json",
    "calling/front_worker.json",
    "calling/date_addr.json",
    "calling/prov_ptrs.json",
    "calling/air_attach.json",
    "calling/assets_manifest.json",
    "calling/techids.pkl",
    "calling/winject_frozen.json",
    "calling/process/winject_frozen.json",
    "calling/subunit_pointers_live.json",
}
BIG_PKL_LIMIT = 1 << 20          # 1 MB

# only these top-level trees go in
TREES = ["assets", "calling", "mcp", "native", "research", "docs"]
ROOT_FILES = ["README.md", "README_EN.md", "INSTALL.md", "requirements.txt", "LICENSE", "clausewitz.py",
              "subunit_catalog.json", "subunit_pointers.json"]
DOCS = []


def keep(rel, size):
    if rel in KEEP_BIG_PKL:
        return True
    if rel.endswith(".pkl") and size > BIG_PKL_LIMIT:
        return False
    return True


def walk():
    picked = []
    for tree in TREES:
        base = os.path.join(ROOT, tree)
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for fn in filenames:
                full = os.path.join(dirpath, fn)
                rel = os.path.relpath(full, ROOT).replace("\\", "/")
                ext = os.path.splitext(fn)[1].lower()
                if ext in SKIP_EXT or fn.endswith(SKIP_SUFFIX):
                    continue
                if rel in SKIP_FILES:
                    continue
                size = os.path.getsize(full)
                if not keep(rel, size):
                    continue
                picked.append((full, rel, size))
    for fn in ROOT_FILES:
        full = os.path.join(ROOT, fn)
        if os.path.isfile(full):
            picked.append((full, fn, os.path.getsize(full)))
    return picked


def main():
    if os.path.isdir(STAGE):
        shutil.rmtree(STAGE)
    os.makedirs(STAGE)
    files = walk()
    staged = []
    seen = set()
    for full, rel, size in files:
        if rel in seen:          # docs/AGENTS.md is both in the tree and in DOCS
            continue
        seen.add(rel)
        dst = os.path.join(STAGE, rel.replace("/", os.sep))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(full, dst)
        staged.append((rel, size, dst))
    for src, rel in DOCS:
        if not os.path.isfile(src):
            continue
        dst = os.path.join(STAGE, rel.replace("/", os.sep))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)
        size = os.path.getsize(dst)
        if rel in seen:
            # authoritative copy wins (docs/AGENTS.md is in the tree too)
            staged = [(r, size if r == rel else s, d) for r, s, d in staged]
        else:
            seen.add(rel)
            staged.append((rel, size, dst))
    # ---- README + MANIFEST ------------------------------------------------
    readme = os.path.join(STAGE, "BUNDLE_README.md")
    with open(readme, "w", encoding="utf-8") as fh:
        fh.write(README_TEXT % NAME)
    staged.append(("BUNDLE_README.md", os.path.getsize(readme), readme))
    staged.sort(key=lambda x: x[0])
    man = os.path.join(STAGE, "MANIFEST.txt")
    with open(man, "w", encoding="utf-8") as fh:
        fh.write("# %s\n# path\tbytes\tsha256[:16]\n" % NAME)
        total = 0
        for rel, size, dst in staged:
            h = hashlib.sha256(open(dst, "rb").read()).hexdigest()[:16]
            fh.write("%s\t%d\t%s\n" % (rel, size, h))
            total += size
        fh.write("# files=%d bytes=%d\n" % (len(staged), total))
    # ---- zip --------------------------------------------------------------
    if os.path.isfile(ZIP):
        os.remove(ZIP)
    with zipfile.ZipFile(ZIP, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for dirpath, dirnames, filenames in os.walk(STAGE):
            for fn in filenames:
                full = os.path.join(dirpath, fn)
                rel = os.path.relpath(full, OUT).replace("\\", "/")
                z.write(full, rel)
    size = os.path.getsize(ZIP)
    zip_sha = hashlib.sha256(open(ZIP, "rb").read()).hexdigest()
    sha_path = os.path.join(OUT, "SHA256.txt")
    with open(sha_path, "w", encoding="utf-8") as fh:
        fh.write("%s  %s.zip\n" % (zip_sha, NAME))
    print("staged files : %d" % len(staged))
    print("uncompressed : %.1f MB" % (sum(s for _, s, _ in staged) / 1048576.0))
    print("zip          : %s  (%.1f MB)" % (ZIP, size / 1048576.0))
    print("sha256       : %s" % zip_sha)


README_TEXT = """# HOI4 Windows tool bundle — %s

HOI4 1.19.5.0 (Windows x64 PE) 独立自动化工具集：核心调用层、逆向层、双模式 MCP 服务端、Rust 原生扫描扩展、符号与命令表。

## 目录

| 路径 | 内容 |
|---|---|
| `calling/` | **核心调用层**（88 模块）：`game.py`、`api.py`、`mem.py` + `process/`（Win32 读写与单线程劫持注入）、`cmdwin.py`、`armyops_win.py`、`airops_win.py`、`navalops_win.py`、`combat.py`、`events_win.py`、`peacewin.py`、`trade_win.py`、`movemicro.py`、`ragemicro.py`、`frontd.py`、`macros.py`、`housekeep.py`、`reddots.py`、`turnengine.py`、`offsets_win.py` |
| `calling/*.pkl`、`*.json` | **预生成命令表**：`cmdvts.json`、`cmdctors.pkl`、`cmdsigs.pkl`、`cmdfields.pkl`、`tokens.pkl` 等 |
| `calling/hoi4_fastmem.pyd` | **可选 Rust 原生扫描扩展**（PyO3 + Rayon 多核 Win32 扫描，Windows x64 / CPython 3.11+ `abi3`）；缺失时自动回落至纯 Python/numpy |
| `native/` | `hoi4_fastmem` 与 `hoi4_recalc` 两个 Rust crate 完整源码及构建脚本 |
| `mcp/server.py` | **MCP 服务端**（Full 模式 **177 个** `hoi4_*` 工具；加 `--compact` 切至 5 工具极简模式，Schema < 1k tokens）；`mcp/config/windows-*.json` 为客户端模板 |
| `research/` | **PE 逆向层**与 342 KB 反汇编笔记 `WINDOWS_1.19.5.md`、`console_commands.json` |
| `research/windows/` | PE 离线反汇编/扫描工具链（`pedis.py`、`vtscan.py`、`rtti_win.py`）、`rtti_win.pkl` 符号表与 **22 套离线回归测试** `test_*.py` |
| `docs/` | 操作手册 `AGENTS.md`、策略提示词 `STRATEGY_PROMPT.md`、崩溃/缺陷排查日志与已知问题清单 |

## 未打入（首次运行自动重建）

| 文件 | 说明 |
|---|---|
| `calling/objindex.pkl`（~50 MB） | RTTI 实例快照，绑定单次游戏进程，重启即失效 |
| `calling/loc.pkl`（~14 MB） | 本地化文本索引，从游戏 `localisation/` 自动生成 |
| `research/windows/evdb_win.pkl`（~12 MB） | 事件定义库，从游戏 `events/` 自动生成 |
| 本机运行状态 `turn_state.json` / `reddots_state.json` / `front_*.json` | 按对局实时生成 |

## 快速启动

```powershell
python -m pip install -r requirements.txt
python clausewitz.py doctor
python clausewitz.py serve            # Full 模式（177 工具）
python clausewitz.py serve --compact  # Compact 模式（5 工具极简）
```

## 关键注意事项

1. 所有 PE 常量均以 `image_base + RVA` 寻址；写命令须走 `cmdwin` + 假虚表路径。
2. `hoi4_offensive_line` 接收**敌方目标省**（或直接传 `enemy_tag` 自动推导，经 `0xD57D90` 敌对连通区匹配）；需要转守时调用 `hoi4_execute_plan(order_id=<type-1 id>, executing=false)` 或 `hoi4_offensive_line(enabled=false)`，无需删除父前线。
3. 整军撤退/调兵优先使用 `hoi4_move_divisions(spread=true, max_per_province=8)`，避免单省堆叠崩溃补给。
4. 保持游戏窗口未最小化；读档后解暂停前先调用 `hoi4_purge_orders` 清理 0 师空订单（防 `0xB2E62E` 崩溃）。
"""


if __name__ == "__main__":
    sys.exit(main())
