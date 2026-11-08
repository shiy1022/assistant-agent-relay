import time
from datetime import date

import pytest

from bench import mailview


@pytest.fixture(autouse=True)
def pacific(monkeypatch):
    monkeypatch.setenv("TZ", "America/Los_Angeles")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def raw(mid, frm, subject, body, *, to="yschore@gmail.com", refs="", irt=""):
    head = [f"Message-ID: {mid}", f"From: {frm}", f"To: {to}", f"Subject: {subject}", "Date: Sat, 07 Nov 2026 19:00:00 -0800"]
    if irt:
        head.append(f"In-Reply-To: {irt}")
    if refs:
        head.append(f"References: {refs}")
    return ("\r\n".join(head + ["Content-Type: text/plain; charset=utf-8", "", body]) + "\r\n").encode()


class FakeGmail:
    """Answers only what a read-only reader needs; anything else fails the test."""

    def __init__(self, folders):
        self.folders = folders  # name → [(uid, INTERNALDATE, raw)]
        self.selected = None
        self.calls = []

    def login(self, user, password):
        self.calls.append(("login", user))

    def list(self):
        return "OK", [b'(\\HasNoChildren) "/" "INBOX"', b'(\\HasNoChildren \\Sent) "/" "[Gmail]/Sent Mail"',
                      b'(\\HasNoChildren \\Drafts) "/" "[Gmail]/Drafts"', b'(\\All \\HasNoChildren) "/" "[Gmail]/All Mail"']

    def select(self, mailbox, readonly=False):
        assert readonly, "opened a folder for writing"
        self.selected = mailbox.strip('"')
        self.calls.append(("examine", self.selected))
        return "OK", [b"1"]

    def uid(self, command, *args):
        box = self.folders.get(self.selected, [])
        if command == "SEARCH":
            return "OK", [b" ".join(str(u).encode() for u, _, _ in box)]
        assert command == "FETCH", f"sent {command}"
        uids, what = args
        assert "PEEK" in what, "a fetch that would mark mail read"
        wanted = {int(u) for u in uids.split(b",")}
        rows = []
        for u, when, data in box:
            if u in wanted:
                part = data.split(b"\r\n\r\n")[0] + b"\r\n\r\n" if "HEADER" in what else data
                rows += [(f'{u} (UID {u} INTERNALDATE "{when}" BODY[] {{{len(part)}}}'.encode(), part), b")"]
        self.calls.append(("fetch", self.selected, "headers" if "HEADER" in what else "whole", sorted(wanted)))
        return "OK", rows

    def logout(self):
        self.calls.append(("logout",))

    def __getattr__(self, name):  # store, expunge, copy, append, …
        raise AssertionError(f"called {name}")


PLANTS = {"account": "yschore@gmail.com", "messages": {
    "welcome": {"message_id": "<w@b>", "folder": "inbox", "at": "2026-11-07T17:01:50-08:00"},
    "invite": {"message_id": "<inv@me>", "folder": "sent", "at": "2026-11-07T19:42:53-08:00"},
    "rsvp": {"message_id": "<r@a>", "folder": "inbox", "at": "2026-11-07T19:51:11-08:00"},
    "gone": {"message_id": "<gone@x>", "folder": "inbox", "at": "2026-11-07T19:55:00-08:00"},
}}


