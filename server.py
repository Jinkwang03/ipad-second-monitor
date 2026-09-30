#!/usr/bin/env python3
"""iPad Display: use an iPad as an extra monitor for a Windows PC.

Run this on the PC, then open the printed address in Safari on the iPad. The
selected display is streamed to the iPad as JPEG tiles over a WebSocket, and the
iPad's touch / Apple Pencil / trackpad / keyboard input is injected back into
Windows.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import io
import itertools
import json
import logging
import os
import re
import secrets
import signal
import socket
import subprocess
import sys
import urllib.request
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

import numpy as np
from aiohttp import WSMsgType, web
from PIL import Image, ImageDraw

import capture
import hotspot
import mdns
import tiles

IS_WINDOWS = sys.platform == "win32"
if IS_WINDOWS:
    import win32

log = logging.getLogger("ipad-display")
VERSION = "1.9.1"
FROZEN = getattr(sys, "frozen", False)   # running as the packaged iPadDisplay.exe
WEB_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)) / "web"
KEY_FILE = Path.home() / ".ipad-display-key"
KEY_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"
ALL_INTERFACES = ("0.0.0.0", "::", "")
NETWORK_CHECK = 5.0     # seconds between checks for Wi-Fi / hotspot changes
DEFAULT_SAVE_DIR = Path.home() / "Downloads" / "iPad Display"
IMAGE_TYPES = {".jpg", ".jpeg", ".png", ".gif", ".heic", ".heif", ".webp", ".bmp", ".tif", ".tiff"}
RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}

MAX_INFLIGHT = 2        # frames sent but not yet drawn by the iPad; keeps latency low on slow Wi-Fi
REFINE_DELAY = 0.2      # a region must be still this long before it is re-sent sharp
REFINE_TILES = 64       # sharpen at most this many tiles per update (whole screen: a few updates),
REFINE_TILES_MOVING = 16  # and fewer next to moving content, so sharpening never holds movement up
SHARP_FRACTION = 0.12   # a sudden change bigger than this share of the screen is sent as moving first
ACTIVITY_TAU = 0.5      # seconds over which a tile's "how often does it change" estimate fades
BUSY_ACTIVITY = 6.0     # a tile (or a neighbour) changing ~12+ times a second is moving: video,
                        # scrolling. Typing, even fast, and a blinking caret stay below that.
QUIET_RADIUS = 2        # sharpen a tile only once the tiles this close to it have stopped changing too
NORMAL_BUDGET = 0.025   # full size only if a whole-screen update would reach the iPad this fast,
NORMAL_MAX_DELAY = 0.040  # moving updates currently arrive (PC screen -> iPad) within this,
PING_LIMIT = 0.020      # and the ping stays low (under this, and near what this Wi-Fi does when idle)
MOTION_DOWN_HOLD = 0.3  # moving content goes light after the connection has struggled for this long...
MOTION_UP_HOLD = 5.0    # ...and back to full size only after it has kept up for this long
MOTION_UP_HOLD_MAX = 60.0  # (longer each time full size had to be given up again soon after)
MOTION_MODES = ("normal", "light")   # full size, or half size (a quarter of the data)
MONITOR_CHECK = 2.0     # seconds between checks for display changes
CURSOR_HZ = 120


def desired_motion_mode(screen_px: int, link_rate: float | None, bytes_per_px: float,
                        delay: float | None = None, rtt: float = 0.0, base_rtt: float | None = None) -> str:
    """Light (half size: the least delay and ping) unless full size ("normal") clearly costs no
    noticeable delay on this connection. Judged for a whole-screen update, not the current one,
    so updates of different sizes can't make the choice flip back and forth."""
    if link_rate is None:
        return "light"                          # nothing measured yet: start with the least delay
    if delay is not None and delay > NORMAL_MAX_DELAY:
        return "light"                          # moving updates already take too long
    if base_rtt is not None and rtt > max(PING_LIMIT, 2 * base_rtt):
        return "light"                          # the Wi-Fi is queueing up
    return "normal" if screen_px * bytes_per_px / link_rate <= NORMAL_BUDGET else "light"


def first_tiles(mask: np.ndarray, limit: int) -> np.ndarray:
    """Keep only the first `limit` set tiles (top to bottom); the rest wait for a later update."""
    if mask.sum() <= limit:
        return mask
    kept = np.zeros(mask.size, bool)
    kept[np.flatnonzero(mask)[:limit]] = True
    return kept.reshape(mask.shape)


class MotionMode:
    """Chooses how moving content is sent, keeping it steady: it starts light, goes to full size
    only after the connection has shown for a while that it can take it (longer each time full
    size had to be given up again soon after), and back to light soon after it struggles."""

    def __init__(self):
        self.mode = "light"
        self.wanted: str | None = None
        self.wanted_since = 0.0
        self.up_hold = MOTION_UP_HOLD
        self.went_up_at = float("-inf")

    def update(self, desired: str, now: float) -> str:
        if desired == self.mode:
            self.wanted = None
            if self.mode == "normal" and now - self.went_up_at > 60:
                self.up_hold = MOTION_UP_HOLD   # full size has held up for a minute: trust it again
            return self.mode
        if desired != self.wanted:
            self.wanted, self.wanted_since = desired, now
        lighter = desired == "light"
        if now - self.wanted_since >= (MOTION_DOWN_HOLD if lighter else self.up_hold):
            if lighter and now - self.went_up_at < 20:
                self.up_hold = min(self.up_hold * 2, MOTION_UP_HOLD_MAX)
            if not lighter:
                self.went_up_at = now
            self.mode, self.wanted = desired, None
        return self.mode


@dataclass
class Job:
    frame: np.ndarray
    mask: np.ndarray
    refine: np.ndarray
    sharp: bool
    keyframe: bool
    new_size: bool
    target: capture.Target
    shown_at: float = 0.0   # perf_counter time the PC drew this frame
    moving: np.ndarray | None = None   # changed tiles to send at moving quality (the rest go sharp)


