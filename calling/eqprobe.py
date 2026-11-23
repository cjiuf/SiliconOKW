"""Probe helpers for 'equipment in armies' (num_equipment_in_armies@<type>).

The engine value is not cached in CCountry (a scan for 2490000000 found
nothing), so it must be summed from the divisions themselves.  These helpers
locate the per-division equipment store.
"""
import struct


def pair_arrays(p, obj, span=0x1400, size_off=0xC, data_off=0x0, maxn=600,
                ptr_lo=0x10000, ptr_hi=0x7F0000000000):
    """CPdxArray members whose elements look like {pointer; int64 count}."""
    out = []
    for off in range(0, span, 8):
        try:
            d = p.u64(obj + off + data_off)
            n = p.i32(obj + off + size_off)
        except Exception:
            continue
        if not (0 < n <= maxn) or not (ptr_lo < d < ptr_hi):
            continue
        raw = p.try_read(d, 16 * n)
        if not raw or len(raw) < 16 * n:
            continue
        vals = struct.unpack("<%dq" % (2 * n), raw)
        pairs = [(vals[2 * i], vals[2 * i + 1]) for i in range(n)]
        if not all(ptr_lo < a < ptr_hi for a, _ in pairs):
            continue
        out.append({"off": off, "n": n, "data": d, "pairs": pairs})
    return out


#: CArmy member: CPdxArray<{CEquipmentVariant*; int64 count x1e5}> holding the
#: equipment the division actually carries.  Found by scanning a CArmy for
#: {pointer; count} arrays: +0x368 and +0x1088 carry the identical list, and
#: summing +0x368 over all 30 GER divisions gives 24900 infantry-equipment
#: pieces, exactly what `get_var num_equipment_in_armies@infantry_equipment`
#: reports.  +0x368 is the one used here (the 0x1088 copy is the same data).
ARM_EQUIP_OFF = 0x368


def division_equipment(p, div_ptr, off=ARM_EQUIP_OFF):
    """[{variant*, count x1e5}] for one division, [] when the array is absent."""
    for a in pair_arrays(p, div_ptr, span=off + 0x38):
        if a["off"] == off:
            return a["pairs"]
    return []


def army_equipment(g, tag=None):
    """{archetype: count} of equipment held by the country's divisions."""
    from game import CO_ARMIES, _arr
    p = g.p
    # The equipment id -> name map is lazy; on a COLD process it is still None,
    # which made total_by_arch silently return {} and the whole answer 0.0.
    # That is exactly what happened right after a game restart (2026-11-20).
    g._load_equipment()
    c = g._c(tag or g.player_tag())
    tot = {}
    for d in _arr(p, c.ptr, CO_ARMIES):
        for k, v in total_by_arch(g, division_equipment(p, d)).items():
            tot[k] = tot.get(k, 0.0) + v
    return dict((k, round(v, 1)) for k, v in tot.items())


def state_arrays(p, obj, cls="CState", span=0x1800, size_off=0xC, data_off=0x0,
                 minn=2, maxn=400):
    """CPdxArray members of `obj` whose first element is an instance of `cls`.

    Returns [{off, n, data, first}] so the caller can tell the owned-state list
    from the controlled-state list (GER has 29 of each in peacetime, ITA has
    more controlled than owned in 1936 because it is occupying Ethiopia).
    """
    out = []
    seen = set()
    for off in range(0, span, 8):
        try:
            d = p.u64(obj + off + data_off)
            n = p.i32(obj + off + size_off)
        except Exception:
            continue
        if not (minn <= n <= maxn) or not (0x10000 < d < 0x7F0000000000):
            continue
        if d in seen:
            continue
        raw = p.try_read(d, 8)
        if not raw:
            continue
        first = struct.unpack("<Q", raw)[0]
        if not (0x10000 < first < 0x7F0000000000):
            continue
        seen.add(d)
        out.append({"off": off, "n": n, "data": d, "first": first})
    return out


def state_ids(p, arr, id_off=0x58):
    return [p.i32(s + id_off) for s in arr if s]


def stock_by_type(g, tag=None):
    """{equipment variant name: count} from the country's stockpile array."""
    rows = (g.equipment_stockpile(tag) or {}).get("stockpile") or []
    out = {}
    for r in rows:
        nm = r.get("equipment")
        if not nm:
            continue
        out[nm] = out.get(nm, 0.0) + float(r.get("count") or 0)
    return dict((k, round(v, 1)) for k, v in out.items())


def resolve(g, totals, arg):
    """Resolve an `@argument` the way the engine's evaluator does.

    The engine takes an equipment entry name.  A VARIANT name (``*_1``) counts
    just that variant; an ARCHETYPE name (``infantry_equipment``) counts every
    variant declared with ``archetype = <name>``.  A name the equipment database
    does not know at all counts 0 -- verified with `get_var`:
    ``@light_tank_equipment`` is 0 because that archetype is commented out in
    1.19 (tanks moved to the chassis system) while ``@light_tank_equipment_1``
    answers 692, and a made-up name also answers 0.

    Returns (value, info).
    """
    if not arg:
        return 0.0, {"match": "no-argument"}
    if arg in totals:
        return round(totals[arg], 1), {"match": "variant"}
    try:
        g._load_equipment()
        known = arg in (g._eq_by_name or {})
    except Exception:
        known = False
    if known:
        group = [n for n in totals if n.startswith(arg + "_")]
        if group:
            return (round(sum(totals[n] for n in group), 1),
                    {"match": "archetype", "variants": sorted(group)})
        return 0.0, {"match": "archetype", "variants": []}
    near = sorted(n for n in (getattr(g, "_eq_by_name", None) or {})
                  if arg in n)[:8]
    return 0.0, {"match": "unknown-name", "similar": near}


def total_by_arch(g, pairs):
    """{archetype name: total count} using the game's equipment table."""
    from game import EV_TYPE_ID
    p = g.p
    tot = {}
    for vp, cnt in pairs:
        try:
            nm = g._eq_by_id.get(p.i32(vp + EV_TYPE_ID))
        except Exception:
            nm = None
        if not nm:
            continue
        tot[nm] = tot.get(nm, 0) + cnt / 100000.0
    return tot
