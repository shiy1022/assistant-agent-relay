import http.client
import json
import threading
import time
import urllib.parse

import pytest

from bench import console
from bench.config import Agent
from bench.console import Console, ConsoleError, Job

WAJO = Agent("wajo", "@fo_test_bot", 90, 900, channel="telegram")
MUSE = Agent("muse", "1807055946647697@bot", 180, 1800, channel="whatsapp")
INSTINCT = Agent("instinct", "+15551234567", 90, 900, channel="whatsapp")
DOTS = Agent("dots", "U0CBGVDR955", 120, 1800, channel="slack")
OLD = Agent("old", "+15557654321", 90, 900, channel="imessage")
AGENTS = {a.name: a for a in (WAJO, MUSE, INSTINCT, DOTS, OLD)}

# Stands in for bench: says it sent, then "listens" for --listen seconds, as the dry run does. A run records for a minute.
FAKE = r"""import sys, time
args = sys.argv[1:]
print("dry run: sent", flush=True)
try:
    time.sleep(float(args[args.index("--listen") + 1]) if "--listen" in args else 60 if args[0] == "run" else 0.2)
except KeyboardInterrupt:
    print("bench: stopped", flush=True)
    sys.exit(130)
if "--fail" in open(args[args.index("--file") + 1]).read() if "--file" in args else False:
    print("bench: the agent said no", flush=True)
    sys.exit(1)
"""


@pytest.fixture
def make(tmp_path, monkeypatch):
    monkeypatch.setattr(console, "DRY_RUN", FAKE)
    made = []

    def make(**kw):
        c = Console(AGENTS, jobs_dir=tmp_path / "jobs", dry_run=True,
                    inboxes=kw.pop("inboxes", {"telegram": tmp_path / "tg" / "messages.jsonl"}),
                    runs_dir=tmp_path / "runs", **kw)
        c.start()
        made.append(c)
        return c

    yield make
    for c in made:
        c.close(grace=5)


def wait_for(check, seconds=10):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(0.05)
    raise AssertionError("timed out")


def finished(*jobs):
    return lambda: all(j.state not in ("queued", "running") for j in jobs)


def test_runs_the_same_bench_commands_you_would_type(tmp_path):
    c = Console(AGENTS, jobs_dir=tmp_path)
    prompt = tmp_path / "prompt.txt"
    assert c.command(Job("1", "send", "wajo", "telegram", "hi", listen=300), prompt) == \
        ["send", "wajo", "--file", str(prompt), "--listen", "300"]
    assert c.command(Job("2", "send", "wajo", "telegram", "hi"), prompt) == ["send", "wajo", "--file", str(prompt)]
    assert c.command(Job("3", "run", "muse", "whatsapp", "hi", task="Travel vF Golden", step="DAY 1 CHECKPOINT", cell="D2"),
                     prompt) == ["run", "muse", "--file", str(prompt), "--task", "Travel vF Golden",
                                 "--step", "DAY 1 CHECKPOINT", "--cell", "D2"]
    assert c.command(Job("4", "sync", "dots", "slack"), prompt) == ["sync", "dots"]


def test_refuses_what_bench_would_refuse_before_starting_anything(tmp_path):
    c = Console(AGENTS, jobs_dir=tmp_path)
    for body, error in [
        ({"agents": [], "text": "hi"}, "pick at least one agent"),
        ({"agents": ["nobody"], "text": "hi"}, "can't send to nobody"),
        ({"agents": ["old"], "text": "hi"}, "can't send to old"),  # iMessage is parked
        ({"agents": ["wajo"], "text": "  \n"}, "the message is empty"),
        ({"agents": ["wajo"], "text": "x" * (console.MAX_TEXT + 1)}, "characters"),
        ({"agents": ["wajo"], "text": "hi", "listen": 99999}, "listen must be between"),
        ({"agents": ["wajo"], "text": "hi", "listen": "soon"}, "listen must be a number"),
        ({"agents": ["wajo"], "text": "hi", "mode": "run", "task": " "}, "needs the task's name"),
        ({"agents": ["wajo"], "text": "hi", "mode": "shout"}, "unknown mode"),
    ]:
        with pytest.raises(ConsoleError, match=error):
            c.submit(body)
    with pytest.raises(ConsoleError, match="nothing to sync"):
        c.sync("muse")
    assert c.jobs == []


