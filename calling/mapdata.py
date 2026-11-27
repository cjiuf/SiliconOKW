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
    import safe_cache
    if not force and safe_cache.exists(RIVER_CACHE):
        return safe_cache.load(RIVER_CACHE)
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
    safe_cache.save(RIVER_CACHE, out, fmt="json")
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
    import safe_cache
    if not force and safe_cache.exists(STATE_CACHE):
        return safe_cache.load(STATE_CACHE)
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
    safe_cache.save(STATE_CACHE, out, fmt="json")
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
    import safe_cache
    if not force and safe_cache.exists(CACHE):
        return safe_cache.load(CACHE)
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
    safe_cache.save(CACHE, adj, fmt="json")
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
    import fastmem as _fm
    import offsets as _O
    from game import _arr
    p = g.p
    # g.country(tag) re-reads the country array on every call, 440 times for
    # 440 countries. Fetch it once and walk it directly.
    g._load_tags()
    ptrs = g.country_ptrs()
    valid_countries = []
    for tid, tag in enumerate(g._tags):
        if not tag or tag == "---":
            continue
        try:
            ci = g._tag2idx[tid]
            if 0 <= ci < len(ptrs):
                valid_countries.append((tag, ptrs[ci]))
        except Exception:
            continue

    st2tag = {}
    hdr_reqs = [(cptr + CO_STATES, 16) for _, cptr in valid_countries]
    hdr_batch = _fm.read_batch(p, hdr_reqs) if hdr_reqs else None
    if hdr_batch is not None:
        pdx_data = getattr(_O, "PDXARRAY_DATA", 0)
        pdx_size = getattr(_O, "PDXARRAY_SIZE", 0x0C)
        min_hdr = max(pdx_data + 8, pdx_size + 4)
        arr_reqs = []
        arr_tags = []
        for (tag, _cptr), raw in zip(valid_countries, hdr_batch):
            if not raw or len(raw) < min_hdr:
                continue
            d = struct.unpack_from("<Q", raw, pdx_data)[0]
            n = struct.unpack_from("<i", raw, pdx_size)[0]
            if d and 0 < n <= 100000 and d >= 0x10000:
                arr_reqs.append((d, 8 * n))
                arr_tags.append((tag, n))
        arr_batch = _fm.read_batch(p, arr_reqs) if arr_reqs else []
        for (tag, n), raw in zip(arr_tags, arr_batch or ()):
            if not raw or len(raw) < 8 * n:
                continue
            for s in struct.unpack("<%dQ" % n, raw):
                st2tag.setdefault(s, tag)
    else:
        for tag, cptr in valid_countries:
            try:
                for s in _arr(p, cptr, CO_STATES):
                    st2tag.setdefault(s, tag)
            except Exception:
                continue

    st2p, _ = load_state_provinces()
    byid = {}
    st_items = list(st2tag.items())
    sid_batch = (
        _fm.read_batch(p, [(sptr + STATE_ID, 4) for sptr, _ in st_items])
        if st_items
        else None
    )
    if sid_batch is not None:
        for (_sptr, tag), raw in zip(st_items, sid_batch):
            if not raw or len(raw) < 4:
                continue
            sid = struct.unpack("<i", raw)[0]
            for q in st2p.get(sid, ()):
                byid[q] = tag
    else:
        for sptr, tag in st_items:
            try:
                sid = p.i32(sptr + STATE_ID)
            except Exception:
                continue
            for q in st2p.get(sid, ()):
                byid[q] = tag
    if not with_ptrs:
        return byid, {}
    byptr = {}
    provs = list(g.oi.instances("CProvince"))
    ps_batch = (
        _fm.read_batch(p, [(pr + PROV_STATE, 8) for pr in provs])
        if provs
        else None
    )
    if ps_batch is not None:
        for pr, raw in zip(provs, ps_batch):
            if not raw or len(raw) < 8:
                continue
            t = st2tag.get(struct.unpack("<Q", raw)[0])
            if t is not None:
                byptr[pr] = t
    else:
        for pr in provs:
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
import session_state as _SS

PROV_VT_OFF = 0x4390588
PROV_PTR_CACHE = _SS.state_path(_SS.PROV_PTRS_NAME)
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
            _SS.ensure_parent(PROV_PTR_CACHE)
            with open(PROV_PTR_CACHE, "w") as _fh:
                json.dump({"pid": g.p.pid, "base": g.p.base,
                           "ptrs": {str(k): v for k, v in out.items()}},
                          _fh)
        except Exception:
            pass
    _MEM[key] = out
    return out


