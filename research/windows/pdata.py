"""Exact function boundaries from the PE .pdata exception directory.

    python pdata.py fn   0x11EDB10      # enclosing function start/end
    python pdata.py dis  0x11EDB10      # disassemble the WHOLE function
    python pdata.py list 0x11ED000 0x11EE000   # functions in a range
    python pdata.py owner 0x2A0FBC8     # which functions reference an address

Every function in hoi4.exe is RUNTIME_FUNCTION {BeginRVA, EndRVA, UnwindRVA}.
"""
import sys
import struct
import pedis


def _sections():
    return pedis.SECS


def load_pdata():
    """[(begin, end, unwind)] for every function, sorted by begin."""
    secs = _sections()
    pd = None
    for s in secs:
        name = s[0] if isinstance(s, (list, tuple)) else s.name
        if name in (".pdata", b".pdata"):
            pd = s
            break
    if pd is None:
        raise SystemExit("no .pdata section")
    # tuple order used by pedis: (name, va, vsize, rawptr, rawsize, chars)
    va, vsize, raw = pd[1], pd[2], pd[3]
    blob = pedis.D[raw:raw + vsize]
    out = []
    for i in range(0, len(blob) - 11, 12):
        b, e, u = struct.unpack_from("<III", blob, i)
        if b == 0 and e == 0:
            continue
        out.append((b, e, u))
    out.sort()
    return out


_PD = None


def pdata():
    global _PD
    if _PD is None:
        _PD = load_pdata()
    return _PD


def enclosing(rva):
    tbl = pdata()
    lo, hi = 0, len(tbl) - 1
    best = None
    while lo <= hi:
        mid = (lo + hi) // 2
        b, e, u = tbl[mid]
        if b <= rva < e:
            return tbl[mid]
        if rva < b:
            hi = mid - 1
        else:
            lo = mid + 1
    # fall back to a linear scan (pdata is not always perfectly ordered)
    for b, e, u in tbl:
        if b <= rva < e:
            return (b, e, u)
    return best


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return
    what = sys.argv[1]
    if what == "fn":
        rva = int(sys.argv[2], 16)
        b, e, u = enclosing(rva)
        print("0x%x is in function 0x%x .. 0x%x  (%d bytes, unwind 0x%x)"
              % (rva, b, e, e - b, u))
    elif what == "dis":
        rva = int(sys.argv[2], 16)
        b, e, u = enclosing(rva)
        print("function 0x%x .. 0x%x (%d bytes)" % (b, e, e - b))
        pedis.dis(b, (e - b) // 2 + 4)
    elif what == "list":
        lo, hi = int(sys.argv[2], 16), int(sys.argv[3], 16)
        for b, e, u in pdata():
            if lo <= b < hi:
                print("0x%08x .. 0x%08x  (%d)" % (b, e, e - b))
    elif what == "owner":
        pedis.xref(int(sys.argv[2], 16))


if __name__ == "__main__":
    main()
