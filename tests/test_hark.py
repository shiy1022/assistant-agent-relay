import itertools
import json
import time
from types import SimpleNamespace

import pytest
from playwright.sync_api import TimeoutError as PlaywrightTimeout

from bench import conversation
from bench.channels import hark as hark_module
from bench.channels.hark import (CHAT_URL, FETCH_JS, ORIGIN, HarkChannel, HarkError, HarkNotFound, HarkWeb, Selectors,
                                 choose_thread, status, type_and_send)
from bench.config import Agent, ConfigError, normalize_handle

HARK = Agent("hark", "hark.com", 180, 1800, channel="hark")
USER = {"id": "u-me", "email": "me@example.com", "name": "Me"}
MAIN, PROJECT, OLD = "conv-main", "conv-project", "conv-old-project"
PROJECT_URL = f"{ORIGIN}/project/{PROJECT}"
ids = itertools.count(1)


def msg(role="assistant", content="", **fields) -> dict:
    return {"id": f"m{next(ids):04d}", "role": role, "content": content, "source": "web",
            "timestamp": int(time.time() * 1000), **fields}


class FakeSite:
    """hark.com as HarkChannel sees it: one account, its threads in memory, `page_size` messages per history page."""

    def __init__(self, *, user=USER, page_size=50):
        self.user, self.page_size = user, page_size
        self.threads = {MAIN: [], PROJECT: [], OLD: []}
        # The API lists the newer Project first; the main thread is the one made at sign-up, with no title.
        self.listed = [{"id": PROJECT, "title": "task-travel logistics", "createdAt": "2026-10-25T08:00:00.000Z",
                        "archivedAt": None},
                       {"id": OLD, "title": "old errands", "createdAt": "2026-10-25T07:00:00.000Z",
                        "archivedAt": "2026-10-25T07:30:00.000Z"},
                       {"id": MAIN, "title": None, "createdAt": "2026-10-25T06:39:19.736Z", "archivedAt": None}]
        self.windows, self.asks = {}, {}  # the main thread's
        self.box, self.sent, self.sent_to, self.calls = "", [], [], []
        self.tab = CHAT_URL
        self.shows_sent = True  # whether a sent message shows up in the thread

    def add(self, *messages, thread=MAIN) -> list[dict]:
        self.threads[thread] += messages
        return list(messages)

    def session(self):
        self.calls.append("session")
        return self.user

    def conversations(self):
        return self.listed

    def history(self, cid, *, after=None, before=None):
        self.calls.append(("history", cid, after, before and before["id"]))
        thread = self.threads[cid]
        position = {m["id"]: i for i, m in enumerate(thread)}
        if after and after not in position:
            raise HarkNotFound("hark.com answered /api/messages/history with HTTP 404: {\"error\":\"message not found\"}")
        if after:
            page = thread[position[after] + 1:][:self.page_size]
        elif before:
            page = thread[:position[before["id"]]][-self.page_size:]
        else:
            page = thread[-self.page_size:]
        start = position[page[0]["id"]] if page else len(thread)
        return {"shape": "records", "log": [{"id": m["id"], "kind": "message"} for m in page],
                "messages": {m["id"]: dict(m) for m in page},
                "windows": dict(self.windows) if cid == MAIN else {}, "asks": dict(self.asks) if cid == MAIN else {},
                "older": {"hasMore": start > 0}, "newer": {"hasMore": False}}

    def meter(self):
        return {"dailyUsed": 62822, "dailyLimit": 8000000, "poolUsed": 100613, "poolLimit": 40000000,
                "poolResetsAt": "2026-11-01T00:00:00Z"}

    def draft(self):
        return self.box

    def tab_url(self):
        return self.tab

    def send(self, text, url=CHAT_URL):
        self.sent.append(text)
        self.sent_to.append(url)
        self.tab = url
        if self.shows_sent:
            self.add(msg("user", text), thread=MAIN if url == CHAT_URL else url.rsplit("/", 1)[-1])


@pytest.fixture
def site():
    return FakeSite()


@pytest.fixture
def dirs(tmp_path):
    return SimpleNamespace(pins=tmp_path / "pins", inbox=tmp_path / "inbox" / "hark" / "messages.jsonl", tmp=tmp_path)


