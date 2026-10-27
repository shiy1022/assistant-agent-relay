"""bench: run Assistant Bench tasks against personal agents.

    bench agents                          list agents that have a handle, and their channels
    bench send AGENT TEXT [--listen S]    send one message (then keep recording for S seconds)
    bench send AGENT --file PATH          send a file's contents exactly, e.g. a task prompt
    bench run AGENT --file PATH --task T  send a task prompt, record until the reply is finished, write runs/<run>/
    bench run-rebuild [RUN_DIR ...]       write runs' transcripts again from what's recorded now (default: every run)
    bench run-save [RUN_DIR ...]          save the pages agents linked to as their answers into runs' deliverables/
    bench snapshot [AGENT] [--hours H]    save what every page agents linked to shows, into their runs (the console does it by itself)
    bench listen [AGENT] [--seconds S]    catch up, then record live until Ctrl-C (or S seconds)
    bench sync [AGENT]                    record whatever arrived or changed since last time
    bench inbox [--agent A] [--follow]    show what's been recorded
    bench export                          write data/conversations/<agent>.jsonl: each agent's messages, cleaned up
    bench reset-log                       list every context reset, and where it took effect
    bench reset-log AGENT --how TEXT      write down a reset you did (--method, --after MSG or --at TIME, --result)
    bench dashboard [--out PATH] [--open] write a web page of every recorded conversation
    bench console [--open] [--dry-run]    the dashboard, live in your browser, with a box to send messages from
    bench telegram-login                  link this Mac to your Telegram account by QR code
    bench telegram-logout                 end that session and delete it
    bench whatsapp-install                build bench-whatsapp and install it in the background
    bench whatsapp-link                   link it to your WhatsApp by QR code, then start it
    bench whatsapp-find NAME              find an agent's WhatsApp address by the name it replies under
    bench whatsapp-status                 is it running, linked and recording?
    bench whatsapp-unlink                 log it out of WhatsApp and delete its session
    bench slack-login                     sign the dedicated Chrome in to the Slack workspace (for send_via: web)
    bench slack-status                    check the Slack token, workspace and agents without sending
    bench hark-login                      sign the dedicated Chrome in to Hark, and pin the account
    bench hark-status                     check Hark's sign-in, message box and token meter without sending
    bench hark-import RUN_DIR             record a Hark run made before the channel existed
    bench reader-status                   (iMessage, parked) is bench-reader running?
"""

import argparse
import asyncio
import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from bench.config import AGENTS_FILE, CHANNELS, Agent, ConfigError, get_agent, load_agents
from bench.inbox import Inbox, ThreadNumbers, inbox_path

READER_LABEL = "local.personal-agent-benchmark.bench-reader"
READER_LOG = Path.home() / "Library" / "Logs" / "bench-reader.log"


class ChannelError(Exception):
    pass


def local_time(iso: str | None) -> str:
    return datetime.fromisoformat(iso).astimezone().strftime("%m-%d %H:%M:%S") if iso else "--"


def format_event(event: dict, thread: int | None = None) -> str:
    """One event as a line. `thread` is the number of the Slack thread it's in, if any."""
    kind = event["kind"]
    if kind == "typing":
        return f"{local_time(event['emitted_at'])}  {event['agent']:<9} … {event['action'].replace('_', ' ')}"
    arrow = "→" if event["is_from_me"] else "←"
    head = f"{local_time(event.get('date'))}  {event['agent']:<9} {arrow} " + (f"[thread {thread}] " if thread else "")
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
            # Telegram counts reactions; WhatsApp says who reacted.
            changes.append("reactions " + " ".join(
                f"{r['emoji']}×{r['count']}" if "count" in r else f"{r['emoji']} by {r.get('by', '?')}"
                for r in event["reactions"]))
        text = (event.get("text") or "").replace("\n", " ⏎ ")
        return head + f"(updated{': ' + ', '.join(changes) if changes else ''}) {text}".rstrip()
    text = (event.get("text") or "").replace("\n", " ⏎ ")
    extras = [f"[{a['name']}]" for a in event.get("attachments", [])]
    if event.get("buttons"):
        extras.append("[buttons: " + " | ".join(b["text"] for row in event["buttons"] for b in row) + "]")
    if event.get("tapback"):
        extras.append(f"[tapback {event['tapback']}]")
    return head + " ".join([text, *extras]).strip()


