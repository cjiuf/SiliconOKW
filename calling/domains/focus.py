"""国策树读取、前置/互斥解析、可点国策筛选、启停国策与持续国策领域 Mixin。"""
from __future__ import annotations

import offsets as O
import sys

from ._shared import (
    CO_ARMIES,
    CO_FOCUS_PROGRESS,
    CO_FOCUS_TREE,
    FP_COMPLETED,
    FP_CURRENT,
    FT_FOCUSES,
    NF_ID,
    NF_KEY,
    NF_MUTEX,
    NF_NAME,
    NF_PREREQ,
    _arr,
    _count,
    item_name,
    read_scoped,
)

class FocusMixin:
    """国策树读取、前置/互斥解析、可点国策筛选、启停国策与持续国策领域 Mixin。"""

    GER_SNOWBALL = ("GER_anschluss", "GER_demand_sudetenland", "GER_end_of_czechoslovakia")
    # (置顶分数, 最低师数, 最低年份, 是否要求和平) — the manpower gates are the game's
    # own: Anschluss needs >500k men under arms (~54 divisions), Sudetenland >725k
    # (~74). Below the gate the focus is reported in `blocked`, never recommended.
    GER_SNOWBALL_CHAIN = {
        "GER_anschluss":               (2000, 54, 1937, True),
        "GER_demand_sudetenland":      (1990, 74, 1938, True),
        "GER_first_vienna_award":      (1985,  0, 1938, True),
        # 1.19.4.1 calls it GER_fate_of_czechoslovakia (the 1.19.2 text said
        # GER_end_of_czechoslovakia, which does not exist in this focus tree).
        "GER_fate_of_czechoslovakia":  (1980,  0, 1938, True),
        "GER_end_of_czechoslovakia":   (1980,  0, 1938, True),
        # GER_danzig_for_slovakia is mutually_exclusive with GER_danzig_or_war
        # (germany.txt:11777). Taking it in 1938.09 is exactly what locked the Polish
        # casus belli in every 2026-11-05 run — it is blacklisted, never scored.
        # GER_demand_memel does not exist in 1.19.4.1: the Memel/Danzig claims come
        # from GER_reassert_eastern_claims, whose only prerequisite is GER_anschluss.
        "GER_reassert_eastern_claims": (1975,  0, 1939, True),
        "GER_molotov_ribbentrop_pact": (1965,  0, 1939, True),
        "GER_mol_rib_pact":            (1965,  0, 1939, True),
        # has_army_manpower > 949999 = ~100 divisions in the field.
        "GER_danzig_or_war":           (1955, 100, 1939, True),
    }
    # Repeatable filler focuses that would otherwise win every empty slot.
    GER_LOW_PRIORITY = {"GER_the_triumphant_will": -100}
    GER_OPENING_TOP = {
        "GER_remilitarize_the_rhineland": 1850,
        "GER_rhineland":                  1850,
        # Anschluss is gated on GER_heed_von_neuraths_concerns OR
        # GER_reorganize_the_wehrmacht (focus_detail, checked live 2026-11-05).
        # Without one of them the annexation chain can NEVER become selectable —
        # that is why 28 focuses passed and Anschluss never appeared once.
        "GER_reorganize_the_wehrmacht":   1880,
        "GER_heed_von_neuraths_concerns": 1875,
        "GER_the_four_year_plan":         1840,
        "GER_four_year_plan":             1840,
        "GER_autarky_efforts":            1830,
        "GER_construct_the_reichsautobahn": 1820,
        "GER_kdf_wagen_factories":        1810,
        "GER_extra_tech_slot":            1800,
    }

    VCAN_FOCUS_WIN = 0x11F2940

    def focus_tree(self, tag=None):
        c = self._c(tag)
        tree = self.p.u64(c.ptr + CO_FOCUS_TREE)
        return _arr(self.p, tree, FT_FOCUSES) if tree else []

    def _focus_info(self, f):
        p = self.p
        info = {
            "key": item_name(p, f + NF_KEY),
            "name": item_name(p, f + NF_NAME),
            "id": p.i32(f + NF_ID),
            "ptr": f,
            "prerequisites": _count(p, f, NF_PREREQ),
            "mutually_exclusive": _count(p, f, NF_MUTEX),
        }
        # 2026-11-19: on the PE build NF_PREREQ / NF_MUTEX do not point at focus
        # arrays (the tool printed `prerequisites: 475` and `mutually_exclusive:
        # 475` for every focus, which is a heap count, not a relationship). The
        # script files are the authority, and focusplan already parses them.
        if sys.platform == "win32" and info["key"]:
            try:
                import focusplan
                m = (focusplan.load().get("focuses") or {}).get(info["key"]) or {}
                if m:
                    groups = [g for g in (m.get("prereq_groups") or []) if g]
                    info["prereq_groups"] = groups
                    info["prerequisites"] = len(groups)
                    info["mutually_exclusive"] = len(m.get("mutex") or [])
                    info["relationship_source"] = "common/national_focus"
            except Exception:
                pass
        return info

    def _focus_prereq_groups(self, f):
        """Each prerequisite block is an OR-group of focus keys; all blocks are AND."""
        p = self.p
        out = []
        for d in _arr(p, f, NF_PREREQ):
            group = []
            for ff in _arr(p, d, 0x08):
                k = item_name(p, ff + NF_KEY)
                if k:
                    group.append(k)
            out.append(group)
        return out

    def _focus_mutex_keys(self, f):
        p = self.p
        out = []
        for x in _arr(p, f, NF_MUTEX):
            k = item_name(p, x + NF_KEY)
            if k:
                out.append(k)
            else:
                for y in _arr(p, x, 0x08):
                    kk = item_name(p, y + NF_KEY)
                    if kk:
                        out.append(kk)
        return out

    def focuses(self, tag=None, only_startable=False):
        """Focus list.  only_startable=True now means what a player means by it.

        2026-11-19: the old filter was `if info["prerequisites"]: continue`, i.e.
        "this focus has at least one prerequisite object" — which is true for
        every non-root focus AND false for every root one, regardless of what is
        already completed.  So the tool listed the 1936 opening focuses as
        "startable" in 1938, long after they were done, and hid every focus whose
        prerequisites were actually satisfied.

        The real predicate is: not completed, not the focus in progress, every
        `prerequisite` block satisfied (OR inside a block, AND across blocks),
        and no completed mutually-exclusive focus.  On the PE build those blocks
        are not readable from CNationalFocus, so they come from the parsed
        script files (focusplan schema 2), which is also what hoi4_focus_detail
        uses.
        """
        out = []
        completed = set()
        cur = 0
        static = None
        if only_startable:
            completed = set(self.completed_focuses(tag))
            prog = self.p.u64(self._c(tag).ptr + CO_FOCUS_PROGRESS)
            cur = self.p.u64(prog + FP_CURRENT) if prog else 0
            if sys.platform == "win32":
                try:
                    import focusplan
                    static = focusplan.load().get("focuses", {})
                except Exception:
                    static = {}
        for f in self.focus_tree(tag):
            info = self._focus_info(f)
            if only_startable:
                k = info["key"]
                if not k or k in completed or (cur and f == cur):
                    continue
                if static is not None:
                    m = static.get(k) or {}
                    groups = [g for g in (m.get("prereq_groups") or []) if g]
                    if any(not any(g in completed for g in grp) for grp in groups):
                        continue
                    if any(x in completed for x in (m.get("mutex") or [])):
                        continue
                elif info["prerequisites"]:
                    continue
            info["completed"] = info["key"] in completed
            out.append(info)
        return out

    def focus_block_reason(self, key, tag=None):
        """Why `key` cannot be started right now (None when nothing blocks it).

        Used by hoi4_set_focus when the engine takes the command but does not
        actually switch the focus — before this the tool answered
        `ok: false, posted: 1` with no explanation at all.
        """
        completed = set(self.completed_focuses(tag))
        if key in completed:
            return "already completed"
        prog = self.p.u64(self._c(tag).ptr + CO_FOCUS_PROGRESS)
        cur = self.p.u64(prog + FP_CURRENT) if prog else 0
        if cur:
            cur_key = item_name(self.p, cur + NF_KEY)
            if cur_key and cur_key != key:
                return ("another focus is already in progress (%s); "
                        "call drop_focus (or hoi4_drop_focus) first" % cur_key)
        try:
            import focusplan
            m = (focusplan.load().get("focuses") or {}).get(key) or {}
        except Exception:
            m = {}
        groups = [g for g in (m.get("prereq_groups") or []) if g]
        unmet = [grp for grp in groups if not any(g in completed for g in grp)]
        if unmet:
            return ("prerequisites not met: " + " AND ".join(
                "(" + " OR ".join(grp) + ")" for grp in unmet))
        mx = [x for x in (m.get("mutex") or []) if x in completed]
        if mx:
            return "mutually exclusive with completed focus %s" % ", ".join(mx)
        return None

    def _wait_until(self, fn, timeout=4.0, step=0.25):
        """Komut kuyruga dusup islenene kadar bekle (Post asenkron)."""
        import time as _t
        end = _t.time() + timeout
        while _t.time() < end:
            try:
                if fn():
                    return True
            except Exception:
                pass
            _t.sleep(step)
        return False

    def _focus_cost_days(self, key):
        if not key:
            return None
        try:
            import focusplan
            m = (focusplan.load().get("focuses") or {}).get(key) or {}
            days = m.get("days")
            if days is not None and float(days) > 0:
                return float(days)
        except Exception:
            pass
        return 70.0

    @read_scoped("current_focus", key_fn=lambda self, tag=None: (self._rs_tag_key(tag),))
    def current_focus(self, tag=None):
        c = self._c(tag)
        prog = self.p.u64(c.ptr + CO_FOCUS_PROGRESS)
        if not prog:
            return None
        cur = self.p.u64(prog + FP_CURRENT)
        if not cur:
            return None
        info = self._focus_info(cur)
        info["completed_count"] = _count(self.p, prog, FP_COMPLETED)
        # 2026-11-22 (复测 5 §3.6): CFocusProgress+0x38 (fixed 1e5) 保存当前国策已推进天数，
        # 结合脚本耗时即可算出 days_left 与完成百分比，不再让模型盲等 70 天。
        try:
            days_done = round(self.p.i64(prog + 0x38) / float(O.FIXED), 3)
            cost_days = self._focus_cost_days(info.get("key"))
            info["progress"] = days_done
            info["days_done"] = round(days_done, 2)
            info["cost_days"] = cost_days
            if cost_days and cost_days > 0:
                info["days_left"] = round(max(0.0, cost_days - days_done), 2)
                info["percent"] = round(min(100.0, days_done / cost_days * 100.0), 1)
        except Exception:
            pass
        return info

    def find_focus(self, key, tag=None):
        for f in self.focus_tree(tag):
            if item_name(self.p, f + NF_KEY) == key:
                return f
        return None

    _FOCUS_AVAIL_CACHE = {}

    @classmethod
    def _focus_available_snippet(cls, key):
        """纯文件只读：从 HOI4 `common/national_focus/*.txt` 提取指定国策的 `available = { ... }` 摘要（D-09/D-10）。"""
        k = str(key or "").strip()
        if not k:
            return None
        if k in cls._FOCUS_AVAIL_CACHE:
            return cls._FOCUS_AVAIL_CACHE[k]
        snippet = None
        try:
            import glob as _glob, os as _os, re as _re
            import mapdata as _md
            fdir = _os.path.join(_md.GAME, "common", "national_focus")
            if _os.path.isdir(fdir):
                pat = _re.compile(r"\bid\s*=\s*" + _re.escape(k) + r"\b")
                for fn in sorted(_glob.glob(_os.path.join(fdir, "*.txt"))):
                    try:
                        with open(fn, encoding="utf-8", errors="replace") as _fh:
                            txt = _fh.read()
                    except OSError:
                        continue
                    m = pat.search(txt)
                    if not m:
                        continue
                    # 定位本 focus 块内紧随其后的 available = { ... }
                    chunk = txt[m.end():m.end() + 4000]
                    next_foc = _re.search(r"\bfocus\s*=\s*\{", chunk)
                    if next_foc:
                        chunk = chunk[:next_foc.start()]
                    ma = _re.search(r"\bavailable\s*=\s*\{", chunk)
                    if not ma:
                        break
                    pos = ma.end()
                    depth = 1
                    while pos < len(chunk) and depth > 0:
                        ch = chunk[pos]
                        if ch == "{":
                            depth += 1
                        elif ch == "}":
                            depth -= 1
                        pos += 1
                    raw = chunk[ma.end():max(ma.end(), pos - 1)]
                    lines = [ln.split("#", 1)[0].strip() for ln in raw.splitlines()]
                    flat = " ".join(ln for ln in lines if ln)
                    if flat:
                        snippet = flat[:260] + ("..." if len(flat) > 260 else "")
                    break
        except Exception:
            snippet = None
        cls._FOCUS_AVAIL_CACHE[k] = snippet
        return snippet

    def set_focus(self, key, tag=None):
        c = self._c(tag)
        f = self.find_focus(key, tag)
        if not f:
            raise KeyError("focus not found: %s" % key)
        if sys.platform == "win32":
            import cmdwin
            r = cmdwin.set_national_focus(self, f, c.tag_id)
            self._settle()
            prog = self.p.u64(c.ptr + CO_FOCUS_PROGRESS)
            got = self.p.u64(prog + FP_CURRENT) if prog else 0
            r.update({"focus": key, "current": hex(got) if got else None,
                      "ok": bool(r.get("ok")) and got == f})
            if not r["ok"]:
                reason = r.get("reason") or self.focus_block_reason(key, tag)
                if not reason:
                    avail_snip = self._focus_available_snippet(key)
                    if avail_snip:
                        r["available_trigger"] = avail_snip
                        reason = (
                            "the engine did not switch to %s; its script `available` trigger is not met: %s"
                            % (key, avail_snip)
                        )
                    else:
                        reason = (
                            "the engine took the command but did not switch the focus; "
                            "its `available` trigger (manpower / army ratio / political "
                            "power / war state) is the remaining candidate — the same "
                            "focus keeps failing until that condition changes"
                        )
                r["reason"] = reason
            return r
        raise NotImplementedError("set_focus requires Windows PE 1.19.5.0")

    def set_focus_ptr(self, focus_ptr, tag=None):
        """Start a national focus by its LIVE tree pointer."""
        c = self._c(tag)
        f = int(focus_ptr)
        import cmdwin
        p = self.p
        r = cmdwin.set_national_focus(self, f, c.tag_id)
        self._settle()
        prog = p.u64(c.ptr + CO_FOCUS_PROGRESS)
        got = p.u64(prog + FP_CURRENT) if prog else 0
        r.update({"focus_ptr": hex(f), "current": hex(got) if got else None,
                  "ok": bool(r.get("ok")) and got == f})
        if not r["ok"] and not r.get("reason"):
            k = item_name(p, f + NF_KEY)
            r["reason"] = self.focus_block_reason(k, tag) if k else (
                "the pointer does not resolve to a focus in the live tree")
        return r

    def drop_focus(self, tag=None):
        c = self._c(tag)
        before = self.current_focus(tag)
        if not before:
            return {"ok": False, "posted": 0, "was": None, "current": None,
                    "reason": "no focus in progress"}
        import cmdwin
        r = cmdwin.drop_national_focus(self, c.tag_id)
        self._settle()
        prog = self.p.u64(c.ptr + CO_FOCUS_PROGRESS)
        got = self.p.u64(prog + FP_CURRENT) if (prog and FP_CURRENT is not None) else 0
        ok = bool(r.get("ok")) and got == 0
        r.update({"was": before.get("key") if isinstance(before, dict) else None,
                  "current": hex(got) if got else None,
                  "ok": ok})
        if not ok and not r.get("reason") and not r.get("error"):
            r["reason"] = "focus remained active after drop command (game may be paused)"
        return r

    def focus_detail(self, key, tag=None):
        """Everything about one focus: prerequisites, mutual exclusions, filters.

        2026-11-18: prerequisites and mutual exclusions used to be read out of
        the live CFocus object through NF_PREREQ / NF_MUTEX, whose offsets were
        never verified on the PE build -- the result was binary garbage
        (`prerequisite_keys` full of mojibake). The authoritative source is the
        game's own script, so parse that (focusplan) and use memory only for the
        live pointer / name / completion state.
        """
        import focusplan
        stat = (focusplan.load().get("focuses") or {}).get(key)
        try:
            f = self.find_focus(key, tag)
        except Exception:
            f = None
        if not stat and not f:
            raise KeyError("no such focus: %s" % key)
        info = {}
        if f:
            try:
                info = dict(self._focus_info(f) or {})
            except Exception:
                info = {}
        info.setdefault("id", key)
        info.setdefault("key", key)
        groups = (stat or {}).get("prereq_groups") or []
        info["prerequisite_groups"] = groups          # outer list = AND, inner = OR
        info["prerequisite_keys"] = [x for g in groups for x in g]
        info["prerequisite_logic"] = (
            " AND ".join("(%s)" % " OR ".join(g) for g in groups) if groups else "")
        info["mutually_exclusive_keys"] = (stat or {}).get("mutex") or []
        if stat:
            info["days"] = stat.get("days")
            info["resolution_1936"] = bool(stat.get("research_slot"))
        info["source"] = "script" if stat else "memory"
        info["ptr"] = hex(f) if f else None
        return info

    def focus_progress(self, tag=None):
        p = self.p
        prog = p.u64(self._c(tag).ptr + CO_FOCUS_PROGRESS)
        cur = self.current_focus(tag)
        days_done = round(p.i64(prog + 0x38) / float(O.FIXED), 3) if prog else 0.0
        cost_days = cur.get("cost_days") if isinstance(cur, dict) else None
        if cur and not cost_days:
            cost_days = self._focus_cost_days(cur.get("key"))
        days_left = (
            round(max(0.0, cost_days - days_done), 2)
            if (cur and cost_days and cost_days > 0)
            else None
        )
        pct = (
            round(min(100.0, days_done / cost_days * 100.0), 1)
            if (cur and cost_days and cost_days > 0)
            else None
        )
        # 2026-11-22 (复测 5 §3.6): 补齐 progress / days_done / cost_days / days_left / percent，
        # 修复 turnloop.snapshot() 读 `progress` 恒得 0.0 且无处读取国策进度的问题；保留 raw_0x38 兼容。
        return {
            "current": cur,
            "progress": days_done,
            "days_done": round(days_done, 2),
            "cost_days": cost_days,
            "days_left": days_left,
            "percent": pct,
            "banked_days": round(days_done, 2) if not cur else 0.0,
            "raw_0x38": days_done if prog else None,
            "completed": _count(p, prog, FP_COMPLETED) if prog else 0,
        }

    def _can_start_focus_win(self, focus_ptr, tag_id):
        """Engine truth on the PE build.  None = the probe itself failed."""
        import cmdwin as W
        try:
            cmd = W.make_national_focus(self, int(focus_ptr), int(tag_id))
        except Exception:
            return None
        try:
            return bool(self.call(self.p.base + self.VCAN_FOCUS_WIN, (cmd, 0),
                                  absolute=True, force=True) & 0xFF)
        except Exception:
            return None
        finally:
            try:
                W.free(self, cmd)
            except Exception:
                pass

    def _can_start_focus_cmd(self, focus_ptr, tag_id):
        """调用 Windows PE CSetNationalFocusCommand::CanExecute 判定国策当前是否可启动。"""
        return self._can_start_focus_win(focus_ptr, tag_id)

    def selectable_focuses(self, tag=None, allow_civil_war=False):
        """Selectable focuses, ranked by the country's own historical AI plan.

        `selectable` is still the set that passes the live prerequisite/mutex check;
        the ranking comes from common/ai_strategy_plans (ai_national_focuses) plus
        static reward flags from common/national_focus.  No game calls.
        """
        import focusplan
        p = self.p
        c = self._c(tag)
        prog = p.u64(c.ptr + CO_FOCUS_PROGRESS)
        if not prog:
            return {"recommended": None, "recommended_reason": "", "count": 0,
                    "selectable": [], "focuses": [], "blocked": []}
        completed = set()
        for f in _arr(p, prog, FP_COMPLETED):
            k = item_name(p, f + NF_KEY)
            if k:
                completed.add(k)
        current = p.u64(prog + FP_CURRENT)
        # The full ranking costs ~15 s on the PE build (two item_name reads per
        # focus over 438 focuses), and hoi4_overview asks for it twice (alerts ->
        # candidates).  Memoise on the three things that can change the answer.
        # 2026-11-20: the key did not contain the COUNTRY, so asking about one
        # country and then another on the same day returned the first country's
        # focus list.  Two different trees, one cache slot.
        ckey = (p.pid, c.tag, self.date_raw(), current, len(completed))
        cached = getattr(self, "_sel_cache", None)
        if cached and cached[0] == ckey and not allow_civil_war:
            return cached[1]

        blacklist = set()
        try:
            import playbooks
            blacklist = playbooks.blacklist(c.tag)
        except Exception:
            blacklist = set()
        if c.tag == "GER" and not allow_civil_war:
            blacklist |= {"GER_oppose_hitler", "GER_oppose_hitler_ww"}

        data = focusplan.load()
        meta = data.get("focuses", {})
        plan = data.get("plans", {}).get(c.tag, [])
        plan_pos = {k: i for i, k in enumerate(plan)}
        # The historical AI plan puts GER_anschluss at #25 and GER_demand_sudetenland
        # at #29: following it literally spends 3.6 years on propaganda/industry and
        # never annexes anything (28 focuses, 0 snowball, measured 2026-11-05).
        # These two tables jump the annexation chain to the top as soon as the
        # country can actually pass its manpower/peace conditions.
        my_divs = len(_arr(p, c.ptr, CO_ARMIES))
        try:
            yr = int(self.date().get("year") or 0)
        except Exception:
            yr = 0
        try:
            import combat as _cb
            at_war = bool(_cb.at_war(self, c.tag))
        except Exception:
            at_war = False
        rows, blocked = [], []
        try:
            import playbooks as _pb
            pb_scores = _pb.focus_scores(c.tag, yr, my_divs, at_war)
        except Exception:
            pb_scores = {}
        for f in self.focus_tree(tag):
            k = item_name(p, f + NF_KEY)
            if not k or k in completed or f == current:
                continue
            if k in blacklist:
                blocked.append(k)
                continue
            ok = True
            if sys.platform == "win32":
                # 2026-11-19 (live): PE cannot read the prerequisite/mutex blocks
                # from CNationalFocus, and this branch used to just SKIP the
                # filter -- so `selectable` was the whole tree (427 of 438 on a
                # 1936 save).  Use the parsed script data (focusplan schema 2),
                # the same source hoi4_focus_list(only_startable) already trusts.
                m0 = meta.get(k) or {}
                groups = [g for g in (m0.get("prereq_groups") or []) if g]
                if any(not any(g in completed for g in grp) for grp in groups):
                    ok = False
                if any(x in completed for x in (m0.get("mutex") or [])):
                    ok = False
            else:
                for group in self._focus_prereq_groups(f):
                    if group and not any(g in completed for g in group):
                        ok = False
                        break
                if any(m in completed for m in self._focus_mutex_keys(f)):
                    ok = False
            if not ok:
                continue
            m = meta.get(k, {})
            score, tier, reason = 0, "D_other", ""
            if k in plan_pos:
                score = 1000 - min(plan_pos[k], 100)
                tier = "S_historical"
                reason = "历史主线优先顺位 #%d" % (plan_pos[k] + 1)
            elif m.get("research_slot"):
                score, tier, reason = 300, "A_research_slot", "奖励额外科研槽"
            elif m.get("annex") or m.get("wargoal"):
                score, tier, reason = 300, "A_annex", "政治吞并/宣战目标"
            elif m.get("industry"):
                score, tier, reason = 150, "B_industry", "工业主线（民工/军工）"
            elif m.get("air_naval"):
                score, tier, reason = 0, "C_air_naval", "空军/海军支线，默认排后"
            if c.tag == "GER":
                sb = self.GER_SNOWBALL_CHAIN.get(k)
                if sb:
                    sb_score, min_d, min_y, need_peace = sb
                    # 2026-11-27 (D-20 附带纠偏): Gotterdammerung (1.19.5) 中 GER_anschluss 的真实
                    # 脚本条件是 `has_war = no` + 对奥陆军实力比（fighting_army_strength_ratio vs AUS），
                    # 而非旧版固定 54 师；和平期且年份达标时不再因 divs < 54 静态隐藏，让引擎探针真测！
                    if need_peace and at_war:
                        blocked.append(
                            "%s (blocked: requires has_war=no, currently at_war=True; divs=%d, year=%d)"
                            % (k, my_divs, yr)
                        )
                        continue
                    if yr < min_y:
                        blocked.append(
                            "%s (wait: year %d/%d, divs=%d, at_war=%s)"
                            % (k, yr, min_y, my_divs, at_war)
                        )
                        continue
                    if k != "GER_anschluss" and my_divs < min_d:
                        blocked.append(
                            "%s (wait: divs %d/%d, year %d/%d, at_war=%s)"
                            % (k, my_divs, min_d, yr, min_y, at_war)
                        )
                        continue
                    score, tier = sb_score, "S0_snowball"
                    if k == "GER_anschluss" and my_divs < min_d:
                        reason = (
                            "德国史实吞并雪球核心主线（divs=%d；1.19.5 Gotterdammerung 使用 "
                            "has_war=no + 对奥陆军实力比而非固定 54 师，须过引擎 available 条件）"
                            % my_divs
                        )
                    else:
                        reason = (
                            "德国史实吞并雪球核心主线（启发式顺位，divs=%d；实际能否启动须过引擎 available 条件）"
                            % my_divs
                        )
                elif k in self.GER_OPENING_TOP:
                    score, tier, reason = (self.GER_OPENING_TOP[k], "S0_opening",
                                           "德国开局核心工业/政治主线")
                elif k in self.GER_LOW_PRIORITY:
                    score, tier, reason = (self.GER_LOW_PRIORITY[k], "Z_filler",
                                           "可重复填充国策，让位给吞并链")
            if k in pb_scores and pb_scores[k] > score:
                score, tier, reason = (pb_scores[k], "S0_playbook", "老手攻略时间轴")
            rows.append({
                "id": k, "key": k, "name": item_name(p, f + NF_NAME),
                "ptr": hex(f), "tier": tier, "score": score, "days": m.get("days", 70),
                "reason": reason, "_plan": plan_pos.get(k, 999),
            })
        # 2026-11-11: the prerequisite/mutex scan above cannot evaluate the
        # `available` trigger block (GER_anschluss needs 250k manpower and
        # rifles; GER_currency_reforms needs a law), which is why the tool used
        # to advertise focuses the engine then refused — the previous run spent
        # dozens of turns clicking them. Ask the engine instead. Only while no
        # focus is running: with one in progress the engine refuses everything
        # else, so the answer would be a useless wall of "no".
        engine_checked = False
        engine_unknown = 0
        if rows and not current:
            keep = []
            # 2026-11-21 (续测 P1-7): this loop fires one g.call (freeze + hijack)
            # per candidate.  It measured >50 s and blew the watchdog, because
            # each probe also sampled the date twice at 13.7 s a go.  The date is
            # cheap now, but the probes are still injections: bound them by wall
            # clock so hoi4_selectable_focuses can never exceed the budget.  Once
            # the deadline passes the remaining candidates are returned as
            # unverified instead of being silently dropped.
            import time as _tf
            _deadline = _tf.time() + 8.0
            for r in rows:
                if _tf.time() > _deadline:
                    engine_unknown += 1
                    keep.append(r)
                    continue
                v = self._can_start_focus_cmd(int(r["ptr"], 16), c.tag_id)
                if v is None:
                    engine_unknown += 1
                    keep.append(r)
                elif v:
                    keep.append(r)
                else:
                    blocked.append("%s (engine: available-trigger / manpower / "
                                   "political-power gate not met)" % r["id"])
            rows = keep
            engine_checked = (engine_unknown == 0)
        rows.sort(key=lambda r: (-r["score"], r["_plan"], r["id"]))
        for r in rows:
            r.pop("_plan", None)
        recommended = rows[0]["id"] if rows else None
        res = {
            "recommended": recommended,
            "recommended_reason": rows[0]["reason"] if rows else "",
            "recommended_verified": bool(engine_checked),
            "count": len(rows),
            "engine_checked": engine_checked,
            "focus_in_progress": item_name(p, current + NF_KEY) if current else None,
            "selectable": rows[:60],
            # 2026-11-22 (复测 5 §3.5): 旧代码把完整对象列表在 `selectable` 和 `focuses`
            # 重复返回了两遍（体积翻倍）；现保留 `selectable` 为完整对象列表，`focuses`
            # 改为仅 key 列表兼顾旧调用方且消除冗余。
            "focuses": [r["id"] for r in rows[:60]],
            "blocked": blocked[:40],
        }
        if not engine_checked:
            res["engine_checked_note"] = (
                "a focus is currently in progress, so engine CanExecute probes were skipped; "
                "`selectable` and `recommended` reflect static prerequisites/heuristics only and "
                "must still pass each focus's script `available` trigger once the focus slot is free"
                if current else
                "engine CanExecute verification was incomplete (%d unverified); check script `available` trigger before assuming startable"
                % engine_unknown
            )
        if sys.platform == "win32":
            # 2026-11-20 (P0 #4): the engine check runs here now.  Prerequisites
            # and mutexes stay on the parsed script data (CNationalFocus does not
            # expose them as a pointer array), but everything that survives that
            # filter is asked of CSetNationalFocusCommand::CanExecute, so the
            # `available` trigger (manpower / rifles / laws / war state / PP) is
            # the engine's answer and not a guess.
            res["verified"] = bool(engine_checked)
            if engine_checked:
                res["note"] = ("PE build: candidates passed the script "
                               "prerequisite/mutex filter AND the engine's own "
                               "CSetNationalFocusCommand::CanExecute (its "
                               "`available` evaluator), through a dry-run command "
                               "that is never posted.")
            elif current:
                res["note"] = ("PE build: a focus is already in progress, so the "
                               "engine refuses every other focus and the CanExecute "
                               "probe is skipped. This is the ranked static "
                               "candidate list; re-run once the slot is free to get "
                               "an engine-verified set.")
            else:
                res["note"] = ("PE build: %d candidate(s) could not be probed "
                               "(the CanExecute call failed) and are listed "
                               "unverified; the rest are engine-checked."
                               % engine_unknown)
            res["count"] = len(rows)
            res["truncated"] = max(0, len(rows) - 60)
        if not allow_civil_war:
            self._sel_cache = (ckey, res)
        return res

    def completed_focuses(self, tag=None):
        p = self.p
        prog = p.u64(self._c(tag).ptr + CO_FOCUS_PROGRESS)
        if not prog:
            return []
        return [item_name(p, f + NF_KEY) for f in _arr(p, prog, FP_COMPLETED)]

    def continuous_focuses(self, tag=None):
        """The 1.19.4.1 continuous-focus catalogue (no completion needed).

        CO_CONT_FOCUS_PAL (+0x1340) is a CContinuousFocusPalette -- a GUI object
        with a std::string at +0x08 and one selection slot at +0x00 -- and holds
        no focus list at all, which is why the old find_arrays() walk returned []
        for every country. The real catalogue is the global
        CContinuousNationalFocus database (14 definitions, key at +0x18, display
        name at +0x38). Verified live 2026-11-11.
        """
        p = self.p
        out = []
        for d in self.oi.instances("CContinuousNationalFocus"):
            k = item_name(p, d + 0x18)
            if not k:
                continue
            out.append({"key": k, "id": item_name(p, d + 0x58) or k,
                        "name": item_name(p, d + 0x38),
                        "ptr": hex(d), "generic": k.startswith("continuous_")})
        out.sort(key=lambda r: (not r["generic"], r["key"]))
        return out

    def set_continuous_focus(self, key, tag=None):
        """Select a continuous focus, and tell the truth about the result.

        2026-11-11: the definition pointer is what the old code passed, and the
        engine refuses it (CanExecute 0) -- the operand has to be the country's
        own CContinuousFocusItem, which this build only materialises while the
        political screen is built, so it cannot be fabricated from Python. The
        tool therefore reports `queued` from CCommand::Run and never claims
        `ok` without it, instead of the old unconditional {"ok": True}.
        """
        c = self._c(tag)
        rows = self.continuous_focuses(c.tag)
        row = next((r for r in rows if key in (r["key"], r["id"])), None)
        if row is None:
            raise KeyError("no such continuous focus: %s (available: %s)"
                           % (key, ", ".join(r["key"] for r in rows)))
        if not row["generic"]:
            return {"ok": False, "focus": key, "queued": False,
                    "error": "%s is a country-specific continuous focus and is "
                             "not offered to %s" % (key, c.tag)}
        cmd = self.cmd.make("SetContinuousFocus", tag=c.tag_id,
                            focus_ptr=int(row["ptr"], 16))
        rv = self.cmd.post(cmd)
        self._settle(0.4)
        ok = bool(rv & 0xFF) if isinstance(rv, int) else False
        return {"ok": ok, "focus": key, "queued": ok, "verified": False,
                "error": "" if ok else
                ("engine refused CSetContinuousFocusCommand::CanExecute: this "
                 "build wants the per-country CContinuousFocusItem, not the "
                 "definition pointer; select it in the UI instead")}

    def drop_continuous_focus(self, tag=None):
        c = self._c(tag)
        cmd = self.cmd.make("DropContinuousFocus", tag=c.tag_id)
        rv = self.cmd.post(cmd)
        self._settle(0.4)
        return {"ok": bool(rv & 0xFF) if isinstance(rv, int) else False,
                "error": getattr(self.cmd, "last_error", "")}