def channel(site, dirs, agents=(HARK,), **kw):
    return HarkChannel(list(agents), site=site, pin_dir=dirs.pins, inbox=dirs.inbox, **kw)


def events(dirs):
    return [json.loads(line) for line in dirs.inbox.read_text().splitlines()] if dirs.inbox.exists() else []


def sync(site, dirs):
    with channel(site, dirs) as h:
        return h.sync()


# MARK: One account, its main thread and Projects

def test_first_sync_records_the_whole_main_thread_then_each_project(site, dirs):
    site.page_size = 2
    site.add(msg(content="Hey, I'm Hark.", source="onboarding"),
             msg(actions=[{"kind": "connect_connector", "label": "Sign in to Google"}], source="onboarding"),
             msg("user", "ping"),
             msg(jobStatus="completed", isStreaming=False),
             msg(content="Pong.", presentation={"kind": "text"}, source="send_bubble"))
    site.add(msg("user", "a project question"), msg(content="a project answer"), thread=PROJECT)
    site.add(msg("user", "an archived question"), thread=OLD)
    assert sync(site, dirs) == 6
    got = events(dirs)
    assert [e["text"] if "text" in e else None for e in got] == [
        "Hey, I'm Hark.", None, "ping", "Pong.", "a project question", "a project answer"]
    assert all(e["kind"] == "new" and e["channel"] == "hark" for e in got)
    assert got[1]["buttons"] == [[{"text": "Sign in to Google", "kind": "connect_connector"}]]  # recorded, not pressed
    assert [e["is_from_me"] for e in got] == [False, False, True, False, True, False]
    assert [e.get("project") for e in got] == [None] * 4 + [PROJECT] * 2  # the main thread's carry none
    assert {c[1] for c in site.calls if c != "session"} == {MAIN, PROJECT}  # an archived Project isn't read
    assert json.loads((dirs.pins / "pins.json").read_text()) == {"user_id": "u-me", "email": "me@example.com",
                                                                  "conversation_id": MAIN}


def test_a_project_made_later_is_recorded_from_its_start(site, dirs):
    site.listed = [c for c in site.listed if c["id"] == MAIN]
    site.add(msg("user", "main before"))
    sync(site, dirs)
    site.listed.insert(0, {"id": PROJECT, "title": "new", "createdAt": "2026-10-27T02:58:05.111Z", "archivedAt": None})
    site.add(msg("user", "first in the project"), msg(content="its answer"), thread=PROJECT)
    site.add(msg(content="main after"))
    assert sync(site, dirs) == 3
    assert [(e["text"], e.get("project")) for e in events(dirs)] == [
        ("main before", None), ("main after", None), ("first in the project", PROJECT), ("its answer", PROJECT)]


def test_state_from_before_projects_carries_over_without_recording_the_main_thread_again(site, dirs):
    old = site.add(msg("user", "ping"), msg(content="Pong."))
    dirs.pins.mkdir(parents=True)
    (dirs.pins / "pins.json").write_text(json.dumps({"user_id": "u-me", "email": "me@example.com", "conversation_id": MAIN}))
    state = dirs.inbox.parent / "state.json"
    state.parent.mkdir(parents=True)
    tracked = {m["id"]: {"sig": hark_module.signature(hark_module.message_event(m, HARK)), "date": None} for m in old}
    state.write_text(json.dumps({"conversation_id": MAIN, "synced": True, "last_id": old[-1]["id"],
                                 "tracked": tracked, "working": {}}))  # as kept when only the main thread was recorded
    site.add(msg(content="new in main"))
    site.add(msg("user", "in the project"), thread=PROJECT)
    assert sync(site, dirs) == 2
    assert [e["text"] for e in events(dirs)] == ["new in main", "in the project"]
    assert set(json.loads(state.read_text())["threads"]) == {MAIN, PROJECT}


def test_another_account_is_refused_before_anything_is_read(site, dirs):
    sync(site, dirs)
    site.user = {"id": "u-someone-else", "email": "other@example.com"}
    site.calls.clear()
    with pytest.raises(HarkError, match="pinned to me@example.com"):
        sync(site, dirs)
    assert site.calls == ["session"]


