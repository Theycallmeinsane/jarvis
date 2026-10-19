"""First-run wizard: `jarvis.bat --setup` (setup.ps1 runs it for you).

Detects the hardware, recommends models, and asks a few questions (all optional, Enter = recommended).
Everything it decides goes into config.local.yaml, which is personal and never committed.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from .config import ROOT, save_local
from .hardware import describe, detect, onnxruntime_status, performance_settings, recommend
from .llm.ollama_models import (OllamaUnavailable, features, find, fuzzy_pick, has, list_models, ollama_url,
                                ranked)
from .tts import VOICE_PRESETS, cuda_onnx_available


def _ask(prompt: str, default: str = "", answers: dict | None = None, key: str = "") -> str:
    if answers is not None:                      # non-interactive (--yes / tests)
        return str(answers.get(key, default))
    try:
        reply = input(f"{prompt} [{default}]: " if default else f"{prompt}: ").strip()
    except EOFError:
        reply = ""
    return reply or default


def _yes(prompt: str, default: bool, answers: dict | None, key: str) -> bool:
    reply = _ask(prompt + (" (Y/n)" if default else " (y/N)"), "y" if default else "n", answers, key)
    return reply.lower().startswith("y")


def _preview(voice: str):
    from .audio import Speaker
    from .config import load_config
    from .tts import create_tts

    tts = create_tts(load_config().tts, voice)
    audio, sr = tts.synthesize("Hello. I'm your assistant. Say hey Jarvis whenever you need me.")
    Speaker().play(audio, sr)


def create_shortcuts(desktop: bool = True, start_menu: bool = True) -> list[Path]:
    """Shortcuts that start Jarvis in the tray (no console window)."""
    import win32com.client  # pywin32, installed with pyttsx3

    from .tray import _icon_image

    icon = ROOT / "data" / "jarvis.ico"
    icon.parent.mkdir(exist_ok=True)
    _icon_image("idle").save(icon, sizes=[(64, 64), (32, 32), (16, 16)])
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    shell = win32com.client.Dispatch("WScript.Shell")
    places = []
    if desktop:
        places.append(Path(shell.SpecialFolders("Desktop")))
    if start_menu:
        places.append(Path(shell.SpecialFolders("Programs")))
    made = []
    for folder in places:
        link = shell.CreateShortcut(str(folder / "Jarvis.lnk"))
        link.TargetPath = str(pythonw)
        link.Arguments = f'"{ROOT / "jarvis_tray.pyw"}"'
        link.WorkingDirectory = str(ROOT)
        link.IconLocation = str(icon)
        link.Description = "Jarvis voice assistant"
        link.Save()
        made.append(folder / "Jarvis.lnk")
    return made


def _choose_model(suggested: str, answers: dict | None) -> str | None:
    """Which Ollama model to use. The ones already installed come first (those that can use tools before the
    rest); `suggested` (from hardware.py) is only what to pull when none of them fits.
    Returns the model for config.local.yaml, or None to leave the setting as it is."""
    from .config import load_config

    try:
        models = ranked(list_models(ollama_url(load_config())), suggested)
    except OllamaUnavailable as e:
        print(f"  {e} Jarvis will use the best model you have once it's running (choose: jarvis.bat --models).")
        return None
    if not models:
        print(f"  No Ollama models yet. For this PC: ollama pull {suggested}  (Jarvis uses it, or any model you pull)")
        return "auto"
    print("\nYour Ollama models (Jarvis needs one that can use tools to act on the PC):")
    for i, m in enumerate(models, 1):
        print(f"  {i:>2}. {m['name']:<32} {m['params']:>7}  {features(m)}{'   <- recommended' if i == 1 else ''}")
    if not has(models[0], "tools"):
        print(f"  None of these can use tools, so Jarvis could only chat. For this PC: ollama pull {suggested}")
    while True:
        reply = _ask("Model (number or name)", models[0]["name"], answers, "model").strip()
        if reply.isdigit() and 1 <= int(reply) <= len(models):
            return models[int(reply) - 1]["name"]
        hit = find(models, reply)
        pick = hit["name"] if hit else fuzzy_pick(models, reply)
        if pick or reply == "auto" or answers is not None:
            return pick or reply
        print("  not one of the models above")


def run_wizard(answers: dict | None = None) -> dict:
    """answers: pre-filled replies for a non-interactive run (keys: name, address, voice, mic, brain, model,
    shortcuts, autostart). Returns the settings written to config.local.yaml."""
    print("\n=== Jarvis setup ===\n")
    hw = detect()
    rec = recommend(hw)
    print(f"This PC: {describe(hw)}")
    ok, status = onnxruntime_status()
    print(f"Speech runtime: {status}")
    print(f"Recommended: Whisper {rec.whisper_model}, voice {rec.voice}, brain: any Ollama model you have "
          f"(or {rec.ollama_model})")
    for n in rec.notes:
        print(f"  - {n}")
    print("\nPress Enter to accept the suggestion in [brackets].\n")

    perf = _ask("Speed or quality? fast (quickest replies) / balanced / quality (best understanding)",
                "balanced", answers, "performance").lower()
    if perf not in ("fast", "balanced", "quality"):
        perf = "balanced"
    settings = performance_settings(perf, hw)
    settings["tts"] = {"voice": rec.voice}
    name = _ask("Your name (optional)", "", answers, "name")
    address = _ask("How should Jarvis address you? (e.g. sir, ma'am, boss, your name, or none)",
                   "none", answers, "address")
    settings["assistant"] = {"user_name": name or None,
                             "address": None if address.lower() in ("", "none", "no") else address}

    gpu_voices = cuda_onnx_available()
    print("\nVoices (all local):")
    for key, p in VOICE_PRESETS.items():
        if p["engine"] == "kokoro" and not gpu_voices:
            continue
        print(f"  {key:10} {p['desc']}")
    while True:
        voice = _ask("Voice (type 'play <name>' to hear one)", rec.voice, answers, "voice")
        if voice.lower().startswith("play "):
            try:
                _preview(voice.split(maxsplit=1)[1])
            except Exception as e:  # noqa: BLE001
                print(f"  couldn't play it: {e}")
            continue
        if voice in VOICE_PRESETS:
            break
        print("  not one of the voices above")
    settings["tts"] = {"voice": voice}

    if answers is None:
        from .audio import list_devices

        print("\nMicrophones and speakers:\n" + list_devices())
    mic = _ask("Microphone (number or part of its name; Enter = Windows default)", "", answers, "mic")
    if mic:
        settings["audio"] = {"input_device": int(mic) if mic.isdigit() else mic}

    brain = _ask("Brain: local (private, free) or claude (smarter, needs an API key)", "local", answers, "brain")
    if brain.lower().startswith("claude"):
        settings["llm"]["provider"] = "claude"
        if not os.environ.get("ANTHROPIC_API_KEY") and answers is None:
            print("  Put ANTHROPIC_API_KEY=... in the .env file in this folder (it's never committed).")
    else:
        settings["llm"]["provider"] = "ollama"
        model = _choose_model(rec.ollama_model, answers)
        if model:
            settings["llm"]["providers"]["ollama"]["model"] = model

    path = save_local(settings)
    print(f"\nSaved your settings to {path.name}.")

    if _yes("Add Jarvis shortcuts to the Desktop and Start menu?", True, answers, "shortcuts"):
        try:
            for link in create_shortcuts():
                print(f"  created {link}")
        except Exception as e:  # noqa: BLE001
            print(f"  couldn't create shortcuts: {e}")
    if _yes("Start Jarvis automatically when you sign in to Windows?", False, answers, "autostart"):
        from .tray import set_autostart

        set_autostart(True)
        print("  Jarvis will start with Windows (turn it off from the tray menu).")
    return settings
