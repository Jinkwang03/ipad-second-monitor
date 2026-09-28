"""Frame pipeline tests: diffing, rectangle merging, wire format and full reconstruction.

Run from the project folder:  python -m unittest discover -s tests -v
"""
import argparse
import asyncio
import io
import sys
import time
import unittest
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import capture  # noqa: E402
import server  # noqa: E402
import tiles  # noqa: E402


def random_frame(h, w, seed=0):
    """Random 8x8 colour blocks: every tile differs, yet it compresses like real content."""
    rng = np.random.default_rng(seed)
    blocks = rng.integers(0, 256, (h // 8 + 1, w // 8 + 1, 3), dtype=np.uint8)
    frame = np.zeros((h, w, 4), np.uint8)
    frame[..., :3] = blocks.repeat(8, axis=0).repeat(8, axis=1)[:h, :w]
    return frame


def covered(rects, h, w):
    m = np.zeros((h, w), bool)
    for x, y, rw, rh in rects:
        assert rw > 0 and rh > 0 and x + rw <= w and y + rh <= h, (x, y, rw, rh)
        m[y:y + rh, x:x + rw] = True
    return m


# A faithful reconstruction scores well above this; a tile drawn in the wrong place ~10 dB.
MIN_PSNR = 30


def psnr(a, b):
    mse = np.mean((a.astype(float) - b.astype(float)) ** 2)
    return float("inf") if mse == 0 else 10 * np.log10(255 ** 2 / mse)


class TileTests(unittest.TestCase):
    def test_identical_frames_have_no_changes(self):
        f = random_frame(130, 200)
        self.assertFalse(tiles.diff_mask(f, f.copy()).any())

    def test_single_pixel_change_marks_its_tile(self):
        f = random_frame(130, 200)          # 3 x 4 tiles, last row/column partial
        g = f.copy()
        g[129, 199, 1] ^= 0xFF
        mask = tiles.diff_mask(f, g)
        self.assertEqual(mask.shape, (3, 4))
        self.assertEqual(list(zip(*np.nonzero(mask))), [(2, 3)])

    def test_first_frame_is_all_dirty(self):
        self.assertTrue(tiles.diff_mask(None, random_frame(100, 100)).all())

    def test_rects_cover_exactly_the_changed_tiles(self):
        rng = np.random.default_rng(1)
        h, w = 700, 1000
        for _ in range(50):
            mask = rng.random(tiles.grid_shape(w, h)) < 0.2
            rects = tiles.mask_to_rects(mask, w, h, max_rects=10_000)
            want = np.kron(mask, np.ones((tiles.TILE, tiles.TILE), bool))[:h, :w]
            np.testing.assert_array_equal(covered(rects, h, w), want)

    def test_many_pieces_collapse_to_bounding_box(self):
        h, w = 640, 640
        mask = np.zeros(tiles.grid_shape(w, h), bool)
        mask[::2, ::2] = True
        rects = tiles.mask_to_rects(mask, w, h, max_rects=4)
        self.assertEqual(rects, [(0, 0, 576, 576)])

    def test_bands_split_tall_rects(self):
        bands = tiles.split_bands([(64, 0, 128, 600)])
        self.assertEqual(bands, [(64, 0, 128, 256), (64, 256, 128, 256), (64, 512, 128, 88)])

    def test_pack_roundtrip(self):
        parts = [((0, 0, 64, 64), b"abc"), ((64, 128, 10, 20), b"")]
        kind, flags, frame_id, out = tiles.unpack_frame(tiles.pack_frame(7, parts, 3))
        self.assertEqual((kind, flags, frame_id, out), (1, 3, 7, parts))

    def test_jpeg_colors_are_right(self):
        f = np.zeros((64, 64, 4), np.uint8)
        f[..., 0] = 200   # blue in BGRA
        data = tiles.encode_jpeg(f, (0, 0, 64, 64), 90, True)
        rgb = np.asarray(Image.open(io.BytesIO(data)).convert("RGB"))
        self.assertLess(np.abs(rgb[32, 32].astype(int) - [0, 0, 200]).max(), 4)


class FakeWebSocket:
    closed = False


def make_hub(w=640, h=400):
    args = argparse.Namespace(monitor="test", test_size=(w, h), view_only=True, quality=90,
                              motion_quality=60, fps=60, capture="auto")
    hub = server.Hub(args, key="")
    hub._set_target(capture.find_target("test", (w, h)), "test")
    return hub


class PipelineTests(unittest.TestCase):
    """Drive Hub.take/encode like a session would and rebuild the picture from the tiles."""

    def setUp(self):
        self.hub = make_hub()
        self.session = server.Session(self.hub, FakeWebSocket(), "test")
        self.hub.sessions.add(self.session)
        self.canvas = None

    def pump(self):
        job = self.hub.take(self.session)
        if job is None:
            return None
        parts = asyncio.run(self.hub.encode(job))
        if self.canvas is None:
            w, h = job.target.rect[2:]
            self.canvas = np.zeros((h, w, 3), np.uint8)
        for (x, y, w, h), data in parts:
            img = np.asarray(Image.open(io.BytesIO(data)).convert("RGB"))
            self.assertEqual(img.shape[:2], (h, w))
            self.canvas[y:y + h, x:x + w] = img
        return job

    def show(self, frame, prev):
        self.hub._publish(frame, tiles.diff_mask(prev, frame), now=time.monotonic())

    def test_reconstruction_motion_then_refinement(self):
        h, w = 400, 640
        base = random_frame(h, w, seed=2)
        self.show(base, None)
        job = self.pump()
        self.assertTrue(job.keyframe and job.sharp and job.new_size)
        self.assertGreater(psnr(self.canvas, base[..., 2::-1]), MIN_PSNR)

        # Small change: sent sharp immediately.
        small = base.copy()
        small[10:20, 10:20, :3] = 255
        self.show(small, base)
        job = self.pump()
        self.assertTrue(job.sharp and not job.keyframe)
        self.assertEqual(int(job.mask.sum()), 1)

        # Big motion: sent at motion quality and remembered for refinement.
        prev = small
        for i in range(5):
            frame = random_frame(h, w, seed=10 + i)
            self.show(frame, prev)
            prev = frame
            job = self.pump()
            self.assertFalse(job.sharp)
        self.assertTrue(self.session.lowq.all())
        self.assertIsNone(self.pump())               # nothing new, not still long enough yet
        motion_psnr = psnr(self.canvas, prev[..., 2::-1])
        self.assertGreater(motion_psnr, 20)          # q60 4:2:0 on saturated random colour ~23 dB

        self.hub.last_change -= 10                   # pretend the screen has been still for a while
        job = self.pump()
        self.assertTrue(job.refine.all() and not job.mask.any())
        self.assertFalse(self.session.lowq.any())
        refined_psnr = psnr(self.canvas, prev[..., 2::-1])
        self.assertGreater(refined_psnr, 45)         # q90 4:4:4 ~53 dB
        self.assertIsNone(self.pump())

    def test_display_change_forces_keyframe(self):
        base = random_frame(400, 640, seed=3)
        self.show(base, None)
        self.pump()
        self.hub._set_target(capture.find_target("test", (320, 200)), "test")
        self.assertIsNone(self.pump())               # no frame for the new size yet
        small = random_frame(200, 320, seed=4)
        self.show(small, None)
        job = self.pump()
        self.assertTrue(job.keyframe and job.new_size)
        self.assertEqual(job.target.rect, (0, 0, 320, 200))

    def test_to_screen_clamps(self):
        self.assertEqual(self.hub.to_screen(0, 0), (0, 0))
        self.assertEqual(self.hub.to_screen(1, 1), (639, 399))
        self.assertEqual(self.hub.to_screen(-3, 7), (0, 399))


if __name__ == "__main__":
    unittest.main()
