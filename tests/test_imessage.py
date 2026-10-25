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
