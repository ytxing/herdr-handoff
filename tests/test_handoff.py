import os
import re
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import http.server
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
FAKE_HERDR = ROOT / "tests" / "fake_herdr.py"

INSERT = ("insert into tasks(id,description,prompt,source_pane,target_pane,state,action,state_since)"
          " values(?,?,?,?,?,?,?,?)")

# A row's age is carried by state_since, so an aged fixture is just an old timestamp.
def ago(days):
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


class JevStub:
    """A local stand-in for the System One endpoint, reached through TYPESAFE_API_URL.

    Isolation sits at the URL rather than by patching jev_ask in-process for the same reason
    herdr isolation sits at HERDR_BIN_PATH: the daemon is a subprocess, and an in-process
    patch never reaches it.
    """

    def __init__(self, answers):
        self.answers = answers
        self.requests = []
        stub = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                stub.requests.append(json.loads(body))
                data = json.dumps({"model": "jev-latest", "answers": stub.answers,
                                   "usage": {"input_tokens": 1, "output_tokens": 1}}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args): pass

        self.server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = "http://127.0.0.1:%d/v1/systemone" % self.server.server_address[1]

    def activate(self):
        os.environ["TYPESAFE_API_KEY"] = "test-key"
        os.environ["TYPESAFE_API_URL"] = self.url

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


class HandoffTestBase(unittest.TestCase):
    """Temp state dir plus a fresh module import. Connections opened via db() are closed in tearDown."""

    def db(self):
        c = self.handoff.conn()
        self._conns.append(c)
        return c

    def run_cli(self, *args):
        env = os.environ.copy()
        env["HANDOFF_STATE_DIR"] = self.tmp.name
        return subprocess.run(["python3", str(ROOT / "handoff.py"), *args], env=env,
                              text=True, capture_output=True)

    def isolate_herdr(self):
        """Route every herdr call through a stub, in-process and subprocess alike.

        HERDR_BIN_PATH rather than a patch of handoff.prompt, because run_cli() shells out to
        the CLI: an in-process patch never reaches the delivery that actually escapes. Without
        this the `done` step of a state-flow test really delivers a [HANDOFF RESULT READY] to
        whichever pane the fixture named -- which is how a live agent pane got a notification
        for a task that never existed.
        """
        shim = Path(self.tmp.name) / "herdr"
        shim.write_text('#!/bin/sh\nexec "%s" "%s" "$@"\n' % (sys.executable, FAKE_HERDR))
        shim.chmod(0o755)
        self._saved_herdr = os.environ.get("HERDR_BIN_PATH")
        os.environ["HERDR_BIN_PATH"] = str(shim)
        self.prompt_log = Path(self.tmp.name) / "prompts.log"
        os.environ["FAKE_HERDR_LOG"] = str(self.prompt_log)

    def delivered(self):
        """Targets the code asked herdr to prompt. Empty means nothing left the test."""
        if not self.prompt_log.exists(): return []
        return [l.split("\t", 1)[0] for l in self.prompt_log.read_text().splitlines() if l]

    def sweeps(self, n=1):
        """Wait for `n` daemon sweeps plus the longest single wait one of them can park on.

        Both terms are read from the module the test's own environment configured, so a test
        written as "two sweeps" stays two sweeps whatever period the suite is running at.
        """
        time.sleep(self.handoff.SWEEP_SECONDS * n
                   + self.handoff.AGENT_WAIT_SLICE_MS / 1000.0 + 0.3)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._conns = []
        os.environ["HANDOFF_STATE_DIR"] = self.tmp.name
        # Jev must be off by default in tests: a developer shell carrying TYPESAFE_API_KEY
        # would otherwise let the daemon tests place real API calls.
        self._saved_jev = {k: os.environ.get(k)
                           for k in ("TYPESAFE_API_KEY", "TYPESAFE_API_URL", "HANDOFF_JEV",
                                     "HANDOFF_JEV_MIN_INTERVAL")}
        for k in self._saved_jev: os.environ.pop(k, None)
        # Tests ask Jev several times in a row on purpose; the floor between requests is a
        # production guard, and the test that wants it sets it for itself.
        os.environ["HANDOFF_JEV_MIN_INTERVAL"] = "0"
        # Removing the key switches Jev off, but the module still resolves its default URL at
        # import, and a test that turns the key back on would send to whatever that resolved
        # to. Pointing it at a closed local port is what makes "no test reaches the live API"
        # a property of the harness rather than of every test remembering to stub.
        os.environ["TYPESAFE_API_URL"] = "http://127.0.0.1:1/dead"
        # The daemon tests wait on a real daemon doing real sweeps, and at the production
        # period that waiting is 45 seconds of this suite -- all of it `time.sleep`. Both
        # clocks are env-tunable, so a test asks for the same number of sweeps in a fraction
        # of the wall time; tests wait through sweeps() so the two stay in step.
        self._saved_timings = {k: os.environ.get(k)
                               for k in ("HANDOFF_SWEEP_SECONDS", "HANDOFF_AGENT_WAIT_SLICE_MS")}
        os.environ["HANDOFF_SWEEP_SECONDS"] = "0.1"
        os.environ["HANDOFF_AGENT_WAIT_SLICE_MS"] = "100"
        self.isolate_herdr()
        sys.path.insert(0, str(ROOT))
        sys.modules.pop("handoff", None)
        import handoff
        self.handoff = handoff

    def tearDown(self):
        for c in self._conns:
            try: c.close()
            except Exception: pass
        for k, v in self._saved_jev.items():
            if v is None: os.environ.pop(k, None)
            else: os.environ[k] = v
        for k, v in self._saved_timings.items():
            if v is None: os.environ.pop(k, None)
            else: os.environ[k] = v
        if self._saved_herdr is None: os.environ.pop("HERDR_BIN_PATH", None)
        else: os.environ["HERDR_BIN_PATH"] = self._saved_herdr
        os.environ.pop("FAKE_HERDR_LOG", None)
        os.environ.pop("HANDOFF_STATE_DIR", None)
        os.environ.pop("HERDR_PANE_ID", None)
        self.tmp.cleanup()


