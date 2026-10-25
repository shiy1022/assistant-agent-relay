"""WhatsApp channel: the runner's side of bench-whatsapp (whatsapp/*.go).

bench-whatsapp runs in the background as a LaunchAgent, linked to your
WhatsApp account, and records the listed agents' chats into its own inbox.
This module installs and links it, keeps its agent list in step with
config/agents.yaml, sends through its local socket and follows its inbox.
"""

import json
import os
import socket
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

from bench.config import PRIVATE_DIR, ROOT, Agent, load_agents
from bench.inbox import Inbox, inbox_path
from bench.qr import show_qr

BRIDGE_DIR = PRIVATE_DIR / "whatsapp"
BINARY = BRIDGE_DIR / "bench-whatsapp"
SOCKET = BRIDGE_DIR / "bench.sock"
LABEL = "local.personal-agent-benchmark.bench-whatsapp"
PLIST = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
LOG = Path.home() / "Library" / "Logs" / "bench-whatsapp.log"


class WhatsAppError(Exception):
    pass


def jid(handle: str) -> str:
    """The WhatsApp address for a handle: a phone number, or a bot ID like 1807055946647697@bot."""
    return handle if handle.endswith("@bot") else handle.removeprefix("+") + "@s.whatsapp.net"


def write_agents(agents: list[Agent] | None = None, bridge_dir: Path = BRIDGE_DIR) -> list[str]:
    """Writes the bridge's agents.json, the only chats it records or messages."""
    if agents is None:
        agents = list(load_agents().values())
    entries = [{"name": a.name, "handle": a.handle, "jids": [jid(a.handle)]} for a in agents if a.channel == "whatsapp"]
    bridge_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(bridge_dir, 0o700)
    path = bridge_dir / "agents.json"
    text = json.dumps(entries, indent=2)
    if not path.exists() or path.read_text() != text:  # the bridge reloads it when it changes
        tmp = path.with_suffix(".tmp")
        tmp.write_text(text)
        os.chmod(tmp, 0o600)
        tmp.replace(path)
    return [e["name"] for e in entries]


def request(op: str, *, socket_path: Path = SOCKET, timeout: float = 60, **fields) -> dict:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect(str(socket_path))
            s.sendall((json.dumps({"op": op, **fields}) + "\n").encode())
            data = b""
            while not data.endswith(b"\n"):
                chunk = s.recv(65536)
                if not chunk:
                    break
                data += chunk
    except (FileNotFoundError, ConnectionRefusedError):
        raise WhatsAppError("bench-whatsapp isn't running. Link it with: uv run bench whatsapp-link") from None
    except TimeoutError:
        raise WhatsAppError("bench-whatsapp didn't answer in time") from None
    reply = json.loads(data or b"{}")
    if not reply.get("ok"):
        raise WhatsAppError(reply.get("error", "bench-whatsapp refused the request"))
    return reply


def send(agent: Agent, text: str, **kw) -> dict:
    if agent.channel != "whatsapp":
        raise WhatsAppError(f"{agent.name} isn't a WhatsApp agent in config/agents.yaml")
    return request("send", agent=agent.name, text=text, **kw)["event"]


def set_online(on: bool, **kw) -> None:
    """Online lets agents' "typing…" reach the bridge; it's off between runs."""
    request("presence", online=on, **kw)


def open_inbox(path: Path | None = None) -> Inbox:
    """The bridge's inbox, positioned after what's already there."""
    box = Inbox(path or inbox_path("whatsapp"))
    box.poll()
    return box


def follow(box: Inbox, agents: set[str], seconds: float | None, on_event: Callable[[dict], None]) -> None:
    end = None if seconds is None else time.monotonic() + seconds
    while end is None or time.monotonic() < end:
        for event in box.poll():
            if event.get("agent") in agents:
                on_event(event)
        time.sleep(0.5)


# MARK: Install, link, status

def launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def domain() -> str:
    return f"gui/{os.getuid()}"


def running() -> bool:
    return launchctl("print", f"{domain()}/{LABEL}").returncode == 0


def start() -> None:
    launchctl("bootout", f"{domain()}/{LABEL}")
    result = launchctl("bootstrap", domain(), str(PLIST))
    if result.returncode != 0:
        raise WhatsAppError(f"couldn't start bench-whatsapp: {result.stderr.strip()}")


def stop() -> None:
    launchctl("bootout", f"{domain()}/{LABEL}")


def install() -> None:
    result = subprocess.run([str(ROOT / "whatsapp" / "install.sh")], capture_output=True, text=True)
    if result.returncode != 0:
        raise WhatsAppError(f"building bench-whatsapp failed:\n{result.stderr.strip()}")
    write_agents()


def link(show=None) -> str:
    """Links bench-whatsapp to your WhatsApp by QR code, then starts it. Returns your number."""
    show = show or (lambda code: show_qr(code, "On your iPhone: WhatsApp → Settings → Linked Devices → Link a Device, then scan this:"))
    if not BINARY.exists():
        raise WhatsAppError("bench-whatsapp isn't installed yet. Run: uv run bench whatsapp-install")
    write_agents()
    stop()  # the background bridge holds the session; it starts again once linking is done
    proc = subprocess.Popen([str(BINARY), "link"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    phone = None
    for line in proc.stdout:
        event = json.loads(line)
        if event["event"] == "code":
            show(event["code"])
        elif event["event"] in ("success", "already_linked"):
            phone = event["phone"]
    proc.wait()
    if proc.returncode != 0 or not phone:
        raise WhatsAppError(proc.stderr.read().strip() or "linking didn't finish")
    start()
    return phone


def unlink() -> None:
    stop()
    if BINARY.exists():
        result = subprocess.run([str(BINARY), "unlink"], capture_output=True, text=True)
        if result.returncode != 0:
            raise WhatsAppError(result.stderr.strip())


def describe() -> str:
    lines = []
    if not BINARY.exists():
        return "bench-whatsapp isn't installed. Run: uv run bench whatsapp-install"
    lines.append(f"bench-whatsapp is {'running' if running() else 'not running'} in the background.")
    try:
        s = request("status", timeout=5)
        lines.append(
            f"WhatsApp: {'linked' if s['logged_in'] else 'not linked'}, {s['status']}, "
            f"{'online' if s['online'] else 'offline'}; agents: {', '.join(s['agents']) or 'none'}"
        )
    except WhatsAppError:
        status_file = BRIDGE_DIR / "status.json"
        if status_file.exists():
            s = json.loads(status_file.read_text())
            lines.append(f"Last status: {s['status']} since {s['since']}")
    if LOG.exists():
        lines += ["", f"Last lines of {LOG}:", *LOG.read_text().splitlines()[-6:]]
    return "\n".join(lines)
