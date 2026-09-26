"""Economic overlap and thesis conflict keys for portfolio arbitration (P12 / §11)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum, unique
from typing import TYPE_CHECKING

from trading.domain.contracts.intent import IntentLeg, TradeIntent
from trading.domain.enums import FamilyId, ModeId

if TYPE_CHECKING:
    from trading.config.discovery import DiscoveryConfig
    from trading.config.risk_policy import RiskPolicyConfig

__all__ = [
    "EconomicExposureKey",
    "ThesisDirection",
    "bands_overlap",
    "directions_conflict",
    "economic_keys_overlap",
    "extract_economic_exposure",
    "m4_open_position_cap",
]


def m4_open_position_cap(
    *,
    risk_policy: RiskPolicyConfig | None = None,
    discovery_config: DiscoveryConfig | None = None,
) -> int:
    """Resolve the portfolio M4 open-position cap from validated configuration."""
    if discovery_config is not None:
        return discovery_config.modes[
            ModeId.M4_STRATEGIC_POSITIONAL.value
        ].max_open_positions
    if risk_policy is not None:
        return risk_policy.max_m4_open_positions
    raise ValueError("risk_policy or discovery_config is required for M4 cap")


@unique
class ThesisDirection(StrEnum):
    """Coarse directional thesis for overlap and conflict checks."""

    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    NEUTRAL = "NEUTRAL"
    VOL_LONG = "VOL_LONG"


@dataclass(frozen=True, slots=True)
class EconomicExposureKey:
    """Scenario exposure bucket: underlying, expiry, direction, and strike band."""

    underlying: str
    expiry: str
    direction: ThesisDirection
    strike_low: Decimal
    strike_high: Decimal

    def normalized_band(self) -> tuple[Decimal, Decimal]:
        return self.strike_low, self.strike_high


def bands_overlap(left: EconomicExposureKey, right: EconomicExposureKey) -> bool:
    """Return True when two keys share underlying/expiry and overlapping strike bands."""
    if left.underlying != right.underlying or left.expiry != right.expiry:
        return False
    return not (
        left.strike_high < right.strike_low or right.strike_high < left.strike_low
    )


def economic_keys_overlap(
    left: EconomicExposureKey, right: EconomicExposureKey
) -> bool:
    """Same thesis bucket: matching direction and identical strike band."""
    return (
        left.underlying == right.underlying
        and left.expiry == right.expiry
        and left.direction == right.direction
        and left.direction is not ThesisDirection.NEUTRAL
        and left.strike_low == right.strike_low
        and left.strike_high == right.strike_high
    )


def directions_conflict(left: EconomicExposureKey, right: EconomicExposureKey) -> bool:
    """Opposing directional theses with overlapping strike bands."""
    if not bands_overlap(left, right):
        return False
    bullish = {ThesisDirection.BULLISH}
    bearish = {ThesisDirection.BEARISH}
    return (left.direction in bullish and right.direction in bearish) or (
        left.direction in bearish and right.direction in bullish
    )


def extract_economic_exposure(intent: TradeIntent) -> EconomicExposureKey | None:
    """Derive a coarse economic exposure key from a trade intent."""
    if not intent.legs:
        return None
    underlying = intent.underlying
    expiry = _expiry_iso(intent.legs)
    family = intent.family_id or ""
    strikes = _strikes(intent.legs)
    if not strikes:
        return None

    if family in {FamilyId.bull_call_debit.value, FamilyId.bull_put_credit.value}:
        low, high = min(strikes), max(strikes)
        return EconomicExposureKey(
            underlying=underlying,
            expiry=expiry,
            direction=ThesisDirection.BULLISH,
            strike_low=low,
            strike_high=high,
        )

    if family in {FamilyId.bear_put_debit.value, FamilyId.bear_call_credit.value}:
        low, high = min(strikes), max(strikes)
        return EconomicExposureKey(
            underlying=underlying,
            expiry=expiry,
            direction=ThesisDirection.BEARISH,
            strike_low=low,
            strike_high=high,
        )

    if family == FamilyId.long_call.value:
        strike = strikes[0]
        return EconomicExposureKey(
            underlying=underlying,
            expiry=expiry,
            direction=ThesisDirection.BULLISH,
            strike_low=strike,
            strike_high=strike,
        )

    if family == FamilyId.long_put.value:
        strike = strikes[0]
        return EconomicExposureKey(
            underlying=underlying,
            expiry=expiry,
            direction=ThesisDirection.BEARISH,
            strike_low=strike,
            strike_high=strike,
        )

    if family in {FamilyId.long_straddle.value, FamilyId.long_strangle.value}:
        low, high = min(strikes), max(strikes)
        return EconomicExposureKey(
            underlying=underlying,
            expiry=expiry,
            direction=ThesisDirection.VOL_LONG,
            strike_low=low,
            strike_high=high,
        )

    if family in {
        FamilyId.short_iron_condor_defined.value,
        FamilyId.short_iron_butterfly_defined.value,
        FamilyId.long_call_butterfly.value,
        FamilyId.long_put_butterfly.value,
    }:
        low, high = min(strikes), max(strikes)
        return EconomicExposureKey(
            underlying=underlying,
            expiry=expiry,
            direction=ThesisDirection.NEUTRAL,
            strike_low=low,
            strike_high=high,
        )

    return None


def count_m4_positions(
    positions: Sequence[object],
    *,
    pending_m4_intents: Sequence[TradeIntent] = (),
) -> int:
    """Count open or pending M4 positions plus M4 intents approved this cycle."""
    total = 0
    for item in positions:
        mode_id = _position_mode_id(item)
        state = _position_state(item)
        if mode_id is ModeId.M4_STRATEGIC_POSITIONAL and state in {
            "OPEN",
            "PENDING_ENTRY",
        }:
            total += 1
    total += sum(
        1
        for intent in pending_m4_intents
        if intent.mode_id is ModeId.M4_STRATEGIC_POSITIONAL
    )
    return total


def _expiry_iso(legs: Sequence[IntentLeg]) -> str:
    expiry = legs[0].contract.expiry
    return expiry.isoformat() if expiry is not None else "NONE"


def _strikes(legs: Sequence[IntentLeg]) -> tuple[Decimal, ...]:
    resolved: list[Decimal] = []
    for leg in legs:
        strike = leg.contract.strike
        if strike is not None:
            resolved.append(strike)
    return tuple(resolved)


def _position_mode_id(item: object) -> ModeId | None:
    from trading.domain.contracts.lifecycle import PositionLifecycleRecord
    from trading.domain.contracts.position import PositionState

    if isinstance(item, PositionLifecycleRecord):
        return item.mode_id or item.position.mode_id
    if isinstance(item, PositionState):
        return item.mode_id
    return None


def _position_state(item: object) -> str:
    from trading.domain.contracts.lifecycle import PositionLifecycleRecord
    from trading.domain.contracts.position import PositionState
    from trading.domain.enums import TradeState

    if isinstance(item, PositionLifecycleRecord):
        return item.position.state.value
    if isinstance(item, PositionState):
        return item.state.value
    return TradeState.CLOSED.value
