from __future__ import annotations

import struct
from pathlib import Path


def image_size(path: str | Path) -> tuple[int, int] | None:
    candidate = Path(path)
    if not candidate.exists() or not candidate.is_file():
        return None
    with candidate.open("rb") as handle:
        signature = handle.read(32)
        if signature.startswith(b"\x89PNG\r\n\x1a\n") and len(signature) >= 24:
            width, height = struct.unpack(">II", signature[16:24])
            return int(width), int(height)
        if signature[:3] == b"GIF" and len(signature) >= 10:
            width, height = struct.unpack("<HH", signature[6:10])
            return int(width), int(height)
        if signature[:2] == b"\xff\xd8":
            handle.seek(2)
            while True:
                marker_prefix = handle.read(1)
                if not marker_prefix:
                    return None
                if marker_prefix != b"\xff":
                    continue
                marker = handle.read(1)
                while marker == b"\xff":
                    marker = handle.read(1)
                if not marker or marker in {b"\xd8", b"\xd9"}:
                    continue
                segment_length_bytes = handle.read(2)
                if len(segment_length_bytes) != 2:
                    return None
                segment_length = struct.unpack(">H", segment_length_bytes)[0]
                if marker in {
                    b"\xc0",
                    b"\xc1",
                    b"\xc2",
                    b"\xc3",
                    b"\xc5",
                    b"\xc6",
                    b"\xc7",
                    b"\xc9",
                    b"\xca",
                    b"\xcb",
                    b"\xcd",
                    b"\xce",
                    b"\xcf",
                }:
                    data = handle.read(5)
                    if len(data) != 5:
                        return None
                    height, width = struct.unpack(">HH", data[1:5])
                    return int(width), int(height)
                handle.seek(max(segment_length - 2, 0), 1)
    return None


def aspect_label(size: tuple[int, int] | None) -> str:
    if not size:
        return ""
    width, height = size
    if width <= 0 or height <= 0:
        return ""
    return f"{width}x{height}"
