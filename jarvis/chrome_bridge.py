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
DEFAULT_PORT = 47815      # uncommon on purpose: 8765 and friends are taken by dev servers


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
            log.warning("Chrome link off: port %s is busy (%s). Set chrome.port in config.local.yaml and the same "
                        "port in the Jarvis Control extension's popup.", self.port, e)
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


def port_status(port: int) -> tuple[bool, str]:
    """For --check: (ok, message) about the port the extension connects to."""
    import socket

    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", port))
            return True, f"port {port} free; link Chrome with: jarvis.bat --chrome-setup"
        except OSError:
            pass
    from websockets.exceptions import InvalidStatus
    from websockets.sync.client import connect

    try:   # a wrong origin: Jarvis turns it away with 403, without touching a linked extension
        connect(f"ws://127.0.0.1:{port}/chrome", origin="http://jarvis-check.invalid", open_timeout=3).close()
    except InvalidStatus as e:
        if e.response.status_code == 403:
            return True, f"Jarvis is already listening on port {port}"
    except Exception:  # noqa: BLE001
        pass
    return False, (f"port {port} is used by another program. Pick a free one in config.local.yaml "
                   "(chrome: port: ...) and set the same port in the Jarvis Control extension's popup")


_bridge: Bridge | None = None


def get(cfg=None) -> Bridge:
    """The shared bridge, started on first use (and by the voice app at start-up)."""
    global _bridge
    if _bridge is None:
        c = (cfg.get("chrome") if cfg is not None else None) or {}
        _bridge = Bridge(int(c.get("port", DEFAULT_PORT)), c.get("extension_ids") or []).start()
    return _bridge
