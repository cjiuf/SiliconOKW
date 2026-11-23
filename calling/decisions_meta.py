"""Static metadata for HOI4 decisions, parsed from `common/decisions/*.txt`.

The live `CDecision` object only gives us the script key.  Everything a player
actually needs in order to judge a decision -- its political-power cost, how long
it runs, whether it is one-shot, and a readable slice of its `available` trigger
-- lives in the script files, so it is parsed once here and cached.

The parser is brace-aware: a decision is a `key = { ... }` block that contains at
least one decision marker (`available`, `visible`, `complete_effect`,
`activation`, `days_remove`, `fire_only_once`, `targets`, `days_mission_timeout`)
and is not a category definition.  The enclosing block name is recorded as the
category, which is what the in-game decision tab groups by.

No game calls.
"""
from __future__ import annotations

import os
import pickle
import re

import config

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, "decisions_meta.pkl")
SCHEMA = 1

MARKERS = ("available", "visible", "complete_effect", "activation",
           "days_remove", "fire_only_once", "targets", "days_mission_timeout",
           "remove_effect", "cancel_effect", "is_good")

TOKEN_RE = re.compile(r"[A-Za-z0-9_.\-]+")


def _strip_comments(text):
    return re.sub(r"#[^\n]*", "", text)


def _walk(text):
    """Yield (key, body, ancestors) for every `name = { ... }` block."""
    i, n = 0, len(text)
    stack = []            # [(name, body_start)]
    while i < n:
        ch = text[i]
        if ch == "{":
            # the name is the last token before this brace
            j = i - 1
            while j >= 0 and text[j] in " \t\r\n=":
                j -= 1
            k = j
            while k >= 0 and (text[k].isalnum() or text[k] in "_.-"):
                k -= 1
            name = text[k + 1:j + 1]
            stack.append((name, i + 1))
            i += 1
            continue
        if ch == "}":
            if stack:
                name, start = stack.pop()
                yield name, text[start:i], [s[0] for s in stack]
            i += 1
            continue
        i += 1


def _num(body, key):
    m = re.search(r"\b%s\s*=\s*(-?\d+)" % key, body)
    return int(m.group(1)) if m else None


def _flag(body, key):
    m = re.search(r"\b%s\s*=\s*(yes|no|true|false)" % key, body, re.I)
    if not m:
        return None
    return m.group(1).lower() in ("yes", "true")


def _block(body, key):
    m = re.search(r"\b%s\s*=\s*\{" % key, body)
    if not m:
        return None
    i = m.end()
    depth, start = 1, i
    while i < len(body) and depth:
        if body[i] == "{":
            depth += 1
        elif body[i] == "}":
            depth -= 1
        i += 1
    return body[start:i - 1] if depth == 0 else None


def _snippet(body, key, width=180):
    b = _block(body, key)
    if b is None:
        return None
    s = " ".join(b.split())
    return s[:width] + ("..." if len(s) > width else "")


def _parse_file(path, out):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = _strip_comments(f.read())
    except OSError:
        return
    for key, body, ancestors in _walk(text):
        if not key or not key[0].isalpha():
            continue
        low = body.lower()
        if not any(re.search(r"\b%s\s*=" % mk, low) for mk in MARKERS):
            continue
        # a category definition is `x = { icon = ... }` with no decision markers
        if "category" in key.lower() and "available" not in low:
            continue
        info = out.setdefault(key, {})
        info.update({
            "key": key,
            "category": ancestors[-1] if ancestors else None,
            "cost_pp": _num(body, "cost"),
            "days_remove": _num(body, "days_remove"),
            "days_mission_timeout": _num(body, "days_mission_timeout"),
            "days_re_enable": _num(body, "days_re_enable"),
            "fire_only_once": _flag(body, "fire_only_once"),
            "is_good": _flag(body, "is_good"),
            "has_available": _block(body, "available") is not None,
            "has_visible": _block(body, "visible") is not None,
            "has_activation": _block(body, "activation") is not None,
            "available": _snippet(body, "available"),
            "visible": _snippet(body, "visible"),
            "activation": _snippet(body, "activation"),
            "file": os.path.basename(path),
        })


def build(force=False):
    root = os.path.join(config.game_dir(), "common", "decisions")
    out = {}
    n = 0
    if os.path.isdir(root):
        for dirpath, _dirnames, files in os.walk(root):
            # categories/ describes the tabs, not the decisions themselves
            if os.path.basename(dirpath).lower() == "categories":
                continue
            for fn in sorted(files):
                if fn.lower().endswith(".txt"):
                    _parse_file(os.path.join(dirpath, fn), out)
                    n += 1
    data = {"schema": SCHEMA, "files": n, "count": len(out), "decisions": out}
    try:
        with open(CACHE, "wb") as f:
            pickle.dump(data, f, protocol=4)
    except OSError:
        pass
    return data


_MEM = None


def load(force=False):
    global _MEM
    if not force and _MEM is None and os.path.exists(CACHE):
        try:
            with open(CACHE, "rb") as f:
                cached = pickle.load(f)
            if isinstance(cached, dict) and cached.get("schema") == SCHEMA:
                _MEM = cached
        except Exception:
            _MEM = None
    if force or _MEM is None:
        _MEM = build(force)
    return _MEM


def get(key):
    return load()["decisions"].get(key)
