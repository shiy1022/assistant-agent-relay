# agent-runner

Runs Assistant Bench tasks against personal agents from this Mac and records their replies.

> **Now targeting Wajo and Instinct over WhatsApp.** See [../BUILD_PLAN_WHATSAPP.md](../BUILD_PLAN_WHATSAPP.md). The iMessage channel described below (`reader/`, `bench/imessage/send.py`) is built and tested but parked; you don't need to set it up.

**Use the `telegram-whatsapp-channels` branch.** It's the newest; `main` is behind. How to get it: [Before you start](docs/connections/README.md#before-you-start).

**Setting up an agent, or a connection broke?** [docs/connections/](docs/connections/README.md) has step-by-step first-time setup for each agent and its channel, how to check each connection, and how to reconnect it.

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

## Hark

Hark has no API, Mac app or number to text, so the runner uses hark.com in the same dedicated Chrome as Slack's web client. What was found along the way is in [../HARK_CONNECTION.md](../HARK_CONNECTION.md).

1. Sign that Chrome in to Hark once. It starts Chrome if it isn't running, and waits while you sign in. A passkey saved elsewhere won't be offered there, so use your password or phone.

   ```bash
   uv run bench hark-login
   ```

   The account signed in the first time is pinned, with its main thread. Later commands refuse to run if another account is signed in.
2. Check everything without sending anything:

   ```bash
   uv run bench hark-status
   ```

Then `uv run bench send hark --file prompt.txt --listen 600` sends a prompt and records Hark's replies. `bench sync hark` and `bench listen hark` work as for the other channels.

**How it works:** [bench/channels/hark.py](bench/channels/hark.py) fills Hark's message box, checks the text, and clicks Send. It never clicks the box itself, because Hark covers the empty box with a tappable suggested prompt. Replies are read from the JSON hark.com's own page loads, so each message has Hark's ID and server time. While Hark works or browses, that's recorded as a "working" or "browsing" stretch, like typing. Buttons on Hark's messages are recorded, never pressed. `hark-status` also shows Hark's own token meter.

A Page Hark builds (the website behind "Your Sedona Page is ready") is recorded as a message of its own: `Page: <name>` with its `hark.com/site/<project>` link, which opens in the dedicated Chrome, or any browser signed in to Hark. Since the link needs Hark's sign-in, the channel also saves a copy of the page as that message's attachment, under `data/inbox/hark/attachments/`: one HTML file with the page's images and fonts embedded, so it opens anywhere, offline, as Hark showed it. `bench run` copies it into the run's `deliverables/`.

`uv run bench hark-import runs/<folder>` records a Hark run made before this channel existed, with the times it was observed. Import before the first sync, which would otherwise record those messages with the time it syncs.

## The console

`bench dashboard` writes a snapshot. `bench console` is the same view, live in your browser, with a box to send messages from:

```bash
uv run bench console --open
```

Leave it running while you use it. Ctrl-C stops it. Only this Mac can reach it, and the address it prints carries a token.

- **To:** pick one agent or several. A message to several goes out on each channel at about the same time. Agents on the same channel, like Muse and Instinct, get it one after the other.
- **Message** sends it with `bench send … --listen`, recording replies for as long as you pick. Sending the next message ends the last one's listening, so you can keep the conversation going.
- **Task run** runs `bench run` with the task, step and cell, and writes `runs/…/` as usual. A run is never cut short: messages for its channel wait for it. Stop ends it as Ctrl-C would.
- **Sync** records what arrived while nothing was recording, as `bench sync` does.
- **Record a reset** writes down a context reset you did, right after the message where it took effect, as `bench reset-log` does (below).
- **Benchmark only** (on by default, in the toolbar) hides what you did with an agent outside the tasks: a message sent without a run, and the agent's replies in the half hour after it. Runs, their messages, and anything the agent sends on its own stay visible. Everything is still recorded underneath; untick it to see it all. (The same toggle is on `bench dashboard`.)
- **Late replies show up by themselves.** Every 60 seconds the console runs `bench sync` for each agent whose channel is idle (not WhatsApp, which is recorded all the time), so a reply that lands after a message's listening ended is recorded without pressing Sync. A channel with a message or run going is left alone. `--auto-sync 0` turns it off, `--auto-sync 30` makes it faster. Each agent's automatic syncs share one `data/console/autosync-<agent>/` folder.

Each message is sent by running the same bench command you'd type, so the checks, locks and records are the same. Each job's prompt and output are kept in `data/console/<job>/`. If a send fails or you stop it partway, check the conversation before sending again: the message may have gone out anyway.