def test_sends_exactly_as_typed_and_keeps_each_jobs_record(make, tmp_path):
    c = make()
    text = "Plan a trip.\n\n  Keep the spaces  \n"
    [job] = c.submit({"agents": ["wajo"], "text": text, "listen": 0})
    wait_for(finished(job))
    assert job.state == "done" and job.exit_code == 0 and job.sent_at
    folder = tmp_path / "jobs" / job.id
    assert (folder / "prompt.txt").read_text() == text
    assert (folder / "output.log").read_text().startswith(f"$ bench send wajo --file {folder / 'prompt.txt'}\n")
    assert json.loads((folder / "job.json").read_text())["state"] == "done"
    assert oct((folder / "prompt.txt").stat().st_mode)[-3:] == "600"


def test_channels_run_side_by_side_and_one_channel_runs_one_at_a_time(make):
    c = make()
    wajo, muse, instinct = c.submit({"agents": ["wajo", "muse", "instinct"], "text": "hi", "listen": 2})
    wait_for(finished(wajo, muse, instinct))
    assert all(j.state == "done" for j in (wajo, muse, instinct))
    assert wajo.started_at < muse.ended_at and muse.started_at < wajo.ended_at  # Telegram and WhatsApp together
    assert instinct.started_at >= muse.ended_at  # then WhatsApp's next
    # Muse's message had gone out, so its listening was cut short for Instinct's (the bridge records it anyway).
    assert (muse.preempted, wajo.preempted, instinct.preempted) == (True, False, False)


def test_a_new_message_stops_the_last_ones_listening_but_never_a_run(make):
    c = make()
    [first] = c.submit({"agents": ["wajo"], "text": "first", "listen": 60})
    wait_for(lambda: first.sent_at)
    [second] = c.submit({"agents": ["wajo"], "text": "second", "listen": 0})
    wait_for(finished(first, second))
    assert (first.state, first.preempted, first.exit_code) == ("done", True, 130)
    assert second.state == "done"

    [run] = c.submit({"agents": ["muse"], "text": "a task", "mode": "run", "task": "Travel"})
    wait_for(lambda: run.state == "running")
    [after] = c.submit({"agents": ["muse"], "text": "after", "listen": 0})
    time.sleep(0.5)
    assert (run.state, run.stopping, after.state) == ("running", False, "queued")
    c.stop(run.id)
    wait_for(finished(run, after))
    assert run.state == "stopped" and after.state == "done"


def test_stop_cancels_whats_queued_and_stops_whats_running_like_ctrl_c(make):
    c = make()
    [first] = c.submit({"agents": ["muse"], "text": "a task", "mode": "run", "task": "Travel"})
    [second] = c.submit({"agents": ["instinct"], "text": "hi", "listen": 60})
    wait_for(lambda: first.sent_at)
    c.stop(second.id)
    assert second.state == "cancelled"
    c.stop(first.id)
    wait_for(finished(first))
    assert (first.state, first.exit_code) == ("stopped", 130)
    assert "bench: stopped" in first.log  # it got the chance to finish up
    with pytest.raises(ConsoleError, match="already stopped"):
        c.stop(first.id)


def test_failures_say_what_bench_said(make):
    c = make()
    [job] = c.submit({"agents": ["dots"], "text": "--fail please", "listen": 0})
    wait_for(finished(job))
    assert (job.state, job.error, job.exit_code) == ("failed", "the agent said no", 1)


