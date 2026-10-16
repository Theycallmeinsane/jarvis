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
from .tts import clean_for_speech, create_tts
from .wakeword import WakeWordDetector

log = logging.getLogger(__name__)

CANCEL = re.compile(r"^(never ?mind|cancel|stop|forget it|nothing)\W*$", re.I)
GOODBYE = re.compile(r"^(thanks?( you)?|that'?s all|goodbye|bye|go to sleep|dismissed)\W*( jarvis)?\W*$", re.I)
YES = re.compile(r"\b(yes|yeah|yep|sure|go ahead|do it|confirm|affirmative|please do|okay|ok)\b", re.I)
# the wake word fires before "...vis" ends, so the recording can start with the tail of it
WAKE_PREFIX = re.compile(r"^\W*((hey|hi|ok|okay)\W+)?(jarvis|travis|vis)\b\W*", re.I)
WAKE_TAILS = {"vis", "is", "this", "his", "hey", "jarvis"}
# Whisper sometimes "hears" these in silence or noise
HALLUCINATIONS = {"", "you", "thank you.", "thanks for watching!", "bye.", ".", "thank you for watching."}


def say(text: str):
    print(f"\033[96mJarvis:\033[0m {text}", flush=True)


class VoiceAssistant:
    """States reported to on_state listeners (the tray icon): loading, idle, listening, thinking, speaking, paused."""

    def __init__(self, cfg: Section, llm: str | None = None, tts: str | None = None,
                 on_state: Callable[[str], None] | None = None):
        self.cfg = cfg
        self.on_state = on_state or (lambda s: None)
        self.state = "loading"
        self.on_state(self.state)
        self.announcements: "queue.Queue[str]" = queue.Queue()
        self.stop_event = threading.Event()     # quit
        self.paused = threading.Event()         # tray "Pause listening"
        self.hush = threading.Event()           # tray "Stop talking"
        bi = cfg.get("barge_in") or {}
        self.barge_in = bool(bi.get("enabled", True))
        # higher than the normal threshold: Jarvis's own voice leaks into the mic on open speakers
        self.barge_threshold = float(bi.get("threshold", 0.8))
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
        self.agent = Agent(cfg, notify=self.announcements.put, confirm=self.confirm, provider=llm)

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
        """Say text sentence by sentence (the next sentence is synthesized while this one plays).
        Returns True if the user barged in with "Hey Jarvis" (or the tray said hush)."""
        text = clean_for_speech(text)
        if not text:
            return False
        say(text)
        self._set_state("speaking")
        self.hush.clear()
        if self.barge_in:
            self.mic.clear()           # keep listening, but only for the wake word
            self.wake.reset()
        else:
            self.mic.muted.set()       # don't let Jarvis hear (and wake) itself
        interrupted = False
        try:
            sentences = [s for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]
            upcoming = self.tts.synthesize(sentences[0])
            for i in range(len(sentences)):
                audio, sr = upcoming
                self.speaker.play(audio, sr, blocking=False)
                end = time.monotonic() + len(audio) / sr
                upcoming = self.tts.synthesize(sentences[i + 1]) if i + 1 < len(sentences) else None
                while time.monotonic() < end:
                    if self.hush.is_set() or self.stop_event.is_set():
                        interrupted = True
                    elif self.barge_in:
                        frame = self.mic.read(timeout=0.05)
                        if frame is not None and self.wake.process(frame, self.barge_threshold):
                            log.info("barge-in")
                            interrupted = True
                    else:
                        time.sleep(0.03)
                    if interrupted:
                        self.speaker.stop()
                        break
                if interrupted:
                    break
        except Exception as e:
            log.warning("TTS failed: %s", e)
        finally:
            if not interrupted:
                time.sleep(0.15)
            self.mic.clear()
            self.wake.reset()
            self.mic.muted.clear()
            self._set_state("idle")
        return interrupted and not self.hush.is_set() and not self.stop_event.is_set()

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
            log.debug("STT %.2fs: %r", time.perf_counter() - t0, text)
            if (text.lower() in HALLUCINATIONS or not re.search(r"\w", text)
                    or re.sub(r"\W", "", text.lower()) in WAKE_TAILS):
                continue
            print(f"\033[93mYou:\033[0m {text}", flush=True)
            return text
        return None

    def confirm(self, question: str) -> bool:
        self.speak(question)
        answer = self.listen(timeout=6) or ""
        return bool(YES.search(answer))

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
                self.speak("At your service, sir.")
                break
            self._set_state("thinking")
            t0 = time.perf_counter()
            reply = self.agent.handle(text)
            log.debug("agent %.2fs", time.perf_counter() - t0)
            if self.speak(reply or "Done."):
                # interrupted with "Hey Jarvis": take the new command right away
                self.chime(CHIME_LISTEN)
                text = self.listen()
                continue
            if follow_up <= 0:
                break
            text = self.listen(timeout=follow_up)   # keep chatting without the wake word
        self.chime(CHIME_DONE)
        self._set_state("idle")

    def run(self):
        self.mic.start()
        name = self.cfg.assistant.get("name", "Jarvis")
        print(f"\n{name} is online using {self.agent.provider.label}. Say \"Hey Jarvis\". (Ctrl+C to quit)\n")
        self._set_state("idle")
        # (not saying its own name here: on open speakers that could wake it)
        self.speak("Online and ready, sir.")
        try:
            while not self.stop_event.is_set():
                if self.paused.is_set():
                    self._set_state("paused")
                    self.mic.clear()
                    self.stop_event.wait(0.3)
                    continue
                if self.state == "paused":
                    self.wake.reset()
                    self._set_state("idle")
                while not self.announcements.empty():
                    self.chime(CHIME_LISTEN)
                    self.speak(self.announcements.get())
                frame = self.mic.read(timeout=0.5)
                if frame is None:
                    continue
                score = self.wake.process(frame)
                if score:
                    log.info("wake word (%.2f)", score)
                    try:
                        self.converse()
                    except Exception:
                        log.exception("conversation failed")
                        self.chime(CHIME_ERROR)
                        self._set_state("idle")
                    self.mic.clear()
                    self.wake.reset()
        except KeyboardInterrupt:
            print("\nShutting down.")
        finally:
            self.mic.stop()

    def stop(self):
        self.stop_event.set()
        self.speaker.stop()


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

    agent = Agent(cfg, notify=out, confirm=confirm, provider=llm)
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
