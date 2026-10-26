"""Run folders: one task prompt sent to one agent, and everything it answered.

    bench run AGENT --file PROMPT --task NAME   sends the prompt, records until the reply is finished, writes the folder
    bench run-rebuild RUN_DIR                   writes the transcript again from what's recorded now
    bench run-save RUN_DIR                      saves the pages the agent linked to as its answer (see bench.deliverables)

Every run is a folder runs/<YYYYmmdd-HHMMSS>-<task and step>-<agent>/ (local time) with:

    run.json          what was run, and when (below)
    prompt.txt        the prompt, exactly as sent
    transcript.jsonl  every message from the prompt to the end of the run, one per
                      line, in the same form as data/conversations (see bench.conversation),
                      with any context reset made during it
    transcript.md     the same, for people
    deliverables/     what the agent made outside the chat, like a page it linked to;
                      bench run saves those itself (bench.deliverables)
    raw/              anything else captured along the way (screenshots, page data);
                      the transcript is what to read

run.json always has these keys; anything else in it (notes, outcome, what the
grader needs) is kept when the folder is rebuilt:

    schema          1
    run             the folder's name
    task, step, cell  which task this is, as in the task sheet
    agent, channel, handle
    started_at      when the run began, just before sending (UTC)
    ended_at        when it stopped recording; null while it's still going
    finished        why it stopped, like "quiet for 180s"; null while it's still going
    prompt_message  the id of the prompt in the transcript
    messages        the ids in transcript.jsonl, in order
    links           every URL in the agent's messages
    deliverables    [{path, source}]: each file in deliverables/, and where it came from;
                    a page bench run saved also has saved_at
    timing          prompt_sent, first_reply, last_change and the seconds between them,
                    agent_messages, streamed_edits, reply_chars
    context         what the agent had been through before the prompt:
                      last_reset            the latest context reset before it (bench.resets):
                                            id, at, how, method, result, approximate, source;
                                            null if none was recorded
                      messages_since_reset  messages with the agent between that reset and the
                                            prompt (all of them before it, if there was no reset)
                      runs_since_reset      the agent's other runs that started in between
"""

import json
import re
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from bench import conversation
from bench.config import ROOT, Agent, load_agents

RUNS_DIR = ROOT / "runs"  # never committed
SCHEMA = 1
SLICE_SECONDS = 10      # how long bench run records between checks for a finished reply
CLOCK_SKEW = timedelta(seconds=120)  # Telegram and WhatsApp round send times down to the second
STANDARD = ("schema", "run", "task", "step", "cell", "agent", "channel", "handle", "started_at", "ended_at",
            "finished", "prompt_message", "messages", "links", "deliverables", "timing", "context")


class RunError(Exception):
    pass


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "run"


def read_info(run_dir: Path) -> dict:
    path = run_dir / "run.json"
    if not path.exists():
        raise RunError(f"{run_dir} has no run.json")
    return json.loads(path.read_text())


def write_info(run_dir: Path, info: dict) -> None:
    ordered = {k: info.get(k) for k in STANDARD} | {k: v for k, v in info.items() if k not in STANDARD}
    (run_dir / "run.json").write_text(json.dumps(ordered, indent=2, ensure_ascii=False) + "\n")


def start(agent: Agent, prompt: str, *, task: str, step: str | None = None, cell: str | None = None,
          runs_dir: Path | None = None, now: datetime | None = None) -> Path:
    """Makes the run's folder in runs/, with the prompt and a run.json that says it's still going."""
    now = now or datetime.now(UTC)
    run_dir = (runs_dir or RUNS_DIR) / f"{now.astimezone():%Y%m%d-%H%M%S}-{slug(' '.join(filter(None, [task, step])))}-{agent.name}"
    run_dir.mkdir(parents=True)
    (run_dir / "prompt.txt").write_text(prompt)
    write_info(run_dir, {"schema": SCHEMA, "run": run_dir.name, "task": task, "step": step, "cell": cell,
                         "agent": agent.name, "channel": agent.channel, "handle": agent.handle,
                         "started_at": iso(now), "messages": [], "links": [], "deliverables": [], "timing": {}})
    return run_dir


