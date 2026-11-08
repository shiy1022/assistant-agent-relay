import itertools
import json
import time
from types import SimpleNamespace

import pytest

from bench import conversation
from bench.channels import wajo as wajo_module
from bench.channels.wajo import (HOME, ORIGIN, WajoChannel, WajoError, WajoWeb, chat_url, choose_thread, new_chat,
                                 status)
from bench.config import Agent, ConfigError, normalize_handle

WAJO = Agent("wajo", "wajo.ai", 90, 900, channel="wajo")
USER = {"sub": "sub-me", "email": "me@example.com", "planTier": "micro"}
ORG = "org-1"
ids = itertools.count(1)


def now_ms(offset: float = 0) -> int:
    return int((time.time() + offset) * 1000)


def msg(role="assistant", content="", at=None, **fields) -> dict:
    return {"id": f"m{next(ids):04d}", "role": role, "content": content, "timestampMs": at or now_ms(), **fields}


class FakeSite:
    """wajo.ai as WajoChannel sees it: one account, its chats in memory, newest activity first."""

    def __init__(self, *, user=USER):
        self.user = user
        self.chat_list: list[dict] = []
        self.threads: dict[str, list[dict]] = {}
        self.runs: dict[str, bool] = {}
        self.sent, self.sent_to, self.calls = [], [], []
        self.box, self.tab = "", HOME
        self.shows_sent = True
        self.new_ids = (f"new-chat-{i}" for i in itertools.count(1))

    def chat(self, chat_id, title=None, channel="SESSION_CHANNEL_UNSPECIFIED", *messages):
        self.chat_list.insert(0, {"id": chat_id, "title": title, "channel": channel, "agentHandle": "fo",
                                  "ownerIsCaller": True, "latestRunStatus": "completed", "lastActivityMs": 0})
        self.threads[chat_id] = []
        self.add(chat_id, *messages)

    def add(self, chat_id, *messages):
        self.threads[chat_id] += messages
        c = next(c for c in self.chat_list if c["id"] == chat_id)
        c["lastActivityMs"] = max([c["lastActivityMs"]] + [m["timestampMs"] for m in messages])

    def session(self):
        self.calls.append("session")
        return self.user

    def org(self):
        return ORG

    def chats(self, org):
        assert org == ORG
        return sorted(self.chat_list, key=lambda c: -c["lastActivityMs"])

    def messages(self, chat_id, org, page_size=6):
        self.calls.append(("messages", chat_id))
        busy = self.runs.get(chat_id, False)
        return {"messages": [dict(m) for m in self.threads[chat_id]], "activeRun": busy,
                "latestRunStatus": "running" if busy else "completed"}

    def draft(self):
        return self.box

    def tab_url(self):
        return self.tab

    def send(self, text, url=HOME):
        self.sent.append(text)
        self.sent_to.append(url)
        chat_id = next(self.new_ids) if url == HOME else url.rsplit("/", 1)[-1]
        if url == HOME:
            self.chat(chat_id, None)
        self.tab = chat_url(chat_id)
        if self.shows_sent:
            self.add(chat_id, msg("user", text))
        return self.tab


@pytest.fixture
def site():
    return FakeSite()


@pytest.fixture
def dirs(tmp_path):
    return SimpleNamespace(pins=tmp_path / "pins", inbox=tmp_path / "inbox" / "wajo" / "messages.jsonl")


def channel(site, dirs, agents=(WAJO,), **kw):
    return WajoChannel(list(agents), site=site, pin_dir=dirs.pins, inbox=dirs.inbox, sleep=lambda s: None, **kw)


def events(dirs):
    return [json.loads(line) for line in dirs.inbox.read_text().splitlines()] if dirs.inbox.exists() else []


def sync(site, dirs):
    with channel(site, dirs) as w:
        return w.sync()


# MARK: One account, recorded from when it's pinned

