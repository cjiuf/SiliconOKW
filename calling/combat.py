"""Combat analysis, using the numbers the game computes itself.

We do not re-implement HOI4's combat formula; any copy would drift from the real
game within a patch. The game already computes, every combat tick, how much damage
each side is dealing, and stores it on the object. We read that and predict the
outcome, which means terrain, equipment, doctrine, air support, the commander,
entrenchment and supply are all already accounted for.

  CLandCombat      +0x28/+0x30  CLandCombatant* (the two sides)
                   +0x38        CProvince   +0x48 CTerrainType (+0x10 name)
  CLandCombatant   +0xC8  front-line divisions (CPdxArray<CArmy*>)
                   +0xE0  reserves          +0xF8 retreating
                   +0x158 ground_damage_str +0x160 ground_damage_org
                   +0x148 air_damage_str    +0x150 air_damage_org
                   +0x138 anti_air_attack   +0x140 air_kills
  CArmy            +0x420 strength(HP) +0x428 organisation +0x430 experience
                   +0x498 days out of supply  +0x1D8 country  +0x3D0 manpower
"""
import struct
import sys

try:
    import offsets as O
except Exception:                      # direct script use
    O = None

# BUG-18 (playtest 2026-11-15): this was the ELF constant, so every read on
# hoi4.exe went to an unmapped address (`OSError [Errno 299] read ...+8`).
# `offsets.py` already resolves to the Windows table on win32.
#
# 2026-11-21 (P4-3): `getattr(O, name, None) or 0x<linux>` was a SILENT fallback.
# While both names exist on the PE build it never fired, but the day one is
# renamed/dropped the reader would quietly start using a Linux offset and return
# plausible-looking garbage.  Fail loudly instead.
def _offset_or_die(name, linux_value):
    v = getattr(O, name, None) if O is not None else None
    if v is None:
        raise RuntimeError(
            "combat.py: offsets table has no %s. Refusing to fall back to the "
            "Linux value %#x -- a wrong offset here reads as a valid-looking "
            "number, not an error (see KNOWN_ISSUES P4-3)." % (name, linux_value))
    return v


G_GAMESTATE = _offset_or_die("G_GAMESTATE", 0x459E9F0)
GS_COMBATS = _offset_or_die("GS_COMBATS", 0x268)

LC_SIDE_A, LC_SIDE_B = O.COMBAT_LC_SIDE_A, O.COMBAT_LC_SIDE_B
LC_PROVINCE, LC_TERRAIN = O.COMBAT_LC_PROVINCE, O.COMBAT_LC_TERRAIN

# ---------------------------------------------------------------- 2026-11-20
# P0 #2 ("hoi4_combat_report.count is always 0").  The battle CONTAINER was
# never the problem: CCombatManager sits at CGameState+0x260, so its
# CPdxArray<CLandCombat*> at +0x08 IS `CGameState+0x268`, the old constant, and
# the header check passes.  Every battle was thrown away one level lower, at
# CLandCombatant, because its division arrays are at different offsets here:
#
#   CLandCombat      +0x28 / +0x30  CLandCombatant*   (identical on both builds)
#                    +0x38         CProvince*
#                    +0x48         CTerrainType*
#   CLandCombatant   +0x20         CPdxArray<CArmy*>  front line   (Linux +0xC8)
#                    +0xE8         CPdxArray<CArmy*>  all divisions in the combat
#   CTerrainType     +0x18         name, MSVC std::string SSO ("mountain")
#                                                 (Linux +0x10)
#
# Measured live 2026-11-20 in four Italian-Ethiopian battles (provinces
# 7944 / 12757 / 1984 / 8062, 1v6, 1v1, 1v1, 3v6 divisions) — `_side()` used to
# read +0xC8, get an empty array on both sides and `continue` past every battle.
#
# NOT recovered: the damage-rate fields (+0x148..+0x160 on Linux).  At those
# offsets this build reads 0 or allocator garbage (one side reported
# air_org_damage 1.4e9).  Guessing them is worse than not having them, so
# DAMAGE_RATES is False on Windows and `verdict()` says "unknown" instead of
# inventing a time-to-break.  The honest live signal stays `activity()`.
_WIN = sys.platform == "win32"
if _WIN:
    TERRAIN_NAME = 0x18
    CB_FRONT, CB_ALL = 0x20, 0xE8
    CB_RESERVES = CB_RETREAT = None
    DAMAGE_RATES = False
