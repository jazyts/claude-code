"""アプリのアイコン（.ico）を標準ライブラリだけで作る。"""

import struct
import zlib

BG = (0x12, 0x3E, 0x7C, 255)
FG = (255, 255, 255, 255)
ACCENT = (0x4C, 0xC3, 0x8A, 255)


def _pixels(n):
    r = n * 0.18
    rows = []
    for y in range(n):
        row = []
        for x in range(n):
            # 角の丸い四角
            cx = min(max(x + 0.5, r), n - r)
            cy = min(max(y + 0.5, r), n - r)
            inside = (x + 0.5 - cx) ** 2 + (y + 0.5 - cy) ** 2 <= r * r
            color = BG if inside else (0, 0, 0, 0)
            fx, fy = (x + 0.5) / n, (y + 0.5) / n
            # 帳簿の罫線3本と、緑の帯
            if inside and 0.24 <= fx <= 0.76:
                for top in (0.30, 0.46, 0.62):
                    if top <= fy <= top + 0.07:
                        color = FG
            if inside and 0.24 <= fx <= 0.52 and 0.76 <= fy <= 0.83:
                color = ACCENT
            row.append(color)
        rows.append(row)
    return rows


def png(n):
    raw = b"".join(b"\x00" + b"".join(bytes(px) for px in row) for row in _pixels(n))

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", n, n, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def ico(sizes=(16, 32, 48, 256)):
    images = [png(n) for n in sizes]
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    entries = b""
    for n, data in zip(sizes, images):
        entries += struct.pack("<BBBBHHII", n % 256, n % 256, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
    return header + entries + b"".join(images)


_cache = {}


def favicon():
    if "ico" not in _cache:
        _cache["ico"] = ico((16, 32, 48))
    return _cache["ico"]


if __name__ == "__main__":
    import sys

    with open(sys.argv[1] if len(sys.argv) > 1 else "aoiro.ico", "wb") as f:
        f.write(ico())
