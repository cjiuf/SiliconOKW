# -*- coding: utf-8 -*-
"""native/hoi4_recalc Rust 独立离线版本重算 CLI + 第二期 calling/profile.py 数据驱动解耦回归测试。

运行：python research/windows/test_recalc_20261122.py
"""
import json
import os
import struct
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "calling"))

import config
import offsets_win as OW
import profile as prof_mod
import pecmd
import cmdwin
import peacewin
import rtti_win

RECALC_EXE = os.path.join(ROOT, "tools", "win", "hoi4_recalc.exe")
SNAPSHOT_JSON = os.path.join(ROOT, "research", "windows", "profile_1.19.5.0.json")

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] %s" % name)
    else:
        FAIL += 1
        print("  [FAIL] %s%s" % (name, (" -- " + str(detail)) if detail else ""))


def verify_profile_contents(prof):
    check("schema_version == 1 & build_id 1.19.5.0",
          prof["schema_version"] == 1
          and "1.19.5.0" in (prof["pe"].get("build_id") or "")
          and prof["pe"]["size_of_image"] == OW.PE_SIZE_OF_IMAGE,
          prof["pe"].get("build_id"))
    check("commands_count >= 400 (exceeds legacy pecmd.py 380)",
          prof["commands_count"] >= 400, prof["commands_count"])
    check("all 56 anchors resolved exact",
          len(prof["anchors"]) >= 56
          and all(a["status"] == "exact" for a in prof["anchors"].values()),
          [k for k, a in prof["anchors"].items() if a["status"] != "exact"])

    # 校验与 offsets_win 关键常量完全吻合
    anc = prof["anchors"]
    check("anchors match offsets_win core globals & offsets",
          anc["G_GAMESTATE"]["value"] == OW.G_GAMESTATE
          and anc["G_IDLER"]["value"] == OW.G_IDLER
          and anc["G_DEFAULT_ALLOC"]["value"] == OW.G_DEFAULT_ALLOC
          and anc["GS_GAME_SPEED"]["value"] == OW.GS_GAME_SPEED
          and anc["G_GAME_SPEED_SECONDS"]["value"] == OW.G_GAME_SPEED_SECONDS
          and anc["GS_COUNTRIES"]["value"] == OW.GS_COUNTRIES
          and anc["CO_TECHNOLOGY_STATUS"]["value"] == OW.CO_TECHNOLOGY_STATUS
          and anc["CO_PRODUCTION_STATUS"]["value"] == OW.CO_PRODUCTION_STATUS
          and anc["CO_DEPLOYMENT_STATUS"]["value"] == OW.CO_DEPLOYMENT_STATUS
          and anc["CO_POLITICAL_STATUS"]["value"] == OW.CO_POLITICAL_STATUS
          and anc["CO_FOCUS_TREE"]["value"] == OW.CO_FOCUS_TREE
          and anc["IDLER_PAUSE_WRITE_SITE"]["value"] == OW.IDLER_PAUSE_WRITE_SITE
          and anc["FN_RESOLVE_HANDLE"]["value"] == 0x2284870)

    # 校验历史 pecmd.py 算错/漏算的 6 个重点命令已被 iced-x86 全自动纠偏
    cmds = prof["commands"]
    c_adv = cmds["CAddAdvisorCommand"]
    check("CAddAdvisorCommand auto-recovered (ctor=0x11CC6D0, size=0x50, args=2, +0x28/+0x30)",
          c_adv["ctor"] == 0x11CC6D0 and c_adv["size"] == 0x50 and c_adv["args"] == 2
          and [f["offset"] for f in c_adv["fields"]] == [0x28, 0x30],
          c_adv)

    c_apl = cmds["CAddProductionLineCommand"]
    check("CAddProductionLineCommand auto-recovered all 4 args (+0x28/+0x2C/+0x34/+0x3C)",
          c_apl["ctor"] == 0x11CD270 and c_apl["size"] == 0x40 and c_apl["args"] == 4
          and [f["offset"] for f in c_apl["fields"]] == [0x28, 0x2C, 0x34, 0x3C],
          c_apl)

    c_rpl = cmds["CRemoveProductionLineCommand"]
    check("CRemoveProductionLineCommand auto-recovered (+0x28 handle, size=0x30)",
          c_rpl["ctor"] == 0x11D0210 and c_rpl["size"] == 0x30 and c_rpl["args"] == 1
          and [f["offset"] for f in c_rpl["fields"]] == [0x28],
          c_rpl)

    c_drop = cmds["CDropCurrentNationalFocusCommand"]
    check("CDropCurrentNationalFocusCommand leaf ctor exact (0x11CE6F0, not 0x11CE6C0)",
          c_drop["ctor"] == 0x11CE6F0 and c_drop["size"] == 0x30,
          c_drop)

    c_res = cmds["CSetResearchCommand"]
    check("CSetResearchCommand recovers all 6 args including 3 stack bools",
          c_res["ctor"] == 0x11D1980 and c_res["size"] == 0x40 and c_res["args"] == 6
          and [f["offset"] for f in c_res["fields"]] == [0x28, 0x30, 0x38, 0x3C, 0x3D, 0x3E],
          c_res)

    c_spd = cmds["CSetGameSpeedCommand"]
    check("CSetGameSpeedCommand exact (ctor=0xF84510, size=0x30, +0x28 int)",
          c_spd["ctor"] == 0xF84510 and c_spd["size"] == 0x30
          and c_spd["can_execute"] == 0xF85FB0
          and [f["offset"] for f in c_spd["fields"]] == [0x28],
          c_spd)


