@echo off
rem Start Jarvis in the system tray (no console window). Right-click the tray icon for options.
cd /d "%~dp0"
start "" ".venv\Scripts\pythonw.exe" "%~dp0jarvis_tray.pyw"
