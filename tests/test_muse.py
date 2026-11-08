import itertools
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from bench import conversation
from bench.channels import muse as muse_module
from bench.channels.muse import HOME, MuseChannel, MuseError, MuseWeb, account_of, status
from bench.config import Agent, ConfigError, normalize_handle

MUSE = Agent("muse", "muse.ai", 180, 1800, channel="muse")
ACCOUNT, COMPUTER = "1452962344561730", "15d53689-dc52-4e8a-9cfd-47d4a837b1b2"
ids = itertools.count(1)


def at(offset_seconds: float = 0) -> str:
    return (datetime.now(UTC) + timedelta(seconds=offset_seconds)).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def msg(role="assistant", content="", when=None, **fields) -> dict:
    n = next(ids)
    mid = f"assistant-msg-{n:04d}" if role == "assistant" else f"{n:04d}-user"
    return {"id": mid, "messageId": mid, "role": role, "content": content, "createdAt": when or at(),
            "isTextStreaming": False, "source": "runtime", **fields}


class FakeSite:
    """muse.ai as MuseChannel sees it: its copy of the main chat, as MuseWeb.read() returns it."""

    def __init__(self, account=ACCOUNT):
        self.account_id = account
        self.messages: list[dict] = []
        self.busy, self.signed_in = False, True
        self.sides = [{"session_id": "wa", "channel": "whatsapp", "title": "WhatsApp", "is_primary": False},
                      {"session_id": "main", "channel": None, "title": "Use Muse on WhatsApp", "is_primary": True}]
        self.sent, self.box, self.tab, self.path = [], "", HOME, "/"
        self.shows_sent = True
        self.copy_lags = False  # muse.ai writes its copy only as Muse finishes its turn
        self.shown: list[dict] = []  # what the page shows that the copy doesn't have yet
        self.reads = 0

    def read(self):
        self.reads += 1
        key = f"{self.account_id}\0shell-bootstrap:wss://{COMPUTER}.metaaivm.com/:chat.history:40:messages"
        shown = [{"id": m["id"], "role": m["role"], "text": ("你：\n\n" if m["role"] == "user" else "") + m["content"]}
                 for m in self.messages[-8:] + self.shown]
        return {"path": self.path, "busy": self.busy, "signedIn": self.signed_in, "shown": shown,
                "main": [{"key": key, "cachedAtMs": 1, "revision": 1, "messages": [dict(m) for m in self.messages[-40:]]}],
                "sides": [{"key": f"{self.account_id}\0x", "sessions": self.sides}]}

    def account(self):
        data = self.read()
        return account_of(data) if data["signedIn"] else None

    def draft(self):
        return self.box

    def tab_url(self):
        return self.tab

    def send(self, text):
        self.sent.append(text)
        if self.shows_sent:
            (self.shown if self.copy_lags else self.messages).append(msg("user", text))

    def finish_turn(self, reply: str):
        """Muse finishes: its copy gets what the page showed, and the reply."""
        self.messages += self.shown + [msg(content=reply)]
        self.shown, self.busy = [], False


@pytest.fixture
def site():
    return FakeSite()


@pytest.fixture
def dirs(tmp_path):
    return SimpleNamespace(pins=tmp_path / "pins", inbox=tmp_path / "inbox" / "muse" / "messages.jsonl")


def channel(site, dirs, agents=(MUSE,), **kw):
    return MuseChannel(list(agents), site=site, pin_dir=dirs.pins, inbox=dirs.inbox, sleep=lambda s: None, **kw)


def events(dirs):
    return [json.loads(line) for line in dirs.inbox.read_text().splitlines()] if dirs.inbox.exists() else []


def sync(site, dirs):
    with channel(site, dirs) as m:
        return m.sync()


