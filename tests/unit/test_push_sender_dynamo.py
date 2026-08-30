from __future__ import annotations

import pytest

from lambdas.push_sender.dynamo import item, plain


def test_item_serializes_every_supported_python_type() -> None:
    assert item(
        {
            "flag": True,
            "name": "text",
            "count": 3,
            "empty": None,
            "days": [1, 2, 3],
            "nested": {"a": "b", "c": 1},
        }
    ) == {
        "flag": {"BOOL": True},
        "name": {"S": "text"},
        "count": {"N": "3"},
        "empty": {"NULL": True},
        "days": {"L": [{"N": "1"}, {"N": "2"}, {"N": "3"}]},
        "nested": {"M": {"a": {"S": "b"}, "c": {"N": "1"}}},
    }


def test_item_rejects_unsupported_python_types() -> None:
    with pytest.raises(TypeError):
        item({"bad": 3.14})
    with pytest.raises(TypeError):
        item({"bad": object()})


def test_plain_is_the_exact_inverse_of_item_for_supported_values() -> None:
    values = {
        "flag": True,
        "name": "text",
        "count": 3,
        "empty": None,
        "days": [1, 2, 3],
        "nested": {"a": "b", "c": 1},
    }
    assert plain(item(values)) == values


def test_plain_rejects_unsupported_or_malformed_attribute_shapes() -> None:
    with pytest.raises(ValueError):
        plain({"bad": {}})
    with pytest.raises(ValueError):
        plain({"bad": {"B": "binary-not-supported"}})
