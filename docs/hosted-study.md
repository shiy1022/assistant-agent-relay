# Hosted study

The hosted study uses Supabase for email/password login, study results, and recordings.
Participants go to `/study`. Researchers go to `/research`.
The hosted site can send prompts and receive replies through the Study Connector
on a participant’s Mac. Participants can also select **Copy and paste messages by hand**.

## Database and accounts

1. In the Supabase project, enable the Data API. Apply the [study migrations](../supabase/migrations/) in order,
   either in SQL Editor or through an authenticated Supabase CLI migration.
2. In **Authentication → Users**, create an account for each invited participant and
   researcher. Give each a unique, privately delivered password. For an internal
   test with a dummy address, **Create new user** with **Auto confirm user** avoids
   sending mail. The site has no public registration form.
3. In SQL Editor, bind each account to its study role. Replace the example emails
   and participant ID with the actual values.

   ```sql
   insert into public.study_members (user_id, pid, role)
   select id, 'P001', 'participant' from auth.users
   where email = 'participant@example.com';

   insert into public.study_members (user_id, pid, role)
   select id, null, 'researcher' from auth.users
   where email = 'researcher@example.com';
   ```

The invite list is enforced in the database. Set a member’s `enabled` field to
`false` to withdraw access and retain their results. Participants can read and write their
own result files. Researchers can read all study results and recordings and
download results with the change history. The web server uses the publishable
key and each person's login session.

## Connect a Mac

From a checkout of the same version of this project, install the Study Connector
for your study’s website. Use the site’s origin without the trailing `/study`.

```sh
uv run python -m bench.study_connector --origin https://study.example.com --install
```

In Study, select **Connect an agent**, choose a channel, and click **Connect this
Mac → Open Study Connector**. Confirm the site and code on your Mac. A pairing
code lasts five minutes. To pair from Terminal, run the same command without
`--install` and enter the code displayed under **Enter the pairing code manually**.
To resume a paired connection, click **Open paired connector**. A pairing lasts
30 days, or until the participant disconnects it in Study.

The connector uses the local agents configured by the benchmark. Open channel setup to enter an agent address locally if it is missing. Keep account tokens in local configuration. For a WhatsApp alternative for an
agent already configured on another channel, use a separate name, such as
`instinct-whatsapp`, in both the local configuration and `STUDY_AGENTS`.

Click **Open channel setup** to open the macOS settings, sign-in, or linking flow
for the selected channel. Complete the channel’s permissions or sign-in on the
Mac, then click **Test connection**. The readiness check requires a reply to a
unique test message. See [channel-specific setup](connections/README.md) for
first-time account requirements. Study shows incoming replies and task status
when the paired Mac is online.

## Start the website

Set these environment variables locally or on the hosting platform:

| Variable | Value |
| --- | --- |
| `SUPABASE_URL` | Supabase Project URL |
| `SUPABASE_PUBLISHABLE_KEY` | Key starting with `sb_publishable_` |
| `STUDY_TASK` | Task name from its `run.json`, defaults to `Restaurant` |
| `STUDY_TASKS_DIR` | Path to the private task directory containing the task's `run.json` and `study.json` |
| `STUDY_AGENTS` | Comma-separated names and channels, such as `instinct:imessage` |
| `STUDY_CONNECTOR_DB` | Persistent local SQLite file for pairings and outstanding tasks, defaults to `data/study-connector.sqlite3` |
| `STUDY_ORIGIN` | The exact site origin, such as `https://study.example.com`, for remote hosting |

The task directory stays private on the host. It is not part of the public
repository. For a local run with the variables in an ignored `.env` file:

```sh
uv run --env-file .env python -m bench.hosted_study --port 8874
```

The local site opens at `http://127.0.0.1:8874/study`. Behind an HTTPS
hosting proxy, start the same service with `--bind 0.0.0.0` and let the host
set `PORT`. Run one Study instance with a persistent writable volume for the connector database. Share the hosted `/study` URL with participants and the hosted
`/research` URL with the team.

## Publish a pilot on Render

Use the included fictional [demo study](../examples/study/browser-smoke/) to
check remote connections while the team prepares the official task. Push a
branch containing both the hosted app and the connector changes to GitHub.
In Render, create a **Web Service**, connect that branch, and select **Python 3**.
Keep the repository root as the service root directory.

| Render setting | Value |
| --- | --- |
| Build command | `uv sync --python 3.12 --locked --no-dev` |
| Start command | `uv run --no-sync python -m bench.hosted_study --bind 0.0.0.0 --origin "${STUDY_ORIGIN:-$RENDER_EXTERNAL_URL}"` |
| Health check path | `/study` |
| `STUDY_TASK` | `Browser Smoke Test` |
| `STUDY_TASKS_DIR` | `examples/study` |
| `STUDY_AGENTS` | `instinct:imessage` for the first pilot |
| `SUPABASE_URL` and `SUPABASE_PUBLISHABLE_KEY` | The values from the intended Supabase project's **Connect** screen |
| `STUDY_CONNECTOR_DB` | `/var/data/study-connector.sqlite3` with a persistent disk mounted at `/var/data` |

Use a paid instance with a persistent disk to keep active tasks and device
pairings across restarts. For a disposable connection demo on a free instance,
leave `STUDY_CONNECTOR_DB` unset. Participants may need to pair again after a
restart. Study results live in Supabase in either case. Use a separate demo
participant ID for the pilot. When using a custom domain, set `STUDY_ORIGIN` to
its exact HTTPS origin and give participants the matching origin for installation.

