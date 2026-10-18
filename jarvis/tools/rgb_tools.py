"""RGB lighting: Gigabyte RGB Fusion 2 motherboards directly (jarvis/fusion.py), everything else through OpenRGB.

A Gigabyte board's lighting chip is driven straight over USB, with no OpenRGB, admin rights or driver. RAM, fans,
GPUs and keyboards go through OpenRGB's SDK server when it's running (Settings > SDK server, or --server).
Config rgb.backend: auto (default) | fusion (board only) | openrgb (never drive the chip directly).
"""
from __future__ import annotations

import difflib
import logging
import re
from pathlib import Path

from .registry import Spoken, tool

log = logging.getLogger(__name__)

COLORS = {
    "red": (255, 0, 0), "green": (0, 255, 0), "blue": (0, 0, 255), "white": (255, 255, 255),
    "warm white": (255, 170, 90), "cool white": (200, 220, 255), "yellow": (255, 200, 0), "orange": (255, 90, 0),
    "purple": (140, 0, 255), "violet": (170, 0, 255), "pink": (255, 40, 140), "magenta": (255, 0, 255),
    "cyan": (0, 255, 255), "teal": (0, 160, 140), "turquoise": (40, 220, 200), "lime": (120, 255, 0),
    "gold": (255, 160, 0), "amber": (255, 120, 0), "crimson": (220, 0, 40), "indigo": (75, 0, 200),
    "sky blue": (80, 170, 255), "light blue": (110, 180, 255), "dark blue": (0, 0, 140), "navy": (0, 0, 100),
    "mint": (90, 255, 160), "lavender": (180, 140, 255), "peach": (255, 140, 100), "jarvis blue": (0, 140, 255),
    "iron man red": (200, 10, 10),
}


def _fail(msg: str):
    from .builtin import ToolFailed

    raise ToolFailed(msg)


def parse_color(text: str) -> tuple[int, int, int]:
    """'red', 'dim blue', 'warm white', '#ff8800', '255 0 128' -> RGB (dim/bright scale it)."""
    t = text.lower().strip()
    scale = 1.0
    for word, s in (("very dim ", 0.15), ("dim ", 0.3), ("dark ", 0.5), ("soft ", 0.6), ("bright ", 1.0)):
        if t.startswith(word) and t[len(word):] in COLORS:
            t, scale = t[len(word):], s
            break
    m = re.fullmatch(r"#?([0-9a-f]{6})", t)
    if m:
        rgb = tuple(int(m.group(1)[i:i + 2], 16) for i in (0, 2, 4))
    elif re.fullmatch(r"\d{1,3}[ ,]+\d{1,3}[ ,]+\d{1,3}", t):
        rgb = tuple(min(255, int(x)) for x in re.findall(r"\d+", t))
    elif t in COLORS:
        rgb = COLORS[t]
    else:
        close = difflib.get_close_matches(t, list(COLORS), n=1, cutoff=0.6)
        if not close:
            raise ValueError(f"I don't know the colour {text}.")
        rgb = COLORS[close[0]]
    return tuple(int(c * scale) for c in rgb)


def _conflicting_apps() -> list[str]:
    import psutil

    names = {"rgbfusion": "Gigabyte RGB Fusion", "aorus": "AORUS Engine", "armourycrate": "ASUS Armoury Crate",
             "lightingservice": "ASUS Aura", "icue": "Corsair iCUE", "signalrgb": "SignalRGB",
             "mysticlight": "MSI Mystic Light"}
    exact = {"gcc": "Gigabyte Control Center", "gigabytecontrolcenter": "Gigabyte Control Center",
             "msi center": "MSI Center"}       # exact names: "gcc" is also inside Intel's "OneApp.IGCC"
    found = set()
    for p in psutil.process_iter(["name"]):
        n = (p.info["name"] or "").lower().removesuffix(".exe")
        found.update(label for key, label in names.items() if key in n)
        if n in exact:
            found.add(exact[n])
    return sorted(found)


