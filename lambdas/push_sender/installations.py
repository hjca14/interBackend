"""Adapter: active push installations for a set of user_ids.

Two steps, matching ``docs/data-model.md``'s description of the 3B.5
``PushInstallations`` table:

1. Query the ``*-push-installations-by-user-index`` GSI (``KEYS_ONLY``
   projection) per ``user_id`` to discover ``installation_id``s. This
   index's eventual consistency is acceptable here: it only ever narrows
   or widens *which* installations this run attempts, and never decides
   ownership or exclusivity -- see the module docstring in
   ``lambdas/push_api/handler.py``.
2. ``BatchGetItem`` the authoritative ``INSTALLATION#<id>``/``INSTALLATION``
   item for each discovered id from the base table, which is where the
   token actually lives. The ``TOKEN#<hash>``/``CLAIM`` item is never read
   here -- it exists only to enforce claim exclusivity on write, per
   Fase 3B.5.

Deduplicates by ``installation_id``, paginates both the GSI query and the
batch reads, and enforces an explicit cap on total fan-out.
"""

from __future__ import annotations

import random
import time
from typing import Any

from .dynamo import item, plain

MAX_INSTALLATIONS_PER_DEVICE = 200
BATCH_GET_CHUNK = 100
MAX_BATCH_RETRY_ATTEMPTS = 3


def _installation_ids_for_user(
    ddb: Any, table_name: str, index_name: str, user_id: str
) -> list[str]:
    ids: list[str] = []
    exclusive_start_key: dict[str, Any] | None = None
    while True:
        kwargs: dict[str, Any] = {
            "TableName": table_name,
            "IndexName": index_name,
            "KeyConditionExpression": "user_id = :u",
            "ExpressionAttributeValues": {":u": {"S": user_id}},
        }
        if exclusive_start_key is not None:
            kwargs["ExclusiveStartKey"] = exclusive_start_key
        result = ddb.query(**kwargs)
        for raw in result.get("Items", []):
            parsed = plain(raw)
            installation_id = parsed.get("installation_id")
            if isinstance(installation_id, str):
                ids.append(installation_id)
        exclusive_start_key = result.get("LastEvaluatedKey")
        if not exclusive_start_key:
            return ids


def active_installations(
    ddb: Any,
    table_name: str,
    index_name: str,
    user_ids: list[str],
    *,
    sleeper: Any = time.sleep,
) -> tuple[list[dict[str, Any]], bool]:
    """Return ``(installations, truncated)`` -- each installation is the
    full plain ``INSTALLATION`` item (including ``token``).
    """
    seen_ids: set[str] = set()
    truncated = False
    for user_id in user_ids:
        for installation_id in _installation_ids_for_user(ddb, table_name, index_name, user_id):
            seen_ids.add(installation_id)
            if len(seen_ids) >= MAX_INSTALLATIONS_PER_DEVICE:
                truncated = True
                break
        if truncated:
            break

    installations: list[dict[str, Any]] = []
    ordered_ids = sorted(seen_ids)
    for start in range(0, len(ordered_ids), BATCH_GET_CHUNK):
        chunk = ordered_ids[start : start + BATCH_GET_CHUNK]
        keys = [
            item({"pk": f"INSTALLATION#{installation_id}", "sk": "INSTALLATION"})
            for installation_id in chunk
        ]
        pending = keys
        for attempt in range(MAX_BATCH_RETRY_ATTEMPTS):
            if not pending:
                break
            if attempt:
                sleeper(0.05 * (2 ** (attempt - 1)) + random.uniform(0, 0.025))
            batch = ddb.batch_get_item(RequestItems={table_name: {"Keys": pending}})
            for raw in batch.get("Responses", {}).get(table_name, []):
                parsed = plain(raw)
                if isinstance(parsed.get("token"), str) and isinstance(
                    parsed.get("installation_id"), str
                ):
                    installations.append(parsed)
            pending = batch.get("UnprocessedKeys", {}).get(table_name, {}).get("Keys", [])
        if pending:
            raise RuntimeError("push installation batch retry exhausted")
    return installations, truncated
