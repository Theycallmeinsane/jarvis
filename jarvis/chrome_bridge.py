"""The link between Jarvis and the "Jarvis Control" Chrome extension (extension/chrome).

Jarvis runs a small WebSocket server on 127.0.0.1; the extension connects out to it and runs the commands
it's sent (tabs, navigation, and a fixed set of page actions). Only the extension's own origin is accepted,
so a web page can't pose as it.
"""
from __future__ import annotations

import itertools
import json
import logging
import threading

log = logging.getLogger(__name__)

EXTENSION_ID = "lbjbjdhghbfkdmnnmjphadllpfohiboj"   # fixed by the "key" in extension/chrome/manifest.json
DEFAULT_PORT = 8765


class NotConnected(Exception):
    pass


class Bridge:
    def __init__(self, port: int = DEFAULT_PORT, extra_ids: list[str] | None = None):
        self.port = port
        self.origins = [f"chrome-extension://{i}" for i in [EXTENSION_ID, *(extra_ids or [])]]
        self._conn = None
        self._pending: dict[int, tuple[threading.Event, dict]] = {}
        self._ids = itertools.count(1)
        self._lock = threading.Lock()
        self._server = None
        self.listening = False

    @property
    def connected(self) -> bool:
        return self._conn is not None

    def start(self) -> "Bridge":
        from websockets.sync.server import serve

        try:
            self._server = serve(self._handler, "127.0.0.1", self.port, origins=self.origins)
        except OSError as e:
            log.warning("Chrome link off: port %s is busy (%s). Change chrome.port in your settings.", self.port, e)
            return self
        threading.Thread(target=self._server.serve_forever, daemon=True, name="chrome-bridge").start()
        self.listening = True
        log.info("Chrome link listening on 127.0.0.1:%s", self.port)
        return self

    def stop(self):
        if self._server:
            self._server.shutdown()

    def _handler(self, ws):
        self._conn = ws
        log.info("Chrome extension connected")
        try:
            for raw in ws:
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                waiter = self._pending.get(msg.get("id"))
                if waiter:
                    waiter[1].update(msg)
                    waiter[0].set()
        except Exception:  # noqa: BLE001  (Chrome closed, extension reloaded)
            pass
        finally:
            if self._conn is ws:
                self._conn = None
                log.info("Chrome extension disconnected")

    def call(self, cmd: str, args: dict | None = None, timeout: float = 15):
        conn = self._conn
        if conn is None:
            raise NotConnected("Chrome isn't linked to Jarvis yet.")
        mid = next(self._ids)
        done, box = threading.Event(), {}
        self._pending[mid] = (done, box)
        try:
            with self._lock:
                conn.send(json.dumps({"id": mid, "cmd": cmd, "args": args or {}}))
            if not done.wait(timeout):
                raise TimeoutError(f"Chrome didn't answer the {cmd} command in time.")
        finally:
            self._pending.pop(mid, None)
        if not box.get("ok"):
            raise RuntimeError(box.get("error") or f"Chrome couldn't do {cmd}.")
        return box.get("result")


_bridge: Bridge | None = None


def get(cfg=None) -> Bridge:
    """The shared bridge, started on first use (and by the voice app at start-up)."""
    global _bridge
    if _bridge is None:
        c = (cfg.get("chrome") if cfg is not None else None) or {}
        _bridge = Bridge(int(c.get("port", DEFAULT_PORT)), c.get("extension_ids") or []).start()
    return _bridge
