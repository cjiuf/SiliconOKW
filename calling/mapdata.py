"""Map data: province definitions, the adjacency graph, and border computation.

Builds the full adjacency graph from provinces.bmp + definition.csv (numpy, ~2s)
and caches it to disk. Combined with live ownership read from memory, this answers
"which provinces of X face country Y".
"""
import os, pickle, struct, sys
import numpy as np

import config
GAME = config.game_dir(required=False)
HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, "mapadj.pkl")



# Static map data never changes during a game. Re-reading it from disk on every
# tick added ~65 ms to the micro engine's tick, so it is loaded once per process
# and kept in memory.
_MEM = {}


def _once(key, fn):
    if key not in _MEM:
        _MEM[key] = fn()
    return _MEM[key]


def _load_definitions_raw(game=GAME):
    """color(rgb) -> id, and id -> type"""
    col2id, ptype = {}, {}
    with open(os.path.join(game, "map", "definition.csv"), encoding="latin-1") as f:
        for line in f:
            parts = line.strip().split(";")
            if len(parts) < 8:
                continue
            try:
                pid, r, gg, b = int(parts[0]), int(parts[1]), int(parts[2]), int(parts[3])
            except ValueError:
                continue
            col2id[(r, gg, b)] = pid
            ptype[pid] = parts[4]
    return col2id, ptype


TERRAIN_CACHE = os.path.join(os.path.dirname(__file__), "mapterrain.pkl")
RIVER_CACHE = os.path.join(os.path.dirname(__file__), "maprivers.pkl")


def _load_terrain_raw(game=GAME):
    """province_id -> terrain name (plains/forest/hills/mountain/urban/marsh/jungle/desert).

    Column 7 of definition.csv. The game's OWN file, so it matches the build
    exactly rather than guessing from a wiki.
    """
    out = {}
    with open(os.path.join(game, "map", "definition.csv"), encoding="latin-1") as f:
        for line in f:
            parts = line.strip().split(";")
            if len(parts) < 8:
                continue
            try:
                out[int(parts[0])] = parts[6]
            except ValueError:
                continue
    return out


def _read_bmp_indexed(path):
    """8-bit paletted BMP -> (h,w) uint8 index array."""
    with open(path, "rb") as f:
        data = f.read()
    off = struct.unpack_from("<I", data, 10)[0]
    w = struct.unpack_from("<i", data, 18)[0]
    h = struct.unpack_from("<i", data, 22)[0]
    bpp = struct.unpack_from("<H", data, 28)[0]
    assert bpp == 8, "rivers.bmp must be 8-bit"
    flip = h > 0
    h = abs(h)
    row = (w + 3) // 4 * 4               # rows are aligned to 4 bytes
    arr = np.frombuffer(data, dtype=np.uint8, count=row * h, offset=off)
    arr = arr.reshape(h, row)[:, :w]
    return arr[::-1] if flip else arr


