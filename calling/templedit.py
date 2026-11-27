import os
"""Division-template editing through CUpdateDivisionTemplateCommand.

Reverse-engineered 2026-11-09 on 1.19.4.1.99a5:

  CUpdateDivisionTemplateCommand vtable 0x4457E68 (one reference: 0x766D1B, which
  writes `vt+0x10` into [obj] — the command is stored at +0x10 of the object).
  Its parameterless factory sits at 0x766CE0: `new(0x288)`, zeroes +0x08..+0x22,
  stores the vtable, then a global at +0x270 and 0 at +0x278/+0x280.  The embedded
  CDivisionTemplate lives at +0x28 (its own ctor is called at 0x766D29).

  The 648-byte payload therefore is: [vtable + 0x28-byte command header]
  [embedded CDivisionTemplate][tag at +0x270][+0x278 int][+0x280 ptr].

  The template body is filled with the engine's own deep-copy helper 0x761410
  (the same one CCreateDivisionTemplateCommand uses), so we never hand-build a
  CDivisionTemplate.

Editing = copy the live template in, then rewrite the line-battalion array at
body+0x68 (a CPdxArray<CSubUnitDefinition*> {data, cap, size}) so it contains the
requested number of each battalion type.
"""
import struct

FACTORY = 0x766CE0          # parameterless: returns a fresh CUpdateDivisionTemplateCommand*
DEEPCOPY = 0x761410         # (dst CDivisionTemplate*, src CDivisionTemplate*)
RUN_CMD = 0x20AE0A0
CMD_SIZE = 0x288

# --- Windows 1.19.5.0 (PE) -------------------------------------------------
# Disassembled 2026-11-16 with research/windows/pedis.py, then verified live:
#
#   RTTI .?AVCUpdateDivisionTemplateCommand@@ -> vtable 0x2A26E38
#   factory 0x172F1E2 `mov ecx,0x290; call operator new` -> SIZE = 0x2B0
#   real ctor 0x1C2BC60 (this, handle_block*, data*, int*, int*, tag*, xp*)
#       +0x28 <- CDivisionTemplateData copy ctor 0x138B2B0(dst=this+0x28, src)
#       +0x270 <- packed {type,id} template handle from the 6th stack arg
#       +0x278 <- *tag (i32)       +0x280 <- xp cost (r8)
#   CanExecute 0x1C2DD90 (vt+0x48):
#       +0x270 handle resolves (0x74AE20), +0x278 tag > 0,
#       array/shape validators 0xBF4B40/0xBF4B50/0xBF4D50/0xBF4B60,
#       grid validator 0x1C2D540 (0 == pass),
#       XP cost 0x172CB90(out, oldData, newData, tag) <= country army XP.
#   Execute 0x1C2D040 -> 0xBF18E0(dst live data, src cmd+0x28): reassigns all
#       three array objects and the trailing scalar/string fields, so a SHALLOW
#       byte copy of the live data is safe (the command owns no destructor).
#
#   Live object is CReferencedDivisionTemplate (vptr 0x29CC350):
#       +0x08 packed handle {type=52,id}, +0x20 MSVC std::string name,
#       +0x18 embedded CDivisionTemplateData (vptr 0x29CC300).
#   Arrays are 0x20-byte CPdxArray objects: +0x00 two u16 grid dims,
#       +0x08 data*, +0x10 cap(i32), +0x14 size(i32), +0x18 IAllocator*.
#   Relative to the live template: line object at +0x60, support at +0x80
#       (which is why template_composition reads t+0x68 / t+0x88 for
#        {data, cap, size}). Relative to cmd+0x28 data: +0x48/+0x68/+0x88.
#
#   VERIFIED 2026-11-16 on the running 1939 GER save: posting a command that
#   turns Infanterie-Division from 9 to 10 infantry flipped
#   hoi4_template_composition to {infantry: 10} and the engine deducted its own
#   army XP (749994 -> 429987). CanExecute returned 1 before the post.
#   Adding an artillery_brigade costs 2.5 XP and was correctly REFUSED
#   (country had 0.75 XP): that is a game rule, not a builder failure.
VT_UPDATE_TEMPLATE = 0x2A26E38
UPDATE_TEMPLATE_SIZE = 0x2B0
CANEXEC_UPDATE_TEMPLATE = 0x1C2DD90
PE_DATA_OFF = 0x28          # embedded CDivisionTemplateData inside the command
PE_DATA_SPAN = 0x230        # bytes to shallow-copy from the live template
PE_LINE_OBJ = PE_DATA_OFF + 0x48
PE_SUPP_OBJ = PE_DATA_OFF + 0x68
PE_LIVE_LINE = 0x68         # live template: {data, cap, size} of the line array
PE_LIVE_SUPP = 0x88
PE_NULL_SLOT_VT = 0x29C1178  # TNullObject placeholders in the 5-column grid

