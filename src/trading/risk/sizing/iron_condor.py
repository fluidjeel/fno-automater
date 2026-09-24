"""Iron condor sizing: wing loss minus net credit."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal

from trading.broker.ports import (
    MarginPreviewLeg,
    MarginPreviewPort,
    MarginPreviewRequest,
)
from trading.config.risk_policy import RiskPolicyConfig
from trading.config.schema import RiskLimits
from trading.domain.contracts.intent import IntentLeg, TradeIntent
from trading.domain.contracts.portfolio import PortfolioSnapshot
from trading.domain.contracts.sizing import SizingRequest
from trading.domain.contracts.snapshot import FeatureSnapshot
from trading.domain.enums import InstrumentKind, OptionType, Side
from trading.domain.primitives import Lots, LotSize, Money, Percent, Price, Rounding
from trading.risk.limits import (
    floor_divide_money,
    open_trade_slots,
    premium_budget_used,
    underlying_exposure_for,
)
from trading.risk.sizing.long_option import LotBounds

__all__ = [
    "IronCondorLegs",
    "IronCondorSizingEngine",
    "IronCondorSizingResult",
    "condor_legs",
    "is_iron_condor",
]


@dataclass(frozen=True, slots=True)
class IronCondorLegs:
    """The four legs of a validated iron condor."""

    short_call: IntentLeg
    long_call: IntentLeg
    short_put: IntentLeg
    long_put: IntentLeg


@dataclass(frozen=True, slots=True)
class IronCondorSizingResult:
    """Sized outcome for a four-leg iron condor."""

    bounds: LotBounds
    approved_lots: int
    cost_per_lot: Money
    estimated_margin: Money
    recalculated_max_loss: Money
    net_credit_per_unit: Price
    wing_width: Decimal
    lot_size: LotSize
    legs: IronCondorLegs


_CONDOR_LEG_COUNT = 4
_WING_LEG_COUNT = 2


def is_iron_condor(intent: TradeIntent) -> bool:
    """Return True when the intent expresses a four-leg iron condor."""
    legs = intent.legs
    if len(legs) != _CONDOR_LEG_COUNT:
        return False
    try:
        condor_legs(intent)
    except ValueError:
        return False
    return True


def condor_legs(intent: TradeIntent) -> IronCondorLegs:
    """Return validated iron condor legs."""
    if len(intent.legs) != _CONDOR_LEG_COUNT:
        raise ValueError("iron condor requires exactly four legs")
    calls = [leg for leg in intent.legs if leg.contract.option_type is OptionType.CALL]
    puts = [leg for leg in intent.legs if leg.contract.option_type is OptionType.PUT]
    if len(calls) != _WING_LEG_COUNT or len(puts) != _WING_LEG_COUNT:
        raise ValueError("iron condor requires two calls and two puts")
    short_call = next((leg for leg in calls if leg.side is Side.SELL), None)
    long_call = next((leg for leg in calls if leg.side is Side.BUY), None)
    short_put = next((leg for leg in puts if leg.side is Side.SELL), None)
    long_put = next((leg for leg in puts if leg.side is Side.BUY), None)
    if short_call is None or long_call is None or short_put is None or long_put is None:
        raise ValueError("iron condor requires one short and one long per wing")
    contracts = [leg.contract for leg in intent.legs]
    if not all(
        contract.instrument_kind is InstrumentKind.OPTION for contract in contracts
    ):
        raise ValueError("iron condor legs must be options")
    expiries = {contract.expiry for contract in contracts}
    if len(expiries) != 1:
        raise ValueError("iron condor legs must share one expiry")
    short_call_strike = short_call.contract.strike
    long_call_strike = long_call.contract.strike
    short_put_strike = short_put.contract.strike
    long_put_strike = long_put.contract.strike
    if (
        short_call_strike is None
        or long_call_strike is None
        or short_put_strike is None
        or long_put_strike is None
    ):
        raise ValueError("iron condor legs require strikes")
    if short_call_strike >= long_call_strike:
        raise ValueError("call wing must have short strike below long strike")
    if long_put_strike >= short_put_strike:
        raise ValueError("put wing must have long strike below short strike")
    if short_put_strike >= short_call_strike:
        raise ValueError(
            "iron condor requires short put strike below short call strike"
        )
    call_width = long_call_strike - short_call_strike
    put_width = short_put_strike - long_put_strike
    if call_width != put_width:
        raise ValueError("iron condor wings must have equal width")
    return IronCondorLegs(
        short_call=short_call,
        long_call=long_call,
        short_put=short_put,
        long_put=long_put,
    )


class IronCondorSizingEngine:
    """Size iron condors from wing width minus net credit."""

    def size(
        self,
        request: SizingRequest,
        leg_snapshots: Mapping[str, FeatureSnapshot],
        policy: RiskPolicyConfig,
        margin_preview: MarginPreviewPort,
        *,
        account_id: str,
        account_risk: RiskLimits,
        preview_request_id: str,
        lot_size: LotSize,
    ) -> IronCondorSizingResult:
        """Compute lots from wing loss minus credit and the min() formula."""
        intent = request.intent
        legs = condor_legs(intent)
        short_call = _leg_snapshot(leg_snapshots, legs.short_call.leg_id)
        long_call = _leg_snapshot(leg_snapshots, legs.long_call.leg_id)
        short_put = _leg_snapshot(leg_snapshots, legs.short_put.leg_id)
        long_put = _leg_snapshot(leg_snapshots, legs.long_put.leg_id)
        net_credit_per_unit = _net_credit_per_unit(
            short_call,
            long_call,
            short_put,
            long_put,
        )
        long_strike = legs.long_call.contract.strike
        short_strike = legs.short_call.contract.strike
        long_put_strike = legs.long_put.contract.strike
        short_put_strike = legs.short_put.contract.strike
        if (
            long_strike is None
            or short_strike is None
            or long_put_strike is None
            or short_put_strike is None
        ):
            raise ValueError("iron condor legs require strikes")
        put_width = short_put_strike - long_put_strike
        call_width = long_strike - short_strike
        wing_width = max(put_width, call_width)
        max_loss_value = wing_width - net_credit_per_unit.value
        if max_loss_value <= 0:
            raise ValueError("net credit exceeds wing width; max loss is undefined")
        max_loss_per_unit = Price(max_loss_value, net_credit_per_unit.tick)
        currency = request.limits.max_loss_per_trade.currency
        one_lot = Lots(1).to_quantity(lot_size)
        loss_per_lot = max_loss_per_unit.notional(one_lot, currency)
        slippage = Percent.from_fraction(policy.slippage_buffer_fraction).of(
            loss_per_lot
        )
        charges = (policy.charges_per_lot.to_money() * 2).quantized(Rounding.CEILING)
        cost_per_lot = (loss_per_lot + slippage + charges).quantized(Rounding.CEILING)

        risk_lots = floor_divide_money(request.limits.max_loss_per_trade, cost_per_lot)
        capital_budget = min(
            request.limits.strategy_allocation_remaining,
            request.limits.margin_available,
            request.limits.daily_loss_remaining,
            key=lambda money: money.amount,
        )
        capital_lots = floor_divide_money(capital_budget, cost_per_lot)
        margin_lots, margin_per_lot = self._margin_lots(
            margin_preview,
            account_id=account_id,
            preview_request_id=preview_request_id,
            legs=legs,
            lot_size=lot_size,
            margin_available=request.limits.margin_available,
        )
        portfolio_limit_lots = self._portfolio_limit_lots(
            request,
            policy,
            cost_per_lot,
            loss_per_lot,
            account_risk=account_risk,
            snapshots=(short_call, long_call, short_put, long_put),
            lot_size=lot_size,
        )
        liquidity_lots = self._liquidity_lots(
            short_call,
            long_call,
            short_put,
            long_put,
            lot_size,
        )

        bounds = LotBounds(
            risk_lots=risk_lots,
            capital_lots=capital_lots,
            margin_lots=margin_lots,
            portfolio_limit_lots=portfolio_limit_lots,
            liquidity_lots=liquidity_lots,
        )
        approved_lots = bounds.approved_lots
        recalculated_max_loss = (cost_per_lot * approved_lots).quantized(
            Rounding.CEILING
        )
        if approved_lots > 0:
            estimated_margin = (margin_per_lot * approved_lots).quantized(
                Rounding.CEILING
            )
        else:
            estimated_margin = Money.zero(currency)

        return IronCondorSizingResult(
            bounds=bounds,
            approved_lots=approved_lots,
            cost_per_lot=cost_per_lot,
            estimated_margin=estimated_margin,
            recalculated_max_loss=recalculated_max_loss,
            net_credit_per_unit=net_credit_per_unit,
            wing_width=wing_width,
            lot_size=lot_size,
            legs=legs,
        )

    @staticmethod
    def _margin_lots(
        margin_preview: MarginPreviewPort,
        *,
        account_id: str,
        preview_request_id: str,
        legs: IronCondorLegs,
        lot_size: LotSize,
        margin_available: Money,
    ) -> tuple[int, Money]:
        quantity = Lots(1).to_quantity(lot_size)
        preview = margin_preview.preview_margin(
            MarginPreviewRequest(
                request_id=preview_request_id,
                account_id=account_id,
                legs=(
                    MarginPreviewLeg(
                        contract=legs.short_call.contract,
                        side=Side.SELL,
                        quantity_contracts=quantity.contracts,
                    ),
                    MarginPreviewLeg(
                        contract=legs.long_call.contract,
                        side=Side.BUY,
                        quantity_contracts=quantity.contracts,
                    ),
                    MarginPreviewLeg(
                        contract=legs.short_put.contract,
                        side=Side.SELL,
                        quantity_contracts=quantity.contracts,
                    ),
                    MarginPreviewLeg(
                        contract=legs.long_put.contract,
                        side=Side.BUY,
                        quantity_contracts=quantity.contracts,
                    ),
                ),
            )
        )
        if not preview.confirmed or preview.margin_required.is_zero:
            return 0, Money.zero(margin_available.currency)
        margin_per_lot = preview.margin_required.quantized(Rounding.CEILING)
        lots = floor_divide_money(margin_available, margin_per_lot)
        return lots, margin_per_lot

    def _portfolio_limit_lots(
        self,
        request: SizingRequest,
        policy: RiskPolicyConfig,
        cost_per_lot: Money,
        loss_per_lot: Money,
        *,
        account_risk: RiskLimits,
        snapshots: tuple[
            FeatureSnapshot, FeatureSnapshot, FeatureSnapshot, FeatureSnapshot
        ],
        lot_size: LotSize,
    ) -> int:
        portfolio = request.portfolio_snapshot
        equity = portfolio.exposure.equity
        slots = open_trade_slots(portfolio, account_risk)
        if slots <= 0:
            return 0

        premium_budget = (equity * policy.options_premium_budget_fraction).quantized(
            Rounding.FLOOR
        )
        premium_remaining = premium_budget - premium_budget_used(portfolio)
        premium_lots = floor_divide_money(premium_remaining, cost_per_lot)

        concentration_cap = (
            equity * policy.underlying_concentration_fraction
        ).quantized(Rounding.FLOOR)
        current = underlying_exposure_for(portfolio, request.intent.underlying)
        if current is not None:
            current_notional = current.gross_notional
        else:
            current_notional = Money.zero(equity.currency)
        concentration_remaining = concentration_cap - current_notional
        concentration_lots = floor_divide_money(concentration_remaining, loss_per_lot)

        delta_lots = self._delta_limit_lots(
            policy,
            snapshots=snapshots,
            portfolio=portfolio,
            lot_size=lot_size,
        )
        return min(premium_lots, concentration_lots, delta_lots, slots)

    @staticmethod
    def _delta_limit_lots(
        policy: RiskPolicyConfig,
        *,
        snapshots: tuple[FeatureSnapshot, ...],
        portfolio: PortfolioSnapshot,
        lot_size: LotSize,
    ) -> int:
        total = Decimal(0)
        for snapshot in snapshots:
            derivatives = snapshot.derivatives
            if derivatives is None or derivatives.greeks is None:
                continue
            delta = derivatives.greeks.delta
            if delta is None:
                continue
            total += Decimal(delta)
        if total == 0:
            return 10_000
        net_delta_per_lot = abs(int(total * Decimal(lot_size.contracts_per_lot)))
        if net_delta_per_lot <= 0:
            return 10_000
        current_delta = int(portfolio.exposure.net_delta)
        headroom = policy.net_delta_limit - abs(current_delta)
        return max(headroom // net_delta_per_lot, 0)

    @staticmethod
    def _liquidity_lots(
        short_call: FeatureSnapshot,
        long_call: FeatureSnapshot,
        short_put: FeatureSnapshot,
        long_put: FeatureSnapshot,
        lot_size: LotSize,
    ) -> int:
        depths = [
            _leg_depth(snapshot)
            for snapshot in (short_call, long_call, short_put, long_put)
        ]
        resolved = [depth for depth in depths if depth is not None]
        if len(resolved) != len(depths):
            return 10_000
        depth = min(resolved)
        return max(depth // lot_size.contracts_per_lot, 0)


def _leg_snapshot(
    leg_snapshots: Mapping[str, FeatureSnapshot],
    leg_id: str,
) -> FeatureSnapshot:
    snapshot = leg_snapshots.get(leg_id)
    if snapshot is None:
        raise ValueError(f"missing feature snapshot for leg {leg_id}")
    return snapshot


def _net_credit_per_unit(
    short_call: FeatureSnapshot,
    long_call: FeatureSnapshot,
    short_put: FeatureSnapshot,
    long_put: FeatureSnapshot,
) -> Price:
    credits: list[Price] = []
    for short_feature, long_feature in ((short_call, long_call), (short_put, long_put)):
        short_bid = short_feature.market.bid
        long_ask = long_feature.market.ask
        if short_bid is None or long_ask is None:
            raise ValueError("iron condor sizing requires short bid and long ask")
        net_value = short_bid.value - long_ask.value
        if net_value <= 0:
            raise ValueError("iron condor wing credit must be positive")
        credits.append(Price(net_value, short_bid.tick))
    total = sum((price.value for price in credits), Decimal(0))
    return Price(total, credits[0].tick)


def _leg_depth(feature: FeatureSnapshot) -> int | None:
    bid_size = feature.market.bid_size
    ask_size = feature.market.ask_size
    if bid_size is None or ask_size is None:
        return None
    return min(bid_size, ask_size)