def openrgb_path(cfg=None) -> str | None:
    """OpenRGB.exe from config, the usual install folders, or a portable copy in C:\\Tools\\OpenRGB."""
    import os

    r = (cfg.get("rgb") or {}) if cfg is not None else {}
    candidates = [r.get("openrgb_path"),
                  r"C:\Tools\OpenRGB\OpenRGB.exe",
                  os.path.expandvars(r"%ProgramFiles%\OpenRGB\OpenRGB.exe"),
                  os.path.expandvars(r"%LOCALAPPDATA%\Programs\OpenRGB\OpenRGB.exe")]
    return next((c for c in candidates if c and Path(c).is_file()), None)


def _client(cfg=None, start: bool = True):
    from openrgb import OpenRGBClient

    r = (cfg.get("rgb") or {}) if cfg is not None else {}
    host, port = r.get("host", "127.0.0.1"), int(r.get("port", 6742))
    try:
        return OpenRGBClient(host, port, name="Jarvis")
    except (ConnectionRefusedError, TimeoutError, OSError):
        pass
    # not running: start it ourselves (minimized, SDK server on) if it's installed
    exe = openrgb_path(cfg)
    if exe and start and r.get("auto_start", True) and host in ("127.0.0.1", "localhost"):
        import subprocess
        import time

        log.info("starting OpenRGB: %s", exe)
        subprocess.Popen([exe, "--server", "--server-port", str(port), "--startminimized"],
                         cwd=str(Path(exe).parent), creationflags=0x08000000)
        for _ in range(30):                 # it scans devices before the server answers
            time.sleep(0.5)
            try:
                return OpenRGBClient(host, port, name="Jarvis")
            except (ConnectionRefusedError, TimeoutError, OSError):
                continue
    if not exe:
        msg = "OpenRGB isn't installed, and it's how I control the lights. Get it from openrgb.org."
    else:
        msg = "OpenRGB didn't start its SDK server, so I can't reach the lights."
    clash = _conflicting_apps()
    if clash:
        many = len(clash) > 1
        msg += (f" {' and '.join(clash)} {'are' if many else 'is'} also running; close {'them' if many else 'it'} "
                "before using OpenRGB, because they'd both try to control the same chips.")
    if exe:
        msg += " Open OpenRGB, turn on Settings > SDK server, and ask me again."
    _fail(msg)


def _pick(devices, name: str):
    if not name or name.lower() in ("all", "everything", "pc", "computer", "lights"):
        return list(devices)
    q = name.lower()
    aliases = {"ram": "dram", "memory": "dram", "motherboard": "", "board": "", "keyboard": "keyboard",
               "mouse": "mouse", "fans": "fan", "graphics card": "gpu", "gpu": "gpu", "ssd": "ssd"}
    key = aliases.get(q, q)
    hits = [d for d in devices if key and key in d.name.lower()]
    if not hits and q in ("motherboard", "board"):
        from openrgb.utils import DeviceType

        hits = [d for d in devices if d.type == DeviceType.MOTHERBOARD]
    if not hits and q in ("ram", "memory"):
        from openrgb.utils import DeviceType

        hits = [d for d in devices if d.type == DeviceType.DRAM]
    return hits


def _mode_names(device) -> list[str]:
    return [m.name for m in device.modes]


BOARD_WORDS = ("motherboard", "board", "mobo")
ALL_WORDS = ("", "all", "everything", "pc", "computer", "lights")


def _fusion_chip():
    from .. import fusion

    try:
        return fusion.find()
    except fusion.NotFound as e:
        log.debug("No direct RGB Fusion chip: %s", e)
    except Exception as e:  # noqa: BLE001  (HID quirks: fall back to OpenRGB)
        log.warning("RGB Fusion chip lookup failed: %s", e)
    return None


def _speed_words(text: str) -> tuple[str, int | None]:
    t = text.lower().strip()
    for word, speed in (("very slow ", 1), ("slow ", 2), ("slower ", 3), ("fast ", 8), ("faster ", 8),
                        ("very fast ", 10), ("quick ", 8)):
        if t.startswith(word):
            return t[len(word):], speed
    return t, None