_CAP_PROV_CACHE = {}
_SS.register_cache("mapdata._CAP_PROV_CACHE", container=_CAP_PROV_CACHE)


def capital_provinces(g):
    """province_id -> tag for every living country's capital state (cached per session/day).

    Linux: `CCountry+0xFF0`；Windows 1.19.5.0 PE: `CCountry+0x1018`（实测 GER=64 柏林,
    CHI=1037 迁都重庆；`+0xFF0` 在 PE 为 `CO_CHARACTERS` 指针）。同时过滤已灭亡无州国
    （`CO_STATES` 与 `CO_STATES_B` 均为空），防止已吞并旧都覆盖存活国。
    """
    import offsets as _O
    cap_off = getattr(_O, "CO_CAPITAL_STATE", None)
    if cap_off is None:
        if sys.platform == "win32":
            return {}
        cap_off = 0xFF0

    p = getattr(g, "p", None)
    pid = getattr(p, "pid", None)
    day_str = ""
    try:
        if hasattr(g, "date"):
            day_str = str((g.date() or {}).get("text") or "")[:10]
    except Exception:
        day_str = ""
    use_cache = isinstance(pid, int) and pid > 0 and getattr(p, "base", None) is not None
    key = (pid, _SS.SESSION.save_epoch, day_str) if use_cache else None
    if key is not None and key in _CAP_PROV_CACHE:
        return _CAP_PROV_CACHE[key]

    out = {}
    try:
        g._load_tags()
        ptrs = g.country_ptrs() or []
        st2p, _ = load_state_provinces()
        co_states = getattr(_O, "CO_STATES", None)
        co_states_b = getattr(_O, "CO_STATES_B", None)
        arr_sz_off = getattr(_O, "PDXARRAY_SIZE", 0x0C)

        cands = []
        for tid, tag in enumerate(g._tags):
            if not tag or tag == "---":
                continue
            try:
                ci = g._tag2idx[tid]
                if 0 <= ci < len(ptrs):
                    cp = int(ptrs[ci])
                    if cp > 0x10000:
                        cands.append((tag, cp))
            except Exception:
                continue

        state_tag = {}
        batched = None
        if len(cands) > 8:
            try:
                import fastmem as _fm
                reqs = []
                for _tag, cp in cands:
                    reqs.append((cp + cap_off, 4))
                    if co_states is not None:
                        reqs.append((cp + co_states + arr_sz_off, 4))
                    if co_states_b is not None:
                        reqs.append((cp + co_states_b + arr_sz_off, 4))
                batched = _fm.read_batch(p, reqs)
            except Exception:
                batched = None

        stride = 1 + (1 if co_states is not None else 0) + (1 if co_states_b is not None else 0)
        if batched is not None and len(batched) == len(cands) * stride:
            # 先探测本进程中是否存在任意存活国（避免离线轻量桩未填 CO_STATES 时误全滤）
            any_alive_known = False
            parsed = []
            for idx, (tag, _cp) in enumerate(cands):
                base_i = idx * stride
                raw_sid = batched[base_i]
                if not raw_sid or len(raw_sid) < 4:
                    continue
                sid = struct.unpack("<i", raw_sid)[0]
                if sid <= 0 or sid >= 100000:
                    continue
                st_cnt = None
                pos = base_i + 1
                if co_states is not None:
                    rb = batched[pos]
                    pos += 1
                    if rb and len(rb) >= 4:
                        v = struct.unpack("<i", rb)[0]
                        if 0 <= v <= 10000:
                            st_cnt = (st_cnt or 0) + v
                if co_states_b is not None:
                    rb = batched[pos]
                    if rb and len(rb) >= 4:
                        v = struct.unpack("<i", rb)[0]
                        if 0 <= v <= 10000:
                            st_cnt = (st_cnt or 0) + v
                if st_cnt is not None and st_cnt > 0:
                    any_alive_known = True
                parsed.append((tag, sid, st_cnt))
            for tag, sid, st_cnt in parsed:
                if any_alive_known and st_cnt is not None and st_cnt <= 0:
                    continue
                state_tag.setdefault(sid, tag)
        else:
            any_alive_known = False
            parsed = []
            for tag, cp in cands:
                try:
                    sid = int(p.i32(cp + cap_off))
                    if sid <= 0 or sid >= 100000:
                        continue
                    st_cnt = None
                    if co_states is not None:
                        try:
                            v = int(p.i32(cp + co_states + arr_sz_off))
                            if 0 <= v <= 10000:
                                st_cnt = (st_cnt or 0) + v
                        except Exception:
                            pass
                    if co_states_b is not None:
                        try:
                            v = int(p.i32(cp + co_states_b + arr_sz_off))
                            if 0 <= v <= 10000:
                                st_cnt = (st_cnt or 0) + v
                        except Exception:
                            pass
                    if st_cnt is not None and st_cnt > 0:
                        any_alive_known = True
                    parsed.append((tag, sid, st_cnt))
                except Exception:
                    continue
            for tag, sid, st_cnt in parsed:
                if any_alive_known and st_cnt is not None and st_cnt <= 0:
                    continue
                state_tag.setdefault(sid, tag)

        for sid, tag in state_tag.items():
            for q in st2p.get(sid, ()):
                out[q] = tag
    except Exception:
        out = {}
    if key is not None:
        if len(_CAP_PROV_CACHE) >= 4:
            _CAP_PROV_CACHE.clear()
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
    import fastmem as _fm
    import offsets_win as _ow
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
    gs_states = getattr(_ow, "GS_STATES", 0x2C8)
    pdx_size = getattr(_ow, "PDXARRAY_SIZE", 0x0C)
    st_owner = getattr(_ow, "CS_OWNER", 0xC8)
    st_ctrl = getattr(_ow, "CS_CONTROLLER", 0xCC)
    st_provs = getattr(_ow, "ST_PROVINCES", 0x18)
    prov_id_off = getattr(_ow, "PROV_ID", PROV_ID)
    try:
        gs = g.gamestate
        arr = p.u64(gs + gs_states)      # GS_STATES, CPdxArray<CState*>
        n = p.i32(gs + gs_states + pdx_size)
    except Exception:
        return out
    try:
        rows = _fm.read_state_provinces_batch(
            p,
            arr,
            max(0, min(n, 4000)),
            off_owner=st_owner,
            off_controller=st_ctrl,
            off_prov_arr=st_provs,
            off_prov_count=st_provs + pdx_size,
            off_prov_id=prov_id_off,
            max_states=4000,
            max_provs_per_state=400,
        )
    except Exception as _e:
        try:
            import diag as _diag
            _diag.warn("mapdata.win_controller_map.batch_fallback", _e)
        except Exception:
            pass
        rows = None
    if rows is not None:
        for _st, _sid, owner_idx, ctrl_idx, pids in rows:
            ctl = ctrl_idx or owner_idx
            t = tag_of(ctl)
            if not t:
                continue
            for pid in pids:
                out[pid] = t
    else:
        for i in range(max(0, min(n, 4000))):
            try:
                st = p.u64(arr + 8 * i)
                if not st:
                    continue
                ctl = p.i32(st + st_ctrl) or p.i32(st + st_owner)
                t = tag_of(ctl)
                if not t:
                    continue
                pa = p.u64(st + st_provs)
                pn = p.i32(st + st_provs + pdx_size)
                if not pa or not (0 < pn <= 400):
                    continue
                for k in range(pn):
                    pr = p.u64(pa + 8 * k)
                    if pr:
                        out[p.i32(pr + prov_id_off)] = t
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


