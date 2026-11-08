"""Wajo channel: talks to Fo on wajo.ai, in the dedicated Chrome.

Fo's own web app is used instead of Telegram, the way Hark's is (see hark.py
and docs/connections/wajo-web.md). No AI drives the page: a fixed script fills
Fo's message box and clicks Send. Everything is read back from wajo.ai's own
server functions, called from inside the page with its sign-in, so every
message carries Wajo's own ID and server time.

What it guarantees:
- It reads and opens only wajo.ai. It never navigates a tab that isn't on
  wajo.ai, and the only server functions it calls are the read-only ones the
  page itself calls to show your account, workspace, chats and messages.
- One Wajo account. Whoever is signed in the first time is pinned, and every
  later command refuses to run if another account is signed in.
- Every chat with Fo is recorded, including the ones Wajo makes from Telegram,
  from the moment the account is pinned. Messages from before then are left
  out: the Telegram channel recorded Telegram's, so they'd be there twice.
- Sending types into one kept chat. The first send starts it from wajo.ai/fo,
  and every later send continues it, until you choose another with
  bench wajo-thread (a chat by title or ID, or "new" to start one).
- It never clicks Fo's message box. The text goes in with fill(), is checked,
  and only then is Send clicked. It never types over text already in the box,
  and clears the box if anything fails before Send.
- Nothing on the page is pressed but Send. The Pages Fo makes are recorded as
  links (wajo.ai/d/<id>), so bench run saves them as deliverables.

wajo.ai shows everything Fo wrote in a chat. In a chat Wajo made from Telegram,
Fo's answers that also reached your phone have IDs ending in -reply-N; the
others never arrived in Telegram. In a chat started on wajo.ai, every answer
shows only there.

While Fo works on a reply (the chat has an active run), that's recorded as
"typing" events with action "working", like Hark's.

wajo.ai's server functions are addressed by IDs that change when Wajo rebuilds
its site. The known IDs are in `FUNCTIONS`; if one stops answering, the channel
reloads wajo.ai once and learns the new IDs from the page's own requests, by
what each one answers (see `WajoWeb.learn`).
"""

import json
import sys
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

from bench.channels import seroval
from bench.channels.chrome import CDP_URL
from bench.channels.site import (ChannelLock, Recorder, SiteError, SiteNetworkError, SiteWeb, fill_and_send, load_json,
                                 ms_time, now_iso, save_pins, shows)
from bench.config import PRIVATE_DIR, Agent
from bench.inbox import inbox_path

PIN_DIR = PRIVATE_DIR / "wajo"
ORIGIN = "https://wajo.ai"
HOME = ORIGIN + "/fo"           # Fo's home page: its box starts a new chat
POLL_SECONDS = 3                # how often listen checks for new messages
SENT_SECONDS = 45               # how long a sent message may take to show up in the chat
PAGE_SIZE = 6                   # turns per messages page (a turn is one of your messages and Fo's answers)
DONE = {"completed", "failed", "cancelled", "canceled", "error", "stopped", "done", "idle", None, ""}
CHANNELS = {"SESSION_CHANNEL_TELEGRAM": "telegram", "SESSION_CHANNEL_UNSPECIFIED": "web"}


class WajoError(SiteError):
    pass


class WajoNetworkError(WajoError, SiteNetworkError):
    """The wajo.ai tab couldn't be read just now. Worth retrying later."""


@dataclass(frozen=True)
class Functions:
    """The IDs of the server functions read, as of Wajo's build 4e4828d (2026-11-07)."""

    session: str = "c942ba17e0fab88e0793430188d32b93b66f59905601773b2ea66c1e3a83add1"     # → sub, email, planTier
    workspaces: str = "17415ed10fb52a1b2f020bbbcb463c3be5a4fbc2031867838d3ef70edc206586"  # → workspaces, defaultOrgId
    chats: str = "670d192d2dd59ad069e95aa173ba138677bd7289bfa1228f1349139c7117af04"       # → chats, nextPageToken
    messages: str = "7f73828785cd1243928dca75fa939c1fba7fd0c0a2997ce82054767e4e1e66c0"    # → messages, activeRun, …


