#!/usr/bin/env python3
"""A stand-in for the herdr CLI, so the test suite cannot touch a live agent pane.

The tests used to run against the real `herdr`, which meant `test_core_state_flow` reached its
`done` step and genuinely delivered a `[HANDOFF RESULT READY]` to whatever pane the fixture
named -- and one of those names, `wA:p1`, was a real working agent.

Isolation has to happen at `HERDR_BIN_PATH` rather than by patching `handoff.prompt`:
HandoffCliTests shells out to the CLI, so an in-process patch never reaches the delivery that
matters. Pointing this script in intercepts both paths.

`agent prompt` is the one that must never escape, so it records instead of delivering. Set
FAKE_HERDR_LOG to the file to record into; if it is unset the call is dropped on the floor,
which is still safe.
"""
import json
import os
import sys

AGENT = {"agent": "claude", "agent_status": "idle", "pane_id": "wA:pTEST",
         "tab_id": "wA:tTEST", "workspace_id": "wA", "terminal_id": "term_test",
         "name": "test-agent", "revision": 1, "state_change_seq": 1, "focused": False,
         "interactive_ready": True}


def emit(payload):
    json.dump(payload, sys.stdout)
    sys.stdout.write("\n")


def main(argv):
    cmd = argv[1:]

    if cmd[:2] == ["agent", "prompt"]:
        target = cmd[2] if len(cmd) > 2 else ""
        text = cmd[3] if len(cmd) > 3 else ""
        log = os.environ.get("FAKE_HERDR_LOG")
        if log:
            with open(log, "a") as fh:
                fh.write("%s\t%s\n" % (target, text.replace("\n", "\\n")))
        emit({"id": "cli:agent:prompt", "result": {"target": target, "type": "agent_prompt"}})
        return 0

    if cmd[:2] == ["agent", "get"]:
        target = cmd[2] if len(cmd) > 2 else ""
        emit({"id": "cli:agent:get",
              "result": {"agent": dict(AGENT, pane_id=target), "type": "agent_info"}})
        return 0

    if cmd[:2] == ["agent", "list"]:
        emit({"id": "cli:agent:list", "result": {"agents": [AGENT], "type": "agent_list"}})
        return 0

    if cmd[:2] == ["agent", "wait"]:
        emit({"id": "cli:agent:wait", "result": {"type": "agent_wait"}})
        return 0

    if cmd[:2] == ["pane", "list"]:
        emit({"id": "cli:pane:list", "result": {"panes": [], "type": "pane_list"}})
        return 0

    sys.stderr.write("fake_herdr: unhandled command: %r\n" % (cmd,))
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
