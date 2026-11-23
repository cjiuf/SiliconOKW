"""Localisation lookup for HOI4 (key -> display text), read from the game's files.

Why this module exists
----------------------
Several tools only ever had the internal script key: ``hoi4_decisions`` printed
``GER_plan_z`` and nothing else, so there was no way to tell a real decision from
a debug one.  The engine's own localisation files carry the answer, but they are
209 files / 14 MB of ``KEY:0 "Text"`` lines and every caller that needs a title
should not re-read them.

No game calls, no ptrace.  The index is built once and cached next to this file
(``loc.pkl``); the cache records the language and the source file count so a
patched game or a language switch rebuilds it.

Priority: the language directory first (English by default), then
``localisation/replace`` -- which is exactly how the game applies overrides.
"""
from __future__ import annotations

import os
import pickle
import re

import config

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, "loc.pkl")
SCHEMA = 1

# KEY:0 "Text"   /   KEY:1 "Text"     (leading whitespace, BOM handled by utf-8-sig)
LINE_RE = re.compile(r'^\s*([A-Za-z0-9_.\-]+):\d*\s*"(.*)"\s*$')


def _language_dirs(game):
    """Language directories, English first (the game ships english + 8 others)."""
    d = os.path.join(game, "localisation")
    if not os.path.isdir(d):
        return []
    langs = [x for x in os.listdir(d) if os.path.isdir(os.path.join(d, x))
             and x.lower() != "replace"]
    langs.sort(key=lambda x: (0 if x.lower().startswith("english") else 1, x))
    return [os.path.join(d, x) for x in langs]


def _read_dir(path, out):
    try:
        files = sorted(f for f in os.listdir(path) if f.lower().endswith(".yml"))
    except OSError:
        return 0
    for fn in files:
        try:
            with open(os.path.join(path, fn), encoding="utf-8-sig",
                      errors="replace") as f:
                for line in f:
                    m = LINE_RE.match(line)
                    if m:
                        out[m.group(1)] = m.group(2).replace('\\"', '"')
        except OSError:
            continue
    return len(files)


def build(force=False):
    game = config.game_dir()
    dirs = _language_dirs(game)
    if not dirs:
        return {"schema": SCHEMA, "lang": None, "count": 0, "keys": {}}
    lang_dir = dirs[0]
    if not force and os.path.exists(CACHE):
        try:
            with open(CACHE, "rb") as f:
                cached = pickle.load(f)
            if (isinstance(cached, dict) and cached.get("schema") == SCHEMA
                    and (cached.get("lang") == os.path.basename(lang_dir)
                         or (cached.get("source") == "hoi4_recalc" and cached.get("lang")))):
                return cached
        except Exception:
            pass
    keys = {}
    n = _read_dir(lang_dir, keys)
    rep = os.path.join(game, "localisation", "replace")
    if os.path.isdir(rep):
        _read_dir(rep, keys)
    data = {"schema": SCHEMA, "lang": os.path.basename(lang_dir),
            "files": n, "count": len(keys), "keys": keys}
    try:
        with open(CACHE, "wb") as f:
            pickle.dump(data, f, protocol=4)
    except OSError:
        pass
    return data


_MEM = None


def load(force=False):
    global _MEM
    if force or _MEM is None:
        _MEM = build(force)
    return _MEM


def get(key, default=None):
    if not key:
        return default
    return load()["keys"].get(key, default)


def get_many(keys):
    k = load()["keys"]
    return {x: k[x] for x in keys if x in k}


def title(key):
    """Display text for a script key, with `_desc` fallback and a safe default."""
    if not key:
        return None
    k = load()["keys"]
    return k.get(key) or k.get(key + "_title") or None


def desc(key):
    if not key:
        return None
    return load()["keys"].get(key + "_desc")