class Watch:
    """Follows one agent's events during a run, and says when its reply is finished:
    once it has answered and then gone quiet for `quiet` seconds, or after `max_wait`.

    Anything from the agent counts as activity: a message, an edit (Muse streams
    by editing), or typing (Hark "working" in its browser).
    """

    listening = True  # keeps recording after sending (see bench.cli.converse)

    def __init__(self, agent: Agent, *, quiet: float | None = None, max_wait: float | None = None,
                 clock: Callable[[], float] | None = None):
        self.agent, self.clock = agent, clock or time.monotonic
        self.quiet = agent.quiet_period_seconds if quiet is None else quiet
        self.max_wait = agent.max_wait_seconds if max_wait is None else max_wait
        self.started = self.clock()
        self.replied = False
        self.last_activity = self.started

    def see(self, event: dict) -> None:
        if event.get("agent") != self.agent.name or (event.get("kind") != "typing" and event.get("is_from_me")):
            return
        self.last_activity = self.clock()
        self.replied = self.replied or event.get("kind") in ("new", "update")

    def finished(self) -> str | None:
        now = self.clock()
        if self.replied and now - self.last_activity >= self.quiet:
            return f"quiet for {self.quiet:.0f}s"
        if now - self.started >= self.max_wait:
            return f"stopped waiting after {self.max_wait:.0f}s" + ("" if self.replied else " with no reply")
        return None

    def next(self) -> float:
        """How long to record before checking again; 0 once the reply is finished."""
        if self.finished():
            return 0
        left = self.started + self.max_wait - self.clock()
        if self.replied:
            left = min(left, self.last_activity + self.quiet - self.clock())
        return max(1.0, min(SLICE_SECONDS, left))


def find_prompt(messages: list[dict], prompt: str, started_at: str | None) -> dict | None:
    """The prompt as sent: the first message of yours with its text from the run's start on,
    or else the first message of yours from then on (a channel may reformat what it sends)."""
    since = conversation.parse(started_at) - CLOCK_SKEW if started_at else None
    mine = [m for m in messages if m["role"] == "user" and (since is None or conversation.parse(m["sent_at"]) >= since)]
    return next((m for m in mine if m["text"].strip() == prompt.strip()), mine[0] if mine else None)


def timing(prompt: dict, replies: list[dict]) -> dict:
    sent = conversation.parse(prompt["sent_at"])
    out = {"prompt_sent": prompt["sent_at"], "first_reply": None, "last_change": None,
           "seconds_to_first_reply": None, "seconds_to_finished": None, "agent_messages": len(replies),
           "streamed_edits": sum(m["versions"] - 1 for m in replies if m["versions"] > 1),
           "reply_chars": sum(len(m["text"]) for m in replies)}
    if replies:
        last = max((t for m in replies for t in (m["sent_at"], m["edited_at"]) if t), key=conversation.parse)
        out |= {"first_reply": replies[0]["sent_at"], "last_change": last,
                "seconds_to_first_reply": round((conversation.parse(replies[0]["sent_at"]) - sent).total_seconds(), 3),
                "seconds_to_finished": round((conversation.parse(last) - sent).total_seconds(), 3)}
    return out


def context(info: dict, everything: list[dict], prompt: dict, runs_dir: Path) -> dict:
    """What the agent had been through before the run's prompt: its last reset, and what came after it."""
    before = everything[:everything.index(prompt)]
    reset = next((m for m in reversed(before) if m["role"] == "reset"), None)
    since = before[before.index(reset) + 1:] if reset else before
    started = conversation.parse(info["started_at"])
    after = conversation.parse(reset["sent_at"]) if reset else None
    runs_between = 0
    for path in runs_dir.glob("*/run.json"):
        try:
            other = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if path.parent.name == info["run"] or other.get("agent") != info["agent"] or not other.get("started_at"):
            continue
        if conversation.parse(other["started_at"]) < started and (after is None or conversation.parse(other["started_at"]) > after):
            runs_between += 1
    last = None
    if reset:
        last = {"id": reset["id"], "at": reset["sent_at"], "how": reset["text"]} | \
            {k: reset["reset"][k] for k in ("method", "result", "approximate", "source")}
    return {"last_reset": last, "messages_since_reset": sum(m["role"] != "reset" for m in since),
            "runs_since_reset": runs_between}


def local(iso_time: str | None) -> str:
    return conversation.parse(iso_time).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z") if iso_time else "--"


