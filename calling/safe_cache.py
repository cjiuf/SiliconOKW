# -*- coding: utf-8 -*-
"""安全缓存读写层（纯 JSON / JSON.GZ 优先 + Pickle SHA256 完整性防篡改）。

设计原则：
1. 静态数据表默认使用纯 JSON（`.json`）或 gzip 压缩 JSON（`.json.gz`），通过极简确定性
   标签编码无损往返 Python `int` 键字典、`tuple`、`set` 等基础容器，彻底消除反序列化
   任意代码执行风险。
2. 保留对本地生成 `.pkl`（如 `hoi4_recalc assets` 生成的 Mod 缓存或未入库本地索引）的
   平滑兼容，但施加三层安全护栏：
   - 设置 `HOI4_NO_PICKLE=1` 时全局禁止任何 `pickle.load`，直接抛 `UnsafePickleError`；
   - 任何存在于哈希清单中的 `.pkl` 若 SHA256 校验失败，立即抛 `CacheIntegrityError`；
   - 未经本地哈希清单验证的陌生 `.pkl` 绝不允许抢占已存在的 `.json`/`.json.gz`。
3. 所有写入均使用同目录 `.tmp` + `os.replace` 原子落盘，绝不写半截文件。
"""
from __future__ import annotations

from array import array as _array
import base64
import gzip
import hashlib
import json
import os
import pickle
import tempfile
from typing import Any, Dict, Iterable, Optional, Tuple

_ALLOWED_ARRAY_TYPECODES = frozenset({"b", "B", "h", "H", "i", "I", "l", "L", "q", "Q", "f", "d"})

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_HASH_MANIFEST = os.path.join(HERE, "cache_hashes.json")

_NO_PICKLE_ENV = "HOI4_NO_PICKLE"
_STRICT_HASH_ENV = "HOI4_STRICT_PICKLE_HASH"


class UnsafePickleError(RuntimeError):
    """在 HOI4_NO_PICKLE=1 或禁止 pickle 的上下文尝试读取 .pkl 时抛出。"""


class CacheIntegrityError(RuntimeError):
    """缓存文件 SHA256 校验不匹配或未在严格白名单中登记时抛出。"""


