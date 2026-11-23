"""Route A: per-division province move / attack (the player's RIGHT CLICK).

This replaces the abandoned `COrderNewRootCommand` front-line path. The command is
`CMoveCommand`, whose payload is an embedded `CUnitMoveAction`:

    CMoveCommand (1.19.4.1 vtable 0x4475CB8, size 0x88)
      +0x28  CUnitMoveAction (vtable 0x44759B8, 0x60 bytes)
               +0x08  0x3648 = unit_move_action token
               +0x0C  the division's handle (CArmy+0x18)
               +0x18  CPdxArray<i32>: target province path (first element = target)
               +0x50..0x5C  order flags
    ctor 0x253A600(this, CArmy*, i32 province, u8, i32, u8, u8, u8, u8, u8)

The real UI call site (0x7B00F1) allocates 0x88 bytes, uses flags
(1, 2, 0, 0, 0, 0, 0) and then calls CCommand::Run (0x20AE0A0); a second call
site (0x24CE3C3) uses (1, 1, 0, 0, 0, 0, al). Both are accepted by the game.

Wartime layer (fourth stage):
  * plug_border_gaps   - find empty border provinces, send 1-2 reserve divisions
  * reserve_garrison   - never strip the last (or second-last) division from a border
  * concentrated_attack- mass 6-9 divisions on 1-2 soft provinces, return honest counts
  * wartime_step       - housekeep -> plug gaps -> concentrated attack, one tick
"""
from __future__ import annotations
import struct
import time
from collections import Counter, deque


def _reset_errbuf(p, errbuf: int):
    """Make `errbuf` a VALID empty libstdc++ std::string.

    Layout: +0 _M_p (must point at the object's own SSO buffer), +8 length,
    +16..+31 the 16-byte local buffer. The old code zeroed all 32 bytes, so
    _M_p was NULL; when CMoveCommand::CanExecute refused an order, CCommand::Run
    wrote the refusal text through that NULL -> wild write, heap corruption and
    the 1940.08.09 SIGSEGV. Fixed once for the whole file.
    """
    import sys
    if sys.platform == "win32":
        return
    if errbuf:
        p.write(errbuf, struct.pack("<QQ", errbuf + 0x10, 0) + b"\0" * 16)


def _alloc_errbuf(g) -> int:
    """A FRESH SSO std::string for one command batch — never a shared buffer.

    2026-11-05/06: this returned ONE buffer per process and every command overwrote
    it. When CCommand::Run wrote a refusal message longer than 15 characters the
    libstdc++ string moved to the heap; the next `_reset_errbuf` then pointed _M_p
    back at the inline buffer and orphaned that block. Two runs died with
    `free(): invalid pointer` + `*** stack smashing detected ***` under 6-10 orders
    per tick. A 32-byte allocation per batch is free by comparison.
    """
    import sys
    if sys.platform == "win32":
        # Windows g.cmd.post() uses cmdwin.enqueue() directly and never touches
        # a libstdc++ errbuf; returning 0 avoids leaking 32B per move batch.
        return 0
    p = g.p
    buf = g.malloc(0x20)
    _reset_errbuf(p, buf)
    return buf


FLAG_SETS = {
    "move": (1, 2, 0, 0, 0, 0, 0),      # UI right-click call site 0x7B00F1
    "attack": (1, 1, 0, 0, 0, 0, 1),    # second game call site 0x24CE3C3
}

A_ORG, A_STR, A_OOS, A_TMPL = 0x428, 0x420, 0x498, 0x3B8
DEFAULT_GARRISON = 1


def _fx(p, a):
    return p.i64(a) / 1e5


def _allowed_tags(g):
    """Country tags a land path may cross: us plus our faction members."""
    tags = set()
    try:
        tags.add(g.player().tag)
    except Exception:
        pass
    try:
        for t in (g.faction_members() or {}).get("members") or []:
            if isinstance(t, str) and len(t) == 3:
                tags.add(t)
    except Exception:
        pass
    return tags


def _land_path(g, src, dst, owners=None, allowed=None):
    """Shortest LAND path src -> dst, excluding src. None if no legal path.

    Sea/lake provinces are dropped. Intermediate provinces must be owned by us
    or a faction member; the destination itself may be enemy (an attack).
    """
    import mapdata
    adj = mapdata.build_adjacency()
    _, ptype = mapdata.load_definitions()
    if owners is None:
        owners, _ = mapdata.province_owner_map(g)
    if allowed is None:
        allowed = _allowed_tags(g)
    q = deque([src])
    prev = {src: None}
    while q:
        a = q.popleft()
        if a == dst:
            break
        for b in adj.get(a, ()):
            if b in prev or ptype.get(b) != "land":
                continue
            if b != dst and owners.get(b) not in allowed:
                continue
            prev[b] = a
            q.append(b)
    if dst not in prev:
        return None
    out = []
    x = dst
    while x != src:
        out.append(x)
        x = prev[x]
    out.reverse()
    return out


def _resolve(g, divisions=None, orders_group=None, tag=None):
    import armyops
    if divisions:
        return [int(x, 16) if isinstance(x, str) else int(x) for x in divisions]
    if orders_group:
        return list(armyops.group_divisions(g, orders_group))
    from game import CO_ARMIES, _arr
    c = g._c(tag)
    return list(_arr(g.p, c.ptr, CO_ARMIES))


def _plan_spread_targets(g, center_prov, divs, tag=None, max_per_province=None,
                         force=False, max_hops=6):
    """以 center_prov 为中心在己方/盟友陆地省上 BFS 扩散，将师按每省上限均衡摊开（P2-2）。

    返回 `(target_by_div, spread_plan, cap, err_dict)`。
    """
    import mapdata
    import unitorders as _uo
    from game import _arr as _arr2

    cap_raw = int(max_per_province) if max_per_province is not None else 8
    cap = max(1, cap_raw if force else min(12, cap_raw))

    owners, _ = mapdata.province_owner_map(g)
    adj = mapdata.build_adjacency()
    _, ptype = mapdata.load_definitions()
    allowed = _allowed_tags(g)

    # 统计当前己方各省驻军数
    occ = Counter()
    try:
        co = g._c(tag).ptr if tag else g.player().ptr
        for _d in _arr2(g.p, co, 0x290):
            pv = _uo.province_of(g, _d)
            if pv is not None:
                occ[int(pv)] += 1
    except Exception:
        pass

    div_cur = {}
    for d in divs:
        pv = _uo.province_of(g, d)
        div_cur[d] = int(pv) if pv is not None else None

    # 中心省必须是陆地省（若为己方/盟友可向外 BFS；否则仅退回单省）
    cand_order = []
    if ptype.get(center_prov, "land") == "land" and (not owners or owners.get(center_prov) in allowed):
        q = deque([(center_prov, 0)])
        seen = {center_prov: 0}
        while q:
            u, dist = q.popleft()
            cand_order.append((dist, u))
            if dist >= max_hops:
                continue
            for nb in adj.get(u, ()):
                if nb in seen:
                    continue
                if ptype and ptype.get(nb) != "land":
                    continue
                if owners and owners.get(nb) not in allowed:
                    continue
                seen[nb] = dist + 1
                q.append((nb, dist + 1))
    else:
        cand_order.append((0, center_prov))

    # 距离优先，同距离按当前驻军少优先
    cand_order.sort(key=lambda x: (x[0], occ.get(x[1], 0), x[1]))
    cand_provs = [pv for _, pv in cand_order]
    cand_set = set(cand_provs)

    # 先扣掉本批即将移走的师（后续逐个落位时再加回目标省），保证容量核算精准
    sim_occ = Counter(occ)
    target_by_div = {}
    spread_plan = Counter()
    unassigned = []
    for d in divs:
        cur = div_cur.get(d)
        # 已在候选区内且当前省未超上限的师原地不动
        if cur is not None and cur in cand_set and sim_occ[cur] <= cap:
            target_by_div[d] = cur
            spread_plan[cur] += 1
        else:
            if cur is not None and sim_occ[cur] > 0:
                sim_occ[cur] -= 1
            unassigned.append(d)

    for d in unassigned:
        picked = None
        for pv in cand_provs:
            if sim_occ[pv] < cap:
                picked = pv
                break
        if picked is None:
            if not force:
                return None, None, cap, {
                    "ok": False,
                    "blocked_by": "spread_capacity_exhausted",
                    "province": center_prov,
                    "max_per_province": cap,
                    "candidate_provinces": len(cand_provs),
                    "divisions": len(divs),
                    "error": (
                        "could not spread %d divisions within %d hops of province %d at "
                        "max_per_province=%d (%d friendly land provinces checked); raise "
                        "max_per_province or pass force=true"
                        % (len(divs), max_hops, center_prov, cap, len(cand_provs))
                    ),
                }
            picked = min(cand_provs, key=lambda pv: (sim_occ[pv], pv))
        sim_occ[picked] += 1
        target_by_div[d] = picked
        spread_plan[picked] += 1

    plan_dict = {str(pv): int(cnt) for pv, cnt in sorted(spread_plan.items())}
    return target_by_div, plan_dict, cap, None


def move_divisions(g, divisions=None, province=None, orders_group=None, tag=None,
                   mode="move", wait=12.0, limit=60, post=True, force=False,
                   spread=False, max_per_province=None):
    """Order divisions to move to (or attack) one province, or spread across nearby provinces."""
    import unitorders
    p = g.p
    if province is None:
        return {"ok": False, "error": "province is required"}
    province = int(province)
    flags = FLAG_SETS.get(mode, FLAG_SETS["move"])
    divs = _resolve(g, divisions, orders_group, tag)[:limit]
    if divisions is None and orders_group is None:
        # 2026-11-10: only the "move everything" form is filtered. An explicit
        # list (paradrop staging, manual override) is always obeyed verbatim.
        import divreserve as _dr
        divs, _para = _dr.filter_out(g, divs)
    if not divs:
        return {"ok": False, "error": "no divisions to order"}

    target_by_div = None
    spread_plan = None
    spread_cap = None
    if spread:
        target_by_div, spread_plan, spread_cap, spread_err = _plan_spread_targets(
            g, province, divs, tag=tag, max_per_province=max_per_province, force=force
        )
        if spread_err is not None:
            return spread_err
    # 2026-11-11: manual "send everything to province X" is exactly how round 3 ended
    # up with 103 divisions in one province: supply 2.9%, 0 attacks, army destroyed.
    # The engine's own front orders spread units; a manual dump does not.
    elif not force:
        try:
            from game import _arr as _arr2
            import unitorders as _uo2
            co = g._c(tag).ptr if tag else g.player().ptr
            here = 0
            for _d in _arr2(g.p, co, 0x290):
                if _uo2.province_of(g, _d) == province:
                    here += 1
            incoming = 0
            for _d in divs:
                if _uo2.province_of(g, _d) != province:
                    incoming += 1
        except Exception:
            here, incoming = 0, 0
        if incoming and (here + incoming) > 12:
            return {"ok": False, "blocked_by": "stacking", "province": province,
                    "already_there": here, "incoming": incoming,
                    "error": ("province %d already holds %d of our divisions; %d more would "
                              "collapse supply (round 3: 103 divisions -> 2.9%% supply, zero "
                              "attacks). Pass spread=true (with optional max_per_province=6..8) "
                              "to auto-distribute across neighbouring provinces, use "
                              "hoi4_army_battleplans / hoi4_deploy_to_border, or pass force=true."
                              % (province, here, incoming))}

    own = g._inj is None
    if own:
        g.attach()
    rows = []
    owners = None
    allowed = None
    try:
        errbuf = _alloc_errbuf(g)
        _reset_errbuf(p, errbuf)
        for d in divs:
            dst = int(target_by_div[d]) if target_by_div is not None else province
            before = unitorders.province_of(g, d)
            row = {"division": hex(d), "from": before, "target": dst}
            if before is None:
                row.update(ok=False, skipped=True,
                           error="stale division pointer (re-query hoi4_divisions)")
                rows.append(row)
                continue
            if before == dst:
                row.update(ok=True, note="already in the target province")
                rows.append(row)
                continue
            planned = [dst]
            if before is not None:
                import mapdata
                if dst not in mapdata.build_adjacency().get(before, ()):
                    if owners is None:
                        owners, _ = mapdata.province_owner_map(g)
                        allowed = _allowed_tags(g)
                    planned = _land_path(g, before, dst, owners, allowed)
                    if not planned:
                        row.update(ok=False,
                                   error="no legal land path from %s to %s" % (before, dst))
                        rows.append(row)
                        continue
            cmd, err = unitorders.build_move(g, d, dst, flags, path=planned)
            if err:
                row.update(ok=False, error=err)
                rows.append(row)
                continue
            if post:
                # CCommand::Run: CanExecute -> Post(queue, cmd, 1). Same as a click.
                _reset_errbuf(p, errbuf)
                rv = g.cmd.post(cmd)   # Run: CanExecute guards against malformed CMoveCommand
                if not (rv & 0xFF):
                    # Fallback B: long path refused -> advance one hop, honestly reported.
                    if len(planned) > 1:
                        cmd2, err2 = unitorders.build_move(g, d, planned[0], flags)
                        if not err2:
                            _reset_errbuf(p, errbuf)
                            if g.cmd.post(cmd2) & 0xFF:
                                row.update(ok=True, command=hex(cmd2), sent=True,
                                           moved_to_hop=planned[0],
                                           final_target=dst,
                                           remaining_hops=len(planned) - 1,
                                           fallback=True)
                                rows.append(row)
                                continue
                    row.update(ok=False, command=hex(cmd), sent=False,
                               error="CanExecute refused (Run returned false)")
                    rows.append(row)
                    continue
            row.update(ok=True, command=hex(cmd), sent=bool(post))
            if len(planned) > 1:
                row["planned_path"] = planned
                row["hops"] = len(planned)
            rows.append(row)
        if post:
            # B1 (review 2026-11-07): while we hold the ptrace stop every game
            # thread is frozen, so the command queue can never be consumed and
            # this poll could only ever time out (12 s of a dead game). Release
            # the stop FIRST, then wait for the path array to fill in.
            if own:
                g.detach()
                own = False
            deadline = time.time() + max(0.0, float(wait or 0))
            while True:
                done = True
                for row in rows:
                    if not row.get("sent"):
                        continue
                    d = int(row["division"], 16)
                    row_dst = int(row.get("target") or province)
                    row["province_now"] = unitorders.province_of(g, d)
                    row["path"] = unitorders.movement(g, d)
                    row["moving"] = bool(row["path"]) or row.get("province_now") == row_dst
                    if not row["moving"]:
                        done = False
                if done or time.time() >= deadline:
                    break
                time.sleep(0.5)
    finally:
        if own:
            g.detach()

    sent = [r for r in rows if r.get("sent")]
    moving = sum(1 for r in sent if r.get("moving"))
    out = {
        "ok": all(r.get("ok") for r in rows),
        "province": province,
        "mode": mode,
        "flags": list(flags),
        "spread": bool(spread),
        "ordered": len(rows),
        "accepted": len(sent),
        "moving": moving,
        "divisions": rows,
    }
    if spread:
        out["max_per_province"] = spread_cap
        out["spread_plan"] = spread_plan
    if sent and not moving:
        out["pending"] = True
        out["note"] = ("Run accepted every command. The movement path/province updates on the "
                       "next queue tick, which can take 10-30 s at this simulation speed; "
                       "re-read hoi4_division_objects/hoi4_move_divisions later. If the game is "
                       "PAUSED nothing moves.")
    return out


def _border_provinces(g, target_tag):
    import mapdata
    target_tag = str(target_tag).upper()
    owners, _ = mapdata.province_owner_map(g)
    adj = mapdata.build_adjacency()
    _, ptype = mapdata.load_definitions()
    own = {g.player().tag}      # run 8: NEVER count an ally's border as ours
    out = []
    for pid, tag in owners.items():
        if tag not in own or ptype.get(pid) != "land":
            continue
        for q in adj.get(pid, ()):
            if owners.get(q) == target_tag and ptype.get(q) == "land":
                out.append(pid)
                break
    return sorted(out)


def _nearest_border_path(g, src, targets, owners=None, allowed=None):
    """Shortest legal land path from src to ANY province in `targets`."""
    import mapdata
    adj = mapdata.build_adjacency()
    _, ptype = mapdata.load_definitions()
    if owners is None:
        owners, _ = mapdata.province_owner_map(g)
    if allowed is None:
        allowed = _allowed_tags(g)
    target_set = set(targets)
    q = deque([src])
    prev = {src: None}
    hit = None
    while q:
        a = q.popleft()
        if a in target_set:
            hit = a
            break
        for b in adj.get(a, ()):
            if b in prev or ptype.get(b) != "land":
                continue
            if b not in target_set and owners.get(b) not in allowed:
                continue
            prev[b] = a
            q.append(b)
    if hit is None:
        return None
    out = []
    x = hit
    while x != src:
        out.append(x)
        x = prev[x]
    out.reverse()
    return out


def _run_move(g, d, province, flags, errbuf):
    """Build + RUN one command; returns (ok, cmd, path, error, fallback)."""
    import unitorders
    before = unitorders.province_of(g, d)
    planned = [province]
    if before is not None:
        import mapdata
        if province not in mapdata.build_adjacency().get(before, ()):
            owners, _ = mapdata.province_owner_map(g)
            planned = _land_path(g, before, province, owners, _allowed_tags(g))
            if not planned:
                return False, None, None, "no legal land path %s -> %s" % (before, province), None
    cmd, err = unitorders.build_move(g, d, province, flags, path=planned)
    if err:
        return False, None, planned, err, None
    _reset_errbuf(g.p, errbuf)
    if g.cmd.post(cmd) & 0xFF:
        return True, cmd, planned, None, None
    # Run 8: the engine refused the whole-way order. Fall back to one hop with a
    # FRESH error buffer (a shared one was the run-4/5 heap corruption).
    if len(planned) > 1:
        try:
            errbuf2 = _alloc_errbuf(g)
            _reset_errbuf(g.p, errbuf2)
            cmd2, err2 = unitorders.build_move(g, d, province, flags,
                                               path=planned, use_first_hop=True)
            if not err2 and cmd2 and g.cmd.post(cmd2) & 0xFF:
                return True, cmd2, planned, None, "first_hop"
        except Exception:
            pass
    # NOTE: do NOT free `cmd` here. The 11-05 work order suggested it, but
    # CCommand::Run can take ownership of the command even when it reports false
    # (it may have queued it, or the queue may delete it on the next tick) and
    # freeing it produced `free(): double free detected in tcache 2` -> SIGABRT
    # with a minidump at 1939.09 (run 3). A 0x88-byte leak per refused order is
    # strictly better than a crash.
    #
    # 2026-11-06: the old `cmd2` retry is gone. `build_move` puts `path[0]` in the
    # command (unitorders.build_move docstring), so building a second command for
    # `planned[0]` reproduces EXACTLY the same order — it can only be refused the
    # same way, and it doubles the number of orphaned command objects per tick.
    return False, None, planned, "CanExecute refused", None


