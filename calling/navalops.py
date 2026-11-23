"""Naval task-force orders through the player command queue (HOI4 1.19.4.1).

Layouts verified live on 2026-11-08:
  CFleet   +0xB0  CPdxArray<CTaskForce*>  (13/12 entries on Home Command)
  CTaskForce +0x18  handle {type=51, id}
  CTaskForce +0xA0  ship count (observed 14)
Constructors (cmdctors.pkl, verified against the binary):
  CNavalMissionSetTypeCommand  0x2253A90  (this, CTaskForce*, int mission)
  CNavalMissionAddRegionCommand 0x2255340 (this, CTaskForce*, int region)
  CNavalMissionRemoveRegionCommand 0x22557B0 (this, CTaskForce*, int region)
"""
import struct

import api
from armyops import ALLOC, RUN_CMD, _mkstring, _readstring

SET_TYPE_CTOR = 0x2253A90
ADD_REGION_CTOR = 0x2255340
REMOVE_REGION_CTOR = 0x22557B0

SET_TYPE_SIZE = 0x50
ADD_REGION_SIZE = 0x40


def _arr(p, obj, off):
    data = p.u64(obj + off)
    n = p.i32(obj + off + 0xC)
    if not data or n <= 0 or n > 4096:
        return []
    raw = p.read(data, n * 8)
    return [struct.unpack_from("<Q", raw, i * 8)[0] for i in range(n)]


def fleet_task_forces(g, fleet_ptr):
    p = g.p
    out = []
    for i, tf in enumerate(_arr(p, fleet_ptr, 0xB0)):
        if not tf:
            continue
        out.append({
            "index": i,
            "ptr": tf,
            "handle": list(struct.unpack("<ii", p.read(tf + 0x18, 8))),
            "ships": p.i32(tf + 0xA0),
        })
    return out


def _run(g, cmd, what):
    s = _mkstring(g)
    ok = g.call(RUN_CMD, [cmd, s]) & 0xFF
    return {"ok": bool(ok), "error": "" if ok else (_readstring(g, s) or "command rejected"), "op": what}


def set_mission(g, tf_ptr, mission):
    api.allow_call(SET_TYPE_CTOR)
    cmd = g.malloc(SET_TYPE_SIZE)
    g.p.write(cmd, b"\0" * SET_TYPE_SIZE)
    g.call(SET_TYPE_CTOR, [cmd, tf_ptr, int(mission)])
    return _run(g, cmd, "set_mission")


def add_region(g, fleet_ptr, region_id):
    """CNavalMissionAddRegionCommand reads the FLEET handle at arg1+0x08."""
    api.allow_call(ADD_REGION_CTOR)
    cmd = g.malloc(ADD_REGION_SIZE)
    g.p.write(cmd, b"\0" * ADD_REGION_SIZE)
    g.call(ADD_REGION_CTOR, [cmd, fleet_ptr, int(region_id)])
    return _run(g, cmd, "add_region")


def remove_region(g, tf_ptr, region_id):
    api.allow_call(REMOVE_REGION_CTOR)
    cmd = g.malloc(ADD_REGION_SIZE)
    g.p.write(cmd, b"\0" * ADD_REGION_SIZE)
    g.call(REMOVE_REGION_CTOR, [cmd, tf_ptr, int(region_id)])
    return _run(g, cmd, "remove_region")
