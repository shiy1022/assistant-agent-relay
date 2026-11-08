# Hark on hark.com

Back to [all connections](README.md). Background on how this route was found: [HARK_CONNECTION.md](../../../HARK_CONNECTION.md).

## How it's connected

Hark has no API, Mac app or number to text, so there's only one link: hark.com itself.

| Link | What it is | Where it lives |
|---|---|---|
| **Ours** | hark.com in the [dedicated Chrome](dedicated-chrome.md), signed in with your Google account. A fixed script fills Hark's message box, checks the text, and clicks Send. It never clicks the box itself, because Hark covers the empty box with a tappable suggested prompt. Replies are read from the JSON hark.com's own page loads. | The sign-in is in the Chrome profile. |
| **Pins** | The account that signed in first, and that account's main thread. Later commands refuse to run if another account is signed in. Also the thread sending types into, if you chose a Project. | `~/Library/Application Support/agent-runner/hark/pins.json` |

Hark keeps its threads on its servers, so nothing is lost while disconnected. The next `bench sync hark` records what arrived in the meantime.

## Threads: the main thread and Projects

Hark has one main thread (hark.com/chat) and any number of Projects (hark.com/project/…), each its own thread. Hark's memory is shared across all of them.

- **Every thread is recorded:** the main thread and each Project that isn't archived. A new Project is recorded from its first message on the next `sync`. An archived one stops being read, and what was recorded from it stays.
- **Sending types into one thread.** That's the main thread unless you choose a Project:

  ```bash
  uv run bench hark-thread
  ```

  ```bash
  uv run bench hark-thread "task-travel logistics"
  ```

  The first lists the threads and marks the one sending goes to. The second chooses a Project by its title, its ID, or the start of its ID. `uv run bench hark-thread main` goes back to the main thread.
- **The tab is moved to that thread's page before typing,** if it's showing another one. It never moves away from a message box with text in it.
- **In recordings,** a Project's messages carry its ID: `project` in the inbox, and `thread: "hark:project:<id>"` in `data/conversations/hark.jsonl`. `bench listen` shows them with `[project <id>]`.
- **In a run's transcript,** a message from another thread is labelled, like "in the main thread". Hark tends to send its unprompted messages in the main thread.

## Set up for the first time

