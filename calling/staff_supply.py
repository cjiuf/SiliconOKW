"""Supply policy: how many divisions a province can actually feed.

Everything static here comes from the game's own files, not from a wiki:

  common/defines/00_defines.lua, block NSupply
      INFRA_TO_SUPPLY = 0.3                 each infrastructure level gives supply
      VP_TO_SUPPLY_BASE = 0.2               flat bonus per VP province
      VP_TO_SUPPLY_BONUS_CONVERSION = 0.05  * VP level
      SUPPLY_BASE_MULT = 0.2                multiplier on the supply base
      NODE_INITIAL_SUPPLY_FLOW = 2.8        a supply hub's own flow
      NODE_FLOW_BONUS_PER_RAIL_LEVEL = 0.34 flow added per railway level
      SUPPLY_FLOW_DROP_REDUCTION_AT_MAX_INFRA = 0.30
                                            max infra cuts the distance falloff
  map/supply_nodes.txt    727 hub provinces, one per line: "<level> <province>"
  map/railways.txt        909 railway paths: "<level> <count> <prov> <prov> ..."
  history/states/*.txt    victory points, infrastructure baseline

The engine's own per-province supply number is NOT readable: CProvince has no
serializer field map (fieldmap.field_map returns {}), and CCountrySupplySystem
keeps its numbers in private C++ state. So this module predicts the capacity and
the shadow log corrects the prediction against the one number the engine does
give us: every division's army_current_supply_ratio / max_supply. When the game
says a province holds N divisions at a supply ratio of r, the capacity in
division-units is at least N*r, which is a direct measurement.
"""
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
CFG_PATH = os.path.join(HERE, "staff_config.json")

# 2026-11-23 (复测 7 B-2 根因修复):
# 旧 DEFAULTS 中 base_divs=2.0, divs_per_infra=3.0 把 00_defines.lua 的 INFRA_TO_SUPPLY=0.3
# 放大了约 10 倍（4 级基建单省算成 14 师、4 省算成 41.3 师），且忽略同枢纽多省共享补给池，
# 导致按推荐上限部署的 42 个师全员 out_of_supply_days=30。
# 现按 HOI4 NSupply 真实规模校准为保守安全系数（4 级基建近枢纽单省约 3.5~4.5 标准师，
# 并结合同州/同枢纽共享折减与引擎实测 army_current_supply_ratio 上限约束）。
DEFAULTS = {
    "enabled": True,
    "shadow": True,
    "base_divs": 1.2,
    "divs_per_infra": 0.55,
    "divs_per_rail": 0.35,
    "rail_cap": 1.5,
    "hub_divs": 2.0,
    "divs_per_vp": 0.05,
    "vp_cap_divs": 0.8,
    "reach_falloff_per_province": 0.16,
    "reach_floor": 0.20,
    "shared_state_penalty": 0.18,
    "shared_state_floor": 0.55,
    "foreign_factor": 0.5,
    "isolated_factor": 0.35,
    "hard_margin": 1.0,
    "soft_margin": 0.9,
    "healthy_pct": 0.65,
    "starving_pct": 0.30,
    "rest_pct": 0.667,
    "max_move_batch": 6,
    "same_state_first": True,
}

_STATIC = {}

REST_PCT = 72.0 / 108.0


def cfg():
    """Supply coefficients: config file wins, DEFAULTS fill the gaps."""
    out = dict(DEFAULTS)
    try:
        with open(CFG_PATH, encoding="utf-8") as f:
            doc = json.load(f)
        out.update(doc.get("supply") or {})
    except Exception:
        pass
    return out


def save_cfg(section, values):
    """Merge one section into staff_config.json (used by the calibration pass)."""
    doc = {}
    try:
        with open(CFG_PATH, encoding="utf-8") as f:
            doc = json.load(f)
    except Exception:
        doc = {}
    doc.setdefault(section, {}).update(values)
    with open(CFG_PATH, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=2, ensure_ascii=False)
        f.write("\n")


# --------------------------------------------------------------- static data
def _game_dir():
    import config as _c
    return _c.game_dir()


