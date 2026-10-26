import argparse
import json
from datetime import UTC, datetime

import pytest

from bench import cli, conversation, resets, runs
from bench.config import Agent
from tests.test_dashboard import FO, tg, wa, write_jsonl

PROMPT = "Plan a 3-day trip to Sedona.\nI've never been."
START = datetime(2026, 10, 25, 3, 32, 50, tzinfo=UTC)


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def wajo_events():
    return [
        tg("new", 7, "Hello", from_me=True, date="2026-10-25T03:23:05.000Z"),
        tg("new", 8, "hey!", date="2026-10-25T03:23:17.000Z"),
        tg("new", 9, PROMPT, from_me=True, date="2026-10-25T03:32:53.000Z"),
        {"kind": "typing", "emitted_at": "2026-10-25T03:32:57.000Z", "channel": "telegram", "agent": "wajo",
         "handle": FO.handle, "action": "typing"},
        tg("new", 10, "Planning your trip", date="2026-10-25T03:33:20.000Z"),
        tg("new", 11, "here's your plan: https://wajo.ai/d/b6ff", date="2026-10-25T03:37:05.000Z"),
        tg("update", 11, "here's your plan: https://wajo.ai/d/b6ff (updated)", date="2026-10-25T03:37:05.000Z",
           date_edited="2026-10-25T03:37:30.000Z"),
        tg("new", 12, "something the next day", date="2026-10-26T09:00:00.000Z"),
    ]


def convos(tmp_path, events):
    return conversation.load({"telegram": write_jsonl(tmp_path / "inbox" / "messages.jsonl", events)})


@pytest.fixture
def agents(monkeypatch):
    monkeypatch.setattr(runs, "load_agents", lambda: {"wajo": FO})


# MARK: Knowing when the reply is finished

def test_watch_waits_for_a_reply_then_for_quiet():
    clock = Clock()
    watch = runs.Watch(FO, quiet=90, max_wait=900, clock=clock)
    watch.see(tg("new", 9, PROMPT, from_me=True))  # your own prompt isn't the agent doing anything
    clock.now = 200
    assert watch.finished() is None and watch.next() == runs.SLICE_SECONDS
    watch.see({"kind": "typing", "agent": "wajo", "action": "typing"})
    clock.now = 250
    watch.see(tg("new", 10, "Planning"))
    clock.now = 335
    assert watch.finished() is None
    assert watch.next() == 5  # just long enough to reach the end of the quiet period
    clock.now = 340
    assert watch.finished() == "quiet for 90s" and watch.next() == 0


def test_watch_counts_edits_and_typing_as_still_going():
    clock = Clock()
    watch = runs.Watch(FO, quiet=90, max_wait=900, clock=clock)
    watch.see(tg("new", 10, "Here"))
    for t in (60, 120, 180):  # Muse streaming by edits, Hark "working" in its browser
        clock.now = t
        watch.see(tg("update", 10, "Here's more") if t < 180 else {"kind": "typing", "agent": "wajo", "action": "working"})
    clock.now = 260
    assert watch.finished() is None
    clock.now = 270
    assert watch.finished() == "quiet for 90s"


def test_watch_ignores_other_agents_and_gives_up_after_max_wait():
    clock = Clock()
    watch = runs.Watch(FO, quiet=90, max_wait=900, clock=clock)
    watch.see(wa("new", "X", "hi from muse"))
    clock.now = 899
    assert watch.next() == 1
    clock.now = 900
    assert watch.finished() == "stopped waiting after 900s with no reply"


def test_watch_defaults_to_the_agents_settings():
    watch = runs.Watch(Agent("muse", "1@bot", 180, 1800, channel="whatsapp"))
    assert (watch.quiet, watch.max_wait) == (180, 1800)


# MARK: The run folder

