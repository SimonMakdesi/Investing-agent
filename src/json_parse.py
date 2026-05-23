"""Extract JSON blocks from Claude responses.

All role prompts ask for prose followed by a fenced ```json``` block.
This module pulls the JSON out reliably.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

log = logging.getLogger(__name__)

_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\}|\[.*?\])\s*```", re.DOTALL)


class JsonExtractError(ValueError):
    pass


def extract_json(text: str) -> Any:
    """Pull the first fenced JSON block out of the response.

    Falls back to greedy {...} match if no fence found. Raises if neither works.
    """
    m = _FENCE_RE.search(text)
    if m:
        candidate = m.group(1)
    else:
        # Fallback: greedy match of the last {...} block in the text
        idx = text.rfind("{")
        end = text.rfind("}")
        if idx == -1 or end <= idx:
            raise JsonExtractError("No JSON block found in response")
        candidate = text[idx : end + 1]

    try:
        return json.loads(candidate)
    except json.JSONDecodeError as e:
        raise JsonExtractError(f"JSON parse failed: {e}\nCandidate:\n{candidate[:500]}") from e
