"""Reads the events each channel records, one JSON line per event.

Every channel writes the same event format (see reader/main.swift): "new" and
"update" events carry a whole message, keyed by guid; Telegram also writes
"deleted" and "typing" events.
"""

import json
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
