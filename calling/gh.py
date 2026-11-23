"""Client for gamehost -- import this from any agent process.

    from gh import GH
    print(GH().call('state', tags=['GER', 'HOL']))
"""
import json
import socket


class GH:
    def __init__(self, host='127.0.0.1', port=8765, timeout=120):
        self.addr = (host, port)
        self.timeout = timeout

    def call(self, op, **args):
        with socket.create_connection(self.addr, timeout=self.timeout) as s:
            s.sendall((json.dumps({'op': op, 'args': args}) + '\n').encode())
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
