# -*- coding: utf-8 -*-
"""Extract HOI4 event definitions (country_event / news_event / state_event ...)
from the game's own event scripts into a queryable pickle.

This is the *static* half of the event tooling: it answers
"what does event X offer, what does each option do?" for any event id the
runtime reader sees in a popup.

Usage (from research/windows):
    python wevdb.py build                 # write evdb_win.pkl
    python wevdb.py show GER_rhineland
    python wevdb.py find rhineland
    python wevdb.py news                  # list all news_event ids
"""
import io
import os
import pickle
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CALLING = os.path.abspath(os.path.join(HERE, "..", "..", "calling"))
if CALLING not in sys.path:
    sys.path.insert(0, CALLING)

import config

OUT = os.path.join(HERE, "evdb_win.pkl")

EVENT_ROOTS = ("country_event", "news_event", "state_event", "unit_leader_event",
               "operative_leader_event", "news_event")


# ---------------------------------------------------------------- tokenizer
def tokens(text):
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c in " \t\r\n\ufeff":
            i += 1
            continue
        if c == "#":
            while i < n and text[i] not in "\r\n":
                i += 1
            continue
        if c == '"':
            j = i + 1
            buf = []
            while j < n:
                if text[j] == "\\" and j + 1 < n:
                    buf.append(text[j])
                    buf.append(text[j + 1])
                    j += 2
                    continue
                if text[j] == '"':
                    break
                buf.append(text[j])
                j += 1
            yield ("STR", "".join(buf))
            i = j + 1
            continue
        if c in "{}=":
            yield (c, c)
            i += 1
            continue
        if c in "<>!":
            j = i
            while j < n and text[j] in "<>!=":
                j += 1
            yield ("OP", text[i:j])
            i = j
            continue
        j = i
        while j < n and text[j] not in ' \t\r\n{}="#<>!=':
            j += 1
        if j == i:
            j = i + 1
        yield ("WORD", text[i:j])
        i = j


def parse(toks):
    """token list -> list of (key, value); value is str | list[(key,value)]."""
    pos = [0]

    def peek():
        return toks[pos[0]] if pos[0] < len(toks) else (None, None)

    def block():
        out = []
        while pos[0] < len(toks):
            k, v = peek()
            if k == "}":
                pos[0] += 1
                return out
            if k not in ("WORD", "STR"):
                pos[0] += 1
                continue
            pos[0] += 1
            nk, nv = peek()
            if nk == "=":
                pos[0] += 1
                nk, nv = peek()
                if nk == "{":
                    pos[0] += 1
                    out.append((v, block()))
                elif nk == "OP":
                    pos[0] += 1
                    nk2, nv2 = peek()
                    pos[0] += 1
                    out.append((v, [("<op>", nv), ("<val>", nv2)]))
                else:
                    pos[0] += 1
                    out.append((v, nv))
            else:
                out.append((v, None))
        return out

    return block()


def load_tree(path):
    raw = open(path, "rb").read()
    if raw[:2] == b"\xff\xfe" or raw[:2] == b"\xfe\xff":
        text = raw.decode("utf-16")
    else:
        text = raw.decode("utf-8", "replace")
    return parse(list(tokens(text)))


def first(block, key, default=None):
    for k, v in block:
        if k == key:
            return v
    return default


def all_of(block, key):
    return [v for k, v in block if k == key]


def flat(block, prefix=""):
    """Flatten an effect block into ['key = value', ...] strings."""
    out = []
    for k, v in block:
        if k == "<op>":
            continue
        if isinstance(v, list):
            inner = [x for x in v if x[0] != "<op>"]
            if inner:
                out.append("%s%s = { %s }" % (prefix, k, flat_str(v)))
            else:
                ops = [x[1] for x in v if x[0] == "<op>"]
                out.append("%s%s %s" % (prefix, k, " ".join(ops)))
        else:
            out.append("%s%s = %s" % (prefix, k, v))
    return out


def flat_str(block):
    parts = []
    i = 0
    for k, v in block:
        if k == "<op>":
            continue
        if isinstance(v, list):
            parts.append("%s = { %s }" % (k, flat_str(v)))
        else:
            parts.append("%s = %s" % (k, v) if v is not None else k)
    return " ".join(parts)