class Hub:
    """Shared state: the capture thread publishes frames, sessions consume them."""

    def __init__(self, args, key: str):
        self.args = args
        self.key = key
        self.lock = threading.Lock()
        self.sessions: set[Session] = set()
        self.loop: asyncio.AbstractEventLoop | None = None
        self.target: capture.Target | None = None
        self.capture_name = ""
        self.frame: np.ndarray | None = None
        self.frame_shown_at = 0.0                     # perf_counter time the PC drew self.frame
        self.last_change: np.ndarray | None = None   # per tile, monotonic time of last change
        self.generation = 0                           # bumped whenever the streamed display changes
        self.wake_capture = threading.Event()
        self.stopping = threading.Event()
        self.pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="jpeg")
        self.injector = None
        if IS_WINDOWS and not args.view_only and args.monitor != "test":
            self.injector = win32.InputInjector()
        self.cursor_msg: dict | None = None
        self.shapes: dict[int, dict] = {}
        self.mirror_hint = ""                         # why we mirror instead of extending, if we do
        self.mdns: mdns.MdnsResponder | None = None   # answers <name>.local on the local network
        self.offered: dict[str, str] = {}             # id -> path of files copied on the PC, for the iPad
        self.drop_box = None                          # "drop files here" box on the iPad's screen
        self.hotspot_started = False                  # we turned on the laptop's Wi-Fi hotspot (turn it off at exit)
        self.hotspot_tried = False

    def key_ok(self, value: str) -> bool:
        return not self.key or secrets.compare_digest(value.encode(), self.key.encode())

    # ---- PC -> iPad files (event loop, except drop_files) -----------------

    def make_offer(self, paths) -> dict:
        """Make these files downloadable by the iPad (under random ids) and describe them."""
        files = [p for p in paths if os.path.isfile(p)]
        self.offered = {secrets.token_urlsafe(9): p for p in files}
        return {"files": [{"id": fid, "name": os.path.basename(p), "size": os.path.getsize(p),
                           "image": os.path.splitext(p)[1].lower() in IMAGE_TYPES}
                          for fid, p in self.offered.items()],
                "folders": len(paths) - len(files)}

    def offer_files(self, paths) -> None:
        msg = {"t": "offer", **self.make_offer(paths)}
        for s in list(self.sessions):
            asyncio.ensure_future(s.send_json(msg))
        log.info("Sent %d file(s) from the PC to the iPad", len(msg["files"]))

    def drop_files(self, paths) -> str:
        """Files dropped on the drop box (window thread); returns what the box should say."""
        files = [p for p in paths if os.path.isfile(p)]
        if not files:
            return "Folders can't be sent - drop files"
        if not self.sessions:
            return "Connect the iPad first"
        self._call(self.offer_files, files)
        return f"Sent {len(files)} file{'s' if len(files) != 1 else ''} to the iPad"

    def drop_box_rect(self):
        """Where the drop box sits: on the streamed display, and only while an iPad is connected."""
        target = self.target
        return target.rect if target is not None and self.sessions else None

    # ---- sessions -------------------------------------------------------

    def add(self, session: Session) -> None:
        with self.lock:
            self.sessions.add(session)
        self.wake_capture.set()
        if self.cursor_msg:
            session.queue_cursor(self.cursor_msg)

    def remove(self, session: Session) -> None:
        with self.lock:
            self.sessions.discard(session)
        if self.injector:
            self.injector.release(session.id)

    def _call(self, fn, *args) -> None:
        """Run fn on the event loop from another thread."""
        loop = self.loop
        if loop is not None:
            try:
                loop.call_soon_threadsafe(fn, *args)
            except RuntimeError:  # loop already closed during shutdown
                pass

    def _wake_sessions(self) -> None:
        for s in list(self.sessions):
            s.wake.set()

    # ---- capture thread ---------------------------------------------------

    def capture_loop(self) -> None:
        source = None
        prev = None
        next_check = 0.0
        idle_since = None
        period = 1.0 / self.args.fps
        while not self.stopping.is_set():
            if not self.sessions:
                idle_since = idle_since or time.monotonic()
                if source is not None and time.monotonic() - idle_since > 30:
                    source.close()
                    source = prev = None
                    with self.lock:
                        self.frame = None
                self.wake_capture.wait(1.0)
                self.wake_capture.clear()
                continue
            idle_since = None
            now = time.monotonic()
            try:
                if source is None or now >= next_check:
                    next_check = now + MONITOR_CHECK
                    target = capture.find_target(self.args.monitor, self.args.test_size)
                    if source is None or target != self.target:
                        if source is not None:
                            source.close()
                            source = None
                        source = capture.open_source(target, self.args.capture)
                        prev = None
                        self._set_target(target, source.name)
                img = source.grab()
            except Exception as exc:
                log.warning("Capture problem: %s (retrying)", exc)
                if source is not None:
                    try:
                        source.close()
                    except Exception:
                        pass
                source = prev = None
                time.sleep(1.0)
                continue
            if img is None and getattr(source, "waits", False):
                continue                       # grab() already waited; wait again for the next change
            if img is not None:
                mask = tiles.diff_mask(prev, img)
                prev = img
                if mask.any():
                    self._publish(img, mask, now, getattr(source, "shown_at", time.perf_counter()))
            delay = period - (time.monotonic() - now)
            if delay > 0:
                time.sleep(delay)
        if source is not None:
            source.close()

    def _set_target(self, target: capture.Target, source_name: str) -> None:
        announce = self.target is None or self.target.rect != target.rect or self.target.device != target.device
        hint = capture.mirror_hint() if target.mirroring_primary else ""
        with self.lock:
            self.mirror_hint = hint
            self.target = target
            self.capture_name = source_name
            self.generation += 1
            self.frame = None
            self.last_change = np.zeros(tiles.grid_shape(*target.rect[2:]))
            self.activity = np.zeros(tiles.grid_shape(*target.rect[2:]))   # decayed count of changes
        if announce:
            log.info("Streaming %s (capture: %s)", target.label, source_name)
            if hint:
                log.info("No second display, so the iPad MIRRORS the main screen. To extend: %s", hint)
        self._call(self._wake_sessions)

    def _publish(self, img: np.ndarray, mask: np.ndarray, now: float, shown_at: float = 0.0) -> None:
        with self.lock:
            if self.last_change is None or self.last_change.shape != mask.shape:
                return
            self.frame = img
            self.frame_shown_at = shown_at or time.perf_counter()
            since = now - self.last_change[mask]
            self.activity[mask] = self.activity[mask] * np.exp(-since / ACTIVITY_TAU) + 1.0
            self.last_change[mask] = now
            for s in self.sessions:
                if s.dirty is not None and s.dirty.shape == mask.shape:
                    s.dirty |= mask
        self._call(self._wake_sessions)

    # ---- per-session frame preparation (event loop) ----------------------

    def take(self, s: Session) -> Job | None:
        """Grab what this session needs next: changed tiles plus tiles due for sharpening."""
        now = time.monotonic()
        new_size = False
        with self.lock:
            if self.frame is None:
                return None
            grid = self.last_change.shape
            keyframe = s.generation != self.generation or s.want_keyframe
            if keyframe:
                new_size = s.generation != self.generation
                s.generation = self.generation
                s.want_keyframe = False
                s.lowq = np.zeros(grid, bool)
                mask = np.ones(grid, bool)
            else:
                mask = s.dirty
            s.dirty = np.zeros(grid, bool)
            still_for = now - self.last_change
            activity = self.activity * np.exp(-still_for / ACTIVITY_TAU)
            # Sharpen a tile only once it and everything around it has been still, and has stopped
            # changing video-fast: calm patches of a playing video, or a video pausing for a
            # moment, would otherwise flash sharp and go soft again (blinking).
            quiet = ((tiles.neighborhood_min(still_for, QUIET_RADIUS) > REFINE_DELAY)
                     & (tiles.neighborhood_max(activity, QUIET_RADIUS) < BUSY_ACTIVITY))
            refine = s.lowq & ~mask & quiet
            busy = tiles.neighborhood_max(activity, 1) >= BUSY_ACTIVITY
            frame, target, shown_at = self.frame, self.target, self.frame_shown_at
        if not mask.any() and not refine.any():
            return None
        # Decide per tile, not per update: tiles in an area that changes many times a second
        # (a playing video, a scroll) always go at moving quality, so nothing flickers between
        # sharp and soft. Other changes (typing, a click) go sharp, unless it's a big sudden
        # one (a new page), which goes as moving first.
        if keyframe:
            moving = np.zeros(grid, bool)
        else:
            moving = mask & busy
            calm = mask & ~moving
            if calm.mean() > SHARP_FRACTION:
                moving |= calm
        refine = first_tiles(refine, REFINE_TILES_MOVING if moving.any() else REFINE_TILES)
        s.lowq[mask] = moving[mask]
        s.lowq[refine] = False
        return Job(frame, mask, refine, not moving.any(), keyframe, new_size, target, shown_at, moving)

    async def encode(self, job: Job, motion_quality: int | None = None, half: bool = False):
        """JPEG-encode a job; returns ([(rect, jpeg), ...], bytes of the changed (non-refine) part).

        Moving content uses `motion_quality`, and `half` sends it at half size (a quarter of the
        data; the page stretches it back). Either way it is re-sent sharp once it stops moving.
        """
        w, h = job.target.rect[2:]
        q_sharp = self.args.quality
        q_motion = motion_quality or self.args.motion_quality
        moving = job.moving if job.moving is not None else np.zeros_like(job.mask)
        main = [(r, q_motion, False, half) for r in tiles.split_bands(tiles.mask_to_rects(moving, w, h))]
        still = (job.mask & ~moving) | job.refine        # calm changes and sharpening, both at full quality
        work = main + [(r, q_sharp, True, False) for r in tiles.split_bands(tiles.mask_to_rects(still, w, h))]
        loop = asyncio.get_running_loop()
        datas = await asyncio.gather(*(loop.run_in_executor(self.pool, tiles.encode_jpeg, job.frame, *item)
                                       for item in work))
        return [(item[0], data) for item, data in zip(work, datas)], sum(len(d) for d in datas[:len(main)])

    def size_message(self, target: capture.Target) -> dict:
        w, h = target.rect[2:]
        return {"t": "size", "w": w, "h": h, "label": target.label, "mirror": target.mirroring_primary,
                "hint": self.mirror_hint if target.mirroring_primary else "",
                "scale": round(target.dpi * 100 / 96), "capture": self.capture_name}

    def to_screen(self, nx, ny):
        """Map normalised (0..1) iPad coordinates onto the streamed display."""
        target = self.target
        if target is None:
            return None
        left, top, w, h = target.rect
        nx = min(max(float(nx), 0.0), 1.0)
        ny = min(max(float(ny), 0.0), 1.0)
        return left + round(nx * (w - 1)), top + round(ny * (h - 1))

    # ---- cursor thread ------------------------------------------------------

    def cursor_loop(self) -> None:
        cache: dict[tuple, int | None] = {}
        ids = itertools.count(1)
        last = None
        system_dpi = win32.system_dpi()
        while not self.stopping.is_set():
            time.sleep(1 / CURSOR_HZ if self.sessions else 0.25)
            target = self.target
            info = win32.get_cursor() if target is not None else None
            if info is None:
                continue
            x, y, showing, hcursor = info
            left, top, w, h = target.rect
            rx, ry = x - left, y - top
            visible = showing and 0 <= rx < w and 0 <= ry < h
            shape = None
            if visible and hcursor:
                key = (hcursor, target.dpi)
                if key not in cache:
                    if len(cache) > 64:
                        cache.clear()
                    cache[key] = self._make_shape(hcursor, next(ids), target.dpi / system_dpi)
                shape = cache[key]
            state = (rx, ry, visible, shape)
            if state != last:
                last = state
                msg = {"t": "c", "x": rx, "y": ry, "v": int(visible)}
                if shape:
                    msg["s"] = shape
                self._call(self._broadcast_cursor, msg)

    def _make_shape(self, hcursor, shape_id: int, scale: float) -> int | None:
        try:
            result = win32.cursor_image(hcursor)
        except Exception as exc:
            log.debug("cursor image failed: %s", exc)
            return None
        if result is None:
            return None
        rgba, hx, hy, inverted = result
        buf = io.BytesIO()
        Image.fromarray(rgba, "RGBA").save(buf, "PNG")
        self.shapes[shape_id] = {"t": "cs", "id": shape_id, "w": rgba.shape[1], "h": rgba.shape[0],
                                 "hx": hx, "hy": hy, "k": round(scale, 3), "inv": inverted,
                                 "png": base64.b64encode(buf.getvalue()).decode()}
        return shape_id

    async def fake_cursor(self) -> None:
        """Test-pattern mode: a cursor circling the screen, to exercise the overlay."""
        img = Image.new("RGBA", (20, 30))
        ImageDraw.Draw(img).polygon([(1, 1), (1, 23), (7, 18), (11, 27), (15, 25), (11, 16), (18, 16)],
                                    fill=(255, 255, 255, 255), outline=(0, 0, 0, 255))
        buf = io.BytesIO()
        img.save(buf, "PNG")
        self.shapes[1] = {"t": "cs", "id": 1, "w": 20, "h": 30, "hx": 1, "hy": 1, "k": 1, "inv": False,
                          "png": base64.b64encode(buf.getvalue()).decode()}
        t0 = time.monotonic()
        while True:
            await asyncio.sleep(1 / 60)
            target = self.target
            if target is None or not self.sessions:
                continue
            w, h = target.rect[2:]
            t = time.monotonic() - t0
            self._broadcast_cursor({"t": "c", "x": round(w / 2 + w / 3 * np.cos(t)),
                                    "y": round(h / 2 + h / 3 * np.sin(t)), "v": 1, "s": 1})

    def _broadcast_cursor(self, msg: dict) -> None:
        self.cursor_msg = msg
        for s in list(self.sessions):
            s.queue_cursor(msg)