# ============================================================ state scanning
def _scan_country_locked(g, tag):
    """Scan one country's divisions. Caller must already hold an attach session."""
    import unitorders
    from game import CO_ARMIES, _arr
    p = g.p
    c = g._c(tag)
    out = []
    for d in _arr(p, c.ptr, CO_ARMIES):
        try:
            path = unitorders.movement(g, d)
            out.append({
                "ptr": d,
                "prov": unitorders.province_of(g, d),
                "path": path,
                "moving": bool(path),
                "org": round(_fx(p, d + A_ORG), 2),
                "str": round(_fx(p, d + A_STR), 3),
                "oos": p.i32(d + A_OOS),
                "template": p.u64(d + A_TMPL),
                "str_damage": round(_fx(p, d + 0x448), 3),
                "org_damage": round(_fx(p, d + 0x450), 3),
            })
        except Exception:
            continue
    # Strength and organisation are ABSOLUTE values here (HOI4 stores them x1e5:
    # a full Infanterie-Division reads str≈227 and org≈53). The old code divided by
    # the best division of the SAME template in the CURRENT snapshot, so an army
    # that had decayed to 20 % strength still reported str_pct=1.0 and kept
    # attacking until the divisions were wiped out (30 -> 21 divisions in 1936).
    # The ceiling is now a per-template high-water mark that survives across ticks,
    # seeded by the first full-strength division the game ever shows us.
    peak_org, peak_str = {}, {}
    for r in out:
        t = r["template"]
        key = (p.pid, t)
        if r["org"] > _PEAK_ORG.get(key, 0.0):
            _PEAK_ORG[key] = r["org"]
        if r["str"] > _PEAK_STR.get(key, 0.0):
            _PEAK_STR[key] = r["str"]
        peak_org[t] = max(peak_org.get(t, 0.0), _PEAK_ORG[key])
        peak_str[t] = max(peak_str.get(t, 0.0), _PEAK_STR[key])
    # 2026-11-10: paratroopers are idle by design and must never be treated as
    # reserve by any land law.  The flag travels with the row so _front_state can
    # drop them before a single law sees them.
    import divreserve as _dr
    for r in out:
        try:
            _res, _why = _dr.is_paradrop_division(g, r["ptr"])
        except Exception:
            _res, _why = False, None
        r["reserved"] = bool(_res)
        r["reserved_reason"] = _why
    for r in out:
        t = r["template"]
        r["org_pct"] = round(min(1.0, r["org"] / peak_org[t]), 3) if peak_org.get(t) else 0.0
        r["str_pct"] = round(min(1.0, r["str"] / peak_str[t]), 3) if peak_str.get(t) else 0.0
    return out


def _front_state(g, target_tag):
    """One attach session: map geometry + our divisions + enemy divisions.

    2026-11-03: uses the LIVE province controller map, not state ownership.
    `st["owners"]` below is the controller map (kept under the old key so every
    law reads the right thing); `st["state_owners"]` keeps the ownership map for
    reference. The enemy path[-1] set is `threatened`, NOT part of the border.
    """
    import mapdata
    target_tag = str(target_tag).upper()
    state_owners, _ = mapdata.province_owner_map(g)
    adj = mapdata.build_adjacency()
    _, ptype = mapdata.load_definitions()
    terrain = mapdata.load_terrain()
    own = _allowed_tags(g)
    own_attach = g._inj is None
    if own_attach:
        g.attach()
    try:
        ours = _scan_country_locked(g, None)
        theirs = _scan_country_locked(g, target_tag)
    finally:
        if own_attach:
            g.detach()
    # 2026-11-10: the paradrop army is not part of the land front.  Removing the
    # rows here (and not in each law) is the only way to cover all eight laws at
    # once: they all read ours / ours_by / staying / incoming.
    reserved_rows = [r for r in ours if r.get("reserved")]
    ours = [r for r in ours if not r.get("reserved")]
    ours_by, theirs_by = {}, {}
    for r in ours:
        if r["prov"] is not None:
            ours_by.setdefault(r["prov"], []).append(r)
    for r in theirs:
        if r["prov"] is not None:
            theirs_by.setdefault(r["prov"], []).append(r)
    try:
        player_tag = g.player_tag()
    except Exception:
        player_tag = next(iter(own), "ENG")
    tagged = ([dict(r, tag=player_tag) for r in ours]
              + [dict(r, tag=target_tag) for r in theirs])
    ctrl = mapdata.province_controller_map(g, divisions=tagged)
    enemy_provs = {p for p, t in ctrl.items()
                   if t == target_tag and ptype.get(p) == "land"}
    border = []
    for p, t in ctrl.items():
        if ptype.get(p) != "land":
            continue
        # Run 8: only provinces THIS country controls (or already garrisons) may
        # form our front. `own` includes every faction member, so Italy's Alps,
        # Libya and Ethiopia were counted as German FRA border — the western
        # front inflated from 8 to 43 provinces and 43 divisions parked there.
        if t != player_tag and p not in ours_by:
            continue
        if any(ctrl.get(q) == target_tag and ptype.get(q) == "land"
               for q in adj.get(p, ())):
            border.append(p)
    # Our divisions standing in a province the area map still calls enemy-held:
    # treat it as ours (occupation in progress).
    for p in list(ours_by):
        if p in border or ptype.get(p) != "land":
            continue
        if any(ctrl.get(q) == target_tag or q in theirs_by
               for q in adj.get(p, ())):
            border.append(p)
            ctrl[p] = player_tag
    border = sorted(set(border))
    # Home (land-connected) divisions: the only ones that can garrison or attack.
    # Overseas garrisons are not part of the front economy.
    home = set(border)
    dq = deque(border)
    while dq:
        a = dq.popleft()
        for b in adj.get(a, ()):
            if b in home or ptype.get(b) != "land":
                continue
            if ctrl.get(b) not in own:
                continue
            home.add(b)
            dq.append(b)
    home_count = sum(1 for r in ours if r["prov"] in home)
    # --- second-line strategic reserve belt (run 7) -------------------------
    # Run 6 lost Berlin because every division was pushed onto the first line:
    # one Polish breakthrough rolled through 22 empty provinces. The belt keeps
    # 2 divisions in each of up to 7 hubs one hop behind the border, and the
    # capital is always one of them.
    border_set = set(border)
    second_line = set()
    for bp in border:
        for nb in adj.get(bp, ()):
            if nb not in border_set and nb in home and ptype.get(nb) == "land":
                second_line.add(nb)
    vp_set = _vp_provinces()
    two_hop_vps = set()
    for sl in sorted(second_line):
        if sl in vp_set:
            two_hop_vps.add(sl)
        for nb2 in adj.get(sl, ()):
            if (nb2 not in border_set and nb2 in home
                    and ptype.get(nb2) == "land" and nb2 in vp_set):
                two_hop_vps.add(nb2)
    capital_prov = _capital_province(g)
    if capital_prov and capital_prov in home and capital_prov not in border_set:
        two_hop_vps.add(capital_prov)
    reserve_hubs = []
    for cand_p in sorted(second_line | two_hop_vps,
                         key=lambda p: (0 if p in two_hop_vps else 1,
                                        -sum(1 for nb in adj.get(p, ())
                                             if nb in border_set), p)):
        if len(reserve_hubs) >= 7:
            break
        if cand_p not in two_hop_vps and any(cand_p in adj.get(h, ())
                                             for h in reserve_hubs):
            # Keep hubs a province apart so the belt covers the whole line.
            continue
        reserve_hubs.append(cand_p)
    # 2 divisions per hub = 12-14 divisions of operational reserve.
    reserve_req = {h: 2 for h in reserve_hubs}
    # Where enemy divisions are marching: threat intel only, never the front line.
    threatened = set()
    for r in theirs:
        path = r.get("path") or []
        if path and ptype.get(path[-1]) == "land":
            threatened.add(path[-1])
    # --- sister-front protection zone (run 12) ------------------------------
    # The 1939-1940 lesson: every tick that the east needed reserves, the west
    # was treated as "idle rear" and stripped to one division per tile; France
    # then walked in (run 11: 6 German provinces lost, FRA front 8 -> 28). A
    # configured sister front is now untouchable, including the belt one hop
    # behind it, no matter whether its stance is hold or attack.
    other_front = set()
    other_front_zone = set()
    try:
        _dirs = (_load_front_state() or {}).get("directives") or {}
        for _ot in _dirs:
            if str(_ot).upper() == target_tag:
                continue
            _ob = set(_border_provinces(g, _ot))
            if not _ob:
                continue
            other_front |= _ob
            other_front_zone |= _ob
            for _op in _ob:
                for _nb in adj.get(_op, ()):
                    if (_nb in home and ptype.get(_nb) == "land"
                            and _nb not in border_set):
                        other_front_zone.add(_nb)
    except Exception:
        other_front, other_front_zone = set(), set()
    return {
        "target_tag": target_tag,
        "owners": ctrl, "state_owners": state_owners,
        "adj": adj, "ptype": ptype, "terrain": terrain,
        "own_tags": own, "enemy_provs": enemy_provs, "border": border,
        "other_front": other_front, "other_front_zone": other_front_zone,
        "threatened": threatened,
        "home_provs": home, "home_count": home_count,
        "second_line": second_line, "reserve_hubs": reserve_hubs,
        "reserve_req": reserve_req, "capital_prov": capital_prov,
        "ours": ours, "theirs": theirs,
        "reserved": reserved_rows, "reserved_count": len(reserved_rows),
        "ours_by": ours_by, "theirs_by": theirs_by,
        "staying": Counter(r["prov"] for r in ours if not r["moving"]),
        "incoming": Counter(r["path"][-1] for r in ours if r["path"]),
        # Run 11 (work order 1107): a division ordered to attack the neighbouring
        # province q keeps `prov == p` for the 2-4 days the combat lasts, but its
        # path is already [.., q], so `staying` dropped it and `incoming` counted it
        # at q. `_plug_state` therefore saw p empty and stripped its neighbours to
        # "plug the gap" that was never there — every single attack ate 2 more
        # divisions. These two counters make the attackers visible again.
        "attacking_from": Counter(
            r["prov"] for r in ours
            if r["moving"] and r["prov"] is not None
            and (r.get("path") or [None])[-1] in enemy_provs
        ),
        "attacking_into": Counter(
            r["path"][-1] for r in ours
            if r["moving"] and r.get("path") and r["path"][-1] in enemy_provs
        ),
        "enemy_neighbors": {
            p: [q for q in adj.get(p, ()) if ctrl.get(q) == target_tag
                and ptype.get(q) == "land"]
            for p in border
        },
    }


_CAPITAL_CACHE = {}


def _capital_province(g):
    """Province of the capital state.

    `CCountry+0xFF0` is a STATE id, so the province has to be looked up in the
    state table; the VP province of that state is the capital itself (Berlin).
    Cached per game pid because `_homeland_state_id` walks the whole state list.
    """
    try:
        pid = g.p.pid
    except Exception:
        return None
    if pid in _CAPITAL_CACHE:
        return _CAPITAL_CACHE[pid] or None
    out = None
    try:
        import mapdata
        sid = int(g._homeland_state_id())
        st2p, _ = mapdata.load_state_provinces()
        provs = [p for p in (st2p.get(sid) or ()) if p]
        vps = _vp_provinces()
        best = [p for p in provs if p in vps] or provs
        if best:
            out = sorted(best)[0]
    except Exception:
        out = None
    _CAPITAL_CACHE[pid] = out or 0
    return out


def _is_front_src(st, p):
    """A source province that touches enemy-owned OR enemy-held ground."""
    if p in st["border"]:
        return True
    return any(st["owners"].get(q) == st["target_tag"] or st["theirs_by"].get(q)
               for q in st["adj"].get(p, ()))


def _own_or_held(st, p):
    """Owned by us/faction OR occupied by one of our divisions."""
    return st["owners"].get(p) in st["own_tags"] or bool(st["ours_by"].get(p))


def _terrain_name(st, q):
    t = st["terrain"].get(q)
    if isinstance(t, str):
        return t
    if isinstance(t, dict):
        return t.get("name") or t.get("type")
    return None


def _enemy_state(st, q):
    rows = st["theirs_by"].get(q, [])
    org = sum(r["org"] for r in rows)
    return {
        "count": len(rows),
        "avg_org": round(org / len(rows), 1) if rows else 0.0,
        "avg_org_pct": round(sum(r["org_pct"] for r in rows) / len(rows), 3) if rows else 0.0,
        "oos": max((int(r["oos"]) for r in rows), default=0),
        "supplied": sum(1 for r in rows if r["oos"] < 5),
    }


# ============================================================ gap plugging
def _pick_gap_divisions(g, st, ep, min_garrison=DEFAULT_GARRISON):
    """Choose 1-2 divisions for an empty border province, without stripping a garrison."""
    adj, owners = st["adj"], st["owners"]
    border = set(st["border"])
    empty = {p for p in border if st["staying"][p] + st["incoming"][p] == 0}
    candidates = []
    # 1) adjacent provinces first: fastest and keeps the line connected
    for src in adj.get(ep, ()):
        if not _own_or_held(st, src):
            continue
        for r in st["ours_by"].get(src, []):
            if r["moving"]:
                continue
            # Run 14: an over-stacked province (Berlin: 96 divisions) is out of
            # supply; its divisions must be allowed to walk out even at 15 % org.
            if st["staying"].get(src, 0) <= 4 and (r["oos"] >= 5 or r["org_pct"] < 0.35):
                continue
            # Do not pull the last garrison out of another front province.
            if _is_front_src(st, src):
                stay = st["staying"][src]
                keep = min_garrison
                if stay - 1 < keep:
                    continue
            if src in empty and st["staying"][src] <= 1:
                continue
            score = 100.0 + r["org_pct"] * 50.0 + min(st["staying"][src], 4) * 10.0
            candidates.append((score, src, r))
    # 2) depth reserve: any non-border province with a REAL land path to the gap
    if not candidates:
        for src, rows in st["ours_by"].items():
            if src in border:
                continue
            for r in rows:
                if r["moving"]:
                    continue
                # Run 14: depth reserve is always movable, whatever its org/oos.
                if _land_path(g, src, ep, owners, st["own_tags"]) is None:
                    continue
                score = 60.0 + r["org_pct"] * 50.0
                candidates.append((score, src, r))
    candidates.sort(key=lambda x: -x[0])
    return candidates


def plug_border_gaps(g, target_tag, min_garrison=DEFAULT_GARRISON, max_gaps=None,
                     dry_run=False):
    """Find border provinces with no defender and no one moving in, then plug them.

    `min_garrison` is the number of divisions that must stay in a source border
    province when it still faces another enemy province.
    """
    import unitorders  # noqa: F401  (kept for the same attach contract as _run_move)
    st = _front_state(g, target_tag)
    border = st["border"]
    empty = [p for p in border
             if st["staying"][p] + st["incoming"][p] == 0]
    if max_gaps:
        empty = empty[:int(max_gaps)]
    if not empty:
        return {"ok": True, "target_tag": st["target_tag"],
                "border_province_count": len(border),
                "empty_border_provinces": [], "plugged_gaps": 0,
                "gaps": [], "note": "no empty border province"}
    rows, used = [], set()
    own_attach = g._inj is None
    if own_attach:
        g.attach()
    try:
        errbuf = _alloc_errbuf(g)
        _reset_errbuf(g.p, errbuf)
        for ep in empty:
            picks = [(s, src, r) for s, src, r in _pick_gap_divisions(g, st, ep, min_garrison)
                     if r["ptr"] not in used]
            # Prefer two when the province is wide and we have spare divisions.
            want = 2 if len(picks) >= 4 else 1
            chosen = picks[:want]
            if not chosen:
                rows.append({"province": ep, "ok": False,
                             "error": "no reserve division available"})
                continue
            for _, src, r in chosen:
                used.add(r["ptr"])
                if dry_run:
                    rows.append({"province": ep, "from": src, "division": hex(r["ptr"]),
                                 "ok": True, "dry_run": True})
                    st["incoming"][ep] += 1
                    st["staying"][src] -= 1
                    continue
                ok, cmd, planned, err, fb = _run_move(
                    g, r["ptr"], ep, FLAG_SETS["move"], errbuf)
                rows.append({"province": ep, "from": src, "division": hex(r["ptr"]),
                             "ok": ok, "command": hex(cmd) if cmd else None,
                             "planned_path": planned, "error": err, "fallback": fb})
                if ok:
                    st["incoming"][ep] += 1
                    st["staying"][src] -= 1
    finally:
        if own_attach:
            g.detach()
    plugged = len({r["province"] for r in rows if r.get("ok")})
    still = [p for p in empty
             if st["staying"][p] + st["incoming"][p] == 0]
    return {"ok": plugged > 0 and not still,
            "target_tag": st["target_tag"],
            "border_province_count": len(border),
            "empty_border_provinces": empty,
            "plugged_gaps": plugged,
            "still_empty": still,
            "gaps": rows,
            "dry_run": bool(dry_run)}


# ============================================================ concentrated attack
# A division that attacks below these absolute floors loses its whole establishment
# in a few days; a human clicks "stop" long before. str/org are the raw game units.
MIN_ATTACK_STR_PCT = 0.65
MIN_ATTACK_ORG = 32.0
MIN_ATTACK_ORG_PCT = 0.60
MOVE_ORG_PCT = 0.55
MOVE_STR_PCT = 0.65
HALT_STR_PCT = 0.55
HALT_ORG = 25.0
_PEAK_ORG = {}
_PEAK_STR = {}


def _halt_dying_attacks(g, st, max_halt=10):
    """Stop divisions that are already attacking while combat-ineffective.

    An order in HOI4 is not cancelled when the division runs out of equipment: it
    keeps assaulting and is destroyed. This is the rule that turns a bad opening
    war into 30 -> 21 divisions. Only moves whose destination is ENEMY-controlled
    are considered, so retreats are left alone.
    """
    import unitorders
    halted, failed = [], []
    for pv, rows in (st.get("ours_by") or {}).items():
        for r in rows:
            if len(halted) >= max_halt:
                return halted, failed
            if not r.get("moving"):
                continue
            if r["str_pct"] >= HALT_STR_PCT and r["org"] >= HALT_ORG:
                continue
            path = r.get("path") or []
            dest = path[-1] if path else None
            if dest is None or st["owners"].get(dest) in st["own_tags"]:
                continue
            try:
                ok, err = unitorders.halt(g, r["ptr"])
                rec = {"division": hex(r["ptr"]), "from": pv, "to": dest,
                       "str_pct": r["str_pct"], "org": r["org"]}
                if not ok and err:
                    rec["error"] = err
                (halted if ok else failed).append(rec)
            except Exception as e:
                failed.append({"division": hex(r["ptr"]), "error": str(e)})
    return halted, failed


def _attack_candidates(st, q, mode):
    """Divisions that may attack province q, respecting the home guard."""
    adj, owners = st["adj"], st["owners"]
    min_org_pct = 0.55
    min_str_pct = MIN_ATTACK_STR_PCT
    max_oos = 3
    total_want = {"careful": 6, "balanced": 8, "aggressive": 9}.get(mode, 8)
    dirs = [p for p in adj.get(q, ()) if owners.get(p) in st["own_tags"]]
    per_dir = []
    for p in dirs:
        rows = [r for r in st["ours_by"].get(p, []) if not r["moving"]]
        stay = len(rows)
        # A province that faces another enemy province must keep a garrison.
        other_enemy = [n for n in adj.get(p, ())
                       if owners.get(n) == st["target_tag"] and n != q]
        keep = (2 if stay >= 5 else 1) if other_enemy else 0
        ready = [r for r in rows
                 if r["str_pct"] >= min_str_pct and r["org"] >= MIN_ATTACK_ORG
                 and r["org_pct"] >= min_org_pct
                 and r["oos"] < max_oos and r["org"] > 0]
        max_take = max(0, stay - keep)
        ready.sort(key=lambda r: -(r["org_pct"] * 0.6 + r["str_pct"] * 0.4))
        per_dir.append({"province": p, "staying": stay, "keep": keep,
                        "ready": ready[:max_take], "other_enemy": other_enemy})
    # Ensure at least one direction contributes; then fill by quality.
    chosen = []
    for d in sorted(per_dir, key=lambda x: -len(x["ready"])):
        if d["ready"] and len(chosen) < total_want:
            chosen.append((d["province"], d["ready"][0]))
            d["ready"] = d["ready"][1:]
    pool = [r for d in per_dir for r in d["ready"]]
    pool.sort(key=lambda r: -(r["org_pct"] * 0.6 + r["str_pct"] * 0.4))
    for r in pool:
        if len(chosen) >= total_want:
            break
        src = next((d["province"] for d in per_dir if any(x["ptr"] == r["ptr"] for x in d["ready"])), None)
        if src is not None:
            chosen.append((src, r))
    seen, out = set(), []
    for src, r in chosen:
        if src is None or r["ptr"] in seen:
            continue
        seen.add(r["ptr"])
        out.append((src, r))
    return out, per_dir, dirs


