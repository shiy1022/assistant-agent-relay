"""Context resets: where an agent's context was wiped, or a wipe was tried.

    bench reset-log                       list every reset
    bench reset-log AGENT --how TEXT      write one down (see bench.cli for the options)

A reset mostly happens outside what the channels record: you ask the agent to
forget, send it a command like /new, or delete something on its website. So
each one is written down in data/resets.jsonl, one per line, with
`bench reset-log` or the console's Record a reset. Clearing a Telegram chat
is found without that, from the notice Telegram leaves in the chat.

Every reset shows where it happened: as a line with role "reset" in
data/conversations/<agent>.jsonl and in run transcripts (bench.conversation),
and as a marked divider in the dashboard and the console.

Each reset has these keys:

    id            like "reset:muse:20261025T215402Z", or "reset:wajo:telegram-8599638402-38" for one found
    agent, channel
    at            when it took effect (UTC); messages after it come after the reset
    approximate   true if the time isn't known exactly; `at` is then the latest it can have been
    method        "asked" (asked the agent to forget), "command" (like /new) or "by hand"
                  (done outside the chat: on the agent's website, in its settings, or on your phone)
    how           what was done, in a sentence or two
    result        what's known about whether it worked, or null
    messages      ids of the messages that did it, as in data/conversations: the request,
                  the agent's answers, your confirmation
    recorded_at   when it was written down
    source        "recorded" (written down) or "detected" (found in what a channel recorded)
"""

import json
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

from bench.config import ROOT, Agent

RESETS_FILE = ROOT / "data" / "resets.jsonl"  # data/ is never committed
METHODS = ("asked", "command", "by hand")
KEYS = ("id", "agent", "channel", "at", "approximate", "method", "how", "result", "messages", "recorded_at", "source")


class ResetError(Exception):
    pass


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse(iso_time: str) -> datetime:
    return datetime.fromisoformat(iso_time)


def load(path: Path | None = None) -> list[dict]:
    """Every reset written down, in the order written."""
    path = path or RESETS_FILE
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def add(agent: Agent, *, how: str, method: str, at: datetime | None = None, messages: Iterable[str] = (),
        result: str | None = None, approximate: bool = False, path: Path | None = None,
        now: datetime | None = None) -> dict:
    """Writes down one reset and returns it."""
    path = path or RESETS_FILE
    how, result = how.strip(), (result or "").strip() or None
    if not how:
        raise ResetError("say what was done, like: Asked it to forget everything, and it said done")
    if method not in METHODS:
        raise ResetError(f"method must be one of {', '.join(METHODS)}, not {method!r}")
    now = now or datetime.now(UTC)
    at = at or now
    taken = {r["id"] for r in load(path)}
    rid = base = f"reset:{agent.name}:{at.astimezone(UTC):%Y%m%dT%H%M%SZ}"
    n = 1
    while rid in taken:
        n += 1
        rid = f"{base}-{n}"
    reset = {"id": rid, "agent": agent.name, "channel": agent.channel, "at": iso(at), "approximate": approximate,
             "method": method, "how": how, "result": result, "messages": list(dict.fromkeys(messages)),
             "recorded_at": iso(now), "source": "recorded"}
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:  # one short line per write, so writers don't interleave
        f.write(json.dumps(reset, ensure_ascii=False) + "\n")
    return reset


def remove(reset_id: str, path: Path | None = None) -> dict:
    """Takes a reset that was written down by mistake back out. Returns it."""
    path = path or RESETS_FILE
    kept, gone = [], None
    for r in load(path):
        if r["id"] == reset_id and gone is None:
            gone = r
        else:
            kept.append(r)
    if gone is None:
        raise ResetError(f"no reset {reset_id} in {path}")
    tmp = path.with_suffix(".tmp")
    tmp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in kept))
    tmp.replace(path)
    return gone


def detected(events: Iterable[dict]) -> list[dict]:
    """Resets found in a channel's events: so far, clearing a Telegram chat, which leaves a "history_clear" notice."""
    found: dict[str, dict] = {}
    for e in events:
        if e.get("service") == "history_clear" and e.get("guid") and e.get("agent"):
            found[e["guid"]] = {
                "id": f"reset:{e['agent']}:{e['guid'].replace(':', '-')}", "agent": e["agent"], "channel": e.get("channel"),
                "at": e.get("date") or e.get("emitted_at"), "approximate": False, "method": "by hand",
                "how": "The chat history was cleared in Telegram. That clears your side; the agent may keep its own copy.",
                "result": None, "messages": [e["guid"]], "recorded_at": e.get("emitted_at"), "source": "detected"}
    return list(found.values())


def by_agent(events: Iterable[dict] = (), path: Path | None = None) -> dict[str, list[dict]]:
    """Every reset, written down or found in `events`, by agent and in time order.
    A reset found in the events is left out if one written down already names its message."""
    recorded = load(path)
    named = {m for r in recorded for m in r.get("messages") or []}
    out: dict[str, list[dict]] = {}
    for r in recorded + [d for d in detected(events) if not named & set(d["messages"])]:
        out.setdefault(r["agent"], []).append({k: r.get(k) for k in KEYS})
    for resets in out.values():
        resets.sort(key=lambda r: parse(r["at"]))
    return out
