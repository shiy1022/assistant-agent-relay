"""Hark channel: talks to Hark on hark.com, in the dedicated Chrome.

Hark has no API, Mac app or number to text, so its own web app is the way in
(see ../HARK_CONNECTION.md). No AI drives the page: a fixed script fills Hark's
message box and clicks Send. Everything is read back from the JSON hark.com's
own page loads (/api/messages/history), fetched from inside the page with its
sign-in, so every message carries Hark's own ID and server time.

What it guarantees:
- It reads and opens only hark.com. It never navigates a tab that isn't on
  hark.com, and it only GETs hark.com's own /api/ paths.
- One Hark account. Whoever is signed in the first time is pinned, with that
  account's main thread, and every later command refuses to run if another
  account is signed in. Projects are separate threads and aren't touched.
- It never clicks Hark's message box: Hark lays a tappable suggested prompt
  (like "Get a bánh mì off DoorDash") over the empty box. The text goes in with
  fill(), is checked, and only then is Send clicked. It never types over text
  already in the box, and clears the box if anything fails before Send.
- Buttons on Hark's messages (connect, approve, book) are recorded, never pressed.
- A Page Hark builds (a website, shown as a card in the chat) is recorded as a
  message with its name and its hark.com/site/<project> link, and a copy of its
  HTML, with its images and fonts embedded, is saved under the inbox's
  attachments/, since the link needs Hark's
  sign-in. Hark delivers it as an empty record with a website_coding_agent tool
  call, which otherwise looks like work.

Hark's empty "work" records (an assistant message with no text yet, while Hark
thinks or works in its browser) are recorded as "typing" events with action
"working", and live browsing windows as action "browsing", so a stretch of work
shows like a stretch of typing.

Hark keeps the thread on its servers, so nothing has to run in the
background: `sync` records whatever arrived or changed since last time, and
`listen` polls every few seconds.
"""

import fcntl
import hashlib
import json
import os
import re
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from bench.channels.chrome import CDP_URL, ChromeError, attach
from bench.config import PRIVATE_DIR, Agent
from bench.deliverables import freeze
from bench.inbox import inbox_path

PIN_DIR = PRIVATE_DIR / "hark"
ORIGIN = "https://hark.com"
CHAT_URL = ORIGIN + "/chat"
SITE_URL = ORIGIN + "/site/{project_id}"  # where hark.com shows a Page it built; needs Hark's sign-in
TRACK_WINDOW = timedelta(hours=48)  # how long a message is watched for edits
POLL_SECONDS = 3                    # how often listen checks for new messages
WORK_EVERY = 10                     # seconds between "working" events for one record (the dashboard joins gaps under 15 s)
SENT_SECONDS = 30                   # how long a sent message may take to show up in Hark's thread
MAX_PAGES = 200                     # history pages read in one go, catching up
DONE = {"completed", "failed", "cancelled", "canceled", "error", "stopped", "done"}


class HarkError(Exception):
    pass


class HarkNetworkError(HarkError):
    """The Hark tab couldn't be read just now. Worth retrying later."""


@dataclass(frozen=True)
class Selectors:
    """Where things are on hark.com. If Hark changes its page, this is the one place to update."""

    box_name: str = "Message input"                 # the message box, by its accessible name
    box_fallback: str = "[data-testid=chat-input]"
    send_name: str = "Send message"                 # the Send button, which shows only once the box has text
    send_name_zh: str = "发送消息"                   # the same button when Chrome's language is Chinese


FETCH_JS = """async ({path, params}) => {
  const query = params ? '?' + new URLSearchParams(params) : '';
  const r = await fetch(path + query, {credentials: 'include', headers: {accept: 'application/json'}});
  return {status: r.status, text: await r.text()};
}"""


TEXT_JS = """async (path) => {
  const r = await fetch(path, {credentials: 'include'});
  return {status: r.status, type: r.headers.get('content-type') || '', text: await r.text()};
}"""


def on_hark(url: str) -> bool:
    return url == ORIGIN or url.startswith(ORIGIN + "/")


# MARK: - hark.com in Chrome

