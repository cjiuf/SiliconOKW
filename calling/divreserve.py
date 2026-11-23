"""Paratrooper exemption for every automatic land-war dispatcher.

Measured 2026-11-10 (run 13, 1936 France drop): a division that is not executing
a front order is treated as "idle reserve" by four independent pieces of our own
tooling:

  * ``movemicro._front_state``  -> _plug_state / _org_cycle / _free_pool /
    _stage_reserves / _width_attack / _forward_rear_divisions / _fill_reserve_hubs
    (driven by the gateway ``front_worker`` thread every 8 s);
  * ``game.activate_army_battleplans`` -> draw_front_line + offensive_line +
    execute_plan (called by housekeep and by deploy_to_border);
  * ``game.housekeep_divisions`` -> create_army(unassigned);
  * ``movemicro.deploy_to_border`` -> move every division to the border.

A paratrooper is idle BY DESIGN: it waits in the province whose air base hosts
the transport wing until the paradrop order is executed.  Every path above moved
it away, which is what "something keeps stealing my orders" really was.

A division is reserved when ANY of these holds:
  1. the division-template name contains a paratroop marker
     ("paratroop", "parachut", "fallschirm", "伞", "空降");
  2. its current order (CDivision+0x20 -> COrderInstance+0x30) is type 4;
  3. it belongs to an army (COrdersGroup) that owns a type-4 order;
  4. the caller passes an explicit set of division pointers.

The module deliberately has no import of game/armyops at module level: game.py
and movemicro.py both import it, and armyops imports game.
"""
from __future__ import annotations

# CDivision / COrderInstance layout, verified live on 1.19.4.1.99a5
CDIV_ORDER = 0x20          # COrderInstance* currently executed
CDIV_TEMPLATE = 0x3B8      # CDivisionTemplate*
ORDER_TYPE = 0x30          # 4 == paradrop
DT_NAME = 0x20
CO_ARMIES = 0x290
ORDER_PARADROP = 4

PARA_NAME_KEYS = ("paratroop", "parachut", "fallschirm", "伞", "空降")

_TMPL_CACHE = {}           # (pid, template_ptr) -> bool
_TMPL_NAME = {}            # (pid, template_ptr) -> str


def _pid(g):
    try:
        return g.p.pid
    except Exception:
        return 0


def template_name(g, tmpl_ptr):
    """Name of a CDivisionTemplate*, cached per (pid, pointer)."""
    if not tmpl_ptr:
        return ""
    key = (_pid(g), int(tmpl_ptr))
    if key in _TMPL_NAME:
        return _TMPL_NAME[key]
    try:
        from game import item_name
        name = item_name(g.p, int(tmpl_ptr) + DT_NAME) or ""
    except Exception:
        name = ""
    _TMPL_NAME[key] = name
    return name


def is_para_template(g, tmpl_ptr):
    """True when the template is a paratrooper template (cached)."""
    if not tmpl_ptr:
        return False
    key = (_pid(g), int(tmpl_ptr))
    if key in _TMPL_CACHE:
        return _TMPL_CACHE[key]
    name = template_name(g, tmpl_ptr).lower()
    val = any(k in name for k in PARA_NAME_KEYS)
    _TMPL_CACHE[key] = val
    return val


def order_type(g, div_ptr):
    """Type of the order the division is currently executing (None if idle)."""
    try:
        o = g.p.u64(div_ptr + CDIV_ORDER)
        if not o:
            return None
        return g.p.i32(o + ORDER_TYPE)
    except Exception:
        return None


def is_paradrop_division(g, div_ptr):
    """(reserved, reason) for one division pointer."""
    try:
        tmpl = g.p.u64(div_ptr + CDIV_TEMPLATE)
    except Exception:
        tmpl = 0
    if is_para_template(g, tmpl):
        return True, "template:%s" % template_name(g, tmpl)
    if order_type(g, div_ptr) == ORDER_PARADROP:
        return True, "order:paradrop"
    return False, None


def army_is_paradrop(g, og):
    """(reserved, reason) for a whole COrdersGroup.

    An army is reserved when it owns a paradrop order or when every one of its
    divisions is a paratrooper.  The first test is the important one: a mixed
    army executing a drop must not be given a front line either.
    """
    import armyops
    try:
        for o in armyops.orders_of(g, og):
            if int(o.get("type") or 0) == ORDER_PARADROP:
                return True, "order:%s" % o.get("id")
    except Exception:
        pass
    try:
        divs = armyops.group_divisions(g, og) or []
    except Exception:
        divs = []
    if divs and all(is_paradrop_division(g, d)[0] for d in divs):
        return True, "all-paratroopers"
    return False, None


def scan(g, tag=None):
    """Every division of `tag` (default: player) with its reservation flag."""
    from game import _arr
    p = g.p
    c = g._c(tag)
    out = []
    try:
        arr = _arr(p, c.ptr, CO_ARMIES)
    except Exception:
        return out
    for d in arr:
        try:
            reserved, why = is_paradrop_division(g, d)
            out.append({
                "division": hex(d),
                "province": _province(g, d),
                "template": template_name(g, p.u64(d + CDIV_TEMPLATE)),
                "order_type": order_type(g, d),
                "reserved": reserved,
                "reason": why,
            })
        except Exception:
            continue
    return out


def _province(g, d):
    try:
        import unitorders
        return unitorders.province_of(g, d)
    except Exception:
        return None


def reserved(g, tag=None):
    rows = [r for r in scan(g, tag) if r["reserved"]]
    return {"count": len(rows), "divisions": rows}


def filter_out(g, divs, explicit=None):
    """Split `divs` into (kept, skipped).  `explicit` is a set of reserved ptrs."""
    explicit = set(int(x) for x in (explicit or ()))
    kept, skipped = [], []
    for d in divs:
        try:
            if int(d) in explicit:
                skipped.append({"division": hex(d), "reason": "explicit"})
                continue
            reserved, why = is_paradrop_division(g, d)
        except Exception:
            reserved, why = False, None
        if reserved:
            skipped.append({"division": hex(d), "reason": why})
        else:
            kept.append(d)
    return kept, skipped
