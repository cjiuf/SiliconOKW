"""科技库、科研槽位管理、陆海空学说、科学家与特别工程领域 Mixin。"""
from __future__ import annotations

import offsets as O
import os
import session_state as _SS
import struct
import sys

from ._shared import (
    CALLING_DIR,
    CO_PROGRAM_STATUS,
    CO_PROJECT_POOL,
    CO_TECH_STATUS,
    FN_CAN_RESEARCH,
    RS_CURRENT,
    RS_SPEED,
    TE_ID,
    TE_PROGRESS,
    TS_RESEARCHED,
    TS_SLOTS,
    TT_ID,
    TT_NAME,
    TT_RESEARCH_COST,
    _arr,
    item_name,
)

class ResearchMixin:
    """科技库、科研槽位管理、陆海空学说、科学家与特别工程领域 Mixin。"""

    TECH_IDS_CACHE = _SS.state_path(_SS.TECH_IDS_NAME)
    _LEGACY_TECH_IDS_CACHE = os.path.join(CALLING_DIR, "techids.pkl")

    DOCTRINE_LOC = {
        "new_mobile_warfare": "GRAND_DOCTRINE_MOBILE_WARFARE",
        "mobile_warfare": "GRAND_DOCTRINE_MOBILE_WARFARE",
        "superior_firepower": "GRAND_DOCTRINE_SUPERIOR_FIREPOWER",
        "grand_battleplan": "GRAND_DOCTRINE_GRAND_BATTLEPLAN",
        "mass_assault": "GRAND_DOCTRINE_MASS_ASSAULT",
        "fleet_in_being": "GRAND_DOCTRINE_FLEET_IN_BEING",
        "convoy_raiding": "GRAND_DOCTRINE_CONVOY_RAIDING",
        "base_strike": "GRAND_DOCTRINE_BASE_STRIKE",
        "battlefield_support": "GRAND_DOCTRINE_BATTLEFIELD_SUPPORT",
        "operational_integrity": "GRAND_DOCTRINE_OPERATIONAL_INTEGRITY",
        "strategic_destruction": "GRAND_DOCTRINE_STRATEGIC_DESTRUCTION",
        "special_forces_quantity": "GRAND_DOCTRINE_SPECIAL_FORCES_QUANTITY",
        "special_forces_quality": "GRAND_DOCTRINE_SPECIAL_FORCES_QUALITY",
    }

    def _tech_ids_from_disk(self):
        """Provisional live-tech-id -> name table (stable across processes)."""
        try:
            import safe_cache
            if safe_cache.exists(self.TECH_IDS_CACHE):
                d = safe_cache.load(self.TECH_IDS_CACHE)
                ids = (d or {}).get("ids") or {}
                return {int(k): v for k, v in ids.items()} if ids else None
            # 兼容迁移：旧版落在 calling/techids.{pkl,json} 的缓存平滑迁入状态目录
            if safe_cache.exists(self._LEGACY_TECH_IDS_CACHE):
                d = safe_cache.load(self._LEGACY_TECH_IDS_CACHE)
                ids = (d or {}).get("ids") or {}
                if ids:
                    res = {int(k): v for k, v in ids.items()}
                    self._save_tech_ids(res)
                    return res
            return None
        except Exception:
            return None

    def _save_tech_ids(self, byid):
        try:
            import safe_cache
            safe_cache.save(self.TECH_IDS_CACHE, {"schema": 1, "ids": dict(byid)}, fmt="json")
        except Exception:
            pass

    def _load_tech_db(self, wait_s=0.0):
        """Build the technology name/id tables.

        2026-11-18: `wait_s` makes the call BLOCK until the background
        CTechnologyTemplate RTTI scan lands instead of returning a cold table.
        Before this, every caller had to retry by hand: `hoi4_research_available`
        raised "technology database not ready ... retry this call" and stayed
        broken for the whole turn, `hoi4_fill_research` found no candidates, and
        `start_research` could not resolve the id -> template pointer at all.

        2026-11-19: the id -> name half of that table only depends on the game's
        own data (a live CTechnology carries the template id at +0x08, the
        template carries it at +0x3C), so it is cached to disk and used
        provisionally until the RTTI scan rebuilds it.  Before this, the first
        `hoi4_research` after every reload printed `researching: null` for all
        four slots even though `slot_busy` was true.
        """
        # 2026-11-17: _selfheal_check() (server.py) DELETES these attributes when
        # the game pid changes, so a plain `self._tech_by_name` raised
        #   AttributeError: 'Game' object has no attribute '_tech_by_name'
        # on the first research call after every reload.  Read them defensively
        # and re-seed the defaults.
        if getattr(self, "_tech_by_name", None) is not None:
            return
        if not hasattr(self, "_tech_by_name"):
            self._tech_by_name = None
        if not hasattr(self, "_tech_by_id"):
            self._tech_by_id = None
        if self._tech_by_id is None and sys.platform == "win32":
            provisional = self._tech_ids_from_disk()
            if provisional:
                self._tech_by_id = provisional
                self._tech_ids_provisional = True
        p = self.p
        if sys.platform == "win32":
            # CTechnologyDatabase has no RTTI-visible singleton on hoi4.exe; the
            # old path retried oi.singleton() twice, rebuilt the whole ObjectIndex
            # and burned 35 s before failing with
            # "TypeError: 'NoneType' object is not iterable".
            #
            # 2026-11-16: CSetResearchCommand wants a **CTechnologyTemplate***,
            # not a live CTechnology.  Its CanExecute (0x11F3360) calls
            # 0x0F51A30, which is literally
            #     return ts->data[tech->+0x38 - 1];      // +0x38 is 1-BASED
            # The live objects in CTechnologyStatus+0x088 all have +0x38 == 0,
            # so every command built from one was refused in C++ *silently*
            # (measured: 0 of 552 accepted, which is why research has never
            # worked on this build and all four slots stayed empty).
            # A CTechnologyTemplate has +0x38 = 1..552 and +0x10 = script key;
            # feeding the template gives CanExecute = 1.
            self._kick_tech_scan()
            if wait_s:
                import time as _time
                _deadline = _time.time() + float(wait_s)
                pid = getattr(getattr(self, "p", None), "pid", None)
                run_key = (pid, "CTechnologyTemplate")
                last_retry = 0.0
                while (_time.time() < _deadline and
                       self._win_vt_cache("CTechnologyTemplate") is None):
                    running = getattr(type(self), "_VT_SCAN_RUNNING", set())
                    # 2026-11-23 (C-2): 若后台冷扫因刚重启/加载中扫出空表已退出，
                    # 主动间隔重扫，绝不傻等到超时或永久死锁在空科技库！
                    if run_key not in running and (_time.time() - last_retry) >= 2.5:
                        last_retry = _time.time()
                        try:
                            self.win_scan_class("CTechnologyTemplate", use_cache=False)
                        except Exception:
                            pass
                        if self._win_vt_cache("CTechnologyTemplate") is not None:
                            break
                    _time.sleep(0.5)
            tmpl = self._win_vt_cache("CTechnologyTemplate")
            byname, byid = {}, {}
            if tmpl:
                id2name = {}
                for h in tmpl:
                    try:
                        nm = item_name(p, h + 0x10)
                    except Exception:
                        continue
                    if not nm or not (1 < len(nm) < 60):
                        continue
                    if not all(32 < ord(ch) < 127 for ch in nm):
                        continue
                    if nm in byname:
                        continue
                    i38 = p.i32(h + 0x38)
                    if not (1 <= i38 <= 4096):
                        continue          # not a real template object
                    byname[nm] = h
                    id2name.setdefault(p.i32(h + 0x3C), nm)
                # _tech_by_id stays keyed by the id a LIVE CTechnology carries
                # at +0x08 (== template +0x3C); research_slots() needs that to
                # name the technology sitting in a slot.
                ts = p.u64(self._c(None).ptr + CO_TECH_STATUS)
                for t in (_arr(p, ts, 0x88) if ts else []):
                    k = p.i32(t + 0x08)
                    nm = id2name.get(k)
                    if nm:
                        byid[k] = nm
            self._tech_by_name = byname
            if byid:
                self._tech_by_id = byid
                self._save_tech_ids(byid)
            elif getattr(self, "_tech_by_id", None):
                # Cold scan: keep the provisional id -> name table loaded from
                # disk instead of clobbering it with {} (that is what made
                # `hoi4_research` print `researching: null` after every reload).
                pass
            else:
                self._tech_by_id = byid
            self._tech_scan_pending = not byname
            if not byname:
                # Nothing cached yet: leave the table unloaded so the next call
                # retries instead of staying dead for the whole process.
                self._tech_by_name = None
            return
        db = self.oi.singleton("CTechnologyDatabase")
        if not db:
            self._oi = None
            db = self.oi.singleton("CTechnologyDatabase")
        if not db:
            # Built too early (main menu / campaign still loading): do NOT cache an
            # empty table, otherwise research stays dead for the whole process.
            self._tech_by_name = None
            self._tech_by_id = {}
            self._oi = None
            return
        byname, byid = {}, {}
        for off in (0x48, 0x78, 0xD8):
            for t in _arr(p, db, off):
                nm = item_name(p, t + TT_NAME)
                if nm:
                    byname[nm] = t
                    byid[p.i32(t + TT_ID)] = nm
        if not byname:
            # The database object exists but is still empty (it is filled after the
            # campaign finishes loading).  Caching {} here made every later lookup
            # come back empty for the rest of the process -- hoi4_research_available
            # then reported count 0 for good.
            self._tech_by_name = None
            self._tech_by_id = {}
            self._oi = None
            return
        self._tech_by_name, self._tech_by_id = byname, byid

    def tech_name(self, tech_obj):
        """CTechnology* (country object) -> technology name"""
        self._load_tech_db()
        return (self._tech_by_id or {}).get(self.p.i32(tech_obj + TE_ID))

    def tech_status(self, tag=None):
        return self.p.u64(self._c(tag).ptr + CO_TECH_STATUS)

    def tech_cost_days(self, tech_name):
        """Base research time of a technology, in days.

        `CTechnologyTemplate+0x3E0` holds days x1000 on the PE build (verified:
        basic_machine_tools 150000 -> "130日"-class values, synth_oil_experiments
        300000, improved_light_tank_chassis 200000).  The old 0x3D0 is not the
        cost: it reads -1 for most techs (0x3D8 is the start year).
        """
        self._load_tech_db()
        t = (self._tech_by_name or {}).get(tech_name)
        if not t:
            return None
        raw = self.p.i32(t + TT_RESEARCH_COST)
        if not (0 < raw < 100000 * 1000):
            return None
        return round(raw / 1000.0, 1)

    def tech_progress(self, tech_obj):
        """Research days already invested in a CTechnology, speed-weighted."""
        if not tech_obj:
            return 0.0
        return self.p.i64(tech_obj + TE_PROGRESS) / float(O.FIXED)

    def research_slots(self, tag=None):
        """What each research slot is doing, with the UI's own numbers.

        Progress lives on the technology, not on the slot:
            progress_days = CTechnology+0x198 / 1e5
            cost_days     = CTechnologyTemplate+0x3E0 / 1000
            speed         = CResearchSlot+0x38 / 1e5   (days gained per day)
        Verified two ways on 1.19.5.0 (2026-11-15):
          * every tech the country starts with shows progress == cost * 1.1
            (56/56 at exactly 1.1, the start-of-game research speed);
          * a running tech gains exactly `speed` x 1e5 per daily tick
            (basic_machine_tools 3535000 -> 3636000 -> ... , speed 101000).
        """
        p = self.p
        # 2026-11-17: without this the very first read after a save load prints
        # `researching: null, progress: 0` for all four slots -- the slots are
        # actually busy, only _tech_by_id (the id -> name map) is not built yet.
        # hoi4_overview then fired a FALSE "RESEARCH_IDLE: 4 slots empty" alert.
        self._load_tech_db()
        ts = self.tech_status(tag)
        out = []
        for i, s in enumerate(_arr(p, ts, TS_SLOTS)):
            cur = p.u64(s + RS_CURRENT)
            nm = self.tech_name(cur) if cur else None
            speed = p.i64(s + RS_SPEED) / float(O.FIXED) or 1.0
            prog_days = self.tech_progress(cur) if cur else 0.0
            cost_days = self.tech_cost_days(nm) if nm else None
            # 2026-11-21 (P2-13): was `... if cost_days else 0.0`, so a slot with
            # 55.35 days done and `required_known: false` reported `percent: 0.0`
            # -- indistinguishable from "just started" for anything that sorts or
            # thresholds on it.  Unknown must stay unknown (None), the same way
            # `required` already does.
            pct = round(100.0 * prog_days / cost_days, 2) if cost_days else None
            rec = {
                "slot": i,
                "researching": nm,
                # slot_busy is the authoritative "is this slot occupied" flag:
                # it is a raw pointer read and does NOT depend on the tech-name
                # database being warm.  Never call a slot idle because the NAME
                # is missing.
                "slot_busy": bool(cur),
                # 2026-11-19 (measured): on the tick a technology finishes, the
                # slot still holds the pointer and the old name while
                # percent == 100 / days_remaining == 0 for one or more reads.
                # `fill_research` and the RESEARCH_IDLE alert both keyed off
                # slot_busy and therefore missed the free slot. Report it
                # separately instead of lying about slot_busy.
                "slot_done": bool(cur and cost_days and prog_days >= cost_days * 0.999),
                "tech_ptr": ("0x%x" % cur) if cur else None,
                "progress": round(prog_days, 3),          # days of research done
                # 2026-11-20 (P2 #12): `required: 0.0` looked like "this costs
                # nothing" while it really meant "CTechnologyTemplate is not warm
                # yet".  null says unknown; `required_known` makes it explicit.
                "required": round(cost_days, 3) if cost_days else None,  # days to do
                "required_known": bool(cost_days),
                "percent": None if pct is None else min(pct, 100.0),
                "speed": speed,
                "base_days": cost_days,
            }
            if cur and nm is None:
                rec["name_unknown"] = ("technology database not warm yet -- "
                                       "the slot IS occupied, retry in ~90 s")
            if cost_days:
                rec["days_remaining"] = round(max(cost_days - prog_days, 0.0) / speed, 1)
            # 2026-11-21 (P3-4): `speed` below 1.0 is the ahead-of-time penalty (the
            # tech's historical year has not arrived), not "research got slower".
            # The 2026-11-20 playtest read 0.296 and assumed a bug.
            if speed < 0.999:
                rec["speed_note"] = ("below 1.0 = ahead-of-time penalty: this tech's "
                                     "historical year has not arrived yet; "
                                     "days_remaining already accounts for it")
            out.append(rec)
        # 2026-11-22 (复测 5 §8.4 时刻 ①): 当某槽刚完成科研（slot_done=True）时，
        # 立刻比对新解锁装备是否导致现有产线过时，实现零延迟提醒。
        done_techs = [r["researching"] for r in out if r.get("slot_done") and r.get("researching")]
        if done_techs:
            try:
                import equipmeta as _em
                _lines = self.production_lines(tag, include_obsolete=False)
                _avail = self._active_variant_names(tag)
                _hits = _em.check_tech_unlocks_obsolete(done_techs, _lines, _avail)
                if _hits:
                    _by_tech = {}
                    for _tk in done_techs:
                        _u = set(_em.tech_unlocks(_tk))
                        _matched = [h for h in _hits if h.get("upgrade_to") in _u]
                        if _matched:
                            _by_tech[_tk] = _matched
                    for r in out:
                        if r.get("slot_done") and r.get("researching") in _by_tech:
                            r["unlocks_obsolete_lines"] = _by_tech[r["researching"]]
                            r["obsolete_alerts"] = [
                                _em.format_obsolete_alert(h) for h in _by_tech[r["researching"]]
                            ]
            except Exception:
                pass
        return out

    def researched(self, tag=None):
        return sorted(filter(None, (self.tech_name(t)
                                    for t in _arr(self.p, self.tech_status(tag), TS_RESEARCHED))))

    def available_research(self, tag=None):
        """Technologies researchable by the static tree, with ZERO injected calls.

        Pure memory read for the live state (researched set + occupied slots) and
        common/technologies/*.txt for start_year / prerequisites.  The final
        legality check is still done by the game's own CanExecute inside RUN_CMD
        when hoi4_start_research posts SetResearch.
        """
        import re, techdata
        # 2026-11-16: this used to iterate `self._tech_by_name` without loading it,
        # so the very first call of a session died with
        # "'NoneType' object is not iterable" and four research slots sat empty
        # until a hoi4_start_research happened to warm the cache.  Load it here and
        # say so when the 30 s vtable scan is still running.
        self._load_tech_db(wait_s=60)
        if self._tech_by_name is None:
            raise RuntimeError(
                "technology database still not ready after waiting 30 s: the "
                "CTechnologyTemplate vtable scan has not finished (it is ~20-40 s "
                "on this build, longer on a cold ObjectIndex). Call "
                "hoi4_status once, wait a few seconds and retry.")
        defs = techdata.load()
        current_year = self.date()["year"]
        max_year = current_year + 1
        researched = set(self.researched(tag))
        active = {s["researching"] for s in self.research_slots(tag) if s.get("researching")}
        my_tag = self._c(tag).tag
        order = {"infantry": 0, "industry": 1, "computing": 2,
                 "armor": 3, "air": 4, "artillery": 5,
                 "support": 6, "naval": 7}
        rows = []
        for nm in self._tech_by_name:
            d = defs.get(nm)
            if not d or d.get("category") not in order:
                continue
            if nm in researched or nm in active:
                continue
            if d.get("always_no") or d.get("special_project_gated"):
                continue
            # Focus-granted modifier techs have no folder; they are not in the tree.
            if not d.get("folder"):
                continue
            allow_tags = d.get("allow_tags") or []
            if allow_tags and my_tag not in allow_tags:
                continue
            # GER_xxx is ours; any other uppercase tag prefix is foreign content.
            m = re.match(r"^([A-Z][A-Za-z0-9]{1,})_", nm)
            if m and m.group(1) != my_tag:
                continue
            if int(d.get("year") or 1936) > max_year:
                continue
            parents = d.get("parents") or []
            if parents and not any(par in researched for par in parents):
                continue
            year = int(d.get("year") or 1936)
            score = techdata.score_tech(nm, d.get("folder", ""), d.get("categories", []),
                                        year, current_year, tag=my_tag)
            rows.append((score, year, nm))
        rows.sort(key=lambda r: (-r[0], r[1], r[2]))
        return [nm for _, _, nm in rows]

    def available_research_detailed(self, tag=None, max_ahead_years=None):
        """带年份、超前年限、估算天数与互斥信息的纯只读可选科技目录（0 注入）。

        相比 `available_research()` 仅返回科技名列表，本方法显式暴露 `start_year`、
        `ahead_years`（按当前日期小数年份精确计算）、`cost_days`、`category`、`xor`，
        供 Standing Orders 编译器与 `hoi4_inspect(research_available)` 诚实计算超前惩罚。
        """
        import techdata
        names = list(self.available_research(tag) or [])
        defs = techdata.load() or {}
        d_now = {}
        try:
            d_now = self.date() or {}
        except Exception:
            d_now = {}
        yr_now = int(d_now.get("year") or 1936)
        mo_now = int(d_now.get("month") or 1)
        day_now = int(d_now.get("day") or 1)
        cur_year_frac = yr_now + (max(1, mo_now) - 1) / 12.0 + (max(1, day_now) - 1) / 365.0
        my_tag = None
        try:
            my_tag = self._c(tag).tag
        except Exception:
            my_tag = tag
        out = []
        for nm in names:
            info = techdata.tech_info(nm, defs=defs) or {}
            sy = int(info.get("start_year") or 1936)
            ahead = round(max(0.0, float(sy) - cur_year_frac), 2)
            if max_ahead_years is not None and ahead > float(max_ahead_years) + 1e-6:
                continue
            cost_days = None
            try:
                cost_days = self.tech_cost_days(nm)
            except Exception:
                cost_days = None
            if cost_days is None:
                cost_days = info.get("base_cost_days")
            score = techdata.score_tech(
                nm,
                info.get("folder") or "",
                info.get("categories") or [],
                sy,
                yr_now,
                tag=my_tag,
            )
            out.append({
                "key": nm,
                "start_year": sy,
                "ahead_years": ahead,
                "cost_days": cost_days,
                "category": info.get("category") or "other",
                "folder": info.get("folder") or "",
                "categories": info.get("categories") or [],
                "xor": info.get("xor") or [],
                "score": score,
            })
        return out

    def _check_foreground_guard(self):
        """纯只读检查游戏窗口是否处于前台（绝不调用 SetForegroundWindow 抢焦点）。

        仅在 Windows 真实进程上拦截；返回 None 表示通过，否则返回 (blocked_by, error, ws)。
        """
        if sys.platform != "win32":
            return None
        proc = getattr(self, "p", None)
        pid = getattr(proc, "pid", None)
        if not pid or not isinstance(pid, int):
            return None
        try:
            import windowstate as _ws
            ws = _ws.state(pid)
        except Exception:
            return None
        # 纯离线 mock 测试对象无真实句柄且找不到该 pid 的窗口，不误拦截
        if ws.get("hwnd") is None:
            if getattr(proc, "h", None):
                return (
                    "foreground",
                    "game window not found for pid %d: refusing to hijack the render thread." % pid,
                    ws,
                )
            return None
        if ws.get("minimized"):
            return (
                "foreground",
                "game window is minimized (pid %d): refusing to hijack the render thread. "
                "Restore and focus 'Hearts of Iron IV' and retry." % pid,
                ws,
            )
        if ws.get("focused") is False:
            return (
                "foreground",
                "game window is not in the foreground (pid %d): refusing to hijack the "
                "render thread. Bring 'Hearts of Iron IV' to the foreground and retry." % pid,
                ws,
            )
        return None

    @staticmethod
    def _classify_research_failure(res=None, exc=None):
        """区分科研失败类型：('environment', blocked_by) 必须中止，('content', blocked_by) 才可换候选。"""
        _ENV_CODES = {"foreground", "process_gone", "timeout", "injector_error"}
        if isinstance(res, dict):
            bb = res.get("blocked_by")
            if bb in _ENV_CODES:
                return ("environment", bb)
            if bb:
                return ("content", str(bb))
        msg = ""
        if exc is not None:
            msg = "%s: %s" % (type(exc).__name__, exc)
        elif isinstance(res, dict) and res.get("error"):
            msg = str(res.get("error"))
        low = msg.lower()
        if ("not in the foreground" in low or "restoredeviceobjects" in low
                or "window_minimized" in low or "window_hidden" in low
                or "window is minimized" in low or "window not found" in low):
            return ("environment", "foreground")
        if (isinstance(exc, TimeoutError) or "-32001" in low
                or "timed out" in low or "timeout" in low):
            return ("environment", "timeout")
        if (isinstance(exc, OSError) or "getlasterror=299" in low
                or "readprocessmemory" in low or "writeprocessmemory" in low
                or "openprocess" in low or "no cingameidler" in low
                or "process" in low and ("died" in low or "not found" in low or "exited" in low)):
            return ("environment", "process_gone")
        if exc is not None and type(exc).__name__ == "CallError":
            return ("environment", "injector_error")
        if "mutually exclusive" in low or "xor" in low:
            return ("content", "mutually_exclusive")
        if "canexecute refused" in low or "can_execute" in low:
            return ("content", "can_execute_refused")
        if "no free research slot" in low:
            return ("content", "no_free_slot")
        if isinstance(exc, KeyError) or "no such technology" in low:
            return ("content", "unknown_tech")
        if isinstance(res, dict) and res.get("can_execute") is False:
            return ("content", "can_execute_refused")
        return ("content", "rejected")

    def start_research(self, tech_name, slot=None, tag=None):
        self._load_tech_db(wait_s=60)
        c = self._c(tag)
        tmpl = (self._tech_by_name or {}).get(tech_name)
        if not tmpl:
            if getattr(self, "_tech_scan_pending", False):
                raise RuntimeError(
                    "technology table still loading: the CTechnologyTemplate "
                    "RTTI scan takes 20-40 s. Retry in about a minute.")
            raise KeyError("no such technology: %s" % tech_name)
        if slot is None:
            slots = self.research_slots(tag)
            # slot_busy is the authoritative occupancy flag; `researching` is
            # None whenever the name table is cold (2026-11-18).
            free = [s["slot"] for s in slots
                    if not s.get("slot_busy") or s.get("slot_done")]
            if not free:
                return {"ok": False, "blocked_by": "no_free_slot",
                        "failure_class": "content", "error": "no free research slot"}
            slot = free[0]
        # 2026-11-22 (复测 5 §3.8): 提前拦截已研究完的科技（包括槽内 slot_done=True
        # 刚完成但尚未清槽的科技），明确返回 already_researched 而非含糊的 not_occupied。
        try:
            _researched_now = set(self.researched(tag))
        except Exception:
            _researched_now = set()
        if tech_name in _researched_now:
            return {"ok": False, "tech": tech_name, "slot": slot,
                    "blocked_by": "already_researched",
                    "failure_class": "content",
                    "error": "%s is already researched" % tech_name}

        prev_slot_state = None
        try:
            _pre_slots = self.research_slots(tag)
            if 0 <= slot < len(_pre_slots):
                prev_slot_state = _pre_slots[slot]
        except Exception:
            prev_slot_state = None
        if (prev_slot_state and prev_slot_state.get("slot_done")
                and prev_slot_state.get("researching") == tech_name):
            return {"ok": False, "tech": tech_name, "slot": slot,
                    "blocked_by": "already_researched",
                    "failure_class": "content",
                    "error": "%s just finished in slot %d (slot_done=True)" % (tech_name, slot)}

        # 2026-11-19 (cheat-level, measured live): `CSetResearchCommand::CanExecute`
        # does NOT enforce the script `XOR = { ... }` block — only the UI's
        # `available` predicate does.  fill_research therefore started
        # concentrated_industry AND dispersed_industry at the same time, which no
        # player can do.  Refuse here.
        try:
            import techdata as _td
            _info = (_td.load() or {}).get(tech_name) or {}
            _xor = _info.get("xor") or []
            if _xor:
                _done = _researched_now
                _active = {s["researching"] for s in self.research_slots(tag)
                           if s.get("researching")}
                _bad = sorted(set(_xor) & (_done | _active))
                if _bad:
                    return {"ok": False, "tech": tech_name, "slot": slot,
                            "blocked_by": "mutually_exclusive",
                            "failure_class": "content",
                            "conflicts_with": _bad,
                            "error": (
                                "%s is mutually exclusive with %s (XOR block in "
                                "common/technologies). The engine would accept it "
                                "but a player cannot. Nothing was sent."
                                % (tech_name, ", ".join(_bad)))}
        except Exception:
            pass
        if sys.platform == "win32":
            # Real command channel on the PE build: hand-built CSetResearchCommand
            # + Post(session, cmd). See calling/cmdwin.py.
            import cmdwin
            try:
                res = cmdwin.set_research(self, tmpl, slot, c.tag_id)
            except Exception as exc:
                fcls, fbb = self._classify_research_failure(exc=exc)
                return {
                    "ok": False,
                    "tech": tech_name,
                    "slot": slot,
                    "blocked_by": fbb,
                    "failure_class": fcls,
                    "posted": 0,
                    "error": str(exc),
                }
            if res.get("can_execute") is False:
                return {
                    "ok": False,
                    "tech": tech_name,
                    "slot": slot,
                    "blocked_by": "can_execute_refused",
                    "failure_class": "content",
                    "can_execute": False,
                    "posted": 0,
                    "error": res.get("error") or "CSetResearchCommand::CanExecute refused",
                }
            self._settle()
            after = self.research_slots(tag)
            # 2026-11-22 (复测 5 §3.8 / §3.9): 严格校验槽位确实被新科技占住：
            # 1) 必须 slot_busy=True 且 slot_done=False（防止把刚完成未清槽的旧科技误判成 ok）；
            # 2) 若发送前该槽已有旧科技且处于 slot_done，必须看到 tech_ptr 变更；
            # 3) 二次回读 0.15s 防止瞬态未清槽造成的假 ok（随后槽又变空）。
            def _slot_matches(got):
                if not got or not got.get("slot_busy") or got.get("slot_done"):
                    return False
                if not (got.get("researching") in (None, tech_name)):
                    return False
                if prev_slot_state and prev_slot_state.get("slot_done"):
                    if (prev_slot_state.get("tech_ptr")
                            and got.get("tech_ptr") == prev_slot_state.get("tech_ptr")):
                        return False
                return True

            got = after[slot]
            ok = _slot_matches(got)
            if ok:
                self._settle(0.15)
                after = self.research_slots(tag)
                got = after[slot]
                ok = _slot_matches(got)
            out = {"ok": ok,
                   "slot": slot,
                   "researching": got.get("researching"),
                   "slot_busy": got.get("slot_busy"),
                   "slot_done": got.get("slot_done"),
                   "can_execute": res.get("can_execute"),
                   "posted": res.get("posted"),
                   "drained": res.get("drained"),
                   "error": res.get("error")}
            if not ok:
                try:
                    if tech_name in set(self.researched(tag)):
                        out["failure_class"] = "content"
                        out["blocked_by"] = "already_researched"
                        out["error"] = out.get("error") or ("%s is already researched" % tech_name)
                        return out
                except Exception:
                    pass
                fcls, fbb = self._classify_research_failure(res=out)
                out["failure_class"] = fcls
                out["blocked_by"] = fbb if fbb != "rejected" else "not_occupied"
            return out
        with self:
            if FN_CAN_RESEARCH is not None and \
                    not self.call(FN_CAN_RESEARCH, (self.tech_status(tag), tmpl, 0)) & 0xFF:
                return {"ok": False, "blocked_by": "can_execute_refused",
                        "failure_class": "content",
                        "error": "%s su an arastirilamaz" % tech_name}
            self.cmd.send("SetResearch", tag=c.tag_id, tech_ptr=tmpl, slot=slot, flag=0)
        self._settle()

        def _ok():
            s = self.research_slots(tag)[slot]
            return bool(s.get("slot_busy")) and \
                s.get("researching") in (None, tech_name)
        ok = self._wait_until(_ok)
        after = self.research_slots(tag)
        out = {"ok": ok, "slot": slot, "researching": after[slot]["researching"],
               "slot_busy": after[slot].get("slot_busy")}
        if not ok:
            out["blocked_by"] = "not_occupied"
            out["failure_class"] = "content"
        return out

    def fill_all_research_slots(self, preferred_techs=None, tag=None, max_attempts=30):
        """Fill EVERY empty research slot, trying preferred techs first then the
        score-sorted available list.  After each command the slot memory is read
        back; content rejections (XOR / CanExecute) move to the next candidate,
        whereas environment errors (foreground guard / dead process / timeout)
        abort immediately without degrading to lower-priority technologies."""
        slots = self.research_slots(tag)
        # 2026-11-21 (P2-12): housekeep could report "4 slots still empty" after a
        # whole run.  The candidate list comes from the technology DATABASE, which
        # is cold right after a server start, and an empty candidate list makes
        # this a silent no-op.  Warm it explicitly first (cheap when already warm).
        try:
            self._load_tech_db(wait_s=5.0)
        except Exception:
            pass
        # 2026-11-18: occupancy must come from slot_busy, never from the tech NAME.
        # `researching` is None whenever the name table is cold, which made this
        # tool believe four busy slots were empty and post four more researches.
        # 2026-11-19: a slot whose tech hit 100 % still reports busy for a tick or
        # two; include those (slot_done) so the free slot is not missed.
        empty = [s["slot"] for s in slots
                 if not s.get("slot_busy") or s.get("slot_done")]
        empty_before = list(empty)
        attempts = []
        preferred = [str(x) for x in (preferred_techs or []) if x]
        attempted = set()
        if not empty:
            return {"ok": True, "empty_before": [], "empty_after": [],
                    "filled": 0, "slots": slots, "attempts": attempts}
        # 2026-11-22 (P3 前台守卫修复): 入口先做一次纯只读前台检查，
        # 游戏不在前台时立刻拒绝、一个槽都不填，绝不尝试候选也不静默降级。
        fg_guard = self._check_foreground_guard()
        if fg_guard is not None:
            fg_code, fg_err, fg_ws = fg_guard
            return {
                "ok": False,
                "aborted": True,
                "blocked_by": fg_code,
                "failure_class": "environment",
                "error": fg_err,
                "window": fg_ws,
                "empty_before": empty_before,
                "empty_after": empty_before,
                "filled": 0,
                "slots": slots,
                "attempts": attempts,
            }
        # 2026-11-16: a failed tech-db scan must not abort the whole fill -- the
        # preferred list is still worth trying, and start_research warms the cache
        # itself.
        try:
            avail = self.available_research(tag)
            avail_err = None
        except Exception as e:
            avail, avail_err = [], "%s: %s" % (type(e).__name__, e)
        if not avail:
            # 2026-11-21 (P2-12): one more attempt after actually waiting for the
            # background scan.  Before this, a cold process returned zero
            # candidates and the whole step was a silent no-op that still left
            # `ok` true in some paths.
            try:
                self._load_tech_db(wait_s=10.0)
                avail = self.available_research(tag)
                avail_err = None
            except Exception as e:
                avail_err = "%s: %s" % (type(e).__name__, e)
        # 2026-11-22 (复测 5 §3.8): 候选池（含调用方显式传入的 preferred）必须先减去
        # 已研究全集以及当前任何科研槽中正在/刚完成（slot_done）的科技，避免把刚 100%
        # 完成的科技（如 oil_processing）重复再选一遍。
        try:
            already_done = set(self.researched(tag))
        except Exception:
            already_done = set()
        in_slots = {s.get("researching") for s in slots if s.get("researching")}
        exclude_techs = already_done | in_slots
        candidates = [
            x for x in (preferred + [y for y in avail if y not in preferred])
            if x not in exclude_techs
        ]
        if not candidates:
            return {"ok": False, "empty_before": empty_before, "empty_after": empty_before,
                    "filled": 0, "slots": slots, "attempts": attempts,
                    "available_error": avail_err,
                    "error": avail_err or "no candidate technology to try"}
        aborted_reason = None
        aborted_error = None
        for slot in list(empty):
            for tech in candidates:
                if tech in attempted:
                    continue
                attempted.add(tech)
                exc_caught = None
                try:
                    res = self.start_research(tech, slot=slot, tag=tag)
                except Exception as e:
                    exc_caught = e
                    fcls, fbb = self._classify_research_failure(exc=e)
                    res = {"ok": False, "blocked_by": fbb,
                           "failure_class": fcls, "error": str(e)}
                ok = bool(res.get("ok"))
                # 2026-11-22 (复测 5 §3.9): 以目标槽真实占用（slot_busy=True 且 slot_done=False）
                # 为最终准绳，防止单条 attempt 报 ok=True 但最终槽仍空。
                if ok and ("slot_busy" in res or "slot_done" in res):
                    if not res.get("slot_busy") or res.get("slot_done"):
                        ok = False
                        res = dict(res)
                        res["ok"] = False
                        res.setdefault("blocked_by", "not_occupied")
                        res.setdefault("failure_class", "content")
                if ok:
                    r_class, r_code = "ok", None
                else:
                    r_class = res.get("failure_class")
                    r_code = res.get("blocked_by")
                    if not r_class or not r_code:
                        r_class, r_code = self._classify_research_failure(
                            res=res, exc=exc_caught)
                entry = {
                    "slot": slot,
                    "tech": tech,
                    "ok": ok,
                    "reason_class": r_class,
                    "blocked_by": r_code,
                    "error": res.get("error"),
                }
                attempts.append(entry)
                if ok:
                    break
                # 2026-11-22 (P3): 环境错误（前台焦点丢失/进程消失/超时/注入失败）
                # 必须立即中止整个 fill，绝不把高优科技降级成低分候选。
                if r_class == "environment":
                    aborted_reason = r_code
                    aborted_error = res.get("error") or ("aborted due to %s" % r_code)
                    break
                if len(attempts) >= max_attempts:
                    break
            if aborted_reason is not None or len(attempts) >= max_attempts:
                break
        try:
            after = self.research_slots(tag)
            empty_after = [s["slot"] for s in after
                           if not s.get("slot_busy") or s.get("slot_done")]
        except Exception:
            after = slots
            empty_after = list(empty_before)
        out = {"ok": (not empty_after) and (aborted_reason is None),
               "empty_before": empty_before,
               "empty_after": empty_after,
               "filled": max(0, len(empty_before) - len(empty_after)),
               "tech_db_warm": self._tech_by_name is not None,
               "slots": after, "attempts": attempts}
        if aborted_reason is not None:
            out["aborted"] = True
            out["blocked_by"] = aborted_reason
            out["failure_class"] = "environment"
            out["error"] = aborted_error
        if avail_err:
            out["available_error"] = avail_err
        return out

    def _doctrine_templates(self):
        # 2026-11-15: these 12 objects are STATIC for the whole process lifetime
        # (game-data templates, loaded once at boot). The original cache key was
        # (pid, base) but callers also cleared _doc_cache_key on module reload,
        # which forced oi.instances() -> `_sanitised()` -> a full multi-GB heap
        # rescan on a stale/expired objindex cache. That rescan is what timed out
        # hoi4_unlock_doctrine (x3). Keep the first non-empty result forever for
        # this pid/base; only a genuinely empty result may be retried.
        key = (self.p.pid, self.p.base)
        cached = getattr(self, "_doc_cache", None)
        if getattr(self, "_doc_cache_key", None) == key and cached:
            return cached
        found = {}
        if sys.platform == "win32":
            # The Linux scan below looks for the vtable 0x447EFA0, which is a Linux
            # VA; on the PE build it matches nothing and every doctrine lookup failed
            # ("template not found").  There are exactly 12 CGrandDoctrineTemplate
            # objects and their script key is a std::string at +0x28
            # ("GRAND_DOCTRINE_MOBILE_WARFARE" ...), which is also what DOCTRINE_LOC
            # holds -- so the object index answers it directly.
            # 2026-11-23 (C-6 学说冷启动修复): 冷启动/换档瞬间 oi.instances() 可能返回空；
            # 若为空立刻回退单类虚表直扫 win_scan_class("CGrandDoctrineTemplate", use_cache=False)，
            # 且严禁把空 dict `{}` 写入 _doc_cache 锁死后续调用！
            objs = []
            try:
                objs = list(self.oi.instances("CGrandDoctrineTemplate") or [])
            except Exception:
                objs = []
            if not objs:
                try:
                    objs = list(self.win_scan_class("CGrandDoctrineTemplate", use_cache=False) or [])
                except Exception:
                    objs = []
            for obj in objs:
                try:
                    nm = self.p.stdstring(obj + 0x28)
                except Exception:
                    nm = None
                if nm:
                    found[nm] = obj
            if found:
                self._doc_cache_key = key
                self._doc_cache = found
            return found
        target = self.p.va(0x447EFA0)
        for m in self.p.regions(writable=False):
            a = m["start"]
            while a < m["end"]:
                n = min(4 << 20, m["end"] - a)
                n -= n % 8
                if n <= 0:
                    break
                buf = self.p.try_read(a, n)
                if buf:
                    for i in range(0, len(buf) - 7, 8):
                        if struct.unpack_from("<Q", buf, i)[0] == target:
                            obj = a + i
                            nm = item_name(self.p, obj + 0x20)
                            if nm:
                                found[nm] = obj
                a += n
            if len(found) >= 12:
                break
        self._doc_cache_key = key
        self._doc_cache = found
        return found

    def unlock_doctrine(self, doctrine, tag=None):
        """Unlock a grand doctrine (mobile_warfare / superior_firepower / ...).

        The command is NDoctrines::CUnlockGrandDoctrineCommand (vtable 0x4360C28,
        +0x28 = CGrandDoctrineTemplate*, +0x30 = 0). Success is verified by
        finding the template pointer in the country's doctrine store at
        CCountry+0x15E0, not by trusting the command's return value.
        """
        loc = self.DOCTRINE_LOC.get(str(doctrine or "").lower())
        if not loc:
            return {"ok": False, "error": "unknown doctrine: %s" % doctrine,
                    "available": sorted(self.DOCTRINE_LOC)}
        templates = self._doctrine_templates()
        tpl = templates.get(loc)
        if not tpl:
            return {"ok": False, "error": "template not found: %s" % loc,
                    "templates": sorted(templates)}
        if sys.platform == "win32":
            # 2026-11-15 (crash #3): send_command() built CUnlockGrandDoctrineCommand
            # from an unverified pecmd layout with only +0x28 set. CanExecute
            # (0x1B10310) rejects +0x30 <= 0 ("#~ invalid country tag") and Execute
            # dereferences a null country, which killed hoi4.exe. The PE layout is
            # now disassembled (see cmdwin.make_unlock_doctrine): +0x28 = doctrine
            # template ptr, +0x30 = country tag id, vtable 0x2AAA8A0, size 0x38.
            import cmdwin
            tag_id = int(self._c(tag).tag_id)
            can0 = cmdwin.can_unlock_doctrine(self, tpl, tag_id)
            if can0 is False:
                return {"ok": False, "doctrine": doctrine, "localization": loc,
                        "template": hex(tpl), "can_execute": 0,
                        "error": ("the game's own CUnlockGrandDoctrineCommand::"
                                  "CanExecute refuses %s: its prerequisite is not met, "
                                  "a land doctrine is already assigned, or there is "
                                  "not enough army XP. Try grand_battleplan / "
                                  "mass_assault / superior_firepower." % loc)}
            cmd = cmdwin.make_unlock_doctrine(self, tpl, tag_id)
            # A paused game never drains the queue (§29.8) -- run the clock
            # briefly and restore the pause state before reading anything back.
            res = cmdwin.post_drained(self, cmd)
            res.update(cmd=hex(cmd), verified=True)
            rv = int(res.get("posted") or 0)
            can1 = cmdwin.can_unlock_doctrine(self, tpl, tag_id)
            # CanExecute 1 -> 0 proves the doctrine was assigned (mutual
            # exclusivity now blocks re-assigning it). CCountry+0x15E0 is a
            # stale Linux offset and always reads empty on PE, so it is dropped.
            applied = bool(can0 is True and can1 is False)
            accepted = bool(rv & 0xFF)
            return {"ok": applied or accepted, "doctrine": doctrine,
                    "localization": loc, "template": hex(tpl),
                    "can_execute_before": can0, "can_execute_after": can1,
                    "assigned": applied, "accepted": accepted, "command": res,
                    "note": (None if applied else
                             "command posted but CanExecute did not flip; the "
                             "doctrine may already be assigned or a popup held "
                             "the clock (drained=%s)" % res.get("drained"))}
        res = self.send_command("UnlockGrandDoctrine", {"0x28": tpl}, tag)
        self._settle(0.6)
        stored = False
        try:
            sub = self.p.u64(self._c(tag).ptr + 0x15E0)
            if sub:
                data = self.p.read(sub, 0x2000)
                stored = any(struct.unpack_from("<Q", data, i)[0] == tpl
                             for i in range(0, len(data) - 7, 8))
        except Exception:
            pass
        accepted = bool((res or {}).get("run_result") or 0)
        return {"ok": bool(stored) or accepted, "doctrine": doctrine,
                "localization": loc, "template": hex(tpl), "stored": stored,
                "accepted": accepted, "command": res}

    def scientists(self, tag=None, limit=60):
        p = self.p
        c = self._c(tag)
        pref = (c.tag + "_").lower()
        out = []
        if sys.platform == "win32":
            # oi.instances("CScientist") is empty on the PE build; the country's
            # own roster is CCountryCharacters+0xA0 (GER 17) plus +0xE8 (4).
            cc = p.u64(c.ptr + 0xFF0)
            pool = list(_arr(p, cc, 0xA0)) + list(_arr(p, cc, 0xE8)) if cc else []
        else:
            pool = list(self.oi.instances("CScientist"))
        for s in pool:
            nm = None
            for off in range(0, 0x120, 8):
                v = item_name(p, s + off)
                if v and v.isprintable() and 2 < len(v) < 48:
                    nm = v
                    break
            if nm and (not pref or nm.lower().startswith(pref) or True):
                out.append({"name": nm, "ptr": hex(s)})
            if len(out) >= limit:
                break
        return out

    def special_projects(self, tag=None):
        """Special-project pool.

        2026-11-16: CO_PROJECT_POOL / CO_PROGRAM_STATUS read a null pointer and an
        unrelated CArmyUpgradesStatus on the PE build, and
        'CSpecialProjectsConsumer' (the real owner) is not reachable from
        CCountry (scanned 0x0..0x3200, 0 hits). Report that instead of handing
        back `pool: null` plus a bare pointer string.
        """
        p = self.p
        c = self._c(tag)
        if sys.platform == "win32":
            # PE 1.19.5.0: CCountry has no CProjectPool pointer (CCountry+0xFA8 is
            # CProgramStatus, which holds *faction facility programmes* such as
            # "land_facility", not special projects).  CProjectPool is an RTTI
            # class, so it is located by vtable scan instead.
            pools = self.win_scan_class("CProjectPool")
            prog = p.u64(c.ptr + 0xFA8)
            res = {"ok": True, "available": True, "source": "pe-rtti vtable scan",
                   "pool": None, "pool_instances": len(pools),
                   "program_status": hex(prog) if prog else None,
                   "program_status_class": self.oi.class_of(prog) if prog else None,
                   "programs": []}
            for pr in _arr(p, prog, 0x20) if prog else []:
                nm = item_name(p, pr + 0x20)
                res["programs"].append({"program": nm, "ptr": hex(pr)})
            if pools:
                res["note"] = ("CProjectPool resolves by vtable (%d live instances, one per "
                               "country) but no field inside it points back at a CCountry, so "
                               "a pool cannot be attributed to a tag yet. `programs` is the "
                               "verified part: CCountry+0xFA8 CProgramStatus -> CPdxArray"
                               "<CProgram*> at +0x20, program key at +0x20." % len(pools))
            return res
        pool = p.u64(c.ptr + CO_PROJECT_POOL)
        prog = p.u64(c.ptr + CO_PROGRAM_STATUS)
        if sys.platform == "win32" and not pool:
            return {"ok": False, "available": False, "pool": None,
                    "program_status": None,
                    "error": "special_projects has no verified container on the PE build",
                    "detail": "CCountry+CO_PROJECT_POOL is null and "
                              "CSpecialProjectsConsumer is not reachable from CCountry",
                    "program_status_class": self.oi.class_of(prog) if prog else None}
        res = {"pool": hex(pool) if pool else None, "program_status": hex(prog) if prog else None}
        if pool:
            from probe import describe
            res["pool_arrays"] = [r for r in describe(p, pool, self.oi, span=0x200) if r["size"]]
        return res

    def tech_folders(self):
        """Technology and doctrine folders."""
        p = self.p
        db = self.oi.singleton("CTechnologyDatabase")
        out = []
        for f in _arr(p, db, 0xA8):
            nm = None
            for off in range(0, 0x200, 8):
                v = item_name(p, f + off)
                if v and v.isprintable() and 2 < len(v) < 48:
                    nm = v
                    break
            out.append({"name": nm, "ptr": hex(f)})
        return out
