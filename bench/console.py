"""The console: the dashboard, live, with a box to send messages to the agents.

    bench console [--port PORT] [--open] [--dry-run]

A small web server on this Mac, reachable only from it (127.0.0.1). The page
shows every recorded conversation, as `bench dashboard` does, and updates by
itself as messages are recorded. A message typed into it is sent by running
the same command you would type: `bench send AGENT --file … --listen S`, or
`bench run` for a task step. So it gets the same checks and locks, and is
recorded the same way. Each job's prompt and output are kept in
data/console/<job>/.

One message can go to several agents. Each channel runs one bench command at a
time (Telegram's session, the WhatsApp bridge's presence, the Slack and Hark
locks), so agents on different channels get it at about the same time, and
agents on the same channel one after the other. When a message is queued for
a channel whose last message has gone out and is only being listened to, that
listening is stopped so the new message goes out now. A task run is never cut
short: the new message waits for it.

Only the page it serves can use it. The address it prints carries a token,
every request has to show it, and requests addressed to any other host name
are refused, so another website open in your browser can't read or send.
"""

import json
import os
import re
import secrets
import shlex
import signal
import subprocess
import sys
import threading
import time
import urllib.parse
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from bench import conversation, dashboard, resets
from bench.config import CHANNELS, PRIVATE_DIR, ROOT, Agent, load_agents
from bench.inbox import inbox_path

PORT = 8787
JOBS_DIR = ROOT / "data" / "console"  # data/ is never committed
TOKEN_FILE = PRIVATE_DIR / "console" / "token"
TEMPLATE = Path(__file__).with_name("console.html")
SENDABLE = ("telegram", "whatsapp", "slack", "hark")  # iMessage is parked
MAX_TEXT = 40_000          # characters in one message
MAX_LISTEN = 3600          # seconds a message may keep recording after it's sent
MAX_BODY = 1_000_000       # bytes in one request
POLL_SECONDS = 1.5         # how often it looks for newly recorded messages
SENT_SKEW = timedelta(seconds=5)  # a sent message can be stamped a little before its job started
STOP_GRACE = 30            # seconds running jobs get to write their records when the console stops
LOG_LINES = 200            # output lines kept in memory per job (all of it is in output.log)
COOKIE = "agent_console_token"

# Stands in for bench in --dry-run: says what it would have run, and sends nothing.
DRY_RUN = r"""import shlex, sys, time
args = sys.argv[1:]
print("dry run, nothing sent: bench " + shlex.join(args), flush=True)
if args[0] != "sync":
    print("dry run: sent", flush=True)
try:
    time.sleep(float(args[args.index("--listen") + 1]) if "--listen" in args else 3 if args[0] != "run" else 20)
except KeyboardInterrupt:
    sys.exit(130)
"""
DRY_SENT = "dry run: sent"


class ConsoleError(Exception):
    pass


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse(iso: str) -> datetime:
    return datetime.fromisoformat(iso)


def same_text(a: str, b: str) -> bool:
    """Whether a recorded message is the one sent. Slack and Telegram store
    formatting their own way, so only letters and digits are compared."""
    norm = lambda s: re.sub(r"\W+", "", s or "").lower()
    return bool(norm(a)) and norm(a) == norm(b)


@dataclass
class Job:
    """One bench command the console runs for one agent."""

    id: str
    kind: str                     # "send", "run" or "sync"
    agent: str
    channel: str
    text: str = ""
    listen: float = 0             # send: seconds to keep recording after sending
    task: str | None = None       # run: as in the task sheet
    step: str | None = None
    cell: str | None = None
    created_at: str = field(default_factory=now_iso)
    state: str = "queued"         # queued, running, done, failed, stopped or cancelled
    started_at: str | None = None
    ended_at: str | None = None
    sent_at: str | None = None    # when the message showed up in the agent's recorded conversation
    sent_id: str | int | None = None  # that message's id in the snapshot
    exit_code: int | None = None
    error: str | None = None
    stopping: bool = False
    preempted: bool = False       # its listening was stopped for the next message
    proc: subprocess.Popen | None = field(default=None, repr=False)
    log: deque = field(default_factory=lambda: deque(maxlen=LOG_LINES), repr=False)

    def public(self) -> dict:
        keys = ("id", "kind", "agent", "channel", "text", "listen", "task", "step", "cell", "created_at", "state",
                "started_at", "ended_at", "sent_at", "sent_id", "exit_code", "error", "stopping", "preempted")
        return {k: getattr(self, k) for k in keys} | {"log": list(self.log)[-12:]}