def test_nobody_signed_in_says_how_to_sign_in(site, dirs):
    site.user = None
    with pytest.raises(HarkError, match="bench hark-login"):
        sync(site, dirs)
    assert not dirs.inbox.exists()


def test_only_one_hark_agent(site, dirs):
    with pytest.raises(HarkError, match="only one Hark agent"):
        channel(site, dirs, agents=(HARK, Agent("hark2", "hark.com", 1, 1, channel="hark")))


# MARK: Replies, work and edits

def test_a_streamed_reply_is_recorded_as_it_grows(site, dirs):
    [reply] = site.add(msg(content="Sedona in", isStreaming=True))
    sync(site, dirs)
    reply.update(content="Sedona in January is red rock.", isStreaming=False)
    sync(site, dirs)
    sync(site, dirs)  # nothing changed
    got = events(dirs)
    assert [(e["kind"], e["text"], e.get("streaming")) for e in got] == [
        ("new", "Sedona in", True), ("update", "Sedona in January is red rock.", None)]


def test_work_records_show_as_working_at_most_every_ten_seconds(site, dirs):
    [work] = site.add(msg(jobStatus="running", isStreaming=True))
    with channel(site, dirs) as h:
        h.sync()
        h.sync()
        assert [(e["kind"], e["action"]) for e in events(dirs)] == [("typing", "working")]
        h.state["working"][work["id"]] = "2000-01-01T00:00:00.000Z"
        h.sync()
        work.update(jobStatus="completed", isStreaming=False)
        h.sync()
    assert [e["kind"] for e in events(dirs)] == ["typing", "typing"]
    assert "guid" not in events(dirs)[0]  # never shown as an empty message


def test_browsing_windows_and_asks(site, dirs):
    site.windows = {"w1": {"live": True, "lines": [{"text": "Searching flights SJC → PHX"}]}}
    site.asks = {"a1": {"question": "Book the Amara for Jan 15–18?", "options": ["Book it", {"label": "Not now"}]}}
    sync(site, dirs)
    browsing, ask = events(dirs)
    assert (browsing["kind"], browsing["action"], browsing["detail"]) == ("typing", "browsing", "Searching flights SJC → PHX")
    assert ask["guid"] == "hark:ask:a1" and ask["text"] == "Book the Amara for Jan 15–18?"
    assert ask["buttons"] == [[{"text": "Book it", "kind": "option"}, {"text": "Not now", "kind": "option"}]]


def test_messages_that_arrive_between_syncs_are_all_recorded(site, dirs):
    site.page_size = 3
    site.add(msg("user", "first"))
    sync(site, dirs)
    site.add(*[msg(content=f"part {i}") for i in range(7)])
    sync(site, dirs)
    assert [e["text"] for e in events(dirs)] == ["first"] + [f"part {i}" for i in range(7)]
    assert any(c[2] for c in site.calls if c != "session")  # caught up with after=


def test_a_work_record_hark_deletes_is_never_where_sync_catches_up_from(site, dirs):
    # 2026-11-07: Hark deleted an empty work record that sync had kept as the last message; paging
    # forward from it answered 404 "message not found", and every sync failed.
    site.page_size = 3
    site.add(msg("user", "first"), msg(content="answer"))
    work = msg(jobStatus="running", isStreaming=True)
    site.add(work)
    sync(site, dirs)
    site.threads[MAIN].remove(work)  # done, so Hark deletes it
    site.add(*[msg(content=f"part {i}") for i in range(7)])
    sync(site, dirs)
    assert [e.get("text") for e in events(dirs) if e["kind"] == "new"] == ["first", "answer"] + [f"part {i}" for i in range(7)]


def test_a_deleted_last_message_is_caught_up_from_the_latest_page_back(site, dirs):
    site.page_size = 3
    site.add(msg("user", "first"), msg(content="answer"))
    sync(site, dirs)
    with channel(site, dirs) as h:  # state as it was written before last_at was kept
        h.state["threads"][MAIN] = {"synced": True, "last_id": "a-record-hark-deleted"}
        h._save_state()
    site.add(*[msg(content=f"part {i}") for i in range(7)])
    sync(site, dirs)
    assert [e.get("text") for e in events(dirs) if e["kind"] == "new"] == ["first", "answer"] + [f"part {i}" for i in range(7)]


