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
.\jarvis.bat --google-setup      # connect Google Calendar + Gmail, step by step (see below)
.\jarvis.bat --chrome-setup      # link Chrome: tabs, pages, videos and Claude by voice (see below)
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
| "Volume to 30", "mute", "louder" | `volume` (the whole PC) |
| "Turn Spotify up", "increase the volume on Spotify", "mute Discord" | `app_volume` (one app) |
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
| "System stats", "how hot is my GPU?", "how much RAM is left?", "uptime" | `system_stats` (CPU, GPU, RAM, disks, network, battery) |
| "Am I online?", "check my internet" | `network_status` (connection, IP, ping, live speed) |
| "Is Chrome running?", "end the frozen app", "set OBS to high priority" | `find_process` / `end_process` / `set_priority` (end/priority ask first; core Windows processes are refused) |
| "Is the print spooler running?", "restart the spooler" | `service_status` / `service_control` (asks; needs admin; core services refused) |
| "What starts with Windows?", "stop Discord starting with Windows" | `list_startup_apps` / `set_startup_app` |
| "Turn off Bluetooth", "airplane mode on" | `radio` (Bluetooth, Wi-Fi, airplane) |
| "Brightness to 40", "make the screen brighter" | `brightness` (laptop panels and DDC/CI monitors) |
| "Switch the sound to my headset" | `audio_output` (default playback device) |
| "What's in the recycle bin?", "empty it", "clear temp files" | `recycle_bin_status` / `empty_recycle_bin` / `clear_temp_files` (both clean-ups say how much and ask first) |
| "Any Windows updates?" | `windows_update_status` (last update, restart pending) |
| "Play the movie Inception", "watch Friends season 2 episode 3" | `play_movie` (your own files, in VLC fullscreen; titles matched fuzzily, release tags ignored) |
| "Pause", "skip 30 seconds", "rewind 2 minutes", "jump to 1:20:00", "subtitles on", "next audio track", "next episode", "movie volume 80", "close the movie" | `vlc_control` (real VLC state; while a movie is open these mean the movie) |
| "What movies do I have?", "do I have Interstellar?" | `list_movies` |
| "Set the lights to purple", "RAM rainbow mode", "lights off", "load the movie lighting profile" | `rgb` (through OpenRGB, see below) |
| "What's on my calendar tomorrow?" / "add dentist Monday at 3" | `calendar_upcoming` / `calendar_add_event` |
| "Any new emails?" / "read the one from Sara" | `gmail_unread` / `gmail_read` |
| "Email Sara that I'll be late" | `gmail_find_address` → `gmail_send` (reads it back, asks first) / `gmail_draft` |
| "Remember that…" / "what do you know about me?" / "forget…" | `remember` / `list_memories` / `forget` |
| "Switch to Claude" / "go back to local" | `switch_model` |
| "Ask Claude to explain quantum tunnelling" | `claude_chat` (claude.ai in Chrome; answer read back) or `ask_claude` (API key) |
| "What tabs do I have open?", "switch to the YouTube tab", "close this tab" | `chrome_tabs` / `chrome_tab` |
| "Summarize this page", "click sign in", "type hello in the search box" | `chrome_read` / `chrome_click` / `chrome_type` |
| "Skip 30 seconds", "play the video at 1.5 speed", "scroll down" | `chrome_video` / `chrome_page` |
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

The Google tools stay hidden until you connect an account. Run the guided setup (about 5 minutes):

```powershell
.\jarvis.bat --google-setup
```

Google only lets the account owner create the sign-in client, so you do the clicks; the setup opens each
Google Cloud page in order and says exactly what to press on it:

