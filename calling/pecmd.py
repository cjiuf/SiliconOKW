"""PE command catalogue: C<Name>Command -> {vtable, ctor, size, fields}.

The Linux build ships a hand-checked table (`commands.COMMANDS`) plus two
extracted pickles (`cmdsigs.pkl`, `cmdfields.pkl`).  Neither is valid here: the
pickles hold ELF offsets from 1.19.4.1 and calling any of them on hoi4.exe takes
the process down.

So the PE table is rebuilt from the executable itself, the same way §72.1 did it
by hand for `CDropCurrentNationalFocusCommand`:

    MSVC RTTI  .?AVC<Name>Command@@  ->  vtable RVA          (rtti_win.pkl)
    .text      lea rax, [rip+vt]     ->  the ctor            (first instruction)
    ctor       mov [rcx+off], src    ->  the field layout    (rcx = `this`)

Everything is an RVA relative to the image base, exactly like `offsets_win`.

`build()` caches to `pecmds.pkl` next to this file; `python pecmd.py Foo`
prints matching entries.

The recovered layout is what `cmdwin.build()` uses, so a command only has to be
named -- no per-command ctor port.
"""
from __future__ import annotations

import os
import pickle
import re
import struct
import sys

import config

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, "pecmds.pkl")

# ---------------------------------------------------------------- PE image
_IMG = None


class PE:
    """Minimal PE32+ reader: sections, RVA<->file offset, raw bytes."""

    def __init__(self, path):
        self.path = path
        with open(path, "rb") as fh:
            self.data = fh.read()
        d = self.data
        if d[:2] != b"MZ":
            raise RuntimeError("%s is not a PE (no MZ)" % path)
        e_lfanew = struct.unpack_from("<I", d, 0x3C)[0]
        if d[e_lfanew:e_lfanew + 4] != b"PE\0\0":
            raise RuntimeError("%s has no PE signature" % path)
        coff = e_lfanew + 4
        nsec = struct.unpack_from("<H", d, coff + 2)[0]
        opt_size = struct.unpack_from("<H", d, coff + 16)[0]
        opt = e_lfanew + 24
        magic = struct.unpack_from("<H", d, opt)[0]
        if magic != 0x20B:
            raise RuntimeError("not PE32+ (magic %#x)" % magic)
        self.image_base = struct.unpack_from("<Q", d, opt + 0x18)[0]
        self.size_of_image = struct.unpack_from("<I", d, opt + 0x38)[0]
        self.sections = {}
        o = opt + opt_size
        for _ in range(nsec):
            name = d[o:o + 8].rstrip(b"\0").decode("latin-1")
            vsize, va, rawsize, rawptr = struct.unpack_from("<IIII", d, o + 8)
            self.sections[name] = {
                "va": va, "vsize": vsize, "raw": rawptr, "rawsize": rawsize}
            o += 40

    def rva2off(self, rva):
        for s in self.sections.values():
            span = max(s["vsize"], s["rawsize"])
            if s["va"] <= rva < s["va"] + span:
                delta = rva - s["va"]
                if delta >= s["rawsize"]:
                    return None            # virtual tail, not backed by the file
                return s["raw"] + delta
        return None

    def read(self, rva, n):
        off = self.rva2off(rva)
        if off is None:
            return b""
        return self.data[off:off + n]

    def text(self):
        s = self.sections[".text"]
        return s["va"], max(s["vsize"], s["rawsize"])


def pe():
    global _IMG
    if _IMG is None:
        _IMG = PE(config.binary_path(required=True))
    return _IMG


# ---------------------------------------------------------------- scanning
_MODRM_RIP = frozenset((0x05, 0x0D, 0x15, 0x1D, 0x25, 0x2D, 0x35, 0x3D))


