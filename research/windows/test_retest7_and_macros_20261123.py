# -*- coding: utf-8 -*-
"""2026-11-23 复测 7~11 离线回归薄入口（已按领域拆入 tests/retest7/）。

运行：python research/windows/test_retest7_and_macros_20261123.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from tests.retest7._common import _MemProc, check, counts, reset_counts  # noqa: F401
from tests.retest7.test_b1_b10_and_macros import (
    test_anti_cheese_reddots_and_declare_war,
    test_b1_b7_wargoal_direct,
    test_b2_supply_model,
    test_b3_front_changes,
    test_b4_b9_wait_and_peace,
    test_b5_b6_border_and_assigned,
    test_b8_b10_construction_and_reclaim,
    test_three_macros_and_health_board,
)
from tests.retest7.test_retest8_and_9 import (
    test_retest8_fixes,
    test_retest9_c1_to_c9_fixes,
    test_retest9_d1_to_d4_fixes,
    test_retest9_d5_d6_and_e1_to_e6_fixes,
)
from tests.retest7.test_retest10_11_and_security import (
    test_retest10_all_fixes,
    test_retest11_all_fixes,
    test_security_and_hard_bugs,
)


def main():
    reset_counts()
    test_b1_b7_wargoal_direct()
    test_b2_supply_model()
    test_b3_front_changes()
    test_b4_b9_wait_and_peace()
    test_b5_b6_border_and_assigned()
    test_b8_b10_construction_and_reclaim()
    test_anti_cheese_reddots_and_declare_war()
    test_three_macros_and_health_board()
    test_retest8_fixes()
    test_retest9_c1_to_c9_fixes()
    test_retest9_d1_to_d4_fixes()
    test_retest9_d5_d6_and_e1_to_e6_fixes()
    test_retest10_all_fixes()
    test_retest11_all_fixes()
    test_security_and_hard_bugs()
    passed, failed = counts()
    print("\nSummary: %d passed, %d failed" % (passed, failed))
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