import session_state as _SS_MAP

CENTROID_CACHE = _SS_MAP.state_path("mapcentroids.pkl")
AIRBASE_PROV_CACHE = _SS_MAP.state_path("mapairbases.pkl")
MAP_SCALE_PIXEL_TO_KM = 7.114


def _build_province_centroids_raw(game=GAME, force=False):
    """计算全图每个省在 provinces.bmp 中的像素质心 (cx, cy) 与地图尺寸 (w, h)。

    2026-11-26: 供 Windows PE 伞降只读目标枚举 (paradrop_targets) 零注入计算航程。
    使用 numpy 向量化 bincount 一次完成（~0.4s），结果持久化至磁盘 JSON 缓存。
    """
    import safe_cache
    if not force and safe_cache.exists(CENTROID_CACHE):
        cached = safe_cache.load(CENTROID_CACHE)
        if isinstance(cached, dict) and cached.get("centroids"):
            return cached
    col2id, _ = load_definitions(game)
    img = _read_bmp_rgb(os.path.join(game, "map", "provinces.bmp"))
    h, w, _ = img.shape
    key = (img[:, :, 0].astype(np.uint32) << 16) | (img[:, :, 1].astype(np.uint32) << 8) | img[:, :, 2]
    uniq = np.unique(key)
    lut = {}
    for (r, g, b), pid in col2id.items():
        lut[(r << 16) | (g << 8) | b] = int(pid)
    m = np.zeros(int(uniq.max()) + 1, dtype=np.int32)
    for k in uniq:
        m[int(k)] = lut.get(int(k), 0)
    ids = m[key]
    max_pid = int(ids.max())
    flat = ids.ravel()
    ys, xs = np.divmod(np.arange(flat.size, dtype=np.int64), w)
    cnt = np.bincount(flat, minlength=max_pid + 1)
    sum_x = np.bincount(flat, weights=xs, minlength=max_pid + 1)
    sum_y = np.bincount(flat, weights=ys, minlength=max_pid + 1)
    centroids = {}
    nz = np.nonzero(cnt)[0]
    for pid in nz.tolist():
        if pid <= 0:
            continue
        c = float(cnt[pid])
        centroids[int(pid)] = [round(float(sum_x[pid]) / c, 2), round(float(sum_y[pid]) / c, 2)]
    out = {"width": int(w), "height": int(h), "centroids": centroids}
    safe_cache.save(CENTROID_CACHE, out, fmt="json")
    return out