def scan_lea(wanted):
    """One pass over .text -> {vtable_rva: [site_rva, ...]} for the wanted set.

    MSVC loads a vtable with `48/4C/49 8D <modrm:rip> <disp32>`; the encoding has
    a fixed length, so a byte scan is both faster and simpler than disassembling
    41 MB.  Only targets in `wanted` are kept, which keeps the index tiny.
    """
    img = pe()
    base, size = img.text()
    start = img.rva2off(base)
    blob = img.data
    out = {}
    i = start
    end = min(start + size, len(blob)) - 6
    while i < end:
        j = blob.find(b"\x8d", i, end)
        if j < 0:
            break
        i = j + 1
        if j == 0 or blob[j - 1] not in (0x48, 0x4C, 0x49):
            continue
        if blob[j + 1] not in _MODRM_RIP:
            continue
        disp = struct.unpack_from("<i", blob, j + 2)[0]
        rva = base + (j - start)
        tgt = (rva + 6 + disp) & 0xFFFFFFFFFFFFFFFF
        if tgt in wanted:
            out.setdefault(tgt, []).append(rva - 1)
    return out


def scan_calls(wanted):
    """One pass over .text -> {target_rva: [call_rva, ...]} for `wanted` callees."""
    img = pe()
    base, size = img.text()
    start = img.rva2off(base)
    blob = img.data
    out = {}
    i = start
    end = min(start + size, len(blob)) - 5
    while i < end:
        j = blob.find(b"\xe8", i, end)
        if j < 0:
            break
        i = j + 1
        rel = struct.unpack_from("<i", blob, j + 1)[0]
        rva = base + (j - start)
        tgt = (rva + 5 + rel) & 0xFFFFFFFFFFFFFFFF
        if tgt in wanted:
            out.setdefault(tgt, []).append(rva)
    return out


# ------------------------------------------------------------- ctor layout
_SUB_NAMES = {
    "rax": ("eax", "ax", "al", "ah"),
    "rbx": ("ebx", "bx", "bl", "bh"),
    "rcx": ("ecx", "cx", "cl", "ch"),
    "rdx": ("edx", "dx", "dl", "dh"),
    "rsi": ("esi", "si", "sil"),
    "rdi": ("edi", "di", "dil"),
    "rbp": ("ebp", "bp", "bpl"),
    "rsp": ("esp", "sp", "spl"),
}
_SUBS = {}
for _f in ("rax", "rbx", "rcx", "rdx", "rsi", "rdi", "rbp", "rsp",
           "r8", "r9", "r10", "r11", "r12", "r13", "r14", "r15"):
    _SUBS[_f] = _f
    if _f.startswith("r") and _f[1:].isdigit():
        for _s in ("d", "w", "b"):
            _SUBS[_f + _s] = _f
    else:
        for _s in _SUB_NAMES[_f]:
            _SUBS[_s] = _f
_ARG = {"rdx": 1, "edx": 1, "dx": 1, "dl": 1,
        "r8": 2, "r8d": 2, "r8w": 2, "r8b": 2,
        "r9": 3, "r9d": 3, "r9w": 3, "r9b": 3}


def fn_start(rva, max_back=0x400):
    """Walk back to the start of the MSVC function that contains `rva`.

    MSVC separates functions with `int3` padding, but tightly packed ones are just
    `ret` followed by the next prologue, so a `ret` also ends the previous
    function.  Walking too far back is harmless -- the parse then fails to find the
    vtable store and the candidate is dropped.
    """
    img = pe()
    off = img.rva2off(rva)
    if off is None:
        return rva
    k = off
    while k > 0 and off - k < max_back:
        prev = img.data[k - 1]
        if prev == 0xCC:
            return rva - (off - k)
        if prev == 0xC3 and off - k > 3:
            # `ret` ends the previous function; make sure it is not an immediate
            # byte (pop r64 is 0x58..0x5F, so a lone 0xC3 right before us is a ret)
            return rva - (off - k)
        k -= 1
    return rva


