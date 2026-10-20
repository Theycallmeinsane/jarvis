"""Deeper Windows control: detailed stats, network, processes, services, startup apps, radios,
brightness, audio output device, clean-up and Windows Update status.

Everything that changes the system asks first; things that need admin rights say so instead of failing
mysteriously. System-critical processes and services are never touched.
"""
from __future__ import annotations

import ctypes
import datetime as dt
import logging
import os
import re
import socket
import subprocess
import time
from pathlib import Path

from .registry import Spoken, tool

log = logging.getLogger(__name__)
NO_WINDOW = 0x08000000


class SystemFailed(Exception):
    """Expected failure, phrased for the user (same role as builtin.ToolFailed)."""


def _fail(msg: str):
    from .builtin import ToolFailed

    raise ToolFailed(msg)


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # noqa: BLE001
        return False


ADMIN_HINT = "That needs administrator rights. Run Jarvis as administrator (right-click the shortcut) to allow it."

# ---------------------------------------------------------------- stats


def _gpu() -> dict | None:
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw,"
             "fan.speed", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5, creationflags=NO_WINDOW).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    if not out:
        return None
    f = [x.strip() for x in out.splitlines()[0].split(",")]
    num = lambda s: float(s) if re.fullmatch(r"[\d.]+", s) else None  # noqa: E731
    return {"name": f[0], "util": num(f[1]), "used": num(f[2]), "total": num(f[3]), "temp": num(f[4]),
            "power": num(f[5]), "fan": num(f[6]) if len(f) > 6 else None}


