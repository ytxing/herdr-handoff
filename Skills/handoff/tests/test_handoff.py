import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

class HandoffCliTests(unittest.TestCase):
    def run_cli(self, *args):
        env = os.environ.copy()
        env["HANDOFF_STATE_DIR"] = self.tmp.name
        return subprocess.run(["python3", str(ROOT / "handoff.py"), *args], env=env,
                              text=True, capture_output=True)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["HANDOFF_STATE_DIR"] = self.tmp.name
        import sys
        sys.path.insert(0, str(ROOT))
        sys.modules.pop("handoff", None)
        import handoff
        self.handoff = handoff
        c = handoff.conn()
        c.execute("insert into tasks(id,description,prompt,source_agent,source_pane,target_agent,target_pane,state,action,state_since) values(?,?,?,?,?,?,?,?,?,?)",
                  ("t_test", "测试任务", "prompt", "A", "p1", "B", "p2", "published", "take", handoff.now()))
        c.commit()

    def tearDown(self):
        os.environ.pop("HANDOFF_STATE_DIR", None)
        self.tmp.cleanup()

    def test_core_state_flow(self):
        result_file = Path(self.tmp.name) / "result.md"
        result_file.write_text("ok")
        for command, expected in [(('take', 't_test'), 'active'),
                                  (('done', 't_test', '--result-file', str(result_file)), 'result_ready'),
                                  (('claim', 't_test'), 'reviewing'),
                                  (('accept', 't_test'), 'finished')]:
            result = self.run_cli(*command)
            self.assertEqual(result.returncode, 0, result.stderr)
            row = self.handoff.conn().execute("select state from tasks where id='t_test'").fetchone()
            self.assertEqual(row[0], expected)

    def test_empty_description_rejected_by_parser(self):
        result = self.run_cli("send", "--source-agent", "A", "--source-pane", "p1",
                              "--target-agent", "B", "--target-pane", "p2",
                              "--description", " ", "--prompt", "x")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("description", result.stderr)

if __name__ == "__main__":
    unittest.main()
