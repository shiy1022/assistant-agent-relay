"""Muse channel: talks to Muse on muse.ai, in the dedicated Chrome.

Muse's own web app is used instead of WhatsApp, the way Hark's is (see hark.py
and docs/connections/muse-web.md). No AI drives the page: a fixed script fills
Muse's message box and clicks Send.

muse.ai's messages travel over an encrypted connection to Muse's cloud
computer, so they can't be read off the network. Instead the channel reads
the copy muse.ai keeps of the main chat in the browser's own storage
(IndexedDB "hatch-chat-cache", store "main-transcripts"): the latest 40
messages, each with Muse's own ID, role, text, time and whether it's still
streaming. muse.ai rewrites it within a second or two of every change.

What it guarantees:
- It reads and opens only muse.ai. It never navigates a tab that isn't on
  muse.ai, and reads nothing but that cache and the page's message box.
- One Muse account. Whoever is signed in the first time is pinned, with the
  cloud computer Muse runs on, and every later command refuses to run if
  another account is signed in.
- It records the main chat: what you send and Muse's answers, and the
  heads-ups Muse posts there on its own. Messages from before the account was
  pinned are recorded too, as far back as muse.ai's copy goes. Muse's
  WhatsApp chat shows on muse.ai as a read-only side chat, and isn't recorded
  here. Once bench-whatsapp's agent list is rewritten from config/agents.yaml,
  it stops recording Muse too, so send benchmark messages only in the main
  chat (muse.ai or the Muse app).
- It never clicks Muse's message box. The text goes in with fill(), is
  checked, and only then is Send clicked. It never types over text already in
  the box, and clears the box if anything fails before Send.
- Cards Muse shows (options, approvals) are recorded as text, never pressed.

While Muse works on something (its Stop button shows), that's recorded as
"typing" events with action "working".

Times: muse.ai's copy stamps one of Muse's replies with when Muse began the
turn (it has been 0.6 s before the message it answers) until the page next
loads the chat from Muse's servers. So a reply's time is when the runner
first saw it, and never before the message it follows in the chat; for the
messages there before the account was pinned, it's muse.ai's own time.

Sending: muse.ai writes its copy only as Muse finishes its turn, which can
take minutes, but shows the sent message on the page at once, under the ID
the copy gives it later. So a send is confirmed by the page, and recorded
from it with the time it showed; the copy's own time replaces it later.
"""

import sys
import time
from datetime import UTC, datetime, timedelta
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from bench.channels.chrome import CDP_URL
from bench.channels.site import (TRACK_WINDOW, ChannelLock, Recorder, SiteError, SiteNetworkError, SiteWeb, fill_and_send,
                                 iso, iso_time, load_json, now_iso, save_pins, shows)
from bench.config import PRIVATE_DIR, Agent
from bench.inbox import inbox_path

PIN_DIR = PRIVATE_DIR / "muse"
ORIGIN = "https://muse.ai"
HOME = ORIGIN + "/"             # the main chat
POLL_SECONDS = 3                # how often listen checks for new messages
SENT_SECONDS = 45               # how long a sent message may take to show up in the main chat


class MuseError(SiteError):
    pass


class MuseNetworkError(MuseError, SiteNetworkError):
    """The muse.ai tab couldn't be read just now. Worth retrying later."""


@dataclass(frozen=True)
class Selectors:
    """Where things are on muse.ai. If Muse changes its page, this is the one place to update.
    Chrome here is set to Chinese, so muse.ai's labels are in Chinese; the English ones are kept too."""

    box_names: tuple[str, ...] = ("消息", "Message")
    send_names: tuple[str, ...] = ("发送", "发送消息", "Send", "Send message")
    stop_names: tuple[str, ...] = ("停止", "Stop")


