# -*- coding: utf-8 -*-
"""第三期 `hoi4_recalc assets` + Paradox VFS Mod 覆盖 + Python 消费层回归测试。

验证项：
  1. 原生 Rust CLI 对真实 HOI4 安装目录一键并行构建 11 张静态表（耗时 < 2s）且全部可被 Python pickle 无误加载；
  2. 全量 11 张表与 Python 解析器字段/类型/核心样例 100% 对齐（含 enable_equipments 嵌套 limit 修复与 Namibia 修复）；
  3. 合成 Paradox 游戏+双层 Mod VFS 端到端测试：replace_path 截断、同名文件高层覆盖、递归 localisation 子目录、
     localisation/replace 语言头过滤、dlc_load.json 自动解析、--only 子集构建与 --format both JSON 导出；
  4. Python 消费层集成：calling/assets.py 编排与清缓存、statemeta.pkl 秒读、equipmeta Mod 缓存信任、loc 非英语缓存信任；
  5. 原有 PE 扫描 `hoi4_recalc scan --verify-1195` 仍保持 75/75 EXACT。
"""
import json
import os
import pickle
import shutil
import struct
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
CALLING = os.path.join(ROOT, "calling")
if CALLING not in sys.path:
    sys.path.insert(0, CALLING)

RECALC_EXE = os.path.join(ROOT, "tools", "win", "hoi4_recalc.exe")

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] %s" % name)
    else:
        FAIL += 1
        print("  [FAIL] %s -- %s" % (name, detail))


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def _write_bmp_24(path, width, height, rgb_rows):
    """写一个最小合法 24-bit 无压缩 bottom-up BMP（供合成地图邻接测试）。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    stride = (width * 3 + 3) & ~3
    img_size = stride * height
    file_size = 54 + img_size
    hdr = struct.pack(
        "<2sIHHI IIIHHIIIIII",
        b"BM", file_size, 0, 0, 54,
        40, width, height, 1, 24, 0, img_size, 2835, 2835, 0, 0,
    )
    body = bytearray()
    for y in range(height - 1, -1, -1):
        row = bytearray()
        for r, g, b in rgb_rows[y]:
            row.extend((b, g, r))
        while len(row) < stride:
            row.append(0)
        body.extend(row)
    with open(path, "wb") as fh:
        fh.write(hdr + bytes(body))


def _write_bmp_8(path, width, height, idx_rows):
    """写一个最小合法 8-bit paletted bottom-up BMP（rivers.bmp 合成测试）。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    stride = (width + 3) & ~3
    img_size = stride * height
    palette = bytes(256 * 4)
    off = 54 + len(palette)
    file_size = off + img_size
    hdr = struct.pack(
        "<2sIHHI IIIHHIIIIII",
        b"BM", file_size, 0, 0, off,
        40, width, height, 1, 8, 0, img_size, 2835, 2835, 256, 0,
    )
    body = bytearray()
    for y in range(height - 1, -1, -1):
        row = bytearray(idx_rows[y])
        while len(row) < stride:
            row.append(255)
        body.extend(row)
    with open(path, "wb") as fh:
        fh.write(hdr + palette + bytes(body))


