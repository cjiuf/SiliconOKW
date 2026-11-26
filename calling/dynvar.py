"""Dynamic-variable registry helpers (PE build 1.19.5.0).

Facts established by disassembly (see research/windows/pedis.py / trigger table notes):

  registry map global   RVA 0x33DF150   (std::unordered_map<u32, std::function<...>>)
  documentation list    RVA 0x33DF1D0   (std::list of {name, description, is_trigger})
  insert helper         RVA 0x1D5170
  registration site     RVA 0x71A080 (family; siblings at +0x1F0, ...)

  map node layout (observed through the registration path):
      +0x38  std::function   (64 bytes, MSVC)
      +0x40  std::string     name (32 bytes)
      +0x60  bool            is_trigger
"""
import struct

import capstone

_CACHE = {}


def _md():
    md = _CACHE.get('md')
    if md is None:
        md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_64)
        md.detail = False
        _CACHE['md'] = md
    return md


def dis(p, rva, n):
    """Disassemble n bytes at RVA; returns list of 'RVA  bytes  mnemonic operands'."""
    code = p.try_read(p.base + rva, n)
    if not code:
        return ['<unreadable %08X>' % rva]
    out = []
    for i in _md().disasm(code, p.base + rva):
        out.append('%08X  %-20s %s %s' % (i.address - p.base, i.bytes.hex(), i.mnemonic, i.op_str))
    return out


def fnv1a(s):
    h = 0x811C9DC5
    for b in s.encode('utf-8'):
        h = ((h ^ b) * 0x01000193) & 0xFFFFFFFF
    return h


def scan_call(p, target_rva, lo=0x1000, hi=0x2000000, chunk=0x200000):
    """Find every `call rel32` / `jmp rel32` whose target is exactly target_rva."""
    hits = []
    for op in (b'\xe8', b'\xe9'):
        for start in range(lo, hi, chunk):
            data = p.try_read(p.base + start, chunk)
            if not data:
                continue
            i = 0
            while True:
                i = data.find(op, i)
                if i < 0 or i + 5 > len(data):
                    break
                rel = struct.unpack_from('<i', data, i + 1)[0]
                rva = start + i
                if (rva + 5 + rel) & 0xFFFFFFFFFFFF == target_rva:
                    hits.append(rva)
                i += 1
    return sorted(set(hits))


def scan_bytes(p, pattern, lo=0x1000, hi=0x2000000, chunk=0x200000):
    """Find every occurrence of a raw byte pattern in [lo,hi). Returns RVAs."""
    hits = []
    for start in range(lo, hi, chunk):
        data = p.try_read(p.base + start, chunk)
        if not data:
            continue
        i = 0
        while True:
            i = data.find(pattern, i)
            if i < 0:
                break
            hits.append(start + i)
            i += 1
    return hits


def scan_lea(p, target_rva, lo=0x1000, hi=0x2000000, chunk=0x200000):
    return scan_lea_multi(p, target_rva, (b'\x48', b'\x4c', b'\x49', b'\x4d'), lo, hi, chunk)


def scan_lea_multi(p, target_rva, rexes, lo=0x1000, hi=0x2000000, chunk=0x200000):
    """Find every `lea reg,[rip+disp]` whose target is exactly target_rva.

    Returns [rva_of_instruction, ...]. Raw byte scan, no disassembly, so it is
    fast; every hit should still be confirmed with dis().
    """
    hits = []
    modrm = (0x05, 0x0D, 0x15, 0x1D, 0x25, 0x2D, 0x35, 0x3D)
    if isinstance(rexes, bytes) and len(rexes) == 1:
        rexes = (rexes,)
    for start in range(lo, hi, chunk):
        data = p.try_read(p.base + start, chunk)
        if not data:
            continue
        for rex in rexes:
            pat = rex + b'\x8d'
            i = 0
            while True:
                i = data.find(pat, i)
                if i < 0 or i + 7 > len(data):
                    break
                if data[i + 2] in modrm:
                    disp = struct.unpack_from('<i', data, i + 3)[0]
                    rva = start + i
                    if (rva + 7 + disp) & 0xFFFFFFFFFFFF == target_rva:
                        hits.append(rva)
                i += 1
    return sorted(set(hits))


#: Variables that must NEVER be swept through `get_var`.
#:
#: 2026-11-20: sweeping all 265 names in one go killed hoi4.exe. error.log's last
#: two lines were "Peace conference trigger is called when there is no active
#: peace conference" -- `pc_total_score` / `pc_current_score` evaluate a
#: peace-conference trigger that asserts outside a conference, and HOI4 exits on
#: a failed assert (no crash dump was written).  Anything that reaches into a
#: subsystem whose context may be absent belongs here, not in a blind sweep.
PROBE_DENY = ("pc_total_score", "pc_current_score")