def _env_true(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def no_pickle_enabled() -> bool:
    return _env_true(_NO_PICKLE_ENV)


def strict_hash_enabled() -> bool:
    return _env_true(_STRICT_HASH_ENV)


def _strip_ext(path: str) -> str:
    low = path.lower()
    if low.endswith(".json.gz"):
        return path[:-8]
    if low.endswith(".json"):
        return path[:-5]
    if low.endswith(".pkl"):
        return path[:-4]
    return path


def candidate_paths(path: str) -> Tuple[str, str, str]:
    """根据给定路径推导 (json_path, json_gz_path, pkl_path)。"""
    stem = _strip_ext(str(path))
    return stem + ".json", stem + ".json.gz", stem + ".pkl"


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(1 << 20)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def local_hash_manifest_path() -> str:
    """返回用户可写目录下的本地 pickle 哈希登记表路径（不污染源码仓库）。"""
    try:
        import session_state as _ss
        return _ss.state_path("pickle_hashes.json")
    except Exception:
        base = os.environ.get("HOI4_STATE_DIR") or tempfile.gettempdir()
        return os.path.join(base, "pickle_hashes.json")


def _norm_key(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


def _load_manifest(path: str) -> Dict[str, str]:
    if not path or not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        if isinstance(raw, dict):
            entries = raw.get("hashes") if isinstance(raw.get("hashes"), dict) else raw
            return {str(k): str(v).lower() for k, v in entries.items() if isinstance(v, str)}
    except Exception as e:
        try:
            import diag as _diag
            _diag.warn("safe_cache.load_manifest", e, detail=path)
        except Exception:
            pass
    return {}


def _lookup_expected_hash(path: str, manifest_paths: Iterable[str]) -> Optional[str]:
    abs_k = _norm_key(path)
    root = os.path.dirname(HERE)
    rel_k = None
    try:
        rel = os.path.relpath(os.path.abspath(path), root).replace("\\", "/")
        if not rel.startswith(".."):
            rel_k = rel.lower()
    except Exception:
        rel_k = None

    for mp in manifest_paths:
        m = _load_manifest(mp)
        if not m:
            continue
        for k, v in m.items():
            kn = k.replace("\\", "/").lower()
            if _norm_key(k) == abs_k or (rel_k is not None and kn == rel_k):
                return v
    return None


def record_pickle_hash(path: str, *, manifest_path: Optional[str] = None) -> str:
    """将本地可信 .pkl 文件的 SHA256 原子登记到本地哈希清单。"""
    digest = sha256_file(path)
    mp = manifest_path or local_hash_manifest_path()
    parent = os.path.dirname(mp)
    if parent:
        os.makedirs(parent, exist_ok=True)
    cur = _load_manifest(mp)
    cur[_norm_key(path)] = digest
    tmp = f"{mp}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"schema": 1, "hashes": cur}, fh, indent=2, sort_keys=True)
    os.replace(tmp, mp)
    return digest


def verify_pickle_hash(
    path: str,
    *,
    manifest_paths: Optional[Iterable[str]] = None,
    require_listed: bool = False,
) -> Tuple[bool, str]:
    """校验 `.pkl` 文件的 SHA256 完整性。返回 `(is_listed_and_valid, status)`。"""
    mps = tuple(manifest_paths) if manifest_paths is not None else (
        local_hash_manifest_path(),
        REPO_HASH_MANIFEST,
    )
    expected = _lookup_expected_hash(path, mps)
    if expected is None:
        if require_listed:
            raise CacheIntegrityError(
                "严格模式下拒绝加载未在哈希白名单登记的 pickle 文件: %s" % path
            )
        return False, "unlisted"
    actual = sha256_file(path)
    if actual.lower() != expected.lower():
        raise CacheIntegrityError(
            "pickle 文件 SHA256 校验失败（可能已被篡改）: %s (expected=%s actual=%s)"
            % (path, expected[:16], actual[:16])
        )
    return True, "verified"


# ------------------------------------------------------- 无损纯 JSON 编解码器
def _sort_key_for_set(item: Any) -> Tuple[str, str]:
    return (type(item).__name__, repr(item))


def encode_obj(obj: Any) -> Any:
    """将 Python 基础对象无损编码为纯 JSON 安全树（支持 int 键 dict / tuple / set / bytes / array）。"""
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if isinstance(obj, (bytes, bytearray)):
        return {"__t__": "bytes", "v": base64.b64encode(bytes(obj)).decode("ascii")}
    if isinstance(obj, _array):
        if obj.typecode not in _ALLOWED_ARRAY_TYPECODES:
            raise TypeError("safe_cache 不支持序列化 array typecode: %r" % obj.typecode)
        return {
            "__t__": "array",
            "tc": obj.typecode,
            "v": base64.b64encode(obj.tobytes()).decode("ascii"),
        }
    if isinstance(obj, list):
        return [encode_obj(x) for x in obj]
    if isinstance(obj, tuple):
        return {"__t__": "tuple", "v": [encode_obj(x) for x in obj]}
    if isinstance(obj, (set, frozenset)):
        items = sorted(obj, key=_sort_key_for_set)
        return {"__t__": "set", "v": [encode_obj(x) for x in items]}
    if isinstance(obj, dict):
        if not obj:
            return {}
        keys = list(obj.keys())
        if all(isinstance(k, str) for k in keys):
            if "__t__" in obj:
                return {"__t__": "dict", "v": {k: encode_obj(v) for k, v in obj.items()}}
            return {k: encode_obj(v) for k, v in obj.items()}
        if all(isinstance(k, int) and not isinstance(k, bool) for k in keys):
            return {"__t__": "idict", "v": {str(k): encode_obj(v) for k, v in obj.items()}}
        return {"__t__": "kdict", "v": [[encode_obj(k), encode_obj(v)] for k, v in obj.items()]}
    raise TypeError("safe_cache 不支持序列化类型: %s" % type(obj).__name__)


def decode_obj(raw: Any) -> Any:
    """将 `encode_obj` 生成的 JSON 树无损还原为原始 Python 数据结构。"""
    if raw is None or isinstance(raw, (bool, int, float, str)):
        return raw
    if isinstance(raw, list):
        return [decode_obj(x) for x in raw]
    if isinstance(raw, dict):
        tag = raw.get("__t__")
        if tag is not None and len(raw) == 2 and "v" in raw:
            val = raw["v"]
            if tag == "tuple":
                return tuple(decode_obj(x) for x in val)
            if tag == "set":
                return {decode_obj(x) for x in val}
            if tag == "bytes" and isinstance(val, str):
                return base64.b64decode(val.encode("ascii"))
            if tag == "idict":
                return {int(k): decode_obj(v) for k, v in val.items()}
            if tag == "kdict":
                return {decode_obj(k): decode_obj(v) for k, v in val}
            if tag == "dict":
                return {k: decode_obj(v) for k, v in val.items()}
        if tag == "array" and len(raw) == 3 and "v" in raw and "tc" in raw:
            tc = raw["tc"]
            val = raw["v"]
            if tc in _ALLOWED_ARRAY_TYPECODES and isinstance(val, str):
                arr = _array(tc)
                arr.frombytes(base64.b64decode(val.encode("ascii")))
                return arr
        return {k: decode_obj(v) for k, v in raw.items()}
    return raw


# --------------------------------------------------------------- 统一加载/保存
def resolve_path(
    path: str,
    *,
    allow_pickle: Optional[bool] = None,
    manifest_paths: Optional[Iterable[str]] = None,
) -> Optional[str]:
    """按安全优先级解析实际应加载的缓存文件路径。

    优先级：
      1. 显式传入 `.json` / `.json.gz` 且存在 -> 直接返回；
      2. 若存在 `.json` 或 `.json.gz`，默认优先返回安全格式；仅当 `.pkl` 同时满足
         允许 pickle + 已登记可信本地 SHA256 且校验通过 + mtime 更新于安全格式时，
         才优先返回较新的本地 Mod 重建 `.pkl`；
      3. 仅存在 `.pkl` 时返回 `.pkl`（由 `load()` 做 `HOI4_NO_PICKLE` 与哈希校验）。
    """
    p = str(path)
    low = p.lower()
    if low.endswith(".json") and os.path.isfile(p):
        return p
    if low.endswith(".json.gz") and os.path.isfile(p):
        return p

    jp, gz, pkl = candidate_paths(p)
    safe_hit = jp if os.path.isfile(jp) else (gz if os.path.isfile(gz) else None)
    pkl_hit = pkl if os.path.isfile(pkl) else None

    use_pkl = (not no_pickle_enabled()) if allow_pickle is None else bool(allow_pickle)
    if safe_hit and pkl_hit and use_pkl:
        try:
            if os.path.getmtime(pkl_hit) > os.path.getmtime(safe_hit):
                listed, _ = verify_pickle_hash(pkl_hit, manifest_paths=manifest_paths, require_listed=False)
                if listed:
                    return pkl_hit
        except CacheIntegrityError:
            raise
        except Exception:
            pass
        return safe_hit
    if safe_hit:
        return safe_hit
    if pkl_hit:
        return pkl_hit
    return None


def exists(path: str) -> bool:
    """判断给定基路径或 .pkl 路径是否存在可用的安全缓存或 .pkl 缓存。"""
    jp, gz, pkl = candidate_paths(path)
    return os.path.isfile(jp) or os.path.isfile(gz) or os.path.isfile(pkl)


def load(
    path: str,
    *,
    allow_pickle: Optional[bool] = None,
    strict_hash: Optional[bool] = None,
    manifest_paths: Optional[Iterable[str]] = None,
) -> Any:
    """透明加载 `.json` / `.json.gz` /（受护栏保护的）`.pkl` 缓存对象。"""
    resolved = resolve_path(path, allow_pickle=allow_pickle, manifest_paths=manifest_paths)
    if not resolved:
        raise FileNotFoundError("cache file not found (tried .json/.json.gz/.pkl): %s" % path)

    low = resolved.lower()
    if low.endswith(".json"):
        with open(resolved, "r", encoding="utf-8") as fh:
            return decode_obj(json.load(fh))
    if low.endswith(".json.gz"):
        with gzip.open(resolved, "rt", encoding="utf-8") as fh:
            return decode_obj(json.load(fh))

    # 回落到 .pkl：严格检查开关与 SHA256 白名单
    use_pkl = (not no_pickle_enabled()) if allow_pickle is None else bool(allow_pickle)
    if not use_pkl:
        raise UnsafePickleError(
            "HOI4_NO_PICKLE 已启用，拒绝反序列化 pickle 文件: %s" % resolved
        )
    req_listed = strict_hash_enabled() if strict_hash is None else bool(strict_hash)
    listed, _status = verify_pickle_hash(
        resolved, manifest_paths=manifest_paths, require_listed=req_listed
    )
    if not listed:
        try:
            import diag as _diag
            _diag.warn("safe_cache.unverified_pickle", "loading unlisted local pickle", detail=resolved)
        except Exception:
            pass
    with open(resolved, "rb") as fh:
        return pickle.load(fh)


def save(
    path: str,
    obj: Any,
    *,
    compress: Optional[bool] = None,
    fmt: Optional[str] = None,
) -> str:
    """原子保存对象至安全 `.json` / `.json.gz`（或显式 `fmt='pkl'` 时写 `.pkl` 并登记哈希）。"""
    p = str(path)
    low = p.lower()
    if fmt is None:
        if low.endswith(".json.gz") or compress is True:
            fmt = "json.gz"
        elif low.endswith(".json") or compress is False:
            fmt = "json"
        elif low.endswith(".pkl"):
            # 默认将 .pkl 保存请求升级为安全 .json，避免继续写入新 pickle
            fmt = "json"
        else:
            fmt = "json"

    jp, gz, pkl = candidate_paths(p)
    if fmt == "json":
        target = p if low.endswith(".json") else jp
    elif fmt == "json.gz":
        target = p if low.endswith(".json.gz") else gz
    elif fmt == "pkl":
        target = p if low.endswith(".pkl") else pkl
    else:
        raise ValueError("unsupported cache format: %r" % fmt)

    parent = os.path.dirname(target)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = f"{target}.{os.getpid()}.tmp"

    if fmt == "json":
        payload = encode_obj(obj)
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        os.replace(tmp, target)
        return target

    if fmt == "json.gz":
        payload = encode_obj(obj)
        with gzip.open(tmp, "wt", encoding="utf-8", compresslevel=6) as fh:
            json.dump(payload, fh, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        os.replace(tmp, target)
        return target

    with open(tmp, "wb") as fh:
        pickle.dump(obj, fh, protocol=4)
    os.replace(tmp, target)
    try:
        record_pickle_hash(target)
    except Exception:
        pass
    return target
