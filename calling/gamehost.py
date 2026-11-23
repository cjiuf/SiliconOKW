"""One owner for the HOI4 process, many callers.

The game has a single tick thread and no locking around the objects we poke.
Two agents injecting at the same time corrupt each other -- that is what killed
the process twice on 2026-11-14/15.  So parallelism has to live *outside* the
game: this module owns the process handle and runs every request one at a time,
in arrival order.

    python -m gamehost --port 8765          # start the owner
    from gh import GH; GH().call('state')   # any other process / agent

Protocol: one JSON object per line, `{"op": ..., "args": {...}}`, answered with
`{"ok": bool, "result": ...}` or `{"ok": false, "error": "..."}`.
"""
import argparse
import json
import os
import socket
import socketserver
import struct
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cmdwin
import peacewin

LOCK = threading.Lock()
G = None


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
    i = p.u64(p.base + 0x33DDB08)
    if not i:
        return {'ok': False, 'error': 'no idler'}
    p.write(i + 0x6C1, b'\x01' if a.get('paused') else b'\x00')
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
                                           g.p.base + 0x30FD000))
    # EndTurn's Execute computes the conference itself; posting does not drain
    # while the window is open, so call Execute straight away.
    cmd = cmdwin.alloc(g, peacewin.ENDTURN_SIZE)
    g.p.write(cmd, b'\0' * peacewin.ENDTURN_SIZE)
    g.p.write(cmd, struct.pack('<Q', g.p.base + peacewin.ENDTURN_VT))
    g.p.write(cmd + 0x28, struct.pack('<i', winner))
    g.p.write(cmd + 0x30, struct.pack('<Q', turn))
    g.call(g.p.base + 0x1ED2300, (cmd,), absolute=True, force=True)
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
    g.call(g.p.base + 0x1ED22E0, (cmd,), absolute=True, force=True)
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
        for line in self.rfile:
            line = line.strip()
            if not line:
                continue
            try:
                req = json.loads(line)
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--port', type=int, default=8765)
    ap.add_argument('--host', default='127.0.0.1')
    a = ap.parse_args()
    srv = Server((a.host, a.port), Handler)
    print('gamehost on %s:%d  ops=%s' % (a.host, a.port, ','.join(sorted(OPS))), flush=True)
    srv.serve_forever()


if __name__ == '__main__':
    main()
