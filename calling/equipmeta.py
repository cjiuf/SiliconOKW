"""HOI4 装备代差、科技解锁与产线/空军/燃油策略提醒分析模块。

纯静态脚本解析（common/units/equipment/*.txt 与 common/technologies/*.txt，带本地
pickle 缓存，冷加载 <0.15s，热加载 <5ms）+ 纯内存数据比对，绝不做任何引擎注入。

覆盖三大策略提醒场景（复测 5 专题 §8）：
  1. 装备代差（OBSOLETE_LINE）：基于真实 archetype / parent / year / 战斗属性，
     精确计算升级目标、属性增益、换装效率损失与回本天数，给出可直接执行的命令。
  2. 库存严重过剩（SURPLUS）：识别仓库存量远超安全储备且仍占大量军工的产线，
     给出建议保留厂数与可腾挪厂数（surplus_factories）。
  3. 空军补充断层（AIR_GAP）与燃油枯竭（FUEL_ZERO）：识别在编机型月补充率过低，
     以及燃油归零但海空仍在挂任务的致命浪费。
"""
from __future__ import annotations

import glob
import os
import pickle
import re

_CACHE_VERSION = 1
_MEM_CACHE = {}

# 装备属性中用于生成人可读升级增益的字段（按战术重要性排序）
_STAT_LABELS = (
    ("soft_attack", "软攻"),
    ("hard_attack", "硬攻"),
    ("breakthrough", "突破"),
    ("defense", "防御"),
    ("ap_attack", "穿甲"),
    ("armor_value", "装甲"),
    ("air_attack", "对空"),
    ("air_defence", "空防"),
    ("air_agility", "机动"),
    ("air_ground_attack", "对地"),
    ("air_bombing", "轰炸"),
    ("naval_strike_attack", "对海"),
    ("maximum_speed", "速度"),
    ("reliability", "可靠性"),
)

_NUM_FIELDS = {k for k, _ in _STAT_LABELS} | {
    "year", "visual_level", "priority", "build_cost_ic",
}

# 用于防噪音去重：key = (pid, tag) -> set of (line_index, archetype, cur_eq, upgrade_to)
_SEEN_OBSOLETE = {}

# 各类别单师安全备货基准（用于判定仓库盈余 SURPLUS，配合 div_count 与下限）
_SURPLUS_PER_DIV = (
    ("infantry_equipment", 200.0, 5000.0),
    ("support_equipment", 15.0, 800.0),
    ("artillery_equipment", 18.0, 800.0),
    ("anti_air_equipment", 12.0, 600.0),
    ("anti_tank_equipment", 12.0, 600.0),
    ("motorized_equipment", 15.0, 1000.0),
    ("mechanized_equipment", 15.0, 800.0),
    ("light_tank", 12.0, 600.0),
    ("medium_tank", 12.0, 600.0),
)


def _game_dir():
    try:
        import mapdata
        if getattr(mapdata, "GAME", None) and os.path.isdir(mapdata.GAME):
            return mapdata.GAME
    except Exception:
        pass
    for cand in (
        os.environ.get("HOI4_GAME_DIR"),
        r"D:\SteamLibrary\steamapps\common\Hearts of Iron IV",
        r"C:\Program Files (x86)\Steam\steamapps\common\Hearts of Iron IV",
    ):
        if cand and os.path.isdir(cand):
            return cand
    return None


def _strip_comments(text: str) -> str:
    """逐行剥离 Paradox 脚本的 # 注释，彻底避开被整段注释掉的旧装备定义。"""
    out = []
    for line in text.splitlines():
        idx = line.find("#")
        if idx >= 0:
            line = line[:idx]
        s = line.strip()
        if s:
            out.append(s)
    return "\n".join(out)


def _iter_top_blocks(body: str, container_key: str):
    """从 container_key = { key1 = { ... } key2 = { ... } } 中切出所有一级子块。"""
    m = re.search(r"\b%s\s*=\s*\{" % re.escape(container_key), body)
    if not m:
        return
    pos = m.end()
    n = len(body)
    depth = 1
    i = pos
    while i < n and depth > 0:
        ch = body[i]
        if ch == "}":
            depth -= 1
            i += 1
            continue
        if ch.isspace():
            i += 1
            continue
        if depth == 1:
            # 跳过 @var = value 宏定义
            if ch == "@":
                nl = body.find("\n", i)
                i = n if nl < 0 else nl + 1
                continue
            km = re.match(r"([A-Za-z0-9_.\-]+)\s*=\s*\{", body[i:])
            if km:
                key = km.group(1)
                b_start = i + km.end()
                b_depth = 1
                j = b_start
                while j < n and b_depth > 0:
                    c2 = body[j]
                    if c2 == "{":
                        b_depth += 1
                    elif c2 == "}":
                        b_depth -= 1
                    j += 1
                yield key, body[b_start:j - 1]
                i = j
                continue
        if ch == "{":
            depth += 1
        i += 1


