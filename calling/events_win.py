# -*- coding: utf-8 -*-
"""Event tooling for the PE (Windows) build of HOI4 1.19.5.

Two halves
----------
static : research/windows/evdb_win.pkl -- every country_event / news_event /
         state_event / unit_leader_event in the game's own scripts, with its
         options, their effects, ai_chance and trigger, plus the localized
         title/description/option text for English and Simplified Chinese.
         Rebuild with:  python research/windows/wevdb.py build

live   : a `CEventWindow` object is created on demand when a popup is shown.
         Its constructor stores the pending `CEvent*` at +0x68:

             CEventWindow  +0x58  owner
                           +0x60  int
                           +0x68  CEvent*        <- the event being shown
             CEvent        +0x20  std::string id  ("GER_rhineland")
                           +0x368 CPdxArray<CEventOption*> data
                           +0x370 u32 count, +0x374 u32 capacity

         (All of this is disassembly-verified: CEventWindow ctor 0x12C5D88,
          CEvent ctor 0x120BD50, option array cross-checked against a 1-option
          and a 3-option event.)

Nothing here writes to the game: the reader is pure ReadProcessMemory.
"""
import os
import pickle
import struct
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

EVT_VTS = (0x2A16008, 0x2A16060)   # CEvent primary / secondary vtable
WND_VTS = (0x2A20830, 0x2A20858, 0x2A20890, 0x2A208A8)  # CEventWindow's four
WND_OFF_EVENT = 0x68               # CEventWindow -> CEvent*
EVT_OFF_ID = 0x20                  # CEvent -> std::string id
EVT_OFF_OPTS = 0x368               # CEvent -> CPdxArray<CEventOption*>
EVT_OFF_COUNT = 0x374              # u32 option count (+0x370 is NOT the count)

_DB = None


def db_path():
    return os.path.join(ROOT, "research", "windows", "evdb_win.pkl")


