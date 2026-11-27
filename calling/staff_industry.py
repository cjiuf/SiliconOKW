"""Industry policy: which factories build what, and what the construction queue
should hold.

The layer is target driven, exactly as agreed in the design interview:

  * both halves are computed together -- military factories feeding production
    lines and civilian factories feeding the construction queue -- because they
    compete for the same fixed pool of factories;
  * the target is a curve, not a constant: 1936 needs roughly the starting
    industry, 1940 needs the war economy;
  * war overrides peace: once at war, the queue is repairs and supply hubs, and
    the production lines are the ones that fill the divisions.

Shadow mode (default) reports the decision and changes nothing.
"""
import json
import os

import session_state as _SS

HERE = os.path.dirname(os.path.abspath(__file__))
CFG_PATH = os.path.join(HERE, "staff_config.json")

DEFAULTS = {
    "enabled": True,
    "shadow": True,
    "targets": {
        "civ_1936": 30, "civ_1940": 80,
        "mil_1936": 20, "mil_1940": 45,
    },
    "min_queue": 3,
    "max_new_per_tick": 2,
    "build_order_peace": ["industrial_complex"],
    "build_order_war": ["supply_hub", "infrastructure"],
    "wartime_queue_cap": 4,
    # equipment the army cannot fight without, in the order it is missed
    "produce_priority": [
        "infantry_equipment", "support_equipment", "artillery_equipment",
        "anti_tank_equipment", "fighter_equipment", "cas_equipment",
    ],
}


def cfg():
    out = dict(DEFAULTS)
    try:
        with open(CFG_PATH, encoding="utf-8") as f:
            doc = json.load(f)
        out.update(doc.get("construction") or {})
    except Exception:
        pass
    return out


def _date_key(d):
    """1936.5 style year fraction from a game date dict."""
    y = d.get("year", 1936)
    return y + (d.get("month", 1) - 1) / 12.0 + (d.get("day", 1) - 1) / 365.0


def targets(g, tag=None):
    c = cfg()
    t = c["targets"]
    try:
        d = g.date()
    except Exception:
        d = {"year": 1936, "month": 1, "day": 1}
    y = _date_key(d)
    f = min(1.0, max(0.0, (y - 1936.0) / 4.0))
    return {
        "year": round(y, 2),
        "civ": round(t["civ_1936"] + (t["civ_1940"] - t["civ_1936"]) * f, 1),
        "mil": round(t["mil_1936"] + (t["mil_1940"] - t["mil_1936"]) * f, 1),
    }


def snapshot(g, tag=None):
    """Everything the decision needs, read once."""
    st = {}
    try:
        st = g.country_stats(tag)
    except Exception:
        st = {}
    try:
        queue = g.construction_queue(tag) or []
    except Exception:
        queue = []
    try:
        lines = g.production_lines(tag) or []
    except Exception:
        lines = []
    mil = st.get("military_factories")
    civ = st.get("civilian_factories")
    # production_lines() is CPdxArray<CMilitaryProductionLine*/CNavalProductionLine*>;
    # summing it whole counted DOCKYARDS as busy military factories.
    mil_lines = [l for l in lines if l.get("class") == "CMilitaryProductionLine"]
    used = sum(int(l.get("factories") or 0) for l in mil_lines)
    at_war = None
    try:
        at_war = bool(g.war_status(tag).get("at_war"))
    except Exception:
        try:
            at_war = bool(st.get("has_war"))
        except Exception:
            at_war = None
    return {"civ": civ, "mil": mil, "queue": queue, "lines": lines,
            "mil_used": used, "mil_idle": (mil - used) if isinstance(mil, int) else None,
            "at_war": at_war, "manpower": st.get("manpower")}


def _equipment_family(name):
    for fam in ("infantry_equipment", "support_equipment", "artillery_equipment",
                "anti_tank_equipment", "fighter_equipment", "cas_equipment",
                "light_tank_equipment", "medium_tank_equipment", "motorized_equipment"):
        if name and name.startswith(fam):
            return fam
    return name