def _top_level_kv(block: str):
    """提取块内顶层（大括号深度 0）的简单 key = value 键值对与简单单层列表。"""
    kv = {}
    lists = {}
    depth = 0
    i = 0
    n = len(block)
    buf = []
    while i < n:
        ch = block[i]
        if ch == "{":
            if depth == 0:
                prefix = "".join(buf).strip()
                buf = []
                lm = re.search(r"([A-Za-z0-9_.\-]+)\s*=\s*$", prefix)
                lkey = lm.group(1) if lm else None
                b_start = i + 1
                b_depth = 1
                j = b_start
                while j < n and b_depth > 0:
                    if block[j] == "{":
                        b_depth += 1
                    elif block[j] == "}":
                        b_depth -= 1
                    j += 1
                inner = block[b_start:j - 1]
                if lkey and "{" not in inner and "=" not in inner:
                    items = [x for x in inner.split() if x]
                    if items:
                        lists[lkey] = items
                i = j
                continue
            depth += 1
            i += 1
            continue
        if ch == "}":
            depth = max(0, depth - 1)
            i += 1
            continue
        if depth == 0:
            if ch == "\n":
                line = "".join(buf).strip()
                buf = []
                if "=" in line:
                    for m in re.finditer(r"\b([A-Za-z0-9_.\-]+)\s*=\s*([^\s={}]+)", line):
                        kv[m.group(1)] = m.group(2).strip('"')
            else:
                buf.append(ch)
        i += 1
    if buf:
        line = "".join(buf).strip()
        if "=" in line:
            for m in re.finditer(r"\b([A-Za-z0-9_.\-]+)\s*=\s*([^\s={}]+)", line):
                kv[m.group(1)] = m.group(2).strip('"')
    return kv, lists


def _parse_float(v):
    try:
        return float(v)
    except Exception:
        return None


def _extract_bare_tokens(block: str):
    """提取 enable_equipments = { ... } 中顶层裸装备名，跳过 limit = { ... } 等子块。"""
    out = []
    i, n = 0, len(block)
    while i < n:
        ch = block[i]
        if ch.isspace():
            i += 1
            continue
        if ch == "{":
            d, j = 1, i + 1
            while j < n and d > 0:
                if block[j] == "{":
                    d += 1
                elif block[j] == "}":
                    d -= 1
                j += 1
            i = j
            continue
        if ch == "}":
            i += 1
            continue
        if ch == '"':
            j = i + 1
            while j < n and block[j] != '"':
                j += 2 if (block[j] == "\\" and j + 1 < n) else 1
            i = j + 1
            continue
        if ch.isalnum() or ch in "_.-":
            j = i
            while j < n and (block[j].isalnum() or block[j] in "_.-"):
                j += 1
            tok = block[i:j]
            k = j
            while k < n and block[k].isspace():
                k += 1
            if k < n and block[k] == "=":
                q = k + 1
                while q < n and block[q].isspace():
                    q += 1
                if q < n and block[q] == "{":
                    d, r = 1, q + 1
                    while r < n and d > 0:
                        if block[r] == "{":
                            d += 1
                        elif block[r] == "}":
                            d -= 1
                        r += 1
                    i = r
                elif q < n and block[q] == '"':
                    r = q + 1
                    while r < n and block[r] != '"':
                        r += 2 if (block[r] == "\\" and r + 1 < n) else 1
                    i = r + 1
                else:
                    r = q
                    while r < n and not block[r].isspace() and block[r] not in "{}":
                        r += 1
                    i = r
                continue
            if tok and tok not in out:
                out.append(tok)
            i = j
            continue
        i += 1
    return out


def _iter_named_blocks(body: str, key: str):
    """返回所有配对大括号的 key = { ... } 内部文本（允许嵌套子块）。"""
    pat = re.compile(r"\b%s\s*=\s*\{" % re.escape(key))
    pos = 0
    n = len(body)
    while True:
        m = pat.search(body, pos)
        if not m:
            break
        d, j = 1, m.end()
        while j < n and d > 0:
            if body[j] == "{":
                d += 1
            elif body[j] == "}":
                d -= 1
            j += 1
        if d == 0:
            yield body[m.end():j - 1]
        pos = j


