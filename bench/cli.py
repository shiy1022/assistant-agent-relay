"""bench: run Assistant Bench tasks against personal agents.

    bench agents                          list agents that have a handle, and their channels
    bench send AGENT TEXT [--listen S]    send one message (then keep recording for S seconds)
    bench listen [AGENT] [--seconds S]    catch up, then record live until Ctrl-C (or S seconds)
    bench sync [AGENT]                    record whatever arrived or changed since last time
    bench inbox [--agent A] [--follow]    show what's been recorded
    bench telegram-login                  link this Mac to your Telegram account by QR code
    bench telegram-logout                 end that session and delete it
    bench whatsapp-install                build bench-whatsapp and install it in the background
    bench whatsapp-link                   link it to your WhatsApp by QR code, then start it
    bench whatsapp-status                 is it running, linked and recording?
    bench whatsapp-unlink                 log it out of WhatsApp and delete its session
    bench reader-status                   (iMessage, parked) is bench-reader running?
"""

import argparse
import asyncio
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from bench.config import AGENTS_FILE, CHANNELS, Agent, ConfigError, get_agent, load_agents
from bench.inbox import Inbox, inbox_path

READER_LABEL = "local.personal-agent-benchmark.bench-reader"
READER_LOG = Path.home() / "Library" / "Logs" / "bench-reader.log"


class ChannelError(Exception):
    pass


def local_time(iso: str | None) -> str:
    return datetime.fromisoformat(iso).astimezone().strftime("%m-%d %H:%M:%S") if iso else "--"


def format_event(event: dict) -> str:
    kind = event["kind"]
    if kind == "typing":
        return f"{local_time(event['emitted_at'])}  {event['agent']:<9} … {event['action'].replace('_', ' ')}"
    arrow = "→" if event["is_from_me"] else "←"
    head = f"{local_time(event.get('date'))}  {event['agent']:<9} {arrow} "
    if kind == "deleted":
        return head + "(deleted)"
    if kind == "update":
        changes = [
            f"{label} {local_time(event[key])}"
            for key, label in [("date_delivered", "delivered"), ("date_read", "read"),
                               ("date_edited", "edited"), ("date_retracted", "unsent")]
            if event.get(key)
        ]
        if event.get("read") and not event.get("date_read"):
            changes.append("read")
        changes += [f"attachment {a['path']}" for a in event.get("attachments", []) if a.get("path")]
        if event.get("reactions"):
            changes.append("reactions " + " ".join(f"{r['emoji']}×{r['count']}" for r in event["reactions"]))
        text = (event.get("text") or "").replace("\n", " ⏎ ")
        return head + f"(updated{': ' + ', '.join(changes) if changes else ''}) {text}".rstrip()
    text = (event.get("text") or "").replace("\n", " ⏎ ")
    extras = [f"[{a['name']}]" for a in event.get("attachments", [])]
    if event.get("buttons"):
        extras.append("[buttons: " + " | ".join(b["text"] for row in event["buttons"] for b in row) + "]")
    if event.get("tapback"):
        extras.append(f"[tapback {event['tapback']}]")
    return head + " ".join([text, *extras]).strip()


def print_event(event: dict) -> None:
    print(format_event(event), flush=True)


def telegram_agents(name: str | None) -> list[Agent]:
    if name:
        agent = get_agent(name)
        if agent.channel != "telegram":
            raise ChannelError(f"{name} uses {agent.channel}; this command is for Telegram agents")
        return [agent]
    agents = [a for a in load_agents().values() if a.channel == "telegram"]
    if not agents:
        raise ChannelError(f"no Telegram agent has a handle in {AGENTS_FILE}")
    return agents


def run_telegram(work, agents: list[Agent]) -> None:
    from bench.channels.telegram import TelegramChannel

    async def main():
        async with TelegramChannel(agents, on_event=print_event) as telegram:
            await work(telegram)

    asyncio.run(main())


def cmd_agents(args: argparse.Namespace) -> None:
    agents = load_agents()
    if not agents:
        print(f"No agent has a handle yet. Add them to {AGENTS_FILE}.")
    for agent in agents.values():
        print(f"{agent.name:<10} {agent.channel:<9} {agent.handle}")


def cmd_send(args: argparse.Namespace) -> None:
    agent = get_agent(args.agent)
    if agent.channel == "telegram":
        async def work(telegram):
            await telegram.sync()
            await telegram.send(agent, args.text)
            if args.listen:
                await telegram.listen(args.listen)
        run_telegram(work, [agent])
    elif agent.channel == "whatsapp":
        from bench.channels import whatsapp
        whatsapp.write_agents()
        box = whatsapp.open_inbox()
        if args.listen:
            whatsapp.set_online(True)
        try:
            whatsapp.send(agent, args.text)
            whatsapp.follow(box, {agent.name}, args.listen or 2, print_event)
        finally:
            if args.listen:
                whatsapp.set_online(False)
    elif agent.channel == "imessage":
        from bench.imessage.send import send
        requested_at = send(agent, args.text)
        print(f"sent to {agent.name} ({agent.handle}) at {requested_at.astimezone():%H:%M:%S}")


def recording_message(seconds: float | None) -> str:
    return "Recording. Press Ctrl-C to stop." if seconds is None else f"Recording for {seconds:.0f}s."


