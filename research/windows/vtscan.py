"""Locate the real constructor of a CCommand class in hoi4.exe.

2026-11-17.  hoi4_delete_order called `OD_CTOR = 0x231C510` and hoi4.exe died
inside exactly that address (Event Log: hoi4.exe+0x231C510, c0000005), so the
constant is wrong for 1.19.5.0.  This script re-derives it from the RTTI vtable
that research/windows/rtti_win.pkl already holds:

  1. vtable RVA for the class,
  2. every place in .text that stores that vtable into [rcx] --
     `48 C7 01 <imm32>` (mov qword [rcx], imm32) or
     `48 8D 05 <disp32>` (lea rax,[rip+disp]), which is what an MSVC ctor does
     in its first two instructions,
  3. reports the function entry (nearest preceding `CC`/`C3`-aligned int3 pad)
     plus a hexdump so the field stores can be read off.

Usage:
  python vtscan.py COrderDeleteCommand
  python vtscan.py COrderDeleteCommand --class-from '?AVCOrderDeleteCommand@@'
"""
import os
import pickle
import struct
import sys

EXE = r"D:\SteamLibrary\steamapps\common\Hearts of Iron IV\hoi4.exe"
RTTI = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rtti_win.pkl")


def load_image(path):
    data = open(path, "rb").read()
    if data[:2] != b"MZ":
        raise SystemExit("not a PE")
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    if data[pe:pe + 4] != b"PE\0\0":
        raise SystemExit("no PE sig")
    nsec = struct.unpack_from("<H", data, pe + 6)[0]
    opt_size = struct.unpack_from("<H", data, pe + 20)[0]
    opt = pe + 24
    magic = struct.unpack_from("<H", data, opt)[0]
    image_base = (struct.unpack_from("<Q", data, opt + 24)[0] if magic == 0x20b
                  else struct.unpack_from("<I", data, opt + 28)[0])
    sec = opt + opt_size
    sections = []
    for i in range(nsec):
        off = sec + i * 40
        name = data[off:off + 8].rstrip(b"\0").decode("latin1")
        vsize, vaddr, rsize, raddr = struct.unpack_from("<IIII", data, off + 8)
        sections.append({"name": name, "va": vaddr, "vsize": vsize,
                         "raw": raddr, "rsize": rsize})
    return data, image_base, sections


def rva_to_off(sections, rva):
    for s in sections:
        if s["va"] <= rva < s["va"] + max(s["vsize"], s["rsize"]):
            return s["raw"] + (rva - s["va"])
    return None


def off_to_rva(sections, off):
    for s in sections:
        if s["raw"] <= off < s["raw"] + s["rsize"]:
            return s["va"] + (off - s["raw"])
    return None


def vtables_for(name):
    if not os.path.exists(RTTI):
        return []
    db = pickle.load(open(RTTI, "rb"))
    classes = db.get("classes") or {}
    hits = []
    for mangled, vts in classes.items():
        if name in mangled:
            hits.append((mangled, vts))
    return hits


def scan_text(data, sections, vts):
    """Find stores of any vtable RVA into [rcx] / lea rax,[rip+vt]."""
    text = next((s for s in sections if s["name"] == ".text"), None)
    if not text:
        raise SystemExit("no .text")
    blob = data[text["raw"]:text["raw"] + text["rsize"]]
    out = []
    for vt in vts:
        # 48 C7 01 imm32   -> mov qword ptr [rcx], imm32
        pat1 = b"\x48\xC7\x01" + struct.pack("<I", vt)
        start = 0
        while True:
            i = blob.find(pat1, start)
            if i < 0:
                break
            out.append((vt, text["va"] + i, "mov [rcx],vt"))
            start = i + 1
        # 48 8D 05 disp32  -> lea rax,[rip+disp]   (disp relative to next insn)
        start = 0
        while True:
            i = blob.find(b"\x48\x8D\x05", start)
            if i < 0:
                break
            disp = struct.unpack_from("<i", blob, i + 3)[0]
            target = text["va"] + i + 7 + disp
            if target == vt:
                out.append((vt, text["va"] + i, "lea rax,[vt]"))
            start = i + 1
    out.sort(key=lambda r: r[1])
    return out


def func_entry(blob_off, text_va, blob):
    """Walk backwards over int3/nop padding to the previous ret/int3 boundary."""
    i = blob_off - text_va
    j = i
    while j > 0 and blob[j - 1] in (0xCC, 0x90, 0xC3):
        j -= 1
    # MSVC aligns function starts to 16 bytes after int3 padding
    while j > 0 and (j % 16) != 0:
        j -= 1
    return text_va + j


def main():
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    name = sys.argv[1]
    data, image_base, sections = load_image(EXE)
    print("image_base = 0x%x  sections = %d" % (image_base, len(sections)))
    hits = vtables_for(name)
    if not hits:
        raise SystemExit("no RTTI vtable for %r in rtti_win.pkl" % name)
    vts = []
    for mangled, vtl in hits:
        print("RTTI %s -> vtable RVA(s) %s" % (mangled, [hex(v) for v in vtl]))
        vts.extend(vtl)
    found = scan_text(data, sections, vts)
    if not found:
        raise SystemExit("no [rcx]=vtable store found; class may not be in .text")
    text = next(s for s in sections if s["name"] == ".text")
    blob = data[text["raw"]:text["raw"] + text["rsize"]]
    print("\n%d reference(s):" % len(found))
    for vt, rva, how in found:
        entry = func_entry(rva, text["va"], blob)
        off = rva_to_off(sections, rva)
        print("\n  vt=0x%x  store at RVA 0x%x (%s)  -> function entry ~0x%x"
              % (vt, rva, how, entry))
        eo = rva_to_off(sections, entry)
        chunk = data[eo:eo + 96]
        for k in range(0, len(chunk), 16):
            print("    %08x  %s" % (entry + k, " ".join("%02x" % b for b in chunk[k:k + 16])))


if __name__ == "__main__":
    main()