def _load_air_base_provinces_raw(game=GAME, force=False):
    """解析 map/buildings.txt 中每个州的 air_base 精确像素落点省 ID -> {state_id: prov_id}。"""
    import safe_cache
    if not force and safe_cache.exists(AIRBASE_PROV_CACHE):
        cached = safe_cache.load(AIRBASE_PROV_CACHE)
        if isinstance(cached, dict) and cached:
            return {int(k): int(v) for k, v in cached.items()}
    bp = os.path.join(game, "map", "buildings.txt")
    if not os.path.exists(bp):
        return {}
    col2id, ptype = load_definitions(game)
    img = _read_bmp_rgb(os.path.join(game, "map", "provinces.bmp"))
    h, w, _ = img.shape
    st2p, _ = load_state_provinces(game)
    cdata = province_centroids(game)
    cents = cdata.get("centroids") or {}
    out = {}
    with open(bp, encoding="utf-8", errors="replace") as f:
        for line in f:
            if ";air_base;" not in line:
                continue
            parts = line.strip().split(";")
            if len(parts) < 5:
                continue
            try:
                sid = int(parts[0])
                bx = float(parts[2])
                bz = float(parts[4])
            except ValueError:
                continue
            px = max(0, min(w - 1, int(round(bx))))
            py = max(0, min(h - 1, int(round(h - 1 - bz))))
            rgb = (int(img[py, px, 0]), int(img[py, px, 1]), int(img[py, px, 2]))
            pid = col2id.get(rgb)
            st_provs = st2p.get(sid) or set()
            if pid and (not st_provs or pid in st_provs) and ptype.get(pid) == "land":
                out[sid] = int(pid)
            elif st_provs:
                # 兜底：选该州内距 (px, py) 最近的陆地省
                best_p, best_d2 = None, None
                for cand in st_provs:
                    if ptype.get(cand) != "land":
                        continue
                    xy = cents.get(cand) or cents.get(str(cand))
                    if not xy:
                        continue
                    d2 = (xy[0] - px) ** 2 + (xy[1] - py) ** 2
                    if best_d2 is None or d2 < best_d2:
                        best_p, best_d2 = int(cand), d2
                if best_p is not None:
                    out[sid] = best_p
    if out:
        safe_cache.save(AIRBASE_PROV_CACHE, {str(k): v for k, v in out.items()}, fmt="json")
    return out


def province_centroids(game=GAME, force=False):
    if force:
        _MEM.pop(("centroids", game), None)
        return _once(("centroids", game), lambda: _build_province_centroids_raw(game, True))
    return _once(("centroids", game), lambda: _build_province_centroids_raw(game))


def load_air_base_provinces(game=GAME, force=False):
    if force:
        _MEM.pop(("airbases", game), None)
        return _once(("airbases", game), lambda: _load_air_base_provinces_raw(game, True))
    return _once(("airbases", game), lambda: _load_air_base_provinces_raw(game))


def province_distance_km(pid_a, pid_b, centroids_data=None, game=GAME):
    """两省像素质心之间的地图距离（公里，含东西经度回绕，比例 7.114 km/px）。"""
    import math
    cd = centroids_data if centroids_data is not None else province_centroids(game)
    cents = (cd or {}).get("centroids") or {}
    w = float((cd or {}).get("width") or 5632)
    a = cents.get(int(pid_a)) or cents.get(str(int(pid_a)))
    b = cents.get(int(pid_b)) or cents.get(str(int(pid_b)))
    if not a or not b:
        return None
    dx = abs(float(a[0]) - float(b[0]))
    if w > 0 and dx > w * 0.5:
        dx = w - dx
    dy = float(a[1]) - float(b[1])
    return round(math.hypot(dx, dy) * MAP_SCALE_PIXEL_TO_KM, 1)
