# Sending from your phone or the agent's website

The runner isn't the only way to message an agent. You can also use the phone app, or the agent's own website. This page says which of those the runner records. Run every command from `agent-runner/`.

Tested 2026-10-25. One marked message was sent from each place, and the agent's reply checked.

## What gets recorded

| Agent | Phone app | The agent's website | The runner, on the Mac |
|---|---|---|---|
| **Wajo (Fo)** | Telegram: **recorded**, at the next `bench sync wajo` | wajo.ai: **not recorded.** The message and Fo's answer stay on wajo.ai and never show in Telegram. | Recorded |
| **Muse** | WhatsApp: **recorded**, live | muse.ai: the WhatsApp chat is shown read-only, and the main thread is a separate conversation. Not recorded. | Recorded |
| **Dots** | Slack, in the DM with the dot: **recorded** with its thread reply, at the next `bench sync dots` | The ChatGPT app: a separate channel. Not recorded. | Recorded |
| **Hark** | The Hark app: **recorded**, at the next `bench sync hark`. Hark labels it `source: ios`. | hark.com: the same main thread the runner uses. **Recorded.** | Recorded |
| **Instinct** | Not set up yet | | |

Every message recorded this way counts as sent by you, at the server's own time, the same as one the runner sent.

## Rules for testers

- **Send benchmark messages only where the runner records them:**
  - Wajo: in Telegram
  - Muse: in WhatsApp
  - Dots: in the Slack DM with the dot
  - Hark: in its main thread, from the app or hark.com

  Any device is fine: phone, Mac app or browser.
- **Never send a benchmark message on wajo.ai, in a muse.ai chat, or to the dot in the ChatGPT app.** The agent sees those messages and remembers them, but the runner doesn't. A message typed on wajo.ai becomes part of the same conversation that Fo answers in Telegram.
- **After sending from another device, sync before you rebuild a run:** `uv run bench sync <agent>`, then `uv run bench run-rebuild runs/<folder>`. WhatsApp records all the time, so Muse needs only the rebuild.

## Worth knowing

- **Wajo splits one Telegram thread into several chats on wajo.ai.** A message sent hours after the last one opened a new chat there. On a multi-day task, a later day can land in a new chat, and Fo then has to look back for the earlier days.
- **Hark records which device each message came from** (`source`: `ios` for the app, `web` or `send_bubble` for hark.com). It's in Hark's inbox, `data/inbox/hark/`, but not yet in `data/conversations/`.
- **Not checked yet:**
  - whether muse.ai shows the WhatsApp messages
  - whether the Hark app shows what the runner sent
  - the Mac apps for Telegram, WhatsApp and Slack. These are the same kind of client as the phone apps, so they should behave the same.
