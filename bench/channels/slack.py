"""Slack channel: DMs agents from your own Slack account through Slack's Web
API, with a user token from a small Slack app you install in one workspace.

Only one workspace is ever used: the one named under `slack: workspace:` in
config/agents.yaml. Before anything else, every command checks that the token
belongs to that workspace and refuses to go on if it doesn't. The workspace's
ID is pinned the first time, so a token for another workspace with the same
name is refused too. The only conversations opened, read or written are the
DMs with agents in config/agents.yaml that have `channel: slack`, each pinned
to the agent's Slack member ID the first time its handle is resolved.

Slack keeps the whole DM on its servers, so nothing has to run in the
background: `sync` records whatever arrived or changed since the last run,
and `listen` polls every few seconds. Slack's API has no read receipts or
typing indicators, so those are never recorded.

Sending goes through the API by default. With `slack: send_via: web`, messages
are typed into Slack's web client instead, through SlackWeb
(bench/channels/slack_web.py). Bots like the dot ignore messages that Slack
marks as sent by an app. Each typed message is then found and recorded
through the API, as usual.
"""

import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from bench.config import PRIVATE_DIR, Agent, slack_send_via, slack_workspace
from bench.inbox import inbox_path

PIN_DIR = PRIVATE_DIR / "slack"
KEYCHAIN_SERVICE = "agent-runner.slack.user_token"
API = "https://slack.com/api/"
# What the runner needs. files:read is optional: without it, attachments are
# recorded by name but not downloaded.
REQUIRED_SCOPES = ("chat:write", "im:history", "im:write", "users:read")
TRACK_WINDOW = timedelta(hours=48)   # how long a message is watched for edits, reactions and deletion
ACTIVE_THREAD = timedelta(minutes=10)  # a thread with a reply this recent is re-read on every poll, to catch streamed edits
REFRESH_LIMIT = 50                   # recent messages re-read by sync, to catch edits made while offline
POLL_SECONDS = 5                     # how often listen checks for new messages
MAX_TEXT = 40_000                    # Slack cuts longer messages off
TYPED_SECONDS = 30                   # how long a message typed in the web client may take to show up in the API
RETRIES = 3                          # times a rate-limited call is retried, after the wait Slack asks for


class SlackError(Exception):
    pass


class SlackNetworkError(SlackError):
    """Slack couldn't be reached or answered with an HTTP error. Worth retrying later."""


class SlackAPIError(SlackError):
    def __init__(self, method: str, error: str, needed: str | None = None):
        self.method, self.error, self.needed = method, error, needed
        hints = {
            "invalid_auth": "the Slack token was rejected. Store a new one (see README.md, Slack).",
            "not_authed": "no Slack token was sent.",
            "token_revoked": "the Slack token was revoked. Reinstall the app and store the new token.",
            "token_expired": "the Slack token expired. Turn off token rotation for the app and store a new token.",
            "account_inactive": "the Slack account this token belongs to is deactivated.",
            "missing_scope": f"the Slack app is missing the {needed} scope. Add it under OAuth & Permissions → "
                             "User Token Scopes, reinstall the app, and store the new token.",
            "user_not_found": "Slack has no member with that ID in this workspace.",
            "channel_not_found": "the DM with this agent doesn't exist any more.",
        }
        super().__init__(f"Slack {method}: {hints.get(error, error)}")


def user_token() -> str:
    """The Slack user token from the Keychain, or SLACK_USER_TOKEN."""
    token = os.environ.get("SLACK_USER_TOKEN", "")
    if not token:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"], capture_output=True, text=True,
        )
        token = result.stdout.strip() if result.returncode == 0 else ""
    if not token:
        raise SlackError(
            "no Slack token found. Create the Slack app (see README.md, Slack), then store its User OAuth Token with:\n"
            f'  security add-generic-password -U -a "$USER" -s {KEYCHAIN_SERVICE} -w'
        )
    if not token.startswith("xoxp-"):
        raise SlackError(
            "the stored Slack token isn't a user token (xoxp-…). A bot token (xoxb-…) would post as the app, "
            "and a dot only answers its owner. Store the User OAuth Token instead."
        )
    return token


