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
        "sent": 1, "received": 1, "first": hi["date"], "last": hey["date"], "median_reply_seconds": 16}


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
