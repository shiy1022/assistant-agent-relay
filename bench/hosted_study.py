"""Participant-only web server for the study. Agent consoles run separately."""

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import UTC, datetime
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, urlopen
from uuid import uuid4
from zipfile import ZipFile, ZipInfo

from bench import study, study_devices, taskview

ACCESS = "study_access"
REFRESH = "study_refresh"
TEMPLATES = Path(__file__).parent
CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
       "media-src 'self' blob:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")


class RequestError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class Supabase:
    def __init__(self, url: str, key: str):
        endpoint = urlsplit(url)
        if (endpoint.scheme != "https" or not endpoint.hostname or not endpoint.hostname.endswith(".supabase.co")
                or endpoint.netloc != endpoint.hostname or endpoint.path not in ("", "/")
                or endpoint.query or endpoint.fragment or not key.startswith("sb_publishable_")):
            raise ValueError("Set SUPABASE_URL and SUPABASE_PUBLISHABLE_KEY for your hosted project.")
        self.url = url.rstrip("/")
        self.key = key

    def request(self, path, *, token=None, body=None, raw=None, mime="application/json", download=False):
        headers = {"apikey": self.key}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if body is not None or raw is not None:
            headers["Content-Type"] = mime
        payload = json.dumps(body, allow_nan=False).encode() if body is not None else raw
        try:
            with urlopen(Request(self.url + path, data=payload, headers=headers), timeout=25) as response:
                data = response.read()
                return (data, response.headers.get("Content-Type")) if download else (json.loads(data) if data else None)
        except HTTPError as error:
            if error.code == 429:
                raise RequestError(429, "Too many requests. Wait and try again.") from None
            if path.startswith("/auth/v1/token"):
                if error.code in (400, 401, 403, 422):
                    raise RequestError(401, "Sign-in failed. Please check your email and password.") from None
            if error.code == 401:
                raise RequestError(401, "Your session expired. Sign in again.") from None
            if error.code == 403:
                raise RequestError(403, "This account cannot access that study data.") from None
            if error.code == 404 and download:
                raise RequestError(404, "Recording not found.") from None
            raise RequestError(502, "The study database could not complete the request.") from None
        except (URLError, TimeoutError):
            raise RequestError(503, "The study database is temporarily unavailable. Try again.") from None

    def member(self, token):
        rows = self.request("/rest/v1/study_members?select=user_id,pid,role", token=token)
        if not rows:
            raise RequestError(403, "This account has no study invitation. Contact the research team.")
        return rows[0]

    def rows(self, table, token, **filters):
        offset = 0
        while True:
            query = urlencode(dict(filters, limit=100, offset=offset))
            rows = self.request(f"/rest/v1/{table}?{query}", token=token)
            yield from rows
            if len(rows) < 100:
                break
            offset += len(rows)

    def save(self, token, pid, path, content):
        self.request("/rest/v1/rpc/study_save_result", token=token,
                     body={"participant": pid, "file_path": path, "payload": content})


class ScopedResults:
    """Separate task results without changing participant identities or older records."""
    def __init__(self, db, scope):
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", scope):
            raise ValueError("Choose a valid STUDY_RESULT_SCOPE.")
        self.db, self.prefix = db, f"tasks/{scope}/"

    def __getattr__(self, name):
        return getattr(self.db, name)

    def save(self, token, pid, path, content):
        return self.db.save(token, pid, self.prefix + path, content)

    def rows(self, table, token, **filters):
        if table not in {"study_results", "study_result_history"}:
            yield from self.db.rows(table, token, **filters)
            return
        match = filters.get("path", "")
        if match.startswith("in.("):
            filters["path"] = "in.(" + ",".join(self.prefix + p for p in match[4:-1].split(",")) + ")"
        elif match.startswith("eq."):
            filters["path"] = "eq." + self.prefix + match[3:]
        else:
            filters["path"] = "like." + self.prefix + "*"
        for row in self.db.rows(table, token, **filters):
            row = dict(row)
            if "path" in row:
                row["path"] = row["path"].removeprefix(self.prefix)
            yield row


class StudyServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, origin, address, db, tasks_dir, task, agents, devices=None):
        super().__init__(address, StudyHandler)
        self.origin = origin
        self.host = urlsplit(origin).netloc
        self.db = db
        spec_file = os.environ.get("STUDY_SPEC_FILE")
        self.spec = json.loads(Path(spec_file).read_text()) if spec_file else study.spec(task, tasks_dir)
        if self.spec.get("task") != task:
            raise ValueError("STUDY_TASK must match the private study specification.")
        self.agents = agents
        self.devices = devices
        self.local_connector = (sys.platform == "darwin" and self.server_address[0] == "127.0.0.1"
                                and urlsplit(origin).hostname in ("127.0.0.1", "localhost"))
        self.channels = {a["name"]: a["channel"] for a in agents if a["channel"] in study_devices.CHANNELS}


class StudyHandler(BaseHTTPRequestHandler):
    server: StudyServer
    timeout = 40

    def log_message(self, format, *args):
        pass

    def respond(self, status, body, mime, *, cookies=(), location=None, download=None):
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Permissions-Policy", "microphone=(self)")
        if download:
            self.send_header("Content-Disposition", f'attachment; filename="{download}"')
        for cookie in cookies:
            self.send_header("Set-Cookie", cookie)
        if location:
            self.send_header("Location", location)
        self.end_headers()
        self.wfile.write(body)

    def json(self, status, data, **headers):
        self.respond(status, json.dumps(data, ensure_ascii=False).encode(), "application/json; charset=utf-8", **headers)

    def cookie(self, name):
        secure = "; Secure" if self.server.origin.startswith("https://") else ""
        return f"{name}; Path=/; HttpOnly; SameSite=Strict{secure}"

    def cookie_value(self, name):
        try:
            cookies = SimpleCookie(self.headers.get("Cookie", ""))
        except CookieError:
            return ""
        return cookies[name].value if name in cookies else ""

    def auth(self):
        token = self.cookie_value(ACCESS)
        if not token:
            raise RequestError(401, "Sign in to continue.")
        return token, self.server.db.member(token)

    def session(self, data):
        access = data["access_token"]
        member = self.server.db.member(access)
        self.json(200, {"member": member}, cookies=(
            self.cookie(f"{ACCESS}={access}; Max-Age={int(data['expires_in'])}"),
            self.cookie(f"{REFRESH}={data['refresh_token']}; Max-Age=604800"),
        ))

    def do_GET(self):
        self.handle_request(False)

    def do_POST(self):
        self.handle_request(True)

    def handle_request(self, post):
        try:
            if self.headers.get("Host") != self.server.host:
                raise RequestError(403, "Open the study at its configured web address.")
            target = urlsplit(self.path)
            if not post:
                return self.get_route(target.path)
            connector = target.path.startswith("/api/connector/")
            if not connector and self.headers.get("Origin") != self.server.origin:
                raise RequestError(403, "Open the study at its configured web address.")
            if self.headers.get_content_type() != "application/json":
                raise RequestError(415, "Send a JSON request.")
            limit = 30_000_000 if target.path == "/api/study/audio" else 1_000_000
            size = int(self.headers.get("Content-Length", "0"))
            if size < 0 or size > limit:
                raise RequestError(413, "This submission is too large.")
            body = json.loads(self.rfile.read(size) or b"{}")
            if not isinstance(body, dict):
                raise ValueError
            if connector:
                return self.connector_route(target.path, body)
            self.post_route(target.path, body)
        except (RequestError, study_devices.DeviceError) as error:
            self.json(error.status, {"error": str(error)})
        except study.StudyError as error:
            self.json(400, {"error": str(error)})
        except (ValueError, UnicodeError, RecursionError):
            self.json(400, {"error": "Invalid study request."})

    def connector_route(self, path, body):
        devices = self.server.devices
        if not devices:
            raise RequestError(404, "The Study Connector is unavailable.")
        action = path.removeprefix("/api/connector/")
        if action == "pair":
            return self.json(200, devices.pair(body.get("code"), body.get("agents"), self.server.channels, self.server.spec["task"]))
        if action in ("poll", "report"):
            credential = self.headers.get("Authorization", "").removeprefix("Bearer ")
            return self.json(200, devices.exchange(credential, action, body, self.server.channels))
        raise RequestError(404, "Page not found.")

    def page(self, name):
        page = (TEMPLATES / name).read_text()
        if name == "study.html":
            page = page.replace("__CONSOLE_TOKEN__", "null").replace("__HOSTED_STUDY__", "true")
        self.respond(200, page.encode(), "text/html; charset=utf-8")

    def get_route(self, path):
        db = self.server.db
        if path == "/":
            return self.respond(303, b"", "text/plain", location="/study")
        if path in ("/study-auth.js", "/study-connect.js"):
            name = {"/study-auth.js": "study_auth.js", "/study-connect.js": "study_connect.js"}[path]
            return self.respond(200, (TEMPLATES / name).read_bytes(),
                                "text/javascript; charset=utf-8")
        if path in ("/study-muse.shortcut", "/study-setup.shortcut"):
            name = "Connect Study - Muse.shortcut"
            return self.respond(200, (TEMPLATES / name).read_bytes(),
                                "application/octet-stream", download="Study Connect.shortcut")
        if path == "/study-connector.zip":
            script = TEMPLATES / "Set Up Study Connector.command"
            archive = BytesIO()
            entry = ZipInfo(script.name)
            entry.create_system = 3
            entry.external_attr = 0o100755 << 16
            with ZipFile(archive, "w") as package:
                package.writestr(entry, script.read_bytes())
            return self.respond(200, archive.getvalue(), "application/zip")
        if path in ("/study", "/research"):
            return self.page("study.html" if path == "/study" else "study_research.html")
        token, member = self.auth()
        if path == "/api/study/session":
            return self.json(200, {"member": member})
        if path == "/api/study/spec" and member["role"] == "participant":
            return self.json(200, self.server.spec)
        if path == "/api/study/agents" and member["role"] == "participant":
            return self.json(200, {"agents": self.server.agents,
                                   "now": datetime.now(UTC).isoformat(),
                                   "connector": bool(self.server.devices),
                                   "local_connector": self.server.local_connector})
        if path == "/api/study/connection" and member["role"] == "participant" and self.server.devices:
            return self.json(200, self.server.devices.status(member["user_id"]))
        if path == "/api/study/load" and member["role"] == "participant":
            pid = member["pid"]
            rows = db.rows("study_results", token, pid=f"eq.{pid}", select="path,content",
                           path="in.(progress.json,day1/submission.json,day2/submission.json)", order="path")
            return self.json(200, {"pid": pid, "files": {r["path"]: r["content"] for r in rows}})
        if path.startswith("/api/research/"):
            if member["role"] != "researcher":
                raise RequestError(403, "Research team access is required.")
            return self.research(token, path.removeprefix("/api/research/"))
        raise RequestError(404, "Page not found.")

    def results(self, token, pid):
        files = {}
        for row in self.server.db.rows("study_results", token, pid=f"eq.{pid}", select="path,content", order="path"):
            content = row["content"]
            if row["path"] == "scenario/changes.jsonl":
                content = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in content)
            files[row["path"]] = content
        return {"pid": pid, "files": files}

    def research(self, token, path):
        db = self.server.db
        if path == "results":
            people = {}
            rows = db.rows("study_results", token, select="pid,path,stage:content->>stage,task:content->>task,updated_at",
                           path="in.(day1/submission.json,day2/submission.json,progress.json)", order="pid,path")
            for row in rows:
                entry = people.setdefault(row["pid"], {"pid": row["pid"], "submitted": [], "updated": ""})
                entry["updated"] = max(entry["updated"], row["updated_at"])
                if row["path"] == "progress.json":
                    entry["stage"] = row["stage"]
                    entry["task"] = row["task"]
                else:
                    entry["submitted"].append(int(row["path"][3]))
            return self.json(200, {"participants": list(people.values())})
        pid, _, item = path.partition("/")
        study.participant_dir(pid)
        if not item:
            return self.json(200, self.results(token, pid))
        if item == "history":
            rows = db.rows("study_result_history", token, pid=f"eq.{pid}", order="id",
                           select="id,path,at,before_value,after_value")
            return self.json(200, {"history": list(rows)})
        if item.startswith("audio/"):
            object_name = quote(f"{pid}/{item.removeprefix('audio/')}", safe="/")
            name = item.removeprefix("audio/")
            if not study.AUDIO_OBJECT.fullmatch(name):
                raise RequestError(404, "Recording not found.")
            data, mime = db.request(f"/storage/v1/object/authenticated/study-audio/{object_name}",
                                    token=token, download=True)
            return self.respond(200, data, mime if mime in study.AUDIO_TYPES else "application/octet-stream")
        raise RequestError(404, "Page not found.")

    def post_route(self, path, body):
        db = self.server.db
        if path == "/api/study/login":
            email, password = body.get("email"), body.get("password")
            if not isinstance(email, str) or not isinstance(password, str) or not email.strip() or not password:
                raise RequestError(400, "Enter your email address and password.")
            return self.session(db.request("/auth/v1/token?grant_type=password",
                                           body={"email": email.strip(), "password": password}))
        if path == "/api/study/refresh":
            refresh = self.cookie_value(REFRESH)
            if not refresh:
                raise RequestError(401, "Sign in to continue.")
            return self.session(db.request("/auth/v1/token?grant_type=refresh_token", body={"refresh_token": refresh}))
        if path == "/api/study/logout":
            return self.json(200, {}, cookies=(self.cookie(f"{ACCESS}=; Max-Age=0"),
                                               self.cookie(f"{REFRESH}=; Max-Age=0")))
        token, member = self.auth()
        if member["role"] != "participant":
            raise RequestError(403, "A participant account is required to save a result.")
        pid = member["pid"]
        if body.get("pid", pid) != pid:
            raise RequestError(403, "This account is assigned to another participant ID.")
        if (isinstance(db, ScopedResults) and path in {"/api/study/save", "/api/study/audio", "/api/study/submit"}
                and body.get("task") != self.server.spec["task"]):
            raise RequestError(409, "The study task changed. Refresh the page before saving.")
        if self.server.devices and path.startswith("/api/study/connection/"):
            owner = member["user_id"]
            devices = self.server.devices
            action = path.removeprefix("/api/study/connection/")
            if action == "code":
                agent = body.get("agent")
                if agent is not None and (not isinstance(agent, str) or agent not in self.server.channels):
                    raise RequestError(400, "Choose an available agent before pairing.")
                return self.json(200, devices.code(owner, agent))
            if action == "launch" and self.server.local_connector:
                code = body.get("code")
                link = "study-connector://open"
                if code is not None:
                    devices.check_code(owner, code)
                    if not re.fullmatch(r"[A-Za-z0-9_-]{24}", code):
                        raise RequestError(400, "Generate a pairing code in Study first.")
                    link = f"study-connector://pair/{code}"
                app = Path.home() / "Applications" / "Study Connector.app"
                try:
                    subprocess.run(["/usr/bin/open", "-a", str(app), link], check=True, capture_output=True, timeout=8)
                except (OSError, subprocess.SubprocessError):
                    raise RequestError(503, "Could not open Study Connector. Check that it is installed on this Mac.") from None
                return self.json(200, {})
            if action == "disconnect":
                devices.disconnect(owner, clear_tests=body.get("clear_tests") is True)
                return self.json(200, {})
            if action == "job":
                return self.json(200, devices.queue(owner, self.server.channels, self.server.spec["task"], body))
            raise RequestError(404, "Page not found.")
        if path == "/api/study/save":
            file_path, content = study.result_payload(body)
            if isinstance(content, list):
                content = [{"at": datetime.now(UTC).isoformat()} | record for record in content]
            db.save(token, pid, file_path, content)
            return self.json(200, {"saved": {"path": file_path}})
        if path == "/api/study/audio":
            name, mime, raw = study.audio_payload(body)
            file_path = f"audio/{name}-{uuid4().hex}{study.AUDIO_TYPES[mime]}"
            object_name = quote(f"{pid}/{file_path.removeprefix('audio/')}", safe="/")
            db.request(f"/storage/v1/object/study-audio/{object_name}", token=token, raw=raw, mime=mime)
            db.save(token, pid, file_path, {"mime": mime, "bytes": len(raw)})
            return self.json(200, {"saved": {"path": file_path}})
        if path == "/api/study/submit":
            day = body.get("day")
            if type(day) is not int or day not in range(1, self.server.spec.get("study_days", 2) + 1):
                raise RequestError(400, "Choose study day 1 or 2.")
            file_path = f"day{day}/submission.json"
            rows = db.rows("study_results", token, pid=f"eq.{pid}", select="path,content",
                           path=f"in.(progress.json,session.json,{file_path})", order="path")
            files = {row["path"]: row["content"] for row in rows}
            if file_path in files:
                return self.json(200, {"submitted": files[file_path]})
            progress = files.get("progress.json", {})
            if progress.get("stage") != "done" or progress.get("day") != day or "session.json" not in files:
                raise RequestError(409, "Finish saving the study day before submitting.")
            db.save(token, pid, file_path, {"day": day, "submitted_at": datetime.now(UTC).isoformat()})
            result = next(db.rows("study_results", token, pid=f"eq.{pid}", select="content", path=f"eq.{file_path}"))
            return self.json(200, {"submitted": result["content"]})
        raise RequestError(404, "Page not found.")


