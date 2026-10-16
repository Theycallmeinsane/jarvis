# Jarvis — a local voice assistant for Windows

Say **"Hey Jarvis"**, ask for something, and Jarvis does it on your PC and answers out loud.
Everything runs **locally by default**: wake word, speech-to-text, the language model and the voice.
Switching to Claude (or any other model) is one setting, or just say *"switch to Claude"*.

```
 mic ─► openWakeWord ("hey jarvis") ─► webrtcvad (record until you pause) ─► faster-whisper (STT)
                                                                                 │
 speaker ◄── Piper / Kokoro / SAPI (TTS) ◄── agent loop ◄──► LLM (Ollama · LM Studio · Claude · OpenAI-compatible)
                                                 │
                                                 └──► tools: apps, web, volume, media, timers, notes, files, ...
```

## Quick start

Requirements: Windows 10/11, Python 3.11, [Ollama](https://ollama.com), a microphone.

```powershell
powershell -ExecutionPolicy Bypass -File setup.ps1 -Gpu   # drop -Gpu if you don't have an NVIDIA card
.\jarvis.bat
```

`setup.ps1` creates `.venv`, installs the packages, pulls the default Ollama model (`qwen3.5:4b`) and
runs a systems check. The first run downloads the speech models (~0.6 GB) into `models/`.

Other ways to run it:

```powershell
.\jarvis-tray.bat                # run quietly in the system tray (right-click the icon for options)
.\jarvis.bat --text              # type instead of talk (good for testing)
.\jarvis.bat --text --speak      # type, but hear the replies
.\jarvis.bat --llm claude        # use Claude for this run
.\jarvis.bat --tts kokoro        # nicer (slower) voice
.\jarvis.bat --list-devices      # find your mic's index / name
.\jarvis.bat --check             # test every component
```

## Talking to Jarvis

* **"Hey Jarvis"** → a chime → speak → Jarvis answers.
* For a few seconds after each answer you can keep talking **without** the wake word.
* "Never mind" / "cancel" stops; "thanks Jarvis" / "that's all" ends the conversation.
* **Push-to-talk:** press **Ctrl+Alt+J** anywhere instead of saying the wake word (`hotkey` in config).
  It works even while listening is paused from the tray, so you can keep the mic "deaf" and use only the key.
* **Interrupt it:** say "Hey Jarvis" (or press the hotkey) while it's talking; it stops mid-sentence and listens.
* **Answers start fast:** replies are streamed, so Jarvis starts speaking the first sentence while the model
  is still writing the rest.
* **Small-model safety net:** action tools (search in browser, open app, type, power, close app, media keys)
  only run if your request actually mentions that kind of action, so a 4B model can't open your browser
  when you asked for a story.
* Risky actions (closing apps, sending email, shutdown, PowerShell) ask *"Shall I…?"* and wait for a yes.
* **Memory:** "remember that my sister's name is Sara" is kept in `data/memory.json` and known in every future
  conversation; "what do you remember about me?" / "forget Sara". Recent exchanges (`data/history.jsonl`) are
  replayed when Jarvis restarts, so it picks up where you left off.

### Tray app

`jarvis-tray.bat` runs Jarvis with no console window. The tray icon's ring changes colour with the state
(blue idle, green listening, amber thinking, cyan speaking, grey paused). Right-click for:
pause listening, stop talking, switch brain (Local / Claude / LM Studio / OpenAI-compatible),
**Start with Windows** (adds a per-user startup entry, no admin needed), open settings / notes / log, quit.
Logs go to `data/jarvis.log`.

### What it can do (built-in workflows)

| Say something like | Tool |
|---|---|
| "Open Spotify", "launch VS Code" | `open_app` (anything in your Start menu) |
| "Close Chrome" | `close_app` (asks first) |
| "Search YouTube for lofi", "google the iPhone 18 release date" | `web_search` |
| "Open github.com" | `open_website` |
| "Who is Ada Lovelace?" | `wikipedia_summary` |
| "What's the latest Python version?", "any tech news today?" | `web_lookup` (DuckDuckGo results read back, no browser) |
| "What does this error on my screen say?", "what's open?" | `look_at_screen` (local qwen3.5 vision, or Claude) |
| "What's the weather in Lahore?" | `get_weather` (wttr.in) |
| "Volume to 30", "mute", "louder" | `volume` |
| "Pause the music", "next song" | `media_control` |
| "Set a timer for 10 minutes", "remind me in an hour to stretch" | `set_timer` / `list_timers` / `cancel_timer` |
| "Take a note: call the bank" / "read my notes" | `take_note` / `read_notes` (`data/notes.md`) |
| "Find my resume PDF" / "open it" | `find_files` / `open_path` |
| "Type: dear sir…" | `type_text` (dictation into the focused window) |
| "What's on my clipboard?" / "copy that" | `read_clipboard` / `copy_to_clipboard` |
| "How's my computer doing?" | `system_status` (CPU, RAM, GPU, disks) |
| "Take a screenshot" | `take_screenshot` (Pictures/Jarvis) |
| "Lock the computer", "restart in a bit" | `lock_computer` / `power` (asks first) |
| "What's on my calendar tomorrow?" / "add dentist Monday at 3" | `calendar_upcoming` / `calendar_add_event` |
| "Any new emails?" / "read the one from Sara" | `gmail_unread` / `gmail_read` |
| "Email Sara that I'll be late" | `gmail_find_address` → `gmail_send` (reads it back, asks first) / `gmail_draft` |
| "Remember that…" / "what do you know about me?" / "forget…" | `remember` / `list_memories` / `forget` |
| "Switch to Claude" / "go back to local" | `switch_model` |
| "Ask Claude to explain quantum tunnelling" | `ask_claude` (local brain hands a hard question to Claude) |
| "Run `Get-Process` in PowerShell" | `run_powershell` (off by default, asks first) |

### Adding your own workflow

Drop a function anywhere that gets imported (e.g. at the bottom of `jarvis/tools/builtin.py`):

```python
from jarvis.tools import tool

@tool("Start my work setup: VS Code, Slack and the project folder.")
def work_mode():
    import os, subprocess
    subprocess.Popen(["code", r"D:\Python\AI-Projects"])
    os.startfile("slack:")
    return "Work mode on."
```

The description string is what the model sees, so describe *when* to use it.
Add `params={...}` (JSON-schema per argument) for inputs and `dangerous=True` to require a spoken yes.

## Google Calendar & Gmail (optional)

The Google tools stay hidden until you connect an account. One-time setup, about 5 minutes:

1. Open [Google Cloud console](https://console.cloud.google.com/) → create a project (any name).
2. **APIs & Services → Library**: enable **Google Calendar API** and **Gmail API**.
3. **APIs & Services → OAuth consent screen**: External, add yourself under *Test users*.
4. **Credentials → Create credentials → OAuth client ID → Desktop app**, then *Download JSON*.
5. Save it as `secrets\google_credentials.json` in this folder (the `secrets/` folder is git-ignored).
6. Run `.\jarvis.bat --google-login`. Your browser opens once to approve access; the token is saved to
   `secrets\google_token.json`.

Permissions requested: calendar events (read/add), Gmail read, and Gmail compose (drafts + send).
Jarvis never sends an email without reading the recipient, subject and body back to you and hearing a yes.
Delete `secrets\google_token.json` to disconnect.

## Switching brains

`config.yaml → llm.provider`:

| provider | what | setup |
|---|---|---|
| `ollama` (default) | local models via Ollama | `ollama pull qwen3.5:4b` |
| `lmstudio` | LM Studio's local server | Developer tab → Start server, set `model` |
| `openai_compat` | llama.cpp server, vLLM, Groq, OpenRouter, OpenAI… | set `base_url`, `model`, key in `.env` |
| `claude` | Anthropic Claude | `ANTHROPIC_API_KEY` in `.env` |

You can switch at runtime by voice ("switch to Claude"), with `--llm`, or with `/model claude` in text mode.
Claude defaults to `claude-opus-5-5` at `effort: low` for snappy voice replies, with Anthropic's server-side
refusal fallback enabled; `claude-sonnet-5-5` or `claude-haiku-5-5` are faster/cheaper options.

Put personal overrides in `config.local.yaml` (git-ignored) instead of editing `config.yaml`, e.g.

```yaml
llm: {provider: claude}
tts: {provider: kokoro}
audio: {input_device: HyperX}
```

## Voices

| `tts.provider` | quality | speed | notes |
|---|---|---|---|
| `piper` (default) | good | very fast | `en_GB-alan-medium` British male; any [Piper voice](https://huggingface.co/rhasspy/piper-voices) |
| `kokoro` | best | slower on CPU | `bm_george` British male; ~330 MB download |
| `sapi` | basic | fast | built-in Windows voices, no download; automatic fallback |

## Tuning

* **Wake word too eager / too deaf:** `wake_word.threshold` (0.5 default; 0.6–0.7 if it fires on TV).
* **Cuts you off mid-sentence:** raise `recording.silence_ms` (800 → 1200).
* **Mishears names:** add them to `stt.hints` (e.g. city, app and people names).
* **Local model does extra things you didn't ask for:** keep `max_tool_rounds: 1` (default for Ollama/LM Studio);
  raise it to 2–3 for 8B+ models so they can chain steps like "find my resume and open it".
* **STT accuracy vs speed:** `stt.model` `base.en` (fastest) → `small.en` (default) → `large-v3-turbo` (best, GPU).
* **Wrong microphone:** `.\jarvis.bat --list-devices`, then set `audio.input_device` to the index or part of its name.
* **Slow first answer:** the Ollama model is loaded at start-up and kept warm for 30 minutes (`keep_alive`).

## Tested on

Windows 11 Pro, i5-12600K, 16 GB RAM, RTX 3050 8 GB, Ollama `qwen3.5:4b` fully on the GPU:

| step | time |
|---|---|
| wake word | real time on the CPU |
| Whisper `small.en` (GPU, `-Gpu` setup) | 0.2–0.5 s per command (CPU fallback ~1–2 s) |
| LLM, request with one tool call | ~5–6 s (two model calls of ~2.7 s) |
| LLM, plain chat answer | ~3 s |
| Piper voice | ~0.1 s to synthesize a 4 s sentence (CPU) |

`qwen3:4b` was much slower here (9–28 s) because it ignores the "no thinking" switch, so `qwen3.5:4b` is the default.

## Project layout

```
jarvis/
  app.py           voice loop (wake → record → STT → agent → TTS → follow-up), barge-in
  tray.py          system tray icon + menu, start-with-Windows
  agent.py         tool-calling loop, confirmation, brain switching
  memory.py        remembered facts + conversation history across restarts
  vision.py        screenshot → vision model (local Ollama or Claude)
  hotkey.py        global push-to-talk hotkey
  audio.py         mic stream, speaker, chimes
  wakeword.py      openWakeWord "hey_jarvis"
  recorder.py      webrtcvad endpointing
  stt.py           faster-whisper (CUDA → CPU fallback)
  tts.py           Piper / Kokoro / SAPI
  llm/             ollama.py, openai_compat.py (LM Studio, etc.), anthropic_claude.py
  tools/           registry.py (@tool decorator), builtin.py (workflows), google_tools.py (Calendar, Gmail),
                   memory_tools.py (remember / forget)
  diagnostics.py   --check
config.yaml        all settings
tests/             offline unit tests:  python -m unittest discover tests
```
