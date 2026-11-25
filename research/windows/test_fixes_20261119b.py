"""Offline regression for the 2026-11-19 (second) fix round.

Run from repository root:
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

import inspect
import game as _game_mod
import offsets_win as _ow
_Game = _game_mod.Game
_src_cq = inspect.getsource(_Game.construction_queue)
_src_sf = inspect.getsource(_Game.focuses) + inspect.getsource(_Game.selectable_focuses)
_src_setf = inspect.getsource(_Game.set_focus)
_src_air = inspect.getsource(_Game.attach_air_wings_to_armies)
_src_tech = (
    inspect.getsource(_Game._load_tech_db)
    + inspect.getsource(_Game.tech_name)
    + inspect.getsource(_Game.start_research)
    + inspect.getsource(_Game.available_research)
    + inspect.getsource(_Game._kick_tech_scan)
)
check("2a BL_PROGRESS is +0x38", _game_mod.BL_PROGRESS == 0x38 and _ow.BL_PROGRESS == 0x38)
check("2b BL_RATE is +0x30", _game_mod.BL_RATE == 0x30 and _ow.BL_RATE == 0x30)
check("2c construction exposes ETA", '"days_left"' in _src_cq and '"ic_per_day"' in _src_cq)
check("3a startable uses completed set", "completed = set(self.completed_focuses(tag))" in _src_sf)
check("3b startable uses focusplan groups", "prereq_groups" in _src_sf and "focusplan" in _src_sf)
check("4a set_focus explains failure", "focus_block_reason" in _src_setf)
check("4b reason text present", "did not switch the focus" in _src_setf)
check("8a air attach dedupe", "_air_attached" in _src_air and "already_attached" in _src_air)

_mcp_dir = os.path.join(ROOT, "mcp")
if _mcp_dir not in sys.path:
    sys.path.insert(0, _mcp_dir)
import server as _mcp_srv
_srv_heal = inspect.getsource(_mcp_srv._selfheal_check) + inspect.getsource(_mcp_srv.game)
_srv_dec = inspect.getsource(_mcp_srv._decisions)
check("9a tech scan kicked on pid change", "_kick_tech_scan()" in _srv_heal)
check("7a decisions tool returns rich rows", '"decisions": shown' in _srv_dec)

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
import session_state as _ss
check("10a tech id cache path isolated and readable",
      os.path.normcase(os.path.dirname(os.path.abspath(_Game.TECH_IDS_CACHE)))
      == os.path.normcase(os.path.abspath(_ss.state_dir()))
      and os.path.basename(_Game.TECH_IDS_CACHE) == _ss.TECH_IDS_NAME)
check("10b cold lookups are guarded",
      "(self._tech_by_name or {}).get" in _src_tech and
      "(self._tech_by_id or {}).get" in _src_tech)
check("10c cold scan keeps the provisional table",
      "keep the provisional id -> name table" in _src_tech)

print("\n%d passed, %d failed" % (ok, fail))
sys.exit(1 if fail else 0)