# 第二期数据驱动覆盖：优先从 calling/profile.py 读取编制修改命令 RVA
try:
    import profile as _prof
    if _prof.load():
        VT_UPDATE_TEMPLATE = _prof.command_field("CUpdateDivisionTemplateCommand", "vt", VT_UPDATE_TEMPLATE)
        UPDATE_TEMPLATE_SIZE = max(
            UPDATE_TEMPLATE_SIZE,
            int(_prof.command_field("CUpdateDivisionTemplateCommand", "size", UPDATE_TEMPLATE_SIZE)),
        )
        CANEXEC_UPDATE_TEMPLATE = _prof.command_field(
            "CUpdateDivisionTemplateCommand", "can_execute", CANEXEC_UPDATE_TEMPLATE
        )
except Exception:
    pass


def _u64(b, o=0):
    return struct.unpack_from("<Q", b, o)[0]


def _read_cstr(p, addr, n=48):
    try:
        return p.read(addr, n).split(b"\0")[0].decode("utf-8", "replace")
    except Exception:
        return ""


def subunit_name(p, entry):
    """CSubUnitDefinition's script key, on BOTH builds.

    Linux 1.19.4.1 (libstdc++ std::string): buffer/pointer at +0x10, string
    length at +0x18, 16-byte SSO buffer at +0x20.

    Windows 1.19.5.0 (MSVC std::string) -- measured live 2026-11-16 on the
    Infanterie-Division template: the key sits at +0x18 (16-byte SSO buffer),
    size at +0x28, capacity at +0x30. `infantry` reads back as
    0x7972746e61666e69 at +0x18 with size 8 / cap 15. Array slots that are
    TNullObject (empty column placeholders) have size 0 and are skipped.

    Using the Linux layout on the PE build is why template_composition returned
    13 line items with every name == "".
    """
    import sys
    try:
        if sys.platform == "win32":
            n = _u64(p.read(entry + 0x28, 8), 0)
            if not (0 < n < 64):
                return ""
            if n <= 15:
                s = p.read(entry + 0x18, n)
            else:
                q = _u64(p.read(entry + 0x18, 8), 0)
                if q < 0x10000:
                    return ""
                s = p.read(q, n)
        else:
            n = _u64(p.read(entry + 0x18, 8), 0)
            if not (0 < n < 40):
                return ""
            if n <= 15:
                s = p.read(entry + 0x20, n)
            else:
                q = _u64(p.read(entry + 0x10, 8), 0)
                if q < 0x10000:
                    return ""
                s = p.read(q, n)
        return s.decode("utf-8", "replace")
    except Exception:
        return ""


import session_state as _SS

_LIVE_POOL = {"pid": None, "by_name": {}}


def _clear_live_pool():
    _LIVE_POOL["pid"] = None
    _LIVE_POOL["by_name"] = {}


_SS.register_cache("templedit._LIVE_POOL", _clear_live_pool)


_VALID_SUBUNIT_KEY_RE = None


def _is_valid_subunit_key(nm):
    global _VALID_SUBUNIT_KEY_RE
    if _VALID_SUBUNIT_KEY_RE is None:
        import re as _re
        _VALID_SUBUNIT_KEY_RE = _re.compile(r"^[A-Za-z][A-Za-z0-9_]{1,62}$")
    return bool(nm and _VALID_SUBUNIT_KEY_RE.match(nm))


def _batch_subunit_names_win(p, cands):
    """Windows 下通过 `fastmem.read_batch` 批量解码候选 CSubUnitDefinition 名称（与 `subunit_name` 100% 等价）。"""
    import fastmem as _fm
    if not cands:
        return []
    # MSVC std::string 布局：+0x18 处 16 字节 SSO/指针，+0x28 处 8 字节 size（共 24 字节）
    hdrs = _fm.read_batch(p, [(c + 0x18, 24) for c in cands])
    if hdrs is None:
        return [subunit_name(p, c) for c in cands]

    names = [""] * len(cands)
    long_reqs = []  # [(idx, ptr, length), ...]
    for idx, raw in enumerate(hdrs):
        if not raw or len(raw) < 24:
            continue
        n = struct.unpack_from("<Q", raw, 16)[0]
        if not (0 < n < 64):
            continue
        if n <= 15:
            names[idx] = raw[:n].decode("utf-8", "replace")
        else:
            q = struct.unpack_from("<Q", raw, 0)[0]
            if q >= 0x10000:
                long_reqs.append((idx, q, n))

    if long_reqs:
        long_raws = _fm.read_batch(p, [(q, n) for _, q, n in long_reqs])
        if long_raws is not None:
            for (idx, _q, n), sraw in zip(long_reqs, long_raws):
                if sraw and len(sraw) == n:
                    names[idx] = sraw.decode("utf-8", "replace")
        else:
            for idx, _q, _n in long_reqs:
                names[idx] = subunit_name(p, cands[idx])
    return names


