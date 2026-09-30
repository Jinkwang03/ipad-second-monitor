"""Win32 plumbing via ctypes: DPI awareness, monitors, GDI capture, cursor, input and clipboard.

Importing this module switches the process to per-monitor DPI awareness so that
every coordinate we see or produce is in physical pixels.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import logging
import struct
import subprocess
import threading
import time
from dataclasses import dataclass

import numpy as np

log = logging.getLogger("ipad-display")

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


def _proto(dll, name, restype, *argtypes):
    fn = getattr(dll, name)
    fn.restype = restype
    fn.argtypes = argtypes
    return fn


# ---------------------------------------------------------------------------
# Process setup
# ---------------------------------------------------------------------------

def _enable_dpi_awareness() -> None:
    try:  # Windows 10 1703+: per-monitor v2
        if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return
    except AttributeError:
        pass
    try:
        ctypes.WinDLL("shcore").SetProcessDpiAwareness(2)
    except (AttributeError, OSError):
        user32.SetProcessDPIAware()


_enable_dpi_awareness()


def tune_process() -> None:
    """1 ms timer resolution for smooth pacing; stop console clicks from freezing us."""
    kernel32.SetConsoleTitleW("iPad Display")
    try:
        ctypes.WinDLL("winmm").timeBeginPeriod(1)
    except OSError:
        pass
    # Clicking into a console with QuickEdit on pauses every write to it, which
    # would stall the server the next time it logs. Turn QuickEdit off.
    STD_INPUT_HANDLE, ENABLE_QUICK_EDIT_MODE, ENABLE_EXTENDED_FLAGS = -10, 0x40, 0x80
    handle = kernel32.GetStdHandle(STD_INPUT_HANDLE)
    mode = wt.DWORD()
    if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
        kernel32.SetConsoleMode(handle, (mode.value & ~ENABLE_QUICK_EDIT_MODE) | ENABLE_EXTENDED_FLAGS)


# ---------------------------------------------------------------------------
# Monitors
# ---------------------------------------------------------------------------

class MONITORINFOEXW(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", wt.RECT), ("rcWork", wt.RECT),
                ("dwFlags", wt.DWORD), ("szDevice", wt.WCHAR * 32)]


class DISPLAY_DEVICEW(ctypes.Structure):
    _fields_ = [("cb", wt.DWORD), ("DeviceName", wt.WCHAR * 32), ("DeviceString", wt.WCHAR * 128),
                ("StateFlags", wt.DWORD), ("DeviceID", wt.WCHAR * 128), ("DeviceKey", wt.WCHAR * 128)]


MONITORENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HMONITOR, wt.HDC, ctypes.POINTER(wt.RECT), wt.LPARAM)
_proto(user32, "EnumDisplayMonitors", wt.BOOL, wt.HDC, ctypes.POINTER(wt.RECT), MONITORENUMPROC, wt.LPARAM)
_proto(user32, "GetMonitorInfoW", wt.BOOL, wt.HMONITOR, ctypes.POINTER(MONITORINFOEXW))
_proto(user32, "EnumDisplayDevicesW", wt.BOOL, wt.LPCWSTR, wt.DWORD, ctypes.POINTER(DISPLAY_DEVICEW), wt.DWORD)
_proto(user32, "GetDpiForSystem", wt.UINT)

VIRTUAL_HINTS = ("virtual", "idd", "parsec", "spacedesk", "dummy", "headless", "mtt", "sudomaker")


@dataclass(frozen=True)
class Monitor:
    index: int          # 1-based, primary first
    device: str         # \\.\DISPLAYn
    left: int
    top: int
    width: int
    height: int
    primary: bool
    adapter: str        # e.g. "Intel(R) Arc(TM) 140T GPU" or "Virtual Display Driver"
    dpi: int

    @property
    def rect(self) -> tuple[int, int, int, int]:
        return (self.left, self.top, self.width, self.height)

    @property
    def is_virtual(self) -> bool:
        name = self.adapter.lower()
        return any(h in name for h in VIRTUAL_HINTS)

    @property
    def scale_percent(self) -> int:
        return round(self.dpi * 100 / 96)

    def describe(self) -> str:
        tags = [t for t, on in (("primary", self.primary), ("virtual", self.is_virtual)) if on]
        extra = f" [{', '.join(tags)}]" if tags else ""
        return (f"{self.index}: {self.device}  {self.width}x{self.height} at ({self.left},{self.top})"
                f"  scale {self.scale_percent}%  {self.adapter}{extra}")


def _monitor_dpi(hmon) -> int:
    try:
        x, y = wt.UINT(), wt.UINT()
        if ctypes.WinDLL("shcore").GetDpiForMonitor(hmon, 0, ctypes.byref(x), ctypes.byref(y)) == 0:
            return x.value
    except (AttributeError, OSError):
        pass
    return 96


def _adapter_names() -> dict[str, str]:
    names = {}
    dd = DISPLAY_DEVICEW(cb=ctypes.sizeof(DISPLAY_DEVICEW))
    i = 0
    while user32.EnumDisplayDevicesW(None, i, ctypes.byref(dd), 0):
        names[dd.DeviceName] = dd.DeviceString
        i += 1
    return names


def list_monitors() -> list[Monitor]:
    raw = []

    def callback(hmon, _hdc, _rect, _lparam):
        mi = MONITORINFOEXW(cbSize=ctypes.sizeof(MONITORINFOEXW))
        if user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
            r = mi.rcMonitor
            raw.append((mi.szDevice, r.left, r.top, r.right - r.left, r.bottom - r.top,
                        bool(mi.dwFlags & 1), _monitor_dpi(hmon)))
        return True

    user32.EnumDisplayMonitors(None, None, MONITORENUMPROC(callback), 0)
    adapters = _adapter_names()
    raw.sort(key=lambda m: (not m[5], m[1], m[2]))
    return [Monitor(i + 1, dev, x, y, w, h, primary, adapters.get(dev, "?"), dpi)
            for i, (dev, x, y, w, h, primary, dpi) in enumerate(raw)]


def system_dpi() -> int:
    return user32.GetDpiForSystem() or 96


# ---------------------------------------------------------------------------
# Network adapters (to label the addresses an iPad can use)
# ---------------------------------------------------------------------------

class SOCKET_ADDRESS(ctypes.Structure):
    _fields_ = [("lpSockaddr", ctypes.c_void_p), ("iSockaddrLength", ctypes.c_int)]


class IP_ADAPTER_UNICAST_ADDRESS(ctypes.Structure):
    pass


IP_ADAPTER_UNICAST_ADDRESS._fields_ = [   # leading fields only; Windows allocates these
    ("Length", wt.ULONG), ("Flags", wt.DWORD), ("Next", ctypes.POINTER(IP_ADAPTER_UNICAST_ADDRESS)),
    ("Address", SOCKET_ADDRESS)]


class IP_ADAPTER_ADDRESSES(ctypes.Structure):
    pass


IP_ADAPTER_ADDRESSES._fields_ = [          # leading fields only, up to OperStatus
    ("Length", wt.ULONG), ("IfIndex", wt.DWORD), ("Next", ctypes.POINTER(IP_ADAPTER_ADDRESSES)),
    ("AdapterName", ctypes.c_char_p), ("FirstUnicastAddress", ctypes.POINTER(IP_ADAPTER_UNICAST_ADDRESS)),
    ("FirstAnycastAddress", ctypes.c_void_p), ("FirstMulticastAddress", ctypes.c_void_p),
    ("FirstDnsServerAddress", ctypes.c_void_p), ("DnsSuffix", ctypes.c_wchar_p),
    ("Description", ctypes.c_wchar_p), ("FriendlyName", ctypes.c_wchar_p),
    ("PhysicalAddress", ctypes.c_ubyte * 8), ("PhysicalAddressLength", wt.ULONG), ("Flags", wt.ULONG),
    ("Mtu", wt.ULONG), ("IfType", wt.ULONG), ("OperStatus", ctypes.c_int)]

iphlpapi = ctypes.WinDLL("iphlpapi")
_proto(iphlpapi, "GetAdaptersAddresses", wt.ULONG, wt.ULONG, wt.ULONG, ctypes.c_void_p,
       ctypes.c_void_p, ctypes.POINTER(wt.ULONG))
UNREACHABLE_ADAPTERS = ("vethernet", "wsl", "hyper-v", "vmware", "virtualbox", "loopback")
HOTSPOT_PREFIX = "192.168.137."   # Windows Mobile hotspot's own network


def network_addresses() -> list[tuple[str, str]]:
    """(label, IPv4) for connected adapters an iPad could reach, e.g. ("Wi-Fi", "192.168.0.23")."""
    size = wt.ULONG(16384)
    for _ in range(3):
        buf = ctypes.create_string_buffer(size.value)
        err = iphlpapi.GetAdaptersAddresses(2, 0x2 | 0x4 | 0x8, None, buf, ctypes.byref(size))  # AF_INET, skip extras
        if err != 111:   # ERROR_BUFFER_OVERFLOW: retry with the size Windows asked for
            break
    if err != 0:
        return []
    found = []
    adapter = ctypes.cast(buf, ctypes.POINTER(IP_ADAPTER_ADDRESSES))
    while adapter:
        a = adapter.contents
        name = a.FriendlyName or ""
        if a.OperStatus == 1 and a.IfType != 24 and not any(h in name.lower() for h in UNREACHABLE_ADAPTERS):
            ua = a.FirstUnicastAddress
            while ua:
                sa = ua.contents.Address
                if sa.lpSockaddr and sa.iSockaddrLength >= 8:
                    ip = ".".join(str(b) for b in ctypes.string_at(sa.lpSockaddr + 4, 4))
                    if not ip.startswith(("169.254.", "127.")):
                        found.append(("Mobile hotspot" if ip.startswith(HOTSPOT_PREFIX) else name, ip))
                ua = ua.contents.Next
        adapter = a.Next
    return found


_proto(user32, "GetDisplayConfigBufferSizes", ctypes.c_long, wt.UINT, ctypes.POINTER(wt.UINT), ctypes.POINTER(wt.UINT))
_proto(user32, "QueryDisplayConfig", ctypes.c_long, wt.UINT, ctypes.POINTER(wt.UINT), ctypes.c_void_p,
       ctypes.POINTER(wt.UINT), ctypes.c_void_p, ctypes.POINTER(ctypes.c_int))
QDC_DATABASE_CURRENT = 0x4
_TOPOLOGIES = {1: "internal", 2: "clone", 4: "extend", 8: "external"}


def display_topology() -> str | None:
    """The Win+P setting: internal / clone (Duplicate) / extend / external, or None if unknown."""
    n_paths, n_modes = wt.UINT(), wt.UINT()
    if user32.GetDisplayConfigBufferSizes(QDC_DATABASE_CURRENT, ctypes.byref(n_paths), ctypes.byref(n_modes)):
        return None
    paths = (ctypes.c_byte * (72 * max(n_paths.value, 1)))()   # DISPLAYCONFIG_PATH_INFO
    modes = (ctypes.c_byte * (64 * max(n_modes.value, 1)))()   # DISPLAYCONFIG_MODE_INFO
    topology = ctypes.c_int()
    if user32.QueryDisplayConfig(QDC_DATABASE_CURRENT, ctypes.byref(n_paths), paths,
                                 ctypes.byref(n_modes), modes, ctypes.byref(topology)):
        return None
    return _TOPOLOGIES.get(topology.value)


# ---------------------------------------------------------------------------
# GDI screen capture (always available; DXGI via dxcam is faster when present)
# ---------------------------------------------------------------------------

class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG),
                ("biPlanes", wt.WORD), ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
                ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", wt.LONG), ("biYPelsPerMeter", wt.LONG),
                ("biClrUsed", wt.DWORD), ("biClrImportant", wt.DWORD)]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wt.DWORD * 3)]


class BITMAP(ctypes.Structure):
    _fields_ = [("bmType", wt.LONG), ("bmWidth", wt.LONG), ("bmHeight", wt.LONG),
                ("bmWidthBytes", wt.LONG), ("bmPlanes", wt.WORD), ("bmBitsPixel", wt.WORD),
                ("bmBits", ctypes.c_void_p)]


_proto(user32, "GetDC", wt.HDC, wt.HWND)
_proto(user32, "ReleaseDC", ctypes.c_int, wt.HWND, wt.HDC)
_proto(gdi32, "CreateCompatibleDC", wt.HDC, wt.HDC)
_proto(gdi32, "CreateDIBSection", wt.HBITMAP, wt.HDC, ctypes.POINTER(BITMAPINFO), wt.UINT,
       ctypes.POINTER(ctypes.c_void_p), wt.HANDLE, wt.DWORD)
_proto(gdi32, "SelectObject", wt.HGDIOBJ, wt.HDC, wt.HGDIOBJ)
_proto(gdi32, "BitBlt", wt.BOOL, wt.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
       wt.HDC, ctypes.c_int, ctypes.c_int, wt.DWORD)
_proto(gdi32, "DeleteObject", wt.BOOL, wt.HGDIOBJ)
_proto(gdi32, "DeleteDC", wt.BOOL, wt.HDC)
_proto(gdi32, "GdiFlush", wt.BOOL)
_proto(gdi32, "GetObjectW", ctypes.c_int, wt.HANDLE, ctypes.c_int, ctypes.c_void_p)
_proto(gdi32, "GetDIBits", ctypes.c_int, wt.HDC, wt.HBITMAP, wt.UINT, wt.UINT, ctypes.c_void_p,
       ctypes.POINTER(BITMAPINFO), wt.UINT)

SRCCOPY = 0x00CC0020


def _bitmapinfo(width: int, height: int) -> BITMAPINFO:
    bmi = BITMAPINFO()
    h = bmi.bmiHeader
    h.biSize, h.biWidth, h.biHeight = ctypes.sizeof(BITMAPINFOHEADER), width, -height  # top-down
    h.biPlanes, h.biBitCount, h.biCompression = 1, 32, 0  # BI_RGB
    return bmi


class GdiCapture:
    """BitBlt a screen rectangle into a DIB section. Returns a fresh BGRX array per grab."""

    name = "GDI"
    waits = False

    def __init__(self, rect):
        self.rect = rect
        self.left, self.top, self.width, self.height = rect
        self.screen_dc = user32.GetDC(None)
        self.mem_dc = gdi32.CreateCompatibleDC(self.screen_dc)
        bits = ctypes.c_void_p()
        self.bitmap = gdi32.CreateDIBSection(self.mem_dc, ctypes.byref(_bitmapinfo(self.width, self.height)),
                                             0, ctypes.byref(bits), None, 0)
        if not self.bitmap:
            self.close()
            raise OSError(f"CreateDIBSection failed ({ctypes.get_last_error()})")
        self.old = gdi32.SelectObject(self.mem_dc, self.bitmap)
        size = self.width * self.height * 4
        self.pixels = np.ctypeslib.as_array((ctypes.c_uint8 * size).from_address(bits.value)) \
            .reshape(self.height, self.width, 4)

    def grab(self) -> np.ndarray:
        if not gdi32.BitBlt(self.mem_dc, 0, 0, self.width, self.height,
                            self.screen_dc, self.left, self.top, SRCCOPY):
            raise OSError(f"BitBlt failed ({ctypes.get_last_error()})")
        gdi32.GdiFlush()
        self.shown_at = time.perf_counter()
        return self.pixels.copy()

    def close(self) -> None:
        if getattr(self, "bitmap", None):
            gdi32.SelectObject(self.mem_dc, self.old)
            gdi32.DeleteObject(self.bitmap)
            self.bitmap = None
        if getattr(self, "mem_dc", None):
            gdi32.DeleteDC(self.mem_dc)
            self.mem_dc = None
        if getattr(self, "screen_dc", None):
            user32.ReleaseDC(None, self.screen_dc)
            self.screen_dc = None


# ---------------------------------------------------------------------------
# Cursor
# ---------------------------------------------------------------------------

class CURSORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("flags", wt.DWORD), ("hCursor", wt.HANDLE), ("ptScreenPos", wt.POINT)]


class ICONINFO(ctypes.Structure):
    _fields_ = [("fIcon", wt.BOOL), ("xHotspot", wt.DWORD), ("yHotspot", wt.DWORD),
                ("hbmMask", wt.HBITMAP), ("hbmColor", wt.HBITMAP)]


_proto(user32, "GetCursorInfo", wt.BOOL, ctypes.POINTER(CURSORINFO))
_proto(user32, "GetIconInfo", wt.BOOL, wt.HANDLE, ctypes.POINTER(ICONINFO))

CURSOR_SHOWING = 1


def get_cursor():
    """Return (x, y, showing, hcursor) in physical screen pixels, or None."""
    ci = CURSORINFO(cbSize=ctypes.sizeof(CURSORINFO))
    if not user32.GetCursorInfo(ctypes.byref(ci)):
        return None
    return ci.ptScreenPos.x, ci.ptScreenPos.y, bool(ci.flags & CURSOR_SHOWING), ci.hCursor or 0


def _bitmap_pixels(hdc, hbitmap) -> np.ndarray:
    bm = BITMAP()
    gdi32.GetObjectW(hbitmap, ctypes.sizeof(BITMAP), ctypes.byref(bm))
    w, h = bm.bmWidth, bm.bmHeight
    buf = (ctypes.c_uint8 * (w * h * 4))()
    if not gdi32.GetDIBits(hdc, hbitmap, 0, h, buf, ctypes.byref(_bitmapinfo(w, h)), 0):
        raise OSError("GetDIBits failed")
    return np.frombuffer(buf, np.uint8).reshape(h, w, 4).copy()


def cursor_image(hcursor):
    """Render a cursor handle to (rgba array, hotspot_x, hotspot_y, has_inverted_pixels)."""
    ii = ICONINFO()
    if not user32.GetIconInfo(hcursor, ctypes.byref(ii)):
        return None
    hdc = user32.GetDC(None)
    try:
        mask = _bitmap_pixels(hdc, ii.hbmMask)[..., 0] != 0
        if ii.hbmColor:
            color = _bitmap_pixels(hdc, ii.hbmColor)
            h = color.shape[0]
            rgba = color[..., [2, 1, 0, 3]].copy()
            inverted = np.zeros(rgba.shape[:2], bool)
            if not rgba[..., 3].any():  # no alpha channel: transparency comes from the AND mask
                transparent = mask[:h]
                inverted = transparent & color[..., :3].any(axis=2)
                rgba[..., 3] = np.where(transparent, 0, 255)
        else:  # monochrome: AND mask on top, XOR mask below
            h = mask.shape[0] // 2
            and_bits, xor_bits = mask[:h], mask[h:]
            rgba = np.zeros((h, mask.shape[1], 4), np.uint8)
            rgba[~and_bits & xor_bits] = (255, 255, 255, 255)
            rgba[~and_bits & ~xor_bits] = (0, 0, 0, 255)
            inverted = and_bits & xor_bits
        # Screen-inverting pixels can't be reproduced in an overlay; draw them black
        # and let the client add a light outline so they stay visible on dark areas.
        rgba[inverted] = (0, 0, 0, 255)
        return rgba, ii.xHotspot, ii.yHotspot, bool(inverted.any())
    finally:
        user32.ReleaseDC(None, hdc)
        gdi32.DeleteObject(ii.hbmMask)
        if ii.hbmColor:
            gdi32.DeleteObject(ii.hbmColor)


# ---------------------------------------------------------------------------
# Input injection
# ---------------------------------------------------------------------------

ULONG_PTR = ctypes.c_size_t


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wt.LONG), ("dy", wt.LONG), ("mouseData", wt.DWORD), ("dwFlags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ULONG_PTR)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD), ("time", wt.DWORD),
                ("dwExtraInfo", ULONG_PTR)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wt.DWORD), ("wParamL", wt.WORD), ("wParamH", wt.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wt.DWORD), ("u", _INPUTUNION)]


class POINTER_INFO(ctypes.Structure):
    _fields_ = [("pointerType", wt.DWORD), ("pointerId", wt.UINT), ("frameId", wt.UINT),
                ("pointerFlags", wt.UINT), ("sourceDevice", wt.HANDLE), ("hwndTarget", wt.HWND),
                ("ptPixelLocation", wt.POINT), ("ptHimetricLocation", wt.POINT),
                ("ptPixelLocationRaw", wt.POINT), ("ptHimetricLocationRaw", wt.POINT),
                ("dwTime", wt.DWORD), ("historyCount", wt.UINT), ("InputData", ctypes.c_int32),
                ("dwKeyStates", wt.DWORD), ("PerformanceCount", ctypes.c_uint64),
                ("ButtonChangeType", ctypes.c_int)]


class POINTER_TOUCH_INFO(ctypes.Structure):
    _fields_ = [("pointerInfo", POINTER_INFO), ("touchFlags", wt.UINT), ("touchMask", wt.UINT),
                ("rcContact", wt.RECT), ("rcContactRaw", wt.RECT), ("orientation", wt.UINT),
                ("pressure", wt.UINT)]


class POINTER_PEN_INFO(ctypes.Structure):
    _fields_ = [("pointerInfo", POINTER_INFO), ("penFlags", wt.UINT), ("penMask", wt.UINT),
                ("pressure", wt.UINT), ("rotation", wt.UINT), ("tiltX", ctypes.c_int32),
                ("tiltY", ctypes.c_int32)]


class _POINTER_TYPE_UNION(ctypes.Union):
    _fields_ = [("touchInfo", POINTER_TOUCH_INFO), ("penInfo", POINTER_PEN_INFO)]


class POINTER_TYPE_INFO(ctypes.Structure):
    _fields_ = [("type", wt.DWORD), ("u", _POINTER_TYPE_UNION)]


# Layout must match the C headers exactly or injection silently misbehaves.
assert ctypes.sizeof(INPUT) == (40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28)
if ctypes.sizeof(ctypes.c_void_p) == 8:
    assert ctypes.sizeof(POINTER_INFO) == 96
    assert ctypes.sizeof(POINTER_TOUCH_INFO) == 144
    assert ctypes.sizeof(POINTER_PEN_INFO) == 120
    assert ctypes.sizeof(POINTER_TYPE_INFO) == 152

_proto(user32, "SendInput", wt.UINT, wt.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
_proto(user32, "GetSystemMetrics", ctypes.c_int, ctypes.c_int)
try:
    _proto(user32, "CreateSyntheticPointerDevice", wt.HANDLE, wt.DWORD, wt.ULONG, wt.DWORD)
    _proto(user32, "InjectSyntheticPointerInput", wt.BOOL, wt.HANDLE, ctypes.POINTER(POINTER_TYPE_INFO), wt.UINT)
    _proto(user32, "DestroySyntheticPointerDevice", None, wt.HANDLE)
    HAVE_SYNTHETIC_POINTER = True
except AttributeError:  # older than Windows 10 1809
    HAVE_SYNTHETIC_POINTER = False

PT_TOUCH, PT_PEN = 2, 3
POINTER_FEEDBACK_DEFAULT = 1
PF_INRANGE, PF_INCONTACT, PF_FIRSTBUTTON = 0x2, 0x4, 0x10
PF_PRIMARY, PF_CANCELED = 0x2000, 0x8000
PF_DOWN, PF_UPDATE, PF_UP = 0x10000, 0x20000, 0x40000
CHANGE_FIRSTBUTTON_DOWN, CHANGE_FIRSTBUTTON_UP = 1, 2
TOUCH_MASK_CONTACTAREA, TOUCH_MASK_ORIENTATION, TOUCH_MASK_PRESSURE = 1, 2, 4
PEN_FLAG_BARREL, PEN_FLAG_ERASER = 1, 4
PEN_MASK_PRESSURE, PEN_MASK_TILT_X, PEN_MASK_TILT_Y = 1, 4, 8

INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
MOUSEEVENTF_MOVE, MOUSEEVENTF_ABSOLUTE, MOUSEEVENTF_VIRTUALDESK = 0x0001, 0x8000, 0x4000
MOUSEEVENTF_WHEEL, MOUSEEVENTF_HWHEEL = 0x0800, 0x1000
MOUSE_BUTTONS = ((1, 0x0002, 0x0004), (2, 0x0008, 0x0010), (4, 0x0020, 0x0040))  # bit, down, up
KEYEVENTF_EXTENDEDKEY, KEYEVENTF_KEYUP, KEYEVENTF_SCANCODE = 0x1, 0x2, 0x8
WHEEL_PER_PIXEL = 1.2      # browsers report ~100 px per wheel notch; Windows uses 120 units
WHEEL_PER_LINE = 40

# KeyboardEvent.code -> (set-1 scan code, extended?). Physical keys, so Windows'
# own keyboard layout and IME (e.g. Korean) decide which character comes out.
_SCAN = {
    "Escape": 0x01, "Minus": 0x0C, "Equal": 0x0D, "Backspace": 0x0E, "Tab": 0x0F,
    "BracketLeft": 0x1A, "BracketRight": 0x1B, "Enter": 0x1C, "ControlLeft": 0x1D,
    "Semicolon": 0x27, "Quote": 0x28, "Backquote": 0x29, "ShiftLeft": 0x2A, "Backslash": 0x2B,
    "Comma": 0x33, "Period": 0x34, "Slash": 0x35, "ShiftRight": 0x36, "NumpadMultiply": 0x37,
    "AltLeft": 0x38, "Space": 0x39, "CapsLock": 0x3A, "ScrollLock": 0x46,
    "Numpad7": 0x47, "Numpad8": 0x48, "Numpad9": 0x49, "NumpadSubtract": 0x4A, "Numpad4": 0x4B,
    "Numpad5": 0x4C, "Numpad6": 0x4D, "NumpadAdd": 0x4E, "Numpad1": 0x4F, "Numpad2": 0x50,
    "Numpad3": 0x51, "Numpad0": 0x52, "NumpadDecimal": 0x53, "IntlBackslash": 0x56,
    "F11": 0x57, "F12": 0x58, "Lang2": 0x71, "Lang1": 0x72,
}
_SCAN.update({f"Digit{d}": 0x02 + (d - 1) for d in range(1, 10)} | {"Digit0": 0x0B})
_SCAN.update({f"F{n}": 0x3B + n - 1 for n in range(1, 11)})
_SCAN.update({f"Key{c}": s for row, start in (("QWERTYUIOP", 0x10), ("ASDFGHJKL", 0x1E), ("ZXCVBNM", 0x2C))
              for s, c in enumerate(row, start)})
KEY_CODES = {code: (scan, False) for code, scan in _SCAN.items()}
KEY_CODES.update({code: (scan, True) for code, scan in {
    "NumpadEnter": 0x1C, "ControlRight": 0x1D, "NumpadDivide": 0x35, "PrintScreen": 0x37,
    "AltRight": 0x38, "NumLock": 0x45, "Home": 0x47, "ArrowUp": 0x48, "PageUp": 0x49,
    "ArrowLeft": 0x4B, "ArrowRight": 0x4D, "End": 0x4F, "ArrowDown": 0x50, "PageDown": 0x51,
    "Insert": 0x52, "Delete": 0x53, "MetaLeft": 0x5B, "MetaRight": 0x5C, "ContextMenu": 0x5D,
}.items()})


def _send_inputs(inputs: list[INPUT]) -> None:
    if inputs:
        arr = (INPUT * len(inputs))(*inputs)
        user32.SendInput(len(inputs), arr, ctypes.sizeof(INPUT))


def _mouse_input(flags: int, dx: int = 0, dy: int = 0, data: int = 0) -> INPUT:
    inp = INPUT(type=INPUT_MOUSE)
    inp.u.mi = MOUSEINPUT(dx, dy, data & 0xFFFFFFFF, flags, 0, 0)
    return inp


def _absolute_move(x: int, y: int) -> INPUT:
    vx, vy = user32.GetSystemMetrics(76), user32.GetSystemMetrics(77)
    vw, vh = max(user32.GetSystemMetrics(78), 2), max(user32.GetSystemMetrics(79), 2)
    nx = round((x - vx) * 65535 / (vw - 1))
    ny = round((y - vy) * 65535 / (vh - 1))
    return _mouse_input(MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK, nx, ny)


class _Contact:
    __slots__ = ("slot", "x", "y", "state", "primary")

    def __init__(self, slot, x, y, primary):
        self.slot, self.x, self.y, self.state, self.primary = slot, x, y, "down", primary


class InputInjector:
    """Turns the iPad's pointer/keyboard events into Windows input.

    Touch and Apple Pencil go through synthetic pointer devices so Windows sees a
    real touchscreen and pen (native scrolling, press-and-hold, pressure, tilt).
    Anything that can't be injected that way falls back to plain mouse input.
    All methods are called from the asyncio thread.
    """

    MAX_TOUCHES = 10
    KEEPALIVE = 0.05   # Windows cancels injected contacts that go quiet for too long

    def __init__(self):
        self.touch_dev = self.pen_dev = None
        if HAVE_SYNTHETIC_POINTER:
            self.touch_dev = user32.CreateSyntheticPointerDevice(PT_TOUCH, self.MAX_TOUCHES,
                                                                 POINTER_FEEDBACK_DEFAULT) or None
            self.pen_dev = user32.CreateSyntheticPointerDevice(PT_PEN, 1, POINTER_FEEDBACK_DEFAULT) or None
        self.contacts: dict[tuple, _Contact] = {}
        self.last_touch = 0.0
        self.pen_state = None          # None | "hover" | "contact"
        self.pen_owner = None
        self.pen_last = None           # (x, y, pressure, tilt_x, tilt_y, flags) of last injection
        self.pen_time = 0.0
        self.mouse_buttons: dict[object, int] = {}
        self.fallback_touch: dict[object, object] = {}   # owner -> pointer id driving the mouse
        self.keys_down: dict[object, set[str]] = {}
        self.wheel_acc = [0.0, 0.0]
        self._warned = set()

    @property
    def caps(self) -> dict:
        return {"touch": bool(self.touch_dev), "pen": bool(self.pen_dev), "mouse": True, "keyboard": True}

    def _warn_once(self, what: str) -> None:
        if what not in self._warned:
            self._warned.add(what)
            log.warning("%s injection failed (error %d); some input may be ignored",
                        what, ctypes.get_last_error())

    # ---- dispatch -------------------------------------------------------

    def pointer(self, owner, kind: str, ptype: str, pid, x: int, y: int,
                pressure: float = 0.0, tilt_x: int = 0, tilt_y: int = 0, buttons: int = 0) -> None:
        """kind: d(own) m(ove) u(p) c(ancel) l(eave); ptype: t(ouch) p(en) m(ouse)."""
        if ptype == "t":
            if self.touch_dev:
                self._touch(owner, kind, pid, x, y)
            else:
                self._touch_as_mouse(owner, kind, pid, x, y)
        elif ptype == "p":
            if self.pen_dev:
                self._pen(owner, kind, x, y, pressure, tilt_x, tilt_y, buttons)
            elif kind != "l":
                self._mouse(owner, x, y, buttons & 1 if kind in ("d", "m") else 0)
        else:
            self._mouse(owner, x, y, buttons)

    # ---- touch ----------------------------------------------------------

    def _touch(self, owner, kind, pid, x, y):
        key = (owner, pid)
        c = self.contacts.get(key)
        if kind == "d" and c is None:
            used = {k.slot for k in self.contacts.values()}
            slot = next((i for i in range(self.MAX_TOUCHES) if i not in used), None)
            if slot is None:
                return
            self.contacts[key] = _Contact(slot, x, y, primary=not self.contacts)
        elif c is None:
            return
        elif kind in ("m", "d"):
            c.x, c.y = x, y
        elif kind == "u":
            c.x, c.y, c.state = x, y, "up"
        elif kind == "c":
            c.state = "cancel"
        else:
            return
        self._inject_touch_frame()

    def _inject_touch_frame(self):
        contacts = list(self.contacts.values())
        if not contacts:
            return
        frame = (POINTER_TYPE_INFO * len(contacts))()
        for item, c in zip(frame, contacts):
            item.type = PT_TOUCH
            ti = item.u.touchInfo
            pi = ti.pointerInfo
            pi.pointerType, pi.pointerId = PT_TOUCH, c.slot
            pi.ptPixelLocation = wt.POINT(c.x, c.y)
            if c.state == "down":
                flags = PF_DOWN | PF_INRANGE | PF_INCONTACT | PF_FIRSTBUTTON
                pi.ButtonChangeType = CHANGE_FIRSTBUTTON_DOWN
            elif c.state == "update":
                flags = PF_UPDATE | PF_INRANGE | PF_INCONTACT | PF_FIRSTBUTTON
            else:
                flags = PF_UP | (PF_CANCELED if c.state == "cancel" else 0)
                pi.ButtonChangeType = CHANGE_FIRSTBUTTON_UP
            pi.pointerFlags = flags | (PF_PRIMARY if c.primary else 0)
            ti.touchMask = TOUCH_MASK_CONTACTAREA | TOUCH_MASK_ORIENTATION | TOUCH_MASK_PRESSURE
            ti.rcContact = wt.RECT(c.x - 2, c.y - 2, c.x + 2, c.y + 2)
            ti.orientation, ti.pressure = 90, 512
        if not user32.InjectSyntheticPointerInput(self.touch_dev, frame, len(contacts)):
            self._warn_once("Touch")
        self.last_touch = time.monotonic()
        self.contacts = {k: c for k, c in self.contacts.items() if c.state in ("down", "update")}
        for c in self.contacts.values():
            c.state = "update"

    def _touch_as_mouse(self, owner, kind, pid, x, y):
        driver = self.fallback_touch.get(owner)
        if kind == "d" and driver is None:
            self.fallback_touch[owner] = driver = pid
        if pid != driver:
            return
        if kind in ("u", "c"):
            del self.fallback_touch[owner]
        self._mouse(owner, x, y, 1 if kind in ("d", "m") else 0)

    # ---- pen ------------------------------------------------------------

    def _pen(self, owner, kind, x, y, pressure, tilt_x, tilt_y, buttons):
        state = self.pen_state
        pen_flags = (PEN_FLAG_BARREL if buttons & 2 else 0) | (PEN_FLAG_ERASER if buttons & 32 else 0)
        if kind == "d":
            if state == "contact":
                kind = "m"
            else:
                self._inject_pen(x, y, pressure, tilt_x, tilt_y, pen_flags,
                                 PF_DOWN | PF_INRANGE | PF_INCONTACT | PF_FIRSTBUTTON, CHANGE_FIRSTBUTTON_DOWN)
                self.pen_state, self.pen_owner = "contact", owner
                return
        if kind == "m":
            if state == "contact":
                self._inject_pen(x, y, pressure, tilt_x, tilt_y, pen_flags,
                                 PF_UPDATE | PF_INRANGE | PF_INCONTACT | PF_FIRSTBUTTON)
            else:
                self._inject_pen(x, y, 0.0, tilt_x, tilt_y, pen_flags, PF_UPDATE | PF_INRANGE)
                self.pen_state, self.pen_owner = "hover", owner
        elif kind in ("u", "c"):
            if state == "contact":
                self._inject_pen(x, y, 0.0, tilt_x, tilt_y, pen_flags,
                                 PF_UP | PF_INRANGE | (PF_CANCELED if kind == "c" else 0), CHANGE_FIRSTBUTTON_UP)
                self.pen_state = "hover"
        elif kind == "l":
            self._pen_leave()

    def _pen_leave(self):
        if self.pen_state == "contact":
            x, y, *_ = self.pen_last
            self._inject_pen(x, y, 0.0, 0, 0, 0, PF_UP | PF_INRANGE | PF_CANCELED, CHANGE_FIRSTBUTTON_UP)
        if self.pen_state is not None:
            x, y, *_ = self.pen_last
            self._inject_pen(x, y, 0.0, 0, 0, 0, PF_UPDATE)  # out of range
        self.pen_state = self.pen_owner = None

    def _inject_pen(self, x, y, pressure, tilt_x, tilt_y, pen_flags, flags, change=0):
        info = POINTER_TYPE_INFO(type=PT_PEN)
        pen = info.u.penInfo
        pi = pen.pointerInfo
        pi.pointerType, pi.pointerId = PT_PEN, 0
        pi.pointerFlags = flags | PF_PRIMARY
        pi.ptPixelLocation = wt.POINT(x, y)
        pi.ButtonChangeType = change
        pen.penFlags = pen_flags
        pen.penMask = PEN_MASK_PRESSURE | PEN_MASK_TILT_X | PEN_MASK_TILT_Y
        in_contact = bool(flags & PF_INCONTACT)
        pen.pressure = max(1, min(1024, round(pressure * 1024))) if in_contact else 0
        pen.tiltX = max(-90, min(90, int(tilt_x)))
        pen.tiltY = max(-90, min(90, int(tilt_y)))
        if not user32.InjectSyntheticPointerInput(self.pen_dev, ctypes.byref(info), 1):
            self._warn_once("Pen")
        self.pen_last = (x, y, pressure, tilt_x, tilt_y, pen_flags)
        self.pen_time = time.monotonic()

    # ---- mouse / wheel / keyboard ----------------------------------------

    def _mouse(self, owner, x, y, buttons):
        prev = self.mouse_buttons.get(owner, 0)
        flags = 0
        for bit, down, up in MOUSE_BUTTONS:
            if (buttons ^ prev) & bit:
                flags |= down if buttons & bit else up
        move = _absolute_move(x, y)
        move.u.mi.dwFlags |= flags
        _send_inputs([move])
        self.mouse_buttons[owner] = buttons

    def wheel(self, x: int, y: int, dx: float, dy: float, mode: int) -> None:
        per = WHEEL_PER_PIXEL if mode == 0 else WHEEL_PER_LINE if mode == 1 else 120
        self.wheel_acc[0] += dx * per
        self.wheel_acc[1] -= dy * per
        inputs = [_absolute_move(x, y)]
        for i, flag in ((1, MOUSEEVENTF_WHEEL), (0, MOUSEEVENTF_HWHEEL)):
            amount = int(self.wheel_acc[i])
            if amount:
                self.wheel_acc[i] -= amount
                inputs.append(_mouse_input(flag, data=amount))
        if len(inputs) > 1:
            _send_inputs(inputs)

    def key(self, owner, code: str, down: bool) -> None:
        entry = KEY_CODES.get(code)
        if entry is None:
            return
        held = self.keys_down.setdefault(owner, set())
        if not down and code not in held:
            return
        (held.add if down else held.discard)(code)
        scan, extended = entry
        flags = KEYEVENTF_SCANCODE | (KEYEVENTF_EXTENDEDKEY if extended else 0) | (0 if down else KEYEVENTF_KEYUP)
        inp = INPUT(type=INPUT_KEYBOARD)
        inp.u.ki = KEYBDINPUT(0, scan, flags, 0, 0)
        _send_inputs([inp])

    # ---- housekeeping -----------------------------------------------------

    def keepalive(self) -> None:
        now = time.monotonic()
        if self.contacts and now - self.last_touch >= self.KEEPALIVE:
            self._inject_touch_frame()
        if self.pen_state == "contact" and now - self.pen_time >= self.KEEPALIVE:
            x, y, pressure, tx, ty, pf = self.pen_last
            self._inject_pen(x, y, pressure, tx, ty, pf, PF_UPDATE | PF_INRANGE | PF_INCONTACT | PF_FIRSTBUTTON)
        elif self.pen_state == "hover" and now - self.pen_time >= 1.0:
            self._pen_leave()   # hover events stopped without a leave event

    def release(self, owner) -> None:
        """Lift every finger, pen, button and key a disconnected client left down."""
        for key, c in self.contacts.items():
            if key[0] == owner:
                c.state = "cancel"
        self._inject_touch_frame()
        if self.pen_owner == owner:
            self._pen_leave()
        if self.mouse_buttons.get(owner):
            x, y, *_ = get_cursor() or (0, 0)
            self._mouse(owner, x, y, 0)
        self.mouse_buttons.pop(owner, None)
        self.fallback_touch.pop(owner, None)
        for code in list(self.keys_down.get(owner, ())):
            self.key(owner, code, False)
        self.keys_down.pop(owner, None)

    def close(self) -> None:
        for dev in (self.touch_dev, self.pen_dev):
            if dev:
                user32.DestroySyntheticPointerDevice(dev)
        self.touch_dev = self.pen_dev = None


# ---------------------------------------------------------------------------
# Clipboard file lists and File Explorer (moving files between the iPad and the PC)
# ---------------------------------------------------------------------------

shell32 = ctypes.WinDLL("shell32", use_last_error=True)
_proto(user32, "OpenClipboard", wt.BOOL, wt.HWND)
_proto(user32, "CloseClipboard", wt.BOOL)
_proto(user32, "EmptyClipboard", wt.BOOL)
_proto(user32, "IsClipboardFormatAvailable", wt.BOOL, wt.UINT)
_proto(user32, "GetClipboardData", wt.HANDLE, wt.UINT)
_proto(user32, "SetClipboardData", wt.HANDLE, wt.UINT, wt.HANDLE)
_proto(user32, "RegisterClipboardFormatW", wt.UINT, wt.LPCWSTR)
_proto(user32, "CreateWindowExW", wt.HWND, wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD, ctypes.c_int,
       ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID)
_proto(user32, "DestroyWindow", wt.BOOL, wt.HWND)
_proto(kernel32, "GlobalAlloc", wt.HGLOBAL, wt.UINT, ctypes.c_size_t)
_proto(kernel32, "GlobalLock", ctypes.c_void_p, wt.HGLOBAL)
_proto(kernel32, "GlobalUnlock", wt.BOOL, wt.HGLOBAL)
_proto(kernel32, "GlobalFree", wt.HGLOBAL, wt.HGLOBAL)
_proto(shell32, "DragQueryFileW", wt.UINT, wt.HANDLE, wt.UINT, wt.LPWSTR, wt.UINT)
CF_HDROP = 15
GMEM_MOVEABLE = 0x0002
HWND_MESSAGE = wt.HWND(-3)
DROPEFFECT_COPY = 1


class _OpenClipboard:
    """Open the clipboard for `owner`, retrying briefly while another app holds it."""

    def __init__(self, owner=None):
        self.owner = owner

    def __enter__(self):
        for _ in range(40):
            if user32.OpenClipboard(self.owner):
                return self
            time.sleep(0.025)
        raise OSError("the clipboard is busy; try again")

    def __exit__(self, *exc):
        user32.CloseClipboard()


def get_clipboard_files() -> list[str]:
    """Paths of the files currently copied (Ctrl+C in File Explorer), or []."""
    with _OpenClipboard():
        if not user32.IsClipboardFormatAvailable(CF_HDROP):
            return []
        hdrop = user32.GetClipboardData(CF_HDROP)
        if not hdrop:
            return []
        paths = []
        for i in range(shell32.DragQueryFileW(hdrop, 0xFFFFFFFF, None, 0)):
            length = shell32.DragQueryFileW(hdrop, i, None, 0)
            buf = ctypes.create_unicode_buffer(length + 1)
            shell32.DragQueryFileW(hdrop, i, buf, length + 1)
            paths.append(buf.value)
        return paths


def _global_bytes(data: bytes):
    handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
    if not handle:
        raise MemoryError("GlobalAlloc failed")
    ctypes.memmove(kernel32.GlobalLock(handle), data, len(data))
    kernel32.GlobalUnlock(handle)
    return handle


def set_clipboard_files(paths) -> None:
    """Put files on the clipboard as if they were copied in File Explorer, so Ctrl+V pastes them."""
    names = "".join(f"{p}\0" for p in paths) + "\0"
    dropfiles = struct.pack("<IiiII", 20, 0, 0, 0, 1)    # DROPFILES: pFiles, pt.x, pt.y, fNC, fWide
    # With no owner window, EmptyClipboard leaves the clipboard ownerless and SetClipboardData
    # may fail, so own it with a hidden message-only window for the duration.
    owner = user32.CreateWindowExW(0, "STATIC", None, 0, 0, 0, 0, 0, HWND_MESSAGE, None, None, None)
    try:
        with _OpenClipboard(owner):
            user32.EmptyClipboard()
            files = _global_bytes(dropfiles + names.encode("utf-16-le"))
            if not user32.SetClipboardData(CF_HDROP, files):
                kernel32.GlobalFree(files)
                raise OSError(f"SetClipboardData failed ({ctypes.get_last_error()})")
            effect = _global_bytes(struct.pack("<I", DROPEFFECT_COPY))
            if not user32.SetClipboardData(user32.RegisterClipboardFormatW("Preferred DropEffect"), effect):
                kernel32.GlobalFree(effect)
    finally:
        if owner:
            user32.DestroyWindow(owner)


def show_in_explorer(path) -> None:
    """Open File Explorer with `path` selected."""
    subprocess.Popen(f'explorer.exe /select,"{path}"')


# ---------------------------------------------------------------------------
# "Drop files here to send them to the iPad" box
# ---------------------------------------------------------------------------

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [("cbSize", wt.UINT), ("style", wt.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int), ("hInstance", wt.HINSTANCE), ("hIcon", wt.HICON),
                ("hCursor", wt.HANDLE), ("hbrBackground", wt.HBRUSH), ("lpszMenuName", wt.LPCWSTR),
                ("lpszClassName", wt.LPCWSTR), ("hIconSm", wt.HICON)]


class PAINTSTRUCT(ctypes.Structure):
    _fields_ = [("hdc", wt.HDC), ("fErase", wt.BOOL), ("rcPaint", wt.RECT), ("fRestore", wt.BOOL),
                ("fIncUpdate", wt.BOOL), ("rgbReserved", ctypes.c_byte * 32)]


_proto(user32, "RegisterClassExW", wt.ATOM, ctypes.POINTER(WNDCLASSEXW))
_proto(user32, "DefWindowProcW", LRESULT, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)
_proto(user32, "GetMessageW", wt.BOOL, ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT)
_proto(user32, "TranslateMessage", wt.BOOL, ctypes.POINTER(wt.MSG))
_proto(user32, "DispatchMessageW", LRESULT, ctypes.POINTER(wt.MSG))
_proto(user32, "PostMessageW", wt.BOOL, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)
_proto(user32, "PostQuitMessage", None, ctypes.c_int)
_proto(user32, "SetWindowPos", wt.BOOL, wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int,
       ctypes.c_int, wt.UINT)
_proto(user32, "ShowWindow", wt.BOOL, wt.HWND, ctypes.c_int)
_proto(user32, "IsWindowVisible", wt.BOOL, wt.HWND)
_proto(user32, "SetLayeredWindowAttributes", wt.BOOL, wt.HWND, wt.COLORREF, wt.BYTE, wt.DWORD)
_proto(user32, "SetWindowRgn", ctypes.c_int, wt.HWND, wt.HRGN, wt.BOOL)
_proto(user32, "BeginPaint", wt.HDC, wt.HWND, ctypes.POINTER(PAINTSTRUCT))
_proto(user32, "EndPaint", wt.BOOL, wt.HWND, ctypes.POINTER(PAINTSTRUCT))
_proto(user32, "InvalidateRect", wt.BOOL, wt.HWND, ctypes.c_void_p, wt.BOOL)
_proto(user32, "SetTimer", ctypes.c_size_t, wt.HWND, ctypes.c_size_t, wt.UINT, ctypes.c_void_p)
_proto(user32, "KillTimer", wt.BOOL, wt.HWND, ctypes.c_size_t)
_proto(user32, "GetClientRect", wt.BOOL, wt.HWND, ctypes.POINTER(wt.RECT))
_proto(user32, "FillRect", ctypes.c_int, wt.HDC, ctypes.POINTER(wt.RECT), wt.HBRUSH)
_proto(user32, "DrawTextW", ctypes.c_int, wt.HDC, wt.LPCWSTR, ctypes.c_int, ctypes.POINTER(wt.RECT), wt.UINT)
_proto(user32, "LoadCursorW", wt.HANDLE, wt.HINSTANCE, ctypes.c_void_p)
_proto(user32, "MonitorFromPoint", wt.HMONITOR, wt.POINT, wt.DWORD)
_proto(user32, "ChangeWindowMessageFilterEx", wt.BOOL, wt.HWND, wt.UINT, wt.DWORD, ctypes.c_void_p)
_proto(gdi32, "CreateSolidBrush", wt.HBRUSH, wt.COLORREF)
_proto(gdi32, "CreatePen", wt.HPEN, ctypes.c_int, ctypes.c_int, wt.COLORREF)
_proto(gdi32, "CreateRoundRectRgn", wt.HRGN, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
       ctypes.c_int, ctypes.c_int)
_proto(gdi32, "RoundRect", wt.BOOL, wt.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
       ctypes.c_int, ctypes.c_int)
_proto(gdi32, "CreateFontW", wt.HFONT, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
       wt.DWORD, wt.DWORD, wt.DWORD, wt.DWORD, wt.DWORD, wt.DWORD, wt.DWORD, wt.DWORD, wt.LPCWSTR)
_proto(gdi32, "SetBkMode", ctypes.c_int, wt.HDC, ctypes.c_int)
_proto(gdi32, "SetTextColor", wt.COLORREF, wt.HDC, wt.COLORREF)
_proto(gdi32, "GetStockObject", wt.HGDIOBJ, ctypes.c_int)
_proto(shell32, "DragFinish", None, wt.HANDLE)
_proto(kernel32, "GetModuleHandleW", wt.HMODULE, wt.LPCWSTR)

WS_POPUP = 0x80000000
WS_EX_TOPMOST, WS_EX_TOOLWINDOW, WS_EX_ACCEPTFILES = 0x8, 0x80, 0x10
WS_EX_LAYERED, WS_EX_NOACTIVATE = 0x80000, 0x08000000
WM_DESTROY, WM_CLOSE, WM_PAINT, WM_TIMER = 0x0002, 0x0010, 0x000F, 0x0113
WM_NCHITTEST, WM_MOUSEACTIVATE, WM_DROPFILES = 0x0084, 0x0021, 0x0233
WM_COPYDATA, WM_COPYGLOBALDATA = 0x004A, 0x0049
HTCAPTION, MA_NOACTIVATE, MSGFLT_ALLOW, LWA_ALPHA = 2, 3, 1, 0x2
SWP_NOACTIVATE, SWP_SHOWWINDOW, HWND_TOPMOST = 0x0010, 0x0040, wt.HWND(-1)
SW_HIDE, SW_SHOWNOACTIVATE = 0, 4
DT_CENTER, DT_VCENTER, DT_SINGLELINE, DT_END_ELLIPSIS = 0x1, 0x4, 0x20, 0x8000
MONITOR_DEFAULTTONEAREST = 2


def _rgb(r: int, g: int, b: int) -> int:
    return r | (g << 8) | (b << 16)


def work_area_at(x: int, y: int):
    """(left, top, right, bottom, dpi) of the work area (screen minus taskbar) of the monitor at x, y."""
    hmon = user32.MonitorFromPoint(wt.POINT(x, y), MONITOR_DEFAULTTONEAREST)
    mi = MONITORINFOEXW(cbSize=ctypes.sizeof(MONITORINFOEXW))
    user32.GetMonitorInfoW(hmon, ctypes.byref(mi))
    r = mi.rcWork
    return r.left, r.top, r.right, r.bottom, _monitor_dpi(hmon)


class DropBox:
    """A small always-on-top box on the iPad's screen; files dropped on it are sent to the iPad.

    `get_rect()` returns the (left, top, width, height) of the display to sit on, or None to
    hide the box (e.g. while no iPad is connected). `on_drop(paths)` handles a drop and returns
    a short message to show in the box for a moment. Runs its own window thread.
    """

    WIDTH, HEIGHT, MARGIN, RADIUS = 210, 64, 16, 16   # logical pixels
    TIMER_PLACE, TIMER_STATUS = 1, 2
    CLASS_NAME = "iPadDisplayDropBox"

    def __init__(self, get_rect, on_drop):
        self.get_rect = get_rect
        self.on_drop = on_drop
        self.hwnd = None
        self.status = ""
        self.placed_for = None
        self.scale = 1.0
        self.fonts = (None, None)
        self._wndproc = WNDPROC(self._proc)   # keep the callback alive for the window's lifetime
        self.ready = threading.Event()
        self.thread = threading.Thread(target=self._run, name="dropbox", daemon=True)

    def start(self) -> None:
        self.thread.start()
        self.ready.wait(5)

    def stop(self) -> None:
        if self.hwnd:
            user32.PostMessageW(self.hwnd, WM_CLOSE, 0, 0)

    # ---- window thread ------------------------------------------------------

    def _run(self) -> None:
        hinst = kernel32.GetModuleHandleW(None)
        wc = WNDCLASSEXW(cbSize=ctypes.sizeof(WNDCLASSEXW), lpfnWndProc=self._wndproc, hInstance=hinst,
                         hCursor=user32.LoadCursorW(None, ctypes.c_void_p(32512)),   # IDC_ARROW
                         lpszClassName=self.CLASS_NAME)
        user32.RegisterClassExW(ctypes.byref(wc))
        style = WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_LAYERED | WS_EX_ACCEPTFILES | WS_EX_NOACTIVATE
        self.hwnd = user32.CreateWindowExW(style, self.CLASS_NAME, "Send to iPad", WS_POPUP,
                                           0, 0, 10, 10, None, None, hinst, None)
        if not self.hwnd:
            log.warning("Could not create the drop box (%d)", ctypes.get_last_error())
            self.ready.set()
            return
        user32.SetLayeredWindowAttributes(self.hwnd, 0, 250, LWA_ALPHA)
        for msg in (WM_DROPFILES, WM_COPYDATA, WM_COPYGLOBALDATA):   # still accept drops if we run elevated
            user32.ChangeWindowMessageFilterEx(self.hwnd, msg, MSGFLT_ALLOW, None)
        self._place()
        user32.SetTimer(self.hwnd, self.TIMER_PLACE, 1000, None)
        self.ready.set()
        msg = wt.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    def _place(self) -> None:
        rect = self.get_rect()
        if rect is None:
            if user32.IsWindowVisible(self.hwnd):
                user32.ShowWindow(self.hwnd, SW_HIDE)
            self.placed_for = None
            return
        left, top, width, height = rect
        area = work_area_at(left + width // 2, top + height // 2)
        if area == self.placed_for:
            if not user32.IsWindowVisible(self.hwnd):
                user32.ShowWindow(self.hwnd, SW_SHOWNOACTIVATE)
            return
        self.placed_for = area                 # a new display: go to its bottom-right corner
        wl, wtop, wr, wb, dpi = area
        k = self.scale = dpi / 96
        w, h, m = round(self.WIDTH * k), round(self.HEIGHT * k), round(self.MARGIN * k)
        user32.SetWindowPos(self.hwnd, HWND_TOPMOST, wr - w - m, wb - h - m, w, h,
                            SWP_NOACTIVATE | SWP_SHOWWINDOW)
        radius = round(self.RADIUS * k)
        user32.SetWindowRgn(self.hwnd, gdi32.CreateRoundRectRgn(0, 0, w + 1, h + 1, radius, radius), True)
        for font in self.fonts:
            if font:
                gdi32.DeleteObject(font)
        self.fonts = tuple(gdi32.CreateFontW(-round(size * k), 0, 0, 0, weight, 0, 0, 0, 1, 0, 0, 5, 0, "Segoe UI")
                           for size, weight in ((15, 600), (12, 400)))
        user32.InvalidateRect(self.hwnd, None, True)

    def _flash(self, text: str) -> None:
        self.status = text
        user32.InvalidateRect(self.hwnd, None, True)
        user32.SetTimer(self.hwnd, self.TIMER_STATUS, 3000, None)

    def _paint(self, hwnd) -> None:
        ps = PAINTSTRUCT()
        hdc = user32.BeginPaint(hwnd, ctypes.byref(ps))
        try:
            rc = wt.RECT()
            user32.GetClientRect(hwnd, ctypes.byref(rc))
            k = self.scale
            brush = gdi32.CreateSolidBrush(_rgb(24, 24, 27))
            user32.FillRect(hdc, ctypes.byref(rc), brush)
            gdi32.DeleteObject(brush)
            pen = gdi32.CreatePen(0, max(2, round(2 * k)), _rgb(56, 189, 248))
            old_pen = gdi32.SelectObject(hdc, pen)
            old_brush = gdi32.SelectObject(hdc, gdi32.GetStockObject(5))   # NULL_BRUSH
            radius = round(self.RADIUS * k)
            gdi32.RoundRect(hdc, 1, 1, rc.right - 1, rc.bottom - 1, radius, radius)
            gdi32.SelectObject(hdc, old_pen)
            gdi32.SelectObject(hdc, old_brush)
            gdi32.DeleteObject(pen)
            gdi32.SetBkMode(hdc, 1)                                        # TRANSPARENT
            flags = DT_CENTER | DT_VCENTER | DT_SINGLELINE | DT_END_ELLIPSIS
            title_font, sub_font = self.fonts
            old_font = gdi32.SelectObject(hdc, title_font)
            gdi32.SetTextColor(hdc, _rgb(255, 255, 255))
            if self.status:
                user32.DrawTextW(hdc, self.status, -1, ctypes.byref(rc), flags)
            else:
                mid = rc.bottom // 2
                user32.DrawTextW(hdc, "Drop files here", -1, ctypes.byref(wt.RECT(0, round(4 * k), rc.right, mid + round(4 * k))), flags)
                gdi32.SelectObject(hdc, sub_font)
                gdi32.SetTextColor(hdc, _rgb(161, 161, 170))
                user32.DrawTextW(hdc, "to send them to the iPad", -1, ctypes.byref(wt.RECT(0, mid, rc.right, rc.bottom - round(6 * k))), flags)
            gdi32.SelectObject(hdc, old_font)
        finally:
            user32.EndPaint(hwnd, ctypes.byref(ps))

    def _proc(self, hwnd, msg, wparam, lparam):
        try:
            if msg == WM_NCHITTEST:
                return HTCAPTION                    # drag the box anywhere to move it
            if msg == WM_MOUSEACTIVATE:
                return MA_NOACTIVATE                # never take focus from what you're working in
            if msg == WM_DROPFILES:
                paths = []
                for i in range(shell32.DragQueryFileW(wparam, 0xFFFFFFFF, None, 0)):
                    length = shell32.DragQueryFileW(wparam, i, None, 0)
                    buf = ctypes.create_unicode_buffer(length + 1)
                    shell32.DragQueryFileW(wparam, i, buf, length + 1)
                    paths.append(buf.value)
                shell32.DragFinish(wparam)
                self._flash(self.on_drop(paths))
                return 0
            if msg == WM_PAINT:
                self._paint(hwnd)
                return 0
            if msg == WM_TIMER:
                if wparam == self.TIMER_PLACE:
                    self._place()
                elif wparam == self.TIMER_STATUS:
                    user32.KillTimer(hwnd, self.TIMER_STATUS)
                    self.status = ""
                    user32.InvalidateRect(hwnd, None, True)
                return 0
            if msg == WM_DESTROY:
                user32.PostQuitMessage(0)
                return 0
        except Exception:
            log.exception("drop box")
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)


# ---------------------------------------------------------------------------
# Console window closing
# ---------------------------------------------------------------------------

HANDLER_ROUTINE = ctypes.WINFUNCTYPE(wt.BOOL, wt.DWORD)
_close_handlers = []   # keep the callbacks alive
CTRL_CLOSE_EVENT, CTRL_LOGOFF_EVENT, CTRL_SHUTDOWN_EVENT = 2, 5, 6


def on_console_close(fn) -> None:
    """Run fn() when the console window is closed (X), or at log-off / shutdown."""
    def handler(event):
        if event in (CTRL_CLOSE_EVENT, CTRL_LOGOFF_EVENT, CTRL_SHUTDOWN_EVENT):
            try:
                fn()
            except Exception:
                pass
        return False   # then let Windows close the program as usual
    callback = HANDLER_ROUTINE(handler)
    _close_handlers.append(callback)
    kernel32.SetConsoleCtrlHandler(callback, True)