else:
    TERRAIN_NAME = 0x10
    CB_FRONT, CB_RESERVES, CB_RETREAT = 0xC8, 0xE0, 0xF8
    CB_ALL = None
    DAMAGE_RATES = True

CB_GROUND_STR, CB_GROUND_ORG = O.COMBAT_CB_GROUND_STR, O.COMBAT_CB_GROUND_ORG
CB_AIR_STR, CB_AIR_ORG = O.COMBAT_CB_AIR_STR, O.COMBAT_CB_AIR_ORG

A_HANDLE, A_STR, A_ORG, A_XP, A_OOS, A_TAG, A_MANPOWER = (
    O.COMBAT_A_HANDLE, O.COMBAT_A_STR, O.COMBAT_A_ORG, O.COMBAT_A_XP,
    O.COMBAT_A_OOS, O.COMBAT_A_TAG, O.COMBAT_A_MANPOWER,
)
PROV_ID = O.PROV_ID


def _fx(p, a):
    return p.i64(a) / 1e5


def _divs(g, combatant, off):
    if not combatant or off is None:
        return []
    from game import _arr
    p = g.p
    try:
        return [d for d in _arr(p, combatant, off) if p.i32(d + A_HANDLE) == 51]
    except Exception:
        return []


def _side(g, combatant, tagof):
    p = g.p
    if not combatant:
        return {"tags": [], "front": 0, "reserves": None, "retreating": None,
                "in_combat": None, "org": 0.0, "hp": 0.0,
                "damage_rates_known": DAMAGE_RATES,
                "org_damage_taken_rate": None, "str_damage_taken_rate": None,
                "air_org_damage": None, "avg_org": 0.0,
                "_front_divs": [], "_reserve_divs": [], "_all": []}
    front = _divs(g, combatant, CB_FRONT)
    res = _divs(g, combatant, CB_RESERVES)
    ret = _divs(g, combatant, CB_RETREAT)
    every = _divs(g, combatant, CB_ALL)
    allv = front + res
    org = sum(_fx(p, d + A_ORG) for d in allv)
    hp = sum(_fx(p, d + A_STR) for d in allv)
    return {
        "tags": sorted({tagof.get(p.i32(d + A_TAG)) for d in allv} - {None}),
        "front": len(front),
        "reserves": len(res) if CB_RESERVES is not None else None,
        "retreating": len(ret) if CB_RETREAT is not None else None,
        "in_combat": len(every) if CB_ALL is not None else None,
        "org": round(org, 1), "hp": round(hp, 1),
        "damage_rates_known": DAMAGE_RATES,
        "org_damage_taken_rate": (round(_fx(p, combatant + CB_GROUND_ORG), 3)
                                  if DAMAGE_RATES else None),
        "str_damage_taken_rate": (round(_fx(p, combatant + CB_GROUND_STR), 3)
                                  if DAMAGE_RATES else None),
        "air_org_damage": (round(_fx(p, combatant + CB_AIR_ORG), 3)
                           if DAMAGE_RATES else None),
        "avg_org": round(org / len(allv), 1) if allv else 0.0,
        "_front_divs": front, "_reserve_divs": res, "_all": allv,
    }


