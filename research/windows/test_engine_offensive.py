# -*- coding: utf-8 -*-
"""离线单测：armyops_win.engine_offensive（2026-11-20 §108.5 后重新启用）。

时间线：
  * §106（c0a9baf）：arg8=1 → 路径B 理论；
  * §107（12fecbc）：实机推翻——空态下恒等表快路径 AV，硬禁用；
  * §108（eb57fb3）：静态闭环——缺的是 front-context 层（UI 每帧刷新
    0xF711E0 维护，命令/Execute 路径里没有）；
  * §108.5 第 1 步实机通过（2026-11-20，UI 手画线后
    COUNTRIES[GER]+0x168 非空、边境省 +0xd0 指入其中）→ 重新启用。

现在的契约（离线可测）：
  1. 空省份直接拒绝，不碰进程（NoTouchG）；
  2. 有省份时必然读进程（NoTouchG 抛 AssertionError = 不再是无脑拒绝；
     真实拒绝逻辑——front-context 层为空——需要实机，离线只做源码断言）；
  3. 常量：G 门 / G_GAMESTATE / GS_COUNTRIES / 0xF711E0。

跑法：
    cd <repo>
    python research/windows/test_engine_offensive.py
"""
import os
import sys
import inspect

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "calling"))

import armyops_win as A                                    # noqa: E402

FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print("  ok  %s" % name)
    else:
        print("  FAIL %s %s" % (name, detail))
        FAILURES.append(name)


class NoTouchG:
    """碰任何属性即失败：空省份路径不允许读进程。"""
    def __getattr__(self, name):
        raise AssertionError("engine_offensive touched g.%s" % name)


def main():
    print("== engine_offensive: no-province = auto-select (§109) ==")
    # §109: empty provinces no longer refuses -- it auto-selects provinces
    # whose controller area matches the root's (0x10BB5A0).  That path reads
    # g.p first, so NoTouchG raises: not a blanket refusal any more.
    try:
        A.engine_offensive(NoTouchG(), 0xdeadbeef, 1, None)
        check("auto-select reads process (not refusal)", False,
              "returned without touching g")
    except AssertionError:
        check("auto-select reads process (not refusal)", True)

    print("== engine_offensive: live-path now requires a process ==")
    try:
        A.engine_offensive(NoTouchG(), 0xdeadbeef, 1, [104, 9281])
        check("raises on NoTouchG (not a blanket refusal)", False,
              "returned without touching g")
    except AssertionError:
        check("raises on NoTouchG (not a blanket refusal)", True)

    print("== engine_offensive: §108/§109 guards present in source ==")
    src = inspect.getsource(A.engine_offensive)
    check("front-context guard present", "front-context layer empty" in src)
    check("controller-area auto-select present", "controller area" in src)
    check("mode-3 replication (arg7=1, arg8=0)",
          "(cmd, og, pa, root, darr, 0, 1, 0, 0)" in src)
    check("+0x6d/+0x6e byte guard present", "p.u8(cmd + 0x6d)" in src
          and "p.u8(cmd + 0x6e)" in src)
    check("no G-gate write on the mode-3 path",
          "gate_addr = p.va(FN_OFFENSIVE_GATE)" not in src)

    print("== engine_offensive: constants ==")
    check("FN_OFFENSIVE_EXECUTE == 0x18D9070", A.FN_OFFENSIVE_EXECUTE == 0x18D9070)
    check("FN_OFFENSIVE_CANEXE == 0x18DF550", A.FN_OFFENSIVE_CANEXE == 0x18DF550)
    check("FN_NEW_OFFENSIVE_CMD == 0x18C9840", A.FN_NEW_OFFENSIVE_CMD == 0x18C9840)
    check("VT_OFFENSIVE == 0x2A853A0", A.VT_OFFENSIVE == 0x2A853A0)
    check("FN_OFFENSIVE_GATE == 0x33DDAB7 (ctor +0x6e = arg8 && G)",
          getattr(A, "FN_OFFENSIVE_GATE", 0) == 0x33DDAB7)
    check("G_GAMESTATE == 0x33DD6B0 (sync offsets_win)",
          getattr(A, "G_GAMESTATE", 0) == 0x33DD6B0)
    check("GS_COUNTRIES == 0x310 (sync offsets_win)",
          getattr(A, "GS_COUNTRIES", 0) == 0x310)
    check("FN_UI_FRONTCTX_REFRESH == 0xF711E0 (§108 UI per-frame refresh)",
          getattr(A, "FN_UI_FRONTCTX_REFRESH", 0) == 0xF711E0)

    print()
    if FAILURES:
        print("FAILED: %d" % len(FAILURES))
        for f in FAILURES:
            print("  - %s" % f)
        sys.exit(1)
    print("all assertions passed")


if __name__ == "__main__":
    main()
