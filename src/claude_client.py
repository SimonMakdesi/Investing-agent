"""Wrapper around the Anthropic API.

Stub for Phase 1 — kept minimal so the import graph compiles. Phase 2
will flesh out role-aware calls, prompt assembly from CLAUDE.md +
role files, and structured output handling.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def not_yet_implemented() -> None:
    raise NotImplementedError(
        "Claude calls are introduced in Phase 2. See PROJECT_BRIEF.md §12."
    )