def _scan_pool_range(p, needle, lo=None, hi=None):
    import sys as _sys
    found = {}
    # 优先走 Rust 多核并行虚表扫描 + 批量名称解包（不可用时无缝回落纯 Python 路径）
    if isinstance(needle, (bytes, bytearray)) and len(needle) == 8:
        try:
            import fastmem as _fm
            raw_regs = _fm.enum_regions(p, writable=True, heap_only=True, merge_adjacent=True)
            if raw_regs is not None:
                sliced = []
                for start, end, _t in raw_regs:
                    a = start if lo is None else max(lo, start)
                    b = end if hi is None else min(hi, end)
                    if a < b:
                        sliced.append({"start": a, "end": b})
                if not sliced:
                    return found
                vt = struct.unpack("<Q", needle)[0]
                hits = _fm.scan_vptrs_flat(p, sliced, [vt], max_hits=500000, chunk_mb=4)
                if hits is not None:
                    if not hits:
                        return found
                    if _sys.platform == "win32":
                        names = _batch_subunit_names_win(p, hits)
                    else:
                        names = [subunit_name(p, c) for c in hits]
                    for cand, nm in zip(hits, names):
                        if _is_valid_subunit_key(nm) and nm not in found:
                            found[nm] = cand
                    return found
        except Exception:
            found.clear()

    for m in p.regions(writable=True):
        a = m["start"] if lo is None else max(lo, m["start"])
        b = m["end"] if hi is None else min(hi, m["end"])
        if a >= b:
            continue
        cur = a
        while cur < b:
            n = min(8 << 20, b - cur)
            try:
                buf = p.try_read(cur, n)
            except Exception:
                buf = None
            if buf:
                i = 0
                while True:
                    i = buf.find(needle, i)
                    if i < 0:
                        break
                    cand = cur + i
                    nm = subunit_name(p, cand)
                    if _is_valid_subunit_key(nm) and nm not in found:
                        found[nm] = cand
                    i += 8
            cur += n
    return found


def live_pool(g, max_span=0x10000000):
    """Rebuild {subunit_key: CSubUnitDefinition*} for the live process.

    2026-11-23 (复测 10 B-6 实机根治):
      * 严格校验 key 必须是合法 ASCII 标识符 (`^[A-Za-z][A-Za-z0-9_]{1,62}$`)，清除乱码与 `---`；
      * 若 anchor +/- max_span 局部扫描漏掉了核心支援连（如 `logistics_company` /
        `signal_company`）或数量不足 140 个（Windows 64 位堆跨页常见现象，实测 112 vs 180+），
        自动升级为全可写堆扫描并按 PID 缓存，从此 `logistics_company` / `signal_company` /
        `marine` / `flame_tank` 等全部兵种 100% 在线可用！
    """
    _SS.sync(g)
    p = g.p
    cached = _LIVE_POOL["by_name"]
    if _LIVE_POOL["pid"] == p.pid and cached:
        k0 = next(iter(cached))
        if subunit_name(p, cached[k0]) == k0:
            return dict(cached)

    anchor = None
    for t in g.division_templates(None):
        try:
            tp = int(t["ptr"], 16)
        except Exception:
            continue
        for off in (0x68, 0x88):
            try:
                data, cap, size = struct.unpack("<Qii", p.read(tp + off, 16))
                if not (data > 0x10000 and 0 < size < 40):
                    continue
                sp = _u64(p.read(data, 8), 0)
                if sp > 0x10000 and _is_valid_subunit_key(subunit_name(p, sp)):
                    anchor = sp
                    break
            except Exception:
                continue
        if anchor:
            break
    if not anchor:
        return dict(cached)

    vt = _u64(p.read(anchor, 8), 0)
    needle = struct.pack("<Q", vt)
    lo = max(0x10000, anchor - max_span)
    hi = anchor + max_span
    found = _scan_pool_range(p, needle, lo=lo, hi=hi)
    if (len(found) < 140
            or "logistics_company" not in found
            or "signal_company" not in found):
        full_found = _scan_pool_range(p, needle, lo=None, hi=None)
        if len(full_found) > len(found):
            found = full_found
    if len(found) < len(cached):
        found.update({k: v for k, v in cached.items() if k not in found})
    _LIVE_POOL["pid"] = p.pid
    _LIVE_POOL["by_name"] = dict(found)
    return dict(found)


