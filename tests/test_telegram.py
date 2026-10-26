import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from telethon.errors import SessionPasswordNeededError
from telethon.tl import types

from bench.channels import telegram as telegram_module
from bench.channels.telegram import TelegramChannel, TelegramError, login
from bench.config import Agent

FO = Agent("wajo", "@fo_test_bot", 90, 900, channel="telegram")
FO_ID, STRANGER_ID = 7001, 9999


def message(id, text="", *, peer=FO_ID, out=False, minutes_ago=1, **fields):
    return types.Message(
        id=id, peer_id=types.PeerUser(peer), message=text, out=out,
        date=datetime.now(UTC) - timedelta(minutes=minutes_ago), **fields,
    )


class FakeClient:
    """The parts of TelegramClient the channel uses, with chats held in memory."""

    def __init__(self):
        self.users = {"fo_test_bot": types.User(id=FO_ID, access_hash=111, username="fo_test_bot", bot=True)}
        self.history = {FO_ID: [], STRANGER_ID: []}
        self.read_outbox_max_id = 0
        self.handlers = []
        self.sent_to = []
        self.session = SimpleNamespace(save_entities=True)

    async def connect(self): pass
    async def disconnect(self): pass
    async def is_user_authorized(self): return True

    async def get_entity(self, handle):
        return self.users[handle.removeprefix("@")]

    async def iter_messages_list(self, peer, limit=None, min_id=0, reverse=False):
        msgs = [m for m in self.history[peer.user_id] if m.id > min_id]
        msgs = sorted(msgs, key=lambda m: m.id, reverse=not reverse)
        return msgs[:limit] if limit else msgs

    def iter_messages(self, peer, limit=None, min_id=0, reverse=False):
        async def gen():
            for m in await self.iter_messages_list(peer, limit, min_id, reverse):
                yield m
        return gen()

    async def get_messages(self, peer, ids):
        return next((m for m in self.history[peer.user_id] if m.id == ids), None)

    async def send_message(self, peer, text, link_preview=True):
        self.sent_to.append(peer.user_id)
        msg = message(max([0, *(m.id for h in self.history.values() for m in h)]) + 1, text,
                      peer=peer.user_id, out=True, minutes_ago=0)
        self.history[peer.user_id].append(msg)
        return msg

    async def download_media(self, msg, file):
        with open(file, "wb") as f:
            f.write(b"OggS voice")
        return file

    def add_event_handler(self, callback, event):
        self.handlers.append((callback, event))

    async def __call__(self, request):
        return SimpleNamespace(dialogs=[SimpleNamespace(read_outbox_max_id=self.read_outbox_max_id)])


@pytest.fixture
def fake():
    return FakeClient()


@pytest.fixture
def dirs(tmp_path):
    return SimpleNamespace(session=tmp_path / "session", inbox=tmp_path / "inbox" / "telegram" / "messages.jsonl")


def run(coro):
    return asyncio.run(coro)


def channel(fake, dirs, agents=(FO,)):
    return TelegramChannel(list(agents), client=fake, session_dir=dirs.session, inbox=dirs.inbox)


def events(dirs):
    return [json.loads(line) for line in dirs.inbox.read_text().splitlines()] if dirs.inbox.exists() else []


def test_first_sync_records_the_agent_chat_and_nothing_else(fake, dirs):
    fake.history[FO_ID] += [message(1, "hi", out=True, minutes_ago=5), message(2, "Hi! I'm Fo.", minutes_ago=4)]
    fake.history[STRANGER_ID] += [message(3, "private", peer=STRANGER_ID)]

    async def go():
        async with channel(fake, dirs) as tg:
            return await tg.sync()

    run(go())
    recorded = events(dirs)
    assert [(e["kind"], e["is_from_me"], e["text"]) for e in recorded] == [
        ("new", True, "hi"), ("new", False, "Hi! I'm Fo."),
    ]
    assert recorded[1]["guid"] == f"telegram:{FO_ID}:2"
    assert recorded[1]["channel"] == "telegram" and recorded[1]["agent"] == "wajo"
    assert recorded[1]["date"].endswith("Z")
    assert "private" not in dirs.inbox.read_text()


def test_make_client_turns_off_the_entity_cache(monkeypatch, tmp_path):
    monkeypatch.setattr(telegram_module, "api_credentials", lambda: (1, "hash"))
    client = telegram_module.make_client(tmp_path)
    assert client.session.save_entities is False
    assert oct(tmp_path.stat().st_mode & 0o777) == "0o700"


def test_another_chat_is_never_recorded_even_if_a_handler_passes_it(fake, dirs):
    async def go():
        async with channel(fake, dirs) as tg:
            await tg._on_message(SimpleNamespace(message=message(5, "from a stranger", peer=STRANGER_ID)))
            return await tg._record(FO, message(6, "also a stranger", peer=STRANGER_ID))

    assert run(go()) is None
    assert events(dirs) == []


