"""Sends iMessages through the Messages app."""

import subprocess
from datetime import UTC, datetime

from bench.config import Agent

# The handle and text arrive as argv, so nothing in a prompt can change the
# script. If Messages can't address the handle as a participant, fall back to
# an existing 1:1 chat with it.
APPLESCRIPT = """
on run argv
    set theHandle to item 1 of argv
    set theText to item 2 of argv
    tell application "Messages"
        try
            set theAccount to 1st account whose service type = iMessage
            send theText to participant theHandle of theAccount
        on error errMsg
            set theChats to (every chat whose id ends with (";-;" & theHandle))
            if (count of theChats) = 0 then error "no iMessage chat with " & theHandle & " (" & errMsg & ")"
            send theText to item 1 of theChats
        end try
    end tell
end run
"""


class SendError(Exception):
    pass


def send(agent: Agent, text: str, timeout: float = 30) -> datetime:
    """Sends text to the agent and returns when the send was requested (UTC).

    The sent and delivered times recorded for the run come from bench-reader,
    which reads them from Messages itself.
    """
    if not text.strip():
        raise SendError("refusing to send an empty message")
    requested_at = datetime.now(UTC)
    try:
        result = subprocess.run(
            ["osascript", "-e", APPLESCRIPT, agent.handle, text],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise SendError(f"Messages didn't respond within {timeout:.0f}s") from None
    if result.returncode != 0:
        raise SendError(result.stderr.strip() or f"osascript exited with {result.returncode}")
    return requested_at
