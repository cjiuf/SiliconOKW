"""离线单测：movemicro._required_garrison 的 hold 配额上限（BUG-G）。

背景：1939.07.30 那局 96 个 line_holder 摊在 22 个边境省上，hold 的
`required_garrison` 每个省要 2，合计 44，可 `hold_cap` 写的是 30 —— 旧的收缩
循环 `if req[p] <= 2: break` 在"每个省都是 2"时立刻退出，cap 从来没生效过。
结果 `_free_pool` 永远空，7 个缺口全报 "no reserve division available"。

跑法：cd stage/winrepo && python research/windows/test_front_laws.py
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "calling"))

import movemicro as M                                    # noqa: E402


def make_state(n_border=22, n_divs=100, pressured=()):
    border = list(range(9000, 9000 + n_border))
    ours = [{"ptr": 0x1000 + i, "prov": border[i % n_border],
             "org_pct": 0.9, "str_pct": 0.95, "moving": False}
            for i in range(n_divs)]
    st = {
        "border": border,
        "ours": ours,
        "theirs": [],
        "theirs_by": {},
        "enemy_neighbors": {},
        "ours_by": {},
        "stance": "hold",
    }
    for i, p in enumerate(border):
        st["enemy_neighbors"][p] = [8000 + i]
        st["theirs_by"][8000 + i] = [{}] * (8 if p in pressured else 1)
        st["ours_by"][p] = [r for r in ours if r["prov"] == p]
    return st


CASES = []


def case(name, ok, **kw):
    CASES.append((name, ok, kw))


case("22 个边境省，hold 配额总和 <= 30", True, n_border=22, n_divs=100)
case("8 个边境省（西线），配额总和 <= 24", True, n_border=8, n_divs=40)
case("40 个边境省，仍然 <= 30", True, n_border=40, n_divs=120)


def main():
    fails = 0
    for name, expect, kw in CASES:
        st = make_state(**kw)
        req = M._required_garrison(st, {"stance": "hold", "min_garrison": 1})
        total = sum(req.values())
        cap = max(len(st["border"]), min(len(st["border"]) * 3, 30))
        good = total <= cap and all(v >= 1 for v in req.values())
        print("%-38s total=%-3d cap=%-3d %s"
              % (name, total, cap, "PASS" if good else "FAIL"))
        if not good:
            fails += 1
            print("    ", req)

    # 压力省必须拿到 3
    st = make_state(n_border=8, n_divs=40, pressured={9001})
    req = M._required_garrison(st, {"stance": "hold", "min_garrison": 1})
    good = req[9001] == 3
    print("%-38s req[9001]=%d %s"
          % ("被压的省保持 3 个师", req[9001], "PASS" if good else "FAIL"))
    if not good:
        fails += 1

    print("\n%d/%d passed" % (len(CASES) + 1 - fails, len(CASES) + 1))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
