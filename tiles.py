"""Change detection, rectangle merging, JPEG encoding and the frame wire format.

Frames are H x W x 4 uint8 numpy arrays in BGRA (or BGRX) byte order, which is
what both GDI and DXGI desktop duplication hand us on Windows.
"""
from __future__ import annotations

import io
import struct

import numpy as np
from PIL import Image

try:  # libjpeg-turbo bindings; ~2x faster than Pillow and accepts BGRA directly
    import simplejpeg
except ImportError:  # pragma: no cover - optional dependency
    simplejpeg = None

TILE = 64    # change-detection granularity in pixels (multiple of the 16px JPEG MCU)
BAND = 256   # tall rectangles are split into bands so they encode/decode in parallel

FRAME_HEADER = struct.Struct("<BBHI")   # type, flags, rect count, frame id
RECT_HEADER = struct.Struct("<HHHHI")   # x, y, w, h, jpeg byte length
MSG_FRAME = 1
FLAG_KEYFRAME = 1
FLAG_SHARP = 2


def grid_shape(width: int, height: int, tile: int = TILE) -> tuple[int, int]:
    return (-(-height // tile), -(-width // tile))


def diff_mask(prev: np.ndarray | None, cur: np.ndarray, tile: int = TILE) -> np.ndarray:
    """Return a (rows, cols) bool mask of the tiles that differ between two frames."""
    h, w = cur.shape[:2]
    if prev is None or prev.shape != cur.shape:
        return np.ones(grid_shape(w, h, tile), dtype=bool)
    a, b = prev.view(np.uint32).reshape(h, w), cur.view(np.uint32).reshape(h, w)
    # One band of tile rows at a time stays in the CPU cache: ~4x faster than diffing it all at once.
    bands = np.array([np.not_equal(a[y:y + tile], b[y:y + tile]).any(axis=0) for y in range(0, h, tile)])
    return np.logical_or.reduceat(bands, np.arange(0, w, tile), axis=1)


def mask_to_rects(mask: np.ndarray, width: int, height: int, tile: int = TILE,
                  max_rects: int = 24) -> list[tuple[int, int, int, int]]:
    """Merge set tiles into pixel rectangles (x, y, w, h) clipped to the frame.

    Horizontal runs of tiles become rectangles, and identical runs on consecutive
    rows are merged vertically. If that still leaves many small pieces, a single
    bounding box is cheaper than paying the per-JPEG header overhead many times.
    """
    rects: list[list[int]] = []          # [col0, row0, col1, row1], end-exclusive
    open_runs: dict[tuple[int, int], int] = {}
    for r in range(mask.shape[0]):
        row = mask[r]
        if not row.any():
            open_runs = {}
            continue
        edges = np.flatnonzero(np.diff(np.concatenate(([False], row, [False])).astype(np.int8)))
        next_runs = {}
        for c0, c1 in zip(edges[0::2].tolist(), edges[1::2].tolist()):
            i = open_runs.get((c0, c1))
            if i is None:
                i = len(rects)
                rects.append([c0, r, c1, r + 1])
            else:
                rects[i][3] = r + 1
            next_runs[(c0, c1)] = i
        open_runs = next_runs

    if len(rects) > max_rects:
        rs, cs = np.nonzero(mask)
        rects = [[int(cs.min()), int(rs.min()), int(cs.max()) + 1, int(rs.max()) + 1]]

    out = []
    for c0, r0, c1, r1 in rects:
        x, y = c0 * tile, r0 * tile
        out.append((x, y, min(c1 * tile, width) - x, min(r1 * tile, height) - y))
    return out


def split_bands(rects, band: int = BAND):
    """Split tall rectangles into horizontal bands of at most `band` rows."""
    out = []
    for x, y, w, h in rects:
        for yy in range(y, y + h, band):
            out.append((x, yy, w, min(band, y + h - yy)))
    return out


def half_size(img: np.ndarray) -> np.ndarray:
    """Halve width and height by averaging 2x2 pixels (drops an odd last row/column)."""
    h, w = img.shape[0] // 2 * 2, img.shape[1] // 2 * 2
    acc = img[0:h:2, 0:w:2].astype(np.uint16)
    acc += img[1:h:2, 0:w:2]
    acc += img[0:h:2, 1:w:2]
    acc += img[1:h:2, 1:w:2]
    return (acc >> 2).astype(np.uint8)


def encode_jpeg(frame: np.ndarray, rect, quality: int, sharp: bool, half: bool = False) -> bytes:
    """JPEG-encode one rectangle of a BGRA frame.

    `sharp` selects 4:4:4 chroma (crisp coloured text) instead of 4:2:0. `half` encodes it at
    half width and height (a quarter of the data); the iPad stretches it back to `rect`.
    """
    x, y, w, h = rect
    sub = frame[y:y + h, x:x + w]
    sub = np.ascontiguousarray(half_size(sub) if half and w > 1 and h > 1 else sub)
    h, w = sub.shape[:2]
    if simplejpeg is not None:
        return simplejpeg.encode_jpeg(sub, quality=quality, colorspace="BGRA",
                                      colorsubsampling="444" if sharp else "420")
    img = Image.frombuffer("RGB", (w, h), sub, "raw", "BGRX", 0, 1)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality, subsampling=0 if sharp else 2)
    return buf.getvalue()


def pack_frame(frame_id: int, parts, flags: int = 0) -> bytes:
    """Serialize [(rect, jpeg_bytes), ...] into one binary WebSocket message."""
    chunks = [FRAME_HEADER.pack(MSG_FRAME, flags, len(parts), frame_id)]
    for (x, y, w, h), data in parts:
        chunks.append(RECT_HEADER.pack(x, y, w, h, len(data)))
        chunks.append(data)
    return b"".join(chunks)


def unpack_frame(buf: bytes):
    """Inverse of pack_frame (used by tests and tools)."""
    kind, flags, count, frame_id = FRAME_HEADER.unpack_from(buf, 0)
    off = FRAME_HEADER.size
    parts = []
    for _ in range(count):
        x, y, w, h, n = RECT_HEADER.unpack_from(buf, off)
        off += RECT_HEADER.size
        parts.append(((x, y, w, h), bytes(buf[off:off + n])))
        off += n
    return kind, flags, frame_id, parts
