# -*- coding: utf-8 -*-
"""2026-11-26 缺陷复现清单全量离线单元测试（零 Win32 / 零活进程）。

覆盖清单全部真实缺陷的回归验证：
  - P0-1: cmdwin.post_drained 先入队后解暂停消竞态 + trade_win 交战拦截/幂等/死进程不伪造 0.0
  - P0-3: peacewin.submit_demands 严格保序、默认追加合并去重、replace=True 覆盖、finish 州归属回读
  - P1-1: airops_win.airbase_wings / country_wings 精确读机场停驻运输机 + paradrop_targets 优先 ready_sources
  - P1-2: battleplan.plan_front 持有伞降单但仍有自由师的集团军不被跳过，且绝不抢夺伞兵师
  - P1-3: validate_orders_group_ptr / validate_division_ptrs 过期指针 stale_pointer 诊断
  - P2-1/P2-2/P2-3/P3-5: 非伞兵师 0 注入拦截、空机场 0 注入拦截、重复伞降目标冲突拦截、修正文案
  - P2-4: upgrade_production 船坞线明确引导 create_production_line + create_production_line 走 post_drained + 简报 [坞]
  - P3-2/P3-4: autopilot fuel.peacetime_mode="save" 归一 + turnengine housekeep budget_s 透传
"""
from __future__ import annotations

import os
import struct
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CALLING = os.path.join(ROOT, "calling")
if CALLING not in sys.path:
    sys.path.insert(0, CALLING)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


class _MemProc:
    """轻量可读写内存模拟器（严格模拟 ReadProcessMemory 不可读异常）。"""

    def __init__(self, base=0x140000000, pid=99001):
        self.base = int(base)
        self.pid = int(pid)
        self.mem = bytearray()
        self.chunks = {}
        self.alive = True
        self.write_log = []
        # 放一个合法 MZ 头给 _proc_alive 探活
        self.put(self.base, b"MZ\x90\x00")

    def put(self, addr, data: bytes):
        self.chunks[int(addr)] = bytearray(data)

    def _locate(self, addr, size):
        addr = int(addr)
        for start, buf in self.chunks.items():
            if start <= addr and addr + size <= start + len(buf):
                return buf, addr - start
        raise OSError("[Errno 299] ReadProcessMemory(0x%x, %d) failed (GetLastError=299)" % (addr, size))

    def read(self, addr, size):
        if not self.alive:
            raise OSError("[Errno 6] ReadProcessMemory failed: process exited (GetLastError=6)")
        buf, off = self._locate(addr, size)
        return bytes(buf[off:off + size])

    def write(self, addr, data: bytes):
        if not self.alive:
            raise OSError("[Errno 6] WriteProcessMemory failed: process exited")
        self.write_log.append((int(addr), bytes(data)))
        addr = int(addr)
        for start, buf in self.chunks.items():
            if start <= addr and addr + len(data) <= start + len(buf):
                off = addr - start
                buf[off:off + len(data)] = data
                return
        self.chunks[addr] = bytearray(data)

    def u8(self, addr):
        return self.read(addr, 1)[0]

    def i32(self, addr):
        return struct.unpack("<i", self.read(addr, 4))[0]

    def u32(self, addr):
        return struct.unpack("<I", self.read(addr, 4))[0]

    def i64(self, addr):
        return struct.unpack("<q", self.read(addr, 8))[0]

    def u64(self, addr):
        return struct.unpack("<Q", self.read(addr, 8))[0]

    def va(self, rva):
        return self.base + int(rva)


