"""Linux process backend: process_vm_readv/writev for memory, ptrace for calls.

This is the verified path; every offset table in the project was recovered
against this build.
"""
import ctypes, os, re, struct

libc = ctypes.CDLL("libc.so.6", use_errno=True)


class iovec(ctypes.Structure):
    _fields_ = [("iov_base", ctypes.c_void_p), ("iov_len", ctypes.c_size_t)]


libc.process_vm_readv.argtypes = [ctypes.c_int, ctypes.POINTER(iovec), ctypes.c_ulong,
                                  ctypes.POINTER(iovec), ctypes.c_ulong, ctypes.c_ulong]
libc.process_vm_readv.restype = ctypes.c_ssize_t
libc.process_vm_writev.argtypes = libc.process_vm_readv.argtypes
libc.process_vm_writev.restype = ctypes.c_ssize_t


def find_pid(name="hoi4"):
    for p in os.listdir("/proc"):
        if not p.isdigit():
            continue
        try:
            with open("/proc/%s/cmdline" % p, "rb") as f:
                cl = f.read().split(b"\0")[0].decode("utf-8", "replace")
        except OSError:
            continue
        if cl.rsplit("/", 1)[-1] == name:
            return int(p)
    return None


class Backend:
    """Raw memory access. The typed helpers live in mem.Proc, not here."""

    def __init__(self, pid=None, exe_name="hoi4"):
        self.pid = pid or find_pid(exe_name)
        if not self.pid:
            raise RuntimeError("hoi4 process bulunamadi")
        self.maps = self._read_maps()
        self.exe_name = exe_name
        self.image_base = self._image_base(exe_name)
        # Instrumentation: every ptrace stop/start injection is counted so a
        # read-only tool can be proven to cost zero injections (hoi4_status).
        self.call_count = 0

    def _read_maps(self):
        out = []
        with open("/proc/%d/maps" % self.pid) as f:
            for line in f:
                m = re.match(r"([0-9a-f]+)-([0-9a-f]+) (\S{4}) ([0-9a-f]+) \S+ \d+\s*(.*)", line)
                if not m:
                    continue
                out.append(dict(start=int(m.group(1), 16), end=int(m.group(2), 16),
                                perms=m.group(3), fileoff=int(m.group(4), 16),
                                path=m.group(5).strip()))
        return out

    def _image_base(self, exe_name):
        cands = [m for m in self.maps if m["path"].endswith("/" + exe_name)]
        if not cands and exe_name != "hoi4":
            # symlinked launch (hoi4g/hoi4n -> hoi4): the maps show the real file
            cands = [m for m in self.maps if m["path"].endswith("/hoi4")]
        if not cands:
            raise RuntimeError("no hoi4 image mapping found")
        return min(m["start"] - m["fileoff"] for m in cands)

    def refresh_maps(self):
        """B4 (review 2026-11-07): `self.maps` was read ONCE at attach time.

        A long campaign mmaps new heap arenas (PAX/PDX allocators), so every
        object allocated in them was invisible to the scans. Re-read the map and
        keep the image base consistent.
        """
        try:
            self.maps = self._read_maps()
        except OSError:
            return self.maps
        exe = getattr(self, "exe_name", None)
        if exe:
            try:
                self.image_base = self._image_base(exe)
            except Exception:
                pass
        return self.maps

    def read(self, addr, size):
        buf = ctypes.create_string_buffer(size)
        l = iovec(ctypes.cast(buf, ctypes.c_void_p), size)
        r = iovec(ctypes.c_void_p(addr), size)
        n = libc.process_vm_readv(self.pid, ctypes.byref(l), 1, ctypes.byref(r), 1, 0)
        if n < 0:
            raise OSError(ctypes.get_errno(), "read %#x+%d" % (addr, size))
        return buf.raw[:n]

    def write(self, addr, data):
        buf = ctypes.create_string_buffer(data, len(data))
        l = iovec(ctypes.cast(buf, ctypes.c_void_p), len(data))
        r = iovec(ctypes.c_void_p(addr), len(data))
        n = libc.process_vm_writev(self.pid, ctypes.byref(l), 1, ctypes.byref(r), 1, 0)
        if n < 0:
            raise OSError(ctypes.get_errno(), "write %#x+%d" % (addr, len(data)))
        return n

    def regions(self):
        return self.maps


# ---------------------------------------------------------------- calls
import signal, time

