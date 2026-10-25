# Wajo (Fo) on Telegram

Back to [all connections](README.md).

## How it's connected

| Link | What it is | Where it lives |
|---|---|---|
| **Ours** | agent-runner signs in to your own Telegram account as a desktop device, through Telegram's official API (Telethon). It shows up in Telegram → Settings → Devices. Nothing runs in the background. It connects only while a `bench` command runs. | API key: Keychain `agent-runner.telegram.api_id` and `agent-runner.telegram.api_hash` (from [my.telegram.org](https://my.telegram.org)). Session: `~/Library/Application Support/agent-runner/telegram/bench.session`. |
| **Theirs** | Fo is connected to Telegram from wajo.ai → Settings → General. Its chat is the bot `@hey_fo_bot`. | Fo's numeric Telegram ID is pinned the first time the username is resolved, in `telegram/peers.json`. |

Telegram keeps the whole chat on its servers, so nothing is lost while disconnected. The next `bench sync wajo` records what arrived in the meantime.

## Check it

```bash
uv run bench sync wajo
```

Healthy: it prints `N events recorded`. It records anything new in Fo's chat and sends nothing. There's no separate Telegram status command.

## If something's wrong

| You see | What it means | Fix |
|---|---|---|
| `not logged in to Telegram yet; run: uv run bench telegram-login` | The session ended. It was terminated under Devices on your phone, Telegram ended it for inactivity, or the session file is gone. | [Link again](#link-again) |
| `no Telegram api_id found` (or `api_hash`) | The Keychain entry is missing. | [Store the API key again](#store-the-api-key-again) |
| `the stored Telegram api_id should be just the number` | The api_id entry holds something other than the number. | [Store the API key again](#store-the-api-key-again) |
| `another bench command is using Telegram; wait for it to finish` | Another `bench` command has the session open. Telethon's session file can't be shared. | Let it finish, or stop it with Ctrl-C in its terminal. |
| `@… is a group or channel, not a person or bot` | `wajo: handle:` in `config/agents.yaml` is wrong. | Put Fo's username there. |
| Your message shows as sent (`→`) but Fo never replies | Probably Fo's side. | Message Fo from your phone. If it doesn't answer there either, see [Fo's side](#fos-side). |

## Link again

1. Clear the old session. This ends it with Telegram if it's still alive, and deletes the session file and Fo's pin. Recorded messages are kept.

   ```bash
   uv run bench telegram-logout
   ```

2. Link this Mac again:

   ```bash
   uv run bench telegram-login
   ```

   A QR code appears in the terminal. On your iPhone: Telegram → Settings → Devices → **Link Desktop Device**, then scan it. The code refreshes every 20–30 seconds, so scan the newest one. If you've set a cloud password (two-step verification), the terminal asks for it. Type it yourself. It goes straight to Telegram and isn't stored.

3. Check it:

   ```bash
   uv run bench sync wajo
   ```

   The first command after linking pins Fo again by its username.

Logging out first matters: `telegram-login` reuses whatever session file is there, and a session Telegram has ended can leave a dead file behind.

## Store the API key again

Use this if the Keychain entries are gone, or if you deleted the app on my.telegram.org and made a new one (API development tools, title "agent-runner", platform "Desktop"). Each command asks for the value. Type it at the prompt, and don't paste it into chat. `-U` replaces an existing entry.

```bash
security add-generic-password -U -a "$USER" -s agent-runner.telegram.api_id -w
```

```bash
security add-generic-password -U -a "$USER" -s agent-runner.telegram.api_hash -w
```

The api_id is just the number, like `1234567`. Then [link again](#link-again) if the session also ended. Otherwise `bench sync wajo` is enough.

## Fo's side

If Fo doesn't answer even when you message it from your phone:

1. In Safari, sign in at [wajo.ai](https://wajo.ai/), open Settings → General and check that Telegram is connected. Connect it again if it isn't. That opens Fo in Telegram.
2. Send "hi" from your phone and wait for a reply.
3. If Fo's username changed, put the new one in `config/agents.yaml` under `wajo: handle:`. A changed username in the config makes the runner pin Fo again. If the config doesn't change, it keeps talking to the same numeric ID, so a username that moves to another account isn't followed.

## Keeping it linked

- Don't terminate the agent-runner device under Telegram → Settings → Devices.
- Telegram ends sessions that stay inactive longer than the limit under Settings → Devices, **Automatically terminate old sessions**. agent-runner only connects while a command runs, so set the longest option there, or run `bench sync wajo` well within the limit.
- The session file gives full access to your Telegram account. It's readable only by you, and must never be committed or shared.
