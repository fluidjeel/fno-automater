"""Shared base model and field types for every versioned contract.

DOMAIN_CONTRACTS.md requires each model to reject unknown fields, NaN and
infinity, naive time and invalid precision, and to serialize and replay without
information loss.

The float ban is enforced once, recursively, on the raw payload of every model.
Doing it per-field would miss the interesting case: pydantic happily coerces a
JSON float into the Decimal inside a nested Money, so a float can smuggle itself
into an accounting path through a dict that never mentions Decimal.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, Self

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    StrictBool,
    StrictInt,
    StrictStr,
    model_validator,
)

from trading.domain.clock import ensure_utc

__all__ = [
    "SCHEMA_VERSION",
    "ContractError",
    "ExactDecimal",
    "NonEmptyStr",
    "StrictBool",
    "StrictInt",
    "StrictModel",
    "StrictStr",
    "UtcDatetime",
    "VersionedModel",
]

# Bumping this is a contract change: producers and consumers must be retested.
SCHEMA_VERSION = "1"


class ContractError(ValueError):
    """Raised when a payload violates a contract rule."""


def _to_utc(value: Any) -> Any:
    """Accept an aware datetime or an ISO string; reject naive time."""
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError as exc:
            raise ContractError(f"not an ISO-8601 timestamp: {value!r}") from exc
    if isinstance(value, datetime):
        return ensure_utc(value)
    return value


def _exact_decimal(value: Any) -> Any:
    """Accept Decimal, int or str. Reject float and non-finite values."""
    if isinstance(value, bool):
        raise ContractError("a bool is not a numeric value")
    if isinstance(value, float):
        raise ContractError(
            "float is not accepted for an exact numeric field; use Decimal, int or str"
        )
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ContractError(f"value must be finite, got {value}")
        return value
    if isinstance(value, int | str):
        try:
            decimal_value = Decimal(value)
        except ArithmeticError as exc:
            raise ContractError(f"not a valid decimal: {value!r}") from exc
        if not decimal_value.is_finite():
            raise ContractError(f"value must be finite, got {value!r}")
        return decimal_value
    return value


def _non_empty(value: Any) -> Any:
    if isinstance(value, str) and not value.strip():
        raise ContractError("identifier must not be empty or whitespace")
    return value


UtcDatetime = Annotated[datetime, BeforeValidator(_to_utc)]
ExactDecimal = Annotated[Decimal, BeforeValidator(_exact_decimal)]
NonEmptyStr = Annotated[StrictStr, BeforeValidator(_non_empty)]


def _assert_no_float(value: Any, path: str) -> None:
    if isinstance(value, float):
        raise ContractError(
            f"{path}: float is not permitted anywhere in a contract payload. "
            "Binary floats cannot represent decimal money, prices or ratios "
            "exactly, so they are rejected before they reach an accounting path."
        )
    if isinstance(value, Mapping):
        for key, item in value.items():
            _assert_no_float(item, f"{path}.{key}")
    elif isinstance(value, Sequence) and not isinstance(value, str | bytes):
        for index, item in enumerate(value):
            _assert_no_float(item, f"{path}[{index}]")


class StrictModel(BaseModel):
    """Immutable, closed model. Unknown fields and floats are rejected."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        validate_assignment=True,
        validate_default=True,
        arbitrary_types_allowed=False,
        ser_json_inf_nan="strings",
    )

    @model_validator(mode="before")
    @classmethod
    def _reject_floats(cls, data: Any) -> Any:
        _assert_no_float(data, cls.__name__)
        return data

    def round_trip(self) -> Self:
        """Serialize to JSON-compatible primitives and validate back.

        Used by contract tests to prove no information is lost.
        """
        return type(self).model_validate(self.model_dump(mode="json"))


class VersionedModel(StrictModel):
    """A contract that records the schema it was written against."""

    schema_version: NonEmptyStr = SCHEMA_VERSION