def combats(g, only_mine=True, me=None):
    """Active land battles, plus a statistical prediction of the outcome.

    NOTE: `ground_damage_*` accumulates on a side's object as the damage that side
    is TAKING (dealt by the opponent). "Who is winning" is therefore answered by
    asking whose organisation pool runs out first.
    """
    from game import _arr
    p = g.p
    gs = p.u64(p.base + G_GAMESTATE)
    if sys.platform == "win32":
        # `GS_COMBATS` is still a Linux-derived guess on the PE build.  Only
        # believe it when the CPdxArray header at that offset is coherent;
        # otherwise report "no battles" instead of walking a bogus array.
        try:
            data = p.u64(gs + GS_COMBATS)
            cap = p.i32(gs + GS_COMBATS + 8)
            size = p.i32(gs + GS_COMBATS + 12)
            if not (0 <= size <= cap <= 4096) or (size and not data):
                return []
        except Exception:
            return []
    # 2026-11-20: `me` used to be hard-wired to the PLAYER tag, so
    # `hoi4_wars(tag="ITA")` filtered on Germany and reported 0 active combats
    # while Italy had four.  Callers that ask about another country pass it in.
    me = me or g.player_tag()
    tagof = {}
    for t in g.tags():
        try:
            tagof[p.i32(g.country(t).ptr + 8)] = t
        except Exception:
            pass
    out = []
    for c in _arr(p, gs, GS_COMBATS):
        try:
            a = _side(g, p.u64(c + LC_SIDE_A), tagof)
            b = _side(g, p.u64(c + LC_SIDE_B), tagof)
        except Exception:
            continue
        if not a["_all"] and not b["_all"]:
            continue
        mine = me in a["tags"] or me in b["tags"]
        if only_mine and not mine:
            continue
        us, them = (a, b) if me in a["tags"] else (b, a)
        # in battles we are not in, "us/them" just means side A/side B
        pr = p.u64(c + LC_PROVINCE)
        tt = p.u64(c + LC_TERRAIN)
        from game import item_name
        rec = {
            "combat_ptr": hex(c),
            "province": p.i32(pr + PROV_ID) if pr else None,
            "terrain": item_name(p, tt + TERRAIN_NAME) if tt else None,
            "we_are_in_it": mine,
            ("us" if mine else "side_a"): {k: v for k, v in us.items() if not k.startswith("_")},
            ("them" if mine else "side_b"): {k: v for k, v in them.items() if not k.startswith("_")},
        }
        rec.update(verdict(us, them))
        rec["_us"], rec["_them"] = us, them
        out.append(rec)
    return out


MIN_RATE = 0.05      # below this, treat the damage rate as "no data yet"


def verdict(us, them, margin=1.25, our_rate=None, their_rate=None, samples=None,
            min_samples=1, decisive_within=None):
    """Who is winning the organisation race? Time to break, in ticks.

    If `our_rate`/`their_rate` are given, the smoothed (EMA) rates are used; otherwise
    the instantaneous ones. When the damage rates are very low, or there is too little
    with too little evidence the verdict is "unknown" and the battle is LEFT ALONE.
    """
    our_loss_raw = our_rate if our_rate is not None else us["org_damage_taken_rate"]
    their_loss_raw = their_rate if their_rate is not None else them["org_damage_taken_rate"]
    width_sat = None if us.get("reserves") is None else us["reserves"] > 0
    if our_loss_raw is None or their_loss_raw is None:
        # 2026-11-20: no damage-rate field on the PE build.  Say so instead of
        # dividing by a number we made up -- a false "winning" here is exactly
        # the kind of wrong conclusion P0 was about.
        return {"ticks_until_we_break": None, "ticks_until_they_break": None,
                "org_edge": None, "hp_edge": None,
                "width_saturated": width_sat,
                "verdict": "unknown",
                "why_unknown": "damage-rate field not located on this build "
                               "(PE 1.19.5); use activity[].killed_delta"}
    if (our_loss_raw < MIN_RATE or their_loss_raw < MIN_RATE or
            (samples is not None and samples < min_samples)):
        return {"ticks_until_we_break": None, "ticks_until_they_break": None,
                "org_edge": None, "hp_edge": None,
                "width_saturated": width_sat,
                "verdict": "unknown",
                "why_unknown": ("damage rate too low (no combat data yet)"
                                if min(our_loss_raw, their_loss_raw) < MIN_RATE
                                else "not enough samples (%s)" % samples)}
    our_loss = max(our_loss_raw, 1e-6)
    their_loss = max(their_loss_raw, 1e-6)
    ttb_us = us["org"] / our_loss          # how many ticks until we break
    ttb_them = them["org"] / their_loss    # how many ticks until they break
    edge = ttb_us / ttb_them if ttb_them > 0 else float("inf")
    our_hp_ticks = us["hp"] / max(us["str_damage_taken_rate"], 1e-6)
    their_hp_ticks = them["hp"] / max(them["str_damage_taken_rate"], 1e-6)
    if edge >= margin:
        v = "winning"
    elif edge <= 1.0 / margin:
        v = "losing"
    else:
        v = "even"
    if decisive_within and ttb_us > decisive_within and ttb_them > decisive_within:
        v = "stalemate"     # neither side breaks soon -> no hasty decision
    return {
        "ticks_until_we_break": round(ttb_us, 1),
        "ticks_until_they_break": round(ttb_them, 1),
        "org_edge": round(edge, 2),
        "hp_edge": round((our_hp_ticks / their_hp_ticks) if their_hp_ticks else float("inf"), 2),
        "width_saturated": width_sat,   # reserves present means width is full
        "verdict": v,
    }