READ_JS = """async ({stopNames}) => {
  const out = {path: location.pathname};
  const db = await new Promise((res, rej) => {
    const q = indexedDB.open('hatch-chat-cache');
    q.onsuccess = () => res(q.result); q.onerror = () => rej(q.error);
    q.onupgradeneeded = () => { q.transaction.abort(); rej(new Error('muse.ai has no chat cache in this Chrome yet')); };
  });
  try {
    const all = (s) => new Promise((res, rej) => {
      if (!db.objectStoreNames.contains(s)) return res([]);
      const q = db.transaction(s, 'readonly').objectStore(s).getAll(); q.onsuccess = () => res(q.result); q.onerror = () => rej(q.error);
    });
    out.main = (await all('main-transcripts')).map(t => ({
      key: t.cacheKey, cachedAtMs: t.cachedAtMs, revision: t.transcriptRevision, hasMore: t.hasMore,
      messages: (t.messageIds || []).map(id => t.messagesById[id]).filter(Boolean)}));
    out.sides = (await all('side-chat-lists')).map(l => ({key: l.cacheKey, sessions: l.sessions || []}));
  } finally { db.close(); }
  out.busy = [...document.querySelectorAll('button')].some(b => stopNames.includes(b.getAttribute('aria-label') || ''));
  // The latest messages the page shows. muse.ai shows a message you send at once, under the ID its copy
  // gives it later, but writes the copy only as Muse finishes its turn, which can take minutes.
  out.shown = [...document.querySelectorAll('[data-message-id][data-message-role]')].slice(-8).map(e => ({
    id: e.getAttribute('data-message-id'), role: e.getAttribute('data-message-role'), text: e.innerText || ''}));
  out.signedIn = !!document.querySelector('textarea');
  return out;
}"""


# MARK: - muse.ai in Chrome

class MuseWeb(SiteWeb):
    """muse.ai in the dedicated Chrome. Use it as a context manager."""

    origin, home, name = ORIGIN, HOME, "Muse"
    error, network_error = MuseError, MuseNetworkError

    def __init__(self, cdp_url: str = CDP_URL, *, selectors: Selectors = Selectors(), **kw):
        super().__init__(cdp_url, **kw)
        self.selectors = selectors

    def read(self) -> dict:
        """muse.ai's copy of the main chat, its side chats, whether Muse is busy, and whether the page has a message box.

        {"main": [{key, cachedAtMs, revision, messages: [...]}], "sides": [{key, sessions}], "busy", "signedIn"}
        A transcript's key starts with the account's ID, then a NUL, then the address of Muse's cloud computer.
        """
        try:
            return self.evaluate(READ_JS, {"stopNames": list(self.selectors.stop_names)})
        except MuseNetworkError as e:
            if "no chat cache" in str(e):
                raise MuseError("muse.ai hasn't shown its chat in the dedicated Chrome yet. Run: uv run bench muse-login") from None
            raise

    def account(self) -> dict | None:
        """The signed-in account, as muse.ai's chat copy names it: {"id", "computer"}, or None if no chat is shown."""
        data = self.read()
        if not data.get("signedIn"):
            return None
        return account_of(data)

    def box(self, page=None):
        page = page or self.page()
        for name in self.selectors.box_names:
            box = page.get_by_role("textbox", name=name, exact=True)
            if box.count() == 1:
                return box
        box = page.locator("textarea")
        if box.count() != 1:
            raise MuseError(f"found {box.count()} message boxes on {page.url.split('?')[0]}, not one. "
                            "If the tab shows a sign-in page, run: uv run bench muse-login")
        return box

    def send_button(self, page=None):
        page = page or self.page()
        button = None
        for name in self.selectors.send_names:
            b = page.get_by_role("button", name=name, exact=True)
            button = b if button is None else button.or_(b)
        return button

    def draft(self) -> str:
        return self.box().input_value()

    def send(self, text: str, sleep: Callable[[float], None] = time.sleep) -> None:
        """Types `text` into the main chat, opening it in the Muse tab first if needed."""
        page = self.page()

        def ready():
            try:
                self.box(page)
                return True
            except MuseError:
                return False
        self.open(HOME, ready, sleep=sleep)
        fill_and_send(page, self.box(page), self.send_button(page), text, error=MuseError, name="Muse",
                      sent_seconds=self.sent_seconds, sleep=sleep)

    def sign_in(self, wait_seconds: float = 600, sleep: Callable[[float], None] = time.sleep,
                clock: Callable[[], float] = time.monotonic) -> bool:
        """Makes sure this Chrome is signed in to Muse, waiting while you sign in. True if it already was."""
        page = self.page()
        if self.signed_in():
            return True
        if not shows(page.url, HOME):
            page.goto(HOME, wait_until="domcontentloaded")
        page.bring_to_front()
        deadline = clock() + wait_seconds
        while clock() < deadline:
            sleep(2)
            if self.on_site(page.url) and self.signed_in():  # signing in passes through Meta's pages
                return False
        raise MuseError(f"not signed in to Muse after {wait_seconds:.0f}s. Sign in at {HOME} "
                        "in the dedicated Chrome, then try again.")

    def signed_in(self) -> bool:
        try:
            return self.account() is not None
        except SiteError:
            return False


