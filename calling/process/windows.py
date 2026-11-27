"""Windows backend: raw memory access via ReadProcessMemory / VirtualQueryEx.

Status (2026-11-13): **read/write path and thread-hijack injection complete.**

Everything above this module (`mem.Proc`, `calling/api.py`, `game.py`, the 149 MCP
tools) is platform-agnostic and only sees this file's `Backend` / `Injector`
surface, so the read-only half of the stack (analysis, reports, combat
evaluation, staff *planning*) runs on Windows as soon as this file exists.

Two things had to be finished here before any tool that *acts* on the game could
work.  Both are done; the notes stay because they are the ABI reference:

  1. `Injector.call()` -- calling a game function with arguments.
     The Linux injector stops every thread and hijacks the main one; the
     Windows equivalent is the same idea with different primitives:

         OpenThread(THREAD_SUSPEND_RESUME | THREAD_GET_CONTEXT |
                    THREAD_SET_CONTEXT | THREAD_QUERY_INFORMATION, FALSE, tid)
         SuspendThread(hThread)
         GetThreadContext(hThread, &ctx)          # CONTEXT_FULL, AMD64
         VirtualAllocEx(hProcess, NULL, n, MEM_COMMIT, PAGE_EXECUTE_READWRITE)
         WriteProcessMemory(scratch, stub_and_args)
         ctx.Rsp  = scratch_stack_top   (16-byte aligned, minus 0x28 shadow space)
         ctx.Rip  = func_addr
         SetThreadContext(hThread, &ctx)
         ResumeThread(hThread)
         ... poll a done flag the stub writes, then restore ctx and resume ...

     Do NOT use CreateRemoteThread for this: a fresh thread has no game TLS, no
     scope stack and no frame pointers, and most Clausewitz functions fault on
     the first access. The hijack keeps the calling thread's identity.

     x64 CONTEXT offsets needed for that (see winnt.h):
         ContextFlags 0x30 (DWORD, set 0x10000B = CONTROL|INTEGER|FP)
         Rax..R15     0x78..0xF0 (DWORD64 each, in that order)
         Rip          0xF8
     Argument registers: rcx, rdx, r8, r9, then [rsp+0x28], [rsp+0x30], ...

  2. Every offset table. `calling/offsets.py`, `rtti.pkl`, `cmdctors.pkl`,
     `cmdvts.json` etc. were recovered from the Linux ELF (`hoi4g`) and are
     meaningless for `hoi4.exe`. The scanners in `research/` already exist; the
     RTTI one must be rewritten for **MSVC RTTI** (`??_7<class>@@6B@` vtables,
     `??_R4` CompleteObjectLocator, TypeDescriptor) instead of the Itanium
     `_ZTV...` / `.rela.dyn` scheme that only exists in ELF.

     Done: `calling/offsets_win.py` is the 1.19.5.0 table, `calling/rtti_win.py`
     builds the MSVC index, and `research/windows/` has the probes that derived
     both.  `calling/pecmd.py` recovers the command constructors on top of it.

Verified against the retail Steam build ("Operation Postern v1.19.5.0.7c6c (c184)",
hoi4.exe, Windows 11): pid 45332, image_base 0x7ff698620000, 116 modules, 25 712
committed regions (13 005 writable / 7.8 GB), ReadProcessMemory ~255 MB/s, full
writable scan ~1.3 s, PE header check OK (MZ, e_lfanew 0x150).

Run this file directly for a smoke test:

    python -m calling.process.windows            # or: python windows.py
"""

import ctypes
import ctypes.wintypes as wt

try:
    from . import winject as _winject
except ImportError:                      # direct script use
    import winject as _winject
import os
import threading
import time

# ---------------------------------------------------------------- Win32 bits
k32 = ctypes.WinDLL("kernel32", use_last_error=True)