1. create a project, 2. turn on the Calendar and Gmail APIs, 3. set up the consent screen,
4. add yourself as a test user (and optionally *Publish app*, so Google doesn't sign Jarvis out every 7 days),
5. create a **Desktop app** client and click *Download JSON*.

It then picks the file up from Downloads, moves it to `secrets\google_credentials.json` (git-ignored) and opens
the Google sign-in once; the token is saved to `secrets\google_token.json`. Already have the JSON?
`.\jarvis.bat --google-setup C:\path\to\client_secret.json`. To sign in again later: `.\jarvis.bat --google-login`.

Permissions requested: calendar events (read/add), Gmail read, and Gmail compose (drafts + send).
Jarvis never sends an email without reading the recipient, subject and body back to you and hearing a yes.
Delete `secrets\google_token.json` to disconnect.

## GitHub (optional)

Jarvis uses the [GitHub CLI](https://cli.github.com)'s login, so it never stores a token itself:
`winget install GitHub.cli` (setup.ps1 offers it), then `gh auth login` once. The tools appear when `gh` is
installed. Set your repos in `config.local.yaml`:

```yaml
github:
  default_repo: you/jarvis        # what "the build" means when you don't name a repo
  watch: [you/jarvis]             # Jarvis says so out loud when one of these builds fails
```

* *"Any GitHub notifications?"*, *"did my last build pass?"*, *"what's open on jarvis?"*, *"recent commits"*
* *"Why did the build fail?"*: reads the failed job's log and explains it in a sentence or two.
* *"Create an issue: wake word misses me in the kitchen"*, *"comment on 12…"*, *"re-run the failed build"*:
  these ask *"Shall I…?"* first. Merging, deleting, force-pushing and changing settings aren't possible by voice.

## Files

Jarvis can work with files on any drive by voice; spoken names don't need to be exact
(*"the resume pdf in downloads"*, *"the D drive"*):

* *"What's in my Downloads?"*, *"biggest files on D"*, *"what's using space in Documents?"*, *"find duplicates in Pictures"*
* *"Summarize the report pdf in documents"*, *"read notes.txt"* (text, PDF, Word and code files)
* *"Make a folder called invoices in documents"*, *"save a note file called shopping list"*, *"copy … to …"*,
  *"zip the photos folder"*, *"unzip …"*
* *"Rename …"*, *"move … to …"*, *"tidy up my downloads"* (sorts loose files into Documents, Images, Videos…) and
  *"delete …"* ask first. Deleting only ever moves things to the **Recycle Bin**.

Jarvis only changes files inside `files.allowed_roots` (default: your user folder and non-system drives).
Windows, Program Files, ProgramData, AppData and Jarvis's own folder are always read-only, and nothing is
overwritten: a copy gets a new name like `report (2).pdf`.

## Chrome and Claude

```powershell
.\jarvis.bat --chrome-setup
```

This opens Chrome's Extensions page: turn on **Developer mode**, click **Load unpacked** and pick the
`extension\chrome` folder (its path is copied for you). The Jarvis Control extension then links to Jarvis
whenever both are running. It only talks to Jarvis on this PC, and Jarvis only accepts that extension.

* *"What tabs do I have open?"*, *"switch to the GitHub tab"*, *"close the Gmail tab"*, *"reopen that tab"*,
  *"close all other tabs"*, *"open youtube"*
* *"Summarize this page"*, *"click next"*, *"type lofi beats in the search box and press enter"*, *"scroll down"*,
  *"zoom in"*, *"mute this tab"*
* *"Pause the video"*, *"skip 30 seconds"*, *"next video"*, *"play at 1.5 speed"*, *"full screen"*
* *"Ask Claude …"* sends it to claude.ai (you need to be signed in there), waits for the answer and reads it
  out; long answers are summarized as their opening with the rest on screen. *"Read Claude's answer"*,
  *"stop Claude"*, *"open Claude"* too.

Clicking anything like *buy*, *pay*, *send*, *delete* or *submit* asks first, and Jarvis never types into
password or card fields. Without the extension, *new tab*, *close this tab*, *next tab*, *go back*, *reload*
and *scroll* still work through keyboard shortcuts.

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

## RGB lighting (OpenRGB)

Vendor RGB apps (Gigabyte RGB Fusion, Armoury Crate, iCUE, Mystic Light...) have no public API, so Jarvis
controls lights through [OpenRGB](https://openrgb.org), which is open source and supports Gigabyte RGB Fusion 2
boards, most RGB RAM (ENE, Patriot, Corsair...), GPUs, fans and keyboards.

1. Install OpenRGB from [openrgb.org](https://openrgb.org) (it may ask to install a small hardware-access
   driver; motherboard and RAM lighting need it and admin rights).
2. **Close your vendor RGB app** (e.g. RGB Fusion) and stop it starting with Windows: two programs writing
   to the same lighting chips flicker or crash. Jarvis tells you if one is still running.
3. In OpenRGB: *Settings > SDK Server > Start server* (and tick *Start at login* + *Start server* if you like).
4. Say *"set the lights to purple"*, *"RAM lights rainbow"*, *"lights off"*, *"load the movie lighting profile"*.

**Gigabyte RGB Fusion boards:** OpenRGB talks to the board's RGB Fusion 2 controller directly (it shows up as your
board's name, e.g. "B660M GAMING DDR4"), so *"set RGB Fusion to red"* and *"RGB Fusion off"* work without the
RGB Fusion app, which has to stay closed. The tray icon also has a **Lights (RGB)** menu with one-click looks
(Jarvis blue, Iron Man red, purple, warm white, rainbow, breathing, off). If OpenRGB isn't running, Jarvis starts it
minimized with its SDK server (it looks in `C:\Tools\OpenRGB`, Program Files, or `rgb.openrgb_path`).

Lighting steps work in routines too, e.g. `- rgb: {action: color, value: dim blue}` in `movie mode`.

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
                   memory_tools.py (remember / forget), chrome.py (tabs, pages, videos, Claude)
  chrome_bridge.py link to the Chrome extension (WebSocket on 127.0.0.1)
extension/chrome/  the Jarvis Control extension (load unpacked; --chrome-setup)
scripts/voice_smoke.py   Piper → wake word → Whisper check with no mic (also runs in GitHub Actions)
  diagnostics.py   --check
config.yaml        all settings
tests/             offline unit tests:  python -m unittest discover tests
```

## License

MIT, see [LICENSE](LICENSE). Copyright (c) 2026 Muhammad (Theycallmeinsane).

The models Jarvis downloads (Whisper, Piper voices, openWakeWord, Kokoro, Ollama models) have their own licenses; check them before redistributing.