def _build_rivers_raw(game=GAME, force=False, min_ratio=0.30, min_pixels=6):
    """(a,b) -> 'small'|'large': is there a RIVER CROSSING between two provinces?

    rivers.bmp is 8-bit paletted; per the game's defines, colour index 0..6 is a
    small river and 7..11 a large one (RIVER_SMALL_STOP_INDEX=6,
    RIVER_LARGE_STOP_INDEX=11).

    Method: where two neighbouring pixels belong to different provinces, that
    contact is a BORDER pixel; if a river pixel sits there it counts. To filter
    noise (rivers that end inside a province near its edge) at least `min_ratio`
    of the border contacts must be river. A crossing costs the attacker 30-60%,
    and a false positive cancels attacks for no reason, so the threshold is
    deliberately conservative.
    """
    if not force and os.path.exists(RIVER_CACHE):
        with open(RIVER_CACHE, "rb") as f:
            return pickle.load(f)
    col2id, _ = load_definitions(game)
    img = _read_bmp_rgb(os.path.join(game, "map", "provinces.bmp"))
    key = (img[:, :, 0].astype(np.uint32) << 16) | (img[:, :, 1].astype(np.uint32) << 8) | img[:, :, 2]
    lut = {}
    for (r, g, b), pid in col2id.items():
        lut[(r << 16) | (g << 8) | b] = pid
    uniq = np.unique(key)
    m = np.zeros(uniq.max() + 1, dtype=np.int32)
    for k in uniq:
        m[k] = lut.get(int(k), -1)
    ids = m[key]
    riv = _read_bmp_indexed(os.path.join(game, "map", "rivers.bmp"))
    if riv.shape != ids.shape:
        raise RuntimeError("rivers.bmp and provinces.bmp differ in size: %s %s"
                           % (riv.shape, ids.shape))
    is_riv = riv <= 11
    is_big = (riv >= 7) & is_riv

    total, rivers, bigs = {}, {}, {}

    def scan(a, b, ra, rb, ba, bb):
        am, bm = a.ravel(), b.ravel()
        ok = (am != bm) & (am >= 0) & (bm >= 0)
        rv = (ra.ravel()[ok] | rb.ravel()[ok])
        bg = (ba.ravel()[ok] | bb.ravel()[ok])
        for x, y, r_, g_ in zip(am[ok].tolist(), bm[ok].tolist(),
                                rv.tolist(), bg.tolist()):
            k = (x, y) if x < y else (y, x)
            total[k] = total.get(k, 0) + 1
            if r_:
                rivers[k] = rivers.get(k, 0) + 1
                if g_:
                    bigs[k] = bigs.get(k, 0) + 1

    scan(ids[:, :-1], ids[:, 1:], is_riv[:, :-1], is_riv[:, 1:], is_big[:, :-1], is_big[:, 1:])
    scan(ids[:-1, :], ids[1:, :], is_riv[:-1, :], is_riv[1:, :], is_big[:-1, :], is_big[1:, :])

    out = {}
    for k, n in total.items():
        r = rivers.get(k, 0)
        if r >= min_pixels and r / n >= min_ratio:
            out[k] = "large" if bigs.get(k, 0) * 2 >= r else "small"
    with open(RIVER_CACHE, "wb") as f:
        pickle.dump(out, f)
    return out


STATE_CACHE = os.path.join(HERE, "mapstates.pkl")


def _load_state_provinces_raw(game=GAME, force=False):
    """(state_id -> set(province_ids), province_id -> state_id).

    From history/states/*.txt. Which provinces make up a state never changes
    during a game, so the mapping is cached to disk.

    Why it is needed: the capital field at CCountry+0xFF0 is a STATE id, not a
    province id. Treating it as a province meant the enemy mainland was never
    found and every enemy unit looked pocketed.
    """
    if not force and os.path.exists(STATE_CACHE):
        with open(STATE_CACHE, "rb") as f:
            return pickle.load(f)
    import re
    d = os.path.join(game, "history", "states")
    st2p, p2st = {}, {}
    for fn in os.listdir(d):
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
        pm = re.search(r"provinces\s*=\s*\{([^}]*)\}", txt)
        if not pm:
            continue
        provs = {int(x) for x in re.findall(r"\d+", pm.group(1))}
        st2p[sid] = provs
        for q in provs:
            p2st[q] = sid
    out = (st2p, p2st)
    with open(STATE_CACHE, "wb") as f:
        pickle.dump(out, f)
    return out


def river_between(rivers, a, b):
    return rivers.get((a, b) if a < b else (b, a))


def _read_bmp_rgb(path):
    with open(path, "rb") as f:
        data = f.read()
    off = struct.unpack_from("<I", data, 10)[0]
    w = struct.unpack_from("<i", data, 18)[0]
    h = struct.unpack_from("<i", data, 22)[0]
    bpp = struct.unpack_from("<H", data, 28)[0]
    assert bpp == 24, "24-bit BMP only"
    flip = h > 0
    h = abs(h)
    row = (w * 3 + 3) & ~3
    arr = np.frombuffer(data, dtype=np.uint8, count=row * h, offset=off)
    arr = arr.reshape(h, row)[:, : w * 3].reshape(h, w, 3)
    if flip:
        arr = arr[::-1]
    return arr[:, :, ::-1]          # BGR -> RGB


