"""Scan CCurrentGameState country tables for corrupt entries.

Born out of the 2026-11-18 crash hunt: hoi4.exe+0xC08724 does
`cmp dword ptr [r8 + rdx*4], ecx` -- an indexed read into a table -- and the
caller chain starts with ``mov rax,[CGameState+0x310]`` (GS_COUNTRIES, the
CPdxArray<CCountry*>).  Both reported triggers were UI panels that walk the
country list (subject/puppet armies, the technology screen), so a single bad
pointer in that array would explain it.

Usage:
    python research/windows/gsscan.py            # auto-detect hoi4.exe
    python research/windows/gsscan.py <pid>
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
    """(data, cap, size) of an inline CPdxArray."""
    return p.u64(addr + PDX_DATA), p.i32(addr + PDX_CAP), p.i32(addr + PDX_SIZE)


def main():
    pid = int(sys.argv[1]) if len(sys.argv) > 1 else find_pid()
    if not pid:
        print("hoi4.exe not running")
        return 2
    p = Proc(pid)
    base = p.base
    gs = p.u64(base + G_GAMESTATE)
    print("pid        : %d" % pid)
    print("image_base : 0x%x" % base)
    print("gamestate  : 0x%x" % gs)
    if not gs:
        print("no game loaded (main menu)")
        return 1

    lo, hi = base, base + 0x4000000            # module image span
    data, cap, size = pdx(p, gs + GS_COUNTRIES)
    print("GS_COUNTRIES data=0x%x cap=%d size=%d" % (data, cap, size))

    t2i = pdx(p, gs + GS_TAG2INDEX)
    tdb = pdx(p, gs + GS_TAG_DB)
    print("GS_TAG2INDEX data=0x%x cap=%d size=%d" % t2i)
    print("GS_TAG_DB    data=0x%x cap=%d size=%d (inline std::string, 0x20 each)"
          % (tdb[0], tdb[1], tdb[2]))

    bad = []
    nulls = []
    rows = []
    for i in range(min(size, 4096)):
        try:
            c = p.u64(data + i * 8)
        except Exception as exc:
            bad.append((i, None, "read failed: %s" % exc))
            continue
        if not c:
            nulls.append(i)
            continue
        try:
            vt = p.u64(c)
        except Exception as exc:
            bad.append((i, c, "vtable read failed: %s" % exc))
            continue
        ok = lo <= vt < hi
        rows.append((i, c, vt, ok))
        if not ok:
            bad.append((i, c, "vtable 0x%x outside image" % vt))

    print("entries read : %d" % len(rows))
    print("null entries : %d %s" % (len(nulls), nulls[:20]))
    print("bad vtables  : %d" % len(bad))
    for b in bad[:20]:
        print("   idx %-4d ptr=%s  %s" % (b[0], ("0x%x" % b[1]) if b[1] else "-", b[2]))

    # duplicate pointers would mean two indices alias one country
    seen = {}
    dups = []
    for i, c, vt, ok in rows:
        if c in seen:
            dups.append((seen[c], i, c))
        else:
            seen[c] = i
    print("duplicate ptrs: %d %s" % (len(dups), dups[:10]))

    # a tag string should exist for every country index
    if tdb[2] and abs(tdb[2] - size) > 8:
        print("WARN: tag db size %d vs countries size %d" % (tdb[2], size))

    print("\nfirst 5:")
    for i, c, vt, ok in rows[:5]:
        print("   idx %-4d ptr=0x%x vt=0x%x ok=%s" % (i, c, vt, ok))
    print("last 5:")
    for i, c, vt, ok in rows[-5:]:
        print("   idx %-4d ptr=0x%x vt=0x%x ok=%s" % (i, c, vt, ok))
    return 0 if not bad and not nulls else 3


if __name__ == "__main__":
    raise SystemExit(main())
