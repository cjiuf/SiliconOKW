"""离线单测：SessionState 统一会话生命周期（换 PID / 读档 / reset_caches 一处清零）。

在仓库根目录执行：
    python research/windows/test_session_state.py
"""
import os
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "calling"))

import airops_win as AW  # noqa: E402
import combat  # noqa: E402
import divreserve  # noqa: E402
import game as game_mod  # noqa: E402
import housekeep  # noqa: E402
import movemicro  # noqa: E402
import peacewin  # noqa: E402
import reddots  # noqa: E402
import session_state as SS  # noqa: E402
import templedit  # noqa: E402
import trade_win  # noqa: E402

ok = fail = 0


def check(name, cond, detail=""):
    global ok, fail
    if cond:
        ok += 1
        print("PASS  %s" % name)
    else:
        fail += 1
        print("FAIL  %s %s" % (name, detail))


# 1. 验证所有核心全局缓存均已注册进 SessionState
snap = SS.snapshot()
reg = set(snap["registered_caches"])
expected_caches = {
    "game._VT_SCAN",
    "game._AIR_SNAP",
    "game._CONV_PROGRESS",
    "templedit._LIVE_POOL",
    "movemicro._DMG_HISTORY",
    "movemicro._ENEMY_DMG_HISTORY",
    "movemicro._TEMPLATE_CACHE",
    "movemicro._CAPITAL_CACHE",
    "movemicro._PEAK_ORG",
    "movemicro._PEAK_STR",
    "reddots._STATES",
    "combat._HIST",
    "combat._LAST_PREV",
    "combat._WR_CACHE",
    "divreserve._TMPL_CACHE",
    "divreserve._TMPL_NAME",
    "airops_win._AUTO_REFRESH_DONE",
    "airops_win._AUTO_REFRESH_EMPTY_AT",
    "trade_win._RES_PTR_CACHE",
    "peacewin._LAST_CONFERENCE",
    "housekeep._REJECTED_STATE_CACHE",
}
check("全部核心模块全局缓存均已注册到 SessionState",
      expected_caches.issubset(reg),
      {"missing": sorted(expected_caches - reg)})


def _seed_all():
    game_mod.Game._VT_SCAN[(1001, "CAirWing")] = [0x1111]
    game_mod._AIR_SNAP.update(pid=1001, ts=123.0, rows=[{"ptr": "0x1"}])
    game_mod._CONV_PROGRESS[(1001, 0x2222)] = 0.5
    templedit._LIVE_POOL["pid"] = 1001
    templedit._LIVE_POOL["by_name"]["infantry"] = 0x3333
    movemicro._DMG_HISTORY[(1001, 0x4444)] = 1.2
    movemicro._ENEMY_DMG_HISTORY[(1001, 123)] = 0.8
    movemicro._TEMPLATE_CACHE[(1001, "GER")] = {"Inf": {}}
    movemicro._CAPITAL_CACHE[(1001, "GER")] = 6521
    movemicro._PEAK_ORG[0x4444] = 60.0
    movemicro._PEAK_STR[0x4444] = 100.0
    reddots._STATES[(1001, "GER")] = {"seen_decisions": {"d1"}}
    reddots._LAST_FOCUS_CANDS[(1001, "GER")] = ["rhineland"]
    combat._HIST[(1001, 0x5555)] = (10.0, 20.0, 0)
    combat._LAST_PREV[1001] = {}
    combat._WR_CACHE[1001] = [0x6666]
    divreserve._TMPL_CACHE[(1001, 0x7777)] = True
    divreserve._TMPL_NAME[(1001, 0x7777)] = "Fallschirm"
    AW._AUTO_REFRESH_DONE[1001] = True
    AW._AUTO_REFRESH_EMPTY_AT[1001] = 1.0
    trade_win._RES_PTR_CACHE[1001] = {"oil": 0x8888}
    peacewin._LAST_CONFERENCE[1001] = 0x9999
    housekeep._REJECTED_STATE_CACHE[(1001, "GER", "arms_factory", 24)] = (1.0, {64})


