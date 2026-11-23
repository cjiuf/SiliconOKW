# -*- coding: utf-8 -*-
"""Validate CONTEXT register offsets by round-tripping them on a scratch thread
of this very process.  No game involved."""
import ctypes
import struct
import os
import sys
import threading

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "calling", "process"))
from ctypes import wintypes as wt  # noqa: E402

import winject as W  # noqa: E402

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
CTX_SIZE = 0x4D0
CONTEXT_ALL = 0x0010001F


def get(h, addr, base):
    struct.pack_into("<I", (ctypes.c_char * (CTX_SIZE + 16)).from_address(base),
                     addr - base + 0x30, CONTEXT_ALL)
    assert k32.GetThreadContext(h, ctypes.c_void_p(addr))


buf = ctypes.create_string_buffer(CTX_SIZE + 16)
base = ctypes.addressof(buf)
addr = (base + 15) & ~15


def rd(h):
    struct.pack_into("<I", buf, addr - base + 0x30, CONTEXT_ALL)
    assert k32.GetThreadContext(h, ctypes.c_void_p(addr)), ctypes.get_last_error()


def g(o):
    return struct.unpack_from("<Q", buf, addr - base + o)[0]


def s(o, v):
    struct.pack_into("<Q", buf, addr - base + o, v & 0xFFFFFFFFFFFFFFFF)


stop = threading.Event()


def body():
    n = 0
    while not stop.is_set():
        n += 1


th = threading.Thread(target=body, daemon=True)
th.start()

h = k32.OpenThread(0x0008 | 0x0010 | 0x0002, False, th.ident)
print("thread %d  handle %s  align offset %d" % (th.ident, h, addr - base))

rd(h)
orig = {o: g(o) for o in (W.OFF_RAX, W.OFF_RCX, W.OFF_RDX, W.OFF_R8, W.OFF_R9,
                          W.OFF_RSP, W.OFF_RIP)}
print("before: " + " ".join("%s=%#x" % (k, v) for k, v in orig.items()))

k32.SuspendThread(h)
rd(h)
sent = {W.OFF_RAX: 0x1111, W.OFF_RCX: 0x2222, W.OFF_RDX: 0x3333,
        W.OFF_R8: 0x4444, W.OFF_R9: 0x5555}
for o, v in sent.items():
    s(o, v)
ok = k32.SetThreadContext(h, ctypes.c_void_p(addr))
print("SetThreadContext=%s err=%d" % (ok, ctypes.get_last_error()))
rd(h)
got = {o: g(o) for o in sent}
print("after : " + " ".join("%s=%#x" % (k, v) for k, v in got.items()))
for o, v in sent.items():
    print("  offset %#04x sent %#x got %#x  %s"
          % (o, v, got[o], "OK" if got[o] == v else "*** MISMATCH ***"))
k32.ResumeThread(h)
stop.set()
th.join(timeout=1)
print("restored rc: %s" % k32.SetThreadContext(h, ctypes.c_void_p(addr)))
