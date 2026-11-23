# -*- coding: utf-8 -*-
"""Stress the injector: many console calls in one attach, verifying the message."""
import struct
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "calling", "process"))
import winject as W  # noqa: E402


def say(*a):
    print(*a)
    sys.stdout.flush()


pid = W.find_pid()
IMG = W.module_base(pid, "hoi4.exe")
inj = W.Injector(pid)
inj.attach()
ALLOC = IMG + 0x30FD000
sc = inj.scratch
SRES, ARR = sc + W.PAYLOAD_OFF, sc + W.PAYLOAD_OFF + 0x40
STRS = sc + W.PAYLOAD_OFF + 0x80
PP = IMG + 0x285FF0
GETTER = IMG + 0x246E70
N = int(sys.argv[1]) if len(sys.argv) > 1 else 20
DELAY = float(sys.argv[2]) if len(sys.argv) > 2 else 0.03
say("scratch %#x  payload %#x" % (sc, W.PAYLOAD_OFF))
inj.tid = W.main_thread(pid)


def call(va, args):
    b = bytearray(0x400)
    struct.pack_into("<QQ", b, 0x18, 0, 0xF)
    struct.pack_into("<QIIQ", b, 0x40, STRS, max(1, len(args)), len(args), ALLOC)
    for i, a in enumerate(args):
        raw = a.encode()
        o = 0x80 + i * 0x20
        b[o:o + min(len(raw), 15)] = raw[:15]
        struct.pack_into("<QQ", b, o + 0x10, len(raw), 15)
    inj._write(SRES, b)
    time.sleep(DELAY)
    inj.call(va, (SRES, ARR), timeout=8.0)
    ok = inj._read(SRES, 1)[0]
    head = inj._read(SRES + 8, 0x20)
    n, cap = struct.unpack_from("<QQ", head, 0x10)
    if cap <= 15:
        msg = inj._read(SRES + 8, n)
    else:
        msg = inj._read(struct.unpack_from("<Q", head, 0)[0], n)
    return ok, msg.decode("utf-8", "replace")


say("warm-up getter -> ok=%d msg=%r" % call(GETTER, []))
bad = 0
for i in range(N):
    n = i % 5
    ok, msg = call(PP, [str(n)])
    good = ok == 1 and ("Granted %d political power" % n) in msg
    if not good:
        bad += 1
        say("  [%2d] BAD ok=%d msg=%r (wanted %d)" % (i + 1, ok, msg, n))
say("after %d calls: bad=%d  dirty=%s  calls=%d" % (N, bad, inj.dirty, inj.call_count))
inj.detach()
say("detached cleanly")

