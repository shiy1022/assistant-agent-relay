# agent-runner

Runs Assistant Bench tasks against personal agents from this Mac and records their replies.

> **Now targeting Wajo and Instinct over WhatsApp.** See [../BUILD_PLAN_WHATSAPP.md](../BUILD_PLAN_WHATSAPP.md). The iMessage channel described below (`reader/`, `bench/imessage/send.py`) is built and tested but parked; you don't need to set it up.

So far (M0): a sender that sends iMessages through the Messages app, and `bench-reader`, the only program allowed to read Messages. It copies the agents' chats, and nothing else, into `data/inbox/`.

## Set up

1. Put the handles in `config/agents.yaml`: phone numbers with country code, or emails. For the dry run, set `self` to your own iMessage handle.
2. Install the reader:

   ```bash
   reader/install.sh
   ```

3. Approve it in System Settings → Privacy & Security → Full Disk Access. The install script opens that pane and prints the path to add. Then restart it:

   ```bash
   launchctl kickstart -k gui/$(id -u)/local.personal-agent-benchmark.bench-reader
   ```

4. Check that it's reading:

   ```bash
   uv run bench reader-status
   ```

   The log should end with `reading chat.db: …`, listing which agents already have a chat. "authorization denied" means the grant hasn't taken effect yet.

Every change to `config/agents.yaml` needs `reader/install.sh` again, and a new approval.

## Try it

```bash
uv run bench send self "ping from agent-runner"
```

```bash
uv run bench inbox --follow
```

The first `send` asks to let Terminal (or Claude) control Messages. Allow it.

## Slack (Dots)

Dots are reached through a DM in Slack, sent from your own Slack account through Slack's Web API. Only the **PJTTY** workspace is used (`slack: workspace:` in `config/agents.yaml`). Every command first checks that the token belongs to PJTTY and stops if it doesn't. PJTTY's team ID is pinned the first time, so another workspace with the same name is refused too. The only conversation opened, read or written is the DM with each Slack agent listed.

1. Connect your dot to PJTTY: in the ChatGPT desktop app, open the dot's profile → **Add** → Slack, and pick PJTTY. Send it a DM in Slack to check that it answers.
2. Create the runner's Slack app **in PJTTY**: go to [api.slack.com/apps](https://api.slack.com/apps) → Create New App → From a manifest, pick PJTTY, and paste [config/slack-app-manifest.yaml](config/slack-app-manifest.yaml). Then Install to Workspace.
3. Copy the **User OAuth Token** (`xoxp-…`) from OAuth & Permissions and store it in the Keychain:

   ```bash
   security add-generic-password -U -a "$USER" -s agent-runner.slack.user_token -w
   ```

4. In Slack, open the dot's profile → ⋮ → Copy member ID, and put it in `config/agents.yaml` under `dots: handle:`.
5. Sign in to PJTTY's Slack once in the dedicated Chrome (the agent-runner profile, started with `--remote-debugging-port=9222`; see `CHROME_COMMAND` in [bench/channels/slack_web.py](bench/channels/slack_web.py)):

   ```bash
   uv run bench slack-login
   ```

   If Chrome is already signed in, it says so and stops. Otherwise it opens PJTTY's sign-in page there and waits while you sign in. When Slack offers its desktop app, choose to use Slack in your browser.
6. Check everything without sending anything:

   ```bash
   uv run bench slack-status
   ```

Then `uv run bench send dots "ping" --listen 120` sends one message and records the reply. `bench sync dots` and `bench listen dots` work the same way as for Telegram. Slack's API has no read receipts or typing indicators, so a reply counts as finished only after the quiet period passes with no new messages or edits.

**Why sending goes through the website:** the dot ignores messages sent through Slack's API, because Slack marks each one with the sending app's ID. So `slack: send_via: web` types each prompt into Slack's web client instead. That's done by `SlackWeb` in [bench/channels/slack_web.py](bench/channels/slack_web.py). It's self-contained: it needs only Playwright and the dedicated Chrome, so it can be reused outside the runner. Its docstring shows how. It opens only `app.slack.com/client/<PJTTY's ID>/<the dot's DM>`. It types only after checking that the tab shows that DM in PJTTY and that the DM's own message box is empty, and never into an open thread's box. Then it clicks Send. Slack's markup lives in one place, `Selectors`, in case Slack changes it. Reading still goes through the API. The dot answers each message in a thread, and the runner records thread replies.

## Tests

```bash
uv run pytest
```

The tests build a test version of `bench-reader` and run it against a fake `chat.db`. They never touch your real messages.