class Session:
    """One connected iPad (or browser)."""

    _ids = itertools.count(1)

    def __init__(self, hub: Hub, ws: web.WebSocketResponse, peer: str):
        self.id = next(Session._ids)
        self.hub, self.ws, self.peer = hub, ws, peer
        self.wake = asyncio.Event()
        self.generation = -1
        self.dirty: np.ndarray | None = None
        self.lowq: np.ndarray | None = None   # tiles last sent blurry, waiting to be sharpened
        self.want_keyframe = False
        self.inflight = 0
        self.last_ack = time.monotonic()
        self.frame_id = 0
        self.cursor_event = asyncio.Event()
        self.cursor_pending: dict | None = None
        self.shapes_sent: set[int] = set()
        self.greeted = False
        self.can_scale = False                  # the page draws half-size updates stretched back
        self.link_rate: float | None = None     # bytes/s from sending an update to the iPad drawing it
        self.motion_bpp = 0.2                   # JPEG bytes per changed pixel of full-size moving content
        self.motion_mode = ""                   # how the last moving update went: sharp / normal / light
        self.motion_auto = MotionMode()
        self.moving_delays: deque[float] = deque(maxlen=15)   # delays of recent moving updates
        self.base_rtt: float | None = None      # the lowest ping seen: what this Wi-Fi does when idle
        self.sent_at: dict[int, tuple[float, int]] = {}
        self.drawn_at: dict[int, tuple[float, bool]] = {}   # frame id -> (when the PC drew it, moving?)
        self.delays: deque[float] = deque(maxlen=30)
        self.rtt = 0.0                          # network round trip, measured by the iPad
        self.last_delay_report = 0.0

    async def send_json(self, obj) -> None:
        if not self.ws.closed:
            try:
                await self.ws.send_str(json.dumps(obj, separators=(",", ":")))
            except ConnectionError:  # the iPad went away mid-send; handle_ws cleans up
                pass

    async def frame_writer(self) -> None:
        try:
            await self._frame_loop()
        except ConnectionError:
            pass

    async def _frame_loop(self) -> None:
        hub = self.hub
        while not self.ws.closed:
            refining = self.lowq is not None and self.lowq.any()
            try:
                await asyncio.wait_for(self.wake.wait(), 0.1 if refining else 1.0)
            except asyncio.TimeoutError:
                pass
            self.wake.clear()
            if self.inflight >= MAX_INFLIGHT:
                if time.monotonic() - self.last_ack < 5:
                    continue
                self.inflight = 0   # acks went missing; don't stall forever
            job = hub.take(self)
            if job is None:
                continue
            if job.new_size:
                await self.send_json(hub.size_message(job.target))
            quality, half = None, False
            if not job.sharp:                      # moving content: full or half size, chosen automatically
                changed_px = int(job.moving.sum()) * tiles.TILE ** 2
                screen_px = job.target.rect[2] * job.target.rect[3]
                recent = sorted(self.moving_delays)
                delay = recent[len(recent) // 2] if len(recent) >= 5 else None
                mode = self.motion_auto.update(
                    desired_motion_mode(screen_px, self.link_rate, self.motion_bpp, delay, self.rtt, self.base_rtt),
                    time.monotonic())
                if mode == "light" and not self.can_scale:   # this page can't stretch half-size updates
                    mode = "normal"
                self.motion_mode = mode
                quality = hub.args.motion_quality
                half = mode == "light"
            parts, motion_bytes = await hub.encode(job, quality, half)
            if quality and not half and motion_bytes:   # learn how big full-size moving updates are
                self.motion_bpp = 0.7 * self.motion_bpp + 0.3 * motion_bytes / changed_px
            flags = (tiles.FLAG_KEYFRAME if job.keyframe else 0) | (tiles.FLAG_SHARP if job.sharp else 0)
            self.frame_id = (self.frame_id + 1) & 0xFFFFFFFF
            if not job.keyframe and job.mask.any():   # new content (not a resend): measure its delay
                self.drawn_at[self.frame_id] = (job.shown_at, not job.sharp)
                if len(self.drawn_at) > 64:
                    self.drawn_at.pop(next(iter(self.drawn_at)))
            payload = tiles.pack_frame(self.frame_id, parts, flags)
            self.sent_at[self.frame_id] = (time.perf_counter(), len(payload))
            if len(self.sent_at) > 64:
                self.sent_at.pop(next(iter(self.sent_at)))
            self.inflight += 1
            await self.ws.send_bytes(payload)

    def queue_cursor(self, msg: dict) -> None:
        self.cursor_pending = msg
        self.cursor_event.set()

    async def cursor_writer(self) -> None:
        while not self.ws.closed:
            await self.cursor_event.wait()
            self.cursor_event.clear()
            msg = self.cursor_pending
            shape = msg.get("s")
            if shape and shape not in self.shapes_sent and shape in self.hub.shapes:
                self.shapes_sent.add(shape)
                await self.send_json(self.hub.shapes[shape])
            await self.send_json(msg)

    def handle(self, m: dict) -> None:
        t = m.get("t")
        hub, injector = self.hub, self.hub.injector
        if t in ("p", "w", "k"):
            log.debug("input from session %d: %s", self.id, m)
        if t == "ack":
            self.inflight = max(0, self.inflight - 1)
            self.last_ack = time.monotonic()
            self.wake.set()
            self._note_rate(self.sent_at.pop(m.get("id"), None))
            self._note_delay(self.drawn_at.pop(m.get("id"), None))
        elif t == "p" and injector:
            pos = hub.to_screen(m["x"], m["y"])
            if pos:
                injector.pointer(self.id, str(m["k"]), str(m["pt"]), m.get("id", 0), *pos,
                                 pressure=float(m.get("p", 0)), tilt_x=int(m.get("tx", 0)),
                                 tilt_y=int(m.get("ty", 0)), buttons=int(m.get("b", 0)))
        elif t == "w" and injector:
            pos = hub.to_screen(m["x"], m["y"])
            if pos:
                injector.wheel(*pos, float(m.get("dx", 0)), float(m.get("dy", 0)), int(m.get("m", 0)))
        elif t == "k" and injector:
            injector.key(self.id, str(m.get("c", "")), bool(m.get("d")))
        elif t == "key":
            self.want_keyframe = True
            self.wake.set()
        elif t == "ping":
            self.rtt = max(0.0, float(m.get("rtt") or 0) / 1000)
            if self.rtt > 0:
                self.base_rtt = self.rtt if self.base_rtt is None else min(self.base_rtt, self.rtt)
            asyncio.ensure_future(self.send_json({"t": "pong", "ts": m.get("ts")}))
        elif t == "hi":
            self._greet(m)

    def _note_rate(self, sent: tuple[float, int] | None) -> None:
        """How fast the iPad takes updates in: bytes / (sending -> drawn), smoothed."""
        if not sent:
            return
        sent_at, size = sent
        # Leave out the idle round trip (the same for small and big updates), but not any extra
        # ping on top of it: that is Wi-Fi queueing, which is exactly a connection not keeping up.
        took = time.perf_counter() - sent_at - (self.base_rtt or 0.0)
        if size >= 30_000 and took > 0.002:      # small updates say little about the connection
            rate = size / took
            self.link_rate = rate if self.link_rate is None else 0.8 * self.link_rate + 0.2 * rate

    def _note_delay(self, drawn: tuple[float, bool] | None) -> None:
        """Screen-to-iPad delay: from the PC drawing a change to the iPad showing it."""
        if not drawn:
            return
        drawn_at, moving = drawn
        # The ack left the iPad right after drawing, and took about half a round trip to arrive.
        delay = max(0.0, time.perf_counter() - drawn_at - self.rtt / 2)
        self.delays.append(delay)
        if moving:
            self.moving_delays.append(delay)
        now = time.monotonic()
        if now - self.last_delay_report >= 1.0:
            self.last_delay_report = now
            ms = sorted(self.delays)[len(self.delays) // 2] * 1000
            asyncio.ensure_future(self.send_json({"t": "lat", "ms": round(ms), "motion": self.motion_mode}))

    def _greet(self, m: dict) -> None:
        self.can_scale = bool(m.get("scale"))
        dpr = float(m.get("dpr") or 1)
        sw, sh = int(m.get("sw") or 0), int(m.get("sh") or 0)
        landscape = int(m.get("vw") or 0) >= int(m.get("vh") or 0)
        long_side, short_side = max(sw, sh), min(sw, sh)
        pw, ph = (long_side, short_side) if landscape else (short_side, long_side)
        native = (round(pw * dpr), round(ph * dpr))
        if self.greeted:
            return
        self.greeted = True
        log.info("iPad connected from %s: screen %dx%d px", self.peer, *native)
        target = self.hub.target
        if target and target.device and not target.mirroring_primary and target.rect[2:] != native:
            log.info("  Tip: set this display to %dx%d with %d%% scale in Windows display settings "
                     "for a pixel-perfect picture.", native[0], native[1], round(dpr * 100))


# ---------------------------------------------------------------------------
# HTTP / WebSocket
# ---------------------------------------------------------------------------

NO_CACHE = {"Cache-Control": "no-cache"}


async def handle_index(request: web.Request) -> web.StreamResponse:
    return web.FileResponse(WEB_DIR / "index.html", headers=NO_CACHE)


async def handle_static(request: web.Request) -> web.StreamResponse:
    return web.FileResponse(WEB_DIR / request.match_info["name"], headers=NO_CACHE)


async def handle_icon(request: web.Request) -> web.Response:
    return web.Response(body=request.app["icon"], content_type="image/png")


async def handle_manifest(request: web.Request) -> web.Response:
    return web.json_response({
        "name": "iPad Display", "short_name": "Display", "display": "fullscreen",
        "background_color": "#000000", "theme_color": "#000000",
        "icons": [{"src": "icon.png", "sizes": "180x180", "type": "image/png"}],
    })


async def handle_ws(request: web.Request) -> web.WebSocketResponse:
    hub: Hub = request.app["hub"]
    ws = web.WebSocketResponse(heartbeat=20, max_msg_size=1 << 20, compress=False)
    await ws.prepare(request)
    if not hub.key_ok(request.query.get("key", "")):
        log.warning("Rejected a connection from %s with a wrong key", request.remote)
        await ws.close(code=4001, message=b"wrong key")
        return ws

    session = Session(hub, ws, request.remote or "?")
    hub.add(session)
    tasks = [asyncio.ensure_future(session.frame_writer()), asyncio.ensure_future(session.cursor_writer())]
    try:
        caps = hub.injector.caps if hub.injector else {}
        await session.send_json({"t": "hello", "v": VERSION, "caps": caps,
                                 "saveDir": friendly_path(hub.args.save_dir)})
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                try:
                    session.handle(json.loads(msg.data))
                except Exception as exc:  # a malformed message must not end the session
                    log.debug("bad message %r: %s", msg.data[:200], exc)
            elif msg.type == WSMsgType.ERROR:
                break
    finally:
        for task in tasks:
            task.cancel()
        hub.remove(session)
        if session.greeted:
            log.info("iPad disconnected (%s)", session.peer)
    return ws


# ---- moving photos and files between the iPad and the PC ------------------

def safe_filename(name: str) -> str:
    """A plain file name that can only land inside the save folder and is valid on Windows."""
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    name = re.sub(r'[<>:"|?*\x00-\x1f]', "_", name).strip(" .")
    stem, dot, ext = name.partition(".")
    if not stem or stem.upper() in RESERVED_NAMES:
        name = f"file_{stem}{dot}{ext}" if stem else f"file{dot}{ext}"
    if len(name) > 150:
        root, ext = os.path.splitext(name)
        name = root[:150 - len(ext)] + ext
    return name


def unique_path(path: Path) -> Path:
    """`path`, or "name (2).ext", "name (3).ext", ... if it already exists."""
    if not path.exists():
        return path
    for n in itertools.count(2):
        candidate = path.with_name(f"{path.stem} ({n}){path.suffix}")
        if not candidate.exists():
            return candidate


def friendly_path(path: Path) -> str:
    try:
        return str(path.relative_to(Path.home()))
    except ValueError:
        return str(path)


def human_size(n: int) -> str:
    for unit in ("bytes", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "bytes" else f"{n:.1f} {unit}"
        n /= 1024


def check_key(request: web.Request) -> Hub:
    hub: Hub = request.app["hub"]
    if not hub.key_ok(request.query.get("key", "")):
        raise web.HTTPForbidden(text="wrong key")
    return hub


async def handle_upload(request: web.Request) -> web.Response:
    """iPad -> PC: the request body is one file, streamed straight to the save folder."""
    hub = check_key(request)
    folder: Path = hub.args.save_dir
    folder.mkdir(parents=True, exist_ok=True)
    name = safe_filename(request.query.get("name", "")) or "file"
    partial = folder / f".{secrets.token_hex(6)}.part"
    size = 0
    try:
        with open(partial, "wb") as f:
            async for chunk in request.content.iter_chunked(1 << 20):
                f.write(chunk)
                size += len(chunk)
        final = unique_path(folder / name)
        os.replace(partial, final)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    try:  # keep the photo's own date (the iPad sends milliseconds since 1970)
        stamp = int(request.query.get("mtime", "")) / 1000
        os.utime(final, (stamp, stamp))
    except (ValueError, OSError, OverflowError):
        pass
    log.info("Received %s (%s) from %s -> %s", final.name, human_size(size), request.remote, folder)
    return web.json_response({"name": final.name, "size": size})


async def handle_saved(request: web.Request) -> web.Response:
    """Act on files the iPad just sent: show them in File Explorer or copy them for Ctrl+V."""
    hub = check_key(request)
    if not IS_WINDOWS:
        return web.json_response({"error": "only available on Windows"}, status=501)
    data = await request.json()
    folder: Path = hub.args.save_dir
    paths = [folder / safe_filename(str(n)) for n in data.get("names", [])[:1000]]
    paths = [p for p in paths if p.is_file()]
    if not paths:
        return web.json_response({"error": "those files are no longer in the folder"}, status=404)
    loop = asyncio.get_running_loop()
    try:
        if data.get("action") == "copy":
            await loop.run_in_executor(None, win32.set_clipboard_files, [str(p) for p in paths])
        else:
            win32.show_in_explorer(paths[-1])
    except OSError as exc:
        return web.json_response({"error": str(exc)}, status=500)
    return web.json_response({"ok": True, "count": len(paths)})


async def handle_clipboard(request: web.Request) -> web.Response:
    """PC -> iPad: list the files currently copied on the PC (Ctrl+C in File Explorer)."""
    hub = check_key(request)
    if not IS_WINDOWS:
        return web.json_response({"error": "only available on Windows"}, status=501)
    try:
        copied = await asyncio.get_running_loop().run_in_executor(None, win32.get_clipboard_files)
    except OSError as exc:
        return web.json_response({"error": str(exc)}, status=503)
    return web.json_response(hub.make_offer(copied))


async def handle_clipboard_file(request: web.Request) -> web.StreamResponse:
    hub = check_key(request)
    path = hub.offered.get(request.match_info["fid"])
    if not path or not os.path.isfile(path):
        raise web.HTTPNotFound(text="copy the file on the PC again")
    disposition = "attachment" if request.query.get("dl") else "inline"
    headers = {"Content-Disposition": f"{disposition}; filename*=UTF-8''{quote(os.path.basename(path))}",
               "Cache-Control": "no-store"}
    return web.FileResponse(path, headers=headers)


async def handle_status(request: web.Request) -> web.Response:
    """Lets a newly started iPad Display recognise an already running one on this port."""
    return web.json_response({"app": "iPad Display", "version": VERSION})


async def handle_quit(request: web.Request) -> web.Response:
    """Another iPad Display is starting on this port: shut down cleanly so it can take over."""
    check_key(request)
    if request.remote not in ("127.0.0.1", "::1"):
        raise web.HTTPForbidden(text="only from this PC")
    log.info("iPad Display was started again, so this copy is closing and the new one takes over.")
    asyncio.get_running_loop().call_later(0.3, signal.raise_signal, signal.SIGINT)   # like Ctrl+C
    return web.json_response({"ok": True})


def make_icon(size: int = 180) -> bytes:
    img = Image.new("RGB", (size, size), (15, 23, 42))
    d = ImageDraw.Draw(img)
    s = size / 180
    d.rounded_rectangle([18 * s, 44 * s, 118 * s, 112 * s], radius=8 * s, outline=(148, 163, 184), width=round(7 * s))
    d.rectangle([52 * s, 112 * s, 84 * s, 128 * s], fill=(148, 163, 184))
    d.rounded_rectangle([96 * s, 62 * s, 162 * s, 140 * s], radius=10 * s, fill=(56, 189, 248))
    d.rounded_rectangle([103 * s, 69 * s, 155 * s, 133 * s], radius=5 * s, fill=(12, 74, 110))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def make_app(hub: Hub) -> web.Application:
    app = web.Application()
    app["hub"] = hub
    app["icon"] = make_icon()
    app["tasks"] = []
    app.router.add_get("/", handle_index)
    app.router.add_get("/ws", handle_ws)
    app.router.add_get("/icon.png", handle_icon)
    app.router.add_get("/manifest.json", handle_manifest)
    app.router.add_get(r"/{name:(app\.js|style\.css)}", handle_static)
    app.router.add_get("/status", handle_status)
    app.router.add_post("/quit", handle_quit)
    app.router.add_post("/upload", handle_upload)
    app.router.add_post("/saved", handle_saved)
    app.router.add_get("/clipboard", handle_clipboard)
    app.router.add_get("/clipboard/{fid}", handle_clipboard_file)

    async def on_startup(app):
        hub.loop = asyncio.get_running_loop()
        hub.loop.set_exception_handler(quiet_disconnects)
        threading.Thread(target=hub.capture_loop, name="capture", daemon=True).start()
        if hub.args.monitor == "test":
            app["tasks"].append(asyncio.ensure_future(hub.fake_cursor()))
        elif IS_WINDOWS:
            threading.Thread(target=hub.cursor_loop, name="cursor", daemon=True).start()
            if not hub.args.no_drop_box:
                hub.drop_box = win32.DropBox(hub.drop_box_rect, hub.drop_files)
                hub.drop_box.start()
        if hub.injector:
            app["tasks"].append(asyncio.ensure_future(input_keepalive(hub.injector)))
        if hub.args.host in ALL_INTERFACES:
            app["tasks"].append(asyncio.ensure_future(watch_network(hub)))

    async def on_cleanup(app):
        hub.stopping.set()
        if hub.mdns:
            hub.mdns.stop()
        if hub.drop_box:
            hub.drop_box.stop()
        hub.wake_capture.set()
        for task in app["tasks"]:
            task.cancel()
        if hub.injector:
            hub.injector.close()
        hub.pool.shutdown(wait=False)

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


def quiet_disconnects(loop, context) -> None:
    """Windows' proactor loop logs a traceback whenever a client vanishes abruptly
    (iPad sleeps, Wi-Fi drops). That is normal here, so keep the console clean."""
    if isinstance(context.get("exception"), (ConnectionResetError, ConnectionAbortedError)):
        return
    loop.default_exception_handler(context)


async def watch_network(hub: Hub) -> None:
    """When the PC joins another Wi-Fi or starts a hotspot, show the addresses that work now."""
    last = {ip for _, ip in network_addresses()}
    while True:
        await asyncio.sleep(NETWORK_CHECK)
        nets = network_addresses()
        current = {ip for _, ip in nets}
        if current == last:
            continue
        last = current
        if not nets:
            log.info("Network changed: this PC is not on any network right now.")
            if hotspot_allowed(hub.args) and not hub.hotspot_tried:
                await asyncio.get_running_loop().run_in_executor(
                    None, start_hotspot, hub, "No network, so the iPad can't reach this PC.")
            continue
        log.info("Network changed. The iPad can now connect at:")
        print_addresses(hub.args, hub.key, named=hub.mdns is not None, nets=nets, qr=hub.mdns is None)


async def input_keepalive(injector) -> None:
    while True:
        await asyncio.sleep(0.02)
        try:
            injector.keepalive()
        except Exception as exc:  # never let a transient failure kill the task
            log.debug("keepalive: %s", exc)


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

def load_key(args) -> str:
    if args.no_auth:
        return ""
    if args.key:
        return args.key
    if not args.new_key:
        try:
            key = KEY_FILE.read_text().strip()
            if key:
                return key
        except OSError:
            pass
    key = "".join(secrets.choice(KEY_ALPHABET) for _ in range(8))
    try:
        KEY_FILE.write_text(key)
    except OSError as exc:
        log.warning("Could not save the access key to %s: %s", KEY_FILE, exc)
    return key


def network_addresses() -> list[tuple[str, str]]:
    """(label, IPv4) of the networks an iPad could reach this PC on, main network first.

    On Windows this leaves out adapters an iPad can never reach (WSL, Hyper-V, VMs), so an
    empty list really means "no network" and the laptop's own hotspot is needed.
    """
    if IS_WINDOWS:
        found = win32.network_addresses()
    else:
        found = [("Network", ip) for ip in sorted(mdns.local_ipv4())]
    default = mdns.route_ip("10.254.254.254")
    return sorted(found, key=lambda net: net[1] != default)


def port_in_use(port: int) -> bool:
    try:
        socket.create_connection(("127.0.0.1", port), 0.5).close()
        return True
    except OSError:
        return False


def port_owner(port: int) -> tuple[int | None, str]:
    """(pid, program name) of whatever listens on this TCP port, from netstat/tasklist."""
    if not IS_WINDOWS:
        return None, ""
    try:
        out = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True,
                             errors="replace", timeout=10).stdout
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 5 and parts[1].endswith(f":{port}") and parts[2] in ("0.0.0.0:0", "[::]:0"):
                pid = int(parts[-1])
                row = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"], capture_output=True,
                                     text=True, errors="replace", timeout=10).stdout.strip()
                return pid, row.split('","')[0].strip('"') if row.startswith('"') else ""
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass
    return None, ""


def take_over_port(args, key: str) -> None:
    """If iPad Display is already running on our port, close that copy so this one can start.

    Newer copies are asked to shut down cleanly (they also turn off a hotspot they started);
    an older copy that doesn't know how is closed directly. Other programs are left alone.
    """
    if not port_in_use(args.port):
        return
    base = f"http://127.0.0.1:{args.port}"
    closed = False
    try:
        with urllib.request.urlopen(base + "/status", timeout=2) as r:
            if json.loads(r.read()).get("app") == "iPad Display":
                urllib.request.urlopen(urllib.request.Request(f"{base}/quit?key={quote(key)}", method="POST"),
                                       timeout=3).read()
                closed = True
    except (OSError, ValueError):
        pass
    if not closed:
        pid, name = port_owner(args.port)
        if pid and name.lower() == "ipaddisplay.exe":
            subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True, timeout=10)
            closed = True
    if closed:
        print("\n  iPad Display was already running, so that copy was closed and this one takes over.")
        for _ in range(50):                      # wait (up to 10 s) for it to let go of the port
            if not port_in_use(args.port):
                break
            time.sleep(0.2)