def ctor_layout(ctor_rva, max_ins=160):
    """Parse one MSVC x64 ctor: fields stored into [this+off] and their argument."""
    from capstone import Cs, CS_ARCH_X86, CS_MODE_64, CS_OP_IMM, CS_OP_MEM, CS_OP_REG
    img = pe()
    md = getattr(ctor_layout, "_md", None)
    if md is None:
        md = Cs(CS_ARCH_X86, CS_MODE_64)
        md.detail = True
        ctor_layout._md = md
    code = img.read(ctor_rva, max_ins * 8)
    if not code:
        return None
    this = {"rcx"}
    derived = {}      # reg -> (arg, kind, size)
    fieldptr = {}     # reg -> offset from this
    out = []
    saw_vt = False
    n = 0
    for ins in md.disasm(code, img.image_base + ctor_rva):
        n += 1
        if n > max_ins:
            break
        ops = ins.operands
        if ins.mnemonic == "mov" and len(ops) == 2 \
                and ops[0].type == CS_OP_REG and ops[1].type == CS_OP_REG:
            dst = _SUBS.get(ins.reg_name(ops[0].reg))
            src = _SUBS.get(ins.reg_name(ops[1].reg))
            if src in this and dst:
                this.add(dst)
                continue
            if src and src in _ARG and dst:
                derived[dst] = (_ARG[src], "ptr", 8)
                continue
            if dst:
                this.discard(dst)
                derived.pop(dst, None)
                fieldptr.pop(dst, None)
            continue
        if ins.mnemonic == "lea" and len(ops) == 2 and ops[0].type == CS_OP_REG \
                and ops[1].type == CS_OP_MEM:
            m = ops[1].mem
            br = _SUBS.get(ins.reg_name(m.base)) if m.base else None
            dst_r = _SUBS.get(ins.reg_name(ops[0].reg))
            if br in this and m.index == 0 and dst_r:
                this.discard(dst_r)
                fieldptr[dst_r] = m.disp
                continue
        if ins.mnemonic in ("mov", "movzx", "movsx") and len(ops) == 2 \
                and ops[0].type == CS_OP_REG and ops[1].type == CS_OP_MEM:
            m = ops[1].mem
            br = _SUBS.get(ins.reg_name(m.base)) if m.base else None
            if br in _ARG and m.index == 0:
                ai = _ARG[br]
                kind = "handle" if m.disp == 8 else ("deref" if m.disp == 0
                                                     else "field%#x" % m.disp)
                derived[_SUBS.get(ins.reg_name(ops[0].reg))] = (ai, kind, ops[0].size)
                continue
        if ins.mnemonic == "mov" and len(ops) == 2 and ops[0].type == CS_OP_MEM:
            m = ops[0].mem
            br = _SUBS.get(ins.reg_name(m.base)) if m.base else None
            src = ops[1]
            off = None
            if br in this and m.index == 0:
                off = m.disp
            elif br in fieldptr and m.index == 0:
                off = fieldptr[br] + m.disp
            if off is not None:
                sz = ops[0].size
                if off == 0 and src.type == CS_OP_REG \
                        and ins.reg_name(src.reg) == "rax":
                    saw_vt = True
                elif src.type == CS_OP_REG:
                    sr = _SUBS.get(ins.reg_name(src.reg))
                    if sr in _ARG:
                        out.append((off, sz, _ARG[sr],
                                    "ptr" if sz == 8 else ("bool" if sz == 1 else "int")))
                    elif sr in derived:
                        ai, kind, _sz = derived[sr]
                        out.append((off, sz, ai, kind))
                elif src.type == CS_OP_IMM:
                    out.append((off, sz, None, "imm:%#x" % (src.imm & 0xFFFFFFFF)))
                continue
        if ops and ops[0].type == CS_OP_REG and ins.mnemonic not in ("cmp", "test"):
            r = _SUBS.get(ins.reg_name(ops[0].reg))
            if r:
                derived.pop(r, None)
                if r not in this:
                    fieldptr.pop(r, None)
        if ins.mnemonic in ("ret", "jmp") and n > 3:
            break
    if not saw_vt:
        return None
    seen = {}
    for off, sz, ai, kind in out:
        if not (0x20 <= off < 0x800) or off in seen:
            continue
        seen[off] = {"offset": off, "size": sz, "arg": ai, "kind": kind}
    fields = sorted(seen.values(), key=lambda f: f["offset"])
    return {"fields": fields,
            "args": max([f["arg"] for f in fields if f["arg"] is not None], default=0)}


def object_size(ctor_rva, fields, calls):
    """`mov ecx, SIZE ; call operator new` in the factory that calls this ctor."""
    img = pe()
    base, _size = img.text()
    start = img.rva2off(base)
    blob = img.data
    for site in calls:
        j = img.rva2off(site)
        if j is None:
            continue
        lo = max(start, j - 0x70)
        seg = blob[lo:j]
        found = None
        for k in range(len(seg) - 5, -1, -1):
            if seg[k] != 0xB9:
                continue
            imm = struct.unpack_from("<I", seg, k + 1)[0]
            if 0x18 <= imm <= 0x4000 and b"\xe8" in seg[k:]:
                found = imm
                break
        if found:
            return found
    hi = max([f["offset"] + f["size"] for f in fields], default=0x28)
    return max(0x40, (hi + 0x20 + 0xF) & ~0xF)


