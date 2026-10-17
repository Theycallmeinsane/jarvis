"""Routines: named, multi-step workflows defined in config.yaml, run by voice or on a schedule.

routines:
  good morning:
    say: Good morning.                      # spoken first (optional)
    steps:                                  # tool calls, run in order
      - get_weather: {location: Paris}
      - calendar_upcoming: {days: 1}
      - web_lookup: {query: top news today, news: true}
    summarize: true                         # have the brain turn the results into a short briefing
    schedule: {time: "08:00", days: [mon, tue, wed, thu, fri]}   # optional, local time
"""
from __future__ import annotations

import datetime as dt
import difflib
import logging
import threading
import time
from typing import Callable

log = logging.getLogger(__name__)

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def routines_from(cfg) -> dict[str, dict]:
    return {str(k).lower(): (v or {}) for k, v in (cfg.get("routines") or {}).items()}


def find(cfg, name: str) -> tuple[str, dict] | None:
    routines = routines_from(cfg)
    key = name.lower().strip()
    if key in routines:
        return key, routines[key]
    close = difflib.get_close_matches(key, list(routines), n=1, cutoff=0.5)
    return (close[0], routines[close[0]]) if close else None


def run_steps(toolbox, routine: dict) -> list[str]:
    """Run each step's tool; a failing step is reported, not fatal."""
    out = []
    for step in routine.get("steps") or []:
        if isinstance(step, str):
            name, args = step, {}
        else:
            name, args = next(iter(step.items()))
            args = args or {}
        if name == "wait":   # - wait: 2   (seconds, e.g. to let an app finish opening)
            time.sleep(min(float(args or 1), 60))
            continue
        if not toolbox.get(name):
            out.append(f"{name}: not available")
            continue
        try:
            out.append(f"{name}: {toolbox.run(name, dict(args))}")
        except Exception as e:
            out.append(f"{name}: failed ({e})")
    return out


class Scheduler:
    """Fires scheduled routines at their time on their days (checked every 20 s)."""

    def __init__(self, cfg, fire: Callable[[str], None]):
        self.cfg = cfg
        self.fire = fire
        self._done: set[tuple[str, str]] = set()   # (routine, date) already run today
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="routines")

    def start(self) -> "Scheduler":
        if any((r.get("schedule") or {}).get("time") for r in routines_from(self.cfg).values()):
            self._thread.start()
        return self

    def stop(self):
        self._stop.set()

    def due(self, now: dt.datetime) -> list[str]:
        names = []
        for name, r in routines_from(self.cfg).items():
            sched = r.get("schedule") or {}
            at = str(sched.get("time") or "")
            if not at:
                continue
            days = [d.lower()[:3] for d in (sched.get("days") or DAYS)]
            hh, mm = (int(x) for x in at.split(":"))
            target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
            # within 2 minutes after the target, once per day
            if DAYS[now.weekday()] in days and target <= now < target + dt.timedelta(minutes=2):
                key = (name, now.date().isoformat())
                if key not in self._done:
                    self._done.add(key)
                    names.append(name)
        return names

    def _loop(self):
        while not self._stop.wait(20):
            for name in self.due(dt.datetime.now()):
                log.info("scheduled routine: %s", name)
                try:
                    self.fire(name)
                except Exception:
                    log.exception("routine %s failed", name)
