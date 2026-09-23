#!/usr/bin/env python3
"""Small, dependency-free Herdr handoff coordinator."""
import argparse, fcntl, json, os, re, select, shutil, sqlite3, subprocess, sys, termios, time, unicodedata, uuid
# urllib.request is deliberately absent: it costs ~37ms to import, a third of the
# time a `handoff take` spends starting up, and only jev_ask ever needs it.
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(os.environ.get("HANDOFF_STATE_DIR", Path.home()/".local/state/handoff"))
DB = ROOT / "handoff.sqlite3"
CLI = str(Path(__file__).resolve())
PROTOCOL_ACK_TIMEOUT = int(os.environ.get("HANDOFF_PROTOCOL_ACK_TIMEOUT", "30"))
PROTOCOL_ACK_RETRIES = int(os.environ.get("HANDOFF_PROTOCOL_ACK_RETRIES", "3"))
EXECUTION_BACKOFF_INITIAL = int(os.environ.get("HANDOFF_EXECUTION_BACKOFF_INITIAL", "120"))
EXECUTION_BACKOFF_MAX = int(os.environ.get("HANDOFF_EXECUTION_BACKOFF_MAX", "28800"))
REVIEW_BACKOFF_INITIAL = int(os.environ.get("HANDOFF_REVIEW_BACKOFF_INITIAL", "120"))
REVIEW_BACKOFF_MAX = int(os.environ.get("HANDOFF_REVIEW_BACKOFF_MAX", "28800"))
SWEEP_SECONDS = float(os.environ.get("HANDOFF_SWEEP_SECONDS", "2"))
AGENT_WAIT_SLICE_MS = int(os.environ.get("HANDOFF_AGENT_WAIT_SLICE_MS", "3000"))
                                    # see the note on the agent wait inside daemon()
AGENT_READ_LINES = 120
INTERRUPTION_MARKERS = ("Conversation interrupted", "Request interrupted by user")

# ---------- Jev (TypeSafe System One) ----------
# Jev supplies two judgments herdr cannot give: whether a due reminder should be held back,
# and -- on demand from the board -- how far each open task has actually got. It never moves
# a task between states: every transition still comes from an explicit handoff command.
JEV_URL = os.environ.get("TYPESAFE_API_URL", "https://api.typesafe.ai/v1/systemone")
JEV_MODEL = os.environ.get("TYPESAFE_MODEL", "jev-latest")
JEV_TIMEOUT = float(os.environ.get("HANDOFF_JEV_TIMEOUT", "8"))
# The floor between two requests. Every Jev call in this process goes through jev_ask, and
# nothing needs an answer twice in five seconds -- a sweep that collides with a review can
# wait, and falls back to the reminder it would have sent anyway.
JEV_MIN_INTERVAL = float(os.environ.get("HANDOFF_JEV_MIN_INTERVAL", "5"))
_JEV_LAST = {"at": 0.0}

def jev_wait_left():
    """Seconds until the next request is allowed; zero when one may go now.

    The interval is read at call time for the same reason the URL is: a test or a supervisor
    that sets it per-process should not have to re-import the module to be heard.
    """
    interval = float(os.environ.get("HANDOFF_JEV_MIN_INTERVAL", JEV_MIN_INTERVAL))
    return max(0.0, interval - (time.time() - _JEV_LAST["at"]))
# hold_off at or above this suppresses the reminder. A false positive only delays a nudge
# by one cycle; a false negative nudges a user who just pressed Escape, or an agent that
# is in the middle of a long-running step.
JEV_SUPPRESS_THRESHOLD = float(os.environ.get("HANDOFF_JEV_SUPPRESS_THRESHOLD", "0.5"))
# Snapshots are read tail-first; the recent end is what the judgment is about, and an
# unbounded buffer would only cost tokens.
JEV_SNAPSHOT_CHARS = 4000
JEV_PROMPT_CHARS = 2000

def now(): return datetime.now(timezone.utc).isoformat()
def conn():
    ROOT.mkdir(parents=True, exist_ok=True)
    c=sqlite3.connect(DB); c.row_factory=sqlite3.Row
    c.execute("""create table if not exists tasks(
      id text primary key, description text not null, prompt text not null,
      source_pane text not null, target_pane text not null, state text not null, action text not null,
      state_since text not null, task_started_at text, previous_node_started_at text,
      last_prompt_at text, next_prompt_at text,
      retry_count integer not null default 0, result_file text, last_action text,
      last_action_at text, error text, source_lifecycle text, target_lifecycle text,
      source_presence text, target_presence text, source_phase text, target_phase text,
      source_stage text, target_stage text)""")
    # Add timing columns to stores created before the board showed task/node start times. They
    # stay nullable for the ALTER TABLE path; new rows always fill task_started_at explicitly.
    columns = {row[1] for row in c.execute("pragma table_info(tasks)")}
    if "task_started_at" not in columns:
        try: c.execute("alter table tasks add column task_started_at text")
        except sqlite3.OperationalError as error:
            if "duplicate column name" not in str(error): raise
    if "previous_node_started_at" not in columns:
        try: c.execute("alter table tasks add column previous_node_started_at text")
        except sqlite3.OperationalError as error:
            if "duplicate column name" not in str(error): raise
    # Jev's working-phase judgment is observed metadata, like the lifecycle columns: nullable,
    # refreshed by the daemon, and absent from stores that predate it.
    for phase_column in ("source_phase", "target_phase", "source_stage", "target_stage"):
        if phase_column not in columns:
            try: c.execute("alter table tasks add column %s text" % phase_column)
            except sqlite3.OperationalError as error:
                if "duplicate column name" not in str(error): raise
    # A legacy row has no creation timestamp. Its first observed state is the closest truthful
    # value, and makes the new column useful immediately after opening an old store.
    c.execute("update tasks set task_started_at=state_since where task_started_at is null")
    c.commit()
    # One open task per Target, enforced by the store rather than by a check that a concurrent
    # send can race past: two sends could both SELECT, both see nothing, and both INSERT.
    # A partial index makes the second insert fail instead of succeeding quietly.
    try:
        c.execute("""create unique index if not exists one_open_task_per_target
                     on tasks(target_pane) where state not in
                     ('finished','rejected','cancelled','timeout','target_absent','source_absent')""")
    except sqlite3.IntegrityError:
        # A database written before this index existed may already hold two open tasks for one
        # Target. Refusing to open it would take the whole tool down -- CLI, daemon and board --
        # over data it did not create, so the index is skipped and the SELECT in send() remains
        # the only guard until the duplicates are cleared.
        pass
    return c
def transition(c, tid, state, action, last_action=None, error=None):
    stamp = now()
    # A new state restarts the node's story, so any Jev completion score filed under the old
    # one goes with it -- the daemon re-judges on its next due cycle. A command that repeats
    # the state the task is already in leaves the score alone: the node has not changed.
    c.execute("update tasks set state=?,action=?,previous_node_started_at=state_since,state_since=?,"
              "last_action=?,last_action_at=?,error=?,retry_count=0,"
              "source_phase=case when state=? then source_phase else null end,"
              "target_phase=case when state=? then target_phase else null end,"
              "source_stage=case when state=? then source_stage else null end,"
              "target_stage=case when state=? then target_stage else null end where id=?",
              (state,action,stamp,last_action or action,stamp,error,
               state,state,state,state,tid)); c.commit()
def herdr(*args, timeout=20):
    try:
        p=subprocess.run([os.environ.get("HERDR_BIN_PATH","herdr"),*args],text=True,capture_output=True,timeout=timeout)
        if p.returncode: return None
        data = json.loads(p.stdout)
        # herdr reports failures as {"error": {...}} -- on stdout, with a non-zero exit today.
        # Judging by the payload as well means a change in exit-code behaviour cannot quietly
        # turn a failure into a result: `agent wait --timeout` returning an error object would
        # otherwise read as "the agent is idle now" and prompt a busy agent.
        if isinstance(data, dict) and "error" in data: return None
        return data
    except Exception: return None
def prompt(agent, text):
    return herdr("agent","prompt",agent,text)
def agent_get(agent): return herdr("agent","get",agent)
def agent_lifecycle(agent):
    """Herdr's own word for one pane: (status, present, focused).

    Three answers from the one `agent get` the sweep already needs, so a caller that wants
    both the status and the focus does not pay for a second look.

    `agent get` rather than the `agent list` roster: this asks about a specific pane the task
    is bound to, and a pane that has been closed must read as absent even while herdr is
    answering happily about every other agent it can see.
    """
    info = agent_get(agent)
    if info is None: return "unknown", "absent", False
    result = info.get("result", info) if isinstance(info, dict) else {}
    wrapped = result.get("agent", result) if isinstance(result, dict) else {}
    if not isinstance(wrapped, dict): return "unknown", "present", False
    return (wrapped.get("agent_status", wrapped.get("status", "unknown")), "present",
            bool(wrapped.get("focused")))

def agent_read(agent):
    """Read the current detection buffer without treating terminal text as JSON."""
    try:
        p = subprocess.run(
            [os.environ.get("HERDR_BIN_PATH", "herdr"), "agent", "read", agent,
             "--source", "detection", "--lines", str(AGENT_READ_LINES)],
            text=True, capture_output=True, timeout=5)
        return p.stdout if p.returncode == 0 else None
    except Exception:
        return None

def has_interruption_marker(snapshot):
    return any(marker in snapshot for marker in INTERRUPTION_MARKERS)

def agent_was_interrupted(agent):
    """Return whether the current detection snapshot shows a user interruption."""
    snapshot = agent_read(agent)
    return snapshot is not None and has_interruption_marker(snapshot)

