"""Lambda entry point.

Orchestrates: secrets -> window -> fetch -> dedup -> classify -> format -> send -> mark.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from datetime import datetime, timezone

import boto3
from anthropic import Anthropic

from .classifier import Classified, classify_emails
from .gmail_client import FetchedEmail, build_credentials, fetch_recent_emails
from .state import filter_unseen, is_first_run, mark_bootstrapped, mark_processed
from .telegram_client import send_message

# ---- logging: structured JSON to stdout ----------------------------------

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

# Hard cap on emails per invoke. This is an UPPER bound; quiet days do less.
# Sized to fit Lambda's 15-min hard timeout under Tier 1's 30k tokens/min
# budget: empirically ~70s per 15-email batch (mix of normal calls and
# rate-limit-triggered 65s sleeps), so 150 emails -> 10 batches -> ~12 min
# with ~3 min of margin. Going higher (e.g. 250) timed out in production
# and AWS retried the failed async invocation, doubling token spend.
MAX_EMAILS_PER_RUN = int(os.getenv("MAX_EMAILS_PER_RUN", "150"))


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "level": record.levelname,
            "msg": record.getMessage(),
            "ts": datetime.now(tz=timezone.utc).isoformat(),
            "logger": record.name,
        }
        for k, v in record.__dict__.items():
            if k in payload or k.startswith("_") or k in logging.LogRecord.__dict__:
                continue
            if k in ("args", "msg", "levelname", "name", "exc_info", "exc_text",
                    "stack_info", "lineno", "pathname", "filename", "module",
                    "msecs", "relativeCreated", "thread", "threadName",
                    "processName", "process", "created", "funcName"):
                continue
            payload[k] = v
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def _setup_logging() -> None:
    root = logging.getLogger()
    root.setLevel(LOG_LEVEL)
    for h in list(root.handlers):
        root.removeHandler(h)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_JsonFormatter())
    root.addHandler(handler)


_setup_logging()
logger = logging.getLogger("handler")


# ---- secrets --------------------------------------------------------------


def _load_secrets(param_path: str) -> dict:
    """Fetch all SecureString parameters under `param_path` in one paginated call.

    Returns a dict keyed by the last path segment (e.g. 'gmail_refresh_token').
    """

    ssm = boto3.client("ssm")
    out: dict[str, str] = {}
    paginator = ssm.get_paginator("get_parameters_by_path")
    for page in paginator.paginate(
        Path=param_path, WithDecryption=True, Recursive=False
    ):
        for p in page["Parameters"]:
            key = p["Name"].rsplit("/", 1)[-1]
            out[key] = p["Value"]
    return out


# ---- formatting -----------------------------------------------------------

SECTION_ORDER = [
    ("interview_invite", "🎯 Interviews"),
    ("rejection", "❌ Rejections"),
    ("recruiter_outreach", "📞 Recruiter outreach"),
    ("followup_needed", "⏰ Action needed"),
    ("application_received", "✅ Applications confirmed"),
]


def _fmt_line(item: Classified) -> str:
    parts = [item.company or "Unknown company"]
    if item.role:
        parts.append(item.role)
    if item.category in ("interview_invite", "followup_needed"):
        if item.next_step:
            parts.append(item.next_step)
        if item.deadline:
            parts.append(item.deadline)
    if item.category == "recruiter_outreach" and item.link:
        parts.append(item.link)
    return "- " + " — ".join(p for p in parts if p)


def format_summary(
    items: list[Classified], scanned: int, window_label: str
) -> str:
    today = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
    lines = [
        f"📬 *Job Search Summary — {today}*",
        f"_Scanned {scanned} emails over last {window_label}_",
        "",
    ]
    by_cat: dict[str, list[Classified]] = {}
    for it in items:
        by_cat.setdefault(it.category, []).append(it)

    for cat, header in SECTION_ORDER:
        bucket = by_cat.get(cat, [])
        if not bucket:
            continue
        lines.append(f"*{header} ({len(bucket)})*")
        for it in bucket:
            lines.append(_fmt_line(it))
        lines.append("")

    other = len(by_cat.get("other", []))
    if other:
        lines.append(f"_{other} other emails skipped._")

    return "\n".join(lines).rstrip() + "\n"


# ---- window selection -----------------------------------------------------


def _lookback_hours(state_table: str) -> tuple[int, str]:
    """First run -> 14d, otherwise -> 24h. Uses a sentinel GetItem (no Scan permission needed)."""

    if is_first_run(state_table):
        return 24 * 14, "14 days"
    return 24, "24 hours"


# ---- lambda entry ---------------------------------------------------------


def lambda_handler(event, context):
    started = time.monotonic()
    param_path = os.environ["SSM_PARAM_PATH"]
    state_table = os.environ["STATE_TABLE_NAME"]

    secrets = _load_secrets(param_path)
    lookback, window_label = _lookback_hours(state_table)
    logger.info("run_start", extra={"window_hours": lookback, "window_label": window_label})

    creds = build_credentials(
        secrets["gmail_client_id"],
        secrets["gmail_client_secret"],
        secrets["gmail_refresh_token"],
    )
    # Cap fetch — Anthropic Tier 1 (30k input tok/min) can only realistically
    # classify a few dozen emails per invoke without sleeping past Lambda's
    # timeout. Fetching more than we'll classify just burns Gmail API quota.
    max_to_fetch = MAX_EMAILS_PER_RUN * 3
    fetched: list[FetchedEmail] = fetch_recent_emails(
        creds, lookback_hours=lookback, max_results=max_to_fetch
    )
    # Newest first so the cap drops the oldest, least-actionable mail.
    fetched.sort(key=lambda e: e.received_at, reverse=True)

    all_ids = [e.id for e in fetched]
    unseen_ids = filter_unseen(state_table, all_ids)
    unseen = [e for e in fetched if e.id in unseen_ids]
    to_classify = unseen[:MAX_EMAILS_PER_RUN]
    capped = len(unseen) - len(to_classify)
    skipped_dedup = len(fetched) - len(unseen)
    if capped:
        logger.info(
            "run_capped",
            extra={"unseen": len(unseen), "classifying": len(to_classify), "dropped": capped},
        )

    if not to_classify:
        logger.info("run_no_new_emails", extra={"fetched": len(fetched)})
        mark_processed(state_table, all_ids)
        mark_bootstrapped(state_table)
        # No Telegram ping when there's nothing to report — empty summaries
        # are noise. Cron will fire again tomorrow.
        return _result(len(fetched), 0, skipped_dedup, 0, 0, 0, started)

    # max_retries=0: rate-limit retries are useless because the SDK's
    # millisecond-scale exponential backoff can't outwait a 60s sliding
    # window. Our own classifier.py retries with the correct 65s sleep.
    client = Anthropic(api_key=secrets["anthropic_api_key"], max_retries=0)
    cls = classify_emails(client, to_classify)

    text = format_summary(cls.items, scanned=len(fetched), window_label=window_label)
    chunks = send_message(
        secrets["telegram_bot_token"], secrets["telegram_chat_id"], text
    )

    # Mark every fetched email — including ones we dropped due to the cap —
    # so they aren't re-fetched next run. The dropped ones are gone for good;
    # for job-search triage that's acceptable since stale emails matter less.
    mark_processed(state_table, all_ids)
    mark_bootstrapped(state_table)

    return _result(
        len(fetched),
        len(cls.items),
        skipped_dedup,
        cls.tokens_input,
        cls.tokens_output,
        chunks,
        started,
    )


def _result(
    fetched: int,
    classified: int,
    skipped_dedup: int,
    tokens_in: int,
    tokens_out: int,
    telegram_sent: int,
    started: float,
) -> dict:
    duration_ms = int((time.monotonic() - started) * 1000)
    payload = {
        "emails_fetched": fetched,
        "emails_classified": classified,
        "emails_skipped_dedup": skipped_dedup,
        "tokens_input": tokens_in,
        "tokens_output": tokens_out,
        "telegram_sent": telegram_sent,
        "duration_ms": duration_ms,
    }
    logger.info("run_done", extra=payload)
    return payload
