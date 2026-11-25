"""Static national-focus planning data for HOI4 1.19.4.1.

Two sources, both read from the game's own files:

  common/ai_strategy_plans/*.txt   -> ai_national_focuses ordered list
  common/national_focus/*.txt      -> focus id, cost (days), reward flags

No game calls.  The result is used by Game.selectable_focuses() to rank the
focuses that are already selectable in memory.
"""
from __future__ import annotations
import os, pickle, re

import config

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, "focusplans.pkl")

DATE_RE = re.compile(r"\b([A-Z][A-Za-z0-9_]{2,})\b")


def _strip_comments(text):
    return re.sub(r"#[^\n]*", "", text)


def _block_after(text, key):
    """Return the body of `key = { ... }` (first occurrence), or ''."""
    blocks = _all_blocks_after(text, key)
    return blocks[0] if blocks else ""


def _all_blocks_after(text, key):
    """Every `key = { ... }` body in the file.

    A strategy plan can carry more than one `ai_national_focuses` block (one per
    DLC branch / difficulty). Taking only the first one silently dropped the
    historical core of the tree, which is how GER_anschluss and
    GER_demand_sudetenland fell out of the plan.
    """
    out = []
    pos = 0
    pat = re.compile(r"\b%s\s*=\s*\{" % re.escape(key))
    while True:
        m = pat.search(text, pos)
        if not m:
            break
        i = m.end()
        start = i
        depth = 1
        n = len(text)
        while i < n and depth:
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
            i += 1
        if depth == 0:
            out.append(text[start:i - 1])
        pos = i
    return out


def _iter_focus_blocks(text):
    """Yield focus/shared_focus blocks at depth 1 (inside focus_tree = {...})."""
    text = _strip_comments(text)
    i, n, depth = 0, len(text), 0
    while i < n:
        ch = text[i]
        if ch == "{":
            depth += 1
            i += 1
            continue
        if ch == "}":
            depth = max(0, depth - 1)
            i += 1
            continue
        if depth == 1 and (ch.isalpha() or ch == "_"):
            j = i
            while j < n and (text[j].isalnum() or text[j] == "_"):
                j += 1
            name = text[i:j]
            k = j
            while k < n and text[k] in " \t\r\n":
                k += 1
            if name in ("focus", "shared_focus") and k < n and text[k] == "=":
                k += 1
                while k < n and text[k] in " \t\r\n":
                    k += 1
                if k < n and text[k] == "{":
                    start = k + 1
                    d = 1
                    q = start
                    while q < n and d:
                        if text[q] == "{":
                            d += 1
                        elif text[q] == "}":
                            d -= 1
                        q += 1
                    yield text[start:q - 1]
                    i = q
                    continue
            i = j
            continue
        i += 1


def _parse_prereq_groups(body):
    """`prerequisite = { focus = a focus = b }` blocks.

    Outer list = AND (several `prerequisite` blocks must ALL be met).
    Inner list = OR  (several `focus =` inside ONE block: any one is enough).
    That is the actual HOI4 grammar, and getting it wrong is why the old
    "A 选错整条链就废" note was wrong about Neurath vs reorganize.
    """
    groups = []
    for blk in _all_blocks_after(body, "prerequisite"):
        keys = re.findall(r"\bfocus\s*=\s*([A-Za-z0-9_]+)", blk)
        if keys:
            groups.append(keys)
    return groups


def _parse_mutex(body):
    blk = _block_after(body, "mutually_exclusive")
    if not blk:
        return []
    return re.findall(r"\bfocus\s*=\s*([A-Za-z0-9_]+)", blk)


def _parse_focus(body):
    m = re.search(r"\bid\s*=\s*([A-Za-z0-9_]+)", body)
    if not m:
        return None, None
    key = m.group(1)
    m = re.search(r"\bcost\s*=\s*(\d+)", body)
    cost = int(m.group(1)) if m else 10
    low = body.lower()
    info = {
        "id": key,
        "days": max(1, cost * 7),
        "research_slot": bool(re.search(r"\badd_research_slot\s*=", low)),
        "annex": bool(re.search(r"\bannex_country\s*=", low)),
        "wargoal": bool(re.search(r"\bcreate_wargoal\s*=", low)),
        "industry": ("industrial_complex" in low or "arms_factory" in low),
        "air_naval": any(k in key.lower() for k in (
            "luftwaffe", "kriegsmarine", "naval", "air", "bomber", "fighter",
            "carrier", "submarine", "u_boat")),
        # 2026-11-18: focus_detail used to read prerequisites out of live memory
        # with unverified offsets and returned binary garbage. The script files
        # are the authority anyway, so parse them here.
        "prereq_groups": _parse_prereq_groups(body),
        "mutex": _parse_mutex(body),
    }
    for axis in ("x", "y"):
        mm = re.search(r"\b%s\s*=\s*(-?\d+)" % axis, body)
        if mm:
            info[axis] = int(mm.group(1))
    return key, info


def _plan_file(game, tag):
    d = os.path.join(game, "common", "ai_strategy_plans")
    if not os.path.isdir(d):
        return None
    cand = [f for f in os.listdir(d)
            if f.lower().startswith(tag.lower() + "_") and f.endswith(".txt")]
    if not cand:
        return None
    def rank(fn):
        n = fn.lower()
        return (0 if "historical" in n else 1 if "default" in n else 2, n)
    return os.path.join(d, sorted(cand, key=rank)[0])


def _parse_plan(path):
    with open(path, encoding="utf-8", errors="replace") as f:
        text = _strip_comments(f.read())
    out = []
    for body in _all_blocks_after(text, "ai_national_focuses"):
        for m in DATE_RE.finditer(body):
            k = m.group(1)
            if k not in out:
                out.append(k)
    return out


SCHEMA = 2          # bump when the cached shape changes -> forces a rebuild


def build(force=False):
    import safe_cache
    if not force and safe_cache.exists(CACHE):
        try:
            cached = safe_cache.load(CACHE)
            if isinstance(cached, dict) and cached.get("schema") == SCHEMA:
                return cached
        except (safe_cache.UnsafePickleError, safe_cache.CacheIntegrityError):
            raise
        except Exception:
            pass
    game = config.game_dir()
    focuses = {}
    fd = os.path.join(game, "common", "national_focus")
    for fn in sorted(os.listdir(fd)):
        if not fn.endswith(".txt"):
            continue
        try:
            text = open(os.path.join(fd, fn), encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        for body in _iter_focus_blocks(text):
            key, info = _parse_focus(body)
            if key:
                focuses[key] = info
    plans = {}
    pd = os.path.join(game, "common", "ai_strategy_plans")
    if os.path.isdir(pd):
        tags = sorted({f.split("_", 1)[0].upper() for f in os.listdir(pd)
                       if f.endswith(".txt") and len(f.split("_", 1)[0]) == 3})
        for tag in tags:
            # Deterministic: _plan_file ranks *historical* above *default*, so an
            # alphabetically-later alternate plan can no longer overwrite the
            # historical focus order (os.listdir order is not stable).
            path = _plan_file(game, tag)
            if not path:
                continue
            try:
                order = _parse_plan(path)
            except OSError:
                continue
            if order:
                plans[tag] = order
    data = {"schema": SCHEMA, "focuses": focuses, "plans": plans}
    safe_cache.save(CACHE, data, fmt="json.gz")
    return data


_MEM = None
def load(force=False):
    global _MEM
    if force or _MEM is None:
        _MEM = build(force)
    return _MEM

