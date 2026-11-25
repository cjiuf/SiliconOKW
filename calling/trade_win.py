"""Windows (PE) 1.19.5.0 resource-trade reader.

Why this module exists
----------------------
`hoi4_trade_routes` used to read `CCountry + 0x112B08`, which is a Linux
`CCurrentGameState` offset.  On hoi4.exe that address is ~1.1 MB past the end of
a CCountry, so the tool reported `count: 32758` (the high word 0x7FF6 of a
pointer) and 32758 fabricated rows.  It was then disabled with "no verified
container exists" -- which was true for `CResourceDeliveryRoute` but wrong about
the data being unreachable.

Measured on the live PE build 2026-11-18/19 (all RVAs from research/windows):

  CCountry + 0x11F8          CCountryResources*   (NOT 0x11C0; the curated
                             subsystem table had `resources` at 0x11C0 with
                             class `None`, which is why it was never used)

  CCountryResources + 0x728  data pointer of a CPdxArray<CPdxArray<T*>>
                             (capacity +0x730, size +0x734)
      slot i (0x18 bytes each) = the country's EXPORT book for resource i+... :
          +0x00 data ptr of CPdxArray<CResourceExchange*>
          +0x08 capacity
          +0x0C size
      Slot 0 is unused; slots 1..7 map to the seven strategic resources in the
      order of `common/resources/00_resources.txt` (= `icon_frame`):
          oil, aluminium, rubber, tungsten, steel, chromium, coal

  CResourceExchange (0xF0 bytes, vtable 0x29D8E60, ctor 0xD046C0):
          +0x88  u32  CCountryTag of the OTHER side (the importer)
          +0x90  CStrategicResource*   (its own +0xD8 is the resource index)
          +0x98  u32  amount, CARGO fixed point (100000 = 1 unit)
          +0xB0  CGregorianDate
          +0xC8  CGregorianDate

  The relationship is stored ONCE, on the supplier's side.  Proof: the whole
  world holds exactly 131 CResourceExchange objects and walking every country's
  export book yields exactly 131 entries -- a 1:1 match, so a deal is never
  written to both countries.  Germany 1936.03 has one entry (coal -> ITA) and
  Italy's book is empty, i.e. Germany is the supplier; the same test on
  USA -> ENG (16 oil) matches history rather than USA importing oil from
  Britain.  Imports are therefore DERIVED: scan every country's export book and
  keep the entries whose +0x88 is the tag you asked about.

Confirmed live on pid 61964 (1936.03.27): 131 routes world-wide, spread over
the seven resources as oil 56 / steel 38 / rubber 19 / aluminium 12 /
tungsten 3 / chromium 2 / coal 1 -- the shape you expect (oil the most traded,
coal almost never).

Read-only: no command objects, no ctor calls, no writes.
"""
from __future__ import annotations

import struct
import offsets_win as _OW

#: CCountry -> CCountryResources
COUNTRY_RESOURCES = _OW.CO_RESOURCES
#: CCountryResources -> data ptr of CPdxArray<CPdxArray<CResourceExchange*>>
TRADE_BOOK = _OW.TRADE_BOOK
TRADE_BOOK_CAP = _OW.TRADE_BOOK_CAP
TRADE_BOOK_SIZE = _OW.TRADE_BOOK_SIZE
SLOT_STRIDE = _OW.TRADE_SLOT_STRIDE
#: CResourceExchange fields
EX_PARTNER = _OW.TRADE_EX_PARTNER
EX_RESOURCE = _OW.TRADE_EX_RESOURCE
EX_AMOUNT = _OW.TRADE_EX_AMOUNT
EX_SIZE = _OW.TRADE_EX_SIZE
#: CStrategicResource -> its own index (1..7)
RES_INDEX = _OW.TRADE_RES_INDEX

FIXED = 100000.0

#: `common/resources/00_resources.txt` order == `icon_frame` == the slot index.
RESOURCE_NAMES = ["oil", "aluminium", "rubber", "tungsten", "steel",
                  "chromium", "coal"]