def build_db(quiet=True):
    """Regenerate the pickle from the installed game files."""
    script = os.path.join(ROOT, "research", "windows", "wevdb.py")
    r = subprocess.run([sys.executable, script, "build"],
                       cwd=os.path.dirname(script),
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("wevdb build failed: %s" % (r.stderr or r.stdout)[-400:])
    return r.stdout.strip()


def load_db(auto_build=True):
    global _DB
    if _DB is not None:
        return _DB
    path = db_path()
    if not os.path.exists(path):
        if not auto_build:
            raise RuntimeError("event db missing: run `python research/windows/wevdb.py build`")
        build_db()
    with open(path, "rb") as _ef:
        _DB = pickle.load(_ef)
    return _DB


def _lang(lang):
    return "zh" if str(lang).lower().startswith(("zh", "cn")) else "en"


def info(event_id, lang="zh"):
    """One event with its options, localized."""
    db = load_db()
    lg = _lang(lang)
    e = db.get(event_id)
    if e is None:
        for k in db:
            if k.lower() == str(event_id).lower():
                e = db[k]
                event_id = k
                break
    if e is None:
        return {"ok": False, "error": "unknown event id", "event_id": event_id}
    return {
        "ok": True,
        "event_id": event_id,
        "kind": e["kind"],
        "file": e["file"],
        "title": e.get("title_" + lg) or "",
        "desc": (e.get("desc_" + lg) or "")[:1500],
        "picture": e["picture"],
        "major": e["major"],
        "hidden": e["hidden"],
        "fire_only_once": e["fire_only_once"],
        "options": [
            {
                "index": i + 1,
                "name": o.get("name_" + lg) or "",
                "name_key": o["name"],
                "effects": o["effects"][:24],
                "ai_chance": o["ai_chance"],
                "trigger": o["trigger"],
                "trigger_raw": o.get("trigger_raw") or [],
            }
            for i, o in enumerate(e["options"])
        ],
    }


def search(query, lang="zh", limit=25):
    db = load_db()
    lg = _lang(lang)
    q = str(query).lower()
    out = []
    for k in sorted(db):
        e = db[k]
        hay = " ".join([k, e.get("title_" + lg) or "", e.get("title_en") or ""]).lower()
        if q in hay:
            out.append({"event_id": k, "kind": e["kind"],
                        "options": len(e["options"]),
                        "title": (e.get("title_" + lg) or e.get("title_en") or "")[:80]})
            if len(out) >= limit:
                break
    return {"count": len(out), "results": out}


def news(lang="zh", limit=200):
    db = load_db()
    lg = _lang(lang)
    out = []
    for k in sorted(db):
        e = db[k]
        if e["kind"] != "news_event":
            continue
        out.append({"event_id": k, "major": e["major"],
                    "title": (e.get("title_" + lg) or e.get("title_en") or "")[:100]})
        if len(out) >= limit:
            break
    return {"count": len(out), "news": out}


# ------------------------------------------------------------------ live side
def _mfcstr(p, addr, cap_max=8192):
    """Read an MSVC std::string ({buf[16] | ptr, size, capacity})."""
    try:
        ptr = p.u64(addr)
        size = p.u64(addr + 0x10)
        cap = p.u64(addr + 0x18)
    except Exception:
        return None
    if size > cap_max or cap > cap_max:
        return None
    base = ptr if cap >= 0x10 else addr
    try:
        raw = p.read(base, size) if size else b""
    except Exception:
        return None
    return raw.decode("utf-8", "replace")


def read_event_obj(g, ev):
    """Decode a CEvent* into {id, option_count, options[]}."""
    p = g.p
    out = {"event_ptr": hex(ev) if ev else None}
    if not ev:
        return out
    try:
        vt = p.u64(ev) - p.base
    except Exception:
        return out
    if vt not in EVT_VTS:
        out["error"] = "not a CEvent (vt rva 0x%X)" % vt
        return out
    out["event_id"] = _mfcstr(p, ev + EVT_OFF_ID)
    try:
        out["option_count"] = p.u32(ev + EVT_OFF_COUNT)
        arr = p.u64(ev + EVT_OFF_OPTS)
        ptrs = []
        if arr and 0 < out["option_count"] <= 20:
            for i in range(out["option_count"]):
                ptrs.append(p.u64(arr + i * 8))
        out["option_ptrs"] = [hex(x) for x in ptrs]
    except Exception as e:
        out["read_error"] = repr(e)
    return out


def _pool_scan(g, lo, hi, needles, chunk=8 << 20):
    """Byte-scan [lo,hi) for 8-byte-aligned occurrences of any needle."""
    p = g.p
    hits = []
    pos = lo
    while pos < hi:
        m = min(chunk, hi - pos)
        try:
            d = p.read(pos, m)
        except Exception:
            d = b""
        if not d:
            pos += m
            continue
        for nd in needles:
            i = d.find(nd)
            while i >= 0:
                if i % 8 == 0:
                    hits.append(pos + i)
                i = d.find(nd, i + 1)
        pos += m
    return hits


def _pairs(node):
    """Normalise a parsed block into [(key, value), ...]."""
    if not node:
        return []
    if isinstance(node, dict):
        return list(node.items())
    out = []
    for item in node:
        if isinstance(item, (list, tuple)) and len(item) == 2:
            out.append((item[0], item[1]))
    return out


_BOOL_TRUE = {"yes", "true", "1"}
_BOOL_FALSE = {"no", "false", "0"}
_LOGIC_KEYS = {"not", "and", "or", "all_of", "any_of", "if", "limit", "else", "else_if"}


def _as_bool(v):
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if s in _BOOL_TRUE:
        return True
    if s in _BOOL_FALSE:
        return False
    return None


def _is_country_tag_token(k):
    s = str(k or "").strip()
    if len(s) != 3 or s.lower() in _LOGIC_KEYS:
        return False
    return s.isupper() and s[0].isalpha() and s.isalnum()


class _EvalCtx:
    """单次事件选项求值期间的纯只读游戏状态缓存（避免重复遍历国家/州表）。"""

    def __init__(self, g, root_tag):
        self.g = g
        self.root_tag = str(root_tag or "GER").upper()
        self._state_owner = None
        self._enemies = {}
        self._relations = {}
        self._focuses = {}
        self._exists = {}
        self._gov = {}

    def resolve_tag(self, token, cur_tag):
        s = str(token or "").strip().upper()
        if s == "ROOT":
            return self.root_tag
        if s == "THIS":
            return cur_tag
        return s

    def state_owner(self, sid):
        if self.g is None:
            return None
        if self._state_owner is None:
            mp = {}
            # 优先支持单测直供 state_owner 映射
            mock_so = getattr(self.g, "_mock_state_owners", None)
            if isinstance(mock_so, dict):
                mp = {int(k): str(v).upper() for k, v in mock_so.items()}
            else:
                try:
                    import offsets as _O
                    from game import _arr, CO_STATES
                    p = self.g.p
                    for c in self.g.countries():
                        ct = str(c.tag).upper()
                        for st in _arr(p, c.ptr, CO_STATES):
                            try:
                                mp[int(p.i32(st + _O.ST_ID))] = ct
                            except Exception:
                                continue
                except Exception:
                    mp = {}
            self._state_owner = mp
        return self._state_owner.get(int(sid))

    def has_war(self, tag):
        if self.g is None:
            return None
        tu = str(tag).upper()
        if tu not in self._enemies:
            try:
                rel = self.relations(tu)
                if rel is not None:
                    self._enemies[tu] = set(rel.get("at_war_with") or [])
                else:
                    import combat
                    self._enemies[tu] = set(combat.enemy_tags(self.g, tu))
            except Exception:
                self._enemies[tu] = None
        es = self._enemies[tu]
        return None if es is None else bool(es)

    def relations(self, tag):
        if self.g is None:
            return None
        tu = str(tag).upper()
        if tu not in self._relations:
            try:
                import diplomacy as _dip
                self._relations[tu] = _dip.country_relations(self.g, tu)
            except Exception:
                self._relations[tu] = None
        return self._relations[tu]

    def country_exists(self, tag):
        if self.g is None:
            return None
        tu = str(tag).upper()
        if tu not in self._exists:
            mock_ex = getattr(self.g, "_mock_exists", None)
            if isinstance(mock_ex, dict) and tu in mock_ex:
                self._exists[tu] = bool(mock_ex[tu])
            else:
                try:
                    c = self.g.country(tu)
                    self._exists[tu] = int(c.state_count or 0) > 0
                except Exception:
                    self._exists[tu] = None
        return self._exists[tu]

    def completed_focuses(self, tag):
        if self.g is None:
            return None
        tu = str(tag).upper()
        if tu not in self._focuses:
            try:
                self._focuses[tu] = set(self.g.completed_focuses(tu) or [])
            except Exception:
                self._focuses[tu] = None
        return self._focuses[tu]

    def government(self, tag):
        if self.g is None:
            return None
        tu = str(tag).upper()
        if tu not in self._gov:
            ide = None
            try:
                pol = self.g.politics(tu) or {}
                ide = pol.get("ruling_ideology") or pol.get("ideology")
            except Exception:
                ide = None
            if not ide:
                try:
                    import legality as _leg
                    ide = _leg.ruling_ideology(self.g, tu)
                except Exception:
                    ide = None
            self._gov[tu] = str(ide).lower() if ide else None
        return self._gov[tu]

    def has_capitulated(self, tag):
        """保守判定国家是否投降：不存在或未处于战争态时必为 False；战时未知返回 None。"""
        if self.g is None:
            return None
        tu = str(tag).upper()
        ex = self.country_exists(tu)
        if ex is False:
            return False
        hw = self.has_war(tu)
        if hw is False:
            return False
        return None


def _extract_candidate_states(pairs):
    """从 any_state 块中提取显式限定的候选州集合及剩余条件。"""
    sids = []
    rest = []
    for k, v in pairs:
        if k == "<op>":
            continue
        kl = str(k).lower()
        if kl == "state" and str(v).strip().isdigit():
            sids.append(int(str(v).strip()))
        elif kl in ("or", "any_of") and isinstance(v, (list, tuple, dict)):
            sub = _pairs(v)
            if sub and all(str(sk).lower() == "state" and str(sv).strip().isdigit()
                           for sk, sv in sub if sk != "<op>"):
                for sk, sv in sub:
                    if sk != "<op>":
                        sids.append(int(str(sv).strip()))
            else:
                rest.append((k, v))
        else:
            rest.append((k, v))
    return sids, rest


def _trig(node, tag, ctx=None, scope_tag=None, scope_state=None):
    """AND over a block -> True / False / None(unknown)."""
    pairs = _pairs(node)
    if not pairs:
        return True
    cur_tag = str(scope_tag or tag).upper()
    unknown = False
    for k, v in pairs:
        if k == "<op>":
            continue
        r = _trig1(k, v, tag, ctx=ctx, scope_tag=cur_tag, scope_state=scope_state, in_or=False)
        if r is False:
            return False
        if r is None:
            unknown = True
    return None if unknown else True


def _trig1(k, v, tag, ctx=None, scope_tag=None, scope_state=None, in_or=False):
    ks = str(k)
    kl = ks.lower()
    cur_tag = str(scope_tag or tag).upper()

    # 逻辑连接符
    if kl in ("tag", "tag_has", "original_tag"):
        target_t = ctx.resolve_tag(v, cur_tag) if ctx else str(v).upper()
        return cur_tag == target_t
    if kl == "not":
        # 2026-11-23 (复测 10 B-5 根治): Paradox Clausewitz 脚本的 `NOT = { A B C }`
        # 语义是 NOR = `NOT (A OR B OR C)`（只要任意一项为 True 即返回 False，全部为 False
        # 才返回 True）！旧代码直接用 AND 求值再取反，导致 news.36 / news.56 里的
        # `NOT = { TAG = GER TAG = ITA ... }` 对 GER 恒错算成 True！
        pairs = _pairs(v)
        if not pairs:
            return True
        vals = [
            _trig1(kk, vv, tag, ctx=ctx, scope_tag=cur_tag, scope_state=scope_state, in_or=True)
            for kk, vv in pairs if kk != "<op>"
        ]
        if any(x is True for x in vals):
            return False
        if any(x is None for x in vals):
            return None
        return True
    if kl in ("or", "any_of"):
        pairs = _pairs(v)
        if not pairs:
            return None
        vals = [
            _trig1(kk, vv, tag, ctx=ctx, scope_tag=cur_tag, scope_state=scope_state, in_or=True)
            for kk, vv in pairs if kk != "<op>"
        ]
        if any(x is True for x in vals):
            return True
        return None if (not vals or any(x is None for x in vals)) else False
    if kl in ("and", "all_of"):
        return _trig(v, tag, ctx=ctx, scope_tag=cur_tag, scope_state=scope_state)
    if kl == "if":
        pairs = _pairs(v)
        limit_node = None
        body = []
        for kk, vv in pairs:
            if kk == "<op>":
                continue
            if str(kk).lower() == "limit":
                limit_node = vv
            else:
                body.append((kk, vv))
        if limit_node is None:
            return None
        lim = _trig(limit_node, tag, ctx=ctx, scope_tag=cur_tag, scope_state=scope_state)
        if lim is False:
            # 在 OR 中未命中 limit 的 if 分支为 False；在 AND 中跳过（True）
            return False if in_or else True
        if lim is None:
            return None
        return _trig(body, tag, ctx=ctx, scope_tag=cur_tag, scope_state=scope_state)

    # 2026-11-23 (复测 6 P1-5): 跨国 / 州作用域切换（仅当传入实时 ctx 时求值）
    if kl == "root" and isinstance(v, (list, tuple, dict)):
        root_t = ctx.root_tag if ctx else str(tag).upper()
        return _trig(v, tag, ctx=ctx, scope_tag=root_t, scope_state=None)
    if _is_country_tag_token(ks) and isinstance(v, (list, tuple, dict)):
        if ctx is None or ctx.g is None:
            return None
        return _trig(v, tag, ctx=ctx, scope_tag=ks.upper(), scope_state=None)
    if ks.isdigit() and isinstance(v, (list, tuple, dict)):
        if ctx is None or ctx.g is None:
            return None
        return _trig(v, tag, ctx=ctx, scope_tag=cur_tag, scope_state=int(ks))
    if kl == "any_state" and isinstance(v, (list, tuple, dict)):
        if ctx is None or ctx.g is None:
            return None
        sids, rest = _extract_candidate_states(_pairs(v))
        if not sids:
            return None
        vals = [
            _trig(rest, tag, ctx=ctx, scope_tag=cur_tag, scope_state=sid)
            for sid in sids
        ]
        if any(x is True for x in vals):
            return True
        return None if any(x is None for x in vals) else False

    # 无活游戏上下文时，其余动态条件一律保持 None（不瞎猜）
    if ctx is None or ctx.g is None:
        return None

    # 州作用域原子条件
    if kl == "state" and scope_state is not None and str(v).strip().isdigit():
        return int(scope_state) == int(str(v).strip())
    if kl in ("is_controlled_by", "is_owned_by") and scope_state is not None:
        own = ctx.state_owner(scope_state)
        if own is None:
            return None
        want = ctx.resolve_tag(v, cur_tag)
        return own == want

    # 国家作用域原子条件
    if kl == "has_war":
        want = _as_bool(v)
        actual = ctx.has_war(cur_tag)
        if want is None or actual is None:
            return None
        return actual == want
    if kl == "exists":
        want = _as_bool(v)
        actual = ctx.country_exists(cur_tag)
        if want is None or actual is None:
            return None
        return actual == want
    if kl == "country_exists":
        want_t = ctx.resolve_tag(v, cur_tag)
        return ctx.country_exists(want_t)
    if kl in ("owns_state", "controls_state", "has_full_control_of_state"):
        if not str(v).strip().isdigit():
            return None
        own = ctx.state_owner(int(str(v).strip()))
        if own is None:
            return None
        if own != cur_tag:
            return False
        # 和平态下 owner == full controller；战时 has_full_control_of_state 保守返回 None
        if kl == "has_full_control_of_state" and ctx.has_war(cur_tag) is True:
            return None
        return True
    if kl == "is_subject":
        want = _as_bool(v)
        rel = ctx.relations(cur_tag)
        if want is None or rel is None:
            return None
        return bool(rel.get("overlord")) == want
    if kl == "is_subject_of":
        rel = ctx.relations(cur_tag)
        if rel is None:
            return None
        want = ctx.resolve_tag(v, cur_tag)
        return str(rel.get("overlord") or "").upper() == want
    if kl == "is_in_faction":
        want = _as_bool(v)
        rel = ctx.relations(cur_tag)
        if want is None or rel is None:
            return None
        return bool(rel.get("faction")) == want
    if kl == "is_in_faction_with":
        rel = ctx.relations(cur_tag)
        if rel is None:
            return None
        want = ctx.resolve_tag(v, cur_tag)
        fac = rel.get("faction")
        if not fac:
            return False
        return want in (fac.get("members") or [])
    if kl == "has_completed_focus":
        fs = ctx.completed_focuses(cur_tag)
        if fs is None:
            return None
        return str(v).strip() in fs
    if kl == "has_government":
        gov = ctx.government(cur_tag)
        if gov is None:
            return None
        return gov == str(v).strip().lower()
    if kl == "has_capitulated":
        want = _as_bool(v)
        actual = ctx.has_capitulated(cur_tag)
        if want is None or actual is None:
            return None
        return actual == want
    return None


def _trigger_positively_names_tag(node, tag):
    """结构化判断 trigger 中是否存在正面（非 NOT 块内）要求本国 tag 的条件。

    2026-11-23 (复测 10 B-5): 旧代码用 `("tag = ger") in tl` 粗糙子串匹配，
    会把 `NOT = { TAG = GER TAG = ITA ... }`（恰恰排除 GER 的选项！）误判为 tag_hit！
    """
    tu = str(tag or "").upper()
    if not tu:
        return False
    for k, v in _pairs(node):
        kl = str(k).lower()
        if kl == "not":
            continue
        if kl in ("tag", "tag_has", "original_tag"):
            if str(v).strip().upper() == tu:
                return True
        elif kl in ("and", "all_of", "or", "any_of") and isinstance(v, (list, tuple, dict)):
            if _trigger_positively_names_tag(v, tu):
                return True
    return False


def applicable_options(event_id, tag, lang="zh", g=None):
    """Which option the game would actually show/allow for `tag`."""
    d = info(event_id, lang=lang)
    if not d.get("ok"):
        return []
    ctx = _EvalCtx(g, tag) if g is not None else None
    out = []
    for o in d["options"]:
        raw = o.get("trigger_raw") or o.get("trigger") or []
        r = _trig(raw, tag, ctx=ctx)
        out.append({"index": o["index"], "name": o["name"], "applicable": r,
                    "trigger": o["trigger"],
                    "_positive_tag": _trigger_positively_names_tag(raw, tag)})
    return out


def _wnd_needles(g):
    return [struct.pack("<Q", g.p.base + r) for r in WND_VTS]


# ------------------------------------------------------- pending event queue
# CGameState is a static object at RVA 0x33DD6B0 (the pointer stored there).
#   [gs+0x560] = CPdxArray data, [gs+0x56C] = count, element stride 0x38:
#       +0x00 u32   record id            -> CSelectEventOptionCommand+0x28
#       +0x08 CEvent*                    (its handle sits at CEvent+0x08)
#       +0x10 pointer                    (country / scope of the event)
GS_HANDLE_RVA = 0x33DD6B0
GS_PENDING = 0x560
GS_PENDING_N = 0x56C
PEND_STRIDE = 0x38


def pending(g, only_player=True):
    """Every event waiting for an answer, straight out of CGameState.

    2026-11-23 (复测 8 B-16 根因修复): CGameState+0x560 存储全图所有国家（含 AI 国家）
    的待决事件；每条记录 +0x10 指向 CEventScope，真机实证 CEventScope+0x08 (i32) 为接收国
    tag_id（如 GER=1，AI 国家=113/117）。默认 `only_player=True` 过滤掉明确属于别国 AI 的
    瞬时事件（如 TUR 的 bftb_turkey.2），避免误触发玩家 PENDING_EVENT 红点急刹。
    """
    p = g.p
    gs = p.u64(p.base + GS_HANDLE_RVA)
    if not gs:
        return []
    arr = p.u64(gs + GS_PENDING)
    n = p.u32(gs + GS_PENDING_N)
    rows = []
    if not arr or n <= 0 or n > 256:
        return rows

    player_tid = None
    tags_list = []
    try:
        if hasattr(g, "_load_tags"):
            g._load_tags()
        tags_list = getattr(g, "_tags", None) or []
        c_pl = g.player()
        if c_pl is not None and getattr(c_pl, "tag_id", None) is not None:
            player_tid = int(c_pl.tag_id)
    except Exception:
        player_tid = None

    scope_vt_va = p.base + SCOPE_VT
    for i in range(n):
        e = arr + i * PEND_STRIDE
        try:
            row = {"slot": p.u32(e), "record": hex(e)}
            ev = p.u64(e + 0x08)
            sc = p.u64(e + 0x10)
            row["scope"] = hex(sc)
        except Exception:
            continue
        owner_tid = None
        if 0x10000 < sc < 0x7F0000000000:
            try:
                if p.u64(sc) == scope_vt_va:
                    tid_raw = int(p.i32(sc + 0x08))
                    if 0 < tid_raw < 4096:
                        owner_tid = tid_raw
            except Exception:
                owner_tid = None
        if owner_tid is not None:
            row["owner_tag_id"] = owner_tid
            if 0 <= owner_tid < len(tags_list):
                row["owner_tag"] = tags_list[owner_tid]
            if only_player and player_tid is not None and owner_tid != player_tid:
                continue
        row.update(read_event_obj(g, ev))
        rows.append(row)
    return rows


# ------------------------------------------------------------- option select
SEL_VT = 0x29B6CE0        # CSelectEventOptionCommand
SEL_SIZE = 0xF0
SEL_CTOR = 0xA590D0
SEL_CANEXEC = 0x15D66C0
SEL_EXECUTE = 0x15D53E0

# --- 2026-11-19: the empty-scope bug -------------------------------------
# `CSelectEventOptionCommand::Execute` ends with
#     lea rdx,[cmd+0x38] ; mov rcx,CEvent* ; mov r8d,[cmd+0xE8]
#     call CEvent::SelectOption (0x120C600)
#     lea rdx,[cmd+0x38] ; call 0x120CFA0        (pending-list cleanup)
# i.e. the option's effects are evaluated against the command's EMBEDDED
# CEventScope at +0x38.  The factory builds that scope EMPTY, so every effect
# that reads a scope (annex_country / add_political_power / set_country_flag /
# transfer_troops ...) silently no-ops while CanExecute still returns true.
# Measured 1937.12.02: `germany.4` (Anschluss) reported
#     posted 1 / drained true / pending 1 -> 0 / ok true
# and Austria was still an independent country with 6 states afterwards.
#
# Fix: copy the real scope from the pending record (+0x10) into cmd+0x38
# before posting, then put the factory's original bytes back once the queue
# has drained, so the command's destructor still owns exactly what it built
# (no double free of the source scope's heap block).
SEL_SCOPE_OFF = 0x38
SEL_SCOPE_SIZE = 0xB0     # 0x57BEE0 writes up to +0xAC, so 0xB0 covers it
SCOPE_VT = 0x2852168      # CEventScope vtable (RVA)

# 第二期数据驱动覆盖：从 calling/profile.py 读取事件虚表与选事件命令 RVA
try:
    import profile as _prof
    if _prof.load():
        EVT_VTS = _prof.anchor_list("VT_EVENT", EVT_VTS)
        WND_VTS = _prof.anchor_list("VT_EVENT_WINDOW", WND_VTS)
        GS_HANDLE_RVA = _prof.anchor("G_GAMESTATE", GS_HANDLE_RVA)
        SCOPE_VT = _prof.anchor("VT_EVENT_SCOPE", SCOPE_VT)
        SEL_VT = _prof.command_field("CSelectEventOptionCommand", "vt", SEL_VT)
        SEL_SIZE = _prof.command_field("CSelectEventOptionCommand", "size", SEL_SIZE)
        SEL_CANEXEC = _prof.command_field("CSelectEventOptionCommand", "can_execute", SEL_CANEXEC)
        SEL_EXECUTE = _prof.command_field("CSelectEventOptionCommand", "execute", SEL_EXECUTE)
except Exception:
    pass


def scope_copy_in(g, cmd, src):
    """Copy the live CEventScope at `src` into the command's inline scope.

    Returns the original 0xB0 bytes so the caller can restore them after the
    command has executed.  Any qword that points into the source object is
    rebased onto the destination — that covers the three self-pointers the
    default ctor writes at +0x18/+0x20/+0x28 (inline storage of the scope's
    own container) and anything else that aliases the object itself.  Heap
    pointers are copied as-is (shared read-only during Execute) and are never
    freed by us.
    """
    p = g.p
    orig = p.read(cmd + SEL_SCOPE_OFF, SEL_SCOPE_SIZE)
    blob = bytearray(p.read(src, SEL_SCOPE_SIZE))
    for off in range(0, SEL_SCOPE_SIZE - 8 + 1, 8):
        q = struct.unpack_from("<Q", blob, off)[0]
        if q and src <= q < src + SEL_SCOPE_SIZE:
            struct.pack_into("<Q", blob, off, cmd + SEL_SCOPE_OFF + (q - src))
    p.write(cmd + SEL_SCOPE_OFF, bytes(blob))
    return orig


def scope_copy_out(g, cmd, orig):
    """Restore the factory-built inline scope (see scope_copy_in)."""
    try:
        g.p.write(cmd + SEL_SCOPE_OFF, orig)
    except Exception:
        pass


def make_select_option(g, slot_id, event_ptr, index, option_name=""):
    """Build CSelectEventOptionCommand through its own factory (0xA590D0).

    ☠️ 2026-11-18 — the old hand-built version was wrong, and this is what
    killed hoi4.exe on every previous attempt.  It wrote a std::string at
    `+0x38`.  That slot is an **embedded CEventScope**: the factory calls
    `0x57BEE0` on `this+0x38`, whose first store is `[rcx] = 0x2852168`
    (= `CEventScope` vtable), then a hash seed at +0x10 and three self
    pointers at +0x18/+0x20/+0x28.  Writing a 12-byte option name there and
    then `+0x48 = len`, `+0x50 = 0xF` clobbered that scope.  The engine's own
    CanExecute only looks at +0x2c/+0x34/+0x374, so it happily returned True
    and the crash landed later, inside Post/Execute.

    So: let the factory build the object (it constructs the scope correctly)
    and only touch the four scalar fields the engine actually reads:

        +0x28 i32  pending-record id
        +0x2c u64  CEvent handle  (= CEvent+0x08)
        +0x34 i32  1-based option index   (CanExecute: must be > 0)
        +0xE8 i32  0-based option index   (Execute indexes CEvent+0x368 with it)

    `option_name` is accepted for backward compatibility and deliberately
    unused — the name is not a field of this command.
    """
    import cmdwin
    p = g.p
    cmd = int(g.call(p.base + SEL_CTOR, (), absolute=True, force=True) or 0)
    if not cmd:
        raise RuntimeError("CSelectEventOptionCommand factory returned NULL")
    vt = p.u64(cmd)
    if vt != p.base + SEL_VT:
        raise RuntimeError("factory returned %#x with vptr %#x, expected %#x"
                           % (cmd, vt, p.base + SEL_VT))
    p.write(cmd + 0x28, struct.pack("<i", int(slot_id)))
    p.write(cmd + 0x2C, p.read(event_ptr + 0x08, 8))
    p.write(cmd + 0x34, struct.pack("<i", int(index)))
    p.write(cmd + 0xE8, struct.pack("<i", int(index) - 1))
    return cmd


def option_name_for(event_id, info_row, index):
    """Loc key of option `index` (1-based): script name, else <id>.<letter>."""
    if info_row:
        for o in info_row.get("options") or []:
            if o["index"] == index and o.get("name_key"):
                return o["name_key"]
    letters = "abcdefghijklmnopqrstuvwxyz"
    if event_id and 1 <= index <= len(letters):
        return "%s.%s" % (event_id, letters[index - 1])
    return ""


def can_execute(g, cmd):
    """Call the command's own CanExecute (slot 9) — read-only, no side effects."""
    vt = g.p.u64(cmd)
    slot = g.p.u64(vt + 0x48)
    return bool(g.call(slot, (cmd, 0), absolute=True, force=True) & 0xFF)


def select_option(g, index=None, event_id=None, slot=None, lang="zh", tag="GER",
                  write=False):
    """Work out WHICH option to answer with -- and never write to the game.

    Writing was attempted three times on 2026-11-17 (enqueue, then Post, with
    +0xE8 = index-1 and a correct CEvent handle).  Every attempt passed the
    engine's own CanExecute and then killed hoi4.exe inside the game's Post /
    Execute on the injected thread.  Until that is understood this function
    only reports:

        * the option list with a True/False/None `applicable` verdict per option
          (evaluated from the option's own `trigger` against the player's tag),
        * the index that the game will pick by itself if nothing is clicked
          (option 1, after EVENT_TIMEOUT_DEFAULT = 13 days),
        * a built-and-validated command object, as proof the layout is right.

    The player clicks the button; the tool never does.
    """
    rows = pending(g)
    if not rows:
        return {"ok": False, "error": "no pending event", "write_disabled": True}
    row = None
    if event_id:
        for r in rows:
            if r.get("event_id") == event_id:
                row = r
                break
    elif slot is not None:
        for r in rows:
            if r.get("slot") == slot:
                row = r
                break
    else:
        row = rows[0]
    if row is None:
        return {"ok": False, "error": "pending event not found", "write_disabled": True,
                "pending": [(r.get("slot"), r.get("event_id")) for r in rows]}

    tag = str(tag or "GER").upper()
    try:
        opts = applicable_options(row.get("event_id") or "", tag, lang=lang, g=g)
    except TypeError:
        # 兼容外部仅接受 3 参数的 monkey-patch 单测
        opts = applicable_options(row.get("event_id") or "", tag, lang=lang)
    rec = None
    recommendation_blocked = False
    recommendation_reason = None
    # 2026-11-19: news events carry one option per country (``tag = GER`` /
    # ``TAG = FRA`` ...). Prefer an applicable option whose trigger names our own tag.
    # 2026-11-23 (复测 6 P1-5): 若存在带未决条件（applicable is None）的选项，且唯一
    # 判 True 的只是“无条件兜底选项”（trigger 为空，如 germany.55 选项 4 直接吞并 -50 PP），
    # 严禁把该兜底项误推为 recommended；返回 recommended=None + recommendation_blocked=True。
    tag_hit = None
    unknown_tag_hits = []
    cond_true = []
    any_true = []
    has_unknown = False
    for o in opts:
        app = o.get("applicable")
        if app is None:
            has_unknown = True
            if o.get("_positive_tag"):
                unknown_tag_hits.append(o["index"])
        elif app is True:
            any_true.append(o["index"])
            t = str(o.get("trigger") or "").strip()
            if t:
                cond_true.append(o["index"])
            if tag_hit is None and o.get("_positive_tag"):
                tag_hit = o["index"]
    # 返回面向调用方的干净选项列表（去掉内部标记 _positive_tag）
    opts_public = [{k: v for k, v in o.items() if not str(k).startswith("_")} for o in opts]
    recommendation_basis = None
    if tag_hit is not None:
        rec = tag_hit
        recommendation_basis = "applicable_positive_tag"
    elif cond_true:
        rec = cond_true[0]
        recommendation_basis = "applicable_conditional"
    elif any_true and not has_unknown:
        rec = any_true[0]
        recommendation_basis = "applicable_unconditional"
    elif any_true and has_unknown:
        rec = None
        recommendation_blocked = True
        recommendation_reason = (
            "other options have unevaluated triggers (applicable=null) while only "
            "unconditional fallback option(s) %s evaluated true -- refusing to blindly "
            "recommend the fallback; inspect option effects and pass explicit index"
            % any_true
        )
    elif len(unknown_tag_hits) == 1 and any(o.get("applicable") is False for o in opts):
        # 2026-11-23 (复测 11 P2-3): 如 lar_news.1 中 SPA/SPB 为西班牙内战动态别名导致
        # country_exists=SPA 暂为 None，但其余选项（如 NOT={tag=GER} / tag=SOV / tag=SPA）
        # 均已确定为 False，且唯一正面指名本国 tag 的选项为 unknown_tag_hits[0] -> 精准推荐该项。
        rec = unknown_tag_hits[0]
        recommendation_basis = "unique_positive_tag_partial_trigger"

    explicit_index = index is not None
    if index is None:
        index = rec if rec is not None else 1
    idx = int(index)
    # 2026-11-19 (crash 2/2): `applicable: None` is NOT "condition false" -- it
    # means the option's trigger could not be evaluated at all, normally because
    # the event's ROOT scope points at a country this tag cannot resolve
    # (`country_capitulated.0` does exactly that).  Posting the command with such
    # a copied, unevaluable scope killed hoi4.exe twice, both times with no crash
    # directory and no dump.  Refuse and let EVENT_TIMEOUT_DEFAULT (13 days) do it.
    unresolved = bool(opts) and all(o["applicable"] is None for o in opts)

    # 2026-11-22 (复测 5 §3.7): auto_resolve_option 不应硬编码 1 —— 当选项 1 的
    # trigger 不适用于本国（如 bftb_news.201 中选项 1 仅给 SOV，GER 只能选 3）时，
    # 引擎自动结算的是本国可见/适用的第一个选项，而非全局选项 1。
    # 若前面存在未决选项（applicable is None），则无法确知引擎自动结算号，返回 None。
    auto_opt = None
    for o in opts:
        if o.get("applicable") is None:
            auto_opt = None
            break
        if o.get("applicable") is True:
            auto_opt = o["index"]
            break

    out = {"ok": True, "event_id": row.get("event_id"), "slot": row.get("slot"),
           "tag": tag, "options": opts_public, "recommended": rec,
           "recommendation_blocked": recommendation_blocked,
           "auto_resolve_option": auto_opt,
           "chosen": (idx if (explicit_index or rec is not None) else None),
           "unresolved_scope": unresolved,
           "write_disabled": not write,
           "note": ("answered through CSelectEventOptionCommand"
                    if write else
                    ("read-only; pass write=True to actually answer it. "
                     "Unanswered events auto-pick the first applicable option (%s) after 13 days."
                     % (auto_opt if auto_opt is not None else "unknown")))}
    if recommendation_reason:
        out["recommendation_reason"] = recommendation_reason
    if recommendation_basis:
        out["recommendation_basis"] = recommendation_basis
    if unresolved:
        out["warning"] = (
            "every option's trigger came back None -- the event's ROOT scope cannot "
            "be resolved for %s. Writing this used to kill hoi4.exe (2/2 on "
            "2026-11-19); leave it to the 13-day timeout." % tag)
    if write and not explicit_index and recommendation_blocked:
        out["ok"] = False
        out["write_disabled"] = True
        out["blocked_by"] = "recommendation_blocked"
        out["error"] = recommendation_reason
        return out
    try:
        name = option_name_for(row.get("event_id"), info(row.get("event_id"), lang=lang), idx)
        cmd = make_select_option(g, row["slot"], int(row["event_ptr"], 16), idx, name)
        out["cmd"] = hex(cmd)
        out["can_execute"] = can_execute(g, cmd)
        if write:
            if unresolved:
                out["ok"] = False
                out["write_disabled"] = True
                out["blocked_by"] = "unresolved_scope"
                out["error"] = ("refusing: no option of %s has an evaluable trigger "
                                "(applicable is None for all of them).  The copied "
                                "CEventScope is unevaluable and posting it killed "
                                "hoi4.exe twice on 2026-11-19."
                                % row.get("event_id"))
                return out
            if not out["can_execute"]:
                out["ok"] = False
                out["error"] = "CanExecute refused; nothing was sent"
            else:
                import cmdwin
                before = len(pending(g))
                orig_scope = None
                sc = row.get("scope")
                if sc:
                    src = int(sc, 16)
                    try:
                        if src and g.p.u64(src) == g.p.base + SCOPE_VT:
                            orig_scope = scope_copy_in(g, cmd, src)
                            out["scope_copied"] = True
                        else:
                            out["scope_copied"] = False
                            out["scope_warning"] = (
                                "pending record's +0x10 is not a CEventScope; "
                                "effects that need a scope will not run")
                    except Exception as e:
                        out["scope_copied"] = False
                        out["scope_error"] = repr(e)
                else:
                    out["scope_copied"] = False
                # Swap slot 0 deleting destructor to no-op BEFORE post_drained so
                # the command queue does not destroy/free `cmd` (and double-free the
                # copied CEventScope pointers) before scope_copy_out restores it.
                cmdwin.attach_fake_vtable(g, cmd, SEL_VT)
                try:
                    out["posted"] = cmdwin.post_drained(g, cmd, seconds=2.5)
                finally:
                    if orig_scope is not None:
                        scope_copy_out(g, cmd, orig_scope)
                after = len(pending(g))
                out["pending_before"] = before
                out["pending_after"] = after
                out["pending_source"] = ("CGameState pending-event array "
                                         "(events_win.pending)")
                # 2026-11-19: `after < before` was a false negative whenever a
                # SECOND event was queued behind this one — the count stayed the
                # same (or grew) and the tool reported "did not go away" even
                # though this event had been answered.  Judge by this event's
                # own id instead.  (Measured: news.25 cleared, bba_news.8 was
                # already waiting => pending_after 1 => bogus ok:false.)
                eid = row.get("event_id")
                still = [r for r in pending(g) if r.get("event_id") == eid]
                out["event_still_pending"] = bool(still)
                out["ok"] = not still
                if not out["ok"]:
                    out["error"] = ("the command was accepted but this event is "
                                    "still in the pending list")
    except Exception as e:
        out["ok"] = False
        out["build_error"] = repr(e)
    return out


def open_events(g, with_db=True, lang="zh", deep=False):
    """Every event waiting for an answer, joined with the script database.

    Reads CGameState's pending-event array, which is O(1) and always accurate;
    `deep=True` additionally looks for the on-screen CEventWindow (slow, ~90 s
    the first time) so the caller can tell whether the popup is really shown.
    """
    try:
        rows = pending(g)
    except Exception as e:
        return {"ok": False, "error": repr(e), "open": 0, "events": []}
    out_rows = []
    for r in rows:
        if with_db and r.get("event_id"):
            d = info(r["event_id"], lang=lang)
            if d.get("ok"):
                r["title"] = d["title"]
                r["desc"] = d["desc"]
                r["options"] = d["options"]
                r["kind"] = d["kind"]
                r["major"] = d["major"]
                r["script_option_count"] = len(d["options"])
        out_rows.append(r)
    out = {"ok": True, "open": len(out_rows), "events": out_rows,
           # 2026-11-21 (P2-7): name the queue so "open: 1" and a tool that says
           # "pending_before: 2" can be compared instead of argued about.  Both
           # read events_win.pending() (the game's pending-event array).
           "pending_source": "CGameState pending-event array (events_win.pending)"}
    if deep:
        try:
            out["windows"] = [hex(w) for w in find_windows(g, deep=True)]
        except Exception as e:
            out["windows_error"] = repr(e)
    return out


def find_windows(g, deep=False):
    """CEventWindow objects.

    A full sweep costs ~90 s (the game's writable set is ~9.3 GB), so the reader
    remembers where the last window lived and re-scans only that 8 MB chunk
    first.  `deep=True` forces the full sweep.
    """
    p = g.p
    needles = _wnd_needles(g)
    hits = []
    hint = getattr(g, "_event_win_hint", None)
    if hint:
        try:
            if p.u64(hint) - p.base in WND_VTS:
                hits.append(hint)
        except Exception:
            pass
    zone = getattr(g, "_event_win_zone", None)
    if not hits and zone:
        try:
            hits = _pool_scan(g, zone[0], zone[1], needles)
        except Exception:
            hits = []
    if not hits and deep:
        for m in p.regions(writable=True):
            hits.extend(_pool_scan(g, m["start"], m["end"], needles))
    if hits:
        a = hits[0]
        g._event_win_hint = a
        z0 = max(0, a & ~0x7FFFFF)
        g._event_win_zone = (z0, z0 + (8 << 20))
    else:
        if deep:
            g._event_win_hint = None
    return hits

