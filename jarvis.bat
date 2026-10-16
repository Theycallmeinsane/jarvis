@echo off
rem Start Jarvis. Extra arguments pass through, e.g.  jarvis.bat --llm claude   or   jarvis.bat --text
cd /d "%~dp0"
set HF_HUB_DISABLE_SYMLINKS_WARNING=1
".venv\Scripts\python.exe" -m jarvis %*