def _uptime() -> str:
    import psutil

    secs = int(time.time() - psutil.boot_time())
    d, rem = divmod(secs, 86400)
    h, m = divmod(rem // 60, 60)
    return (f"{d} day{'s' * (d != 1)}, " if d else "") + f"{h} hour{'s' * (h != 1)} {m} minutes"


def _throughput(seconds: float = 1.0) -> tuple[float, float]:
    import psutil

    a = psutil.net_io_counters()
    time.sleep(seconds)
    b = psutil.net_io_counters()
    return (b.bytes_recv - a.bytes_recv) / seconds, (b.bytes_sent - a.bytes_sent) / seconds


def _rate(bps: float) -> str:
    bits = bps * 8
    for unit, div in (("Gbps", 1e9), ("Mbps", 1e6), ("kbps", 1e3)):
        if bits >= div:
            return f"{bits / div:.1f} {unit}"
    return f"{bits:.0f} bps"


def _ping(host: str = "1.1.1.1", port: int = 443, timeout: float = 2.0) -> float | None:
    t0 = time.perf_counter()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return (time.perf_counter() - t0) * 1000
    except OSError:
        return None


@tool("Detailed computer statistics: CPU (load, speed, busiest core), GPU (load, VRAM, temperature, power), "
      "memory, disks, network speed, battery and uptime. Pick one section or 'all'.",
      {"section": {"type": "string", "enum": ["all", "cpu", "gpu", "memory", "disk", "network", "battery",
                                              "uptime"]}},
      required=[])
def system_stats(section: str = "all"):
    import psutil

    parts = []
    want = lambda s: section in ("all", s)  # noqa: E731
    if want("cpu"):
        per = psutil.cpu_percent(interval=0.5, percpu=True)
        freq = psutil.cpu_freq()
        speed = f" at {freq.current / 1000:.1f} GHz" if freq and freq.current else ""
        parts.append(f"CPU {sum(per) / len(per):.0f}% busy{speed}, busiest core {max(per):.0f}%")
    if want("gpu"):
        g = _gpu()
        if g:
            bits = [f"{g['util']:.0f}% busy" if g["util"] is not None else "",
                    f"{g['used'] / 1024:.1f} of {g['total'] / 1024:.0f} GB VRAM" if g["used"] else "",
                    f"{g['temp']:.0f} degrees" if g["temp"] is not None else "",
                    f"{g['power']:.0f} watts" if g["power"] is not None else ""]
            parts.append(f"GPU {g['name']}: " + ", ".join(b for b in bits if b))
        elif section == "gpu":
            parts.append("No NVIDIA GPU stats available on this PC")
    if want("memory"):
        vm = psutil.virtual_memory()
        parts.append(f"memory {vm.percent:.0f}% used, {vm.available / 2**30:.1f} GB free of {vm.total / 2**30:.0f} GB")
    if want("disk"):
        for p in psutil.disk_partitions():
            if "cdrom" in p.opts or not p.fstype:
                continue
            try:
                u = psutil.disk_usage(p.mountpoint)
                parts.append(f"drive {p.mountpoint[:1]} {u.free / 2**30:.0f} GB free of {u.total / 2**30:.0f} GB")
            except OSError:
                pass
    if want("network"):
        down, up = _throughput(1.0)
        ping = _ping()
        parts.append(f"network {_rate(down)} down, {_rate(up)} up right now, "
                     + (f"internet reachable in {ping:.0f} ms" if ping else "internet NOT reachable"))
    if want("battery"):
        bat = psutil.sensors_battery()
        if bat:
            plug = "charging" if bat.power_plugged else "on battery"
            parts.append(f"battery {bat.percent:.0f}%, {plug}")
        elif section == "battery":
            parts.append("This PC has no battery (it's a desktop)")
    if want("uptime"):
        parts.append(f"up for {_uptime()}")
    return "; ".join(parts) + "."


@tool("Internet / network status: connected or not, connection type, local IP, ping time, and current "
      "download/upload speed. Use for 'am I online', 'is the internet working', 'how fast is my internet'.",
      triggers=("internet", "network", "online", "connection", "connected", "wifi", "wi-fi", "wi fi", "ping",
                "ethernet", "speed", "ip", "download", "upload", "lag"))
def network_status():
    import psutil

    stats = psutil.net_if_stats()
    addrs = psutil.net_if_addrs()
    active = []
    for name, st in stats.items():
        if not st.isup or "loopback" in name.lower() or name.lower().startswith(("vethernet", "vmware", "virtualbox")):
            continue
        ipv4 = [a.address for a in addrs.get(name, []) if a.family == socket.AF_INET and not a.address.startswith("169.254")]
        if ipv4:
            kind = "Wi-Fi" if re.search(r"wi-?fi|wlan|wireless", name, re.I) else "Ethernet" if re.search(
                r"ethernet|lan", name, re.I) else name
            speed = f" at {st.speed / 1000:.1f} Gbps" if st.speed >= 1000 else (f" at {st.speed} Mbps" if st.speed else "")
            active.append(f"{kind}{speed} (IP {ipv4[0]})")
    ping = _ping()
    down, up = _throughput(1.0)
    online = f"Online, {ping:.0f} ms to the internet" if ping else "No internet connection right now"
    link = "; ".join(active) or "no active network adapter"
    return Spoken(f"{online}. Connected via {link}. Traffic right now: {_rate(down)} down, {_rate(up)} up.")


# ---------------------------------------------------------------- processes

CRITICAL = {"system", "system idle process", "idle", "registry", "smss", "csrss", "wininit", "winlogon", "services",
            "lsass", "lsaiso", "svchost", "fontdrvhost", "dwm", "memory compression", "secure system", "spoolsv",
            "explorer", "sihost", "ctfmon", "audiodg", "msmpeng", "securityhealthservice", "taskhostw",
            "runtimebroker", "startmenuexperiencehost", "searchhost", "shellexperiencehost", "conhost",
            "python", "pythonw", "ollama", "ollama app"}


def _procs_named(name: str):
    import psutil

    q = name.lower().replace(".exe", "").replace(" ", "")
    hits = []
    for p in psutil.process_iter(["name", "pid", "memory_info"]):
        pname = (p.info["name"] or "").lower().removesuffix(".exe")
        if q and q in pname.replace(" ", ""):
            hits.append(p)
    return hits


@tool("Find running programs/processes by name: how many, their memory use and CPU.",
      {"name": {"type": "string"}}, triggers=("process", "running", "program", "task", "app", "is", "find"))
def find_process(name: str):
    import psutil

    hits = _procs_named(name)
    if not hits:
        return Spoken(f"Nothing called {name} is running.")
    for p in hits:
        try:
            p.cpu_percent(None)
        except psutil.Error:
            pass
    time.sleep(0.4)
    mem = cpu = 0.0
    names = set()
    for p in hits:
        try:
            mem += p.info["memory_info"].rss
            cpu += p.cpu_percent(None) / psutil.cpu_count()
            names.add(p.info["name"])
        except (psutil.Error, AttributeError):
            pass
    return Spoken(f"{', '.join(sorted(names))}: {len(hits)} process{'es' * (len(hits) != 1)}, "
                  f"{mem / 2**20:.0f} MB of memory, {cpu:.0f}% CPU.")


def _end_question(args: dict) -> str:
    hits = _procs_named(args.get("name", ""))
    names = sorted({(p.info["name"] or "") for p in hits})
    if not hits:
        return f"End {args.get('name')}?"
    return f"End {', '.join(names)} ({len(hits)} process{'es' * (len(hits) != 1)})? Unsaved work in it will be lost."


@tool("End (kill) a running program by name, e.g. a frozen app. Asks first; refuses system-critical processes.",
      {"name": {"type": "string"}}, dangerous=True, confirm=_end_question,
      triggers=("end", "kill", "terminate", "force", "close", "stop", "frozen", "responding", "quit"))
def end_process(name: str):
    import psutil

    hits = _procs_named(name)
    if not hits:
        _fail(f"Nothing called {name} is running.")
    blocked = {(p.info["name"] or "").lower().removesuffix(".exe") for p in hits} & CRITICAL
    if blocked:
        _fail(f"I won't end {', '.join(sorted(blocked))}: Windows or Jarvis needs it to keep running.")
    ended = failed = 0
    for p in hits:
        try:
            p.terminate()
            ended += 1
        except psutil.AccessDenied:
            failed += 1
        except psutil.NoSuchProcess:
            pass
    gone, alive = psutil.wait_procs(hits, timeout=3)
    for p in alive:
        try:
            p.kill()
        except psutil.Error:
            pass
    if failed and not ended:
        _fail(f"Windows didn't let me end {name}. " + ("" if is_admin() else ADMIN_HINT))
    return Spoken(f"Ended {name} ({ended} process{'es' * (ended != 1)})." +
                  (f" {failed} couldn't be ended without admin rights." if failed else ""))


PRIORITIES = {"low": 0x40, "below normal": 0x4000, "normal": 0x20, "above normal": 0x8000, "high": 0x80}


@tool("Change a program's CPU priority: low, below normal, normal, above normal or high (never realtime).",
      {"name": {"type": "string"}, "level": {"type": "string", "enum": list(PRIORITIES)}},
      triggers=("priority", "prioritize", "prioritise", "boost", "faster", "slow down"),
      dangerous=True, confirm=lambda a: f"Set {a.get('name')} to {a.get('level')} priority?")
def set_priority(name: str, level: str):
    import psutil

    hits = _procs_named(name)
    if not hits:
        _fail(f"Nothing called {name} is running.")
    if {(p.info["name"] or "").lower().removesuffix(".exe") for p in hits} & CRITICAL:
        _fail(f"I won't change the priority of {name}; Windows manages it.")
    done = 0
    for p in hits:
        try:
            p.nice(PRIORITIES[level])
            done += 1
        except psutil.Error:
            pass
    if not done:
        _fail(f"Windows didn't let me change {name}'s priority. " + ("" if is_admin() else ADMIN_HINT))
    return Spoken(f"{name} is now at {level} priority.")


# ---------------------------------------------------------------- services

CRITICAL_SERVICES = {"rpcss", "dcomlaunch", "lsm", "samss", "eventlog", "plugplay", "power", "profsvc", "schedule",
                     "winmgmt", "cryptsvc", "bfe", "mpssvc", "windefend", "wscsvc", "audiosrv", "audioendpointbuilder",
                     "dhcp", "dnscache", "nsi", "netprofm", "lanmanworkstation", "brokerinfrastructure",
                     "systemeventsbroker", "coremessagingregistrar", "staterepository", "usermanager", "gpsvc"}


def _find_service(name: str):
    import psutil

    q = name.lower().replace(" ", "")
    best = None
    for s in psutil.win_service_iter():
        try:
            sname, disp = s.name(), s.display_name()
        except Exception:  # noqa: BLE001
            continue
        if q in (sname.lower(), disp.lower().replace(" ", "")):
            return s
        if best is None and (q in sname.lower() or q in disp.lower().replace(" ", "")):
            best = s
    return best


@tool("Status of a Windows service (e.g. 'print spooler', 'windows update', 'xbox').",
      {"name": {"type": "string"}}, triggers=("service", "spooler", "running", "status"))
def service_status(name: str):
    s = _find_service(name)
    if not s:
        _fail(f"I couldn't find a service called {name}.")
    info = s.as_dict()
    return Spoken(f"{info['display_name']} is {info['status']} (starts {info['start_type']}).")


def _service_question(args: dict) -> str:
    s = _find_service(args.get("name", ""))
    label = s.display_name() if s else args.get("name")
    return f"{args.get('action', '').capitalize()} the {label} service?"


@tool("Start, stop or restart a Windows service. Asks first; needs admin rights; never touches core services.",
      {"name": {"type": "string"}, "action": {"type": "string", "enum": ["start", "stop", "restart"]}},
      dangerous=True, confirm=_service_question, triggers=("service", "spooler", "start", "stop", "restart"))
def service_control(name: str, action: str):
    s = _find_service(name)
    if not s:
        _fail(f"I couldn't find a service called {name}.")
    sname = s.name()
    if sname.lower() in CRITICAL_SERVICES:
        _fail(f"I won't {action} {s.display_name()}: Windows depends on it.")
    if not is_admin():
        _fail(ADMIN_HINT)
    cmds = {"start": [["sc", "start", sname]], "stop": [["sc", "stop", sname]],
            "restart": [["sc", "stop", sname], ["sc", "start", sname]]}[action]
    for i, c in enumerate(cmds):
        subprocess.run(c, capture_output=True, text=True, timeout=30, creationflags=NO_WINDOW)
        if action == "restart" and i == 0:
            time.sleep(2)
    time.sleep(1.5)
    return Spoken(f"{s.display_name()} is now {_find_service(sname).status()}.")


# ---------------------------------------------------------------- startup apps

RUN_KEYS = [("HKCU", r"Software\Microsoft\Windows\CurrentVersion\Run"),
            ("HKLM", r"Software\Microsoft\Windows\CurrentVersion\Run")]
APPROVED = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run"


def _startup_entries() -> list[dict]:
    import winreg

    hives = {"HKCU": winreg.HKEY_CURRENT_USER, "HKLM": winreg.HKEY_LOCAL_MACHINE}
    out = []
    for hive, path in RUN_KEYS:
        try:
            with winreg.OpenKey(hives[hive], path) as k:
                i = 0
                while True:
                    try:
                        name, value, _ = winreg.EnumValue(k, i)
                    except OSError:
                        break
                    out.append({"name": name, "hive": hive, "command": value, "enabled": True})
                    i += 1
        except OSError:
            continue
        try:   # Task Manager's enable/disable switch: first byte 2 = enabled, 3 = disabled
            with winreg.OpenKey(hives[hive], APPROVED) as k:
                for e in out:
                    if e["hive"] == hive:
                        try:
                            data, _ = winreg.QueryValueEx(k, e["name"])
                            e["enabled"] = not (data and data[0] % 2 == 1)
                        except OSError:
                            pass
        except OSError:
            pass
    folder = Path(os.environ.get("APPDATA", "")) / r"Microsoft\Windows\Start Menu\Programs\Startup"
    if folder.exists():
        for f in folder.iterdir():
            if f.suffix.lower() in (".lnk", ".bat", ".cmd", ".exe", ".url"):
                out.append({"name": f.stem, "hive": "folder", "command": str(f), "enabled": True})
    return out


@tool("List the apps that start automatically with Windows (and whether each is enabled).",
      triggers=("startup", "start up", "boot", "automatically", "autostart", "launch at", "on login",
                "with windows", "when windows start"))
def list_startup_apps():
    entries = _startup_entries()
    if not entries:
        return Spoken("Nothing is set to start with Windows.")
    spoken = lambda n: re.sub(r"_[0-9A-F]{16,}$", "", n)  # noqa: E731  "MicrosoftEdgeAutoLaunch_7DD8..."
    on = [spoken(e["name"]) for e in entries if e["enabled"]]
    off = [spoken(e["name"]) for e in entries if not e["enabled"]]
    text = f"{len(on)} app{'s' * (len(on) != 1)} start with Windows: {', '.join(on) or 'none'}."
    if off:
        text += f" Disabled: {', '.join(off)}."
    return Spoken(text)


@tool("Enable or disable an app starting with Windows (like Task Manager's Startup tab). Asks first.",
      {"name": {"type": "string"}, "enabled": {"type": "boolean"}},
      dangerous=True, triggers=("startup", "start up", "boot", "automatically", "autostart", "on login",
                                "with windows"),
      confirm=lambda a: f"{'Enable' if a.get('enabled') else 'Disable'} {a.get('name')} at startup?")
def set_startup_app(name: str, enabled: bool):
    import difflib
    import winreg

    entries = [e for e in _startup_entries() if e["hive"] != "folder"]
    names = {e["name"].lower(): e for e in entries}
    key = next((k for k in names if name.lower() in k), None) or \
        next(iter(difflib.get_close_matches(name.lower(), list(names), n=1, cutoff=0.5)), None)
    if not key:
        _fail(f"{name} isn't in the startup list.")
    e = names[key]
    if e["hive"] == "HKLM" and not is_admin():
        _fail(f"{e['name']} is set up for all users. {ADMIN_HINT}")
    hive = winreg.HKEY_CURRENT_USER if e["hive"] == "HKCU" else winreg.HKEY_LOCAL_MACHINE
    with winreg.CreateKeyEx(hive, APPROVED, 0, winreg.KEY_SET_VALUE) as k:
        winreg.SetValueEx(k, e["name"], 0, winreg.REG_BINARY, bytes([2 if enabled else 3]) + bytes(11))
    return Spoken(f"{e['name']} will {'now' if enabled else 'no longer'} start with Windows.")


# ---------------------------------------------------------------- radios (Bluetooth / Wi-Fi / airplane)

def _radios(kind: str | None, state: str | None):
    import asyncio

    from winrt.windows.devices.radios import Radio, RadioAccessStatus, RadioKind, RadioState

    async def run():
        if await Radio.request_access_async() != RadioAccessStatus.ALLOWED:
            return None
        found = []
        for r in await Radio.get_radios_async():
            k = RadioKind(r.kind).name.lower()
            if kind and kind != k:
                continue
            if state:
                await r.set_state_async(RadioState.ON if state == "on" else RadioState.OFF)
            found.append((r.name, k, RadioState(r.state).name.lower()))
        return found
    return asyncio.run(run())


@tool("Turn Bluetooth or Wi-Fi on/off, airplane mode (all radios off/on), or say their current state.",
      {"radio": {"type": "string", "enum": ["bluetooth", "wifi", "airplane"]},
       "action": {"type": "string", "enum": ["on", "off", "status"]}},
      triggers=("bluetooth", "blue tooth", "wifi", "wi-fi", "wi fi", "airplane", "flight mode", "radio", "wireless"))
def radio(radio: str, action: str):
    kind = {"bluetooth": "bluetooth", "wifi": "wi_fi", "airplane": None}[radio]
    if radio == "airplane" and action != "status":
        state = "off" if action == "on" else "on"       # airplane ON = radios OFF
    else:
        state = None if action == "status" else action
    try:
        found = _radios(kind, state)
    except ImportError:
        _fail("Radio control needs the winrt-Windows.Devices.Radios package (run setup.ps1).")
    if found is None:
        _fail("Windows didn't allow radio control. Check Settings, Privacy, Radios.")
    if not found:
        what = {"wifi": "a Wi-Fi adapter", "bluetooth": "Bluetooth"}.get(radio, "any radios")
        _fail(f"This PC doesn't have {what}.")
    if state is None:
        return Spoken("; ".join(f"{n} is {s}" for n, _, s in found) + ".")
    if radio == "airplane":
        return Spoken(f"Airplane mode {'on' if action == 'on' else 'off'}: radios are {state}.")
    return Spoken(f"{found[0][0]} is now {state}.")


# ---------------------------------------------------------------- brightness

@tool("Set or check screen brightness (laptop screens and external monitors that support DDC/CI).",
      {"action": {"type": "string", "enum": ["set", "up", "down", "get"]},
       "percent": {"type": "integer", "description": "0-100 for set; step for up/down (default 15)"}},
      required=["action"], triggers=("brightness", "brighter", "dimmer", "dim", "bright", "darker", "screen",
                                     "monitor"))
def brightness(action: str, percent: int | None = None):
    levels = []
    try:   # external monitors over DDC/CI
        from monitorcontrol import get_monitors

        for m in get_monitors():
            with m:
                cur = m.get_luminance()
                if action != "get":
                    step = percent if percent is not None else 15
                    target = {"set": step, "up": cur + step, "down": cur - step}[action]
                    m.set_luminance(max(0, min(100, int(target))))
                    cur = m.get_luminance()
                levels.append(cur)
    except Exception as e:  # noqa: BLE001
        log.debug("DDC/CI brightness failed: %s", e)
    if not levels:   # laptop panel through WMI
        from .builtin import _ps

        cur = _ps("(Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightness).CurrentBrightness")
        if not cur.strip().isdigit():
            _fail("This display doesn't let Windows change its brightness; use the buttons on the monitor.")
        cur = int(cur.strip())
        if action != "get":
            step = percent if percent is not None else 15
            target = max(0, min(100, int({"set": step, "up": cur + step, "down": cur - step}[action])))
            _ps(f"(Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightnessMethods)"
                f".WmiSetBrightness(1,{target})")
            cur = target
        levels.append(cur)
    word = "is at" if action == "get" else "set to"
    return Spoken(f"Brightness {word} {levels[0]}%." if len(levels) == 1 else
                  f"Brightness {word} " + ", ".join(f"{x}%" for x in levels) + " on your monitors.")


# ---------------------------------------------------------------- audio output device

def _output_devices():
    from pycaw.constants import DEVICE_STATE, EDataFlow
    from pycaw.pycaw import AudioUtilities

    return [(d.FriendlyName, d.id) for d in AudioUtilities.GetAllDevices(
        data_flow=EDataFlow.eRender.value, device_state=DEVICE_STATE.ACTIVE.value) if d.FriendlyName]


def _set_default_endpoint(device_id: str):
    """IPolicyConfig (undocumented but stable since Vista) is what the Sound settings page uses."""
    from ctypes import HRESULT, c_int, c_void_p
    from ctypes.wintypes import LPCWSTR

    import comtypes
    from comtypes import COMMETHOD, GUID, IUnknown

    class IPolicyConfig(IUnknown):
        _iid_ = GUID("{f8679f50-850a-41cf-9c72-430f290290c8}")
        _methods_ = [COMMETHOD([], HRESULT, f"Unused{i}", (["in"], c_void_p, "a"), (["in"], c_void_p, "b"))
                     for i in range(10)] + [
            COMMETHOD([], HRESULT, "SetDefaultEndpoint", (["in"], LPCWSTR, "id"), (["in"], c_int, "role")),
        ]

    policy = comtypes.CoCreateInstance(GUID("{870af99c-171d-4f9e-af0d-e63df40c2bc9}"), interface=IPolicyConfig)
    for role in (0, 1, 2):   # console, multimedia, communications
        policy.SetDefaultEndpoint(device_id, role)


@tool("Switch which speakers/headphones Windows plays sound through, or list them.",
      {"device": {"type": "string", "description": "Part of the device name, e.g. 'headset', 'monitor', 'hyperx'; "
                                                   "empty to list"}},
      required=[], triggers=("output", "speaker", "headphone", "headset", "sound", "audio", "device", "play through",
                             "switch"))
def audio_output(device: str = ""):
    import difflib

    import comtypes

    comtypes.CoInitialize()
    try:
        devices = _output_devices()
        from pycaw.pycaw import AudioUtilities

        current = getattr(AudioUtilities.GetSpeakers(), "FriendlyName", "")
        if not device:
            return Spoken(f"Sound is playing through {current}. Other outputs: " +
                          ", ".join(n for n, _ in devices if n != current) + ".")
        q = device.lower()
        alias = {"headphones": "headset", "monitor": "vg", "tv": "hdmi", "speakers": "speakers"}
        hit = next(((n, i) for n, i in devices if q in n.lower() or alias.get(q, "~") in n.lower()), None)
        if not hit:
            close = difflib.get_close_matches(q, [n.lower() for n, _ in devices], n=1, cutoff=0.3)
            hit = next(((n, i) for n, i in devices if close and n.lower() == close[0]), None)
        if not hit:
            _fail(f"I couldn't find an output called {device}. I have: {', '.join(n for n, _ in devices)}.")
        _set_default_endpoint(hit[1])
        now = getattr(AudioUtilities.GetSpeakers(), "FriendlyName", "")
        if now != hit[0]:
            _fail(f"Windows didn't switch to {hit[0]}.")
        return Spoken(f"Sound now plays through {hit[0]}. (Jarvis's own voice switches after a restart.)")
    finally:
        comtypes.CoUninitialize()


# ---------------------------------------------------------------- clean-up

def _recycle_bin_info() -> tuple[int, int]:
    class SHQUERYRBINFO(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_ulong), ("i64Size", ctypes.c_longlong), ("i64NumItems", ctypes.c_longlong)]

    info = SHQUERYRBINFO(ctypes.sizeof(SHQUERYRBINFO), 0, 0)
    ctypes.windll.shell32.SHQueryRecycleBinW(None, ctypes.byref(info))
    return int(info.i64NumItems), int(info.i64Size)


