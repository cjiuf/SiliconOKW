"""Static strategic-naval-region catalogue for HOI4, read from the game's own files.

Why this module exists
----------------------
``hoi4_naval_mission`` needs numeric strategic-region ids, and it validates them
against the live ``CStrategicNavalRegion`` table because an out-of-range id is a
NULL DEREFERENCE inside ``CNavalMissionAddRegionCommand::CanExecute`` (that is the
crash that killed hoi4.exe on 2026-11-16).  The ids are dense and undocumented --
before this module there was no way to find them from the tool side, so nobody
ever gave the fleet a mission.

Sources (no game calls, no ptrace):
  map/strategicregions/*.txt   -> id, name key, naval_terrain, province list
  localisation/<lang>/*.yml    -> STRATEGICREGION_<id> display name

The live table is still the authority on whether an id is *usable right now*
(navalops_win.region_is_valid).  This catalogue only answers "which ids exist and
what are they called", which is what a caller needs to pick one.
"""
from __future__ import annotations

import os
import pickle
import re

import config

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, "navalregions.pkl")
SCHEMA = 1


def _strip_comments(text):
    return re.sub(r"#[^\n]*", "", text)


def _iter_blocks(text, key):
    """Yield the body of every ``key = { ... }`` block (nesting-aware)."""
    pat = re.compile(r"\b%s\s*=\s*\{" % re.escape(key))
    for m in pat.finditer(text):
        i = m.end()
        start = i
        depth = 1
        n = len(text)
        while i < n and depth:
            ch = text[i]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
            i += 1
        if depth == 0:
            yield text[start:i - 1]


def _languages(game):
    d = os.path.join(game, "localisation")
    if not os.path.isdir(d):
        return []
    langs = [x for x in os.listdir(d) if os.path.isdir(os.path.join(d, x))]
    # English first, then anything else as a fallback.
    langs.sort(key=lambda x: (0 if x.lower().startswith("english") else 1, x))
    return langs


def _parse_names(game):
    """STRATEGICREGION_<id> -> localised display name."""
    out = {}
    for lang in _languages(game)[:2]:
        d = os.path.join(game, "localisation", lang)
        try:
            files = [f for f in os.listdir(d) if f.lower().endswith(".yml")]
        except OSError:
            continue
        for fn in files:
            try:
                raw = open(os.path.join(d, fn), encoding="utf-8-sig",
                           errors="replace").read()
            except OSError:
                continue
            for m in re.finditer(
                    r'^\s*(STRATEGICREGION_\d+):\d*\s*"(.*?)"\s*$',
                    raw, re.MULTILINE):
                out.setdefault(m.group(1), m.group(2))
        if out:
            break
    return out


def _parse_regions(game):
    d = os.path.join(game, "map", "strategicregions")
    by_id = {}
    if not os.path.isdir(d):
        return by_id
    for fn in sorted(os.listdir(d)):
        if not fn.lower().endswith(".txt"):
            continue
        try:
            text = _strip_comments(
                open(os.path.join(d, fn), encoding="utf-8", errors="replace").read())
        except OSError:
            continue
        for body in _iter_blocks(text, "strategic_region"):
            m = re.search(r"\bid\s*=\s*(\d+)", body)
            if not m:
                continue
            rid = int(m.group(1))
            nm = re.search(r'\bname\s*=\s*"([^"]+)"', body)
            terr = re.search(r"\bnaval_terrain\s*=\s*([a-zA-Z_]+)", body)
            prov = re.search(r"\bprovinces\s*=\s*\{(.*?)\}", body, re.S)
            by_id[rid] = {
                "id": rid,
                "name_key": nm.group(1) if nm else None,
                "naval_terrain": terr.group(1) if terr else None,
                "provinces": len((prov.group(1) or "").split()) if prov else 0,
            }
    return by_id


def build(force=False):
    if not force and os.path.exists(CACHE):
        try:
            with open(CACHE, "rb") as f:
                cached = pickle.load(f)
            if isinstance(cached, dict) and cached.get("schema") == SCHEMA:
                return cached
        except Exception:
            pass
    game = config.game_dir()
    regions = _parse_regions(game)
    names = _parse_names(game)
    for rid, rec in regions.items():
        key = rec.get("name_key") or ("STRATEGICREGION_%d" % rid)
        rec["name"] = names.get(key) or key
        rec["name_key"] = key
    by_name = {}
    for rid, rec in regions.items():
        by_name.setdefault((rec.get("name") or "").strip().lower(), rid)
        by_name.setdefault(rec["name_key"].strip().lower(), rid)
    data = {"schema": SCHEMA, "regions": regions, "by_name": by_name}
    try:
        with open(CACHE, "wb") as f:
            pickle.dump(data, f)
    except OSError:
        pass
    return data


_MEM = None


def load(force=False):
    global _MEM
    if force or _MEM is None:
        _MEM = build(force)
    return _MEM


def lookup(name_or_id):
    """Region id for an id, a display name ("North Sea") or a name key."""
    data = load()
    if isinstance(name_or_id, int) or (
            isinstance(name_or_id, str) and name_or_id.strip().isdigit()):
        return int(name_or_id)
    return data["by_name"].get(str(name_or_id).strip().lower())


def catalogue(valid_ids=None):
    """All known regions, annotated with whether the live table says they are usable.

    `valid_ids` is the set from navalops_win.valid_regions(); pass None when the
    game is not reachable and every region is reported as "unknown".
    """
    data = load()
    valid = None if valid_ids is None else set(int(x) for x in valid_ids)
    out = []
    for rid in sorted(data["regions"]):
        rec = dict(data["regions"][rid])
        rec["usable_now"] = None if valid is None else (rid in valid)
        out.append(rec)
    return out
