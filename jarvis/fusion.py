"""Direct control of Gigabyte RGB Fusion 2 motherboard lighting (the ITE IT5702 / IT8297 USB chip).

The chip is a USB HID device that takes 64-byte feature reports, so Jarvis can drive it without OpenRGB, without
admin rights and without extra packages (it uses Windows' own hid.dll). Writing the same chip as the RGB Fusion
app is fine: whoever wrote last wins, though RGB Fusion may re-apply its own look after sleep or a restart.

The last look and any saved looks are kept in data/lights.json, so "lights on" brings back what you had.
"""
from __future__ import annotations

import json
import logging
import struct
import sys
from dataclasses import asdict, dataclass

from .config import ROOT

log = logging.getLogger(__name__)

VENDORS = (0x048D, 0x0414)
PRODUCTS = (0x8297, 0x8950, 0x5702, 0x5711, 0xA100)
USAGE_PAGE, USAGE = 0xFF89, 0xCC
REPORT = 0xCC
SIZE = 64

# effect ids the chip understands
STATIC, BREATHING, FLASHING, COLOR_CYCLE, RANDOM = 1, 2, 3, 4, 8
EFFECTS = {"static": STATIC, "breathing": BREATHING, "flashing": FLASHING, "double flash": FLASHING,
           "color cycle": COLOR_CYCLE, "random": RANDOM}
EFFECT_WORDS = {"rainbow": "color cycle", "cycle": "color cycle", "colour cycle": "color cycle",
                "spectrum cycle": "color cycle", "spectrum": "color cycle", "breathe": "breathing",
                "pulse": "breathing", "pulsing": "breathing", "flash": "flashing", "blink": "flashing",
                "blinking": "flashing", "strobe": "flashing", "solid": "static", "direct": "static"}

STATE_FILE = ROOT / "data" / "lights.json"


class NotFound(Exception):
    pass


@dataclass
class Look:
    effect: str = "static"
    color: tuple[int, int, int] = (255, 255, 255)
    brightness: int = 100          # percent
    speed: int = 5                 # 1 slow .. 10 fast
    on: bool = True


def effect_name(text: str) -> str | None:
    t = text.lower().replace(" mode", "").replace(" effect", "").strip()
    t = EFFECT_WORDS.get(t, t)
    return t if t in EFFECTS else None


def cc(*data: int) -> bytes:
    """A short command: CC <cmd> <args...>, zero padded."""
    return bytes([REPORT, *data]).ljust(SIZE, b"\0")


def led_slots(product: int) -> list[tuple[int, int]]:
    """(register, zone mask) for every on-board lighting zone the chip has."""
    slots = [(0x20 + i, 1 << i) for i in range(8)]
    if product == 0x5711:
        slots += [(0x90 + i, 1 << (8 + i)) for i in range(3)]
    if product == 0xA100:
        slots = [(0x20, 0xFF)]
    return slots


def effect_packet(register: int, mask: int, look: Look) -> bytes:
    """One zone's effect: mode, brightness, colour (sent blue-green-red) and timings."""
    r, g, b = look.color if look.on else (0, 0, 0)
    mode = EFFECTS.get(look.effect, STATIC) if look.on else STATIC
    speed = max(1, min(10, int(look.speed)))
    bright = max(0, min(255, round(255 * look.brightness / 100))) if look.on else 0
    periods, params = [0, 0, 0, 0], [0, 0, 0, 0]
    if mode == BREATHING:
        fade = 2300 - speed * 190                 # 2.1 s (slow) .. 0.4 s (fast) each way
        periods = [fade, fade, 200, 0]
    elif mode == FLASHING:
        periods = [100, 100, 2700 - speed * 220, 0]
        if look.effect == "double flash":
            params = [0, 1, 2, 0]
    elif mode == COLOR_CYCLE:
        step = 3500 - speed * 300
        periods, params = [step, step - 200, 0, 0], [7, 0, 0, 0]
    elif mode == RANDOM:
        periods, params = [100, 0, 0, 0], [1, 5, 0, 0]
    body = struct.pack("<BBIIBBBB4s4sHHHH4B", REPORT, register, mask, 0, 0, mode, bright, 0,
                       bytes([b, g, r, 0]), bytes(4), *periods, *params)
    return body.ljust(SIZE, b"\0")


def apply_packet(product: int) -> bytes:
    return cc(0x28, 0xFF, 0x07 if product == 0x5711 else 0x00)


class Fusion:
    """The board's lighting chip. `transport` sends/reads feature reports (Windows HID, or a fake in tests)."""

    def __init__(self, transport, product: int):
        self.t, self.product = transport, product
        self.name = "Gigabyte RGB Fusion"
        self.flags = 0
        try:
            self.t.send(cc(0x60))
            info = self.t.read(REPORT)
            if len(info) >= 40:
                self.flags = info[11]
                self.name = info[12:40].split(b"\0", 1)[0].decode("ascii", "replace").strip() or self.name
        except OSError as e:
            log.debug("RGB Fusion info read failed: %s", e)

    def show(self, look: Look):
        if self.flags & 0x02:
            self.t.send(cc(0x48, 0))          # take the chip back from Windows Dynamic Lighting
        self.t.send(cc(0x31, 0))              # music "beat" mode off
        for register, mask in led_slots(self.product):
            self.t.send(effect_packet(register, mask, look))
        self.t.send(apply_packet(self.product))

    def close(self):
        self.t.close()


def find() -> Fusion:
    if sys.platform != "win32":
        raise NotFound("Direct RGB Fusion control works on Windows only.")
    from . import winhid

    dev = winhid.open_first(VENDORS, PRODUCTS, USAGE_PAGE, USAGE)
    if dev is None:
        raise NotFound("I can't find a Gigabyte RGB Fusion 2 lighting chip on this PC.")
    return Fusion(dev, dev.product)


def available() -> bool:
    try:
        find().close()
        return True
    except (NotFound, OSError):
        return False


# --- remembered looks -------------------------------------------------------------------------------------

def _load() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save(data: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(data, indent=1), encoding="utf-8")


def _look(d: dict | None) -> Look:
    if not d:
        return Look()
    d = {**asdict(Look()), **d}
    return Look(d["effect"], tuple(d["color"]), int(d["brightness"]), int(d["speed"]), bool(d["on"]))


def current() -> Look:
    return _look(_load().get("current"))


def apply(look: Look, chip: Fusion | None = None) -> Look:
    """Show a look on the board and remember it."""
    own = chip is None
    chip = chip or find()
    try:
        chip.show(look)
    finally:
        if own:
            chip.close()
    data = _load()
    data["current"] = asdict(look)
    _save(data)
    return look


def profiles() -> dict[str, Look]:
    return {k: _look(v) for k, v in (_load().get("profiles") or {}).items()}


def save_profile(name: str, look: Look):
    data = _load()
    data.setdefault("profiles", {})[name] = asdict(look)
    _save(data)
