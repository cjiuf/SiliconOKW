"""German air-wing resolution + attach-to-army orders (HOI4 1.19.4.1).

The 2026-11-09 brief proposed
    CAirWing+0x30 -> CAirWingPool+0x60 -> CCountryAirContainer+0x10 == 1
as the ownership chain.  Measured on a fresh 1936 game (217 wings, 182 with a
pool):

  * only ONE wing has a non-null CAirWingPool+0x60;
  * 1083 of 1116 CCountryAirContainer objects report +0x10 == 1,
    so the value is not a country id (Germany's tag_id is 1, but so is almost
    everything else's).

What does hold: CAirWingPool+0x40 is a per-country CStrategicAir.  217 wings
resolve to 44 distinct CStrategicAir objects, i.e. one air force per country
with wings.  Germany's air force is picked out by its OOB signature, which
matches the in-game "Air Forces Overview" exactly (15 wings: 7 tac, 5 fighter,
1 cas, 1 nav, 1 transport).
"""
import struct

WING_VT = 0x440E200
ATTACH_CTOR = 0x241F840   # CAttachAirWingToArmyCommand(this, CAirWing*, COrdersGroup*, 0)
ATTACH_SIZE = 0x48
RUN_CMD = 0x20AE0A0

# 1936 German air OOB, as shown in the Air Forces Overview panel.
GER_1936_SIG = {"tac_bomber": 7, "fighter": 5, "cas": 1, "nav_bomber": 1,
                "transport_plane": 1}


def _cstr(p, addr, n=40):
    try:
        d = p.read(addr, n)
        return d.split(b"\0")[0].decode("utf-8", "replace")
    except Exception:
        return ""


def scan_wings(g):
    """Every live CAirWing (vtable scan, de-duplicated), with pool and type."""
    from objindex import _scan_regions_for_vptrs
    vt = g.p.base + WING_VT
    hits = _scan_regions_for_vptrs(
        g.p, g.p.regions(writable=True), [vt], max_hits=100_000, chunk_size=8 << 20
    )
    out = []
    for h in sorted(set(hits)):
        try:
            ht, hid = struct.unpack("<ii", g.p.read(h + 0x18, 8))
            if not (0 < hid < 100000):
                continue
            pool = g.p.u64(h + 0x30)
            if not pool:
                continue
            defp = g.p.u64(pool + 0x20)
            typ = ""
            if defp:
                q = g.p.u64(defp + 0x10)
                if q > 0x1000:
                    typ = _cstr(g.p, q, 32)
            sa = g.p.u64(pool + 0x40)
            out.append({"ptr": h, "id": hid, "pool": pool, "type": typ, "sa": sa})
        except Exception:
            continue
    out.sort(key=lambda w: w["id"])
    return out


def groups(wings):
    gs = {}
    for w in wings:
        gs.setdefault(w["sa"], []).append(w)
    return gs


def ger_air_wings(g):
    """Germany's air wings: the CStrategicAir group matching the 1936 OOB."""
    wings = scan_wings(g)
    gs = groups(wings)
    best, best_score = None, -1
    for sa, lst in gs.items():
        if not sa:
            continue
        hist = {}
        for w in lst:
            hist[w["type"]] = hist.get(w["type"], 0) + 1
        score = sum(min(hist.get(k, 0), v) for k, v in GER_1936_SIG.items())
        score -= abs(len(lst) - sum(GER_1936_SIG.values()))
        if score > best_score:
            best, best_score = lst, score
    if not best:
        return {"ok": False, "count": 0, "wings": [], "all_groups": len(gs)}
    hist = {}
    for w in best:
        hist[w["type"]] = hist.get(w["type"], 0) + 1
    return {
        "ok": True,
        "count": len(best),
        "types": hist,
        "sa": hex(best[0]["sa"]),
        "groups_total": len(gs),
        "wings_total": len(wings),
        "wings": [{"id": w["id"], "ptr": hex(w["ptr"]), "type": w["type"]} for w in best],
    }