def test_no_paging_when_the_latest_page_reaches_back_far_enough(site, dirs):
    site.page_size = 50
    site.add(msg("user", "first"), msg(content="answer"))
    sync(site, dirs)
    with channel(site, dirs) as h:
        h.state["threads"][MAIN]["last_id"] = "a-record-hark-deleted"  # gone, but the latest page holds what's new
        h._save_state()
    site.add(msg(content="more"))
    site.calls.clear()
    assert sync(site, dirs) == 1
    assert [c for c in site.calls if c != "session"] == [("history", MAIN, None, None), ("history", PROJECT, None, None)]


def test_messages_let_go_after_the_track_window_are_not_recorded_again(site, dirs):
    site.add(msg(content="old", timestamp=int((time.time() - 3 * 86400) * 1000)))
    sync(site, dirs)
    with channel(site, dirs) as h:
        h.state["tracked"].clear()
        assert h.sync() == 0


# MARK: Sending

def test_send_records_the_message_from_the_thread(site, dirs):
    with channel(site, dirs) as h:
        h.sync()
        event = h.send(HARK, "Plan a 3-day trip to Sedona 🌵\nfor MLK weekend")
        with pytest.raises(HarkError, match="isn't the Hark agent"):
            h.send(Agent("muse", "1@bot", 1, 1, channel="whatsapp"), "hi")
        with pytest.raises(HarkError, match="empty"):
            h.send(HARK, "  ")
    assert site.sent == ["Plan a 3-day trip to Sedona 🌵\nfor MLK weekend"]
    assert event["is_from_me"] and event["message_id"] == site.threads[MAIN][-1]["id"]
    assert events(dirs) == [event]


def test_send_goes_to_the_project_chosen_and_main_is_still_recorded(site, dirs):
    sync(site, dirs)
    assert "Project \"task-travel logistics\"" in choose_thread("task-travel logistics", pin_dir=dirs.pins, site=site)
    with channel(site, dirs) as h:
        h.sync()
        event = h.send(HARK, "Help me book a 3-day trip")
        site.add(msg(content="Morning! A note from Hark."))  # unprompted, in the main thread
        site.add(msg(content="Who's going?"), thread=PROJECT)
        h.sync()
    assert site.sent_to == [PROJECT_URL]
    assert event["project"] == PROJECT and event["message_id"] == site.threads[PROJECT][-2]["id"]
    assert [(e["text"], e.get("project")) for e in events(dirs)] == [
        ("Help me book a 3-day trip", PROJECT), ("Morning! A note from Hark.", None), ("Who's going?", PROJECT)]


def test_choosing_a_thread(site, dirs):
    sync(site, dirs)
    pins = dirs.pins / "pins.json"
    choose_thread(PROJECT[:6], pin_dir=dirs.pins, site=site)
    assert json.loads(pins.read_text())["send_to"] == PROJECT
    listing = choose_thread(pin_dir=dirs.pins, site=site)
    assert listing.splitlines()[1:] == ["  the main thread",
                                        f'  Project "task-travel logistics" ({PROJECT})  ← bench send and bench run type here']
    choose_thread("main", pin_dir=dirs.pins, site=site)
    assert "send_to" not in json.loads(pins.read_text())  # the main thread is the default
    with pytest.raises(HarkError, match="archived"):
        choose_thread(OLD, pin_dir=dirs.pins, site=site)
    with pytest.raises(HarkError, match="no Project matches"):
        choose_thread("nonexistent", pin_dir=dirs.pins, site=site)
    with pytest.raises(HarkError, match="no Project matches"):
        choose_thread("con", pin_dir=dirs.pins, site=site)  # too short to stand for an ID


