"""离线单测：Game._front_plan_check —— 宣战前置的「前线+进攻线+已指派集团军」判定。

不碰游戏进程。所有内存读取都用桩替换，只验证判定逻辑本身：
  1. 完全没画线                -> ok=False, problem 提到 hoi4_army_battleplans
  2. 画了线但 assigned == 0    -> ok=False, problem 提到 hoi4_assign_divisions
  3. 有师但没进攻线            -> ok=False, problem 提到 hoi4_offensive_line
  4. 前线+进攻线+已指派        -> ok=True, plan 里有具体军队

跑法：
    cd stage/winrepo
    python research/windows/test_front_plan_check.py
"""
import os
import sys
import types

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))          # stage/winrepo
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "calling"))

import armyops_win as A                                   # noqa: E402
import armyops                                            # noqa: E402
from calling.game import Game                             # noqa: E402


# --------------------------------------------------------------------------- stubs
class FakeP:
    """只实现 _front_plan_check 用到的那一个读取。"""
    def u8(self, addr):
        return 0                                          # 不是军团组


class FakeCountry:
    tag = "GER"


def make_game(*, fronts, orders, children, groups=(0x1000,)):
    g = Game.__new__(Game)                                # 不走 __init__，不连进程
    g.p = FakeP()
    g._c = lambda tag=None: FakeCountry()
    armyops.orders_groups = lambda g_, co: list(groups)
    A.fronts_towards = lambda g_, tgt, own=None: list(fronts)
    A.orders = lambda g_, og: list(orders)
    A.order_children = lambda g_, og: list(children)
    return g


FRONT_HANDLE = [11, 2]


def front_order(oid=7, assigned=0, handle=None):
    return {"ptr": "0x2000", "id": oid, "type": 2,
            "root_front": list(handle or FRONT_HANDLE),
            "root_section": 2, "from": 0, "to": 100000,
            "assigned_divisions": assigned}


def offensive_child(root=7, oid=8, assigned=0, typ=1):
    return {"root": root, "ptr": 0x3000, "id": oid, "type": typ,
            "assigned": assigned, "executing": 0}


FACE = [{"front": 0x4000, "front_handle": FRONT_HANDLE, "section": 2,
         "section_index": 0, "border_provinces": [85, 86]}]


# --------------------------------------------------------------------------- cases
CASES = []


def case(name, expect_ok, expect_in_problem, **kw):
    CASES.append((name, expect_ok, expect_in_problem, kw))


case("1. 完全没画线", False, "hoi4_army_battleplans",
     fronts=FACE, orders=[], children=[])
case("2. 画了线但 assigned == 0", False, "hoi4_assign_divisions",
     fronts=FACE, orders=[front_order(assigned=0)], children=[offensive_child(assigned=0)])
case("3. 有师但没进攻线", False, "hoi4_offensive_line",
     fronts=FACE, orders=[front_order(assigned=12)], children=[])
case("4. 前线+进攻线+已指派", True, None,
     fronts=FACE, orders=[front_order(assigned=12)], children=[offensive_child(assigned=12)])

# 额外：命令对的是别的方向，不能算数
case("5. 线画在别的方向上", False, "hoi4_army_battleplans",
     fronts=FACE, orders=[front_order(assigned=12, handle=[99, 9])],
     children=[offensive_child(assigned=12)])

# 额外：只有前进线没有进攻线，但子命令类型是 2（不是 1）
case("6. 子命令不是进攻线(type=2)", False, "hoi4_offensive_line",
     fronts=FACE, orders=[front_order(assigned=12)],
     children=[offensive_child(assigned=12, typ=2)])


def main():
    fails = 0
    for name, expect_ok, expect_in_problem, kw in CASES:
        g = make_game(**kw)
        r = g._front_plan_check("POL")
        ok = bool(r.get("ok"))
        prob = str(r.get("problem") or "")
        good = (ok == expect_ok) and (expect_in_problem is None
                                      or expect_in_problem in prob)
        print("%-34s ok=%-5s %s" % (name, ok, "PASS" if good else "FAIL"))
        if not good:
            fails += 1
            print("    expect ok=%s problem~%r" % (expect_ok, expect_in_problem))
            print("    got      %s" % r)
    print("\n%d/%d passed" % (len(CASES) - fails, len(CASES)))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
