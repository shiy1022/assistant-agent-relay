import json
import os
import shutil
import socket
import tempfile
import threading
from pathlib import Path

import pytest

from bench.channels import whatsapp
from bench.channels.whatsapp import WhatsAppError
from bench.config import Agent, ConfigError, normalize_handle
from bench.inbox import Inbox

MUSE = Agent("muse", "1807055946647697@bot", 180, 1800, channel="whatsapp")
INSTINCT = Agent("instinct", "+15550100009", 90, 900, channel="whatsapp")
FO = Agent("wajo", "@hey_fo_bot", 90, 900, channel="telegram")


def test_handles_and_addresses():
    assert normalize_handle("1807055946647697@bot", "whatsapp") == "1807055946647697@bot"
    assert whatsapp.jid(MUSE.handle) == "1807055946647697@bot"
    assert whatsapp.jid(INSTINCT.handle) == "15550100009@s.whatsapp.net"
    with pytest.raises(ConfigError):
        normalize_handle("muse@bot.example", "whatsapp")


def test_agents_json_lists_only_whatsapp_agents(tmp_path):
    names = whatsapp.write_agents([MUSE, INSTINCT, FO], bridge_dir=tmp_path / "wa")
    assert names == ["muse", "instinct"]
    path = tmp_path / "wa" / "agents.json"
    assert json.loads(path.read_text())[0] == {"name": "muse", "handle": MUSE.handle, "jids": [MUSE.handle]}
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert oct((tmp_path / "wa").stat().st_mode & 0o777) == "0o700"
    mtime = path.stat().st_mtime_ns
    whatsapp.write_agents([MUSE, INSTINCT], bridge_dir=tmp_path / "wa")
    assert path.stat().st_mtime_ns == mtime  # unchanged, so the bridge doesn't reload


@pytest.fixture
def bridge_socket():
    """A stand-in for bench-whatsapp's socket that answers like the real one."""
    folder = tempfile.mkdtemp(prefix="bw")  # Unix socket paths are limited to 104 bytes
    path = Path(folder) / "bench.sock"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path))
    server.listen()
    requests = []

    def serve():
        while True:
            try:
                conn, _ = server.accept()
            except OSError:
                return
            with conn:
                req = json.loads(conn.makefile().readline())
                requests.append(req)
                if req["op"] == "send" and req["agent"] != "muse":
                    reply = {"ok": False, "error": f"{req['agent']!r} isn't in agents.json, so the bridge won't message it"}
                elif req["op"] == "send":
                    reply = {"ok": True, "event": {"kind": "new", "agent": "muse", "text": req["text"], "is_from_me": True}}
                elif req["op"] == "found":
                    seen = sum(r["op"] == "found" for r in requests) > 1  # a reply turns up on the second check
                    match = {"chat": "98765432101234@lid", "lid": "98765432101234@lid", "phone": "+15550100009", "push_name": "Instinct"}
                    reply = {"ok": True, "active": True, "found": [match] if seen else []}
                else:
                    reply = {"ok": True, "online": req.get("online")}
                conn.sendall((json.dumps(reply) + "\n").encode())

    threading.Thread(target=serve, daemon=True).start()
    yield path, requests
    server.close()
    shutil.rmtree(folder)


def test_send_and_presence_go_through_the_socket(bridge_socket):
    path, requests = bridge_socket
    event = whatsapp.send(MUSE, "hi", socket_path=path)
    assert event["text"] == "hi"
    whatsapp.set_online(True, socket_path=path)
    assert requests == [{"op": "send", "agent": "muse", "text": "hi"}, {"op": "presence", "online": True}]


def test_refusals(bridge_socket):
    path, _ = bridge_socket
    with pytest.raises(WhatsAppError, match="isn't in agents.json"):
        whatsapp.send(INSTINCT, "hi", socket_path=path)
    with pytest.raises(WhatsAppError, match="isn't a WhatsApp agent"):
        whatsapp.send(FO, "hi", socket_path=path)
    with pytest.raises(WhatsAppError, match="isn't running"):
        whatsapp.send(MUSE, "hi", socket_path=path.with_name("missing.sock"))


def test_find_waits_for_a_reply_then_settles(bridge_socket):
    path, requests = bridge_socket
    found = whatsapp.find("Instinct", seconds=5, poll=0.05, settle=0.2, socket_path=path)
    assert [m["phone"] for m in found] == ["+15550100009"]
    assert requests[0] == {"op": "find", "name": "Instinct", "seconds": 5}


def test_private_id_handles():
    assert normalize_handle("98765432101234@lid", "whatsapp") == "98765432101234@lid"
    assert whatsapp.jid("98765432101234@lid") == "98765432101234@lid"


def test_follow_shows_only_new_events_for_the_agent(tmp_path):
    inbox = tmp_path / "messages.jsonl"
    inbox.write_text(json.dumps({"kind": "new", "guid": "old", "agent": "muse"}) + "\n")
    box = whatsapp.open_inbox(inbox)
    with inbox.open("a") as f:
        f.write(json.dumps({"kind": "new", "guid": "a", "agent": "muse", "text": "hey!"}) + "\n")
        f.write(json.dumps({"kind": "typing", "agent": "instinct", "action": "typing"}) + "\n")
    seen = []
    whatsapp.follow(box, {"muse"}, 0.6, seen.append)
    assert [e["guid"] for e in seen] == ["a"]


def test_link_shows_each_code_then_starts_the_bridge(tmp_path, monkeypatch):
    fake = tmp_path / "bench-whatsapp"
    fake.write_text("#!/bin/sh\n"
                    "echo '{\"event\":\"code\",\"code\":\"2@first\"}'\n"
                    "echo '{\"event\":\"code\",\"code\":\"2@second\"}'\n"
                    "echo '{\"event\":\"success\",\"phone\":\"+15550100000\"}'\n")
    fake.chmod(0o755)
    calls = []
    monkeypatch.setattr(whatsapp, "BINARY", fake)
    monkeypatch.setattr(whatsapp, "write_agents", lambda: calls.append("agents"))
    monkeypatch.setattr(whatsapp, "stop", lambda: calls.append("stop"))
    monkeypatch.setattr(whatsapp, "start", lambda: calls.append("start"))
    shown = []
    assert whatsapp.link(show=shown.append) == "+15550100000"
    assert shown == ["2@first", "2@second"]
    assert calls == ["agents", "stop", "start"]


def test_link_failure_is_reported_and_the_bridge_left_stopped(tmp_path, monkeypatch):
    fake = tmp_path / "bench-whatsapp"
    fake.write_text("#!/bin/sh\necho '{\"event\":\"timeout\"}'\necho 'no QR code was scanned in time' >&2\nexit 1\n")
    fake.chmod(0o755)
    started = []
    monkeypatch.setattr(whatsapp, "BINARY", fake)
    monkeypatch.setattr(whatsapp, "write_agents", lambda: None)
    monkeypatch.setattr(whatsapp, "stop", lambda: None)
    monkeypatch.setattr(whatsapp, "start", lambda: started.append(True))
    with pytest.raises(WhatsAppError, match="no QR code was scanned"):
        whatsapp.link(show=lambda code: None)
    assert started == []
