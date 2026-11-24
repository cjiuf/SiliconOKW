# -*- coding: utf-8 -*-
"""Windows x64 thread-hijack injection for HOI4 1.19.5.0  (generated from stage/winject.py).

Model (same as calling/process/linux.py): **freeze every thread, hijack the one that
owns the game window, restore, thaw.**  Freezing is what makes game-state mutation
safe: without it a worker can run the simulation while we are inside `pp`.

Verified on HOI4 1.19.5.0:
  * kernel32 sentinels (GetCurrentProcessId / GetTickCount64) round-trip
  * console `pp` really grants political power to the player country
"""
import ctypes
import ctypes.wintypes as wt
import json
import os
import struct
import tempfile
import time

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
user32 = ctypes.WinDLL("user32", use_last_error=True)

# HWNDs are pointer-sized: never let ctypes default these to c_int.
user32.GetForegroundWindow.restype = ctypes.c_void_p
user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
user32.SetForegroundWindow.restype = wt.BOOL
user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
user32.ShowWindow.restype = wt.BOOL

TH32CS_SNAPTHREAD = 0x00000004
TH32CS_SNAPMODULE = 0x00000008
TH32CS_SNAPMODULE32 = 0x00000010

THREAD_SUSPEND_RESUME = 0x0002
THREAD_GET_CONTEXT = 0x0008
THREAD_SET_CONTEXT = 0x0010
THREAD_QUERY_INFORMATION = 0x0040
THREAD_RIGHTS = (THREAD_SUSPEND_RESUME | THREAD_GET_CONTEXT |
                 THREAD_SET_CONTEXT | THREAD_QUERY_INFORMATION)

PROCESS_ALL = 0x1F0FFF
MEM_COMMIT = 0x1000
MEM_RESERVE = 0x2000
MEM_RELEASE = 0x8000
PAGE_EXECUTE_READWRITE = 0x40

CONTEXT_AMD64 = 0x00100000
CONTEXT_FULL = CONTEXT_AMD64 | 0x1 | 0x2 | 0x8          # 0x10000B
CONTEXT_ALL = CONTEXT_FULL | 0x4 | 0x10                 # 0x10001F

CTX_SIZE = 0x4D0
OFF_FLAGS = 0x30
OFF_RAX, OFF_RCX, OFF_RDX, OFF_R8, OFF_R9 = 0x78, 0x80, 0x88, 0xB8, 0xC0
OFF_RSP, OFF_RIP = 0x98, 0xF8

STUB_OFF, RES_OFF, FLAG_OFF = 0x00, 0x200, 0x210   # stub grew: stack args
STACK_OFF, STACK_SIZE = 0x1000, 0x8000           # 32 KiB; stacks grow down, so the
                                                 # stub at 0x00 can never be reached
#: control area cleared before every call; caller payload must live at or above this
CONTROL_SIZE = STACK_OFF + STACK_SIZE + 0x200      # 0x9200
#: payload offsets handed out by :meth:`Injector.payload`
PAYLOAD_OFF = CONTROL_SIZE                          # 0x9200
SCRATCH_SIZE = 0x20000

ARG_REGS = (OFF_RCX, OFF_RDX, OFF_R8, OFF_R9)


class CallError(RuntimeError):
    pass


class THREADENTRY32(ctypes.Structure):
    _fields_ = [("dwSize", wt.DWORD), ("cntUsage", wt.DWORD), ("th32ThreadID", wt.DWORD),
                ("th32OwnerProcessID", wt.DWORD), ("tpBasePri", wt.LONG),
                ("tpDeltaPri", wt.LONG), ("dwFlags", wt.DWORD)]


class MODULEENTRY32(ctypes.Structure):
    _fields_ = [("dwSize", wt.DWORD), ("th32ModuleID", wt.DWORD), ("th32ProcessID", wt.DWORD),
                ("GlblcntUsage", wt.DWORD), ("ProccntUsage", wt.DWORD),
                ("modBaseAddr", ctypes.c_void_p), ("modBaseSize", wt.DWORD),
                ("hModule", wt.HMODULE), ("szModule", ctypes.c_char * 256),
                ("szExePath", ctypes.c_char * 260)]


WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, ctypes.c_void_p, ctypes.c_void_p)

