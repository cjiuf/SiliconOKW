# -*- coding: utf-8 -*-
"""Graded injector verification: context read-back, then real calls.

Every step prints before it runs, so a crash pinpoints the failing step.
"""
import ctypes
import struct
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "calling", "process"))
import winject as W  # noqa: E402
from winject import k32  # noqa: E402


def say(*a):
    print(*a)
    sys.stdout.flush()


pid = W.find_pid()
IMG = W.module_base(pid, "hoi4.exe")
say("pid %d  image %#x" % (pid, IMG))

inj = W.Injector(pid)
inj.attach()
ALLOC = IMG + 0x30FD000
sc = inj.scratch
SRES, ARR, STRS = sc + 0x1000, sc + 0x1040, sc + 0x1080
say("scratch %#x  SRES %#x  ARR %#x  STRS %#x" % (sc, SRES, ARR, STRS))


def readback(func_addr, args):
    """Set the hijack context, read it back, restore. Executes nothing."""
    if not inj.frozen:
        inj._freeze()
    rsp = sc + W.STACK_OFF + W.STACK_SIZE - 0x108
    cand = inj.tid or W.main_thread(pid)
    pool = dict((tid, h) for h, tid in inj.frozen)
    if cand not in pool:
        cand = inj.frozen[0][1]
    h = pool[cand]
    old = inj._get_ctx(h)
    saved = ctypes.string_at(old.addr, W.CTX_SIZE)
    new = W._Ctx(old.addr)
    new.set(W.OFF_RIP, func_addr)
    new.set(W.OFF_RSP, rsp)
    new.set(W.OFF_RAX, 0)
    for i, off in enumerate(W.ARG_REGS):
        new.set(off, args[i] if i < len(args) else 0)
    _check = k32.SetThreadContext(h, new.ptr())
    cur = inj._get_ctx(h)
    got = dict(rip=cur.get(W.OFF_RIP), rsp=cur.get(W.OFF_RSP),
               rcx=cur.get(W.OFF_RCX), rdx=cur.get(W.OFF_RDX), rax=cur.get(W.OFF_RAX))
    k32.SetThreadContext(h, W._Ctx(saved).ptr())
    return cand, rsp, got, _check


def call(va, args, timeout=8.0, verbose=False):
    b = bytearray(0x400)
    struct.pack_into("<QQ", b, 0x18, 0, 0xF)
    struct.pack_into("<QIIQ", b, 0x40, STRS, 8, len(args), ALLOC)
    for i, a in enumerate(args):
        raw = a.encode()
        o = 0x80 + i * 0x20
        b[o:o + min(len(raw), 15)] = raw[:15]
        struct.pack_into("<QQ", b, o + 0x10, len(raw), 15)
    inj._write(sc + 0x1000, b)
    return inj.call(va, (SRES, ARR), timeout=timeout, debug=verbose)


def sresult():
    ok = inj._read(SRES, 1)[0]
    n, c = struct.unpack_from("<QQ", inj._read(SRES + 8, 0x20), 0x10)
    if c <= 15:
        msg = inj._read(SRES + 8, n)
    else:
        p = struct.unpack("<Q", inj._read(SRES + 8, 8))[0]
        msg = inj._read(p, n)
    return ok, msg.decode("utf-8", "replace")


def pp_value():
    gs = struct.unpack("<Q", inj._read(IMG + 0x33DD6B0, 8))[0]
    data, cap, size, _ = struct.unpack("<QIIQ", inj._read(gs + 0x310, 0x18))
    for i in range(size):
        c = struct.unpack("<Q", inj._read(data + i * 8, 8))[0]
        if struct.unpack("<I", inj._read(c + 0x08, 4))[0] == 1:
            pol = struct.unpack("<Q", inj._read(c + 0xF90, 8))[0]
            return struct.unpack("<q", inj._read(pol + 0xE0, 8))[0] / 1e5


say("\n[1] context read-back (no execution)")
tid, rsp, got, scok = readback(IMG + 0x285FF0, (SRES, ARR))
say("    thread %d   SetThreadContext=%s" % (tid, scok))
say("    expect rip=%#x rsp=%#x rcx=%#x rdx=%#x rax=0" % (IMG + 0x285FF0, rsp, SRES, ARR))
say("    actual rip=%#x rsp=%#x rcx=%#x rdx=%#x rax=%#x"
    % (got["rip"], got["rsp"], got["rcx"], got["rdx"], got["rax"]))
good = (got["rip"] == IMG + 0x285FF0 and got["rsp"] == rsp
        and got["rcx"] == SRES and got["rdx"] == ARR)
say("    VERDICT: %s" % ("context applied" if good else "CONTEXT MISMATCH"))
if not good:
    inj.detach()
    raise SystemExit(2)

say("\n[2] getter with no args: GetConsoleCmdManager (expect %#x)" % (IMG + 0x33DD968))
r = call(IMG + 0x246E70, [], verbose=True)
say("    returned %#x   calls=%d dirty=%s" % (r, inj.call_count, inj.dirty))

say("\n[3] pp 0  (real command, zero delta)   PP now %.3f" % pp_value())
call(IMG + 0x285FF0, ["0"], verbose=True)
say("    SResult ok=%d msg=%r" % sresult())
say("    PP now %.3f   calls=%d dirty=%s" % (pp_value(), inj.call_count, inj.dirty))

say("\n[4] pp 7  (expect +7)")
call(IMG + 0x285FF0, ["7"], verbose=True)
say("    SResult ok=%d msg=%r" % sresult())
say("    PP now %.3f   calls=%d dirty=%s" % (pp_value(), inj.call_count, inj.dirty))

inj.detach()
say("\ndetached cleanly")