FUNCTIONS = Functions()
SHAPES = {  # how each function's answer is recognised, when learning new IDs
    "session": {"sub", "email"},
    "workspaces": {"workspaces", "defaultOrgId"},
    "chats": {"chats", "nextPageToken"},
    "messages": {"messages", "activeRun"},
}


@dataclass(frozen=True)
class Selectors:
    """Where things are on wajo.ai. If Wajo changes its page, this is the one place to update."""

    box: str = 'textarea[placeholder^="Ask Fo"], textarea[aria-label^="Ask Fo"]'
    send_names: tuple[str, ...] = ("Send message", "Send", "发送", "发送消息")  # the Send button, by its accessible name


CALL_JS = """async ({path, payload}) => {
  const query = payload ? '?payload=' + encodeURIComponent(payload) : '';
  const r = await fetch(path + query, {credentials: 'include',
                                       headers: {accept: 'application/json', 'x-tsr-serverfn': 'true'}});
  return {status: r.status, text: await r.text()};
}"""


def chat_url(chat_id: str) -> str:
    return f"{ORIGIN}/fo/c/{chat_id}"


# MARK: - wajo.ai in Chrome

class WajoWeb(SiteWeb):
    """wajo.ai in the dedicated Chrome. Use it as a context manager."""

    origin, home, name = ORIGIN, HOME, "Wajo"
    error, network_error = WajoError, WajoNetworkError

    def __init__(self, cdp_url: str = CDP_URL, *, selectors: Selectors = Selectors(),
                 functions: Functions = FUNCTIONS, functions_path: Path | None = PIN_DIR / "functions.json", **kw):
        super().__init__(cdp_url, **kw)
        self.selectors = selectors
        self.functions_path = functions_path
        learned = load_json(functions_path) if functions_path else {}
        self.functions = replace(functions, **{k: v for k, v in learned.items() if k in SHAPES})
        self._sub: str | None = None
        self._learned = False

    # Server functions

    def call(self, name: str, data: dict | None = None, *, with_context: bool = True):
        """One of wajo.ai's read-only server functions, called from inside the Wajo tab. Returns its result."""
        fn = getattr(self.functions, name)
        context = {"bootedSub": self.sub()} if with_context else None
        result = self.evaluate(CALL_JS, {"path": f"/_serverFn/{fn}",
                                         "payload": seroval.payload(data, context) if context else None})
        status = result["status"]
        if status in (401, 403):
            raise WajoError("wajo.ai says nobody is signed in. Run: uv run bench wajo-login")
        if status in (404, 500) and not self._learned:
            self.learn()  # Wajo rebuilt its site, so the function's ID changed
            return self.call(name, data, with_context=with_context)
        if status != 200:
            raise WajoNetworkError(f"wajo.ai answered its {name} function with HTTP {status}")
        try:
            answer = seroval.decode(json.loads(result["text"]))
        except ValueError:
            raise WajoNetworkError(f"wajo.ai's answer to its {name} function isn't JSON") from None
        if not isinstance(answer, dict) or "result" not in answer:
            raise WajoNetworkError(f"wajo.ai's answer to its {name} function has no result")
        return answer["result"]

    def sub(self) -> str:
        """The signed-in account's ID, which every other call carries."""
        if self._sub is None:
            user = self.session()
            if not user:
                raise WajoError("nobody is signed in to wajo.ai in the dedicated Chrome. Run: uv run bench wajo-login")
            self._sub = user["sub"]
        return self._sub

    def session(self) -> dict | None:
        """The signed-in Wajo account ({sub, email, planTier, …}), or None."""
        user = self.call("session", with_context=False)
        return user if isinstance(user, dict) and user.get("sub") else None

    def org(self) -> str:
        """The workspace chats are listed in: the account's default one."""
        return self.call("workspaces")["defaultOrgId"]

    def chats(self, org: str) -> list[dict]:
        """Every chat in the workspace, newest activity first."""
        out, token = [], None
        for _ in range(20):
            page = self.call("chats", {"workspaceId": org, "scope": "workspace", "agentIds": [], "pageToken": token})
            out += page.get("chats") or []
            token = page.get("nextPageToken")
            if not token:
                return out
        return out

    def messages(self, chat_id: str, org: str, page_size: int = PAGE_SIZE) -> dict:
        """A chat's latest messages ({messages, activeRun, latestRunStatus, …})."""
        return self.call("messages", {"sessionId": chat_id, "orgId": org, "pageSize": page_size})

    def learn(self, seconds: float = 20) -> None:
        """Reloads wajo.ai once and learns the server functions' IDs from the page's own requests:
        Fo's home page shows the account, workspace and chats, and a chat's page its messages."""
        self._learned = True
        page = self.page()
        if self.on_site(page.url):
            try:
                if self.draft():
                    raise WajoError("wajo.ai changed, and learning its new server functions means reloading the "
                                    "Wajo tab, but its message box has text in it. Send or delete it, then try again.")
            except WajoError as e:
                if "message box has text" in str(e):
                    raise
        found: dict[str, str] = {}

        def on_response(response):
            if "/_serverFn/" not in response.url or response.status != 200:
                return
            try:
                result = seroval.decode(json.loads(response.body())).get("result")
            except Exception:  # noqa: BLE001 - another function's answer, or a stream
                return
            if isinstance(result, dict):
                for name, keys in SHAPES.items():
                    if keys <= set(result):
                        found[name] = response.url.split("/_serverFn/", 1)[1].split("?", 1)[0]

        def wait(names: set[str]) -> None:
            deadline = time.monotonic() + seconds
            while not names <= set(found) and time.monotonic() < deadline:
                page.wait_for_timeout(500)  # lets the page's responses come in meanwhile

        page.on("response", on_response)
        try:
            page.goto(HOME, wait_until="domcontentloaded")
            wait({"session", "workspaces", "chats"})
            self.functions = replace(self.functions, **found)
            if "messages" not in found:
                try:
                    chats = self.chats(self.org())
                except WajoError:
                    chats = []
                if chats:
                    page.goto(chat_url(chats[0]["id"]), wait_until="domcontentloaded")
                    wait({"messages"})
        finally:
            page.remove_listener("response", on_response)
        if not found:
            raise WajoError("wajo.ai's server functions changed, and they couldn't be found again from the page. "
                            "Check wajo.ai/fo in the dedicated Chrome; if it works there, bench/channels/wajo.py "
                            "needs updating.")
        self.functions = replace(self.functions, **found)
        if self.functions_path:
            save_pins(self.functions_path, asdict(self.functions))
        print(f"wajo: wajo.ai changed; learned its new server functions ({', '.join(sorted(found))})", file=sys.stderr)

    # The page

    def box(self, page=None):
        page = page or self.page()
        box = page.locator(self.selectors.box)
        if box.count() != 1:
            box = page.locator("textarea")
        if box.count() != 1:
            raise WajoError(f"found {box.count()} message boxes on {page.url.split('?')[0]}, not one. "
                            "If the tab shows a sign-in page, run: uv run bench wajo-login")
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

    def send(self, text: str, url: str = HOME, sleep: Callable[[float], None] = time.sleep) -> str:
        """Types `text` into the chat whose page is `url` (wajo.ai/fo starts a new one), and returns the
        tab's address after sending: a new chat's own page."""
        page = self.page()

        def ready():
            try:
                self.box(page)
                return True
            except WajoError:
                return False
        self.open(url, ready, sleep=sleep)
        fill_and_send(page, self.box(page), self.send_button(page), text, error=WajoError, name="Fo",
                      sent_seconds=self.sent_seconds, sleep=sleep)
        return page.url

    def sign_in(self, wait_seconds: float = 600, sleep: Callable[[float], None] = time.sleep,
                clock: Callable[[], float] = time.monotonic) -> bool:
        """Makes sure this Chrome is signed in to Wajo, waiting while you sign in. True if it already was."""
        page = self.page()
        if self.signed_in():
            return True
        page.goto(HOME, wait_until="domcontentloaded")
        page.bring_to_front()
        deadline = clock() + wait_seconds
        while clock() < deadline:
            sleep(2)
            if self.on_site(page.url) and self.signed_in():  # signing in passes through login.wajo.ai
                return False
        raise WajoError(f"not signed in to Wajo after {wait_seconds:.0f}s. Sign in at {HOME} "
                        "in the dedicated Chrome, then try again.")

    def signed_in(self) -> bool:
        try:
            return self.session() is not None
        except SiteError:
            return False


