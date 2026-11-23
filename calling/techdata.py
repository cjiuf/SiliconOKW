"""Static technology definitions from the game's own common/technologies/*.txt.

Pure file parsing (no game calls).  Used by available_research as the year and
prerequisite table; the live CTechnologyStatus only supplies what is already
researched / currently in a slot.
"""
from __future__ import annotations
import os, pickle, re

import config

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, "techdefs.pkl")

FILE_CATEGORY = {
    "infantry": "infantry",
    "industry": "industry",
    "electronic_mechanical_engineering": "computing",
    "armor": "armor",
    "nsb_armor": "armor",
    "air_techs": "air",
    "bba_air_techs": "air",
    "artillery": "artillery",
    "support": "support",
    "naval": "naval",
    "mtg_naval": "naval",
    "mtg_naval_support": "naval",
    "special_projects_tech": "special",
}


def _strip_comments(text):
    return re.sub(r"#[^\n]*", "", text)


def _top_blocks(text):
    """Yield (name, body) for every depth-1 `name = { ... }` block."""
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
                    yield name, text[start:q-1]
                    i = q
                    continue
            i = j
            continue
        i += 1


def _parse_block(body):
    m = re.search(r"\bstart_year\s*=\s*(\d+)", body)
    year = int(m.group(1)) if m else 1936
    m = re.search(r"\bfolder\s*=\s*\{.*?\bname\s*=\s*([A-Za-z0-9_]+)", body, re.S)
    folder = m.group(1) if m else ""
    cats = []
    m = re.search(r"\bcategories\s*=\s*\{([^}]*)\}", body)
    if m:
        cats = [x for x in re.findall(r"[A-Za-z0-9_]+", m.group(1))]
    leads = re.findall(r"\bleads_to_tech\s*=\s*([A-Za-z0-9_]+)", body)
    # 2026-11-19: `XOR = { other_tech }` is the script-level mutual exclusion.
    # The ENGINE does not enforce it inside CSetResearchCommand::CanExecute —
    # only the UI's `available` predicate does — so the tool happily started
    # concentrated_industry AND dispersed_industry together (measured live).
    # Capture it here so start_research / fill_research can refuse.
    xor = []
    for m in re.finditer(r"\bXOR\s*=\s*\{([^}]*)\}", body):
        xor.extend(re.findall(r"[A-Za-z0-9_]+", m.group(1)))
    m = re.search(r"\bmutually_exclusive\s*=\s*\{([^}]*)\}", body)
    if m:
        xor.extend(re.findall(r"[A-Za-z0-9_]+", m.group(1)))
    xor = sorted(set(x for x in xor if x))
    return {"year": year, "folder": folder, "categories": cats,
            "leads_to": leads, "xor": xor}


def _category(fname, folder, cats):
    c = FILE_CATEGORY.get(fname.lower())
    if c:
        return c
    s = (folder + " " + " ".join(cats)).lower()
    for key, val in (("infantry", "infantry"), ("industry", "industry"),
                     ("electronic", "computing"), ("computing", "computing"),
                     ("armor", "armor"), ("tank", "armor"),
                     ("air", "air"), ("fighter", "air"), ("bomber", "air"),
                     ("artillery", "artillery"), ("support", "support"),
                     ("naval", "naval")):
        if key in s:
            return val
    return "other"


def build(force=False):
    if not force and os.path.exists(CACHE):
        with open(CACHE, "rb") as f:
            cached = pickle.load(f)
        # 2026-11-19: the pickle predates the `xor` table; rebuild once so
        # start_research can refuse mutually exclusive techs.
        if cached and all("xor" in v for v in cached.values()):
            return cached
    game = config.game_dir()
    d = os.path.join(game, "common", "technologies")
    techs = {}
    for fn in sorted(os.listdir(d)):
        if not fn.endswith(".txt"):
            continue
        stem = fn[:-4]
        with open(os.path.join(d, fn), encoding="utf-8", errors="replace") as f:
            text = f.read()
        for name, body in _top_blocks(text):
            info = _parse_block(body)
            info["category"] = _category(stem, info["folder"], info["categories"])
            info["file"] = stem
            techs[name] = info
    # reverse path edges: parent -> child means the child needs at least one parent
    parents = {}
    for name, info in techs.items():
        for child in info.pop("leads_to", []):
            parents.setdefault(child, set()).add(name)
    for name, info in techs.items():
        info["parents"] = sorted(parents.get(name, ()))
    with open(CACHE, "wb") as f:
        pickle.dump(techs, f)
    return techs


_MEM = None
def load(force=False):
    global _MEM
    if force or _MEM is None:
        _MEM = build(force)
    return _MEM

def score_tech(tech_id, folder, categories, start_year, current_year, tag=None):
    """Veteran priority score (higher = research first).

    2026-11-05: the old rule was `year_gap <= 1: score += 120`, so a 1937 tech
    researched in 1936 (year_gap = -1) received exactly the same bonus as an
    on-time 1936 tech. Four slots went straight into 300+ day ahead-of-time
    projects. Ahead-of-time research is now punished hard; only the on-time
    column is rewarded.
    """
    score = 0
    year_gap = int(current_year) - int(start_year or 1936)
    if year_gap < 0:
        score -= 600 * abs(year_gap)      # 超前科技：重罚，除非当年科技已点完
    elif year_gap == 0:
        score += 180                      # 当年最新科技最高优先
    elif year_gap == 1:
        score += 120                      # 去年未补完的核心科技
    elif year_gap <= 3:
        score += 60
    else:
        score -= 40

    cats = set(categories or [])
    tid = (tech_id or "").lower()
    if "industry" in cats or "construction" in tid or "machine_tools" in tid:
        score += 300
    elif "electronics" in cats or "computing" in tid or "mechanical_computing" in tid:
        score += 280
    elif "infantry_weapons" in cats or "artillery" in cats:
        score += 220
    elif "armor" in cats and ("medium" in tid or "heavy" in tid or "main_battle" in tid):
        score += 200
    elif "light_air" in cats or "fighter" in tid or "cas" in tid:
        score += 180
    elif "support_tech" in cats and any(k in tid for k in ("engineers", "recon", "logistics", "maintenance")):
        score += 140
    elif any(k in tid for k in ("paratroopers", "marines", "mountaineers", "armored_car",
                                "amphibious", "mine")):
        score -= 200

    # Veteran per-year list from calling/playbooks.py gets absolute priority.
    if tag:
        try:
            import playbooks
            by_yr = (playbooks.get_playbook(tag) or {}).get("research_by_year") or {}
            for yr, tech_list in sorted(by_yr.items()):
                if int(yr) <= int(current_year) and tech_id in tech_list:
                    score += 1200 - tech_list.index(tech_id) * 15
                    break
        except Exception:
            pass
    return score