def account_of(data: dict) -> dict | None:
    """The account and cloud computer muse.ai's main chat copy belongs to (the newest, if there are several)."""
    main = sorted(data.get("main") or [], key=lambda t: t.get("cachedAtMs") or 0)
    if not main:
        return None
    account, _, source = (main[-1].get("key") or "").partition("\0")
    computer = source.split("wss://", 1)[-1].split(".", 1)[0] if "wss://" in source else None
    return {"id": account, "computer": computer} if account else None


def transcript(data: dict, pins: dict) -> dict | None:
    """The pinned account's main chat, from muse.ai's copy."""
    mine = [t for t in data.get("main") or [] if (t.get("key") or "").partition("\0")[0] == pins.get("account_id")
            and (not pins.get("computer") or pins["computer"] in (t.get("key") or ""))]
    return max(mine, key=lambda t: t.get("cachedAtMs") or 0) if mine else None


# MARK: - Muse's records → events

def card_text(msg: dict) -> str | None:
    """What a card (widget or presentation) Muse showed says, when the message has no text of its own."""
    parts = []
    for card in (msg.get("widgets") or []) + (msg.get("presentations") or []):
        if isinstance(card, dict):
            text = card.get("displayText") or card.get("title")
            if text:
                parts.append(str(text))
    return "\n".join(parts) or None


def cards(msg: dict) -> list[list[dict]] | None:
    """The cards on a message, as a row of buttons: recorded, never pressed."""
    row = [{"text": str(c.get("title") or c.get("displayText") or c.get("kind") or "?")[:200],
            "kind": str(c.get("kind") or c.get("template") or "card")}
           for c in (msg.get("widgets") or []) + (msg.get("presentations") or []) if isinstance(c, dict)]
    return [row] if row else None


def message_event(msg: dict, agent: Agent) -> dict:
    """One Muse message in the shared event format, without kind and emitted_at."""
    reply_to = msg.get("replyToMessageId")
    event = {
        "channel": "muse",
        "agent": agent.name,
        "handle": agent.handle,
        "guid": f"muse:{msg['id']}",
        "message_id": msg["id"],
        "is_from_me": msg.get("role") == "user",
        "text": msg.get("content") or card_text(msg),
        "date": iso_time(msg.get("createdAt")),
        "rich": True if msg.get("widgets") or msg.get("presentations") or msg.get("images") or msg.get("files") else None,
        "buttons": cards(msg),
        "streaming": True if msg.get("isTextStreaming") else None,
        # Muse names the message it answers by its ID without the "assistant-msg-" prefix, sometimes its own
        "reply_to": reply_to if reply_to and reply_to not in str(msg["id"]) else None,
        "source": msg.get("source"),
    }
    return {k: v for k, v in event.items() if v is not None}


# MARK: - Channel