# MARK: - Wajo's records → events

def tool_summary(calls: list[dict] | None) -> list[dict] | None:
    """What Fo did for a message: each tool's name and status, without its inputs or results."""
    out = [{"name": c.get("displayLabel") or c.get("name") or "?", "status": c.get("status")} for c in calls or []]
    return out or None


def message_event(msg: dict, chat: dict, agent: Agent) -> dict:
    """One Wajo message in the shared event format, without kind and emitted_at."""
    event = {
        "channel": "wajo",
        "agent": agent.name,
        "handle": agent.handle,
        "guid": f"wajo:{msg['id']}",
        "message_id": msg["id"],
        "is_from_me": msg.get("role") == "user",
        "text": msg.get("content") or None,
        "date": ms_time(msg.get("timestampMs")),
        "source": msg.get("source"),
        "chat": chat["id"],
        "chat_title": chat.get("title"),
        "chat_channel": CHANNELS.get(chat.get("channel"), chat.get("channel")),
        "run": msg.get("runId"),
        "tools": tool_summary(msg.get("toolCalls")),
        "pages": [f"{ORIGIN}/d/{a}" for a in msg.get("producedArtifactIds") or []] or None,  # Fo's Pages
    }
    return {k: v for k, v in event.items() if v is not None}


def running(result: dict) -> bool:
    return bool(result.get("activeRun")) or result.get("latestRunStatus") not in DONE