def hubs():
    """Province ids that carry a supply hub."""
    if "hubs" in _STATIC:
        return _STATIC["hubs"]
    out = set()
    try:
        path = os.path.join(_game_dir(), "map", "supply_nodes.txt")
        with open(path, encoding="latin-1") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2:
                    try:
                        out.add(int(parts[1]))
                    except ValueError:
                        pass
    except OSError:
        pass
    _STATIC["hubs"] = out
    return out


def rail_levels():
    """province_id -> best railway level passing through it."""
    if "rails" in _STATIC:
        return _STATIC["rails"]
    out = {}
    try:
        path = os.path.join(_game_dir(), "map", "railways.txt")
        with open(path, encoding="latin-1") as f:
            for line in f:
                parts = line.split()
                if len(parts) < 3:
                    continue
                try:
                    lvl = int(parts[0])
                except ValueError:
                    continue
                for tok in parts[2:]:
                    try:
                        q = int(tok)
                    except ValueError:
                        continue
                    if lvl > out.get(q, 0):
                        out[q] = lvl
    except OSError:
        pass
    _STATIC["rails"] = out
    return out


def state_infra_static():
    """state_id -> baseline infrastructure level, from history/states.

    Used for states we do not hold (a pocket in occupied land still has whatever
    infrastructure is left), so that a division standing there still gets a
    capacity instead of disappearing from the report.
    """
    if "st_infra_static" in _STATIC:
        return _STATIC["st_infra_static"]
    import re
    out = {}
    d = os.path.join(_game_dir(), "history", "states")
    try:
        names = sorted(os.listdir(d))
    except OSError:
        names = []
    for fn in names:
        if not fn.endswith(".txt"):
            continue
        try:
            txt = open(os.path.join(d, fn), encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        m = re.search(r"\bid\s*=\s*(\d+)", txt)
        if not m:
            continue
        sid = int(m.group(1))
        # the LAST infrastructure= inside the file is the 1936 baseline only when
        # there is a single history block; take the first one, which is the
        # starting level (later dated blocks are the 1939/1940 build-up).
        bm = re.search(r"infrastructure\s*=\s*(\d+)", txt)
        out[sid] = int(bm.group(1)) if bm else 0
    _STATIC["st_infra_static"] = out
    return out


def vp_levels():
    """province_id -> victory point level (max over the state files)."""
    if "vps" in _STATIC:
        return _STATIC["vps"]
    import re
    out = {}
    d = os.path.join(_game_dir(), "history", "states")
    try:
        names = sorted(os.listdir(d))
    except OSError:
        names = []
    for fn in names:
        if not fn.endswith(".txt"):
            continue
        try:
            txt = open(os.path.join(d, fn), encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        for block in re.findall(r"victory_points\s*=\s*\{([^}]*)\}", txt):
            nums = re.findall(r"\d+", block)
            if len(nums) >= 2:
                try:
                    q, lvl = int(nums[0]), int(nums[1])
                except ValueError:
                    continue
                if lvl > out.get(q, 0):
                    out[q] = lvl
    _STATIC["vps"] = out
    return out


def province_state():
    """province_id -> state_id (static, from history/states)."""
    if "p2st" in _STATIC:
        return _STATIC["p2st"]
    import mapdata
    _, p2st = mapdata.load_state_provinces()
    _STATIC["p2st"] = p2st
    return p2st


def adjacency():
    if "adj" in _STATIC:
        return _STATIC["adj"]
    import mapdata
    adj = mapdata.build_adjacency()
    _STATIC["adj"] = adj
    return adj


def _our_tag(g, tag=None):
    if tag:
        return tag
    for fn in ("player_tag", "current_tag"):
        f = getattr(g, fn, None)
        if callable(f):
            try:
                t = f()
                if t:
                    return t
            except Exception:
                pass
    try:
        c = g.player()
        t = getattr(c, "tag", None)
        if isinstance(t, str) and 2 <= len(t) <= 3:
            return t
    except Exception:
        pass
    for tid, t in enumerate(getattr(g, "_tags", []) or []):
        if t and t != "---" and getattr(g, "_tag2idx", {}).get(tid) == 0:
            return t
    return None


def land_reachable(g, tag=None, ctrl=None, max_depth=400):
    """Provinces our CAPITAL can reach over land we actually control.

    Pixel adjacency connects Germany to East Prussia through Poland, so plain
    adjacency is useless here: the supply grid only runs over controlled ground
    (everything else needs convoys). Measured 2026-11-11: 13 divisions in East
    Prussia sat at 29% supply while the same 13 in a connected province sat at
    75-97%.
    """
    import mapdata
    me = _our_tag(g, tag)
    if ctrl is None:
        ctrl = mapdata.province_controller_map(g)
    caps = mapdata.capital_provinces(g)
    starts = [q for q, t in caps.items() if t == me] or [q for q, t in ctrl.items() if t == me][:40]
    adj = adjacency()
    seen = set(starts)
    frontier = list(starts)
    depth = 0
    while frontier and depth < max_depth:
        depth += 1
        nxt = []
        for q in frontier:
            for r in adj.get(q, ()):
                if r in seen:
                    continue
                if ctrl.get(r) != me:
                    continue
                seen.add(r)
                nxt.append(r)
        frontier = nxt
    return seen


def hub_distance(max_depth=12):
    """province_id -> province steps to the nearest supply hub (BFS over land)."""
    key = "hubdist:%d" % max_depth
    if key in _STATIC:
        return _STATIC[key]
    adj = adjacency()
    dist = {}
    frontier = []
    for q in hubs():
        dist[q] = 0
        frontier.append(q)
    depth = 0
    while frontier and depth < max_depth:
        depth += 1
        nxt = []
        for q in frontier:
            for r in adj.get(q, ()):
                if r not in dist:
                    dist[r] = depth
                    nxt.append(r)
        frontier = nxt
    _STATIC[key] = dist
    return dist


# ------------------------------------------------------------------- live
def state_infrastructure(g, tag=None):
    """state_id -> {"infra": level, "buildings": {name: level}} for owned states."""
    import struct
    from game import (_arr, CO_STATES, ST_ID, ST_BUILDINGS, BG_TYPE_ID, BG_LEVEL)
    p = g.p
    g._load_buildings()
    byid = getattr(g, "_bld_by_id", {}) or {}
    out = {}
    for s in _arr(p, g._c(tag).ptr, CO_STATES):
        try:
            sid = p.i32(s + ST_ID)
        except OSError:
            continue
        blds = {}
        for b in _arr(p, s, ST_BUILDINGS):
            try:
                nm = byid.get(p.i32(b + BG_TYPE_ID))
                if nm:
                    blds[nm] = struct.unpack("<h", p.read(b + BG_LEVEL, 2))[0]
            except OSError:
                continue
        out[sid] = {"infra": blds.get("infrastructure", 0), "buildings": blds}
    return out


def capacity(g, tag=None, extra_states=(), ctrl=None):
    """province_id -> capacity record for every province we own, plus any state
    named in `extra_states` (where our divisions stand even if we do not hold it).

    {"prov", "state", "infra", "rail", "vp", "hub", "dist", "cap_divs", "foreign"}
    """
    c = cfg()
    st_infra = state_infrastructure(g, tag)
    owned = set(st_infra)
    static_infra = state_infra_static()
    p2st = province_state()
    rails = rail_levels()
    vps = vp_levels()
    hubs_ = hubs()
    hdist = hub_distance()
    out = {}
    want = owned | set(extra_states or ())
    try:
        reachable = land_reachable(g, tag, ctrl)
    except Exception:
        reachable = None
    for prov, sid in p2st.items():
        if sid not in want:
            continue
        st = st_infra.get(sid)
        foreign = st is None
        infra = (static_infra.get(sid, 0) if foreign else st["infra"])
        rail = min(rails.get(prov, 0), 5)
        vp = vps.get(prov, 0)
        d = hdist.get(prov, 99)
        reach = max(c["reach_floor"], 1.0 - c["reach_falloff_per_province"] * d)
        base = c["base_divs"] + c["divs_per_infra"] * infra
        cap = base * reach
        cap += min(c["rail_cap"], c["divs_per_rail"] * rail)
        cap += min(c["vp_cap_divs"], c["divs_per_vp"] * vp)
        if prov in hubs_:
            cap += c["hub_divs"]
        isolated = False
        if reachable is not None and prov not in reachable:
            isolated = True
            cap *= c["isolated_factor"]
        if foreign:
            # Outside our own supply network the province lives off whatever the
            # occupier can scrounge locally; the engine gives no capital flow.
            cap *= c["foreign_factor"]
        out[prov] = {"prov": prov, "state": sid, "infra": infra,
                     "rail": rails.get(prov, 0), "vp": vp,
                     "hub": prov in hubs_, "dist": d, "foreign": foreign,
                     "isolated": isolated,
                     "cap_divs": round(cap, 2)}
    return out


def load(g, tag=None, limit=400):
    """province_id -> what is actually standing there and how well it is fed."""
    rows = g.division_detail(tag, limit=limit)["divisions"]
    by = {}
    for r in rows:
        prov = r.get("province")
        if prov is None:
            continue
        d = by.setdefault(prov, {"prov": prov, "n": 0, "pcts": [], "ptrs": [],
                                 "oos": [], "moving": 0})
        d["n"] += 1
        mx = r.get("max_supply")
        cur = r.get("army_current_supply_ratio")
        if mx and cur is not None:
            # army_current_supply_ratio is hours of carried supply, max_supply is
            # the 108 h buffer ceiling (SUPPLY_GRACE = 72 h is the resting value of
            # a fully supplied unit, so a healthy division reads 0.67 and a full
            # one 1.0). Keep the 0..1 buffer fill as the health number.
            d["pcts"].append(max(0.0, min(2.0, cur / mx)))
        if r.get("out_of_supply_days") is not None:
            d["oos"].append(r["out_of_supply_days"])
        if r.get("moving"):
            d["moving"] += 1
        if r.get("ptr"):
            d["ptrs"].append(r["ptr"])
    for d in by.values():
        pcts = d.pop("pcts")
        oos = d.pop("oos")
        d["pct"] = round(sum(pcts) / len(pcts), 3) if pcts else None
        d["pct_min"] = round(min(pcts), 3) if pcts else None
        d["oos_days"] = round(sum(oos) / len(oos), 1) if oos else None
    return by


def plan(g, tag=None, apply_moves=False):
    """The staff's supply decision for this tick.

    Two independent measures, because the model alone is not trustworthy:

      * MEASURED (primary): the engine reports every division's carried supply.
        A province whose divisions sit below the resting buffer is over capacity
        *right now*, whatever the formula says. n*(pct/healthy) is how many it
        can actually feed.
      * MODELLED (prior): infrastructure + hub + railway + distance, used for
        provinces with no measurement and as the deploy-time prevention cap.

    Shadow mode (default): computes everything, moves nothing.
    """
    c = cfg()
    by = load(g, tag)
    p2st = province_state()
    st_need = {p2st[q] for q in by if q in p2st}
    try:
        import mapdata
        ctrl = mapdata.province_controller_map(g)
    except Exception:
        ctrl = None
    caps = capacity(g, tag, extra_states=st_need, ctrl=ctrl)
    healthy = c["healthy_pct"]
    provinces = []
    for prov, d in by.items():
        cap = caps.get(prov)
        row = dict(cap) if cap else {"prov": prov, "cap_divs": 0.0, "state": None,
                                     "foreign": None, "infra": None, "rail": None,
                                     "vp": None, "hub": None, "dist": None,
                                     "isolated": None}
        row.update({"n": d["n"], "pct": d["pct"], "pct_min": d["pct_min"],
                    "oos_days": d["oos_days"], "moving": d["moving"]})
        n = d["n"]
        pct = d["pct"]
        # how many the province can feed, measured
        feedable = None
        if pct is not None and n:
            if pct >= healthy:
                feedable = n
            else:
                feedable = round(n * (pct / healthy), 2)
        row["feedable"] = feedable
        # 2026-11-22 (P3): when the province has no row in the supply model the
        # fallback above leaves cap_divs 0.0 and state/infra/rail null, and the old
        # arithmetic then printed `cap_divs: 0` next to `feedable: 2` with
        # `over_model: 2` -- a line that contradicted itself and could not be
        # explained.  Say the model row is missing instead of computing nonsense
        # from it; `over` then rests on the measured value alone.
        has_model = cap is not None
        row["model_missing"] = (not has_model)
        if has_model:
            model_over = round(n - row["cap_divs"] * c["hard_margin"], 2)
        else:
            model_over = None
            row["model_missing_why"] = ("this province is not in the supply model "
                                        "(state/node table); feedable/over_measured "
                                        "come from the game's own supply ratio")
        measured_over = round(n - feedable, 2) if feedable is not None else 0.0
        row["over_model"] = model_over
        row["over_measured"] = measured_over
        row["over"] = max([v for v in (model_over, measured_over)
                           if v is not None] or [0.0])
        row["state_name"] = None
        provinces.append(row)
    provinces.sort(key=lambda r: (-r["over"], r["pct"] if r["pct"] is not None else 9))

    # Supply in this game is a STATE/node budget first and a province budget
    # second: on 2026-03-06 three neighbouring provinces of state 67 sat at 0.55,
    # 0.57 and 0.58 with 7, 5 and 1 divisions - spreading inside the state did
    # nothing. A state whose divisions are below the resting buffer is saturated
    # and must export divisions, not shuffle them.
    state_rows = {}
    for r in provinces:
        st = r.get("state")
        if st is None:
            continue
        sr = state_rows.setdefault(st, {"state": st, "n": 0, "_w": 0.0, "_p": 0.0})
        sr["n"] += r["n"]
        if r["pct"] is not None:
            sr["_w"] += r["n"]
            sr["_p"] += r["pct"] * r["n"]
    for sr in state_rows.values():
        sr["pct"] = round(sr["_p"] / sr["_w"], 3) if sr["_w"] else None
        sr["feedable"] = (round(sr["n"] * (sr["pct"] / c["healthy_pct"]), 2)
                          if sr["pct"] is not None else None)
        sr["over"] = (round(sr["n"] - sr["feedable"], 2)
                      if sr["feedable"] is not None else 0.0)
        sr.pop("_w", None)
        sr.pop("_p", None)
    states = sorted(state_rows.values(), key=lambda x: -(x["over"] or 0))
    saturated = {sr["state"] for sr in states
                 if sr["pct"] is not None and sr["pct"] < c["healthy_pct"]}

    overloaded = [r for r in provinces if r["over"] > 0]
    starving = [r for r in provinces if r["pct"] is not None and r["pct"] < c["starving_pct"]]
    cutoff = [r for r in provinces if r.get("isolated") and r["pct"] is not None
              and r["pct"] < healthy]
    moves = []
    room = {prov: max(0.0, caps[prov]["cap_divs"] - by.get(prov, {}).get("n", 0))
            for prov in caps}
    adj = adjacency()
    for r in overloaded:
        need = int(r["over"] // 1)
        if need <= 0:
            continue
        src_state = p2st.get(r["prov"])
        cands = []
        for q in adj.get(r["prov"], ()):
            if q not in caps or room.get(q, 0) < 1:
                continue
            if caps[q].get("isolated"):
                continue
            q_state = p2st.get(q)
            # never shuffle into a state that is itself saturated
            if q_state in saturated:
                continue
            same = q_state == src_state
            # leaving a saturated state is the point, so a same-state move is
            # only preferred when the state is NOT saturated
            rank = 0 if (same and src_state not in saturated) else 1
            cands.append((rank, -room[q], q))
        cands.sort()
        for _, _, q in cands:
            if need <= 0:
                break
            take = int(min(need, room[q], c["max_move_batch"]))
            if take <= 0:
                continue
            moves.append({"from": r["prov"], "to": q, "count": take,
                          "same_state": p2st.get(q) == src_state,
                          "to_cap": caps[q]["cap_divs"], "to_room": round(room[q], 2)})
            room[q] -= take
            need -= take
        # Fallback: no ADJACENT province could take the surplus. Berlin is
        # surrounded by its own saturated state, so the adjacency-only search
        # returned nothing and 75 divisions sat at 22.6% supply for months while
        # `moves` stayed empty. A division can march across the country, so when
        # the neighbours are full, send the surplus to the emptiest provinces we
        # own anywhere on the network.
        if need > 0:
            # Destination choice must be driven by MEASUREMENT, not by the model.
            # The first version ranked by modelled room and spread 20 provinces
            # over capacity and cut 12 of them off (1944-04). So: a province may
            # only receive if the engine says it is at or above the resting
            # buffer right now; the model then says how much room is left, and an
            # unmeasured (empty) province is filled to half its modelled cap.
            measured_rows = {x["prov"]: x for x in provinces}
            far = []
            for q in caps:
                if q == r["prov"] or caps[q].get("isolated"):
                    continue
                if p2st.get(q) in saturated:
                    continue
                qr = measured_rows.get(q)
                qpct = qr.get("pct") if qr else None
                qn = qr.get("n") if qr else 0
                if qpct is not None and qpct < healthy:
                    continue
                cap_q = caps[q]["cap_divs"]
                # Use live `room[q]` if already decremented by earlier overloaded provinces
                room_q = room.get(q, max(0.0, cap_q - qn))
                if qpct is None:
                    room_q = min(room_q, cap_q * 0.5)
                if room_q < 1:
                    continue
                far.append(((1.0 if qpct is not None else 0.0, qpct or 0.0), room_q, q))
            far.sort(key=lambda t: (-t[0][0], -t[0][1], -t[1]))
            for _, room_q, q in far[:3]:
                if need <= 0:
                    break
                take = int(min(need, room_q, c["max_move_batch"]))
                if take <= 0:
                    continue
                moves.append({"from": r["prov"], "to": q, "count": take,
                              "far": True, "to_cap": caps[q]["cap_divs"],
                              "to_pct": measured_rows.get(q, {}).get("pct"),
                              "to_room": round(room_q, 2)})
                room[q] = room.get(q, 0.0) - take
                need -= take
    pcts = [d["pct"] for d in by.values() if d["pct"] is not None]
    summary = {
        "divisions": sum(d["n"] for d in by.values()),
        "provinces_held": len(by),
        "provinces_over": len(overloaded),
        "divisions_over": int(round(sum(r["over"] for r in overloaded))),
        "provinces_starving": len(starving),
        "provinces_cut_off": len(cutoff),
        "avg_pct": round(sum(pcts) / max(1, len(pcts)), 3),
        "worst": provinces[0] if provinces else None,
        "max_stack": max([d["n"] for d in by.values()] or [0]),
    }
    res = {"ok": True, "mode": "shadow" if c["shadow"] else "live",
           "config": {k: c[k] for k in ("base_divs", "divs_per_infra", "hub_divs",
                                        "hard_margin", "healthy_pct")},
           "summary": summary, "provinces": provinces, "states": states,
           "overloaded": overloaded, "moves": moves, "cut_off": cutoff}
    if apply_moves and not c["shadow"]:
        res["applied"] = _apply(g, moves, by)
    return res


import session_state as _SS

SHADOW_LOG = _SS.state_path(_SS.STAFF_SHADOW_NAME)


def shadow_log(report, date=None, extra=None):
    """Append one line per tick so the coefficients can be fitted later.

    The measurement we get from the engine is (divisions in a province, supply
    ratio there). A province holding N divisions at ratio r proves the capacity
    is at least N*r; a province at r<1 pins it near N*r. Those two facts are
    enough to fit the five coefficients in capacity().
    """
    rows = []
    for r in report.get("provinces", []):
        if not r.get("n"):
            continue
        rows.append({"prov": r["prov"], "n": r["n"], "pct": r.get("pct"),
                     "cap": r.get("cap_divs"), "infra": r.get("infra"),
                     "rail": r.get("rail"), "vp": r.get("vp"),
                     "hub": 1 if r.get("hub") else 0, "dist": r.get("dist"),
                     "foreign": 1 if r.get("foreign") else 0})
    rec = {"date": date, "summary": report.get("summary"), "provinces": rows}
    if extra:
        rec.update(extra)
    # One sample per game day: the worker polls every few wall seconds, and a
    # paused game would otherwise write the same day a hundred times.
    if date:
        if getattr(shadow_log, "_last_date", None) == date:
            return 0
        try:
            with open(SHADOW_LOG, "rb") as f:
                f.seek(0, 2)
                f.seek(max(0, f.tell() - 65536))
                tail = f.read().decode("utf-8", "replace")
            for line in reversed(tail.splitlines()):
                if not line.strip():
                    continue
                try:
                    if json.loads(line).get("date") == date:
                        return 0
                except ValueError:
                    pass
                break
        except OSError:
            pass
    try:
        _SS.ensure_parent(SHADOW_LOG)
        with open(SHADOW_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        if date:
            shadow_log._last_date = date
    except OSError:
        pass
    return len(rows)


def _apply(g, moves, load_rows=None, max_moves=4):
    """Execute the moves with the ordinary division-move tool. Shadow mode never
    reaches this; a province whose move is refused is reported, not retried.

    Bound the work: each move polls for a path (wait=2 s), and a staff tick must
    not take longer than the poll interval or the worker falls behind the game.
    """
    out = []
    for m in moves[:max_moves]:
        ptrs = list((load_rows or {}).get(m["from"], {}).get("ptrs", []))[:m["count"]]
        # division_detail reports pointers as hex strings; the move resolver wants
        # integers. Passing the strings through made every staff move a silent
        # no-op (Berlin kept growing while `moves` was non-empty).
        ptrs = [int(p, 16) if isinstance(p, str) else p for p in ptrs]
        if not ptrs:
            out.append({"move": m, "result": {"ok": False, "error": "no division pointers"}})
            continue
        try:
            r = g.move_divisions(divisions=ptrs, province=m["to"], force=False, wait=2.0)
        except Exception as e:
            r = {"ok": False, "error": repr(e)}
        out.append({"move": m, "result": r})
    return out


# ---------------------------------------------------------------- calibration
def calibrate(min_samples=25, write=False):
    """Fit the five capacity coefficients to what the engine actually did.

    The engine never reports a province's capacity, but it reports, for every
    division, how full its supply buffer is. A province holding N divisions at a
    buffer of r proves capacity >= N*r, and a province below the resting buffer
    pins capacity near N*r. Both are used, the healthy ones as lower bounds with
    a smaller weight.

    Returns the fit and the residuals; write=True stores it in staff_config.json.
    """
    import numpy as np
    c = cfg()
    samples = []
    try:
        with open(SHADOW_LOG, encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                for row in rec.get("provinces") or []:
                    if row.get("foreign"):
                        continue
                    if not row.get("n") or row.get("pct") is None:
                        continue
                    samples.append(row)
    except OSError:
        return {"ok": False, "error": "no shadow log yet"}
    if len(samples) < min_samples:
        return {"ok": False, "error": "not enough samples", "samples": len(samples)}
    # The shadow log holds one row per province per DAY, so a quiet province would
    # dominate the fit with hundreds of identical points. Keep one sample per
    # (province, stack size), and keep the healthiest one: capacity is a ceiling,
    # and the same N at a better ratio is the better lower bound.
    best = {}
    for r in samples:
        key = (r.get("prov"), r.get("n"))
        if key not in best or (r.get("pct") or 0) > (best[key].get("pct") or 0):
            best[key] = r
    samples = list(best.values())
    rows, y, w = [], [], []
    for r in samples:
        pct = float(r["pct"])
        n = float(r["n"])
        if pct >= 0.99:                 # healthy: only a lower bound
            target, weight = n, 0.4
        elif pct >= 0.08:               # throttled: capacity is about n*pct
            target, weight = n * pct, 1.0
        else:                           # cut off (pocket): no information
            continue
        reach = max(c["reach_floor"], 1.0 - c["reach_falloff_per_province"] * (r.get("dist") or 0))
        rows.append([reach, (r.get("infra") or 0) * reach, r.get("rail") or 0,
                     1.0 if r.get("hub") else 0.0, min(r.get("vp") or 0, 20) * 0.05])
        y.append(target)
        w.append(weight)
    if len(rows) < min_samples:
        return {"ok": False, "error": "not enough usable samples", "samples": len(rows)}
    A = np.array(rows)
    Y = np.array(y)
    W = np.sqrt(np.array(w))
    coef, *_ = np.linalg.lstsq(A * W[:, None], Y * W, rcond=None)
    coef = np.clip(coef, 0.0, None)
    pred = A @ coef
    err = pred - Y
    out = {
        "ok": True,
        "samples": len(rows),
        "fit": {"base_divs": round(float(coef[0]), 3),
                "divs_per_infra": round(float(coef[1]), 3),
                "divs_per_rail": round(float(coef[2]), 3),
                "hub_divs": round(float(coef[3]), 3),
                "divs_per_vp": round(float(coef[4]) / 0.05, 3)},
        "residual": {"mean_abs": round(float(np.abs(err).mean()), 3),
                     "p90_abs": round(float(np.percentile(np.abs(err), 90)), 3)},
        # A fit with no infra or hub term cannot be right in a game whose whole
        # supply model is built on them; that shape means the sample set had no
        # variation, not that infrastructure does not matter.
        "trustworthy": bool(coef[1] > 0.2 and float(np.abs(err).mean()) < 2.0),
        "current": {k: c[k] for k in ("base_divs", "divs_per_infra", "divs_per_rail",
                                      "hub_divs", "divs_per_vp")},
    }
    if write:
        if not out["trustworthy"]:
            out["written"] = False
            out["refused"] = ("fit is degenerate (no infra/hub term or residual > 2 "
                              "divisions); keep the measured coefficients")
        else:
            save_cfg("supply", {k: v for k, v in out["fit"].items() if v > 0})
            out["written"] = True
    return out


def front_budget(g, enemy_tag, tag=None, margin=0.9):
    """How many divisions a front can actually hold, province by province.

    2026-11-23 (复测 7 B-2):
      1. 使用校准后的保守省补给系数；
      2. 多个边境省共享同一个州（通常同属一个 Supply Hub 供水池）时，按共享省数打折，
         杜绝 4 个相邻省把同一枢纽的总补给池重复计算 4 遍；
      3. 读取引擎实测 `army_current_supply_ratio` / `out_of_supply_days`：若某边境省此刻
         已经断补或补给率低于 healthy_pct，直接用实测 feedable 封顶，理论模型绝不盖过现实。
    """
    c_cfg = cfg()
    try:
        import movemicro
        border = movemicro._border_provinces(g, str(enemy_tag).upper())
    except Exception:
        border = []
    caps = capacity(g, tag)
    # 读一次实时负载用于实测封顶（失败不影响静态估算）
    live_load = {}
    try:
        live_load = load(g, tag) or {}
    except Exception:
        live_load = {}

    # 统计这条边境上落在同一州内的省数（共享补给池折扣）
    state_counts = {}
    for bp in border:
        st = (caps.get(bp) or {}).get("state")
        if st is not None:
            state_counts[st] = state_counts.get(st, 0) + 1

    pen = float(c_cfg.get("shared_state_penalty", 0.18))
    floor_s = float(c_cfg.get("shared_state_floor", 0.55))
    healthy = float(c_cfg.get("healthy_pct", 0.65))

    per = {}
    measured_capped = []
    for bp in border:
        c = caps.get(bp)
        if not c:
            base_cap = 2.0
        else:
            st = c.get("state")
            share_n = state_counts.get(st, 1) if st is not None else 1
            share_mult = max(floor_s, 1.0 / (1.0 + pen * max(0, share_n - 1)))
            base_cap = max(1.0, c["cap_divs"] * margin * share_mult)

        # 实测优先：若该省当前已有部队且出现断补或补给率 < healthy，绝不高于实测可养活数
        ld = live_load.get(bp)
        if ld and ld.get("n"):
            pct = ld.get("pct")
            oos = ld.get("oos_days") or 0.0
            if pct is not None and (pct < healthy or oos > 0):
                feedable = max(1.0, float(ld["n"]) * (float(pct) / max(healthy, 0.1)))
                if feedable < base_cap:
                    measured_capped.append({
                        "province": int(bp),
                        "divisions_now": int(ld["n"]),
                        "supply_pct": pct,
                        "oos_days": oos,
                        "model_cap": round(base_cap, 2),
                        "measured_cap": round(feedable, 2),
                    })
                    base_cap = feedable
        per[bp] = round(base_cap, 2)
    total = round(sum(per.values()), 2)
    return {
        "ok": bool(border),
        "enemy_tag": str(enemy_tag).upper(),
        "border_provinces": border,
        "per_province": per,
        "capacity": total,
        "margin": margin,
        "supply_model": "calibrated_hub_shared_plus_measured",
        "measured_capped_provinces": measured_capped,
    }
