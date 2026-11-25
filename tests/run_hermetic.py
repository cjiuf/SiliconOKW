# -*- coding: utf-8 -*-
"""零 Win32 / 零活进程 / 零 Pickle 跨平台离线纯逻辑测试统一入口。

用法：
    python tests/run_hermetic.py            # 默认开启 HOI4_NO_PICKLE=1 + 隔离临时 HOI4_STATE_DIR
    python tests/run_hermetic.py --with-legacy  # 额外串联全部 research/windows/test_*.py
"""
from __future__ import annotations

import argparse
import glob
import os
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS_DIR = os.path.join(ROOT, "tests")


def run_unittest_suite(verbosity: int = 2) -> unittest.TestResult:
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    loader = unittest.TestLoader()
    suite = loader.discover(TESTS_DIR, pattern="test_*.py", top_level_dir=ROOT)
    runner = unittest.TextTestRunner(verbosity=verbosity)
    return runner.run(suite)


def run_legacy_scripts(env: dict) -> tuple[int, list[tuple[str, int, str]]]:
    scripts = sorted(glob.glob(os.path.join(ROOT, "research", "windows", "test_*.py")))
    sub_env = dict(env)
    sub_env.setdefault("PYTHONIOENCODING", "utf-8")
    failures = []
    for script in scripts:
        cp = subprocess.run(
            [sys.executable, script],
            cwd=ROOT,
            env=sub_env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if cp.returncode != 0:
            rel = os.path.relpath(script, ROOT)
            failures.append((rel, cp.returncode, (cp.stdout + "\n" + cp.stderr)[-600:]))
    return len(scripts), failures


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="运行零依赖纯逻辑离线回归测试")
    ap.add_argument("--with-legacy", action="store_true", help="同时运行 research/windows/test_*.py 全部旧回归脚本")
    ap.add_argument("--no-pickle", action="store_true", help="全局强制 HOI4_NO_PICKLE=1（注意：依赖本地未入库 evdb_win.pkl 的用例将拒载）")
    ap.add_argument("-q", "--quiet", action="store_true", help="精简输出")
    ns = ap.parse_args(argv)

    t0 = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="hoi4_hermetic_") as td:
        old_state = os.environ.get("HOI4_STATE_DIR")
        old_no_pkl = os.environ.get("HOI4_NO_PICKLE")
        os.environ["HOI4_STATE_DIR"] = td
        if ns.no_pickle:
            os.environ["HOI4_NO_PICKLE"] = "1"
        try:
            res = run_unittest_suite(verbosity=1 if ns.quiet else 2)
            ok_unit = res.wasSuccessful()
        finally:
            if old_state is None:
                os.environ.pop("HOI4_STATE_DIR", None)
            else:
                os.environ["HOI4_STATE_DIR"] = old_state
            if old_no_pkl is None:
                os.environ.pop("HOI4_NO_PICKLE", None)
            else:
                os.environ["HOI4_NO_PICKLE"] = old_no_pkl

        legacy_ok = True
        if ns.with_legacy:
            env = dict(os.environ)
            env["HOI4_STATE_DIR"] = td
            total_leg, bad_leg = run_legacy_scripts(env)
            legacy_ok = (len(bad_leg) == 0)
            print(f"\n[legacy] PASSED {total_leg - len(bad_leg)}/{total_leg}")
            for rel, rc, tail in bad_leg:
                print(f"  [FAIL] {rel} (rc={rc}):\n{tail}")

    dt = time.monotonic() - t0
    print(f"\n[hermetic] elapsed={dt:.2f}s unit_ok={ok_unit} legacy_ok={legacy_ok}")
    return 0 if (ok_unit and legacy_ok) else 1


if __name__ == "__main__":
    raise SystemExit(main())
