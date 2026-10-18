"""Voice loop: wake word -> record -> transcribe -> agent -> speak -> short follow-up window."""
from __future__ import annotations

import logging
import queue
import re
import threading
import time
from typing import Callable

from .agent import Agent
from .audio import CHIME_DONE, CHIME_ERROR, CHIME_LISTEN, CHIME_SR, Microphone, Speaker
from .config import Section
from .recorder import CommandRecorder
from .stt import Transcriber
from .config import save_local
from .tts import VOICE_PRESETS, SentenceSplitter, clean_for_speech, create_tts, resolve_preset
from .wakeword import WakeWordDetector

log = logging.getLogger(__name__)

CANCEL = re.compile(r"^(never ?mind|cancel|stop|forget it|nothing|shut up|be quiet|quiet|stop talking|"
                    r"enough|that'?s enough|stop it|silence)\W*$", re.I)
GOODBYE = re.compile(r"^(thanks?( you)?|that'?s all|goodbye|bye|go to sleep|dismissed)\W*( jarvis)?\W*$", re.I)
YES = re.compile(r"\b(yes|yeah|yep|sure|go ahead|do it|confirm|affirmative|please do|okay|ok)\b", re.I)
# the wake word fires before "...vis" ends, so the recording can start with the tail of it
WAKE_PREFIX = re.compile(r"^\W*((hey|hi|hay|hate|a|ok|okay)\W+)?(jarvis|travis|vis)\b\W*", re.I)
WAKE_TAILS = {"vis", "is", "this", "his", "hey", "jarvis"}
# Whisper sometimes "hears" these in silence or noise
HALLUCINATIONS = {"", "you", "thank you.", "thanks for watching!", "bye.", ".", "thank you for watching."}


class _Confirmation:
    """A yes/no question the agent thread hands to the speaking thread."""

    def __init__(self, question: str):
        self.question = question
        self.answer = False
        self.done = threading.Event()


def _media_playing() -> bool:
    """Is any app playing sound through Windows' media controls (Spotify, a YouTube video, VLC...)?"""
    try:
        from . import media

        return any(s.status == "playing" for s in media.sessions())
    except Exception:  # noqa: BLE001  (not Windows, or the media API is unavailable)
        return False


def say(text: str):
    print(f"\033[96mJarvis:\033[0m {text}", flush=True)