def _build_adjacency_raw(game=GAME, force=False):
    """{province_id: set(neighbour_ids)}: cached to disk."""
    if not force and os.path.exists(CACHE):
        with open(CACHE, "rb") as f:
            return pickle.load(f)
    col2id, _ = load_definitions(game)
    img = _read_bmp_rgb(os.path.join(game, "map", "provinces.bmp"))
    key = (img[:, :, 0].astype(np.uint32) << 16) | (img[:, :, 1].astype(np.uint32) << 8) | img[:, :, 2]
    lut = {}
    for (r, g, b), pid in col2id.items():
        lut[(r << 16) | (g << 8) | b] = pid
    uniq = np.unique(key)
    m = np.zeros(uniq.max() + 1, dtype=np.int32)
    for k in uniq:
        m[k] = lut.get(int(k), -1)
    ids = m[key]
    adj = {}
    def add(a, b):
        am = a.ravel(); bm = b.ravel()
        ok = (am != bm) & (am >= 0) & (bm >= 0)
        for x, y in zip(am[ok].tolist(), bm[ok].tolist()):
            adj.setdefault(x, set()).add(y)
            adj.setdefault(y, set()).add(x)
    add(ids[:, :-1], ids[:, 1:])
    add(ids[:-1, :], ids[1:, :])
    # the map wraps east-west
    add(ids[:, -1:], ids[:, :1])
    with open(CACHE, "wb") as f:
        pickle.dump(adj, f)
    return adj


# ------------------------------------------------------------------ bellek
PROV_ID    = 0xA4
PROV_STATE = 0xE0
# 2026-11-16: CState::id sits at +0x58 on hoi4.exe (PE 1.19.5.0), not +0x48.
# Reading +0x48 returns 0 for every state, which made province_owner_map()
# return an EMPTY dict — and that in turn made every BFS path fail with
# "no legal land path" (deploy_to_border / move_divisions / attack_country).
# Verified live: the +0x58 column of GER's 34 CState objects is exactly
# [4,5,42,50,51,...,978], i.e. hoi4_states(GER).
STATE_ID   = 0x58 if sys.platform == "win32" else 0x48
CO_STATES  = 0x460


def province_owner_map(g, with_ptrs=False):
    """province_id -> tag (live). With `with_ptrs`, also CProvince* -> tag.

    PERFORMANCE: the old version read all 26,826 CProvince objects one at a time
    (two reads each, ~646 ms) and accounted for 90% of the micro engine's tick.
    Which state a province belongs to never changes during a game. That mapping
    comes statically from history/states. The only live part is who holds the
    state, which is ~1,100 reads from the countries' state lists. Unless
    `with_ptrs` is asked for, no province object is read at all.
    """
    from game import _arr
    p = g.p
    # g.country(tag) re-reads the country array on every call, 440 times for
    # 440 countries. Fetch it once and walk it directly.
    g._load_tags()
    ptrs = g.country_ptrs()
    st2tag = {}
    for tid, tag in enumerate(g._tags):
        if not tag or tag == "---":
            continue
        try:
            ci = g._tag2idx[tid]
            if not (0 <= ci < len(ptrs)):
                continue
            for s in _arr(p, ptrs[ci], CO_STATES):
                st2tag.setdefault(s, tag)
        except Exception:
            continue
    st2p, _ = load_state_provinces()
    byid = {}
    for sptr, tag in st2tag.items():
        try:
            sid = p.i32(sptr + STATE_ID)
        except Exception:
            continue
        for q in st2p.get(sid, ()):
            byid[q] = tag
    if not with_ptrs:
        return byid, {}
    byptr = {}
    for pr in g.oi.instances("CProvince"):
        try:
            t = st2tag.get(p.u64(pr + PROV_STATE))
            if t is not None:
                byptr[pr] = t
        except Exception:
            pass
    return byid, byptr


# CProvince vtable offset in 1.19.4.1 (f584). CProvince objects live in a heap
# region that ObjectIndex's writable-only scan does NOT cover, so we look for the
# vtable in every readable mapping once and cache the result on disk.
PROV_VT_OFF = 0x4390588
PROV_PTR_CACHE = os.path.join(HERE, "prov_ptrs.json")
PROV_MAX_ID = 30000


