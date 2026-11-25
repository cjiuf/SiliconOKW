"""验证 legacy gamehost.py / gh.py 已移出生产目录且具备回环与令牌鉴权保护。"""
import os
import pathlib
import sys
import threading

ROOT = pathlib.Path(__file__).resolve().parents[2]
LEGACY_DIR = ROOT / "research" / "windows" / "legacy"
CALLING_DIR = ROOT / "calling"

if str(LEGACY_DIR) not in sys.path:
    sys.path.insert(0, str(LEGACY_DIR))

import gamehost
import gh


def test_moved_out_of_calling():
    assert not (CALLING_DIR / "gamehost.py").exists(), "gamehost.py 不应留在 calling/"
    assert not (CALLING_DIR / "gh.py").exists(), "gh.py 不应留在 calling/"
    assert (LEGACY_DIR / "gamehost.py").exists()
    assert (LEGACY_DIR / "gh.py").exists()


def test_loopback_enforcement():
    for ok_host in ("127.0.0.1", "localhost", "::1"):
        assert gamehost.validate_loopback_host(ok_host) == ok_host
    for bad_host in ("0.0.0.0", "192.168.1.10", "10.0.0.5", "example.com", ""):
        raised = False
        try:
            gamehost.validate_loopback_host(bad_host)
        except ValueError:
            raised = True
        assert raised, f"非回环地址 {bad_host!r} 应被拒绝"


def test_token_required_and_enforced():
    old_env = os.environ.pop(gamehost.TOKEN_ENV, None)
    try:
        raised = False
        try:
            gamehost.Server(("127.0.0.1", 0), gamehost.Handler)
        except RuntimeError:
            raised = True
        assert raised, "缺少 HOI4_GAMEHOST_TOKEN 时应拒绝启动"
    finally:
        if old_env is not None:
            os.environ[gamehost.TOKEN_ENV] = old_env

    # 启动临时回环服务，注册只读桩操作验证鉴权拒绝与放行
    old_ops = dict(gamehost.OPS)
    old_game = gamehost._game
    gamehost.OPS["ping"] = lambda g, a: {"echo": a.get("msg", "")}
    gamehost._game = lambda: None
    srv = gamehost.Server(("127.0.0.1", 0), gamehost.Handler, auth_token="secret-test-token")
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        # 1. 错误令牌与空令牌必须被拒绝
        for bad_tok in ("", "wrong-token"):
            err = None
            try:
                gh.GH(host="127.0.0.1", port=port, token=bad_tok).call("ping", msg="hi")
            except RuntimeError as e:
                err = str(e)
            assert err == "unauthorized", f"期望 unauthorized，实得 {err!r}"

        # 2. 正确令牌应正常返回
        res = gh.GH(host="127.0.0.1", port=port, token="secret-test-token").call("ping", msg="ok")
        assert res == {"echo": "ok"}
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(timeout=2)
        gamehost.OPS.clear()
        gamehost.OPS.update(old_ops)
        gamehost._game = old_game


def test_bundle_excludes_legacy_and_ptr_compat():
    tools_dir = ROOT / "tools"
    mcp_dir = ROOT / "mcp"
    for d in (tools_dir, mcp_dir):
        if str(d) not in sys.path:
            sys.path.insert(0, str(d))
    import build_bundle
    assert "legacy" in build_bundle.SKIP_DIRS, "build_bundle.SKIP_DIRS 必须排除 legacy 目录"

    import server as _srv
    assert _srv._ptr(None) is None
    assert _srv._ptr(12345) == 12345
    assert _srv._ptr("0x5606b3212a40") == 0x5606B3212A40
    assert _srv._ptr(" 0X10 ") == 16
    assert _srv._ptr("0123") == 123


if __name__ == "__main__":
    test_moved_out_of_calling()
    test_loopback_enforcement()
    test_token_required_and_enforced()
    test_bundle_excludes_legacy_and_ptr_compat()
    print("PASS: test_gamehost_security")