class VoiceAssistant:
    """States reported to on_state listeners (the tray icon): loading, idle, listening, thinking, speaking, paused."""

    def __init__(self, cfg: Section, llm: str | None = None, tts: str | None = None,
                 on_state: Callable[[str], None] | None = None,
                 on_announce: Callable[[str], None] | None = None):
        self.cfg = cfg
        self.on_state = on_state or (lambda s: None)
        self.on_announce = on_announce or (lambda m: None)   # e.g. a Windows notification from the tray
        self.state = "loading"
        self.on_state(self.state)
        self.announcements: "queue.Queue[str]" = queue.Queue()
        self.stop_event = threading.Event()     # quit
        self.paused = threading.Event()         # tray "Pause listening"
        self.hush = threading.Event()           # tray "Stop talking"
        self._active = None                     # (sentence queue, splitter) of the reply being spoken
        self._voice_thread = threading.current_thread()   # the thread that owns the mic (set again in run)
        self._heard_at: float | None = None               # when the last command finished transcribing
        self.hotkey = None
        combo = cfg.get("hotkey")
        if combo:
            from .hotkey import Hotkey

            try:
                self.hotkey = Hotkey(combo).start()
            except ValueError as e:
                log.warning("hotkey disabled: %s", e)
        bi = cfg.get("barge_in") or {}
        self.barge_in = bool(bi.get("enabled", True))
        # higher than the normal threshold: Jarvis's own voice leaks into the mic on open speakers
        self.barge_threshold = float(bi.get("threshold", 0.8))
        # videos and songs say things that sound like "Jarvis": be stricter while something is playing
        self.media_threshold = float(cfg.wake_word.get("threshold_while_media", 0.7))
        self._media_playing = False
        print("Loading... (first run downloads the speech models)")
        self.mic = Microphone(cfg.audio.get("input_device"))
        self.speaker = Speaker(cfg.audio.get("output_device"))
        self.tts = create_tts(cfg.tts, tts)
        self.stt = Transcriber(**{k: v for k, v in cfg.stt.items()
                                  if k in ("model", "device", "compute_type", "language", "download_root", "hints")})
        ww = cfg.wake_word
        self.wake = WakeWordDetector(ww.get("model", "hey_jarvis"), ww.get("threshold", 0.5),
                                     ww.get("cooldown_seconds", 1.5))
        rec = cfg.recording
        self.recorder = CommandRecorder(self.mic, rec.get("vad_aggressiveness", 2), rec.get("silence_ms", 800),
                                        rec.get("start_timeout_s", 6), rec.get("max_seconds", 20))
        duck = cfg.get("ducking") or {}
        self.ducker = None
        if duck.get("enabled", True):
            from .appvolume import Ducker

            self.ducker = Ducker(float(duck.get("level", 0.3)))
        self.agent = Agent(cfg, notify=self._announce, confirm=self.confirm, provider=llm,
                           speak=self._speak_from_tool, switch_voice=self.switch_voice,
                           unduck=self._unduck)
        from .routines import Scheduler

        self.scheduler = Scheduler(cfg, self._run_scheduled).start()
        from .tools.github_tools import start_watcher

        self.github_watch = start_watcher(cfg, self._announce)   # spoken alert when a watched build fails
        if (cfg.get("chrome") or {}).get("enabled", True):
            try:
                from . import chrome_bridge

                chrome_bridge.get(cfg)       # listen now, so the Chrome extension is linked before you ask
            except Exception:  # noqa: BLE001  (websockets missing: Chrome tools fall back to shortcuts)
                log.warning("Chrome link unavailable", exc_info=True)

    def _run_scheduled(self, name: str):
        """A routine's scheduled time came: run it through the brain and announce the briefing."""
        reply = self.agent.handle(f"Run my '{name}' routine now and brief me.")
        if reply:
            self._announce(reply)

    def switch_voice(self, name: str, remember: bool = True) -> str:
        """Load another voice preset (from the tool, the tray or code) and remember it for next time."""
        preset = resolve_preset(name)
        if not preset:
            return f"I don't have a voice called {name}. Options: {', '.join(VOICE_PRESETS)}."
        new = create_tts(self.cfg.tts, preset)
        self.tts = new
        if remember:
            save_local({"tts": {"voice": new.preset}})
        if new.preset != preset:
            return f"The {preset} voice needs an NVIDIA GPU, so I'm using {new.preset} instead."
        return f"Voice changed to {preset}, {VOICE_PRESETS[preset]['desc']}."

    def _duck(self):
        if self.ducker:
            try:
                self.ducker.duck()
            except Exception:  # noqa: BLE001  (never let audio mixer trouble break a conversation)
                log.debug("ducking failed", exc_info=True)

    def _unduck(self):
        if self.ducker:
            try:
                self.ducker.restore()
            except Exception:  # noqa: BLE001
                log.debug("restoring volume failed", exc_info=True)

    def _speak_from_tool(self, text: str):
        """read_aloud: the text joins the reply that's being spoken (or is said at the next quiet moment)."""
        active = self._active
        if active is None:
            self.announcements.put(text)
            return
        q, splitter = active
        splitter.flush()
        for sentence in re.split(r"(?<=[.!?])\s+|\n+", text):
            if sentence.strip():
                q.put(sentence)

    def _announce(self, message: str):
        """Timers/reminders: spoken at the next quiet moment, plus a desktop notification if available."""
        self.announcements.put(message)
        try:
            self.on_announce(message)
        except Exception:
            log.debug("announce listener failed", exc_info=True)

    # ------------------------------------------------------------ audio helpers

    def chime(self, sound):
        # non-blocking and the mic stays open, so "Hey Jarvis, open Spotify" in one breath isn't cut
        if self.cfg.audio.get("chime", True):
            self.speaker.play(sound, CHIME_SR, blocking=False)

    def _set_state(self, state: str):
        if state != self.state:
            self.state = state
            try:
                self.on_state(state)
            except Exception:
                log.debug("state listener failed", exc_info=True)

    def speak(self, text: str) -> bool:
        """Say a complete text. Returns True if the user barged in with "Hey Jarvis"."""
        q: "queue.Queue[str | None]" = queue.Queue()
        for sentence in re.split(r"(?<=[.!?])\s+", text):
            q.put(sentence)
        q.put(None)
        return self.speak_stream(q)

    def speak_stream(self, sentences: "queue.Queue[str | None]", ack_after: float | None = None) -> bool:
        """Speak sentences as they arrive on the queue (None ends it), synthesizing the next one while
        the current one plays. The mic keeps listening for "Hey Jarvis" the whole time (barge-in),
        including while the reply is still being generated.
        Returns True if the user barged in (False when the tray said hush / quitting)."""
        self.hush.clear()
        if self.barge_in:
            self.mic.clear()           # keep listening, but only for the wake word
            self.wake.reset()
        else:
            self.mic.muted.set()       # don't let Jarvis hear (and wake) itself
        interrupted = False
        ready = None                   # synthesized audio waiting for its turn
        playing_until = 0.0
        finished = False
        spoke = False
        acked = False
        started = time.monotonic()
        try:
            while True:
                now = time.monotonic()
                if ready is not None and now >= playing_until:
                    audio, sr = ready
                    ready = None
                    self._set_state("speaking")
                    self.speaker.play(audio, sr, blocking=False)
                    playing_until = now + len(audio) / sr
                    if not spoke and self._heard_at:
                        log.info("first words %.2fs after you stopped talking", time.perf_counter() - self._heard_at)
                        self._heard_at = None
                    spoke = True
                if (ack_after and not spoke and not acked and ready is None and not finished
                        and now - started > ack_after):
                    # the brain is taking a while: say something short so it doesn't feel dead
                    acked = True
                    phrase = (self.cfg.assistant.get("ack_phrase") or "").strip()
                    ready = self.tts.synthesize(phrase) if phrase else (CHIME_LISTEN, CHIME_SR)
                    continue
                if ready is None and not finished:
                    try:
                        item = sentences.get_nowait()
                    except queue.Empty:
                        item = ""
                    if item is None:
                        finished = True
                    elif isinstance(item, _Confirmation):
                        # the agent needs a yes/no: finish the current sentence, then ask on this thread
                        while time.monotonic() < playing_until:
                            time.sleep(0.03)
                        item.answer = self._ask_yes_no(item.question)
                        item.done.set()
                        self._set_state("thinking")
                        continue
                    elif item and (text := clean_for_speech(item)):
                        say(text)
                        ready = self.tts.synthesize(text)
                        continue  # play it straight away if nothing is playing
                if finished and ready is None and time.monotonic() >= playing_until:
                    break
                if self.hush.is_set() or self.stop_event.is_set():
                    interrupted = True
                elif self._hotkey_pressed():
                    log.info("barge-in (hotkey)")
                    interrupted = True
                elif self.barge_in:
                    frame = self.mic.read(timeout=0.04)
                    if frame is not None and self.wake.process(frame, self.barge_threshold):
                        log.info("barge-in")
                        interrupted = True
                else:
                    time.sleep(0.03)
                if interrupted:
                    self.speaker.stop()
                    break
        except Exception as e:
            log.warning("TTS failed: %s", e)
        finally:
            if spoke and not interrupted:
                time.sleep(0.15)
            self.mic.clear()
            self.wake.reset()
            self.mic.muted.clear()
            self._set_state("idle")
        return interrupted and not self.hush.is_set() and not self.stop_event.is_set()

    def respond(self, text: str) -> bool:
        """Run the agent in the background and speak its reply while it's still being written.
        Returns True if the user barged in."""
        self._set_state("thinking")
        q: "queue.Queue[str | None]" = queue.Queue()
        splitter = SentenceSplitter(q.put)
        self._active = (q, splitter)

        def work():
            t0 = time.perf_counter()
            try:
                reply = self.agent.handle(text, on_text=splitter.feed)
                splitter.flush()
                if not splitter.emitted:   # provider didn't stream
                    q.put(reply or "Done.")
            except Exception:
                log.exception("agent failed")
                q.put("Sorry, something went wrong.")
            finally:
                log.debug("agent %.2fs", time.perf_counter() - t0)
                q.put(None)

        worker = threading.Thread(target=work, daemon=True, name="agent")
        worker.jarvis_queue = q   # lets confirm() tell this reply's thread from a stale one
        worker.start()
        try:
            return self.speak_stream(q, ack_after=float(self.cfg.assistant.get("ack_after_seconds") or 0) or None)
        finally:
            self._active = None

    def listen(self, timeout: float | None = None) -> str | None:
        """Record and transcribe one utterance. Leftovers of the wake word ("...vis", "Jarvis")
        don't count: keep listening until real words arrive or the timeout runs out."""
        timeout = self.cfg.recording.get("start_timeout_s", 6) if timeout is None else timeout
        deadline = time.monotonic() + timeout
        self._set_state("listening")
        while (remaining := deadline - time.monotonic()) > 0.3:
            audio = self.recorder.record(start_timeout=remaining)
            if audio is None:
                return None
            t0 = time.perf_counter()
            text = WAKE_PREFIX.sub("", self.stt.transcribe(audio)).strip()
            self._heard_at = time.perf_counter()
            log.info("heard %r (%.1fs of speech, transcribed in %.2fs)", text, len(audio) / 16000,
                     self._heard_at - t0)
            if (text.lower() in HALLUCINATIONS or not re.search(r"\w", text)
                    or re.sub(r"\W", "", text.lower()) in WAKE_TAILS):
                continue
            print(f"\033[93mYou:\033[0m {text}", flush=True)
            return text
        return None

    def _ask_yes_no(self, question: str) -> bool:
        self.speak(question)
        answer = self.listen(timeout=6) or ""
        return bool(YES.search(answer))

    def confirm(self, question: str) -> bool:
        """Called by the agent before a risky tool runs. Only one thread may use the mic at a time:
        the voice thread asks directly, the current reply's agent thread hands the question over,
        and anything else (an interrupted reply still finishing, a scheduled routine) is declined."""
        me = threading.current_thread()
        if me is self._voice_thread:
            return self._ask_yes_no(question)
        active = self._active
        if active is None or getattr(me, "jarvis_queue", None) is not active[0]:
            log.info("declined %r: no one is listening for an answer", question)
            return False
        q, splitter = active
        splitter.flush()                 # say whatever it was in the middle of first
        request = _Confirmation(question)
        q.put(request)
        if not request.done.wait(timeout=60):  # e.g. the user barged in and nobody is listening
            return False
        return request.answer

    # ------------------------------------------------------------ conversation

    def converse(self):
        self.chime(CHIME_LISTEN)
        text = self.listen()
        follow_up = float(self.cfg.assistant.get("follow_up_seconds", 6))
        while text and not self.stop_event.is_set():
            if CANCEL.match(text):
                self.speak("Very well.")
                break
            if GOODBYE.match(text):
                self.speak(f"At your service{self._address()}.")
                break
            if self.respond(text):
                # interrupted with "Hey Jarvis": take the new command right away
                self.chime(CHIME_LISTEN)
                text = self.listen()
                continue
            if follow_up <= 0:
                break
            if not self.cfg.assistant.get("follow_up_while_media", False) and _media_playing():
                break   # a video or song would be heard as the next command
            text = self.listen(timeout=follow_up)   # keep chatting without the wake word
        self.chime(CHIME_DONE)
        self._set_state("idle")

    def _watch_media(self):
        while not self.stop_event.wait(3):
            self._media_playing = _media_playing()

    def run(self):
        self._voice_thread = threading.current_thread()
        self.mic.start()
        threading.Thread(target=self._watch_media, daemon=True, name="media-watch").start()
        name = self.cfg.assistant.get("name", "Jarvis")
        print(f"\n{name} is online using {self.agent.provider.label}. Say \"Hey Jarvis\". (Ctrl+C to quit)\n")
        self._set_state("idle")
        # (not saying its own name here: on open speakers that could wake it)
        barged = self.speak(f"Online and ready{self._address()}.")
        try:
            if barged:
                self._converse_safely()
            while not self.stop_event.is_set():
                if self.paused.is_set():   # wake word off; the push-to-talk hotkey still works
                    self._set_state("paused")
                    self.mic.clear()
                    if self._hotkey_pressed():
                        self._converse_safely()
                        self._set_state("paused")
                    self.stop_event.wait(0.2)
                    continue
                if self.state == "paused":
                    self.wake.reset()
                    self._set_state("idle")
                while not self.announcements.empty():
                    self.chime(CHIME_LISTEN)
                    if self.speak(self.announcements.get()):
                        self._converse_safely()
                if self._hotkey_pressed():
                    log.info("push-to-talk")
                    self._converse_safely()
                    continue
                frame = self.mic.read(timeout=0.2)
                if frame is None:
                    continue
                score = self.wake.process(frame, max(self.media_threshold, self.wake.threshold) if self._media_playing else None)
                if score:
                    log.info("wake word (%.2f)", score)
                    self._converse_safely()
        except KeyboardInterrupt:
            print("\nShutting down.")
        finally:
            self.mic.stop()

    def _address(self) -> str:
        """', sir' / ', boss' / '' depending on assistant.address."""
        address = (self.cfg.assistant.get("address") or "").strip()
        return f", {address}" if address else ""

    def _hotkey_pressed(self) -> bool:
        if self.hotkey and self.hotkey.pressed.is_set():
            self.hotkey.pressed.clear()
            return True
        return False

    def _converse_safely(self):
        self._duck()          # turn the music down while we talk (restored below)
        try:
            self.converse()
        except Exception:
            log.exception("conversation failed")
            self.chime(CHIME_ERROR)
            self._set_state("idle")
        finally:
            self._unduck()
        self.mic.clear()
        self.wake.reset()

    def stop(self):
        self.stop_event.set()
        self.speaker.stop()
        self.scheduler.stop()
        if self.github_watch:
            self.github_watch.stop()
        if self.hotkey:
            self.hotkey.stop()


def run_text_mode(cfg: Section, llm: str | None = None, tts: str | None = None, speak: bool = False):
    """Type instead of talk: handy for testing tools and brains without a mic."""
    engine = create_tts(cfg.tts, tts) if speak else None
    speaker = Speaker(cfg.audio.get("output_device")) if speak else None

    def out(text: str):
        text = clean_for_speech(text)
        say(text)
        if engine:
            audio, sr = engine.synthesize(text)
            speaker.play(audio, sr)

    def confirm(q: str) -> bool:
        out(q)
        return bool(YES.search(input("(yes/no) > ")))

    agent = Agent(cfg, notify=out, confirm=confirm, provider=llm, speak=out)
    print(f"Text mode using {agent.provider.label}. Type 'quit' to exit, '/model claude' to switch brains.\n")
    while True:
        try:
            text = input("\033[93mYou:\033[0m ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not text:
            continue
        if text.lower() in ("quit", "exit"):
            break
        if text.startswith("/model"):
            print(agent.switch(text.split(maxsplit=1)[1] if " " in text else "ollama"))
            continue
        out(agent.handle(text) or "Done.")
