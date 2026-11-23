# -*- coding: utf-8 -*-
"""Single-step the first instructions of an injected call and dump registers.

Sets the trap flag in the hijack context, so the debugger sees every instruction
boundary and can show exactly when a register stops matching what we asked for.
"""
import ctypes
import os
import struct
import sys
import threading
import time

from ctypes import wintypes as wt

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "calling", "process"))
import winject as W  # noqa: E402

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
DBG_CONTINUE = 0x00010002
DBG_EXCEPTION_NOT_HANDLED = 0x80010001
EXC, EPROC = 1, 5
OFF_EFLAGS = 0x44

CODES = {0x80000003: "BREAKPOINT", 0x80000004: "SINGLE_STEP",
         0xC0000005: "ACCESS_VIOLATION", 0xC000001D: "ILLEGAL_INSN"}


class EXCEPTION_RECORD(ctypes.Structure):
    pass


EXCEPTION_RECORD._fields_ = [
    ("ExceptionCode", wt.DWORD), ("ExceptionFlags", wt.DWORD),
    ("ExceptionRecord", ctypes.POINTER(EXCEPTION_RECORD)),
    ("ExceptionAddress", ctypes.c_void_p), ("NumberParameters", wt.DWORD),
    ("ExceptionInformation", ctypes.c_void_p * 15)]


class EXCEPTION_DEBUG_INFO(ctypes.Structure):
    _fields_ = [("ExceptionRecord", EXCEPTION_RECORD), ("dwFirstChance", wt.DWORD)]


class _U(ctypes.Union):
    _fields_ = [("Exception", EXCEPTION_DEBUG_INFO), ("blob", ctypes.c_byte * 176)]


class DEBUG_EVENT(ctypes.Structure):
    _fields_ = [("dwDebugEventCode", wt.DWORD), ("dwProcessId", wt.DWORD),
                ("dwThreadId", wt.DWORD), ("u", _U)]


for fn, args, res in (("DebugActiveProcess", [wt.DWORD], wt.BOOL),
                      ("DebugActiveProcessStop", [wt.DWORD], wt.BOOL),
                      ("WaitForDebugEvent", [ctypes.POINTER(DEBUG_EVENT), wt.DWORD], wt.BOOL),
                      ("ContinueDebugEvent", [wt.DWORD, wt.DWORD, wt.DWORD], wt.BOOL)):
    f = getattr(k32, fn)
    f.argtypes, f.restype = args, res

pid = W.find_pid()
IMG = W.module_base(pid, "hoi4.exe")
PP = IMG + 0x285FF0
inj = W.Injector(pid)
inj.attach()
sc = inj.scratch
SRES, ARR, STRS = sc + 0x1000, sc + 0x1040, sc + 0x1080
inj.prepare_console_args(["0"], IMG + 0x30FD000)
tid = W.main_thread(pid)

if not k32.DebugActiveProcess(pid):
    raise SystemExit("DebugActiveProcess failed %d" % ctypes.get_last_error())
print("pid %d image %#x sc %#x  target tid %d" % (pid, IMG, sc, tid))

state = {"setup": False, "steps": 0, "h": None, "saved": None}


def setup():
    time.sleep(1.0)
    W.focus_game(pid)
    inj._freeze(only=tid)
    h = dict((t, hh) for hh, t in inj.frozen)[tid]
    state["h"] = h
    old = inj._get_ctx(h)
    state["saved"] = ctypes.string_at(old.addr, W.CTX_SIZE)
    rsp = sc + W.STACK_OFF + W.STACK_SIZE - 0x108
    buf = bytearray(0x40)
    struct.pack_into("<Q", buf, 0, sc + W.STUB_OFF)
    inj._write(rsp, buf)
    new = W._Ctx(old.addr)
    new.set(W.OFF_RIP, PP)
    new.set(W.OFF_RSP, rsp)
    new.set(W.OFF_RAX, 0)
    new.set(W.OFF_RCX, SRES)
    new.set(W.OFF_RDX, ARR)
    ef = struct.unpack_from("<I", ctypes.string_at(new.addr, W.CTX_SIZE), OFF_EFLAGS)[0]
    struct.pack_into("<I", new.raw, new.off(OFF_EFLAGS), ef | 0x100)
    k32.SetThreadContext(h, new.ptr())
    chk = inj._get_ctx(h)
    print("  after SetThreadContext: RIP=%#x RSP=%#x RCX=%#x (want %#x) RDX=%#x (want %#x)"
          % (chk.get(W.OFF_RIP), chk.get(W.OFF_RSP), chk.get(W.OFF_RCX), SRES,
             chk.get(W.OFF_RDX), ARR))
    print("  EFlags now %#x (TF=%d)"
          % (struct.unpack_from("<I", ctypes.string_at(chk.addr, W.CTX_SIZE), OFF_EFLAGS)[0],
             bool(struct.unpack_from("<I", ctypes.string_at(chk.addr, W.CTX_SIZE), OFF_EFLAGS)[0] & 0x100)))
    state["setup"] = True
    k32.ResumeThread(h)