def _all_empty():
    return (
        not game_mod.Game._VT_SCAN
        and game_mod._AIR_SNAP["pid"] is None
        and game_mod._AIR_SNAP["rows"] is None
        and not game_mod._CONV_PROGRESS
        and templedit._LIVE_POOL["pid"] is None
        and not templedit._LIVE_POOL["by_name"]
        and not movemicro._DMG_HISTORY
        and not movemicro._ENEMY_DMG_HISTORY
        and not movemicro._TEMPLATE_CACHE
        and not movemicro._CAPITAL_CACHE
        and not movemicro._PEAK_ORG
        and not movemicro._PEAK_STR
        and not reddots._STATES
        and not reddots._LAST_FOCUS_CANDS
        and not combat._HIST
        and not combat._LAST_PREV
        and not combat._WR_CACHE
        and not divreserve._TMPL_CACHE
        and not divreserve._TMPL_NAME
        and not AW._AUTO_REFRESH_DONE
        and not AW._AUTO_REFRESH_EMPTY_AT
        and not trade_win._RES_PTR_CACHE
        and not peacewin._LAST_CONFERENCE
        and not housekeep._REJECTED_STATE_CACHE
    )


# 保留容器对象引用用于校验原位 clear（兼容测试直接持有或 monkey-patch 容器）
vt_ref = game_mod.Game._VT_SCAN
dmg_ref = movemicro._DMG_HISTORY
rd_ref = reddots._STATES

# 2. 同会话同 (pid, world_ptr) 下 sync 不误清缓存
SS.invalidate("test_setup", pid=1001, world_ptr=0x11110000)
_seed_all()
triggered_same = SS.sync(pid=1001, world_ptr=0x11110000)
check("同 PID 同 world_ptr 下 sync 不触发误清零",
      triggered_same is False and len(game_mod.Game._VT_SCAN) == 1 and len(movemicro._DMG_HISTORY) == 1)

# 3. 同 PID 读旧档（world_ptr 改变）一处清零全部已注册缓存
triggered_reload = SS.sync(pid=1001, world_ptr=0x22220000)
check("同 PID 读旧档 (world_ptr 变更) 触发全部缓存一处清零",
      triggered_reload is True and _all_empty())
check("清零保持原容器对象引用不变 (兼容 monkey-patch)",
      game_mod.Game._VT_SCAN is vt_ref and movemicro._DMG_HISTORY is dmg_ref and reddots._STATES is rd_ref)

# 4. 切换 PID 一处清零全部已注册缓存
_seed_all()
triggered_pid = SS.sync(pid=2002, world_ptr=0x22220000)
check("切换 PID 触发全部缓存一处清零", triggered_pid is True and _all_empty())

# 4b. 切换 PID 时若本次 world_ptr=None，应作废旧进程 world_ptr；新进程首次读出后正常锚定且不二次误清，再次读档仍可检出
SS.invalidate("test_pid_none_setup", pid=3001, world_ptr=0xAAAA0000)
_seed_all()
t_switch = SS.sync(pid=3002, world_ptr=None)
check("换 PID 且 world_ptr=None 时触发清零并清空旧进程 world_ptr",
      t_switch is True and _all_empty() and SS.snapshot()["world_ptr"] is None)
_seed_all()
t_anchor = SS.sync(pid=3002, world_ptr=0xBBBB0000)
check("新进程首次读出 world_ptr 正常锚定且不触发二次误清",
      t_anchor is False and len(game_mod.Game._VT_SCAN) == 1
      and SS.snapshot()["world_ptr"] == hex(0xBBBB0000))
t_reload2 = SS.sync(pid=3002, world_ptr=0xCCCC0000)
check("新进程锚定后再次同 PID 读档准确触发一处清零",
      t_reload2 is True and _all_empty())

# 5. Game.reset_caches() 联动清零所有模块缓存
_seed_all()


class _FakeGame(game_mod.Game):
    def __init__(self):
        self.p = types.SimpleNamespace(pid=2002)


res = _FakeGame().reset_caches()
check("Game.reset_caches() 联动清零所有注册缓存并返回 cleared=True",
      res.get("cleared") is True and _all_empty() and "movemicro._DMG_HISTORY" in (res.get("cleared_caches") or []))

# 6. HOI4_STATE_DIR 环境变量覆盖与全量运行期文件迁出源码目录校验
import api as api_mod  # noqa: E402
import consolecmd  # noqa: E402
import frontd  # noqa: E402
import mapdata  # noqa: E402
import staff_supply  # noqa: E402
import turnengine  # noqa: E402
from process import winject as WJ  # noqa: E402
sys.path.insert(0, os.path.join(ROOT, "mcp"))
import server as mcp_srv  # noqa: E402

