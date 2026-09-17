---
name: herdr-task-handoff
description: "Use the local Herdr task handoff service for one explicit A-to-B task, including delivery, completion, reminders, and the terminal board."
---

# Herdr Task Handoff

Use the `handoff` command after installation. If it is not available, run the
installation script in this package and try again.
One Target holds at most one unfinished task at a time. Different Targets are independent,
so concurrent handoffs are fine as long as they are not aimed at the same pane.

## Confirm the Herdr pane first

Use the exact `pane_id` returned by Herdr. Do not shorten it or type only the pane suffix.
Before sending or updating a task, confirm the pane with:

```sh
herdr pane current --current
herdr pane list --workspace <workspace-id>
```

Copy the complete value, such as `wA:p28`, into `handoff --source-pane`,
`--target-pane`, or `--pane`. A value such as `p28` is incomplete and will be shown as
absent even when Herdr has a live pane named `wA:p28`.

## Sending

The sender must explicitly provide both Pane IDs:

```sh
./handoff send \
  --source-pane <source-pane> \
  --target-pane <target-pane> \
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
./handoff take <task-id> --pane <your-pane>
```

When finished, write a readable result file and run:

```sh
./handoff done <task-id> --result-file <path> --pane <your-pane>
```

For a task with a handoff ID, do not use `herdr agent prompt` to return the result to
Source. `handoff done` is the only return path; the handoff service saves the result and
notifies Source. Direct Agent-to-Agent Herdr prompts bypass the task record and can cause
duplicate or untracked delivery.

If work began before `take`, use `--implicit-take` with `done`. Use `progress` only to
reset the long execution timer. Use `reject` only when explicitly refusing the task; do not
use it merely because a reminder arrived.

A task that needs an answer from Source is not a separate protocol state: submit what you
have with `done` and put the question in the result file. Source reads it and sends a
follow-up task. There is no blocked/reply round trip, so `active` always means exactly one
thing -- the Target has taken the task and is working on it.

## Source review

When the result prompt arrives, run:

```sh
./handoff claim <task-id> --pane <your-pane>
```

Inspect the saved result, then mark the task finished:

```sh
./handoff claim <task-id> --pane <your-pane>
```

`claim` records the Source review and moves the task to `finished`. If changes are needed, send a new task.

To remove a task and its saved result permanently, run `./handoff delete <task-id>` only
when that deletion is intended.

To remove a state class or all tasks, use `./handoff delete --state <state>` or
`./handoff delete --all`.

## Daemon and board

The daemon is manual; never assume it is running:

```sh
./handoff daemon start
./handoff daemon status
./handoff ui
./handoff daemon stop
```

The board shows the current state, required action, per-agent Herdr status for both ends,
`START` (task start), `PREV` (previous node start; the first node falls back to `START`), state start time,
state duration, next retry, retry count, and errors. Active tasks appear first; each group is ordered by
the newest current node start time.
The task rows have a muted right-edge scrollbar when rendered.

The board is interactive and can act on tasks, not just display them:

| Key | Action |
|---|---|
| `↓` / `↑`, `j` / `k` | move the cursor |
| `space` | toggle the cursor row's checkbox |
| `a` | select all / none |
| `r` | re-send the stored prompt to the Targets of the checked tasks |
| `d` | delete the checked tasks — asks for confirmation first |
| `y`, `Enter` | confirm the pending delete; any other key cancels it |
| `t` | start or stop the daemon, after a confirmation |
| `q`, `Ctrl-C` | quit |

`r` re-sends only a task still open as `published` or `active`, and only to a Target whose
Herdr status is `idle` or `done` (Herdr reports both as ready for input). Everything else is
skipped and the board says why on its own line above the key legend. Herdr itself does not
refuse a prompt to a busy agent, so this check is what keeps the board from interrupting work
already in progress. A re-send is headed `[HANDOFF TASK — RE-SENT]` and names the state the
task is still in, so the Target can tell a nudge from a new task.

## Daemon single-instance guarantee

Enforced by a file lock (`daemon.lock`), not by the pid file. A pid file outlives its process:
`kill -9` leaves one behind naming something that no longer exists, and nothing in user space
can tell that from a live daemon -- which is how a running daemon came to be shown as stopped.
The kernel releases the lock when its holder dies, so it cannot go stale. The pid file stays,
but only as a convenience for humans.

- `daemon start` refuses when another daemon holds the lock, instead of running a second one
  that would overwrite the record and double-deliver every reminder
- `daemon status` applies the same check as the board, so the two cannot disagree
- `daemon stop` waits for the lock to actually free before reporting success, and says
  `no daemon is running` when there is nothing to stop
- `t` on the board asks for confirmation first

## Board states

The STATE column prints the state name verbatim -- `published`, `active`, `result_ready`,
`finished`, `rejected`, `cancelled`, `timeout`, `*_absent` -- and colours each one. That is
the same word `handoff delete --state <name>` takes, so what the board shows can be pasted
into a command. Earlier only `result_ready` was prettified to "result ready", which made the
one that differed the hardest to match against anything.

Protocol reminders default to 30 seconds. Execution and review backoff default to 2, 4,
8 minutes and cap at 8 hours. If an expected Agent is `working`, the daemon first uses
`herdr agent wait <agent> --until idle`; that wait time is outside the backoff timer.

If a pane moves or its terminal identity disappears, the task is marked absent. Re-select
the Target and send a new task; the service does not migrate it automatically.

## Closed tasks cannot be advanced

A command that would move a closed task (`finished`, `rejected`, `cancelled`, `timeout`,
`*_absent`) back into the live set is refused, with a non-zero exit and a message naming the
current state. This covers `take`, `progress`, `done`, `done-implicit` and `reject`. Without the guard, an agent obeying a stale reminder would set a finished task back
to `active`, redo the work, overwrite the saved result and notify the Source a second time.

`claim` is deliberately idempotent: it lands on `finished`, so re-running it is a
harmless retry rather than a resurrection.