threading.Thread(target=setup, daemon=True).start()

ev = DEBUG_EVENT()
end = time.time() + 30
while time.time() < end:
    if not k32.WaitForDebugEvent(ctypes.byref(ev), 300):
        continue
    code, t = ev.dwDebugEventCode, ev.dwThreadId
    act = DBG_CONTINUE
    if code == EXC:
        rec = ev.u.Exception.ExceptionRecord
        c = rec.ExceptionCode & 0xFFFFFFFF
        addr = rec.ExceptionAddress or 0
        if c == 0x80000003:
            act = DBG_CONTINUE                        # attach breakpoint
        elif c == 0x80000004:
            h = state["h"]
            if h:
                cur = inj._get_ctx(h)
                state["steps"] += 1
                print("  step %-2d RIP=%#x RCX=%#x RDX=%#x R8=%#x RSP=%#x"
                      % (state["steps"], cur.get(W.OFF_RIP), cur.get(W.OFF_RCX),
                         cur.get(W.OFF_RDX), cur.get(W.OFF_R8), cur.get(W.OFF_RSP)))
                # re-arm the trap flag
                rbuf = ctypes.create_string_buffer(W.CTX_SIZE + 16)
                base = ctypes.addressof(rbuf)
                a2 = (base + 15) & ~15
                ctypes.memmove(a2, cur.addr, W.CTX_SIZE)
                ef = struct.unpack_from("<I", rbuf, a2 - base + OFF_EFLAGS)[0]
                struct.pack_into("<I", rbuf, a2 - base + OFF_EFLAGS, ef | 0x100)
                k32.SetThreadContext(h, ctypes.c_void_p(a2))
            if state["steps"] >= 8:
                print("  (stopping after 8 steps)")
                end = 0
        else:
            print("  EXC %-18s at %#x (hoi4+%#x)"
                  % (CODES.get(c, hex(c)), addr, addr - IMG if addr > IMG else 0))
            h = state["h"]
            if h:
                cur = inj._get_ctx(h)
                print("      RIP=%#x RSP=%#x RCX=%#x RDX=%#x R8=%#x"
                      % (cur.get(W.OFF_RIP), cur.get(W.OFF_RSP), cur.get(W.OFF_RCX),
                         cur.get(W.OFF_RDX), cur.get(W.OFF_R8)))
            act = DBG_EXCEPTION_NOT_HANDLED
            end = 0
    elif code == EPROC:
        print("  PROCESS EXIT")
        k32.ContinueDebugEvent(pid, t, DBG_CONTINUE)
        break
    k32.ContinueDebugEvent(pid, t, act)

# restore the thread if it is still alive
h = state["h"]
if h and state["saved"]:
    try:
        scratch = ctypes.create_string_buffer(state["saved"], W.CTX_SIZE)
        base = ctypes.addressof(scratch)
        a2 = (base + 15) & ~15
        ctypes.memmove(a2, state["saved"], W.CTX_SIZE)
        k32.SuspendThread(h)
        k32.SetThreadContext(h, ctypes.c_void_p(a2))
        k32.ResumeThread(h)
        print("thread context restored")
    except Exception as e:
        print("restore failed: %r" % e)
inj.detach()
try:
    k32.DebugActiveProcessStop(pid)
except Exception:
    pass
print("done")