# One question, and the only thing Jev is asked about a task besides how far along it is:
# what state is the agent that owes the next step in? A `choice` has no ten-option ceiling the
# way a score does, so the menu can be as fine as it is useful -- and it holds both kinds of
# answer, the ones on the way through the work and the ones that are not on the way at all.
#
# Nothing here restates what Herdr already reports. Herdr answers `blocked` itself, from the
# approval prompts it recognises in the pane, so Jev is not asked about waiting for a person.
JEV_MOVING = [
    "unstarted",     # 0  nothing about this task is visible
    "reading",       # 1  absorbing the request itself
    "exploring",     # 2  finding out what is already there
    "planning",      # 3  deciding how to go about it
    "groundwork",    # 4  the work the task needs before the task itself
    "output",        # 5  the first concrete piece of the result
    "working",       # 6  the main body, output accumulating
    "first-pass",    # 7  a complete rough version exists
    "refining",      # 8  closing gaps and leftover cases
    "verifying",     # 9  checking it against what was asked
    "concluding",    # 10 writing up the outcome
    "done",          # 11 the result exists and is stated
]
# States that are not positions on that line: trouble the task ran into, and the reasons a
# finished turn is not moving the task on. They can occur at any point, which is exactly why
# they cannot be rungs.
JEV_HELD = [
    "waiting",       # parked on something it started and is still running
    "restarting",    # threw the approach away and is starting over
    "workaround",    # found an obstacle and is routing around it
    "diagnosing",    # something is wrong and it does not know what yet
    "fixing",        # knows what broke and is repairing it
    "error",         # stopped on a failure, and nothing is being done about it
    "unreported",    # the work looks finished but `handoff done` never ran
    "stuck",         # tried what it had, nothing worked, no next step it can see
    "elsewhere",     # the turn ended and the pane moved on
]
JEV_STATE_WORDS = JEV_MOVING + JEV_HELD
JEV_STATE_OPTIONS = {
    "unstarted": "Nothing about this task is visible: the pane is idle, or busy with something"
                 " unrelated.",
    "reading": "Reading the request itself: what was asked for, and the material it points at.",
    "exploring": "Finding out what is already there before changing anything: looking through"
                 " the code, the data, the documents, or running the thing to see what happens.",
    "planning": "Deciding how to go about it: weighing approaches or laying out a plan; nothing"
                " produced yet.",
    "groundwork": "Doing what the task needs before the task itself: environment, inputs,"
                  " scaffolding, collecting or cleaning the material -- real work, and none of"
                  " it the substance yet.",
    "output": "The first concrete piece of the result now exists.",
    "working": "Working through the main body; concrete output is accumulating.",
    "first-pass": "A complete first pass exists, however rough; the rest is filling it in.",
    "refining": "Refining what exists: closing the remaining gaps and the leftover cases.",
    "verifying": "Checking the result against what the task asked for: verification,"
                 " measurement, comparison.",
    "concluding": "Writing up the outcome: the summary, or the result being handed back, is"
                  " being produced.",
    "done": "Complete: the task's result exists and is stated; nothing about it is still in"
            " progress.",
    "waiting": "Parked on something it started that is still running -- a build, a job, a long"
               " command, a run that will take hours -- and it has nothing to do until that"
               " finishes. This covers the long background tasks; there is no separate word for"
               " them because a snapshot cannot say how long a command will take.",
    "restarting": "Abandoned the approach it had been taking and is starting over on a"
                  " different one; what was already produced is being thrown away.",
    "workaround": "Found an obstacle the task did not account for -- a limit, a broken"
                  " assumption, something that will not work as planned -- and is routing"
                  " around it rather than through the planned work.",
    "diagnosing": "Something is wrong and it does not yet know what: it is narrowing down the"
                  " cause before it can repair anything.",
    "fixing": "Something already produced came back wrong -- a failure, a check that did not"
              " pass, an approach that was rejected -- and is being repaired.",
    "error": "Stopped on a failure that nothing is being done about: the pane shows an error,"
             " a crash, or a run that failed, and the turn ended there. Unlike `fixing` or"
             " `diagnosing`, nobody is working on it.",
    "unreported": "Finished and parked: the work looks done -- the result exists, or the agent"
                  " said so -- but `handoff done` was never run, so the task is still open.",
    "stuck": "It has tried the routes it had and none of them work, with no failure on screen"
             " to explain it: it is going in circles and needs someone else to decide something.",
    "elsewhere": "Finished and parked, on other work: the turn ended and the pane has moved on"
                 " to something unrelated to this task.",
}
# The score answers a different question and is allowed to contradict the state: the state says
# what the agent is doing, the score says how much of the way to a finished result that is. The
# same state can sit at very different scores -- a long `working` two hours in against one five
# minutes in -- so nothing here restates the state list. The API caps this question at ten
# levels, which is the only reason there are ten.
JEV_SCORE_LEVELS = [
    "Nothing has been done toward the task.",
    "Just begun: almost none of the result exists yet.",
    "Early: a small part of what the task needs is in place.",
    "About a third of the way to a finished result.",
    "A substantial part is done, but not yet most of it.",
    "About half of the work toward a finished result.",
    "More than half: the bulk exists and real work remains.",
    "Most of the way: only a minority of the job is left.",
    "Nearly there: the last part is all that is missing.",
    "Finished: the task's result is complete.",
]
JEV_SCORE_MAX = len(JEV_SCORE_LEVELS) - 1
# The daemon asks exactly one thing per sweep: whether this reminder should be held back.
# Scoring is not a background activity -- it is asked for on demand from the board, over a
# richer state (see jev_score_all), so a score is never silently rewritten under the user.
JEV_QUESTIONS = {
    "hold_off": {
        "type": "noul",
        "instructions": "A reminder about the pending handoff task is due. Based on the "
                        "terminal snapshot, should the reminder be held back this cycle?",
        "criteria": {
            "true": "The user deliberately interrupted or ended the agent's turn (for "
                    "example pressing Escape), OR the agent is visibly engaged with this "
                    "task right now -- for example it started a long-running command, build "
                    "or simulation that is still running and it is waiting on it.",
            "false": "The turn ended without the task being reported, and the agent is "
                     "idle, done, or busy with something unrelated to this task."}},
}

def state_question(row):
    """What the agent that owes the next step is doing, or why it is not doing anything.

    One question, not two: the state and the reason it is not moving are the same kind of
    answer about the same agent, and asking separately only invited them to contradict each
    other in the same cell.
    """
    return {
        "type": "choice",
        "instructions": "What is the agent that owes the next step on task `%s` (%s) doing "
                        "right now? The snapshot is its terminal; the recorded history says "
                        "how the task got here."
                        % (row["id"], row["description"].replace("\n", " ")[:120]),
        "criteria": JEV_STATE_OPTIONS}

def jev_enabled():
    """Jev is on when a key is configured and not explicitly switched off.

    Without a key every Jev path falls back to the pre-Jev behaviour, so the daemon never
    depends on the API being reachable or even configured.
    """
    return bool(os.environ.get("TYPESAFE_API_KEY")) and os.environ.get("HANDOFF_JEV", "1") != "0"

def jev_ask(state, questions, partial=False):
    """One System One request. Returns {question_id: answer} or None on any failure.

    The return type is deliberately flat: a noul comes back as a float, a choice as the
    chosen option key, a score as a number. Any malformed or missing answer fails the whole
    request -- for the daemon's reminder decision a half-parsed judgment would be worse than
    the fallback path it replaces. `partial=True` is for the board's batch review, where the
    questions are independent per task and scoring the ones that came back beats scoring none.
    """
    import urllib.request          # ~37ms, and only the Jev paths ever pay it
    key = os.environ.get("TYPESAFE_API_KEY")
    if not key: return None
    if jev_wait_left() > 0: return None        # asked too recently; callers fall back
    # The URL is read at call time, not import time, so a test or a supervisor that sets
    # TYPESAFE_API_URL per-process does not have to re-import the module.
    url = os.environ.get("TYPESAFE_API_URL", JEV_URL)
    body = json.dumps({"state": state, "model": JEV_MODEL, "questions": questions}).encode()
    req = urllib.request.Request(url, data=body, headers={
        "Authorization": "Bearer " + key, "Content-Type": "application/json"})
    _JEV_LAST["at"] = time.time()   # counted on the attempt, so an outage is not retried flat out
    try:
        with urllib.request.urlopen(req, timeout=JEV_TIMEOUT) as resp:
            data = json.loads(resp.read())
    except Exception:
        return None
    answers = data.get("answers") if isinstance(data, dict) else None
    if not isinstance(answers, dict): return None
    out = {}
    for qid, q in questions.items():
        a = answers.get(qid)
        if not isinstance(a, dict):
            if partial: continue
            return None
        if q["type"] == "noul":
            v = a.get("noul")
            if not isinstance(v, (int, float)):
                if partial: continue
                return None
            out[qid] = float(v)
        elif q["type"] == "choice":
            v = a.get("choice")
            if v not in q["criteria"]:
                if partial: continue
                return None
            out[qid] = v
        elif q["type"] == "score":
            v = a.get("score")
            if not isinstance(v, (int, float)):
                if partial: continue
                return None
            out[qid] = float(v)
        else:
            if partial: continue
            return None
    return out

def _jev_state(row, snapshot):
    return {"task": {"id": row["id"], "description": row["description"],
                     "prompt": row["prompt"][:JEV_PROMPT_CHARS]},
            "pending_action": row["action"],
            "agent_terminal_snapshot": snapshot[-JEV_SNAPSHOT_CHARS:]}

def jev_score_text(value):
    """A raw Jev score as the string the board renders, or None when there is none.

    Stored exactly as it arrived. The answer is not one of ten fixed numbers -- it is a
    position on the scale (3.61, 3.24, 3.74 in live calls), so the digits are the answer.
    Rounding them to a whole level collapsed 3.2 and 3.8 into the same 33%.
    """
    if value is None: return None
    return str(min(max(value, 0), JEV_SCORE_MAX))

_UNSET = object()      # decide_reminder sentinel: "the caller did not fetch this for me"

def decide_reminder(row, agent, snapshot=_UNSET, answers=_UNSET):
    """The reminder text to send this sweep, or None to hold off without spending a retry.

    Exact interruption markers are checked first: a known marker is a certain, free answer,
    and asking the model about it would only add variance (the canonical Codex marker scores
    around 0.64, uncomfortably close to the suppress threshold). Beyond the markers, Jev
    answers one question -- hold_off: the user deliberately ended the turn, or the agent is
    visibly working on the task (a background simulation it is monitoring counts, which is
    exactly the case Herdr's lifecycle cannot see once the turn ends). The reminder text
    itself is always the standard one. The daemon passes its combined answer set in so a
    sweep never pays for two requests; on any Jev failure the standard reminder goes out,
    since the markers have already had their say.
    """
    if snapshot is _UNSET: snapshot = agent_read(agent)
    if snapshot is not None and has_interruption_marker(snapshot):
        return None
    if jev_enabled() and snapshot and snapshot.strip():
        if answers is _UNSET:
            answers = jev_ask(_jev_state(row, snapshot), JEV_QUESTIONS)
        if answers is not None and answers["hold_off"] >= JEV_SUPPRESS_THRESHOLD:
            return None
    return reminder_text(row, row["action"])

def record_identity(c, row, a):
    """Refresh one side's pane from whoever just ran a protocol command.

    The pane is recorded only once Herdr confirms it resolves. An agent that cannot tell what
    its own pane is has handed over a placeholder (`unknown`), and another handed over the
    same id with its workspace prefix stripped (`p16`). Both went in unchecked; the second
    stranded a live task, because the daemon addresses the Target by this value and every
    later lookup found no such pane. Keeping the recorded pane when the new one does not
    resolve costs nothing -- the row still points at a pane that existed, which beats one
    that never did. The command itself still goes through: this is a repair, not a gate.
    """
    if agent_get(a.pane) is None: return
    side = "target" if row["action"] in ("take", "done") else "source"
    c.execute(f"update tasks set {side}_pane=? where id=?", (a.pane, row["id"]))
    c.commit()

def delete_tasks(c, ids):
    """Drop tasks by id: unlink each saved result file, then remove the rows. Returns count."""
    ids = list(ids)
    if not ids: return 0
    marks = ",".join("?" * len(ids))
    rows = c.execute("select id,result_file from tasks where id in (%s)" % marks, tuple(ids)).fetchall()
    for item in rows:
        if item["result_file"]:
            try: Path(item["result_file"]).unlink()
            except FileNotFoundError: pass
    c.execute("delete from tasks where id in (%s)" % marks, tuple(ids)); c.commit()
    return len(rows)