import session_state as _SS

_HIST = {}
# Baseline captured by activity() for the OTHER reader in the same request, so
# activity + active_division_combats do not consume each other's delta.
_LAST_PREV = {}
_SS.register_cache("combat._HIST", container=_HIST)
_SS.register_cache("combat._LAST_PREV", container=_LAST_PREV)


def _scan(g, tags=None):
    """One pass over every division of `tags`.

    2026-11-19: this is the ONLY division-level combat signal that works on the
    PE build.  `combats()` never validates its CGameState+0x268 array, and the
    old damage fields CArmy+0x448/+0x450 are Linux offsets that read 0.0 on every
    division even mid-battle.  What does move is CArmy+0x678 (`killed`, measured
    104 -> 157 during one Dutch battle) plus str/org.

    Returns (cur, info, maxorg, resolved_tags) where
    cur[division_ptr] = (killed, str, org).
    """
    from game import CO_ARMIES, _arr, item_name
    p = g.p
    if tags is None:
        tags = [g.player_tag()] + [t for t in enemy_tags(g)
                                   if t != g.player_tag()]
    cur, info, maxorg = {}, {}, {}
    for t in tags:
        try:
            c = g.country(t)
        except Exception:
            continue
        try:
            divs = _arr(p, c.ptr, CO_ARMIES)
        except Exception:
            continue
        for d in divs:
            try:
                killed = p.i32(d + A_KILLED)
                org = _fx(p, d + A_ORG)
                st = _fx(p, d + A_STR)
                tmpl = p.u64(d + A_TMPL)
            except Exception:
                continue
            name = None
            try:
                name = item_name(p, tmpl + 0x20) if tmpl else None
            except Exception:
                pass
            prov = None
            try:
                pr = p.u64(d + ATTACK_PROV)
                prov = p.i32(pr + PROV_ID) if pr else None
            except Exception:
                pass
            cur[d] = (killed, st, org)
            info[d] = {"tag": t, "division": hex(d), "province": prov,
                       "template": name, "killed": killed,
                       "str": round(st, 3), "org": round(org, 1),
                       "out_of_supply_days": p.i32(d + A_OOS)}
            if name:
                maxorg[name] = max(maxorg.get(name, 0.0), org)
    return cur, info, maxorg, tags


def activity(g, tags=None, limit=40):
    """Ground truth for "is anything fighting": kill / damage deltas per division.

    Samples `killed`, strength and organisation, keeps the previous sample per
    process, and reports every division whose numbers MOVED.  Pure read, no
    engine calls.  First sighting of a division only seeds the baseline -- its
    `killed` is a whole-career total (measured: 3 Belgian divisions reported
    1075 / 1069 / 1042 on their first sample, which was not this battle).
    """
    p = g.p
    prev = _HIST.get(p.pid) or {}
    cur, info, _maxorg, tags = _scan(g, tags)
    rows, new_seen = [], 0
    for d, (killed, st, org) in cur.items():
        old = prev.get(d)
        if old is None:
            new_seen += 1
            continue
        dk = killed - old[0]
        ds = st - old[1]
        do = org - old[2]
        if not dk and ds > -0.05 and do > -0.05:
            continue
        r = dict(info[d])
        r.update({"killed_delta": dk, "str_delta": round(ds, 3),
                  "org_delta": round(do, 1)})
        rows.append(r)
    _LAST_PREV[p.pid] = prev
    _HIST[p.pid] = cur
    rows.sort(key=lambda r: (-r["killed_delta"], r["org_delta"]))
    engaged = sum(1 for r in rows if r["org_delta"] < -0.05 or r["str_delta"] < -0.05)
    return {"count": len(rows), "first_sample": not prev, "baseline_only": new_seen,
            "engaged": engaged, "divisions": rows[:limit]}


def report(g):
    """Readable summary; internal pointers are dropped."""
    return [{k: v for k, v in c.items() if not k.startswith("_")} for c in combats(g)]


