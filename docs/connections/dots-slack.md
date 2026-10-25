# Dots on Slack (PJTTY)

Back to [all connections](README.md).

Only the **PJTTY** workspace is ever used. Every command first checks that the token belongs to PJTTY, and to PJTTY's pinned team ID, and stops if it doesn't. Never point agent-runner at another workspace, and never "fix" a workspace error by changing the config.

## How it's connected

| Link | What it is | Where it lives |
|---|---|---|
| **Ours: reading** | Slack's Web API, as you, with the User OAuth Token (`xoxp-…`) of the **agent-runner** Slack app installed in PJTTY. | Keychain `agent-runner.slack.user_token`. The app's manifest: [config/slack-app-manifest.yaml](../../config/slack-app-manifest.yaml). |
| **Ours: sending** | Typed into app.slack.com in the [dedicated Chrome](dedicated-chrome.md), signed in to PJTTY as you (`slack: send_via: web`). The dot ignores messages sent through the API, because Slack marks them with the app's ID. | Slack's sign-in, in the Chrome profile |
| **Pins** | PJTTY's team ID (`T0CARQF460P`), and the DM with the dot, set the first time. | `~/Library/Application Support/agent-runner/slack/pins.json` |
| **Theirs** | The dot, **yschore-dot** (member `U0CBGVDR955`), added to PJTTY from the ChatGPT desktop app. It's not the separate "ChatGPT" app (`U0CC083J1PT`). It replies in a thread under each message, about 10 s later. | `dots: handle:` in `config/agents.yaml` |

Slack keeps the whole DM on its servers, so nothing is lost while disconnected. The next `bench sync dots` records what arrived in the meantime.

## Set up for the first time

