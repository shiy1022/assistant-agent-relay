import itertools
import json
import time
from types import SimpleNamespace

import pytest
from playwright.sync_api import TimeoutError as PlaywrightTimeout

from bench.channels import hark as hark_module
from bench.channels.hark import CHAT_URL, FETCH_JS, HarkChannel, HarkError, HarkWeb, Selectors, status, type_and_send
from bench.config import Agent, ConfigError, normalize_handle
from bench.deliverables import freeze as deliverables_freeze

HARK = Agent("hark", "hark.com", 180, 1800, channel="hark")
USER = {"id": "u-me", "email": "me@example.com", "name": "Me"}
MAIN, PROJECT = "conv-main", "conv-project"
ids = itertools.count(1)


def msg(role="assistant", content="", **fields) -> dict:
    return {"id": f"m{next(ids):04d}", "role": role, "content": content, "source": "web",
            "timestamp": int(time.time() * 1000), **fields}


class FakeSite:
    """hark.com as HarkChannel sees it: one account, its threads in memory, `page_size` messages per history page."""

    def __init__(self, *, user=USER, page_size=50):
        self.user, self.page_size = user, page_size
        self.threads = {MAIN: [], PROJECT: []}
        # The API lists the newer Project first; the main thread is the one made at sign-up.
        self.listed = [{"id": PROJECT, "createdAt": "2026-10-25T08:00:00.000Z", "archivedAt": None},
                       {"id": MAIN, "createdAt": "2026-10-25T06:39:19.736Z", "archivedAt": None}]
        self.windows, self.asks = {}, {}
        self.box, self.sent, self.calls = "", [], []
        self.shows_sent = True  # whether a sent message shows up in the thread
        self.files = {}         # what /api/files/… paths answer; a HarkError to fail

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
        if after:
            page = thread[position[after] + 1:][:self.page_size]
        elif before:
            page = thread[:position[before["id"]]][-self.page_size:]
        else:
            page = thread[-self.page_size:]
        start = position[page[0]["id"]] if page else len(thread)
        return {"shape": "records", "log": [{"id": m["id"], "kind": "message"} for m in page],
                "messages": {m["id"]: dict(m) for m in page}, "windows": dict(self.windows), "asks": dict(self.asks),
                "older": {"hasMore": start > 0}, "newer": {"hasMore": False}}

    def meter(self):
        return {"dailyUsed": 62822, "dailyLimit": 8000000, "poolUsed": 100613, "poolLimit": 40000000,
                "poolResetsAt": "2026-11-01T00:00:00Z"}

    def text(self, path):
        self.calls.append(("text", path))
        answer = self.files.get(path)
        if isinstance(answer, Exception):
            raise answer
        if answer is None:
            raise hark_module.HarkNetworkError(f"hark.com answered {path} with HTTP 404")
        return answer

    def draft(self):
        return self.box

    def send(self, text):
        self.sent.append(text)
        if self.shows_sent:
            self.add(msg("user", text))


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


# MARK: One account, its main thread

def test_first_sync_records_the_whole_main_thread_and_nothing_else(site, dirs):
    site.page_size = 2
    site.add(msg(content="Hey, I'm Hark.", source="onboarding"),
             msg(actions=[{"kind": "connect_connector", "label": "Sign in to Google"}], source="onboarding"),
             msg("user", "ping"),
             msg(jobStatus="completed", isStreaming=False),
             msg(content="Pong.", presentation={"kind": "text"}, source="send_bubble"))
    site.add(msg("user", "a project question"), thread=PROJECT)
    assert sync(site, dirs) == 4
    got = events(dirs)
    assert [e["text"] if "text" in e else None for e in got] == ["Hey, I'm Hark.", None, "ping", "Pong."]
    assert all(e["kind"] == "new" and e["channel"] == "hark" for e in got)
    assert got[1]["buttons"] == [[{"text": "Sign in to Google", "kind": "connect_connector"}]]  # recorded, not pressed
    assert [e["is_from_me"] for e in got] == [False, False, True, False]
    assert {c[1] for c in site.calls if c != "session"} == {MAIN}  # the Project is never read
    assert json.loads((dirs.pins / "pins.json").read_text()) == {"user_id": "u-me", "email": "me@example.com",
                                                                  "conversation_id": MAIN}


