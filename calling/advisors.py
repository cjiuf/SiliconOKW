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


_TRAITS_CACHE = {}
_ADVISORS_CACHE = {}


def clear_caches():
    """清空静态顾问/特质/理念目录进程内缓存。"""
    _TRAITS_CACHE.clear()
    _ADVISORS_CACHE.clear()
    _IDEAS_CATALOG_CACHE.clear()
    _LEADER_TRAITS_CACHE.clear()


def load_traits(game_dir=None):
    game_dir = game_dir if game_dir is not None else config.game_dir(required=False)
    if not game_dir:
        return {}
    if game_dir in _TRAITS_CACHE:
        return _TRAITS_CACHE[game_dir]
    variables = {}
    raw_files = []
    for p in glob.glob(os.path.join(game_dir, "common", "country_leader", "*.txt")):
        with open(p, encoding="utf-8", errors="replace") as _fh:
            raw = _fh.read()
        raw_files.append(raw)
        for vm in re.finditer(r"^\s*@([A-Za-z0-9_]+)\s*=\s*([+-]?\d+(?:\.\d+)?)", raw, re.M):
            variables[vm.group(1)] = float(vm.group(2))
    mods = {}
    for raw in raw_files:
        t = _strip(raw)
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
    _TRAITS_CACHE[game_dir] = mods
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
    game_dir = game_dir if game_dir is not None else config.game_dir(required=False)
    if not game_dir:
        return []
    t_up = str(tag or "GER").upper()
    cache_key = (game_dir, t_up)
    if cache_key in _ADVISORS_CACHE:
        return _ADVISORS_CACHE[cache_key]
    traits = load_traits(game_dir)
    out = []
    for p in glob.glob(os.path.join(game_dir, "common", "characters", "*%s*.txt" % t_up)):
        with open(p, encoding="utf-8", errors="replace") as _fh:
            t = _strip(_fh.read())
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
    _ADVISORS_CACHE[cache_key] = out
    return out


def advisor_table(tag="GER", slot=None, game_dir=None):
    rows = load_advisors(tag, game_dir)
    if slot:
        rows = [r for r in rows if r["slot"] == slot]
    return {"count": len(rows), "advisors": rows}


# 常见核心法案离线兜底表（无游戏安装目录的纯离线单测环境下也可精确校验槽位与费用）
_BUILTIN_LAW_SLOTS = {
    "economy": (
        "undisturbed_isolation", "isolation", "civilian_economy",
        "low_economic_mobilisation", "partial_economic_mobilisation",
        "war_economy", "autarkic_economy", "tot_economic_mobilisation",
        "totaler_krieg_economy",
    ),
    "mobilization_laws": (
        "disarmed_nation", "volunteer_only", "limited_conscription",
        "extensive_conscription", "service_by_requirement",
        "all_adults_serve", "all_men_serve", "scraping_the_barrel",
    ),
    "trade_laws": (
        "free_trade", "export_focus", "limited_exports", "closed_economy",
    ),
}

_IDEAS_CATALOG_CACHE = {}
_LEADER_TRAITS_CACHE = {}


def _iter_named_blocks(text):
    """解析顶层或一级子块中的 `key = { ... }`，返回 (key, body) 列表。"""
    out = []
    n = len(text)
    i = 0
    while i < n:
        ch = text[i]
        if ch.isspace():
            i += 1
            continue
        if ch.isalpha() or ch == "_":
            j = i
            while j < n and (text[j].isalnum() or text[j] in "_."):
                j += 1
            key = text[i:j]
            k = j
            while k < n and text[k] in " \t\r\n":
                k += 1
            if k < n and text[k] == "=":
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
                    out.append((key, text[start:q - 1]))
                    i = q
                    continue
                # 跳过简单值
                while k < n and text[k] not in " \t\r\n{}":
                    k += 1
                i = k
                continue
            i = j
            continue
        i += 1
    return out


