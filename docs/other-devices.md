# Sending from your phone or the agent's website

The runner isn't the only way to message an agent. You can also use the phone app, or the agent's own website. This page says which of those the runner records. Run every command from `agent-runner/`.

Tested 2026-10-25, with one marked message sent from each place and the agent's reply checked. Since 2026-11-07 the runner reaches Wajo and Muse on their websites instead of Telegram and WhatsApp, which changes their rows (below, from how the sites work; the phone apps weren't re-tested).

## What gets recorded

| Agent | Phone app | The agent's website | The runner, on the Mac |
|---|---|---|---|
| **Wajo (Fo)** | Telegram: **recorded**, at the next `bench sync wajo`. Wajo shows your Telegram conversation as chats on wajo.ai, and the runner records every chat there. It lands in another chat than the runner's kept one. | wajo.ai: **recorded.** This is the runner's channel now. | Recorded, on wajo.ai |
| **Muse** | WhatsApp: **not recorded** by the muse.ai channel. bench-whatsapp still records it until its agent list is rewritten (see [muse-web.md](connections/muse-web.md#whats-recorded)). The Muse app: not checked yet; it most likely shows the same main chat as muse.ai. | muse.ai: the main chat is **recorded.** This is the runner's channel now. The WhatsApp chat shows there read-only. | Recorded, in muse.ai's main chat |
| **Dots** | Slack, in the DM with the dot: **recorded** with its thread reply, at the next `bench sync dots` | The ChatGPT app: a separate channel. Not recorded. | Recorded |
| **Hark** | The Hark app: **recorded**, at the next `bench sync hark`. Hark labels it `source: ios`. | hark.com: the same main thread the runner uses. **Recorded.** | Recorded |
| **Instinct** | Not set up yet | | |

Every message recorded this way counts as sent by you, at the server's own time, the same as one the runner sent.

## Rules for testers

- **Send benchmark messages only where the runner records them:**
  - Wajo: on wajo.ai, in the chat the runner keeps (`uv run bench wajo-thread` shows which)
  - Muse: in its main chat on muse.ai
  - Dots: in the Slack DM with the dot
  - Hark: in its main thread, from the app or hark.com

  Any device is fine: phone, Mac app or browser.
- **Never send a benchmark message in Muse's WhatsApp chat or to the dot in the ChatGPT app.** The agent sees those messages and remembers them, but the runner doesn't. Telegram messages to Fo are recorded, but in another chat than the runner's.
- **Before 2026-11-07** it was the other way round for Wajo and Muse: Telegram and WhatsApp were recorded, wajo.ai and muse.ai weren't. Runs from then keep their channel.
- **After sending from another device, sync before you rebuild a run:** `uv run bench sync <agent>`, then `uv run bench run-rebuild runs/<folder>`. A run holds only messages on the channel it was made on.

## Worth knowing

- **Wajo splits one Telegram thread into several chats on wajo.ai.** A message sent hours after the last one opened a new chat there. The runner doesn't: it keeps sending in one chat on wajo.ai until you choose another with `bench wajo-thread`.
- **Hark records which device each message came from** (`source`: `ios` for the app, `web` or `send_bubble` for hark.com). It's in Hark's inbox, `data/inbox/hark/`, but not yet in `data/conversations/`.
- **Not checked yet:**
  - whether the Muse app shows muse.ai's main chat
  - whether the Hark app shows what the runner sent
  - the Mac apps for Telegram, WhatsApp and Slack. These are the same kind of client as the phone apps, so they should behave the same.
