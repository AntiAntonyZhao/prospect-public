#!/usr/bin/env python3
"""PreToolUse hook — enforces CLAUDE.md Hard rule 1 during Claude Code sessions.

factbank.yaml is the master. Since 2026-10-09 the user may let Claude Code edit it, but
only edit by edit, each one approved by the user:
- Edit / MultiEdit on factbank.yaml → "ask": Claude Code shows the change and waits for
  the user's yes. Nothing reaches the file without it.
- Write on factbank.yaml (replacing the whole file) → blocked: changes must be edits the
  user can read one by one.
- A shell command that names factbank.yaml and looks like a write (redirect, sed -i,
  tee, mv, cp, rm, python open(..., 'w')) → blocked: it would bypass the approval.
Blocking is exit code 2 with the reason on stderr; asking is exit 0 with a JSON decision.
"""
import json
import os
import re
import sys

MASTER = "factbank.yaml"
WRITE_HINTS = re.compile(r"(>\s*\S*factbank\.yaml|sed\s+-i|tee\b|\bmv\b|\bcp\b|\brm\b|python[^\n]*open\([^\n]*factbank\.yaml[^\n]*['\"]w)")


def block(reason: str) -> int:
    print(f"BLOCKED: {reason} (CLAUDE.md Hard rule 1).", file=sys.stderr)
    return 2


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0
    tool = data.get("tool_name", "")
    ti = data.get("tool_input", {}) or {}

    if tool in ("Edit", "Write", "MultiEdit"):
        path = ti.get("file_path") or ti.get("path") or ""
        if os.path.basename(path) != MASTER:
            return 0
        if tool == "Write":
            return block(
                f"{MASTER} is never rewritten whole; make each change an Edit the user approves"
            )
        print(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "ask",
                        "permissionDecisionReason": (
                            f"{MASTER} is the master (Hard rule 1): this edit needs your "
                            "approval. Check it is true before you accept."
                        ),
                    }
                }
            )
        )
        return 0

    if tool == "Bash":
        cmd = ti.get("command", "") or ""
        if MASTER in cmd and WRITE_HINTS.search(cmd):
            return block(
                f"this command looks like it writes to {MASTER}; only an approved Edit may"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
