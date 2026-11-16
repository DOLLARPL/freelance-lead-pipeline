"""Generuje dwie ikony PWA bez zewnetrznych bibliotek (czysty zlib + struct).

Ciemne tlo, akcentowy krzyz celownika. Uruchamiane raz, wynik lezy w repo.
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

BG = (8, 9, 10)
ACCENT = (94, 106, 210)
FG = (232, 234, 237)


def pixel(x: int, y: int, size: int) -> tuple[int, int, int]:
    cx = cy = size / 2
    r = size * 0.30
    thickness = max(2.0, size * 0.035)
    dist = ((x - cx) ** 2 + (y - cy) ** 2) ** 0.5
    if abs(dist - r) < thickness / 2:
        return ACCENT
    if abs(x - cx) < thickness / 2 and abs(y - cy) < r * 0.75:
        return FG
    if abs(y - cy) < thickness / 2 and abs(x - cx) < r * 0.75:
        return FG
    if dist < thickness / 2:
        return FG
    return BG


def write_png(path: Path, size: int) -> None:
    raw = bytearray()
    for y in range(size):
        raw.append(0)
        for x in range(size):
            raw.extend(pixel(x, y, size))
    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )
    header = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
    png += chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + chunk(b"IEND", b"")
    path.write_bytes(png)


if __name__ == "__main__":
    here = Path(__file__).resolve().parent
    write_png(here / "icon-192.png", 192)
    write_png(here / "icon-512.png", 512)
    print("ok")