def _make_fake_pe_header(size_of_image, timestamp, image_base=0x140000000):
    buf = bytearray(0x200)
    buf[0:2] = b"MZ"
    struct.pack_into("<I", buf, 0x3C, 0x80)
    buf[0x80:0x84] = b"PE\0\0"
    coff = 0x84
    struct.pack_into("<I", buf, coff + 4, timestamp)
    opt = coff + 20
    struct.pack_into("<H", buf, opt, 0x20B)
    struct.pack_into("<Q", buf, opt + 0x18, image_base)
    struct.pack_into("<I", buf, opt + 0x38, size_of_image)
    return bytes(buf)


def test_profile_data_driven(snap):
    print("\n== 2. 第二期数据驱动层 calling/profile.py 集成与热切测试 ==")
    prof_mod.reset_cache()
    s = prof_mod.summary()
    check("profile.summary() loads 1.19.5.0 profile cleanly",
          s["loaded"] and s["anchors_exact"] >= 56 and s["commands_count"] >= 400
          and s["match"] in ("exact", "offline_snapshot"),
          s)

    # pecmd.build() 透明直读 profile.commands()（403 条，免 pecmds.pkl 与 Capstone）
    cat = pecmd.build()
    check("pecmd.build() serves 403 commands straight from profile.json",
          len(cat) >= 400 and cat["CAddAdvisorCommand"]["ctor"] == 0x11CC6D0
          and cat["CAddAdvisorCommand"]["size"] == 0x50
          and cat["CAddProductionLineCommand"]["args"] == 4,
          (len(cat), cat.get("CAddAdvisorCommand")))

    # verify_live_process：匹配 vs 篡改 timestamp 模拟游戏更新拦截
    pe_info = snap["pe"]
    good_hdr = _make_fake_pe_header(pe_info["size_of_image"], pe_info["timestamp"])
    bad_hdr = _make_fake_pe_header(pe_info["size_of_image"], pe_info["timestamp"] + 1)

    class _FakeProc:
        def __init__(self, hdr):
            self.base = 0x7FF600000000
            self._hdr = hdr
        def read(self, addr, n):
            return self._hdr[:n]

    v_ok = prof_mod.verify_live_process(_FakeProc(good_hdr))
    v_bad = prof_mod.verify_live_process(_FakeProc(bad_hdr))
    check("verify_live_process exact match -> ok=True", v_ok["ok"] and v_ok["status"] == "exact", v_ok)
    check("verify_live_process drifted timestamp -> ok=False, status=mismatch",
          (not v_bad["ok"]) and v_bad["status"] == "mismatch", v_bad)

    # 热切模拟新版 Profile (HOI4_PROFILE) 验证 cmdwin / peacewin 常量自动切换并能干净还原
    with tempfile.TemporaryDirectory() as td:
        fake_prof = json.loads(json.dumps(snap))
        fake_prof["pe"]["build_id"] = "Simulated v1.19.6.0"
        fake_prof["pe"]["timestamp"] = pe_info["timestamp"] + 0x1234
        fake_prof["anchors"]["FN_POST"]["value"] = 0x22D0000
        fake_prof["anchors"]["PEACE_GET_CONF"]["value"] = 0x1EE0000
        fake_prof["commands"]["CSetGameSpeedCommand"]["vt"] = 0x2A00000
        fake_prof["commands"]["CAddAdvisorCommand"]["can_execute"] = 0x11F0000
        fp = os.path.join(td, "profile_fake_1196.json")
        with open(fp, "w", encoding="utf-8") as fh:
            json.dump(fake_prof, fh)

        old_env = os.environ.get("HOI4_PROFILE")
        try:
            os.environ["HOI4_PROFILE"] = fp
            prof_mod.reset_cache()
            cmdwin._apply_profile()
            peacewin._apply_profile()
            check("HOI4_PROFILE hot-switches cmdwin & peacewin constants to simulated 1.19.6",
                  cmdwin.FN_POST == 0x22D0000
                  and cmdwin.VT_SET_GAME_SPEED == 0x2A00000
                  and cmdwin.CANEXEC_ADD_ADVISOR == 0x11F0000
                  and peacewin.GET_CONF == 0x1EE0000,
                  (hex(cmdwin.FN_POST), hex(cmdwin.VT_SET_GAME_SPEED), hex(peacewin.GET_CONF)))
        finally:
            if old_env is None:
                os.environ.pop("HOI4_PROFILE", None)
            else:
                os.environ["HOI4_PROFILE"] = old_env
            prof_mod.reset_cache()
            cmdwin._apply_profile()
            peacewin._apply_profile()

        check("restoring HOI4_PROFILE reverts cmdwin & peacewin back to 1.19.5.0",
              cmdwin.FN_POST == 0x22C4250
              and cmdwin.VT_SET_GAME_SPEED == 0x29F3A80
              and peacewin.GET_CONF == 0x1ED1EE0)


