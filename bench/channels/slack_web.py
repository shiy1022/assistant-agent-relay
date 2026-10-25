"""Slack web control: drives Slack's own web client in a Chrome that's already running.

Self-contained: it needs only Playwright and a Chrome started with a debugging
port (CHROME_COMMAND), and knows nothing else about agent-runner, so it can be
reused as is.

    from bench.channels.slack_web import Conversation, SlackWeb

    dm = Conversation(team_id="T0CARQF460P", team="PJTTY", channel_id="D0CCSN3KHG8", label="yschore-dot")
    with SlackWeb() as web:            # attaches to Chrome at 127.0.0.1:9222
        web.sign_in(dm, "https://pjtty.slack.com/")  # waits for you to sign in, if you aren't yet
        print(web.check(dm))           # opens the conversation and checks it; types nothing
        web.send(dm, "hello\\nworld")   # types it and clicks Send

Why type at all: Slack marks every message sent through its API with the
sending app's ID, and bots like the dot skip those. A message typed into the
web client arrives exactly as if a person had typed it.

What it guarantees:
- The only pages it opens are the conversation itself
  (https://app.slack.com/client/<team ID>/<channel ID>) and, from sign_in, the
  workspace's sign-in page.
- It types only after checking that the tab shows that conversation, in three
  ways: the URL, the "… - <team> - Slack" title, and the "Message to <label>"
  box.
- It never types over a draft. Slack syncs drafts between devices, so one
  could be yours.
- Each line goes in as one block of text, so emoji survive and no
  autocomplete pops up. Shift+Enter starts each new line, and only the Send
  button sends.
- If anything fails before Send, it clears the box, so half a message is never
  left behind.
- Leaving the with-block detaches from Chrome, which keeps running with its
  tabs as they were.
It reads nothing on the page beyond those checks. Read replies through
Slack's API instead.
"""

import json
import sys
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright

__all__ = ["CHROME_COMMAND", "Conversation", "Selectors", "SlackWeb", "SlackWebError", "WebStatus"]

CDP_URL = "http://127.0.0.1:9222"
CHROME_COMMAND = ('open -na "Google Chrome" --args --user-data-dir="$HOME/Library/Application Support/agent-runner/chrome" '
                  "--remote-debugging-port=9222 --disable-background-timer-throttling "
                  "--disable-backgrounding-occluded-windows --disable-renderer-backgrounding")


class SlackWebError(Exception):
    pass


@dataclass(frozen=True)
class Conversation:
    """One Slack conversation, as the web client addresses and labels it."""

    team_id: str     # the workspace's ID, like T0CARQF460P
    team: str        # the workspace's name, as in the page title "… - PJTTY - Slack"
    channel_id: str  # the conversation's ID; D… for a DM
    label: str       # the name in its "Message to …" box, like yschore-dot

    @property
    def url(self) -> str:
        return f"https://app.slack.com/client/{self.team_id}/{self.channel_id}"

    @property
    def workspace_url(self) -> str:
        return f"https://app.slack.com/client/{self.team_id}/"


@dataclass(frozen=True)
class Selectors:
    """Where things are in Slack's web client. If Slack changes its markup, this is the one place to update."""

    # The conversation's own box and Send button. An open thread has its own box ("Reply to thread with …")
    # and Send button inside reply_container instead, and those are never used.
    box: str = '[data-qa="message_input_container"] [data-qa="message_input"] [data-qa="texty_input"]'
    send: str = '[data-qa="message_input_container"] button[data-qa="texty_send_button"]'
    blank_class: str = "ql-blank"  # on the box while it's empty


@dataclass(frozen=True)
class WebStatus:
    url: str
    title: str
    draft: bool  # the box isn't empty, so send() would refuse

    def __str__(self) -> str:
        note = " (its message box has a draft, so sending would refuse)" if self.draft else ""
        return f"signed in; {self.title} is open in the dedicated Chrome{note}"


def warn(message: str) -> None:
    print(f"slack web: {message}", file=sys.stderr)


