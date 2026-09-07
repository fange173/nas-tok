"""Rasterize NasTok PNG icons from the geometric mark (stdlib only)."""
from __future__ import annotations

import math
import os
import struct
import zlib

OUT = os.path.join(os.path.dirname(__file__), "..", "app", "static")


def write_png(path: str, size: int, rows: list[list[int]]) -> None:
    def chunk(tag: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(tag + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)

    raw = b"".join(b"\x00" + bytes(row) for row in rows)
    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    blob = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )
    with open(path, "wb") as fh:
        fh.write(blob)


def lerp(a: int, b: int, t: float) -> int:
    return int(a + (b - a) * t)


def icon_rows(size: int, *, maskable: bool = False) -> list[list[int]]:
    rrect = size * 0.22
    cx = cy = size / 2
    cr = size * (0.32 if maskable else 0.38)
    p1 = (cx - size * 0.055, cy - size * 0.12)
    p2 = (cx - size * 0.055, cy + size * 0.12)
    p3 = (cx + size * 0.145, cy)

    def in_round_rect(x: int, y: int) -> bool:
        px, py = x + 0.5, y + 0.5
        hw = hh = size / 2 - 0.5
        dx = abs(px - cx) - (hw - rrect)
        dy = abs(py - cy) - (hh - rrect)
        ox, oy = max(dx, 0.0), max(dy, 0.0)
        return math.hypot(ox, oy) + min(max(dx, dy), 0.0) - rrect <= 0

    def in_circle(x: int, y: int) -> bool:
        return (x + 0.5 - cx) ** 2 + (y + 0.5 - cy) ** 2 <= cr * cr

    def in_tri(x: int, y: int) -> bool:
        ax, ay = p1
        bx, by = p2
        cx_, cy_ = p3
        px, py = x + 0.5, y + 0.5
        v0x, v0y = cx_ - ax, cy_ - ay
        v1x, v1y = bx - ax, by - ay
        v2x, v2y = px - ax, py - ay
        dot00 = v0x * v0x + v0y * v0y
        dot01 = v0x * v1x + v0y * v1y
        dot02 = v0x * v2x + v0y * v2y
        dot11 = v1x * v1x + v1y * v1y
        dot12 = v1x * v2x + v1y * v2y
        den = dot00 * dot11 - dot01 * dot01
        if abs(den) < 1e-9:
            return False
        inv = 1 / den
        u = (dot11 * dot02 - dot01 * dot12) * inv
        v = (dot00 * dot12 - dot01 * dot02) * inv
        return u >= 0 and v >= 0 and u + v <= 1

    rows: list[list[int]] = []
    for y in range(size):
        row: list[int] = []
        for x in range(size):
            inside = True if maskable else in_round_rect(x, y)
            if not inside:
                row.extend((0, 0, 0, 0))
                continue
            if in_circle(x, y):
                t = (x + y) / (2 * size)
                if in_tri(x, y):
                    row.extend((255, 255, 255, 255))
                else:
                    row.extend((lerp(99, 139, t), lerp(102, 92, t), lerp(241, 246, t), 255))
            else:
                row.extend((11, 11, 16, 255))
        rows.append(row)
    return rows


def main() -> None:
    os.makedirs(OUT, exist_ok=True)
    specs = [
        ("apple-touch-icon.png", 180, False),
        ("icon-192.png", 192, False),
        ("icon-512.png", 512, False),
        ("icon-512-maskable.png", 512, True),
    ]
    for name, size, mask in specs:
        path = os.path.join(OUT, name)
        write_png(path, size, icon_rows(size, maskable=mask))
        print(name, os.path.getsize(path))


if __name__ == "__main__":
    main()