class HarkWeb:
    """hark.com in the dedicated Chrome. Use it as a context manager.

    It reuses the tab already on hark.com, or opens one on hark.com/chat.
    Leaving the with-block detaches from Chrome, which keeps running.
    """

    def __init__(self, cdp_url: str = CDP_URL, *, selectors: Selectors = Selectors(), sent_seconds: float = 15,
                 browser=None):
        self.cdp_url = cdp_url
        self.selectors = selectors
        self.sent_seconds = sent_seconds  # for the box to empty after Send
        self.browser = browser            # tests pass a stand-in; otherwise __enter__ attaches
        self._attached = None

    def __enter__(self):
        if self.browser is None:
            self._attached = attach(self.cdp_url)
            try:
                self.browser = self._attached.__enter__()
            except ChromeError as e:
                self._attached = None
                raise HarkError(str(e)) from None
        return self

    def __exit__(self, *exc):
        if self._attached is not None:
            self._attached.__exit__(None, None, None)
            self._attached = self.browser = None

    def page(self):
        """The tab on hark.com, else a new one on hark.com/chat."""
        if self.browser is None:
            raise HarkError("use HarkWeb as a context manager: with HarkWeb() as web: …")
        for context in self.browser.contexts:
            for page in context.pages:
                if on_hark(page.url):
                    return page
        page = self.browser.contexts[0].new_page()
        page.goto(CHAT_URL, wait_until="domcontentloaded")
        return page

    def get(self, path: str, page=None, **params):
        """GETs one of hark.com's own /api/ paths from inside the Hark tab, with its sign-in."""
        from playwright.sync_api import Error as PlaywrightError
        if not path.startswith("/api/"):
            raise HarkError(f"not fetching {path}: only hark.com's own /api/ paths are read")
        page = page or self.page()
        if not on_hark(page.url):
            raise HarkNetworkError(f"the Hark tab is at {page.url.split('?')[0]}, not hark.com")
        query = {k: str(v) for k, v in params.items() if v is not None} or None
        try:
            result = page.evaluate(FETCH_JS, {"path": path, "params": query})
        except PlaywrightError as e:  # mid-navigation, reloaded or closed
            raise HarkNetworkError(f"couldn't read {path} from the Hark tab: {str(e).splitlines()[0]}") from None
        if result["status"] in (401, 403):
            raise HarkError("hark.com says nobody is signed in. Run: uv run bench hark-login")
        if result["status"] != 200:
            raise HarkNetworkError(f"hark.com answered {path} with HTTP {result['status']}")
        try:
            return json.loads(result["text"])
        except ValueError:
            raise HarkNetworkError(f"hark.com's answer to {path} isn't JSON") from None

    def text(self, path: str, page=None) -> str:
        """The text (a page's HTML) at one of hark.com's own /api/ paths, fetched from inside the Hark tab.

        hark.com answers /api/files/… by redirecting to a signed copy of the file; the
        browser follows that itself, so only hark.com is ever asked.
        """
        from playwright.sync_api import Error as PlaywrightError
        if not path.startswith("/api/"):
            raise HarkError(f"not fetching {path}: only hark.com's own /api/ paths are read")
        page = page or self.page()
        if not on_hark(page.url):
            raise HarkNetworkError(f"the Hark tab is at {page.url.split('?')[0]}, not hark.com")
        try:
            result = page.evaluate(TEXT_JS, path)
        except PlaywrightError as e:
            raise HarkNetworkError(f"couldn't read {path} from the Hark tab: {str(e).splitlines()[0]}") from None
        if result["status"] in (401, 403):
            raise HarkError("hark.com says nobody is signed in. Run: uv run bench hark-login")
        if result["status"] != 200:
            raise HarkNetworkError(f"hark.com answered {path.split('?')[0]} with HTTP {result['status']}")
        return result["text"]

    def session(self, page=None) -> dict | None:
        """The signed-in Hark user ({id, email, name, …}), or None."""
        return (self.get("/api/auth/get-session", page) or {}).get("user")

    def conversations(self) -> list[dict]:
        return self.get("/api/conversations").get("conversations") or []

    def history(self, conversation_id: str, *, after: str | None = None, before: dict | None = None) -> dict:
        """The latest page of a thread, or the page after a message ID or before a message."""
        # No "v=2": since 2026-10-28 that variant answers without any messages, and paging under it gives 404.
        params = {"conversationId": conversation_id, "after": after}
        if before:
            params |= {"before": before["id"], "beforeCreatedAt": before["timestamp"]}
        return self.get("/api/messages/history", **params)

    def meter(self) -> dict | None:
        """Hark's own token meter: dailyUsed, dailyLimit, poolUsed, poolLimit and when each resets."""
        return (self.get("/api/billing/summary").get("meters") or {}).get("harkTokens")

    def draft(self) -> str:
        """What's in Hark's message box right now."""
        return message_box(self.page(), self.selectors).input_value()

    def send(self, text: str) -> None:
        type_and_send(self.page(), text, self.selectors, self.sent_seconds)

    def sign_in(self, wait_seconds: float = 600, sleep: Callable[[float], None] = time.sleep,
                clock: Callable[[], float] = time.monotonic) -> bool:
        """Makes sure this Chrome is signed in to Hark, waiting while you sign in. True if it already was."""
        page = self.page()
        if self.session(page):
            return True
        if not page.url.startswith(CHAT_URL):
            page.goto(CHAT_URL, wait_until="domcontentloaded")
        page.bring_to_front()
        deadline = clock() + wait_seconds
        while clock() < deadline:
            sleep(2)
            if on_hark(page.url):  # signing in passes through Google or Apple; only hark.com is read
                try:
                    if self.session(page):
                        return False
                except HarkNetworkError:
                    pass
        raise HarkError(f"not signed in to Hark after {wait_seconds:.0f}s. Sign in at {CHAT_URL} "
                        "in the dedicated Chrome, then try again.")


