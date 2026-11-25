"""
Extracts the field layout of all 370 CCommand classes automatically.

Every command's Describe method at vtable+0xB8 walks all of its fields for logging
and dispatches them to different helper functions by type:
    0x34771C0(buf, locKey, int)        -> integer or bool field
    0x3475520(buf, locKey, CString*, 0) -> object pointer (+off to read its name)
    0x34755E0(buf, locKey, void*)      -> embedded object (e.g. CBuildingReference)
Parsing that gives us each field's offset and type.
"""
from __future__ import annotations
import os, pickle, re, struct, subprocess

import config
BIN = config.binary_path(required=False)
CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cmdfields.pkl")

H_INT = 0x34771C0     # integer or bool
H_STR = 0x3475520     # CString* (object name)
H_OBJ = 0x34755E0     # embedded object
H_TAG = 0xD78240      # CCountryTag -> ad


def _dis(va, end):
    out = subprocess.run(["objdump", "-d", "--start-address=%#x" % va,
                          "--stop-address=%#x" % end, "-M", "intel", BIN],
                         capture_output=True, text=True).stdout
    return out.splitlines()[7:]


def describe_fields(describe, end):
    """[(offset, kind, name_off)] kind: int|bool|ptr|obj|tag"""
    lines = _dis(describe, end)
    OBJ = ("rbx", "rdi", "r14", "r15", "r12", "r13")
    fields = []
    pend = None          # (offset, size_hint)
    holder = None        # the register holding the pointer
    name_off = 0
    for ln in lines:
        t = ln.split("\t")[-1].strip()
        m = re.match(r"(?:mov|movzx|movsx|lea)\s+(\w+),\s*(?:(QWORD|DWORD|BYTE|WORD) PTR )?"
                     r"\[(\w+)\+(0x[0-9a-f]+)\]", t)
        if m and m.group(3) in OBJ:
            pend = (int(m.group(4), 16), (m.group(2) or "LEA"))
            holder = m.group(1)
            name_off = 0
            continue
        m = re.match(r"add\s+(\w+),(0x[0-9a-f]+)", t)
        if m:
            reg, imm = m.group(1), int(m.group(2), 16)
            if reg in OBJ and reg != holder:
                pend = (imm, "LEA"); holder = reg; name_off = 0
            elif reg == holder:
                name_off = imm
            continue
        m = re.match(r"(?:call|jmp)\s+([0-9a-f]+)", t)
        if m and pend:
            tgt = int(m.group(1), 16)
            off, sz = pend
            if tgt == H_INT:
                fields.append((off, "bool" if sz == "BYTE" else "int", 0))
            elif tgt == H_STR:
                fields.append((off, "ptr", name_off))
            elif tgt == H_OBJ:
                fields.append((off, "obj", 0))
            elif tgt == H_TAG:
                fields.append((off, "tag", 0))
            pend = None; holder = None; name_off = 0
    seen, out = set(), []
    for o, k, n in fields:
        if o not in seen and 0x20 <= o < 0x400:
            seen.add(o); out.append((o, k, n))
    return sorted(out)


def execute_offsets(execute, end):
    """The [this+off] fields read inside Execute, as [(offset, kind)].
    A fallback for when Describe gives nothing away."""
    lines = _dis(execute, end, n=400)
    out = {}
    for ln in lines:
        t = ln.split("\t")[-1].strip()
        m = re.match(r"(?:mov|movzx|movsx|lea|cmp)\s+\w+,\s*(QWORD|DWORD|BYTE|WORD)? ?PTR "
                     r"\[(rbx|rdi|r14|r15|r12|r13)\+(0x[0-9a-f]+)\]", t)
        if not m:
            m = re.match(r"cmp\s+(QWORD|DWORD|BYTE|WORD) PTR \[(rbx|rdi|r14|r15|r12|r13)\+"
                         r"(0x[0-9a-f]+)\],", t)
        if m:
            sz = m.group(1) or "QWORD"
            off = int(m.group(3), 16)
            if 0x20 <= off < 0x400:
                kind = {"BYTE": "bool?", "WORD": "int?", "DWORD": "int?"}.get(sz, "ptr?")
                out.setdefault(off, kind)
    return sorted(out.items())


def build(force=False):
    import safe_cache
    if safe_cache.exists(CACHE) and not force:
        return safe_cache.load(CACHE)
    raise NotImplementedError(
        "Linux ELF/objdump command scanner removed; use prebuilt cmdfields.json or PE recalc."
    )


if __name__ == "__main__":
    import sys
    d = build(force=True)
    print("commands:", len(d))
    pat = sys.argv[1] if len(sys.argv) > 1 else ""
    n = 0
    for k in sorted(d):
        if pat and pat.lower() not in k.lower():
            continue
        v = d[k]
        print("%-46s size=%-5s type=%-8s %s" % (
            k, v.get("size"), hex(v["type_id"]) if v["type_id"] else "-",
            [(hex(o), t, hex(n)) for o, t, n in v["fields"]]))
        n += 1
        if n > 60: break
