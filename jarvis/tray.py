"""System tray app: Jarvis runs in the background with a status icon and a right-click menu."""
from __future__ import annotations

import ctypes
import logging
import os
import sys
import threading
from pathlib import Path

from .config import ROOT, Section, resolve_path
from .tts import VOICE_PRESETS

log = logging.getLogger(__name__)

STATE_COLOURS = {
    "loading": (120, 120, 120), "idle": (40, 120, 220), "listening": (40, 200, 90),
    "thinking": (235, 170, 30), "speaking": (0, 200, 220), "paused": (90, 90, 90), "error": (220, 50, 50),
}
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
# tray "Lights (RGB)" menu: one click per look, through OpenRGB (e.g. a Gigabyte RGB Fusion 2 board)
LIGHT_PRESETS = [("Jarvis blue", "color", "jarvis blue"), ("Iron Man red", "color", "iron man red"),
                 ("Purple", "color", "purple"), ("Warm white", "color", "warm white"),
                 ("Dim blue", "color", "dim blue"), ("Rainbow", "effect", "rainbow"),
                 ("Breathing", "effect", "breathing"), ("Off", "off", "")]
APP_NAME = "Jarvis"


def _icon_image(state: str):
    from PIL import Image, ImageDraw

    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    colour = STATE_COLOURS.get(state, STATE_COLOURS["idle"])
    d.ellipse((4, 4, size - 4, size - 4), outline=colour + (255,), width=7)       # arc-reactor ring
    d.ellipse((22, 22, size - 22, size - 22), fill=colour + (255,))
    return img


# ------------------------------------------------------------ start with Windows

def _launch_command() -> str:
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    return f'"{pythonw}" "{ROOT / "jarvis_tray.pyw"}"'


def autostart_enabled() -> bool:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, APP_NAME)
            return True
    except OSError:
        return False


def set_autostart(enabled: bool):
    """Adds/removes Jarvis under HKCU\\...\\Run (current user only, no admin needed)."""
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, _launch_command())
        else:
            try:
                winreg.DeleteValue(key, APP_NAME)
            except FileNotFoundError:
                pass


def _single_instance() -> bool:
    # a named mutex: if another tray Jarvis already holds it, don't start a second one
    ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\JarvisVoiceAssistant")
    return ctypes.windll.kernel32.GetLastError() != 183  # ERROR_ALREADY_EXISTS


