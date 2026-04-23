"""Phase 63.1 Plan 01 Task 2 — regression guard.

livekit-plugins-google 1.5.6 silently drops `session.generate_reply()` for
`gemini-3.1-flash-live-preview` (capability guard at
`plugins/google/realtime/realtime_api.py:707-715` — `mutable_chat_context`
is False for any model whose name contains `"3.1"`). The call returns a
future whose exception is never awaited, so the regression surfaces only
as one WARN log line per call plus missing agent behavior.

Phase 63.1 Plan 02 removes both active call sites in `src/agent.py`
(opening greeting + intake injection). This test prevents reintroduction.

RED today: both active call sites still present in `src/agent.py` at
lines ~712 (intake) and ~755 (greeting). Flips GREEN only after Plan 02
deletes them.

Scope: scans `src/` only. Comments and string-literals that merely mention
`generate_reply(` are permitted; only lines whose first non-whitespace
character is NOT `#` are flagged (rudimentary but adequate — false
positives would be rare and the failure message embeds offender context).
"""

from __future__ import annotations

import re
from pathlib import Path

SRC_DIR = Path(__file__).parent.parent / "src"

# Match active invocations: word-boundary `generate_reply`, optional
# whitespace, open-paren. Catches both `session.generate_reply(` and
# bare `generate_reply(` at start of expression.
_PATTERN = re.compile(r"\bgenerate_reply\s*\(")


def _iter_python_files(root: Path):
    for path in root.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        yield path


def test_no_active_generate_reply_calls_in_src():
    offenders: list[str] = []
    for path in _iter_python_files(SRC_DIR):
        text = path.read_text(encoding="utf-8")
        for lineno, raw in enumerate(text.splitlines(), start=1):
            # Skip fully-commented lines (rudimentary heuristic).
            if raw.lstrip().startswith("#"):
                continue
            if _PATTERN.search(raw):
                rel = path.relative_to(SRC_DIR.parent)
                # Normalize path separator for cross-platform stability.
                rel_str = str(rel).replace("\\", "/")
                offenders.append(f"{rel_str}:{lineno}: {raw.strip()}")

    assert not offenders, (
        "Phase 63.1: active `generate_reply(` call(s) found in src/. "
        "livekit-plugins-google 1.5.6 silently drops generate_reply() for "
        "gemini-3.1-flash-live-preview (capability guard at "
        "plugins/google/realtime/realtime_api.py:707-715). The future's "
        "exception is never awaited — the call is a no-op plus one WARN "
        "log line per call. Remove the call (Plan 02) or move it under a "
        "test fixture. Offenders:\n  " + "\n  ".join(offenders)
    )