def test_first_sync_pins_the_account_and_records_what_muse_ai_holds(site, dirs):
    site.messages = [msg(content="Heads-up for tomorrow", when=at(-86400 * 5)),
                     msg("user", "can you show your memory.md"),
                     msg(content="", presentations=[{"kind": "file", "displayText": "MEMORY.md"}]),
                     msg(content="", presentationCarrier=True)]  # an empty record Muse fills in later
    assert sync(site, dirs) == 3
    got = events(dirs)
    assert [e["text"] for e in got] == ["Heads-up for tomorrow", "can you show your memory.md", "MEMORY.md"]
    assert [e["is_from_me"] for e in got] == [False, True, False]
    assert got[2]["rich"] is True and got[2]["buttons"] == [[{"text": "MEMORY.md", "kind": "file"}]]
    assert all(e["channel"] == "muse" and e["guid"] == f"muse:{e['message_id']}" for e in got)
    pins = json.loads((dirs.pins / "pins.json").read_text())
    assert pins["account_id"] == ACCOUNT and pins["computer"] == COMPUTER


def test_a_streamed_reply_is_recorded_as_it_grows(site, dirs):
    sync(site, dirs)
    reply = msg(content="Let me", isTextStreaming=True)
    site.messages.append(reply)
    sync(site, dirs)
    reply.update(content="Let me check your calendar.", isTextStreaming=False)
    sync(site, dirs)
    assert [(e["kind"], e["text"], e.get("streaming")) for e in events(dirs)] == [
        ("new", "Let me", True), ("update", "Let me check your calendar.", None)]


def test_muse_working_shows_as_working(site, dirs):
    site.busy = True
    with channel(site, dirs) as m:
        m.sync()
        m.sync()  # within ten seconds: not again
    assert [(e["kind"], e["action"]) for e in events(dirs)] == [("typing", "working")]


def test_a_reply_names_the_message_it_answers_but_not_itself(site, dirs):
    question = msg("user", "where to?")
    site.messages = [question, msg(content="Austin.", replyToMessageId=question["id"]),
                     {**msg(content="A tip"), "id": "assistant-msg-abc", "replyToMessageId": "abc"}]
    sync(site, dirs)
    got = events(dirs)
    assert got[1]["reply_to"] == question["id"] and "reply_to" not in got[2]
    convo = conversation.load(inboxes={"muse": dirs.inbox}, resets_file=dirs.pins / "resets.jsonl")["muse"]
    assert convo[1]["reply_to"] == f"muse:{question['id']}"


def test_another_account_is_refused_before_anything_is_recorded(site, dirs):
    sync(site, dirs)
    site.account_id = "someone-else"
    with pytest.raises(MuseError, match="pinned to 1452962344561730"):
        sync(site, dirs)
    assert not dirs.inbox.exists()


def test_nobody_signed_in_says_how_to_sign_in(site, dirs):
    site.signed_in = False
    with pytest.raises(MuseError, match="bench muse-login"):
        sync(site, dirs)


def test_send_records_the_message_from_the_main_chat(site, dirs):
    with channel(site, dirs) as m:
        sent = m.send(MUSE, "ping")
    assert site.sent == ["ping"] and sent["text"] == "ping" and sent["is_from_me"] is True


def test_a_sent_message_that_never_shows_up_is_an_error(site, dirs, monkeypatch):
    monkeypatch.setattr(muse_module, "SENT_SECONDS", 0)
    site.shows_sent = False
    with channel(site, dirs) as m, pytest.raises(MuseError, match="didn't show up.*Muse isn't working"):
        m.send(MUSE, "ping")


def test_a_send_shown_on_the_page_counts_while_muse_ai_s_copy_lags(site, dirs):
    # 2026-11-07: Muse worked for minutes on a Housewarming prompt, and muse.ai wrote its copy only then
    site.copy_lags = True
    with channel(site, dirs) as m:
        m.sync()
        sent = m.send(MUSE, "Plan my housewarming")
        assert sent["text"] == "Plan my housewarming" and sent["is_from_me"] is True
        site.busy = True
        m.sync()
        site.finish_turn("Here's a plan.")
        m.sync()
    got = [(e["kind"], e.get("text") or e.get("action")) for e in events(dirs)]
    assert got[0] == ("new", "Plan my housewarming") and ("typing", "working") in got
    assert got[-1] == ("new", "Here's a plan.")
    assert sum(e.get("message_id") == sent["message_id"] for e in events(dirs)) >= 1  # one message, not two


def test_muse_working_is_proof_enough_that_a_send_went(site, dirs, monkeypatch):
    monkeypatch.setattr(muse_module, "SENT_SECONDS", 0)
    site.copy_lags, site.busy = True, True
    site.shown_on_page = False
    site.send = lambda text: site.sent.append(text)  # the page doesn't show it either
    with channel(site, dirs) as m:
        assert m.send(MUSE, "ping") is None  # no error, so a run keeps recording
    assert site.sent == ["ping"]