def template_arrays(g, tmpl_ptr):
    p = g.p
    out = {}
    for label, off in (("line", 0x68), ("support", 0x88)):
        hdr = p.read(tmpl_ptr + off, 16)
        data, cap, size = struct.unpack("<Qii", hdr)
        items = []
        for k in range(min(size, 40)):
            e = _u64(p.read(data + 8 * k, 8), 0)
            items.append({"ptr": e, "name": subunit_name(p, e)})
        out[label] = {"data": data, "cap": cap, "size": size, "items": items}
    return out


def build_array(g, keep, add):
    """Allocate a fresh CPdxArray payload: keep=[ptr...] then add=[(ptr,name)...]."""
    p = g.p
    total = len(keep) + len(add)
    buf = g.malloc(8 * total + 16)
    for i, ptr in enumerate(keep):
        p.write(buf + 8 * i, struct.pack("<Q", ptr))
    for j, (ptr, _n) in enumerate(add):
        p.write(buf + 8 * (len(keep) + j), struct.pack("<Q", ptr))
    return buf, total


def _looks_like_template(p, addr):
    """Guard: CDivisionTemplate starts with a vtable then a std::string name at +0x08."""
    try:
        vt = _u64(p.read(addr, 8), 0)
        rel = vt - p.base if vt > p.base else vt
        if not (0x4000000 < rel < 0x6000000):
            return False, "vtable %#x out of range" % vt
        n = _u64(p.read(addr + 0x10, 8), 0)
        if not (0 < n <= 64):
            return False, "name length %d" % n
        q = _u64(p.read(addr + 0x08, 8), 0)
        txt = p.read(q, n) if q else b""
        if not txt or not all(32 <= c < 127 for c in txt):
            return False, "name bytes unreadable"
        return True, txt.decode()
    except Exception as e:
        return False, str(e)




def _pe_template_arrays(g, t):
    """Live CReferencedDivisionTemplate arrays, including the empty grid slots.

    The 5-column division-designer grid stores TNullObject pointers in the
    unused columns; those MUST be preserved or the layout (and the validator)
    sees a different template. Relative to t: line {data,cap,size} at +0x68,
    support at +0x88 (the array object itself is 8 bytes earlier).
    """
    p = g.p
    out = {}
    for label, off in (("line", PE_LIVE_LINE), ("support", PE_LIVE_SUPP)):
        data, cap, size = struct.unpack("<Qii", p.read(t + off, 16))
        if not (data > 0x10000 and 0 <= size <= 60):
            raise ValueError("template %#x has a bad %s array (%#x/%d/%d)"
                             % (t, label, data, cap, size))
        ptrs = [p.u64(data + 8 * k) for k in range(size)]
        out[label] = {"data": data, "cap": cap, "size": size,
                      "ptrs": ptrs, "names": [subunit_name(p, e) for e in ptrs]}
    return out


def _pe_plan(g, arr, counts, label):
    """Delta-edit one array: return (new_slot_pointers, info) or (None, error).

    Semantics: each name in `counts` is adjusted to that many battalions;
    names NOT listed are left alone. Extra battalions go into the grid's
    empty TNullObject columns first (cheapest in army XP), removing one turns
    it back into an empty column. If all columns are full, slots are appended.
    """
    if not counts:
        return None, None                       # "not requested": keep as-is
    p = g.p
    try:
        pool = live_pool(g)
    except Exception:
        pool = {}
    by_name = {}
    for e, nm in zip(arr["ptrs"], arr["names"]):
        if nm:
            by_name.setdefault(nm, e)
    null_ptr = next((e for e, nm in zip(arr["ptrs"], arr["names"]) if not nm), 0)

    want = {}
    for nm, cnt in counts.items():
        # "artillery" is the SUPPORT company, "artillery_brigade" the line
        # battalion. Callers mix them up constantly -- normalise, don't fail.
        n2 = nm
        if label == "line" and n2 == "artillery":
            n2 = "artillery_brigade"
        elif label == "support" and n2 == "artillery_brigade":
            n2 = "artillery"
        try:
            c = int(cnt)
        except (TypeError, ValueError):
            return None, "count for %r is not a number" % nm
        if c < 0 or c > 60:
            return None, "count for %r out of range" % nm
        want[n2] = want.get(n2, 0) + c

    slots = list(arr["ptrs"])
    names = list(arr["names"])
    for nm, w in want.items():
        ptr = by_name.get(nm) or pool.get(nm)
        if not ptr:
            return None, "no subunit named %r (not in template, not in live pool)" % nm
        cur = [i for i, n in enumerate(names) if n == nm]
        if w < len(cur):
            if not null_ptr and w < len(cur):
                return None, "cannot remove %r: template has no empty column" % nm
            for i in cur[w:]:
                slots[i] = null_ptr
                names[i] = ""
        elif w > len(cur):
            need = w - len(cur)
            free = [i for i, n in enumerate(names) if not n]
            for i in free[:need]:
                slots[i] = ptr
                names[i] = nm
            if need > len(free):
                slots.extend([ptr] * (need - len(free)))
                names.extend([nm] * (need - len(free)))
    info = {"before": arr["names"], "after": names, "size": len(slots),
            "before_size": arr["size"], "counts": want}
    return slots, info