class Console:
    """The jobs, one worker per channel, and the latest snapshot of what's recorded."""

    def __init__(self, agents: dict[str, Agent] | None = None, *, jobs_dir: Path = JOBS_DIR,
                 python: str = sys.executable, dry_run: bool = False, inboxes: dict[str, Path] | None = None,
                 runs_dir: Path = dashboard.RUNS_DIR, resets_file: Path | None = None):
        self.all_agents = load_agents() if agents is None else agents
        self.agents = {n: a for n, a in self.all_agents.items() if a.channel in SENDABLE}
        self.jobs_dir = jobs_dir
        self.python = python
        self.dry_run = dry_run
        self.live = inboxes is None  # reading the real inboxes, so it may write data/conversations
        self.inboxes = inboxes or {c: inbox_path(c) for c in CHANNELS}
        self.runs_dir = runs_dir
        self.resets_file = resets_file or resets.RESETS_FILE
        self.lock = threading.Condition()
        self.refreshing = threading.Lock()  # one snapshot at a time
        self.jobs: list[Job] = []
        self.running: dict[str, Job | None] = {c: None for c in SENDABLE}
        self.closing = False
        self.snap: dict | None = None
        self.signature: tuple | None = None
        self.version = 0
        self.watch_error: str | None = None
        self.threads = [threading.Thread(target=self._lane, args=(c,), name=f"lane-{c}", daemon=True) for c in SENDABLE]
        self.threads.append(threading.Thread(target=self._watch, name="watch", daemon=True))

    def start(self) -> None:
        self.refresh()
        for t in self.threads:
            t.start()

    # MARK: What the page asks for

    def submit(self, body: dict) -> list[Job]:
        """Queues the message for each agent asked for. Raises ConsoleError if anything's wrong with it."""
        names = body.get("agents")
        if not isinstance(names, list) or not names or not all(isinstance(n, str) for n in names):
            raise ConsoleError("pick at least one agent")
        names = list(dict.fromkeys(names))
        unknown = [n for n in names if n not in self.agents]
        if unknown:
            raise ConsoleError(f"can't send to {', '.join(unknown)}: not an agent with a handle in config/agents.yaml")
        text = body.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ConsoleError("the message is empty")
        if len(text) > MAX_TEXT:
            raise ConsoleError(f"the message is {len(text)} characters; the most is {MAX_TEXT}")
        mode = body.get("mode", "message")
        if mode == "message":
            try:
                listen = float(body.get("listen") or 0)
            except (TypeError, ValueError):
                raise ConsoleError("listen must be a number of seconds") from None
            if not 0 <= listen <= MAX_LISTEN:
                raise ConsoleError(f"listen must be between 0 and {MAX_LISTEN} seconds")
            fields = {"kind": "send", "listen": listen}
        elif mode == "run":
            task, step, cell = (str(body.get(k) or "").strip() or None for k in ("task", "step", "cell"))
            if not task:
                raise ConsoleError("a task run needs the task's name, as in the task sheet")
            fields = {"kind": "run", "task": task, "step": step, "cell": cell}
        else:
            raise ConsoleError(f"unknown mode {mode!r}")
        with self.lock:
            if self.closing:
                raise ConsoleError("the console is stopping")
            jobs = [self._add(Job(id=self._new_id(n), agent=n, channel=self.agents[n].channel, text=text, **fields))
                    for n in names]
            self._preempt()
            self.lock.notify_all()
        return jobs

    def sync(self, name: str) -> Job:
        """Queues `bench sync AGENT`, which records what arrived while nothing was recording."""
        agent = self.agents.get(name)
        if agent is None:
            raise ConsoleError(f"{name} isn't an agent with a handle in config/agents.yaml")
        if agent.channel == "whatsapp":
            raise ConsoleError("bench-whatsapp records WhatsApp all the time, so there's nothing to sync")
        with self.lock:
            job = self._add(Job(id=self._new_id(name), kind="sync", agent=name, channel=agent.channel))
            self.lock.notify_all()
        return job

    def record_reset(self, body: dict) -> dict:
        """Writes down a reset you did (bench.resets), right after one of the agent's messages, or now."""
        if self.dry_run:
            raise ConsoleError("dry run: nothing is written down")
        agent = self.all_agents.get(str(body.get("agent") or ""))
        if agent is None:
            raise ConsoleError(f"{body.get('agent')} isn't an agent in config/agents.yaml")
        how, result = (body.get(k) if isinstance(body.get(k), str) else "" for k in ("how", "result"))
        if len(how) > MAX_TEXT or len(result) > MAX_TEXT:
            raise ConsoleError("that's too long")
        at, messages = None, []
        if after := body.get("after"):
            with self.lock:
                items = next((a["items"] for a in (self.snap or {}).get("agents", []) if a["name"] == agent.name), [])
            boundary = next((i for i in items if i["type"] == "message" and i.get("guid") == after), None)
            if boundary is None:
                raise ConsoleError("that message isn't among the agent's recorded messages")
            at, messages = parse(boundary["date"]), [after]
        try:
            reset = resets.add(agent, how=how, method=str(body.get("method") or ""), at=at, messages=messages,
                               result=result, path=self.resets_file)
        except resets.ResetError as e:
            raise ConsoleError(str(e)) from None
        if self.live:
            try:
                conversation.export()  # so data/conversations shows it too, as after bench reset-log
            except (OSError, ValueError):
                pass  # the next bench command writes it
        self.refresh()
        return reset

    def stop(self, job_id: str) -> Job:
        """Cancels a queued job. Stops a running one as Ctrl-C would, so it still writes its records;
        asked again while it's stopping, it ends it at once."""
        with self.lock:
            job = next((j for j in self.jobs if j.id == job_id), None)
            if job is None:
                raise ConsoleError(f"no job {job_id}")
            if job.state == "queued":
                job.state, job.ended_at = "cancelled", now_iso()
            elif job.state == "running" and job.proc is None:
                job.stopping = True  # picked, not started yet: it won't start
            elif job.state == "running":
                if not job.stopping:
                    job.stopping = True
                    signal_job(job.proc, signal.SIGINT)
                else:
                    signal_job(job.proc, signal.SIGTERM, group=True)
            else:
                raise ConsoleError(f"that job has already {job.state}")
            self.lock.notify_all()
            return job

    def state(self, since: int | None = None) -> dict:
        with self.lock:
            return {
                "version": self.version,
                "snapshot": None if since == self.version else self.snap,
                "jobs": [j.public() for j in self.jobs[-200:]],
                "agents": [{"name": a.name, "channel": a.channel, "quiet_period_seconds": a.quiet_period_seconds,
                            "max_wait_seconds": a.max_wait_seconds, "can_sync": a.channel != "whatsapp"}
                           for a in self.agents.values()],
                "dry_run": self.dry_run,
                "now": now_iso(),
                "error": self.watch_error,
            }

    def close(self, grace: float = STOP_GRACE) -> list[Job]:
        """Cancels what's queued and stops what's running, giving it `grace` seconds to write its records.
        Returns the jobs that were still running."""
        with self.lock:
            self.closing = True
            for job in self.jobs:
                if job.state == "queued":
                    job.state, job.ended_at = "cancelled", now_iso()
            running = [j for j in self.running.values() if j]
            procs = [j.proc for j in running if j.proc]
            for job in running:
                job.stopping = True
            for proc in procs:
                signal_job(proc, signal.SIGINT)
            self.lock.notify_all()
        deadline = time.monotonic() + grace
        for proc in procs:
            try:
                proc.wait(max(0.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                signal_job(proc, signal.SIGTERM, group=True)
        return running

    # MARK: Jobs

    def _new_id(self, agent: str) -> str:
        return f"{datetime.now():%Y%m%d-%H%M%S}-{agent}-{secrets.token_hex(2)}"

    def _add(self, job: Job) -> Job:
        self.jobs.append(job)
        return job

    def _next(self, channel: str) -> Job | None:
        return next((j for j in self.jobs if j.channel == channel and j.state == "queued"), None)

    def _preempt(self) -> None:
        """Stops listening after a sent message when the next message for that channel is waiting.
        Called with the lock held."""
        for channel, job in self.running.items():
            if (job and job.kind == "send" and job.sent_at and job.proc and not job.stopping
                    and self._next(channel)):
                job.preempted = job.stopping = True
                signal_job(job.proc, signal.SIGINT)

    def _lane(self, channel: str) -> None:
        while True:
            with self.lock:
                while not (job := self._next(channel)):
                    if self.closing:
                        return
                    self.lock.wait()
                job.state, job.started_at = "running", now_iso()
                self.running[channel] = job
            try:
                self._execute(job)
            except Exception as e:  # never let one job stop the lane
                with self.lock:
                    job.state, job.error, job.ended_at = "failed", f"the console couldn't run it: {e}", now_iso()
            with self.lock:
                self.running[channel] = None
                self.lock.notify_all()
            self.refresh()

    def command(self, job: Job, prompt: Path) -> list[str]:
        """What follows `bench` for a job, as you'd type it."""
        if job.kind == "sync":
            return ["sync", job.agent]
        if job.kind == "send":
            return ["send", job.agent, "--file", str(prompt), *(["--listen", f"{job.listen:g}"] if job.listen else [])]
        return ["run", job.agent, "--file", str(prompt), "--task", job.task,
                *(["--step", job.step] if job.step else []), *(["--cell", job.cell] if job.cell else [])]

    def _execute(self, job: Job) -> None:
        folder = self.jobs_dir / job.id
        folder.mkdir(parents=True, exist_ok=True)
        os.chmod(folder, 0o700)  # it holds what you sent
        prompt = folder / "prompt.txt"
        if job.kind != "sync":
            prompt.write_text(job.text)  # sent exactly as typed, with no shell quoting in between
            os.chmod(prompt, 0o600)
        args = self.command(job, prompt)
        with (folder / "output.log").open("w") as log:
            os.chmod(log.name, 0o600)
            log.write("$ bench " + shlex.join(args) + "\n")
            with self.lock:
                if job.stopping:  # stopped between being picked and starting
                    job.state, job.ended_at = "cancelled", now_iso()
                    return
                job.proc = subprocess.Popen(
                    [self.python, *(["-c", DRY_RUN] if self.dry_run else ["-m", "bench.cli"]), *args], cwd=ROOT, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, bufsize=1, start_new_session=True,  # the console, not your terminal's Ctrl-C, stops it
                    env={**os.environ, "PYTHONUNBUFFERED": "1"})
            for line in job.proc.stdout:
                log.write(line)
                log.flush()
                with self.lock:
                    job.log.append(line.rstrip("\n"))
                    if self.dry_run and line.startswith(DRY_SENT) and not job.sent_at:
                        job.sent_at = now_iso()
                        self._preempt()
            code = job.proc.wait()
        with self.lock:
            job.exit_code, job.ended_at, job.proc = code, now_iso(), None
            if job.preempted:
                job.state = "done"
            elif job.stopping:
                job.state = "stopped"
            elif code == 0:
                job.state = "done"
            else:
                job.state = "failed"
                errors = [l for l in job.log if l.startswith("bench: ")]
                job.error = (errors[-1].removeprefix("bench: ") if errors else
                             next((l for l in reversed(job.log) if l.strip()), f"exited with code {code}"))
            (folder / "job.json").write_text(json.dumps(job.public() | {"log": None}, indent=2, ensure_ascii=False) + "\n")

    # MARK: What's recorded

    def _signature(self) -> tuple:
        """Changes whenever an inbox or a run is written."""
        def stat(p: Path):
            try:
                s = p.stat()
                return s.st_size, s.st_mtime_ns
            except OSError:
                return None
        runs = sorted((p.parent.name, stat(p)) for p in self.runs_dir.glob("*/run.json"))
        return tuple((c, stat(p)) for c, p in sorted(self.inboxes.items())), tuple(runs), stat(self.resets_file)

    def refresh(self) -> None:
        """Takes a new snapshot if anything was recorded, then matches sent messages to their jobs."""
        with self.refreshing:
            signature = self._signature()
            if signature != self.signature:
                snap = dashboard.snapshot(self.all_agents, self.inboxes, self.runs_dir, resets_file=self.resets_file)
                for agent in snap["agents"]:
                    for item in agent["items"]:
                        for a in item.get("attachments") or []:
                            if a.get("url", "").startswith("file://"):  # the page is on http://, which can't open file:// links
                                a["url"] = "/attachment?" + urllib.parse.urlencode(
                                    {"path": urllib.parse.unquote(urllib.parse.urlsplit(a["url"]).path)})
                with self.lock:
                    self.snap, self.signature, self.version = snap, signature, self.version + 1
            with self.lock:
                self._match_sent()
                self._preempt()

    def _match_sent(self) -> None:
        """Marks each message job sent once its message is recorded in the agent's conversation."""
        claimed = {j.sent_id for j in self.jobs if j.sent_id}
        conversations = {a["name"]: a["items"] for a in (self.snap or {}).get("agents", [])}
        for job in self.jobs:
            if job.kind == "sync" or job.sent_at or not job.started_at:
                continue
            since = parse(job.started_at) - SENT_SKEW
            for m in conversations.get(job.agent, []):
                if (m["type"] == "message" and m["from_me"] and m.get("id") is not None and m["id"] not in claimed
                        and m.get("recorded") and parse(m["recorded"]) >= since and same_text(m.get("text"), job.text)):
                    job.sent_at, job.sent_id = m["date"], m["id"]
                    claimed.add(m["id"])
                    break

    def _watch(self) -> None:
        while not self.closing:
            try:
                self.refresh()
                self.watch_error = None
            except Exception as e:  # shown on the page; the next look may work
                self.watch_error = f"couldn't read what's recorded: {e}"
            time.sleep(POLL_SECONDS)

    def attachment_roots(self) -> list[Path]:
        return [p.parent.resolve() for p in self.inboxes.values()]


def signal_job(proc: subprocess.Popen, sig: int, *, group: bool = False) -> None:
    """Ctrl-C goes to bench itself, so it can detach and write its records; a forced stop goes to everything it started."""
    try:
        os.killpg(proc.pid, sig) if group else proc.send_signal(sig)
    except (ProcessLookupError, PermissionError):
        pass


def load_token(path: Path = TOKEN_FILE) -> str:
    """The console's token, made once and kept, so its address stays the same between starts."""
    if path.exists() and (token := path.read_text().strip()):
        return token
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    token = secrets.token_urlsafe(24)
    path.write_text(token)
    os.chmod(path, 0o600)
    return token


# MARK: - Web server

class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], console: Console, token: str):
        super().__init__(address, Handler)
        self.console = console
        self.token = token
        port = self.server_address[1]
        self.hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}