def task_text(row, resend=False):
    """The prompt a Target receives for a task; the board's resend key reuses it, marked as a repeat.

    A repeat must not read like a first delivery. Otherwise the Target cannot tell a stale
    nudge from a new task, and its only recourse is to go inspect the task record -- or to
    redo work that is already on file.
    """
    head = "[HANDOFF TASK — RE-SENT]" if resend else "[HANDOFF TASK]"
    repeat = ("\nThis repeats a task sent to you earlier. It is still open as `%s`."
              " Check what you have already done before redoing any work.\n" % row["state"]) if resend else ""
    return (head + "\nTask ID: {id}\nDescription: {description}\n"
            "Source: {source_pane}\nTarget: {target_pane}\n"
            + repeat + "\n"
            "Before any work, run:\npython3 {cli} take {id} --pane <your-pane>\n\n"
            "Task:\n{prompt}\n\n"
            "On completion run:\npython3 {cli} done {id} --result-file <path> --pane <your-pane>\n"
            'Only if refusing run:\npython3 {cli} reject {id} --reason "<reason>"'
            ).format(cli=CLI, id=row["id"], description=row["description"],
                     source_pane=row["source_pane"], target_pane=row["target_pane"],
                     prompt=row["prompt"])

IDENTITY_ARGS = "--pane <your-pane>"
REMINDER_COMMAND = {
    "take":   "python3 {cli} take {id} " + IDENTITY_ARGS,
    "done":   "python3 {cli} done {id} --result-file <path> " + IDENTITY_ARGS,
    "claim":  "python3 {cli} claim {id} " + IDENTITY_ARGS,
    "accept": "python3 {cli} accept {id} " + IDENTITY_ARGS,
}
REMINDER_WHY = {
    "take":   "This task is waiting for you to accept it.",
    "done":   "You accepted this task but have not submitted a result yet.",
    "claim":  "A result has been submitted and is waiting for you to look at it.",
    "accept": "You reviewed a result but have not marked the task finished yet.",
}
# The pane that owns each pending action.  There is deliberately no fallback:
# an unknown/non-action marker must never be turned into a reminder for Source.
REMINDER_RECIPIENT = {
    "take": "target",
    "done": "target",
    "claim": "source",
    "accept": "source",
}

def reminder_text(row, action):
    """The nudge the daemon sends: says why, and gives the exact command for that action.

    A generic `handoff {action} {id}` is not enough -- `done` needs `--result-file`, so the
    agent would be handed a command that argparse rejects.
    """
    command = REMINDER_COMMAND.get(action, "python3 {cli} {action} {id}").format(
        cli=CLI, id=row["id"], action=action)
    why = REMINDER_WHY.get(action, "This task is waiting on you.")
    return ("[HANDOFF REMINDER]\nTask ID: {id}\nDescription: {desc}\n\n{why}\n\nRun:\n{cmd}"
            ).format(id=row["id"], desc=row["description"], why=why, cmd=command)

def cmd_send(a):
    if not a.description.strip(): raise SystemExit("description must not be empty")
    c=conn(); tid="t_"+uuid.uuid4().hex[:10]
    # One open task per TARGET, not one per state store. The point is that a Target is never
    # asked to work on two handoffs at once; scoping it globally meant one window's long
    # experiment made `send` fail for every other window sharing the store.
    if c.execute("select 1 from tasks where target_pane=? and state not in "
                 "('finished','rejected','cancelled','timeout','target_absent','source_absent')"
                 " limit 1", (a.target_pane,)).fetchone():
        raise SystemExit("this target already has an unfinished task")
    # Explicit source and target are required; validate when Herdr is available.
    if agent_get(a.source_pane) is None: raise SystemExit("source pane is absent or herdr is unavailable")
    if agent_get(a.target_pane) is None: raise SystemExit("target pane is absent or herdr is unavailable")
    started_at = now()
    try:
        c.execute("insert into tasks(id,description,prompt,source_pane,target_pane,state,action,state_since,"
                  "task_started_at,previous_node_started_at,next_prompt_at,source_presence,target_presence) "
                  "values(?,?,?,?,?,?,?,?,?,?,?,?,?)",
          (tid,a.description,a.prompt,a.source_pane,a.target_pane,"published","take",started_at,
           started_at,None,datetime.fromtimestamp(time.time()+PROTOCOL_ACK_TIMEOUT,timezone.utc).isoformat(),
           "present","present"))
        c.commit()
    except sqlite3.IntegrityError:
        # The partial unique index turned the check above into something a concurrent send
        # cannot race past. This is the authoritative guard; the SELECT only gives a nicer
        # message in the common single-sender case.
        raise SystemExit("this target already has an unfinished task")
    row = {"id":tid, "description":a.description, "prompt":a.prompt,
           "source_pane":a.source_pane, "target_pane":a.target_pane}
    # The delivery failed, so there is no next step: a terminal state carrying "take" would have
    # the board name a command that can never run. Kept in step with the invariant that every
    # state in CLOSED_STATES carries action "none".
    if prompt(a.target_pane, task_text(row)) is None: transition(c,tid,"timeout","none",error="prompt failed")
    print(tid)

def cmd_action(a):
    c=conn(); row=c.execute("select * from tasks where id=?",(a.id,)).fetchone()
    if not row: raise SystemExit("unknown task")
    # An agent obeying a stale reminder must not be able to resurrect a closed task: `take`
    # would set it back to active, it would redo the work, `done` would overwrite the saved
    # result and notify the Source a second time. The guard only blocks the transition OUT of
    # a terminal state, so every live state behaves exactly as before.
    if a.cmd in RESURRECTING_COMMANDS and row["state"] in CLOSED_STATES:
        raise SystemExit("task %s is already %s; `%s` refused" % (a.id, row["state"], a.cmd))
    if a.cmd in ("take", "done", "done-implicit", "claim", "accept"):
        record_identity(c, row, a)
    if a.cmd=="take": transition(c,a.id,"active","done","take")
    elif a.cmd=="done":
        p=Path(a.result_file).expanduser()
        if not p.is_file() or not os.access(p,os.R_OK): raise SystemExit("result file is not readable")
        dest=ROOT/"results"/(a.id+".md"); dest.parent.mkdir(exist_ok=True); shutil.copyfile(p,dest)
        c.execute("update tasks set result_file=? where id=?",(str(dest),a.id)); c.commit(); transition(c,a.id,"result_ready","claim","done")
        prompt(row["source_pane"],f"[HANDOFF RESULT READY]\nTask ID: {a.id}\nDescription: {row['description']}\nResult file: {dest}\n\nInspect it, then finish the task with:\npython3 {CLI} claim {a.id} --pane <your-pane>")
    elif a.cmd in ("claim", "accept"): transition(c,a.id,"finished","none",a.cmd)
    elif a.cmd=="reject": transition(c,a.id,"rejected","none","reject",a.reason)
    elif a.cmd=="cancel": transition(c,a.id,"cancelled","none","cancel")
    elif a.cmd=="delete":
        if a.id: ids = [r["id"] for r in c.execute("select id from tasks where id=?", (a.id,))]
        elif a.state: ids = [r["id"] for r in c.execute("select id from tasks where state=?", (a.state,))]
        else: ids = [r["id"] for r in c.execute("select id from tasks")]
        print(f"deleted {delete_tasks(c, ids)} task(s)")
    elif a.cmd=="done-implicit":
        p=Path(a.result_file).expanduser()
        if not p.is_file(): raise SystemExit("result file is not readable")
        dest=ROOT/"results"/(a.id+".md"); dest.parent.mkdir(exist_ok=True)
        if p.resolve() != dest.resolve(): shutil.copyfile(p,dest)
        c.execute("update tasks set result_file=? where id=?",(str(dest),a.id)); c.commit(); transition(c,a.id,"result_ready","claim","done-implicit")

def duplicate_open_targets(c):
    """Targets held by more than one open task. Only reachable when the index is missing."""
    marks = ",".join("?" * len(CLOSED_STATES))
    return c.execute("select target_pane, count(*) n from tasks where state not in (%s)"
                     " group by target_pane having n > 1" % marks,
                     tuple(CLOSED_STATES)).fetchall()

def cmd_list(_):
    c = conn()
    # conn() skips the index when legacy duplicates stop it being created, which leaves the
    # store without its one-open-task-per-Target guarantee. Say so rather than degrade quietly:
    # in that state two concurrent sends can both get through.
    for r in duplicate_open_targets(c):
        sys.stderr.write("handoff: %s holds %d open tasks; the one-open-task-per-target "
                         "index is not in force until that is resolved\n"
                         % (r["target_pane"], r["n"]))
    for r in c.execute("select * from tasks order by state_since"):
        age=int(time.time()-datetime.fromisoformat(r["state_since"]).timestamp())
        print(f"{r['id']}\t{r['description'].replace(chr(10),' / ')}\t{r['source_pane']} → {r['target_pane']}\t{r['state']}\t{r['action']}\t{age}s")

def cmd_clean(a):
    c = conn()
    try:
        ids = clean_task_ids(c, a.what, a.days)
        print("deleted %d task(s)" % delete_tasks(c, ids))
    finally: c.close()