for _f, _a, _r in (
    (k32.OpenProcess, (wt.DWORD, wt.BOOL, wt.DWORD), ctypes.c_void_p),
    (k32.GetProcessTimes, (ctypes.c_void_p, ctypes.POINTER(wt.FILETIME),
                           ctypes.POINTER(wt.FILETIME), ctypes.POINTER(wt.FILETIME),
                           ctypes.POINTER(wt.FILETIME)), wt.BOOL),
    (k32.OpenThread, (wt.DWORD, wt.BOOL, wt.DWORD), ctypes.c_void_p),
    (k32.SuspendThread, (ctypes.c_void_p,), wt.DWORD),
    (k32.ResumeThread, (ctypes.c_void_p,), wt.DWORD),
    (k32.GetThreadContext, (ctypes.c_void_p, ctypes.c_void_p), wt.BOOL),
    (k32.SetThreadContext, (ctypes.c_void_p, ctypes.c_void_p), wt.BOOL),
    (k32.VirtualAllocEx, (ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, wt.DWORD, wt.DWORD),
     ctypes.c_void_p),
    (k32.VirtualFreeEx, (ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, wt.DWORD), wt.BOOL),
    (k32.WriteProcessMemory, (ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                              ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)), wt.BOOL),
    (k32.ReadProcessMemory, (ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                             ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)), wt.BOOL),
    (k32.FlushInstructionCache, (ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t), wt.BOOL),
    (k32.CreateToolhelp32Snapshot, (wt.DWORD, wt.DWORD), ctypes.c_void_p),
    (k32.Thread32First, (ctypes.c_void_p, ctypes.POINTER(THREADENTRY32)), wt.BOOL),
    (k32.Thread32Next, (ctypes.c_void_p, ctypes.POINTER(THREADENTRY32)), wt.BOOL),
    (k32.Module32First, (ctypes.c_void_p, ctypes.POINTER(MODULEENTRY32)), wt.BOOL),
    (k32.Module32Next, (ctypes.c_void_p, ctypes.POINTER(MODULEENTRY32)), wt.BOOL),
    (k32.GetThreadTimes, (ctypes.c_void_p, ctypes.POINTER(wt.FILETIME),
                          ctypes.POINTER(wt.FILETIME), ctypes.POINTER(wt.FILETIME),
                          ctypes.POINTER(wt.FILETIME)), wt.BOOL),
    (k32.CloseHandle, (ctypes.c_void_p,), wt.BOOL),
    (user32.EnumWindows, (WNDENUMPROC, ctypes.c_void_p), wt.BOOL),
    (user32.GetWindowThreadProcessId, (ctypes.c_void_p, ctypes.POINTER(wt.DWORD)), wt.DWORD),
    (user32.GetWindowTextLengthW, (ctypes.c_void_p,), ctypes.c_int),
    (user32.GetWindowTextW, (ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int), ctypes.c_int),
):
    _f.argtypes = _a
    _f.restype = _r


def _check(ok, what):
    if not ok:
        raise CallError("%s failed, GetLastError=%d" % (what, ctypes.get_last_error()))


def _ft(ft):
    return (ft.dwHighDateTime << 32) | ft.dwLowDateTime


def list_threads(pid):
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0)
    if not snap or snap == 0xFFFFFFFFFFFFFFFF:
        raise CallError("CreateToolhelp32Snapshot(t) failed")
    out = []
    try:
        te = THREADENTRY32()
        te.dwSize = ctypes.sizeof(THREADENTRY32)
        ok = k32.Thread32First(snap, ctypes.byref(te))
        while ok:
            if te.th32OwnerProcessID == pid:
                out.append(te.th32ThreadID)
            ok = k32.Thread32Next(snap, ctypes.byref(te))
    finally:
        k32.CloseHandle(snap)
    return out


def window_threads(pid):
    """[(tid, title)] for every top-level window owned by pid."""
    out = []

    def cb(h, _):
        p = wt.DWORD()
        t = user32.GetWindowThreadProcessId(ctypes.c_void_p(h), ctypes.byref(p))
        if p.value == pid:
            n = user32.GetWindowTextLengthW(ctypes.c_void_p(h))
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(ctypes.c_void_p(h), buf, n + 1)
            out.append((t, buf.value))
        return True

    user32.EnumWindows(WNDENUMPROC(cb), 0)
    return out


def main_thread(pid):
    """The thread that owns the game window (the game/render thread)."""
    cands = window_threads(pid)
    for t, title in cands:
        if "hearts of iron" in title.lower():
            return t
    return cands[0][0] if cands else None