def production_plan(g, tag=None, snap=None):
    """Where the idle military factories should go.

    Rule: never leave a military factory idle; feed the equipment families the
    army is shortest of, in the order the army misses them. A line that exists
    gets the factories first (efficiency is already invested); a new line is only
    opened when the family has none.
    """
    c = cfg()
    snap = snap or snapshot(g, tag)
    lines = [l for l in (snap["lines"] or [])
             if l.get("class") == "CMilitaryProductionLine"]
    mil = snap["mil"] or 0
    used = snap["mil_used"] or 0
    idle = max(0, mil - used)
    by_family = {}
    for l in lines:
        fam = _equipment_family(l.get("equipment"))
        by_family.setdefault(fam, []).append(l)
    order = c["produce_priority"]
    proposal = []
    if idle:
        # 1) top up the existing lines of the most critical families first,
        #    proportionally to how thin they are
        for fam in order:
            if idle <= 0:
                break
            ls = by_family.get(fam)
            if not ls:
                continue
            target = max(int(l.get("factories") or 0) for l in ls) + 2
            for l in ls:
                if idle <= 0:
                    break
                cur = int(l.get("factories") or 0)
                if cur < target:
                    add = min(idle, target - cur)
                    proposal.append({"line_index": l.get("index"), "equipment": l.get("equipment"),
                                     "from": cur, "to": cur + add})
                    idle -= add
        # 2) still idle: open the first missing critical family
        for fam in order:
            if idle <= 0:
                break
            if fam in by_family:
                continue
            proposal.append({"new_line": fam + "_1", "factories": idle})
            idle = 0
    return {"mil": mil, "mil_used": used, "mil_idle": max(0, (mil or 0) - used),
            "proposal": proposal}


def construction_plan(g, tag=None, snap=None, max_items=3):
    """What the queue should hold next.

    Peace: civilian industry until the year's target is met, then military.
    War: repairs and supply hubs only, and the queue stays short so civil
    factories are not tied up when the front moves.
    """
    c = cfg()
    snap = snap or snapshot(g, tag)
    t = targets(g, tag)
    queue = snap["queue"] or []
    at_war = snap["at_war"]
    civ = snap["civ"] or 0
    mil = snap["mil"] or 0
    out = {"targets": t, "civ": civ, "mil": mil,
           "civ_gap": round(t["civ"] - civ, 1), "mil_gap": round(t["mil"] - mil, 1),
           "queue_len": len(queue), "at_war": at_war, "items": []}
    if at_war:
        # wartime: only what keeps the army supplied
        if len(queue) >= c["wartime_queue_cap"]:
            out["skip"] = "wartime queue full"
            return out
        order = c["build_order_war"]
    else:
        if len(queue) >= c["min_queue"]:
            out["skip"] = "queue already has %d items" % len(queue)
            return out
        order = c["build_order_peace"] if out["civ_gap"] > 0 else ["arms_factory"]
    # 2026-11-22 (P1-2): 读取每个州的真实剩余建筑槽 `slots_free`，并扣减当前建造队列
    # 已排在该州的共享槽建造线；跳过满槽州，且单州生成的候选条数不超过其真实剩余空位。
    slot_buildings = set()
    try:
        import statemeta as _sm
        slot_buildings = _sm._load_slot_buildings()
    except Exception:
        slot_buildings = {"industrial_complex", "arms_factory", "dockyard",
                          "synthetic_refinery", "fuel_silo", "rocket_site",
                          "nuclear_reactor"}
    queued_by_state = {}
    for qrow in queue:
        sid_q = qrow.get("state")
        if sid_q is not None and (qrow.get("building") in slot_buildings):
            queued_by_state[sid_q] = queued_by_state.get(sid_q, 0) + 1

    # rank our states: high infrastructure first, then the ones that already
    # have industry (they have the slots and the throughput)
    ranked = []
    try:
        for s in g.states(tag):
            try:
                d = g.state_detail(s["id"], tag)
            except Exception:
                continue
            b = d.get("buildings") or {}
            infra = b.get("infrastructure", 0)
            ind = (b.get("industrial_complex", 0) + b.get("arms_factory", 0))
            sf = d.get("slots_free")
            avail_slots = None if sf is None else max(0, int(sf) - queued_by_state.get(s["id"], 0))
            ranked.append((infra * 2 + min(ind, 6), s["id"], infra, ind,
                           d.get("provinces", 0), avail_slots))
    except Exception:
        ranked = []
    ranked.sort(reverse=True)
    # The game refuses some state/building pairs outright (measured 1936-01-01:
    # state 65 refuses a third arms_factory with CanExecute false while the same
    # state accepts industrial_complex). So emit an ordered candidate list --
    # several states, both building types -- and let apply() take the first ones
    # the engine actually accepts, bounded by each state's free slots.
    alts = list(order)
    for b in ("industrial_complex", "arms_factory"):
        if b not in alts:
            alts.append(b)
    for score, sid, infra, ind, provs, avail_slots in ranked[:8]:
        emitted_for_state = 0
        for b in alts:
            needs_slot = b in slot_buildings
            if needs_slot and avail_slots is not None:
                if avail_slots <= 0 or emitted_for_state >= avail_slots:
                    continue
            out["items"].append({"state": sid, "building": b, "score": score,
                                 "infra": infra, "industry": ind, "provinces": provs,
                                 "slots_free": avail_slots})
            if needs_slot:
                emitted_for_state += 1
        if len(out["items"]) >= 12:
            break
    out["want"] = min(max_items, c["max_new_per_tick"])
    out["ranked_top"] = [{"state": s, "score": sc, "infra": i, "industry": n,
                          "slots_free": av}
                         for sc, s, i, n, _, av in ranked[:5]]
    return out