def _parse_all(game_dir: str):
    """解析全部装备定义与科技 enable_equipments 映射。"""
    raw_eq = {}
    eq_glob = os.path.join(game_dir, "common", "units", "equipment", "*.txt")
    for fn in sorted(glob.glob(eq_glob)):
        try:
            txt = open(fn, encoding="utf-8-sig", errors="replace").read()
        except OSError:
            continue
        cleaned = _strip_comments(txt)
        for key, blk in _iter_top_blocks(cleaned, "equipments"):
            kv, lists = _top_level_kv(blk)
            rec = {
                "key": key,
                "file": os.path.basename(fn),
                "is_archetype": kv.get("is_archetype", "").lower() == "yes",
                "is_buildable": kv.get("is_buildable", "yes").lower() != "no",
                "archetype": kv.get("archetype"),
                "parent": kv.get("parent"),
                "interface_category": kv.get("interface_category"),
                "types": list(lists.get("type") or ([kv["type"]] if "type" in kv else [])),
                "stats": {},
            }
            for nf in _NUM_FIELDS:
                if nf in kv:
                    fv = _parse_float(kv[nf])
                    if fv is not None:
                        if nf in ("year", "visual_level", "priority"):
                            rec[nf] = int(round(fv))
                        else:
                            rec["stats"][nf] = fv
            raw_eq[key] = rec

    # 解析继承链：archetype -> parent -> self，补齐子类继承的属性与年份
    equipments = {}
    for key, rec in raw_eq.items():
        chain = []
        seen = set()
        cur_k = key
        while cur_k and cur_k not in seen and cur_k in raw_eq:
            seen.add(cur_k)
            chain.append(raw_eq[cur_k])
            nxt = raw_eq[cur_k].get("parent")
            if not nxt:
                arch = raw_eq[cur_k].get("archetype")
                if arch and arch not in seen:
                    nxt = arch
            cur_k = nxt
        chain.reverse()
        merged_stats = {}
        merged_year = None
        merged_cat = None
        merged_types = []
        merged_arch = rec.get("archetype") or (key if rec.get("is_archetype") else None)
        for node in chain:
            merged_stats.update(node.get("stats") or {})
            if node.get("year") is not None:
                merged_year = node["year"]
            if node.get("interface_category"):
                merged_cat = node["interface_category"]
            if node.get("types"):
                merged_types = list(node["types"])
            if not merged_arch and node.get("archetype"):
                merged_arch = node["archetype"]
            elif not merged_arch and node.get("is_archetype"):
                merged_arch = node["key"]
        equipments[key] = {
            "key": key,
            "file": rec["file"],
            "is_archetype": rec["is_archetype"],
            "is_buildable": rec["is_buildable"],
            "archetype": merged_arch or _fallback_family(key)[0],
            "parent": rec.get("parent"),
            "year": merged_year,
            "visual_level": rec.get("visual_level"),
            "priority": rec.get("priority"),
            "interface_category": merged_cat,
            "types": merged_types,
            "stats": merged_stats,
        }

    # 解析科技树 enable_equipments
    tech_to_eq = {}
    eq_to_tech = {}
    tech_glob = os.path.join(game_dir, "common", "technologies", "*.txt")
    for fn in sorted(glob.glob(tech_glob)):
        try:
            txt = open(fn, encoding="utf-8-sig", errors="replace").read()
        except OSError:
            continue
        cleaned = _strip_comments(txt)
        for tkey, blk in _iter_top_blocks(cleaned, "technologies"):
            # 允许文件中有多个 enable_equipments 块，并正确跳过内嵌 limit = { ... } 条件块
            found = []
            for eblk in _iter_named_blocks(blk, "enable_equipments"):
                for tok in _extract_bare_tokens(eblk):
                    if tok not in found:
                        found.append(tok)
            if found:
                tech_to_eq[tkey] = found
                for eq in found:
                    eq_to_tech.setdefault(eq, tkey)

    return {
        "version": _CACHE_VERSION,
        "equipments": equipments,
        "tech_to_eq": tech_to_eq,
        "eq_to_tech": eq_to_tech,
    }


def _cache_path():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "equipmeta.pkl")


def _dir_sig(game_dir: str):
    parts = []
    for sub in (("common", "units", "equipment"), ("common", "technologies")):
        d = os.path.join(game_dir, *sub)
        if os.path.isdir(d):
            fs = sorted(glob.glob(os.path.join(d, "*.txt")))
            parts.append((len(fs), max((int(os.path.getmtime(x)) for x in fs), default=0)))
        else:
            parts.append((0, 0))
    return (game_dir, tuple(parts))


