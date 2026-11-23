# -*- coding: utf-8 -*-
"""Tiny RVA disassembler for hoi4.exe (capstone)."""
import struct
import os
import sys

import capstone

import config
EXE = config.binary_path()
D = open(EXE, "rb").read()
e = struct.unpack_from("<I", D, 0x3C)[0]
coff = e + 4
nsec = struct.unpack_from("<H", D, coff + 2)[0]
opt = coff + 20
opt_size = struct.unpack_from("<H", D, coff + 16)[0]
base = struct.unpack_from("<Q", D, opt + 0x18)[0]
secs = []
for i in range(nsec):
    o = opt + opt_size + i * 40
    nm = D[o:o + 8].rstrip(b"\0").decode()
    vs, va, rs, rp = struct.unpack_from("<IIII", D, o + 8)
    secs.append((nm, va, vs, rp, rs))

MD = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_64)
MD.detail = False


def rva2off(rva):
    for nm, va, vs, rp, rs in secs:
        if va <= rva < va + max(vs, rs):
            return rp + (rva - va)
    return None


def dis(rva, n=40, label=""):
    off = rva2off(rva)
    print("\n=== %s  rva %#x ===" % (label, rva))
    for ins in MD.disasm(D[off:off + n * 8], base + rva):
        print("  %#012x  %-8s %s" % (ins.address, ins.mnemonic, ins.op_str))
        n -= 1
        if n <= 0:
            break


if __name__ == "__main__":
    targets = [("pp", 0x285FF0), ("tag", 0x2A4570), ("deironman", 0x25FE10),
               ("test", 0x2A5070), ("manpower", 0x281850)]
    for nm, rva in targets:
        dis(rva, 26, nm)
