# Jarvis — a local voice assistant for Windows

[![tests](https://github.com/Theycallmeinsane/jarvis/actions/workflows/tests.yml/badge.svg)](https://github.com/Theycallmeinsane/jarvis/actions/workflows/tests.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

Say **"Hey Jarvis"**, ask for something, and Jarvis does it on your PC and answers out loud.
Your voice goes in through local speech-to-text (Whisper), a local brain (Ollama) decides what to do and runs
Windows workflows, and the answer comes out through a local open-source voice (a female voice by default).
Nothing leaves your PC unless you switch the brain to Claude.

```
 mic ─► openWakeWord ("hey jarvis") ─► webrtcvad (record until you pause) ─► faster-whisper (STT)
                                                                                 │
 speaker ◄── Piper / Kokoro (TTS, female or male) ◄── agent ◄──► brain (Ollama · LM Studio · Claude · any OpenAI-compatible)
                                                        │
                                                        └──► Windows workflows: apps, windows, settings, media, timers...
```

## Quick start

**You need:** Windows 10 or 11, a microphone, an internet connection for the first install (~2–4 GB of
downloads). An NVIDIA GPU makes it faster but isn't required. Python and Ollama are installed for you if missing.

1. **Get the code**: `git clone https://github.com/Theycallmeinsane/jarvis` (or *Code → Download ZIP* and unzip).
2. **Install**: open PowerShell in the folder and run

   ```powershell
   powershell -ExecutionPolicy Bypass -File setup.ps1
   ```

   It checks your hardware (GPU, VRAM, RAM), installs the right packages and a brain model that fits your PC,
   asks a few optional questions (your name, how to address you, voice, microphone) and offers Desktop/Start
   menu shortcuts. Add `-Yes` to accept every recommendation without questions.
3. **Start it** (activate the app): double-click the **Jarvis** shortcut, or run `.\jarvis-tray.bat`.
   An icon appears in the system tray (bottom-right, next to the clock). Wait for "Online and ready".
4. **Talk**: say **"Hey Jarvis"** (or press **Ctrl+Alt+J**), wait for the chime, then e.g.
   *"open Spotify"*, *"what's the weather?"*, *"switch to the male voice"*, *"run work mode"*.

To start Jarvis automatically, right-click the tray icon → **Start with Windows**. To stop it: tray icon → **Quit**.
Settings you choose live in `config.local.yaml` (personal, git-ignored); `config.yaml` holds the shared defaults.
After pulling a new version, run `setup.ps1` again: it updates packages and keeps your settings.

| PC | what setup picks |
|---|---|
| NVIDIA GPU, 12 GB+ VRAM | Whisper large-v3-turbo, brain qwen3.5:9b, HD voice |
| NVIDIA GPU, 8 GB | Whisper small.en, brain qwen3.5:4b, HD voice (Kokoro on the GPU) |
| NVIDIA GPU, 4–6 GB | Whisper base.en, brain qwen3.5:2b, Piper voice |
| no NVIDIA GPU, 16 GB RAM | Whisper base.en on CPU, brain qwen3.5:2b, Piper voice |
| no NVIDIA GPU, 8 GB RAM | Whisper base.en on CPU, brain qwen3.5:0.8b (consider `--llm claude`) |

Other ways to run it:

```powershell
.\jarvis.bat                     # run in a console window (shows what it hears and says)
.\jarvis.bat --setup             # run the setup questions again (voice, name, mic, brain)
.\jarvis.bat --text              # type instead of talk (good for testing)
.\jarvis.bat --text --speak      # type, but hear the replies
.\jarvis.bat --llm claude        # use Claude for this run
.\jarvis.bat --voice jarvis      # pick a voice for this run (see "Voices")
.\jarvis.bat --list-devices      # find your mic's index / name
.\jarvis.bat --check             # test every component
.\jarvis.bat --wake-test         # live wake-word score meter, to tune the threshold for your mic
.\jarvis.bat --google-login      # connect Google Calendar + Gmail (see below)
```

First time with your own voice? Follow [docs/FIRST_RUN.md](docs/FIRST_RUN.md) (10 minutes).

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
| "What's the weather in Paris?" | `get_weather` (wttr.in) |
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
| "Switch to Chrome", "snap VS Code left", "maximize Spotify" | `window_action` / `list_windows` |
| "Close the Notepad window" | `close_window` (names the window and asks first) |
| "Show the desktop", "next desktop", "open task manager", "clipboard history" | `desktop_action` |
| "Press control S", "hit F5", "alt tab" | `press_keys` |
| "Open Bluetooth settings", "open Windows Update" | `open_settings` (30+ Settings pages) |
| "Open my Downloads", "open the recycle bin" | `open_folder` |
| "What's using my memory?" | `top_processes` |
| "Dark mode on", "switch to light mode" | `theme` |
| "Am I on Wi-Fi?", "connect to HomeWiFi" | `wifi` |
| "Read this to me" (selected text), "read my clipboard" | `read_aloud` (word for word, local voice) |
| "What's on my calendar tomorrow?" / "add dentist Monday at 3" | `calendar_upcoming` / `calendar_add_event` |
| "Any new emails?" / "read the one from Sara" | `gmail_unread` / `gmail_read` |
| "Email Sara that I'll be late" | `gmail_find_address` → `gmail_send` (reads it back, asks first) / `gmail_draft` |
| "Remember that…" / "what do you know about me?" / "forget…" | `remember` / `list_memories` / `forget` |
| "Switch to Claude" / "go back to local" | `switch_model` |
| "Ask Claude to explain quantum tunnelling" | `ask_claude` (local brain hands a hard question to Claude) |
| "Run `Get-Process` in PowerShell" | `run_powershell` (off by default, asks first) |

### Routines (multi-step workflows, no code)

Define them in `config.yaml` (or `config.local.yaml`) and say *"run good morning"* / *"work mode"*:

```yaml
routines:
  good morning:
    say: Good morning.
    steps:
      - get_datetime
      - get_weather: {}
      - calendar_upcoming: {days: 1}
      - web_lookup: {query: top headlines today, news: true}
    summarize: true                                   # spoken as a short briefing
    schedule: {time: "08:00", days: [mon, tue, wed, thu, fri]}   # optional: runs by itself
  work mode:
    say: Work mode is on.
    steps:
      - open_app: {name: Visual Studio Code}
      - volume: {action: set, percent: 30}
    summarize: false
```

Each step is any tool from the table above with its arguments. Steps you wrote yourself run without the
"Shall I…?" question, so only put actions in a routine that you're happy to run unattended. Scheduled routines
are announced out loud (and as a notification in tray mode) while Jarvis is running.

### Adding your own workflow in Python

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

## MCP servers (plug in more tools)

Any [Model Context Protocol](https://modelcontextprotocol.io) server can give Jarvis new abilities (files,
GitHub, Spotify, home automation, databases…). Add it under `mcp_servers` in `config.local.yaml`:

```yaml
mcp_servers:
  files:
    command: npx
    args: ["-y", "@modelcontextprotocol/server-filesystem", "C:/Users/YOU/Documents"]
    tools: [read_text_file, list_directory, search_files]   # optional: only expose these
  my-http-server:
    url: http://localhost:8000/mcp                         # streamable-HTTP servers work too
```

Each server's tools show up as `<server>_<tool>` and work with every brain. `confirm: auto` (the default)
asks "Shall I…?" before any tool the server doesn't mark as read-only; set `always` or `never` per server.
Keep the allow-list short with small local models, since every tool adds to the prompt. Servers' own logs go
to `data/mcp.log`. `tests/fixtures/demo_mcp_server.py` is a 20-line example server you can copy.

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

All open source, all local. Pick one in setup, with `tts.voice` in `config.local.yaml`, from the tray menu
(**Voice**), with `--voice`, or just say *"switch to the female voice"* / *"switch to the male voice"*.

| `tts.voice` | voice | engine | runs on |
|---|---|---|---|
| `friday` (default) | female, British | Piper `en_GB-jenny_dioco-medium` | any PC |
| `amy` | female, American | Piper `en_US-amy-medium` | any PC |
| `jarvis` | male, British | Piper `en_GB-alan-medium` | any PC |
| `heart-hd` | female, American, most natural | Kokoro `af_heart` | NVIDIA GPU |
| `bella-hd` | female, American | Kokoro `af_bella` | NVIDIA GPU |
| `friday-hd` | female, British | Kokoro `bf_emma` | NVIDIA GPU |
| `jarvis-hd` | male, British | Kokoro `bm_george` | NVIDIA GPU |
| `windows` | built-in Windows voice | SAPI | any PC, no download |

HD voices fall back to the matching Piper voice on PCs without an NVIDIA GPU, and if Piper can't load either,
Jarvis falls back to the Windows voice (the log says why) instead of going silent.

Measured on an i5-12600K + RTX 3050: Piper medium voices speak 4–5× faster than real time on the CPU;
Kokoro runs 4–7× real time on the GPU but only 0.6× on the CPU, which is why it's GPU-only. Sending each
voice's speech through Whisper gave 8–13% word differences for every voice (mostly how numbers are written),
so they're all equally clear; pick the one you like the sound of (`setup` can play a sample).

Any other [Piper voice](https://huggingface.co/rhasspy/piper-voices) works too: `tts: {voice: piper, piper: {voice: en_US-kristin-medium}}`.

## Tuning

* **Wake word too eager / too deaf:** `wake_word.threshold` (0.5 default; 0.6–0.7 if it fires on TV).
  `.\jarvis.bat --wake-test` shows live scores so you can pick the value.
* **A different wake word:** train one with openWakeWord's training notebook (linked from the
  [openWakeWord README](https://github.com/dscripka/openWakeWord)) and set `wake_word.model` to the
  path of the `.onnx` file.
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
  routines.py      config-defined multi-step workflows + daily scheduler
  mcp_client.py    MCP servers' tools as Jarvis tools
  vision.py        screenshot → vision model (local Ollama or Claude)
  hotkey.py        global push-to-talk hotkey
  audio.py         mic stream, speaker, chimes
  wakeword.py      openWakeWord "hey_jarvis"
  recorder.py      webrtcvad endpointing
  stt.py           faster-whisper (CUDA → CPU fallback)
  tts.py           Piper / Kokoro / SAPI
  llm/             ollama.py, openai_compat.py (LM Studio, etc.), anthropic_claude.py
  tools/           registry.py (@tool decorator), builtin.py (workflows), windows.py (windows, desktops,
                   shortcuts, Settings, theme, Wi-Fi, read aloud), google_tools.py (Calendar, Gmail),
                   memory_tools.py (remember / forget)
scripts/voice_smoke.py   Piper → wake word → Whisper check with no mic (also runs in GitHub Actions)
  diagnostics.py   --check
config.yaml        all settings
tests/             offline unit tests:  python -m unittest discover tests
```

## License

MIT, see [LICENSE](LICENSE). Copyright (c) 2026 Muhammad (Theycallmeinsane).

The models Jarvis downloads (Whisper, Piper voices, openWakeWord, Kokoro, Ollama models) have their own licenses; check them before redistributing.