def test_a_rich_cards_caption_is_recorded_with_its_text(site, dirs):
    # Hark puts the sentence under a table or order card in presentation.<kind>.ornament, not in content.
    table = {"kind": "table", "title": "Hotels", "table": {"columns": ["Hotel"], "rows": [["Four Seasons"]],
                                                           "ornament": "I'd go with the Four Seasons."}}
    site.add(msg("user", "hotels?"),
             msg(content="| Hotel |\n| --- |\n| Four Seasons |", presentation=table, source="send_bubble"),
             msg(content="Pay at pickup.", presentation={"kind": "order_summary", "order_summary": {"ornament": "Pay at pickup."}}),
             msg(content="Plain.", presentation={"kind": "text"}))
    sync(site, dirs)
    texts = [e["text"] for e in events(dirs)]
    assert texts[1] == "| Hotel |\n| --- |\n| Four Seasons |\n\nI'd go with the Four Seasons."
    assert texts[2] == "Pay at pickup."  # a caption already in the text isn't repeated
    assert texts[3] == "Plain."


def test_a_rich_cards_structure_is_recorded_for_the_console(site, dirs):
    order = {"kind": "order_summary", "title": "Enterprise, PHX", "order_summary": {
        "merchant": "Enterprise, PHX", "site": "enterprise.com", "destination": "Pickup Sat 1 PM; return Mon 3 PM",
        "line_items": [{"name": "Compact Nissan Versa or similar", "qty": "1", "price": "$180.20",
                        "description": "Pay Later, unlimited mileage", "image_url": "https://x/y.png"}],
        "breakdown": [{"label": "Taxes and fees", "amount": "$85.29"}], "total": "$265.49",
        "payment": "Pay Later at counter", "ornament": "Nothing is charged now.",
        "action": {"id": "f0e5", "label": "Reserve Car"}}}
    table = {"kind": "table", "title": "Hotels", "table": {"columns": ["Hotel", "Price"], "rows": [["Four Seasons", "$3,145"]],
                                                           "ornament": "I'd go with the Four Seasons.", "row_sites": ["fourseasons.com"]}}
    site.add(msg("user", "car?"),
             msg(content="| Item | Price |", presentation=order, source="send_bubble"),
             msg(content="| Hotel |", presentation=table, source="send_bubble"),
             msg(content="Plain.", presentation={"kind": "text"}))
    sync(site, dirs)
    cards = [e.get("card") for e in events(dirs)]
    assert cards[1] == {"kind": "order_summary", "title": "Enterprise, PHX", "merchant": "Enterprise, PHX", "site": "enterprise.com",
                        "destination": "Pickup Sat 1 PM; return Mon 3 PM",
                        "line_items": [{"name": "Compact Nissan Versa or similar", "qty": "1", "price": "$180.20",
                                        "description": "Pay Later, unlimited mileage"}],  # no image address
                        "breakdown": [{"label": "Taxes and fees", "amount": "$85.29"}], "total": "$265.49",
                        "payment": "Pay Later at counter", "ornament": "Nothing is charged now.", "action": "Reserve Car"}
    assert cards[2] == {"kind": "table", "title": "Hotels", "columns": ["Hotel", "Price"], "rows": [["Four Seasons", "$3,145"]],
                        "ornament": "I'd go with the Four Seasons."}
    assert cards[0] is None and cards[3] is None


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


PAGE_HTML = "<!DOCTYPE html><html><head><title>Sedona MLK Weekend 2027</title></head><body><h1>Sedona</h1></body></html>"
DOWNLOAD = "/api/files/conv-main/artifact?key=sessions%2Fconv-main%2Fartifacts%2F978650e9-29a%2Fwebsite%2Findex.html"