def hotspot_allowed(args) -> bool:
    return IS_WINDOWS and args.host in ALL_INTERFACES and args.monitor != "test" and not args.no_hotspot


def start_hotspot(hub: Hub, why: str) -> None:
    """Turn on the laptop's own Wi-Fi (Windows Mobile hotspot) and show how to join it."""
    hub.hotspot_tried = True
    print("\n  " + " ".join(filter(None, [why, "Turning on this laptop's own Wi-Fi (Mobile hotspot)..."])),
          flush=True)
    was_on = hotspot.status().get("state") == "On"
    info = hotspot.start()
    if not info.get("ok"):
        print(f"  Could not turn on the hotspot: {info.get('error')}", flush=True)
        return
    hub.hotspot_started = not was_on
    print("\n  The laptop's Wi-Fi is ON. On the iPad, join this Wi-Fi (or scan the code with the Camera):\n")
    print(f"      Wi-Fi name : {info['ssid']}")
    print(f"      Password   : {info['passphrase']}\n")
    print_qr(hotspot.wifi_qr_text(info["ssid"], info["passphrase"]))
    if info.get("offline"):
        print("\n  There's no internet, so the iPad will say \"No Internet Connection\" - that's fine.")
    time.sleep(2)   # give the hotspot's own address a moment to appear
    sys.stdout.flush()


