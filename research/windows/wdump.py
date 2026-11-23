# -*- coding: utf-8 -*-
"""Disassemble an RVA range of hoi4.exe, resolving rip-relative targets."""

import struct
import os
import sys

from capstone import CS_ARCH_X86, CS_MODE_64, Cs
from capstone.x86 import X86_OP_MEM, X86_OP_REG, X86_REG_RIP

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "calling"))
import config  # noqa: E402

EXE = config.binary_path()
D = open(EXE, "rb").read()
e = struct.unpack_from("<I", D, 0x3C)[0]
coff = e + 4
nsec = struct.unpack_from("<H", D, coff + 2)[0]
opt = coff + 20
opt_size = struct.unpack_from("<H", D, coff + 16)[0]
SECS = []
for i in range(nsec):
    o = opt + opt_size + i * 40
    nm = D[o:o + 8].rstrip(b"\0").decode()
    vs, va, rs, rp = struct.unpack_from("<IIII", D, o + 8)
    SECS.append((nm, va, vs, rp, rs))
BASE = 0x140000000

md = Cs(CS_ARCH_X86, CS_MODE_64)
md.detail = True


def rva2off(rva):
    for nm, va, vs, rp, rs in SECS:
        if va <= rva < va + max(vs, rs):
            return rp + (rva - va)
    return None


def mem(rva, n):
    off = rva2off(rva)
    if off is None:
        return None
    return D[off:off + n]


def cstr(rva, n=48):
    b = mem(rva, n)
    if b is None:
        return None
    z = b.find(b"\0")
    return b[:z] if z >= 0 else b


def annot(insn):
    """Return list of '; -> 0xTARGET  "string"' notes for rip-relative ops."""
    out = []
    for op in insn.operands:
        if op.type == X86_OP_MEM and op.mem.base == X86_REG_RIP:
            tgt = insn.address + insn.size + op.mem.disp
            note = "-> %#x" % tgt
            s = cstr(tgt, 40)
            if s and len(s) >= 3 and all(32 <= c < 127 or c in (9, 10) for c in s):
                note += ' "%s"' % s.decode("ascii", "replace")
            out.append(note)
    return out


def dump(rva, n=128, label=""):
    off = rva2off(rva)
    if off is None:
        print("!! %#x not mapped" % rva)
        return
    code = D[off:off + n]
    print("--- %#x %s" % (rva, label))
    for i in md.disasm(code, rva):
        notes = annot(i)
        tail = ("   ; " + " ; ".join(notes)) if notes else ""
        print("  %08x  %-28s %-7s %s%s" % (i.address, i.bytes.hex(), i.mnemonic, i.op_str, tail))


if __name__ == "__main__":
    a = int(sys.argv[1], 0)
    n = int(sys.argv[2], 0) if len(sys.argv) > 2 else 128
    dump(a, n)