TH32CS_SNAPPROCESS = 0x00000002
TH32CS_SNAPMODULE = 0x00000008
TH32CS_SNAPMODULE32 = 0x00000010
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_VM_OPERATION = 0x0008
PROCESS_VM_READ = 0x0010
PROCESS_VM_WRITE = 0x0020

MEM_COMMIT = 0x1000
MEM_IMAGE = 0x1000000
MEM_MAPPED = 0x40000

PAGE_NOACCESS = 0x01
PAGE_READONLY = 0x02
PAGE_READWRITE = 0x04
PAGE_WRITECOPY = 0x08
PAGE_EXECUTE = 0x10
PAGE_EXECUTE_READ = 0x20
PAGE_EXECUTE_READWRITE = 0x40
PAGE_EXECUTE_WRITECOPY = 0x80
PAGE_GUARD = 0x100

ERROR_PARTIAL_COPY = 299
ERROR_ACCESS_DENIED = 5


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wt.DWORD),
        ("cntUsage", wt.DWORD),
        ("th32ProcessID", wt.DWORD),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", wt.DWORD),
        ("cntThreads", wt.DWORD),
        ("th32ParentProcessID", wt.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wt.DWORD),
        ("szExeFile", ctypes.c_wchar * 260),
    ]


class MODULEENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wt.DWORD),
        ("th32ModuleID", wt.DWORD),
        ("th32ProcessID", wt.DWORD),
        ("GlblcntUsage", wt.DWORD),
        ("ProccntUsage", wt.DWORD),
        ("modBaseAddr", ctypes.POINTER(ctypes.c_byte)),
        ("modBaseSize", wt.DWORD),
        ("hModule", wt.HMODULE),
        ("szModule", ctypes.c_wchar * 256),
        ("szExePath", ctypes.c_wchar * 260),
    ]


class MEMORY_BASIC_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_void_p),
        ("AllocationBase", ctypes.c_void_p),
        ("AllocationProtect", wt.DWORD),
        ("__alignment1", wt.DWORD),
        ("RegionSize", ctypes.c_size_t),
        ("State", wt.DWORD),
        ("Protect", wt.DWORD),
        ("Type", wt.DWORD),
        ("__alignment2", wt.DWORD),
    ]


