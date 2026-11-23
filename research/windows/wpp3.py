import struct, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "calling"))
import winject as W
pid = W.find_pid(); print("pid", pid)
inj = W.Injector(pid); inj.attach()
IMG = W.module_base(pid, "hoi4.exe"); ALLOC = IMG + 0x30FD000
sc = inj.scratch; SRES, ARR, STRS = sc+0x1000, sc+0x1040, sc+0x1080
gs = struct.unpack("<Q", inj._read(IMG + 0x33DD6B0, 8))[0]
print("gs %#x" % gs)
if not (0x10000 < gs < 0x7FFFFFFFFFFF):
    print("NO GAMESTATE"); inj.detach(); raise SystemExit
data, cap, size, _ = struct.unpack("<QIIQ", inj._read(gs + 0x310, 0x18))
C = [struct.unpack("<Q", inj._read(data+i*8,8))[0] for i in range(size)]
def pp_of(i):
    pol = struct.unpack("<Q", inj._read(C[i] + 0xF90, 8))[0]
    return struct.unpack("<q", inj._read(pol + 0xE0, 8))[0]/100000.0
def cmd(va, args):
    buf = bytearray(0x200)
    struct.pack_into("<QQ", buf, 0x18, 0, 0xF)
    struct.pack_into("<QIIQ", buf, 0x40, STRS, 8, len(args), ALLOC)
    for i,a in enumerate(args):
        raw=a.encode(); o=0x80+i*0x20
        buf[o:o+min(len(raw),15)]=raw[:15]
        struct.pack_into("<QQ", buf, o+0x10, len(raw), 15)
    inj._write(sc+0x1000, buf)
    r = inj.call(va, (SRES, ARR), timeout=6.0)
    b = inj._read(SRES+8, 0x20); n,c = struct.unpack_from("<QQ", b, 0x10)
    return inj._read(SRES,1)[0], (b[:n] if c<=15 else inj._read(struct.unpack_from("<Q",b,0)[0], min(n,120))), r
print("GER PP before %.3f" % pp_of(1))
for k in range(3):
    ok,msg,r = cmd(IMG+0x285FF0, ["100"])
    print("call%d ok=%d ret=%#x msg=%r  PP=%.3f" % (k+1, ok, r, msg, pp_of(1)))
print("call_count", inj.call_count, "tid", inj.last_tid)
inj.detach()
print("alive:", W.module_base(pid,"hoi4.exe") is not None)
