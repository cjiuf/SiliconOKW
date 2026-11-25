"""
Collects country statistics from the game's own trigger functions.

Each C*Trigger class has a value getter in vtable+0xC0 (or +0xB8/+0x50)
    int64 GetValue(this, scope)
The country-scoped ones among them are detected statically
(the ones whose first instructions are `lea rdi,[rsi+0x8]; call CCountryTag::GetCountry`)
and called at runtime.
"""
from __future__ import annotations
import os, pickle, re, subprocess

import config
BIN = config.binary_path(required=False)
CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "stats.pkl")
GET_COUNTRY = 0xD77A90


def _dis(va, end, n=14):
    out = subprocess.run(["objdump", "-d", "--start-address=%#x" % va,
                          "--stop-address=%#x" % min(end, va + 0x100),
                          "-M", "intel", BIN], capture_output=True, text=True).stdout
    return [l.split("\t")[-1].strip() for l in out.splitlines()[7:]][:n]


def snake(cls):
    s = re.sub(r"^C", "", cls)
    s = re.sub(r"Trigger$", "", s)
    s = re.sub(r"(?<!^)(?=[A-Z])", "_", s).lower()
    return s


def build(force=False):
    """{stat_name: (function_vaddr, class)}"""
    import safe_cache
    if safe_cache.exists(CACHE) and not force:
        return safe_cache.load(CACHE)
    raise NotImplementedError(
        "Linux ELF/objdump trigger scanner removed; use prebuilt stats.json."
    )


if __name__ == "__main__":
    d = build(force=True)
    print("country-scoped statistics:", len(d))
    for k in sorted(d)[:60]:
        print("   %-44s %#x" % (k, d[k][0]))