libc.ptrace.restype = ctypes.c_long
libc.ptrace.argtypes = [ctypes.c_long, ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p]

PTRACE_TRACEME=0; PTRACE_PEEKDATA=2; PTRACE_POKEDATA=5; PTRACE_CONT=7
PTRACE_KILL=8; PTRACE_SINGLESTEP=9; PTRACE_GETREGS=12; PTRACE_SETREGS=13
PTRACE_GETFPREGS=14; PTRACE_SETFPREGS=15
PTRACE_ATTACH=16; PTRACE_DETACH=17; PTRACE_SETOPTIONS=0x4200
PTRACE_SEIZE=0x4206; PTRACE_INTERRUPT=0x4207

REG_FIELDS = ["r15","r14","r13","r12","rbp","rbx","r11","r10","r9","r8","rax","rcx",
              "rdx","rsi","rdi","orig_rax","rip","cs","eflags","rsp","ss",
              "fs_base","gs_base","ds","es","fs","gs"]

class Regs(ctypes.Structure):
    _fields_ = [(n, ctypes.c_ulonglong) for n in REG_FIELDS]
    def __repr__(self):
        return "rip=%#x rsp=%#x rax=%#x rdi=%#x" % (self.rip, self.rsp, self.rax, self.rdi)

def _ptrace(req, pid, addr=0, data=0):
    ctypes.set_errno(0)
    r = libc.ptrace(req, pid, ctypes.c_void_p(addr), ctypes.c_void_p(data))
    e = ctypes.get_errno()
    if r == -1 and e != 0:
        raise OSError(e, "ptrace(%d, %d): %s" % (req, pid, os.strerror(e)))
    return r


class CallError(Exception): pass


# ---------------------------------------------------------- 1.19.4.1 CALL WHITELIST
# An injected call runs inside the LIVE game. A stale 1.19.2 address (or a guessed
# one) is a SIGSEGV in the game process, so the lowest layer refuses anything that
# was not recovered against THIS build. Higher layers add their statically verified
# constructors through api.allow_call().
VERIFIED_IMAGE_CALLS = {
    0x510950,    # operator new(size_t)                      _Znwm@plt
    0x510260,    # operator delete(void*)                    _ZdlPv@plt
    0x2F212D0,   # CCommandQueue::Post(queue, cmd, force)
    0x20AE0A0,   # CCommand::Run(cmd, std::string* err)
    0x21928A0,   # CInGameIdler::GetCommandQueue()           idler->vt[0x90]
    0x2046880,   # app->vt[0x90] -> *(app+0x380)             returns the queue
    0x23151C0,   # COrderGroupCommand ctor                   hoi4_create_army
    0x23167D0,   # CArmyGroupCommand ctor                    hoi4_create_army_group
    0x2332940,   # CSetArmyLeaderCommand ctor                hoi4_assign_commander
    0x2325E10,   # COrderNewRootCommand ctor                 (front line: DISABLED)
    0x253A600,   # CMoveCommand ctor                         route A, per-division move
    0x1CF6ED0,   # CSetNationalFocusCommand ctor             base-init reference
}


def allow_call(offset):
    """Register one more statically verified image offset (called by api.allow_call)."""
    VERIFIED_IMAGE_CALLS.add(int(offset))