def test_send_refuses_a_project_that_was_archived(site, dirs):
    sync(site, dirs)
    choose_thread(PROJECT, pin_dir=dirs.pins, site=site)
    site.listed[0]["archivedAt"] = "2026-10-28T00:00:00.000Z"
    with channel(site, dirs) as h, pytest.raises(HarkError, match="archived or gone"):
        h.send(HARK, "hello")
    assert site.sent == []


def test_project_messages_export_with_their_thread():
    event = {"kind": "new", "emitted_at": "2026-10-27T03:00:00.000Z", "channel": "hark", "agent": "hark",
             "handle": "hark.com", "guid": "hark:m1", "message_id": "m1", "is_from_me": True, "text": "hi",
             "date": "2026-10-27T03:00:00.000Z", "project": PROJECT}
    assert conversation.message(event, 1, event["emitted_at"], None)["thread"] == f"hark:project:{PROJECT}"
    assert conversation.message({**event, "project": None}, 1, event["emitted_at"], None)["thread"] is None


def test_a_run_transcript_says_when_a_message_came_in_another_thread():
    from bench import runs

    def m(role, text, thread):
        return {"role": role, "text": text, "sent_at": "2026-10-27T03:00:00.000Z", "edited_at": None, "versions": 1,
                "deleted_at": None, "attachments": [], "buttons": [], "reactions": [], "channel": "hark", "thread": thread}

    project = f"hark:project:{PROJECT}"
    md = runs.markdown({"agent": "hark", "channel": "hark", "run": "r", "task": "Travel Logistics"},
                       [m("user", "Plan it", project), m("agent", "Who's going?", project), m("agent", "Morning!", None)])
    heads = [line for line in md.splitlines() if line.startswith("## ")]
    assert ["in the main thread" in h for h in heads] == [False, False, True]


def test_a_sent_message_that_never_shows_up_is_an_error(site, dirs):
    site.shows_sent = False
    now = [0.0]
    with channel(site, dirs, sleep=lambda s: now.__setitem__(0, now[0] + s), clock=lambda: now[0]) as h:
        with pytest.raises(HarkError, match="didn't show up"):
            h.send(HARK, "hello")


def test_listen_polls_until_the_time_is_up(site, dirs):
    now = [0.0]

    def sleep(seconds):
        now[0] += seconds
        site.add(msg(content=f"at {now[0]:.0f}"))

    with channel(site, dirs, sleep=sleep, clock=lambda: now[0]) as h:
        h.listen(9, poll_seconds=3)
    assert [e["text"] for e in events(dirs)] == ["at 3", "at 6", "at 9"]


def test_one_command_at_a_time(site, dirs):
    with channel(site, dirs):
        with pytest.raises(HarkError, match="another bench command"):
            channel(site, dirs).__enter__()


def test_status_checks_without_sending_or_recording(site, dirs):
    sync(site, dirs)
    before = events(dirs)
    report = status(pin_dir=dirs.pins, site=site)
    assert "signed in as me@example.com" in report and f"Main thread: {MAIN} (idle)" in report
    assert f'Project "task-travel logistics": {PROJECT} (idle)' in report and OLD not in report
    assert "Sending to: the main thread (the Hark tab is showing it)" in report
    assert "found, empty" in report and "62,822 of 8,000,000 today" in report
    choose_thread(PROJECT, pin_dir=dirs.pins, site=site)
    assert "Sending to: Project \"task-travel logistics\"" in status(pin_dir=dirs.pins, site=site)
    assert "sending opens the thread first" in status(pin_dir=dirs.pins, site=site)
    assert site.sent == [] and events(dirs) == before


# MARK: Importing an earlier run

def write_run(dirs, conversation=MAIN, lines=()):
    run = dirs.tmp / "runs" / "20261025-002245-travel-day1-hark"
    run.mkdir(parents=True)
    (run / "run.json").write_text(json.dumps({"agent": "hark", "conversation_id": conversation}))
    (run / "events.jsonl").write_text("".join(json.dumps(line) + "\n" for line in lines))
    return run


def run_event(kind, m, at):
    return {"agent": "hark", "channel": "web", "kind": kind, "message_id": m["id"], "emitted_at": at,
            "text": m.get("content", ""), "hark": {k: v for k, v in m.items() if k != "content"}}