def load_ideas_catalog(game_dir=None):
    """解析 `common/ideas/*.txt`，返回 `{idea_key: {key, slot, is_law, cost, ...}}`。

    含进程级内存缓存；无游戏目录时返回核心经济/征兵/贸易法案兜底表，确保离线可测。
    """
    gdir = game_dir if game_dir is not None else config.game_dir(required=False)
    cache_key = gdir or "__offline__"
    if cache_key in _IDEAS_CATALOG_CACHE:
        return _IDEAS_CATALOG_CACHE[cache_key]

    catalog = {}
    slot_members = {}

    # 预填核心法案槽位兜底，后续从真实脚本覆写精化
    for slot_name, keys in _BUILTIN_LAW_SLOTS.items():
        slot_members.setdefault(slot_name, set()).update(keys)
        for k in keys:
            catalog[k] = {
                "key": k,
                "slot": slot_name,
                "is_law": True,
                "cost": 150,
                "file": "_builtin",
            }

    if gdir:
        ideas_dir = os.path.join(gdir, "common", "ideas")
        if os.path.isdir(ideas_dir):
            for path in sorted(glob.glob(os.path.join(ideas_dir, "*.txt"))):
                fname = os.path.basename(path)
                try:
                    with open(path, "r", encoding="utf-8", errors="replace") as fh:
                        raw = _strip(fh.read())
                except OSError:
                    continue
                for top_k, top_body in _iter_named_blocks(raw):
                    if top_k != "ideas":
                        continue
                    for slot_name, slot_body in _iter_named_blocks(top_body):
                        is_law = bool(
                            slot_name in _BUILTIN_LAW_SLOTS
                            or re.search(r"\blaw\s*=\s*yes\b", slot_body)
                        )
                        for idea_key, idea_body in _iter_named_blocks(slot_body):
                            if idea_key in ("allowed", "available", "visible", "modifier", "rule"):
                                continue
                            m_cost = re.search(r"\bcost\s*=\s*(\d+)", idea_body)
                            cost = int(m_cost.group(1)) if m_cost else (150 if slot_name != "country" else 0)
                            catalog[idea_key] = {
                                "key": idea_key,
                                "slot": slot_name,
                                "is_law": is_law,
                                "cost": cost,
                                "file": fname,
                            }
                            if slot_name != "country":
                                slot_members.setdefault(slot_name, set()).add(idea_key)

    # 填充同槽互斥列表（仅对法案与单槽设计商槽位标注，country 精神不互斥）
    for key, rec in catalog.items():
        sl = rec.get("slot")
        if sl and sl != "country" and sl in slot_members:
            peers = sorted(x for x in slot_members[sl] if x != key)
            rec["mutually_exclusive_with"] = peers[:40]
        else:
            rec["mutually_exclusive_with"] = []

    _IDEAS_CATALOG_CACHE[cache_key] = catalog
    return catalog


def load_leader_traits(tag="GER", game_dir=None):
    """解析 `common/characters/*{tag}*.txt` 中陆军将领/元帅的静态特质列表。

    返回 `{character_key_lower: {"role": ..., "traits": [...], "skill": int|None}}`。
    """
    gdir = game_dir if game_dir is not None else config.game_dir(required=False)
    t_up = str(tag or "GER").upper()
    cache_key = (gdir or "__offline__", t_up)
    if cache_key in _LEADER_TRAITS_CACHE:
        return _LEADER_TRAITS_CACHE[cache_key]

    out = {}
    if gdir:
        cdir = os.path.join(gdir, "common", "characters")
        if os.path.isdir(cdir):
            for path in sorted(glob.glob(os.path.join(cdir, "*%s*.txt" % t_up))):
                try:
                    with open(path, "r", encoding="utf-8", errors="replace") as fh:
                        raw = _strip(fh.read())
                except OSError:
                    continue
                for ckey, body in _blocks(raw):
                    for role in ("field_marshal", "corps_commander", "navy_leader"):
                        sub = _subblock(body, role)
                        if not sub:
                            continue
                        tr = re.search(r"\btraits\s*=\s*\{([^}]*)\}", sub)
                        tlist = [x.strip() for x in tr.group(1).split() if x.strip()] if tr else []
                        m_sk = re.search(r"\bskill\s*=\s*(\d+)", sub)
                        out[ckey.lower()] = {
                            "key": ckey,
                            "role": role,
                            "traits": tlist,
                            "skill": int(m_sk.group(1)) if m_sk else None,
                        }
                        break

    _LEADER_TRAITS_CACHE[cache_key] = out
    return out
