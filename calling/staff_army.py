"""Army policy: how many divisions the country can actually field, and the
"换编" (re-organisation) that keeps the army matching that number.

Division cap = min(year cap, equipment-backed, manpower-backed, supply-backed),
exactly as agreed in the design interview. Each of the four is reported with its
own number so the binding constraint is never a mystery.

Equipment needs come from the game's own subunit definitions
(common/units/*.txt: `need = { infantry_equipment = 100 }`, `manpower = 1000`),
not from a wiki.

Destructive actions -- disbanding divisions, editing a template -- are only ever
*proposed* unless `allow_destructive` is true and shadow is off.
"""
import json
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
CFG_PATH = os.path.join(HERE, "staff_config.json")

DEFAULTS = {
    "enabled": True,
    "shadow": True,
    "year_caps": {"1936": 30, "1937": 42, "1938": 60, "1939": 84, "1940": 120},
    "divs_per_army": 24,
    "min_army": 4,
    "reserve_equipment_days": 30,
    "allow_destructive": False,
    "disband_below_strength": 0.55,
    "retemplate_target": "Infanterie-Division",
    "retemplate_min_xp": 50.0,
    "retemplate_min_artillery": 1500,
    "train_batch": 3,
    "train_min_gap": 4,
}

_STATIC = {}


def cfg():
    out = dict(DEFAULTS)
    try:
        with open(CFG_PATH, encoding="utf-8") as f:
            doc = json.load(f)
        out.update(doc.get("army") or {})
    except Exception:
        pass
    return out


def _game_dir():
    import config as _c
    return _c.game_dir()


def subunits():
    """subunit name -> {need: {equipment: n}, manpower: n}, from common/units."""
    if "subunits" in _STATIC:
        return _STATIC["subunits"]
    out = {}
    d = os.path.join(_game_dir(), "common", "units")
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
        # each subunit block: <name> = { ... need = { ... } ... manpower = N ... }
        for m in re.finditer(r"\n\t([a-z_0-9]+)\s*=\s*\{", txt):
            name = m.group(1)
            start = m.end()
            depth, i = 1, start
            while i < len(txt) and depth:
                if txt[i] == "{":
                    depth += 1
                elif txt[i] == "}":
                    depth -= 1
                i += 1
            body = txt[start:i]
            need = {}
            nm = re.search(r"need\s*=\s*\{([^}]*)\}", body)
            if nm:
                for eq, n in re.findall(r"([a-z_0-9]+)\s*=\s*(\d+)", nm.group(1)):
                    need[eq] = need.get(eq, 0) + int(n)
            mp = re.search(r"(?<!max_)\bmanpower\s*=\s*(\d+)", body)
            ms = re.search(r"\bmax_strength\s*=\s*([\d.]+)", body)
            if need or mp:
                out[name] = {"need": need, "manpower": int(mp.group(1)) if mp else 0,
                             "max_strength": float(ms.group(1)) if ms else 0.0}
    _STATIC["subunits"] = out
    return out


def template_needs(comp, subs=None):
    """One division of this template: equipment, manpower, and max strength."""
    subs = subs or subunits()
    need, mp, mstr = {}, 0, 0.0
    for group in ("line", "support"):
        for name, n in (comp.get(group) or {}).items():
            s = subs.get(name)
            if not s:
                continue
            for eq, v in s["need"].items():
                need[eq] = need.get(eq, 0) + v * n
            mp += s["manpower"] * n
            mstr += s.get("max_strength", 0.0) * n
    return {"equipment": need, "manpower": mp, "max_strength": mstr}


