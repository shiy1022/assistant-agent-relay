import io
import json
import time
import urllib.error
from datetime import timedelta
from types import SimpleNamespace

import pytest
from playwright.sync_api import TimeoutError as PlaywrightTimeout

from bench.channels import slack as slack_module
from bench.channels import slack_web
from bench.channels.slack_web import Conversation, Selectors, SlackWeb, SlackWebError
from bench.channels.slack import SlackAPI, SlackChannel, SlackError, block_text, mrkdwn, status
from bench.config import Agent, ConfigError, load_agents, normalize_handle

DOTS = Agent("dots", "U0DOT0001", 120, 1800, channel="slack")
ME, DOT, STRANGER = "U0ME00001", "U0DOT0001", "U0STRANGE"
DM, OTHER_DM = "D0DOTS001", "D0OTHER01"
SCOPES = ["chat:write", "im:history", "im:write", "users:read"]


def ts(minutes_ago: float = 1, seq: int = 0) -> str:
    return f"{int(time.time() - minutes_ago * 60)}.{seq:06d}"


class FakeSlack:
    """The parts of Slack's Web API the channel uses, with DMs held in memory."""

    def __init__(self, *, team="PJTTY", team_id="T0PJTTY01", url="https://pjtty.slack.com/", scopes=SCOPES):
        self.auth = {"team": team, "team_id": team_id, "url": url, "user": "me", "user_id": ME, "scopes": list(scopes)}
        self.users = {
            DOT: {"id": DOT, "name": "dot", "is_bot": True, "profile": {"display_name": "Dot"}},
            STRANGER: {"id": STRANGER, "name": "stranger", "profile": {}},
        }
        self.dms = {DOT: DM, STRANGER: OTHER_DM}
        self.history = {DM: [], OTHER_DM: []}  # top-level messages, any order
        self.replies = {}                       # parent ts -> replies
        self.calls = []
        self.downloads = []

    def message(self, text, *, user=DOT, at=None, **fields):
        return {"type": "message", "user": user, "text": text, "ts": at or ts(), **fields}

    def post(self, msg, dm=DM):
        self.history[dm].append(msg)
        return msg

    def reply(self, parent, msg):
        msg["thread_ts"] = parent["ts"]
        self.replies.setdefault(parent["ts"], []).append(msg)
        parent["reply_count"] = len(self.replies[parent["ts"]])
        parent["latest_reply"] = max(m["ts"] for m in self.replies[parent["ts"]])
        parent["thread_ts"] = parent["ts"]
        return msg

    def channels_called(self):
        return {params["channel"] for method, params in self.calls if "channel" in params}

    def call(self, method, **params):
        params = {k: v for k, v in params.items() if v is not None}
        self.calls.append((method, params))
        if method == "auth.test":
            return {"ok": True, **self.auth}
        if method == "users.info":
            return {"ok": True, "user": self.users[params["user"]]}
        if method == "users.list":
            return {"ok": True, "members": list(self.users.values())}
        if method == "conversations.open":
            return {"ok": True, "channel": {"id": self.dms[params["users"]]}}
        if method == "conversations.history":
            msgs = sorted(self.history[params["channel"]], key=lambda m: float(m["ts"]), reverse=True)
            if "oldest" in params:
                msgs = [m for m in msgs if float(m["ts"]) > float(params["oldest"])]
            start = int(params.get("cursor") or 0)
            page = msgs[start:start + params["limit"]]
            more = start + params["limit"] < len(msgs)
            return {"ok": True, "messages": [dict(m) for m in page], "has_more": more,
                    "response_metadata": {"next_cursor": str(start + params["limit"]) if more else ""}}
        if method == "conversations.replies":
            parent = next(m for m in self.history[params["channel"]] if m["ts"] == params["ts"])
            return {"ok": True, "messages": [dict(parent), *map(dict, self.replies.get(params["ts"], []))]}
        if method == "chat.postMessage":
            msg = self.message(params["text"], user=ME, at=ts(0, len(self.calls)))
            if "thread_ts" in params:
                parent = next(m for m in self.history[params["channel"]] if m["ts"] == params["thread_ts"])
                self.reply(parent, msg)
            else:
                self.post(msg, params["channel"])
            return {"ok": True, "ts": msg["ts"], "message": dict(msg)}
        raise AssertionError(f"unexpected call {method}")

    def download(self, url, dest):
        self.downloads.append(url)
        dest.write_bytes(b"%PDF fake")


