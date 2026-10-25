import json
from datetime import UTC, datetime

from bench import conversation
from tests.test_dashboard import rich, tg, wa, write_jsonl

KEYS = {"id", "agent", "channel", "role", "text", "sent_at", "edited_at", "deleted_at", "versions", "reply_to",
        "thread", "attachments", "buttons", "links", "reactions", "delivered_at", "read_at", "rich", "recorded_at"}


def slack(kind, ts, text, *, from_me=False, reply_to=None, at=None):
    seconds, _, fraction = ts.partition(".")
    date = datetime.fromtimestamp(int(seconds), UTC).strftime("%Y-%m-%dT%H:%M:%S.") + fraction[:3] + "Z"
    event = {"kind": kind, "emitted_at": at or date, "channel": "slack", "agent": "dots", "handle": "U0DOT0001",
             "guid": f"slack:D0DOTS001:{ts}", "message_id": ts, "is_from_me": from_me, "text": text, "date": date,
             "attachments": []}
    return event | ({"reply_to": reply_to} if reply_to else {})


def hark(kind, id, text, *, from_me=False, date, at=None, **fields):
    return {"kind": kind, "emitted_at": at or date, "channel": "hark", "agent": "hark", "handle": "hark.com",
            "guid": f"hark:{id}", "message_id": id, "is_from_me": from_me, "text": text, "date": date, **fields}


def load(tmp_path, **inboxes):
    return conversation.load({c: write_jsonl(tmp_path / c / "messages.jsonl", events) for c, events in inboxes.items()})


def test_a_streamed_reply_is_one_message_with_its_final_text(tmp_path):
    convo = load(tmp_path, whatsapp=[
        wa("new", "P", "plan a trip", from_me=True),
        wa("update", "P", "plan a trip", from_me=True, date_read="2026-10-25T03:57:58.000Z"),
        wa("new", "R", "", date="2026-10-25T03:59:13.000Z", raw=rich("Here's")),
        wa("update", "R", "Here's your", date="2026-10-25T03:59:13.000Z", date_edited="2026-10-25T03:59:14.000Z"),
        wa("update", "R", "Here's your plan", date="2026-10-25T03:59:13.000Z", date_edited="2026-10-25T03:59:15.000Z"),
    ])
    prompt, reply = convo["muse"]
    assert (prompt["role"], prompt["read_at"]) == ("user", "2026-10-25T03:57:58.000Z")
    assert (reply["role"], reply["text"], reply["versions"]) == ("agent", "Here's your plan", 3)
    assert reply["edited_at"] == "2026-10-25T03:59:15.000Z"
    assert reply["recorded_at"] == "2026-10-25T03:59:13.000Z"  # when it was first seen, not last


def test_text_only_in_a_rich_response_is_decoded(tmp_path):
    convo = load(tmp_path, whatsapp=[wa("new", "R", "", raw=rich("Hey! Yep"))])
    assert convo["muse"][0]["text"] == "Hey! Yep"
    assert convo["muse"][0]["rich"] is True


def test_messages_are_in_the_order_sent_not_the_order_recorded(tmp_path):
    # A Hark run imported with its observed times, then a first sync that found older messages.
    convo = load(tmp_path, hark=[
        hark("new", "p", "Plan a trip", from_me=True, date="2026-10-25T07:22:45.427Z", at="2026-10-25T07:22:47.079Z"),
        hark("new", "a", "Where from?", date="2026-10-25T07:22:57.212Z", triggered_by="p"),
        hark("new", "o", "Hi, I'm Hark", date="2026-10-25T06:39:19.757Z", at="2026-10-25T07:38:47.572Z"),
        hark("new", "x", "ping", from_me=True, date="2026-10-25T06:58:13.933Z", at="2026-10-25T07:38:47.575Z"),
    ])
    assert [m["text"] for m in convo["hark"]] == ["Hi, I'm Hark", "ping", "Plan a trip", "Where from?"]


def test_same_second_messages_keep_telegrams_order(tmp_path):
    convo = load(tmp_path, telegram=[tg("new", 6, "second", date="2026-10-25T03:15:45.000Z"),
                                     tg("new", 5, "first", date="2026-10-25T03:15:45.000Z", at="2026-10-25T03:15:45.000Z")])
    assert [m["text"] for m in convo["wajo"]] == ["first", "second"]


