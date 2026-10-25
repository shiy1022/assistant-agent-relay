"""Loads config/agents.yaml."""

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
AGENTS_FILE = ROOT / "config" / "agents.yaml"
INBOX_DIR = ROOT / "data" / "inbox"
# Secrets and sessions stay outside the repo, readable only by you.
PRIVATE_DIR = Path.home() / "Library" / "Application Support" / "agent-runner"

CHANNELS = ("telegram", "whatsapp", "slack", "imessage")

# iMessage handles end up inside a Swift string literal in bench-reader, so
# these patterns also keep quotes and backslashes out.
PHONE = re.compile(r"^\+[1-9]\d{7,14}$")
EMAIL = re.compile(r"^[^@\s\"\\]+@[^@\s\"\\]+\.[^@\s\"\\]+$")
TELEGRAM_USERNAME = re.compile(r"^@[a-z][a-z0-9_]{3,31}$")
WHATSAPP_BOT = re.compile(r"^\d{5,25}@bot$")  # Meta's own agents, like Muse, have bot IDs instead of numbers
# A Slack member ID (what "Copy member ID" gives), or the name they go by.
SLACK_MEMBER_ID = re.compile(r"^[UW][A-Z0-9]{6,}$")
SLACK_NAME = re.compile(r"^@[a-z0-9][a-z0-9._ -]{0,79}$")
NAME = re.compile(r"^[a-z][a-z0-9_-]*$")


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Agent:
    name: str
    handle: str
    quiet_period_seconds: float
    max_wait_seconds: float
    channel: str = "imessage"


def normalize_handle(raw: str, channel: str = "imessage") -> str:
    """Returns the handle in the form the channel uses.

    telegram: lowercase @username. whatsapp: E.164 phone number, or a bot ID
    like 1807055946647697@bot. slack: member ID like U07ABCDEF, or a lowercase
    @name. imessage: E.164 phone number or lowercase email, as Messages stores
    them.
    """
    raw = raw.strip()
    if channel == "slack":
        if SLACK_MEMBER_ID.match(raw):  # IDs are uppercase as copied; anything else is a name
            return raw
        handle = raw.lower() if raw.startswith("@") else "@" + raw.lower()
        if not SLACK_NAME.match(handle):
            raise ConfigError(f"{raw!r} is not a Slack member ID (like U07ABCDEF) or @name")
        return handle
    if channel == "telegram":
        handle = raw.lower() if raw.startswith("@") else "@" + raw.lower()
        if not TELEGRAM_USERNAME.match(handle):
            raise ConfigError(f"{raw!r} is not a Telegram username (5–32 letters, digits or _, like @fo_example)")
        return handle
    if channel == "whatsapp" and WHATSAPP_BOT.match(raw.lower()):
        return raw.lower()
    handle = raw.lower() if "@" in raw else re.sub(r"[\s().-]", "", raw)
    if PHONE.match(handle) or (channel == "imessage" and EMAIL.match(handle)):
        return handle
    if channel == "whatsapp":
        raise ConfigError(f"{raw!r} is not a phone number with country code (+15551234567) or a bot ID (digits@bot)")
    raise ConfigError(f"{raw!r} is not a phone number with country code (+15551234567) or an email address")


def slack_workspace(path: Path = AGENTS_FILE) -> str:
    """The one Slack workspace agent-runner may use, from `slack: workspace:` in agents.yaml."""
    data = yaml.safe_load(path.read_text()) or {}
    workspace = str((data.get("slack") or {}).get("workspace") or "").strip()
    if not workspace:
        raise ConfigError(f"set slack: workspace: in {path} to the one Slack workspace agent-runner may use")
    return workspace


def slack_send_via(path: Path = AGENTS_FILE) -> str:
    """How Slack messages are sent, from `slack: send_via:` in agents.yaml: "api" (the default) or "web"."""
    data = yaml.safe_load(path.read_text()) or {}
    send_via = str((data.get("slack") or {}).get("send_via") or "api").strip()
    if send_via not in ("api", "web"):
        raise ConfigError(f"slack: send_via: in {path} must be api or web, not {send_via!r}")
    return send_via


def load_agents(path: Path = AGENTS_FILE) -> dict[str, Agent]:
    """Agents that have a handle, keyed by name. Agents left blank are skipped."""
    data = yaml.safe_load(path.read_text()) or {}
    defaults = data.get("defaults") or {}
    agents: dict[str, Agent] = {}
    seen: dict[tuple[str, str], str] = {}
    for name, entry in (data.get("agents") or {}).items():
        if not NAME.match(str(name)):
            raise ConfigError(f"agent name {name!r} must be lowercase letters, digits, - or _")
        entry = entry or {}
        channel = str(entry.get("channel") or "imessage")
        if channel not in CHANNELS:
            raise ConfigError(f"agent {name}: channel must be one of {', '.join(CHANNELS)}, not {channel!r}")
        raw = str(entry.get("handle") or "").strip()
        if not raw:
            continue
        try:
            handle = normalize_handle(raw, channel)
        except ConfigError as e:
            raise ConfigError(f"agent {name}: {e}") from None
        if (channel, handle) in seen:
            raise ConfigError(f"agents {seen[channel, handle]} and {name} have the same handle")
        seen[channel, handle] = name
        if channel == "slack" and not str((data.get("slack") or {}).get("workspace") or "").strip():
            raise ConfigError(f"agent {name} uses Slack, so set slack: workspace: in {path} to the workspace it's in")
        agents[name] = Agent(
            name=name,
            handle=handle,
            quiet_period_seconds=float(entry.get("quiet_period_seconds", defaults.get("quiet_period_seconds", 90))),
            max_wait_seconds=float(entry.get("max_wait_seconds", defaults.get("max_wait_seconds", 900))),
            channel=channel,
        )
    return agents


def get_agent(name: str, path: Path = AGENTS_FILE) -> Agent:
    agents = load_agents(path)
    if name not in agents:
        known = ", ".join(agents) or "none have a handle yet"
        raise ConfigError(f"unknown agent {name!r} in {path} ({known})")
    return agents[name]