def resource_name(slot):
    """Trade-book slot (1-based) -> resource key ('' when out of range).

    Slot 0 is never used; slots 1..7 are the seven resources in file order,
    so `slot - 1` indexes RESOURCE_NAMES.
    """
    if 1 <= slot <= len(RESOURCE_NAMES):
        return RESOURCE_NAMES[slot - 1]
    return ""


def _book(p, country_ptr):
    """(outer data ptr, outer size) of one country's trade book, or None."""
    try:
        res = p.u64(country_ptr + COUNTRY_RESOURCES)
        if not res:
            return None
        outer = p.u64(res + TRADE_BOOK)
        size = p.u32(res + TRADE_BOOK_SIZE)
        if not outer or not (0 < size <= 32):
            return None
        return res, outer, size
    except Exception:
        return None


def _entries(p, country_ptr):
    """[(resource_index, partner_tag_id, amount_units, exchange_ptr), ...].

    This is the country's EXPORT book: every returned row means "this country
    sells `amount` of `resource` to `partner`".
    """
    b = _book(p, country_ptr)
    if not b:
        return []
    _res, outer, size = b
    out = []
    try:
        head = p.read(outer, size * SLOT_STRIDE)
    except Exception:
        return []
    for i in range(size):
        base = i * SLOT_STRIDE
        data = struct.unpack_from("<Q", head, base)[0]
        n = struct.unpack_from("<i", head, base + 0x0C)[0]
        if not data or not (0 < n < 512):
            continue
        try:
            blk = p.read(data, n * 8)
        except Exception:
            continue
        for k in range(n):
            e = struct.unpack_from("<Q", blk, k * 8)[0]
            if not e or e < 0x10000000000:
                continue
            try:
                row = p.read(e + EX_PARTNER, 0x18)
            except Exception:
                continue
            partner = struct.unpack_from("<I", row, 0)[0]
            amount = struct.unpack_from("<I", row, EX_AMOUNT - EX_PARTNER)[0]
            out.append((i, partner, amount / FIXED, e))
    return out


def tag_id_map(g):
    """{CCountryTag id (u32) -> 'GER'} for every country."""
    m = {}
    for t in g.tags():
        try:
            m[g._c(t).tag_id] = t
        except Exception:
            continue
    return m


def country_trades(g, tag=None, with_imports=True):
    """One country's export and import books."""
    c = g._c(tag)
    p = g.p
    ids = tag_id_map(g)
    me = c.tag_id
    exports = []
    for ridx, partner, amount, _e in _entries(p, c.ptr):
        exports.append({"resource": resource_name(ridx), "resource_index": ridx,
                        "to": ids.get(partner, "#%d" % partner),
                        "to_tag_id": partner, "amount": round(amount, 2)})
    imports = []
    scanned = 0
    if with_imports:
        for t in g.tags():
            try:
                other = g._c(t)
            except Exception:
                continue
            if other.tag_id == me:
                continue
            scanned += 1
            try:
                for ridx, partner, amount, _e in _entries(p, other.ptr):
                    if partner == me:
                        imports.append({"resource": resource_name(ridx),
                                        "resource_index": ridx,
                                        "from": t, "from_tag_id": other.tag_id,
                                        "amount": round(amount, 2)})
            except Exception:
                continue
    return {"exports": exports, "imports": imports, "scanned_countries": scanned}


def world_trades(g):
    """Every export entry on the map: [(supplier, resource, importer, amount)]."""
    p = g.p
    ids = tag_id_map(g)
    rows = []
    for t in g.tags():
        try:
            c = g._c(t)
        except Exception:
            continue
        for ridx, partner, amount, _e in _entries(p, c.ptr):
            rows.append({"from": t, "to": ids.get(partner, "#%d" % partner),
                         "to_tag_id": partner,
                         "resource": resource_name(ridx),
                         "resource_index": ridx,
                         "amount": round(amount, 2)})
    return rows


