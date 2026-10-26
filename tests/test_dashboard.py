import base64
import json
from datetime import UTC, datetime

from bench import dashboard
from bench.config import Agent

FO = Agent("wajo", "@fo_test_bot", 90, 900, channel="telegram")
MUSE = Agent("muse", "1807055946647697@bot", 180, 1800, channel="whatsapp")


def tg(kind, id, text=None, *, from_me=False, date="2026-10-25T03:15:29.000Z", at=None, **fields):
    event = {"kind": kind, "emitted_at": at or date, "channel": "telegram", "agent": "wajo", "handle": FO.handle,
             "guid": f"telegram:7001:{id}", "message_id": id, "is_from_me": from_me, "date": date, "attachments": [], **fields}
    if text is not None:
        event["text"] = text
    return event


def wa(kind, id, text="", *, from_me=False, date="2026-10-25T03:57:58.000Z", at=None, **fields):
    return {"kind": kind, "emitted_at": at or date, "channel": "whatsapp", "agent": "muse", "handle": MUSE.handle,
            "guid": f"whatsapp:muse:{id}", "message_id": id, "is_from_me": from_me, "date": date, "attachments": [],
            "text": text, **fields}


def rich(text):
    """A Meta AI rich response as bench-whatsapp records it in "raw"."""
    doc = {"response_id": "r1", "sections": [{"view_model": {"__typename": "GenAISingleLayoutViewModel", "primitive": {
        "__typename": "GenAIMarkdownTextUXPrimitive", "text": text}}}]}
    return {"richResponseMessage": {"unifiedResponse": {"data": base64.b64encode(json.dumps(doc).encode()).decode()}}}


def write_jsonl(path, events):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(e) + "\n" for e in events))
    return path


def build(tmp_path, telegram=(), whatsapp=(), runs=(), agents=(FO, MUSE)):
    inboxes = {"telegram": write_jsonl(tmp_path / "tg" / "messages.jsonl", telegram),
               "whatsapp": write_jsonl(tmp_path / "wa" / "inbox" / "messages.jsonl", whatsapp)}
    for name, info in runs:
        (tmp_path / "runs" / name).mkdir(parents=True)
        (tmp_path / "runs" / name / "run.json").write_text(json.dumps(info))
    return dashboard.snapshot({a.name: a for a in agents}, inboxes, tmp_path / "runs",
                              now=datetime(2026, 10, 25, 4, 0, tzinfo=UTC))


def agent(snap, name):
    return next(a for a in snap["agents"] if a["name"] == name)


def messages(snap, name):
    return [i for i in agent(snap, name)["items"] if i["type"] == "message"]


def test_each_message_appears_once_as_last_recorded(tmp_path):
    snap = build(tmp_path, telegram=[
        tg("new", 4, "hi", from_me=True, read=False),
        tg("new", 5, date="2026-10-25T03:15:45.000Z"),  # a rich message whose text came later
        tg("update", 4, "hi", from_me=True, read=True, at="2026-10-25T03:19:30.000Z"),
        tg("update", 5, "hey you!", rich=True, date="2026-10-25T03:15:45.000Z", at="2026-10-25T03:19:30.000Z"),
    ])
    hi, hey = messages(snap, "wajo")
    assert (hi["text"], hi["from_me"], hi["read"]) == ("hi", True, True)
    assert (hey["text"], hey["rich"], hey["versions"]) == ("hey you!", True, 1)
    assert hey["recorded"] == "2026-10-25T03:15:45.000Z"  # first seen, not last updated
    assert hey["reply_seconds"] == 16
    assert agent(snap, "wajo")["stats"] == {
        "sent": 1, "received": 1, "first": hi["date"], "last": hey["date"], "median_reply_seconds": 16,
        "resets": 0, "last_reset": None}


def test_streamed_reply_counts_its_versions(tmp_path):
    reply = dict(date="2026-10-25T03:59:13.000Z")
    snap = build(tmp_path, whatsapp=[
        wa("new", "A", "Plan a trip", from_me=True),
        wa("update", "A", "Plan a trip", from_me=True, date_delivered="2026-10-25T03:57:58.000Z",
           date_read="2026-10-25T03:57:58.000Z", reactions=[{"by": "agent", "emoji": "👍"}]),
        wa("new", "B", "Here's", **reply),
        wa("update", "B", "Here's your plan", date_edited="2026-10-25T03:59:14.000Z", **reply),
        wa("update", "B", "Here's your plan", date_edited="2026-10-25T03:59:14.000Z", **reply),
        wa("update", "B", "Here's your plan for MLK weekend", date_edited="2026-10-25T03:59:20.000Z", **reply),
    ])
    prompt, plan = messages(snap, "muse")
    assert prompt["date_read"] and prompt["reactions"] == [{"by": "agent", "emoji": "👍"}]
    assert (plan["text"], plan["versions"], plan["date_edited"]) == ("Here's your plan for MLK weekend", 3, "2026-10-25T03:59:20.000Z")
    assert plan["reply_seconds"] == 75