def attach(g, wing_ptrs, army_ptrs):
    """Post CAttachAirWingToArmyCommand for each wing, round-robin over armies."""
    import api
    api.allow_call(ATTACH_CTOR)
    api.allow_call(RUN_CMD)
    attached, refused = [], []
    with g:
        errbuf = g.malloc(0x28)
        for i, w in enumerate(wing_ptrs):
            og = army_ptrs[i % len(army_ptrs)]
            cmd = g.malloc(ATTACH_SIZE)
            g.p.write(cmd, b"\0" * ATTACH_SIZE)
            g.call(ATTACH_CTOR, (cmd, w, og, 0))
            g.p.write(errbuf, struct.pack("<QQB", errbuf + 0x10, 0, 0))
            rv = g.call(RUN_CMD, (cmd, errbuf))
            if rv & 0xFF:
                attached.append({"wing": hex(w), "army": hex(og)})
            else:
                refused.append(hex(w))
    return {"attached_count": len(attached), "refused": len(refused),
            "attached": attached[:20], "refused_sample": refused[:10]}


# ---------------------------------------------------------------------------
# Strategic-air orders merged 2026-11-09 from the 海空接口逆向 session
# (work/Clausewitz-MCP/calling/airops.py, itself verified live 2026-11-08).
#   CStratAirSetMissionCommand    0x2419850 (this, CAirWing*, int mission, bool, bool)
#   CStratAirEnableMissionCommand 0x2418B90 (this, CAirWing*, int value, CStrategicAir*|0)
# `value` is a mission bitmask, not an on/off switch: fighters 1, strategic bombing 8.
# ---------------------------------------------------------------------------
SET_MISSION_CTOR = 0x2419850
ENABLE_MISSION_CTOR = 0x2418B90
TRANSFER_CTOR = 0x241AF40
SET_MISSION_SIZE = 0x48
ENABLE_MISSION_SIZE = 0x60
TRANSFER_SIZE = 0x48


def _run_cmd(g, cmd, what):
    import api
    from armyops import _mkstring, _readstring
    api.allow_call(RUN_CMD)
    s = _mkstring(g)
    ok = g.call(RUN_CMD, [cmd, s]) & 0xFF
    return {"ok": bool(ok), "op": what,
            "error": "" if ok else (_readstring(g, s) or "command rejected")}


def set_mission(g, wing_ptr, mission, flag1=False, flag2=False):
    import api
    api.allow_call(SET_MISSION_CTOR)
    cmd = g.malloc(SET_MISSION_SIZE)
    g.p.write(cmd, b"\0" * SET_MISSION_SIZE)
    g.call(SET_MISSION_CTOR, [cmd, wing_ptr, int(mission), int(bool(flag1)), int(bool(flag2))])
    r = _run_cmd(g, cmd, "set_mission")
    # 2026-11-21 (续测 P1-8): the mission mask alone never makes a wing fly.
    # +0xA4 is a persistent enable flag (measured: written 1, still 1 after
    # 1.8 s) and `enable_mission` is the engine's own command for it -- it was
    # simply never called here, which is why 5 fighters sat at mission=1 with
    # mission_on=false through peace and war alike.
    if mission:
        try:
            enable_mission(g, wing_ptr, 1)
        except Exception:
            pass
    return r


def enable_mission(g, wing_ptr, value, target_ptr=0):
    import api
    api.allow_call(ENABLE_MISSION_CTOR)
    cmd = g.malloc(ENABLE_MISSION_SIZE)
    g.p.write(cmd, b"\0" * ENABLE_MISSION_SIZE)
    g.call(ENABLE_MISSION_CTOR, [cmd, wing_ptr, int(value), int(target_ptr)])
    return _run_cmd(g, cmd, "enable_mission")