def page_url(host: str, args, key: str) -> str:
    return f"http://{host}:{args.port}/" + (f"?key={key}" if key else "")


def print_addresses(args, key: str, named: bool, nets=None, qr: bool = True) -> None:
    """Print the address to open on the iPad (and its QR code), plus per-network fallbacks."""
    if args.host not in ALL_INTERFACES:
        main_url, others = page_url(args.host, args, key), []
    else:
        nets = network_addresses() if nets is None else nets
        if named:
            main_url, others = page_url(f"{args.name}.local", args, key), nets
        elif nets:
            main_url, others = page_url(nets[0][1], args, key), nets[1:]
        else:
            main_url, others = page_url("127.0.0.1", args, key), []
    if qr:
        print("\n  On the iPad (same Wi-Fi), open Safari and go to:\n")
        print(f"      {main_url}\n")
        print_qr(main_url)
        if others:
            print("\n  This name keeps working when the PC's address changes. If it doesn't open, use:"
                  if named else "\n  On another network:")
    else:  # a network change: the .local name is unchanged, only the fallbacks moved
        print(f"      {main_url}   (same as before)")
    width = max((len(label) for label, _ in others), default=0)
    for label, ip in others:
        print(f"      {label:<{width}}  {page_url(ip, args, key)}")
    sys.stdout.flush()


