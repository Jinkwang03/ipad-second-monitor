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


class MotionModeTests(unittest.TestCase):
    """Moving content goes light (the least delay) unless full size clearly costs no delay."""
    PX = 2266 * 1488                               # judged for a whole-screen update

    def desired(self, rate, delay=None, rtt=0.0, base_rtt=None):
        return server.desired_motion_mode(self.PX, rate, 0.2, delay, rtt, base_rtt)

    def test_light_unless_the_connection_is_clearly_fast(self):
        self.assertEqual(self.desired(None), "light")              # nothing measured yet
        self.assertEqual(self.desired(15e6), "light")              # ordinary Wi-Fi: 674 KB in ~45 ms
        self.assertEqual(self.desired(40e6), "normal")             # fast: ~17 ms
        self.assertEqual(self.desired(40e6, delay=0.030, rtt=0.004, base_rtt=0.003), "normal")

    def test_late_updates_or_a_rising_ping_mean_light(self):
        self.assertEqual(self.desired(40e6, delay=0.060), "light")                # arriving late
        self.assertEqual(self.desired(40e6, rtt=0.030, base_rtt=0.004), "light")  # Wi-Fi queueing

    def test_starts_light_goes_normal_slowly_and_back_quickly(self):
        auto = server.MotionMode()
        self.assertEqual(auto.mode, "light")
        self.assertEqual(auto.update("normal", 0.0), "light")
        self.assertEqual(auto.update("normal", 4.9), "light")      # not yet 5 s of keeping up
        self.assertEqual(auto.update("normal", 5.1), "normal")
        self.assertEqual(auto.update("light", 6.0), "normal")
        self.assertEqual(auto.update("light", 6.35), "light")      # struggling for 0.3 s: light

    def test_waits_longer_after_full_size_failed_again(self):
        auto = server.MotionMode()
        auto.update("normal", 0.0); auto.update("normal", 5.1)     # normal at 5.1 s
        auto.update("light", 6.0); auto.update("light", 6.4)       # ...but it failed right away
        auto.update("normal", 7.0)
        self.assertEqual(auto.update("normal", 12.1), "light")     # 5 s is no longer enough
        self.assertEqual(auto.update("normal", 17.1), "normal")    # it now waits 10 s

    def test_never_flip_flops(self):
        auto = server.MotionMode()
        seen = {auto.update("normal" if i % 2 else "light", i * 0.05) for i in range(400)}
        self.assertEqual(seen, {"light"})                           # a wobbly link never switches


class NeighborhoodTests(unittest.TestCase):
    def test_neighborhood_min(self):
        a = np.full((5, 5), 9.0)
        a[2, 2] = 1.0
        out = tiles.neighborhood_min(a, 1)
        self.assertEqual(out[1:4, 1:4].tolist(), [[1.0] * 3] * 3)   # the 3x3 around it
        self.assertEqual(out[0, 0], 9.0)


