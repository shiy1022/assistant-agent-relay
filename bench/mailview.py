"""The benchmark inbox's mail for one task day, read-only, for the console.

It logs in to Gmail over IMAP with the app password in the Keychain (service
agent-runner.gmail.app_password, account $USER; see tasks/housewarming_plant.py),
opens each folder with EXAMINE, and fetches with BODY.PEEK, so nothing is
marked read, moved or changed. It shows only:

- everything in Sent and Drafts from that day: what you or an agent sent or drafted;
- inbox mail from that day that was planted (by Message-ID, from the task's
  plants.json), comes from a planted sender, or replies in a planted or sent
  message's thread.

The rest of the inbox (newsletters, receipts, alerts) is never fetched past its headers, and never shown.
"""

import email
import email.header
import email.policy
import email.utils
import html
import imaplib
import os
import re
import subprocess
from datetime import UTC, date, datetime, timedelta

from bench.taskview import iso, local_day_bounds

HOST = "imap.gmail.com"
ACCOUNT = "yschore@gmail.com"
KEYCHAIN_SERVICE = "agent-runner.gmail.app_password"
MAX_TEXT = 20_000  # characters of one message's text shown
HEADERS = "(MESSAGE-ID FROM TO CC SUBJECT DATE IN-REPLY-TO REFERENCES)"


class MailError(Exception):
    pass


