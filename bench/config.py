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

CHANNELS = ("telegram", "whatsapp", "imessage")

# iMessage handles end up inside a Swift string literal in bench-reader, so
# these patterns also keep quotes and backslashes out.
PHONE = re.compile(r"^\+[1-9]\d{7,14}$")
EMAIL = re.compile(r"^[^@\s\"\\]+@[^@\s\"\\]+\.[^@\s\"\\]+$")
TELEGRAM_USERNAME = re.compile(r"^@[a-z][a-z0-9_]{3,31}$")
WHATSAPP_BOT = re.compile(r"^\d{5,25}@bot$")  # Meta's own agents, like Muse, have bot IDs instead of numbers
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
    like 1807055946647697@bot. imessage: E.164 phone number or lowercase
    email, as Messages stores them.
    """
    raw = raw.strip()
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
