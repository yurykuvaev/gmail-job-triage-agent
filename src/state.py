"""DynamoDB-backed dedup store.

Schema:
    PK: message_id (string)
    Attributes: processed_at (ISO string), expires_at (epoch seconds, TTL)
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import boto3
from botocore.config import Config

logger = logging.getLogger(__name__)

TTL_DAYS = 30

# Sentinel row used to detect "first run" without needing dynamodb:Scan.
# Written once after the first successful run; checked by GetItem on startup.
BOOTSTRAP_KEY = "__bootstrap__"


def _table(table_name: str):
    ddb = boto3.resource(
        "dynamodb",
        config=Config(retries={"max_attempts": 5, "mode": "standard"}),
    )
    return ddb.Table(table_name)


def filter_unseen(table_name: str, message_ids: list[str]) -> set[str]:
    """Return the subset of `message_ids` that have NOT been processed yet."""

    if not message_ids:
        return set()
    table = _table(table_name)
    seen: set[str] = set()
    # BatchGetItem caps at 100 keys per request.
    ddb = boto3.client("dynamodb")
    for i in range(0, len(message_ids), 100):
        chunk = message_ids[i : i + 100]
        resp = ddb.batch_get_item(
            RequestItems={
                table.table_name: {
                    "Keys": [{"message_id": {"S": mid}} for mid in chunk],
                    "ProjectionExpression": "message_id",
                }
            }
        )
        for item in resp["Responses"].get(table.table_name, []):
            seen.add(item["message_id"]["S"])
    unseen = set(message_ids) - seen
    logger.info(
        "state_dedup",
        extra={"total": len(message_ids), "seen": len(seen), "unseen": len(unseen)},
    )
    return unseen


def is_first_run(table_name: str) -> bool:
    """First run = sentinel row absent. No Scan permission required."""

    resp = _table(table_name).get_item(Key={"message_id": BOOTSTRAP_KEY})
    return "Item" not in resp


def mark_bootstrapped(table_name: str) -> None:
    """Write the sentinel row (no TTL — must persist)."""

    _table(table_name).put_item(
        Item={
            "message_id": BOOTSTRAP_KEY,
            "processed_at": datetime.now(tz=timezone.utc).isoformat(),
        }
    )


def mark_processed(table_name: str, message_ids: list[str]) -> None:
    """Write dedup markers with TTL."""

    if not message_ids:
        return
    table = _table(table_name)
    now = datetime.now(tz=timezone.utc)
    expires_at = int((now + timedelta(days=TTL_DAYS)).timestamp())
    with table.batch_writer(overwrite_by_pkeys=["message_id"]) as batch:
        for mid in message_ids:
            batch.put_item(
                Item={
                    "message_id": mid,
                    "processed_at": now.isoformat(),
                    "expires_at": expires_at,
                }
            )
    logger.info("state_marked", extra={"count": len(message_ids)})