def game_window(pid):
    """HWND of the game's top-level window, or None."""
    found = []

    def cb(h, _):
        p = wt.DWORD()
        user32.GetWindowThreadProcessId(ctypes.c_void_p(h), ctypes.byref(p))
        if p.value == pid:
            n = user32.GetWindowTextLengthW(ctypes.c_void_p(h))
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(ctypes.c_void_p(h), buf, n + 1)
            if "hearts of iron" in buf.value.lower():
                found.append(h)
                return False
        return True

    user32.EnumWindows(WNDENUMPROC(cb), 0)
    return found[0] if found else None


def focus_game(pid, settle=0.45):
    """Bring the game window to the foreground and let it settle.

    D3D loses the device when the window is not foreground; the render thread then
    sits inside RestoreDeviceObjects and must never be hijacked there.  Returns True
    if the window already was (or now is) the foreground window.
    """
    hwnd = game_window(pid)
    if not hwnd:
        return False
    fg = user32.GetForegroundWindow()
    if fg == hwnd:
        return True
    user32.ShowWindow(ctypes.c_void_p(hwnd), 9)          # SW_RESTORE
    user32.SetForegroundWindow(ctypes.c_void_p(hwnd))
    time.sleep(settle)
    return user32.GetForegroundWindow() == hwnd


def pick_idle_thread(pid, inj, image_base=None, image_size=0, skip=()):
    """A thread parked outside the game image -- i.e. blocked in a wait.

    Hijacking the window/render thread is fragile: it may be suspended inside a
    D3D critical section, and the moment the injected call allocates or touches
    the device the process faults.  A thread waiting in ntdll is idle by
    definition and is the safe place to borrow a stack/context.
    """
    if image_base is None:
        image_base = module_base(pid, "hoi4.exe")
    out = []
    for tid in sorted(list_threads(pid)):
        if tid in skip:
            continue
        h = k32.OpenThread(THREAD_GET_CONTEXT | THREAD_SUSPEND_RESUME, False, tid)
        if not h:
            continue
        try:
            ctx = inj._get_ctx(h)
            rip = ctx.get(OFF_RIP)
        finally:
            k32.CloseHandle(h)
        if not (image_base <= rip < image_base + (image_size or 0x38A3000)):
            out.append((tid, rip))
    return out


def module_base(pid, name):
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32, pid)
    if not snap or snap == 0xFFFFFFFFFFFFFFFF:
        return None
    try:
        me = MODULEENTRY32()
        me.dwSize = ctypes.sizeof(MODULEENTRY32)
        ok = k32.Module32First(snap, ctypes.byref(me))
        while ok:
            if me.szModule.decode("latin-1").lower() == name.lower():
                return me.modBaseAddr
            ok = k32.Module32Next(snap, ctypes.byref(me))
    finally:
        k32.CloseHandle(snap)
    return None


class _Ctx:
    def __init__(self, copy_from=None):
        self.raw = ctypes.create_string_buffer(CTX_SIZE + 16)
        self.addr = (ctypes.addressof(self.raw) + 15) & ~15
        self.base = ctypes.addressof(self.raw)
        if copy_from is not None:
            ctypes.memmove(self.addr, copy_from, CTX_SIZE)
        else:
            ctypes.memset(self.addr, 0, CTX_SIZE)

    def off(self, o):
        return self.addr - self.base + o

    def get(self, o):
        return struct.unpack_from("<Q", self.raw, self.off(o))[0]

    def set(self, o, v):
        struct.pack_into("<Q", self.raw, self.off(o), v & 0xFFFFFFFFFFFFFFFF)

    def ptr(self):
        return ctypes.c_void_p(self.addr)


# --------------------------------------------------------------- suspend log
# 2026-11-21 (P0-2): if this server process dies between SuspendThread and
# ResumeThread, the target keeps one thread with a suspend count of 1 forever and
# every later injector call that touches it hangs.  Record the TIDs we suspend in
# a small JSON file and let the next server process resume exactly those.
#
# Guard against PID REUSE: the record carries the target's creation time, and a
# record is only honoured when the pid AND the creation time still match.
def _default_frozen_file():
    """将孤儿线程恢复日志放在用户可写目录，避免源码目录只读时静默失效。"""
    override = os.environ.get("HOI4_STATE_DIR")
    if override:
        base = override
    else:
        local_app = os.environ.get("LOCALAPPDATA")
        base = os.path.join(local_app, "SiliconOKW") if local_app else tempfile.gettempdir()
    return os.path.join(base, "winject_frozen.json")


FROZEN_FILE = _default_frozen_file()