After deployment, open `https://<service>.onrender.com/study` and verify that
a participant and a researcher can sign in at their respective addresses.
Share the participant URL and login privately. For the first remote test, have
the participant open the page on a Mac, install the connector from the same
GitHub branch, pair it with the hosted site, complete the selected channel's
sign-in and permissions, then click **Test connection**. Use **Open channel
setup** to configure a missing local address. Submit a demo result and confirm
that it appears in `/research` before sending the link to the other participants.

When the formal study arrives, give the host the private task directory and
set `STUDY_TASKS_DIR` and `STUDY_TASK` accordingly.

For a smoke test, complete a fictional study day, click **Submit results**, and
reload the page. Sign in separately as a researcher and confirm the participant
shows **Submitted day 1** and that the saved answers and conversation are readable.

### Private, single-session task deployment

The hosted renderer supports `chat_mode: "together"`, `card_in_chat`,
`round_offset`, `step_prefix`, scale `extra` choices, configurable handoff
triggers, and `study_days: 1`. Two agents have separate timers and handoffs;
opening jobs can be queued together. The updated Mac connector processes two
agents concurrently while serializing each agent. Existing connector installs
must be updated for parallel execution.

Keep private task specifications outside this public repository. To activate one:

1. Apply `supabase/migrations/202611160001_task_scopes.sql` in Supabase.
2. Add the private specification as a Render Secret File named `study.json`.
3. Set `STUDY_SPEC_FILE=/etc/secrets/study.json`, `STUDY_TASK` to the exact `task`
   in that file, and `STUDY_RESULT_SCOPE` to a new stable slug for this protocol.
4. Deploy and verify participant login, the interview, two-agent chat, surveys,
   submission and the research view before inviting participants.

Scoped results use `tasks/<scope>/...` in Supabase. Earlier demo results remain
untouched. The active research view shows the active scope; earlier results
remain in Supabase under their original paths. Old browser tabs cannot save to
an activated scoped task until refreshed. Hosted participants cannot use the
local console's `browse=1` shortcut to bypass required answers.

For Task 1, use the private repository's
`Yueheng_Phase1_tasks/Restaurant_gathering_v1_console/study.json`, with
`study_days: 1`, `task_number: 1`, and `required_agents: 2`. Keep this file in
Render Secret Files, and use `restaurant-gathering-v1` as its result scope.
The connection dialog shows **Continue to Task 1** after a successful test.
Continuing from session setup checks both selected agents before opening the
interview; an unfinished session resumes at its current step. The participant
still completes the interview and confirms the scenario before task messages
are sent. Connection tests and practice messages remain separate from the task.

The website sends only participant messages and fixed task prompts to agents.
It does not automatically grant agents email/calendar access, run a live AI
interviewer, or perform the research team's rubric coding. Those remain separate
study operations. Do not import pilot participant records as new responses.

### Browser-to-Shortcuts setup

The production Connect agent dialog offers the shared **Study Connect** shortcut
for all supported channels. Add it once from
https://www.icloud.com/shortcuts/a055e270a7c54af4bd3f6a77cd71e0b5,
return to Study, copy a pairing code and click **Run Study setup**.
The `shortcuts://run-shortcut?name=Study%20Connect` link opens the installed
shortcut without Finder or Terminal. Adding alone does not run it; the browser
may ask to open Shortcuts. Keep the shortcut name unchanged.

First use requires **Shortcuts → Settings → Advanced → Allow Running Scripts**.
The signed `/study-setup.shortcut` download remains a fallback; the old
`/study-muse.shortcut` endpoint is an alias. The artifact is signed using
`scripts/build_study_shortcut.py` and `scripts/setup-study-shortcut.sh`, with
Apple’s `shortcuts sign --mode anyone`, not a Developer ID app signature.
Rebuilding the file does not update the public iCloud snapshot: share the new
script and update the page link too. Pairing dialogs have an explicit long
AppleEvent timeout; cancellation is separate from errors.

The pairing code records the selected agent. Redeeming it atomically queues
that agent’s setup on the server, so the connector opens its login/setup even
if the Study tab has been closed. **Open channel setup** retries. Muse, Hark and Wajo use dedicated
Chrome sign-in; Chrome must be installed. WhatsApp and Telegram display QR codes
on a temporary loopback-only browser page, with an unguessable path, Host check,
no caching and no remote assets. QR codes never go to the Study server.
WhatsApp needs Go to build the bridge (the official installer page opens if
missing). Telegram needs API credentials preconfigured by the administrator;
its optional two-step password uses a hidden native input dialog.

Dots/Slack still requires a valid user token and the correct workspace configured
on each Mac; setup validates those before browser sign-in. iMessage still needs
user-approved Full Disk Access for bench-reader. The webpage’s Test connection
restarts the reader after permission changes. Setup does not grant OS permissions
or create service credentials automatically. These requirements are surfaced as
actionable errors rather than hidden Terminal prompts.

Muse pairing/opening was previously tested on this development Mac. The shared
multi-channel flow and browser QR plumbing have automated tests; first-time
service login, actual QR linking and a successful agent reply require the relevant
participant’s account and are not established by those tests.
