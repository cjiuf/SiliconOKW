import struct, zlib

def convert(src, dst):
    d = open(src, 'rb').read()
    hdr_size = 118
    ncolors = 256
    width, height = 1280, 720
    bpl = 5120
    off = hdr_size
    end = d.index(b'\0', off)
    off = end + 1
    off += ncolors * 12
    rows = []
    for y in range(height):
        row = d[off + y * bpl: off + y * bpl + width * 4]
        row = row + b'\x00' * (width * 4 - len(row))
        # masks: red 0xFF0000, green 0xFF00, blue 0xFF at 32bpp little-endian
        # bytes per pixel: [pad?, b, g, r] check empirically: assume little-endian dword = XRGB
        px = bytearray(width * 4)
        for x in range(width):
            b0, b1, b2, b3 = row[x*4:x*4+4]
            # little-endian: value = b0 | b1<<8 | b2<<16 | b3<<24
            # mask red 0xFF0000 -> b2; green -> b1; blue -> b0
            px[x*4+0] = b2  # R
            px[x*4+1] = b1  # G
            px[x*4+2] = b0  # B
            px[x*4+3] = 0xFF
        rows.append(bytes(px))
    raw = b''.join(b'\x00' + r for r in rows)

    def chunk(typ, data):
        return struct.pack('>I', len(data)) + typ + data + struct.pack('>I', zlib.crc32(typ + data) & 0xffffffff)

    png = b'\x89PNG\r\n\x1a\n'
    png += chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 6, 0, 0, 0))
    png += chunk(b'IDAT', zlib.compress(raw, 6))
    png += chunk(b'IEND', b'')
    open(dst, 'wb').write(png)
    print("wrote", dst, len(png))

if __name__ == '__main__':
    import sys
    if len(sys.argv) < 2:
        raise SystemExit('usage: xwd2png.py <in.xwd> [out.png]')
    convert(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else 'scr.png')