def load(game_dir: str | None = None):
    """加载装备与科技解锁元数据（带内存 + 磁盘缓存）。"""
    gd = game_dir or _game_dir()
    if not gd or not os.path.isdir(gd):
        return {"version": _CACHE_VERSION, "equipments": {}, "tech_to_eq": {}, "eq_to_tech": {}}
    sig = _dir_sig(gd)
    if sig in _MEM_CACHE:
        return _MEM_CACHE[sig]
    cpath = _cache_path()
    import safe_cache
    try:
        if safe_cache.exists(cpath):
            cached = safe_cache.load(cpath)
        else:
            cached = None
        if (isinstance(cached, dict)
                and cached.get("version") == _CACHE_VERSION
                and isinstance(cached.get("equipments"), dict)):
            # 1) 纯原版签名精确命中；2) 由 hoi4_recalc assets 为当前游戏+Mod 重建的缓存直接信任
            same_sig = cached.get("sig") == sig
            recalc_mod = (
                cached.get("source") == "hoi4_recalc"
                and bool(cached.get("mods"))
                and isinstance(cached.get("sig"), (tuple, list))
                and len(cached["sig"]) >= 1
                and os.path.normcase(str(cached["sig"][0])) == os.path.normcase(str(gd))
            )
            if same_sig or recalc_mod:
                _MEM_CACHE[sig] = cached
                return cached
    except (safe_cache.UnsafePickleError, safe_cache.CacheIntegrityError):
        raise
    except Exception:
        pass
    data = _parse_all(gd)
    data["sig"] = sig
    _MEM_CACHE[sig] = data
    try:
        safe_cache.save(cpath, data, fmt="json")
    except Exception:
        pass
    return data


def _fallback_family(name: str):
    """'artillery_equipment_2' -> ('artillery_equipment', 2)；无后缀返回 (name, -1)。"""
    s = str(name or "")
    m = re.match(r"^(.*)_(\d+)$", s)
    if m:
        return m.group(1), int(m.group(2))
    return s, -1


def equipment_info(eq_key: str, data=None):
    d = data if data is not None else load()
    meta = (d.get("equipments") or {}).get(eq_key)
    if meta:
        return meta
    fam, tier = _fallback_family(eq_key)
    return {
        "key": eq_key,
        "archetype": fam,
        "parent": None,
        "year": None,
        "visual_level": tier if tier >= 0 else None,
        "stats": {},
        "types": [],
    }


def tech_unlocks(tech_key: str, data=None):
    d = data if data is not None else load()
    return list((d.get("tech_to_eq") or {}).get(tech_key) or [])


def _tier_rank(eq_key: str, meta: dict):
    """用于在同 archetype 内排序新旧代次的综合秩元组。"""
    _, num = _fallback_family(eq_key)
    yr = meta.get("year")
    vl = meta.get("visual_level")
    st = meta.get("stats") or {}
    combat = (
        float(st.get("soft_attack") or 0.0)
        + float(st.get("hard_attack") or 0.0)
        + float(st.get("breakthrough") or 0.0)
        + float(st.get("defense") or 0.0)
        + float(st.get("air_attack") or 0.0) * 2.0
        + float(st.get("air_ground_attack") or 0.0) * 2.0
    )
    return (
        int(yr) if yr is not None else 0,
        int(vl) if vl is not None else -1,
        int(num),
        round(combat, 2),
    )


def _is_descendant(candidate: str, ancestor: str, eq_map: dict) -> bool:
    """检查 candidate 是否沿 parent 链继承自 ancestor。"""
    cur = candidate
    seen = set()
    while cur and cur not in seen:
        seen.add(cur)
        m = eq_map.get(cur)
        if not m:
            break
        par = m.get("parent")
        if par == ancestor:
            return True
        cur = par
    return False


def format_stat_delta(old_meta: dict, new_meta: dict) -> str:
    """将两个同族装备的战斗数值差格式化为简洁中文摘要。"""
    s0 = (old_meta or {}).get("stats") or {}
    s1 = (new_meta or {}).get("stats") or {}
    parts = []
    for field, label in _STAT_LABELS:
        v0 = s0.get(field)
        v1 = s1.get(field)
        if v0 is None or v1 is None:
            continue
        diff = float(v1) - float(v0)
        if abs(diff) < 1e-4:
            continue
        if field == "reliability":
            pct = round(diff * 100.0, 1)
            if abs(pct) >= 0.5:
                parts.append("%+.0f%% %s" % (pct, label))
            continue
        if diff > 0:
            parts.append("+%g %s" % (round(diff, 1), label))
        elif field in ("soft_attack", "breakthrough", "defense", "air_attack"):
            parts.append("%g %s" % (round(diff, 1), label))
        if len(parts) >= 4:
            break
    if not parts:
        y0, y1 = (old_meta or {}).get("year"), (new_meta or {}).get("year")
        if y0 and y1 and y1 > y0:
            return "%d→%d 换代升级" % (y0, y1)
        return "同族更高代次型号"
    return " / ".join(parts)


