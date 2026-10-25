"""Each agent's conversation as plain messages, the same way for every channel.

The channel inboxes are event logs: a message is written again on every edit,
receipt or reaction, Muse's streamed replies come as dozens of versions, the
lines aren't in the order the messages were sent, and the fields differ by
channel. This turns them into one message per line, as last recorded, in the
order sent, with the same fields for every channel:

    bench export          writes data/conversations/<agent>.jsonl for every agent

Every line has all of these keys (null or [] when they don't apply):

    id            unique across channels, like "telegram:8599638402:12"
    agent         the agent's name in config/agents.yaml
    channel       telegram, whatsapp, slack, hark or imessage
    role          "user" (sent by you) or "agent"
    text          the message's text, "" if it has none (see attachments and buttons)
    sent_at       when it was sent, ISO 8601 in UTC
    edited_at     when it was last edited
    deleted_at    when it was deleted or unsent; the text is what it said before
    versions      how many different texts it had (a streamed reply has many)
    reply_to      the id of the message it answers or quotes
    thread        Slack only: the id of the thread's first message
    attachments   [{name, mime_type, path}]: path is absolute, if it was downloaded
    buttons       rows of [{text, kind, url}] the agent offered; never pressed
    links         every URL in the message, its link preview and its buttons
    reactions     [{emoji, count or by}]
    delivered_at  when it reached the other side, where the channel says
    read_at       when it was read, where the channel says
    rich          true if it had content beyond text (cards, pages, rich layouts)
    recorded_at   when agent-runner first recorded it
"""

import base64
import binascii
import json
import os
import re
from datetime import datetime
from pathlib import Path

from bench.config import CHANNELS, ROOT
from bench.inbox import Inbox, inbox_path

OUT_DIR = ROOT / "data" / "conversations"  # data/ is never committed
URL = re.compile(r"https?://[^\s<>()\"'`*|]+[^\s<>()\"'`*|.,;:!?]")


def parse(iso: str) -> datetime:
    return datetime.fromisoformat(iso)


def unified_text(raw: dict | None) -> str:
    """The text of a Meta AI rich response, for Muse messages recorded before
    bench-whatsapp decoded them itself (see unifiedText in whatsapp/content.go)."""
    data = (((raw or {}).get("richResponseMessage") or {}).get("unifiedResponse") or {}).get("data")
    if not data:
        return ""
    try:
        doc = json.loads(base64.b64decode(data))
    except (binascii.Error, ValueError):
        return ""
    texts: list[str] = []

    def walk(node):
        if isinstance(node, dict):
            text, typename = node.get("text"), node.get("__typename", "")
            if isinstance(text, str) and (not typename or "Text" in typename):
                texts.append(text)
            for key, value in node.items():
                if key != "text":
                    walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(doc)
    return "\n".join(t for t in texts if t)


def event_text(event: dict) -> str:
    return event.get("text") or unified_text(event.get("raw")) or ""


def sibling(guid: str, message_id) -> str:
    """The id of another message in the same chat: guids end in the message's own ID."""
    return f"{guid.rsplit(':', 1)[0]}:{message_id}"


def links(text: str, event: dict) -> list[str]:
    found = URL.findall(text)
    if (event.get("link_preview") or {}).get("url"):
        found.append(event["link_preview"]["url"])
    found += [b["url"] for row in event.get("buttons") or [] for b in row if b.get("url")]
    return list(dict.fromkeys(found))


def message(event: dict, versions: int, recorded_at: str, inbox_dir: Path) -> dict:
    """One message, from the last event recorded for it."""
    guid = event["guid"]
    text = event_text(event)
    if event.get("channel") == "hark":
        reply_to = event.get("triggered_by")
    else:
        reply_to = event.get("reply_to")
    thread = None
    if event.get("channel") == "slack":
        thread = sibling(guid, reply_to or event["message_id"])
    return {
        "id": guid,
        "agent": event.get("agent"),
        "channel": event.get("channel"),
        "role": "user" if event.get("is_from_me") else "agent",
        "text": text,
        "sent_at": event.get("date") or recorded_at,
        "edited_at": event.get("date_edited"),
        "deleted_at": event.get("date_deleted") or event.get("date_retracted"),
        "versions": versions,
        "reply_to": sibling(guid, reply_to) if reply_to else None,
        "thread": thread,
        "attachments": [
            {"name": a.get("name") or (Path(a["path"]).name if a.get("path") else None),
             "mime_type": a.get("mime_type"),
             "path": str(inbox_dir / a["path"]) if a.get("path") else None}
            for a in event.get("attachments") or []
        ],
        "buttons": event.get("buttons") or [],
        "links": links(text, event),
        "reactions": event.get("reactions") or [],
        "delivered_at": event.get("date_delivered"),
        "read_at": event.get("date_read"),
        "rich": bool(event.get("rich") or event.get("raw")),
        "recorded_at": recorded_at,
    }


def order_key(m: dict) -> tuple:
    # Telegram and WhatsApp times are to the second; Telegram's IDs count up, so they break ties.
    message_id = m["id"].rsplit(":", 1)[-1]
    return parse(m["sent_at"]), parse(m["recorded_at"]), int(message_id) if message_id.isdigit() else 0, m["id"]


def messages(events: list[dict], inbox_dir: Path) -> list[dict]:
    """Every message in `events`, once each as last recorded, in the order they were sent."""
    latest: dict[str, dict] = {}
    recorded: dict[str, str] = {}
    texts: dict[str, list[str]] = {}
    for event in events:
        guid = event.get("guid")
        if not guid or event.get("kind") not in ("new", "update", "deleted"):
            continue
        latest[guid] = event
        recorded.setdefault(guid, event.get("emitted_at"))
        seen = texts.setdefault(guid, [])
        if (text := event_text(event)) and (not seen or seen[-1] != text):
            seen.append(text)
    out = [message(e, len(texts[g]), recorded[g], inbox_dir) for g, e in latest.items()]
    return sorted(out, key=order_key)


def load(inboxes: dict[str, Path] | None = None) -> dict[str, list[dict]]:
    """Each agent's messages, by agent name, from every channel's inbox."""
    inboxes = inboxes or {c: inbox_path(c) for c in CHANNELS}
    out: dict[str, list[dict]] = {}
    for path in inboxes.values():
        events = Inbox(path).poll()
        for name in {e["agent"] for e in events if e.get("agent")}:
            out.setdefault(name, []).extend(messages([e for e in events if e.get("agent") == name], path.parent))
    return {name: sorted(ms, key=order_key) for name, ms in sorted(out.items()) if ms}


def write_jsonl(path: Path, rows: list[dict]) -> Path:
    """Writes the whole file at once, so a reader never sees half of it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
    tmp.replace(path)
    return path


def export(out_dir: Path = OUT_DIR, **kw) -> list[Path]:
    """Writes out_dir/<agent>.jsonl for every agent with recorded messages. Returns the files written."""
    return [write_jsonl(out_dir / f"{name}.jsonl", ms) for name, ms in load(**kw).items()]