@pytest.fixture
def fake():
    return FakeSlack()


@pytest.fixture
def dirs(tmp_path):
    return SimpleNamespace(pins=tmp_path / "pins", inbox=tmp_path / "inbox" / "slack" / "messages.jsonl")


def channel(fake, dirs, agents=(DOTS,), workspace="PJTTY", send_via="api", **kw):
    return SlackChannel(list(agents), api=fake, workspace=workspace, pin_dir=dirs.pins, inbox=dirs.inbox,
                        send_via=send_via, **kw)


def events(dirs):
    return [json.loads(line) for line in dirs.inbox.read_text().splitlines()] if dirs.inbox.exists() else []


def sync(fake, dirs, **kw):
    with channel(fake, dirs, **kw) as s:
        return s.sync()


# MARK: Only PJTTY

def test_a_token_for_another_workspace_is_refused_before_anything_else(dirs):
    fake = FakeSlack(team="Acme", team_id="T0ACME001", url="https://acme.slack.com/")
    fake.post(fake.message("private"))
    with pytest.raises(SlackError, match="is for Acme .*not PJTTY. Nothing was read or sent"):
        sync(fake, dirs)
    assert [m for m, _ in fake.calls] == ["auth.test"]
    assert not dirs.inbox.exists()
    assert not (dirs.pins / "pins.json").exists()


def test_the_workspace_is_pinned_by_id(fake, dirs):
    sync(fake, dirs)
    assert json.loads((dirs.pins / "pins.json").read_text())["team_id"] == "T0PJTTY01"
    impostor = FakeSlack(team_id="T0IMPOST1")  # also called PJTTY
    with pytest.raises(SlackError, match="pinned as team T0PJTTY01"):
        sync(impostor, dirs)
    assert [m for m, _ in impostor.calls] == ["auth.test"]


def test_the_workspace_can_be_named_by_its_address(dirs):
    sync(FakeSlack(team="PJTTY Team"), dirs, workspace="pjtty")


def test_missing_scopes_are_named(dirs):
    with pytest.raises(SlackError, match="missing these user token scopes: im:write"):
        sync(FakeSlack(scopes=["chat:write", "im:history", "users:read"]), dirs)


def test_bot_tokens_are_refused(monkeypatch):
    monkeypatch.setenv("SLACK_USER_TOKEN", "xoxb-123")
    with pytest.raises(SlackError, match="isn't a user token"):
        slack_module.user_token()


def test_missing_token_explains_how_to_add_it(monkeypatch):
    monkeypatch.delenv("SLACK_USER_TOKEN", raising=False)
    monkeypatch.setattr(slack_module, "KEYCHAIN_SERVICE", "agent-runner.test.missing-slack-token")
    with pytest.raises(SlackError, match="security add-generic-password"):
        slack_module.user_token()


# MARK: Recording

def test_first_sync_records_the_agent_dm_and_nothing_else(fake, dirs):
    fake.post(fake.message("hi", user=ME, at=ts(5)))
    fake.post(fake.message("Hi! I'm your dot.", at=ts(4)))
    fake.post(fake.message("private", user=STRANGER), dm=OTHER_DM)

    assert sync(fake, dirs) == 2
    recorded = events(dirs)
    assert [(e["kind"], e["is_from_me"], e["text"]) for e in recorded] == [
        ("new", True, "hi"), ("new", False, "Hi! I'm your dot."),
    ]
    assert recorded[1]["guid"].startswith(f"slack:{DM}:")
    assert recorded[1]["channel"] == "slack" and recorded[1]["agent"] == "dots"
    assert recorded[1]["date"].endswith("Z")
    assert fake.channels_called() == {DM}
    assert [p["users"] for m, p in fake.calls if m == "conversations.open"] == [DOT]
    assert "private" not in dirs.inbox.read_text()