def main():
    print("\n== 1. 静态基线快照 research/windows/profile_1.19.5.0.json ==")
    check("profile_1.19.5.0.json exists", os.path.isfile(SNAPSHOT_JSON))
    snap = None
    if os.path.isfile(SNAPSHOT_JSON):
        with open(SNAPSHOT_JSON, "r", encoding="utf-8") as fh:
            snap = json.load(fh)
        verify_profile_contents(snap)
        test_profile_data_driven(snap)

    exe_path = config.binary_path(required=False)
    if not (os.path.isfile(RECALC_EXE) and exe_path and os.path.isfile(exe_path)):
        print("\n[SKIP] hoi4_recalc.exe 或 hoi4.exe 不可用，跳过实机 PE 重算测试")
        print("\nSummary: %d passed, %d failed" % (PASS, FAIL))
        return 1 if FAIL else 0

    print("\n== 3. 运行 hoi4_recalc.exe 实时扫描 hoi4.exe 并与 rtti_win.pkl 全量对账 ==")
    with tempfile.TemporaryDirectory() as td:
        out_prof = os.path.join(td, "prof.json")
        out_rtti = os.path.join(td, "rtti.json")
        out_rep = os.path.join(td, "report.md")
        t0 = time.monotonic()
        cp = subprocess.run(
            [RECALC_EXE, "scan", "--exe", exe_path,
             "--out", out_prof, "--rtti-out", out_rtti,
             "--report", out_rep, "--verify-1195"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        dt = time.monotonic() - t0
        check("hoi4_recalc scan --verify-1195 exit=0 & 75/75 EXACT",
              cp.returncode == 0 and "75/75 EXACT" in cp.stdout,
              cp.stdout + "\n" + cp.stderr)
        check("total scan elapsed < 5.0s (actual %.2fs)" % dt, dt < 5.0, dt)
        check("markdown report written",
              os.path.isfile(out_rep) and "75 / 75 EXACT" in open(out_rep, encoding="utf-8").read())

        live_prof = json.load(open(out_prof, encoding="utf-8"))
        verify_profile_contents(live_prof)

        # 全量比对 RTTI 映射表（8,616 类 / 16,567 vtable 100% 集合等价）+ 测试 rtti_win 透明消费 JSON
        rtti_json = json.load(open(out_rtti, encoding="utf-8"))
        py_rtti = rtti_win.build()
        check("RTTI plain class map 100%% identical to rtti_win.pkl (%d classes)" % len(py_rtti["vtables"]),
              rtti_json["vtables"] == py_rtti["vtables"],
              "rust=%d py=%d" % (len(rtti_json["vtables"]), len(py_rtti["vtables"])))

        old_rtti_env = os.environ.get("HOI4_RTTI_JSON")
        try:
            os.environ["HOI4_RTTI_JSON"] = out_rtti
            prof_mod.reset_cache()
            rtti_win._MAP = None
            from_json = rtti_win.build()
            check("rtti_win.build() transparently loads Rust rtti.json",
                  from_json["source"] == out_rtti
                  and from_json["vtables"] == py_rtti["vtables"],
                  from_json.get("source"))
        finally:
            if old_rtti_env is None:
                os.environ.pop("HOI4_RTTI_JSON", None)
            else:
                os.environ["HOI4_RTTI_JSON"] = old_rtti_env
            prof_mod.reset_cache()
            rtti_win._MAP = None

        # 测试 diff 子命令（同版 SAME + 模拟版本漂移 DRIFTED / ABI_CHANGED）
        cp_same = subprocess.run(
            [RECALC_EXE, "diff", "--old", SNAPSHOT_JSON, "--new", out_prof],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        check("diff identical profiles reports SAME",
              cp_same.returncode == 0 and "SAME" in cp_same.stdout
              and "DRIFTED" not in cp_same.stdout and "ABI_CHANGED" not in cp_same.stdout,
              cp_same.stdout[:300])

        mutated = json.loads(json.dumps(live_prof))
        mutated["anchors"]["GS_GAME_SPEED"]["value"] = 0x4C0
        mutated["commands"]["CSetGameSpeedCommand"]["size"] = 0x38
        mut_path = os.path.join(td, "mutated.json")
        with open(mut_path, "w", encoding="utf-8") as fh:
            json.dump(mutated, fh)
        cp_diff = subprocess.run(
            [RECALC_EXE, "diff", "--old", SNAPSHOT_JSON, "--new", mut_path],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        check("diff detects simulated DRIFTED anchor & ABI_CHANGED command",
              cp_diff.returncode == 0
              and "GS_GAME_SPEED" in cp_diff.stdout and "DRIFTED" in cp_diff.stdout
              and "CSetGameSpeedCommand" in cp_diff.stdout and "ABI_CHANGED" in cp_diff.stdout,
              cp_diff.stdout[:500])

    print("\nSummary: %d passed, %d failed" % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
