"""Validated configuration. Every volatile market rule lives here, not in code.

BROKER_SPEC.md: "Never hard-code remembered exchange/broker thresholds in domain
logic." PROJECT_CONTEXT.md: "volatile market rules are not buried in code."

The mechanism is VerifiedValue. A market rule is not a bare number; it is a
number plus the authority it came from plus the date someone checked it. Reading
an unverified value raises rather than returning a plausible default, because a
stale lot size or margin rule produces orders the exchange rejects, or worse,
accepts at the wrong size.

Values in the shipped configuration are deliberately null. They stay null until
verified against current official documentation.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from enum import StrEnum, unique
from typing import Generic, TypeVar
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator, model_validator

from trading.domain.contracts.base import (
    ExactDecimal,
    NonEmptyStr,
    StrictInt,
    StrictModel,
    VersionedModel,
)
from trading.domain.enums import Exchange, ReasonCode

__all__ = [
    "AppConfig",
    "ConfigNotVerifiedError",
    "Environment",
    "ExchangeRules",
    "FreshnessRules",
    "InstrumentRule",
    "MarginRules",
    "RiskLimits",
    "StorageRules",
    "VerifiedValue",
]

T = TypeVar("T", int, Decimal, str)


class ConfigNotVerifiedError(RuntimeError):
    """Raised when unverified configuration is read.

    Carries ReasonCode.CONFIG_UNVERIFIED so the caller can block entries with a
    machine-readable cause rather than a stack trace.
    """

    reason_code = ReasonCode.CONFIG_UNVERIFIED

    def __init__(self, path: str, source: str) -> None:
        super().__init__(
            f"{path} has not been verified against {source}. Confirm the current "
            "value from that authority, set it in configuration with today's "
            "verified_at date, and re-run. The system fails closed rather than "
            "assuming a default for a value the exchange or broker controls."
        )
        self.path = path
        self.source = source


@unique
class Environment(StrEnum):
    BACKTEST = "BACKTEST"
    PAPER = "PAPER"
    LIVE = "LIVE"

    @property
    def touches_real_capital(self) -> bool:
        return self is Environment.LIVE


class VerifiedValue(StrictModel, Generic[T]):
    """A market rule together with its provenance."""

    value: T | None = None
    source: NonEmptyStr
    verified_at: date | None = None
    note: str = ""

    @model_validator(mode="after")
    def _provenance_is_coherent(self) -> VerifiedValue[T]:
        if self.value is not None and self.verified_at is None:
            raise ValueError(
                f"a value sourced from {self.source} must record verified_at; an "
                "unattributed number is indistinguishable from a guess"
            )
        if self.value is None and self.verified_at is not None:
            raise ValueError("verified_at without a value is meaningless")
        return self

    @property
    def is_verified(self) -> bool:
        return self.value is not None and self.verified_at is not None

    def require(self, path: str) -> T:
        """Read the value, or fail closed naming what must be verified."""
        if self.value is None or self.verified_at is None:
            raise ConfigNotVerifiedError(path, self.source)
        return self.value


class ExchangeRules(StrictModel):
    """Per-exchange operating envelope. Every number is exchange-controlled."""

    exchange: Exchange
    timezone: NonEmptyStr
    max_orders_per_second: VerifiedValue[int]
    max_orders_per_day: VerifiedValue[int]
    algo_id_required_above_ops: VerifiedValue[int]
    session_open_local: NonEmptyStr
    session_close_local: NonEmptyStr

    @field_validator("timezone")
    @classmethod
    def _timezone_resolves(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown timezone {value!r}") from exc
        return value

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


class InstrumentRule(StrictModel):
    """Contract specifications. These change; they are never code constants."""

    symbol: NonEmptyStr
    exchange: Exchange
    lot_size: VerifiedValue[int]
    tick_size: VerifiedValue[Decimal]
    freeze_quantity: VerifiedValue[int]
    min_contract_value: VerifiedValue[Decimal]


class MarginRules(StrictModel):
    """Margin composition and penalty rules set by the regulator or exchange."""

    min_cash_fraction_of_margin: VerifiedValue[Decimal]
    expiry_day_short_option_elm_fraction: VerifiedValue[Decimal]
    margin_buffer_fraction: VerifiedValue[Decimal]


class RiskLimits(StrictModel):
    """Account policy. Owned by us, so it is required rather than verified."""

    max_loss_per_trade_fraction: ExactDecimal = Field(gt=0, le=Decimal("0.1"))
    daily_loss_cap_fraction: ExactDecimal = Field(gt=0, le=Decimal("0.2"))
    max_portfolio_risk_fraction: ExactDecimal = Field(gt=0, le=Decimal("1"))
    max_concurrent_trades: StrictInt = Field(gt=0)
    max_margin_utilisation_fraction: ExactDecimal = Field(gt=0, le=Decimal("1"))

    @model_validator(mode="after")
    def _limits_nest_correctly(self) -> RiskLimits:
        if self.max_loss_per_trade_fraction > self.daily_loss_cap_fraction:
            raise ValueError(
                f"per-trade loss cap {self.max_loss_per_trade_fraction} exceeds the "
                f"daily cap {self.daily_loss_cap_fraction}, so one trade could "
                "breach the day limit before the day limit could stop it"
            )
        if self.daily_loss_cap_fraction > self.max_portfolio_risk_fraction:
            raise ValueError("daily loss cap exceeds total portfolio risk budget")
        return self


class FreshnessRules(StrictModel):
    """Per-timeframe staleness thresholds.

    DATA_SPEC.md is explicit that there is no single universal threshold, so this
    is a mapping keyed by timeframe label rather than one number.
    """

    max_age_ms_by_timeframe: dict[NonEmptyStr, StrictInt] = Field(default_factory=dict)
    max_clock_drift_ms: StrictInt = Field(gt=0)
    warmup_bars_by_timeframe: dict[NonEmptyStr, StrictInt] = Field(default_factory=dict)
    quote_max_age_ms: StrictInt | None = Field(default=None, gt=0)
    max_leg_quote_skew_ms: StrictInt | None = Field(default=None, ge=0)
    protection_stale_escalate_after_ms: StrictInt | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def _thresholds_are_positive(self) -> FreshnessRules:
        for label, age in self.max_age_ms_by_timeframe.items():
            if age <= 0:
                raise ValueError(f"freshness threshold for {label} must be positive")
        for label, bars in self.warmup_bars_by_timeframe.items():
            if bars < 0:
                raise ValueError(f"warm-up bars for {label} must not be negative")
        return self

    def max_age_ms(self, timeframe: str) -> int:
        """Fail closed on an unconfigured timeframe rather than guessing."""
        if timeframe not in self.max_age_ms_by_timeframe:
            raise ConfigNotVerifiedError(
                f"freshness.max_age_ms_by_timeframe[{timeframe}]",
                "strategy specification and observed feed latency",
            )
        return self.max_age_ms_by_timeframe[timeframe]

    def require_quote_max_age_ms(self) -> int:
        """Fail closed when multi-leg quote age is not configured."""
        if self.quote_max_age_ms is None:
            raise ConfigNotVerifiedError(
                "freshness.quote_max_age_ms",
                "strategy specification and observed feed latency",
            )
        return self.quote_max_age_ms

    def require_max_leg_quote_skew_ms(self) -> int:
        """Fail closed when multi-leg quote skew is not configured."""
        if self.max_leg_quote_skew_ms is None:
            raise ConfigNotVerifiedError(
                "freshness.max_leg_quote_skew_ms",
                "strategy specification and observed feed latency",
            )
        return self.max_leg_quote_skew_ms

    def require_protection_stale_escalate_after_ms(self) -> int:
        """Fail closed when stale-protection escalation is not configured."""
        if self.protection_stale_escalate_after_ms is None:
            raise ConfigNotVerifiedError(
                "freshness.protection_stale_escalate_after_ms",
                "internal protection policy and observed feed latency",
            )
        return self.protection_stale_escalate_after_ms


class StorageRules(StrictModel):
    """Durability expectations. Invariant 10 blocks entries when uncertain."""

    durable_write_required_before_submit: bool = True
    raw_retention_days: StrictInt = Field(gt=0)
    audit_retention_days: StrictInt = Field(gt=0)

    @model_validator(mode="after")
    def _audit_outlives_raw_data(self) -> StorageRules:
        if self.audit_retention_days < self.raw_retention_days:
            raise ValueError(
                "audit retention is shorter than raw retention; audit lineage must "
                "outlive the data it references"
            )
        return self


class AppConfig(VersionedModel):
    """The whole validated configuration for one environment."""

    environment: Environment
    account_id: NonEmptyStr
    base_currency: NonEmptyStr = "INR"
    exchanges: tuple[ExchangeRules, ...]
    instruments: tuple[InstrumentRule, ...] = ()
    margin: MarginRules
    risk: RiskLimits
    freshness: FreshnessRules
    storage: StorageRules

    @model_validator(mode="after")
    def _identities_are_unique(self) -> AppConfig:
        if not self.exchanges:
            raise ValueError("at least one exchange must be configured")
        exchanges = [rules.exchange for rules in self.exchanges]
        if len(set(exchanges)) != len(exchanges):
            raise ValueError("each exchange may be configured only once")
        symbols = [(rule.exchange, rule.symbol) for rule in self.instruments]
        if len(set(symbols)) != len(symbols):
            raise ValueError("duplicate instrument definition")
        return self

    def exchange_rules(self, exchange: Exchange) -> ExchangeRules:
        for rules in self.exchanges:
            if rules.exchange is exchange:
                return rules
        raise ConfigNotVerifiedError(
            f"exchanges[{exchange}]", "exchange operating specifications"
        )

    def instrument_rule(self, exchange: Exchange, symbol: str) -> InstrumentRule:
        for rule in self.instruments:
            if rule.exchange is exchange and rule.symbol == symbol:
                return rule
        raise ConfigNotVerifiedError(
            f"instruments[{exchange}/{symbol}]", "broker instrument master"
        )

    def unverified_paths(self) -> tuple[str, ...]:
        """Every market rule still awaiting verification, as dotted paths."""
        pending: list[str] = []
        for rules in self.exchanges:
            prefix = f"exchanges[{rules.exchange.value}]"
            for name in (
                "max_orders_per_second",
                "max_orders_per_day",
                "algo_id_required_above_ops",
            ):
                if not getattr(rules, name).is_verified:
                    pending.append(f"{prefix}.{name}")
        for rule in self.instruments:
            prefix = f"instruments[{rule.exchange.value}/{rule.symbol}]"
            for name in (
                "lot_size",
                "tick_size",
                "freeze_quantity",
                "min_contract_value",
            ):
                if not getattr(rule, name).is_verified:
                    pending.append(f"{prefix}.{name}")
        for name in (
            "min_cash_fraction_of_margin",
            "expiry_day_short_option_elm_fraction",
            "margin_buffer_fraction",
        ):
            if not getattr(self.margin, name).is_verified:
                pending.append(f"margin.{name}")
        return tuple(pending)

    def require_ready_for(self, environment: Environment) -> None:
        """Fail closed unless every market rule is verified for a live account."""
        if not environment.touches_real_capital:
            return
        pending = self.unverified_paths()
        if pending:
            raise ConfigNotVerifiedError(
                ", ".join(pending), "current official exchange and broker documentation"
            )