class TrayApp:
    BRAINS = [("Local (Ollama)", "ollama"), ("Claude", "claude"), ("LM Studio", "lmstudio"),
              ("OpenAI-compatible", "openai_compat")]

    def __init__(self, cfg: Section, llm: str | None = None, tts: str | None = None):
        import pystray

        self.cfg = cfg
        self.llm, self.tts = llm, tts
        self.assistant = None
        self.state = "loading"
        self._models: list[dict] = []          # installed Ollama models, for the "Ollama model" submenu
        self._models_note = "Looking for models..."
        menu = pystray.Menu(
            pystray.MenuItem(lambda _: self._status_text(), None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Pause listening", self._toggle_pause, checked=lambda _: self._paused()),
            pystray.MenuItem("Stop talking", self._hush),
            pystray.MenuItem("Brain", pystray.Menu(*[
                pystray.MenuItem(label, self._brain_setter(key), radio=True,
                                 checked=lambda _, k=key: self._brain() == k)
                for label, key in self.BRAINS],
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Ollama model", pystray.Menu(self._model_items)))),
            pystray.MenuItem("Lights (RGB)", pystray.Menu(*[
                pystray.MenuItem(label, self._rgb_setter(action, value))
                for label, action, value in LIGHT_PRESETS])),
            pystray.MenuItem("Speed vs quality", pystray.Menu(*[
                pystray.MenuItem(label, self._perf_setter(key), radio=True,
                                 checked=lambda _, k=key: (self.cfg.get("performance") or "balanced") == k)
                for label, key in (("Fast", "fast"), ("Balanced", "balanced"), ("Quality", "quality"))])),
            pystray.MenuItem("Voice", pystray.Menu(*[
                pystray.MenuItem(f"{name} ({p['desc']})", self._voice_setter(name), radio=True,
                                 checked=lambda _, n=name: self._voice() == n)
                for name, p in VOICE_PRESETS.items()])),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Start with Windows", self._toggle_autostart, checked=lambda _: autostart_enabled()),
            pystray.MenuItem("Open settings", lambda: self._open(ROOT / "config.yaml")),
            pystray.MenuItem("Open notes", lambda: self._open(resolve_path(cfg.tools.get("notes_file", "data/notes.md")))),
            pystray.MenuItem("Open log", lambda: self._open(ROOT / "data" / "jarvis.log")),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", self._quit),
        )
        self.icon = pystray.Icon("jarvis", _icon_image("loading"), "Jarvis: loading...", menu)

    # ------------------------------------------------------------ state

    def _status_text(self) -> str:
        brain = self.assistant.agent.provider.label if self.assistant else "starting"
        return f"Jarvis: {self.state} ({brain})"

    def _on_state(self, state: str):
        self.state = state
        self.icon.icon = _icon_image(state)
        self.icon.title = f"Jarvis: {state}"
        self.icon.update_menu()

    def _paused(self) -> bool:
        return bool(self.assistant and self.assistant.paused.is_set())

    def _brain(self) -> str | None:
        return self.assistant.agent.provider.name if self.assistant else None

    # ------------------------------------------------------------ menu actions

    def _toggle_pause(self, *_):
        if self.assistant:
            ev = self.assistant.paused
            ev.clear() if ev.is_set() else ev.set()

    def _hush(self, *_):
        if self.assistant:
            self.assistant.hush.set()
            self.assistant.speaker.stop()

    def _brain_setter(self, key: str):
        def set_brain(*_):
            if not self.assistant:
                return

            def work():
                msg = self.assistant.agent.switch(key)
                self.icon.notify(msg, "Jarvis")
                self.icon.update_menu()
            threading.Thread(target=work, daemon=True).start()
        return set_brain

    def _model(self) -> str | None:
        p = self.assistant.agent.provider if self.assistant else None
        return p.model if p is not None and p.name == "ollama" else None

    def _model_items(self):
        """The "Ollama model" submenu, rebuilt from the last list (pystray builds menus ahead of time on
        Windows, so the list is fetched at start-up, after a switch, and with "Refresh list")."""
        import pystray

        from .llm.ollama_models import features

        if not self._models:
            yield pystray.MenuItem(self._models_note, None, enabled=False)
        for m in self._models:
            extra = ", ".join(x for x in (m["params"], features(m)) if x)
            yield pystray.MenuItem(f"{m['name']} ({extra})" if extra else m["name"], self._model_setter(m["name"]),
                                   radio=True, checked=lambda _, n=m["name"]: self._model() == n)
        yield pystray.Menu.SEPARATOR
        yield pystray.MenuItem("Refresh list", lambda *_: self._refresh_models())

    def _refresh_models(self):
        def work():
            from .llm.ollama_models import OllamaUnavailable, is_chat, list_models, ollama_url

            try:
                self._models = [m for m in list_models(ollama_url(self.cfg)) if is_chat(m)]
                self._models_note = "No models installed (ollama pull ...)"
            except OllamaUnavailable:
                self._models, self._models_note = [], "Ollama isn't running"
            self.icon.update_menu()
        threading.Thread(target=work, daemon=True).start()

    def _model_setter(self, name: str):
        def set_model(*_):
            if not self.assistant:
                return

            def work():
                msg = self.assistant.agent.switch("ollama", model=name)   # also saved for next time
                self.icon.notify(msg[:250], "Jarvis")
                self._refresh_models()
            threading.Thread(target=work, daemon=True).start()
        return set_model

    def _rgb_setter(self, action: str, value: str):
        def set_lights(*_):
            def work():
                from .tools.builtin import ToolFailed
                from .tools.rgb_tools import rgb

                try:
                    msg = rgb(action, value, ctx=self.assistant.agent.toolbox if self.assistant else None)
                except ToolFailed as e:
                    msg = str(e)
                except Exception as e:  # noqa: BLE001
                    msg = f"Lights: {e}"
                self.icon.notify(msg[:250], "Jarvis lights")
            threading.Thread(target=work, daemon=True).start()
        return set_lights

    def _perf_setter(self, key: str):
        def set_perf(*_):
            from .config import save_local
            from .hardware import performance_settings

            save_local(performance_settings(key))
            self.cfg["performance"] = key
            self.icon.notify(f"Speed vs quality set to {key}. Quit and start Jarvis again to apply.", "Jarvis")
            self.icon.update_menu()
        return set_perf

    def _voice(self) -> str | None:
        return getattr(self.assistant.tts, "preset", None) if self.assistant else None

    def _voice_setter(self, name: str):
        def set_voice(*_):
            if not self.assistant:
                return

            def work():
                msg = self.assistant.switch_voice(name)
                self.icon.notify(msg, "Jarvis")
                self.icon.update_menu()
                self.assistant.announcements.put(msg)   # say it in the new voice
            threading.Thread(target=work, daemon=True).start()
        return set_voice

    def _toggle_autostart(self, *_):
        try:
            set_autostart(not autostart_enabled())
        except OSError as e:
            self.icon.notify(f"Couldn't change startup setting: {e}", "Jarvis")
        self.icon.update_menu()

    @staticmethod
    def _open(path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.touch()
        os.startfile(str(path))

    def _quit(self, *_):
        if self.assistant:
            self.assistant.stop()
        self.icon.stop()

    # ------------------------------------------------------------ run

    def _worker(self):
        from .app import VoiceAssistant

        try:
            self.assistant = VoiceAssistant(self.cfg, self.llm, self.tts, on_state=self._on_state,
                                            on_announce=lambda m: self.icon.notify(m, "Jarvis"))
            self._refresh_models()
            self.icon.notify('Say "Hey Jarvis".', "Jarvis is online")
            self.assistant.run()
        except Exception as e:
            log.exception("Jarvis crashed")
            self._on_state("error")
            self.icon.notify(f"Jarvis stopped: {e}", "Jarvis")

    def run(self):
        if not _single_instance():
            log.info("Jarvis is already running in the tray")
            return
        threading.Thread(target=self._worker, daemon=True).start()
        self.icon.run()


def run_tray(cfg: Section, llm: str | None = None, tts: str | None = None):
    TrayApp(cfg, llm, tts).run()
