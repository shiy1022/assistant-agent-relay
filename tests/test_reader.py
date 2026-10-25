import json
import subprocess
from datetime import datetime

import pytest

from tests.chatdb import apple_ns
from tests.conftest import FIXTURES, ReaderEnv, build_reader

INSTINCT = "+15550100001"
BOBA = "boba@example.com"
BODIES = json.loads((FIXTURES / "attributed_bodies.json").read_text())


def test_first_run_skips_existing_history(tmp_path, reader_bin):
    env = ReaderEnv(tmp_path, reader_bin)
    chat = env.db.chat(INSTINCT)
    env.db.message(chat, "from before the reader was installed")
    assert env.run() == []
    env.db.message(chat, "after")
    assert [e["text"] for e in env.run()] == ["after"]


def test_only_agent_chats_are_read(reader):
    instinct = reader.db.chat(INSTINCT)
    boba = reader.db.chat("Boba@Example.com")  # Messages keeps the case it was first given
    stranger = reader.db.chat("+15559999999")
    group = reader.db.chat("chat123456789", group=True)
    reader.db.message(stranger, "private")
    reader.db.message(group, "group chat that an agent is in")
    sent = reader.db.message(instinct, "Plan a trip to Tokyo", from_me=True)
    reader.db.message(instinct, "Sure, when?")
    reader.db.message(boba, "hi")

    events = reader.run()

    assert [(e["agent"], e["is_from_me"], e["text"]) for e in events] == [
        ("instinct", True, "Plan a trip to Tokyo"),
        ("instinct", False, "Sure, when?"),
        ("boba", False, "hi"),
    ]
    first = events[0]
    assert first["kind"] == "new"
    assert first["rowid"] == sent
    assert first["handle"] == INSTINCT
    assert abs(datetime.fromisoformat(first["date"]).timestamp() - datetime.now().timestamp()) < 60
    assert "date_delivered" not in first  # 0 in chat.db means not delivered yet
    assert events[2]["handle"] == BOBA


@pytest.mark.parametrize("decoder", [None, "scanner", "unarchiver"])
@pytest.mark.parametrize("fixture", BODIES, ids=["short", "long"])
def test_text_from_attributed_body(reader, decoder, fixture):
    chat = reader.db.chat(INSTINCT)
    reader.db.message(chat, None, body=bytes.fromhex(fixture["hex"]))
    [event] = reader.run(decoder=decoder)
    assert event["text"] == fixture["text"]
    assert event["attributed_body_b64"]


def test_changes_are_written_once_as_updates(reader):
    chat = reader.db.chat(INSTINCT)
    rowid = reader.db.message(chat, "Book the 11:25 flight", from_me=True)
    [new] = reader.run()
    assert new["kind"] == "new"

    reader.db.update(rowid, date_delivered=apple_ns())
    [update] = reader.run()
    assert update["kind"] == "update"
    assert update["guid"] == new["guid"]
    assert update["date_delivered"]
    assert reader.run() == []

    reader.db.update(rowid, text="Book the 2:10 flight", date_edited=apple_ns())
    [edit] = reader.run()
    assert edit["text"] == "Book the 2:10 flight"
    assert edit["date_edited"]


def test_tapbacks_and_link_previews(reader):
    chat = reader.db.chat(INSTINCT)
    reader.db.message(chat, "Loved “Sure, when?”", associated_message_type=2000,
                      associated_message_guid="p:0/ABC")
    reader.db.message(chat, "https://example.com/itinerary", balloon_bundle_id="com.apple.messages.URLBalloonProvider")
    tapback, link = reader.run()
    assert (tapback["tapback"], tapback["tapback_target"]) == (2000, "p:0/ABC")
    assert "tapback" not in link
    assert link["balloon"] == "com.apple.messages.URLBalloonProvider"


def test_attachments_are_copied_from_the_messages_folder_only(reader, tmp_path):
    chat = reader.db.chat(INSTINCT)
    rowid = reader.db.message(chat, "￼")
    photo = reader.attachments / "ab" / "12" / "photo.jpg"
    photo.parent.mkdir(parents=True)
    photo.write_bytes(b"jpg")
    secret = tmp_path / "secret.txt"
    secret.write_text("not an attachment")
    reader.db.attachment(rowid, str(photo), "photo.jpg")
    reader.db.attachment(rowid, str(secret), "secret.txt", "text/plain")
    reader.db.attachment(rowid, str(reader.attachments / "ab" / "evil.txt"), "../../evil.txt", "text/plain")
    (reader.attachments / "ab" / "evil.txt").write_text("x")

    [event] = reader.run()

    photo_out, secret_out, evil_out = event["attachments"]
    assert (reader.out / photo_out["path"]).read_bytes() == b"jpg"
    assert photo_out["name"] == "photo.jpg"
    assert "path" not in secret_out
    assert not any(p.name == "secret.txt" for p in reader.out.rglob("*"))
    evil_path = (reader.out / evil_out["path"]).resolve()
    assert evil_path.is_relative_to((reader.out / "attachments").resolve())


def test_attachment_that_downloads_later_is_sent_as_an_update(reader):
    chat = reader.db.chat(INSTINCT)
    rowid = reader.db.message(chat, "￼")
    pdf = reader.attachments / "cd" / "itinerary.pdf"
    reader.db.attachment(rowid, str(pdf), "itinerary.pdf", "application/pdf")

    [new] = reader.run()
    assert "path" not in new["attachments"][0]

    pdf.parent.mkdir(parents=True)
    pdf.write_bytes(b"pdf")
    [update] = reader.run()
    assert (reader.out / update["attachments"][0]["path"]).read_bytes() == b"pdf"


def test_production_build_cannot_be_pointed_at_another_database(tmp_path):
    binary = build_reader(tmp_path / "bench-reader", test=False)
    result = subprocess.run(
        [str(binary), "--once", "--db", str(tmp_path / "other.db"), "--out", str(tmp_path / "out")],
        capture_output=True, text=True,
    )
    assert result.returncode == 64
    assert "unknown argument --db" in result.stderr
