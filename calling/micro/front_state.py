"""前线态势扫描、单位角色/配额评估与旧版战时补洞/进攻接口。"""
from __future__ import annotations

import os as _os
import re as _re
from collections import Counter, deque

import session_state as _SS

from .directives import _load_front_state
from .pathing import (
    A_OOS,
    A_ORG,
    A_STR,
    A_TMPL,
    DEFAULT_GARRISON,
    FLAG_SETS,
    _alloc_errbuf,
    _allowed_tags,
    _border_provinces,
    _fx,
    _land_path,
    _reset_errbuf,
    _run_move,
)

# ============================================================ state scanning
def _scan_country_locked(g, tag):
    """Scan one country's divisions. Caller must already hold an attach session."""
    import divreserve as _dr
    import fastmem as _fm
    import unitorders
    from game import CO_ARMIES, _arr
    p = g.p
    c = g._c(tag)
    div_ptrs = _arr(p, c.ptr, CO_ARMIES)
    out = []
    order_types = {}

    use_batch = (
        bool(div_ptrs)
        and getattr(unitorders.province_of, "__module__", "") == "unitorders"
        and getattr(unitorders.movement, "__module__", "") == "unitorders"
    )
    rows = None
    if use_batch:
        try:
            rows = _fm.read_divisions_batch(
                p,
                div_ptrs,
                off_prov_ptr=unitorders.CARMY_PROVINCE,
                off_prov_id=unitorders.PROV_ID,
                off_path_ptr=unitorders.CARMY_PATH,
                off_path_len=unitorders.CARMY_PATH_SIZE,
                off_org=A_ORG,
                off_str=A_STR,
                off_oos=A_OOS,
                off_tmpl=A_TMPL,
                off_str_dmg=0x448,
                off_org_dmg=0x450,
                off_order_ptr=_dr.CDIV_ORDER,
                off_order_type=_dr.ORDER_TYPE,
            )
        except Exception as _e:
            try:
                import diag as _diag
                _diag.warn("front_state.scan_country.batch_fallback", _e, detail=str(tag))
            except Exception:
                pass
            rows = None
    if rows is not None:
        for r in rows:
            if r is None:
                continue
            path = r["path"]
            order_types[r["ptr"]] = r["order_type"]
            out.append({
                "ptr": r["ptr"],
                "prov": r["prov"],
                "path": path,
                "moving": bool(path),
                "org": round(r["org_raw"] / 1e5, 2),
                "str": round(r["str_raw"] / 1e5, 3),
                "oos": r["oos"],
                "template": r["template"],
                "str_damage": round(r["str_dmg_raw"] / 1e5, 3),
                "org_damage": round(r["org_dmg_raw"] / 1e5, 3),
            })
    else:
        for d in div_ptrs:
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
    native_para = getattr(_dr.is_paradrop_division, "__module__", "") == "divreserve"
    for r in out:
        try:
            if native_para and r["ptr"] in order_types:
                _res, _why = _dr.is_paradrop_division(
                    g, r["ptr"], tmpl=r["template"], order_type_val=order_types[r["ptr"]]
                )
            else:
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
_SS.register_cache("movemicro._CAPITAL_CACHE", container=_CAPITAL_CACHE)


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
_SS.register_cache("movemicro._PEAK_ORG", container=_PEAK_ORG)
_SS.register_cache("movemicro._PEAK_STR", container=_PEAK_STR)


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


_TEMPLATE_CACHE = {}                 # (pid, tag) -> {template name: composition}
_SS.register_cache("movemicro._TEMPLATE_CACHE", container=_TEMPLATE_CACHE)
_VP_CACHE = None
_VP_RING_CACHE = None

SPEARHEAD_KEYS = ("panzer", "armor", "tank", "motor", "mechan", "mech",
                  "semi_motor", "cavalry_armored")
WIDTH_BASE = {"plains": 70, "desert": 70, "forest": 60, "hills": 60,
              "mountain": 50, "marsh": 50, "jungle": 50, "urban": 80}

# CA-MOD 州级突破与规避权重（避免直撞马奇诺防线）
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
