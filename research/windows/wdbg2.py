# -*- coding: utf-8 -*-
"""Attach a debugger, then run ONE injected `pp` as the first call.

Reports the fatal exception code/address and every thread's RIP at fault time,
plus which module each RIP belongs to.
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

EXC, CTHR, CPROC, ETHR, EPROC, LOAD, UNLOAD, OUTSTR, RIPEV = range(1, 10)


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


for fn, args, res in (
        ("DebugActiveProcess", [wt.DWORD], wt.BOOL),
        ("DebugActiveProcessStop", [wt.DWORD], wt.BOOL),
        ("WaitForDebugEvent", [ctypes.POINTER(DEBUG_EVENT), wt.DWORD], wt.BOOL),
        ("ContinueDebugEvent", [wt.DWORD, wt.DWORD, wt.DWORD], wt.BOOL)):
    f = getattr(k32, fn)
    f.argtypes = args
    f.restype = res

CODES = {0x80000003: "BREAKPOINT(int3)", 0xC0000005: "ACCESS_VIOLATION",
         0xC000001D: "ILLEGAL_INSTRUCTION", 0xC0000094: "DIV0",
         0xC00000FD: "STACK_OVERFLOW", 0xC0000409: "STACK_BUFFER_OVERRUN",
         0xE06D7363: "C++EXCEPTION", 0xC0000374: "HEAP_CORRUPTION",
         0xC0000602: "FAIL_FAST"}

pid = W.find_pid()
IMG = W.module_base(pid, "hoi4.exe")


class ME(ctypes.Structure):
    _fields_ = [("dwSize", wt.DWORD), ("th32ModuleID", wt.DWORD), ("th32ProcessID", wt.DWORD),
                ("GlblcntUsage", wt.DWORD), ("ProccntUsage", wt.DWORD),
                ("modBaseAddr", ctypes.c_void_p), ("modBaseSize", wt.DWORD),
                ("hModule", ctypes.c_void_p), ("szModule", ctypes.c_char * 256),
                ("szExePath", ctypes.c_char * 260)]


def modules():
    snap = k32.CreateToolhelp32Snapshot(0x0A, pid)
    me = ME()
    me.dwSize = ctypes.sizeof(me)
    out = []
    if k32.Module32First(snap, ctypes.byref(me)):
        while True:
            out.append((me.modBaseAddr or 0, me.modBaseSize, me.szModule.decode("mbcs", "replace")))
            if not k32.Module32Next(snap, ctypes.byref(me)):
                break
    k32.CloseHandle(snap)
    return out


MODS = modules()


def where(a):
    for base, size, nm in MODS:
        if base <= a < base + size:
            return "%s+%#x" % (nm, a - base)
    return "%#x" % a


print("pid %d  image %#x  modules %d" % (pid, IMG, len(MODS)))
if not k32.DebugActiveProcess(pid):
    raise SystemExit("DebugActiveProcess failed %d" % ctypes.get_last_error())
print("debugger attached")

res = {}


def worker():
    time.sleep(1.5)
    try:
        inj = W.Injector(pid)
        inj.attach()
        ALLOC = IMG + 0x30FD000
        sc = inj.scratch
        SRES, ARR, STRS = sc + 0x1000, sc + 0x1040, sc + 0x1080
        b = bytearray(0x400)
        struct.pack_into("<QQ", b, 0x18, 0, 0xF)
        struct.pack_into("<QIIQ", b, 0x40, STRS, 8, 1, ALLOC)
        b[0x80:0x81] = b"0"
        struct.pack_into("<QQ", b, 0x90, 1, 15)
        inj._write(sc + 0x1000, b)
        res["SRES"] = SRES
        res["ARR"] = ARR
        res["step"] = "calling"
        inj.call(IMG + 0x285FF0, (SRES, ARR), timeout=10.0, debug=True)
        res["step"] = "returned"
        res["ok"] = inj._read(SRES, 1)[0]
        inj.detach()
    except BaseException as e:      # noqa: BLE001
        res["error"] = repr(e)


th = threading.Thread(target=worker, daemon=True)
th.start()
ev = DEBUG_EVENT()
end = time.time() + 45
fatal = None
while time.time() < end and fatal is None:
    if not k32.WaitForDebugEvent(ctypes.byref(ev), 300):
        if res.get("step") == "returned" and not th.is_alive():
            break
        continue
    code, tid = ev.dwDebugEventCode, ev.dwThreadId
    if code == EXC:
        rec = ev.u.Exception.ExceptionRecord
        c = rec.ExceptionCode & 0xFFFFFFFF
        addr = rec.ExceptionAddress or 0
        first = ev.u.Exception.dwFirstChance
        print("  EXC tid=%-6d %-18s first=%d at %s" % (tid, CODES.get(c, hex(c)), first, where(addr)))
        if c != 0x80000003:
            hh = k32.OpenThread(0x0008 | 0x0010, False, tid)
            if hh:
                buf = ctypes.create_string_buffer(W.CTX_SIZE + 16)
                base = ctypes.addressof(buf)
                a2 = (base + 15) & ~15
                struct.pack_into("<I", buf, a2 - base + 0x30, 0x0010001F)
                if k32.GetThreadContext(hh, ctypes.c_void_p(a2)):
                    def g(o):
                        return struct.unpack_from("<Q", buf, a2 - base + o)[0]
                    print("      RIP=%#x RSP=%#x RCX=%#x RDX=%#x R8=%#x RAX=%#x"
                          % (g(W.OFF_RIP), g(W.OFF_RSP), g(W.OFF_RCX), g(W.OFF_RDX),
                             g(W.OFF_R8), g(W.OFF_RAX)))
                    print("      expected SRES=%#x ARR=%#x"
                          % (res.get("SRES", 0), res.get("ARR", 0)))
                k32.CloseHandle(hh)
        if c == 0x80000003 and addr and "ntdll" in where(addr):
            k32.ContinueDebugEvent(pid, tid, DBG_CONTINUE)      # attach breakpoint
        else:
            fatal = (c, addr, tid)
            k32.ContinueDebugEvent(pid, tid, DBG_EXCEPTION_NOT_HANDLED)
    elif code == EPROC:
        print("  PROCESS EXIT")
        k32.ContinueDebugEvent(pid, tid, DBG_CONTINUE)
        break
    else:
        k32.ContinueDebugEvent(pid, tid, DBG_CONTINUE)

if fatal:
    time.sleep(0.4)
    print("\n  thread RIPs at fault time:")
    for t in sorted(W.list_threads(pid)):
        h = k32.OpenThread(0x0008 | 0x0010 | 0x0002, False, t)
        if not h:
            continue
        ctx = W.Injector(pid)
        try:
            buf = ctypes.create_string_buffer(W.CTX_SIZE + 16)
            base = ctypes.addressof(buf)
            addr = (base + 15) & ~15
            c2 = W._Ctx.__new__(W._Ctx)
            c2.raw, c2.base, c2.addr = buf, base, addr
            struct.pack_into("<I", buf, addr - base + 0x30, 0x0010001F)
            if k32.GetThreadContext(h, ctypes.c_void_p(addr)):
                rip = struct.unpack_from("<Q", buf, addr - base + W.OFF_RIP)[0]
                mark = " <-- FaultingThread" if t == fatal[2] else ""
                print("    tid %-6d %s%s" % (t, where(rip), mark))
        finally:
            k32.CloseHandle(h)
else:
    print("  (no fatal exception captured)")

th.join(timeout=2)
print("result: %s" % res)
try:
    k32.DebugActiveProcessStop(pid)
    print("debugger detached")
except Exception as e:
    print("detach: %r" % e)