def app_password() -> str:
    try:
        out = subprocess.run(["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", os.environ.get("USER", ""), "-w"],
                             capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise MailError(f"couldn't read the Gmail app password from the Keychain: {e}") from None
    if out.returncode != 0 or not out.stdout.strip():
        raise MailError(f"no Gmail app password in the Keychain (service {KEYCHAIN_SERVICE}). "
                        "Add it as tasks/housewarming_plant.py says.")
    return out.stdout.strip()


def ids_in(value: str | None) -> list[str]:
    return re.findall(r"<[^<>\s]+>", value or "")


def addresses(value: str | None) -> list[str]:
    return [a.lower() for _, a in email.utils.getaddresses([value or ""]) if a]


def text_of(msg: email.message.EmailMessage) -> str:
    part = msg.get_body(preferencelist=("plain", "html"))
    if part is None:
        return ""
    try:
        body = part.get_content()
    except (LookupError, ValueError):
        body = (part.get_payload(decode=True) or b"").decode("utf-8", "replace")
    if part.get_content_subtype() == "html":
        body = re.sub(r"(?is)<(script|style).*?</\1>", "", body)
        body = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>", "\n", body)
        body = html.unescape(re.sub(r"<[^>]+>", "", body))
    body = re.sub(r"\n{3,}", "\n\n", body.replace("\r\n", "\n")).strip()
    return body[:MAX_TEXT] + ("…" if len(body) > MAX_TEXT else "")


def _folders(conn) -> dict[str, str]:
    """Gmail's Sent and Drafts folders, by their special-use flags, so a localized name still works."""
    typ, rows = conn.list()
    found = {}
    for row in rows or []:
        line = row.decode(errors="replace") if isinstance(row, bytes) else str(row)
        flags = re.search(r"\(([^)]*)\)", line)
        name = line.rsplit(' "/" ', 1)[-1].strip().strip('"')
        for flag, key in (("\\Sent", "sent"), ("\\Drafts", "drafts")):
            if flags and flag in flags.group(1):
                found[key] = name
    return found


def _fetch_day(conn, folder: str, start: datetime, end: datetime) -> list[tuple[bytes, datetime, email.message.Message]]:
    """(uid, arrival time, headers) of each message that arrived in the folder in [start, end)."""
    typ, _ = conn.select(f'"{folder}"', readonly=True)  # EXAMINE
    if typ != "OK":
        raise MailError(f"couldn't open {folder}")
    since = (start.astimezone(UTC) - timedelta(days=1)).strftime("%d-%b-%Y")
    before = (end.astimezone(UTC) + timedelta(days=1)).strftime("%d-%b-%Y")
    typ, data = conn.uid("SEARCH", None, "SINCE", since, "BEFORE", before)
    uids = (data[0] or b"").split() if typ == "OK" and data else []
    out = []
    for i in range(0, len(uids), 200):
        typ, rows = conn.uid("FETCH", b",".join(uids[i:i + 200]), f"(INTERNALDATE BODY.PEEK[HEADER.FIELDS {HEADERS}])")
        for row in rows or []:
            if not isinstance(row, tuple):
                continue
            uid = re.search(rb"UID (\d+)", row[0])
            when = re.search(rb'INTERNALDATE "([^"]+)"', row[0])
            if not uid or not when:
                continue
            arrived = datetime.strptime(when.group(1).decode(), "%d-%b-%Y %H:%M:%S %z")
            if start <= arrived < end:
                out.append((uid.group(1), arrived, email.message_from_bytes(row[1])))
    return out


def _bodies(conn, folder: str, uids: list[bytes]) -> dict[bytes, email.message.EmailMessage]:
    if not uids:
        return {}
    conn.select(f'"{folder}"', readonly=True)
    typ, rows = conn.uid("FETCH", b",".join(uids), "(BODY.PEEK[])")
    out = {}
    for row in rows or []:
        if isinstance(row, tuple) and (uid := re.search(rb"UID (\d+)", row[0])):
            out[uid.group(1)] = email.message_from_bytes(row[1], policy=email.policy.default)
    return out


def read_day(day: date, plants: dict | None = None, *, account: str | None = None, password: str | None = None,
             connect=lambda: imaplib.IMAP4_SSL(HOST, timeout=30)) -> dict:
    """The day's mail that belongs to the task, oldest first."""
    plants = plants or {}
    account = account or plants.get("account") or ACCOUNT
    planted = {m["message_id"]: key for key, m in (plants.get("messages") or {}).items() if m.get("message_id")}
    start, end = local_day_bounds(day)
    conn = connect()
    try:
        try:
            conn.login(account, password or app_password())
        except imaplib.IMAP4.error as e:
            raise MailError(f"Gmail refused the login for {account}: {e}") from None
        folders = _folders(conn)
        boxes = [("inbox", "INBOX"), *((k, folders[k]) for k in ("sent", "drafts") if k in folders)]
        listed = {kind: _fetch_day(conn, name, start, end) for kind, name in boxes}

        mine = {account.lower()}
        sent_ids = {h.get("Message-ID", "").strip() for kind in ("sent", "drafts") for _, _, h in listed.get(kind, [])}
        senders = {a for _, _, h in listed["inbox"] if h.get("Message-ID", "").strip() in planted
                   for a in addresses(h.get("From"))} - mine
        threads = set(planted) | sent_ids
        keep: dict[str, list] = {"inbox": [], "sent": [], "drafts": []}
        # Inbox mail is kept if it's planted, from a planted sender, or in a planted or sent thread; others never show.
        changed = True
        inbox = list(listed["inbox"])
        while changed:  # a reply to a reply is in the thread too
            changed = False
            for item in list(inbox):
                h = item[2]
                mid = h.get("Message-ID", "").strip()
                refs = set(ids_in(h.get("In-Reply-To"))) | set(ids_in(h.get("References")))
                if mid in planted or set(addresses(h.get("From"))) & senders or refs & threads:
                    keep["inbox"].append(item)
                    threads.add(mid)
                    inbox.remove(item)
                    changed = True
        keep["sent"], keep["drafts"] = listed.get("sent", []), listed.get("drafts", [])

        messages = []
        for kind, name in boxes:
            bodies = _bodies(conn, name, [uid for uid, _, _ in keep[kind]])
            for uid, arrived, h in keep[kind]:
                full = bodies.get(uid)
                mid = h.get("Message-ID", "").strip()
                messages.append({
                    "folder": kind,
                    "kind": "planted" if mid in planted else {"inbox": "received", "sent": "sent", "drafts": "draft"}[kind],
                    "plant": planted.get(mid),
                    "message_id": mid,
                    "from": str(h.get("From") or ""), "to": str(h.get("To") or ""), "cc": str(h.get("Cc") or ""),
                    "subject": str(email.header.make_header(email.header.decode_header(h.get("Subject") or ""))),
                    "date": iso(arrived),
                    "in_reply_to": ids_in(h.get("In-Reply-To"))[:1],
                    "text": text_of(full) if full is not None else "",
                })
        found = {m["message_id"] for m in messages}
        missing = sorted(key for mid, key in planted.items() if mid not in found
                         and (at := plants["messages"][key].get("at")) and start <= datetime.fromisoformat(at) < end)
        return {"account": account, "date": day.isoformat(), "read_at": iso(datetime.now(UTC)),
                "messages": sorted(messages, key=lambda m: m["date"]), "missing_plants": missing}
    finally:
        try:
            conn.logout()
        except Exception:
            pass

