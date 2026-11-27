"""陆地 BFS 寻路、按师移动/分散部署（move_divisions / deploy_to_border）与底层移动发令。"""
from __future__ import annotations

import math
import struct
import sys
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
                     protect_borders=None, min_other_garrison=0,
                     max_divisions=None, reserve_divisions=0):
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
    from . import directives as _dir_mod
    from . import front_state as _fs_mod
    _resolve_fn = _resolve
    _border_provinces_fn = _border_provinces
    _allowed_tags_fn = _allowed_tags
    _load_front_state_fn = _dir_mod._load_front_state
    _front_state_fn = _fs_mod._front_state
    _alloc_errbuf_fn = _alloc_errbuf
    _run_move_fn = _run_move
    target_tag = str(target_tag).upper()
    me_tag = str(tag or g.player_tag()).upper()
    created = []
    if include_unassigned:
        try:
            created.append(g.housekeep_divisions(tag))
        except Exception as e:
            created.append({"ok": False, "error": str(e)})
    groups = g.order_groups(tag)["groups"]
    divs = _resolve_fn(g, None, orders_group, tag)
    import divreserve as _dr
    divs, _para = _dr.filter_out(g, divs)
    if not divs:
        return {"ok": False, "error": "no divisions", "target_tag": target_tag,
                "exempt_paratroopers": len(_para)}
    border = _border_provinces_fn(g, target_tag)
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
    allowed = _allowed_tags_fn(g)

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
            fstate = _load_front_state_fn()
            for otag, odir in (fstate.get("directives") or {}).items():
                otag = str(otag).upper()
                if otag == target_tag:
                    continue
                odir = odir or {}
                if str(odir.get("stance") or "").lower() in ("balanced_attack", "blitzkrieg"):
                    mg = 99
                else:
                    mg = max(1, int(odir.get("min_garrison") or 1))
                for bp in _border_provinces_fn(g, otag):
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
        errbuf = _alloc_errbuf_fn(g)
        # Run 7: fill the second-line reserve belt FIRST (2 divisions per hub,
        # Berlin included), then spread the rest over the first line. Deploying
        # everything onto the border is how Berlin ended up with 0 divisions.
        hub_list = []
        try:
            hub_list = [int(h) for h in (_front_state_fn(g, target_tag).get("reserve_hubs") or ())]
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
            ok, cmd, planned3, err, fb = _run_move_fn(g, d, hp, FLAG_SETS["move"], errbuf)
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
        # 2026-11-26 (长跑 §9 根治):
        #   1) 单省补给上限按 floor(allot[bp]) 严格取整（至少 1 师），防止 2.05 浮点配额放进 3 师导致全线超载；
        #   2) 支持 max_divisions / reserve_divisions 硬预算（由 plan_front_allocation 透传），
        #      超过配额或触及预备队底线时立刻留作后方战略预备队，绝不再把 26 师全砸向 15.8 补给边境。
        allot_int = {
            bp: max(1, int(math.floor(float(v) + 1e-6)))
            for bp, v in allot.items()
            if v is not None and float(v) > 0
        }
        max_border_divs = int(max_divisions) if max_divisions is not None else None
        reserve_floor = max(0, int(reserve_divisions or 0))
        held_back = 0
        held_provs = Counter()
        for idx_m, (d, src) in enumerate(movable):
            remaining_movable = len(movable) - idx_m
            cur_border_total = sum(cov.values())
            if (max_border_divs is not None and cur_border_total >= max_border_divs) or (
                reserve_floor > 0 and remaining_movable <= reserve_floor
            ):
                held_provs[src] += 1
                held_back += 1
                rows.append({"division": hex(d), "from": src, "to": None,
                             "ok": False, "held_in_reserve": True,
                             "error": "held in strategic reserve (budget=%s, reserve=%d)"
                                      % (max_border_divs, reserve_floor)})
                continue
            dists = _border_distances(g, src, border, owners, allowed)
            cands = [bp for bp in border if bp in dists
                     and cov[bp] < min(cap_per, allot_int.get(bp, cap_per))]
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
            ok, cmd, planned2, err, fb = _run_move_fn(g, d, bp, FLAG_SETS["move"], errbuf)
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
    # 若调用方显式传了 max_divisions 预算且实际部署在补给承载范围内，不再误报 supply overload；
    # 未传预算时保留原有 total_targeted > 1.5 * supply_total 预警口径（兼容既有契约）。
    deployed_on_border = int(sum(cov.values()))
    effective_target_n = deployed_on_border if max_border_divs is not None else len(rows)
    if supply_total > 0 and effective_target_n > 1.5 * supply_total:
        warnings.append(
            "supply overload: %d divisions targeted at %s's %d border provinces whose total "
            "supply allotment is only %.1f (%d held in reserve at origin)"
            % (effective_target_n, target_tag, len(border), supply_total, held_back)
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
