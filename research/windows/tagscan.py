"""Dump GS_TAG2INDEX next to GS_COUNTRIES and flag tags that index past the end.

GS_TAG2INDEX is CPdxArray<int32> with 443 entries while GS_COUNTRIES is
CPdxArray<CCountry*> with 440.  Any lookup that walks the *tag* table and then
indexes the *country* table with the result will read past the end for those 3
extra tags -- which matches hoi4.exe+0xC08724 (an indexed 4-byte table read)
being reached from `mov rax,[CGameState+0x310]` (GS_COUNTRIES).

Usage: python research/windows/tagscan.py [pid]
"""
import os
import struct
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from calling.mem import Proc  # noqa: E402

G_GAMESTATE = 0x33DD6B0
GS_COUNTRIES = 0x310
GS_TAG2INDEX = 0x340
GS_TAG_DB = 0x358
PDX_DATA, PDX_CAP, PDX_SIZE = 0x00, 0x08, 0x0C


def find_pid():
    out = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq hoi4.exe", "/FO", "CSV", "/NH"],
        capture_output=True, text=True,
    ).stdout.strip()
    for line in out.splitlines():
        parts = [x.strip('"') for x in line.split('","')]
        if len(parts) >= 2 and parts[0].lower() == "hoi4.exe":
            return int(parts[1])
    return None


def pdx(p, addr):
    return p.u64(addr + PDX_DATA), p.i32(addr + PDX_CAP), p.i32(addr + PDX_SIZE)


def msvc_string(p, addr):
    """MSVC std::string (0x20 bytes): buf[16] @+0, size @+0x10, cap @+0x18."""
    size = p.u64(addr + 0x10)
    cap = p.u64(addr + 0x18)
    if cap > 15:
        buf = p.u64(addr)
    else:
        buf = addr
    if size <= 0 or size > 64:
        return ""
    raw = p.read(buf, int(size))
    return raw.rstrip(b"\x00").decode("utf-8", "replace")


def main():
    pid = int(sys.argv[1]) if len(sys.argv) > 1 else find_pid()
    if not pid:
        print("hoi4.exe not running")
        return 2
    p = Proc(pid)
    gs = p.u64(p.base + G_GAMESTATE)
    print("pid=%d gamestate=0x%x" % (pid, gs))
    if not gs:
        print("main menu")
        return 1

    cdata, ccap, csize = pdx(p, gs + GS_COUNTRIES)
    idata, icap, isize = pdx(p, gs + GS_TAG2INDEX)
    sdata, scap, ssize = pdx(p, gs + GS_TAG_DB)
    print("countries : size=%d" % csize)
    print("tag2index : size=%d" % isize)
    print("tagdb     : size=%d" % ssize)
    print("")

    n = min(isize, ssize, 2048)
    out_of_range = []
    negatives = []
    pairs = []
    for i in range(n):
        idx = p.i32(idata + i * 4)
        tag = msvc_string(p, sdata + i * 0x20) if ssize else ""
        pairs.append((i, tag, idx))
        if idx < 0:
            negatives.append((i, tag, idx))
        elif idx >= csize:
            out_of_range.append((i, tag, idx))

    print("tags with negative index : %d" % len(negatives))
    for r in negatives[:20]:
        print("   tag#%-4d %-6s -> %d" % r)
    print("tags indexing past countries(%d) : %d" % (csize, len(out_of_range)))
    for r in out_of_range[:20]:
        print("   tag#%-4d %-6s -> %d" % r)

    # duplicate country indices would mean a tag aliases another country
    seen = {}
    dups = []
    for i, tag, idx in pairs:
        if idx < 0:
            continue
        if idx in seen:
            dups.append((seen[idx], tag, i, idx))
        else:
            seen[idx] = tag
    print("countries reachable by 2+ tags : %d" % len(dups))
    for d in dups[:20]:
        print("   country %d used by %s and %s" % (d[3], d[0], d[1]))

    print("\nfirst 8 :", [(t, i) for _, t, i in pairs[:8]])
    print("last 8  :", [(t, i) for _, t, i in pairs[-8:]])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
