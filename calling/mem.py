"""Live process memory access. Platform-independent layer.

Raw reads and writes are delegated to a platform backend (process/linux.py);
everything here is platform-independent.
Nothing above this module needs to know which operating system it is on.
"""
import os
import struct
import ctypes
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from process import Backend, find_pid          # noqa: E402,F401


_WIN_MEM_PRIVATE = 0x20000
_WIN_MEM_IMAGE = 0x1000000


class Proc:
    """Typed memory access to a HOI4 process."""

    def __init__(self, pid=None, exe_name=None):
        import config
        self.exe_name = exe_name or config.EXE_NAME
        self.be = Backend(pid, self.exe_name)
        self.pid = self.be.pid
        self.base = self.be.image_base
        self.image_size = getattr(self.be, "image_size", 0)
        self.maps = self.be.regions()

    @property
    def call_count(self):
        """当前进程在本会话内完成的真实函数注入总次数（穿透后端与 winject 全局计数器）。"""
        be_cnt = int(getattr(self.be, "call_count", 0) or 0)
        if sys.platform == "win32":
            try:
                from process import winject as _wj
                return max(be_cnt, int(_wj.call_count(self.pid)))
            except Exception:
                pass
        return be_cnt

    def va(self, file_vaddr):
        """Address in the binary -> address in the running process."""
        return self.base + file_vaddr

    def refresh_maps(self):
        """Re-read the region list; a long campaign maps new arenas on the fly.

        `self.maps` is a snapshot taken at attach time. ObjectIndex's fallback scan
        calls this before walking every writable region, otherwise heap arenas
        mmapped after attach are invisible (B4, review 2026-11-07). The Linux
        backend has no such method -- its maps are already current.
        """
        fn = getattr(self.be, "refresh_maps", None)
        if fn is not None:
            try:
                self.maps = fn()
                self.image_size = getattr(self.be, "image_size", getattr(self, "image_size", 0))
            except Exception:
                pass
        return self.maps

    # ---------- raw ----------
    def read(self, addr, size):
        return self.be.read(addr, size)

    def write(self, addr, data):
        return self.be.write(addr, data)

    def try_read(self, addr, size):
        try:
            return self.read(addr, size)
        except OSError:
            return None

    def read_into(self, addr, buf, size=None):
        """直接读入调用方复用的可写缓冲，减少扫描循环的大块分配与拷贝。"""
        fn = getattr(getattr(self, "be", None), "read_into", None)
        if fn is not None:
            return fn(addr, buf, size)
        n = len(buf) if size is None else int(size)
        if n <= 0:
            return 0
        data = self.read(addr, n)
        got = len(data)
        buf[:got] = data
        return got

    def try_read_into(self, addr, buf, size=None):
        fn = getattr(getattr(self, "be", None), "read_into", None)
        if fn is not None:
            try:
                return fn(addr, buf, size)
            except OSError:
                return 0
        n = len(buf) if size is None else int(size)
        if n <= 0:
            return 0
        data = self.try_read(addr, n)
        if not data:
            return 0
        got = len(data)
        buf[:got] = data
        return got

    # ---------- typed ----------
    def u8 (self,a): return self.read(a,1)[0]
    def u16(self,a): return struct.unpack("<H", self.read(a,2))[0]
    def u32(self,a): return struct.unpack("<I", self.read(a,4))[0]
    def i32(self,a): return struct.unpack("<i", self.read(a,4))[0]
    def u64(self,a): return struct.unpack("<Q", self.read(a,8))[0]
    def i64(self,a): return struct.unpack("<q", self.read(a,8))[0]
    def f32(self,a): return struct.unpack("<f", self.read(a,4))[0]
    def f64(self,a): return struct.unpack("<d", self.read(a,8))[0]

    def ptr(self,a):
        v = self.try_read(a,8)
        return struct.unpack("<Q", v)[0] if v and len(v)==8 else None

    def cstr(self, addr, maxlen=256):
        b = self.try_read(addr, maxlen)
        if not b: return None
        i = b.find(b"\0")
        return b[:i if i>=0 else len(b)].decode("utf-8","replace")

    _MSVC_STRING = sys.platform == "win32"

    def stdstring(self, addr):
        """std::string @addr -> str.

        MSVC (Windows): { char buf[16]; size_t size; size_t cap; }
        libstdc++ (Linux): { char* p; size_t n; char sso[16]; }
        Both are 0x20 bytes but the fields sit in different places.
        """
        d = self.try_read(addr, 32)
        if not d or len(d) < 32:
            return None
        if self._MSVC_STRING:
            n, cap = struct.unpack_from("<QQ", d, 0x10)
            if n > 0x10000 or cap > 0x10000 or n > cap:
                return None
            if cap <= 15:                       # SSO
                return d[:n].decode("utf-8", "replace")
            if n == 0:
                return ""
            p = struct.unpack_from("<Q", d, 0)[0]
            b = self.try_read(p, n)
            return b.decode("utf-8", "replace") if b is not None else None
        p, n = struct.unpack_from("<QQ", d, 0)
        if n > 0x10000:
            return None
        if p == addr + 16:            # SSO
            return d[16:16+n].decode("utf-8", "replace")
        if n == 0:
            return ""
        b = self.try_read(p, n)
        return b.decode("utf-8", "replace") if b is not None else None

    # ---------- scan ----------
    def regions(self, writable=True, anon_only=False, min_size=0, heap_only=None):
        """枚举可读内存区。

        在 Windows 下 `writable=True` 默认启用 `heap_only=True`：仅保留
        `MEM_PRIVATE`（C++ 堆/arena/栈）以及主进程可执行文件自身的 `MEM_IMAGE`
        可写段（`.data` 静态全局），跳过 `MEM_MAPPED`（DX11/文件映射）和其他
        系统 DLL 的可写段，可砍掉大量无效区域且不影响游戏对象扫描。
        如需完整无过滤枚举，显式传 `heap_only=False`。
        """
        if heap_only is None:
            heap_only = bool(writable)
        img_base = getattr(self, "base", 0) or 0
        img_size = getattr(self, "image_size", 0) or getattr(getattr(self, "be", None), "image_size", 0) or 0
        exe_low = (getattr(self, "exe_name", None) or "").lower()
        exe_names = {exe_low, exe_low + ".exe"} if exe_low else set()
        out = []
        for m in self.maps:
            if "r" not in m["perms"]:
                continue
            if writable and "w" not in m["perms"]:
                continue
            if anon_only and m["path"]:
                continue
            if m["path"].startswith("/dev/"):
                continue
            if m["end"] - m["start"] < min_size:
                continue
            if heap_only and "type" in m:
                mtype = m["type"]
                if mtype == _WIN_MEM_PRIVATE:
                    pass
                elif mtype == _WIN_MEM_IMAGE:
                    in_main = False
                    if img_size > 0 and img_base <= m["start"] < img_base + img_size:
                        in_main = True
                    elif exe_names and m.get("path"):
                        if os.path.basename(m["path"]).lower() in exe_names:
                            in_main = True
                    if not in_main:
                        continue
                else:
                    # 跳过 MEM_MAPPED（DX11/共享映射）等非游戏堆区域
                    continue
            out.append(m)
        return out

    def scan_value(self, needle, regions=None, limit=None, align=8):
        """needle: bytes. Returns list of addresses."""
        if not needle:
            return []
        hits = []
        regions = regions if regions is not None else self.regions()
        CH = 8 << 20
        overlap = max(0, len(needle) - 1)
        buf = bytearray(CH)
        for m in regions:
            a = m["start"]
            last_hit = -1
            while a < m["end"]:
                n = min(CH, m["end"] - a)
                got = self.try_read_into(a, buf, n)
                if got >= len(needle):
                    i = 0
                    while True:
                        i = buf.find(needle, i, got)
                        if i < 0:
                            break
                        addr = a + i
                        if addr > last_hit and (align == 1 or addr % align == 0):
                            hits.append(addr)
                            last_hit = addr
                            if limit and len(hits) >= limit:
                                return hits
                        i += 1
                if a + n >= m["end"]:
                    break
                a += max(1, n - overlap)
        return hits

    def scan_ptr(self, value, **kw):
        return self.scan_value(struct.pack("<Q", value), **kw)


if __name__ == "__main__":
    p = Proc()
    print("pid  =", p.pid)
    print("base =", hex(p.base))
    print("bolge =", len(p.maps))
