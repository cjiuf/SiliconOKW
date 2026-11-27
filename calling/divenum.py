"""Division enumeration that refuses to report a silent zero (PE 1.19.5.0).

Why this module exists
----------------------
2026-11-16, campaign #2: after the 1939.05.07 crash-restart, `hoi4_army_status`
answered `divisions_count: 0` although 109 divisions were standing in Germany.
`hoi4_deploy_to_border` / `hoi4_army_battleplans` / `hoi4_attack_country` /
`hoi4_move_divisions` all go through the same read, so the whole land war
answered "no divisions" -- and the army never left Berlin.

The container is not the problem: `CCountry+0x290` is a real `CPdxArray<CArmy*>`
(2026-11-16: ENG 122, SOV 212, FRA 122, ITA 124).  What can be wrong is *which*
`CCountry` we resolved for a tag.  `api.country()` trusts the cached
`tag2index` row, and `api.py` records the identical symptom once before
("divisions(GER) == 0 while the UI showed 106").

So this module resolves a country by OBJECT IDENTITY -- every `CCountry` stores
its own tag id at `+0x08` in this build -- and never through `tag2index`.
It also offers `deep_scan()`, an RTTI fallback that finds `CArmy` objects and
attributes them through their province, which is used only when the country's
own array is empty and the caller asks for a verified answer.
"""
import struct

import offsets as O

PDX_SIZE = O.PDXARRAY_SIZE       # 0x0C
PDX_DATA = O.PDXARRAY_DATA       # 0x00

CO_TAG_ID = 0x08                 # u32 tag id inside CCountry (== country index)
CO_ARMIES = 0x290                # CPdxArray<CArmy*>
CARMY_PROVINCE = 0x1F0           # CProvince*
PROV_ID = 0xA4                   # i32 province id inside CProvince

MAX_ARMY = 5000


def _arr(p, obj, off, limit=MAX_ARMY):
    """CPdxArray<T*> -> list of pointers, with the bound that keeps a wrong
    offset from asking for gigabytes (same guard as game._arr)."""
    try:
        n = p.i32(obj + off + PDX_SIZE)
        d = p.u64(obj + off + PDX_DATA)
        if n <= 0 or not d or n > limit or d < 0x10000:
            return []
        return list(struct.unpack("<%dQ" % n, p.read(d, 8 * n)))
    except OSError:
        return []


def _index_by_tag(g):
    """tag id -> index in countries[], read from each CCountry's own tag field.

    Cached on the Game object and keyed on (pid, array address, element count):
    a save reload reallocates both the array and the objects.
    """
    p = g.p
    ptrs = g.country_ptrs()
    try:
        data = p.u64(g.gamestate + O.GS_COUNTRIES + PDX_DATA)
    except Exception:
        data = 0
    key = (p.pid, data, len(ptrs))
    cached = getattr(g, "_div_tagmap", None)
    if cached and cached[0] == key:
        return cached[1], ptrs
    out = {}
    for i, q in enumerate(ptrs):
        try:
            tid = p.u32(q + CO_TAG_ID)
        except Exception:
            continue
        if 0 <= tid < 100000 and tid not in out:
            out[tid] = i
    try:
        g._div_tagmap = (key, out)
    except Exception:
        pass
    return out, ptrs


def tag_id(g, tag):
    """(TAG, tag id) for a tag string, refreshing the tag table on a miss."""
    g._load_tags()
    tag = str(tag or "").upper()
    if tag not in g._tags:
        g._tags = None
        g._load_tags()
    if tag not in g._tags:
        raise KeyError("unknown tag: %r" % (tag,))
    return tag, g._tags.index(tag)


def country(g, tag):
    """(CCountry pointer, tag id) resolved by object identity.

    Raises instead of returning a pointer to the wrong country: issuing orders
    for somebody else's army is the worst outcome this stack can produce.
    """
    tag, tid = tag_id(g, tag)
    idx, ptrs = _index_by_tag(g)
    i = idx.get(tid)
    if i is None or i >= len(ptrs):
        raise KeyError(
            "no live CCountry carries tag id %d (%s); tag may be unformed/annexed/"
            "absent in this save (countries array has %d entries)" % (tid, tag, len(ptrs))
        )
    return ptrs[i], tid


def armies(g, tag):
    """The country's divisions straight out of CCountry+0x290."""
    p = g.p
    ptr, tid = country(g, tag)
    n = 0
    try:
        n = p.i32(ptr + CO_ARMIES + PDX_SIZE)
    except OSError:
        n = 0
    rows = _arr(p, ptr, CO_ARMIES)
    if n < len(rows):
        n = len(rows)
    return {"ptr": ptr, "tid": tid, "count": int(n), "armies": rows,
            "source": "CCountry+0x290",
            "verified": bool(0 <= n <= MAX_ARMY)}


