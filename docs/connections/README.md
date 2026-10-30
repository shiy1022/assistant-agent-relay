# Connections: setting up, checking and reconnecting

How agent-runner reaches each personal agent, how to set each one up the first time, how to tell whether a connection still works, and what to do when one breaks. Run every command from `agent-runner/`.

Each agent's page has the same sections: how it's connected, **set up for the first time**, check it, what to do if something's wrong, and how to reconnect it.

## The agents

Each agent has two links, and either one can break:

- **Ours:** agent-runner ↔ your account on that channel. The fix is here, with a `bench` command.
- **Theirs:** the agent ↔ that channel, like Fo connected to Telegram or the dot added to Slack. Only the agent's own app can fix this one.

| Agent | Channel | Our side | Check | Doc |
|---|---|---|---|---|
| **Wajo (Fo)** | Telegram | A Telethon session on your Telegram account | `uv run bench sync wajo` | [wajo-telegram.md](wajo-telegram.md) |
| **Muse** | WhatsApp | `bench-whatsapp`, a linked device that runs in the background | `uv run bench whatsapp-status` | [muse-instinct-whatsapp.md](muse-instinct-whatsapp.md) |
| **Instinct** | WhatsApp | The same bridge (not set up yet) | `uv run bench whatsapp-status` | [muse-instinct-whatsapp.md](muse-instinct-whatsapp.md#instinct-once-you-have-access) |
| **Dots** | Slack (one workspace: the one your dot is in) | A user token for reading, and the dedicated Chrome for sending | `uv run bench slack-status` | [dots-slack.md](dots-slack.md) |
| **Hark** | hark.com | The dedicated Chrome, signed in to Hark | `uv run bench hark-status` | [hark.md](hark.md) |

Dots and Hark share the [dedicated Chrome](dedicated-chrome.md).

## Before you start

Do this once on the Mac, before setting up any agent.

1. **Tools:** with Homebrew, install uv (it runs agent-runner and fetches Python 3.12 or newer itself) and Go (it builds the WhatsApp bridge):

   ```bash
   brew install uv go
   ```

2. **Get the code from the `telegram-whatsapp-channels` branch.** It's the newest. `main` doesn't have its fixes and docs yet. Clone it into a folder named `agent-runner`, which is the name these docs use:

   ```bash
   git clone -b telegram-whatsapp-channels https://github.com/shiy1022/assistant-agent-relay.git agent-runner
   ```

   If you already have a copy, switch it to that branch and update it, from `agent-runner/`. If `git switch` says there's no such branch, run `git fetch origin` first.

   ```bash
   git switch telegram-whatsapp-channels
   ```

   ```bash
   git pull
   ```

3. **Google Chrome** in `/Applications`, for Dots and Hark. Your everyday Chrome is fine, and nothing else needs installing for the browser. See [the dedicated Chrome](dedicated-chrome.md#set-it-up-for-the-first-time).
4. **Install agent-runner's Python packages,** from `agent-runner/`:

   ```bash
   uv sync
   ```

5. **Run the tests.** They use fakes and touch no real account. They also compile the parked iMessage reader, which needs Xcode's command line tools (`xcode-select --install` if `swiftc` is missing).

   ```bash
   uv run pytest
   ```

6. **Keep your iPhone nearby.** Telegram and WhatsApp are linked by scanning a QR code with it.

**Rules for every agent:**
- Use your own accounts. The runner messages each agent as you.
- Set each agent to **ask before it sends or buys anything**. The runner records approval requests and never approves them.
- Connect nothing else to any agent for now: no email, calendar or payment method.
- Secrets go in the Keychain, entered at the `security` prompt yourself. Never paste them into chat or into a file in the repo.

## Set up from scratch

Set up the agents in this order. Each step ends with a check and a first message, so a problem shows up before you move on.

| Step | Agent | What you'll do | Time |
|---|---|---|---|
| 1 | [Wajo (Fo)](wajo-telegram.md#set-up-for-the-first-time) | Connect Fo to Telegram, make a Telegram API key, link this Mac by QR code | ~15 min |
| 2 | [Muse](muse-instinct-whatsapp.md#set-up-for-the-first-time) | Start Muse in WhatsApp, build the bridge, link it by QR code | ~15 min |
| 3 | [The dedicated Chrome](dedicated-chrome.md#set-it-up-for-the-first-time) | Start it once. Steps 4 and 5 sign it in. | ~2 min |
| 4 | [Dots](dots-slack.md#set-up-for-the-first-time) | Add the dot to Slack, create the runner's Slack app and token, sign Chrome in to Slack | ~20 min |
| 5 | [Hark](hark.md#set-up-for-the-first-time) | Sign Chrome in to Hark | ~10 min |
| Later | [Instinct](muse-instinct-whatsapp.md#instinct-once-you-have-access) | Once you have access: find its WhatsApp address and add it | ~5 min |

Each agent is one entry under `agents:` in [config/agents.yaml](../../config/agents.yaml), with a `channel` and a `handle`. An agent with a blank handle is skipped, so you can set them up one at a time. `uv run bench agents` lists the ones that are ready. When all are done, run [Check everything](#check-everything).

## Check everything

None of these send anything to an agent. `sync wajo` records whatever's new in Fo's chat, because Telegram has no separate status command.

```bash
uv run bench whatsapp-status
```

```bash
uv run bench slack-status
```

```bash
uv run bench hark-status
```

```bash
uv run bench sync wajo
```

All healthy looks like this (checked 2026-10-25):

```
bench-whatsapp is running in the background.
WhatsApp: linked, connected, offline; agents: muse

Slack: <your workspace> (https://<it>.slack.com/), team T…, signed in as <you> (U…)
…
  web client: signed in; <your dot> (DM) - <your workspace> - Slack is open in the dedicated Chrome

Hark: signed in as <your Google email> (<id>)
Main thread: <thread id> (idle)
Message box: found, empty
…

1 events recorded
```

If a check fails, open that agent's doc. It has a table that goes from the error you see to the fix.

## Which side broke?

- **A command errors, or a check says `NOT READY`, `not running`, `not linked` or `Last status: …`:** it's ours. Follow the agent's doc.
- **Every check passes and your message is recorded as sent (`→`), but no reply ever comes:** it's probably theirs. Message the agent yourself, from your phone or its app. If it doesn't answer there either, reconnect it on the agent's side. Each doc has a section for that.

## After a Mac restart

| Channel | What to do |
|---|---|
| WhatsApp | Nothing. The bridge starts itself when you log in. |
| Telegram | Nothing. Nothing runs in the background, and the session is kept in a file. |
| Hark | Nothing. Any Hark command starts the dedicated Chrome. |
| Slack (Dots) | Start the dedicated Chrome first, because Slack commands don't start it. `uv run bench hark-status` starts it and checks Hark too. |

Then run the checks above.

## What's lost while disconnected

- **Telegram, Slack and Hark:** nothing. Their servers keep the whole chat, and the next `bench sync <agent>` records what arrived in the meantime.
- **WhatsApp, offline but still linked:** nothing. WhatsApp holds messages for the device until it reconnects.
- **WhatsApp, unlinked:** messages from the gap aren't recorded. The bridge drops WhatsApp's history copy on purpose. Read them on your phone.

## If a run was cut off

Reconnect first. Then record what arrived and rebuild that run's transcript from everything recorded:

```bash
uv run bench sync <agent>
```

```bash
uv run bench run-rebuild runs/<run folder>
```

WhatsApp has nothing to sync, since the bridge records all the time, so run only `run-rebuild` for Muse or Instinct.

## Optional: an end-to-end ping

The checks above never message an agent. To confirm a reply really comes back, send one ping. It becomes part of the agent's conversation, so skip it right before a run that needs a clean context.

```bash
uv run bench send <agent> "ping" --listen 120
```

## Where everything lives

Never commit these or paste them anywhere. The Telegram session and WhatsApp's `session.db` each give full access to your account.

| What | Where |
|---|---|
| Telegram API key | Keychain: `agent-runner.telegram.api_id`, `agent-runner.telegram.api_hash` |
| Slack user token | Keychain: `agent-runner.slack.user_token` |
| Telegram session, Fo's pin | `~/Library/Application Support/agent-runner/telegram/` (`bench.session`, `peers.json`) |
| WhatsApp bridge | `~/Library/Application Support/agent-runner/whatsapp/` (`session.db`, `agents.json`, `status.json`, `inbox/`) |
| WhatsApp background job, log | `~/Library/LaunchAgents/local.personal-agent-benchmark.bench-whatsapp.plist`, `~/Library/Logs/bench-whatsapp.log` |
| Slack pins (your workspace's team ID, the dot's DM) | `~/Library/Application Support/agent-runner/slack/pins.json` |
| Hark pins (account, main thread) | `~/Library/Application Support/agent-runner/hark/pins.json` |
| Dedicated Chrome profile | `~/Library/Application Support/agent-runner/chrome/` |
| Recorded messages | `data/inbox/<channel>/` (WhatsApp's are in the bridge's `inbox/`) |

**Pins** tie each channel to one account, workspace or thread the first time it's used. After that, a command refuses to run rather than follow a change. Deleting a pins file is how you tell the runner the change is real. Only do that when it really is.

**"another bench command is using …"** means a lock is held. One command uses each channel at a time. Let the other command finish, or stop it with Ctrl-C.
