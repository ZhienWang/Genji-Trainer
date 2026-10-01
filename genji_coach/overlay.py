"""A small always-on-top, click-through tip box.

Overwatch has to run in Borderless Windowed mode for any overlay to show on top of it.
"""

from __future__ import annotations

import queue
import sys
import tkinter as tk

BG = "#101418"
ACCENT = "#7CFC9A"
TEXT = "#E8F0EA"
MUTED = "#9AA7A0"


class Overlay:
    def __init__(self, position: str = "top-right", seconds: float = 7.0):
        self._queue: queue.Queue[tuple[str, str, str]] = queue.Queue()
        self._seconds = seconds
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", 0.9)
        self.root.configure(bg=BG)

        frame = tk.Frame(self.root, bg=BG, padx=14, pady=10,
                         highlightbackground=ACCENT, highlightthickness=2)
        frame.pack()
        tk.Label(frame, text="Genji Coach", fg=ACCENT, bg=BG, font=("Segoe UI", 9, "bold")).pack(anchor="w")
        self._main = tk.Label(frame, fg=TEXT, bg=BG, font=("Segoe UI", 14, "bold"),
                              wraplength=420, justify="left")
        self._main.pack(anchor="w")
        self._detail = tk.Label(frame, fg=MUTED, bg=BG, font=("Segoe UI", 10),
                                wraplength=420, justify="left")
        self._detail.pack(anchor="w", pady=(4, 0))
        self._credit = tk.Label(frame, fg=MUTED, bg=BG, font=("Segoe UI", 8, "italic"))
        self._credit.pack(anchor="w", pady=(4, 0))

        self._position = position
        self._hide_job: str | None = None
        self.root.after(100, self._poll)

    def show(self, spoken: str, detail: str, credit: str = "") -> None:
        """Thread-safe: queue a tip for display."""
        self._queue.put((spoken, detail, credit))

    def _place(self) -> None:
        self.root.update_idletasks()
        w, h = self.root.winfo_reqwidth(), self.root.winfo_reqheight()
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        x = 24 if "left" in self._position else sw - w - 24
        # Stay clear of the kill feed (top-right) and the ability HUD (bottom).
        y = 220 if "top" in self._position else sh - h - 260
        self.root.geometry(f"+{x}+{y}")

    def _make_click_through(self) -> None:
        if sys.platform != "win32":
            return
        import ctypes
        GWL_EXSTYLE, WS_EX_LAYERED, WS_EX_TRANSPARENT, WS_EX_TOOLWINDOW = -20, 0x80000, 0x20, 0x80
        hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
        style = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        ctypes.windll.user32.SetWindowLongW(
            hwnd, GWL_EXSTYLE, style | WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW)

    def _poll(self) -> None:
        try:
            spoken, detail, credit = self._queue.get_nowait()
        except queue.Empty:
            pass
        else:
            self._main.config(text=spoken)
            self._detail.config(text=detail)
            self._credit.config(text=f"Source: {credit}" if credit else "")
            self._place()
            self.root.deiconify()
            self.root.attributes("-topmost", True)
            self._make_click_through()
            if self._hide_job:
                self.root.after_cancel(self._hide_job)
            self._hide_job = self.root.after(int(self._seconds * 1000), self.root.withdraw)
        self.root.after(100, self._poll)

    def run(self, stop_check) -> None:
        def check():
            if stop_check():
                self.root.destroy()
            else:
                self.root.after(250, check)
        self.root.after(250, check)
        self.root.mainloop()