def _pe_cost(g, old_data, new_data, tag):
    """The engine's own army-XP cost for this edit (0x172CB90). Units: 1 XP = 1e6."""
    import cmdwin
    p = g.p
    outbuf = cmdwin.alloc(g, 0x20)
    tagp = cmdwin.alloc(g, 0x10)
    p.write(outbuf, b"\0" * 0x20)
    p.write(tagp, struct.pack("<i", int(tag)))
    g.call(p.base + 0x172CB90, (outbuf, old_data, new_data, tagp),
           absolute=True, force=True)
    return p.u64(outbuf)


def _pe_army_xp(g, tag):
    """Country army XP through the game's own dynamic-variable accessor."""
    import cmdwin
    p = g.p
    xpbuf = cmdwin.alloc(g, 0x20)
    tagp = cmdwin.alloc(g, 0x10)
    p.write(xpbuf, b"\0" * 0x20)
    p.write(tagp, struct.pack("<i", int(tag)))
    country = g.call(p.base + 0xC07DA0, (tagp,), absolute=True, force=True)
    g.call(p.base + 0x2572DB0, (xpbuf, country + 0x10),
           absolute=True, force=True)
    return p.u64(xpbuf)


def _pe_build(g, t, line_slots, supp_slots, tag):
    """Assemble CUpdateDivisionTemplateCommand with NO foreign ctor call.

    The command object is never destroyed (fake vtable slot 0 is a no-op), so
    shallow-copying the live CDivisionTemplateData is safe: Execute reassigns
    every array/string on the destination rather than freeing the source.
    """
    import cmdwin
    from commands import BASE_INIT
    p = g.p
    blob = bytearray(UPDATE_TEMPLATE_SIZE)
    struct.pack_into("<Q", blob, 0, p.base + VT_UPDATE_TEMPLATE)
    for off, fmt, val in BASE_INIT:
        if off + struct.calcsize(fmt) <= UPDATE_TEMPLATE_SIZE:
            struct.pack_into(fmt, blob, off, val)
    blob[PE_DATA_OFF:PE_DATA_OFF + PE_DATA_SPAN] = p.read(t + 0x18, PE_DATA_SPAN)
    for obj_off, ptrs in ((PE_LINE_OBJ, line_slots), (PE_SUPP_OBJ, supp_slots)):
        if ptrs is None:
            continue
        buf = cmdwin.alloc(g, 8 * len(ptrs) + 16)
        p.write(buf, b"".join(struct.pack("<Q", int(e)) for e in ptrs))
        struct.pack_into("<Qii", blob, obj_off + 8, buf, len(ptrs), len(ptrs))
    struct.pack_into("<Q", blob, 0x270, g._template_handle(t)[3])
    struct.pack_into("<i", blob, 0x278, int(tag))
    struct.pack_into("<Q", blob, 0x280, 0)
    cmd = cmdwin.alloc(g, UPDATE_TEMPLATE_SIZE)
    p.write(cmd, bytes(blob))
    cmdwin.attach_fake_vtable(g, cmd, VT_UPDATE_TEMPLATE)   # last: overwrites +0
    return cmd


