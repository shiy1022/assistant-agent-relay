import json
from argparse import Namespace
from datetime import UTC, datetime

import pytest

from bench import cli, conversation, dashboard, resets, runs
from bench.config import Agent
from bench.console import Console, ConsoleError
from bench.resets import ResetError

WAJO = Agent("wajo", "@fo_test_bot", 90, 900, channel="telegram")
MUSE = Agent("muse", "1807055946647697@bot", 180, 1800, channel="whatsapp")


def tg(id, text=None, *, from_me=False, at="2026-10-25T21:00:00.000Z", **fields):
    event = {"kind": "new", "emitted_at": at, "channel": "telegram", "agent": "wajo", "handle": WAJO.handle,
             "guid": f"telegram:7001:{id}", "message_id": id, "is_from_me": from_me, "date": at, "attachments": [], **fields}
    if text is not None:
        event["text"] = text
    return event


CHAT = [
    tg(1, "Plan a trip to Sedona", from_me=True, at="2026-10-25T21:00:00.000Z"),
    tg(2, "Here's your Sedona plan", at="2026-10-25T21:00:20.000Z"),
    tg(3, "Please forget the Sedona trip", from_me=True, at="2026-10-25T21:10:00.000Z"),
    tg(4, "Done, it's gone", at="2026-10-25T21:10:30.000Z"),
    tg(5, from_me=True, service="history_clear", at="2026-10-25T21:20:00.000Z"),  # cleared on the phone
    tg(6, "Where was I going?", from_me=True, at="2026-10-25T21:30:00.000Z"),
    tg(7, "Sedona", at="2026-10-25T21:30:15.000Z"),
]
AFTER_FORGET = datetime(2026, 10, 25, 21, 10, 30, tzinfo=UTC)


@pytest.fixture
def inbox(tmp_path):
    path = tmp_path / "tg" / "messages.jsonl"
    path.parent.mkdir()
    path.write_text("".join(json.dumps(e) + "\n" for e in CHAT))
    return path


def test_resets_are_written_down_one_per_line(tmp_path):
    path = tmp_path / "resets.jsonl"
    now = datetime(2026, 10, 26, 1, 0, tzinfo=UTC)
    first = resets.add(WAJO, how="Asked Fo to forget the trip", method="asked", at=AFTER_FORGET,
                       messages=["telegram:7001:3", "telegram:7001:4"], result="  ", path=path, now=now)
    again = resets.add(WAJO, how="The same second, again", method="command", at=AFTER_FORGET, path=path, now=now)
    assert first == {"id": "reset:wajo:20261025T211030Z", "agent": "wajo", "channel": "telegram",
                     "at": "2026-10-25T21:10:30.000Z", "approximate": False, "method": "asked",
                     "how": "Asked Fo to forget the trip", "result": None,
                     "messages": ["telegram:7001:3", "telegram:7001:4"], "recorded_at": "2026-10-26T01:00:00.000Z",
                     "source": "recorded"}
    assert again["id"] == "reset:wajo:20261025T211030Z-2"
    assert [r["id"] for r in resets.load(path)] == [first["id"], again["id"]]
    assert resets.remove(again["id"], path)["how"] == "The same second, again"
    assert [r["id"] for r in resets.load(path)] == [first["id"]]
    with pytest.raises(ResetError, match="no reset"):
        resets.remove(again["id"], path)
    with pytest.raises(ResetError, match="say what was done"):
        resets.add(WAJO, how=" ", method="asked", path=path)
    with pytest.raises(ResetError, match="method must be one of"):
        resets.add(WAJO, how="Deleted it", method="magic", path=path)


def test_a_cleared_telegram_chat_is_a_reset_unless_one_written_down_names_it(tmp_path):
    path = tmp_path / "resets.jsonl"
    [found] = resets.by_agent(CHAT, path)["wajo"]
    assert (found["source"], found["method"], found["at"], found["messages"]) == \
        ("detected", "by hand", "2026-10-25T21:20:00.000Z", ["telegram:7001:5"])
    resets.add(WAJO, how="Cleared the Telegram chat on your phone", method="by hand",
               at=datetime(2026, 10, 25, 21, 20, tzinfo=UTC), messages=["telegram:7001:5"], path=path)
    [written] = resets.by_agent(CHAT, path)["wajo"]
    assert written["source"] == "recorded"