def _proc_start_time(pid):
    """Creation time of `pid` as float seconds (FILETIME 100 ns ticks since 1601)."""
    h = k32.OpenProcess(0x1000, False, int(pid))       # QUERY_LIMITED_INFORMATION
    if not h:
        return None
    try:
        c, e, k, u = (wt.FILETIME(), wt.FILETIME(), wt.FILETIME(), wt.FILETIME())
        if not k32.GetProcessTimes(h, ctypes.byref(c), ctypes.byref(e),
                                   ctypes.byref(k), ctypes.byref(u)):
            return None
        return ((c.dwHighDateTime << 32) | c.dwLowDateTime) / 1e7
    finally:
        k32.CloseHandle(h)


def _write_frozen(pid, tids):
    try:
        parent = os.path.dirname(FROZEN_FILE)
        if parent:
            os.makedirs(parent, exist_ok=True)
        data = {"pid": int(pid), "start": _proc_start_time(pid),
                "tids": sorted({int(t) for t in tids}), "ts": time.time()}
        tmp = FROZEN_FILE + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(data, fh)
        os.replace(tmp, FROZEN_FILE)
    except Exception:
        pass


def clear_frozen():
    try:
        os.remove(FROZEN_FILE)
    except OSError:
        pass


def read_frozen():
    try:
        with open(FROZEN_FILE) as fh:
            return json.load(fh)
    except Exception:
        return None


def resume_orphans(pid):
    """Resume threads a PREVIOUS server suspended before it was killed.

    Returns a report dict so the caller can log it.  Refuses when the pid does
    not match, or when the pid matches but the creation time differs (a recycled
    pid must never have another process's threads resumed).
    """
    rec = read_frozen()
    if not rec:
        return {"found": False}
    out = {"found": True, "pid": rec.get("pid"), "tids": rec.get("tids")}
    if int(rec.get("pid") or 0) != int(pid):
        out["skipped"] = "different pid"
        clear_frozen()
        return out
    now = _proc_start_time(pid)
    if rec.get("start") and now and abs(float(rec["start"]) - float(now)) > 2.0:
        out["skipped"] = "pid reused (creation time differs)"
        clear_frozen()
        return out
    resumed, failed = [], []
    for tid in rec.get("tids") or []:
        h = k32.OpenThread(THREAD_RIGHTS, False, int(tid))
        if not h:
            failed.append({"tid": tid, "error": "OpenThread failed"})
            continue
        try:
            if k32.ResumeThread(h) == 0xFFFFFFFF:
                failed.append({"tid": tid, "error": "ResumeThread failed"})
            else:
                resumed.append(int(tid))
        finally:
            k32.CloseHandle(h)
    out["resumed"] = resumed
    out["failed"] = failed
    clear_frozen()
    return out