class MuseChannel(Recorder):
    """A muse.ai session that records Muse's main chat, and sends there.

        with MuseChannel(agents) as muse:
            muse.sync()
            muse.send(agent, "hi")
            muse.listen(120)
    """

    channel = "muse"
    network_error = MuseNetworkError

    def __init__(self, agents: list[Agent], *, site=None, pin_dir: Path = PIN_DIR, inbox: Path | None = None, **kw):
        muse = [a for a in agents if a.channel == "muse"]
        if not muse:
            raise MuseError("no agent in config/agents.yaml has channel: muse and a handle")
        if len(muse) > 1:
            raise MuseError(f"only one Muse agent is supported, since there's one signed-in account; "
                            f"found {', '.join(a.name for a in muse)}")
        super().__init__(muse[0], inbox or inbox_path("muse"), **kw)
        self.site = site
        self._own_site = None
        self.pin_dir = pin_dir
        self.pins_path = pin_dir / "pins.json"
        self.pins: dict = {}
        self._lock: ChannelLock | None = None

    def __enter__(self):
        self._lock = ChannelLock(self.pin_dir, "Muse", MuseError).__enter__()
        try:
            if self.site is None:
                self.site = self._own_site = MuseWeb().__enter__()
            self.pins = connect(self.site, self.pins_path)
            self.load_state()
        except BaseException:
            self.__exit__()
            raise
        return self

    def __exit__(self, *exc):
        if self._own_site is not None:
            self._own_site.__exit__()
            self._own_site = self.site = None
        if self._lock is not None:
            self._lock.__exit__()
            self._lock = None

    def _seen_before(self, event: dict) -> bool:
        if not self.state["threads"].get("main", {}).get("synced"):
            return False  # the first sync records everything muse.ai's copy holds
        return super()._seen_before(event)

    def _transcript(self, data: dict) -> dict:
        t = transcript(data, self.pins)
        if t is None:
            raise MuseNetworkError("muse.ai's copy of the main chat isn't there for the pinned account right now")
        return t

    def _dated(self, event: dict, after: str | None, live: bool) -> dict:
        """The event with its time settled: a reply first seen live is dated when it was first seen, and
        nothing is dated before the message it follows in the chat (`after`)."""
        seen = self.state.setdefault("seen", {})
        mid = event["message_id"]
        if live and not event["is_from_me"] and mid not in self.state["tracked"] and mid not in seen \
                and not self._seen_before(event):  # new to the runner, not an old one it stopped watching after 48 h
            seen[mid] = now_iso()
        date = max(filter(None, [event.get("date"), seen.get(mid)]), default=None)
        if after and (not date or date <= after):
            date = iso(datetime.fromisoformat(after.replace("Z", "+00:00")) + timedelta(milliseconds=1))
        return event | ({"date": date} if date else {})

    def sync(self) -> int:
        """Records what arrived or changed in the main chat since last time. Returns events written."""
        data = self.site.read()
        t = self._transcript(data)
        live = bool(self.state["threads"].get("main", {}).get("synced"))
        written, after = 0, None
        for msg in t["messages"]:
            if not msg.get("id") or (msg.get("role") != "user" and not message_event(msg, self.agent).get("text")):
                continue  # an empty record Muse fills in later
            event = self._dated(message_event(msg, self.agent), after, live)
            after = event.get("date") or after
            written += 1 if self._record(event) else 0
        if data.get("busy"):
            written += self._working("main", "working")
        self.state["threads"]["main"] = {"synced": True, "revision": t.get("revision"), "cached_at": t.get("cachedAtMs")}
        self._prune()
        seen = self.state.get("seen") or {}
        for mid in [m for m, at in seen.items() if m not in self.state["tracked"] and at < iso(datetime.now(UTC) - TRACK_WINDOW)]:
            del seen[mid]
        self._save_state()
        return written

    def send(self, agent: Agent, text: str) -> dict | None:
        """Sends `text` in the main chat, then finds and records it: from muse.ai's copy if it's there
        already, else from the page, which shows it at once. If neither shows it but Muse is working,
        it's taken as sent, and recorded from the copy by a later sync."""
        if agent != self.agent:
            raise MuseError(f"{agent.name} isn't the Muse agent in config/agents.yaml")
        if not text.strip():
            raise MuseError("refusing to send an empty message")
        data = self.site.read()
        before = {m.get("id") for m in self._transcript(data)["messages"]} | {m.get("id") for m in data.get("shown") or []}
        self.site.send(text)
        deadline = self.clock() + SENT_SECONDS
        busy = False
        while True:
            try:
                data = self.site.read()
                messages = self._transcript(data)["messages"]
            except MuseNetworkError:
                data, messages = {}, []
            mine = [m for m in messages if m.get("id") not in before and m.get("role") == "user"]
            if mine:
                msg = next((m for m in mine if (m.get("content") or "").strip() == text.strip()), mine[-1])
                if (msg.get("content") or "").strip() != text.strip():
                    print(f"muse: Muse shows the sent message as {msg.get('content')!r}", file=sys.stderr)
                return self._record(message_event(msg, self.agent))
            shown = [m for m in data.get("shown") or [] if m.get("id") not in before and m.get("role") == "user"
                     and (m.get("text") or "").strip().endswith(text.strip()[-200:])]
            if shown:  # the page shows it; muse.ai's copy catches up when Muse finishes its turn
                return self._record(message_event({"id": shown[-1]["id"], "role": "user", "content": text,
                                                   "createdAt": now_iso(), "source": "runtime"}, self.agent))
            busy = busy or bool(data.get("busy"))
            if self.clock() >= deadline:
                if busy:
                    print(f"muse: muse.ai hasn't shown the sent message within {SENT_SECONDS}s, but Muse is working, "
                          "so it went; it's recorded once muse.ai's copy has it", file=sys.stderr)
                    return None
                raise MuseError(f"clicked Send, but the message didn't show up in Muse's main chat within {SENT_SECONDS}s, "
                                f"and Muse isn't working. Check {HOME} in the dedicated Chrome.")
            self.sleep(1)

    def listen(self, seconds: float | None = None, poll_seconds: float = POLL_SECONDS) -> None:
        super().listen(seconds, poll_seconds)


