"""Offline regression tests for the 2026-11-19 fixes.

  * hoi4_trade_routes  -- the export book really is CCountry+0x11F8 -> +0x728
  * hoi4_attach_air_wings / hoi4_air_wings -- ownership is CStrategicAir+0x90

Run:  python research/windows/test_air_trade_20261119.py

No game process is required. Everything here either reads hoi4.exe with the
repo's own PE reader (pedis) or exercises pure-Python logic. The live behaviour
was verified on pid 61964 (GER, 1936.03.27) and is written up in
research/WINDOWS_1.19.5.md §101/§102.
"""
import io
import os
import re
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CALLING = os.path.abspath(os.path.join(HERE, "..", "..", "calling"))
sys.path.insert(0, CALLING)
sys.path.insert(0, HERE)

FAILS = []


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + ("   " + detail if detail else ""))
    if not cond:
        FAILS.append(name)


def bytes_at(rva, n):
    import pedis
    return pedis.read(rva, n)


def has(rva, hexstr):
    """True when `hexstr` occurs at rva (whitespace ignored)."""
    want = bytes.fromhex(hexstr.replace(" ", ""))
    return bytes_at(rva, len(want)) == want


def va_of(rva):
    import pedis
    return pedis.IMAGE_BASE + rva


def vt_class(vt_rva):
    """Class name for a vtable, from the cached RTTI scan (rtti_win.json.gz / .pkl)."""
    import safe_cache
    path = os.path.join(HERE, "rtti_win.pkl")
    if not safe_cache.exists(path):
        return "<no rtti_win>"
    raw = safe_cache.load(path).get("vt", {}).get(vt_rva)
    # MSVC decorated name: ".?AVCResourceExchange@@"
    if raw and raw.startswith(".?AV"):
        return raw[4:].split("@@")[0]
    return raw


# ------------------------------------------------------------- trade routes
def test_trade_win():
    print("\n[trade]  CCountryResources +0x728 export book")
    import trade_win as TW

    check("CCountry -> CCountryResources is +0x11F8", TW.COUNTRY_RESOURCES == 0x11F8)
    check("trade book data ptr is +0x728", TW.TRADE_BOOK == 0x728)
    check("trade book size is +0x734", TW.TRADE_BOOK_SIZE == 0x734)
    check("slot stride is 0x18", TW.SLOT_STRIDE == 0x18)
    check("exchange partner at +0x88", TW.EX_PARTNER == 0x88)
    check("exchange resource at +0x90", TW.EX_RESOURCE == 0x90)
    check("exchange amount at +0x98", TW.EX_AMOUNT == 0x98)
    check("amount is CARGO fixed point", TW.FIXED == 100000.0)

    # The seven resources, in the order of common/resources/00_resources.txt
    # (= icon_frame), which is the trade-book slot order.
    check("seven resources", len(TW.RESOURCE_NAMES) == 7, str(TW.RESOURCE_NAMES))
    check("slot 1 is oil", TW.resource_name(1) == "oil")
    check("slot 5 is steel", TW.resource_name(5) == "steel")
    check("slot 7 is coal", TW.resource_name(7) == "coal")
    check("slot 0 is unused", TW.resource_name(0) == "")
    check("slot 8 is out of range", TW.resource_name(8) == "")

    # ... and that is genuinely the file order, not a guess.
    import config
    path = os.path.join(config.find_game_dir(), "common", "resources",
                        "00_resources.txt")
    txt = io.open(path, encoding="utf-8", errors="replace").read()
    order = re.findall(r"^\s*([a-z_]+)\s*=\s*\{", txt, re.M)
    order = [x for x in order if x != "resources"]
    check("00_resources.txt order matches RESOURCE_NAMES",
          order == TW.RESOURCE_NAMES, "%r vs %r" % (order, TW.RESOURCE_NAMES))

    # CResourceExchange really is the class the book points at.
    import pedis
    check("vtable 0x29D8E60 is CResourceExchange",
          vt_class(0x29D8E60) == "CResourceExchange", str(vt_class(0x29D8E60)))

    # The ctor allocates/clears 0xF0 bytes: `mov edx, 0xf0` at 0xD052C8.
    check("CResourceExchange size is 0xF0 (ctor mov edx,0xf0)",
          has(0xD052C8, "BA F0 00 00 00"))

    # CCreateTradeCommand proves the container: it is the only writer.
    check("CCreateTradeCommand vtable +0x48 is CanExecute 0x16DABF0",
          pedis.u64(0x2A5F388 + 0x48) - pedis.IMAGE_BASE == 0x16DABF0,
          hex(pedis.u64(0x2A5F388 + 0x48)))
    check("CCreateTradeCommand vtable +0x50 is Execute 0x16DA8A0",
          pedis.u64(0x2A5F388 + 0x50) - pedis.IMAGE_BASE == 0x16DA8A0,
          hex(pedis.u64(0x2A5F388 + 0x50)))
    # Execute -> worker 0xD15640, and the worker is where every offset is used:
    check("worker reads [rax+0x11F8] (CCountryResources)",
          has(0xD1566E, "48 8B B0 F8 11 00 00"))
    check("worker reads [rsi+0x728] (trade book data)",
          has(0xD15681, "48 8B 86 28 07 00 00"))
    check("worker reads slot size at +0x0C",
          has(0xD1568C, "48 63 44 C8 0C"))
    check("worker compares the partner tag at +0x88",
          has(0xD156A3, "48 8D 8F 88 00 00 00"))
    check("worker scales the amount by 100000 (imul r9,r15,0x186a0)",
          has(0xD157DB, "4D 69 CF A0 86 01 00"))