def state(g, tag=None):
    """Everything the army decision needs, read once."""
    out = {"divisions": None, "templates": [], "stockpile": {}, "output": {},
           "manpower": None, "armies": None, "unassigned": None,
           "divisions_read": True}
    try:
        out["divisions"] = g.division_detail(tag, limit=400)["divisions"]
    except Exception:
        out["divisions"] = []
        # 2026-11-12: this used to be indistinguishable from "the country has no
        # army". Measured live: 225 divisions on the map, cap 120, staff still
        # training 6 a tick because this read had failed and n was 0.
        out["divisions_read"] = False
    try:
        comps = g.template_composition(tag) or []
        out["templates"] = comps
    except Exception:
        out["templates"] = []
    try:
        raw = (g.equipment_stockpile(tag) or {}).get("stockpile") or []
        # equipment_stockpile returns [{"equipment": name, "count": n}, ...]
        for row in raw:
            if isinstance(row, dict):
                eq = _family(row.get("equipment"))
                if eq:
                    out["stockpile"][eq] = out["stockpile"].get(eq, 0.0) + float(row.get("count") or 0)
    except Exception:
        out["stockpile"] = {}
    try:
        for l in g.production_lines(tag) or []:
            eq = l.get("equipment")
            if eq:
                out["output"][eq] = out["output"].get(eq, 0.0) + float(l.get("output_per_day") or 0)
    except Exception:
        pass
    try:
        out["manpower"] = (g.country_stats(tag) or {}).get("manpower")
    except Exception:
        out["manpower"] = None
    try:
        out["armies"] = g.order_groups(tag)
    except Exception:
        out["armies"] = None
    try:
        out["unassigned"] = len(g.unassigned_divisions(tag))
    except Exception:
        out["unassigned"] = None
    # A conveyor line is one division that has been paid for but is not on the
    # map yet. Counting only deployed divisions made the staff open a fresh
    # conveyor every tick while production crawled: measured 1936-11-24, 14
    # conveyors for a 30-division army, i.e. 42 divisions ordered against a cap
    # of 36. Training must be measured against fielded + in training.
    out["in_training"] = 0
    try:
        for conv in g.conveyors(tag) or []:
            out["in_training"] += int(conv.get("lines") or 0)
    except Exception:
        pass
    return out


def _year_cap(c, year):
    caps = c["year_caps"]
    years = sorted(int(y) for y in caps)
    pick = years[0]
    for y in years:
        if year >= y:
            pick = y
    return caps[str(pick)]


def _family(eq):
    for fam in ("infantry_equipment", "support_equipment", "artillery_equipment",
                "anti_tank_equipment", "anti_air_equipment", "fighter_equipment"):
        if eq and eq.startswith(fam):
            return fam
    return eq