def gmail():
    return FakeGmail({
        "INBOX": [
            (1, "07-Nov-2026 12:01:01 +0000", raw("<promo@shop>", "Shop <deals@shop.example>", "50% off", "Buy now")),
            (2, "08-Nov-2026 01:01:50 +0000", raw("<w@b>", "Front Desk <desk@bldg.example>", "Welcome home", "Quiet hours 10 PM")),
            (3, "08-Nov-2026 03:51:11 +0000", raw("<r@a>", "Alice <alice@x.example>", "Re: Housewarming", "We're in!", irt="<inv@me>")),
            (4, "08-Nov-2026 04:40:06 +0000", raw("<n@b>", "Front Desk <desk@bldg.example>", "Elevators", "Closed Sat")),
            (5, "08-Nov-2026 04:41:00 +0000", raw("<rr@c>", "Carol <carol@y.example>", "Re: Re: Housewarming", "Me too?", refs="<r@a>")),
            (6, "08-Nov-2026 04:45:00 +0000", raw("<rrr@d>", "Dan <dan@z.example>", "Re: Re: Re: Housewarming", "And me", irt="<rr@c>")),
            (7, "08-Nov-2026 05:00:00 +0000", raw("<alert@g>", "Google <no-reply@accounts.google.com>", "Security alert", "New sign-in")),
            (8, "08-Nov-2026 05:01:00 +0000", raw("<self@me>", "yschore@gmail.com", "note to self", "unrelated")),
            (9, "08-Nov-2026 09:00:00 +0000", raw("<late@b>", "Front Desk <desk@bldg.example>", "Next day", "Tomorrow's mail")),
        ],
        "[Gmail]/Sent Mail": [
            (11, "08-Nov-2026 03:42:53 +0000", raw("<inv@me>", "yschore@gmail.com", "Housewarming!", "Come over", to="Alice <alice@x.example>")),
            (12, "08-Nov-2026 04:51:00 +0000", raw("<sent@agent>", "yschore@gmail.com", "Re: Housewarming", "Sorry, just 10", to="Bob <bob@x.example>")),
            (13, "06-Nov-2026 18:00:00 +0000", raw("<old@me>", "yschore@gmail.com", "Earlier", "Not today")),
        ],
        "[Gmail]/Drafts": [
            (21, "08-Nov-2026 05:20:00 +0000", raw("<d@agent>", "yschore@gmail.com", "Gate list", "Names: …", to="desk@bldg.example")),
        ],
    })


def test_shows_only_the_tasks_mail_of_the_day_and_never_changes_anything():
    fake = gmail()
    out = mailview.read_day(date(2026, 11, 7), PLANTS, password="app-password", connect=lambda: fake)
    got = [(m["folder"], m["kind"], m["plant"], m["subject"]) for m in out["messages"]]
    assert got == [
        ("inbox", "planted", "welcome", "Welcome home"),
        ("sent", "planted", "invite", "Housewarming!"),
        ("inbox", "planted", "rsvp", "Re: Housewarming"),
        ("inbox", "received", None, "Elevators"),                 # from a planted sender
        ("inbox", "received", None, "Re: Re: Housewarming"),      # in a planted thread
        ("inbox", "received", None, "Re: Re: Re: Housewarming"),  # a reply to that reply
        ("sent", "sent", None, "Re: Housewarming"),
        ("drafts", "draft", None, "Gate list"),
    ]
    welcome = out["messages"][0]
    assert welcome["text"] == "Quiet hours 10 PM" and welcome["date"] == "2026-11-08T01:01:50.000Z"
    assert welcome["from"] == "Front Desk <desk@bldg.example>" and welcome["message_id"] == "<w@b>"
    assert out["missing_plants"] == ["gone"] and out["account"] == "yschore@gmail.com"
    # The promo, the alert, the note to yourself and the next day's mail were never fetched past their headers.
    whole = [(c[1], c[3]) for c in fake.calls if c[0] == "fetch" and c[2] == "whole"]
    assert whole == [("INBOX", [2, 3, 4, 5, 6]), ("[Gmail]/Sent Mail", [11, 12]), ("[Gmail]/Drafts", [21])]
    assert ("examine", "INBOX") in fake.calls and fake.calls[-1] == ("logout",)


def test_a_refused_login_says_so():
    fake = gmail()

    def refuse(user, password):
        raise mailview.imaplib.IMAP4.error("[AUTHENTICATIONFAILED] Invalid credentials")

    fake.login = refuse
    with pytest.raises(mailview.MailError, match="Gmail refused the login for yschore@gmail.com"):
        mailview.read_day(date(2026, 11, 7), PLANTS, password="wrong", connect=lambda: fake)
    assert fake.calls == [("logout",)]


def test_html_mail_is_shown_as_text():
    msg = mailview.email.message_from_bytes(
        b"Content-Type: text/html; charset=utf-8\r\n\r\n<style>p{}</style><p>Hi&nbsp;Bob,<br>see you</p><p>Yueheng</p>",
        policy=mailview.email.policy.default)
    assert mailview.text_of(msg) == "Hi\xa0Bob,\nsee you\nYueheng"