def test_send_escapes_markup_and_refuses_other_agents(fake, dirs):
    other = Agent("other", STRANGER, 90, 900, channel="slack")
    with channel(fake, dirs) as s:
        s.sync()
        event = s.send(DOTS, "Plan dinner for <4 people> & book it")
        with pytest.raises(SlackError, match="isn't a Slack agent"):
            s.send(other, "hi")
        with pytest.raises(SlackError, match="empty"):
            s.send(DOTS, "  \n")
        with pytest.raises(SlackError, match="cuts messages off"):
            s.send(DOTS, "x" * 40_001)
    posted = [p for m, p in fake.calls if m == "chat.postMessage"]
    assert posted == [{"channel": DM, "text": "Plan dinner for &lt;4 people&gt; &amp; book it",
                       "unfurl_links": False, "unfurl_media": False}]
    assert event["kind"] == "new" and event["is_from_me"]
    assert event["text"] == "Plan dinner for <4 people> & book it"
    assert fake.channels_called() == {DM}


def test_a_reply_streamed_into_a_thread_is_recorded_as_it_grows(fake, dirs):
    with channel(fake, dirs) as s:
        s.sync()
        prompt = s.send(DOTS, "Find me a flight")
        parent = next(m for m in fake.history[DM] if m["ts"] == prompt["message_id"])
        reply = fake.reply(parent, fake.message("Looking", at=ts(0, 900)))
        s.sync()
        reply["text"] = "Looking at flights to SFO…"
        reply["edited"] = {"user": DOT, "ts": ts(0, 901)}
        s.sync()
        s.sync()  # nothing changed

    replies = [e for e in events(dirs) if e.get("reply_to")]
    assert [(e["kind"], e["text"]) for e in replies] == [("new", "Looking"), ("update", "Looking at flights to SFO…")]
    assert replies[0]["reply_to"] == prompt["message_id"] and not replies[0]["is_from_me"]
    assert replies[1]["date_edited"].endswith("Z")


def test_quiet_old_threads_are_not_re_read(fake, dirs):
    parent = fake.post(fake.message("old question", user=ME, at=ts(60)))
    fake.reply(parent, fake.message("old answer", at=ts(59)))
    sync(fake, dirs)
    fake.calls.clear()
    sync(fake, dirs)
    assert "conversations.replies" not in [m for m, _ in fake.calls]


def test_edits_made_while_offline_are_caught_on_sync(fake, dirs):
    msg = fake.post(fake.message("draft", at=ts(3)))
    sync(fake, dirs)
    msg["text"] = "final"
    msg["edited"] = {"user": DOT, "ts": ts(1)}
    msg["reactions"] = [{"name": "white_check_mark", "count": 1, "users": [ME]}]
    sync(fake, dirs)
    last = events(dirs)[-1]
    assert last["kind"] == "update" and last["text"] == "final"
    assert last["reactions"] == [{"emoji": ":white_check_mark:", "count": 1}]


def test_messages_that_arrive_between_syncs_are_all_recorded(fake, dirs, monkeypatch):
    monkeypatch.setattr(slack_module, "REFRESH_LIMIT", 2)
    fake.post(fake.message("first", at=ts(10)))
    sync(fake, dirs)
    for i in range(5):
        fake.post(fake.message(f"burst {i}", at=ts(5 - i)))
    sync(fake, dirs)
    assert [e["text"] for e in events(dirs)] == ["first", *(f"burst {i}" for i in range(5))]


def test_deletions_are_recorded(fake, dirs):
    keep = fake.post(fake.message("keep", at=ts(5)))
    gone = fake.post(fake.message("oops", at=ts(4)))
    parent = fake.post(fake.message("question", user=ME, at=ts(3)))
    fake.reply(parent, fake.message("answer", at=ts(2)))
    sync(fake, dirs)
    fake.history[DM].remove(gone)
    parent.update(subtype="tombstone", text="This message was deleted.")
    sync(fake, dirs)
    deleted = [e for e in events(dirs) if e["kind"] == "deleted"]
    assert sorted(e["text"] for e in deleted) == ["oops", "question"]
    assert all(e["date_deleted"] for e in deleted)
    assert keep["ts"] in {e["message_id"] for e in events(dirs)}


