# -*- coding: utf-8 -*-
"""Windows PE 版本化 Profile 统一加载器与指纹校验层（第二期数据驱动解耦）。

职责：
  1. 极速读取当前安装（或运行中）`hoi4.exe` 的 PE 头指纹 (`SizeOfImage`, `TimeDateStamp`, `ImageBase`)；
  2. 匹配并加载 `native/hoi4_recalc` 产出的版本化 `profile_*.json`（含 56+ 核心锚点与 400+ 命令布局）；
  3. 当检测到 `hoi4.exe` 更新且本地存在 `tools/win/hoi4_recalc.exe` 时，可在 ~0.25s 内自动重算生成新版 Profile；
  4. 为 `offsets_win` / `cmdwin` / `peacewin` / `airops_win` / `navalops_win` / `events_win` /
     `templedit` / `rtti_win` / `pecmd` 提供零循环依赖、支持默认值回落的统一查表 API。
"""
from __future__ import annotations

import glob
import json
import os
import struct
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PROFILES_DIR = os.path.join(HERE, "profiles")
RECALC_EXE = os.path.join(ROOT, "tools", "win", "hoi4_recalc.exe")

_LOADED = False
_ACTIVE_PROFILE = None
_ACTIVE_META = {
    "source": None,
    "match": "unloaded",      # exact | auto_recalculated | env_override | offline_snapshot | mismatch | none
    "exe_fingerprint": None,
    "profile_pe": None,
    "error": None,
}
_RTTI_CACHE = None


def parse_pe_header(blob, path=None, file_size=None):
    """从 PE 前 4 KB 字节中解析轻量指纹（耗时 < 0.2 ms）。"""
    if not blob or len(blob) < 0x100 or blob[:2] != b"MZ":
        return None
    e_lfanew = struct.unpack_from("<I", blob, 0x3C)[0]
    if e_lfanew + 0x60 > len(blob) or blob[e_lfanew:e_lfanew + 4] != b"PE\0\0":
        return None
    coff = e_lfanew + 4
    timestamp = struct.unpack_from("<I", blob, coff + 4)[0]
    opt = coff + 20
    magic = struct.unpack_from("<H", blob, opt)[0]
    if magic != 0x20B:
        return None
    image_base = struct.unpack_from("<Q", blob, opt + 0x18)[0]
    size_of_image = struct.unpack_from("<I", blob, opt + 0x38)[0]
    return {
        "path": path,
        "file_size": file_size,
        "image_base": int(image_base),
        "size_of_image": int(size_of_image),
        "timestamp": int(timestamp),
    }


def pe_fingerprint(exe_path):
    """读取磁盘上 `hoi4.exe` 的 PE 轻量指纹。"""
    if not exe_path or not os.path.isfile(exe_path):
        return None
    try:
        sz = os.path.getsize(exe_path)
        with open(exe_path, "rb") as fh:
            head = fh.read(4096)
        return parse_pe_header(head, path=exe_path, file_size=sz)
    except OSError:
        return None


def _installed_exe():
    try:
        import config
        return config.binary_path(required=False)
    except Exception:
        return None


def _candidate_profile_paths():
    seen = set()
    out = []
    for pat in (
        os.path.join(PROFILES_DIR, "profile_*.json"),
        os.path.join(ROOT, "research", "windows", "profile_*.json"),
        os.path.join(HERE, "profile_*.json"),
    ):
        for p in sorted(glob.glob(pat)):
            ap = os.path.abspath(p)
            if ap not in seen and os.path.isfile(ap):
                seen.add(ap)
                out.append(ap)
    return out


