"""What the web-app channels share: an agent's own website, driven in the dedicated Chrome.

Wajo (wajo.ai) and Muse (muse.ai) are reached the way Hark is (see hark.py):
a fixed script, never AI clicking. It finds the site's tab in the dedicated
Chrome, fills the agent's message box without clicking it, checks the text,
clicks Send by its name, and reads everything back from the page's own JSON.

This module has the parts that don't depend on the site:

- `SiteWeb`: the attachment to Chrome, the site's tab, and `fill_and_send`.
- `Recorder`: the inbox and state files, "new" and "update" events for
  messages that are new or changed, "typing" events while the agent works,
  and `listen`, which syncs every few seconds.
- `ChannelLock`, so two bench commands never use one site at once.
- Pins: the account signed in the first time, kept in
  ~/Library/Application Support/agent-runner/<site>/pins.json.

Every event has the fields the other channels write (see bench/conversation.py).
"""

import fcntl
import hashlib
import json
import os
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

from bench.channels.chrome import CDP_URL, ChromeError, attach
from bench.config import Agent

TRACK_WINDOW = timedelta(hours=48)  # how long a message is watched for edits
WORK_EVERY = 10                     # seconds between "working" events for one record (the dashboard joins gaps under 15 s)


class SiteError(Exception):
    pass


class SiteNetworkError(SiteError):
    """The site's tab couldn't be read just now. Worth retrying later."""


# MARK: - Times and signatures

def iso(dt: datetime | None) -> str | None:
    return dt.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z") if dt else None


def now_iso() -> str:
    return iso(datetime.now(UTC))


def ms_time(ms) -> str | None:
    return iso(datetime.fromtimestamp(ms / 1000, UTC)) if isinstance(ms, (int, float)) and ms > 0 else None


def iso_time(value) -> str | None:
    """An ISO time from a site, in the form every event uses."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return iso(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        return None


def signature(event: dict) -> str:
    return hashlib.sha1(json.dumps(event, sort_keys=True).encode()).hexdigest()


def on_origin(url: str, origin: str) -> bool:
    return url == origin or url.startswith(origin + "/")


def shows(page_url: str, url: str) -> bool:
    """Whether the tab at `page_url` shows the page `url` (or a page under it)."""
    page, want = urlsplit(page_url), urlsplit(url)
    have, path = page.path.rstrip("/"), want.path.rstrip("/")
    return page.netloc == want.netloc and (have == path or bool(path) and have.startswith(path + "/"))


# MARK: - The site's tab in Chrome

class SiteWeb:
    """One website in the dedicated Chrome. Use it as a context manager.

    It reuses the tab already on the site, or opens one on `home`. Leaving the
    with-block detaches from Chrome, which keeps running with its tabs as they were.
    Subclasses set `origin`, `home`, `name` and `error`.
    """

    origin = ""
    home = ""
    name = ""
    error: type[SiteError] = SiteError
    network_error: type[SiteError] = SiteNetworkError

    def __init__(self, cdp_url: str = CDP_URL, *, browser=None, sent_seconds: float = 15):
        self.cdp_url = cdp_url
        self.browser = browser  # tests pass a stand-in; otherwise __enter__ attaches
        self.sent_seconds = sent_seconds  # for the box to empty after Send
        self._attached = None

    def __enter__(self):
        if self.browser is None:
            self._attached = attach(self.cdp_url)
            try:
                self.browser = self._attached.__enter__()
            except ChromeError as e:
                self._attached = None
                raise self.error(str(e)) from None
        return self

    def __exit__(self, *exc):
        if self._attached is not None:
            self._attached.__exit__(None, None, None)
            self._attached = self.browser = None

    def on_site(self, url: str) -> bool:
        return on_origin(url, self.origin)

    def page(self):
        """The tab on the site, else a new one on its home page."""
        if self.browser is None:
            raise self.error(f"use {type(self).__name__} as a context manager")
        for context in self.browser.contexts:
            for page in context.pages:
                if self.on_site(page.url):
                    return page
        page = self.browser.contexts[0].new_page()
        page.goto(self.home, wait_until="domcontentloaded")
        return page

    def evaluate(self, script: str, arg=None, page=None):
        """Runs `script` in the site's tab, which must be on the site."""
        from playwright.sync_api import Error as PlaywrightError
        page = page or self.page()
        if not self.on_site(page.url):
            raise self.network_error(f"the {self.name} tab is at {page.url.split('?')[0]}, not {self.origin}")
        try:
            return page.evaluate(script, arg)
        except PlaywrightError as e:  # mid-navigation, reloaded or closed
            raise self.network_error(f"couldn't read the {self.name} tab: {str(e).splitlines()[0]}") from None

    def tab_url(self) -> str:
        return self.page().url

    def open(self, url: str, ready: Callable[[], bool], seconds: float = 20,
             sleep: Callable[[float], None] = time.sleep) -> None:
        """Puts the site's tab on `url` if it isn't there, and waits until `ready()`.
        Refuses to leave a page whose message box has text in it."""
        page = self.page()
        if not shows(page.url, url):
            try:
                draft = self.draft()
            except SiteError:
                draft = ""  # this page has no message box, so nothing is lost by leaving it
            if draft:
                raise self.error(f"{self.name}'s message box already has text in it. Send or delete it in Chrome, "
                                 "then try again.")
            page.goto(url, wait_until="domcontentloaded")
        deadline = time.monotonic() + seconds
        while not ready():
            if time.monotonic() > deadline:
                raise self.error(f"{url} didn't finish loading in the dedicated Chrome within {seconds:.0f}s")
            sleep(0.5)

    def draft(self) -> str:
        raise NotImplementedError