def message_box(page, selectors: Selectors = Selectors()):
    box = page.get_by_role("textbox", name=selectors.box_name, exact=True)
    if box.count() != 1:
        box = page.locator(selectors.box_fallback)
    if box.count() != 1:
        raise HarkError(f"found {box.count()} message boxes on {page.url.split('?')[0]}, not one. "
                        "If the tab shows a sign-in page, run: uv run bench hark-login")
    return box


def type_and_send(page, text: str, selectors: Selectors = Selectors(), sent_seconds: float = 15,
                  sleep: Callable[[float], None] = time.sleep) -> None:
    """Fills Hark's message box with `text`, checks it, and clicks Send. Never clicks the box itself."""
    from playwright.sync_api import TimeoutError as PlaywrightTimeout
    if not text.strip():
        raise HarkError("refusing to send an empty message")
    if not on_hark(page.url):
        raise HarkError(f"not typing anything: the tab is at {page.url.split('?')[0]}, not hark.com")
    box = message_box(page, selectors)
    if box.input_value():
        raise HarkError("Hark's message box already has text in it. Send or delete it in Chrome, then try again.")
    try:
        box.fill(text)  # focuses without a click; the empty box is covered by a tappable suggested prompt
        if box.input_value() != text:
            raise HarkError("the text didn't go into Hark's message box exactly")
        # By name, never by position: empty or busy, the same spot is the dictation or stop button
        send = page.get_by_role("button", name=selectors.send_name, exact=True).or_(
            page.get_by_role("button", name=selectors.send_name_zh, exact=True))
        try:
            send.wait_for(state="visible", timeout=10_000)
        except PlaywrightTimeout:
            raise HarkError("Hark's Send button didn't appear") from None
        send.click()
    except BaseException:
        box.fill("")  # never leave half a message behind
        raise
    deadline = time.monotonic() + sent_seconds
    while box.input_value():
        if time.monotonic() > deadline:
            raise HarkError("clicked Send, but Hark's message box didn't empty. Check hark.com/chat in Chrome.")
        sleep(0.2)


# MARK: - Hark's records → events

def iso(dt: datetime | None) -> str | None:
    return dt.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z") if dt else None


def now_iso() -> str:
    return iso(datetime.now(UTC))


def ms_time(ms) -> str | None:
    return iso(datetime.fromtimestamp(ms / 1000, UTC)) if isinstance(ms, (int, float)) else None


def ordered(history: dict) -> list[dict]:
    """A history page's messages, in the thread's order.

    hark.com has served two shapes: a dict of messages by id plus a "log" giving the order (until
    2026-10-28), and since then a list already in order. Both are read."""
    messages = history.get("messages") or {}
    if isinstance(messages, list):
        return sorted((m for m in messages if isinstance(m, dict) and m.get("id")), key=lambda m: m.get("timestamp") or 0)
    return [messages[e["id"]] for e in history.get("log") or [] if e.get("kind") == "message" and e.get("id") in messages]


def ids(history: dict) -> set[str]:
    """The ids of a history page's messages, whichever shape the page has."""
    messages = history.get("messages") or {}
    if isinstance(messages, list):
        return {m["id"] for m in messages if isinstance(m, dict) and m.get("id")}
    return set(messages)


def running(item: dict) -> bool:
    status = item.get("jobStatus")
    return bool(item.get("isStreaming") or item.get("live") or (status and status not in DONE))


def is_work(msg: dict) -> bool:
    """An empty assistant record that stands for Hark thinking or working, not a message."""
    return msg.get("role") != "user" and not msg.get("content") and not msg.get("actions") and not pages(msg)


def pages(msg: dict) -> list[dict]:
    """The Pages (websites Hark built) a record carries, as hark.com finds them for its website card:
    a tool call whose result names a project and whose artifacts include website/….html."""
    found = []
    for call in msg.get("toolCalls") or []:
        result = call.get("result") if isinstance(call.get("result"), dict) else {}
        html = [a for a in call.get("artifactKeys") or []
                if isinstance(a, dict) and (a.get("path") or "").startswith("website/") and a["path"].endswith(".html")]
        if not (isinstance(result.get("project_id"), str) and html):
            continue
        index = next((a for a in html if a["path"] == "website/index.html"), html[0])
        name = (call.get("input") or {}).get("project_name") or result.get("project_name") or "Page"
        found.append({"project_id": result["project_id"], "name": str(name),
                      "url": SITE_URL.format(project_id=result["project_id"]),
                      "download": index.get("download_url"),
                      "built_at": ms_time(call.get("updatedAt") or call.get("dispatchedAt"))})
    return found