THREADS = ThreadNumbers()  # Slack threads, numbered the same way by every command


def print_event(event: dict) -> None:
    if event.get("channel") == "slack" and not THREADS.numbers:
        THREADS.add(Inbox(inbox_path("slack")).poll())  # so send and listen number threads as bench inbox does
    print(format_event(event, THREADS.of(event)), flush=True)


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


def run_telegram(work, agents: list[Agent], on_event=print_event) -> None:
    from bench.channels.telegram import TelegramChannel

    async def main():
        async with TelegramChannel(agents, on_event=on_event) as telegram:
            await work(telegram)

    asyncio.run(main())


def slack_channel(agent: Agent, on_event=print_event):
    from bench.channels.slack import SlackChannel
    return SlackChannel([agent], on_event=on_event)


def hark_channel(agent: Agent, on_event=print_event):
    from bench.channels.hark import HarkChannel
    return HarkChannel([agent], on_event=on_event)


def cmd_agents(args: argparse.Namespace) -> None:
    agents = load_agents()
    if not agents:
        print(f"No agent has a handle yet. Add them to {AGENTS_FILE}.")
    for agent in agents.values():
        print(f"{agent.name:<10} {agent.channel:<9} {agent.handle}")


class ListenFor:
    """Keeps recording for a fixed time after sending, as bench send --listen does."""

    def __init__(self, seconds: float | None):
        self.left = seconds or 0
        self.listening = bool(seconds)

    def next(self) -> float:
        seconds, self.left = self.left, 0
        return seconds


def converse(agent: Agent, text: str, plan, on_event=print_event) -> None:
    """Sends `text` to the agent, then keeps recording for as long as `plan` says:
    `plan.next()` is how many seconds to record next, 0 to stop (ListenFor, or
    runs.Watch for bench run). `plan.listening` is whether it records at all."""
    if agent.channel == "telegram":
        async def work(telegram):
            await telegram.sync()
            await telegram.send(agent, text)
            while seconds := plan.next():
                await telegram.listen(seconds)
        run_telegram(work, [agent], on_event)
    elif agent.channel == "whatsapp":
        from bench.channels import whatsapp
        whatsapp.write_agents()
        box = whatsapp.open_inbox()
        if plan.listening:
            whatsapp.set_online(True)
        try:
            whatsapp.send(agent, text)
            seconds = plan.next()
            whatsapp.follow(box, {agent.name}, seconds or 2, on_event)
            while seconds and (seconds := plan.next()):
                whatsapp.follow(box, {agent.name}, seconds, on_event)
        finally:
            if plan.listening:
                whatsapp.set_online(False)
    elif agent.channel == "slack":
        with slack_channel(agent, on_event) as slack:
            slack.sync()
            slack.send(agent, text)
            while seconds := plan.next():
                slack.listen(seconds)
    elif agent.channel == "hark":
        with hark_channel(agent, on_event) as hark:
            hark.sync()
            hark.send(agent, text)
            while seconds := plan.next():
                hark.listen(seconds)
    elif agent.channel == "imessage":
        from bench.imessage.send import send
        requested_at = send(agent, text)
        print(f"sent to {agent.name} ({agent.handle}) at {requested_at.astimezone():%H:%M:%S}")


def cmd_send(args: argparse.Namespace) -> None:
    agent = get_agent(args.agent)
    if args.file:
        args.text = Path(args.file).read_text()  # exactly as written, with no shell quoting in between
    if not args.text:
        raise ChannelError("give the message as TEXT or with --file")
    converse(agent, args.text, ListenFor(args.listen))


