"""Builds the dashboard: one static HTML page of what's been recorded with each agent.

It reads the channel inboxes as they are and never connects to Telegram or
WhatsApp, so it shows only what was already recorded. Telegram messages that
arrived while nothing was running show up after `bench sync`. Context resets
(bench.resets) show where they took effect.
"""

import json
import os
import statistics
from datetime import UTC, datetime, timedelta
from pathlib import Path

from bench import resets
from bench.config import CHANNELS, ROOT, Agent, load_agents
from bench.conversation import unified_text
from bench.inbox import Inbox, inbox_path

OUT = ROOT / "data" / "dashboard.html"  # data/ is never committed
RUNS_DIR = ROOT / "runs"
TEMPLATE = Path(__file__).with_name("dashboard.html")
TYPING_GAP_SECONDS = 15  # typing events closer together than this are one stretch of typing
OFF_TASK_SECONDS = 30 * 60  # an agent's messages this soon after a message you sent outside a run are replies to it

# Message fields the page shows as recorded.
KEPT = ("date_delivered", "date_read", "date_played", "date_edited", "date_deleted", "date_retracted",
        "read", "rich", "rich_media", "reactions", "buttons", "link_preview", "service", "card")


def parse(iso: str) -> datetime:
    return datetime.fromisoformat(iso)


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


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
            "guid": guid,
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
    """Where each run began, from runs/…/run.json: its started_at, or the time its folder is named after (local time).
    Each carries the ids of the messages the run recorded, and when it ended, so the page can tell task traffic apart."""
    items = []
    for path in sorted(runs_dir.glob("*/run.json")):
        try:
            info = json.loads(path.read_text())
            started = parse(info["started_at"]) if info.get("started_at") else \
                datetime.strptime(path.parent.name[:15], "%Y%m%d-%H%M%S").astimezone()
        except (ValueError, OSError):
            continue
        ids = {str(m) for m in (info.get("messages") or []) if m is not None}
        if info.get("prompt_message"):
            ids.add(str(info["prompt_message"]))
        items.append({"type": "run", "agent": info.get("agent"), "date": iso(started), "name": path.parent.name,
                      "ended": info.get("ended_at"), "message_ids": sorted(ids),
                      **{k: info[k] for k in ("task", "step", "cell") if info.get(k)}})
    return items


def mark_benchmark(messages: list[dict], runs: list[dict]) -> None:
    """Sets `benchmark` on each message: whether it belongs to the benchmark rather than to something else you
    did with the agent. A message a run recorded, or sent while a run was going, is benchmark traffic. A message
    you sent outside any run is not, and neither are the agent's messages in the half hour after it (its replies).
    What the agent sends on its own, with nothing from you before it, is benchmark traffic: that's proactivity."""
    in_runs = {i for r in runs for i in r.get("message_ids", [])}
    windows = []
    for r in runs:
        try:
            windows.append((parse(r["date"]), parse(r["ended"]) if r.get("ended") else None))
        except (ValueError, TypeError):
            pass

    def during_run(at: datetime) -> bool:
        # A channel may stamp the prompt a moment before the run wrote its start (whole seconds vs milliseconds).
        grace = timedelta(seconds=5)
        return any(start - grace <= at and (end is None or at <= end) for start, end in windows)

    off_task_until = None
    for m in sorted(messages, key=lambda m: m["date"]):
        at = parse(m["date"])
        # A run lists messages by their conversation id (`whatsapp:muse:…`), which is the item's guid; the
        # item's id is the channel's own (bare) id. Either may match.
        if str(m.get("id")) in in_runs or str(m.get("guid")) in in_runs or during_run(at):
            m["benchmark"] = True
            off_task_until = None
        elif m["from_me"]:
            m["benchmark"] = False
            off_task_until = at + timedelta(seconds=OFF_TASK_SECONDS)
        elif off_task_until and at <= off_task_until:
            m["benchmark"] = False
            off_task_until = at + timedelta(seconds=OFF_TASK_SECONDS)  # a reply keeps the exchange going
        else:
            m["benchmark"] = True


def reset_items(agent_resets: list[dict], messages: list[dict]) -> list[dict]:
    """Each reset (bench.resets) where it took effect, and on each message that did it, which reset it was part of."""
    by_guid = {m["guid"]: m for m in messages}
    items = []
    for r in agent_resets:
        for guid in r["messages"] or []:
            if guid in by_guid:
                by_guid[guid]["reset"] = r["id"]
        items.append({"type": "reset", "date": r["at"]} | {k: r[k] for k in resets.KEYS if k not in ("at", "agent", "channel")})
    return items


