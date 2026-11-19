"""Follow events written by the local bench-reader."""

import time
from datetime import datetime

from bench.config import Agent
from bench.inbox import Inbox, inbox_path


def open_inbox() -> Inbox:
    box = Inbox(inbox_path("imessage"))
    box.poll()
    return box


def events(box: Inbox, agent: Agent, since: datetime, seconds: float | None):
    """Yield events for this exact contact and ignore delayed records for older messages."""
    deadline = None if seconds is None else time.monotonic() + seconds
    while True:
        for event in box.poll():
            date = event.get("date")
            if (event.get("agent") == agent.name and event.get("handle") == agent.handle
                    and event.get("service") == "iMessage" and date
                    and datetime.fromisoformat(date) >= since):
                yield event
        remaining = 0.5 if deadline is None else deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(remaining, 0.5))
