"""Offline MSVC RTTI resolver for hoi4.exe (PE32+).

Finds a class's vtable from its mangled RTTI name, without a disassembler and
without touching the running game.

Usage:
    python pe_rtti.py "COrderAssignCommand" ["CCreateDivisionTemplateCommand" ...]

For each class it prints the TypeDescriptor RVA, the CompleteObjectLocator RVA,
the vtable RVA and the first VT_SLOTS method pointers (slot 0 = first virtual
method, i.e. the destructor for most HOI4 commands).
"""
import struct
import sys


class PE:
    def __init__(self, path):
        with open(path, "rb") as f:
            self.buf = f.read()
        b = self.buf
        if b[:2] != b"MZ":
            raise SystemExit("not a PE")
        e_lfanew = struct.unpack_from("<I", b, 0x3C)[0]
        if b[e_lfanew:e_lfanew + 4] != b"PE\0\0":
            raise SystemExit("no PE signature")
        coff = e_lfanew + 4
        nsec = struct.unpack_from("<H", b, coff + 2)[0]
        size_opt = struct.unpack_from("<H", b, coff + 16)[0]
        opt = coff + 20
        magic = struct.unpack_from("<H", b, opt)[0]
        if magic != 0x20B:
            raise SystemExit("not PE32+ (magic %#x)" % magic)
        self.image_base = struct.unpack_from("<Q", b, opt + 0x18)[0]
        self.sections = []
        sec = opt + size_opt
        for i in range(nsec):
            o = sec + i * 40
            name = b[o:o + 8].rstrip(b"\0").decode("latin-1")
            vsize, va, rawsize, rawptr = struct.unpack_from("<IIII", b, o + 8)
            self.sections.append((name, va, vsize, rawsize, rawptr))

    def rva_to_off(self, rva):
        for name, va, vsize, rawsize, rawptr in self.sections:
            span = max(vsize, rawsize)
            if va <= rva < va + span:
                return rawptr + (rva - va)
        raise KeyError("RVA %#x not in any section" % rva)

    def off_to_rva(self, off):
        for name, va, vsize, rawsize, rawptr in self.sections:
            if rawptr <= off < rawptr + rawsize:
                return va + (off - rawptr)
        raise KeyError("offset %#x not in any section" % off)

    def u32(self, rva):
        return struct.unpack_from("<I", self.buf, self.rva_to_off(rva))[0]

    def u64(self, rva):
        return struct.unpack_from("<Q", self.buf, self.rva_to_off(rva))[0]

    def text_range(self):
        for name, va, vsize, rawsize, rawptr in self.sections:
            if name == ".text":
                return va, va + max(vsize, rawsize)
        raise SystemExit("no .text")


def find_all(buf, needle):
    out = []
    i = buf.find(needle)
    while i != -1:
        out.append(i)
        i = buf.find(needle, i + 1)
    return out


def resolve(pe, cls, vt_slots=24):
    b = pe.buf
    needle = (".?AV%s@@" % cls).encode()
    hits = find_all(b, needle)
    if not hits:
        return {"class": cls, "error": "RTTI name not found"}
    res = {"class": cls, "name_hits": len(hits), "vtables": []}
    for h in hits:
        name_rva = pe.off_to_rva(h)
        desc_rva = name_rva - 0x10          # vfptr(8) + spare(8) then name
        # CompleteObjectLocator: scan .rdata for pTypeDescriptor == desc_rva
        col_rvas = []
        for off in find_all(b, struct.pack("<I", desc_rva)):
            try:
                rva = pe.off_to_rva(off)
            except KeyError:
                continue
            cand = rva - 0xC
            if cand < 0:
                continue
            try:
                if pe.u32(cand + 0xC) != desc_rva:
                    continue          # pTypeDescriptor must match
                sig = pe.u32(cand)
                if sig != 1:
                    continue          # x64 COL signature
            except KeyError:
                continue
            col_rvas.append(cand)
        entry = {"desc_rva": desc_rva, "col_rvas": col_rvas, "vtables": []}
        for col in col_rvas:
            col_va = pe.image_base + col
            for off in find_all(b, struct.pack("<Q", col_va)):
                try:
                    rva = pe.off_to_rva(off)
                except KeyError:
                    continue
                vt = rva + 8
                t0, t1 = pe.text_range()
                slots = []
                for i in range(vt_slots):
                    try:
                        slots.append(pe.u64(vt + i * 8))
                    except KeyError:
                        break
                entry["vtables"].append({
                    "vt_rva": vt, "vt_va": pe.image_base + vt,
                    "slots": [("text" if t0 <= (s - pe.image_base) < t1 else "?")
                              + ":%#x" % (s - pe.image_base) for s in slots],
                })
        res["vtables"].append(entry)
    return res


def main():
    path = r"D:\SteamLibrary\steamapps\common\Hearts of Iron IV\hoi4.exe"
    pe = PE(path)
    print("image_base %#x  sections %s" % (pe.image_base,
          ", ".join("%s@%#x+%#x" % (s[0], s[1], max(s[2], s[3])) for s in pe.sections)))
    classes = sys.argv[1:] or ["COrderAssignCommand"]
    for cls in classes:
        r = resolve(pe, cls)
        print("\n=== %s ===" % cls)
        if "error" in r:
            print("  ", r["error"])
            continue
        for e in r["vtables"]:
            print("  TypeDescriptor %#x  COL %s" % (e["desc_rva"],
                  ", ".join("%#x" % c for c in e["col_rvas"]) or "-"))
            for vt in e["vtables"]:
                print("    vtable RVA %#x (VA %#x)" % (vt["vt_rva"], vt["vt_va"]))
                for i, s in enumerate(vt["slots"]):
                    print("      slot %2d  %s" % (i, s))


if __name__ == "__main__":
    main()