def _score_target(st, q):
    terrain = (_terrain_name(st, q) or "").lower()
    score = 0.0
    if "plains" in terrain:
        score += 50
    elif "hills" in terrain or "forest" in terrain:
        score -= 20
    elif any(x in terrain for x in ("mountain", "urban", "city", "marsh", "jungle")):
        score -= 60
    dirs = [p for p in st["adj"].get(q, ()) if st["owners"].get(p) in st["own_tags"]]
    score += max(0, len(dirs) - 1) * 30
    e = _enemy_state(st, q)
    score -= e["count"] * 12
    if e["count"]:
        score += (1.0 - e["avg_org_pct"]) * 25
    else:
        score += 35
    return round(score, 1), terrain, dirs, e


def attack_country(g, target_tag, mode="balanced", max_attacks=2, tag=None,
                   plug_border_gaps_done=0):
    """Concentrated punch: 1-2 soft provinces, 6-9 divisions each, home guard kept.

    Returns `accepted_total` and `refused_total`; `ok` is true only when at least
    one command was accepted (or the caller plugged a gap).
    """
    st = _front_state(g, target_tag)
    if not st["border"]:
        # 2026-11-21 (P1-8): "no land border" used to be the whole answer, and the
        # 2026-11-20 playtest had hoi4_border_fronts report 8 border provinces for
        # the same country at the same moment.  The two disagree because this path
        # reads mapadj.pkl (a cached adjacency graph) while border_fronts walks the
        # engine's own CFront.  Ask the engine too and report both numbers, so the
        # caller can tell "really no border" from "stale map cache".
        _eng, _eng_err = None, None
        try:
            import armyops_win as _A
            _segs = _A.fronts_towards(g, st["target_tag"],
                                      g.player_tag() if tag is None else tag)
            _eng = sum(len(s.get("border_provinces") or []) for s in _segs)
        except Exception as _e:
            _eng_err = repr(_e)
        out = {"ok": False, "target_tag": st["target_tag"], "mode": mode,
               "error": "no land border with %s (mapadj: 0)" % st["target_tag"],
               "mapadj_border_provinces": 0,
               "engine_border_provinces": _eng,
               "accepted_total": 0, "refused_total": 0,
               "plugged_gaps": plug_border_gaps_done}
        if _eng_err:
            out["engine_error"] = _eng_err
        if _eng:
            out["error"] = ("no land border with %s according to mapadj.pkl, but the "
                            "engine's CFront reports %d border province(s) -- the "
                            "cached adjacency graph is stale. Use "
                            "hoi4_border_fronts + hoi4_move_divisions instead."
                            % (st["target_tag"], _eng))
            out["hint"] = "mapadj_stale"
        return out
    limit = 2 if int(max_attacks or 2) > 2 else max(1, int(max_attacks or 2))
    scored = []
    skipped = []
    for q in sorted(st["enemy_provs"]):
        if not any(st["owners"].get(p) in st["own_tags"] for p in st["adj"].get(q, ())):
            continue
        score, terrain, dirs, e = _score_target(st, q)
        plan, per_dir, _ = _attack_candidates(st, q, mode)
        need = 4 if mode == "careful" else 6
        if len(plan) < need:
            skipped.append({"province": q, "terrain": terrain, "score": score,
                            "ready": len(plan), "need": need,
                            "reason": "not enough ready divisions after home guard"})
            continue
        scored.append({"province": q, "score": score, "terrain": terrain,
                       "directions": len(dirs), "enemy": e, "plan": plan,
                       "per_dir": per_dir})
    scored.sort(key=lambda x: -x["score"])
    targets = scored[:limit]
    rows, accepted_total, refused_total = [], 0, 0
    used_global = set()
    halted, halt_failed = [], []
    own_attach = g._inj is None
    if own_attach:
        g.attach()
    try:
        errbuf = _alloc_errbuf(g)
        _reset_errbuf(g.p, errbuf)
        # Pull the dying attacks out FIRST: they would otherwise keep grinding
        # themselves to zero while the fresh divisions walk in.
        try:
            halted, halt_failed = _halt_dying_attacks(g, st)
        except Exception as e:
            halt_failed = [{"error": str(e)}]
        for t in targets:
            q = t["province"]
            rec = {"province": q, "terrain": t["terrain"], "score": t["score"],
                   "directions": t["directions"], "enemy": t["enemy"],
                   "divisions": [], "results": []}
            for src, r in t["plan"]:
                if r["ptr"] in used_global:
                    continue
                used_global.add(r["ptr"])
                ok, cmd, planned, err, fb = _run_move(
                    g, r["ptr"], q, FLAG_SETS["attack"], errbuf)
                rec["divisions"].append(hex(r["ptr"]))
                rec["results"].append({"division": hex(r["ptr"]), "from": src,
                                       "ok": ok, "command": hex(cmd) if cmd else None,
                                       "error": err, "fallback": fb})
                if ok:
                    accepted_total += 1
                else:
                    refused_total += 1
            rec["accepted"] = sum(1 for x in rec["results"] if x["ok"])
            rec["refused"] = sum(1 for x in rec["results"] if not x["ok"])
            if rec["results"]:
                rows.append(rec)
    finally:
        if own_attach:
            g.detach()
    return {
        "ok": accepted_total > 0 or plug_border_gaps_done > 0,
        "target_tag": st["target_tag"], "mode": mode,
        "attacks": rows, "attacks_count": len(rows),
        "accepted_total": accepted_total, "refused_total": refused_total,
        "halted_weak_attacks": halted, "halt_failed": halt_failed,
        "plugged_gaps": plug_border_gaps_done,
        "skipped": skipped[:20], "skipped_count": len(skipped),
        "note": ("accepted means the game's CCommand::Run accepted the order; it does NOT "
                 "mean the province was captured. Check hoi4_front_changes with reset=true."),
    }


# ============================================================ wartime tick
def wartime_status(g):
    import combat
    try:
        at = bool(combat.at_war(g))
    except Exception:
        at = False
    enemies = []
    try:
        enemies = combat.enemy_tags(g)
    except Exception:
        pass
    date = None
    try:
        date = g.date()["text"]
    except Exception:
        pass
    return {"ok": True, "at_war": at, "enemy": enemies[0] if enemies else None,
            "enemies": enemies, "date": date}


def wartime_step(g, target_tag=None, max_attacks=2, dry_run=False):
    """One local wartime tick: housekeep -> plug gaps -> concentrated punch."""
    status = wartime_status(g)
    enemy = (target_tag or status.get("enemy") or "").upper() or None
    if not status.get("at_war") or not enemy:
        return {"ok": False, "at_war": bool(status.get("at_war")), "enemy": enemy,
                "date": status.get("date"), "error": "not at war or no enemy tag",
                "plugged_gaps": 0, "accepted_total": 0, "refused_total": 0}
    housekeep = None
    try:
        n_unassigned = len(g.unassigned_divisions())
        if n_unassigned >= 1 and not dry_run:
            housekeep = g.housekeep_divisions()
        elif n_unassigned >= 1:
            housekeep = {"dry_run": True, "unassigned": n_unassigned}
    except Exception as e:
        housekeep = {"ok": False, "error": str(e)}
    gaps = plug_border_gaps(g, enemy, dry_run=dry_run)
    atk = attack_country(g, enemy, mode="balanced", max_attacks=max_attacks,
                         plug_border_gaps_done=gaps.get("plugged_gaps", 0))
    return {
        "ok": bool(gaps.get("plugged_gaps", 0) > 0 or atk.get("accepted_total", 0) > 0),
        "at_war": True, "enemy": enemy, "date": status.get("date"),
        "housekeep": housekeep,
        "plugged_gaps": gaps.get("plugged_gaps", 0),
        "still_empty": gaps.get("still_empty", []),
        "accepted_total": atk.get("accepted_total", 0),
        "refused_total": atk.get("refused_total", 0),
        "halted_weak_attacks": len(atk.get("halted_weak_attacks") or []),
        "gap_detail": gaps,
        "attack_detail": atk,
        "dry_run": bool(dry_run),
    }


# ============================================================ deployment
def _border_distances(g, src, targets, owners=None, allowed=None):
    """One BFS from `src`: {border_province: path_length} for every reachable target."""
    import mapdata
    adj = mapdata.build_adjacency()
    _, ptype = mapdata.load_definitions()
    if owners is None:
        owners, _ = mapdata.province_owner_map(g)
    if allowed is None:
        allowed = _allowed_tags(g)
    target_set = set(targets)
    q = deque([src])
    dist = {src: 0}
    out = {}
    while q and len(out) < len(target_set):
        a = q.popleft()
        for b in adj.get(a, ()):
            if b in dist or ptype.get(b) != "land":
                continue
            if b not in target_set and owners.get(b) not in allowed:
                continue
            dist[b] = dist[a] + 1
            if b in target_set:
                out[b] = dist[b]
            q.append(b)
    return out


def _other_neighbor_borders(owners, adj, ptype, me_tag, target_tag, ignore_tags=None):
    """计算我方除 target_tag 与忽略列表（如盟友/附庸）外，所有其他陆地邻国的边境省集合。"""
    me_u = str(me_tag).upper()
    tgt_u = str(target_tag).upper()
    ign = {me_u, tgt_u} | {str(x).upper() for x in (ignore_tags or ())}
    out = {}
    for prov, own in (owners or {}).items():
        if own != me_u:
            continue
        if ptype and ptype.get(prov) != "land":
            continue
        for nb in (adj or {}).get(prov, ()):
            if ptype and ptype.get(nb) != "land":
                continue
            nb_own = owners.get(nb)
            if not nb_own or nb_own in ign:
                continue
            out.setdefault(nb_own, set()).add(prov)
    return out


def deploy_to_border(g, target_tag, orders_group=None, include_unassigned=True, tag=None,
                     protect_borders=None, min_other_garrison=0):
    """One-click: assemble unassigned divisions, then spread them EVENLY over every
    border province facing `target_tag`, while never stripping another directive's
    garrison or active wartime fronts.

    2026-11-23 (复测 6 P0-2):
      - 自动保护已交战其他敌国边境与 `protect_borders` 指定邻国，防止单点部署抽空主战线；
      - 返回 `other_fronts_at_risk`（列出其他非盟友邻国的接壤省数、部署前后驻军与被抽走师数，
        并标注哪些属于宣战 target_tag 会拉进来的潜在参战国）；
      - 返回 `supply_capacity_total`、`held_in_reserve_summary`（说清未派出师所在省与面向邻国）
        以及补给超载/边境抽空 `warnings`。"""
    import unitorders
    target_tag = str(target_tag).upper()
    me_tag = str(tag or g.player_tag()).upper()
    created = []
    if include_unassigned:
        try:
            created.append(g.housekeep_divisions(tag))
        except Exception as e:
            created.append({"ok": False, "error": str(e)})
    groups = g.order_groups(tag)["groups"]
    divs = _resolve(g, None, orders_group, tag)
    import divreserve as _dr
    divs, _para = _dr.filter_out(g, divs)
    if not divs:
        return {"ok": False, "error": "no divisions", "target_tag": target_tag,
                "exempt_paratroopers": len(_para)}
    border = _border_provinces(g, target_tag)
    if not border:
        return {"ok": False, "target_tag": target_tag,
                "error": "当前与 %s 没有直接接壤的陆地边境省" % target_tag,
                "new_armies_created": len([g for g in groups if g.get("is_army_group")])}
    own = g._inj is None
    if own:
        g.attach()
    rows = []
    import mapdata
    owners, _ = mapdata.province_owner_map(g)
    adj = mapdata.build_adjacency()
    _, ptype = mapdata.load_definitions()
    allowed = _allowed_tags(g)

    # 识别我方盟友/附庸、已交战敌国、以及宣战 target_tag 可能拉入的潜在参战国
    ally_ignore = set()
    active_enemies = set()
    co_belligerents = set()
    try:
        import diplomacy as _dip
        my_rel = _dip.country_relations(g, me_tag)
        if my_rel.get("faction"):
            ally_ignore.update(my_rel["faction"].get("members") or [])
        ally_ignore.update(my_rel.get("subjects") or [])
        if my_rel.get("overlord"):
            ally_ignore.add(my_rel["overlord"])
        active_enemies.update(my_rel.get("at_war_with") or [])
        fc = _dip.war_escalation_forecast(g, target_tag, me_tag=me_tag)
        co_belligerents.update(fc.get("co_belligerents") or [])
    except Exception:
        try:
            import combat as _cb
            active_enemies.update(_cb.enemy_tags(g, me_tag))
        except Exception:
            pass

    other_borders = _other_neighbor_borders(
        owners, adj, ptype, me_tag, target_tag, ignore_tags=ally_ignore
    )

    # 记录所有受控师在部署前的省份（用于精确对比部署前后其他边境驻军变化）
    all_before_provs = {}
    for d in divs:
        p0 = unitorders.province_of(g, d)
        if p0 is not None:
            all_before_provs[d] = p0

    protected_tags = {str(x).upper() for x in (protect_borders or ()) if x}
    try:
        # other directives' garrisons, active wartime fronts, and caller-protected borders are off limits
        other_keep = {}
        try:
            fstate = _load_front_state()
            for otag, odir in (fstate.get("directives") or {}).items():
                otag = str(otag).upper()
                if otag == target_tag:
                    continue
                odir = odir or {}
                if str(odir.get("stance") or "").lower() in ("balanced_attack", "blitzkrieg"):
                    mg = 99
                else:
                    mg = max(1, int(odir.get("min_garrison") or 1))
                for bp in _border_provinces(g, otag):
                    other_keep[bp] = max(other_keep.get(bp, 0), mg)
        except Exception:
            other_keep = {}
        # 自动保护已经处于战争状态的其他敌国边境（每省至少 1 师），避免抽空正在交战的前线
        for etag in active_enemies:
            if etag == target_tag:
                continue
            for bp in other_borders.get(etag, ()):
                other_keep[bp] = max(other_keep.get(bp, 0), 1)
        # 显式要求保护的邻国或全局其他边境保底驻军
        if protected_tags:
            mg_p = max(1, int(min_other_garrison or 1))
            for ptag in protected_tags:
                for bp in other_borders.get(ptag, ()):
                    other_keep[bp] = max(other_keep.get(bp, 0), mg_p)
        elif int(min_other_garrison or 0) > 0:
            mg_all = int(min_other_garrison)
            for bprovs in other_borders.values():
                for bp in bprovs:
                    other_keep[bp] = max(other_keep.get(bp, 0), mg_all)

        border_set = set(border)
        cov = Counter()
        kept = Counter()
        movable = []
        after_provs = dict(all_before_provs)
        for d in divs:
            p = all_before_provs.get(d)
            if p is None:
                continue
            if p in border_set and cov[p] < 4:
                cov[p] += 1
                rows.append({"division": hex(d), "from": p, "to": p,
                             "ok": True, "note": "already on the border"})
                continue
            if p in other_keep and kept[p] < other_keep[p]:
                kept[p] += 1
                continue
            movable.append((d, p))
        # Even spread: every division goes to the least-covered reachable tile.
        errbuf = _alloc_errbuf(g)
        # Run 7: fill the second-line reserve belt FIRST (2 divisions per hub,
        # Berlin included), then spread the rest over the first line. Deploying
        # everything onto the border is how Berlin ended up with 0 divisions.
        hub_list = []
        try:
            hub_list = [int(h) for h in (_front_state(g, target_tag).get("reserve_hubs") or ())]
        except Exception:
            hub_list = []
        hub_cov = Counter()
        rest = []
        for d, src in movable:
            need_hub = [h for h in hub_list if hub_cov[h] < 2]
            if not need_hub:
                rest.append((d, src))
                continue
            dists_h = _border_distances(g, src, need_hub, owners, allowed)
            reach = [h for h in need_hub if h in dists_h]
            if not reach:
                rest.append((d, src))
                continue
            hp = min(reach, key=lambda h: (hub_cov[h], dists_h[h]))
            ok, cmd, planned3, err, fb = _run_move(g, d, hp, FLAG_SETS["move"], errbuf)
            rows.append({"division": hex(d), "from": src, "to": hp,
                         "role": "reserve_hub", "ok": ok,
                         "command": hex(cmd) if cmd else None, "error": err})
            if ok:
                hub_cov[hp] += 1
                after_provs[d] = hp
        movable = rest
        # 2026-11-11 staff layer: each border province gets a supply allotment,
        # and divisions that do not fit stay where they are as the operational reserve.
        allot = {}
        try:
            import staff_supply as _ss
            _fb = _ss.front_budget(g, target_tag, tag)
            allot = {int(k): v for k, v in (_fb.get("per_province") or {}).items()}
        except Exception as _e:
            allot = {}
        cap_per = max(2, int(len(movable) / max(1, len(border))) + 1)
        held_back = 0
        held_provs = Counter()
        for d, src in movable:
            dists = _border_distances(g, src, border, owners, allowed)
            cands = [bp for bp in border if bp in dists
                     and cov[bp] < min(cap_per, allot.get(bp, cap_per))]
            if not cands:
                held_provs[src] += 1
                if any(bp in dists for bp in border):
                    held_back += 1
                    rows.append({"division": hex(d), "from": src, "to": None,
                                 "ok": False, "held_in_reserve": True,
                                 "error": "all reachable border provinces are at their "
                                          "supply allotment"})
                    continue
                rows.append({"division": hex(d), "from": src, "to": None,
                             "ok": False, "error": "no legal land path to any border province"})
                continue
            bp = min(cands, key=lambda x: (cov[x], dists[x]))
            ok, cmd, planned2, err, fb = _run_move(g, d, bp, FLAG_SETS["move"], errbuf)
            rows.append({"division": hex(d), "from": src, "to": bp,
                         "ok": ok, "command": hex(cmd) if cmd else None,
                         "planned_path": planned2, "error": err,
                         "fallback": fb})
            if ok:
                cov[bp] += 1
                after_provs[d] = bp
            else:
                held_provs[src] += 1
    finally:
        if own:
            g.detach()

    # 计算其他非盟友邻国边境部署前后驻军变化与风险清单
    other_fronts_at_risk = []
    for otag, bset in other_borders.items():
        before_n = sum(1 for pv in all_before_provs.values() if pv in bset)
        after_n = sum(1 for pv in after_provs.values() if pv in bset)
        pulled = max(0, before_n - after_n)
        other_fronts_at_risk.append({
            "tag": otag,
            "border_provinces": len(bset),
            "divisions_before": before_n,
            "divisions_on_border": after_n,
            "divisions_pulled_away": pulled,
            "is_at_war": otag in active_enemies,
            "is_co_belligerent": otag in co_belligerents,
        })
    other_fronts_at_risk.sort(
        key=lambda r: (
            not r["is_co_belligerent"],
            not r["is_at_war"],
            -r["divisions_pulled_away"],
            -r["border_provinces"],
            r["tag"],
        )
    )

    # 汇总未移动/被补给上限留驻原地的师所在位置及面向邻国
    by_border_country = Counter()
    for pv, cnt in held_provs.items():
        matched = [ot for ot, bs in other_borders.items() if pv in bs]
        if matched:
            for ot in matched:
                by_border_country[ot] += cnt
        elif pv in border_set:
            by_border_country[target_tag] += cnt
        else:
            by_border_country["interior"] += cnt
    held_in_reserve_summary = {
        "total_not_moved": int(sum(held_provs.values())),
        "held_by_supply_cap": held_back,
        "by_border_country": dict(by_border_country),
        "top_provinces": [
            {"province": int(pv), "divisions": int(cnt)}
            for pv, cnt in held_provs.most_common(12)
        ],
    }

    supply_total = round(sum(float(v or 0.0) for v in allot.values()), 2)
    warnings = []
    # 2026-11-23 (复测 7 B-2): 回读目标边境省上当前已有师的真实断补状态，
    # 若该边境已经出现断补师，立刻发出高优先级告警，防止盲目往断补区继续堆兵。
    oos_on_border = 0
    low_supply_on_border = 0
    try:
        import staff_supply as _ss_chk
        _ld_chk = _ss_chk.load(g, tag) or {}
        for bp in border:
            _lr = _ld_chk.get(bp)
            if not _lr:
                continue
            if (_lr.get("oos_days") or 0) > 0:
                oos_on_border += int(_lr.get("n") or 0)
            elif _lr.get("pct") is not None and float(_lr["pct"]) < 0.50:
                low_supply_on_border += int(_lr.get("n") or 0)
    except Exception:
        pass
    if oos_on_border > 0 or low_supply_on_border > 0:
        warnings.append(
            "border already supply-stressed: %d divisions out of supply, %d below 50%% supply "
            "on %s border -- thin out divisions or upgrade template quality (9inf+3art) instead of stacking more"
            % (oos_on_border, low_supply_on_border, target_tag)
        )
    if supply_total > 0 and len(rows) > 1.5 * supply_total:
        warnings.append(
            "supply overload: %d divisions targeted at %s's %d border provinces whose total "
            "supply allotment is only %.1f (%d held in reserve at origin)"
            % (len(rows), target_tag, len(border), supply_total, held_back)
        )
    for fr in other_fronts_at_risk:
        if fr["is_co_belligerent"] and fr["divisions_on_border"] < max(1, fr["border_provinces"] // 2):
            warnings.append(
                "co-belligerent border exposed: %s (%d border provinces) has only %d divisions "
                "(was %d, pulled away %d) -- declaring on %s will pull %s into the war"
                % (fr["tag"], fr["border_provinces"], fr["divisions_on_border"],
                   fr["divisions_before"], fr["divisions_pulled_away"], target_tag, fr["tag"])
            )
        elif fr["divisions_pulled_away"] > 0 and fr["divisions_on_border"] == 0 and fr["border_provinces"] >= 3:
            warnings.append(
                "border stripped empty: pulled %d divisions away from %s (%d border provinces now at 0)"
                % (fr["divisions_pulled_away"], fr["tag"], fr["border_provinces"])
            )

    # 2026-11-23 (复测 9 E-2/E-3): 检测被逐师搬运的师是否仍隶属挂有前线订单的集团军。
    # deploy_to_border 只下 CMoveCommand 不改前线订单；若原军仍挂着别的边境前线，
    # 引擎下个 tick 会按前线订单把师拉回，导致在两条边境间来回跑。
    armies_with_active_orders = []
    try:
        import sys as _sys_mm
        if _sys_mm.platform == "win32" and getattr(g, "p", None) is not None:
            import armyops_win as _aw_mm
            moved_og_cnt = Counter()
            for r in rows:
                if not r.get("ok") or not r.get("division"):
                    continue
                try:
                    dptr = int(r["division"], 16)
                    ogp = int(g.p.u64(dptr + _aw_mm.CARMY_OG) or 0)
                    if ogp:
                        moved_og_cnt[ogp] += 1
                except Exception:
                    continue
            for ogp, cnt in moved_og_cnt.items():
                try:
                    ords = _aw_mm.orders(g, ogp) or []
                except Exception:
                    ords = []
                if ords:
                    armies_with_active_orders.append({
                        "army": hex(ogp),
                        "moved_divisions": int(cnt),
                        "order_ids": [o.get("id") for o in ords],
                    })
    except Exception:
        armies_with_active_orders = []

    if armies_with_active_orders:
        warnings.append(
            "active_orders_conflict: 被搬运的师中有 %d 支集团军仍挂着现有前线订单（%s）。"
            "hoi4_deploy_to_border 仅下逐师移动命令而不修改前线订单，引擎会自动将这些师拉回原前线；"
            "如需真换防，请先用 hoi4_delete_order 撤掉旧前线订单后再用 hoi4_army_battleplans 重画。"
            % (
                len(armies_with_active_orders),
                ", ".join("%s(%d师, orders=%s)" % (x["army"], x["moved_divisions"], x["order_ids"])
                          for x in armies_with_active_orders[:4]),
            )
        )

    deployed = sum(1 for r in rows if r["ok"])
    out = {
        "ok": deployed > 0,
        "target_tag": target_tag,
        "border_province_count": len(border),
        "border_provinces": border,
        "per_province": dict(cov),
        "reserve_hubs": hub_list,
        "reserve_hub_garrison": dict(hub_cov),
        "divisions_ordered": len(rows),
        "divisions_deployed": deployed,
        "divisions_refused": len(rows) - deployed,
        "held_in_reserve": held_back,
        "held_in_reserve_summary": held_in_reserve_summary,
        "supply_allotment": allot,
        "supply_capacity_total": supply_total,
        "out_of_supply_on_border": oos_on_border,
        "low_supply_on_border": low_supply_on_border,
        "other_fronts_at_risk": other_fronts_at_risk,
        "armies_with_active_orders": armies_with_active_orders,
        "warnings": warnings,
        "new_armies_created": len([g for g in groups if g.get("is_army_group")]),
        "divisions": rows,
    }
    if warnings:
        out["warning"] = "; ".join(warnings)
    return out


def mapdata_owner(g):
    import mapdata
    return mapdata.province_owner_map(g)


# ======================================================================
# FIFTH STAGE: front directives + CA-level tactical tick
#
# The model sets ONE directive per enemy ("hold this line, take these
# provinces, keep 1-2 home guards").  The gateway then runs
# `tactical_front_tick` every 2 game days inside a single attach session and
# costs zero model tokens.  Five laws, in the order of the work sheet:
#   1. classify_division_role    spearhead vs line_holder
#   2. auto_hold_and_org_cycle   plug 0-garrison holes + rotate low-org
#   3. concentrated_width_attack score targets, fill combat width only
#   4. pinning_attack            1 line division pins the reinforcing province
#   5. abort_losing_attacks      break off org<18% / 2.5:1 losing attacks
#
# Directive state lives in `calling/front_directives.json` because the MCP
# server and the gateway can be different processes; both read the same file.
# ======================================================================
import json as _json
import os as _os
import re as _re
import threading as _threading

FRONT_STATE_PATH = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)),
                                 "front_directives.json")
