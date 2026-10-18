"""A small on-screen controller for the PC's lights: colours, effects, brightness, speed, on/off.

Opens with `jarvis.bat --lights`, the tray's Lights menu, or "open the lights panel". A Gigabyte RGB Fusion 2 board
is driven directly (jarvis/fusion.py); without one it falls back to OpenRGB through the rgb tool.
"""
from __future__ import annotations

import threading

from . import fusion
from .tools import rgb_tools

SWATCHES = ["jarvis blue", "iron man red", "purple", "pink", "orange", "gold", "green", "cyan", "warm white", "white"]
EFFECT_LABELS = [("Static", "static"), ("Breathing", "breathing"), ("Flashing", "flashing"),
                 ("Double flash", "double flash"), ("Rainbow", "color cycle"), ("Random", "random")]
BG, FG, MUTED, PANEL = "#15171c", "#e8eaf0", "#9aa0ad", "#22252d"


def _hex(rgb) -> str:
    return "#%02x%02x%02x" % tuple(rgb)


def run() -> int:
    import tkinter as tk
    from tkinter import colorchooser

    root = tk.Tk()
    root.title("Jarvis lights")
    root.configure(bg=BG, padx=16, pady=14)
    root.resizable(False, False)
    look = fusion.current()
    status = tk.StringVar(value="")
    effect_var = tk.StringVar(value=look.effect)
    bright_var, speed_var = tk.IntVar(value=look.brightness), tk.IntVar(value=look.speed)

    def label(text, row, pady=(10, 4)):
        tk.Label(root, text=text, bg=BG, fg=MUTED, font=("Segoe UI", 9)).grid(row=row, column=0, columnspan=5,
                                                                             sticky="w", pady=pady)

    preview = tk.Frame(root, width=330, height=10, bg=_hex(look.color))
    preview.grid(row=0, column=0, columnspan=5, sticky="we")

    def say(text):
        root.after(0, lambda: status.set(text))

    def apply(**changes):
        for k, v in changes.items():
            setattr(look, k, v)
        if "color" in changes:
            preview.configure(bg=_hex(look.color))
            if look.effect in ("color cycle", "random"):
                look.effect = "static"
                effect_var.set("static")

        def work():
            try:
                fusion.apply(look)
                say("Lights off." if not look.on else f"{look.effect.capitalize()}, {look.brightness}%")
                return
            except fusion.NotFound:
                pass
            except Exception as e:  # noqa: BLE001
                say(f"The lighting chip didn't take it ({e}). Trying OpenRGB...")
            try:          # no Gigabyte chip: what OpenRGB can do
                if not look.on:
                    say(rgb_tools.rgb("off"))
                elif "effect" in changes and look.effect != "static":
                    say(rgb_tools.rgb("effect", look.effect))
                else:
                    say(rgb_tools.rgb("color", _hex(look.color)))
            except Exception as e:  # noqa: BLE001
                say(str(e))

        threading.Thread(target=work, daemon=True).start()

    label("Colour", 1)
    for i, name in enumerate(SWATCHES):
        c = _hex(rgb_tools.COLORS[name])
        tk.Button(root, bg=c, activebackground=c, width=5, height=1, relief="flat", cursor="hand2",
                  command=lambda n=name: apply(color=rgb_tools.COLORS[n], on=True)).grid(
            row=2 + i // 5, column=i % 5, padx=3, pady=3)

    def custom():
        picked = colorchooser.askcolor(color=_hex(look.color), title="Pick a colour")[0]
        if picked:
            apply(color=tuple(int(x) for x in picked), on=True)

    tk.Button(root, text="Custom colour...", command=custom, bg=PANEL, fg=FG, relief="flat",
              activebackground=PANEL, activeforeground=FG).grid(row=4, column=0, columnspan=5, sticky="we", pady=(4, 0))

    label("Effect", 5)
    for i, (text, key) in enumerate(EFFECT_LABELS):
        tk.Radiobutton(root, text=text, value=key, variable=effect_var, bg=BG, fg=FG, selectcolor=PANEL,
                       activebackground=BG, activeforeground=FG, anchor="w",
                       command=lambda k=key: apply(effect=k, on=True)).grid(
            row=6 + i // 3, column=(i % 3) * 2, columnspan=2, sticky="w")

    def slider(text, var, lo, hi, row, key):
        label(text, row)
        s = tk.Scale(root, from_=lo, to=hi, orient="horizontal", variable=var, bg=BG, fg=FG, troughcolor=PANEL,
                     highlightthickness=0, length=330, showvalue=True)
        s.grid(row=row + 1, column=0, columnspan=5, sticky="we")
        s.bind("<ButtonRelease-1>", lambda _e: apply(**{key: var.get(), "on": True}))

    slider("Brightness (%)", bright_var, 0, 100, 8, "brightness")
    slider("Speed (slow to fast)", speed_var, 1, 10, 10, "speed")

    row = tk.Frame(root, bg=BG)
    row.grid(row=12, column=0, columnspan=5, sticky="we", pady=(12, 4))
    for text, on in (("On", True), ("Off", False)):
        tk.Button(row, text=text, width=12, bg=PANEL, fg=FG, relief="flat", activebackground=PANEL,
                  activeforeground=FG, command=lambda v=on: apply(on=v)).pack(side="left", expand=True, padx=4)
    tk.Label(root, textvariable=status, bg=BG, fg=MUTED, font=("Segoe UI", 9), wraplength=330,
             justify="left").grid(row=13, column=0, columnspan=5, sticky="w")
    root.mainloop()
    return 0