k32.CreateToolhelp32Snapshot.restype = wt.HANDLE
k32.CreateToolhelp32Snapshot.argtypes = [wt.DWORD, wt.DWORD]
k32.Process32FirstW.argtypes = [wt.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
k32.Process32NextW.argtypes = [wt.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
k32.Module32FirstW.argtypes = [wt.HANDLE, ctypes.POINTER(MODULEENTRY32W)]
k32.Module32NextW.argtypes = [wt.HANDLE, ctypes.POINTER(MODULEENTRY32W)]
k32.OpenProcess.restype = wt.HANDLE
k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
k32.CloseHandle.argtypes = [wt.HANDLE]
k32.ReadProcessMemory.argtypes = [wt.HANDLE, ctypes.c_uint64, wt.LPVOID, ctypes.c_size_t,
                                  ctypes.POINTER(ctypes.c_size_t)]
k32.WriteProcessMemory.argtypes = [wt.HANDLE, wt.LPVOID, wt.LPCVOID, ctypes.c_size_t,
                                   ctypes.POINTER(ctypes.c_size_t)]
k32.VirtualQueryEx.argtypes = [wt.HANDLE, wt.LPCVOID, ctypes.POINTER(MEMORY_BASIC_INFORMATION),
                               ctypes.c_size_t]
k32.VirtualQueryEx.restype = ctypes.c_size_t

_RPM = k32.ReadProcessMemory
_GET_LAST_ERR = ctypes.get_last_error

MAX_USER_ADDR = 0x7FFFFFFFFFFF


def _err(msg):
    return ctypes.WinError(ctypes.get_last_error(), msg)


# ---------------------------------------------------------------- the backend
def find_pid(name="hoi4.exe"):
    """PID of the first process whose image name matches `name` (case-insensitive).

    `config.EXE_NAME` is already "hoi4.exe" on Windows, but accept a bare "hoi4"
    as well so the same call sites keep working.
    """
    want = {name.lower(), name.lower() + ".exe"}
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == INVALID_HANDLE_VALUE:
        return None
    try:
        e = PROCESSENTRY32W()
        e.dwSize = ctypes.sizeof(e)
        ok = k32.Process32FirstW(snap, ctypes.byref(e))
        while ok:
            if e.szExeFile.lower() in want:
                return int(e.th32ProcessID)
            ok = k32.Process32NextW(snap, ctypes.byref(e))
        return None
    finally:
        k32.CloseHandle(snap)


def _modules(pid):
    """[(base, size, exe_name, full_path)] for every module of `pid`."""
    out = []
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32, pid)
    if snap == INVALID_HANDLE_VALUE:
        return out
    try:
        m = MODULEENTRY32W()
        m.dwSize = ctypes.sizeof(m)
        ok = k32.Module32FirstW(snap, ctypes.byref(m))
        while ok:
            out.append((int(ctypes.cast(m.modBaseAddr, ctypes.c_void_p).value or 0),
                        int(m.modBaseSize), m.szModule, m.szExePath))
            ok = k32.Module32NextW(snap, ctypes.byref(m))
    finally:
        k32.CloseHandle(snap)
    return out


def _protect_to_perms(protect):
    """Windows page protection -> the "rwx" string the rest of the stack expects."""
    p = protect & 0xFF
    readable = p in (PAGE_READONLY, PAGE_READWRITE, PAGE_WRITECOPY,
                     PAGE_EXECUTE_READ, PAGE_EXECUTE_READWRITE, PAGE_EXECUTE_WRITECOPY)
    writable = p in (PAGE_READWRITE, PAGE_WRITECOPY,
                     PAGE_EXECUTE_READWRITE, PAGE_EXECUTE_WRITECOPY)
    executable = p in (PAGE_EXECUTE, PAGE_EXECUTE_READ,
                       PAGE_EXECUTE_READWRITE, PAGE_EXECUTE_WRITECOPY)
    if protect & PAGE_GUARD:
        return "---g"
    return ("r" if readable else "-") + ("w" if writable else "-") + ("x" if executable else "-")


class _TlsReadState:
    """线程局部预分配读缓冲与标量槽，零堆分配供 Backend 复用。"""
    __slots__ = (
        "n", "n_ref", "buf64", "buf64_ref",
        "u8", "u8_ref", "u16", "u16_ref",
        "u32", "u32_ref", "i32", "i32_ref",
        "u64", "u64_ref", "i64", "i64_ref",
        "f32", "f32_ref", "f64", "f64_ref",
    )

    def __init__(self):
        self.n = ctypes.c_size_t(0)
        self.n_ref = ctypes.byref(self.n)
        self.buf64 = (ctypes.c_char * 64)()
        self.buf64_ref = ctypes.byref(self.buf64)
        self.u8 = ctypes.c_uint8(0)
        self.u8_ref = ctypes.byref(self.u8)
        self.u16 = ctypes.c_uint16(0)
        self.u16_ref = ctypes.byref(self.u16)
        self.u32 = ctypes.c_uint32(0)
        self.u32_ref = ctypes.byref(self.u32)
        self.i32 = ctypes.c_int32(0)
        self.i32_ref = ctypes.byref(self.i32)
        self.u64 = ctypes.c_uint64(0)
        self.u64_ref = ctypes.byref(self.u64)
        self.i64 = ctypes.c_int64(0)
        self.i64_ref = ctypes.byref(self.i64)
        self.f32 = ctypes.c_float(0.0)
        self.f32_ref = ctypes.byref(self.f32)
        self.f64 = ctypes.c_double(0.0)
        self.f64_ref = ctypes.byref(self.f64)


class Backend:
    """Raw memory access. The typed helpers live in mem.Proc, not here."""

    def __init__(self, pid=None, exe_name="hoi4.exe"):
        self.exe_name = exe_name
        self.pid = pid or find_pid(exe_name)
        if not self.pid:
            raise RuntimeError("hoi4 process not found (is hoi4.exe running?)")
        self.h = k32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_OPERATION |
                                 PROCESS_VM_READ | PROCESS_VM_WRITE, False, self.pid)
        if not self.h:
            err = ctypes.get_last_error()
            if err == ERROR_ACCESS_DENIED:
                raise RuntimeError(
                    "OpenProcess(PID %d) denied (error 5). Start this Python process as "
                    "Administrator: reading another process needs PROCESS_VM_READ." % self.pid)
            raise _err("OpenProcess(PID %d)" % self.pid)
        self.modules = _modules(self.pid)
        self.maps = self._read_maps()
        self.image_base, self.image_size = self._image_info(exe_name)
        # Instrumentation: every injection is counted so a read-only tool can be
        # proven to cost zero injections (hoi4_status reports this).
        self.call_count = 0
        self._tls = threading.local()

    def _tls_bufs(self):
        """线程局部预分配 ctypes 缓冲区与标量槽位，避免热路径高频小读重复堆分配。"""
        st = getattr(self._tls, "s", None)
        if st is None:
            st = _TlsReadState()
            self._tls.s = st
        return st

    # ---- lifecycle ----
    def close(self):
        try:
            if getattr(self, "h", None):
                k32.CloseHandle(self.h)
                self.h = None
        except Exception:
            pass

    def __del__(self):
        self.close()

    # ---- maps ----
    def _read_maps(self):
        out = []
        addr = 0
        mbi = MEMORY_BASIC_INFORMATION()
        size = ctypes.sizeof(mbi)
        import bisect as _bisect
        ranges = sorted((b, b + s, name, path) for b, s, name, path in self.modules)
        starts = [r[0] for r in ranges]
        while addr < MAX_USER_ADDR:
            got = k32.VirtualQueryEx(self.h, ctypes.c_void_p(addr), ctypes.byref(mbi), size)
            if not got:
                break
            base = int(mbi.BaseAddress or 0)
            rsize = int(mbi.RegionSize or 0)
            if rsize <= 0:
                break
            if mbi.State == MEM_COMMIT:
                path = ""
                idx = _bisect.bisect_right(starts, base) - 1
                if idx >= 0:
                    mb, me, name, mpath = ranges[idx]
                    if mb <= base < me:
                        path = mpath or name
                out.append(dict(start=base, end=base + rsize,
                                perms=_protect_to_perms(mbi.Protect),
                                fileoff=0, path=path, type=int(mbi.Type),
                                alloc=int(mbi.AllocationBase or 0)))
            addr = base + rsize
        return out

    def _image_info(self, exe_name):
        want = {exe_name.lower(), exe_name.lower() + ".exe"}
        for base, size, name, path in self.modules:
            if name.lower() in want or os.path.basename(path).lower() in want:
                return base, size
        if self.modules:
            return self.modules[0][0], self.modules[0][1]
        raise RuntimeError("no module list for pid %d (try running as Administrator)" % self.pid)

    def _image_base(self, exe_name):
        return self._image_info(exe_name)[0]

    def refresh_maps(self):
        """Re-read the region list; a long campaign maps new arenas on the fly."""
        try:
            self.modules = _modules(self.pid)
            self.maps = self._read_maps()
            self.image_base, self.image_size = self._image_info(self.exe_name)
        except Exception:
            pass
        return self.maps

    def regions(self):
        return self.maps

    # ---- raw access ----
    def read(self, addr, size):
        if size <= 0:
            return b""
        st = self._tls_bufs()
        if size <= 64:
            ok = _RPM(self.h, addr, st.buf64_ref, size, st.n_ref)
            got = st.n.value
            if not ok:
                err = _GET_LAST_ERR()
                if err == ERROR_PARTIAL_COPY and got:
                    return st.buf64[:got]
                raise OSError(err, "read %#x+%d" % (addr, size))
            return st.buf64[:got]
        buf = ctypes.create_string_buffer(size)
        ok = _RPM(self.h, addr, buf, size, st.n_ref)
        got = st.n.value
        if not ok:
            err = _GET_LAST_ERR()
            if err == ERROR_PARTIAL_COPY and got:
                return buf.raw[:got]
            raise OSError(err, "read %#x+%d" % (addr, size))
        return buf.raw[:got]

    def read_u8(self, addr):
        st = self._tls_bufs()
        if not _RPM(self.h, addr, st.u8_ref, 1, st.n_ref) or st.n.value < 1:
            raise OSError(_GET_LAST_ERR() or ERROR_PARTIAL_COPY, "read %#x+1" % addr)
        return st.u8.value

    def read_u16(self, addr):
        st = self._tls_bufs()
        if not _RPM(self.h, addr, st.u16_ref, 2, st.n_ref) or st.n.value < 2:
            raise OSError(_GET_LAST_ERR() or ERROR_PARTIAL_COPY, "read %#x+2" % addr)
        return st.u16.value

    def read_u32(self, addr):
        st = self._tls_bufs()
        if not _RPM(self.h, addr, st.u32_ref, 4, st.n_ref) or st.n.value < 4:
            raise OSError(_GET_LAST_ERR() or ERROR_PARTIAL_COPY, "read %#x+4" % addr)
        return st.u32.value

    def read_i32(self, addr):
        st = self._tls_bufs()
        if not _RPM(self.h, addr, st.i32_ref, 4, st.n_ref) or st.n.value < 4:
            raise OSError(_GET_LAST_ERR() or ERROR_PARTIAL_COPY, "read %#x+4" % addr)
        return st.i32.value

    def read_u64(self, addr):
        st = self._tls_bufs()
        if not _RPM(self.h, addr, st.u64_ref, 8, st.n_ref) or st.n.value < 8:
            raise OSError(_GET_LAST_ERR() or ERROR_PARTIAL_COPY, "read %#x+8" % addr)
        return st.u64.value

    def read_i64(self, addr):
        st = self._tls_bufs()
        if not _RPM(self.h, addr, st.i64_ref, 8, st.n_ref) or st.n.value < 8:
            raise OSError(_GET_LAST_ERR() or ERROR_PARTIAL_COPY, "read %#x+8" % addr)
        return st.i64.value

    def read_f32(self, addr):
        st = self._tls_bufs()
        if not _RPM(self.h, addr, st.f32_ref, 4, st.n_ref) or st.n.value < 4:
            raise OSError(_GET_LAST_ERR() or ERROR_PARTIAL_COPY, "read %#x+4" % addr)
        return st.f32.value

    def read_f64(self, addr):
        st = self._tls_bufs()
        if not _RPM(self.h, addr, st.f64_ref, 8, st.n_ref) or st.n.value < 8:
            raise OSError(_GET_LAST_ERR() or ERROR_PARTIAL_COPY, "read %#x+8" % addr)
        return st.f64.value

    def read_into(self, addr, buf, size=None):
        """直接读入可写缓冲区（如 bytearray/memoryview），避免每次分配与二次拷贝。"""
        n_req = len(buf) if size is None else int(size)
        if n_req <= 0:
            return 0
        if n_req > len(buf):
            raise ValueError("size %d exceeds buffer len %d" % (n_req, len(buf)))
        st = self._tls_bufs()
        dst = ctypes.byref(ctypes.c_char.from_buffer(buf))
        ok = _RPM(self.h, addr, dst, n_req, st.n_ref)
        got = int(st.n.value)
        if not ok:
            err = _GET_LAST_ERR()
            if err == ERROR_PARTIAL_COPY and got:
                return got
            raise OSError(err, "read %#x+%d" % (addr, n_req))
        return got

    def write(self, addr, data):
        buf = ctypes.create_string_buffer(bytes(data), len(data))
        n = ctypes.c_size_t(0)
        ok = k32.WriteProcessMemory(self.h, ctypes.c_void_p(addr), buf, len(data),
                                    ctypes.byref(n))
        if not ok:
            raise OSError(ctypes.get_last_error(), "write %#x+%d" % (addr, len(data)))
        return n.value


