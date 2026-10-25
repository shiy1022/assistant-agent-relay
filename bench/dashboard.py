"""Builds the dashboard: one static HTML page of what's been recorded with each agent.

It reads the channel inboxes as they are and never connects to Telegram or
WhatsApp, so it shows only what was already recorded. Telegram messages that
arrived while nothing was running show up after `bench sync`.
"""

import base64
import binascii
import json
import os
import statistics
from datetime import UTC, datetime
from pathlib import Path

from bench.config import CHANNELS, ROOT, Agent, load_agents
from bench.inbox import Inbox, inbox_path

OUT = ROOT / "data" / "dashboard.html"  # data/ is never committed
RUNS_DIR = ROOT / "runs"
TEMPLATE = Path(__file__).with_name("dashboard.html")
TYPING_GAP_SECONDS = 15  # typing events closer together than this are one stretch of typing

# Message fields the page shows as recorded.
KEPT = ("date_delivered", "date_read", "date_played", "date_edited", "date_deleted", "date_retracted",
        "read", "rich", "rich_media", "reactions", "buttons", "link_preview")


def parse(iso: str) -> datetime:
    return datetime.fromisoformat(iso)


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


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


def message_items(events: list[dict], inbox: Path) -> list[dict]:
    """Each message as last recorded, with how many versions of its text were seen
    (Muse streams a reply by editing one message as it grows)."""
    latest: dict[str, dict] = {}
    first_recorded: dict[str, str] = {}
    texts: dict[str, list[str]] = {}
    for event in events:
        guid = event.get("guid")
        if not guid:
            continue
        latest[guid] = event
        first_recorded.setdefault(guid, event.get("emitted_at"))
        text = event.get("text") or unified_text(event.get("raw"))
        seen = texts.setdefault(guid, [])
        if text and (not seen or seen[-1] != text):
            seen.append(text)

    items = []
    for guid, event in latest.items():
        text = event.get("text") or unified_text(event.get("raw"))
        item = {
            "type": "message",
            "id": event.get("message_id"),
            "from_me": bool(event.get("is_from_me")),
            "text": text,
            "decoded": bool(text) and not event.get("text"),  # read from the raw rich response here
            "date": event.get("date") or first_recorded[guid],
            "recorded": first_recorded[guid],
            "versions": len(texts[guid]),
            "reply_to": event.get("reply_to"),
            "attachments": [
                {"name": a.get("name") or Path(a["path"]).name, **({"url": (inbox.parent / a["path"]).as_uri()} if a.get("path") else {})}
                for a in event.get("attachments") or []
                if a.get("name") or a.get("path")
            ],
        }
        item |= {k: event[k] for k in KEPT if event.get(k)}
        items.append(item)
    return items


def typing_items(events: list[dict]) -> list[dict]:
    """Stretches of typing (or recording), each from its first event to its last."""
    items: list[dict] = []
    for event in events:
        if event.get("kind") != "typing" or not event.get("emitted_at"):
            continue
        at = parse(event["emitted_at"])
        last = items[-1] if items else None
        if last and last["action"] == event.get("action") and (at - parse(last["until"])).total_seconds() <= TYPING_GAP_SECONDS:
            last["until"] = event["emitted_at"]
        else:
            items.append({"type": "typing", "action": event.get("action", "typing"), "date": event["emitted_at"], "until": event["emitted_at"]})
    return items


def run_items(runs_dir: Path) -> list[dict]:
    """Where each run began, from runs/<YYYYmmdd-HHMMSS>-…/run.json (local time)."""
    items = []
    for path in sorted(runs_dir.glob("*/run.json")):
        try:
            started = datetime.strptime(path.parent.name[:15], "%Y%m%d-%H%M%S").astimezone()
            info = json.loads(path.read_text())
        except (ValueError, OSError):
            continue
        items.append({"type": "run", "agent": info.get("agent"), "date": iso(started), "name": path.parent.name,
                      **{k: info[k] for k in ("task", "step", "cell") if info.get(k)}})
    return items


def timeline(messages: list[dict], typing: list[dict], runs: list[dict]) -> list[dict]:
    """Everything in order, with the reply time on each first reply after one of your messages."""
    order = {"run": 0, "message": 1, "typing": 1}
    items = sorted(messages + typing + runs,
                   key=lambda i: (parse(i["date"]), order[i["type"]], i.get("recorded") or "", str(i.get("id") or "").rjust(32)))
    by_id = {m["id"]: m for m in messages if m.get("id") is not None}
    previous = None
    for item in items:
        if item["type"] != "message":
            continue
        if not item["from_me"] and previous and previous["from_me"]:
            item["reply_seconds"] = max(0.0, (parse(item["date"]) - parse(previous["date"])).total_seconds())
        if (target := by_id.get(item.get("reply_to"))) and target.get("text"):
            item["reply_to_text"] = target["text"][:140]
        previous = item
    return items


def stats(items: list[dict]) -> dict:
    messages = [i for i in items if i["type"] == "message"]
    replies = [i["reply_seconds"] for i in messages if "reply_seconds" in i]
    return {
        "sent": sum(m["from_me"] for m in messages),
        "received": sum(not m["from_me"] for m in messages),
        "first": messages[0]["date"] if messages else None,
        "last": messages[-1]["date"] if messages else None,
        "median_reply_seconds": statistics.median(replies) if replies else None,
    }


def display_path(path: Path) -> str:
    try:
        return "~/" + str(path.relative_to(Path.home()))
    except ValueError:
        return str(path)


def snapshot(agents: dict[str, Agent] | None = None, inboxes: dict[str, Path] | None = None,
             runs_dir: Path = RUNS_DIR, now: datetime | None = None) -> dict:
    """Every agent's recorded conversation, as the page's data."""
    agents = load_agents() if agents is None else agents
    inboxes = inboxes or {c: inbox_path(c) for c in CHANNELS}
    events = {channel: Inbox(path).poll() for channel, path in inboxes.items()}
    runs = run_items(runs_dir)

    # Agents in config order, then any that were recorded but aren't listed any more.
    seen = {(e["agent"], channel) for channel, evs in events.items() for e in evs if e.get("agent")}
    entries = {(a.name, a.channel): a.handle for a in agents.values()}
    for name, channel in sorted(seen - set(entries)):
        handle = next((e.get("handle") for e in events[channel] if e.get("agent") == name and e.get("handle")), "")
        entries[name, channel] = handle

    out = []
    for (name, channel), handle in entries.items():
        if channel not in inboxes:
            continue
        mine = [e for e in events[channel] if e.get("agent") == name]
        items = timeline(message_items(mine, inboxes[channel]), typing_items(mine),
                         [r for r in runs if r["agent"] == name])
        out.append({"name": name, "channel": channel, "handle": handle, "items": items, "stats": stats(items)})

    return {
        "generated_at": iso(now or datetime.now(UTC)),
        "agents": out,
        "sources": [
            {"channel": channel, "path": display_path(path), "events": len(events[channel]),
             "last": max((e["emitted_at"] for e in events[channel] if e.get("emitted_at")), default=None)}
            for channel, path in inboxes.items() if path.exists()
        ],
    }


def render(data: dict) -> str:
    # "<" escaped so no recorded text can close the <script> it sits in.
    blob = json.dumps(data, ensure_ascii=False).replace("<", "\\u003c")
    return TEMPLATE.read_text().replace("__DASHBOARD_DATA__", blob)


def write(out: Path = OUT, **kw) -> Path:
    html = render(snapshot(**kw))
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp")
    tmp.write_text(html)
    os.chmod(tmp, 0o600)  # it holds your chats
    tmp.replace(out)
    return out