def safe_name(name: str) -> str:
    name = re.sub(r"[^A-Za-z0-9._ -]", "_", name).strip(". ")[:120]
    return name or "page"


def buttons(actions: list[dict] | None) -> list[list[dict]] | None:
    row = [{"text": a.get("label") or a.get("kind") or "?", "kind": a.get("kind", "button")} for a in actions or []]
    return [row] if row else None


def ornament(presentation: dict | None) -> str | None:
    """The caption Hark puts under a rich card (a table, an order summary), which isn't in `content`.

    It lives at presentation.ornament or presentation.<kind>.ornament, like
    {"kind": "table", "table": {"rows": …, "ornament": "I'd go with the Four Seasons."}}.
    """
    if not isinstance(presentation, dict):
        return None
    inner = presentation.get(presentation.get("kind") or "")
    for holder in (presentation, inner if isinstance(inner, dict) else {}):
        text = holder.get("ornament")
        if isinstance(text, str) and text.strip():
            return text.strip()
    return None


def card(presentation: dict | None) -> dict | None:
    """The structured card behind a rich Hark message, trimmed to what the console draws: an order summary
    (merchant, items, breakdown, total, how it's paid, the button's label), a table, or a titled summary.
    Image addresses are left out. The button is recorded by its label only; it's never pressed."""
    if not isinstance(presentation, dict):
        return None
    kind = presentation.get("kind")
    inner = presentation.get(kind) if isinstance(presentation.get(kind), dict) else {}
    text = lambda v: v.strip() if isinstance(v, str) and v.strip() else None
    if kind == "order_summary":
        action = inner.get("action") if isinstance(inner.get("action"), dict) else {}
        out = {
            "kind": kind,
            "title": text(presentation.get("title")) or text(inner.get("merchant")),
            "merchant": text(inner.get("merchant")),
            "site": text(inner.get("site")),
            "destination": text(inner.get("destination")),
            "line_items": [{k: text(i.get(k)) for k in ("name", "qty", "price", "description") if text(i.get(k))}
                           for i in inner.get("line_items") or [] if isinstance(i, dict)],
            "breakdown": [{"label": text(b.get("label")), "amount": text(b.get("amount"))}
                          for b in inner.get("breakdown") or [] if isinstance(b, dict) and text(b.get("label"))],
            "total": text(inner.get("total")),
            "payment": text(inner.get("payment")),
            "ornament": text(inner.get("ornament")),
            "action": text(action.get("label")),
        }
    elif kind == "table":
        out = {
            "kind": kind,
            "title": text(presentation.get("title")) or text(inner.get("title")),
            "columns": [str(c) for c in inner.get("columns") or []],
            "rows": [[str(c) for c in r] for r in inner.get("rows") or [] if isinstance(r, list)],
            "ornament": text(inner.get("ornament")),
        }
    elif kind == "summary":
        out = {"kind": kind, "title": text(presentation.get("title")) or text(inner.get("title")),
               "markdown": text(inner.get("markdown"))}
    else:
        return None
    return {k: v for k, v in out.items() if v not in (None, [], "")}


def message_event(msg: dict, agent: Agent, attachments: list[dict] | None = None) -> dict:
    """One Hark message in the shared event format, without kind and emitted_at.
    `attachments` are the files saved for it, like the copy of a Page (see HarkChannel._page_files)."""
    presentation = (msg.get("presentation") or {}).get("kind")
    text = msg.get("content") or ""
    caption = ornament(msg.get("presentation"))
    if caption and caption not in text:
        text = f"{text}\n\n{caption}" if text else caption
    found = pages(msg)
    for page in found:  # the card hark.com shows: the Page's name, and where it opens
        text = "\n\n".join(filter(None, [text, f"Page: {page['name']}\n{page['url']}"]))
    event = {
        "channel": "hark",
        "agent": agent.name,
        "handle": agent.handle,
        "guid": f"hark:{msg['id']}",
        "message_id": msg["id"],
        # Hark drops product announcements into the thread with role "user" and source "announcement";
        # they're Hark's, not yours, and they're marked as a notice so they don't count as a prompt.
        "is_from_me": msg.get("role") == "user" and msg.get("source") != "announcement",
        "service": "announcement" if msg.get("source") == "announcement" else None,
        "text": text or None,
        "date": ms_time(msg.get("timestamp")),
        "rich": True if (presentation and presentation != "text") or found else None,
        "card": card(msg.get("presentation")),
        "buttons": buttons(msg.get("actions")),
        "hark_kind": "page" if found else None,
        "pages": found or None,
        "attachments": attachments or None,
        "streaming": True if msg.get("isStreaming") else None,
        "triggered_by": msg.get("triggeredByMessageId"),  # the message of yours it answers
        "source": msg.get("source"),
    }
    return {k: v for k, v in event.items() if v is not None}