def estimate_retool(line: dict, old_meta: dict, new_meta: dict):
    """估算同 archetype 换装的效率损失百分比、回本天数与可读说明。

    HOI4 同 archetype（同底盘/同家族）换装保留约 70% 当前效率（即损失约 30%）；
    换装后新装备单位战力更高，按战力增益比与产线效率恢复周期估算回本天数。
    """
    loss_pct = 30
    s0 = (old_meta or {}).get("stats") or {}
    s1 = (new_meta or {}).get("stats") or {}

    def _power(st):
        return (
            float(st.get("soft_attack") or 0.0) * 1.2
            + float(st.get("hard_attack") or 0.0) * 1.0
            + float(st.get("breakthrough") or 0.0) * 1.0
            + float(st.get("defense") or 0.0) * 0.6
            + float(st.get("air_attack") or 0.0) * 2.0
            + float(st.get("air_defence") or 0.0) * 1.0
            + float(st.get("air_agility") or 0.0) * 0.5
            + float(st.get("air_ground_attack") or 0.0) * 1.8
            + float(st.get("air_bombing") or 0.0) * 1.2
        )

    p0, p1 = _power(s0), _power(s1)
    c0 = float(s0.get("build_cost_ic") or line.get("unit_cost") or 1.0)
    c1 = float(s1.get("build_cost_ic") or c0)
    if p0 > 0 and p1 > p0:
        # 单位装备战力提升幅度（同时考虑少量造价增加）
        eff_gain = max(0.08, (p1 / p0) / max(0.7, (c1 / max(c0, 0.1)) ** 0.5) - 1.0)
    else:
        eff_gain = 0.20

    # 效率损失 30% 在 HOI4 中按线性近似约需 40~55 天恢复，期间平均损失 ~15% 产出；
    # 回本天数 ≈ (效率恢复期累计损失产出比例) / 战力净增益比例，夹在 [10, 90] 天内。
    lost_ic_days = (loss_pct / 100.0) * 20.0
    payback_days = int(round(max(10.0, min(90.0, lost_ic_days / max(eff_gain, 0.08)))))

    fac = int(line.get("factories") or 0)
    icpd = float(line.get("output_per_day") or 0.0)
    if fac > 0 and icpd > 0:
        text = "损失约 %d%% 效率，当前 %d 厂 / %.1f IC/天 → 约 %d 天回本" % (
            loss_pct, fac, icpd, payback_days)
    else:
        text = "损失约 %d%% 效率，约 %d 天回本" % (loss_pct, payback_days)
    return loss_pct, payback_days, text


def analyze_production_lines(lines, available_variants, game_dir: str | None = None):
    """逐条分析军工产线是否过时，返回 {line_index: analysis_dict}。"""
    data = load(game_dir)
    eq_map = data.get("equipments") or {}

    avail_names = []
    for v in (available_variants or []):
        nm = v.get("equipment") if isinstance(v, dict) else v
        if nm and nm not in avail_names:
            avail_names.append(str(nm))

    # 每个 archetype 下本国已解锁的最佳变体
    best_by_arch = {}
    for eq in avail_names:
        meta = equipment_info(eq, data)
        arch = meta.get("archetype")
        if not arch:
            continue
        rank = _tier_rank(eq, meta)
        prev = best_by_arch.get(arch)
        if prev is None or rank > prev[0]:
            best_by_arch[arch] = (rank, eq, meta)

    result = {}
    for line in (lines or []):
        idx = line.get("index")
        cur_eq = line.get("equipment")
        if idx is None or not cur_eq:
            continue
        cur_meta = equipment_info(cur_eq, data)
        arch = cur_meta.get("archetype")
        info = {
            "line": int(idx),
            "equipment": cur_eq,
            "archetype": arch,
            "variant_year": cur_meta.get("year"),
            "obsolete": False,
            "upgrade_to": None,
        }
        if line.get("class") != "CMilitaryProductionLine":
            result[int(idx)] = info
            continue

        best = best_by_arch.get(arch) if arch else None
        if best is not None:
            best_rank, best_eq, best_meta = best
            cur_rank = _tier_rank(cur_eq, cur_meta)
            is_newer = False
            if best_eq != cur_eq:
                if _is_descendant(best_eq, cur_eq, eq_map):
                    is_newer = True
                elif best_meta.get("year") and cur_meta.get("year") and best_meta["year"] > cur_meta["year"]:
                    is_newer = True
                elif best_rank > cur_rank:
                    # 防止同年代不同变体误判，要求 visual_level 或数字后缀严格更大
                    _, c_num = _fallback_family(cur_eq)
                    _, b_num = _fallback_family(best_eq)
                    c_vl = cur_meta.get("visual_level")
                    b_vl = best_meta.get("visual_level")
                    if (b_vl is not None and c_vl is not None and b_vl > c_vl) or (
                            b_num >= 0 and c_num >= 0 and b_num > c_num):
                        is_newer = True
            if is_newer:
                gain = format_stat_delta(cur_meta, best_meta)
                loss_pct, payback_days, retool_text = estimate_retool(line, cur_meta, best_meta)
                info.update({
                    "obsolete": True,
                    "upgrade_to": best_eq,
                    "upgrade_to_year": best_meta.get("year"),
                    "upgrade_gain": gain,
                    "retool_efficiency_loss_pct": loss_pct,
                    "payback_days": payback_days,
                    "retool_cost": retool_text,
                    "factories": int(line.get("factories") or 0),
                    "output_per_day": float(line.get("output_per_day") or 0.0),
                    "suggested_action": (
                        'hoi4_set_production_line_equipment(line=%d, equipment="%s")'
                        % (int(idx), best_eq)
                    ),
                })
        result[int(idx)] = info
    return result