def main():
    parser = argparse.ArgumentParser(description="Run the hosted study site without the local agent console.")
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8080")))
    parser.add_argument("--origin", default=os.environ.get("STUDY_ORIGIN"))
    parser.add_argument("--task", default=os.environ.get("STUDY_TASK", "Restaurant"))
    parser.add_argument("--tasks-dir", type=Path, default=Path(os.environ.get("STUDY_TASKS_DIR", taskview.TASKS_DIR)))
    parser.add_argument("--agents", default=os.environ.get("STUDY_AGENTS", "instinct:imessage"))
    parser.add_argument("--connector-db", type=Path, default=Path(os.environ.get("STUDY_CONNECTOR_DB", "data/study-connector.sqlite3")))
    args = parser.parse_args()
    origin = args.origin or f"http://127.0.0.1:{args.port}"
    address = urlsplit(origin)
    if (origin != f"{address.scheme}://{address.netloc}" or not address.hostname
            or (address.scheme != "https"
                and (args.bind != "127.0.0.1" or address.scheme != "http" or address.hostname not in ("127.0.0.1", "localhost")))):
        parser.error("Set STUDY_ORIGIN to the site's HTTPS origin, without a path.")
    agents = []
    for value in args.agents.split(","):
        name, _, channel = value.strip().partition(":")
        if not name or not re.fullmatch(study.AGENT, name):
            parser.error("Set STUDY_AGENTS to comma-separated names, optionally followed by :channel.")
        agents.append({"name": name, "channel": channel})
    try:
        db = Supabase(os.environ.get("SUPABASE_URL", ""), os.environ.get("SUPABASE_PUBLISHABLE_KEY", ""))
        if os.environ.get("STUDY_SPEC_FILE") and not os.environ.get("STUDY_RESULT_SCOPE"):
            raise ValueError("Set STUDY_RESULT_SCOPE when loading a private study file to preserve earlier results.")
        if os.environ.get("STUDY_RESULT_SCOPE"):
            db = ScopedResults(db, os.environ["STUDY_RESULT_SCOPE"])
        server = StudyServer(origin, (args.bind, args.port), db, args.tasks_dir, args.task, agents, study_devices.Devices(args.connector_db))
    except (ValueError, study.StudyError) as error:
        parser.error(str(error))
    print(f"Study website: {origin}/study", flush=True)
    with server:
        server.serve_forever()


if __name__ == "__main__":
    main()