def test_a_job_is_sent_once_its_message_is_recorded(tmp_path):
    inbox = tmp_path / "tg" / "messages.jsonl"
    inbox.parent.mkdir()
    c = Console(AGENTS, jobs_dir=tmp_path / "jobs", inboxes={"telegram": inbox}, runs_dir=tmp_path / "runs")
    early = Job("a", "send", "wajo", "telegram", "hello *there*", state="running", started_at="2026-10-25T04:00:00.000Z")
    again = Job("b", "send", "wajo", "telegram", "hello there", state="running", started_at="2026-10-25T04:00:00.000Z")
    c.jobs = [early, again]

    def message(id, text, at, from_me=True):
        return {"kind": "new", "emitted_at": at, "channel": "telegram", "agent": "wajo", "guid": f"telegram:1:{id}",
                "message_id": id, "is_from_me": from_me, "date": at, "attachments": [], "text": text}

    inbox.write_text("".join(json.dumps(e) + "\n" for e in [
        message(1, "hello there", "2026-10-25T03:00:00.000Z"),  # an hour before: some other time it was sent
        message(2, "hello there", "2026-10-25T04:00:01.000Z", from_me=False),
        message(3, "hello there", "2026-10-25T04:00:02.000Z"),  # Telegram dropped the *…*
    ]))
    c.refresh()
    assert (early.sent_id, early.sent_at) == (3, "2026-10-25T04:00:02.000Z")
    assert again.sent_id is None  # one message, one job

    with inbox.open("a") as f:
        f.write(json.dumps(message(4, "hello there", "2026-10-25T04:00:09.000Z")) + "\n")
    c.refresh()
    assert again.sent_id == 4


def test_a_reply_with_a_link_gets_its_page_saved(tmp_path, monkeypatch):
    inbox = tmp_path / "tg" / "messages.jsonl"
    inbox.parent.mkdir()
    passes = []
    monkeypatch.setattr(console, "SNAPSHOT_SETTLE", 0)
    monkeypatch.setattr(console.snapshots, "save_new", lambda **kw: passes.append(kw["convos"]) or [])
    assert not Console(AGENTS, jobs_dir=tmp_path / "jobs", inboxes={"telegram": inbox}).snapshot_links  # off unless asked
    c = Console(AGENTS, jobs_dir=tmp_path / "jobs", inboxes={"telegram": inbox}, runs_dir=tmp_path / "runs",
                snapshot_links=True)
    c.start()
    try:
        wait_for(lambda: len(passes) == 1)  # one pass when it starts, for what came while it was off
        inbox.write_text(json.dumps({"kind": "new", "emitted_at": "2026-10-27T03:56:15.000Z", "channel": "telegram",
                                     "agent": "wajo", "guid": "telegram:1:9", "message_id": 9, "is_from_me": False,
                                     "date": "2026-10-27T03:56:15.000Z", "attachments": [],
                                     "text": "pay here https://app.gojinko.com/checkout?t=abc"}) + "\n")
        wait_for(lambda: len(passes) == 2)
        assert passes[-1]["wajo"][0]["links"] == ["https://app.gojinko.com/checkout?t=abc"]
    finally:
        c.close(grace=1)


def test_a_dry_run_saves_no_pages(tmp_path):
    assert not Console(AGENTS, jobs_dir=tmp_path / "jobs", dry_run=True).snapshot_links


# MARK: The web server

@pytest.fixture
def server(make):
    c = make()
    s = console.Server(("127.0.0.1", 0), c, "s3cret-token")
    threading.Thread(target=s.serve_forever, daemon=True).start()
    yield s
    s.shutdown()
    s.server_close()


def request(server, method, path, *, host=None, headers=None, body=None):
    port = server.server_address[1]
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    hdrs = {"Host": host or f"127.0.0.1:{port}", **(headers or {})}
    data = json.dumps(body).encode() if body is not None else None
    if data is not None:
        hdrs.setdefault("Content-Type", "application/json")
    conn.request(method, path, body=data, headers=hdrs)
    r = conn.getresponse()
    out = r.status, dict(r.getheaders()), r.read()
    conn.close()
    return out