def build():
    game = config.game_dir()
    root = os.path.join(game, "events")
    db = {}
    files = []
    for dirpath, _dirs, names in os.walk(root):
        for fn in names:
            if fn.endswith(".txt"):
                files.append(os.path.join(dirpath, fn))
    for path in sorted(files):
        try:
            tree = load_tree(path)
        except Exception as e:
            print("!! parse fail %s: %s" % (path, e))
            continue
        for kind, body in tree:
            if kind not in EVENT_ROOTS or not isinstance(body, list):
                continue
            eid = first(body, "id")
            if not eid:
                continue
            opts = []
            for ob in all_of(body, "option"):
                if not isinstance(ob, list):
                    continue
                name = first(ob, "name")
                eff = []
                for k, v in ob:
                    if k in ("name", "ai_chance", "trigger", "original_recipient_only", "highlight_states"):
                        continue
                    if isinstance(v, list):
                        eff.append("%s = { %s }" % (k, flat_str(v)))
                    else:
                        eff.append("%s = %s" % (k, v) if v is not None else k)
                o = {
                    "name": name,
                    "effects": eff,
                    "ai_chance": flat_str(first(ob, "ai_chance") or []),
                    "trigger": flat_str(first(ob, "trigger") or []),
                    "trigger_raw": first(ob, "trigger") or [],
                }
                opts.append(o)
            db[eid] = {
                "kind": kind,
                "file": os.path.relpath(path, game),
                "title": first(body, "title"),
                "desc": first(body, "desc"),
                "picture": first(body, "picture"),
                "major": first(body, "major"),
                "is_triggered_only": first(body, "is_triggered_only"),
                "hidden": first(body, "hidden"),
                "fire_only_once": first(body, "fire_only_once"),
                "immediate": flat(first(body, "immediate") or []),
                "options": opts,
            }
    bake_texts(db)
    pickle.dump(db, open(OUT, "wb"))
    print("wrote %s  (%d events, %d options)" % (OUT, len(db), sum(len(v["options"]) for v in db.values())))
    kinds = {}
    for v in db.values():
        kinds[v["kind"]] = kinds.get(v["kind"], 0) + 1
    print("by kind:", kinds)


# ---------------------------------------------------------------- localisation
LOC_CACHE = {}


def load_loc(lang="simp_chinese"):
    if lang in LOC_CACHE:
        return LOC_CACHE[lang]
    game = config.game_dir()
    d = os.path.join(game, "localisation", lang)
    loc = {}
    if os.path.isdir(d):
        for fn in sorted(os.listdir(d)):
            if not fn.endswith(".yml"):
                continue
            raw = open(os.path.join(d, fn), "rb").read()
            if raw[:2] == b"\xff\xfe":
                text = raw.decode("utf-16")
            else:
                text = raw.decode("utf-8-sig", "replace")
            for line in text.splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                m = re.match(r'^([A-Za-z0-9_.\-]+):\d*\s*"(.*)"\s*$', line)
                if not m:
                    continue
                key, val = m.group(1), m.group(2)
                val = val.replace("\\n", "\n").replace('\\"', '"')
                loc[key] = val
    LOC_CACHE[lang] = loc
    return loc


def T(key, lang="simp_chinese"):
    if not isinstance(key, str) or not key:
        return ""
    loc = load_loc(lang)
    v = loc.get(key)
    if v is None:
        return "<%s>" % key
    return v


LANGS = (("en", "english"), ("zh", "simp_chinese"))


def _key(v):
    return v if isinstance(v, str) else ""


def bake_texts(db):
    """Attach title_en/title_zh, desc_en/desc_zh and option name_en/name_zh."""
    for code, lang in LANGS:
        loc = load_loc(lang)
        for e in db.values():
            e["title_" + code] = loc.get(_key(e["title"]), "")
            e["desc_" + code] = loc.get(_key(e["desc"]), "")
            for o in e["options"]:
                o["name_" + code] = loc.get(_key(o["name"]), "")
    return db


def show(eid, lang="zh"):
    db = pickle.load(open(OUT, "rb"))
    e = db.get(eid)
    if not e:
        cands = [k for k in db if eid.lower() in k.lower()][:20]
        print("not found. candidates:", cands)
        return
    print("=" * 78)
    print("%s   [%s]  %s" % (eid, e["kind"], e["file"]))
    print("TITLE: %s  (%s)" % (e.get("title_" + lang) or T(e["title"], "english"), e["title"]))
    d = e.get("desc_" + lang) or T(e["desc"], "english") if e["desc"] else ""
    print("DESC : %s" % (d[:400].replace("\n", " ") + ("..." if len(d) > 400 else "")))
    print("flags: major=%s triggered_only=%s hidden=%s once=%s picture=%s"
          % (e["major"], e["is_triggered_only"], e["hidden"], e["fire_only_once"], e["picture"]))
    if e["immediate"]:
        print("IMMEDIATE: %s" % "; ".join(e["immediate"]))
    for i, o in enumerate(e["options"], 1):
        nm = o.get("name_" + lang) or T(o["name"], "english")
        print("  [%d] %s   (%s)" % (i, nm or "<no name>", o["name"]))
        for line in o["effects"]:
            print("        %s" % line)
        if o["ai_chance"]:
            print("        ai_chance: %s" % o["ai_chance"])
        if o["trigger"]:
            print("        trigger  : %s" % o["trigger"])


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return
    cmd = sys.argv[1]
    if cmd == "build":
        build()
    elif cmd == "show":
        show(sys.argv[2])
    elif cmd == "find":
        db = pickle.load(open(OUT, "rb"))
        pat = sys.argv[2].lower()
        for k in sorted(db):
            if pat in k.lower():
                print("%-46s %-12s %d opts  %s" % (k, db[k]["kind"], len(db[k]["options"]), T(db[k]["title"])[:60]))
    elif cmd == "news":
        db = pickle.load(open(OUT, "rb"))
        for k in sorted(db):
            if db[k]["kind"] == "news_event":
                print("%-46s %d opts  %s" % (k, len(db[k]["options"]), T(db[k]["title"])[:60]))
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
