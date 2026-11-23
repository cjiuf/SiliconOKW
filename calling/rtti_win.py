"""MSVC RTTI map for the Windows build: class name -> vtable RVA.

The Linux build is analysed by `calling/rtti.py` (Itanium RTTI + `.rela.dyn`).
`hoi4.exe` is a PE with MSVC RTTI: every vtable is immediately preceded by a
`RTTICompleteObjectLocator`, which points at a `TypeDescriptor` whose name is the
decorated string `.?AVCState@@`.  `research/windows/rtti_win.py` walks the image
and writes that relation to a pickle; this module loads it and exposes the same
public surface as `rtti.py`, so `game.py` / `objindex.py` do not need to know
which build they are talking to:

    build() -> {"vtables": {class_name: [vtable_rva, ...]}, "vt": {rva: name}}

All addresses are **RVAs** (image-relative), exactly like the values in
`offsets_win.py`; `mem.Proc.va()` adds the ASLR base.

Names are reduced to the plain class name (`CState`, `TGameItemDatabase`) so
`ObjectIndex.instances("CBuildingDatabase")` works without the MSVC decoration.
Instances of one class may own several vtables (multiple inheritance, or a
second vtable emitted for a different construction); all of them are returned,
which is what makes the memory scan complete.
"""
import os
import pickle
import re

import config

# ---------------------------------------------------------------- pickle
# The scanner lives in research/windows/ and writes next to itself; earlier
# runs put the pickle in the stage root. Accept any of those, and an explicit
# override, so a checkout does not have to care where the dump was produced.
def _candidates():
    env = os.environ.get("CLAUSEWITZ_RTTI_WIN")
    if env:
        yield env
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(here)
    yield os.path.join(here, "rtti_win.pkl")
    yield os.path.join(root, "research", "windows", "rtti_win.pkl")
    try:
        yield config.dump("rtti_win.pkl")
    except Exception:
        pass
    yield os.path.join(root, "rtti_win.pkl")
    yield os.path.join(os.path.dirname(root), "rtti_win.pkl")


def pickle_path():
    for p in _candidates():
        if p and os.path.isfile(p):
            return p
    raise RuntimeError(
        "rtti_win.pkl not found (the MSVC RTTI map of hoi4.exe).\n"
        "Generate it with:  python research/windows/rtti_win.py\n"
        "or point CLAUSEWITZ_RTTI_WIN at an existing dump.")


# ---------------------------------------------------------------- names
# .?AV<name>@@            class            -> <name>
# .?AU<name>@@            struct           -> <name>
# .?AV?$T@VArg@@@@        class template   -> T
_DECORATED = re.compile(r"^\.\?A[VU]")


def plain_name(decorated):
    """`.?AVCState@@` -> `CState`; template names keep only the template name."""
    s = _DECORATED.sub("", decorated)
    if s.startswith("?$"):
        s = s[2:]
        return s.split("@", 1)[0]
    return s.split("@@", 1)[0].split("@", 1)[0]


_MAP = None


def build(path=None):
    """{class_name: [vtable_rva,...]} for every class in the image (cached).

    第二期数据驱动优先链：
      1. 未指定 path 时，优先从 `calling/profile.py::load_rtti()` 加载与当前 hoi4.exe
         指纹匹配的 Rust `hoi4_recalc` 导出 JSON（零 pickle 版本依赖）；
      2. 若无匹配的 JSON，平滑回落到原有的 `research/windows/rtti_win.pkl`。
    """
    global _MAP
    if _MAP is not None and path is None:
        return _MAP
    if path is None:
        try:
            import profile as _prof
            from_json = _prof.load_rtti()
            if from_json and from_json.get("vtables"):
                _MAP = from_json
                return _MAP
        except Exception:
            pass
    p = path or pickle_path()
    with open(p, "rb") as f:
        raw = pickle.load(f)
    classes = {}
    for dec, vts in raw["classes"].items():
        classes.setdefault(plain_name(dec), []).extend(vts)
    for v in classes.values():
        v.sort()
    out = {
        "vtables": classes,
        "vt": {int(k): plain_name(v) for k, v in raw["vt"].items()},
        "pe": raw.get("pe"),
        "source": p,
    }
    if path is None:
        _MAP = out
    return out


def class_names():
    return sorted(build()["vtables"])


if __name__ == "__main__":
    import sys
    m = build()
    print("pickle        :", m["source"])
    print("classes       :", len(m["vtables"]))
    print("vtables       :", len(m["vt"]))
    pats = sys.argv[1:] or ["CState", "CBuilding", "CBuildingDatabase", "CCountry"]
    for pat in pats:
        hits = [k for k in m["vtables"] if pat in k]
        print("--- %s: %d ---" % (pat, len(hits)))
        for h in sorted(hits)[:12]:
            print("   %-40s %s" % (h, " ".join("%#x" % v for v in m["vtables"][h][:4])))
