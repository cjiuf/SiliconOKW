# -*- coding: utf-8 -*-
"""Detailed PE disassembler + RTTI/vtable helper for hoi4.exe 1.19.5.0.

Addresses are RVAs (image-relative). Image base in the file is 0x140000000.

Usage (all from stage/winrepo/research/windows, or set PYTHONPATH):
    python pedis.py dis 0x11D2F00 0x80
    python pedis.py fn  0x11D2F00            # whole function (ret/int3 bounded)
    python pedis.py vt  0x2A11A88 0x40      # dump vtable slots as RVAs
    python pedis.py vtname CAddAdvisorCommand
    python pedis.py xref 0x2A11A88          # all `lea reg,[rip+vt]` sites in .text
    python pedis.py calls 0x11D2F00         # all `call 0x11D2F00` sites in .text
    python pedis.py str 0x29C77F8           # resolve a rip-relative lea target's string

All static: opens hoi4.exe read-only, never touches the live process.
"""
import os
import struct
import sys

import capstone
from capstone import Cs, CS_ARCH_X86, CS_MODE_64, CS_OP_IMM, CS_OP_MEM, CS_OP_REG
from capstone.x86 import X86_INS_LEA, X86_INS_CALL, X86_INS_JMP

HERE = os.path.dirname(os.path.abspath(__file__))
CALLING = os.path.abspath(os.path.join(HERE, "..", "..", "calling"))
if CALLING not in sys.path:
    sys.path.insert(0, CALLING)

import config

EXE = config.binary_path(required=True)
D = open(EXE, "rb").read()
e = struct.unpack_from("<I", D, 0x3C)[0]
coff = e + 4
nsec = struct.unpack_from("<H", D, coff + 2)[0]
opt = coff + 20
opt_size = struct.unpack_from("<H", D, coff + 16)[0]
IMAGE_BASE = struct.unpack_from("<Q", D, opt + 0x18)[0]
SIZE_OF_IMAGE = struct.unpack_from("<I", D, opt + 0x38)[0]
SECS = []
for i in range(nsec):
    o = opt + opt_size + i * 40
    nm = D[o:o + 8].rstrip(b"\0").decode("latin-1")
    vs, va, rs, rp = struct.unpack_from("<IIII", D, o + 8)
    SECS.append((nm, va, vs, rp, rs))

MD = Cs(CS_ARCH_X86, CS_MODE_64)
MD.detail = True


def rva2off(rva):
    for nm, va, vs, rp, rs in SECS:
        if va <= rva < va + max(vs, rs):
            d = rva - va
            if d >= rs:
                return None
            return rp + d
    return None


def off2rva(off):
    for nm, va, vs, rp, rs in SECS:
        if rp <= off < rp + rs:
            return va + (off - rp)
    return None


def read(rva, n):
    o = rva2off(rva)
    if o is None:
        return b""
    return D[o:o + n]


def u64(rva):
    b = read(rva, 8)
    return struct.unpack_from("<Q", b)[0] if len(b) == 8 else 0


def cstr(rva, maxn=120):
    o = rva2off(rva)
    if o is None:
        return None
    end = D.find(b"\0", o, o + maxn)
    if end < 0:
        end = o + maxn
    try:
        return D[o:end].decode("utf-8")
    except Exception:
        return repr(D[o:end])


# RTTI: vtable[-1] = COL RVA (image-relative pointer stored as u32? MSVC stores
# an RVA (4 bytes) in .rdata for x64 COL pointer). Actually the pointer before the
# vtable is a 8-byte VA when /bigobj? In this exe it is a 4-byte RVA. We resolve
# robustly: try reading a u64 = IMAGE_BASE + something (VA), then u32 RVA.
def vtable_class(vt_rva):
    """Return the .?AV...@@ class name whose vtable is at vt_rva, else None."""
    import rtti_win
    m = rtti_win.build()
    return m["vt"].get(vt_rva)


def _resolve_target(ins, operand):
    """RIP-relative memory operand -> absolute RVA (int) or None."""
    if operand.type == CS_OP_MEM and operand.mem.base == capstone.x86.X86_REG_RIP:
        return (ins.address + ins.size + operand.mem.disp) - IMAGE_BASE
    return None


def _tgt_name(tgt):
    if tgt is None:
        return ""
    cls = vtable_class(tgt)
    s = cstr(tgt)
    parts = []
    if cls:
        parts.append("VT:%s" % cls)
    if s and s.strip() and all(32 <= ord(c) < 127 for c in s[:40]):
        parts.append('str:"%s"' % s[:48])
    return ("  ; " + " ".join(parts)) if parts else ""


