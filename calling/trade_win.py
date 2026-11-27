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


_COMMON_EXPORTERS = {
    "oil": ("ROM", "SOV", "VEN", "USA", "IRQ", "PER", "MEX", "INS"),
    "rubber": ("INS", "MAL", "SIA", "BRA", "ENG", "FRA"),
    "tungsten": ("POR", "SWE", "SOV", "USA", "CHI"),
    "steel": ("SWE", "FRA", "USA", "SOV", "ENG", "GER"),
    "aluminium": ("FRA", "HUN", "YUG", "USA", "SOV", "NOR"),
    "chromium": ("TUR", "SOV", "SAF", "CUB", "YUG", "GRE"),
    "coal": ("GER", "ENG", "USA", "SOV", "POL"),
}


def suggest_exporters(g, resource="oil", importer_tag=None, limit=3):
    """纯只读推荐当前非敌对且有该资源出口能力/盈余的真实国家 TAG 列表（毫秒级，零注入）。"""
    rkey = _RES_ALIASES.get(str(resource or "").strip().lower())
    if not rkey:
        return []
    try:
        me = str(importer_tag or g.player_tag() or "").upper()
    except Exception:
        me = str(importer_tag or "").upper()
    enemies = set()
    try:
        import combat as _cb
        enemies = {str(x).upper() for x in (_cb.enemy_tags(g, me) or ())}
    except Exception:
        enemies = set()
    try:
        all_tags = {str(t).upper() for t in (g.tags() or ())}
    except Exception:
        all_tags = set()

    # 1. 优先从全球活跃贸易账本统计正在出口该资源的卖方（说明其贸易法允许出口且有供给）
    active_export_amt = {}
    try:
        for row in (world_trades(g) or ()):
            if row.get("resource") != rkey:
                continue
            src = str(row.get("from") or "").upper()
            if not src or src == me or src in enemies:
                continue
            active_export_amt[src] = active_export_amt.get(src, 0.0) + float(row.get("amount") or 0.0)
    except Exception:
        pass

    cands = set(active_export_amt.keys())
    for t in _COMMON_EXPORTERS.get(rkey, ()):
        if t != me and t not in enemies and (not all_tags or t in all_tags):
            cands.add(t)

    scored = []
    for t in cands:
        surplus = 0.0
        try:
            res = g.resources(t) or {}
            surplus = float(res.get(rkey) or 0.0)
        except Exception:
            surplus = 0.0
        exp_amt = float(active_export_amt.get(t) or 0.0)
        if surplus >= 4.0 or exp_amt >= 8.0:
            scored.append((surplus + exp_amt * 0.25, surplus, exp_amt, t))
    scored.sort(key=lambda x: (-x[0], x[3]))
    return [t for _s, _sur, _exp, t in scored[:max(1, int(limit))]]


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


def _proc_alive(p):
    """检测目标进程是否仍在运行（防止进程退出后 _book 吞异常把失败伪造成 amount_after=0.0）。"""
    if p is None:
        return False
    base = int(getattr(p, "base", 0) or 0)
    if base <= 0:
        return True
    try:
        hdr = p.read(base, 4)
        return bool(hdr) and len(hdr) >= 2
    except Exception:
        return False


def _current_import_amount_checked(g, exporter_ptr, importer_tag_id, ridx):
    """返回 `(amount, alive)`：进程死亡/内存不可读时明确返回 `(None, False)`，绝不假报 0.0。"""
    p = g.p
    try:
        res = p.u64(exporter_ptr + COUNTRY_RESOURCES)
    except Exception:
        if not _proc_alive(p):
            return None, False
        return None, True
    if not res:
        if not _proc_alive(p):
            return None, False
        return 0.0, True
    for slot, partner, amt, _e in _entries(p, exporter_ptr):
        if slot == ridx and partner == importer_tag_id:
            return round(amt, 2), True
    if not _proc_alive(p):
        return None, False
    return 0.0, True


def _current_import_amount(g, exporter_ptr, importer_tag_id, ridx):
    amt, alive = _current_import_amount_checked(g, exporter_ptr, importer_tag_id, ridx)
    if not alive or amt is None:
        return 0.0
    return amt