def test_pinned_id_is_kept_when_the_username_moves(fake, dirs):
    async def go():
        async with channel(fake, dirs):
            pass
        # Someone else takes the username; the channel keeps talking to the pinned account.
        fake.users["fo_test_bot"] = types.User(id=4242, access_hash=222, username="fo_test_bot", bot=True)
        async with channel(fake, dirs) as tg:
            await tg.send(FO, "hello again")

    run(go())
    assert fake.sent_to == [FO_ID]
    pins = json.loads((dirs.session / "peers.json").read_text())
    assert pins["wajo"]["id"] == FO_ID
    assert oct((dirs.session / "peers.json").stat().st_mode & 0o777) == "0o600"


def test_send_records_the_message_and_refuses_other_agents(fake, dirs):
    other = Agent("muse", "+15550100002", 180, 1800, channel="whatsapp")

    async def go():
        async with channel(fake, dirs) as tg:
            sent = await tg.send(FO, "Plan a trip to Tokyo")
            with pytest.raises(TelegramError):
                await tg.send(other, "hi")
            with pytest.raises(TelegramError):
                await tg.send(FO, "   ")
            # The outgoing-message update that Telegram also sends changes nothing.
            await tg._on_message(SimpleNamespace(message=fake.history[FO_ID][-1]))
            return sent

    sent = run(go())
    assert (sent["kind"], sent["is_from_me"], sent["text"], sent["read"]) == ("new", True, "Plan a trip to Tokyo", False)
    assert len(events(dirs)) == 1


def test_edits_and_reactions_made_while_offline_are_caught_on_sync(fake, dirs):
    fake.history[FO_ID].append(message(1, "Flights from $1,240"))

    async def go():
        async with channel(fake, dirs) as tg:
            await tg.sync()
        fake.history[FO_ID][0] = message(
            1, "Flights from $1,190", edit_date=datetime.now(UTC),
            reactions=types.MessageReactions(results=[types.ReactionCount(reaction=types.ReactionEmoji("👍"), count=1)]),
        )
        async with channel(fake, dirs) as tg:
            first = await tg.sync()
            again = await tg.sync()
        return first, again

    assert run(go()) == (1, 0)
    update = events(dirs)[-1]
    assert (update["kind"], update["text"]) == ("update", "Flights from $1,190")
    assert update["date_edited"] and update["reactions"] == [{"emoji": "👍", "count": 1}]


def test_messages_sent_while_offline_are_recovered(fake, dirs):
    async def go():
        async with channel(fake, dirs) as tg:
            await tg.send(FO, "Find me a hotel")
        fake.history[FO_ID].append(message(2, "Here are three hotels", minutes_ago=0))
        async with channel(fake, dirs) as tg:
            await tg.sync()

    run(go())
    assert [e["text"] for e in events(dirs)] == ["Find me a hotel", "Here are three hotels"]


def test_read_receipts_live_and_on_sync(fake, dirs):
    async def go():
        async with channel(fake, dirs) as tg:
            await tg.send(FO, "first")
            await tg._on_read(SimpleNamespace(chat_id=FO_ID, max_id=1))
            await tg.send(FO, "second")
        fake.read_outbox_max_id = 2
        async with channel(fake, dirs) as tg:
            await tg.sync()

    run(go())
    updates = [e for e in events(dirs) if e["kind"] == "update"]
    assert [(u["text"], u["read"], "date_read" in u) for u in updates] == [("first", True, True), ("second", True, False)]


def test_buttons_voice_memos_and_typing(fake, dirs):
    markup = types.ReplyInlineMarkup(rows=[types.KeyboardInlineButtonRow(buttons=[
        types.KeyboardInlineButton(text="Book it", type=types.InlineButtonTypeCallback(data=b"book")),
        types.KeyboardInlineButton(text="Details", type=types.InlineButtonTypeUrl(url="https://example.com/trip")),
    ])])
    voice = types.MessageMediaDocument(voice=True, document=types.Document(
        id=1, access_hash=1, file_reference=b"", date=datetime.now(UTC), mime_type="audio/ogg", size=10, dc_id=1,
        attributes=[types.DocumentAttributeAudio(duration=3, voice=True)],
    ))
    fake.history[FO_ID] += [message(1, "Shall I book?", reply_markup=markup), message(2, media=voice)]

    async def go():
        async with channel(fake, dirs) as tg:
            await tg.sync()
            await tg._on_typing(types.UpdateUserTyping(user_id=FO_ID, action=types.SendMessageTypingAction()))
            await tg._on_typing(types.UpdateUserTyping(user_id=STRANGER_ID, action=types.SendMessageTypingAction()))
            await tg._on_typing(types.UpdateUserTyping(user_id=FO_ID, action=types.SendMessageRecordAudioAction()))

    run(go())
    buttons_event, voice_event, typing, recording = events(dirs)
    assert buttons_event["buttons"] == [[
        {"text": "Book it", "kind": "callback"},
        {"text": "Details", "kind": "url", "url": "https://example.com/trip"},
    ]]
    [attachment] = voice_event["attachments"]
    assert attachment["mime_type"] == "audio/ogg"
    assert (dirs.inbox.parent / attachment["path"]).read_bytes() == b"OggS voice"
    assert (typing["kind"], typing["action"], recording["action"]) == ("typing", "typing", "record_audio")


