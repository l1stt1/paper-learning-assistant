"""DPI-aware Tk presentation shared by the Windows desktop entry points."""
from __future__ import annotations

import os
from pathlib import Path


def enable_high_dpi():
    """Avoid Windows bitmap stretching; call before creating any Tk window.

    Tk 8.6 uses a single scaling factor, so request system DPI awareness rather
    than claiming support for per-monitor layout changes it does not implement.
    The packaged executables declare the same setting in their manifest.
    """
    if os.name == "nt":
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (AttributeError, OSError):
            ctypes.windll.user32.SetProcessDPIAware()


class DesktopTheme:
    def __init__(self, root, assets):
        import tkinter as tk
        from tkinter import ttk
        self.scale = root.winfo_fpixels("1i") / 96
        self.images = []
        self.style = ttk.Style(root)
        self.style.theme_use("clam")
        density = min((100, 125, 150, 175, 200, 250, 300), key=lambda n: abs(n - self.scale * 100))
        folder = Path(assets) / "desktop" / str(density)
        self.logo = tk.PhotoImage(master=root, file=str(folder / "logo.png"))
        for name, normal, hover, foreground in (
            ("Primary", "primary", "primary-hover", "#ffffff"),
            ("Quiet", "quiet", "quiet-hover", "#294c51"),
        ):
            normal_image = tk.PhotoImage(master=root, file=str(folder / (normal + ".png")))
            hover_image = tk.PhotoImage(master=root, file=str(folder / (hover + ".png")))
            disabled_image = tk.PhotoImage(master=root, file=str(folder / "disabled.png"))
            self.images.extend((normal_image, hover_image, disabled_image))
            element = name + ".surface"
            self.style.element_create(element, "image", normal_image,
                ("disabled", disabled_image), ("pressed", hover_image), ("active", hover_image),
                border=self.px(9), sticky="nsew")
            self.style.layout(name + ".TButton", [(element, {"sticky": "nsew", "children": [
                ("Button.focus", {"sticky": "nsew", "children": [
                    ("Button.padding", {"sticky": "nsew", "children": [
                        ("Button.label", {"sticky": "nsew"})]})]})]})])
            self.style.configure(name + ".TButton", foreground=foreground, background="#ffffff",
                font=("Microsoft YaHei UI", 10, "bold" if name == "Primary" else "normal"),
                padding=(self.px(20), self.px(12)), borderwidth=0, focusthickness=1,
                focuscolor="#b9e0db" if name == "Primary" else "#507f80")
            self.style.map(name + ".TButton", foreground=[("disabled", "#8c9e9e")])
        self.style.configure("Small.TButton", font=("Microsoft YaHei UI", 9), foreground="#287571",
            background="#ffffff", padding=(self.px(10), self.px(7)), borderwidth=0,
            focuscolor="#287571")
        self.style.map("Small.TButton", background=[("active", "#edf6f3")],
            foreground=[("disabled", "#8c9e9e")])

    def px(self, value):
        return round(value * self.scale)