class TestP0TradeAndPostDrained(unittest.TestCase):
    """P0-1: post_drained 先入队后解暂停 + trade_win 交战拦截与进程死亡防伪造 0.0。"""

    def test_post_drained_enqueues_while_still_paused(self):
        import cmdwin
        import session as S

        p = _MemProc()
        idler = 0x20000000
        p.put(p.base + S.G_IDLER, struct.pack("<Q", idler))
        p.put(idler + S.IDLER_PAUSED, b"\x01")  # 初始为暂停态

        events = []
        date_val = [100]

        class _FakeGame:
            def __init__(self, proc):
                self.p = proc

            def date_raw(self):
                return date_val[0]

        g = _FakeGame(p)

        def _fake_enqueue(_g, _cmd):
            # 断言：调用 enqueue 时游戏仍必须是暂停态（idler+IDLER_PAUSED == 1）
            paused_now = bool(p.u8(idler + S.IDLER_PAUSED))
            events.append(("enqueue", paused_now))
            return 1

        orig_write = p.write

        def _track_write(addr, data):
            if addr == idler + S.IDLER_PAUSED:
                events.append(("pause_write", data[0]))
            return orig_write(addr, data)

        p.write = _track_write

        checks = [0]

        def _done_fn():
            checks[0] += 1
            return checks[0] >= 1

        with mock.patch.object(cmdwin, "enqueue", side_effect=_fake_enqueue):
            res = cmdwin.post_drained(g, 0x30000000, seconds=1.0, done_fn=_done_fn)

        self.assertEqual(res["posted"], 1)
        self.assertTrue(res["was_paused"])
        self.assertTrue(res["drained"])
        # 事件顺序必须是：暂停态下 enqueue -> 写 0 解暂停 -> done_fn 命中 -> 写 1 恢复暂停
        self.assertEqual(events[0], ("enqueue", True))
        self.assertEqual(events[1], ("pause_write", 0))
        self.assertEqual(events[-1], ("pause_write", 1))
        self.assertEqual(p.u8(idler + S.IDLER_PAUSED), 1)

    def test_trade_blocks_at_war_and_short_circuits_idempotent(self):
        import trade_win as TW
        import combat

        p = _MemProc()
        exp_ptr = 0x21000000000
        res_obj = 0x21000010000
        outer = 0x21000020000
        slot_data = 0x21000030000
        ex_ptr = 0x21000040000
        # 构造 SIA 正在向 GER (tag_id=1) 出口 8.0 单位橡胶 (slot=3)
        p.put(exp_ptr + TW.COUNTRY_RESOURCES, struct.pack("<Q", res_obj))
        p.put(res_obj + TW.TRADE_BOOK, struct.pack("<Qii", outer, 8, 8))
        p.put(outer, b"\x00" * (8 * TW.SLOT_STRIDE))
        p.write(outer + 3 * TW.SLOT_STRIDE, struct.pack("<Qii", slot_data, 1, 1))
        p.put(slot_data, struct.pack("<Q", ex_ptr))
        p.put(ex_ptr + TW.EX_PARTNER, struct.pack("<IIQII", 1, 0, 0x21000050000, int(8 * TW.FIXED), 0))

        class _FakeGame:
            def __init__(self):
                self.p = p

            def _c(self, tag):
                t = str(tag or "GER").upper()
                if t == "GER":
                    return SimpleNamespace(tag="GER", tag_id=1, ptr=0x20000000)
                return SimpleNamespace(tag=t, tag_id=2, ptr=exp_ptr)

            def tags(self):
                return ["GER", "SIA", "BRA"]

            def resources(self, tag):
                return {"rubber": 55.0}

        g = _FakeGame()
        TW._RES_PTR_CACHE[p.pid] = {k: 0x21050000 + i * 0x100 for i, k in enumerate(TW.RESOURCE_NAMES)}

        # 1) 交战拦截（0 注入）
        with mock.patch.object(combat, "enemy_tags", return_value=["SIA"]):
            r_war = TW.create_trade(g, "SIA", "rubber", 16)
        self.assertFalse(r_war["ok"])
        self.assertEqual(r_war["blocked_by"], "at_war")

        # 2) 已等于 8 单位时幂等短路
        with mock.patch.object(combat, "enemy_tags", return_value=[]):
            r_idem = TW.create_trade(g, "SIA", "rubber", 8)
        self.assertTrue(r_idem["ok"])
        self.assertTrue(r_idem["already_at_target"])
        self.assertFalse(r_idem["posted"])
        self.assertEqual(r_idem["amount_after"], 8.0)

    def test_trade_does_not_fake_zero_when_process_crashes(self):
        import trade_win as TW
        import combat
        import win_pipeline as WP

        p = _MemProc()
        exp_ptr = 0x22000000000
        res_obj = 0x22000010000
        outer = 0x22000020000
        slot_data = 0x22000030000
        ex_ptr = 0x22000040000
        p.put(exp_ptr + TW.COUNTRY_RESOURCES, struct.pack("<Q", res_obj))
        p.put(res_obj + TW.TRADE_BOOK, struct.pack("<Qii", outer, 8, 8))
        p.put(outer, b"\x00" * (8 * TW.SLOT_STRIDE))
        p.write(outer + 3 * TW.SLOT_STRIDE, struct.pack("<Qii", slot_data, 1, 1))
        p.put(slot_data, struct.pack("<Q", ex_ptr))
        p.put(ex_ptr + TW.EX_PARTNER, struct.pack("<IIQII", 1, 0, 0x22000050000, int(16 * TW.FIXED), 0))

        class _FakeGame:
            def __init__(self):
                self.p = p

            def _c(self, tag):
                t = str(tag or "GER").upper()
                if t == "GER":
                    return SimpleNamespace(tag="GER", tag_id=1, ptr=0x20000000)
                return SimpleNamespace(tag=t, tag_id=2, ptr=exp_ptr)

            def tags(self):
                return ["GER", "SIA"]

            def resources(self, tag):
                return {"rubber": 55.0}

        g = _FakeGame()
        TW._RES_PTR_CACHE[p.pid] = {k: 0x22050000 + i * 0x100 for i, k in enumerate(TW.RESOURCE_NAMES)}

        def _crash_run(self_pipe, **kwargs):
            # 模拟命令投递后游戏进程崩溃退出
            p.alive = False
            return WP.CommandResult(ok=True, cmd=0x22090000, can_execute=True, posted=1, drained=True)

        with mock.patch.object(combat, "enemy_tags", return_value=[]), \
             mock.patch.object(WP.WinCommandPipeline, "run", new=_crash_run):
            r = TW.create_trade(g, "SIA", "rubber", 24)

        self.assertFalse(r["ok"])
        self.assertIs(r["alive"], False)
        self.assertIsNone(r["amount_after"])
        self.assertEqual(r["blocked_by"], "process_exited")


