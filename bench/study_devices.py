"""Pair local study connectors and exchange jobs over the study server."""

import hashlib
import json
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

CODE_TTL = 300
DEVICE_TTL = 30 * 86400
ONLINE_TTL = 15
JOB_TTL = 300
RUN_TTL = 2400
CHANNELS = {"imessage", "whatsapp", "muse", "slack", "wajo", "hark", "telegram"}


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


class DeviceError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


class Devices:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch(mode=0o600, exist_ok=True)
        self.path = path
        with self.transaction() as db:
            db.executescript("""
                create table if not exists codes (
                    code text primary key, owner text not null unique, expires real not null
                );
                create table if not exists devices (
                    id text primary key, owner text not null unique, credential text not null unique,
                    expires real not null, active real not null, agents text not null, verified text not null
                );
                create table if not exists jobs (
                    id text primary key, owner text not null, device text not null, agent text not null,
                    channel text not null, kind text not null, prompt text not null, step text not null,
                    task text not null, created real not null, updated real not null,
                    state text not null, sent_at text, replies text not null, error text
                );
                create index if not exists jobs_owner_created on jobs (owner, created);
                create table if not exists requests (
                    owner text not null, request_id text not null, job text not null,
                    primary key (owner, request_id)
                );
                create index if not exists jobs_device_state on jobs (device, state, created);
            """)

            if 'agent' not in {row[1] for row in db.execute('pragma table_info(codes)')}:
                db.execute('alter table codes add column agent text')

    @contextmanager
    def transaction(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            with db:
                db.execute("begin immediate")
                yield db
        finally:
            db.close()

    def code(self, owner, agent=None):
        code = secrets.token_urlsafe(18)
        now = time.time()
        with self.transaction() as db:
            db.execute("delete from codes where owner = ? or expires <= ?", (owner, now))
            db.execute("insert into codes (code, owner, expires, agent) values (?, ?, ?, ?)",
                       (digest(code), owner, now + CODE_TTL, agent))
        return {"code": code, "expires_in": CODE_TTL}

    def check_code(self, owner, code):
        if not isinstance(code, str) or len(code) != 24:
            raise DeviceError(400, "Generate a pairing code in Study first.")
        with self.transaction() as db:
            valid = db.execute("select 1 from codes where code = ? and owner = ? and expires > ?",
                               (digest(code), owner, time.time())).fetchone()
        if not valid:
            raise DeviceError(403, "Generate a new pairing code in Study.")

    def pair(self, code, agents, configured, task=""):
        if not isinstance(code, str) or len(code) != 24:
            raise DeviceError(400, "Enter the pairing code shown in Study.")
        agents = self.agents(agents, configured)
        now = time.time()
        credential = secrets.token_urlsafe(32)
        with self.transaction() as db:
            pair = db.execute("select owner, agent from codes where code = ? and expires > ?",
                              (digest(code), now)).fetchone()
            if not pair:
                raise DeviceError(403, "This pairing code expired or was used. Generate a new code in Study.")
            owner, target = pair["owner"], pair["agent"]
            if target is not None and target not in configured:
                raise DeviceError(400, "This agent is no longer available. Generate a new pairing code.")
            self.remove(db, owner, now)
            db.execute("delete from codes where owner = ?", (owner,))
            device_id = uuid4().hex
            db.execute("insert into devices values (?, ?, ?, ?, ?, ?, ?)",
                       (device_id, owner, digest(credential), now + DEVICE_TTL, now, json.dumps(agents), "[]"))
            if target is not None:
                # Pairing and setup are one transaction: no browser polling is required.
                db.execute("""insert into jobs values (?, ?, ?, ?, ?, 'setup', '', 'CHANNEL SETUP', ?, ?, ?, 'queued', null, '[]', null)""",
                           (uuid4().hex, owner, device_id, target, configured[target], task, now, now))
        return {"credential": credential, "setup_queued": target is not None}

    @staticmethod
    def agents(value, configured):
        if not isinstance(value, dict):
            raise DeviceError(400, "Invalid connector channels.")
        return {name: channel for name, channel in configured.items() if value.get(name) == channel}

    def remove(self, db, owner, now):
        db.execute("delete from devices where owner = ?", (owner,))
        db.execute("""update jobs set state = 'failed', updated = ?, error = 'Mac disconnected.'
                      where owner = ? and state in ('queued', 'running')""", (now, owner))

    def disconnect(self, owner, *, clear_tests=False):
        with self.transaction() as db:
            self.remove(db, owner, time.time())
            db.execute("delete from codes where owner = ?", (owner,))
            if clear_tests:
                db.execute("delete from jobs where owner = ? and kind in ('setup', 'test')", (owner,))

    @staticmethod
    def expire(db, owner, now):
        db.execute("""update jobs set state = 'failed', updated = ?, error = 'The Mac did not pick up this task.'
                      where owner = ? and state = 'queued' and updated <= ?""", (now, owner, now - JOB_TTL))
        db.execute("""update jobs set state = 'failed', updated = ?,
                          error = 'Connection lost during this task. Check the agent before sending again.'
                      where owner = ? and state = 'running' and (updated <= ? or not exists (
                            select 1 from devices where id = jobs.device and active > ?
                          ))""", (now, owner, now - RUN_TTL, now - 45))

    @staticmethod
    def public(row):
        return {key: row[key] for key in ("id", "agent", "channel", "kind", "prompt", "step", "state", "sent_at", "error")} | {
            "replies": json.loads(row["replies"]),
        }

    def status(self, owner):
        now = time.time()
        with self.transaction() as db:
            self.expire(db, owner, now)
            device = db.execute("select * from devices where owner = ? and expires > ?", (owner, now)).fetchone()
            jobs = db.execute("select * from jobs where owner = ? order by created desc limit 160", (owner,)).fetchall()
        return {
            "device": None if device is None else {
                "online": device["active"] > now - ONLINE_TTL,
                "agents": json.loads(device["agents"]),
                "verified": json.loads(device["verified"]),
            },
            "jobs": [self.public(row) for row in reversed(jobs)],
        }

    def queue(self, owner, configured, task, body):
        kind, agent = body.get("kind"), body.get("agent")
        request_id = body.get("request_id")
        if request_id is not None and (kind != "run" or not isinstance(request_id, str) or not 1 <= len(request_id) <= 200):
            raise DeviceError(400, "Invalid message request ID.")
        if kind not in ("setup", "test", "run") or not isinstance(agent, str) or agent not in configured:
            raise DeviceError(400, "Choose an agent and a valid action.")
        channel = configured[agent]
        if kind == "run":
            prompt, step = body.get("text"), body.get("step")
            if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 40_000:
                raise DeviceError(400, "Enter a message of up to 40,000 characters.")
            if not isinstance(step, str) or not step or len(step) > 100:
                raise DeviceError(400, "Choose a valid study step.")
        elif kind == "test":
            prompt = f"This is a Study connection test. Please reply exactly: pong {secrets.token_hex(3)}"
            step = "CONNECTION TEST"
        else:
            prompt, step = "", "CHANNEL SETUP"
            setup_step = body.get("setup_step")
            if setup_step is not None:
                if channel != "imessage" or setup_step not in ("contact", "signin", "permissions"):
                    raise DeviceError(400, "Choose a valid iMessage setup step.")
                step = "IMESSAGE " + setup_step.upper()
        now = time.time()
        with self.transaction() as db:
            self.expire(db, owner, now)
            if request_id is not None:
                existing = db.execute("""select jobs.* from requests join jobs on jobs.id = requests.job
                                         where requests.owner = ? and requests.request_id = ?""", (owner, request_id)).fetchone()
                if existing:
                    if any(existing[key] != value for key, value in {
                        "agent": agent, "channel": channel, "kind": kind, "task": task, "step": step, "prompt": prompt
                    }.items()):
                        raise DeviceError(409, "This request ID was already used for another message.")
                    return self.public(existing)
            device = db.execute("select * from devices where owner = ? and expires > ?", (owner, now)).fetchone()
            if device is None or device["active"] <= now - ONLINE_TTL:
                raise DeviceError(409, "Open the Study Connector on your Mac.")
            if kind != "setup" and json.loads(device["agents"]).get(agent) != channel:
                raise DeviceError(409, "Set up this channel on your Mac first.")
            if kind == "run" and agent not in json.loads(device["verified"]):
                raise DeviceError(409, "Test this connection before starting.")
            pending = db.execute("""select id from jobs where device = ? and state in ('queued', 'running')
                                     and kind != 'run' """, (device["id"],)).fetchone()
            if pending:
                raise DeviceError(409, "Wait for the current connection step to finish.")
            if kind == "setup" and channel == "imessage":
                verified = [name for name in json.loads(device["verified"]) if name != agent]
                db.execute("update devices set verified = ? where id = ?", (json.dumps(verified), device["id"]))
            job = uuid4().hex
            db.execute("""insert into jobs values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', null, '[]', null)""",
                       (job, owner, device["id"], agent, channel, kind, prompt, step, task, now, now))
            if request_id is not None:
                db.execute("insert into requests values (?, ?, ?)", (owner, request_id, job))
        return {"id": job, "agent": agent, "channel": channel, "kind": kind, "step": step,
                "state": "queued", "sent_at": None, "replies": [], "error": None}

    def exchange(self, credential, action, body, configured):
        if not credential or len(credential) != 43:
            raise DeviceError(401, "Pair this Mac with Study.")
        now = time.time()
        with self.transaction() as db:
            device = db.execute("select * from devices where credential = ? and expires > ?",
                                (digest(credential), now)).fetchone()
            if not device:
                raise DeviceError(401, "This Mac is no longer paired. Pair it again in Study.")
            device_id = device["id"]
            self.expire(db, device["owner"], now)
            if action == "poll":
                agents = self.agents(body.get("agents"), configured)
                old = json.loads(device["agents"])
                verified = [name for name in json.loads(device["verified"]) if old.get(name) == agents.get(name)
                            and name in agents]
                db.execute("update devices set active = ?, agents = ?, verified = ? where id = ?",
                           (now, json.dumps(agents), json.dumps(verified), device_id))
                if body.get("busy") is True:
                    return {"job": None}
                busy_agents = body.get("busy_agents", [])
                if not isinstance(busy_agents, list) or not all(isinstance(a, str) for a in busy_agents):
                    raise DeviceError(400, "Invalid active agent list.")
                candidates = db.execute("""select * from jobs where device = ? and state = 'queued'
                                           order by created""", (device_id,)).fetchall()
                job = next((j for j in candidates if j["agent"] not in busy_agents
                            and (not busy_agents or j["kind"] == "run")), None)
                if job is None:
                    return {"job": None}
                db.execute("update jobs set state = 'running', updated = ? where id = ?", (now, job["id"]))
                return {"job": {key: job[key] for key in ("id", "agent", "channel", "kind", "prompt", "step", "task")}}
            job = db.execute("select * from jobs where id = ? and device = ?",
                             (body.get("id") if isinstance(body.get("id"), str) else "", device_id)).fetchone()
            if not job:
                raise DeviceError(404, "Task not found.")
            if job["state"] != "running":
                return {"accepted": False}
            state, sent_at, error = body.get("state"), body.get("sent_at"), body.get("error")
            replies = body.get("replies")
            if state not in ("running", "done", "failed") or not isinstance(replies, list) or len(replies) > 100:
                raise DeviceError(400, "Invalid task update.")
            if not all(isinstance(r, dict) and isinstance(r.get("text"), str) and len(r["text"]) <= 40_000
                       and isinstance(r.get("at"), str) and len(r["at"]) <= 48
                       and isinstance(r.get("id"), str) and len(r["id"]) <= 160 for r in replies):
                raise DeviceError(400, "Invalid reply.")
            replies = [{key: reply[key] for key in ("id", "at", "text")} for reply in replies]
            sent_at = sent_at if isinstance(sent_at, str) and len(sent_at) <= 48 else None
            error = error[:240] if isinstance(error, str) else None
            db.execute("update devices set active = ? where id = ?", (now, device_id))
            db.execute("update jobs set state = ?, updated = ?, sent_at = ?, replies = ?, error = ? where id = ?",
                       (state, now, sent_at, json.dumps(replies), error, job["id"]))
            expected = job["prompt"].rsplit(": ", 1)[-1]
            verified = set(json.loads(device["verified"]))
            if state == "failed":
                verified.discard(job["agent"])
            elif (job["kind"] == "test" and state == "done" and sent_at
                  and any(r["text"].strip().lower() == expected for r in replies)):
                verified.add(job["agent"])
            db.execute("update devices set verified = ? where id = ?", (json.dumps(sorted(verified)), device_id))
            return {"accepted": True}
