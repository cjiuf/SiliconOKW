"""Offline regression for the 2026-11-19 (second) fix round.

Run from stage/winrepo:
    python research/windows/test_fixes_20261119b.py

Covers:
  1. modifier totals read the CPdxArray SIZE (+0x1C), not the capacity (+0x18)
  2. construction lines read the accumulator at +0x38 and expose ic_per_day/ETA
  3. hoi4_focus_list(only_startable) really filters completed/prereq/mutex
  4. hoi4_set_focus explains a silent failure
  5. loc.py loads the game's localisation index
  6. decisions_meta parses common/decisions (cost / one-shot / trigger slice)
  7. hoi4_decisions rows carry title + cost
  8. air-wing re-attach is reported as idempotent, not as a failure
  9. server kicks the tech scan on a pid change
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "calling"))

ok = fail = 0


def check(name, cond, detail=""):
    global ok, fail
    if cond:
        ok += 1
        print("PASS  %s" % name)
    else:
        fail += 1
        print("FAIL  %s %s" % (name, detail))


def src(path):
    with open(os.path.join(ROOT, path), encoding="utf-8") as f:
        return f.read()


api = src("calling/api.py")
check("1a modifier size offset", "CM_SIZE = 0x1C" in api)
check("1b modifier id sanity gate", "CM_MAX_ID" in api and "0 <= mid < self.CM_MAX_ID" in api)
check("1c capacity no longer used as count", "cnt = p.i32(cm + self.CM_COUNT)" not in api)

game = src("calling/game.py")
check("2a BL_PROGRESS is +0x38", "BL_PROGRESS         = 0x38" in game)
check("2b BL_RATE is +0x30", "BL_RATE             = 0x30" in game)
check("2c construction exposes ETA", '"days_left"' in game and '"ic_per_day"' in game)
check("3a startable uses completed set", "completed = set(self.completed_focuses(tag))" in game)
check("3b startable uses focusplan groups", "prereq_groups" in game and "focusplan" in game)
check("4a set_focus explains failure", "focus_block_reason" in game)
check("4b reason text present", "did not switch the focus" in game)
check("8a air attach dedupe", "_air_attached" in game and "already_attached" in game)

srv = src("mcp/server.py")
check("9a tech scan kicked on pid change", "_kick_tech_scan()" in srv)
check("7a decisions tool returns rich rows", '"decisions": shown' in srv)

# --- 5. localisation -------------------------------------------------------
try:
    import loc
    data = loc.load()
    check("5a loc index built", data.get("count", 0) > 10000, "count=%s" % data.get("count"))
    ger = loc.title("GER_plan_z")
    check("5b loc resolves a decision title", bool(ger), "GER_plan_z=%r" % (ger,))
except Exception as e:
    check("5a loc index built", False, repr(e))

# --- 6. decision metadata --------------------------------------------------
try:
    import decisions_meta
    meta = decisions_meta.load()
    check("6a decision script parsed", meta.get("count", 0) > 1000, "count=%s" % meta.get("count"))
    pz = meta["decisions"].get("GER_plan_z") or {}
    check("6b GER_plan_z found", bool(pz), "missing")
    check("6c one-shot flag", pz.get("fire_only_once") is True, repr(pz.get("fire_only_once")))
    check("6d available trigger slice", bool(pz.get("available")), repr(pz.get("available")))
    check("6e category recorded", pz.get("category") == "GER_military_buildup", repr(pz.get("category")))
except Exception as e:
    check("6a decision script parsed", False, repr(e))

# --- 10. tech id cache survives a reload -----------------------------------
check("10a tech id cache written", os.path.exists(
    os.path.join(ROOT, "calling", "techids.pkl")))
check("10b cold lookups are guarded",
      "(self._tech_by_name or {}).get" in game and
      "(self._tech_by_id or {}).get" in game)
check("10c cold scan keeps the provisional table",
      "keep the provisional id -> name table" in game)

print("\n%d passed, %d failed" % (ok, fail))
sys.exit(1 if fail else 0)