def _read_json(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _matches_fp(prof, fp):
    if not prof or not fp:
        return False
    pe = prof.get("pe") or {}
    return (
        int(pe.get("size_of_image") or 0) == int(fp["size_of_image"])
        and int(pe.get("timestamp") or 0) == int(fp["timestamp"])
    )


def recalc_available():
    return sys.platform == "win32" and os.path.isfile(RECALC_EXE)


def run_recalc(exe_path, out_profile=None, out_rtti=None, report_path=None, timeout=30):
    """调用 `tools/win/hoi4_recalc.exe scan` 为指定 `hoi4.exe` 生成 Profile / RTTI JSON。"""
    if not os.path.isfile(RECALC_EXE):
        raise FileNotFoundError("hoi4_recalc.exe not found at %s" % RECALC_EXE)
    if not exe_path or not os.path.isfile(exe_path):
        raise FileNotFoundError("hoi4.exe not found: %s" % exe_path)
    fp = pe_fingerprint(exe_path)
    tag = (
        "%08x_%08x" % (fp["timestamp"], fp["size_of_image"])
        if fp else "custom"
    )
    os.makedirs(PROFILES_DIR, exist_ok=True)
    if out_profile is None:
        out_profile = os.path.join(PROFILES_DIR, "profile_%s.json" % tag)
    cmd = [RECALC_EXE, "scan", "--exe", exe_path, "--out", out_profile]
    if out_rtti:
        cmd += ["--rtti-out", out_rtti]
    if report_path:
        cmd += ["--report", report_path]
    cp = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    if cp.returncode != 0:
        raise RuntimeError(
            "hoi4_recalc failed (rc=%d): %s"
            % (cp.returncode, (cp.stderr or cp.stdout)[-500:])
        )
    return {
        "profile_path": out_profile,
        "rtti_path": out_rtti,
        "report_path": report_path,
        "stdout": cp.stdout,
    }


def reset_cache():
    """清空内存缓存（主要用于单测或切换 HOI4_PROFILE 后重新加载）。"""
    global _LOADED, _ACTIVE_PROFILE, _RTTI_CACHE
    _LOADED = False
    _ACTIVE_PROFILE = None
    _RTTI_CACHE = None
    _ACTIVE_META.update(
        source=None,
        match="unloaded",
        exe_fingerprint=None,
        profile_pe=None,
        error=None,
    )


def load(path=None, force=False, allow_auto_recalc=None):
    """加载并返回当前生效的 PE Profile dict（若无可用 profile 则返回 None）。"""
    global _LOADED, _ACTIVE_PROFILE
    if _LOADED and not force and path is None:
        return _ACTIVE_PROFILE

    if allow_auto_recalc is None:
        allow_auto_recalc = os.environ.get("HOI4_AUTO_RECALC", "1") != "0"

    # 1. 显式传入 path 或环境变量 HOI4_PROFILE
    explicit = path or os.environ.get("HOI4_PROFILE")
    if explicit:
        try:
            prof = _read_json(explicit)
            exe = _installed_exe()
            fp = pe_fingerprint(exe) if exe else None
            _ACTIVE_PROFILE = prof
            _ACTIVE_META.update(
                source=os.path.abspath(explicit),
                match="exact" if (fp and _matches_fp(prof, fp)) else "env_override",
                exe_fingerprint=fp,
                profile_pe=prof.get("pe"),
                error=None,
            )
            _LOADED = True
            return _ACTIVE_PROFILE
        except Exception as e:
            _ACTIVE_PROFILE = None
            _ACTIVE_META.update(
                source=explicit,
                match="none",
                error="failed to load explicit profile %s: %s" % (explicit, e),
            )
            _LOADED = True
            return None

    exe = _installed_exe()
    fp = pe_fingerprint(exe) if exe else None
    cands = _candidate_profile_paths()

    first_valid = None
    first_valid_path = None
    for cp in cands:
        try:
            prof = _read_json(cp)
        except Exception:
            continue
        if not isinstance(prof, dict) or "anchors" not in prof or "commands" not in prof:
            continue
        if first_valid is None:
            first_valid, first_valid_path = prof, cp
        if fp and _matches_fp(prof, fp):
            _ACTIVE_PROFILE = prof
            _ACTIVE_META.update(
                source=cp,
                match="exact",
                exe_fingerprint=fp,
                profile_pe=prof.get("pe"),
                error=None,
            )
            _LOADED = True
            return _ACTIVE_PROFILE

    # 2. 若本机 hoi4.exe 存在但没有任何现成 profile 匹配 -> 按需自动调用 hoi4_recalc.exe
    if fp and exe and allow_auto_recalc and recalc_available():
        try:
            tag = "%08x_%08x" % (fp["timestamp"], fp["size_of_image"])
            out_p = os.path.join(PROFILES_DIR, "profile_%s.json" % tag)
            out_r = os.path.join(PROFILES_DIR, "rtti_%s.json" % tag)
            run_recalc(exe, out_profile=out_p, out_rtti=out_r)
            prof = _read_json(out_p)
            _ACTIVE_PROFILE = prof
            _ACTIVE_META.update(
                source=out_p,
                match="auto_recalculated",
                exe_fingerprint=fp,
                profile_pe=prof.get("pe"),
                error=None,
            )
            _LOADED = True
            return _ACTIVE_PROFILE
        except Exception as e:
            _ACTIVE_META["error"] = "auto recalc failed: %s" % e

    # 3. 回落到仓库内置基线快照（offline_snapshot 或 mismatch）
    if first_valid is not None:
        _ACTIVE_PROFILE = first_valid
        _ACTIVE_META.update(
            source=first_valid_path,
            match="mismatch" if fp else "offline_snapshot",
            exe_fingerprint=fp,
            profile_pe=first_valid.get("pe"),
        )
        _LOADED = True
        return _ACTIVE_PROFILE

    _ACTIVE_PROFILE = None
    _ACTIVE_META.update(
        source=None,
        match="none",
        exe_fingerprint=fp,
        profile_pe=None,
    )
    _LOADED = True
    return None


def anchor(name, default=None):
    """读取单个 `exact` 锚点值（单值取 value，多值取首个 values[0]）；未命中返回 default。"""
    prof = load()
    if not prof:
        return default
    ent = (prof.get("anchors") or {}).get(name)
    if not isinstance(ent, dict) or ent.get("status") != "exact":
        return default
    if ent.get("value") is not None:
        return int(ent["value"])
    vals = ent.get("values")
    if vals:
        return int(vals[0])
    return default


def anchor_list(name, default=None):
    """读取多值锚点（返回 int tuple，例如多继承类 vtable）；未命中返回 default。"""
    prof = load()
    if not prof:
        return default
    ent = (prof.get("anchors") or {}).get(name)
    if not isinstance(ent, dict) or ent.get("status") != "exact":
        return default
    vals = ent.get("values")
    if vals:
        return tuple(int(v) for v in vals)
    if ent.get("value") is not None:
        return (int(ent["value"]),)
    return default


def commands():
    """返回 Profile 中完整的 `{class_name: spec}` 命令目录（无则返回 {}）。"""
    prof = load()
    if not prof:
        return {}
    return prof.get("commands") or {}


def command(cls):
    """查询单个命令类条目（精确类名）。"""
    return commands().get(cls)


def command_field(cls, key, default=None):
    """快速读取单个命令类的指定字段（如 vt / ctor / size / can_execute / execute）。"""
    ent = command(cls)
    if not isinstance(ent, dict):
        return default
    val = ent.get(key)
    return default if val is None else val


def load_rtti(force=False, auto_recalc=None):
    """优先从 `hoi4_recalc` 导出的 RTTI JSON 加载与 `rtti_win.build()` 同构的符号表。

    若尚未生成匹配当前 PE 指纹的 RTTI JSON，返回 None（由 `rtti_win.py` 平滑回落 `rtti_win.pkl`）。
    """
    global _RTTI_CACHE
    if _RTTI_CACHE is not None and not force:
        return _RTTI_CACHE

    prof = load()
    pe = (prof.get("pe") if prof else None) or {}
    ts = pe.get("timestamp")
    sz = pe.get("size_of_image")

    rtti_cands = []
    env_r = os.environ.get("HOI4_RTTI_JSON")
    if env_r:
        rtti_cands.append(env_r)
    if ts and sz:
        tag = "%08x_%08x" % (int(ts), int(sz))
        rtti_cands.append(os.path.join(PROFILES_DIR, "rtti_%s.json" % tag))
    for pat in (
        os.path.join(PROFILES_DIR, "rtti_*.json"),
        os.path.join(ROOT, "research", "windows", "rtti_*.json"),
    ):
        rtti_cands.extend(sorted(glob.glob(pat)))

    chosen = None
    for p in rtti_cands:
        if not p or not os.path.isfile(p):
            continue
        try:
            raw = _read_json(p)
        except Exception:
            continue
        rpe = raw.get("pe") or {}
        if ts and sz and (
            int(rpe.get("timestamp") or 0) != int(ts)
            or int(rpe.get("size_of_image") or 0) != int(sz)
        ):
            if p != env_r:
                continue
        chosen = (p, raw)
        break

    # 若当前 PE 指纹是新版（auto_recalculated 或 mismatch）且缺少对应 rtti_*.json，可按需一键生成
    if chosen is None and auto_recalc is True and recalc_available():
        exe = _installed_exe()
        fp = pe_fingerprint(exe) if exe else None
        if exe and fp:
            tag = "%08x_%08x" % (fp["timestamp"], fp["size_of_image"])
            out_p = os.path.join(PROFILES_DIR, "profile_%s.json" % tag)
            out_r = os.path.join(PROFILES_DIR, "rtti_%s.json" % tag)
            try:
                run_recalc(exe, out_profile=out_p, out_rtti=out_r)
                chosen = (out_r, _read_json(out_r))
            except Exception:
                chosen = None

    if chosen is None:
        return None

    src_path, raw = chosen
    vtables = {
        str(k): sorted(int(x) for x in v)
        for k, v in (raw.get("vtables") or {}).items()
    }
    vt_rev = {}
    for cls, vts in vtables.items():
        for v in vts:
            vt_rev[v] = cls
    _RTTI_CACHE = {
        "vtables": vtables,
        "vt": vt_rev,
        "pe": raw.get("pe"),
        "source": src_path,
    }
    return _RTTI_CACHE


def verify_live_process(proc):
    """纯只读校验运行中 `hoi4.exe` 进程头是否与当前加载 Profile 的 PE 指纹匹配。"""
    prof = load()
    if not prof:
        return {"ok": False, "status": "no_profile"}
    try:
        head = proc.read(proc.base, 4096)
        live_fp = parse_pe_header(head)
    except Exception as e:
        return {"ok": False, "status": "read_error", "error": str(e)}
    if not live_fp:
        return {"ok": False, "status": "invalid_pe_header"}
    pe = prof.get("pe") or {}
    ok = (
        int(pe.get("size_of_image") or 0) == live_fp["size_of_image"]
        and int(pe.get("timestamp") or 0) == live_fp["timestamp"]
    )
    return {
        "ok": ok,
        "status": "exact" if ok else "mismatch",
        "live": {"size_of_image": hex(live_fp["size_of_image"]),
                 "timestamp": hex(live_fp["timestamp"])},
        "profile": {"build_id": pe.get("build_id"),
                    "size_of_image": hex(int(pe.get("size_of_image") or 0)),
                    "timestamp": hex(int(pe.get("timestamp") or 0))},
    }


def summary():
    """紧凑 Profile 摘要（供 `hoi4_status` 与诊断工具展示）。"""
    prof = load()
    pe = (prof.get("pe") if prof else None) or {}
    anchors_dict = (prof.get("anchors") if prof else None) or {}
    exact_cnt = sum(1 for a in anchors_dict.values() if isinstance(a, dict) and a.get("status") == "exact")
    return {
        "loaded": prof is not None,
        "match": _ACTIVE_META.get("match"),
        "source": _ACTIVE_META.get("source"),
        "build_id": pe.get("build_id"),
        "size_of_image": hex(int(pe["size_of_image"])) if pe.get("size_of_image") else None,
        "timestamp": hex(int(pe["timestamp"])) if pe.get("timestamp") else None,
        "anchors_exact": exact_cnt,
        "anchors_total": len(anchors_dict),
        "commands_count": int(prof.get("commands_count") or 0) if prof else 0,
        "recalc_cli": recalc_available(),
        "error": _ACTIVE_META.get("error"),
    }