class TestP0PeaceDemandsAndFinish(unittest.TestCase):
    """P0-3: peace_demands 保序、默认追加合并去重、replace=True 覆盖、finish 州归属回读。"""

    def _setup_peace_env(self):
        import peacewin as PW
        import cmdwin

        p = _MemProc()
        conf = 0x30000000
        p.put(conf, b"\x00" * 0x280)
        # 构造 +0x1F8 空红黑树 head 哨兵
        head = 0x30001000
        p.put(head, struct.pack("<QQQ", head, head, head))
        p.write(conf + PW.CONF_SUBMIT_MAP, struct.pack("<QQ", head, 0))
        # 构造 participants 数组：winner=1 拥有 score=500, score_now=250
        part_arr = 0x30002000
        part_obj = 0x30002100
        p.put(part_obj, b"\x00" * 0x60)
        p.write(part_obj + 0x08, struct.pack("<i", 1))
        p.write(part_obj + 0x48, struct.pack("<ii", 250, 500))
        p.put(part_arr, struct.pack("<Q", part_obj))
        p.write(conf + 0xF8, struct.pack("<Qii", part_arr, 1, 1))

        # 构造模拟堆分配器
        heap_cursor = [0x31000000]

        def _fake_alloc(_g, size):
            addr = heap_cursor[0]
            heap_cursor[0] += (int(size) + 0x3F) & ~0x3F
            p.put(addr, b"\x00" * max(0x40, int(size)))
            return addr

        def _make_state_act(winner_idx, loser_idx, sid, cost):
            a = _fake_alloc(None, 0x50)
            p.write(a + 8, struct.pack("<i", PW.ACT_TYPE_TAKE_STATE))
            p.write(a + 0x30, struct.pack("<iii", winner_idx, loser_idx, winner_idx))
            p.write(a + PW.ACT_COST, struct.pack("<i", cost))
            p.write(a + 0x48, struct.pack("<i", sid))
            return a

        return p, conf, _fake_alloc, _make_state_act

    def test_submit_demands_preserves_order_and_appends_across_losers(self):
        import peacewin as PW
        import cmdwin

        p, conf, fake_alloc, make_act = self._setup_peace_env()

        # INS 可选州按升序返回 [334, 335, 672]，HOL 返回 [7, 35, 36]
        ins_acts = {334: make_act(1, 20, 334, 60), 335: make_act(1, 20, 335, 80), 672: make_act(1, 20, 672, 90)}
        hol_acts = {7: make_act(1, 10, 7, 100), 35: make_act(1, 10, 35, 50), 36: make_act(1, 10, 36, 40)}

        class _FakeGame:
            def __init__(self):
                self.p = p
                self.owned_ger = {64, 65}
                self.state_owners = {64: "GER", 65: "GER", 672: "INS", 335: "INS", 334: "INS", 7: "HOL", 35: "HOL", 36: "HOL"}

            def tags(self):
                return ["GER", "HOL", "INS"]

            def _c(self, t):
                m = {"GER": 1, "HOL": 10, "INS": 20}
                return SimpleNamespace(tag=str(t).upper(), tag_id=m[str(t).upper()], ptr=0x20000000)

            def player_tag(self):
                return "GER"

            def date(self):
                return {"text": "1936.12.01.01"}

            def states(self, tag=None):
                return [{"id": sid} for sid in sorted(self.owned_ger)]

            def state_detail(self, sid):
                return {"id": sid, "owner": self.state_owners.get(sid), "name": "State_%d" % sid}

        g = _FakeGame()

        def _fake_build_actions(_g, loser_idx, kind="state", winner_idx=None):
            if loser_idx == 20:
                return [ins_acts[334], ins_acts[335], ins_acts[672]]
            if loser_idx == 10:
                return [hol_acts[7], hol_acts[35], hol_acts[36]]
            return []

        with mock.patch.object(cmdwin, "alloc", side_effect=fake_alloc), \
             mock.patch.object(PW, "build_actions", side_effect=_fake_build_actions), \
             mock.patch.object(PW, "live_conference", return_value=conf):
            # 第 1 次：向 INS 传入高价值优先顺序 [672, 335]
            f1 = {}
            _, acts1 = PW.submit_demands(g, conf, 1, 20, states=[672, 335], filter_info=f1)
            self.assertEqual(f1["matched_states"], [672, 335])
            self.assertEqual([p.i32(a + 0x48) for a in acts1], [672, 335])
            self.assertEqual(f1["mode"], "append")
            self.assertEqual(f1["estimated_total_cost"], 170)
            self.assertFalse(f1["over_budget"])

            # 第 2 次：向 HOL 提诉求 [7, 35] -> 必须追加在 INS 后，绝不覆写 INS！
            f2 = {}
            _, acts2 = PW.submit_demands(g, conf, 1, 10, states=[7, 35], filter_info=f2)
            self.assertEqual([p.i32(a + 0x48) for a in acts2], [672, 335, 7, 35])
            self.assertEqual(f2["previous_actions"], 2)
            self.assertEqual(f2["added_actions"], 2)
            self.assertEqual(f2["total_queued_actions"], 4)
            self.assertEqual(f2["queued_states_by_loser"], {"INS": [672, 335], "HOL": [7, 35]})
            self.assertEqual(f2["estimated_total_cost"], 320)
            self.assertTrue(f2["over_budget"])
            self.assertEqual(PW.map_size(g, conf, PW.CONF_SUBMIT_MAP), 1)

            # 第 3 次：重复交 335 并新增 334 -> 自动去重 335，只新增 334
            f3 = {}
            _, acts3 = PW.submit_demands(g, conf, 1, 20, states=[335, 334], filter_info=f3)
            self.assertEqual([p.i32(a + 0x48) for a in acts3], [672, 335, 7, 35, 334])
            self.assertEqual(f3["skipped_duplicate_actions"], 1)
            self.assertEqual(f3["added_actions"], 1)

            # 第 4 次：测试 finish 回读（模拟引擎结算后拿到 672/335/7，漏掉 35/334）
            def _fake_exec(_g, what, _idx):
                g.owned_ger |= {672, 335, 7}
                for sid in (672, 335, 7):
                    g.state_owners[sid] = "GER"
                p.write(conf + 0xDD, b"\x01")
                p.write(conf + 0x1C8 + 8, struct.pack("<Q", 1))
                p.write(conf + 0x1F8 + 8, struct.pack("<Q", 0))
                return {"ran": what, "result": 1}

            with mock.patch.object(PW, "execute", side_effect=_fake_exec):
                fin = PW.finish(g, 1)
            self.assertEqual(fin["states_before_count"], 2)
            self.assertEqual(fin["states_after_count"], 5)
            self.assertEqual(fin["net_states_gained"], [7, 335, 672])
            self.assertEqual(fin["acquired_states"], [672, 335, 7])
            self.assertEqual([x["state"] for x in fin["not_acquired_states"]], [35, 334])
            self.assertTrue(fin["partial_acquisition"])
            self.assertIn("acquisition_warning", fin)

            # 验证 replace=True 可覆盖队列
            p.write(conf + 0xDD, b"\x00")
            p.write(conf + 0x1F8 + 8, struct.pack("<Q", 1))
            f4 = {}
            _, acts4 = PW.submit_demands(g, conf, 1, 10, states=[36], filter_info=f4, replace=True)
            self.assertEqual(f4["mode"], "replace")
            self.assertEqual([p.i32(a + 0x48) for a in acts4], [36])