def is_fo(chat: dict) -> bool:
    """Whether a chat is the account's own chat with Fo (not someone else's agent shared into the workspace)."""
    return (chat.get("agentHandle") in (None, "fo")) and chat.get("ownerIsCaller", True) is not False


# MARK: - Channel

class WajoChannel(Recorder):
    """A wajo.ai session that records every chat with Fo, and sends in the kept chat.

        with WajoChannel(agents) as wajo:
            wajo.sync()
            wajo.send(agent, "hi")
            wajo.listen(120)
    """

    channel = "wajo"
    network_error = WajoNetworkError

    def __init__(self, agents: list[Agent], *, site=None, pin_dir: Path = PIN_DIR, inbox: Path | None = None, **kw):
        wajo = [a for a in agents if a.channel == "wajo"]
        if not wajo:
            raise WajoError("no agent in config/agents.yaml has channel: wajo and a handle")
        if len(wajo) > 1:
            raise WajoError(f"only one Wajo agent is supported, since there's one signed-in account; "
                            f"found {', '.join(a.name for a in wajo)}")
        super().__init__(wajo[0], inbox or inbox_path("wajo"), **kw)
        self.site = site
        self._own_site = None
        self.pin_dir = pin_dir
        self.pins_path = pin_dir / "pins.json"
        self.pins: dict = {}
        self._lock: ChannelLock | None = None

    def __enter__(self):
        self._lock = ChannelLock(self.pin_dir, "Wajo", WajoError).__enter__()
        try:
            if self.site is None:
                self.site = self._own_site = WajoWeb(functions_path=self.pin_dir / "functions.json").__enter__()
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

    @property
    def org(self) -> str:
        return self.pins["org_id"]

    @property
    def since(self) -> str:
        """Messages from before this (when the account was pinned) aren't recorded."""
        return self.pins["since"]

    def _seen_before(self, event: dict) -> bool:
        """Messages from before the account was pinned are skipped. A chat read for the first time
        is recorded from then on; after that, an untracked message older than 48 hours was recorded
        before and let go."""
        if not event.get("date"):
            return False
        if event["date"] < self.since:
            return True
        return event.get("chat") in self.state["threads"] and super()._seen_before(event)

    # MARK: Recording

    def chats(self) -> list[dict]:
        """The account's chats with Fo, newest activity first."""
        return [c for c in self.site.chats(self.org) if is_fo(c)]

    def sync(self) -> int:
        """Records what arrived or changed in any chat since last time. Returns events written.

        The chat list says when each chat was last active, so a chat's messages are
        read only when it changed, or while Fo is working in it."""
        written = 0
        threads = self.state["threads"]
        for chat in sorted(self.chats(), key=lambda c: c.get("lastActivityMs") or 0):  # oldest first
            seen = threads.get(chat["id"])
            activity = chat.get("lastActivityMs") or 0
            if seen is None and ms_time(activity) and ms_time(activity) < self.since:
                threads[chat["id"]] = {"activity": activity, "status": chat.get("latestRunStatus"), "running": False}
                continue  # nothing in it since the account was pinned
            if seen and seen.get("activity") == activity and seen.get("status") == chat.get("latestRunStatus") \
                    and not seen.get("running"):
                continue  # nothing new in this chat
            written += self._sync_chat(chat, PAGE_SIZE if seen is None else 2)
        self._prune()
        self._save_state()
        return written

    def _sync_chat(self, chat: dict, page_size: int = PAGE_SIZE) -> int:
        result = self.site.messages(chat["id"], self.org, page_size)
        written = 0
        for msg in result.get("messages") or []:
            if msg.get("role") != "user" and not msg.get("content"):
                continue  # a step of Fo's work, with no text: recorded as "working" while the run lasts
            written += 1 if self._record(message_event(msg, chat, self.agent)) else 0
        busy = running(result)
        if busy:
            written += self._working(f"run:{chat['id']}", "working", extra={"chat": chat["id"]})
        self.state["threads"][chat["id"]] = {"activity": chat.get("lastActivityMs") or 0,
                                             "status": chat.get("latestRunStatus"), "running": busy}
        return written

    def send(self, agent: Agent, text: str) -> dict | None:
        """Sends `text` in the kept chat (starting it if there's none yet), then finds and records it there."""
        if agent != self.agent:
            raise WajoError(f"{agent.name} isn't the Wajo agent in config/agents.yaml")
        if not text.strip():
            raise WajoError("refusing to send an empty message")
        chat_id = self.pins.get("send_to")
        chats = {c["id"]: c for c in self.chats()}
        if chat_id and chat_id not in chats:
            raise WajoError(f"the chat agent-runner sends to ({chat_id}) is gone from wajo.ai, so nothing was sent. "
                            "Choose another with: uv run bench wajo-thread")
        before = {m["id"] for m in (self.site.messages(chat_id, self.org).get("messages") or [])} if chat_id else set()
        sent_at = datetime.now(UTC)
        landed = self.site.send(text, chat_url(chat_id) if chat_id else HOME)
        deadline = self.clock() + SENT_SECONDS
        while True:
            if not chat_id:
                chat_id = new_chat(landed, self.chats(), sent_at)
                if chat_id:
                    self.pins["send_to"] = chat_id
                    save_pins(self.pins_path, self.pins)
                    print(f"wajo: started a chat, {chat_url(chat_id)}; later sends continue it "
                          "(bench wajo-thread chooses another)", file=sys.stderr)
            if chat_id:
                chat = next((c for c in self.chats() if c["id"] == chat_id), {"id": chat_id})
                result = self.site.messages(chat_id, self.org)
                mine = [m for m in result.get("messages") or [] if m["id"] not in before and m.get("role") == "user"]
                if mine:
                    msg = next((m for m in mine if (m.get("content") or "").strip() == text.strip()), mine[-1])
                    if (msg.get("content") or "").strip() != text.strip():
                        print(f"wajo: Wajo shows the sent message as {msg.get('content')!r}", file=sys.stderr)
                    return self._record(message_event(msg, chat, self.agent))
            if self.clock() >= deadline:
                where = chat_url(chat_id) if chat_id else HOME
                raise WajoError(f"clicked Send, but the message didn't show up in Fo's chat within {SENT_SECONDS}s. "
                                f"Check {where} in the dedicated Chrome.")
            self.sleep(2)

    def listen(self, seconds: float | None = None, poll_seconds: float = POLL_SECONDS) -> None:
        super().listen(seconds, poll_seconds)


