import json
import subprocess

import pytest

from bench.config import Agent
from bench.imessage import send as send_module
from bench.inbox import Inbox
from bench.imessage.send import SendError, send

AGENT = Agent("instinct", "+15550100001", 90, 900)


def test_send_passes_handle_and_text_as_arguments(monkeypatch):
    calls = []
    monkeypatch.setattr(send_module.subprocess, "run",
                        lambda cmd, **kw: calls.append(cmd) or subprocess.CompletedProcess(cmd, 0, "", ""))
    text = 'Say "hi" & end tell'
    send(AGENT, text)
    [cmd] = calls
    assert cmd[:2] == ["osascript", "-e"]
    assert cmd[3:] == [AGENT.handle, text]
    assert text not in cmd[2]


def test_send_reports_messages_errors(monkeypatch):
    monkeypatch.setattr(send_module.subprocess, "run",
                        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "", "Not authorized to send Apple events"))
    with pytest.raises(SendError, match="Not authorized"):
        send(AGENT, "hello")


def test_send_refuses_empty_text():
    with pytest.raises(SendError):
        send(AGENT, "  ")


def event(guid: str, kind: str = "new", **fields) -> str:
    return json.dumps({"guid": guid, "kind": kind, **fields}) + "\n"


def test_inbox_waits_for_complete_lines_and_keeps_latest(tmp_path):
    path = tmp_path / "messages.jsonl"
    inbox = Inbox(path)
    assert inbox.poll() == []

    path.write_text(event("A", text="hi") + event("B")[:10])
    assert [e["guid"] for e in inbox.poll()] == ["A"]

    with path.open("a") as f:
        f.write(event("B")[10:] + event("A", "update", text="hi (edited)"))
    assert [e["guid"] for e in inbox.poll()] == ["B", "A"]
    assert inbox.messages["A"]["text"] == "hi (edited)"


def test_inbox_starts_over_when_file_is_replaced(tmp_path):
    path = tmp_path / "messages.jsonl"
    path.write_text(event("A") + event("B"))
    inbox = Inbox(path)
    inbox.poll()
    path.write_text(event("C"))
    assert [e["guid"] for e in inbox.poll()] == ["C"]


def test_task_waits_for_new_reply_from_configured_contact(tmp_path, monkeypatch):
    from datetime import UTC, datetime

    from bench import cli
    from bench.imessage import inbox
    from bench.runs import Watch

    path = tmp_path / "messages.jsonl"
    monkeypatch.setattr(inbox, "inbox_path", lambda channel: path)
    now = datetime.now(UTC)
    base = dict(agent=AGENT.name, service="iMessage", handle=AGENT.handle, is_from_me=False)
    stale = event("stale", date="2000-01-01T00:00:00Z", **base)
    path.write_text(stale)

    def send_and_reply(agent, text):
        with path.open("a") as f:
            f.write(stale + event("reply", date=now.isoformat(), text="bench pong", **base))
        return now

    monkeypatch.setattr(send_module, "send", send_and_reply)
    watch = Watch(AGENT, quiet=0, max_wait=2)
    observed = []

    def record(message):
        observed.append(message["guid"])
        watch.see(message)

    cli.converse(AGENT, "bench ping", watch, record)
    assert observed == ["reply"]
    assert watch.finished() == "quiet for 0s"


def test_echo_only_replies_to_incoming_test_ping(monkeypatch):
    import argparse

    from bench import cli
    from bench.imessage import inbox

    def incoming(*args):
        yield {"kind": "new", "is_from_me": True, "text": "bench ping"}
        yield {"kind": "new", "is_from_me": False, "text": "unrelated"}
        yield {"kind": "new", "is_from_me": False, "text": "bench ping"}

    sent = []
    monkeypatch.setattr(cli, "get_agent", lambda name: AGENT)
    monkeypatch.setattr(inbox, "open_inbox", lambda: None)
    monkeypatch.setattr(inbox, "events", incoming)
    monkeypatch.setattr(send_module, "send", lambda agent, text: sent.append((agent, text)))
    cli.cmd_imessage_echo(argparse.Namespace(agent=AGENT.name))
    assert sent == [(AGENT, "bench pong")]


def test_reader_messages_appear_in_study_snapshot_and_task_transcript(tmp_path):
    from datetime import UTC, datetime

    from bench import conversation, dashboard, runs

    agent = Agent("instinct", "+15550100001", 1, 10, channel="imessage")
    sent = "2026-11-15T20:00:00Z"
    replied = "2026-11-15T20:00:03Z"
    inbox = tmp_path / "messages.jsonl"
    base = dict(agent=agent.name, handle=agent.handle, service="iMessage")
    inbox.write_text(
        event("prompt", **base, is_from_me=True, text="bench ping", date=sent, emitted_at=sent)
        + event("reply", **base, is_from_me=False, text="bench pong", date=replied, emitted_at=replied)
    )
    sources = {"imessage": inbox}
    run_dir = runs.start(agent, "bench ping", task="Restaurant", step="Day 1",
                         runs_dir=tmp_path / "runs", now=datetime.fromisoformat(sent))
    info = runs.rebuild(run_dir, conversation.load(sources))
    assert info["messages"] == ["prompt", "reply"]
    assert info["timing"]["agent_messages"] == 1

    snap = dashboard.snapshot({agent.name: agent}, sources, run_dir.parent, now=datetime.now(UTC))
    [study_agent] = snap["agents"]
    reply = next(item for item in study_agent["items"] if item.get("guid") == "reply")
    assert study_agent["channel"] == "imessage"
    assert reply["text"] == "bench pong" and not reply["from_me"] and not reply.get("service")