def cmd_run(args: argparse.Namespace) -> None:
    from bench import runs
    agent = get_agent(args.agent)
    if agent.channel == "imessage":
        raise ChannelError("bench run doesn't support iMessage, which is parked")
    prompt = Path(args.file).read_text()  # exactly as written, with no shell quoting in between
    if not prompt.strip():
        raise ChannelError(f"{args.file} is empty")
    run_dir = runs.start(agent, prompt, task=args.task, step=args.step, cell=args.cell)
    watch = runs.Watch(agent, quiet=args.quiet, max_wait=args.max_wait)
    print(f"Run {run_dir.name}: sending to {agent.name}, then recording until it's quiet for {watch.quiet:.0f}s "
          f"(at most {watch.max_wait:.0f}s). Ctrl-C stops early and still writes the transcript.", flush=True)

    def on_event(event: dict) -> None:
        print_event(event)
        watch.see(event)

    try:
        converse(agent, prompt, watch, on_event)
        reason = watch.finished() or "stopped recording"
    except KeyboardInterrupt:
        reason = "stopped with Ctrl-C"
    except BaseException as e:
        runs.end(run_dir, f"failed: {e}")
        raise
    info = runs.finish(run_dir, reason)
    t = info["timing"]
    print(f"\n{reason}. {t['agent_messages']} messages from {agent.name}"
          + (f", first after {t['seconds_to_first_reply']:.0f}s" if t["first_reply"] else "")
          + f".\nWrote {display(run_dir)}")
    save_deliverables(run_dir)


def save_deliverables(run_dir: Path, again: bool = False) -> bool:
    """Saves the pages the run's agent linked to as its answer, and says what it saved.
    Returns whether every one was saved."""
    from bench import deliverables
    saved, unsaved = deliverables.save(run_dir, again=again)
    for d in saved:
        print(f"Saved {d['path']} from {d['source']}")
    for d in unsaved:
        print(f"bench: couldn't save {d['source']}: {d['error']}\n"
              f"  Try again with: uv run bench run-save {display(run_dir)}", file=sys.stderr)
    return not unsaved


def cmd_run_rebuild(args: argparse.Namespace) -> None:
    from bench import conversation, runs
    dirs = [Path(d).expanduser().resolve() for d in args.run_dir] or sorted(p.parent for p in runs.RUNS_DIR.glob("*/run.json"))
    everything = conversation.load()
    for run_dir in dirs:
        info = runs.rebuild(run_dir, everything)
        print(f"{run_dir.name}: {len(info['messages'])} messages, {len(info['deliverables'])} deliverables")


def cmd_run_save(args: argparse.Namespace) -> None:
    from bench import runs
    dirs = [Path(d).expanduser().resolve() for d in args.run_dir] or sorted(p.parent for p in runs.RUNS_DIR.glob("*/run.json"))
    if not all([save_deliverables(run_dir, args.again) for run_dir in dirs]):
        from bench.deliverables import DeliverableError
        raise DeliverableError("some pages weren't saved; each run lists them in run.json under unsaved_deliverables")


def cmd_export(args: argparse.Namespace) -> None:
    from bench import conversation
    for path in conversation.export():
        print(f"Wrote {display(path)}")


def cmd_reset_log(args: argparse.Namespace) -> None:
    from bench import conversation, resets
    if args.remove:
        gone = resets.remove(args.remove)
        print(f"Removed {gone['id']}: {gone['how']}")
        return
    everything = conversation.load()
    if not args.agent:
        lines = sorted((m for ms in everything.values() for m in ms if m["role"] == "reset"), key=conversation.order_key)
        if not lines:
            print("No resets written down yet. Write one down with: uv run bench reset-log AGENT --how \"what you did\"")
        for m in lines:
            r = m["reset"]
            print(f"{local_time(m['sent_at'])}  {m['agent']:<9} ↺ {r['method']}{' (by this time)' if r['approximate'] else ''}: {m['text']}"
                  + (f"\n{'':26}result: {r['result']}" if r["result"] else "") + f"\n{'':26}{m['id']}")
        return
    agent = get_agent(args.agent)
    if not args.how:
        raise ChannelError("say what was done, with --how")
    mine = [m for m in everything.get(agent.name, []) if m["role"] != "reset"]

    def message(ref: str) -> dict:
        """A message of this agent's, by its id in data/conversations or just its last part."""
        found = next((m for m in mine if ref in (m["id"], m["id"].rsplit(":", 1)[-1])), None)
        if found is None:
            raise ChannelError(f"no message {ref!r} of {agent.name}'s is recorded. Their ids are in data/conversations/{agent.name}.jsonl")
        return found

    messages = [message(ref)["id"] for ref in args.messages or []]
    at = None
    if args.after:
        boundary = message(args.after)
        at = datetime.fromisoformat(boundary["sent_at"])
        messages.append(boundary["id"])
    elif args.at:
        at = datetime.fromisoformat(args.at)
        at = at if at.tzinfo else at.astimezone()  # a time with no zone is this Mac's local time
    r = resets.add(agent, how=args.how, method=args.method, at=at, messages=messages, result=args.result,
                   approximate=args.approximate)
    print(f"Recorded {r['id']}: {'by ' if r['approximate'] else ''}{local_time(r['at'])}, {r['how']}")