def test_import_keeps_the_observed_times_and_sync_does_not_repeat_them(site, dirs):
    [ping] = site.add(msg("user", "ping"))
    prompt, work, reply = msg("user", "Plan Sedona"), msg(jobStatus="running", isStreaming=True), msg(content="Who's going?")
    run = write_run(dirs, lines=[
        run_event("new", prompt, "2026-10-25T07:22:47.000Z"),
        run_event("new", work, "2026-10-25T07:22:47.100Z"),
        run_event("update", {**work, "jobStatus": "completed", "isStreaming": False}, "2026-10-25T07:22:57.000Z"),
        run_event("new", reply, "2026-10-25T07:22:57.900Z"),
    ])
    site.add(prompt, {**work, "jobStatus": "completed", "isStreaming": False}, reply)
    with channel(site, dirs) as h:
        assert h.import_run(run) == 3
        assert h.sync() == 1  # only the ping, which the run didn't see
    got = events(dirs)
    assert [(e["kind"], e.get("text") or e.get("action"), e["emitted_at"]) for e in got[:3]] == [
        ("new", "Plan Sedona", "2026-10-25T07:22:47.000Z"),
        ("typing", "working", "2026-10-25T07:22:47.100Z"),
        ("new", "Who's going?", "2026-10-25T07:22:57.900Z")]
    assert got[3]["message_id"] == ping["id"]


def test_a_run_from_another_thread_is_not_imported(site, dirs):
    run = write_run(dirs, conversation=PROJECT)
    with channel(site, dirs) as h, pytest.raises(HarkError, match="not the pinned main thread"):
        h.import_run(run)


# MARK: The page

class FakeBox:
    def __init__(self, page):
        self.page = page

    def count(self):
        return self.page.boxes

    def input_value(self):
        return self.page.typed

    def fill(self, text):
        self.page.log.append(("fill", text))
        self.page.typed = text if not self.page.mangles else text[:-1]

    def click(self):
        self.page.log.append(("click", "box"))
        self.page.typed = "Get a bánh mì off DoorDash"  # what tapping the suggested prompt does


class FakeSend:
    def __init__(self, page, name):
        self.page, self.name = page, name

    def or_(self, other):
        return self if self.name == self.page.send_label else other

    def wait_for(self, state, timeout):
        if not self.page.typed or not self.page.send_shows or self.name != self.page.send_label:
            raise PlaywrightTimeout("no Send button")

    def click(self):
        self.page.log.append(("click", "send"))
        if self.page.send_fails:
            raise RuntimeError("Send button went away")
        self.page.sent.append(self.page.typed)
        self.page.typed = ""


class FakePage:
    def __init__(self, url=CHAT_URL, *, typed="", boxes=1, responses=None):
        self.url, self.typed, self.boxes = url, typed, boxes
        self.log, self.sent, self.visited, self.fetched = [], [], [], []
        self.mangles = self.send_fails = False
        self.send_shows = True
        self.send_label = Selectors().send_name  # the Send button's name in the page's language
        self.responses = responses or {}

    def get_by_role(self, role, name, exact):
        if role == "textbox" and name == Selectors().box_name:
            return FakeBox(self)
        if role == "button" and name in (Selectors().send_name, Selectors().send_name_zh):
            return FakeSend(self, name)
        raise AssertionError(f"unexpected role {role} {name}")

    def locator(self, selector):
        return FakeBox(self)

    def goto(self, url, wait_until=None):
        self.visited.append(url)
        self.url = url

    def evaluate(self, js, arg):
        assert js == FETCH_JS
        self.fetched.append(arg)
        body = self.responses.get(arg["path"])
        return {"status": 200, "text": json.dumps(body)} if body is not None else {"status": 404, "text": ""}


def test_typing_fills_the_box_and_clicks_send_never_the_box():
    page = FakePage()
    type_and_send(page, "Plan a trip 🌵\nfor two")
    assert page.sent == ["Plan a trip 🌵\nfor two"]
    assert page.log == [("fill", "Plan a trip 🌵\nfor two"), ("click", "send")]


def test_send_is_found_when_hark_shows_in_chinese():
    page = FakePage()
    page.send_label = Selectors().send_name_zh
    type_and_send(page, "hello")
    assert page.sent == ["hello"]


