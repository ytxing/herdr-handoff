---
name: handoff
description: "Use the local handoff service for a single explicit A-to-B Herdr task, including send, take, done, claim, accept, timeout reminders, and the terminal board."
---

# Handoff

Use the `handoff` entrypoint from this distribution. If it is not on `PATH`, run the
included `install-global.sh` first or invoke `./handoff` from the plugin directory.
The first version supports one unfinished task at a time: Source A sends to Target B.
Do not create parallel batches.

## Sending

The sender must explicitly provide both Herdr identities; do not infer Source:

```sh
./handoff send \
  --source-agent <source-agent> --source-pane <source-pane> \
  --target-agent <target-agent> --target-pane <target-pane> \
  --description "<short or multiline description>" \
  --prompt "<task instructions>"
```

`description` is required. The service verifies that both Agents exist through Herdr.
After `send` returns a task ID, do not poll Target or run `herdr agent wait` in the Source
turn. End the turn or continue unrelated work; the handoff daemon handles later reminders
and result notification.

## Receiving and reporting

When a task arrives, execute `take` before doing work:

```sh
./handoff take <task-id>
```

When finished, write a readable result file and run:

```sh
./handoff done <task-id> --result-file <path>
```

For a task with a handoff ID, do not use `herdr agent prompt` to return the result to
Source. `handoff done` is the only return path; the handoff service saves the result and
notifies Source. Direct Agent-to-Agent Herdr prompts bypass the task record and can cause
duplicate or untracked delivery.

If work began before `take`, use `--implicit-take` with `done`. Use `progress` only to
reset the long execution timer. Use `reject` only when explicitly refusing the task;
do not use it merely because a reminder arrived. Use `blocked` when Source must answer.

## Source review

When the result prompt arrives, run:

```sh
./handoff claim <task-id>
```

Inspect the saved result, then either accept or request changes:

```sh
./handoff accept <task-id>
./handoff request-changes <task-id> --description "<required changes>"
```

`claim` means received for review. `accept` is the final acceptance command and moves the task to `finished`.

To remove a task and its saved result permanently, run `./handoff delete <task-id>` only
when that deletion is intended.

## Daemon and board

The daemon is manual; never assume it is running:

```sh
./handoff daemon start
./handoff daemon status
./handoff ui
./handoff daemon stop
```

The board is read-only and shows the current state, required action, Herdr presence and
lifecycle, state start time, state duration, next retry, retry count, and errors.

Protocol reminders default to 30 seconds. Execution and review backoff default to 2, 4,
8 minutes and cap at 8 hours. If an expected Agent is `working`, the daemon first uses
`herdr agent wait <agent> --until idle`; that wait time is outside the backoff timer.

If a pane moves or its terminal identity disappears, the task is marked absent. Re-select
the Target and send a new task; the service does not migrate it automatically.