def display(path: Path) -> str:
    try:
        return str(path.relative_to(Path.cwd()))
    except ValueError:
        return str(path)


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
    if agent and agent.channel == "slack":
        with slack_channel(agent) as slack:
            slack.sync()
            print(recording_message(args.seconds), flush=True)
            slack.listen(args.seconds)
        return
    if agent and agent.channel == "hark":
        with hark_channel(agent) as hark:
            hark.sync()
            print(recording_message(args.seconds), flush=True)
            hark.listen(args.seconds)
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
    if args.agent and get_agent(args.agent).channel == "slack":
        with slack_channel(get_agent(args.agent)) as slack:
            print(f"{slack.sync()} events recorded")
        return
    if args.agent and get_agent(args.agent).channel == "hark":
        with hark_channel(get_agent(args.agent)) as hark:
            print(f"{hark.sync()} events recorded")
        return

    async def work(telegram):
        print(f"{await telegram.sync()} events recorded")
    run_telegram(work, telegram_agents(args.agent))


def event_order(event: dict) -> tuple[datetime, datetime]:
    """By when the message was sent, then when it was recorded: a sync can record
    old messages after newer ones, and Hark's imported runs come before their sync."""
    recorded = datetime.fromisoformat(event["emitted_at"]) if event.get("emitted_at") else datetime.min.replace(tzinfo=UTC)
    return (datetime.fromisoformat(event["date"]) if event.get("date") else recorded), recorded


def cmd_inbox(args: argparse.Namespace) -> None:
    if args.agent:
        channels = [get_agent(args.agent).channel]
    else:
        channels = [c for c in CHANNELS if inbox_path(c).exists()]
    inboxes = [Inbox(inbox_path(c)) for c in channels]
    events = sorted((e for inbox in inboxes for e in inbox.poll()), key=event_order)
    THREADS.add(events)  # numbered from the whole inbox, not just the lines shown
    for event in [e for e in events if not args.agent or e.get("agent") == args.agent][-args.last:]:
        print_event(event)
    while args.follow:
        time.sleep(1)
        for inbox in inboxes:
            for event in inbox.poll():
                if not args.agent or event.get("agent") == args.agent:
                    print_event(event)


def cmd_dashboard(args: argparse.Namespace) -> None:
    from bench import dashboard
    path = dashboard.write(Path(args.out).expanduser().resolve() if args.out else dashboard.OUT)
    print(f"Wrote {path}")
    if args.open:
        subprocess.run(["open", str(path)])


def cmd_console(args: argparse.Namespace) -> None:
    from bench import console
    console.serve(args.port, open_browser=args.open, dry_run=args.dry_run)


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


def cmd_whatsapp_find(args: argparse.Namespace) -> None:
    from bench.channels import whatsapp
    print(f"Looking for a WhatsApp chat named “{args.name}” for {args.seconds:.0f}s.\n"
          f"On your iPhone, send {args.name} a message in WhatsApp now and wait for its reply.\n"
          "(Only a matching sender name is kept; no message content is recorded.)", flush=True)
    found = whatsapp.find(args.name, args.seconds)
    if not found:
        print(f"\nNo chat named “{args.name}” replied in time.")
        return
    for match in found:
        address = match.get("phone") or match.get("lid") or match["chat"]
        names = " / ".join(filter(None, [match.get("verified_name"), match.get("push_name")]))
        print(f"\nFound {names}: {address}  (chat {match['chat']})")