# ---------------------------------------------------------------------------
# Air bases and the paradrop range gate (found 2026-11-10, 1.19.4.1)
#
# COrderInstance::CanSetProvinceTarget (0x22EEFE0) ends with
#     airbase = fn(GetProvince(order->0xB8)->[0xE0], &ordersGroup->[0x3C])
#     range   = 0x23DF580(airbase, 0x100000, &ordersGroup->[0x3C], targetProv)
#     if (range <= 0) reject
#     return 0xDA7BB0(targetProv, sourceProv) <= range
# 0x23DF580 walks CAirBase+0x30[countryIndex] (a CCountryAirContainer) and keeps
# the biggest range among the wings whose definition carries the 0x100000
# capability bit (transport planes).  Consequence: the paradrop SOURCE PROVINCE
# must be a province whose air base actually hosts the transport wing.  That was
# the reason every target province was refused.
#
#   CAirBase             vtable 0x446F7D8  +0x30 CArray<CCountryAirContainer*>
#                                          +0x68 CProvinceDefinition*
#   CCountryAirContainer +0xB8 CArray<CAirWing*>  +0xC4 count
#   CAirWing             +0x20 definition, definition+0x590 capability mask
# ---------------------------------------------------------------------------
CAIRBASE_VT = 0x446F7D8
CAIRBASE_CONTAINERS = 0x30
CAIRBASE_PROVDEF = 0x68
CCONT_WINGS = 0xB8
CCONT_COUNT = 0xC4
WING_DEF = 0x20
DEF_CAPABILITIES = 0x590
CAP_TRANSPORT = 0x100000


def air_bases(g):
    """Every live CAirBase (one per province that can host planes)."""
    return g._scan_vtable(CAIRBASE_VT)


def country_air_container(g, airbase, country_index):
    p = g.p
    arr = p.u64(airbase + CAIRBASE_CONTAINERS)
    if not arr:
        return 0
    return p.u64(arr + int(country_index) * 8)


def container_wings(g, container):
    p = g.p
    n = p.i32(container + CCONT_COUNT)
    a = p.u64(container + CCONT_WINGS)
    if not a or not (0 < n < 400):
        return []
    return [w for w in (p.u64(a + i * 8) for i in range(n)) if w]


def wing_has_capability(g, wing, mask):
    d = g.p.u64(wing + WING_DEF)
    return bool(d) and bool(g.p.u64(d + DEF_CAPABILITIES) & mask)


def transport_bases(g, country_index, mask=CAP_TRANSPORT):
    """[(airbase, province_definition, [transport wings])] for one country."""
    out = []
    for ab in air_bases(g):
        c = country_air_container(g, ab, country_index)
        if not c:
            continue
        wings = [w for w in container_wings(g, c) if wing_has_capability(g, w, mask)]
        if wings:
            out.append((ab, g.p.u64(ab + CAIRBASE_PROVDEF), wings))
    return out


def provinces_of_definitions(g, defs, limit=2000):
    """{province_definition: province_id}: one pass over the province table."""
    p = g.p
    want = set(d for d in defs if d)
    found = {}
    for pid in range(1, limit):
        if not want:
            break
        try:
            pr = g.province_ptr(pid)
        except Exception:
            continue
        if not pr:
            continue
        d = p.u64(pr + 0xE0)
        if d in want:
            found[d] = pid
            want.discard(d)
    return found


def paradrop_source_provinces(g, orders_group):
    """Provinces a paradrop can start from: those whose air base holds a
    transport-capable wing of this country.  The country index is the value the
    engine itself uses (COrdersGroup+0x3C)."""
    p = g.p
    idx = p.i32(orders_group + 0x3C)
    bases = transport_bases(g, idx)
    defmap = provinces_of_definitions(g, [d for _ab, d, _w in bases])
    out = []
    for ab, d, wings in bases:
        pid = defmap.get(d)
        if pid:
            out.append({"province": pid, "airbase": ab, "wings": wings})
    out.sort(key=lambda r: r["province"])
    return out


FN_AIRBASE_OF_PROVDEF = 0xDBE710   # (province_definition*, int* country_index) -> CAirBase*


def source_has_transport(g, orders_group, province):
    """The transport-capable wings this country keeps in that province's air base.

    Empty list -> the province cannot start a paradrop (0x22EEFE0 range gate).
    """
    import api
    p = g.p
    pr = g.province_ptr(int(province))
    if not pr:
        return []
    d = p.u64(pr + 0xE0)
    if not d:
        return []
    api.allow_call(FN_AIRBASE_OF_PROVDEF)
    idx_ptr = orders_group + 0x3C
    ab = g.call(FN_AIRBASE_OF_PROVDEF, [d, idx_ptr]) & 0xFFFFFFFFFFFFFFFF
    if not ab:
        return []
    c = country_air_container(g, ab, p.i32(idx_ptr))
    if not c:
        return []
    return [w for w in container_wings(g, c) if wing_has_capability(g, w, CAP_TRANSPORT)]