def screen_summary(request: dict) -> tuple[str, dict]:
    """What a secure screen asks for (a sign-in, a card form), in words, and the button Hark shows for it.

    Only labels are recorded: the screen's instructions, its fields' names and its sign-in choices. Never a value,
    and nothing is ever entered. Hark sends these as request.displayHint "secure_screen" with request.screen."""
    screen = request.get("screen") if isinstance(request.get("screen"), dict) else {}
    site = request.get("prompt") if isinstance(request.get("prompt"), str) else None
    title = (screen.get("header") or {}).get("title") if isinstance(screen.get("header"), dict) else None
    parts = [c for c in (screen.get("components") or []) if isinstance(c, dict)]
    instructions = [c["text"] for c in parts if c.get("type") == "instructions" and isinstance(c.get("text"), str)]
    fields = [c["label"] for c in parts if c.get("type") not in ("instructions", "choice") and isinstance(c.get("label"), str)]
    choices = [c["label"] for c in parts if c.get("type") == "choice" and isinstance(c.get("label"), str)]
    primary = (screen.get("primary_action") or {}).get("label") if isinstance(screen.get("primary_action"), dict) else None
    heading = " · ".join(dict.fromkeys(filter(None, [site, title])))  # Hark often repeats the title as the site
    lines = [f"Action required: {heading}" if heading else "Action required: a secure screen"]
    lines += instructions
    if fields:
        lines.append("Fields: " + ", ".join(fields))
    if choices:
        lines.append("Or continue with: " + ", ".join(choices))
    if primary:
        lines.append(f"Button: {primary}")
    lines.append("(Hark's secure screen; recorded, nothing entered)")
    is_login = "login" in (title or "").lower() or "sign in" in (title or "").lower()
    label = f"Sign in to {site}" if is_login and site else (title or site or "Open")
    return "\n".join(lines), {"text": label, "kind": "secure_screen"}


def ask_event(ask_id: str, ask: dict, agent: Agent) -> dict:
    """A question or approval Hark is waiting on, as a message from Hark. Its options are recorded, never chosen."""
    text = next((ask[k] for k in ("question", "prompt", "title", "text", "message", "label") if isinstance(ask.get(k), str)), "")
    options = ask.get("options") or ask.get("choices") or []
    row = [{"text": o if isinstance(o, str) else str(o.get("label") or o.get("text") or o.get("value") or "?"), "kind": "option"}
           for o in options]
    kind = "ask"
    request = ask.get("request")
    if not text and isinstance(request, dict) and (request.get("screen") or request.get("displayHint")):
        text, button = screen_summary(request)
        row, kind = [button], "secure_screen"
    elif not text and not options and not set(ask) - {"id", "at", "status", "dispatchId", "dispatchedAt", "updatedAt"}:
        # Hark trims an ask to its ids once it's answered or withdrawn: say so instead of dumping them.
        text, kind = "(Hark closed this request" + (f": {ask['status']}" if isinstance(ask.get("status"), str) else "") + ")", "ask_closed"
    event = {
        "channel": "hark", "agent": agent.name, "handle": agent.handle,
        "guid": f"hark:ask:{ask_id}", "message_id": f"ask:{ask_id}", "is_from_me": False,
        "text": text or json.dumps(ask, ensure_ascii=False)[:2000],
        "date": ms_time(ask.get("createdAt") or ask.get("timestamp") or ask.get("at")),
        "rich": True, "buttons": [row] if row else None, "hark_kind": kind,
    }
    return {k: v for k, v in event.items() if v is not None}


def window_detail(window: dict) -> str | None:
    """The latest line of what a browsing window says it's doing."""
    lines = window.get("lines")
    if isinstance(lines, list) and lines:
        last = lines[-1]
        return str(last.get("text") if isinstance(last, dict) else last)[:300]
    narrative = window.get("narrative")
    return narrative[:300] if isinstance(narrative, str) else None


def signature(event: dict) -> str:
    return hashlib.sha1(json.dumps(event, sort_keys=True).encode()).hexdigest()


# MARK: - Channel

