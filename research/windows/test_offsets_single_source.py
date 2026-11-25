"""离线守门单测：PE 1.19.5.0 偏移单一真源（Single Source of Truth）一致性校验。

在仓库根目录执行：
    python research/windows/test_offsets_single_source.py

校验内容：
  1. `calling/offsets.py` 无 `import *`（保证 `pyflakes` 零盲区）且导出符号全覆盖 `offsets_win.py`；
  2. `calling/offsets_win.py` 保留全部 PE tombstone 保护（`FP_COMPLETED` 等必须显式为 `None`）；
  3. `research/windows/profile_1.19.5.0.json` 全部 56 个 `anchors` 已接入 `offsets_win.py` 且数值 100% 吻合；
  4. 全部 13 个核心业务模块跨文件无同名异值偏移冲突，且不私自硬编码已收敛的 PE RVA/字段常量。
"""
import ast
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "calling"))

import offsets as O  # noqa: E402
import offsets_win as OW  # noqa: E402

ok = fail = 0


def check(name, cond, detail=""):
    global ok, fail
    if cond:
        ok += 1
        print("PASS  %s" % name)
    else:
        fail += 1
        print("FAIL  %s %s" % (name, detail))


# 1. offsets.py 无 star-import 且完整转发 offsets_win 符号集
off_src = open(os.path.join(ROOT, "calling", "offsets.py"), encoding="utf-8").read()
off_tree = ast.parse(off_src)
has_star = any(
    isinstance(n, ast.ImportFrom) and any(a.name == "*" for a in n.names)
    for n in ast.walk(off_tree)
)
check("offsets.py 无 star-import", not has_star)

ow_names = {k for k in vars(OW) if not k.startswith("_")}
o_all = set(getattr(O, "__all__", ()))
check("offsets.__all__ 完整覆盖 offsets_win 公开符号", ow_names == o_all,
      {"missing_in_all": sorted(ow_names - o_all)[:10],
       "extra_in_all": sorted(o_all - ow_names)[:10]})

# 2. PE tombstone 保护不退化
for tomb in ("FP_CURRENT", "FP_COMPLETED", "FP_ROOT_FOCUSES",
             "GS_PLAYER_TAG_ALT", "CO_CAPITAL_STATE", "FN_IDLER_SET_PAUSED"):
    check(f"offsets_win.{tomb} 保持显式 None", getattr(OW, tomb) is None)

