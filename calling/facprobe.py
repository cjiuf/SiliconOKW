"""Per-state factory census, to explain the 46/32/13 vs 38/29/10 gap.

`Country._win_building_counts()` sums building levels over CCountry+0x460
(CONTROLLED states) and reports 46/32/13 for GER 1936, while BOTH the industry
panel's tooltip and `get_var num_of_{civilian,military,naval}_factories` say
38/29/10.  This module prints the per-state breakdown so the difference can be
seen instead of guessed at.
"""
import struct

BLD_CIV, BLD_MIL, BLD_DOCK = 20013, 20002, 19446


def state_rows(g, tag=None, list_off=0x460):
    """[{id, core, civ, mil, dock}] for one of the country's state lists."""
    from game import _arr, ST_BUILDINGS, BG_TYPE_ID, BG_LEVEL
    p = g.p
    c = g._c(tag or g.player_tag())
    g._load_buildings()
    out = []
    for s in _arr(p, c.ptr, list_off):
        row = {"id": p.i32(s + 0x58), "ptr": s,
               "noncore": p.i32(s + 0x2F0) == 1,
               "civ": 0, "mil": 0, "dock": 0}
        for b in _arr(p, s, ST_BUILDINGS):
            t = g._bld_by_id.get(p.i32(b + BG_TYPE_ID))
            lv = struct.unpack("<h", p.read(b + BG_LEVEL, 2))[0]
            if t == "industrial_complex":
                row["civ"] += lv
            elif t == "arms_factory":
                row["mil"] += lv
            elif t == "dockyard":
                row["dock"] += lv
        out.append(row)
    return out


def census(g, tag=None):
    rows = state_rows(g, tag, 0x460)
    core = [r for r in rows if not r["noncore"]]
    return {
        "states": len(rows),
        "core_states": len(core),
        "civ": sum(r["civ"] for r in rows),
        "mil": sum(r["mil"] for r in rows),
        "dock": sum(r["dock"] for r in rows),
        "core_civ": sum(r["civ"] for r in core),
        "core_mil": sum(r["mil"] for r in core),
        "core_dock": sum(r["dock"] for r in core),
        "rows": rows,
    }