`--dry-run` shows everything and sends nothing, for trying it out.

## Running a task

The task sheet isn't in this repo. How to get it, pick a step and score it: [docs/running-tasks.md](docs/running-tasks.md).

```bash
uv run bench run wajo --file prompt.txt --task "Travel vF Golden" --step "DAY 1 CHECKPOINT" --cell D2
```

This sends the prompt and records until the agent has answered and then gone quiet for its `quiet_period_seconds`, or until `max_wait_seconds` (`--quiet` and `--max-wait` override them). It works the same on every channel. Then it writes the run's folder. Ctrl-C stops early and still writes the transcript.

Last, it saves the pages the agent linked to as its answer into the folder's `deliverables/`, so the answer is in the run and not just a link to it. Each page is saved as it was served (`.html`) and as text with each link's address (`.txt`). Only links on a per-agent allowlist are fetched, `PAGES` in [bench/deliverables.py](bench/deliverables.py). So far that's Wajo's plans, `wajo.ai/d/<id>`, and Hark's Pages, `hark.com/site/<id>`, which are never fetched from here: they need Hark's sign-in, so the copy the Hark channel saved is used (above). Other links an agent sends, like referral or invite links, are never opened. If a page can't be saved, the run says so and lists it in `run.json` under `unsaved_deliverables`. To try again, or to save the pages for a run made before this existed:

```bash
uv run bench run-save runs/<folder>
```

Pages already in `deliverables/` are skipped. `--again` saves them again as they are now, for example after the agent updated its page. These files sit next to the earlier copies, which are kept.

## Recorded data

What to read, for people and for other agents:

- **`runs/<YYYYmmdd-HHMMSS>-<task>-<agent>/`**: one task prompt sent to one agent.
  - `run.json`: the task, the agent, when the run started and ended, why it stopped, the IDs of its messages, the links the agent sent, timing, and `context`: the last context reset before the prompt, if one was recorded, and how many messages and runs came between. It has the same keys for every run. Notes added by hand are kept.
  - `prompt.txt`: the prompt, exactly as sent.
  - `transcript.jsonl`: every message from the prompt to the end of the run, and any context reset made during it.
  - `transcript.md`: the same, for people.
  - `deliverables/`: what the agent made outside the chat, like the page Wajo links to. `bench run` saves those itself (above). Files put here by hand are listed in `run.json` the next time it's rebuilt.
  - `raw/`: anything else captured along the way.
- **`data/conversations/<agent>.jsonl`**: everything recorded with each agent, one message per line, as last edited, in the order sent. Each context reset is a line too, with `"role": "reset"`, where it took effect.
- **`data/resets.jsonl`**: every context reset written down, one per line (below).

Both use the same fields for every channel. They're described at the top of [bench/conversation.py](bench/conversation.py) and [bench/runs.py](bench/runs.py).

`data/conversations/` is written again after every `send`, `run`, `listen`, `sync` and `reset-log`, or with `uv run bench export`. Messages that arrive after a run ends, like Telegram messages picked up by a later `bench sync`, reach a run's transcript with:

```bash
uv run bench run-rebuild runs/<folder>
```

The channel inboxes (`data/inbox/<channel>/messages.jsonl`, and WhatsApp's in `~/Library/Application Support/agent-runner/whatsapp/inbox/`) are the raw event logs these are built from. In an inbox, a message appears again on every edit, receipt or reaction, and lines aren't in the order sent, so don't read them directly.

## Context resets

A reset mostly happens outside what the runner records: you ask the agent to forget, send it `/new`, or delete something on its website. So write each one down as soon as it's done, including attempts that didn't work, or later runs can't tell what the agent was told before. In the console, use **Record a reset**. Or:

```bash
uv run bench reset-log muse --how "Asked Muse to forget everything; you said yes from your phone" --after 3EB0B86206C514E4FAB2D0 --result "Says its memory is empty"
```

`--after` is the message the reset took effect after, usually the agent's "done" (its id in `data/conversations/<agent>.jsonl`, or the id's last part). Without it, the reset is placed at `--at TIME`, or now. `--method` is `asked` (the default), `command` or `by hand`. `uv run bench reset-log` lists them all, and `--remove ID` takes back one written down by mistake.

Clearing a Telegram chat is found without this, from the notice Telegram leaves in the chat. The dashboard and the console show each reset as a marked divider, and each message that did it as part of the reset. What's known about resetting each agent: [docs/context-reset.md](docs/context-reset.md).

## Tests

```bash
uv run pytest
```

The tests build a test version of `bench-reader` and run it against a fake `chat.db`. They never touch your real messages.
