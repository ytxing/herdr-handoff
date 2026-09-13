import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

INSERT = ("insert into tasks(id,description,prompt,source_agent,source_pane,target_agent,"
          "target_pane,state,action,state_since) values(?,?,?,?,?,?,?,?,?,?)")


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

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._conns = []
        os.environ["HANDOFF_STATE_DIR"] = self.tmp.name
        import sys
        sys.path.insert(0, str(ROOT))
        sys.modules.pop("handoff", None)
        import handoff
        self.handoff = handoff

    def tearDown(self):
        for c in self._conns:
            try: c.close()
            except Exception: pass
        os.environ.pop("HANDOFF_STATE_DIR", None)
        os.environ.pop("HERDR_PANE_ID", None)
        self.tmp.cleanup()


class HandoffCliTests(HandoffTestBase):
    def setUp(self):
        super().setUp()
        c = self.db()
        c.execute(INSERT, ("t_test", "测试任务", "prompt", "A", "p1", "B", "p2",
                           "published", "take", self.handoff.now()))
        c.commit()

    def test_core_state_flow(self):
        result_file = Path(self.tmp.name) / "result.md"
        result_file.write_text("ok")
        identity = ('--pane', 'wA:p2')
        for command, expected in [(('take', 't_test', *identity), 'active'),
                                  (('done', 't_test', '--result-file', str(result_file), *identity), 'result_ready'),
                                  (('claim', 't_test', '--pane', 'wA:p1'), 'finished')]:
            result = self.run_cli(*command)
            self.assertEqual(result.returncode, 0, result.stderr)
            row = self.db().execute("select state from tasks where id='t_test'").fetchone()
            self.assertEqual(row[0], expected)

    def test_empty_description_rejected_by_parser(self):
        result = self.run_cli("send", "--source-pane", "wA:p1",
                              "--target-pane", "wA:p2",
                              "--description", " ", "--prompt", "x")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("description", result.stderr)

    def test_delete_removes_task(self):
        result = self.run_cli("delete", "t_test")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIsNone(self.db().execute("select * from tasks where id='t_test'").fetchone())


class BoardRenderTests(HandoffTestBase):
    """The board is a pure function over an explicit task list, so it needs no terminal to test."""

    ROWS = [("t_aaaa111122", "中文描述测试",         "prompt", "h1",      "wA:p2V", "h2",   "wA:p2W", "published", "take"),
            ("t_bbbb333344", "an ascii description", "prompt", "t1-main", "wA:pH",  "gone", "wA:pZZ", "active",    "done"),
            ("t_cccc555566", "短",                   "prompt", "h2",      "wA:p2W", "h1",   "wA:p2V", "finished",  "none"),
            # live task whose Target is busy, so the resend gate on agent status gets exercised
            ("t_dddd777777", "目标在忙",              "prompt", "h2",      "wA:p2W", "h1",   "wA:p2V", "active",    "done")]

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

    def test_no_line_ever_exceeds_the_requested_width(self):
        for width in range(43, 161):
            for line in self.board(width, selected={"t_bbbb333344"}, cursor=1).split("\n"):
                self.assertLessEqual(self.handoff._dw(line), width,
                                     "width=%d produced %r" % (width, line))

    def test_checkbox_and_cursor_render_on_the_right_rows(self):
        lines = self.board(130, selected={"t_bbbb333344"}, cursor=1).split("\n")
        cursors = [l for l in lines if l.startswith("❯")]
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

    def test_status_keeps_its_colour_on_a_finished_row(self):
        """Dimming a closed row must not strip the colour off its status words."""
        self.handoff._ANSI_ON = True
        try:
            row = [l for l in self.board(150).split("\n") if "t_cccc555566" in l][0]
        finally:
            self.handoff._ANSI_ON = False
        self.assertIn("\033[32midle", row, "idle stays green even on a finished row")
        self.assertIn("\033[32mfinished", row, "the STATE column already behaved this way")

    def seed_many(self, count=20):
        c = self.db()
        for k in range(count):
            c.execute(INSERT, ("t_many%06d" % k, "任务%d" % k, "prompt", "h1", "wA:p2V", "h2", "wA:p2W",
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

    def test_the_window_follows_the_cursor(self):
        items = self.seed_many()
        last = len(items) - 1
        height = 12
        lines = self.handoff.render_board(120, statuses=self.statuses, items=items,
                                          height=height, cursor=last).split("\n")
        marked = [l for l in lines if l.startswith("❯")]
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

    def test_nothing_on_the_board_is_bold(self):
        """Weight was dropped from the whole board: colour alone carries the meaning.

        Asserts on the rendered escape codes rather than the style tables, so a bold code
        reintroduced anywhere in the render path gets caught.
        """
        self.handoff._ANSI_ON = True
        try:
            frame = self.board(150, selected={"t_bbbb333344"}, cursor=1,
                               message=("Deleted 1 task(s)", ("red",)))
        finally:
            self.handoff._ANSI_ON = False
        codes = set(re.findall(r"\x1b\[([0-9;]+)m", frame))
        self.assertEqual(sorted(c for c in codes if c == "1" or c.startswith("1;")), [],
                         "bold was removed from the board on request")

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
        self.assertTrue(any("ROUTE" in header(w) for w in range(150, 42, -1)),
                        "ROUTE must appear as the fallback before routing is dropped entirely")
        self.assertNotIn("working", self.board(43), "status words are the first thing dropped")
        self.assertNotIn("ROUTE", header(43))

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
            "If still working run:\npython3 %s progress t_aaaa111122\n"
            'Only if refusing run:\npython3 %s reject t_aaaa111122 --reason "<reason>"'
            % (cli, cli, cli, cli))
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
        ident = ("--pane", "wA:p1")
        cases = [("take", ident), ("progress", ()), ("reply", ("--message", "x")),
                 ("reject", ("--reason", "no")), ("blocked", ("--reason", "stuck"))]
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
        result = self.run_cli("claim", "t_cccc555566", "--pane", "wA:p1")
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
        for action in ("take", "done", "claim", "accept", "reply"):
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
        c.execute("update tasks set target_agent='old-name', target_pane='wA:p2Y'"
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