class ChannelLock:
    """One bench command uses Hark at a time, so two never write the same state."""

    def __init__(self, pin_dir: Path):
        pin_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(pin_dir, 0o700)
        self.path = pin_dir / "bench.lock"

    def __enter__(self):
        self.file = self.path.open("w")
        try:
            fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.file.close()
            raise HarkError("another bench command is using Hark; wait for it to finish") from None
        return self

    def __exit__(self, *exc):
        fcntl.flock(self.file, fcntl.LOCK_UN)
        self.file.close()


class HarkChannel:
    """A hark.com session that records the Hark agent's main thread.

        with HarkChannel(agents) as hark:
            hark.sync()
            hark.send(agent, "hi")
            hark.listen(120)
    """

    def __init__(self, agents: list[Agent], *, site=None, pin_dir: Path = PIN_DIR, inbox: Path | None = None,
                 on_event: Callable[[dict], None] | None = None,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic):
        hark = [a for a in agents if a.channel == "hark"]
        if not hark:
            raise HarkError("no agent in config/agents.yaml has channel: hark and a handle")
        if len(hark) > 1:
            raise HarkError(f"only one Hark agent is supported, since there's one signed-in account; "
                            f"found {', '.join(a.name for a in hark)}")
        self.agent = hark[0]
        self.site = site      # a HarkWeb, attached on entry unless one is given
        self._own_site = None
        self.pin_dir = pin_dir
        self.pins_path = pin_dir / "pins.json"
        self.inbox = inbox or inbox_path("hark")
        self.state_path = self.inbox.parent / "state.json"
        self.attachments_dir = self.inbox.parent / "attachments"  # copies of the Pages Hark built
        self.on_event = on_event
        self.sleep, self.clock = sleep, clock
        self.pins: dict = {}
        self.state: dict = {}
        self._lock: ChannelLock | None = None

    def __enter__(self):
        self._lock = ChannelLock(self.pin_dir).__enter__()
        try:
            if self.site is None:
                self.site = self._own_site = HarkWeb().__enter__()
            self.pins = connect(self.site, self.pins_path)
            self.state = json.loads(self.state_path.read_text()) if self.state_path.exists() else {}
            if self.state.get("conversation_id") != self.pins["conversation_id"]:
                self.state = {"conversation_id": self.pins["conversation_id"], "synced": False, "last_id": None,
                              "tracked": {}, "working": {}}
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

    # MARK: Recording

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
        """Whether an untracked message is one recorded earlier and since let go."""
        if not self.state["synced"] or not event.get("date"):
            return False  # the first sync records the whole thread
        return event["date"] < iso(datetime.now(UTC) - TRACK_WINDOW)

    def _working(self, record_id: str, action: str, detail: str | None = None, at: str | None = None) -> int:
        """A "typing" event while Hark works on something, at most one per record every WORK_EVERY seconds."""
        at = at or now_iso()
        last = self.state["working"].get(record_id)
        if last and (datetime.fromisoformat(at) - datetime.fromisoformat(last)).total_seconds() < WORK_EVERY:
            return 0
        self.state["working"][record_id] = at
        event = {"channel": "hark", "agent": self.agent.name, "handle": self.agent.handle, "action": action,
                 "message_id": record_id, **({"detail": detail} if detail else {})}
        self._emit("typing", event, at)
        return 1

    def _observe(self, msg: dict, at: str | None = None) -> int:
        if is_work(msg):
            return self._working(msg["id"], "working", at=at) if running(msg) else 0
        return 1 if self._record(message_event(msg, self.agent, self._page_files(msg)), at) else 0

    def _page_files(self, msg: dict) -> list[dict]:
        """Saves a copy of each Page the record carries, as attachments/<message>/<name>.html, and lists them.
        A Page that couldn't be saved is listed without a path; the next sync tries again."""
        files = []
        for page in pages(msg):
            dest = self.attachments_dir / msg["id"] / f"{safe_name(page['name'])}.html"
            info = {"name": dest.name, "mime_type": "text/html"}
            if not dest.exists():
                if not page.get("download"):
                    files.append(info)
                    continue
                try:
                    html = self.site.text(page["download"])
                except HarkError as e:
                    print(f"hark: couldn't save the Page {page['name']!r} of message {msg['id']}: {e}", file=sys.stderr)
                    files.append(info)
                    continue
                html, missing = freeze(html)  # with its images and fonts inside, so it opens anywhere
                for problem in missing:
                    print(f"hark: the saved Page {page['name']!r} is missing a file: {problem}", file=sys.stderr)
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(html)
            files.append(info | {"bytes": dest.stat().st_size, "path": str(dest.relative_to(self.inbox.parent))})
        return files

    def _prune(self) -> None:
        cutoff = iso(datetime.now(UTC) - TRACK_WINDOW)
        tracked = self.state["tracked"]
        for key in [k for k, t in tracked.items() if (t.get("date") or "9") < cutoff]:
            del tracked[key]
        working = self.state["working"]
        for key in [k for k, at in working.items() if at < cutoff]:
            del working[key]

    def _save_state(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(self.state))

    # MARK: What the runner calls

    def sync(self) -> int:
        """Records what arrived or changed in the thread since last time. Returns events written."""
        cid = self.pins["conversation_id"]
        latest = self.site.history(cid)
        pages = [latest]
        if not self.state["synced"]:  # first sync: the whole thread
            page = latest
            while (page.get("older") or {}).get("hasMore") and ordered(page):
                page = self.site.history(cid, before=ordered(page)[0])
                pages.insert(0, page)
        elif (last := self.state["last_id"]) and last not in ids(latest) \
                and (latest.get("older") or {}).get("hasMore"):
            # A lot arrived since last time: page forward from the last recorded message until the latest page.
            caught_up, cursor = [], last
            try:
                for _ in range(MAX_PAGES):
                    page = self.site.history(cid, after=cursor)
                    if not ordered(page):
                        break
                    caught_up.append(page)
                    if any(m["id"] in ids(latest) for m in ordered(page)):
                        break
                    cursor = ordered(page)[-1]["id"]
            except HarkNetworkError as e:
                if "HTTP 404" not in str(e):
                    raise
                # hark.com no longer knows the last recorded message (it drops or rewrites old ones), so page
                # backwards from the latest page instead, until a page holds something already recorded.
                caught_up, page = [], latest
                for _ in range(MAX_PAGES):
                    if not (page.get("older") or {}).get("hasMore") or not ordered(page):
                        break
                    page = self.site.history(cid, before=ordered(page)[0])
                    if not ordered(page):
                        break
                    caught_up.insert(0, page)
                    if any(m["id"] in self.state["tracked"] for m in ordered(page)):
                        break
            pages = caught_up + pages
        messages: dict[str, dict] = {}
        for page in pages:
            for msg in ordered(page):
                messages[msg["id"]] = msg
        written = sum(self._observe(msg) for msg in messages.values())
        for wid, window in (latest.get("windows") or {}).items():
            if running(window):
                written += self._working(f"window:{wid}", "browsing", window_detail(window))
        for aid, ask in (latest.get("asks") or {}).items():
            written += bool(self._record(ask_event(aid, ask, self.agent)))
        if messages:
            self.state["last_id"] = list(messages)[-1]
        self.state["synced"] = True
        self._prune()
        self._save_state()
        return written

    def send(self, agent: Agent, text: str) -> dict | None:
        """Sends `text` in Hark's main thread, then finds and records it there."""
        if agent != self.agent:
            raise HarkError(f"{agent.name} isn't the Hark agent in config/agents.yaml")
        if not text.strip():
            raise HarkError("refusing to send an empty message")
        cid = self.pins["conversation_id"]
        before = ids(self.site.history(cid))
        self.site.send(text)
        deadline = self.clock() + SENT_SECONDS
        while True:
            mine = [m for m in ordered(self.site.history(cid)) if m["id"] not in before and m.get("role") == "user"]
            if mine:
                msg = next((m for m in mine if m.get("content") == text), mine[0])
                if msg.get("content") != text:
                    print(f"hark: Hark shows the sent message as {msg.get('content')!r}", file=sys.stderr)
                return self._record(message_event(msg, self.agent))
            if self.clock() >= deadline:
                raise HarkError(f"clicked Send, but the message didn't show up in Hark's thread within {SENT_SECONDS}s. "
                                "Check hark.com/chat in the dedicated Chrome.")
            self.sleep(1)

    def listen(self, seconds: float | None = None, poll_seconds: float = POLL_SECONDS) -> None:
        """Keeps recording for `seconds`, or until interrupted, checking every `poll_seconds`."""
        deadline = None if seconds is None else self.clock() + seconds
        while True:
            wait = poll_seconds if deadline is None else min(poll_seconds, deadline - self.clock())
            if wait > 0:
                self.sleep(wait)
            try:
                self.sync()
            except HarkNetworkError as e:  # the tab was mid-reload or hark.com hiccuped; the next poll catches up
                print(f"hark: {e}; trying again in {poll_seconds:.0f}s", file=sys.stderr)
            if deadline is not None and self.clock() >= deadline:
                return

    def usage(self) -> dict | None:
        """Hark's token meter right now (see HarkWeb.meter)."""
        return self.site.meter()

    def import_run(self, run_dir: Path) -> int:
        """Records a run made before this channel existed, from its events.jsonl, with the times it was observed.

        Import before the first sync: sync records anything not yet recorded,
        but with the time it syncs.
        """
        info = json.loads((run_dir / "run.json").read_text())
        if info.get("conversation_id") != self.pins["conversation_id"]:
            raise HarkError(f"{run_dir.name} was recorded in Hark thread {info.get('conversation_id')}, "
                            f"not the pinned main thread {self.pins['conversation_id']}; not importing it")
        written = 0
        events = run_dir / "events.jsonl"
        if not events.exists():
            events = run_dir / "raw" / "events.jsonl"  # where it is once the run folder is in the standard layout (bench.runs)
        for line in events.read_text().splitlines():
            e = json.loads(line)
            at, raw = e.get("emitted_at"), e.get("hark") or {}
            if e.get("kind") in ("new", "update") and e.get("message_id"):
                written += self._observe({**raw, "content": e.get("text") or ""}, at)
            elif e.get("kind") == "window" and running(raw):
                written += self._working(f"window:{e['window_id']}", "browsing", window_detail(raw), at)
            elif e.get("kind") == "approval_requested":
                written += bool(self._record(ask_event(e["ask_id"], raw, self.agent), at))
        self._save_state()
        return written


