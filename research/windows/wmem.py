# -*- coding: utf-8 -*-
"""Standalone read-only memory probe for the live hoi4.exe (Windows).

Does NOT need the MCP server.  Usage:

  python wmem.py scan CEvent                 # all objects whose vtable == CEvent
  python wmem.py scan CEvent --limit 5
  python wmem.py dump 0x1da1f8a5390 0x120    # hex dump + decoded std::strings
  python wmem.py str  0x1da3e10d0e0 64

Vtable RVAs come from research/windows/rtti_win.pkl.
"""
import ctypes
import ctypes.wintypes as wt
import os
import pickle
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
RTTI = os.path.join(HERE, "rtti_win.pkl")

k32 = ctypes.WinDLL("kernel32", use_last_error=True)

PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010
MEM_COMMIT = 0x1000
PAGE_GUARD = 0x100
PAGE_NOACCESS = 0x01
WRITABLE = (0x04, 0x08, 0x40, 0x80)  # PAGE_READWRITE, WRITECOPY, EXECUTE_READWRITE, EXECUTE_WRITECOPY


class MEMORY_BASIC_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_size_t),
        ("AllocationBase", ctypes.c_size_t),
        ("AllocationProtect", wt.DWORD),
        ("RegionSize", ctypes.c_size_t),
        ("State", wt.DWORD),
        ("Protect", wt.DWORD),
        ("Type", wt.DWORD),
    ]


k32.VirtualQueryEx.argtypes = [wt.HANDLE, ctypes.c_size_t,
                               ctypes.POINTER(MEMORY_BASIC_INFORMATION), ctypes.c_size_t]
k32.VirtualQueryEx.restype = ctypes.c_size_t
k32.ReadProcessMemory.argtypes = [wt.HANDLE, ctypes.c_size_t, ctypes.c_void_p,
                                  ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
k32.ReadProcessMemory.restype = wt.BOOL
k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
k32.OpenProcess.restype = wt.HANDLE


def find_pid(name="hoi4.exe"):
    import subprocess
    out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True).stdout
    for line in out.splitlines():
        parts = [p.strip('"') for p in line.split('","')]
        if len(parts) >= 2 and parts[0].lower() == name:
            return int(parts[1])
    return None


