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
    def run_cli(self, *args):
        env = os.environ.copy()
        env["HANDOFF_STATE_DIR"] = self.tmp.name
        return subprocess.run(["python3", str(ROOT / "handoff.py"), *args], env=env,
                              text=True, capture_output=True)

    def setUp(self):
        super().setUp()
        c = self.db()
        c.execute(INSERT, ("t_test", "测试任务", "prompt", "A", "p1", "B", "p2",
                           "published", "take", self.handoff.now()))
        c.commit()

    def test_core_state_flow(self):
        result_file = Path(self.tmp.name) / "result.md"
        result_file.write_text("ok")
        identity = ('--agent-name', 'B', '--tab', 't1', '--pane', 'p2')
        for command, expected in [(('take', 't_test', *identity), 'active'),
                                  (('done', 't_test', '--result-file', str(result_file), *identity), 'result_ready'),
                                  (('claim', 't_test', '--agent-name', 'A', '--tab', 't1', '--pane', 'p1'), 'finished')]:
            result = self.run_cli(*command)
            self.assertEqual(result.returncode, 0, result.stderr)
            row = self.db().execute("select state from tasks where id='t_test'").fetchone()
            self.assertEqual(row[0], expected)

    def test_empty_description_rejected_by_parser(self):
        result = self.run_cli("send", "--source-agent", "A", "--source-pane", "p1",
                              "--target-agent", "B", "--target-pane", "p2",
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
            ("t_cccc555566", "短",                   "prompt", "h2",      "wA:p2W", "h1",   "wA:p2V", "finished",  "none")]

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

    def board(self, width, **kw):
        return self.handoff.render_board(width, statuses=self.statuses,
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
        self.assertRegex(frame, r"t1-main\s+absent")
        self.assertRegex(frame, r"gone\s+absent", "an agent herdr does not know reads as absent")
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
        """`send` and the board's resend key must deliver byte-identical text."""
        row = self.db().execute("select * from tasks where id='t_aaaa111122'").fetchone()
        cli = self.handoff.CLI
        expected = (
            "[HANDOFF TASK]\nTask ID: t_aaaa111122\nDescription: 中文描述测试\n"
            "Source: h1 / wA:p2V\nTarget: h2 / wA:p2W\n\n"
            "Before any work, run:\npython3 %s take t_aaaa111122 --agent-name <your-agent> --tab <your-tab> --pane <your-pane>\n\n"
            "Task:\nprompt\n\n"
            "On completion run:\npython3 %s done t_aaaa111122 --result-file <path> --agent-name <your-agent> --tab <your-tab> --pane <your-pane>\n"
            "If still working run:\npython3 %s progress t_aaaa111122\n"
            'Only if refusing run:\npython3 %s reject t_aaaa111122 --reason "<reason>"'
            % (cli, cli, cli, cli))
        self.assertEqual(self.handoff.task_text(row), expected)

    def test_delete_tasks_removes_the_row_and_its_result_file(self):
        result = Path(self.tmp.name) / "r.md"
        result.write_text("x")
        c = self.db()
        c.execute("update tasks set result_file=? where id='t_aaaa111122'", (str(result),))
        c.commit()
        self.assertEqual(self.handoff.delete_tasks(c, ["t_aaaa111122"]), 1)
        self.assertFalse(result.exists(), "the saved result file is removed too")
        self.assertIsNone(c.execute("select * from tasks where id='t_aaaa111122'").fetchone())
        self.assertEqual(c.execute("select count(*) from tasks").fetchone()[0], 2,
                         "unrelated tasks survive")
        self.assertEqual(self.handoff.delete_tasks(c, []), 0, "an empty id list is a no-op")

    def test_resend_targets_only_ready_agents(self):
        """Herdr will deliver to a busy agent, so the board must gate on status itself."""
        sent = []
        original = self.handoff.prompt
        self.handoff.prompt = lambda agent, text: (sent.append(agent), {"ok": 1})[1]
        try:
            delivered, skipped, failed = self.handoff.resend_tasks(
                ["t_aaaa111122", "t_bbbb333344", "t_cccc555566"], self.statuses)
        finally:
            self.handoff.prompt = original
        self.assertEqual(delivered, ["h2"], "h2 is idle, so it is the only delivery")
        self.assertEqual(failed, [])
        self.assertTrue(any("h1 skipped — it is working, not idle" == s for s in skipped), skipped)
        self.assertTrue(any("gone skipped — it is no longer in Herdr" == s for s in skipped), skipped)

    def test_a_renamed_agent_is_shown_by_its_current_name(self):
        """Agent names are not durable, so the pane's current occupant wins over the stored name.

        This is what produced a task reading "h3-main working": a stale name recorded at send
        time, paired with a live status looked up by pane id.
        """
        lookup, label = self.handoff._lookup_status, self.handoff.live_label
        statuses = {"h3": ("working", "h3"), "wA:p2Y": ("working", "h3")}
        entry = lookup(statuses, "h3-main", "wA:p2Y")          # stored name is stale
        self.assertEqual(label(entry, "h3-main"), "h3", "show who is in the pane now")
        self.assertIsNone(lookup(statuses, "h3-main", "wA:pZZ"), "an unknown pane resolves to nothing")
        self.assertEqual(label(None, "h3-main"), "h3-main", "fall back to the recorded name")


if __name__ == "__main__":
    unittest.main()