A_TMPL = O.COMBAT_A_TMPL
# 2026-11-19: A_STR_DAMAGE / A_ORG_DAMAGE are DEAD on the PE build -- they are
# the Linux offsets of the accumulated damage fields and read 0.0 on every
# division (verified on a battle where `killed` was moving 104 -> 157).  Kept
# only so nobody re-derives them; the live signal is A_KILLED plus str/org
# deltas, see _scan()/activity() above.
A_STR_DAMAGE, A_ORG_DAMAGE, A_KILLED = (
    O.COMBAT_A_STR_DAMAGE, O.COMBAT_A_ORG_DAMAGE, O.COMBAT_A_KILLED,
)


def active_division_combats(g, tags=None, limit=80):
    """Divisions that are actually being shot at right now.

    2026-11-19: the old version filtered on CArmy+0x448/+0x450 -- LINUX damage
    offsets.  Every division read 0.0 for them, so this "fallback" returned an
    empty list no matter what, which is why `hoi4_combat_report` said 0 battles
    while the player watched one.

    It now uses the same sampler as `activity()` and reports:
      * with a previous sample -> divisions whose organisation or strength
        DROPPED, or whose `killed` grew, since that sample (`basis: "delta"`);
      * with no previous sample -> divisions whose organisation is below the
        best organisation seen for the SAME template (`basis:
        "org_vs_template_max"`) -- the only honest thing one snapshot can say.

    This function does NOT update the history; `activity()` owns that, and the
    MCP handler calls both in one request.
    """
    p = g.p
    prepared = _LAST_PREV.pop(p.pid, None)
    prev = prepared if prepared is not None else (_HIST.get(p.pid) or {})
    cur, info, maxorg, tags = _scan(g, tags)
    out = []
    if prev:
        for d, (killed, st, org) in cur.items():
            old = prev.get(d)
            if old is None:
                continue
            dk, ds, do = killed - old[0], st - old[1], org - old[2]
            if dk <= 0 and ds > -0.05 and do > -0.05:
                continue
            r = dict(info[d])
            r["str_damage"] = round(-ds if ds < 0 else 0.0, 3)
            r["org_damage"] = round(-do if do < 0 else 0.0, 1)
            r["killed_delta"] = dk
            out.append(r)
        basis = "delta"
    else:
        for d, (killed, st, org) in cur.items():
            r = dict(info[d])
            mx = maxorg.get(r.get("template") or "", 0.0)
            if not mx or org >= mx * 0.97:
                continue
            r["str_damage"] = 0.0
            r["org_damage"] = round(mx - org, 1)
            r["killed_delta"] = None
            r["org_pct_of_max"] = round(org / mx, 2)
            out.append(r)
        basis = "org_vs_template_max"
    out.sort(key=lambda r: -(r["org_damage"] * 10 + abs(r.get("killed_delta") or 0)))
    return {"count": len(out), "tags": tags, "basis": basis,
            "divisions": out[:limit]}


ATTACK_PROV = O.COMBAT_ATTACK_PROV     # CArmy -> the CProvince it is in


def we_attack(g, rec):
    """Are we the ATTACKER here? True when our division is not in the battle province."""
    p = g.p
    prov = rec.get("province")
    for d in rec["_us"]["_all"]:
        pr = p.u64(d + ATTACK_PROV)
        if pr and p.i32(pr + PROV_ID) != prov:
            return True
    return False


def neighbours_of(g, province_id, enemy_tags):
    """ENEMY provinces adjacent to the battle province, as pinning targets."""
    import mapdata
    owners, _ = mapdata.province_owner_map(g)
    adj = mapdata.build_adjacency()
    _, ptype = mapdata.load_definitions()
    out = []
    for n in adj.get(province_id, ()):
        if ptype.get(n) == "land" and owners.get(n) in enemy_tags:
            out.append(n)
    return sorted(out)


CO_ARMIES_IN_COMBAT = O.COMBAT_CO_ARMIES_IN_COMBAT     # CCountry (from the serializer: num_armies_in_combat)


CO_DIPLOMACY_STATUS = _offset_or_die("CO_DIPLOMACY_STATUS", 0xF60)  # PE 0xF88
DS_WAR_RELATIONS    = O.COMBAT_DS_WAR_RELATIONS                     # CPdxArray<CWarRelation*>
WR_FIRST, WR_SECOND = O.COMBAT_WR_FIRST, O.COMBAT_WR_SECOND         # country ids of the two sides
WR_INSTIGATOR       = O.COMBAT_WR_INSTIGATOR                        # first_was_instigator (bool)
WR_THREAT           = O.COMBAT_WR_THREAT


