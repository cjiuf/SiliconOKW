"""One owner for the HOI4 process, many callers (legacy experimental tool).

历史遗留的实验性多进程代理服务端，已移出生产目录 `calling/` 至 `research/windows/legacy/`。
安全约束：
1. 仅允许绑定本机回环地址（127.0.0.1 / ::1 / localhost），拒绝任何非回环监听；
2. 强制要求环境变量 `HOI4_GAMEHOST_TOKEN`（非空），每条请求必须携带匹配 token，使用常量时间比较。

    HOI4_GAMEHOST_TOKEN=... python research/windows/legacy/gamehost.py --port 8765
    from gh import GH; GH().call('state')

Protocol: one JSON object per line, `{"token": ..., "op": ..., "args": {...}}`,
answered with `{"ok": bool, "result": ...}` or `{"ok": false, "error": "..."}`.
"""
import argparse
import hmac
import ipaddress
import json
import os
import socketserver
import struct
import sys
import threading
import time

# 将仓库 calling/ 目录加入搜索路径，供遗留实验工具导入核心模块
_CALLING_DIR = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "calling"))
if _CALLING_DIR not in sys.path:
    sys.path.insert(0, _CALLING_DIR)

import cmdwin
import offsets_win as _OW
import peacewin

LOCK = threading.Lock()
G = None
TOKEN_ENV = "HOI4_GAMEHOST_TOKEN"
_ALLOWED_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


def validate_loopback_host(host: str) -> str:
    """校验监听地址必须为本机回环地址，禁止暴露到局域网或 0.0.0.0。"""
    h = (host or "").strip()
    if h.lower() in _ALLOWED_LOOPBACK_HOSTS:
        return h
    try:
        if ipaddress.ip_address(h).is_loopback:
            return h
    except ValueError:
        pass
    raise ValueError(f"gamehost 仅允许监听回环地址（如 127.0.0.1 / ::1），拒绝绑定 {host!r}")


def require_auth_token(token: str | None = None) -> str:
    """要求配置非空本地鉴权令牌，防止无鉴权本地 TCP 控制游戏进程。"""
    val = (token if token is not None else os.environ.get(TOKEN_ENV, "")).strip()
    if not val:
        raise RuntimeError(f"必须设置非空环境变量 {TOKEN_ENV} 才能启动 gamehost")
    return val


def verify_token(provided: object, expected: str) -> bool:
    """常量时间比对请求携带的鉴权令牌。"""
    if not isinstance(provided, str) or not expected:
        return False
    return hmac.compare_digest(provided, expected)


def _game():
    global G
    if G is None:
        from game import Game
        G = Game()
    return G


# --- operations -------------------------------------------------------------
# Every op takes the live Game and a dict; anything that touches the process
# must run under LOCK.

def op_state(g, a):
    import wprov
    from m3run import divs_of_tag
    tags = a.get('tags') or ['GER']
    out = {}
    for t in tags:
        try:
            owned = wprov.owned(g, t)
            out[t] = {'provinces': len(sum(owned.values(), [])),
                      'states': sorted(owned),
                      'divisions': len(divs_of_tag(g, t))}
        except Exception as e:
            out[t] = {'error': str(e)[:120]}
    out['player'] = cmdwin.player_tag_id(g)
    out['conference'] = [hex(c) for c in peacewin.conference(g)]
    return out


def op_speed(g, a):
    return cmdwin.set_game_speed(g, int(a.get('speed', 3)))


def op_pause(g, a):
    p = g.p
    i = p.u64(p.base + _OW.G_IDLER)
    if not i:
        return {'ok': False, 'error': 'no idler'}
    p.write(i + _OW.SESSION_IDLER_PAUSED, b'\x01' if a.get('paused') else b'\x00')
    return {'paused': bool(a.get('paused'))}


def op_declare_war(g, a):
    import wprov
    frm = wprov.cidx(g, a['from']) if a.get('from') else None
    to = wprov.cidx(g, a['to'])
    objs = cmdwin.make_declare_war(g, to, from_id=frm, wargoal=int(a.get('wargoal', 0)),
                                   force=True)
    return {'posted': bool(cmdwin.post(g, objs['cmd'])), 'from': a.get('from'),
            'to': a['to']}


