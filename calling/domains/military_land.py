"""陆军编制、新兵训练队列、师模板复制、集团军编组与陆战微操委托领域 Mixin。"""
from __future__ import annotations

import offsets as O
import legality
import struct
import sys

from ._shared import (
    CO_ARMIES,
    CO_CAPITAL_STATE,
    CO_DIV_TEMPLATES,
    CO_SUBUNITS,
    CO_THEATRES,
    DT_NAME,
    SU_CATEGORY,
    SU_KEY,
    _CONV_PROGRESS,
    _arr,
    item_name,
)

class MilitaryLandMixin:
    """陆军编制、新兵训练队列、师模板复制、集团军编组与陆战微操委托领域 Mixin。"""

    _VP_CACHE = {}

    PE_CREATE_CTOR   = 0x1C2B7D0
    PE_CREATE_VT     = 0x2A26EF8
    PE_CREATE_CANEXE = 0x1C2DBF0
    PE_CREATE_SIZE   = 0x290

    def armies(self, tag=None):
        p = self.p
        c = self._c(tag)
        out = []
        for i, a in enumerate(_arr(p, c.ptr, CO_ARMIES)):
            out.append({"index": i, "ptr": hex(a), "class": self.oi.class_of(a)})
        return {"count": len(out), "armies": out}

    def division_templates(self, tag=None):
        p = self.p
        c = self._c(tag)
        out = []
        for i, t in enumerate(_arr(p, c.ptr, CO_DIV_TEMPLATES)):
            out.append({"index": i, "name": item_name(p, t + DT_NAME), "ptr": hex(t)})
        return out

    def _template_handle(self, t_ptr):
        """Packed {type,id} handle of a CDivisionTemplate (it lives at +0x08)."""
        raw = self.p.read(t_ptr + 0x08, 8)
        t, i = struct.unpack("<ii", raw)
        return raw, t, i, (i << 32) | (t & 0xFFFFFFFF)

    def conveyors(self, tag=None):
        """Live division-training conveyors.

        CCountry+0xF48 -> CDeploymentStatus, its CPdxArray<CMilitaryDeploymentConveyor*>
        at +0x68. Verified live 2026-11-04: after CreateConveyor the array went from
        0 to 1 and the element's class resolves to CMilitaryDeploymentConveyor.
        (An `oi.instances()` scan cannot be used here — the index is a snapshot taken
        before the conveyor existed.)
        """
        p = self.p
        c = self._c(tag)
        out = []
        # Windows puts CDeploymentStatus at +0xF70 (offsets_win); the Linux table has
        # no name for it and has always used +0xF48. Reading the Linux offset on the PE
        # build returned a null pointer, so conveyors() was empty for ever and
        # train_divisions reported "no conveyor appeared" even when one had been made.
        dep_off = O.CO_DEPLOYMENT_STATUS
        conv_off = O.CO_DEPLOYMENT_CONVEYORS
        dep = p.u64(c.ptr + dep_off)
        if not dep:
            return out
        for conv in _arr(p, dep, conv_off):
            try:
                h_type, h_id = struct.unpack("<ii", p.read(conv + 0x08, 8))
            except Exception:
                continue
            row = {"ptr": hex(conv), "handle": [h_type, h_id],
                   "packed_handle": (h_id << 32) | (h_type & 0xFFFFFFFF)}
            # CMilitaryDeploymentConveyor.  The field map is build-specific:
            #   Linux 1.19.4.1: +0x18 template  +0x48 lines  +0x70 line array  +0x88 priority
            #   PE    1.19.5.0: +0x20 template  +0x50 priority  +0x78 line array
            #                   +0x4C deploy province (int32)
            # Verified 2026-11-15 by creating a conveyor and matching +0x20 against the
            # template pointer hoi4_division_templates returns.
            try:
                if sys.platform == "win32":
                    t_off, prio_off, arr_off = 0x20, 0x50, 0x78
                    row["deploy_province"] = p.i32(conv + 0x4C)
                else:
                    t_off, prio_off, arr_off = 0x18, 0x88, 0x70
                row["priority"] = p.i32(conv + prio_off)
                row["template"] = item_name(p, p.u64(conv + t_off) + DT_NAME)
                # 2026-11-22 (复测 5 §3.10): CPdxArray 布局为 {data +0, cap +8, size +0x0C}；
                # 旧代码读 `arr_off + 8` 读成了容量 (cap=6) 而非实际长度 (size=5)，导致
                # `lines: 6` 与 `lines_detail` 5 条长期不一致。现改为读 PDXARRAY_SIZE (+0x0C)。
                _arr_cap = p.i32(conv + arr_off + 8)
                _arr_size = p.i32(conv + arr_off + O.PDXARRAY_SIZE)
                row["lines"] = _arr_size
                row["lines_capacity"] = _arr_cap
                # 2026-11-21 (P2-5 / P2-4): the tool promised "deployment state and
                # its lines" and returned a bare integer.  Walk the line array once
                # and report each line's progress, the conveyor's deployment state,
                # and whether it just started a NEW batch -- a progress drop from
                # 0.991 to 0.008 is the next batch, not corruption.
                _rows, _done, _best = [], 0, 0.0
                for _ln in _arr(p, conv, arr_off):
                    _md = p.u64(_ln + 0x30 if sys.platform == "win32" else _ln + 0x28)
                    if not _md or _md < 0x10000000000:
                        continue
                    _pr = p.u32(_md + 0x60) / 1e5
                    _rows.append({"line_ptr": hex(_ln), "progress": round(_pr, 3),
                                  "done": _pr >= 0.999})
                    _best = max(_best, _pr)
                    _done += 1 if _pr >= 0.999 else 0
                row["lines_detail"] = _rows
                if len(_rows) != _arr_size:
                    row["lines_mismatch"] = {"array_size": _arr_size, "detail_count": len(_rows)}
                row["progress"] = round(_best, 3)
                row["lines_done"] = _done
                row["deployment_state"] = (
                    "no_location" if not row.get("deploy_province")
                    else "ready" if _rows and _done >= len(_rows)
                    else "training")
                _prev = _CONV_PROGRESS.get((p.pid, conv))
                if _prev is not None and _best + 0.01 < _prev and _best < 0.25:
                    row["new_batch"] = True
                    row["previous_progress"] = _prev
                _CONV_PROGRESS[(p.pid, conv)] = _best
            except Exception:
                pass
            out.append(row)
        return out

    def deployment_province(self, state_id=None, tag=None) -> int:
        """A province inside `state_id` that can host a deployment (VP province first).

        CSetConveyorLocationCommand does NOT take a state id: its Execute bounds-checks
        the value against the game's PROVINCE count (13414), and a state id such as 64
        is refused while Berlin's province 6521 is accepted. That single wrong unit is
        why every conveyor stayed at "No location set" and 100 %-trained divisions
        never appeared on the map.
        """
        import mapdata
        st = int(state_id or self._homeland_state_id(tag))
        key = (mapdata.GAME, st)
        if key in self._VP_CACHE:
            return self._VP_CACHE[key]
        import glob, os as _os, re
        provs = sorted((mapdata._load_state_provinces_raw()[0] or {}).get(st) or [])
        best = None
        for fn in glob.glob(_os.path.join(mapdata.GAME, "history", "states", "%d-*.txt" % st)):
            try:
                txt = open(fn, encoding="utf-8", errors="replace").read()
            except OSError:
                continue
            m = re.search(r"victory_points\s*=\s*\{([^}]*)\}", txt)
            if not m:
                continue
            pairs = []
            for line in m.group(1).splitlines():
                line = line.split("#")[0].strip()
                parts = line.split()
                if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
                    pairs.append((int(parts[1]), int(parts[0])))
            if pairs:
                best = max(pairs)[1]
        if best is None:
            best = provs[0] if provs else 6521
        self._VP_CACHE[key] = best
        return best

    def _homeland_state_id(self, tag=None) -> int:
        """A deployment state that is connected to the capital by land.

        HOI4 refuses to deploy a finished division into a state that has no land
        connection to the capital: East Prussia (state 5 / 763, behind the Polish
        corridor in 1936) is the classic trap — the training conveyor fills to 100 %
        and nothing ever appears on the map. `CCountry+0xFF0` is the capital STATE
        id, so the capital's own state is always the safe answer. The hard-coded
        German list is only a fallback for the case where the capital is occupied.
        """
        p = self.p
        c = self._c(tag)
        try:
            own = {int(s["id"]) for s in self.states(c.tag)}
        except Exception:
            own = set()
        cap = 0
        if CO_CAPITAL_STATE is not None:
            try:
                cap = p.i32(c.ptr + CO_CAPITAL_STATE)
            except Exception:
                cap = 0
        if cap and (not own or cap in own):
            return cap
        if c.tag == "GER":
            # Brandenburg 64 = Berlin; then the other core states west of the corridor.
            for pref in (64, 59, 60, 52, 51, 55, 42, 57, 65):
                if pref in own:
                    return pref
        # last resort: an owned state that is not one of the two East Prussian ids
        rest = sorted(x for x in own if x not in (5, 763))
        if rest:
            return rest[0]
        return 64

    def train_divisions(self, template_name=None, lines=2, state_id=None, tag=None,
                        conveyor=None):
        """Open a division-training conveyor and push its first line onto the map.

        If `conveyor` (hex pointer or packed handle of an existing conveyor from
        `hoi4_conveyors`) is given, adds `lines` parallel training lines to THAT
        existing conveyor instead of creating a brand-new conveyor (复测 5 §3.11).

        Three things killed the 1936 run here (2 conveyors x 5 lines, 17 months,
        0 divisions):
          * the deployment state was `states("GER")[0]` = East Prussia, an exclave
            behind the Polish corridor, so a finished division could never deploy;
          * 10 parallel lines competed for ~26 rifles/day, so no single division ever
            reached 100 % equipment;
          * the conveyor ran at normal equipment priority.
        The defaults are therefore: capital state, 2 lines, priority 2 (highest),
        unlimited series. Raise `lines` only when the rifle stockpile is large.
        """
        p = self.p
        c = self._c(tag)
        from commands import COMMANDS as _CMDS
        # 2026-11-22 (复测 5 §3.11): 支持指定已有传送带指针直接加线，不再被迫新建传送带
        if conveyor is not None and str(conveyor).strip():
            want = str(conveyor).strip().lower()
            existing = self.conveyors(c.tag)
            hit = None
            for cv in existing:
                if (str(cv.get("ptr") or "").lower() == want
                        or str(cv.get("packed_handle") or "") == want
                        or hex(int(cv.get("packed_handle") or 0)).lower() == want):
                    hit = cv
                    break
            if hit is None:
                return {"ok": False, "error": "no existing conveyor matches %r" % conveyor,
                        "conveyors": [x.get("ptr") for x in existing]}
            add_n = max(1, int(lines or 1))
            lines_before = int(hit.get("lines") or 0)
            with self:
                rv = self._run_cmd(self.cmd.make("AddConveyorLine",
                                                 conveyor_handle=hit["packed_handle"],
                                                 count=add_n))
            self._settle(0.4)
            after_rows = [x for x in self.conveyors(c.tag) if x.get("ptr") == hit.get("ptr")]
            lines_after = int(after_rows[0].get("lines") or lines_before) if after_rows else lines_before
            return {
                "ok": bool(lines_after > lines_before or (rv & 0xFF)),
                "mode": "add_lines_to_existing",
                "conveyor": hit.get("ptr"),
                "conveyor_handle": hit.get("handle"),
                "template": hit.get("template"),
                "added_requested": add_n,
                "lines_before": lines_before,
                "lines_after": lines_after,
                "error": getattr(self, "_last_cmd_error", "") if lines_after <= lines_before else "",
            }
        tmpls = self.division_templates(c.tag)
        if not tmpls:
            return {"ok": False, "error": "no division templates found"}
        pick = None
        for t in tmpls:
            if template_name and (t.get("name") == template_name
                                  or str(t.get("index")) == str(template_name)):
                pick = t
                break
        if not pick:
            pick = next((t for t in tmpls if "Infanterie" in (t.get("name") or "")
                         or "Infantry" in (t.get("name") or "")), tmpls[0])
        t_ptr = int(pick["ptr"], 16)
        _, h_type, h_id, packed = self._template_handle(t_ptr)
        if h_type <= 0:
            return {"ok": False, "error": "template handle looks invalid: %d" % h_type}
        before = {x["ptr"] for x in self.conveyors(c.tag)}
        with self:
            cmd = self.cmd.make("CreateConveyor", template_handle=packed, priority=2)
            rv = self._run_cmd(cmd)
        self._settle(0.6)
        new = [x for x in self.conveyors(c.tag) if x["ptr"] not in before]
        if not new:
            return {"ok": False, "run": bool(rv & 0xFF), "template": pick.get("name"),
                    "template_handle": [h_type, h_id],
                    "error": getattr(self, "_last_cmd_error", "") or "no conveyor appeared"}
        conv = new[0]
        ch = conv["packed_handle"]
        if state_id is None or int(state_id) in (5, 763):
            state_id = self._homeland_state_id(c.tag)
        prov = self.deployment_province(state_id, tag=c.tag)
        errors = []
        with self:
            rv_loc = self._run_cmd(self.cmd.make("SetConveyorLocation",
                                                 conveyor_handle=ch, province_id=int(prov)))
            if not (rv_loc & 0xFF):
                errors.append("location: %s" % (getattr(self, "_last_cmd_error", "") or "refused"))
            if "SetConveyorPriority" in _CMDS:
                rv_pri = self._run_cmd(self.cmd.make("SetConveyorPriority",
                                                     conveyor_handle=ch, priority=2))
                if not (rv_pri & 0xFF):
                    errors.append("priority: %s" % (getattr(self, "_last_cmd_error", "") or "refused"))
            if "SetConveyorSeries" in _CMDS:
                self._run_cmd(self.cmd.make("SetConveyorSeries",
                                            conveyor_handle=ch, series=0))
            if int(lines) > 1:
                self._run_cmd(self.cmd.make("AddConveyorLine",
                                            conveyor_handle=ch, count=int(lines) - 1))
        self._settle(0.4)
        return {"ok": True, "template": pick.get("name"), "template_handle": [h_type, h_id],
                "conveyor": conv["ptr"], "conveyor_handle": conv["handle"],
                "lines": int(lines), "state_id": int(state_id),
                "deploy_province": int(prov),
                "errors": errors,
                "deploy_error": getattr(self, "_last_cmd_error", "")}

    def deploy_ready_conveyors(self, tag=None, min_progress=0.0):
        """Push a conveyor's divisions onto the map (the in-game deploy button).

        2026-11-18: this used to report `deployed: <how many conveyors it poked>`,
        which is NOT a division count. Live test: 3 conveyors of 6 lines each with
        min_progress=0.3 returned `deployed: 3` while the division count went
        30 -> 45. It also deploys the WHOLE conveyor (every line), not "the
        finished division" the old docstring claimed. Both are now reported as
        they are: `deployed` is the real DIVISION delta, `lines` says how many
        divisions each conveyor pushed, and `conveyors_deployed` keeps the old
        meaning under a name that cannot be mistaken for divisions.
        """
        def _divs():
            # 2026-11-21 (P2-3): this used to read CCountry+0x290 by hand, and the
            # 2026-11-20 playtest got divisions_before: 35 here against
            # divisions_count: 30 from hoi4_army_status in the same minute.  Use
            # the SAME verified reader army_status uses, so they cannot disagree.
            try:
                return int(self.divisions(tag, limit=1)["count"])
            except Exception:
                return None
        before = _divs()
        deployed, attempts = 0, []
        for conv in self.conveyors(tag):
            prog = None
            if min_progress > 0:
                try:
                    _, prog = self._conveyor_progress(conv)
                except Exception:
                    prog = None
                if prog is None or prog < min_progress:
                    attempts.append({"conveyor": conv["ptr"], "skipped": True,
                                     "progress": prog, "need": min_progress})
                    continue
            with self:
                rv = self._run_cmd(self.cmd.make("DeployConveyor",
                                                 conveyor_handle=conv["packed_handle"]))
            attempts.append({"conveyor": conv["ptr"], "lines": conv.get("lines"),
                             "run": bool(rv & 0xFF),
                             "error": getattr(self, "_last_cmd_error", "")})
            if rv & 0xFF:
                deployed += 1
        if deployed:
            self._settle(0.5)
        after = _divs()
        delta = (after - before) if (before is not None and after is not None) else None
        out = {"conveyors": len(attempts), "conveyors_deployed": deployed,
               "deployed": delta, "divisions_before": before, "divisions_after": after,
               "attempts": attempts,
               "note": ("DeployConveyor pushes the WHOLE conveyor (all of its lines), "
                        "not a single finished division; use min_progress to gate it")}
        if deployed and delta is not None and delta <= 0:
            out["warning"] = ("the command was accepted but no division appeared -- the "
                              "conveyor may still be waiting for equipment")
        return out

    def _pe_create_template_cost(self, cmd):
        """Army XP the engine will charge for this create command.

        0x1C2BD70(cmd, &out) writes the cost into out; CanExecute then does
        `if (army_xp < cost) return false`.  Measured 2026-11-18 on a fresh
        1936 save: Infanterie-Division 8,000,000 / Gebirgs-Brigade 3,000,000 /
        Kavallerie-Brigade 3,000,000 — i.e. 80 / 30 / 30 army XP at 100,000 per
        XP.  It is a COST, not a count, which is exactly why CanExecute looked
        like it "always returns 0": every previous test ran with 0 army XP.
        """
        import cmdwin
        p = self.p
        buf = cmdwin.alloc(self, 0x40)
        try:
            p.write(buf, b"\0" * 0x40)
            self.call(p.base + 0x1C2BD70, (cmd, buf), absolute=True, force=True)
            return int(p.u64(buf))
        finally:
            try:
                cmdwin.free(self, buf)
            except Exception:
                pass

    def _pe_army_xp(self, tag=None):
        """Country army XP in the engine's 1/100000 display units."""
        import templedit
        c = self._c(tag)
        return int(templedit._pe_army_xp(self, int(c.tag_id)))

    def _pe_duplicate_template(self, source_name, tag=None, new_name=None, dry_run=False):
        """Create a new template from an existing one on the PE build.

        2026-11-23 (D-6): 支持 dry_run=True 零消耗询价（实扣 XP 随源模板营数/支援连动态变化），
        并补齐 cmdwin.attach_fake_vtable 防止队列释放命令对象时 CRT free 崩进程。
        """
        import cmdwin
        p = self.p
        c = self._c(tag)
        rows = self.division_templates(c.tag)
        src_ref = None
        for i, r in enumerate(rows):
            if str(source_name) in (str(i), str(r.get("name"))):
                src_ref = int(r["ptr"], 16)
                break
        if src_ref is None:
            return {"ok": False, "error": "template not found: %s" % source_name,
                    "have": [r.get("name") for r in rows]}
        before = len(rows)
        target = new_name or (source_name + " Copy")
        cmd = cmdwin.alloc(self, self.PE_CREATE_SIZE)
        p.write(cmd, b"\0" * self.PE_CREATE_SIZE)
        tagbuf = cmdwin.alloc(self, 0x10)
        p.write(tagbuf, struct.pack("<i", int(c.tag_id)) + b"\0" * 12)
        try:
            self.call(p.base + self.PE_CREATE_CTOR,
                      (cmd, src_ref + 0x18, 0, tagbuf, 0),
                      absolute=True, force=True)
        except Exception as e:
            try:
                cmdwin.free(self, tagbuf)
            except Exception:
                pass
            return {"ok": False, "stage": "ctor", "cmd": hex(cmd),
                    "error": "%s: %s" % (type(e).__name__, e)}
        try:
            cmdwin.free(self, tagbuf)
        except Exception:
            pass
        if p.u64(cmd) != p.base + self.PE_CREATE_VT:
            return {"ok": False, "stage": "ctor", "cmd": hex(cmd),
                    "error": "ctor wrote vptr %#x, expected %#x"
                             % (p.u64(cmd), p.base + self.PE_CREATE_VT)}
        # rename the embedded copy (MSVC std::string: 16-byte SSO/ptr union at +0,
        # size at +0x10, capacity at +0x18 -> 32 bytes total)
        na = cmd + 0x28 + 0x08
        nb = target.encode("utf-8")
        if len(nb) <= 15:
            p.write(na, nb.ljust(16, b"\0") + struct.pack("<QQ", len(nb), 15))
        else:
            sb = cmdwin.alloc(self, len(nb) + 1)
            p.write(sb, nb + b"\0")
            p.write(na, struct.pack("<QQQQ", sb, 0, len(nb), len(nb)))
        cost = self._pe_create_template_cost(cmd)
        xp = self._pe_army_xp(c.tag)
        can = bool(self.call(p.base + self.PE_CREATE_CANEXE, (cmd, 0),
                             absolute=True, force=True) & 0xFF)
        res = {"ok": False, "mode": "pe-create-division-template",
               "cmd": hex(cmd), "source": source_name, "new_name": target,
               "dry_run": bool(dry_run),
               "can_execute": can, "before": before,
               "xp_cost": cost, "xp_cost_display": round(cost / 100000.0, 2),
               "army_xp": xp, "army_xp_display": round(xp / 100000.0, 2),
               "xp_after_estimate": round(max(0.0, (xp - cost) / 100000.0), 2),
               "hint": (
                   "XP 费用由源模板当前营与支援连数量动态决定；如需在复制后改编，"
                   "可先对源模板调 hoi4_template_edit(template=%r, ..., dry_run=true) 预估改编差价，"
                   "确保总 XP 足够再执行。" % source_name
               )}
        if not can:
            if cost > xp:
                # The whole "CanExecute always returns 0" mystery, solved
                # 2026-11-18: this is an ordinary resource gate, not a wall.
                res["blocked_by"] = "army_xp"
                res["error"] = ("creating this template costs %.1f army XP, "
                                "you only have %.1f"
                                % (cost / 100000.0, xp / 100000.0))
                res["hint"] = ("army XP comes from national focuses, an army "
                               "chief with experience_gain_army, and combat — "
                               "hoi4_thresholds shows the pool")
            else:
                res["error"] = ("CCreateDivisionTemplateCommand::CanExecute "
                                "refused although the XP cost is covered "
                                "(%.1f <= %.1f) — unexpected, report it"
                                % (cost / 100000.0, xp / 100000.0))
            res["after"] = len(self.division_templates(c.tag))
            return res
        if dry_run:
            res["ok"] = True
            res["note"] = "dry_run: validated and quoted XP cost only; no command posted"
            return res
        cmdwin.attach_fake_vtable(self, cmd, self.PE_CREATE_VT)
        posted = cmdwin.post_drained(self, cmd, seconds=2.0)
        self._settle(0.3)
        after = self.division_templates(c.tag)
        res.update({"posted": posted, "after": len(after),
                    "templates": [r.get("name") for r in after],
                    "xp_after": self._pe_army_xp(c.tag),
                    "ok": len(after) > before})
        if not res["ok"]:
            res["error"] = "no new template appeared after posting"
        return res

    def duplicate_division_template(self, source_name, tag=None, new_name=None, dry_run=False):
        """Task 2 (2026-11-02 work order): borrow/copy an existing CDivisionTemplate.

        CCreateDivisionTemplateCommand ctor 0x1FDC190 deep-copies the source template
        (embedded at CReferencedDivisionTemplate+0x18) into the command and posts it
        through CCommand::Run -- the exact path the UI 'Duplicate' button uses.
        """
        # 2026-11-17: KILLED A LIVE GAME. The comment below (2026-11-10) said this
        # path was "reverted / stays disabled" but the code was still reachable, so
        # a real campaign called it and hoi4.exe died inside CCreateDivisionTemplateCommand
        # ctor (0x1FDC190) -- process vanished, no crash folder, ReadProcessMemory
        # returned 299 (ERROR_PARTIAL_COPY) inside self.call(). Everything after that
        # call in the session read "hoi4 process not found".
        # Hard-refuse on the PE build until the CDivisionTemplate /
        # CReferencedDivisionTemplate layering is actually verified by disassembly.
        if sys.platform == "win32":
            return self._pe_duplicate_template(source_name, tag, new_name, dry_run=dry_run)
        import struct as _s
        c = self._c(tag)
        arr = c.ptr + 0x1B8                      # CPdxArray<CReferencedDivisionTemplate*>
        d = self.p.u64(arr)
        n = min(self.p.i32(arr + 0x0C), 64)
        src_ref = None
        for i in range(n):
            t = self.p.u64(d + i * 8)
            nm = item_name(self.p, t + 0x20)
            if nm == source_name:
                src_ref = t
                break
        if src_ref is None:
            return {"ok": False, "error": "template not found: %s" % source_name,
                    "have": [item_name(self.p, self.p.u64(d + i * 8) + 0x20)
                             for i in range(n)]}
        # 2026-11-10 test: src_ref+0x18 crashed the ctor (sig=6 at 0x1FDC190);
        # reverted until the real CDivisionTemplate/CReferencedDivisionTemplate
        # layering is confirmed. CCreateDivisionTemplateCommand stays disabled.
        src_inner = src_ref + 0x18
        target_name = new_name or (source_name + " Copy")
        import api
        api.allow_call(0x1FDC190)
        with self:
            cmd = self.cmd.new(0x290)
            self.p.write(cmd, b"\0" * 0x290)
            tagbuf = self.malloc(16)
            try:
                self.p.write(tagbuf, _s.pack("<i", c.tag_id) + b"\0" * 12)
                self.call(0x1FDC190, (cmd, src_inner, 0, tagbuf, 0))
                # V2-01: the deep-copied template keeps the source name, so
                # CanExecute rejects the duplicate. Rewrite the embedded
                # std::string at (cmd+0x28)+0x08 before running.
                na = cmd + 0x28 + 0x08
                nb = target_name.encode("utf-8")
                if len(nb) <= 15:
                    self.p.write(na, _s.pack("<QQ", na + 0x10, len(nb)) + nb.ljust(16, b"\0"))
                else:
                    sb = self.malloc(len(nb) + 1)
                    self.p.write(sb, nb + b"\0")
                    self.p.write(na, _s.pack("<QQQ", sb, len(nb), len(nb) + 1))
                rv = self._run_cmd(cmd)
            finally:
                self.free(tagbuf)
        self._settle()
        arr2 = c.ptr + 0x1B8
        n2 = self.p.i32(arr2 + 0x0C)
        names = [item_name(self.p, self.p.u64(self.p.u64(arr2) + i * 8) + 0x20)
                 for i in range(min(n2, 64))]
        return {"ok": bool(rv & 0xFF) or n2 > n, "run": bool(rv & 0xFF),
                "new_name": target_name,
                "templates_before": n, "templates_after": n2,
                "templates": names,
                "error": getattr(self, "_last_cmd_error", "")}

    def template_composition(self, tag=None, only=None):
        """Division templates with their real battalion/company makeup."""
        p = self.p
        c = self._c(tag)
        out = []
        # 2026-11-16: the old key_at() probed every 8-byte slot for a
        # libstdc++-shaped std::string. On the PE build CSubUnitDefinition uses
        # an MSVC std::string (SSO buffer +0x18 / size +0x28), so every name
        # came back empty. templedit.subunit_name() now dispatches by platform
        # and handles TNullObject placeholder slots (size 0).
        import templedit

        def counts(tp, arr_off):
            hdr = p.try_read(tp + arr_off, 16)
            if not hdr:
                return {}
            data, cap, size = struct.unpack("<Qii", hdr)
            acc = {}
            for k in range(min(size, 60)):
                e = p.try_read(data + 8 * k, 8)
                if not e:
                    break
                ep = struct.unpack("<Q", e)[0]
                kk = templedit.subunit_name(p, ep)
                if not kk:
                    continue          # TNullObject / empty column slot
                acc[kk] = acc.get(kk, 0) + 1
            return acc

        for i, t in enumerate(_arr(p, c.ptr, CO_DIV_TEMPLATES)):
            name = item_name(p, t + DT_NAME)
            if only is not None and str(only) not in (str(i), str(name)):
                continue
            out.append({
                "index": i,
                "name": name,
                "line": counts(t, 0x68),
                "support": counts(t, 0x88),
                "ptr": hex(t),
            })
        return out

    def subunit_types(self, tag=None, filter_text=""):
        """Battalion types, used when building a division template.

        2026-11-10: CCountry+0x26C0 (CO_SUBUNITS) is an EMPTY array in 1.19.4.1 —
        this tool returned count 0 for every country. Fall back to the live
        CSubUnitDefinition pool (vtable scan anchored on a real template).
        """
        p = self.p
        c = self._c(tag)
        out = []
        for s in _arr(p, c.ptr, CO_SUBUNITS):
            k = item_name(p, s + SU_KEY)
            if k and (not filter_text or filter_text.lower() in k.lower()):
                out.append({"key": k, "category": item_name(p, s + SU_CATEGORY), "ptr": hex(s)})
        if not out:
            try:
                import templedit
                pool = templedit.live_pool(self)
                for k, ptr in sorted(pool.items()):
                    if k and (not filter_text or filter_text.lower() in k.lower()):
                        out.append({"key": k, "category": None, "ptr": hex(ptr),
                                    "source": "live_pool"})
            except Exception:
                pass
        return out

    def theatres(self, tag=None):
        p = self.p
        return [{"ptr": hex(t), "class": self.oi.class_of(t)}
                for t in _arr(p, self._c(tag).ptr, CO_THEATRES)]

    def order_groups(self, tag=None):
        """The country's ARMIES and ARMY GROUPS (COrdersGroup)."""
        import armyops
        p = self.p
        co = self.country(tag) if tag else self.player()
        out, seen = [], set()
        for og in armyops.orders_groups(self, co):
            seen.add(og)
            out.append(armyops.og_info(self, og))
            parent = p.u64(og + armyops.OG_PARENT)
            if parent and parent not in seen:
                seen.add(parent)
                out.append(armyops.og_info(self, parent))
        # 2026-11-18: `og_info` reads member_armies from og+0x228+0xC, which on a
        # freshly created army group reported 615 for two armies. Recompute every
        # group's membership from the real parent links (one pass, no guessing),
        # and make a group's division count the sum of its armies.
        kids = {}
        for row in out:
            pg = row.get("parent_group")
            if pg:
                kids.setdefault(pg, []).append(row)
        for row in out:
            if row.get("is_army_group"):
                mine = kids.get(row["ptr"], [])
                row["member_armies"] = len(mine)
                row["member_ptrs"] = [k["ptr"] for k in mine]
                row["divisions"] = sum(int(k.get("divisions") or 0) for k in mine)
                row["members_source"] = "parent_links"
        return {"count": len(out), "groups": out}

    def unassigned_divisions(self, tag=None):
        """Divisions not attached to any army (CArmy*)."""
        co = self.country(tag) if tag else self.player()
        divs = _arr(self.p, co.ptr, CO_ARMIES)
        if not divs:
            return []
        import armyops as _ao
        import fastmem as _fm
        og_off = getattr(_ao, "CARMY_OG", 0xC0)
        batch = _fm.read_batch(self.p, [(d + og_off, 8) for d in divs])
        if batch is not None:
            out = []
            for d, raw in zip(divs, batch):
                if raw is not None and len(raw) == 8:
                    if struct.unpack("<Q", raw)[0] == 0:
                        out.append(d)
                elif self.p.u64(d + og_off) == 0:
                    out.append(d)
            return out
        return [d for d in divs if self.p.u64(d + og_off) == 0]

    def create_army(self, divisions=None, count=None, tag=None):
        """Collect divisions into a new army (the '+' button in the UI).

        The command is QUEUED (CCommand::Run) and only takes effect when the game
        advances a tick. In a paused game the old code reported success while no
        army existed, and a second call in the same paused tick took the SAME
        divisions again and silently did nothing. Both are reported honestly now.
        """
        if sys.platform == "win32":
            import armyops_win as armyops
        else:
            import armyops
        def _n():
            try:
                return (self.order_groups(tag) or {}).get("count")
            except Exception:
                return None
        before = _n()
        divs = divisions or self.unassigned_divisions(tag)
        if count:
            divs = divs[:count]
        if not divs:
            return {"ok": False, "error": "no divisions to assign"}
        ok, err = armyops.create_army(self, divs)
        after = _n()
        res = {"ok": ok, "error": err, "divisions": len(divs),
               "armies_before": before, "armies_after": after}
        rej = getattr(self, "_last_division_rejected", None)
        if rej:
            res["rejected"] = list(rej[:10])
        if sys.platform == "win32":
            # armyops_win calls 0xC417A0 directly, so the army exists the moment
            # the call returns -- no queue, no tick, nothing to wait for.
            return res
        if ok and before is not None and after is not None and after <= before:
            res["queued"] = True
            res["note"] = ("army count %s -> %s: the command is queued and lands on the "
                           "next game tick; do not issue a second create_army in the "
                           "same paused tick, it would take the same divisions again"
                           % (before, after))
        return res

    def add_divisions_to_army(self, orders_group, divisions=None, count=None,
                              from_army=None, order_id=None, tag=None,
                              move_to_province=None, spread=True,
                              max_per_province=None):
        """将未编组师或指定源集团军的师并入已存在的集团军（不建新军）。

        2026-11-23 (C-3 & 复测 11 P2-1):
          1. `divisions` 与 `from_army` 均省略：把当前未编组师（自动避开伞兵）并入 `orders_group`
             （未指定 `count` 时自动补足至 24 师上限，或全部未编组师）；
          2. 指定 `from_army`：把源集团军的师全部（或前 `count` 个）并入目标军（合并散军）；
          3. 指定 `divisions=[ptr, ...]`：精确并入指定师列表；
          4. 默认仅改归属不改物理位置（返回 `positions_moved: False`）；若同时传入
             `move_to_province=<province_id>`，并军成功后自动调用 `move_divisions(..., spread=spread)`
             把转入的师一步调离原省份（避免继续吃前线补给）。
        """
        og = int(orders_group, 16) if isinstance(orders_group, str) else int(orders_group or 0)
        if not og:
            return {"ok": False, "error": "orders_group is required"}
        if sys.platform != "win32":
            return {"ok": False, "error": "add_divisions_to_army is currently implemented on Windows PE 1.19.5.0"}
        import armyops_win as _aw
        import divreserve as _dr

        if divisions is not None:
            divs = [int(x, 16) if isinstance(x, str) else int(x) for x in divisions]
        elif from_army is not None:
            src_og = int(from_army, 16) if isinstance(from_army, str) else int(from_army)
            if src_og == og:
                return {"ok": False, "error": "from_army and orders_group are the same army"}
            divs = list(_aw.divisions_of(self, src_og))
        else:
            divs = [d for d in self.unassigned_divisions(tag)
                    if not _dr.is_paradrop_division(self, d)[0]]
            if count is None:
                cur_n = len(_aw.divisions_of(self, og))
                room = max(0, 24 - cur_n)
                if room > 0:
                    divs = divs[:room]
        if count is not None:
            divs = divs[:max(0, int(count))]
        if not divs:
            return {"ok": False, "error": "no eligible divisions to add to army %s" % hex(og)}
        res = _aw.add_divisions_to_army(self, og, divs, order_id=order_id)
        if move_to_province is not None and isinstance(res, dict) and res.get("ok"):
            moved_ptrs = res.get("transferred_divisions") or [hex(d) for d in divs]
            if moved_ptrs:
                mr = self.move_divisions(
                    divisions=moved_ptrs,
                    province=int(move_to_province),
                    tag=tag,
                    spread=bool(spread),
                    max_per_province=max_per_province,
                )
                res["move_result"] = {
                    "ok": bool(mr.get("ok")),
                    "province": mr.get("province"),
                    "spread": bool(mr.get("spread")),
                    "spread_plan": mr.get("spread_plan"),
                    "ordered": mr.get("ordered"),
                    "accepted": mr.get("accepted"),
                    "moving": mr.get("moving"),
                    "blocked_by": mr.get("blocked_by"),
                    "error": mr.get("error"),
                }
                res["positions_moved"] = bool((mr.get("accepted") or 0) > 0)
                if res["positions_moved"]:
                    res["position_note"] = (
                        "transferred %d division(s) and issued move orders toward province %d (spread=%s)"
                        % (len(moved_ptrs), int(move_to_province), bool(spread))
                    )
        return res

    def create_army_group(self, armies=None, tag=None):
        """Collect armies into a new army group (field marshal command)."""
        p = self.p
        if sys.platform == "win32":
            import armyops_win as AW
            co = self.country(tag) if tag else None
            mine = [og for og in AW.armies(self, co)
                    if not p.u8(og + AW.OG_IS_GROUP) and not p.u64(og + AW.OG_PARENT)]
        else:
            import armyops
            mine = [og for og in armyops.orders_groups(
                        self, self.country(tag) if tag else None)
                    if not p.u8(og + armyops.OG_IS_GROUP)
                    and not p.u64(og + armyops.OG_PARENT)]
        if armies is None:
            armies = mine
        if len(armies) < 1:
            return {"ok": False, "error": "no armies to group"}
        if sys.platform == "win32":
            ok, err, info = AW.create_army_group(self, armies)
            out = {"ok": ok, "error": err, "armies": len(armies)}
            out.update(info or {})
            return out
        import armyops
        ok, err = armyops.create_army_group(self, armies)
        return {"ok": ok, "error": err, "armies": len(armies)}

    def assign_commander(self, orders_group, character_key=None, leader_ptr=None,
                         allow_unverified=False):
        """Assign a commander to an army or army group.

        Player rules are enforced: only a FIELD MARSHAL may lead an army group, only a
        corps commander. If the character's `visible` condition is not met,
        For example a general not visible under the current government is REFUSED.
        """
        import armyops
        if isinstance(orders_group, str):
            orders_group = int(orders_group, 16)
        if isinstance(leader_ptr, str):
            leader_ptr = int(leader_ptr, 16)
        key = character_key
        if leader_ptr is None:
            for gen in self.generals()["generals"]:
                if gen["key"] == character_key or gen["name"] == character_key:
                    leader_ptr = int(gen["leader_ptr"], 16)
                    key = gen["key"]
                    break
            else:
                return {"ok": False,
                        "error": "找不到该将领: %s (先跑 hoi4_available_generals 看可用的 key)" % character_key}
        ok, why = legality.check_commander(self, orders_group, leader_ptr, key, allow_unverified)
        if not ok:
            return {"ok": False, "error": why, "rejected_by": "player_rules"}
        if sys.platform == "win32":
            # armyops.set_army_leader is built on the Linux call path (g.malloc +
            # a fixed 1.19.4.1 ctor VA), which does not exist on the PE build.
            # CSetArmyLeaderCommand's ctor is (this, CArmyLeader*, COrdersGroup*, u8),
            # so the three fields are leader / army / flag -- and cmdwin already knows
            # how to build it through the real ctor.
            self.send_command("SetArmyLeader",
                              {"arg0": leader_ptr, "arg1": orders_group, "arg2": 0})
            self._settle(0.5)
            after = armyops.og_info(self, orders_group).get("leader_ptr")
            same = bool(after) and int(after, 16) == int(leader_ptr)
            return {"ok": same, "leader_ptr": hex(leader_ptr), "after": after,
                    "error": "" if same else "the game did not attach the leader"}
        ok, err = armyops.set_army_leader(self, orders_group, leader_ptr)
        return {"ok": ok, "error": err}

    def division_detail(self, tag=None, limit=200, order_group=None):
        """Divisions one by one, using the game's own field names: organisation, strength,
        experience, entrenchment, fuel, supply, parent army, template, location."""
        import fieldmap, armyops, unitorders
        p = self.p
        fm = fieldmap.field_map(self, "CArmy")
        keep = {"organisation", "strength", "experience", "dig_in", "dig_in_cap",
                "fuel", "max_supply", "army_current_supply_ratio", "out_of_supply_days",
                "str_damage", "org_damage", "killed"}
        if order_group:
            divs = armyops.group_divisions(self, order_group)
        else:
            divs = list(_arr(p, self._c(tag).ptr, CO_ARMIES))
            # Divisions assigned to an army group are no longer in the country
            # array; without this union a fully grouped army reads as empty
            # (measured 1944-06: 225 divisions on the map, 0 returned).
            seen = set(divs)
            try:
                for og in (self.order_groups(tag).get("groups") or []):
                    gptr = og.get("ptr")
                    if isinstance(gptr, str):
                        gptr = int(gptr, 16)
                    if not gptr:
                        continue
                    try:
                        for d in armyops.group_divisions(self, gptr):
                            if d not in seen:
                                seen.add(d)
                                divs.append(d)
                    except Exception:
                        continue
            except Exception:
                pass
        out = []
        lost = 0
        target_divs = divs[:limit]
        import fastmem as _fm
        use_batch = (
            bool(target_divs)
            and getattr(unitorders.province_of, "__module__", "") == "unitorders"
            and getattr(unitorders.movement, "__module__", "") == "unitorders"
        )
        batch_rows = (
            _fm.read_divisions_batch(
                p,
                target_divs,
                off_prov_ptr=unitorders.CARMY_PROVINCE,
                off_prov_id=unitorders.PROV_ID,
                off_path_ptr=unitorders.CARMY_PATH,
                off_path_len=unitorders.CARMY_PATH_SIZE,
                off_tmpl=0x3B8,
                off_og=armyops.CARMY_OG,
                off_handle=unitorders.CARMY_HANDLE,
            )
            if use_batch
            else None
        )
        tmpl_names = {}
        og_names = {}

        def _tmpl_name(t_ptr):
            if not t_ptr:
                return None
            if t_ptr not in tmpl_names:
                tmpl_names[t_ptr] = item_name(p, t_ptr + DT_NAME)
            return tmpl_names[t_ptr]

        def _og_name(og_ptr):
            if not og_ptr:
                return None
            if og_ptr not in og_names:
                og_names[og_ptr] = p.stdstring(og_ptr + armyops.OG_NAME)
            return og_names[og_ptr]

        for i, a in enumerate(target_divs):
            # One division that dies / merges mid-read used to abort the whole
            # loop: 225 divisions on the map became 0 and staff_army trained 108
            # phantom divisions on the empty answer.
            try:
                if batch_rows is not None:
                    br = batch_rows[i]
                    if not a or a < 0x10000 or br is None or not br["vtable"]:
                        lost += 1
                        continue
                    tmpl = br["template"]
                    og = br["og"]
                    row = {
                        "index": i,
                        "ptr": hex(a),
                        "handle": br["handle"],
                        "province": br["prov"],
                        "moving": bool(br["path"]),
                        "template": _tmpl_name(tmpl),
                        "army": _og_name(og),
                        "army_ptr": hex(og) if og else None,
                    }
                else:
                    if not a or a < 0x10000 or not p.u64(a):
                        lost += 1
                        continue
                    tmpl = p.u64(a + 0x3B8)
                    og = p.u64(a + armyops.CARMY_OG)
                    row = {
                        "index": i,
                        "ptr": hex(a),
                        "handle": [p.i32(a + 0x18), p.i32(a + 0x1C)],
                        "province": unitorders.province_of(self, a),
                        "moving": bool(unitorders.movement(self, a)),
                        "template": _tmpl_name(tmpl),
                        "army": _og_name(og),
                        "army_ptr": hex(og) if og else None,
                    }
                row.update(fieldmap.read_fields(self, a, fm, keep))
                out.append(row)
            except Exception:
                lost += 1
                continue
        res = {"count": len(divs), "divisions": out}
        if lost:
            res["skipped"] = lost
        return res

    def divisions(self, tag=None, limit=200, deep=False):
        """The country's divisions (CArmy in the game's code).

        2026-11-16: this used to read `CCountry+0x290` of whatever pointer
        `country(tag)` handed back.  After the 1939.05.07 crash-restart that
        pointer was not Germany's, so the count was 0 while 109 divisions stood
        in the Reich and every land-war tool answered "no divisions".
        Resolution now goes through `divenum`, which matches the tag id the
        CCountry object carries itself, and `deep=True` adds an RTTI fallback
        when the array is empty.  `count` is never silently zero any more: a
        failed resolution raises, and a disagreement with the deep scan is
        reported in `warning`.
        """
        import divenum
        import fastmem as _fm
        import unitorders as _uo
        p = self.p
        c = self._c(tag)
        me = c.tag
        info = divenum.armies(self, me)
        rows = info["armies"]
        count = info["count"]
        source = info["source"]
        warning = None
        if count <= 0 and deep:
            try:
                hits = divenum.deep_scan(self, me)
            except Exception as e:
                hits = []
                warning = "deep scan failed: %r" % (e,)
            if hits:
                warning = ("CCountry+0x290 is empty but %d CArmy object(s) stand in "
                           "provinces of states %s owns" % (len(hits), me))
                rows, count, source = hits, len(hits), "rtti-scan"
        target_rows = rows[:limit]
        batch_rows = (
            _fm.read_divisions_batch(
                p,
                target_rows,
                off_prov_ptr=_uo.CARMY_PROVINCE,
                off_tmpl=0x3B8,
            )
            if target_rows
            else []
        )
        tmpl_names = {}
        out = []
        for i, a in enumerate(target_rows):
            br = batch_rows[i] if i < len(batch_rows) else None
            if br is not None:
                tmpl = br["template"]
                prov_ptr = br["prov_ptr"]
            else:
                tmpl = p.u64(a + 0x3B8)
                prov_ptr = p.u64(a + _uo.CARMY_PROVINCE)
            if tmpl:
                if tmpl not in tmpl_names:
                    tmpl_names[tmpl] = item_name(p, tmpl + DT_NAME)
                tname = tmpl_names[tmpl]
            else:
                tname = None
            out.append({"index": i, "ptr": hex(a),
                        "template": tname,
                        "province_ptr": hex(prov_ptr)})
        res = {"count": int(count), "divisions": out, "source": source,
               "country_ptr": hex(info["ptr"]), "tag_id": info["tid"],
               "verified": bool(info.get("verified"))}
        if warning:
            res["warning"] = warning
        return res

    def army_status(self, tag=None, deep=False):
        """One verified answer to "how many divisions do I have, and where".

        `hoi4_army_status` used to be `divisions()["count"]` plus three other
        reads, so a bad country pointer showed up as a calm `0`.  Any caller
        that gates a declaration of war on this number gets `countable: false`
        instead of a lie.

        `deep=True` adds the RTTI fallback.  It is off by default because the
        first `CArmy` vtable scan of a game process takes ~40 s (afterwards it
        is cached); `hoi4_division_selftest` is the tool that pays that cost on
        purpose, and `war_readiness_check` pays it only when it sees a zero.
        """
        import divenum
        c = self._c(tag)
        me = c.tag
        d = self.divisions(me, limit=1, deep=deep)
        try:
            armies = self.order_groups(me)
        except Exception as e:
            armies = {"count": 0, "groups": [], "error": repr(e)}
        try:
            unassigned = len(self.unassigned_divisions(me))
        except Exception as e:
            unassigned = None
            d.setdefault("warning", "unassigned_divisions failed: %r" % (e,))
        try:
            conveyors = self.conveyors(me)
        except Exception:
            conveyors = []
        res = {
            "tag": me,
            "armies": armies,
            "divisions_count": d["count"],
            "unassigned_divisions": unassigned,
            "conveyors": conveyors,
            "source": d.get("source"),
            "countable": bool(d.get("verified")),
            "country_ptr": d.get("country_ptr"),
            "tag_id": d.get("tag_id"),
            "selftest": divenum.selftest(self, me, deep=deep),
        }
        if d.get("warning"):
            res["warning"] = d["warning"]
        return res

    def move_divisions(self, divisions=None, province=None, orders_group=None, tag=None,
                       mode="move", wait=12.0, limit=60, force=False,
                       spread=False, max_per_province=None):
        import movemicro
        return movemicro.move_divisions(self, divisions, province, orders_group, tag,
                                        mode=mode, wait=wait, limit=limit, force=force,
                                        spread=spread, max_per_province=max_per_province)

    def deploy_to_border(self, target_tag, orders_group=None, include_unassigned=True, tag=None,
                         protect_borders=None, min_other_garrison=0):
        import movemicro
        return movemicro.deploy_to_border(
            self, target_tag, orders_group, include_unassigned, tag,
            protect_borders=protect_borders, min_other_garrison=min_other_garrison,
        )

    def attack_country(self, target_tag, mode="balanced", max_attacks=2, tag=None):
        import movemicro
        return movemicro.attack_country(self, target_tag, mode, max_attacks, tag)

    def plug_border_gaps(self, target_tag, min_garrison=1, max_gaps=None, dry_run=False):
        import movemicro
        return movemicro.plug_border_gaps(self, target_tag, min_garrison,
                                         max_gaps, dry_run)

    def wartime_status(self):
        import movemicro
        return movemicro.wartime_status(self)

    def wartime_step(self, target_tag=None, max_attacks=2, dry_run=False):
        import movemicro
        return movemicro.wartime_step(self, target_tag, max_attacks, dry_run)

    def set_front_directive(self, enemy_tag, stance="balanced_attack",
                            priority_provinces=None, priority_states=None,
                            min_garrison=1, deploy=None, tag=None, battleplan=True,
                            armies=None):
        # 2026-11-17: the wrapper forgot `battleplan`, but mcp/server.py passes it
        # unconditionally, so every hoi4_set_front_directive call died with
        # "MoveMixin.set_front_directive() got an unexpected keyword argument
        # 'battleplan'" -- i.e. the whole land-war entry point was dead on this
        # fork while movemicro itself supported the parameter all along.
        import movemicro
        return movemicro.set_front_directive(self, enemy_tag, stance,
                                             priority_provinces, priority_states,
                                             min_garrison, deploy, tag, battleplan,
                                             armies=armies)

    def front_directives(self):
        import movemicro
        return movemicro.front_directives(self)

    def clear_front_directive(self, enemy_tag=None):
        import movemicro
        return movemicro.clear_front_directive(enemy_tag)

    def tactical_front_tick(self, enemy_tag=None, force=False, dry_run=False):
        import movemicro
        return movemicro.tactical_front_tick(self, enemy_tag, force, dry_run)