def dis(rva, n=60, label=""):
    off = rva2off(rva)
    if off is None:
        print("unmapped rva %#x" % rva)
        return
    print("\n=== %s  rva %#x (VA %#x) ===" % (label or "dis", rva, IMAGE_BASE + rva))
    code = D[off:off + n * 10]
    cnt = 0
    for ins in MD.disasm(code, IMAGE_BASE + rva):
        line = "  %#012x  %-9s %s" % (ins.address - IMAGE_BASE, ins.mnemonic, ins.op_str)
        extra = ""
        for op in ins.operands:
            if ins.id in (X86_INS_LEA, X86_INS_CALL, X86_INS_JMP):
                t = _resolve_target(ins, op)
                if t is not None:
                    # call/jmp rel32 imm also handled here
                    extra += _tgt_name(t)
            if ins.id in (X86_INS_CALL, X86_INS_JMP) and op.type == CS_OP_IMM:
                t = (ins.address + ins.size + op.imm) - IMAGE_BASE
                extra += ("  ; -> %#x%s" % (t, _tgt_name(t)))
        # resolve any rip-relative lea/mov target for annotation
        if ins.id == X86_INS_LEA:
            for op in ins.operands:
                t = _resolve_target(ins, op)
                if t is not None:
                    extra += ("  ; rip->%#x%s" % (t, _tgt_name(t)))
        print(line + extra)
        cnt += 1
        if cnt >= n:
            break
        if ins.mnemonic == "ret" and cnt > 4:
            break


def vt(rva, n=0x40, resolve=True):
    print("\n=== vtable %#x (VA %#x) ===" % (rva, IMAGE_BASE + rva))
    cls = vtable_class(rva) if resolve else None
    if cls:
        print("  class: %s" % cls)
    for i in range(0, n, 8):
        val = u64(rva + i)
        if val == 0:
            print("  +%04x  %#x" % (i, 0))
            continue
        # value is a VA (IMAGE_BASE + fn_rva) if it lands in image
        fn = val - IMAGE_BASE if IMAGE_BASE <= val < IMAGE_BASE + SIZE_OF_IMAGE else None
        tag = ""
        if fn is not None:
            s = cstr(fn)
            tag = "  ; fn %#x" % fn
        print("  +%04x  %#018x%s" % (i, val, tag))


def xref(vt_rva):
    """All `lea reg,[rip+X]` in .text whose target == vt_rva (ctor/clone/factory)."""
    txt = [s for s in SECS if s[0] == ".text"][0]
    _nm, va, vs, rp, rs = txt
    blob = D[rp:rp + rs]
    wanted_va = IMAGE_BASE + vt_rva
    out = []
    i = 0
    while True:
        j = blob.find(b"\x8d", i)
        if j < 0:
            break
        i = j + 1
        if j == 0 or blob[j - 1] not in (0x48, 0x4C, 0x49):
            continue
        if (j + 6) > len(blob):
            break
        modrm = blob[j + 1]
        if (modrm & 0xC7) != 0x05:  # mod=00 rm=101 => rip-relative
            continue
        disp = struct.unpack_from("<i", blob, j + 2)[0]
        site_rva = va + j - 1  # include the REX prefix byte
        tgt_va = (IMAGE_BASE + site_rva) + 7 + disp  # len(REX+8D+modrm+disp32)=7
        if (tgt_va - IMAGE_BASE) == vt_rva:
            out.append(site_rva)
    print("lea->%#x sites (%d):" % (vt_rva, len(out)))
    for s in out:
        print("   %#x" % s)
    return out


def calls(target_rva):
    """All `call rel32` sites in .text whose target == target_rva."""
    txt = [s for s in SECS if s[0] == ".text"][0]
    _nm, va, vs, rp, rs = txt
    blob = D[rp:rp + rs]
    out = []
    i = 0
    while True:
        j = blob.find(b"\xe8", i)
        if j < 0:
            break
        i = j + 1
        if (j + 5) > len(blob):
            break
        rel = struct.unpack_from("<i", blob, j + 1)[0]
        site_rva = va + j
        tgt = site_rva + 5 + rel
        if tgt == target_rva:
            out.append(site_rva)
    print("call->%#x sites (%d):" % (target_rva, len(out)))
    for s in out:
        print("   %#x" % s)
    return out


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(0)
    cmd = sys.argv[1]
    if cmd == "dis":
        dis(int(sys.argv[2], 0), int(sys.argv[3], 0) if len(sys.argv) > 3 else 60)
    elif cmd == "fn":
        dis(int(sys.argv[2], 0), 400)
    elif cmd == "vt":
        vt(int(sys.argv[2], 0), int(sys.argv[3], 0) if len(sys.argv) > 3 else 0x40)
    elif cmd == "vtname":
        import rtti_win
        m = rtti_win.build()
        name = sys.argv[2]
        for k, vs in m["vtables"].items():
            if k == name or name.lower() in k.lower():
                print("%s -> %s" % (k, ["%#x" % v for v in vs]))
    elif cmd == "xref":
        xref(int(sys.argv[2], 0))
    elif cmd == "calls":
        calls(int(sys.argv[2], 0))
    elif cmd == "str":
        print(repr(cstr(int(sys.argv[2], 0))))
    elif cmd == "u64":
        print("%#x" % u64(int(sys.argv[2], 0)))
    else:
        print(__doc__)