def fill_and_send(page, box, send_button, text: str, *, error: type[SiteError], name: str, sent_seconds: float = 15,
                  sleep: Callable[[float], None] = time.sleep) -> None:
    """Fills `box` with `text`, checks it, and clicks `send_button`. Never clicks the box itself.

    `box` and `send_button` are Playwright locators. The send button is found by
    its name, never by position, because the same spot can be the dictation or
    stop button. If anything fails before Send, the box is emptied again.
    """
    from playwright.sync_api import TimeoutError as PlaywrightTimeout
    if not text.strip():
        raise error("refusing to send an empty message")
    if box.input_value():
        raise error(f"{name}'s message box already has text in it. Send or delete it in Chrome, then try again.")
    try:
        box.fill(text)  # focuses without a click: a placeholder or suggestion can lie over the empty box
        if box.input_value() != text:
            raise error(f"the text didn't go into {name}'s message box exactly")
        try:
            send_button.wait_for(state="visible", timeout=10_000)
        except PlaywrightTimeout:
            raise error(f"{name}'s Send button didn't appear") from None
        if not send_button.is_enabled():
            deadline = time.monotonic() + 5
            while not send_button.is_enabled():
                if time.monotonic() > deadline:
                    raise error(f"{name}'s Send button stayed disabled")
                sleep(0.2)
        send_button.click()
    except BaseException:
        box.fill("")  # never leave half a message behind
        raise
    deadline = time.monotonic() + sent_seconds
    while box.input_value():
        if time.monotonic() > deadline:
            raise error(f"clicked Send, but {name}'s message box didn't empty. Check it in the dedicated Chrome.")
        sleep(0.2)


# MARK: - Locks and pins

class ChannelLock:
    """One bench command uses a site at a time, so two never write the same state."""

    def __init__(self, pin_dir: Path, name: str, error: type[SiteError] = SiteError):
        pin_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(pin_dir, 0o700)
        self.path, self.name, self.error = pin_dir / "bench.lock", name, error

    def __enter__(self):
        self.file = self.path.open("w")
        try:
            fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.file.close()
            raise self.error(f"another bench command is using {self.name}; wait for it to finish") from None
        return self

    def __exit__(self, *exc):
        fcntl.flock(self.file, fcntl.LOCK_UN)
        self.file.close()


def load_json(path: Path) -> dict:
    return json.loads(path.read_text()) if path.exists() else {}