def test_first_sync_pins_the_account_and_records_nothing_from_before(site, dirs):
    site.chat("tg", "Trip", "SESSION_CHANNEL_TELEGRAM", msg("user", "an old Telegram message", now_ms(-3600)),
              msg(content="an old answer", at=now_ms(-3590)))
    assert sync(site, dirs) == 0
    pins = json.loads((dirs.pins / "pins.json").read_text())
    assert {k: pins[k] for k in ("sub", "email", "org_id")} == {"sub": "sub-me", "email": "me@example.com", "org_id": ORG}
    assert ("messages", "tg") not in site.calls  # a chat with nothing since the pin isn't even read


def test_everything_after_the_pin_is_recorded_in_every_chat(site, dirs):
    site.chat("tg", "Trip", "SESSION_CHANNEL_TELEGRAM", msg("user", "old", now_ms(-3600)))
    sync(site, dirs)
    site.add("tg", msg("user", "from my phone", source="inbound_photon"),
             msg(content="", toolCalls=[{"name": "web_search", "status": "done"}]),  # a work step, no text
             msg(content="an answer", runId="run-1", toolCalls=[{"name": "web_search", "status": "done"}],
                 producedArtifactIds=["page-1"]))
    site.chat("web", "Housewarming", "SESSION_CHANNEL_UNSPECIFIED", msg("user", "typed on wajo.ai", now_ms(1)))
    assert sync(site, dirs) == 3
    got = events(dirs)
    assert [e["text"] for e in got] == ["from my phone", "an answer", "typed on wajo.ai"]
    assert [e["is_from_me"] for e in got] == [True, False, True]
    assert [(e["chat"], e["chat_channel"]) for e in got] == [("tg", "telegram"), ("tg", "telegram"), ("web", "web")]
    assert got[0]["source"] == "inbound_photon"
    assert got[1]["tools"] == [{"name": "web_search", "status": "done"}]
    assert got[1]["pages"] == [f"{ORIGIN}/d/page-1"]
    assert all(e["channel"] == "wajo" and e["guid"] == f"wajo:{e['message_id']}" for e in got)


def test_only_chats_that_changed_are_read_again(site, dirs):
    site.chat("a", "A", "SESSION_CHANNEL_UNSPECIFIED")
    site.chat("b", "B", "SESSION_CHANNEL_UNSPECIFIED")
    sync(site, dirs)
    site.add("a", msg("user", "hi"))
    site.add("b", msg("user", "yo"))
    sync(site, dirs)
    site.calls.clear()
    site.add("b", msg(content="hello", at=now_ms(1)))
    assert sync(site, dirs) == 1
    assert [c for c in site.calls if c != "session"] == [("messages", "b")]


def test_an_edited_message_is_recorded_again_as_an_update(site, dirs):
    site.chat("a", "A", "SESSION_CHANNEL_UNSPECIFIED")
    sync(site, dirs)
    m = msg(content="draft")
    site.add("a", m)
    sync(site, dirs)
    m["content"] = "final"
    site.add("a")  # nothing new, but the list says the chat moved
    next(c for c in site.chat_list if c["id"] == "a")["lastActivityMs"] += 1
    sync(site, dirs)
    assert [(e["kind"], e["text"]) for e in events(dirs)] == [("new", "draft"), ("update", "final")]


def test_a_running_reply_shows_as_working_and_is_read_until_it_ends(site, dirs):
    site.chat("a", "A", "SESSION_CHANNEL_UNSPECIFIED")
    sync(site, dirs)
    site.add("a", msg("user", "plan it"))
    site.runs["a"] = True
    with channel(site, dirs) as w:
        w.sync()
        w.state["working"].clear()
        w.sync()  # the list hasn't changed, but Fo is still working
        site.runs["a"] = False
        site.add("a", msg(content="done"))
        w.sync()
    kinds = [(e["kind"], e.get("action") or e.get("text")) for e in events(dirs)]
    assert kinds == [("new", "plan it"), ("typing", "working"), ("typing", "working"), ("new", "done")]


