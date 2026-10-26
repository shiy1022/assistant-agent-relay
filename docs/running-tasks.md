# Running the tasks

How to run an Assistant Bench task against an agent, step by step, and record it. Set up and check the agent's connection first: [Connections](connections/README.md). Run every command from the repo's root.

## The task sheet

The task sheet, `Assistant Bench 2.0 Tasks vLIVE.xlsx`, is kept out of this repo on purpose. It has each task's success criteria and scoring, and the agents being tested search the web. Get the current copy from the team and put it in `tasks/`, which is gitignored.

Never commit it, and never paste its prompts or criteria anywhere public: an issue, a PR, a commit message, a run you share.

- The **Assignment** tab says which tester runs which agents.
- Every other tab is one task. Each row is one step, in order. Columns shift a little between tabs, so go by the headers:

| Column | What it is |
|---|---|
| A | The step, like `DAY 1 CHECKPOINT` or `DAY 2 LEARNING` |
| C | Setup: what to do before sending |
| D | The exact prompt to send, or `NO PROMPT` |
| E | Success criteria |
| After that | Each scoring category: its rubric, then a Score + Comments column for each of Agents 1–4 |

## Which tasks can run now

The runner talks to each agent one to one and nothing else is connected to the agents yet, so:

- **Run now:** tasks whose setup needs no connected account, like Travel.
- **Not yet:** tasks whose setup connects email, calendar, a bank or a store account to the agent. Connecting those is still a TODO. Until then, the rule in [Connections](connections/README.md#before-you-start) stands: connect nothing else to any agent.
- **By hand:** tasks set in a group chat, like Group Dinner. The runner never reads or sends in group chats.

No agent has a reset that's passed a full check yet. What's known so far is in [Resetting an agent's context](context-reset.md). Until then, in the `run.json` of a task's first run, note what the agent had already been told, if anything. Notes added by hand are kept.

## Run a step

1. Do the step's setup (column C) yourself.
2. Copy the prompt (column D) into a file, like `prompt.txt`, with a text editor. It's sent exactly as written, so fill in any placeholder like `[name]` first, and change nothing else.
3. Send it and record the reply:

   ```bash
   uv run bench run wajo --file prompt.txt --task "Travel vF Golden" --step "DAY 1 CHECKPOINT" --cell D2
   ```

   `--task` is the tab's name, `--step` is column A, and `--cell` is the cell the prompt came from. They're saved in the run's `run.json`, so every run can be matched to its row.
4. It records until the agent has answered and gone quiet for its `quiet_period_seconds` (see `config/agents.yaml`), then writes `runs/<time>-<task>-<agent>/`. Ctrl-C stops early and still writes it.

Run each step on its day, in the same conversation as the steps before it. Follow-ups a tester writes, like those that start `TESTERS:` or depend on the last reply, are sent the same way, with the `--cell` of the row they belong to.

## Steps with no prompt

A step that says `NO PROMPT` checks whether the agent acts on its own, like sending a digest or flagging a conflict. Record whatever arrives:

```bash
uv run bench sync wajo
```

Telegram, Slack and Hark keep the whole chat, so syncing once after the step is enough. WhatsApp records all the time, so there's nothing to sync. These messages go to `data/conversations/<agent>.jsonl`, not to a run folder.

## Score it

Read the run's `transcript.md`, and anything the agent made in `deliverables/`, against the success criteria. Put the scores in the sheet, in that agent's columns. To see every recorded conversation at once:

```bash
uv run bench dashboard --open
```

If messages arrived after a run ended, add them to its transcript first:

```bash
uv run bench run-rebuild runs/<run folder>
```