class HandoffCliTests(HandoffTestBase):
    def setUp(self):
        super().setUp()
        c = self.db()
        c.execute(INSERT, ("t_test", "测试任务", "prompt", "wA:pTEST-SRC", "wA:pTEST-DST",
                           "published", "take", self.handoff.now()))
        c.commit()

    def test_core_state_flow(self):
        result_file = Path(self.tmp.name) / "result.md"
        result_file.write_text("ok")
        identity = ('--pane', 'wA:pTEST-DST')
        for command, expected in [(('take', 't_test', *identity), 'active'),
                                  (('done', 't_test', '--result-file', str(result_file), *identity), 'result_ready'),
                                  (('claim', 't_test', '--pane', 'wA:pTEST-SRC'), 'finished')]:
            result = self.run_cli(*command)
            self.assertEqual(result.returncode, 0, result.stderr)
            row = self.db().execute("select state from tasks where id='t_test'").fetchone()
            self.assertEqual(row[0], expected)

    def test_task_and_previous_node_start_times_follow_state_flow(self):
        """The task clock stays fixed while each transition records the node it left."""
        before = self.db().execute(
            "select state_since,task_started_at,previous_node_started_at from tasks where id='t_test'").fetchone()
        self.assertEqual(before["task_started_at"], before["state_since"])
        self.assertIsNone(before["previous_node_started_at"])

        result = self.run_cli("take", "t_test", "--pane", "wA:pTEST-DST")
        self.assertEqual(result.returncode, 0, result.stderr)
        after = self.db().execute(
            "select state_since,task_started_at,previous_node_started_at from tasks where id='t_test'").fetchone()
        self.assertEqual(after["task_started_at"], before["task_started_at"])
        self.assertEqual(after["previous_node_started_at"], before["state_since"])
        self.assertNotEqual(after["state_since"], before["state_since"])

    def test_send_records_a_fixed_task_start_time(self):
        result = self.run_cli("send", "--source-pane", "wA:pTEST-SRC",
                              "--target-pane", "wA:pTEST-NEW", "--description", "new",
                              "--prompt", "work")
        self.assertEqual(result.returncode, 0, result.stderr)
        row = self.db().execute(
            "select state_since,task_started_at,previous_node_started_at from tasks where id=?",
            (result.stdout.strip(),)).fetchone()
        self.assertEqual(row["task_started_at"], row["state_since"])
        self.assertIsNone(row["previous_node_started_at"])

    def test_an_unresolvable_pane_never_overwrites_a_recorded_one(self):
        """A pane Herdr cannot resolve must not replace one that resolves.

        2026-09-15, twice in one afternoon. An agent that could not name its own pane passed a
        placeholder (`unknown`); an earlier one passed the same id with its workspace prefix
        stripped (`p16`). record_identity wrote both in unchecked -- the only writer of these
        columns that trusted its input. The `p16` one stranded a live task: the daemon
        addresses the Target by this value, so every later lookup found no such pane and the
        task was closed as target_absent with its request never delivered.

        The command itself must still go through. Refusing the pane is a repair to the record,
        not a gate on the protocol: an agent with a garbled pane id is still an agent that
        just took the task.
        """
        os.environ["FAKE_HERDR_ABSENT"] = "unknown,p16,bogus"
        try:
            result_file = Path(self.tmp.name) / "r.md"
            result_file.write_text("ok")
            for command in (("take", "t_test", "--pane", "unknown"),
                            ("done", "t_test", "--result-file", str(result_file), "--pane", "p16"),
                            ("claim", "t_test", "--pane", "bogus")):
                result = self.run_cli(*command)
                self.assertEqual(result.returncode, 0, result.stderr)
        finally:
            os.environ.pop("FAKE_HERDR_ABSENT", None)
        row = self.db().execute("select * from tasks where id='t_test'").fetchone()
        self.assertEqual(row["target_pane"], "wA:pTEST-DST",
                         "an unresolvable Target pane must not replace the recorded one")
        self.assertEqual(row["source_pane"], "wA:pTEST-SRC",
                         "an unresolvable Source pane must not replace the recorded one")
        self.assertEqual(row["state"], "finished",
                         "the guard must not block the protocol itself")

    def test_empty_description_rejected_by_parser(self):
        result = self.run_cli("send", "--source-pane", "wA:pTEST-SRC",
                              "--target-pane", "wA:pTEST-DST",
                              "--description", " ", "--prompt", "x")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("description", result.stderr)

    def test_an_old_database_with_duplicates_still_opens(self):
        """The index migration must not brick the tool over data it did not create.

        A store written before the index existed can hold two open tasks for one Target;
        refusing to open it would take the CLI, the daemon and the board down together.
        """
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "handoff.sqlite3"          # conn() opens exactly this name
            raw = sqlite3.connect(db)
            raw.execute("create table tasks(id text primary key, description text not null,"
                        " prompt text not null, source_pane text not null, target_pane text not null,"
                        " state text not null, action text not null, state_since text not null)")
            for tid in ("t_a", "t_b"):
                raw.execute("insert into tasks values(?,?,?,?,?,?,?,?)",
                            (tid, "旧数据", "p", "wA:pX", "wA:pSAME", "active", "done", "2026-01-01"))
            raw.commit(); raw.close()
            saved = self.handoff.ROOT, self.handoff.DB
            self.handoff.ROOT, self.handoff.DB = Path(d), db
            try:
                c = self.handoff.conn()
                self.assertEqual(c.execute("select count(*) from tasks").fetchone()[0], 2,
                                 "the store must still open when the index cannot be created")
                columns = {row[1] for row in c.execute("pragma table_info(tasks)")}
                self.assertIn("task_started_at", columns)
                self.assertIn("previous_node_started_at", columns)
                self.assertIn("source_phase", columns)
                self.assertIn("target_phase", columns)
                self.assertEqual(c.execute("select task_started_at from tasks where id='t_a'").fetchone()[0],
                                 "2026-01-01")
            finally:
                self.handoff.ROOT, self.handoff.DB = saved

    def test_list_warns_about_a_degraded_store(self):
        """A store that could not take the index must not look healthy.

        The migration skips the index when legacy duplicates block it, which silently gives up
        the one-open-task-per-Target guarantee. list is the diagnostic entry point, so it says
        so -- on stderr, leaving the tab-separated stdout that scripts parse untouched.
        """
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "handoff.sqlite3"
            raw = sqlite3.connect(db)
            raw.execute("create table tasks(id text primary key, description text not null,"
                        " prompt text not null, source_pane text not null, target_pane text not null,"
                        " state text not null, action text not null, state_since text not null)")
            for tid in ("t_a", "t_b"):
                raw.execute("insert into tasks values(?,?,?,?,?,?,?,?)",
                            (tid, "旧数据", "p", "wA:pX", "wA:pSAME", "active", "done", "2026-01-01"))
            raw.commit(); raw.close()
            env = os.environ.copy(); env["HANDOFF_STATE_DIR"] = d
            out = subprocess.run(["python3", str(ROOT / "handoff.py"), "list"],
                                 env=env, text=True, capture_output=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("wA:pSAME", out.stderr, "the duplicate Target is named")
        self.assertIn("not in force", out.stderr)
        self.assertEqual(len(out.stdout.strip().splitlines()), 2,
                         "stdout stays the plain task listing that scripts parse")
        self.assertNotIn("not in force", out.stdout,
                         "the warning stays on stderr; scripts parse stdout")

    def test_delete_removes_task(self):
        result = self.run_cli("delete", "t_test")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIsNone(self.db().execute("select * from tasks where id='t_test'").fetchone())

    def clean_fixture(self, rows):
        """Seed one task per (state, age in days). setUp's published task is the open control."""
        c = self.db()
        for tid, state, age in rows:
            c.execute(INSERT, (tid, "d", "p", "wA:pX", "wA:pCLEAN-" + tid, state,
                               "none" if state in self.handoff.CLOSED_STATES else "take", ago(age)))
        c.commit()

    def remaining(self):
        return sorted(r[0] for r in self.db().execute("select id from tasks"))

    def test_clean_invalid_collects_only_the_ones_without_a_result(self):
        self.clean_fixture([("t_absent", "target_absent", 1), ("t_sabsent", "source_absent", 1),
                            ("t_timeout", "timeout", 1), ("t_cancel", "cancelled", 1),
                            ("t_fin", "finished", 1), ("t_rej", "rejected", 1)])
        result = self.run_cli("clean", "invalid")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("deleted 4", result.stdout)
        self.assertEqual(self.remaining(), ["t_fin", "t_rej", "t_test"],
                         "finished and rejected are outcomes somebody may still read")

    def test_clean_old_collects_terminal_records_past_the_threshold(self):
        """Age decides, not state -- and an open task is out of reach however old it is."""
        self.clean_fixture([("t_old_fin", "finished", 30), ("t_old_timeout", "timeout", 30),
                            ("t_new_fin", "finished", 1)])
        c = self.db()
        c.execute("update tasks set state_since=? where id='t_test'", (ago(30),)); c.commit()
        result = self.run_cli("clean", "old")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("deleted 2", result.stdout)
        self.assertEqual(self.remaining(), ["t_new_fin", "t_test"],
                         "the open task survives: deleting it would strand its Target")

    def test_clean_old_takes_a_threshold(self):
        self.clean_fixture([("t_2d", "finished", 2), ("t_40d", "rejected", 40)])
        self.assertIn("deleted 1", self.run_cli("clean", "old", "--days", "30").stdout)
        self.assertEqual(self.remaining(), ["t_2d", "t_test"])
        self.assertEqual(self.run_cli("clean", "old", "--days", "1").returncode, 0)
        self.assertEqual(self.remaining(), ["t_test"])

    def test_a_failed_delivery_leaves_no_next_step(self):
        """`timeout` after a delivery failure must not name a command.

        ACTION means "what happens next"; a terminal task has nothing next, and the `take` it
        used to carry pointed at a pane that may not even exist.
        """
        os.environ["FAKE_HERDR_PROMPT_FAIL"] = "1"
        try:
            result = self.run_cli("send", "--source-pane", "wA:pTEST-SRC",
                                  "--target-pane", "wA:pTEST-FRESH",   # setUp's task holds pTEST-DST
                                  "--description", "d", "--prompt", "p")
        finally:
            os.environ.pop("FAKE_HERDR_PROMPT_FAIL", None)
        self.assertEqual(result.returncode, 0, result.stderr)
        tid = result.stdout.strip()
        row = self.db().execute("select state,action from tasks where id=?", (tid,)).fetchone()
        self.assertEqual(row["state"], "timeout")
        self.assertEqual(row["action"], "none", "a terminal state names no next step")

    def test_no_terminal_state_is_written_with_an_action(self):
        """The invariant, read off the source rather than driven path by path.

        Every state in CLOSED_STATES must be paired with action "none". This is the check that
        stops `timeout` quietly regaining a `take` the next time that branch is touched.
        """
        src = (ROOT / "handoff.py").read_text()
        for m in re.finditer(r'transition\(\s*c\s*,\s*[^,]+,\s*"([a-z_]+)"\s*,\s*"([a-z_]+)"', src):
            state, action = m.group(1), m.group(2)
            if state in self.handoff.CLOSED_STATES:
                self.assertEqual(action, "none",
                                 "%s is terminal but is written with action %r" % (state, action))

    def test_send_is_gated_per_target_not_per_store(self):
        """One window's long task must not block every other window on the same store.

        The constraint exists so a Target is never handed two handoffs at once; scoping it to
        the whole store made an unrelated experiment elsewhere fail `send` here.
        """
        c = self.db()
        c.execute(INSERT, ("t_open", "占着 A 的任务", "p", "wA:pTEST-SRC", "wA:pTEST-A",
                           "active", "done", self.handoff.now()))
        c.commit()
        same = self.run_cli("send", "--source-pane", "wA:pTEST-SRC", "--target-pane", "wA:pTEST-A",
                            "--description", "d", "--prompt", "p")
        self.assertNotEqual(same.returncode, 0, "one Target must not hold two open tasks")
        self.assertIn("this target already has an unfinished task", same.stderr)

        other = self.run_cli("send", "--source-pane", "wA:pTEST-SRC", "--target-pane", "wA:pTEST-B",
                             "--description", "d", "--prompt", "p")
        self.assertEqual(other.returncode, 0, other.stderr)

    def test_the_store_rejects_a_second_open_task_for_one_target(self):
        """The SELECT in send() cannot serialise two concurrent sends; the store must.

        Both can read "nothing open" before either inserts, so the constraint has to live where
        the write does. Bypassing the check here stands in for that interleaving.
        """
        c = self.db()
        def insert(tid, state):
            c.execute(INSERT, (tid, "并发", "p", "wA:pTEST-SRC", "wA:pTEST-SAME",
                               state, "take", self.handoff.now()))
            c.commit()
        insert("t_open0", "published")
        with self.assertRaises(sqlite3.IntegrityError,
                               msg="the store must reject a second open task for one Target"):
            insert("t_open1", "active")
        self.assertEqual(
            c.execute("select count(*) from tasks where target_pane='wA:pTEST-SAME'").fetchone()[0], 1)
        # a terminal state frees the Target again, which is what the partial index says
        c.execute("update tasks set state='finished' where id='t_open0'"); c.commit()
        insert("t_open_again", "published")

    def test_an_error_payload_is_not_mistaken_for_a_result(self):
        """herdr puts failures on stdout as {"error": ...} with a non-zero exit.

        Judging by exit code alone would have been enough today, but the payload is what the
        CLI actually promises; `agent wait --timeout` returning an error object that was read as
        a result would prompt an agent that is still busy.
        """
        stub = Path(self.tmp.name) / "herdr"
        stub.write_text('#!/bin/sh\nprintf %s \'{"error":{"code":"timeout"},"id":"x"}\'\nexit 0\n')
        stub.chmod(0o755)
        self.assertIsNone(self.handoff.herdr("agent", "wait", "wA:pX", "--until", "idle"),
                          "an error payload must not be returned as a result")

    def test_no_test_delivers_to_a_live_agent(self):
        """Regression guard for a leak that put a real RESULT READY into a working agent pane.

        The fixture used to name a real pane and handoff.prompt went straight to herdr, so the
        `done` step below notified a live agent about a task that never existed. Isolation is
        applied at HERDR_BIN_PATH because run_cli() shells out -- patching handoff.prompt in
        this process would never reach the delivery that escapes.
        """
        result_file = Path(self.tmp.name) / "r.md"
        result_file.write_text("ok")
        for args in (("take", "t_test", "--pane", "wA:pTEST-DST"),
                     ("done", "t_test", "--result-file", str(result_file), "--pane", "wA:pTEST-DST")):
            self.assertEqual(self.run_cli(*args).returncode, 0)
        # `done` notifies the Source, which is why the leaked notification landed on whichever
        # live pane the fixture had named as source.
        self.assertEqual(self.delivered(), ["wA:pTEST-SRC"],
                         "the delivery was captured by the stub instead of reaching herdr")
        self.assertIn("HANDOFF RESULT READY", self.prompt_log.read_text(),
                      "and it is a real handoff notification, not a test-local stub of one")


class DaemonLifecycleTests(HandoffTestBase):
    """Uniqueness must not rest on a file that outlives the process it describes."""

    def reap(self, p):
        """Idempotent cleanup: a daemon that already exited must not fail the teardown, and one
        that was killed still has to be waited on or it stays a zombie for the whole run."""
        try: p.kill()
        except ProcessLookupError: pass
        try: p.wait(timeout=5)
        except subprocess.TimeoutExpired: pass

    def start_daemon(self):
        # The daemon writes nothing to stderr when it works, so anything there is a crash. It
        # used to go to DEVNULL, which turned a `NameError` on the second sweep into nothing
        # but a missing reminder three assertions later; tearDown now reports the text.
        self.daemon_log = Path(self.tmp.name) / "daemon.err"
        self._daemon_err = open(self.daemon_log, "wb")
        p = subprocess.Popen([sys.executable, str(ROOT / "handoff.py"), "daemon", "start"],
                             env=dict(os.environ, HANDOFF_STATE_DIR=self.tmp.name),
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=self._daemon_err, start_new_session=True)
        # Registered before the wait, not after it: an exception anywhere below used to abandon a
        # daemon that had already been spawned, and a run that left sixteen of them behind is how
        # this was noticed.
        self.addCleanup(self.reap, p)
        for _ in range(50):
            if self.handoff.daemon_running(): return p
            time.sleep(0.1)
        self.fail("daemon never took the lock")

    def tearDown(self):
        if getattr(self, "_daemon_err", None):
            self._daemon_err.close()
            noise = (self.daemon_log.read_text().strip()
                     if self.daemon_log.exists() else "")
            if noise: self.fail("the daemon wrote to stderr:\n" + noise)
        if self.handoff.daemon_running():
            (Path(self.tmp.name) / "daemon.stop").touch()
            for _ in range(80):
                if not self.handoff.daemon_running(): break
                time.sleep(0.1)
        super().tearDown()

    def test_a_second_daemon_is_refused(self):
        p = self.start_daemon()
        try:
            second = self.run_cli("daemon", "start")
            self.assertNotEqual(second.returncode, 0, "a second daemon must not be allowed")
            self.assertIn("already running", second.stderr)
        finally:
            self.reap(p)

    def test_status_agrees_with_the_board(self):
        """The CLI used to answer from the mere existence of the pid file, so the two disagreed."""
        self.assertEqual(self.run_cli("daemon", "status").stdout.strip(), "stopped")
        self.assertFalse(self.handoff.daemon_running())
        p = self.start_daemon()
        try:
            self.assertEqual(self.run_cli("daemon", "status").stdout.strip(), "running")
            self.assertTrue(self.handoff.daemon_running())
        finally:
            self.reap(p)

    def test_a_killed_daemon_does_not_read_as_running(self):
        """The reported bug: SIGKILL left a pid file naming a process that no longer existed.

        The pid file outlives whatever wrote it, so nothing in user space can tell a stale one
        from a live daemon. The lock is released by the kernel on death, so it can.
        """
        p = self.start_daemon()
        self.assertEqual(int((Path(self.tmp.name) / "daemon.pid").read_text()), p.pid)
        os.kill(p.pid, signal.SIGKILL); p.wait()
        for _ in range(30):
            if not self.handoff.daemon_running(): break
            time.sleep(0.1)
        self.assertFalse(self.handoff.daemon_running(), "a killed daemon must not read as running")
        self.assertEqual(self.run_cli("daemon", "status").stdout.strip(), "stopped",
                         "and `daemon status` must agree with the board")

    def test_a_new_daemon_can_take_over_at_once(self):
        p = self.start_daemon()
        os.kill(p.pid, signal.SIGKILL); p.wait()
        for _ in range(30):
            if not self.handoff.daemon_running(): break
            time.sleep(0.1)
        q = self.start_daemon()               # no wait, no manual cleanup of a stale file
        try:
            self.assertEqual(int((Path(self.tmp.name) / "daemon.pid").read_text()), q.pid,
                             "the pid file now names the daemon that is actually running")
        finally:
            self.reap(q)

    def test_a_reminder_holds_off_while_its_pane_has_the_users_focus(self):
        """Nudging a pane the user is looking at is a loop: nudge, Escape, nudge.

        Herdr reports the pane as focused, so the daemon can tell. It skips without spending a
        retry -- otherwise a task would age toward its timeout while the user simply sits there
        -- and reminders resume once focus moves away.
        """
        c = self.db()
        c.execute(INSERT, ("t_nag", "待领取", "p", "wA:pTEST-SRC", "wA:pTEST-FOCUSED",
                           "published", "take", self.handoff.now()))
        c.execute("update tasks set next_prompt_at=? where id='t_nag'", (self.handoff.now(),))
        c.commit()
        focus_file = Path(self.tmp.name) / "focused"
        os.environ["FAKE_HERDR_FOCUS_FILE"] = str(focus_file)
        focus_file.touch()                     # the user is sitting in that pane
        p = None
        try:
            p = self.start_daemon()
            self.sweeps(2)
            self.assertEqual(self.delivered(), [], "a focused pane must not be reminded")
            self.assertEqual(
                self.db().execute("select retry_count from tasks where id='t_nag'").fetchone()[0], 0,
                "holding off must not spend a retry")

            focus_file.unlink()                    # the user looks elsewhere
            self.sweeps(2)
            self.assertIn("wA:pTEST-FOCUSED", self.delivered(),
                          "reminders resume once the pane is no longer focused")
        finally:
            os.environ.pop("FAKE_HERDR_FOCUS_FILE", None)
            if p: self.reap(p)

    def test_interruption_markers_cover_codex_and_claude(self):
        for snapshot in ("■ Conversation interrupted - tell the model what to do differently.",
                          "[Request interrupted by user]"):
            with patch.object(self.handoff, "agent_read", return_value=snapshot):
                self.assertTrue(self.handoff.agent_was_interrupted("wA:pTEST"), snapshot)
        with patch.object(self.handoff, "agent_read", return_value="› Ask Claude to do anything"):
            self.assertFalse(self.handoff.agent_was_interrupted("wA:pTEST"))

    def test_a_reminder_holds_off_after_the_user_interrupts_the_agent(self):
        """A current Herdr detection snapshot can identify an interrupted turn."""
        c = self.db()
        c.execute(INSERT, ("t_interrupted", "被打断后不催办", "p", "wA:pTEST-SRC",
                           "wA:pTEST-INTERRUPTED", "published", "take", self.handoff.now()))
        c.execute("update tasks set next_prompt_at=? where id='t_interrupted'", (self.handoff.now(),))
        c.commit()
        read_file = Path(self.tmp.name) / "agent-read.txt"
        read_file.write_text("■ Conversation interrupted - tell the model what to do differently.\n")
        os.environ["FAKE_HERDR_READ_FILE"] = str(read_file)
        p = None
        try:
            p = self.start_daemon()
            self.sweeps(2)
            self.assertEqual(self.delivered(), [], "an interrupted turn must not be reminded")
            self.assertEqual(
                self.db().execute("select retry_count from tasks where id='t_interrupted'").fetchone()[0], 0,
                "an interruption must not spend a retry")

            read_file.write_text("› Ask Codex to do anything\n")
            self.sweeps(2)
            self.assertIn("wA:pTEST-INTERRUPTED", self.delivered(),
                          "reminders resume when the interruption marker leaves the snapshot")
        finally:
            os.environ.pop("FAKE_HERDR_READ_FILE", None)
            if p: self.reap(p)

    def test_a_blocked_agent_is_waited_on_not_reminded(self):
        """An approval prompt is someone being asked something, not an agent ignoring us.

        herdr refuses a prompt to a blocked pane before any input is sent, so a reminder here
        would bounce -- and if the refusal counted as an attempt, three of them would walk the
        task to `timeout` with nothing ever delivered.
        """
        c = self.db()
        c.execute(INSERT, ("t_blocked", "等一个批准框", "p", "wA:pTEST-SRC", "wA:pTEST-DST",
                           "published", "take", self.handoff.now()))
        c.commit()
        os.environ["FAKE_HERDR_STATUS"] = "blocked"
        os.environ["FAKE_HERDR_WAIT_S"] = "30"      # never reaches idle while blocked
        p = None
        try:
            p = self.start_daemon()
            self.sweeps(3)
            row = dict(self.db().execute(
                "select * from tasks where id='t_blocked'").fetchone())
            self.assertEqual(row["state"], "published", "a human deciding is not a timeout")
            self.assertEqual(row["retry_count"], 0, "and it does not spend an attempt")
            self.assertEqual(self.delivered(), [], "no reminder is pushed at the prompt")
        finally:
            os.environ.pop("FAKE_HERDR_STATUS", None)
            os.environ.pop("FAKE_HERDR_WAIT_S", None)
            if p: self.reap(p)

    def test_stop_works_while_the_daemon_waits_on_a_busy_agent(self):
        """A review finding: an unbounded `herdr agent wait` parked the daemon.

        It sat in the wait for as long as the agent stayed busy, so `stop` reported failure
        while the daemon was merely waiting -- a false negative, not a lock misjudgement.
        """
        c = self.db()
        c.execute(INSERT, ("t_busy", "等一个忙碌的 agent", "p", "wA:pTEST-SRC", "wA:pTEST-DST",
                           "published", "take", self.handoff.now()))
        c.commit()
        os.environ["FAKE_HERDR_STATUS"] = "working"      # the daemon will enter the wait
        os.environ["FAKE_HERDR_WAIT_S"] = "30"
        p = None
        try:
            p = self.start_daemon()
            self.sweeps()   # let it get into the wait
            result = self.run_cli("daemon", "stop")
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertIn("daemon stopped", result.stdout)
        finally:
            os.environ.pop("FAKE_HERDR_STATUS", None)
            os.environ.pop("FAKE_HERDR_WAIT_S", None)
            if p: self.reap(p)

    def test_stop_request_names_the_daemon_that_is_running(self):
        p = self.start_daemon()
        try:
            ok, message = self.handoff.request_stop()
            self.assertTrue(ok, message)
            self.assertEqual((Path(self.tmp.name) / "daemon.stop").read_text().strip(), str(p.pid),
                             "the request must name the instance it was aimed at")
        finally:
            self.reap(p)

    def test_a_start_cannot_slip_in_while_a_stop_is_deciding(self):
        """The race the second review described, pinned.

        While a stop holds the lifecycle guard no start can complete, so the pid it reads cannot
        be that of a replacement. Reading a successor's pid and addressing the request at it is
        how the previous attempt managed to stop exactly the wrong instance.
        """
        p = self.start_daemon()
        try:
            guard = self.handoff.lifecycle_lock()
            self.assertIsNotNone(guard, "the guard is free while only a daemon holds the main lock")
            try:
                blocked = self.run_cli("daemon", "start")
                self.assertNotEqual(blocked.returncode, 0)
                self.assertIn("another start or stop is in progress", blocked.stderr)
            finally:
                guard.close()
            # and with the guard released the same start goes through, so the guard is the only
            # thing that was in its way
            after = self.run_cli("daemon", "start")
            self.assertIn("already running", after.stderr)
        finally:
            self.reap(p)

    def test_a_stop_addressed_to_another_daemon_is_ignored(self):
        """A review finding: `stop` used to be a broadcast.

        If the addressed daemon exited and a replacement started before the request landed, the
        bare stop file killed the replacement -- so `stop` followed by `start` silently ended up
        as just `stop`.
        """
        p = self.start_daemon()
        try:
            (Path(self.tmp.name) / "daemon.stop").write_text("999999")   # some other daemon
            self.sweeps()
            self.assertTrue(self.handoff.daemon_running(),
                            "a request addressed to a different daemon must be ignored")
        finally:
            self.reap(p)

    def test_an_unaddressed_stop_file_still_stops(self):
        """A plain `touch daemon.stop` means "whichever daemon is running" and must keep working."""
        p = self.start_daemon()
        try:
            (Path(self.tmp.name) / "daemon.stop").touch()
            for _ in range(60):
                if not self.handoff.daemon_running(): break
                time.sleep(0.1)
            self.assertFalse(self.handoff.daemon_running())
        finally:
            self.reap(p)

    def test_stop_reports_whether_it_actually_stopped(self):
        self.assertEqual(self.run_cli("daemon", "stop").stdout.strip(), "no daemon is running")
        p = self.start_daemon()
        result = self.run_cli("daemon", "stop")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("daemon stopped", result.stdout)
        self.assertFalse(self.handoff.daemon_running())
        p.wait()

    def test_the_daemon_scores_nothing_on_its_own(self):
        """Scoring is on demand from the board; the sweep never asks for one.

        A background score would rewrite the number under whoever is reading the board, and
        a reviewer would have no way to tell their own review from the daemon's guess. The
        idle reason is the same kind of reading, so the sweep is not allowed to leave one
        either: both columns move only when the review key is pressed.
        """
        c = self.db()
        c.execute(INSERT, ("t_phase", "不自动打分", "p", "wA:pTEST-SRC", "wA:pTEST-DST",
                           "active", "done", self.handoff.now()))
        c.commit()
        read_file = Path(self.tmp.name) / "agent-read.txt"
        read_file.write_text("editing handoff.py ...\n")
        os.environ["FAKE_HERDR_READ_FILE"] = str(read_file)
        os.environ["FAKE_HERDR_STATUS"] = "working"
        stub = JevStub({"hold_off": {"type": "noul", "noul": 0.9},
                        "work_progress": {"type": "score", "score": 6.4}})
        stub.activate()
        p = None
        try:
            p = self.start_daemon()
            self.sweeps(3)
            row = self.db().execute(
                "select target_phase from tasks where id='t_phase'").fetchone()
            self.assertIsNone(row[0], "the daemon must not write a score by itself")
            self.assertTrue(stub.requests)
            self.assertEqual(set(stub.requests[0]["questions"]), {"hold_off"},
                             "the sweep asks the reminder question and nothing else")
            row = self.db().execute(
                "select target_stage from tasks where id='t_phase'").fetchone()
            self.assertIsNone(row[0], "and writes no observation of its own")
        finally:
            stub.stop()
            os.environ.pop("FAKE_HERDR_STATUS", None)
            os.environ.pop("FAKE_HERDR_READ_FILE", None)
            if p: self.reap(p)

    def test_jev_suppresses_a_reminder_without_any_marker(self):
        """The snapshot carries no interruption marker; only Jev's judgment holds the reminder."""
        c = self.db()
        c.execute(INSERT, ("t_jevsup", "主动结束则不催", "p", "wA:pTEST-SRC", "wA:pTEST-JEV",
                           "published", "take", self.handoff.now()))
        c.execute("update tasks set next_prompt_at=? where id='t_jevsup'", (self.handoff.now(),))
        c.commit()
        read_file = Path(self.tmp.name) / "agent-read.txt"
        read_file.write_text("用户按了 Esc，回合已结束\n")   # deliberately no known marker
        os.environ["FAKE_HERDR_READ_FILE"] = str(read_file)
        stub = JevStub({"hold_off": {"type": "noul", "noul": 0.97}})
        stub.activate()
        p = None
        try:
            p = self.start_daemon()
            self.sweeps(2)
            self.assertEqual(self.delivered(), [], "a user-ended turn must not be reminded")
            self.assertEqual(
                self.db().execute("select retry_count from tasks where id='t_jevsup'").fetchone()[0],
                0, "suppression must not spend a retry")
            self.assertTrue(stub.requests)
            self.assertIn("hold_off", stub.requests[0]["questions"])

            stub.answers["hold_off"]["noul"] = 0.05        # the simulation finished; resume
            self.sweeps(2)
            self.assertIn("wA:pTEST-JEV", self.delivered())
            log = self.prompt_log.read_text()
            self.assertIn("[HANDOFF REMINDER]", log, "the reminder text is the standard one")
            self.assertIn(self.handoff.REMINDER_WHY["take"].replace("\n", "\\n"), log)
        finally:
            stub.stop()
            os.environ.pop("FAKE_HERDR_READ_FILE", None)
            if p: self.reap(p)


class JevTests(HandoffTestBase):
    """Jev paths, with the model itself either stubbed over HTTP or patched out."""

    def row(self, action="take"):
        return {"id": "t_jev", "description": "d", "prompt": "p", "action": action}

    def test_jev_requires_an_api_key(self):
        self.assertFalse(self.handoff.jev_enabled(), "the base class strips the key")
        os.environ["TYPESAFE_API_KEY"] = "k"
        self.assertTrue(self.handoff.jev_enabled())
        os.environ["HANDOFF_JEV"] = "0"
        self.assertFalse(self.handoff.jev_enabled(), "HANDOFF_JEV=0 is the kill switch")

    def test_jev_is_asked_at_most_once_per_interval(self):
        """A second request inside the window is dropped before it leaves the process."""
        os.environ["HANDOFF_JEV_MIN_INTERVAL"] = "30"
        stub = JevStub({"q": {"type": "noul", "noul": 0.9}})
        stub.activate()
        try:
            q = {"q": {"type": "noul", "instructions": "i"}}
            self.assertEqual(self.handoff.jev_ask({}, q), {"q": 0.9})
            self.assertIsNone(self.handoff.jev_ask({}, q),
                              "the second call inside the window is refused")
            self.assertEqual(len(stub.requests), 1, "and never reaches the endpoint")
            self.assertGreater(self.handoff.jev_wait_left(), 0)
        finally:
            stub.stop()

    def test_the_window_reopens(self):
        os.environ["HANDOFF_JEV_MIN_INTERVAL"] = "0.3"
        self.handoff.jev_ask({}, {"q": {"type": "noul", "instructions": "i"}})
        os.environ["TYPESAFE_API_URL"] = "http://127.0.0.1:1/dead"
        self.assertIsNone(self.handoff.jev_ask({}, {"q": {"type": "noul", "instructions": "i"}}),
                          "still inside the window")
        time.sleep(0.35)
        self.assertEqual(self.handoff.jev_wait_left(), 0.0, "and then it is over")

    def test_a_stray_key_cannot_reach_the_live_endpoint(self):
        """A test that turns Jev on without a stubbed URL must fail locally, not call out.

        The default URL is resolved at import from the environment, so removing the key alone
        would leave the live endpoint in place for any test that set it back.
        """
        os.environ["TYPESAFE_API_KEY"] = "k"          # deliberately no URL override
        self.assertTrue(self.handoff.jev_enabled())
        self.assertIn("127.0.0.1", self.handoff.JEV_URL,
                      "the harness, not the API, is what an unstubbed call resolves to")
        self.assertIsNone(self.handoff.jev_ask({}, {"q": {"type": "noul", "instructions": "i"}}),
                          "the call fails on the closed port instead of leaving the machine")

    def test_jev_ask_parses_answers(self):
        stub = JevStub({"hold_off": {"type": "noul", "noul": 0.9},
                        "t_x": {"type": "score", "score": 6.4, "confidence": 0.7}})
        stub.activate()
        try:
            out = self.handoff.jev_ask({"x": 1}, {
                "hold_off": {"type": "noul", "instructions": "i"},
                "t_x": {"type": "score", "instructions": "i",
                        "criteria": self.handoff.JEV_SCORE_LEVELS}})
        finally:
            stub.stop()
        self.assertEqual(out, {"hold_off": 0.9, "t_x": 6.4})

    def test_jev_ask_partial_keeps_the_answers_that_arrived(self):
        """The board's batch review must not lose five scores to one malformed answer."""
        stub = JevStub({"t_a": {"type": "score", "score": 3.2}})      # t_b never comes back
        stub.activate()
        try:
            questions = {"t_a": {"type": "score", "instructions": "i",
                                 "criteria": self.handoff.JEV_SCORE_LEVELS},
                         "t_b": {"type": "score", "instructions": "i",
                                 "criteria": self.handoff.JEV_SCORE_LEVELS}}
            self.assertIsNone(self.handoff.jev_ask({}, questions),
                              "the daemon's strict mode still fails the whole request")
            self.assertEqual(self.handoff.jev_ask({}, questions, partial=True), {"t_a": 3.2})
        finally:
            stub.stop()
        self.assertEqual(stub.requests[0]["model"], "jev-latest")

    def test_jev_ask_returns_none_on_failure(self):
        os.environ["TYPESAFE_API_KEY"] = "k"
        os.environ["TYPESAFE_API_URL"] = "http://127.0.0.1:1/unreachable"
        self.assertIsNone(self.handoff.jev_ask({}, {"q": {"type": "noul", "instructions": "i"}}),
                          "any transport failure degrades to the pre-Jev path")

    def test_decide_reminder_holds_off_when_the_user_ended_the_turn(self):
        os.environ["TYPESAFE_API_KEY"] = "k"
        with patch.object(self.handoff, "agent_read", return_value="snapshot"), \
             patch.object(self.handoff, "jev_ask",
                          return_value={"hold_off": 0.9}) as ask:
            self.assertIsNone(self.handoff.decide_reminder(self.row(), "wA:pX"))
        self.assertTrue(ask.called)

    def test_decide_reminder_reuses_the_daemons_answers(self):
        """Pre-fetched answers must not trigger a second request for the same sweep."""
        os.environ["TYPESAFE_API_KEY"] = "k"
        with patch.object(self.handoff, "jev_ask") as ask:
            text = self.handoff.decide_reminder(
                self.row(), "wA:pX", snapshot="snapshot",
                answers={"hold_off": 0.1})
            self.assertEqual(text, self.handoff.reminder_text(self.row(), "take"),
                             "a low hold_off sends the one standard reminder")
            suppressed = self.handoff.decide_reminder(
                self.row(), "wA:pX", snapshot="snapshot",
                answers={"hold_off": 0.9})
            self.assertIsNone(suppressed)
        self.assertFalse(ask.called, "the answers were already paid for")

    def test_a_transition_clears_the_progress_score(self):
        """A score belongs to the node it described; a new state re-judges from scratch."""
        c = self.db()
        c.execute(INSERT, ("t_score", "打分", "p", "wA:pTEST-SRC", "wA:pTEST-DST",
                           "active", "done", self.handoff.now()))
        c.execute("update tasks set target_phase='7' where id='t_score'")
        c.commit()
        self.handoff.transition(c, "t_score", "result_ready", "claim", "done")
        row = self.db().execute("select target_phase from tasks where id='t_score'").fetchone()
        self.assertIsNone(row[0])

    def test_decide_reminder_falls_back_to_markers_when_jev_fails(self):
        os.environ["TYPESAFE_API_KEY"] = "k"
        marker = "■ Conversation interrupted - tell the model what to do differently."
        with patch.object(self.handoff, "agent_read", return_value=marker), \
             patch.object(self.handoff, "jev_ask", return_value=None):
            self.assertIsNone(self.handoff.decide_reminder(self.row(), "wA:pX"),
                              "a failed Jev call must not weaken the marker suppression")
        with patch.object(self.handoff, "agent_read", return_value="› idle"), \
             patch.object(self.handoff, "jev_ask", return_value=None):
            text = self.handoff.decide_reminder(self.row(), "wA:pX")
        self.assertEqual(text, self.handoff.reminder_text(self.row(), "take"),
                         "failure falls back to the standard reminder")

    def seed_open_tasks(self):
        c = self.db()
        c.execute(INSERT, ("t_open1", "第一个", "p1", "wA:pTEST-SRC", "wA:pTEST-A",
                           "published", "take", self.handoff.now()))
        c.execute(INSERT, ("t_open2", "第二个", "p2", "wA:pTEST-SRC", "wA:pTEST-B",
                           "result_ready", "claim", self.handoff.now()))
        c.execute(INSERT, ("t_done", "已结束", "p3", "wA:pTEST-SRC", "wA:pTEST-C",
                           "finished", "none", self.handoff.now()))
        c.commit()
        return c

    def test_review_scores_every_open_task_in_one_request(self):
        """One request for the whole board, one question per open task, no closed ones."""
        c = self.seed_open_tasks()
        stub = JevStub({"t_open1": {"type": "score", "score": 7.6, "confidence": 0.8},
                        "t_open2": {"type": "score", "score": 2.2}})
        stub.activate()
        try:
            scored, unanswered = self.handoff.jev_score_all(c)
        finally:
            stub.stop()
        self.assertEqual(len(stub.requests), 1, "the whole board costs one request")
        self.assertEqual(set(stub.requests[0]["questions"]),
                         {"t_open1", "t_open2", "t_open1_state", "t_open2_state"},
                         "a score and a state per open task, and nothing about closed ones")
        self.assertEqual(scored, {"t_open1": "7.6", "t_open2": "2.2"},
                         "the score is stored as it arrived, not snapped to a level")
        self.assertEqual(unanswered, 0)
        row = dict(c.execute("select * from tasks where id='t_open1'").fetchone())
        self.assertEqual(row["target_phase"], "7.6", "take is the Target's step, so dst_phase")
        row = dict(c.execute("select * from tasks where id='t_open2'").fetchone())
        self.assertEqual(row["source_phase"], "2.2", "claim is the Source's step, so src_phase")
        closed = dict(c.execute("select * from tasks where id='t_done'").fetchone())
        self.assertIsNone(closed["source_phase"])
        self.assertIsNone(closed["target_phase"])

    def test_review_state_carries_the_task_history(self):
        """A snapshot alone cannot tell a fresh task from one that has been retried four times."""
        c = self.seed_open_tasks()
        c.execute("update tasks set retry_count=4, last_action='take',"
                  " previous_node_started_at='2026-09-23T10:00:00+00:00',"
                  " target_lifecycle='working', source_phase='3' where id='t_open2'")
        c.commit()
        stub = JevStub({"t_open2": {"type": "score", "score": 5.0}})
        stub.activate()
        try:
            self.handoff.jev_score_all(c)
        finally:
            stub.stop()
        state = stub.requests[0]["state"]
        sent = next(t for t in state["tasks"] if t["id"] == "t_open2")
        self.assertEqual(sent["retry_count"], 4, "spent retries travel with the task")
        self.assertEqual(sent["last_action"], "take")
        self.assertEqual(sent["previous_node_started_at"], "2026-09-23T10:00:00+00:00",
                         "the node before this one is part of the judgment")
        self.assertEqual(sent["source"]["previous_score"], "3",
                         "the score a previous review left is shown, not silently replaced")
        self.assertEqual(sent["target"]["lifecycle"], "working")
        self.assertIn("state_since", sent)

    def test_the_review_asks_one_state_question_per_task(self):
        """Both kinds of answer live in the one menu: where the work is, and why it is parked."""
        c = self.seed_open_tasks()          # t_open1 published/take, t_open2 result_ready/claim
        stub = JevStub({"t_open1": {"type": "score", "score": 4.0},
                        "t_open1_state": {"type": "choice", "choice": "waiting"},
                        "t_open2": {"type": "score", "score": 7.0},
                        "t_open2_state": {"type": "choice", "choice": "verifying"}})
        stub.activate()
        try:
            self.handoff.jev_score_all(c)
        finally:
            stub.stop()
        choices = stub.requests[0]["questions"]["t_open1_state"]["criteria"]
        self.assertEqual(set(choices), set(self.handoff.JEV_STATE_OPTIONS))
        row = dict(c.execute("select * from tasks where id='t_open1'").fetchone())
        self.assertEqual(row["target_stage"], "waiting", "the Target owes the take")
        row = dict(c.execute("select * from tasks where id='t_open2'").fetchone())
        self.assertEqual(row["source_stage"], "verifying", "the Source owes the claim")
        self.assertIsNone(row["target_stage"])

    def test_an_answer_outside_the_menu_is_not_a_reading(self):
        c = self.seed_open_tasks()
        stub = JevStub({"t_open1": {"type": "score", "score": 4.0},
                        "t_open1_state": {"type": "choice", "choice": "working"},
                        "t_open2": {"type": "score", "score": 7.0},
                        "t_open2_state": {"type": "choice", "choice": "tie"}})
        stub.activate()
        try:
            self.handoff.jev_score_all(c)
        finally:
            stub.stop()
        row = dict(c.execute("select * from tasks where id='t_open1'").fetchone())
        self.assertEqual(row["target_stage"], "working")
        row = dict(c.execute("select * from tasks where id='t_open2'").fetchone())
        self.assertIsNone(row["source_stage"], "not one of the twenty, so nothing is stored")

    def test_an_unanswered_state_question_leaves_the_last_answer(self):
        """`partial` is per question: a missing answer must not erase the one on file."""
        c = self.seed_open_tasks()
        c.execute("update tasks set target_stage='diagnosing' where id='t_open1'")
        c.commit()
        stub = JevStub({"t_open1": {"type": "score", "score": 4.0},
                        "t_open2": {"type": "score", "score": 7.0}})
        stub.activate()
        try:
            self.handoff.jev_score_all(c)
        finally:
            stub.stop()
        row = dict(c.execute("select * from tasks where id='t_open1'").fetchone())
        self.assertEqual(row["target_stage"], "diagnosing",
                         "the question went unanswered, so the answer on file stands")

    def test_a_state_change_drops_a_stale_state_word(self):
        """The word describes one node; the node after it has its own."""
        c = self.db()
        c.execute(INSERT, ("t_idle", "为什么", "p", "wA:pTEST-SRC", "wA:pTEST-DST",
                           "active", "done", self.handoff.now()))
        c.execute("update tasks set target_stage='waiting' where id='t_idle'")
        c.commit()
        self.handoff.transition(c, "t_idle", "result_ready", "claim", "done")
        row = self.db().execute("select target_stage from tasks where id='t_idle'").fetchone()
        self.assertIsNone(row[0], "the word described the node the task has left")

    def test_a_failed_review_leaves_the_scores_standing(self):
        """An outage must not blank numbers somebody is reading."""
        c = self.seed_open_tasks()
        c.execute("update tasks set target_phase='6' where id='t_open1'")
        c.commit()
        os.environ["TYPESAFE_API_KEY"] = "k"
        os.environ["TYPESAFE_API_URL"] = "http://127.0.0.1:1/unreachable"
        scored, unanswered = self.handoff.jev_score_all(c)
        self.assertEqual(scored, {})
        self.assertEqual(unanswered, 2)
        self.assertEqual(
            dict(c.execute("select * from tasks where id='t_open1'").fetchone())["target_phase"],
            "6", "the previous score survives a failed review")
        self.assertIn("scores unchanged", self.handoff.review_summary(scored, unanswered))

    def test_review_summary_counts_without_naming_tasks(self):
        self.assertEqual(self.handoff.review_summary({}, 0), "No open task to score")
        line = self.handoff.review_summary({"t_a": "3", "t_b": "5"}, 1)
        self.assertIn("Scored 2 tasks", line)
        self.assertIn("1 unanswered", line)
        self.assertNotIn("t_", line, "no task ids in the message")

    def test_every_board_key_is_a_single_press(self):
        """Scoring and deleting are plain keys, and no key waits for a second one.

        `clean` is a command; the board's `d` deletes what is checked, not what a filter
        matches, so the two never needed to share a prefix.
        """
        self.assertEqual([k for k, _ in self.handoff.LEGEND],
                         ["↑↓", "space", "a", "r", "s", "d", "t", "q"])
        self.assertEqual(dict(self.handoff.LEGEND)["s"], "jev score")
        frame = self.handoff.render_board(150, statuses={}, tabs={}, items=[])
        self.assertIn("jev score", frame)

    def test_invalid_tasks_are_the_ones_that_ended_without_a_result(self):
        c = self.db()
        for tid, state in (("t_a", "target_absent"), ("t_b", "source_absent"),
                           ("t_c", "timeout"), ("t_d", "cancelled"),
                           ("t_e", "finished"), ("t_f", "rejected"), ("t_g", "active")):
            c.execute(INSERT, (tid, "d", "p", "wA:pX", "wA:pY", state,
                               "none" if state in self.handoff.CLOSED_STATES else "take",
                               self.handoff.now()))
        c.commit()
        self.assertEqual(sorted(self.handoff.invalid_task_ids(c)),
                         ["t_a", "t_b", "t_c", "t_d"],
                         "a finished or rejected task is an outcome somebody may still read")
        self.assertEqual(self.handoff.delete_tasks(c, self.handoff.invalid_task_ids(c)), 4)
        self.assertEqual(sorted(r[0] for r in c.execute("select id from tasks")),
                         ["t_e", "t_f", "t_g"])

    def test_decide_reminder_without_jev_matches_the_legacy_path(self):
        with patch.object(self.handoff, "agent_read",
                          return_value="[Request interrupted by user]"):
            self.assertIsNone(self.handoff.decide_reminder(self.row(), "wA:pX"))
        with patch.object(self.handoff, "agent_read", return_value="› idle"):
            text = self.handoff.decide_reminder(self.row(), "wA:pX")
        self.assertEqual(text, self.handoff.reminder_text(self.row(), "take"))


class BoardRenderTests(HandoffTestBase):
    """The board is a pure function over an explicit task list, so it needs no terminal to test."""

    ROWS = [("t_aaaa111122", "中文描述测试",         "prompt", "wA:p2V", "wA:p2W", "published", "take"),
            ("t_bbbb333344", "an ascii description", "prompt", "wA:pH",  "wA:pZZ", "active",    "done"),
            ("t_cccc555566", "短",                   "prompt", "wA:p2W", "wA:p2V", "finished",  "none"),
            # live task whose Target is busy, so the resend gate on agent status gets exercised
            ("t_dddd777777", "目标在忙",              "prompt", "wA:p2W", "wA:p2V", "active",    "done")]

    def setUp(self):
        super().setUp()
        os.environ["HERDR_PANE_ID"] = "wA:p2V"
        self.handoff._ANSI_ON = False
        c = self.db()
        for row in self.ROWS:
            c.execute(INSERT, row + (self.handoff.now(),))
        c.commit()
        # Explicit statuses keep herdr out of the test; `gone` is deliberately unknown to it.
        # Each value is (agent_status, current name) as agent_statuses() returns them.
        self.statuses = {"h1": ("working", "h1"), "h2": ("idle", "h2"),
                         "wA:p2V": ("working", "h1"), "wA:p2W": ("idle", "h2")}
        # pane -> tab, as pane_tabs() returns it. Passed in so no test reaches out to herdr.
        self.tabs = {"wA:p2V": "wA:tN", "wA:p2W": "wA:tN", "wA:pH": "wA:tC", "wA:pZZ": "wA:tC"}

    def board(self, width, **kw):
        return self.handoff.render_board(width, statuses=self.statuses, tabs=self.tabs,
                                         items=self.handoff.board_items(), **kw)

    def test_terminal_size_leaves_a_safe_right_edge_for_the_board(self):
        """The last physical terminal cell can clip or wrap a timestamp at the pane edge."""
        class Size:
            columns, lines = 80, 20

        get_size = self.handoff.os.get_terminal_size
        self.handoff.os.get_terminal_size = lambda fd: Size()
        try:
            self.assertEqual(self.handoff.terminal_size(), (79, 20))
        finally:
            self.handoff.os.get_terminal_size = get_size

    def test_confirmation_defaults_to_no(self):
        self.assertTrue(self.handoff._confirm_yes("y"))
        self.assertTrue(self.handoff._confirm_yes("Y"))
        for key in ("\r", "\n", " ", "n", "N", "ESC"):
            self.assertFalse(self.handoff._confirm_yes(key), repr(key))

    def test_no_line_ever_exceeds_the_requested_width(self):
        for width in range(43, 161):
            for line in self.board(width, selected={"t_bbbb333344"}, cursor=1).split("\n"):
                self.assertLessEqual(self.handoff._dw(line), width,
                                     "width=%d produced %r" % (width, line))

    def test_empty_board_also_respects_the_requested_width(self):
        for width in range(43, 161):
            lines = self.handoff.render_board(width, statuses=self.statuses, tabs={}, items=[]).split("\n")
            for line in lines:
                self.assertLessEqual(self.handoff._dw(line), width,
                                     "empty board width=%d produced %r" % (width, line))

    def test_the_cursor_and_checked_rows_carry_a_background(self):
        """Whole-row highlight, not just the marker character.

        A background has to go on every cell and separator: each cell ends with its own reset,
        so wrapping the finished line would not survive past the first one.
        """
        self.handoff._ANSI_ON = True
        try:
            lines = self.board(150, selected={"t_aaaa111122"}, cursor=1).split("\n")
        finally:
            self.handoff._ANSI_ON = False
        row_of = lambda tid: [l for l in lines if tid in l][0]
        self.assertIn("\033[48;5;238m", row_of("t_bbbb333344"),   # cursor=1 in this fixture
                      "the cursor row is backgrounded")
        self.assertIn("\033[48;5;235m", row_of("t_aaaa111122"),
                      "a checked row is backgrounded, at the weaker shade")
        self.assertNotIn("\033[48;5;", row_of("t_cccc555566"),
                         "an untouched row stays plain")

    def test_cursor_background_reaches_the_right_edge(self):
        """The cursor highlight fills the row's trailing empty cells too."""
        self.handoff._ANSI_ON = True
        try:
            row = [line for line in self.board(150, cursor=1).splitlines()
                   if "t_bbbb333344" in line][0]
        finally:
            self.handoff._ANSI_ON = False
        plain = re.sub(r"\033\[[0-9;]*m", "", row)
        self.assertEqual(self.handoff._dw(plain), 150)
        self.assertRegex(row, r"\033\[48;5;238m(?:\033\[[0-9;]+m)?[│█]\033\[0m$")

    def test_checkbox_and_cursor_render_on_the_right_rows(self):
        lines = self.board(130, selected={"t_bbbb333344"}, cursor=1).split("\n")
        cursors = [l for l in lines if l.startswith(">")]
        self.assertEqual(len(cursors), 1, "exactly one row carries the cursor")
        self.assertIn("t_bbbb333344", cursors[0])
        self.assertIn("[x]", cursors[0], "the cursor row is the one that was selected")
        self.assertIn("[ ]", [l for l in lines if "t_aaaa111122" in l][0])

    def test_status_columns_show_a_name_and_state(self):
        frame = self.board(150)
        self.assertRegex(frame, r"h1\s+working")
        self.assertRegex(frame, r"h2\s+idle")
        self.assertRegex(frame, r"\?:wA:pH\s+absent")
        self.assertRegex(frame, r"\?:wA:pZZ\s+absent",
                         "an unresolvable agent is shown by workspace:tab:pane, not its dead name")
        self.assertNotIn("●", frame, "status dots were dropped in favour of plain words")
        self.assertNotIn("○", frame)

    def test_board_shows_the_progress_score_on_the_action(self):
        """Jev's two answers replace the pending command: `verifying 82.22%`."""
        c = self.db()
        c.execute("update tasks set target_stage='verifying', target_phase='7.4'"
                  " where id='t_aaaa111122'")                                       # take
        c.execute("update tasks set target_phase='9.00' where id='t_cccc555566'")   # finished
        c.commit()
        frame = self.board(150)
        row = [l for l in frame.split("\n") if "t_aaaa111122" in l][0]
        self.assertIn("verifying 82.22%", row)
        self.assertNotIn("verifying h2", row,
                         "the actor's name is already in its own column; a reading is not a command")
        self.assertNotIn("take", row, "the reading stands in for the implied command")
        closed = [l for l in frame.split("\n") if "t_cccc555566" in l][0]
        self.assertNotIn("100.00%", closed,
                         "a closed task's score is no longer refreshed and stays hidden")

    def test_the_stage_and_the_score_are_shown_side_by_side(self):
        """The word is a choice Jev made; the percentage is its own answer. Neither derives
        the other -- a task can be picked as `working` while scoring near either edge."""
        c = self.db()
        for i, stage in enumerate(self.handoff.JEV_MOVING):
            score = i * self.handoff.JEV_SCORE_MAX / (len(self.handoff.JEV_MOVING) - 1)
            pct = "%.2f%%" % (score * 100 / self.handoff.JEV_SCORE_MAX)
            c.execute("update tasks set target_stage=?, target_phase=?"
                      " where id='t_aaaa111122'", (stage, score))
            c.commit()
            row = [l for l in self.board(150).split("\n") if "t_aaaa111122" in l][0]
            self.assertIn("%s %s" % (stage, pct), row, stage)

    def test_off_path_states_are_offered_but_are_not_rungs(self):
        """Trouble can strike at any point, which is exactly why it cannot be a position."""
        for word in self.handoff.JEV_HELD:
            self.assertIn(word, self.handoff.JEV_STATE_OPTIONS, "Jev can pick it")
            self.assertNotIn(word, self.handoff.JEV_MOVING, "but it is not on the line")
        self.assertEqual(self.handoff.stage_style("workaround"), ("magenta",),
                         "off-path states do not borrow a position's colour")
        self.assertEqual({self.handoff.stage_style(w) for w in ("stuck", "error")}, {("red",)},
                         "the two that ask for a person look like an alarm")
        self.assertEqual({self.handoff.stage_style(w) for w in self.handoff.JEV_HELD
                          if w not in ("stuck", "error")},
                         {("magenta",)}, "the rest share one colour")
        self.assertEqual(len(self.handoff.JEV_SCORE_LEVELS), 10)
        for level in self.handoff.JEV_SCORE_LEVELS:
            self.assertNotIn(level, self.handoff.JEV_HELD and
                             [self.handoff.JEV_STATE_OPTIONS[w] for w in self.handoff.JEV_HELD],
                             "the score's scale stays on the line")

    def test_every_held_word_says_something_different(self):
        """Nine ways to be off the main line, none of them a synonym of another."""
        trouble = {w: self.handoff.JEV_STATE_OPTIONS[w] for w in self.handoff.JEV_HELD}
        self.assertEqual(set(trouble), {"waiting", "restarting", "workaround", "diagnosing",
                                        "fixing", "error", "unreported", "stuck", "elsewhere"})
        for word, text in trouble.items():
            self.assertTrue(text.strip(), word)
        self.assertEqual(len(set(trouble.values())), len(trouble),
                         "no two describe the same situation")

    def test_an_off_path_stage_reaches_the_board(self):
        c = self.db()
        c.execute("update tasks set target_stage='workaround', target_phase='7.4'"
                  " where id='t_aaaa111122'")
        c.commit()
        row = [l for l in self.board(150).split("\n") if "t_aaaa111122" in l][0]
        self.assertIn("workaround 82.22%", row,
                      "the situation and how far along are shown together")

    def test_either_answer_can_arrive_without_the_other(self):
        c = self.db()
        c.execute("update tasks set target_stage='verifying', target_phase=NULL"
                  " where id='t_aaaa111122'")
        c.execute("update tasks set target_stage=NULL, target_phase='6.60'"
                  " where id='t_bbbb333344'")
        c.commit()
        frame = self.board(150)
        self.assertIn("verifying", frame, "the word needs no number behind it")
        self.assertIn("73.33%", frame, "and the number needs no word")

    def test_the_stage_is_a_choice_jev_makes(self):
        """Named rungs offered as a choice -- not a rounding of the score, and more than ten
        of them, because a `choice` has no ten-option ceiling the way a score does."""
        self.assertEqual(list(self.handoff.JEV_STATE_OPTIONS),
                         self.handoff.JEV_MOVING + self.handoff.JEV_HELD,
                         "the states on the way first, then the ones that are not")
        self.assertGreater(len(self.handoff.JEV_MOVING), 10)
        self.assertEqual(len(self.handoff.JEV_SCORE_LEVELS), 10, "the score keeps its cap")

    def test_the_two_answers_are_not_derived_from_each_other(self):
        """The stage says where the agent is; the score says how far along that is. Neither
        list is a restatement of the other, and the same stage can carry either number."""
        self.assertFalse(set(self.handoff.JEV_SCORE_LEVELS) & set(self.handoff.JEV_STATE_OPTIONS.values()),
                         "the score's scale must not be the stage list over again")
        c = self.db()
        for score in ("1.00", "8.00"):
            c.execute("update tasks set target_stage='working', target_phase=?"
                      " where id='t_aaaa111122'", (score,))
            c.commit()
            row = [l for l in self.board(150).split("\n") if "t_aaaa111122" in l][0]
            self.assertIn("working", row)
        self.assertIn("88.89%", self.board(150),
                      "`working` at the top of the score scale is shown as it is")

    def test_the_stored_score_is_not_quantised(self):
        """A score of any precision is kept in full; nothing snaps it to a level."""
        self.assertEqual(self.handoff.jev_score_text(3.61), "3.61")
        self.assertEqual(self.handoff.jev_score_text(3.6129), "3.6129")
        self.assertEqual(self.handoff.jev_score_text(0), "0")
        self.assertEqual(self.handoff.jev_score_text(9.4), "9")      # clamped to the scale
        self.assertEqual(self.handoff.jev_score_text(-0.5), "0")
        self.assertIsNone(self.handoff.jev_score_text(None))

    def test_the_status_columns_are_herdrs_own_words(self):
        """A fact re-read every frame; Jev's reading of the same agent goes in PROCESS."""
        c = self.db()
        c.execute("update tasks set target_stage='waiting', target_phase='3.00'"
                  " where id='t_aaaa111122'")
        c.commit()
        frame = self.board(150)
        row = [l for l in frame.split("\n") if "t_aaaa111122" in l][0]
        self.assertIn("idle", row, "the live word stays, whatever the review said")
        self.assertIn("waiting 33.33%", row, "and the review's word is in the cell beside it")
        self.assertIn("?:wA:pZZ absent", frame, "an unresolvable pane says so, in red")

    def test_the_state_word_carries_its_own_colour(self):
        """On the way through, held, or asking for a person -- three readings of one word."""
        style = self.handoff.stage_style
        self.assertEqual({style(w) for w in ("stuck", "error")}, {("red",)},
                         "the two that ask for a person")
        self.assertEqual({style(w) for w in self.handoff.JEV_HELD
                          if w not in ("stuck", "error")},
                         {("magenta",)}, "the rest of the held states share one colour")
        self.assertEqual({style(w) for w in self.handoff.JEV_MOVING},
                         {("blue",), ("cyan",), ("yellow",), ("green",)},
                         "and the moving ones walk the scale")
        for word in self.handoff.JEV_STATE_OPTIONS:
            self.assertTrue(style(word)[0] in self.handoff._CODES, word)

    def test_the_process_column_is_not_the_dimmest_thing_on_the_board(self):
        """It is the column you read first; `dim` made it the hardest to read."""
        c = self.db()
        c.execute("update tasks set target_stage='working', target_phase='4'"
                  " where id='t_aaaa111122'")
        c.execute("update tasks set target_stage='verifying', target_phase='7'"
                  " where id='t_bbbb333344'")
        c.commit()
        self.assertEqual(set(self.handoff.stage_style(w)
                             for w in self.handoff.JEV_MOVING),
                         {("blue",), ("cyan",), ("yellow",), ("green",)},
                         "every stage is coloured, none of them dim")
        self.assertEqual(self.handoff.stage_style("unstarted"), ("blue",))
        self.assertEqual(self.handoff.stage_style("done"), ("green",))
        frame = self.board(150)
        self.assertIn("working 44.44%", frame)
        self.assertIn("verifying 77.78%", frame)

    def test_the_column_is_named_for_what_it_holds(self):
        """It stopped being only the next command once it carried where the work is."""
        frame = self.board(150)
        self.assertIn("PROCESS", frame)
        self.assertNotIn("ACTION", frame)

    def test_two_close_scores_stay_two_different_percentages(self):
        """3.2 and 3.8 both used to render as 33%; the answer's own digits now reach the board."""
        c = self.db()
        c.execute("update tasks set target_phase='3.21' where id='t_aaaa111122'")
        c.execute("update tasks set target_phase='3.79' where id='t_bbbb333344'")
        c.commit()
        frame = self.board(150)
        self.assertIn("35.67%", frame)
        self.assertIn("42.11%", frame)

    def test_the_mine_row_keeps_its_command_alongside_the_score(self):
        """▶ names the keystroke you owe; the score qualifies it instead of replacing it."""
        c = self.db()
        # The ▶ row needs the pending action to be mine: result_ready/claim is the Source's
        # step, and wA:p2V (HERDR_PANE_ID in this fixture) is t_aaaa111122's Source.
        c.execute("update tasks set state='result_ready', action='claim',"
                  " source_stage='working', source_phase='4'"
                  " where id='t_aaaa111122'")
        c.commit()
        row = [l for l in self.board(150).split("\n") if "t_aaaa111122" in l][0]
        self.assertIn("▶ claim · working 44.44%", row)

    def test_board_orders_active_tasks_before_inactive_by_latest_node_time(self):
        c = self.db()
        times = {
            "t_aaaa111122": "2026-09-17T08:00:00+00:00",
            "t_bbbb333344": "2026-09-17T10:00:00+00:00",
            "t_cccc555566": "2026-09-17T12:00:00+00:00",
            "t_dddd777777": "2026-09-17T09:00:00+00:00",
        }
        for task_id, node_started_at in times.items():
            c.execute("update tasks set state_since=? where id=?", (node_started_at, task_id))
        c.execute("update tasks set state='cancelled', action='none' where id='t_dddd777777'")
        c.commit()

        items = self.handoff.board_items()

        self.assertEqual([item["id"] for item in items],
                         ["t_bbbb333344", "t_aaaa111122", "t_cccc555566", "t_dddd777777"])

    def test_inactive_task_age_is_frozen_at_terminal_transition(self):
        c = self.db()
        c.execute("update tasks set state='finished', action='none', state_since=?, last_action_at=? where id=?",
                  ("2000-01-01T00:00:00+00:00", "2000-01-01T00:02:00+00:00", "t_cccc555566"))
        c.commit()

        with patch.object(self.handoff.time, "time", return_value=946684800):
            first = next(item for item in self.handoff.board_items() if item["id"] == "t_cccc555566")
        with patch.object(self.handoff.time, "time", return_value=4102444800):
            later = next(item for item in self.handoff.board_items() if item["id"] == "t_cccc555566")

        self.assertEqual(first["age"], "2m")
        self.assertEqual(later["age"], first["age"])

    def test_status_words_line_up_down_a_column(self):
        """Names are padded to the column's widest, so the states read as a straight line.

        Measured in display columns, not string indices: the descriptions include CJK,
        so a character offset would differ between rows even when the rendering lines up.
        """
        dw = self.handoff._dw
        offsets = [dw(line[:m.start()]) for line in self.board(150).split("\n")
                   for m in [re.search(r"\b(?:idle|done|working|blocked|unknown|absent)\b", line)] if m]
        self.assertGreater(len(offsets), 1, "expected several rows carrying a status word")
        self.assertEqual(len(set(offsets)), 1,
                         "SRC state words start at differing display columns: %r" % sorted(set(offsets)))

    def test_a_finished_row_is_one_grey_end_to_end(self):
        """Nothing on a closed row keeps a colour: it recedes as one line, not as a rainbow."""
        self.handoff._ANSI_ON = True
        try:
            row = [l for l in self.board(150).split("\n") if "t_cccc555566" in l][0]
        finally:
            self.handoff._ANSI_ON = False
        closed = "\033[%sm" % self.handoff._CODES["closed"]
        self.assertIn(closed + "finished", row, "the STATE word takes the row's grey")
        self.assertIn(closed + "idle", row, "and so do the status words")
        for colour in ("\033[32m", "\033[33m", "\033[36m", "\033[35m"):
            self.assertNotIn(colour, row, "a closed row has no live colour left")

    def seed_many(self, count=20):
        c = self.db()
        for k in range(count):
            c.execute(INSERT, ("t_many%06d" % k, "任务%d" % k, "prompt", "wA:p2V", "wA:p2W",
                               "finished", "none", self.handoff.now()))
        c.commit()
        return self.handoff.board_items()

    def test_frame_never_exceeds_the_terminal_height(self):
        """A frame taller than the pane scrolls, which smears the previous frame across it."""
        items = self.seed_many()
        for height in range(7, 27):
            for cursor in (0, 5, len(items) - 1):
                lines = self.handoff.render_board(120, statuses=self.statuses, items=items,
                                                  height=height, cursor=cursor).split("\n")
                self.assertLessEqual(len(lines), height,
                                     "height=%d cursor=%d produced %d lines" % (height, cursor, len(lines)))

    def test_scrollbar_marks_the_visible_window_at_the_right_edge(self):
        short_items = self.handoff.board_items()
        short = self.handoff.render_board(80, statuses=self.statuses, tabs=self.tabs,
                                          items=short_items, height=20).splitlines()
        self.assertNotIn("│", short[2], "the list header has no scrollbar cell")
        self.assertEqual(short[3][-1], "│", "the track remains visible beside task rows")

        items = self.seed_many()
        height = 12
        room = height - self.handoff.BOARD_CHROME

        def slots(cursor):
            lines = self.handoff.render_board(80, statuses=self.statuses, items=items,
                                              height=height, cursor=cursor).splitlines()
            return [line[-1] for line in lines[3:3 + room]]

        top = slots(0)
        bottom = slots(len(items) - 1)
        self.assertEqual(top, ["█", "█", "│", "│", "│", "│"])
        self.assertEqual(bottom, ["│", "│", "│", "│", "█", "█"])

        self.handoff._ANSI_ON = True
        try:
            colored = self.handoff.render_board(80, statuses=self.statuses, items=items,
                                                 height=height, cursor=0)
        finally:
            self.handoff._ANSI_ON = False
        self.assertIn("\033[90m█", colored, "the scrollbar thumb uses a muted gray")
        self.assertNotIn("\033[36m█", colored, "the scrollbar thumb is not bright cyan")

    def test_the_window_follows_the_cursor(self):
        items = self.seed_many()
        last = len(items) - 1
        height = 12
        lines = self.handoff.render_board(120, statuses=self.statuses, items=items,
                                          height=height, cursor=last).split("\n")
        marked = [l for l in lines if l.startswith(">")]
        self.assertEqual(len(marked), 1, "the cursor row must stay on screen")
        self.assertIn(items[last]["id"], marked[0])
        room = height - self.handoff.BOARD_CHROME
        self.assertIn("showing %d-%d of %d" % (len(items) - room + 1, len(items), len(items)), lines[0],
                      "a windowed board says which slice it is showing")

    def test_unwindowed_rendering_is_unchanged(self):
        """height=None keeps every row, which is what the non-TTY and test paths rely on."""
        items = self.seed_many()
        frame = self.handoff.render_board(120, statuses=self.statuses, items=items)
        self.assertEqual(len(frame.split("\n")), len(items) + self.handoff.BOARD_CHROME)
        self.assertNotIn("/%d" % len(items), frame.split("\n")[0])

    def test_the_operators_name_carries_a_travelling_wave(self):
        """A crest runs along one name per row, in that agent kind's own colour.

        Asserts on the rendered escape codes rather than the style tables, so a wave that
        never reaches a row -- or a colour that silently drops out -- gets caught.
        """
        self.handoff._ANSI_ON = True
        try:
            frame = self.board(150, selected={"t_bbbb333344"}, cursor=1,
                               message=("Deleted 1 task(s)", ("red",)))
            levels = self.handoff.pulse_level
            self.assertGreater(len({levels(t) for t in (0.0, 0.4, 0.8, 1.2, 1.6)}), 1,
                               "the shade has to move")
            self.assertGreaterEqual(len({levels(0.0, i) for i in range(8)}), 3,
                                    "and the shade varies along the name -- that is the wave")
            for level in range(self.handoff.PULSE_LEVELS):
                code = self.handoff.agent_style("codex", level)[0]
                self.assertIn(code, self.handoff._CODES, "shades stay pre-registered")
        finally:
            self.handoff._ANSI_ON = False
        self.assertEqual(self.handoff._CODES["agent:codex"], "38;2;130;139;251",
                         "codex's colour is the one it was given")
        self.assertNotIn("\x1b[1m", frame, "weight is still carried by colour alone")
        self.assertEqual(self.handoff.agent_style("codex"), ("agent:codex",))
        self.assertEqual(self.handoff.agent_style(None), (), "an unknown pane has no colour")

    def test_resend_summary_counts_tasks_without_naming_them(self):
        """A task id would be the longest thing on the line and repeats what the board shows."""
        summary = self.handoff.resend_summary(
            ["h2"],
            [("already finished", None), ("already finished", None),
             ("it is working, not idle", "h1")],
            ["h4"])
        self.assertIn("Re-sent 1 task to h2", summary)
        self.assertIn("2 tasks skipped — already finished", summary)
        self.assertIn("h1 skipped — it is working, not idle", summary)
        self.assertIn("Could not reach h4", summary)
        self.assertNotIn("t_", summary, "no task ids in the message")

    def test_resend_summary_when_nothing_happens(self):
        self.assertEqual(self.handoff.resend_summary([], [], []), "No task was re-sent")

    def test_every_line_is_erased_as_it_is_written(self):
        """Writing text does not clear the rest of the line.

        Replacing the multi-task resend message with the shorter delete confirmation stranded
        the old tail on screen: the trailing \\033[J clears below the cursor, which by then is
        already on the last line.
        """
        out = self.handoff.frame_bytes("aa\nbb\ncc")
        self.assertTrue(out.startswith("\033[H"))
        for line in ("aa", "bb", "cc"):
            self.assertIn(line + "\033[K", out, "each line must erase its own tail")
        self.assertIn("aa\033[K\r\nbb", out,
                      "each line must return to column zero even when tty newline conversion is off")
        self.assertTrue(out.endswith("\033[K\033[J"))

    def test_last_two_lines_are_reserved(self):
        """The message line and the legend are always the final two lines, empty or not."""
        for msg in (None, ("Resent to h2", ("boldyellow",))):
            lines = self.board(120, message=msg).split("\n")
            self.assertIn("q quit", lines[-1], "the legend is always the last line")
            self.assertEqual(lines[-2], "" if msg is None else "Resent to h2")

    def test_columns_shrink_in_ladder_order(self):
        header = lambda w: self.board(w).split("\n")[2]
        self.assertIn("SRC", header(150))
        self.assertIn("DST", header(150))
        self.assertIn("START", header(150))
        self.assertIn("PREV", header(150))
        self.assertNotIn("TASK START", header(150))
        self.assertNotIn("PREV NODE", header(150))
        self.assertTrue(any("ROUTE" in header(w) for w in range(150, 42, -1)),
                        "ROUTE must appear as the fallback before routing is dropped entirely")
        self.assertNotIn("working", self.board(43), "status words are the first thing dropped")
        self.assertNotIn("ROUTE", header(43))
        self.assertNotIn("START", header(43))
        self.assertNotIn("PREV", header(43))

    def test_board_shows_the_task_and_previous_node_start(self):
        task_start = "2026-09-16T08:09:00+00:00"
        previous_start = "2026-09-16T08:10:00+00:00"
        c = self.db()
        c.execute("update tasks set task_started_at=?,previous_node_started_at=? where id=?",
                  (task_start, previous_start, "t_bbbb333344"))
        c.commit()
        frame = self.board(150)
        header, row = frame.splitlines()[2], [line for line in frame.splitlines()
                                              if "t_bbbb333344" in line][0]
        self.assertIn("START", header)
        self.assertIn("PREV", header)
        self.assertIn(self.handoff._display_time(task_start), row)
        self.assertIn(self.handoff._display_time(previous_start), row)

    def test_board_falls_back_to_task_start_when_previous_node_is_missing(self):
        task_start = "2026-09-16T08:09:00+00:00"
        c = self.db()
        c.execute("update tasks set task_started_at=?,previous_node_started_at=null,state_since=? where id=?",
                  (task_start, task_start, "t_aaaa111122"))
        c.commit()

        item = next(item for item in self.handoff.board_items() if item["id"] == "t_aaaa111122")
        self.assertEqual(item["previous_node_start"], self.handoff._display_time(task_start))
        self.assertEqual(item["previous_node_start"], item["task_start"])

        frame = self.board(150)
        row = [line for line in frame.splitlines() if "t_aaaa111122" in line][0]
        self.assertEqual(row.count(self.handoff._display_time(task_start)), 2,
                         "START and the PREV fallback should show the same initial node time")

    def test_cjk_measures_as_two_columns(self):
        dw, fit = self.handoff._dw, self.handoff._fit
        self.assertEqual(dw("中文"), 4)
        self.assertEqual(dw("ab"), 2)
        for width in range(1, 12):
            self.assertLessEqual(dw(fit("中文中文中文", width)), width)

    def test_task_text_pins_the_wire_format(self):
        """A first delivery is byte-exact: scripts and agents depend on this shape."""
        row = self.db().execute("select * from tasks where id='t_aaaa111122'").fetchone()
        cli = self.handoff.CLI
        expected = (
            "[HANDOFF TASK]\nTask ID: t_aaaa111122\nDescription: 中文描述测试\n"
            "Source: wA:p2V\nTarget: wA:p2W\n\n"
            "Before any work, run:\npython3 %s take t_aaaa111122 --pane <your-pane>\n\n"
            "Task:\nprompt\n\n"
            "On completion run:\npython3 %s done t_aaaa111122 --result-file <path> --pane <your-pane>\n"
            'Only if refusing run:\npython3 %s reject t_aaaa111122 --reason "<reason>"'
            % (cli, cli, cli))
        self.assertEqual(self.handoff.task_text(row), expected)

    def test_a_resend_is_marked_as_a_repeat(self):
        """h2's second finding: a repeat must not read like a first delivery.

        The Target otherwise cannot tell a stale nudge from a new task, and its only options
        are to go inspect the record itself or to redo work that is already on file.
        """
        row = self.db().execute("select * from tasks where id='t_aaaa111122'").fetchone()
        repeat = self.handoff.task_text(row, resend=True)
        self.assertNotIn("RE-SENT", self.handoff.task_text(row))
        self.assertIn("[HANDOFF TASK — RE-SENT]", repeat)
        self.assertIn("still open as `published`", repeat)
        self.assertIn("before redoing any work", repeat)

    def test_resend_blocked_reasons(self):
        reason = self.handoff.resend_blocked_reason
        self.assertIn("already finished", reason("finished"))
        self.assertIn("Source", reason("result_ready"),
                      "a delivered task is waiting on the Source, not the Target")

    def test_a_closed_task_cannot_be_resurrected(self):
        """h2's third finding, and the damaging one: `take` had no state guard.

        An agent obeying a stale reminder would set a finished task back to active, redo the
        work, overwrite the saved result and notify the Source a second time.
        """
        c = self.db()
        c.execute("update tasks set state='finished', action='none' where id='t_cccc555566'")
        c.commit()
        ident = ("--pane", "wA:pTEST-DST")
        cases = [("take", ident), ("reject", ("--reason", "no"))]
        for command, extra in cases:
            result = self.run_cli(command, "t_cccc555566", *extra)
            self.assertNotEqual(result.returncode, 0, "%s on a closed task must be refused" % command)
            self.assertIn("refused", result.stderr, command)
            state = self.db().execute("select state from tasks where id='t_cccc555566'").fetchone()[0]
            self.assertEqual(state, "finished",
                             "%s moved a closed task out of its final state" % command)

    def test_claim_on_a_closed_task_is_a_harmless_retry(self):
        """claim/accept land on `finished`, so re-running one stays allowed."""
        c = self.db()
        c.execute("update tasks set state='finished' where id='t_cccc555566'")
        c.commit()
        result = self.run_cli("claim", "t_cccc555566", "--pane", "wA:pTEST-SRC")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_delete_tasks_removes_the_row_and_its_result_file(self):
        result = Path(self.tmp.name) / "r.md"
        result.write_text("x")
        c = self.db()
        c.execute("update tasks set result_file=? where id='t_aaaa111122'", (str(result),))
        c.commit()
        self.assertEqual(self.handoff.delete_tasks(c, ["t_aaaa111122"]), 1)
        self.assertFalse(result.exists(), "the saved result file is removed too")
        self.assertIsNone(c.execute("select * from tasks where id='t_aaaa111122'").fetchone())
        self.assertEqual(c.execute("select count(*) from tasks").fetchone()[0], len(self.ROWS) - 1,
                         "unrelated tasks survive")
        self.assertEqual(self.handoff.delete_tasks(c, []), 0, "an empty id list is a no-op")

    def test_resend_targets_only_ready_agents(self):
        """Herdr will deliver to a busy agent, so the board must gate on status itself."""
        sent = []
        original = self.handoff.prompt
        self.handoff.prompt = lambda agent, text: (sent.append(agent), {"ok": 1})[1]
        try:
            delivered, skipped, failed = self.handoff.resend_tasks(
                ["t_aaaa111122", "t_bbbb333344", "t_cccc555566", "t_dddd777777"],
                self.statuses, self.tabs)
        finally:
            self.handoff.prompt = original
        self.assertEqual(delivered, ["h2"], "h2 is idle, so it is the only delivery")
        self.assertEqual(failed, [])
        self.assertIn(("it is working, not idle", "h1"), skipped)
        self.assertIn(("it is no longer in Herdr", "?:wA:pZZ"), skipped,
                      "a vanished agent is named by its pane, not its dead name")

    def test_every_reminder_command_is_one_the_cli_accepts(self):
        """Parse the emitted command with the real parser.

        Twice now the daemon has handed an agent a command argparse rejects -- `source_reply`
        was not a subcommand at all, and `reply` needs `--message`. Checking the generated text
        against the actual parser is what catches that class of bug.
        """
        import shlex
        filled = {"<your-agent>": "a", "<your-tab>": "t", "<your-pane>": "p",
                  "<path>": "/tmp/result.md", '"<your answer>"': "yes"}
        parser = self.handoff.build_parser()
        for action in ("take", "done", "claim", "accept"):
            text = self.handoff.reminder_text({"id": "t_x", "description": "d"}, action)
            self.assertIn(self.handoff.REMINDER_WHY[action], text, "the reminder must say why")
            command = text.split("Run:\n", 1)[1].strip()
            for placeholder, value in filled.items():
                command = command.replace(placeholder, value)
            argv = shlex.split(command)[2:]          # drop "python3" and the script path
            self.assertEqual(parser.parse_args(argv).op, action,
                             "the %s reminder produced an unrunnable command" % action)

    def test_resend_refuses_tasks_that_are_already_done(self):
        sent = []
        original = self.handoff.prompt
        self.handoff.prompt = lambda agent, text: (sent.append(agent), {"ok": 1})[1]
        try:
            delivered, skipped, failed = self.handoff.resend_tasks(
                ["t_cccc555566"], self.statuses)     # this row is finished
        finally:
            self.handoff.prompt = original
        self.assertEqual(delivered, [], "a finished task must not be re-sent")
        self.assertEqual(sent, [], "nothing reached the Target")
        self.assertEqual(skipped, [("already finished", None)],
                         "a task-level reason is counted, not tied to an id")

    def test_a_renamed_agent_is_shown_by_its_current_name(self):
        """Agent names are not durable, so the pane's current occupant wins over the stored name.

        This is what produced a task reading "h3-main working": a stale name recorded at send
        time, paired with a live status looked up by pane id.
        """
        lookup, label = self.handoff._lookup_status, self.handoff.live_label
        statuses = {"h3": ("working", "h3"), "wA:p2Y": ("working", "h3")}
        entry = lookup(statuses, "h3-main", "wA:p2Y")          # stored name is stale
        self.assertEqual(label(entry, "h3-main", "wA:p2Y"), "h3", "show who is in the pane now")
        self.assertIsNone(lookup(statuses, "h3-main", "wA:pZZ"), "an unknown pane resolves to nothing")

    def test_the_pane_wins_when_the_old_name_has_been_taken(self):
        """Names get reused; the pane is what the task is bound to, so it is asked first."""
        lookup = self.handoff._lookup_status
        statuses = {"h3-main": ("idle", "h3-main"),     # a different agent now holds the old name
                    "wA:p2Y": ("working", "h9")}        # the original, renamed, still in its pane
        self.assertEqual(lookup(statuses, "h3-main", "wA:p2Y"), ("working", "h9"))

    def test_pane_ref_carries_workspace_tab_and_pane(self):
        ref = self.handoff.pane_ref
        self.assertEqual(ref("wA:p2Y", "wA:tN"), "wA:p2Y", "Herdr pane id is already native")
        self.assertEqual(ref("wA:p2Y"), "wA:p2Y", "with no tab known the pane still locates it")
        self.assertEqual(ref("wA:p2Y", "h3"), "wA:p2Y")
        self.assertEqual(ref("wA:p2Y", "wB:t1"), "wA:p2Y")

    def test_an_unresolvable_agent_is_shown_as_a_marked_pane(self):
        """A name that no longer exists helps nobody; the pane id is the durable locator."""
        label = self.handoff.live_label
        self.assertEqual(label(None, "h3-main", "wA:p2Y"), "?:wA:p2Y",
                         "the question mark stands in for the agent that is gone")
        self.assertEqual(label(None, "h3-main"), "h3-main",
                         "nothing durable recorded, so the stored name is all there is")
        self.assertEqual(label(("idle", "h3"), "h3-main", "wA:p2Y"), "h3",
                         "a live agent still wins")
        self.assertEqual(label(("idle", None), "h3-main", "wA:p2Y"), "?:wA:p2Y",
                         "present but unnamed is still unresolved")

    def test_the_unknown_agent_label_is_red(self):
        self.handoff._ANSI_ON = True
        try:
            row = [l for l in self.board(150).split("\n") if "t_bbbb333344" in l][0]
        finally:
            self.handoff._ANSI_ON = False
        self.assertIn("\033[31m?:wA:pZZ", row, "the stale-agent marker is red")
        self.assertNotIn("\033[31mh1", row, "a resolved agent name is not")

    def test_resend_addresses_a_real_target_not_the_display_label(self):
        """The display label may be a shortened pane reference that herdr cannot resolve."""
        sent = []
        original = self.handoff.prompt
        self.handoff.prompt = lambda agent, text: (sent.append(agent), {"ok": 1})[1]
        c = self.db()
        # renamed Target: stored name is stale, the pane holds an unnamed live agent
        c.execute("update tasks set target_pane='wA:p2Y'"
                  " where id='t_aaaa111122'")
        c.commit()
        statuses = {"wA:p2Y": ("idle", None)}
        try:
            delivered, skipped, failed = self.handoff.resend_tasks(
                ["t_aaaa111122"], statuses, tabs={})   # tabs={} forces the recorded tab to be used
        finally:
            self.handoff.prompt = original
        self.assertEqual(sent, ["wA:p2Y"], "the raw pane id is addressable; the label is not")
        self.assertEqual(delivered, ["?:wA:p2Y"], "the report names it by its display label")
        self.assertEqual(skipped, [])
        self.assertEqual(failed, [])


if __name__ == "__main__":
    unittest.main()