def test_every_channel_gives_the_same_fields(tmp_path):
    convo = load(tmp_path,
                 telegram=[tg("new", 9, "hi", from_me=True, read=True)],
                 whatsapp=[wa("new", "A", "hi", from_me=True)],
                 slack=[slack("new", "1792911379.937829", "hi", from_me=True)],
                 hark=[hark("new", "h", "hi", from_me=True, date="2026-10-25T06:58:13.933Z")])
    assert set(convo) == {"wajo", "muse", "dots", "hark"}
    for messages in convo.values():
        assert set(messages[0]) == KEYS
        assert messages[0]["role"] == "user"


def test_replies_point_at_message_ids(tmp_path):
    convo = load(tmp_path,
                 telegram=[tg("new", 9, "q", from_me=True), tg("new", 10, "a", reply_to=9)],
                 slack=[slack("new", "1792911379.937829", "ping", from_me=True),
                        slack("new", "1792911392.852719", "pong", reply_to="1792911379.937829")],
                 hark=[hark("new", "p", "q", from_me=True, date="2026-10-25T07:22:45.427Z"),
                       hark("new", "a", "a", date="2026-10-25T07:22:57.212Z", triggered_by="p")])
    assert convo["wajo"][1]["reply_to"] == "telegram:7001:9"
    assert convo["hark"][1]["reply_to"] == "hark:p"
    ping, pong = convo["dots"]
    assert pong["reply_to"] == ping["id"] == "slack:D0DOTS001:1792911379.937829"
    assert ping["thread"] == pong["thread"] == ping["id"]
    assert convo["wajo"][0]["thread"] is None  # only Slack has threads


def test_a_deleted_message_keeps_what_it_said(tmp_path):
    convo = load(tmp_path, telegram=[tg("new", 4, "oops"), tg("deleted", 4, "oops", date_deleted="2026-10-25T03:20:00.000Z")])
    assert [(m["text"], m["deleted_at"]) for m in convo["wajo"]] == [("oops", "2026-10-25T03:20:00.000Z")]


def test_links_attachments_and_buttons(tmp_path):
    convo = load(tmp_path, telegram=[tg(
        "new", 11, "your plan: https://wajo.ai/d/b6ff. anyone can open it",
        link_preview={"url": "https://wajo.ai/join?code=X"},
        buttons=[[{"text": "Open", "kind": "url", "url": "https://wajo.ai/open"}, {"text": "No", "kind": "callback"}]],
        attachments=[{"name": "plan.pdf", "mime_type": "application/pdf", "path": "attachments/11/plan.pdf"}, {"name": "big.mov"}])])
    m = convo["wajo"][0]
    assert m["links"] == ["https://wajo.ai/d/b6ff", "https://wajo.ai/join?code=X", "https://wajo.ai/open"]
    assert m["attachments"] == [
        {"name": "plan.pdf", "mime_type": "application/pdf", "path": str(tmp_path / "telegram" / "attachments/11/plan.pdf")},
        {"name": "big.mov", "mime_type": None, "path": None}]
    assert m["buttons"][0][0]["text"] == "Open"


def test_typing_is_not_a_message(tmp_path):
    convo = load(tmp_path, hark=[{"kind": "typing", "emitted_at": "2026-10-25T07:22:47.079Z", "channel": "hark",
                                  "agent": "hark", "handle": "hark.com", "action": "working", "message_id": "w1"}])
    assert convo == {}


def test_export_writes_one_file_per_agent_from_every_inbox(tmp_path):
    inboxes = {"telegram": write_jsonl(tmp_path / "inbox" / "telegram" / "messages.jsonl", [tg("new", 9, "hi", from_me=True)]),
               # WhatsApp's inbox lives elsewhere (see bench.inbox.inbox_path), but its export doesn't
               "whatsapp": write_jsonl(tmp_path / "private" / "whatsapp" / "inbox" / "messages.jsonl", [wa("new", "A", "yo")])}
    out = tmp_path / "conversations"
    written = conversation.export(out, inboxes=inboxes)
    assert sorted(p.name for p in written) == ["muse.jsonl", "wajo.jsonl"]
    assert sorted(p.name for p in out.iterdir()) == ["muse.jsonl", "wajo.jsonl"]  # nothing half-written left behind
    assert [json.loads(line)["text"] for line in (out / "muse.jsonl").read_text().splitlines()] == ["yo"]
