# Resetting an agent's context

What's known so far about wiping each agent's context so a run starts from scratch, and how to check that a wipe really worked. Run every command from `agent-runner/`.

**Status, 2026-10-25:** quick tests on Wajo, Muse, Dots and Hark. No reset has passed the full check below yet. Until one has, note in the `run.json` of a task's first run what the agent had already been told ([Running the tasks](running-tasks.md)).

## What "context" means

An agent keeps context in several places, and a reset can clear some and miss others:

| Layer | What it is |
|---|---|
| **Thread** | The chat the agent reads back |
| **Memory** | Saved facts, preferences and persona |
| **Pending work** | Reminders, schedules, goals, to-dos, background jobs. These send messages into later runs on their own. |
| **Artifacts** | Pages, files, projects, saved workflows |
| **Connected data** | Connectors and what was read from them. Disconnecting doesn't delete what the agent already learned. |
| **Ambient** | Account name, email, phone, location, timezone, browser language, and anything the agent can read again on its own, like a Slack workspace's channels or public web pages. No reset clears these. Keep them the same across runs. |

**Reset between runs, never between the steps of one task.** A task's `LEARNING` steps test that the agent remembers the earlier days. A reset has to clear memory without turning memory off.

## Each agent

### Wajo (Fo), Telegram

None of these gives a clean start yet.

- **`/new`, `/reset` and `/clear` are just chat.** Fo answers each one ("fresh slate it is", "cleared") and keeps everything. `/new` does start a new conversation on wajo.ai.
- **Asking Fo to delete what it holds** removes memories, schedules, contacts and pages, in theory. In the test:
  - Fo found no memories, but one **tracked goal**, which it can only cancel, not delete. The cancelled goal keeps all its details, and Fo still reads it.
  - Fo said it had deleted a plan page. The page still loaded afterwards. **Don't trust an agent's own report of what it deleted.**
- **The Telegram conversation shows up as chats on wajo.ai.** You can delete them there, one at a time, and Fo seems to stop reading them. The cancelled goal stays.
- **Clearing the chat in Telegram changes nothing on Fo's side.** Wajo keeps its own copy of every message.
- **What's left:** email support@wajo.ai to wipe your data and keep the account, or delete the account (wajo.ai/user, General tab). Neither has been tried.

### Muse, WhatsApp

- **`/reset-ai`, `/reset` and `/new` aren't resets.** `/reset-ai` is a Meta AI command; Muse doesn't know it, and offers choices instead. `/new` only says it'll start fresh.
- **Asking Muse to forget works in two steps.** It writes a cleanup plan listing what it will delete permanently, then waits for a yes. Send that yes yourself, from your phone. After it, Muse reported empty memory, no goals or tasks, and wouldn't recall the old trip.
- **The old messages stay in the WhatsApp chat.** Muse could still read them. It just doesn't use them.
- **Reset Muse** (muse.ai → Settings → Data Controls) is the full reset. It can't be undone, and it hasn't been tried.
- **Check Muse's connectors once.** Facebook, Instagram and Threads can connect on their own when they share an Accounts Center with Muse.

### Instinct, WhatsApp

Not set up yet. Public reports say `/new` gives "Fresh slate it is." Long-term memory probably survives it.

### Dots, Slack

- **Never delete the dot.** Deleting is its only full reset. But since early October 2026 many users couldn't create a new dot after deleting one, and a plan gets only one dot.
- **Asking the dot to forget specific things worked on the first check.**
  - It recalled two planted facts in a new thread, and applied them unasked.
  - Asked to forget them, it said it removed them. It needed no confirmation, and it noted the old messages stay in Slack.
  - Asked again right after, in new threads, it didn't use them.
  - The 24 h check is still to do.
- **Each top-level DM is its own thread, but they all share the dot's notes.** A new DM isn't a new context.
- **The dot can read PJTTY's channels and public web pages, this repo included,** and remembers what it learns there. Asking it to forget can't stop that.

### Hark, hark.com

- **Asking Hark to forget everything** got "Done", with no confirmation step. Afterwards:
  - It said it had nothing saved.
  - It wouldn't recall the old trip, while pointing out the old messages are still in the thread.
  - Its scheduled tasks were empty, and its Home cards generic.
- **The main thread can't be archived or deleted from the page.** Hark's own API has calls for it, but the runner never calls Hark's API to change anything.
- **Settings** has Scheduled tasks (each can be deleted), and System preferences → Data (export, and delete the account). There's **no page for viewing or deleting memory.**
- **"Wipe your memory" leaves Pages alone.** On 2026-10-27, half an hour after a wipe that reported "only your name is left", Hark linked its Oct 25 Sedona Page (2 adults, Fri Jan 15, a $5,000 budget) as the itinerary for a fresh run with different dates and one traveller. Pages and Projects are the artifacts layer: delete them separately (hark.com → Projects, and each Page's own menu) as part of a reset, and check the thread's old Page cards afterwards.
- **Hark's page follows the browser's language.** The dedicated Chrome is set to Chinese, so the runner also knows Hark's Chinese name for the Send button (`Selectors` in [bench/channels/hark.py](../bench/channels/hark.py)).

## How to check a reset

An agent saying "I don't remember" proves nothing. It may still use the fact, or make something up. So:

1. **Make up facts** for each layer:
   - a fact
   - a standing instruction
   - a reminder a few days out
   - a small page or note
   - a preference that would change a real task

   Use unusual values nobody could guess. Keep them in `data/canaries/` (gitignored). **Never commit them or put them anywhere public**: the agents read the web.
2. **Plant them**, and wait 24 h. Some agents write memory hours later.
3. **Check they stuck**, in a new thread where the agent has one. Ask about each topic **without naming the value**, and give a small task the preference would change. A fact the agent can't recall here can't be tested: plant it again, or drop it.
4. **Reset.**
5. **Ask the same way again,** right away and 24 h later. Also check the agent's own pages for memory, schedules and files, on every surface it has. Let the reminder's time pass: if it arrives, pending work survived.
6. **Score each fact:**
   - **cleared:** recalled before the reset, and absent after it, in answers, pages and behaviour
   - **leaked:** something shows it after the reset
   - **untestable:** step 3 failed
7. **Reset again,** because the questions themselves are now part of the context. An answer that repeats a fact also puts it back in the thread.

**Every run** then gets only checks that send the agent nothing:
- **Before:** do the agent's reset, and check its pages are empty.
- **After:** scan the transcript for every made-up value ever used, and for phrases like "last time" and "you mentioned".

## Recording a reset

Write down every reset, and every attempt, as soon as it's done: `uv run bench reset-log AGENT --how "…" --after MESSAGE`, or **Record a reset** in the console. It then shows where it took effect in `data/conversations/<agent>.jsonl`, in run transcripts, and in the dashboard and the console. Each later run's `run.json` names it under `context`, with how many messages and runs came after it. Clearing a Telegram chat is found by itself. See [Context resets](../README.md#context-resets).

The quick tests of 2026-10-25 are written down already, timed by their messages. Deleting the chats on wajo.ai left no message, so it's placed at the latest it can have been, just before the next question at 15:34.

## Still to do

- `bench reset <agent>`: does the scripted part of each agent's reset and walks you through the rest. Recording where it happened is done (`bench reset-log`).
- The `context` block in `run.json` has the last reset before the run, and the messages and runs since. Still to add: the reset's level, and the check that backs it.
- `bench leakcheck`: the per-run scan.
- The full check above for each agent, starting with Muse, Hark and Dots.
