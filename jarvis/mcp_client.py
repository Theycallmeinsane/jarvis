"""MCP (Model Context Protocol) client: plug any MCP server's tools into Jarvis via config.yaml.

mcp_servers:
  files:                                   # name (used as the tool prefix)
    command: npx
    args: ["-y", "@modelcontextprotocol/server-filesystem", "C:/Users/me/Documents"]
    confirm: auto                          # auto (default): ask before tools not marked read-only
                                           # always | never
    tools: [read_file, list_directory]     # optional allow-list (small local models like few tools)
  remote:
    url: http://localhost:8000/mcp         # streamable HTTP server
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
import threading
from typing import Any

from .config import ROOT

log = logging.getLogger(__name__)


def _text_of(result) -> str:
    parts = []
    for item in getattr(result, "content", None) or []:
        text = getattr(item, "text", None)
        parts.append(text if text is not None else f"[{getattr(item, 'type', 'content')}]")
    structured = getattr(result, "structured_content", None)
    if not parts and structured is not None:
        parts.append(str(structured))
    out = "\n".join(parts).strip() or "(no output)"
    return ("Error: " + out) if getattr(result, "is_error", False) else out


class MCPManager:
    """Runs an asyncio loop in a background thread; one long-lived session task per server."""

    def __init__(self, servers: dict[str, dict]):
        self.servers = servers
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True, name="mcp")
        self.sessions: dict[str, Any] = {}
        self.tools: dict[str, list] = {}       # server -> list of mcp Tool objects
        self._stops: dict[str, asyncio.Event] = {}
        self._errlog = None

    # ------------------------------------------------------------ lifecycle

    def start(self, timeout: float = 45) -> "MCPManager":
        self.thread.start()
        (ROOT / "data").mkdir(exist_ok=True)
        self._errlog = open(ROOT / "data" / "mcp.log", "a", encoding="utf-8")   # servers' stderr
        for name, spec in self.servers.items():
            ready: "asyncio.Future" = asyncio.run_coroutine_threadsafe(self._make_future(), self.loop).result()
            asyncio.run_coroutine_threadsafe(self._serve(name, spec or {}, ready), self.loop)
            try:
                tools = asyncio.run_coroutine_threadsafe(self._wait(ready, timeout), self.loop).result(timeout + 5)
                self.tools[name] = tools
                log.info("MCP %s: %d tools", name, len(tools))
            except Exception as e:
                log.warning("MCP server %s unavailable: %s", name, e)
        return self

    async def _make_future(self):
        return self.loop.create_future()

    @staticmethod
    async def _wait(fut, timeout):
        return await asyncio.wait_for(asyncio.shield(fut), timeout)

    async def _serve(self, name: str, spec: dict, ready: "asyncio.Future"):
        """Open the transport + session, report the tool list, then hold it open until stop()."""
        from mcp import ClientSession

        stop = asyncio.Event()
        self._stops[name] = stop
        try:
            async with self._transport(spec) as streams:
                read, write = streams[0], streams[1]
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    listed = await session.list_tools()
                    self.sessions[name] = session
                    if not ready.done():
                        ready.set_result(list(listed.tools))
                    await stop.wait()
        except BaseException as e:  # noqa: BLE001 - report anything (incl. task-group errors) to start()
            if not ready.done():
                ready.set_exception(e if isinstance(e, Exception) else RuntimeError(str(e)))
            else:
                log.warning("MCP server %s stopped: %s", name, e)
        finally:
            self.sessions.pop(name, None)

    def _transport(self, spec: dict):
        if spec.get("url"):
            from mcp.client.streamable_http import streamable_http_client

            return streamable_http_client(spec["url"])
        from mcp import StdioServerParameters
        from mcp.client.stdio import stdio_client

        command = spec.get("command") or ""
        if command in ("python", "python3"):
            command = sys.executable    # use the venv's Python for Python-based servers
        params = StdioServerParameters(
            command=command, args=[str(a) for a in spec.get("args") or []],
            env={**os.environ, **{k: str(v) for k, v in (spec.get("env") or {}).items()}},
            cwd=spec.get("cwd"))
        return stdio_client(params, errlog=self._errlog or sys.stderr)

    def stop(self):
        for ev in list(self._stops.values()):
            self.loop.call_soon_threadsafe(ev.set)

    # ------------------------------------------------------------ calls

    def call(self, server: str, tool: str, arguments: dict, timeout: float = 90) -> str:
        session = self.sessions.get(server)
        if session is None:
            raise RuntimeError(f"MCP server {server} is not connected")
        fut = asyncio.run_coroutine_threadsafe(session.call_tool(tool, arguments or {}), self.loop)
        return _text_of(fut.result(timeout))


_manager: MCPManager | None = None
_lock = threading.Lock()


def get_manager(cfg) -> MCPManager | None:
    """Start the configured servers once per process."""
    global _manager
    servers = cfg.get("mcp_servers") or {}
    if not servers:
        return None
    with _lock:
        if _manager is None:
            _manager = MCPManager(dict(servers)).start()
    return _manager


def _safe_name(server: str, tool: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", f"{server}_{tool}")[:64]


def mcp_tools(cfg) -> list:
    """Wrap every allowed MCP tool as a Jarvis Tool."""
    from .tools.registry import Tool

    manager = get_manager(cfg)
    if manager is None:
        return []
    out = []
    for server, tools in manager.tools.items():
        spec = (cfg.get("mcp_servers") or {}).get(server) or {}
        allow = set(spec.get("tools") or [])
        policy = str(spec.get("confirm", "auto")).lower()
        for t in tools:
            if allow and t.name not in allow:
                continue
            schema = dict(getattr(t, "input_schema", None) or {})
            schema.setdefault("type", "object")
            schema.setdefault("properties", {})
            schema.setdefault("required", [])
            ann = getattr(t, "annotations", None)
            read_only = bool(getattr(ann, "read_only_hint", False)) if ann else False
            dangerous = policy == "always" or (policy == "auto" and not read_only)

            def call(_server=server, _tool=t.name, **kwargs):
                return manager.call(_server, _tool, kwargs)

            desc = (t.description or t.name).strip()[:600] + f" (via {server})"
            out.append(Tool(_safe_name(server, t.name), desc, schema, call, dangerous=dangerous))
    return out