def timeline(messages: list[dict], typing: list[dict], runs: list[dict], agent_resets: list[dict] = ()) -> list[dict]:
    """Everything in order, with the reply time on each first reply after one of your messages."""
    order = {"run": 0, "message": 1, "typing": 1, "reset": 2}  # a reset goes after the messages of its second
    items = sorted(messages + typing + runs + reset_items(list(agent_resets), messages),
                   key=lambda i: (parse(i["date"]), order[i["type"]], i.get("recorded") or "", str(i.get("id") or "").rjust(32)))
    by_id = {m["id"]: m for m in messages if m.get("id") is not None}
    previous = None
    for item in items:
        if item["type"] != "message" or item.get("service"):  # a notice Telegram left isn't a message to answer
            continue
        if not item["from_me"] and previous and previous["from_me"]:
            item["reply_seconds"] = max(0.0, (parse(item["date"]) - parse(previous["date"])).total_seconds())
        if (target := by_id.get(item.get("reply_to"))) and target.get("text"):
            item["reply_to_text"] = target["text"][:140]
        previous = item
    return items


def by_thread(items: list[dict]) -> list[dict]:
    """Slack: each thread's messages together, under a header, with threads in the order they began.

    The dot answers each message in a thread under it, so a reply can arrive
    after a newer thread has started; it still goes under its own thread. Reply
    times are measured within each thread, and runs stay where they began.
    """
    threads: dict[str, list[dict]] = {}
    for item in items:
        if item["type"] == "message":
            threads.setdefault(item.get("reply_to") or item["id"], []).append(item)
    others = [i for i in items if i["type"] != "message"]
    out = []
    for number, (root, messages) in enumerate(sorted(threads.items(), key=lambda t: parse(t[1][0]["date"])), 1):
        start = messages[0]["date"]
        while others and parse(others[0]["date"]) <= parse(start):
            out.append(others.pop(0))
        previous = None
        for m in messages:
            m.pop("reply_to_text", None)  # the thread's header says what it replies to
            m.pop("reply_seconds", None)
            m["thread"] = number
            if not m["from_me"] and previous and previous["from_me"]:
                m["reply_seconds"] = max(0.0, (parse(m["date"]) - parse(previous["date"])).total_seconds())
            previous = m
        first = next((m for m in messages if m["id"] == root), None)
        out.append({"type": "thread", "number": number, "id": root, "date": start, "until": messages[-1]["date"],
                    "title": ((first or {}).get("text") or "").strip()[:120] or None,
                    "from_me": bool(first and first["from_me"]), "messages": len(messages),
                    "first_reply_seconds": next((m["reply_seconds"] for m in messages if "reply_seconds" in m), None)})
        out += messages
    return out + others


def stats(items: list[dict]) -> dict:
    messages = [i for i in items if i["type"] == "message" and not i.get("service")]
    replies = [i["reply_seconds"] for i in messages if "reply_seconds" in i]
    reset_dates = [i["date"] for i in items if i["type"] == "reset"]
    return {
        "sent": sum(m["from_me"] for m in messages),
        "received": sum(not m["from_me"] for m in messages),
        "first": min((m["date"] for m in messages), key=parse, default=None),  # Slack's are grouped by thread, not by time
        "last": max((m["date"] for m in messages), key=parse, default=None),
        "median_reply_seconds": statistics.median(replies) if replies else None,
        "resets": len(reset_dates),
        "last_reset": max(reset_dates, key=parse, default=None),
    }


def display_path(path: Path) -> str:
    try:
        return "~/" + str(path.relative_to(Path.home()))
    except ValueError:
        return str(path)


def snapshot(agents: dict[str, Agent] | None = None, inboxes: dict[str, Path] | None = None,
             runs_dir: Path = RUNS_DIR, now: datetime | None = None, resets_file: Path | None = None) -> dict:
    """Every agent's recorded conversation, as the page's data."""
    agents = load_agents() if agents is None else agents
    inboxes = inboxes or {c: inbox_path(c) for c in CHANNELS}
    events = {channel: Inbox(path).poll() for channel, path in inboxes.items()}
    runs = run_items(runs_dir)
    all_resets = resets.by_agent([e for evs in events.values() for e in evs], resets_file)

    # Agents in config order, then any that were recorded but aren't listed any more.
    seen = {(e["agent"], channel) for channel, evs in events.items() for e in evs if e.get("agent")}
    seen |= {(r["agent"], r["channel"]) for rs in all_resets.values() for r in rs}
    entries = {(a.name, a.channel): a.handle for a in agents.values()}
    for name, channel in sorted(seen - set(entries)):
        handle = next((e.get("handle") for e in events.get(channel, []) if e.get("agent") == name and e.get("handle")), "")
        entries[name, channel] = handle

    out = []
    for (name, channel), handle in entries.items():
        if channel not in inboxes:
            continue
        mine = [e for e in events[channel] if e.get("agent") == name]
        messages, agent_runs = message_items(mine, inboxes[channel]), [r for r in runs if r["agent"] == name]
        mark_benchmark(messages, agent_runs)
        items = timeline(messages, typing_items(mine), agent_runs,
                         [r for r in all_resets.get(name, []) if r["channel"] == channel])
        if channel == "slack":
            items = by_thread(items)
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
