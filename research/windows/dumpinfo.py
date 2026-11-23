"""Read a Windows minidump and print the faulting thread's stack as RVAs.

Written for the 2026-11-18 hoi4.exe crash hunt: the game dies at a fixed RVA
(0xC08724) with no stack in the log, so WER LocalDumps is enabled and this
script turns the resulting .dmp into something disassemblable with wdis.py.

    python research/windows/dumpinfo.py <file.dmp> [--stack 128]

Output:
  * image base / size of hoi4.exe
  * exception code + faulting RVA
  * registers
  * every 8-byte stack slot that points into hoi4.exe, as an RVA, in order
"""
import os
import struct
import sys

MAGIC = b"MDMP"


class Dump:
    def __init__(self, path):
        with open(path, "rb") as fh:
            self.buf = fh.read()
        if self.buf[:4] != MAGIC:
            raise SystemExit("not a minidump: %r" % self.buf[:4])
        self.streams = {}
        (self.version, self.nstreams, self.dir_rva) = struct.unpack_from("<III", self.buf, 4)
        for i in range(self.nstreams):
            stype, size, rva = struct.unpack_from("<III", self.buf, self.dir_rva + i * 12)
            self.streams.setdefault(stype, (size, rva))
        self._mods = None
        self._mem = None

    # ---------------------------------------------------------------- modules
    def modules(self):
        if self._mods is not None:
            return self._mods
        out = []
        if 4 in self.streams:
            _size, rva = self.streams[4]
            n = struct.unpack_from("<I", self.buf, rva)[0]
            off = rva + 4
            for _ in range(n):
                base, size = struct.unpack_from("<QI", self.buf, off)
                namerva = struct.unpack_from("<I", self.buf, off + 20)[0]
                name = self._string(namerva)
                out.append((base, size, name))
                off += 108
        self._mods = out
        return out

    def _string(self, rva):
        length = struct.unpack_from("<I", self.buf, rva)[0]
        raw = self.buf[rva + 4: rva + 4 + length]
        return raw.decode("utf-16-le", "replace")

    # ---------------------------------------------------------------- memory
    def memory(self):
        """[(start, size, file_offset)] sorted by start."""
        if self._mem is not None:
            return self._mem
        out = []
        if 9 in self.streams:                      # Memory64List
            _s, rva = self.streams[9]
            n, baserva = struct.unpack_from("<QQ", self.buf, rva)
            off = rva + 16
            data = baserva
            for _ in range(n):
                start, size = struct.unpack_from("<QQ", self.buf, off)
                out.append((start, size, data))
                data += size
                off += 16
        if 5 in self.streams:                      # MemoryList
            _s, rva = self.streams[5]
            n = struct.unpack_from("<I", self.buf, rva)[0]
            off = rva + 4
            for _ in range(n):
                start, size, doff = struct.unpack_from("<QII", self.buf, off)
                out.append((start, size, doff))
                off += 16
        out.sort()
        self._mem = out
        return out

    def read(self, addr, size):
        for start, msz, foff in self.memory():
            if start <= addr and addr + size <= start + msz:
                rel = addr - start
                return self.buf[foff + rel: foff + rel + size]
        return None

    def u64(self, addr):
        raw = self.read(addr, 8)
        return struct.unpack("<Q", raw)[0] if raw else None

    # -------------------------------------------------------------- exception
    def exception(self):
        if 6 not in self.streams:
            return None
        _size, rva = self.streams[6]
        thread_id = struct.unpack_from("<I", self.buf, rva)[0]
        code = struct.unpack_from("<I", self.buf, rva + 8)[0]
        flags = struct.unpack_from("<I", self.buf, rva + 12)[0]
        exc_addr = struct.unpack_from("<Q", self.buf, rva + 24)[0]
        nparams = struct.unpack_from("<I", self.buf, rva + 32)[0]
        params = struct.unpack_from("<15Q", self.buf, rva + 40)
        ctx_size, ctx_rva = struct.unpack_from("<II", self.buf, rva + 160)
        return {
            "thread_id": thread_id, "code": code, "flags": flags,
            "address": exc_addr, "nparams": nparams,
            "params": params[:max(2, min(nparams, 15))],
            "ctx_size": ctx_size, "ctx_rva": ctx_rva,
        }

    def context(self, ctx_rva):
        b = self.buf
        def r(off):
            return struct.unpack_from("<Q", b, ctx_rva + off)[0]
        return {
            "Rax": r(0x78), "Rcx": r(0x80), "Rdx": r(0x88), "Rbx": r(0x90),
            "Rsp": r(0x98), "Rbp": r(0xA0), "Rsi": r(0xA8), "Rdi": r(0xB0),
            "R8": r(0xB8), "R9": r(0xC0), "R10": r(0xC8), "R11": r(0xD0),
            "R12": r(0xD8), "R13": r(0xE0), "R14": r(0xE8), "R15": r(0xF0),
            "Rip": r(0xF8),
        }


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    path = sys.argv[1]
    nslots = 128
    if "--stack" in sys.argv:
        nslots = int(sys.argv[sys.argv.index("--stack") + 1])

    d = Dump(path)
    print("file      : %s (%.1f MB)" % (os.path.basename(path), len(d.buf) / 1048576.0))
    print("streams   : %s" % sorted(d.streams))

    img = None
    main_mod = None
    for base, size, name in d.modules():
        if name.lower().endswith("hoi4.exe"):
            main_mod = (base, size)
            break
    if not main_mod:
        for base, size, name in d.modules():
            if name.lower().endswith(".exe"):
                main_mod = (base, size)
                break
    if not main_mod:
        print("no main module found")
        return 1
    base, size = main_mod
    img = (base, base + size)
    print("image     : base=0x%x size=0x%x" % (base, size))

    ex = d.exception()
    if not ex:
        print("no exception stream")
        return 1
    ctx = d.context(ex["ctx_rva"])
    print("exception : code=0x%08x flags=0x%x addr=0x%x" % (ex["code"], ex["flags"], ex["address"]))
    if img[0] <= ex["address"] < img[1]:
        print("            -> RVA 0x%X" % (ex["address"] - base))
    for i, v in enumerate(ex["params"]):
        print("            param%d = 0x%x" % (i, v))
    print("registers : " + " ".join("%s=0x%x" % (k, v) for k, v in ctx.items()))

    rsp = ctx["Rsp"]
    raw = d.read(rsp, 8 * nslots)
    if not raw:
        print("stack not captured in dump (Rsp=0x%x)" % rsp)
        return 1
    print("\nstack slots pointing into the image (RVA):")
    found = []
    for i in range(len(raw) // 8):
        v = struct.unpack_from("<Q", raw, i * 8)[0]
        if img[0] <= v < img[1]:
            found.append((i * 8, v - base))
    for off, rva in found:
        print("   rsp+0x%-4x -> RVA 0x%-8X" % (off, rva))
    if not found:
        print("   (none)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
