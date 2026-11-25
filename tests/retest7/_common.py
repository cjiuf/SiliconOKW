# -*- coding: utf-8 -*-
"""复测 7~11 离线回归测试共享夹具（纯机械迁移自 test_retest7_and_macros_20261123.py）。"""
import json
import os
import struct
import sys
import tempfile
import time
import types
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_CALLING = os.path.join(ROOT, "calling")
_MCP = os.path.join(ROOT, "mcp")
if _CALLING not in sys.path:
    sys.path.insert(0, _CALLING)
if _MCP not in sys.path:
    sys.path.insert(0, _MCP)

import armyops_win as AW
import cmdwin
import diplomacy as DIP
import game as game_mod
import housekeep as HK
import macros as MC
import peacewin as PW
import reddots as RD
import server as MCP
import staff_supply as SS
import turnengine as TE
import turnloop as TL

PASS = 0
FAIL = 0
FAILURES = []


def reset_counts():
    global PASS, FAIL
    PASS = 0
    FAIL = 0
    FAILURES.clear()


def counts():
    return PASS, FAIL


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] %s" % name)
    else:
        FAIL += 1
        msg = "%s%s" % (name, (" -- " + str(detail)) if detail else "")
        FAILURES.append(msg)
        print("  [FAIL] %s" % msg)


class _MemProc:
    def __init__(self):
        self.pid = 77701
        self.base = 0x7FF600000000
        self.mem = {}

    def put_bytes(self, addr, data):
        for i, b in enumerate(data):
            self.mem[addr + i] = b

    def write(self, addr, data):
        self.put_bytes(addr, data)

    def put_u64(self, addr, val):
        self.put_bytes(addr, struct.pack("<Q", val))

    def put_i32(self, addr, val):
        self.put_bytes(addr, struct.pack("<i", val))

    def read(self, addr, n):
        return bytes(self.mem.get(addr + i, 0) for i in range(n))

    def try_read(self, addr, n):
        return self.read(addr, n)

    def stdstring(self, addr):
        n = self.u64(addr + 0x10)
        cap = self.u64(addr + 0x18)
        if not (0 < n < 256):
            return None
        b = self.read(addr, n) if cap <= 15 else self.read(self.u64(addr), n)
        return b.decode("utf-8", "replace")

    def u64(self, addr):
        return struct.unpack("<Q", self.read(addr, 8))[0]

    def i64(self, addr):
        return struct.unpack("<q", self.read(addr, 8))[0]

    def i32(self, addr):
        return struct.unpack("<i", self.read(addr, 4))[0]

    def u32(self, addr):
        return struct.unpack("<I", self.read(addr, 4))[0]

    def u8(self, addr):
        return self.mem.get(addr, 0)

