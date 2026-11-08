# Wajo (Fo) on wajo.ai

Back to [all connections](README.md). Since 2026-11-07 this is how the runner reaches Fo. Telegram is still an option: [wajo-telegram.md](wajo-telegram.md).

## How it's connected

| Link | What it is | Where it lives |
|---|---|---|
| **Ours** | wajo.ai in the [dedicated Chrome](dedicated-chrome.md), signed in to your Wajo account. A fixed script fills Fo's box ("Ask Fo anything…"), checks the text, and clicks **Send**. It never clicks the box itself. Messages are read from the JSON that wajo.ai's own page loads: its chat list and each chat's messages, called from inside the tab. | The sign-in is in the Chrome profile. |
| **Pins** | The account signed in first, its workspace, the time it was pinned, and the chat sending types into. Later commands refuse to run if another account is signed in. | `~/Library/Application Support/agent-runner/wajo/pins.json` |

Wajo keeps every chat on its servers, so nothing is lost while disconnected. The next `bench sync wajo` records what arrived in the meantime.

## Chats

On wajo.ai, Fo's conversation is split into chats. Wajo also makes chats out of your Telegram conversation, splitting it when there's a long gap.

- **Every chat with Fo is recorded,** from when the account was pinned (2026-11-07 18:55 PT). That includes the chats Wajo makes from Telegram, so a message you send Fo from your phone is recorded too. Messages from before the pin aren't recorded here: the Telegram channel recorded those, so they'd be there twice.
- **Sending types into one kept chat.** The first send starts it from wajo.ai/fo, and every later send continues it, as Hark's main thread does. The first one was started by the test ping on 2026-11-07 ("Untitled chat", `882dd9a8…`). To see the chats, or choose another:

  ```bash
  uv run bench wajo-thread
  ```

  ```bash
  uv run bench wajo-thread "History Travel Interests"
  ```

  ```bash
  uv run bench wajo-thread new
  ```

  The first lists every chat and marks the one sending goes to. The second chooses a chat by its title, its ID, or the start of either. The third makes the next send start a new chat, which is then kept. Fo's memory carries across chats either way.
- **The tab is moved to that chat's page before typing,** if it's showing another one. It never moves away from a message box with text in it.
- **In recordings,** each message says which chat it's in: `chat`, `chat_title` and `chat_channel` (`web` or `telegram`) in the inbox, and `thread: "wajo:chat:<id>"` in `data/conversations/wajo.jsonl`. A run's transcript labels a message from another chat.

## What's recorded, and what's different from Telegram

- **Fo's messages, with Wajo's own IDs and server times.** Each of Fo's answers also lists the tools it used (`tools`: name and status, like `web_search`), and the Pages it made (`pages`: `wajo.ai/d/<id>`). Pages count as links, so `bench run` saves them into the run's `deliverables/`.
- **While Fo works,** the chat has an active run. That's recorded as "working" stretches, like typing. Steps of Fo's work with no text aren't messages.
- **wajo.ai shows more than Telegram did.** In a chat Wajo made from Telegram, Fo's answers that reached your phone have IDs ending in `-reply-N`. Its other answers stayed on wajo.ai, like two on 11-01 that said the task was closed. In a chat started on wajo.ai, every answer shows only there.
- **Nothing on the page is pressed but Send.** Buttons on Fo's messages, like Share or Open, are left alone.
- **Read state:** wajo.ai shows no unread marks or read receipts, so opening a chat changes nothing the runner records.

## Set up for the first time

