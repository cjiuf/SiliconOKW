"""Focus policy: keep one national focus running, chosen from the engine's own
ranked list.

`Game.selectable_focuses()` already scores every available focus (historical plan
position, annex/wargoal, research slot, industry, snowball gates) and asks the
engine whether it can actually be started. This module only makes the decision
"when nothing is running, take the best one", so the campaign never sits idle
because nobody clicked the tree.
"""
import os
import json

HERE = os.path.dirname(os.path.abspath(__file__))
CFG_PATH = os.path.join(HERE, "staff_config.json")

DEFAULTS = {"enabled": True, "shadow": False, "min_score": 0}


def cfg():
    out = dict(DEFAULTS)
    try:
        with open(CFG_PATH, encoding="utf-8") as f:
            doc = json.load(f)
        out.update((doc.get("focus") or {}))
    except Exception:
        pass
    return out


def plan(g, tag=None):
    c = cfg()
    try:
        cur = g.current_focus(tag)
    except Exception as e:
        return {"ok": False, "error": repr(e)}
    running = cur
    if running:
        return {"ok": True, "running": {"key": running.get("key"), "name": running.get("name")},
                "action": None}
    try:
        sel = g.selectable_focuses(tag) or {}
    except Exception as e:
        return {"ok": False, "error": repr(e)}
    rows = sel.get("selectable") or []
    rows = [r for r in rows if isinstance(r, dict)]
    if not rows:
        return {"ok": True, "action": None, "reason": "nothing selectable"}
    best = max(rows, key=lambda r: (r.get("score") or 0))
    if (best.get("score") or 0) < c["min_score"]:
        return {"ok": True, "action": None, "reason": "best score below floor", "best": best}
    return {"ok": True, "action": "set_focus", "key": best.get("key"),
            "name": best.get("name"), "score": best.get("score"),
            "tier": best.get("tier"), "why": best.get("reason"),
            "candidates": len(rows)}


def apply(g, doc=None, tag=None):
    c = cfg()
    doc = doc or plan(g, tag)
    if c["shadow"] or doc.get("action") != "set_focus":
        return {"ok": True, "skipped": True}
    try:
        r = g.set_focus(doc["key"], tag)
        return {"ok": bool(r.get("ok")), "key": doc["key"], "result": r}
    except Exception as e:
        return {"ok": False, "error": repr(e)}
