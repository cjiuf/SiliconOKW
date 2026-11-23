# -*- coding: utf-8 -*-
"""Read-only probe: list hoi4 threads and their current RIP. No calls, no freeze."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "calling", "process"))
import winject as W  # noqa: E402
from winject import k32  # noqa: E402

pid = W.find_pid()
IMG = W.module_base(pid, "hoi4.exe")
print("pid %d  image %#x" % (pid, IMG))
print("window thread (EnumWindows) : %s" % W.main_thread(pid))
print("window_threads              : %s" % W.window_threads(pid))

tids = sorted(W.list_threads(pid))
print("thread count %d: %s" % (len(tids), tids))
inj = W.Injector(pid)
inj.attach()
print("\n%-7s %-20s" % ("tid", "rip"))
for tid in tids:
    h = k32.OpenThread(0x0008 | 0x0010 | 0x0002, False, tid)
    if not h:
        print("%-7d <open failed>" % tid)
        continue
    ctx = inj._get_ctx(h)
    rip = ctx.get(W.OFF_RIP)
    tag = "  <- in hoi4.exe +%#x" % (rip - IMG) if IMG <= rip < IMG + 0x38A3000 else ""
    print("%-7d %#-20x%s" % (tid, rip, tag))
    k32.CloseHandle(h)
inj.detach()