class PipelineTests(unittest.TestCase):
    """Drive Hub.take/encode like a session would and rebuild the picture from the tiles."""

    def setUp(self):
        self.hub = make_hub()
        self.session = server.Session(self.hub, FakeWebSocket(), "test")
        self.hub.sessions.add(self.session)
        self.canvas = None
        self.half_sized = 0

    def pump(self, allow_half=True):
        """Take and encode the next update like a session would, as a page that can (or can't)
        stretch half-size updates, and draw it onto self.canvas."""
        job = self.hub.take(self.session)
        if job is None:
            return None
        parts, _ = asyncio.run(self.hub.encode(job, half=allow_half))
        if self.canvas is None:
            w, h = job.target.rect[2:]
            self.canvas = np.zeros((h, w, 3), np.uint8)
        for (x, y, w, h), data in parts:
            img = Image.open(io.BytesIO(data)).convert("RGB")
            if img.size != (w, h):              # big moving areas come at half size; stretch like the iPad
                self.assertEqual(img.size, (w // 2, h // 2))
                self.half_sized += 1
                img = img.resize((w, h), Image.BILINEAR)
            self.canvas[y:y + h, x:x + w] = np.asarray(img)
        return job

    def show(self, frame, prev):
        self.hub._publish(frame, tiles.diff_mask(prev, frame), now=time.monotonic())

    def wait(self, seconds=10):
        """Pretend the screen has been still for a while."""
        self.hub.last_change -= seconds

    def test_reconstruction_motion_then_refinement(self):
        h, w = 400, 640
        base = random_frame(h, w, seed=2)
        self.show(base, None)
        job = self.pump()
        self.assertTrue(job.keyframe and job.sharp and job.new_size)
        self.assertGreater(psnr(self.canvas, base[..., 2::-1]), MIN_PSNR)
        self.wait()

        # Small change after a pause: sent sharp immediately.
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
        self.assertGreater(self.half_sized, 0)       # whole-screen motion went at half size
        self.assertIsNone(self.pump())               # nothing new, not still long enough yet
        motion_psnr = psnr(self.canvas, prev[..., 2::-1])
        self.assertGreater(motion_psnr, 12)          # blurry while moving (half size, q60)...

        self.hub.last_change -= 10                   # pretend the screen has been still for a while
        refined = np.zeros_like(self.session.lowq)
        updates = 0
        while (job := self.pump()) is not None:      # sharpened a few tiles per update, so a
            self.assertFalse(job.mask.any())         # movement starting again isn't held up
            self.assertLessEqual(int(job.refine.sum()), server.REFINE_TILES)
            refined |= job.refine
            updates += 1
        self.assertTrue(refined.all() and updates == 2)   # 70 tiles: 64 + 6
        self.assertFalse(self.session.lowq.any())
        refined_psnr = psnr(self.canvas, prev[..., 2::-1])
        self.assertGreater(refined_psnr, 45)         # ...then sharp once still (q90 4:4:4 ~53 dB)
        self.assertIsNone(self.pump())

    def test_sharpening_next_to_movement_is_kept_small(self):
        prev = random_frame(400, 640, seed=3)
        self.show(prev, None)
        self.pump()
        for i in range(3):                            # the whole screen moves (all 70 tiles soft)
            frame = random_frame(400, 640, seed=30 + i)
            self.show(frame, prev)
            prev = frame
            self.pump()
        self.wait()                                   # it stops...
        frame = prev.copy()                           # ...and a small area starts moving again
        frame[0:64, 0:64, :3] = np.random.default_rng(1).integers(0, 256, (64, 64, 3))
        for i in range(8):
            frame = frame.copy()
            frame[0:64, 0:64, :3] = np.random.default_rng(2 + i).integers(0, 256, (64, 64, 3))
            self.show(frame, prev)
            prev = frame
            job = self.pump()
        self.assertTrue(job.moving[0, 0])
        self.assertLessEqual(int(job.refine.sum()), server.REFINE_TILES_MOVING)

    def test_a_playing_video_keeps_one_quality(self):
        # A video around 12% of the screen used to flip between sharp and soft every few frames,
        # because each update was judged by its total size. Now tiles that keep changing stay
        # at moving quality, and a change elsewhere after a pause (typing) is still sharp.
        base = random_frame(400, 640, seed=7)        # 7 x 10 tiles
        self.show(base, None)
        self.pump()
        self.wait()
        prev = base
        for i in range(12):
            frame = prev.copy()
            rows = 3 if i % 2 else 2                 # 12 or 8 of 70 tiles: either side of 12%
            frame[64:64 + 64 * rows, 128:384, :3] = np.random.default_rng(i).integers(0, 256, (64 * rows, 256, 3))
            if i == 11:
                frame[320:330, 5:15, :3] = 255       # someone types in a corner that was still
            self.show(frame, prev)
            prev = frame
            job = self.pump()
            if i >= 7:                               # once it's clearly a video, always moving quality
                self.assertTrue(job.moving[1:3, 2:6].all(), f"video sent sharp at frame {i}")
        self.assertTrue(job.mask[5, 0] and not job.moving[5, 0])   # the typing went sharp

    def test_a_video_pausing_for_a_moment_does_not_flash_sharp(self):
        base = random_frame(400, 640, seed=10)
        self.show(base, None)
        self.pump()
        self.wait()
        prev = base
        for i in range(60):                          # a video plays for 2 s (30 frames a second)...
            self.hub.last_change -= 1 / 30
            frame = prev.copy()
            frame[64:256, 128:384, :3] = np.random.default_rng(60 + i).integers(0, 256, (192, 256, 3))
            self.show(frame, prev)
            prev = frame
            self.pump()
        self.hub.last_change -= 0.3                  # ...then holds still for 0.3 s:
        job = self.pump()
        self.assertTrue(job is None or not job.refine.any())   # no sharp flash
        self.hub.last_change -= 1.0                  # really paused (over a second):
        self.assertTrue(self.pump().refine[1:4, 2:6].all())    # now it sharpens

    def test_typing_stays_sharp(self):
        base = random_frame(400, 640, seed=9)
        self.show(base, None)
        self.pump()
        self.wait()
        prev = base
        for i in range(10):                          # ten keystrokes into the same tile, 0.2 s apart
            self.hub.last_change -= 0.2
            frame = prev.copy()
            frame[70:80, 70 + i * 5:74 + i * 5, :3] = 255
            self.show(frame, prev)
            prev = frame
            job = self.pump()
            self.assertTrue(job.sharp, f"keystroke {i} was sent soft")

    def test_calm_parts_of_a_playing_video_are_not_sharpened_one_by_one(self):
        # In light mode, calm patches of a video that got sharpened on their own made it blink.
        base = random_frame(400, 640, seed=8)
        self.show(base, None)
        self.pump()
        self.wait()
        prev = base
        for i in range(10):                          # the video area plays for a moment...
            frame = prev.copy()
            frame[64:256, 128:384, :3] = np.random.default_rng(40 + i).integers(0, 256, (192, 256, 3))
            self.show(frame, prev)
            prev = frame
            self.pump()
        self.hub.last_change[1:4, 2:4] -= 10         # ...its left half pauses for a while,
        frame = prev.copy()                           # while its right half keeps moving
        frame[64:256, 256:384, :3] = np.random.default_rng(99).integers(0, 256, (192, 128, 3))
        self.show(frame, prev)
        job = self.pump()
        self.assertFalse(job.refine[1:4, 2:4].any())   # the paused half is not sharpened yet
        self.wait()                                    # the whole video pauses:
        self.assertTrue(self.pump().refine[1:4, 2:6].all())   # now it all sharpens together

    def test_old_pages_never_get_half_size_updates(self):
        # A page that doesn't stretch half-size updates would draw them in a corner (broken look).
        prev = random_frame(400, 640, seed=5)
        self.show(prev, None)
        self.pump(allow_half=False)
        for i in range(3):
            frame = random_frame(400, 640, seed=20 + i)
            self.show(frame, prev)
            prev = frame
            job = self.pump(allow_half=False)
            self.assertFalse(job.sharp)          # big motion...
        self.assertEqual(self.half_sized, 0)     # ...but always full size for this page
        self.assertGreater(psnr(self.canvas, prev[..., 2::-1]), 20)

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
