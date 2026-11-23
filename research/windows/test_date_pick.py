"""Regression test for calling.api.pick_live_date -- pure, no game process.

Every candidate set below is taken from a real scan of this campaign (the
numbers are the measured hour counters, not invented), or reconstructed from
the documented ones in AGENTS.md Sec.13 / Sec.14.1 / Sec.16.10.

Run:  python research/windows/test_date_pick.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "calling"))
import api  # noqa: E402

P = api.parts_to_date
CASES = []


def case(name, vals, log, known, paused, want, want_reason):
    CASES.append((name, vals, log, known, paused, want, want_reason))


# ---------------------------------------------------------------- real 1938 set
# 2026-11-19d, live scan: 145 signature-valid CDates; the distinct values are
# these four (17039616 alone appears ~120 times).  Clock = 1938.10.05.21,
# game.log = 1938.10.04.22 (23 h behind, normal).
LIVE38 = P(1938, 10, 5, 21)          # 16983549
LOG38 = P(1938, 10, 4, 22)           # 16983526
R1938 = [P(1930, 1, 1), P(1935, 8, 14), LIVE38, P(1945, 3, 1)]
case("1938 cold start, log 23h behind", R1938, LOG38, None, None, LIVE38, "first>log")
case("1938 warm, paused", R1938, LOG38, LIVE38, True, LIVE38, "value==date_high (paused)")

# ------------------------------------------------------- the midnight clock bug
# The clock is at 00:00 one game hour in 24.  The retired `% 24 != 0` filter
# threw exactly this candidate away and then picked a bookmark.
MID = P(1936, 7, 16, 0)
case("clock exactly at midnight",
     [P(1930, 3, 4), P(1935, 8, 14), MID, P(1941, 10, 13)],
     MID - 5, None, None, MID, "first>log")

# ------------------------------------------------------------- stale game.log
# Documented case (AGENTS.md Sec.13): tool said 1936.02.17.17 while the HUD said
# 1936.07.16.09; the log stamp was 1935.12.07, five months stale, and a BOOKMARK
# sat exactly on that stamp.  "smallest >= log" would return the bookmark; the
# clock is the first candidate strictly AFTER the floor.
DOC = P(1936, 7, 16, 9)
STALE = [P(1930, 3, 4), P(1931, 7, 5), P(1935, 8, 14), P(1935, 12, 7),
         DOC, P(1937, 9, 17), P(1941, 10, 13), P(1945, 3, 1), P(1945, 3, 17)]
case("log 5 months stale + bookmark on the log stamp", STALE,
     P(1935, 12, 7), None, None, DOC, "first>log")

# ------------------------------------------- clock advanced since the last read
NEXT = P(1938, 10, 6, 4)
case("clock advanced 7h while running",
     [P(1930, 1, 1), P(1935, 8, 14), NEXT, P(1945, 3, 1)],
     P(1938, 10, 5, 20), LIVE38, False, NEXT, "first>log")

# ---------------------------------------- game.log written at the clock's hour
# Nothing plausible between the log stamp and the next bookmark: the stamp was
# the clock.  (1937.09.17 is 428 days on, i.e. outside the plausible window.)
case("clock == log stamp (paused)", [P(1930, 1, 1), DOC, P(1937, 9, 17), P(1945, 3, 1)],
     DOC, None, True, DOC, "log stamp is the clock (paused, nothing ticked)")
case("clock == log stamp (unpaused)", [P(1930, 1, 1), DOC, P(1937, 9, 17), P(1945, 3, 1)],
     DOC, None, False, DOC, "log stamp is the clock")

# ------------------------------------------------------------------ no log
case("no log, no memory -> largest", [P(1930, 1, 1), P(1935, 8, 14), LIVE38],
     None, None, None, LIVE38, "largest(no-log)")

# ------------------------------------------------- every candidate below the log
# Cannot happen with a real game.log (it is a lower bound), but the reader must
# return SOMETHING instead of None -- returning None is what used to freeze
# hoi4_date for days.
case("all below log", [P(1930, 1, 1), P(1935, 8, 14)], LIVE38, None, None,
     P(1935, 8, 14), "largest(all below floor)")

# --------------------------------------------------------------- degenerate
case("single candidate", [LIVE38], LOG38, None, None, LIVE38, "first>log")
case("empty", [], LOG38, None, None, None, "no-candidates")
case("duplicates collapse", [LIVE38, LIVE38, LIVE38], None, LIVE38, True,
     LIVE38, "value==date_high (paused)")

# ------------------------------------------------------------ KNOWN LIMITATION
# A stale copy of a RECENT clock value (here: yesterday's reading, still in the
# heap) cannot be told apart from the clock without a tick test, so while the
# game RUNS the pick can come back one read behind.  This is why the tick test
# in _find_date_exact runs first whenever the game is unpaused.  Documented, not
# asserted -- it must never turn into a hard failure of the suite.
LIMIT = [P(1930, 1, 1), LIVE38, NEXT, P(1945, 3, 1)]


def main():
    bad = 0
    for name, vals, log, known, paused, want, want_reason in CASES:
        got, why = api.pick_live_date(vals, log, known, paused)
        ok = got == want and why == want_reason
        bad += 0 if ok else 1
        print("%-4s %-46s got=%-10s want=%-10s %s"
              % ("OK" if ok else "FAIL", name, got, want, why))
    got, why = api.pick_live_date(LIMIT, P(1938, 10, 5, 20), LIVE38, False)
    print("%-4s %-46s got=%-10s (clock would be %s) %s"
          % ("INFO", "stale copy of a recent clock value", got, NEXT, why))
    print("\n%d/%d passed" % (len(CASES) - bad, len(CASES)))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
