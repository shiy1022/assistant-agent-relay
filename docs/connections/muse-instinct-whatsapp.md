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

Read the **second line**. `WhatsApp: …` means the bridge answered. `Last status: …` means it didn't. The first line only checks that the LaunchAgent is loaded, so it can say "running" even when the bridge has exited.

## If something's wrong

| You see | What it means | Fix |
|---|---|---|
| `Last status: logged_out`, and a Mac notification "WhatsApp unlinked agent-runner" | WhatsApp unlinked the device. It was removed on your phone, the phone went 14 days without opening WhatsApp, or WhatsApp logged it out. | [Link again](#link-again) |
| `Last status: not_linked` | It was never linked, or its session was deleted. | [Link again](#link-again) |
| `Last status: stopped` or `connected` | The bridge exited, or its job was unloaded. | [Restart the bridge](#restart-the-bridge) |
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

1. Get in at [instinct.com](https://instinct.com/), then message Instinct on WhatsApp from your phone.
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