class Injector:
    def __init__(self, pid, tid=None):
        self.pid = pid
        self.tid = tid
        self.h = None
        self.scratch = None
        self.call_count = 0
        self.last_tid = None
        self.dirty = False
        self.frozen = []          # [(handle, tid)] still suspended by us
        self._extra_handles = []  # hijacked threads we resumed: keep the handle open

    # ------------------------------------------------------------------ setup
    def attach(self):
        if self.h and self.scratch:
            return
        if not self.h:
            self.h = k32.OpenProcess(PROCESS_ALL, False, self.pid)
            if not self.h:
                raise CallError("OpenProcess(%d) failed, GetLastError=%d" %
                                (self.pid, ctypes.get_last_error()))
        if not self.scratch:
            self.scratch = k32.VirtualAllocEx(self.h, None, SCRATCH_SIZE,
                                              MEM_COMMIT | MEM_RESERVE, PAGE_EXECUTE_READWRITE)
            if not self.scratch:
                err = ctypes.get_last_error()
                k32.CloseHandle(self.h)
                self.h = None
                raise CallError("VirtualAllocEx failed, GetLastError=%d" % err)

    def detach(self):
        self._thaw()
        for h in self._extra_handles:
            k32.CloseHandle(h)
        self._extra_handles = []
        if self.dirty:
            import sys as _s
            print("[clausewitz] injector dirty - thread left inside game code, "
                  "scratch page leaked on purpose", file=_s.stderr)
            self.scratch = None
        elif self.scratch and self.h:
            k32.VirtualFreeEx(self.h, ctypes.c_void_p(self.scratch), 0, MEM_RELEASE)
            self.scratch = None
        if self.h:
            k32.CloseHandle(self.h)
            self.h = None

    def _freeze(self, only=None, all_threads=False):
        """Suspend threads.  ``only`` suspends just that thread (default); pass
        ``all_threads=True`` to stop the whole process first.

        Freezing every thread is the classic approach, but a thread parked inside
        an ntdll/heap critical section holds that lock forever; the hijacked thread
        then faults the moment the game allocates.  Suspending only the thread we
        are about to hijack avoids the whole class of failures.
        """
        if self.frozen:
            return
        tids = list_threads(self.pid) if all_threads else [only]
        for tid in tids:
            if tid is None:
                continue
            h = k32.OpenThread(THREAD_RIGHTS, False, tid)
            if not h:
                continue
            if k32.SuspendThread(h) == 0xFFFFFFFF:
                k32.CloseHandle(h)
                continue
            self.frozen.append((h, tid))
            self._sync_frozen()

    def _sync_frozen(self):
        """Keep the on-disk journal equal to the threads we still have suspended.

        2026-11-21: the journal was written on every freeze but only cleared by
        _thaw().  A SUCCESSFUL hijack resumes its thread at the end of call() and
        drops it from self.frozen without ever going through _thaw(), so the file
        kept listing a thread that was already running again.  Every place that
        mutates self.frozen must go through here.
        """
        tids = [t for _h, t in self.frozen]
        if tids:
            _write_frozen(self.pid, tids)
        else:
            clear_frozen()

    def _thaw(self):
        for h, _tid in self.frozen:
            k32.ResumeThread(h)
            k32.CloseHandle(h)
        self.frozen = []
        clear_frozen()

    # --------------------------------------------------------------------- io
    def _read(self, addr, n):
        buf = ctypes.create_string_buffer(n)
        got = ctypes.c_size_t()
        _check(k32.ReadProcessMemory(self.h, ctypes.c_void_p(addr), buf, n, ctypes.byref(got)),
               "ReadProcessMemory(%#x)" % addr)
        return buf.raw[:got.value]

    def _write(self, addr, data):
        data = bytes(data)
        buf = ctypes.create_string_buffer(data, len(data))
        got = ctypes.c_size_t()
        _check(k32.WriteProcessMemory(self.h, ctypes.c_void_p(addr), buf, len(data),
                                      ctypes.byref(got)), "WriteProcessMemory(%#x)" % addr)

    def _get_ctx(self, h):
        c = _Ctx()
        c.set(OFF_FLAGS, CONTEXT_ALL)
        _check(k32.GetThreadContext(h, c.ptr()), "GetThreadContext")
        return c

    # ------------------------------------------------------------------- call
    def call(self, func_addr, args=(), timeout=5.0, thread_id=None, debug=False,
             all_threads=False):
        self.attach()
        if self.dirty or self.pid in _DIRTY_PIDS:
            raise CallError(
                "injector is dirty: an earlier call timed out while the thread was "
                "inside game code. Give the game time to finish, then call "
                "winject.clear_dirty(%d) and re-attach." % self.pid)
        want = thread_id or self.tid or main_thread(self.pid)
        own = False
        if not self.frozen:
            # The D3D device must be live before the render thread is hijacked:
            # a non-foreground window sits inside RestoreDeviceObjects and dies.
            # 2026-11-19: focus_game() must not be *hoped* to work.  The two
            # crashes at 23:36 / 23:46 were exactly this -- the window sat behind
            # DSH/QQ, SetForegroundWindow did not take, the hijack landed inside
            # RestoreDeviceObjects and the process died with no minidump at all
            # (ReadProcessMemory(<heap>+0x210) GetLastError=299).  Refusing here
            # costs one retry; not refusing costs the campaign.
            if not focus_game(self.pid):
                raise CallError(
                    "game window is not in the foreground (pid %d): refusing to "
                    "hijack the render thread. A backgrounded window sits inside "
                    "RestoreDeviceObjects and the process dies on the call -- this "
                    "killed hoi4.exe twice on 2026-11-18. Bring 'Hearts of Iron IV "
                    "(DirectX 11)' to the front (and keep it there) and retry."
                    % self.pid)
            self._freeze(only=want, all_threads=all_threads)
            own = True

        sc = self.scratch
        stub, res, flag = sc + STUB_OFF, sc + RES_OFF, sc + FLAG_OFF
        rsp = sc + STACK_OFF + STACK_SIZE - 0x108
        assert rsp % 16 == 8

        # Choose the thread first: the stub has to embed its resume address.
        want = thread_id or self.tid or main_thread(self.pid)
        pool = dict((tid, h) for h, tid in self.frozen)
        cand = want if want in pool else (self.frozen[0][1] if self.frozen else None)
        if cand is None:
            raise CallError("no frozen thread to hijack")
        h = pool[cand]
        if debug:
            print("   hijack tid %d (window thread %s), %d frozen" %
                  (cand, main_thread(self.pid), len(self.frozen)))

        try:
            old = self._get_ctx(h)
            saved = ctypes.string_at(old.addr, CTX_SIZE)
        except Exception:
            if own:
                self._thaw()
            raise

        # Zero only the control area (stub / result / flag / stack).  Callers may
        # park ABI payloads above CONTROL_SIZE and must not have them wiped.
        buf = bytearray(CONTROL_SIZE)
        # The stub loads the arguments itself.  Setting RCX/RDX/R8/R9 through
        # SetThreadContext is not reliable here: the values read back correctly
        # right after the call but the thread starts with them clobbered (RCX ==
        # entry RIP, R8 == entry RSP).  Code we control avoids the question.
        a = [args[i] if i < len(args) else 0 for i in range(4)]
        # Win64 passes the 5th argument onwards on the stack: at the CALL SITE
        # arg4 must sit at [rsp+0x20], arg5 at [rsp+0x28], ... so the callee sees
        # them at [rsp+0x28] after the CPU pushes the return address.  Without
        # this, calling a constructor that takes more than four arguments (every
        # command ctor with an optional flag) was impossible.
        extra = max(0, len(args) - 4)
        frame = 0x28 + 8 * extra
        if frame % 16 != 8:          # entry RSP % 16 == 8, CALL needs RSP % 16 == 0
            frame += 8
        stack_args = b""
        for i in range(extra):
            # Full 64-bit store: a pointer passed as the 6th argument (the scope
            # block handed to the diplomatic action constructors) does not fit in
            # an imm32 and was being truncated to its low half.
            disp = 0x20 + 8 * i
            if disp < 0x80:
                store = b"\x48\x89\x44\x24" + bytes([disp])
            else:
                store = b"\x48\x89\x84\x24" + struct.pack("<I", disp)
            stack_args += (b"\x48\xb8" + struct.pack("<Q", int(args[4 + i]) & 0xFFFFFFFFFFFFFFFF)
                           + store)
        code = (b"\x48\x81\xec" + struct.pack("<I", frame)           # sub  rsp, frame
                + b"\x48\xb9" + struct.pack("<Q", a[0])              # mov  rcx, a0
                + b"\x48\xba" + struct.pack("<Q", a[1])              # mov  rdx, a1
                + b"\x49\xb8" + struct.pack("<Q", a[2])              # mov  r8,  a2
                + b"\x49\xb9" + struct.pack("<Q", a[3])              # mov  r9,  a3
                + stack_args
                + b"\x48\xb8" + struct.pack("<Q", func_addr)         # mov  rax, fn
                + b"\xff\xd0"                                        # call rax
                + b"\x48\xa3" + struct.pack("<Q", res)               # mov  [res], rax
                + b"\x48\xb8" + struct.pack("<Q", flag)              # mov  rax, flag
                + b"\xc7\x00\x01\x00\x00\x00"                        # mov  dword[rax], 1
                # Park here; the injector restores the WHOLE context.  Do not be
                # tempted to resume the thread from inside the stub ("mov rsp,
                # orig_rsp / jmp orig_rip"): the hijacked thread is stopped in the
                # middle of unrelated code, so its caller-saved registers are live.
                # The target call clobbers rcx/rdx/r8-r11 and the thread then
                # resumes with them destroyed (measured: render thread wedged and
                # the whole process stopped responding).
                + b"\xeb\xfe")                                       # jmp  $
        assert len(code) < RES_OFF, "stub (%d bytes) would overwrite the result slot" % len(code)
        buf[STUB_OFF:STUB_OFF + len(code)] = code
        STUB_LEN = len(code)
        struct.pack_into("<Q", buf, rsp - sc, 0)
        self._write(sc, buf)
        k32.FlushInstructionCache(self.h, ctypes.c_void_p(sc), SCRATCH_SIZE)
        self._write(flag, b"\0\0\0\0")
        self._write(res, b"\0" * 8)

        # choose a thread that is already frozen by us
        new = _Ctx(old.addr)
        new.set(OFF_RIP, stub)
        new.set(OFF_RSP, rsp)
        new.set(OFF_RAX, 0)
        for i, off in enumerate(ARG_REGS):
            new.set(off, args[i] if i < len(args) else 0)
        _check(k32.SetThreadContext(h, new.ptr()), "SetThreadContext")
        # Verify the context actually landed before letting the thread run: a
        # half-applied context executes game code with stale registers and takes
        # the process down with an access violation inside the callee.
        chk = self._get_ctx(h)
        want_r = (args[0] if len(args) > 0 else 0,
                  args[1] if len(args) > 1 else 0,
                  args[2] if len(args) > 2 else 0,
                  args[3] if len(args) > 3 else 0)
        got_r = tuple(chk.get(o) for o in ARG_REGS)
        if chk.get(OFF_RIP) != stub or chk.get(OFF_RSP) != rsp:
            k32.SetThreadContext(h, _Ctx(saved).ptr())
            if own:
                self._thaw()
            raise CallError("context did not take effect: rip=%#x rsp=%#x args=%s want=%s"
                            % (chk.get(OFF_RIP), chk.get(OFF_RSP),
                               tuple("%#x" % v for v in got_r),
                               tuple("%#x" % v for v in want_r)))
        _check(k32.ResumeThread(h) != 0xFFFFFFFF, "ResumeThread")
        # The thread runs again, so it is not ours to thaw any more.
        self.frozen = [(hh, tt) for hh, tt in self.frozen if tt != cand]
        self._extra_handles.append(h)
        # ... and the journal must stop claiming we still hold it.
        self._sync_frozen()

        timed_out_in_stub = False
        timed_out_rip = 0
        t0 = time.time()
        while time.time() - t0 < timeout:
            if self._read(flag, 4) != b"\0\0\0\0":
                break
            time.sleep(0.001)
        else:
            # Give a slow command a last chance: `tag` switches the player country
            # and takes ~20 s, far past any sane polling budget.
            grace = time.time() + max(2.0, timeout * 0.25)
            while time.time() < grace:
                if self._read(flag, 4) != b"\0\0\0\0":
                    break
                time.sleep(0.005)
            else:
                # Still inside the call.  Two very different situations:
                #   RIP inside the stub -> restoring the whole context is safe and frees it.
                #   RIP anywhere else -> it is executing game code; restoring would
                #     rewind it, suspending it would freeze the game.  Leave it
                #     alone and refuse further calls on this process until the
                #     caller has given the game time to finish.
                cur = self._get_ctx(h)
                rip = cur.get(OFF_RIP)
                if not (stub <= rip < stub + STUB_LEN):
                    self.dirty = True
                    _DIRTY_PIDS.add(self.pid)
                    raise CallError(
                        "timeout %.1fs: tid %d still inside %#x (RIP=%#x); no context "
                        "was restored and the thread was left running. Wait for the "
                        "game to settle before the next call."
                        % (timeout, cand, func_addr, rip))
                timed_out_in_stub = True
                timed_out_rip = rip

        # Suspend the thread while it is parked on `jmp $` before restoring CONTEXT,
        # per Win32 SetThreadContext contract, then resume and close the handle so
        # _extra_handles does not leak across long sessions.
        _check(k32.SuspendThread(h) != 0xFFFFFFFF, "SuspendThread(restore)")
        completed_flag = False
        parked_at_jmp = False
        try:
            if timed_out_in_stub:
                # 在线程彻底停稳后复核 flag 与 RIP：仅当已置位 flag 或停在末尾 jmp $
                # (stub + STUB_LEN - 2) 才说明 call rax 与 mov [res], rax 已执行完成。
                completed_flag = self._read(flag, 4) != b"\0\0\0\0"
                stopped_rip = self._get_ctx(h).get(OFF_RIP)
                parked_at_jmp = (stopped_rip == stub + STUB_LEN - 2)
                timed_out_rip = stopped_rip
            _check(k32.SetThreadContext(h, _Ctx(saved).ptr()), "SetThreadContext(restore)")
        finally:
            k32.ResumeThread(h)
        if h in self._extra_handles:
            self._extra_handles.remove(h)
            k32.CloseHandle(h)

        if timed_out_in_stub and not (completed_flag or parked_at_jmp):
            if own:
                self._thaw()
            raise CallError(
                "timeout %.1fs: tid %d was still inside stub before completion "
                "(RIP=%#x, flag=0); context restored without returning stale 0."
                % (timeout, cand, timed_out_rip))

        self.call_count += 1
        self.last_tid = cand
        out = struct.unpack("<Q", self._read(res, 8))[0]
        if own:
            self._thaw()
        return out

    def call_i64(self, func_addr, args=(), **kw):
        v = self.call(func_addr, args, **kw)
        return v - (1 << 64) if v >= (1 << 63) else v

    # ------------------------------------------------------- console commands
    def prepare_console_args(self, strings, alloc, off=PAYLOAD_OFF):
        """Lay out ``SResult`` + ``CPdxArray<CString>`` + the strings in scratch.

        Verified against hoi4 1.19.5.0: a console command is
        ``SResult* fn(SResult* out, const CPdxArray<CString>* args)`` where
        ``SResult`` is ``{bool ok; std::string msg;}`` (0x28) and ``CPdxArray``
        is ``{void* data; int32 cap; int32 size; IAllocator* alloc}`` with the
        element count at **+0x0C**.  ``CString`` is MSVC ``std::string``
        (0x20, SSO up to 15 bytes, length at +0x10, capacity at +0x18).

        Arguments longer than 15 bytes must go to a real buffer; an earlier
        version truncated them to 15 and wrote the untruncated length, which
        would have made e.g. ``add_equipment 600 transport_plane_equipment_1``
        read past its own SSO.  Long strings are parked after the slot array,
        still inside the scratch page -- no operator new, no heap block to leak.

        Returns ``(sres_addr, arr_addr)``; pass them as the two call arguments.
        """
        sc = self.scratch
        sres, arr, strs = sc + off, sc + off + 0x40, sc + off + 0x80
        n = len(strings)
        tail = (0x80 + 0x20 * max(1, n) + 0xF) & ~0xF
        buf = bytearray(tail)
        struct.pack_into("<QQ", buf, 0x18, 0, 0xF)              # msg = "" , cap 15
        struct.pack_into("<QIIQ", buf, 0x40, strs, max(1, n), n, alloc)
        for i, s in enumerate(strings):
            raw = s.encode("utf-8")
            o = 0x80 + i * 0x20
            if len(raw) <= 15:
                buf[o:o + len(raw)] = raw
                struct.pack_into("<QQ", buf, o + 0x10, len(raw), 15)
            else:
                hp = sc + off + len(buf)
                struct.pack_into("<Q", buf, o, hp)
                struct.pack_into("<QQ", buf, o + 0x10, len(raw), len(raw))
                buf.extend(raw + b"\0")
        self._write(sres, buf)
        return sres, arr

    def run_console_cmd(self, func_addr, strings, alloc, timeout=30.0):
        """Call one console command implementation and return ``(ok, message)``."""
        sres, arr = self.prepare_console_args(strings, alloc)
        self.call(func_addr, (sres, arr), timeout=timeout)
        ok = self._read(sres, 1)[0]
        head = self._read(sres + 8, 0x20)
        n, cap = struct.unpack_from("<QQ", head, 0x10)
        if cap <= 15:
            msg = self._read(sres + 8, n)
        else:
            msg = self._read(struct.unpack_from("<Q", head, 0)[0], n)
        return ok, msg.decode("utf-8", "replace")


