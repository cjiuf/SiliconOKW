#!/usr/bin/env python3
"""Advisor / officer-corps role table (pure file parsing, no memory).

Parses common/characters/*.txt advisor blocks and resolves every trait's numeric
modifiers from common/country_leader/*.txt, including @scripted-variable values
(@chief_experience_gain_medium etc.).
"""
import glob
import os
import re

import config


def _strip(t):
    return re.sub(r"#[^\n]*", "", t)


def _blocks(text):
    for m in re.finditer(r"\n\t([A-Za-z0-9_.]+)\s*=\s*\{", text):
        key = m.group(1)
        i = m.end()
        d = 1
        while d and i < len(text):
            d += 1 if text[i] == "{" else (-1 if text[i] == "}" else 0)
            i += 1
        yield key, text[m.end():i - 1]


def load_traits(game_dir=None):
    game_dir = game_dir or config.game_dir()
    variables = {}
    for p in glob.glob(os.path.join(game_dir, "common", "country_leader", "*.txt")):
        raw = open(p, encoding="utf-8", errors="replace").read()
        for vm in re.finditer(r"^\s*@([A-Za-z0-9_]+)\s*=\s*([+-]?\d+(?:\.\d+)?)", raw, re.M):
            variables[vm.group(1)] = float(vm.group(2))
    mods = {}
    for p in glob.glob(os.path.join(game_dir, "common", "country_leader", "*.txt")):
        t = _strip(open(p, encoding="utf-8", errors="replace").read())
        for key, body in _blocks(t):
            kv = {}
            for m in re.finditer(r"^\s*([A-Za-z0-9_]+)\s*=\s*([+-]?\d+(?:\.\d+)?|@[A-Za-z0-9_]+)\s*$", body, re.M):
                k, v = m.group(1), m.group(2)
                if k in ("sprite", "factor", "base"):
                    continue
                if v.startswith("@"):
                    v = variables.get(v[1:])
                    if v is None:
                        continue
                kv[k] = float(v)
            if kv:
                mods.setdefault(key, {}).update(kv)
    return mods


def _subblock(text, keyword):
    """Raw body of `keyword = { ... }` inside an advisor block, or None."""
    m = re.search(r"\b" + keyword + r"\s*=\s*\{", text)
    if not m:
        return None
    i, d = m.end(), 1
    while d and i < len(text):
        d += 1 if text[i] == "{" else (-1 if text[i] == "}" else 0)
        i += 1
    return text[m.end():i - 1].strip()


def load_advisors(tag="GER", game_dir=None):
    game_dir = game_dir or config.game_dir()
    traits = load_traits(game_dir)
    out = []
    for p in glob.glob(os.path.join(game_dir, "common", "characters", "*%s*.txt" % tag)):
        t = _strip(open(p, encoding="utf-8", errors="replace").read())
        for ckey, body in _blocks(t):
            for am in re.finditer(r"\badvisor\s*=\s*\{", body):
                j = am.end()
                d = 1
                while d and j < len(body):
                    d += 1 if body[j] == "{" else (-1 if body[j] == "}" else 0)
                    j += 1
                ab = body[am.end():j - 1]
                slot = re.search(r"\bslot\s*=\s*([A-Za-z0-9_]+)", ab)
                tok = re.search(r"\bidea_token\s*=\s*([A-Za-z0-9_]+)", ab)
                cost = re.search(r"\bcost\s*=\s*(\d+)", ab)
                tr = re.search(r"\btraits\s*=\s*\{([^}]*)\}", ab)
                tlist = tr.group(1).split() if tr else []
                mods = {}
                for tt in tlist:
                    for mk, mv in traits.get(tt, {}).items():
                        mods[mk] = round(mods.get(mk, 0.0) + mv, 4)
                out.append({
                    "character": ckey,
                    "slot": slot.group(1) if slot else "?",
                    "idea_token": tok.group(1) if tok else ckey,
                    "cost": int(cost.group(1)) if cost else None,
                    "traits": tlist,
                    "modifiers": mods,
                    "army_xp_per_day": mods.get("experience_gain_army", 0.0),
                    # 2026-11-12: the actual gates. legality._eval() understands
                    # has_completed_focus / has_government / NOT / OR / AND; an
                    # unknown key is reported and never blocks the hire.
                    "available_block": _subblock(ab, "available"),
                    "visible_block": _subblock(ab, "visible"),
                })
    out.sort(key=lambda a: (-a["army_xp_per_day"], a["slot"], a["character"]))
    return out


def advisor_table(tag="GER", slot=None, game_dir=None):
    rows = load_advisors(tag, game_dir)
    if slot:
        rows = [r for r in rows if r["slot"] == slot]
    return {"count": len(rows), "advisors": rows}