def test_start_writes_the_prompt_exactly_and_a_run_still_going(tmp_path):
    run_dir = runs.start(FO, PROMPT, task="Travel vF Golden", step="DAY 1 CHECKPOINT", cell="D2",
                         runs_dir=tmp_path, now=START)
    assert run_dir.name == f"{START.astimezone():%Y%m%d-%H%M%S}-travel-vf-golden-day-1-checkpoint-wajo"
    assert (run_dir / "prompt.txt").read_text() == PROMPT
    info = json.loads((run_dir / "run.json").read_text())
    assert list(info)[:len(runs.STANDARD)] == list(runs.STANDARD)
    assert info["started_at"] == "2026-10-25T03:32:50.000Z"
    assert info["ended_at"] is None and info["finished"] is None


def test_finish_cuts_the_transcript_to_the_run(tmp_path, agents):
    run_dir = runs.start(FO, PROMPT, task="Travel vF Golden", runs_dir=tmp_path / "runs", now=START)
    info = runs.finish(run_dir, "quiet for 90s", now=datetime(2026, 10, 25, 3, 40, tzinfo=UTC),
                       conversations=convos(tmp_path, wajo_events()))

    transcript = [json.loads(line) for line in (run_dir / "transcript.jsonl").read_text().splitlines()]
    assert [m["text"] for m in transcript] == [PROMPT, "Planning your trip", "here's your plan: https://wajo.ai/d/b6ff (updated)"]
    assert info["messages"] == [m["id"] for m in transcript]
    assert info["prompt_message"] == "telegram:7001:9"
    assert (info["ended_at"], info["finished"]) == ("2026-10-25T03:40:00.000Z", "quiet for 90s")
    assert info["links"] == ["https://wajo.ai/d/b6ff"]
    assert info["timing"] == {
        "prompt_sent": "2026-10-25T03:32:53.000Z", "first_reply": "2026-10-25T03:33:20.000Z",
        "last_change": "2026-10-25T03:37:30.000Z", "seconds_to_first_reply": 27.0, "seconds_to_finished": 277.0,
        "agent_messages": 2, "streamed_edits": 1, "reply_chars": len("Planning your trip") + len(transcript[2]["text"])}
    md = (run_dir / "transcript.md").read_text()
    assert md.startswith("# Travel vF Golden · wajo (telegram)")
    assert "I've never been." in md and "something the next day" not in md


def test_rebuild_keeps_extra_fields_and_lists_deliverables(tmp_path, agents):
    run_dir = runs.start(FO, PROMPT, task="Travel vF Golden", runs_dir=tmp_path / "runs", now=START)
    runs.end(run_dir, "saved by hand", now=datetime(2026, 10, 25, 3, 48, tzinfo=UTC))
    info = runs.read_info(run_dir)
    runs.write_info(run_dir, info | {"notes": "asked where I'm flying from",
                                     "deliverables": [{"path": "deliverables/plan.html", "source": "https://wajo.ai/d/b6ff"}],
                                     "timing": {"send_clicked": "2026-10-25T03:32:52.900Z"}})
    (run_dir / "deliverables").mkdir()
    (run_dir / "deliverables" / "plan.html").write_text("<h1>Sedona</h1>")
    (run_dir / "deliverables" / "notes.txt").write_text("saved later")

    info = runs.rebuild(run_dir, convos(tmp_path, wajo_events()))
    assert info["notes"] == "asked where I'm flying from"
    assert info["deliverables"] == [{"path": "deliverables/notes.txt", "source": None},
                                    {"path": "deliverables/plan.html", "source": "https://wajo.ai/d/b6ff"}]
    assert info["timing"]["send_clicked"] == "2026-10-25T03:32:52.900Z"  # kept alongside what's computed
    assert info["timing"]["agent_messages"] == 2
    assert list(json.loads((run_dir / "run.json").read_text()))[-1] == "notes"  # standard keys first