def _country_id_map(g):
    """country_id -> TAG. Pure read."""
    p = g.p
    out = {}
    for t in g.tags():
        try:
            out[p.i32(g.country(t).ptr + 8)] = t
        except Exception:
            pass
    return out


# CWarRelation on the 1.19.5 PE build (vtable 0x29DCC08, sizeof 0x1D0).
# Verified live on both wars present in a 1936 save: an Italy/Ethiopia war read
# (16, 44) and a fresh German/Dominican war read (1, 73), with the second copy of
# the pair at +0x78/+0x7C agreeing on both.
WR_WIN_VTABLE = O.COMBAT_WR_WIN_VTABLE
WR_WIN_A, WR_WIN_B = O.COMBAT_WR_WIN_A, O.COMBAT_WR_WIN_B
WR_WIN_A2, WR_WIN_B2 = O.COMBAT_WR_WIN_A2, O.COMBAT_WR_WIN_B2

_WR_CACHE = {}
_SS.register_cache("combat._WR_CACHE", container=_WR_CACHE)


def _war_relation_ptrs(g):
    """Every live CWarRelation* on the PE build (cached per process).

    `oi.instances()` cannot see these objects, so match the vtable pointer
    directly in readable memory. ~131 hits, one full scan per game process.
    """
    key = (g.p.pid, g.p.base)
    if key in _WR_CACHE:
        return _WR_CACHE[key]
    needle = struct.pack("<Q", g.p.base + WR_WIN_VTABLE)
    try:
        hits = list(g.p.scan_value(needle, g.p.regions(writable=False)))
    except Exception:
        hits = []
    _WR_CACHE[key] = hits
    return hits


def _war_relations_win(g, me_tag, refresh=False):
    """Windows: read the country's own relation array (fast, always fresh).

    2026-11-19 (live): this used to rely on a full-memory vtable scan that costs
    **28 s** and was therefore cached for the whole process lifetime.  A war
    declared afterwards never entered the cache, so `hoi4_wars()` kept answering
    "not at war" while Belgium's side of the same table said GER.  The array was
    there all along:

        CCountry + CO_DIPLOMACY_STATUS (0xF88)  -> CDiplomacyStatus*
        CDiplomacyStatus + 0x20                 -> CPdxArray<CRelation*>
        each element: +0x08 relation type id (14346 = CWarRelation, 12220 =
        non-aggression, 10458 = guarantee, 12497 = subject), +0x10 / +0x14 the
        two CCountry ids.

    Verified live: GER + 0xF88 has exactly one entry, CWarRelation(1, 7), and the
    same object pointer sits in BEL's array.  The RTTI class name is the filter
    (the array also carries pacts/guarantees/subject relations), exactly like the
    ELF branch below.  The slow scan stays as a fallback only.
    """
    p = g.p
    if refresh:
        _WR_CACHE.clear()
    from game import _arr
    try:
        me = g.country(me_tag)
        me_id = p.i32(me.ptr + 8)
    except Exception:
        return []
    id2tag = _country_id_map(g)

    def _harvest(ptrs):
        got = []
        for a in ptrs:
            try:
                atk = p.i32(a + WR_WIN_A2) or p.i32(a + WR_WIN_A)
                dfd = p.i32(a + WR_WIN_B2) or p.i32(a + WR_WIN_B)
            except Exception:
                continue
            if not atk or not dfd or atk == dfd:
                continue
            if atk == me_id:
                enemy_id = dfd
            elif dfd == me_id:
                enemy_id = atk
            else:
                continue
            enemy = id2tag.get(enemy_id)
            if not enemy or enemy == me_tag:
                continue
            got.append({"enemy": enemy, "me": me_tag, "threat": None, "_ptr": a})
        return got

    # ---- fast path: the country's own relation array --------------------
    _arr_ok = False
    try:
        dip = p.u64(me.ptr + CO_DIPLOMACY_STATUS)
        arr = _arr(p, dip, DS_WAR_RELATIONS) if dip else []
        # 2026-11-21 (P1-3): a READABLE but EMPTY array is a valid answer --
        # "this country has no relation objects at all", which is the normal
        # state of a peaceful major in 1936-38.  Only a failed read justifies
        # the 28-57 s full-memory vtable scan below.  Measured: hoi4_overview's
        # BORDER_UNDEFENDED block spent 57 s in that fallback for a country at
        # peace, which is what pushed the cold call past the 50 s budget.
        _arr_ok = bool(dip)
    except Exception:
        arr = []
    wars = []
    for a in arr:
        try:
            cls = g.oi.class_of(a) or ""
        except Exception:
            cls = ""
        if cls and "War" not in cls:
            continue
        wars.append(a)
    out = _harvest(wars)
    if out or _arr_ok:
        return out

    # ---- fallback: full vtable scan (28 s, cached per process) ----------
    # 2026-11-16: `oi.instances("CWarRelation")` returns ZERO objects on
    # hoi4.exe — the ObjectIndex writable-region scan does not cover the heap
    # block these live in (same problem as CProvince). Scan for the vtable
    # ourselves instead; 131 objects found, and +0x10/+0x14 hold the two
    # country indices (verified: ITA 16 vs ETH 44, JAP 48 vs CHI 38, and
    # GER 1 vs BEL 7 / LUX 6 / HOL 8 / ENG 2 ...).
    return _harvest(_war_relation_ptrs(g))


