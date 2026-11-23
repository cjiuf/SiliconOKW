"""Find every `call rel32` to a given RVA in hoi4.exe's .text.

Usage: python research/windows/xrefs.py 0xC085F0 [--limit 40]

Prints each caller RVA plus the instructions leading up to the call, so the
argument setup is visible.
"""
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from wdis import load, EXE  # noqa: E402

from capstone import Cs, CS_ARCH_X86, CS_MODE_64  # noqa: E402


def get_text(sections):
    for s in sections:
        if s["name"] == ".text":
            return s
    raise SystemExit(".text not found")


def main():
    target = int(sys.argv[1], 0)
    limit = 40
    if "--limit" in sys.argv:
        limit = int(sys.argv[sys.argv.index("--limit") + 1])

    data, image_base, sections = load(EXE)
    t = get_text(sections)
    code = data[t["raw"]: t["raw"] + t["rsize"]]
    text_rva = t["va"]

    hits = []
    for i in range(len(code) - 5):
        if code[i] != 0xE8:
            continue
        rel = struct.unpack_from("<i", code, i + 1)[0]
        next_rva = text_rva + i + 5
        if (next_rva + rel) & 0xFFFFFFFF == target:
            hits.append(text_rva + i)

    print("call sites to RVA 0x%X : %d" % (target, len(hits)))

    md = Cs(CS_ARCH_X86, CS_MODE_64)
    for rva in hits[:limit]:
        print("\n=== caller RVA 0x%X ===" % rva)
        start = rva - 0x24
        off = start - text_rva
        chunk = code[off: off + 0x34]
        for ins in md.disasm(chunk, image_base + start):
            marker = "   <== CALL" if ins.mnemonic == "call" else ""
            print("  0x%X  %-7s %s%s" % (ins.address - image_base, ins.mnemonic, ins.op_str, marker))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