class Proc(object):
    def __init__(self, pid=None):
        self.pid = pid or find_pid()
        if not self.pid:
            raise SystemExit("hoi4.exe not running")
        self.h = k32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, self.pid)
        if not self.h:
            raise SystemExit("OpenProcess failed: %d" % ctypes.get_last_error())
        self.base = self._image_base()

    def _image_base(self):
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        hmods = (ctypes.c_void_p * 1024)()
        need = wt.DWORD(0)
        psapi.EnumProcessModulesEx.argtypes = [wt.HANDLE, ctypes.POINTER(ctypes.c_void_p), wt.DWORD, ctypes.POINTER(wt.DWORD), wt.DWORD]
        psapi.EnumProcessModulesEx.restype = wt.BOOL
        if not psapi.EnumProcessModulesEx(self.h, hmods, ctypes.sizeof(hmods), ctypes.byref(need), 0x03):
            return 0
        n = need.value // ctypes.sizeof(ctypes.c_void_p)
        psapi.GetModuleBaseNameW.argtypes = [wt.HANDLE, ctypes.c_void_p, wt.LPWSTR, wt.DWORD]
        psapi.GetModuleBaseNameW.restype = wt.DWORD
        buf = ctypes.create_unicode_buffer(260)
        for i in range(n):
            base = hmods[i]
            if not base:
                continue
            psapi.GetModuleBaseNameW(self.h, base, buf, 260)
            if buf.value.lower() == "hoi4.exe":
                return base
        return 0

    def read(self, addr, n):
        buf = ctypes.create_string_buffer(n)
        got = ctypes.c_size_t(0)
        if not k32.ReadProcessMemory(self.h, addr, buf, n, ctypes.byref(got)):
            return b""
        return buf.raw[:got.value]

    def u64(self, addr):
        b = self.read(addr, 8)
        return struct.unpack("<Q", b)[0] if len(b) == 8 else 0

    def u32(self, addr):
        b = self.read(addr, 4)
        return struct.unpack("<I", b)[0] if len(b) == 4 else 0

    def regions(self, writable=True):
        addr = 0
        mbi = MEMORY_BASIC_INFORMATION()
        while k32.VirtualQueryEx(self.h, addr, ctypes.byref(mbi), ctypes.sizeof(mbi)):
            a = mbi.BaseAddress
            sz = mbi.RegionSize
            if mbi.State == MEM_COMMIT and not (mbi.Protect & PAGE_GUARD) and mbi.Protect != PAGE_NOACCESS:
                if (not writable) or (mbi.Protect & 0xFF) in WRITABLE:
                    yield a, a + sz
            addr = a + sz
            if addr > 0x7FFFFFFFFFFF:
                break

    def scan(self, rvas, limit=None, max_hits=200000):
        needles = [struct.pack("<Q", self.base + r) for r in rvas]
        hits = []
        for a, b in self.regions():
            pos = a
            while pos < b:
                n = min(8 << 20, b - pos)
                data = self.read(pos, n)
                if not data:
                    break
                for nd in needles:
                    i = data.find(nd)
                    while i >= 0:
                        if i % 8 == 0:
                            hits.append(pos + i)
                            if limit and len(hits) >= limit:
                                return hits
                        i = data.find(nd, i + 1)
                pos += n
                if len(hits) >= max_hits:
                    return hits
        return hits

    def cstr(self, addr, maxn=200):
        b = self.read(addr, maxn)
        return b.split(b"\0")[0].decode("utf-8", "replace")

    def stdstring(self, addr):
        """MSVC std::string at addr -> (size, cap, text)."""
        ptr, size, cap = self.u64(addr), self.u64(addr + 0x10), self.u64(addr + 0x18)
        if size > 4096 or cap > 4096:
            return (size, cap, "<bad size/cap>")
        base = ptr if cap >= 0x10 else addr
        b = self.read(base, size) if size else b""
        return (size, cap, b.decode("utf-8", "replace"))


def rtti_rvas(name):
    d = pickle.load(open(RTTI, "rb"))["classes"]
    out = list(d.get(".?AV%s@@" % name) or [])
    if not out:
        pref = ".?AV%s@" % name
        for k, v in d.items():
            if k.startswith(pref):
                out.extend(v)
    return out


def _addr(s):
    tot = 0
    for part in str(s).split('+'):
        tot += int(part, 0)
    return tot


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return
    p = Proc()
    cmd = sys.argv[1]
    if cmd == "pid":
        print(p.pid, hex(p.base))
    elif cmd == "scan":
        name = sys.argv[2]
        lim = None
        if "--limit" in sys.argv:
            lim = int(sys.argv[sys.argv.index("--limit") + 1])
        rvas = rtti_rvas(name)
        hits = p.scan(rvas, limit=lim)
        print("%s rvas=%s hits=%d" % (name, [hex(r) for r in rvas], len(hits)))
        for h in hits[:40]:
            print("   " + hex(h))
    elif cmd == "dump":
        a = _addr(sys.argv[2])
        n = int(sys.argv[3], 0) if len(sys.argv) > 3 else 0x100
        b = p.read(a, n)
        for i in range(0, len(b), 16):
            row = b[i:i + 16]
            hexp = " ".join("%02x" % c for c in row)
            asc = "".join(chr(c) if 32 <= c < 127 else "." for c in row)
            print("%08x  %-47s  %s" % (i, hexp, asc))
        print("--- as qwords ---")
        for i in range(0, len(b) - 7, 8):
            print("+0x%03x  %016x" % (i, struct.unpack_from("<Q", b, i)[0]))
    elif cmd == "str":
        a = _addr(sys.argv[2])
        print(repr(p.cstr(a, int(sys.argv[3], 0) if len(sys.argv) > 3 else 200)))
    elif cmd == "stdstr":
        a = _addr(sys.argv[2])
        print(p.stdstring(a))
    else:
        print(__doc__)


if __name__ == "__main__":
    main()