FRONT_STANCES = ("prepare", "hold", "balanced_attack", "blitzkrieg")
TICK_HOURS = 24                      # 1 game day — run 5: 48 h was too slow to plug
_STATE_LOCK = _threading.RLock()
_DMG_HISTORY = {}                    # (pid, division ptr) -> org_damage last tick
_ENEMY_DMG_HISTORY = {}              # (pid, province) -> enemy org_damage last tick
_TEMPLATE_CACHE = {}                 # (pid, tag) -> {template name: composition}
_VP_CACHE = None
_VP_RING_CACHE = None

SPEARHEAD_KEYS = ("panzer", "armor", "tank", "motor", "mechan", "mech",
                  "semi_motor", "cavalry_armored")
WIDTH_BASE = {"plains": 70, "desert": 70, "forest": 60, "hills": 60,
              "mountain": 50, "marsh": 50, "jungle": 50, "urban": 80}

# CA-MOD state-level breakthrough & avoidance weights, ported from
# CA_GER_strategy.txt / GER.txt. The AI mod drives straight at Danzig / Lodz /
# Warsaw and never grinds into the Maginot line; the per-province scorer here
# had no idea what a state was, so it happily attacked Alsace-Lorraine.
CA_PRIORITY_STATES = {
    "POL": {85: 240, 87: 220, 10: 260, 90: 160, 88: 160, 86: 140, 98: 180},
    "FRA": {29: 240, 785: 220, 16: 300, 15: 180},
    "BEL": {6: 220, 34: 220, 977: 220},
    "HOL": {7: 220, 35: 200, 36: 180},
    "SOV": {206: 220, 202: 220, 195: 220, 219: 260, 217: 200},
}
CA_AVOID_STATES = {
    "FRA": {28: -800, 17: -400, 18: -300, 27: -300, 735: -500, 21: -400},
    "SOV": {94: -350, 93: -250, 194: -350},
}


def _load_front_state():
    with _STATE_LOCK:
        try:
            with open(FRONT_STATE_PATH, encoding="utf-8") as fh:
                st = _json.load(fh)
            if isinstance(st, dict) and isinstance(st.get("directives"), dict):
                return st
        except Exception:
            pass
        return {"directives": {}}


def _save_front_state(st):
    with _STATE_LOCK:
        tmp = "%s.%d.%d.tmp" % (FRONT_STATE_PATH, _os.getpid(), _threading.get_ident())
        with open(tmp, "w", encoding="utf-8") as fh:
            _json.dump(st, fh, ensure_ascii=False, indent=1)
        for _attempt in range(3):
            try:
                _os.replace(tmp, FRONT_STATE_PATH)
                break
            except PermissionError:
                time.sleep(0.05)


def _merge_save_front_state(updated_directives):
    """Merge runtime tick updates back into the latest on-disk front_directives.json
    so concurrent set_front_directive / clear_front_directive calls are never lost."""
    with _STATE_LOCK:
        cur = {"directives": {}}
        if _os.path.exists(FRONT_STATE_PATH):
            try:
                with open(FRONT_STATE_PATH, encoding="utf-8") as fh:
                    d = _json.load(fh)
                    if isinstance(d, dict) and isinstance(d.get("directives"), dict):
                        cur = d
            except Exception:
                pass
        for k, upd in updated_directives.items():
            if k in cur["directives"]:
                cur["directives"][k].update(upd)
        tmp = "%s.%d.%d.tmp" % (FRONT_STATE_PATH, _os.getpid(), _threading.get_ident())
        with open(tmp, "w", encoding="utf-8") as fh:
            _json.dump(cur, fh, ensure_ascii=False, indent=1)
        for _attempt in range(3):
            try:
                _os.replace(tmp, FRONT_STATE_PATH)
                break
            except PermissionError:
                time.sleep(0.05)


def _now_text():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _game_date(g):
    try:
        return g.date()
    except Exception:
        return {}


def _date_raw(g):
    try:
        return int(g.date_raw())
    except Exception:
        return None


def _template_meta(g):
    """Per-country template composition, cached for the process lifetime of a pid."""
    try:
        tag = g.player().tag
        pid = g.p.pid
    except Exception:
        tag, pid = None, None
    key = (pid, tag)
    if key in _TEMPLATE_CACHE:
        return _TEMPLATE_CACHE[key]
    out = {}
    try:
        for row in g.template_composition(tag) or []:
            out[row.get("name")] = {"line": row.get("line") or {},
                                    "support": row.get("support") or {}}
    except Exception:
        pass
    _TEMPLATE_CACHE[key] = out
    return out


def _division_width(comp):
    """Combat width from the real battalion list: infantry 2, artillery 3."""
    n = 0
    for k, c in (comp or {}).get("line", {}).items():
        n += int(c) * (3 if "artillery" in k else 2)
    return max(10, min(28, n or 20))


def _vp_provinces():
    global _VP_CACHE
    if _VP_CACHE is not None:
        return _VP_CACHE
    import mapdata
    out = set()
    root = getattr(mapdata, "GAME", None)
    if root:
        d = _os.path.join(root, "history", "states")
        try:
            for fn in _os.listdir(d):
                if not fn.endswith(".txt"):
                    continue
                try:
                    txt = open(_os.path.join(d, fn), encoding="utf-8",
                               errors="replace").read()
                except OSError:
                    continue
                for m in _re.finditer(r"victory_points\s*=\s*\{([^}]*)\}", txt):
                    nums = _re.findall(r"\d+", m.group(1))
                    for i in range(0, len(nums) - 1, 2):
                        out.add(int(nums[i]))
        except Exception:
            pass
    _VP_CACHE = out
    return out


def _vp_ring_bonus():
    """+350 on a VP province, +220 one hop away, +110 two hops away.

    Run 9 took 11 border provinces in 55 days and then stalled: the scorer only
    rewarded the VP province ITSELF (+130), so the one or two approach tiles in
    front of Warsaw / Lodz / Krakow scored like any rural tile and the
    spearheads wandered into the countryside. This is CA-MOD's "drive at the
    victory points, not at the map" gradient.
    """
    global _VP_RING_CACHE
    if _VP_RING_CACHE is not None:
        return _VP_RING_CACHE
    import mapdata
    adj = mapdata.build_adjacency()
    vps = _vp_provinces()
    bonus = {v: 350 for v in vps}
    ring1 = set()
    for v in vps:
        for n1 in adj.get(v, ()):
            if n1 not in bonus:
                bonus[n1] = 220
                ring1.add(n1)
    for n1 in ring1:
        for n2 in adj.get(n1, ()):
            if n2 not in bonus:
                bonus[n2] = 110
    _VP_RING_CACHE = bonus
    return bonus


def _terrain_width(st, q, directions):
    name = (_terrain_name(st, q) or "").lower()
    base = 70
    for k, v in WIDTH_BASE.items():
        if k in name:
            base = v
            break
    return base + 35 * max(0, int(directions) - 1)


def _state_of_province():
    import mapdata
    return mapdata.load_state_provinces()[1]


def _classify_roles(g, st):
    """Law 1: spearhead (armour / motorised / mech) vs line_holder (walking infantry)."""
    from game import item_name, DT_NAME
    p = g.p
    meta = _template_meta(g)
    counts = {"spearhead": 0, "line_holder": 0}
    by_template = {}
    for r in st["ours"]:
        tmpl = r.get("template") or 0
        name = item_name(p, tmpl + DT_NAME) if tmpl else ""
        comp = meta.get(name) or {}
        text = (name + " " + " ".join((comp.get("line") or {}).keys())).lower()
        role = "spearhead" if any(k in text for k in SPEARHEAD_KEYS) else "line_holder"
        r["role"] = role
        r["template_name"] = name
        r["width"] = _division_width(comp)
        counts[role] += 1
        by_template[name] = {"role": role, "width": r["width"]}
    st["role_of"] = {r["ptr"]: r["role"] for r in st["ours"]}
    return {"counts": counts, "templates": by_template,
            "spearhead_divisions": counts["spearhead"],
            "line_holder_divisions": counts["line_holder"]}


def _required_garrison(st, directive=None):
    """Dynamic garrison quota per front province.

    2026-11-06 (run-5 post-mortem): the previous version made the *offensive*
    front thinner than the *hold* front — the French border demanded 3-4 per
    province (France has 4+ divisions across the Rhine) while Poland got 1, so
    the Wehrmacht bled east-to-west every tick and 19 western provinces were
    empty on war day. The quota now follows the STANCE:
      * hold / prepare: `min_garrison` per tile (max 2 if under attack), whole
        line capped at 14 divisions or 22 % of the army — a passive border must
        never eat a third of the army;
      * balanced_attack / blitzkrieg: 2-4 per tile so the front has depth;
        `_attack_keep` (0-1) still lets all but one of them attack immediately.
    """
    directive = directive or {}
    stance = str(directive.get("stance") or st.get("stance") or "balanced_attack").lower()
    min_g = max(1, int(directive.get("min_garrison") or 1))
    req = {}
    total = len(st["ours"])
    border_n = max(1, len(st["border"]))

    if stance in ("hold", "prepare", "defend", "defend_only"):
        # Run 13 (work order 1108): the old `hold_cap = max(len(border)*2, ...)`
        # made the "cap" equal to 2 divisions per tile, so a 24-tile French border
        # legally demanded 48-72 divisions and kept 62 of 112 (55 % of the army)
        # parked in the west for the whole Polish campaign. The cap is now a real
        # cap: 1 division per tile, 2 where the tile is actually being pushed,
        # and never more than 18-22 divisions in total.
        req = {}
        # Run 14: run 13 proved that 1 division per tile is NOT enough — France
        # attacked the moment the west thinned (8 tiles, 12 divisions, ~19 German
        # provinces lost). Two per tile with a hard 30-division ceiling: the west
        # keeps ~16-24 and the east still gets 90+.
        # cap 是"最多占多少师"，但一条前线每个省至少要 1 个 —— 边境省比 30 还多
        # 时物理上压不下去，所以 cap 取 max(边境省数, min(省数*3, 30))。
        hold_cap = max(len(st["border"]), min(len(st["border"]) * 3, 30))
        pressure = {}
        for p in st["border"]:
            nbrs = st["enemy_neighbors"].get(p) or []
            adj_divs = sum(len(st["theirs_by"].get(q, [])) for q in nbrs)
            inc = sum(1 for r in st["theirs"]
                      if (r.get("path") or [None])[-1] == p)
            pressure[p] = inc * 10 + adj_divs
            req[p] = 3 if (inc > 0 or adj_divs >= 6) else 2
        # 2026-11-18 (BUG-G)：旧循环 `if req[p] <= 2: break` 在"每个省都是 2"时
        # 立刻退出，hold_cap 形同虚设 —— 22 个边境省合法要求 44 个师，cap 却写着
        # 30，预备池因此永远是 0，7 个缺口全报 "no reserve division available"。
        # 现在真的削到 cap：先削压力最小的省，最低留 1。
        guard = 0
        while sum(req.values()) > hold_cap and guard < 4 * max(1, len(req)):
            guard += 1
            cand = [p for p in req if req[p] > 1]
            if not cand:
                break
            cand.sort(key=lambda p: (pressure.get(p, 0), -req[p]))
            req[cand[0]] -= 1
        return req

    # Run 7: every front tile keeps 2-3 line holders (never one), the line is
    # capped at 60 % of the home army so the Schwerpunkt fist always exists, and
    # the second-line reserve hubs are in the same quota dict so `_free_pool`
    # protects their two divisions.
    for p in st["border"]:
        nbrs = st["enemy_neighbors"].get(p) or []
        adj_divs = sum(len(st["theirs_by"].get(q, [])) for q in nbrs)
        inc = sum(1 for r in st["theirs"]
                  if (r.get("path") or [None])[-1] == p)
        # Run 13 (work order 1108): an OFFENSIVE front keeps ONE division per tile
        # (two only where it is actually being pushed). The old 2-3 per tile welded
        # 32-48 divisions onto a 16-tile line, so `_width_attack` never found the
        # 4-6 ready divisions a Schwerpunkt needs and the eastern front produced
        # ZERO battles for four months (measured 1940.02: count 0).
        req[p] = 2 if (inc > 0 or adj_divs >= 4) else 1
    home_n = int(st.get("home_count") or total)
    cap = max(border_n, int(home_n * 0.60))
    while sum(req.values()) > cap:
        p = max(req, key=lambda x: req[x])
        if req[p] <= 2:
            break
        req[p] -= 1
    for h, n in (st.get("reserve_req") or {}).items():
        req[int(h)] = int(n)
    return req


