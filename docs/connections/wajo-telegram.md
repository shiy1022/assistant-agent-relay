# Wajo (Fo) on Telegram

Back to [all connections](README.md).

## How it's connected

| Link | What it is | Where it lives |
|---|---|---|
| **Ours** | agent-runner signs in to your own Telegram account as a desktop device, through Telegram's official API (Telethon). It shows up in Telegram → Settings → Devices. Nothing runs in the background. It connects only while a `bench` command runs. | API key: Keychain `agent-runner.telegram.api_id` and `agent-runner.telegram.api_hash` (from [my.telegram.org](https://my.telegram.org)). Session: `~/Library/Application Support/agent-runner/telegram/bench.session`. |
| **Theirs** | Fo is connected to Telegram from wajo.ai → Settings → General. Its chat is the bot `@hey_fo_bot`. | Fo's numeric Telegram ID is pinned the first time the username is resolved, in `telegram/peers.json`. |

Telegram keeps the whole chat on its servers, so nothing is lost while disconnected. The next `bench sync wajo` records what arrived in the meantime.

## Set up for the first time

Do [Before you start](README.md#before-you-start) first. You need your iPhone, a Wajo account, and about 15 minutes.

### On your iPhone: Telegram and Fo

1. **Telegram:** install it from the App Store and sign up with your number, if you don't have an account yet.
2. **Connect Fo to Telegram:** in Safari, sign in at [wajo.ai](https://wajo.ai/) and open Settings → General → connect Telegram. That opens Fo's chat in Telegram. Tap **Start** if Telegram shows it.
3. Send Fo "hi" and wait for a reply.
4. Tap Fo's name at the top of the chat and note its **username**, which starts with @. Ours is `@hey_fo_bot`.
5. Check that Fo **asks before spending money**, because it has its own payment card. Connect nothing else to Fo for now: no email, calendar or payment.

### On the Mac: an API key

agent-runner uses Telegram's official API, which needs a free API key of your own.

6. Go to [my.telegram.org](https://my.telegram.org) and log in with your number. The login code arrives in Telegram on your iPhone, not by SMS.
7. Open **API development tools** and create an app. Use the title "agent-runner", any short name (like `agentrunner`), and the platform "Desktop". Copy the **api_id** (a number) and the **api_hash**.
8. Store both in the Keychain. Each command asks for the value, so enter it at the prompt, and don't paste it into chat:

   ```bash
   security add-generic-password -U -a "$USER" -s agent-runner.telegram.api_id -w
   ```

   ```bash
   security add-generic-password -U -a "$USER" -s agent-runner.telegram.api_hash -w
   ```

### On the Mac: tell agent-runner about Fo

9. In `config/agents.yaml`, under `agents:`, put Fo's username:

   ```yaml
   wajo:
     channel: telegram
     handle: "@hey_fo_bot"    # Fo's Telegram username
   ```

### Together: link this Mac to your Telegram

10. Run:

    ```bash
    uv run bench telegram-login
    ```

    A QR code appears in the terminal. On your iPhone: Telegram → Settings → Devices → **Link Desktop Device**, then scan it. The code refreshes every 20–30 seconds, so scan the newest one. If you've set a cloud password (two-step verification), the terminal asks for it. Type it yourself. It goes straight to Telegram and isn't stored. It ends with `Logged in to Telegram as <your name>.`

11. Check it. The first time, this also pins Fo to its numeric Telegram ID:

    ```bash
    uv run bench sync wajo
    ```

12. Send a first message and watch for the reply:

    ```bash
    uv run bench send wajo "hi" --listen 120
    ```

    On your iPhone the message shows in Fo's chat as sent by you. That's expected: the runner is you, on another device.

13. **Recommended:** on your iPhone, under Telegram → Settings → Devices → **Automatically terminate old sessions**, choose the longest option. agent-runner only connects while a command runs, so a short limit would end its session between runs.

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