def cmd_whatsapp_status(args: argparse.Namespace) -> None:
    from bench.channels import whatsapp
    print(whatsapp.describe())


def cmd_whatsapp_unlink(args: argparse.Namespace) -> None:
    from bench.channels import whatsapp
    whatsapp.unlink()
    print("Logged bench-whatsapp out of WhatsApp and deleted its session. Recorded messages are kept.")


def cmd_slack_login(args: argparse.Namespace) -> None:
    from bench.channels.slack import sign_in
    print(sign_in(list(load_agents().values())))


def cmd_slack_status(args: argparse.Namespace) -> None:
    from bench.channels.slack import status
    print(status(list(load_agents().values())))


def cmd_snapshot(args: argparse.Namespace) -> None:
    from bench import snapshots
    if args.mhtml:
        print(f"Added {snapshots.add_mhtml(on_event=print)} .mhtml files.")
        return
    done = snapshots.save_new(agents=[get_agent(args.agent).name] if args.agent else None, hours=args.hours,
                              retry=args.retry, again=tuple(args.again or ()),
                              on_event=lambda e: print(snapshots.describe(e), flush=True))
    if not done:
        print(f"No new links from agents in the last {args.hours:g} hours.")


def cmd_hark_login(args: argparse.Namespace) -> None:
    from bench.channels.hark import sign_in
    print(sign_in())


def cmd_hark_status(args: argparse.Namespace) -> None:
    from bench.channels.hark import status
    print(status())


def cmd_hark_import(args: argparse.Namespace) -> None:
    agent = next((a for a in load_agents().values() if a.channel == "hark"), None)
    if agent is None:
        raise ChannelError(f"no Hark agent has a handle in {AGENTS_FILE}")
    with hark_channel(agent) as hark:
        print(f"{hark.import_run(Path(args.run_dir).expanduser().resolve())} events recorded")


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


RECORDING = {"send", "run", "listen", "sync", "hark-import", "reset-log"}  # commands after which data/conversations is written again