def test_another_account_is_refused_before_anything_is_read(site, dirs):
    sync(site, dirs)
    site.user = {"sub": "someone-else", "email": "else@example.com"}
    site.calls.clear()
    with pytest.raises(WajoError, match="pinned to me@example.com"):
        sync(site, dirs)
    assert site.calls == ["session"]


def test_nobody_signed_in_says_how_to_sign_in(site, dirs):
    site.user = None
    with pytest.raises(WajoError, match="bench wajo-login"):
        sync(site, dirs)


def test_only_one_wajo_agent(site, dirs):
    with pytest.raises(WajoError, match="only one Wajo agent"):
        channel(site, dirs, agents=(WAJO, Agent("fo2", "wajo.ai", 1, 1, channel="wajo")))


# MARK: Sending in the kept chat

def test_the_first_send_starts_a_chat_and_later_sends_continue_it(site, dirs):
    with channel(site, dirs) as w:
        w.sync()
        sent = w.send(WAJO, "ping")
        w.send(WAJO, "and again")
    assert sent["text"] == "ping" and sent["chat"] == "new-chat-1"
    assert site.sent_to == [HOME, chat_url("new-chat-1")]
    assert json.loads((dirs.pins / "pins.json").read_text())["send_to"] == "new-chat-1"
    assert [e["text"] for e in events(dirs)] == ["ping", "and again"]


def test_a_chat_chosen_with_wajo_thread_is_sent_to(site, dirs):
    site.chat("tg", "History Travel Interests", "SESSION_CHANNEL_TELEGRAM", msg("user", "old", now_ms(-60)))
    site.chat("web", "Untitled chat", "SESSION_CHANNEL_UNSPECIFIED")
    listing = choose_thread("history travel", pin_dir=dirs.pins, site=site)
    assert '"History Travel Interests" (tg, from telegram)  ← bench send and bench run type here' in listing
    with channel(site, dirs) as w:
        w.send(WAJO, "hello")
    assert site.sent_to == [chat_url("tg")]
    assert "No chat is kept yet" in choose_thread("new", pin_dir=dirs.pins, site=site)
    with pytest.raises(WajoError, match="no chat matches"):
        choose_thread("nothing like it", pin_dir=dirs.pins, site=site)


def test_send_refuses_a_kept_chat_that_is_gone(site, dirs):
    site.chat("web", "A", "SESSION_CHANNEL_UNSPECIFIED")
    choose_thread("web", pin_dir=dirs.pins, site=site)
    site.chat_list.clear()
    with channel(site, dirs) as w, pytest.raises(WajoError, match="is gone"):
        w.send(WAJO, "hello")
    assert site.sent == []


def test_a_sent_message_that_never_shows_up_is_an_error(site, dirs, monkeypatch):
    monkeypatch.setattr(wajo_module, "SENT_SECONDS", 0)
    site.shows_sent = False
    with channel(site, dirs) as w, pytest.raises(WajoError, match="didn't show up"):
        w.send(WAJO, "hello")


def test_new_chat_is_where_the_tab_went_or_the_newest_active_one():
    from datetime import UTC, datetime
    assert new_chat("https://wajo.ai/fo/c/abc?x=1", [], datetime.now(UTC)) == "abc"
    chats = [{"id": "old", "lastActivityMs": now_ms(-600)}, {"id": "fresh", "lastActivityMs": now_ms(1)}]
    assert new_chat(HOME, chats, datetime.now(UTC)) == "fresh"
    assert new_chat(HOME, chats[:1], datetime.now(UTC)) is None


# MARK: Records, status and config

def test_wajo_messages_export_with_their_chat_and_pages(site, dirs, tmp_path):
    site.chat("a", "A", "SESSION_CHANNEL_UNSPECIFIED")
    sync(site, dirs)
    site.add("a", msg("user", "plan it"), msg(content="here: https://wajo.ai/d/page-1", producedArtifactIds=["page-2"]))
    sync(site, dirs)
    convo = conversation.load(inboxes={"wajo": dirs.inbox}, resets_file=tmp_path / "resets.jsonl")["wajo"]
    assert [m["thread"] for m in convo] == ["wajo:chat:a", "wajo:chat:a"]
    assert convo[1]["links"] == ["https://wajo.ai/d/page-1", "https://wajo.ai/d/page-2"]
    assert convo[0]["role"] == "user" and convo[1]["role"] == "agent"