class SlackOnly(urllib.request.HTTPRedirectHandler):
    """Follows redirects only within slack.com, so the token is never sent anywhere else."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not is_slack_url(newurl):
            raise SlackNetworkError(f"Slack redirected to {urllib.parse.urlsplit(newurl).hostname}; not following")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def is_slack_url(url: str) -> bool:
    parts = urllib.parse.urlsplit(url)
    host = parts.hostname or ""
    return parts.scheme == "https" and (host == "slack.com" or host.endswith(".slack.com"))


class SlackAPI:
    """Slack's Web API with one user token. Every request goes to slack.com."""

    def __init__(self, token: str, *, sleep: Callable[[float], None] = time.sleep):
        self.token = token
        self.sleep = sleep
        self.opener = urllib.request.build_opener(SlackOnly)

    def call(self, method: str, **params) -> dict:
        body = urllib.parse.urlencode(
            {k: ("true" if v else "false") if isinstance(v, bool) else v for k, v in params.items() if v is not None}
        ).encode()
        for attempt in range(RETRIES + 1):
            request = urllib.request.Request(API + method, data=body, headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
            })
            try:
                with self.opener.open(request, timeout=30) as response:
                    payload = json.load(response)
                    scopes = response.headers.get("x-oauth-scopes", "")
                break
            except urllib.error.HTTPError as e:
                if e.code == 429 and attempt < RETRIES:
                    self.sleep(float(e.headers.get("Retry-After") or 5))
                    continue
                raise SlackNetworkError(f"Slack {method}: HTTP {e.code}") from None
            except OSError as e:  # URLError, timeouts, dropped connections
                raise SlackNetworkError(f"couldn't reach Slack ({method}): {getattr(e, 'reason', e)}") from None
        if not payload.get("ok"):
            raise SlackAPIError(method, payload.get("error", "unknown error"), payload.get("needed"))
        if method == "auth.test":
            payload["scopes"] = [s.strip() for s in scopes.split(",") if s.strip()]
        return payload

    def download(self, url: str, dest: Path) -> None:
        if not is_slack_url(url):
            raise SlackNetworkError(f"not downloading from {urllib.parse.urlsplit(url).hostname}, which isn't Slack")
        request = urllib.request.Request(url, headers={"Authorization": f"Bearer {self.token}"})
        try:
            with self.opener.open(request, timeout=120) as response:
                if response.headers.get_content_type() == "text/html":
                    raise SlackNetworkError("Slack sent a sign-in page instead of the file")
                dest.write_bytes(response.read())
        except OSError as e:
            raise SlackNetworkError(f"couldn't download {dest.name}: {getattr(e, 'reason', e)}") from None


class ChannelLock:
    """One bench command uses Slack at a time, so two never write the same state."""

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
            raise SlackError("another bench command is using Slack; wait for it to finish") from None
        return self

    def __exit__(self, *exc):
        fcntl.flock(self.file, fcntl.LOCK_UN)
        self.file.close()


def domain(url: str) -> str:
    """pjtty for https://pjtty.slack.com/."""
    return (urllib.parse.urlsplit(url).hostname or "").removesuffix(".slack.com")


def check_workspace(auth: dict, workspace: str, pins: dict) -> None:
    """Refuses a token for any workspace but the configured one (and, once pinned, its ID)."""
    where = f"{auth.get('team')} ({auth.get('url')})"
    if workspace.lower() not in (str(auth.get("team", "")).lower(), domain(auth.get("url", "")).lower()):
        raise SlackError(f"the Slack token is for {where}, not {workspace}. Nothing was read or sent.")
    pinned = pins.get("team_id")
    if pinned and pinned != auth.get("team_id"):
        raise SlackError(
            f"the Slack token is for {where}, team {auth.get('team_id')}, but {workspace} was pinned as team {pinned}. "
            f"Nothing was read or sent. If {workspace} really is a new workspace, delete {PIN_DIR / 'pins.json'}."
        )
    missing = [s for s in REQUIRED_SCOPES if s not in auth.get("scopes", [])]
    if missing:
        raise SlackError(
            f"the Slack app is missing these user token scopes: {', '.join(missing)}. Add them under "
            "OAuth & Permissions → User Token Scopes, reinstall the app, and store the new token."
        )


# MARK: - Message → event

def iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def now_iso() -> str:
    return iso(datetime.now(UTC))


def ts_time(ts: str | None) -> datetime | None:
    """A Slack timestamp like 1729812345.123456 as a time."""
    if not ts:
        return None
    seconds, _, micros = ts.partition(".")
    return datetime.fromtimestamp(int(seconds), UTC).replace(microsecond=int((micros + "000000")[:6]))


