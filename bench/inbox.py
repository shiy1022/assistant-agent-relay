"""Reads the events each channel records, one JSON line per event.

Every channel writes the same event format (see reader/main.swift): "new" and
"update" events carry a whole message, keyed by guid; Telegram also writes
"deleted" and "typing" events.

Slack conversations come in threads (the dot answers each message in a thread
under it), so a Slack message's thread is its own ID if it starts one, or the
`reply_to` of the thread it's in.
"""

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from bench.config import INBOX_DIR, PRIVATE_DIR


def inbox_path(channel: str) -> Path:
    if channel == "imessage":
        return INBOX_DIR / "messages.jsonl"  # where bench-reader writes
    if channel == "whatsapp":
        # bench-whatsapp runs in the background, where ~/Documents may be off limits.
        return PRIVATE_DIR / "whatsapp" / "inbox" / "messages.jsonl"
    return INBOX_DIR / channel / "messages.jsonl"


@dataclass
class Inbox:
    """Follows one channel's messages.jsonl.

    A message can be written more than once (an update carries the whole
    message again after an edit, a receipt or a finished download), so
    `messages` keeps the latest event per guid.
    """

    path: Path = INBOX_DIR / "messages.jsonl"
    offset: int = 0
    messages: dict[str, dict] = field(default_factory=dict)

    def poll(self) -> list[dict]:
        """Events written since the last poll. A half-written last line waits for the next poll."""
        try:
            with self.path.open("rb") as f:
                if f.seek(0, 2) < self.offset:  # the file was replaced; start over
                    self.offset = 0
                f.seek(self.offset)
                chunk = f.read()
        except FileNotFoundError:
            return []
        end = chunk.rfind(b"\n") + 1
        self.offset += end
        events = [json.loads(line) for line in chunk[:end].splitlines() if line.strip()]
        for event in events:
            if "guid" in event:
                self.messages[event["guid"]] = event
        return events


def slack_thread(event: dict) -> str | None:
    """The Slack thread a message belongs to, by the ID of its first message. None outside Slack."""
    if event.get("channel") != "slack" or not event.get("message_id"):
        return None
    return event.get("reply_to") or event["message_id"]


def ts_key(ts: str) -> tuple[int, int]:
    seconds, _, fraction = str(ts).partition(".")
    return int(seconds), int((fraction + "000000")[:6])


class ThreadNumbers:
    """Numbers each agent's Slack threads 1, 2, … in the order they began."""

    def __init__(self, events: Iterable[dict] = ()):
        self.numbers: dict[tuple[str, str], int] = {}
        self.add(events)

    def add(self, events: Iterable[dict]) -> None:
        roots = {(e.get("agent"), root) for e in events if (root := slack_thread(e))}
        for agent, root in sorted(roots - set(self.numbers), key=lambda r: ts_key(r[1])):
            self.numbers[agent, root] = sum(a == agent for a, _ in self.numbers) + 1

    def of(self, event: dict) -> int | None:
        """The event's thread number, or None outside Slack. A thread not seen before gets the next number."""
        root = slack_thread(event)
        if root is None:
            return None
        if (event.get("agent"), root) not in self.numbers:
            self.add([event])
        return self.numbers[event.get("agent"), root]