class Injector:
    """Stop every thread, hijack the main one to make the call, then restore it."""

    def __init__(self, proc, scratch_size=0x40000):
        self.p = proc
        self.pid = proc.pid
        self.scratch_size = scratch_size
        self.tids = []
        self.saved = None
        self.base = getattr(proc, "base", 0) or 0
        self.text = None
        for m in getattr(proc, "maps", []) or []:
            if m["path"].endswith("/hoi4") and "x" in m["perms"]:
                if self.text is None or (m["end"] - m["start"]) > (self.text["end"] - self.text["start"]):
                    self.text = m

    # ---- thread management ----
    def _all_tids(self):
        return sorted(int(t) for t in os.listdir("/proc/%d/task" % self.pid))

    def attach(self):
        self.tids = self._all_tids()
        ok = []
        for t in self.tids:
            try:
                _ptrace(PTRACE_SEIZE, t, 0, 0)
                _ptrace(PTRACE_INTERRUPT, t, 0, 0)
                os.waitpid(t, os.WUNTRACED | 0x40000000)  # __WALL
                ok.append(t)
            except OSError:
                pass
        self.tids = ok
        if self.pid not in self.tids:
            self.detach()
            raise CallError("ana thread durdurulamadi")
        return self.tids

    def detach(self):
        for t in list(self.tids):
            try: _ptrace(PTRACE_DETACH, t, 0, 0)
            except OSError: pass
        self.tids = []

    # ---- register ----
    def getregs(self, tid=None):
        r = Regs()
        _ptrace(PTRACE_GETREGS, tid or self.pid, 0, ctypes.addressof(r))
        return r

    def setregs(self, r, tid=None):
        _ptrace(PTRACE_SETREGS, tid or self.pid, 0, ctypes.addressof(r))

    # C5 (review 2026-11-07): PTRACE_GETREGS only covers the general-purpose
    # registers. The main thread of a 3D engine is mid-way through SSE code when
    # we hijack it, so xmm0-15 / mxcsr must be saved and restored too, otherwise
    # every injected call silently corrupts a float computation in flight.
    def getfpregs(self, tid=None):
        buf = ctypes.create_string_buffer(512)     # x86-64 user_fpregs_struct
        _ptrace(PTRACE_GETFPREGS, tid or self.pid, 0, ctypes.addressof(buf))
        return buf.raw

    def setfpregs(self, raw, tid=None):
        if not raw:
            return
        buf = ctypes.create_string_buffer(bytes(raw), 512)
        _ptrace(PTRACE_SETFPREGS, tid or self.pid, 0, ctypes.addressof(buf))

    def where(self):
        """Where is the main thread right now? Being in a syscall is relatively safe."""
        try:
            with open("/proc/%d/task/%d/syscall" % (self.pid, self.pid)) as _f:
                s = _f.read().split()
            return s[0]
        except OSError:
            return "?"

    # ---- calling ----
    def _in_image_text(self, rip):
        return bool(self.text and self.text["start"] <= rip < self.text["end"])

    # Blocking wait syscalls: the thread is asleep, not holding the ptmalloc arena.
    #   7 select, 23 select, 35 nanosleep, 230 clock_nanosleep,
    #   232 epoll_wait, 271 ppoll, 281 epoll_pwait
    # 202 (futex) was REMOVED on purpose: a thread parked on futex is usually
    # inside glibc's malloc()/free() waiting for the ptmalloc main_arena lock (or
    # inside the loader/llvmpipe).  Hijacking it there and re-entering operator
    # new on the same thread corrupts the heap: "free(): invalid pointer" /
    # "stack smashing detected" after 10-20 minutes of play.  Being in futex is
    # NOT evidence that the thread is safe to steal.
    SAFE_WAIT_SYSCALLS = {7, 23, 24, 35, 230, 232, 271, 281}

    def _syscall_no(self):
        try:
            with open("/proc/%d/task/%d/syscall" % (self.pid, self.pid)) as _f:
                tok = _f.read().split()
            return int(tok[0]) if tok and tok[0].lstrip("-").isdigit() else None
        except (OSError, ValueError):
            return None

    # futex(202) two-phase rule (2026-11-02 work order):
    #   * while the game RUNS the main thread parks in futex inside malloc/free
    #     (ptmalloc arena lock); hijacking it there re-enters operator new on
    #     the same thread and corrupts the heap ("free(): invalid pointer" /
    #     "stack smashing"), so futex is REFUSED for the first FUTEX_GRACE tries;
    #   * if we keep landing on futex after FUTEX_GRACE tries the game is
    #     PAUSED: the main thread is idle in the frame limiter's futex (no sim,
    #     no worker threads competing for the malloc lock).  A paused game is
    #     otherwise 100% uncommandable, so from then on futex is allowed --
    #     provided the return address on top of stack is NOT in malloc/free.
    FUTEX_SYSCALL = 202
    FUTEX_GRACE = 15          # first 15 tries refuse futex (~45 ms grace)

    _MALLOC_SYMS = ("malloc", "__libc_malloc", "free", "__libc_free",
                    "cfree", "calloc", "realloc")
    # libc wait code that is SAFE to hijack even though it sits on a futex:
    # pthread_join / cond wait / direct futex syscall trampolines.
    _SAFE_WAIT_SYMS = ("pthread_join", "pthread_timedjoin", "pthread_clockjoin",
                       "__pthread_clockjoin_ex", "pthread_cond_wait",
                       "pthread_cond_timedwait", "pthread_cond_clockwait",
                       "__futex_abstimed_wait", "__futex_wait", "futex",
                       "sem_wait", "sem_timedwait", "sem_clockwait")
    # Anonymous local nptl helpers (no dynsym name) verified safe on THIS libc
    # build (Debian 2.36): 0x8f340-0x8f39f = join/exit futex wait (waits for a
    # thread tid to clear; holds NO ptmalloc lock). Verified by disassembly
    # 2026-11-02: ret 0x8f388 = right after the futex call into the
    # __futex_abstimed_wait trampoline at 0x9a700.
    _SAFE_WAIT_OFFSETS = [(0x8f340, 0x8f3a0)]
    _malloc_ranges = None       # [(start_off, end_off)] inside the libc mapping
    _safewait_ranges = None     # [(start_off, end_off)] safe futex-wait code

    def _libc_malloc_ranges(self, libc_start, libc_end, libc_path):
        """Resolve malloc/free code ranges inside libc once (offsets)."""
        if self._malloc_ranges is not None:
            return self._malloc_ranges
        ranges = []
        safewaits = []
        try:
            import subprocess as _sp
            out = _sp.run(["nm", "-D", "--defined-only", libc_path],
                          capture_output=True, text=True, timeout=10).stdout
            for line in out.splitlines():
                parts = line.split()
                if len(parts) >= 3 and parts[1] in ("T", "t", "W", "i", "I"):
                    try:
                        off = int(parts[0], 16)
                    except ValueError:
                        continue
                    nm = parts[2].split("@")[0]
                    if nm in self._MALLOC_SYMS:
                        ranges.append((off - 0x40, off + 0x400))
                    if any(nm.startswith(w) or w in nm for w in self._SAFE_WAIT_SYMS):
                        safewaits.append((off - 0x200, off + 0x600))
        except Exception:
            ranges = []
            safewaits = []
        # always include the build-verified anonymous-helper offsets
        for slo, shi in self._SAFE_WAIT_OFFSETS:
            safewaits.append((slo, shi))
        self._malloc_ranges = ranges
        self._safewait_ranges = safewaits
        return ranges

    def _stack_top_in_malloc(self, rsp):
        """True when the return address at [rsp] is inside libc malloc/free code.

        A paused game parks the main thread in the frame limiter's futex (also in
        libc, but in __futex_wait, NOT malloc) -- that state is safe to hijack.
        A running game parks it inside malloc waiting for the ptmalloc arena
        lock -- that state corrupts the heap if hijacked.
        """
        try:
            rd = getattr(self, "read", None) or getattr(self.p, "read")
            ra = struct.unpack("<Q", rd(rsp, 8))[0]
        except Exception:
            return True                      # unreadable -> treat as unsafe
        try:
            for m in (getattr(self, "maps", None) or self.p.maps):
                if m["start"] <= ra < m["end"]:
                    path = m.get("path") or ""
                    if "libc" not in path or "hoi4" in path:
                        return False          # not libc at all
                    ranges = self._libc_malloc_ranges(m["start"], m["end"], path)
                    # nm symbol offsets are FILE offsets; the mapping may be
                    # mapped at a file offset (x-text at 0x28000 for libc), so
                    # the in-file offset of ra is ra - vaddr + fileoff.
                    off = ra - m["start"] + m.get("fileoff", 0)
                    for lo, hi in getattr(self, "_safewait_ranges", None) or []:
                        if lo <= off <= hi:
                            return False     # join/cond/futex wait = safe
                    for lo, hi in ranges:
                        if lo <= off <= hi:
                            return True
                    return False             # libc but outside malloc/free
        except Exception:
            pass
        return False

    def _safe_stop(self, attempts=20):
        """Safe-RIP guard: never hijack the main thread inside libc/malloc/llvmpipe.

        Accept:
          * RIP inside the game's own executable segment, or
          * the thread parked in a blocking wait syscall (poll/sleep), or
          * (paused-game mode) futex after FUTEX_GRACE tries when the top of
            stack does not point into libc malloc/free.
        Anything else gets 3 ms to move on and is grabbed again.
        """
        r = None
        futex_seen = 0
        for attempt in range(attempts):
            r = self.getregs(self.pid)
            if self._in_image_text(r.rip):
                return r
            sc = self._syscall_no()
            if sc in self.SAFE_WAIT_SYSCALLS:
                return r
            if sc == self.FUTEX_SYSCALL:
                futex_seen += 1
                if futex_seen > self.FUTEX_GRACE and \
                        not self._stack_top_in_malloc(r.rsp):
                    return r                      # paused-game idle futex
            else:
                futex_seen = 0
            _ptrace(PTRACE_CONT, self.pid, 0, 0)
            time.sleep(0.003)
            _ptrace(PTRACE_INTERRUPT, self.pid, 0, 0)
            os.waitpid(self.pid, os.WUNTRACED | 0x40000000)
        raise CallError("no safe instruction point after %d tries (last rip=%#x, syscall=%s)"
                        % (attempts, r.rip if r else 0, self._syscall_no()))

    def call(self, func_addr, args=(), scratch_writer=None, timeout=5.0, xmm_zero=True):
        """Call func_addr with args (6 register arguments, plus stack args if needed).
        scratch_writer(scratch_base) may write args into scratch and return updated
        args. Returns RAX."""
        self.call_count = getattr(self, "call_count", 0) + 1
        proc = getattr(self, "p", None)
        if proc is not None:
            proc.call_count = getattr(proc, "call_count", 0) + 1
        # ---- hard address whitelist (task 1.3) ----
        off = func_addr - self.base if self.base and func_addr >= self.base else None
        if off not in VERIFIED_IMAGE_CALLS:
            raise CallError(
                "Blocked unverified call address: %#x (image offset %s). "
                "Only 1.19.4.1-verified addresses may be injected; register a "
                "disassembled one with api.allow_call()."
                % (func_addr, hex(off) if off is not None else "?"))
        main = self.pid
        old = self._safe_stop()          # task 1.2: no ptrace stop inside libc/llvmpipe
        saved = Regs.from_buffer_copy(old)
        saved_fp = None
        try:
            saved_fp = self.getfpregs(main)
        except Exception:
            saved_fp = None       # older kernel: run exactly as before

        # task 1.1: keep the x86_64 128-byte Red Zone untouched. Everything we write
        # sits below safe_rsp, which is 256 bytes under the interrupted rsp.
        safe_rsp = (old.rsp - 256) & ~0xF
        scratch = (safe_rsp - 0x80000) & ~0xFFF
        if scratch_writer:
            args = scratch_writer(scratch) or args

        new = Regs.from_buffer_copy(old)
        argregs = ["rdi","rsi","rdx","rcx","r8","r9"]
        for i, v in enumerate(args[:6]):
            setattr(new, argregs[i], ctypes.c_ulonglong(v & 0xFFFFFFFFFFFFFFFF).value)
        new.rax = 0
        # return address 0, so the function ends with SIGSEGV at rip=0
        # arguments 7 and beyond go on the stack, per the System V ABI:
        #   [rsp]=return address, [rsp+8]=arg7, [rsp+16]=arg8, ...
        stack_args = list(args[6:])
        sp = (scratch - 0x1000) & ~0xF
        if len(stack_args) % 2:          # rsp % 16 must be 8 at the call
            sp -= 8
        sp -= 8 * len(stack_args)
        sp -= 8
        buf = struct.pack("<Q", 0) + b"".join(
            struct.pack("<Q", v & 0xFFFFFFFFFFFFFFFF) for v in stack_args)
        if sp + len(buf) > safe_rsp:
            raise CallError("refusing stack write: would touch the red zone")
        self.p.write(sp, buf)
        new.rsp = sp
        new.rip = func_addr
        self.setregs(new, main)

        _ptrace(PTRACE_CONT, main, 0, 0)
        t0 = time.time()
        status = None
        while True:
            try:
                wpid, status = os.waitpid(main, os.WNOHANG | 0x40000000)
            except ChildProcessError:
                wpid, status = 0, None
            if wpid == main:
                break
            if time.time() - t0 > timeout:
                try:
                    _ptrace(PTRACE_INTERRUPT, main, 0, 0)
                    os.waitpid(main, 0x40000000)
                except Exception:
                    pass
                try:
                    self.setfpregs(saved_fp, main)
                except Exception:
                    pass
                try:
                    self.setregs(saved, main)
                except Exception:
                    pass
                raise CallError("call timed out (function %#x)" % func_addr)
            time.sleep(0.001)

        res = self.getregs(main)
        sig = os.WSTOPSIG(status) if os.WIFSTOPPED(status) else None
        rax = res.rax
        rip = res.rip
        try:
            self.setfpregs(saved_fp, main)
        except Exception:
            pass
        self.setregs(saved, main)
        if sig != signal.SIGSEGV or rip != 0:
            raise CallError("unexpected state: sig=%s rip=%#x (rax=%#x)" % (sig, rip, rax))
        return rax
