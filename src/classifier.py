"""Anthropic classification layer.

A single batched call sends all (deduped) emails and asks for a JSON array
back. Parse defensively; retry once on JSON failure with a stricter reminder.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass

from anthropic import Anthropic, APIError
from pydantic import BaseModel, Field, ValidationError

from .gmail_client import FetchedEmail
from .prompts import CATEGORIES, SYSTEM_PROMPT

logger = logging.getLogger(__name__)

MODEL_ID = "claude-sonnet-4-6"
MAX_BATCH = 30
TOKEN_HARD_CAP = 200_000
# rough heuristic: ~4 chars per token; used only to decide whether to chunk.
TOKEN_SOFT_CAP = 150_000


class Classified(BaseModel):
    """Validated classification result for a single email."""

    id: str
    category: str
    company: str | None = None
    role: str | None = None
    next_step: str | None = None
    deadline: str | None = None
    link: str | None = None


@dataclass
class ClassifyResult:
    items: list[Classified]
    tokens_input: int
    tokens_output: int


def _approx_tokens(items: list[dict]) -> int:
    return sum(len(json.dumps(i)) for i in items) // 4


def _strip_fences(text: str) -> str:
    """Tolerate the occasional ```json fence even though the prompt forbids it."""

    fenced = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", text, re.DOTALL)
    if fenced:
        return fenced.group(1)
    return text.strip()


def _parse(text: str) -> list[Classified]:
    payload = json.loads(_strip_fences(text))
    if not isinstance(payload, list):
        raise ValueError("Model response was not a JSON array")
    out: list[Classified] = []
    for raw in payload:
        item = Classified.model_validate(raw)
        if item.category not in CATEGORIES:
            item = item.model_copy(update={"category": "other"})
        out.append(item)
    return out


def _call_once(
    client: Anthropic, batch: list[dict], strict_reminder: bool = False
) -> tuple[list[Classified], int, int]:
    user_msg = json.dumps(batch, ensure_ascii=False)
    if strict_reminder:
        user_msg = (
            "Your previous reply was not valid JSON. Reply with ONLY a JSON array, "
            "no markdown, no commentary.\n\nInput:\n" + user_msg
        )

    resp = client.messages.create(
        model=MODEL_ID,
        max_tokens=8192,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_msg}],
    )
    text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
    items = _parse(text)
    return items, resp.usage.input_tokens, resp.usage.output_tokens


def _classify_batch(
    client: Anthropic, batch: list[dict]
) -> tuple[list[Classified], int, int]:
    """One batch, with a single retry on parse failure."""

    attempts = 0
    last_err: Exception | None = None
    backoff = 1.0
    while attempts < 3:
        try:
            return _call_once(client, batch, strict_reminder=attempts > 0)
        except (json.JSONDecodeError, ValueError, ValidationError) as e:
            last_err = e
            logger.warning("classifier_parse_retry", extra={"attempt": attempts, "err": str(e)})
        except APIError as e:
            last_err = e
            logger.warning("classifier_api_retry", extra={"attempt": attempts, "err": str(e)})
            time.sleep(backoff)
            backoff *= 2
        attempts += 1
    raise RuntimeError(f"classifier failed after {attempts} attempts") from last_err


def classify_emails(client: Anthropic, emails: list[FetchedEmail]) -> ClassifyResult:
    """Classify all emails; chunk if the prompt would be too large."""

    items: list[Classified] = []
    tokens_in = 0
    tokens_out = 0

    prompt_dicts = [e.to_prompt_dict() for e in emails]
    approx = _approx_tokens(prompt_dicts)
    if approx > TOKEN_HARD_CAP:
        logger.warning(
            "classifier_hard_cap_hit",
            extra={"approx_tokens": approx, "truncating_to": MAX_BATCH},
        )
        prompt_dicts = prompt_dicts[:MAX_BATCH]

    # chunk to MAX_BATCH if soft cap exceeded, otherwise send in one shot.
    chunk_size = MAX_BATCH if approx > TOKEN_SOFT_CAP else len(prompt_dicts) or 1

    for i in range(0, len(prompt_dicts), chunk_size):
        batch = prompt_dicts[i : i + chunk_size]
        if not batch:
            continue
        results, ti, to = _classify_batch(client, batch)
        items.extend(results)
        tokens_in += ti
        tokens_out += to

    return ClassifyResult(items=items, tokens_input=tokens_in, tokens_output=tokens_out)
