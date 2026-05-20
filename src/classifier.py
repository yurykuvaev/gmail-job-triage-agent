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
# Sized to fit in Anthropic Tier 1 input-token-per-minute budget (30k).
# ~15 emails * ~1300 tokens each ≈ 20k per call, leaves headroom + system prompt.
MAX_BATCH = 15
TOKEN_HARD_CAP = 200_000
TOKEN_SOFT_CAP = 25_000

# How long to wait when we hit a 429. The token bucket window is one minute,
# so anything under that won't help; 65s gives a safety margin.
RATE_LIMIT_SLEEP_SEC = 65
# Polite pause between successful batches to spread token usage across the minute.
INTER_BATCH_SLEEP_SEC = 4


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
    """One batch with bounded retries.

    - Parse failures: retry with a stricter reminder.
    - 429 rate limits: sleep for a full token-bucket window before retrying.
    - Other API errors: short exponential backoff.
    """

    attempts = 0
    last_err: Exception | None = None
    backoff = 2.0
    while attempts < 4:
        try:
            return _call_once(client, batch, strict_reminder=attempts > 0)
        except (json.JSONDecodeError, ValueError, ValidationError) as e:
            last_err = e
            logger.warning("classifier_parse_retry", extra={"attempt": attempts, "err": str(e)})
        except APIError as e:
            last_err = e
            status = getattr(e, "status_code", None)
            if status == 429:
                logger.warning(
                    "classifier_rate_limit",
                    extra={"attempt": attempts, "sleep_sec": RATE_LIMIT_SLEEP_SEC},
                )
                time.sleep(RATE_LIMIT_SLEEP_SEC)
            else:
                logger.warning(
                    "classifier_api_retry",
                    extra={"attempt": attempts, "err": str(e), "sleep_sec": backoff},
                )
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

    # Always chunk to MAX_BATCH so we never blow past Tier 1's 30k tokens/min
    # limit on a single call. Daily runs (~30 emails) become 2 batches with a
    # small inter-batch sleep — negligible. Backfill (~200) becomes 14 batches.
    chunk_size = MAX_BATCH

    batches = [prompt_dicts[i : i + chunk_size] for i in range(0, len(prompt_dicts), chunk_size)]
    for idx, batch in enumerate(batches):
        if not batch:
            continue
        if idx > 0:
            # Spread token spend across the rate-limit window.
            time.sleep(INTER_BATCH_SLEEP_SEC)
        results, ti, to = _classify_batch(client, batch)
        items.extend(results)
        tokens_in += ti
        tokens_out += to

    return ClassifyResult(items=items, tokens_input=tokens_in, tokens_output=tokens_out)
