# Muse and Instinct on WhatsApp

Back to [all connections](README.md).

## How it's connected

| Link | What it is | Where it lives |
|---|---|---|
| **Ours** | `bench-whatsapp` (Go, whatsmeow) is a linked device on your own WhatsApp number, listed under WhatsApp → Settings → Linked Devices on your phone. It runs all the time as a LaunchAgent, starts when you log in to the Mac, and restarts itself 30 s after a crash. `bench` talks to it through a local socket. It records and messages only the agents in its `agents.json`, which `bench` writes from `config/agents.yaml`. | `~/Library/Application Support/agent-runner/whatsapp/`: `session.db` (the device's keys), `agents.json`, `inbox/`, `status.json`, `bench.sock`. LaunchAgent `local.personal-agent-benchmark.bench-whatsapp`. Log: `~/Library/Logs/bench-whatsapp.log`. |
| **Theirs: Muse** | Meta's bot inside WhatsApp, at `1807055946647697@bot`. | `muse: handle:` in `config/agents.yaml` |
| **Theirs: Instinct** | Not set up yet. The handle stays blank until you have access. See [Instinct](#instinct-once-you-have-access). | `instinct: handle:` |

**What's lost while disconnected:**
- **Offline but still linked** (Mac asleep, network down, bridge stopped): nothing. WhatsApp holds messages for the device and delivers them when it reconnects.
- **Unlinked:** messages from the gap aren't recorded. A newly linked device doesn't get them, and the bridge drops WhatsApp's history copy on purpose. Read them on your phone if they matter.

## Set up for the first time

Do [Before you start](README.md#before-you-start) first. You need your iPhone with WhatsApp on your own number, a Meta account for Muse, Go on the Mac, and about 15 minutes.

**Know the risk first.** bench-whatsapp is an unofficial client on your own number. It can see your whole account, and WhatsApp can ban numbers that use unofficial clients. This was accepted for the benchmark. To keep the risk low, the bridge messages only the agents, stays human-paced, and never sends read receipts. Stop at the first warning from WhatsApp, and unlink it under Linked Devices if anything looks wrong.

### On your iPhone: Muse in WhatsApp

1. Update WhatsApp from the App Store. (muse.ai in Safari can't connect WhatsApp.)
2. In WhatsApp, on the **Chats** tab, tap the **Muse icon**, a small hooded character with a smiling face, or **Try Muse** if a message offers it.
3. Check that the Meta account shown is the one you use for Muse. If it isn't, tap **Use another account**. Follow the prompts and tap **Get started**, and the chat with Muse opens.
   - **No Muse icon?** In the Muse app, sign in and send "Connect my WhatsApp account", then tap **Connect WhatsApp → Connect**. It also works from Settings → **Messaging Channels** → WhatsApp.
4. Send Muse "hi" and wait for a reply.
5. In Muse's settings, set approvals to **ask before sending or buying**. Connect nothing else to Muse for now: no email, calendar or payment.

### On the Mac: build and install the bridge

6. Build `bench-whatsapp` and install it. This needs Go. It installs to `~/Library/Application Support/agent-runner/whatsapp/` and writes the LaunchAgent, but the bridge doesn't start until it's linked.

   ```bash
   uv run bench whatsapp-install
   ```

7. In `config/agents.yaml`, under `agents:`, put Muse's address. Muse is a Meta bot, so its address is a bot ID, not a phone number. It's the same for everyone (whatsmeow knows it as `MuseJID`):

   ```yaml
   muse:
     channel: whatsapp
     handle: "1807055946647697@bot"
     quiet_period_seconds: 180   # Muse streams each answer by editing one message
     max_wait_seconds: 1800
   ```

### Together: link the bridge to your WhatsApp

8. Run:

   ```bash
   uv run bench whatsapp-link
   ```

   A QR code appears in the terminal. On your iPhone: WhatsApp → Settings → **Linked Devices** → **Link a Device**, then scan it. The code refreshes every 20–30 seconds, so scan the newest one. It ends with `Linked to WhatsApp as +1…, and bench-whatsapp is running in the background.` WhatsApp then offers the new device a copy of your history, and the bridge drops all of it.

9. Check it. The last line should list `agents: muse`:

   ```bash
   uv run bench whatsapp-status
   ```

10. **If the address is in doubt,** have the bridge find it. Run this, then message Muse from your iPhone and wait for its reply. It prints the address of a chat whose sender name matches, and keeps no message content:

    ```bash
    uv run bench whatsapp-find Muse
    ```

11. Send a first message and watch for the reply. Muse streams its answer, so the reply shows up several times as it grows:

    ```bash
    uv run bench send muse "hi" --listen 120
    ```

    On your iPhone the message shows in Muse's chat as sent by you. That's expected.

12. **From now on,** open WhatsApp on your iPhone at least every 14 days, or WhatsApp logs out linked devices.

Instinct uses the same bridge, so it needs no new install or link. See [Instinct, once you have access](#instinct-once-you-have-access).

## Check it

```bash
uv run bench whatsapp-status
```

Healthy:

```
bench-whatsapp is running in the background.
WhatsApp: linked, connected, offline; agents: muse
```

"offline" is normal. The bridge shows you as offline except while a run waits for a reply.

The first line says whether the bridge process is up. The second line is what it reports: `WhatsApp: …` when it answers, or `Last status: …`, the last state it recorded, when it doesn't.

## If something's wrong

| You see | What it means | Fix |
|---|---|---|
| `not running` with `Last status: logged_out`, and a Mac notification "WhatsApp unlinked agent-runner" | WhatsApp unlinked the device. It was removed on your phone, the phone went 14 days without opening WhatsApp, or WhatsApp logged it out. | [Link again](#link-again) |
| `not running` with `Last status: not_linked` | It was never linked, or its session was deleted. | [Link again](#link-again) |
| `not running` with `Last status: stopped` or `connected`, or no last status | The bridge exited or crashed, or its job was unloaded. | [Restart the bridge](#restart-the-bridge) |
| `WhatsApp: linked, disconnected` | It lost the network and reconnects by itself. | Wait a minute. If it stays disconnected, [restart the bridge](#restart-the-bridge). |
| `WhatsApp: … replaced` | Another copy of the session connected. | [Restart the bridge](#restart-the-bridge). If it keeps happening, [start clean](#start-clean). |
| `bench-whatsapp isn't running. Link it with: uv run bench whatsapp-link` | `send` or `listen` couldn't reach the socket. | Run `whatsapp-status` first. Linked: [restart](#restart-the-bridge). Not linked: [link again](#link-again). |
| `bench-whatsapp didn't answer in time` | The bridge is stuck. | [Restart the bridge](#restart-the-bridge) |
| `bench-whatsapp isn't installed` | The binary is gone. | [Rebuild it](#rebuild-it) |
| `another bench-whatsapp is using the session` | A second copy is running, such as a link still in progress in another terminal. | Close the other one, then run the command again. |
| Your message shows as sent (`→`) but the agent never replies | Probably the agent's side. | Message it from your phone. If it doesn't answer there either, see [Muse's side](#muses-side). |

## Restart the bridge

```bash
uv run bench whatsapp-link
```

When the device is still linked, this doesn't show a QR code. It writes the agent list, restarts the bridge, and prints `Linked to WhatsApp as +1…, and bench-whatsapp is running in the background.` When it isn't linked, it shows a QR code, as below. So it's the one command to run whenever WhatsApp looks wrong.

## Link again

1. Run:

   ```bash
   uv run bench whatsapp-link
   ```

2. A QR code appears in the terminal. On your iPhone: WhatsApp → Settings → **Linked Devices** → **Link a Device**, then scan it. The code refreshes every 20–30 seconds, so scan the newest one.
3. It prints `Linked to WhatsApp as +1…`, and the bridge starts in the background.
4. Check it:

   ```bash
   uv run bench whatsapp-status
   ```

When a device is unlinked, whatsmeow deletes its keys itself, so linking again doesn't need a cleanup first. If `whatsapp-link` reports it's already linked but `whatsapp-status` says `not linked`, [start clean](#start-clean).

## Start clean

This logs the device out of WhatsApp and deletes its session (`session.db`, `state.json`, `status.json`). Recorded messages are kept.

```bash
uv run bench whatsapp-unlink
```

If it can't reach WhatsApp, remove the device on your phone under Linked Devices instead. Then [link again](#link-again).

## Rebuild it

Needed if the binary is missing, or after changing `whatsapp/*.go`. It needs Go. A bridge that's already running picks up the new build.

```bash
uv run bench whatsapp-install
```

If it was never linked, [link it](#link-again) next.

## Muse's side

If Muse doesn't answer even when you message it from your phone:

1. Update WhatsApp from the App Store.
2. In WhatsApp, on the **Chats** tab, tap the Muse icon, or **Try Muse**. Check that the Meta account shown is the one you use for Muse.
3. No Muse icon: in the Muse app, go to Settings → **Messaging Channels** → WhatsApp → Connect, or send Muse "Connect my WhatsApp account".
4. Send "hi" from your phone and wait for a reply.

If Muse answers on your phone but nothing is recorded, its address may have changed. The log would show `ignored a message in a chat with bot account …@bot, which isn't in agents.json`:

```bash
tail -20 ~/Library/Logs/bench-whatsapp.log
```

To find the new address, run this, then message Muse from your phone and wait for its reply. It keeps only the sender name and address, not message content.

```bash
uv run bench whatsapp-find Muse
```

Put the address it prints in `config/agents.yaml` under `muse: handle:`, then [restart the bridge](#restart-the-bridge) so it gets the new list.

## Instinct, once you have access

Instinct is invite-only, and whether it works over WhatsApp isn't confirmed yet. This checks it.

1. In Safari, go to [instinct.com](https://instinct.com/), enter your number and the code you receive, then join the waitlist or use an invite. Once you're in, message Instinct on WhatsApp from your phone, and set it to ask before it sends or buys anything if it offers that.
2. Find its address. Run this, then message Instinct from your phone and wait for its reply:

   ```bash
   uv run bench whatsapp-find Instinct
   ```

3. Put the printed address (a `+` phone number, or `…@lid`) in `config/agents.yaml` under `instinct: handle:`.
4. Give the bridge the new list. It's already linked, so this only restarts it:

   ```bash
   uv run bench whatsapp-link
   ```

5. `uv run bench whatsapp-status` should now end with `agents: muse, instinct`.

## Keeping it linked

- Open WhatsApp on your iPhone at least every 14 days. Otherwise WhatsApp logs out all linked devices.
- Don't remove the device under Linked Devices.
- `session.db` gives full access to your WhatsApp account. It's readable only by you, and must never be committed or shared.
