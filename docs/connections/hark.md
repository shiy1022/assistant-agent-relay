# Hark on hark.com

Back to [all connections](README.md). Background on how this route was found: [HARK_CONNECTION.md](../../../HARK_CONNECTION.md).

## How it's connected

Hark has no API, Mac app or number to text, so there's only one link: hark.com itself.

| Link | What it is | Where it lives |
|---|---|---|
| **Ours** | hark.com in the [dedicated Chrome](dedicated-chrome.md), signed in with your Google account. A fixed script fills Hark's message box, checks the text, and clicks Send. It never clicks the box itself, because Hark covers the empty box with a tappable suggested prompt. Replies are read from the JSON hark.com's own page loads. | The sign-in is in the Chrome profile. |
| **Pins** | The account that signed in first, and that account's main thread. Later commands refuse to run if another account is signed in. | `~/Library/Application Support/agent-runner/hark/pins.json` |

Hark keeps the thread on its servers, so nothing is lost while disconnected. The next `bench sync hark` records what arrived in the meantime.

## Set up for the first time

Do [Before you start](README.md#before-you-start) first. You need a Google account to sign in to Hark with, Google Chrome on the Mac, and about 10 minutes. The free plan is enough. It allows 8M Hark tokens a day and 40M a month.

**No card on the account.** Hark places real orders with a connected card, so the benchmark account must never have one.

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
   - On a new account, finish Hark's onboarding until the chat opens. When it asks to connect Google (Gmail, Drive, Calendar, Contacts), skip it. Nothing is connected for now.
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

   It fills Hark's message box, checks the text, and clicks Send. While Hark thinks or browses, that's recorded as a "working" or "browsing" stretch. When Hark builds a Page (a website), it's recorded as a message, `Page: <name>` with its `hark.com/site/<project>` link, and a copy of it, one HTML file with its images and fonts embedded, is saved under `data/inbox/hark/attachments/`, since the link only opens signed in to Hark.

## Check it

```bash
uv run bench hark-status
```

It starts the dedicated Chrome if it isn't running. It sends nothing and records nothing. Healthy:

```
Hark: signed in as <your Google email> (<id>)
Main thread: <thread id> (idle)
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
| `Hark's Send button didn't appear`, `clicked Send, but Hark's message box didn't empty` or `…didn't show up in Hark's thread within 30s` | The send may not have gone through. | Look at hark.com/chat in the dedicated Chrome. Hark names its Send button in the page's language, and the runner knows only the English and Chinese names. If the page shows another language, add that name to `Selectors`. If Hark changed its page, update `Selectors` too. |
| `couldn't read /api/… from the Hark tab`, `hark.com answered … with HTTP …` or `… isn't JSON` | The tab was reloading, or hark.com had a hiccup. `listen` retries these by itself. | Run the command again. If one keeps failing on `/api/messages/history`, check that the pinned thread still exists ([re-pin](#re-pin-the-account-or-thread)). |
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

3. Check that the `Main thread:` that `hark-status` shows is the one you chat in at hark.com/chat. Projects are separate threads, and none of them should be pinned.

## Keeping it signed in

- Leave the Hark tab on hark.com. The runner opens a new tab on hark.com/chat if there isn't one.
- Don't press buttons on Hark's messages (connect, approve, book) on the benchmark's behalf. The runner records them and never presses them.
- Hark keeps asking to connect Google (Gmail, Drive, Calendar, Contacts). Nothing is connected for now. Connecting it is a benchmark decision, not a fix for a connection problem.
