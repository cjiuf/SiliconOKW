"""Disassemble hoi4.exe at an RVA and resolve RIP-relative targets.

2026-11-17.  Written to look at RVA 0xB2E62E -- the address in
`crashes/hoi4_2026.../exception.txt` that has now killed the game three times
(19:13, 20:06, and the 1938.06.01 tick recorded in AGENTS.md).  Every one of
them happened while hoi4_pause_in_hours had the clock running.

Usage:
  python wdis.py 0xB2E62E                 # 40 instructions from the fault address
  python wdis.py 0xB2E62E --back 120 --n 60
  python wdis.py 0xB2E62E --func          # walk back to the function prologue first

Needs `capstone` (5.x, pip install capstone).
"""
import argparse
import struct
import sys

from capstone import Cs, CS_ARCH_X86, CS_MODE_64, CS_OP_IMM, CS_OP_MEM
import capstone.x86 as X

EXE = r"D:\SteamLibrary\steamapps\common\Hearts of Iron IV\hoi4.exe"


def load(path):
    data = open(path, "rb").read()
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    nsec = struct.unpack_from("<H", data, pe + 6)[0]
    opt_size = struct.unpack_from("<H", data, pe + 20)[0]
    opt = pe + 24
    magic = struct.unpack_from("<H", data, opt)[0]
    image_base = (struct.unpack_from("<Q", data, opt + 24)[0] if magic == 0x20B
                  else struct.unpack_from("<I", data, opt + 28)[0])
    sec = opt + opt_size
    sections = []
    for i in range(nsec):
        off = sec + i * 40
        name = data[off:off + 8].rstrip(b"\0").decode("latin1")
        vsize, vaddr, rsize, raddr = struct.unpack_from("<IIII", data, off + 8)
        sections.append({"name": name, "va": vaddr, "vsize": vsize,
                         "raw": raddr, "rsize": rsize})
    return data, image_base, sections


def rva_to_off(sections, rva):
    for s in sections:
        if s["va"] <= rva < s["va"] + max(s["vsize"], s["rsize"]):
            return s["raw"] + (rva - s["va"])
    return None


def off_to_rva(sections, off):
    for s in sections:
        if s["raw"] <= off < s["raw"] + s["rsize"]:
            return s["va"] + (off - s["raw"])
    return None


def find_func_start(blob, rva, lo=0x400):
    """Walk back over int3/cc padding to the previous aligned boundary."""
    i = rva
    best = rva
    while i > rva - lo and i > 0:
        off = rva_to_off(_SECS, i)
        if off is None:
            break
        b = _DATA[off]
        if b in (0xCC, 0x90) and _DATA[off - 1] in (0xCC, 0x90, 0xC3):
            best = i + 1
        i -= 1
    # round up to the next 16-byte boundary at/after the padding run
    while best % 16:
        best += 1
    return best


_DATA, _BASE, _SECS = load(EXE)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("rva", type=lambda s: int(s, 0))
    ap.add_argument("--n", type=int, default=40)
    ap.add_argument("--back", type=int, default=0)
    ap.add_argument("--func", action="store_true")
    a = ap.parse_args()

    start = a.rva - a.back
    if a.func:
        start = find_func_start(_DATA, a.rva)
    off = rva_to_off(_SECS, start)
    if off is None:
        raise SystemExit("RVA 0x%x is not mapped" % start)
    code = _DATA[off:off + 16 * max(a.n, 1) + 32]

    md = Cs(CS_ARCH_X86, CS_MODE_64)
    md.detail = True
    print("image_base 0x%x   start RVA 0x%x" % (_BASE, start))
    print("fault RVA  0x%x  -> file offset 0x%x" % (a.rva, rva_to_off(_SECS, a.rva) or 0))
    print()
    n = 0
    for ins in md.disasm(code, start):
        mark = "  <<< FAULT" if ins.address <= a.rva < ins.address + ins.size else ""
        text = "%-8s %s" % (ins.mnemonic, ins.op_str)
        extra = ""
        for op in ins.operands:
            if op.type == CS_OP_IMM and font_ok(op.imm):
                extra += "   ; imm=0x%x" % op.imm
            if op.type == CS_OP_MEM and op.mem.base == X.X86_REG_RIP:
                tgt = ins.address + ins.size + op.mem.disp
                extra += "   ; [rip] -> RVA 0x%x" % tgt
            if op.type == CS_OP_IMM and ins.mnemonic in ("call", "jmp"):
                extra += "   ; target RVA 0x%x" % op.imm
        print("  %08x  %-40s%s%s" % (ins.address, text, extra, mark))
        n += 1
        if n >= a.n:
            break


def font_ok(v):
    return 0 < v < 0x1000000


if __name__ == "__main__":
    main()
