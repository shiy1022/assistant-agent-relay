"""Telegram channel: talks to agents from your own Telegram account through
Telegram's official API (Telethon).

Only the chats of agents in config/agents.yaml with `channel: telegram` are
ever read or written. Each agent is pinned to its numeric Telegram ID the
first time its username is resolved, and from then on the channel talks to
that ID, so a username that later moves to another account isn't followed.
Telethon normally caches every user and chat it sees in its session file;
that's turned off, so nothing about your other chats is stored.

Telegram keeps the whole chat on its servers, so nothing has to run in the
background: `sync` records whatever arrived or changed since the last run,
and while connected the channel records new messages, edits, reactions,
read receipts and typing as they happen.
"""

import asyncio
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from getpass import getpass
from pathlib import Path

from telethon import TelegramClient, events
from telethon.errors import SessionPasswordNeededError
from telethon.tl import functions, types

from bench.config import PRIVATE_DIR, Agent
from bench.inbox import inbox_path
from bench.qr import show_qr

SESSION_DIR = PRIVATE_DIR / "telegram"
KEYCHAIN_SERVICE = "agent-runner.telegram.{}"
TRACK_WINDOW = timedelta(hours=48)  # how long a message is watched for edits, reactions and receipts
REFRESH_LIMIT = 50                  # recent messages re-read by sync, to catch edits made while offline


class TelegramError(Exception):
    pass


def api_credentials() -> tuple[int, str]:
    """api_id and api_hash from the Keychain, or TELEGRAM_API_ID / TELEGRAM_API_HASH."""
    values = {}
    for key in ("api_id", "api_hash"):
        value = os.environ.get(f"TELEGRAM_{key.upper()}", "")
        if not value:
            result = subprocess.run(
                ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE.format(key), "-w"],
                capture_output=True, text=True,
            )
            value = result.stdout.strip() if result.returncode == 0 else ""
        if not value:
            raise TelegramError(
                f"no Telegram {key} found. Create one at my.telegram.org, then store it with:\n"
                f'  security add-generic-password -U -a "$USER" -s {KEYCHAIN_SERVICE.format(key)} -w'
            )
        values[key] = value
    try:
        return int(values["api_id"]), values["api_hash"]
    except ValueError:
        raise TelegramError(
            "the stored Telegram api_id should be just the number from my.telegram.org (like 1234567). "
            "Store it again with:\n"
            f'  security add-generic-password -U -a "$USER" -s {KEYCHAIN_SERVICE.format("api_id")} -w'
        ) from None


def make_client(session_dir: Path = SESSION_DIR) -> TelegramClient:
    session_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(session_dir, 0o700)
    api_id, api_hash = api_credentials()
    client = TelegramClient(
        str(session_dir / "bench"), api_id, api_hash,
        device_model="agent-runner", system_version="macOS", app_version="0.1",
    )
    client.session.save_entities = False
    return client


class SessionLock:
    """Telethon's session file can't be shared, so one bench command uses Telegram at a time."""

    def __init__(self, session_dir: Path):
        session_dir.mkdir(parents=True, exist_ok=True)
        self.path = session_dir / "bench.lock"

    def __enter__(self):
        self.file = self.path.open("w")
        try:
            fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.file.close()
            raise TelegramError("another bench command is using Telegram; wait for it to finish") from None
        return self

    def __exit__(self, *exc):
        fcntl.flock(self.file, fcntl.LOCK_UN)
        self.file.close()


def secure(session_dir: Path) -> None:
    for path in session_dir.glob("bench.session*"):
        os.chmod(path, 0o600)


# MARK: - Message → event

def iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def now_iso() -> str:
    return iso(datetime.now(UTC))


