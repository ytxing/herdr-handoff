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
import time

def focused():
    """Read focus from a FILE, not the environment.

    A daemon's environment is frozen when it is spawned, so a test that wants focus to change
    while the daemon runs has to vary something the daemon can still see.
    """
    path = os.environ.get("FAKE_HERDR_FOCUS_FILE")
    return bool(path) and os.path.exists(path)


AGENT = {"agent": "claude", "agent_status": os.environ.get("FAKE_HERDR_STATUS", "idle"), "pane_id": "wA:pTEST",
         "tab_id": "wA:tTEST", "workspace_id": "wA", "terminal_id": "term_test",
         "name": "test-agent", "revision": 1, "state_change_seq": 1,
         "focused": focused(),
         "interactive_ready": True}


def emit(payload):
    json.dump(payload, sys.stdout)
    sys.stdout.write("\n")


def main(argv):
    cmd = argv[1:]

    if cmd[:2] == ["agent", "prompt"]:
        if os.environ.get("FAKE_HERDR_PROMPT_FAIL"):
            sys.stderr.write("fake_herdr: delivery refused\n")
            return 1
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
        # The real CLI answers an unknown name with {"error": ...} and a non-zero exit. A test
        # that needs that answer names the value in FAKE_HERDR_ABSENT; every other name
        # resolves, which keeps the fixtures readable.
        if target in (os.environ.get("FAKE_HERDR_ABSENT") or "").split(","):
            emit({"id": "cli:agent:get",
                  "error": {"code": "agent_not_found",
                            "message": "agent target %s not found" % target}})
            return 1
        emit({"id": "cli:agent:get",
              "result": {"agent": dict(AGENT, pane_id=target), "type": "agent_info"}})
        return 0

    if cmd[:2] == ["agent", "list"]:
        emit({"id": "cli:agent:list", "result": {"agents": [AGENT], "type": "agent_list"}})
        return 0

    if cmd[:2] == ["agent", "wait"]:
        # Mirrors the real CLI: honour --timeout, and fail when the agent stays busy past it.
        # FAKE_HERDR_WAIT_S makes an agent look permanently busy, which is the state that used
        # to park the daemon and make `stop` report a failure that was not real.
        busy = float(os.environ.get("FAKE_HERDR_WAIT_S", "0") or 0)
        budget = None
        if "--timeout" in cmd:
            budget = float(cmd[cmd.index("--timeout") + 1]) / 1000.0
        nap = busy if budget is None else min(busy, budget)
        if nap > 0: time.sleep(nap)
        if budget is not None and busy > budget:
            sys.stderr.write("fake_herdr: agent still busy\n")
            return 1
        emit({"id": "cli:agent:wait", "result": {"type": "agent_wait"}})
        return 0

    if cmd[:2] == ["pane", "list"]:
        emit({"id": "cli:pane:list", "result": {"panes": [], "type": "pane_list"}})
        return 0

    sys.stderr.write("fake_herdr: unhandled command: %r\n" % (cmd,))
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