def test_run_json_says_what_came_before_the_prompt(tmp_path, agents):
    first = runs.start(FO, "an earlier run", task="t", runs_dir=tmp_path / "runs", now=datetime(2026, 10, 25, 3, 0, tzinfo=UTC))
    run_dir = runs.start(FO, PROMPT, task="Travel vF Golden", runs_dir=tmp_path / "runs", now=START)
    info = runs.rebuild(run_dir, convos(tmp_path, wajo_events()))
    assert info["context"] == {"last_reset": None, "messages_since_reset": 2, "runs_since_reset": 1}
    assert "No context reset recorded before it, and 2 messages before it." in (run_dir / "transcript.md").read_text()

    # Reset after the first run and the hello, before the prompt; the next day's reset doesn't count.
    resets.add(FO, how="Asked Fo to forget everything.", method="asked", at=datetime(2026, 10, 25, 3, 23, 10, tzinfo=UTC))
    resets.add(FO, how="Another, later.", method="command", at=datetime(2026, 10, 26, tzinfo=UTC))
    info = runs.rebuild(run_dir, convos(tmp_path, wajo_events()))
    assert info["context"] == {
        "last_reset": {"id": "reset:wajo:20261025T032310Z", "at": "2026-10-25T03:23:10.000Z",
                       "how": "Asked Fo to forget everything.", "method": "asked", "result": None,
                       "approximate": False, "source": "recorded"},
        "messages_since_reset": 1, "runs_since_reset": 0}  # Fo's "hey!" came after it
    assert "Last context reset before it: " in (run_dir / "transcript.md").read_text()
    assert list(json.loads((run_dir / "run.json").read_text()))[:len(runs.STANDARD)] == list(runs.STANDARD)
    assert first.exists()


def test_the_prompt_is_found_even_if_the_channel_reformatted_it(tmp_path, agents):
    events = [e | {"text": "Plan a 3-day trip to Sedona.\n> I've never been."} if e.get("message_id") == 9 else e
              for e in wajo_events()]
    run_dir = runs.start(FO, PROMPT, task="t", runs_dir=tmp_path / "runs", now=START)
    info = runs.rebuild(run_dir, convos(tmp_path, events))
    assert info["prompt_message"] == "telegram:7001:9"


def test_a_prompt_that_was_never_recorded_is_an_error(tmp_path, agents):
    run_dir = runs.start(FO, PROMPT, task="t", runs_dir=tmp_path / "runs", now=datetime(2026, 10, 27, tzinfo=UTC))
    with pytest.raises(runs.RunError, match="bench sync wajo"):
        runs.rebuild(run_dir, convos(tmp_path, wajo_events()))


# MARK: bench run

