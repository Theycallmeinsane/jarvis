"""Google Calendar + Gmail tools.

One-time setup (see README "Google Calendar & Gmail"):
  1. Google Cloud console -> enable Calendar API + Gmail API -> OAuth client ID (Desktop app)
  2. save the JSON as secrets/google_credentials.json
  3. jarvis.bat --google-login   (opens the browser once; token saved to secrets/google_token.json)
The tools only appear once the credentials file exists. Both files are git-ignored.
"""
from __future__ import annotations

import base64
import datetime as dt
import logging
import re
from email.mime.text import MIMEText
from email.utils import parseaddr

from ..config import resolve_path
from .registry import tool

log = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",   # drafts + send
]
_services: dict[str, object] = {}


def _paths(cfg):
    g = cfg.get("google") or {}
    return (resolve_path(g.get("credentials_file", "secrets/google_credentials.json")),
            resolve_path(g.get("token_file", "secrets/google_token.json")))


def google_enabled(cfg) -> bool:
    return (cfg.get("google") or {}).get("enabled", True) and _paths(cfg)[0].exists()


def login(cfg) -> str:
    """Interactive OAuth consent in the browser; run once from the command line."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    creds_file, token_file = _paths(cfg)
    if not creds_file.exists():
        return f"No OAuth client at {creds_file} yet. Run: jarvis.bat --google-setup"
    flow = InstalledAppFlow.from_client_secrets_file(str(creds_file), SCOPES)
    creds = flow.run_local_server(port=0, prompt="consent")
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text(creds.to_json(), encoding="utf-8")
    _services.clear()
    return f"Google connected. Token saved to {token_file}."


def _credentials(cfg):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    _, token_file = _paths(cfg)
    if not token_file.exists():
        raise RuntimeError("Google isn't connected yet. Run: jarvis.bat --google-login")
    creds = Credentials.from_authorized_user_file(str(token_file), SCOPES)
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            token_file.write_text(creds.to_json(), encoding="utf-8")
        else:
            raise RuntimeError("Google login expired. Run: jarvis.bat --google-login")
    return creds


def _service(ctx, name: str):
    if name not in _services:
        from googleapiclient.discovery import build

        version = {"calendar": "v3", "gmail": "v1"}[name]
        _services[name] = build(name, version, credentials=_credentials(ctx.cfg), cache_discovery=False)
    return _services[name]


def _local_tz():
    return dt.datetime.now().astimezone().tzinfo


def _parse_when(text: str) -> dt.datetime:
    """Accept ISO-ish strings from the model: 2026-10-17T15:00, 2026-10-17 15:00, ..."""
    text = text.strip().replace(" ", "T", 1) if re.match(r"\d{4}-\d\d-\d\d \d", text.strip()) else text.strip()
    when = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    return when if when.tzinfo else when.replace(tzinfo=_local_tz())


def _say_time(iso: str) -> str:
    if "T" not in iso:  # all-day event
        return dt.date.fromisoformat(iso).strftime("%A %d %B (all day)")
    t = dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(_local_tz())
    return t.strftime("%A %d %B at %I:%M %p").replace(" 0", " ")


# ---------------------------------------------------------------- calendar

@tool("List upcoming Google Calendar events.",
      {"days": {"type": "integer", "description": "How many days ahead to look (default 1 = rest of today)"},
       "max_results": {"type": "integer", "description": "default 10"}},
      required=[], enabled=google_enabled)
def calendar_upcoming(days: int = 1, max_results: int = 10, ctx=None):
    now = dt.datetime.now().astimezone()
    end = (now + dt.timedelta(days=max(days, 1) - 1)).replace(hour=23, minute=59, second=59)
    items = _service(ctx, "calendar").events().list(
        calendarId="primary", timeMin=now.isoformat(), timeMax=end.isoformat(),
        singleEvents=True, orderBy="startTime", maxResults=max_results).execute().get("items", [])
    if not items:
        return "No events in that period."
    lines = []
    for e in items:
        start = e["start"].get("dateTime") or e["start"].get("date")
        where = f" at {e['location']}" if e.get("location") else ""
        lines.append(f"{_say_time(start)}: {e.get('summary', '(no title)')}{where}")
    return "\n".join(lines)


@tool("Add an event to Google Calendar.",
      {"title": {"type": "string"},
       "start": {"type": "string", "description": "Local start time, ISO format e.g. 2026-10-17T15:00"},
       "duration_minutes": {"type": "integer", "description": "default 60"},
       "location": {"type": "string"},
       "description": {"type": "string"}},
      required=["title", "start"], enabled=google_enabled)
def calendar_add_event(title: str, start: str, duration_minutes: int = 60, location: str = "",
                       description: str = "", ctx=None):
    begin = _parse_when(start)
    body = {"summary": title, "start": {"dateTime": begin.isoformat()},
            "end": {"dateTime": (begin + dt.timedelta(minutes=duration_minutes or 60)).isoformat()}}
    if location:
        body["location"] = location
    if description:
        body["description"] = description
    _service(ctx, "calendar").events().insert(calendarId="primary", body=body).execute()
    return f"Added '{title}' on {_say_time(begin.isoformat())}."


# ---------------------------------------------------------------- gmail

def _header(msg, name):
    return next((h["value"] for h in msg["payload"].get("headers", []) if h["name"].lower() == name), "")


def _body_text(payload) -> str:
    """Prefer text/plain; fall back to stripped HTML."""
    def walk(part):
        if part.get("parts"):
            for p in part["parts"]:
                yield from walk(p)
        else:
            yield part
    parts = list(walk(payload))
    for mime in ("text/plain", "text/html"):
        for p in parts:
            data = p.get("body", {}).get("data")
            if p.get("mimeType") == mime and data:
                text = base64.urlsafe_b64decode(data).decode("utf-8", "replace")
                if mime == "text/html":
                    text = re.sub(r"<(style|script).*?</\1>", " ", text, flags=re.S | re.I)
                    text = re.sub(r"<[^>]+>", " ", text)
                return re.sub(r"\s+", " ", text).strip()
    return ""


@tool("List unread emails in the Gmail inbox (sender, subject, snippet). Use it to summarize new mail.",
      {"max_results": {"type": "integer", "description": "default 5"},
       "query": {"type": "string", "description": "Optional Gmail search, e.g. 'from:amazon' or 'newer_than:1d'"}},
      required=[], enabled=google_enabled)
def gmail_unread(max_results: int = 5, query: str = "", ctx=None):
    gm = _service(ctx, "gmail").users().messages()
    q = "is:unread in:inbox " + (query or "")
    ids = gm.list(userId="me", q=q.strip(), maxResults=max_results).execute().get("messages", [])
    if not ids:
        return "No unread emails."
    lines = []
    for m in ids:
        msg = gm.get(userId="me", id=m["id"], format="metadata", metadataHeaders=["From", "Subject"]).execute()
        sender = parseaddr(_header(msg, "from"))[0] or _header(msg, "from")
        lines.append(f"[{m['id']}] From {sender}: {_header(msg, 'subject')} — {msg.get('snippet', '')[:160]}")
    return "\n".join(lines)


@tool("Read the full text of one email by its id (from gmail_unread).",
      {"message_id": {"type": "string"}}, enabled=google_enabled)
def gmail_read(message_id: str, ctx=None):
    msg = _service(ctx, "gmail").users().messages().get(userId="me", id=message_id, format="full").execute()
    body = _body_text(msg["payload"]) or msg.get("snippet", "")
    return (f"From: {_header(msg, 'from')}\nSubject: {_header(msg, 'subject')}\n"
            f"Date: {_header(msg, 'date')}\n\n{body[:3000]}")


@tool("Find someone's email address from past Gmail conversations.",
      {"name": {"type": "string", "description": "Person's name as the user said it"}}, enabled=google_enabled)
def gmail_find_address(name: str, ctx=None):
    gm = _service(ctx, "gmail").users().messages()
    ids = gm.list(userId="me", q=f'from:"{name}" OR to:"{name}"', maxResults=10).execute().get("messages", [])
    found: dict[str, str] = {}
    for m in ids:
        msg = gm.get(userId="me", id=m["id"], format="metadata", metadataHeaders=["From", "To"]).execute()
        for field in ("from", "to"):
            for part in _header(msg, field).split(","):
                disp, addr = parseaddr(part)
                if addr and (name.lower() in disp.lower() or name.lower() in addr.lower()):
                    found[addr.lower()] = disp or addr
    if not found:
        return f"No address found for {name}."
    return "\n".join(f"{d} <{a}>" for a, d in list(found.items())[:5])


def _mime(to: str, subject: str, body: str) -> dict:
    msg = MIMEText(body)
    msg["to"], msg["subject"] = to, subject
    return {"raw": base64.urlsafe_b64encode(msg.as_bytes()).decode()}


def _check_address(to: str):
    if not re.fullmatch(r"[^@\s,]+@[^@\s,]+\.[^@\s,]+", to.strip()):
        raise ValueError(f"'{to}' is not an email address; use gmail_find_address first")


@tool("Save an email as a Gmail draft (safe: nothing is sent).",
      {"to": {"type": "string", "description": "Email address"}, "subject": {"type": "string"},
       "body": {"type": "string"}}, enabled=google_enabled)
def gmail_draft(to: str, subject: str, body: str, ctx=None):
    _check_address(to)
    _service(ctx, "gmail").users().drafts().create(userId="me", body={"message": _mime(to, subject, body)}).execute()
    return f"Draft to {to} saved."


@tool("Send an email from the user's Gmail. Always tell the user who it goes to and what it says first.",
      {"to": {"type": "string", "description": "Email address"}, "subject": {"type": "string"},
       "body": {"type": "string"}},
      dangerous=True, enabled=google_enabled,
      confirm=lambda a: f"Send an email to {a.get('to')}, subject {a.get('subject')}, saying: {a.get('body')}. Shall I send it?")
def gmail_send(to: str, subject: str, body: str, ctx=None):
    _check_address(to)
    _service(ctx, "gmail").users().messages().send(userId="me", body=_mime(to, subject, body)).execute()
    return f"Email sent to {to}."