def new_chat(landed_url: str, chats: list[dict], sent_at: datetime) -> str | None:
    """The chat a send from wajo.ai/fo started: the one the tab moved to, else the newest one active since."""
    if "/fo/c/" in landed_url:
        return landed_url.split("/fo/c/", 1)[1].split("/")[0].split("?")[0]
    since = sent_at.timestamp() * 1000 - 5000
    fresh = [c for c in chats if (c.get("lastActivityMs") or 0) >= since]
    return max(fresh, key=lambda c: c.get("lastActivityMs") or 0)["id"] if fresh else None


# MARK: - Account, chats, sign-in and status

def connect(site, pins_path: Path) -> dict:
    """Checks who's signed in to Wajo before anything else, and pins that account and its workspace."""
    user = site.session()
    if not user:
        raise WajoError("nobody is signed in to wajo.ai in the dedicated Chrome. Run: uv run bench wajo-login")
    pins = load_json(pins_path)
    if pins.get("sub") and pins["sub"] != user.get("sub"):
        raise WajoError(f"wajo.ai is signed in as {user.get('email')}, but agent-runner is pinned to {pins.get('email')}. "
                        f"Nothing was read or sent. Sign in as {pins.get('email')}, or, if the account really "
                        f"changed, delete {pins_path}.")
    if not pins.get("org_id"):
        pins = {"sub": user["sub"], "email": user.get("email"), "org_id": site.org(), "since": now_iso()} \
            | {k: v for k, v in pins.items() if k == "send_to"}
        save_pins(pins_path, pins)
    return pins