def snake(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def buttons(markup) -> list[list[dict]] | None:
    """Button rows on a bot message: each button's text and what tapping it does."""
    rows = getattr(markup, "rows", None)
    if not rows:
        return None
    out = []
    for row in rows:
        cells = []
        for button in row.buttons:
            action = getattr(button, "type", None) or button  # newer layers put the action in .type
            kind = type(action).__name__
            for prefix in ("InlineButtonType", "ButtonType", "KeyboardButton"):
                kind = kind.removeprefix(prefix)
            cell = {"text": button.text, "kind": snake(kind) or "default"}
            if url := getattr(action, "url", None):
                cell["url"] = url
            cells.append(cell)
        out.append(cells)
    return out


def reactions(value) -> list[dict] | None:
    results = getattr(value, "results", None)
    if not results:
        return None
    return [
        {"emoji": getattr(r.reaction, "emoticon", None) or type(r.reaction).__name__, "count": r.count}
        for r in results
    ]


def link_preview(media) -> dict | None:
    page = media.webpage if isinstance(media, types.MessageMediaWebPage) else None
    fields = {"url": getattr(page, "url", None), "title": getattr(page, "title", None), "site": getattr(page, "site_name", None)}
    return {k: v for k, v in fields.items() if v} or None  # a pending preview has only a URL


def rich_text(node) -> str:
    """Plain text of a rich message (bots can send formatted blocks instead of
    plain text): one block per line, list items as "- …", links as "text (url)"."""
    if node is None:
        return ""
    name = type(node).__name__
    if name == "TextPlain":
        return node.text
    if name == "TextEmpty":
        return ""
    if name == "TextConcat":
        return "".join(rich_text(t) for t in node.texts)
    if name == "TextUrl":
        text = rich_text(node.text)
        return text if node.url in text else f"{text} ({node.url})"
    if name.startswith("Text"):  # bold, italic, fixed width, …: keep the words
        return rich_text(getattr(node, "text", None))
    if name.startswith("PageListItem"):
        return "- " + (rich_text(node.text) if hasattr(node, "text") else "\n  ".join(map(rich_text, node.blocks)))
    if name == "PageBlockDivider":
        return "—"
    parts = []
    for value in vars(node).values():  # any other block: the text of whatever it contains
        for child in value if isinstance(value, list) else [value]:
            if hasattr(child, "to_dict") and (text := rich_text(child)):
                parts.append(text)
    return "\n".join(parts)


def service(msg) -> str | None:
    """What a service message is a notice of, like "history_clear" when the chat was cleared. None for a message."""
    action = getattr(msg, "action", None)
    return snake(type(action).__name__.removeprefix("MessageAction")) if action else None


def message_event(msg, agent: Agent, attachments: list[dict] | None = None) -> dict:
    """One Telegram message in the shared event format, without kind and emitted_at."""
    rich = getattr(msg, "rich_message", None)
    rich_media = len(rich.photos) + len(rich.documents) if rich else 0
    event = {
        "channel": "telegram",
        "agent": agent.name,
        "handle": agent.handle,
        "guid": f"telegram:{msg.chat_id}:{msg.id}",
        "message_id": msg.id,
        "is_from_me": bool(msg.out),
        "text": msg.message or rich_text(rich) or None,
        "service": service(msg),  # a notice Telegram put in the chat, not a message anyone sent (bench.resets)
        "rich": True if rich else None,
        "rich_media": rich_media or None,  # photos or files inside a rich message; not downloaded yet
        "date": iso(msg.date),
        "date_edited": iso(msg.edit_date),
        "reply_to": getattr(msg.reply_to, "reply_to_msg_id", None),
        "buttons": buttons(msg.reply_markup),
        "reactions": reactions(msg.reactions),
        "link_preview": link_preview(msg.media),
        "attachments": attachments or [],
    }
    return {k: v for k, v in event.items() if v is not None}


def signature(event: dict) -> str:
    return hashlib.sha1(json.dumps(event, sort_keys=True).encode()).hexdigest()


def safe_name(name: str) -> str:
    name = re.sub(r"[^A-Za-z0-9._-]", "_", name).lstrip(".")[:120]
    return name or "attachment"


# MARK: - Channel

class TelegramChannel:
    """A connected Telegram session that records the listed agents' chats.

        async with TelegramChannel(agents) as telegram:
            await telegram.sync()
            await telegram.send(agent, "hi")
            await telegram.listen(120)
    """

    def __init__(self, agents: list[Agent], *, client=None, session_dir: Path = SESSION_DIR,
                 inbox: Path | None = None, on_event: Callable[[dict], None] | None = None):
        self.agents = {a.name: a for a in agents if a.channel == "telegram"}
        if not self.agents:
            raise TelegramError("no agent in config/agents.yaml has channel: telegram and a handle")
        self._given_client = client
        self.session_dir = session_dir
        self.inbox = inbox or inbox_path("telegram")
        self.state_path = self.inbox.parent / "state.json"
        self.pins_path = session_dir / "peers.json"
        self.attachments_dir = self.inbox.parent / "attachments"
        self.on_event = on_event
        self.state: dict[str, dict] = {}
        self.peers: dict[str, types.InputPeerUser] = {}
        self.by_peer: dict[int, Agent] = {}
        self.client = None
        self._lock: SessionLock | None = None

    async def __aenter__(self):
        self._lock = SessionLock(self.session_dir).__enter__()
        try:
            self.client = self._given_client or make_client(self.session_dir)
            await self.client.connect()
            if not await self.client.is_user_authorized():
                raise TelegramError("not logged in to Telegram yet; run: uv run bench telegram-login")
            self.state = json.loads(self.state_path.read_text()) if self.state_path.exists() else {}
            for agent in self.agents.values():
                await self._pin(agent)
            self._listen_for_updates()
        except BaseException:
            await self._close()
            raise
        return self

    async def __aexit__(self, *exc):
        await self._close()

    async def _close(self):
        if self.client is not None:
            await self.client.disconnect()
            secure(self.session_dir)
        if self._lock is not None:
            self._lock.__exit__()
            self._lock = None

    async def _pin(self, agent: Agent) -> None:
        """Ties the agent to one Telegram account, the first time its username is resolved."""
        pins = json.loads(self.pins_path.read_text()) if self.pins_path.exists() else {}
        pin = pins.get(agent.name)
        if not pin or pin["username"] != agent.handle:
            entity = await self.client.get_entity(agent.handle)
            if not isinstance(entity, types.User):
                raise TelegramError(f"{agent.handle} is a group or channel, not a person or bot")
            pin = {"username": agent.handle, "id": entity.id, "access_hash": entity.access_hash, "bot": bool(entity.bot)}
            pins[agent.name] = pin
            self.pins_path.write_text(json.dumps(pins, indent=2))
            os.chmod(self.pins_path, 0o600)
        self.peers[agent.name] = types.InputPeerUser(user_id=pin["id"], access_hash=pin["access_hash"])
        self.by_peer[pin["id"]] = agent
        state = self.state.get(agent.name)
        if not state or state["peer_id"] != pin["id"]:
            self.state[agent.name] = {"peer_id": pin["id"], "last_id": 0, "tracked": {}}

    def _listen_for_updates(self) -> None:
        peers = list(self.by_peer)
        self.client.add_event_handler(self._on_message, events.NewMessage(chats=peers))
        self.client.add_event_handler(self._on_message, events.MessageEdited(chats=peers))
        self.client.add_event_handler(self._on_read, events.MessageRead(chats=peers, inbox=False))
        self.client.add_event_handler(self._on_deleted, events.MessageDeleted())
        self.client.add_event_handler(self._on_typing, events.Raw(types.UpdateUserTyping))
        self.client.add_event_handler(self._on_reactions, events.Raw(types.UpdateMessageReactions))

    # MARK: Recording

    async def _record(self, agent: Agent, msg) -> dict | None:
        """Writes a "new" or "update" event if the message is new or changed."""
        state = self.state[agent.name]
        if msg.chat_id != state["peer_id"]:
            return None  # never record another chat, whatever handler passed it in
        key = str(msg.id)
        tracked = state["tracked"].get(key)
        if tracked is None and msg.id <= state["last_id"]:
            return None  # older than anything still being watched
        event = message_event(msg, agent, await self._attachments(msg))
        if event["is_from_me"]:
            previous = tracked["event"] if tracked else {}
            event["read"] = previous.get("read", False)
            if "date_read" in previous:
                event["date_read"] = previous["date_read"]
        sig = signature(event)
        if tracked and tracked["sig"] == sig:
            return None
        state["tracked"][key] = {"sig": sig, "event": event}
        state["last_id"] = max(state["last_id"], msg.id)
        return self._emit("update" if tracked else "new", event)

    def _emit(self, kind: str, event: dict) -> dict:
        line = {"kind": kind, "emitted_at": now_iso(), **event}
        self.inbox.parent.mkdir(parents=True, exist_ok=True)
        with self.inbox.open("a") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
        self.state_path.write_text(json.dumps(self.state))
        if self.on_event:
            self.on_event(line)
        return line

    async def _attachments(self, msg) -> list[dict]:
        if not isinstance(msg.media, (types.MessageMediaPhoto, types.MessageMediaDocument)):
            return []
        file = msg.file
        name = file.name or f"{'voice' if msg.voice else 'photo' if msg.photo else 'file'}{file.ext or ''}"
        dest = self.attachments_dir / str(msg.chat_id) / str(msg.id) / safe_name(name)
        info = {"name": name, "mime_type": file.mime_type, "bytes": file.size}
        if not dest.exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                await self.client.download_media(msg, file=str(dest))
            except Exception as e:  # recorded without a path; the next sync tries again
                print(f"telegram: couldn't download an attachment of message {msg.id}: {e}", file=sys.stderr)
                return [info]
        info["path"] = str(dest.relative_to(self.inbox.parent))
        return [info]

    def _mark_read(self, agent: Agent, max_id: int, observed: str | None) -> int:
        """Marks our messages up to max_id as read. `observed` is when the receipt arrived, if seen live."""
        count = 0
        for key, tracked in sorted(self.state[agent.name]["tracked"].items(), key=lambda kv: int(kv[0])):
            event = tracked["event"]
            if event["is_from_me"] and not event.get("read") and int(key) <= max_id:
                event = {**event, "read": True, **({"date_read": observed} if observed else {})}
                tracked.update(sig=signature(event), event=event)
                self._emit("update", event)
                count += 1
        return count

    def _prune(self, agent: Agent) -> None:
        cutoff = iso(datetime.now(UTC) - TRACK_WINDOW)
        tracked = self.state[agent.name]["tracked"]
        for key in [k for k, t in tracked.items() if t["event"].get("date", "") < cutoff]:
            del tracked[key]
        self.state_path.write_text(json.dumps(self.state))

    # MARK: Live updates

    async def _on_message(self, event) -> None:
        agent = self.by_peer.get(event.message.chat_id)
        if agent:
            await self._record(agent, event.message)

    async def _on_read(self, event) -> None:
        agent = self.by_peer.get(event.chat_id)
        if agent:
            self._mark_read(agent, event.max_id, observed=now_iso())

    async def _on_deleted(self, event) -> None:
        if event.chat_id is not None and event.chat_id not in self.by_peer:
            return
        # Private-chat deletions don't say which chat; message IDs are unique per account.
        for agent in self.agents.values():
            tracked = self.state[agent.name]["tracked"]
            for msg_id in event.deleted_ids:
                if (entry := tracked.pop(str(msg_id), None)) is not None:
                    self._emit("deleted", {**entry["event"], "date_deleted": now_iso()})

    async def _on_typing(self, update) -> None:
        agent = self.by_peer.get(update.user_id)
        if agent:
            action = snake(type(update.action).__name__.removeprefix("SendMessage").removesuffix("Action"))
            line = {"kind": "typing", "emitted_at": now_iso(), "channel": "telegram",
                    "agent": agent.name, "handle": agent.handle, "action": action}
            self.inbox.parent.mkdir(parents=True, exist_ok=True)
            with self.inbox.open("a") as f:
                f.write(json.dumps(line) + "\n")
            if self.on_event:
                self.on_event(line)

    async def _on_reactions(self, update) -> None:
        agent = self.by_peer.get(getattr(update.peer, "user_id", None))
        if agent:
            msg = await self.client.get_messages(self.peers[agent.name], ids=update.msg_id)
            if msg:
                await self._record(agent, msg)

    # MARK: What the runner calls

    async def sync(self) -> int:
        """Records what arrived or changed since the last recorded message. Returns events written."""
        written = 0
        for agent in self.agents.values():
            state, peer = self.state[agent.name], self.peers[agent.name]
            async for msg in self.client.iter_messages(peer, min_id=state["last_id"], reverse=True):
                written += bool(await self._record(agent, msg))
            async for msg in self.client.iter_messages(peer, limit=REFRESH_LIMIT):
                if str(msg.id) in state["tracked"]:
                    written += bool(await self._record(agent, msg))
            result = await self.client(functions.messages.GetPeerDialogsRequest(peers=[types.InputDialogPeer(peer)]))
            if result.dialogs:
                written += self._mark_read(agent, result.dialogs[0].read_outbox_max_id, observed=None)
            self._prune(agent)
        return written

    async def send(self, agent: Agent, text: str) -> dict | None:
        if self.agents.get(agent.name) != agent:
            raise TelegramError(f"{agent.name} isn't a Telegram agent in config/agents.yaml")
        if not text.strip():
            raise TelegramError("refusing to send an empty message")
        msg = await self.client.send_message(self.peers[agent.name], text, link_preview=False)
        return await self._record(agent, msg)

    async def listen(self, seconds: float | None = None) -> None:
        """Keeps recording live updates for `seconds`, or until interrupted."""
        if seconds is None:
            await self.client.run_until_disconnected()
        else:
            await asyncio.sleep(seconds)


# MARK: - Login and logout

def print_qr(url: str) -> None:
    show_qr(url, "On your iPhone: Telegram → Settings → Devices → Link Desktop Device, then scan this:")


async def login(session_dir: Path = SESSION_DIR, *, client=None, show_qr=print_qr, ask_password=getpass) -> str:
    """Logs this Mac in to your Telegram account by QR code. Returns your name."""
    with SessionLock(session_dir):
        client = client or make_client(session_dir)
        await client.connect()
        try:
            if not await client.is_user_authorized():
                qr = await client.qr_login()
                while True:
                    show_qr(qr.url)
                    try:
                        await qr.wait()
                        break
                    except asyncio.TimeoutError:
                        await qr.recreate()
                    except SessionPasswordNeededError:
                        # You type this yourself; it goes straight to Telegram and is never stored.
                        await client.sign_in(password=ask_password("Telegram cloud password (two-step verification): "))
                        break
            me = await client.get_me()
            return " ".join(filter(None, [me.first_name, me.last_name])) or me.username or str(me.id)
        finally:
            await client.disconnect()
            secure(session_dir)


async def logout(session_dir: Path = SESSION_DIR) -> bool:
    """Ends this Mac's Telegram session and deletes it. Returns False if it wasn't logged in."""
    with SessionLock(session_dir):
        client = make_client(session_dir)
        await client.connect()
        logged_in = await client.is_user_authorized()
        if logged_in:
            await client.log_out()
        else:
            await client.disconnect()
        for path in [*session_dir.glob("bench.session*"), session_dir / "peers.json"]:
            path.unlink(missing_ok=True)
        return logged_in
