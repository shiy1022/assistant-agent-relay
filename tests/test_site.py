"""The parts the web-app channels share (bench/channels/site.py), with a stand-in page."""

import pytest
from playwright.sync_api import TimeoutError as PlaywrightTimeout

from bench.channels.site import SiteError, SiteWeb, fill_and_send, shows


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
        self.page.typed = "a suggested prompt"  # what tapping a suggestion laid over the box would do


class FakeSend:
    def __init__(self, page, name):
        self.page, self.name = page, name

    def or_(self, other):
        return self if self.name == self.page.send_label else other

    def wait_for(self, state, timeout):
        if not self.page.typed or self.name != self.page.send_label:
            raise PlaywrightTimeout("no Send button")

    def is_enabled(self):
        return True

    def click(self):
        self.page.log.append(("click", "send"))
        if self.page.send_fails:
            raise RuntimeError("Send button went away")
        self.page.sent.append(self.page.typed)
        self.page.typed = "" if self.page.empties else self.page.typed


class FakePage:
    """A site's tab: one message box, and a Send button with `send_label` as its name."""

    def __init__(self, url="https://example.ai/chat", *, typed="", boxes=1, send_label="Send"):
        self.url, self.typed, self.boxes, self.send_label = url, typed, boxes, send_label
        self.log, self.sent, self.visited = [], [], []
        self.mangles = self.send_fails = False
        self.empties = True

    def box(self):
        return FakeBox(self)

    def button(self, *names):
        button = None
        for name in names:
            b = FakeSend(self, name)
            button = b if button is None else button.or_(b)
        return button

    def get_by_role(self, role, name, exact):
        if role == "textbox":
            return FakeBox(self) if name == "Message" else type("NoBox", (), {"count": lambda self: 0})()
        return FakeSend(self, name)

    def locator(self, selector):
        return FakeBox(self)

    def goto(self, url, wait_until=None):
        self.visited.append(url)
        self.url = url


def send(page, text, *names):
    fill_and_send(page, page.box(), page.button(*(names or ("Send message", "Send"))), text, error=SiteError,
                  name="Agent", sent_seconds=0.5, sleep=lambda s: None)


def test_it_fills_the_box_and_clicks_send_never_the_box():
    page = FakePage()
    send(page, "Plan a housewarming 🏠\nfor ten")
    assert page.sent == ["Plan a housewarming 🏠\nfor ten"]
    assert page.log == [("fill", "Plan a housewarming 🏠\nfor ten"), ("click", "send")]


def test_send_is_found_by_any_of_its_names():
    page = FakePage(send_label="发送")
    send(page, "hello", "Send", "发送")
    assert page.sent == ["hello"]


def test_text_already_in_the_box_is_never_sent_over():
    page = FakePage(typed="a draft of yours")
    with pytest.raises(SiteError, match="already has text"):
        send(page, "hello")
    assert page.log == [] and page.typed == "a draft of yours"


@pytest.mark.parametrize("problem", ["mangles", "send_fails", "no_button"])
def test_a_failed_send_leaves_no_text_behind(problem):
    page = FakePage(send_label="Something else" if problem == "no_button" else "Send")
    setattr(page, problem, True) if problem != "no_button" else None
    with pytest.raises((SiteError, RuntimeError)):
        send(page, "hello")
    assert page.typed == "" and page.sent == []
    assert page.log[-1] == ("fill", "")


def test_a_box_that_does_not_empty_after_send_is_an_error():
    page = FakePage()
    page.empties = False
    with pytest.raises(SiteError, match="didn't empty"):
        send(page, "hello")


def test_empty_messages_are_refused():
    with pytest.raises(SiteError, match="empty"):
        send(FakePage(), "  \n")


def test_shows():
    assert shows("https://wajo.ai/fo/c/abc?x=1", "https://wajo.ai/fo/c/abc")
    assert shows("https://muse.ai/", "https://muse.ai/")
    assert not shows("https://wajo.ai/fo/c/abcd", "https://wajo.ai/fo/c/abc")
    assert not shows("https://muse.ai/thread/1", "https://muse.ai/")


class Context:
    def __init__(self, pages):
        self.pages = pages

    def new_page(self):
        page = FakePage("about:blank")
        self.pages.append(page)
        return page


class Example(SiteWeb):
    origin, home, name = "https://example.ai", "https://example.ai/chat", "Example"

    def draft(self):
        return self.page().typed


def test_the_tab_on_the_site_is_reused_and_others_are_left_alone():
    other, mine = FakePage("https://mail.example.com/"), FakePage("https://example.ai/chat")
    web = Example(browser=type("Browser", (), {"contexts": [Context([other, mine])]})())
    assert web.page() is mine
    web.open("https://example.ai/other", lambda: True)
    assert mine.visited == ["https://example.ai/other"] and other.visited == []


def test_a_new_tab_opens_on_the_home_page_when_none_is_on_the_site():
    context = Context([FakePage("https://mail.example.com/")])
    web = Example(browser=type("Browser", (), {"contexts": [context]})())
    assert web.page().visited == ["https://example.ai/chat"]


def test_a_page_with_a_draft_is_never_left():
    mine = FakePage("https://example.ai/chat", typed="half a message")
    web = Example(browser=type("Browser", (), {"contexts": [Context([mine])]})())
    with pytest.raises(SiteError, match="already has text"):
        web.open("https://example.ai/other", lambda: True)
    assert mine.visited == []