def op_peace_demands(g, a):
    cs = peacewin.conference(g)
    if not cs:
        return {'ok': False, 'error': 'no conference'}
    import wprov
    conf = cs[0]
    winner = wprov.cidx(g, a.get('winner', 'GER'))
    loser = wprov.cidx(g, a.get('loser'))
    kinds = a.get('kinds') or ['state']
    got = []
    for kind in kinds:
        got += peacewin.build_actions(g, loser, kind)
    turn = peacewin.new_turn(g)
    if got:
        data = cmdwin.alloc(g, 8 * len(got))
        g.p.write(data, b''.join(struct.pack('<Q', x) for x in got))
        g.p.write(turn + 0x10, struct.pack('<QiiQ', data, len(got), len(got),
                                           g.p.base + _OW.G_DEFAULT_ALLOC))
    # EndTurn 的 Execute 直接在和会窗口开启时执行结算
    cmd = cmdwin.alloc(g, peacewin.ENDTURN_SIZE)
    g.p.write(cmd, b'\0' * peacewin.ENDTURN_SIZE)
    g.p.write(cmd, struct.pack('<Q', g.p.base + peacewin.ENDTURN_VT))
    g.p.write(cmd + 0x28, struct.pack('<i', winner))
    g.p.write(cmd + 0x30, struct.pack('<Q', turn))
    g.call(g.p.base + _OW.PEACE_EXEC_ENDTURN, (cmd,), absolute=True, force=True)
    time.sleep(1)
    return {'actions': len(got), 'types': [g.p.i32(x + 8) for x in got],
            'maps': {hex(o): g.p.u64(conf + o + 8) for o in (0x1C8, 0x1D8, 0x1E8, 0x1F8)}}


def op_peace_done(g, a):
    import wprov
    cs = peacewin.conference(g)
    if not cs:
        return {'ok': False, 'error': 'no conference'}
    idx = wprov.cidx(g, a.get('country', 'GER'))
    cmd = cmdwin.alloc(g, peacewin.DONE_SIZE)
    g.p.write(cmd, b'\0' * peacewin.DONE_SIZE)
    g.p.write(cmd, struct.pack('<Q', g.p.base + peacewin.DONE_VT))
    g.p.write(cmd + 0x28, struct.pack('<i', idx))
    g.call(g.p.base + _OW.PEACE_EXEC_DONE, (cmd,), absolute=True, force=True)
    time.sleep(1)
    return {'conferences_left': len(peacewin.conference(g))}


OPS = {
    'state': op_state,
    'speed': op_speed,
    'pause': op_pause,
    'declare_war': op_declare_war,
    'peace_demands': op_peace_demands,
    'peace_done': op_peace_done,
}


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        expected_token = getattr(self.server, 'auth_token', '')
        for line in self.rfile:
            line = line.strip()
            if not line:
                continue
            try:
                req = json.loads(line)
                if not isinstance(req, dict):
                    raise ValueError('request must be a JSON object')
                if not verify_token(req.get('token'), expected_token):
                    out = {'ok': False, 'error': 'unauthorized'}
                    self.wfile.write((json.dumps(out) + '\n').encode())
                    continue
                op = req.get('op')
                if op not in OPS:
                    raise KeyError('unknown op %r' % op)
                with LOCK:
                    res = OPS[op](_game(), req.get('args') or {})
                out = {'ok': True, 'result': res}
            except Exception as e:
                out = {'ok': False, 'error': '%s: %s' % (type(e).__name__, str(e)[:300])}
            self.wfile.write((json.dumps(out) + '\n').encode())


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = False

    def __init__(self, server_address, RequestHandlerClass, auth_token=None, bind_and_activate=True):
        host, port = server_address
        safe_host = validate_loopback_host(host)
        self.auth_token = require_auth_token(auth_token)
        super().__init__((safe_host, port), RequestHandlerClass, bind_and_activate=bind_and_activate)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--port', type=int, default=8765)
    ap.add_argument('--host', default='127.0.0.1',
                    help='仅允许回环地址（127.0.0.1 / ::1 / localhost）')
    a = ap.parse_args()
    host = validate_loopback_host(a.host)
    token = require_auth_token()
    srv = Server((host, a.port), Handler, auth_token=token)
    print('gamehost on %s:%d  ops=%s' % (host, a.port, ','.join(sorted(OPS))), flush=True)
    srv.serve_forever()


if __name__ == '__main__':
    main()