Do [Before you start](README.md#before-you-start) first. You need your Wajo account and about 5 minutes.

**A card is connected, so keep Fo asking first.** See [INTEGRATIONS_PLAN.md](../../../INTEGRATIONS_PLAN.md#wajo-fo).

1. In `config/agents.yaml`, under `agents:`, set Wajo's channel. Its handle is always `wajo.ai`, which means whichever account is signed in there in the dedicated Chrome:

   ```yaml
   wajo:
     channel: wajo
     handle: "wajo.ai"
   ```

2. Sign in. This starts the [dedicated Chrome](dedicated-chrome.md) if it isn't running, opens wajo.ai/fo, and waits up to 10 minutes while you sign in:

   ```bash
   uv run bench wajo-login
   ```

3. Sign in in that Chrome window, through login.wajo.ai. Passkeys saved in your everyday Chrome or Keychain aren't offered in this profile. If Chrome asks "Sign in to Chrome?", choose **Use Chrome without an account**.
4. It ends with `Signed in to Wajo as <email> in the dedicated Chrome. Messages from <time> on are recorded.` That pins the account.
5. Check everything, without sending anything:

   ```bash
   uv run bench wajo-status
   ```

6. Send a first message and watch for the reply. This starts the kept chat:

   ```bash
   uv run bench send wajo "ping" --listen 120
   ```

## Check it

```bash
uv run bench wajo-status
```

It starts the dedicated Chrome if it isn't running. It sends nothing and records nothing. Healthy (2026-11-07):

```
Wajo: signed in as yschore@gmail.com (5799144f-…, plan micro)
Recording every chat with Fo, from 2026-11-08T02:55:51.201Z on
Chats: 7, latest "Untitled chat" (882dd9a8-…, from web)
Sending to: "Untitled chat" (882dd9a8-…, from web) (idle; the Wajo tab is showing it)
Message box: found, empty
```

## If something's wrong

| You see | What it means | Fix |
|---|---|---|
| `nobody is signed in to wajo.ai in the dedicated Chrome` or `wajo.ai says nobody is signed in` | Wajo's sign-in expired, or you signed out. | Run `uv run bench wajo-login` and sign in. |
| `wajo.ai is signed in as A, but agent-runner is pinned to B`, or `NOT READY: agent-runner is pinned to B` | Another account is signed in to wajo.ai in the dedicated Chrome. | Sign out on wajo.ai and sign in as B. If the benchmark really moved to a new account, [re-pin](#re-pin-the-account). |
| `Message box: has text in it`, or `Fo's message box already has text in it` | Something was left in the box. | In the dedicated Chrome, delete the text in Fo's box. |
| `found N message boxes on …, not one` | The tab isn't showing a chat, usually because it's on a sign-in page. | Sign in again. If you're signed in and it still fails, Wajo changed its page: update `Selectors` in [bench/channels/wajo.py](../../bench/channels/wajo.py). |
| `Fo's Send button didn't appear`, `…message box didn't empty` or `…didn't show up in Fo's chat within 45s` | The send may not have gone through. | Look at the chat in the dedicated Chrome before sending again. Wajo names its Send button "Send"; if that changed, update `Selectors`. |
| `wajo: wajo.ai changed; learned its new server functions` | Wajo rebuilt its site, so the IDs of the functions its page calls changed. The runner reloaded wajo.ai once and found them again. | Nothing. They're saved in `wajo/functions.json`. |
| `wajo.ai's server functions changed, and they couldn't be found again` | Wajo changed its site more than that. | Check wajo.ai/fo works in the dedicated Chrome. If it does, `bench/channels/wajo.py` needs updating. |
| `the chat agent-runner sends to (…) is gone from wajo.ai` | The kept chat was deleted on wajo.ai. Nothing was sent. | `uv run bench wajo-thread` to list the chats, then choose one, or `new`. |
| `couldn't read the Wajo tab` or `wajo.ai answered its … function with HTTP …` | The tab was reloading, or wajo.ai had a hiccup. `listen` retries these by itself. | Run the command again. |
| Chrome errors, or `another bench command is using Wajo` | | As for [Hark](hark.md#if-somethings-wrong). |

## Re-pin the account

Do this only if the benchmark really moved to another Wajo account. Recorded messages are kept.

```bash
rm "$HOME/Library/Application Support/agent-runner/wajo/pins.json"
```

```bash
uv run bench wajo-login
```

This pins the signed-in account from now on, and the next send starts a new kept chat.

## Going back to Telegram

Set `wajo: channel: telegram` and `handle: "@hey_fo_bot"` in `config/agents.yaml` (the old lines are there, commented out). Runs made on either channel keep the channel they were made on. Messages sent on wajo.ai while Telegram is the channel aren't recorded.
