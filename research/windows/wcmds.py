# -*- coding: utf-8 -*-
"""Dump and exercise the Windows console command table.

    python wcmds.py            # catalogue + a few safe commands
    python wcmds.py "pp 5" "add_stability 1"

Reads G_CONSOLE_MANAGER (an INLINE CPdxArray<CConsoleCmd> at RVA 0x33DD968 in
1.19.5.0), prints 370 entries with their handler RVAs and aliases, then runs the
given commands through `consolecmd.run` (the same path `g.console()` uses).
"""
import os
import struct
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "calling"))

import offsets_win as O          # noqa: E402
import consolecmd as C           # noqa: E402
from api import HOI4             # noqa: E402

SAFE = ["pp 3", "pp 1 2 3", "pp", "manpower", "manpower 1000", "xp 10"]


def main():
    g = HOI4()
    p = g.p
    a = g.va(O.G_CONSOLE_MANAGER)
    raw = p.read(a, 0x18)
    data, cap, size, alloc = struct.unpack_from("<QiiQ", raw)
    print("G_CONSOLE_MANAGER @ %#x  data=%#x cap=%d size=%d alloc=base+%#x"
          % (a, data, cap, size, alloc - g.base))

    base, count = C.table(g)
    print("table %#x -> %d entries (walked, global says %d)" % (base, count, size))

    names = [p.stdstring(base + i * C.STRIDE + O.CMD_NAME) for i in range(count)]
    functions = [p.u64(base + i * C.STRIDE + O.CMD_FUNC) for i in range(count)]
    bad_fn = [f for f in functions if not (g.base <= f < g.base + O.PE_SIZE_OF_IMAGE)]
    print("nameless=%d  handler-outside-image=%d" % (sum(1 for n in names if not n), len(bad_fn)))
    dup = sorted({n for n in names if names.count(n) > 1})
    print("duplicate names: %s" % dup)

    cat = C.commands(g)
    print("catalogue: %d keys, %d with aliases" % (len(cat), sum(1 for v in cat.values() if v["aliases"])))
    for k in ("pp", "manpower", "tag", "annex", "spawn", "add_equipment"):
        print("  %-14s %s" % (k, cat.get(k)))

    for line in (sys.argv[1:] or SAFE):
        t0 = time.time()
        try:
            ok, msg = C.run(g, line)
            print("%-24s ok=%-5s %-58s %.3fs" % (line, ok, (msg or "")[:58], time.time() - t0))
        except Exception as e:
            print("%-24s EXC %r" % (line, e))
            break


if __name__ == "__main__":
    main()