def check_tech_unlocks_obsolete(tech_keys, lines, available_variants, game_dir: str | None = None):
    """时刻 ①：科技完成瞬间，检查新解锁装备是否令现有任意军工产线立刻过时。"""
    data = load(game_dir)
    unlocked = []
    for tk in (tech_keys or []):
        for eq in tech_unlocks(tk, data):
            if eq not in unlocked:
                unlocked.append(eq)
    if not unlocked:
        return []
    # 把刚解锁的装备并入可用列表（防止科技刚完成的一帧内 PS_VARIANTS_ACTIVE 尚未刷新）
    merged_avail = list(available_variants or [])
    existing_names = {
        (v.get("equipment") if isinstance(v, dict) else v) for v in merged_avail
    }
    for eq in unlocked:
        if eq not in existing_names:
            merged_avail.append({"equipment": eq})
            existing_names.add(eq)
    analysis = analyze_production_lines(lines, merged_avail, game_dir=game_dir)
    hits = []
    for item in analysis.values():
        if item.get("obsolete") and item.get("upgrade_to") in unlocked:
            hits.append(item)
    hits.sort(key=lambda x: (-float(x.get("output_per_day") or 0.0), int(x["line"])))
    return hits


def format_obsolete_alert(item: dict) -> str:
    """按复测 5 §8.6 文案模板格式化三要素齐全的装备代差告警。"""
    rec_word = "建议立刻换" if int(item.get("payback_days") or 99) <= 45 else "建议择机换装"
    return (
        "OBSOLETE_LINE(line %d): %s → %s (%s); 换装%s，%s -> %s"
        % (
            int(item["line"]),
            item["equipment"],
            item["upgrade_to"],
            item.get("upgrade_gain") or "换代升级",
            item.get("retool_cost") or "损失约 30% 效率",
            rec_word,
            item.get("suggested_action") or "",
        )
    )


def clear_obsolete_seen(pid=None, tag=None, line_index=None):
    """清除去重记录（例如换装成功或测试重置时）。"""
    if pid is None and tag is None:
        _SEEN_OBSOLETE.clear()
        return
    key = (pid, str(tag or "").upper())
    if line_index is None:
        _SEEN_OBSOLETE.pop(key, None)
        return
    seen = _SEEN_OBSOLETE.get(key)
    if seen:
        _SEEN_OBSOLETE[key] = {x for x in seen if x[0] != int(line_index)}