# ------------------------------------------------------------------ build
def build(force=False, verbose=False):
    if not force:
        # 第二期数据驱动：优先直接加载 native/hoi4_recalc 产出的精确命令目录（含 .pdata +
        # iced-x86 全栈参数/子对象追踪，免去 pecmds.pkl 与 Capstone 慢扫）。
        try:
            import profile as _prof
            pcmds = _prof.commands()
            if pcmds:
                out = {k: dict(v) for k, v in pcmds.items()}
                if verbose:
                    for k in sorted(out):
                        e = out[k]
                        print("%-46s vt=%#010x ctor=%s size=%#06x args=%d fields=%s"
                              % (k, e["vt"],
                                 ("%#010x" % e["ctor"]) if e.get("ctor") else "None",
                                 e["size"], e["args"],
                                 [(hex(f["offset"]), f["size"], f["arg"], f["kind"])
                                  for f in e.get("fields", [])]))
                return out
        except Exception:
            pass
    if os.path.exists(CACHE) and not force:
        with open(CACHE, "rb") as fh:
            return pickle.load(fh)
    import rtti_win
    m = rtti_win.build()
    wanted = {}
    for cls, vts in m["vtables"].items():
        if re.fullmatch(r"C[A-Za-z0-9_]*Command", cls):
            for vt in vts:
                wanted[vt] = cls
    leas = scan_lea(set(wanted))
    out = {}
    for vt, cls in sorted(wanted.items(), key=lambda kv: kv[1]):
        # Several functions reference a command vtable: the ctor, the clone and
        # often a factory.  Score them instead of taking the first -- the factory
        # writes the vtable and nothing else, and the clone copies from another
        # object rather than from the arguments.
        best = None
        for s in sorted(leas.get(vt, []))[:6]:
            st = fn_start(s)
            lay = ctor_layout(st)
            if lay is None:
                continue
            score = (len(lay["fields"]), lay["args"], -st)
            if best is None or score > best[0]:
                best = (score, st, lay)
        if best is None:
            continue
        ctor, layout = best[1], best[2]
        ent = {"vt": vt, "ctor": ctor, "size": None, "args": layout["args"],
               "fields": layout["fields"], "class": cls}
        if cls not in out or len(layout["fields"]) > len(out[cls]["fields"]):
            out[cls] = ent
    calls = scan_calls(set(e["ctor"] for e in out.values()))
    for e in out.values():
        e["size"] = object_size(e["ctor"], e["fields"], calls.get(e["ctor"], []))
    with open(CACHE, "wb") as fh:
        pickle.dump(out, fh)
    if verbose:
        for k in sorted(out):
            e = out[k]
            print("%-46s vt=%#010x ctor=%#010x size=%#06x args=%d fields=%s"
                  % (k, e["vt"], e["ctor"], e["size"], e["args"],
                     [(hex(f["offset"]), f["size"], f["arg"], f["kind"])
                      for f in e["fields"]]))
    return out


def get(cls, force=False):
    """One entry by class name, with or without the leading C / trailing Command."""
    tab = build(force=force)
    if cls in tab:
        return tab[cls]
    for k in tab:
        if k.lower() == ("c" + str(cls).lower()):
            return tab[k]
    for k in tab:
        if k.lower() == ("c" + str(cls) + "command").lower():
            return tab[k]
    return tab[str(cls) + "Command"]


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    tab = build(force="-f" in sys.argv, verbose=not args)
    if args:
        for p in args:
            hits = [k for k in sorted(tab) if p.lower() in k.lower()]
            for k in hits:
                e = tab[k]
                print("%-46s vt=%#010x ctor=%#010x size=%#06x args=%d"
                      % (k, e["vt"], e["ctor"], e["size"], e["args"]))
                for f in e["fields"]:
                    print("      +%#06x %d arg%s %s"
                          % (f["offset"], f["size"],
                             f["arg"] if f["arg"] is not None else "-", f["kind"]))
            if not hits:
                print("no match: %s" % p)
        print("total classes: %d" % len(tab))
