"""外交关系、阵营、贸易、战争状态、借口报价/制造与宣战领域 Mixin。"""
from __future__ import annotations

import struct
import sys

from ._shared import (
    CO_RELATIONS_ARR,
    CO_STATES,
    CO_TRADE_ROUTES,
    FACTION_MEMBERS,
    REL_INDEX,
    ST_ID,
    _arr,
    _count,
    item_name,
)

class DiplomacyWarMixin:
    """外交关系、阵营、贸易、战争状态、借口报价/制造与宣战领域 Mixin。"""

    WIN_CO_DIPLO_STATUS = 0xF88
    WIN_DIPLO_RELATIONS_OFF = 0x08

    FACTION_VT_RVA = 0x4353758      # NFactions::CFaction vtable, 1.19.4.1

    def relations(self, tag=None, limit=60):
        """Relation records with other countries (array index = country index)."""
        p = self.p
        c = self._c(tag)
        self._load_tags()
        ptrs = self.country_ptrs()
        idx_to_tag = {}
        for tid, t in enumerate(self._tags):
            if t and t != "---":
                idx_to_tag[self._tag2idx[tid]] = t
        # --- PE 1.19.5.0 layout, verified 2026-11-16 -----------------------
        # CCountry+0xF88         -> CDiplomacyStatus*      (class_of agrees)
        # CDiplomacyStatus+0x08  -> CPdxArray<CRelationStatus*>  cap 440/size 440
        # The Linux values (CO_RELATIONS_ARR 0x2888, REL_INDEX 0x50) are stale
        # on hoi4.exe: 0x2888 points into the per-country state/province
        # registry, so the old path died on `read 0x52+4`, and +0x50 of a
        # PE CRelationStatus is 0. The array position IS the country index.
        if sys.platform == "win32":
            dip = p.u64(c.ptr + self.WIN_CO_DIPLO_STATUS)
            rows = _arr(p, dip, self.WIN_DIPLO_RELATIONS_OFF) if dip else []
        else:
            rows = _arr(p, c.ptr, CO_RELATIONS_ARR)
        out = []
        for i, r in enumerate(rows):
            t = idx_to_tag.get(i)
            if not t:
                continue
            if sys.platform == "win32":
                idx = i
            else:
                idx = p.i32(r + REL_INDEX)
            out.append({"tag": t, "ptr": hex(r), "index": idx})
            if len(out) >= limit:
                break
        return out

    def _faction_objects(self):
        """Every live NFactions::CFaction.

        2026-11-23 (复测 6 P0-1): Windows 1.19.5.0 优先 O(1) 直读
        `CGameState + 0x3F8 -> CFactionSystem + 0x20`（零扫描、永远实时反映
        战中新加入阵营如 BEL 入同盟国），仅在读取失败时回退全内存 vtable 扫描。
        """
        if sys.platform == "win32":
            try:
                p = self.p
                gs = self.gamestate
                if gs:
                    fs = p.u64(gs + 0x3F8)
                    if fs and 0x10000 < fs < 0x7FFFFFFFFFFF:
                        arr = _arr(p, fs, 0x20)
                        valid = [x for x in arr if 0x10000 < x < 0x7FFFFFFFFFFF]
                        if len(valid) == len(arr) and len(arr) <= 128:
                            return valid
            except Exception:
                pass
        pid = self.p.pid
        cache = getattr(self, "_faction_objects_cache", None)
        if cache and cache[0] == pid:
            return cache[1]
        if sys.platform == "win32":
            hits = self.win_scan_class("CFaction")
            self._faction_objects_cache = (pid, hits)
            return hits
        needle = struct.pack("<Q", self.p.base + self.FACTION_VT_RVA)
        hits = []
        for m in self.p.regions(writable=True):
            a, b = m["start"], m["end"]
            pos = a
            while pos < b:
                n = min(8 << 20, b - pos)
                try:
                    data = self.p.read(pos, n)
                except Exception:
                    pos += n
                    continue
                st = 0
                while True:
                    i = data.find(needle, st)
                    if i < 0:
                        break
                    hits.append(pos + i)
                    st = i + 8
                pos += n
        hits = sorted(set(hits))
        self._faction_objects_cache = (pid, hits)
        return hits

    def _faction_ptr(self, tag=None):
        """The CFaction this country belongs to, or 0.

        2026-11-23: Windows 下 _faction_objects() 已是 O(1) 实时直读 CFactionSystem，
        不再对单国永久缓存，确保战中新加入/退出阵营立即可见。
        """
        p = self.p
        c = self._c(tag)
        if sys.platform != "win32":
            cache = getattr(self, "_faction_ptr_cache", None)
            key = (p.pid, c.tag)
            if cache and cache[0] == key:
                return cache[1]
        found = 0
        for f in self._faction_objects():
            try:
                if c.ptr in _arr(p, f, FACTION_MEMBERS):
                    found = f
                    break
            except Exception:
                continue
        if sys.platform != "win32":
            self._faction_ptr_cache = ((p.pid, c.tag), found)
        return found

    def faction(self, tag=None):
        p = self.p
        f = self._faction_ptr(tag)
        if not f:
            return None
        nm = None
        for off in range(0, 0x200, 8):
            s = item_name(p, f + off)
            if s and s.isprintable() and 2 < len(s) < 48 and " " not in s[:1]:
                nm = s
                break
        members = []
        for m in _arr(p, f, FACTION_MEMBERS):
            cc = self.country_by_ptr(m)
            members.append(cc.tag if cc else hex(m))
        leader = 0
        try:
            leader = p.u64(f + FACTION_MEMBERS - 8)
        except Exception:
            pass
        lc = self.country_by_ptr(leader) if leader else None
        return {"ptr": hex(f), "name": nm, "class": self.oi.class_of(f),
                "members": members,
                "leader": lc.tag if lc else (members[0] if members else None)}

    def faction_members(self, tag=None):
        info = self.faction(tag)
        if not info:
            return {"faction": None, "members": []}
        return {"faction": info.get("name"), "leader": info.get("leader"),
                "members": info.get("members") or []}

    def create_trade(self, partner, resource, amount=None, factories=None,
                     tag=None, dry_run=False):
        """建立、调整或取消从 partner 进口 resource 的贸易协定（PE 1.19.5.0 原生 CCreateTradeCommand）。

        2026-11-23 (C-5):
          * `partner`: 出口国 TAG（如 'ROM' / 'SOV' / 'VEN' / 'SWE'）
          * `resource`: 资源名（'oil' / 'rubber' / 'tungsten' / 'steel' / 'aluminium' / 'chromium' / 'coal'）
          * `amount`: 目标进口总单位数（如 8 = 1 民工，16 = 2 民工，0 = 取消贸易）
          * `factories`: 民工快捷参数（与 amount 二选一，自动换算为 factories * 8）
        """
        if sys.platform != "win32":
            return {"ok": False, "error": "create_trade is currently implemented on Windows PE 1.19.5.0"}
        if amount is None:
            if factories is not None:
                amount = int(factories) * 8
            else:
                amount = 8
        import trade_win as TW
        return TW.create_trade(
            self,
            exporter_tag=partner,
            resource=resource,
            amount=int(amount),
            importer_tag=tag,
            dry_run=bool(dry_run),
        )

    def trade_routes(self, tag=None, limit=40, world=False):
        """Resource trade routes (exports / imports) for one country.

        2026-11-16: the PE path used to read CCountry+0x112B08, a Linux
        CCurrentGameState offset that lands ~1.1 MB past the end of the object
        and produced `count: 32758` bogus rows, so the tool was disabled with
        "no verified container".

        2026-11-19: the container was found, just not where CResourceDeliveryRoute
        lives.  The real book is CCountry+0x11F8 (CCountryResources) +0x728: one
        CPdxArray<CResourceExchange*> per strategic resource, holding the
        country's EXPORT deals.  See calling/trade_win.py for the full offset
        table and the 131-objects/131-entries proof that a deal is stored only
        on the supplier's side (imports are derived by scanning the world).
        """
        if sys.platform == "win32":
            import trade_win as TW
            try:
                if world:
                    rows = TW.world_trades(self)
                    return {"ok": True, "available": True, "world": True,
                            "count": len(rows),
                            "returned": min(len(rows), int(limit or 40)),
                            "routes": rows[:int(limit or 40)],
                            "resource_index_order": TW.RESOURCE_NAMES}
                c = self._c(tag)
                info = TW.country_trades(self, tag)
                exp, imp = info["exports"], info["imports"]
                net = {}
                for r in exp:
                    net[r["resource"]] = net.get(r["resource"], 0) - r["amount"]
                for r in imp:
                    net[r["resource"]] = net.get(r["resource"], 0) + r["amount"]
                lim = int(limit or 40)
                return {"ok": True, "available": True, "country": c.tag,
                        "export_count": len(exp), "import_count": len(imp),
                        "export_units": round(sum(r["amount"] for r in exp), 2),
                        "import_units": round(sum(r["amount"] for r in imp), 2),
                        "net": {k: round(v, 2) for k, v in sorted(net.items())},
                        "exports": exp[:lim], "imports": imp[:lim],
                        "truncated": len(exp) > lim or len(imp) > lim,
                        "scanned_countries": info["scanned_countries"],
                        "direction_note": "a deal is stored on the SUPPLIER's "
                                          "side only; `exports` is read, "
                                          "`imports` is derived by scanning "
                                          "every other country's export book",
                        "resource_index_order": TW.RESOURCE_NAMES}
            except Exception as e:
                return {"ok": False, "available": False, "routes": [],
                        "error": "%s: %s" % (type(e).__name__, e)}
        p = self.p
        out = []
        for r in _arr(p, self._c(tag).ptr, CO_TRADE_ROUTES)[:limit]:
            out.append({"ptr": hex(r), "class": self.oi.class_of(r)})
        return {"count": _count(p, self._c(tag).ptr, CO_TRADE_ROUTES), "routes": out}

    def wars(self, tag=None):
        """Wars we are in. PURE READS ONLY.

        This used to call `country_stats()`, which ptrace-calls ~45 trigger
        functions recovered from a 1.19.2 scan; the game died within 10 s every
        time `hoi4_wars` was invoked. `combat.enemy_tags` reads the CWarRelation
        objects straight out of the country's diplomacy status instead.
        """
        import combat
        c = self._c(tag)
        enemies = combat.enemy_tags(self, c.tag)
        try:
            recs = combat.combats(self, only_mine=True, me=c.tag)
        except Exception:
            recs = []
        mine = 0
        for r in recs:
            mine += (r.get("us") or {}).get("front") or 0
        # 2026-11-20 (P0 #3): `CCountry+0x12F0` reads 0 for EVERY country on the
        # PE build, including Italy with six divisions in combat and Ethiopia
        # with one -- verified live against the real battle list.  Reporting a
        # field that is dead everywhere is worse than not reporting it, so the
        # count is derived from the battle list and the dead field is dropped.
        return {"at_war": bool(enemies), "enemies": enemies,
                "active_combats": len(recs),
                "divisions_in_combat": mine,
                # 2026-11-21 (P1-8): hoi4_combat_report and this tool now count the
                # SAME list, so a difference can only be scope (all=true) or timing.
                # Name the source so a future mismatch is traceable instead of
                # looking like two tools disagreeing about reality.
                "active_combats_source": "combat.combats(only_mine=True, me=%s)" % c.tag,
                "armies_in_combat": None,
                "armies_in_combat_note": "CCountry+0x12F0 is dead on the PE build "
                                         "(0 for ITA/ETH while they had 6/1 divisions "
                                         "fighting); use divisions_in_combat"}

    def diplomatic_action_info(self):
        """A guide to the diplomatic actions."""
        # 2026-11-22 (P2-2): Windows _win_action_classes() 返回 `classes`，Linux 返回 `actions`。
        cls_info = self.diplomatic_action_classes() or {}
        actions = cls_info.get("actions")
        if actions is None:
            actions = cls_info.get("classes") or []
        try:
            incoming = self.command_help("CIncomingDiplomaticActionActingCommand")
        except Exception as e:
            incoming = {"error": repr(e)}
        out = {
            "ok": bool(cls_info.get("ok", True)),
            "count": len(actions),
            "actions": actions,
            "how": "CDiplomaticActionCommand carries a CDiplomaticAction at +0x28. "
                   "These are polymorphic structures of 0x90+ bytes containing embedded sub-objects; "
                   "generic diplomatic actions are not wrapped on Windows (use hoi4_justify_wargoal / "
                   "hoi4_declare_war). To answer incoming diplomatic offers, "
                   "CIncomingDiplomaticActionActingCommand can be used.",
            "incoming_command": incoming,
        }
        if cls_info.get("source"):
            out["source"] = cls_info["source"]
        if cls_info.get("note"):
            out["note"] = cls_info["note"]
        return out

    def wargoal_types(self):
        import diplomacy
        return sorted(diplomacy.wargoal_types(self))

    def _pick_target_state_id(self, target_tag):
        """First state owned by `target_tag` (used when the caller names none)."""
        try:
            for st in _arr(self.p, self.country(target_tag).ptr, CO_STATES):
                sid = self.p.i32(st + ST_ID)
                if sid > 0:
                    return sid
        except Exception:
            return None
        return None

    def ensure_wargoal_state_array(self, activity, state_id=None):
        """Make sure a CTimedWargoalActivity carries a non-empty state array.

        Returns a small report; `repaired` says whether the array had to be
        rebuilt.  The buffer comes from the game's own allocator so the engine
        can free it later, and the header is written as the engine's
        CPdxArray layout: +0x68 data / +0x70 capacity / +0x74 size.
        """
        if not activity:
            return {"ok": False, "why": "no activity"}
        p = self.p
        raw = activity.get("ptr")
        try:
            a = int(raw, 16) if isinstance(raw, str) else int(raw)
        except Exception:
            return {"ok": False, "why": "unreadable activity pointer"}
        try:
            size = p.i32(a + 0x74)
            cap = p.i32(a + 0x70)
            data = p.u64(a + 0x68)
        except Exception as e:
            return {"ok": False, "why": "read failed: %r" % (e,)}
        if size > 0 and 0x10000000000 < data < 0x7F0000000000:
            return {"ok": True, "repaired": False, "size": size, "capacity": cap}
        if state_id is None:
            return {"ok": False, "repaired": False, "size": size,
                    "why": "state array is empty and no state id was available"}
        import cmdwin as _cw
        buf = _cw.alloc(self, 16)
        p.write(buf, struct.pack("<i", int(state_id)))
        p.write(a + 0x68, struct.pack("<Q", buf))
        p.write(a + 0x70, struct.pack("<i", 1))
        p.write(a + 0x74, struct.pack("<i", 1))
        return {"ok": True, "repaired": True, "size": p.i32(a + 0x74),
                "capacity": p.i32(a + 0x70), "data": hex(buf), "state": int(state_id),
                "reason": ("PE engine creates CTimedWargoalActivity with an empty state array; "
                           "populated target state_id to prevent completion AV at hoi4.exe+0xB2E62E")}

    def _win_wargoal_types_map(self):
        """Windows 1.19.5.0 纯只读构建 {wargoal_name: CWarGoalType*} 映射并按 (pid, base) 缓存。"""
        p = getattr(self, "p", None)
        key = (getattr(p, "pid", None), getattr(p, "base", None))
        cached = getattr(self, "_win_wgt_cache", None)
        if getattr(self, "_win_wgt_cache_key", None) == key and cached:
            return cached
        found = {}
        try:
            db = self.oi.singleton("CWarGoalDatabase") if hasattr(self, "oi") else None
            if db:
                for it in _arr(p, db, 0x40):
                    nm = item_name(p, it + 0x18)
                    if not nm or not (2 < len(nm) < 48):
                        for o in range(0, 0x120, 8):
                            v2 = item_name(p, it + o)
                            if v2 and v2.isprintable() and 2 < len(v2) < 48 and not v2.startswith("/"):
                                nm = v2
                                break
                    if nm and nm not in found:
                        found[nm] = it
        except Exception:
            pass
        if not found and hasattr(self, "win_scan_class"):
            try:
                for it in (self.win_scan_class("CWarGoalType") or ()):
                    nm = item_name(p, it + 0x18)
                    if not nm or not (2 < len(nm) < 48):
                        for o in range(0, 0x120, 8):
                            v2 = item_name(p, it + o)
                            if v2 and v2.isprintable() and 2 < len(v2) < 48 and not v2.startswith("/"):
                                nm = v2
                                break
                    if nm and nm not in found:
                        found[nm] = it
            except Exception:
                pass
        if found:
            self._win_wgt_cache_key = key
            self._win_wgt_cache = found
        return found

    def _resolve_wargoal_type(self, wargoal, target_tag=None):
        """解析宣战理由类型指针，返回 (ptr, valid_names, explicit_missing)。

        2026-11-26 (P0-1 根治):
          绝不再用 `except: wgt = 0` 静默吞掉未解析的理由名（如 'annex'）。
          实测 `wgt == 0` 会落回 CGenerateWarGoalAction 默认 ctor 的空理由，引擎返回
          posted=1 但 200 天不生成 CTimedWargoalActivity。
        """
        wg = str(wargoal or "").strip()
        if not wg:
            return 0, [], True
        wmap = self._win_wargoal_types_map() if sys.platform == "win32" else {}
        if wg in wmap:
            return int(wmap[wg]), sorted(wmap.keys()), False
        try:
            ptr = int(self.resolve("wargoal:" + wg, target_tag) or 0)
            if ptr:
                return ptr, sorted(wmap.keys()), False
        except KeyError:
            return 0, sorted(wmap.keys()), True
        except Exception:
            pass
        if wmap:
            return 0, sorted(wmap.keys()), True
        # 离线极简 mock 无 CWarGoalDatabase：仅放行默认合法名 take_state，其余一律拒掉
        if wg != "take_state":
            return 0, ["take_state", "take_claimed_state", "take_core_state", "topple_government"], True
        return 0, [], False

    def justify_wargoal(self, target_tag, wargoal="take_state", states=None,
                        dry_run=False, force=False, tag=None):
        """Start justifying a war goal, gated by war_readiness_check unless force=True.

        2026-11-23 (D-5): 补齐 `tag=None` 可选形参（对齐 turnengine.run_plan 的 justify_wargoal 动作），
        避免在短上下文 Plan DSL 下抛 TypeError。

        We only choose the TARGET, and for types like take_state WHICH STATES;
        the duration and political power cost are decided by the game.
        Default is `take_state` (what the diplomacy dialog opens with): the
        script-only `annex_everything` never passes CanExecute in 1.19.4.1.
        """
        import diplomacy
        _ = tag  # Windows 命令通道按当前玩家会话发起，保留形参兼容 Plan DSL 与多国调用方
        if sys.platform == "win32":
            # 2026-11-26 (P0-1 根治): 前置 0 注入校验 wargoal 名称，绝不把未知理由名（如 'annex'）
            # 静默吞成 wgt=0 后伪造 ok=True！dry_run 与真写共用同一套解析门禁。
            wgt, valid_wgs, wg_missing = self._resolve_wargoal_type(wargoal, target_tag)
            if wg_missing:
                return {
                    "ok": False,
                    "dry_run": bool(dry_run),
                    "posted": 0,
                    "blocked_by": "unknown_wargoal",
                    "target": target_tag,
                    "wargoal": wargoal,
                    "wargoal_type_ptr": None,
                    "valid_wargoals": valid_wgs,
                    "suggested_wargoal": "take_state",
                    "error": (
                        "unknown wargoal type %r (not found in CWarGoalDatabase). "
                        "Use 'take_state' for standard diplomatic justification "
                        "(available: %s). Nothing was posted."
                        % (wargoal, ", ".join(valid_wgs) if valid_wgs else "take_state")
                    ),
                }
            # 2026-11-15: the old path called diplomacy._new_action_param ->
            # g.call(ctor, ...) on a PE backend whose setregs is not implemented;
            # winject then read a scratch flag it never wrote and hoi4.exe died
            # with GetLastError=299. The PE channel is now native (see
            # cmdwin.justify_wargoal): CGenerateWarGoalAction's own default ctor
            # builds the object, wrapped in CDiplomaticActionCommand -- the exact
            # same two-object shell that declares real wars (§34).
            if dry_run:
                # 2026-11-22 (P1-1): a dry run must show the PP verdict too -- that
                # is the whole point of asking before you commit.
                _q = {}
                try:
                    _q = self.wargoal_quote(target_tag, wargoal, states)
                except Exception:
                    pass
                _pp = _q.get("current_pp")
                _need = _q.get("estimated_upfront_pp", _q.get("estimated_total_pp"))
                return {"ok": True, "dry_run": True, "posted": 0,
                        "target": target_tag, "wargoal": wargoal,
                        "wargoal_type_ptr": hex(wgt) if wgt else None,
                        "wargoal_valid": True,
                        "political_power": _pp,
                        "estimated_upfront_pp": _need,
                        "estimated_total_pp": _q.get("estimated_total_pp"),
                        "estimated_pp_shortfall": _q.get("estimated_pp_shortfall"),
                        "estimated_can_justify_now": _q.get("estimated_can_justify_now"),
                        "would_refuse": (None if (_pp is None or _need is None)
                                         else bool(_pp < float(_need))),
                        "quote": _q,
                        "note": "dry_run: no command built or posted"}
            # No duplicate justification: one at a time, same as the Linux path.
            try:
                for _j in diplomacy.justifying(self):
                    if _j.get("source_class") != "CTimedWargoalActivity":
                        continue
                    if str(_j.get("target") or "").upper() == str(target_tag).upper():
                        return {"ok": False, "blocked_by": "already_justifying",
                                "error": "already justifying a war goal against %s" % target_tag,
                                "justifying": _j}
            except Exception:
                pass
            # 2026-11-22 (P1-1): the UI refuses a justification you cannot pay for
            # ("not enough political power"); CGenerateWarGoalAction's command
            # channel does not.  Measured live: PP 2 + a 40 PP quote drove the
            # country to **-36**, where no law / advisor / decision could be taken
            # for weeks while the justification itself barely moved.  Every number
            # needed is a pure read, so refuse and show them.
            _warn = None
            try:
                _q = self.wargoal_quote(target_tag, wargoal, states)
            except Exception:
                _q = {}
            try:
                _pp = int(self.player().political_power)
            except Exception:
                _pp = None
            _need = _q.get("estimated_upfront_pp", _q.get("estimated_total_pp"))
            if _need is not None and _pp is not None and _pp < float(_need):
                if not force:
                    return {"ok": False, "blocked_by": "political_power",
                            "error": ("justifying against %s requires about %.0f upfront political "
                                      "power (%s days x %.2f/day estimate) and you have %d. "
                                      "The game's UI refuses starting without enough PP; the raw "
                                      "command channel does not and drives PP negative (measured: -36), "
                                      "in addition to daily %.2f PP drain while running. "
                                      "Wait for PP, pick a cheaper goal, or pass force=true to override."
                                      % (target_tag, float(_need),
                                         _q.get("estimated_days"),
                                         float(_q.get("daily_pp_drain") or 0), _pp,
                                         float(_q.get("daily_pp_drain") or 0))),
                            "political_power": _pp,
                            "estimated_upfront_pp": _need,
                            "estimated_pp_shortfall": round(max(0.0, float(_need) - float(_pp)), 1),
                            "required_pp": _need,
                            "force_hint": "pass force=true to bypass the estimated upfront PP guard",
                            "quote": _q}
                _warn = ("force=true: PP %d is below estimated upfront ~%.0f PP; "
                         "engine will deduct PP upfront (may go negative) and pause if PP < %.2f/day"
                         % (_pp, float(_need), float(_q.get("daily_pp_drain") or 0.2)))
            import cmdwin
            c = self._c(target_tag)
            # 2026-11-19 (crash #7, 05:59): the activity's state array starts
            # EMPTY, and the engine formats the "war goal finished" message from
            # states[size-1] (+0x68/+0x70/+0x74) -- size 0 reads [-4] and the
            # game dies at hoi4.exe+0xB2E62E the moment the counter reaches
            # zero.  With the console's `instant_wargoal` the counter is already
            # zero on the first frame, so the old 2 s drain lost that race
            # 100% of the time (verified: exception.txt -> base+0xB2E62E).
            # => drain only a few frames, then put a state id into the array
            #    BEFORE the clock is allowed to move again.
            r = cmdwin.justify_wargoal(self, c.tag_id, wgt, seconds=0.05)
            self._settle(0.6)
            mine = None
            repaired = None
            for _ in range(10):
                self._settle(0.35)
                try:
                    for _j in diplomacy.justifying(self):
                        if _j.get("source_class") != "CTimedWargoalActivity":
                            continue
                        if str(_j.get("target") or "").upper() == str(target_tag).upper():
                            mine = _j
                            break
                except Exception:
                    pass
                if mine:
                    break
            if mine and int(mine.get("states_in_activity") or 0) <= 0:
                try:
                    repaired = self.ensure_wargoal_state_array(
                        mine, self._pick_target_state_id(target_tag))
                except Exception as _e:
                    repaired = {"ok": False, "why": repr(_e)}
                if repaired and repaired.get("ok"):
                    try:
                        for _j in diplomacy.justifying(self):
                            if str(_j.get("target") or "").upper() == str(target_tag).upper():
                                mine = _j
                                break
                    except Exception:
                        pass
            if mine and mine.get("ptr"):
                diplomacy.track_activity_ptr(self, mine["ptr"])
            # 2026-11-26 (P0-1 根治): 未读到 CTimedWargoalActivity 时绝不再返回 ok=True 假成功！
            ok_final = bool(r.get("ok")) and (mine is not None)
            out_j = {
                "ok": ok_final,
                "posted": r.get("posted"),
                "warning": _warn,
                "target": target_tag,
                "target_id": c.tag_id,
                "wargoal": wargoal,
                "wargoal_type_ptr": hex(wgt) if wgt else None,
                "action": hex(r.get("act") or 0),
                "cmd": hex(r.get("cmd") or 0),
                "started": mine,
                "verified": mine is not None,
                "wargoal_state_repaired": repaired,
                "note": (
                    None if mine else
                    "command was posted but no CTimedWargoalActivity appeared; "
                    "check hoi4_inspect(topic='war_readiness', target=%r) and ensure wargoal='take_state' "
                    "and game clock can advance" % target_tag
                ),
            }
            if not ok_final and mine is None:
                out_j["blocked_by"] = "justification_not_started"
                out_j["error"] = (
                    "justify_wargoal(%s, wargoal=%r) posted command but engine did not create "
                    "a CTimedWargoalActivity (check war_readiness, target legality, world tension, or wargoal type)"
                    % (target_tag, wargoal)
                )
            return out_j
        readiness = None
        if not force:
            try:
                import playbooks as _pb
                # NOTE: justify_wargoal has no `tag` parameter — using `tag` here
                # raised NameError, the except swallowed it and the whole veteran
                # gate silently disappeared (caught 2026-11-05 by calling it live).
                pbk = _pb.get_playbook(self.player().tag)
            except Exception:
                pbk = {}
            forbid = int(pbk.get("forbid_manual_justify_before_year") or 0)
            fallback = int(pbk.get("justify_fallback_year") or forbid)
            # 2026-11-06: a minor nation, or a major switched to `cheese_breakout`,
            # may justify freely — the historical no-justify rule only applies to
            # the seven majors in "historical" mode.
            try:
                import playbooks as _pbm
                if _pbm.get_playbook_mode(self.player().tag) != "historical":
                    forbid = 0
            except Exception:
                pass
            if forbid and not force:
                year = 0
                try:
                    year = int(self.date().get("year") or 0)
                except Exception:
                    pass
                done = set()
                try:
                    done = set(self.completed_focuses() or [])
                except Exception:
                    pass
                # 1936-1938: never. From the fallback year on: only when the focus
                # chain failed to deliver a casus belli (e.g. Poland accepted Danzig,
                # ai_chance 10/11 -> the ~9% branch that grants no wargoal).
                allowed = (year >= fallback
                           and any(f in done for f in ("GER_danzig_or_war",))
                           ) or year >= forbid
                if not allowed:
                    return {
                        "ok": False,
                        "blocked_by": "veteran_playbook",
                        "error": pbk.get("manual_justify_note", "manual justify forbidden"),
                        "hint": ("Historical line: use the focus tree. For a deliberate "
                                 "1936 opening (e.g. the Dutch/East-Indies rubber plan) pass "
                                 "force=true — the engine accepts it immediately; the "
                                 "playbook gate is advisory, not a game rule."),
                        "year": year,
                        "completed": sorted(f for f in ("GER_danzig_or_war",) if f in done),
                    }
            # 2026-11-16: justifying is a CLAIM, not an attack -- it starts a
            # 250-day clock and must be possible long before the army is
            # deployed.  Only the advisory half of the readiness check applies;
            # the border/front-tick blockers belong to declare_war.
            readiness = self.war_readiness_check(target_tag, hard=False, force=True)
            if readiness.get("blockers"):
                return {"ok": False, "blocked_by": "war_readiness_check",
                        "error": "; ".join(readiness["blockers"]),
                        "readiness": readiness}
        sp = []
        if states:
            want = {int(x) for x in states}
            for tag in (target_tag,):
                for st in _arr(self.p, self.country(tag).ptr, CO_STATES):
                    if self.p.i32(st + ST_ID) in want:
                        sp.append(st)
            missing = want - {self.p.i32(x + ST_ID) for x in sp}
            if missing:
                return {"ok": False, "error": "these states do not belong to the target: %s" % sorted(missing)}
        # 2026-11-19: the activity must never be born with an empty state array
        # (see the crash note above the helpers).  With no explicit states we
        # record the target's first state -- the engine only needs *a* state to
        # point the completion message at.
        guard_state_id = self.p.i32(sp[0] + ST_ID) if sp else None
        auto_state = None
        if not sp:
            auto_state = self._pick_target_state_id(target_tag)
            if auto_state is not None:
                guard_state_id = auto_state
                for st in _arr(self.p, self.country(target_tag).ptr, CO_STATES):
                    if self.p.i32(st + ST_ID) == auto_state:
                        sp = [st]
                        break
        # 2026-11-11: CGenerateWarGoalAction::CanExecute refuses silently when the
        # country cannot pay the engine's political-power price, and every caller only
        # saw "CanExecute refused every attempt" (three runs blamed the whole command).
        # Ask the engine for the quote first and report the real blocker.
        if not dry_run:
            try:
                for _j in diplomacy.justifying(self):
                    if _j.get("source_class") != "CTimedWargoalActivity":
                        continue        # a queued CGenerateWarGoalAction is not a justification
                    if str(_j.get("target") or "").upper() == str(target_tag).upper():
                        return {"ok": False, "blocked_by": "already_justifying",
                                "error": "already justifying a war goal against %s" % target_tag,
                                "justifying": _j}
            except Exception:
                pass
            try:
                quote = self.wargoal_quote(target_tag, wargoal, states)
            except Exception as _e:
                quote = None
            if (quote and quote.get("ok") and quote.get("can_justify_now") is False
                    and (quote.get("pp_shortfall") or 0) > 0):
                return {"ok": False, "blocked_by": "political_power",
                        "error": "not enough political power: %s" % (quote.get("note") or ""),
                        "current_pp": quote.get("current_pp"),
                        "required_pp": quote.get("required_pp"),
                        "pp_shortfall": quote.get("pp_shortfall"),
                        "quote": quote,
                        "states": [self.p.i32(x + ST_ID) for x in sp]}
        try:
            _pp0 = self.politics().get("political_power")
        except Exception:
            _pp0 = None
        ok, err = diplomacy.justify_wargoal(self, target_tag, wargoal, sp, dry_run=dry_run)
        _pp_spent = None
        mine = None
        if ok and not dry_run:
            # The engine accepts at most one justification at a time and refuses the
            # second one at execution time, long after CCommand::Run returned true.
            # Round 3 reported ok:true for commands that never became an activity.
            for _ in range(10):
                # A tick must run BEFORE justifying() is asked: the activity only
                # exists once the loop consumed the command, and the first query
                # used to cache an empty answer (objindex negative cache 2s/5s).
                self._settle(0.35)
                for _j in diplomacy.justifying(self):
                    if _j.get("source_class") != "CTimedWargoalActivity":
                        continue
                    if str(_j.get("target") or "").upper() == str(target_tag).upper():
                        mine = _j
                        break
                if mine:
                    break
        # 2026-11-19 guard #2: repair the state array the instant the activity
        # exists, so a 195-day timer can never end on an empty CPdxArray.
        state_guard = None
        if mine and not dry_run:
            try:
                state_guard = self.ensure_wargoal_state_array(mine, guard_state_id)
            except Exception as _e:
                state_guard = {"ok": False, "why": repr(_e)}
        # The PP is charged on a later tick too: read it AFTER the poll.
        try:
            _pp1 = self.politics().get("political_power")
            _pp_spent = (round(float(_pp0) - float(_pp1), 1)
                         if (_pp0 is not None and _pp1 is not None) else None)
        except Exception:
            _pp_spent = None
        if ok and not dry_run and not mine:
                try:
                    import session as _sess
                    _paused = _sess.is_paused(self)
                except Exception:
                    _paused = False
                if _paused:
                    # The command sits in the queue; the engine builds the activity on
                    # the next unpaused frame. Failing here made callers retry and leak
                    # one CGenerateWarGoalAction per attempt (round 4: six of them).
                    return {"ok": True, "queued": True,
                            "note": ("the game is PAUSED: the justification is queued and "
                                     "will start on the next unpaused frame - call "
                                     "hoi4_justifying after unpausing to confirm"),
                            "wargoal": wargoal,
                            "states": [self.p.i32(x + ST_ID) for x in sp]}
                # 2026-11-13: with the `instant_wargoal` cheat on there is no timed
                # activity at all - the CB is created on the same tick. A live
                # CWarGoal for (me, target) proves the justification completed, so
                # report success instead of "the engine charged PP and dropped it".
                try:
                    _cb = diplomacy.find_wargoal(self, target_tag)
                except Exception:
                    _cb = None
                if _cb:
                    return {"ok": True, "instant": True, "wargoal": wargoal,
                            "casus_belli": _cb, "pp_spent": _pp_spent,
                            "states": [self.p.i32(x + ST_ID) for x in sp]}
                return {"ok": False,
                        "blocked_by": "engine_dropped" if (_pp_spent or 0) > 0 else "not_started",
                        "pp_spent": _pp_spent,
                        "error": (("the engine charged %.1f PP but created no timed war-goal "
                                   "activity for %s — the justification action was dropped "
                                   "after the cost was taken" % (_pp_spent, target_tag))
                                  if (_pp_spent or 0) > 0 else
                                  ("the command was queued but no timed war-goal activity "
                                   "for %s appeared (the engine allows one justification at "
                                   "a time — check hoi4_justifying)" % target_tag)),
                        "wargoal": wargoal,
                        "states": [self.p.i32(x + ST_ID) for x in sp],
                        "active": diplomacy.justifying(self, include_queued=True)}
        return {"ok": ok, "error": err, "wargoal": wargoal, "dry_run": bool(dry_run),
                "pp_spent": _pp_spent,
                "states": [self.p.i32(x + ST_ID) for x in sp],
                "readiness": readiness,
                "state_guard": state_guard,
                # live progress straight from CTimedWargoalActivity (days_left/eta_date)
                "justifying": (diplomacy.justifying(self) if ok and not dry_run else [])}

    def wargoal_quote(self, target_tag, wargoal="take_state", states=None, tag=None):
        """对 target_tag 正当化指定州要多少 PP、还差多少、大概多少天（引擎级精确 PP）。"""
        import diplomacy
        if sys.platform == "win32":
            # PE 1.19.5.0: an ENGINE-EXACT quote is impossible without killing
            # the process. Two independent experiments (2026-11-16, crashes #6
            # and #7) proved both halves fatal:
            #   * writing the PP field politics+0xE0 -> process dies
            #     (the page / engine state does not tolerate external writes);
            #   * even with PP untouched, calling the command validator
            #     0x11C2420 (cmdwin.validate_declare_war) hits winject's stale
            #     scratch flag and takes the game down.
            # So the PE path is PURE READ + the game's own static cost model.
            # No injected calls, no writes, no post. `required_pp` stays None
            # and the reply says exactly why.
            import diplomacy as _D
            try:
                src = tag or self.player().tag
                _c = self.country(src)
                cur_pp = _c.political_power
                tgt = self.country(target_tag)
                # 2026-11-26: O(1) 直读当前发起国正当化，杜绝 0 正当化时触发 3.8 GB 全堆扫描
                acts = [x for x in _D.justifying_fast(self, tag=src)
                        if x.get("from") == src and
                        x.get("source_class") == "CTimedWargoalActivity"]
                concurrent = len(acts)
                state_ids = [int(x) for x in (states or [])]
                if not state_ids and wargoal in _D.WARGOAL_NEEDS_STATES:
                    state_ids = [s["id"] for s in self.states(target_tag)[:1]] \
                        if hasattr(self, "states") else [0]
                n = max(1, len(state_ids))
                days = _D._estimate_days(n, concurrent)
                total_pp = round(days * _D.WARGOAL_DAILY_PP, 1)
                est_shortfall = (
                    round(max(0.0, float(total_pp) - float(cur_pp)), 1)
                    if cur_pp is not None else None
                )
                est_can_now = (
                    bool(float(cur_pp) >= float(total_pp))
                    if cur_pp is not None else None
                )
                return {
                    "ok": True, "mode": "pe-static-estimate",
                    "source": src, "target": target_tag, "wargoal": wargoal,
                    "states": state_ids,
                    "current_pp": cur_pp,
                    "required_pp": None,
                    "pp_shortfall": None,
                    "estimated_upfront_pp": total_pp,
                    "estimated_pp_shortfall": est_shortfall,
                    "estimated_can_justify_now": est_can_now,
                    "justify_gate_note": (
                        "hoi4_justify_wargoal defaults to guarding against negative PP using "
                        "estimated_upfront_pp (~%.1f PP, matching game UI upfront check); "
                        "pass force=true to override" % total_pp
                    ),
                    "required_pp_note": (
                        "engine-exact PP threshold unavailable on PE: writing "
                        "political power and calling the diplomatic validator "
                        "both crash hoi4.exe (crashes #6/#7, 2026-11-16); "
                        "use estimated_upfront_pp / estimated_can_justify_now"
                    ),
                    "can_justify_now": None,
                    "can_justify_now_note": (
                        "validator 0x11C2420 is fatal on this build; see "
                        "estimated_can_justify_now for the static PP check used by "
                        "hoi4_justify_wargoal"
                    ),
                    "allowed_by_rules": None,
                    "daily_pp_drain": _D.WARGOAL_DAILY_PP,
                    "concurrent_justifications": concurrent,
                    "estimated_days": days,
                    "estimated_completion_date": _D._add_game_days(self, days),
                    "estimated_total_pp": total_pp,
                    "estimate_source": ("00_invasion.txt: 200 base + 50/extra state, "
                                        "x1.5 per concurrent justification; 0.2 PP/day "
                                        "(unmodified base; exact duration appears in CTimedWargoalActivity once started)"),
                    "note": "pure-read estimate; nothing was written or posted",
                }
            except Exception as e:
                return {"ok": False, "unsupported_on_windows": True,
                        "error": "PE static quote failed: %s" % e,
                        "target": target_tag, "wargoal": wargoal}
        sp, sids = [], []
        if states:
            want = {int(x) for x in states}
            for st in _arr(self.p, self.country(target_tag).ptr, CO_STATES):
                sid = self.p.i32(st + ST_ID)
                if sid in want:
                    sp.append(st)
                    sids.append(sid)
            missing = want - set(sids)
            if missing:
                return {"ok": False,
                        "error": "these states do not belong to %s: %s" % (target_tag, sorted(missing))}
        return diplomacy.wargoal_quote(self, target_tag, wargoal, sp, sids, source_tag=tag)

    def justifying(self):
        import diplomacy
        return diplomacy.justifying(self)

    def diplomatic_actions(self):
        import diplomacy
        cat = diplomacy.action_catalog()
        raw = diplomacy._load_action_cache()
        allmap = (raw or {}).get("_all") or {}
        return {"sendable": sorted(k for k in cat if k != "_all"),
                "all_classes": sorted(allmap)}

    def send_diplomatic_action(self, action_class, target_tag, dry_run=False):
        import diplomacy
        if sys.platform == "win32":
            return {"ok": False,
                    "blocked_by": "unsupported_platform",
                    "platform": "win32",
                    "unsupported_on_windows": True,
                    "alternatives": ["hoi4_justify_wargoal", "hoi4_declare_war"],
                    "error": "diplomatic action objects are built through the Linux "
                             "call path (setregs is missing on the PE backend); "
                             "only hoi4_justify_wargoal / hoi4_declare_war have native Windows channels.",
                    "action": action_class, "target": target_tag}
        ok, err = diplomacy.send_action(self, action_class, target_tag, dry_run=dry_run)
        return {"ok": ok, "error": err}

    def declare_war(self, target_tag, dry_run=False, force=False, override_checks=False,
                    ack_co_belligerents=False):
        """Declare war -- behind a preflight covering army, border, battle plan,
        casus belli, AND diplomatic escalation (factions / guarantees / subjects).

        `force` skips advisory warnings (rifles, division ratio, timeline).
        `ack_co_belligerents=True` acknowledges undefended co-belligerent borders
        (e.g. a guarantor/faction ally with 0 divisions on our shared border).
        `override_checks=True` bypasses the hard military/wargoal preflight gates.
        Note: diplomatic illegality (self, already at war, same faction ally,
        own subject/overlord) is still checked unless override_checks=True.
        """
        readiness = self.war_readiness_check(
            target_tag, force=force,
            hard=not override_checks,
            ack_co_belligerents=bool(ack_co_belligerents or override_checks),
        )
        _diplo_summary = {
            "target_faction": readiness.get("target_faction"),
            "guaranteed_by": readiness.get("guaranteed_by") or [],
            "overlord": readiness.get("overlord"),
            "subjects": readiness.get("subjects") or [],
            "co_belligerents": readiness.get("co_belligerents") or [],
            "new_co_belligerents": readiness.get("new_co_belligerents") or [],
            "co_belligerent_borders": readiness.get("co_belligerent_borders") or [],
            "total_enemy_divisions_est": readiness.get("total_enemy_divisions_est"),
        }
        if not readiness["ready"] and not dry_run:
            return {"ok": False, "blocked_by": "war_preflight",
                    "error": "; ".join(readiness["blockers"] or readiness["warnings"]),
                    "readiness": readiness,
                    **_diplo_summary,
                    "hint": ("Check readiness.blockers: draw battle plan with "
                             "hoi4_army_battleplans(enemy_tag=%s), verify casus belli, "
                             "and guard borders of any co-belligerents (guarantors / "
                             "faction allies) listed in co_belligerent_borders. "
                             "ack_co_belligerents=true confirms undefended co-belligerent "
                             "borders; override_checks=true bypasses hard gates."
                             % str(target_tag).upper())}
        import diplomacy
        import combat
        import time as _t
        if sys.platform == "win32":
            if dry_run:
                # BUG-08 (playtest 2026-11-15): dry_run used to fall through to the
                # real declaration -- a "probe" started the Dutch war. A dry run now
                # only evaluates the player-side gate and posts nothing.
                return {"ok": bool(readiness.get("ready")), "dry_run": True,
                        "posted": 0, "target": target_tag,
                        "readiness": readiness,
                        **_diplo_summary,
                        "warnings": readiness.get("warnings") or [],
                        "note": "dry_run: no command was built or posted"}
            # Real action channel on the PE build: CDeclareWarAction wrapped in a
            # CDiplomaticActionCommand.  The availability gate (action+0x70) is
            # bypassed, so war_readiness_check stays the only guard.
            import cmdwin
            c = self._c(target_tag)
            r = cmdwin.declare_war(self, c.tag_id)
            self._settle()
            # combat.war_relations scans live CWarRelation objects, but the object
            # index is a snapshot -- refresh=True is what makes a just-declared war
            # visible (a full rescan costs ~25 s, hence one shot rather than a poll).
            verified = None
            if r.get("ok"):
                try:
                    _t.sleep(2.0)
                    verified = target_tag.upper() in combat.enemy_tags(
                        self, self.player_tag(), refresh=True)
                except Exception:
                    verified = None
            return {"ok": bool(r.get("ok")), "posted": r.get("posted"),
                    "target": target_tag, "target_id": c.tag_id,
                    "action": r.get("act"), "cmd": r.get("cmd"),
                    "at_war_verified": verified,
                    **_diplo_summary,
                    "warnings": readiness.get("warnings") or []}
        cb = None if dry_run else diplomacy.find_wargoal(self, target_tag)
        ok, err = diplomacy.declare_war(self, target_tag, dry_run=dry_run)
        # 2026-11-13: CCommand::Run only queues the action, so a `true` return means
        # "CanExecute accepted and the command was posted", not "war exists". Poll
        # the war-relation table so the caller never has to guess.
        verified = None
        if ok and not dry_run:
            for _ in range(10):
                _t.sleep(1.0)
                try:
                    if target_tag in combat.enemy_tags(self):
                        verified = True
                        break
                except Exception:
                    pass
            if verified is None:
                verified = False
        return {"ok": ok, "error": err, "readiness": readiness,
                "wargoal": cb, "at_war_verified": verified,
                **_diplo_summary,
                "warnings": readiness.get("warnings") or []}