def _brightness(text: str, now: int) -> int:
    t = text.lower().replace("percent", "").replace("%", "").strip()
    if t in ("up", "brighter", "more", "higher"):
        return min(100, now + 25)
    if t in ("down", "dimmer", "less", "lower", "dim"):
        return max(5, now - 25)
    if t in ("max", "full", "maximum", "100"):
        return 100
    if t in ("low", "min", "minimum"):
        return 10
    if t in ("half", "medium"):
        return 50
    m = re.search(r"\d{1,3}", t)
    if not m:
        _fail("How bright? Say a percentage, like 50 percent.")
    return max(0, min(100, int(m.group())))


def _fusion(action: str, value: str, chip) -> str:
    from .. import fusion

    look = fusion.current()
    name = chip.name
    if action == "status":
        return Spoken(f"I control your {name} motherboard lights directly: {look.effect}, "
                      f"{look.brightness} percent, {'on' if look.on else 'off'}.")
    if action == "off":
        look.on = False
        fusion.apply(look, chip)
        return "Lights off."
    if action in ("color", "on"):
        if value or action == "color":
            try:
                look.color = parse_color(value or "white")
            except ValueError as e:
                _fail(str(e))
            if look.effect in ("color cycle", "random"):
                look.effect = "static"
        look.on = True
        fusion.apply(look, chip)
        return f"Lights set to {value}." if value else "Lights on."
    if action == "effect":
        wanted, speed = _speed_words(value)
        effect = fusion.effect_name(wanted)
        if not effect:
            _fail(f"Your board's lights can't do {value}. They can do: {', '.join(fusion.EFFECTS)}.")
        look.effect, look.on = effect, True
        if speed:
            look.speed = speed
        fusion.apply(look, chip)
        return f"{effect.capitalize()} on."
    if action == "brightness":
        look.brightness, look.on = _brightness(value, look.brightness), True
        fusion.apply(look, chip)
        return f"Lights at {look.brightness} percent."
    if action == "profile":
        saved = fusion.profiles()
        hit = next((k for k in saved if k.lower() == value.lower()), None) or next(
            (k for k in saved if k.lower() in difflib.get_close_matches(value.lower(), [s.lower() for s in saved],
                                                                       n=1, cutoff=0.5)), None)
        if not hit:
            _fail(f"No lighting profile called {value}. Profiles: {', '.join(saved) or 'none saved'}.")
        fusion.apply(saved[hit], chip)
        return f"Loaded the {hit} lighting profile."
    if action == "save_profile":
        if not value:
            _fail("What should I call the profile?")
        fusion.save_profile(value, look)
        return f"Saved the current lighting as {value}."
    _fail(f"I can't do {action} with the lights.")