def create_trade(g, exporter_tag, resource, amount, importer_tag=None, dry_run=False):
    """通过引擎原生 CCreateTradeCommand 建立/调整/取消资源进口贸易。

    amount 为目标资源总进口单位数（如 8 = 1 座民工买 8 单位，16 = 2 座民工，0 = 取消贸易）。

    2026-11-26 (P0-1 根治):
      1. 发令前纯只读校验交战状态（`CanExecute` 0x16DABF0 不校验交战），交战时 0 注入拦截并推荐卖方；
      2. 若当前已等于目标进口量则幂等短路返回 `ok=True`，不浪费时钟或触碰引擎删除重建路径；
      3. 显式挂接 `vt_rva=VT_CREATE_TRADE` 私有虚表，并通过改造后的 `post_drained`（暂停态先入队再短暂解暂停 + `done_fn`）消除 TBB `hourlyUpdateUnits` 线程竞态；
      4. 回读循环实时校验进程存活，若进程异常退出明确回报 `alive=False, amount_after=None, blocked_by="process_exited"`，绝不伪造 `amount_after=0.0`。
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

    # 纯只读前置护栏 1：交战拦截（CCreateTradeCommand::CanExecute 不校验敌对状态）
    if amt > 0:
        try:
            import combat as _cb
            enemies = {str(x).upper() for x in (_cb.enemy_tags(g, imp.tag) or ())}
        except Exception:
            enemies = set()
        if str(exp.tag).upper() in enemies:
            sugg = suggest_exporters(g, rkey, imp.tag, limit=3)
            return {
                "ok": False,
                "blocked_by": "at_war",
                "importer": imp.tag,
                "exporter": exp.tag,
                "resource": rkey,
                "amount_requested": amt,
                "suggested_exporters": sugg,
                "error": "cannot import %s from %s: %s is currently at war with %s"
                % (rkey, exp.tag, imp.tag, exp.tag),
            }

    ridx = RESOURCE_NAMES.index(rkey) + 1
    rptr = resource_pointers(g).get(rkey)
    if not rptr:
        return {
            "ok": False,
            "error": "could not resolve CStrategicResource* for %s" % rkey,
        }

    before_amt, alive0 = _current_import_amount_checked(g, exp.ptr, imp.tag_id, ridx)
    if not alive0 or before_amt is None:
        return {
            "ok": False,
            "alive": False,
            "blocked_by": "process_exited",
            "importer": imp.tag,
            "exporter": exp.tag,
            "resource": rkey,
            "amount_requested": amt,
            "amount_before": None,
            "amount_after": None,
            "error": "hoi4.exe process is not readable before trade command",
        }

    # 采集卖方资源余量诊断（纯只读，不影响执行，用于解释为何被截断）
    exporter_surplus = None
    try:
        _eres = g.resources(exp.tag) or {}
        if isinstance(_eres, dict) and rkey in _eres and _eres[rkey] is not None:
            exporter_surplus = round(float(_eres[rkey]), 2)
    except Exception:
        exporter_surplus = None

    # 幂等短路：非 dry_run 且目标数量与当前完全一致时，无需发令
    if (not dry_run) and abs(before_amt - amt) < 0.01:
        out_idem = {
            "ok": True,
            "verified": True,
            "posted": False,
            "already_at_target": True,
            "can_execute": True,
            "importer": imp.tag,
            "exporter": exp.tag,
            "resource": rkey,
            "amount_requested": amt,
            "amount_before": before_amt,
            "amount_after": before_amt,
            "civ_factories_cost_est": (amt + 7) // 8 if amt > 0 else 0,
        }
        if exporter_surplus is not None:
            out_idem["exporter_resource_surplus"] = exporter_surplus
        return out_idem

    def _trade_done():
        cur, ok_alive = _current_import_amount_checked(g, exp.ptr, imp.tag_id, ridx)
        if not ok_alive:
            return True
        if cur is None:
            return False
        return abs(cur - amt) < 0.01 or abs(cur - before_amt) >= 0.01

    import win_pipeline as _wp
    pipe = _wp.WinCommandPipeline(g, only_if_live=True)
    res = pipe.run(
        build_fn=lambda _g: cmdwin.build_by_vt(_g, VT_CREATE_TRADE, SIZE_CREATE_TRADE, [
            (0x28, "<i", int(imp.tag_id)),
            (0x2C, "<i", int(exp.tag_id)),
            (0x30, "<Q", int(rptr)),
            (0x38, "<i", amt),
        ]),
        vt_rva=VT_CREATE_TRADE,
        can_exec_rva=CANEXEC_CREATE_TRADE,
        drain=True,
        seconds=1.5,
        done_fn=_trade_done,
        dry_run=dry_run,
    )
    if dry_run:
        out_dry = {
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
        if exporter_surplus is not None:
            out_dry["exporter_resource_surplus"] = exporter_surplus
        return out_dry
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
    alive = True
    if posted:
        deadline = _time.time() + 2.0
        while True:
            cur_amt, alive = _current_import_amount_checked(g, exp.ptr, imp.tag_id, ridx)
            if not alive:
                after_amt = None
                break
            if cur_amt is not None:
                after_amt = cur_amt
                if abs(after_amt - amt) < 0.01 or abs(after_amt - before_amt) >= 0.01:
                    break
            if _time.time() >= deadline:
                break
            _time.sleep(0.08)

    if not alive:
        return {
            "ok": False,
            "alive": False,
            "verified": False,
            "posted": posted,
            "can_execute": True,
            "blocked_by": "process_exited",
            "importer": imp.tag,
            "exporter": exp.tag,
            "resource": rkey,
            "amount_requested": amt,
            "amount_before": before_amt,
            "amount_after": None,
            "error": (
                "hoi4.exe process exited or crashed during CCreateTradeCommand "
                "(%s <- %s, %s: %.1f -> %d); amount_after is unavailable (not 0.0)"
                % (imp.tag, exp.tag, rkey, before_amt, amt)
            ),
        }

    verified = posted and after_amt is not None and (
        abs(after_amt - amt) < 0.01
        or (amt > 0 and after_amt > before_amt)
        or (amt == 0 and after_amt == 0.0)
    )
    out = {
        "ok": bool(verified),
        "alive": True,
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
    if exporter_surplus is not None:
        out["exporter_resource_surplus"] = exporter_surplus
    if not verified:
        out["error"] = (
            "trade command posted but %s export book to %s for %s stayed %.1f -> %.1f "
            "(target %d; exporter may lack exportable surplus, be embargoed/at war, or game paused)"
            % (exp.tag, imp.tag, rkey, before_amt, after_amt, amt)
        )
    return out