def label(chat: dict) -> str:
    where = CHANNELS.get(chat.get("channel"), "web")
    return f'"{chat.get("title") or "untitled"}" ({chat["id"]}, from {where})'


def choose_thread(choice: str | None = None, *, pin_dir: Path = PIN_DIR, site=None) -> str:
    """Lists Fo's chats, and with `choice`, makes it the one sending types into.

    `choice` is "new" (the next send starts a chat), a chat's ID or the start of
    one, or its title. Every chat is recorded either way.
    """
    own = site is None
    with ChannelLock(pin_dir, "Wajo", WajoError):
        site = site or WajoWeb(functions_path=pin_dir / "functions.json").__enter__()
        try:
            pins = connect(site, pin_dir / "pins.json")
            chats = [c for c in site.chats(pins["org_id"]) if is_fo(c)]
            if choice:
                if choice.strip().lower() == "new":
                    pins.pop("send_to", None)
                else:
                    pins["send_to"] = pick_chat(choice, chats)["id"]
                save_pins(pin_dir / "pins.json", pins)
            send_to = pins.get("send_to")
            lines = [label(c) + ("  ← bench send and bench run type here" if c["id"] == send_to else "") for c in chats]
            if not send_to:
                lines.append("No chat is kept yet: the next send starts one from wajo.ai/fo, and later sends continue it.")
            elif send_to not in [c["id"] for c in chats]:
                lines.append(f"Sending is set to {send_to}, which is gone. Choose another, or: uv run bench wajo-thread new")
            return "Recording all of these:\n" + "\n".join(f"  {line}" for line in lines)
        finally:
            if own:
                site.__exit__()