def open_panel():
    import subprocess
    import sys
    from pathlib import Path

    exe = Path(sys.executable)
    quiet = exe.with_name("pythonw.exe")
    subprocess.Popen([str(quiet if quiet.exists() else exe), "-m", "jarvis", "--lights"],
                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


@tool("Control the PC's RGB lighting: the Gigabyte RGB Fusion motherboard lights directly, plus RAM, fans, GPU "
      "and others through OpenRGB. Set a colour ('red', 'dim blue', 'warm white'), an effect (rainbow, "
      "breathing, flashing, static; 'slow breathing'), brightness (a percent, up, down), turn it off/on, load or "
      "save a lighting profile, list devices, or open the on-screen lights panel.",
      {"action": {"type": "string",
                  "enum": ["color", "effect", "brightness", "off", "on", "profile", "save_profile", "status", "panel"]},
       "value": {"type": "string", "description": "colour, effect, brightness or profile name"},
       "device": {"type": "string", "description": "optional: ram, motherboard, fans, gpu, keyboard... default all"}},
      required=["action"],
      triggers=("rgb", "light", "lighting", "led", "colour", "color", "rainbow", "breathing", "glow", "profile",
                "fusion", "gigabyte", "brightness"))
def rgb(action: str, value: str = "", device: str = "", ctx=None):
    cfg = ctx.cfg if ctx else None
    if action == "panel":
        open_panel()
        return "Opening the lights panel."
    backend = ((cfg.get("rgb") or {}) if cfg is not None else {}).get("backend", "auto")
    where = (device or "").lower().strip()
    chip = _fusion_chip() if backend != "openrgb" and (where in ALL_WORDS or where in BOARD_WORDS) else None
    if chip:
        from .builtin import ToolFailed

        try:
            said = _fusion(action, value, chip)
        except ToolFailed:
            raise
        except Exception as e:  # noqa: BLE001  (the chip didn't take it: OpenRGB may still manage)
            log.warning("Direct RGB Fusion control failed, trying OpenRGB: %s", e)
            said = None
        finally:
            chip.close()
        if said is not None:
            if where in ALL_WORDS and backend == "auto" and action not in ("status", "profile", "save_profile"):
                _openrgb_quietly(action, value, cfg)       # RAM, fans... if OpenRGB happens to be running
            return Spoken(said)
    if action == "brightness":
        _fail("I can only change brightness on the Gigabyte motherboard lights.")
    return _openrgb(action, value, device, _client(cfg))


def _openrgb_quietly(action: str, value: str, cfg):
    from openrgb.utils import DeviceType

    try:
        client = _client(cfg, start=False)
    except Exception:  # noqa: BLE001  (OpenRGB not running: the board was enough)
        return
    try:
        others = [d for d in client.devices if d.type != DeviceType.MOTHERBOARD]
        if others:
            _openrgb(action, value, "", client, others)
    except Exception as e:  # noqa: BLE001
        log.info("OpenRGB devices skipped: %s", e)


def _openrgb(action: str, value: str, device: str, client, targets=None):
    from openrgb.utils import RGBColor

    try:
        devices = client.devices
        if action == "status":
            if not devices:
                return Spoken("OpenRGB is running but hasn't found any RGB devices.")
            return Spoken("I can control: " + ", ".join(d.name for d in devices) + ".")
        if action == "profile":
            profiles = [p.name for p in client.profiles]
            hit = next((p for p in profiles if p.lower() == value.lower()), None) or next(
                iter(difflib.get_close_matches(value.lower(), [p.lower() for p in profiles], n=1, cutoff=0.5)), None)
            if not hit:
                _fail(f"No lighting profile called {value}. Profiles: {', '.join(profiles) or 'none saved'}.")
            real = next(p for p in profiles if p.lower() == hit.lower())
            client.load_profile(real)
            return Spoken(f"Loaded the {real} lighting profile.")
        if action == "save_profile":
            if not value:
                _fail("What should I call the profile?")
            client.save_profile(value)
            return Spoken(f"Saved the current lighting as {value}.")
        targets = targets or _pick(devices, device)
        if not targets:
            _fail(f"I don't see {device} in OpenRGB. I have: {', '.join(d.name for d in devices)}.")
        if action == "off":
            for d in targets:
                d.off()
            return Spoken("Lights off." if len(targets) == len(devices) else f"{device.title()} lights off.")
        if action in ("color", "on"):
            try:
                rgb_value = parse_color(value or "white") if action == "color" or value else (255, 255, 255)
            except ValueError as e:
                _fail(str(e))
            for d in targets:
                try:
                    if "Direct" in _mode_names(d):
                        d.set_mode("Direct")
                    elif "Static" in _mode_names(d):
                        d.set_mode("Static")
                except Exception:  # noqa: BLE001
                    pass
                d.set_color(RGBColor(*rgb_value))
            label = value or "white"
            return Spoken(f"Lights set to {label}." if len(targets) == len(devices) else
                          f"{device.title()} set to {label}.")
        if action == "effect":
            wanted = value.lower().replace(" mode", "").strip()
            synonyms = {"rainbow": ["rainbow", "spectrum cycle", "rainbow wave", "color cycle", "colour cycle"],
                        "breathing": ["breathing", "breathe", "pulse"], "static": ["static", "direct"],
                        "flashing": ["flashing", "flash", "strobe"], "wave": ["wave", "color wave", "rainbow wave"],
                        "cycle": ["color cycle", "spectrum cycle", "colour cycle"]}
            options = synonyms.get(wanted, [wanted])
            done = []
            for d in targets:
                names = _mode_names(d)
                lower = [n.lower() for n in names]
                match = next((names[lower.index(o)] for o in options if o in lower), None) or next(
                    (names[lower.index(c)] for c in difflib.get_close_matches(wanted, lower, n=1, cutoff=0.6)), None)
                if match:
                    d.set_mode(match)
                    done.append(d.name)
            if not done:
                modes = sorted({m for d in targets for m in _mode_names(d)})
                _fail(f"None of the lights have a {value} effect. They support: {', '.join(modes[:10])}.")
            return Spoken(f"{value.capitalize()} effect on {len(done)} device{'s' * (len(done) != 1)}.")
        _fail(f"I can't do {action} with the lights.")
    finally:
        try:
            client.disconnect()
        except Exception:  # noqa: BLE001
            pass


def route_rgb(t: str, has) -> list[tuple[str, dict]] | None:
    if not has("rgb"):
        return None
    noun = (r"(?:rgb fusion|gigabyte rgb|gigabyte lights?|fusion|rgb lights?|rgb|lights?|lighting|leds?|pc lights|"
            r"computer lights|motherboard lights?)")
    if re.fullmatch(r"(?:open |show )(?:the |my )?(?:rgb fusion|gigabyte|rgb|lights?|lighting) ?(?:panel|controller|"
                    r"control panel|controls|app)", t):
        return [("rgb", {"action": "panel"})]
    level = r"(?P<v>\d{1,3}(?: ?%| percent)?|max|full|half|up|down)"
    m = re.fullmatch(r"(?:set |change |put |turn )?(?:the |my )?" + noun + r" brightness(?: to| at)? " + level, t) \
        or re.fullmatch(r"(?:set |change )?(?:the )?brightness of (?:the |my )?" + noun + r"(?: to| at)? " + level, t) \
        or re.fullmatch(r"(?:set |put |turn )?(?:the |my )?" + noun + r" (?:to|at) (?P<v>\d{1,3}(?: ?%| percent))", t)
    if m:
        return [("rgb", {"action": "brightness", "value": m.group("v")})]
    if re.fullmatch(r"(?:dim|lower) (?:the |my )?" + noun + r"|(?:make )?(?:the |my )?" + noun + r" dimmer", t):
        return [("rgb", {"action": "brightness", "value": "down"})]
    if re.fullmatch(r"brighten (?:the |my )?" + noun + r"|(?:make )?(?:the |my )?" + noun + r" brighter", t):
        return [("rgb", {"action": "brightness", "value": "up"})]
    m = re.fullmatch(r"(?:set |make |change |turn )?(?:the |my )?(?:(?P<dev>ram|memory|motherboard|fans|gpu|keyboard) )?"
                     + noun + r"(?: to)? (?P<c>[a-z #0-9 ]+)", t) \
        or re.fullmatch(r"(?:make|turn|set) (?:the |my )?" + noun + r" (?P<c>[a-z ]+?)", t)
    if m:
        c = m.group("c").strip()
        dev = m.groupdict().get("dev") or ""
        if c in ("off", "out"):
            return [("rgb", {"action": "off", "device": dev})]
        if c == "on":
            return [("rgb", {"action": "on", "device": dev})]
        effect = re.sub(r"\s*(?:mode|effect)$", "", c)
        if re.sub(r"^(?:very )?(?:slow|fast|quick) ", "", effect) in (
                "rainbow", "breathing", "static", "wave", "cycle", "flashing", "spectrum cycle", "color cycle",
                "double flash", "random", "pulse", "pulsing"):
            return [("rgb", {"action": "effect", "value": effect, "device": dev})]
        try:
            parse_color(c)
            return [("rgb", {"action": "color", "value": c, "device": dev})]
        except ValueError:
            pass          # not a colour: maybe "... lighting profile" below
    if re.fullmatch(r"(?:turn )?(?:off|kill) (?:the |my )?" + noun + r"|" + noun + r" off", t):
        return [("rgb", {"action": "off"})]
    m = re.fullmatch(r"(?:load |use )?(?:the )?(?P<p>[a-z0-9 ]+) (?:lighting|rgb) profile", t)
    if m:
        return [("rgb", {"action": "profile", "value": m.group("p")})]
    return None