def test_messages_let_go_after_the_track_window_are_not_recorded_again(fake, dirs, monkeypatch):
    parent = fake.post(fake.message("question", user=ME, at=ts(3)))
    fake.reply(parent, fake.message("answer", at=ts(2)))
    sync(fake, dirs)
    before = len(events(dirs))
    monkeypatch.setattr(slack_module, "TRACK_WINDOW", timedelta(0))
    sync(fake, dirs)  # lets everything go
    sync(fake, dirs)
    assert len(events(dirs)) == before == 2


def test_blocks_buttons_previews_and_files(fake, dirs):
    fake.auth["scopes"].append("files:read")
    fake.post(fake.message(
        "fallback text",
        blocks=[
            {"type": "header", "text": {"type": "plain_text", "text": "Flight options"}},
            {"type": "section", "text": {"type": "mrkdwn", "text": "*UA 123* for <@U0ME00001>: <https://united.com/x|details> &amp; more"}},
            {"type": "rich_text", "elements": [{"type": "rich_text_list", "style": "bullet", "elements": [
                {"type": "rich_text_section", "elements": [{"type": "text", "text": "Window seat"}]},
                {"type": "rich_text_section", "elements": [{"type": "emoji", "name": "airplane", "unicode": "2708-fe0f"}]},
            ]}]},
            {"type": "actions", "elements": [
                {"type": "button", "text": {"type": "plain_text", "text": "Book"}, "style": "primary", "value": "book"},
                {"type": "button", "text": {"type": "plain_text", "text": "Open"}, "url": "https://united.com/x"},
            ]},
        ],
        attachments=[{"original_url": "https://united.com/x", "title": "United", "service_name": "united.com"}],
        files=[{"id": "F1", "name": "itinerary.pdf", "mimetype": "application/pdf", "size": 9,
                "url_private_download": "https://files.slack.com/files-pri/T1-F1/download/itinerary.pdf"}],
    ))
    sync(fake, dirs)
    event = events(dirs)[0]
    assert event["text"] == ("Flight options\n*UA 123* for @U0ME00001: details (https://united.com/x) & more\n"
                             "- Window seat\n- ✈️")
    assert event["rich"] is True
    assert event["buttons"] == [[{"text": "Book", "kind": "button", "style": "primary", "value": "book"},
                                 {"text": "Open", "kind": "button", "url": "https://united.com/x"}]]
    assert event["link_preview"] == {"url": "https://united.com/x", "title": "United", "site": "united.com"}
    assert event["attachments"][0]["name"] == "itinerary.pdf"
    assert (dirs.inbox.parent / event["attachments"][0]["path"]).read_bytes() == b"%PDF fake"
    assert "Book" not in [m for m, _ in fake.calls]  # buttons are recorded, never pressed


def test_files_are_listed_but_not_downloaded_without_files_read(fake, dirs):
    fake.post(fake.message("here", files=[{"id": "F1", "name": "a.png", "url_private_download": "https://files.slack.com/a.png"}]))
    sync(fake, dirs)
    assert events(dirs)[0]["attachments"] == [{"name": "a.png"}]
    assert fake.downloads == []


@pytest.mark.parametrize("raw, plain", [
    ("<https://a.com>", "https://a.com"),
    ("<https://a.com|https://a.com>", "https://a.com"),
    ("<mailto:me@a.com|me@a.com>", "me@a.com"),
    ("<#C123|general> <!here> <@U1|sam>", "#general @here @sam"),
    ("1 &lt; 2 &amp;&amp; 3 &gt; 2", "1 < 2 && 3 > 2"),
])
def test_mrkdwn_to_plain_text(raw, plain):
    assert mrkdwn(raw) == plain


def test_legacy_attachments_and_markdown_blocks():
    assert block_text([{"type": "markdown", "text": "**Done.** Booked."}, {"type": "divider"}]) == "**Done.** Booked.\n—"
    msg = {"text": "", "attachments": [{"pretext": "Heads up", "text": "Your order shipped",
                                        "fields": [{"title": "ETA", "value": "Fri"}]}]}
    assert slack_module.message_text(msg) == "Heads up\nYour order shipped\nETA: Fri"