def print_qr(text: str) -> None:
    try:
        import qrcode
    except ImportError:
        return
    qr = qrcode.QRCode(border=2)
    qr.add_data(text)
    qr.make(fit=True)
    matrix = qr.get_matrix()
    if len(matrix) % 2:
        matrix.append([False] * len(matrix[0]))
    # Two modules per character; light modules are drawn, dark ones are the (dark) background.
    glyph = {(False, False): "█", (False, True): "▀", (True, False): "▄", (True, True): " "}
    try:  # skip it where the output can't show block characters (e.g. redirected to a file)
        "".join(glyph.values()).encode(sys.stdout.encoding or "ascii")
    except (UnicodeEncodeError, LookupError):
        return
    lines = ["    " + "".join(glyph[(t, b)] for t, b in zip(matrix[r], matrix[r + 1]))
             for r in range(0, len(matrix), 2)]
    print("\n".join(lines))


def parse_size(text: str) -> tuple[int, int]:
    w, _, h = text.lower().partition("x")
    return int(w), int(h)


def parse_name(text: str) -> str:
    name = text.lower().removesuffix(".local")
    if not re.fullmatch(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?", name):
        raise argparse.ArgumentTypeError("use letters, digits and hyphens, e.g. ipad-display")
    return name


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Use an iPad as an extra monitor for this PC.")
    p.add_argument("--port", type=int, default=8765, help="port to listen on (default 8765)")
    p.add_argument("--host", default="0.0.0.0", help="address to listen on (default: all)")
    p.add_argument("--name", type=parse_name, default="ipad-display",
                   help="name the iPad can use instead of the IP address: http://<name>.local (default ipad-display)")
    p.add_argument("--no-mdns", action="store_true", help="don't answer for <name>.local; use IP addresses only")
    p.add_argument("--monitor", default="auto",
                   help="display to stream: auto (default: the second/virtual display), a number from --list, "
                        "a name like DISPLAY2, or 'test' for a synthetic test pattern")
    p.add_argument("--list", action="store_true", help="list displays and exit")
    p.add_argument("--capture", choices=("auto", "dxgi", "gdi"), default="auto", help="screen capture method")
    p.add_argument("--fps", type=int, default=60, help="maximum frames per second (default 60)")
    p.add_argument("--quality", type=int, default=90, help="JPEG quality for still content (default 90)")
    p.add_argument("--motion-quality", type=int, default=65, help="JPEG quality while things move (default 65)")
    p.add_argument("--view-only", action="store_true", help="ignore touch/pen/keyboard input from the iPad")
    p.add_argument("--hotspot", action="store_true",
                   help="turn on this laptop's own Wi-Fi (Mobile hotspot) for the iPad - works without internet")
    p.add_argument("--no-hotspot", action="store_true",
                   help="never turn the hotspot on by itself when the laptop has no network")
    p.add_argument("--no-drop-box", action="store_true",
                   help="don't show the 'Drop files here' box for sending files to the iPad")
    p.add_argument("--save-dir", type=Path, default=DEFAULT_SAVE_DIR,
                   help=r"where photos and files sent from the iPad are saved (default Downloads\iPad Display)")
    p.add_argument("--key", help="use this access key instead of the saved one")
    p.add_argument("--new-key", action="store_true", help="generate a new access key (old links stop working)")
    p.add_argument("--no-auth", action="store_true", help="no access key (anyone on the network can connect)")
    p.add_argument("--test-size", type=parse_size, default=(1280, 800), help="test pattern size, e.g. 1280x800")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)
    args.fps = max(1, min(args.fps, 120))
    return args