class Handler(BaseHTTPRequestHandler):
    server: Server

    def log_message(self, format, *args):  # each job's output is in data/console/<job>/output.log
        pass

    # MARK: Checks

    def _host_ok(self) -> bool:
        """Refuses requests addressed to any other name, so a website can't reach this by pointing its own name at 127.0.0.1."""
        return self.headers.get("Host") in self.server.hosts

    def _cookie_ok(self) -> bool:
        cookie = SimpleCookie(self.headers.get("Cookie") or "")
        return COOKIE in cookie and secrets.compare_digest(cookie[COOKIE].value, self.server.token)

    def _token_ok(self) -> bool:
        return secrets.compare_digest(self.headers.get("X-Console-Token") or "", self.server.token)

    def _origin_ok(self) -> bool:
        origin = self.headers.get("Origin")
        return origin is None or origin in {f"http://{h}" for h in self.server.hosts}

    # MARK: Replies

    def _send(self, status: int, body: bytes, content_type: str, **headers) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")  # it holds your chats
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in headers.items():
            self.send_header(k.replace("_", "-"), v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, data) -> None:
        self._send(status, json.dumps(data, ensure_ascii=False).encode(), "application/json; charset=utf-8")

    def _error(self, status: int, message: str) -> None:
        self._json(status, {"error": message})

    # MARK: Routes

    def do_GET(self):
        url = urllib.parse.urlsplit(self.path)
        query = urllib.parse.parse_qs(url.query)
        if not self._host_ok():
            return self._error(403, "open the console at the address bench console printed")
        if url.path == "/":
            if query.get("t") and secrets.compare_digest(query["t"][0], self.server.token):
                # Kept in a cookie, then dropped from the address, so a reload works and the token isn't left on screen.
                return self._send(303, b"", "text/plain", Location="/",
                                  Set_Cookie=f"{COOKIE}={self.server.token}; Path=/; HttpOnly; SameSite=Strict")
            if not self._cookie_ok():
                return self._send(403, b"Open the console at the address `uv run bench console` printed. It has the token.",
                                  "text/plain; charset=utf-8")
            page = TEMPLATE.read_text().replace("__CONSOLE_TOKEN__", json.dumps(self.server.token))
            return self._send(200, page.encode(), "text/html; charset=utf-8",
                              Content_Security_Policy="default-src 'self'; script-src 'unsafe-inline'; "
                                                      "style-src 'unsafe-inline'; img-src 'self' data:; frame-ancestors 'none'")
        if url.path == "/api/state":
            if not self._token_ok():
                return self._error(403, "wrong or missing token; reload the page")
            since = query.get("since", [None])[0]
            return self._json(200, self.server.console.state(int(since) if since and since.isdigit() else None))
        if url.path == "/attachment":
            if not self._cookie_ok():
                return self._error(403, "reload the console first")
            path = Path(query.get("path", [""])[0]).resolve()
            if not any(path.is_relative_to(root) for root in self.server.console.attachment_roots()) or not path.is_file():
                return self._error(404, "no such attachment")
            return self._send(200, path.read_bytes(), "application/octet-stream",
                              Content_Disposition=f"attachment; filename*=UTF-8''{urllib.parse.quote(path.name)}")
        return self._error(404, "not found")

    def do_POST(self):
        if not (self._host_ok() and self._origin_ok() and self._token_ok()):
            return self._error(403, "wrong or missing token; reload the page")
        if not (self.headers.get("Content-Type") or "").startswith("application/json"):
            return self._error(415, "send JSON")
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            return self._error(413, "that's too long")
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(body, dict):
                raise ValueError
        except ValueError:
            return self._error(400, "send a JSON object")
        console = self.server.console
        try:
            if self.path == "/api/send":
                return self._json(200, {"jobs": [j.public() for j in console.submit(body)]})
            if self.path == "/api/sync":
                return self._json(200, {"jobs": [console.sync(str(body.get("agent") or "")).public()]})
            if self.path == "/api/stop":
                return self._json(200, {"jobs": [console.stop(str(body.get("job") or "")).public()]})
            if self.path == "/api/reset":
                return self._json(200, {"reset": console.record_reset(body)})
        except ConsoleError as e:
            return self._error(400, str(e))
        return self._error(404, "not found")


def serve(port: int = PORT, *, open_browser: bool = False, dry_run: bool = False) -> None:
    """Runs the console until Ctrl-C."""
    console = Console(dry_run=dry_run)
    token = load_token()
    try:
        server = Server(("127.0.0.1", port), console, token)
    except OSError as e:
        raise ConsoleError(f"couldn't listen on port {port} ({e.strerror}). Is a console already running? "
                           "Pick another port with --port.") from None
    console.start()
    url = f"http://127.0.0.1:{server.server_address[1]}/?t={token}"
    print(f"The console is at {url}\n"
          + ("Dry run: messages typed into it aren't sent anywhere.\n" if dry_run else "")
          + "Leave this running while you use it. Ctrl-C stops it.", flush=True)
    if open_browser:
        subprocess.run(["open", url])
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        running = [j for j in console.running.values() if j]
        if running:
            print(f"\nStopping {', '.join(j.agent for j in running)} as Ctrl-C would, so the records get written…", flush=True)
        console.close()
        print("Console stopped.")