def page_card(**fields) -> dict:
    """Hark's record for a Page it built: no text, a website_coding_agent tool call with the site's files."""
    call = {"dispatchId": "tu_XFXIS6MCL4PF42LMYKSBKJH7R5", "toolName": "website_coding_agent",
            "input": {"project_name": "Sedona MLK Weekend 2027"}, "status": "completed",
            "result": {"project_id": "dd7d20a1-bca3-42b5-a0d0-eda7c0f80714", "project_name": "Sedona MLK Weekend 2027"},
            "updatedAt": 1792966568480, "dispatchedAt": 1792966567453,
            "artifactKeys": [{"path": "website/index.html", "download_url": DOWNLOAD,
                              "s3_key": "sessions/conv-main/artifacts/978650e9-29a/website/index.html"}]}
    return msg(content="", toolCalls=[call], entryKind="card", **fields)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Saving a Page embeds its images and fonts; nothing in these tests has any, and nothing may be fetched."""
    def refuse(url):
        raise AssertionError(f"tried to fetch {url}")
    monkeypatch.setattr(hark_module, "freeze", lambda html: deliverables_freeze(html, fetch=refuse))


def test_a_page_hark_built_is_recorded_with_its_link_and_a_saved_copy(site, dirs):
    site.files[DOWNLOAD] = PAGE_HTML
    site.add(msg("user", "Plan Sedona"), page_card(), msg(content="Your Sedona Page is ready."))
    sync(site, dirs)
    prompt, page, ready = events(dirs)
    assert page["kind"] == "new" and page["hark_kind"] == "page" and page["rich"] is True
    assert page["text"] == "Page: Sedona MLK Weekend 2027\nhttps://hark.com/site/dd7d20a1-bca3-42b5-a0d0-eda7c0f80714"
    assert page["pages"] == [{"project_id": "dd7d20a1-bca3-42b5-a0d0-eda7c0f80714", "name": "Sedona MLK Weekend 2027",
                              "url": "https://hark.com/site/dd7d20a1-bca3-42b5-a0d0-eda7c0f80714", "download": DOWNLOAD,
                              "built_at": "2026-10-25T22:16:08.480Z"}]
    [copy] = page["attachments"]
    assert copy == {"name": "Sedona MLK Weekend 2027.html", "mime_type": "text/html", "bytes": len(PAGE_HTML),
                    "path": f"attachments/{page['message_id']}/Sedona MLK Weekend 2027.html"}
    assert (dirs.inbox.parent / copy["path"]).read_text() == PAGE_HTML
    assert ready["text"] == "Your Sedona Page is ready."
    # The next sync reads nothing again and writes nothing new
    with channel(site, dirs) as h:
        assert h.sync() == 0
    assert [c for c in site.calls if c[0] == "text"] == [("text", DOWNLOAD)]


def test_a_page_that_could_not_be_saved_is_recorded_and_saved_by_a_later_sync(site, dirs):
    site.add(page_card())
    with channel(site, dirs) as h:
        h.sync()
        [page] = events(dirs)
        assert page["attachments"] == [{"name": "Sedona MLK Weekend 2027.html", "mime_type": "text/html"}]  # no path yet
        assert "https://hark.com/site/dd7d20a1" in page["text"]
        site.files[DOWNLOAD] = PAGE_HTML
        assert h.sync() == 1
    page = events(dirs)[-1]
    assert page["kind"] == "update" and page["attachments"][0]["path"].endswith("Sedona MLK Weekend 2027.html")


def test_a_page_still_being_built_is_work_not_a_message(site, dirs):
    card = page_card(jobStatus="running", isStreaming=True)
    card["toolCalls"][0] = {**card["toolCalls"][0], "status": "running", "result": None, "artifactKeys": []}
    site.add(card)
    sync(site, dirs)
    assert [(e["kind"], e["action"]) for e in events(dirs)] == [("typing", "working")]


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
    assert "signed in as me@example.com" in report and MAIN in report and "idle" in report
    assert "found, empty" in report and "62,822 of 8,000,000 today" in report
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


def test_a_secure_screen_is_recorded_in_words_with_its_button_and_no_values():
    # What Hark sends when it needs a sign-in or a card: a request with a screen, not a question.
    ask = {"id": "a35a", "at": 1793136462503, "status": "waiting_for_input", "request": {
        "requestId": "uir_1", "prompt": "Booking.com", "displayHint": "secure_screen", "screen": {
            "state": "awaiting_input", "header": {"title": "Login", "icon_url": "https://icons.example/booking.ico"},
            "components": [
                {"type": "instructions", "text": "Enter your Booking.com credentials and we'll save them to your encrypted vault."},
                {"type": "text_input", "id": "username", "label": "Email", "placeholder": "user@domain.com", "autocomplete": "username"},
                {"type": "text_input", "id": "password", "label": "Password", "autocomplete": "current-password", "secret": True},
                {"type": "choice", "label": "Google", "provider": "google", "screen": {"components": [{"type": "text_input", "label": "Email or phone"}]}},
                {"type": "choice", "label": "Apple", "provider": "apple"}],
            "primary_action": {"id": "submit", "label": "Login", "kind": "submit"}}}}
    agent = SimpleNamespace(name="hark", handle="hark.com")
    event = hark_module.ask_event("a35a", ask, agent)
    assert event["text"] == ("Action required: Booking.com · Login\n"
                             "Enter your Booking.com credentials and we'll save them to your encrypted vault.\n"
                             "Fields: Email, Password\nOr continue with: Google, Apple\nButton: Login\n"
                             "(Hark's secure screen; recorded, nothing entered)")
    assert event["buttons"] == [[{"text": "Sign in to Booking.com", "kind": "secure_screen"}]]
    assert event["hark_kind"] == "secure_screen" and event["date"] == hark_module.ms_time(1793136462503)
    assert "autocomplete" not in event["text"] and "{" not in event["text"]

    card = {"request": {"prompt": "Hark Wallet", "displayHint": "secure_screen", "screen": {
        "header": {"title": "Add a card"}, "components": [{"type": "text_input", "label": "Card number", "secret": True}],
        "primary_action": {"label": "Save card"}}}}
    event = hark_module.ask_event("c1", card, agent)
    assert event["text"].startswith("Action required: Hark Wallet · Add a card\nFields: Card number\nButton: Save card")
    assert event["buttons"] == [[{"text": "Add a card", "kind": "secure_screen"}]]


def test_a_closed_ask_is_recorded_as_closed_not_as_json():
    agent = SimpleNamespace(name="hark", handle="hark.com")
    event = hark_module.ask_event("a35a", {"id": "a35a825c", "at": 1793136462503}, agent)
    assert event["text"] == "(Hark closed this request)" and event["hark_kind"] == "ask_closed" and "buttons" not in event
    event = hark_module.ask_event("a35a", {"id": "a35a825c", "at": 1793136462503, "status": "resolved"}, agent)
    assert event["text"] == "(Hark closed this request: resolved)"


def test_history_pages_are_read_in_both_shapes_hark_has_served():
    # Until 2026-10-28: messages by id plus a log for the order. Since then: a list, already ordered by time.
    old = {"messages": {"b": {"id": "b", "role": "assistant", "timestamp": 2}, "a": {"id": "a", "role": "user", "timestamp": 1}},
           "log": [{"kind": "message", "id": "a"}, {"kind": "window", "id": "w"}, {"kind": "message", "id": "b"}]}
    new = {"messages": [{"id": "b", "role": "assistant", "timestamp": 2}, {"id": "a", "role": "user", "timestamp": 1}],
           "older": {"hasMore": True, "cursor": {"id": "a", "createdAt": 1}}, "newer": {"hasMore": False}, "threads": []}
    assert [m["id"] for m in hark_module.ordered(old)] == ["a", "b"]
    assert [m["id"] for m in hark_module.ordered(new)] == ["a", "b"]
    assert hark_module.ids(old) == hark_module.ids(new) == {"a", "b"}
    assert hark_module.ordered({}) == [] and hark_module.ids({}) == set()


def test_hark_announcements_are_notices_not_messages_from_me():
    agent = SimpleNamespace(name="hark", handle="hark.com")
    msg = {"id": "5e2f", "role": "user", "source": "announcement", "timestamp": 1793205900906,
           "content": "Good news! I have a Mac app now, Hark Pro for macOS."}
    event = hark_module.message_event(msg, agent)
    assert event["is_from_me"] is False and event["service"] == "announcement"
    assert event["text"].startswith("Good news!")
    mine = hark_module.message_event({"id": "a1", "role": "user", "timestamp": 1, "content": "hi"}, agent)
    assert mine["is_from_me"] is True and "service" not in mine