def test_bench_run_sends_records_until_quiet_and_writes_the_folder(tmp_path, monkeypatch, agents):
    inbox = tmp_path / "inbox" / "messages.jsonl"
    monkeypatch.setattr(runs, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(conversation, "inbox_path", lambda channel: tmp_path / ("inbox" if channel == "telegram" else channel) / "messages.jsonl")
    monkeypatch.setattr(cli, "get_agent", lambda name: FO)
    prompt_file = tmp_path / "prompt.txt"
    prompt_file.write_text(PROMPT)
    clock = Clock()
    monkeypatch.setattr(runs.time, "monotonic", clock)
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    sent = []

    def converse(agent, text, plan, on_event):
        sent.append(text)
        script = [tg("new", 9, text, from_me=True, date=now), tg("new", 10, "on it", date=now), tg("new", 11, "done", date=now)]
        write_jsonl(inbox, script)
        for event in script:
            on_event(event)
        slices = []
        while seconds := plan.next():
            slices.append(seconds)
            clock.now += seconds
        assert sum(slices) == FO.quiet_period_seconds

    monkeypatch.setattr(cli, "converse", converse)
    cli.cmd_run(argparse.Namespace(agent="wajo", file=str(prompt_file), task="Travel", step=None, cell=None,
                                   quiet=None, max_wait=None))
    assert sent == [PROMPT]
    [run_dir] = (tmp_path / "runs").iterdir()
    info = runs.read_info(run_dir)
    assert info["finished"] == "quiet for 90s"
    assert [m["text"] for m in map(json.loads, (run_dir / "transcript.jsonl").read_text().splitlines())] == [PROMPT, "on it", "done"]


def test_bench_run_that_fails_to_send_says_so_in_run_json(tmp_path, monkeypatch):
    monkeypatch.setattr(runs, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(cli, "get_agent", lambda name: FO)
    prompt_file = tmp_path / "prompt.txt"
    prompt_file.write_text(PROMPT)

    def converse(agent, text, plan, on_event):
        raise cli.ChannelError("not logged in")

    monkeypatch.setattr(cli, "converse", converse)
    with pytest.raises(cli.ChannelError):
        cli.cmd_run(argparse.Namespace(agent="wajo", file=str(prompt_file), task="Travel", step=None, cell=None,
                                       quiet=None, max_wait=None))
    [run_dir] = (tmp_path / "runs").iterdir()
    assert runs.read_info(run_dir)["finished"] == "failed: not logged in"


def test_listen_for_records_once():
    plan = cli.ListenFor(120)
    assert (plan.listening, plan.next(), plan.next()) == (True, 120, 0)
    assert (cli.ListenFor(None).listening, cli.ListenFor(None).next()) == (False, 0)


def test_inbox_shows_events_in_the_order_sent():
    synced_late = {"kind": "new", "date": "2026-10-25T06:39:19.757Z", "emitted_at": "2026-10-25T07:38:47.572Z"}
    imported = {"kind": "new", "date": "2026-10-25T07:22:45.427Z", "emitted_at": "2026-10-25T07:22:47.079Z"}
    typing = {"kind": "typing", "emitted_at": "2026-10-25T07:22:47.079Z"}
    assert sorted([typing, imported, synced_late], key=cli.event_order) == [synced_late, imported, typing]


class FakeChannel:
    def __init__(self, log):
        self.log = log

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.log.append("closed")

    def sync(self):
        self.log.append("sync")

    def send(self, agent, text):
        self.log.append(("send", text))

    def listen(self, seconds):
        self.log.append(("listen", seconds))


class Slices:
    listening = True

    def __init__(self, *seconds):
        self.seconds = list(seconds)

    def next(self):
        return self.seconds.pop(0) if self.seconds else 0


@pytest.mark.parametrize("channel, factory", [("slack", "slack_channel"), ("hark", "hark_channel")])
def test_converse_sends_then_records_each_slice(monkeypatch, channel, factory):
    log = []
    monkeypatch.setattr(cli, factory, lambda agent, on_event: FakeChannel(log))
    cli.converse(Agent("x", "h", 90, 900, channel=channel), "hi", Slices(10, 10, 3))
    assert log == ["sync", ("send", "hi"), ("listen", 10), ("listen", 10), ("listen", 3), "closed"]


def test_converse_on_whatsapp_follows_the_bridge_while_online(monkeypatch):
    from bench.channels import whatsapp
    log = []
    monkeypatch.setattr(whatsapp, "write_agents", lambda: None)
    monkeypatch.setattr(whatsapp, "open_inbox", lambda: "box")
    monkeypatch.setattr(whatsapp, "set_online", lambda on: log.append(("online", on)))
    monkeypatch.setattr(whatsapp, "send", lambda agent, text: log.append(("send", text)))
    monkeypatch.setattr(whatsapp, "follow", lambda box, names, seconds, on_event: log.append(("follow", seconds)))
    muse = Agent("muse", "1@bot", 180, 1800, channel="whatsapp")
    cli.converse(muse, "hi", Slices(10, 4))
    assert log == [("online", True), ("send", "hi"), ("follow", 10), ("follow", 4), ("online", False)]
    log.clear()
    cli.converse(muse, "hi", cli.ListenFor(None))  # bench send without --listen: just long enough to see it sent
    assert log == [("send", "hi"), ("follow", 2)]