def test_the_conversation_shows_each_reset_where_it_took_effect(tmp_path, inbox):
    path = tmp_path / "resets.jsonl"
    resets.add(WAJO, how="Asked Fo to forget the trip", method="asked", at=AFTER_FORGET,
               messages=["telegram:7001:3", "telegram:7001:4"], result="It still says Sedona", path=path)
    lines = conversation.load({"telegram": inbox}, path)["wajo"]
    assert [(m["role"], m["id"].rsplit(":", 1)[-1]) for m in lines] == [
        ("user", "1"), ("agent", "2"), ("user", "3"), ("agent", "4"),
        ("reset", "20261025T211030Z"),  # after the "done" of its own second
        ("user", "5"), ("reset", "telegram-7001-5"),  # the cleared chat, found by itself
        ("user", "6"), ("agent", "7")]
    reset = lines[4]
    assert set(reset) == set(lines[0]) | {"reset"}  # every key a message has, and one more
    assert (reset["text"], reset["sent_at"], reset["reset"]["result"]) == \
        ("Asked Fo to forget the trip", "2026-10-25T21:10:30.000Z", "It still says Sedona")

    transcript = runs.markdown({"run": "r", "agent": "wajo", "channel": "telegram"}, lines)
    assert "## Context reset (asked) · " in transcript and "Result: It still says Sedona" in transcript


def test_the_dashboard_marks_each_reset_and_the_messages_that_did_it(tmp_path, inbox):
    path = tmp_path / "resets.jsonl"
    resets.add(WAJO, how="Asked Fo to forget the trip", method="asked", at=AFTER_FORGET,
               messages=["telegram:7001:3", "telegram:7001:4"], path=path)
    snap = dashboard.snapshot({"wajo": WAJO}, {"telegram": inbox}, tmp_path / "runs",
                              now=datetime(2026, 10, 26, tzinfo=UTC), resets_file=path)
    [agent] = snap["agents"]
    kinds = [(i["type"], i.get("id")) for i in agent["items"]]
    assert kinds[3:6] == [("message", 4), ("reset", "reset:wajo:20261025T211030Z"), ("message", 5)]
    by_id = {i["id"]: i for i in agent["items"] if i["type"] == "message"}
    assert (by_id[3]["reset"], by_id[4]["reset"]) == ("reset:wajo:20261025T211030Z",) * 2
    assert "reset" not in by_id[2]
    # Telegram's notice isn't a message you sent, so it isn't counted or timed as one.
    assert by_id[5]["service"] == "history_clear" and by_id[7]["reply_seconds"] == 15
    assert agent["stats"]["sent"] == 3 and agent["stats"]["resets"] == 2
    assert agent["stats"]["last_reset"] == "2026-10-25T21:20:00.000Z"


def test_bench_reset_log_writes_one_down_after_a_message(tmp_path, inbox, monkeypatch, capsys):
    real_load = conversation.load
    monkeypatch.setattr(conversation, "load", lambda: real_load({"telegram": inbox}))
    monkeypatch.setattr(cli, "get_agent", lambda name: {"wajo": WAJO}[name])
    args = Namespace(agent="wajo", how="Asked Fo to forget the trip", method="asked", after="4", at=None,
                     messages=["3"], result=None, approximate=False, remove=None)
    cli.cmd_reset_log(args)
    [written] = resets.load()
    assert (written["at"], written["messages"]) == ("2026-10-25T21:10:30.000Z", ["telegram:7001:3", "telegram:7001:4"])
    assert "Recorded reset:wajo:20261025T211030Z" in capsys.readouterr().out

    cli.cmd_reset_log(Namespace(agent=None, remove=None))
    listed = capsys.readouterr().out
    assert "wajo      ↺ asked: Asked Fo to forget the trip" in listed and "↺ by hand:" in listed  # and the cleared chat

    with pytest.raises(cli.ChannelError, match="no message '99'"):
        cli.cmd_reset_log(Namespace(**{**vars(args), "after": "99"}))


def test_the_console_writes_one_down_after_a_message_but_not_in_a_dry_run(tmp_path, inbox):
    c = Console({"wajo": WAJO}, jobs_dir=tmp_path / "jobs", inboxes={"telegram": inbox}, runs_dir=tmp_path / "runs")
    c.refresh()
    reset = c.record_reset({"agent": "wajo", "how": "Asked Fo to forget", "method": "asked", "after": "telegram:7001:4"})
    assert (reset["at"], reset["messages"]) == ("2026-10-25T21:10:30.000Z", ["telegram:7001:4"])
    assert any(i["type"] == "reset" and i["id"] == reset["id"] for i in c.state()["snapshot"]["agents"][0]["items"])
    for body, error in [({"agent": "nobody", "how": "x", "method": "asked"}, "isn't an agent"),
                        ({"agent": "wajo", "how": "", "method": "asked"}, "say what was done"),
                        ({"agent": "wajo", "how": "x", "method": "asked", "after": "telegram:7001:99"}, "isn't among")]:
        with pytest.raises(ConsoleError, match=error):
            c.record_reset(body)
    dry = Console({"wajo": WAJO}, jobs_dir=tmp_path / "jobs", inboxes={"telegram": inbox}, dry_run=True)
    with pytest.raises(ConsoleError, match="dry run"):
        dry.record_reset({"agent": "wajo", "how": "x", "method": "asked"})
    assert len(resets.load()) == 1
