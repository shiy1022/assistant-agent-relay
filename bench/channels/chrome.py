"""The dedicated Chrome that the web channels drive.

A real Google Chrome with its own profile and a debugging port. Playwright
attaches to it instead of launching its own browser, so sign-ins behave like a
normal browser's and stay in the profile. You sign in to each site by hand,
once. Shared by the Hark channel and, if it switches over, SlackWeb.

Self-contained: it needs only Playwright and the standard library.

    from bench.channels.chrome import attach
    with attach() as browser:   # starts Chrome first if it isn't running
        pages = browser.contexts[0].pages

Leaving the with-block detaches. Chrome keeps running with its tabs as they were.
"""

import json
import subprocess
import time
import urllib.parse
import urllib.request
from contextlib import contextmanager
from pathlib import Path

__all__ = ["CDP_URL", "CHROME_COMMAND", "ChromeError", "attach", "ensure_window", "is_running", "start"]

CDP_URL = "http://127.0.0.1:9222"
PROFILE_DIR = Path.home() / "Library" / "Application Support" / "agent-runner" / "chrome"
# The last three stop Chrome from pausing tabs you aren't looking at, which would delay or drop updates.
FLAGS = ("--no-first-run", "--no-default-browser-check", "--disable-background-timer-throttling",
         "--disable-backgrounding-occluded-windows", "--disable-renderer-backgrounding")
CHROME_COMMAND = ('open -na "Google Chrome" --args --user-data-dir="$HOME/Library/Application Support/agent-runner/chrome" '
                  "--remote-debugging-port=9222 " + " ".join(FLAGS))


class ChromeError(Exception):
    pass


def is_running(cdp_url: str = CDP_URL) -> bool:
    try:
        with urllib.request.urlopen(f"{cdp_url}/json/version", timeout=2):
            return True
    except OSError:
        return False


def start(cdp_url: str = CDP_URL, url: str | None = None, seconds: float = 20) -> None:
    """Starts the dedicated Chrome with its debugging port, and waits until it answers."""
    port = urllib.parse.urlsplit(cdp_url).port or 9222
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    PROFILE_DIR.chmod(0o700)
    subprocess.run(["open", "-na", "Google Chrome", "--args", f"--user-data-dir={PROFILE_DIR}",
                    f"--remote-debugging-port={port}", *FLAGS, *([url] if url else [])], check=True)
    deadline = time.monotonic() + seconds
    while not is_running(cdp_url):
        if time.monotonic() > deadline:
            raise ChromeError(f"started Chrome, but its debugging port didn't answer within {seconds:.0f}s. "
                              f"Quit that Chrome window and start it with:\n  {CHROME_COMMAND}")
        time.sleep(0.5)


def ensure_window(cdp_url: str = CDP_URL) -> None:
    """Chrome 156 can't be attached to while it has no window, so one is opened if needed."""
    with urllib.request.urlopen(f"{cdp_url}/json/list", timeout=5) as r:
        targets = json.load(r)
    if not any(t.get("type") == "page" for t in targets):
        urllib.request.urlopen(urllib.request.Request(f"{cdp_url}/json/new?about:blank", method="PUT"), timeout=5).close()


@contextmanager
def attach(cdp_url: str = CDP_URL, *, start_if_needed: bool = True):
    """Playwright's handle on the dedicated Chrome, starting Chrome first if it isn't running."""
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import sync_playwright

    if not is_running(cdp_url):
        if not start_if_needed:
            raise ChromeError(f"the dedicated Chrome isn't running with its debugging port. Start it with:\n  {CHROME_COMMAND}")
        start(cdp_url)
    try:
        ensure_window(cdp_url)
    except OSError as e:
        raise ChromeError(f"couldn't reach the dedicated Chrome at {cdp_url}: {e}") from None
    playwright = sync_playwright().start()
    try:
        try:
            browser = playwright.chromium.connect_over_cdp(cdp_url)
        except PlaywrightError as e:
            raise ChromeError(f"couldn't attach to the dedicated Chrome at {cdp_url}: {e}") from None
        yield browser
    finally:
        playwright.stop()  # detaches; Chrome and its tabs stay as they are
