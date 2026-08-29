"""Adapter: safe removal of a definitively invalid push installation.

Only ever called after the FCM result classification is
``INVALID_TOKEN`` (see ``domain/push/fcm_result.py`` -- FCM's own
``UNREGISTERED`` signal). Deletes both authoritative Fase 3B.5 items
(``INSTALLATION#<id>``/``INSTALLATION`` and ``TOKEN#<hash>``/``CLAIM``) in
one transaction, conditioned on the exact ``user_id``/``token_hash`` this
sender just read. A concurrent re-registration, token rotation, or account
switch changes those values, so the condition fails and the delete is
safely skipped instead of removing a *different*, now-valid installation
-- the same transactional invariant ``lambdas/push_api/handler.py``
already relies on for writes.
"""

from __future__ import annotations

from typing import Any

from .dynamo import item


def delete_invalid_installation(
    ddb: Any, table_name: str, *, installation_id: str, user_id: str, token_hash: str
) -> bool:
    """Returns ``True`` if deleted, ``False`` if the condition no longer
    held -- an expected, benign race, not an error.
    """
    try:
        ddb.transact_write_items(
            TransactItems=[
                {
                    "Delete": {
                        "TableName": table_name,
                        "Key": item(
                            {"pk": f"INSTALLATION#{installation_id}", "sk": "INSTALLATION"}
                        ),
                        "ConditionExpression": "user_id = :uid AND token_hash = :hash",
                        "ExpressionAttributeValues": item({":uid": user_id, ":hash": token_hash}),
                    }
                },
                {
                    "Delete": {
                        "TableName": table_name,
                        "Key": item({"pk": f"TOKEN#{token_hash}", "sk": "CLAIM"}),
                        "ConditionExpression": (
                            "claimed_installation_id = :iid AND claimed_user_id = :uid"
                        ),
                        "ExpressionAttributeValues": item(
                            {":iid": installation_id, ":uid": user_id}
                        ),
                    }
                },
            ]
        )
        return True
    except Exception as error:
        if _is_conflict(error):
            return False
        raise


def _is_conflict(error: Exception) -> bool:
    response = getattr(error, "response", None)
    details = response.get("Error") if isinstance(response, dict) else None
    code = details.get("Code") if isinstance(details, dict) else None
    return code in {"TransactionCanceledException", "ConditionalCheckFailedException"}