def caps(g, tag=None, snap=None):
    c = cfg()
    snap = snap or state(g, tag)
    try:
        year = g.date().get("year", 1936)
    except Exception:
        year = 1936
    subs = subunits()
    # the template the army is actually built from: the most numerous one
    counts = {}
    for d in snap.get("divisions") or []:
        t = d.get("template")
        if t:
            counts[t] = counts.get(t, 0) + 1
    main_tmpl = max(counts, key=counts.get) if counts else None
    comp = next((t for t in snap.get("templates") or [] if t.get("name") == main_tmpl), None)
    if comp is None:
        # No division is on the map, so there is no "most numerous template" to
        # read the needs from. Fall back to the template the staff is meant to
        # build -- measured 1947-03: 0 divisions left need_per_division empty,
        # every cap but the year cap unknown, and the army never rebuilt.
        want = c.get("retemplate_target")
        tl = snap.get("templates") or []
        comp = (next((t for t in tl if t.get("name") == want), None)
                or next((t for t in tl if (t.get("line") or {}).get("infantry")), None))
        if comp is not None:
            main_tmpl = comp.get("name")
    needs = template_needs(comp, subs) if comp else {"equipment": {}, "manpower": 0}

    # equipment: stock + N days of output, divided by the per-division requirement
    # Equipment-backed cap: what the army could ADD, not what it would take to
    # equip it from nothing. The divisions already in the field are wearing their
    # equipment; the stockpile only has to cover new ones plus attrition. Ten
    # percent of the army's kit is held back as the attrition reserve (a division
    # that loses its rifles is reforged from the stockpile, not from production).
    days = c["reserve_equipment_days"]
    n_now = len(snap.get("divisions") or [])
    eq_caps = {}
    stock = {_family(k): v for k, v in (snap.get("stockpile") or {}).items()}
    out30 = {_family(k): v * days for k, v in (snap.get("output") or {}).items()}
    for eq, per in (needs["equipment"] or {}).items():
        fam = _family(eq)
        if not per:
            continue
        have = float(stock.get(fam) or 0) + float(out30.get(fam) or 0)
        reserve = 0.10 * n_now * per
        spare = max(0.0, have - reserve)
        eq_caps[fam] = int(n_now + spare // per)
    equipment_cap = min(eq_caps.values()) if eq_caps else None

    manpower_cap = None
    if snap.get("manpower") and needs["manpower"]:
        manpower_cap = int(snap["manpower"] // needs["manpower"])

    supply_cap = None
    try:
        import staff_supply as ss
        rep = ss.plan(g, tag)
        feedable = 0.0
        for st in rep.get("states") or []:
            if st.get("feedable"):
                feedable += st["feedable"]
        # in peacetime the army is spread over the whole country; only the
        # states we actually hold count, and the front is where it fights
        if feedable <= 0:
            # Nothing is measured when no division stands on the map, and the
            # measured loop then reports zero supply: 0 divisions -> 0 supply ->
            # cap 0 -> never train. That deadlock kept the army at zero from
            # 1944-08 to 1947-03. With no measurement the model is the only
            # prior, which is exactly what it is documented to be.
            model = ss.capacity(g, tag) or {}
            feedable = sum(float(v.get("cap_divs") or 0.0) for v in model.values())
        supply_cap = int(round(feedable))
    except Exception:
        supply_cap = None

    year_cap = _year_cap(c, year)
    parts = {"year": year_cap, "equipment": equipment_cap,
             "manpower": manpower_cap, "supply": supply_cap}
    binding = min((v, k) for k, v in parts.items() if v is not None)[1]
    return {"year": year, "caps": parts, "cap": min(v for v in parts.values() if v is not None),
            "binding": binding, "template": main_tmpl,
            "template_counts": counts, "need_per_division": needs,
            "equipment_caps": eq_caps}


def plan(g, tag=None):
    c = cfg()
    snap = state(g, tag)
    capinfo = caps(g, tag, snap)
    divs = snap.get("divisions") or []
    armies_obj = snap.get("armies")
    army_groups = ((armies_obj.get("groups") if isinstance(armies_obj, dict) else armies_obj)
                   or [])
    if snap.get("divisions_read", True) and not divs and army_groups:
        # armies exist but the division list came back empty: the read is broken,
        # not the army. Training on this is what produced 108 phantom divisions.
        snap["divisions_read"] = False
    if not snap.get("divisions_read", True):
        return {"ok": False, "actions": [], "caps": capinfo,
                "cap": capinfo.get("cap"), "binding": capinfo.get("binding"),
                "template": capinfo.get("template"), "divisions": None,
                "problems": ["division list unreadable this tick - refusing to train "
                             "(a failed read must never look like an empty army)"]}
    n = len(divs)
    in_training = int(snap.get("in_training") or 0)
    total = n + in_training
    cap = capinfo["cap"]
    actions = []
    if cap - total >= c["train_min_gap"]:
        actions.append({"action": "train", "count": min(c["train_batch"], cap - total),
                        "template": capinfo["template"],
                        "why": "cap %d > fielded %d + training %d" % (cap, n, in_training)})
    if n - cap > 0:
        tstr = {}
        for t in snap.get("templates") or []:
            tstr[t.get("name")] = template_needs(t, None).get("max_strength") or 0.0
        def _share(d):
            m = tstr.get(d.get("template")) or 0.0
            if not m:
                return 1.0
            return (d.get("strength") or 0) / m
        weak = sorted(divs, key=_share)[: int(n - cap)]
        actions.append({"action": "disband", "count": len(weak),
                        "divisions": [{"ptr": d.get("ptr"), "strength": d.get("strength"),
                                       "template": d.get("template")} for d in weak],
                        "why": "divisions %d > cap %d (binding: %s)" % (n, cap, capinfo["binding"]),
                        "destructive": True})
    # 换编 1/3: retemplate when the standard template is below 9 inf + 3 art and we can pay
    tgt = next((t for t in snap.get("templates") or [] if t.get("name") == c["retemplate_target"]), None)
    if tgt:
        line = dict(tgt.get("line") or {})
        sup = dict(tgt.get("support") or {})
        # "9步3炮" = nine infantry BATTALIONS plus three LINE-artillery BATTALIONS.
        # subunit_catalog.json: artillery_brigade support=False group=combat_support
        #                       artillery         support=True  group=support
        # Writing "artillery" into the line array is rejected by templedit.write_arr
        # ("not a line subunit"), and the read-back check below could never see a
        # key that does not exist -> a retemplate proposal every single tick.
        want_line = {"infantry": 9, "artillery_brigade": 3}
        if line.get("infantry", 0) < 9 or line.get("artillery_brigade", 0) < 3:
            actions.append({"action": "retemplate", "template": tgt.get("name"),
                            "from": {"line": line, "support": sup},
                            "to": {"line": want_line, "support": sup},
                            "why": "line is %s; the standard is 9 infantry + 3 artillery_brigade"
                                   % (line,),
                            "destructive": True})
    # 换编 3/3: armies of divs_per_army
    per = c["divs_per_army"]
    armies = (snap.get("armies") or {}).get("count") if isinstance(snap.get("armies"), dict) else None
    unassigned = snap.get("unassigned")
    if unassigned:
        groups = []
        left = unassigned
        while left > 0:
            take = min(per, left)
            if take < c["min_army"] and groups:
                groups[-1] += take
            else:
                groups.append(take)
            left -= take
        actions.append({"action": "regroup", "armies": groups, "unassigned": unassigned,
                        # CCommand::Run only queues: a second create_army in the same
                        # paused tick takes the SAME divisions again and does nothing.
                        "one_per_tick": True,
                        "why": "%d divisions are in no army" % unassigned})
    destructive = [a for a in actions if a.get("destructive")]
    allowed = (not c["shadow"]) and c["allow_destructive"]
    out = {"ok": True, "mode": "shadow" if c["shadow"] else "live",
           "divisions": n, "in_training": in_training, "committed": total,
           "cap": capinfo["cap"], "caps": capinfo["caps"],
           "binding": capinfo["binding"], "template": capinfo["template"],
           "need_per_division": capinfo["need_per_division"],
           "actions": actions,
           "destructive_blocked": [a for a in destructive if not allowed],
           "problems": ([("division cap is bound by %s at %d, army has %d"
                          % (capinfo["binding"], cap, n))] if cap < n else [])}
    return out


def apply(g, tag=None, plan_doc=None, allow_destructive=None):
    """Carry out the safe part of the plan.

    Low risk only: regroup (create one army per tick) and train. Disbanding and
    template edits stay proposals unless allow_destructive is explicitly true.
    """
    c = cfg()
    plan_doc = plan_doc or plan(g, tag)
    allow = c["allow_destructive"] if allow_destructive is None else bool(allow_destructive)
    out = {"ok": True, "done": [], "skipped": []}
    regrouped = False
    for a in plan_doc.get("actions") or []:
        kind = a.get("action")
        if a.get("destructive") and not allow:
            out["skipped"].append({"action": kind, "reason": "destructive, needs allow_destructive"})
            continue
        try:
            if kind == "regroup":
                if regrouped:
                    out["skipped"].append({"action": "regroup",
                                           "reason": "one army per tick"})
                    continue
                r = g.create_army(None, a["armies"][0], tag)
                regrouped = True
                out["done"].append({"action": "regroup", "army_size": a["armies"][0], "result": r})
                # one per tick: CCommand::Run only queues, a second call in the
                # same tick would take the same divisions again — but the loop
                # must NOT break, or a later train/retemplate is swallowed.
            elif kind == "retemplate":
                # plan() has generated this action since 2026-11-11; apply() never
                # handled it, so the 9-inf/3-art conversion silently did nothing.
                import templedit
                pick = None
                for t in g.division_templates(tag):
                    if t.get("name") == a.get("template"):
                        pick = t
                        break
                if not pick:
                    out["skipped"].append({"action": kind, "reason": "template not found"})
                    continue
                r = templedit.edit_template(g, int(pick["ptr"], 16),
                                            a["to"].get("line"), False,
                                            a["to"].get("support"))
                out["done"].append({"action": "retemplate", "result": r})
            if kind == "train":
                r = g.train_divisions(a.get("template"), a.get("count") or 1, None, tag)
                out["done"].append({"action": "train", "result": r})
            elif kind == "disband":
                # no verified disband command exists yet; never pretend
                out["skipped"].append({"action": "disband",
                                       "reason": "no verified disband command in this build"})
        except Exception as e:
            out["ok"] = False
            out["done"].append({"action": kind, "error": repr(e)})
    return out