def mrkdwn(text: str) -> str:
    """Plain text of Slack's markup: links as "label (url)", mentions as @name, entities unescaped."""
    def token(m: re.Match) -> str:
        target, _, label = m.group(1).partition("|")
        if target.startswith("@"):
            return "@" + (label or target[1:])
        if target.startswith("#"):
            return "#" + (label or target[1:])
        if target.startswith("!"):
            command = target[1:].split("^")[0]
            return label or ("@" + command if command in ("here", "channel", "everyone") else "")
        target = target.removeprefix("mailto:")
        return target if not label or label == target else f"{label} ({target})"
    text = re.sub(r"<([^<>]+)>", token, text or "")
    return text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


def rich_text(element: dict) -> str:
    """Plain text of a rich_text element (what Slack's own composer sends)."""
    kind = element.get("type")
    children = element.get("elements") or []
    if kind == "rich_text":  # paragraphs, lists, quotes and code blocks
        return "\n".join(rich_text(c) for c in children)
    if kind == "text":
        return element.get("text", "")
    if kind == "link":
        text, url = element.get("text"), element.get("url", "")
        return url if not text or text == url else f"{text} ({url})"
    if kind == "user":
        return "@" + element.get("user_id", "")
    if kind == "channel":
        return "#" + element.get("channel_id", "")
    if kind == "usergroup":
        return "@" + element.get("usergroup_id", "")
    if kind == "broadcast":
        return "@" + element.get("range", "here")
    if kind == "emoji":
        if element.get("unicode"):
            return "".join(chr(int(c, 16)) for c in element["unicode"].split("-"))
        return f":{element.get('name', '')}:"
    if kind == "date":
        return element.get("fallback") or ""
    if kind == "rich_text_list":
        ordered = element.get("style") == "ordered"
        indent = "  " * int(element.get("indent") or 0)
        return "\n".join(f"{indent}{f'{i}.' if ordered else '-'} {rich_text(c)}" for i, c in enumerate(children, 1))
    if kind == "rich_text_quote":
        return "\n".join("> " + line for line in "".join(map(rich_text, children)).splitlines())
    return "".join(map(rich_text, children))  # a paragraph or code block


def text_object(obj: dict | None) -> str:
    if not obj:
        return ""
    return mrkdwn(obj.get("text", "")) if obj.get("type") == "mrkdwn" else obj.get("text", "")


def block_text(blocks: list[dict]) -> str:
    """Plain text of a message's blocks, one block per paragraph."""
    parts = []
    for block in blocks:
        kind = block.get("type")
        if kind == "rich_text":
            parts.append(rich_text(block))
        elif kind in ("section", "header"):
            parts.append("\n".join(filter(None, [text_object(block.get("text")),
                                                 *(text_object(f) for f in block.get("fields") or [])])))
        elif kind == "markdown":
            parts.append(block.get("text", ""))
        elif kind == "context":
            parts.append(" ".join(text_object(e) for e in block.get("elements") or [] if e.get("type") != "image"))
        elif kind == "divider":
            parts.append("—")
        elif kind == "image":
            parts.append(f"[image: {block.get('alt_text') or block.get('image_url', '')}]")
    return "\n".join(p for p in parts if p)


def control(element: dict) -> dict:
    """A button or other control: its label, what kind it is, and its link if it has one."""
    label = element.get("text")
    cell = {"text": text_object(label) if isinstance(label, dict) else str(label or element.get("name") or ""),
            "kind": element.get("type", "button")}
    for key in ("url", "style", "value"):
        if element.get(key):
            cell[key] = element[key]
    return cell


def buttons(msg: dict) -> list[list[dict]] | None:
    """Rows of buttons and other controls on a message, from its blocks and legacy attachments."""
    rows = []
    for block in msg.get("blocks") or []:
        if block.get("type") == "actions":
            rows.append([control(e) for e in block.get("elements") or []])
        elif block.get("type") == "section" and block.get("accessory", {}).get("type") not in (None, "image"):
            rows.append([control(block["accessory"])])
    for attachment in msg.get("attachments") or []:
        if attachment.get("actions"):
            rows.append([control(a) for a in attachment["actions"]])
    return [r for r in rows if r] or None


def link_preview(msg: dict) -> dict | None:
    for attachment in msg.get("attachments") or []:
        url = attachment.get("original_url") or attachment.get("from_url")
        if url:
            fields = {"url": url, "title": attachment.get("title"), "site": attachment.get("service_name")}
            return {k: v for k, v in fields.items() if v}
    return None