def save_pins(path: Path, pins: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(pins, indent=2))
    os.chmod(path, 0o600)


# MARK: - Recording

class Recorder:
    """Writes one site channel's events to its inbox, and remembers what it has recorded.

    State, in the inbox's folder as state.json: `tracked` (each message recorded in
    the last 48 hours, with a signature, so an edit is written as an "update"),
    `working` (when each "typing" event was last written), and whatever the
    channel keeps under `threads`.
    """

    channel = ""
    network_error: type[SiteError] = SiteNetworkError

    def __init__(self, agent: Agent, inbox: Path, *, on_event: Callable[[dict], None] | None = None,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic):
        self.agent = agent
        self.inbox = inbox
        self.state_path = inbox.parent / "state.json"
        self.on_event = on_event
        self.sleep, self.clock = sleep, clock
        self.state: dict = {"threads": {}, "tracked": {}, "working": {}}

    def load_state(self) -> None:
        state = load_json(self.state_path)
        self.state = {"threads": {}, "tracked": {}, "working": {}} | state

    def _save_state(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(self.state))

    def base(self) -> dict:
        return {"channel": self.channel, "agent": self.agent.name, "handle": self.agent.handle}

    def _emit(self, kind: str, event: dict, emitted_at: str | None = None) -> dict:
        line = {"kind": kind, "emitted_at": emitted_at or now_iso(), **event}
        self.inbox.parent.mkdir(parents=True, exist_ok=True)
        with self.inbox.open("a") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
        self._save_state()
        if self.on_event:
            self.on_event(line)
        return line

    def _record(self, event: dict, emitted_at: str | None = None) -> dict | None:
        """Writes a "new" or "update" event if the message is new or changed."""
        tracked = self.state["tracked"].get(event["message_id"])
        if tracked is None and self._seen_before(event):
            return None
        sig = signature(event)
        if tracked and tracked["sig"] == sig:
            return None
        self.state["tracked"][event["message_id"]] = {"sig": sig, "date": event.get("date")}
        return self._emit("update" if tracked else "new", event, emitted_at)

    def _seen_before(self, event: dict) -> bool:
        """Whether an untracked message is one recorded earlier and since let go (older than 48 hours)."""
        return bool(event.get("date")) and event["date"] < iso(datetime.now(UTC) - TRACK_WINDOW)

    def _working(self, record_id: str, action: str = "working", detail: str | None = None,
                 extra: dict | None = None) -> int:
        """A "typing" event while the agent works on something, at most one per record every WORK_EVERY seconds."""
        at = now_iso()
        last = self.state["working"].get(record_id)
        if last and (datetime.fromisoformat(at) - datetime.fromisoformat(last)).total_seconds() < WORK_EVERY:
            return 0
        self.state["working"][record_id] = at
        event = self.base() | {"action": action, "message_id": record_id} \
            | ({"detail": detail} if detail else {}) | (extra or {})
        self._emit("typing", event, at)
        return 1

    def _prune(self) -> None:
        cutoff = iso(datetime.now(UTC) - TRACK_WINDOW)
        tracked = self.state["tracked"]
        for key in [k for k, t in tracked.items() if (t.get("date") or "9") < cutoff]:
            del tracked[key]
        working = self.state["working"]
        for key in [k for k, at in working.items() if at < cutoff]:
            del working[key]

    def sync(self) -> int:
        raise NotImplementedError

    def listen(self, seconds: float | None = None, poll_seconds: float = 3) -> None:
        """Keeps recording for `seconds`, or until interrupted, checking every `poll_seconds`."""
        deadline = None if seconds is None else self.clock() + seconds
        while True:
            wait = poll_seconds if deadline is None else min(poll_seconds, deadline - self.clock())
            if wait > 0:
                self.sleep(wait)
            try:
                self.sync()
            except self.network_error as e:  # the tab was mid-reload or the site hiccuped; the next poll catches up
                print(f"{self.channel}: {e}; trying again in {poll_seconds:.0f}s", file=sys.stderr)
            if deadline is not None and self.clock() >= deadline:
                return