# 3. profile_1.19.5.0.json 全部 56 个 anchors 与 offsets_win 一一对应无冲突
prof = json.loads(
    open(os.path.join(ROOT, "research", "windows", "profile_1.19.5.0.json"), encoding="utf-8").read()
)
anchors = prof["anchors"]
ANCHOR_MAP = {
    "ALLOC_VTABLE": "ALLOC_VTABLE",
    "CMD_QUEUE_VT_PUSH": "CMD_QUEUE_VT_PUSH",
    "CO_DEPLOYMENT_STATUS": "CO_DEPLOYMENT_STATUS",
    "CO_FOCUS_TREE": "CO_FOCUS_TREE",
    "CO_POLITICAL_STATUS": "CO_POLITICAL_STATUS",
    "CO_PRODUCTION_STATUS": "CO_PRODUCTION_STATUS",
    "CO_TECHNOLOGY_STATUS": "CO_TECHNOLOGY_STATUS",
    "FN_DIPLO_VALIDATE": "CMD_FN_DIPLO_VALIDATE",
    "FN_GET_COUNTRY": "CMD_FN_GET_COUNTRY",
    "FN_NOOP_DTOR": "CMD_FN_NOOP_DTOR",
    "FN_POST": "CMD_FN_POST",
    "FN_RESOLVE_HANDLE": "CMD_FN_RESOLVE_HANDLE",
    "FN_SET_GAME_SPEED": "CMD_FN_SET_GAME_SPEED",
    "GS_COUNTRIES": "GS_COUNTRIES",
    "GS_GAME_SPEED": "GS_GAME_SPEED",
    "G_DEFAULT_ALLOC": "G_DEFAULT_ALLOC",
    "G_GAMESTATE": "G_GAMESTATE",
    "G_GAME_SPEED_SECONDS": "G_GAME_SPEED_SECONDS",
    "G_IDLER": "G_IDLER",
    "IDLER_PAUSED": "SESSION_IDLER_PAUSED",
    "IDLER_PAUSE_WRITE_SITE": "IDLER_PAUSE_WRITE_SITE",
    "OP_DELETE_SIZED": "CMD_OP_DELETE_SIZED",
    "OP_NEW": "CMD_OP_NEW",
    "PEACE_CANEXEC_DONE": "PEACE_CANEXEC_DONE",
    "PEACE_CANEXEC_PASS": "PEACE_CANEXEC_PASS",
    "PEACE_DONE_VT": "PEACE_DONE_VT",
    "PEACE_ENDTURN_VT": "PEACE_ENDTURN_VT",
    "PEACE_EXEC_DONE": "PEACE_EXEC_DONE",
    "PEACE_EXEC_ENDTURN": "PEACE_EXEC_ENDTURN",
    "PEACE_EXEC_PASS": "PEACE_EXEC_PASS",
    "PEACE_FN_INSTALL_TURN": "PEACE_FN_INSTALL_TURN",
    "PEACE_GET_CONF": "PEACE_GET_CONF",
    "PEACE_PASS_VT": "PEACE_PASS_VT",
    "SESSION_BUSY_FLAG": "CMD_SESSION_BUSY_FLAG",
    "SESSION_CMD_QUEUE": "CMD_SESSION_QUEUE",
    "VT_AIRWING": "AIR_VT_WING",
    "VT_AIRWINGPOOL": "AIR_VT_WINGPOOL",
    "VT_BUILDING_LINE": "VT_BUILDING_LINE",
    "VT_BUILDING_REF": "CMD_VT_BUILDING_REF",
    "VT_COUNTRY": "VT_COUNTRY",
    "VT_CURRENT_GAMESTATE": "VT_CURRENT_GAMESTATE",
    "VT_DECLARE_WAR": "CMD_VT_DECLARE_WAR",
    "VT_DIPLO_CMD": "CMD_VT_DIPLO_CMD",
    "VT_EVENT": "EVENT_EVT_VTS",
    "VT_EVENT_SCOPE": "EVENT_SCOPE_VT",
    "VT_EVENT_WINDOW": "EVENT_WND_VTS",
    "VT_FLEET": "NAVAL_FLEET_VTS",
    "VT_GAMESTATE": "VT_GAMESTATE",
    "VT_GENERATE_WARGOAL": "CMD_VT_GENERATE_WARGOAL",
    "VT_MIL_LINE": "VT_MIL_LINE",
    "VT_NAV_LINE": "VT_NAV_LINE",
    "VT_PEACE_CONFERENCE": "PEACE_VT_CONFERENCE",
    "VT_PEACE_TURN": "PEACE_VT_TURN",
    "VT_STATE": "VT_STATE",
    "VT_TASKFORCE": "NAVAL_TASKFORCE_VTS",
    "VT_WARGOAL": "VT_WARGOAL",
}
check("ANCHOR_MAP 覆盖 profile 全部 anchors", set(ANCHOR_MAP) == set(anchors),
      {"missing": sorted(set(anchors) - set(ANCHOR_MAP))})

# 单值主虚表常量（保留 int 类型兼容 game.py，对应 profile values[0]）
PRIMARY_VT_ANCHORS = {"VT_MIL_LINE", "VT_NAV_LINE"}

mismatches = []
for ak, ow_name in sorted(ANCHOR_MAP.items()):
    entry = anchors[ak]
    want = tuple(entry["values"]) if "values" in entry else entry["value"]
    got = getattr(OW, ow_name, None)
    if ak in PRIMARY_VT_ANCHORS and isinstance(want, tuple):
        if got != want[0]:
            mismatches.append((ak, ow_name, want[0], got))
        continue
    if isinstance(got, list):
        got = tuple(got)
    if got != want:
        mismatches.append((ak, ow_name, want, got))
check("profile 全部 56 个 anchors 与 offsets_win 数值完全一致", not mismatches, mismatches)

# 4. 跨 13 个核心业务模块扫描：无同名异值常量冲突，且无私自顶层十六进制硬编码回潮
TARGET_MODS = [
    "armyops_win", "events_win", "peacewin", "trade_win", "cmdwin",
    "legality", "airops_win", "navalops_win", "combat", "unitorders",
    "session", "game", "probe",
]
# 仅放行极少数历史未迁/派生表达式常量，其余顶层十六进制常量必须来自 offsets_win / offsets
ALLOW_LITERAL_HEX = {
    ("peacewin", "ACTION_SIZE"),
    ("peacewin", "ACTION_SIZE_CTOR"),
    ("unitorders", "ACT_TOKEN"),
    ("unitorders", "ACT_DIV_HANDLE"),
    ("unitorders", "ACT_PATH"),
    ("game", "FN_DYNAMIC_VAR"),
}

