# -*- coding: utf-8 -*-
"""Cheap byte-pattern scanners on hoi4.exe (static, read-only).

Usage:
  python wfind.py callrange 0xA59000 0xA59160     # call rel32 targeting RVA in [lo,hi)
  python wfind.py call 0xA590C0                    # exact target
  python wfind.py movimm 0x34                      # `mov dword ptr [reg+0x34], imm/reg` sites
  python wfind.py lea 0x33EB2A0                    # lea rip-relative -> RVA
"""
import struct
import sys

import pedis


def iter_calls():
    D = pedis.D
    text = [s for s in pedis.SECS if s[0] == ".text"][0]
    nm, va, vs, rp, rs = text
    for i in range(rs - 5):
        if D[rp + i] != 0xE8:
            continue
        rel = struct.unpack_from("<i", D, rp + i + 1)[0]
        yield va + i, (va + i + 5 + rel) & 0xFFFFFFFFFFFFFFFF


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return
    cmd = sys.argv[1]
    if cmd in ("callrange", "call"):
        if cmd == "call":
            lo = hi = int(sys.argv[2], 0)
            hi = lo + 1
        else:
            lo, hi = int(sys.argv[2], 0), int(sys.argv[3], 0)
        hits = [(s, t) for s, t in iter_calls() if lo <= t < hi]
        print("calls into [0x%X,0x%X): %d" % (lo, hi, len(hits)))
        for s, t in hits:
            print("   site 0x%X -> 0x%X" % (s, t))
    elif cmd == "movimm":
        off = int(sys.argv[2], 0)
        b = bytes([0xC7])  # mov r/m32, imm32
        D = pedis.D
        text = [s for s in pedis.SECS if s[0] == ".text"][0]
        nm, va, vs, rp, rs = text
        n = 0
        for i in range(rs - 7):
            if D[rp + i] != 0xC7 or D[rp + i + 1] & 0xC0 != 0x40:
                continue
            modrm = D[rp + i + 1]
            if modrm & 0xC7 != 0x80:  # [reg+disp32]
                continue
            disp = struct.unpack_from("<i", D, rp + i + 2)[0]
            if disp != off:
                continue
            n += 1
            print("   0x%X  disp=0x%X" % (va + i, disp))
        print("total", n)
    elif cmd == "lea":
        print("use: python pedis.py xref 0x%X" % int(sys.argv[2], 0))
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
