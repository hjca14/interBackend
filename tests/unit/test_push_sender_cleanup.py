from __future__ import annotations

from typing import Any

from lambdas.push_sender.cleanup import delete_invalid_installation

TABLE = "push-table"


class Conflict(Exception):
    response = {"Error": {"Code": "TransactionCanceledException"}}


class FakeDdb:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[list[dict[str, Any]]] = []

    def transact_write_items(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs["TransactItems"])
        if self.fail:
            raise Conflict
        return {}


def test_deletes_both_authoritative_items_transactionally() -> None:
    ddb = FakeDdb()
    deleted = delete_invalid_installation(
        ddb, TABLE, installation_id="iid-1", user_id="user-a", token_hash="hash-1"
    )
    assert deleted is True
    actions = ddb.calls[0]
    assert len(actions) == 2
    installation_delete = actions[0]["Delete"]
    claim_delete = actions[1]["Delete"]
    assert installation_delete["Key"]["pk"]["S"] == "INSTALLATION#iid-1"
    assert claim_delete["Key"]["pk"]["S"] == "TOKEN#hash-1"
    assert installation_delete["ExpressionAttributeValues"][":uid"]["S"] == "user-a"
    assert installation_delete["ExpressionAttributeValues"][":hash"]["S"] == "hash-1"


def test_condition_failure_is_a_benign_skip_not_an_error() -> None:
    ddb = FakeDdb(fail=True)
    deleted = delete_invalid_installation(
        ddb, TABLE, installation_id="iid-1", user_id="user-a", token_hash="hash-1"
    )
    assert deleted is False


def test_unexpected_infrastructure_error_propagates() -> None:
    class BrokenDdb(FakeDdb):
        def transact_write_items(self, **kwargs: Any) -> dict[str, Any]:
            raise RuntimeError("dependency unavailable")

    ddb = BrokenDdb()
    try:
        delete_invalid_installation(
            ddb, TABLE, installation_id="iid-1", user_id="user-a", token_hash="hash-1"
        )
        raise AssertionError("expected RuntimeError")
    except RuntimeError:
        pass
