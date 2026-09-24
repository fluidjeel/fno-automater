"""Durable portfolio Greek and scenario-P&L snapshots for paper telemetry."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from trading.analytics.stress import PositionStressInput, build_stress_report
from trading.broker.ports import BrokerPort
from trading.config.risk_policy import RiskPolicyConfig
from trading.domain.contracts.base import NonEmptyStr, UtcDatetime, VersionedModel
from trading.domain.contracts.common import Versions
from trading.domain.contracts.intent import TradeIntent
from trading.domain.contracts.position import PositionLegState, PositionState
from trading.domain.contracts.risk import RiskDecision
from trading.domain.contracts.snapshot import FeatureSnapshot
from trading.domain.enums import Side, TradeState
from trading.domain.ids import IdFactory
from trading.domain.primitives import Currency, Money
from trading.portfolio.exposure import PositionExposureInput, build_exposure_report
from trading.portfolio.snapshot import build_broker_snapshot

__all__ = [
    "PortfolioRiskJournal",
    "PortfolioRiskRecord",
    "build_portfolio_risk_record",
    "read_latest_portfolio_risk",
]

_BETA_VERSION = "paper-v1"
_CORRELATION_WINDOW = "60d"
_DEFAULT_BETA = Decimal("1")


class PortfolioRiskRecord(VersionedModel):
    """One decision-time portfolio risk surface."""

    as_of: UtcDatetime
    portfolio_snapshot_id: NonEmptyStr
    open_position_count: int
    margin_used: Money
    margin_available: Money
    margin_utilisation_fraction: Decimal
    exposure: dict[str, object]
    stress: dict[str, object]


class PortfolioRiskJournal:
    """Append-only daily JSONL of portfolio risk records."""

    def __init__(self, root: Path) -> None:
        self._root = root
        self._root.mkdir(parents=True, exist_ok=True)

    def path_for(self, as_of: datetime) -> Path:
        day = as_of.astimezone(UTC).strftime("%Y-%m-%d")
        return self._root / f"{day}.jsonl"

    def append(self, record: PortfolioRiskRecord) -> Path:
        path = self.path_for(record.as_of)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(record.model_dump_json() + "\n")
        return path

    def read_latest(
        self, *, before: datetime | None = None
    ) -> PortfolioRiskRecord | None:
        records = self.read(before=before)
        return records[-1] if records else None

    def read(
        self, *, before: datetime | None = None
    ) -> tuple[PortfolioRiskRecord, ...]:
        records: list[PortfolioRiskRecord] = []
        for path in sorted(self._root.glob("*.jsonl")):
            for line in path.read_text(encoding="utf-8").splitlines():
                text = line.strip()
                if not text:
                    continue
                record = PortfolioRiskRecord.model_validate_json(text)
                if before is not None and record.as_of > before:
                    continue
                records.append(record)
        records.sort(key=lambda item: item.as_of)
        return tuple(records)


def read_latest_portfolio_risk(root: Path) -> PortfolioRiskRecord | None:
    """Read the newest portfolio risk record when the journal directory exists."""
    journal_root = root / "data" / "paper" / "portfolio_risk"
    if not journal_root.is_dir():
        return None
    return PortfolioRiskJournal(journal_root).read_latest()


def build_portfolio_risk_record(
    *,
    broker: BrokerPort,
    account_id: str,
    positions: Sequence[PositionState],
    open_book: Mapping[str, tuple[TradeIntent, RiskDecision]],
    snapshots: Mapping[str, FeatureSnapshot],
    risk_policy: RiskPolicyConfig,
    id_factory: IdFactory,
    as_of: datetime,
    reserved_capital: Money | None = None,
) -> PortfolioRiskRecord:
    """Build one portfolio risk snapshot from broker truth and market quotes."""
    currency = (
        reserved_capital.currency if reserved_capital is not None else Currency.INR
    )
    reserved = (
        reserved_capital if reserved_capital is not None else Money.zero(currency)
    )
    portfolio = build_broker_snapshot(
        broker,
        account_id=account_id,
        versions=_empty_versions(),
        id_factory=id_factory,
        reserved_capital=reserved,
    )
    open_positions = tuple(
        position for position in positions if position.state is TradeState.OPEN
    )
    exposure_inputs = _exposure_inputs(open_positions, open_book, snapshots)
    stress_inputs = _stress_inputs(open_positions, open_book, snapshots)
    exposure = build_exposure_report(
        portfolio,
        exposure_inputs,
        beta_version=_BETA_VERSION,
        correlation_window=_CORRELATION_WINDOW,
    )
    stress = build_stress_report(
        as_of=as_of,
        snapshot_id=portfolio.portfolio_snapshot_id,
        equity=portfolio.exposure.equity,
        positions=stress_inputs,
        tail_budget_fraction=risk_policy.tail_budget_fraction,
    )
    margin_used = portfolio.exposure.margin_used
    margin_available = portfolio.exposure.margin_available
    total_margin = margin_used.amount + margin_available.amount
    utilisation = (
        Decimal(0)
        if total_margin <= 0
        else (margin_used.amount / total_margin).quantize(Decimal("0.000001"))
    )
    return PortfolioRiskRecord(
        as_of=as_of,
        portfolio_snapshot_id=portfolio.portfolio_snapshot_id,
        open_position_count=len(open_positions),
        margin_used=margin_used,
        margin_available=margin_available,
        margin_utilisation_fraction=utilisation,
        exposure=exposure.model_dump(mode="json"),
        stress=stress.model_dump(mode="json"),
    )


def _empty_versions() -> Versions:
    return Versions(code_version="paper-risk", config_version="0", config_checksum="0")


def _exposure_inputs(
    positions: Sequence[PositionState],
    open_book: Mapping[str, tuple[TradeIntent, RiskDecision]],
    snapshots: Mapping[str, FeatureSnapshot],
) -> tuple[PositionExposureInput, ...]:
    rows: list[PositionExposureInput] = []
    for position in positions:
        book = open_book.get(position.trade_id)
        intent = book[0] if book is not None else None
        aggregated = _aggregate_leg_greeks(position.legs, snapshots)
        notional = _position_notional(position.legs, snapshots)
        directional_sign = 0
        if aggregated.delta > 0:
            directional_sign = 1
        elif aggregated.delta < 0:
            directional_sign = -1
        underlying = position.legs[0].contract.underlying
        rows.append(
            PositionExposureInput(
                trade_id=position.trade_id,
                underlying=underlying,
                sector=_sector_for(underlying),
                expiry=position.legs[0].contract.expiry,
                delta=aggregated.delta,
                vega=aggregated.vega,
                theta=aggregated.theta,
                gamma=aggregated.gamma,
                notional=notional,
                directional_sign=directional_sign,
                event_ids=_event_ids(intent),
                beta_to_nifty=_beta_for(underlying),
            )
        )
    return tuple(rows)


def _stress_inputs(
    positions: Sequence[PositionState],
    open_book: Mapping[str, tuple[TradeIntent, RiskDecision]],
    snapshots: Mapping[str, FeatureSnapshot],
) -> tuple[PositionStressInput, ...]:
    rows: list[PositionStressInput] = []
    for position in positions:
        book = open_book.get(position.trade_id)
        intent, decision = book if book is not None else (None, None)
        max_loss = _defined_risk_max_loss(intent, decision)
        aggregated = _aggregate_leg_greeks(position.legs, snapshots)
        spot = _reference_spot(position.legs, snapshots)
        delta_pnl = Money.of(
            str((aggregated.delta * spot / Decimal(100)).quantize(Decimal("0.01"))),
            max_loss.currency,
        )
        vega_pnl = Money.of(
            str(aggregated.vega.quantize(Decimal("0.01"))),
            max_loss.currency,
        )
        rows.append(
            PositionStressInput(
                trade_id=position.trade_id,
                defined_risk_max_loss=max_loss,
                delta_pnl_per_pct=delta_pnl,
                vega_pnl_per_pct=vega_pnl,
                is_defined_risk=True,
            )
        )
    return tuple(rows)


def _defined_risk_max_loss(
    intent: TradeIntent | None, decision: RiskDecision | None
) -> Money:
    if decision is not None and decision.recalculated_max_loss is not None:
        return decision.recalculated_max_loss
    if intent is not None:
        return intent.estimated_max_loss
    return Money.zero(Currency.INR)


def _event_ids(_intent: TradeIntent | None) -> tuple[str, ...]:
    return ()


def _sector_for(underlying: str) -> str:
    if underlying in {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"}:
        return "INDEX"
    return "EQUITY"


def _beta_for(underlying: str) -> Decimal:
    if underlying == "NIFTY":
        return Decimal("1")
    if underlying == "BANKNIFTY":
        return Decimal("1.2")
    return _DEFAULT_BETA


def _lot_size(snapshot: FeatureSnapshot | None) -> int:
    if snapshot is None:
        return 1
    raw = snapshot.features.get("lot_size")
    if raw is None:
        return 1
    return max(int(raw), 1)


def _signed_contracts(leg: PositionLegState) -> int:
    qty = leg.quantity_contracts
    return qty if leg.side is Side.BUY else -qty


def _aggregate_leg_greeks(
    legs: Sequence[PositionLegState],
    snapshots: Mapping[str, FeatureSnapshot],
) -> _GreekTotals:
    delta = Decimal(0)
    vega = Decimal(0)
    theta = Decimal(0)
    gamma = Decimal(0)
    for leg in legs:
        snapshot = snapshots.get(leg.contract.symbol)
        lot_size = _lot_size(snapshot)
        signed = Decimal(_signed_contracts(leg) * lot_size)
        leg_delta, leg_vega, leg_theta, leg_gamma = _greeks_from_snapshot(snapshot)
        delta += leg_delta * signed
        vega += leg_vega * signed
        theta += leg_theta * signed
        gamma += leg_gamma * signed
    return _GreekTotals(delta=delta, vega=vega, theta=theta, gamma=gamma)


def _greeks_from_snapshot(
    snapshot: FeatureSnapshot | None,
) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    if snapshot is None or snapshot.derivatives is None:
        return Decimal(0), Decimal(0), Decimal(0), Decimal(0)
    greeks = snapshot.derivatives.greeks
    if greeks is None or not greeks.converged:
        return Decimal(0), Decimal(0), Decimal(0), Decimal(0)
    return (
        greeks.delta or Decimal(0),
        greeks.vega or Decimal(0),
        greeks.theta or Decimal(0),
        greeks.gamma or Decimal(0),
    )


def _position_notional(
    legs: Sequence[PositionLegState],
    snapshots: Mapping[str, FeatureSnapshot],
) -> Money:
    currency = Currency.INR
    total = Decimal(0)
    for leg in legs:
        snapshot = snapshots.get(leg.contract.symbol)
        lot_size = _lot_size(snapshot)
        total += leg.average_entry_price.value * Decimal(
            leg.quantity_contracts * lot_size
        )
    return Money.of(str(total.quantize(Decimal("0.01"))), currency)


def _reference_spot(
    legs: Sequence[PositionLegState],
    snapshots: Mapping[str, FeatureSnapshot],
) -> Decimal:
    for leg in legs:
        snapshot = snapshots.get(leg.contract.symbol)
        if snapshot is None:
            continue
        if (
            snapshot.derivatives is not None
            and snapshot.derivatives.underlying_price is not None
        ):
            return snapshot.derivatives.underlying_price.value
        last = snapshot.market.last
        if last is not None:
            return last.value
    return Decimal(0)


class _GreekTotals:
    __slots__ = ("delta", "gamma", "theta", "vega")

    def __init__(
        self,
        *,
        delta: Decimal,
        vega: Decimal,
        theta: Decimal,
        gamma: Decimal,
    ) -> None:
        self.delta = delta
        self.vega = vega
        self.theta = theta
        self.gamma = gamma
