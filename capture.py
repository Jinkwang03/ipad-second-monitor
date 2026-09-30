"""Choosing which display to stream and grabbing its pixels."""
from __future__ import annotations

import logging
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

log = logging.getLogger("ipad-display")
IS_WINDOWS = sys.platform == "win32"
if IS_WINDOWS:
    import win32


@dataclass(frozen=True)
class Target:
    """The desktop rectangle being streamed."""
    rect: tuple[int, int, int, int]   # left, top, width, height in physical pixels
    label: str
    device: str = ""                  # \\.\DISPLAYn, used to find the DXGI output
    dpi: int = 96
    mirroring_primary: bool = False


def find_target(spec: str, test_size=(1280, 800)) -> Target:
    """Resolve --monitor (auto | N | DISPLAYn) to the display to stream."""
    if spec == "test" or not IS_WINDOWS:
        return Target((0, 0, *test_size), f"test pattern {test_size[0]}x{test_size[1]}")
    monitors = win32.list_monitors()
    chosen = None
    if spec == "auto":
        others = [m for m in monitors if not m.primary]
        others.sort(key=lambda m: not m.is_virtual)
        chosen = others[0] if others else next((m for m in monitors if m.primary), monitors[0])
    elif spec.isdigit():
        chosen = next((m for m in monitors if m.index == int(spec)), None)
    else:
        wanted = spec.upper().lstrip("\\.").replace("\\", "")
        chosen = next((m for m in monitors if m.device.upper().endswith(wanted)), None)
    if chosen is None:
        raise LookupError(f"No display matches --monitor {spec!r}. Run with --list to see them.")
    return Target(chosen.rect, chosen.describe(), chosen.device, chosen.dpi,
                  mirroring_primary=chosen.primary and spec == "auto")


def mirror_hint() -> str:
    """Why there is no second display to extend onto, and what to do about it."""
    if not IS_WINDOWS:
        return ""
    if win32.display_topology() == "clone":
        return "Windows is set to Duplicate. Press Win+P and choose Extend."
    packages = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Packages"
    control = sorted(packages.glob("VirtualDrivers.Virtual-Display-Driver_*/VDD Control.exe"))
    if control:
        return ("The virtual display driver is downloaded but not installed. Open VDD Control, "
                f"click Install and approve the admin prompt:\n    {control[0]}")
    return ("Install a virtual display driver: run  winget install --id=VirtualDrivers.Virtual-Display-Driver -e  "
            "then open VDD Control and click Install.")


class _WaitForFrame:
    """Wraps DXGI's output duplication so each frame request waits briefly for the next screen
    update instead of returning at once: a change is picked up the moment Windows draws it,
    rather than at the next polling tick, without spinning the CPU while the screen is still."""

    def __init__(self, inner, timeout_ms: int):
        self._inner, self._timeout_ms = inner, timeout_ms

    def AcquireNextFrame(self, _timeout, info, resource):   # noqa: N802 (COM method name)
        return self._inner.AcquireNextFrame(self._timeout_ms, info, resource)

    def __getattr__(self, name):
        return getattr(self._inner, name)


class DxgiCapture:
    """DXGI desktop duplication through dxcam: fast, and returns None when nothing changed."""

    name = "DXGI"
    waits = True       # grab() blocks until the screen changes (or WAIT_MS passes)
    WAIT_MS = 30

    def __init__(self, target: Target):
        from dxcam.dxcam import DXCamera, Device, Output
        from dxcam.util.io import enum_dxgi_adapters

        for noisy in ("comtypes", "comtypes._post_coinit.unknwn"):  # dxcam re-enables these on import
            logging.getLogger(noisy).setLevel(logging.WARNING)

        # Enumerate afresh rather than using dxcam's import-time snapshot, so a
        # virtual display added while we run is found too.
        self.rect = target.rect
        self.camera = None
        for p_adapter in enum_dxgi_adapters():
            device = Device(p_adapter)
            for p_output in device.enum_outputs():
                output = Output(p_output)
                if output.devicename == target.device:
                    output.update_desc()
                    self.camera = DXCamera(output=output, device=device, region=None, output_color="BGRA",
                                           max_buffer_len=2, backend="dxgi", processor_backend="numpy")
                    break
            if self.camera:
                break
        if self.camera is None:
            raise RuntimeError(f"no DXGI output for {target.device}")
        if (self.camera.width, self.camera.height) != target.rect[2:]:
            self.close()
            raise RuntimeError("DXGI output size does not match the monitor (rotated display?)")
        time.sleep(0.1)  # the first AcquireNextFrame right after creation can fail
        self.shown_at = time.perf_counter()

    def grab(self):
        dup = getattr(self.camera, "_duplicator", None)
        if dup is not None and dup.duplicator is not None and not isinstance(dup.duplicator, _WaitForFrame):
            dup.duplicator = _WaitForFrame(dup.duplicator, self.WAIT_MS)   # again after dxcam recovers
        frame = self.camera.grab()
        if frame is not None:
            if frame.shape[:2] != (self.rect[3], self.rect[2]):
                raise RuntimeError("display size changed")
            # When Windows presented it (same clock as perf_counter); used to measure the delay.
            now = time.perf_counter()
            shown = getattr(dup, "latest_frame_time", 0.0) if dup is not None else 0.0
            self.shown_at = shown if now - 1.0 < shown <= now else now
        return frame

    def close(self):
        if self.camera is not None:
            self.camera.release()
            self.camera = None


class TestPattern:
    """Synthetic moving content for trying the pipeline without a real display."""

    name = "test pattern"
    waits = False

    def __init__(self, target: Target):
        self.rect = target.rect
        w, h = target.rect[2:]
        yy, xx = np.mgrid[0:h, 0:w]
        base = np.zeros((h, w, 4), np.uint8)
        base[..., 0] = (xx * 255 // max(w - 1, 1)).astype(np.uint8)
        base[..., 1] = (yy * 255 // max(h - 1, 1)).astype(np.uint8)
        base[..., 2] = 90
        for i in range(0, w, 128):              # grid lines to judge sharpness
            base[:, i:i + 2, :3] = 230
        for j in range(0, h, 128):
            base[j:j + 2, :, :3] = 230
        self.base = base
        self.t0 = time.monotonic()

    def grab(self):
        frame = self.base.copy()
        h, w = frame.shape[:2]
        t = time.monotonic() - self.t0
        size = min(w, h) // 6
        x = int((w - size) * (0.5 + 0.5 * np.sin(t * 1.3)))
        y = int((h - size) * (0.5 + 0.5 * np.sin(t * 0.9 + 1)))
        frame[y:y + size, x:x + size, :3] = (40, 200, 255)
        bar = int(t * 4) % 20                    # a "clock" that ticks four times a second
        frame[h - 40:h - 20, 20:20 + bar * 20, :3] = 255
        self.shown_at = time.perf_counter()
        return frame

    def close(self):
        pass


def open_source(target: Target, method: str):
    """method: auto | dxgi | gdi. Test targets always use the test pattern."""
    if not target.device:
        return TestPattern(target)
    if method in ("auto", "dxgi"):
        try:
            return DxgiCapture(target)
        except Exception as exc:  # dxcam missing, driver quirk, remote session, ...
            if method == "dxgi":
                raise
            log.info("DXGI capture unavailable (%s); using GDI", exc)
    return win32.GdiCapture(target.rect)
