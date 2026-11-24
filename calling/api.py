"""
Live game API, Hearts of Iron IV 1.19.2 (Linux native).

Two capabilities:
  1) READ   : direct reads from game memory, plus calling the game's own getters
  2) ACTION : running any of the 383 console commands programmatically, by calling
              functions inside the process

Reverse-engineering notes are in research/FINDINGS.md; offsets in offsets.py.
"""
from __future__ import annotations
import json, os, re, struct, sys, time
from typing import Iterable, Optional

from mem import Proc
from call import Injector, CallError
import offsets as O

_LIBC_TRIM_FN = None
_LIBC_TRIM_INIT = False


def _read_chunk(proc, addr, buf, n):
    """优先使用 try_read_into 零拷贝读入缓冲；兼容仅提供 try_read 的测试替身。"""
    fn = getattr(proc, "try_read_into", None)
    if fn is not None:
        return fn(addr, buf, n)
    data = proc.try_read(addr, n)
    if not data:
        return 0
    got = len(data)
    buf[:got] = data
    return got


def _malloc_trim():
    """Linux 下归还空闲堆页；Windows 上直接 no-op，避免循环内上万次加载 libc.so.6 抛异常。"""
    global _LIBC_TRIM_FN, _LIBC_TRIM_INIT
    if sys.platform == "win32":
        return
    if not _LIBC_TRIM_INIT:
        _LIBC_TRIM_INIT = True
        try:
            import ctypes
            _LIBC_TRIM_FN = ctypes.CDLL("libc.so.6").malloc_trim
        except Exception:
            _LIBC_TRIM_FN = None
    if _LIBC_TRIM_FN is not None:
        try:
            _LIBC_TRIM_FN(0)
        except Exception:
            pass


def _s64(u: int) -> int:
    return struct.unpack("<q", struct.pack("<Q", u & 0xFFFFFFFFFFFFFFFF))[0]


def _sdiv(a: int, b: int) -> int:
    q = abs(a) // abs(b)
    return q if (a < 0) == (b < 0) else -q


def date_to_parts(hours: int):
    """Clausewitz hour counter -> (year, month, day, hour)"""
    year, rem = divmod(hours, O.DAYS_PER_YEAR * 24)
    doy, hour = divmod(rem, 24)
    month, day = 1, doy
    for length in O.MONTH_LENGTHS:
        if day < length:
            break
        day -= length
        month += 1
    return year, month, day + 1, hour


def parts_to_date(year, month, day, hour=0) -> int:
    doy = sum(O.MONTH_LENGTHS[: month - 1]) + (day - 1)
    return (year * O.DAYS_PER_YEAR + doy) * 24 + hour


def pick_live_date(values, log_date=None, known=None, paused=None):
    """Pick the live clock out of the signature-valid CDate values.

    Pure function -- no game access -- so the rule can be regression-tested
    against recorded candidate sets (`research/windows/test_date_pick.py`).

    Everything that is not the live clock is a BOOKMARK or a SCHEDULED date:
    a constant that never moves.  Therefore

      * game.log is a LOWER BOUND: it only lags the clock, it can never lead
        it, so every candidate below the last log stamp is a leftover;
      * the clock is the FIRST candidate after that floor.  If that jump is
        implausibly large (> 400 days) then nothing sits between the floor and
        the next bookmark, which means the floor stamp WAS the clock;
      * when the game is PAUSED, time cannot have moved, so the value this
        process already read is authoritative.  When it is RUNNING that value
        may be hours old and a stale copy of it can still be in memory, so it
        must not be trusted -- the tick test / the log floor decide instead.

    A "not midnight" test must never be used: the clock is at 00:00 one game
    hour in 24, and then that test silently throws the real clock away.

    Returns (value, reason).
    """
    vals = sorted({int(v) for v in values})
    if not vals:
        return None, "no-candidates"
    if known:
        known = int(known)
        if paused and known in vals:
            return known, "value==date_high (paused)"
    floor = int(log_date) if log_date else None
    if known and paused and (floor is None or known - 48 > floor):
        # paused + a remembered value: the clock cannot have moved backwards
        floor = known - 48
    if floor is None:
        return vals[-1], "largest(no-log)"
    if paused and floor in vals:
        # 2026-11-21: a STOPPED clock cannot have moved past its own last log
        # stamp, so the stamp is the clock.  Without this, `strict[0]` picks the
        # next CDate above the floor -- measured on a paused 1936.02.20 game:
        # the log stamp was 16960566 and the picker returned 16960999, a
        # scheduled date 18 days out, which the reporting layer then had to
        # reject as "18 d ahead".  `paused` here means "nothing ticked during
        # the 2.5 s window", not the unreliable idler byte.
        return int(floor), "log stamp is the clock (paused, nothing ticked)"
    strict = [v for v in vals if v > floor]
    if strict and strict[0] - floor <= 24 * 400:
        return strict[0], "first>log"
    if floor in vals:
        # nothing plausible between the log stamp and the next bookmark, so the
        # log stamp itself was the clock when it was written
        return floor, "log stamp is the clock"
    if strict:
        # 2026-11-21: a candidate YEARS above the log is a bookmark / planned
        # date, never the clock.  Measured on a fresh 1936.01.01 campaign: this
        # branch returned 1945.03.01.00, which then failed the ±400-day cache
        # check on EVERY call, so each date read re-scanned 3.8 GB (11-22 s) and
        # hoi4_session_info blew its 50 s budget.  Prefer the log stamp.
        below = [v for v in vals if v <= floor]
        if below:
            return below[-1], "largest(below floor) (far)"
        return None, "no candidate within 400 days of the log"
    return vals[-1], "largest(all below floor)"


# Bookmark / scheduled-date distance above the log stamp that we refuse to call
# a clock when the candidate sits at midnight.  A live clock CAN be at 00:00 (one
# hour in 24), so midnight alone proves nothing; but a midnight value this far
# above the game's own log is a stored event date, not a running clock.
DATE_MIDNIGHT_AHEAD_DAYS = 30
# Hard-gate thresholds for `date_raw` itself (see date_plausible).
DATE_AHEAD_REJECT_DAYS = 45
DATE_AHEAD_FRESH_DAYS = 3


def date_bookmark_shape(v, logd) -> bool:
    """Is `v` the STORED-EVENT-DATE shape rather than a clock reading?

    Midnight + more than DATE_MIDNIGHT_AHEAD_DAYS above the game's own log.
    Event dates, bookmarks and scheduled dates are all written at 00:00
    (`_find_date_exact` says so in its own docstring), while a live clock is at
    00:00 for one hour in 24 -- so midnight alone proves nothing, and "ahead of
    the log" alone proves nothing either (a log can sit months behind a running
    game).  Only the two together are suspicious.

    Used by `date()` for REPORTING: the value is replaced with the log stamp, a
    proven lower bound, so a caller never sees a date months in the future.
    """
    try:
        v = int(v)
    except (TypeError, ValueError):
        return False
    if not logd:
        return False
    return v % 24 == 0 and v > int(logd) + 24 * DATE_MIDNIGHT_AHEAD_DAYS


def date_plausible(v, logd, log_age_s=None) -> bool:
    """Hard gate for `date_raw`: may `v` be handed out as THE live clock?

    Two rules, both already accepted by this codebase:
      * more than DATE_AHEAD_REJECT_DAYS above the log.  `date_raw`'s verified
        branch has used a 60-day allowance since 2026-11-19 ("60 days is already
        generous"); 45 is that same rule, tightened enough to catch the measured
        56.75-day case.
      * more than DATE_AHEAD_FRESH_DAYS above a log appended within two hours:
        then the log pins the clock to hours and the reading cannot be days
        ahead.  Expressed as a freshness test, not a blanket rule, because a
        PAUSED game stops appending and a legitimate clock may be far ahead of a
        months-old stamp.
    """
    try:
        v = int(v)
    except (TypeError, ValueError):
        return False
    if not logd:
        return True
    logd = int(logd)
    ahead = v - logd
    # The log is written FROM the clock, so it can never lead it: anything more
    # than a few hours BEHIND the last log stamp is provably not the live clock
    # (2026-11-21: a midnight constant was still being returned 15 h behind a
    # freshly written stamp).  Three hours covers the hourly granularity.
    if ahead < -3:
        return False
    if ahead > 24 * DATE_AHEAD_REJECT_DAYS:
        return False
    if log_age_s is not None and log_age_s <= 7200 and ahead > 24 * DATE_AHEAD_FRESH_DAYS:
        return False
    return True


class ConsoleResult:
    __slots__ = ("ok", "message", "command")

    def __init__(self, ok, message, command):
        self.ok, self.message, self.command = bool(ok), message, command

    def __repr__(self):
        return "ConsoleResult(ok=%s, %r)" % (self.ok, self.message)

    def to_dict(self):
        return {"ok": self.ok, "message": self.message, "command": self.command}