# MARK: - Account, sign-in and status

def connect(site, pins_path: Path) -> dict:
    """Checks who's signed in to Muse before anything else, and pins that account and its cloud computer."""
    who = site.account()
    if not who:
        raise MuseError("nobody is signed in to muse.ai in the dedicated Chrome, or its chat isn't showing. "
                        "Run: uv run bench muse-login")
    pins = load_json(pins_path)
    if pins.get("account_id") and pins["account_id"] != who["id"]:
        raise MuseError(f"muse.ai is signed in as account {who['id']}, but agent-runner is pinned to {pins['account_id']}. "
                        f"Nothing was read or sent. Sign in to that account, or, if the account really changed, "
                        f"delete {pins_path}.")
    if not pins.get("account_id"):
        pins = {"account_id": who["id"], "computer": who.get("computer"), "since": now_iso()}
        save_pins(pins_path, pins)
    return pins


def sign_in(*, pin_dir: Path = PIN_DIR, site=None) -> str:
    """Makes sure the dedicated Chrome is signed in to Muse, waiting while you sign in, then pins the account."""
    print("Checking that the dedicated Chrome is signed in to Muse. If it isn't, sign in there.", flush=True)
    own = site is None
    site = site or MuseWeb().__enter__()
    try:
        already = site.sign_in()
        with ChannelLock(pin_dir, "Muse", MuseError):
            pins = connect(site, pin_dir / "pins.json")
    finally:
        if own:
            site.__exit__()
    return (f"{'Already signed' if already else 'Signed'} in to Muse (account {pins['account_id']}) in the dedicated "
            "Chrome.")


def status(*, pin_dir: Path = PIN_DIR, site=None) -> str:
    """Checks the sign-in, the pinned account, the main chat and the message box, without sending or recording."""
    own = site is None
    with ChannelLock(pin_dir, "Muse", MuseError):
        site = site or MuseWeb().__enter__()
        try:
            data = site.read()
            who = account_of(data) if data.get("signedIn") else None
            if not who:
                return "Muse: nobody is signed in to muse.ai in the dedicated Chrome. Run: uv run bench muse-login"
            pins = load_json(pin_dir / "pins.json")
            lines = [f"Muse: signed in as account {who['id']} (cloud computer {who.get('computer')})"]
            if pins.get("account_id") and pins["account_id"] != who["id"]:
                lines.append(f"NOT READY: agent-runner is pinned to account {pins['account_id']}, so nothing would be read or sent")
                return "\n".join(lines)
            if not pins:
                lines.append("Not pinned yet. The first sync pins this account.")
            t = transcript(data, pins or {"account_id": who["id"]})
            if t:
                last = (t["messages"] or [{}])[-1]
                lines.append(f"Main chat: {len(t['messages'])} messages in muse.ai's copy, the latest at "
                             f"{iso_time(last.get('createdAt'))} ({'Muse is working on something' if data.get('busy') else 'idle'})")
            sides = [s for lst in data.get("sides") or [] for s in lst["sessions"] if not s.get("is_primary")]
            for s in sides:
                note = "read-only on muse.ai; not recorded" if s.get("channel") == "whatsapp" else "not recorded"
                lines.append(f"Side chat: {s.get('title')!r} ({note})")
            tab = "showing it" if shows(site.tab_url(), HOME) and data.get("path") == "/" else \
                "showing another page; sending opens the main chat first"
            lines.append(f"Sending to: the main chat (the Muse tab is {tab})")
            try:
                lines.append("Message box: " + ("has text in it, so sending would refuse" if site.draft() else "found, empty"))
            except SiteError as e:
                lines.append(f"Message box: NOT READY: {e}")
            return "\n".join(lines)
        finally:
            if own:
                site.__exit__()