def test_text_already_in_the_box_is_never_sent_over():
    page = FakePage(typed="half-written")
    with pytest.raises(HarkError, match="already has text"):
        type_and_send(page, "hello")
    assert page.log == [] and page.typed == "half-written"


@pytest.mark.parametrize("problem", ["mangles", "send_fails", "no_send"])
def test_a_failed_send_leaves_no_text_behind(problem):
    page = FakePage()
    page.mangles, page.send_fails, page.send_shows = problem == "mangles", problem == "send_fails", problem != "no_send"
    with pytest.raises((HarkError, RuntimeError)):
        type_and_send(page, "hello")
    assert page.typed == "" and page.sent == []


def test_typing_is_refused_off_hark():
    page = FakePage("https://accounts.google.com/signin")
    with pytest.raises(HarkError, match="not hark.com"):
        type_and_send(page, "hello")
    assert page.log == []


def browser(*pages):
    new = []

    def new_page():
        page = FakePage("about:blank")
        new.append(page)
        context.pages.append(page)
        return page

    context = SimpleNamespace(pages=list(pages), new_page=new_page)
    return SimpleNamespace(contexts=[context], new=new)


def test_hark_web_reads_only_hark_api_paths_from_the_hark_tab():
    slack, hark = FakePage("https://app.slack.com/client/T0/D0"), FakePage(responses={"/api/auth/get-session": {"user": USER}})
    web = HarkWeb(browser=browser(slack, hark))
    assert web.session() == USER
    assert hark.fetched == [{"path": "/api/auth/get-session", "params": None}] and slack.fetched == []
    with pytest.raises(HarkError, match="only hark.com's own /api/"):
        web.get("/chat")
    assert slack.visited == []  # other tabs are never navigated


def test_hark_web_send_opens_the_thread_first_only_when_needed():
    page = FakePage("https://hark.com/settings/connected-cards")
    web = HarkWeb(browser=browser(page))
    web.send("to the project", PROJECT_URL, sleep=lambda s: None)
    web.send("again", PROJECT_URL, sleep=lambda s: None)
    assert page.visited == [PROJECT_URL] and page.sent == ["to the project", "again"]
    web.send("to main", CHAT_URL, sleep=lambda s: None)
    assert page.visited == [PROJECT_URL, CHAT_URL]


def test_hark_web_send_never_leaves_a_half_written_message():
    page = FakePage(typed="half-written")
    with pytest.raises(HarkError, match="already has text"):
        HarkWeb(browser=browser(page)).send("hello", PROJECT_URL, sleep=lambda s: None)
    assert page.visited == [] and page.typed == "half-written"


def test_hark_web_opens_hark_chat_when_no_tab_is_on_hark():
    b = browser(FakePage("https://app.slack.com/client/T0/D0"))
    web = HarkWeb(browser=b)
    web.page()
    assert [p.visited for p in b.new] == [[CHAT_URL]]


def test_sign_in_waits_without_reading_other_sites():
    page = FakePage(responses={"/api/auth/get-session": {"user": None}})
    page.bring_to_front = lambda: None
    web = HarkWeb(browser=browser(page))
    now = [0.0]

    def sleep(seconds):
        now[0] += seconds
        if now[0] == 2:  # you're on Google's sign-in page
            page.url = "https://accounts.google.com/v3/signin"
        elif now[0] == 4:  # back on Hark, signed in
            page.url = CHAT_URL
            page.responses["/api/auth/get-session"] = {"user": USER}

    assert web.sign_in(wait_seconds=30, sleep=sleep, clock=lambda: now[0]) is False
    assert len(page.fetched) == 2  # before, and after coming back; never while on Google


# MARK: Config

@pytest.mark.parametrize("raw", ["hark.com", "https://hark.com/", "HARK.COM", "https://www.hark.com"])
def test_hark_handles(raw):
    assert normalize_handle(raw, "hark") == "hark.com"


def test_hark_handle_must_be_hark_com():
    with pytest.raises(ConfigError, match="hark.com"):
        normalize_handle("hark.ai", "hark")
