"""Per-app volume (the Windows volume mixer) and "ducking" the music while Jarvis listens and talks."""
from __future__ import annotations

import contextlib
import logging
import os
import threading

log = logging.getLogger(__name__)

ALIASES = {"spotify's": "spotify", "youtube": "chrome", "browser": "chrome", "google chrome": "chrome",
           "edge": "msedge", "microsoft edge": "msedge", "media player": "microsoft.media.player",
           "discord's": "discord", "steam's": "steam", "games": "steam", "the game": "steam"}
MEDIA_WORDS = {"music", "the music", "song", "the song", "media", "the media", "player", "it"}
OWN = {os.path.basename(p).lower() for p in ("python.exe", "pythonw.exe")}


@contextlib.contextmanager
def _com():
    import comtypes

    comtypes.CoInitialize()
    try:
        yield
    finally:
        comtypes.CoUninitialize()


def _sessions():
    from pycaw.pycaw import AudioUtilities

    out = []
    for s in AudioUtilities.GetAllSessions():
        if s.Process is None or s.SimpleAudioVolume is None:
            continue
        try:
            name = s.Process.name().lower().removesuffix(".exe")
        except Exception:  # noqa: BLE001  (process exited)
            continue
        out.append((name, s.SimpleAudioVolume, s.Process.pid))
    return out


def _playing_apps() -> list[str]:
    """Process-name keywords of the apps Windows says are playing media right now."""
    try:
        from . import media

        ids = [s.app.lower() for s in media.sessions() if s.status == "playing"]
    except Exception:  # noqa: BLE001
        return []
    return ids


def resolve(app: str) -> str:
    a = app.lower().strip().removesuffix(" app").removesuffix("'s").strip()
    return ALIASES.get(a, a)


def _matching(app: str):
    """Audio sessions for an app name; 'music' / 'it' = whatever is playing now."""
    key = resolve(app)
    sessions = [s for s in _sessions() if s[0] + ".exe" not in OWN and s[2] != os.getpid()]
    if key in MEDIA_WORDS or not key:
        playing = _playing_apps()
        return [s for s in sessions if any(s[0] in pid for pid in playing)], "the music"
    hits = [s for s in sessions if key.replace(" ", "") in s[0].replace(" ", "")]
    return hits, key.title()


def control(app: str, action: str, percent: int | None = None) -> tuple[bool, str]:
    """action: get | set | up | down | mute | unmute. Reads the real level back afterwards."""
    with _com():
        hits, label = _matching(app)
        if not hits:
            if label == "the music":
                return False, "Nothing is playing, so I can't tell which app's volume you mean."
            return False, f"{label} isn't making any sound right now, so it has no volume to change."
        vol = hits[0][1]
        current = round(vol.GetMasterVolume() * 100)
        if action == "get":
            muted = " (muted)" if vol.GetMute() else ""
            return True, f"{label} is at {current}%{muted}."
        if action in ("mute", "unmute"):
            for _, v, _ in hits:
                v.SetMute(1 if action == "mute" else 0, None)
            ok = bool(vol.GetMute()) == (action == "mute")
            return ok, f"{label} {'muted' if action == 'mute' else 'unmuted'}." if ok else f"{label} didn't {action}."
        step = percent if percent is not None else 15
        target = {"set": step, "up": current + step, "down": current - step}[action]
        target = max(0, min(100, int(target)))
        for _, v, _ in hits:
            v.SetMute(0, None)
            v.SetMasterVolume(target / 100, None)
        now = round(vol.GetMasterVolume() * 100)
        return now == target, f"{label} volume is now {now}%."


class Ducker:
    """Turns playing apps down while Jarvis is listening/speaking, then restores exactly what it changed.
    If you move an app's volume yourself in the meantime, that app is left alone."""

    def __init__(self, level: float = 0.3):
        self.level = level            # fraction of the original volume while ducked
        # pid -> (original level, level we set). COM objects aren't kept: they're only valid inside
        # the CoInitialize block that created them, so sessions are looked up again when restoring.
        self._saved: dict[int, tuple[float, float]] = {}
        self._lock = threading.Lock()

    @property
    def active(self) -> bool:
        return bool(self._saved)

    def duck(self):
        if self.level >= 1:
            return
        with self._lock, _com():
            if self._saved:
                return
            playing = _playing_apps()
            for name, vol, pid in _sessions():
                if pid == os.getpid() or not any(name in app for app in playing):
                    continue
                try:
                    original = vol.GetMasterVolume()
                    lowered = original * self.level
                    vol.SetMasterVolume(lowered, None)
                    self._saved[pid] = (original, lowered)
                except Exception:  # noqa: BLE001
                    pass
            if self._saved:
                log.debug("ducked %d app(s)", len(self._saved))

    def restore(self):
        with self._lock, _com():
            if not self._saved:
                return
            for _, vol, pid in _sessions():
                if pid not in self._saved:
                    continue
                original, lowered = self._saved[pid]
                try:
                    if abs(vol.GetMasterVolume() - lowered) < 0.02:   # untouched since we lowered it
                        vol.SetMasterVolume(original, None)
                except Exception:  # noqa: BLE001  (app closed)
                    pass
            self._saved.clear()
