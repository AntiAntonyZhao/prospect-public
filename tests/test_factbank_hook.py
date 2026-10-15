"""The PreToolUse hook (.claude/hooks/protect_factbank.py), Hard rule 1 as of 2026-10-09:
an Edit to the fact bank asks the user; a whole-file Write or a shell write is blocked."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parents[1] / ".claude" / "hooks" / "protect_factbank.py"
MASTER = "C:/repo/" + "factbank" + ".yaml"


def run(tool: str, tool_input: dict) -> subprocess.CompletedProcess:
    payload = json.dumps({"tool_name": tool, "tool_input": tool_input})
    return subprocess.run(
        [sys.executable, str(HOOK)], input=payload, capture_output=True, text=True, timeout=30
    )


@pytest.mark.parametrize("tool", ["Edit", "MultiEdit"])
def test_an_edit_to_the_master_asks_the_user(tool):
    r = run(tool, {"file_path": MASTER, "old_string": "a", "new_string": "b"})
    assert r.returncode == 0
    decision = json.loads(r.stdout)["hookSpecificOutput"]
    assert (
        decision["permissionDecision"] == "ask"
        and "Hard rule 1" in decision["permissionDecisionReason"]
    )


def test_rewriting_the_whole_master_is_blocked():
    r = run("Write", {"file_path": MASTER, "content": "x"})
    assert r.returncode == 2 and "BLOCKED" in r.stderr


@pytest.mark.parametrize(
    "command",
    [
        "rm " + MASTER,
        "cp other.yaml " + MASTER,
        "echo x > " + MASTER,
        "sed -i s/a/b/ " + MASTER,
    ],
)
def test_a_shell_write_to_the_master_is_blocked(command):
    r = run("Bash", {"command": command})
    assert r.returncode == 2 and "BLOCKED" in r.stderr


@pytest.mark.parametrize(
    ("tool", "tool_input"),
    [
        ("Bash", {"command": "grep -n kind " + MASTER}),  # reading is fine
        ("Edit", {"file_path": "C:/repo/prospect.toml", "old_string": "a", "new_string": "b"}),
        ("Write", {"file_path": "C:/repo/notes.md", "content": "x"}),
    ],
)
def test_everything_else_passes_untouched(tool, tool_input):
    r = run(tool, tool_input)
    assert r.returncode == 0 and r.stdout == ""
