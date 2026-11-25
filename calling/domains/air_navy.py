"""海军舰队与空军联队查询、任务下达及空军附着集团军领域 Mixin。"""
from __future__ import annotations

import struct
import sys

from ._shared import (
    CO_FLEETS,
    FL_NAME,
    _arr,
    _count_by,
    item_name,
)

class AirNavyMixin:
    """海军舰队与空军联队查询、任务下达及空军附着集团军领域 Mixin。"""

    def fleets(self, tag=None):
        p = self.p
        out = []
        c = self._c(tag)
        for i, f in enumerate(_arr(p, c.ptr, CO_FLEETS)):
            row = {"index": i, "ptr": hex(f), "name": None}
            if sys.platform == "win32":
                # PE 1.19.5: CFleet+0xE0 is a char* to the fleet name (measured:
                # "Kriegsmarine", "Unterseeboots-Flotte", "德意志国第1舰队").
                # +0xA8 CCountry*, +0xD0 CNavyTheaterGroup*, +0xD8 home CProvince*,
                # +0xC0 CPdxArray<CTaskForce*> (size at +0xCC).
                try:
                    np = p.u64(f + 0xE0)
                    row["name"] = p.cstr(np) if np else None
                except Exception:
                    pass
                try:
                    row["country"] = hex(p.u64(f + 0xA8) or 0)
                    row["navy_group"] = hex(p.u64(f + 0xD0) or 0)
                    row["home_port"] = hex(p.u64(f + 0xD8) or 0)
                    row["task_force_count"] = p.i32(f + 0xB8 + 0x0C)
                except Exception:
                    pass
            else:
                row["name"] = item_name(p, f + FL_NAME)
            out.append(row)
        return out

    def air_wings(self, tag=None, refresh=False, show_all=False, limit=0):
        """All CAirWing instances in live memory.

        Run 11 (work order 1107): the old version kept only the wings whose first
        0x200 bytes contained the raw CCountry* pointer. CAirWing does not store
        that pointer, so every wing in the game was filtered out and the air force
        never took part in a single run. Ownership is now left to the command
        layer: CAttachAirWingToArmyCommand::CanExecute refuses a foreign wing in
        C++ and returns false instead of corrupting anything.
        """
        if refresh:
            self.oi.build(force=True)
        wings = self.oi.instances("CAirWing")
        # 2026-11-08: the vtable scan returned every object TWICE, 0x10 apart
        # (434 hits for ~217 wings) because a CAirWing carries the same vtable
        # at +0x00 and at +0x10. Keep only the lower address of each pair, and
        # require a readable handle at +0x18 so a stray vtable word in data
        # cannot be mistaken for a wing.
        import struct as _st
        raw = sorted(set(wings))
        hs = set(raw)
        wings = [w for w in raw if (w - 0x10) not in hs]
        good = []
        for w in wings:
            try:
                _t, _i = _st.unpack("<ii", self.p.read(w + 0x18, 8))
            except Exception:
                continue
            if 0 < _i < 100000:
                good.append(w)
        # 2026-11-16 (PE): airops.scan_wings() scans the LINUX vtable
        # 0x440E200, which matches nothing on hoi4.exe -- that is why this
        # tool reported 0 wings while 280 real CAirWing objects existed.
        # airops_win reads them through the MSVC RTTI index instead.
        if sys.platform == "win32":
            try:
                import airops_win as AW
                me = self._c(tag).tag
                wings = AW.all_wings(self, refresh=refresh)
                owned = AW.country_wings(self, me, wings=wings)
                types = {}
                for w in wings:
                    types[w["type"]] = types.get(w["type"], 0) + 1
                # 2026-11-22 (P2-2): 仅当显式传 all=True 时返回全世界统计；
                # 传 tag="ITA" 等非玩家国家时一律通过 country_wings(self, me) 精确过滤。
                if show_all:
                    # 2026-11-16: the old code dumped all 401 wings on every
                    # call (~15k tokens). Default is now the player's own
                    # block; the whole world is opt-in and capped.
                    cap = int(limit or 80)
                    rows = []
                    for w in wings[:cap]:
                        row = {"ptr": hex(w["ptr"]), "id": w["handle"] >> 32,
                               "handle": hex(w["handle"]), "type": w["type"],
                               "sa": hex(w["sa"])}
                        row.update(AW.wing_state(self, w["ptr"]))
                        rows.append(row)
                    return {"count": len(wings),
                            "groups": len(AW.wing_groups(wings)),
                            "types": types, "returned": len(rows),
                            "truncated": len(wings) > len(rows),
                            "ownership": "global census (all=true)",
                            "wings": rows}
                owned_rows = owned.get("wings") or []
                cap = int(limit or 0)
                ret_rows = owned_rows[:cap] if cap > 0 else owned_rows
                return {"tag": me,
                        "count": owned.get("count", 0),
                        "returned": len(ret_rows),
                        "truncated": len(owned_rows) > len(ret_rows),
                        "total_in_world": len(wings),
                        "groups_in_world": len(AW.wing_groups(wings)),
                        "ownership": "CStrategicAir+0x90 = CCountryTag id "
                                     "(exact: CAirWing+0x38 -> pool+0x40 -> sa)",
                        "scope": "%s air force only: %d wings owned through "
                                 "CStrategicAir+0x90; pass all=true for the "
                                 "world census" % (me, owned.get("count", 0)),
                        "types": _count_by(owned_rows, "type"),
                        "wings": ret_rows}
            except Exception as e:
                return {"count": len(good), "raw_hits": len(raw),
                        "scan_error": repr(e),
                        "wings": [{"ptr": hex(w)} for w in good[:300]]}
        return {"count": len(good), "raw_hits": len(raw),
                "error": "Linux airops path removed; Windows PE only",
                "wings": [{"ptr": hex(w)} for w in good[:300]]}

    def set_air_missions(self, tag=None, fighter_mask=1, cas_mask=2):
        """Give the player's own wings a sensible default mission.

        2026-11-17: nothing in the whole stack ever set an air mission -- the 1940
        campaign ended with all 15 wings at `mission: 0` since 1936.  Fighters go
        to Air Superiority (1), CAS to Close Air Support (2); transports and naval
        bombers are left alone.  One command per type, no ctor calls.
        """
        if sys.platform != "win32":
            return {"ok": False, "error": "set_air_missions is the Windows path"}
        import airops_win as AW
        try:
            rows = AW.german_wings(self).get("wings") or []
        except Exception as e:
            return {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}
        out = []
        # 2026-11-17: this used to cover only `fighter` and `cas`, so the seven
        # tac_bomber wings and the naval bomber were never given a mission at all.
        # (A later revision also ran a "repair" pass for wings whose +0xA4 was 0,
        # on the theory that it was the enable bit -- it is not; see _light_alerts.)
        groups = [("fighter", int(fighter_mask)), ("cas", int(cas_mask)),
                  ("tac_bomber", int(cas_mask)), ("nav_bomber", 16)]
        wanted = {}
        for typ, mask in groups:
            for r in rows:
                if r.get("type") == typ:
                    wanted[int(r["handle"], 16)] = mask
        # repair: mask present but the wing is not actually flying
        by_mask = {}
        for h, m in wanted.items():
            by_mask.setdefault(m, []).append(h)
        for mask, handles in sorted(by_mask.items()):
            try:
                cmd = AW.make_set_mission(self, handles, mask, 1, 1)
                if not AW.can_exec(self, cmd, AW.CANEXEC_SET_MISSION):
                    out.append({"mask": mask, "ok": False, "wings": len(handles),
                                "error": "CStratAirSetMissionCommand::CanExecute refused"})
                    continue
                AW.post(self, cmd)
                # 2026-11-21 (续测 P1-8): the mask alone never makes a wing fly.
                # +0xA4 is a PERSISTENT enable flag (measured: written 1, still 1
                # after 1.8 s) and nothing in this tool ever set it, so five
                # fighters sat at mission=1 / mission_on=false through peace AND
                # through a declared war.  airops.enable_mission() cannot be used
                # here: ENABLE_MISSION_CTOR is a Linux ELF offset.  Set the flag
                # directly -- the engine keeps it (same file, WING_MISSION_ON).
                try:
                    import struct as _st8
                    for _w in (AW.german_wings(self).get("wings") or []):
                        if (_w.get("mission") or 0) == mask and _w.get("ptr"):
                            self.p.write(int(_w["ptr"], 16) + 0xA4, _st8.pack("<i", 1))
                except Exception as _e8:
                    out.append({"mask": mask, "enable_error": repr(_e8)[:120]})
                out.append({"mask": mask, "ok": True, "wings": len(handles)})
            except Exception as e:
                out.append({"mask": mask, "ok": False,
                            "error": "%s: %s" % (type(e).__name__, e)})
        # read back: a wing with no mask at all is the only thing we can prove
        # (see the +0xA4 measurement note in _light_alerts; it is not an enable bit)
        try:
            after = AW.german_wings(self).get("wings") or []
            still = [{"wing": r["id"], "type": r.get("type"),
                      "mask": r.get("mission")} for r in after
                     if r.get("type") != "transport_plane"
                     and not r.get("mission")]
        except Exception:
            still = None
        return {"ok": any(x.get("ok") for x in out), "results": out,
                "still_grounded": still}

    def attach_air_wings_to_armies(self, tag=None, refresh_oi=False,
                                   allow_unverified=False, wings=None):
        """Attach the player's air wings to the player's armies (round-robin).

        2026-11-17: the Windows build had NO working path here at all.  The body
        below still used the 1.19.4.1 Linux constants (ctor 0x241F840, Run
        0x20AE0A0) AND refused by default because wing ownership could not be
        proven -- so `attach_air_wings_to_armies()` returned
        `attached: 0, scanned: 656` every single tick of the 1940 campaign and the
        Luftwaffe never flew.  (Calling that ctor on PE would also have executed
        garbage at image_base+0x241F840.)

        The PE build has a verified command builder instead:
        `airops_win.make_attach` -> CAttachAirWingToArmyCommand
        (vt 0x2A97728, size 0x48, CanExecute 0x19D3920, +0x28 = wing handle
        array, +0x40 = army handle).  Ownership is proven by `german_wings()`
        (the deterministic id-1..k starting block), so only the player's own
        aircraft are ever handed to the command.
        """
        import api, armyops
        if getattr(self, "_air_attach_broken", False):
            return {"ok": False, "skipped": "circuit_breaker"}
        c = self._c(tag)
        ogs = [og for og in armyops.orders_groups(self, c)
               if not self.p.u8(og + armyops.OG_IS_GROUP)]
        if not ogs:
            return {"ok": False, "error": "no armies (COrdersGroup) to attach air wings to"}

        if sys.platform == "win32":
            import airops_win as AW
            _tag = c.tag
            # 2026-11-22 (P3-4): 实机证实 COrdersGroup+0x70 是 CPdxArray<CAirWing*>，
            # 直接从游戏内存读取当前已挂载映射，彻底消除读档/重启后缓存过期导致的假失败。
            def _live_wing_army_map(og_list):
                m = {}
                for _og in og_list:
                    try:
                        for _wp in _arr(self.p, _og, 0x70):
                            if _wp:
                                m[int(_wp)] = int(_og)
                    except Exception:
                        continue
                return m

            def _run_pass(do_refresh):
                ger = AW.country_wings(self, _tag, refresh=bool(do_refresh))
                if not ger.get("ok"):
                    return {"ok": False, "attached_count": 0, "scanned_wings": 0,
                            "error": ger.get("error") or "no player air-wing block"}
                rows = ger["wings"]
                if wings:
                    keep = {i for i, _h in AW.select_wings(self, wings)}
                    rows = [r for r in rows if r["id"] in keep]
                live_map = _live_wing_army_map(ogs)
                attached, refused = [], []
                done = getattr(self, "_air_attached", None)
                if done is None:
                    done = self._air_attached = set()
                _unattachable = ("nav_bomber", "naval_bomber", "strat_bomber",
                                 "strategic_bomber", "maritime_patrol_plane", "transport_plane")
                unattached_idx = 0
                for r in rows:
                    w = int(r["ptr"], 16)
                    cur_og = live_map.get(w)
                    if cur_og is not None:
                        # 内存实证已挂在我方某陆军集团军上，幂等直接通过，不做无谓改挂与注入
                        attached.append({
                            "wing": r["id"], "type": r["type"],
                            "army": hex(cur_og), "already_attached": True,
                            "verified_from_memory": True,
                        })
                        done.add((w, cur_og))
                        continue
                    if r.get("type") in _unattachable:
                        refused.append({"wing": r["id"], "type": r["type"],
                                        "reason": "wing_type_unsupported"})
                        continue
                    og = ogs[unattached_idx % len(ogs)]
                    unattached_idx += 1
                    try:
                        hw = self.p.u64(w + 0x18)          # packed CAirWing handle
                        hog = self.p.u64(og + 0x08)        # packed COrdersGroup handle
                        if not hw or not hog:
                            refused.append({"wing": r["id"], "reason": "stale_handle"})
                            continue
                        cmd = AW.make_attach(self, [hw], hog)
                        if not AW.can_exec(self, cmd, AW.CANEXEC_ATTACH):
                            refused.append({
                                "wing": r["id"], "type": r["type"],
                                "army": hex(og), "reason": "can_execute_refused",
                            })
                            continue
                        res = AW.post(self, cmd)
                        done.add((w, og))
                        attached.append({"wing": r["id"], "type": r["type"],
                                         "army": hex(og), "posted": res.get("posted")})
                    except Exception as e:
                        refused.append({"wing": r["id"], "reason": "error",
                                        "error": "%s: %s" % (type(e).__name__, e)})
                return {"ok": bool(attached), "backend": "windows",
                        "scanned_wings": len(rows), "attached_count": len(attached),
                        "refused": len(refused), "attached": attached,
                        "refused_detail": refused[:12]}

            res_out = _run_pass(refresh_oi)
            # 若首次全部拒绝且未显式刷新索引，清本地缓存并 refresh=True 重试一次自愈
            if not res_out.get("ok") and not refresh_oi and res_out.get("scanned_wings", 0) > 0:
                self._air_attached = set()
                res_retry = _run_pass(True)
                res_retry["retried_after_refresh"] = True
                return res_retry
            return res_out
        wings_all = self.oi.instances("CAirWing")
        if not wings_all and refresh_oi:
            self.oi.build(force=True)
            wings_all = self.oi.instances("CAirWing")
        if not wings_all:
            return {"ok": False, "error": "no CAirWing in the object index",
                    "scanned_wings": 0, "attached_count": 0}
        wings = wings_all
        if not allow_unverified:
            return {"ok": False, "skipped": "wing ownership unverified",
                    "scanned_wings": len(wings), "attached_count": 0,
                    "note": "see the 2026-11-07 canary: no owner field found in "
                            "CAirWing/CAirWingPool; the command accepts foreign wings"}
        already = getattr(self, "_attached_wings_set", None)
        if already is None or getattr(self, "_attached_wings_pid", None) != self.p.pid:
            already = set()
            self._attached_wings_set = already
            self._attached_wings_pid = self.p.pid
        api.allow_call(0x241F840)
        api.allow_call(0x20AE0A0)
        attached, refused = [], 0
        try:
            with self:
                errbuf = self.malloc(0x28)
                for w in wings:
                    if w in already:
                        continue
                    if self.oi.class_of(w) != "CAirWing":
                        continue
                    og = ogs[len(attached) % len(ogs)]
                    cmd = self.cmd.new(0x48)
                    self.p.write(cmd, b"\0" * 0x48)
                    self.call(0x241F840, (cmd, w, og, 0))
                    # C1 (review 2026-11-07): errbuf is a libstdc++ std::string.
                    # Writing 0x20 zero bytes set _M_p = NULL, so the moment
                    # CCommand::Run wrote a refusal reason libstdc++ dereferenced
                    # NULL -> SIGSEGV. A valid SSO empty string is
                    # _M_p = this+0x10, _M_string_length = 0, _M_local_buf[0] = 0.
                    self.p.write(errbuf, struct.pack("<QQB", errbuf + 0x10, 0, 0))
                    rv = self.call(0x20AE0A0, (cmd, errbuf))
                    if rv & 0xFF:
                        already.add(w)
                        attached.append({"wing": hex(w), "army": hex(og)})
                    else:
                        refused += 1
        except Exception as e:
            self._air_attach_broken = True
            return {"ok": False, "error": str(e), "attached_count": len(attached),
                    "scanned_wings": len(wings), "refused": refused}
        return {"ok": bool(attached) or bool(already),
                "scanned_wings": len(wings),
                "attached_count": len(attached),
                "refused": refused,
                "total_attached": len(already),
                "attached": attached[:40]}