def _human(n: float) -> str:
    for unit in ("bytes", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "bytes" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


@tool("How much is in the Recycle Bin.", triggers=("recycle", "bin", "trash", "deleted"))
def recycle_bin_status():
    items, size = _recycle_bin_info()
    return Spoken("The Recycle Bin is empty." if not items else f"The Recycle Bin has {items} items, {_human(size)}.")


def _empty_question(args: dict) -> str:
    items, size = _recycle_bin_info()
    return f"Permanently delete the {items} items ({_human(size)}) in the Recycle Bin? This can't be undone."


@tool("Empty the Recycle Bin (permanent). Always asks first and says how much will be deleted.",
      dangerous=True, confirm=_empty_question, triggers=("empty", "recycle", "bin", "trash", "clear"))
def empty_recycle_bin():
    items, size = _recycle_bin_info()
    if not items:
        return Spoken("The Recycle Bin is already empty.")
    # SHERB_NOCONFIRMATION | SHERB_NOPROGRESSUI | SHERB_NOSOUND: Jarvis already asked out loud
    ctypes.windll.shell32.SHEmptyRecycleBinW(None, None, 0x1 | 0x2 | 0x4)
    left, _ = _recycle_bin_info()
    if left:
        _fail(f"Windows emptied part of it; {left} items are left.")
    return Spoken(f"Emptied the Recycle Bin: {items} items, {_human(size)} freed.")


def _temp_candidates(days: int = 2, root: str | None = None) -> tuple[list[Path], int]:
    base = Path(root or os.environ.get("TEMP", ""))
    cutoff = time.time() - days * 86400
    files, total = [], 0
    if not base.is_dir():
        return files, total
    for dirpath, _, names in os.walk(base):
        for n in names:
            p = Path(dirpath) / n
            try:
                st = p.stat()
            except OSError:
                continue
            if st.st_mtime < cutoff:
                files.append(p)
                total += st.st_size
    return files, total


def _temp_question(args: dict) -> str:
    files, total = _temp_candidates()
    return f"Delete {len(files)} temporary files older than two days ({_human(total)})? Apps recreate them if needed."


@tool("Clear old temporary files (older than two days, in your Temp folder) to free space. Asks first.",
      dangerous=True, confirm=_temp_question, triggers=("temp", "temporary", "clean", "clear", "junk", "space"))
def clear_temp_files(ctx=None):
    root = ((ctx.cfg.get("system") or {}).get("temp_dir") if ctx else None) or None
    files, total = _temp_candidates(root=root)
    freed = removed = 0
    for p in files:
        try:
            size = p.stat().st_size
            p.unlink()
            removed += 1
            freed += size
        except OSError:
            pass      # in use by a running app: leave it
    return Spoken(f"Removed {removed} temporary files and freed {_human(freed)}."
                  + (f" {len(files) - removed} were in use and stayed." if removed < len(files) else ""))


# ---------------------------------------------------------------- Windows Update

@tool("Windows Update status: when updates were last installed and whether a restart is pending.",
      triggers=("update", "windows update", "patch", "restart pending", "reboot"))
def windows_update_status():
    import winreg

    from .builtin import _ps

    pending = False
    for path in (r"SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired",
                 r"SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending"):
        try:
            winreg.CloseKey(winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path))
            pending = True
        except OSError:
            pass
    last = _ps("Get-HotFix | Sort-Object InstalledOn -Descending | Select-Object -First 1 | "
               "ForEach-Object { '{0} on {1:d MMMM yyyy}' -f $_.HotFixID, $_.InstalledOn }", timeout=40)
    text = f"The last update installed was {last}." if last else "I couldn't read the update history."
    text += " A restart is pending to finish installing updates." if pending else " No restart is pending."
    return Spoken(text + " Say 'open Windows Update' to check for new ones.")