def collect_obsolete_alerts(lines, available_variants, pid=None, tag=None,
                            dedupe=True, max_alerts=3, max_payback_days=60,
                            min_ic_per_day=10.0, game_dir: str | None = None):
    """按 §8.8 防噪音规则为 overview.alerts 生成装备代差告警。

    规则：
      1. 同 (line, archetype, cur_eq, upgrade_to) 仅在 overview 主动提醒一次；
         一旦换装完成或产线删除，自动清位。
      2. 仅报“可造 + 回本 < 60 天 + 产线有实际投入（ic_per_day >= 10 或工厂 >= 2）”。
      3. 最多返回 3 条，按影响面（output_per_day * max(1, 60 - payback_days)）降序，
         超出部分折叠为一条 `+N more` 摘要。
    返回 (alerts_list, all_obsolete_items, suppressed_count)。
    """
    analysis = analyze_production_lines(lines, available_variants, game_dir=game_dir)
    all_obs = [v for v in analysis.values() if v.get("obsolete")]

    # 先清理已经换装完成或产线已删除的旧去重记录
    key = (pid, str(tag or "").upper())
    active_sig = {
        (int(v["line"]), str(v.get("archetype") or ""), str(v["equipment"]), str(v["upgrade_to"]))
        for v in all_obs
    }
    seen = _SEEN_OBSOLETE.setdefault(key, set())
    if seen:
        seen.intersection_update(active_sig)

    eligible = []
    suppressed = 0
    for item in all_obs:
        pb = int(item.get("payback_days") or 999)
        icpd = float(item.get("output_per_day") or 0.0)
        fac = int(item.get("factories") or 0)
        if pb >= max_payback_days:
            continue
        if icpd < min_ic_per_day and fac < 2:
            continue
        sig = (
            int(item["line"]),
            str(item.get("archetype") or ""),
            str(item["equipment"]),
            str(item["upgrade_to"]),
        )
        if dedupe and sig in seen:
            suppressed += 1
            continue
        eligible.append((sig, item))

    eligible.sort(
        key=lambda pair: (
            -(float(pair[1].get("output_per_day") or 0.0)
              * max(1, max_payback_days - int(pair[1].get("payback_days") or 0))),
            int(pair[1]["line"]),
        )
    )

    alerts = []
    for sig, item in eligible[:max_alerts]:
        alerts.append(format_obsolete_alert(item))
        if dedupe:
            seen.add(sig)
    if len(eligible) > max_alerts:
        extra = eligible[max_alerts:]
        extra_desc = ", ".join(
            "line %d(%s→%s)" % (int(it["line"]), it["equipment"], it["upgrade_to"])
            for _, it in extra[:5]
        )
        alerts.append(
            "OBSOLETE_LINE(+%d more): 另有 %d 条过时产线可换装（%s），查看 hoi4_production 获取详情"
            % (len(extra), len(extra), extra_desc)
        )
        if dedupe:
            for sig, _ in extra:
                seen.add(sig)

    return alerts, all_obs, suppressed


def compute_surplus_lines(lines, stockpile_totals, total_mil: int, div_count: int):
    """识别库存严重过剩（库存 / 安全储备基准 > 2.0）且仍占军工 > 20% 的产线（§8.5）。"""
    total_mil = max(int(total_mil or 0), 1)
    div_count = max(int(div_count or 0), 1)
    totals = stockpile_totals or {}

    out = []
    for line in (lines or []):
        if line.get("class") != "CMilitaryProductionLine":
            continue
        fac = int(line.get("factories") or 0)
        share = fac / float(total_mil)
        if fac < 5 or share <= 0.20:
            continue
        eq = str(line.get("equipment") or "")
        eq_low = eq.lower()
        fam_key = None
        per_div = 15.0
        floor_need = 1000.0
        for prefix, pd, fl in _SURPLUS_PER_DIV:
            if prefix in eq_low:
                fam_key = prefix
                per_div = pd
                floor_need = fl
                break
        if not fam_key:
            continue
        stock_qty = sum(
            float(v or 0.0) for k, v in totals.items()
            if k and fam_key in str(k).lower()
        )
        reserve_need = max(floor_need, div_count * per_div)
        ratio = stock_qty / max(reserve_need, 1.0)
        if ratio <= 2.0:
            continue
        # 建议保留约 12~15% 军工（至少 2 厂），其余为可腾挪工厂
        keep = max(2, min(fac - 1, int(round(total_mil * 0.14))))
        surplus_fac = max(0, fac - keep)
        if surplus_fac <= 0:
            continue
        out.append({
            "line": int(line["index"]),
            "equipment": eq,
            "family": fam_key,
            "stockpile": int(round(stock_qty)),
            "reserve_need": int(round(reserve_need)),
            "ratio": round(ratio, 2),
            "factories": fac,
            "total_mil": total_mil,
            "factory_share_pct": round(share * 100.0, 1),
            "suggested_factories": keep,
            "surplus_factories": surplus_fac,
            "suggested_action": (
                "hoi4_set_production_amount(line=%d, amount=%d)" % (int(line["index"]), keep)
            ),
            "alert": (
                "SURPLUS(line %d): %s 库存 %d（为安全储备基准 %d 的 %.1f 倍），"
                "仍占 %d/%d 座军工（%.0f%%），可腾出约 %d 座军工给缺口产线（如战斗机/CAS/装甲）"
                " -> hoi4_set_production_amount(line=%d, amount=%d)"
                % (
                    int(line["index"]), eq, int(round(stock_qty)),
                    int(round(reserve_need)), ratio, fac, total_mil,
                    share * 100.0, surplus_fac, int(line["index"]), keep,
                )
            ),
        })
    out.sort(key=lambda x: (-x["surplus_factories"], -x["ratio"]))
    return out


