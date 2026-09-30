---
name: herdr-task-handoff
description: "Use the local Herdr task handoff service for explicit A→B→A tasks only: source delegates, target returns a result, and source reviews/claims it. Includes delivery, completion, reminders, and the terminal board."
---

# Herdr Task Handoff

Use this skill only for **A→B→A** handoffs: Source delegates, Target works and
returns a result via `handoff done`, and Source reviews/claims it via `handoff claim`.

Do **not** use for one-way fire-and-forget tasks where the Target works
independently and the Source does not need to review the result. In that case,
use `herdr agent prompt <target> "..."` directly, or do the work yourself.

Use the `handoff` command after installation. If it is not available, run the
installation script in this package and try again.
One Target holds at most one unfinished task at a time. Different Targets are independent,
so concurrent handoffs are fine as long as they are not aimed at the same pane.

## Your pane, and the Target's pane

Every command that names your own end must use the pane you are in. Herdr hands that pane to
this shell, so it needs no lookup and no retyping:

```sh
printf '%s\n' "$HERDR_PANE_ID"
```

The commands take it directly, as `"$HERDR_PANE_ID"`:

```sh
./handoff send  --source-pane "$HERDR_PANE_ID" --target-pane <target-pane> ...
./handoff take  <task-id> --pane "$HERDR_PANE_ID"
./handoff done  <task-id> --result-file <path> --pane "$HERDR_PANE_ID"
./handoff claim <task-id> --pane "$HERDR_PANE_ID"
```

`take`, `done` and `claim` refuse a `--pane` that is not the pane the command runs in, and a
`--pane` that is not the pane this task's end is recorded as.

The Target's pane is the one value that has to be read from Herdr, and copied whole --
`wA:p28`, never `p28`:

```sh
herdr pane list --workspace <workspace-id>
```

A shortened value is not a shorthand for the same pane: the service shows it as absent even
when Herdr has a live pane named `wA:p28`.

## Sending

The sender provides both Pane IDs, its own from `$HERDR_PANE_ID`:

```sh
./handoff send \
  --source-pane "$HERDR_PANE_ID" \
  --target-pane <target-pane> \
  --description "<short or multiline description>" \
  --prompt "<task instructions>"
```

`description` is required. The service verifies through Herdr that both Agents exist, and
that `--source-pane` is the pane the command runs in.
After `send` returns a task ID, do not poll Target or run `herdr agent wait` in the Source
turn. End the turn or continue unrelated work; the handoff daemon handles later reminders
and result notification.

## Receiving and reporting

When a task arrives, execute `take` before doing work:

```sh
./handoff take <task-id> --pane "$HERDR_PANE_ID"
```

When finished, write a readable result file and run:

```sh
./handoff done <task-id> --result-file <path> --pane "$HERDR_PANE_ID"
```

For a task with a handoff ID, do not use `herdr agent prompt` to return the result to
Source. `handoff done` is the only return path; the handoff service saves the result and
notifies Source. Direct Agent-to-Agent Herdr prompts bypass the task record and can cause
duplicate or untracked delivery.

If work began before `take`, use `--implicit-take` with `done`. Use `reject` only when
explicitly refusing the task; do not use it merely because a reminder arrived.

A task that needs an answer from Source is not a separate protocol state: submit what you
have with `done` and put the question in the result file. Source reads it and sends a
follow-up task. There is no blocked/reply round trip, so `active` always means exactly one
thing -- the Target has taken the task and is working on it.

## Source review

When the result prompt arrives, run:

```sh
./handoff claim <task-id> --pane "$HERDR_PANE_ID"
```

Inspect the saved result, then mark the task finished:

```sh
./handoff claim <task-id> --pane "$HERDR_PANE_ID"
```

`claim` records the Source review and moves the task to `finished`. If changes are needed, send a new task.

To remove a task and its saved result permanently, run `./handoff delete <task-id>` only
when that deletion is intended.

To remove a state class or all tasks, use `./handoff delete --state <state>` or
`./handoff delete --all`.

To collect only records that are safe to drop, use `./handoff clean`. It never touches a task
that is still open. `./handoff clean invalid` removes the tasks that ended without a result
(`target_absent`, `source_absent`, `timeout`, `cancelled`); `./handoff clean old` removes any
terminal task that has stood unchanged for more than 7 days (`--days N` changes that), which
includes `finished` and `rejected`.

## Daemon and board

The daemon is manual; never assume it is running:

```sh
./handoff daemon start
./handoff daemon status
./handoff ui
./handoff daemon stop
```

The board shows the current state, what the process column makes of it (the next command, or
Jev's reading of the agent that owes it -- what that agent is doing and how far along the task
is), and a per-agent status for both ends straight from Herdr (`working`, `idle`, `blocked`,
`done`), each painted in its tool's own colour. The end that owes the next step is lit and the other is dimmed, which is how the
board says whose move it is: a bright band travels along the name of the end that owes the
next step, and the other name is dimmed.
`START` (task start) and `REMIND` (countdown to the next reminder for remindable actions; `due` when overdue and blank otherwise),
state duration, next retry, retry count, and errors. Active tasks appear first; each group is ordered by
the newest current node start time.
The task rows have a muted right-edge scrollbar when rendered.

The board is interactive and can act on tasks, not just display them:

| Key | Action |
|---|---|
| `↓` / `↑` | move the cursor |
| `space` | toggle the cursor row's checkbox |
| `a` | select all / none |
| `r` | re-send the stored prompt to the Targets of the checked tasks |
| `s` | have Jev re-score every open task |
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
8, 16, 32, and 60 minutes and cap at 1 hour. If an expected Agent is `working`, the daemon first uses
`herdr agent wait <agent> --until idle`; that wait time is outside the backoff timer.

The daemon skips a reminder when the target pane is focused or its current Herdr detection
snapshot contains an interruption marker. These skips do not consume retries; reminders resume
after the pane is no longer focused and the marker is gone. When Jev is configured it also
holds a reminder back for work that is still visibly running, chooses the next reminder delay
from the 2, 4, 8, 16, 32, or 60 minute tier, or keeps the current deadline and tier, and
updates the board reading with the same request when a reminder is due. A selected tier drives
the later exponential backoff until 60 minutes. Reminder text asks the agent to report its state, waiting
reason, awaited work, and expected completion or next change.

A reminder names the exact command that is outstanding. Answer it with that command. None
goes out while your pane is `blocked` -- an approval prompt is you being asked something, and
herdr refuses a prompt to a blocked pane anyway.

If a pane moves or its terminal identity disappears, the task is marked absent. Re-select
the Target and send a new task; the service does not migrate it automatically.

## Closed tasks cannot be advanced

A command that would move a closed task (`finished`, `rejected`, `cancelled`, `timeout`,
`*_absent`) back into the live set is refused, with a non-zero exit and a message naming the
current state. This covers `take`, `done`, `done-implicit` and `reject`. Without the guard, an agent obeying a stale reminder would set a finished task back
to `active`, redo the work, overwrite the saved result and notify the Source a second time.

`claim` is deliberately idempotent: it lands on `finished`, so re-running it is a
harmless retry rather than a resurrection.