def _needed(enemy_n):
    """Divisions required to fire on a target (run 8: broad-front Blitzkrieg).

    2026-11-06 (run-7 post-mortem): demanding 4 divisions against a single
    defender, with 2 welded at home, meant 8 divisions per 1-division target —
    only 1-2 provinces could ever attack per tick and 110 divisions watched a
    40-division Poland for 15 months. The point of a blitzkrieg is that the
    WHOLE line attacks the weak tiles at once:
      * 0 enemy divisions: 1 division walks in;
      * 1 enemy division: 2 (plus one home guard = 3v1);
      * 2 enemy divisions: 4 (2:1);
      * 3+: min(9, 2n+2) concentrated Schwerpunkt punch.
    """
    enemy_n = int(enemy_n or 0)
    if enemy_n <= 0:
        return 1
    if enemy_n == 1:
        # Run 14 (user: "一个满编集团一个月就该平推"): requiring 3 divisions on a
        # 1-defender tile, with only 1 spare per launch province, meant EVERY
        # defended tile was skipped ("not enough ready divisions (0)") while the
        # army sat on the border. 2:1 is enough when we outnumber them 3:1 overall.
        return 2
    if enemy_n == 2:
        return 3
    return min(6, enemy_n + 2)


def _attack_keep(st, p, target):
    """Home guard a launch province `p` keeps while attacking `target` (run 9).

    The enemy stack inside `target` is pinned by our assault and cannot
    counter-attack `p`, so `p` only guards against OTHER adjacent enemy
    provinces:
      * only touches `target`: 0 against an empty tile, 1 otherwise;
      * touches other enemy provinces: 1, or 2 when those hold >= 3 divisions.

    Run 8 welded 2 divisions into every launch province, so 2 provinces x 4
    divisions could only field 4 attackers against a 2-division tile that
    needed 5 — `ready=4 need=5` deadlocked the whole front for months.
    """
    staying = int(st["staying"].get(p, 0))
    if staying <= 0:
        return 0
    other_nbrs = [q for q in (st["enemy_neighbors"].get(p) or []) if q != target]
    if not other_nbrs:
        for q in st["adj"].get(p, ()):
            if q == target:
                continue
            if st["owners"].get(q) == st["target_tag"] or st["theirs_by"].get(q):
                other_nbrs.append(q)
    enemy_n = len(st["theirs_by"].get(target, []))
    recent = st.get("recent_captured") or set()
    if not other_nbrs:
        # A freshly captured tile always keeps one guard; a settled one may step
        # out entirely against an empty target. No cooldown: once 2+ divisions
        # stand in the captured province the extra ones push on immediately.
        keep = 1 if (p in recent or enemy_n > 0) else 0
    else:
        flank_divs = sum(len(st["theirs_by"].get(q, [])) for q in other_nbrs)
        # Run 13: 2 only against a real flank threat (>= 5 enemy divisions next
        # door). The old >= 3 threshold pinned two divisions on almost every
        # launch tile, which is how a 16-tile front ate 32 divisions doing nothing.
        keep = 2 if flank_divs >= 5 else 1
    return max(0, min(keep, staying))


def _lock_valid(st, q):
    """Is a sticky staging target still worth keeping?"""
    if q is None:
        return False
    q = int(q)
    if st["owners"].get(q) != st["target_tag"]:
        return False
    if q not in st["enemy_provs"]:
        return False
    if not [p for p in st["adj"].get(q, ()) if _own_or_held(st, p)]:
        return False
    if len(st["theirs_by"].get(q, [])) >= 5:
        return False
    return True


def _launch_provs(st, q):
    """Our land provinces that can jump straight into enemy province `q`."""
    return sorted(p for p in st["adj"].get(q, ()) if _own_or_held(st, p))


def _free_pool(g, st, req, need_pool=True, locked=None):
    """Divisions that can be moved without opening a new hole.

    - all divisions in non-front owned/held land provinces that have a real land
      path to the front (overseas garrisons are left alone);
    - the surplus above required_garrison in front provinces.

    `locked` = launch provinces of the locked main attack. Nothing is ever
    stripped out of them: the previous tick's massing divisions were being
    re-absorbed here ("left hand masses, right hand steals") and the fist never
    formed.
    """
    pool = []
    border = set(st["border"])
    locked = {int(x) for x in (locked or ())}
    # Run 10: a province captured in the last 5 game days is never a donor for
    # another front — that is how the 14<->20 province seesaw started.
    locked |= {int(x) for x in (st.get("recent_captured") or ())}
    ref = st["border"][0] if st["border"] else None
    # Every OTHER configured front keeps its garrison. Without this, deploying to
    # POL treats the FRA border as "idle reserve" and strips the Siegfried line on
    # day one — which is exactly how the 1940.06 run lost 6 divisions in a week
    # (81 -> 75) while the east was still assembling.
    # Run 13 (work order 1108): a sister front keeps ONE division per border tile,
    # not 99 for the border plus its one-hop belt. Run 12 welded 62 divisions onto
    # the French border and none of them could be recalled east; the ~40 surplus
    # now stays in the pool. The belt itself is not protected at all — a division
    # idling one hop behind a quiet front is exactly what the east needs.
    other_front = {int(p): 1 for p in (st.get("other_front") or ())}
    for p, rows in st["ours_by"].items():
        if st["ptype"].get(p) != "land":
            continue
        if p in locked:
            continue
        if p in border:
            surplus = st["staying"].get(p, 0) - max(req.get(p, 1), other_front.get(p, 0))
            if surplus <= 0:
                continue
            cand = [r for r in rows if not r["moving"]
                    and r["org_pct"] >= MOVE_ORG_PCT and r["str_pct"] >= MOVE_STR_PCT]
            cand.sort(key=lambda r: -(r["org_pct"] * 0.6 + r["str_pct"] * 0.4))
            pool.extend((p, r) for r in cand[:surplus])
        else:
            if not _own_or_held(st, p):
                continue
            # Run 14: NO org/str filter outside the front line. A division stuck
            # in an over-stacked rear province is out of supply (oos >= 5) and
            # pinned at 15-20 % org; requiring 0.35 here is what kept Berlin's
            # 96 divisions there for ever.
            cand = [r for r in rows if not r["moving"]]
            if not cand:
                continue
            # Run 7: a non-border province with a quota (the reserve hubs) keeps
            # its divisions. Berlin had 0 on 1940.02.20 and the war ended there.
            keep_n = max(int(req.get(p, 0) or 0), int(other_front.get(p, 0) or 0))
            if keep_n:
                cand.sort(key=lambda r: -(r["org_pct"] * 0.6 + r["str_pct"] * 0.4))
                cand = cand[keep_n:]
                if not cand:
                    continue
            if need_pool:
                # 2026-11-06: `_land_path(p, border[0])` treated East Prussia as an
                # island whenever border[0] was on the other side of the Corridor.
                # `home_provs` is the BFS closure of ALL border provinces.
                if st.get("home_provs") and p not in st["home_provs"]:
                    continue
            cand.sort(key=lambda r: (0 if st["staying"].get(p, 0) > 6 else 1,
                                     0 if r.get("role") == "spearhead" else 1,
                                     -(r["org_pct"] * 0.6 + r["str_pct"] * 0.4)))
            pool.extend((p, r) for r in cand)
    return pool


def _forward_rear_divisions(g, st, directive, max_moves=4, dry_run=False):
    """March idle divisions sitting in the deep rear to the nearest front province.

    2026-11-06: divisions that finish training land in Brandenburg/6521 and are not
    "on the front", so neither `_plug_state` (it only fills provinces that already
    touch the enemy) nor `_free_pool` (it only feeds the staging law, which looks at
    launch provinces) ever moved them. 50+ fresh divisions sat in Berlin for months
    while the Polish border ran on 2-6 attacking divisions per tick.

    Run 12 (review 2026-11-07): the 24/tick budget turned the whole army into a
    permanent march — 20-29 divisions were re-tasked EVERY tick, so a quarter of
    the Wehrmacht was always in transit and could neither attack, dig in nor
    defend, and the tiles they vacated were counter-attacked. The law now only
    touches the real deep rear (>= 2 hops behind the line), never the second-line
    belt and never a sister front's protection zone, and moves at most 4/tick.
    """
    border = set(st["border"])
    if not border:
        return {"ok": True, "moved": 0, "note": "no front"}
    hubs = {int(h) for h in (st.get("reserve_hubs") or ())}
    hub_req = {int(k): int(v) for k, v in (st.get("reserve_req") or {}).items()}
    # Protected: everything one hop behind THIS front plus a sister front's first
    # line. The sister front's belt is deliberately NOT protected any more (run 13):
    # that belt is where the eastern army has to come from.
    protected = set(st.get("second_line") or ()) | set(st.get("other_front") or ())
    rear = []
    for p, rows in st["ours_by"].items():
        if p in border or p in protected or st["ptype"].get(p) != "land":
            continue
        if not _own_or_held(st, p) or st["enemy_neighbors"].get(p):
            continue
        # Run 14 (work order 96): a reserve hub below quota still keeps its two.
        if p in hubs and (st["staying"].get(p, 0) + st["incoming"].get(p, 0)
                          <= hub_req.get(p, 2)):
            continue
        idle = [r for r in rows if not r["moving"]]
        if not idle:
            continue
        idle.sort(key=lambda r: -(r["org_pct"] * 0.6 + r["str_pct"] * 0.4))
        # BUG (2026-11-08): this used to require org >= 0.65. A province holding
        # 70-96 divisions (Berlin after every newly trained division lands there)
        # instantly exceeds its supply limit, every division's org is capped at
        # 15-20 %, and the org filter then refuses to let ANY of them leave —
        # the Berlin deadlock seen in the 1939.06-08 screenshots. A division in a
        # rear province is always allowed to march, whatever its org.
        #
        # Also take EVERY idle division of the province, not just the first one:
        # one-per-province-per-tick cannot drain a 96-division black hole.
        for r in idle:
            rear.append((p, r, st["staying"].get(p, 0)))
    if not rear:
        return {"ok": True, "moved": 0, "note": "no idle rear division"}
    # Biggest stack first, then the most exhausted: evacuate the black hole.
    rear.sort(key=lambda x: (-x[2], x[1]["org_pct"]))
    max_stack = rear[0][2]
    eff_max = 16 if max_stack > 8 else max_moves
    errbuf = None if dry_run else _alloc_errbuf(g)
    rows = []
    border_list = sorted(border)
    for i, (p, r, stack) in enumerate(rear[:eff_max]):
        # Round-robin the targets: sending every division out of Berlin to the
        # single nearest border province is exactly how one tile ends up with 70
        # divisions and the province next to it with zero.
        target = border_list[i % len(border_list)] if border_list else None
        path = _land_path(g, p, target, st["owners"], st["own_tags"]) if target else None
        if not path:
            path = _nearest_border_path(g, p, border, st["owners"], st["own_tags"])
        if not path:
            continue
        if dry_run:
            rows.append({"division": hex(r["ptr"]), "from": p, "to": path[-1],
                         "ok": True, "dry_run": True})
            r["moving"] = True
            continue
        ok, cmd, planned, err, fb = _run_move(g, r["ptr"], path[-1],
                                              FLAG_SETS["move"], errbuf)
        if ok:
            r["moving"] = True
            st["staying"][p] = st["staying"].get(p, 0) - 1
            st["incoming"][path[-1]] = st["incoming"].get(path[-1], 0) + 1
        rows.append({"division": hex(r["ptr"]), "from": p, "to": path[-1],
                     "ok": ok, "error": err, "dry_run": bool(dry_run)})
    return {"ok": True, "moved": sum(1 for x in rows if x["ok"]),
            "tried": len(rows), "moves": rows, "dry_run": bool(dry_run)}


def _fill_reserve_hubs(g, st, dry_run=False):
    """Law 2c (run 7): weld 2 divisions into every second-line reserve hub.

    `_free_pool` protects the hubs, but nothing ever FILLED them: on 1940.02.20
    Berlin held zero divisions and one Polish breakthrough rolled 22 provinces
    deep. Deep-rear divisions are drawn first (they are the ones idling in the
    deployment state); border surplus only as a last resort and never below 2
    per tile.
    """
    hubs = [int(h) for h in (st.get("reserve_hubs") or ())]
    quota = {int(k): int(v) for k, v in (st.get("reserve_req") or {}).items()}
    if not hubs:
        return {"ok": True, "moved": 0, "note": "no reserve hub"}
    border = set(st["border"])
    deficits = {}
    for h in hubs:
        d = quota.get(h, 2) - (st["staying"].get(h, 0) + st["incoming"].get(h, 0))
        if d > 0:
            deficits[h] = d
    if not deficits:
        return {"ok": True, "moved": 0, "hubs": hubs, "attempts": [],
                "dry_run": bool(dry_run)}
    rows, used = [], set()
    errbuf = None if dry_run else _alloc_errbuf(g)
    if errbuf is not None:
        _reset_errbuf(g.p, errbuf)
    cand = []
    for src, divs in st["ours_by"].items():
        if src in border or src in quota or st["ptype"].get(src) != "land":
            continue
        if not _own_or_held(st, src):
            continue
        idle = [r for r in divs
                if not r["moving"] and r["ptr"] not in used
                and r["org_pct"] >= 0.55]
        if not idle:
            continue
        # ONE multi-target BFS per source province, not one per hub.
        dm = _border_distances(g, src, list(deficits), st["owners"], st["own_tags"])
        for h in deficits:
            if h in dm:
                cand.extend((dm[h], src, r, h) for r in idle)
    cand.sort(key=lambda x: (x[0], -x[2]["org_pct"]))
    if not cand:
        # Last resort: one division above the 2-per-tile line quota.
        for h in list(deficits):
            for src, divs in st["ours_by"].items():
                if src not in border:
                    continue
                spare = (st["staying"].get(src, 0)
                         - max(2, int((st.get("required") or {}).get(src, 2))))
                if spare < 1:
                    continue
                for r in divs:
                    if r["moving"] or r["ptr"] in used or r["org_pct"] < 0.6:
                        continue
                    if _land_path(g, src, h, st["owners"], st["own_tags"]) is None:
                        continue
                    cand.append((1, src, r, h))
                    break
    for _, src, r, h in cand:
        if deficits.get(h, 0) <= 0 or r["ptr"] in used:
            continue
        used.add(r["ptr"])
        if dry_run:
            rows.append({"hub": h, "from": src, "division": hex(r["ptr"]),
                         "ok": True, "dry_run": True})
            r["moving"] = True          # T2: hide it from every later law
            st["staying"][src] -= 1
            st["incoming"][h] += 1
            deficits[h] -= 1
            continue
        ok, cmd, planned, err, fb = _run_move(g, r["ptr"], h,
                                              FLAG_SETS["move"], errbuf)
        rows.append({"hub": h, "from": src, "division": hex(r["ptr"]),
                     "ok": ok, "error": err})
        if ok:
            # T2 (review 2026-11-07): the row dict is shared with every later
            # law; without this flag `_width_attack` re-tasked the division in
            # the SAME tick, overwriting the command we just posted.
            r["moving"] = True
            st["staying"][src] -= 1
            st["incoming"][h] += 1
            deficits[h] -= 1
    return {"ok": True, "moved": sum(1 for r in rows if r.get("ok")),
            "hubs": hubs, "unfilled": {k: v for k, v in deficits.items() if v > 0},
            "attempts": rows, "dry_run": bool(dry_run)}


def _update_captured_tracker(g, st, directive):
    """Track provinces captured within the last 5 game days (120 hours)."""
    raw_now = _date_raw(g) or 0
    own_now = sorted(p for p, t in st["owners"].items()
                     if t in st["own_tags"] and st["ptype"].get(p) == "land")
    prev_own = set(int(x) for x in (directive.get("known_own_provs") or []))
    cap_map = dict(directive.get("captured_at") or {})
    if prev_own and raw_now:
        for p in own_now:
            if p not in prev_own:
                cap_map[str(p)] = raw_now
    own_set = set(own_now)
    fresh_map, recent_set = {}, set()
    for k, ts in cap_map.items():
        try:
            pid = int(k)
        except Exception:
            continue
        if pid in own_set and (not raw_now or (raw_now - int(ts)) <= 120):
            fresh_map[str(pid)] = int(ts)
            recent_set.add(pid)
    directive["known_own_provs"] = own_now
    directive["captured_at"] = fresh_map
    st["recent_captured"] = recent_set
    return recent_set


def _garrison_captured(g, st, directive, target_garrison=3, max_moves=16,
                       dry_run=False):
    """Push divisions from former launch provinces into freshly taken tiles.

    Run 9's seesaw: the lead division walks into B, the assault ends, the three
    divisions left in A are now in a non-border province, and until the next
    `_plug_state` B is held by one spent division — Poland retakes it. This law
    moves the idle A stack into B on the very next tick (1 hop, no cooldown).
    """
    recent = set(st.get("recent_captured") or ())
    if not recent:
        return {"ok": True, "reinforced": 0, "moves": [], "provinces": []}
    req = st.get("required") or _required_garrison(st, directive)
    st["required"] = req
    border_set = set(st["border"])
    hubs = set(st.get("reserve_hubs") or ())
    locked = set(st.get("locked_launch") or ()) | recent
    candidates = sorted(
        [p for p in recent
         if p in border_set or st["staying"].get(p, 0) + st["incoming"].get(p, 0) == 0],
        key=lambda p: (0 if p in border_set else 1,
                       st["staying"].get(p, 0) + st["incoming"].get(p, 0)))
    if not candidates:
        return {"ok": True, "reinforced": 0, "moves": [], "provinces": []}
    errbuf = None if dry_run else _alloc_errbuf(g)
    if errbuf is not None:
        _reset_errbuf(g.p, errbuf)
    moves, used, reinforced = [], set(), []
    for p in candidates:
        if len(moves) >= max_moves:
            break
        stay_in = st["staying"].get(p, 0) + st["incoming"].get(p, 0)
        atk_out = st.get("attacking_from", {}).get(p, 0)
        have = (stay_in + atk_out) if stay_in >= 1 else stay_in
        want = target_garrison if p in border_set else 1
        need = want - have
        if need <= 0:
            continue
        donors = []
        for src in st["adj"].get(p, ()):
            if not _own_or_held(st, src) or src in recent:
                continue
            if src in border_set:
                keep = max(2, int(req.get(src, 2)))
            elif src in hubs:
                keep = 1
            else:
                keep = 0
            spare = st["staying"].get(src, 0) - keep
            if spare <= 0:
                continue
            avail = [r for r in st["ours_by"].get(src, [])
                     if not r["moving"] and r["org_pct"] >= 0.45
                     and r["ptr"] not in used]
            avail.sort(key=lambda r: (0 if r.get("role") == "spearhead" else 1,
                                      -(r["org_pct"] * 0.6 + r["str_pct"] * 0.4)))
            donors.extend((1, src, r) for r in avail[:spare])
        if len(donors) < need:
            for src, r in _free_pool(g, st, req, locked=locked):
                if r["ptr"] in used or any(r["ptr"] == x[2]["ptr"] for x in donors):
                    continue
                path = _land_path(g, src, p, st["owners"], st["own_tags"])
                if path is None or len(path) > 3:
                    continue
                donors.append((len(path), src, r))
        donors.sort(key=lambda x: (x[0], -(x[2]["org_pct"] * 0.6
                                           + x[2]["str_pct"] * 0.4)))
        chosen = donors[:min(need, 3)]
        if not chosen:
            continue
        reinforced.append(p)
        for hops, src, r in chosen:
            if len(moves) >= max_moves:
                break
            used.add(r["ptr"])
            if dry_run:
                moves.append({"from": src, "to": p, "hops": hops,
                              "division": hex(r["ptr"]), "ok": True, "dry_run": True})
                r["moving"] = True      # T2: never re-task it in this tick
                st["staying"][src] -= 1
                st["incoming"][p] += 1
                continue
            ok, cmd, planned, err, fb = _run_move(g, r["ptr"], p,
                                                  FLAG_SETS["move"], errbuf)
            moves.append({"from": src, "to": p, "hops": hops,
                          "division": hex(r["ptr"]), "ok": ok,
                          "command": hex(cmd) if cmd else None,
                          "error": err, "fallback": fb})
            if ok:
                r["moving"] = True      # T2
                st["staying"][src] -= 1
                st["incoming"][p] += 1
    return {"ok": True, "reinforced": sum(1 for m in moves if m.get("ok")),
            "provinces": reinforced, "recent_captured": sorted(recent),
            "moves": moves, "dry_run": bool(dry_run)}