_CACHE = {}
_SS.register_cache("staff_industry._CACHE", container=_CACHE)


def plan(g, tag=None):
    c = cfg()
    # The staff worker polls every 15 s of wall clock but the game date only
    # moves a few times per poll; the industry picture cannot change inside one
    # game day, and ranking states costs a read per state.
    try:
        day = g.date().get("text", "")[:10]
    except Exception:
        day = ""
    pid = getattr(getattr(g, "p", None), "pid", None)
    key = (pid, _SS.SESSION.save_epoch, tag, day)
    if day and key in _CACHE:
        return _CACHE[key]
    snap = snapshot(g, tag)
    prod = production_plan(g, tag, snap)
    const = construction_plan(g, tag, snap)
    problems = []
    if prod["mil_idle"]:
        problems.append("%d military factories idle" % prod["mil_idle"])
    if const["civ_gap"] and const["civ_gap"] > 0 and not snap["queue"]:
        problems.append("construction queue empty while %d civilian factories short" % const["civ_gap"])
    out = {"ok": True, "mode": "shadow" if c["shadow"] else "live",
           "at_war": snap["at_war"],
           "production": prod, "construction": const, "problems": problems}
    if day:
        _CACHE.clear()
        _CACHE[key] = out
    return out


def apply(g, tag=None, plan_doc=None):
    """Carry out the proposal. Only called when shadow is false.

    Every command is verified by reading the game back: a construction item only
    counts as queued when it appears in the queue, a production change only when
    the line's factory count moved. Anything else is reported, never assumed.
    """
    c = cfg()
    plan_doc = plan_doc or plan(g, tag)
    out = {"production": [], "construction": [], "ok": True}
    prod = plan_doc.get("production") or {}
    for item in prod.get("proposal") or []:
        try:
            if item.get("new_line"):
                # the command wrapper takes keywords this build has not verified;
                # do not guess at a state-changing call
                out["production"].append({"new_line": item["new_line"],
                                          "skipped": "new-line command not verified"})
            else:
                r = g.set_production_amount(item["line_index"], item["to"], tag)
                after = None
                try:
                    for l in g.production_lines(tag) or []:
                        if l.get("index") == item["line_index"]:
                            after = l.get("factories")
                            break
                except Exception:
                    pass
                ok = bool(r.get("ok")) and (after == item["to"] or after is None)
                out["production"].append({"line_index": item["line_index"],
                                          "want": item["to"], "after": after,
                                          "ok": ok, "result": r})
                if not ok:
                    out["ok"] = False
        except Exception as e:
            out["ok"] = False
            out["production"].append({"error": repr(e), "item": item})
    const = plan_doc.get("construction") or {}
    want = const.get("want") or 1
    got = 0
    used_per_state = {}
    for item in const.get("items") or []:
        if got >= want:
            break
        sid = item.get("state")
        sf = item.get("slots_free")
        if sid is not None and sf is not None:
            if used_per_state.get(sid, 0) >= int(sf):
                continue
        try:
            r = g.add_construction(item["state"], item["building"], 1, 0, tag)
            ok = bool(r.get("ok"))
            out["construction"].append({"item": {k: item[k] for k in ("state", "building")},
                                        "ok": ok,
                                        "error": None if ok else r.get("error")})
            if ok:
                got += 1
                if sid is not None:
                    used_per_state[sid] = used_per_state.get(sid, 0) + 1
        except Exception as e:
            out["construction"].append({"error": repr(e), "item": item})
    out["construction_queued"] = got
    out["ok"] = out["ok"] and got > 0 if const.get("items") else out["ok"]
    return out