class SlackWeb:
    """An attachment to the Chrome that's signed in to Slack. Use it as a context manager.

    One attachment serves any number of check() and send() calls. Each reuses
    the tab already showing the conversation, or the workspace, before opening
    a new one.
    """

    def __init__(self, cdp_url: str = CDP_URL, *, selectors: Selectors = Selectors(), load_seconds: float = 30,
                 sent_seconds: float = 15, warn: Callable[[str], None] = warn, browser=None):
        self.cdp_url = cdp_url
        self.selectors = selectors
        self.load_seconds = load_seconds  # for a conversation and its message box to appear
        self.sent_seconds = sent_seconds  # for the box to empty after Send
        self.warn = warn
        self.browser = browser            # tests pass a stand-in; otherwise __enter__ attaches
        self._playwright = None

    def __enter__(self):
        if self.browser is None:
            try:
                ensure_window(self.cdp_url)
            except OSError:
                raise SlackWebError("the dedicated Chrome isn't running with its debugging port. "
                                    f"Start it with:\n  {CHROME_COMMAND}") from None
            self._playwright = sync_playwright().start()
            try:
                self.browser = self._playwright.chromium.connect_over_cdp(self.cdp_url)
            except PlaywrightError as e:
                self.__exit__()
                raise SlackWebError(f"couldn't attach to the dedicated Chrome at {self.cdp_url}: {e}") from None
        return self

    def __exit__(self, *exc):
        if self._playwright is not None:  # detaches; Chrome and its tabs stay as they are
            self._playwright.stop()
            self._playwright = None
            self.browser = None

    # MARK: What callers use

    def check(self, conversation: Conversation) -> WebStatus:
        """Opens the conversation and checks it's the right one. Types nothing."""
        page = self.open(conversation)
        return WebStatus(url=page.url, title=page.title(), draft=not self._blank(page))

    def send(self, conversation: Conversation, text: str) -> None:
        """Types `text` into the conversation and clicks Send, once the tab checks out."""
        if not text.strip():
            raise SlackWebError("refusing to send an empty message")
        type_and_send(self.open(conversation), text, self.selectors, self.sent_seconds, self.warn)

    def sign_in(self, conversation: Conversation, sign_in_url: str, wait_seconds: float = 600) -> bool:
        """Makes sure this Chrome is signed in to the conversation's workspace.

        If it isn't, this opens `sign_in_url` (the workspace's own address, like
        https://pjtty.slack.com/), brings the tab to the front, and waits for
        you to sign in. Returns True if it was already signed in.
        """
        page = self._tab(conversation, sign_in_url)
        if not page.url.startswith(conversation.workspace_url):
            page.goto(conversation.url, wait_until="domcontentloaded")
        if self._box_appears(page, self.load_seconds):
            return True
        page.goto(sign_in_url, wait_until="domcontentloaded")
        page.bring_to_front()
        if not self._box_appears(page, wait_seconds, url=conversation.workspace_url):
            raise SlackWebError(f"not signed in to {conversation.team} after {wait_seconds:.0f}s. "
                                f"Sign in at {sign_in_url} in the dedicated Chrome, then try again.")
        return False

    def open(self, conversation: Conversation):
        """The tab showing the conversation, once it's checked. For callers that need the page itself."""
        page = self._tab(conversation)
        if not page.url.startswith(conversation.url):
            page.goto(conversation.url, wait_until="domcontentloaded")
        if not self._box_appears(page, self.load_seconds):
            raise SlackWebError(f"Slack's message box didn't appear at {conversation.url}. If the tab shows a "
                                f"sign-in page, sign in to {conversation.team} there, then try again.")
        check_page(page, conversation, self.selectors)
        return page

    # MARK: Tabs

    def _tab(self, conversation: Conversation, *also: str):
        """The tab already on the conversation, else one on its workspace (or on `also`), else a new one."""
        if self.browser is None:
            raise SlackWebError("use SlackWeb as a context manager: with SlackWeb() as web: …")
        pages = [page for context in self.browser.contexts for page in context.pages]
        for prefix in (conversation.url, conversation.workspace_url, *also):
            for page in pages:
                if page.url.startswith(prefix):
                    return page
        return self.browser.contexts[0].new_page()

    def _box_appears(self, page, seconds: float, *, url: str | None = None) -> bool:
        try:
            if url:
                page.wait_for_url(lambda u: u.startswith(url), timeout=seconds * 1000)
            page.locator(self.selectors.box).first.wait_for(timeout=seconds * 1000)
            return True
        except PlaywrightTimeout:
            return False

    def _blank(self, page) -> bool:
        return page.locator(self.selectors.box).evaluate(is_blank_js(self.selectors))