def _pe_edit_template(g, template_ptr, line_counts, dry_run, support_counts, tag=None):
    """Windows 1.19.5.0: the proven player-command path (see constants above)."""
    import cmdwin
    p = g.p
    t = int(template_ptr)
    if p.u64(t) - p.base != 0x29CC350:
        return {"ok": False, "error":
                "pointer %#x is not a CReferencedDivisionTemplate (vptr %#x)"
                % (t, p.u64(t))}
    tag_id = int(g._c(tag).tag_id) if tag else int(g.player().tag_id)
    arrs = _pe_template_arrays(g, t)
    line_slots, line_info = _pe_plan(g, arrs["line"], line_counts, "line")
    # 2026-11-23 (复测 10 B-6 根治): _pe_plan 失败时返回 (None, "error msg")！
    # 旧代码错写成 `if line_info is None` / `if supp_info is None`，导致传入不存在的
    # 支援连时错误字符串被当成正常 info 放行、跳过修改且最后仍报 ok: True + posted: 1！
    if line_counts and (line_slots is None or not isinstance(line_info, dict)):
        return {"ok": False, "applied": False,
                "error": str(line_info or line_slots or "invalid line plan"),
                "stage": "line"}
    supp_slots, supp_info = _pe_plan(g, arrs["support"], support_counts, "support")
    if support_counts and (supp_slots is None or not isinstance(supp_info, dict)):
        return {"ok": False, "applied": False,
                "error": str(supp_info or supp_slots or "invalid support plan"),
                "stage": "support"}
    if line_slots is None and supp_slots is None:
        return {"ok": False, "applied": False, "error": "nothing to edit (line/support empty)"}

    cmd = _pe_build(g, t, line_slots, supp_slots, tag_id)
    can = bool(g.call(p.base + CANEXEC_UPDATE_TEMPLATE, (cmd, 0),
                      absolute=True, force=True) & 0xFF)
    cost = _pe_cost(g, t + 0x18, cmd + PE_DATA_OFF, tag_id)
    xp = _pe_army_xp(g, tag_id)
    res = {
        "ok": can, "applied": False, "mode": "pe-player-command", "cmd": hex(cmd),
        "template": hex(t), "handle": hex(p.u64(cmd + 0x270)), "tag": tag_id,
        "can_execute": can, "xp_cost": cost, "army_xp": xp,
        "xp_note": "engine units: 1 army XP = 100,000 (verified: +1 line battalion = 500,000 = 5 XP); the engine deducts its own cost",
        "army_xp_display": round(xp / 100000.0, 2),
        "xp_cost_display": round(cost / 100000.0, 2),
        "dry_run": bool(dry_run),
        "before": {k: v["names"] for k, v in arrs.items()},
        "plan": {"line": line_info, "support": supp_info},
    }
    if not can:
        res["error"] = ("engine CanExecute refused the edit: cost=%d army_xp=%d "
                        "(not enough army experience, or an illegal layout)"
                        % (cost, xp))
        return res
    if dry_run:
        res["note"] = "stopped before posting; command object left inert"
        res["warning"] = ("PREVIEW ONLY (dry_run=true): NO edit was sent to the game; "
                          "pass dry_run=false to actually apply this change")
        return res

    posted = cmdwin.post_drained(g, cmd, seconds=2.5)
    res["posted"] = posted
    after = _pe_template_arrays(g, t)
    res["after"] = {k: v["names"] for k, v in after.items()}
    try:
        res["xp_after"] = _pe_army_xp(g, tag_id)
    except Exception:
        pass
    # 2026-11-18: compare COMPACTED slot lists (without compacting, a straight
    # list comparison reported ok:false on a perfectly good edit).
    # 2026-11-23 (B-6): 同时核验 line 与 support 两份数组，绝不只检查 line 漏掉 support！
    def _compact(names):
        return [n for n in names if n]

    want_line = _compact(line_info["after"] if isinstance(line_info, dict) else arrs["line"]["names"])
    want_supp = _compact(supp_info["after"] if isinstance(supp_info, dict) else arrs["support"]["names"])
    line_match = (_compact(after["line"]["names"]) == want_line)
    supp_match = (_compact(after["support"]["names"]) == want_supp)
    applied_ok = bool(posted.get("posted")) and line_match and supp_match
    res["ok"] = applied_ok
    res["applied"] = applied_ok
    if not applied_ok:
        res["error"] = (
            "template edit post readback mismatch (posted=%s, line_match=%s, support_match=%s)"
            % (bool(posted.get("posted")), line_match, supp_match)
        )
    return res