def export_quietly() -> None:
    from bench import conversation
    try:
        conversation.export()
    except (OSError, ValueError) as e:
        print(f"bench: couldn't write data/conversations: {e}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bench", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("agents").set_defaults(func=cmd_agents)
    p = sub.add_parser("send")
    p.add_argument("agent")
    p.add_argument("text", nargs="?")
    p.add_argument("--file", help="send this file's contents instead of TEXT")
    p.add_argument("--listen", type=float, metavar="SECONDS", help="keep recording replies this long after sending")
    p.set_defaults(func=cmd_send)
    p = sub.add_parser("run")
    p.add_argument("agent")
    p.add_argument("--file", required=True, help="the prompt, sent exactly as written")
    p.add_argument("--task", required=True, help="the task's name, as in the task sheet")
    p.add_argument("--step", help="the step within the task, like \"DAY 1 CHECKPOINT\"")
    p.add_argument("--cell", help="the task sheet cell the prompt came from, like D2")
    p.add_argument("--quiet", type=float, metavar="SECONDS",
                   help="the reply is finished after this long with nothing new (default: the agent's quiet_period_seconds)")
    p.add_argument("--max-wait", type=float, metavar="SECONDS",
                   help="stop recording after this long whatever happens (default: the agent's max_wait_seconds)")
    p.set_defaults(func=cmd_run)
    p = sub.add_parser("run-rebuild")
    p.add_argument("run_dir", nargs="*", help="runs/… folders (default: every run)")
    p.set_defaults(func=cmd_run_rebuild)
    p = sub.add_parser("run-save")
    p.add_argument("run_dir", nargs="*", help="runs/… folders (default: every run)")
    p.add_argument("--again", action="store_true", help="save pages already in deliverables/ again, as they are now")
    p.set_defaults(func=cmd_run_save)
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
    sub.add_parser("export").set_defaults(func=cmd_export)
    p = sub.add_parser("reset-log", description="List context resets, or write one down. See bench/resets.py.")
    p.add_argument("agent", nargs="?", help="the agent that was reset (leave out to list every reset)")
    p.add_argument("--how", help="what was done, like \"Asked it to forget everything; it said done\"")
    p.add_argument("--method", choices=("asked", "command", "by hand"), default="asked",
                   help="asked it to forget (default), sent a command like /new, or did it by hand outside the chat")
    when = p.add_mutually_exclusive_group()
    when.add_argument("--after", metavar="MESSAGE", help="it took effect right after this message (its id, or the id's last part)")
    when.add_argument("--at", metavar="TIME", help="it took effect at this time, like 2026-10-25T14:54 (this Mac's time), default now")
    p.add_argument("--messages", nargs="+", metavar="MESSAGE", help="the messages that did it, like the request and the agent's answer")
    p.add_argument("--result", help="what's known about whether it worked")
    p.add_argument("--approximate", action="store_true", help="the time isn't known exactly; give the latest it can have been")
    p.add_argument("--remove", metavar="ID", help="take a reset written down by mistake back out")
    p.set_defaults(func=cmd_reset_log)
    p = sub.add_parser("dashboard")
    p.add_argument("--out", help="where to write it (default data/dashboard.html)")
    p.add_argument("--open", action="store_true", help="open it in your browser afterwards")
    p.set_defaults(func=cmd_dashboard)
    p = sub.add_parser("console")
    p.add_argument("--port", type=int, default=8787, help="the port on 127.0.0.1 to serve it on (default 8787)")
    p.add_argument("--open", action="store_true", help="open it in your browser")
    p.add_argument("--dry-run", action="store_true", help="show what would be sent, and send nothing")
    p.set_defaults(func=cmd_console)
    sub.add_parser("telegram-login").set_defaults(func=cmd_telegram_login)
    sub.add_parser("telegram-logout").set_defaults(func=cmd_telegram_logout)
    sub.add_parser("whatsapp-install").set_defaults(func=cmd_whatsapp_install)
    sub.add_parser("whatsapp-link").set_defaults(func=cmd_whatsapp_link)
    p = sub.add_parser("whatsapp-find")
    p.add_argument("name", help="the name the agent sends under, like Instinct")
    p.add_argument("--seconds", type=float, default=300)
    p.set_defaults(func=cmd_whatsapp_find)
    sub.add_parser("whatsapp-status").set_defaults(func=cmd_whatsapp_status)
    sub.add_parser("whatsapp-unlink").set_defaults(func=cmd_whatsapp_unlink)
    sub.add_parser("slack-login").set_defaults(func=cmd_slack_login)
    sub.add_parser("slack-status").set_defaults(func=cmd_slack_status)
    p = sub.add_parser("snapshot")
    p.add_argument("agent", nargs="?")
    p.add_argument("--hours", type=float, default=72, help="how far back to look for links (default 72)")
    p.add_argument("--retry", action="store_true", help="try again the pages that couldn't be saved")
    p.add_argument("--mhtml", action="store_true", help="add the viewable .mhtml to pages saved before it existed")
    p.add_argument("--again", action="append", metavar="LINK", help="save this link's page again, even though it's saved")
    p.set_defaults(func=cmd_snapshot)
    sub.add_parser("hark-login").set_defaults(func=cmd_hark_login)
    sub.add_parser("hark-status").set_defaults(func=cmd_hark_status)
    p = sub.add_parser("hark-import")
    p.add_argument("run_dir", help="a runs/… folder with run.json and events.jsonl")
    p.set_defaults(func=cmd_hark_import)
    sub.add_parser("reader-status").set_defaults(func=cmd_reader_status)

    args = parser.parse_args(argv)
    from bench.channels.hark import HarkError
    from bench.channels.slack import SlackError
    from bench.channels.telegram import TelegramError
    from bench.channels.whatsapp import WhatsAppError
    from bench.console import ConsoleError
    from bench.deliverables import DeliverableError
    from bench.imessage.send import SendError
    from bench.resets import ResetError
    from bench.runs import RunError
    try:
        args.func(args)
    except (ConfigError, ChannelError, ConsoleError, DeliverableError, HarkError, ResetError, RunError, SendError,
            SlackError, TelegramError, WhatsAppError) as e:
        print(f"bench: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    finally:
        if args.command in RECORDING:
            export_quietly()
    return 0


if __name__ == "__main__":
    sys.exit(main())
