"""Small DynamoDB AttributeValue (de)serializer, local to this Lambda's
asset -- mirrors the same hand-rolled pattern already used independently in
lambdas/read_api, lambdas/device_api and lambdas/push_api rather than
depending across lambda packages.
"""

from __future__ import annotations

from typing import Any


def item(values: dict[str, Any]) -> dict[str, dict[str, Any]]:
    def attribute(value: Any) -> dict[str, Any]:
        if isinstance(value, bool):
            return {"BOOL": value}
        if isinstance(value, str):
            return {"S": value}
        if isinstance(value, int):
            return {"N": str(value)}
        if value is None:
            return {"NULL": True}
        if isinstance(value, list):
            return {"L": [attribute(element) for element in value]}
        if isinstance(value, dict):
            return {"M": item(value)}
        raise TypeError("unsupported item value")

    return {key: attribute(value) for key, value in values.items()}


def plain(attributes: dict[str, Any]) -> dict[str, Any]:
    def value(attribute: dict[str, Any]) -> Any:
        if "S" in attribute:
            return attribute["S"]
        if "N" in attribute:
            return int(attribute["N"])
        if "BOOL" in attribute:
            return attribute["BOOL"]
        if "NULL" in attribute:
            return None
        if "M" in attribute:
            return {key: value(element) for key, element in attribute["M"].items()}
        if "L" in attribute:
            return [value(element) for element in attribute["L"]]
        raise ValueError("unsupported DynamoDB attribute")

    return {key: value(attribute) for key, attribute in attributes.items()}