def _plug_state(g, st, min_garrison=1, max_gaps=None, dry_run=False, directive=None):
    """Law 2a: fill every front province up to its dynamic required_garrison.

    Replaces the old "only plug 0-division provinces with 1 division" law. It now
    also reinforces a province that is below quota (e.g. a salient under attack)
    and never strips a front province below its own quota.

    `directive` carries the stance: a `hold` front keeps `min_garrison` per tile
    (capped at 14 divisions for the whole line), an offensive front keeps depth.
    """
    req = _required_garrison(st, directive)
    st["required"] = req
    locked = set(st.get("locked_launch") or ())
    border = st["border"]
    deficits = []
    for p in border:
        stay_in = st["staying"].get(p, 0) + st["incoming"].get(p, 0)
        atk_out = st.get("attacking_from", {}).get(p, 0)
        # Run 11: divisions that are fighting OUT of p are still standing in p and
        # still hold the tile. Counting them only when at least one division is
        # actually home (`stay_in >= 1`) keeps the "empty province" alarm intact.
        have = (stay_in + atk_out) if stay_in >= 1 else stay_in
        need = req.get(p, 1) - have
        if need > 0:
            deficits.append((0 if have == 0 else 1, -need, p, need))
    deficits.sort()
    if max_gaps:
        deficits = deficits[:int(max_gaps)]
    rows, used = [], set()
    errbuf = None
    if not dry_run:
        errbuf = _alloc_errbuf(g)
        _reset_errbuf(g.p, errbuf)
    pool = _free_pool(g, st, req, locked=locked)
    # Run 13: a HOLD front may only plug itself from its own depth (2 hops). Without
    # this the western front pulled the eastern staging pool west every tick — the
    # mirror image of the 62-division trap we just removed.
    _stance = str((directive or {}).get("stance") or "").lower()
    if _stance in ("hold", "prepare", "defend", "defend_only"):
        _near = set(border)
        for _ in range(2):
            _near |= {nb for _p in list(_near) for nb in st["adj"].get(_p, ())}
        pool = [(p, r) for (p, r) in pool if p in _near]
    # Adjacent reserves first: they arrive in hours instead of days.
    for _, _, p, need in deficits:
        want = min(need, 2)
        picks = [(src, r) for src, r in pool
                 if r["ptr"] not in used
                 and src in st["adj"].get(p, ())]
        if len(picks) < want:
            # 2026-11-06: check the land path and take the NEAREST reserve. The old
            # fill took any pool division, so East Prussia could be "reinforced" by
            # a division in Bavaria (no land path, order refused, slot wasted).
            near = []
            for src, r in pool:
                if r["ptr"] in used or any(r["ptr"] == x[1]["ptr"] for x in picks):
                    continue
                path = _land_path(g, src, p, st["owners"], st["own_tags"])
                if path is None or len(path) > 10:
                    continue
                near.append((len(path), src, r))
            near.sort(key=lambda x: (x[0], -(x[2]["org_pct"] * 0.6 + x[2]["str_pct"] * 0.4)))
            picks += [(src, r) for _, src, r in near[:max(0, want - len(picks))]]
        if not picks and st["staying"].get(p, 0) == 0 and locked:
            # Documented exception: a front province is completely empty and no
            # other reserve exists, so borrow a single division from the massing
            # point (never the last one standing there).
            for src in sorted(locked):
                cand = [r for r in st["ours_by"].get(src, [])
                        if not r["moving"] and r["org_pct"] >= 0.35
                        and r["ptr"] not in used
                        and st["staying"].get(src, 0) > 1]
                if cand:
                    cand.sort(key=lambda r: -(r["org_pct"] * 0.6 + r["str_pct"] * 0.4))
                    picks = [(src, cand[0])]
                    break
        if not picks:
            # 2026-11-18 (BUG-G)：96 个师摊在 22 个边境省上（平均 4.4/省），
            # required 只有 2/省，可 _free_pool 一个都拿不出来 —— 缺口省自己也算
            # surplus，池子被"每个省都只略高于配额"切碎。直接从最堆的边境省借
            # 一个，保留至少 2 个，别让缺口真的空着。
            donors = []
            for src in st["border"]:
                if src == p or st["staying"].get(src, 0) <= 2:
                    continue
                if _land_path(g, src, p, st["owners"], st["own_tags"]) is None:
                    continue
                cand = [r for r in st["ours_by"].get(src, [])
                        if not r["moving"] and r["org_pct"] >= 0.45
                        and r["ptr"] not in used]
                if cand:
                    cand.sort(key=lambda r: -(r["org_pct"] * 0.6 + r["str_pct"] * 0.4))
                    donors.append((st["staying"].get(src, 0), src, cand[0]))
            donors.sort(key=lambda x: -x[0])
            if donors:
                picks = [(donors[0][1], donors[0][2])]
        chosen = picks[:want]
        if not chosen:
            rows.append({"province": p, "ok": False,
                         "error": "no reserve division available"})
            continue
        for src, r in chosen:
            if dry_run:
                used.add(r["ptr"])
                r["moving"] = True      # T2
                rows.append({"province": p, "from": src, "division": hex(r["ptr"]),
                             "role": r.get("role"), "ok": True, "dry_run": True,
                             "need": need})
                st["incoming"][p] += 1
                st["staying"][src] -= 1
                continue
            ok, cmd, planned, err, fb = _run_move(
                g, r["ptr"], p, FLAG_SETS["move"], errbuf)
            rows.append({"province": p, "from": src, "division": hex(r["ptr"]),
                         "role": r.get("role"), "ok": ok, "need": need,
                         "command": hex(cmd) if cmd else None,
                         "planned_path": planned, "error": err, "fallback": fb})
            if ok:
                used.add(r["ptr"])
                r["moving"] = True      # T2
                st["incoming"][p] += 1
                st["staying"][src] -= 1
    def _in_tile(p):
        stay_in = st["staying"].get(p, 0) + st["incoming"].get(p, 0)
        atk_out = st.get("attacking_from", {}).get(p, 0)
        return (stay_in + atk_out) if stay_in >= 1 else stay_in
    still = [p for p in border if _in_tile(p) < req.get(p, 1)]
    empty = [p for p in border if _in_tile(p) == 0]
    return {"empty": empty, "still_empty": still,
            "required": req,
            "deficits": [{"province": p, "need": n} for _, _, p, n in deficits],
            "plugged_gaps": len({r["province"] for r in rows if r.get("ok")}),
            "gaps": rows, "dry_run": bool(dry_run)}


def _org_cycle(g, st, min_garrison=1, max_moves=6, dry_run=False, locked=None):
    """Law 2b: rotate full-org reserves into a province that is being hit or dying.

    `locked` = launch provinces of the locked main attack; they are never donors,
    otherwise the rotation law eats the divisions the staging law just massed.
    """
    locked = {int(x) for x in (locked or ())}
    hubs = {int(h) for h in (st.get("reserve_hubs") or ())}
    attacked = set()
    for r in st["theirs"]:
        if r.get("path"):
            attacked.add(r["path"][-1])
    rows, used, errbuf = [], set(), None
    if not dry_run:
        errbuf = _alloc_errbuf(g)
        _reset_errbuf(g.p, errbuf)
    for pv in st["border"]:
        if len(rows) >= max_moves:
            break
        here = [r for r in st["ours_by"].get(pv, []) if not r["moving"]]
        if not here:
            continue
        avg = sum(r["org_pct"] for r in here) / len(here)
        if pv not in attacked and avg >= 0.22:
            continue
        donors = []
        for src in st["adj"].get(pv, ()):
            if src in locked:
                continue
            # A reserve hub never donates below its 2-division belt quota.
            if src in hubs and st["staying"].get(src, 0) <= 2:
                continue
            if not _own_or_held(st, src):
                continue
            keep = min_garrison if _is_front_src(st, src) else 0
            spare = st["staying"][src] - keep
            if spare < 1:
                continue
            cand = [r for r in st["ours_by"].get(src, [])
                    if not r["moving"] and r["org_pct"] >= 0.65
                    and r.get("role") == "line_holder" and r["ptr"] not in used]
            cand.sort(key=lambda r: -r["org_pct"])
            donors.extend((src, r) for r in cand[:max(0, min(2, spare))])
        for src, r in donors[:2]:
            if len(rows) >= max_moves:
                break
            used.add(r["ptr"])
            if dry_run:
                rows.append({"province": pv, "from": src, "division": hex(r["ptr"]),
                             "ok": True, "dry_run": True,
                             "reason": "attacked" if pv in attacked else "low_org"})
                r["moving"] = True      # T2
                st["staying"][src] -= 1
                st["incoming"][pv] += 1
                continue
            ok, cmd, planned, err, fb = _run_move(g, r["ptr"], pv, FLAG_SETS["move"], errbuf)
            rows.append({"province": pv, "from": src, "division": hex(r["ptr"]),
                         "ok": ok, "error": err, "fallback": fb,
                         "reason": "attacked" if pv in attacked else "low_org"})
            if ok:
                r["moving"] = True      # T2
                st["staying"][src] -= 1
                st["incoming"][pv] += 1
    return {"ok": bool(rows), "province_count": len(st["border"]),
            "attacked": sorted(attacked), "moves": rows,
            "moved": sum(1 for r in rows if r.get("ok")), "dry_run": bool(dry_run)}


def _score_target_v5(st, q, directive):
    terrain = (_terrain_name(st, q) or "").lower()
    e = _enemy_state(st, q)
    p2s = _state_of_province()
    etag = str(st.get("target_tag") or "").upper()
    st_id = p2s.get(q)
    dirs = [p for p in st["adj"].get(q, ()) if st["owners"].get(p) in st["own_tags"]]
    score = 0.0
    why = []
    ca_add = (CA_PRIORITY_STATES.get(etag) or {}).get(st_id, 0)
    ca_sub = (CA_AVOID_STATES.get(etag) or {}).get(st_id, 0)
    if ca_add:
        score += ca_add
        why.append("ca_priority_state_%s" % st_id)
    if ca_sub:
        score += ca_sub
        why.append("ca_avoid_state_%s" % st_id)
    if q in (directive.get("priority_provinces") or []):
        score += 300
        why.append("priority_province")
    if p2s.get(q) in (directive.get("priority_states") or []):
        score += 200
        why.append("priority_state")
    if e["count"] == 0:
        score += 600
        why.append("undefended")
    if e["oos"] >= 3:
        score += 500
        why.append("out_of_supply")
    vp_b = _vp_ring_bonus().get(q, 0)
    if vp_b:
        score += vp_b
        why.append("vp_ring_%d" % vp_b)
    if "plains" in terrain:
        score += 60
    elif "hills" in terrain or "forest" in terrain:
        score -= 25
    elif any(x in terrain for x in ("mountain", "urban", "city", "marsh", "jungle")):
        score -= 80
    score += 35 * max(0, len(dirs) - 1)
    score -= 30 * e["count"]
    return {"province": q, "score": round(score, 1), "terrain": terrain,
            "directions": len(dirs), "enemy": e, "why": why}