def our_provinces(g, tag):
    """Province ids belonging to states we hold, resolved without tag2index.

    `mapdata.province_controller_map` derives its tag names from the cached
    `tag2index`, i.e. from exactly the table that can go stale, so the deep
    fallback cannot use it.  Ownership does not change during a game: read the
    country's own state list once and expand it with the static history map.
    """
    import mapdata
    p = g.p
    ptr, tid = country(g, tag)
    # CO_STATES only: it is the verified "owned states" list.  CO_STATES_B/C are
    # two further state lists whose meaning is not decoded, and over-including
    # them would let the fallback claim divisions standing on somebody else's
    # soil -- worse than reporting nothing.
    states = set(_arr(p, ptr, O.CO_STATES, limit=20000))
    _st2p, p2st = mapdata.load_state_provinces()
    return {pid for pid, sid in p2st.items() if sid in states}, states


def deep_scan(g, tag):
    """RTTI fallback: every CArmy standing in a province of one of our states.

    Used only when `CCountry+0x290` is empty and the caller needs a verified
    answer.  `win_scan_class` walks writable memory once per (pid, class) and
    caches the hit list, so the expensive part happens at most once per game
    process.
    """
    p = g.p
    provs, _states = our_provinces(g, tag)
    out, seen = [], set()
    for a in g.win_scan_class("CArmy"):
        if a in seen:
            continue
        seen.add(a)
        try:
            pr = p.u64(a + CARMY_PROVINCE)
            if not pr:
                continue
            pid = p.i32(pr + PROV_ID)
        except Exception:
            continue
        if pid in provs:
            out.append(a)
    return out


def selftest(g, tag=None, deep=True):
    """Compare the three independent readings of "how many divisions do I have".

    Run this at the start of a campaign and after every game restart: a
    disagreement here is exactly the 2026-11-16 failure, and it is silent
    unless somebody looks.
    """
    tag = str(tag or g.player_tag()).upper()
    info = armies(g, tag)
    try:
        tidx = g._tag2idx[g._tags.index(tag)]
        ptrs = g.country_ptrs()
        legacy_ptr = ptrs[tidx] if 0 <= tidx < len(ptrs) else None
    except Exception:
        tidx, legacy_ptr = None, None
    legacy_n = None
    if legacy_ptr:
        try:
            legacy_n = g.p.i32(legacy_ptr + CO_ARMIES + PDX_SIZE)
        except OSError:
            legacy_n = None
    deep = None
    if not info["count"] and deep:
        try:
            deep = len(deep_scan(g, tag))
        except Exception as e:
            deep = "error: %r" % (e,)
    agree = (legacy_n is None or legacy_n == info["count"])
    return {
        "tag": tag,
        "identity_count": info["count"],
        "identity_ptr": hex(info["ptr"]),
        "identity_tag_id": info["tid"],
        "tag2index_row": tidx,
        "tag2index_ptr": hex(legacy_ptr) if legacy_ptr else None,
        "tag2index_count": legacy_n,
        "same_country_object": bool(legacy_ptr == info["ptr"]),
        "deep_scan_count": deep,
        "ok": bool(agree and (legacy_ptr == info["ptr"])),
        "note": ("identity and tag2index agree" if agree and legacy_ptr == info["ptr"]
                 else "MISMATCH: do not trust a zero from the legacy path"),
    }


def all_countries_divisions(g, tags=None, non_zero_only=False):
    """Single-pass division counts across all (or specified) live countries.

    Builds `_index_by_tag(g)` once and reads `CCountry+0x290` array sizes directly
    without triggering any full-heap scan.
    """
    p = g.p
    if hasattr(g, "_load_tags"):
        g._load_tags()
    tag_table = list(getattr(g, "_tags", None) or ())
    if not tag_table and hasattr(g, "tags"):
        tag_table = list(g.tags() or ())
    tag_to_tid = {
        str(t).upper(): tid
        for tid, t in enumerate(tag_table)
        if t and str(t) != "---"
    }
    if tags is None:
        want_tags = sorted(tag_to_tid.keys())
    else:
        seen = set()
        want_tags = []
        for t in tags:
            tu = str(t or "").upper()
            if tu and tu not in seen:
                seen.add(tu)
                want_tags.append(tu)
    idx, ptrs = _index_by_tag(g)
    by_tag = {}
    for tu in want_tags:
        tid = tag_to_tid.get(tu)
        if tid is None:
            continue
        ci = idx.get(tid)
        if ci is None or not (0 <= ci < len(ptrs)):
            continue
        ptr = ptrs[ci]
        if not ptr:
            continue
        n = 0
        try:
            n = int(p.i32(ptr + CO_ARMIES + PDX_SIZE))
        except OSError:
            n = 0
        if n < 0 or n > MAX_ARMY:
            n = len(_arr(p, ptr, CO_ARMIES))
        if non_zero_only and n == 0:
            continue
        by_tag[tu] = n
    top = [
        {"tag": t, "count": cnt}
        for t, cnt in sorted(by_tag.items(), key=lambda kv: (-kv[1], kv[0]))[:20]
    ]
    return {
        "total_divisions": sum(by_tag.values()),
        "countries_count": len(by_tag),
        "by_tag": by_tag,
        "top": top,
    }