#: pids with a thread still inside a timed-out call.  A per-Injector flag is not
#: enough: every console command attaches a fresh injector, so a second call would
#: happily hijack the same thread again while it is still inside the first one --
#: that nests stubs and mangles the stack (seen: render thread wedged).
_DIRTY_PIDS = set()


def clear_dirty(pid=None):
    """Forget the timed-out state for `pid` (or every process). Returns cleared pids."""
    if pid is None:
        cleared = sorted(_DIRTY_PIDS)
        _DIRTY_PIDS.clear()
        return cleared
    p_int = int(pid)
    was = p_int in _DIRTY_PIDS
    _DIRTY_PIDS.discard(p_int)
    return [p_int] if was else []


def dirty_status(pid=None):
    """Read-only inspection of dirty injector pids."""
    pids = sorted(_DIRTY_PIDS)
    return {
        "dirty_pids": pids,
        "is_dirty": (int(pid) in _DIRTY_PIDS) if pid is not None else bool(pids),
    }


def find_pid(name="hoi4.exe"):
    import subprocess
    out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq %s" % name, "/FO", "CSV", "/NH"],
                         capture_output=True, text=True).stdout
    for line in out.splitlines():
        parts = [x.strip('"') for x in line.split('","')]
        if len(parts) > 1 and parts[0].lower() == name.lower():
            return int(parts[1])
    return None