Do [Before you start](README.md#before-you-start) first. You need:
- ChatGPT Pro or Business Premium, with a dot created. There's one dot per plan.
- The ChatGPT desktop app on the Mac.
- A Slack workspace where you're a member and can install apps. Ours is **PJTTY**, and it's the only one agent-runner may use. On a Business or Enterprise Slack, an admin may have to approve both the dot and the runner's app.
- About 20 minutes.

### Connect the dot to Slack

1. In the ChatGPT desktop app, open the dot's profile → **Add** → Slack, and pick PJTTY.
2. In Slack, DM the dot yourself and check that it answers. It replies in a thread under your message, about 10 s later.

### Create the runner's Slack app and token

3. Go to [api.slack.com/apps](https://api.slack.com/apps) → **Create New App** → **From a manifest**, pick **PJTTY**, and paste [config/slack-app-manifest.yaml](../../config/slack-app-manifest.yaml). It asks for user token scopes only (`chat:write`, `im:history`, `im:write`, `users:read`, and optionally `files:read`), so the app acts as you and has no bot of its own.
4. Open **OAuth & Permissions**, click **Install to Workspace**, and allow it. Leave token rotation off, because a rotating token expires.
5. Copy the **User OAuth Token** (`xoxp-…`) from the same page. Don't copy a bot token. A dot only answers its owner, so the app has to act as you.
6. Store the token in the Keychain. The command asks for it, so enter it at the prompt, and don't paste it into chat:

   ```bash
   security add-generic-password -U -a "$USER" -s agent-runner.slack.user_token -w
   ```

### Tell agent-runner about the workspace and the dot

7. In Slack, open the dot's profile → ⋮ → **Copy member ID**. Make sure it's the dot (ours is yschore-dot), not the separate "ChatGPT" app.
8. In `config/agents.yaml`, name the workspace and set sending to the web client, at the top level:

   ```yaml
   slack:
     workspace: PJTTY
     send_via: web    # the dot ignores messages sent through Slack's API
   ```

   Then add the dot under `agents:`, with its member ID:

   ```yaml
   dots:
     channel: slack
     handle: "U0CBGVDR955"    # the dot's member ID
     quiet_period_seconds: 120
     max_wait_seconds: 1800
   ```

9. Pin the workspace and the DM. This reads through the API only and sends nothing. The first run checks that the token is for PJTTY, pins PJTTY's team ID, and opens the DM with the dot:

   ```bash
   uv run bench sync dots
   ```

### Sign the dedicated Chrome in to Slack, for sending

10. Start the [dedicated Chrome](dedicated-chrome.md#set-it-up-for-the-first-time), if it isn't running.
11. Run:

    ```bash
    uv run bench slack-login
    ```

    It opens PJTTY's sign-in page in the dedicated Chrome and waits up to 10 minutes while you sign in. When Slack offers its desktop app, choose to use Slack in your browser. It ends with `Signed in to PJTTY in the dedicated Chrome.`

12. Check everything, without sending anything:

    ```bash
    uv run bench slack-status
    ```

13. Send a first message and watch for the reply:

    ```bash
    uv run bench send dots "ping" --listen 120
    ```

    It types "ping" into the DM in the dedicated Chrome, clicks Send, and records the dot's reply in its thread.

## Check it

```bash
uv run bench slack-status
```

Healthy:

```
Slack: PJTTY (https://pjtty.slack.com/), team T0CARQF460P, signed in as <you> (U…)
Token scopes: identify, im:history, files:read, users:read, chat:write, im:write
Sending: typed into the web client
dots: U0CBGVDR955 → yschoredot (U0CBGVDR955, bot)
  web client: signed in; yschore-dot (DM) - PJTTY - Slack is open in the dedicated Chrome
```

It opens the DM in the dedicated Chrome, but types nothing and records nothing.

## If something's wrong

| You see | What it means | Fix |
|---|---|---|
| `the Slack token was rejected` or `the Slack token was revoked` | The token stopped working, for example because the app was uninstalled. | [Get a new token](#get-a-new-token) |
| `the Slack token expired. Turn off token rotation…` | Token rotation got turned on for the app. | Turn it off under the app's OAuth & Permissions, then [get a new token](#get-a-new-token). |
| `missing the … scope` or `missing these user token scopes` | The app lost a scope. | Add it under OAuth & Permissions → **User Token Scopes** (compare with the manifest), then [get a new token](#get-a-new-token). |
| `no Slack token found` | The Keychain entry is gone. | [Get a new token](#get-a-new-token) (just step 4 if you still have the token) |
| `the stored Slack token isn't a user token (xoxp-…)` | The bot token was stored. | Store the **User** OAuth Token instead. |
| `the Slack token is for …, not PJTTY` | The token is from another workspace. | Make sure you copy the token of the app installed **in PJTTY**. |
| `… but PJTTY was pinned as team T…` | The token is for a different workspace that's also named PJTTY. | Same as above. Delete `pins.json` only if PJTTY itself was really recreated. |
| `web client: NOT READY: the dedicated Chrome isn't running…` | Chrome isn't up. Slack commands don't start it. | [Start the dedicated Chrome](dedicated-chrome.md#start-it) |
| `web client: NOT READY: Slack's message box didn't appear…` | Chrome is signed out of Slack. | [Sign Chrome back in](#sign-chrome-back-in-to-slack) |
| `the message box already has a draft in it` | There's text in the dot's DM box. Slack syncs drafts, so it may be from your phone. | Delete the draft in that DM, on any device. |
| `clicked Send, but Slack's message box didn't empty` | The send may not have gone through. | Look at the DM in the dedicated Chrome. If Slack changed its page, update `Selectors` in [bench/channels/slack_web.py](../../bench/channels/slack_web.py). |
| `typed and sent the message …, but it didn't show up through the API within 30s` | It was sent, but the API didn't show it in time. | Check the DM, then `uv run bench sync dots`. |
| `Slack has no member with that ID` or `the DM with this agent doesn't exist any more` | The dot was removed, or came back as a new member. | [The dot's side](#the-dots-side) |
| `run uv run bench sync with a Slack agent first…` (from `slack-login`) | `pins.json` is missing. | `uv run bench sync dots`, then `slack-login` again. |
| `another bench command is using Slack; wait for it to finish` | Another `bench` command is using Slack. | Let it finish, or stop it with Ctrl-C. |
| Your message shows as sent (`→`) but no thread reply ever comes | Probably the dot's side. | DM the dot yourself in Slack. If it doesn't answer there either, see [the dot's side](#the-dots-side). |

## Get a new token

1. Go to [api.slack.com/apps](https://api.slack.com/apps) and open **agent-runner** in PJTTY. If it's gone: Create New App → From a manifest → pick **PJTTY** → paste [config/slack-app-manifest.yaml](../../config/slack-app-manifest.yaml).
2. Under **OAuth & Permissions**, click **Install to Workspace** (or **Reinstall**) and allow it.
3. Copy the **User OAuth Token** (`xoxp-…`), not a bot token.
4. Store it. The command asks for it, so enter it at the prompt, and don't paste it into chat:

   ```bash
   security add-generic-password -U -a "$USER" -s agent-runner.slack.user_token -w
   ```

5. Check it:

   ```bash
   uv run bench slack-status
   ```

## Sign Chrome back in to Slack

1. Make sure the [dedicated Chrome](dedicated-chrome.md) is running. Slack commands don't start it.
2. Run:

   ```bash
   uv run bench slack-login
   ```

   If Chrome is still signed in, it says so and stops. Otherwise it opens PJTTY's sign-in page there and waits up to 10 minutes while you sign in. When Slack offers its desktop app, choose to use Slack in your browser.
3. `uv run bench slack-status` should end with `web client: signed in; …`.

## The dot's side

If the dot doesn't answer even when you DM it yourself:

1. In the ChatGPT desktop app, open the dot's profile → **Add** → Slack, and pick PJTTY.
2. DM it in Slack yourself. It should answer in a thread within about 10 s.
3. If it came back as a new Slack member (`slack-status` says `Slack has no member with that ID`), open its profile in Slack → ⋮ → **Copy member ID**. Make sure it's yschore-dot, not the "ChatGPT" app. Put the ID in `config/agents.yaml` under `dots: handle:`. A changed handle makes the next `uv run bench sync dots` pin the dot again and open the DM with the new member. Messages recorded from the old DM are kept.

## Keeping it connected

- If you uninstall the agent-runner app or change its scopes, [store the new token](#get-a-new-token) afterwards.
- Don't type in the dot's DM box from any device while a run is going, because a leftover draft blocks the next send.
- Slack can sign the dedicated Chrome out, for example if you sign out of all sessions from another device. `slack-status` catches this before a run.
