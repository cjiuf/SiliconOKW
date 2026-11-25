"""State name, category and building-slot capacity, read from the game's files.

Why this exists
---------------
`hoi4_state_detail` could only report a numeric id plus a building map, so the
2026-11-20 playtest could not answer two questions a player asks constantly:
"42 是哪个州" and "这个州还能不能再盖".  Neither value is in the CState object
in a form worth reverse-engineering (a scan of the first 0x260 bytes finds no
name pointer), but both are declared in the game's own data files:

    history/states/*.txt            id = 64 / name = "STATE_64" / state_category = metropolis
    common/state_category/*.txt     local_building_slots = 12
    common/buildings/*.txt          <building> = { ... shares_slots = yes ... }

That is the same source the UI reads, so the numbers agree with the tooltip by
construction rather than by guessing an offset.

Everything is cached per process; the first call pays ~1 s for 1000+ state
files, every later call is a dict lookup.
"""
import os
import pickle
import re

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_PATH = os.path.join(HERE, "statemeta.pkl")
SCHEMA = 1

_BLOCK = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\s*=\s*\{")
_COMMENT = re.compile(r"#[^\n]*")

_CACHE = {"game_dir": None, "states": None, "categories": None, "slot_buildings": None, "disk_tried": False}


def reset_cache():
    """清空内存缓存（例如通过 hoi4_recalc assets 重建 statemeta.pkl 后刷新）。"""
    _CACHE.update(game_dir=None, states=None, categories=None, slot_buildings=None, disk_tried=False)


def _try_load_disk_cache():
    """优先从 `hoi4_recalc assets` 生成的缓存秒读（~1ms 替代 ~1s 冷扫）。"""
    if _CACHE["disk_tried"]:
        return
    _CACHE["disk_tried"] = True
    import safe_cache
    if not safe_cache.exists(CACHE_PATH):
        return
    try:
        d = safe_cache.load(CACHE_PATH)
        if (
            isinstance(d, dict)
            and d.get("schema") == SCHEMA
            and isinstance(d.get("states"), dict)
            and isinstance(d.get("categories"), dict)
            and isinstance(d.get("slot_buildings"), (set, list, tuple))
        ):
            _CACHE["states"] = d["states"]
            _CACHE["categories"] = d["categories"]
            _CACHE["slot_buildings"] = set(d["slot_buildings"])
    except (safe_cache.UnsafePickleError, safe_cache.CacheIntegrityError):
        raise
    except Exception:
        pass


def _game_dir():
    if _CACHE["game_dir"]:
        return _CACHE["game_dir"]
    d = None
    try:
        import config
        d = config.game_dir(required=False)
    except Exception:
        d = None
    if not d:
        d = os.environ.get("HOI4_PATH")
    _CACHE["game_dir"] = d
    return d


def _read(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


def _blocks(text):
    """Yield (name, body) for every `name = { ... }` block, brace-matched."""
    text = _COMMENT.sub("", text)
    pos = 0
    while True:
        m = _BLOCK.search(text, pos)
        if not m:
            return
        depth, j = 1, m.end()
        while j < len(text) and depth:
            c = text[j]
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
            j += 1
        yield m.group(1), text[m.end(): j - 1]
        pos = j


def _num(body, key, default=None):
    m = re.search(r"\b%s\s*=\s*(-?\d+(?:\.\d+)?)" % re.escape(key), body)
    if not m:
        return default
    v = float(m.group(1))
    return int(v) if v == int(v) else v


def _str(body, key):
    m = re.search(r'\b%s\s*=\s*"([^"]*)"' % re.escape(key), body)
    return m.group(1) if m else None


def _ident(body, key):
    """Unquoted script token, e.g. `state_category = town`."""
    m = re.search(r"\b%s\s*=\s*([A-Za-z_][A-Za-z0-9_]*)" % re.escape(key), body)
    return m.group(1) if m else None


def _load_categories():
    if _CACHE["categories"] is None:
        _try_load_disk_cache()
    if _CACHE["categories"] is not None:
        return _CACHE["categories"]
    out = {}
    g = _game_dir()
    if g:
        d = os.path.join(g, "common", "state_category")
        try:
            names = os.listdir(d)
        except OSError:
            names = []
        for fn in names:
            if not fn.endswith(".txt"):
                continue
            body = _read(os.path.join(d, fn))
            slots = _num(body, "local_building_slots")
            if slots is not None:
                out[fn[:-4]] = int(slots)
    _CACHE["categories"] = out
    return out


def _load_slot_buildings():
    """Building keys that CONSUME a state building slot (`shares_slots = yes`)."""
    if _CACHE["slot_buildings"] is None:
        _try_load_disk_cache()
    if _CACHE["slot_buildings"] is not None:
        return _CACHE["slot_buildings"]
    out = set()
    g = _game_dir()
    if g:
        d = os.path.join(g, "common", "buildings")
        try:
            names = os.listdir(d)
        except OSError:
            names = []
        for fn in names:
            if not fn.endswith(".txt"):
                continue
            for name, body in _blocks(_read(os.path.join(d, fn))):
                if name == "buildings":
                    # the file wraps everything in one `buildings = { ... }` block
                    for sub, sub_body in _blocks(body):
                        if re.search(r"\bshares_slots\s*=\s*yes\b", sub_body):
                            out.add(sub)
                    continue
                if re.search(r"\bshares_slots\s*=\s*yes\b", body):
                    out.add(name)
    _CACHE["slot_buildings"] = out
    return out


def _load_states():
    if _CACHE["states"] is None:
        _try_load_disk_cache()
    if _CACHE["states"] is not None:
        return _CACHE["states"]
    out = {}
    g = _game_dir()
    if g:
        d = os.path.join(g, "history", "states")
        try:
            names = os.listdir(d)
        except OSError:
            names = []
        for fn in names:
            if not fn.endswith(".txt"):
                continue
            text = _COMMENT.sub("", _read(os.path.join(d, fn)))
            sid = _num(text, "id")
            if sid is None:
                continue
            out[int(sid)] = {
                "name_key": _str(text, "name"),
                "category": (_str(text, "state_category")
                             or _ident(text, "state_category") or "").strip() or None,
            }
    _CACHE["states"] = out
    return out


def state_meta(state_id, buildings=None, slots_bonus=None):
    """Name / category / slot accounting for one state id.

    `buildings` is the {type: level} map `state_detail` already produced;
    `slots_bonus` is the country's `global_building_slots_factor` (0.2 = +20%).
    Returns {} when the game directory cannot be read, so callers can simply
    merge the dict into their answer without special-casing.
    """
    row = _load_states().get(int(state_id))
    if not row:
        return {}
    out = {"name_key": row["name_key"], "state_category": row["category"]}
    try:
        import loc
        if row["name_key"]:
            out["name"] = loc.get(row["name_key"], row["name_key"])
    except Exception:
        out["name"] = row["name_key"]
    base = _load_categories().get(row["category"])
    if base is None:
        return out
    out["building_slots"] = int(base)
    if slots_bonus:
        # the game applies the country modifier to the BASE and floors it
        out["building_slots_bonus"] = int(base * float(slots_bonus))
        out["building_slots_total"] = int(base + int(base * float(slots_bonus)))
    else:
        out["building_slots_total"] = int(base)
    if buildings:
        shares = _load_slot_buildings()
        used = 0
        for k, v in buildings.items():
            if k in shares:
                used += int(v or 0)
        out["slots_used"] = used
        out["slots_free"] = int(out["building_slots_total"]) - used
    return out