# ---------------------------------------------------------------- calls
# Share the exact CallError class raised by winject.Injector so callers
# catching process.CallError actually intercept thread-hijack failures.
CallError = _winject.CallError


class Injector(_winject.Injector):
    """Thread-hijack injector (see calling/process/winject.py).

    Same surface as `process/linux.Injector`, so `calling/api.py` is unchanged.
    `attach()` only opens handles -- it does not freeze the game, so reads taken
    inside `with g:` see a live (moving) process.  `call()` suspends one running
    thread, redirects RIP, waits for the done flag and restores the exact context.
    """

    def __init__(self, proc, scratch_size=0x2000):
        super().__init__(proc.pid)
        self.p = proc
        self.base = getattr(proc, "base", 0) or 0
        self.text = None

    def attach(self):
        super().attach()
        return []

    def detach(self):
        super().detach()

    def where(self):
        return "windows: thread hijack (tid %s)" % self.last_tid

    def getregs(self, tid=None):
        raise CallError("windows backend: getregs not implemented yet")

    def setregs(self, r, tid=None):
        raise CallError("windows backend: setregs not implemented yet")

    def getfpregs(self, tid=None):
        raise CallError("windows backend: getfpregs not implemented yet")

    def setfpregs(self, raw, tid=None):
        raise CallError("windows backend: setfpregs not implemented yet")

    def call(self, func_addr, args=(), scratch_writer=None, timeout=5.0, xmm_zero=True):
        if scratch_writer is not None:
            raise CallError("windows backend: scratch_writer is not supported in Injector.call; allocate via cmdwin.alloc/winject payload instead")
        res = super().call(func_addr, args, timeout=timeout)
        be = getattr(self.p, "be", None)
        if be is not None and hasattr(be, "call_count"):
            be.call_count += 1
        return res