class TestP1AndP2AirbaseParadropAndFront(unittest.TestCase):
    """P1-1 / P1-2 / P1-3 / P2-1 / P2-2 / P2-3 / P3-5 全链路测试。"""

    def test_airbase_wings_maps_exact_transport_station_and_range(self):
        import airops_win as AW
        import offsets_win as OW

        p = _MemProc()
        gs = 0x40000000
        sam = 0x40010000
        lp_arr = 0x40020000
        prov64 = 0x40025000
        sa_arr = 0x40030000
        sa = 0x40031000
        abm = 0x40040000
        ab_arr = 0x40041000
        ab64 = 0x40042000
        c_arr = 0x40043000
        abc = 0x40044000
        pools_arr = 0x40045000
        pool = 0x40046000
        def_ptr = 0x40047000
        wings_arr = 0x40048000
        wing = 0x40049000

        p.put(p.base + OW.G_GAMESTATE, struct.pack("<Q", gs))
        p.put(gs + AW.GS_STRAT_AIR_MGR, struct.pack("<Q", sam))

        # sam + 0x48 陆地机场省表（state 64 -> [9496]）
        p.put(sam + AW.SAM_LAND_AIRBASE_PROVS, struct.pack("<Qii", lp_arr, 70, 70))
        p.put(lp_arr, b"\x00" * (70 * 0x18))
        p.put(prov64, struct.pack("<i", 9496))
        p.write(lp_arr + 64 * 0x18, struct.pack("<Qii", prov64, 1, 1))

        # sam + 0x30 -> sa[1] (GER tag_id=1)
        p.put(sam + AW.SAM_STRAT_AIRS, struct.pack("<Qii", sa_arr, 4, 4))
        p.put(sa_arr, b"\x00" * 32)
        p.write(sa_arr + 8 * 1, struct.pack("<Q", sa))
        p.put(sa, b"\x00" * 0xA0)
        p.write(sa + AW.SA_OWNER, struct.pack("<I", 1))
        p.write(sa + AW.SA_AIRBASE_MGR, struct.pack("<Q", abm))

        # abm + 0x90 -> airbases[64] = ab64
        p.put(abm + AW.ABM_AIRBASES, struct.pack("<Qii", ab_arr, 70, 70))
        p.put(ab_arr, b"\x00" * (70 * 8))
        p.write(ab_arr + 64 * 8, struct.pack("<Q", ab64))

        # ab64 + 0x30 -> [1] = abc -> +0xB8 pools -> [pool] -> wings -> [wing]
        p.put(ab64 + AW.AB_COUNTRIES, struct.pack("<Qii", c_arr, 4, 4))
        p.put(c_arr, b"\x00" * 32)
        p.write(c_arr + 8 * 1, struct.pack("<Q", abc))
        p.put(abc + AW.ABC_POOLS, struct.pack("<Qii", pools_arr, 1, 1))
        p.put(pools_arr, struct.pack("<Q", pool))

        # pool + 0x20 -> def (name="transport_plane", +0x5A8 = 0x100000)
        p.put(pool, b"\x00" * 0x50)
        p.write(pool + AW.AWP_DEF, struct.pack("<Q", def_ptr))
        p.write(pool + AW.AWP_WINGS, struct.pack("<Qii", wings_arr, 1, 1))
        p.write(pool + 0x40, struct.pack("<Q", sa))
        p.put(def_ptr, b"\x00" * 0x5C0)
        tname = b"transport_plane"
        p.write(def_ptr + 0x18, tname + b"\x00" * (16 - len(tname)))
        p.write(def_ptr + 0x18 + 0x10, struct.pack("<QQ", len(tname), 15))
        p.write(def_ptr + AW.AWDEF_MISSION_FLAGS, struct.pack("<Q", AW.AWDEF_PARADROP_BIT))

        # wing: 80 架空闲运输机，航程 1000.0 km
        p.put(wings_arr, struct.pack("<Q", wing))
        p.put(wing, b"\x00" * 0x520)
        p.write(wing, struct.pack("<Q", p.base + AW.VT_AIRWING_MAIN))
        p.write(wing + 0x18, struct.pack("<II", 69, 15))
        p.write(wing + 0x38, struct.pack("<Q", pool))
        p.write(wing + AW.WING_MIN_0x60, struct.pack("<ii", 1, 1))
        p.write(wing + AW.WING_BUSY_0x79, b"\x00")
        p.write(wing + AW.WING_PLANES, struct.pack("<I", 80))
        p.write(wing + AW.WING_MISSION, struct.pack("<II", 0, 0))
        p.write(wing + AW.WING_RANGE, struct.pack("<q", 100_000_000))

        g = SimpleNamespace(p=p, _c=lambda t=None: SimpleNamespace(tag="GER", tag_id=1))
        ab = AW.airbase_wings(g, "GER")
        self.assertTrue(ab["ok"])
        self.assertEqual(ab["state_provinces"][64], [9496])
        self.assertEqual(len(ab["airbases"]), 1)
        self.assertEqual(ab["airbases"][0]["available_paradrop_planes"], 80)
        self.assertEqual(ab["airbases"][0]["max_transport_range_km"], 1000.0)
        wmeta = ab["by_wing_ptr"][wing]
        self.assertEqual(wmeta["air_base_state"], 64)
        self.assertEqual(wmeta["air_base_province"], 9496)
        self.assertTrue(wmeta["paradrop_ready"])

    def test_paradrop_zero_injection_guards(self):
        import armyops_win as A

        p = _MemProc()
        og = 0x50000000
        p.put(og, b"\x00" * 0x100)
        p.write(og, struct.pack("<Q", p.base + A.OG_VT))
        p.write(og + A.OG_HANDLE, struct.pack("<II", 53, 1))

        # 构造 1 个伞兵师 (tmpl_para+0x5B5=1) 与 1 个步兵师 (tmpl_inf+0x5B5=0)
        tmpl_para = 0x50010000
        tmpl_inf = 0x50011000
        p.put(tmpl_para, b"\x00" * 0x600)
        p.write(tmpl_para + A.DT_CAN_PARADROP, b"\x01")
        p.put(tmpl_inf, b"\x00" * 0x600)
        p.write(tmpl_inf + A.DT_CAN_PARADROP, b"\x00")

        div_para = 0x50020000
        div_inf = 0x50021000
        for d, hid, tmpl in ((div_para, 10, tmpl_para), (div_inf, 11, tmpl_inf)):
            p.put(d, b"\x00" * 0x200)
            p.write(d, struct.pack("<Q", p.base + A.CARMY_VT))
            p.write(d + A.CARMY_HANDLE, struct.pack("<II", 51, hid))
            p.write(d + A.CARMY_TEMPLATE, struct.pack("<Q", tmpl))

        def _boom(*_a, **_kw):
            raise AssertionError("预检拦截路径绝不允许触发注入 attach/call")

        g = SimpleNamespace(
            p=p, _inj=None, attach=_boom, call=_boom,
            _c=lambda t=None: SimpleNamespace(tag="GER", tag_id=1),
        )

        # 1) P1-3: 过期集团军指针 -> 0 注入拦截并返回 stale_pointer
        ok, err, info = A.paradrop(g, 0x263D1A1D800, 9496, 391)
        self.assertFalse(ok)
        self.assertEqual(info["blocked_by"], "stale_orders_group_pointer")
        self.assertTrue(info["stale_pointer"])
        self.assertIn("hoi4_inspect", err)

        # 2) P2-2: 全传普通步兵师 -> 0 注入拦截
        with mock.patch.object(A, "paratrooper_divisions", return_value={"paratrooper_count": 1, "paratroopers": [hex(div_para)]}):
            ok2, err2, info2 = A.paradrop(g, og, 9496, 391, divisions=[div_inf])
        self.assertFalse(ok2)
        self.assertEqual(info2["blocked_by"], "no_paratrooper_divisions")

        # 3) P1-1 / P2-3: 传空机场省 6570 (0 架运输机) -> 0 注入拦截并推荐 ready_sources (9496)
        fake_src_meta = {
            "ok": True,
            "airbase_scan_ok": True,
            "range_km": 1000.0,
            "total_transport_planes": 80,
            "ready_sources": [{"state_id": 64, "province": 9496, "available_paradrop_planes": 80, "max_transport_range_km": 1000.0}],
            "transport_wings": [{"id": 15, "planes": 80, "air_base_state": 64, "air_base_province": 9496}],
            "sources": [
                {"state_id": 64, "province": 9496, "all_provinces": [9496], "air_base_level": 10,
                 "transport_planes": 80, "available_paradrop_planes": 80, "max_transport_range_km": 1000.0, "can_launch_paradrop": True},
                {"state_id": 51, "province": 6570, "all_provinces": [6570], "air_base_level": 10,
                 "transport_planes": 0, "available_paradrop_planes": 0, "max_transport_range_km": None, "can_launch_paradrop": False},
            ],
        }
        import mapdata
        with mock.patch.object(A, "paradrop_sources", return_value=fake_src_meta), \
             mock.patch.object(mapdata, "load_definitions", return_value=({}, {391: "land", 9496: "land", 6570: "land"})), \
             mock.patch.object(mapdata, "province_distance_km", return_value=580.0):
            ok3, err3, info3 = A.paradrop(g, og, 6570, 391, divisions=[div_para, div_inf])
        self.assertFalse(ok3)
        self.assertEqual(info3["blocked_by"], "insufficient_transports_at_source")
        self.assertEqual(info3["skipped_non_paratroopers"], [hex(div_inf)])
        self.assertEqual(info3["ready_sources"][0]["province"], 9496)

        # 4) P3-5: 同集团军已有同目标 type==4 伞降单 -> 0 注入拦截
        ex_order = 0x25000030000
        tgt_buf = 0x25000031000
        p.put(tgt_buf, struct.pack("<i", 391))
        p.put(ex_order, b"\x00" * 0x100)
        p.write(ex_order + 0x70, struct.pack("<Qii", tgt_buf, 1, 1))
        p.write(ex_order + 0xB8, struct.pack("<i", 9496))
        with mock.patch.object(A, "paradrop_sources", return_value=fake_src_meta), \
             mock.patch.object(A, "orders", return_value=[{"id": 77, "type": 4, "ptr": hex(ex_order), "assigned_divisions": 1}]), \
             mock.patch.object(mapdata, "load_definitions", return_value=({}, {391: "land", 9496: "land"})), \
             mock.patch.object(mapdata, "province_distance_km", return_value=580.0):
            ok4, err4, info4 = A.paradrop(g, og, 9496, 391, divisions=[div_para])
        self.assertFalse(ok4)
        self.assertEqual(info4["blocked_by"], "duplicate_paradrop_target")
        self.assertEqual(info4["existing_orders"][0]["order_id"], 77)

    def test_plan_front_keeps_army_with_paradrop_and_protects_paratroopers(self):
        """P1-2: 持有 type==4 伞降单但仍有自由师的集团军不被跳过，且不抢走伞兵师。"""
        from calling.domains.battleplan import BattleplanMixin
        import armyops
        import armyops_win as A

        p = _MemProc()
        og_mix = 0x60000000
        og_pure_para = 0x60001000
        for og in (og_mix, og_pure_para):
            p.put(og, b"\x00" * 0x80)
            p.write(og + A.OG_IS_GROUP, b"\x00")

        para_divs = [0x61000000 + i * 0x100 for i in range(6)]
        inf_divs = [0x62000000 + i * 0x100 for i in range(10)]
        pure_para_divs = [0x63000000 + i * 0x100 for i in range(4)]

        class _DummyGame(BattleplanMixin):
            def __init__(self):
                self.p = p

            def _c(self, tag=None):
                return SimpleNamespace(tag="GER", tag_id=1, ptr=0x20000000)

            def country(self, tag):
                return SimpleNamespace(tag=str(tag).upper(), tag_id=2, ptr=0x20001000)

            def _supply_front_guard(self, enemy_tag, tag=None):
                return {"capacity": 50}

        g = _DummyGame()
        fake_front = {
            "front": 0x64000000,
            "front_handle": [66, 1],
            "section": 1,
            "section_provinces": [101, 102, 103],
            "border_provinces": [101, 102],
            "slice_from": 0,
            "slice_to": A.FULL,
        }

        def _fake_orders(_g, og):
            if og == og_mix:
                return [{"id": 10, "type": 4, "ptr": "0x65000000", "assigned_divisions": 6}]
            return [{"id": 11, "type": 4, "ptr": "0x65001000", "assigned_divisions": 4}]

        def _fake_assigned(_g, og, oid):
            if og == og_mix and oid == 10:
                return set(para_divs)
            if og == og_pure_para and oid == 11:
                return set(pure_para_divs)
            return set()

        def _fake_divs(_g, og):
            return (para_divs + inf_divs) if og == og_mix else list(pure_para_divs)

        with mock.patch.object(A, "fronts_towards", return_value=[fake_front]), \
             mock.patch.object(A, "target_provinces", return_value=[201, 202]), \
             mock.patch.object(armyops, "orders_groups", return_value=[og_mix, og_pure_para]), \
             mock.patch.object(A, "orders", side_effect=_fake_orders), \
             mock.patch.object(A, "order_assigned_divisions", side_effect=_fake_assigned), \
             mock.patch.object(A, "divisions_of", side_effect=_fake_divs):
            res = g.plan_front("HOL", dry_run=True)

        self.assertTrue(res["ok"])
        # og_mix 保留 10 个自由步兵师进入 plan，6 个伞兵师受保护；og_pure_para 全员在伞降单上被放进 busy 不打扰
        plan = res["steps"]["plan"]
        self.assertEqual(len(plan), 1)
        self.assertEqual(plan[0]["army"], hex(og_mix))
        self.assertEqual(plan[0]["divisions"], 10)
        self.assertEqual(res["steps"]["armies_with_existing_orders"], [hex(og_pure_para)])
        self.assertEqual(res["steps"]["armies_with_special_orders"][0]["reserved_special_divisions"], 6)


