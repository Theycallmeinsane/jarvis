"""Global push-to-talk hotkey (Win32 RegisterHotKey, no extra packages, no admin)."""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import logging
import threading

log = logging.getLogger(__name__)

MODS = {"alt": 0x1, "ctrl": 0x2, "control": 0x2, "shift": 0x4, "win": 0x8}
MOD_NOREPEAT = 0x4000
WM_HOTKEY, WM_QUIT = 0x0312, 0x0012
NAMED_KEYS = {"space": 0x20, "enter": 0x0D, "pause": 0x13, "insert": 0x2D, "home": 0x24, "end": 0x23,
              **{f"f{i}": 0x6F + i for i in range(1, 13)}}


def parse(combo: str) -> tuple[int, int]:
    """'ctrl+alt+j' -> (modifiers, virtual key code)."""
    *mods, key = [p.strip().lower() for p in combo.split("+")]
    mod_bits = 0
    for m in mods:
        if m not in MODS:
            raise ValueError(f"unknown modifier {m!r} in hotkey {combo!r}")
        mod_bits |= MODS[m]
    if key in NAMED_KEYS:
        vk = NAMED_KEYS[key]
    elif len(key) == 1 and key.isalnum():
        vk = ord(key.upper())
    else:
        raise ValueError(f"unsupported key {key!r} in hotkey {combo!r}")
    return mod_bits, vk


class Hotkey:
    """Sets `pressed` (a threading.Event) whenever the combo is pressed anywhere in Windows."""

    def __init__(self, combo: str):
        self.combo = combo
        self.mods, self.vk = parse(combo)
        self.pressed = threading.Event()
        self._thread_id = None
        self._thread = threading.Thread(target=self._loop, daemon=True, name="hotkey")

    def start(self) -> "Hotkey":
        self._thread.start()
        return self

    def _loop(self):
        user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
        self._thread_id = kernel32.GetCurrentThreadId()
        if not user32.RegisterHotKey(None, 1, self.mods | MOD_NOREPEAT, self.vk):
            log.warning("Couldn't register hotkey %s (another app may be using it)", self.combo)
            return
        log.info("Push-to-talk hotkey: %s", self.combo)
        msg = wt.MSG()
        try:
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == WM_HOTKEY:
                    self.pressed.set()
        finally:
            user32.UnregisterHotKey(None, 1)

    def stop(self):
        if self._thread_id:
            ctypes.windll.user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
