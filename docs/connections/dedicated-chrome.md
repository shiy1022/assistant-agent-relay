# The dedicated Chrome

Back to [all connections](README.md). Used by [Dots](dots-slack.md) (sending only) and [Hark](hark.md).

## What it is

- A real Google Chrome with its own profile at `~/Library/Application Support/agent-runner/chrome` and a debugging port on `127.0.0.1:9222`. Playwright attaches to it and never launches a browser of its own, so sign-ins behave as in a normal browser and stay in the profile across restarts.
- It runs as a **second Chrome app** next to your everyday one, with its own Dock icon. It isn't signed in to Chrome and doesn't sync.
- It's signed in to PJTTY's Slack and to Hark (with your Google account), each once by hand.
- Detaching leaves Chrome running with its tabs as they were.

The code is in [bench/channels/chrome.py](../../bench/channels/chrome.py).

## Check it

```bash
curl -s http://127.0.0.1:9222/json/version
```

If it's up, this prints JSON with `"Browser": "Chrome/…"`. Nothing at all means it isn't running with its debugging port.

## Start it

Either let a Hark command start it, which also checks Hark:

```bash
uv run bench hark-status
```

Or start it yourself:

```bash
open -na "Google Chrome" --args --user-data-dir="$HOME/Library/Application Support/agent-runner/chrome" --remote-debugging-port=9222 --no-first-run --no-default-browser-check --disable-background-timer-throttling --disable-backgrounding-occluded-windows --disable-renderer-backgrounding
```

Hark commands start it when it isn't running. Slack commands don't, and say so. After a Mac restart, start it before you use Dots.

The last three flags stop Chrome from pausing tabs you aren't looking at, which would delay or drop updates. Always start it with the full command.

## If something goes wrong

| You see | What it means | Fix |
|---|---|---|
| `the dedicated Chrome isn't running with its debugging port` | It isn't running. | [Start it](#start-it) |
| `started Chrome, but its debugging port didn't answer within 20s` | A Chrome is already open on this profile **without** the port, for example one reopened from the Dock or restored after a crash. | Quit the dedicated Chrome (right-click its Dock icon → Quit, not your everyday Chrome), then [start it](#start-it) with the command. |
| `couldn't reach the dedicated Chrome at http://127.0.0.1:9222` | Something else may be using port 9222. | See what's listening with the command below. If it isn't this Chrome, quit it, then start Chrome again. |
| `couldn't attach … Browser context management is not supported` | Chrome has no window open. The runner opens one by itself, so this should be rare. | Open any tab in the dedicated Chrome, then run the command again. |
| Replies arrive late only while Chrome is in the background | It was started without the anti-throttling flags. | Quit it and start it with the full command. |
| Signed out of Slack or Hark | The site ended the session. | `uv run bench slack-login` or `uv run bench hark-login` |

To see what's listening on the port:

```bash
lsof -nP -iTCP:9222 -sTCP:LISTEN
```

## Rules for this Chrome

- Choose **Use Chrome without an account** whenever Chrome asks you to sign in to Chrome.
- Passkeys saved in your everyday Chrome or Keychain aren't available here. Sign in with a password or your phone.
- Tabs can stay open or be closed. The runner reuses a tab on the right page or opens a new one.
- Leave the message boxes in the Slack DM and on hark.com empty. A leftover draft makes the next send refuse to run.