class TestP2ProductionAndP3Fixes(unittest.TestCase):
    """P2-4 船坞线提示 + create_production_line 走 post_drained + P3-2/P3-4 验证。"""

    def test_upgrade_production_explains_naval_dockyard_line(self):
        from calling.domains.economy import EconomyMixin
        import offsets as O

        p = _MemProc()
        co_ptr = 0x70000000
        prod_ptr = 0x70010000
        lines_arr = 0x70020000
        mil_line = 0x70030000
        nav_line = 0x70031000
        var_gun = 0x70040000
        var_sub = 0x70041000
        var_tp = 0x70042000

        p.put(co_ptr + O.CO_PRODUCTION_STATUS, struct.pack("<Q", prod_ptr))
        p.put(prod_ptr + O.PS_MIL_LINES, struct.pack("<Qii", lines_arr, 2, 2))
        p.put(lines_arr, struct.pack("<QQ", mil_line, nav_line))
        p.put(mil_line + O.ML_VARIANT, struct.pack("<Q", var_gun))
        p.put(nav_line + O.ML_VARIANT, struct.pack("<Q", var_sub))
        p.put(var_gun + O.EV_TYPE_ID, struct.pack("<i", 1))
        p.put(var_sub + O.EV_TYPE_ID, struct.pack("<i", 2))
        p.put(var_tp + O.EV_TYPE_ID, struct.pack("<i", 3))

        class _FakeEco(EconomyMixin):
            def __init__(self):
                self.p = p
                self._eq_by_id = {1: "infantry_equipment_1", 2: "submarine_1", 3: "transport_plane_equipment_1"}

            def _c(self, tag=None):
                return SimpleNamespace(tag="GER", tag_id=1, ptr=co_ptr)

            def _load_equipment(self):
                pass

            def _variant_for(self, eq, tag=None):
                return var_tp, {"type": 70, "id": 99}

            def handle_of(self, ptr):
                if ptr == mil_line:
                    return {"type": 56, "id": 1}
                return {"type": 57, "id": 2}

        g = _FakeEco()
        r = g.set_production_line_equipment(1, "transport_plane_equipment_1")
        self.assertFalse(r["ok"])
        self.assertEqual(r["blocked_by"], "not_military_production_line")
        self.assertTrue(r["is_naval_line"])
        self.assertIn("create_production_line", r["error"])
        self.assertEqual(r["military_lines"], [{"line": 0, "equipment": "infantry_equipment_1"}])

    def test_p3_peacetime_mode_alias_and_turnengine_specs(self):
        import autopilot as AP
        import turnengine as TE

        # P3-2: fuel.peacetime_mode="save" 归一为 "conserve" 且不报错
        errs, warns = [], []
        refs = {
            "focus_keys": set(), "tech_defs": {}, "idea_lookup": {},
            "decision_keys": set(), "building_keys": set(),
            "states_by_id": {}, "templates": set(), "equipment_keys": set(),
            "active_variants": set(), "doctrines": set(), "subunits": set(),
        }
        norm = AP._compile_stage_profile(
            None,
            {"fuel": {"peacetime_mode": "save"}},
            stage_path="stages[0]",
            ctx={},
            refs=refs,
            errors=errs,
            warnings=warns,
        )
        self.assertEqual(errs, [])
        self.assertEqual(norm["fuel"]["peacetime_mode"], "conserve")
        self.assertTrue(any("已自动归一化" in w for w in warns))

        # P3-4 / P0-3 / P3-5: TurnEngine Plan DSL 放行新增可选参数
        ok, verrs = TE.validate_plan({
            "actions": [
                {"op": "housekeep", "args": {"budget_s": 60.0}},
                {"op": "peace_demands", "args": {"loser": "INS", "states": [672, 335], "replace": False}},
                {"op": "paradrop", "args": {"orders_group": "0x50000000", "target": 391, "replace_existing": True}},
            ]
        })
        self.assertTrue(ok, verrs)

        # P2-4: 简报生产线标注船坞线 [坞]
        md = TE._render_brief_markdown({
            "meta": {"date": "1936.01.01.01", "tag": "GER", "paused": True},
            "country": {},
            "factories": {},
            "army": {},
            "war": {"at_war": False, "enemies": [], "combats": []},
            "focus": {},
            "research": [],
            "construction": [],
            "production": [
                {"index": 0, "equipment": "infantry_equipment_1", "factories": 8, "class": "CMilitaryProductionLine"},
                {"index": 12, "equipment": "submarine_1", "factories": 2, "class": "CNavalProductionLine"},
            ],
            "production_total": 2,
            "decisions_mine": [],
            "events": [],
        })
        self.assertIn("#12[坞]:submarine_1(2厂)", md)


if __name__ == "__main__":
    unittest.main()
