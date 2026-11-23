"""离线单测：armyops_win.offensive_line 的默认拒绝（2026-11-18 修的第 1 号 bug）。

背景：`COrderNewFrontCommand` 的裸 ctor `0x18C9840` 第 5 个参数是 **out 指针**，
旧代码把 `CPdxArray<CArmy*>` 塞进去，ctor 把 out 标志写进了数组头 —— 也就是
直接写穿了 [0] 那个堆指针。下一次解引用它就是
`ReadProcessMemory(0x1e99e9d0210) GetLastError=299`，两次杀进程。

所以默认路径必须**在碰游戏之前**就拒绝。这个测试用一个任何属性访问都爆炸的
假 game 证明「拒绝发生在任何读取之前」。

跑法：
    cd stage/winrepo
    python research/windows/test_offensive_line_refusal.py
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "calling"))

import armyops_win as A                                    # noqa: E402


class NoTouchG:
    """碰一下 g 的任何属性就算失败：默认路径不允许读进程。"""
    def __getattr__(self, name):
        raise AssertionError("offensive_line touched g.%s -- it must refuse "
                             "before any game access" % name)


class BoomG:
    """unsafe=True 才允许走到这里；p 一读就说明真的进函数体了。"""
    @property
    def p(self):
        raise RuntimeError("reached the guarded body")

    def __getattr__(self, name):
        raise RuntimeError("reached the guarded body (%s)" % name)


CASES = []


def case(name, fn, expect_ok, expect_in_error):
    CASES.append((name, fn, expect_ok, expect_in_error))


# 2026-11-19d: the refusal message changed once 0x10B8620 was proven to bail for
# every front order this tool creates -- the guard now points at
# manual_offensive() instead of at hoi4_execute_plan.  Assert on the stable part.
case("1. 默认调用直接拒绝",
     lambda: A.offensive_line(NoTouchG(), 0x1000, 7, [1, 2, 3]),
     False, "manual_offensive")
case("2. unsafe=False 同样拒绝",
     lambda: A.offensive_line(NoTouchG(), 0x1000, 7, [1, 2, 3], unsafe=False),
     False, "manual_offensive")
case("3. 有师的调用也拒绝（不是靠师数挡）",
     lambda: A.offensive_line(NoTouchG(), 0x1000, 7, [1, 2, 3], [0xAA, 0xBB]),
     False, "manual_offensive")
case("4. 默认开关是关的", lambda: A.RAW_OFFENSIVE_CTOR_ALLOWED, False, None)


def main():
    fails = 0
    for name, fn, expect_ok, expect_in_error in CASES:
        try:
            r = fn()
        except AssertionError as e:                       # 碰到游戏了 = 失败
            print("%-40s RAISED  FAIL" % name)
            print("    %s" % e)
            fails += 1
            continue
        ok = r[0] if isinstance(r, tuple) else r
        err = r[1] if isinstance(r, tuple) and len(r) > 1 else ""
        good = (bool(ok) == bool(expect_ok))
        if expect_in_error:
            good = good and (expect_in_error in str(err))
        print("%-40s ok=%-5s %s" % (name, bool(ok), "PASS" if good else "FAIL"))
        if not good:
            fails += 1
            print("    expect ok=%s error~%r" % (expect_ok, expect_in_error))
            print("    got    %r" % (r,))

    # 5. unsafe=True 必须真的进函数体（证明上面的拒绝不是无条件死返回）
    try:
        A.offensive_line(BoomG(), 0x1000, 7, [1, 2, 3], unsafe=True)
        print("%-40s no raise FAIL" % "5. unsafe=True 进入函数体")
        fails += 1
    except RuntimeError:
        print("%-40s PASS" % "5. unsafe=True 进入函数体")

    total = len(CASES) + 1
    print("\n%d/%d passed" % (total - fails, total))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