def markdown(info: dict, messages: list[dict]) -> str:
    title = " · ".join(filter(None, [info.get("task"), info.get("step"), f"{info['agent']} ({info['channel']})"]))
    lines = [f"# {title}", "",
             f"Run {info['run']}. Started {local(info.get('started_at'))}; "
             + (f"ended {local(info['ended_at'])} ({info['finished']})." if info.get("ended_at") else "still going."),
             ""]
    if (ctx := info.get("context")) is not None:
        r = ctx["last_reset"]
        lines += [(f"Last context reset before it: {'by ' if r['approximate'] else ''}{local(r['at'])}, {r['how']} "
                   f"Then {ctx['messages_since_reset']} messages and {ctx['runs_since_reset']} runs before this one."
                   if r else f"No context reset recorded before it, and {ctx['messages_since_reset']} messages before it."), ""]
    for m in messages:
        if m["role"] == "reset":  # see bench.resets
            r = m["reset"]
            lines += [f"## Context reset ({r['method']}) · {'by ' if r['approximate'] else ''}{local(m['sent_at'])}", "",
                      m["text"], ""] + ([f"Result: {r['result']}", ""] if r["result"] else [])
            continue
        who = "You" if m["role"] == "user" else info["agent"]
        head = [f"sent {local(m['sent_at'])}"]
        if m["edited_at"]:
            head.append(f"last edited {local(m['edited_at'])}")
        if m["versions"] > 1:
            head.append(f"{m['versions']} versions")
        if m["deleted_at"]:
            head.append(f"deleted {local(m['deleted_at'])}")
        lines += [f"## {who} · {', '.join(head)}", "", m["text"] or "*(no text)*", ""]
        extras = [f"- attachment: {a['name']}" + (f" ({a['path']})" if a["path"] else "") for a in m["attachments"]]
        extras += [f"- buttons: {' | '.join(b['text'] for b in row)}" for row in m["buttons"]]
        extras += [f"- reactions: {' '.join(r['emoji'] for r in m['reactions'])}"] if m["reactions"] else []
        if extras:
            lines += [*extras, ""]
    return "\n".join(lines)


def rebuild(run_dir: Path, conversations: dict[str, list[dict]] | None = None) -> dict:
    """Writes transcript.jsonl, transcript.md and run.json from what's recorded now. Returns run.json."""
    info = read_info(run_dir)
    agent = load_agents().get(info.get("agent"))
    if agent:
        info |= {"channel": agent.channel, "handle": agent.handle}
    prompt_path = run_dir / "prompt.txt"
    if not prompt_path.exists():
        raise RunError(f"{run_dir} has no prompt.txt")
    everything = (conversations if conversations is not None else conversation.load()).get(info["agent"], [])
    prompt = next((m for m in everything if m["id"] == info.get("prompt_message")), None) \
        or find_prompt(everything, prompt_path.read_text(), info.get("started_at"))
    if prompt is None:
        raise RunError(f"the prompt of {run_dir.name} isn't among {info['agent']}'s recorded messages. "
                       f"Record them first: uv run bench sync {info['agent']}")
    start_at = conversation.parse(prompt["sent_at"])
    end_at = conversation.parse(info["ended_at"]) if info.get("ended_at") else None
    messages = [m for m in everything
                if conversation.parse(m["sent_at"]) >= start_at and (end_at is None or conversation.parse(m["sent_at"]) <= end_at)]
    messages = [prompt] + [m for m in messages if m["id"] != prompt["id"]]
    replies = [m for m in messages if m["role"] == "agent"]

    known = {d.get("path"): d for d in info.get("deliverables") or []}
    files = sorted(p.relative_to(run_dir).as_posix() for p in (run_dir / "deliverables").rglob("*") if p.is_file()) \
        if (run_dir / "deliverables").is_dir() else []
    info |= {
        "schema": SCHEMA,
        "run": run_dir.name,
        "started_at": info.get("started_at") or prompt["sent_at"],
    }
    info |= {
        "prompt_message": prompt["id"],
        "messages": [m["id"] for m in messages],
        "links": list(dict.fromkeys(link for m in replies for link in m["links"])),
        "deliverables": [{"path": f, "source": None} | (known.get(f) or {}) for f in files if not f.endswith(".DS_Store")],
        "timing": (info.get("timing") or {}) | timing(prompt, replies),
        "context": context(info, everything, prompt, run_dir.parent),
    }
    conversation.write_jsonl(run_dir / "transcript.jsonl", messages)
    (run_dir / "transcript.md").write_text(markdown(info, messages))
    write_info(run_dir, info)
    return info


def end(run_dir: Path, reason: str, *, now: datetime | None = None) -> None:
    """Marks the run as ended now, for `reason`."""
    write_info(run_dir, read_info(run_dir) | {"ended_at": iso(now or datetime.now(UTC)), "finished": reason})


def finish(run_dir: Path, reason: str, *, now: datetime | None = None, **kw) -> dict:
    """Marks the run as ended now, for `reason`, and writes its transcript."""
    end(run_dir, reason, now=now)
    return rebuild(run_dir, **kw)