def province_objects(g, force=False):
    """province_id -> CProvince* (cached; one full scan per game process)."""
    key = ("prov_objs", g.p.pid, g.p.base)
    if not force and key in _MEM:
        return _MEM[key]
    try:
        import json
        c = json.load(open(PROV_PTR_CACHE))
        # Never trust an empty/partial cache: the background staff fires a tick
        # while the game is still on the main menu, finds no CProvince at all,
        # and would otherwise poison this pid for the whole session.
        if (c.get("pid") == g.p.pid and c.get("base") == g.p.base
                and len(c.get("ptrs") or {}) > 1000):
            _MEM[key] = {int(k): int(v) for k, v in c["ptrs"].items()}
            return _MEM[key]
    except Exception:
        pass
    vt = g.p.base + PROV_VT_OFF
    needle = struct.pack("<Q", vt)
    hits = g.p.scan_value(needle, g.p.regions(writable=False))
    out = {}
    for h in hits:
        try:
            pid = g.p.i32(h + PROV_ID)
        except OSError:
            continue
        if 0 < pid < PROV_MAX_ID:
            out.setdefault(pid, h)
    if len(out) > 1000:
        try:
            import json
            json.dump({"pid": g.p.pid, "base": g.p.base,
                       "ptrs": {str(k): v for k, v in out.items()}},
                      open(PROV_PTR_CACHE, "w"))
        except Exception:
            pass
    _MEM[key] = out
    return out


_CAP_PROV_CACHE = {}


def capital_provinces(g):
    """province_id -> tag for every country's capital state (cached per pid).

    `CCountry+0xFF0` is a STATE id (verified: GER -> 64 -> Berlin 9496). The
    capital state's provinces come from the static state table, so no per-country
    CO_STATES walk is needed and two countries can never claim the same capital.
    """
    key = getattr(g.p, "pid", None)
    if key in _CAP_PROV_CACHE:
        return _CAP_PROV_CACHE[key]
    out = {}
    try:
        g._load_tags()
        ptrs = g.country_ptrs()
        st2p, _ = load_state_provinces()
        state_tag = {}
        for tid, tag in enumerate(g._tags):
            if not tag or tag == "---":
                continue
            try:
                ci = g._tag2idx[tid]
                if not (0 <= ci < len(ptrs)):
                    continue
                sid = int(g.p.i32(ptrs[ci] + 0xFF0))
                if sid > 0:
                    state_tag.setdefault(sid, tag)
            except Exception:
                continue
        for sid, tag in state_tag.items():
            for q in st2p.get(sid, ()):
                out[q] = tag
    except Exception:
        out = {}
    _CAP_PROV_CACHE[key] = out
    return out


def _win_controller_map(g, divisions=None):
    """1.19.5 province -> controller tag.

    The Linux path walked CProvince objects looking for a CState pointer at
    +0xE0 and a CControllerArea at +0xF0. On the PE build a province carries a
    COwnerArea at +0xD8 and a CControllerArea at +0xD0, and no CState pointer,
    so `province_objects()` (a fixed Linux vtable scan) found nothing and every
    caller saw an empty map -- `front_changes()` reported provinces_seen=0.

    Occupation in HOI4 flips the *state* controller (CState+0xCC), so this reads
    that and expands each state to its provinces. Per-province combat occupation
    within a contested state follows the state controller until the state flips.
    """
    import struct as _st
    p = g.p
    try:
        g._load_tags()
        tags, t2i = g._tags, g._tag2idx
    except Exception:
        tags, t2i = [], {}

    def tag_of(idx):
        try:
            return tags[t2i[idx]]
        except Exception:
            return None

    out = {}
    try:
        gs = g.gamestate
        arr = p.u64(gs + 0x2C8)          # GS_STATES, CPdxArray<CState*>
        n = p.i32(gs + 0x2C8 + 0xC)
    except Exception:
        return out
    for i in range(max(0, min(n, 4000))):
        try:
            st = p.u64(arr + 8 * i)
            if not st:
                continue
            ctl = p.i32(st + 0xCC) or p.i32(st + 0xC8)
            t = tag_of(ctl)
            if not t:
                continue
            pa = p.u64(st + 0x18)        # ST_PROVINCES
            pn = p.i32(st + 0x18 + 0xC)
            if not pa or not (0 < pn <= 400):
                continue
            for k in range(pn):
                pr = p.u64(pa + 8 * k)
                if pr:
                    out[p.i32(pr + 0xA4)] = t
        except Exception:
            continue
    if divisions:
        seen = {}
        for r in divisions:
            pv, tag = r.get("prov"), r.get("tag")
            if pv is not None and tag:
                seen.setdefault(pv, set()).add(tag)
        for pv, ts in seen.items():
            if len(ts) == 1:
                out[pv] = next(iter(ts))
    return out