# ---------------------------------------------------------------- fast-path phrases (called from router.route)

def route_system(t: str, has) -> list[tuple[str, dict]] | None:
    """Clear-cut system phrases -> tool calls without the brain. `t` is the router's normalized text."""
    if has("system_stats"):
        m = re.fullmatch(r"(?:what(?:'s| is) )?(?:my |the )?(?P<s>cpu|processor|gpu|graphics card|memory|ram|disk|"
                         r"storage|battery|uptime)(?: usage| load| temperature| temp| use| space| status| stats| level)?"
                         r"(?: like| now| right now)?", t) \
            or re.fullmatch(r"how (?:hot|busy) is (?:my |the )?(?P<s>cpu|processor|gpu|graphics card)", t) \
            or re.fullmatch(r"how much (?P<s>ram|memory|disk space|storage) (?:is |am i )?(?:left|free|used|using)", t)
        if m:
            s = {"processor": "cpu", "graphics card": "gpu", "ram": "memory", "storage": "disk",
                 "disk space": "disk"}.get(m.group("s"), m.group("s"))
            return [("system_stats", {"section": s})]
        if re.fullmatch(r"(?:show |give me |tell me )?(?:my |the )?(?:system|computer|pc) (?:stats|statistics|status|"
                        r"health|info|report)|how(?:'s| is) my (?:pc|computer|system) doing", t):
            return [("system_stats", {"section": "all"})]
        if re.fullmatch(r"how long (?:has|have) (?:my |the )?(?:pc|computer|system|i) been (?:on|up|running)", t):
            return [("system_stats", {"section": "uptime"})]
    if has("network_status") and re.fullmatch(
            r"(?:am i|are we) (?:online|connected)(?: to the internet)?|is (?:my |the )?(?:internet|wi-?fi|network|"
            r"connection) (?:working|up|down|ok|okay)|(?:check |test )?(?:my |the )?(?:internet|network|connection)"
            r"(?: speed| status| connection)?|how fast is (?:my |the )?(?:internet|connection)", t):
        return [("network_status", {})]
    if has("brightness"):
        m = re.fullmatch(r"(?:set |change |turn )?(?:the |screen |monitor )?brightness(?: to| at)? (\d{1,3})"
                         r"(?: ?%| percent)?", t)
        if m:
            return [("brightness", {"action": "set", "percent": int(m.group(1))})]
        if re.fullmatch(r"(?:make (?:the |my )?screen |turn (?:the )?brightness |screen |brightness )?"
                        r"(?:brighter|up)(?: a bit| a little)?", t) and "bright" in t:
            return [("brightness", {"action": "up"})]
        if re.fullmatch(r"(?:make (?:the |my )?screen |turn (?:the )?brightness |screen |brightness )?"
                        r"(?:dimmer|darker|down|dim)(?: a bit| a little)?", t) and ("bright" in t or "dim" in t
                                                                                    or "dark" in t):
            return [("brightness", {"action": "down"})]
    if has("radio"):
        m = re.fullmatch(r"(?:turn |switch )?(?P<a>on|off) (?:the )?(?P<r>bluetooth|wi-?fi|airplane mode|flight mode)"
                         r"|(?:turn |switch )?(?:the )?(?P<r2>bluetooth|wi-?fi|airplane mode|flight mode) (?P<a2>on|off)", t)
        if m:
            r = (m.group("r") or m.group("r2")).replace("-", "")
            r = {"flight mode": "airplane", "airplane mode": "airplane"}.get(r, r)
            return [("radio", {"radio": r, "action": m.group("a") or m.group("a2")})]
    if has("audio_output"):
        m = re.fullmatch(r"(?:switch|change|move|play)(?: the)? (?:sound|audio|output)(?: output)? (?:to|through|on) "
                         r"(?:the |my )?(?P<d>[\w .-]+)", t)
        if m:
            return [("audio_output", {"device": m.group("d")})]
    if has("recycle_bin_status") and re.fullmatch(r"(?:what(?:'s| is) in |how full is |check )(?:the |my )?"
                                                   r"(?:recycle bin|trash|bin)", t):
        return [("recycle_bin_status", {})]
    if has("list_startup_apps") and re.fullmatch(r"(?:what|which|list|show)(?: me)?(?: the)? (?:apps|programs) "
                                                  r"(?:start|run|open|launch) (?:with|at|on) (?:windows|startup|boot)"
                                                  r"|(?:list |show )?(?:my )?startup (?:apps|programs)", t):
        return [("list_startup_apps", {})]
    if has("windows_update_status") and re.fullmatch(r"(?:check |any )?(?:for )?(?:windows )?updates?(?: status)?|"
                                                      r"is windows up to date", t):
        return [("windows_update_status", {})]
    return None