class Country:
    """Read wrapper around a single CCountry instance."""

    def __init__(self, game: "HOI4", tag: str, tag_id: int, ptr: int):
        self.game, self.tag, self.tag_id, self.ptr = game, tag, tag_id, ptr

    # ---- sub-objects ----
    @property
    def economy(self):
        if O.CO_ECONOMY is None:
            return 0
        return self.game.p.u64(self.ptr + O.CO_ECONOMY)

    @property
    def politics(self): return self.game.p.u64(self.ptr + O.CO_POLITICS)

    # ---- industry ----
    @property
    def military_factories(self) -> int:
        if sys.platform == "win32":
            return self._win_building_counts()[1]
        return _sdiv(self.game.p.i64(self.economy + O.EC_MIL_FACTORIES), O.FIXED)

    @property
    def naval_dockyards(self) -> int:
        if sys.platform == "win32":
            return self._win_building_counts()[2]
        return _sdiv(self.game.p.i64(self.economy + O.EC_NAVAL_DOCKYARDS), O.FIXED)

    # ---- factories (Windows): sum building levels over the owned states ----
    # CBuilding type ids are stable in 1.19.5.0 and were verified by summing the
    # same states the UI counts (GER 35+28+10 = the 73 on the top bar; BEL 11+6+0,
    # HOL 11+4+3, LUX 3+1+0, FRA 35+8+11, ENG 32+14+19 all agree).
    BLD_CIV, BLD_MIL, BLD_DOCK = 20013, 20002, 19446
    # 2026-11-23 (复测 6 P1-4): CState + 0xB0 是真实的 CPdxArray<int32> cores
    # （拥有该州核心的国家 tag_id 列表，已实证 GER 19 州、FRA 86 州与被占德国州
    # 42/50/58/59 100% 精确）。彻底废除已失效的 CState + 0x2F0 猜测。
    ST_CORES_ARR = 0xB0

    def _state_is_core_win(self, state_ptr):
        """判断 CState 是否属于本国核心州（纯只读 CState + 0xB0 CPdxArray<int32>）。"""
        p = self.game.p
        try:
            cores_d = p.u64(state_ptr + self.ST_CORES_ARR)
            cores_n = p.i32(state_ptr + self.ST_CORES_ARR + 0x0C)
            if not cores_d or cores_n <= 0:
                return False
            if cores_n > 128:
                return None  # 异常长度，视为不可读而非强行判 False
            vals = struct.unpack("<%di" % cores_n, p.read(cores_d, 4 * cores_n))
            return int(self.tag_id) in vals
        except Exception:
            return None

    def _win_building_counts(self, core_only=False):
        p = self.game.p
        d = p.u64(self.ptr + 0x460)
        n = p.i32(self.ptr + 0x46C)
        civ = mil = dock = 0
        core_reads = 0
        core_errors = 0
        if d and 0 < n < 2000:
            for s in struct.unpack("<%dQ" % n, p.read(d, 8 * n)):
                if core_only:
                    is_core = self._state_is_core_win(s)
                    if is_core is None:
                        core_errors += 1
                        continue
                    core_reads += 1
                    if not is_core:
                        continue
                dd = p.u64(s + 0x158)
                nn = p.i32(s + 0x164)
                if not dd or not (0 < nn < 400):
                    continue
                for b in struct.unpack("<%dQ" % nn, p.read(dd, 8 * nn)):
                    try:
                        tid = p.i32(b + 0x08)
                        lvl = struct.unpack("<h", p.read(b + 0x40, 2))[0]
                    except OSError:
                        continue
                    if tid == self.BLD_CIV:
                        civ += lvl
                    elif tid == self.BLD_MIL:
                        mil += lvl
                    elif tid == self.BLD_DOCK:
                        dock += lvl
        if core_only and n > 0 and core_reads == 0 and core_errors > 0:
            return None, None, None
        return civ, mil, dock

    # ---- manpower (Windows) ----
    # Every CState carries an embedded CStateManpower at +0x830:
    #
    #     +0x18  int32   manpower AVAILABLE from this state  (the UI's number)
    #     +0x1C  int32   population that cannot be recruited ("locked")
    #     +0x20  int32   population of the state
    #
    # Summing over the owned states reproduces the campaign autosave exactly:
    # HOL 23 857, BEL 9 542, LUX 4 432 (no drift, no army) and GER/FRA/ENG/SOV
    # within the recruitment that happened after the save was written.
    # `total` = sum of (population - locked) is the manpower before what the
    # army/air force/navy already uses; `used` is their difference.
    #
    # CCountry+0x328 is the embedded CCountryManpower; +0x10 is the recruitment
    # ratio in fixed(1e5) (GER 2.5%, LUX 1.5%) and cross-checks the total, but
    # its +0x20 counter is NOT the used manpower (GER 330 450 vs 323 550 here),
    # so it is not used for the arithmetic.
    MP_OBJ = 0x328                   # CCountryManpower inside CCountry
    MP_RATIO = 0x10                  # int64 fixed(1e5)
    SM_OBJ = 0x830                   # CStateManpower inside CState
    SM_AVAILABLE = 0x18              # int32
    SM_LOCKED = 0x1C                 # int32 unrecruitable part
    SM_TOTAL = 0x20                  # int32 population

    def _win_manpower_parts(self):
        """(available, total, used, population)."""
        p = self.game.p
        d = p.u64(self.ptr + 0x460)
        n = p.i32(self.ptr + 0x46C)
        population = recruitable = available = 0
        if d and 0 < n < 2000:
            for s in struct.unpack("<%dQ" % n, p.read(d, 8 * n)):
                try:
                    tot = p.i32(s + self.SM_OBJ + self.SM_TOTAL)
                    locked = p.i32(s + self.SM_OBJ + self.SM_LOCKED)
                    avail = p.i32(s + self.SM_OBJ + self.SM_AVAILABLE)
                except OSError:
                    continue
                population += tot
                recruitable += tot - locked
                available += avail
        return available, recruitable, recruitable - available, population

    def _win_manpower(self):
        return self._win_manpower_parts()[0]

    @property
    def civilian_factories(self) -> int:
        if sys.platform == "win32":
            return self._win_building_counts()[0]
        e = self.economy
        total = _sdiv(self.game.p.i64(e + O.EC_CIV_FACTORIES), O.FIXED)
        return total - (self.game.p.i32(e + O.EC_CIV_RESERVED_A)
                        + self.game.p.i32(e + O.EC_CIV_RESERVED_B))

    @property
    def total_factories(self) -> int:
        return self.civilian_factories + self.military_factories + self.naval_dockyards

    # ---- politics ----
    @property
    def political_power(self) -> int:
        return _sdiv(self.game.p.i64(self.politics + O.PO_POLITICAL_POWER), O.FIXED)

    # 2026-11-23 (复测 6 P1-2): 经 CanCreateUnitLeader (0x725236) 反汇编确认，
    # CCountry + 0x1F0 为指挥点数（Command Power，1e5 定点，GER 实测 12000000 -> 120.0）。
    WIN_CO_COMMAND_POWER = 0x1F0

    @property
    def command_power(self):
        if sys.platform == "win32":
            try:
                return round(self.game.p.i64(self.ptr + self.WIN_CO_COMMAND_POWER) / O.FIXED, 2)
            except Exception:
                return None
        return None

    @property
    def ruling_party_popularity(self) -> float:
        """Ruling party popularity as a 0..1 fraction, or None when unreadable.

        2026-11-20 (live): the old comment claiming "CPoliticalStatus+0xD0 is
        not a pointer on this build" was wrong.  `CCountry+CO_POLITICS+0xD0` is
        a valid `CPoliticalParty*` -- `legality.py` already dereferences the very
        same slot to read the ideology group -- and `party+0x88` is the
        popularity, fixed point 1e5*100 (GER reads 4_000_000 -> 0.40).  That is
        exactly the Linux formula, so both platforms agree.
        """
        try:
            sub = self.game.p.u64(self.politics + O.PO_RULING_SUB)
            if not sub:
                return None
            v = self.game.p.i64(sub + 0x88) / (O.FIXED * 100)
            return v if 0.0 <= v <= 1.0 else None
        except Exception:
            return None

    @property
    def stability_parts(self) -> dict:
        """stability split into its three terms, so the total is auditable."""
        base = self.stability_base
        mods = self._win_modifier_value("stability_factor") if sys.platform == "win32" else 0.0
        pop = self.ruling_party_popularity
        party = None if pop is None else round(pop * self.STABILITY_PARTY_COEF, 6)
        total = base + mods + (party or 0.0)
        return {"base": round(base, 6), "modifiers": round(mods, 6),
                "party_popularity": pop, "party_term": party,
                "total": round(total, 6)}

    @property
    def stability_base(self) -> float:
        return self.game.p.i64(self.ptr + O.CO_STABILITY_BASE) / O.FIXED

    @property
    def war_support_base(self) -> float:
        return self.game.p.i64(self.ptr + O.CO_WARSUPPORT_BASE) / O.FIXED

    # ---- by calling the game's own functions (exact values) ----
    #: Stability contribution of the ruling party's popularity.
    #:
    #: Derivation (2026-11-20, live 1.19.5.0 PE, GER at 1936.03.01):
    #:   engine `get_var stability`      = 0.81   (the engine's own answer)
    #:   base + stability_factor mods    = 0.75   (two direct reads)
    #:   ruling party popularity         = 0.40   (party+0x88)
    #:   => the missing term is +0.06, i.e. 0.15 x popularity.
    #: The same +6.00% was already recorded in this file from the in-game
    #: stability tooltip ("执政党支持率"), so two independent observations agree.
    #: Only ONE (popularity, total) data point exists on this save, so the
    #: coefficient is fitted, not proven -- `stability_parts` returns every term
    #: so a wrong coefficient is visible instead of silently baked in.
    STABILITY_PARTY_COEF = 0.15

    def stability(self) -> float:
        """Stability as the engine reports it: base + modifiers + party term."""
        if sys.platform == "win32":
            return self.stability_parts["total"]
        return self.stability_base

    #: common/defines/00_defines.lua: `WAR_SUPPORT_TENSION_IMPACT = 0.4`
    #: -- "Total impact of world tension", i.e. +40 points at 100% tension.
    #: Cross-checked against the in-game war-support tooltip (GER 1936.03):
    #:   base 30% + fleet glory 5% + tension 5.91% = 40.91%, panel shows 40%
    #:   => 5.91% / 0.4 = 14.775% world tension that day.
    WAR_SUPPORT_TENSION_IMPACT = 0.4

    def war_support(self) -> float:
        if sys.platform == "win32":
            return self.war_support_parts()["total"]
        return self.war_support_base

    def war_support_parts(self) -> dict:
        """Every term of the engine's war support, so a gap is visible.

        `tension` stays None until a live world-tension source is located
        (KNOWN_ISSUES P1 #16) -- that is a real missing term, not a zero.
        """
        base = self.war_support_base
        mods = self._win_modifier_value("war_support_factor")
        tension = None
        try:
            tension = self.game.world_tension()
        except Exception:
            tension = None
        term = None if tension is None else tension * self.WAR_SUPPORT_TENSION_IMPACT
        total = base + mods + (term or 0.0)
        return {"base": round(base, 6), "modifiers": round(mods, 6),
                "world_tension": None if tension is None else round(tension, 6),
                "tension_term": None if term is None else round(term, 6),
                "tension_known": tension is not None,
                "total": round(total, 6)}

    def manpower(self):
        if sys.platform == "win32":
            return self._win_manpower()
        # 1.19.4: 直读字段（引擎函数地址已失效）
        # +0x23F0 only behaves for the country whose UI is being drawn. On an AI
        # country the same slot can hold a heap pointer (measured HOL:
        # 0x5bf3df000000 = 1.01e14) which staff_army turned into a ten-billion
        # division manpower cap. Refuse anything that cannot be manpower.
        v = self.game.p.i64(self.ptr + O.CO_MANPOWER_CUR)
        if -1 <= v <= 500_000_000:
            return int(v)
        return None

    @property
    def manpower_total(self):
        """Manpower before subtracting what the army/air force/navy already use.

        Windows only: the Linux build keeps it behind an engine call that is not
        located yet, so `None` there means unknown -- not zero.
        """
        if sys.platform == "win32":
            return self._win_manpower_parts()[1]
        return None

    @property
    def population(self):
        """Sum of the owned states' population (Windows only, None elsewhere)."""
        if sys.platform == "win32":
            return self._win_manpower_parts()[3]
        return None

    # ---- identity ----
    @property
    def name(self) -> str:
        return self.game.p.stdstring(self.ptr + O.CO_NAME) or self.tag

    @property
    def adjective(self) -> str:
        return self.game.p.stdstring(self.ptr + O.CO_ADJECTIVE) or ""

    @property
    def is_player(self) -> bool:
        return self.game.player_tag_id() == self.tag_id

    # ---- other ----
    @property
    def nukes(self) -> int:
        if O.CO_NUKES is None:
            return 0
        if sys.platform == "win32":
            # 2026-11-20 (P1 #8): CCountry+0x1310 is CPdxArray<CNuke> with the
            # elements stored INLINE (stride 0x48) and the count as fixed(1e8)
            # at +0x18 of each element.  Measured live: the console command
            # `nukes 5` turned element[0].+0x18 from 0 into 500000000 and
            # changed nothing else in the country; writing 0 back restored the
            # count.  Every country has two CNuke objects (GER/BEL/FRA all 2),
            # which is why the old code -- reading the array's header as if it
            # were the object -- reported a constant 2 for everybody.
            p = self.game.p
            data = p.u64(self.ptr + O.CO_NUKES)
            size = p.i32(self.ptr + O.CO_NUKES + 12)
            if not data or not (0 <= size <= 64):
                return 0
            total = 0
            for i in range(size):
                total += p.i64(data + i * 0x48 + 0x18)
            return int(round(total / 100000000.0))
        o = self.game.p.u64(self.ptr + O.CO_NUKES)
        return _sdiv(self.game.p.i64(o + 0x18), O.FIXED) if o else 0

    @property
    def fuel(self) -> float:
        """Current fuel.

        2026-11-20: these fields are **Q15** fixed point (32768), not 1e5.  The
        old /O.FIXED made every fuel number 100000/32768 = 3.0518x too small:
        raw 11_562_965_200 / 32768 = 352_874.7, which is exactly what the engine
        reports (`get_var fuel_k` = 352.87).  The ratio stayed correct, which is
        why only absolute values were wrong.
        """
        if sys.platform == "win32":
            o = self.game.p.u64(self.ptr + O.CO_FUEL_STATUS)
            return self.game.p.i64(o + 0x08) / 32768.0 if o else 0.0
        o = self.game.p.u64(self.ptr + O.CO_FUEL)
        return self.game.p.i64(o + 0x8) / O.FIXED if o else 0.0

    @property
    def fuel_capacity(self):
        if sys.platform == "win32":
            o = self.game.p.u64(self.ptr + O.CO_FUEL_STATUS)
            return self.game.p.i64(o + 0x10) / 32768.0 if o else 0.0
        return None

    @property
    def state_count(self) -> int:
        return self.game.p.i32(self.ptr + O.CO_STATES + O.PDXARRAY_SIZE)

    def state_ptrs(self):
        base = self.game.p.u64(self.ptr + O.CO_STATES)
        n = self.state_count
        if not base or n <= 0:
            return []
        return list(struct.unpack("<%dQ" % n, self.game.p.read(base, 8 * n)))

    # ---- modifier'lar ----
    # Windows: CCountry+0x5D0 and +0x5E8 are CPdxArray<CModifier*>.  A CModifier
    # is a small polymorphic object whose +0x10 points at an array of
    # {int32 id; 4 pad; int64 value} pairs, count at +0x18 (value = fixed 1e5).
    # The Linux build keeps one flat sorted array at CO_MODIFIERS instead, which
    # is why the old code read pointer fragments here.
    #
    # Verified against the stability tooltip: the sum of `stability_factor` is
    # exactly the "帝国议会 +5.00%" line and `war_support_factor` gives GER
    # 0.30 + 0.05 = 0.35, the 35% on the top bar.
    CM_ARRAYS = (0x5D0, 0x5E8)
    CM_PAIRS = 0x10
    CM_CAPACITY = 0x18
    # 2026-11-19: +0x18 is the CAPACITY, +0x1C is the SIZE.  Reading `capacity`
    # entries walked past the live records into uninitialised heap and produced
    # the junk keys `modifier_1702213344`, `modifier_3211359`, `none: 5.6e12`
    # (those "values" were heap pointers divided by 1e5).  Measured live: a
    # CModifier with capacity 6 / size 5 had the 6th slot holding a freed
    # pointer.  Same CPdxArray convention as everywhere else.
    CM_SIZE = 0x1C
    CM_MAX_ID = 65536            # any id outside this is not a modifier id

    def _win_modifier_totals(self):
        p = self.game.p
        tot = {}
        for base in self.CM_ARRAYS:
            d = p.u64(self.ptr + base)
            n = p.i32(self.ptr + base + 0x0C)
            if not d or not (0 < n < 2000):
                continue
            ptrs_raw = p.try_read(d, 8 * n)
            if not ptrs_raw:
                continue
            for cm in struct.unpack("<%dQ" % (len(ptrs_raw) // 8), ptrs_raw):
                if not cm:
                    continue
                arr = p.u64(cm + self.CM_PAIRS)
                cnt = p.i32(cm + self.CM_SIZE)
                if not arr or not (0 < cnt < 1000):
                    continue
                raw = p.try_read(arr, 16 * cnt)
                if not raw:
                    continue
                for i in range(min(cnt, len(raw) // 16)):
                    mid, val = struct.unpack_from("<i4xq", raw, i * 16)
                    if not (0 <= mid < self.CM_MAX_ID):
                        continue
                    tot[mid] = tot.get(mid, 0) + val
        return tot

    def _win_modifier_value(self, name) -> float:
        mid = self.game.modifier_id(name)
        if mid is None:
            return 0.0
        return self._win_modifier_totals().get(mid, 0) / O.FIXED

    def modifier(self, name_or_id) -> float:
        mid = self.game.modifier_id(name_or_id) if isinstance(name_or_id, str) else int(name_or_id)
        if mid is None:
            raise KeyError("unknown modifier: %r" % (name_or_id,))
        if sys.platform == "win32":
            return self._win_modifier_totals().get(mid, 0) / O.FIXED
        return self.game.read_modifier(self.ptr + O.CO_MODIFIERS, mid) / O.FIXED

    def modifiers(self) -> dict:
        """Every modifier active on this country, as {name: value}."""
        if sys.platform == "win32":
            return {self.game.modifier_name(mid) or ("modifier_%d" % mid): v / O.FIXED
                    for mid, v in self._win_modifier_totals().items()}
        c = self.ptr + O.CO_MODIFIERS
        data = self.game.p.u64(c + O.PDXARRAY_DATA)
        n = self.game.p.i32(c + O.PDXARRAY_SIZE)
        if not data or n <= 0:
            return {}
        raw = self.game.p.read(data, 16 * n)
        out = {}
        for i in range(n):
            mid, val = struct.unpack_from("<i4xq", raw, i * 16)
            out[self.game.modifier_name(mid) or ("modifier_%d" % mid)] = val / O.FIXED
        return out

    # 2026-11-20 (P2 #10): "which national spirit / law gives me this modifier?"
    # Every active CIdea carries its own CPdxArray<CModifier*> at +0x90, and each
    # CModifier is a list of 16-byte (i32 modifier_id, i64 value) entries at
    # +0x10 with the count at +0x18.  Measured live on GER 1936:
    # partial_economic_mobilisation -> 5 entries (consumer_goods 0.25,
    # two -0.10 conversion costs, ...), sour_loser -> 1.  The country totals in
    # `modifiers()` are the sum of these plus tech/focus/occupation sources,
    # which this walk does NOT cover -- hence the explicit `unattributed` list.
    IDEA_MOD_OFF = 0x90
    CMOD_LIST = 0x10
    CMOD_COUNT = 0x18
    CMOD_STRIDE = 16

    def modifier_sources(self):
        """{modifier_name: [{"source": idea_key, "value": v}, ...]} (PE build)."""
        if sys.platform != "win32":
            return {}
        p = self.game.p
        out = {}
        try:
            # NB: `Country.politics` is the +0xF90 OFFSET, not a method — the
            # dict with the idea pointers comes from Game.politics(tag).
            rows = self.game.politics(self.tag).get("active_idea_ptrs") or []
        except Exception:
            return {}
        for row in rows:
            key, ptr = row.get("key"), row.get("ptr")
            if not key or not ptr:
                continue
            try:
                data = p.u64(ptr + self.IDEA_MOD_OFF)
                n = p.i32(ptr + self.IDEA_MOD_OFF + 12)
            except Exception:
                continue
            if not data or not (0 <= n <= 64):
                continue
            for i in range(n):
                try:
                    c = p.u64(data + i * 8)
                    if not c:
                        continue
                    arr = p.u64(c + self.CMOD_LIST)
                    cnt = p.i32(c + self.CMOD_COUNT)
                except Exception:
                    continue
                if not arr or not (0 <= cnt <= 512):
                    continue
                for j in range(cnt):
                    try:
                        mid = p.i32(arr + j * self.CMOD_STRIDE)
                        val = p.i64(arr + j * self.CMOD_STRIDE + 8)
                    except Exception:
                        break
                    nm = self.game.modifier_name(mid)
                    # Only ids the game can name are real modifiers; a CModifier
                    # whose list is laid out differently otherwise contributes
                    # ids like 6881356 with values around 1e11.
                    if not nm or not (0 < mid < 100000) or abs(val) > 10 ** 8:
                        continue
                    out.setdefault(nm, []).append(
                        {"source": key, "value": round(val / O.FIXED, 6)})
        return out

    # ---- experience: army / navy / air -------------------------------------
    # CCountry+0x1588 -> CCountryExperienceStatus (RTTI-verified on 1.19.5.0:
    # exactly 440 live objects, one per country, +0x08 back-points at its
    # CCountry).  The pools are Q15 fixed point -- 1.0 experience == 32768 --
    # which is why the engine's own conversion routine (img+0x2572DB0, used by
    # templedit._pe_army_xp) multiplies by exactly 100000/32768 = 3.0517578.
    #
    # +0x10 is ARMY and is cross-checked every time hoi4_template_edit runs:
    # 13694383/32768 = 417.92 XP, and the engine printed 417.92 for the same
    # read.
    #
    # 2026-11-17 (player screenshot cross-check): the top bar showed
    # 150 / 417 / 59 / 405  (command power / army / navy / air), which pins
    # +0x28 = NAVY (59.56) and +0x40 = AIR (405.02).  The first guess had them
    # the other way round; the fleet-size ordering settles it too -- SWI is
    # landlocked and reads exactly 0.00 at +0x28, while USA/JAP/ITA (the big
    # navies) read 213/99/92 there.  +0x18 reads ~0 for every country and is
    # not one of the three pools.
    XPS_OBJ = 0x1588
    XP_OFF = {"army": 0x10, "navy": 0x28, "air": 0x40}
    XP_Q15 = 32768.0

    @property
    def experience(self) -> dict:
        p = self.game.p
        out = {"army": None, "navy": None, "air": None}
        try:
            o = p.u64(self.ptr + self.XPS_OBJ)
            if not o:
                return out
            for k, off in self.XP_OFF.items():
                out[k] = round(p.u32(o + off) / self.XP_Q15, 2)
        except Exception:
            pass
        return out

    def summary(self) -> dict:
        def rnd(v, n=4):
            return round(v, n) if isinstance(v, (int, float)) else None
        if sys.platform == "win32":
            civ, mil, dock = self._win_building_counts()
            # 2026-11-19 (live): do NOT synthesise stability/war support from the
            # modifier table.  `_win_modifier_totals()` does not even contain
            # `stability_factor` (measured: 52 keys, none of them), so the old
            # `0.5 + modifier` formula silently answered 0.5 / 0.55 while the game
            # said 0.528 / 0.35.  The real getters are one read each and are cheap.
            stab_val, ws_val = self.stability(), self.war_support()
        else:
            civ, mil, dock = self.civilian_factories, self.military_factories, self.naval_dockyards
            stab_val, ws_val = self.stability(), self.war_support()
        core_civ = core_mil = core_dock = None
        if sys.platform == "win32":
            # 2026-11-23 (复测 6 P1-4): 通过 CState + 0xB0 真实核心国家 tag_id 数组
            # 精确统计本国核心州建筑等级；仅当读取本身全部失败时返回 None（不再拿失效
            # 字段 +0x2F0 凑出 1/1，也不把 0 核心州的流亡政府伪造成全量工厂）。
            core_civ, core_mil, core_dock = self._win_building_counts(core_only=True)
        return {
            "tag": self.tag,
            "name": self.name,
            "is_player": self.is_player,
            "experience": self.experience,
            "civilian_factories": civ,
            "military_factories": mil,
            "naval_dockyards": dock,
            # 2026-11-20 (live, user cross-check): the industry panel's totals are
            # core states PLUS the occupied (non-core) states weighted by
            # compliance.  GER 1936: core 35/28/10 + occupied 11/4/3 at 15.1%
            # compliance = 38/29/10, exactly what the UI and
            # `get_var num_of_*_factories` report, while the raw sum of every
            # built level is 46/32/13.  Both numbers are real, so report both
            # instead of pretending one of them is the other.
            "core_factories": {"civilian": core_civ, "military": core_mil,
                               "dockyards": core_dock},
            "factories_note": "civilian_factories/military_factories are RAW "
                              "built levels; the industry panel shows core + "
                              "occupied x compliance (see core_factories)",
            "political_power": self.political_power,
            "stability": rnd(stab_val),
            "war_support": rnd(ws_val),
            "manpower": self.manpower(),
            "fuel": rnd(self.fuel, 2),
            "nukes": self.nukes,
            "states": self.state_count,
            "ruling_party_popularity": rnd(self.ruling_party_popularity),
        }

    def __repr__(self):
        return "<Country %s @%#x>" % (self.tag, self.ptr)



# ---------------------------------------------------------------- CALL GUARD
# Calling a virtual whose signature was only guessed, with guessed arguments, CRASHED
# the game while hunting for the pause flag. The rule since: in a live game, only
# functions whose signature has been recovered statically may be called. Anything new
# gets disassembled first, then added here.
VERIFIED_CALLS = {
    # ---- only addresses re-derived for 1.19.4.1 (build f54d) ----
    0x510950,   # operator new(size_t)                      _Znwm@plt
    0x510260,   # operator delete(void*)                    _ZdlPv@plt
    0x2F212D0,  # CCommandQueue::Post(queue, cmd, force)
    0x20AE0A0,  # CCommand::Run(cmd, std::string* err)
    0x21928A0,  # CInGameIdler::GetCommandQueue()
    0x2046880,  # app->vt[0x90]: returns *(app+0x380) == queue
    0x23151C0,  # COrderGroupCommand ctor
    0x23167D0,  # CArmyGroupCommand ctor
    0x2332940,  # CSetArmyLeaderCommand ctor
    0x2325E10,  # COrderNewRootCommand ctor (front line: DISABLED)
    0x253A600,  # CMoveCommand ctor (route A)
    0x1CF6ED0,  # CSetNationalFocusCommand ctor (base-init reference)
    # 0xA4FB80 / 0xA563A0 were here: they are 1.19.2 addresses. In 1.19.4.1 they land
    # in the middle of unrelated functions and every call faulted (sig=11, rip on the
    # stack). Removed 2026-11-04; the diplomatic ctors are looked up in diploactions.pkl.
}


# ---------------------------------------------------------------- MULTIPLAYER GUARD
# An injected call ptrace-stops all 28 threads and hijacks the main one. In a running
# multiplayer game that causes a lockup, it froze a live session once, with the audio
# glitching. So when multiplayer is detected, injected calls are disabled.
# Reads (process_vm_readv) are unaffected, so all the analysis tools keep working.
_mp_cache = [None, 0.0]

# The ONLY calls allowed in multiplayer. None of them touches live game state:
#   operator new/delete  -> the allocator
#   CMoveCommand ctor    -> writes to fresh memory, reads the division's handle
#   CCommandQueue::Post  -> queues the command (the player's normal path)
# These were not what caused the freeze; calling a routine that TOUCHES GAME
# STATE was.
MP_SAFE_CALLS = {0x510950, 0x510260, 0x2F212D0, 0x20AE0A0, 0x253A600}


def multiplayer_detected(game, ttl=30.0):
    import time as _t
    if _mp_cache[0] is not None and _t.time() - _mp_cache[1] < ttl:
        return _mp_cache[0]
    try:
        import session
        _mp_cache[0] = bool(session.info(game).get("is_multiplayer"))
    except Exception:
        _mp_cache[0] = None          # unknown, so do not block
    _mp_cache[1] = _t.time()
    return _mp_cache[0]


_stats_loaded = False


def _load_stat_calls():
    """DISABLED 2026-11-04.

    `stats.pkl` was scanned on 2026-10-29 against the 1.19.2 build, so it holds 45
    trigger-function addresses that do not exist in 1.19.4.1. Whitelisting them let
    `g.wars() -> country_stats()` ptrace-call 45 wild addresses and smash the game
    stack (hoi4_wars killed the process within 10 s, every time).

    Do NOT re-enable until `research/stats.py` has been re-run against this binary
    and every recovered signature re-verified. Pure memory reads are unaffected.
    """
    global _stats_loaded
    _stats_loaded = True
    return


# Country-scope getters (f(this, scope) -> int64) that have been re-derived from
# THIS 1.19.4.1 binary and re-verified. Empty on purpose: the old `stats.pkl`
# table is a 1.19.2 artefact and calling any of its 45 addresses smashes the game
# stack. `Game._scope_call` refuses everything that is not listed here.
SCOPE_CALL_ALLOW = set()


def allow_call(addr):
    """Add a function to the verified-call list, once you are sure of its signature.

    The low-level Injector keeps its own whitelist (it sees absolute addresses), so
    every registration has to reach both layers.
    """
    VERIFIED_CALLS.add(addr)
    try:
        from process import backend as _be
        _be.allow_call(addr)
    except Exception:
        pass


class HOI4:
    def __init__(self, pid: Optional[int] = None):
        self.p = Proc(pid)
        self.read_only = False   # True means every injected call is refused
        self._inj: Optional[Injector] = None
        self._depth = 0
        self._tags = None
        self._tag2idx = None
        self._moddefs = None
        self._date_addr = None
        self._cmds = None
        self._dead_date_addrs = set()
        self._dead_date_pid = None

    # =============================================== core globals
    @property
    def base(self): return self.p.base

    def va(self, off): return self.p.va(off)

    @property
    def gamestate(self) -> int:
        gs = self.p.u64(self.va(O.G_GAMESTATE))
        if not gs:
            raise RuntimeError("no game loaded (gamestate is NULL), probably at the main menu)")
        return gs

    # =============================================== calling functions
    def __enter__(self):
        self.attach(); return self

    def __exit__(self, *a):
        self.detach()

    def attach(self):
        """Attach once when making several calls; much faster than attaching per call.

        2026-11-08 (run 20): every process that injects now takes an exclusive
        cross-process lock (xlock.FileLock: flock on POSIX, msvcrt on Windows)
        on hoi4_ptrace.lock first. The gateway runs a front_worker
        thread that calls `hoi4_tactical_front_tick` every 8 s, and the model (or a
        stage/ script) can attach at the same moment: two PTRACE_SEIZE +
        register rewrites on the same main thread garble RIP/RSP and kill the
        process. The kernel serialises them instead.
        """
        if self._depth == 0:
            from xlock import FileLock
            self._lock = FileLock()
            self._lock.acquire()
            try:
                self._inj = Injector(self.p)
                self._inj.attach()
            except Exception:
                try:
                    self._lock.release()
                except Exception:
                    pass
                self._lock = None
                raise
        self._depth += 1
        return self

    def detach(self):
        self._depth = max(0, self._depth - 1)
        if self._depth == 0:
            try:
                if self._inj:
                    self._inj.detach(); self._inj = None
            finally:
                lk = getattr(self, "_lock", None)
                if lk is not None:
                    try:
                        lk.release()
                    except Exception:
                        pass
                    self._lock = None

    def call(self, func_vaddr: int, args: Iterable[int] = (), signed=False,
             scratch_writer=None, absolute=False, force=False):
        """Call a function inside the game. func_vaddr is an ELF offset (absolute=False)."""
        if getattr(self, "read_only", False):
            raise CallError(
                "READ-ONLY mode is on: no injected function calls. This guard exists so "
                "a loop cannot accidentally make one, because a call stops every game "
                "thread. Set g.read_only=False to lift it.")
        if not force and func_vaddr not in MP_SAFE_CALLS and multiplayer_detected(self):
            raise CallError(
                "MULTIPLAYER session detected: injected calls are disabled. A call stops "
                "every game thread and hijacks the main one, which FREEZES a running "
                "multiplayer game. The read tools (analysis, reports, combat evaluation) "
                "keep working. If you really need it, pause the game and pass force=True. "
                "(Allowed in multiplayer: operator new/delete, the CMoveCommand "
                "constructor, and posting to the command queue.)")
        if not absolute and func_vaddr not in VERIFIED_CALLS and not force:
            _load_stat_calls()
        if not absolute and func_vaddr not in VERIFIED_CALLS and not force:
            raise CallError(
                "0x%X is not on the verified-call list. Calling a function whose signature "
                "is unknown, with guessed arguments, crashes the game. Recover the "
                "signature by disassembly first, then add it with api.allow_call(0x%X) "
                "(or pass force=True if you are knowingly taking the risk)."
                % (func_vaddr, func_vaddr))
        own = self._inj is None
        if own:
            self.attach()
        try:
            addr = func_vaddr if absolute else self.va(func_vaddr)
            r = self._inj.call(addr, tuple(args), scratch_writer=scratch_writer)
            return _s64(r) if signed else r
        finally:
            if own:
                self.detach()

    def malloc(self, size: int) -> int:
        """Scratch memory the game itself can read.

        On the PE build `O.FN_OPERATOR_NEW` is None (the CRT `operator new` was
        never located) and calling the CRT allocator from a hijacked thread takes
        the process down (cmdwin §25).  Everything that needs a scratch buffer --
        command ctors, `_i32buf`, error sinks -- therefore goes through the game's
        own `CPdxNewDeleteAllocator` singleton, which is what `cmdwin.alloc` wraps.
        """
        if sys.platform == "win32":
            import cmdwin
            return cmdwin.alloc(self, int(size))
        return self.call(O.FN_OPERATOR_NEW, (size,))

    def free(self, ptr: int):
        if sys.platform == "win32":
            import cmdwin
            return cmdwin.free(self, int(ptr))
        self.call(O.FN_OPERATOR_DELETE, (ptr,))

    # =============================================== console commands
    def console(self, command: str) -> ConsoleResult:
        """Run a console command, e.g. console('add_political_power 100')."""
        argv = command.split()
        if not argv:
            raise ValueError("empty command")
        return self.console_argv(argv)

    def console_argv(self, argv) -> ConsoleResult:
        """Run a console command.

        Two paths, in this order:

          1. `consolecmd.run` -- look the command up in the engine's own 370-entry
             command table and call its handler directly. No keyboard, no window
             manager, no X server, and it keeps working after a save reload.
          2. `xconsole.run`  -- the old typing path, kept as a fallback in case
             the table cannot be located (it needs matchbox + a focused window).
        """
        cmd = " ".join(str(a) for a in argv)
        try:
            import consolecmd
            ok, msg = consolecmd.run(self, cmd)
            return ConsoleResult(ok, msg, cmd)
        except Exception as e:
            direct_err = repr(e)[:200]
        try:
            import xconsole
            depth = getattr(self, "_depth", 0)
            for _ in range(depth):
                self.detach()
            try:
                info = xconsole.run(cmd)
            finally:
                for _ in range(depth):
                    self.attach()
            ok = bool(info.get("ok"))
            return ConsoleResult(ok, "" if ok else (info.get("note") or "typing failed")
                                 + " / direct: " + direct_err, cmd)
        except Exception as e:
            return ConsoleResult(False, "direct: %s / typing: %r" % (direct_err, e), cmd)

    # =============================================== console command catalogue
    def command_catalog(self) -> dict:
        """{name: {func, entry}} straight from the engine's console command table.

        2026-11-10: the old version dereferenced `G_CONSOLE_MANAGER = 0x4652018`,
        which in 1.19.4.1 points at float constants (0x3F800000C2B40000 = 1.0f /
        -90.0f), so it always raised OSError and `hoi4_debug_list_commands` never
        worked. The table is now found by scanning for its own first entry.
        """
        if self._cmds is not None:
            return self._cmds
        import consolecmd
        self._cmds = consolecmd.commands(self)
        return self._cmds

    # =============================================== countries
    def _load_tags(self):
        # 2026-11-06: the cache was permanent. After a save reload (new game PID,
        # re-ordered tag2index) `country("GER")` kept returning the OLD process's
        # CCountry pointer: research_slots read 0, country_stats returned {} and
        # 4 research slots sat empty for the rest of the run. Key the cache on PID.
        key = getattr(self, "_tags_pid", None)
        if self._tags is not None and key == self.p.pid:
            return
        gs = self.gamestate
        n = self.p.i32(gs + O.GS_TAG2INDEX + O.PDXARRAY_SIZE)
        if sys.platform == "win32":
            # 1.19.5.0: no image global holds the tag database -- it is an INLINE
            # CPdxArray<std::string> right after the tag2index array (0x340+0x18).
            # Same shape as Linux apart from the missing double dereference, and it
            # is the LIVE array, so it also carries the dynamic tags D01.. created by
            # releases and civil wars (size 443 == tag2index size 443).
            tidb = gs + O.GS_TAG_DB
            tarr = self.p.u64(tidb + O.PDXARRAY_DATA)
            n_db = self.p.i32(tidb + O.PDXARRAY_SIZE)
        else:
            tdb = self.p.u64(self.va(O.G_COUNTRYTAG_DB))
            tarr = self.p.u64(tdb + O.PDXARRAY_DATA)
            n_db = self.p.i32(tdb + O.PDXARRAY_SIZE)
        # NOTE: the tag2index array can be LONGER than the tag database (441 vs
        # 440). The extra slot is reserved for countries created mid-game, such as by
        # a civil war, and it has NO NAME in the static database. Reading n entries goes
        # out of bounds and returns None. player_tag() used to break right here.
        self._tags = [self.p.stdstring(tarr + i * O.STDSTRING_SZ)
                      if i < n_db else None for i in range(n)]
        tmap = self.p.u64(gs + O.GS_TAG2INDEX + O.PDXARRAY_DATA)
        self._tag2idx = struct.unpack("<%di" % n, self.p.read(tmap, 4 * n))
        self._tags_pid = self.p.pid

    def _live_country_idx(self, tid: int) -> int:
        """tag2index[tid] read live (4 bytes) so an annexation/reload never uses a
        stale row of the cached tuple."""
        gs = self.gamestate
        n = self.p.i32(gs + O.GS_TAG2INDEX + O.PDXARRAY_SIZE)
        if not (0 <= int(tid) < n):
            return -1
        tmap = self.p.u64(gs + O.GS_TAG2INDEX + O.PDXARRAY_DATA)
        return self.p.i32(tmap + 4 * int(tid))

    def country_ptrs(self):
        gs = self.gamestate
        arr = self.p.u64(gs + O.GS_COUNTRIES + O.PDXARRAY_DATA)
        n = self.p.i32(gs + O.GS_COUNTRIES + O.PDXARRAY_SIZE)
        return struct.unpack("<%dQ" % n, self.p.read(arr, 8 * n))

    def tags(self):
        self._load_tags()
        return [t for t in self._tags if t and t != "---"]

    def country(self, tag: str) -> Country:
        self._load_tags()
        tag = tag.upper()
        if tag not in self._tags:
            self._tags = None
            self._load_tags()
        if tag not in self._tags:
            raise KeyError("unknown tag: %s" % tag)
        tid = self._tags.index(tag)
        ptrs = self.country_ptrs()
        # 2026-11-06: use the cached tag2index VALUE. The live read was tried for a
        # few hours and returned a wrong row for GER once the countries array had
        # been reallocated by an annexation (divisions("GER") == 0 while the UI
        # showed 106). The cached value is a snapshot of the array's CONTENT, which
        # stays valid when the array itself is reallocated; the pid-keyed reload of
        # _load_tags above covers a game restart.
        ci = self._tag2idx[tid]
        if not (0 <= ci < len(ptrs)):
            self._tags = None
            self._load_tags()
            ci = self._tag2idx[tid]
        # 2026-11-16: never hand back a pointer that does not carry this tag id.
        # `CCountry+0x08` stores the country's own tag id in this build, so the
        # row can be checked instead of trusted.  When it is wrong (the 2026-11-16
        # symptom: divisions("GER") == 0 while the UI showed 109) rebuild the map
        # from the objects themselves.  Every caller -- divisions, armies, orders,
        # occupation -- goes through here, so this repairs all of them at once.
        if not self._country_index_ok(ci, tid, ptrs):
            alt = self._identity_country_index(tid, ptrs)
            if alt is not None:
                if alt != ci:
                    print("[country] tag2index row %r for %s is wrong; using "
                          "country %d instead" % (ci, tag, alt),
                          file=sys.stderr, flush=True)
                ci = alt
        return Country(self, tag, tid, ptrs[ci])

    def _country_index_ok(self, ci, tid, ptrs) -> bool:
        try:
            return bool(0 <= ci < len(ptrs)
                        and int(self.p.u32(ptrs[ci] + O.CO_TAG_ID)) == int(tid))
        except Exception:
            return False

    def _identity_country_index(self, tid, ptrs):
        """Index in countries[] whose CCountry carries tag id `tid`, or None."""
        for i, q in enumerate(ptrs):
            try:
                if int(self.p.u32(q + O.CO_TAG_ID)) == int(tid):
                    return i
            except Exception:
                continue
        return None

    def countries(self):
        self._load_tags()
        ptrs = self.country_ptrs()
        for tid, tag in enumerate(self._tags):
            if not tag or tag == "---":
                continue
            ci = self._tag2idx[tid]
            if not self._country_index_ok(ci, tid, ptrs):
                ci = self._identity_country_index(tid, ptrs)
                if ci is None:
                    continue
            yield Country(self, tag, tid, ptrs[ci])

    def country_by_ptr(self, ptr: int) -> Optional[Country]:
        self._load_tags()
        ptrs = self.country_ptrs()
        n_ptrs = len(ptrs)
        for tid, tag in enumerate(self._tags):
            if tid >= len(self._tag2idx):
                break
            ci = self._tag2idx[tid]
            if 0 <= ci < n_ptrs and ptrs[ci] == ptr:
                return Country(self, tag, tid, ptr)
        return None

    def player_tag_id(self) -> int:
        """The played country's TAG INDEX, in tag2index space.

        Linux 1.19.4.1: gamestate+0x518.  Windows 1.19.5.0: gamestate+0x520 (0x518
        reads 15 = SPR while playing GER; verified by switching tag: the field went
        1 -> 16 -> 1 for GER -> ITA -> GER).

        Do NOT use +0x51C as a fallback: it is not the player tag. While playing GER
        it read 440, which looked valid by coincidence and pointed at the wrong country.
        """
        return self.p.i32(self.gamestate + O.GS_PLAYER_TAG)

    def player_country_ptr(self) -> int:
        """CCountry* address of the played country.

        Chain: gamestate+0x518 (tag index) -> tag2index[] -> countries[].
        Countries created by a civil war have no name in the static tag database, so
        reading the name straight from `_tags[id]` does not work; go via the object.
        """
        self._load_tags()
        tid = self.player_tag_id()
        if not (0 <= tid < len(self._tag2idx)):
            raise RuntimeError("invalid player tag index: %r" % tid)
        ci = self._tag2idx[tid]
        ptrs = self.country_ptrs()
        if not (0 <= ci < len(ptrs)):
            raise RuntimeError("invalid player country index: %r" % ci)
        if not self._country_index_ok(ci, tid, ptrs):
            alt = self._identity_country_index(tid, ptrs)
            if alt is not None:
                ci = alt
        return ptrs[ci]

    def player_tag(self) -> str:
        """The played country's TAG. Raises rather than staying quiet if it cannot be
        mistaking another country for "us" and issuing orders for it is the worst
        possible outcome."""
        self._load_tags()
        tid = self.player_tag_id()
        cur = self._tags[tid] if 0 <= tid < len(self._tags) else None
        if cur and cur != "---":
            return cur
        # "---" is the empty country slot (tag index 0): the player's country has been
        # destroyed, or none is assigned yet. Mistaking that for a real country and
        # issuing orders for it would be a disaster, so we resolve via the object.
        c = self.country_by_ptr(self.player_country_ptr())
        if c is None or c.tag == "---":
            raise RuntimeError("could not determine the played country (tag index %r, name %r) "
                               "-- it may have been destroyed" % (tid, cur))
        return c.tag

    def player(self) -> Country:
        return self.country(self.player_tag())

    # =============================================== modifier'lar
    def _load_moddefs(self):
        if self._moddefs is not None:
            return
        tbl = self.p.u64(self.va(O.G_MODIFIER_DEFS))
        by_id, by_name, raw_key = {}, {}, {}
        i = 0
        misses = 0
        while i < 8192 and misses < 64:
            a = tbl + i * O.MODDEF_STRIDE
            nm = self.p.stdstring(a + O.MODDEF_NAME)
            got = self.p.try_read(a + O.MODDEF_ID, 4)
            if got is None or len(got) < 4:
                break
            mid = struct.unpack("<i", got)[0]
            if nm and nm.isascii() and len(nm) > 2 and mid == i:
                key = nm.lower()
                for pfx in ("modifier_", "mod_"):
                    if key.startswith(pfx):
                        key = key[len(pfx):]
                        break
                by_id[mid] = key
                by_name[key] = mid
                raw_key[mid] = nm
                misses = 0
            else:
                misses += 1
            i += 1
        self._moddefs = (by_id, by_name, raw_key)

    def modifier_id(self, name: str):
        self._load_moddefs()
        k = name.lower()
        for pfx in ("modifier_", "mod_"):
            if k.startswith(pfx):
                k = k[len(pfx):]
                break
        return self._moddefs[1].get(k)

    def modifier_name(self, mid: int):
        self._load_moddefs()
        return self._moddefs[0].get(mid)

    def modifier_loc_key(self, mid: int):
        self._load_moddefs()
        return self._moddefs[2].get(mid)

    def modifier_list(self):
        self._load_moddefs()
        return sorted(self._moddefs[1])

    def read_modifier(self, container: int, mid: int) -> int:
        """Binary search over CPdxArray<{int32 id; int64 val}>, as the game does it."""
        data = self.p.u64(container + O.PDXARRAY_DATA)
        n = self.p.i32(container + O.PDXARRAY_SIZE)
        if not data or n <= 0:
            return 0
        raw = self.p.read(data, 16 * n)
        lo, hi = 0, n
        while lo < hi:
            m = (lo + hi) // 2
            if struct.unpack_from("<i", raw, m * 16)[0] < mid:
                lo = m + 1
            else:
                hi = m
        if lo >= n or struct.unpack_from("<i", raw, lo * 16)[0] != mid:
            return 0
        return struct.unpack_from("<q", raw, lo * 16 + 8)[0]

    # =============================================== date
    def _scan_logs_date(self):
        """Latest [YYYY.MM.DD.hh] stamp found in the game's log files."""
        import re, glob as _glob
        best = None
        d = None
        try:
            d = os.path.join(os.path.dirname(self.p.path), "..", "logs")
        except Exception:
            d = None
        cands = []
        for pat in ("game.log", "error.log"):
            for root in (os.path.expanduser(
                    "~/.local/share/Paradox Interactive/Hearts of Iron IV/logs"),):
                cands.append(os.path.join(root, pat))
        for f in cands:
            try:
                with open(f, "r", errors="ignore") as _fh:
                    _fh.seek(0, os.SEEK_END)
                    _fh.seek(max(0, _fh.tell() - 200000))
                    txt = _fh.read()
            except Exception:
                continue
            for m in re.finditer(r"\[(\d{4})\.(\d{2})\.(\d{2})\.(\d{2})\]", txt):
                y, mo, dd, hh = map(int, m.groups())
                v = parts_to_date(y, mo, dd, hh)
                if best is None or v > best:
                    best = v
        return best

    def _date_sig_ok(self, addr):
        """True when `addr` holds the real CDate field triple.

        2026-11-08 (run 16): the ACTUAL 1.19.4.1 layout around the hour counter is

            -0x08 float  hour / 24.0        (0.208333 for 05:00)
            -0x04 int32  hour               (0..23)
            +0x00 int32  hour_counter = (year*365 + day_of_year)*24 + hour
            +0x04 int32  0

        The old test (`-8` is any float in 0.01..0.99 and `+4` is 0..23) also
        accepts the game's ring buffer of *historical* dates, which is how the
        clock froze 71 days in the past.
        """
        try:
            # one atomic read: the game ticks between separate reads and the
            # four fields then disagree, which made a live clock look invalid
            f, h, v, z = struct.unpack("<fiii", self.p.read(addr - 8, 16))
        except Exception:
            return False
        return (0 <= h <= 23 and z == 0 and (v % 24) == h
                and abs(f - h / 24.0) < (2e-2 if sys.platform == "win32" else 1e-4)
                and parts_to_date(1930, 1, 1) <= v <= parts_to_date(1948, 1, 1))

    def _date_from_log(self):
        """Last [YYYY.MM.DD.HH] stamp in the newest game.log -- the most reliable
        hint when several CDate triples survive (a 1936 campaign keeps the previous
        session's 1942 date in UI/save metadata, which otherwise wins by size)."""
        import glob as _g, re as _re, os as _os3
        best = None
        for pat in (os.path.expanduser("~/hoi4-*/logs/game.log"),
                    "/mnt/c/Users/*/Documents/Paradox Interactive/Hearts of Iron IV/logs/game.log"):
            for f in _g.glob(pat):
                try:
                    st = _os3.stat(f)
                except Exception:
                    continue
                if best is None or st.st_mtime > best[0]:
                    best = (st.st_mtime, f)
        if not best:
            return None
        try:
            with open(best[1], "rb") as fh:
                fh.seek(max(0, _os3.path.getsize(best[1]) - 200000))
                txt = fh.read().decode("utf-8", "replace")
        except Exception:
            return None
        hits = _re.findall(r"\[(\d{4})\.(\d{2})\.(\d{2})\.(\d{2})\]", txt)
        for y, m, d, h in reversed(hits):
            try:
                return parts_to_date(int(y), int(m), int(d), int(h))
            except Exception:
                continue
        return None

    def _find_date_exact(self, force=False):
        """Scan for the exact CDate signature and return the live clock address.

        Event dates in the game are all stored at 00:00, the live clock is not,
        so midnight candidates are only used when nothing else is left. When more
        than one candidate survives, the one that actually ticks wins.
        """
        import time as _t
        import numpy as np
        cache = getattr(self, "_date_exact_cache", None)
        # 2026-11-10 (review WO-004): the 30 s TTL meant every call older than 30 s
        # re-walked ~3.8 GB of writable memory and slept 2.5 s inside the gateway
        # lock. The address stays valid for the whole life of the process; a save
        # reload / backward jump still invalidates it via find_date_address(force=True).
        if not force and cache and cache[0] == self.p.pid and self._date_sig_ok(cache[2]):
            # 2026-11-19d: a signature check alone is not enough -- the
            # allocator can hand this address to a DIFFERENT CDate (an event
            # date, a bookmark) later in the session, and then the cache pins
            # the clock to a frozen value forever.  A live clock is always
            # within ~400 days of the game's own log stamp, so re-validate the
            # VALUE too and drop the cache otherwise.
            _cv = self.p.i32(cache[2])
            _cl = None
            try:
                _cl = self._log_date()
            except Exception:
                _cl = None
            if not _cl or (_cl - 24 * 400) <= _cv <= (_cl + 24 * 400):
                return cache[2]
            self._date_exact_cache = None
        lo, hi = parts_to_date(1930, 1, 1), parts_to_date(1948, 1, 1)
        regs = self.p.regions(writable=True)
        cands = None
        try:
            import fastmem as _fm
            cands = _fm.scan_i32_range(self.p, regs, lo, hi, max_hits=500_000, chunk_mb=4)
        except Exception:
            cands = None
        if cands is None:
            cands = []
            CH = 4 << 20
            buf = bytearray(CH)
            mv = memoryview(buf)
            for m in regs:
                a = m["start"]
                rem = a % 4
                if rem:
                    a += 4 - rem
                while a < m["end"]:
                    n = min(CH, m["end"] - a)
                    n -= n % 4
                    if n <= 0:
                        break
                    got = _read_chunk(self.p, a, buf, n)
                    got -= got % 4
                    if got >= 16:
                        arr = np.frombuffer(mv[:got], dtype=np.int32)
                        idx = np.nonzero((arr >= lo) & (arr <= hi))[0]
                        if idx.size:
                            cands.extend((a + idx.astype(np.int64) * 4).tolist())
                        del arr
                    a += n
                _malloc_trim()
        dead = getattr(self, "_dead_date_addrs", None) or ()
        good = [a for a in cands if a not in dead and self._date_sig_ok(a)]
        self._date_debug = {
            "n_cands": len(cands), "n_good": len(good),
            "good": [(hex(a), self.p.i32(a)) for a in good[:12]],
            "log_date": self._log_date(), "date_from_log": self._date_from_log(),
            "depth": getattr(self, "_depth", None),
        }
        if not good:
            return None
        _tick_empty = None
        if len(good) > 1 and getattr(self, "_depth", 0) == 0:
            # the live clock is the one that moves
            first = {a: self.p.i32(a) for a in good}
            # 2026-11-09: 0.4 s is shorter than one game hour at speed 4
            # (~2.5 s), so the tick test never fired and the scan fell back to
            # a stale CDate left in the event queue (read 1942.06.23 on a 1936
            # campaign). 2.5 s guarantees at least one hourly tick.
            # 2026-11-10 (review WO-004): only when we are NOT inside `with g:` —
            # an attached injector has every game thread stopped, so nothing can
            # tick and the sleep is pure added latency (and, when paused, always
            # fails).
            _t.sleep(2.5)
            ticked = [a for a in good if self.p.try_read(a, 4) and self.p.i32(a) != first[a]]
            _tick_empty = not ticked
            if ticked:
                good = ticked
        # ---- picking the live clock out of N signature-valid CDates ----------
        # Every candidate that is NOT the clock is a BOOKMARK or a SCHEDULED
        # date: a constant that never moves.  Consequences, and none of them is
        # a heuristic:
        #   (a) the clock can never be BEHIND the last game.log stamp (the log
        #       only lags, it cannot lead), and it can never be behind a value
        #       this very process already read (time does not run backwards
        #       inside one process), so both are FLOORS;
        #   (b) once this process has read the clock, its VALUE is known --
        #       match by value instead of re-guessing;
        #   (c) the clock is the SMALLEST candidate at/above the floor; every
        #       bookmark before the campaign start and every scheduled date is
        #       larger.
        # 2026-11-19d: the old tail did the opposite -- it threw away everything
        # ahead of the (stale) log, then took whatever was closest to that log,
        # else the LARGEST value (always a future bookmark).  Measured here: 145
        # signature-valid CDates, ~120 of them the same two 1945.03 values, live
        # clock 1938.10.05.21.
        logd = None
        try:
            logd = self._log_date()
        except Exception:
            logd = None
        self._date_debug["log_for_pick"] = hex(logd) if logd else None
        # 2026-11-21: `is_paused` reads idler+0x6C1 -- the byte the SPACE key
        # toggles and the only player-pause flag there is -- but the clock can be
        # frozen while that byte is 0 (an event popup or the Esc menu is a SECOND
        # pause source).  The tick test is direct evidence about the clock, so
        # when it ran its verdict wins BOTH ways; the byte is only a fallback.
        if _tick_empty is not None:
            _paused = bool(_tick_empty)
        else:
            try:
                import session as _sess_p
                _paused = bool(_sess_p.is_paused(self))
            except Exception:
                _paused = None
        self._date_debug["paused"] = _paused
        _pick, _why = pick_live_date([self.p.i32(a) for a in good], logd,
                                     getattr(self, "_date_high", None), _paused)
        self._date_debug["pick_reason"] = _why
        self._date_debug["pick_value"] = _pick
        if _pick is None:
            return None
        best = None
        _prev = getattr(self, "_win_date", None)
        if _prev is not None and self.p.try_read(_prev, 4) and self.p.i32(_prev) == _pick:
            best = _prev
        if best is None:
            for a in good:
                if self.p.i32(a) == _pick:
                    best = a
                    break
        if best is None:                    # the field moved between reads
            return None
        self._date_exact_cache = (self.p.pid, _t.time(), best)
        return best

    # ---------------------------------------------------------- Windows CDate
    # 1.19.5.0/MSVC keeps the same CDate triple as the ELF build:
    #   -0x08 float hour/24   -0x04 int32 hour   +0x00 int32 hour_counter   +0x04 0
    # The counter is (year*365 + day_of_year)*24 + hour, so it is bounded and
    # unique enough to scan for directly.  The Linux hint machinery is not used:
    # its log paths do not exist here.
    def _win_date_gs(self):
        """CDate candidates inside the `CCurrentGameState` object itself.

        BUG-20 in the 2026-11-15 playtest: the tool reported 1936.08.25 while the
        HUD said 1937.02.  The old scanner looked at *every* writable region and
        then had to guess which CDate copy was the live one; a copy left behind by
        an earlier save is byte-for-byte identical in shape, so the guess can land
        months in the past -- and a paused game gives the tick test nothing to see.

        The current date is a member of the game-state object, and that object is a
        single allocation we can walk.  Restricting the search to it removes the
        ambiguity instead of guessing at it.  If the object has no such field the
        list comes back empty and the caller falls back to the global scan.
        """
        out = []
        try:
            gs = self.gamestate
        except Exception:
            return out
        if not gs:
            return out
        for off in range(0, 0x2000, 4):
            a = gs + off
            try:
                if self._date_sig_ok(a):
                    v = self.p.i32(a)
                    if parts_to_date(1930, 1, 1) <= v <= parts_to_date(1970, 1, 1):
                        out.append(a)
            except Exception:
                break
        return out

    def _win_date_scan(self):
        import numpy as np
        lo, hi = parts_to_date(1930, 1, 1), parts_to_date(1970, 1, 1)
        regs = self.p.regions(writable=True, min_size=4096)
        try:
            import fastmem as _fm
            fast_addrs = _fm.scan_i32_range(self.p, regs, lo, hi, max_hits=500_000, chunk_mb=4)
            if fast_addrs is not None:
                return [ad for ad in fast_addrs if self._date_sig_ok(ad)]
        except Exception:
            pass
        hits = []
        CH = 8 << 20
        buf = bytearray(CH)
        mv = memoryview(buf)
        for m in regs:
            a = m["start"]
            rem = a % 4
            if rem:
                a += 4 - rem
            while a < m["end"]:
                n = min(CH, m["end"] - a)
                n -= n % 4
                if n <= 0:
                    break
                got = _read_chunk(self.p, a, buf, n)
                got -= got % 4
                if got >= 16:
                    i32 = np.frombuffer(mv[:got], dtype=np.int32)
                    idx = np.nonzero((i32 >= lo) & (i32 <= hi))[0]
                    if idx.size:
                        for ad in (a + idx.astype(np.int64) * 4).tolist():
                            if self._date_sig_ok(ad):
                                hits.append(ad)
                    del i32
                a += n
        return hits

    def _win_date_addr(self, force=False):
        """Pick the live CDate: the exact signature scan, then the old heuristics.

        2026-11-19 (live bug): `hoi4_date` reported 1936.02.17.17 while the HUD
        said 1936.07.16.  A scan of the address space found 29 signature-valid
        CDates in that campaign -- bookmarks at 1930.03.04 / 1931.07.05 /
        1935.08.14 / 1935.12.07, scheduled dates at 1937.09.17 / 1941.10.13 /
        1945.03.01 / 1945.03.17, and exactly ONE at a non-midnight hour:
        1936.07.16.09.  The old selection sorted by |value - game.log| and the
        log stamp was five months stale, so the closest candidate was a
        1935.12.07 bookmark; the midnight filter further down never ran because
        the log hint had already collapsed the list.

        `_find_date_exact()` (exact CDate triple + ticking test + "the live
        clock is the only candidate at a non-midnight hour") returns the right
        address; the Windows path simply was not calling it.  It is cached per
        pid, so the 2.5 s tick probe is paid once.
        """
        # 2026-11-21 (P0-1): `_date_debug["pick_reason"]` is written ONLY by the
        # heuristic picker further down.  It used to survive from one call to the
        # next, so a number produced by the EXACT scan inherited the previous
        # call's "first>log" label and vice versa -- `date()` then had no way to
        # tell which path had produced the value it was about to stamp "clock"
        # (observed: source "clock", pick "first>log", value 1945.03.01.00, which
        # the docstring below already lists as a SCHEDULED DATE constant).
        if getattr(self, "_date_debug", None) is None:
            self._date_debug = {}
        self._date_debug.pop("pick_reason", None)
        self._date_debug.pop("pick_value", None)
        if force:
            self._date_exact_cache = None
            self._win_date = None
            self._win_date_pid = None
        # 2026-11-21 (P0-2 follow-up): try the CHEAP game-state scan FIRST.
        # `_find_date_exact` walks all 3.8 GB of writable memory (30-80 s on this
        # machine) and it used to run before this branch, which is exactly why the
        # first tool call after loading a campaign timed out.  The live clock lives
        # inside CCurrentGameState; one unambiguous signature-valid candidate there
        # is enough, and the global scan stays as the fallback.
        try:
            _gs = self._win_date_gs()
        except Exception:
            _gs = []
        if len(_gs) == 1:
            try:
                _gv = self.p.i32(_gs[0])
            except Exception:
                _gv = None
            if (_gv is not None
                    and parts_to_date(1935, 1, 1) <= _gv <= parts_to_date(1965, 1, 1)
                    and date_plausible(_gv, self._log_date())):
                self._win_date = _gs[0]
                self._win_date_pid = self.p.pid
                self._win_date_last = _gv
                self._date_verified = True
                self._date_debug["gs_single"] = hex(_gv)
                return _gs[0]
        # 2026-11-21: reuse the address a PREVIOUS server process found for this
        # same pid BEFORE paying for a 3.8 GB scan.  date_addr.json is written
        # every time a scan succeeds, and a server restart is the common case
        # (every mcp_load).  Without this, the first tool call after each restart
        # paid 11-22 s and hoi4_session_info blew its 50 s budget.
        if not force:
            try:
                import json as _j, os as _o
                _df = _o.path.join(_o.path.dirname(_o.path.abspath(__file__)),
                                   "date_addr.json")
                with open(_df) as _fh:
                    _c = _j.load(_fh)
                if int(_c.get("pid") or 0) == self.p.pid and _c.get("addr"):
                    _ad = (int(_c["addr"], 16) if isinstance(_c["addr"], str)
                           else int(_c["addr"]))
                    _v = self.p.i32(_ad)
                    if (parts_to_date(1935, 1, 1) <= _v <= parts_to_date(1965, 1, 1)
                            and date_plausible(_v, self._log_date())):
                        self._win_date = _ad
                        self._win_date_pid = self.p.pid
                        self._win_date_last = _v
                        self._date_verified = True
                        self._date_debug["disk_cache"] = hex(_v)
                        return _ad
            except Exception:
                pass
        try:
            exact = self._find_date_exact(force=force)
        except Exception:
            exact = None
        if exact:
            try:
                v = self.p.i32(exact)
            except Exception:
                v = None
            if v is not None and parts_to_date(1935, 1, 1) <= v <= parts_to_date(1965, 1, 1):
                self._win_date = exact
                self._win_date_pid = self.p.pid
                self._win_date_last = v
                self._date_verified = True
                return exact
        import time as _t
        cur = getattr(self, "_win_date", None)
        if cur and getattr(self, "_win_date_pid", None) == self.p.pid:
            try:
                v = self.p.i32(cur)
                last = getattr(self, "_win_date_last", None)
                if (last is not None
                        and parts_to_date(1935, 1, 1) <= v <= parts_to_date(1965, 1, 1)
                        and 0 <= v - last <= 24 * 400):
                    lrv = None
                    try:
                        lrv = self._log_date()
                    except Exception:
                        lrv = None
                    if lrv and v > lrv + 24 * 550:
                        # stale copy that a previous Game() cached: rescan
                        self._win_date = None
                        self._win_date_pid = None
                        raise ValueError("date cache ahead of the log")
                    if lrv and lrv - v > 24 * 550:
                        # BUG-20: the cached copy is months BEHIND the game's own
                        # log stamp, so it cannot be the live clock.  Drop it and
                        # rescan (the game-state scan now usually finds the real
                        # one directly).
                        self._win_date = None
                        self._win_date_pid = None
                        raise ValueError("date cache behind the log")
                    self._win_date_last = v
                    return cur
            except Exception:
                pass
        if getattr(self, "_win_date", None) is None:
            gs_hits = self._win_date_gs()
            if len(gs_hits) == 1:
                # One unambiguous clock inside the game-state object: use it and
                # never consult the global scan or the cross-process cache.
                self._win_date = gs_hits[0]
                self._win_date_pid = self.p.pid
                self._win_date_last = self.p.i32(gs_hits[0])
                return gs_hits[0]
            try:
                import json as _j, os as _o
                with open(_o.path.join(_o.path.dirname(_o.path.abspath(__file__)), "date_addr.json")) as _dfh:
                    c = _j.load(_dfh)
                if int(c.get("pid") or 0) == self.p.pid and c.get("addr"):
                    ad = int(c["addr"], 16) if isinstance(c["addr"], str) else int(c["addr"])
                    v0 = self.p.i32(ad)
                    if parts_to_date(1935, 1, 1) <= v0 <= parts_to_date(1965, 1, 1):
                        self._win_date = ad
                        self._win_date_pid = self.p.pid
                        self._win_date_last = v0
                        return ad
            except Exception:
                pass
        # 2026-11-21: if `_find_date_exact` already walked the address space this
        # call and produced no usable address, do NOT walk it again with the
        # heuristic scanner -- that is 2 x 3.8 GB per read and it blew
        # hoi4_session_info's 50 s watchdog on a fresh campaign.  Returning None
        # makes date_raw fall back to the game.log lower bound (cheap, and the
        # log is written FROM the clock so it cannot lead it).
        if (getattr(self, "_date_debug", None) or {}).get("n_good") is not None:
            return None
        hits = self._win_date_gs()
        if not hits:
            hits = self._win_date_scan()
        if not hits:
            self._win_date = None
            return None
        last = getattr(self, "_win_date_last", None)
        # A running game advances the real CDate once per day, and stale copies
        # from earlier saves do not move.  Whatever ticked wins, unconditionally:
        # preferring "newest" or "closest to last" both picked a 1945 remnant over
        # the live 1936 date.  Only fall back to the heuristic when the game is
        # paused and nothing moved at all.
        if len(hits) > 1:
            first = {a: self.p.i32(a) for a in hits}
            _t.sleep(1.2)
            ticked = [a for a in hits
                      if self.p.try_read(a, 4) and self.p.i32(a) != first[a]]
            if ticked:
                hits = ticked
                last = None
        # Belt and braces for the paused case: keep only plausible campaign dates.
        plausible = [a for a in hits
                     if parts_to_date(1935, 1, 1) <= self.p.i32(a) <= parts_to_date(1965, 1, 1)]
        if plausible:
            hits = plausible
        if last:
            hits.sort(key=lambda a: abs(self.p.i32(a) - last))
        elif len(hits) > 1:
            # Paused: nothing ticked, so the "largest value" rule below used to
            # hand back a frozen 1945 CDate inside a 1936 campaign (seen every
            # time a fresh Game() was built while the game sat paused). The
            # game's own log stamp is a lower bound that can lag the clock but
            # can never be ahead of it, so prefer the candidate closest to it
            # and throw away anything more than 45 days ahead of it.
            lrv = None
            try:
                lrv = self._log_date()
            except Exception:
                lrv = None
            if lrv:
                # 2026-11-19: the log is a FLOOR, not a target.  A stale-looking
                # log (this campaign's was five months behind) made "closest to
                # the log" pick a bookmark that sat *before* it, and the midnight
                # filter never ran because the list had already collapsed to one.
                # Keep every copy that is not older than the log (a log stamp can
                # lag the clock, never lead it) and take the SMALLEST of those:
                # the live clock is the first candidate at/after the floor, all
                # later ones are scheduled/bookmark dates.
                above = [a for a in hits if self.p.i32(a) >= lrv - 24]
                if above:
                    hits = above
                    hits.sort(key=lambda a: self.p.i32(a))
                else:
                    hits.sort(key=lambda a: -self.p.i32(a))
            else:
                midday = [a for a in hits if self.p.i32(a) % 24]
                if midday:
                    hits = midday
                hits.sort(key=lambda a: -self.p.i32(a))
        self._win_date = hits[0]
        self._win_date_pid = self.p.pid
        self._win_date_last = self.p.i32(hits[0])
        return hits[0]

    def find_date_address(self, force=False) -> int:
        if sys.platform == "win32":
            a = self._win_date_addr(force=force)
            # 2026-11-21 (续测 P0-1): `_win_date_addr` located a perfectly good
            # address (verified=True, value 16978179) but never stored it in
            # `_date_addr`, so `_scan_date` saw "no address" on every later call,
            # refused to scan (rate limit) and answered from the game.log stamp --
            # i.e. the clock FROZE at the last log line while the game ran on.
            # Every path that finds an address must publish it here.
            if a:
                self._date_addr = a
            return a
        """Locate the game date. 1.19.4 layout of a CDate object:

            +0x00 ptr, +0x08 float (0..1, day fraction), +0x0C int32 hour,
            +0x10 int32 hour_counter = (year*365 + day_of_year)*24 + hour

        So: scan writable memory around the date in game.log, keep int32 values
        whose -8 dword is a float in (0,1) and whose +4 dword is an hour 0..23,
        then take the largest (= the live clock, not a cached copy).
        """
        import json as _json, numpy as np
        # 2026-11-06 (run 6): a CDate copy that is permanently frozen must never
        # come back. The old fallback re-sorted by distance to game.log on every
        # rescan and re-picked the same dead copy, so a paused-clock read stayed
        # at 1939.05 for 40 real minutes while the game was at 1939.09.
        if getattr(self, "_dead_date_pid", None) != self.p.pid:
            self._dead_date_addrs = set()
            self._dead_date_pid = self.p.pid
        dead = self._dead_date_addrs
        if len(dead) > 200:
            dead.clear()
        cache_f = os.path.join(os.path.dirname(os.path.abspath(__file__)), "date_addr.json")
        # 2026-11-08 (run 16): try the EXACT CDate signature first. The heuristic
        # below picks by distance to the game.log stamp, and a stalled log pinned
        # the clock 71 days in the past inside the game's ring buffer of
        # historical dates. This path is exact and self-verifying.
        self._date_verified = False
        try:
            exact = self._find_date_exact()
        except Exception:
            exact = None
        if exact:
            self._date_verified = True
            self._date_addr = exact
            self._date_last = self.p.i32(exact)
            self._date_static = (self._date_last, time.time())
            try:
                _tmp = f"{cache_f}.{os.getpid()}.tmp"
                with open(_tmp, "w") as fh:
                    _json.dump({"pid": self.p.pid, "base": self.p.base,
                                "addr": exact, "last": self._date_last}, fh)
                os.replace(_tmp, cache_f)
            except Exception:
                pass
            return exact
        if not self._date_addr and not force:
            try:
                with open(cache_f) as fh:
                    c = _json.load(fh)
                if c.get("pid") == self.p.pid and c.get("base") == self.p.base:
                    if c["addr"] not in dead:
                        self._date_addr = c["addr"]
                        if c.get("last"):
                            self._date_last = c["last"]
            except Exception:
                pass
        if not force and time.time() < getattr(self, "_date_retry_at", 0):
            # A scan that found nothing ticking just ran; do not walk 3.8 GB again
            # until the cooldown expires (the caller falls back to the last value).
            return 0
        if self._date_addr and not force and self._date_addr not in dead:
            v = self.p.try_read(self._date_addr, 4)
            if v:
                cur = struct.unpack("<i", v)[0]
                if parts_to_date(1900, 1, 1) <= cur <= parts_to_date(2100, 1, 1):
                    # The date address must be validated against the game's own
                    # game.log stamp: the game keeps several stale copies of the
                    # date around (a loaded save, a world-news stamp, the autosave
                    # header) and a stale copy reads as a perfectly valid date.
                    #
                    # The window has to be WIDE enough that a lagging log does not
                    # force a rescan: the rescan reads the whole 3.8 GB address
                    # space and costs 5.9 GB of peak memory, which is what used
                    # to OOM-kill the MCP child. A 30-day window accepted stale
                    # copies (the tool reported 1936.12.13 while the game was at
                    # 1936.08.18), so the stale copy is rejected by a different
                    # test: it must ADVANCE while we watch it. A stale copy never
                    # moves.
                    log_ref = self._log_date()
                    prev = getattr(self, "_date_last", None)
                    if prev is not None and cur < prev:
                        # time went backwards -> stale copy, rescan
                        dead.add(self._date_addr)
                        self._date_addr = None
                    elif log_ref is not None and abs(cur - log_ref) > 24 * 45:
                        # far away from the game's own stamp -> wrong address
                        dead.add(self._date_addr)
                        self._date_addr = None
                    else:
                        # The cached address can still be a stale copy that happens
                        # to sit within 45 days of a lagging game.log (seen on a
                        # fresh campaign: the tool froze on 1935.12.26 while the
                        # clock showed 1936.01.30). A live clock advances; when the
                        # value has not moved for 3 s and the game is NOT paused,
                        # drop the address and do the ticking scan.
                        import time as _t
                        seen = getattr(self, "_date_static", None)
                        shut = False
                        if getattr(self, "_date_unverified", False):
                            # best-effort address from the last fallback: cheap reads
                            # only, the log clamp + monotonic guard police the value
                            self._date_last = cur
                            return self._date_addr
                        if seen is not None and cur == seen[0] and _t.time() - seen[1] > 3.0:
                            try:
                                import session as _sess
                                shut = bool(_sess.is_paused(self))
                            except Exception:
                                shut = True
                            if not shut:
                                # Run 8: before declaring the address dead, close
                                # any popup that is holding the clock and give the
                                # game 0.3 s to move again.
                                try:
                                    self.dismiss_popups_and_unpause()
                                except Exception:
                                    pass
                                _t.sleep(0.3)
                                _mv = self.p.try_read(self._date_addr, 4)
                                if _mv and struct.unpack("<i", _mv)[0] != cur:
                                    _nv = struct.unpack("<i", _mv)[0]
                                    self._date_last = _nv
                                    self._date_static = (_nv, _t.time())
                                    return self._date_addr
                                dead.add(self._date_addr)
                                self._date_addr = None
                        if self._date_addr:
                            self._date_static = ((cur, _t.time()) if seen is None or cur != seen[0]
                                                 else seen)
                            self._date_last = cur
                            return self._date_addr
        ref = self._log_date() or self._scan_logs_date()
        if ref:
            # game.log can lag behind the live clock (world-news popups, autosaves,
            # or a freshly loaded campaign). A +/-10 day window then finds no live
            # clock at all, so keep a wider window around the last log stamp.
            LO, HI = ref - 24 * 120, ref + 24 * 120
        else:
            LO, HI = parts_to_date(1930, 1, 1), parts_to_date(1995, 1, 1)
        cands = []
        CH = 4 << 20
        buf = bytearray(CH)
        mv = memoryview(buf)
        for m in self.p.regions(writable=True):
            a = m["start"]
            rem = a % 4
            if rem:
                a += 4 - rem
            while a < m["end"]:
                n = min(CH, m["end"] - a)
                n -= n % 4
                if n <= 0:
                    break
                got = _read_chunk(self.p, a, buf, n)
                got -= got % 4
                if got >= 4:
                    arr = np.frombuffer(mv[:got], dtype=np.int32)
                    idx = np.nonzero((arr >= LO) & (arr <= HI))[0]
                    if idx.size:
                        vals = arr[idx].tolist()
                        addrs = (a + idx.astype(np.int64) * 4).tolist()
                        cands.extend(zip(vals, addrs))
                    # Keep only the candidates nearest to the log stamp. The old
                    # code collected every match in a Python list, which is what
                    # turned this scan into a 5.9 GB allocation.
                    if len(cands) > 4000:
                        if ref is not None:
                            cands.sort(key=lambda kv: abs(kv[0] - ref))
                        cands = cands[:2000]
                    del arr
                a += n
            # Linux 下归还空闲堆页；Windows 上直接 no-op
            _malloc_trim()
        if not cands:
            raise RuntimeError("could not find the game date (is a game loaded?)")
        good = []
        for v, a in cands:
            if a in dead:
                continue
            m8 = self.p.try_read(a - 8, 4)
            p4 = self.p.try_read(a + 4, 4)
            if not m8 or not p4:
                continue
            f = struct.unpack("<f", m8)[0]
            h = struct.unpack("<i", p4)[0]
            if 0.01 < f < 0.99 and 0 <= h <= 23:
                good.append((v, a))
        if good:
            # The live clock is the copy that actually ticks. The game's own
            # game.log stamp is the reference: pick the ticking candidate that is
            # CLOSEST to it.
            #
            # The old code took the LARGEST value ("the live clock is the biggest
            # copy"), which picked a stale FUTURE copy: the tool reported
            # 1937.02.28 while the game was at 1936.08.18. Distance to the log
            # stamp is the reliable criterion.
            import time as _t
            ticking = []
            # Test in ONE detached batch. Inside `with g:` the injector has
            # stopped every game thread, so NO candidate can tick; the old loop
            # slept 0.9 s per candidate, concluded "nothing ticks", and the
            # fallback below then re-picked the same dead copy from `good[0]`.
            # That is exactly the 1939.05 freeze. Detach for 0.55 s, sample 60
            # candidates three times, re-attach.
            sample = [a for _v, a in good[:60] if a not in dead]
            v1 = v2 = v3 = {}
            # 2026-11-06 17:53: the detach-and-re-attach version of this batch
            # test SIGSEGV'd the game (it pokes at the injector from a thread that
            # does not own the attach session). Only sample while we hold NO
            # injector; inside `with g:` the game threads are stopped anyway, so
            # fall through to the log-nearest candidate and let the next
            # un-attached read verify it.
            if sample and int(getattr(self, "_depth", 0) or 0) == 0:
                v1 = {a: self.p.try_read(a, 4) for a in sample}
                _t.sleep(0.25)
                v2 = {a: self.p.try_read(a, 4) for a in sample}
                _t.sleep(0.25)
                v3 = {a: self.p.try_read(a, 4) for a in sample}
            for _a in sample:
                s1, s2, s3 = v1.get(_a), v2.get(_a), v3.get(_a)
                if not (s1 and s2 and s3):
                    continue
                a1, a2, a3 = (struct.unpack("<i", x)[0] for x in (s1, s2, s3))
                if a1 <= a2 <= a3 and a3 > a1 and (a3 - a1) <= 72:
                    ticking.append((a3, _a))
            if ticking:
                # Among TICKING copies there is normally exactly one (the live
                # clock); if several, the furthest one is the present, since a
                # stale copy can never advance past it.
                ticking.sort(reverse=True)
                self._date_addr = ticking[0][1]
                self._date_last = ticking[0][0]
                self._date_unverified = False
                self._save_date_cache()
                return self._date_addr
            # Nothing ticked right now (the game is paused, or the injector stopped
            # the game threads for a housekeep while we scanned). Fall back to the
            # copy nearest game.log, but flag it as unverified: date_raw then applies
            # the 2-day log clamp and the monotonic guard, so a stale copy can be
            # rejected without paying for another 3.8 GB scan on every call.
            # 2026-11-11: "nearest to the log" is wrong when the game is paused:
            # the log lags the clock by however long nothing happened, so the
            # nearest copy can be an older stale one. The clock is NEVER behind
            # the log, so restrict to copies in [log, log+60d] and take the
            # largest; that skips both the stale past copy and the frozen 1945
            # copy. Only without such a candidate fall back to nearest.
            pick = None
            if ref is not None:
                window = [kv for kv in good if ref <= kv[0] <= ref + 24 * 60]
                if window:
                    window.sort(reverse=True)
                    pick = window[0]
                else:
                    good.sort(key=lambda kv: abs(kv[0] - ref))
                    pick = good[0]
            else:
                good.sort(reverse=True)
                pick = good[0]
            self._date_addr = pick[1]
            self._date_unverified = True
            self._save_date_cache()
            return self._date_addr
        live = [(v, a) for v, a in cands if a not in dead] or cands
        best = min(live, key=lambda kv: abs(kv[0] - (ref or kv[0])))
        self._date_addr = best[1]
        self._save_date_cache()
        return best[1]

    def _save_date_cache(self):
        import json as _json
        f = os.path.join(os.path.dirname(os.path.abspath(__file__)), "date_addr.json")
        try:
            _tmp = f"{f}.{os.getpid()}.tmp"
            with open(_tmp, "w") as fh:
                _json.dump({"pid": self.p.pid, "base": self.p.base,
                            "addr": self._date_addr,
                            "last": getattr(self, "_date_last", None)}, fh)
            os.replace(_tmp, f)
        except OSError:
            pass

    # 2026-11-11: the stock Paradox log directory is NOT where this stack writes:
    # the game may run with -userdir=<somewhere>, so its logs live in
    # <userdir>/logs. Reading the default directory picked up files from an old
    # 1942 campaign and date_raw() clamped every fresh 1936 campaign to
    # 1942.06.23 (the "date stuck at 1942" bug). Resolve the userdir from the
    # live process cmdline; HOI4_GAME_LOG / HOI4_ERROR_LOG still win.
    @staticmethod
    def _userdir_log_dir():
        try:
            import subprocess
            cmd = subprocess.run(["ps", "-p", str(os.getpid()), "-o", "cmd="],
                                 capture_output=True, text=True, timeout=3).stdout
        except Exception:
            cmd = ""
        return cmd

    def _log_paths(self):
        env_g = os.environ.get("HOI4_GAME_LOG")
        env_e = os.environ.get("HOI4_ERROR_LOG")
        if env_g and env_e:
            return env_g, env_e
        ud = getattr(self, "_hoi4_userdir", None)
        if ud is None:
            ud = ""
            try:
                cmd = open("/proc/%d/cmdline" % self.p.pid, "rb").read().split(b"\0")
                for tok in cmd:
                    t = tok.decode("utf-8", "replace")
                    if t.startswith("-userdir="):
                        ud = t.split("=", 1)[1]
                        break
            except Exception:
                ud = ""
            self._hoi4_userdir = ud
        if ud:
            base = os.path.join(ud, "logs")
            return (env_g or os.path.join(base, "game.log"),
                    env_e or os.path.join(base, "error.log"))
        try:
            import config as _cfg
            d = os.path.join(_cfg.user_dir(), "logs")
        except Exception:
            d = os.path.expanduser(
                "~/.local/share/Paradox Interactive/Hearts of Iron IV/logs")
        return env_g or os.path.join(d, "game.log"), env_e or os.path.join(d, "error.log")

    LOG_PATH = os.environ.get(
        "HOI4_GAME_LOG",
        os.path.expanduser("~/.local/share/Paradox Interactive/Hearts of Iron IV/logs/game.log"))

    ERROR_LOG_PATH = os.environ.get(
        "HOI4_ERROR_LOG",
        os.path.expanduser("~/.local/share/Paradox Interactive/Hearts of Iron IV/logs/error.log"))

    def _log_date(self):
        """The last [YYYY.MM.DD.HH] stamp in game.log / error.log, as a Clausewitz hour.

        2026-11-06 (run 7): game.log only gets a line when an event fires, so it
        can sit a month behind the clock and every log-based sanity check then
        accepted a stale CDate copy (the tool reported 1936.03.05 while the game
        was at 1936.04.08). error.log carries an hourly decision warning in this
        mod, so the LAST stamp of either file is an accurate lower bound for the
        live clock — and neither file can ever be ahead of it.
        """
        best = None
        for path in self._log_paths():
            try:
                with open(path, "r", errors="replace") as f:
                    f.seek(0, os.SEEK_END)
                    f.seek(max(0, f.tell() - 200000))
                    txt = f.read()
            except OSError:
                continue
            ms = re.findall(r"\[(\d{4})\.(\d{2})\.(\d{2})\.(\d{2})\]", txt)
            if not ms:
                continue
            y, mo, d, h = (int(x) for x in ms[-1])
            v = parts_to_date(y, mo, d, h)
            if best is None or v > best:
                best = v
        return best

    def _log_age_s(self):
        """Seconds since game.log / error.log were last written (the newer one).

        2026-11-21 (P0-1): `_log_date()` returns a LOWER BOUND, and how tight that
        bound is depends entirely on how long ago the file was appended.  A line
        stamped `[1945.01.02.01]` and written 30 seconds ago pins the live clock
        to within a few hours of that stamp; the same stamp read an hour later
        pins nothing.  `date()` needs the distinction to tell "the log lags" from
        "my reading is a frozen constant".
        """
        ages = []
        for path in self._log_paths():
            try:
                ages.append(time.time() - os.path.getmtime(path))
            except OSError:
                continue
        return min(ages) if ages else None

    def dismiss_popups_and_unpause(self):
        """Close a blocking World News / event popup and clear the pause flag.

        Run 7 lost ten months of game time to a single open news window: the
        clock stood still, every front tick was skipped, and 110 divisions
        watched Poland. Return activates a popup's default button and is
        harmless on the map (Escape would open the main menu, so never use it).
        The idler's pause byte (+0x6C1) is the same one `session.is_paused`
        reads; clearing it resumes the clock even when no popup has focus.
        """
        if sys.platform != "win32":
            try:
                import subprocess
                for disp in (":1", ":0"):
                    subprocess.run(
                        ["env", "DISPLAY=%s" % disp, "XDG_RUNTIME_DIR=/run/user/1001",
                         "xdotool", "key", "Return"],
                        timeout=2, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception:
                pass
        try:
            # 2026-11-17: the old literal 0x459E908 is a LINUX ELF offset.  On
            # the PE build self.va() still resolves base+offset, so at best the
            # read returned junk and at worst `idler` was a plausible-looking
            # non-zero value and the next store wrote 0 into an arbitrary
            # address.  Take the constant from session.py, which is the one the
            # rest of the codebase uses for the same CInGameIdler.
            #
            # NOTE: no synthetic input here.  An earlier revision posted
            # WM_KEYDOWN/WM_KEYUP VK_RETURN to the game window to dismiss a
            # popup; that is keyboard injection AND it silently answers whatever
            # dialog is open with its default option, which is the same trap as
            # the 13-day EVENT_TIMEOUT_DEFAULT auto-resolve.  Popups are read
            # and answered through the event queue instead (see hoi4_events).
            import session as _sess
            idler = self.p.u64(self.va(_sess.G_IDLER))
            if idler and self.p.u8(idler + _sess.IDLER_PAUSED) != 0:
                self.p.write(idler + _sess.IDLER_PAUSED, b"\x00")
        except Exception:
            pass
        return True

    def _gs_clock_raw(self):
        """The EXACT clock, straight out of CCurrentGameState+0x468.

        2026-11-21 (P0-1b): every other date path in this file ultimately guesses.
        The scan walks 8.5 GB of writable memory and then has to decide which of
        ~178 CDate-shaped candidates is the live one -- and while the game is
        PAUSED none of them tick, so it picks "the largest value below game.log",
        which is a stale copy (measured: it returned 16975872 while the game was
        really at 16979654, and still set `_date_verified = True`).

        CCurrentGameState+0x468 needs none of that.  It is an int32 the tick loop
        bumps while the game runs and that stops dead when it is paused, and it is
        the same hour count `date_raw` uses, shifted by a fixed 43,800,000 hours
        (5000 years).  Measured on pid 29864 at three moments -- 60779848 /
        16979848, 60779854 / 16979854, 60779861 / 16979861 -- exact to the hour.
        Two memory reads, no scan, no log, works while paused.
        """
        off = getattr(O, "GS_CLOCK", None)
        base = getattr(O, "G_GAMESTATE", None)
        offv = getattr(O, "GS_CLOCK_OFF", None)
        if off is None or base is None or offv is None:
            return None
        try:
            gs = self.p.u64(self.p.base + base)
            if not gs:
                return None
            tick = self.p.i32(gs + off)
            if not (0 < tick < 0x7FFFFFFF):
                return None
            val = int(tick) - int(offv)
            # 2026-11-22 (P0-1c): the sanity check used to compare this against
            # `game.log`, which is only written when an event fires.  In this mod
            # error.log can sit WEEKS behind (measured: log said 1936.10.01 while
            # the engine clock said 1936.12.25), so the "suspicious" verdict hit
            # the correct answer and the whole exact path was thrown away --
            # self-locking, because `_gs_clock_ok` never became True.
            #
            # Validate against the engine's OWN date fields instead, which sit
            # right next to the clock in CCurrentGameState:
            #   +0x478 year, +0x484 day-of-year (0-based, 365-day years),
            #   +0x488 month (0-based).   Measured 2026-11-22: clock 60767975 ->
            #   (1936, doy 358, month 11) and the three fields read exactly that.
            # A wrong epoch constant shifts these by days-to-years, so a +/-1 day
            # tolerance still catches it while surviving a calendar quirk.
            try:
                _y = self.p.i32(gs + 0x478)
                _doy = self.p.i32(gs + 0x484)
                _m1 = self.p.i32(gs + 0x488)
                _days = val // 24
                _gs_days = _y * 365 + _doy
                # 2026-11-22 (P2-1): 用连续总天数比对并保留 ±1 天容差，
                # 兼容 12 月 31 日 23:00（HUD 已显示次年 1 月 1 日 00:00）的跨年日界。
                if not (1800 <= _y <= 2200
                        and abs(_days - _gs_days) <= 1
                        and 0 <= _m1 <= 11):
                    self._gs_clock_reject = {"year": _y, "doy": _doy,
                                             "month0": _m1,
                                             "decoded": [_days // 365, _days % 365]}
                    return None
                self._gs_clock_reject = None
            except Exception:
                pass
            return val
        except Exception:
            return None

    def date_raw(self) -> int:
        """Live clock, with the midnight-bookmark gate applied exactly once.

        2026-11-21 (P0-1): every path inside `_date_raw_inner` -- the exact scan,
        its per-pid cache, the game-state scan, `pick_live_date`, the monotonic
        high-water mark -- can end up holding a stored event date, because they
        all ultimately compare against a game.log that may be months stale.  The
        2026-11-20 playtest measured the consequence: 1945.03.01.00 for a game
        whose own autosave said 1945.1.1.

        Rather than trusting (and re-patching) each of those paths, gate the
        result here: if the number is a midnight constant far above the game's
        own log, drop it and return the log stamp instead.  The log is a proven
        LOWER bound -- it can lag the clock but never lead it -- so it is always
        a safer answer than a bookmark months in the future, and it keeps every
        date-driven gate (wait_for, pause_in_hours, war days, front ticks)
        pointing at roughly the right week instead of the wrong year.
        """
        # 2026-11-21 (P0-1b): the exact clock first.  The number is exact; the
        # only thing that ever needed checking was the epoch constant, and since
        # 2026-11-22 (P0-1c) that check compares against CCurrentGameState's own
        # year / day-of-year / month fields instead of game.log.  The bookmark /
        # pick / log-floor heuristics below therefore never apply to it.
        _exact = self._gs_clock_raw()
        if _exact is not None:
            self._gs_clock_ok = True
            self._date_verified = True
            self._date_addr = None
            self._win_date = None
            # UPDATE, do not replace: `n_good` and friends are a scan cache
            # that a later fallback read still relies on.
            _dbg = dict(getattr(self, "_date_debug", None) or {})
            _dbg.update({"gs_clock": True, "pick_reason": None})
            self._date_debug = _dbg
            return int(_exact)
        try:
            # fell through: this reading is NOT the exact clock, make sure
            # date() does not label it as one
            (getattr(self, "_date_debug", None) or {}).pop("gs_clock", None)
        except Exception:
            pass
        v = self._date_raw_inner()
        if getattr(self, "_date_debug", None):
            self._date_debug.pop("gated_midnight_bookmark", None)
        try:
            logd = self._log_date()
        except Exception:
            logd = None
        if v and not date_plausible(v, logd, self._log_age_s()):
            try:
                self._date_debug["gated_midnight_bookmark"] = v
            except Exception:
                pass
            self._date_verified = False
            # 2026-11-21: while the game is RUNNING the tick test can finally
            # tell the clock from a bookmark, so throw the poisoned cache away
            # and let the next call re-scan.  While it is PAUSED nothing ticks,
            # a re-scan would find the same constant again and cost 5 s of CPU
            # per call, so keep the cache and keep answering with the log floor.
            try:
                import session as _sess_d
                _running = not bool(_sess_d.is_paused(self))
            except Exception:
                _running = False
            if _running:
                # Rate-limited: a full scan walks ~3.8 GB.  Doing it on EVERY
                # read while the game runs made one `date_raw()` cost tens of
                # seconds and pushed hoi4_wait_for past the client timeout
                # (measured 2026-11-21).  One attempt every two minutes is
                # enough to re-bind the real clock once it ticks; until then the
                # log floor is a cheap, correct-enough answer.
                _now = time.time()
                if _now - getattr(self, "_gate_rescan_at", 0.0) > 120.0:
                    self._gate_rescan_at = _now
                    try:
                        _bad = getattr(self, "_win_date", None)
                        if _bad:
                            self._dead_date_addrs.add(_bad)
                        self._win_date = None
                        self._win_date_pid = None
                        self._date_exact_cache = None
                        self._date_addr = None
                    except Exception:
                        pass
            if getattr(self, "_date_high", None) and logd and self._date_high > logd:
                # the high-water mark is the same bookmark; do not let it drag
                # every later read back up to it
                self._date_high = int(logd)
            if logd:
                self._date_last = int(logd)
                return int(logd)
        return v

    def _scan_date(self, force=False):
        """The ONLY way to (re)locate the date address.  One scan per 120 s.

        2026-11-21 (续测 P0-1): `_date_raw_inner` contained FIVE unconditional
        `find_date_address(force=True)` calls.  Every one walks the whole writable
        address space (measured 13.7-26.6 s), and two of them run on a plain
        `date_raw()`.  Because `cmdwin.post_drained` samples the clock twice per
        command and `session.pause` three times, EVERY write tool took 27-55 s and
        blew the 50 s watchdog, and `hoi4_overview` needed three tries in a row.
        Nothing may bypass this gate any more: when it refuses, the caller falls
        back to the game.log stamp and the read costs microseconds.
        """
        now = time.time()
        if getattr(self, "_date_addr", None) and not force:
            return self._date_addr
        if now - getattr(self, "_date_scan_at", 0.0) <= 120.0:
            return None
        self._date_scan_at = now
        try:
            # A scan only happens when we have NO usable address, so force the
            # full path.  force=False can answer from a stale disk cache or bail
            # out early -- that is how the first read of a process ended up with
            # no address at all, after which every later read fell back to the
            # game.log stamp and the clock froze while the game ran on.
            return self.find_date_address(force=True)
        except Exception:
            return None

    def _date_raw_inner(self) -> int:
        """Read the live clock and re-locate it when the cached one stops ticking.

        A fresh game process is usually PAUSED at the main menu / campaign start,
        so the first scan cannot distinguish the live clock from a stale copy and
        falls back to the value nearest game.log.  That copy then never advances
        and every date-based feature (front tick gate, wait_for, war days) freezes.
        When the game is running and the cached value has been static for >3 s,
        force one scan; that scan can now see which candidate ticks.

        2026-11-04: every value is now bounds-checked to [1930, 1960]. A freed heap
        chunk that used to hold a CDate still parses as a valid int32, which is how
        `hoi4_date` reported 112217.05.06 / 0000.08.03 and once triggered a
        premature war-goal justification. Outside the window the address is dropped
        and the game's own log stamp is used instead.
        """
        # 2026-11-06: LO was 1936.01.01, but a fresh campaign keeps stale CDate
        # copies from the day BEFORE the start (1935.12.26/27). Those made every
        # `date_raw` fail the range test, rescan the whole 3.8 GB address space
        # (5 s of CPU) and do it again on the next call — the child sat at 60 %
        # CPU and the driver blocked for minutes. The log floor below turns such
        # a copy into the correct date instead of rejecting it.
        # 2026-11-10: HI was 1946.01.01, but this campaign is in November 1946, so
        # every genuine clock reading was rejected as out of range and the code fell
        # back to a stale CDate copy (it reported 1942.06.23 for weeks of play).
        LO, HI = parts_to_date(1935, 1, 1), parts_to_date(1965, 1, 1)
        # A NEW game process invalidates every date pointer we hold. The MCP server
        # keeps one Game object across game restarts, and the old address can still
        # read as a plausible date in the new process (it showed 1935.12.26 12:00,
        # the campaign start stamp, on a game that was at 1936.01.05).
        try:
            _pid = self.p.pid
        except Exception:
            _pid = None
        if getattr(self, "_date_pid", None) != _pid:
            self._date_pid = _pid
            self._date_addr = None
            self._date_last = None
            self._date_static = None
            self._date_retry_at = 0
            self._date_high = None
            self._date_exact_cache = None
            self._win_date = None
            self._win_date_pid = None
            self._win_date_last = None
            self._dead_date_addrs = set()
        # 2026-11-21 (live): a scan whose result gets REJECTED leaves `_date_addr`
        # empty, so every later read walked the 3.8 GB address space again.  One
        # `hoi4_get_country` after a server restart was still running at 199 s and
        # had to be killed.  Rate-limit the scan ITSELF (the old limiter only
        # stopped the cache clearing); between attempts the log floor answers.
        _now = time.time()
        addr = self._scan_date()
        if addr and getattr(self, "_date_verified", False):
            # The address came from the EXACT CDate signature scan (float hour/24 at
            # -8, hour at -4, counter at 0, zero at +4). Every heuristic below
            # compares against game.log, and a stalled log clamps a perfectly good
            # clock (that is exactly how the 71-day drift happened), so a verified
            # read bypasses them.
            v = self.p.i32(addr)
            # 2026-11-11: EXCEPT when the "verified" object is a stale CDate copy
            # years ahead of the game's own log. A frozen 1945.04.04 CDate matches
            # the signature perfectly (float, hour, counter, zero) and used to be
            # returned forever, which pinned a fresh 1936 campaign to 1945 — every
            # date-based gate then believed nine years had passed. The log stamp
            # can lag the clock but can never be months behind it, so it wins.
            _lrv = self._log_date()
            if (LO <= v <= HI and _lrv and LO <= _lrv <= HI
                    and v > _lrv + 24 * 60):
                # 2026-11-19 (live): the allowance here was 550 days, and that is
                # how a 1937.09.17 bookmark beat a 1936.10.10 clock by 342 days
                # while the HUD plainly said 1936.10.10.17.  game.log is written
                # as the game runs, so in an active session it cannot lag by two
                # months; 60 days is already generous.
                try:
                    self._dead_date_addrs.add(addr)
                except Exception:
                    pass
                self._date_addr = None
                self._date_verified = False
                addr = None
            elif LO <= v <= HI and not (_lrv and v < _lrv - 48):
                self._date_last = v
                self._date_static = (v, time.time())
                # 2026-11-19d: this used to be a plain `= v`, so ONE bad scan
                # that bound to a stale CDate dragged the monotonic high-water
                # mark DOWN to it -- and every later guard then defended that
                # wrong value (this is how hoi4_date froze ~4 weeks back while
                # the HUD kept moving).  The mark may only ever go up.
                _hi0 = getattr(self, "_date_high", None)
                self._date_high = v if (not _hi0 or v >= _hi0) else _hi0
                return v
            elif LO <= v <= HI:
                # 2026-11-11: a "verified" address can still hold a STALE CDate
                # copy that has fallen behind game.log (seen: tool 1938.06.30,
                # log 1938.07.24, screen 1938.08.28). game.log cannot be ahead of
                # the clock, so a read more than two days behind it is dead.
                try:
                    self._dead_date_addrs.add(addr)
                except Exception:
                    pass
                self._date_addr = None
                self._date_verified = False
                addr = None
                v = 0
        v = self.p.i32(addr) if addr else 0
        if not (LO <= v <= HI):
            if addr:
                self._date_addr = None
            try:
                a2 = self._scan_date(force=True)
                v = self.p.i32(a2) if a2 else 0
            except Exception:
                v = 0
        now = time.time()
        if LO <= v <= HI and addr and getattr(self, "_date_last", None) is None:
            # FIRST read of this process: the scan may have happened while the
            # clock was not yet ticking (a fresh campaign is paused at
            # 1935.12.26 12:00), and the log-proximity fallback can then bind to a
            # historical CDate copy. If the game is running, a live clock moves
            # within 0.3 s; one that does not is stale, so rescan once.
            try:
                import session as _sess0
                _run = not _sess0.is_paused(self)
            except Exception:
                _run = True
            if _run:
                time.sleep(0.3)
                if self.p.i32(addr) == v:
                    self._date_addr = None
                    try:
                        _a0 = self._scan_date(force=True)
                        if _a0:
                            v = self.p.i32(_a0)
                    except Exception:
                        pass
        if LO <= v <= HI:
            # A read that lags the game's OWN log stamp by more than 60 days is a
            # stale CDate copy, not a slow clock: game.log records real events
            # (declarations, focus completions) and cannot be months ahead of the
            # session. This is what caught the 1936.08.16 vs 1937.03.02 flap.
            log_ref = self._log_date()
            if log_ref and LO <= log_ref <= HI and v < log_ref - 24 * 2:
                # game.log is written AS the game runs, so it can lag the live clock
                # but can never be AHEAD of it. A read that is more than two days
                # behind the log is therefore a stale CDate copy (seen: 1935.12.26
                # on a game whose clock and log both said 1936.01).
                self._date_addr = None
                try:
                    a2 = self._scan_date(force=True)
                    v2 = self.p.i32(a2) if a2 else 0
                    if LO <= v2 <= HI and v2 > v:
                        v = v2
                except Exception:
                    pass
                if v < log_ref - 24 * 2:
                    v = log_ref
            prev = getattr(self, "_date_last", None)
            if prev and LO <= prev <= HI and v < prev - 240:
                # Time does not run backwards except when a save is loaded. A copy
                # that is 10+ days older than the last reading is a stale CDate in
                # the heap, not a reload: rescan once, then accept whatever we get.
                self._date_addr = None
                try:
                    _a1 = self._scan_date(force=True)
                    v2 = self.p.i32(_a1) if _a1 else 0
                    if LO <= v2 <= HI and v2 >= prev - 240:
                        v = v2
                except Exception:
                    pass
                if v < prev - 240:
                    v = prev          # hold the last good clock, never run backwards
            # --- monotonic high-water mark (2026-11-05) --------------------------
            # A 2026-11-05 run reported 1936.05.27 and then 1936.02.18: the scan had
            # bound to a stale CDate copy, and a 99-day dip is inside the +-240 h
            # guard above. In-game time never moves backwards inside one process, so
            # hold the highest value ever read; a genuine reload needs an MCP restart.
            hi = getattr(self, "_date_high", None)
            _lrhi = self._log_date()
            if (hi is not None and _lrhi and LO <= _lrhi <= HI
                    and hi > _lrhi + 24 * 550):
                # 2026-11-06: a scan made before the campaign existed (main menu,
                # game.log not written yet) seeded this mark from a garbage 1945
                # CDate copy, and the final high-water re-apply below then undid
                # every log clamp — the clock was pinned to 1945.04.27 for the
                # whole session while the game sat at 1936.01.01. game.log can
                # never be months behind the live clock, so the mark is wrong.
                hi = self._date_high = _lrhi
            if hi is None:
                # Never seed the mark from an UNVERIFIED read: that is exactly
                # how the 1945 copy became permanent.
                seed = (v if not getattr(self, "_date_unverified", False)
                        else (_lrhi or v))
                try:
                    _lr = self._log_date()
                    if _lr and LO <= _lr <= HI and _lr > seed:
                        seed = _lr
                except Exception:
                    pass
                hi = self._date_high = seed
            if v < hi:
                self._date_addr = None
                try:
                    _a2b = self._scan_date(force=True)
                    v2 = self.p.i32(_a2b) if _a2b else 0
                    if LO <= v2 <= HI and v2 >= hi:
                        v = v2
                except Exception:
                    pass
                if v < hi:
                    v = hi
            if v > hi and not getattr(self, "_date_unverified", False):
                self._date_high = v
            # A read that is more than 60 days AHEAD of the game's own log stamp is a
            # garbage heap value that happens to fall inside [1930,1960] (seen right
            # after a save reload: 1959.09.14 on a game at 1940.01.01). game.log
            # cannot be months behind the live clock, so it wins.
            _lr2 = self._log_date()
            if _lr2 and LO <= _lr2 <= HI and v > _lr2 + 24 * 550:
                self._date_addr = None
                v = _lr2
                self._date_high = _lr2
            # Single-step forward jump guard: more than 30 days between two reads is
            # only believable when the game's own log stamp is in the same window.
            _prev2 = getattr(self, "_date_last", None)
            if _prev2 and LO <= _prev2 <= HI and v - _prev2 > 24 * 30:
                _lr3 = self._log_date()
                if not (_lr3 and LO <= _lr3 <= HI and abs(v - _lr3) < 24 * 15):
                    v = _prev2
                    self._date_high = _prev2
            seen = getattr(self, "_date_static", None)
            if seen is not None and v == seen[0]:
                if now - seen[1] > 3.0:
                    try:
                        import session
                        paused = session.is_paused(self)
                    except Exception:
                        paused = True
                    if not paused:
                        # This copy has been frozen for >3 s while the game runs:
                        # it is a dead CDate, blacklist it BEFORE the rescan so
                        # the scan cannot hand the same address straight back.
                        _dead = getattr(self, "_dead_date_addrs", None)
                        if _dead is not None and self._date_addr:
                            _dead.add(self._date_addr)
                        self._date_addr = None
                        addr = self._scan_date(force=True)
                        v2 = self.p.i32(addr) if addr else 0
                        if LO <= v2 <= HI:
                            v = v2
                        # 2026-11-06: this branch used to `return v` immediately,
                        # bypassing every guard below. A fresh campaign then reported
                        # 1945.04.27 while game.log said 1936.01.01.12.
                        _lr3 = self._log_date()
                        if _lr3 and LO <= _lr3 <= HI and v > _lr3 + 24 * 60:
                            # one-directional: only a read AHEAD of the log is garbage.
                            # (the bidirectional version dragged the clock 25 days back
                            # whenever game.log had not been written for two months)
                            v = _lr3
                            self._date_high = _lr3
                        self._date_static = (v, now)
            else:
                self._date_static = (v, now)
            # 2026-11-06: the `_date_static` rescan above runs AFTER the monotonic
            # guard and used to adopt an older CDate copy (seen: 1938.10 -> 1938.09.17
            # repeatedly). Re-apply the high-water mark before returning.
            _hi2 = getattr(self, "_date_high", None)
            _lr5 = self._log_date()
            if (_hi2 and _lr5 and LO <= _lr5 <= HI
                    and _hi2 > _lr5 + 24 * 90):
                # Same garbage-mark guard, re-applied at the very end.
                _hi2 = self._date_high = _lr5
            if _hi2 and LO <= _hi2 <= HI and v < _hi2:
                v = _hi2
            # 2026-11-06 (run 6): game.log is written AS the game runs, so a read
            # that is more than a day BEHIND the last log stamp is a stale or
            # frozen copy. This is the last line of defence against the 40-minute
            # 1939.05 freeze that made the front skip every tactical tick.
            if _lr5 and LO <= _lr5 <= HI and _lr5 > v:
                v = _lr5
                if not _hi2 or _lr5 > _hi2:
                    self._date_high = _lr5
            self._date_last = v
            return v
        # Out of range even after a forced rescan: trust the game's own log stamp,
        # then the last good value. Never hand a garbage "112217" to a driver.
        # 2026-11-06: `_date_last` itself can be poisoned — a scan made at the
        # main menu (game.log not written yet) picked a 1945 CDate copy and this
        # fallback then returned it forever (hoi4_date said 1945.04.27 while the
        # campaign sat at 1936.01.01). A value months ahead of game.log is the
        # garbage one, so drop it and prefer the log.
        _lr = self._log_date()
        _last = getattr(self, "_date_last", None)
        if _last and _lr and LO <= _lr <= HI and _last > _lr + 24 * 90:
            _last = None
        # 2026-11-19d: the old tail returned the FIRST usable value in
        # (_last, log, scan_logs), so a frozen _date_last won over a perfectly
        # good game.log stamp every single time.  Take the largest candidate
        # instead: within one process the clock only moves forward, and the log
        # stamp is a lower bound that can never be ahead of the clock.
        _cands = [x for x in (_last, _lr, self._scan_logs_date())
                  if x and LO <= x <= HI]
        if _lr and LO <= _lr <= HI:
            _keep = [x for x in _cands if x >= _lr - 48]
            _cands = _keep or [_lr]
        if _cands:
            _v = max(_cands)
            self._date_last = _v
            return _v
        return v

    def date(self) -> dict:
        """Current date, plus how much of it can be trusted.

        Fields that matter to a caller:
          raw / text        the answer (already substituted, see below)
          source            clock | clock (scan, unverified: <why>) |
                            log floor (…) | stale (BEHIND …) | suspect (AHEAD …) |
                            fallback
          suspect           true whenever the number is NOT a plain clock reading
          rejected_raw      the midnight constant that was thrown away, if any
          log_raw           the game's own last log stamp (a LOWER bound)
          log_delta_days    answer - log stamp, in days
          lower_bound       present whenever `raw` is the log stamp rather than a
                            clock read

        2026-11-21 (P0-1): the 2026-11-20 playtest's central complaint — "the
        date is months off while `source` still says clock" — reproduced live:
        this build reported 1945.03.01.00 (hour 00, same value at FOUR addresses)
        while game.log had written [1945.01.03.06] and the autosave written
        minutes earlier said 1945.1.1 — 56.75 days ahead, just under the old
        60-day guard.  1945.03.01 is a SCHEDULED DATE, as `_find_date_exact`'s own
        docstring lists.  So the reporting layer now refuses that shape outright
        and answers with the log stamp, which can lag the clock but never leads
        it.
        """
        logd = None
        try:
            logd = self._log_date()
        except Exception:
            logd = None
        age = self._log_age_s()
        h = self.date_raw()
        _dbg_now = getattr(self, "_date_debug", None) or {}
        _gs_clock = bool(_dbg_now.get("gs_clock"))
        rejected = None
        # Reporting-layer gate: `date_raw` only rejects on proof (45 days, or a
        # fresh log); the displayed answer is additionally protected against the
        # midnight-bookmark shape even when the log has gone quiet for hours.
        # The exact CGameState clock needs neither: it cannot be a bookmark.
        if (not _gs_clock) and logd and date_bookmark_shape(h, logd):
            rejected = int(h)
            h = int(logd)
        y, m, d, hh = date_to_parts(h)
        out = {"year": y, "month": m, "day": d, "hour": hh,
               "raw": h, "text": "%04d.%02d.%02d.%02d" % (y, m, d, hh)}
        out["log_raw"] = logd
        out["log_age_s"] = None if age is None else round(age, 1)
        out["suspect"] = False
        # `pick_reason` is only present when the number came from the HEURISTIC
        # picker (pick_live_date), never from the tick-verified exact scan.  A
        # heuristic pick must not inherit the verified flag.
        _pick = (getattr(self, "_date_debug", None) or {}).get("pick_reason")
        _verified = bool(getattr(self, "_date_verified", False)) and not _pick
        _gated = (getattr(self, "_date_debug", None) or {}).get(
            "gated_midnight_bookmark")
        if logd is not None:
            _dh = int(h) - int(logd)
            out["log_delta_hours"] = _dh
            # 向零截断整数天，避免 -1 小时被 Python `//` 向下取整误报为 -1 天
            out["log_delta_days"] = _sdiv(_dh, 24)
        if rejected is not None:
            out["source"] = "log floor (midnight bookmark %d d ahead rejected)" % (
                (rejected - int(logd)) // 24)
            out["suspect"] = True
            out["rejected_raw"] = rejected
            out["lower_bound"] = logd
        elif _gated:
            # `date_raw` itself refused the value and answered with the log stamp.
            out["source"] = "log floor (rejected %d d ahead)" % (
                (int(_gated) - int(logd)) // 24 if logd else 0)
            out["suspect"] = True
            out["rejected_raw"] = int(_gated)
            out["lower_bound"] = logd
        elif logd and int(h) < int(logd) - 3:
            _behind = int(logd) - int(h)
            out["source"] = "stale (BEHIND game.log by %s)" % (
                "%d d" % (_behind // 24) if _behind >= 24 else "%d h" % _behind)
            out["suspect"] = True
            out["lower_bound"] = logd
        elif logd and (not _gs_clock) and (
                # the log was appended recently -> it pins the clock to hours, so
                # "days ahead of it" cannot be the live date
                (age is not None and age <= 7200
                 and int(h) - int(logd) > 24 * 3)
                # a HEURISTIC pick this far ahead is the 1945.03.01/17 trap: not
                # proof (a log can lag months), but it must not be called "clock"
                or (_pick and int(h) - int(logd) > 24 * 45)):
            out["source"] = "suspect (AHEAD of game.log by %d d%s)" % (
                (int(h) - int(logd)) // 24,
                ", log %d s old" % int(age) if age is not None else "")
            out["suspect"] = True
            out["lower_bound"] = logd
        elif _gs_clock:
            # exact: two memory reads off CCurrentGameState+0x468, no scan, no log
            out["source"] = "clock (CGameState+0x468, exact)"
        elif _verified:
            if logd and int(h) - int(logd) > 24 * 3:
                out["source"] = "clock (game.log lags %d d)" % (
                    (int(h) - int(logd)) // 24)
            else:
                out["source"] = "clock"
        elif _pick:
            # honest label for a heuristic value with no contradiction in sight
            out["source"] = "clock (scan, unverified: %s)" % _pick
        else:
            out["source"] = "fallback"
        out["pick"] = _pick
        return out

    def pause_in_hours(self, hours: int) -> ConsoleResult:
        return self.console("pause_in_hours %d" % hours)

    @property
    def hours_until_pause(self) -> Optional[int]:
        if O.GS_PAUSE_HOURS is None:
            return None
        return self.p.i32(self.gamestate + O.GS_PAUSE_HOURS)

    # =============================================== summary
    def snapshot(self, tag: str = None) -> dict:
        with self:
            c = self.country(tag) if tag else self.player()
            s = c.summary()
            s["date"] = self.date()["text"]
            return s