# ---------------------------------------------------------------- smoke test
if __name__ == "__main__":
    import sys
    import time as _t

    print("platform :", sys.platform)
    pid = find_pid(os.environ.get("HOI4_EXE") or "hoi4.exe")
    print("pid      :", pid)
    if not pid:
        raise SystemExit("hoi4.exe not running")

    b = Backend(pid)
    print("image_base: %#x" % b.image_base)
    print("modules  :", len(b.modules))
    print("regions  :", len(b.maps), "committed")
    hdr = b.read(b.image_base, 0x40)
    print("MZ       :", hdr[:2], "| PE offset:", hex(int.from_bytes(hdr[0x3c:0x40], "little")))
    if hdr[:2] != b"MZ":
        raise SystemExit("image_base does not point at a PE header")

    # read bandwidth over the biggest readable region (rough sanity check)
    big = max((m for m in b.maps if "r" in m["perms"]), key=lambda m: m["end"] - m["start"])
    t0 = _t.time()
    n = 0
    addr = big["start"]
    while addr < big["end"] and n < (64 << 20):
        chunk = min(1 << 20, big["end"] - addr)
        try:
            n += len(b.read(addr, chunk))
        except OSError:
            break
        addr += chunk
    dt = _t.time() - t0
    print("read     : %.1f MB in %.3fs (%.1f MB/s)" % (n / 1e6, dt, n / 1e6 / max(dt, 1e-9)))
    print("OK - read path works")