# MARK: - Account, sign-in and status

def load_pins(path: Path) -> dict:
    return json.loads(path.read_text()) if path.exists() else {}


def save_pins(path: Path, pins: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(pins, indent=2))
    os.chmod(path, 0o600)


def connect(site, pins_path: Path) -> dict:
    """Checks who's signed in to Hark before anything else, and pins that account and its main thread."""
    user = site.session()
    if not user:
        raise HarkError("nobody is signed in to hark.com in the dedicated Chrome. Run: uv run bench hark-login")
    pins = load_pins(pins_path)
    if pins.get("user_id") and pins["user_id"] != user.get("id"):
        raise HarkError(f"hark.com is signed in as {user.get('email')}, but agent-runner is pinned to {pins.get('email')}. "
                        f"Nothing was read or sent. Sign in as {pins.get('email')}, or, if the account really "
                        f"changed, delete {pins_path}.")
    if not pins.get("conversation_id"):
        # The main thread is the one made at sign-up; Projects come later.
        threads = sorted((c for c in site.conversations() if not c.get("archivedAt")), key=lambda c: c.get("createdAt") or "")
        if not threads:
            raise HarkError("this Hark account has no chat thread yet. Open hark.com/chat once in the dedicated Chrome.")
        pins = {"user_id": user["id"], "email": user.get("email"), "conversation_id": threads[0]["id"]}
        save_pins(pins_path, pins)
    return pins


