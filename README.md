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

## Tests

```bash
uv run pytest
```

The tests build a test version of `bench-reader` and run it against a fake `chat.db`. They never touch your real messages.