def test_rich_response_without_text_is_decoded_from_raw(tmp_path):
    snap = build(tmp_path, whatsapp=[wa("new", "C", "", raw=rich("Hey! Yep, I'm here"))])
    [message] = messages(snap, "muse")
    assert (message["text"], message["decoded"]) == ("Hey! Yep, I'm here", True)
    assert dashboard.unified_text({"richResponseMessage": {"unifiedResponse": {"data": "not base64!"}}}) == ""


def test_typing_events_close_together_are_one_stretch(tmp_path):
    typing = {"kind": "typing", "channel": "telegram", "agent": "wajo", "handle": FO.handle, "action": "typing"}
    snap = build(tmp_path, telegram=[
        {**typing, "emitted_at": "2026-10-25T03:15:34.000Z"},
        {**typing, "emitted_at": "2026-10-25T03:15:39.000Z"},
        {**typing, "emitted_at": "2026-10-25T03:20:00.000Z"},
    ])
    stretches = [i for i in agent(snap, "wajo")["items"] if i["type"] == "typing"]
    assert [(s["date"], s["until"]) for s in stretches] == [
        ("2026-10-25T03:15:34.000Z", "2026-10-25T03:15:39.000Z"),
        ("2026-10-25T03:20:00.000Z", "2026-10-25T03:20:00.000Z"),
    ]


def test_runs_mark_where_they_began_for_their_agent(tmp_path):
    started = datetime(2026, 10, 24, 20, 57, 29).astimezone()  # run folders are named in local time
    snap = build(tmp_path, whatsapp=[
        wa("new", "A", "hi", from_me=True, date="2026-10-25T03:40:34.000Z"),
        wa("new", "B", "Plan a trip", from_me=True, date=dashboard.iso(started.replace(second=58))),
    ], runs=[
        (started.strftime("%Y%m%d-%H%M%S") + "-travel-day1-muse", {"task": "Travel", "step": "DAY 1", "agent": "muse"}),
        ("20261024-200000-other-wajo", {"task": "Travel", "agent": "wajo"}),
        ("not-a-run", {"agent": "muse"}),
    ])
    kinds = [(i["type"], i.get("text") or i.get("step")) for i in agent(snap, "muse")["items"]]
    assert kinds == [("message", "hi"), ("run", "DAY 1"), ("message", "Plan a trip")]
    assert [i["type"] for i in agent(snap, "wajo")["items"]] == ["run"]


def test_recorded_agents_missing_from_config_still_show(tmp_path):
    snap = build(tmp_path, whatsapp=[wa("new", "A", "hi", from_me=True)], agents=[FO])
    assert [(a["name"], a["channel"], a["handle"]) for a in snap["agents"]] == [
        ("wajo", "telegram", FO.handle), ("muse", "whatsapp", MUSE.handle)]
    assert agent(snap, "wajo")["items"] == []


def test_attachments_link_to_the_recorded_file(tmp_path):
    snap = build(tmp_path, telegram=[tg("update", 9, attachments=[{"name": "memo.ogg", "path": "attachments/9/memo.ogg"}])])
    [message] = messages(snap, "wajo")
    assert message["attachments"] == [{"name": "memo.ogg", "url": (tmp_path / "tg" / "attachments/9/memo.ogg").as_uri()}]


def test_page_embeds_the_data_safely(tmp_path):
    snap = build(tmp_path, telegram=[tg("new", 1, "</script><script>alert(1)</script>")])
    html = dashboard.render(snap)
    assert "__DASHBOARD_DATA__" not in html
    blob = html.split('<script id="data" type="application/json">', 1)[1].split("</script>", 1)[0]
    assert json.loads(blob) == snap


def test_write_keeps_the_page_private(tmp_path, monkeypatch):
    monkeypatch.setattr(dashboard, "snapshot", lambda **kw: {"generated_at": "2026-10-25T04:00:00.000Z", "agents": [], "sources": []})
    out = dashboard.write(tmp_path / "out" / "dashboard.html")
    assert oct(out.stat().st_mode & 0o777) == "0o600"
    assert "Agent conversations" in out.read_text()


DOTS = Agent("dots", "U0DOT0001", 120, 1800, channel="slack")


