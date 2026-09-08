"""Bounded pure validation primitives for Capability OS execution contracts.

This module deliberately supports a conservative JSON-Schema subset and a tiny
predicate language. Unsupported schema keywords or predicate operators fail
closed instead of falling through to arbitrary evaluation.
"""

from __future__ import annotations

from typing import Any


class ContractValidationError(ValueError):
    pass


_MISSING = object()
_ALLOWED_SCHEMA_KEYS = frozenset({
    "type",
    "required",
    "properties",
    "additionalProperties",
    "items",
    "enum",
    "const",
    "minimum",
    "maximum",
    "minLength",
    "maxLength",
    "minItems",
    "maxItems",
})


def _schema_type_matches(value: Any, expected: str) -> bool:
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    raise ContractValidationError(f"unsupported JSON schema type: {expected}")


def validate_schema(value: Any, schema: dict[str, Any], *, path: str = "$") -> None:
    if not isinstance(schema, dict):
        raise ContractValidationError("JSON schema must be an object")
    unknown = set(schema) - _ALLOWED_SCHEMA_KEYS
    if unknown:
        raise ContractValidationError(f"unsupported JSON schema keyword: {sorted(unknown)[0]}")

    declared_type = schema.get("type")
    if declared_type is not None:
        if isinstance(declared_type, str):
            allowed_types = (declared_type,)
        elif isinstance(declared_type, list) and declared_type and all(
            isinstance(item, str) for item in declared_type
        ):
            allowed_types = tuple(declared_type)
        else:
            raise ContractValidationError("JSON schema type must be a string or non-empty array")
        if not any(_schema_type_matches(value, item) for item in allowed_types):
            raise ContractValidationError(f"{path} does not match declared type")

    if "const" in schema and value != schema["const"]:
        raise ContractValidationError(f"{path} does not match const")
    if "enum" in schema:
        options = schema["enum"]
        if not isinstance(options, list) or value not in options:
            raise ContractValidationError(f"{path} is outside enum")

    if isinstance(value, dict):
        required = schema.get("required", [])
        if not isinstance(required, list) or any(not isinstance(item, str) for item in required):
            raise ContractValidationError("JSON schema required must be a string array")
        missing = [item for item in required if item not in value]
        if missing:
            raise ContractValidationError(f"{path} is missing required property: {missing[0]}")
        properties = schema.get("properties", {})
        if not isinstance(properties, dict):
            raise ContractValidationError("JSON schema properties must be an object")
        additional = schema.get("additionalProperties", True)
        if not isinstance(additional, bool):
            raise ContractValidationError("additionalProperties must be boolean")
        for key, item in value.items():
            child = properties.get(key)
            if child is None:
                if not additional:
                    raise ContractValidationError(f"{path}.{key} is not allowed")
                continue
            validate_schema(item, child, path=f"{path}.{key}")

    if isinstance(value, list):
        minimum = schema.get("minItems")
        maximum = schema.get("maxItems")
        if minimum is not None and len(value) < int(minimum):
            raise ContractValidationError(f"{path} has too few items")
        if maximum is not None and len(value) > int(maximum):
            raise ContractValidationError(f"{path} has too many items")
        item_schema = schema.get("items")
        if item_schema is not None:
            if not isinstance(item_schema, dict):
                raise ContractValidationError("JSON schema items must be an object")
            for index, item in enumerate(value):
                validate_schema(item, item_schema, path=f"{path}[{index}]")

    if isinstance(value, str):
        minimum = schema.get("minLength")
        maximum = schema.get("maxLength")
        if minimum is not None and len(value) < int(minimum):
            raise ContractValidationError(f"{path} is shorter than minLength")
        if maximum is not None and len(value) > int(maximum):
            raise ContractValidationError(f"{path} exceeds maxLength")

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        minimum = schema.get("minimum")
        maximum = schema.get("maximum")
        if minimum is not None and value < minimum:
            raise ContractValidationError(f"{path} is below minimum")
        if maximum is not None and value > maximum:
            raise ContractValidationError(f"{path} exceeds maximum")


def _resolve_path(context: dict[str, Any], raw_path: str) -> Any:
    path = str(raw_path or "").strip()
    if not path or len(path) > 500:
        raise ContractValidationError("predicate path is invalid")
    current: Any = context
    for segment in path.split("."):
        if not segment or len(segment) > 200:
            raise ContractValidationError("predicate path segment is invalid")
        if isinstance(current, dict) and segment in current:
            current = current[segment]
        else:
            return _MISSING
    return current


def evaluate_predicates(predicates: tuple[Any, ...], *, context: dict[str, Any], label: str) -> None:
    if len(predicates) > 100:
        raise ContractValidationError(f"{label} exceeds predicate bound")
    for index, raw in enumerate(predicates):
        if not isinstance(raw, dict):
            raise ContractValidationError(f"{label}[{index}] must be an object")
        unknown = set(raw) - {"path", "op", "value"}
        if unknown:
            raise ContractValidationError(f"unsupported predicate field: {sorted(unknown)[0]}")
        path = str(raw.get("path") or "")
        op = str(raw.get("op") or "").strip().lower()
        actual = _resolve_path(context, path)
        expected = raw.get("value")
        if op == "exists":
            passed = actual is not _MISSING
        elif op == "missing":
            passed = actual is _MISSING
        elif actual is _MISSING:
            passed = False
        elif op == "eq":
            passed = actual == expected
        elif op == "ne":
            passed = actual != expected
        elif op == "truthy":
            passed = bool(actual)
        elif op == "falsy":
            passed = not bool(actual)
        elif op in {"in", "not_in"}:
            if not isinstance(expected, list):
                raise ContractValidationError(f"{label}[{index}] {op} value must be an array")
            contained = actual in expected
            passed = contained if op == "in" else not contained
        else:
            raise ContractValidationError(f"unsupported predicate operator: {op}")
        if not passed:
            raise ContractValidationError(f"{label}[{index}] failed: {path} {op}")