def attachment_text(msg: dict) -> str:
    """Text of legacy attachments that aren't link previews (some bots still send these)."""
    parts = []
    for a in msg.get("attachments") or []:
        if a.get("original_url") or a.get("from_url"):
            continue
        fields = [f"{f.get('title')}: {f.get('value')}" if f.get("title") else str(f.get("value", ""))
                  for f in a.get("fields") or []]
        parts.append("\n".join(filter(None, [a.get("pretext"), a.get("title"), a.get("text"), *fields]))
                     or a.get("fallback", ""))
    return "\n".join(mrkdwn(p) for p in parts if p)


def message_text(msg: dict) -> str:
    """What the message shows: its blocks if it has any, else its text, plus any attachment text."""
    shown = block_text(msg["blocks"]) if msg.get("blocks") else ""
    return "\n".join(filter(None, [shown or mrkdwn(msg.get("text", "")), attachment_text(msg)]))


def reactions(msg: dict) -> list[dict] | None:
    return [{"emoji": f":{r['name']}:", "count": r.get("count", 0)} for r in msg.get("reactions") or []] or None


def is_reply(msg: dict) -> bool:
    """A reply in a thread. A thread's first message has thread_ts too, equal to its own ts."""
    return bool(msg.get("thread_ts")) and msg["thread_ts"] != msg["ts"]


def message_event(msg: dict, agent: Agent, channel_id: str, me: str, attachments: list[dict]) -> dict:
    """One Slack message in the shared event format, without kind and emitted_at."""
    ts = msg["ts"]
    event = {
        "channel": "slack",
        "agent": agent.name,
        "handle": agent.handle,
        "guid": f"slack:{channel_id}:{ts}",
        "message_id": ts,
        "is_from_me": msg.get("user") == me,
        "text": message_text(msg) or None,
        "rich": True if any(b.get("type") != "rich_text" for b in msg.get("blocks") or []) else None,
        "date": iso(ts_time(ts)),
        "date_edited": iso(ts_time((msg.get("edited") or {}).get("ts"))),
        "reply_to": msg["thread_ts"] if is_reply(msg) else None,  # the thread it's a reply in
        "subtype": msg.get("subtype"),
        "buttons": buttons(msg),
        "reactions": reactions(msg),
        "link_preview": link_preview(msg),
        "attachments": attachments,
    }
    return {k: v for k, v in event.items() if v is not None}


def signature(event: dict) -> str:
    return hashlib.sha1(json.dumps(event, sort_keys=True).encode()).hexdigest()


def safe_name(name: str) -> str:
    name = re.sub(r"[^A-Za-z0-9._-]", "_", name).lstrip(".")[:120]
    return name or "attachment"