Do [Before you start](README.md#before-you-start) first. You need a Google account to sign in to Hark with, Google Chrome on the Mac, and about 10 minutes. The free plan is enough. It allows 8M Hark tokens a day and 40M a month.

**A card is connected, so keep Hark asking first.** Hark places real orders with the card in Settings → Connected cards, and during its free-pizza bug it charged users in full. It asks before it spends unless told otherwise. Never give it a standing permission for a kind of purchase, which would cover every purchase of that kind, and keep auto top-up off. See [INTEGRATIONS_PLAN.md](../../../INTEGRATIONS_PLAN.md#hark).

1. In `config/agents.yaml`, under `agents:`, add Hark. Its handle is always `hark.com`, which means whichever account is signed in there in the dedicated Chrome:

   ```yaml
   hark:
     channel: hark
     handle: "hark.com"
     quiet_period_seconds: 180   # Hark keeps working in its browser after it first replies
     max_wait_seconds: 1800
   ```

2. Sign in. This starts the [dedicated Chrome](dedicated-chrome.md) if it isn't running, opens hark.com/chat, and waits up to 10 minutes while you sign in:

   ```bash
   uv run bench hark-login
   ```

3. In that Chrome window, sign in, or sign up, with your Google account:
   - Use your password or your phone. Passkeys saved in your everyday Chrome or Keychain aren't offered in this profile.
   - If Chrome asks "Sign in to Chrome?", choose **Use Chrome without an account**.
   - On a new account, finish Hark's onboarding until the chat opens. When it asks to connect Google (Gmail, Drive, Calendar, Contacts), connect the same Google account as the other agents. The benchmark uses its Gmail and Calendar.
4. It ends with `Signed in to Hark as <email> in the dedicated Chrome.` This pins that account and its main thread. If it says `this Hark account has no chat thread yet`, open hark.com/chat once in the dedicated Chrome and run `hark-login` again.
5. Check everything, without sending anything:

   ```bash
   uv run bench hark-status
   ```

6. **Only if you have Hark runs from before this channel existed:** import each one now, with the times it was observed. The first `send` or `sync` would otherwise record those messages with the time it syncs.

   ```bash
   uv run bench hark-import runs/<folder>
   ```

7. Send a first message and watch for the reply:

   ```bash
   uv run bench send hark "ping" --listen 120
   ```

   It fills Hark's message box, checks the text, and clicks Send. While Hark thinks or browses, that's recorded as a "working" or "browsing" stretch.

## Check it

```bash
uv run bench hark-status
```

It starts the dedicated Chrome if it isn't running. It sends nothing and records nothing. Healthy:

```
Hark: signed in as <your Google email> (<id>)
Main thread: <thread id> (idle)
Project "<title>": <thread id> (idle)
Sending to: the main thread (the Hark tab is showing it)
Message box: found, empty
Hark tokens: 100,613 of 8,000,000 today; 100,613 of 40,000,000 this period (resets …)
```

## If something's wrong

| You see | What it means | Fix |
|---|---|---|
| `nobody is signed in to hark.com in the dedicated Chrome` or `hark.com says nobody is signed in` | Hark's sign-in expired, or you signed out. | [Sign back in](#sign-back-in) |
| `hark.com is signed in as A, but agent-runner is pinned to B`, or `NOT READY: agent-runner is pinned to B` | Another account is signed in to hark.com in the dedicated Chrome. | Sign out on hark.com and sign in as B. If the benchmark really moved to a new account, [re-pin](#re-pin-the-account-or-thread). |
| `Message box: has text in it`, or `Hark's message box already has text in it` | Something was left in the box. | On hark.com/chat in the dedicated Chrome, select the text in the box and delete it. |
| `found N message boxes on …, not one` | The tab isn't showing the chat, usually because it's on a sign-in page. | [Sign back in](#sign-back-in). If you're signed in and it still fails, Hark changed its page. Update `Selectors` in [bench/channels/hark.py](../../bench/channels/hark.py). |
| `Hark's Send button didn't appear`, `clicked Send, but Hark's message box didn't empty` or `…didn't show up in … within 30s` | The send may not have gone through, or it went to another thread. | Look at hark.com/chat in the dedicated Chrome. Hark names its Send button in the page's language, and the runner knows only the English and Chinese names. If the page shows another language, add that name to `Selectors`. If Hark changed its page, update `Selectors` too. |
| `couldn't read /api/… from the Hark tab`, `hark.com answered … with HTTP …` or `… isn't JSON` | The tab was reloading, or hark.com had a hiccup. `listen` retries these by itself. | Run the command again. If one keeps failing on `/api/messages/history`, check that the pinned thread still exists ([re-pin](#re-pin-the-account-or-thread)). |
| `the Project agent-runner sends to (…) is archived or gone` | The Project chosen with `hark-thread` was archived or deleted on Hark. Nothing was sent. | `uv run bench hark-thread` to list the threads, then choose one, or `main`. |
| `this Hark account has no chat thread yet` | The account has never opened the chat. | Open hark.com/chat once in the dedicated Chrome. |
| `the dedicated Chrome isn't running…`, `started Chrome, but its debugging port didn't answer…` or `couldn't attach…` | Chrome trouble. | [Dedicated Chrome](dedicated-chrome.md#if-something-goes-wrong) |
| `another bench command is using Hark; wait for it to finish` | Another `bench` command is using Hark. | Let it finish, or stop it with Ctrl-C. |

## Sign back in

1. Run this. It starts the dedicated Chrome if needed, opens hark.com/chat, and waits up to 10 minutes while you sign in:

   ```bash
   uv run bench hark-login
   ```

2. Sign in with your Google account, using your password or your phone. Passkeys saved in your normal Chrome or Keychain aren't offered in this profile.
3. If Chrome asks "Sign in to Chrome?", choose **Use Chrome without an account**. This profile shouldn't sync your browser data.
4. It ends with `Signed in to Hark as <email> in the dedicated Chrome.` Then check it:

   ```bash
   uv run bench hark-status
   ```

## Re-pin the account or thread

Do this only if the benchmark really moved to another Hark account, or if the pinned main thread is gone. Recorded messages are kept.

1. Delete the pins:

   ```bash
   rm "$HOME/Library/Application Support/agent-runner/hark/pins.json"
   ```

2. With the right account signed in, pin it again. This pins the signed-in account and its oldest thread that isn't archived:

   ```bash
   uv run bench hark-login
   ```

3. Check that the `Main thread:` that `hark-status` shows is the one you chat in at hark.com/chat. Projects aren't pinned: they're recorded anyway. To send in one, use `bench hark-thread` ([Threads](#threads-the-main-thread-and-projects)).

## Keeping it signed in

- Leave the Hark tab on hark.com. The runner opens a new tab on hark.com/chat if there isn't one.
- Don't press buttons on Hark's messages (connect, approve, book) on the benchmark's behalf. The runner records them and never presses them.
- Google (Gmail and Calendar) and a card are connected in Hark's settings, under Connected accounts and logins and Connected cards. If Hark asks to reconnect one, do it there yourself. It isn't a runner connection problem.