def test_a_reply_is_never_dated_before_the_message_it_follows(site, dirs):
    # muse.ai's copy stamps a reply with when Muse began its turn: once 0.6 s before the prompt it answers
    prompt = msg("user", "Who would you suggest?", when="2026-11-08T03:24:51.658Z")
    site.messages = [prompt, msg(content="Perfect — Saturday it is.", when="2026-11-08T03:24:51.032Z")]
    sync(site, dirs)
    got = events(dirs)
    assert got[1]["date"] == "2026-11-08T03:24:51.659Z"


def test_old_messages_let_go_after_48_hours_are_not_recorded_again(site, dirs):
    # 2026-11-07: the first version of first-seen times re-recorded these as new, dated that night
    site.messages = [msg(content="Tip 6", when=at(-5 * 86400)), msg("user", "old", when=at(-4 * 86400)),
                     msg(content="an old answer", when=at(-4 * 86400 + 5))]
    assert sync(site, dirs) == 3
    with channel(site, dirs) as m:
        assert m.sync() == 0 and not m.state.get("seen")


def test_a_reply_seen_live_is_dated_when_it_was_first_seen(site, dirs):
    sync(site, dirs)
    prompt = msg("user", "plan it", when=at(-120))
    site.messages += [prompt, msg(content="On it.", when=at(-119))]  # stamped when Muse began, two minutes ago
    sync(site, dirs)
    reply = events(dirs)[-1]
    assert reply["text"] == "On it." and reply["date"] >= at(-5)
    site.messages[-1]["createdAt"] = at(-100)  # muse.ai loads the chat again, with Muse's own later time
    assert sync(site, dirs) == 0  # the time first seen stands


def test_only_one_muse_agent(site, dirs):
    with pytest.raises(MuseError, match="only one Muse agent"):
        channel(site, dirs, agents=(MUSE, Agent("muse2", "muse.ai", 1, 1, channel="muse")))


def test_status_checks_without_sending_or_recording(site, dirs):
    site.messages = [msg(content="hi")]
    text = status(pin_dir=dirs.pins, site=site)
    assert f"signed in as account {ACCOUNT} (cloud computer {COMPUTER})" in text
    assert "Not pinned yet" in text and "Main chat: 1 messages" in text
    assert "Side chat: 'WhatsApp' (read-only on muse.ai; not recorded)" in text
    assert "Use Muse on WhatsApp" not in text  # that's the main chat itself
    assert "the Muse tab is showing it" in text and "Message box: found, empty" in text
    site.tab, site.path = "https://muse.ai/thread/wa", "/thread/wa"
    assert "showing another page; sending opens the main chat first" in status(pin_dir=dirs.pins, site=site)
    assert site.sent == [] and not dirs.inbox.exists()


def test_one_command_at_a_time(site, dirs):
    with channel(site, dirs), pytest.raises(MuseError, match="another bench command is using Muse"):
        channel(site, dirs).__enter__()


@pytest.mark.parametrize("raw", ["muse.ai", "https://muse.ai/", "MUSE.AI"])
def test_muse_handles(raw):
    assert normalize_handle(raw, "muse") == "muse.ai"


def test_muse_handle_must_be_muse_ai():
    with pytest.raises(ConfigError, match="muse.ai"):
        normalize_handle("1807055946647697@bot", "muse")


class FakeTab:
    def __init__(self, url, data=None):
        self.url, self.data, self.visited = url, data, []

    def evaluate(self, script, arg):
        return self.data

    def goto(self, url, wait_until=None):
        self.visited.append(url)


def test_muse_web_reads_only_from_the_muse_tab():
    other = FakeTab("https://mail.google.com/")
    mine = FakeTab("https://muse.ai/", {"signedIn": True, "main": [], "sides": []})
    web = MuseWeb(browser=SimpleNamespace(contexts=[SimpleNamespace(pages=[other, mine])]))
    assert web.read()["signedIn"] is True
    assert web.account() is None  # signed in, but no chat shown yet
    assert other.visited == [] and mine.visited == []