def escape(text: str) -> str:
    """Slack reads <, > and & as markup, so they're escaped the way Slack's own composer does."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# MARK: - Channel

class SlackChannel:
    """A Slack session that records the listed agents' DMs.

        with SlackChannel(agents) as slack:
            slack.sync()
            slack.send(agent, "hi")
            slack.listen(120)
    """

    def __init__(self, agents: list[Agent], *, api=None, workspace: str | None = None, pin_dir: Path = PIN_DIR,
                 inbox: Path | None = None, on_event: Callable[[dict], None] | None = None,
                 send_via: str | None = None, web=None,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic):
        self.agents = {a.name: a for a in agents if a.channel == "slack"}
        if not self.agents:
            raise SlackError("no agent in config/agents.yaml has channel: slack and a handle")
        self._given_api = api
        self.workspace = workspace
        self.pin_dir = pin_dir
        self.pins_path = pin_dir / "pins.json"
        self.inbox = inbox or inbox_path("slack")
        self.state_path = self.inbox.parent / "state.json"
        self.attachments_dir = self.inbox.parent / "attachments"
        self.on_event = on_event
        self.send_via = send_via
        self.web = web      # a SlackWeb, attached on the first typed send unless one is given
        self._own_web = None
        self.sleep, self.clock = sleep, clock
        self.api = None
        self.auth: dict = {}
        self.pins: dict = {}
        self.state: dict[str, dict] = {}
        self._lock: ChannelLock | None = None

    def __enter__(self):
        self._lock = ChannelLock(self.pin_dir).__enter__()
        try:
            self.api = self._given_api or SlackAPI(user_token(), sleep=self.sleep)
            self.auth, self.pins = connect(self.api, self.workspace or slack_workspace(), self.pins_path)
            self.send_via = self.send_via or slack_send_via()
            self.state = json.loads(self.state_path.read_text()) if self.state_path.exists() else {}
            for agent in self.agents.values():
                self._pin(agent)
        except BaseException:
            self.__exit__()
            raise
        return self

    def __exit__(self, *exc):
        if self._own_web is not None:
            self._own_web.__exit__()
            self._own_web = self.web = None
        if self._lock is not None:
            self._lock.__exit__()
            self._lock = None

    @property
    def me(self) -> str:
        return self.auth["user_id"]

    def conversation(self, agent: Agent):
        """The agent's DM, as SlackWeb addresses it."""
        return conversation(self.pins, agent.name)

    def _web(self):
        if self.web is None:
            from bench.channels.slack_web import SlackWeb
            self.web = self._own_web = SlackWeb().__enter__()
        return self.web

    def _pin(self, agent: Agent) -> None:
        """Ties the agent to one Slack member and the DM with them, the first time its handle is resolved."""
        pin = self.pins.setdefault("agents", {}).get(agent.name)
        if not pin or pin["handle"] != agent.handle or pin["me"] != self.me or "label" not in pin:
            member = resolve(self.api, agent.handle)
            opened = self.api.call("conversations.open", users=member["id"], return_im=True)["channel"]
            if not opened["id"].startswith("D"):  # only ever a one-to-one DM
                raise SlackError(f"Slack opened {opened['id']} for {agent.handle}, which isn't a DM")
            pin = {"handle": agent.handle, "me": self.me, "user_id": member["id"], "name": member.get("name"),
                   "label": label(member), "bot": bool(member.get("is_bot")), "dm": opened["id"]}
            self.pins["agents"][agent.name] = pin
            save_pins(self.pins_path, self.pins)
        state = self.state.get(agent.name)
        if not state or state["dm"] != pin["dm"]:
            self.state[agent.name] = {"dm": pin["dm"], "synced": False, "last_ts": None, "tracked": {}, "threads": {}}

    # MARK: Recording

    def _seen_before(self, state: dict, msg: dict) -> bool:
        """Whether a message that isn't being watched is one recorded earlier and since let go."""
        if not state["synced"]:
            return False  # the first sync records the whole DM
        if is_reply(msg):
            mark = (state["threads"].get(msg["thread_ts"]) or {}).get("last_ts")
            if not mark:  # a thread that stopped being watched: anything older than the window was recorded
                return ts_time(msg["ts"]) < datetime.now(UTC) - TRACK_WINDOW
            return Decimal(msg["ts"]) <= Decimal(mark)
        return bool(state["last_ts"]) and Decimal(msg["ts"]) <= Decimal(state["last_ts"])

    def _record(self, agent: Agent, msg: dict) -> dict | None:
        """Writes a "new", "update" or "deleted" event if the message is new or changed."""
        state = self.state[agent.name]
        tracked = state["tracked"].get(msg["ts"])
        if msg.get("subtype") == "tombstone":  # a deleted message that still has replies
            return self._deleted(agent, msg["ts"]) if tracked else None
        if tracked is None and self._seen_before(state, msg):
            return None
        event = message_event(msg, agent, state["dm"], self.me, self._attachments(state["dm"], msg))
        sig = signature(event)
        if tracked and tracked["sig"] == sig:
            return None
        state["tracked"][msg["ts"]] = {"sig": sig, "event": event}
        return self._emit("update" if tracked else "new", event)

    def _deleted(self, agent: Agent, ts: str) -> dict | None:
        entry = self.state[agent.name]["tracked"].pop(ts, None)
        return self._emit("deleted", {**entry["event"], "date_deleted": now_iso()}) if entry else None

    def _emit(self, kind: str, event: dict) -> dict:
        line = {"kind": kind, "emitted_at": now_iso(), **event}
        self.inbox.parent.mkdir(parents=True, exist_ok=True)
        with self.inbox.open("a") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
        self._save_state()
        if self.on_event:
            self.on_event(line)
        return line

    def _attachments(self, dm: str, msg: dict) -> list[dict]:
        out = []
        can_download = "files:read" in self.auth.get("scopes", [])
        for file in msg.get("files") or []:
            name = file.get("name") or file.get("title") or file.get("id") or "file"
            info = {k: v for k, v in {"name": name, "mime_type": file.get("mimetype"), "bytes": file.get("size")}.items() if v}
            url = file.get("url_private_download") or file.get("url_private")
            dest = self.attachments_dir / dm / msg["ts"] / safe_name(name)
            if url and can_download and not dest.exists():
                dest.parent.mkdir(parents=True, exist_ok=True)
                try:
                    self.api.download(url, dest)
                except SlackError as e:  # recorded without a path; the next sync tries again
                    print(f"slack: couldn't download an attachment of message {msg['ts']}: {e}", file=sys.stderr)
            if dest.exists():
                info["path"] = str(dest.relative_to(self.inbox.parent))
            out.append(info)
        return out

    def _prune(self, agent: Agent) -> None:
        cutoff = datetime.now(UTC) - TRACK_WINDOW
        state = self.state[agent.name]
        for ts in [ts for ts, t in state["tracked"].items() if t["event"].get("date", "") < iso(cutoff)]:
            del state["tracked"][ts]
        for ts in [ts for ts, t in state["threads"].items() if ts_time(t["latest"]) < cutoff]:
            del state["threads"][ts]
        self._save_state()

    def _save_state(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(self.state))

    def _history(self, dm: str, *, oldest: str | None = None, every_page: bool) -> tuple[list[dict], bool]:
        """Top-level messages, newest first, and whether older ones weren't returned."""
        messages, cursor = [], None
        while True:
            page = self.api.call("conversations.history", channel=dm, oldest=oldest, cursor=cursor,
                                 limit=200 if every_page else REFRESH_LIMIT)
            messages += page.get("messages") or []
            cursor = (page.get("response_metadata") or {}).get("next_cursor")
            if not cursor or not every_page:
                return messages, bool(page.get("has_more")) and not every_page

    def _replies(self, dm: str, parent: str) -> list[dict]:
        messages, cursor = [], None
        while True:
            page = self.api.call("conversations.replies", channel=dm, ts=parent, cursor=cursor, limit=200)
            messages += [m for m in page.get("messages") or [] if m["ts"] != parent]
            cursor = (page.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                return messages

    def _sync_agent(self, agent: Agent) -> int:
        state = self.state[agent.name]
        dm, written = state["dm"], 0
        if state["synced"]:
            # Usually one call: the latest messages, which also shows edits made since.
            recent, more = self._history(dm, every_page=False)
            if more and state["last_ts"] and Decimal(recent[-1]["ts"]) > Decimal(state["last_ts"]):
                recent += self._history(dm, oldest=state["last_ts"], every_page=True)[0]  # a lot arrived
        else:  # first sync: the whole DM
            recent, more = self._history(dm, every_page=True)
        messages = {m["ts"]: m for m in recent}
        ordered = sorted(messages.values(), key=lambda m: Decimal(m["ts"]))
        for msg in ordered:
            written += bool(self._record(agent, msg))
        if ordered:
            last = ordered[-1]["ts"]
            if not state["last_ts"] or Decimal(last) > Decimal(state["last_ts"]):
                state["last_ts"] = last

        # A watched top-level message missing from a listing that covers its time was deleted.
        covered = min(Decimal(ts) for ts in messages) if more and messages else Decimal(0)
        for ts, tracked in list(state["tracked"].items()):
            if "reply_to" not in tracked["event"] and Decimal(ts) >= covered and ts not in messages:
                written += bool(self._deleted(agent, ts))

        # Threads are re-read when a reply is added, and while a recent reply may still be streaming in.
        active_since = datetime.now(UTC) - ACTIVE_THREAD
        for parent in ordered:
            latest = parent.get("latest_reply")
            if not parent.get("reply_count") or not latest:
                continue
            thread = state["threads"].get(parent["ts"])
            if thread and thread["latest"] == latest and ts_time(latest) < active_since:
                continue
            replies = sorted(self._replies(dm, parent["ts"]), key=lambda m: Decimal(m["ts"]))
            for reply in replies:
                written += bool(self._record(agent, reply))
            listed = {r["ts"] for r in replies}
            for ts, tracked in list(state["tracked"].items()):
                if tracked["event"].get("reply_to") == parent["ts"] and ts not in listed:
                    written += bool(self._deleted(agent, ts))
            state["threads"][parent["ts"]] = {"latest": latest, "last_ts": replies[-1]["ts"] if replies else None}

        state["synced"] = True
        self._prune(agent)
        return written

    # MARK: What the runner calls

    def sync(self) -> int:
        """Records what arrived or changed since the last recorded message. Returns events written."""
        return sum(self._sync_agent(agent) for agent in self.agents.values())

    def send(self, agent: Agent, text: str, *, thread: str | None = None) -> dict | None:
        """Sends `text` as you, in the DM or, with `thread`, as a reply in that thread."""
        if self.agents.get(agent.name) != agent:
            raise SlackError(f"{agent.name} isn't a Slack agent in config/agents.yaml")
        if not text.strip():
            raise SlackError("refusing to send an empty message")
        if len(text) > MAX_TEXT:
            raise SlackError(f"the message is {len(text)} characters; Slack cuts messages off after {MAX_TEXT}")
        dm = self.state[agent.name]["dm"]
        if self.send_via == "web":
            if thread:
                raise SlackError("replying in a thread isn't supported when sending through the web client")
            return self._send_typed(agent, dm, text)
        sent = self.api.call("chat.postMessage", channel=dm, text=escape(text), thread_ts=thread,
                             unfurl_links=False, unfurl_media=False)
        msg = {"ts": sent["ts"], **(sent.get("message") or {})}
        msg.setdefault("user", self.me)
        if thread:
            msg.setdefault("thread_ts", thread)
        return self._record(agent, msg)

    def _send_typed(self, agent: Agent, dm: str, text: str) -> dict | None:
        """Types the message in Slack's web client, then finds and records it through the API."""
        from bench.channels.slack_web import SlackWebError
        before = (self._history(dm, every_page=False)[0] or [{"ts": "0"}])[0]["ts"]
        pin = self.pins["agents"][agent.name]
        try:
            self._web().send(self.conversation(agent), text)
        except SlackWebError as e:
            raise SlackError(str(e)) from None
        deadline = self.clock() + TYPED_SECONDS
        while True:
            for msg in self._history(dm, every_page=False)[0]:
                typed = msg.get("user") == self.me and not msg.get("bot_id") and not msg.get("app_id")
                if typed and Decimal(msg["ts"]) > Decimal(before):
                    event = self._record(agent, msg)
                    shown = (event or {}).get("text") or message_text(msg)
                    if shown.split() != text.split():
                        print(f"slack: Slack shows the typed message as {shown!r}", file=sys.stderr)
                    return event
            if self.clock() >= deadline:
                raise SlackError(f"typed and sent the message in Slack's web client, but it didn't show up through "
                                 f"the API within {TYPED_SECONDS}s. Check the DM with {pin['label']}.")
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
            except SlackNetworkError as e:  # Slack or the network hiccuped; the next poll catches up
                print(f"slack: {e}; trying again in {poll_seconds:.0f}s", file=sys.stderr)
            if deadline is not None and self.clock() >= deadline:
                return


# MARK: - Workspace, members and status

def load_pins(path: Path) -> dict:
    return json.loads(path.read_text()) if path.exists() else {}


def save_pins(path: Path, pins: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(pins, indent=2))
    os.chmod(path, 0o600)


def connect(api, workspace: str, pins_path: Path) -> tuple[dict, dict]:
    """Checks the token is for `workspace` before anything else, and pins the workspace's ID."""
    auth = api.call("auth.test")
    pins = load_pins(pins_path)
    check_workspace(auth, workspace, pins)
    if not pins.get("team_id"):
        pins.update(team_id=auth["team_id"], team=auth.get("team"), url=auth.get("url"))
        save_pins(pins_path, pins)
    return auth, pins


def conversation(pins: dict, agent_name: str):
    """A pinned agent's DM as a slack_web.Conversation."""
    from bench.channels.slack_web import Conversation
    pin = pins["agents"][agent_name]
    return Conversation(team_id=pins["team_id"], team=pins["team"], channel_id=pin["dm"], label=pin["label"])


MEMBER_ID = re.compile(r"^[UW][A-Z0-9]{6,}$")


def label(member: dict) -> str:
    """The name Slack shows for a member, as in its "Message to …" box."""
    profile = member.get("profile") or {}
    return profile.get("display_name") or member.get("real_name") or profile.get("real_name") or member.get("name") or member["id"]


def resolve(api, handle: str) -> dict:
    """The workspace member a handle names: a member ID like U07ABCDEF, or @name."""
    if MEMBER_ID.match(handle):
        member = api.call("users.info", user=handle)["user"]
    else:
        wanted = handle.removeprefix("@").lower()
        matches, cursor = [], None
        while True:
            page = api.call("users.list", cursor=cursor, limit=200)
            for m in page.get("members") or []:
                profile = m.get("profile") or {}
                names = {m.get("name"), profile.get("display_name_normalized"), profile.get("real_name_normalized"),
                         profile.get("display_name"), m.get("real_name")}
                if not m.get("deleted") and wanted in {str(n).lower() for n in names if n}:
                    matches.append(m)
            cursor = (page.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                break
        if len(matches) != 1:
            found = ", ".join(f"{m.get('name')} ({m['id']})" for m in matches)
            raise SlackError(f"{len(matches)} members are named {handle}{': ' + found if found else ''}. "
                             "Use the member ID instead (the dot's profile in Slack → ⋮ → Copy member ID).")
        member = matches[0]
    if member.get("deleted"):
        raise SlackError(f"{handle} is deactivated in this workspace")
    return member


def check_web(pins: dict, agent: Agent, web=None) -> str:
    """Whether the agent's DM is open and ready in the web client, without typing anything."""
    from bench.channels.slack_web import SlackWeb, SlackWebError
    pin = (pins.get("agents") or {}).get(agent.name)
    if not pin or "label" not in pin:
        return f"not checked yet; run uv run bench sync {agent.name} first"
    try:
        if web is not None:
            return str(web.check(conversation(pins, agent.name)))
        with SlackWeb() as web:
            return str(web.check(conversation(pins, agent.name)))
    except SlackWebError as e:
        return f"NOT READY: {e}"


def sign_in(agents: list[Agent], *, pin_dir: Path = PIN_DIR, web=None) -> str:
    """Makes sure the dedicated Chrome is signed in to the pinned workspace, waiting while you sign in."""
    from bench.channels.slack_web import SlackWeb, SlackWebError
    pins = load_pins(pin_dir / "pins.json")
    agent = next((a for a in agents if a.channel == "slack" and a.name in (pins.get("agents") or {})), None)
    if not pins.get("team_id") or agent is None:
        raise SlackError("run uv run bench sync with a Slack agent first, so the workspace and its DM are pinned")
    dm = conversation(pins, agent.name)
    print(f"Checking that the dedicated Chrome is signed in to {pins['team']}. If it isn't, sign in there; "
          "when Slack offers its desktop app, choose to use Slack in your browser.", flush=True)
    try:
        if web is not None:
            already = web.sign_in(dm, pins["url"])
        else:
            with SlackWeb() as web:
                already = web.sign_in(dm, pins["url"])
    except SlackWebError as e:
        raise SlackError(str(e)) from None
    return f"{'Already signed' if already else 'Signed'} in to {pins['team']} in the dedicated Chrome."


def status(agents: list[Agent], *, api=None, workspace: str | None = None, pin_dir: Path = PIN_DIR,
           send_via: str | None = None, web=None) -> str:
    """Checks the token, the workspace and each agent's DM without sending or recording anything."""
    workspace = workspace or slack_workspace()
    send_via = send_via or slack_send_via()
    with ChannelLock(pin_dir):
        api = api or SlackAPI(user_token())
        auth, pins = connect(api, workspace, pin_dir / "pins.json")
        lines = [f"Slack: {auth.get('team')} ({auth.get('url')}), team {auth['team_id']}, "
                 f"signed in as {auth.get('user')} ({auth['user_id']})",
                 f"Token scopes: {', '.join(auth.get('scopes', []))}"]
        if "files:read" not in auth.get("scopes", []):
            lines.append("No files:read scope, so attachments are recorded by name only.")
        for agent in [a for a in agents if a.channel == "slack"]:
            member = resolve(api, agent.handle)
            kind = "bot" if member.get("is_bot") else "person"
            lines.append(f"{agent.name}: {agent.handle} → {member.get('name')} ({member['id']}, {kind})")
            if send_via == "web":
                lines.append(f"  web client: {check_web(pins, agent, web)}")
        lines.insert(2, f"Sending: {'typed into the web client' if send_via == 'web' else 'through the API'}")
        if not any(a.channel == "slack" for a in agents):
            lines.append("No Slack agent has a handle yet in config/agents.yaml.")
        return "\n".join(lines)