def compute_air_gap_alerts(wings, lines):
    """识别在编作战飞机规模与月产能严重脱节的机型（§8.5 AIR_GAP）。"""
    deployed = {}
    for w in (wings or []):
        wtype = str(w.get("type") or "").lower()
        if not wtype or wtype == "transport_plane":
            continue
        deployed[wtype] = deployed.get(wtype, 0) + int(w.get("planes") or 0)

    monthly_prod = {}
    for l in (lines or []):
        if l.get("class") != "CMilitaryProductionLine":
            continue
        eq = str(l.get("equipment") or "").lower()
        upd = float(l.get("output_units_per_day") or 0.0)
        if upd <= 0:
            continue
        mtype = None
        if "fighter" in eq or "small_plane_airframe" in eq:
            mtype = "fighter"
        elif "cas" in eq or "small_plane_cas" in eq:
            mtype = "cas"
        elif "tac_bomber" in eq or "medium_plane_airframe" in eq:
            mtype = "tac_bomber"
        elif "nav_bomber" in eq or "small_plane_naval" in eq:
            mtype = "nav_bomber"
        elif "strat_bomber" in eq or "large_plane_airframe" in eq:
            mtype = "strat_bomber"
        if mtype:
            monthly_prod[mtype] = monthly_prod.get(mtype, 0.0) + upd * 30.0

    labels = {
        "fighter": "战斗机",
        "cas": "近距支援机(CAS)",
        "tac_bomber": "战术轰炸机",
        "nav_bomber": "海军轰炸机",
        "strat_bomber": "战略轰炸机",
    }
    deprio_types = {"tac_bomber", "strat_bomber"}
    alerts = []
    for wtype, count in sorted(deployed.items(), key=lambda kv: -kv[1]):
        # 规模 >= 200 架才评估月补充率，避免小队误报
        if count < 200:
            continue
        mprod = monthly_prod.get(wtype, 0.0)
        rate = mprod / float(count)
        if rate < 0.05:
            if wtype in deprio_types:
                hint = "若不打算主力使用%s可忽略，否则增配军工或改转战斗机/CAS" % labels.get(wtype, wtype)
            else:
                hint = "建议立刻给%s产线增配军工" % labels.get(wtype, wtype)
            alerts.append(
                "AIR_GAP(%s): 在编%s %d 架，当前月产仅 %.1f 架（月补充率 %.1f%% < 5%%），"
                "高强度空战损耗无法补回 -> %s"
                % (wtype, labels.get(wtype, wtype), count, mprod, rate * 100.0, hint)
            )
    return alerts


FUEL_CRITICAL_THRESHOLD = 10.0


def is_fuel_critical(fuel) -> bool:
    """燃油是否已枯竭（<= 10.0）；供 FUEL_ZERO 红点、IDLE_AIR/NAVY 与 housekeep 共享单一真源。"""
    if fuel is None:
        return False
    try:
        return float(fuel) <= FUEL_CRITICAL_THRESHOLD
    except Exception:
        return False


def compute_fuel_zero_alert(fuel, wings, fleets):
    """识别燃油归零/枯竭但海空力量仍挂着任务执行（§8.5 FUEL_ZERO / 复测 9 D-2）。"""
    if not is_fuel_critical(fuel):
        return None
    fval = float(fuel)
    active_wings = sum(
        1 for w in (wings or [])
        if (w.get("type") or "") != "transport_plane" and w.get("mission")
    )
    active_tfs = 0
    for f in (fleets or []):
        if f.get("is_reserve"):
            continue
        # 2026-11-23 (复测 9 D-2): 舰队明确无指派海区 (regions == 0) 时停在母港不耗油，
        # 不再误报为“挂着任务烧油”；保留 regions is None 的旧 mock 兼容。
        f_regs = f.get("regions")
        if f_regs is not None and int(f_regs) <= 0:
            continue
        for tf in (f.get("task_forces") or []):
            if tf.get("is_reserve"):
                continue
            tf_regs = tf.get("regions")
            if tf_regs is not None and int(tf_regs) <= 0:
                continue
            m = int(tf.get("effective_mission") or tf.get("mission") or 0)
            if m > 0 and m != 8:
                active_tfs += 1
    if active_wings == 0 and active_tfs == 0:
        return None
    return (
        "FUEL_ZERO: 当前燃油为 %.0f，但仍有 %d 个空军联队与 %d 个海军特遣舰队挂着任务 —— "
        "无油状态下飞机无法有效起飞、舰船与装甲战斗力暴跌，继续扩产战机/舰艇也无法发挥战力"
        " -> 立即调用 hoi4_trade(partner='<出口国TAG，如ROM/SOV/VEN>', resource='oil', amount=8) 进口石油 / 建合成炼油厂 / 暂停非必要海空巡逻"
        % (fval, active_wings, active_tfs)
    )
