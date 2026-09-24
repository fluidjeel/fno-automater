"""Phase-1 confidence-bucket sizing (ADESK-D1). Downscale only; never live BOUNDED.

PART 7.2/7.3: agent confidence is a closed ConfidenceBucket; size_multiplier is
mapped deterministically and type-capped at 1.0. Phase-2 upscale (ADESK-D6) is a
separate deterministic envelope — this module never emits m > 1.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal

from pydantic import Field, model_validator

from trading.domain.contracts.base import ExactDecimal, StrictModel
from trading.domain.enums import ConfidenceBucket

__all__ = [
    "PHASE1_M_CEILING",
    "PHASE1_M_FLOOR",
    "PHASE1_SIZE_MULTIPLIERS",
    "Phase1SizingAdvice",
    "bucket_from_confidence",
    "phase1_size_multiplier",
]

# Spec-suggested floor; agent may halve but not enlarge (Phase 1).
PHASE1_M_FLOOR = Decimal("0.5")
PHASE1_M_CEILING = Decimal("1")

# Deterministic bucket → multiplier. Every value is in [m_floor, 1.0] by construction.
PHASE1_SIZE_MULTIPLIERS: Mapping[ConfidenceBucket, Decimal] = {
    ConfidenceBucket.P10: PHASE1_M_FLOOR,
    ConfidenceBucket.P30: Decimal("0.625"),
    ConfidenceBucket.P50: Decimal("0.75"),
    ConfidenceBucket.P70: Decimal("0.875"),
    ConfidenceBucket.P90: Decimal("1.0"),
}


def _assert_phase1_table() -> None:
    if set(PHASE1_SIZE_MULTIPLIERS) != set(ConfidenceBucket):
        missing = set(ConfidenceBucket) - set(PHASE1_SIZE_MULTIPLIERS)
        raise RuntimeError(f"PHASE1_SIZE_MULTIPLIERS missing buckets: {missing}")
    for bucket, multiplier in PHASE1_SIZE_MULTIPLIERS.items():
        if multiplier < PHASE1_M_FLOOR or multiplier > PHASE1_M_CEILING:
            raise RuntimeError(
                f"Phase-1 multiplier for {bucket} must be in "
                f"[{PHASE1_M_FLOOR}, {PHASE1_M_CEILING}], got {multiplier}"
            )


_assert_phase1_table()


def bucket_from_confidence(value: Decimal | str | ConfidenceBucket) -> ConfidenceBucket:
    """Map a confidence value onto ConfidenceBucket. Unknown values fail closed."""
    if isinstance(value, ConfidenceBucket):
        return value
    if isinstance(value, str):
        try:
            return ConfidenceBucket(value)
        except ValueError as exc:
            raise ValueError(
                f"confidence {value!r} is not a ConfidenceBucket member; "
                "Phase-1 sizing rejects continuous or unknown values"
            ) from exc
    quantized = Decimal(value)
    # Accept exact bucket decimals only — no nearest-bucket rounding.
    format(quantized, "f")
    # Normalize "0.10" / "0.1" via Decimal compare against members.
    for bucket in ConfidenceBucket:
        if Decimal(bucket.value) == quantized:
            return bucket
    raise ValueError(
        f"confidence {value!r} is not a closed ConfidenceBucket "
        f"({', '.join(b.value for b in ConfidenceBucket)}); fail closed"
    )


def phase1_size_multiplier(bucket: ConfidenceBucket) -> Decimal:
    """Return the Phase-1 downscale multiplier for a bucket (always <= 1.0)."""
    multiplier = PHASE1_SIZE_MULTIPLIERS[bucket]
    if multiplier > PHASE1_M_CEILING:
        raise ValueError(
            f"Phase-1 multiplier {multiplier} exceeds ceiling {PHASE1_M_CEILING}"
        )
    return multiplier


class Phase1SizingAdvice(StrictModel):
    """Shared sizing advice: closed bucket → type-capped multiplier <= 1.0."""

    confidence_bucket: ConfidenceBucket
    size_multiplier: ExactDecimal = Field(le=Decimal("1"), ge=Decimal("0"))

    @model_validator(mode="after")
    def _matches_phase1_table(self) -> Phase1SizingAdvice:
        expected = phase1_size_multiplier(self.confidence_bucket)
        if self.size_multiplier != expected:
            raise ValueError(
                f"size_multiplier {self.size_multiplier} does not match Phase-1 "
                f"map for {self.confidence_bucket} (expected {expected})"
            )
        if self.size_multiplier > PHASE1_M_CEILING:
            raise ValueError("Phase-1 size_multiplier cannot exceed 1.0")
        return self

    @classmethod
    def from_bucket(cls, bucket: ConfidenceBucket) -> Phase1SizingAdvice:
        return cls(
            confidence_bucket=bucket,
            size_multiplier=phase1_size_multiplier(bucket),
        )

    @classmethod
    def from_confidence(
        cls, confidence: Decimal | str | ConfidenceBucket
    ) -> Phase1SizingAdvice:
        return cls.from_bucket(bucket_from_confidence(confidence))