def war_relations(g, tag=None, refresh=False):
    """Wars the country is IN: PURE READS, no injected calls.

    On Windows this scans live `CWarRelation` objects (see `_war_relations_win`);
    `refresh=True` forces the object index to be rebuilt, which is required to
    see a war declared since the last scan.

    On the ELF build: CCountry+0xF60 (CDiplomacyStatus) +0x20 -> array of
    CWarRelation*.

    The enemy is NOT read from an offset. The side fields inside CWarRelation mix
    two different id spaces (a CCountry+0x08 id in one place, an index into the
    country array in another). For countries created mid-game by a civil war the
    two diverge, which produced a wrong or empty enemy list. Object identity is
    used instead: whichever countries have the SAME CWarRelation pointer in their
    war list are the two sides. Offset-independent and exact.

    This also sees wars that have been declared but have not yet made contact,
    which `combats()` cannot.
    """
    from game import _arr
    p = g.p
    me_tag = tag or g.player_tag()
    if sys.platform == "win32":
        return _war_relations_win(g, me_tag, refresh=refresh)
    try:
        c = g.country(me_tag)
        mine = _arr(p, p.u64(c.ptr + CO_DIPLOMACY_STATUS), DS_WAR_RELATIONS)
    except Exception:
        return []
    if not mine:
        return []

    # 2026-11-04: CDiplomacyStatus+0x20 also holds non-war relations in 1.19.4.1
    # (trade agreements, opinion modifiers). Germany and Romania share a Romanian
    # oil-trade object, which is exactly how `hoi4_wartime_status` reported
    # "at war with ROM" for four years of peace. Only accept an element whose RTTI
    # class actually is a war relation. If RTTI cannot name any of them, the list
    # is only believed when the battle counter proves that fighting is happening.
    keep, unknown = [], 0
    for wr in mine:
        try:
            cls = g.oi.class_of(wr) or ""
        except Exception:
            cls = ""
        if not cls:
            unknown += 1
            keep.append(wr)
        elif "War" in cls:
            keep.append(wr)
    if unknown == len(mine):
        try:
            fighting = p.i32(c.ptr + CO_ARMIES_IN_COMBAT) > 0 or bool(combats(g, only_mine=True))
        except Exception:
            fighting = False
        if not fighting:
            return []
    mine = keep
    if not mine:
        return []
    want = set(mine)
    sides = {}
    for t in g.tags():
        try:
            d = p.u64(g.country(t).ptr + CO_DIPLOMACY_STATUS)
            for wr in _arr(p, d, DS_WAR_RELATIONS):
                if wr in want:
                    sides.setdefault(wr, set()).add(t)
        except Exception:
            continue
    out = []
    for wr in mine:
        others = sorted(sides.get(wr, set()) - {me_tag})
        if not others:
            continue
        try:
            threat = round(p.i64(wr + WR_THREAT) / 1e5, 3)
        except Exception:
            threat = None
        for enemy in others:
            out.append({"enemy": enemy, "me": me_tag, "threat": threat, "_ptr": wr})
    if out:
        return out
    # FALLBACK: in 1.19.4.1 the diplomacy records of a country that is AT WAR can
    # be empty, so the enemy cannot be found that way. Find it from the armies
    # instead: every CArmy carries its owner's country pointer, and the armies
    # that are in combat belong to the enemy.
    return _enemies_from_armies(g, me_tag)