def test_status_checks_without_sending_or_recording(site, dirs):
    site.chat("a", "Housewarming", "SESSION_CHANNEL_UNSPECIFIED", msg("user", "x"))
    text = status(pin_dir=dirs.pins, site=site)
    assert "signed in as me@example.com" in text and "Not pinned yet" in text
    assert "Sending to: a new chat from wajo.ai/fo" in text and "Message box: found, empty" in text
    choose_thread("a", pin_dir=dirs.pins, site=site)
    site.box = "half"
    text = status(pin_dir=dirs.pins, site=site)
    assert '"Housewarming" (a, from web) (idle; the Wajo tab is showing another page' in text
    assert "has text in it, so sending would refuse" in text
    assert site.sent == [] and not dirs.inbox.exists()


def test_one_command_at_a_time(site, dirs):
    with channel(site, dirs), pytest.raises(WajoError, match="another bench command is using Wajo"):
        channel(site, dirs).__enter__()


@pytest.mark.parametrize("raw", ["wajo.ai", "https://wajo.ai/", "WAJO.AI", "www.wajo.ai"])
def test_wajo_handles(raw):
    assert normalize_handle(raw, "wajo") == "wajo.ai"


def test_wajo_handle_must_be_wajo_ai():
    with pytest.raises(ConfigError, match="wajo.ai"):
        normalize_handle("@hey_fo_bot", "wajo")


# MARK: wajo.ai's server functions, from inside the tab

class FakeTab:
    url = "https://wajo.ai/fo"

    def __init__(self, answers):
        self.answers, self.called = answers, []

    def evaluate(self, script, arg):
        self.called.append(arg["path"])
        return self.answers.pop(0)


def web_with(tab, tmp_path):
    web = WajoWeb(browser=SimpleNamespace(contexts=[SimpleNamespace(pages=[tab])]),
                  functions_path=tmp_path / "functions.json")
    web._sub = "sub-me"
    return web


def answer(result):
    from bench.channels.seroval import encode
    return {"status": 200, "text": json.dumps(encode({"result": result}))}


def test_server_functions_are_called_from_the_wajo_tab(tmp_path):
    tab = FakeTab([answer({"workspaces": [], "defaultOrgId": "org-9"})])
    assert web_with(tab, tmp_path).org() == "org-9"
    assert tab.called == [f"/_serverFn/{wajo_module.FUNCTIONS.workspaces}"]


def test_a_moved_function_is_learned_again_once(tmp_path, monkeypatch):
    tab = FakeTab([{"status": 500, "text": "{}"}, answer({"workspaces": [], "defaultOrgId": "org-9"})])
    web = web_with(tab, tmp_path)
    learned = []

    def learn(self, seconds=20):
        learned.append(True)
        self._learned = True
        self.functions = wajo_module.replace(self.functions, workspaces="new-id")
    monkeypatch.setattr(WajoWeb, "learn", learn)
    assert web.org() == "org-9"
    assert learned == [True] and tab.called[-1] == "/_serverFn/new-id"


def test_signed_out_answers_say_how_to_sign_in(tmp_path):
    with pytest.raises(WajoError, match="bench wajo-login"):
        web_with(FakeTab([{"status": 401, "text": ""}]), tmp_path).org()


def test_only_wajo_tabs_are_read(tmp_path):
    tab = FakeTab([])
    tab.url = "https://mail.google.com/"
    web = WajoWeb(browser=SimpleNamespace(contexts=[SimpleNamespace(pages=[tab], new_page=lambda: tab)]),
                  functions_path=None)
    tab.goto = lambda url, wait_until=None: None  # opening wajo.ai in a new tab "fails": it stays off wajo.ai
    with pytest.raises(WajoError, match="not https://wajo.ai"):
        web.call("session", with_context=False)
    assert tab.called == []
