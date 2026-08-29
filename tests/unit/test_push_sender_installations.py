from __future__ import annotations

from typing import Any

from lambdas.push_sender.installations import (
    BATCH_GET_CHUNK,
    MAX_INSTALLATIONS_PER_DEVICE,
    active_installations,
)

TABLE = "push-table"
INDEX = "by-user-index"


def av(item: dict[str, object]) -> dict[str, dict[str, object]]:
    return {
        key: ({"N": str(value)} if isinstance(value, int) else {"S": value})
        for key, value in item.items()
    }


class FakeDdb:
    def __init__(
        self,
        query_pages: dict[str, list[list[dict[str, object]]]],
        installations: dict[str, dict[str, object]],
        *,
        unprocessed_rounds: int = 0,
    ) -> None:
        self.query_pages = {user: list(pages) for user, pages in query_pages.items()}
        self.installations = installations
        self.unprocessed_rounds = unprocessed_rounds
        self.batch_calls: list[dict[str, Any]] = []

    def query(self, **kwargs: Any) -> dict[str, Any]:
        user_id = kwargs["ExpressionAttributeValues"][":u"]["S"]
        pages = self.query_pages.get(user_id, [])
        page = pages.pop(0) if pages else []
        result: dict[str, Any] = {"Items": [av(item) for item in page]}
        if pages:
            result["LastEvaluatedKey"] = {"user_id": {"S": user_id}}
        return result

    def batch_get_item(self, **kwargs: Any) -> dict[str, Any]:
        self.batch_calls.append(kwargs)
        keys = kwargs["RequestItems"][TABLE]["Keys"]
        if self.unprocessed_rounds > 0:
            self.unprocessed_rounds -= 1
            served, pending = [], keys
        else:
            served, pending = keys, []
        responses = []
        for key in served:
            installation_id = key["pk"]["S"].removeprefix("INSTALLATION#")
            if installation_id in self.installations:
                responses.append(av(self.installations[installation_id]))
        result: dict[str, Any] = {"Responses": {TABLE: responses}}
        if pending:
            result["UnprocessedKeys"] = {TABLE: {"Keys": pending}}
        return result


def installation(installation_id: str, user_id: str, token: str = "tok") -> dict[str, object]:
    return {
        "pk": f"INSTALLATION#{installation_id}",
        "sk": "INSTALLATION",
        "installation_id": installation_id,
        "user_id": user_id,
        "token": token,
        "token_hash": f"hash-{installation_id}",
    }


def test_zero_installations_is_a_valid_empty_result() -> None:
    ddb = FakeDdb({"u1": [[]]}, {})
    installations, truncated = active_installations(ddb, TABLE, INDEX, ["u1"])
    assert installations == []
    assert truncated is False


def test_single_user_single_installation() -> None:
    ddb = FakeDdb(
        {"u1": [[{"user_id": "u1", "installation_id": "i1"}]]},
        {"i1": installation("i1", "u1")},
    )
    installations, truncated = active_installations(ddb, TABLE, INDEX, ["u1"])
    assert [i["installation_id"] for i in installations] == ["i1"]
    assert truncated is False


def test_multiple_users_multiple_installations_are_all_collected() -> None:
    ddb = FakeDdb(
        {
            "u1": [
                [
                    {"user_id": "u1", "installation_id": "i1"},
                    {"user_id": "u1", "installation_id": "i2"},
                ]
            ],
            "u2": [[{"user_id": "u2", "installation_id": "i3"}]],
        },
        {
            "i1": installation("i1", "u1"),
            "i2": installation("i2", "u1"),
            "i3": installation("i3", "u2"),
        },
    )
    installations, truncated = active_installations(ddb, TABLE, INDEX, ["u1", "u2"])
    assert {i["installation_id"] for i in installations} == {"i1", "i2", "i3"}
    assert truncated is False


def test_duplicate_installation_ids_are_deduplicated() -> None:
    # Defensive: even though the GSI should not structurally produce
    # duplicates, the adapter must not double-send if it somehow did.
    ddb = FakeDdb(
        {"u1": [[{"user_id": "u1", "installation_id": "i1"}]]},
        {"i1": installation("i1", "u1")},
    )
    ddb.query_pages["u1"] = [
        [
            {"user_id": "u1", "installation_id": "i1"},
            {"user_id": "u1", "installation_id": "i1"},
        ]
    ]
    installations, _ = active_installations(ddb, TABLE, INDEX, ["u1"])
    assert len(installations) == 1


def test_gsi_query_pagination_across_multiple_pages_is_followed() -> None:
    ddb = FakeDdb(
        {
            "u1": [
                [{"user_id": "u1", "installation_id": "i1"}],
                [{"user_id": "u1", "installation_id": "i2"}],
            ]
        },
        {"i1": installation("i1", "u1"), "i2": installation("i2", "u1")},
    )
    installations, truncated = active_installations(ddb, TABLE, INDEX, ["u1"])
    assert {i["installation_id"] for i in installations} == {"i1", "i2"}


def test_fan_out_is_capped_and_reports_truncation() -> None:
    many = [
        {"user_id": "u1", "installation_id": f"i{i}"}
        for i in range(MAX_INSTALLATIONS_PER_DEVICE + 5)
    ]
    installs = {
        f"i{i}": installation(f"i{i}", "u1") for i in range(MAX_INSTALLATIONS_PER_DEVICE + 5)
    }
    ddb = FakeDdb({"u1": [many]}, installs)
    installations, truncated = active_installations(ddb, TABLE, INDEX, ["u1"])
    assert len(installations) == MAX_INSTALLATIONS_PER_DEVICE
    assert truncated is True


def test_batch_get_chunks_across_the_100_item_limit() -> None:
    count = BATCH_GET_CHUNK + 20
    many = [{"user_id": "u1", "installation_id": f"i{i}"} for i in range(count)]
    installs = {f"i{i}": installation(f"i{i}", "u1") for i in range(count)}
    ddb = FakeDdb({"u1": [many]}, installs)
    installations, _ = active_installations(ddb, TABLE, INDEX, ["u1"])
    assert len(installations) == count
    assert len(ddb.batch_calls) == 2


def test_unprocessed_keys_are_retried_with_backoff() -> None:
    ddb = FakeDdb(
        {
            "u1": [
                [
                    {"user_id": "u1", "installation_id": "i1"},
                    {"user_id": "u1", "installation_id": "i2"},
                ]
            ]
        },
        {"i1": installation("i1", "u1"), "i2": installation("i2", "u1")},
        unprocessed_rounds=1,
    )
    sleeps: list[float] = []
    installations, _ = active_installations(ddb, TABLE, INDEX, ["u1"], sleeper=sleeps.append)
    assert {i["installation_id"] for i in installations} == {"i1", "i2"}
    assert len(sleeps) == 1


def test_unprocessed_keys_exhausting_retries_raises() -> None:
    ddb = FakeDdb(
        {
            "u1": [
                [
                    {"user_id": "u1", "installation_id": "i1"},
                    {"user_id": "u1", "installation_id": "i2"},
                ]
            ]
        },
        {"i1": installation("i1", "u1"), "i2": installation("i2", "u1")},
        unprocessed_rounds=10,
    )
    try:
        active_installations(ddb, TABLE, INDEX, ["u1"], sleeper=lambda _: None)
        raise AssertionError("expected RuntimeError")
    except RuntimeError:
        pass