def _width_plan(st, target, directive, used):
    """Role-separated Schwerpunkt planner (run 7, after the 6th-run collapse).

    1. Empty tile (`enemy_n == 0`): one division steps in immediately.
    2. Defended tile (`enemy_n >= 1`):
       - every launch province keeps `_attack_keep` (>= 2) entrenched
         `line_holder` divisions at home, untouched;
       - `spearhead` (armour / motorised) may join from org 65 %;
       - spare `line_holder` divisions need org 78 % and str 70 %;
       - the plan is valid only when the total ready force reaches
         `_needed(enemy)` and, against 2+ defenders, at least two directions
         (or three spearheads in a one-direction corridor).
    """
    q = target["province"]
    max_width = _terrain_width(st, q, target["directions"])
    e = target["enemy"]
    enemy_n = int(e.get("count") or 0)
    need = _needed(enemy_n)
    per_dir = []
    widths = []
    for src in st["adj"].get(q, ()):
        if not _own_or_held(st, src):
            continue
        rows = [r for r in st["ours_by"].get(src, [])
                if not r["moving"] and r["ptr"] not in used]
        if not rows:
            continue
        widths.extend(float(r.get("width") or 20) for r in rows[:2])
        keep = _attack_keep(st, src, q) if _is_front_src(st, src) else 0
        if enemy_n == 0:
            # Free grab: one tripwire may step out even if it is the only one.
            keep = max(0, keep - 1)
            cand = [r for r in rows if r["org_pct"] >= 0.40
                    and r["str_pct"] >= 0.40 and r["oos"] < 4]
            cand.sort(key=lambda r: (0 if r.get("role") == "spearhead" else 1,
                                     -r["org_pct"]))
            allowed = max(0, len(rows) - keep)
            if allowed > 0 and cand:
                per_dir.append({"province": src, "keep": keep,
                                "staying": len(rows), "ready": cand[:allowed]})
            continue
        # Defended tile: the entrenched line holders stay, the rest may attack.
        line = [r for r in rows if r.get("role") != "spearhead"]
        spear = [r for r in rows if r.get("role") == "spearhead"]
        line.sort(key=lambda r: -(r["org_pct"] * 0.6 + r["str_pct"] * 0.4))
        guards_line = line[:keep]
        spare_line = line[keep:]
        # If there are fewer line holders than the guard quota, spearheads fill
        # the remaining guard slots (the strongest ones stay home).
        guard_short = max(0, keep - len(guards_line))
        spare_spear = spear[guard_short:]
        # Run 9: a division that just marched 1-2 tiles to the assembly point must
        # be able to attack on the next tick. 0.70/0.65 left `ready=4 need=5`
        # deadlocked for months because the 5th division arrived at 0.62-0.66.
        ready_spear = [r for r in spare_spear if r["org_pct"] >= 0.50
                       and r["str_pct"] >= 0.55 and r["oos"] < 3 and r["org"] > 0]
        ready_line = [r for r in spare_line if r["org_pct"] >= 0.62
                      and r["str_pct"] >= 0.55 and r["oos"] < 3 and r["org"] > 0]
        ready = ready_spear + ready_line
        if ready:
            per_dir.append({"province": src, "keep": keep,
                            "staying": len(rows), "ready": ready})
    if not per_dir:
        return [], max_width, 0.0
    total_ready = sum(len(d["ready"]) for d in per_dir)
    if enemy_n >= 1:
        spear_n = sum(1 for d in per_dir for r in d["ready"]
                      if r.get("role") == "spearhead")
        # Run 8: a lone defender dies to 2 armoured divisions; with infantry only
        # it takes 3. No two-direction requirement for single-division tiles —
        # that is what kept 85 % of the line standing still.
        need_eff = need
        if enemy_n == 1:
            need_eff = 2 if spear_n >= 1 else 3
        if total_ready < need_eff:
            return [], max_width, 0.0
        if enemy_n >= 2 and len(per_dir) < 2 and spear_n < 3:
            # Never a one-direction infantry frontal bash into 2+ defenders.
            return [], max_width, 0.0
    widths.sort()
    div_w = widths[len(widths) // 2] if widths else 20.0
    # HOI4 queues divisions above the combat width as reserves and feeds them in
    # as width frees; refusing to queue more than the physical width is what made
    # every 2+-division stack look "unattackable" in run 6.
    width_cap = max_width + 2.0 * div_w
    chosen, width = [], 0.0
    for d in sorted(per_dir, key=lambda x: -len(x["ready"])):
        if not d["ready"] or width + float(d["ready"][0].get("width") or 20.0) > max_width:
            continue
        r = d["ready"].pop(0)
        chosen.append((d["province"], r))
        width += float(r.get("width") or 20.0)
    if enemy_n == 0:
        return chosen[:2], max_width, width
    pool = [(d["province"], r) for d in per_dir for r in d["ready"]]
    pool.sort(key=lambda x: (0 if x[1].get("role") == "spearhead" else 1,
                             -(x[1]["org_pct"] * 0.6 + x[1]["str_pct"] * 0.4)))
    for src, r in pool:
        if len(chosen) >= 12:
            break
        if width + float(r.get("width") or 20.0) > width_cap:
            continue
        chosen.append((src, r))
        width += float(r.get("width") or 20.0)
    return chosen, max_width, width


def _width_attack(g, st, directive, stance="balanced_attack", max_targets=None,
                  dry_run=False, max_moves=16, skip=None):
    """Law 3: concentrated Schwerpunkt (run 7).

    2026-11-06 (run-6 post-mortem): 6-10 two-division attacks per tick across
    the whole border lost every one of them AND ate the reserves that
    `_stage_reserves` needed to build a real fist. Now:
      * undefended tiles are free grabs: up to 4 per tick, not counted against
        the target budget;
      * DEFENDED tiles are limited to `max_targets` (2) real Schwerpunkte and
        `_width_plan` refuses any of them below `_needed()` (local 3:1);
      * `skip` = the province the staging law already attacked this tick.
    """
    if max_targets is None:
        # Run 12 (review 2026-11-07): back to a concentrated fist. Run 8-11 opened
        # 6 simultaneous Schwerpunkte, which with 2-3 divisions each is a broad
        # front of half-attacks that Poland counter-attacks one by one. 3 axes on
        # the attack / 2 when balanced, plus the 2 staging axes below.
        max_targets = 3 if stance == "blitzkrieg" else 2
    skip = {int(x) for x in (skip or ()) if x}
    candidates = []
    for q in sorted(st["enemy_provs"]):
        t = _score_target_v5(st, q, directive)
        if not t["directions"]:
            continue
        candidates.append(t)
    candidates.sort(key=lambda x: -x["score"])
    empties = [t for t in candidates if t["enemy"]["count"] == 0][:4]
    defended = [t for t in candidates if t["enemy"]["count"] >= 1]
    rows = []
    used = set()
    accepted_total = 0
    schwerpunkt_n = 0
    errbuf = None
    if not dry_run:
        errbuf = _alloc_errbuf(g)
        _reset_errbuf(g.p, errbuf)
    for t in empties + defended:
        if accepted_total >= max_moves:
            break
        if t["province"] in skip:
            continue
        # Run 11 (work order 1107): never re-issue an attack on a province that
        # already has enough divisions attacking it. Re-ordering a division that is
        # mid-combat resets its combat progress and pulls a fresh stack out of the
        # line every 24 h, which is how the "fist" bled out in runs 8-10.
        q = t["province"]
        already_atk = st.get("attacking_into", {}).get(q, 0)
        need_q = _needed(t["enemy"]["count"])
        if already_atk >= need_q or (t["enemy"]["count"] == 0 and already_atk >= 1):
            t["skipped"] = "already_under_attack (%d/%d)" % (already_atk, need_q)
            continue
        if t["enemy"]["count"] >= 1 and schwerpunkt_n >= max_targets:
            break
        plan, max_width, width = _width_plan(st, t, directive, used)
        # A single division walking into an empty province is a legal free grab.
        need = _needed(t["enemy"]["count"])
        if len(plan) < need:
            t["skipped"] = "not enough ready divisions after home guard (%d)" % len(plan)
            continue
        t["max_width"] = max_width
        t["planned_width"] = round(width, 1)
        rec = {"province": t["province"], "score": t["score"], "terrain": t["terrain"],
               "directions": t["directions"], "enemy": t["enemy"],
               "max_width": max_width, "planned_width": round(width, 1),
               "why": t["why"], "results": []}
        for src, r in plan:
            if r["ptr"] in used or accepted_total >= max_moves:
                continue
            used.add(r["ptr"])
            if dry_run:
                rec["results"].append({"division": hex(r["ptr"]), "from": src,
                                       "ok": True, "dry_run": True})
                continue
            ok, cmd, planned, err, fb = _run_move(g, r["ptr"], t["province"],
                                                  FLAG_SETS["attack"], errbuf)
            rec["results"].append({"division": hex(r["ptr"]), "from": src,
                                   "role": r.get("role"), "ok": ok,
                                   "command": hex(cmd) if cmd else None,
                                   "error": err, "fallback": fb})
            if ok:
                accepted_total += 1
        rec["accepted"] = sum(1 for x in rec["results"] if x.get("ok"))
        rec["refused"] = sum(1 for x in rec["results"] if not x.get("ok"))
        if rec["accepted"] and t["enemy"]["count"] >= 1:
            schwerpunkt_n += 1
        rows.append(rec)
    return {"ok": sum(r["accepted"] for r in rows) > 0,
            "targets": rows,
            "schwerpunkte": schwerpunkt_n,
            "empty_grabs": sum(1 for r in rows if r["enemy"]["count"] == 0),
            "accepted_total": sum(r["accepted"] for r in rows),
            "refused_total": sum(r["refused"] for r in rows),
            "skipped": [{"province": t["province"], "score": t["score"],
                         "reason": t.get("skipped")}
                        for t in candidates if t.get("skipped")][:20],
            "scored_count": len(candidates),
            "note": "accepted != captured; verify with hoi4_front_changes(reset=true)"}


def _stage_reserves(g, st, directive, dry_run=False, max_axes=3,
                    max_stage_per_axis=5):
    """Law 3a (run 9): 3-axis parallel Schwerpunkt staging.

    Run 8b's health snapshot read `schwerpunkte=1 accepted=3 ready=4 need=5`:
    the old law kept ONE sticky target, looped `targets[:2]`, and returned on
    the first branch it hit — the whole eastern front waited for one division
    to walk to one province. This version maintains up to three spatially
    separated axes at once; an axis that can fire does so and the loop
    CONTINUES to the next axis.
    """
    req = st.get("required") or _required_garrison(st, directive)
    st["required"] = req
    targets = []
    for q in sorted(st["enemy_provs"]):
        t = _score_target_v5(st, q, directive)
        if t["directions"] and t["score"] > -200:
            targets.append(t)
    targets.sort(key=lambda x: -x["score"])
    if not targets:
        return {"ok": True, "staged": 0, "attacked": 0, "axes": [],
                "schwerpunkte": 0, "note": "no target"}

    by_prov = {t["province"]: t for t in targets}
    prev_axes = [int(x) for x in (directive.get("staging_targets") or [])]
    legacy = directive.get("staging_target")
    if legacy and int(legacy) not in prev_axes:
        prev_axes.insert(0, int(legacy))

    active_axes, claimed_launch = [], set()
    for q in prev_axes:
        if q in by_prov and _lock_valid(st, q) and q not in active_axes:
            active_axes.append(q)
            claimed_launch |= set(_launch_provs(st, q))
            if len(active_axes) >= max_axes:
                break
    for pass_overlap in (False, True):
        if len(active_axes) >= max_axes:
            break
        for t in targets:
            q = t["province"]
            if q in active_axes:
                continue
            lps = set(_launch_provs(st, q))
            if not lps:
                continue
            if not pass_overlap and (lps & claimed_launch):
                continue
            active_axes.append(q)
            claimed_launch |= lps
            if len(active_axes) >= max_axes:
                break

    errbuf = None if dry_run else _alloc_errbuf(g)
    if errbuf is not None:
        _reset_errbuf(g.p, errbuf)
    all_locked_launch = set(st.get("locked_launch") or ()) | claimed_launch
    raw_pool = _free_pool(g, st, req, locked=all_locked_launch)
    used = set()
    total_staged = total_attacked = 0
    all_moves, axis_reports, kept_axes = [], [], []

    for q in active_axes:
        t = by_prov.get(q)
        if not t:
            continue
        launch = _launch_provs(st, q)
        if not launch:
            continue
        launch_set = set(launch)
        ready = []
        for p in launch:
            rows = [r for r in st["ours_by"].get(p, [])
                    if not r["moving"] and r["ptr"] not in used]
            spare = max(0, len(rows) - _attack_keep(st, p, q))
            cand = [r for r in rows
                    if r["org_pct"] >= (0.50 if r.get("role") == "spearhead" else 0.62)
                    and r["str_pct"] >= 0.55 and r["oos"] < 3 and r["org"] > 0]
            cand.sort(key=lambda r: (0 if r.get("role") == "spearhead" else 1,
                                     -(r["org_pct"] * 0.6 + r["str_pct"] * 0.4)))
            ready.extend(cand[:spare])
        enemy_n = t["enemy"]["count"]
        need = _needed(enemy_n)
        # Run 11 (work order 1107): divisions already attacking q are invisible to
        # `ready` (they are `moving`) and to `en_route` (their path ends at q, not
        # at a launch province), so every tick staged 4-5 fresh divisions into the
        # same breach. Count them and hold the axis.
        already_atk = st.get("attacking_into", {}).get(q, 0)
        if already_atk >= need or (enemy_n == 0 and already_atk >= 1):
            kept_axes.append(q)
            axis_reports.append({"target": q, "status": "in_combat",
                                 "attacking": already_atk, "ready": len(ready),
                                 "needed": need})
            continue

        if len(ready) >= need:
            atk = ready[:max(need, min(8, len(ready)))]
            moves = []
            for r in atk:
                used.add(r["ptr"])
                if dry_run:
                    moves.append({"division": hex(r["ptr"]), "target": q,
                                  "ok": True, "dry_run": True})
                    continue
                ok, cmd, planned, err, fb = _run_move(g, r["ptr"], q,
                                                      FLAG_SETS["attack"], errbuf)
                moves.append({"division": hex(r["ptr"]), "target": q, "ok": ok,
                              "command": hex(cmd) if cmd else None,
                              "error": err, "fallback": fb})
            n_ok = sum(1 for m in moves if m.get("ok"))
            total_attacked += n_ok
            all_moves.extend(moves)
            kept_axes.append(q)
            axis_reports.append({"target": q, "status": "attacked",
                                 "attacked": n_ok, "ready": len(ready),
                                 "needed": need})
            continue

        en_route = sum(
            1 for r in st["ours"]
            if r["ptr"] not in used and (
                (r["moving"] and (r.get("path") or [None])[-1] in launch_set
                 and r.get("org_pct", 0) >= 0.35)
                or (not r["moving"] and r.get("prov") in launch_set
                    and 0.40 <= r.get("org_pct", 0) < 0.62)))
        if len(ready) + en_route + already_atk >= need:
            kept_axes.append(q)
            axis_reports.append({"target": q, "status": "waiting",
                                 "ready": len(ready),
                                 "waiting": en_route + already_atk,
                                 "needed": need})
            continue

        deficit = need - (len(ready) + en_route + already_atk)
        ready_ptrs = {x["ptr"] for x in ready}
        pool = []
        for src, r in raw_pool:
            if r["ptr"] in used or r["ptr"] in ready_ptrs or src in launch_set:
                continue
            dst = min(launch, key=lambda lp: (0 if src in st["adj"].get(lp, ()) else 1,
                                              len(st["ours_by"].get(lp, []))))
            path = _land_path(g, src, dst, st["owners"], st["own_tags"])
            if path is None or len(path) > 6:
                continue
            pool.append((len(path), 0 if r.get("role") == "spearhead" else 1,
                         -(r["org_pct"] * 0.6 + r["str_pct"] * 0.4), src, r, dst))
        pool.sort(key=lambda x: (x[0], x[1], x[2]))
        chosen = pool[:min(deficit, max_stage_per_axis)]
        if not chosen:
            axis_reports.append({"target": q, "status": "no_pool",
                                 "ready": len(ready), "needed": need})
            continue
        moves = []
        for hops, _role, _o, src, r, dst in chosen:
            used.add(r["ptr"])
            if dry_run:
                moves.append({"from": src, "to": dst, "target": q, "hops": hops,
                              "division": hex(r["ptr"]), "ok": True, "dry_run": True})
                continue
            ok, cmd, planned, err, fb = _run_move(g, r["ptr"], dst,
                                                  FLAG_SETS["move"], errbuf)
            moves.append({"from": src, "to": dst, "target": q, "hops": hops,
                          "division": hex(r["ptr"]), "ok": ok,
                          "command": hex(cmd) if cmd else None,
                          "error": err, "fallback": fb})
        n_staged = sum(1 for m in moves if m.get("ok"))
        total_staged += n_staged
        all_moves.extend(moves)
        if n_staged or ready:
            kept_axes.append(q)
        axis_reports.append({"target": q, "status": "staged", "staged": n_staged,
                             "ready": len(ready), "needed": need, "launch": launch})

    directive["staging_targets"] = kept_axes[:max_axes]
    directive["staging_target"] = kept_axes[0] if kept_axes else None
    primary = axis_reports[0] if axis_reports else {}
    return {"ok": True, "staged": total_staged, "attacked": total_attacked,
            "target": directive["staging_target"],
            "targets": directive["staging_targets"],
            "schwerpunkte": len(kept_axes),
            "needed": primary.get("needed"), "ready": primary.get("ready"),
            "waiting": primary.get("waiting"), "axes": axis_reports,
            "moves": all_moves, "locked": bool(kept_axes)}


def _pinning_attack(g, st, directive, attack_rows, max_pins=2, dry_run=False):
    """Law 4: one line division attacks the enemy province that is feeding
    reinforcements into our breakthrough target, so those divisions cannot move."""
    targets = [r["province"] for r in attack_rows.get("targets", [])]
    if not targets:
        return {"ok": True, "pins": [], "note": "no attack target this tick"}
    pins, errbuf = [], None
    if not dry_run:
        errbuf = _alloc_errbuf(g)
        _reset_errbuf(g.p, errbuf)
    for t in targets:
        if len(pins) >= max_pins:
            break
        for s in st["adj"].get(t, ()):
            if st["owners"].get(s) != st["target_tag"]:
                continue
            feeding = [r for r in st["theirs_by"].get(s, [])
                       if r.get("path") and t in r["path"]]
            if not feeding:
                continue
            donor = None
            for src in st["adj"].get(s, ()):
                if not _own_or_held(st, src):
                    continue
                keep = int(directive.get("min_garrison") or 1) if _is_front_src(st, src) else 0
                spare = st["staying"][src] - keep
                if spare < 2:
                    continue
                cand = [r for r in st["ours_by"].get(src, [])
                        if not r["moving"] and r.get("role") == "line_holder"
                        and r["org_pct"] >= MIN_ATTACK_ORG_PCT
                        and r["str_pct"] >= MIN_ATTACK_STR_PCT and r["oos"] < 3]
                if cand:
                    cand.sort(key=lambda r: -r["org_pct"])
                    donor = (src, cand[0])
                    break
            if not donor:
                continue
            src, r = donor
            if dry_run:
                pins.append({"target": t, "pin_province": s, "from": src,
                             "division": hex(r["ptr"]), "feeding": len(feeding),
                             "ok": True, "dry_run": True})
            else:
                ok, cmd, planned, err, fb = _run_move(g, r["ptr"], s,
                                                      FLAG_SETS["attack"], errbuf)
                pins.append({"target": t, "pin_province": s, "from": src,
                             "division": hex(r["ptr"]), "feeding": len(feeding),
                             "ok": ok, "error": err, "fallback": fb})
                if ok:
                    st["staying"][src] -= 1
            break
    return {"ok": True, "pins": pins, "pinned": sum(1 for p in pins if p.get("ok"))}


def _abort_losing_attacks(g, st, directive, dry_run=False):
    """Law 5: cancel an attack when org<18% or our org loss is 2.5x the enemy's."""
    pid = g.p.pid
    raw_now = _date_raw(g) or 0
    rows, errbuf = [], None
    if not dry_run:
        errbuf = _alloc_errbuf(g)
        _reset_errbuf(g.p, errbuf)
    for r in st["ours"]:
        path = r.get("path") or []
        if not path:
            continue
        dst = path[-1]
        if dst not in st["enemy_provs"]:
            continue
        cur = r.get("prov")
        why = None
        if r["org_pct"] < 0.30:
            why = "org %.0f%% < 30%%" % (r["org_pct"] * 100)
        elif r["str_pct"] < 0.55:
            why = "str %.0f%% < 55%%" % (r["str_pct"] * 100)
        else:
            key = (pid, r["ptr"])
            ekey = (pid, dst)
            e_now = sum(x["org_damage"] for x in st["theirs_by"].get(dst, []))
            prev = _DMG_HISTORY.get(key)
            e_prev = _ENEMY_DMG_HISTORY.get(ekey)
            if isinstance(prev, tuple) and isinstance(e_prev, tuple) and raw_now:
                mine = r["org_damage"] - prev[0]
                theirs = e_now - e_prev[0]
                days = max(1.0, (raw_now - prev[1]) / 24.0)
                mine_d = mine / days
                theirs_d = theirs / days
                if mine_d > 0.1 and mine_d > 2.5 * max(theirs_d, 0.01):
                    why = ("org loss %.2f/day vs enemy %.2f/day (>2.5x)"
                           % (mine_d, theirs_d))
            _DMG_HISTORY[key] = (r["org_damage"], raw_now)
            _ENEMY_DMG_HISTORY[ekey] = (e_now, raw_now)
        if not why or cur is None:
            continue
        if dry_run:
            rows.append({"division": hex(r["ptr"]), "province": cur,
                         "target": dst, "reason": why, "ok": True, "dry_run": True})
            continue
        # 2026-11-18 (BUG-F)：原来把 `cur`（师自己所在的省）当目标省传给
        # _run_move，于是每一次都走 "no legal land path 3510 -> 3510" ——
        # 中止进攻从来没有成功过一次，5 条 cancelled 记录全是这个错。
        # 在 UI 上取消进攻就是右键自己的省，`unitorders.halt` 做的正是这件事。
        import unitorders
        ok, err = unitorders.halt(g, r["ptr"])
        fb = None
        if not ok:
            # 兜底：退到一个相邻的、我们自己控制的省，至少别让它在原地流血。
            try:
                import mapdata
                owners = st.get("owners") or mapdata.province_owner_map(g)[0]
                allowed = st.get("own_tags") or _allowed_tags(g)
                ptype = st.get("ptype") or {}
                for nb in (st.get("adj") or mapdata.build_adjacency()).get(cur, ()):
                    if ptype and ptype.get(nb) != "land":
                        continue
                    if owners.get(nb) not in allowed:
                        continue
                    ok2, _cmd2, _pl2, err2, _fb2 = _run_move(
                        g, r["ptr"], nb, FLAG_SETS["move"], errbuf)
                    if ok2:
                        ok, err, fb = True, "", {"retreat_to": nb}
                        break
                    err = err2 or err
            except Exception as e:
                err = "%s; fallback failed: %r" % (err, e)
        rows.append({"division": hex(r["ptr"]), "province": cur, "target": dst,
                     "reason": why, "action": "halt", "ok": ok, "error": err,
                     "fallback": fb})
    # refresh the baseline for divisions that were not attacking
    for r in st["ours"]:
        if not (r.get("path") and r["path"][-1] in st["enemy_provs"]):
            _DMG_HISTORY[(pid, r["ptr"])] = (r["org_damage"], raw_now)
    return {"ok": True, "aborted": sum(1 for x in rows if x.get("ok")), "cancelled": rows}


_TICK_COUNT = {"n": 0}


def _run_front_tick(g, directive, dry_run=False):
    enemy = directive["enemy_tag"]
    stance = directive.get("stance") or "balanced_attack"
    # 2026-11-08 (run 20): _front_state/_classify_roles are pure memory reads
    # (process_vm_readv). They used to run INSIDE `with g:`, i.e. with every game
    # thread frozen, for hundreds of milliseconds every 8 s. Move them out; only
    # the actual command posting happens under the freeze.
    st = _front_state(g, enemy)
    roles = _classify_roles(g, st)

    # ---------------------------------------------------------------- 2026-11-17
    # Two things this tick got WRONG for the whole 1940 campaign. Both are now
    # handled here, BEFORE the C++ laws and OUTSIDE `with g:` -- plan_front and
    # the air-attach command both need the game's own command queue to drain,
    # which cannot happen while every game thread is ptrace-frozen.
    #
    #  * air:  attach_air_wings_to_armies() used to refuse on Windows
    #          (`attached: 0, scanned: 656`), so the Luftwaffe never flew a sortie.
    #  * land: the tick only ever right-clicked individual divisions. 141 of them
    #          piled into a few Polish border provinces (out of supply, attacks
    #          refused) and the French border was left empty. A player draws ONE
    #          FRONT LINE + ONE OFFENSIVE LINE PER ARMY and lets the engine spread
    #          them along that army's own slice of the front.
    if not dry_run and stance in ("balanced_attack", "blitzkrieg"):
        if not directive.get("air_attached"):
            try:
                air_res = g.attach_air_wings_to_armies()
            except Exception as e:
                air_res = {"error": "%s: %s" % (type(e).__name__, e)}
            directive["air_attached"] = True
            directive["air_attach_result"] = {
                "attached": air_res.get("attached_count", 0),
                "scanned": air_res.get("scanned_wings", 0),
                "refused": air_res.get("refused"),
                "error": air_res.get("error"),
                "wings": (air_res.get("attached") or [])[:8],
            }
            # 2026-11-17: attaching alone does not make the wings fly. Give them
            # a mission too (fighters -> air superiority, CAS -> close support).
            try:
                directive["air_missions"] = g.set_air_missions()
            except Exception as e:
                directive["air_missions"] = {"error": "%s: %s" % (type(e).__name__, e)}
        if directive.get("battleplan", True) and not directive.get("battleplan_drawn"):
            try:
                _dir_armies = directive.get("armies") or None
                _force_bp = bool(directive.get("force_battleplan", False))
                bp = g.plan_front(enemy, armies=_dir_armies, split=True, force=_force_bp)
            except Exception as e:
                bp = {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}
            steps = (bp or {}).get("steps") or {}
            directive["battleplan_drawn"] = bool((bp or {}).get("ok"))
            directive["battleplan_result"] = {
                "ok": (bp or {}).get("ok"),
                "error": (bp or {}).get("error"),
                "mode": (bp or {}).get("mode"),
                "front": steps.get("front"),
                "armies": steps.get("armed"),
            }
    with g:
        # Resolve the sticky Schwerpunkt BEFORE the holding laws run, so the
        # massing point is protected from _plug_state/_org_cycle this tick.
        # Run 9: protect the launch provinces of ALL staging axes (up to 3).
        raw_targets = [int(q) for q in (directive.get("staging_targets") or [])]
        legacy_t = directive.get("staging_target")
        if legacy_t and int(legacy_t) not in raw_targets:
            raw_targets.insert(0, int(legacy_t))
        valid_targets = [int(q) for q in raw_targets if _lock_valid(st, int(q))][:3]
        locked_lps = set()
        if valid_targets:
            directive["staging_targets"] = valid_targets
            directive["staging_target"] = valid_targets[0]
            st["locked_target"] = valid_targets[0]
            for q in valid_targets:
                locked_lps.update(_launch_provs(st, q))
            st["locked_launch"] = sorted(locked_lps)
            directive["staging_ticks"] = int(directive.get("staging_ticks") or 0) + 1
        else:
            directive["staging_targets"] = []
            directive["staging_target"] = None
            directive["staging_ticks"] = 0
            st["locked_target"] = None
            st["locked_launch"] = []
        # Run 10: track the last 5 game days of captures and immediately push the
        # idle former-launch stacks into them (no cooldown) BEFORE the holding
        # laws run, so a fresh tile is never left to a single spent division.
        captured = _update_captured_tracker(g, st, directive)
        garr = _garrison_captured(g, st, directive, target_garrison=3,
                                  max_moves=16, dry_run=dry_run)
        gaps = _plug_state(g, st, int(directive.get("min_garrison") or 1),
                           dry_run=dry_run, directive=directive)
        hubfill = _fill_reserve_hubs(g, st, dry_run=dry_run)
        cycle = _org_cycle(g, st, int(directive.get("min_garrison") or 1),
                           dry_run=dry_run, locked=st.get("locked_launch"))
        # Only an attacking front pulls the rear forward; a `hold` front must not
        # drain the other theatre (that is the 2026-11-05 Siegfried-line lesson).
        # Run 8: it now runs LAST, with a 24-division budget, because a fast
        # advance leaves 2-division garrisons in every province behind the new
        # line and the old 4/tick cap could never catch up.
        fwd = {"ok": True, "moved": 0, "note": "hold stance"}
        attack = {"targets": [], "accepted_total": 0, "refused_total": 0, "skipped": []}
        pin = {"pins": [], "pinned": 0}
        staged = {"ok": True, "staged": 0}
        if stance in ("balanced_attack", "blitzkrieg"):
            # 2026-11-17: the air attach and the native battle plan now run at the
            # TOP of this function (outside `with g:`), see the block above.
            # The old `battleplan_enabled` opt-in drew ONE front line for the whole
            # army; never fall back to it silently.
            if (not dry_run and directive.get("battleplan_enabled")
                    and not directive.get("battleplan_activated")):
                directive["battleplan_activated"] = True
                try:
                    directive["battleplan_optin_result"] = g.activate_army_battleplans(enemy)
                except Exception as e:
                    directive["battleplan_optin_result"] = {"error": str(e)}
            d2 = dict(directive)
            if valid_targets:
                # Run 9: all active axes are priority provinces for the scorer.
                d2["priority_provinces"] = list(valid_targets) + [
                    p for p in (directive.get("priority_provinces") or [])
                    if p not in valid_targets]
            bp_on = bool(directive.get("battleplan_drawn"))
            if bp_on:
                # 2026-11-17: the native front line + offensive line is the attack
                # now. The engine spreads each army over its own slice, respecting
                # width and supply. Massing extra divisions on top of that is
                # exactly what turned the 1940 front into a supply-dead blob.
                attack = {"targets": [], "accepted_total": 0, "skipped": "battleplan"}
            else:
                attack = _width_attack(g, st, d2, stance, dry_run=dry_run)
            used_ptrs = {int(x["division"], 16)
                         for t in (attack.get("targets") or [])
                         for x in (t.get("results") or [])
                         if x.get("ok") and x.get("division")}
            if used_ptrs:
                for rows in st["ours_by"].values():
                    for row in rows:
                        if row["ptr"] in used_ptrs:
                            row["moving"] = True
            if bp_on:
                pin = {"pins": [], "pinned": 0, "skipped": "battleplan"}
            else:
                pin = _pinning_attack(g, st, directive, attack, max_pins=3, dry_run=dry_run)
            for _p in (pin.get("pins") or ()):
                try:
                    used_ptrs.add(int(_p["division"], 16))
                except Exception:
                    pass
            for rows in st["ours_by"].values():
                for row in rows:
                    if row["ptr"] in used_ptrs:
                        row["moving"] = True
            # T1 (review 2026-11-07): NEVER call `st = _front_state(g, enemy)`
            # here. This whole block runs inside `with g:`, i.e. every game thread
            # is ptrace-frozen, so the CCommandQueue has not been consumed yet:
            # a fresh scan reads every division we just ordered as moving=False,
            # wipes st["staying"]/st["incoming"] and hands the same divisions to
            # `_stage_reserves` / `_forward_rear_divisions`, which overwrite the
            # attack orders in the SAME tick. The old "refresh so staging sees
            # them as busy" comment was exactly backwards — `moving=True` below
            # is the correct way to make them visible to the later laws.
            if bp_on:
                staged = {"ok": True, "staged": 0, "skipped": "battleplan"}
                fwd = {"ok": True, "moved": 0, "skipped": "battleplan"}
            else:
                staged = _stage_reserves(g, st, directive, dry_run=dry_run,
                                         max_axes=2, max_stage_per_axis=6)
                fwd = _forward_rear_divisions(g, st, directive, max_moves=10,
                                              dry_run=dry_run)
            if staged.get("staged"):
                attack["deferred"] = ("staging %d division(s) towards %s"
                                      % (staged["staged"], staged.get("target")))
            if staged.get("attacked"):
                attack["accepted_total"] = (int(attack.get("accepted_total") or 0)
                                            + int(staged["attacked"]))
                attack.setdefault("targets", []).append({
                    "province": staged.get("target"),
                    "results": staged.get("moves", []),
                    "from_staging": True,
                })
        abort = _abort_losing_attacks(g, st, directive, dry_run=dry_run)
    # 2026-11-06: the MCP child reached 9.9 GB RSS and was OOM-killed during a run.
    # The front tick builds several 13k-entry province dicts per call; CPython keeps
    # that arena. A periodic full collect keeps the child alive through a long war.
    _TICK_COUNT["n"] = _TICK_COUNT.get("n", 0) + 1
    if _TICK_COUNT["n"] % 10 == 0:
        import gc
        gc.collect()
    return {
        "ok": bool(gaps.get("plugged_gaps") or attack.get("accepted_total")
                   or cycle.get("moved") or pin.get("pinned") or staged.get("staged")
                   or fwd.get("moved") or hubfill.get("moved")),
        "enemy": enemy, "stance": stance, "date": _game_date(g).get("text"),
        "border_provinces": len(st["border"]),
        "exempt_paratroopers": st.get("reserved_count", 0),
        "roles": roles["counts"],
        "required_garrison": st.get("required") or {},
        "plugged_gaps": gaps.get("plugged_gaps", 0),
        "still_empty": gaps.get("still_empty", []),
        "org_rotated": cycle.get("moved", 0),
        "forwarded": fwd.get("moved", 0),
        "reserve_hubs": st.get("reserve_hubs") or [],
        "reserve_hub_moved": hubfill.get("moved", 0),
        "reserve_hub_garrison": {str(h): st["staying"].get(h, 0)
                                 for h in (st.get("reserve_hubs") or [])},
        "schwerpunkte": staged.get("schwerpunkte", 0) or attack.get("schwerpunkte", 0),
        "empty_grabs": attack.get("empty_grabs", 0),
        "staged": staged.get("staged", 0),
        "staging_towards": staged.get("target"),
        "staging_axes": staged.get("axes", []),
        "staging_targets": directive.get("staging_targets") or [],
        "recent_captured": sorted(captured)[:20],
        "garrisoned": garr.get("reinforced", 0),
        "staging_ticks": directive.get("staging_ticks"),
        "locked_launch": st.get("locked_launch") or [],
        "staging_detail": {"needed": staged.get("needed"),
                           "ready": staged.get("ready"),
                           "waiting": staged.get("waiting")},
        "accepted_total": attack.get("accepted_total", 0),
        "refused_total": attack.get("refused_total", 0),
        "attacks": attack.get("targets", []),
        "pinned": pin.get("pinned", 0),
        "pins": pin.get("pins", []),
        "aborted": abort.get("aborted", 0),
        "cancelled": abort.get("cancelled", []),
        "skipped": attack.get("skipped", []),
        "dry_run": bool(dry_run),
        "laws": {"hold": gaps, "org_cycle": cycle, "staging": staged,
                 "forward": fwd, "attack": attack, "pinning": pin, "abort": abort,
                 "reserve_hubs": hubfill},
    }


def set_front_directive(g, enemy_tag, stance="balanced_attack", priority_provinces=None,
                        priority_states=None, min_garrison=1, deploy=None, tag=None,
                        battleplan=True, armies=None):
    """Store the model's campaign order for one enemy; optionally deploy first.

    `battleplan=True` (the default since 2026-11-17) makes the first tactical tick
    draw a NATIVE front line + offensive line for free armies (or armies already
    facing `enemy_tag`, or explicit `armies=[...]`) on `enemy_tag`'s own slice of
    the front -- never stripping armies guarding other borders (e.g. FRA Maginot).
    Pass battleplan=False to get the pure micro tick.
    """
    stance = str(stance or "balanced_attack").lower()
    if stance not in FRONT_STANCES:
        return {"ok": False, "error": "stance must be one of %s" % (FRONT_STANCES,)}
    enemy_tag = str(enemy_tag or "").upper()
    if enemy_tag in ("AUTO", "?", ""):
        try:
            import combat
            tags = combat.enemy_tags(g)
            enemy_tag = (tags[0] if tags else "").upper()
        except Exception:
            enemy_tag = ""
    if not enemy_tag:
        return {"ok": False, "error": "no enemy tag (pass enemy_tag or start a war first)"}
    st = _load_front_state()
    _armies_norm = [str(x) for x in armies] if armies else None
    rec = {
        "enemy_tag": enemy_tag, "stance": stance,
        "priority_provinces": [int(x) for x in (priority_provinces or [])],
        "priority_states": [int(x) for x in (priority_states or [])],
        "min_garrison": max(1, min(2, int(min_garrison or 1))),
        "battleplan": bool(battleplan),
        "armies": _armies_norm,
        "updated": _now_text(), "date": _game_date(g).get("text"),
    }
    prev = st["directives"].get(enemy_tag) or {}
    rec["last_tick"] = prev.get("last_tick")
    if prev.get("battleplan_drawn"):
        # a re-issued directive keeps the plan that is already on the map
        rec["battleplan_drawn"] = prev.get("battleplan_drawn")
        rec["battleplan_result"] = prev.get("battleplan_result")
    if prev.get("air_attached"):
        rec["air_attached"] = True
        rec["air_attach_result"] = prev.get("air_attach_result")
    st["directives"][enemy_tag] = rec
    _save_front_state(st)
    dep = None
    if deploy is None:
        deploy = (stance == "prepare")
    if deploy:
        try:
            dep = deploy_to_border(g, enemy_tag, tag=tag)
        except Exception as e:
            dep = {"ok": False, "error": str(e)}

    # 2026-11-16: storing the directive is NOT arming it.  Nothing on this box
    # executed front_directives.json (there is no gateway process here), so
    # `armed: true` used to mean "a JSON file exists" while the army stood still
    # for four months.  Tick once right now and report what actually happened.
    arm = {"ok": False, "error": "not attempted"}
    try:
        import frontd
        arm = frontd.kick(g, force=True)
        worker = frontd.status()
    except Exception as e:
        worker = {"error": "%s: %s" % (type(e).__name__, e)}
    st2 = _load_front_state()
    rec2 = (st2.get("directives") or {}).get(enemy_tag) or {}
    lt = rec2.get("last_tick") or {}
    pid_now = getattr(getattr(g, "p", None), "pid", None)
    armed = bool(lt and (lt.get("pid") in (None, pid_now)))
    return {"ok": True, "directive": rec2 or rec,
            "armed": armed, "arm_attempt": arm, "worker": worker,
            "last_tick": lt or None,
            "divisions_seen": lt.get("divisions_seen") if lt else None,
            "stored_at": FRONT_STATE_PATH,
            "note": (None if armed else
                     "directive stored but NO tactical tick has run: nothing is "
                     "driving the army yet. Check `worker.alive`; if it is false the "
                     "frontd thread is not running in any MCP server process.")}


def clear_front_directive(enemy_tag=None):
    st = _load_front_state()
    if enemy_tag:
        st["directives"].pop(str(enemy_tag).upper(), None)
    else:
        st["directives"] = {}
    _save_front_state(st)
    return {"ok": True, "directives": sorted(st["directives"])}


def front_directives(g=None):
    """Directives plus the only two facts that matter: is a tick driving them,
    and is anything alive that will keep ticking them."""
    st = _load_front_state()
    pid_now = getattr(getattr(g, "p", None), "pid", None) if g is not None else None
    out = []
    for tag, d in st["directives"].items():
        row = dict(d)
        lt = row.get("last_tick") or {}
        row["hours_since_tick"] = None
        if g is not None and lt.get("raw"):
            try:
                _cur_raw = _date_raw(g)
                if _cur_raw is not None:
                    delta = _cur_raw - lt["raw"]
                    if delta < 0:
                        # The clock read jumped backwards (a stale CDate copy, or a save
                        # loaded over the top).  A negative age is not "fresh": report
                        # nothing and let the caller see the date instead.
                        row["hours_since_tick"] = None
                        row["clock_warning"] = ("clock reads %d game hours EARLIER than "
                                                "the last tick" % delta)
                    else:
                        row["hours_since_tick"] = delta
            except Exception:
                pass
        # 2026-11-16: "directive exists" != "army is being driven".
        # `armed` answers the second question: a tick really ran against THIS game
        # process.  `tick_did_orders` is a different fact -- whether that tick had
        # anything to order -- and must never be confused with it.
        row["ticked"] = bool(lt)
        row["tick_pid_matches"] = bool(lt and (lt.get("pid") in (None, pid_now)))
        row["armed"] = bool(lt and row["tick_pid_matches"])
        row["tick_did_orders"] = (lt.get("ok") if lt else None)
        row["divisions_seen"] = lt.get("divisions_seen") if lt else None
        if not lt:
            row["armed_reason"] = "last_tick is null: the directive has never run"
        elif not row["tick_pid_matches"]:
            row["armed_reason"] = ("last_tick is from game pid %s, current pid is %s"
                                   % (lt.get("pid"), pid_now))
        else:
            row["armed_reason"] = None
        out.append(row)
    try:
        import frontd
        worker = frontd.status()
    except Exception as e:
        worker = {"error": "%s: %s" % (type(e).__name__, e)}
    res = {"ok": True, "count": len(out), "directives": out,
           "armed_count": sum(1 for r in out if r["armed"]),
           "worker": worker, "state_path": FRONT_STATE_PATH}
    if out and not res["armed_count"]:
        res["warning"] = ("no armed directive: %d stored, none ticked. "
                          "The army is NOT being commanded." % len(out))
    elif out and not worker.get("alive"):
        res["warning"] = ("directives are armed but the frontd worker is not alive "
                          "(heartbeat %s s old): nothing will tick them." %
                          worker.get("heartbeat_age_s"))
    return res


def tactical_front_tick(g, enemy_tag=None, force=False, dry_run=False):
    """Run the five CA laws for every armed directive, at most once per 2 game days."""
    st = _load_front_state()
    if not st["directives"]:
        return {"ok": True, "ticks": [],
                "note": "no front directive; call hoi4_set_front_directive"}
    keys = [str(enemy_tag).upper()] if enemy_tag else list(st["directives"])
    raw = _date_raw(g)
    out = []
    for key in keys:
        d = st["directives"].get(key)
        if not d:
            out.append({"enemy": key, "ok": False, "error": "no directive for this tag"})
            continue
        lt = d.get("last_tick") or {}
        last = lt.get("raw")
        pid_now = getattr(getattr(g, "p", None), "pid", None)
        last_pid = lt.get("pid")
        _reset_campaign_flags = False
        if last_pid is not None and pid_now is not None and int(last_pid) != int(pid_now):
            # New game process (reload / restart): the old raw belongs to another
            # timeline and must never gate the new one.
            last = None
            lt = {}
            _reset_campaign_flags = True
        if last is not None and raw is not None and raw < last:
            # Older save loaded in the same process.
            last = None
            lt = {}
            _reset_campaign_flags = True
        if _reset_campaign_flags:
            # Clear sticky per-campaign flags so a restarted/reloaded game redraws
            # battleplans and re-attaches air wings instead of stalling forever.
            d["battleplan_drawn"] = False
            d["air_attached"] = False
            d.pop("known_own_provs", None)
            d.pop("captured_at", None)
            d.pop("staging_targets", None)
            d.pop("staging_target", None)
            d.pop("staging_ticks", None)
        # Honour the 2-game-day gate only for a plausible forward delta.
        # Only the 1-game-day gate is honoured here, and only for a plausible
        # forward delta. 2026-11-06 (run 6): the clock reading itself froze at
        # 1939.05 for 40 minutes; the front then skipped EVERY tick while the
        # real date was 1939.09. A wall-clock escape hatch guarantees command —
        # but only while the game is actually running (never spam a paused game).
        wall_elapsed = time.time() - float(lt.get("wall_ts") or 0)
        gate = bool(not force and not dry_run and raw is not None and last
                    and raw >= last and raw - last < TICK_HOURS)
        if gate and wall_elapsed >= 8.0:
            try:
                import session as _sess
                running = not _sess.is_paused(g)
            except Exception:
                running = True
            gate = not running
        if gate:
            out.append({"enemy": key, "ok": True, "skipped": True,
                        "hours_until_next": TICK_HOURS - (raw - last),
                        "date": _game_date(g).get("text")})
            continue
        try:
            res = _run_front_tick(g, d, dry_run=dry_run)
        except Exception as e:
            res = {"ok": False, "enemy": key,
                   "error": "%s: %s" % (type(e).__name__, e),
                   "date": _game_date(g).get("text")}
        if not dry_run:
            _roles = res.get("roles") or {}
            d["last_tick"] = {
                "pid": pid_now, "raw": raw, "date": res.get("date"),
                "ts": time.time(), "wall_ts": time.time(),
                "plugged_gaps": res.get("plugged_gaps", 0),
                "accepted_total": res.get("accepted_total", 0),
                "refused_total": res.get("refused_total", 0),
                "staged": res.get("staged", 0),
                "pinned": res.get("pinned", 0),
                "aborted": res.get("aborted", 0),
                # `ok` here means "this tick actually issued at least one order".
                # A tick that ran but had nothing to order reports ok:false, so it
                # must NOT be read as "the executor is dead".
                "ok": res.get("ok"),
                "divisions_seen": (int(_roles.get("spearhead") or 0)
                                   + int(_roles.get("line_holder") or 0)),
                "border_provinces": res.get("border_provinces"),
                "error": res.get("error"),
            }
        out.append(res)
    if not dry_run:
        _merge_save_front_state({k: st["directives"][k] for k in keys if k in st["directives"]})
    return {"ok": any(t.get("ok") for t in out), "date": _game_date(g).get("text"),
            "ticks": out}


