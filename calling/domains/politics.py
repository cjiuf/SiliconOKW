"""内政决议、政治理念/法案、推荐理念及顾问聘用领域 Mixin。"""
from __future__ import annotations

import re
import sys

from ._shared import (
    CO_DECISIONS,
    CO_POLITICS,
    DECISION_NAME,
    DS_ACTIVE,
    DS_ALL,
    DS_RUNNING,
    DS_VISIBLE,
    IDEA_NAME,
    PL_ACTIVE_IDEAS,
    PL_ALL_IDEAS,
    PL_PARTIES,
    _arr,
    _count,
    item_name,
    read_scoped,
)

class PoliticsMixin:
    """内政决议、政治理念/法案、推荐理念及顾问聘用领域 Mixin。"""

    # (need_ws, need_war, strict_gt, non_autocratic_needs_war)
    # 2026-11-27 (D-24 根治): 对齐 HOI4 1.19.5 common/ideas/_economic.txt 与 _manpower.txt
    # 真实脚本：war_economy 等使用严格大于 `has_war_support > 0.5`（0.500 不通过，必须 > 0.50），
    # 且非 fascism/communism 国家还需 has_war = yes。解析逻辑兼容旧 2 元组子类 mock。
    _LAW_GATE = {
        "free_trade": (0.0, False, False, False),
        "export_focus": (0.0, False, False, False),
        "limited_exports": (0.0, False, False, False),
        "civilian_economy": (0.0, False, False, False),
        "low_economic_mobilisation": (0.15, False, True, False),
        "partial_economic_mobilisation": (0.25, False, True, False),
        "war_economy": (0.50, False, True, True),
        "extensive_conscription": (0.20, False, False, False),
        "autarkic_economy": (0.50, False, True, False),
        "service_by_requirement": (0.60, True, False, False),
        "closed_economy": (0.0, True, False, False),
        "tot_economic_mobilisation": (0.80, True, True, False),
        "totaler_krieg_economy": (0.80, False, True, False),
        "all_adults_serve": (0.70, True, True, False),
        "all_men_serve": (0.80, True, True, False),
        "scraping_the_barrel": (0.85, True, True, False),
        "total_mobilization": (0.80, True, True, False),
    }

    @staticmethod
    def _unpack_law_gate(gate):
        """兼容 2/3/4 元组 _LAW_GATE，返回 (need_ws, need_war, strict_gt, non_auto_war)。"""
        n = len(gate)
        need_ws = float(gate[0])
        need_war = bool(gate[1])
        strict_gt = bool(gate[2]) if n >= 3 else False
        non_auto_war = bool(gate[3]) if n >= 4 else False
        return need_ws, need_war, strict_gt, non_auto_war

    @staticmethod
    def _law_available_snippet(key):
        """纯只读从 common/ideas/*.txt 提取指定法案的 available = { ... } 摘要（D-24）。"""
        import glob as _glob
        import os as _os
        try:
            import config as _cfg
            gdir = _cfg.game_dir(required=False)
        except Exception:
            gdir = None
        if not gdir or not key:
            return None
        pat = re.compile(r"\b" + re.escape(str(key)) + r"\s*=\s*\{")
        for path in sorted(_glob.glob(_os.path.join(gdir, "common", "ideas", "*.txt"))):
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as fh:
                    txt = fh.read()
            except OSError:
                continue
            m = pat.search(txt)
            if not m:
                continue
            i, d = m.end(), 1
            n = len(txt)
            while d and i < n:
                ch = txt[i]
                if ch == "{":
                    d += 1
                elif ch == "}":
                    d -= 1
                i += 1
            body = txt[m.end():i - 1]
            ma = re.search(r"\bavailable\s*=\s*\{", body)
            if not ma:
                return None
            j, da = ma.end(), 1
            nb = len(body)
            while da and j < nb:
                ch = body[j]
                if ch == "{":
                    da += 1
                elif ch == "}":
                    da -= 1
                j += 1
            raw = re.sub(r"#[^\n]*", "", body[ma.end():j - 1])
            clean = " ".join(raw.split())
            return clean[:240] if clean else None
        return None

    # Law hierarchy. `recommended_ideas` used to advertise ANY law of the same
    # category that was not active -- including civilian_economy while the country
    # was already on partial_economic_mobilisation. An agent that followed the
    # recommendation downgraded the country's economy.
    _LAW_RANK = {
        "undisturbed_isolation": 0, "isolation": 1,
        "civilian_economy": 2, "low_economic_mobilisation": 3,
        "partial_economic_mobilisation": 4, "war_economy": 5,
        "autarkic_economy": 5, "tot_economic_mobilisation": 6,
        "totaler_krieg_economy": 7,
        "disarmed_nation": 0, "volunteer_only": 1, "limited_conscription": 2,
        "extensive_conscription": 3, "service_by_requirement": 4,
        "all_adults_serve": 5, "all_men_serve": 5, "scraping_the_barrel": 6,
    }

    def _decision_name(self, d):
        return item_name(self.p, d + DECISION_NAME)

    @read_scoped(
        "decisions",
        key_fn=lambda self, tag=None, which="visible", rich=True: (self._rs_tag_key(tag), str(which), bool(rich)),
    )
    def decisions(self, tag=None, which="visible", rich=True):
        """Decisions, optionally with everything a player needs to judge one.

        2026-11-19: this used to return `{key, ptr}` and nothing else, so the
        tool was unusable — "GER_plan_z" tells you nothing about what it costs,
        whether it is a debug decision, or when it can be taken.  The script key
        is still the identity, but the row now carries the localised title, the
        category, the political-power cost, the run time and a readable slice of
        the `available` trigger (parsed from common/decisions, see
        decisions_meta.py).  `ptr` stays for hoi4_select_decision.

        2026-11-22 (P1-4): `which` now means what it says.
          * "visible"  — the country's visible tab        (CDecisionStatus+0x88)
          * "takeable" — everything the engine lists as candidate (+0xD0)
          * "active"   — decisions CURRENTLY IN PROGRESS   (+0x148)
          * "all"      — every definition                  (+0x10)
        Before this, "active" pointed at +0xD0, which is the candidate list: it
        returned 100+ rows at a point where nothing was running, and the UI's own
        "active" tab was empty.  +0x148 holds {vtable, CDecision*, ...} records;
        measured live, HOL/BEL/TUR/POR all carry `seize_some_trains_woo`
        (days_remove 180) there and GER carries nothing.
        """
        c = self._c(tag)
        me = c.tag
        ds = self.p.u64(c.ptr + CO_DECISIONS)
        if which == "active":
            rows = []
            known_tags = self._known_country_tags()
            for item in self._running_decisions(ds):
                nm = item["key"]
                row = {"key": nm, "ptr": item["ptr"], "running": True}
                if rich:
                    row.update(self.decision_meta(nm))
                    vt = self._decision_tag_scope(row.get("visible"),
                                                  row.get("available"),
                                                  key=nm, known_tags=known_tags)
                    row["visible_tag"] = vt
                    row["mine"] = (vt is None or vt == me)
                rows.append(row)
            return rows
        off = {"all": DS_ALL, "takeable": DS_ACTIVE,
               "visible": DS_VISIBLE}[which]
        known_tags = self._known_country_tags() if rich else None
        out = []
        for d in _arr(self.p, ds, off):
            nm = self._decision_name(d)
            if not nm:
                continue
            row = {"key": nm, "ptr": d}
            if rich:
                row.update(self.decision_meta(nm))
                vt = self._decision_tag_scope(row.get("visible"), row.get("available"),
                                              key=nm, known_tags=known_tags)
                row["visible_tag"] = vt
                # CDecisionStatus+0x88 is NOT strictly "this country's tab": the
                # engine also lists decisions that belong to another tag (their
                # `visible` block starts with `tag = ENG`, or key starts with `JAP_`/`SIA_`).
                row["mine"] = (vt is None or vt == me)
            out.append(row)
        if rich and which == "visible":
            out.sort(key=lambda r: (not r.get("mine"), r["key"]))
        return out

    def _running_decisions(self, ds):
        """Decisions in progress for one country (CDecisionStatus+0x148).

        The records are {vtable, CDecision*, ...} with a stride wider than 8, and
        the exact stride has not been disassembled; instead of guessing it, the
        block is scanned 8 bytes at a time for pointers that resolve to a decision
        name.  That is stride-independent and skips the vtable/padding words.
        """
        p = self.p
        out, seen = [], set()
        try:
            data = p.u64(ds + DS_RUNNING)
            size = p.i32(ds + DS_RUNNING + 12)
        except Exception:
            return out
        if not data or not (0 < size <= 512):
            return out
        for off in range(0, size * 0x20, 8):
            try:
                v = p.u64(data + off)
            except Exception:
                break
            if not v or v in seen:
                continue
            nm = self._decision_name(v)
            if nm:
                seen.add(v)
                out.append({"key": nm, "ptr": v})
        return out

    def _known_country_tags(self):
        """当前游戏存在的全部 3 字母国家 TAG 集合（用于识别 JAP_/SIA_ 等外邦决议前缀，避开 MTG/DLC）。"""
        cached = getattr(self, "_known_tags_set", None)
        if cached:
            return cached
        try:
            tags = {str(t).upper() for t in (self.tags() or ()) if t and len(str(t)) == 3}
        except Exception:
            tags = set()
        if tags:
            self._known_tags_set = frozenset(tags)
            return self._known_tags_set
        return frozenset()

    @staticmethod
    def _decision_tag_scope(*triggers, key=None, known_tags=None):
        """The country a decision is scoped to, looking at triggers and TAG_ key prefix."""
        for vis in triggers:
            if not vis:
                continue
            m = re.search(r"\b(?:original_tag|tag)\s*=\s*([A-Z]{3})\b", vis)
            if m:
                return m.group(1)
        # 2026-11-23 (C-6): 如 JAP_sue_for_peace_nuke_mission / SIA_standing_our_ground_mission
        # 在 visible/available 里没写 tag=XXX，但 key 前缀本身是明确的他国 3 字母 TAG；
        # 仅当该前缀属于真实国家 TAG 集合（自动排除 MTG/AAT/BBA/NSB/DLC 等）时归属该国。
        if key:
            km = re.match(r"^([A-Z]{3})_", str(key))
            if km:
                pref = km.group(1)
                if known_tags and pref in known_tags:
                    return pref
        return None

    @staticmethod
    def _decision_visible_tag(vis):
        """The country a decision's `visible` trigger is scoped to, if any."""
        if not vis:
            return None
        m = re.search(r"\b(?:original_tag|tag)\s*=\s*([A-Z]{3})\b", vis)
        return m.group(1) if m else None

    def decision_meta(self, key):
        """Static metadata for one decision key (title / cost / trigger slice)."""
        try:
            import decisions_meta
            m = decisions_meta.get(key) or {}
        except Exception:
            m = {}
        try:
            import loc
            title = loc.title(key)
        except Exception:
            title = None
        return {
            "title": title,
            "category": m.get("category"),
            "cost_pp": m.get("cost_pp"),
            "days_remove": m.get("days_remove"),
            "days_mission_timeout": m.get("days_mission_timeout"),
            "fire_only_once": m.get("fire_only_once"),
            "is_good": m.get("is_good"),
            "has_available": m.get("has_available"),
            "available": m.get("available"),
            "visible": m.get("visible"),
            "meta_source": "common/decisions" if m else "not in script files",
        }

    def decision_definitions(self):
        """key -> CDecision* from the game's own CDecisionDatabase (4187 entries)."""
        if getattr(self, "_dec_defs", None) is not None:
            return self._dec_defs
        p = self.p
        db = self.oi.singleton("CDecisionDatabase")
        out = {}
        for it in _arr(p, db, 0x28):
            nm = item_name(p, it + DECISION_NAME)
            if nm:
                out[nm] = it
        self._dec_defs = out
        return out

    def select_decision(self, key, tag=None):
        """Take a decision and PROVE it ran.

        2026-11-22 (P1-2): this used to return `ok` = the command's own return
        code, so a decision the engine refused (GER_plan_z, whose `visible` needs a
        focus that was never completed) came back as `ok:true, error:""` with
        nothing done.  "Visible" is not "available".  A decision only counts as
        taken when something observable changes: political power moves, or the
        decision shows up in the country's in-progress list (+0x148).  Otherwise
        the call fails and says why.
        """
        c = self._c(tag)
        target = None
        source = None
        for which in ("visible", "takeable", "all"):
            for d in self.decisions(c.tag, which, rich=False):
                if d["key"] == key:
                    target = d["ptr"]; source = which; break
            if target:
                break
        if not target:
            # Generic decisions (improved_worker_conditions, infrastructure_building_slot,
            # ...) are in the database but not in the country's "visible" array until a
            # category becomes visible. The command takes the definition pointer.
            target = self.decision_definitions().get(key)
            source = "database"
        if not target:
            raise KeyError("decision not found: %s" % key)
        pp0 = c.political_power
        try:
            running0 = {r["key"] for r in self.decisions(c.tag, "active", rich=False)}
        except Exception:
            running0 = set()
        with self:
            cmd = self.cmd.make("SelectDecision", tag=c.tag_id, decision_ptr=target)
            # CCommand::Run: CanExecute -> Post(queue, cmd, 1), the click path.
            rv = self._run_cmd(cmd)
        ran = bool(rv & 0xFF)
        for _ in range(8):
            self._settle(0.35)
            pp1 = c.political_power
            if pp1 != pp0:
                return {"ok": True, "decision": key, "source": source, "ran": ran,
                        "verified_by": "political_power",
                        "political_power_before": pp0, "political_power_after": pp1}
            try:
                running1 = {r["key"] for r in self.decisions(c.tag, "active", rich=False)}
            except Exception:
                running1 = running0
            if key in running1:
                return {"ok": True, "decision": key, "source": source, "ran": ran,
                        "verified_by": "active_list",
                        "political_power_before": pp0, "political_power_after": pp1}
        meta = self.decision_meta(key) or {}
        why = []
        if meta.get("available"):
            why.append("its `available` trigger is: %s" % meta["available"][:220])
        return {"ok": False, "decision": key, "source": source, "ran": ran,
                "verified_by": None,
                "error": ("the engine did not run it: political power, the in-progress "
                          "list and the decision itself are all unchanged after ~3 s. "
                          "A decision can be VISIBLE and still not takeable -- check "
                          "hoi4_decision_detail(key).available. " + " ".join(why)).strip(),
                "political_power_before": pp0,
                "political_power_after": c.political_power}

    def decision_detail(self, key, tag=None):
        p = self.p
        for which in ("visible", "active", "all"):
            for d in self.decisions(tag, which, rich=False):
                if d["key"] == key:
                    out = {"key": key, "list": which, "ptr": hex(d["ptr"])}
                    out.update(self.decision_meta(key))
                    out["fields"] = self.browse_object(d["ptr"], 0x80)["fields"][:16]
                    return out
        raise KeyError("no such decision: %s" % key)

    def _idea_entries(self, tag=None, which="all"):
        """Country idea instances: key, pointer, and the law-category object at +0xAA0.

        PL_ALL_IDEAS and PL_ACTIVE_IDEAS point at per-country CIdea instances. They are
        the correct operands for CReplaceIdeaCommand; CIdeaDatabase entries are only
        definitions and are not accepted as a law replacement operand.
        """
        p = self.p
        c = self._c(tag)
        pol = p.u64(c.ptr + CO_POLITICS)
        off = PL_ALL_IDEAS if which == "all" else PL_ACTIVE_IDEAS
        out = []
        for i in _arr(p, pol, off):
            nm = item_name(p, i + IDEA_NAME)
            if nm:
                out.append({"key": nm, "ptr": i, "category": p.u64(i + 0xAA0)})
        return out

    def politics(self, tag=None):
        p = self.p
        c = self._c(tag)
        pol = p.u64(c.ptr + CO_POLITICS)
        active = self._idea_entries(tag, "active")
        ruling_ideo = None
        try:
            import legality as _leg
            ruling_ideo = _leg.ruling_ideology(self, c.tag)
        except Exception:
            ruling_ideo = None
        return {
            "political_power": c.political_power,
            "ruling_party_popularity": c.ruling_party_popularity,
            "ruling_ideology": ruling_ideo,
            "active_ideas": [a["key"] for a in active],
            "active_idea_ptrs": active,
            "available_idea_count": _count(p, pol, PL_ALL_IDEAS),
            "party_count": _count(p, pol, PL_PARTIES),
        }

    def available_ideas(self, tag=None, filter_text=""):
        out = []
        for e in self._idea_entries(tag, "all"):
            if not filter_text or filter_text.lower() in e["key"].lower():
                out.append(e)
        return out

    def idea_catalog(self, tag=None, slot=None, filter_text="", include_advisors=True, limit=120):
        """统一返回当前国家的法案 / 工业设计商 / 顾问完整目录（纯只读 0 注入）。

        每条记录统一携带 `(key, kind, slot, cost, active, available, mutually_exclusive_with)`，
        让 Standing Orders 编译器与 `hoi4_inspect(idea_catalog)` 可以在声明期做存在性、
        槽位冲突与 PP 费用校验。
        """
        import advisors as _adv

        c = self._c(tag)
        my_tag = c.tag
        try:
            pol = self.politics(my_tag) or {}
        except Exception:
            pol = {}
        active_set = set(pol.get("active_ideas") or ())
        live_all = {}
        try:
            for e in self._idea_entries(my_tag, "all") or ():
                if e.get("key"):
                    live_all[e["key"]] = e
        except Exception:
            live_all = {}

        static_cat = _adv.load_ideas_catalog()
        slot_flt = str(slot or "").strip().lower()
        txt_flt = str(filter_text or "").strip().lower()

        rows = []
        seen = set()

        # 1) 合并本国 live ideas 与静态法案/工业槽位元数据
        candidate_keys = list(live_all.keys())
        for k in active_set:
            if k not in live_all:
                candidate_keys.append(k)
        # 无 live ideas（如离线测试）时仍纳入核心法案表
        if not candidate_keys:
            for k, meta in static_cat.items():
                if meta.get("is_law"):
                    candidate_keys.append(k)

        # 2026-11-26 (A3 性能优化): 循环外单次读取 summary 与 at_war，消除对 18 个法案
        # 反复调 c.summary() 与 self.wars() 导致的 ~395ms 热态延迟。
        try:
            _sum_cache = c.summary()
        except Exception:
            _sum_cache = None
        _at_war_cache = None
        try:
            import combat as _cb_ic
            _at_war_cache = bool(_cb_ic.at_war(self, my_tag))
        except Exception:
            try:
                _at_war_cache = bool((self.wars(my_tag) or {}).get("at_war"))
            except Exception:
                _at_war_cache = False

        for k in candidate_keys:
            if k in seen:
                continue
            meta = static_cat.get(k) or {}
            sl = meta.get("slot") or "unknown"
            # 跳过纯国家精神（country），除非显式请求或处于 active 状态
            if sl == "country" and slot_flt != "country" and k not in active_set:
                continue
            if slot_flt and sl.lower() != slot_flt:
                continue
            if txt_flt and txt_flt not in k.lower() and txt_flt not in sl.lower():
                continue
            seen.add(k)
            is_law = bool(meta.get("is_law") or k in self._LAW_GATE)
            cost = int(meta.get("cost")) if meta.get("cost") is not None else (150 if is_law else 150)
            gate_reason = (
                self._idea_block_reason(
                    k, c, my_tag, summary=_sum_cache, at_war=_at_war_cache,
                    ideology=pol.get("ruling_ideology"),
                )
                if (is_law and k not in active_set) else None
            )
            rows.append({
                "key": k,
                "kind": "law" if is_law else "idea",
                "slot": sl,
                "cost": cost,
                "active": k in active_set,
                "available": (k in active_set) or (gate_reason is None),
                "block_reason": gate_reason,
                "mutually_exclusive_with": list(meta.get("mutually_exclusive_with") or ()),
            })

        # 2) 合并本国顾问角色表（common/characters）
        if include_advisors:
            try:
                adv_rows = _adv.load_advisors(my_tag) or []
            except Exception:
                adv_rows = []
            # 2026-11-26 (A3): 将 legality._state 提到 75 个顾问的循环外单次求值（省 ~30ms）
            _leg = None
            _leg_st = None
            if adv_rows:
                try:
                    import legality as _leg_mod
                    _leg = _leg_mod
                    _leg_st = _leg._state(self, my_tag)
                except Exception:
                    _leg = None
                    _leg_st = None
            for a in adv_rows:
                ck = a.get("character") or ""
                tok = a.get("idea_token") or ck
                sl = a.get("slot") or "political_advisor"
                if slot_flt and sl.lower() != slot_flt:
                    continue
                if txt_flt and txt_flt not in ck.lower() and txt_flt not in tok.lower() and txt_flt not in sl.lower():
                    continue
                if ck in seen:
                    continue
                seen.add(ck)
                cost = int(a.get("cost")) if a.get("cost") is not None else 150
                is_active = (ck in active_set) or (tok in active_set)
                avail_ok = True
                blk = None
                if _leg is not None and _leg_st is not None:
                    try:
                        for bname in ("visible_block", "available_block"):
                            blk_txt = a.get(bname)
                            if not blk_txt:
                                continue
                            unk = []
                            if not _leg._eval(blk_txt, _leg_st, unk) and not unk:
                                avail_ok = False
                                blk = "%s unmet" % bname
                                break
                    except Exception:
                        pass
                rows.append({
                    "key": ck,
                    "idea_token": tok,
                    "kind": "advisor",
                    "slot": sl,
                    "cost": cost,
                    "active": is_active,
                    "available": is_active or avail_ok,
                    "block_reason": blk,
                    "traits": list(a.get("traits") or ()),
                    "mutually_exclusive_with": [],
                })

        rows.sort(key=lambda r: (0 if r["kind"] == "law" else (1 if r["kind"] == "advisor" else 2),
                                 r["slot"], r["key"]))
        lim = max(1, int(limit or 120))
        items_out = rows[:lim]
        return {
            "tag": my_tag,
            "political_power": pol.get("political_power", getattr(c, "political_power", None)),
            "active_ideas": sorted(active_set),
            "total": len(rows),
            "returned": len(items_out),
            "limit": lim,
            "truncated": len(rows) > len(items_out),
            "prefilter_note": (
                "law/advisor `available` is a pure-read static pre-filter (strict `>` war_support "
                "and war/ideology gates aligned with common/ideas); country-specific DLC triggers "
                "are authoritatively checked by the engine's CanExecute when applying."
            ),
            "items": items_out,
        }

    def _find_idea(self, key, tag=None):
        for e in self._idea_entries(tag, "all"):
            if e["key"] == key:
                return e
        return None

    def _active_idea_in_category(self, category, tag=None):
        if not category:
            return None
        for e in self._idea_entries(tag, "active"):
            if e["category"] == category:
                return e
        return None

    def _replace_cmd(self, old, new, tag=None):
        c = self._c(tag)
        return self.cmd.make("CReplaceIdea", tag=c.tag_id,
                             old_ptr=old["ptr"], new_ptr=new["ptr"])

    def _free_cmd(self, cmd_ptr):
        """Return a command object to the game's heap.

        `cmd.make()` allocates with the game's operator new. A command that is
        only used for a dry-run CanExecute is never posted, so the game never
        frees it. Leaking one per probe used to add ~500 MB of game memory per
        hoi4_overview call, which OOM-killed the whole box (the MCP child hit
        7.8 GB and the attached game died with it).
        """
        if not cmd_ptr:
            return
        try:
            self.free(cmd_ptr)
        except Exception:
            pass

    def _replace_can_execute(self, old, new, tag=None):
        """Windows PE 下交由 replace_idea 内部 cmdwin 门禁核验。"""
        return False

    def replace_idea(self, key, tag=None):
        """Replace the current law in the same category, using the game's own command."""
        c = self._c(tag)
        new = self._find_idea(key, tag)
        if not new:
            raise KeyError("idea not found: %s" % key)
        old = self._active_idea_in_category(new["category"], tag)
        if not old:
            raise KeyError("no active idea in category for: %s" % key)
        before = c.political_power
        # 2026-11-22 (P1-3): work out WHY it would be refused before we post, so a
        # silent failure can be explained.  Live case: war_economy came back
        # run:true / error:"" / active:false while the real reason was PP -30
        # against a 150 PP law -- the caller could only guess.
        why = self._idea_block_reason(key, c, tag)
        with self:
            cmd = self._replace_cmd(old, new, tag)
            rv = self._run_cmd(cmd)
        # The law change lands on a later tick: poll instead of one 0.25s nap.
        active = set()
        for _ in range(10):
            self._settle(0.3)
            active = set(self.politics(c.tag)["active_ideas"])
            if key in active:
                break
        out = {"ok": key in active, "idea": key, "old": old["key"],
               "method": "replace", "run": bool(rv & 0xFF),
               "error": getattr(self, "_last_cmd_error", ""),
               "pp_before": before, "pp_after": c.political_power,
               "active": key in active}
        if key not in active:
            snip = None
            if not why:
                snip = self._law_available_snippet(key)
            out["reason"] = why or (
                "the engine refused the law change (CanExecute said no; script available: {%s})" % snip
                if snip else
                "the engine refused the law change (CanExecute said no; nothing else changed)"
            )
            if snip:
                out["available_trigger"] = snip
        return out

    def _idea_block_reason(self, key, c, tag=None, summary=None, at_war=None, ideology=None):
        """Best-effort explanation of why a law change would be refused."""
        reasons = []
        try:
            s = summary if isinstance(summary, dict) else c.summary()
            pp = int(s.get("political_power") or 0)
            ws = float(s.get("war_support") or 0)
        except Exception:
            return None
        gate = self._LAW_GATE.get(key)
        if gate:
            need_ws, need_war, strict_gt, non_auto_war = self._unpack_law_gate(gate)
            effective_need_war = need_war
            if non_auto_war and not effective_need_war:
                ideo = ideology
                if ideo is None:
                    try:
                        import legality as _leg_ig
                        ideo = _leg_ig.ruling_ideology(self, tag or c.tag)
                    except Exception:
                        ideo = None
                if ideo and str(ideo).lower() not in ("fascism", "communism"):
                    effective_need_war = True
            if effective_need_war:
                if at_war is None:
                    try:
                        import combat as _cb_br
                        at_war = bool(_cb_br.at_war(self, tag or c.tag))
                    except Exception:
                        try:
                            at_war = bool(self.wars(tag or c.tag).get("at_war"))
                        except Exception:
                            at_war = False
                if not at_war:
                    reasons.append(
                        "needs a war (non-fascist/communist requires has_war=yes)"
                        if (non_auto_war and not need_war) else
                        "needs a war (this law is war-only)"
                    )
            if strict_gt and need_ws > 0:
                if ws <= need_ws + 1e-6:
                    reasons.append("needs war_support > %.2f (strictly greater; have %.2f)" % (need_ws, ws))
            elif ws < need_ws:
                reasons.append("needs war_support >= %.2f (have %.2f)" % (need_ws, ws))
            if pp < 150:
                reasons.append("costs 150 political power (have %d)" % pp)
        return "; ".join(reasons) or None

    def can_replace_idea(self, key, tag=None):
        new = self._find_idea(key, tag)
        if not new:
            return {"id": key, "legal": False, "error": "idea not found"}
        old = self._active_idea_in_category(new["category"], tag)
        if not old:
            return {"id": key, "legal": False, "error": "no active idea in category"}
        with self:
            ok = self._replace_can_execute(old, new, tag)
        return {"id": key, "legal": ok, "current": old["key"],
                "method": "replace", "ptr": new["ptr"]}

    def recommended_ideas(self, tag=None, force=False):
        """Law candidates, pre-filtered IN PYTHON -- zero game calls, zero ptrace.

        `hoi4_overview` used to probe every candidate law with a real
        `CReplaceIdeaCommand::CanExecute` injection (~15 command allocations per
        cold call, dozens of stop/start cycles on the main thread).  That was the
        "overview takes several seconds" complaint AND the biggest source of
        `free(): invalid pointer`: every probe allocated a command in the game's
        heap.  The list below is now built from memory reads only:

          * free_trade / export_focus / limited_exports / partial_economic_mobilisation
            are always advertised when they are not the active law;
          * war_economy / extensive_conscription need war_support >= 0.50 or a war;
          * the late-war tiers additionally require a war and higher war support.

        `legal` is therefore a PRE-FILTER verdict, not a verdict from the game.
        The authoritative check is the single CanExecute that CCommand::Run does
        when the law is actually changed; a refusal shows up as ok=false there.
        """
        c = self._c(tag)
        active = {a["key"] for a in self._idea_entries(tag, "active")}
        summary = c.summary()
        ws = float(summary.get("war_support") or 0.0)
        try:
            import combat
            at_war = bool(combat.at_war(self, c.tag))
        except Exception:
            at_war = False
        order = ["free_trade", "export_focus", "limited_exports", "autarkic_economy",
                 "closed_economy", "civilian_economy", "low_economic_mobilisation",
                 "partial_economic_mobilisation", "war_economy",
                 "tot_economic_mobilisation", "totaler_krieg_economy",
                 "extensive_conscription", "service_by_requirement", "all_adults_serve",
                 "scraping_the_barrel", "all_men_serve", "total_mobilization"]
        have = {i["key"]: i for i in self._idea_entries(tag, "all")}
        out, blocked = [], []
        for k in order:
            if k not in have or k in active:
                continue
            new = have[k]
            old = self._active_idea_in_category(new["category"], tag)
            if not old:
                continue
            cur_rank = self._LAW_RANK.get(old["key"])
            new_rank = self._LAW_RANK.get(k)
            if cur_rank is not None and new_rank is not None and new_rank <= cur_rank:
                blocked.append({"id": k, "why": "would downgrade the current law %s"
                                                % old["key"]})
                continue
            gate_raw = self._LAW_GATE.get(k, (0.50, False, True, False))
            need_ws, need_war, strict_gt, non_auto_war = self._unpack_law_gate(gate_raw)
            if non_auto_war and not need_war:
                try:
                    import legality as _leg_ri
                    _ideo_ri = _leg_ri.ruling_ideology(self, c.tag)
                    if _ideo_ri and str(_ideo_ri).lower() not in ("fascism", "communism"):
                        need_war = True
                except Exception:
                    pass
            if need_war and not at_war:
                blocked.append({"id": k, "why": "needs a war"})
                continue
            if strict_gt and need_ws > 0:
                if ws <= need_ws + 1e-6:
                    blocked.append({"id": k, "why": "needs war_support > %.2f (strictly greater; have %.2f)"
                                                    % (need_ws, ws)})
                    continue
            elif ws < need_ws:
                blocked.append({"id": k, "why": "needs war_support >= %.2f (have %.2f)"
                                                   % (need_ws, ws)})
                continue
            out.append({"id": k, "ptr": new["ptr"], "cost_pp": 150,
                        "affordable": c.political_power >= 150,
                        "legal": True, "current": old["key"],
                        "method": "replace", "prefilter": True})
        return {"political_power": c.political_power, "ideas": out,
                "blocked": blocked, "war_support": ws, "at_war": at_war,
                "cached": False, "source": "python_prefilter"}

    def idea_definitions(self):
        """CIdeaDatabase definition pointers (the global idea/law/advisor definitions)."""
        if getattr(self, "_idea_defs", None) is not None:
            return self._idea_defs
        db = self.oi.singleton("CIdeaDatabase")
        out = {}
        if db:
            for x in _arr(self.p, db, 0x68):
                k = item_name(self.p, x + IDEA_NAME)
                if k:
                    out[k] = x
        self._idea_defs = out
        return out

    def add_idea(self, key, tag=None):
        """Apply an idea through the same command a politics-tab click uses.

        Laws and mutually-exclusive advisors are CReplaceIdeaCommand: the country
        already owns one idea in that category, so AddIdea would fail the category
        check. National spirits are CAddIdeaCommand. The branch is chosen from the
        country's own idea list, not guessed from the key.
        """
        c = self._c(tag)
        active = {a["key"] for a in self._idea_entries(tag, "active")}
        if key in active:
            return {"ok": True, "idea": key, "method": "already_active",
                    "run": True, "active": True,
                    "pp_before": c.political_power, "pp_after": c.political_power}
        new = self._find_idea(key, tag)
        if new and new["category"] and self._active_idea_in_category(new["category"], tag):
            return self.replace_idea(key, tag)

        # National spirits / first advisor in an empty category: CAddIdeaCommand.
        ptr = self.idea_definitions().get(key) or (new["ptr"] if new else None)
        if not ptr:
            # V2-04: advisors are not CIdeas at all — route them to CAddAdvisorCommand.
            try:
                hired = self.hire_advisor(key, tag)
            except Exception as e:
                hired = {"ok": False, "attempted": False, "error": repr(e)}
            if hired.get("attempted"):
                return hired
            raise KeyError("idea not found: %s" % key)
        before = c.political_power
        with self:
            cmd = self.cmd.make("AddIdea", tag=c.tag_id,
                                idea_ptr=ptr, flag=1)
            rv = self._run_cmd(cmd)
        self._settle()
        active2 = set(self.politics(c.tag)["active_ideas"])
        out = {"ok": key in active2, "idea": key, "method": "add",
               "run": bool(rv & 0xFF), "error": getattr(self, "_last_cmd_error", ""),
               "pp_before": before,
               "pp_after": c.political_power, "active": key in active2}
        if key not in active2:
            # 2026-11-22 (P1-3): run:true + error:"" + active:false is unreadable
            # on its own.  Say that the engine refused it and why we think so.
            out["reason"] = (self._idea_block_reason(key, c, tag)
                             or "the engine refused the idea (nothing changed; "
                                "national spirits cannot be added this way -- "
                                "advisors go through hoi4_hire_advisor)")
        return out

    def remove_idea(self, key, tag=None):
        p = self.p
        c = self._c(tag)
        pol = p.u64(c.ptr + CO_POLITICS)
        for i in _arr(p, pol, PL_ACTIVE_IDEAS):
            if item_name(p, i + IDEA_NAME) == key:
                with self:
                    self.cmd.send("RemoveIdea", tag=c.tag_id, idea_ptr=i, flag=1)
                self._settle()
                return {"ok": key not in self.politics(c.tag)["active_ideas"], "idea": key}
        raise KeyError("not an active idea: %s" % key)

    def idea_categories(self, tag=None):
        """Idea, law and advisor categories: law slots and advisor roles."""
        p = self.p
        db = self.oi.singleton("CIdeaDatabase")
        out = []
        for c in _arr(p, db, 0x98):
            nm = None
            for o in range(0, 0x200, 8):
                v = item_name(p, c + o)
                if v and v.isprintable() and 2 < len(v) < 48:
                    nm = v
                    break
            out.append(nm)
        return [x for x in out if x]

    def hire_advisor(self, key, tag=None, slot_override=None):
        """Recruit a political advisor / chief of staff / theorist.

        1.19.4.1 CAddAdvisorCommand ctor 0x1CEB5C0, disassembled 2026-11-10:
            mov rax,[rsi+0x8] ; mov [rdi+0x24],rax  -> arg1 = CAdvisor* (handle at +0x08)
            mov r12,[rdx] ; mov rbx,[rdx+8] ...     -> arg2 = std::string* (slot name)
        Then CCommand::Run (CanExecute -> Post) through _run_cmd.
        """
        import advisors as _adv
        c = self._c(tag)
        rows = _adv.load_advisors(c.tag)
        k = (key or "").lower()
        row = None
        for r in rows:
            names = {r["character"].lower(), r["idea_token"].lower(),
                     r["character"].lower().replace(c.tag.lower() + "_", "")}
            if k in names:
                row = r
                break
        if row is None:
            return {"ok": False, "attempted": False,
                    "error": "no advisor named %r for %s" % (key, c.tag)}
        # 2026-11-12: refuse BEFORE injecting when the character's own script
        # trigger is provably unmet. legality._eval already implements the small
        # trigger subset used in common/characters; anything it cannot resolve
        # lands in `unknown` and we fall through to the engine as before.
        try:
            import legality as _leg
            _st = _leg._state(self, c.tag)
            for _bname in ("visible_block", "available_block"):
                _blk = row.get(_bname)
                if not _blk:
                    continue
                _unk = []
                if not _leg._eval(_blk, _st, _unk) and not _unk:
                    return {"ok": False, "attempted": False,
                            "advisor": row["character"], "slot": row.get("slot"),
                            "error": "%s condition unmet: {%s}"
                                     % (_bname, " ".join(_blk.split())[:160])}
        except Exception:
            pass
        ch = 0
        for r in (self.characters(c.tag) or {}).get("characters", []):
            ck = (r.get("key") or "").lower()
            if ck == row["character"].lower() or ck.endswith(
                    row["character"].lower().replace(c.tag.lower() + "_", "")):
                ch = int(r["ptr"], 16)
                break
        if not ch and sys.platform != "win32":
            # 2026-11-16: only the Linux path needs the CCharacter pointer here.
            # The PE path asks `character_handle()` for a packed handle, and
            # `self.characters()` is truncated (67 of ~200 entries: it stops at
            # "hermann_goring"), so every advisor after H was refused with
            # "CCharacter not found" while 2000 PP sat at the cap.
            return {"ok": False, "attempted": True,
                    "error": "CCharacter for %s not found" % row["character"]}
        # CanExecute does __dynamic_cast(resolved_handle, CReferenceObject,
        # CCharacter) — the argument is the PERSON, not the CAdvisor. Confirmed by
        # reading the typeinfo at 0x43493e8 ("10CCharacter") and 0x44d8fa0
        # ("16CReferenceObject") that 0x1CEBC00 passes to __dynamic_cast.
        adv = ch

        slot = slot_override if slot_override is not None else (row.get("slot") or "")

        if sys.platform == "win32":
            # The PE build has neither g.malloc nor the Linux ctor VA, but cmdwin
            # knows CAddAdvisorCommand's real ctor — (this, handleptr, std::string*)
            # at 0x11D2B40 — so the hire goes through the same command queue a
            # politics-tab click uses. Success is read back from the live idea list,
            # not from the post return value: the hire lands on a game tick.
            token = (row.get("idea_token") or "").lower()
            cost = int(row.get("cost") or 0)
            try:
                pp0 = int(self.player().summary().get("political_power") or 0)
            except Exception:
                pp0 = 0
            pp1 = pp0
            if cost and pp0 < cost:
                return {"ok": False, "attempted": False, "advisor": row["character"],
                        "slot": slot, "cost": cost, "political_power": pp0,
                        "error": "not enough political power: have %d, need %d"
                                 % (pp0, cost)}
            r = None
            import cmdwin as _cw
            try:
                _h = self.character_handle(row["character"], c.tag)
                _packed = (_h["id"] << 32) | (_h["type"] & 0xFFFFFFFF)
                # 2026-11-15: PE disassembly (cmdwin.make_add_advisor) proved
                #   vtable 0x2A11A88, size 0x50, packed handle at +0x28,
                #   slot std::string at +0x30. pecmd's "ctor" 0x11D2F00 is the
                #   deleting destructor (vtable slot 0) -- NEVER call it. The
                #   old +0x24 offset was Linux's; CanExecute (0x11ECC20) reads
                #   +0x28, which is why the command posted but never applied.
                #   The built object is byte-identical to the real ctor's output.
                can0 = _cw.can_add_advisor(self, _packed, slot)
                if can0 is False:
                    return {"ok": False, "attempted": True, "advisor": row["character"],
                            "idea_token": token, "slot": slot, "cost": cost,
                            "political_power": pp0, "can_execute": 0,
                            "error": ("the game's own CAddAdvisorCommand::CanExecute "
                                      "rejects this hire (slot %r): the character is "
                                      "already hired, that slot is full, its script "
                                      "trigger is unmet, or the cost cannot be paid. "
                                      "Pick another advisor." % slot)}
                _cmd = _cw.make_add_advisor(self, _packed, slot)
                # A paused game never drains the queue: post with a brief clock
                # run, then restore the pause state.
                r = _cw.post_drained(self, _cmd)
                r["handle"] = hex(_packed)
            except Exception as _e:
                return {"ok": False, "attempted": True, "advisor": row["character"],
                        "slot": slot, "error": "could not build CAddAdvisorCommand: %s" % _e}
            # Read-back: advisors do NOT appear in active_ideas (they live in a
            # separate character-slot list), so the reliable proof is the
            # CanExecute flip: 1 (hireable) -> 0 (already hired) after Execute.
            can1 = _cw.can_add_advisor(self, _packed, slot)
            try:
                pp1 = int(self.player().summary().get("political_power") or 0)
            except Exception:
                pp1 = pp0
            # 2026-11-19 (measured): `cost` is None for every advisor that has no
            # explicit price in the script, so `bool(cost and pp1 < pp0)` reported
            # pp_spent=False while the hire really charged 148 PP (154 -> 6).
            # Derive the charge from the PP ledger instead of from `cost`.
            _delta = int(pp1) - int(pp0)
            pp_spent_amount = -_delta if _delta < 0 else 0
            pp_spent = bool(pp_spent_amount > 0)
            # 2026-11-21 (P1-3): the 2026-11-20 playtest saw `cost: 100` but only
            # 93 PP gone and called the tool inconsistent.  `cost` is the scripted
            # price; `pp_spent_amount` is the OBSERVED balance drop, and the drain
            # runs the clock for a few game days, so the PP earned meanwhile
            # shrinks the drop.  Say that instead of leaving two numbers to argue.
            pp_delta_note = None
            if cost and pp_spent_amount and abs(int(cost) - int(pp_spent_amount)) >= 1:
                pp_delta_note = (
                    "cost is the scripted price (%d) while pp_spent_amount is the "
                    "observed balance drop (%d): the command drain runs the clock "
                    "for a few game days, so political power earned in the meantime "
                    "reduces the visible drop."
                    % (int(cost), int(pp_spent_amount)))
            applied = bool((can0 is True and can1 is False) or pp_spent)
            err = ""
            if not applied:
                err = ("CAddAdvisorCommand was queued (rv=%s) but the hire did not "
                       "land: CanExecute is still %s and PP went %d -> %d. The game "
                       "may have been held by a popup (drained=%s). Check the "
                       "available/visible trigger; a slot holds three advisors."
                       % (r.get("posted"), can1, pp0, pp1, r.get("drained")))
            return {"ok": applied, "attempted": True, "advisor": row["character"],
                    "idea_token": token, "slot": slot,
                    "cost": cost if cost else (pp_spent_amount or None),
                    "political_power_before": pp0, "political_power_after": pp1,
                    "can_execute_before": can0, "can_execute_after": can1,
                    "pp_spent": pp_spent, "pp_spent_amount": pp_spent_amount,
                    "pp_delta_note": pp_delta_note,
                    "drained": r.get("drained"),
                    "rv": r.get("posted"), "error": err}
        raise NotImplementedError("hire_advisor requires Windows PE 1.19.5.0")