def test_rich_messages_are_read_as_text(fake, dirs):
    # Fo greets in Telegram's rich-message format, with no plain text at all.
    greeting = types.RichMessage(blocks=[
        types.PageBlockParagraph(text=types.TextPlain("hey you! good to see you 👋")),
    ], photos=[], documents=[])
    plan = types.RichMessage(blocks=[
        types.PageBlockHeader(text=types.TextBold(types.TextPlain("Tokyo, Nov 14–18"))),
        types.PageBlockList(items=[
            types.PageListItemText(text=types.TextConcat([types.TextPlain("Flight: "), types.TextBold(types.TextPlain("NH7"))])),
            types.PageListItemText(text=types.TextUrl(text=types.TextPlain("Hotel"), url="https://example.com/h", webpage_id=0)),
        ]),
    ], photos=[], documents=[])
    fake.history[FO_ID] += [message(1, rich_message=greeting), message(2, rich_message=plan)]

    async def go():
        async with channel(fake, dirs) as tg:
            await tg.sync()

    run(go())
    first, second = events(dirs)
    assert (first["text"], first["rich"]) == ("hey you! good to see you 👋", True)
    assert second["text"] == "Tokyo, Nov 14–18\n- Flight: NH7\n- Hotel (https://example.com/h)"


def test_pending_link_previews_keep_the_url(fake, dirs):
    pending = types.MessageMediaWebPage(webpage=types.WebPagePending(id=1, date=None, url="https://wajo.ai/join"))
    fake.history[FO_ID].append(message(1, "invite link", media=pending))

    async def go():
        async with channel(fake, dirs) as tg:
            await tg.sync()

    run(go())
    assert events(dirs)[0]["link_preview"] == {"url": "https://wajo.ai/join"}


def test_clearing_the_chat_is_recorded_from_telegrams_notice(fake, dirs):
    # Clearing the chat on your phone leaves a service message, which bench.resets reads as a reset.
    cleared = types.MessageService(id=2, peer_id=types.PeerUser(FO_ID), out=True, date=datetime.now(UTC),
                                   action=types.MessageActionHistoryClear())
    fake.history[FO_ID] += [message(1, "hi", out=True, minutes_ago=5), cleared]

    async def go():
        async with channel(fake, dirs) as tg:
            await tg.sync()

    run(go())
    hi, notice = events(dirs)
    assert "service" not in hi
    assert (notice["service"], notice["is_from_me"], notice.get("text")) == ("history_clear", True, None)


def test_deletions_only_for_messages_in_the_agent_chat(fake, dirs):
    fake.history[FO_ID].append(message(1, "oops"))

    async def go():
        async with channel(fake, dirs) as tg:
            await tg.sync()
            await tg._on_deleted(SimpleNamespace(chat_id=None, deleted_ids=[1, 555]))

    run(go())
    assert [(e["kind"], e["message_id"]) for e in events(dirs)] == [("new", 1), ("deleted", 1)]


def test_one_command_at_a_time(fake, dirs):
    async def go():
        async with channel(fake, dirs):
            with pytest.raises(TelegramError, match="another bench command"):
                async with channel(FakeClient(), dirs):
                    pass

    run(go())


def test_missing_credentials_explain_how_to_add_them(monkeypatch):
    monkeypatch.delenv("TELEGRAM_API_ID", raising=False)
    monkeypatch.delenv("TELEGRAM_API_HASH", raising=False)
    monkeypatch.setattr(telegram_module.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=44, stdout=""))
    with pytest.raises(TelegramError, match="security add-generic-password"):
        telegram_module.api_credentials()


def test_login_by_qr_with_a_cloud_password(tmp_path):
    shown, asked = [], []

    class QR:
        url = "tg://login?token=abc"
        waits = 0

        async def wait(self):
            QR.waits += 1
            if QR.waits == 1:
                raise asyncio.TimeoutError  # the first code expired unscanned
            raise SessionPasswordNeededError(request=None)

        async def recreate(self):
            pass

    class LoginClient(FakeClient):
        signed_in_with = None

        async def is_user_authorized(self): return False
        async def qr_login(self): return QR()
        async def sign_in(self, password): LoginClient.signed_in_with = password
        async def get_me(self): return SimpleNamespace(first_name="Yueheng", last_name=None, username=None, id=1)

    name = run(login(tmp_path, client=LoginClient(), show_qr=shown.append,
                     ask_password=lambda prompt: asked.append(prompt) or "pw"))
    assert name == "Yueheng"
    assert shown == [QR.url, QR.url]
    assert LoginClient.signed_in_with == "pw" and len(asked) == 1