def probe_values(g, names, keep_raw=False, deny=PROBE_DENY):
    """Ask the engine for every name via the `get_var` console command.

    `get_var` is the only generic evaluator this build exposes. It answers
    "<name> = <number>" for plain values and "<name> = <scope name> (<number>)"
    when the variable resolves to a scope object, so both shapes are parsed to
    the trailing number. Returns (values, raw) with None where unparsable.

    Names in `deny` are skipped with the reason recorded: see PROBE_DENY.
    """
    import re
    vals, raw = {}, {}
    for n in names:
        if n in deny:
            vals[n] = None
            raw[n] = "SKIPPED: in PROBE_DENY (asserts outside its subsystem)"
            continue
        try:
            msg = (g.console("get_var " + n).to_dict() or {}).get("message") or ""
        except Exception as e:                                  # noqa: BLE001
            vals[n] = None
            raw[n] = repr(e)
            continue
        raw[n] = msg.strip()
        m = (re.search(r"\((-?[\d.]+)\)\s*$", msg)
             or re.search(r"=\s*(-?[\d.]+)\s*$", msg))
        vals[n] = float(m.group(1)) if m else None
    return (vals, raw) if keep_raw else vals


def scan_scalar(p, base, size, int_value, float_value=None, cap=24):
    """Find a scalar in a memory window in the encodings HOI4 actually uses.

    Returns offsets *relative to base*, keyed q (int64), d (int32), f (float32)
    and D (float64). Used to locate a value the console reported inside a
    structure we already have a pointer to.
    """
    data = p.try_read(base, size) or b""
    if float_value is None:
        float_value = float(int_value)
    out = {"q": [], "d": [], "f": [], "D": []}
    n8, n4 = len(data) // 8, len(data) // 4
    for i, (v,) in enumerate(struct.iter_unpack("<q", data[:n8 * 8])):
        if v == int_value and len(out["q"]) < cap:
            out["q"].append(i * 8)
    for i, (v,) in enumerate(struct.iter_unpack("<i", data[:n4 * 4])):
        if v == int_value and len(out["d"]) < cap:
            out["d"].append(i * 4)
    for i, (v,) in enumerate(struct.iter_unpack("<f", data[:n4 * 4])):
        if abs(v - float_value) < 0.01 and len(out["f"]) < cap:
            out["f"].append(i * 4)
    for i, (v,) in enumerate(struct.iter_unpack("<d", data[:n8 * 8])):
        if abs(v - float_value) < 0.01 and len(out["D"]) < cap:
            out["D"].append(i * 8)
    return out


def scan_tag_arrays(p, obj, size_off=0x18, data_off=0x0, span=0x320, maxn=6,
                    max_tag=442, limit=24):
    """Find CPdxArray-like members of `obj` whose elements all look like country tags.

    A state's `cores` is a small container of country tags, so this narrows the
    search to the handful of offsets that hold one.  Returns {offset: [tags]}.
    """
    out = {}
    for off in range(0, span, 8):
        if len(out) >= limit:
            break
        try:
            n = p.i32(obj + off + size_off)
            d = p.u64(obj + off + data_off)
        except Exception:
            continue
        if not (0 < n <= maxn) or not (0x10000 < d < 0x7F0000000000):
            continue
        raw = p.try_read(d, 4 * n)
        if not raw or len(raw) < 4 * n:
            continue
        vals = list(struct.unpack("<%di" % n, raw))
        if all(1 <= v <= max_tag for v in vals):
            out[off] = vals
    return out


def read_qwords(p, rva, n=8):
    raw = p.try_read(p.base + rva, n * 8)
    if not raw:
        return None
    return list(struct.unpack('<%dQ' % n, raw))


def walk_registry(p, rva=0x33DF1D0, limit=400):
    """Walk the dynamic-variable registry list.

    Layout observed in memory (map at 0x33DF1D0):
        +0x00  float max_load_factor (1.0)
        +0x08  list sentinel pointer
        +0x10  element count
        +0x18  bucket vector begin
        +0x30  bucket mask (0x1FF), +0x38 bucket count (0x200)

    Each list node: +0x00 next, +0x08 prev, +0x10 key std::string,
    +0x30 function storage, +0x40 secondary std::string, +0x60 bool.
    """
    raw = p.try_read(p.base + rva, 0x40)
    if not raw:
        return {'ok': False, 'why': 'unreadable registry'}
    sent = struct.unpack_from('<Q', raw, 0x08)[0]
    count = struct.unpack_from('<Q', raw, 0x10)[0]
    out = []
    node = sent
    seen = set()
    while len(out) < limit:
        nxt = p.try_read(node, 8)
        if not nxt:
            break
        node = struct.unpack('<Q', nxt)[0]
        if node == sent or node in seen:
            break
        seen.add(node)
        k = node_strings(p, node + 0x10)
        s = node_strings(p, node + 0x40)
        out.append({'node': '%X' % node,
                    'key': k[3] if k else None,
                    'aux': s[3] if s else None})
    return {'ok': True, 'declared': count, 'walked': len(out), 'items': out}


def node_strings(p, addr, maxlen=64):
    """Best-effort read of an MSVC std::string at addr -> (ptr, size, cap, text)."""
    raw = p.try_read(addr, 32)
    if not raw:
        return None
    cap = struct.unpack_from('<Q', raw, 0x18)[0]
    size = struct.unpack_from('<Q', raw, 0x10)[0]
    if size > maxlen or cap < size:
        return None
    if cap <= 0xF:
        ptr = addr
    else:
        ptr = struct.unpack_from('<Q', raw, 0)[0]
    data = p.try_read(ptr, size)
    if data is None:
        return None
    try:
        return (ptr, size, cap, data.decode('utf-8', 'replace'))
    except Exception:
        return None
