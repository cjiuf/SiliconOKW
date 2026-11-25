# -*- coding: utf-8 -*-
"""阶段 3 WP3 单元测试：safe_cache 安全序列化、SHA256 防篡改与 0 跟踪 .pkl 验收。"""
import os
import pickle
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CALLING = os.path.join(ROOT, "calling")
if CALLING not in sys.path:
    sys.path.insert(0, CALLING)

import safe_cache as SC

MIGRATED_ASSETS = [
    "calling/cmdctors.json",
    "calling/cmdfields.json",
    "calling/cmdsigs.json",
    "calling/diploactions.json",
    "calling/fieldmaps.json",
    "calling/focusplans.json.gz",
    "calling/mapadj.json",
    "calling/maprivers.json",
    "calling/mapstates.json",
    "calling/navalregions.json",
    "calling/pecmds.json",
    "calling/stats.json",
    "calling/techdefs.json",
    "calling/tokens.json.gz",
    "research/windows/blddb_win.json",
    "research/windows/eqtypes_win.json",
    "research/windows/rtti_win.json.gz",
    "research/windows/techdb_win.json",
]


class TestSafeCache(unittest.TestCase):
    def setUp(self):
        self._old_no_pkl = os.environ.get("HOI4_NO_PICKLE")
        self._old_strict = os.environ.get("HOI4_STRICT_PICKLE_HASH")
        self._old_state = os.environ.get("HOI4_STATE_DIR")
        os.environ.pop("HOI4_NO_PICKLE", None)
        os.environ.pop("HOI4_STRICT_PICKLE_HASH", None)

    def tearDown(self):
        for k, v in (
            ("HOI4_NO_PICKLE", self._old_no_pkl),
            ("HOI4_STRICT_PICKLE_HASH", self._old_strict),
            ("HOI4_STATE_DIR", self._old_state),
        ):
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_zero_tracked_pkl_in_git_and_all_18_load_without_pickle(self):
        tracked = subprocess.check_output(
            ["git", "ls-files", "*.pkl"], cwd=ROOT, text=True
        ).splitlines()
        self.assertEqual(tracked, [], f"仓库不应再跟踪任何 .pkl 文件，实际残留: {tracked}")

        os.environ["HOI4_NO_PICKLE"] = "1"
        for rel in MIGRATED_ASSETS:
            full = os.path.join(ROOT, rel)
            self.assertTrue(os.path.isfile(full), f"缺少迁移后的安全资产: {rel}")
            # 用旧的 .pkl 虚拟路径也能透明解析到 .json / .json.gz 且零 pickle 加载
            stem = full[:-8] if full.endswith(".json.gz") else full[:-5]
            obj = SC.load(stem + ".pkl")
            self.assertIsNotNone(obj)

    def test_lossless_roundtrip_all_python_types(self):
        from array import array
        sample = {
            "plain": {"a": 1, "b": [True, False, None, 3.14, "中文"]},
            "int_keys": {100: {1, 2, 3}, 200: (10, "x")},
            "tuple_keys": {(1, 2): "small", (3, 4): "large"},
            "escaped_dict": {"__t__": "user_value", "v": 42},
            "escaped_array_dict": {"__t__": "array", "tc": "q", "v": "literal"},
            "empty_dict": {},
            "empty_set": set(),
            "nested_tuple": ((1, 2), ("a", {9, 8})),
            "raw_bytes": bytes(range(256)),
            "empty_bytes": b"",
            "int64_arr": array("q", [-10, 0, 123456789012345]),
            "uint64_arr": array("Q", [0, 0x7FF600001234, (1 << 63) + 7]),
            "empty_arr": array("q"),
        }
        with tempfile.TemporaryDirectory() as td:
            for fmt in ("json", "json.gz"):
                p = os.path.join(td, "sample.pkl")
                saved = SC.save(p, sample, fmt=fmt)
                got = SC.load(saved, allow_pickle=False)
                self.assertEqual(got, sample)
                self.assertIsInstance(got["int_keys"][100], set)
                self.assertIsInstance(got["int_keys"][200], tuple)
                self.assertIsInstance(got["nested_tuple"], tuple)
                self.assertEqual(got["tuple_keys"][(1, 2)], "small")
                self.assertEqual(got["escaped_dict"], {"__t__": "user_value", "v": 42})
                self.assertEqual(got["escaped_array_dict"], {"__t__": "array", "tc": "q", "v": "literal"})
                self.assertIsInstance(got["raw_bytes"], bytes)
                self.assertEqual(got["raw_bytes"], bytes(range(256)))
                self.assertEqual(got["empty_bytes"], b"")
                self.assertIsInstance(got["int64_arr"], array)
                self.assertEqual(got["int64_arr"].typecode, "q")
                self.assertEqual(list(got["int64_arr"]), [-10, 0, 123456789012345])
                self.assertEqual(got["uint64_arr"].typecode, "Q")
                self.assertEqual(list(got["uint64_arr"]), [0, 0x7FF600001234, (1 << 63) + 7])
                self.assertEqual(len(got["empty_arr"]), 0)

    def test_objindex_disk_cache_runs_under_ho4_no_pickle(self):
        """验证 ObjectIndex 本地磁盘缓存落盘 .json.gz 且在 HOI4_NO_PICKLE=1 下全速命中与增量刷新。"""
        import struct as _st
        import mem
        import objindex

        class _FakeBe:
            def __init__(self, base, blobs):
                self.pid = 7788
                self.image_base = base
                self.image_size = 0x4000000
                self.blobs = blobs
                self.read_into_calls = 0

            def regions(self):
                return []

            def read(self, addr, size):
                for b, data in self.blobs.items():
                    if b <= addr < b + len(data):
                        off = addr - b
                        return data[off:off + size]
                raise OSError(299, "bad addr")

            def read_into(self, addr, buf, size=None):
                self.read_into_calls += 1
                data = self.read(addr, len(buf) if size is None else size)
                buf[:len(data)] = data
                return len(data)

        base = 0x7FF600000000
        blob1 = bytearray(4096)
        _st.pack_into("<Q", blob1, 0x00, base + 0x1000)
        blob2 = bytearray(4096)
        _st.pack_into("<Q", blob2, 0x40, base + 0x1000)
        r1 = dict(start=0x10000000, end=0x10001000, perms="rw-", path="", type=0x20000)
        r2 = dict(start=0x10002000, end=0x10003000, perms="rw-", path="", type=0x20000)

        p = object.__new__(mem.Proc)
        p.exe_name = "hoi4.exe"
        p.be = _FakeBe(base, {r1["start"]: bytes(blob1), r2["start"]: bytes(blob2)})
        p.pid = 7788
        p.base = base
        p.image_size = 0x4000000
        p.maps = [r1]

        os.environ["HOI4_NO_PICKLE"] = "1"
        with tempfile.TemporaryDirectory() as td:
            cpath = os.path.join(td, "objindex.pkl")  # 即便传入旧 .pkl 路径也应透明升级为 .json.gz
            oi = objindex.ObjectIndex(p, {"CBuildingDatabase": [0x1000]}, cache_path=cpath)
            oi.build(force=True)
            self.assertFalse(os.path.exists(cpath), "不应再落盘 .pkl 文件")
            self.assertTrue(os.path.isfile(os.path.join(td, "objindex.json.gz")))

            # 新增堆区后新建 ObjectIndex：在 HOI4_NO_PICKLE=1 下命中 .json.gz 缓存且仅增量扫 r2
            p.maps = [r1, r2]
            p.be.read_into_calls = 0
            oi2 = objindex.ObjectIndex(p, {"CBuildingDatabase": [0x1000]}, cache_path=cpath)
            idx2 = oi2.build()
            self.assertEqual(list(idx2[base + 0x1000]), [0x10000000, 0x10002040])
            self.assertEqual(p.be.read_into_calls, 1)

    def test_no_pickle_blocks_pkl_and_tamper_triggers_integrity_error(self):
        with tempfile.TemporaryDirectory() as td:
            os.environ["HOI4_STATE_DIR"] = td
            pkl_path = os.path.join(td, "local_only.pkl")
            with open(pkl_path, "wb") as fh:
                pickle.dump({"hello": 123}, fh)
            SC.record_pickle_hash(pkl_path)

            # 1) 正常哈希校验通过
            self.assertEqual(SC.load(pkl_path), {"hello": 123})

            # 2) HOI4_NO_PICKLE=1 严格拒载
            os.environ["HOI4_NO_PICKLE"] = "1"
            with self.assertRaises(SC.UnsafePickleError):
                SC.load(pkl_path)
            os.environ.pop("HOI4_NO_PICKLE", None)

            # 3) 篡改 .pkl 任意字节 -> 立即抛 CacheIntegrityError
            with open(pkl_path, "wb") as fh:
                pickle.dump({"hello": 999}, fh)
            with self.assertRaises(SC.CacheIntegrityError):
                SC.load(pkl_path)

    def test_untrusted_pkl_cannot_hijack_json_but_verified_newer_mod_pkl_can(self):
        with tempfile.TemporaryDirectory() as td:
            os.environ["HOI4_STATE_DIR"] = td
            base = os.path.join(td, "table.pkl")
            jp = SC.save(base, {"source": "safe_json"}, fmt="json")
            time.sleep(0.02)

            # 放置一个未登记哈希但时间戳更新的陌生 .pkl -> 绝不能抢占 .json
            with open(base, "wb") as fh:
                pickle.dump({"source": "rogue_pkl"}, fh)
            self.assertEqual(SC.load(base), {"source": "safe_json"})
            self.assertEqual(SC.resolve_path(base), jp)

            # 一旦通过本地信任链登记 SHA256（如 assets.rebuild 后），可优先生效
            SC.record_pickle_hash(base)
            self.assertEqual(SC.load(base), {"source": "rogue_pkl"})

            # 开启 HOI4_NO_PICKLE=1 后重新锁定为纯 JSON
            os.environ["HOI4_NO_PICKLE"] = "1"
            self.assertEqual(SC.load(base), {"source": "safe_json"})


if __name__ == "__main__":
    unittest.main()