# MARK: Members, polling and the lock

def test_agents_can_be_named_and_are_pinned_to_their_id(fake, dirs):
    named = Agent("dots", "@dot", 120, 1800, channel="slack")
    with channel(fake, dirs, agents=[named]):
        pass
    pins = json.loads((dirs.pins / "pins.json").read_text())
    assert pins["agents"]["dots"] | {"name": None} == {"handle": "@dot", "me": ME, "user_id": DOT, "name": None,
                                                       "label": "Dot", "bot": True, "dm": DM}
    fake.users["U0DOTTWO1"] = {"id": "U0DOTTWO1", "name": "dot-two", "profile": {"display_name": "dot"}}
    fake.calls.clear()
    with channel(fake, dirs, agents=[named]):
        pass  # pinned: not looked up again
    assert "users.list" not in [m for m, _ in fake.calls]


def test_ambiguous_names_ask_for_the_member_id(fake, dirs):
    fake.users["U0DOTTWO1"] = {"id": "U0DOTTWO1", "name": "dot", "profile": {}}
    with pytest.raises(SlackError, match="2 members are named @dot"):
        with channel(fake, dirs, agents=[Agent("dots", "@dot", 120, 1800, channel="slack")]):
            pass


def test_listen_polls_until_the_time_is_up(fake, dirs):
    clock = SimpleNamespace(now=0.0)

    def sleep(seconds):
        clock.now += seconds
        if clock.now >= 10 and not fake.history[DM]:
            fake.post(fake.message("done"))

    with channel(fake, dirs, sleep=sleep, clock=lambda: clock.now) as s:
        s.listen(30, poll_seconds=5)
    assert clock.now == 30
    assert [e["text"] for e in events(dirs)] == ["done"]
    assert sum(m == "conversations.history" for m, _ in fake.calls) == 6


def test_one_command_at_a_time(fake, dirs):
    with channel(fake, dirs):
        with pytest.raises(SlackError, match="another bench command"):
            with channel(FakeSlack(), dirs):
                pass


def test_status_checks_without_sending_or_recording(fake, dirs):
    text = status([DOTS], api=fake, workspace="PJTTY", pin_dir=dirs.pins, send_via="api")
    assert "PJTTY (https://pjtty.slack.com/), team T0PJTTY01" in text
    assert "dots: U0DOT0001 → dot (U0DOT0001, bot)" in text
    assert "attachments are recorded by name only" in text
    assert {m for m, _ in fake.calls} == {"auth.test", "users.info"}
    assert not dirs.inbox.exists()


# MARK: Sending through the web client

class FakeWeb:
    """Stands in for SlackWeb: "typing" a message makes it appear in the DM as a typed one."""

    def __init__(self, fake, *, shows_up=True):
        self.fake, self.shows_up, self.sent, self.checked = fake, shows_up, [], []

    def send(self, conversation, text):
        self.sent.append((conversation, text))
        if self.shows_up:
            self.fake.post(self.fake.message(text, user=ME, at=ts(0, 500), client_msg_id="c1"))
            self.fake.post(self.fake.message("sent with the API", user=ME, at=ts(0, 501), bot_id="B0RUNNER1", app_id="A0RUNNER1"))

    def check(self, conversation):
        self.checked.append(conversation)
        return "signed in; the DM is open"


PJTTY_DM = Conversation(team_id="T0PJTTY01", team="PJTTY", channel_id=DM, label="Dot")


def test_web_sending_types_the_message_and_records_it_from_the_api(fake, dirs):
    web = FakeWeb(fake)
    with channel(fake, dirs, send_via="web", web=web) as s:
        s.sync()
        event = s.send(DOTS, "Plan dinner for 4")
        with pytest.raises(SlackError, match="thread"):
            s.send(DOTS, "hi", thread=event["message_id"])
    assert web.sent == [(PJTTY_DM, "Plan dinner for 4")]
    assert "chat.postMessage" not in [m for m, _ in fake.calls]
    assert event["kind"] == "new" and event["is_from_me"] and event["text"] == "Plan dinner for 4"
    assert [e["text"] for e in events(dirs)] == ["Plan dinner for 4"]  # not the app-sent one