def daemon(a):
    if a.op=="status":
        print("running" if daemon_running() else "stopped"); return
    if a.op=="stop":
        ok, message = request_stop()
        if not ok:
            if message == NO_DAEMON: print(message); return
            raise SystemExit(message)
        # Report what actually happened rather than that the request was filed. A sweep parked
        # on a busy agent takes at most one slice to notice (it re-checks between tasks), so the
        # budget covers that with room to spare.
        for _ in range(150):
            if not daemon_running(): print("daemon stopped"); return
            time.sleep(0.1)
        raise SystemExit("the daemon did not stop within 15 seconds")
    guard = lifecycle_lock(retries=3)
    if guard is None:
        raise SystemExit("another start or stop is in progress; try again")
    try:
        lock = daemon_lock()
        if lock is None:
            raise SystemExit("a daemon is already running")   # its pid is in daemon.pid
        # Only reachable when no daemon holds the lock, so clearing a leftover stop file here
        # cannot rob a running daemon of its own shutdown request. Publishing the pid inside the
        # guard is what stops a concurrent stop from reading it as it changes hands.
        try: (ROOT/"daemon.stop").unlink()
        except FileNotFoundError: pass
        (ROOT/"daemon.pid").write_text(str(os.getpid())); print("daemon started")
    finally:
        guard.close()
    try:
        while not stop_requested():
            c=conn()
            for r in c.execute("select * from tasks where state not in ('finished','rejected','cancelled','timeout')").fetchall():
                # Re-check between tasks: every busy agent costs a wait slice, so a sweep full of
                # them would otherwise run past the budget `daemon stop` allows for.
                if stop_requested(): break
                # `none` is a terminal/non-action marker.  It can be written when a
                # target/source disappears or after a task is completed.  Never turn
                # it into a source reminder: doing so used to send `none <task-id>`
                # to the wrong pane.
                recipient = REMINDER_RECIPIENT.get(r["action"])
                if recipient is None or r["state"] in ("target_absent", "source_absent"):
                    continue
                ag = r[f"{recipient}_pane"]
                # Both ends, not just the one about to be reminded: the other half of the row
                # used to stay frozen at whatever an earlier sweep saw, and that stale half is
                # part of what the review sends to Jev as the task's context.
                lives = {end: agent_lifecycle(r["%s_pane" % end])
                         for end in ("source", "target")}
                lifecycle, present, focused = lives[recipient]
                side = recipient
                # The sweep's one Jev request is the reminder judgment and nothing else.
                # Neither the score nor the idle reason is written here: both are written by
                # the board's review key, so what is on screen is what somebody asked for and
                # it stays put until the task moves state or the review runs again.
                # The fallback is resolved first: `time.time() >= ... or now()` would call
                # now() AFTER reading the clock, so a row with no next_prompt_at -- one a
                # test or an older store wrote by hand -- would never look due.
                due_at = r["next_prompt_at"] or now()
                reminder_due = time.time() >= datetime.fromisoformat(due_at).timestamp()
                snapshot, answers = None, None
                if jev_enabled() and reminder_due:
                    snapshot = agent_read(ag)
                    if snapshot and snapshot.strip():
                        answers = jev_ask(_jev_state(r, snapshot), JEV_QUESTIONS)
                c.execute("update tasks set source_lifecycle=?, source_presence=?,"
                          " target_lifecycle=?, target_presence=? where id=?",
                          (lives["source"][0], lives["source"][1],
                           lives["target"][0], lives["target"][1], r['id'])); c.commit()
                if present=="absent": transition(c,r["id"],"target_absent" if side == "target" else "source_absent","none",error="Herdr Agent absent"); continue
                # A blocked agent is showing an approval prompt: it is waiting on the person
                # in front of it, not failing to answer us. Reminding it would be refused by
                # herdr anyway (`agent_blocked`, before any input is sent), and counting that
                # refusal as an attempt walked the task through its three protocol retries to
                # `timeout` without a single reminder ever landing. So it waits like a busy
                # agent does, and the task does not age while a human decides.
                if lifecycle in ("working", "blocked"):
                    # Herdr owns the wait and it stays outside the task backoff, but the wait is
                    # bounded. Waiting indefinitely parked the daemon for as long as an agent
                    # stayed busy, so `stop` reported failure while it was simply waiting.
                    if herdr("agent","wait",ag,"--until","idle",
                             "--timeout",str(AGENT_WAIT_SLICE_MS),
                             timeout=AGENT_WAIT_SLICE_MS/1000.0 + 5) is None:
                        continue      # still working; the next sweep re-checks the stop file
                # The agent may have completed the action while the status query or wait was
                # in progress. Re-read before prompting so a stale row cannot send the old
                # command after `done`, `claim`, or another transition.
                fresh = c.execute("select * from tasks where id=?", (r["id"],)).fetchone()
                if not fresh or fresh["state"] in ("finished", "rejected", "cancelled", "timeout"):
                    continue
                if (fresh["action"] not in REMINDER_RECIPIENT or
                        fresh["state"] in ("target_absent", "source_absent")):
                    continue
                if fresh["state"] != r["state"] or fresh["action"] != r["action"]:
                    continue
                # The user is looking at this pane. A reminder would start a turn in the very
                # window they are working in -- which is what pressing Escape undoes -- and they
                # can see the pending state for themselves. Skipped without consuming a retry, so
                # a task cannot age toward its timeout while they are sitting there; moving focus
                # away resumes reminders on the next sweep.
                if focused:
                    continue
                if reminder_due:
                    # The send/hold-off judgment lives in decide_reminder: interruption markers
                    # first, then Jev's hold_off. The answers fetched above are passed along,
                    # so a due reminder does not trigger a second request. None means hold off
                    # this sweep; like the focus skip above, holding off does not spend a retry.
                    kw = {"snapshot": snapshot, "answers": answers} if snapshot is not None else {}
                    text = decide_reminder(r, ag, **kw)
                    if text is None:
                        continue
                    retries = r["retry_count"]
                    protocol = r["state"] in ("published", "result_ready")
                    if protocol and retries >= PROTOCOL_ACK_RETRIES:
                        transition(c, r["id"], "timeout", "none", error="protocol acknowledgement timeout")
                        continue
                    if prompt(ag, text) is None:
                        # herdr refuses a prompt to a pane that went blocked between the status
                        # query and here, and a submission can stall. Nothing reached the agent,
                        # so this is not an attempt: counting it would age the task toward a
                        # timeout with no reminder behind it.
                        continue
                    if protocol:
                        delay = PROTOCOL_ACK_TIMEOUT
                    else:
                        delay = min(EXECUTION_BACKOFF_MAX, EXECUTION_BACKOFF_INITIAL * (2 ** retries))
                    c.execute("update tasks set last_prompt_at=?,next_prompt_at=?,retry_count=retry_count+1 where id=?",(now(),datetime.fromtimestamp(time.time()+delay,timezone.utc).isoformat(),r['id'])); c.commit()
            time.sleep(SWEEP_SECONDS)
    finally:
        # Only remove the pid file if it still names this process. Deleting it blind let a
        # daemon that was exiting erase the record belonging to whoever holds the daemon role
        # now, leaving a live daemon with nothing pointing at it.
        try:
            if (ROOT/"daemon.pid").read_text().strip() == str(os.getpid()):
                (ROOT/"daemon.pid").unlink()
        except (FileNotFoundError, ValueError): pass
        # The stop file is deliberately left alone. `daemon start` clears it once it holds the
        # lock, and an exiting daemon removing it could delete a request aimed at its successor.
        lock.close()               # releases the flock; the kernel would do it anyway
# ---------- board rendering ----------

CLOSED_STATES = ("finished","rejected","cancelled","timeout",
                 "target_absent","source_absent")
# Commands that would move a closed task back into the live set. `claim`/`accept` are absent on
# purpose: they land on `finished`, so re-running one is a harmless retry, not a resurrection.
RESURRECTING_COMMANDS = ("take","done","done-implicit","reject")
# The board prints the state name verbatim: it is the same word `handoff delete --state <name>`
# takes, and six of the seven states were already shown untranslated -- only `result_ready` was
# being prettified, which made the one that differed the hardest to match against a command.
STATE_STYLE = {"published":("blue",),"active":("cyan",),"result_ready":("magenta",),
               "finished":("green",),"rejected":("red",),
               "cancelled":("dim",),"timeout":("red",),
               "target_absent":("red",),"source_absent":("red",)}
ACTION_LABEL = {"take":"take","done":"done","claim":"claim","accept":"accept","none":"—"}
# No bold anywhere: weight is carried by colour alone. The bold codes are deliberately
# absent from this table so a stray `("boldred",)` fails to render instead of creeping back.
# Underline is the one attribute here, and only ever on the Target's name: it marks a role
# rather than a weight, and the board uses it nowhere else.
_CODES = {"dim":"2","underline":"4","red":"31","green":"32","yellow":"33","blue":"34",
          "magenta":"35","cyan":"36",
          # Row backgrounds. The cursor row is the brighter of the two; a row that is both is
          # shown as the cursor, since that is what the next keypress will act on.
          "bg_cursor":"48;5;238", "bg_picked":"48;5;235", "scroll_thumb":"90"}
# A closed row, end to end. One grey for the whole line, below anything a live row uses, so a
# finished task recedes without any of it going unreadable.
_CODES["closed"] = "38;5;240"

_ANSI_ON = False

def _use_color():
    global _ANSI_ON
    _ANSI_ON = (sys.stdout.isatty() and os.environ.get("TERM","") not in ("","dumb")
                and "NO_COLOR" not in os.environ)
    return _ANSI_ON

def _cw(ch):
    """Terminal columns for one character. CJK counts as 2."""
    return 2 if unicodedata.east_asian_width(ch) in ("W","F") else 1

def _dw(s): return sum(_cw(ch) for ch in s)

def _visible_dw(s): return _dw(re.sub(r"\033\[[0-9;]*m", "", s))

def _fit(s, width):
    if width <= 0: return ""
    if _dw(s) <= width: return s
    if width == 1: return "…"
    out, w = "", 0
    for ch in s:
        if w + _cw(ch) > width - 1: break
        out += ch; w += _cw(ch)
    return out + "…"

def _fit_segments(segs, width):
    """Trim a [(text, styles)] list to `width` display columns. Returns (segments, used)."""
    out, used = [], 0
    for text, styles in segs:
        if used + _dw(text) > width:
            room = width - used
            if room > 1:
                piece = _fit(text, room)
                out.append((piece, styles)); used += _dw(piece)
            break
        out.append((text, styles)); used += _dw(text)
    return out, used

def _pad(s, width, right=False):
    gap = max(0, width - _dw(s))
    return " "*gap + s if right else s + " "*gap

def _paint(s, *styles):
    flat = []
    for st in styles: flat.extend(st if isinstance(st,(tuple,list)) else (st,))
    if not flat or not _ANSI_ON: return s
    return "".join("\033[%sm" % _CODES[x] for x in flat if x in _CODES) + s + "\033[0m"