def sign_in(*, pin_dir: Path = PIN_DIR, site=None) -> str:
    """Makes sure the dedicated Chrome is signed in to Hark, waiting while you sign in, then pins the account."""
    print("Checking that the dedicated Chrome is signed in to Hark. If it isn't, sign in there "
          "(a passkey saved elsewhere won't be offered; use your password or phone).", flush=True)
    own = site is None
    site = site or HarkWeb().__enter__()
    try:
        already = site.sign_in()
        with ChannelLock(pin_dir):
            pins = connect(site, pin_dir / "pins.json")
    finally:
        if own:
            site.__exit__()
    return f"{'Already signed' if already else 'Signed'} in to Hark as {pins.get('email')} in the dedicated Chrome."


def status(*, pin_dir: Path = PIN_DIR, site=None) -> str:
    """Checks the sign-in, the pinned account, the message box and Hark's token meter, without sending or recording."""
    own = site is None
    with ChannelLock(pin_dir):
        site = site or HarkWeb().__enter__()
        try:
            user = site.session()
            if not user:
                return "Hark: nobody is signed in to hark.com in the dedicated Chrome. Run: uv run bench hark-login"
            pins = load_pins(pin_dir / "pins.json")
            lines = [f"Hark: signed in as {user.get('email')} ({user.get('id')})"]
            if pins.get("user_id") and pins["user_id"] != user.get("id"):
                lines.append(f"NOT READY: agent-runner is pinned to {pins.get('email')}, so nothing would be read or sent")
            elif not pins:
                lines.append("Not pinned yet. The first sync pins this account and its main thread.")
            else:
                busy = any(running(m) for m in ordered(site.history(pins["conversation_id"])))
                lines.append(f"Main thread: {pins['conversation_id']} ({'Hark is working on something' if busy else 'idle'})")
            try:
                lines.append("Message box: " + ("has text in it, so sending would refuse" if site.draft() else "found, empty"))
            except HarkError as e:
                lines.append(f"Message box: NOT READY: {e}")
            meter = site.meter()
            if meter:
                lines.append(f"Hark tokens: {meter.get('dailyUsed', 0):,} of {meter.get('dailyLimit', 0):,} today; "
                             f"{meter.get('poolUsed', 0):,} of {meter.get('poolLimit', 0):,} this period "
                             f"(resets {meter.get('poolResetsAt', '?')})")
            return "\n".join(lines)
        finally:
            if own:
                site.__exit__()