def test_a_typed_message_that_never_shows_up_is_an_error(fake, dirs):
    clock = SimpleNamespace(now=0.0)

    def sleep(seconds):
        clock.now += seconds

    with channel(fake, dirs, send_via="web", web=FakeWeb(fake, shows_up=False), sleep=sleep, clock=lambda: clock.now) as s:
        with pytest.raises(SlackError, match="didn't show up through the API within 30s"):
            s.send(DOTS, "hello")
    assert clock.now >= 30


def test_status_checks_the_web_client_without_typing(fake, dirs):
    web = FakeWeb(fake)
    with channel(fake, dirs):
        pass  # pins the DM
    text = status([DOTS], api=fake, workspace="PJTTY", pin_dir=dirs.pins, send_via="web", web=web)
    assert "Sending: typed into the web client" in text
    assert "web client: signed in; the DM is open" in text
    assert web.checked == [PJTTY_DM] and web.sent == []


class FakeKeyboard:
    def __init__(self, page):
        self.page = page

    def insert_text(self, text):
        self.page.log.append(("insert", text))
        self.page.typed += text

    def press(self, key):
        self.page.log.append(("press", key))
        if key == "Shift+Enter":
            self.page.typed += "\n"
        elif key == "Backspace" and self.page.selected:
            self.page.typed, self.page.selected = "", False
        elif key == "Meta+A":
            self.page.selected = True


class FakeElement:
    def __init__(self, page, selector):
        self.page, self.selector = page, selector

    @property
    def first(self):
        return self

    def wait_for(self, timeout):
        if not self.page.has_box():
            raise PlaywrightTimeout("no message box")

    def count(self):
        return self.page.boxes if self.page.has_box() else 0

    def get_attribute(self, name):
        return self.page.aria

    def evaluate(self, js):
        if js == slack_web.is_blank_js(Selectors()):
            return not self.page.typed
        if js == slack_web.LINES_JS:
            return self.page.typed.count("\n") + 1

    def click(self):
        self.page.log.append(("click", "send" if self.selector == Selectors().send else "box"))
        if self.selector == Selectors().send:
            if self.page.send_fails:
                raise RuntimeError("Send button went away")
            self.page.sent.append(self.page.typed)
            self.page.typed = ""


class FakePage:
    """A Slack tab. It shows the message box on a PJTTY conversation once signed in."""

    def __init__(self, *, url=PJTTY_DM.url, title="Dot (DM) - PJTTY - Slack", aria="Message to Dot",
                 boxes=1, typed="", signed_in=True):
        self.url, self._title, self.aria, self.boxes, self.typed, self.signed_in = url, title, aria, boxes, typed, signed_in
        self.keyboard, self.log, self.sent, self.selected, self.send_fails = FakeKeyboard(self), [], [], False, False
        self.visited, self.fronted = [], False
        self.user_signs_in = False  # whether the person signs in while sign_in() waits

    def has_box(self):
        return self.signed_in and self.url.startswith("https://app.slack.com/client/")

    def title(self):
        return self._title

    def locator(self, selector):
        return FakeElement(self, selector)

    def goto(self, url, wait_until=None):
        self.visited.append(url)
        self.url = url if self.signed_in or not url.startswith("https://app.slack.com/") else "https://pjtty.slack.com/"

    def wait_for_url(self, matches, timeout):
        if self.user_signs_in:
            self.signed_in, self.url = True, PJTTY_DM.workspace_url + "C0GENERAL"
        if not matches(self.url):
            raise PlaywrightTimeout("still signing in")

    def bring_to_front(self):
        self.fronted = True

    def wait_for_function(self, js, timeout):
        pass


class FakeBrowser:
    def __init__(self, *pages, signed_in=True):
        self.signed_in, self.new_pages = signed_in, []
        context = SimpleNamespace(pages=list(pages), new_page=self.new_page)
        self.contexts = [context]

    def new_page(self):
        page = FakePage(url="about:blank", signed_in=self.signed_in)
        self.new_pages.append(page)
        self.contexts[0].pages.append(page)
        return page


