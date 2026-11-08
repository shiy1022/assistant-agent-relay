# Muse on muse.ai

Back to [all connections](README.md). Since 2026-11-07 this is how the runner reaches Muse. WhatsApp is still an option: [muse-instinct-whatsapp.md](muse-instinct-whatsapp.md).

## How it's connected

| Link | What it is | Where it lives |
|---|---|---|
| **Ours** | muse.ai in the [dedicated Chrome](dedicated-chrome.md), signed in to your Meta account. A fixed script fills Muse's box (消息, or "Message" in English), checks the text, and clicks **发送** (Send). It never clicks the box itself. | The sign-in is in the Chrome profile. |
| **Pins** | The account signed in first, and the cloud computer Muse runs on. Later commands refuse to run if another account is signed in. | `~/Library/Application Support/agent-runner/muse/pins.json` |

**How replies are read:** muse.ai's messages travel over an encrypted connection to Muse's cloud computer, so the network can't be read. Instead the runner reads the copy muse.ai keeps of the main chat in the browser (IndexedDB `hatch-chat-cache`, store `main-transcripts`). It holds the latest 40 messages, each with Muse's own ID, role, text, time and whether it's still streaming. muse.ai rewrites it a few seconds after each change: 8 s after the test ping on 2026-11-07, and up to about 25 s for a streamed reply. Nothing else on the page is read, except whether Muse's Stop button (停止) shows.

Muse keeps the chat on its servers, and muse.ai loads it again when the tab opens, so nothing is lost while disconnected. Only the latest 40 messages are in the copy, so sync at least that often during a busy stretch. `bench run` and `bench listen` sync every 3 seconds.

## What's recorded

- **The main chat:** what you send, Muse's answers, and the heads-ups Muse posts there on its own (several a day in early November). Its first sync recorded the 40 messages muse.ai held then, back to 2026-10-28.
- **Cards** Muse shows (options, files, approvals) are recorded as text and buttons, never pressed.
- **While Muse works** (its Stop button shows), that's recorded as "working" stretches, like typing.
- **A streamed reply** is recorded as it grows, like Muse's WhatsApp replies were.
- **Times:** a message's time can move by some seconds once muse.ai loads the chat again. The latest one is kept.
- **Not recorded:**
  - Muse's WhatsApp chat, by this channel. muse.ai shows it as a read-only side chat. bench-whatsapp records only the agents in its `agents.json`, which `bench` rewrites from `config/agents.yaml` the next time a WhatsApp command sends, listens or links. Until then it still records Muse's WhatsApp chat as before; after that, not at all. So send benchmark messages only in the main chat, on muse.ai or in the Muse app.
  - Side chats, if you start any.
- **Read state:** with the muse.ai tab open on the main chat, muse.ai counts its messages as seen. The WhatsApp side chat is never opened.

## Set up for the first time

Do [Before you start](README.md#before-you-start) first. You need your Meta account for Muse and about 5 minutes.

1. In `config/agents.yaml`, under `agents:`, set Muse's channel. Its handle is always `muse.ai`:

   ```yaml
   muse:
     channel: muse
     handle: "muse.ai"
     quiet_period_seconds: 180
     max_wait_seconds: 1800
   ```

2. Sign in. This starts the [dedicated Chrome](dedicated-chrome.md) if it isn't running, opens muse.ai, and waits up to 10 minutes while you sign in:

   ```bash
   uv run bench muse-login
   ```

3. In that Chrome window, sign in with your Meta account (Continue as …). If Chrome asks "Sign in to Chrome?", choose **Use Chrome without an account**. Wait until the main chat shows.
4. It ends with `Signed in to Muse (account <id>) in the dedicated Chrome.` That pins the account.
5. Check everything, without sending anything:

   ```bash
   uv run bench muse-status
   ```

6. Send a first message and watch for the reply:

   ```bash
   uv run bench send muse "ping" --listen 120
   ```

## Check it

```bash
uv run bench muse-status
```

It starts the dedicated Chrome if it isn't running. It sends nothing and records nothing. Healthy (2026-11-07):

```
Muse: signed in as account 1452962344561730 (cloud computer 15d53689-…)
Main chat: 40 messages in muse.ai's copy, the latest at 2026-11-08T02:55:51.280Z (idle)
Side chat: 'WhatsApp' (read-only on muse.ai; not recorded)
Sending to: the main chat (the Muse tab is showing it)
Message box: found, empty
```

## If something's wrong

| You see | What it means | Fix |
|---|---|---|
| `nobody is signed in to muse.ai in the dedicated Chrome, or its chat isn't showing` | Muse's sign-in expired, or the tab is on Meta's sign-in page. | Run `uv run bench muse-login` and sign in. |
| `muse.ai hasn't shown its chat in the dedicated Chrome yet` | muse.ai hasn't saved its copy of the chat in this Chrome. | Open muse.ai in the dedicated Chrome and wait for the chat to show. |
| `muse.ai is signed in as account A, but agent-runner is pinned to B` | Another Meta account is signed in to muse.ai. | Sign in as B. If the benchmark really moved, [re-pin](#re-pin-the-account). |
| `muse.ai's copy of the main chat isn't there for the pinned account right now` | The tab was reloading. `listen` retries this by itself. | Run the command again. If it keeps failing, reload muse.ai in the dedicated Chrome. |
| `Message box: has text in it`, or `Muse's message box already has text in it` | Something was left in the box. | In the dedicated Chrome, delete the text in Muse's box. |
| `Muse's Send button didn't appear`, `…message box didn't empty` or `…didn't show up in Muse's main chat within 45s` | The send may not have gone through. | Look at muse.ai in the dedicated Chrome before sending again. muse.ai names its buttons in Chrome's language; the runner knows the Chinese and English names. If the page shows another language, add its names to `Selectors` in [bench/channels/muse.py](../../bench/channels/muse.py). |
| Chrome errors, or `another bench command is using Muse` | | As for [Hark](hark.md#if-somethings-wrong). |

## Re-pin the account

Do this only if the benchmark really moved to another Meta account. Recorded messages are kept.

```bash
rm "$HOME/Library/Application Support/agent-runner/muse/pins.json"
```

```bash
uv run bench muse-login
```

## Going back to WhatsApp

Set `muse: channel: whatsapp` and `handle: "1807055946647697@bot"` in `config/agents.yaml` (the old lines are there, commented out), then run `uv run bench listen muse --seconds 5`, which rewrites bench-whatsapp's list so it records Muse again. Runs made on either channel keep the channel they were made on.