def sl(kind, ts, text, *, from_me=False, thread=None, **fields):
    """A Slack message; `thread` is the ts of the thread's first message, for replies."""
    date = dashboard.iso(datetime.fromtimestamp(float(ts), UTC))
    event = {"kind": kind, "emitted_at": date, "channel": "slack", "agent": "dots", "handle": DOTS.handle,
             "guid": f"slack:D1:{ts}", "message_id": ts, "is_from_me": from_me, "date": date, "attachments": [],
             "text": text, **fields}
    if thread:
        event["reply_to"] = thread
    return event


def slack_snapshot(tmp_path, events, runs=()):
    for name, info in runs:
        (tmp_path / "runs" / name).mkdir(parents=True)
        (tmp_path / "runs" / name / "run.json").write_text(json.dumps(info))
    inboxes = {"slack": write_jsonl(tmp_path / "slack" / "messages.jsonl", events)}
    return dashboard.snapshot({"dots": DOTS}, inboxes, tmp_path / "runs", now=datetime(2026, 10, 25, 9, 0, tzinfo=UTC))


def test_slack_conversations_are_grouped_and_labelled_by_thread(tmp_path):
    a, b = "1792911000.000100", "1792911100.000200"
    snap = slack_snapshot(tmp_path, [
        sl("new", a, "Plan dinner for 4", from_me=True),
        sl("new", "1792911010.000000", "On it", thread=a),
        sl("new", b, "Book a flight", from_me=True),
        sl("new", "1792911130.000000", "Which day?", thread=b),
        sl("new", "1792911200.000000", "Booked: Nopa, 7pm", thread=a),  # a late reply to the first thread
        sl("new", "1792911300.000000", "Friday", from_me=True, thread=b),
        sl("new", "1792911305.000000", "Done", thread=b),
    ])
    items = agent(snap, "dots")["items"]
    assert [(i["type"], i.get("number") or i.get("thread"), i.get("text") or i.get("title")) for i in items] == [
        ("thread", 1, "Plan dinner for 4"), ("message", 1, "Plan dinner for 4"), ("message", 1, "On it"),
        ("message", 1, "Booked: Nopa, 7pm"),
        ("thread", 2, "Book a flight"), ("message", 2, "Book a flight"), ("message", 2, "Which day?"),
        ("message", 2, "Friday"), ("message", 2, "Done"),
    ]
    heads = [i for i in items if i["type"] == "thread"]
    assert [(h["messages"], h["first_reply_seconds"], h["from_me"]) for h in heads] == [(3, 10.0, True), (4, 30.0, True)]
    replies = {i["text"]: i.get("reply_seconds") for i in items if i["type"] == "message"}
    # Measured within each thread: "Booked" follows the dot's own "On it", so it isn't a first reply.
    assert replies == {"Plan dinner for 4": None, "On it": 10.0, "Booked: Nopa, 7pm": None,
                       "Book a flight": None, "Which day?": 30.0, "Friday": None, "Done": 5.0}
    assert not any("reply_to_text" in i for i in items)  # the header already names the thread
    stats = agent(snap, "dots")["stats"]
    assert stats["first"].startswith("2026-10-25T") and stats["last"] == dashboard.iso(datetime.fromtimestamp(1792911305, UTC))
    assert stats["median_reply_seconds"] == 10.0


def test_slack_threads_keep_runs_in_place_and_survive_a_missing_first_message(tmp_path):
    started = datetime.fromtimestamp(1792911050, UTC).astimezone()  # run folders are named in local time
    snap = slack_snapshot(tmp_path, [
        sl("new", "1792911000.000000", "warm-up", from_me=True),
        sl("new", "1792911100.000000", "Plan a trip", from_me=True),
        sl("new", "1792911200.000000", "orphan reply", thread="1792900000.000000"),  # its thread began before recording
    ], runs=[(started.strftime("%Y%m%d-%H%M%S") + "-travel-day1-dots", {"task": "Travel", "step": "DAY 1", "agent": "dots"})])
    items = agent(snap, "dots")["items"]
    assert [(i["type"], i.get("title") or i.get("text") or i.get("step")) for i in items] == [
        ("thread", "warm-up"), ("message", "warm-up"), ("run", "DAY 1"),
        ("thread", "Plan a trip"), ("message", "Plan a trip"),
        ("thread", None), ("message", "orphan reply"),
    ]


def test_other_channels_are_not_grouped(tmp_path):
    snap = build(tmp_path, telegram=[tg("new", 1, "hi", from_me=True), tg("new", 2, "hello", reply_to=1)])
    items = agent(snap, "wajo")["items"]
    assert [i["type"] for i in items] == ["message", "message"]
    assert items[1]["reply_to_text"] == "hi" and "thread" not in items[1]