# MARK: - Page steps (each takes a Playwright page, so they're easy to test)

def ensure_window(cdp_url: str) -> None:
    """Chrome 156 can't be attached to while it has no window, so one is opened if needed."""
    with urllib.request.urlopen(f"{cdp_url}/json/list", timeout=5) as r:
        targets = json.load(r)
    if not any(t.get("type") == "page" for t in targets):
        urllib.request.urlopen(urllib.request.Request(f"{cdp_url}/json/new?about:blank", method="PUT"), timeout=5).close()


def is_blank_js(selectors: Selectors) -> str:
    return f"el => el.classList.contains({selectors.blank_class!r})"


LINES_JS = "el => el.children.length"  # the box holds one paragraph per line


def check_page(page, conversation: Conversation, selectors: Selectors = Selectors()) -> None:
    """Refuses unless the tab shows exactly this conversation, in this workspace."""
    title = page.title()
    box = page.locator(selectors.box)
    problems = []
    if not page.url.startswith(conversation.url):
        problems.append(f"the tab is at {page.url}")
    if not title.endswith(f" - {conversation.team} - Slack"):
        problems.append(f"the page title is {title!r}")
    if box.count() != 1:
        problems.append(f"it has {box.count()} message boxes")
    elif box.get_attribute("aria-label") != f"Message to {conversation.label}" and f"{conversation.label} (DM)" not in title:
        problems.append(f"the message box is labelled {box.get_attribute('aria-label')!r}")
    if problems:
        raise SlackWebError(f"not typing anything: {'; '.join(problems)}. Expected {conversation.label} "
                            f"in {conversation.team} ({conversation.url}).")


def clear(page, selectors: Selectors = Selectors()) -> None:
    page.locator(selectors.box).click()
    page.keyboard.press("Meta+A")
    page.keyboard.press("Backspace")


def type_and_send(page, text: str, selectors: Selectors = Selectors(), sent_seconds: float = 15,
                  warn: Callable[[str], None] = warn) -> None:
    """Types `text` into a checked tab's message box and clicks Send."""
    box = page.locator(selectors.box)
    blank = is_blank_js(selectors)
    if not box.evaluate(blank):
        raise SlackWebError("the message box already has a draft in it (Slack syncs drafts between devices). "
                            "Send or delete it in Slack, then try again.")
    lines = text.split("\n")
    try:
        box.click()
        for i, line in enumerate(lines):
            if i:
                page.keyboard.press("Shift+Enter")  # a new line; Enter alone would send
            if line:
                page.keyboard.insert_text(line)
        if box.evaluate(blank):
            raise SlackWebError("the text didn't go into Slack's message box")
        if (shown := box.evaluate(LINES_JS)) != len(lines):
            warn(f"the message box shows {shown} lines for a {len(lines)}-line message; Slack may have formatted some of it")
        page.wait_for_function(f"() => document.querySelector({selectors.send!r})?.getAttribute('aria-disabled') !== 'true'",
                               timeout=5000)
        page.locator(selectors.send).click()
    except BaseException:
        clear(page, selectors)  # never leave half a message behind as a draft
        raise
    try:
        page.wait_for_function(f"() => document.querySelector({selectors.box!r})?.classList.contains({selectors.blank_class!r})",
                               timeout=sent_seconds * 1000)
    except PlaywrightTimeout:
        raise SlackWebError("clicked Send, but Slack's message box didn't empty. Check the conversation in Chrome.") from None