def mem_edit(g, template_ptr, line_counts, support_counts=None, pool_path=None):
    """ROUTE A: rewrite the live CDivisionTemplate arrays in place.

    NOTE (2026-11-09): bypasses the 50-army-experience cost the editor charges;
    the run that uses it records the cost as spent.

    line_counts / support_counts: {"infantry": 9, "artillery": 3, ...}
    Subunits already in this template are reused; anything else comes from the
    global pointer pool (subunit_pointers.json, harvested from every country).
    """
    import json as _json
    p = g.p
    try:
        _d, _c, _s = struct.unpack("<Qii", p.read(template_ptr + 0x68, 16))
        if not (_d and 1 <= _c <= 60 and 1 <= _s <= 40):
            return {"ok": False, "error": "line array header looks wrong: data=%#x cap=%d size=%d"
                    % (_d, _c, _s)}
    except Exception as e:
        return {"ok": False, "error": "cannot read template header: %s" % e}

    before = template_arrays(g, template_ptr)
    by_name = {}
    for it in before["line"]["items"] + before["support"]["items"]:
        by_name.setdefault(it["name"], []).append(it["ptr"])

    pool = {}
    _here = os.path.dirname(os.path.abspath(__file__))
    _root = os.path.dirname(_here)
    for cand in (pool_path, os.path.join(_root, "subunit_pointers.json")):
        if cand and os.path.exists(cand):
            try:
                pool = {k: int(v, 16) for k, v in _json.load(open(cand)).items()}
                break
            except Exception:
                pool = {}

    meta = {}
    for cand in (os.path.join(_root, "subunit_catalog.json"),):
        if os.path.exists(cand):
            try:
                meta = {r["key"]: r for r in _json.load(open(cand, encoding="utf-8"))}
                break
            except Exception:
                meta = {}

    live = {}
    try:
        live = live_pool(g)
        res_pool_src = "live"
    except Exception:
        res_pool_src = "json"
    if not live:
        res_pool_src = "json"

    def ptr_for(nm):
        if by_name.get(nm):
            return by_name[nm][0]
        if live.get(nm):
            return live[nm]
        return pool.get(nm)

    def write_arr(off, counts, want_support):
        want = []
        for nm, cnt in counts.items():
            # "artillery" is the SUPPORT company and "artillery_brigade" the line
            # battalion. Callers (and LLMs) mix them up constantly; map the obvious
            # one instead of failing with "not a line subunit".
            if not want_support and nm == "artillery":
                nm = "artillery_brigade"
            elif want_support and nm == "artillery_brigade":
                nm = "artillery"
            if meta and nm in meta and bool(meta[nm].get("support")) != want_support:
                kind = "support" if want_support else "line"
                return None, ("%r is not a %s subunit (catalog says support=%s)"
                              % (nm, kind, meta[nm].get("support")))
            ptr = ptr_for(nm)
            if not ptr:
                return None, "no subunit named %r (not in template, not in pool)" % nm
            for _ in range(int(cnt)):
                want.append(ptr)
        if not want:
            return None, "empty array"
        buf = g.malloc(8 * len(want) + 16)
        for i2, ptr in enumerate(want):
            p.write(buf + 8 * i2, struct.pack("<Q", ptr))
        p.write(template_ptr + off, struct.pack("<Qii", buf, len(want), len(want)))
        return len(want), None

    res = {"ok": True, "mode": "memory-write", "template": hex(template_ptr),
           "before": [i3["name"] for i3 in before["line"]["items"]],
           "before_support": [i3["name"] for i3 in before["support"]["items"]],
           "xp_note": "50 army XP treated as spent (not deducted by the engine)"}

    if line_counts:
        n, err = write_arr(0x68, line_counts, False)
        if err:
            return {"ok": False, "error": err, "stage": "line"}
        res["line_size"] = n

    if support_counts:
        n, err = write_arr(0x88, support_counts, True)
        if err:
            return {"ok": False, "error": err, "stage": "support"}
        res["support_size"] = n

    after = template_arrays(g, template_ptr)
    res["after"] = [i3["name"] for i3 in after["line"]["items"]]
    res["support"] = [i3["name"] for i3 in after["support"]["items"]]
    return res


