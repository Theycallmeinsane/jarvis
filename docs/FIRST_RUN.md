# First run with your own voice

Everything below was tested with synthesized speech; these steps check it with your real mic and room.
Takes about 10 minutes.

## 1. Components

```powershell
.\jarvis.bat --check
```
All five rows should say OK. If **microphone** shows the wrong device, run `.\jarvis.bat --list-devices`
and put the right index or part of its name in `config.local.yaml`:

```yaml
audio: {input_device: HyperX}
```

## 2. Wake word sensitivity

```powershell
.\jarvis.bat --wake-test
```
Say "Hey Jarvis" five times at your normal distance and volume, then talk normally for a bit / play a video.

* Your tries should peak well above `0.5` (usually 0.8–1.0).
* Normal talk and video should stay under it.

Adjust `wake_word.threshold` in `config.local.yaml` if needed (e.g. `0.4` if it misses you, `0.65` if it
fires on its own).

## 3. A conversation

```powershell
.\jarvis.bat
```
Try, in order:

| say | expect |
|---|---|
| "Hey Jarvis" … (pause) … "what time is it?" | chime, then the time |
| (straight after, no wake word) "and the weather in Lahore?" | weather |
| "Hey Jarvis, set a timer for one minute to stretch" (one breath) | timer set; a minute later it speaks the reminder |
| "Hey Jarvis, tell me a long story about a robot" then "Hey Jarvis" while it talks | it stops and listens |
| "Hey Jarvis, remember that my favourite tea is green tea" | "noted"; quit and restart Jarvis, then ask "what's my favourite tea?" |
| "Hey Jarvis, open Notepad" then "type hello from Jarvis" | Notepad opens and the text is typed |
| "Hey Jarvis, close Notepad" | asks "Shall I…?"; say "yes" |
| press **Ctrl+Alt+J** and ask anything | same as the wake word |
| "Hey Jarvis, what's on my screen?" | a short description |
| "Thanks Jarvis" | "At your service, sir." and back to waiting |

## 4. If something's off

| symptom | fix |
|---|---|
| cuts you off mid-sentence | `recording.silence_ms: 1200` |
| waits too long after you stop | `recording.silence_ms: 600` |
| mishears names | add them to `stt.hints` |
| hears itself on speakers and stops talking | `barge_in.threshold: 0.9`, or use a headset |
| replies too slowly | try `stt.model: base.en`; keep `max_tool_rounds: 1` |
| everything in `data/jarvis.log` (tray mode) | run `.\jarvis.bat --debug` in a console for details |
