"""Wrapper around the Anthropic API.

Key design:
- The system prompt is `CLAUDE.md` + the role's prompt, *cached* via
  prompt caching so repeated calls within ~5 min get a ~90% discount
  on the system prompt cost. This matters when the Analyst role is
  called 5-7 times in a row.
- Every call is logged to `archive/claude_calls/` as JSON (timestamp,
  role, model, system, user, response, usage). This is the audit trail
  the brief asks for.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from anthropic import Anthropic
from anthropic.types import Message

from src.config import ARCHIVE_DIR, REPO_ROOT, STOCKHOLM_TZ, settings

log = logging.getLogger(__name__)

CONSTITUTION_PATH = REPO_ROOT / "CLAUDE.md"
PROMPTS_DIR = REPO_ROOT / "prompts"
CALL_ARCHIVE = ARCHIVE_DIR / "claude_calls"

# Model IDs — see CLAUDE.md and pyproject.toml for context.
MODEL_OPUS = "claude-opus-4-7"
MODEL_SONNET = "claude-sonnet-4-6"

# Per-role default model. Opus for the high-stakes reasoning calls;
# Sonnet for compression / journaling / monitoring where it's plenty.
ROLE_MODEL: dict[str, str] = {
    "screener": MODEL_SONNET,
    "analyst": MODEL_OPUS,
    "portfolio_manager": MODEL_OPUS,
    "daily_pm": MODEL_OPUS,  # event-driven daily trade decisions — high-stakes, so Opus
    "journal_keeper": MODEL_SONNET,
    "event_monitor": MODEL_SONNET,
}

# Max output tokens per role. Keep these tight — long responses are a smell.
ROLE_MAX_TOKENS: dict[str, int] = {
    "screener": 2_000,
    "analyst": 4_000,
    "portfolio_manager": 4_000,
    "daily_pm": 4_000,
    "journal_keeper": 3_000,
    "event_monitor": 1_500,
}


@dataclass
class RoleResponse:
    role: str
    model: str
    text: str
    input_tokens: int
    output_tokens: int
    cache_creation_tokens: int
    cache_read_tokens: int
    elapsed_seconds: float


_client: Anthropic | None = None


def _client_singleton() -> Anthropic:
    global _client
    if _client is None:
        settings.require("anthropic_api_key")
        _client = Anthropic(api_key=settings.anthropic_api_key)
    return _client


def _load_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _build_system(role: str) -> list[dict]:
    """System prompt = constitution + role prompt, both marked for caching."""
    constitution = _load_text(CONSTITUTION_PATH)
    role_prompt_path = PROMPTS_DIR / f"{role}.md"
    if not role_prompt_path.exists():
        raise FileNotFoundError(
            f"Role prompt not found: {role_prompt_path}. "
            "Add the role file under prompts/."
        )
    role_prompt = _load_text(role_prompt_path)

    return [
        {
            "type": "text",
            "text": constitution,
            "cache_control": {"type": "ephemeral"},
        },
        {
            "type": "text",
            "text": role_prompt,
            "cache_control": {"type": "ephemeral"},
        },
    ]


def call_role(
    role: str,
    user_message: str,
    *,
    model: str | None = None,
    max_tokens: int | None = None,
) -> RoleResponse:
    """Make one Claude call for the given role and return the response.

    The system prompt (constitution + role) is automatically prepended and
    cached. The caller only supplies the role-specific user message.

    Every call is archived as JSON under archive/claude_calls/.
    """
    if role not in ROLE_MODEL:
        raise ValueError(f"Unknown role: {role}. Known: {list(ROLE_MODEL)}")

    chosen_model = model or ROLE_MODEL[role]
    chosen_max_tokens = max_tokens or ROLE_MAX_TOKENS[role]
    system_blocks = _build_system(role)

    log.info("Calling Claude (role=%s, model=%s, max_tokens=%d)", role, chosen_model, chosen_max_tokens)
    started = time.monotonic()
    msg: Message = _client_singleton().messages.create(
        model=chosen_model,
        max_tokens=chosen_max_tokens,
        system=system_blocks,
        messages=[{"role": "user", "content": user_message}],
    )
    elapsed = time.monotonic() - started

    # Aggregate the text from any text blocks (we don't use tool use yet).
    text = "".join(block.text for block in msg.content if hasattr(block, "text"))
    usage = msg.usage
    response = RoleResponse(
        role=role,
        model=chosen_model,
        text=text,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cache_creation_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
        cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
        elapsed_seconds=elapsed,
    )

    log.info(
        "Claude returned (role=%s, in=%d, out=%d, cache_create=%d, cache_read=%d, %.1fs)",
        role,
        response.input_tokens,
        response.output_tokens,
        response.cache_creation_tokens,
        response.cache_read_tokens,
        response.elapsed_seconds,
    )

    _archive_call(
        role=role,
        model=chosen_model,
        system_blocks=system_blocks,
        user_message=user_message,
        response=response,
    )
    return response


def call_lightweight(
    *,
    system: str,
    user: str,
    model: str = MODEL_SONNET,
    max_tokens: int = 600,
    label: str = "utility",
) -> tuple[str, dict]:
    """A simple Claude call for utility tasks (news classification, etc).

    Skips the constitution + role-prompt loading and the per-call archive —
    these would balloon storage and cost for hundreds of tiny classification
    calls. Use only for stateless, single-shot helpers.

    Returns (text, usage_dict).
    """
    started = time.monotonic()
    msg = _client_singleton().messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    text = "".join(block.text for block in msg.content if hasattr(block, "text"))
    usage = msg.usage
    elapsed = time.monotonic() - started
    log.debug(
        "Lightweight Claude call (%s, %s): in=%d out=%d %.1fs",
        label, model, usage.input_tokens, usage.output_tokens, elapsed,
    )
    return text, {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "elapsed_seconds": elapsed,
    }


def _archive_call(
    *,
    role: str,
    model: str,
    system_blocks: list[dict],
    user_message: str,
    response: RoleResponse,
) -> None:
    CALL_ARCHIVE.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(tz=STOCKHOLM_TZ).strftime("%Y%m%d_%H%M%S")
    # Counter file lets multiple calls in the same second land in unique files.
    counter = len(list(CALL_ARCHIVE.glob(f"{ts}_*"))) + 1
    path = CALL_ARCHIVE / f"{ts}_{counter:02d}_{role}.json"

    record = {
        "timestamp": datetime.now(tz=STOCKHOLM_TZ).isoformat(),
        "role": role,
        "model": model,
        # We don't archive the full constitution text every time — too noisy.
        # The role prompt and user message are the interesting per-call pieces.
        "system_block_sizes": [len(b["text"]) for b in system_blocks],
        "user_message": user_message,
        "response_text": response.text,
        "usage": {
            "input_tokens": response.input_tokens,
            "output_tokens": response.output_tokens,
            "cache_creation_tokens": response.cache_creation_tokens,
            "cache_read_tokens": response.cache_read_tokens,
        },
        "elapsed_seconds": response.elapsed_seconds,
    }
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