def province_controller_map(g, divisions=None):
    """province_id -> controller tag, LIVE (civil-war occupation included).

    The old province_owner_map() derives the tag from each country's CO_STATES
    list, i.e. *state ownership*. In a civil war (and any war before a peace
    treaty) occupied provinces only change their controller; ownership stays with
    the original country, so front_changes reported 0 while a state was lost.

    Controller resolution:
      1. Every CProvince has a CControllerArea pointer at +0xF0. All provinces
         controlled by the same country share one area object.
      2. T5 (review 2026-11-07): the area's tag is anchored on the CAPITAL
         province first. A raw majority vote of the owners flipped an area that
         contained more occupied enemy provinces than homeland provinces (East
         Prussia with 16 Polish provinces vs 15 German ones was reported as
         "POL"), which made our own ground look like enemy territory. Only when
         no capital is inside the area do we fall back to the owner vote.
      3. `area == 0` (sea / uninitialised) no longer forms one giant bucket that
         voted for itself; those provinces keep their state owner.
      4. Optional `divisions` overlay: a province with divisions of exactly one
         tag present is forced to that tag (handles freshly occupied ground
         before the area pointer updates).
    """
    import sys as _sys
    if _sys.platform == "win32":
        return _win_controller_map(g, divisions)
    from collections import Counter
    owners, _ = province_owner_map(g)
    ptrs = province_objects(g)
    caps = capital_provinces(g)
    groups = {}
    for pid, pr in ptrs.items():
        try:
            area = g.p.u64(pr + 0xF0)
        except OSError:
            area = 0
        if not area:
            continue
        groups.setdefault(area, []).append(pid)
    byid = {}
    for area, pids in groups.items():
        cap_hits = Counter(caps[x] for x in pids
                           if caps.get(x) and owners.get(x) == caps[x])
        if len(cap_hits) == 1:
            tag = next(iter(cap_hits))
        else:
            c = Counter(owners.get(x) for x in pids
                        if owners.get(x) not in (None, "---"))
            tag = c.most_common(1)[0][0] if c else None
        for pid in pids:
            byid[pid] = tag or owners.get(pid)
    # Any province missing from the CProvince scan keeps its state owner.
    for pid, t in owners.items():
        byid.setdefault(pid, t)
    if divisions:
        seen = {}
        for r in divisions:
            pv, tag = r.get("prov"), r.get("tag")
            if pv is not None and tag:
                seen.setdefault(pv, set()).add(tag)
        for pv, tags in seen.items():
            if len(tags) == 1:
                byid[pv] = next(iter(tags))
    return byid


def border_provinces(g, own_tag, enemy_tag, owner_by_id=None, adj=None):
    """Land provinces of own_tag that border enemy_tag, as a list of ids."""
    if owner_by_id is None:
        owner_by_id = province_controller_map(g)
    if adj is None:
        adj = build_adjacency()
    _, ptype = load_definitions()
    out = []
    for pid, t in owner_by_id.items():
        if t != own_tag or ptype.get(pid) != "land":
            continue
        for n in adj.get(pid, ()):
            if owner_by_id.get(n) == enemy_tag and ptype.get(n) == "land":
                out.append(pid)
                break
    return sorted(out)


# ---------------------------------------------------------------- cached API
def load_definitions(game=GAME):
    return _once(("defs", game), lambda: _load_definitions_raw(game))


def load_terrain(game=GAME):
    return _once(("terrain", game), lambda: _load_terrain_raw(game))


def build_adjacency(game=GAME, force=False):
    if force:
        _MEM.pop(("adj", game), None)
        return _once(("adj", game), lambda: _build_adjacency_raw(game, True))
    return _once(("adj", game), lambda: _build_adjacency_raw(game))


def build_rivers(game=GAME, force=False, **kw):
    if force:
        _MEM.pop(("riv", game), None)
        return _once(("riv", game), lambda: _build_rivers_raw(game, True, **kw))
    return _once(("riv", game), lambda: _build_rivers_raw(game, **kw))


def load_state_provinces(game=GAME, force=False):
    if force:
        _MEM.pop(("states", game), None)
        return _once(("states", game), lambda: _load_state_provinces_raw(game, True))
    return _once(("states", game), lambda: _load_state_provinces_raw(game))