def edit_template(g, template_ptr, line_counts=None, dry_run=True, support_counts=None, tag=None):
    """line_counts: {"infantry": 10, "artillery_brigade": 3, ...}.

    Windows 1.19.5.0 dispatches to the PE player-command path (verified live
    2026-11-16). Linux keeps the historical command-factory / in-memory path.
    """
    import sys
    if sys.platform == "win32":
        try:
            return _pe_edit_template(g, template_ptr, line_counts, dry_run,
                                     support_counts, tag=tag)
        except Exception as e:
            return {"ok": False, "error": "PE template edit failed: %s" % e,
                    "mode": "pe-player-command"}
    import api
    p = g.p
    if (line_counts or support_counts) and not dry_run:
        return mem_edit(g, template_ptr, line_counts, support_counts)
    api.allow_call(FACTORY)
    api.allow_call(DEEPCOPY)
    api.allow_call(RUN_CMD)

    src_ptr = template_ptr
    okp, why = _looks_like_template(p, src_ptr)
    if not okp:
        # CReferencedDivisionTemplate: the real CDivisionTemplate is embedded at +0x18
        alt = template_ptr + 0x18
        ok2, why2 = _looks_like_template(p, alt)
        if ok2:
            src_ptr, okp, why = alt, True, why2
        else:
            return {"ok": False, "error": "template pointer %#x rejected: %s / +0x18: %s"
                    % (template_ptr, why, why2)}
    cmd = g.call(FACTORY, [])
    if not cmd:
        return {"ok": False, "error": "factory returned NULL"}

    # deep-copy the live template into the command body
    g.call(DEEPCOPY, [cmd + 0x28, src_ptr])
    # The command object's `this` is alloc+0x10 (the factory stores vt+0x10), so the
    # embedded CDivisionTemplate sits at this+0x18.  Probe a few offsets and keep the
    # first one whose line array header looks sane.
    body = None
    # V2-01 (reviewed 2026-11-10): DEEPCOPY puts the CDivisionTemplate at cmd+0x28;
    # its line array sits at +0x50, i.e. cmd+0x78 == (cmd+0x10)+0x68. The old list
    # probed 0x80/0x90/0xA0/0x70 and never the right one -> dry_run always failed.
    for _off in (0x10, 0x18, 0x28, 0x38, 0x08):
        _cand = cmd + _off
        try:
            _d, _c, _s = struct.unpack("<Qii", p.read(_cand + 0x68, 16))
            if 0 < _s <= 40 and 1 <= _c <= 60 and _d:
                body = _cand
                break
        except Exception:
            pass
    if body is None:
        blk = b""
        try:
            blk = p.read(cmd, 0x288)
        except Exception:
            pass
        hits = []
        for needle in (b"Infanterie", b"Panzer", b"Kavallerie"):
            i = blk.find(needle)
            if i >= 0:
                hits.append({"needle": needle.decode(), "off": hex(i)})
        return {"ok": False, "error": "no valid embedded template header found",
                "name_scan": hits, "cmd": hex(cmd),
                "head": blk[:64].hex()}
    # V2-01: body is probed as a CReferencedDivisionTemplate-shaped base, so the
    # template name std::string sits at +0x20 (== inner+0x08), not +0x08.
    try:
        name = p.stdstring(body + 0x20) or "(copied)"
    except Exception:
        name = "(copied)"
    arr = template_arrays(g, body)
    if arr["line"]["size"] <= 0:
        return {"ok": False, "error": "copy produced an empty line array", "cmd": hex(cmd)}

    result = {
        "ok": True, "cmd": hex(cmd), "template_copy": hex(body), "src": hex(src_ptr),
        "name_probe": name,
        "before": {k: [i["name"] for i in v["items"]] for k, v in arr.items()},
        "dry_run": dry_run,
    }
    if dry_run:
        result["note"] = "stopped before patching / Run"
        return result

    if line_counts:
        by_name = {}
        for it in arr["line"]["items"]:
            by_name.setdefault(it["name"], []).append(it["ptr"])
        want = []
        for nm, cnt in line_counts.items():
            pool = by_name.get(nm, [])
            if not pool:
                return {"ok": False, "error": "no subunit named %r in the template" % nm,
                        "have": sorted(by_name)}
            for i in range(int(cnt)):
                want.append(pool[i % len(pool)])
        buf, total = build_array(g, want, [])
        p.write(body + 0x68, struct.pack("<Qii", buf, total, total))

    p.write(cmd + 0x270, struct.pack("<i", 1))   # CCountryTag id for GER
    p.write(cmd + 0x278, struct.pack("<i", 0))
    p.write(cmd + 0x280, struct.pack("<Q", 0))

    s = g.malloc(0x28)
    p.write(s, struct.pack("<QQB", s + 0x10, 0, 0))
    rv = g.call(RUN_CMD, [cmd, s])
    err = ""
    try:
        q, n = struct.unpack("<QQ", p.read(s, 16))
        err = p.read(q, min(n, 200)).decode("utf-8", "replace") if q else ""
    except Exception:
        pass
    result["run"] = bool(rv & 0xFF)
    result["error"] = err
    return result