def cmd_listen(args: argparse.Namespace) -> None:
    agent = get_agent(args.agent) if args.agent else None
    if agent and agent.channel == "whatsapp":
        from bench.channels import whatsapp
        whatsapp.write_agents()
        box = whatsapp.open_inbox()
        whatsapp.set_online(True)
        print(recording_message(args.seconds), flush=True)
        try:
            whatsapp.follow(box, {agent.name}, args.seconds, print_event)
        finally:
            whatsapp.set_online(False)
        return

    async def work(telegram):
        await telegram.sync()
        print(recording_message(args.seconds), flush=True)
        await telegram.listen(args.seconds)
    run_telegram(work, telegram_agents(args.agent))


def cmd_sync(args: argparse.Namespace) -> None:
    if args.agent and get_agent(args.agent).channel == "whatsapp":
        print("bench-whatsapp records WhatsApp continuously, so there's nothing to sync. See: uv run bench inbox")
        return

    async def work(telegram):
        print(f"{await telegram.sync()} events recorded")
    run_telegram(work, telegram_agents(args.agent))


def cmd_inbox(args: argparse.Namespace) -> None:
    if args.agent:
        channels = [get_agent(args.agent).channel]
    else:
        channels = [c for c in CHANNELS if inbox_path(c).exists()]
    inboxes = [Inbox(inbox_path(c)) for c in channels]
    events = sorted((e for inbox in inboxes for e in inbox.poll()), key=lambda e: e.get("emitted_at", ""))
    for event in [e for e in events if not args.agent or e.get("agent") == args.agent][-args.last:]:
        print_event(event)
    while args.follow:
        time.sleep(1)
        for inbox in inboxes:
            for event in inbox.poll():
                if not args.agent or event.get("agent") == args.agent:
                    print_event(event)


def cmd_telegram_login(args: argparse.Namespace) -> None:
    from bench.channels.telegram import login
    name = asyncio.run(login())
    print(f"\nLogged in to Telegram as {name}. You can close this tab.")


def cmd_telegram_logout(args: argparse.Namespace) -> None:
    from bench.channels.telegram import logout
    if asyncio.run(logout()):
        print("Logged out. This Mac's Telegram session is ended and deleted.")
    else:
        print("This Mac wasn't logged in to Telegram.")


def cmd_whatsapp_install(args: argparse.Namespace) -> None:
    from bench.channels import whatsapp
    whatsapp.install()
    print(f"Installed bench-whatsapp in {whatsapp.BRIDGE_DIR}.\nNext, link it to your WhatsApp: uv run bench whatsapp-link")


def cmd_whatsapp_link(args: argparse.Namespace) -> None:
    from bench.channels import whatsapp
    phone = whatsapp.link()
    print(f"\nLinked to WhatsApp as {phone}, and bench-whatsapp is running in the background. You can close this tab.")


def cmd_whatsapp_status(args: argparse.Namespace) -> None:
    from bench.channels import whatsapp
    print(whatsapp.describe())


def cmd_whatsapp_unlink(args: argparse.Namespace) -> None:
    from bench.channels import whatsapp
    whatsapp.unlink()
    print("Logged bench-whatsapp out of WhatsApp and deleted its session. Recorded messages are kept.")


def cmd_reader_status(args: argparse.Namespace) -> None:
    result = subprocess.run(
        ["launchctl", "print", f"gui/{os.getuid()}/{READER_LABEL}"], capture_output=True, text=True
    )
    if result.returncode != 0:
        print("bench-reader is not installed. Run reader/install.sh.")
    else:
        fields = dict(
            line.strip().split(" = ", 1) for line in result.stdout.splitlines() if " = " in line
        )
        print(f"bench-reader: {fields.get('state', 'unknown')}, pid {fields.get('pid', '-')}")
    if READER_LOG.exists():
        print(f"\nLast lines of {READER_LOG}:")
        print("".join(READER_LOG.read_text().splitlines(keepends=True)[-8:]), end="")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bench", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("agents").set_defaults(func=cmd_agents)
    p = sub.add_parser("send")
    p.add_argument("agent")
    p.add_argument("text")
    p.add_argument("--listen", type=float, metavar="SECONDS", help="keep recording replies this long after sending")
    p.set_defaults(func=cmd_send)
    p = sub.add_parser("listen")
    p.add_argument("agent", nargs="?")
    p.add_argument("--seconds", type=float, help="stop after this long (default: until Ctrl-C)")
    p.set_defaults(func=cmd_listen)
    p = sub.add_parser("sync")
    p.add_argument("agent", nargs="?")
    p.set_defaults(func=cmd_sync)
    p = sub.add_parser("inbox")
    p.add_argument("--agent")
    p.add_argument("--follow", "-f", action="store_true")
    p.add_argument("--last", type=int, default=20, help="how many past events to show (default 20)")
    p.set_defaults(func=cmd_inbox)
    sub.add_parser("telegram-login").set_defaults(func=cmd_telegram_login)
    sub.add_parser("telegram-logout").set_defaults(func=cmd_telegram_logout)
    sub.add_parser("whatsapp-install").set_defaults(func=cmd_whatsapp_install)
    sub.add_parser("whatsapp-link").set_defaults(func=cmd_whatsapp_link)
    sub.add_parser("whatsapp-status").set_defaults(func=cmd_whatsapp_status)
    sub.add_parser("whatsapp-unlink").set_defaults(func=cmd_whatsapp_unlink)
    sub.add_parser("reader-status").set_defaults(func=cmd_reader_status)

    args = parser.parse_args(argv)
    from bench.channels.telegram import TelegramError
    from bench.channels.whatsapp import WhatsAppError
    from bench.imessage.send import SendError
    try:
        args.func(args)
    except (ConfigError, ChannelError, SendError, TelegramError, WhatsAppError) as e:
        print(f"bench: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