def _human_age(sec):
    sec = max(0, int(sec))
    if sec < 60: return "%ds" % sec
    if sec < 3600: return "%dm" % (sec // 60)
    if sec < 86400: return "%dh%02dm" % (sec // 3600, sec % 3600 // 60)
    return "%dd%02dh" % (sec // 86400, sec % 86400 // 3600)

def _display_time(value):
    """Use a compact local timestamp that fits beside the board's operational columns."""
    if not value: return "—"
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.astimezone().strftime("%m-%d %H:%M")
    except (TypeError, ValueError, OverflowError):
        return _fit(str(value), 11)

# Twelve greys for the operator's name to walk up and down: the name breathes, so the end
# that owes the next step is the one your eye lands on without reading a word. Pre-registered
# like every other code, so an unknown style still renders as plain text instead of leaking a
# raw escape.
PULSE_SECONDS = float(os.environ.get("HANDOFF_PULSE_SECONDS", "2.4"))
# A tenth of the period per character, so the crest travels along the name instead of the
# whole thing brightening at once.
PULSE_LAG = float(os.environ.get("HANDOFF_PULSE_LAG", "0.1"))
# How many shades a kind's colour is broken into for the wave. Twelve reads as smooth at the
# board's frame rate without turning into a gradient nobody can see.
PULSE_LEVELS = 12
def pulse_level(now, offset=0):
    """Which shade of its own colour one character of a breathing name is at.

    `offset` puts each character a little behind the one before it, so a crest runs along the
    name. The triangle is squared: the highlight is a narrow band and the name keeps its own
    colour the rest of the time -- a name that was mostly highlight would wash out the very
    colour that says which tool it is.
    """
    if not _ANSI_ON: return None
    phase = (now / PULSE_SECONDS - offset * PULSE_LAG) % 1.0
    wave = (1.0 - abs(2.0 * phase - 1.0)) ** 2.0
    return min(PULSE_LEVELS - 1, int(wave * PULSE_LEVELS))

# Herdr's own words, live. Jev's reading of the agent goes in PROCESS instead: this
# column is re-read every frame, and a judgment written here at the last `s` would
# be a fact from the past sitting among facts from now.
STATUS_STYLE = {"idle":("green",), "done":("green",), "working":("yellow",),
                "blocked":("red",), "unknown":("dim",)}
# Each agent kind has its own colour, so the two ends of a task are told apart by which tool
# is running rather than by decoding a name. RGB rather than a 256-colour index, so a kind can
# be matched exactly; codex's value was given as rgb(130, 139, 251). Anything unlisted takes a
# stable pick from the fallback list, so an unfamiliar tool keeps one colour across runs.
AGENT_RGB = {"codex": (130, 139, 251), "claude": (217, 119, 87), "gemini": (66, 133, 244),
             "cursor": (205, 205, 205), "copilot": (140, 160, 180), "kimi": (150, 120, 220),
             "qwen": (110, 190, 140), "grok": (230, 120, 180), "cline": (210, 170, 120),
             "amp": (110, 170, 230), "opencode": (170, 140, 200), "droid": (160, 200, 120),
             "devin": (120, 180, 200), "kiro": (200, 190, 130)}
AGENT_RGB_FALLBACK = ((150, 150, 150), (170, 140, 200), (110, 170, 230), (110, 190, 140),
                      (210, 170, 120), (230, 120, 180), (120, 180, 200), (200, 190, 130))

def agent_stem(kind):
    """The _CODES prefix a kind's shades live under, or None for a pane we know nothing of."""
    if not kind: return None
    if kind in AGENT_RGB: return "agent:" + kind
    return "agent:fallback%d" % (sum(ord(ch) * (i + 1) for i, ch in enumerate(kind))
                                 % len(AGENT_RGB_FALLBACK))

def agent_style(kind, level=None):
    """The colour a kind is drawn in, optionally one shade down the wave from its crest."""
    stem = agent_stem(kind)
    if stem is None: return ()
    return (stem if level is None else "%s:%d" % (stem, level),)

def _shade(rgb, level):
    """One step along the wave: the kind's own colour at the trough, a highlight at the crest.

    Brighter, not darker -- toward white rather than toward black -- so the running band reads
    as light on the name instead of a shadow through it. Level 0 is the colour itself, which
    is what the end that is not moving keeps.
    """
    toward_white = 0.55 * (level / (PULSE_LEVELS - 1.0))
    return tuple(int(round(c + (255 - c) * toward_white)) for c in rgb)

# Registered like every other style: a raw "38;2;..." sitting in a style tuple would be
# dropped by _paint as an unknown name and render as plain text.
for _kind, _rgb in AGENT_RGB.items():
    _CODES["agent:" + _kind] = "38;2;%d;%d;%d" % _rgb
    for _level in range(PULSE_LEVELS):
        _CODES["agent:%s:%d" % (_kind, _level)] = "38;2;%d;%d;%d" % _shade(_rgb, _level)
for _i, _rgb in enumerate(AGENT_RGB_FALLBACK):
    _CODES["agent:fallback%d" % _i] = "38;2;%d;%d;%d" % _rgb
    for _level in range(PULSE_LEVELS):
        _CODES["agent:fallback%d:%d" % (_i, _level)] = "38;2;%d;%d;%d" % _shade(_rgb, _level)

def stage_style(stage, score=None):
    """One colour per stage, walking the scale: pale while nothing exists, green when done.

    The off-path states are the exception -- they are not at any point on that walk, so they
    get their own colour and mean "look at this" rather than "this far along".
    """
    # The held states share a colour -- they say "look at this" rather than "this far along"
    # -- except the ones that are asking for a person.
    if stage in ("stuck", "error"): return ("red",)
    if stage in JEV_HELD: return ("magenta",)
    if stage in JEV_MOVING:
        i = JEV_MOVING.index(stage)
    elif score is not None:
        i = round(min(max(score, 0), JEV_SCORE_MAX) * (len(JEV_MOVING) - 1) / JEV_SCORE_MAX)
    else:
        return ("cyan",)
    quarter = len(JEV_MOVING) / 4.0
    return (("blue", "cyan", "yellow", "green")[min(3, int(i / quarter))],)
# Herdr reports `idle` and `done` alike as "ready for input"; both count as safe to prompt.
READY_STATUSES = ("idle","done")
STATUS_TTL = 1.0
FRAME_SECONDS = float(os.environ.get("HANDOFF_FRAME_SECONDS", "0.15"))
_STATUS_CACHE = {"at":0.0, "map":{}, "kinds":{}}

def agent_statuses(max_age=STATUS_TTL):
    """Map agent name AND pane id -> (agent_status, current name). One herdr call covers all.

    The name is carried alongside the status because agent names are not durable: a task
    records the name it was sent to, but the pane may host a renamed agent by the time you
    look at the board. Showing the stored name next to a live status otherwise renders a
    name that no longer exists.
    """
    if time.time() - _STATUS_CACHE["at"] < max_age: return _STATUS_CACHE["map"]
    data = herdr("agent","list", timeout=5)
    if not data: return _STATUS_CACHE["map"]     # keep the last known map; retry next tick
    m, kinds = {}, {}
    for ag in ((data.get("result") or {}).get("agents") or []):
        st = ag.get("agent_status")
        if not st: continue
        entry = (st, ag.get("name"))
        for key in (ag.get("name"), ag.get("pane_id")):
            if not key: continue
            m[key] = entry
            if ag.get("agent"): kinds[key] = ag["agent"]
    _STATUS_CACHE.update(at=time.time(), map=m, kinds=kinds)
    return m

def agent_kind(pane, name=None):
    """Which tool is running at one end of a task, for the colour its column is drawn in.

    Resolved the way `_lookup_status` resolves the status itself -- pane first, then name --
    because a task records the pane it was sent to, and an agent that has since been renamed
    or moved is only findable under its current name.
    """
    kinds = _STATUS_CACHE["kinds"]
    return kinds.get(pane) or (kinds.get(name) if name else None)

_PANE_TAB_CACHE = {"at":0.0, "map":{}}

def pane_tabs(max_age=STATUS_TTL):
    """Map pane id -> tab id. Unlike `agent list`, this still knows panes whose agent has gone,
    which is the only way to name the tab of a task whose Target has since disappeared."""
    if time.time() - _PANE_TAB_CACHE["at"] < max_age: return _PANE_TAB_CACHE["map"]
    data = herdr("pane","list", timeout=5)
    if not data: return _PANE_TAB_CACHE["map"]
    m = {p["pane_id"]: p["tab_id"]
         for p in ((data.get("result") or {}).get("panes") or [])
         if p.get("pane_id") and p.get("tab_id")}
    _PANE_TAB_CACHE.update(at=time.time(), map=m)
    return m

def _lookup_status(statuses, name, pane):
    """Resolve one side of a task to a live agent.

    The pane is asked first because it is what the task is actually bound to -- record_identity
    refreshes it on every protocol command -- while a name is only a hint. Names get reused: if
    another agent has since taken the old name, asking by name returns that impostor and the
    renamed original, still sitting in its pane, is never found.
    """
    entry = statuses.get(pane) if pane else None
    return entry if entry is not None else (statuses.get(name) if name else None)

def pane_ref(pane, tab=None):
    """Return Herdr's native pane locator; tab is display-only metadata."""
    return pane or None

def live_label(entry, stored_name, pane=None, tab=None):
    """What to print for one side of a task.

    A resolvable agent is shown by the name it goes by now. When it cannot be resolved the
    name recorded at send time is worthless -- that is precisely the name that no longer
    exists -- so the ? takes its place and the pane's coordinates are printed instead.
    """
    if entry and entry[1]: return entry[1]
    ref = pane_ref(pane, tab)
    return ("?:" + ref) if ref else stored_name

NOT_READY_REASON = {"working": "it is working, not idle",
                    "blocked": "it is waiting on an approval dialog",
                    "unknown": "its state is unknown"}

def not_ready_reason(entry):
    if entry is None: return "it is no longer in Herdr"
    return NOT_READY_REASON.get(entry[0], "it is %s" % entry[0])

def stop_requested():
    """Whether a stop request is addressed to this process.

    A bare stop file (empty, as plain `touch` writes it) means "whichever daemon is running",
    which keeps a hand-issued `touch daemon.stop` working. `daemon stop` writes the pid it
    means, so a request aimed at a daemon that has already exited cannot land on its successor.
    """
    try: target = (ROOT/"daemon.stop").read_text().strip()
    except FileNotFoundError: return False
    return target in ("", str(os.getpid()))

def _flock(path, retries=0):
    """Take an exclusive advisory lock. Returns the handle, or None if it is held elsewhere.

    Keeps the handle alive for as long as the lock is wanted; closing it releases the lock. The
    kernel releases it too if the holder dies, which is the property a pid file cannot promise.
    """
    ROOT.mkdir(parents=True, exist_ok=True)
    for attempt in range(retries + 1):
        fh = open(path, "a")            # append: never truncate, just create if missing
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fh
        except BlockingIOError:
            fh.close()
            if attempt < retries: time.sleep(0.1)   # the board probes this lock every second
        except OSError:
            fh.close(); raise       # a real IO error must not masquerade as "already running"
    return None

def daemon_lock(retries=0):
    """The lock a running daemon holds for its whole life."""
    return _flock(ROOT/"daemon.lock", retries)

def lifecycle_lock(retries=0):
    """Serialises start against stop.

    Without it, `stop` could check that a daemon was running, have that daemon exit, and then
    read the pid of the daemon that replaced it -- addressing the request at the wrong instance,
    which would dutifully stop. Holding this across the running-check, the pid read and the
    request write makes the decision atomic with respect to a start, which needs it too.
    """
    return _flock(ROOT/"daemon.lifecycle", retries)

NO_DAEMON = "no daemon is running"

def request_stop():
    """Ask the daemon that is running now to stop. Returns (ok, message)."""
    guard = lifecycle_lock(retries=3)
    if guard is None: return False, "another start or stop is in progress; try again"
    try:
        if not daemon_running(): return False, NO_DAEMON
        # Read the pid inside the guard: a replacement cannot have taken over since the check,
        # so this names the daemon the request is actually meant for.
        try: target = (ROOT/"daemon.pid").read_text().strip()
        except (FileNotFoundError, ValueError): target = ""
        try: (ROOT/"daemon.stop").write_text(target)
        except OSError: return False, "could not write the stop file"
        return True, "stopping"
    finally:
        guard.close()

def daemon_running():
    """True only while a daemon actually holds the lock."""
    if not (ROOT/"daemon.lock").exists(): return False
    fh = daemon_lock()
    if fh is None: return True                  # the lock is held: a daemon is alive
    fh.close(); return False

def board_items():
    """Query tasks and enrich each with routing, computed age and ownership."""
    me = os.environ.get("HERDR_PANE_ID")
    c = conn()
    try: rows = c.execute("select * from tasks order by state_since").fetchall()
    finally: c.close()                      # the board redraws every second; do not leak connections
    items = []
    for r in rows:
        action = r["action"]; to_target = action in ("take","done")
        actor = r["target_pane"] if to_target else r["source_pane"]
        actor_pane = r["target_pane"] if to_target else r["source_pane"]
        task_started_at = r["task_started_at"] or r["state_since"]
        previous_node_started_at = r["previous_node_started_at"] or task_started_at
        node_started_at = r["state_since"] or task_started_at
        state_started_at = datetime.fromisoformat(r["state_since"])
        if r["state"] in CLOSED_STATES:
            # A terminal row is no longer active, so its state duration ends at the transition
            # that closed it. Legacy rows without last_action_at use state_since and show 0s.
            age_end = datetime.fromisoformat(r["last_action_at"] or r["state_since"])
            age_seconds = age_end.timestamp() - state_started_at.timestamp()
        else:
            age_seconds = time.time() - state_started_at.timestamp()
        items.append({
            "id": r["id"], "desc": r["description"].replace("\n"," / "),
            "src_agent": "", "src_pane": r["source_pane"],
            "dst_agent": "", "dst_pane": r["target_pane"],
            "route": "%s → %s" % (r["source_pane"], r["target_pane"]),
            "state": r["state"], "action": action, "actor": actor,
            "mine": (bool(me) and actor_pane == me and action != "none"
                     and r["state"] not in CLOSED_STATES),
            "closed": r["state"] in CLOSED_STATES,
            # A phase qualifies a live `working` status; on a closed task the daemon no longer
            # refreshes it, so showing it there would only report something stale.
            "src_phase": r["source_phase"] if r["state"] not in CLOSED_STATES else None,
            "dst_phase": r["target_phase"] if r["state"] not in CLOSED_STATES else None,
            "src_stage": r["source_stage"] if r["state"] not in CLOSED_STATES else None,
            "dst_stage": r["target_stage"] if r["state"] not in CLOSED_STATES else None,
            "age": _human_age(age_seconds),
            "since": node_started_at,
            "task_start": _display_time(task_started_at),
            "previous_node_start": _display_time(previous_node_started_at),
        })
    # Active tasks come first. Within each group, the task whose current node started most
    # recently comes first, so the board's top rows reflect the latest activity.
    items.sort(key=lambda x: x["since"], reverse=True)
    items.sort(key=lambda x: x["closed"])
    return items

MARK_W = 4          # cursor glyph + "[x]" checkbox
LEGEND = [("↑↓", "move"), ("space", "select"), ("a", "all"), ("r", "resend"),
          ("s", "jev score"), ("d", "delete"), ("t", "start/stop daemon"), ("q", "quit")]

def _legend_segments():
    """Keys rendered bright so they stand out from their dim descriptions."""
    segs = []
    for idx, (key, label) in enumerate(LEGEND):
        if idx: segs.append(("  ·  ", ("dim",)))
        segs.append((key, ("cyan",)))
        segs.append((" " + label, ("dim",)))
    return segs

BOARD_CHROME = 6      # title, rule, column header, blank, message line, legend

def render_board(width=100, selected=None, cursor=None, statuses=None, items=None,
                 message=None, height=None, tabs=None):
    selected = selected or frozenset()
    if statuses is None: statuses = agent_statuses()
    if items is None: items = board_items()
    if tabs is None: tabs = pane_tabs()
    frame_width = max(1, width)
    scrollbar_width = 1 if frame_width > 1 and items else 0
    content_width = frame_width - scrollbar_width
    for i in items:
        i["src_status"] = _lookup_status(statuses, i["src_agent"], i["src_pane"])
        i["dst_status"] = _lookup_status(statuses, i["dst_agent"], i["dst_pane"])
        # Live pane -> tab mapping first: a pane can be moved between tabs, and the mapping
        # survives the agent. The tab recorded at send time is only a fallback.
        i["src_label"] = live_label(i["src_status"], i["src_agent"], i["src_pane"],
                                    None)
        i["dst_label"] = live_label(i["dst_status"], i["dst_agent"], i["dst_pane"],
                                    None)
        i["src_known"] = bool(i["src_status"] and i["src_status"][1])
        i["dst_known"] = bool(i["dst_status"] and i["dst_status"][1])
        i["route"] = "%s → %s" % (i["src_label"], i["dst_label"])
        # ACTION names the actor too, so it needs the same resolution as the SRC/DST columns.
        # The action determines which side must act. Agent names are runtime metadata and
        # are intentionally absent from the task record, so never compare stored names here.
        # Keep render_board useful with hand-built items in callers and with rows from an older
        # store that only has state_since. conn() backfills the database value, but this fallback
        # keeps the renderer's contract independent of that migration detail.
        task_started_at = i.get("task_started_at") or i.get("since")
        i["task_start"] = i.get("task_start") or _display_time(task_started_at)
        i["previous_node_start"] = i.get("previous_node_start") or _display_time(
            i.get("previous_node_started_at") or task_started_at)

    def next_cell(i):
        if i["action"] == "none": return "—", ("dim",)
        label = ACTION_LABEL.get(i["action"], i["action"])
        # With Jev's answers on file they replace the pending command: STATE already implies it
        # (active -> done), and "working 40.11%" says more than "done". Neither form names an
        # agent -- SRC/DST already do, and which of the two is lit is what says whose move it
        # is. The ▶ row keeps its command: that one is the keystroke you owe. Hand-built items
        # may not carry the stage or phase keys.
        # Two answers, shown side by side and not derived from each other: which rung Jev
        # picked, and how far up the scale it put the task. Either can be missing on its own.
        side = "dst" if i["action"] in ("take", "done") else "src"
        stage, phase = i.get(side + "_stage"), i.get(side + "_phase")
        if stage or phase is not None:
            bits = [stage] if stage in JEV_STATE_OPTIONS else []
            score = None
            if phase is not None:
                score = float(phase)
                bits.append("%.2f%%" % (score * 100 / JEV_SCORE_MAX))
            text = " ".join(bits)
            if i["mine"]: return "▶ %s · %s" % (label, text), ("yellow",)
            return text, (stage_style(stage, score),)
        if i["mine"]: return "▶ %s" % label, ("yellow",)
        return label, ("cyan",)

    def name_of(i, which): return i["src_label"] if which == "src" else i["dst_label"]
    def stat_of(i, which):
        entry = i["src_status"] if which == "src" else i["dst_status"]
        return entry[0] if entry else None

    def widest(header, values):
        return max([_dw(header)] + [_dw(v) for v in values])

    kid = widest("ID", [i["id"] for i in items])
    kst = widest("STATE", [i["state"] for i in items])
    kag = widest("AGE", [i["age"] for i in items])
    knx = widest("PROCESS", [next_cell(i)[0] for i in items])
    krt = widest("ROUTE", [i["route"] for i in items])
    ksn = widest("SRC", [name_of(i,"src") for i in items])
    kdn = widest("DST", [name_of(i,"dst") for i in items])
    kts = widest("START", [i["task_start"] for i in items])
    kpn = widest("PREV", [i["previous_node_start"] for i in items])

    def status_cell(i, which, dim):
        """(plain, segments) for one SRC/DST cell.

        Names are padded to the column's widest so the status words line up down the board,
        and the name is left unstyled while only the status carries colour -- the eye
        should land on the state, not on the agent name. The status keeps that colour on a
        dimmed row too: the STATE column does, and greying one but not the other left a
        finished row with a coloured state and apparently uncoloured agent statuses.
        """
        name, st = name_of(i, which), stat_of(i, which)
        name_w = ksn if which == "src" else kdn
        known = i["src_known"] if which == "src" else i["dst_known"]
        # Herdr's own word, live. Jev's reading of this agent goes in the PROCESS column,
        # which is where a judgment belongs -- this column is a fact that is re-read every
        # frame, and a word written there at the last `s` would be a fact from the past.
        word = "absent" if st is None else st
        if st is None: style = ("red",)
        else: style = STATUS_STYLE.get(st, ("dim",))
        # The word is painted in its tool's colour at both ends -- which tool is where is the
        # column's job now -- and which end is being waited on is carried by the name beside
        # it (a wave and an underline, or neither). A red word is an alarm and keeps its
        # colour. A closed row is one grey from end to end.
        pending = REMINDER_RECIPIENT.get(i["action"])
        side = "source" if which == "src" else "target"
        if dim and "red" not in style: style = ("closed",)
        # `?:pane` means the agent could not be resolved. It stays red even on a dimmed row,
        # the same way the status word keeps its colour there.
        # Two things are said by one name. Which tool is at this end is the colour: each kind
        # has its own, resolved the way the status was (pane first, then name). Which end owes
        # the next step is the wave running along it plus an underline -- shades of that same
        # colour, so the two never fight. Styles, not characters, so the width is unchanged.
        kind = agent_kind(i["%s_pane" % which], i["%s_agent" % which])
        name_base = (("dim",) if dim else ()) if known else ("red",)
        padding = " " * max(0, name_w - _dw(name)) + " "
        if not known or kind is None:
            segs = [(name, name_base)]
        elif dim:
            segs = [(name, ("closed",))]
        elif pending != side:
            segs = [(name, agent_style(kind))]
        else:
            now = time.time()
            segs = [(ch, agent_style(kind, pulse_level(now, i)))
                    for i, ch in enumerate(name)]
        if known and not dim and pending == side:
            segs = [(ch, st + ("underline",)) for ch, st in segs]
        return name + padding + word, segs + [(padding, name_base), (word, style)]

    ksf = widest("SRC", [status_cell(i,"src",False)[0] for i in items])
    kdf = widest("DST", [status_cell(i,"dst",False)[0] for i in items])

    # Narrowing ladder: keep the operational columns visible first, then drop routing, action,
    # and finally the two timestamps. Every removed optional column is accounted for by fixed(),
    # so the description never consumes space reserved by a column on the right.
    routing, show_action = "full", True
    show_task_start, show_previous_node_start = True, True
    MIN_DESC = 12
    def routing_w():
        if routing == "full":  return ksf + kdf
        if routing == "names": return ksn + kdn
        if routing == "route": return krt
        return 0
    def routing_count():
        if routing in ("full", "names"): return 2
        return 1 if routing == "route" else 0
    def fixed():
        widths = MARK_W + kid + kst + kag + routing_w()
        count = 5 + routing_count()       # MARK, ID, DESCRIPTION, STATE, AGE
        if show_action: widths += knx; count += 1
        if show_task_start: widths += kts; count += 1
        if show_previous_node_start: widths += kpn; count += 1
        return widths + 2 * (count - 1)
    while content_width - fixed() < MIN_DESC:
        if routing == "full": routing = "names"
        elif routing == "names": routing = "route"
        elif show_action: show_action = False
        elif routing == "route": routing = "none"
        elif show_previous_node_start: show_previous_node_start = False
        elif show_task_start: show_task_start = False
        else: break
    desc_w = max(1, content_width - fixed())

    def row(cells, bg=()):
        """Each cell is (text_or_segments, styles, width, align); last cell is not padded.

        `bg` is applied to every cell and to the separators between them. Wrapping the finished
        line in a background would not survive: every cell ends with its own reset. A highlighted
        row gets trailing background-filled cells so its highlight reaches the board edge.
        """
        parts = []
        visible = 0
        for idx, (text, styles, w, align) in enumerate(cells):
            if isinstance(text,(list,tuple)):
                segs = list(text)
            else:
                if _dw(text) > w: text = _fit(text, w)   # a cell must never exceed its column
                segs = [(text, styles)]
            if idx < len(cells)-1:
                gap = max(0, w - _dw("".join(s for s,_ in segs)))
                segs = ([(" "*gap, ())] + segs) if align == "r" else (segs + [(" "*gap, ())])
            parts.append("".join(_paint(s, *(tuple(bg) + tuple(st))) for s, st in segs))
            visible += _dw("".join(s for s,_ in segs)) + (2 if idx else 0)
        rendered = _paint("  ", *bg).join(parts)
        if bg:
            return rendered + _paint(" " * max(0, content_width - visible), *bg)
        return rendered.rstrip()

    def routing_cells(i, dim, header):
        if header:
            if routing == "full":  return [("SRC",("dim",),ksf,"l"), ("DST",("dim",),kdf,"l")]
            if routing == "names": return [("SRC",("dim",),ksn,"l"), ("DST",("dim",),kdn,"l")]
            if routing == "route": return [("ROUTE",("dim",),krt,"l")]
            return []
        if routing == "full":  return [(status_cell(i,"src",dim)[1],(),ksf,"l"),
                                       (status_cell(i,"dst",dim)[1],(),kdf,"l")]
        if routing == "names": return [(name_of(i,"src"),dim,ksn,"l"), (name_of(i,"dst"),dim,kdn,"l")]
        if routing == "route": return [(i["route"], dim, krt, "l")]
        return []

    def assemble(mark, _id, desc, state, nxt, age, task_start, previous_node_start,
                 ms, ds, ss, ns, as_, ts, ps, i=None, header=False, bg=()):
        cells = [(mark, ms, MARK_W, "l"), (_id, ds, kid, "l"), (desc, ds, desc_w, "l")]
        cells.append((state, ss, kst, "l"))
        cells += routing_cells(i, ds, header)
        if show_action: cells.append((nxt, ns, knx, "l"))
        cells.append((age, as_, kag, "r"))
        if show_task_start: cells.append((task_start, ts, kts, "l"))
        if show_previous_node_start: cells.append((previous_node_start, ps, kpn, "l"))
        return row(cells, bg)

    n = len(items)
    # Bound the frame to the terminal height. A frame taller than the pane scrolls on every
    # redraw, which shifts what \033[H means and leaves the previous frame's tail on screen.
    start, window = 0, items
    if height is not None and items:
        room = max(1, height - BOARD_CHROME)
        if len(items) > room:
            start = max(0, min((cursor or 0) - room + 1, len(items) - room))
            window = items[start:start + room]

    def scrollbar_slots(total, visible, offset):
        if not total or not visible: return []
        if total <= visible:
            return [("│", ("dim",))] * visible
        thumb = max(1, (visible * visible + total - 1) // total)
        travel = visible - thumb
        maximum_offset = total - visible
        thumb_start = (offset * travel + maximum_offset // 2) // maximum_offset
        slots = [("│", ("dim",))] * visible
        for idx in range(thumb_start, thumb_start + thumb):
            slots[idx] = ("█", ("scroll_thumb",))
        return slots

    scroll_slots = scrollbar_slots(n, len(window), start)
    row_backgrounds = []
    awaiting = sum(1 for i in items if i["mine"] and not i["closed"])
    daemon_on = daemon_running()
    # Header is built from segments so it can be truncated instead of overrunning a narrow pane.
    segs = [("Handoff", ()), (" · %d task%s" % (n, "" if n == 1 else "s"), ("dim",))]
    if len(window) < n:                      # say which slice is on screen, so nothing looks lost
        segs.append((" · showing %d-%d of %d" % (start + 1, start + len(window), n), ("dim",)))
    if awaiting: segs.append((" · %d waiting on you" % awaiting, ("yellow",)))
    segs += [(" · daemon ", ("dim",)),
             ("on" if daemon_on else "off", ("green",) if daemon_on else ("dim",))]
    clock = datetime.now().strftime("%H:%M:%S")
    head, used = _fit_segments(segs, max(0, content_width - len(clock) - 1))
    lines = ["".join(_paint(t, *s) for t, s in head)
             + " " * max(1, content_width - used - len(clock)) + _paint(clock, "dim"),
             _paint("─" * max(1, content_width), "dim")]
    lines.append(assemble("", "ID", "DESCRIPTION", "STATE", "PROCESS", "AGE",
                          "START", "PREV",
                          ("dim",), ("dim",), ("dim",), ("dim",), ("dim",),
                          ("dim",), ("dim",), header=True))
    if not items:
        lines.append(_paint(_fit("  No tasks yet — create one with: handoff send", frame_width), ("dim",)))
    for idx, i in enumerate(window):
        here = start + idx == cursor
        ds = ("closed",) if i["closed"] else ()
        nxt, ns = next_cell(i)
        picked = i["id"] in selected
        glyph = ">" if here else ("▸" if i["mine"] else " ")
        box = "[x]" if picked else "[ ]"
        ms = ("cyan",) if here else (("yellow",) if i["mine"] else ds)
        bg = ("bg_cursor",) if here else (("bg_picked",) if picked else ())
        state_style = STATE_STYLE.get(i["state"], ())
        if i["closed"] and "red" not in state_style: state_style = ds
        lines.append(assemble(glyph + box, i["id"], _fit(i["desc"], desc_w),
                              i["state"], nxt, i["age"], i["task_start"],
                              i["previous_node_start"], ms, ds,
                              state_style, ns, ds, ds, ds, i=i, bg=bg))
        row_backgrounds.append(bg)
    # The last two lines are fixed furniture: the message line, then the key legend.
    # The message line is reserved even when empty so the board never shifts under a keypress.
    lines.append("")
    lines.append(_paint(_fit(message[0], frame_width), *message[1]) if message else "")
    lines.append("".join(_paint(t, *s) for t, s in _fit_segments(_legend_segments(), frame_width)[0]))

    if scrollbar_width:
        def edge_line(line, marker=" ", styles=()):
            padding = " " * max(0, content_width - _visible_dw(line))
            return line + padding + _paint(marker, *styles)

        decorated = []
        for line_no, line in enumerate(lines):
            if 3 <= line_no < 3 + len(window):
                slot = line_no - 3
                marker, styles = scroll_slots[slot]
                styles = tuple(row_backgrounds[slot]) + tuple(styles)
                decorated.append(edge_line(line, marker, styles))
            else:
                decorated.append(line)
        lines = decorated
    return "\n".join(lines)

# ---------- interactive board ----------

def terminal_size(fallback=(110, 30)):
    """The terminal's real size, asked of the tty itself.

    Deliberately not shutil.get_terminal_size(): that prefers $COLUMNS and $LINES, which go
    stale the moment a pane is split or resized. Measured in a pty sized 80x20 with a leftover
    COLUMNS=200, the ioctl reports 80 columns while shutil reports 200 -- so the board rendered
    wider than its pane and the right-hand columns were clipped, AGE reading as "AG" and the
    clock losing its last digit.
    """
    try:
        size = os.get_terminal_size(sys.stdout.fileno())
        # The final physical cell is a wrap boundary on common terminals. Leaving it unused
        # prevents a full-width timestamp or clock from losing its last character at the pane edge.
        return max(1, size.columns - 1), size.lines
    except (OSError, ValueError):
        return max(1, fallback[0] - 1), fallback[1]

def frame_bytes(frame):
    """Encode a frame for the terminal, erasing each line's tail as it is written.

    Overwriting a long message with a shorter one otherwise strands the old tail on screen:
    writing text does not clear the rest of the line, and a trailing \\033[J only clears below
    the cursor, which by then sits on the last line.
    """
    return "\033[H" + "\033[K\r\n".join(frame.split("\n")) + "\033[K\033[J"

def _read_key(timeout):
    """Return a key name ("UP"/"DOWN"/"ESC"), a literal character, or None on timeout.

    Reads the raw fd, never sys.stdin. The buffered reader would swallow a whole escape
    sequence into Python's buffer, leaving the fd empty so the next select() reports no
    data: every arrow key would degrade to a bare Escape and the leftover bytes would
    desynchronise the keys that follow.
    """
    fd = sys.stdin.fileno()
    def ready(t): return bool(select.select([fd], [], [], t)[0])
    if not ready(timeout): return None
    ch = os.read(fd, 1).decode("utf-8", "replace")
    if ch != "\033": return ch
    if not ready(0.03): return "ESC"                                       # bare Escape
    if os.read(fd, 1) != b"[": return "ESC"
    if not ready(0.03): return "ESC"
    return {b"A":"UP", b"B":"DOWN", b"C":"RIGHT", b"D":"LEFT"}.get(os.read(fd, 1))

def _confirm_yes(key):
    """The confirmation prompt is [y/N]: only an explicit y confirms."""
    return key in ("y", "Y")

def toggle_daemon():
    """The daemon is a foreground process, so start it detached; stopping goes through the guard."""
    ok, message = request_stop()
    if ok: return "Stopping the daemon"
    if message != NO_DAEMON: return message      # the guard was busy, or the write failed
    subprocess.Popen([sys.executable, CLI, "daemon", "start"],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)
    return "Starting the daemon"

# Only these states leave something for the Target to do. A finished task must not be
# re-sent, and neither must one whose ball is already in the Source's court.
RESENDABLE_ACTIONS = {
    "published": "take",
    "active": "done",
}

def resend_blocked_reason(state):
    """Why a task's own state rules out a re-send. Phrased to follow "N tasks skipped — "."""
    if state in CLOSED_STATES:
        return "already %s" % state
    return "the Source has it now"

def resend_tasks(ids, statuses, tabs=None):
    """Re-deliver each task's prompt, but only to a Target that is ready for input."""
    if tabs is None: tabs = pane_tabs()
    c = conn()
    try: rows = [c.execute("select * from tasks where id=?", (tid,)).fetchone() for tid in ids]
    finally: c.close()
    sent, skipped, failed = [], [], []
    for row in rows:
        if not row: continue
        if RESENDABLE_ACTIONS.get(row["state"]) != row["action"]:
            skipped.append((resend_blocked_reason(row["state"]), None))   # counted, not named
            continue
        # State can change while the board is deciding what to resend.  Re-read immediately
        # before delivery; an invalidated resend is discarded rather than replayed later.
        fresh_conn = conn()
        try:
            fresh = fresh_conn.execute("select * from tasks where id=?", (row["id"],)).fetchone()
        finally:
            fresh_conn.close()
        if not fresh or RESENDABLE_ACTIONS.get(fresh["state"]) != fresh["action"]:
            skipped.append(("state changed", None))
            continue
        row = fresh
        entry = _lookup_status(statuses, None, row["target_pane"])
        who = live_label(entry, None, row["target_pane"], None)
        if entry is None or entry[0] not in READY_STATUSES:
            skipped.append((not_ready_reason(entry), who)); continue
        # Address the pane's current occupant by its live name, or the raw pane id. Never the
        # display label: that may be a shortened pane reference herdr cannot resolve.
        target = (entry[1] if entry[1] else None) or row["target_pane"]
        if prompt(target, task_text(row, resend=True)) is None: failed.append(who)
        else: sent.append(who)
    return sent, skipped, failed

def resend_summary(sent, skipped, failed):
    """One line describing what a re-send did, in counts rather than task ids.

    An id would be the longest thing on the line and says nothing the board above does not
    already show. How many were left alone, and why, is what the message is for.
    """
    parts = []
    if sent:
        parts.append("Re-sent %d task%s to %s"
                     % (len(sent), "" if len(sent) == 1 else "s", ", ".join(sorted(sent))))
    counts = {}
    for reason, who in skipped:
        if who is None: counts[reason] = counts.get(reason, 0) + 1     # count task-level reasons
        else: parts.append("%s skipped — %s" % (who, reason))          # agent-level ones name the agent
    for reason, n in counts.items():
        parts.append("%d task%s skipped — %s" % (n, "" if n == 1 else "s", reason))
    for name in failed: parts.append("Could not reach %s" % name)
    return " · ".join(parts) or "No task was re-sent"

# A task in one of these states ended without a result and will never move again. `finished`
# and `rejected` are deliberate outcomes somebody may still want to read, so they stay.
INVALID_STATES = ("target_absent", "source_absent", "timeout", "cancelled")

# How long a terminal record stays before `clean old` collects it.
CLEAN_OLD_DAYS = 7

def invalid_task_ids(c):
    """Ids of the tasks that ended without a result -- what a board collects and never reads."""
    marks = ",".join("?" * len(INVALID_STATES))
    return [r["id"] for r in c.execute("select id from tasks where state in (%s)" % marks,
                                       INVALID_STATES)]

def old_task_ids(c, days=CLEAN_OLD_DAYS):
    """Terminal tasks that have sat unchanged for more than `days`.

    A terminal state never moves again, so `state_since` on one of these rows is the moment the
    task ended. Open tasks are never collected: deleting one would leave a Target holding a task
    id the service no longer knows, and its eventual `done` would be refused as unknown.
    """
    cutoff = datetime.now(timezone.utc).timestamp() - days * 86400
    marks = ",".join("?" * len(CLOSED_STATES))
    return [r["id"] for r in c.execute("select id, state_since from tasks where state in (%s)"
                                       % marks, tuple(CLOSED_STATES))
            if datetime.fromisoformat(r["state_since"]).timestamp() < cutoff]

def clean_task_ids(c, what, days=CLEAN_OLD_DAYS):
    if what == "invalid": return invalid_task_ids(c)
    if what == "old": return old_task_ids(c, days)
    raise SystemExit("clean takes `invalid` or `old`")

def actor_side(row):
    """Which side owes the next action. The Jev score columns follow this same rule."""
    return REMINDER_RECIPIENT.get(row["action"]) or "source"

def target_owes(row):
    """Whether the Target holds the task up -- the only end whose idleness is worth a question."""
    return REMINDER_RECIPIENT.get(row["action"]) == "target"

def review_rows(c):
    """The tasks an on-demand review covers: every open one, oldest first."""
    marks = ",".join("?" * len(CLOSED_STATES))
    return c.execute("select * from tasks where state not in (%s) order by state_since" % marks,
                     tuple(CLOSED_STATES)).fetchall()

def review_state(rows, snapshots):
    """What the review shows Jev: each task's whole timeline, not just the terminal now.

    A snapshot alone cannot tell a task that just started from one that has been retried
    four times, so the record a task carries -- when it was registered, which node it came
    from, what it last did, how many nudges it has spent, and the score a previous review
    left -- goes in alongside it. The snapshot is the one that owes the next step: that is
    the side the score is about, and the only one whose idleness says anything about the task.
    The prompt is truncated to keep the request bounded.
    """
    return {"now": now(), "tasks": [
        {"id": r["id"], "description": r["description"],
         "prompt": r["prompt"][:JEV_PROMPT_CHARS],
         "state": r["state"], "pending_action": r["action"],
         "task_started_at": r["task_started_at"],
         "state_since": r["state_since"],
         "previous_node_started_at": r["previous_node_started_at"],
         "last_action": r["last_action"], "last_action_at": r["last_action_at"],
         "retry_count": r["retry_count"], "error": r["error"],
         "source": {"pane": r["source_pane"], "presence": r["source_presence"],
                    "lifecycle": r["source_lifecycle"], "previous_score": r["source_phase"]},
         "target": {"pane": r["target_pane"], "presence": r["target_presence"],
                    "lifecycle": r["target_lifecycle"], "previous_score": r["target_phase"]},
         "agent_terminal_snapshot": (snapshots.get(r["id"]) or "")[-JEV_SNAPSHOT_CHARS:]}
        for r in rows]}

def review_questions(rows):
    """One score and one idle question per task, asked together so the board costs one request."""
    questions = {}
    for r in rows:
        questions[r["id"]] = {
            "type": "score",
            "instructions": "How far along is task `%s` (%s) toward a finished result? "
                            "Answer on the 0-9 scale below, where 0 is nothing done and 9 is "
                            "finished -- a position, not a description of what the agent "
                            "happens to be doing. Weigh the terminal snapshot against the "
                            "recorded history: elapsed time, retries, and the action it has "
                            "been sitting on." % (r["id"], r["description"].replace("\n", " ")[:120]),
            "criteria": JEV_SCORE_LEVELS}
        questions["%s_state" % r["id"]] = state_question(r)
    return questions

def jev_score_all(c):
    """Score every open task in one Jev request. Returns (scored, unanswered) counts.

    Nothing is written when the request fails, so a board with no key configured or a Jev
    outage leaves the previous scores standing rather than blanking them.
    """
    rows = review_rows(c)
    if not rows or not jev_enabled(): return {}, len(rows)
    snapshots = {r["id"]: agent_read(r[actor_side(r) + "_pane"]) or "" for r in rows}
    answers = jev_ask(review_state(rows, snapshots), review_questions(rows), partial=True)
    scored = {}
    for r in rows:
        text = jev_score_text((answers or {}).get(r["id"]))
        if text is not None:
            c.execute("update tasks set %s_phase=? where id=?" % actor_side(r), (text, r["id"]))
            scored[r["id"]] = text
        stage = (answers or {}).get("%s_state" % r["id"])
        if stage in JEV_STATE_OPTIONS:
            c.execute("update tasks set %s_stage=? where id=?" % actor_side(r), (stage, r["id"]))
    c.commit()
    return scored, len(rows) - len(scored)

def review_summary(scored, unanswered):
    """One line for the board: what the review managed, in counts rather than ids."""
    if not scored and not unanswered: return "No open task to score"
    if not scored:
        return "Jev could not score %d task%s — scores unchanged" % (
            unanswered, "" if unanswered == 1 else "s")
    line = "Scored %d task%s" % (len(scored), "" if len(scored) == 1 else "s")
    if unanswered: line += " · %d unanswered" % unanswered
    return line

def ui(_):
    if not sys.stdout.isatty():
        print(render_board(terminal_size()[0])); return
    _use_color()
    fd = sys.stdin.fileno(); saved = termios.tcgetattr(fd)
    selected, cursor = set(), 0
    mode, pending, flash = None, [], None
    statuses, tabs, last_status = {}, {}, 0.0
    try:
        attr = termios.tcgetattr(fd)
        attr[3] &= ~(termios.ICANON | termios.ECHO)   # cbreak; keep ISIG so Ctrl-C still works
        termios.tcsetattr(fd, termios.TCSADRAIN, attr)
        sys.stdout.write("\033[?25l\033[H\033[2J")
        while True:
            width, height = terminal_size()
            if time.time() - last_status >= STATUS_TTL:
                statuses = agent_statuses(max_age=0); tabs = pane_tabs(max_age=0)
                last_status = time.time()
            items = board_items()
            if selected: selected &= {i["id"] for i in items}   # drop ids that vanished
            cursor = max(0, min(cursor, len(items)-1)) if items else 0
            if flash and time.time() > flash[1]: flash = None
            if mode == "confirm_delete":
                message = ("Delete %d selected task%s?   [y/N]"
                           % (len(pending), "" if len(pending) == 1 else "s"), ("red",))
            elif mode == "confirm_daemon":
                message = ("Stop the daemon?   [y/N]" if daemon_running()
                           else "Start the daemon?   [y/N]", ("red",))
            elif flash: message = (flash[0], ("yellow",))
            else: message = None
            frame = render_board(width, selected, cursor, statuses, items,
                                 message, height=height, tabs=tabs)
            sys.stdout.write(frame_bytes(frame))
            sys.stdout.flush()

            # The operator's name breathes, so the frame is redrawn several times a second
            # rather than once: a one-second tick turned the pulse into a blink.
            key = _read_key(FRAME_SECONDS)
            if key is None: continue
            if mode == "confirm_daemon":
                if _confirm_yes(key): flash = (toggle_daemon(), time.time()+4)
                else: flash = ("Daemon unchanged", time.time()+2)
                mode = None
                continue
            if mode == "confirm_delete":
                if _confirm_yes(key):
                    c = conn()
                    try: n = delete_tasks(c, pending)
                    finally: c.close()
                    selected -= set(pending)
                    flash = ("Deleted %d task%s" % (n, "" if n == 1 else "s"), time.time()+3)
                else:
                    flash = ("Nothing was deleted", time.time()+2)
                mode, pending = None, []
                continue
            if key in ("q","Q"): break
            if key == "UP": cursor = max(0, cursor-1)
            elif key == "DOWN":
                if items: cursor = min(len(items)-1, cursor+1)
            elif key == " ":
                if items:
                    tid = items[cursor]["id"]
                    if tid in selected: selected.discard(tid)
                    else: selected.add(tid)
            elif key == "a":
                ids = {i["id"] for i in items}
                selected = set() if ids and selected >= ids else set(ids)
            elif key == "r":
                if not selected: flash = ("Select a task first — ↑↓ moves, space toggles", time.time()+3)
                else:
                    sent, skipped, failed = resend_tasks(sorted(selected), statuses)
                    flash = (resend_summary(sent, skipped, failed), time.time()+5)
            elif key == "s":
                if not jev_enabled():
                    flash = ("Jev is off — set TYPESAFE_API_KEY to score", time.time()+5)
                elif jev_wait_left() > 0:
                    # Say so rather than silently returning the empty summary a failed call
                    # would also produce: the user pressed a key and deserves to know why
                    # nothing happened.
                    flash = ("Jev was asked too recently — %.0fs to wait" % jev_wait_left(),
                             time.time()+3)
                else:
                    c = conn()
                    try: scored, unanswered = jev_score_all(c)
                    finally: c.close()
                    flash = (review_summary(scored, unanswered), time.time()+5)
            elif key == "d":
                if not selected: flash = ("Select a task first — ↑↓ moves, space toggles", time.time()+3)
                else: pending, mode = sorted(selected), "confirm_delete"
            elif key == "t":
                mode = "confirm_daemon"      # a stray keypress must not stop the service
    except KeyboardInterrupt:
        pass
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
        sys.stdout.write("\033[?25h\n")
        sys.stdout.flush()
def build_parser():
    """Exposed so tests can check that a generated command is one the CLI actually accepts."""
    p=argparse.ArgumentParser(); sp=p.add_subparsers(dest="op",required=True)
    s=sp.add_parser("send"); s.add_argument("--source-pane",required=True); s.add_argument("--target-pane",required=True); s.add_argument("--description",required=True); s.add_argument("--prompt",required=True); s.set_defaults(fn=cmd_send)
    for n in ("take","claim","accept","reject","cancel","done-implicit"):
        x=sp.add_parser(n); x.add_argument("id"); x.add_argument("--reason", "--description", dest="reason", default=""); x.add_argument("--result-file"); x.set_defaults(fn=cmd_action,cmd=n)
        if n in ("take", "claim", "accept", "done-implicit"):
            x.add_argument("--pane", required=True)
    x=sp.add_parser("delete"); x.add_argument("id", nargs="?"); x.add_argument("--state"); x.add_argument("--all", action="store_true"); x.set_defaults(fn=cmd_action,cmd="delete")
    d=sp.add_parser("done"); d.add_argument("id"); d.add_argument("--result-file",required=True); d.add_argument("--implicit-take",action="store_true"); d.add_argument("--pane",required=True); d.set_defaults(fn=cmd_action,cmd="done")
    l=sp.add_parser("list"); l.set_defaults(fn=cmd_list)
    n=sp.add_parser("clean"); n.add_argument("what", choices=("invalid","old")); n.add_argument("--days", type=float, default=CLEAN_OLD_DAYS); n.set_defaults(fn=cmd_clean)
    d=sp.add_parser("daemon"); d.add_argument("op",choices=("start","stop","status")); d.set_defaults(fn=daemon)
    u=sp.add_parser("ui"); u.set_defaults(fn=ui)
    return p

def main():
    a=build_parser().parse_args(); a.fn(a)
if __name__=="__main__": main()