mod_vals = {}
rogue_literals = []
for mname in TARGET_MODS:
    mod = __import__(mname)
    for k, v in vars(mod).items():
        if not k.isupper():
            continue
        if isinstance(v, int) and not isinstance(v, bool):
            mod_vals.setdefault(k, []).append((mname, v))
        elif isinstance(v, tuple) and v and all(isinstance(x, int) for x in v):
            mod_vals.setdefault(k, []).append((mname, v))
    src = open(os.path.join(ROOT, "calling", f"{mname}.py"), encoding="utf-8").read()
    lines = src.splitlines()
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, ast.Assign):
            raw = lines[node.lineno - 1].split("#", 1)[0].strip()
            if ("0x" in raw or "0X" in raw) and not any(tok in raw for tok in ("_OW.", "O.", "_O.", "_offset_or_die(")):
                targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
                if len(node.targets) == 1 and isinstance(node.targets[0], ast.Tuple):
                    targets = [e.id for e in node.targets[0].elts if isinstance(e, ast.Name)]
                if any(t.isupper() and (mname, t) not in ALLOW_LITERAL_HEX for t in targets):
                    rogue_literals.append((mname, node.lineno, raw[:100]))

# 跨模块同名异值检测（排除语义本就不同的极少数短名，或要求全零冲突）
conflicts = []
for k, occs in sorted(mod_vals.items()):
    if len(occs) > 1:
        uniq = {v for _, v in occs}
        if len(uniq) > 1:
            conflicts.append((k, occs))

check("13 个核心模块间无同名异值偏移常量冲突", not conflicts, conflicts)
check("13 个核心模块无未收敛的顶层十六进制硬编码偏移", not rogue_literals, rogue_literals)
check("calling/probe.py ALLOC_OFF 已收敛至 PE G_DEFAULT_ALLOC",
      __import__("probe").ALLOC_OFF == OW.G_DEFAULT_ALLOC == 0x30FD000)

# 5. 全仓 AST 扫描：禁止通过 getattr(O/_O/_OW, "不存在的符号", ...) 静默落回或携带过时 Linux 默认值
OFFSET_ALIASES = {"O", "_O", "_OW", "_ow", "OW", "offsets", "offsets_win"}
OPTIONAL_NONE_ATTRS = {"CO_COUNTRY_INTEL"}
bad_getattrs = []
for rel_dir in ("calling", "mcp"):
    base_dir = os.path.join(ROOT, rel_dir)
    for cur_root, dirs, files in os.walk(base_dir):
        dirs[:] = sorted(d for d in dirs if d != "__pycache__")
        for fn in sorted(files):
            if not fn.endswith(".py") or fn == "_peek_adj.py":
                continue
            fp = os.path.join(cur_root, fn)
            rel_fp = os.path.relpath(fp, ROOT).replace("\\", "/")
            src = open(fp, encoding="utf-8", errors="replace").read()
            tree = ast.parse(src)
            for node in ast.walk(tree):
                if (isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Name)
                        and node.func.id == "getattr"
                        and len(node.args) >= 2
                        and isinstance(node.args[0], ast.Name)
                        and node.args[0].id in OFFSET_ALIASES
                        and isinstance(node.args[1], ast.Constant)
                        and isinstance(node.args[1].value, str)):
                    sym = node.args[1].value
                    if not sym.isupper():
                        continue
                    if not hasattr(OW, sym):
                        if sym not in OPTIONAL_NONE_ATTRS:
                            bad_getattrs.append((f"{rel_fp}:{node.lineno}", sym, "MISSING_IN_OFFSETS_WIN"))
                        continue
                    if len(node.args) >= 3 and isinstance(node.args[2], ast.Constant) and isinstance(node.args[2].value, int):
                        real_val = getattr(OW, sym)
                        if isinstance(real_val, int) and node.args[2].value != real_val:
                            bad_getattrs.append(
                                (f"{rel_fp}:{node.lineno}", sym,
                                 f"stale_default={hex(node.args[2].value)} != real={hex(real_val)}")
                            )

check("全仓 getattr(offsets, ...) 无拼错符号且无过时 Linux 偏移默认值", not bad_getattrs, bad_getattrs)

print(f"\n{ok} passed, {fail} failed")
sys.exit(1 if fail else 0)
