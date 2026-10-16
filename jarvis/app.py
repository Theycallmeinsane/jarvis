"""Voice loop: wake word -> record -> transcribe -> agent -> speak -> short follow-up window."""
from __future__ import annotations

import logging
import queue
import re
import time

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
    def __init__(self, cfg: Section, llm: str | None = None, tts: str | None = None):
        self.cfg = cfg
        self.announcements: "queue.Queue[str]" = queue.Queue()
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

    def speak(self, text: str):
        text = clean_for_speech(text)
        if not text:
            return
        say(text)
        self.mic.muted.set()           # don't let Jarvis hear (and wake) itself
        try:
            for sentence in re.split(r"(?<=[.!?])\s+", text):
                if sentence.strip():
                    audio, sr = self.tts.synthesize(sentence)
                    self.speaker.play(audio, sr)
        except Exception as e:
            log.warning("TTS failed: %s", e)
        finally:
            time.sleep(0.15)
            self.mic.clear()
            self.wake.reset()
            self.mic.muted.clear()

    def listen(self, timeout: float | None = None) -> str | None:
        """Record and transcribe one utterance. Leftovers of the wake word ("...vis", "Jarvis")
        don't count: keep listening until real words arrive or the timeout runs out."""
        timeout = self.cfg.recording.get("start_timeout_s", 6) if timeout is None else timeout
        deadline = time.monotonic() + timeout
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
        while text:
            if CANCEL.match(text):
                self.speak("Very well.")
                break
            if GOODBYE.match(text):
                self.speak("At your service, sir.")
                break
            t0 = time.perf_counter()
            reply = self.agent.handle(text)
            log.debug("agent %.2fs", time.perf_counter() - t0)
            self.speak(reply or "Done.")
            if follow_up <= 0:
                break
            text = self.listen(timeout=follow_up)   # keep chatting without the wake word
        self.chime(CHIME_DONE)

    def run(self):
        self.mic.start()
        name = self.cfg.assistant.get("name", "Jarvis")
        print(f"\n{name} is online using {self.agent.provider.label}. Say \"Hey Jarvis\". (Ctrl+C to quit)\n")
        self.speak(f"{name} online.")
        try:
            while True:
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
                    self.mic.clear()
                    self.wake.reset()
        except KeyboardInterrupt:
            print("\nShutting down.")
        finally:
            self.mic.stop()


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
