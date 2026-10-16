# Start Jarvis in the system tray without a console window.
# Run with the venv's pythonw:  .venv\Scripts\pythonw.exe jarvis_tray.pyw   (or use jarvis-tray.bat)
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from jarvis.__main__ import main  # noqa: E402

main(["--tray"])