def test_typing_inserts_lines_and_clicks_send():
    page = FakePage()
    slack_web.type_and_send(page, "Plan dinner 🍕\n\n- for 4")
    assert page.sent == ["Plan dinner 🍕\n\n- for 4"]
    assert page.log == [("click", "box"), ("insert", "Plan dinner 🍕"), ("press", "Shift+Enter"),
                        ("press", "Shift+Enter"), ("insert", "- for 4"), ("click", "send")]
    assert ("press", "Enter") not in page.log  # Enter alone would send early


def test_a_draft_in_the_box_is_never_sent():
    page = FakePage(typed="half-written reply")
    with pytest.raises(SlackWebError, match="already has a draft"):
        slack_web.type_and_send(page, "hello")
    assert page.sent == [] and page.typed == "half-written reply"


def test_a_failed_send_leaves_no_draft_behind():
    page = FakePage()
    page.send_fails = True
    with pytest.raises(RuntimeError):
        slack_web.type_and_send(page, "hello")
    assert page.typed == "" and page.sent == []


@pytest.mark.parametrize("page, problem", [
    (FakePage(url="https://app.slack.com/client/T0PJTTY01/C0GENERAL"), "the tab is at"),
    (FakePage(url="https://app.slack.com/client/T0ACME001/" + DM), "the tab is at"),
    (FakePage(title="Dot (DM) - Acme - Slack"), "page title"),
    (FakePage(title="general (Channel) - PJTTY - Slack", aria="Message to general"), "labelled 'Message to general'"),
    (FakePage(boxes=2), "2 message boxes"),
])
def test_typing_is_refused_anywhere_but_the_conversation(page, problem):
    with pytest.raises(SlackWebError, match=f"not typing anything: .*{problem}"):
        slack_web.check_page(page, PJTTY_DM)


def test_slack_web_reuses_the_conversations_tab():
    other = FakePage(url="https://hark.com/chat")
    tab = FakePage()
    browser = FakeBrowser(other, tab)
    with SlackWeb(browser=browser) as web:
        assert not web.check(PJTTY_DM).draft
        web.send(PJTTY_DM, "one")
        web.send(PJTTY_DM, "two")
    assert tab.sent == ["one", "two"] and tab.visited == []
    assert other.log == [] and other.visited == [] and browser.new_pages == []


def test_slack_web_moves_a_workspace_tab_to_the_conversation_and_never_borrows_others():
    elsewhere = FakePage(url="https://app.slack.com/client/T0ACME001/C0RANDOM")
    channel_tab = FakePage(url="https://app.slack.com/client/T0PJTTY01/C0GENERAL")
    with SlackWeb(browser=FakeBrowser(elsewhere, channel_tab)) as web:
        web.send(PJTTY_DM, "hi")
    assert channel_tab.visited == [PJTTY_DM.url] and channel_tab.sent == ["hi"]
    assert elsewhere.visited == [] and elsewhere.log == []

    browser = FakeBrowser(elsewhere)
    with SlackWeb(browser=browser) as web:
        web.check(PJTTY_DM)
    assert browser.new_pages[0].visited == [PJTTY_DM.url] and elsewhere.visited == []


def test_slack_web_must_be_used_as_a_context_manager():
    with pytest.raises(SlackWebError, match="context manager"):
        SlackWeb().send(PJTTY_DM, "hi")


def test_sign_in_waits_for_you_and_knows_when_you_already_are():
    with SlackWeb(browser=FakeBrowser(FakePage())) as web:
        assert web.sign_in(PJTTY_DM, "https://pjtty.slack.com/") is True

    browser = FakeBrowser(signed_in=False)
    with SlackWeb(browser=browser, load_seconds=0) as web:
        with pytest.raises(SlackWebError, match="not signed in to PJTTY"):
            web.sign_in(PJTTY_DM, "https://pjtty.slack.com/", wait_seconds=0)
    page = browser.new_pages[0]
    assert page.visited == [PJTTY_DM.url, "https://pjtty.slack.com/"] and page.fronted

    page.user_signs_in = True  # the next try reuses the sign-in tab, and this time you sign in
    with SlackWeb(browser=browser, load_seconds=0) as web:
        assert web.sign_in(PJTTY_DM, "https://pjtty.slack.com/") is False
    assert len(browser.new_pages) == 1 and page.has_box()