def pick_chat(choice: str, chats: list[dict]) -> dict:
    """The chat `choice` names: its ID, its title, or the start of either (at least 4 characters)."""
    want = choice.strip()
    title = lambda c: (c.get("title") or "").strip().lower()  # noqa: E731
    matches = [c for c in chats if c["id"] == want] \
        or [c for c in chats if title(c) == want.lower()] \
        or ([c for c in chats if c["id"].startswith(want) or title(c).startswith(want.lower())] if len(want) >= 4 else [])
    if len(matches) == 1:
        return matches[0]
    known = ", ".join(label(c) for c in chats) or "none"
    raise WajoError(f"{'more than one' if matches else 'no'} chat matches {choice!r}. Give new, or one of these by "
                    f"title or ID: {known}")


def sign_in(*, pin_dir: Path = PIN_DIR, site=None) -> str:
    """Makes sure the dedicated Chrome is signed in to Wajo, waiting while you sign in, then pins the account."""
    print("Checking that the dedicated Chrome is signed in to Wajo. If it isn't, sign in there.", flush=True)
    own = site is None
    site = site or WajoWeb(functions_path=pin_dir / "functions.json").__enter__()
    try:
        already = site.sign_in()
        with ChannelLock(pin_dir, "Wajo", WajoError):
            pins = connect(site, pin_dir / "pins.json")
    finally:
        if own:
            site.__exit__()
    return (f"{'Already signed' if already else 'Signed'} in to Wajo as {pins.get('email')} in the dedicated Chrome. "
            f"Messages from {pins['since']} on are recorded.")


def status(*, pin_dir: Path = PIN_DIR, site=None) -> str:
    """Checks the sign-in, the pinned account, the chats and the one sent to, and the message box,
    without sending or recording."""
    own = site is None
    with ChannelLock(pin_dir, "Wajo", WajoError):
        site = site or WajoWeb(functions_path=pin_dir / "functions.json").__enter__()
        try:
            user = site.session()
            if not user:
                return "Wajo: nobody is signed in to wajo.ai in the dedicated Chrome. Run: uv run bench wajo-login"
            pins = load_json(pin_dir / "pins.json")
            lines = [f"Wajo: signed in as {user.get('email')} ({user.get('sub')}, plan {user.get('planTier')})"]
            if pins.get("sub") and pins["sub"] != user.get("sub"):
                lines.append(f"NOT READY: agent-runner is pinned to {pins.get('email')}, so nothing would be read or sent")
                return "\n".join(lines)
            if not pins:
                lines.append("Not pinned yet. The first sync pins this account, and records from then on.")
            else:
                lines.append(f"Recording every chat with Fo, from {pins['since']} on")
            org = pins.get("org_id") or site.org()
            chats = [c for c in site.chats(org) if is_fo(c)]
            lines.append(f"Chats: {len(chats)}" + (f", latest {label(chats[0])}" if chats else ""))
            send_to = pins.get("send_to")
            if not send_to:
                lines.append("Sending to: a new chat from wajo.ai/fo, kept for later sends")
            else:
                chat = next((c for c in chats if c["id"] == send_to), None)
                if chat is None:
                    lines.append(f"Sending to: NOT READY: chat {send_to} is gone. Choose another: uv run bench wajo-thread")
                else:
                    busy = running(site.messages(send_to, org, 1))
                    tab = "showing it" if shows(site.tab_url(), chat_url(send_to)) else \
                        "showing another page; sending opens the chat first"
                    lines.append(f"Sending to: {label(chat)} ({'Fo is working on something' if busy else 'idle'}; "
                                 f"the Wajo tab is {tab})")
            try:
                lines.append("Message box: " + ("has text in it, so sending would refuse" if site.draft() else "found, empty"))
            except SiteError as e:
                lines.append(f"Message box: NOT READY: {e}")
            return "\n".join(lines)
        finally:
            if own:
                site.__exit__()


__all__ = ["FUNCTIONS", "HOME", "ORIGIN", "Functions", "Selectors", "WajoChannel", "WajoError", "WajoNetworkError",
           "WajoWeb", "choose_thread", "sign_in", "status"]