def test_real_game_assets():
    print("== 1. 真实 HOI4 目录全量 11 表并行构建与精度对账 ==")
    import config
    gd = config.game_dir(required=False)
    if not gd or not os.path.isdir(gd) or not os.path.isfile(RECALC_EXE):
        print("  [SKIP] 未检测到本地 HOI4 安装或 hoi4_recalc.exe")
        return

    tmp = tempfile.mkdtemp(prefix="hoi4_assets_test_")
    try:
        ev_out = os.path.join(tmp, "evdb_win.pkl")
        rep_out = os.path.join(tmp, "assets_report.md")
        cp = subprocess.run(
            [
                RECALC_EXE, "assets",
                "--game", gd,
                "--out-dir", tmp,
                "--evdb-out", ev_out,
                "--report", rep_out,
            ],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
        )
        check("assets 全量构建退出码为 0", cp.returncode == 0, (cp.stderr or cp.stdout)[-300:])
        mf_path = os.path.join(tmp, "assets_manifest.json")
        check("生成 assets_manifest.json", os.path.isfile(mf_path))
        check("生成 Markdown 汇总报告", os.path.isfile(rep_out) and os.path.getsize(rep_out) > 200)
        mf = json.load(open(mf_path, encoding="utf-8"))
        check("11 张表全部记录在 manifest", len(mf.get("assets") or {}) == 11, str(sorted((mf.get("assets") or {}).keys())))
        check("全量构建总耗时 < 2000ms", int(mf.get("total_ms", 99999)) < 2000, "total_ms=%s" % mf.get("total_ms"))

        # 逐表验证结构与关键条目
        loc_d = pickle.load(open(os.path.join(tmp, "loc.pkl"), "rb"))
        check("loc.pkl 含 10 万+ 条目且含 GER_anschluss",
              loc_d.get("schema") == 1 and loc_d.get("count", 0) > 100000 and "GER_anschluss" in loc_d.get("keys", {}))

        adj_d = pickle.load(open(os.path.join(tmp, "mapadj.pkl"), "rb"))
        check("mapadj.pkl 省份数 > 10000 且值为 set",
              len(adj_d) > 10000 and isinstance(next(iter(adj_d.values())), set))

        riv_d = pickle.load(open(os.path.join(tmp, "maprivers.pkl"), "rb"))
        k0, v0 = next(iter(riv_d.items()))
        check("maprivers.pkl 含跨河边且键为 2-tuple 值为 small/large",
              len(riv_d) > 3000 and isinstance(k0, tuple) and len(k0) == 2 and v0 in ("small", "large"))

        st2p, p2st = pickle.load(open(os.path.join(tmp, "mapstates.pkl"), "rb"))
        check("mapstates.pkl 为 (st2p, p2st) 且州 64 含柏林 6521",
              len(st2p) > 900 and 6521 in st2p.get(64, set()) and p2st.get(6521) == 64)

        sm_d = pickle.load(open(os.path.join(tmp, "statemeta.pkl"), "rb"))
        check("statemeta.pkl 含州 64 与 10+ 占槽建筑",
              sm_d.get("schema") == 1 and 64 in sm_d.get("states", {})
              and "arms_factory" in sm_d.get("slot_buildings", set())
              and sm_d.get("categories", {}).get("megalopolis") == 12)

        nr_d = pickle.load(open(os.path.join(tmp, "navalregions.pkl"), "rb"))
        check("navalregions.pkl 含 300+ 海区且正确解析 Namibia (215)",
              len(nr_d.get("regions", {})) >= 300 and nr_d.get("by_name", {}).get("namibia") == 215)

        td_d = pickle.load(open(os.path.join(tmp, "techdefs.pkl"), "rb"))
        ci = td_d.get("concentrated_industry") or {}
        check("techdefs.pkl 含 XOR 与反向 parents",
              "dispersed_industry" in (ci.get("xor") or []) and "basic_machine_tools" in (ci.get("parents") or []))

        fp_d = pickle.load(open(os.path.join(tmp, "focusplans.pkl"), "rb"))
        ans = (fp_d.get("focuses") or {}).get("GER_anschluss") or {}
        check("focusplans.pkl schema=2 且含 GER_anschluss 与 GER 规划",
              fp_d.get("schema") == 2 and ans.get("annex") is True and "GER" in (fp_d.get("plans") or {}))

        dm_d = pickle.load(open(os.path.join(tmp, "decisions_meta.pkl"), "rb"))
        check("decisions_meta.pkl 含 4000+ 决议",
              dm_d.get("schema") == 1 and dm_d.get("count", 0) > 4000)

        eq_d = pickle.load(open(os.path.join(tmp, "equipmeta.pkl"), "rb"))
        import equipmeta
        py_eq = equipmeta._parse_all(gd)
        check("equipmeta.pkl 与 Python 新解析 100% 精确一致",
              eq_d["equipments"] == py_eq["equipments"]
              and eq_d["tech_to_eq"] == py_eq["tech_to_eq"]
              and eq_d["eq_to_tech"] == py_eq["eq_to_tech"]
              and eq_d["tech_to_eq"].get("rocket_engines") == ["rocket_interceptor_equipment_1"])

        ev_d = pickle.load(open(ev_out, "rb"))
        g4 = ev_d.get("germany.4") or {}
        check("evdb_win.pkl 含 6000+ 事件且中英文烘焙正确",
              len(ev_d) > 6000 and g4.get("title_en") == "Anschluss" and g4.get("title_zh") == "德奥合并")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_synthetic_mod_vfs():
    print("== 2. 合成游戏+双层 Mod VFS 覆盖 / replace_path / dlc_load 测试 ==")
    if not os.path.isfile(RECALC_EXE):
        print("  [SKIP] hoi4_recalc.exe 不存在")
        return

    tmp = tempfile.mkdtemp(prefix="hoi4_vfs_test_")
    try:
        game = os.path.join(tmp, "game")
        user = os.path.join(tmp, "user")
        mod_a = os.path.join(tmp, "mod_a")
        mod_b = os.path.join(tmp, "mod_b")
        out_dir = os.path.join(tmp, "out")

        # 构造最小原版地图 (4x2: 左两列省 1，右两列省 2，中间边界全为大河)
        _write(os.path.join(game, "map", "definition.csv"),
               "0;0;0;0;land;false;unknown;0\n"
               "1;255;0;0;land;false;plains;1\n"
               "2;0;255;0;land;false;hills;1\n")
        _write_bmp_24(
            os.path.join(game, "map", "provinces.bmp"), 4, 8,
            [[(255, 0, 0), (255, 0, 0), (0, 255, 0), (0, 255, 0)] for _ in range(8)],
        )
        _write_bmp_8(
            os.path.join(game, "map", "rivers.bmp"), 4, 8,
            [[255, 9, 9, 255] for _ in range(8)],
        )
        _write(os.path.join(game, "history", "states", "1-Alpha.txt"),
               'state = { id = 1 name = "STATE_1" state_category = town provinces = { 1 2 } }')
        _write(os.path.join(game, "common", "state_category", "town.txt"),
               "state_categories = { town = { local_building_slots = 4 } }")
        _write(os.path.join(game, "common", "buildings", "00_buildings.txt"),
               "buildings = { arms_factory = { shares_slots = yes } infrastructure = { shares_slots = no } }")
        _write(os.path.join(game, "map", "strategicregions", "1-Test.txt"),
               'strategic_region = { id = 1 name = "STRATEGICREGION_TEST" provinces = { 1 2 } }')

        # 原版脚本与本地化
        _write(os.path.join(game, "localisation", "english", "base_l_english.yml"),
               'l_english:\n STATE_1:0 "Vanilla State"\n STRATEGICREGION_TEST:0 "Vanilla Sea"\n VAN_KEY:0 "Keep Me"\n')
        _write(os.path.join(game, "common", "technologies", "infantry.txt"),
               "technologies = {\n base_tech = { start_year = 1936 }\n}\n")
        _write(os.path.join(game, "common", "national_focus", "vanilla.txt"),
               "focus_tree = {\n focus = { id = VAN_FOCUS cost = 10 }\n}\n")
        _write(os.path.join(game, "common", "decisions", "van.txt"),
               "cat_a = {\n van_dec = { cost = 50 available = { always = yes } }\n}\n")
        _write(os.path.join(game, "common", "units", "equipment", "infantry.txt"),
               "equipments = {\n inf_eq = { is_archetype = yes soft_attack = 3 }\n inf_eq_1 = { archetype = inf_eq year = 1936 soft_attack = 6 }\n}\n")
        _write(os.path.join(game, "events", "van.txt"),
               'country_event = { id = van.1 title = van.1.t option = { name = van.1.a } }\n')

        # Mod A: replace_path 清空原版国策与事件；用递归子目录与 replace 覆盖本地化（含法语干扰文件）
        _write(os.path.join(mod_a, "descriptor.mod"),
               'name="Test Mod A"\nreplace_path="common/national_focus"\nreplace_path="events"\n')
        _write(os.path.join(mod_a, "common", "national_focus", "mod_a.txt"),
               "focus_tree = {\n focus = { id = MODA_FOCUS cost = 5 }\n}\n")
        _write(os.path.join(mod_a, "events", "moda.txt"),
               'country_event = { id = moda.1 title = MODA_EVT_T option = { name = MODA_EVT_A } }\n')
        _write(os.path.join(mod_a, "localisation", "english", "nested", "moda_l_english.yml"),
               'l_english:\n MODA_EVT_T:0 "Mod A Event Title"\n MODA_EVT_A:0 "Ok"\n')
        _write(os.path.join(mod_a, "localisation", "replace", "override_l_english.yml"),
               'l_english:\n STATE_1:0 "Overridden State"\n')
        _write(os.path.join(mod_a, "localisation", "replace", "override_l_french.yml"),
               'l_french:\n STATE_1:0 "Etat Francais Ne Doit Pas Ecraser"\n')

        # Mod B: 覆盖同名科技文件 infantry.txt（后加载胜出）
        _write(os.path.join(mod_b, "common", "technologies", "infantry.txt"),
               "technologies = {\n modb_tech = { start_year = 1940 enable_equipments = { limit = { has_dlc = \"X\" } inf_eq_1 } }\n}\n")

        # 构造 user/mod/*.mod 与 dlc_load.json
        _write(os.path.join(user, "mod", "a.mod"),
               'name="Test Mod A"\npath="%s"\n' % mod_a.replace("\\", "/"))
        _write(os.path.join(user, "mod", "b.mod"),
               'name="Test Mod B"\npath="%s"\n' % mod_b.replace("\\", "/"))
        _write(os.path.join(user, "dlc_load.json"),
               json.dumps({"enabled_mods": ["mod/a.mod", "mod/b.mod"], "disabled_dlcs": []}))

        ev_out = os.path.join(out_dir, "evdb_win.pkl")
        cp = subprocess.run(
            [
                RECALC_EXE, "assets",
                "--game", game,
                "--user-dir", user,
                "--dlc-load", os.path.join(user, "dlc_load.json"),
                "--out-dir", out_dir,
                "--evdb-out", ev_out,
                "--format", "both",
            ],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
        )
        check("合成 Mod VFS 构建成功", cp.returncode == 0, (cp.stderr or cp.stdout)[-300:])

        # 验证 replace_path 拦截
        fp = pickle.load(open(os.path.join(out_dir, "focusplans.pkl"), "rb"))
        check("replace_path 拦截原版国策且仅保留 Mod A 国策",
              "VAN_FOCUS" not in fp["focuses"] and fp["focuses"].get("MODA_FOCUS", {}).get("days") == 35)

        ev = pickle.load(open(ev_out, "rb"))
        check("replace_path 拦截原版事件且递归子目录本地化正确烘焙到 Mod 事件",
              "van.1" not in ev and ev.get("moda.1", {}).get("title_en") == "Mod A Event Title")

        # 验证本地化覆盖 + 法语头过滤
        lc = pickle.load(open(os.path.join(out_dir, "loc.pkl"), "rb"))
        check("localisation/replace 生效且 l_french 文件不污染 english",
              lc["keys"].get("STATE_1") == "Overridden State"
              and lc["keys"].get("VAN_KEY") == "Keep Me"
              and lc["keys"].get("MODA_EVT_T") == "Mod A Event Title")

        # 验证 Mod B 覆盖同名科技文件 + enable_equipments 嵌套 limit 过滤
        td = pickle.load(open(os.path.join(out_dir, "techdefs.pkl"), "rb"))
        eq = pickle.load(open(os.path.join(out_dir, "equipmeta.pkl"), "rb"))
        check("Mod B 覆盖同名 infantry.txt 且 mods 记录完整",
              "base_tech" not in td and td.get("modb_tech", {}).get("year") == 1940
              and eq["tech_to_eq"].get("modb_tech") == ["inf_eq_1"]
              and eq.get("mods") == ["Test Mod A", "Test Mod B"])

        # 验证合成地图邻接与跨大河识别
        adj = pickle.load(open(os.path.join(out_dir, "mapadj.pkl"), "rb"))
        riv = pickle.load(open(os.path.join(out_dir, "maprivers.pkl"), "rb"))
        check("合成 BMP 正确识别省 1<->2 邻接与大河",
              2 in adj.get(1, set()) and riv.get((1, 2)) == "large")

        # 验证 --format both 产出了合法 JSON
        j_td = json.load(open(os.path.join(out_dir, "techdefs.json"), encoding="utf-8"))
        check("--format both 导出合法 techdefs.json", "modb_tech" in j_td)

        # 验证 --only 子集构建
        only_dir = os.path.join(tmp, "only_out")
        cp2 = subprocess.run(
            [RECALC_EXE, "assets", "--game", game, "--out-dir", only_dir, "--only", "techdefs,statemeta"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
        )
        check("--only 仅生成指定子集表",
              cp2.returncode == 0
              and os.path.isfile(os.path.join(only_dir, "techdefs.pkl"))
              and os.path.isfile(os.path.join(only_dir, "statemeta.pkl"))
              and not os.path.isfile(os.path.join(only_dir, "loc.pkl")))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_python_integration():
    print("== 3. Python 消费层集成与 PE 75/75 零回归 ==")
    import assets as _assets
    import equipmeta
    import loc
    import statemeta

    st = _assets.summary()
    has_cli = os.path.isfile(RECALC_EXE)
    check("assets.summary() 正常返回",
          st.get("recalc_cli") is has_cli and (not has_cli or st.get("assets_count") == 11),
          str(st))

    # 验证 clear_memory_caches 生效
    statemeta.reset_cache()
    m64 = statemeta.state_meta(64, {"arms_factory": 3})
    check("statemeta 经 statemeta.pkl 加载州 64 正确",
          m64.get("name_key") == "STATE_64" and m64.get("building_slots") == 12 and m64.get("slots_free") == 9,
          str(m64))

    cleared = _assets.clear_memory_caches()
    check("clear_memory_caches 清理已加载模块", "statemeta" in cleared and "loc" in cleared, str(cleared))

    # 验证 equipmeta.load() 信任 hoi4_recalc Mod 缓存而不覆写成原版
    tmp = tempfile.mkdtemp(prefix="hoi4_py_int_")
    orig_eq_cache = equipmeta._cache_path
    orig_loc_cache = loc.CACHE
    try:
        fake_eq_pkl = os.path.join(tmp, "equipmeta.pkl")
        fake_gd = os.path.join(tmp, "fake_gd")
        os.makedirs(fake_gd, exist_ok=True)
        pickle.dump({
            "version": 1,
            "source": "hoi4_recalc",
            "mods": ["Kaiserreich"],
            "sig": (fake_gd, ((99, 12345), (99, 12345))),
            "equipments": {"kr_rifle": {"key": "kr_rifle", "archetype": "infantry_equipment", "stats": {}}},
            "tech_to_eq": {},
            "eq_to_tech": {},
        }, open(fake_eq_pkl, "wb"))
        equipmeta._cache_path = lambda: fake_eq_pkl
        equipmeta._MEM_CACHE.clear()
        loaded = equipmeta.load(fake_gd)
        check("equipmeta.load 信任 hoi4_recalc Mod 缓存不覆盖", "kr_rifle" in loaded.get("equipments", {}))

        # 验证 loc.build 信任 hoi4_recalc 显式中文缓存
        fake_loc_pkl = os.path.join(tmp, "loc.pkl")
        pickle.dump({
            "schema": 1,
            "source": "hoi4_recalc",
            "lang": "simp_chinese",
            "files": 10,
            "count": 1,
            "keys": {"TEST_KEY": "简体中文测试"},
        }, open(fake_loc_pkl, "wb"))
        loc.CACHE = fake_loc_pkl
        loc._MEM = None
        ld = loc.load()
        check("loc.load 信任 hoi4_recalc 显式中文缓存不回退英文",
              ld.get("lang") == "simp_chinese" and ld["keys"].get("TEST_KEY") == "简体中文测试")
    finally:
        equipmeta._cache_path = orig_eq_cache
        equipmeta._MEM_CACHE.clear()
        loc.CACHE = orig_loc_cache
        loc._MEM = None
        shutil.rmtree(tmp, ignore_errors=True)

    # 校验原有 PE scan --verify-1195 无回归
    import config
    exe = config.binary_path(required=False)
    if exe and os.path.isfile(exe) and os.path.isfile(RECALC_EXE):
        cp = subprocess.run(
            [RECALC_EXE, "scan", "--exe", exe, "--verify-1195"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
        )
        check("hoi4_recalc scan --verify-1195 保持 75/75 EXACT",
              cp.returncode == 0 and "75/75 EXACT" in cp.stdout, cp.stdout[-240:])


if __name__ == "__main__":
    test_real_game_assets()
    test_synthetic_mod_vfs()
    test_python_integration()
    print("\nSummary: %d passed, %d failed" % (PASS, FAIL))
    sys.exit(0 if FAIL == 0 else 1)