def test_the_page_opens_only_with_its_token(server):
    assert request(server, "GET", "/")[0] == 403
    assert request(server, "GET", "/?t=wrong")[0] == 403
    status, headers, _ = request(server, "GET", "/?t=s3cret-token")
    assert status == 303 and headers["Location"] == "/"
    cookie = headers["Set-Cookie"].split(";")[0]
    assert "HttpOnly" in headers["Set-Cookie"] and "SameSite=Strict" in headers["Set-Cookie"]
    status, headers, body = request(server, "GET", "/", headers={"Cookie": cookie})
    assert status == 200 and b'const TOKEN = "s3cret-token";' in body
    assert headers["Cache-Control"] == "no-store"


def test_other_host_names_and_websites_are_refused(server):
    token = {"X-Console-Token": "s3cret-token"}
    assert request(server, "GET", "/api/state", headers=token)[0] == 200
    assert request(server, "GET", "/api/state")[0] == 403
    # A website pointing its own name at 127.0.0.1 can't read your chats…
    assert request(server, "GET", "/api/state", host="evil.example:8787", headers=token)[0] == 403
    # …or send, even if it had the token: its pages carry their own Origin.
    body = {"agents": ["wajo"], "text": "hi"}
    assert request(server, "POST", "/api/send", headers={**token, "Origin": "https://evil.example"}, body=body)[0] == 403
    assert request(server, "POST", "/api/send", body=body)[0] == 403
    assert request(server, "POST", "/api/send", headers={**token, "Content-Type": "text/plain"}, body=body)[0] == 415
    # A cross-site fetch with a custom header has to ask first, and nothing answers.
    assert request(server, "OPTIONS", "/api/send")[0] == 501
    assert server.console.jobs == []


def test_sending_from_the_page(server):
    token = {"X-Console-Token": "s3cret-token"}
    origin = {"Origin": f"http://127.0.0.1:{server.server_address[1]}"}
    status, _, body = request(server, "POST", "/api/send", headers={**token, **origin},
                              body={"agents": ["wajo", "dots"], "text": "hi", "listen": 0})
    assert status == 200 and [j["agent"] for j in json.loads(body)["jobs"]] == ["wajo", "dots"]
    status, _, body = request(server, "POST", "/api/send", headers=token, body={"agents": ["wajo"], "text": ""})
    assert (status, json.loads(body)) == (400, {"error": "the message is empty"})
    wait_for(finished(*server.console.jobs))
    state = json.loads(request(server, "GET", "/api/state", headers=token)[2])
    assert [j["state"] for j in state["jobs"]] == ["done", "done"]
    assert [a["name"] for a in state["agents"]] == ["wajo", "muse", "instinct", "dots"]
    assert state["dry_run"] is True and state["snapshot"]["agents"]
    again = json.loads(request(server, "GET", f"/api/state?since={state['version']}", headers=token)[2])
    assert again["snapshot"] is None  # nothing new was recorded


def test_attachments_are_served_only_from_the_inboxes(server, tmp_path):
    cookie = {"Cookie": f"{console.COOKIE}=s3cret-token"}
    saved = tmp_path / "tg" / "attachments" / "9" / "memo.ogg"
    saved.parent.mkdir(parents=True)
    saved.write_bytes(b"ogg")
    url = "/attachment?" + urllib.parse.urlencode({"path": str(saved)})
    assert request(server, "GET", url, headers=cookie)[2] == b"ogg"
    assert request(server, "GET", url)[0] == 403
    for path in ("/etc/passwd", str(tmp_path / "tg" / ".." / "jobs"), str(saved.parent / ".." / ".." / ".." / "x")):
        assert request(server, "GET", "/attachment?" + urllib.parse.urlencode({"path": path}), headers=cookie)[0] == 404


def test_stopping_the_console_lets_running_jobs_write_their_records(make):
    c = make()
    [run] = c.submit({"agents": ["dots"], "text": "a task", "mode": "run", "task": "Travel"})
    [queued] = c.submit({"agents": ["dots"], "text": "later", "listen": 0})
    wait_for(lambda: run.sent_at)
    c.close(grace=5)
    wait_for(finished(run))
    assert (run.state, run.exit_code) == ("stopped", 130)
    assert "bench: stopped" in run.log
    assert queued.state == "cancelled"
    with pytest.raises(ConsoleError, match="stopping"):
        c.submit({"agents": ["wajo"], "text": "hi"})