def main(argv=None) -> None:
    args = parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except AttributeError:
            pass
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s  %(message)s", datefmt="%H:%M:%S")
    for noisy in ("aiohttp.access", "comtypes", "dxcam"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    if IS_WINDOWS:
        win32.tune_process()
        if args.list:
            print("Displays:")
            for m in win32.list_monitors():
                print("  " + m.describe())
            return
    elif args.monitor != "test":
        print("Not running on Windows: streaming a test pattern instead of a real display.")
        args.monitor = "test"

    try:
        target = capture.find_target(args.monitor, args.test_size)
    except LookupError as exc:
        sys.exit(str(exc))
    key = load_key(args)
    take_over_port(args, key)
    hub = Hub(args, key)
    app = make_app(hub)

    caps = hub.injector.caps if hub.injector else {}
    input_text = ", ".join(k for k, on in caps.items() if on) or "off (view only)"
    if args.host in ALL_INTERFACES and not args.no_mdns:
        try:
            hub.mdns = mdns.MdnsResponder(args.name)
            hub.mdns.start()
        except OSError as exc:
            hub.mdns = None
            log.warning("Could not publish %s.local (%s); use the number address instead.", args.name, exc)

    print(f"\n  iPad Display {VERSION}")
    print(f"  Display : {target.label}")
    print(f"  Input   : {input_text}")
    print(f"  Files   : photos and files sent from the iPad are saved in {args.save_dir}")
    if target.mirroring_primary:
        print("\n  Windows has no second display, so the iPad will MIRROR (duplicate) your main screen.")
        print("  To EXTEND onto the iPad instead:")
        print("  " + capture.mirror_hint().replace("\n", "\n  "))
        print("  The new display is picked up automatically while this program runs.")
    if hotspot_allowed(args) and (args.hotspot or not network_addresses()):
        start_hotspot(hub, "" if args.hotspot else "This laptop isn't on any network.")
    if IS_WINDOWS:   # closing the window with X skips normal shutdown; still switch our hotspot off
        win32.on_console_close(lambda: hub.hotspot_started and hotspot.stop())
        signal.signal(signal.SIGBREAK, signal.default_int_handler)   # Ctrl+Break stops cleanly, like Ctrl+C
    print_addresses(args, key, named=hub.mdns is not None)
    print("\n  Tip: Share > Add to Home Screen gives you a full-screen app.   Ctrl+C stops.\n")

    try:
        web.run_app(app, host=args.host, port=args.port, print=None, access_log=None)
    except OSError as exc:
        pid, name = port_owner(args.port)
        who = f"{name} (PID {pid})" if name else "another program"
        sys.exit(f"Port {args.port} is already used by {who}, so iPad Display can't start ({exc}).\n"
                 f"Close that program, or start iPad Display on another port: iPadDisplay.exe --port 8766")
    finally:
        if hub.hotspot_started:
            print("Turning off the laptop's Wi-Fi hotspot...")
            hotspot.stop()


if __name__ == "__main__":
    try:
        main()
    except (SystemExit, Exception) as exc:
        # Started from a shortcut, the console window would vanish before the message
        # could be read, so keep it open until the user has seen what went wrong.
        failed = not isinstance(exc, SystemExit) or exc.code not in (0, None)
        if not (FROZEN and failed):
            raise
        if isinstance(exc, SystemExit):
            print(exc.code if isinstance(exc.code, str) else "iPad Display stopped.")
        else:
            import traceback
            traceback.print_exc()
        input("\nPress Enter to close this window...")
        sys.exit(1)
