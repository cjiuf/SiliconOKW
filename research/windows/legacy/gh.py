"""Client for legacy gamehost -- import this from any local agent process.

    HOI4_GAMEHOST_TOKEN=... python ...
    from gh import GH
    print(GH().call('state', tags=['GER', 'HOL']))
"""
import json
import os
import socket

TOKEN_ENV = "HOI4_GAMEHOST_TOKEN"


class GH:
    def __init__(self, host='127.0.0.1', port=8765, timeout=120, token=None):
        self.addr = (host, port)
        self.timeout = timeout
        # 默认从环境变量读取本地鉴权令牌，避免无鉴权调用
        self.token = token if token is not None else os.environ.get(TOKEN_ENV, '')

    def call(self, op, **args):
        payload = {'token': self.token, 'op': op, 'args': args}
        with socket.create_connection(self.addr, timeout=self.timeout) as s:
            s.sendall((json.dumps(payload) + '\n').encode())
            buf = b''
            while not buf.endswith(b'\n'):
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
        r = json.loads(buf.decode())
        if not r.get('ok'):
            raise RuntimeError(r.get('error') or 'gamehost error')
        return r['result']
