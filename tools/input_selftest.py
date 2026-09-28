"""Check that iPad Display can inject touch, pen, mouse, wheel and keyboard input on this PC.

Opens a small always-on-top window for a few seconds and injects input into it only:
before every step it checks that the window is under the target point (and, for keys,
that it has keyboard focus), so nothing else on your desktop gets clicked or typed into.

    .venv\\Scripts\\python.exe tools\\input_selftest.py
"""
import ctypes
import ctypes.wintypes as wt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import win32  # noqa: E402  (first: makes the process DPI aware, so Tk reports physical pixels)
import tkinter as tk  # noqa: E402

user32 = win32.user32
user32.WindowFromPoint.argtypes = [wt.POINT]
user32.WindowFromPoint.restype = wt.HWND
user32.GetAncestor.argtypes = [wt.HWND, wt.UINT]
user32.GetAncestor.restype = wt.HWND
user32.GetForegroundWindow.restype = wt.HWND
user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
GA_ROOT = 2
OWNER = "selftest"
SETTLE_MS = 900


class SelfTest:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("iPad Display - input self-test")
        self.root.attributes("-topmost", True)
        w, h = 640, 380
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        self.root.geometry(f"{w}x{h}+{(sw - w) // 2}+{(sh - h) // 2}")
        self.label = tk.Label(self.root, font=("Segoe UI", 14), wraplength=w - 40, justify="center",
                              text="Testing input injection...\nPlease don't use the mouse or keyboard for a few seconds.")
        self.label.pack(expand=True, fill="both")
        self.events = []
        for seq in ("<ButtonPress-1>", "<ButtonRelease-1>", "<B1-Motion>", "<ButtonPress-3>", "<MouseWheel>", "<KeyPress>"):
            self.root.bind(seq, lambda e, s=seq: self.events.append((s, getattr(e, "keysym", ""))))
        self.injector = win32.InputInjector()
        self.saved_cursor = (win32.get_cursor() or (None, None))[:2]
        self.results = []
        self.errors = []
        self.finished = False
        self.root.protocol("WM_DELETE_WINDOW", self.finish)
        self.root.after(30_000, self.finish)   # safety net: never linger
        self.steps = [
            ("touch tap", self.touch_tap, lambda ev: {"<ButtonPress-1>", "<ButtonRelease-1>"} <= ev),
            ("touch drag", self.touch_drag, lambda ev: {"<ButtonPress-1>", "<B1-Motion>"} <= ev),
            ("pen stroke", self.pen_stroke, lambda ev: {"<ButtonPress-1>", "<B1-Motion>", "<ButtonRelease-1>"} <= ev),
            ("mouse right-click", self.right_click, lambda ev: "<ButtonPress-3>" in ev),
            ("scroll wheel", self.wheel, lambda ev: "<MouseWheel>" in ev),
            ("keyboard", self.keyboard, lambda ev: "a" in self.keysyms()),
        ]
        self.root.after(800, self.run_step, 0)

    # ---- guards ----------------------------------------------------------

    def my_root(self):
        return user32.GetAncestor(self.root.winfo_id(), GA_ROOT)

    def owns(self, x, y) -> bool:
        hwnd = user32.WindowFromPoint(wt.POINT(x, y))
        return bool(hwnd) and user32.GetAncestor(hwnd, GA_ROOT) == self.my_root()

    def has_focus(self) -> bool:
        fg = user32.GetForegroundWindow()
        return bool(fg) and user32.GetAncestor(fg, GA_ROOT) == self.my_root()

    def center(self):
        return (self.root.winfo_rootx() + self.root.winfo_width() // 2,
                self.root.winfo_rooty() + self.root.winfo_height() // 2)

    def keysyms(self):
        return {k.lower() for s, k in self.events if s == "<KeyPress>"}

    # ---- steps (each returns the screen points it will touch, then injects) ---

    def later(self, ms, fn, *args, **kwargs):
        def call():
            try:
                fn(*args, **kwargs)
            except Exception as exc:  # record it; finish() still releases everything
                self.errors.append(f"{getattr(fn, '__name__', fn)}: {exc}")
        self.root.after(ms, call)

    def touch_tap(self):
        x, y = self.center()
        yield [(x, y)]
        self.injector.pointer(OWNER, "d", "t", 1, x, y)
        self.later(60, self.injector.pointer, OWNER, "u", "t", 1, x, y)

    def touch_drag(self):
        x, y = self.center()
        pts = [(x - 150 + 15 * i, y + 60) for i in range(21)]
        yield pts
        self.injector.pointer(OWNER, "d", "t", 2, *pts[0])
        for i, p in enumerate(pts[1:], 1):
            self.later(16 * i, self.injector.pointer, OWNER, "m", "t", 2, *p)
        self.later(16 * len(pts), self.injector.pointer, OWNER, "u", "t", 2, *pts[-1])

    def pen_stroke(self):
        x, y = self.center()
        pts = [(x - 120 + 12 * i, y - 60 + (i % 5) * 4) for i in range(21)]
        yield pts
        self.injector.pointer(OWNER, "m", "p", 0, *pts[0])                    # hover in
        self.later(30, self.injector.pointer, OWNER, "d", "p", 0, *pts[0], 0.6, 10, -5, 1)
        for i, p in enumerate(pts[1:], 1):
            self.later(30 + 16 * i, self.injector.pointer, OWNER, "m", "p", 0, *p, 0.4 + i / 50, 10, -5, 1)
        end = 30 + 16 * len(pts)
        self.later(end, self.injector.pointer, OWNER, "u", "p", 0, *pts[-1])
        self.later(end + 40, self.injector.pointer, OWNER, "l", "p", 0, *pts[-1])

    def right_click(self):
        x, y = self.center()
        yield [(x, y)]
        self.injector.pointer(OWNER, "d", "m", 1, x, y, buttons=2)
        self.later(60, self.injector.pointer, OWNER, "u", "m", 1, x, y, buttons=0)

    def wheel(self):
        x, y = self.center()
        yield [(x, y)]
        self.injector.wheel(x, y, 0, 100, 0)

    def keyboard(self):
        yield "focus"
        self.injector.key(OWNER, "KeyA", True)
        self.later(40, self.injector.key, OWNER, "KeyA", False)

    # ---- driver ------------------------------------------------------------

    def run_step(self, i):
        if self.finished:
            return
        if i >= len(self.steps):
            return self.finish()
        name, step, check = self.steps[i]
        self.events.clear()
        self.errors.clear()
        try:
            gen = step()
            needs = next(gen)
            if needs == "focus":
                self.root.focus_force()
                safe, why = self.has_focus(), "the test window did not get keyboard focus"
            else:
                safe, why = all(self.owns(x, y) for x, y in needs), "another window covers the test window"
            if not safe:
                self.results.append((name, "SKIPPED", why))
                return self.root.after(100, self.run_step, i + 1)
            next(gen, None)
        except Exception as exc:
            self.errors.append(str(exc))
        self.root.after(SETTLE_MS, self.check_step, i, name, check)

    def check_step(self, i, name, check):
        seen = {s for s, _ in self.events}
        ok = check(seen) and not self.errors
        detail = "; ".join(self.errors) or ", ".join(sorted(seen)) or "no events"
        self.results.append((name, "ok" if ok else "FAILED", detail))
        self.run_step(i + 1)

    def finish(self):
        if self.finished:
            return
        self.finished = True
        try:
            self.injector.release(OWNER)     # lift any finger/pen/button/key still down
            self.injector.close()
        finally:
            if self.saved_cursor[0] is not None:
                user32.SetCursorPos(*self.saved_cursor)
            self.root.destroy()

    def run(self):
        caps = self.injector.caps
        self.root.mainloop()
        print(f"Injection devices: touch={'yes' if caps['touch'] else 'no (mouse fallback)'}, "
              f"pen={'yes' if caps['pen'] else 'no (mouse fallback)'}")
        width = max((len(n) for n, *_ in self.results), default=0)
        for name, status, detail in self.results:
            print(f"  {name:<{width}}  {status:<7}  {detail}")
        return all(status != "FAILED" for _, status, _ in self.results)


if __name__ == "__main__":
    sys.exit(0 if SelfTest().run() else 1)