def test_sign_in_needs_the_workspace_pinned_first(dirs):
    with pytest.raises(SlackError, match="pinned"):
        slack_module.sign_in([DOTS], pin_dir=dirs.pins)


def test_sign_in_uses_the_pinned_workspace(fake, dirs):
    with channel(fake, dirs):
        pass  # pins the workspace and the DM
    seen = []
    web = SimpleNamespace(sign_in=lambda conversation, url: seen.append((conversation, url)) or True)
    assert slack_module.sign_in([DOTS], pin_dir=dirs.pins, web=web) == "Already signed in to PJTTY in the dedicated Chrome."
    assert seen == [(PJTTY_DM, "https://pjtty.slack.com/")]


# MARK: HTTP

class Response(io.BytesIO):
    def __init__(self, body: dict, headers: dict | None = None):
        super().__init__(json.dumps(body).encode())
        self.headers = headers or {}


def test_rate_limited_calls_wait_and_retry():
    waits, attempts = [], []

    def open_(request, timeout):
        attempts.append(request.full_url)
        if len(attempts) == 1:
            raise urllib.error.HTTPError(request.full_url, 429, "ratelimited", {"Retry-After": "7"}, None)
        return Response({"ok": True, "team": "PJTTY"}, {"x-oauth-scopes": "chat:write, im:history"})

    api = SlackAPI("xoxp-test", sleep=waits.append)
    api.opener = SimpleNamespace(open=open_)
    assert api.call("auth.test")["scopes"] == ["chat:write", "im:history"]
    assert waits == [7.0]
    assert attempts == ["https://slack.com/api/auth.test"] * 2


def test_api_errors_explain_the_fix():
    api = SlackAPI("xoxp-test")
    api.opener = SimpleNamespace(open=lambda r, timeout: Response({"ok": False, "error": "missing_scope", "needed": "im:write"}))
    with pytest.raises(slack_module.SlackAPIError, match="missing the im:write scope"):
        api.call("conversations.open", users=DOT)


def test_the_token_never_leaves_slack(tmp_path):
    api = SlackAPI("xoxp-test")
    with pytest.raises(slack_module.SlackNetworkError, match="isn't Slack"):
        api.download("https://evil.example/f.pdf", tmp_path / "f.pdf")
    with pytest.raises(slack_module.SlackNetworkError, match="not following"):
        slack_module.SlackOnly().redirect_request(None, None, 302, "Found", {}, "https://slack.com.evil.example/x")
    assert slack_module.is_slack_url("https://files.slack.com/files-pri/x")
    assert not slack_module.is_slack_url("http://files.slack.com/x")


# MARK: Config

@pytest.mark.parametrize("raw, handle", [("U07ABCDEF", "U07ABCDEF"), ("@Dot", "@dot"), ("My Dot", "@my dot"), ("umbrella", "@umbrella")])
def test_slack_handles(raw, handle):
    assert normalize_handle(raw, "slack") == handle


def test_slack_send_via(tmp_path):
    from bench.config import slack_send_via
    path = tmp_path / "agents.yaml"
    path.write_text("slack:\n  workspace: PJTTY\n")
    assert slack_send_via(path) == "api"
    path.write_text("slack:\n  workspace: PJTTY\n  send_via: carrier-pigeon\n")
    with pytest.raises(ConfigError, match="must be api or web"):
        slack_send_via(path)


def test_slack_agents_need_the_workspace(tmp_path):
    path = tmp_path / "agents.yaml"
    path.write_text("agents:\n  dots:\n    channel: slack\n    handle: U07ABCDEF\n")
    with pytest.raises(ConfigError, match="set slack: workspace:"):
        load_agents(path)
    path.write_text("slack:\n  workspace: PJTTY\nagents:\n  dots:\n    channel: slack\n    handle: U07ABCDEF\n")
    assert load_agents(path)["dots"].channel == "slack"