# ============================================================ CCreateTradeCommand (PE 1.19.5.0)
# 2026-11-23 (C-5): 经 PE 反汇编确认（rtti_win.pkl -> .?AVCCreateTradeCommand@@）：
#   vtable      = 0x2A5F388
#   size        = 0x40
#   CanExecute  = 0x16DABF0  校验 [+0x28] 与 [+0x2C] 合法国家，且 [[+0x30]+0x10] != 0
#   Execute     = 0x16DA8A0  调用 0xD15640(ecx=[+0x2C] 卖方, edx=[+0x28] 买方,
#                                          r8=[+0x30] CStrategicResource*, r9d=[+0x38] 单位数)
#                            内部乘以 100000 (0x186A0) 写入卖方出口账本；amount=0 则取消贸易。
VT_CREATE_TRADE = _OW.TRADE_VT_CREATE
SIZE_CREATE_TRADE = _OW.TRADE_SIZE_CREATE
CANEXEC_CREATE_TRADE = _OW.TRADE_CANEXEC_CREATE

_RES_ALIASES = {
    "oil": "oil",
    "aluminium": "aluminium",
    "aluminum": "aluminium",
    "rubber": "rubber",
    "tungsten": "tungsten",
    "steel": "steel",
    "chromium": "chromium",
    "coal": "coal",
}

import session_state as _SS

_RES_PTR_CACHE = {}
_SS.register_cache("trade_win._RES_PTR_CACHE", container=_RES_PTR_CACHE)


def resource_pointers(g, force=False):
    """返回 {resource_name: CStrategicResource*}（按 pid 缓存，0.01s O(1) 收集全 7 种）。"""
    p = g.p
    pid = getattr(p, "pid", None)
    if not force and pid is not None and _RES_PTR_CACHE.get(pid):
        cached = _RES_PTR_CACHE[pid]
        if len(cached) >= len(RESOURCE_NAMES):
            return dict(cached)
    out = dict(_RES_PTR_CACHE.get(pid) or {}) if (not force and pid is not None) else {}
    # 1. 最快零堆扫路径：直接从全球现有贸易记录 CResourceExchange+0x90 读取 CStrategicResource*
    for t in g.tags():
        if len(out) >= len(RESOURCE_NAMES):
            break
        try:
            co = g._c(t)
        except Exception:
            continue
        for ridx, _partner, _amt, ex_ptr in _entries(p, co.ptr):
            rname = resource_name(ridx)
            if not rname or rname in out:
                continue
            try:
                rp = p.u64(ex_ptr + EX_RESOURCE)
                if (0x10000000000 < rp < 0x7F0000000000
                        and p.i32(rp + RES_INDEX) == ridx
                        and p.u8(rp + 0x10) != 0):
                    out[rname] = rp
            except Exception:
                continue
    # 2. 兜底：若某资源全球 0 交易，用单类虚表扫描补齐
    if len(out) < len(RESOURCE_NAMES) and hasattr(g, "win_scan_class"):
        try:
            for rp in g.win_scan_class("CStrategicResource"):
                try:
                    ridx = p.i32(rp + RES_INDEX)
                    rname = resource_name(ridx)
                    if rname and rname not in out and p.u8(rp + 0x10) != 0:
                        out[rname] = rp
                except Exception:
                    continue
        except Exception as _e:
            import diag as _diag
            _diag.warn("trade_win.resource_pointers.scan", _e)
    if pid is not None and out:
        _RES_PTR_CACHE[pid] = dict(out)
    return out


def _current_import_amount(g, exporter_ptr, importer_tag_id, ridx):
    p = g.p
    for slot, partner, amt, _e in _entries(p, exporter_ptr):
        if slot == ridx and partner == importer_tag_id:
            return round(amt, 2)
    return 0.0