# ---------------------------------------------------------------- air wings
def test_air_ownership():
    print("\n[air]  CAirWing+0x38 -> CAirWingPool+0x40 -> CStrategicAir+0x90")
    import airops_win as AW

    check("SA_OWNER constant is 0x90", AW.SA_OWNER == 0x90)
    check("CAirWing vtable is 0x29F7748", AW.VT_AIRWING_MAIN == 0x29F7748)
    check("CAirWingPool vtable is 0x29F77D8", AW.VT_AIRWINGPOOL == 0x29F77D8)
    import pedis
    check("vtable 0x29F7748 is CAirWing",
          vt_class(AW.VT_AIRWING_MAIN) == "CAirWing", str(vt_class(AW.VT_AIRWING_MAIN)))
    check("vtable 0x29F77D8 is CAirWingPool",
          vt_class(AW.VT_AIRWINGPOOL) == "CAirWingPool",
          str(vt_class(AW.VT_AIRWINGPOOL)))

    # The old heuristic must be gone: german_wings takes a tag and delegates.
    check("german_wings accepts an explicit tag", "tag" in AW.german_wings.__code__.co_varnames)
    check("country_wings exists", callable(getattr(AW, "country_wings", None)))
    check("no 'id-1 block' guessing left in the source",
          "contiguous run 1..k" not in AW.german_wings.__doc__.replace("\n", " ")
          or "heuristic" in AW.german_wings.__doc__)

    # CAirWing ctor (0xFDDCE0) clears the pool slot at +0x38 (`mov [rdi+0x38],r13`)
    check("CAirWing ctor touches the pool slot at +0x38",
          has(0xFDDDD2, "4C 89 6F 38"))
    # and reads the owner-relevant dword through [rsi+0x20] + 0x410
    check("CAirWing ctor reads [.. +0x410]",
          has(0xFDDDC5, "8B 81 10 04 00 00"))


# ------------------------------------------------------------------- attach
def test_attach_wiring():
    print("\n[attach]  hoi4_attach_air_wings no longer refuses")
    import inspect
    mcp_dir = os.path.abspath(os.path.join(CALLING, "..", "mcp"))
    if mcp_dir not in sys.path:
        sys.path.insert(0, mcp_dir)
    import server as _srv
    blk = inspect.getsource(_srv._attach_air)
    desc = next(t["description"] for t in _srv.TOOLS if t["name"] == "hoi4_attach_air_wings")
    check("the win32 refusal gate is gone", "win32 and not a.get" not in blk)
    check("it always routes to the verified attach path",
          "_air_ger_attach(a)" in blk)
    check("the docstring no longer claims it refuses", "Refuses by default" not in desc)


if __name__ == "__main__":
    test_trade_win()
    test_air_ownership()
    test_attach_wiring()
    print("\n%s" % ("ALL PASS" if not FAILS else "FAILURES: %s" % FAILS))
    sys.exit(1 if FAILS else 0)
