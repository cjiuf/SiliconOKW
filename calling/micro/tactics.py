"""五法则战区微操执行引擎：后方补位、新占守城、补洞换防、宽幅进攻、预备队集结、牵制与止损。"""
from __future__ import annotations

import session_state as _SS

from .directives import _date_raw
from .front_state import (
    CA_AVOID_STATES,
    CA_PRIORITY_STATES,
    MIN_ATTACK_ORG_PCT,
    MIN_ATTACK_STR_PCT,
    _attack_keep,
    _enemy_state,
    _free_pool,
    _is_front_src,
    _launch_provs,
    _lock_valid,
    _needed,
    _own_or_held,
    _required_garrison,
    _state_of_province,
    _terrain_name,
    _terrain_width,
    _vp_ring_bonus,
)
from .pathing import (
    FLAG_SETS,
    _alloc_errbuf,
    _allowed_tags,
    _border_distances,
    _land_path,
    _nearest_border_path,
    _reset_errbuf,
    _run_move,
)

_DMG_HISTORY = {}                    # (pid, division ptr) -> org_damage last tick
_ENEMY_DMG_HISTORY = {}              # (pid, province) -> enemy org_damage last tick
_SS.register_cache("movemicro._DMG_HISTORY", container=_DMG_HISTORY)
_SS.register_cache("movemicro._ENEMY_DMG_HISTORY", container=_ENEMY_DMG_HISTORY)


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
