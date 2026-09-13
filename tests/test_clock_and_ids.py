"""Time and identity boundaries.

Invariant 11: one logical order keeps one idempotency key across retries.
Invariant 19: critical values carry an explicit time.
Invariant 21: same snapshot, config and code version yields the same decision.
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from trading.domain.clock import (
    Clock,
    FrozenClock,
    SteppingClock,
    TimeError,
    ensure_utc,
    to_exchange_local,
)
from trading.domain.ids import (
    IdError,
    IdFactory,
    SequentialIdFactory,
    derive_idempotency_key,
)

IST = ZoneInfo("Asia/Kolkata")
INSTANT = datetime(2026, 9, 14, 4, 0, 0, tzinfo=UTC)

ORDER = {
    "account_id": "ACC-1",
    "strategy_id": "positional_index_options_poc",
    "strategy_version": "0.1.0",
    "intent_id": "INT-0001",
    "leg_id": "leg-1",
    "side": "BUY",
    "quantity_contracts": 75,
}


class TestNaiveTimeIsRejected:
    def test_ensure_utc_rejects_naive(self) -> None:
        with pytest.raises(TimeError, match="naive"):
            ensure_utc(datetime(2026, 9, 14, 9, 30))

    def test_ensure_utc_normalizes_other_zones(self) -> None:
        local = datetime(2026, 9, 14, 9, 30, tzinfo=IST)
        assert ensure_utc(local) == datetime(2026, 9, 14, 4, 0, tzinfo=UTC)

    def test_clock_constructors_reject_naive(self) -> None:
        with pytest.raises(TimeError, match="naive"):
            FrozenClock(datetime(2026, 9, 14, 9, 30))
        with pytest.raises(TimeError, match="naive"):
            SteppingClock(datetime(2026, 9, 14, 9, 30))

    def test_non_datetime_is_rejected(self) -> None:
        with pytest.raises(TimeError, match="must be a datetime"):
            ensure_utc("2026-09-14T09:30:00+05:30")  # type: ignore[arg-type]


class TestExchangeLocalRendering:
    def test_utc_instant_renders_in_exchange_time(self) -> None:
        """Session logic runs in exchange-local time; storage stays UTC."""
        assert to_exchange_local(INSTANT, IST).hour == 9
        assert to_exchange_local(INSTANT, IST).minute == 30

    def test_clock_exposes_both_views_of_one_instant(self) -> None:
        clock = FrozenClock(INSTANT)
        assert clock.now_utc() == INSTANT
        assert clock.now_in(IST) == INSTANT.astimezone(IST)


class TestClockDoubles:
    def test_frozen_clock_does_not_advance_on_read(self) -> None:
        clock = FrozenClock(INSTANT)
        assert clock.now_utc() == clock.now_utc()

    def test_frozen_clock_refuses_to_advance_backwards(self) -> None:
        clock = FrozenClock(INSTANT)
        with pytest.raises(TimeError, match="non-negative"):
            clock.advance(timedelta(seconds=-1))

    def test_stepping_clock_is_strictly_monotonic(self) -> None:
        clock = SteppingClock(INSTANT, step=timedelta(seconds=1))
        readings = [clock.now_utc() for _ in range(5)]
        assert readings == sorted(readings)
        assert len(set(readings)) == 5

    def test_stepping_clock_requires_a_positive_step(self) -> None:
        with pytest.raises(TimeError, match="monotonic"):
            SteppingClock(INSTANT, step=timedelta(0))

    def test_doubles_satisfy_the_clock_protocol(self) -> None:
        assert isinstance(FrozenClock(INSTANT), Clock)
        assert isinstance(SteppingClock(INSTANT), Clock)


class TestIdempotencyKeyStability:
    """Invariant 11: one logical order, one key, across retries and processes."""

    def test_same_logical_order_yields_the_same_key(self) -> None:
        assert derive_idempotency_key(**ORDER) == derive_idempotency_key(**ORDER)  # type: ignore[arg-type]

    def test_key_signature_admits_no_attempt_or_timestamp(self) -> None:
        """A retry cannot change the key because it cannot influence the inputs."""
        parameters = set(inspect.signature(derive_idempotency_key).parameters)
        assert parameters.isdisjoint({"attempt", "attempt_number", "now", "timestamp"})

    def test_key_is_stable_across_processes(self) -> None:
        """blake2b of an explicit field order; no PYTHONHASHSEED dependence."""
        assert derive_idempotency_key(**ORDER) == (  # type: ignore[arg-type]
            "83e65c7aafd8d0272bdd320d1d437938"
        )

    @pytest.mark.parametrize(
        "changed",
        [
            {"account_id": "ACC-2"},
            {"strategy_id": "other"},
            {"strategy_version": "0.2.0"},
            {"intent_id": "INT-0002"},
            {"leg_id": "leg-2"},
            {"side": "SELL"},
            {"quantity_contracts": 150},
        ],
    )
    def test_every_identifying_field_changes_the_key(
        self, changed: dict[str, object]
    ) -> None:
        assert derive_idempotency_key(**{**ORDER, **changed}) != derive_idempotency_key(  # type: ignore[arg-type]
            **ORDER  # type: ignore[arg-type]
        )

    def test_resize_is_a_different_logical_order(self) -> None:
        """A RESIZE must not be deduplicated against the original quantity."""
        resized = {**ORDER, "quantity_contracts": 150}
        assert derive_idempotency_key(**resized) != derive_idempotency_key(**ORDER)  # type: ignore[arg-type]

    def test_zero_quantity_is_rejected(self) -> None:
        with pytest.raises(IdError, match="non-zero"):
            derive_idempotency_key(**{**ORDER, "quantity_contracts": 0})  # type: ignore[arg-type]

    def test_empty_component_is_rejected(self) -> None:
        with pytest.raises(IdError, match="must not be empty"):
            derive_idempotency_key(**{**ORDER, "leg_id": ""})  # type: ignore[arg-type]

    def test_separator_injection_is_rejected(self) -> None:
        """Without this, ("a", "b") and ("a\\x1fb", "") would collide."""
        with pytest.raises(IdError, match="field separator"):
            derive_idempotency_key(**{**ORDER, "leg_id": "leg\x1f1"})  # type: ignore[arg-type]

    def test_field_boundaries_cannot_be_confused(self) -> None:
        left = derive_idempotency_key(**{**ORDER, "intent_id": "A", "leg_id": "BC"})  # type: ignore[arg-type]
        right = derive_idempotency_key(**{**ORDER, "intent_id": "AB", "leg_id": "C"})  # type: ignore[arg-type]
        assert left != right


class TestSequentialIdFactory:
    def test_ids_are_unique_and_sortable(self) -> None:
        factory = SequentialIdFactory(INSTANT)
        ids = [factory.new_id("intent") for _ in range(3)]
        assert len(set(ids)) == 3
        assert ids == sorted(ids)

    def test_replay_from_the_same_state_reproduces_ids(self) -> None:
        """Invariant 21: deterministic identifiers make replay comparable."""
        first = [SequentialIdFactory(INSTANT).new_id("order") for _ in range(1)]
        second = [SequentialIdFactory(INSTANT).new_id("order") for _ in range(1)]
        assert first == second

    def test_prefix_is_validated(self) -> None:
        with pytest.raises(IdError, match="must not be empty"):
            SequentialIdFactory(INSTANT).new_id("")

    def test_satisfies_the_id_factory_protocol(self) -> None:
        assert isinstance(SequentialIdFactory(INSTANT), IdFactory)