calling_dir = os.path.normcase(os.path.abspath(os.path.join(ROOT, "calling")))
runtime_paths = {
    "winject.FROZEN_FILE": WJ.FROZEN_FILE,
    "server._AIR_DONE_PATH": mcp_srv._AIR_DONE_PATH,
    "frontd.BEAT_PATH": frontd.BEAT_PATH,
    "frontd.LOCK_PATH": frontd.LOCK_PATH,
    "frontd.DIRECTIVES_PATH": frontd.DIRECTIVES_PATH,
    "movemicro.FRONT_STATE_PATH": movemicro.FRONT_STATE_PATH,
    "game.FRONT_SNAPSHOT_PATH": game_mod.FRONT_SNAPSHOT_PATH,
    "game.OBJINDEX_CACHE_PATH": game_mod.OBJINDEX_CACHE_PATH,
    "game.Game.TECH_IDS_CACHE": game_mod.Game.TECH_IDS_CACHE,
    "reddots.STATE_PATH": reddots.STATE_PATH,
    "turnengine.STATE_PATH": turnengine.STATE_PATH,
    "api.DATE_ADDR_CACHE": api_mod.DATE_ADDR_CACHE,
    "mapdata.PROV_PTR_CACHE": mapdata.PROV_PTR_CACHE,
    "consolecmd.CACHE": consolecmd.CACHE,
    "staff_supply.SHADOW_LOG": staff_supply.SHADOW_LOG,
}
outside_ok = all(
    not os.path.normcase(os.path.abspath(p)).startswith(calling_dir + os.sep)
    for p in runtime_paths.values()
)
check("全部 14 处运行态文件路径均已迁出源码目录 calling/", outside_ok, runtime_paths)
check("frontd.DIRECTIVES_PATH 与 movemicro.FRONT_STATE_PATH 指向同一文件",
      os.path.abspath(frontd.DIRECTIVES_PATH) == os.path.abspath(movemicro.FRONT_STATE_PATH),
      (frontd.DIRECTIVES_PATH, movemicro.FRONT_STATE_PATH))

old_env = os.environ.get("HOI4_STATE_DIR")
try:
    os.environ["HOI4_STATE_DIR"] = os.path.join(HERE, "_tmp_state_dir_probe")
    p_air = SS.state_path(SS.AIR_ATTACH_NAME)
    check("HOI4_STATE_DIR 正确覆盖 state_path 输出",
          p_air == os.path.join(HERE, "_tmp_state_dir_probe", "air_attach.json"), p_air)
finally:
    if old_env is None:
        os.environ.pop("HOI4_STATE_DIR", None)
    else:
        os.environ["HOI4_STATE_DIR"] = old_env

# 7. mcp/server.py 首次创建 Game() 正确通过 from process import winject 触发孤儿线程恢复（P2-1）
old_g, old_Game, old_rec = mcp_srv._g, mcp_srv.Game, mcp_srv._WINJECT_RECOVERY
old_resume = WJ.resume_orphans
old_heal, old_jump = mcp_srv._selfheal_check, mcp_srv._date_backjump
resumed_pids = []
try:
    class _DummyProc:
        pid = 42424
        base = 0x140000000
    class _DummyGame:
        p = _DummyProc()
        def _kick_tech_scan(self):
            return None
    mcp_srv._g = None
    mcp_srv.Game = _DummyGame
    mcp_srv._selfheal_check = lambda g: None
    mcp_srv._date_backjump = lambda g: None
    WJ.resume_orphans = lambda pid: (resumed_pids.append(pid), {"found": False, "resumed": 0})[1]
    mcp_srv.game()
    check("server.game() 首次启动正确导入 process.winject 并调用 resume_orphans",
          resumed_pids == [42424] and mcp_srv._WINJECT_RECOVERY == {"found": False, "resumed": 0},
          (resumed_pids, mcp_srv._WINJECT_RECOVERY))
finally:
    mcp_srv._g, mcp_srv.Game, mcp_srv._WINJECT_RECOVERY = old_g, old_Game, old_rec
    WJ.resume_orphans = old_resume
    mcp_srv._selfheal_check, mcp_srv._date_backjump = old_heal, old_jump

print(f"\n{ok} passed, {fail} failed")
sys.exit(1 if fail else 0)