def create_trade(g, exporter_tag, resource, amount, importer_tag=None, dry_run=False):
    """通过引擎原生 CCreateTradeCommand 建立/调整/取消资源进口贸易。

    amount 为目标资源总进口单位数（如 8 = 1 座民工买 8 单位，16 = 2 座民工，0 = 取消贸易）。
    """
    import cmdwin
    import time as _time

    rkey = _RES_ALIASES.get(str(resource or "").strip().lower())
    if not rkey:
        return {
            "ok": False,
            "error": "unknown resource %r (expected one of: %s)"
            % (resource, ", ".join(RESOURCE_NAMES)),
        }
    try:
        amt = int(amount)
    except Exception:
        return {"ok": False, "error": "amount must be an integer >= 0 (e.g. 8, 16, or 0 to cancel)"}
    if amt < 0:
        return {"ok": False, "error": "amount must be >= 0 (pass 0 to cancel an existing trade)"}

    imp = g._c(importer_tag)
    exp = g._c(str(exporter_tag).upper())
    if imp.tag_id == exp.tag_id:
        return {"ok": False, "error": "cannot trade with yourself (%s)" % imp.tag}

    ridx = RESOURCE_NAMES.index(rkey) + 1
    rptr = resource_pointers(g).get(rkey)
    if not rptr:
        return {
            "ok": False,
            "error": "could not resolve CStrategicResource* for %s" % rkey,
        }

    before_amt = _current_import_amount(g, exp.ptr, imp.tag_id, ridx)

    import win_pipeline as _wp
    pipe = _wp.WinCommandPipeline(g, only_if_live=True)
    res = pipe.run(
        build_fn=lambda _g: cmdwin.build_by_vt(_g, VT_CREATE_TRADE, SIZE_CREATE_TRADE, [
            (0x28, "<i", int(imp.tag_id)),
            (0x2C, "<i", int(exp.tag_id)),
            (0x30, "<Q", int(rptr)),
            (0x38, "<i", amt),
        ]),
        can_exec_rva=CANEXEC_CREATE_TRADE,
        drain=True,
        seconds=1.5,
        dry_run=dry_run,
    )
    if dry_run:
        return {
            "ok": bool(res.can_execute),
            "dry_run": True,
            "can_execute": bool(res.can_execute),
            "importer": imp.tag,
            "exporter": exp.tag,
            "resource": rkey,
            "amount_requested": amt,
            "amount_before": before_amt,
            "resource_ptr": hex(rptr),
        }
    if res.can_execute is False:
        return {
            "ok": False,
            "blocked_by": "can_execute_refused",
            "can_execute": False,
            "importer": imp.tag,
            "exporter": exp.tag,
            "resource": rkey,
            "amount_requested": amt,
            "amount_before": before_amt,
            "error": "CCreateTradeCommand::CanExecute refused (%s <- %s, %s=%d)"
            % (imp.tag, exp.tag, rkey, amt),
        }
    posted = bool(res.posted)

    after_amt = before_amt
    if posted:
        deadline = _time.time() + 2.0
        while True:
            after_amt = _current_import_amount(g, exp.ptr, imp.tag_id, ridx)
            if abs(after_amt - amt) < 0.01 or abs(after_amt - before_amt) >= 0.01 or _time.time() >= deadline:
                break
            _time.sleep(0.1)

    verified = posted and (
        abs(after_amt - amt) < 0.01
        or (amt > 0 and after_amt > before_amt)
        or (amt == 0 and after_amt == 0.0)
    )
    out = {
        "ok": bool(verified),
        "verified": bool(verified),
        "posted": posted,
        "can_execute": True,
        "importer": imp.tag,
        "exporter": exp.tag,
        "resource": rkey,
        "amount_requested": amt,
        "amount_before": before_amt,
        "amount_after": after_amt,
        "civ_factories_cost_est": (amt + 7) // 8 if amt > 0 else 0,
    }
    if not verified:
        out["error"] = (
            "trade command posted but %s export book to %s for %s stayed %.1f -> %.1f "
            "(target %d; exporter may lack exportable surplus, be embargoed/at war, or game paused)"
            % (exp.tag, imp.tag, rkey, before_amt, after_amt, amt)
        )
    return out
