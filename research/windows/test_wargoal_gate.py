"""离线单测：game._wargoal_gate —— 宣战前置的第 4 条硬拦（必须有战争借口）。

背景：Windows 的宣战通道 `cmdwin.declare_war` -> `CDeclareWarAction` 绕过了引擎
自己的可用性检查 `action+0x70`，所以"没借口也能宣"这件事只能由工具侧挡。
以前这条只写在 AGENTS.md 里当规矩，工具不检查（2026-11-18 补上）。

不碰游戏进程，纯逻辑：
  1. 有 CB                              -> problem=None
  2. 没 CB、也没在造                   -> problem 提到 hoi4_justify_wargoal
  3. 没 CB、但借口正在造               -> problem 提到 days_left / ETA
  4. 正在造的是**别的国家**            -> 仍按"没有"处理
  5. cb 是个空 dict / 没有 ptr         -> 仍按"没有"处理

跑法：
    cd stage/winrepo
    python research/windows/test_wargoal_gate.py
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))          # stage/winrepo
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "calling"))

from calling.game import _wargoal_gate                     # noqa: E402

CB = {"ptr": "0x1234", "id": 77, "owner_tag": "GER", "target_tag": "HOL",
      "wargoal": "take_state", "valid": True}

JUST_HOL = [{"target": "HOL", "days_left": 3, "eta_date": "1936.08.04",
             "source_class": "CTimedWargoalActivity", "pp_drain": 0.6}]
JUST_BEL = [{"target": "BEL", "days_left": 40, "eta_date": "1936.09.10",
             "source_class": "CTimedWargoalActivity", "pp_drain": 0.6}]

CASES = [
    ("1. 有 CB", CB, [], None, "have_cb"),
    ("2. 没 CB 也没在造", None, [], "hoi4_justify_wargoal", "missing"),
    ("3. 借口正在造", None, JUST_HOL, "days left", "justifying"),
    ("4. 在造别的国家的", None, JUST_BEL, "hoi4_justify_wargoal", "missing"),
    ("5. cb 是空 dict", {}, [], "hoi4_justify_wargoal", "missing"),
    ("6. cb 没有 ptr", {"wargoal": "take_state"}, [], "hoi4_justify_wargoal",
     "missing"),
]


def main():
    fails = 0
    for name, cb, just, expect_in_problem, expect_state in CASES:
        prob, state = _wargoal_gate("HOL", cb, just)
        good = (state == expect_state)
        if expect_in_problem is None:
            good = good and prob is None
        else:
            good = good and (prob is not None and expect_in_problem in prob)
        print("%-26s state=%-11s %s" % (name, state, "PASS" if good else "FAIL"))
        if not good:
            fails += 1
            print("    expect state=%r problem~%r" % (expect_state, expect_in_problem))
            print("    got    state=%r problem=%r" % (state, prob))
    print("\n%d/%d passed" % (len(CASES) - fails, len(CASES)))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