def _enemies_from_armies(g, me_tag):
    """Enemy tags of the countries whose armies are FIGHTING OURS.

    `num_armies_in_combat` is a WORLD counter, so it cannot be used on its own.
    The reliable signal is the battle list: a CLandCombat has two sides, and
    each side's divisions carry their owner's country pointer. A battle with one
    of our divisions on one side and theirs on the other names the enemy.
    """
    p = g.p
    A_COUNTRY = 0x1D8
    try:
        from game import _arr
        from game import item_name
    except Exception:
        return []
    # tag for a country pointer
    ptr2tag = {}
    for t in g.tags():
        try:
            ptr2tag[g.country(t).ptr] = t
        except Exception:
            continue
    enemies = []
    try:
        recs = combats(g, only_mine=True)
    except Exception:
        recs = []
    for rec in recs:
        try:
            sides = rec.get("_us", {}), rec.get("_them", {})
        except Exception:
            continue
        for side in sides:
            for d in side.get("_all", []):
                cp = p.u64(d + A_COUNTRY)
                tag = ptr2tag.get(cp)
                if tag and tag != me_tag and tag not in enemies:
                    enemies.append(tag)
    if enemies:
        return [{"enemy": t, "me": me_tag, "threat": None, "_ptr": None} for t in enemies]
    # The battle list can be empty (CGameState+combats is unreliable in some
    # saves), so go by geography: the enemy is the country that owns armies in
    # the provinces where OUR armies are.
    try:
        from game import _arr
        our_provs = {}
        for a in _arr(p, g.country(me_tag).ptr, 0x290):
            try:
                pr = p.u64(a + 0x1F0)
                if pr:
                    our_provs.setdefault(p.i32(pr + 0xA4), []).append(a)
            except Exception:
                continue
        if not our_provs:
            return []
        for t in g.tags():
            if t == me_tag:
                continue
            try:
                c = g.country(t)
                armies = _arr(p, c.ptr, 0x290)
            except Exception:
                continue
            if not armies:
                continue
            for a in armies:
                try:
                    pr = p.u64(a + 0x1F0)
                    if pr and p.i32(pr + 0xA4) in our_provs:
                        enemies.append(t)
                        break
                except Exception:
                    continue
    except Exception:
        pass
    if enemies:
        return [{"enemy": t, "me": me_tag, "threat": None, "_ptr": None} for t in enemies]
    # Fall back to the world counter: any country with armies in combat.
    try:
        from game import _arr
        mine = {p.u64(a + A_COUNTRY)
                for a in _arr(p, g.country(me_tag).ptr, 0x290)}
    except Exception:
        return []
    for t in g.tags():
        if t == me_tag:
            continue
        try:
            c = g.country(t)
            if p.i32(c.ptr + 0x12F0) <= 0:
                continue
            armies = _arr(p, c.ptr, 0x290)
        except Exception:
            continue
        for a in armies:
            if p.u64(a + A_COUNTRY) in mine:
                enemies.append(t)
                break
    return [{"enemy": t, "me": me_tag, "threat": None, "_ptr": None} for t in enemies]


def at_war(g, tag=None):
    """PURE-READ war detection.

    In order: (1) war relations from the diplomacy records, which also catch wars
    declared but not yet fought; (2) num_armies_in_combat; (3) the active battle list.

    DO NOT use `g.wars()` here: it goes through `country_stats()`, which stops every
    game thread and runs ~45 functions inside the process. Called from a loop it
    FREEZES the game (see FINDINGS, the freeze post-mortem).
    """
    p = g.p
    try:
        if war_relations(g, tag):
            return True
    except Exception:
        pass
    if sys.platform != "win32":
        try:
            c = g.country(tag) if tag else g.player()
            if p.i32(c.ptr + CO_ARMIES_IN_COMBAT) > 0:
                return True
        except Exception:
            pass
    try:
        return bool(combats(g, only_mine=True))
    except Exception:
        return False


def enemy_tags(g, tag=None, refresh=False):
    """Tags of the countries we are at war with: pure reads."""
    return sorted({w["enemy"] for w in war_relations(g, tag, refresh=refresh)})
